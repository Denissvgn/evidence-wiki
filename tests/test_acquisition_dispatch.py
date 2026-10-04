"""Real bound run journal and native transport fixtures preserve failed attempts."""

import json

import pytest
import yaml

from evidence_wiki import RunPolicy, Workspace
from evidence_wiki import acquisition_dispatch as dispatch
from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_contracts import canonical, digest
from evidence_wiki.pack_discovery import owner
from tests.test_acquisition_transport import FakeResponse
from tests.test_execution_session import fixture as fixture


def selected(fixture):
    root, policy, execution, context = fixture
    from evidence_wiki.planning_contracts import normalize
    from evidence_wiki.requirements_projection import BASE
    from tests.test_research_planning import question_plan, request
    legacy = json.loads((root / "docs/research-requirements.json").read_bytes())
    original = legacy["request"]["payload"]["questions"][0]
    declared = request(root.parent)
    declared["request"]["payload"]["authority"]["source_scope"] = [str(root / "raw"),"https://example.org/source"]
    declared["request"]["payload"]["authority"]["allowed_actions"].append("acquisition")
    declared["request"]["payload"]["budgets"].update(bytes=256,downloads=2,source_requests=2)
    declared["request"]["payload"]["questions"] = [original]
    declared["request"]["payload"]["derived_questions"] = []
    declared["request"]["payload"]["scope"] = []
    plan = question_plan(original["id"])
    plan["criteria"][0]["required_scope"] = []
    coverage = yaml.safe_load((root / "sources/coverage/q.yml").read_bytes())
    plan["facets"] = [{key: facet[key] for key in ("facet_id", "description", "evidence_path", "source_policy",
        "freshness_policy", "identity_policy", "min_sources")} | {"required": True} for facet in coverage["required_facets"]]
    declared["decisions"]["question_plans"] = [plan]
    normalized, _ = normalize(declared)
    (root / "docs/research-requirements.json").write_bytes(canonical({**normalized, "schema_version": BASE})+b"\n")
    page = root / "wiki/questions/q.md"
    text = page.read_text()
    _blank, front, body = text.split("---", 2)
    metadata = yaml.safe_load(front)
    metadata["metadata"] = {"research_id": original["id"], "original_text": original["text"],
        "original_ids_json": json.dumps([original["id"]])}
    page.write_text("---\n" + yaml.safe_dump(metadata) + "---" + body)
    config = yaml.safe_load((root / "research.yml").read_bytes())
    config.pop("evidence_usage", None)
    config.setdefault("integrations", {})["acquisition"] = {"enabled": True, "providers": ["web"],
        "target_root": "raw/web", "max_downloads_per_run": 1, "require_license_check": False,
        "web": {"allowed_domains": ["example.org"], "max_download_bytes": 128, "target_root": "raw/web"}}
    (root / "research.yml").write_text(yaml.safe_dump(config))
    from tests.test_draft_admission import update_policy
    update_policy(root)
    frozen = policy.to_dict()
    frozen["policy_id"] = None
    frozen["allowed_effects"] = sorted(set(frozen["allowed_effects"]) | {"acquire"})
    frozen["host_capability_refs"]["acquire"] = "native-web"
    frozen["continuation_limits"]["acquisition_attempts"] = 2
    grants = context.grants | {"acquisition_operations.dispatch.web", "research_session.acquire", "research_session.prepare_acquisition", "acquisition_operations.prepare", "request_lifecycle_operations.current",
        "source_requests.run_add_request", "source_requests.run_list_requests"}
    selected_context = HostContext([root], grants, context.actor_ref, [root], authority_file=context.authority_file,
        state_directory=None, mutation_control_directory=context.mutation_control_directory,
        controller_refs=context.controller_refs, role_controller_refs=context.role_controller_refs,
        credential_refs=context.credential_refs)
    return root, RunPolicy(frozen), execution, selected_context


def opener(transport, content, calls, root, control):
    def execute(plan, **values):
        from evidence_wiki.mutation_store import MutationStore
        locks = owner("_workspace_locks")
        with MutationStore(root, control).workspace_locked(), locks.workspace_lock(
                owner("fetch_sources").acquisition_workspace_lock_path(root), timeout_seconds=0):
            calls.append(plan.url)
        response = FakeResponse(content, url=plan.url, headers={"Content-Type": "text/plain"})
        return transport(plan, **values,
            opener=lambda *_: response, resolve_hostnames=False)
    return execute


