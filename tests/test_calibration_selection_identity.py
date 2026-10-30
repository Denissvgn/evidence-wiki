"""Exact reference declarations never cache host observations or authority."""

import copy
import hashlib
from dataclasses import FrozenInstanceError, replace

import pytest

from evidence_wiki import calibration_registry as owner
from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.errors import UsageError
from evidence_wiki.mutation_contracts import canonical
from evidence_wiki.qualification_calibration import calibration_suite
from evidence_wiki.reference_oracles import DOMAIN, protected_cases


@pytest.fixture(scope="module")
def suite():
    return calibration_suite(protected_cases(), suite_ref="exact-references", split_ref="held-out-finite",
        domains=[DOMAIN], rubric={"id": "finite", "revision": "1"}, model_settings={"temperature": "0"})


@pytest.fixture
def cold_cache():
    owner._suite_assessment.cache_clear()
    yield
    owner._suite_assessment.cache_clear()


def test_shared_assessment_retains_all_references_and_detached_views(suite, cold_cache, monkeypatch):
    calls = []
    assess = owner.assess_references

    def observed(cases):
        calls.append(len(cases))
        return assess(cases)

    monkeypatch.setattr(owner, "assess_references", observed)
    selections = [owner.CalibrationSelection(name, copy.deepcopy(suite), model_revision="revision")
        for name in ("evidence", "scope", "critic", "adjudicator")]
    assert calls == [600]
    for selection in selections:
        document = selection.document()
        assert len(document["reference_assessment"]["results"]) == 600
        assert document["reference_assessment"]["baseline_materials"] == 100
        assert selection.sha256 == hashlib.sha256(canonical(document)).hexdigest()
        document["reference_assessment"]["results"].clear()
        assert len(selection.document()["reference_assessment"]["results"]) == 600
    assert all(type(raw) is bytes for raw in (selection._document_bytes for selection in selections))
    with pytest.raises(FrozenInstanceError):
        selections[0].config_ref = "changed"


def test_changed_case_cannot_reuse_original_assessment(suite, cold_cache):
    owner.CalibrationSelection("first", suite, model_revision="revision")
    changed = copy.deepcopy(suite)
    for case in changed["cases"]:
        if case["unit_ref"] == changed["cases"][0]["unit_ref"]:
            case["expected_label"] = "reject"
    with pytest.raises(ExecutionError, match="suite_changed"):
        owner.CalibrationSelection("second", changed, model_revision="revision")
    changed = calibration_suite(changed["cases"], **{key: changed[key] for key in
        ("suite_ref", "split_ref", "domains", "rubric", "model_settings", "drift_ref")})
    with pytest.raises(ValueError, match="reference_label_not_reproduced"):
        owner.CalibrationSelection("second", changed, model_revision="revision")


def test_current_program_binding_prevents_reusing_assessment(suite, cold_cache, monkeypatch):
    owner.CalibrationSelection("first", suite, model_revision="revision")
    previous = owner.program_identity()
    monkeypatch.setattr(owner, "program_identity", lambda: previous | {"source_sha256": "0" * 64})
    with pytest.raises(ExecutionError, match="reference_program_changed"):
        owner.CalibrationSelection("second", suite, model_revision="revision")
    assert owner._suite_assessment.cache_info().misses == 2


def test_changed_metadata_preserves_exact_new_document(suite, cold_cache):
    first = owner.CalibrationSelection("first", suite, model_revision="revision")
    changed = calibration_suite(suite["cases"], **({key: suite[key] for key in
        ("suite_ref", "split_ref", "domains", "rubric", "model_settings", "drift_ref")} | {"split_ref": "new-hold-out"}))
    second = owner.CalibrationSelection("second", changed, model_revision="revision")
    assert first.document()["split_ref"] != second.document()["split_ref"]
    assert first.document()["suite_sha256"] != second.document()["suite_sha256"]
    assert len(second.document()["reference_assessment"]["results"]) == 600
    assert owner._suite_assessment.cache_info().maxsize == 4


@pytest.mark.parametrize("invalid", [float("nan"), {1: "non-text-key"}, {"payload": "x" * 1_048_577}])
def test_malformed_or_unbounded_input_cannot_reach_assessment_cache(suite, cold_cache, invalid):
    owner.CalibrationSelection("first", suite, model_revision="revision")
    before = owner._suite_assessment.cache_info()
    with pytest.raises(UsageError):
        owner.CalibrationSelection("second", invalid, model_revision="revision")
    assert owner._suite_assessment.cache_info() == before


@pytest.mark.parametrize("filename, field", [
    ("qualification_calibration.py", "scorer_source_sha256"),
    ("finite_domain.py", "classifier_source_sha256"),
])
def test_fresh_source_bytes_invalidate_reference_cache(suite, cold_cache, monkeypatch, filename, field):
    first = owner.CalibrationSelection("first", suite, model_revision="revision")
    original_read = owner.Path.read_bytes

    def changed_read(path):
        raw = original_read(path)
        return raw + b"\n# changed declaration\n" if path.name == filename else raw

    monkeypatch.setattr(owner.Path, "read_bytes", changed_read)
    second = owner.CalibrationSelection("second", suite, model_revision="revision")
    assert owner._suite_assessment.cache_info().misses == 2
    assert first.document()[field] != second.document()[field]
    assert len(second.document()["reference_assessment"]["results"]) == 600


def test_config_and_revision_are_separate_from_shared_reference_declarations(suite, cold_cache):
    selections = [owner.CalibrationSelection(config, suite, model_revision=revision)
        for config, revision in (("first", "revision"), ("second", "revision"), ("first", "new-revision"))]
    assert owner._suite_assessment.cache_info().misses == 1
    assert owner._suite_assessment.cache_info().hits == 2
    assert len({selection.sha256 for selection in selections}) == 3
    assert len({selection.document()["reference_assessment"]["assessment_ref"] for selection in selections}) == 1
    with pytest.raises(TypeError):
        replace(selections[0], config_ref="replacement")


def test_assessment_cache_evicts_exact_bounded_declarations(suite, cold_cache):
    for index in range(5):
        changed = calibration_suite(suite["cases"], **({key: suite[key] for key in
            ("suite_ref", "split_ref", "domains", "rubric", "model_settings", "drift_ref")}
            | {"split_ref": f"hold-out-{index}"}))
        owner.CalibrationSelection("first", changed, model_revision="revision")
    assert owner._suite_assessment.cache_info().currsize == 4
    assert owner._suite_assessment.cache_info().misses == 5
    first = calibration_suite(suite["cases"], **({key: suite[key] for key in
        ("suite_ref", "split_ref", "domains", "rubric", "model_settings", "drift_ref")}
        | {"split_ref": "hold-out-0"}))
    selection = owner.CalibrationSelection("first", first, model_revision="revision")
    assert owner._suite_assessment.cache_info().misses == 6
    assert len(selection.document()["reference_assessment"]["results"]) == 600
