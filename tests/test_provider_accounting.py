"""Unit tests for the run-scoped provider-call ledger (CR-5 T6).

Why these tests exist, in one sentence each:

- A declared rate limit that is merely recorded is the exact gap CR-5 was filed
  over, so every ceiling is asserted as a *refusal*, not as a report.
- A budget kept in memory silently resets on every restart, so the ledger is
  written, every in-process object is thrown away, the module is loaded afresh,
  and the ceiling must still hold.
- A budget that resets when its ledger is damaged is worse than no budget, so
  each way of corrupting the file is asserted to refuse rather than to count
  zero.
- Reservation happens before transport, so a refused overage must leave the
  ledger byte-identical: nothing spent, nothing owed.
- ``discover_sources.py`` will migrate its academic ledger onto this module, so
  the last case drives *both* writers against one file and proves each reads the
  other's bytes and draws down the same budget.

The clock is injected everywhere. Nothing here sleeps.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests._script_loader import load_script as load_script_module
from tests._script_loader import load_script_uncached

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"


ACCOUNTING = load_script_module("provider_accounting_under_test", "_provider_accounting.py")

LEDGER = "provider-requests.jsonl"
LOCK = "provider-requests.lock"
VERSION = ACCOUNTING.ACCOUNTING_SCHEMA_VERSION
T0 = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)

# The academic ledger discovery keeps today, named exactly as discover_sources.py
# names it, so the compatibility case configures this module into that shape.
ACADEMIC_LEDGER = "academic-provider-requests.jsonl"
ACADEMIC_LOCK = "academic-provider-requests.lock"
ACADEMIC_EVENT_TYPE = "academic_provider_request"


class AccountingTestCase(unittest.TestCase):
    """Shared scaffolding: a run directory and a terse reserve() wrapper."""

    def run_dir(self, root: Path, run_id: str = "run-2026-08-09T120000Z") -> Path:
        run_dir = root / "runs" / run_id
        run_dir.mkdir(parents=True)
        return run_dir

    def reserve(self, run_dir: Path, provider: str, count: int = 1, **kwargs):
        options = {
            "ledger_filename": LEDGER,
            "lock_filename": LOCK,
            "schema_version": VERSION,
        }
        options.update(kwargs)
        return ACCOUNTING.reserve(run_dir, provider, count, **options)

    def ledger(self, run_dir: Path, filename: str = LEDGER) -> Path:
        return run_dir / filename

    def records(self, run_dir: Path, filename: str = LEDGER) -> list[dict]:
        path = self.ledger(run_dir, filename)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class RollingRateLimitWindowTests(AccountingTestCase):
    def test_requests_inside_the_window_are_counted_and_the_next_one_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            limit = {"requests": 2, "per": "minute"}

            self.reserve(run_dir, "keepa", rate_limit=limit, now=T0)
            second = self.reserve(run_dir, "keepa", rate_limit=limit, now=T0)
            self.assertEqual(2, second.window_used)

            with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
                self.reserve(run_dir, "keepa", rate_limit=limit, now=T0)

            self.assertEqual("ACQUISITION_PROVIDER_RATE_LIMITED", ctx.exception.error_code)
            self.assertEqual("rate_limit", ctx.exception.details["ceiling"])
            self.assertEqual("minute", ctx.exception.details["window"])
            self.assertEqual(2, len(self.records(run_dir)))

    def test_a_request_exactly_one_window_ago_has_left_the_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            limit = {"requests": 1, "per": "minute"}
            self.reserve(run_dir, "keepa", rate_limit=limit, now=T0)

            for offset, allowed in ((1, False), (30, False), (59, False), (60, True), (61, True)):
                with self.subTest(seconds_after=offset, allowed=allowed):
                    moment = T0 + timedelta(seconds=offset)
                    if allowed:
                        reservation = self.reserve(run_dir, "keepa", rate_limit=limit, now=moment)
                        self.assertEqual(1, reservation.window_used)
                        # Undo so each boundary case sees the same ledger.
                        self.ledger(run_dir).write_text(
                            "".join(
                                f"{json.dumps(record, separators=(',', ':'))}\n"
                                for record in self.records(run_dir)[:1]
                            ),
                            encoding="utf-8",
                        )
                    else:
                        with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
                            self.reserve(run_dir, "keepa", rate_limit=limit, now=moment)
                        self.assertEqual("ACQUISITION_PROVIDER_RATE_LIMITED", ctx.exception.error_code)

    def test_the_window_rolls_rather_than_resetting_on_a_boundary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            limit = {"requests": 2, "per": "minute"}
            self.reserve(run_dir, "keepa", rate_limit=limit, now=T0)
            self.reserve(run_dir, "keepa", rate_limit=limit, now=T0 + timedelta(seconds=30))

            # At T0+60 the first reservation has aged out and the second has not:
            # a fixed per-minute bucket would have freed both.
            reservation = self.reserve(run_dir, "keepa", rate_limit=limit, now=T0 + timedelta(seconds=60))
            self.assertEqual(2, reservation.window_used)

            with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
                self.reserve(run_dir, "keepa", rate_limit=limit, now=T0 + timedelta(seconds=60))

            self.assertEqual("2026-08-09T12:01:30Z", ctx.exception.details["clears_at"])

    def test_remediation_states_the_window_and_when_it_clears(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            limit = {"requests": 1, "per": "hour"}
            self.reserve(run_dir, "keepa", rate_limit=limit, now=T0)

            with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
                self.reserve(run_dir, "keepa", rate_limit=limit, now=T0 + timedelta(minutes=5))

            self.assertEqual("2026-08-09T13:00:00Z", ctx.exception.details["clears_at"])
            self.assertIn("hour", ctx.exception.remediation)
            self.assertIn("2026-08-09T13:00:00Z", ctx.exception.remediation)

    def test_a_multi_request_plan_reserves_every_slot_or_none_of_them(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            limit = {"requests": 4, "per": "minute"}
            self.reserve(run_dir, "keepa", 3, rate_limit=limit, now=T0)

            with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
                self.reserve(run_dir, "keepa", 2, rate_limit=limit, now=T0)

            self.assertEqual(3, len(self.records(run_dir)))
            self.assertEqual(2, ctx.exception.details["requested"])
            self.assertEqual("2026-08-09T12:01:00Z", ctx.exception.details["clears_at"])

    def test_a_plan_larger_than_the_whole_limit_can_never_clear(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))

            with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
                self.reserve(run_dir, "keepa", 5, rate_limit={"requests": 4, "per": "minute"}, now=T0)

            self.assertIsNone(ctx.exception.details["clears_at"])
            self.assertIn("Waiting cannot admit this request", ctx.exception.remediation)
            self.assertEqual([], self.records(run_dir))

    def test_hour_windows_are_measured_in_hours(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            limit = {"requests": 1, "per": "hour"}
            self.reserve(run_dir, "keepa", rate_limit=limit, now=T0)

            with self.assertRaises(ACCOUNTING.ProviderAccountingError):
                self.reserve(run_dir, "keepa", rate_limit=limit, now=T0 + timedelta(minutes=59, seconds=59))

            reservation = self.reserve(run_dir, "keepa", rate_limit=limit, now=T0 + timedelta(hours=1))
            self.assertEqual(1, reservation.window_used)

    def test_no_declared_rate_limit_leaves_the_window_unenforced(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            for index in range(50):
                with self.subTest(request=index):
                    reservation = self.reserve(run_dir, "keepa", now=T0)
                    self.assertIsNone(reservation.window_used)
            self.assertEqual(50, len(self.records(run_dir)))


class PerRunCeilingTests(AccountingTestCase):
    def test_the_cumulative_ceiling_refuses_the_request_that_would_exceed_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            for index in range(3):
                with self.subTest(request=index):
                    reservation = self.reserve(run_dir, "keepa", per_run_max=3, now=T0)
                    self.assertEqual(index + 1, reservation.per_run_used)

            with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
                self.reserve(run_dir, "keepa", per_run_max=3, now=T0)

            self.assertEqual("ACQUISITION_PROVIDER_RATE_LIMITED", ctx.exception.error_code)
            self.assertEqual("per_run_max", ctx.exception.details["ceiling"])
            self.assertEqual("run", ctx.exception.details["window"])
            self.assertIsNone(ctx.exception.details["clears_at"])
            self.assertIn("does not clear until a new run starts", ctx.exception.remediation)

    def test_the_cumulative_ceiling_counts_every_provider_in_the_ledger(self):
        """The run budget is the ledger's, not one provider's — discovery's semantics."""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            self.reserve(run_dir, "arxiv", 2, per_run_max=3, now=T0)
            self.reserve(run_dir, "openalex", per_run_max=3, now=T0)

            for provider in ("arxiv", "openalex"):
                with self.subTest(provider=provider):
                    with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
                        self.reserve(run_dir, provider, per_run_max=3, now=T0)
                    self.assertEqual("per_run_max", ctx.exception.details["ceiling"])
                    self.assertEqual(3, ctx.exception.details["used"])

    def test_a_zero_ceiling_refuses_before_the_ledger_is_even_created(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))

            with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
                self.reserve(run_dir, "keepa", per_run_max=0, now=T0)

            self.assertEqual("per_run_max", ctx.exception.details["ceiling"])
            self.assertFalse(self.ledger(run_dir).exists())

    def test_no_cumulative_ceiling_leaves_the_run_budget_unenforced(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            reservation = self.reserve(run_dir, "keepa", 40, now=T0)
            self.assertEqual(40, reservation.per_run_used)
            self.assertIsNone(reservation.per_run_max)


class BothCeilingsTogetherTests(AccountingTestCase):
    def test_a_declared_rate_limit_tightens_a_looser_run_budget(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            limit = {"requests": 2, "per": "minute"}
            self.reserve(run_dir, "keepa", 2, per_run_max=25, rate_limit=limit, now=T0)

            with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
                self.reserve(run_dir, "keepa", per_run_max=25, rate_limit=limit, now=T0)

            self.assertEqual("rate_limit", ctx.exception.details["ceiling"])

    def test_a_generous_declared_rate_limit_cannot_loosen_the_run_budget(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            limit = {"requests": 1000, "per": "minute"}
            self.reserve(run_dir, "keepa", 2, per_run_max=2, rate_limit=limit, now=T0)

            with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
                self.reserve(run_dir, "keepa", per_run_max=2, rate_limit=limit, now=T0)

            self.assertEqual("per_run_max", ctx.exception.details["ceiling"])

    def test_the_run_budget_survives_a_cleared_rate_limit_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            limit = {"requests": 2, "per": "minute"}
            self.reserve(run_dir, "keepa", 2, per_run_max=3, rate_limit=limit, now=T0)
            self.reserve(run_dir, "keepa", per_run_max=3, rate_limit=limit, now=T0 + timedelta(minutes=1))

            # The window is empty again an hour later; the run's own ceiling is not.
            with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
                self.reserve(run_dir, "keepa", per_run_max=3, rate_limit=limit, now=T0 + timedelta(hours=1))

            self.assertEqual("per_run_max", ctx.exception.details["ceiling"])


class InterleavedProvidersTests(AccountingTestCase):
    def test_each_provider_gets_its_own_rate_limit_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            limit = {"requests": 1, "per": "minute"}
            self.reserve(run_dir, "keepa", rate_limit=limit, now=T0)

            # keepa is spent; ebay-browse has not made a request at all.
            with self.assertRaises(ACCOUNTING.ProviderAccountingError):
                self.reserve(run_dir, "keepa", rate_limit=limit, now=T0)
            reservation = self.reserve(run_dir, "ebay-browse", rate_limit=limit, now=T0)

            self.assertEqual(1, reservation.window_used)
            self.assertEqual(2, reservation.per_run_used)

    def test_usage_reports_only_the_named_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            self.reserve(run_dir, "keepa", 2, now=T0)
            self.reserve(run_dir, "ebay-browse", now=T0 + timedelta(seconds=1))
            self.reserve(run_dir, "keepa", now=T0 + timedelta(seconds=2))

            keepa = ACCOUNTING.usage(run_dir, "keepa", ledger_filename=LEDGER)
            ebay = ACCOUNTING.usage(run_dir, "ebay-browse", ledger_filename=LEDGER)

            self.assertEqual(3, len(keepa))
            self.assertEqual(1, len(ebay))
            self.assertEqual({"keepa"}, {event.provider_id for event in keepa})
            # Events come back in the order they were reserved.
            self.assertEqual(
                ["2026-08-09T12:00:00Z", "2026-08-09T12:00:00Z", "2026-08-09T12:00:02Z"],
                [event.reserved_at for event in keepa],
            )

    def test_usage_of_an_unseen_provider_is_empty_rather_than_an_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            self.reserve(run_dir, "keepa", now=T0)
            self.assertEqual((), ACCOUNTING.usage(run_dir, "spapi", ledger_filename=LEDGER))

    def test_usage_of_a_run_that_never_reserved_is_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            self.assertEqual((), ACCOUNTING.usage(run_dir, "keepa", ledger_filename=LEDGER))


class RestartStabilityTests(AccountingTestCase):
    def test_a_freshly_loaded_module_still_refuses_over_the_run_budget(self):
        """Drop every in-memory object, re-import the module, re-read from disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            for _ in range(3):
                ACCOUNTING.reserve(
                    run_dir,
                    "keepa",
                    1,
                    ledger_filename=LEDGER,
                    lock_filename=LOCK,
                    schema_version=VERSION,
                    per_run_max=3,
                    now=T0,
                )

            restarted = load_script_uncached("provider_accounting_after_restart", "_provider_accounting.py")
            self.assertIsNot(restarted, ACCOUNTING)

            with self.assertRaises(restarted.ProviderAccountingError) as ctx:
                restarted.reserve(
                    run_dir,
                    "keepa",
                    1,
                    ledger_filename=LEDGER,
                    lock_filename=LOCK,
                    schema_version=VERSION,
                    per_run_max=3,
                    now=T0,
                )

            self.assertEqual("per_run_max", ctx.exception.details["ceiling"])
            self.assertEqual(3, len(restarted.usage(run_dir, "keepa", ledger_filename=LEDGER)))

    def test_a_freshly_loaded_module_still_refuses_inside_the_rate_limit_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            limit = {"requests": 2, "per": "minute"}
            self.reserve(run_dir, "keepa", 2, rate_limit=limit, now=T0)

            restarted = load_script_uncached("provider_accounting_after_window_restart", "_provider_accounting.py")
            with self.assertRaises(restarted.ProviderAccountingError) as ctx:
                restarted.reserve(
                    run_dir,
                    "keepa",
                    1,
                    ledger_filename=LEDGER,
                    lock_filename=LOCK,
                    schema_version=VERSION,
                    rate_limit=limit,
                    now=T0 + timedelta(seconds=30),
                )

            self.assertEqual("rate_limit", ctx.exception.details["ceiling"])
            self.assertEqual("2026-08-09T12:01:00Z", ctx.exception.details["clears_at"])

    def test_reservations_are_durable_before_the_call_the_caller_is_about_to_make(self):
        """A crash between reserve() and transport must leave the slot spent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            reservation = self.reserve(run_dir, "keepa", now=T0)

            on_disk = self.records(run_dir)
            self.assertEqual(1, len(on_disk))
            self.assertEqual(reservation.events[0].call_id, on_disk[0]["call_id"])
            self.assertTrue(on_disk[0]["budget_consumed"])


class RefusalIsPreTransportTests(AccountingTestCase):
    def test_a_refused_overage_leaves_the_ledger_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            self.reserve(run_dir, "keepa", 2, per_run_max=2, now=T0)
            before = self.ledger(run_dir).read_bytes()

            for kwargs in (
                {"per_run_max": 2},
                {"rate_limit": {"requests": 1, "per": "minute"}},
                {"per_run_max": 2, "rate_limit": {"requests": 1, "per": "hour"}},
            ):
                with self.subTest(**kwargs):
                    with self.assertRaises(ACCOUNTING.ProviderAccountingError):
                        self.reserve(run_dir, "keepa", now=T0, **kwargs)
                    self.assertEqual(before, self.ledger(run_dir).read_bytes())

    def test_the_refusal_states_that_no_network_io_was_executed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))

            with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
                self.reserve(run_dir, "keepa", per_run_max=0, now=T0)

            self.assertIs(False, ctx.exception.details["network_io_executed"])

    def test_a_refusal_names_the_provider_the_run_and_the_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir), run_id="run-named")

            with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
                self.reserve(run_dir, "keepa", per_run_max=0, now=T0)

            self.assertEqual("keepa", ctx.exception.details["provider"])
            self.assertEqual("run-named", ctx.exception.details["run_id"])
            self.assertTrue(ctx.exception.details["ledger_path"].endswith(LEDGER))


