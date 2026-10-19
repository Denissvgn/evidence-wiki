"""Validation helper reuse retains script identity and fresh semantic values."""

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_strict_evidence import CORE

SCRIPTS = Path(__file__).resolve().parents[1] / "workspace-template/scripts"


def test_validation_loader_cannot_be_selected_for_pdf_extraction(tmp_path, monkeypatch):
    capture = CORE.sibling("_host_capture_normalization")
    normalizer = SimpleNamespace(_bounded_bytes_available=lambda: False)
    inspected = SimpleNamespace(inspect_record=lambda *_args, **_kwargs: {
        "profile": {"content_format": "pdf"}})
    monkeypatch.setattr(capture, "_validation_sibling", lambda name: {
        "normalize_sources": normalizer, "_host_capture": inspected}[name])
    with pytest.raises(ValueError, match="capture_read_only_pdf_extraction_unavailable"):
        capture.normalize(tmp_path, {}, _read_only=True)


def test_validation_helpers_reuse_only_matching_root_and_generation(tmp_path):
    loader = CORE.sibling("_workspace_module_loader")
    owners = []
    for label in ("first", "second"):
        root = tmp_path / label
        root.mkdir()
        for name in ("_host_capture_normalization.py", "_workspace_module_loader.py"):
            shutil.copyfile(SCRIPTS / name, root / name)
        (root / "normalize_sources.py").write_text(f"VALUE = {label!r}\nPDF_LAYOUT_TEXT_CACHE = {{}}\n")
        owner = loader.load_workspace_module(root, "_host_capture_normalization")
        owners.append(owner)
        first = owner._validation_sibling("normalize_sources")
        assert owner._validation_sibling("normalize_sources") is first
        assert first.VALUE == label
        assert owner.sibling("normalize_sources") is not first
    assert owners[0]._validation_sibling("normalize_sources") is not owners[1]._validation_sibling("normalize_sources")
    first = owners[0]._validation_sibling("normalize_sources")
    (tmp_path / "first/normalize_sources.py").write_text("VALUE = 'changed'\nPDF_LAYOUT_TEXT_CACHE = {}\n")
    refreshed = owners[0]._validation_sibling("normalize_sources")
    assert refreshed is not first and refreshed.VALUE == "changed"
    assert len(owners[0]._VALIDATION_MODULES) == 1


def test_current_snapshot_detaches_actual_projection_result_and_refreshes(tmp_path, monkeypatch):
    questions = CORE.sibling("_question_dependencies")
    actual = {"result_id": "first", "values": [1, {"value": 2.5}]}
    preimages = []

    def project(*args, **kwargs):
        preimage = {"producer_id": "producer", "claims": [{"question_slug": "question"}],
            "local_context": {"mechanical_reasons": [{"claim_id": "claim", "reasons": []}]},
            "global_material": {"material": {"worker_semantics": {
                "computation": {"result": questions.typed_node(actual)}}}}}
        preimages.append(preimage)
        return preimage

    owners = {"_review_dependencies": SimpleNamespace(project=project, dependency_items=lambda value: [],
        finish=lambda preimage, basis: preimage), "_review_dependency_contract": SimpleNamespace(
        basis=lambda *args, **kwargs: {"basis_id": "basis"}), "_question_dependencies": questions}
    monkeypatch.setattr(CORE, "sibling", owners.__getitem__)
    monkeypatch.setattr(CORE, "_dependency_inputs", lambda *args: {})
    monkeypatch.setattr(CORE, "_dependency_role", lambda *args: "evaluator")
    monkeypatch.setattr(CORE, "_dependency_snapshot", lambda *args: {"computation": args[-1]})
    policy = {"schema_version": CORE.POLICY_SCHEMA_V2}
    current = CORE.dependency_current(tmp_path, {"computation": {}}, policy, None, None, None, "claim")
    assert current["snapshot"]["computation"] == actual
    current["snapshot"]["computation"]["values"][1]["value"] = 99
    assert questions.typed_value(preimages[0]["global_material"]["material"]["worker_semantics"]["computation"]["result"]) == actual
    actual["result_id"] = "second"
    refreshed = CORE.dependency_current(tmp_path, {"computation": {}}, policy, None, None, None, "claim")
    assert refreshed["snapshot"]["computation"] == actual
    assert len(preimages) == 2
