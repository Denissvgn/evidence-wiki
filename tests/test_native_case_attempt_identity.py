"""Keep shared-executor attempt identities separate without moving packet coordinates."""

import ast
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from evidence_wiki.agent_execution import BoundedSubprocessExecutor, ExecutionError
from tools._review_dependency_workload import digest

SOURCE = Path(__file__).resolve().parents[1] / "tools" / "_review_dependency_cases.py"


def _drive_expressions():
    tree = ast.parse(SOURCE.read_text())
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "NativeCases")
    drive = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == "drive")
    expressions = {}
    for name in ("run_id", "identifier", "candidate"):
        assignments = [
            node.value for node in ast.walk(drive)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
        ]
        assert len(assignments) == 1
        expressions[name] = compile(ast.Expression(assignments[0]), str(SOURCE), "eval")
    return expressions


def _evaluate(expression, **values):
    return eval(expression, {"__builtins__": {}, "digest": digest, "str": str}, values)  # noqa: S307


def _attempt(identifier, run_id, config_ref):
    document = {"attempt_id": identifier, "run_id": run_id, "config_ref": config_ref, "binding_id": run_id}
    return SimpleNamespace(attempt_id=identifier, document=lambda: document)


def test_shared_executor_attempt_identity_is_unique_and_repeatable():
    expressions = _drive_expressions()
    # Only _check is detached: exercise the real executor's storage key and comparison.
    executor = SimpleNamespace(_check=lambda attempt: None, _attempts={})
    identifiers = set()
    for cycle in range(4):
        for claim_id in ("first", "second", "third"):
            run_id = _evaluate(expressions["run_id"], cycle=cycle, claim={"id": claim_id})
            for config_ref in ("evidence", "critic"):
                identifier = _evaluate(expressions["identifier"], run_id=run_id, config_ref=config_ref)
                assert identifier not in identifiers
                identifiers.add(identifier)
                attempt = _attempt(identifier, run_id, config_ref)
                assert BoundedSubprocessExecutor._stored(executor, attempt) is None
                stored = SimpleNamespace(attempt=attempt)
                executor._attempts[identifier] = stored
                repeated_id = _evaluate(expressions["identifier"], run_id=run_id, config_ref=config_ref)
                repeated = _attempt(repeated_id, run_id, config_ref)
                assert BoundedSubprocessExecutor._stored(executor, repeated) is stored
                for suffix in ("", "-apply", "-intake-99999999999999999999"):
                    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", identifier + suffix)
    assert len(identifiers) == 24


def test_shared_executor_still_rejects_conflicting_attempt_pin():
    original = _attempt("review-evidence", "first-run", "evidence")
    executor = SimpleNamespace(_check=lambda attempt: None, _attempts={original.attempt_id: SimpleNamespace(attempt=original)})
    changed = _attempt(original.attempt_id, "second-run", "evidence")
    with pytest.raises(ExecutionError, match="execution_attempt_conflict"):
        BoundedSubprocessExecutor._stored(executor, changed)


def test_packet_coordinate_search_retains_every_original_candidate():
    expressions = _drive_expressions()
    for config_ref in ("evidence", "critic"):
        identifier = _evaluate(expressions["identifier"], run_id="review-" + "f" * 24, config_ref=config_ref)
        for ordinal in range(1024):
            candidate = _evaluate(expressions["candidate"], identifier=identifier, config_ref=config_ref, ordinal=ordinal)
            assert candidate == "review-" + config_ref + "-packet-" + str(ordinal)
