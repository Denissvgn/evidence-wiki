"""Current complete dependency inputs remain separate from native review identity."""

import copy
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from evidence_wiki import execution_session as driver
from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.execution_continuations import claim_sources
from evidence_wiki.execution_disagreement import material_basis
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.review_context import create_review_context
from evidence_wiki.review_delivery import ReviewDelivery
from tests.test_complete_review_delivery import complete_case as _complete_case_fixture
from tests.test_review_context import inputs
from tools import _review_dependency_workload as workload

complete_case = _complete_case_fixture


@pytest.fixture
def complete_inputs():
    values = inputs(1)
    subject = values["owner_snapshot"]["claim"]
    for name in ("generator", "desired_outcome"):
        subject.pop(name)
    subject.update(qualification="inference", premises=["middle"], derivation="Derived from middle.")
    middle = copy.deepcopy(subject)
    middle.update(id="middle", premises=["leaf"], derivation="Derived from leaf.")
    leaf = copy.deepcopy(subject)
    leaf.update(id="leaf", question_slug="other-question", qualification="supported", premises=[], derivation=None)
    values["owner_snapshot"]["premises"] = [middle]
    material = {"schema_version": "research-complete-review-material/v1", "basis_id": values["basis_id"],
        "native_snapshot_sha256": digest(values["owner_snapshot"]), "claim_id": subject["id"],
        "claims": [subject, middle, leaf], "questions": [
            {**values["owner_snapshot"]["question"], "metadata": {"scope": "capacity"}, "body": "Compare retained evidence."},
            {"slug": "other-question", "original_id": "other-original", "question": "What was retained?",
                "metadata": {"scope": "demand"}, "body": "Retained demand question context."}]}
    return values, material


def session(version="complete"):
    return SimpleNamespace(root="native-root", execution=SimpleNamespace(profile=SimpleNamespace(
        review_delivery=ReviewDelivery(context_version=version))))


def prepared(values):
    return {"snapshot": values["owner_snapshot"], "rubric": values["rubric"],
        "result": {"basis_id": values["basis_id"]}}


def test_current_check_reads_actual_material_and_actual_global_basis(monkeypatch, complete_inputs):
    values, material = complete_inputs
    context = create_review_context(**values, complete_material=material)
    reads = []
    current = copy.deepcopy(material)

    def read(root, native):
        reads.append((root, copy.deepcopy(native)))
        return current

    monkeypatch.setattr("evidence_wiki.review_material.current_review_material", read)
    native = prepared(values)
    driver._check_review_context(session(), context, native, values["sources"])
    assert reads == [("native-root", native)]
    current["questions"][1]["body"] += " Changed."
    with pytest.raises(ExecutionError, match="owner_changed"):
        driver._check_review_context(session(), context, native, values["sources"])
    current["questions"][1]["body"] = material["questions"][1]["body"]
    changed = copy.deepcopy(native)
    changed["result"]["basis_id"] = "sha256:" + "d" * 64
    with pytest.raises(ExecutionError, match="owner_changed"):
        driver._check_review_context(session(), context, changed, values["sources"])
    assert values["owner_snapshot"] == native["snapshot"]
    assert context.binding()["owner_snapshot_sha256"] == digest(native["snapshot"])
    assert context.binding()["complete_material_sha256"] == digest(material)


def test_complete_profile_refuses_legacy_context_without_fallback(monkeypatch, complete_inputs):
    values, material = complete_inputs
    context = create_review_context(**values)
    assert not context.document()["manifest"]["context_complete"]
    monkeypatch.setattr("evidence_wiki.review_material.current_review_material", lambda *_: material)
    with pytest.raises(ExecutionError, match="version_unsupported"):
        driver._check_review_context(session(), context, prepared(values), values["sources"])
    complete = create_review_context(**values, complete_material=material)
    with pytest.raises(ExecutionError, match="version_unsupported"):
        driver._check_review_context(session("legacy"), complete, prepared(values), values["sources"])


def test_legacy_check_never_promotes_or_fetches_complete_material(monkeypatch):
    values = inputs(1)
    context = create_review_context(**values)

    def unexpected(*_):
        pytest.fail("Legacy context requested complete material.")

    monkeypatch.setattr("evidence_wiki.review_material.current_review_material", unexpected)
    driver._check_review_context(session("legacy"), context, prepared(values), values["sources"])
    assert context.document()["schema_version"] == "research-review-context/v1"