def test_actual_bound_dispatch_outside_gates_and_historical_replay_no_second_fetch(fixture, monkeypatch):
    root, policy, execution, context = selected(fixture)
    transport = owner("_acquisition_transport")
    calls = []
    actual = transport.execute_planned_request
    with context.activate(), Workspace.open(root) as workspace:
        source = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/source",
            rationale="Need current original", question_slugs=["q"], scope={"facet_id": "primary"})["request"]
        with workspace.research.session(policy, run_id="acquire", execution=execution) as session:
            with dispatch.native.gate(session):
                dispatch.native._begin(session)
            with monkeypatch.context() as fixture_transport:
                fixture_transport.setattr(transport, "execute_planned_request", opener(actual, b"Scoped retained original", calls,
                    root, context.mutation_control_directory))
                result = session.acquire(request_id="fetch", source_request_id=source["request_id"], expected_request_basis=digest(source))
                repeated = session.acquire(request_id="fetch", source_request_id=source["request_id"], expected_request_basis=digest(source))
            assert result == repeated and len(calls) == 1
            assert result["byte_count"] == len(b"Scoped retained original") and not result["source_accepted"]
            ledger = owner("_provider_accounting").load_events(root / "runs/acquire", ledger_filename=owner("fetch_sources").REGISTERED_LEDGER_FILENAME,
                schema_version=owner("fetch_sources").ACCOUNTING_SCHEMA_VERSION)
            assert len(ledger) == 1


@pytest.mark.parametrize("kind",["scope","bytes","downloads"])
def test_frozen_original_limits_refuse_before_opener(fixture,monkeypatch,kind):
    from evidence_wiki.errors import EvidenceWikiError
    root,policy,execution,context=selected(fixture)
    path=root/"docs/research-requirements.json"
    frozen=json.loads(path.read_bytes())
    if kind=="scope":frozen["request"]["payload"]["authority"]["source_scope"]=[str(root/"raw")]
    else:frozen["request"]["payload"]["budgets"][kind]=0
    path.write_bytes(canonical(frozen)+b"\n")
    from tests.test_draft_admission import update_policy
    update_policy(root)
    transport=owner("_acquisition_transport")
    monkeypatch.setattr(transport,"execute_planned_request",lambda *_args,**_kwargs:pytest.fail("Unpermitted transport executed"))
    with context.activate(),Workspace.open(root) as workspace:
        source=workspace.source_requests.add(kind="web",query_or_identifier="https://example.org/source",
            rationale="Need current original",question_slugs=["q"],scope={"facet_id":"primary"})["request"]
        with workspace.research.session(policy,run_id="refused",execution=execution) as session:
            with dispatch.native.gate(session):dispatch.native._begin(session)
            with pytest.raises(EvidenceWikiError) as caught:
                dispatch.dispatch(session,request_id="fetch",source_request_id=source["request_id"],expected_request_basis=digest(source))
            assert caught.value.details["reason"] in {"original_acquisition_scope_ungranted","frozen_acquisition_budget_exhausted"}
            records=execution.records(session)
            assert not any(row["kind"]=="execution_acquisition_reservation" for row in records)


def test_failed_transport_spends_reservation_and_same_request_never_refetches(fixture,monkeypatch):
    from evidence_wiki.errors import EvidenceWikiError
    root,policy,execution,context=selected(fixture)
    transport=owner("_acquisition_transport")
    calls=[]
    def failure(*_args,**_kwargs):
        calls.append(True)
        raise RuntimeError("Unobserved transport failure")
    monkeypatch.setattr(transport,"execute_planned_request",failure)
    with context.activate(),Workspace.open(root) as workspace:
        source=workspace.source_requests.add(kind="web",query_or_identifier="https://example.org/source",
            rationale="Need current original",question_slugs=["q"],scope={"facet_id":"primary"})["request"]
        with workspace.research.session(policy,run_id="failed-transport",execution=execution) as session:
            with dispatch.native.gate(session):dispatch.native._begin(session)
            values={"request_id":"fetch","source_request_id":source["request_id"],"expected_request_basis":digest(source)}
            with pytest.raises(RuntimeError):dispatch.dispatch(session,**values)
            with pytest.raises(EvidenceWikiError):dispatch.dispatch(session,**values)
            assert calls==[True]
            with pytest.raises(EvidenceWikiError):dispatch.dispatch(session,**{**values,"request_id":"other"})
            assert calls==[True]
            rows=[row for row in execution.records(session) if row["kind"]=="execution_acquisition_reservation"]
            assert len(rows)==1 and rows[0]["data"]["max_bytes"]==128


def test_bound_preparation_reports_remaining_without_reserving_or_starting_run(fixture):
    root,policy,execution,context=selected(fixture)
    from evidence_wiki.acquisition_contracts import schema_document
    from evidence_wiki.onboarding_contract import _matches
    with context.activate(),Workspace.open(root) as workspace:
        source=workspace.source_requests.add(kind="web",query_or_identifier="https://example.org/source",
            rationale="Need current original",question_slugs=["q"],scope={"facet_id":"primary"})["request"]
        with workspace.research.session(policy,run_id="prepared-only",execution=execution) as session:
            before=owner("_evidence_revision").capture_workspace(root).revision_id
            result=session.prepare_acquisition(request_id=source["request_id"])
            _matches(result,schema_document("evidence-acquisition-preparation/v1"))
            assert result["attempts"]["remaining"]>0 and result["execution_authority"]=="not_established"
            assert result["budget_status"]=="selected_run_observed_reservation_required"
            assert owner("_evidence_revision").capture_workspace(root).revision_id==before
            assert not owner("run_controller").run_state_path(root,"prepared-only").exists()
