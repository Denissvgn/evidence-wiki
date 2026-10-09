"""Frozen exact inventories and failure precedence for the finite outer gate."""

import copy

import pytest

from tools._acceptance_protocol import (
    REQUIRED_CATEGORIES,
    bounded_json,
    digest,
    freeze_manifest,
    grade_evidence,
    policy,
    validate_cases,
    validate_manifest,
)


def cases(requirement="owner_receipt"):
    return [{"id": "bounded-owner", "categories": sorted(REQUIRED_CATEGORIES), "severity": "critical",
             "evidence_requirement": requirement, "selectors": ["tests/test_owner.py::test_owner"]}]


def manifest(requirement="owner_receipt"):
    return freeze_manifest(cases(requirement), ["tests/test_owner.py::test_owner[a]"], inputs={"candidate": "fixed"})


def observations(node="tests/test_owner.py::test_owner[a]"):
    result = {"outcome": "refused", "authorized": False, "basis_id": "a" * 64}
    return [{"event": "started", "nodeid": node},
            {"event": "report", "nodeid": node, "phase": "setup", "outcome": "passed", "wasxfail": None, "assertion_failure": False},
            {"event": "owner_result", "nodeid": node, "owner": "Workspace.release", "result": result,
             "result_sha256": digest(result), "owner_result_sha256": digest(result)},
            {"event": "assertions", "nodeid": node, "observed": 2},
            *[{"event": "report", "nodeid": node, "phase": phase, "outcome": "passed", "wasxfail": None,
               "assertion_failure": False} for phase in ("call", "teardown")],
            {"event": "artifacts", "nodeid": node, "refs": []}, {"event": "finished", "nodeid": node}]


def test_exact_native_refusal_passes_only_challenge_and_keeps_all_unknowns():
    result = grade_evidence(manifest(), observations(), process_complete=True)
    assert result["status"] == "PASS"
    assert result["denominators"]["exact_nodes"] == 1
    assert all(result[field] is None for field in ("native_authorization", "answer_success", "monetary_cost", "model_calls", "tokens"))


@pytest.mark.parametrize("change", ["missing", "extra", "duplicate", "skip", "xfail", "zero-assertions", "no-owner", "malformed-owner", "malformed-phase"])
def test_missing_extra_duplicate_skipped_or_unchecked_nodes_abstain(change):
    events = observations()
    if change == "missing":
        events.pop()
    elif change == "extra":
        events.append({"event": "started", "nodeid": "tests/test_extra.py::test_extra"})
    elif change == "duplicate":
        events.append(copy.deepcopy(events[4]))
    elif change == "skip":
        events[4]["outcome"] = "skipped"
    elif change == "xfail":
        events[4]["wasxfail"] = "expected"
    elif change == "zero-assertions":
        events[3]["observed"] = 0
    elif change == "no-owner":
        events.pop(2)
    elif change == "malformed-owner":
        events[2]["result_sha256"] = "0" * 64
    else:
        events[4]["phase"] = []
    assert grade_evidence(manifest(), events, process_complete=True)["status"] == "ABSTAIN"


def test_critical_failure_survives_votes_gaps_and_policy_threshold_edits():
    frozen = manifest()
    events = observations()
    events[4].update(outcome="failed", assertion_failure=False)
    events.append({"event": "vote", "nodeid": events[0]["nodeid"], "verdict": "PASS"})
    events.pop()
    events.pop()
    result = grade_evidence(frozen, events, process_complete=False)
    assert result["status"] == "FAIL"
    assert result["cases"][0]["status"] == "FAIL"
    frozen["policy"]["critical_bypasses_allowed"] = 1
    result = grade_evidence(frozen, [], process_complete=False)
    assert result["status"] == "FAIL"
    assert "policy_drift" in result["failures"]


def test_candidate_and_retention_drift_dominate_abstention():
    assert grade_evidence(manifest(), [], drift=True)["status"] == "FAIL"
    assert grade_evidence(manifest(), [], evidence_intact=False)["status"] == "FAIL"


def test_setup_and_teardown_assertion_errors_are_incomplete_owner_calls():
    for phase in ("setup", "teardown"):
        events = observations()
        next(event for event in events if event.get("phase") == phase).update(outcome="failed", assertion_failure=True)
        assert grade_evidence(manifest(), events, process_complete=True)["status"] == "ABSTAIN"


def test_collection_expands_all_parameters_once_and_seed_freezes_case_priority():
    inventory = cases("assertions")
    inventory.append({"id": "second-owner", "categories": ["replay"], "severity": "critical",
                      "evidence_requirement": "assertions", "selectors": ["tests/test_owner.py::test_second"]})
    nodes = ["tests/test_owner.py::test_owner[a]", "tests/test_owner.py::test_owner[b]", "tests/test_owner.py::test_second"]
    frozen = freeze_manifest(inventory, nodes, inputs={}, seed=7)
    assert set(frozen["exact_nodeids"]) == set(nodes)
    assert freeze_manifest(inventory, nodes, inputs={}, seed=7) == frozen
    with pytest.raises(ValueError, match="exact_inventory"):
        freeze_manifest(inventory, nodes + nodes[:1], inputs={})
    with pytest.raises(ValueError, match="unresolved_selector"):
        freeze_manifest(inventory, nodes[:2], inputs={})
    with pytest.raises(ValueError, match="node_membership"):
        freeze_manifest(inventory, nodes + ["tests/test_extra.py::test_extra"], inputs={})


