"""Public authoring and request operations retain canonical owners and refusals."""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager

import pytest
import yaml

from evidence_wiki import Workspace, _script_host
from evidence_wiki._facades.source_requests import SourceRequestsNamespace
from evidence_wiki._facades.strict import StrictNamespace
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext


def owner(stem):
    return _script_host.load_packaged_script(_script_host.shared_assets_root(), stem)


@pytest.fixture
def workspace_root(tmp_path, monkeypatch):
    monkeypatch.delenv("EVIDENCE_WIKI_AUTHORITY_FILE", raising=False)
    monkeypatch.delenv("EVIDENCE_WIKI_STATE_DIR", raising=False)
    root = tmp_path / "workspace"
    (root / "wiki/questions").mkdir(parents=True)
    (root / "sources").mkdir()
    (root / "research.yml").write_text(yaml.safe_dump({"wiki": {"root": "wiki"},
        "sources": {"manifest_path": "sources/manifest.jsonl", "coverage_dir": "sources/coverage"}}))
    (root / "wiki/questions/sample.md").write_text("---\ntype: question\nstatus: open\nquestion: What is observed?\n---\n")
    (root / "sources/manifest.jsonl").write_text(json.dumps({"id": "sample-source",
        "provenance": {"scope": {"facet_id": "primary"}}}) + "\n")
    return root


@pytest.fixture
def strict_host(tmp_path, monkeypatch, request):
    from tests.test_strict_evidence import host

    return host.__wrapped__(tmp_path, monkeypatch, request)


def template():
    return {"coverage_profile": "declared", "required_facets": [{"facet_id": "primary", "description": "Primary observation",
        "evidence_path": "official_guidance", "source_policy": "official_primary", "freshness_policy": "no_staleness_check",
        "identity_policy": "official_domain_match", "min_sources": 1}], "optional_facets": []}


def request_namespace(root):
    workspace = Workspace.open(root)
    return workspace, SourceRequestsNamespace(workspace)


def test_inline_coverage_matches_canonical_owner_and_does_not_mutate_input(workspace_root, capsys, monkeypatch):
    coverage = owner("coverage_manifest")
    monkeypatch.setattr(coverage, "timestamp_utc", lambda: "2026-10-02T00:00:00Z")
    monkeypatch.setattr(coverage, "main", lambda *a, **k: pytest.fail("CLI main"))
    monkeypatch.setattr(coverage, "parse_args", lambda *a, **k: pytest.fail("CLI parser"))
    value = template()
    original = copy.deepcopy(value)
    with Workspace.open(workspace_root) as workspace:
        result = workspace.coverage.init("sample", template=value)
    expected = coverage.run_init_document(workspace_root, coverage.load_config(workspace_root), slug="sample",
                                         template=original, force=True)
    assert result == expected and value == original
    result["manifest"]["required_facets"][0]["accepted_source_ids"].append("caller-edit")
    stored = yaml.safe_load((workspace_root / "sources/coverage/sample.yml").read_text())
    assert stored["required_facets"][0]["accepted_source_ids"] == []
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("arguments, code", [
    ({"force": "yes"}, "VALUE_INVALID"), ({"force": 1}, "VALUE_INVALID"),
    ({"coverage_profile": 3}, "VALUE_INVALID"), ({"coverage_profile": " "}, "VALUE_INVALID"),
    ({"template": []}, "COVERAGE_TEMPLATE_INVALID"), ({"template": {"unexpected": True}}, "COVERAGE_TEMPLATE_INVALID"),
    ({"template": {"required_facets": [{"min_sources": float("nan")}] }}, "COVERAGE_TEMPLATE_INVALID"),
])
def test_coverage_arguments_refuse_without_writes(workspace_root, arguments, code):
    with Workspace.open(workspace_root) as workspace, pytest.raises(EvidenceWikiError) as exc:
        workspace.coverage.init("sample", **arguments)
    assert exc.value.error_code == code
    assert not (workspace_root / "sources/coverage/sample.yml").exists()