def test_closure_source_selection_and_disagreement_hash_use_all_question_context(complete_inputs):
    values, material = complete_inputs
    native = prepared(values)
    assert claim_sources(native, values["sources"], complete_material=material) == values["sources"]
    with pytest.raises(ExecutionError, match="premise_graph_invalid"):
        material_basis(native["snapshot"], native["rubric"], values["sources"])
    basis = material_basis(native["snapshot"], native["rubric"], values["sources"], complete_material=material)
    changed = copy.deepcopy(material)
    changed["questions"][1]["body"] += " Changed."
    assert material_basis(native["snapshot"], native["rubric"], values["sources"], complete_material=changed) != basis
    assert material["native_snapshot_sha256"] == digest(native["snapshot"])


def test_packet_preflight_consumes_all_real_codec_pages_without_review_credit(monkeypatch, complete_case):
    from evidence_wiki.packet_pages import create
    from evidence_wiki.review_packet_sections import create as create_sections

    values, context, diagnostics, instructions = complete_case
    visible, _binding = create_sections(context, values["sources"], diagnostics, counter_instruction=instructions)
    selected = []
    consumed = []
    closed = []
    checked = []
    host = SimpleNamespace(require_grant=lambda _: None)

    class Handle:
        def __init__(self, pages, label):
            self.pages, self.label = pages, label
            self._context = host
            self._pages = self

        def _check_snapshot(self, *current):
            checked.append((self.label, current))

        def header(self):
            return self.pages.header()

        def page(self, identifier):
            consumed.append((self.label, identifier))
            return self.pages.page(identifier)

        def completeness(self, pages):
            return self.pages.completeness(pages)

    @contextmanager
    def prepare(root, **kwargs):
        selected.append((root, kwargs))
        yield Handle(create(context, values["sources"], page_chars=256, sections=visible), kwargs["context_id"]), ()
        closed.append(kwargs["context_id"])

    monkeypatch.setattr("evidence_wiki.review_page_operations._prepared_snapshot", prepare)
    monkeypatch.setattr("evidence_wiki.review_page_operations.current_context", lambda: host)
    originals = []
    workload.review_packet_preflight("native-root", SimpleNamespace(review_delivery=ReviewDelivery(context_version="complete")),
        [values["sources"][0]["source_id"]], originals)
    assert len(selected) == len(workload.CLAIM_IDS) * 2 == len(originals)
    assert {(kwargs["claim_id"], kwargs["task_role"]) for _, kwargs in selected} == {
        (claim, role) for claim in workload.CLAIM_IDS for role in ("evaluator", "critic")}
    assert len(consumed) == sum(len(value["packet"]["header"]["pages"]) for _, value in originals)
    assert len(closed) == len(checked) == len(originals)
    assert all(kwargs["_closing_time"] for _, kwargs in selected)
    for _, value in originals:
        assert value["context_version"] == "complete"
        assert value["packet"]["schema_version"] == "research-review-packet/v2"
        assert value["packet"]["complete"]["complete"]
        assert value["packet_sha256"] == workload.digest(value["packet"])
        assert value["review_credit"] == value["approval_credit"] == 0


def test_packet_preflight_never_retains_a_packet_when_closing_scope_refuses(monkeypatch, complete_case):
    from evidence_wiki.errors import UsageError
    from evidence_wiki.packet_pages import create
    from evidence_wiki.review_packet_sections import create as create_sections

    values, context, diagnostics, instructions = complete_case
    visible, _binding = create_sections(context, values["sources"], diagnostics, counter_instruction=instructions)
    pages = create(context, values["sources"], page_chars=256, sections=visible)
    host = SimpleNamespace(require_grant=lambda _: None)
    observed = []

    @contextmanager
    def prepare(*_args, **_kwargs):
        yield SimpleNamespace(_context=host, _pages=pages, _check_snapshot=lambda *_: observed.append("checked")), ()
        observed.append("closing")
        raise UsageError("REVIEW_PAGE_REFUSED", "The closing owner changed.", details={"reason": "current_owner_changed"})

    monkeypatch.setattr("evidence_wiki.review_page_operations._prepared_snapshot", prepare)
    monkeypatch.setattr("evidence_wiki.review_page_operations.current_context", lambda: host)
    originals = []
    with pytest.raises(UsageError):
        workload.review_packet_preflight("native-root", SimpleNamespace(review_delivery=ReviewDelivery(context_version="complete")),
            [values["sources"][0]["source_id"]], originals)
    assert observed == ["checked", "closing"]
    assert len(originals) == 1 and originals[0][0].endswith("-failure")
    assert "packet" not in originals[0][1]


