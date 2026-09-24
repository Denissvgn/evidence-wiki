"""Cold-source handoff and strict public refusal regressions."""

import json
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from evidence_wiki._filesystem import os
from evidence_wiki._pack_io import canonical
from evidence_wiki.pack_discovery import owner
from evidence_wiki.planning import compile_plan
from evidence_wiki.setup_application import apply_plan
from tests.test_strict_evidence import host as host
from tests.test_strict_evidence import review
from tests.test_workspace_application import in_process as in_process
from tests.test_workspace_application import local_request
from tools import validate_installed_artifacts as artifacts
from tools._journey_cases import load_cases
from tools.freeze_agent_trials import freeze


@pytest.mark.skipif(os.open not in os.supports_dir_fd, reason="Local setup requires anchored filesystem operations")
def test_local_capture_timestamp_is_observed_and_stable_on_replay(tmp_path, in_process):
    before = datetime.now(timezone.utc).replace(microsecond=0)
    plan = compile_plan(canonical(local_request(tmp_path)))
    apply_plan(canonical(plan))
    sidecar = next((tmp_path / "workspace/raw").rglob("*.provenance.yml"))
    original = sidecar.read_bytes()
    value = yaml.safe_load(original)
    assert before <= datetime.fromisoformat(value["retrieved_at"]) <= datetime.now(timezone.utc)
    assert value["retrieved_by"] == "local_setup" and value["source_type"] == "local_file"
    assert "publication_date" not in value and "license" not in value
    apply_plan(canonical(plan))
    assert sidecar.read_bytes() == original


@pytest.mark.parametrize("relative,data,accepted", [
    ("raw/.locks/acquisition.lock", b"", True),
    ("raw/.locks/acquisition.lock", b"unapproved", False),
    ("raw/.locks/other.lock", b"", False),
    ("raw/web/unapproved.html", b"unapproved", False),
])
def test_only_exact_empty_coordination_inode_is_not_raw_evidence(host, relative, data, accepted):
    path = host.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    core = owner("_strict_evidence")
    if accepted:
        assert core.prepare_review(host.root, "product-source")["registration"]
    else:
        with pytest.raises(Exception) as caught:
            core.prepare_review(host.root, "product-source")
        assert caught.value.details["reason"] == "assessment_unapproved_raw_input"


def test_copied_resolver_refuses_unreviewed_claim_as_json(host):
    path = host.root / "wiki/questions/vendor-product-spec.md"
    path.write_text(path.read_text().replace("status: answered", "status: open"))
    owner("question_claim").run_claim(host.root, slug="vendor-product-spec", agent_id="caller")
    result = subprocess.run([sys.executable, "-B", str(host.root / "scripts/question_resolve.py"), "--project-root", str(host.root),
        "answer", "--slug", "vendor-product-spec", "--agent-id", "caller", "--answer-page", "wiki/synthesis/vendor-product-answer.md",
        "--source-id", "web:vendor-official-product-spec", "--format", "json"], text=True, capture_output=True, timeout=60)
    assert result.returncode == 2 and "Traceback" not in result.stderr
    payload = json.loads(result.stderr or result.stdout)
    assert payload["error_code"] == "STRICT_EVIDENCE_REFUSED"
    assert payload["details"]["reason"] == "strict_claim_review_required"


@pytest.mark.parametrize("text,blocked", [
    ("Annual maintenance costs are absent from the supplied data.", False),
    ("Scheduled maintenance costs are part of this operating budget.", False),
    ("Service temporarily unavailable. Please try again later.", True),
    ("The website is down for maintenance. Please try again later.", True),
])
def test_html_unavailability_is_distinct_from_research_subject(text, blocked):
    reasons = owner("normalize_sources").html_unusable_evidence_reasons("Retained observations", text, "<p>" + text + "</p>")
    assert ("html_error_page:official_error_page" in reasons) is blocked


def test_local_html_is_not_labeled_as_web_acquisition():
    lint = owner("lint")
    local = {"retrieved_by": "local_setup", "source_type": "local_file"}
    assert not lint.is_web_curation_record({"kind": "html"}, local)
    assert lint.is_web_curation_record({"kind": "html"}, {"retrieved_by": "web-capture", "source_type": "local_file"})
    assert lint.is_web_curation_record({"kind": "html"}, {"retrieved_by": "local_setup"})


def test_strict_export_reports_owner_blocker_category(host):
    # A valid strict review cannot replace the independent publication lifecycle.
    path = host.root / "wiki/questions/vendor-product-spec.md"
    path.write_text(path.read_text().replace("status: answered", "status: open"))
    review(host)
    result = owner("_strict_evidence").publication(host.root)
    assert not result["questions"][0]["accepted"]
    assert "publication_source_quality" in result["questions"][0]["gaps"]


def test_installed_process_ignores_inherited_pythonpath(tmp_path, monkeypatch):
    (tmp_path / "sitecustomize.py").write_text("raise RuntimeError('checkout poison')")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    out = artifacts.run([sys.executable, "-c", "import os; print('PYTHONPATH' in os.environ)"])
    assert out.strip() == "False"