def test_coverage_existing_manifest_and_revision_refusals_preserve_codes(workspace_root):
    with Workspace.open(workspace_root) as workspace:
        workspace.coverage.init("sample", template=template())
        before = (workspace_root / "sources/coverage/sample.yml").read_bytes()
        with pytest.raises(EvidenceWikiError) as exc:
            workspace.coverage.init("sample", template=template())
        assert exc.value.error_code == "COVERAGE_MANIFEST_EXISTS"
        with pytest.raises(EvidenceWikiError) as exc:
            workspace.coverage.revision({})
        assert exc.value.error_code.startswith("DOMAIN_PACK_REVISION")
        assert (workspace_root / "sources/coverage/sample.yml").read_bytes() == before


def test_coverage_links_declared_sources_and_backfills_request_scope(workspace_root):
    handle, requests = request_namespace(workspace_root)
    with handle:
        added = requests.add(kind="other", query_or_identifier="Observed", rationale="Gap", question_slugs=["sample"])
        request_id = added["request"]["request_id"]
        handle.coverage.init("sample", template=template())
        linked = handle.coverage.link("sample", "primary", accepted_source_ids=["sample-source"],
                                      blocking_request_ids=[request_id])
        facet = linked["manifest"]["required_facets"][0]
        assert facet["accepted_source_ids"] == ["sample-source"] and facet["blocking_request_ids"] == [request_id]
        assert linked["scope_backfilled_request_ids"] == [request_id]
        assert requests.list()["requests"][0]["scope"] == {"facet_id": "primary"}
        cleared = handle.coverage.link("sample", "primary", clear_accepted_source_ids=True, clear_blocking_request_ids=True)
        assert cleared["manifest"]["required_facets"][0]["accepted_source_ids"] == []
        assert cleared["manifest"]["required_facets"][0]["blocking_request_ids"] == []
        assert requests.list()["requests"][0]["scope"] == {"facet_id": "primary"}


@pytest.mark.parametrize("arguments", [{"clear_accepted_source_ids": 1}, {"clear_blocking_request_ids": "true"},
    {"accepted_source_ids": "sample-source"}, {"blocking_request_ids": ["x"] * 257},
    {"accepted_source_ids": ["x" * 513]}, {"accepted_source_ids": [1]}])
def test_coverage_link_arguments_refuse_before_write(workspace_root, arguments):
    with Workspace.open(workspace_root) as workspace:
        workspace.coverage.init("sample", template=template())
        before = (workspace_root / "sources/coverage/sample.yml").read_bytes()
        with pytest.raises(EvidenceWikiError) as exc:
            workspace.coverage.link("sample", "primary", **arguments)
        assert exc.value.error_code == "VALUE_INVALID"
        assert (workspace_root / "sources/coverage/sample.yml").read_bytes() == before


def test_coverage_scope_conflict_refuses_before_manifest_write(workspace_root):
    handle, requests = request_namespace(workspace_root)
    with handle:
        added = requests.add(kind="other", query_or_identifier="Observed", rationale="Gap", question_slugs=["sample"],
                             scope={"facet_id": "other"})
        handle.coverage.init("sample", template=template())
        before = (workspace_root / "sources/coverage/sample.yml").read_bytes()
        with pytest.raises(EvidenceWikiError) as exc:
            handle.coverage.link("sample", "primary", blocking_request_ids=[added["request"]["request_id"]])
        assert exc.value.error_code == "FACET_SCOPE_CONFLICT"
        assert (workspace_root / "sources/coverage/sample.yml").read_bytes() == before


def test_coverage_backfill_failure_retains_known_postwrite_failure(workspace_root, monkeypatch):
    coverage = owner("coverage_manifest")

    def backfill_failure(*args):
        raise OSError("synthetic backfill failure")

    monkeypatch.setattr(coverage, "backfill_facet_scope", backfill_failure)
    handle, requests = request_namespace(workspace_root)
    with handle:
        added = requests.add(kind="other", query_or_identifier="Observed", rationale="Gap", question_slugs=["sample"])
        handle.coverage.init("sample", template=template())
        with pytest.raises(EvidenceWikiError) as exc:
            handle.coverage.link("sample", "primary", blocking_request_ids=[added["request"]["request_id"]])
        assert exc.value.error_code == "WORKSPACE_UNREADABLE"
        assert exc.value.details["failed_step"] == "request_scope_backfill"
        manifest = yaml.safe_load((workspace_root / "sources/coverage/sample.yml").read_text())
        assert manifest["required_facets"][0]["blocking_request_ids"] == [added["request"]["request_id"]]


