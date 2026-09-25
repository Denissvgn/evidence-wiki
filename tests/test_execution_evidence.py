"""Original execution bytes, independently authenticated outcomes, and consumer gates."""

from __future__ import annotations

import contextlib
import copy
import io
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from evidence_wiki._filesystem import os
from tests._execution_fixture import (
    KEYS,
    NOW,
    SOURCE_ID,
    authenticate,
    binding,
    canonical,
    example,
    host_policy,
    identifier,
    independently_recalculate,
    workspace,
)
from tests._script_loader import load_isolated_module

SCRIPTS = Path(__file__).resolve().parents[1] / "workspace-template/scripts"
EXECUTION = load_isolated_module("execution_intake", SCRIPTS / "_execution_evidence.py")
NORMALIZE = load_isolated_module("execution_normalize", SCRIPTS / "normalize_sources.py")
VERIFY = load_isolated_module("execution_verify", SCRIPTS / "normalize_verify.py")
LINT = load_isolated_module("execution_lint", SCRIPTS / "lint.py")
POLICY = load_isolated_module("execution_policy", SCRIPTS / "_evidence_policies.py")
REVISION = load_isolated_module("execution_revision", SCRIPTS / "_evidence_revision.py")


@pytest.fixture
def trusted(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    path = tmp_path / "authority.json"
    policy = host_policy(path)
    monkeypatch.setenv("EVIDENCE_WIKI_AUTHORITY_FILE", str(path.resolve()))
    config, record, folder = workspace(root)
    return root, config, record, folder, path, policy


def normalize(root, config, record):
    eligible = NORMALIZE.EligibleRecord(record=copy.deepcopy(record), method=NORMALIZE.normalization_method(root, record))
    source = NORMALIZE.normalize_selected_record(root, config, eligible)
    path, _result = NORMALIZE.write_normalized_source(
        source, root / "sources/normalized", "sources/manifest.jsonl", "2026-09-10", project_root=root,
        force=True, normalized_at="2026-09-10T00:00:00Z",
    )
    return source, path


def consumer_results(root, config, record, path):
    frontmatter, _body = LINT.load_frontmatter(path)
    verification = VERIFY.build_report(root)
    findings = {"issues": []}
    _foreign, violations, _thin = LINT.check_normalized_record_contract(root, path.parent, {SOURCE_ID: record}, findings, config=config)
    inputs = POLICY.PolicyInputs(root, config, {SOURCE_ID: record}, {SOURCE_ID: frontmatter}, {}, [], {}, {}, {})
    reasons = POLICY.source_unusable_evidence_reasons(inputs, SOURCE_ID)
    verdict = POLICY.evaluate_source_policy("independent_execution_pass", [SOURCE_ID], inputs, now=NOW)
    return verification, violations, reasons, verdict


def test_failed_observation_fix_and_recalculated_pass_remain_distinct(trusted):
    root, config, record, folder, _path, _policy = trusted
    files = {path.name: path.read_bytes() for path in folder.iterdir()}
    with patch("subprocess.run", side_effect=AssertionError("receipts are inert")), patch("socket.create_connection", side_effect=AssertionError("offline")):
        report = EXECUTION.inspect_execution(root, config, record)
        assert report["valid"], report
        assessment = EXECUTION.assess_verification(report, root, config, NOW)
    assert [item["outcome"] for item in report["records"]] == ["failed", "inconclusive", "passed"]
    assert [item["record_type"] for item in report["records"]] == ["observation", "hypothesis", "observation"]
    assert [item["outcome"] for item in report["receipts"]] == ["failed", "passed"]
    assert len({item["record_id"] for item in report["records"]}) == 3
    assert report["authority"] == "not_evaluated"
    for item in report["records"]:
        if item["record_type"] == "observation":
            expected, actual, outcome = independently_recalculate(files, item["payload"])
            assert expected == "9.50"
            assert outcome == item["outcome"]
            assert (actual == expected) == (outcome == "passed")
    assert assessment["eligible"], assessment
    assert [item["independent"] for item in assessment["receipts"]] == [True, True]
    assert [item["positive_eligible"] for item in assessment["receipts"]] == [False, True]
    source, path = normalize(root, config, record)
    assert source.record["metadata"]["execution_evidence"] == report
    verification, violations, reasons, verdict = consumer_results(root, config, record, path)
    assert verification["overall_result"] == "verified", verification
    assert violations == 0 and not reasons
    assert verdict.verdict == "pass", verdict
    serialized = json.dumps(assessment)
    assert all(secret not in serialized for secret in KEYS.values())


def test_failed_execution_is_usable_without_becoming_a_positive_verification(trusted):
    root, config, _record, folder, _path, _policy = trusted
    for child in folder.iterdir():
        child.unlink()
    files, _ = example(history=False, outcome="failed")
    config, record, _ = workspace(root, files)
    source, path = normalize(root, config, record)
    assert NORMALIZE.status_for(source) == "content_extracted"
    assert NORMALIZE.record_unusable_evidence_reasons(source.record) == []
    verification, violations, reasons, verdict = consumer_results(root, config, record, path)
    assert verification["overall_result"] == "verified"
    assert violations == 0 and not reasons
    assert verdict.verdict == "fail"
    report = source.record["metadata"]["execution_evidence"]
    assessment = EXECUTION.assess_verification(report, root, config, NOW)
    assert assessment["receipts"][0]["independent"]
    assert assessment["receipts"][0]["outcome"] == "failed"


@pytest.mark.parametrize("change", ["input", "cost", "parameter", "output", "patch", "environment", "log", "suite", "target", "run", "schema", "count-bool"])
def test_changed_evidence_cannot_reuse_a_matching_receipt(trusted, change):
    root, config, record, folder, _path, _policy = trusted
    path = folder / "execution-record.json"
    document = json.loads(path.read_bytes())
    artifact = {"input": "inputs.txt", "output": "result.txt", "patch": "fix.patch", "environment": "environment.json", "log": "run.log", "suite": "suite.txt"}.get(change)
    if artifact:
        (folder / artifact).write_bytes(b"changed\n")
    elif change in {"cost", "parameter"}:
        document["records"][-1]["payload"]["parameters"]["cost" if change == "cost" else "factor"] = "9"
    elif change == "target":
        document["receipts"][-1]["payload"]["target_record_id"] = document["receipts"][0]["payload"]["target_record_id"]
    elif change == "run":
        document["receipts"][-1]["payload"]["run_id"] = "different-run"
    elif change == "schema":
        document["schema_version"] = "execution-evidence/v999"
    else:
        document["receipts"][-1]["payload"]["counts"]["passed"] = True
    path.write_bytes(canonical(document))
    report = EXECUTION.inspect_execution(root, config, record)
    assert not report["valid"], report
    assert not EXECUTION.assess_verification(report, root, config, NOW)["eligible"]


@pytest.mark.parametrize("change", ["unsigned", "forged-label", "signature", "same-controller", "shared-secret", "key-revoked", "receipt-revoked", "policy-revision", "expiry", "workspace-trust", "public-trust"])
def test_evaluator_names_and_embedded_policy_cannot_authorize_a_pass(trusted, monkeypatch, change):
    root, config, record, folder, authority, policy = trusted
    path = folder / "execution-record.json"
    document = json.loads(path.read_bytes())
    envelope = document["receipts"][-1]
    now = NOW
    if change == "unsigned":
        del envelope["authentication"]
    elif change == "forged-label":
        envelope["authentication"]["principal"] = "owner"
    elif change == "signature":
        envelope["authentication"]["signature"] = "0" * 64
    elif change == "same-controller":
        policy["principals"]["evaluator"]["controller"] = "runner"
    elif change == "shared-secret":
        policy["principals"]["evaluator"]["keys"]["evaluator-key"] = KEYS["runner"]
    elif change == "key-revoked":
        policy["revoked_keys"] = ["evaluator-key"]
    elif change == "receipt-revoked":
        policy["revoked_envelopes"] = [identifier("evidence-authenticated-payload/v1", envelope["payload"])]
    elif change == "policy-revision":
        config["evidence_trust"]["policy_revision"] = "2"
    elif change == "expiry":
        now += timedelta(days=365)
    elif change == "workspace-trust":
        authority = root / "embedded-policy.json"
        monkeypatch.setenv("EVIDENCE_WIKI_AUTHORITY_FILE", str(authority.resolve()))
    authority.write_bytes(canonical(policy))
    os.chmod(authority, 0o644 if change == "public-trust" else 0o600)
    path.write_bytes(canonical(document))
    report = EXECUTION.inspect_execution(root, config, record)
    assert report["valid"], report
    assessment = EXECUTION.assess_verification(report, root, config, now)
    assert not assessment["eligible"], assessment


def test_resealed_generation_and_updated_target_do_not_repair_evaluator_authentication(trusted):
    root, config, record, folder, _authority, _policy = trusted
    path = folder / "execution-record.json"
    document = json.loads(path.read_bytes())
    payload = document["records"][-1]["payload"]
    payload["parameters"]["cost"] = "3"
    document["records"][-1] = authenticate(payload, "runner", "generator")
    target = identifier("evidence-execution-record/v1", payload)
    document["selected_record_id"] = target
    receipt = document["receipts"][-1]["payload"]
    receipt["target_record_id"] = target
    document["selected_receipt_id"] = identifier("evidence-verification-receipt/v1", receipt)
    path.write_bytes(canonical(document))
    report = EXECUTION.inspect_execution(root, config, record)
    assert report["valid"], report
    assessment = EXECUTION.assess_verification(report, root, config, NOW)
    assert not assessment["eligible"]
    assert assessment["receipts"][-1]["reason"] == "authentication_signature_mismatch"


@pytest.mark.parametrize("outcome", ["skipped", "inconclusive"])
def test_nonpassing_receipts_preserve_exact_outcome(trusted, outcome):
    root, config, record, folder, _authority, _policy = trusted
    path = folder / "execution-record.json"
    document = json.loads(path.read_bytes())
    receipt = document["receipts"][-1]["payload"]
    receipt["outcome"] = outcome
    receipt["assertions"][0]["outcome"] = outcome
    receipt["counts"] = {key: int(key == outcome) for key in EXECUTION.OUTCOMES}
    document["receipts"][-1] = authenticate(receipt, "evaluator", "evaluator")
    document["selected_receipt_id"] = identifier("evidence-verification-receipt/v1", receipt)
    path.write_bytes(canonical(document))
    report = EXECUTION.inspect_execution(root, config, record)
    assert report["valid"], report
    assessment = EXECUTION.assess_verification(report, root, config, NOW)
    assert assessment["receipts"][-1]["independent"]
    assert assessment["receipts"][-1]["outcome"] == outcome
    assert not assessment["eligible"]


@pytest.mark.parametrize("producer", ["normalize_sources.py", "external-normalizer"])
@pytest.mark.parametrize("change", ["original", "rendered"])
def test_native_and_external_consumers_recheck_the_original_closure(trusted, producer, change):
    root, config, record, folder, _authority, _policy = trusted
    _source, path = normalize(root, config, record)
    frontmatter, body, _error = LINT._normalized_contract.split_record(path.read_text(encoding="utf-8"))
    frontmatter["normalizer"]["name"] = producer
    if change == "original":
        (folder / "result.txt").write_bytes(b"100\n")
    else:
        frontmatter["execution_evidence"]["records"][0]["outcome"] = "passed"
    path.write_text("---\n" + yaml.safe_dump(frontmatter) + "---\n" + body, encoding="utf-8")
    verification, violations, reasons, verdict = consumer_results(root, config, record, path)
    assert verification["overall_result"] == "not_verified"
    assert violations > 0 and reasons
    assert verdict.verdict == "fail"


def test_unknown_required_execution_profile_cannot_fall_back_to_generic_html(trusted):
    root, config, record, _folder, _authority, _policy = trusted
    record.update(kind="html", raw_paths=["raw/page.html"], metadata={"execution_profile": "execution_evidence/v999"})
    assert NORMALIZE.normalization_method(root, record) == "execution"
    source = NORMALIZE.normalize_execution_record(root, config, record)
    assert source.extraction_method == "execution_stub"
    assert NORMALIZE.record_unusable_evidence_reasons(source.record)


def test_swapped_fifo_is_never_opened_in_blocking_mode(tmp_path, monkeypatch):
    if not hasattr(os, "mkfifo"):
        pytest.skip("This host has no filesystem FIFO object.")
    path = tmp_path / "artifact.txt"
    path.write_bytes(b"original")
    expected = REVISION.observation(path.stat())
    path.unlink()
    os.mkfifo(path)
    original_open = os.open

    def checked_open(path, flags, *args, **kwargs):
        if path == "artifact.txt":
            assert flags & os.O_NONBLOCK, "a swapped FIFO must never block capture"
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(REVISION.os, "open", checked_open)
    monkeypatch.setattr(REVISION.os, "supports_dir_fd", {*os.supports_dir_fd, checked_open})
    with pytest.raises(REVISION.ScriptRefusal) as caught:
        REVISION.read_observed_file(tmp_path, "artifact.txt", expected)
    assert caught.value.error_code == "EVIDENCE_REVISION_CHANGED"


def test_original_bytes_are_checked_even_if_artifact_manifest_is_rehashed(trusted):
    root, config, record, folder, _authority, _policy = trusted
    path = folder / "execution-record.json"
    document = json.loads(path.read_bytes())
    changed = b"unexpected environment\n"
    (folder / "environment.json").write_bytes(changed)
    for member in document["artifacts"]:
        if member["path"] == "environment.json":
            member.update(binding(changed))
    path.write_bytes(canonical(document))
    report = EXECUTION.inspect_execution(root, config, record)
    assert not report["valid"]
    assert report["reason"] == "execution_artifact_binding_mismatch"


def test_api_and_cli_validate_execution_without_writing_or_executing(trusted):
    from evidence_wiki import Workspace, cli, contract
    from evidence_wiki.errors import ConfigError, SourceError

    root, _config, _record, _folder, _authority, _policy = trusted
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with Workspace.open(root) as opened:
        report = opened.normalize.validate_execution(SOURCE_ID)
        assert report["valid"] and report["verification"]["eligible"], report
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            assert cli.main(["normalize", "execution", "--target", str(root), "--source-id", SOURCE_ID]) == 0
        cli_report = json.loads(output.getvalue())
        api_verification = report.pop("verification")
        cli_verification = cli_report.pop("verification")
        api_verification.pop("evaluated_at")
        cli_verification.pop("evaluated_at")
        assert api_verification == cli_verification
        assert report == cli_report
        with pytest.raises(SourceError):
            opened.normalize.validate_execution("absent")
        assert contract()["artifact_schemas"]["execution_evidence"] == "execution-evidence/v1"
    with pytest.raises(ConfigError):
        opened.normalize.validate_execution(SOURCE_ID)
    assert before == {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_absent_receipt_is_preserved_as_absent(trusted):
    root, config, record, folder, _authority, _policy = trusted
    document = json.loads((folder / "execution-record.json").read_bytes())
    document.update(receipts=[], selected_receipt_id=None)
    document["artifacts"] = [item for item in document["artifacts"] if item["path"] != "evaluation.log"]
    (folder / "evaluation.log").unlink()
    (folder / "execution-record.json").write_bytes(canonical(document))
    report = EXECUTION.inspect_execution(root, config, record)
    assert report["valid"], report
    assessment = EXECUTION.assess_verification(report, root, config, NOW)
    assert assessment["receipts"] == [] and not assessment["eligible"]
    assert assessment["reason"] == "verification_receipt_missing"


@pytest.mark.parametrize("change", ["size", "count", "depth", "case", "parent"])
def test_caller_supplied_closures_are_bounded_before_parsing(change):
    files = {"execution-record.json": b"not parsed"}
    if change == "size":
        files["execution-record.json"] = b"x" * (16 * 1024 * 1024 + 1)
    elif change == "count":
        files.update({f"extra-{index}.txt": b"" for index in range(256)})
    elif change == "depth":
        files["directory/" * 33 + "data"] = b""
    elif change == "case":
        files.update({"Input/a": b"", "input/b": b""})
    else:
        files.update({"input": b"", "input/child": b""})
    with patch.object(EXECUTION, "json_document", side_effect=AssertionError("bounds must precede parsing")):
        with pytest.raises(EXECUTION.EvidenceInvalid, match="artifact_(bound_exceeded|path_collision)"):
            EXECUTION.validate_closure(SOURCE_ID, files)


def test_another_records_pass_cannot_override_the_selected_failed_observation(trusted):
    root, config, record, folder, _authority, _policy = trusted
    document = json.loads((folder / "execution-record.json").read_bytes())
    receipt = document["receipts"][0]["payload"]
    document["selected_record_id"] = receipt["target_record_id"]
    document["selected_receipt_id"] = identifier("evidence-verification-receipt/v1", receipt)
    (folder / "execution-record.json").write_bytes(canonical(document))
    report = EXECUTION.inspect_execution(root, config, record)
    assert report["valid"], report
    assessment = EXECUTION.assess_verification(report, root, config, NOW)
    assert assessment["receipts"][-1]["positive_eligible"]
    assert not assessment["eligible"]


@pytest.mark.parametrize("change", ["incomplete-scope", "missing-predecessor", "time-travel", "duplicate-record"])
def test_incomplete_checks_and_invalid_history_refuse(trusted, change):
    root, config, record, folder, _authority, _policy = trusted
    document = json.loads((folder / "execution-record.json").read_bytes())
    if change == "incomplete-scope":
        receipt = document["receipts"][-1]["payload"]
        receipt.update(assertions=[], counts=dict.fromkeys(EXECUTION.OUTCOMES, 0))
    elif change == "missing-predecessor":
        document["records"][-1]["payload"]["predecessor"] = "sha256:" + "0" * 64
    elif change == "time-travel":
        document["records"][-1]["payload"]["started_at"] = "2026-09-09T23:50:00Z"
    else:
        document["records"].append(copy.deepcopy(document["records"][-1]))
    (folder / "execution-record.json").write_bytes(canonical(document))
    assert not EXECUTION.inspect_execution(root, config, record)["valid"]


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "extra-file"])
def test_unsafe_or_undeclared_originals_refuse_before_authentication(trusted, kind):
    root, config, record, folder, _authority, _policy = trusted
    path = folder / "unexpected.txt"
    if kind == "symlink":
        path.symlink_to(folder / "inputs.txt")
    elif kind == "hardlink":
        os.link(folder / "inputs.txt", path)
    elif kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("This platform has no filesystem FIFO objects")
        os.mkfifo(path)
    else:
        path.write_bytes(b"not in the original declaration")
    report = EXECUTION.inspect_execution(root, config, record)
    assert not report["valid"], report


