"""Bounded native dependency closure and deterministic policy admission."""

import copy

import pytest

from evidence_wiki.pack_discovery import owner
from tests.test_question_dependencies import _manifest


def _chain(count, *, version="evidence-strict-claims/v1"):
    manifest = _manifest()
    source = manifest["claims"][0]
    claims = []
    for index in range(count):
        claim = copy.deepcopy(source)
        claim["id"] = "claim-" + str(index)
        if index:
            claim.update(qualification="inference", premises=["claim-" + str(index - 1)], derivation="Follows from the retained premise.")
        if version.endswith("/v2"):
            claim["calculations"] = []
        claims.append(claim)
    return {**manifest, "schema_version": version, "claims": claims}


def test_total_closure_bound_accepts_32_and_refuses_33_without_truncation():
    native = owner("_review_dependencies")
    selected = native.closure(_chain(32), "claim-31")
    assert [row["id"] for row in selected] == ["claim-" + str(index) for index in reversed(range(32))]
    with pytest.raises((ValueError, owner("_review_dependencies").sibling("_strict_evidence").ScriptRefusal)):
        native.closure(_chain(33), "claim-32")


def test_shared_transitive_premise_is_retained_exactly_once():
    native = owner("_review_dependencies")
    manifest = _chain(4)
    manifest["claims"][-1]["premises"] = ["claim-1", "claim-2"]
    assert [row["id"] for row in native.closure(manifest, "claim-3")] == ["claim-3", "claim-1", "claim-0", "claim-2"]


def test_explicit_claim_schema_preserves_required_empty_calculations():
    native = owner("_review_dependencies")
    manifest = _chain(2, version="evidence-strict-claims/v2")
    assert all(row["calculations"] == [] for row in native.closure(manifest, "claim-1"))
    del manifest["claims"][0]["calculations"]
    with pytest.raises((ValueError, owner("_review_dependencies").sibling("_strict_evidence").ScriptRefusal)):
        native.closure(manifest, "claim-1")
    legacy = _chain(1)
    legacy["claims"][0]["calculations"] = []
    with pytest.raises((ValueError, owner("_review_dependencies").sibling("_strict_evidence").ScriptRefusal)):
        native.closure(legacy, "claim-0")


@pytest.mark.parametrize("change", ["cycle", "missing"])
def test_invalid_premise_graph_refuses(change):
    manifest = _chain(2)
    manifest["claims"][-1]["premises"] = ["claim-1" if change == "cycle" else "absent"]
    with pytest.raises((ValueError, owner("_review_dependencies").sibling("_strict_evidence").ScriptRefusal)):
        owner("_review_dependencies").closure(manifest, "claim-1")


def _rule(primitive):
    return {"domain_pack": {"name": "sample", "policy_vocabularies": {"freshness_policy": {"pack:sample/check": "Check"}},
                            "policy_rules": {"pack:sample/check": {"all_of": [primitive]}}}}


def test_volatile_primitive_refuses_from_parsed_graph():
    with pytest.raises((ValueError, owner("_review_dependencies").sibling("_strict_evidence").ScriptRefusal)):
        owner("_review_dependencies")._deterministic_policy(_rule({"max_age": {"field": "provenance/retrieved_at", "hours": 48}}))


@pytest.mark.parametrize("field", ["status", "human_reviews", "grounding"])
def test_excluded_question_policy_reads_refuse_or_fail_native_validation(field):
    with pytest.raises((ValueError, owner("_review_dependencies").sibling("_strict_evidence").ScriptRefusal)):
        owner("_review_dependencies")._deterministic_policy(_rule({"equals": {"field": "record/value", "question_field": field}}))


def test_semantic_question_policy_read_and_no_rules_are_supported():
    native = owner("_review_dependencies")
    native._deterministic_policy({})
    native._deterministic_policy(_rule({"equals": {"field": "record/value", "question_field": "metadata/value"}}))