def test_source_add_list_fulfill_use_real_owners_without_cli_or_stdout(workspace_root, monkeypatch, capsys):
    requests = owner("source_requests")
    for name in ("main", "parse_args", "render_text_report"):
        monkeypatch.setattr(requests, name, lambda *a, **k: pytest.fail("CLI transport invoked"))
    handle, namespace = request_namespace(workspace_root)
    with handle:
        scope = {"facet_id": "primary"}
        first = namespace.add(kind="other", query_or_identifier="  Observations  ", rationale="A declared gap.",
                              priority="high", question_slugs=["sample"], scope=scope)
        assert first["created"] and scope == {"facet_id": "primary"}
        duplicate = namespace.add(kind="other", query_or_identifier="Observations", rationale="Replay.",
                                  question_slugs=["sample"], scope=scope)
        assert not duplicate["created"] and duplicate["request"]["request_id"] == first["request"]["request_id"]
        listed = namespace.list(statuses=["open"], kinds=["other"], scope=scope)
        assert listed["requests"] == [first["request"]]
        fulfilled = namespace.fulfill(request_id=first["request"]["request_id"], source_id="sample-source",
                                      match_scope=scope, require_scope=True)
        assert fulfilled["updated"] and not fulfilled["contingent"]
        assert fulfilled["request"]["status"] == "fulfilled"
        replay = namespace.fulfill(request_id=first["request"]["request_id"], source_id="sample-source",
                                  match_scope=scope, require_scope=True)
        assert not replay["updated"] and not namespace.list(statuses=["open"])["requests"]
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("method, arguments, code", [
    ("add", {"priority": "urgent"}, "REQUEST_PRIORITY_INVALID"),
    ("add", {"priority": 1}, "REQUEST_PRIORITY_INVALID"),
    ("add", {"question_slugs": "sample"}, "REQUEST_ARGUMENT_INVALID"),
    ("add", {"question_slugs": ["../escape"]}, "REQUEST_ARGUMENT_INVALID"),
    ("add", {"query_or_identifier": "x" * 4097}, "REQUEST_ARGUMENT_INVALID"),
    ("add", {"scope": {"facet_id=other": "primary"}}, "REQUEST_SCOPE_INVALID"),
    ("add", {"scope": {"UPPER": "primary"}}, "REQUEST_SCOPE_INVALID"),
    ("list", {"statuses": ["missing"]}, "REQUEST_STATUS_INVALID"),
    ("list", {"statuses": "open"}, "REQUEST_ARGUMENT_INVALID"),
    ("list", {"kinds": ["web"] * 257}, "REQUEST_ARGUMENT_INVALID"),
    ("fulfill", {"require_scope": 1}, "REQUEST_ARGUMENT_INVALID"),
])
def test_source_argument_choices_and_bounds_are_checked_before_engine(workspace_root, monkeypatch, method, arguments, code):
    requests = owner("source_requests")
    for name in ("run_add", "run_list", "run_fulfill"):
        monkeypatch.setattr(requests, name, lambda *a, **k: pytest.fail("owner engine received invalid arguments"))
    handle, namespace = request_namespace(workspace_root)
    with handle, pytest.raises(EvidenceWikiError) as exc:
        defaults = {"kind": "other", "query_or_identifier": "Observed", "rationale": "Gap"} if method == "add" else {
            "request_id": "request", "source_id": "sample-source"} if method == "fulfill" else {}
        getattr(namespace, method)(**{**defaults, **arguments})
    assert exc.value.error_code == code
    assert not (workspace_root / "sources/source-requests.jsonl").exists()


