"""Tests for candidate review and selection commands (E36-T01).

`discover_sources.py candidates {list,select,reject,transition}` is an offline read/write
stage over the durable candidate store `sources/discovery/candidates.jsonl`. It
never contacts a provider: selection only links a candidate to a source request
(or mints one) and records a durable audit event, and rejection records a reason.
Status updates are written atomically under a stable workspace lock, so concurrent
writers serialize instead of clobbering each other.

These tests cover listing/filtering, selection (link + create-request),
rejection, idempotency, status supersession, audit-log entries, error envelopes,
no-network behavior, malformed-store handling, and concurrent-safe writes.
"""

import contextlib
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
DISCOVER_PATH = SCRIPTS / "discover_sources.py"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DISCOVER = load_script_module("discover_sources_candidates_under_test", "discover_sources.py")
LOCKS = load_script_module("discover_sources_locks_under_test", "_workspace_locks.py")


def candidate(
    candidate_id: str,
    *,
    title: str = "acme/tool",
    url: str = "https://github.com/acme/tool",
    source_type: str = "code_repository",
    trust_tier: str = "primary_non_official",
    discovered_at: str = "2026-06-19T12:00:00Z",
    license_value: str | None = "MIT",
    extra: dict | None = None,
) -> dict:
    record = {
        "schema_version": "1.0",
        "candidate_id": candidate_id,
        "request_id": None,
        "seed_source_id": None,
        "discovery_run_id": "disc-1a2b3c4d5e",
        "discovered_at": discovered_at,
        "discovered_by": "discover_sources.py/github",
        "provider": "github",
        "url": url,
        "title": title,
        "source_type": source_type,
        "trust_tier": trust_tier,
        "relevance_score": 0.9,
        "trust_score": 0.7,
        "official_source": None,
        "jurisdiction": None,
        "license": license_value,
        "terms_url": None,
        "rationale": "candidate rationale",
        "recommended_action": "review",
        "network_io_executed": True,
        "reasoning": {
            "matched_query_terms": ["acme/tool"],
            "authority_reason": "a",
            "freshness_reason": "f",
            "scope_reason": "s",
            "risk_flags": ["unknown_officialness"],
        },
    }
    if extra:
        record.update(extra)
    return record


def canonical_candidate(candidate_id: str, state: str, **extra) -> dict:
    state_fields = {
        "lifecycle_schema_version": DISCOVER.CANDIDATE_LIFECYCLE_VERSION,
        "lifecycle_state": state,
        "status": DISCOVER.CANDIDATE_STATE_TO_LEGACY_STATUS[state],
    }
    if state in {"selected", "failed", "fetched"}:
        state_fields.update(
            {
                "selected_for_request_id": "req-1a2b3c4d5e",
                "source_request_id": "req-1a2b3c4d5e",
                "selection_status": "selected",
            }
        )
    if state == "rejected":
        state_fields.update({"rejection_reason": "already rejected", "selection_status": "rejected"})
    if state == "fetched":
        state_fields.update({"fetched_source_id": "paper:existing", "fetch_status": "fetched"})
    if state == "failed":
        state_fields.update({"failure_reason": "earlier failure", "fetch_status": "failed"})
    if state == "superseded":
        state_fields.update(
            {
                "superseded_by_candidate_id": "cand-replacement0",
                "selection_status": "obsolete",
                "fetch_status": "not_fetchable",
            }
        )
    state_fields.update(extra)
    return candidate(candidate_id, extra=state_fields)


