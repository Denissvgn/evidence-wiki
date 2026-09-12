"""Isolated suite shards retain complete execution and reject incomplete evidence."""

import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from tools.run_test_groups import main as run_groups
from tools.verify_test_shards import main as verify_main
from tools.verify_test_shards import verify

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def completed_shards(tmp_path_factory):
    root = tmp_path_factory.mktemp("sharded-repo")
    evidence = tmp_path_factory.mktemp("sharded-evidence")
    (root / "tools").mkdir()
    (root / "tests").mkdir()
    shutil.copyfile(ROOT / "tools/_suite_plugin.py", root / "tools/_suite_plugin.py")
    for module in ("test_script_loader.py", "test_script_host.py", "test_extra.py"):
        (root / "tests" / module).write_text(
            'import json, os\nfrom pathlib import Path\n'
            'def record(name):\n'
            '    with (Path(__file__).parents[1] / "executions.jsonl").open("a") as stream:\n'
            '        stream.write(json.dumps([Path(__file__).name, name, os.getpid()]) + "\\n")\n'
            'def test_first():\n    record("first")\n'
            'def test_second():\n    record("second")\n', encoding="utf-8")
    git = shutil.which("git")
    assert git is not None
    subprocess.run([git, "init", "--quiet", str(root)], check=True)
    subprocess.run([git, "-C", str(root), "-c", "user.name=Suite test", "-c", "user.email=suite@example.invalid",
                    "-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", "Fixture"], check=True)
    commit = subprocess.check_output([git, "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("GITHUB_RUN_ID", "1234")
        patch.setenv("RUNNER_OS", "Linux")
        patch.setenv("RUNNER_ARCH", "X64")
        patch.delenv("COVERAGE_PROCESS_START", raising=False)
        patch.delenv("COVERAGE_FILE", raising=False)
        for index in (1, 2, 3):
            assert run_groups(["--root", str(root), "--output", str(evidence / str(index)),
                               "--group-size", "1", "--shard-count", "3", "--shard-index", str(index)]) == 0
    reports = [json.loads(path.read_text()) for path in sorted(evidence.glob("*/manifest.json"))]
    version = ".".join(reports[0]["python"].split()[0].split(".")[:2])
    options = {"commit": commit, "run_id": "1234", "platforms": [f"Linux/X64/{version}"],
               "shard_count": 3, "group_size": 1}
    return root, evidence, options, reports


def test_real_shards_run_every_test_in_fresh_groups_and_both_combined_orders(completed_shards):
    root, evidence, options, reports = completed_shards
    summary = verify(root, evidence, **options)
    assert list(summary.values()) == [{"collected": 6, "groups": 3, "shards": 3, "combined_orders": 2}]
    assert [[row["label"] for row in report["groups"]] for report in reports] == [
        ["group-001"], ["group-002"], ["group-003"]]
    assert [len(report["combined_orders"]) for report in reports] == [2, 0, 0]
    events = [json.loads(line) for line in (root / "executions.jsonl").read_text().splitlines()]
    assert Counter((module, name) for module, name, _ in events) == {
        (module, name): (1 if module == "test_extra.py" else 3)
        for module in ("test_extra.py", "test_script_host.py", "test_script_loader.py")
        for name in ("first", "second")}
    assert sorted(Counter(pid for _, _, pid in events).values()) == [2, 2, 2, 4, 4]
    for report in reports:
        for row in [report["collection"], *report["groups"], *report["combined_orders"]]:
            assert Path(row["log"]).is_file() and Path(row["record"]).is_file()


@pytest.mark.parametrize("damage", [
    "missing_shard", "duplicate_shard", "extra_platform", "failed", "interrupted", "wrong_commit", "wrong_run",
    "changed_sources", "source_hash", "wrong_shard_count", "wrong_group_size", "partial_targets",
    "collection_differs", "duplicate_collection", "missing_group", "duplicate_group", "wrong_group",
    "missing_execution", "failed_process", "failed_result", "incomplete_execution",
    "missing_order", "extra_order", "wrong_order", "malformed",
])
def test_verifier_refuses_incomplete_or_mixed_results(completed_shards, tmp_path, damage):
    root, _, options, originals = completed_shards
    reports = json.loads(json.dumps(originals))
    report = reports[0]
    if damage == "missing_shard":
        reports.pop()
    elif damage == "duplicate_shard":
        reports.append(reports[0])
    elif damage == "extra_platform":
        reports[0]["runner"]["RUNNER_OS"] = "Unexpected"
    elif damage in ("failed", "interrupted"):
        report["status"] = damage
    elif damage == "wrong_commit":
        report["commit"] = "different"
    elif damage == "wrong_run":
        report["runner"]["GITHUB_RUN_ID"] = "different"
    elif damage == "changed_sources":
        report["sources_unchanged"] = False
    elif damage == "source_hash":
        report["source_sha256"]["tools/_suite_plugin.py"] = "different"
    elif damage == "wrong_shard_count":
        report["shard"]["count"] = 4
    elif damage == "wrong_group_size":
        report["group_size"] = 160
    elif damage == "partial_targets":
        report["targets"] = ["tests/test_extra.py"]
    elif damage == "collection_differs":
        reports[1]["collection"]["result"]["collected"].pop()
    elif damage == "duplicate_collection":
        report["collection"]["result"]["collected"].append(report["collection"]["result"]["collected"][0])
    elif damage == "missing_group":
        report["groups"].clear()
    elif damage == "duplicate_group":
        report["groups"].append(report["groups"][0])
    elif damage == "wrong_group":
        report["groups"] = reports[1]["groups"]
    elif damage == "missing_execution":
        report["groups"][0]["result"]["executed"].pop()
    elif damage == "failed_process":
        report["groups"][0]["exit_code"] = 1
    elif damage == "failed_result":
        report["groups"][0]["result"]["exit_code"] = 1
    elif damage == "incomplete_execution":
        report["groups"][0]["execution_complete"] = False
    elif damage == "missing_order":
        report["combined_orders"].pop()
    elif damage == "extra_order":
        reports[1]["combined_orders"] = report["combined_orders"]
    elif damage == "wrong_order":
        report["combined_orders"].reverse()
    elif damage == "malformed":
        report["groups"] = None
    for index, item in enumerate(reports):
        directory = tmp_path / str(index)
        directory.mkdir()
        (directory / "manifest.json").write_text(json.dumps(item))
    args = ["--root", str(root), "--evidence", str(tmp_path), "--commit", options["commit"],
            "--run-id", options["run_id"], "--platform", options["platforms"][0],
            "--shard-count", "3", "--group-size", "1"]
    assert verify_main(args) == 1


def test_verifier_requires_every_expected_platform(completed_shards):
    root, evidence, options, _ = completed_shards
    with pytest.raises(ValueError, match="missing shards"):
        verify(root, evidence, **dict(options, platforms=[*options["platforms"], "Windows/X64/3.12"]))


def test_verifier_checks_live_sources(completed_shards, tmp_path):
    root, evidence, options, _ = completed_shards
    changed = tmp_path / "changed"
    shutil.copytree(root, changed, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    (changed / "tests/test_extra.py").write_text("def test_changed():\n    pass\n")
    with pytest.raises(ValueError, match="identity differs"):
        verify(changed, evidence, **options)


@pytest.mark.parametrize("count,index", [(0, 1), (3, 0), (3, 4), (-1, 1)])
def test_invalid_shard_arguments_do_not_start_collection(tmp_path, count, index):
    output = tmp_path / "evidence"
    with pytest.raises(SystemExit) as refusal:
        run_groups(["--output", str(output), "--shard-count", str(count), "--shard-index", str(index)])
    assert refusal.value.code == 2 and not output.exists()


def test_too_many_shards_fails_instead_of_claiming_an_empty_pass(completed_shards, tmp_path):
    root, _, _, _ = completed_shards
    output = tmp_path / "evidence"
    assert run_groups(["--root", str(root), "--output", str(output), "--group-size", "1",
                       "--shard-count", "4"]) == 1
    report = json.loads((output / "manifest.json").read_text())
    assert report["status"] == "failed" and report["groups"] == []
    assert "exceeds" in report["error"]
