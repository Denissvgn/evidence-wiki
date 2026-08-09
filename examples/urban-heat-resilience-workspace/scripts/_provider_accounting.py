#!/usr/bin/env python3
"""Durable, run-scoped provider-call accounting: one ledger, two ceilings.

A provider budget that lives in memory is not a budget. A process that crashes
mid-run, an operator who reruns a command, a host that drives three scripts in
sequence — each would start from zero, and the ceiling an operator reviewed would
be whatever the longest-lived process happened to remember. So the ledger is a
file: an append-only JSONL record under the active run directory, guarded by the
same workspace lock every other multi-writer store in this workspace uses, read
back from disk on every reservation.

The discipline this module generalizes already exists. ``discover_sources.py``
has kept a per-run ledger of academic provider calls since the academic
discovery route landed: ``runs/<run_id>/academic-provider-requests.jsonl``, one
compact JSON object per line, reserved *before* transport so a crash counts the
attempt rather than losing it. CR-5 needs the same guarantee for a registered
provider's declared ``rate_limit``, and a second ledger with its own subtly
different rules would be the beginning of two budgets that disagree. This module
is that machinery with the academic vocabulary lifted out: the caller names the
ledger file, the lock file, and the schema version, so the existing academic
ledger is one configuration of this module rather than a fork of it.

Two ceilings, enforced together, neither able to loosen the other:

``per_run_max``
    The cumulative cap for the run, counted over **every** record in the ledger
    regardless of which provider wrote it. This is what discovery's
    ``max_academic_provider_requests_per_run`` is today — a run-wide budget that
    arXiv and OpenAlex draw down together — and what an acquisition run's
    ``max_downloads_per_run`` is. The ledger file *is* the budget's scope.

``rate_limit``
    A rolling window over **this provider's** records only, because the limit is
    the provider's own declaration. Sixty requests per minute means the 61st
    request within any rolling minute is refused, computed from the timestamps
    on disk rather than from wall-clock buckets: a fixed bucket would let 120
    requests through in the two seconds either side of a minute boundary. A
    record whose timestamp is exactly one window old has left the window.

Both are checked before anything is appended, and the append is all-or-nothing:
a plan of three requests either reserves three slots or reserves none. Refusal
therefore happens strictly pre-transport, and the ledger never carries a record
for a request the caller was told it could not make.

Fail-closed on damage. A ledger that will not parse, carries a foreign schema
version, repeats a ``call_id``, or has been replaced by a symlink refuses loudly
instead of being treated as empty — a budget that resets when its ledger is
corrupted is not a budget, it is an invitation. The one thing this module cannot
notice on its own is deletion: an absent ledger is indistinguishable from a run
that has reserved nothing. Callers that need deletion to be fatal pin the
ledger's existence in run state, exactly as discovery's run-state accounting
marker does, and validate that marker before calling in here.

This module never imports ``evidence_wiki``. A declared ``rate_limit`` arrives
either as a mapping or as an object exposing ``requests``/``per``; both are read
duck-typed, so a workspace whose scripts are deployed without the package
installed accounts exactly as one whose scripts run beside it.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
# One lock implementation for every workspace store, so a reservation serializes
# against a concurrent reservation the same way a candidate append serializes
# against a lifecycle rewrite.
from _workspace_locks import workspace_lock  # noqa: E402

ACCOUNTING_SCHEMA_VERSION = "1.0"

# Stable machine-readable codes. ACQUISITION_PROVIDER_RATE_LIMITED is the CR-5
# refusal code and is shared by both ceilings (details["ceiling"] says which
# one fired); the rest describe damage to, or misuse of, the ledger itself.
# A calling surface with its own established code for the same condition should
# map these onto it rather than leaking a second vocabulary to hosts.
ERROR_RATE_LIMITED = "ACQUISITION_PROVIDER_RATE_LIMITED"
ERROR_LEDGER_INVALID = "PROVIDER_ACCOUNTING_LEDGER_INVALID"
ERROR_WRITE_FAILED = "PROVIDER_ACCOUNTING_WRITE_FAILED"
ERROR_ARGUMENT_INVALID = "PROVIDER_ACCOUNTING_ARGUMENT_INVALID"

ACCOUNTING_REMEDIATION = (
    "Preserve the affected run for audit, restore its provider-call ledger from a trusted backup, or start "
    "a fresh run. Do not reset, deduplicate, or hand-edit provider accounting."
)

# Same rendering as every other timestamp this workspace writes: UTC, whole
# seconds, trailing Z. Two reservations in the same second share a timestamp,
# which rounds a rolling window in the budget's favour rather than the caller's.
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

RATE_LIMIT_WINDOWS: dict[str, int] = {"minute": 60, "hour": 3600}
RATE_LIMIT_PERIODS = tuple(RATE_LIMIT_WINDOWS)

DEFAULT_EVENT_TYPE = "provider_request"

# The lock lives beside the run's other lock files, matching the layout
# discovery already writes: runs/<run_id>/.locks/<lock_filename>.
LOCK_DIRECTORY_NAME = ".locks"

# Identity fields the module owns outright. A caller that could set ``run_id``
# or ``reserved_at`` through ``extra_fields`` could also backdate its own way
# out of a window, so these are refused rather than merged.
PROTECTED_RECORD_KEYS = frozenset({"schema_version", "call_id", "run_id", "provider", "reserved_at"})


class ProviderAccountingError(Exception):
    """Structured provider-accounting failure carrying a stable machine-readable code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        remediation: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.remediation = remediation or ACCOUNTING_REMEDIATION
        self.details = details or {}


