"""Supplied answer isolation and optional actual native interface execution."""

import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools._comparative_native_recipe import (
    FIXTURE_SOURCE_TEXT,
    FrozenSourceReferenceArtifacts,
    _retain_output,
    execute_recipe,
)
from tools._comparative_protocol import ARMS, freeze_plan, generator_packet
from tools._journey_cases import canonical, digest


def packet_for(arm, *, content=FIXTURE_SOURCE_TEXT, seconds=900):
    guides = {name: {"content": "Use the public " + name + " interface.", "descriptor": None} for name in ARMS}
    for guide in guides.values():
        guide["sha256"] = hashlib.sha256(guide["content"].encode()).hexdigest()
    conditions = {"candidate": {"reference": "local candidate", "revision": None, "identity_sha256": None},
        "model": {"reference": None, "revision": None, "settings": None},
        "capabilities": ["read_supplied_sources", "public_native_support"],
        "rubric_sha256": digest("retained private rubric identity"), "host_observation": "generative",
        "interface_guides": guides, "policy": {"source_access": "supplied_only"}}
    case = {"id": "observation", "original_text": "What solar reflectance is stated in the supplied observation?",
        "stratum": "controlled_snapshot", "sources": [{"source_id": "observation", "content": content,
            "provenance": {"origin": "Synthetic retained observation", "captured_at": None, "authorization": None},
            "rights": {"status": "allowed", "basis": "Synthetic authored text"}}]}
    plan = freeze_plan(study_id="native-interface-observation", cases=[case], conditions=conditions,
        budgets={"seconds": seconds, "model_calls": 100, "tool_calls": 200, "tokens": 1000000,
                 "cost": 0, "max_attempts": 1})
    slot = next(row for row in plan["schedule"] if row["arm"] == arm)
    return generator_packet(plan, slot["slot_id"])


def proposed_answer(packet):
    content = packet["sources"][0]["content"]
    quote = content[:content.index(".", content.index("0.74")) + 1]
    return {"original_text": packet["original_text"], "behavior": "answered",
        "text": "The supplied observation states a reflectance of 0.74.", "scope": "The supplied observation only.",
        "limitations": ["One observation does not establish a universal value."],
        "citations": [{"source_id": "observation", "start": 0, "end": len(quote), "quote": quote}]}


def test_document_only_retains_independent_exact_answer_without_native_approval(monkeypatch):
    def unexpected(*_args):
        raise AssertionError("Document-only execution invoked native setup")

    monkeypatch.setattr("tools._comparative_native_recipe._fixture_provision", unexpected)
    packet = packet_for("document_only")
    answer = proposed_answer(packet)
    result = execute_recipe(packet, arm="document_only", proposed_answer=answer)
    assert result["answer"] == answer
    assert result["native_support"]["native_approval"] == "not_applicable"
    assert result["native_support"]["operation_calls"] == []
    assert result["native_support"]["provider_usage"] == dict.fromkeys(("model_calls", "tool_calls", "tokens", "cost"))
    answer["text"] = "Changed outside the result"
    packet["sources"][0]["content"] = "Changed outside the result"
    assert result["answer"]["text"] == "The supplied observation states a reflectance of 0.74."
    assert result["native_support"]["frozen_source_artifacts"]["sha256"] != digest(packet["sources"])


def test_recipe_never_authors_an_answer_or_accepts_private_comparison_fields():
    packet = packet_for("document_only")
    result = execute_recipe(packet, arm="document_only")
    assert result["answer"] is None
    assert result["native_support"]["current"] is None
    changed = copy.deepcopy(packet)
    changed["conditions"]["policy"]["expected_label"] = "conforms"
    with pytest.raises(ValueError, match="condition_privileged_field"):
        execute_recipe(changed, arm="document_only")


def test_frozen_reference_artifacts_detach_content_and_require_full_source_hash():
    packet = packet_for("document_only")
    frozen = FrozenSourceReferenceArtifacts.from_packet(packet)
    copied = frozen.sources()
    copied[0]["content"] = "changed"
    assert frozen.sources()[0]["content"] == FIXTURE_SOURCE_TEXT
    packet["sources"][0]["content"] += "additional evidence"
    with pytest.raises(ValueError, match="recipe_source_digest_mismatch"):
        FrozenSourceReferenceArtifacts.from_packet(packet)


@pytest.mark.parametrize("max_steps", [0, 65, True, 1.0])
def test_invalid_drive_bounds_fail_before_provisioning(tmp_path, max_steps):
    with pytest.raises(ValueError, match="recipe_step_bound"):
        execute_recipe(packet_for("low_level"), arm="low_level", output_directory=tmp_path / "new", max_steps=max_steps)
    assert not (tmp_path / "new").exists()