class CandidateLifecycleTests(unittest.TestCase):
    def write_workspace(
        self,
        root: Path,
        candidates: list[dict],
        *,
        discovery_enabled: bool = False,
        requests: list[dict] | None = None,
        question_slugs: list[str] | None = None,
    ) -> Path:
        workspace = root / "workspace"
        (workspace / "sources" / "discovery").mkdir(parents=True, exist_ok=True)
        (workspace / "wiki" / "questions").mkdir(parents=True, exist_ok=True)
        config = [
            "project:",
            "  name: candidate-lifecycle-fixture",
            "sources:",
            "  manifest_path: sources/manifest.jsonl",
            "  source_requests_path: sources/source-requests.jsonl",
            "wiki:",
            "  root: wiki",
            "integrations:",
            "  discovery:",
            f"    enabled: {'true' if discovery_enabled else 'false'}",
        ]
        (workspace / "research.yml").write_text("\n".join(config) + "\n", encoding="utf-8")
        store = workspace / "sources" / "discovery" / "candidates.jsonl"
        store.write_text(
            "".join(json.dumps(record) + "\n" for record in candidates), encoding="utf-8"
        )
        if requests:
            (workspace / "sources" / "source-requests.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in requests), encoding="utf-8"
            )
        for slug in question_slugs or []:
            (workspace / "wiki" / "questions" / f"{slug}.md").write_text(
                f"# {slug}\n", encoding="utf-8"
            )
        return workspace

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = DISCOVER.main(argv)
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def store_records(self, workspace: Path) -> list[dict]:
        path = workspace / "sources" / "discovery" / "candidates.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def audit_events(self, workspace: Path) -> list[dict]:
        path = workspace / "sources" / "discovery" / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    # --- list ------------------------------------------------------------

    def test_list_reports_all_candidates_and_counts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                [candidate("cand-aaa1110000"), candidate("cand-bbb2220000")],
            )
            code, stdout, stderr = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "candidates", "list"]
            )
        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertEqual(2, report["count"])
        self.assertEqual({"total": 2, "new": 2, "selected": 0, "rejected": 0, "fetched": 0}, report["counts"])
        self.assertEqual(2, report["state_counts"]["proposed"])
        self.assertFalse(report["network_io_executed"])
        # A candidate with no explicit status is explicitly mapped to proposed,
        # while the coarse legacy status remains new for older consumers.
        self.assertEqual(["cand-aaa1110000", "cand-bbb2220000"], [c["candidate_id"] for c in report["candidates"]])
        self.assertTrue(all(c["lifecycle_state"] == "proposed" for c in report["candidates"]))
        self.assertTrue(all(c["lifecycle_migration"]["review_state_inferred"] is False for c in report["candidates"]))

    def test_list_defaults_legacy_candidate_policy_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                [candidate("cand-paper0000", source_type="paper")],
            )
            code, stdout, stderr = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "candidates", "list"]
            )
        self.assertEqual(0, code, stderr)
        record = json.loads(stdout)["candidates"][0]
        self.assertEqual("academic_method_existence", record["evidence_path"])
        self.assertEqual("academic_indexed", record["source_policy"])
        self.assertEqual("publication_identity", record["freshness_policy"])
        self.assertEqual("citation_id_resolves", record["identity_policy"])
        self.assertIsNone(record["selected_for_request_id"])
        self.assertIsNone(record["selected_at"])
        self.assertIsNone(record["source_request_id"])
        self.assertEqual("needs_manual_review", record["selection_status"])
        self.assertEqual("not_planned", record["fetch_status"])
        self.assertEqual(["academic_method_existence"], record["evidence_areas"])

    def test_list_filters_by_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                [
                    candidate("cand-new0000000"),
                    candidate("cand-sel0000000", extra={"status": "selected", "selected_request_id": "req-1"}),
                    candidate("cand-rej0000000", extra={"status": "rejected", "rejection_reason": "mirror"}),
                ],
            )
            code, stdout, _ = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "candidates", "list", "--status", "new"]
            )
        self.assertEqual(0, code)
        report = json.loads(stdout)
        self.assertEqual(1, report["count"])
        self.assertEqual("cand-new0000000", report["candidates"][0]["candidate_id"])
        self.assertEqual({"total": 3, "new": 1, "selected": 1, "rejected": 1, "fetched": 0}, report["counts"])

    def test_list_filters_by_canonical_lifecycle_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                [
                    canonical_candidate("cand-proposed00", "proposed"),
                    canonical_candidate("cand-reviewed00", "reviewed"),
                ],
            )
            code, stdout, stderr = self.run_cli(
                [
                    "--project-root", str(workspace), "--format", "json",
                    "candidates", "list", "--state", "reviewed",
                ]
            )

        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertEqual(["reviewed"], report["filter_states"])
        self.assertEqual(["cand-reviewed00"], [record["candidate_id"] for record in report["candidates"]])
        self.assertEqual(1, report["state_counts"]["proposed"])
        self.assertEqual(1, report["state_counts"]["reviewed"])

    def test_list_filters_by_request_id_across_candidate_link_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                [
                    candidate("cand-direct0000", extra={"request_id": "req-target"}),
                    candidate("cand-selected00", extra={"status": "selected", "selected_for_request_id": "req-target"}),
                    candidate("cand-source0000", extra={"source_request_id": "req-target"}),
                    candidate("cand-other00000", extra={"request_id": "req-other"}),
                ],
            )
            code, stdout, _ = self.run_cli(
                [
                    "--project-root",
                    str(workspace),
                    "--format",
                    "json",
                    "candidates",
                    "list",
                    "--request-id",
                    "req-target",
                ]
            )
        self.assertEqual(0, code)
        report = json.loads(stdout)
        self.assertEqual("req-target", report["filter_request_id"])
        self.assertEqual(3, report["count"])
        self.assertEqual(
            ["cand-direct0000", "cand-selected00", "cand-source0000"],
            [record["candidate_id"] for record in report["candidates"]],
        )

    def test_list_works_with_no_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), [])
            (workspace / "sources" / "discovery" / "candidates.jsonl").unlink()
            code, stdout, _ = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "candidates", "list"]
            )
        self.assertEqual(0, code)
        self.assertEqual(0, json.loads(stdout)["count"])

    # --- select (link existing request) ----------------------------------

    def test_select_links_existing_request_and_audits(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                [candidate("cand-aaa1110000")],
                requests=[{"request_id": "req-1a2b3c4d5e", "status": "open"}],
            )
            code, stdout, stderr = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "candidates", "select",
                 "--candidate-id", "cand-aaa1110000", "--request-id", "req-1a2b3c4d5e",
                 "--reason", "primary_non_official trust tier fits the repository evidence policy"]
            )
            stored = self.store_records(workspace)
            events = self.audit_events(workspace)
        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertTrue(report["updated"])
        self.assertFalse(report["created_request"])
        self.assertEqual("selected", report["status"])
        self.assertFalse(report["network_io_executed"])
        record = stored[0]
        self.assertEqual("selected", record["status"])
        self.assertEqual("req-1a2b3c4d5e", record["selected_for_request_id"])
        self.assertEqual("req-1a2b3c4d5e", record["selected_request_id"])
        self.assertEqual("req-1a2b3c4d5e", record["source_request_id"])
        self.assertEqual("selected", record["selection_status"])
        self.assertEqual("planned", record["fetch_status"])
        self.assertEqual("discover_sources.py/candidates", record["selected_by"])
        self.assertEqual("primary_non_official trust tier fits the repository evidence policy", record["selection_reason"])
        self.assertRegex(record["selected_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(1, len(events))
        self.assertEqual("select", events[0]["event"])
        self.assertEqual("cand-aaa1110000", events[0]["candidate_id"])
        self.assertEqual("req-1a2b3c4d5e", events[0]["request_id"])
        self.assertEqual("primary_non_official trust tier fits the repository evidence policy", events[0]["reason"])
        self.assertEqual("proposed", events[0]["prior_state"])
        self.assertEqual("selected", events[0]["new_state"])
        self.assertEqual("discover_sources.py/candidates", events[0]["actor"])
        self.assertEqual("disc-1a2b3c4d5e", events[0]["run_id"])

    def test_select_unknown_request_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), [candidate("cand-aaa1110000")])
            code, stdout, stderr = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "candidates", "select",
                 "--candidate-id", "cand-aaa1110000", "--request-id", "req-missing00"]
            )
            stored = self.store_records(workspace)
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual("REQUEST_UNKNOWN", json.loads(stderr)["error_code"])
        # A failed link leaves the candidate untouched (still new).
        self.assertNotIn("status", stored[0])
        self.assertEqual([], self.audit_events(workspace))

    def test_select_unknown_candidate_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                [candidate("cand-aaa1110000")],
                requests=[{"request_id": "req-1a2b3c4d5e", "status": "open"}],
            )
            code, _, stderr = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "candidates", "select",
                 "--candidate-id", "cand-does-not-exist", "--request-id", "req-1a2b3c4d5e"]
            )
        self.assertEqual(2, code)
        self.assertEqual("CANDIDATE_UNKNOWN", json.loads(stderr)["error_code"])

    def test_select_is_idempotent_with_canonical_selected_for_request_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                [
                    candidate(
                        "cand-selected00",
                        extra={
                            "status": "selected",
                            "selected_for_request_id": "req-1a2b3c4d5e",
                            "selected_at": "2026-06-19T12:30:00Z",
                            "selected_by": "discover_sources.py/candidates",
                        },
                    )
                ],
                requests=[{"request_id": "req-1a2b3c4d5e", "status": "open"}],
            )
            code, stdout, stderr = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "candidates", "select",
                 "--candidate-id", "cand-selected00", "--request-id", "req-1a2b3c4d5e"]
            )
            stored = self.store_records(workspace)
            events = self.audit_events(workspace)
        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertFalse(report["updated"])
        self.assertEqual("req-1a2b3c4d5e", stored[0]["selected_for_request_id"])
        self.assertNotIn("selected_request_id", stored[0])
        self.assertEqual([], events)

    def test_select_requires_exactly_one_of_request_or_create(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), [candidate("cand-aaa1110000")])
            # neither
            code, _, stderr = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "candidates", "select",
                 "--candidate-id", "cand-aaa1110000"]
            )
            self.assertEqual(2, code)
            self.assertEqual("VALUE_INVALID", json.loads(stderr)["error_code"])
            # both
            code, _, stderr = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "candidates", "select",
                 "--candidate-id", "cand-aaa1110000", "--request-id", "req-1", "--create-request"]
            )
            self.assertEqual(2, code)
            self.assertEqual("VALUE_INVALID", json.loads(stderr)["error_code"])

    def test_select_is_idempotent_for_same_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                [candidate("cand-aaa1110000")],
                requests=[{"request_id": "req-1a2b3c4d5e", "status": "open"}],
            )
            argv = ["--project-root", str(workspace), "--format", "json", "candidates", "select",
                    "--candidate-id", "cand-aaa1110000", "--request-id", "req-1a2b3c4d5e"]
            first, out1, _ = self.run_cli(argv)
            second, out2, _ = self.run_cli(argv)
            events = self.audit_events(workspace)
        self.assertEqual(0, first)
        self.assertEqual(0, second)
        self.assertTrue(json.loads(out1)["updated"])
        self.assertFalse(json.loads(out2)["updated"], "re-selecting the same request is a no-op")
        # The no-op second run does not append a duplicate audit event.
        self.assertEqual(1, len(events))

    def test_selected_candidate_cannot_be_silently_relinked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                [canonical_candidate("cand-selected00", "selected")],
                requests=[
                    {"request_id": "req-1a2b3c4d5e", "status": "open"},
                    {"request_id": "req-other0000", "status": "open"},
                ],
            )
            before = (workspace / "sources" / "discovery" / "candidates.jsonl").read_bytes()
            code, stdout, stderr = self.run_cli(
                [
                    "--project-root", str(workspace), "--format", "json", "candidates", "select",
                    "--candidate-id", "cand-selected00", "--request-id", "req-other0000",
                ]
            )
            after = (workspace / "sources" / "discovery" / "candidates.jsonl").read_bytes()

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual("CANDIDATE_CORRELATION_CONFLICT", json.loads(stderr)["error_code"])
        self.assertEqual(before, after)

    # --- select (create request) -----------------------------------------

    def test_select_create_request_mints_and_links_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                [candidate("cand-aaa1110000", source_type="code_repository")],
            )
            code, stdout, stderr = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "candidates", "select",
                 "--candidate-id", "cand-aaa1110000", "--create-request", "--priority", "high"]
            )
            request_path = workspace / "sources" / "source-requests.jsonl"
            requests = [json.loads(line) for line in request_path.read_text().splitlines() if line.strip()]
            stored = self.store_records(workspace)
        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertTrue(report["created_request"])
        self.assertTrue(report["updated"])
        self.assertEqual(1, len(requests))
        request = requests[0]
        self.assertEqual("code", request["kind"])  # code_repository -> code
        self.assertEqual("high", request["priority"])
        self.assertEqual("https://github.com/acme/tool", request["query_or_identifier"])
        self.assertEqual("open", request["status"])
        self.assertEqual(request["request_id"], stored[0]["selected_request_id"])

    def test_select_create_request_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), [candidate("cand-aaa1110000")])
            argv = ["--project-root", str(workspace), "--format", "json", "candidates", "select",
                    "--candidate-id", "cand-aaa1110000", "--create-request"]
            _, out1, _ = self.run_cli(argv)
            _, out2, _ = self.run_cli(argv)
            request_path = workspace / "sources" / "source-requests.jsonl"
            requests = [json.loads(line) for line in request_path.read_text().splitlines() if line.strip()]
        # Re-running reuses the open request instead of minting a duplicate.
        self.assertTrue(json.loads(out1)["created_request"])
        self.assertFalse(json.loads(out2)["created_request"])
        self.assertEqual(json.loads(out1)["request_id"], json.loads(out2)["request_id"])
        self.assertEqual(1, len(requests))

    def test_create_request_validates_question_slug(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), [candidate("cand-aaa1110000")])
            code, _, stderr = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "candidates", "select",
                 "--candidate-id", "cand-aaa1110000", "--create-request",
                 "--question-slug", "no-such-question"]
            )
        self.assertEqual(2, code)
        self.assertEqual("QUESTION_UNKNOWN", json.loads(stderr)["error_code"])

    # --- reject ----------------------------------------------------------

    def test_reject_records_reason_and_audits(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), [candidate("cand-bbb2220000")])
            code, stdout, stderr = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "candidates", "reject",
                 "--candidate-id", "cand-bbb2220000", "--reason", "lower-trust mirror of official source"]
            )
            stored = self.store_records(workspace)
            events = self.audit_events(workspace)
        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertTrue(report["updated"])
        self.assertEqual("rejected", report["status"])
        record = stored[0]
        self.assertEqual("rejected", record["status"])
        self.assertEqual("lower-trust mirror of official source", record["rejection_reason"])
        self.assertEqual("discover_sources.py/candidates", record["rejected_by"])
        self.assertEqual(1, len(events))
        self.assertEqual("reject", events[0]["event"])
        self.assertEqual("lower-trust mirror of official source", events[0]["reason"])

    def test_reject_is_idempotent_for_same_reason(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), [candidate("cand-bbb2220000")])
            argv = ["--project-root", str(workspace), "--format", "json", "candidates", "reject",
                    "--candidate-id", "cand-bbb2220000", "--reason", "mirror"]
            _, out1, _ = self.run_cli(argv)
            _, out2, _ = self.run_cli(argv)
            event_count = len(self.audit_events(workspace))
        self.assertTrue(json.loads(out1)["updated"])
        self.assertFalse(json.loads(out2)["updated"])
        self.assertEqual(1, event_count)

    def test_rejected_candidate_reason_cannot_be_silently_rewritten(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), [candidate("cand-bbb2220000")])
            base = [
                "--project-root", str(workspace), "--format", "json", "candidates", "reject",
                "--candidate-id", "cand-bbb2220000", "--reason",
            ]
            self.assertEqual(0, self.run_cli([*base, "mirror"])[0])
            before = (workspace / "sources" / "discovery" / "candidates.jsonl").read_bytes()
            code, stdout, stderr = self.run_cli([*base, "different reason"])
            after = (workspace / "sources" / "discovery" / "candidates.jsonl").read_bytes()
            events = self.audit_events(workspace)

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual("CANDIDATE_CORRELATION_CONFLICT", json.loads(stderr)["error_code"])
        self.assertEqual(before, after)
        self.assertEqual(1, len(events))

    def test_rejected_candidate_is_terminal_and_cannot_be_selected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                [candidate("cand-aaa1110000")],
                requests=[{"request_id": "req-1a2b3c4d5e", "status": "open"}],
            )
            self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "candidates", "reject",
                 "--candidate-id", "cand-aaa1110000", "--reason", "changed my mind later"]
            )
            code, stdout, stderr = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "candidates", "select",
                 "--candidate-id", "cand-aaa1110000", "--request-id", "req-1a2b3c4d5e"]
            )
            record = self.store_records(workspace)[0]
            events = self.audit_events(workspace)
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual("CANDIDATE_TRANSITION_INVALID", json.loads(stderr)["error_code"])
        self.assertEqual("rejected", record["lifecycle_state"])
        self.assertEqual("changed my mind later", record["rejection_reason"])
        self.assertEqual(1, len(events))

    def test_blank_reason_is_value_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), [candidate("cand-bbb2220000")])
            code, _, stderr = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "candidates", "reject",
                 "--candidate-id", "cand-bbb2220000", "--reason", "   "]
            )
        self.assertEqual(2, code)
        self.assertEqual("VALUE_INVALID", json.loads(stderr)["error_code"])

    # --- complete lifecycle state machine -------------------------------

    def test_every_legal_lifecycle_transition_is_executable_and_audited(self):
        for prior_state, allowed_states in DISCOVER.CANDIDATE_STATE_TRANSITIONS.items():
            for new_state in allowed_states:
                with self.subTest(prior_state=prior_state, new_state=new_state), tempfile.TemporaryDirectory() as tmpdir:
                    workspace = self.write_workspace(
                        Path(tmpdir),
                        [
                            canonical_candidate("cand-transition", prior_state),
                            canonical_candidate("cand-replacement0", "proposed"),
                        ],
                        requests=[{"request_id": "req-1a2b3c4d5e", "status": "open"}],
                    )
                    (workspace / "sources" / "manifest.jsonl").write_text(
                        json.dumps({"id": "paper:fetched", "kind": "pdf"}) + "\n",
                        encoding="utf-8",
                    )
                    argv = [
                        "--project-root",
                        str(workspace),
                        "--format",
                        "json",
                        "candidates",
                        "transition",
                        "--candidate-id",
                        "cand-transition",
                        "--expected-state",
                        prior_state,
                        "--to-state",
                        new_state,
                        "--reason",
                        f"exercise {prior_state} to {new_state}",
                        "--actor",
                        "lifecycle-test-agent",
                        "--run-id",
                        "run-lifecycle-test",
                    ]
                    if new_state == "selected":
                        argv += ["--request-id", "req-1a2b3c4d5e"]
                    if new_state == "fetched":
                        argv += ["--source-id", "paper:fetched"]
                    if new_state == "superseded":
                        argv += ["--superseded-by-candidate-id", "cand-replacement0"]

                    code, stdout, stderr = self.run_cli(argv)
                    stored = {record["candidate_id"]: record for record in self.store_records(workspace)}
                    events = self.audit_events(workspace)

                self.assertEqual(0, code, stderr)
                report = json.loads(stdout)
                self.assertTrue(report["updated"])
                self.assertEqual(new_state, report["lifecycle_state"])
                self.assertEqual(new_state, stored["cand-transition"]["lifecycle_state"])
                self.assertEqual(
                    DISCOVER.CANDIDATE_STATE_TO_LEGACY_STATUS[new_state],
                    stored["cand-transition"]["status"],
                )
                self.assertEqual(1, len(events))
                event = events[0]
                self.assertEqual("candidate_transition", event["event_type"])
                self.assertEqual(prior_state, event["prior_state"])
                self.assertEqual(new_state, event["new_state"])
                self.assertEqual("cand-transition", event["candidate_id"])
                self.assertEqual("lifecycle-test-agent", event["actor"])
                self.assertEqual(f"exercise {prior_state} to {new_state}", event["reason"])
                self.assertEqual("run-lifecycle-test", event["run_id"])
                self.assertIn("request_id", event)
                self.assertIn("source_id", event)
                self.assertRegex(event["at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_transition_repeat_is_idempotent_and_different_reason_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), [canonical_candidate("cand-repeat000", "proposed")])
            first = [
                "--project-root", str(workspace), "--format", "json", "candidates", "transition",
                "--candidate-id", "cand-repeat000", "--expected-state", "proposed",
                "--to-state", "reviewed", "--reason", "review complete",
            ]
            code, _, stderr = self.run_cli(first)
            self.assertEqual(0, code, stderr)
            repeat = [
                "--project-root", str(workspace), "--format", "json", "candidates", "transition",
                "--candidate-id", "cand-repeat000", "--expected-state", "reviewed",
                "--to-state", "reviewed", "--reason", "review complete",
            ]
            code, stdout, stderr = self.run_cli(repeat)
            self.assertEqual(0, code, stderr)
            self.assertFalse(json.loads(stdout)["updated"])
            before = (workspace / "sources" / "discovery" / "candidates.jsonl").read_bytes()
            repeat[-1] = "different review rationale"
            code, stdout, stderr = self.run_cli(repeat)
            after = (workspace / "sources" / "discovery" / "candidates.jsonl").read_bytes()
            event_count = len(self.audit_events(workspace))

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual("CANDIDATE_CORRELATION_CONFLICT", json.loads(stderr)["error_code"])
        self.assertEqual(before, after)
        self.assertEqual(1, event_count)

    def test_stale_and_illegal_transitions_fail_without_rewriting_store_or_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), [canonical_candidate("cand-guard0000", "reviewed")])
            store = workspace / "sources" / "discovery" / "candidates.jsonl"
            before = store.read_bytes()
            code, _, stderr = self.run_cli(
                [
                    "--project-root", str(workspace), "--format", "json", "candidates", "transition",
                    "--candidate-id", "cand-guard0000", "--expected-state", "proposed",
                    "--to-state", "deferred", "--reason", "stale writer",
                ]
            )
            self.assertEqual(2, code)
            self.assertEqual("CANDIDATE_STATE_STALE", json.loads(stderr)["error_code"])
            self.assertEqual(before, store.read_bytes())

            workspace = self.write_workspace(Path(tmpdir) / "illegal", [canonical_candidate("cand-jump00000", "proposed")])
            (workspace / "sources" / "manifest.jsonl").write_text(
                json.dumps({"id": "paper:fetched"}) + "\n", encoding="utf-8"
            )
            store = workspace / "sources" / "discovery" / "candidates.jsonl"
            before = store.read_bytes()
            code, _, stderr = self.run_cli(
                [
                    "--project-root", str(workspace), "--format", "json", "candidates", "transition",
                    "--candidate-id", "cand-jump00000", "--expected-state", "proposed",
                    "--to-state", "fetched", "--source-id", "paper:fetched", "--reason", "illegal jump",
                ]
            )
            after = store.read_bytes()
            events = self.audit_events(workspace)

        self.assertEqual(2, code)
        self.assertEqual("CANDIDATE_TRANSITION_INVALID", json.loads(stderr)["error_code"])
        self.assertEqual(before, after)
        self.assertEqual([], events)

    def test_legacy_states_map_explicitly_without_inferring_or_persisting_review(self):
        legacy_records = [
            candidate("cand-implicit00"),
            candidate("cand-new000000", extra={"status": "new"}),
            candidate("cand-selected00", extra={"status": "selected", "selected_request_id": "req-1"}),
            candidate("cand-rejected00", extra={"status": "rejected", "rejection_reason": "mirror"}),
            candidate("cand-fetched000", extra={"status": "fetched"}),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), legacy_records)
            store = workspace / "sources" / "discovery" / "candidates.jsonl"
            before = store.read_bytes()
            code, stdout, stderr = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "candidates", "list"]
            )
            after = store.read_bytes()

        self.assertEqual(0, code, stderr)
        self.assertEqual(before, after, "read-only migration views must not rewrite legacy records")
        by_id = {record["candidate_id"]: record for record in json.loads(stdout)["candidates"]}
        self.assertEqual("proposed", by_id["cand-implicit00"]["lifecycle_state"])
        self.assertEqual("proposed", by_id["cand-new000000"]["lifecycle_state"])
        self.assertEqual("selected", by_id["cand-selected00"]["lifecycle_state"])
        self.assertEqual("rejected", by_id["cand-rejected00"]["lifecycle_state"])
        self.assertEqual("fetched", by_id["cand-fetched000"]["lifecycle_state"])
        for record in by_id.values():
            self.assertFalse(record["lifecycle_migration"]["review_state_inferred"])

    def test_contradictory_lifecycle_fields_fail_closed_without_rewrite(self):
        conflicting = candidate(
            "cand-conflict00",
            extra={
                "lifecycle_schema_version": "2.0",
                "lifecycle_state": "reviewed",
                "status": "selected",
            },
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), [conflicting])
            store = workspace / "sources" / "discovery" / "candidates.jsonl"
            before = store.read_bytes()
            code, stdout, stderr = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "candidates", "list"]
            )
            after = store.read_bytes()

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual("CANDIDATE_STATE_INVALID", json.loads(stderr)["error_code"])
        self.assertEqual(before, after)

    def test_concurrent_expected_state_writers_serialize_one_update_and_one_stale_refusal(self):
        if not LOCKS.multiprocess_lock_supported():
            self.skipTest("No process-safe workspace lock backend is available")
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), [canonical_candidate("cand-race000000", "proposed")])
            env = {**os.environ, "PYTHONPATH": str(SCRIPTS)}
            processes = [
                subprocess.Popen(
                    [
                        sys.executable, str(DISCOVER_PATH), "--project-root", str(workspace),
                        "--format", "json", "candidates", "transition",
                        "--candidate-id", "cand-race000000", "--expected-state", "proposed",
                        "--to-state", new_state, "--reason", f"concurrent {new_state}",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
                for new_state in ("reviewed", "deferred")
            ]
            results = [process.communicate(timeout=30) for process in processes]
            codes = [process.returncode for process in processes]
            records = self.store_records(workspace)
            events = self.audit_events(workspace)

        self.assertEqual([0, 2], sorted(codes), results)
        self.assertEqual(1, len(records))
        self.assertIn(records[0]["lifecycle_state"], {"reviewed", "deferred"})
        self.assertEqual(1, len(events))
        refusal = next(
            json.loads(stderr)
            for process, (_stdout, stderr) in zip(processes, results, strict=True)
            if process.returncode == 2
        )
        self.assertEqual("CANDIDATE_STATE_STALE", refusal["error_code"])

    # --- malformed store -------------------------------------------------

    def test_malformed_store_is_workspace_unreadable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), [candidate("cand-aaa1110000")])
            store = workspace / "sources" / "discovery" / "candidates.jsonl"
            store.write_text(store.read_text() + "{not valid json\n", encoding="utf-8")
            code, _, stderr = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "candidates", "list"]
            )
        self.assertEqual(2, code)
        self.assertEqual("WORKSPACE_UNREADABLE", json.loads(stderr)["error_code"])

    # --- no network ------------------------------------------------------

    def test_candidates_commands_open_no_socket(self):
        def forbid_socket(*args, **kwargs):  # pragma: no cover - only fires on a bug
            raise AssertionError("candidate review must not open a network socket")

        original = socket.socket
        socket.socket = forbid_socket
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                workspace = self.write_workspace(
                    Path(tmpdir),
                    [candidate("cand-aaa1110000")],
                    requests=[{"request_id": "req-1a2b3c4d5e", "status": "open"}],
                )
                for argv in (
                    ["candidates", "list"],
                    ["candidates", "select", "--candidate-id", "cand-aaa1110000", "--request-id", "req-1a2b3c4d5e"],
                    ["candidates", "reject", "--candidate-id", "cand-aaa1110000", "--reason", "x"],
                ):
                    code, _, _ = self.run_cli(["--project-root", str(workspace), "--format", "json", *argv])
                    self.assertEqual(0, code)
        finally:
            socket.socket = original

    # --- concurrency -----------------------------------------------------

    def test_candidate_rewrite_retries_transient_windows_replace_contention(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), [candidate("cand-retry00000")])
            store = workspace / "sources" / "discovery" / "candidates.jsonl"
            updated = canonical_candidate("cand-retry00000", "rejected", rejection_reason="not relevant")
            real_replace = Path.replace
            attempts = 0

            def flaky_replace(source: Path, target: Path):
                nonlocal attempts
                if source.name.startswith(f"{store.name}.") and target == store:
                    attempts += 1
                    if attempts < 3:
                        error = PermissionError(13, "synthetic Windows sharing violation", str(target))
                        error.winerror = 5
                        raise error
                return real_replace(source, target)

            with mock.patch.object(Path, "replace", new=flaky_replace), mock.patch.object(DISCOVER.time, "sleep") as sleep:
                DISCOVER.rewrite_candidates(store, [updated])

            self.assertEqual(3, attempts)
            self.assertEqual(2, sleep.call_count)
            self.assertEqual("rejected", self.store_records(workspace)[0]["lifecycle_state"])
            self.assertEqual([], list(store.parent.glob(f"{store.name}.*.tmp")))

    def test_concurrent_rejects_do_not_lose_updates(self):
        if not LOCKS.multiprocess_lock_supported():
            self.skipTest("No process-safe workspace lock backend is available")
        count = 8
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                [candidate(f"cand-{index:010d}", discovered_at=f"2026-06-19T12:00:{index:02d}Z") for index in range(count)],
            )
            env = {**os.environ, "PYTHONPATH": str(SCRIPTS)}
            processes = [
                subprocess.Popen(
                    [
                        sys.executable, str(DISCOVER_PATH),
                        "--project-root", str(workspace), "--format", "json",
                        "candidates", "reject",
                        "--candidate-id", f"cand-{index:010d}",
                        "--reason", f"reason-{index}",
                    ],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                )
                for index in range(count)
            ]
            results = [process.communicate(timeout=60) for process in processes]
            codes = [process.returncode for process in processes]
            stored = self.store_records(workspace)
            events = self.audit_events(workspace)

        self.assertEqual([0] * count, codes, [err.decode() for _out, err in results])
        # No write was lost: every candidate is rejected, the file still has
        # exactly `count` records, and the audit trail captured every reject.
        self.assertEqual(count, len(stored))
        self.assertTrue(all(record.get("status") == "rejected" for record in stored), stored)
        self.assertEqual({record["candidate_id"] for record in stored}, {f"cand-{i:010d}" for i in range(count)})
        self.assertEqual(count, len(events))


if __name__ == "__main__":
    unittest.main()