@dataclass(frozen=True)
class RateLimit:
    """A declared rolling-window ceiling, normalized from whatever the caller passed."""

    requests: int
    per: str

    @property
    def window_seconds(self) -> int:
        return RATE_LIMIT_WINDOWS[self.per]

    @property
    def window(self) -> timedelta:
        return timedelta(seconds=self.window_seconds)


@dataclass(frozen=True)
class Event:
    """One reserved provider call, as read back from the ledger."""

    provider_id: str
    run_id: str
    call_id: str
    reserved_at: str
    moment: datetime
    record: Mapping[str, Any]


@dataclass(frozen=True)
class Reservation:
    """The outcome of a successful, already-durable reservation."""

    provider_id: str
    run_id: str
    ledger_path: Path
    reserved_at: str
    events: tuple[Event, ...]
    per_run_used: int
    per_run_max: int | None
    window_used: int | None
    rate_limit: RateLimit | None

    @property
    def count(self) -> int:
        return len(self.events)


# --- Validation quartet: arguments, filenames, declarations, records ---------


def _argument_error(message: str, details: dict[str, Any] | None = None) -> ProviderAccountingError:
    return ProviderAccountingError(
        ERROR_ARGUMENT_INVALID,
        message,
        remediation=(
            "Fix the reservation call: pass the active run directory, a provider id, a positive request "
            "count, bare ledger and lock filenames, and a well-formed declared rate limit."
        ),
        details={**(details or {}), "network_io_executed": False},
    )


def _validate_run_dir(run_dir: Any) -> Path:
    if not isinstance(run_dir, (str, Path)):
        raise _argument_error(f"run_dir must be a path, not {type(run_dir).__name__}")
    # ``Path("")`` silently becomes ``Path(".")``, which would account a run's
    # calls against the working directory. Refuse the empty string outright.
    if not str(run_dir).strip():
        raise _argument_error("run_dir must be a non-empty path")
    return Path(run_dir)


def _run_id_for(run_dir: Path) -> str:
    """Derive the run id from the run directory, the way the layout defines it."""
    run_id = run_dir.name
    if not run_id or run_id in {".", ".."}:
        run_id = run_dir.resolve().name
    if not run_id:
        raise _argument_error(
            f"Cannot derive a run id from run_dir {run_dir.as_posix()!r}",
            {"run_dir": run_dir.as_posix()},
        )
    return run_id


