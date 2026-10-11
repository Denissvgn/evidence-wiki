"""Public finite selections derive and preserve canonical policy identities."""

import copy
import hashlib
import importlib
import json
import subprocess
from pathlib import Path

import pytest

from evidence_wiki import RunPolicy
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.work_cycle_contracts import WorkProposal


def test_finite_selections_reconstruct_modified_policy_without_stale_identity(tmp_path, monkeypatch):
    example = Path(__file__).resolve().parents[1] / "examples/unattended-host"
    monkeypatch.syspath_prepend(str(example))
    recipe = importlib.import_module("finite_host")
    support = importlib.import_module("support")
    for name in ("root", "private", "control", "workers"):
        (tmp_path / name).mkdir(mode=0o700)
    original = support.policy(tmp_path / "root", "Retain a finite observation.")
    document = {name: str(tmp_path / name) for name in ("root", "private", "control", "workers")}
    document.update(worker=str(example / "finite_workers.py"), interpreter=str(Path(__file__).resolve().parents[1] / ".venv/bin/python"),
        policy=original.to_dict(), rubric={"id": "finite-fixture", "revision": "1"}, expires_utc="2099-01-01T00:00:00+00:00",
        keys={name: hashlib.sha256(("finite-selection-fixture/" + name).encode()).hexdigest() for name in
              ("receipt", "qualifier", "generator", "evidence", "scope", "critic", "adjudicator")})
    untouched = copy.deepcopy(document)
    policy, profile, suite, executor, authority, signer, context = recipe.selections(document)
    assert document == untouched
    assert policy.policy_id != original.policy_id
    assert RunPolicy(policy.to_dict()).policy_id == policy.policy_id
    assert [row["profile_ref"] for row in policy.to_dict()["required_evaluators"]] == ["evidence", "scope", "critic"]
    assert [selection.config_ref for selection in profile.calibration.selections] == ["adjudicator", "critic", "evidence", "scope"]
    assert len(suite["cases"]) == 600
    restored = recipe.selections(copy.deepcopy(document))
    assert restored[0].policy_id == policy.policy_id and restored[1].sha256 == profile.sha256
    assert all(executor.calibration_configuration(name)["execution_profile_sha256"] == profile.sha256
               for name in ("evidence", "scope", "critic", "adjudicator"))
    changed = policy.to_dict()
    changed["objective"] += " Changed."
    with pytest.raises(EvidenceWikiError) as caught:
        RunPolicy(changed)
    assert caught.value.error_code == "RUN_POLICY_IDENTITY_MISMATCH"
    assert caught.value.details == {"field": "/policy_id"}


def test_public_finite_planner_emits_only_the_exact_owner_action():
    project = Path(__file__).resolve().parents[1]
    action = {"operation": "prepare", "original_id": "q", "claim_id": None, "evidence_refs": []}
    basis = "sha256:" + "a" * 64
    payload = {"input": {"basis_id": basis, "owner_actions": [action]}}
    child = subprocess.run([str(project / ".venv/bin/python"), "-B",
        str(project / "examples/unattended-host/finite_workers.py"), "planner"], input=json.dumps(payload),
        capture_output=True, text=True, timeout=5, check=True, cwd=project)
    assert child.stderr == ""
    proposal = WorkProposal.from_dict(json.loads(child.stdout))
    assert proposal.basis_id == basis and len(proposal.candidates) == 1
    chosen = proposal.candidates[0].document()
    assert {name: chosen[name] for name in action} == action
    assert chosen["candidate_id"] == "next"
