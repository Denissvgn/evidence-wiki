"""Small actual pytest processes qualify joins, custody and partial retention."""

import json
import shutil
from pathlib import Path

import pytest

from tools._acceptance_protocol import REQUIRED_CATEGORIES, canonical_json, digest
from tools.measure_integration_costs import _signed
from tools.verify_acceptance_paths import ROOT, read_acceptance_report, run_acceptance

KEY = b"acceptance-host-custody-key-000000"


def candidate(tmp_path, body):
    root = tmp_path / "candidate"
    (root / "src/evidence_wiki").mkdir(parents=True)
    (root / "src/evidence_wiki/__init__.py").write_text("__version__ = 'fixture'\n")
    (root / "tests").mkdir()
    (root / "tools").mkdir()
    for name in ("_acceptance_protocol.py", "_acceptance_pytest.py"):
        shutil.copyfile(ROOT / "tools" / name, root / "tools" / name)
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (root / "tests/test_owner.py").write_text("import evidence_wiki\nimport pytest\n\n" + body)
    return root


def execute(tmp_path, body, *, requirement="owner_receipt", timeout=10):
    root = candidate(tmp_path, body)
    cases = [{"id": "bounded-owner", "categories": sorted(REQUIRED_CATEGORIES), "severity": "critical",
              "evidence_requirement": requirement, "selectors": ["tests/test_owner.py::test_owner"]}]
    return run_acceptance(repo_root=root, cases=cases, evidence_root=tmp_path / "run", timeout_seconds=timeout,
                          observer_key=KEY), root


def test_real_collection_expanded_nodes_receipts_phase_joins_and_external_custody(tmp_path):
    value, root = execute(tmp_path, """@pytest.mark.parametrize('mode', ['a', 'b'])
def test_owner(request, mode):
    result = {'outcome': 'refused', 'authorized': False, 'credential': 'do-not-retain'}
    assert result['authorized'] is False
    request.config.pluginmanager.get_plugin('acceptance-records').record_owner_result(request.node.nodeid, 'fixture.owner', result)
""")
    report = value["report"]
    assert report["grade"]["status"] == "PASS"
    assert report["catalog_scope"] == "declared_fixture"
    assert report["grade"]["denominators"]["exact_nodes"] == 2
    assert len(report["processes"]) == 2
    assert report["unstarted_reservations"] == []
    assert report["manifest"]["inputs"]["candidate"]["tests/test_owner.py"]["sha256"]
    retained = Path(value["report_path"]).parent
    events = [json.loads(line) for line in (retained / "execute.events.jsonl").read_text().splitlines()]
    assert all("credential" not in event.get("result", {}) for event in events)
    assert all(process["receipt"]["meter_id"] is None for process in report["processes"])
    assert all(process["event"]["metrics"]["model_calls"] is None for process in report["processes"])
    assert read_acceptance_report(value["report_path"], observer_key=KEY, expected_report_sha256=value["report_sha256"]) == report
    with pytest.raises(ValueError, match="latest_report"):
        read_acceptance_report(value["report_path"], observer_key=KEY, expected_report_sha256="0" * 64)
    target = retained / "execute.events.jsonl"
    target.chmod(0o600)
    target.write_text(target.read_text() + "{}\n")
    with pytest.raises(ValueError, match="retained_evidence"):
        read_acceptance_report(value["report_path"], observer_key=KEY, expected_report_sha256=value["report_sha256"])
    assert root.is_dir()


def test_seeded_module_blocks_execute_each_module_fixture_once(tmp_path):
    root = candidate(tmp_path, """@pytest.fixture(scope='module')
def module_receipt(tmp_path_factory):
    path = tmp_path_factory.getbasetemp() / 'fixture-count.txt'
    count = int(path.read_text()) + 1 if path.exists() else 1
    path.write_text(str(count))
    return count

def test_first(module_receipt):
    assert module_receipt == 1

def test_last(module_receipt):
    assert module_receipt == 1
""")
    (root / "tests/test_other.py").write_text("def test_other():\n    assert True\n")
    cases = [
        {"id": "first-owner", "categories": sorted(REQUIRED_CATEGORIES), "severity": "critical",
         "evidence_requirement": "assertions", "selectors": ["tests/test_owner.py::test_first"]},
        {"id": "other-owner", "categories": ["replay"], "severity": "critical",
         "evidence_requirement": "assertions", "selectors": ["tests/test_other.py::test_other"]},
        {"id": "last-owner", "categories": ["forged-review"], "severity": "critical",
         "evidence_requirement": "assertions", "selectors": ["tests/test_owner.py::test_last"]},
    ]
    value = run_acceptance(repo_root=root, cases=cases, evidence_root=tmp_path / "run", seed=4,
                           timeout_seconds=10, observer_key=KEY)
    report = value["report"]
    assert report["manifest"]["challenge_priority"] == ["last-owner", "other-owner", "first-owner"]
    assert report["manifest"]["exact_nodeids"] == ["tests/test_owner.py::test_last", "tests/test_owner.py::test_first",
                                                    "tests/test_other.py::test_other"]
    assert report["grade"]["status"] == "PASS" and report["grade"]["denominators"]["finished_nodes"] == 3
    assert (tmp_path / "run/workers/execute/pytest-temp/fixture-count.txt").read_text() == "1"
    assert read_acceptance_report(value["report_path"], observer_key=KEY,
                                  expected_report_sha256=value["report_sha256"]) == report