class DamagedLedgerTests(AccountingTestCase):
    def assert_refuses(self, run_dir: Path):
        with self.assertRaises(ACCOUNTING.ProviderAccountingError) as reading:
            ACCOUNTING.usage(run_dir, "keepa", ledger_filename=LEDGER)
        self.assertEqual("PROVIDER_ACCOUNTING_LEDGER_INVALID", reading.exception.error_code)

        # And the refusal reaches the budget, rather than a damaged ledger
        # quietly handing a fresh allowance back to the caller.
        with self.assertRaises(ACCOUNTING.ProviderAccountingError) as reserving:
            self.reserve(run_dir, "keepa", per_run_max=25, now=T0)
        self.assertEqual("PROVIDER_ACCOUNTING_LEDGER_INVALID", reserving.exception.error_code)

    def valid_record(self, run_id: str, **overrides) -> dict:
        record = {
            "schema_version": VERSION,
            "event_type": "provider_request",
            "call_id": "keepa-call-0001",
            "run_id": run_id,
            "provider": "keepa",
            "reserved_at": "2026-08-09T12:00:00Z",
            "budget_consumed": True,
        }
        record.update(overrides)
        return record

    def test_every_shape_of_damaged_record_refuses_instead_of_counting_zero(self):
        run_id = "run-damaged"
        cases = {
            "unparseable json": "{not valid json\n",
            "not an object": "[1, 2, 3]\n",
            "foreign schema version": json.dumps(self.valid_record(run_id, schema_version="2.0")) + "\n",
            "foreign run": json.dumps(self.valid_record("run-other")) + "\n",
            "missing event type": json.dumps(self.valid_record(run_id, event_type="")) + "\n",
            "missing provider": json.dumps(self.valid_record(run_id, provider=None)) + "\n",
            "missing call id": json.dumps(self.valid_record(run_id, call_id="  ")) + "\n",
            "unreadable timestamp": json.dumps(self.valid_record(run_id, reserved_at="last tuesday")) + "\n",
            "duplicate call id": (
                json.dumps(self.valid_record(run_id)) + "\n" + json.dumps(self.valid_record(run_id)) + "\n"
            ),
        }
        for label, content in cases.items():
            with self.subTest(damage=label), tempfile.TemporaryDirectory() as tmpdir:
                run_dir = self.run_dir(Path(tmpdir), run_id=run_id)
                self.ledger(run_dir).write_text(content, encoding="utf-8")
                self.assert_refuses(run_dir)

    def test_a_schema_mismatch_names_the_version_the_ledger_is_read_at(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir), run_id="run-skew")
            self.ledger(run_dir).write_text(
                json.dumps(self.valid_record("run-skew", schema_version="9.9")) + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
                ACCOUNTING.usage(run_dir, "keepa", ledger_filename=LEDGER)

            self.assertEqual(VERSION, ctx.exception.details["expected_schema_version"])
            self.assertIn("9.9", str(ctx.exception))

    def test_a_damaged_line_is_reported_by_line_number(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir), run_id="run-lines")
            self.ledger(run_dir).write_text(
                json.dumps(self.valid_record("run-lines")) + "\n\n" + "{oops\n",
                encoding="utf-8",
            )

            with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
                ACCOUNTING.usage(run_dir, "keepa", ledger_filename=LEDGER)

            self.assertEqual(3, ctx.exception.details["line"])

    def test_a_symlinked_ledger_is_refused_rather_than_followed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = self.run_dir(root)
            elsewhere = root / "elsewhere.jsonl"
            elsewhere.write_text("", encoding="utf-8")
            self.ledger(run_dir).symlink_to(elsewhere)

            self.assert_refuses(run_dir)

    def test_a_hard_linked_ledger_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = self.run_dir(root)
            elsewhere = root / "elsewhere.jsonl"
            elsewhere.write_text("", encoding="utf-8")
            os.link(elsewhere, self.ledger(run_dir))

            self.assert_refuses(run_dir)

    def test_a_directory_where_the_ledger_belongs_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            self.ledger(run_dir).mkdir()

            self.assert_refuses(run_dir)

    def test_blank_lines_are_tolerated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir), run_id="run-blank")
            self.ledger(run_dir).write_text(
                "\n" + json.dumps(self.valid_record("run-blank")) + "\n\n",
                encoding="utf-8",
            )
            self.assertEqual(1, len(ACCOUNTING.usage(run_dir, "keepa", ledger_filename=LEDGER)))


