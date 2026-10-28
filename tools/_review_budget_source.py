"""Unchanged historical budgets after directory and process custody repairs."""

from __future__ import annotations

import ast
import hashlib
import threading
from contextlib import contextmanager
from contextvars import ContextVar

_ACTIVE = ContextVar("review_directory_budget_source", default=None)
_RUNTIME_EDITS = {
    "tools/_review_dependency_workload.py": {"compile_fixture_policy", "_fixture_planning_root", "provision",
        "_SetupChildCustody", "_setup_child_custody", "_setup_dispatch_associations"},
    "tools/_review_dependency_pilot.py": {"completed_extended_pilot_measurements"},
    "tools/run_review_dependency_workload.py": {"_completed_pilot_measurements"},
    "tools/_review_dependency_accounting.py": {"dispatch_snapshot"},
    "tools/_review_dependency_pair_closing.py": {"_executor"},
}
_ADDED_SOURCE = "tools/_review_budget_source.py"
_TEST_SOURCE = "tests/test_selective_review_workload.py"
_PAIRS = {("direct", "independent-claim-text"), ("scoped_mcp", "independent-claim-limitations")}
_CAPTURE_RUNTIME_EDITS = {
    **_RUNTIME_EDITS,
    "tools/_review_dependency_workload.py": _RUNTIME_EDITS["tools/_review_dependency_workload.py"] | {"host_read_preflight"},
    "tools/run_review_dependency_workload.py": _RUNTIME_EDITS["tools/run_review_dependency_workload.py"] | {"_owner_read"},
    "tools/_execution_budget.py": {"provisioning_window"},
    "tools/_review_dependency_runner.py": {"reference_plan", "preflight", "_budget_producer",
        "_capture_questions", "_capture_completed_mode"},
    "src/evidence_wiki/research_journal_contracts.py": {"_RecordCodecMemo", "_record_codec_key",
        "_codec_schema_bytes", "_codec_schema_backend", "_codec_native_schema_key",
        "_canonical_owned_record", "canonical_record"},
}
_CAPTURE_TEST_SOURCES = {_TEST_SOURCE, "tests/test_review_pair_closing.py", "tests/test_journal_owned_scan.py",
                         "tests/test_review_journal_artifacts.py", "tests/test_review_dependency_workload.py"}
_HELPER_RUNTIME_EDITS = {
    **_CAPTURE_RUNTIME_EDITS,
    "workspace-template/scripts/_evidence_policies.py": {"source_unusable_evidence_reasons"},
}
_HELPER_TEST_SOURCES = _CAPTURE_TEST_SOURCES | {"tests/test_evidence_policies.py", "tests/test_qualified_packet_module_cache.py"}
_PREPARATION_RUNTIME_EDITS = {
    **_HELPER_RUNTIME_EDITS,
    "src/evidence_wiki/research_journal_collections.py": {"_prepared"},
}
_PREPARATION_TEST_SOURCES = _HELPER_TEST_SOURCES | {"tests/test_journal_collection_codec.py"}
_CUSTODY_RUNTIME_EDITS = {
    **_PREPARATION_RUNTIME_EDITS,
    "src/evidence_wiki/_review_dependency_custody.py": {"_Original.original_material"},
    "workspace-template/scripts/_strict_evidence.py": {"_dependency_original", "dependency_review_result", "validate_dependency_review"},
}
_CUSTODY_TEST_SOURCES = _PREPARATION_TEST_SOURCES | {
    "tests/test_review_dependency_custody.py", "tests/test_review_dependencies.py", "tests/test_strict_dependency_selection.py"}


def _require(condition):
    if not condition:
        raise ValueError("review_budget_source_incompatible")


def _unchanged_module(raw, path, edits=None):
    edits = _RUNTIME_EDITS if edits is None else edits
    tree = ast.parse(raw)
    retained = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in edits[path]:
            continue
        if isinstance(node, ast.ClassDef):
            node.body = [item for item in node.body if not (isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and f"{node.name}.{item.name}" in edits[path])]
        if isinstance(node, ast.Import) and path == "tools/_review_dependency_workload.py":
            node.names = [item for item in node.names if item.name not in {"os", "stat"} or item.asname is not None]
            if not node.names:
                continue
        if (isinstance(node, ast.ImportFrom) and path == "src/evidence_wiki/research_journal_collections.py"
                and node.level == 1 and node.module == "research_journal_contracts"):
            node.names = [item for item in node.names if item.asname is not None
                or item.name not in {"canonical_record", "detached_record", "_canonical_owned_record"}]
            if not node.names:
                continue
        retained.append(node)
    tree.body = retained
    return ast.dump(tree, include_attributes=False)


