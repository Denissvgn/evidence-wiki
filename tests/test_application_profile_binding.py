"""Application allocation bindings use the native profile byte contract."""

import copy

import pytest

from tests._native_application_profile_fixture import native_application_profile
from tests.test_selective_review_workload import cohort as cohort
from tests.test_selective_review_workload import pin as pin
from tools._execution_budget import _application_profile_sha256
from tools._review_dependency_workload import digest


@pytest.mark.parametrize("version,seconds", [("v1", 600), ("v2", 3600)])
def test_application_profile_hash_matches_native_owner_and_refuses_workload_hash(version, seconds):
    profile = native_application_profile({"schema_version": "research-review-application-lease/" + version,
        "seconds": seconds, "not_after_utc": "2099-01-01T00:00:00+00:00"})
    document = profile.document()
    assert _application_profile_sha256(document) == profile.sha256
    assert _application_profile_sha256(document) != digest(document)
    altered = copy.deepcopy(document)
    altered["profile_ref"] += "-changed"
    assert _application_profile_sha256(altered) != profile.sha256


@pytest.mark.parametrize("version", [None, "research-execution-profile/v15", "research-execution-profile/v18"])
def test_application_profile_hash_refuses_unknown_schema(version):
    with pytest.raises(ValueError, match="execution_runtime_binding_invalid"):
        _application_profile_sha256({"schema_version": version})


@pytest.mark.parametrize("change", [None, "workload_hash", "profile_tamper", "schema", "lease"])
def test_formal_allocation_uses_native_profile_hash_before_runtime_configuration(cohort, monkeypatch, change):
    from datetime import datetime, timedelta

    from evidence_wiki import HostRuntime
    from tools import _review_dependency_accounting as accounting
    from tools import _review_journal_artifacts as journals
    from tools import run_review_dependency_workload as workload
    from tools._execution_budget import execution_policy, validate_workload_allocation_v5

    plan, _pair = cohort
    policy = execution_policy("eight-hour")
    caps = {**plan["phase_caps"], "job": 1000}
    plan.update(schema_version=accounting.EXTENDED_PLAN, execution_policy=policy,
        execution_policy_sha256=digest(policy), phase_caps=caps,
        formal_application_policy=workload.formal_application_policy(1000),
        expires_at=(datetime.fromisoformat(plan["frozen_at"]) + timedelta(hours=8)).isoformat())
    lease = {**plan["formal_application_policy"]["application_lease"], "not_after_utc": plan["expires_at"]}
    profile = native_application_profile(lease)
    allocation = dict.fromkeys(("schema_version", "allocation_binding", "run_policy", "profile", "authority_raw_sha256",
        "authority_start", "authority_end", "realized_sources", "realized_claims_sha256", "realized_input_manifest_sha256",
        "worker_command_manifest", "issued_before_worker_outputs", "execution_policy", "execution_policy_sha256", "credential_expiries"))
    allocation.update(schema_version="review-workload-runtime-allocation/v2", profile=profile.document(),
        review_application_binding={"profile_sha256": profile.sha256, "lease": lease})
    if change == "workload_hash":
        allocation["review_application_binding"]["profile_sha256"] = digest(allocation["profile"])
    elif change == "profile_tamper":
        allocation["profile"]["profile_ref"] += "-changed"
    elif change == "schema":
        allocation["profile"]["schema_version"] = "research-execution-profile/v18"
    elif change == "lease":
        allocation["profile"]["review_application_lease"]["seconds"] -= 1

    # Isolate preceding retained-plan/pin admission; keep the execution, lease,
    # profile-binding and native-runtime type checks under test unchanged.
    monkeypatch.setattr(accounting, "validate_plan", lambda value: value)
    limits = workload.runtime_hard_limits(plan["observation_policy"], policy, phase_caps=caps, ceiling_binding=plan)
    limits_raw = accounting.canonical({"hard_limits": limits, "source_protocol_limits": workload.HARD_LIMITS})
    monkeypatch.setattr(accounting, "read_pin", lambda *_args, **_kwargs: limits_raw)
    monkeypatch.setattr(workload, "read_pinned_artifact", lambda _pin: ({"host_id": "direct"}, b""))
    reached = []

    class ApplicationBindingAccepted(Exception):
        pass

    def stop_before_runtime_configuration(_plan):
        reached.append("application_binding_accepted")
        raise ApplicationBindingAccepted

    monkeypatch.setattr(journals, "selected", stop_before_runtime_configuration)
    runtime = object.__new__(HostRuntime)
    if change is None:
        with pytest.raises(ApplicationBindingAccepted):
            validate_workload_allocation_v5(allocation, plan, runtime, {"private_roots": []})
        assert reached == ["application_binding_accepted"]
    else:
        with pytest.raises(ValueError, match="execution_runtime_binding_invalid"):
            validate_workload_allocation_v5(allocation, plan, runtime, {"private_roots": []})
        assert reached == []
