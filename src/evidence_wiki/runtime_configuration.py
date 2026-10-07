"""Explicit private host runtime loading without environment-selected code or prompts."""

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from ._pack_io import json_document
from .agent_execution import (
    BoundedSubprocessExecutor,
    ExecutionProfile,
    ExecutionRole,
    ExecutionRoleLimit,
    ExecutionSchedule,
)
from .agent_qualification import HostReviewSigner, QualificationAuthority, QualifiedEvaluators, SigningIdentity
from .errors import ConfigError, EvidenceWikiError
from .execution_roles import ExecutionRoleRegistry, ExecutionRoleScope
from .host_context import HostContext
from .research_execution import ResearchExecution
from .run_policy import RunPolicy

SCHEMA = "research-host-runtime/v1"


def refuse(reason):
    raise ConfigError("RESEARCH_RUNTIME_REFUSED", "Explicit host runtime could not be loaded.",
        details={"reason": reason}, recoverable=False, remediation="Use an owner-private explicit runtime file and native policy/service identities; no automatic authority or credential setup occurs.")


def _fields(value, required):
    if type(value) is not dict or set(value) != set(required):
        refuse("runtime_shape_invalid")
    return value


def _key(value):
    if type(value) is not str or re.fullmatch(r"[a-f0-9]{64,128}", value) is None or len(value)%2:
        refuse("private_key_invalid")
    return bytes.fromhex(value)


def _profile(value):
    base = {"schema_version", "profile_ref", "total_steps", "currency", "pricing_ref", "roles", "job_budgets", "cleanup_timeout_seconds"}
    optional = {"scheduling", "role_scopes", "required_diversity"}
    if (type(value) is not dict or not base <= set(value) or set(value)-base-optional
            or value["schema_version"] not in {"research-execution-profile/v1", "research-execution-profile/v2", "research-execution-profile/v3"}):
        refuse("runtime_profile_unsupported")
    roles = tuple(ExecutionRole(**_fields(row, {"config_ref","role","controller_ref","model_ref","prompt_version"})) for row in value["roles"])
    budget = _fields(value["job_budgets"], {"model_calls","tool_calls","tokens","cost"})
    scopes = None
    if "role_scopes" in value:
        registry = _fields(value["role_scopes"], {"schema_version","scopes"})
        scopes = ExecutionRoleRegistry(tuple(ExecutionRoleScope(row["config_ref"], row["task_role"], tuple(row["capabilities"])) for row in registry["scopes"]))
        if scopes.document() != registry:
            refuse("runtime_scope_unsupported")
    schedule = None
    if "scheduling" in value:
        data = value["scheduling"]
        limits = [ExecutionRoleLimit(row["role"], row["controller_ref"], row["budgets"]["model_calls"], row["budgets"]["tool_calls"],
            row["budgets"]["tokens"], row["budgets"]["cost"], row["budgets"]["seconds"]) for row in data["role_limits"]]
        schedule = ExecutionSchedule(tuple(limits), tuple(data["retry_reasons"]), data["backoff_seconds"], tuple(tuple(row) for row in data["failover"]))
    profile = ExecutionProfile(value["profile_ref"], value["total_steps"], value["currency"], value["pricing_ref"], roles,
        budget["model_calls"], budget["tool_calls"], budget["tokens"], budget["cost"], value["cleanup_timeout_seconds"],
        schedule=schedule, role_scopes=scopes, required_diversity=tuple(value.get("required_diversity", ())))
    if profile.document() != value:
        refuse("runtime_profile_not_reproduced")
    return profile


class NativeProcessMeter:
    """Known local process/tool lifetime; no model, tokens or provider charge claim."""

    def finalize(self, _attempt, summary):
        return {"seconds": summary["seconds"], "model_calls": 0, "tool_calls": 1, "tokens": 0, "cost": "0"}


