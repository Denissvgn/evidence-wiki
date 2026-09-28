"""Native HTML classification currency, format compatibility, and scoped refresh."""

import contextlib
import copy
import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from evidence_wiki.cli import main
from evidence_wiki.errors import SourceError, error_from_envelope
from evidence_wiki.onboarding_contract import _matches
from evidence_wiki.pack_discovery import owner
from evidence_wiki.source_contracts import schema_document
from evidence_wiki.source_inspection import inspect
from tests.test_html_usability import html_page, normalize_page
from tests.test_source_capabilities import route_request

PROFILE = owner("_html_usability_profile")
NORMALIZE = owner("normalize_sources")
CONTRACT = owner("_normalized_contract")
REVISION_INPUTS = json.loads(
    (Path(__file__).parent / "fixtures/html-usability/revision-inputs.json").read_text()
)["cases"]


@pytest.mark.parametrize("case", REVISION_INPUTS, ids=lambda case: case["name"])
def test_classification_revision_input_contract(case):
    metadata = copy.deepcopy(case["frontmatter"])
    before = copy.deepcopy(metadata)
    result = PROFILE.evaluate_html_usability(metadata, current_revision=case["current_revision"])
    assert result.state == case["expected_state"]
    assert result.requires_recheck is (result.state not in {"current", "not_applicable"})
    assert result.applicable is (result.state != "not_applicable")
    assert (result.reason is None) is (result.state in {"current", "not_applicable"})
    assert metadata == before


@pytest.mark.parametrize("change", [
    {"source_kind": "pdf"}, {"extraction_method": "pdf_text"}, {"extraction_method": []},
    {"extraction_method": {}}, {"normalizer": None}, {"normalizer": {"name": ""}},
    {"normalizer": {"name": 1}}, {"extraction_method": None},
])
def test_native_html_coordinate_contradictions_are_not_exemptions(change):
    metadata = {"source_kind": "html", "extraction_method": "html_text",
                "normalizer": {"name": "normalize_sources.py", "version": 3}, "html_usability_version": 1}
    metadata.update(change)
    result = PROFILE.evaluate_html_usability(metadata, source_kind="html", effective_method="html")
    assert result.state == "invalid" and result.reason == "html_usability_profile_invalid"


def test_manifest_and_effective_method_qualify_native_claims():
    metadata = REVISION_INPUTS[1]["frontmatter"]
    assert PROFILE.evaluate_html_usability(metadata, source_kind="pdf").state == "invalid"
    assert PROFILE.evaluate_html_usability(metadata, effective_method="pdf").state == "invalid"


@pytest.mark.parametrize("change", [{"extraction_method": "future_html"}, {"normalizer": {}}, {"source_kind": None}])
def test_future_html_claims_are_not_repairable_by_damaging_another_coordinate(change):
    metadata = copy.deepcopy(REVISION_INPUTS[2]["frontmatter"])
    metadata.update(change)
    assert PROFILE.evaluate_html_usability(metadata, source_kind="html", effective_method="html").state == "unsupported"


@pytest.mark.parametrize("kind,method", [
    ("pdf", "pdf_text"), ("table", "table_text"), ("docx", "docx_text_tables"),
    ("host_capture", "host_text"), ("web_link", "web_stub"), ("structured_data", "adapter"),
])
def test_other_methods_do_not_acquire_a_native_html_revision_requirement(kind, method):
    metadata = {"source_kind": kind, "extraction_method": method,
                "normalizer": {"name": "normalize_sources.py", "version": 3}, "html_usability_version": False}
    assert PROFILE.evaluate_html_usability(metadata).state == "not_applicable"
    assert not any(v.field == "html_usability_version" for v in CONTRACT.check_frontmatter(metadata))


@pytest.mark.parametrize("value", [None, False, "1", -1, 2])
def test_declared_foreign_html_keeps_its_own_classification_contract(value):
    metadata = {"source_kind": "html", "extraction_method": "html_text",
                "normalizer": {"name": "external-html-tool", "version": "1.0"}, "html_usability_version": value}
    assert PROFILE.evaluate_html_usability(metadata, source_kind="html", effective_method="html").state == "not_applicable"
    assert not any(v.field == "html_usability_version" for v in CONTRACT.check_frontmatter(metadata))


@pytest.mark.parametrize("html", ['<p>Measured value: 42.</p>', '<p>502 Bad Gateway.</p>', '', '<p>Unclosed'])
def test_native_writer_stamps_observed_classification_without_claiming_usable_content(tmp_path, html):
    _, metadata = normalize_page(tmp_path, html_page(html))
    assert metadata["html_usability_version"] == 1
    assert PROFILE.evaluate_html_usability(metadata).state == "current"