def _check_sources(historical, current, originals, root, *, edits=None, tests=None):
    """Check exact membership and preserve all other runtime definitions and data."""
    _require(type(historical) is dict and type(current) is dict and type(originals) is dict)
    edits = _RUNTIME_EDITS if edits is None else edits
    tests = {_TEST_SOURCE} if tests is None else tests
    _require(set(originals) == set(edits))
    _require(set(current) - set(historical) == {_ADDED_SOURCE} and not set(historical) - set(current))
    changed = {path for path in historical if historical[path] != current[path]}
    _require(changed == set(edits) | tests)
    for path in edits:
        old = originals[path]
        _require(type(old) is str and hashlib.sha256(old.encode()).hexdigest() == historical[path]["sha256"])
        raw = (root / path).read_bytes()
        _require(hashlib.sha256(raw).hexdigest() == current[path]["sha256"])
        _require(_unchanged_module(old, path, edits) == _unchanged_module(raw, path, edits))


def _current_producer(binding, recipe, historical, current, generation):
    """Bind exact declared repairs without claiming historical measurements anew."""
    from evidence_wiki import _review_producer
    from tools._review_dependency_runner import _read

    before = _read(binding["historical_producer_manifest_pin"])
    after = _read(binding["current_producer_manifest_pin"])

    def identity(manifest):
        return _review_producer.content_id("evidence-review-producer/v1", {
            "schema_version": "evidence-review-producer/v1",
            "manifest_id": _review_producer.content_id("evidence-review-producer-manifest/v1", manifest)})

    _require(identity(before) == recipe["review_producer_id"]
             and identity(after) == generation.producer_id
             and generation.document()["manifest"] == after)
    old = {row["path"]: row for row in before["members"]}
    new = {row["path"]: row for row in after["members"]}
    _require(len(old) == len(before["members"]) and len(new) == len(after["members"])
             and set(old) == set(new))
    repairs = {"package/evidence_wiki/research_journal_contracts.py": "src/evidence_wiki/research_journal_contracts.py"}
    if binding["schema_version"] in {"review-directory-budget-source/v5", "review-directory-budget-source/v6", "review-directory-budget-source/v7"}:
        repairs["assets/workspace-template/scripts/_evidence_policies.py"] = "workspace-template/scripts/_evidence_policies.py"
    if binding["schema_version"] in {"review-directory-budget-source/v6", "review-directory-budget-source/v7"}:
        repairs["package/evidence_wiki/research_journal_collections.py"] = "src/evidence_wiki/research_journal_collections.py"
    if binding["schema_version"] == "review-directory-budget-source/v7":
        repairs["package/evidence_wiki/_review_dependency_custody.py"] = "src/evidence_wiki/_review_dependency_custody.py"
        repairs["assets/workspace-template/scripts/_strict_evidence.py"] = "workspace-template/scripts/_strict_evidence.py"
    _require({path for path in old if old[path] != new[path]} == set(repairs))
    for member, source in repairs.items():
        for rows, manifest in ((old, historical), (new, current)):
            _require(rows[member]["sha256"] == "sha256:" + manifest[source]["sha256"]
                     and rows[member]["size_bytes"] == manifest[source]["bytes"])
    return generation.producer_id