class ConcurrentReservationTests(AccountingTestCase):
    def test_the_lock_makes_the_ceiling_hold_under_concurrent_reservations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            attempts = 16
            ceiling = 6

            def attempt(_index: int) -> str:
                try:
                    self.reserve(run_dir, "keepa", per_run_max=ceiling, now=T0)
                except ACCOUNTING.ProviderAccountingError as exc:
                    return exc.error_code
                return "reserved"

            with ThreadPoolExecutor(max_workers=attempts) as pool:
                outcomes = list(pool.map(attempt, range(attempts)))

            self.assertEqual(ceiling, outcomes.count("reserved"))
            self.assertEqual(attempts - ceiling, outcomes.count("ACQUISITION_PROVIDER_RATE_LIMITED"))

            records = self.records(run_dir)
            self.assertEqual(ceiling, len(records))
            self.assertEqual(ceiling, len({record["call_id"] for record in records}))

    def test_concurrent_reservations_do_not_interleave_within_a_multi_slot_plan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))

            def attempt(index: int) -> None:
                self.reserve(run_dir, f"provider-{index % 2}", 3, now=T0)

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(attempt, range(8)))

            records = self.records(run_dir)
            self.assertEqual(24, len(records))
            # Every plan's three records landed consecutively, so no writer
            # split another writer's append.
            providers = [record["provider"] for record in records]
            for start in range(0, len(providers), 3):
                with self.subTest(plan=start // 3):
                    self.assertEqual(1, len(set(providers[start : start + 3])))


class RecordShapeTests(AccountingTestCase):
    def test_the_record_carries_the_identity_fields_a_ledger_is_read_by(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir), run_id="run-shape")
            self.reserve(run_dir, "keepa", now=T0)

            record = self.records(run_dir)[0]
            self.assertEqual(
                ["schema_version", "event_type", "call_id", "run_id", "provider", "reserved_at", "budget_consumed"],
                list(record),
            )
            self.assertEqual(VERSION, record["schema_version"])
            self.assertEqual("provider_request", record["event_type"])
            self.assertEqual("run-shape", record["run_id"])
            self.assertEqual("keepa", record["provider"])
            self.assertEqual("2026-08-09T12:00:00Z", record["reserved_at"])
            self.assertTrue(record["call_id"].startswith("keepa-call-"))

    def test_extra_fields_are_appended_and_a_restated_key_keeps_its_position(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            self.reserve(
                run_dir,
                "arxiv",
                now=T0,
                extra_fields={"event_type": ACADEMIC_EVENT_TYPE, "command": "academic", "attempt": 2},
            )

            record = self.records(run_dir)[0]
            self.assertEqual(
                [
                    "schema_version",
                    "event_type",
                    "call_id",
                    "run_id",
                    "provider",
                    "reserved_at",
                    "budget_consumed",
                    "command",
                    "attempt",
                ],
                list(record),
            )
            self.assertEqual(ACADEMIC_EVENT_TYPE, record["event_type"])
            self.assertEqual(2, record["attempt"])

    def test_extra_fields_may_not_forge_an_identity_field(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            for key in sorted(ACCOUNTING.PROTECTED_RECORD_KEYS):
                with self.subTest(key=key):
                    with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
                        self.reserve(run_dir, "keepa", now=T0, extra_fields={key: "forged"})
                    self.assertEqual("PROVIDER_ACCOUNTING_ARGUMENT_INVALID", ctx.exception.error_code)
            self.assertEqual([], self.records(run_dir))

    def test_every_reserved_slot_gets_its_own_call_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            reservation = self.reserve(run_dir, "keepa", 4, now=T0)

            call_ids = {event.call_id for event in reservation.events}
            self.assertEqual(4, len(call_ids))
            self.assertEqual(call_ids, {record["call_id"] for record in self.records(run_dir)})

    def test_the_ledger_is_compact_jsonl_with_one_object_per_line(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            self.reserve(run_dir, "keepa", 2, now=T0)

            raw = self.ledger(run_dir).read_bytes()
            self.assertTrue(raw.endswith(b"\n"))
            self.assertNotIn(b", ", raw)
            self.assertEqual(2, raw.count(b"\n"))


class ArgumentValidationTests(AccountingTestCase):
    def test_malformed_arguments_are_refused_before_anything_is_written(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            cases = {
                "zero count": {"count": 0},
                "negative count": {"count": -1},
                "boolean count": {"count": True},
                "empty provider": {"provider": "  "},
                "provider with whitespace": {"provider": "kee pa"},
                "negative per_run_max": {"per_run_max": -1},
                "boolean per_run_max": {"per_run_max": True},
                "ledger with a separator": {"ledger_filename": "../escape.jsonl"},
                "lock with a separator": {"lock_filename": f"nested{os.sep}lock"},
                "empty schema version": {"schema_version": ""},
                "unserializable extra field": {"extra_fields": {"when": object()}},
                "non-mapping extra fields": {"extra_fields": ["command", "academic"]},
                "non-string extra key": {"extra_fields": {7: "seven"}},
            }
            for label, override in cases.items():
                with self.subTest(argument=label):
                    kwargs = {"provider": "keepa", "count": 1, "now": T0}
                    kwargs.update(override)
                    provider = kwargs.pop("provider")
                    count = kwargs.pop("count")
                    with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
                        self.reserve(run_dir, provider, count, **kwargs)
                    self.assertEqual("PROVIDER_ACCOUNTING_ARGUMENT_INVALID", ctx.exception.error_code)
            self.assertEqual([], self.records(run_dir))

    def test_a_missing_run_directory_is_refused_rather_than_defaulting_to_the_cwd(self):
        for label, run_dir in (("empty string", ""), ("blank string", "   "), ("not a path", 7)):
            with self.subTest(run_dir=label):
                with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
                    ACCOUNTING.reserve(
                        run_dir,
                        "keepa",
                        1,
                        ledger_filename=LEDGER,
                        lock_filename=LOCK,
                        schema_version=VERSION,
                        now=T0,
                    )
                self.assertEqual("PROVIDER_ACCOUNTING_ARGUMENT_INVALID", ctx.exception.error_code)

    def test_malformed_rate_limit_declarations_are_refused(self):
        cases = {
            "missing per": {"requests": 5},
            "missing requests": {"per": "minute"},
            "zero requests": {"requests": 0, "per": "minute"},
            "boolean requests": {"requests": True, "per": "minute"},
            "unknown window": {"requests": 5, "per": "fortnight"},
            "non-string window": {"requests": 5, "per": 60},
        }
        for label, declaration in cases.items():
            with self.subTest(declaration=label):
                with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
                    ACCOUNTING.coerce_rate_limit(declaration)
                self.assertEqual("PROVIDER_ACCOUNTING_ARGUMENT_INVALID", ctx.exception.error_code)

    def test_a_rate_limit_is_read_from_a_mapping_or_from_attributes(self):
        class DeclaredRateLimit:
            requests = 60
            per = "minute"

        for label, declaration in (
            ("mapping", {"requests": 60, "per": "minute"}),
            ("attributes", DeclaredRateLimit()),
        ):
            with self.subTest(source=label):
                limit = ACCOUNTING.coerce_rate_limit(declaration)
                self.assertEqual(60, limit.requests)
                self.assertEqual("minute", limit.per)
                self.assertEqual(60, limit.window_seconds)

        self.assertIsNone(ACCOUNTING.coerce_rate_limit(None))

    def test_an_object_exposing_neither_shape_is_refused(self):
        with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
            ACCOUNTING.coerce_rate_limit(object())
        self.assertEqual("PROVIDER_ACCOUNTING_ARGUMENT_INVALID", ctx.exception.error_code)

    def test_the_clock_accepts_an_instant_or_a_callable_and_normalizes_to_utc(self):
        naive = datetime(2026, 8, 9, 12, 0, 0)
        offset = datetime(2026, 8, 9, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
        for label, clock in (
            ("aware datetime", T0),
            ("callable", lambda: T0),
            ("naive datetime read as utc", naive),
            ("non-utc offset", offset),
            ("sub-second precision truncated", T0.replace(microsecond=987654)),
        ):
            with self.subTest(clock=label), tempfile.TemporaryDirectory() as tmpdir:
                run_dir = self.run_dir(Path(tmpdir))
                reservation = self.reserve(run_dir, "keepa", now=clock)
                self.assertEqual("2026-08-09T12:00:00Z", reservation.reserved_at)

    def test_an_unusable_clock_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir))
            for label, clock in (("string", "2026-08-09T12:00:00Z"), ("callable returning a string", lambda: "now")):
                with self.subTest(clock=label):
                    with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
                        self.reserve(run_dir, "keepa", now=clock)
                    self.assertEqual("PROVIDER_ACCOUNTING_ARGUMENT_INVALID", ctx.exception.error_code)

    def test_the_ledger_and_lock_paths_follow_the_run_layout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = self.run_dir(Path(tmpdir), run_id="run-layout")
            self.assertEqual(run_dir / LEDGER, ACCOUNTING.ledger_path(run_dir, LEDGER))
            self.assertEqual(run_dir / ".locks" / LOCK, ACCOUNTING.lock_path(run_dir, LOCK))

            self.reserve(run_dir, "keepa", now=T0)
            self.assertTrue((run_dir / ".locks").is_dir())


class AcademicLedgerCompatibilityTests(AccountingTestCase):
    """The migration guard: discovery's ledger must be one configuration of this module.

    ``discover_sources.py`` keeps a durable academic provider-call ledger today.
    Unit 8 replaces that machinery with calls into this module, and the change
    is only behaviour-preserving if both writers can share one file: a workspace
    part-way through a run must not find its existing ledger unreadable, and the
    budget must not restart at zero on the first invocation after the upgrade.
    These cases drive both implementations against the same bytes.
    """

    @classmethod
    def setUpClass(cls):
        cls.discover = load_script_module("provider_accounting_discover_sources", "discover_sources.py")

    def academic_run(self, root: Path, run_id: str = "run-academic") -> Path:
        run_dir = root / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / ACADEMIC_LEDGER).write_text("", encoding="utf-8")
        (run_dir / "run-state.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "run_id": run_id,
                    "state": {"current": "discovering"},
                    "academic_provider_request_accounting": {
                        "schema_version": "1.0",
                        "ledger_path": f"runs/{run_id}/{ACADEMIC_LEDGER}",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return run_dir

    def discovery_context(self, root: Path, run_id: str, limit: int = 25) -> dict:
        return {
            "project_root": root,
            "run_id": run_id,
            "limit": limit,
            "command": "academic",
            "scope_id": "req-paper-1234567890",
            "network_io_executed": False,
        }

    def academic_reserve(self, run_dir: Path, provider: str, *, attempt: int = 1, limit: int = 25, now=T0):
        """Reserve through this module using discovery's ledger configuration."""
        return ACCOUNTING.reserve(
            run_dir,
            provider,
            1,
            ledger_filename=ACADEMIC_LEDGER,
            lock_filename=ACADEMIC_LOCK,
            schema_version=self.discover.SCHEMA_VERSION,
            per_run_max=limit,
            now=now,
            extra_fields={
                "event_type": ACADEMIC_EVENT_TYPE,
                "command": "academic",
                "scope_id": "req-paper-1234567890",
                "attempt": attempt,
                "budget_consumed": True,
            },
        )

    def test_the_constants_this_module_is_configured_with_are_discoverys_own(self):
        self.assertEqual(ACADEMIC_LEDGER, self.discover.ACADEMIC_PROVIDER_REQUESTS_FILENAME)
        self.assertEqual(ACADEMIC_LOCK, self.discover.ACADEMIC_PROVIDER_REQUESTS_LOCK_FILENAME)
        self.assertEqual(self.discover.SCHEMA_VERSION, ACCOUNTING.ACCOUNTING_SCHEMA_VERSION)
        self.assertEqual(25, self.discover.DEFAULT_MAX_ACADEMIC_PROVIDER_REQUESTS_PER_RUN)

    def test_this_module_writes_records_discovery_reads_as_valid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = self.academic_run(root)
            self.academic_reserve(run_dir, "arxiv")
            self.academic_reserve(run_dir, "openalex", attempt=2)

            events = self.discover.load_academic_provider_request_events(root, "run-academic")

            self.assertEqual(2, len(events))
            self.assertEqual(["arxiv", "openalex"], [event["provider"] for event in events])
            self.assertEqual({ACADEMIC_EVENT_TYPE}, {event["event_type"] for event in events})
            self.assertEqual(2, self.discover.academic_provider_request_count(root, "run-academic"))

    def test_this_module_reads_records_discovery_wrote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = self.academic_run(root)
            context = self.discovery_context(root, "run-academic")
            written = [
                self.discover.reserve_academic_provider_request(context, provider="arxiv", attempt=1),
                self.discover.reserve_academic_provider_request(context, provider="openalex", attempt=1),
            ]

            events = ACCOUNTING.load_events(run_dir, ledger_filename=ACADEMIC_LEDGER)
            arxiv = ACCOUNTING.usage(run_dir, "arxiv", ledger_filename=ACADEMIC_LEDGER)

            self.assertEqual(2, len(events))
            self.assertEqual([record["call_id"] for record in written], [event.call_id for event in events])
            self.assertEqual(1, len(arxiv))
            self.assertEqual("run-academic", arxiv[0].run_id)

    def test_both_writers_draw_down_one_shared_budget_in_one_ledger(self):
        """The mid-run upgrade case: records written before and after interleave."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = self.academic_run(root)
            context = self.discovery_context(root, "run-academic", limit=4)

            self.discover.reserve_academic_provider_request(context, provider="arxiv", attempt=1)
            self.academic_reserve(run_dir, "openalex", limit=4)
            self.discover.reserve_academic_provider_request(context, provider="arxiv", attempt=2)
            self.academic_reserve(run_dir, "openalex", limit=4)

            self.assertEqual(4, self.discover.academic_provider_request_count(root, "run-academic"))
            self.assertEqual(4, len(ACCOUNTING.load_events(run_dir, ledger_filename=ACADEMIC_LEDGER)))

            # Each implementation refuses the fifth, on the same shared count.
            with self.assertRaises(ACCOUNTING.ProviderAccountingError) as mine:
                self.academic_reserve(run_dir, "openalex", limit=4)
            self.assertEqual("per_run_max", mine.exception.details["ceiling"])
            self.assertEqual(4, mine.exception.details["used"])

            with self.assertRaises(self.discover.DiscoverSourcesError) as theirs:
                self.discover.reserve_academic_provider_request(context, provider="arxiv", attempt=3)
            self.assertEqual("ACADEMIC_PROVIDER_REQUEST_BUDGET_EXCEEDED", theirs.exception.error_code)
            self.assertEqual(4, theirs.exception.details["used"])

    def test_the_two_writers_produce_the_same_on_disk_framing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = self.academic_run(root)
            self.discover.reserve_academic_provider_request(
                self.discovery_context(root, "run-academic"),
                provider="arxiv",
                attempt=1,
            )
            self.academic_reserve(run_dir, "arxiv")

            lines = (run_dir / ACADEMIC_LEDGER).read_text(encoding="utf-8").splitlines()
            self.assertEqual(2, len(lines))
            theirs, mine = (json.loads(line) for line in lines)

            # Same keys, same values for everything except the fields that are
            # unique to a call by construction.
            self.assertEqual(set(theirs), set(mine))
            volatile = {"call_id", "reserved_at"}
            self.assertEqual(
                {key: value for key, value in theirs.items() if key not in volatile},
                {key: value for key, value in mine.items() if key not in volatile},
            )
            # Compact separators, no sorted-key rewriting, one object per line.
            for line in lines:
                with self.subTest(line=line[:40]):
                    self.assertNotIn(", ", line)
                    self.assertTrue(line.startswith('{"schema_version":"1.0","event_type":"'))

    def test_an_existing_ledger_keeps_its_budget_across_the_migration(self):
        """A workspace mid-run must not get a fresh allowance from the upgrade."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = self.academic_run(root)
            context = self.discovery_context(root, "run-academic", limit=3)
            for attempt in range(1, 4):
                self.discover.reserve_academic_provider_request(context, provider="arxiv", attempt=attempt)

            with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
                self.academic_reserve(run_dir, "arxiv", limit=3)

            self.assertEqual("ACQUISITION_PROVIDER_RATE_LIMITED", ctx.exception.error_code)
            self.assertEqual(3, ctx.exception.details["used"])
            self.assertEqual(3, self.discover.academic_provider_request_count(root, "run-academic"))

    def test_a_declared_rate_limit_tightens_the_academic_run_budget(self):
        """What T6 adds on top of the existing academic accounting."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = self.academic_run(root)
            self.academic_reserve(run_dir, "arxiv", limit=25)

            with self.assertRaises(ACCOUNTING.ProviderAccountingError) as ctx:
                ACCOUNTING.reserve(
                    run_dir,
                    "arxiv",
                    1,
                    ledger_filename=ACADEMIC_LEDGER,
                    lock_filename=ACADEMIC_LOCK,
                    schema_version=self.discover.SCHEMA_VERSION,
                    per_run_max=25,
                    rate_limit={"requests": 1, "per": "minute"},
                    now=T0 + timedelta(seconds=30),
                    extra_fields={"event_type": ACADEMIC_EVENT_TYPE},
                )

            self.assertEqual("rate_limit", ctx.exception.details["ceiling"])
            self.assertEqual(1, self.discover.academic_provider_request_count(root, "run-academic"))


if __name__ == "__main__":
    unittest.main()