def test_missing_or_unreadable_original_is_not_stamped_current(tmp_path, monkeypatch):
    record = {"id": "web:missing", "kind": "html", "raw_paths": ["raw/web/missing.html"]}
    missing = NORMALIZE.normalize_html_record(tmp_path, record)
    metadata = NORMALIZE.frontmatter_for(missing, "sources/manifest.jsonl", tmp_path / "out.md", "2026-09-28")
    assert "html_usability_version" not in metadata
    monkeypatch.setattr(NORMALIZE, "read_html_text", lambda *args: ("", ["cannot read HTML file"]))
    _, metadata = normalize_page(tmp_path, html_page('<p>Measured value: 42.</p>'))
    assert "html_usability_version" not in metadata


def test_frontmatter_cannot_stamp_a_manual_construct_or_foreign_adapter_as_native_html(tmp_path):
    source, _ = normalize_page(tmp_path, html_page('<p>Measured value: 42.</p>'))
    for other in (
        replace(source, html_usability_version=None),
        replace(source, adapter_name="external-html-tool", adapter_version="1"),
        replace(source, extraction_method="pdf_text"),
    ):
        metadata = NORMALIZE.frontmatter_for(other, "sources/manifest.jsonl", tmp_path / "out.md", "2026-09-28")
        assert "html_usability_version" not in metadata


@pytest.mark.parametrize("version", [True, "1", 0, 2])
def test_native_writer_cannot_stamp_an_invalid_or_future_observation(tmp_path, version):
    source, _ = normalize_page(tmp_path, html_page('<p>Measured value: 42.</p>'))
    with pytest.raises(ValueError, match="classification revision"):
        NORMALIZE.frontmatter_for(replace(source, html_usability_version=version),
                                 "sources/manifest.jsonl", tmp_path / "out.md", "2026-09-28")


def test_bounded_abstention_is_a_current_policy_observation_not_acceptance(tmp_path, monkeypatch):
    monkeypatch.setattr(NORMALIZE, "HTML_MAX_BYTES", 12)
    _, metadata = normalize_page(tmp_path, html_page('<p>502 Bad Gateway.</p>'))
    assert metadata["html_usability_version"] == 1
    assert any("truncated" in warning for warning in metadata["parse_warnings"])


@pytest.mark.parametrize("version,valid", [(1, True), (2, True), (None, False), (True, False), ("1", False),
                                         (0, False), (-1, False), ({}, False), ([], False)])
def test_present_native_revision_is_typed_but_not_limited_to_the_current_value(tmp_path, version, valid):
    _, metadata = normalize_page(tmp_path, html_page('<p>Measured value: 42.</p>'))
    metadata["html_usability_version"] = version
    violations = [v for v in CONTRACT.check_frontmatter(metadata) if v.field == "html_usability_version"]
    assert bool(violations) is (not valid)
    if violations:
        assert violations[0].code == CONTRACT.FRONTMATTER_INVALID
    del metadata["html_usability_version"]
    assert not CONTRACT.check_frontmatter(metadata)


@pytest.mark.parametrize("value", [True, 0, -1, "1"])
def test_invalid_current_revision_is_a_programmer_error(value):
    with pytest.raises(ValueError, match="positive integer"):
        PROFILE.evaluate_html_usability({}, current_revision=value)


def normalize_command(root, *arguments):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = NORMALIZE.main(["--project-root", str(root), *arguments, "--format", "json"])
    return code, json.loads(stdout.getvalue() or stderr.getvalue())


