"""Native Windows is refused at every entry point before any effect.

The wheel installs on any platform, so the refusal is a runtime check. The package
refuses on import. Workspace scripts run without the package, so each script entry
point refuses before its first sibling import: importing a sibling would already
write that module's bytecode into the workspace. Shared modules refuse too, for
callers that import them directly. Faking ``os.name`` in-process breaks
``pathlib``, so in-process checks pass the platform name, and entry points run in
a subprocess that fakes ``os.name`` before the script starts.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evidence_wiki import _platform
from evidence_wiki._script_host import load_packaged_script, shared_assets_root

TEMPLATE = REPO_ROOT / "workspace-template"
SCRIPTS = TEMPLATE / "scripts"
# The shared modules that refuse at import. Pinned, so removing one refusal fails
# even where an entry point's own refusal still runs first.
SHARED_REFUSING_MODULES = {"_script_errors", "_workspace_module_loader", "_native_fs", "_workspace_locks"}
# Runs a script as ``python <script>`` would, after faking a native Windows host.
FAKE_WINDOWS = (
    "import os, runpy, sys\n"
    "script = sys.argv[1]\n"
    "sys.argv = sys.argv[1:]\n"
    "sys.path[0] = os.path.dirname(script)\n"
    "os.name = 'nt'\n"
    "runpy.run_path(script, run_name='__main__')\n"
)
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


def _entry_points(trees):
    return sorted(stem for stem, tree in trees.items() if any(
        isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__" for node in tree.body))


def _precedes_entry_refusal(node):
    """Only the docstring, ``__future__`` imports and ``import os`` may run before an entry point refuses."""
    if isinstance(node, ast.Expr):
        return isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    if isinstance(node, ast.ImportFrom):
        return node.module == "__future__"
    return isinstance(node, ast.Import) and [(alias.name, alias.asname) for alias in node.names] == [("os", None)]


def _state(root):
    return sorted((path.relative_to(root).as_posix(), path.is_dir(), path.stat().st_size, path.stat().st_mtime_ns)
                  for path in root.rglob("*"))


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


def test_every_shared_refusing_module_refuses_first_with_the_shared_line():
    stems, trees = _stems(), _trees()
    entry_points = set(_entry_points(trees))
    refusing = {stem for stem, tree in trees.items() if stem not in entry_points
                and any(_is_platform_refusal(node, _platform.REFUSAL) for node in tree.body)}
    assert refusing == SHARED_REFUSING_MODULES
    for stem in sorted(refusing):
        body = trees[stem].body
        index = next(index for index, node in enumerate(body) if _is_platform_refusal(node, _platform.REFUSAL))
        earlier = [effect for node in body[:index] for effect in _effects(node)]
        assert not earlier, (stem, earlier)
        assert not set().union(*(_statement_imports(node, stems) for node in body[:index])), stem


def test_every_script_entry_point_refuses_before_its_first_sibling_import():
    trees = _trees()
    entry_points = _entry_points(trees)
    assert len(entry_points) >= 39
    for stem in entry_points:
        body = trees[stem].body
        refusals = [index for index, node in enumerate(body) if _is_platform_refusal(node, _platform.REFUSAL)]
        assert len(refusals) == 1, stem
        assert all(_precedes_entry_refusal(node) for node in body[:refusals[0]]), stem


def test_refused_entry_points_leave_the_workspace_unchanged_with_bytecode_writing_on(tmp_path):
    workspace = tmp_path / "workspace"
    shutil.copytree(TEMPLATE, workspace, ignore=shutil.ignore_patterns("__pycache__"))
    entry_points = _entry_points(_trees())
    assert len(entry_points) >= 39
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX"} and not key.startswith("COVERAGE_")}
    before = _state(workspace)
    for stem in entry_points:
        script = workspace / "scripts" / f"{stem}.py"
        done = subprocess.run([sys.executable, "-c", FAKE_WINDOWS, str(script), "--project-root", str(workspace)],
                              cwd=workspace, env=env, capture_output=True, text=True, timeout=60, check=False)
        assert done.returncode == 1, (stem, done.returncode, done.stderr[-400:])
        assert done.stderr.strip().splitlines()[-1] == _platform.REFUSAL, (stem, done.stderr[-400:])
        assert _state(workspace) == before, stem
