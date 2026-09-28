"""Classification qualifications across direct coverage, revision and retrieval owners."""

import contextlib
import io
import json

import pytest
import yaml

from evidence_wiki.pack_discovery import owner
from evidence_wiki.source_inspection import inspect
from tests.test_html_usability_profile import CONTRACT, edit_metadata, normalize_command, workspace_bytes
from tests.test_html_usability_profile import html_workspace as html_workspace


@pytest.mark.parametrize("case", ["current", "legacy", "future", "invalid", "shell", "mixed", "mixed_stale", "legacy_shell",
                                  "foreign", "table", "missing", "unidentified"])
def test_direct_coverage_checks_every_selected_source_without_inspection(html_workspace, case):
    workspace = html_workspace
    if case in {"legacy", "foreign", "mixed_stale"}:
        edit_metadata(workspace.paths["study"], remove=("html_usability_version",),
                      changes={"normalizer": {"name": "external-html", "version": "1"}} if case == "foreign" else {})
    elif case in {"future", "invalid"}:
        edit_metadata(workspace.paths["study"], changes={"html_usability_version": 2 if case == "future" else True})
    elif case == "missing":
        workspace.paths["study"].unlink()
    elif case == "unidentified":
        edit_metadata(workspace.paths["study"], remove=("normalizer",))
    elif case == "legacy_shell":
        edit_metadata(workspace.paths["gateway"], remove=("html_usability_version",))
    keys = (["gateway"] if case in {"shell", "legacy_shell"} else ["study", "gateway"] if case == "mixed"
            else ["study", "measurements"] if case == "mixed_stale" else ["measurements"] if case == "table" else ["study"])
    selected = [workspace.records[key]["id"] for key in keys]
    (workspace.root / "sources/jurisdictions.yml").write_text(yaml.safe_dump({"jurisdiction_profiles": [
        {"jurisdiction_id": "fixture-authority", "name": "Fixture authority", "official_domains": ["example.org"], "blocked_domains": []}]}))
    policies = owner("_evidence_policies")
    inputs = policies.load_policy_inputs(workspace.root)
    before = workspace_bytes(workspace.root)
    results = [policies.evaluate_source_policy("official_primary", selected, inputs),
               policies.evaluate_freshness_policy("no_staleness_check", selected, inputs),
               policies.evaluate_identity_policy("none", selected, inputs)]
    passes = case in {"current", "foreign", "table"}
    assert [result.verdict for result in results] == ["pass" if passes else "fail"] * 3
    reasons = {"legacy": "html_usability_recheck_required", "mixed_stale": "html_usability_recheck_required",
               "legacy_shell": "html_usability_recheck_required", "future": "html_usability_profile_unsupported",
               "invalid": "html_usability_profile_invalid", "missing": "html_usability_profile_invalid",
               "unidentified": "html_usability_profile_invalid", "shell": "html_error_page:official_error_page",
               "mixed": "html_error_page:official_error_page"}
    if not passes:
        assert all(any(reasons[case] in reason for reason in result.reasons) for result in results)
    if case == "legacy_shell":
        assert all(any("html_error_page:official_error_page" in reason for reason in result.reasons) for result in results)
    if case == "legacy":
        assert inputs.normalized_records[selected[0]]["evidence_usable"] is True
        assert all("marked unusable evidence" not in reason for result in results for reason in result.reasons)
    assert workspace_bytes(workspace.root) == before

    coverage = {"schema_version": "1.0", "question_slug": "measurements", "created_at": "2026-09-28T10:00:00Z",
                "updated_at": "2026-09-28T10:00:00Z", "coverage_profile": "official-observation", "coverage_verdict": "pending",
                "required_facets": [{"facet_id": "measurement", "description": "Retained official observation.", "required": True,
                    "evidence_path": "official_guidance", "source_policy": "official_primary", "freshness_policy": "no_staleness_check",
                    "identity_policy": "none", "min_sources": 1, "accepted_source_ids": selected,
                    "blocking_request_ids": [], "facet_verdict": "pending"}], "optional_facets": []}
    path = workspace.root / "sources/coverage/measurements.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(coverage))
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = owner("coverage_manifest").main(["--project-root", str(workspace.root), "evaluate", "--slug", "measurements", "--format", "json"])
    assert code == 0, stderr.getvalue()
    report = json.loads(stdout.getvalue())
    assert report["coverage_verdict"] == ("pass" if passes else "blocked")
    if not passes:
        assert reasons[case] in json.dumps(report["policy_results"])


