"""Classification qualifications across direct coverage, revision and retrieval owners."""

import contextlib
import hashlib
import io
import json

import pytest
import yaml

from evidence_wiki.pack_discovery import owner
from evidence_wiki.source_inspection import inspect
from tests.test_html_usability_profile import CONTRACT, CURRENT, edit_metadata, normalize_command, workspace_bytes
from tests.test_html_usability_profile import html_workspace as html_workspace


def evaluate_selected_policies(workspace, selected):
    policies = owner("_evidence_policies")
    inputs = policies.load_policy_inputs(workspace.root)
    return [policies.evaluate_source_policy("official_primary", selected, inputs),
            policies.evaluate_freshness_policy("no_staleness_check", selected, inputs),
            policies.evaluate_identity_policy("none", selected, inputs)]


def evaluate_coverage(workspace, source_id):
    from tools.probe_html_usability import coverage_document

    path = workspace.root / "sources/coverage/identity-review.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(coverage_document("identity-review", source_id)))
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert owner("coverage_manifest").main([
            "--project-root", str(workspace.root), "evaluate", "--slug", "identity-review", "--format", "json"]) == 0
    return json.loads(output.getvalue())


def test_a_retained_copy_cannot_hide_a_rejected_canonical_capture(html_workspace):
    workspace = html_workspace
    (workspace.root / "sources/jurisdictions.yml").write_text(yaml.safe_dump({"jurisdiction_profiles": [
        {"jurisdiction_id": "fixture-authority", "name": "Fixture authority", "official_domains": ["example.org"], "blocked_domains": []}]}))
    source_id = workspace.records["study"]["id"]
    path = workspace.paths["study"]
    backup = path.parent / "zz-retained-study.md"
    backup.write_bytes(path.read_bytes())
    raw = workspace.root / workspace.records["study"]["raw_paths"][0]
    raw.write_bytes(b'<p>502 Bad Gateway. Please try again later.</p>')
    sidecar = raw.with_name(raw.name + ".provenance.yml")
    provenance = yaml.safe_load(sidecar.read_text())
    provenance["checksum"] = "sha256:" + hashlib.sha256(raw.read_bytes()).hexdigest()
    sidecar.write_text(yaml.safe_dump(provenance))
    with contextlib.redirect_stdout(io.StringIO()):
        assert owner("source_inventory").main(["--project-root", str(workspace.root)]) == 0
    assert normalize_command(workspace.root, "--source-id", source_id)[0] == 0
    assert inspect(target=workspace.root, source_ids=[source_id])["sources"][0]["usability"] == "not_ready"
    before = workspace_bytes(workspace.root)
    results = evaluate_selected_policies(workspace, [source_id])
    assert all(result.verdict == "fail" for result in results)
    assert all(any("normalized_record_ambiguous" in reason for reason in result.reasons) for result in results)
    assert workspace_bytes(workspace.root) == before
    assert evaluate_coverage(workspace, source_id)["coverage_verdict"] == "blocked"
    backup.unlink()
    assert evaluate_coverage(workspace, source_id)["coverage_verdict"] == "blocked"


@pytest.mark.parametrize("copy_name", ["aaa-copy.md", "zzz-copy.md", "nested/copy.md"])
@pytest.mark.parametrize("state", ["current", "legacy", "future", "foreign", "table"])
def test_duplicate_identity_refuses_selected_sources_without_replacing_canonical_metadata(html_workspace, copy_name, state):
    workspace = html_workspace
    key = "measurements" if state == "table" else "study"
    path = workspace.paths[key]
    copy = path.parent / copy_name
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(path.read_bytes())
    if state == "legacy":
        edit_metadata(path, remove=("html_usability_version",))
    elif state == "future":
        edit_metadata(path, changes={"html_usability_version": owner("_html_usability_profile").HTML_USABILITY_VERSION + 1})
    elif state == "foreign":
        edit_metadata(path, changes={"normalizer": {"name": "external-html", "version": "1"}})
    source_id = workspace.records[key]["id"]
    before = workspace_bytes(workspace.root)
    policies = owner("_evidence_policies")
    inputs = policies.load_policy_inputs(workspace.root)
    actual, _, _ = CONTRACT.split_record(path.read_text())
    assert inputs.normalized_records[source_id] == actual
    issue = next(issue for issue in inputs.normalized_record_issues[source_id] if issue["reason"] == "normalized_record_ambiguous")
    assert issue["paths"] == sorted([path.name, copy_name])
    assert all(result.verdict == "fail" for result in evaluate_selected_policies(workspace, [source_id]))
    assert all("normalized_record_ambiguous" in " ".join(result.reasons) for result in evaluate_selected_policies(workspace, [source_id]))
    other = workspace.records["study" if key == "measurements" else "measurements"]["id"]
    assert policies.evaluate_freshness_policy("no_staleness_check", [other], inputs).verdict == "pass"
    assert policies.evaluate_identity_policy("none", [other], inputs).verdict == "pass"
    assert all(result.verdict == "fail" for result in evaluate_selected_policies(workspace, [source_id, other]))
    assert workspace_bytes(workspace.root) == before


