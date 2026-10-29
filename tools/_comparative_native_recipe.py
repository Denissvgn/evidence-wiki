"""Bounded public native support, kept separate from independently authored answers."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools._comparative_protocol import (
    ARMS,
    MAX_PACKET_BYTES,
    _bounded_copy,
    _budgets,
    _public_configuration,
    _sources,
    normalize_answer,
)
from tools._journey_cases import canonical, digest, require

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "unattended-host"
FIXTURE_SOURCE_TEXT = "Measured solar reflectance was 0.74.\n"
MAX_STEPS = 64
MAX_SECONDS = 900
MAX_RECIPE_BYTES = 1_048_576
_PACKET_FIELDS = {"schema_version", "original_text", "sources", "source_availability", "conditions", "budgets",
                  "interface_guide", "answer_contract"}
_SOURCE_FIELDS = {"source_id", "content", "provenance", "rights", "content_sha256"}


def _checked_packet(packet):
    packet = _bounded_copy(packet, MAX_PACKET_BYTES, "recipe_packet_bound")
    require(type(packet) is dict and set(packet) == _PACKET_FIELDS, "recipe_packet_shape_invalid")
    require(packet["schema_version"] == "evidence-comparative-generator/v1", "recipe_packet_version_invalid")
    require(type(packet["original_text"]) is str and 0 < len(packet["original_text"].encode()) <= 16_384,
            "recipe_original_invalid")
    _budgets(packet["budgets"])
    _public_configuration(packet["conditions"])
    require(type(packet["sources"]) is list and all(type(row) is dict and set(row) == _SOURCE_FIELDS
            for row in packet["sources"]), "recipe_sources_invalid")
    sources = []
    for row in packet["sources"]:
        require(type(row["content"]) is str, "recipe_source_content_unavailable")
        require(hashlib.sha256(row["content"].encode()).hexdigest() == row["content_sha256"],
                "recipe_source_digest_mismatch")
        sources.append({key: value for key, value in row.items() if key != "content_sha256"})
    _sources(sources)
    expected_availability = {"usable": sum(row["rights"]["status"] == "allowed" for row in sources),
                             "unusable": sum(row["rights"]["status"] != "allowed" for row in sources)}
    require(packet["source_availability"] == expected_availability, "recipe_source_availability_mismatch")
    guide = packet["interface_guide"]
    require(type(guide) is dict and set(guide) == {"content", "descriptor", "sha256"}, "recipe_guide_invalid")
    require((guide["content"] is None) != (guide["descriptor"] is None), "recipe_guide_invalid")
    if guide["content"] is not None:
        require(type(guide["content"]) is str and hashlib.sha256(guide["content"].encode()).hexdigest() == guide["sha256"],
                "recipe_guide_digest_mismatch")
    require(type(packet["answer_contract"]) is dict and
            packet["answer_contract"].get("offset_unit") == "unicode_code_point", "recipe_answer_contract_invalid")
    return packet, sources


@dataclass(frozen=True)
class FrozenSourceReferenceArtifacts:
    """Detached immutable supplied-source bytes; source text remains inert data."""

    artifact_bytes: bytes = field(repr=False)

    @classmethod
    def from_packet(cls, packet):
        _, sources = _checked_packet(packet)
        return cls(canonical(sources))

    @property
    def sha256(self):
        return hashlib.sha256(self.artifact_bytes).hexdigest()

    def sources(self):
        return json.loads(self.artifact_bytes)

    def document(self):
        return {"schema_version": "evidence-comparative-frozen-sources/v1", "sha256": self.sha256,
                "sources": [{"source_id": row["source_id"],
                             "content_sha256": hashlib.sha256(row["content"].encode()).hexdigest(),
                             "rights": row["rights"], "provenance": row["provenance"]}
                            for row in self.sources()]}


def _fixture_provision(directory, original_text):
    """Load the fixed public example as an opaque trusted host service factory."""
    previous = sys.modules.get("support")
    try:
        support_spec = importlib.util.spec_from_file_location("support", EXAMPLE / "support.py")
        support = importlib.util.module_from_spec(support_spec)
        sys.modules["support"] = support
        support_spec.loader.exec_module(support)
        fixture_spec = importlib.util.spec_from_file_location("comparative_fixture_host", EXAMPLE / "fixture_host.py")
        fixture = importlib.util.module_from_spec(fixture_spec)
        fixture_spec.loader.exec_module(fixture)
    finally:
        if previous is None:
            sys.modules.pop("support", None)
        else:
            sys.modules["support"] = previous
    return fixture.provision(directory, original_text, scenario="repair")


def _error_document(error):
    from evidence_wiki.errors import EvidenceWikiError

    if isinstance(error, EvidenceWikiError):
        return {"kind": "native_refusal", "error_code": error.error_code}
    return {"kind": "host_failure", "error_code": "COMPARATIVE_NATIVE_CONFIGURATION_UNAVAILABLE"}


def _invoke(calls, operation, call, *, arguments=None):
    record = {"sequence": len(calls), "operation": operation, "arguments": arguments or {},
              "status": "started", "result_sha256": None, "error": None}
    calls.append(record)
    started = time.monotonic()
    try:
        result = call()
    except (Exception, SystemExit) as error:
        record.update(status="refused", error=_error_document(error), elapsed_seconds=time.monotonic() - started)
        raise
    record.update(status="returned", elapsed_seconds=time.monotonic() - started)
    if type(result) in {dict, list, str, int, float, bool} or result is None:
        record["result_sha256"] = digest(result)
    return result


def _manual_drive(session, calls, control, *, max_steps, steps):
    for index in range(max_steps):
        control.checkpoint("comparative_manual_step")
        _invoke(calls, "ResearchSession.current", session.current)
        work = _invoke(calls, "ResearchSession.next", session.next)
        # Public next() exposes the current native next_work owner and basis.
        basis = work["current"]["basis_id"]
        arguments = {"request_id": "manual-" + str(index), "expected_basis_id": basis}
        control.checkpoint("comparative_manual_advance")
        step = _invoke(calls, "ResearchSession.advance", lambda arguments=arguments: session.advance(**arguments),
                       arguments=arguments)
        steps.append(step)
        if step["terminal"]:
            break
        if step.get("action") == "reconcile" and not step.get("historical"):
            time.sleep(min(0.02, control.remaining_seconds("comparative_manual_poll")))
    return steps


def _read_outputs(session, calls, destination):
    destination["current"] = _invoke(calls, "ResearchSession.current", session.current)
    destination["brief"] = _invoke(calls, "ResearchSession.brief", session.brief)
    destination["report"] = _invoke(calls, "ResearchSession.report", lambda: session.report(max_records=64),
                                   arguments={"max_records": 64})


def _source_binding(root, frozen):
    raw = (root / "raw" / "papers" / "study.md").read_bytes()
    records = [json.loads(line) for line in (root / "sources" / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    require(len(records) == 1, "recipe_native_source_inventory_changed")
    supplied = frozen.sources()[0]
    return {"packet_source_id": supplied["source_id"], "native_source_id": records[0]["id"],
            "packet_content_sha256": hashlib.sha256(supplied["content"].encode()).hexdigest(),
            "native_raw_sha256": hashlib.sha256(raw).hexdigest(), "content_exact": raw == supplied["content"].encode()}


def _refresh(workspace, runtime, calls, control, *, max_steps):
    from evidence_wiki.errors import EvidenceWikiError

    arguments = {"run_id": "comparative-refresh", "previous_run_id": "comparative", "request_id": "refresh",
                 "max_steps": max_steps, "planner_config_ref": "planner", "source_updates": ()}
    try:
        result = _invoke(calls, "ResearchNamespace.refresh", lambda: workspace.research.refresh(
            runtime.policy, execution=runtime.execution, operation_control=control, **arguments), arguments=arguments)
        refreshed = {"request": {**arguments, "source_updates": []}, "result": result,
                     "current": None, "brief": None, "report": None,
                     "qualification": "separate_same_source_native_lineage; no changed-evidence observation"}
        with workspace.research.session(runtime.policy, run_id=arguments["run_id"], execution=runtime.execution) as session:
            _read_outputs(session, calls, refreshed)
        return refreshed
    except EvidenceWikiError as error:
        return {"request": {**arguments, "source_updates": []}, "result": None, "error": _error_document(error),
                "qualification": "separate_native_refresh_refusal; no changed-evidence observation"}


def _retain_output(directory, rendered):
    """Retain the exact bounded return bytes without replacing an earlier result."""
    path = directory / "native-support-result.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def execute_recipe(packet, *, arm, output_directory=None, proposed_answer=None, max_steps=64,
                   refresh=False, refresh_max_steps=8):
    """Execute one declared interface; never author or grade the common answer.

    Native execution uses synthetic public-example services. Its qualified
    internal policy is preserved. The common outer research deadline and call
    bound do not establish matched native/provider usage, model identity, or
    task conformance. A document-only answer needs no native approval.
    A native fixture directory retains the exact returned canonical bytes in
    ``native-support-result.json`` for independent host hashing and custody.
    """
    packet, sources = _checked_packet(packet)
    require(arm in ARMS, "recipe_arm_invalid")
    require(type(max_steps) is int and 1 <= max_steps <= MAX_STEPS, "recipe_step_bound")
    require(type(refresh) is bool and type(refresh_max_steps) is int and 1 <= refresh_max_steps <= 8,
            "recipe_refresh_bound")
    frozen = FrozenSourceReferenceArtifacts(canonical(sources))
    answer = None if proposed_answer is None else normalize_answer(
        proposed_answer, original_text=packet["original_text"], sources=sources)
    support = {"schema_version": "evidence-comparative-native-support/v1", "arm": arm,
        "observation_class": "document_only" if arm == "document_only" else "synthetic_native_execution",
        "native_approval": "not_applicable" if arm == "document_only" else "unobserved",
        "executed": False, "setup": None, "operation_calls": [], "cycle": None, "steps": [],
        "operation_call_scope": "explicit recipe provision, driver, and collection calls; context/workspace/session entry and factory-internal calls are not enumerated",
        "current": None, "brief": None, "report": None, "refresh": None, "error": None,
        "frozen_source_artifacts": frozen.document(), "source_binding": None, "native_receipt_refs": [],
        "native_receipt_reference_kind": "bounded native report record, snapshot, and accounting commitments",
        "common_conditions_sha256": digest(packet["conditions"]), "common_budgets": packet["budgets"],
        "outer_drive_bounds": {"seconds": min(MAX_SECONDS, packet["budgets"]["seconds"]), "steps": max_steps},
        "native_policy": None, "native_execution_profile": None,
        "provider_usage": {"model_calls": None, "tool_calls": None, "tokens": None, "cost": None},
        "qualification": {"native_models": "not_applicable" if arm == "document_only" else "synthetic",
            "native_meter": "not_applicable" if arm == "document_only" else "synthetic",
            "native_internal_caps_matched": None if arm == "document_only" else False,
            "common_answer_conformance": "requires_separate_blind_judgment"}}
    limitations = ["The common answer, when supplied, is independent of native approval and is not graded here."]
    if arm != "document_only":
        require(output_directory is not None, "recipe_native_directory_required")
        directory = Path(output_directory).absolute()
        require(directory.resolve() == directory and not directory.is_symlink() and not directory.exists(),
                "recipe_native_directory_must_be_new_canonical")
        require(not directory.is_relative_to(ROOT) and not ROOT.is_relative_to(directory),
                "recipe_native_directory_must_be_outside_checkout")
        require(len(sources) == 1 and sources[0]["rights"]["status"] == "allowed" and
                sources[0]["content"] == FIXTURE_SOURCE_TEXT,
                "recipe_native_fixture_source_unsupported")
        from evidence_wiki import OperationControl, Workspace

        seconds = support["outer_drive_bounds"]["seconds"]
        control = OperationControl(deadline_utc=(datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(),
                                   timeout_seconds=seconds)
        calls = support["operation_calls"]
        try:
            # The selected host factory owns credentials; neither runtime nor
            # private authority files are serialized into comparison output.
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                root, runtime, setup = _invoke(calls, "public_example.provision", lambda: _fixture_provision(
                    directory, packet["original_text"]), arguments={"scenario": "repair"})
                support.update(setup=setup, native_policy=runtime.policy.to_dict(),
                               native_execution_profile=runtime.execution.profile.document(),
                               source_binding=_source_binding(root, frozen))
                require(support["source_binding"]["content_exact"], "recipe_native_source_mismatch")
                control.checkpoint("comparative_after_provision")
                with runtime.context.activate(), Workspace.open(root) as workspace:
                    support["executed"] = True
                    if arm == "low_level":
                        with workspace.research.session(runtime.policy, run_id="comparative", execution=runtime.execution) as session:
                            _manual_drive(session, calls, control, max_steps=max_steps, steps=support["steps"])
                            _read_outputs(session, calls, support)
                    else:
                        arguments = {"run_id": "comparative", "request_id": "drive", "max_steps": max_steps}
                        support["cycle"] = _invoke(calls, "ResearchNamespace.run_cycle", lambda: workspace.research.run_cycle(
                            runtime.policy, execution=runtime.execution, operation_control=control, **arguments), arguments=arguments)
                        with workspace.research.session(runtime.policy, run_id="comparative", execution=runtime.execution) as session:
                            _read_outputs(session, calls, support)
                    support["native_approval"] = "separate_native_current"
                    if refresh:
                        support["refresh"] = _refresh(workspace, runtime, calls, control, max_steps=refresh_max_steps)
        except (Exception, SystemExit) as error:
            support["error"] = _error_document(error)
        finally:
            control.close()
        limitations.extend([
            "Native workers and meters are synthetic protocol support, not a real provider/model quality observation.",
            "The original qualified native policy/profile caps are retained; matched native internal caps are not established.",
            "The outer deadline is checked around native operations; the public fixture provisioner itself does not accept that control.",
            "The manual arm may retain a nonterminal run at its call bound; only recorded owners can establish a terminal outcome.",
            "Native acceptance of a fixture claim does not establish that the independently authored answer addresses the original.",
        ])
        if refresh:
            limitations.append("Refresh is additional unmatched same-source lineage evidence, not a changed-evidence comparison.")
        references = []
        reports = [support["report"]]
        if support["refresh"]:
            reports.append(support["refresh"].get("report"))
        for report in reports:
            if report is not None:
                references.append(report["snapshot_ref"])
                if report.get("accounting"):
                    references.append(report["accounting"]["audit_ref"])
                references.extend(row["record_ref"] for row in report["trace"])
        support["native_receipt_refs"] = list(dict.fromkeys(ref.removeprefix("sha256:") for ref in references))[:32]
    result = {"schema_version": "evidence-comparative-recipe/v1", "answer": answer,
              "native_support": support, "limitations": limitations}
    rendered = canonical(result)
    require(len(rendered) <= MAX_RECIPE_BYTES, "recipe_result_bound")
    if arm != "document_only" and directory.is_dir():
        _retain_output(directory, rendered)
    return json.loads(rendered)
