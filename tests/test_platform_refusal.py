"""Native Windows is refused at every entry point before any effect.

The wheel installs on any platform, so the refusal is a runtime check. The package
refuses on import. Workspace scripts run without the package, so each script entry
point must reach, through its unconditional top-level imports, a module that
refuses at import time. Faking ``os.name`` breaks ``pathlib``, so behavior is
tested by passing the platform name, and coverage is checked statically.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evidence_wiki import _platform
from evidence_wiki._script_host import load_packaged_script, shared_assets_root

SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
# The modules that refuse at import. Pinned, so removing one refusal fails even
# where another refusal still covers every entry point.
REFUSING_MODULES = {"_script_errors", "_workspace_module_loader", "_native_fs", "_workspace_locks",
                    "snapshot_user_edits"}
# Calls that would act on the filesystem or start a process before the refusal.
EFFECT_ROOTS = {"open", "os", "subprocess", "shutil"}
EFFECT_METHODS = {"write_text", "write_bytes", "mkdir", "touch", "unlink", "rename", "replace", "rmdir"}


@pytest.fixture(scope="module")
def script_errors():
    return load_packaged_script(shared_assets_root(), "_script_errors")


def _stems():
    return {path.stem for path in SCRIPTS.glob("*.py")}


def _trees():
    return {path.stem: ast.parse(path.read_text(encoding="utf-8")) for path in SCRIPTS.glob("*.py")}


def _statement_imports(node, stems):
    if isinstance(node, ast.Import):
        return {alias.name.split(".")[0] for alias in node.names} & stems
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return {node.module.split(".")[0]} & stems
    if isinstance(node, ast.Try):
        return set().union(*(_statement_imports(child, stems) for child in node.body))
    return set()


def _closure(stem, graph):
    seen, pending = set(), [stem]
    while pending:
        current = pending.pop()
        if current not in seen:
            seen.add(current)
            pending.extend(graph[current])
    return seen


def _is_platform_refusal(node, refusal):
    """Recognize ``if os.name == "nt": raise SystemExit(<refusal>)`` or the shared call."""
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        call = node.value
        return (isinstance(call.func, ast.Name) and call.func.id == "refuse_unsupported_platform"
                and ast.unparse(call.args) == ast.unparse([ast.parse("os.name", mode="eval").body]))
    if not isinstance(node, ast.If) or node.orelse or len(node.body) != 1:
        return False
    test, body = node.test, node.body[0]
    return (isinstance(test, ast.Compare) and isinstance(test.left, ast.Attribute) and test.left.attr == "name"
            and isinstance(test.left.value, ast.Name) and test.left.value.id in {"os", "_os"}
            and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
            and isinstance(test.comparators[0], ast.Constant) and test.comparators[0].value == "nt"
            and isinstance(body, ast.Raise) and isinstance(body.exc, ast.Call)
            and isinstance(body.exc.func, ast.Name) and body.exc.func.id == "SystemExit"
            and len(body.exc.args) == 1 and isinstance(body.exc.args[0], ast.Constant)
            and body.exc.args[0].value == refusal)


def _effects(node):
    found = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        func, names = call.func, []
        while isinstance(func, ast.Attribute):
            names.append(func.attr)
            func = func.value
        if isinstance(func, ast.Name):
            names.append(func.id)
        if (names and names[-1] in EFFECT_ROOTS) or EFFECT_METHODS & set(names):
            found.append(ast.unparse(call))
    return found


def test_package_refuses_native_windows_by_platform_name():
    assert _platform.require_supported("posix") is None
    with pytest.raises(_platform.PlatformUnsupported) as refused:
        _platform.require_supported("nt")
    assert str(refused.value) == _platform.REFUSAL
    assert refused.value.error_code == "PLATFORM_UNSUPPORTED"
    assert isinstance(refused.value, ImportError)


@pytest.mark.parametrize("relative", ["__init__.py", "_filesystem.py"])
def test_package_modules_refuse_before_their_first_effect(relative):
    tree = ast.parse((SRC_ROOT / "evidence_wiki" / relative).read_text(encoding="utf-8"))
    calls = [index for index, node in enumerate(tree.body) if isinstance(node, ast.Expr)
             and isinstance(node.value, ast.Call)]
    refusal = next(index for index in calls if ast.unparse(tree.body[index]).endswith("require_supported(_os.name)"))
    assert refusal == calls[0]
    for node in tree.body[:refusal]:
        assert isinstance(node, (ast.Expr, ast.Import, ast.ImportFrom)), ast.unparse(node)
        if isinstance(node, ast.ImportFrom) and node.level:
            assert node.module == "_platform"


def test_script_refusal_exits_with_the_shared_code(script_errors, capsys):
    assert script_errors.refuse_unsupported_platform("posix") is None
    with pytest.raises(SystemExit) as refused:
        script_errors.refuse_unsupported_platform("nt")
    assert refused.value.code == script_errors.PLATFORM_REFUSAL == _platform.REFUSAL
    assert script_errors.classify_error_code(refused.value.code) == "PLATFORM_UNSUPPORTED"
    assert script_errors.default_recoverable("PLATFORM_UNSUPPORTED") is False
    assert "Linux or macOS" in script_errors.remediation_for("PLATFORM_UNSUPPORTED")
    assert script_errors.handle_system_exit(refused.value, json_mode=True, default_exit_code=2) == 2
    envelope = json.loads(capsys.readouterr().err)
    assert (envelope["error_code"], envelope["recoverable"]) == ("PLATFORM_UNSUPPORTED", False)


def test_every_refusing_module_refuses_first_with_the_shared_line():
    stems, trees = _stems(), _trees()
    refusing = {stem for stem, tree in trees.items()
                if any(_is_platform_refusal(node, _platform.REFUSAL) for node in tree.body)}
    assert refusing == REFUSING_MODULES
    for stem in sorted(refusing):
        body = trees[stem].body
        index = next(index for index, node in enumerate(body) if _is_platform_refusal(node, _platform.REFUSAL))
        earlier = [effect for node in body[:index] for effect in _effects(node)]
        assert not earlier, (stem, earlier)
        assert not set().union(*(_statement_imports(node, stems) for node in body[:index])), stem


def test_every_script_entry_point_reaches_a_platform_refusal_before_any_effect():
    stems, trees = _stems(), _trees()
    graph = {stem: set().union(*(_statement_imports(node, stems) for node in tree.body))
             for stem, tree in trees.items()}
    entry_points = sorted(stem for stem, tree in trees.items() if any(
        isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__" for node in tree.body))
    assert len(entry_points) >= 39
    for stem in entry_points:
        before = []
        for node in trees[stem].body:
            reached = set().union(*(_closure(target, graph) for target in _statement_imports(node, stems)))
            if reached & REFUSING_MODULES or _is_platform_refusal(node, _platform.REFUSAL):
                break
            before.extend(_effects(node))
        else:
            pytest.fail(f"{stem} reaches no platform refusal through its top-level imports")
        assert not before, (stem, before)