def _validate_filename(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _argument_error(f"{label} must be a non-empty string")
    name = value.strip()
    # A ledger or lock name is a leaf inside the run directory. Anything with a
    # separator or a parent reference would let a caller account one run's calls
    # against another run's budget.
    separators = {"/", "\\", os.sep, os.altsep or os.sep}
    if name in {".", ".."} or Path(name).name != name or any(separator in name for separator in separators):
        raise _argument_error(f"{label} must be a bare filename without path separators; rejected {value!r}")
    return name


def _validate_provider_id(provider_id: Any) -> str:
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise _argument_error("provider_id must be a non-empty string")
    value = provider_id.strip()
    if any(character.isspace() or ord(character) < 0x20 for character in value):
        raise _argument_error(f"provider_id must not contain whitespace or control characters; rejected {provider_id!r}")
    return value


def _validate_count(count: Any) -> int:
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise _argument_error(f"count must be a positive integer; rejected {count!r}")
    return count


def _validate_schema_version(schema_version: Any) -> str:
    if not isinstance(schema_version, str) or not schema_version.strip():
        raise _argument_error("schema_version must be a non-empty string")
    return schema_version.strip()


def _validate_per_run_max(per_run_max: Any) -> int | None:
    if per_run_max is None:
        return None
    if isinstance(per_run_max, bool) or not isinstance(per_run_max, int) or per_run_max < 0:
        raise _argument_error(f"per_run_max must be a non-negative integer or None; rejected {per_run_max!r}")
    return per_run_max


def coerce_rate_limit(rate_limit: Any) -> RateLimit | None:
    """Read a declared rate limit from a mapping or an attribute-carrying object.

    The package's ``RateLimit`` dataclass and a plain ``{"requests": 60, "per":
    "minute"}`` mapping are both accepted, because workspace scripts must keep
    working in a deployment where only the scripts exist.
    """
    if rate_limit is None:
        return None
    if isinstance(rate_limit, RateLimit):
        return rate_limit
    if isinstance(rate_limit, Mapping):
        if "requests" not in rate_limit or "per" not in rate_limit:
            raise _argument_error("rate_limit mapping must declare 'requests' and 'per'")
        requests = rate_limit["requests"]
        per = rate_limit["per"]
    else:
        requests = getattr(rate_limit, "requests", None)
        per = getattr(rate_limit, "per", None)
        if requests is None or per is None:
            raise _argument_error(
                f"rate_limit must expose 'requests' and 'per'; rejected {type(rate_limit).__name__}"
            )
    if isinstance(requests, bool) or not isinstance(requests, int) or requests < 1:
        raise _argument_error(f"rate_limit.requests must be a positive integer; rejected {requests!r}")
    if not isinstance(per, str) or per not in RATE_LIMIT_WINDOWS:
        raise _argument_error(
            f"rate_limit.per must be one of {RATE_LIMIT_PERIODS}; rejected {per!r}",
            {"accepted": list(RATE_LIMIT_PERIODS)},
        )
    return RateLimit(requests=requests, per=per)


def _validate_extra_fields(extra_fields: Any) -> dict[str, Any]:
    if extra_fields is None:
        return {}
    if not isinstance(extra_fields, Mapping):
        raise _argument_error(f"extra_fields must be a mapping; rejected {type(extra_fields).__name__}")
    extras: dict[str, Any] = {}
    for key, value in extra_fields.items():
        if not isinstance(key, str) or not key.strip():
            raise _argument_error(f"extra_fields keys must be non-empty strings; rejected {key!r}")
        name = key.strip()
        if name in PROTECTED_RECORD_KEYS:
            raise _argument_error(
                f"extra_fields may not set {name!r}; this module owns the record's identity fields",
                {"protected_keys": sorted(PROTECTED_RECORD_KEYS)},
            )
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise _argument_error(f"extra_fields[{name!r}] must be JSON-serializable: {exc}") from exc
        extras[name] = value
    return extras


def _coerce_now(now: Any) -> datetime:
    """Resolve the injectable clock to a UTC instant truncated to whole seconds."""
    if now is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        moment = now
    elif callable(now):
        produced = now()
        if not isinstance(produced, datetime):
            raise _argument_error("now() must return a datetime")
        moment = produced
    else:
        raise _argument_error(f"now must be a datetime, a callable returning one, or None; rejected {now!r}")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0)