def test_retained_revision_bytes_have_new_safe_logical_coordinates():
    native = owner("_review_dependencies")
    revision = "sha256:" + "a" * 64
    assert native.revision_artifact_path(revision, "source-record.json") == "source-revisions/" + "a" * 64 + "/source-record.json"
    assert native.revision_artifact_path(revision, "evidence/caf\u00e9.txt") != native.revision_artifact_path(revision, "evidence/cafe\u0301.txt")
    for revision_id, path in [("sha256:bad", "record.json"), (revision, "../outside"), (revision, "/absolute")]:
        with pytest.raises((ValueError, owner("_review_dependencies").sibling("_strict_evidence").ScriptRefusal)):
            native.revision_artifact_path(revision_id, path)
    coordinate = native.revision_artifact_path(revision, "record.json")
    for existing in [coordinate, coordinate.upper(), "source-revisions", coordinate + "/child"]:
        with pytest.raises((ValueError, owner("_review_dependencies").sibling("_strict_evidence").ScriptRefusal)):
            native.revision_artifact_path(revision, "record.json", [existing])


def test_actual_hidden_workspace_bytes_are_retained_and_committed(tmp_path):
    import hashlib

    native = owner("_review_dependencies")
    revisions = owner("_evidence_revision")
    (tmp_path / ".gitignore").write_bytes(b"cache/\n")
    folder = tmp_path / ".obsidian"
    folder.mkdir()
    selected = folder / "workspace.json"
    selected.write_bytes(b'{"layout":"wide"}\n')
    capture = revisions.capture_workspace(tmp_path)
    assert set(capture.files) == {".gitignore", ".obsidian/workspace.json"}
    for path, raw in capture.files.items():
        committed = native._commit(path, raw, workspace=True)
        assert committed == {"path": path, "sha256": "sha256:" + hashlib.sha256(raw).hexdigest()}
    before = native._commit(".obsidian/workspace.json", capture.files[".obsidian/workspace.json"], workspace=True)
    selected.write_bytes(b'{"layout":"narrow"}\n')
    current = revisions.capture_workspace(tmp_path)
    after = native._commit(".obsidian/workspace.json", current.files[".obsidian/workspace.json"], workspace=True)
    assert before["path"] == after["path"] and before["sha256"] != after["sha256"]


@pytest.mark.parametrize("path", [".gitignore", ".obsidian/workspace.json", "notes/.settings/value.txt", "café/.config"])
def test_workspace_coordinates_allow_actual_hidden_components_without_normalizing_them(path):
    assert owner("_review_dependencies")._workspace_path(path) == path


@pytest.mark.parametrize("path", [
    None, 1, "", "/", "/absolute", "./file", "../file", "a/../b", "a//b", "a/./b", "a/",
    "a\\b", "a:b", "a<b", 'a"b', "a|b", "a?b", "a*b", "a.", "a ", ". ", "..", "a\x00b", "a\nb", "a\x7fb",
    "\ud800", "é" * 2049, "a/" * 65 + "file",
])
def test_invalid_workspace_coordinates_are_refused(path):
    native = owner("_review_dependencies")
    with pytest.raises((ValueError, native.sibling("_strict_evidence").ScriptRefusal)):
        native._workspace_path(path)


@pytest.mark.parametrize("path", [".gitignore", ".obsidian/workspace.json", "notes/.settings/value.txt"])
def test_hidden_workspace_support_does_not_relax_source_artifact_paths(path):
    native = owner("_review_dependencies")
    with pytest.raises((ValueError, native.sibling("_strict_evidence").ScriptRefusal)):
        native._commit(path, b"retained source bytes")
    with pytest.raises((ValueError, native.sibling("_strict_evidence").ScriptRefusal)):
        native.revision_artifact_path("sha256:" + "a" * 64, path)


def test_hidden_capture_casefold_collision_still_refuses_before_projection(tmp_path):
    from types import SimpleNamespace

    native = owner("_review_dependencies")
    capture = SimpleNamespace(files={".Settings/config": b"first", ".settings/config": b"second"})
    with pytest.raises(native.sibling("_strict_evidence").ScriptRefusal) as caught:
        native.project(tmp_path, "claim", config={}, policy={}, capture=capture, manifest={}, view=None)
    assert caught.value.details["reason"] == "selective_workspace_path_collision"