@contextmanager
def directory_budget_source(binding_pin, *, pair):
    """Select a pinned budget-only source binding for one of the two fixed pairs."""
    from evidence_wiki import _review_producer
    from tools import run_review_dependency_workload as legacy
    from tools._review_dependency_runner import _read

    _require(_ACTIVE.get() is None and pair in _PAIRS)
    binding = _read(binding_pin)
    keys = {
        "schema_version", "scope", "historical_recipe_pin", "historical_manifest_pin",
        "current_manifest_pin", "historical_sources_pin", "qualification"}
    _require(type(binding) is dict)
    protocol = binding.get("schema_version")
    custody = protocol == "review-directory-budget-source/v7"
    preparation = custody or protocol == "review-directory-budget-source/v6"
    helper_reuse = preparation or protocol == "review-directory-budget-source/v5"
    repaired = helper_reuse or protocol == "review-directory-budget-source/v4"
    if repaired:
        keys |= {"historical_producer_manifest_pin", "current_producer_manifest_pin"}
    _require(set(binding) == keys)
    expected_scope = ("unchanged_budgets_after_atomic_original_custody_reads" if custody
        else "unchanged_budgets_after_selective_setup_codec_repair" if preparation
        else "unchanged_budgets_after_validated_helper_module_reuse" if helper_reuse
        else "unchanged_budgets_after_capture_and_codec_repairs" if repaired
        else "unchanged_budgets_after_directory_and_setup_custody_repairs")
    _require(protocol in {"review-directory-budget-source/v3", "review-directory-budget-source/v4", "review-directory-budget-source/v5", "review-directory-budget-source/v6", "review-directory-budget-source/v7"}
             and binding["scope"] == expected_scope
             and binding["qualification"] is False)
    recipe = _read(binding["historical_recipe_pin"])
    historical = _read(binding["historical_manifest_pin"])
    current = _read(binding["current_manifest_pin"])
    originals = _read(binding["historical_sources_pin"])
    _require(recipe.get("schema_version") == "selective-complete-pilot-measurement/v5")
    historical_id, current_id = legacy.digest(historical), legacy.digest(current)
    _require(recipe["candidate_sha256"] == historical_id)
    _require(legacy.candidate_manifest(legacy.ROOT) == current)
    source_checks = ({"edits": _CUSTODY_RUNTIME_EDITS, "tests": _CUSTODY_TEST_SOURCES} if custody
        else {"edits": _PREPARATION_RUNTIME_EDITS, "tests": _PREPARATION_TEST_SOURCES} if preparation
        else {"edits": _HELPER_RUNTIME_EDITS, "tests": _HELPER_TEST_SOURCES} if helper_reuse
        else {"edits": _CAPTURE_RUNTIME_EDITS, "tests": _CAPTURE_TEST_SOURCES} if repaired else {})
    _check_sources(historical, current, originals, legacy.ROOT, **source_checks)
    generation = _review_producer.capture()
    if repaired:
        producer_id = _current_producer(binding, recipe, historical, current, generation)
    else:
        _require(generation.producer_id == recipe["review_producer_id"])
        producer_id = generation.producer_id
    selected = {"active": True, "thread": threading.get_ident(), "historical": historical_id,
                "current": current_id, "recipe": legacy.digest(recipe), "current_producer": producer_id}
    token = _ACTIVE.set(selected)
    try:
        yield
    finally:
        selected["active"] = False
        _ACTIVE.reset(token)
        _require(_read(binding_pin) == binding and _read(binding["historical_recipe_pin"]) == recipe)
        if repaired:
            _require(_current_producer(binding, recipe, historical, current, generation) == producer_id)
        _require(legacy.candidate_manifest(legacy.ROOT) == current)
        _review_producer.assert_current(generation)


def _selected(historical_candidate, current_candidate):
    binding = _ACTIVE.get()
    _require(binding is not None and binding["active"] is True
             and binding["thread"] == threading.get_ident()
             and historical_candidate == binding["historical"]
             and current_candidate == binding["current"])
    return binding


def measurement_candidate(measured, current_candidate):
    """Keep the historical recipe unchanged while selecting its original source."""
    from tools._review_dependency_workload import digest

    historical = measured.get("candidate_sha256")
    binding = _selected(historical, current_candidate)
    _require(digest(measured) == binding["recipe"])
    return historical


def historical_plan_candidate(historical_candidate, current_candidate):
    """Use historical identity only in reconstructed historical plan expectations."""
    _selected(historical_candidate, current_candidate)
    return historical_candidate


def execution_producer(measured, current_candidate):
    """Select the pinned current producer under the unchanged historical caps."""
    from tools._review_dependency_workload import digest

    binding = _selected(measured.get("candidate_sha256"), current_candidate)
    _require(digest(measured) == binding["recipe"])
    return binding["current_producer"]