def test_refresh_changes_the_workspace_revision_without_changing_extracted_text(html_workspace):
    workspace = html_workspace
    old = edit_metadata(workspace.paths["study"], remove=("html_usability_version",))
    revision = owner("_evidence_revision")
    before = revision.capture_workspace(workspace.root)
    code, _ = normalize_command(workspace.root, "--source-id", workspace.records["study"]["id"])
    assert code == 0
    after = revision.capture_workspace(workspace.root)
    metadata, _, error = CONTRACT.split_record(workspace.paths["study"].read_text())
    assert error is None and metadata["html_usability_version"] == 1
    assert metadata["content_hash"] == old["content_hash"]
    assert metadata["raw_fingerprint"] == old["raw_fingerprint"]
    assert after.revision_id != before.revision_id
    changed = {path for path in before.files if before.files[path] != after.files[path]}
    assert changed == {workspace.paths["study"].relative_to(workspace.root).as_posix()}


@pytest.mark.parametrize("key,query", [("gateway", "Bad Gateway"), ("study", "Measured reflectance")])
@pytest.mark.parametrize("state", ["current", "legacy", "future", "null", "boolean", "string", "zero"])
def test_format_verification_and_search_do_not_promote_classification_readiness(html_workspace, key, query, state):
    workspace = html_workspace
    path = workspace.paths[key]
    values = {"future": 2, "null": None, "boolean": True, "string": "1", "zero": 0}
    if state == "legacy":
        edit_metadata(path, remove=("html_usability_version",))
    elif state in values:
        edit_metadata(path, changes={"html_usability_version": values[state]})
    metadata, body, error = CONTRACT.split_record(path.read_text())
    assert error is None and metadata["normalizer"]["version"] == 3
    assert metadata["normalized_format"] == 1 and metadata["status"] == "content_extracted"
    assert metadata["evidence_usable"] is (key == "study")
    if key == "gateway":
        assert metadata["unusable_evidence_reasons"] == ["html_error_page:official_error_page"]
        assert any("unusable evidence: html_error_page:official_error_page" in warning for warning in metadata["parse_warnings"])
        assert "unusable evidence: html_error_page:official_error_page" in body
        assert "502 Bad Gateway" in body
    before = workspace_bytes(workspace.root)
    source_id = workspace.records[key]["id"]
    verifier = owner("normalize_verify")
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = verifier.main(["--project-root", str(workspace.root), "--source-id", source_id, "--format", "json"])
    verified = state in {"current", "legacy", "future"}
    report = json.loads(stdout.getvalue())
    assert code == (0 if verified else verifier.EXIT_NOT_VERIFIED)
    assert report["overall_result"] == ("verified" if verified else "not_verified")
    assert report["contract"]["written_version"] == 1
    if not verified:
        assert any(row["field"] == "html_usability_version" for row in report["records"][0]["violations"])
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        assert owner("query_index").main([query, "--project-root", str(workspace.root), "--scope", "normalized", "--format", "json"]) == 0
    hits = json.loads(stdout.getvalue())["results"]
    hit = next(hit for hit in hits if source_id in hit["source_ids"])
    assert hit["path"] == path.relative_to(workspace.root).as_posix()
    row = inspect(target=workspace.root, source_ids=[source_id])["sources"][0]
    ready = key == "study" and state == "current"
    assert row["usability"] == ("usable" if ready else "not_ready")
    assert not row["evidence_accepted"] and row["semantic_adequacy"] == "not_evaluated"
    if verified:
        assert row["complete"] and row["retrieval"] == "lexically_indexable"
        assert row["evidence_usable"] is metadata["evidence_usable"]
    policies = owner("_evidence_policies")
    result = policies.evaluate_identity_policy("none", [source_id], policies.load_policy_inputs(workspace.root))
    assert result.verdict == ("pass" if ready else "fail")
    assert workspace_bytes(workspace.root) == before


@pytest.mark.parametrize("key", ["study", "measurements"])
@pytest.mark.parametrize("marker", ["absent", None, False, "1", 2])
def test_foreign_html_and_native_tables_keep_their_format_readiness_and_admission_contracts(html_workspace, key, marker):
    workspace = html_workspace
    changes = {"normalizer": {"name": "external-html", "version": "1.0"}} if key == "study" else {}
    if marker != "absent":
        changes["html_usability_version"] = marker
    edit_metadata(workspace.paths[key], changes=changes,
                  remove=("html_usability_version",) if marker == "absent" else ())
    source_id = workspace.records[key]["id"]
    before = workspace_bytes(workspace.root)
    report = owner("normalize_verify").run_verify(workspace.root, source_ids=[source_id])
    assert report["overall_result"] == "verified"
    assert report["counts"]["external" if key == "study" else "native"] == 1
    row = inspect(target=workspace.root, source_ids=[source_id])["sources"][0]
    assert row["usability"] == "usable" and not row["evidence_accepted"]
    policies = owner("_evidence_policies")
    result = policies.evaluate_identity_policy("none", [source_id], policies.load_policy_inputs(workspace.root))
    assert result.verdict == "pass"
    assert workspace_bytes(workspace.root) == before