@pytest.mark.parametrize("body,status", [
    ("def test_owner():\n    assert False\n", "FAIL"),
    ("def test_owner():\n    pass\n", "ABSTAIN"),
    ("@pytest.mark.skip(reason='unavailable')\ndef test_owner():\n    assert True\n", "ABSTAIN"),
    ("@pytest.mark.xfail(reason='known')\ndef test_owner():\n    assert False\n", "ABSTAIN"),
])
def test_native_failure_skip_xfail_and_empty_pass_do_not_become_clean_pass(tmp_path, body, status):
    value, _ = execute(tmp_path, body, requirement="assertions")
    assert value["report"]["grade"]["status"] == status


def test_timeout_retains_real_start_and_partial_attempt_without_retry(tmp_path):
    value, _ = execute(tmp_path, "def test_owner():\n    import time\n    assert True\n    time.sleep(5)\n",
                       requirement="assertions", timeout=1)
    report = value["report"]
    assert report["grade"]["status"] == "ABSTAIN"
    assert len(report["processes"]) == 2
    assert report["processes"][1]["receipt"]["started_at"]
    assert "timeout" in report["processes"][1]["receipt"]["failure"]
    assert report["grade"]["denominators"]["started_nodes"] == 1


def test_expected_refusal_context_and_real_native_artifact_snapshot(tmp_path):
    value, _ = execute(tmp_path, """def test_owner(tmp_path):
    with pytest.raises(ValueError):
        raise ValueError('native refusal')
    (tmp_path / 'native.jsonl').write_text('{"kind":"refused","basis_id":"abc","secret":"no"}\\n')
""", requirement="native_artifact")
    assert value["report"]["grade"]["status"] == "PASS"
    retained = Path(value["report_path"]).parent
    snapshots = list((retained / "artifacts").glob("*.json"))
    assert len(snapshots) == 1
    row = json.loads(snapshots[0].read_text())
    assert row["public"] == [{"basis_id": "abc", "kind": "refused"}]
    assert row["native_sha256"] and row["native_bytes"] > 0


def test_output_limit_retains_failure_and_actual_start(tmp_path):
    value, _ = execute(tmp_path, "def test_owner():\n    import sys\n    assert True\n    sys.stdout.write('x' * 2000000)\n",
                       requirement="assertions")
    assert value["report"]["grade"]["status"] == "ABSTAIN"
    assert value["report"]["processes"][1]["receipt"]["output_overflow"] is True
    assert value["report"]["processes"][1]["receipt"]["started_at"]


def test_candidate_drift_is_fail_even_when_native_assertions_pass(tmp_path):
    value, _ = execute(tmp_path, "def test_owner():\n    from pathlib import Path\n    assert True\n    Path(__file__).write_text('changed candidate')\n",
                       requirement="assertions")
    assert value["report"]["grade"]["status"] == "FAIL"
    assert "input_drift" in value["report"]["grade"]["failures"]


def test_setup_failure_keeps_reservations_and_forbids_reuse(tmp_path, monkeypatch):
    import tools.verify_acceptance_paths as runner

    def refuse(*_args):
        raise OSError("copy unavailable")
    monkeypatch.setattr(runner, "_copy_capsule", refuse)
    value, root = execute(tmp_path, "def test_owner():\n    assert True\n", requirement="assertions")
    assert value["report"]["grade"]["status"] == "ABSTAIN"
    assert value["report"]["unstarted_reservations"] == ["collect", "execute"]
    assert all(row["state"] == "not_reserved" for row in value["report"]["processes"])
    with pytest.raises(FileExistsError):
        run_acceptance(repo_root=root, cases=value["report"]["manifest"]["cases"] if value["report"]["manifest"]
                       else [{"id": "bounded-owner", "categories": sorted(REQUIRED_CATEGORIES), "severity": "critical",
                              "evidence_requirement": "assertions", "selectors": ["tests/test_owner.py::test_owner"]}],
                       evidence_root=tmp_path / "run", observer_key=KEY)