# --- Ledger paths and timestamps --------------------------------------------


def ledger_path(run_dir: str | Path, ledger_filename: str) -> Path:
    """Return the ledger file for a run: ``<run_dir>/<ledger_filename>``."""
    directory = _validate_run_dir(run_dir)
    return directory / _validate_filename(ledger_filename, label="ledger_filename")


def lock_path(run_dir: str | Path, lock_filename: str) -> Path:
    """Return the reservation lock file: ``<run_dir>/.locks/<lock_filename>``."""
    directory = _validate_run_dir(run_dir)
    return directory / LOCK_DIRECTORY_NAME / _validate_filename(lock_filename, label="lock_filename")


def format_timestamp(moment: datetime) -> str:
    """Render an instant the way every record in these ledgers is written."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def parse_timestamp(value: Any) -> datetime | None:
    """Read a recorded timestamp back, or return None when it is unusable."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _compact_json(record: Mapping[str, Any]) -> str:
    return json.dumps(dict(record), separators=(",", ":"), sort_keys=False, allow_nan=False)


# --- Reading the ledger ------------------------------------------------------


def _ledger_invalid(message: str, details: dict[str, Any]) -> ProviderAccountingError:
    return ProviderAccountingError(
        ERROR_LEDGER_INVALID,
        message,
        remediation=ACCOUNTING_REMEDIATION,
        details={**details, "network_io_executed": False},
    )


def _validate_ledger_file(path: Path, *, run_id: str) -> bool:
    """Refuse a ledger that is not a plain, singly linked regular file.

    Returns False when the ledger simply does not exist yet. A symlink or a hard
    link is refused rather than followed: either would let a budget be written
    somewhere the run does not own, or read from somewhere it does not control.
    """
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise _ledger_invalid(
            f"Cannot inspect provider-call ledger {path.as_posix()}: {exc}",
            {"run_id": run_id, "ledger_path": path.as_posix()},
        ) from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or int(getattr(metadata, "st_nlink", 1) or 1) != 1:
        raise _ledger_invalid(
            f"Provider-call ledger must be a singly linked regular file: {path.as_posix()}",
            {"run_id": run_id, "ledger_path": path.as_posix()},
        )
    return True