def workspace_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def html_workspace(tmp_path):
    root = tmp_path / "workspace"
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(["init", "--target", str(root), "--project-name", "html-revisions",
                     "--project-description", "Observe retained HTML classifications."]) == 0
    for relative, text in {
        "raw/web/gateway.html": "<p>502 Bad Gateway. Please try again later.</p>",
        "raw/web/study.html": "<title>Measurements</title><p>Measured reflectance: 0.74.</p>",
        "raw/data/measurements.csv": "sample,value\nroof,0.74\n",
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.with_name(path.name + ".provenance.yml").write_text(yaml.safe_dump({
            "origin_url": "https://example.org/study", "retrieved_at": "2026-09-28T10:00:00Z",
            "retrieved_by": "fixture", "source_type": "official_web", "license": "CC0-1.0",
            "checksum": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        }), encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert owner("source_inventory").main(["--project-root", str(root)]) == 0
    records = {Path(r["raw_paths"][0]).stem: r for r in NORMALIZE.load_manifest(root / "sources/manifest.jsonl")}
    code, _ = normalize_command(root, "--all")
    assert code == 0
    paths = {key: root / "sources/normalized" / (NORMALIZE.safe_source_id(r["id"]) + ".md")
             for key, r in records.items()}
    return SimpleNamespace(root=root, records=records, paths=paths)


def edit_metadata(path, *, changes=None, remove=()):
    metadata, body, error = CONTRACT.split_record(path.read_text())
    assert error is None
    for key in remove:
        metadata.pop(key, None)
    metadata.update(copy.deepcopy(changes or {}))
    path.write_text("---\n" + NORMALIZE.render_yaml(metadata) + "\n---\n" + body, encoding="utf-8")
    return metadata


def source_command(root, operation, *arguments):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(["agent", operation, "--target", str(root), *arguments])
    assert code == 0, stderr.getvalue() or stdout.getvalue()
    result = json.loads(stdout.getvalue())
    _matches(result, schema_document(result["schema_version"]))
    return result


@pytest.mark.parametrize("state", ["current", "legacy", "future", "invalid", "shell"])
@pytest.mark.parametrize("status", ["content_extracted", "partial"])
@pytest.mark.parametrize("needs_complete", [False, True])
def test_inspection_and_routes_preserve_classification_blockers_without_execution(
    html_workspace, monkeypatch, state, status, needs_complete,
):
    workspace = html_workspace
    key = "gateway" if state == "shell" else "study"
    changes = {"status": status}
    if state in {"future", "invalid"}:
        changes["html_usability_version"] = 2 if state == "future" else True
    edit_metadata(workspace.paths[key], changes=changes,
                  remove=("html_usability_version",) if state == "legacy" else ())
    source_id = workspace.records[key]["id"]
    request = route_request(source_ids=[source_id], output_format="html", needs_complete=needs_complete)
    request["preferred_tools"] = []
    route_path = workspace.root.parent / "routes.json"
    route_path.write_text(json.dumps(request))

    def forbidden(*args, **kwargs):
        pytest.fail("Read-only inspection attempted HTML parsing or normalization")

    for name in ("HTMLContentExtractor", "normalize_html_record", "run_normalization", "html_unusable_evidence_reasons"):
        monkeypatch.setattr(NORMALIZE, name, forbidden)
    before = workspace_bytes(workspace.root)
    result = source_command(workspace.root, "source-status", "--source-id", source_id)
    row = result["sources"][0]
    assert row["semantic_adequacy"] == "not_evaluated" and not row["evidence_accepted"]
    if state != "invalid":
        assert row["extraction"] == status
        assert row["complete"] is (status == "content_extracted")
        assert row["retrieval"] == "lexically_indexable"
        assert row["evidence_usable"] is (state != "shell")
    else:
        assert row["extraction"] == "invalid" and "normalized_contract_failed" in row["reasons"]
    reasons = {"legacy": "html_usability_recheck_required", "future": "html_usability_profile_unsupported",
               "invalid": "html_usability_profile_invalid", "shell": "html_error_page:official_error_page"}
    if state != "current":
        assert reasons[state] in row["reasons"]
        assert row["usability"] == "not_ready"
    else:
        assert row["usability"] == ("usable" if status == "content_extracted" else "partial")
    routed = source_command(workspace.root, "routes", "--from-file", str(route_path))
    local = next(route for route in routed["routes"] if route["kind"] == "local_source")
    eligible = state == "current" and (status == "content_extracted" or not needs_complete)
    assert local["state"] == ("usable_for_caller_review" if eligible else "blocked")
    assert not local["evidence_accepted"]
    if state in {"legacy", "future", "invalid"}:
        remediation = next(item for item in routed["remediation"] if item["reason"] == reasons[state])
        assert "HTML" in remediation["action"] and not remediation["executed"]
    assert workspace_bytes(workspace.root) == before


@pytest.mark.parametrize("original", ["unchanged", "changed", "missing"])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("qualification", ["ocr", "rendering"])
def test_partial_readiness_cannot_hide_currency_or_original_blockers(html_workspace, original, legacy, qualification):
    workspace = html_workspace
    changes = {"status": "partial"}
    if qualification == "ocr":
        changes["needs_ocr"] = True
    else:
        changes["rendered_coverage"] = {"total_values": 2, "rendered_values": 1, "ratio": 0.5}
    edit_metadata(workspace.paths["study"], changes=changes,
                  remove=("html_usability_version",) if legacy else ())
    record = workspace.records["study"]
    path = workspace.root / record["raw_paths"][0]
    if original == "changed":
        path.write_text(path.read_text() + "<p>Additional observation.</p>")
    elif original == "missing":
        path.unlink()
    before = workspace_bytes(workspace.root)
    row = inspect(target=workspace.root, source_ids=[record["id"]])["sources"][0]
    reason = "ocr_required" if qualification == "ocr" else "partial_rendered_table_or_values"
    assert reason in row["reasons"] and not row["complete"]
    assert row["usability"] == ("partial" if original == "unchanged" and not legacy else "not_ready")
    if original != "unchanged":
        assert ("raw_source_missing" if original == "missing" else "raw_revision_changed_since_inventory") in row["reasons"]
    if legacy:
        assert "html_usability_recheck_required" in row["reasons"]
    assert workspace_bytes(workspace.root) == before


@pytest.mark.parametrize("kind", [None, "pdf"])
def test_inspection_checks_actual_manifest_kind_before_admitting_native_html(html_workspace, kind):
    workspace = html_workspace
    records = list(workspace.records.values())
    record = workspace.records["study"]
    record["kind"] = kind
    (workspace.root / "sources/manifest.jsonl").write_text("".join(json.dumps(row) + "\n" for row in records))
    row = inspect(target=workspace.root, source_ids=[record["id"]])["sources"][0]
    assert row["usability"] == "not_ready"
    assert "html_usability_profile_invalid" in row["reasons"]


@pytest.mark.parametrize("selector", ["pending", "all", "single", "multiple", "force"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_legacy_html_refreshes_once_under_every_selector(html_workspace, selector, dry_run):
    workspace = html_workspace
    target = workspace.paths["study"]
    old = edit_metadata(target, remove=("html_usability_version",))
    options = {
        "pending": [], "all": ["--all"], "single": ["--source-id", workspace.records["study"]["id"]],
        "multiple": ["--source-id", workspace.records["study"]["id"], "--source-id", workspace.records["gateway"]["id"]],
        "force": ["--source-id", workspace.records["study"]["id"], "--force"],
    }[selector]
    before = workspace_bytes(workspace.root)
    code, report = normalize_command(workspace.root, *options, *(["--dry-run"] if dry_run else []))
    assert code == 0
    assert report["summary"]["stale"] == 1
    after = workspace_bytes(workspace.root)
    if dry_run:
        assert before == after and report["summary"]["would_update"] == 1
    else:
        metadata = NORMALIZE.read_output_frontmatter(target)
        assert metadata["html_usability_version"] == 1
        assert metadata["created"] == old["created"]
        assert metadata["raw_fingerprint"] == old["raw_fingerprint"]
        assert metadata["content_hash"] == old["content_hash"]
        assert {name for name in before if before[name] != after[name]} == {target.relative_to(workspace.root).as_posix()}
        replay_before = workspace_bytes(workspace.root)
        code, replay = normalize_command(workspace.root, "--source-id", workspace.records["study"]["id"])
        assert code == 0 and replay["summary"]["skipped_existing"] == 1
        assert replay_before == workspace_bytes(workspace.root)


@pytest.mark.parametrize("version", [None, True, "1", 0, -1, {}, []])
def test_malformed_html_revision_repairs_from_originals(html_workspace, version):
    workspace = html_workspace
    edit_metadata(workspace.paths["study"], changes={"html_usability_version": version})
    code, report = normalize_command(workspace.root, "--source-id", workspace.records["study"]["id"])
    assert code == 0 and report["summary"]["updated"] == 1
    assert NORMALIZE.read_output_frontmatter(workspace.paths["study"])["html_usability_version"] == 1


@pytest.mark.parametrize("selector", ["pending", "all", "all-force", "force", "selected"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_future_revision_refuses_the_batch_before_any_selected_write(html_workspace, selector, dry_run):
    workspace = html_workspace
    edit_metadata(workspace.paths["gateway"], remove=("html_usability_version",))
    edit_metadata(workspace.paths["study"], changes={"html_usability_version": 2})
    selected = ["--source-id", workspace.records["gateway"]["id"], "--source-id", workspace.records["study"]["id"]]
    options = {"pending": [], "all": ["--all"], "all-force": ["--all", "--force"],
               "force": [*selected, "--force"], "selected": selected}[selector]
    before = workspace_bytes(workspace.root)
    code, report = normalize_command(workspace.root, *options, *(["--dry-run"] if dry_run else []))
    assert code == 2 and report["error_code"] == "NORMALIZATION_PROFILE_UNSUPPORTED"
    assert report["details"]["stored_version"] == 2 and report["details"]["supported_version"] == 1
    assert report["details"]["source_id"] == workspace.records["study"]["id"]
    assert isinstance(error_from_envelope(report), SourceError)
    assert before == workspace_bytes(workspace.root)


def test_future_revision_guard_precedes_other_staleness_triggers(html_workspace):
    workspace = html_workspace
    edit_metadata(workspace.paths["study"], changes={"html_usability_version": 2, "raw_fingerprint": "sha256:changed",
                                                   "normalizer": {"name": "normalize_sources.py", "version": 1},
                                                   "extraction_method": "future_html"})
    before = workspace_bytes(workspace.root)
    code, report = normalize_command(workspace.root, "--source-id", workspace.records["study"]["id"], "--force")
    assert code == 2 and report["error_code"] == "NORMALIZATION_PROFILE_UNSUPPORTED"
    assert before == workspace_bytes(workspace.root)


def test_unselected_future_html_does_not_expand_selected_normalization(html_workspace):
    workspace = html_workspace
    edit_metadata(workspace.paths["gateway"], changes={"html_usability_version": 2})
    edit_metadata(workspace.paths["study"], remove=("html_usability_version",))
    untouched = workspace.paths["gateway"].read_bytes()
    code, report = normalize_command(workspace.root, "--source-id", workspace.records["study"]["id"])
    assert code == 0 and report["summary"]["updated"] == 1
    assert workspace.paths["gateway"].read_bytes() == untouched


def test_legacy_cached_gateway_verdict_is_recomputed_without_changing_body_hash(html_workspace):
    workspace = html_workspace
    record = copy.deepcopy(workspace.records["gateway"])
    source = NORMALIZE.normalize_html_record(workspace.root, record)
    source.record.pop("evidence_usable", None)
    source.record.pop("unusable_evidence_reasons", None)
    source.warnings = [warning for warning in source.warnings if "unusable evidence:" not in warning]
    source.html_usability_version = None
    metadata = NORMALIZE.frontmatter_for(source, "sources/manifest.jsonl", workspace.paths["gateway"], "2026-09-28")
    workspace.paths["gateway"].write_text(NORMALIZE.render_markdown(source, metadata))
    assert metadata["evidence_usable"] is True and "html_usability_version" not in metadata
    assert owner("normalize_verify").run_verify(workspace.root, source_ids=[record["id"]])["overall_result"] == "verified"
    before = workspace.paths["gateway"].read_bytes()
    code, _ = normalize_command(workspace.root, "--source-id", record["id"])
    actual = NORMALIZE.read_output_frontmatter(workspace.paths["gateway"])
    assert code == 0 and not actual["evidence_usable"] and actual["html_usability_version"] == 1
    assert actual["content_hash"] == metadata["content_hash"]
    assert actual["raw_fingerprint"] == metadata["raw_fingerprint"]
    assert workspace.paths["gateway"].read_bytes() != before


def test_current_foreign_html_and_non_html_are_not_staled_by_missing_native_revision(html_workspace):
    workspace = html_workspace
    edit_metadata(workspace.paths["study"], changes={"normalizer": {"name": "external-html", "version": 3}},
                  remove=("html_usability_version",))
    before = workspace_bytes(workspace.root)
    code, report = normalize_command(workspace.root)
    assert code == 0 and report["summary"]["selected"] == 0
    assert before == workspace_bytes(workspace.root)


def test_force_explicitly_refreshes_a_current_supported_html_record(html_workspace, monkeypatch):
    workspace = html_workspace
    previous = NORMALIZE.read_output_frontmatter(workspace.paths["study"])
    monkeypatch.setattr(NORMALIZE, "timestamp_utc", lambda: "2030-01-01T00:00:00Z")
    before = workspace_bytes(workspace.root)
    code, report = normalize_command(workspace.root, "--source-id", workspace.records["study"]["id"], "--force")
    assert code == 0 and report["summary"]["updated"] == 1 and report["summary"]["stale"] == 0
    actual = NORMALIZE.read_output_frontmatter(workspace.paths["study"])
    assert actual["normalized_at"] == "2030-01-01T00:00:00Z"
    assert actual["created"] == previous["created"] and actual["content_hash"] == previous["content_hash"]
    after = workspace_bytes(workspace.root)
    assert {name for name in before if before[name] != after[name]} == {
        workspace.paths["study"].relative_to(workspace.root).as_posix(),
    }
