"""Private native review fixtures with actual subprocess cleanup."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from evidence_wiki._process_groups import ProcessGroupOwnership
from tools._integration_owner_pass import _group_alive


def fixture_document():
    """Construct inert synthetic inputs; native provisioning owns realized IDs."""
    questions = [
        {"slug": "capacity", "original_id": "capacity", "question": "Does retained capacity cover retained demand?"},
        {
            "slug": "demand",
            "original_id": "demand",
            "question": "What demand and independent service note were retained?",
        },
    ]

    def evidence(source, quote):
        return {
            "source_id": source,
            "record_sha256": "sha256:" + "0" * 64,
            "observed_at": "2026-10-12T12:00:00Z",
            "capture": "primary",
            "quote": quote,
            "location_hint": None,
            "anchor": None,
        }

    claims = []
    for identifier, slug, text, premises in (
        ("observed-capacity", "capacity", "Retained capacity is six units.", []),
        ("effective-capacity", "capacity", "Effective capacity equals retained capacity.", ["observed-capacity"]),
        ("observed-demand", "demand", "Retained demand is four units.", []),
        (
            "adequacy",
            "capacity",
            "Effective capacity covers retained demand.",
            ["effective-capacity", "observed-demand"],
        ),
        ("margin", "capacity", "The retained margin is two units.", ["effective-capacity", "observed-demand"]),
        ("independent-note", "demand", "The service note says scheduled maintenance.", []),
    ):
        source = "demand" if identifier == "observed-demand" else "shared"
        quote = (
            "Demand: 4 units."
            if source == "demand"
            else ("Service note: scheduled maintenance." if identifier == "independent-note" else "Capacity: 6 units.")
        )
        values = [evidence(source, quote)]
        calculations = []
        if identifier == "margin":
            values.append(evidence("demand", "Demand: 4 units."))
            calculations.append(
                {
                    "result_id": "sha256:" + "0" * 64,
                    "pointer": "/graphs/capacity/outputs/margin",
                    "expected": "2",
                    "form": "value",
                    "unit": "units",
                    "rounded": False,
                }
            )
        claims.append(
            {
                "id": identifier,
                "question_slug": slug,
                "qualification": "inference" if premises else "supported",
                "text": text,
                "scope": "These retained synthetic records only.",
                "time": "Captured observation only.",
                "units": "units",
                "evidence": values,
                "premises": premises,
                "derivation": "Use retained values." if premises else None,
                "limitations": ["Deterministic synthetic mechanics only."],
                "calculations": calculations,
            }
        )
    policy = {
        "schema_version": "evidence-strict-policy/v1",
        "policy_id": "retained-records",
        "revision": "1",
        "assurance": "artifact_checked",
        "claims_path": "docs/research-claims.json",
        "instructions": {"AGENTS.md": "sha256:" + "0" * 64},
        "rubric": {
            "id": "retained-records",
            "revision": "1",
            "criteria": dict.fromkeys(
                ("support", "source_suitability", "scope", "time", "units", "counterevidence"),
                "Assess retained material.",
            ),
        },
        "human_review": False,
        "max_source_age_seconds": 31536000,
        "max_review_age_seconds": 86400,
    }
    sources = {
        name: {"raw": body, "normalized": body, "provenance": {}, "ancestry": []}
        for name, body in (
            ("shared", "Capacity: 6 units. Service note: scheduled maintenance.\n"),
            ("demand", "Demand: 4 units.\n"),
            ("unused", "Independent archived note.\n"),
        )
    }
    return {
        "inventory": {"schema_version": "evidence-strict-claims/v2", "questions": questions, "claims": claims},
        "source_material": sources,
        "global_inputs": {"policy": policy, "unclassified": {"notes/context.txt": "Retained observer context.\n"}},
        "question_context": {
            q["slug"]: {
                "question": q["question"],
                "metadata": {
                    "owner": "analyst",
                    "scope": "retained capacity" if q["slug"] == "capacity" else "retained demand and service note",
                },
                "body": "Compare retained capacity against retained demand.\n"
                if q["slug"] == "capacity"
                else "Report demand and the independent service note.\n",
            }
            for q in questions
        },
    }


def execute(directory, *, mode="selective_reuse", cases=None, refresh=False):
    """An isolated child owns every native worker and its closed launch guard."""
    directory = Path(directory)
    directory.mkdir(mode=0o700)
    supplied = directory / "fixture.json"
    supplied.write_text(json.dumps(fixture_document(), allow_nan=False))
    output = directory / "native"
    command = [
        sys.executable,
        "-m",
        "tools._review_dependency_cases",
        "--fixture",
        str(supplied),
        "--output",
        str(output),
        "--mode",
        mode,
    ]
    for case_id in cases or ():
        command.extend(["--case", case_id])
    if refresh:
        command.append("--refresh")
    with (directory / "stdout.log").open("wb") as stdout, (directory / "stderr.log").open("wb") as stderr:
        child = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[1],
            stdout=stdout,
            stderr=stderr,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        guard = ProcessGroupOwnership(child, _group_alive)
        try:
            from tools._review_dependency_case_contracts import INITIAL_CASES
            from tools._review_dependency_cases import case_watchdog
            selected = cases or INITIAL_CASES
            budgets = [case_watchdog(selected[offset:offset + 8])["watchdog_seconds"]
                for offset in range(0, len(selected), 8)]
            child.wait(timeout=sum(budgets) + 660)
            assert guard.confirm_completed(), "Native acceptance process group remained active."
        finally:
            if child.returncode is None or not guard.confirm_completed():
                guard.stop()
                child.wait(timeout=5)
                assert guard.confirm_completed(), "Native acceptance cleanup was not confirmed."
    report_path = output / "report.json"
    assert report_path.is_file(), "Native acceptance report missing; inspect retained process logs."
    report = json.loads(report_path.read_bytes())
    assert child.returncode == 0, (child.returncode, report_path, report["status"])
    assert report["cleanup"]["status"] == "confirmed"
    assert all(row["group_absent"] and row["leader_reaped"] for row in report["cleanup"]["children"])
    for cleanup in report["cleanup"].get("shards", []):
        assert cleanup["status"] == "confirmed"
        assert all(row["group_absent"] and row["leader_reaped"] for row in cleanup["children"])
    assert report["qualification"] is False
    return report


def require_storage():
    from tests._usage_fixture import require_host_storage

    require_host_storage()
    if os.open not in os.supports_dir_fd:
        raise RuntimeError("Native no-follow descriptor storage is required.")


def case_evidence_report(selected=("unchanged-baseline",)):
    """Build inert byte-evidence inputs without launching any native owner."""
    from tools._review_dependency_case_contracts import case_manifest, case_plan
    from tools._review_dependency_cases import case_watchdog
    from tools._review_dependency_workload import digest
    rows = case_manifest()
    for row in rows:
        if row["case_id"] in selected:
            row.update(status="passed", assertions=["inert_fixture_assertion"])
    source = {"candidate_sha256": "0" * 64, "worker_sha256": "1" * 64,
        "case_manifest_sha256": digest(case_manifest()), "case_plan_sha256": digest(case_plan())}
    watchdog = case_watchdog(selected)
    if len(selected) > 8:
        shards = [case_watchdog(selected[offset:offset + 8]) for offset in range(0, len(selected), 8)]
        watchdog.update(fixture_reservations=sum(shard["fixture_reservations"] for shard in shards),
            watchdog_seconds=sum(shard["watchdog_seconds"] for shard in shards) + 600,
            shard_count=len(shards))
    return {"mode": "selective_reuse", "fixture_sha256": "2" * 64, "selected_case_ids": list(selected),
        "status": "completed", "cases": rows, "originals": [],
        "cleanup": {"status": "confirmed", "children": [], "plan_sha256": "3" * 64},
        "watchdog": watchdog}, source
