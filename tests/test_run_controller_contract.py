import json
import re
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_CONTROLLER_DOC = REPO_ROOT / "workspace-template" / "docs" / "run-controller.md"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "run-controller"
RUN_STATE_FIXTURE = FIXTURE_DIR / "run-state.json"
EVENTS_FIXTURE = FIXTURE_DIR / "events.jsonl"

REQUIRED_TOP_LEVEL_FIELDS = {
    "schema_version",
    "run_id",
    "started_at",
    "updated_at",
    "agent_id",
    "handoff",
    "state",
    "state_history",
    "workspace_baseline",
    "academic_provider_request_accounting",
    "question_counts",
    "source_counts",
    "candidate_counts",
    "coverage_counts",
    "budget_state",
    "budget_overrides",
    "failure_records",
    "final_verdict",
}

STATE_NAMES = (
    "initialized",
    "planned",
    "discovering",
    "candidates_ready",
    "fetch_planned",
    "fetching",
    "evidence_ready",
    "answering",
    "verifying",
    "complete",
    "blocked_on_sources",
    "no_ship",
    "failed",
)

FORBIDDEN_DOMAIN_TERMS = (
    "madrid",
    "autonomo",
    "spain",
    "spanish",
    "legal",
    "jurisdiction",
    "tax",
    "iva",
    "irpf",
)

SECRET_KEY_RE = re.compile(r"(token|secret|password|credential|api[_-]?key)", re.IGNORECASE)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def json_scalar_values(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from json_scalar_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from json_scalar_values(child)
    elif value is not None:
        yield str(value)


class RunControllerContractTests(unittest.TestCase):
    def test_document_defines_run_state_paths_schema_fields_and_states(self):
        text = RUN_CONTROLLER_DOC.read_text(encoding="utf-8")

        self.assertIn("runs/<run_id>/run-state.json", text)
        self.assertIn("runs/<run_id>/events.jsonl", text)
        self.assertIn('`schema_version`: `"1.0"`', text)
        for field in sorted(REQUIRED_TOP_LEVEL_FIELDS):
            with self.subTest(field=field):
                self.assertIn(f"`{field}`", text)
        for state_name in STATE_NAMES:
            with self.subTest(state=state_name):
                self.assertIn(f"`{state_name}`", text)
        self.assertIn("provider tokens or secrets", text)

    def test_run_state_fixture_matches_required_shape(self):
        document = load_json(RUN_STATE_FIXTURE)

        self.assertEqual(REQUIRED_TOP_LEVEL_FIELDS, set(document))
        self.assertEqual("1.0", document["schema_version"])
        self.assertEqual("run-2026-06-28T120000Z-sample", document["run_id"])
        self.assertEqual("agent-pm", document["agent_id"])
        self.assertEqual(
            {
                "schema_version": "1.0",
                "ledger_path": (
                    "runs/run-2026-06-28T120000Z-sample/academic-provider-requests.jsonl"
                ),
            },
            document["academic_provider_request_accounting"],
        )

        state = document["state"]
        self.assertEqual({"current", "entered_at", "allowed_next_states", "blocking_reason"}, set(state))
        self.assertIn(state["current"], STATE_NAMES)
        self.assertLessEqual(set(state["allowed_next_states"]), set(STATE_NAMES))

        history = document["state_history"]
        self.assertGreaterEqual(len(history), 2)
        self.assertEqual(history[-1]["to_state"], state["current"])
        for entry in history:
            with self.subTest(entry=entry):
                self.assertIn(entry["to_state"], STATE_NAMES)
                if entry["from_state"] is not None:
                    self.assertIn(entry["from_state"], STATE_NAMES)

        self.assertEqual(
            {"total", "open", "in_progress", "answered", "blocked", "deferred", "rejected", "claimed"},
            set(document["question_counts"]),
        )
        self.assertEqual(
            {"manifest_records", "normalized", "unnormalized", "source_requests_open", "source_requests_fulfilled"},
            set(document["source_counts"]),
        )
        self.assertEqual({"total", "new", "selected", "rejected", "fetched"}, set(document["candidate_counts"]))
        self.assertEqual({"required", "satisfied", "missing", "unknown"}, set(document["coverage_counts"]))
        self.assertEqual(
            {
                "questions_processed_this_run",
                "questions_remaining_this_run",
                "source_requests_opened_this_run",
                "source_requests_remaining_this_run",
                "releases_this_run",
                "releases_remaining_this_run",
                "discovery_results_this_run",
                "discovery_results_remaining_this_run",
                "acquisition_downloads_this_run",
                "acquisition_downloads_remaining_this_run",
                "github_archive_bytes_this_run",
                "github_archive_bytes_remaining_this_run",
                "academic_provider_requests_this_run",
                "academic_provider_requests_remaining_this_run",
                "manual_url_deliveries_this_run",
                "manual_url_deliveries_remaining_this_run",
                "stop_reasons",
                "should_stop",
            },
            set(document["budget_state"]),
        )
        self.assertEqual([], document["failure_records"])
        self.assertIsNone(document["final_verdict"])

    def test_events_fixture_records_state_transitions_as_jsonl(self):
        run_state = load_json(RUN_STATE_FIXTURE)
        lines = [line for line in EVENTS_FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
        events = [json.loads(line) for line in lines]

        self.assertGreaterEqual(len(events), 2)
        self.assertEqual([entry["to_state"] for entry in run_state["state_history"]], [event["to_state"] for event in events])

        for event in events:
            with self.subTest(event=event["event_id"]):
                self.assertEqual("1.0", event["schema_version"])
                self.assertEqual(run_state["run_id"], event["run_id"])
                self.assertEqual("agent-pm", event["agent_id"])
                self.assertEqual("state_transition", event["event_type"])
                self.assertIn(event["to_state"], STATE_NAMES)
                if event["from_state"] is not None:
                    self.assertIn(event["from_state"], STATE_NAMES)
                self.assertIsInstance(event["data"], dict)

    def test_fixtures_are_domain_neutral_and_do_not_contain_secrets(self):
        combined_values: list[str] = []
        combined_values.extend(json_scalar_values(load_json(RUN_STATE_FIXTURE)))
        for line in EVENTS_FIXTURE.read_text(encoding="utf-8").splitlines():
            if line.strip():
                combined_values.extend(json_scalar_values(json.loads(line)))

        findings: list[str] = []
        for value in combined_values:
            lowered = value.lower()
            if SECRET_KEY_RE.search(value):
                findings.append(f"secret-like value: {value}")
            for forbidden in FORBIDDEN_DOMAIN_TERMS:
                if forbidden in lowered:
                    findings.append(f"domain-specific value: {value}")

        self.assertEqual([], findings)