def test_kind_and_fulfillment_scope_refusals_keep_owner_codes(workspace_root):
    handle, namespace = request_namespace(workspace_root)
    with handle:
        with pytest.raises(EvidenceWikiError) as exc:
            namespace.add(kind="pack:missing/kind", query_or_identifier="Observed", rationale="Gap")
        assert exc.value.error_code == "REQUEST_KIND_UNDECLARED"
        result = namespace.add(kind="other", query_or_identifier="Observed", rationale="Gap", scope={"facet_id": "other"})
        before = (workspace_root / "sources/source-requests.jsonl").read_bytes()
        with pytest.raises(EvidenceWikiError) as exc:
            namespace.fulfill(request_id=result["request"]["request_id"], source_id="sample-source", require_scope=True)
        assert exc.value.error_code == "REQUEST_SCOPE_MISMATCH"
        assert (workspace_root / "sources/source-requests.jsonl").read_bytes() == before


def test_source_lock_refusal_is_typed_and_preserves_details(workspace_root, monkeypatch):
    requests = owner("source_requests")

    @contextmanager
    def unavailable(*args, **kwargs):
        raise requests.LockUnavailableError("Lock contended.", details={"reason": "contended"})
        yield

    monkeypatch.setattr(requests, "workspace_lock", unavailable)
    handle, namespace = request_namespace(workspace_root)
    with handle, pytest.raises(EvidenceWikiError) as exc:
        namespace.add(kind="other", query_or_identifier="Observed", rationale="Gap")
    assert exc.value.error_code == "LOCK_UNAVAILABLE" and exc.value.details["reason"] == "contended"
    assert not (workspace_root / "sources/source-requests.jsonl").exists()


def test_source_postwrite_failure_reports_unknown_commit_and_preserves_record(workspace_root, monkeypatch):
    requests = owner("source_requests")

    def log_failure(*args):
        raise OSError("synthetic log failure")

    monkeypatch.setattr(requests, "append_log_entry", log_failure)
    handle, namespace = request_namespace(workspace_root)
    with handle:
        with pytest.raises(EvidenceWikiError) as exc:
            namespace.add(kind="other", query_or_identifier="Observed", rationale="Gap")
        assert exc.value.error_code == "REQUEST_MUTATION_UNCERTAIN"
        assert not exc.value.recoverable and exc.value.details["commit_outcome"] == "unknown"
        assert len(namespace.list()["requests"]) == 1


def test_new_namespaces_refuse_closed_handles(workspace_root):
    handle, requests = request_namespace(workspace_root)
    strict = StrictNamespace(handle)
    handle.close()
    for call in (strict.check, lambda: requests.list(), lambda: handle.coverage.init("sample")):
        with pytest.raises(EvidenceWikiError) as exc:
            call()
        assert exc.value.error_code == "WORKSPACE_UNREADABLE"


def test_strict_checks_and_registered_review_use_canonical_authentication(strict_host, monkeypatch, capsys):
    from tests.test_strict_evidence import CORE, review

    strict = owner("strict_evidence")
    monkeypatch.setattr(strict, "main", lambda *a, **k: pytest.fail("CLI main"))
    with Workspace.open(strict_host.root) as workspace:
        namespace = StrictNamespace(workspace)
        before = (strict_host.host / "evidence-state.json").read_bytes()
        blocked = namespace.check()
        assert blocked["claims"] and not blocked["claims"][0]["accepted"]
        prepared = namespace.prepare_review("product-source")
        assert prepared["registration"]["action"] == "register-strict-review"
        assert (strict_host.host / "evidence-state.json").read_bytes() == before
        with pytest.raises(EvidenceWikiError):
            namespace.register_review({"payload": {"action": "register-strict-review", "principal": "evaluator", "controller": "claimed"}})
        assert (strict_host.host / "evidence-state.json").read_bytes() == before
        envelope = review(strict_host)
        original = copy.deepcopy(envelope)
        receipt = namespace.register_review(envelope)
        assert receipt["request_id"] == envelope["payload"]["request_id"] and envelope == original
        checked = namespace.check()
        assert checked["claims"][0]["accepted"]
        exported = namespace.export()
        assert exported["verdict"] == "ship" and exported["questions"] == CORE.publication(strict_host.root)["questions"]
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def outside_snapshot(path):
    return sorted((item.relative_to(path).as_posix(), item.read_bytes())
                  for item in path.rglob("*") if item.is_file())