def test_capture_path_collection_keeps_native_file_bound(tmp_path):
    from types import SimpleNamespace

    native = owner("_review_dependencies")
    capture = SimpleNamespace(files={".hidden/" + str(index): b"" for index in range(native.MAX_FILES + 1)})
    with pytest.raises(native.sibling("_strict_evidence").ScriptRefusal) as caught:
        native.project(tmp_path, "claim", config={}, policy={}, capture=capture, manifest={}, view=None)
    assert caught.value.details["reason"] == "selective_workspace_path_bound"


@pytest.fixture
def workspace_projection(tmp_path, monkeypatch):
    """Hold authenticated owner inputs fixed while projecting captured material."""
    import hashlib
    import json
    from pathlib import Path
    from types import SimpleNamespace

    import yaml

    native = owner("_review_dependencies")
    contract = native.sibling("_review_dependency_contract")
    sample = json.loads((Path(__file__).parent / "fixtures/native-review-material.json").read_text())
    producer_document = sample["global_material"]["material"]["worker_semantics"]["producer"]
    producer_document["identity"]["manifest_id"] = contract.content_id(
        "evidence-review-producer-manifest/v1", producer_document["manifest"])
    producer = SimpleNamespace(document=lambda: copy.deepcopy(producer_document),
        producer_id=contract.content_id("evidence-review-producer/v1", producer_document["identity"]))
    counter = {**sample["local_context"]["counterevidence"], "state": "not_observed"}
    role = {**sample["global_material"]["material"]["worker_semantics"]["roles"][0], "output_contract": {}}
    inputs = SimpleNamespace(source_documents=lambda *_: [], counter_projection=lambda *_: copy.deepcopy(counter),
        role_semantics=lambda: [copy.deepcopy(role)], assert_current=lambda: None)
    config = {"wiki": {"root": "wiki"}}
    manifest = {"schema_version": sample["claim_schema_version"], "claims": sample["claims"], "questions": [
        {"slug": "question", "original_id": "question", "question": "Sample"},
        {"slug": "other", "original_id": "other", "question": "Other question?"}]}
    instruction = b"Assess the retained material.\n"
    policy = {"schema_version": "evidence-strict-policy/v2", "review_basis_mode": "claim_dependencies_v1",
        "policy_id": "sample", "revision": "1", "assurance": "artifact_checked", "claims_path": "claims.json",
        "instructions": {"instructions.md": "sha256:" + hashlib.sha256(instruction).hexdigest()},
        "rubric": {"id": "sample", "revision": "1", "criteria": dict.fromkeys(
            native.sibling("_strict_contract").CHECKS, "Assess the retained material.")},
        "human_review": False, "max_source_age_seconds": 3600, "max_review_age_seconds": 3600}
    capture = SimpleNamespace(files={"research.yml": yaml.safe_dump(config).encode(),
        "claims.json": json.dumps(manifest).encode(), "instructions.md": instruction})
    for question in manifest["questions"]:
        capture.files["wiki/questions/" + question["slug"] + ".md"] = (
            "---\n" + yaml.safe_dump({"type": "question", "status": "open", "question": question["question"]})
            + "---\nRetained question context.\n").encode()
    summaries = {row["slug"]: {"policy_results": []} for row in manifest["questions"]}
    qualification = next(row["value"] for row in sample["global_material"]["material"]["global_dependencies"]
        if row["kind"] == "qualification")
    monkeypatch.setattr(native, "_owner_inputs", lambda: (inputs, producer))
    monkeypatch.setattr(native, "_requirements", lambda *_: {})
    monkeypatch.setattr(native, "_coverage", lambda *_: ({}, summaries))
    inventory = next(row["value"]["sources"] for row in sample["global_material"]["material"]["global_dependencies"]
        if row["kind"] == "source_inventory")
    monkeypatch.setattr(native, "_source_material", lambda *_: (copy.deepcopy(inventory), [], {}))
    monkeypatch.setattr(native, "_mechanical", lambda *_: copy.deepcopy(sample["local_context"]["mechanical"]))
    monkeypatch.setattr(native, "_facts", lambda *_: copy.deepcopy(sample["local_context"]["mechanical_reasons"]))
    monkeypatch.setattr(native, "_qualification", lambda *_: copy.deepcopy(qualification))

    def project():
        material = native.project(tmp_path, "claim", config=config, policy=policy, capture=capture,
            manifest=manifest, view=SimpleNamespace(trust={"content_hash": "sha256:" + "1" * 64}))
        basis = contract.basis(material, native.dependency_items(material),
            required_review_role="evaluator", computation_mode="none")
        return material, basis["basis_id"]

    project.inputs = {"manifest": manifest, "config": config, "policy": policy, "capture": capture,
                      "tmp_path": tmp_path, "native": native}
    return capture.files, project