def load_events(
    run_dir: str | Path,
    *,
    ledger_filename: str,
    schema_version: str = ACCOUNTING_SCHEMA_VERSION,
) -> tuple[Event, ...]:
    """Read every reservation in a run's ledger, refusing loudly on damage.

    Every record is validated: it must be a JSON object at the expected schema
    version, bound to this run, naming a provider, carrying a unique non-empty
    ``call_id`` and a parseable UTC timestamp. Anything else raises rather than
    being skipped, because a skipped record is a slot silently handed back.
    """
    directory = _validate_run_dir(run_dir)
    run_id = _run_id_for(directory)
    expected_version = _validate_schema_version(schema_version)
    path = ledger_path(directory, ledger_filename)
    details = {"run_id": run_id, "ledger_path": path.as_posix()}
    if not _validate_ledger_file(path, run_id=run_id):
        return ()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise _ledger_invalid(f"Cannot read provider-call ledger {path.as_posix()}: {exc}", details) from exc

    events: list[Event] = []
    seen_call_ids: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        located = {**details, "line": line_number}
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise _ledger_invalid(
                f"Invalid JSONL in {path.as_posix()} at line {line_number}: {exc}",
                located,
            ) from exc
        if not isinstance(record, dict):
            raise _ledger_invalid(f"Provider-call record at line {line_number} is not a JSON object.", located)
        if record.get("schema_version") != expected_version:
            raise _ledger_invalid(
                (
                    f"Provider-call record at line {line_number} declares schema_version "
                    f"{record.get('schema_version')!r}; this ledger is read at {expected_version!r}."
                ),
                {**located, "expected_schema_version": expected_version},
            )
        if record.get("run_id") != run_id:
            raise _ledger_invalid(
                (
                    f"Provider-call record at line {line_number} belongs to run "
                    f"{record.get('run_id')!r}, not {run_id!r}."
                ),
                located,
            )
        event_type = record.get("event_type")
        if not isinstance(event_type, str) or not event_type.strip():
            raise _ledger_invalid(f"Provider-call record at line {line_number} has no event_type.", located)
        provider = record.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            raise _ledger_invalid(f"Provider-call record at line {line_number} names no provider.", located)
        call_id = record.get("call_id")
        if not isinstance(call_id, str) or not call_id.strip():
            raise _ledger_invalid(f"Provider-call record at line {line_number} has no call_id.", located)
        call_id = call_id.strip()
        if call_id in seen_call_ids:
            raise _ledger_invalid(
                f"Duplicate provider call_id {call_id!r} in {path.as_posix()} at line {line_number}.",
                {**located, "call_id": call_id},
            )
        moment = parse_timestamp(record.get("reserved_at"))
        if moment is None:
            raise _ledger_invalid(
                (
                    f"Provider-call record at line {line_number} has an unreadable reserved_at "
                    f"{record.get('reserved_at')!r}."
                ),
                located,
            )
        seen_call_ids.add(call_id)
        events.append(
            Event(
                provider_id=provider.strip(),
                run_id=run_id,
                call_id=call_id,
                reserved_at=record["reserved_at"],
                moment=moment,
                record=MappingProxyType(dict(record)),
            )
        )
    return tuple(events)


def usage(run_dir: str | Path, provider_id: str, *, ledger_filename: str) -> tuple[Event, ...]:
    """Return one provider's reservations in this run, in the order they were made.

    The whole ledger is validated first — a record another provider wrote is
    still this run's accounting, and a ledger that cannot be trusted for one
    provider cannot be trusted for any. Records are read at this module's
    ``ACCOUNTING_SCHEMA_VERSION``; a caller pinning a different version reads
    through :func:`load_events` instead.
    """
    provider = _validate_provider_id(provider_id)
    events = load_events(run_dir, ledger_filename=ledger_filename)
    return tuple(event for event in events if event.provider_id == provider)


# --- Window arithmetic -------------------------------------------------------


def events_in_window(events: tuple[Event, ...], moment: datetime, limit: RateLimit) -> tuple[Event, ...]:
    """Return the events inside the rolling window that ends at ``moment``.

    The window is half-open: a reservation made exactly one window ago has aged
    out, so a 60-per-minute provider that spent its budget at 12:00:00 is free
    again at 12:01:00 rather than at 12:01:01.
    """
    threshold = moment - limit.window
    return tuple(event for event in events if event.moment > threshold)


def _window_clears_at(
    in_window: tuple[Event, ...],
    limit: RateLimit,
    *,
    count: int,
) -> datetime | None:
    """When enough reservations will have aged out to admit ``count`` more.

    None means the window never clears for this request: the caller asked for
    more slots than the declared limit holds, so waiting cannot help.
    """
    needed = len(in_window) + count - limit.requests
    if needed <= 0:
        return None
    ordered = sorted(event.moment for event in in_window)
    if needed > len(ordered):
        return None
    clears_at = ordered[needed - 1] + limit.window
    # Reported at whole-second resolution like every other timestamp here, so a
    # sub-second remainder in a hand-written record rounds up rather than
    # advertising a clearing time that has not arrived yet.
    if clears_at.microsecond:
        clears_at = clears_at.replace(microsecond=0) + timedelta(seconds=1)
    return clears_at