@pytest.mark.parametrize("operation", ["request_add", "request_list", "request_fulfill", "coverage_init", "coverage_link", "coverage_evaluate"])
@pytest.mark.parametrize("alias", ["sources", "requests", "manifest", "log", "request_lock", "log_lock", "holder", "exclusive",
                                   "removal_guard", "metadata", "control_metadata", "question", "coverage_custom", "normalized_custom"])
def test_new_api_paths_reject_static_aliases_before_effects(workspace_root, tmp_path, operation, alias):
    outside = tmp_path / "outside"
    outside.mkdir()
    target_file = outside / "record"
    target_file.write_bytes(b"outside-marker")
    paths = {
        "requests": workspace_root / "sources/source-requests.jsonl", "manifest": workspace_root / "sources/manifest.jsonl",
        "log": workspace_root / "log.md", "request_lock": workspace_root / "sources/.locks/source-requests.lock",
        "log_lock": workspace_root / ".locks/log.lock", "holder": workspace_root / "sources/.locks/source-requests.lock.holder.json",
        "exclusive": workspace_root / "sources/.locks/source-requests.lock.exclusive",
        "metadata": workspace_root / "workspace-system.yml", "control_metadata": workspace_root / "runs/orchestrations/session/session.json",
        "question": workspace_root / "wiki/questions/sample.md",
    }
    with Workspace.open(workspace_root) as workspace:
        requests = SourceRequestsNamespace(workspace)
        seeded = requests.add(kind="other", query_or_identifier="Seeded observation", rationale="Gap", question_slugs=["sample"])
        workspace.coverage.init("sample", template=template())
        if alias in {"sources", "removal_guard", "coverage_custom", "normalized_custom"}:
            if alias == "sources":
                path = workspace_root / "sources"
                path.rename(workspace_root / "prior-sources")
            elif alias == "removal_guard":
                path = workspace_root / "sources/.locks/source-requests.lock.exclusive.removal-guard"
            else:
                field = "coverage_dir" if alias == "coverage_custom" else "normalized_dir"
                path = workspace_root / "sources/custom"
                config = yaml.safe_load((workspace_root / "research.yml").read_text())
                config["sources"][field] = "sources/custom"
                (workspace_root / "research.yml").write_text(yaml.safe_dump(config))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(outside, target_is_directory=True)
        else:
            path = paths[alias]
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                path.unlink()
            path.symlink_to(target_file)
        before = outside_snapshot(outside)
        grants = {"source_requests.run_add_request", "source_requests.run_list_requests", "source_requests.run_fulfill_request",
                  "coverage_manifest.run_init_inline", "coverage_manifest.run_link", "coverage_manifest.run_evaluate"}
        context = HostContext(allowed_roots=[workspace_root], writable_roots=[workspace_root], grants=grants, actor_ref="host")
        calls = {
            "request_add": lambda: requests.add(kind="other", query_or_identifier="Observed", rationale="Gap", question_slugs=["sample"]),
            "request_list": requests.list,
            "request_fulfill": lambda: requests.fulfill(request_id=seeded["request"]["request_id"], source_id="sample-source"),
            "coverage_init": lambda: workspace.coverage.init("sample", template=template()),
            "coverage_link": lambda: workspace.coverage.link("sample", "primary"),
            "coverage_evaluate": lambda: workspace.coverage.evaluate("sample"),
        }
        with context.activate(), pytest.raises(EvidenceWikiError) as error:
            calls[operation]()
        assert outside_snapshot(outside) == before
        expected = "HOST_CONTEXT_REFUSED" if alias in {"sources", "log"} else "WORKSPACE_PATH_REFUSED"
        assert error.value.error_code == expected
        assert "outside-marker" not in error.value.message + str(error.value.details)