@pytest.mark.parametrize("path,raw", [
    ("wiki/questions/notes.md", b"---\ntype: note\n---\nScope conditions.\n"),
    ("wiki/questions/appendix/context.md", b"Nested reviewer context.\n"),
    ("wiki/questions/appendix/question.md", b"---\ntype: question\n---\nNested reviewer context.\n"),
])
def test_unprojected_question_directory_material_changes_global_and_claim_basis(workspace_projection, path, raw):
    import hashlib

    files, project = workspace_projection
    baseline, baseline_basis = project()
    files[path] = raw
    added, added_basis = project()
    assert added["global_material"]["dependency_id"] != baseline["global_material"]["dependency_id"]
    assert added_basis != baseline_basis
    dependencies = added["global_material"]["material"]["global_dependencies"]
    assert {"kind": "workspace_material", "coordinate": path,
        "value": {"path": path, "sha256": "sha256:" + hashlib.sha256(raw).hexdigest()}} in dependencies
    files[path] += b"Changed scope conditions.\n"
    edited, edited_basis = project()
    assert edited["global_material"]["dependency_id"] != added["global_material"]["dependency_id"]
    assert edited_basis != added_basis
    del files[path]
    deleted, deleted_basis = project()
    assert deleted == baseline
    assert deleted_basis == baseline_basis
    assert deleted_basis != edited_basis


def test_unrelated_declared_question_edit_preserves_claim_basis(workspace_projection):
    files, project = workspace_projection
    before, before_basis = project()
    files["wiki/questions/other.md"] += b"Additional unrelated question context.\n"
    after, after_basis = project()
    assert after == before
    assert after_basis == before_basis
    assert [row["path"] for row in after["questions"]] == ["wiki/questions/question.md"]
    files["wiki/questions/question.md"] += b"Additional target question context.\n"
    changed, changed_basis = project()
    assert changed["global_material"] == before["global_material"]
    assert changed_basis != before_basis


@pytest.mark.parametrize("materialized", [False, True])
def test_same_question_sibling_edit_preserves_target_basis(workspace_projection, materialized):
    import json

    import yaml

    files, project = workspace_projection
    held = project.inputs
    manifest = held["manifest"]
    sibling = {**copy.deepcopy(manifest["claims"][0]), "id": "sibling", "text": "An independent retained observation.",
               "premises": [], "evidence": _manifest()["claims"][0]["evidence"]}
    manifest["claims"].append(sibling)
    files["claims.json"] = json.dumps(manifest).encode()
    if materialized:
        questions = owner("_question_dependencies")
        row = questions.project(held["tmp_path"], held["config"], held["policy"], held["capture"], manifest,
                                "question", coverage_summary={"policy_results": []})
        front, body = questions.parse_page(files[row["path"]])
        front.update(row["canonical_mirrors"], status="answered", answered_by="worker")
        files[row["path"]] = ("---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n" + body).encode()
    before, original_basis = project()
    sibling["text"] = "The independent retained observation changed."
    files["claims.json"] = json.dumps(manifest).encode()
    after, current_basis = project()
    assert before == after
    assert original_basis == current_basis
    assert [row["id"] for row in after["claims"]] == ["claim"]
    assert after["questions"][0]["canonical_mirrors"]["grounding"] == []
    assert owner("_review_dependencies").dependency_items(before) == owner("_review_dependencies").dependency_items(after)
    manifest["claims"][0]["text"] = "The target observation changed."
    files["claims.json"] = json.dumps(manifest).encode()
    target_changed, target_basis = project()
    assert target_basis != current_basis
    assert target_changed["global_material"] == after["global_material"]