@pytest.mark.parametrize("packet_name", ["native-v2-current.json", "native-v2-stale.json", "native-v2-bounded.json", None])
def test_context_qualification_preserves_original_bytes_without_converting_it_to_authority(trusted, packet_name):
    root, config, record, folder, _authority, _policy = trusted
    document = json.loads((folder / "execution-record.json").read_bytes())
    path = "context.json" if packet_name else "context.txt"
    data = (Path(__file__).parent / "fixtures/codebase-intake/native-packets" / packet_name).read_bytes() if packet_name else b"A plain text prompt.\n"
    (folder / path).write_bytes(data)
    document["artifacts"].append({"path": path, **binding(data), "role": "context-packet" if packet_name else "context"})
    payload = document["records"][-1]["payload"]
    payload["model"]["context"] = {"path": path, "content_hash": binding(data)["content_hash"]}
    document["records"][-1] = authenticate(payload, "runner", "generator")
    target = identifier("evidence-execution-record/v1", payload)
    document["selected_record_id"] = target
    receipt = document["receipts"][-1]["payload"]
    receipt["target_record_id"] = target
    document["receipts"][-1] = authenticate(receipt, "evaluator", "evaluator")
    document["selected_receipt_id"] = identifier("evidence-verification-receipt/v1", receipt)
    (folder / "execution-record.json").write_bytes(canonical(document))
    report = EXECUTION.inspect_execution(root, config, record)
    assert report["valid"], report
    qualification = report["records"][-1]["qualified_context"]
    if packet_name:
        assert qualification[path]["original"] == binding(data)
        assert qualification[path]["authority"] == "not_established"
        assert "content" not in qualification[path]["qualifications"]["response"]
    else:
        assert qualification == {}