# --- Reserving ---------------------------------------------------------------


def _refuse_per_run(
    *,
    provider_id: str,
    run_id: str,
    path: Path,
    used: int,
    count: int,
    per_run_max: int,
) -> ProviderAccountingError:
    return ProviderAccountingError(
        ERROR_RATE_LIMITED,
        (
            f"Run {run_id} has already reserved {used} provider request(s); reserving {count} more for "
            f"{provider_id} would exceed the per-run ceiling of {per_run_max}."
        ),
        remediation=(
            f"The per-run window is the run itself: it does not clear until a new run starts. Start a new run, "
            f"or raise the reviewed per-run provider request budget above {per_run_max}."
        ),
        details={
            "provider": provider_id,
            "run_id": run_id,
            "ceiling": "per_run_max",
            "requested": count,
            "used": used,
            "limit": per_run_max,
            "window": "run",
            "clears_at": None,
            "ledger_path": path.as_posix(),
            "network_io_executed": False,
        },
    )


def _refuse_rate_limit(
    *,
    provider_id: str,
    run_id: str,
    path: Path,
    used: int,
    count: int,
    limit: RateLimit,
    clears_at: datetime | None,
) -> ProviderAccountingError:
    cleared = format_timestamp(clears_at) if clears_at is not None else None
    if cleared is None:
        when = (
            f"Waiting cannot admit this request: {count} request(s) at once exceeds the declared ceiling of "
            f"{limit.requests} per {limit.per} on its own."
        )
    else:
        when = f"The rolling {limit.per} window clears at {cleared}; wait until then and retry."
    return ProviderAccountingError(
        ERROR_RATE_LIMITED,
        (
            f"Provider {provider_id} has reserved {used} request(s) in the current rolling {limit.per}; "
            f"reserving {count} more would exceed its declared rate limit of {limit.requests} per {limit.per}."
        ),
        remediation=(
            f"{when} Alternatively, reduce the request count, or declare a rate limit the service actually "
            f"permits — a declared limit only tightens this workspace's own budgets, it never raises them."
        ),
        details={
            "provider": provider_id,
            "run_id": run_id,
            "ceiling": "rate_limit",
            "requested": count,
            "used": used,
            "limit": limit.requests,
            "window": limit.per,
            "window_seconds": limit.window_seconds,
            "clears_at": cleared,
            "ledger_path": path.as_posix(),
            "network_io_executed": False,
        },
    )


def _build_record(
    *,
    schema_version: str,
    call_id: str,
    run_id: str,
    provider_id: str,
    reserved_at: str,
    extras: dict[str, Any],
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": schema_version,
        "event_type": DEFAULT_EVENT_TYPE,
        "call_id": call_id,
        "run_id": run_id,
        "provider": provider_id,
        "reserved_at": reserved_at,
        "budget_consumed": True,
    }
    # ``extra_fields`` may restate ``event_type`` or ``budget_consumed`` — a
    # caller keeping an established record vocabulary needs that — and dict
    # update keeps a restated key in its canonical position. Identity fields
    # were refused during validation.
    record.update(extras)
    return record