@pytest.mark.parametrize("retrieval", ["separate", "legacy_envelope", "paired"])
def test_original_custody_reconstructs_closure_local_question_rows(workspace_projection, monkeypatch, retrieval):
    import json
    from types import SimpleNamespace

    from evidence_wiki import host_context

    files, project = workspace_projection
    held = project.inputs
    manifest = held["manifest"]
    sibling = {**copy.deepcopy(manifest["claims"][0]), "id": "sibling", "text": "An independent retained observation.",
               "premises": [], "evidence": _manifest()["claims"][0]["evidence"]}
    manifest["claims"].append(sibling)
    files["claims.json"] = json.dumps(manifest).encode()
    coverage = {row["slug"]: {"manifest": {}, "summary": {"policy_results": []}} for row in manifest["questions"]}
    monkeypatch.setattr(held["native"], "_coverage", lambda *_: (coverage,
        {slug: row["summary"] for slug, row in coverage.items()}))
    original, original_basis = project()
    core = owner("_strict_evidence")
    snapshot = core._dependency_snapshot(held["tmp_path"], held["config"], held["policy"], held["capture"], manifest,
        original, {"selected": {}}, None)
    original_inputs = {path: files[path] for path in ["claims.json", *[row["path"] for row in original["questions"]]]}
    closed = []
    reads = []
    envelope = {"authenticated": "original"}
    proof = SimpleNamespace(original_inputs=lambda: original_inputs, original_snapshot=lambda: snapshot,
                            assert_current=lambda: closed.append(True))
    def compare(supplied):
        proof.assert_current()
        core.require(supplied is envelope and supplied == {"authenticated": "original"},
            "selective_original_envelope_changed")
        reads.append("envelope")
    proof.assert_review_envelope = compare
    if retrieval == "paired":
        def material(supplied):
            compare(supplied)
            return original_inputs, snapshot
        proof.original_material = material
        proof.original_inputs = proof.original_snapshot = lambda: pytest.fail("separate custody read")
    selected = {} if retrieval == "separate" else {"envelope": envelope}
    monkeypatch.setattr(host_context, "review_custody", lambda *_args, **_kwargs: proof)
    sibling["text"] = "The independent retained observation changed."
    files["claims.json"] = json.dumps(manifest).encode()
    current, current_basis = project()
    assert current_basis == original_basis
    payload = {"claim_id": "claim", "claim_schema_version": manifest["schema_version"], "snapshot": snapshot}
    reproduced = core._dependency_original(held["tmp_path"], held["config"], held["policy"], payload,
        {"material": current, "snapshot": snapshot}, purpose="reuse", **selected)
    assert reproduced is proof and closed == [True] * (1 if retrieval == "separate" else 2)
    assert reads == ([] if retrieval == "separate" else ["envelope", "envelope"])
    original_inputs["unexpected.md"] = b"unexpected"
    with pytest.raises(core.ScriptRefusal) as caught:
        core._dependency_original(held["tmp_path"], held["config"], held["policy"], payload,
            {"material": current, "snapshot": snapshot}, purpose="reuse", **selected)
    assert caught.value.details["reason"] == "selective_original_custody_scope_changed"
    del original_inputs["unexpected.md"]
    reproduce = core._dependency_snapshot
    if retrieval != "separate":
        def changed_envelope(*args, **kwargs):
            value = reproduce(*args, **kwargs)
            envelope["authenticated"] = "changed during reproduction"
            return value
        with monkeypatch.context() as changed:
            changed.setattr(core, "_dependency_snapshot", changed_envelope)
            with pytest.raises(core.ScriptRefusal) as caught:
                core._dependency_original(held["tmp_path"], held["config"], held["policy"], payload,
                    {"material": current, "snapshot": snapshot}, purpose="reuse", **selected)
        assert caught.value.details["reason"] == "selective_original_envelope_changed"
        envelope["authenticated"] = "original"
    def changed():
        raise core.refusal("selective_original_custody_changed_at_closing")
    def changed_at_closing(*args, **kwargs):
        value = reproduce(*args, **kwargs)
        proof.assert_current = changed
        return value
    with monkeypatch.context() as closing:
        closing.setattr(core, "_dependency_snapshot", changed_at_closing)
        with pytest.raises(core.ScriptRefusal) as caught:
            core._dependency_original(held["tmp_path"], held["config"], held["policy"], payload,
                {"material": current, "snapshot": snapshot}, purpose="reuse", **selected)
    assert caught.value.details["reason"] == "selective_original_custody_changed_at_closing"