@dataclass(frozen=True)
class HostRuntime:
    """Owner-selected services shared by Python and CLI; no worker-created grants."""

    policy: RunPolicy
    context: HostContext = field(repr=False)
    execution: ResearchExecution = field(repr=False)

    def __post_init__(self):
        if type(self.policy) is not RunPolicy or type(self.context) is not HostContext or type(self.execution) is not ResearchExecution:
            refuse("runtime_services_invalid")

    @classmethod
    def from_services(cls, policy, context, execution):
        return cls(policy, context, execution)

    @classmethod
    def from_file(cls, value):
        if value == "-":
            refuse("stdin_runtime_forbidden")
        descriptors = []
        try:
            supplied = Path(value).expanduser().absolute()
            if supplied.is_symlink() or ".." in supplied.parts:
                refuse("runtime_path_invalid")
            if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
                refuse("private_runtime_platform_unsupported")
            descriptor = os.open(supplied.anchor, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            descriptors.append(descriptor)
            for part in supplied.parts[1:-1]:
                descriptor = os.open(part, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=descriptor)
                descriptors.append(descriptor)
            file = os.open(supplied.name, os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK, dir_fd=descriptor)
            descriptors.append(file)
            before = os.fstat(file)
            if (not stat.S_ISREG(before.st_mode) or before.st_mode & 0o077 or before.st_nlink != 1
                    or before.st_uid != os.geteuid() or before.st_size > 1_048_576):
                refuse("runtime_file_not_private")
            chunks, size = [], 0
            while chunk := os.read(file, min(65536, 1_048_577-size)):
                size += len(chunk)
                if size > 1_048_576:
                    refuse("runtime_file_bound")
                chunks.append(chunk)
            after = os.fstat(file)
            current = os.stat(supplied.name, dir_fd=descriptor, follow_symlinks=False)
            fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
            frozen = tuple(getattr(before, name) for name in fields)
            if any(tuple(getattr(view, name) for name in fields) != frozen for view in (after, current)):
                refuse("runtime_file_changed")
            for parent, child, part in zip(descriptors[:-2], descriptors[1:-1], supplied.parts[1:-1], strict=True):
                actual, held = os.stat(part, dir_fd=parent, follow_symlinks=False), os.fstat(child)
                if (actual.st_dev,actual.st_ino)!=(held.st_dev,held.st_ino):
                    refuse("runtime_path_changed")
            return cls.from_document(json_document(b"".join(chunks)))
        except EvidenceWikiError:
            raise
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            refuse("runtime_file_unavailable_or_invalid")
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @classmethod
    def from_document(cls, data):
        try:
            _fields(data, {"schema_version","policy","context","profile","commands","cwd","meter","keys","qualification_authority","qualification_records","review_signer"})
            if data["schema_version"] != SCHEMA or data["meter"] not in {"native_process", "unobserved"}:
                refuse("runtime_version_or_meter_unsupported")
            policy, profile = RunPolicy(data["policy"]), _profile(data["profile"])
            context = HostContext(**data["context"])
            keys = _fields(data["keys"], {"observer","receipt"})
            declared = _fields(data["qualification_authority"], {"issuer_ref","key","generator_controller_ref"})
            authority = QualificationAuthority(declared["issuer_ref"], _key(declared["key"]), declared["generator_controller_ref"])
            qualifications = QualifiedEvaluators(data["qualification_records"], authority)
            signing = _fields(data["review_signer"], {"authority_policy_id","authority_revision","generator","reviewers","expires_utc","not_before_utc"})
            def identity(row):
                row = _fields(row, {"config_ref","principal","key_id","controller_ref","role","key"})
                return SigningIdentity(**{**row,"key":_key(row["key"])})
            signer = HostReviewSigner(signing["authority_policy_id"], signing["authority_revision"], identity(signing["generator"]),
                tuple(identity(row) for row in signing["reviewers"]), signing["expires_utc"], signing["not_before_utc"])
            executor = BoundedSubprocessExecutor(profile, commands=data["commands"], cwd=Path(data["cwd"]),
                receipt_key=_key(keys["receipt"]), meter=NativeProcessMeter() if data["meter"] == "native_process" else None)
            return cls.from_services(policy, context, ResearchExecution(executor, qualifications=qualifications,
                review_signer=signer, observer_key=_key(keys["observer"])))
        except EvidenceWikiError:
            raise
        except (ValueError, TypeError, KeyError, OSError):
            refuse("runtime_configuration_invalid")