def reserve(
    run_dir: str | Path,
    provider_id: str,
    count: int,
    *,
    ledger_filename: str,
    lock_filename: str,
    schema_version: str,
    per_run_max: int | None = None,
    rate_limit: Any = None,
    now: datetime | Callable[[], datetime] | None = None,
    extra_fields: Mapping[str, Any] | None = None,
) -> Reservation:
    """Durably consume ``count`` provider-call slots immediately before transport.

    Both ceilings are checked against the ledger as it exists on disk, under the
    run's reservation lock, and the records are appended before this returns —
    so a process that dies between here and the network has already spent what
    it was about to spend. That asymmetry is deliberate: over-counting a crashed
    attempt is a smaller failure than letting a crash loop spend an unbounded
    budget.

    ``per_run_max`` is checked first because it is the workspace's own budget;
    ``rate_limit`` is the provider's declaration and can only tighten it
    further. Either may be None. Refusal raises
    ``ACQUISITION_PROVIDER_RATE_LIMITED`` with ``details["ceiling"]`` naming
    which one fired, and writes nothing at all — a refused overage leaves no
    record, so retrying after the window clears is honest rather than
    double-charged.

    A run whose reservation lock cannot be acquired raises
    ``LockUnavailableError`` from ``_workspace_locks`` unchanged, so a caller
    reports lock contention with the same envelope it already uses for every
    other workspace store.
    """
    directory = _validate_run_dir(run_dir)
    run_id = _run_id_for(directory)
    provider = _validate_provider_id(provider_id)
    requested = _validate_count(count)
    version = _validate_schema_version(schema_version)
    ceiling = _validate_per_run_max(per_run_max)
    limit = coerce_rate_limit(rate_limit)
    extras = _validate_extra_fields(extra_fields)
    moment = _coerce_now(now)
    reserved_at = format_timestamp(moment)

    path = ledger_path(directory, ledger_filename)
    lock = lock_path(directory, lock_filename)

    with workspace_lock(lock, purpose=f"provider-call budget for {run_id}"):
        events = load_events(directory, ledger_filename=ledger_filename, schema_version=version)
        used = len(events)
        if ceiling is not None and used + requested > ceiling:
            raise _refuse_per_run(
                provider_id=provider,
                run_id=run_id,
                path=path,
                used=used,
                count=requested,
                per_run_max=ceiling,
            )

        in_window: tuple[Event, ...] = ()
        if limit is not None:
            provider_events = tuple(event for event in events if event.provider_id == provider)
            in_window = events_in_window(provider_events, moment, limit)
            if len(in_window) + requested > limit.requests:
                raise _refuse_rate_limit(
                    provider_id=provider,
                    run_id=run_id,
                    path=path,
                    used=len(in_window),
                    count=requested,
                    limit=limit,
                    clears_at=_window_clears_at(in_window, limit, count=requested),
                )

        records = [
            _build_record(
                schema_version=version,
                call_id=f"{provider}-call-{uuid.uuid4().hex}",
                run_id=run_id,
                provider_id=provider,
                reserved_at=reserved_at,
                extras=extras,
            )
            for _ in range(requested)
        ]
        payload = "".join(f"{_compact_json(record)}\n" for record in records)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # One write for the whole reservation: a plan of three requests must
            # not be able to leave one slot spent and two lost.
            with path.open("a", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise ProviderAccountingError(
                ERROR_WRITE_FAILED,
                f"Cannot persist the provider-call reservation for run {run_id}: {exc}",
                remediation="Restore workspace write access before retrying; no provider transport was invoked.",
                details={
                    "provider": provider,
                    "run_id": run_id,
                    "requested": requested,
                    "ledger_path": path.as_posix(),
                    "network_io_executed": False,
                },
            ) from exc

        reserved = tuple(
            Event(
                provider_id=provider,
                run_id=run_id,
                call_id=record["call_id"],
                reserved_at=reserved_at,
                moment=moment,
                record=MappingProxyType(dict(record)),
            )
            for record in records
        )
        return Reservation(
            provider_id=provider,
            run_id=run_id,
            ledger_path=path,
            reserved_at=reserved_at,
            events=reserved,
            per_run_used=used + requested,
            per_run_max=ceiling,
            window_used=(len(in_window) + requested) if limit is not None else None,
            rate_limit=limit,
        )
