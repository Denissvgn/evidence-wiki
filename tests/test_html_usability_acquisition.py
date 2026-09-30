"""HTML currency is checked before acquisition reuse and durable fulfilment."""

import json
from types import SimpleNamespace

import pytest

from evidence_wiki.pack_discovery import owner
from tests import test_contingent_bookkeeping_baseline as bookkeeping
from tests.test_contingent_bookkeeping_baseline import stored_request
from tests.test_delegated_acquisition_e2e import ACQUIRER, CONTROLLER, ORCHESTRATION_ID, QUESTION_SLUG
from tests.test_html_usability_profile import CURRENT, edit_metadata, normalize_command, workspace_bytes
from tests.test_html_usability_profile import html_workspace as html_workspace


def acquisition_workspace(tmp_path, mode):
    driver = bookkeeping.ProviderAcquisitionBookkeepingTests()
    root = driver.init_workspace(tmp_path)
    if mode == "provider":
        driver.enable_providers(root)
    else:
        driver.configure(root, delegated=True)
    request_id = driver.block_question_on_a_request(root)
    if mode == "provider":
        driver.select_candidate(root, request_id)
    source_id = driver.deliver_paper(root, request_id)
    path = driver.normalized_record_for(root, source_id)
    assert owner("normalize_sources").read_output_frontmatter(path)["html_usability_version"] == CURRENT
    return SimpleNamespace(root=root, driver=driver, request_id=request_id, source_id=source_id, path=path, mode=mode)


def change_revision(path, state):
    if state == "absent":
        edit_metadata(path, remove=("html_usability_version",))
    else:
        edit_metadata(path, changes={"html_usability_version": {"prior": 1, "future": CURRENT + 1,
                                                               "invalid": True}[state]})


def profile_reason(state):
    return {"absent": "html_usability_recheck_required", "prior": "html_usability_recheck_required",
            "future": "html_usability_profile_unsupported", "invalid": "html_usability_profile_invalid"}[state]


@pytest.mark.parametrize("mode", ["provider", "delegated"])
@pytest.mark.parametrize("state", ["absent", "prior", "future", "invalid"])
def test_acquisition_does_not_issue_an_order_over_noncurrent_html(tmp_path, mode, state):
    w = acquisition_workspace(tmp_path, mode)
    change_revision(w.path, state)
    w.driver.start(w.root)
    evidence = w.driver.evidence_bytes(w.root)
    requests = (w.root / "sources/source-requests.jsonl").read_bytes()
    question = w.root / f"wiki/questions/{QUESTION_SLUG}.md"
    before_question = question.read_bytes()
    code, result = w.driver.next_action(w.root)
    assert code == CONTROLLER.EXIT_INVALID and result["error_code"] == "ORCHESTRATION_POSTCONDITION_FAILED"
    assert result["details"]["quality_failures"][0]["reason"] == profile_reason(state)
    assert result["details"]["quality_failures"][0]["source_id"] == w.source_id
    assert w.driver.evidence_bytes(w.root) == evidence
    assert (w.root / "sources/source-requests.jsonl").read_bytes() == requests
    assert question.read_bytes() == before_question
    session = CONTROLLER.load_session(w.root, ORCHESTRATION_ID)
    assert session.get("pending_action_id") is None and session.get("active_run_id") is None


@pytest.mark.parametrize("mode", ["provider", "delegated"])
@pytest.mark.parametrize("state", ["absent", "prior", "future", "invalid"])
def test_acquisition_refuses_noncurrent_claims_before_committing_requests(tmp_path, monkeypatch, mode, state):
    w = acquisition_workspace(tmp_path, mode)
    w.driver.start(w.root)
    code, order = w.driver.next_action(w.root)
    assert code == 0 and order["phase"] == "acquisition"
    w.driver.fulfil_and_reopen(w.root, w.request_id, w.source_id)
    if mode == "provider":
        w.driver.discover(w.root, ["candidates", "transition", "--candidate-id", w.driver.CANDIDATE_ID,
            "--expected-state", "selected", "--to-state", "fetched", "--source-id", w.source_id,
            "--reason", "Retained source is available for the scoped request.", "--actor", ACQUIRER,
            "--run-id", order["run_id"]])
    change_revision(w.path, state)
    evidence = w.driver.evidence_bytes(w.root)
    requests = (w.root / "sources/source-requests.jsonl").read_bytes()
    question = w.root / f"wiki/questions/{QUESTION_SLUG}.md"
    before_question = question.read_bytes()
    frozen = CONTROLLER.work_order_path(w.root, ORCHESTRATION_ID, order["action_id"])
    before_order = frozen.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("Acquisition verification executed HTML normalization")

    normalizer = CONTROLLER.load_sibling_module("normalize_sources")
    for name in ("normalize_html_record", "html_unusable_evidence_reasons", "run_normalization"):
        monkeypatch.setattr(normalizer, name, forbidden)
    code, result = w.driver.submit(w.root, order["action_id"])
    assert code == CONTROLLER.EXIT_INVALID and result["error_code"] == "ORCHESTRATION_POSTCONDITION_FAILED"
    assert result["details"]["quality_failures"][0]["reason"] == profile_reason(state)
    assert stored_request(w.root, w.request_id)["status"] == "open"
    assert w.driver.question_status(w.root) == "blocked"
    assert w.driver.evidence_bytes(w.root) == evidence
    assert (w.root / "sources/source-requests.jsonl").read_bytes() == requests
    assert question.read_bytes() == before_question and frozen.read_bytes() == before_order