def test_seeded_module_blocks_preserve_all_case_membership_and_parameter_obligations():
    inventory = cases("assertions")
    inventory.append({"id": "other-owner", "categories": ["replay"], "severity": "critical",
                      "evidence_requirement": "assertions", "selectors": ["tests/test_other.py::test_other"]})
    inventory.append({"id": "later-owner", "categories": ["forged-review"], "severity": "critical",
                      "evidence_requirement": "assertions", "selectors": ["tests/test_owner.py::test_later", "tests/test_other.py::test_later"]})
    nodes = ["tests/test_owner.py::test_owner[a]", "tests/test_owner.py::test_owner[b]",
             "tests/test_other.py::test_other", "tests/test_owner.py::test_later", "tests/test_other.py::test_later"]
    orders = set()
    for seed in range(8):
        frozen = freeze_manifest(inventory, nodes, inputs={}, seed=seed, timeout_seconds=1800)
        assert validate_manifest(frozen) == frozen
        assert freeze_manifest(inventory, nodes, inputs={}, seed=seed, timeout_seconds=1800) == frozen
        assert len(frozen["exact_nodeids"]) == len(nodes) and set(frozen["exact_nodeids"]) == set(nodes)
        assert set(frozen["membership"].values()) == {case["id"] for case in inventory}
        modules = [node.split("::", 1)[0] for node in frozen["exact_nodeids"]]
        blocks = [module for index, module in enumerate(modules) if index == 0 or module != modules[index - 1]]
        assert blocks == frozen["module_order"] and len(blocks) == len(set(modules))
        assert frozen["policy"]["retry_budget"] == 0 and frozen["timeout_seconds"] == 1800
        orders.add(tuple(frozen["challenge_priority"]))
        changed = copy.deepcopy(frozen)
        changed["module_order"].reverse()
        changed["manifest_sha256"] = digest({key: value for key, value in changed.items() if key != "manifest_sha256"})
        with pytest.raises(ValueError, match="reconstruction_conflict"):
            validate_manifest(changed)
    assert len(orders) > 1


def test_closed_cases_category_denominator_and_immutable_policy():
    incomplete = cases()
    incomplete[0]["categories"].remove("infinite-repair")
    with pytest.raises(ValueError, match="category_inventory"):
        validate_cases(incomplete)
    injected = cases()
    injected[0]["threshold"] = 1
    with pytest.raises(ValueError, match="case_shape"):
        validate_cases(injected)
    detached = policy()
    detached["critical_bypasses_allowed"] = 9
    assert policy()["critical_bypasses_allowed"] == 0


def test_bounds_are_checked_before_encoding_and_execution():
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match="observation_bound"):
        bounded_json(cyclic)
    with pytest.raises(ValueError, match="execution_budget"):
        freeze_manifest(cases(), ["tests/test_owner.py::test_owner"], inputs={}, timeout_seconds=float("inf"))
    with pytest.raises(ValueError, match="selector"):
        bad = cases()
        bad[0]["selectors"] = ["tests/../outside.py::test_owner"]
        validate_cases(bad)


@pytest.mark.parametrize("change", ["missing-category", "category-type", "severity", "unresolved", "seed", "budget", "order", "membership", "collection", "schema", "extra-field"])
def test_rehashed_malformed_or_downgraded_manifest_never_passes(change):
    frozen = manifest()
    if change == "missing-category":
        frozen["cases"][0]["categories"].pop()
    elif change == "category-type":
        frozen["cases"][0]["categories"] = "replay"
    elif change == "severity":
        frozen["cases"][0]["severity"] = "standard"
    elif change == "unresolved":
        frozen["cases"][0]["selectors"].append("tests/test_owner.py::test_missing")
    elif change == "seed":
        frozen["seed"] = "0"
    elif change == "budget":
        frozen["timeout_seconds"] = -1
    elif change == "order":
        frozen["challenge_priority"] = []
    elif change == "membership":
        frozen["membership"][frozen["exact_nodeids"][0]] = "unowned"
    elif change == "collection":
        frozen["collected_nodeids"] = []
    elif change == "schema":
        frozen["schema_version"] = "unrecognized"
    else:
        frozen["threshold"] = 1
    frozen["manifest_sha256"] = digest({key: value for key, value in frozen.items() if key != "manifest_sha256"})
    assert grade_evidence(frozen, observations(), process_complete=True)["status"] != "PASS"


@pytest.mark.parametrize("change", ["before-start", "after-finish", "owner-after-call", "assert-before-setup", "teardown-before-call", "duplicate-start", "artifact-before-call"])
def test_phase_order_and_duplicate_events_are_not_bag_of_passes(change):
    events = observations()
    if change == "before-start":
        events[0], events[1] = events[1], events[0]
    elif change == "after-finish":
        events.append(events.pop(2))
    elif change == "owner-after-call":
        owner = events.pop(2)
        events.insert(4, owner)
    elif change == "assert-before-setup":
        events[1], events[3] = events[3], events[1]
    elif change == "teardown-before-call":
        events[4], events[5] = events[5], events[4]
    elif change == "duplicate-start":
        events.insert(1, copy.deepcopy(events[0]))
    else:
        events[3], events[6] = events[6], events[3]
    assert grade_evidence(manifest(), events, process_complete=True)["status"] == "ABSTAIN"


def test_native_artifact_requirement_cannot_be_replaced_by_owner_return():
    assert grade_evidence(manifest("native_artifact"), observations(), process_complete=True)["status"] == "ABSTAIN"