def intercept_execution(monkeypatch, mutate):
    import tools.verify_acceptance_paths as runner

    original = runner.observe_command
    def observe(plan, mode, argv, **options):
        value = original(plan, mode, argv, **options)
        if mode == "execute":
            mutate(Path(options["directory"]), value)
        return value
    monkeypatch.setattr(runner, "observe_command", observe)


def test_actual_process_and_result_exit_codes_must_agree(tmp_path, monkeypatch):
    def substitute_result(directory, _value):
        path = directory / "execute.result.json"
        value = json.loads(path.read_text())
        value["exit_code"] = 1
        path.write_bytes(canonical_json(value))
    intercept_execution(monkeypatch, substitute_result)
    value, _ = execute(tmp_path, "def test_owner():\n    assert True\n", requirement="assertions")
    assert value["report"]["grade"]["status"] == "ABSTAIN"
    assert value["report"]["processes"][1]["receipt"]["exit_code"] == 0


def test_exit_one_with_all_passing_phases_is_incomplete(tmp_path, monkeypatch):
    root = candidate(tmp_path, "def test_owner():\n    assert True\n")
    (root / "tests/conftest.py").write_text("def pytest_sessionfinish(session, exitstatus):\n    session.exitstatus = 1\n")
    def match_actual_exit(directory, observed):
        assert observed["receipt"]["exit_code"] == 1
        path = directory / "execute.result.json"
        result = json.loads(path.read_text())
        result["exit_code"] = 1
        path.write_bytes(canonical_json(result))
    intercept_execution(monkeypatch, match_actual_exit)
    catalog = [{"id": "bounded-owner", "categories": sorted(REQUIRED_CATEGORIES), "severity": "critical",
                "evidence_requirement": "assertions", "selectors": ["tests/test_owner.py::test_owner"]}]
    # A failed collection cannot execute; only force the actual execution exit.
    (root / "tests/conftest.py").write_text("def pytest_sessionfinish(session, exitstatus):\n    if not session.config.option.collectonly:\n        session.exitstatus = 1\n")
    value = run_acceptance(repo_root=root, cases=catalog, evidence_root=tmp_path / "run", observer_key=KEY)
    assert value["report"]["grade"]["status"] == "ABSTAIN"
    assert value["report"]["processes"][1]["receipt"]["exit_code"] == 1


@pytest.mark.parametrize("attack", ["empty-body", "wrong-node", "wrong-native-hash", "wrong-projection", "wrong-pid", "cross-node-ref"])
def test_self_consistent_artifact_hashes_do_not_replace_native_body_bindings(tmp_path, monkeypatch, attack):
    def alter(directory, _value):
        path = directory / "execute.events.jsonl"
        events = [json.loads(line) for line in path.read_text().splitlines()]
        rows = [event for event in events if event["event"] == "artifacts"]
        if attack == "cross-node-ref":
            rows[1]["refs"] = rows[0]["refs"]
        else:
            ref = rows[0]["refs"][0]
            target = directory / "artifacts" / ref["path"]
            body = json.loads(target.read_text())
            if attack == "empty-body":
                body = {}
            elif attack == "wrong-node":
                body["nodeid"] = "tests/test_other.py::test_other"
            elif attack == "wrong-native-hash":
                body["native_sha256"] = "f" * 64
            elif attack == "wrong-projection":
                body["public"][0]["basis_id"] = "substituted"
            else:
                body["capture_pid"] += 1
            target.write_bytes(canonical_json(body))
            ref["sha256"] = digest(body)
        path.write_bytes(b"".join(canonical_json(event) for event in events))
    intercept_execution(monkeypatch, alter)
    value, _ = execute(tmp_path, """@pytest.mark.parametrize('mode', ['a', 'b'])
def test_owner(tmp_path, mode):
    assert mode in ['a', 'b']
    (tmp_path / 'native.jsonl').write_text('{"kind":"refused","basis_id":"abc"}\\n')
""", requirement="native_artifact")
    assert value["report"]["grade"]["status"] == "ABSTAIN"
    assert "artifact_binding_unqualified" in value["report"]["reconstruction_gaps"]


def test_reader_independently_regrades_even_a_resigned_host_grade(tmp_path):
    value, _ = execute(tmp_path, "def test_owner():\n    assert False\n", requirement="assertions")
    path = Path(value["report_path"])
    altered = json.loads(path.read_text())
    altered["grade"]["status"] = "PASS"
    signed = _signed(altered, KEY)
    path.chmod(0o600)
    path.write_bytes(canonical_json(signed))
    with pytest.raises(ValueError, match="independent_regrade"):
        read_acceptance_report(path, observer_key=KEY, expected_report_sha256=digest(signed))