@pytest.mark.parametrize("case", ["current", "shell", "foreign", "table", "unnormalized"])
@pytest.mark.parametrize("candidate_scope", [None, ["candidate-html"]])
def test_acquisition_preflight_preserves_current_and_other_source_contracts(html_workspace, case, candidate_scope):
    w = html_workspace
    key = "gateway" if case == "shell" else "measurements" if case == "table" else "study"
    path = w.paths[key]
    source_id = w.records[key]["id"]
    if case == "foreign":
        edit_metadata(path, changes={"normalizer": {"name": "external-html", "version": "1"}}, remove=("html_usability_version",))
    elif case == "unnormalized":
        path.unlink()
    manifest = w.root / "sources/manifest.jsonl"
    rows = [json.loads(line) for line in manifest.read_text().splitlines()]
    for row in rows:
        if row["id"] == source_id:
            row["provenance"].update(request_id="request-html", candidate_id="candidate-html")
    manifest.write_text("".join(json.dumps(row) + "\n" for row in rows))
    other = "study" if key != "study" else "gateway"
    edit_metadata(w.paths[other], remove=("html_usability_version",))
    controller = owner("orchestration_controller")
    before = workspace_bytes(w.root)
    matched, reusable, _ = controller.acquisition_reuse_baselines(
        w.root, controller.load_config(w.root), ["request-html"], candidate_scope)
    assert set(matched) == (set() if case == "unnormalized" else {source_id})
    assert reusable == ([source_id] if case == "unnormalized" else [])
    if case != "unnormalized":
        failure = controller.normalized_source_quality_failure(w.root, path, next(r for r in rows if r["id"] == source_id))
        assert (failure is not None) is (case == "shell")
    assert workspace_bytes(w.root) == before


@pytest.mark.parametrize("mode", ["provider", "delegated"])
def test_acquisition_can_restart_after_explicit_legacy_refresh(tmp_path, mode):
    w = acquisition_workspace(tmp_path, mode)
    change_revision(w.path, "prior")
    w.driver.start(w.root)
    assert w.driver.next_action(w.root)[0] == CONTROLLER.EXIT_INVALID
    code, abandoned = w.driver.controller(w.root, "abandon", "--orchestration-id", ORCHESTRATION_ID,
        "--agent-id", "pm-agent", "--reason", "Refresh retained HTML before issuing acquisition.")
    assert code == CONTROLLER.EXIT_INVALID and abandoned["status"] == "failed"
    assert normalize_command(w.root, "--source-id", w.source_id)[0] == 0
    fresh_id = "orch-current-html"
    assert w.driver.controller(w.root, "start", "--orchestration-id", fresh_id, "--agent-id", "pm-agent")[0] == 0
    code, order = w.driver.controller(w.root, "next", "--orchestration-id", fresh_id)
    assert code == 0 and order["phase"] == "acquisition", order
    order = CONTROLLER.hydrate_integrity_baselines(w.root, order)
    guard = next(row for row in order["required_postconditions"] if row["check"] == "manifest_records_increased")
    assert guard["matching_source_records_before"][w.source_id]["normalized_fingerprint"] == CONTROLLER.file_digest(w.path)


@pytest.mark.parametrize("mode", ["provider", "delegated"])
def test_current_html_reuse_commits_without_rewriting_evidence(tmp_path, mode):
    w = acquisition_workspace(tmp_path, mode)
    w.driver.start(w.root)
    code, order = w.driver.next_action(w.root)
    assert code == 0 and order["phase"] == "acquisition", order
    before = w.driver.evidence_bytes(w.root)
    w.driver.fulfil_and_reopen(w.root, w.request_id, w.source_id)
    if mode == "provider":
        w.driver.discover(w.root, ["candidates", "transition", "--candidate-id", w.driver.CANDIDATE_ID,
            "--expected-state", "selected", "--to-state", "fetched", "--source-id", w.source_id,
            "--reason", "Retained source is available for the scoped request.", "--actor", ACQUIRER,
            "--run-id", order["run_id"]])
    code, result = w.driver.submit(w.root, order["action_id"])
    assert code == 0, result
    assert stored_request(w.root, w.request_id)["status"] == "fulfilled"
    assert w.driver.question_status(w.root) == "open"
    assert w.driver.evidence_bytes(w.root) == before


def test_acquisition_quality_uses_actual_normalization_method(html_workspace, monkeypatch):
    w = html_workspace
    controller = owner("orchestration_controller")
    normalizer = controller.load_sibling_module("normalize_sources")
    monkeypatch.setattr(normalizer, "normalization_method", lambda *args: "adapter")
    failure = controller.normalized_source_quality_failure(w.root, w.paths["study"], w.records["study"])
    assert failure["reason"] == "html_usability_profile_invalid"