@pytest.mark.parametrize("owner_name, seam, arguments", [
    ("source_requests", "run_add_request", {"kind": "other", "query_or_identifier": "Observed", "rationale": "Gap"}),
    ("coverage_manifest", "run_init_inline", {"slug": "sample", "template": None}),
])
def test_configuration_link_is_rejected_before_parse(workspace_root, tmp_path, monkeypatch, owner_name, seam, arguments):
    module = owner(owner_name)
    external = tmp_path / "configuration"
    external.write_text("outside-marker: value\n")
    path = workspace_root / "research.yml"
    path.unlink()
    path.symlink_to(external)
    monkeypatch.setattr(module, "load_config", lambda *args: pytest.fail("linked configuration was parsed"))
    with pytest.raises(Exception) as error:
        getattr(module, seam)(workspace_root, **arguments)
    assert error.value.error_code == "WORKSPACE_PATH_REFUSED"
    assert "outside-marker" not in str(error.value)


@pytest.mark.parametrize("alias", ["normalized", "structured", "provenance", "candidate", "jurisdiction", "other_coverage"])
def test_checked_evaluation_guards_every_policy_reader_input(workspace_root, tmp_path, alias):
    outside = tmp_path / "outside-input"
    outside.write_bytes(b"outside-marker")
    with Workspace.open(workspace_root) as workspace:
        workspace.coverage.init("sample", template=template())
        requests = SourceRequestsNamespace(workspace)
        requests.add(kind="other", query_or_identifier="Observed", rationale="Gap", question_slugs=["sample"])
        paths = {
            "normalized": workspace_root / "sources/normalized/unselected.md",
            "structured": workspace_root / "sources/normalized/sample-source.structured.json",
            "provenance": workspace_root / "raw/record.txt.provenance.yml",
            "candidate": workspace_root / "sources/discovery/candidates.jsonl",
            "jurisdiction": workspace_root / "sources/custom-jurisdictions.yml",
            "other_coverage": workspace_root / "sources/coverage/unselected.yml",
        }
        if alias == "provenance":
            record = {"id": "sample-source", "raw_paths": ["raw/record.txt"]}
            (workspace_root / "sources/manifest.jsonl").write_text(json.dumps(record) + "\n")
        if alias == "jurisdiction":
            config = yaml.safe_load((workspace_root / "research.yml").read_text())
            config["integrations"] = {"discovery": {"jurisdictions_path": "sources/custom-jurisdictions.yml"}}
            (workspace_root / "research.yml").write_text(yaml.safe_dump(config))
        path = paths[alias]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(outside)
        before = (workspace_root / "sources/coverage/sample.yml").read_bytes()
        with pytest.raises(EvidenceWikiError) as error:
            workspace.coverage.evaluate("sample")
        assert error.value.error_code == "WORKSPACE_PATH_REFUSED"
        assert (workspace_root / "sources/coverage/sample.yml").read_bytes() == before
        assert outside.read_bytes() == b"outside-marker"


def test_fulfillment_checks_referenced_question_and_selected_normalized_leaves(workspace_root, tmp_path):
    with Workspace.open(workspace_root) as workspace:
        requests = SourceRequestsNamespace(workspace)
        added = requests.add(kind="other", query_or_identifier="Observed", rationale="Gap", question_slugs=["sample"])
        outside = tmp_path / "outside-record"
        outside.write_bytes(b"outside-marker")
        normalized = workspace_root / "sources/normalized/sample-source.md"
        normalized.parent.mkdir()
        normalized.symlink_to(outside)
        before = (workspace_root / "sources/source-requests.jsonl").read_bytes()
        with pytest.raises(EvidenceWikiError) as error:
            requests.fulfill(request_id=added["request"]["request_id"], source_id="sample-source")
        assert error.value.error_code == "WORKSPACE_PATH_REFUSED"
        normalized.unlink()
        question = workspace_root / "wiki/questions/sample.md"
        question.unlink()
        question.symlink_to(outside)
        with pytest.raises(EvidenceWikiError) as error:
            requests.fulfill(request_id=added["request"]["request_id"], source_id="sample-source")
        assert error.value.error_code == "WORKSPACE_PATH_REFUSED"
        assert (workspace_root / "sources/source-requests.jsonl").read_bytes() == before
