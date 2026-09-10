"""Exercise original-byte intake, independent consumer gates, and declared limits."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from tests._script_loader import load_isolated_module

SCRIPTS = Path(__file__).resolve().parents[1] / "workspace-template/scripts"
INTAKE = load_isolated_module("packet_intake", SCRIPTS / "_qualified_packet.py")
NORMALIZE = load_isolated_module("packet_normalize", SCRIPTS / "normalize_sources.py")
VERIFY = load_isolated_module("packet_verify", SCRIPTS / "normalize_verify.py")
LINT = load_isolated_module("packet_lint", SCRIPTS / "lint.py")
POLICY = load_isolated_module("packet_policy", SCRIPTS / "_evidence_policies.py")
pytestmark = pytest.mark.skipif(os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"), reason="requires no-follow capture")
FIXTURES = Path(__file__).parent / "fixtures/codebase-intake/native-packets"
SOURCE_ID = "codebase:sample"


def delivery(root: Path, packet: bytes, *, required: bool = False):
    config = {
        "project": {"name": "Packet intake"},
        "sources": {"manifest_path": "sources/manifest.jsonl", "normalized_dir": "sources/normalized"},
        "integrations": {"codebase_analysis": {"intake_profile": INTAKE.PROFILE,
                                               "require_live_reconciliation": required}},
    }
    record = {"id": SOURCE_ID, "kind": "codebase_architecture", "raw_paths": [],
              "raw_fingerprint": "sha256:synthetic", "metadata": {}}
    folder = root / INTAKE.artifact_relative(config, record)
    folder.mkdir(parents=True, exist_ok=True)
    (root / "sources/normalized").mkdir(exist_ok=True)
    (root / "research.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (root / "sources/manifest.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    deposit(folder, packet)
    return config, record, folder


def deposit(folder: Path, packet: bytes):
    manifest = {
        "schema_version": "1", "artifact_kind": "codebase_evidence", "source_id": SOURCE_ID,
        "intake_profile": INTAKE.PROFILE, "packet_path": "packet.json",
        "generated_at": "2026-09-10T00:00:00Z", "producer": {"name": "agent-wiki-cli", "version": "1.8.0"},
        "invocation": {"executed_by": "external_worker", "argv": ["llm-wiki", "context"],
                       "plugins_enabled": False, "hooks_enabled": False, "network_access": False},
        "files": [{"path": "packet.json", "size_bytes": len(packet), "sha256": hashlib.sha256(packet).hexdigest()}],
    }
    (folder / "packet.json").write_bytes(packet)
    (folder / INTAKE.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")


def normalize(root: Path, config: dict, record: dict):
    source = NORMALIZE.normalize_codebase_record(root, config, copy.deepcopy(record))
    path, _result = NORMALIZE.write_normalized_source(
        source, root / "sources/normalized", "sources/manifest.jsonl", "2026-09-10",
        project_root=root, force=True, normalized_at="2026-09-10T00:00:00Z",
    )
    return path


def consumer_results(root, config, record, path):
    frontmatter, _body = LINT.load_frontmatter(path)
    verification = VERIFY.build_report(root)
    findings = {"issues": []}
    _foreign, violations, _thin = LINT.check_normalized_record_contract(
        root, path.parent, {SOURCE_ID: record}, findings, config=config,
    )
    inputs = POLICY.PolicyInputs(root, config, {SOURCE_ID: record}, {SOURCE_ID: frontmatter}, {}, [], {}, {}, {})
    reasons = POLICY.source_unusable_evidence_reasons(inputs, SOURCE_ID)
    return verification, violations, reasons


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("*.json")), ids=lambda path: path.stem)
def test_native_packets_preserve_every_qualification_without_promoting_trust(tmp_path, fixture):
    original = fixture.read_bytes()
    config, record, folder = delivery(tmp_path, original)
    with patch("socket.create_connection", side_effect=AssertionError("offline")), patch("subprocess.run", side_effect=AssertionError("inert")):
        report = INTAKE.inspect_packet(tmp_path, config, record)
    assert report["valid"], report
    assert report["policy_satisfied"]
    assert report["worker_authentication"] == "not_established"
    assert report["host_reconciliation"]["state"] == "unevaluated"
    expected = json.loads(original)
    omitted = []
    if "content" in expected["response"]:
        del expected["response"]["content"]
        omitted.append("/response/content")
    assert report["qualifications"] == expected
    assert report["omitted_fields"] == omitted
    assert (folder / "packet.json").read_bytes() == original
    assert report["original"]["sha256"] == hashlib.sha256(original).hexdigest()
    path = normalize(tmp_path, config, record)
    verification, violations, reasons = consumer_results(tmp_path, config, record, path)
    assert verification["overall_result"] == "verified", verification
    assert violations == 0
    assert reasons == []


@pytest.mark.parametrize("mutation", ["whitespace", "bom", "duplicate", "version", "identity", "missing", "nested", "path", "depth"])
def test_recomputed_delivery_checksums_cannot_repair_an_invalid_native_packet(tmp_path, mutation):
    original = (FIXTURES / "native-v2-current.json").read_bytes()
    payload = json.loads(original)
    if mutation == "whitespace":
        packet = json.dumps(payload, indent=2).encode()
    elif mutation == "bom":
        packet = b"\xef\xbb\xbf" + original
    elif mutation == "duplicate":
        packet = b'{"packet_id":"duplicate",' + original[1:]
    else:
        if mutation == "version":
            payload["schema_version"] = "llm-wiki-qualified-context-packet/v999"
        elif mutation == "identity":
            payload["packet_id"] = "sha256:" + "0" * 64
        elif mutation == "missing":
            del payload["response"]["knowledge"]
        elif mutation == "nested":
            payload["response"]["knowledge"]["availability"] = "invented"
        elif mutation == "path":
            first = next(iter(payload["response"]["files"]))
            payload["response"]["files"]["../../private.json"] = payload["response"]["files"].pop(first)
        elif mutation == "depth":
            payload["assurance"] = {"nested": []}
            value = payload["assurance"]["nested"]
            for _ in range(100):
                child = []
                value.append(child)
                value = child
        packet = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    config, record, _folder = delivery(tmp_path, packet)
    report = INTAKE.inspect_packet(tmp_path, config, record)
    assert report["delivery_integrity"] == "valid"
    assert report["valid"] is False
    assert report["reason"] in {"malformed-context-packet", "context-packet-path-policy-rejected"}
    source = NORMALIZE.normalize_codebase_record(tmp_path, config, record)
    assert source.extraction_method == "codebase_stub"
    assert not source.record["metadata"]["qualified_context"]["valid"]


@pytest.mark.parametrize("mutation", ["missing", "wrong_size", "duplicate", "unsafe", "undeclared", "script", "symlink", "hardlink", "oversized", "many_entries", "directory_link"])
def test_delivery_refuses_unsafe_or_unbound_members(tmp_path, mutation):
    config, record, folder = delivery(tmp_path, (FIXTURES / "native-v1.json").read_bytes())
    manifest = json.loads((folder / INTAKE.MANIFEST).read_bytes())
    if mutation == "missing":
        (folder / "packet.json").unlink()
    elif mutation == "wrong_size":
        manifest["files"][0]["size_bytes"] += 1
    elif mutation == "duplicate":
        manifest["files"].append(manifest["files"][0])
    elif mutation == "unsafe":
        manifest["files"][0]["path"] = "../packet.json"
    elif mutation in {"undeclared", "script"}:
        (folder / ("extra.md" if mutation == "undeclared" else "hook.py")).write_text("inert", encoding="utf-8")
    elif mutation in {"symlink", "hardlink"}:
        outside = tmp_path / "outside.json"
        outside.write_text("untrusted", encoding="utf-8")
        (folder / "packet.json").unlink()
        if mutation == "symlink":
            (folder / "packet.json").symlink_to(outside)
        else:
            os.link(outside, folder / "packet.json")
    elif mutation == "oversized":
        with (folder / "packet.json").open("wb") as stream:
            stream.truncate(INTAKE.MAX_BYTES + 1)
    elif mutation == "many_entries":
        for index in range(INTAKE.MAX_ENTRIES + 1):
            (folder / f"entry{index}.json").touch()
    elif mutation == "directory_link":
        (folder / "alias").symlink_to(tmp_path, target_is_directory=True)
    (folder / INTAKE.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    report = INTAKE.inspect_packet(tmp_path, config, record)
    assert report["valid"] is False
    assert report["delivery_integrity"] != "valid"


def test_concurrent_member_replacement_retries_a_whole_generation(tmp_path):
    config, record, folder = delivery(tmp_path, (FIXTURES / "native-v1.json").read_bytes())
    read = INTAKE.read_observed_file
    replacement = (FIXTURES / "native-v2-stale.json").read_bytes()
    changed = False

    def racing_read(root, relative, identity):
        nonlocal changed
        if not changed:
            changed = True
            previous = (folder / "packet.json").stat()
            deposit(folder, replacement)
            os.utime(folder / "packet.json", ns=(previous.st_atime_ns, previous.st_mtime_ns))
        return read(root, relative, identity)

    with patch.object(INTAKE, "read_observed_file", side_effect=racing_read):
        report = INTAKE.inspect_packet(tmp_path, config, record)
    assert changed
    assert report["valid"], report
    assert report["packet_id"] == json.loads(replacement)["packet_id"]


@pytest.mark.parametrize("foreign", [False, True])
@pytest.mark.parametrize("mutation", ["strip", "forge", "replace", "require_live", "unknown_profile"])
def test_all_consumers_recheck_native_and_external_normalized_records(tmp_path, foreign, mutation):
    config, record, folder = delivery(tmp_path, (FIXTURES / "native-v2-current.json").read_bytes())
    path = normalize(tmp_path, config, record)
    frontmatter, _error = LINT.load_frontmatter(path)
    body = path.read_text(encoding="utf-8").split("\n---\n", 1)[1]
    if foreign:
        frontmatter["normalizer"] = {"name": "external-tool", "version": "1"}
    if mutation == "strip":
        del frontmatter["qualified_context"]
    elif mutation == "forge":
        frontmatter["qualified_context"]["host_reconciliation"]["state"] = "current"
    elif mutation == "replace":
        deposit(folder, (FIXTURES / "native-v2-absent.json").read_bytes())
    elif mutation == "require_live":
        config["integrations"]["codebase_analysis"]["require_live_reconciliation"] = True
    else:
        config["integrations"]["codebase_analysis"]["intake_profile"] = "unknown/v1"
    path.write_text("---\n" + yaml.safe_dump(frontmatter) + "---\n" + body, encoding="utf-8")
    (tmp_path / "research.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
    verification, violations, reasons = consumer_results(tmp_path, config, record, path)
    assert verification["overall_result"] != "verified"
    assert violations > 0
    assert reasons


def test_required_live_reconciliation_is_a_policy_refusal_even_for_valid_original_bytes(tmp_path):
    config, record, _folder = delivery(tmp_path, (FIXTURES / "native-v2-current.json").read_bytes(), required=True)
    report = INTAKE.inspect_packet(tmp_path, config, record)
    assert report["valid"] and not report["policy_satisfied"]
    assert report["policy_reason"] == "live_reconciliation_required"
    path = normalize(tmp_path, config, record)
    verification, violations, reasons = consumer_results(tmp_path, config, record, path)
    assert verification["overall_result"] != "verified"
    assert violations and "live_reconciliation_required" in reasons


def test_adapter_verification_receives_original_delivery_and_configured_policy(tmp_path):
    config, record, _folder = delivery(tmp_path, (FIXTURES / "native-v2-current.json").read_bytes())
    path = normalize(tmp_path, config, record)
    NORMALIZE.verify_adapter_output(tmp_path, path, [record], path.parent, config=config)
    config["integrations"]["codebase_analysis"]["require_live_reconciliation"] = True
    with pytest.raises(NORMALIZE.AdapterError, match="NORMALIZED_CONTRACT_QUALIFIED_PACKET_INVALID"):
        NORMALIZE.verify_adapter_output(tmp_path, path, [record], path.parent, config=config)
    assert not path.exists()


def test_api_cli_are_read_only_and_share_the_same_report(tmp_path):
    from evidence_wiki import Workspace, cli, contract
    from evidence_wiki.errors import ConfigError, SourceError

    config, record, _folder = delivery(tmp_path, (FIXTURES / "native-v2-current.json").read_bytes())
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with Workspace.open(tmp_path) as workspace:
        expected = workspace.normalize.validate_packet(SOURCE_ID)
        assert expected == INTAKE.inspect_packet(tmp_path, config, record)
        assert workspace.normalize.profiles() == INTAKE.profiles()
        for command, report in [("profiles", INTAKE.profiles()), ("packet", expected)]:
            args = ["normalize", command, "--target", str(tmp_path)]
            if command == "packet":
                args += ["--source-id", SOURCE_ID]
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                assert cli.main(args) == 0
            assert json.loads(output.getvalue()) == report
        with pytest.raises(SourceError):
            workspace.normalize.validate_packet("absent")
    with pytest.raises(ConfigError):
        workspace.normalize.validate_packet(SOURCE_ID)
    assert before == {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert contract()["intake_profiles"] == INTAKE.profiles()
    assert "NORMALIZED_CONTRACT_QUALIFIED_PACKET_INVALID" in contract()["normalized_source_format"]["violation_codes"]