def test_native_recipe_refuses_substitution_and_nonprivate_repository_targets(tmp_path):
    with pytest.raises(ValueError, match="recipe_native_fixture_source_unsupported"):
        execute_recipe(packet_for("core_library", content="Another retained observation."),
                       arm="core_library", output_directory=tmp_path / "new")
    with pytest.raises(ValueError, match="recipe_native_directory_must_be_outside_checkout"):
        execute_recipe(packet_for("low_level"), arm="low_level",
                       output_directory=Path(__file__).resolve().parents[1] / "new-native-runtime")
    assert not (tmp_path / "new").exists()


def test_native_output_custody_retains_exact_private_bytes_without_overwriting(tmp_path):
    rendered = canonical({"operation_receipts": [{"result_sha256": digest("returned public owner document")}],
                          "answer": None})
    _retain_output(tmp_path, rendered)
    path = tmp_path / "native-support-result.json"
    assert path.read_bytes() == rendered
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        _retain_output(tmp_path, canonical({"replacement": True}))
    assert path.read_bytes() == rendered


@pytest.mark.parametrize("arm", ["low_level", "core_library"])
def test_fixture_byte_drift_is_retained_and_refused_before_native_activation(tmp_path, monkeypatch, arm):
    activations = []

    def unexpected_activation():
        activations.append(True)
        raise AssertionError("Mismatched frozen evidence reached native activation")

    def drifted_provision(directory, _original):
        (directory / "raw" / "papers").mkdir(parents=True)
        (directory / "sources").mkdir()
        (directory / "raw" / "papers" / "study.md").write_text("A substituted observation.\n")
        (directory / "sources" / "manifest.jsonl").write_text(json.dumps({"id": "observation"}) + "\n")
        runtime = SimpleNamespace(
            policy=SimpleNamespace(to_dict=lambda: {}),
            execution=SimpleNamespace(profile=SimpleNamespace(document=lambda: {})),
            context=SimpleNamespace(activate=unexpected_activation),
        )
        return directory, runtime, {}

    monkeypatch.setattr("tools._comparative_native_recipe._fixture_provision", drifted_provision)
    directory = tmp_path / arm
    result = execute_recipe(packet_for(arm), arm=arm, output_directory=directory)
    support = result["native_support"]
    assert support["source_binding"]["content_exact"] is False
    assert support["error"]["kind"] == "host_failure"
    assert support["executed"] is False
    assert support["current"] is None and support["cycle"] is None
    assert [row["operation"] for row in support["operation_calls"]] == ["public_example.provision"]
    assert activations == []
    assert (directory / "native-support-result.json").read_bytes() == canonical(result)


@pytest.mark.skipif(os.environ.get("EVIDENCE_COMPARATIVE_NATIVE_GATE") != "1",
                    reason="Actual synthetic native sessions are an explicitly selected expensive gate")
def test_actual_three_arm_interfaces_keep_native_receipts_and_answers_separate(tmp_path):
    results = {}
    for arm in ARMS:
        packet = packet_for(arm)
        results[arm] = execute_recipe(packet, arm=arm, output_directory=tmp_path / arm,
                                     proposed_answer=proposed_answer(packet))
        assert results[arm]["answer"] == proposed_answer(packet)
    assert results["document_only"]["native_support"]["native_approval"] == "not_applicable"
    for arm in ("low_level", "core_library"):
        result = results[arm]
        support = result["native_support"]
        retained = tmp_path / arm / "native-support-result.json"
        assert retained.read_bytes() == canonical(result)
        assert retained.stat().st_mode & 0o777 == 0o600
        assert support["error"] is None, support["error"]
        assert support["executed"] and support["current"] is not None
        assert support["observation_class"] == "synthetic_native_execution"
        assert support["source_binding"]["content_exact"] is True
        assert support["source_binding"]["packet_content_sha256"] == hashlib.sha256(FIXTURE_SOURCE_TEXT.encode()).hexdigest()
        assert support["qualification"]["native_internal_caps_matched"] is False
        assert support["native_policy"]["budgets"]["seconds"] == 1800
        assert support["outer_drive_bounds"] == {"seconds": 900, "steps": 64}
        assert support["brief"] is not None and support["report"] is not None
        assert support["native_receipt_refs"] and all(len(ref) == 64 for ref in support["native_receipt_refs"])
        operations = [row["operation"] for row in support["operation_calls"]]
        if arm == "low_level":
            assert "ResearchSession.next" in operations and "ResearchSession.advance" in operations
            assert "ResearchNamespace.run_cycle" not in operations
            assert 1 <= len(support["steps"]) <= 64
        else:
            assert operations.count("ResearchNamespace.run_cycle") == 1
            assert "ResearchSession.advance" not in operations
            assert support["cycle"]["terminal"] is True
        authority = json.loads((tmp_path / arm / "fixture-private" / "authority.json").read_text())
        rendered = canonical(result)
        assert not any(key.encode() in rendered for principal in authority["principals"].values()
                       for key in principal["keys"].values())
        assert b"expected_label" not in rendered