def test_isolated_capsule_contains_no_source_package_and_binds_inputs(tmp_path):
    capsule = artifacts.isolated_fixtures(tmp_path)
    manifest = json.loads((capsule / "inputs.json").read_text())
    assert not (capsule / "src").exists() and not (capsule / "evidence_wiki").exists()
    assert all(artifacts.sha256_of(capsule / name) == sha for name, sha in manifest.items())
    assert "tools/qualify_journeys.py" in manifest
    assert load_cases(capsule / "tests/fixtures/onboarding-journeys/cases.json")["trials"] == 3


def test_journey_ids_cannot_escape_report_directory(tmp_path):
    source = Path(__file__).parent / "fixtures/onboarding-journeys/cases.json"
    value = json.loads(source.read_text())
    value["cases"][0]["id"] = "../../outside"
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="case_shape_invalid"):
        load_cases(path)


def test_distribution_failure_keeps_incomplete_evidence(tmp_path):
    output = tmp_path / "observed"
    with pytest.raises(artifacts.ValidationError):
        artifacts.main(["--dist-dir", str(tmp_path / "absent"), "--evidence-dir", str(output)])
    result = json.loads((output / "summary.json").read_text())
    assert result["status"] == "failed" and result["error_type"] == "ValidationError"


def test_retained_distribution_evidence_does_not_move_execution_into_checkout(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    repo.mkdir()
    output = repo / "reports/installed"
    monkeypatch.setattr(artifacts, "REPO_ROOT", repo)
    scratch = tmp_path / "execution"
    scratch.mkdir()
    monkeypatch.setattr(artifacts.tempfile, "mkdtemp", lambda **_: str(scratch))
    def observe(args, summary, selected):
        assert selected == scratch and not selected.is_relative_to(repo)
        path = selected / "wheel/journeys/result.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"outcome":"observed"}')
        summary["status"] = "passed"
    monkeypatch.setattr(artifacts, "validate_distributions", observe)
    assert artifacts.main(["--dist-dir", str(tmp_path), "--evidence-dir", str(output)]) == 0
    report = json.loads((output / "summary.json").read_text())
    assert report["execution_root"] == str(scratch)
    assert (output / "wheel/journeys/result.json").read_bytes() == (scratch / "wheel/journeys/result.json").read_bytes()


@pytest.mark.parametrize("namespace", ["strict", "computation"])
@pytest.mark.parametrize("arguments", [[], ["unknown-operation"], ["check", "--private-key=must-not-echo"]])
def test_closed_cli_arguments_refuse_as_one_content_free_json_document(namespace, arguments):
    result = subprocess.run([sys.executable, "-B", "-m", "evidence_wiki.cli", namespace, *arguments],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 2 and not result.stderr
    value = json.loads(result.stdout)
    assert value["error_code"] == ("STRICT_EVIDENCE_REFUSED" if namespace == "strict" else "COMPUTATION_REFUSED")
    assert value["details"]["reason"] == namespace + "_arguments_invalid"
    assert "must-not-echo" not in result.stdout


def test_prepared_agent_prompts_never_receive_live_pass_credit(tmp_path):
    from evidence_wiki import __version__

    wheel = tmp_path / "candidate.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("evidence_wiki.dist-info/METADATA", "Name: evidence-wiki\nVersion: " + __version__ + "\n")
    output = tmp_path / "prepared"
    result = freeze(output, package=wheel, python=sys.executable, capabilities=["Local terminal; no production credentials"], work_root=tmp_path / "workspaces")
    report = json.loads(Path(result["path"]).read_text())
    assert result["trials"] == 99 and report["status"] == "awaiting_live_execution"
    assert report["publication_ready"] is False and report["semantic_metrics"] is None
    assert {row["framework"] for row in report["trials"]} == {"pi", "opencode", "gemini"}
    assert all(row["provider"] is None and row["operator_setting_explanations"] is None for row in report["trials"])
    for row in report["trials"]:
        prompt = (output / row["prompt"]).read_text()
        assert not Path(row["work"]).is_relative_to(output)
        assert not (Path(row["work"]) / "frozen-suite.json").exists()
        assert "expected_outcomes" not in prompt and "_journey_driver" not in prompt
        assert "private_rubric" not in prompt and "Discover its installed instructions" in prompt


@pytest.mark.parametrize("placement", ["checkout", "reports", "overlap"])
def test_novice_workspace_refuses_private_instruction_ancestry(tmp_path, placement):
    from tools.freeze_agent_trials import ROOT

    output = tmp_path / "reports"
    work = ROOT / "reports/uncreated-trial" if placement == "checkout" else output / "work" if placement == "reports" else tmp_path
    with pytest.raises(ValueError, match="outside_checkout_and_private_reports"):
        freeze(output, package=tmp_path / "absent.whl", python=sys.executable, capabilities=[], work_root=work)
    assert not output.exists()