@pytest.mark.parametrize("reason", ["counter_instruction_claim_changed", "decisive_context_incomplete", "current_owner_changed",
    "host_selection_changed", "complete_packet_requires_supported_paging_executor", "packet_bound"])
def test_packet_failure_telemetry_is_code_specific(reason):
    from evidence_wiki.errors import ConfigError

    error = ConfigError("REVIEW_PAGE_REFUSED", "Private content.", details={"reason": reason})
    assert workload.failure_document(error)["reason"] == reason
    other = ConfigError("STRICT_EVIDENCE_REFUSED", "Private content.", details={"reason": reason})
    assert workload.failure_document(other)["reason"] is None
    private = ConfigError("REVIEW_PAGE_REFUSED", "private_credential", details={"reason": "private_credential"})
    assert workload.failure_document(private)["reason"] is None


def test_repaired_basis_uses_one_fresh_complete_closure_for_each_observation(monkeypatch, complete_inputs):
    values, material = complete_inputs
    native = prepared(values)
    observed = []
    current = copy.deepcopy(material)

    def read(root, owner):
        observed.append((root, owner))
        return current

    monkeypatch.setattr("evidence_wiki.review_material.current_review_material", read)
    first = driver._current_disagreement_basis(session(), native, values["sources"])
    assert observed == [("native-root", native)]
    assert first == material_basis(native["snapshot"], native["rubric"], values["sources"], complete_material=current)
    current["questions"][1]["body"] += " Newly retained conditions."
    second = driver._current_disagreement_basis(session(), native, values["sources"])
    assert len(observed) == 2
    assert second != first
    assert native["snapshot"]["premises"][0]["premises"] == ["leaf"]
    assert digest(native["snapshot"]) == material["native_snapshot_sha256"]


@pytest.mark.parametrize("purpose", ["adjudicate", "semantic_adjudicate", "counterevidence"])
def test_complete_unsupported_investigation_refuses_before_worker_selection(monkeypatch, purpose):
    from evidence_wiki.workflow_contracts import WorkflowContractError

    selected = session()
    selected.policy = SimpleNamespace(to_dict=lambda: {
        "allowed_effects": ["model"], "host_capability_refs": {"model": "host-model"}})
    selected.context = SimpleNamespace(require_grant=lambda _: None)
    monkeypatch.setattr(driver, "require_machine_compatible", lambda _: None)

    def unexpected(*_):
        pytest.fail("Unsupported complete investigation reached worker selection.")

    monkeypatch.setattr(driver, "_worker_config", unexpected)
    with pytest.raises(WorkflowContractError) as failure:
        driver.reserve(selected, "investigation", {"operation": "adjudicate", "purpose": purpose}, {})
    assert failure.value.error_code == "RESEARCH_REVIEW_CONTEXT_VERSION_UNSUPPORTED"


def test_legacy_adjudication_reaches_existing_worker_selection(monkeypatch):
    selected = session("legacy")
    selected.execution.profile.disagreement = None
    selected.policy = SimpleNamespace(to_dict=lambda: {
        "allowed_effects": ["model"], "host_capability_refs": {"model": "host-model"}})
    selected.context = SimpleNamespace(require_grant=lambda _: None)
    monkeypatch.setattr(driver, "require_machine_compatible", lambda _: None)

    class WorkerSelectionReached(Exception):
        pass

    def select(*_):
        raise WorkerSelectionReached

    monkeypatch.setattr(driver, "_worker_config", select)
    with pytest.raises(WorkerSelectionReached):
        driver.reserve(selected, "investigation", {"operation": "adjudicate", "purpose": "adjudicate"}, {})