@pytest.mark.parametrize("defect", ["noncanonical", "missing_id", "wrong_id", "wrong_type", "malformed", "invalid_utf8"])
def test_canonical_record_identity_cannot_fall_back_to_another_file(html_workspace, defect):
    workspace = html_workspace
    path = workspace.paths["study"]
    source_id = workspace.records["study"]["id"]
    if defect == "noncanonical":
        other = path.parent / "nested/elsewhere.md"
        other.parent.mkdir()
        path.rename(other)
    elif defect == "missing_id":
        edit_metadata(path, remove=("source_id",))
    elif defect == "wrong_id":
        edit_metadata(path, changes={"source_id": "manual:other"})
    elif defect == "wrong_type":
        edit_metadata(path, changes={"type": "source_note"})
    elif defect == "invalid_utf8":
        path.write_bytes(path.read_bytes().replace(b"source_id: ", b"source_id: \xff", 1))
    else:
        path.write_text("---\nsource_id: [\n---\n")
    if defect in {"missing_id", "wrong_id", "wrong_type"}:
        actual, _, _ = CONTRACT.split_record(path.read_text())
        inputs = owner("_evidence_policies").load_policy_inputs(workspace.root)
        assert inputs.normalized_records[source_id] == actual
    reason = "normalized_record_noncanonical" if defect == "noncanonical" else "normalized_record_identity_invalid"
    before = workspace_bytes(workspace.root)
    for result in evaluate_selected_policies(workspace, [source_id]):
        assert result.verdict == "fail" and any(reason in entry for entry in result.reasons)
    if defect == "wrong_id":
        for result in evaluate_selected_policies(workspace, ["manual:other"]):
            assert result.verdict == "fail"
            assert any("normalized_record_noncanonical" in entry for entry in result.reasons)
    assert workspace_bytes(workspace.root) == before


def test_manifest_id_collisions_cannot_share_one_canonical_record(html_workspace):
    workspace = html_workspace
    path = workspace.paths["study"]
    record = dict(workspace.records["study"])
    source_ids = ["manual:Observation", "manual:observation"]
    manifest = workspace.root / "sources/manifest.jsonl"
    manifest.write_text("".join(json.dumps({**record, "id": source_id}) + "\n" for source_id in source_ids))
    edit_metadata(path, changes={"source_id": source_ids[0]})
    path.rename(CONTRACT.expected_record_path(path.parent, source_ids[0]))
    for source_id in source_ids:
        for result in evaluate_selected_policies(workspace, [source_id]):
            assert result.verdict == "fail" and any("normalized_record_ambiguous" in entry for entry in result.reasons)


def test_canonical_symlink_cannot_supply_normalized_metadata(html_workspace):
    workspace = html_workspace
    path = workspace.paths["study"]
    retained = workspace.root.parent / "retained.md"
    path.rename(retained)
    path.symlink_to(retained)
    for result in evaluate_selected_policies(workspace, [workspace.records["study"]["id"]]):
        assert result.verdict == "fail" and any("normalized_record_identity_invalid" in entry for entry in result.reasons)


def test_acquisition_quality_requires_current_native_html(html_workspace):
    workspace = html_workspace
    path = workspace.paths["study"]
    edit_metadata(path, remove=("html_usability_version",))
    before = workspace_bytes(workspace.root)
    failure = owner("orchestration_controller").normalized_source_quality_failure(
        workspace.root, path, workspace.records["study"])
    assert failure is not None and failure["reason"] == "html_usability_recheck_required"
    assert workspace_bytes(workspace.root) == before


def test_acquisition_preflight_does_not_freeze_legacy_html(html_workspace):
    workspace = html_workspace
    edit_metadata(workspace.paths["study"], remove=("html_usability_version",))
    manifest = workspace.root / "sources/manifest.jsonl"
    records = [json.loads(line) for line in manifest.read_text().splitlines()]
    for record in records:
        if record["id"] == workspace.records["study"]["id"]:
            record["provenance"]["request_id"] = "request-html"
    manifest.write_text("".join(json.dumps(record) + "\n" for record in records))
    controller = owner("orchestration_controller")
    before = workspace_bytes(workspace.root)
    with pytest.raises(controller.OrchestrationControllerError) as caught:
        controller.acquisition_reuse_baselines(workspace.root, controller.load_config(workspace.root), ["request-html"], None)
    assert "html_usability_recheck_required" in json.dumps(caught.value.details)
    assert workspace_bytes(workspace.root) == before


@pytest.mark.parametrize("case", ["current", "legacy", "future", "invalid", "shell", "mixed", "mixed_stale", "legacy_shell",
                                  "foreign", "table", "missing", "unidentified"])
def test_direct_coverage_checks_every_selected_source_without_inspection(html_workspace, case):
    workspace = html_workspace
    if case in {"legacy", "foreign", "mixed_stale"}:
        edit_metadata(workspace.paths["study"], remove=("html_usability_version",),
                      changes={"normalizer": {"name": "external-html", "version": "1"}} if case == "foreign" else {})
    elif case in {"future", "invalid"}:
        edit_metadata(workspace.paths["study"], changes={"html_usability_version": CURRENT + 1 if case == "future" else True})
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
    assert error is None and metadata["html_usability_version"] == CURRENT
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
    values = {"future": CURRENT + 1, "null": None, "boolean": True, "string": "1", "zero": 0}
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