def test_post_start_observer_exception_reconciles_real_started_attempt(tmp_path, monkeypatch):
    import tools.verify_acceptance_paths as runner

    original = runner.observe_command
    def fail_after_stop(*args, **options):
        original(*args, **options)
        raise RuntimeError("post-start interruption without returned receipt")
    monkeypatch.setattr(runner, "observe_command", fail_after_stop)
    value, _ = execute(tmp_path, "def test_owner():\n    assert True\n", requirement="assertions")
    report = value["report"]
    assert report["grade"]["status"] == "ABSTAIN"
    assert report["processes"][0]["start"]["pid"] > 0
    assert report["processes"][0]["state"] == "closed_started"
    assert report["unstarted_reservations"] == ["execute"]


def test_native_artifact_only_case_and_unretained_key_do_not_qualify(tmp_path):
    value, _ = execute(tmp_path, """def test_owner(request):
    assert True
    request.config.pluginmanager.get_plugin('acceptance-records').record_owner_result(request.node.nodeid, 'fixture.owner', {'outcome': 'refused'})
""", requirement="native_artifact")
    assert value["report"]["grade"]["status"] == "ABSTAIN"


def test_cli_requires_external_private_key_before_dispatch(tmp_path, monkeypatch):
    import tools.verify_acceptance_paths as runner

    def no_dispatch(**_kwargs):
        pytest.fail("invalid key dispatched work")
    monkeypatch.setattr(runner, "run_acceptance", no_dispatch)
    evidence = tmp_path / "run"
    with pytest.raises(SystemExit):
        runner.main(["--evidence-dir", str(evidence)])
    key = tmp_path / "key"
    key.write_bytes(KEY)
    key.chmod(0o644)
    with pytest.raises(SystemExit):
        runner.main(["--evidence-dir", str(evidence), "--observer-key", str(key)])
    assert not evidence.exists()


def test_known_legacy_fixture_tuple_captures_private_native_root(tmp_path):
    value, _ = execute(tmp_path, """from types import SimpleNamespace
@pytest.fixture(scope='module')
def accepted_native(tmp_path_factory):
    root = tmp_path_factory.mktemp('native-owner')
    return SimpleNamespace(root=root)
@pytest.fixture
def accepted_session(accepted_native):
    return accepted_native, SimpleNamespace(root=accepted_native.root)
def test_owner(accepted_session):
    selected, session = accepted_session
    assert session.root == selected.root
    (selected.root / 'native.jsonl').write_text('{"kind":"refused","basis_id":"abc"}\\n')
""", requirement="native_artifact")
    assert value["report"]["grade"]["status"] == "PASS"
    artifacts = list((Path(value["report_path"]).parent / "artifacts").glob("*.json"))
    assert len(artifacts) == 1
    assert json.loads(artifacts[0].read_text())["changed_since_setup"] is True


def test_unchanged_shared_journal_cannot_substitute_for_actual_owner_return(tmp_path):
    value, _ = execute(tmp_path, """from types import SimpleNamespace
@pytest.fixture(scope='module')
def accepted_native(tmp_path_factory):
    root = tmp_path_factory.mktemp('native-owner')
    (root / 'native.jsonl').write_text('{"kind":"previously-accepted"}\\n')
    return SimpleNamespace(root=root)
@pytest.fixture
def accepted_session(accepted_native):
    return accepted_native, SimpleNamespace(root=accepted_native.root)
def test_owner(accepted_session):
    selected, session = accepted_session
    assert session.root == selected.root
""", requirement="owner_receipt")
    assert value["report"]["grade"]["status"] == "ABSTAIN"
    artifacts = list((Path(value["report_path"]).parent / "artifacts").glob("*.json"))
    assert json.loads(artifacts[0].read_text())["changed_since_setup"] is False


def test_ephemeral_generated_observer_key_leaves_custody_unqualified(tmp_path):
    root = candidate(tmp_path, "def test_owner():\n    assert True\n")
    catalog = [{"id": "bounded-owner", "categories": sorted(REQUIRED_CATEGORIES), "severity": "critical",
                "evidence_requirement": "assertions", "selectors": ["tests/test_owner.py::test_owner"]}]
    value = run_acceptance(repo_root=root, cases=catalog, evidence_root=tmp_path / "run")
    assert value["report"]["grade"]["status"] == "ABSTAIN"
    assert "observer_key_custody_unqualified" in value["report"]["grade"]["gaps"]
