"""Public HTML subprocess journeys keep extraction, currentness and admission separate."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_copied_scripts_and_public_cli_preserve_html_qualifications(tmp_path):
    cli = Path(sys.executable).with_name("evidence-wiki.exe" if os.name == "nt" else "evidence-wiki")
    result = subprocess.run([
        sys.executable, "-I", str(ROOT / "tools/probe_html_usability.py"), "--cli", str(cli),
        "--root", str(tmp_path / "workspace"), "--corpus", str(ROOT / "tests/fixtures/html-usability/pages.json"),
    ], cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=300, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["html_cli_journeys"] == report["cached_refresh"] == report["selected_replay"] == "passed"
    assert len(report["cases"]) == 10
    assert sum(row["usability"] == "not_ready" for row in report["cases"]) == 6
    assert all(row["complete"] and not row["evidence_accepted"] for row in report["cases"])
