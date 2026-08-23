#!/usr/bin/env python3
"""Manage durable, provider-aware parent orchestration sessions.

The controller is deliberately model-neutral.  It never launches an LLM or a
network transport.  Instead it derives one bounded work order from durable
workspace artifacts, persists it under ``runs/orchestrations/<id>/``, and
accepts a small structured result after independently checking the workspace.
The package-side host runner is responsible for handing work orders to Codex,
Claude, or another compatible agent process.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import shutil
import socket
import stat
import sys
import tempfile
import uuid
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from types import ModuleType, SimpleNamespace
from typing import Any

# Workspace sibling modules are protected static inputs.  Prevent controller
# imports from creating ``scripts/__pycache__`` entries that would mutate the
# very tree fingerprinted for pending-action integrity.
sys.dont_write_bytecode = True

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to manage research orchestration") from exc

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _orchestration_config import (
    ACQUISITION_MODE_DELEGATED,
    ACQUISITION_MODE_PROVIDERS,
    ACQUISITION_MODES,
    DEFAULT_MAX_ATTEMPTS_PER_REQUEST,
    MAX_MAX_ATTEMPTS_PER_REQUEST,
    OrchestrationConfigError,
    orchestration_config,
    valid_agent_id,
)
from _provider_plugins import registered_ids
from _provider_registry import ProviderListError, ProviderNotRegisteredError, validate_provider_ids
from _script_errors import emit_error, handle_system_exit, json_mode_requested
from _workspace_locks import LockUnavailableError, read_lock_holder, workspace_lock
from _workspace_module_loader import load_workspace_module
from source_failure_taxonomy import is_attempt_failure_code, is_retryable_attempt_failure_code

SCHEMA_VERSION = "1.0"
SESSION_ARTIFACT_TYPE = "orchestration_session"
WORK_ORDER_ARTIFACT_TYPE = "orchestration_work_order"
RESULT_ARTIFACT_TYPE = "orchestration_result"

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_BLOCKED = 3
EXIT_PAUSED = 4
#: A second driver already holds this session's lock.
#:
#: Distinct from ``EXIT_INVALID`` on purpose: this is the one refusal a host is
#: *supposed* to retry, and 2 already means "permanently refused" for a dozen
#: other conditions. A shell-only caller that can read nothing but ``$?`` must be
#: able to tell "come back in a moment" from "this will never succeed"; 3 and 4
#: already carry session outcomes, so contention gets its own code.
#:
#: 6, not 5. The package's own ``evidence-wiki orchestrate`` returns a controller
#: status verbatim for ``start``/``next``/``submit`` *and* returns 5 from
#: ``EXIT_RUNNER_FAILED`` for a managed ``run``/``resume`` whose runner failed or
#: whose control artifacts were tampered with. Sharing 5 would have made the one
#: code a caller must retry indistinguishable from one it must never retry --
#: exactly the confusion this constant exists to remove -- and ``EXIT_RUNNER_FAILED``
#: is already released, so the new code is the one that moves.
EXIT_DRIVER_BUSY = 6

#: How long the exclusive-create *fallback* backend may consider this session's
#: lock file abandoned, overriding the module-wide 900 s default at this one call
#: site (and only here -- every other workspace lock keeps the conservative
#: default).
#:
#: Only the last-resort fallback reads this: fcntl and msvcrt learn of a holder's
#: death from the OS, so on those platforms the value is inert. On a filesystem
#: with neither, a driver killed mid-call would otherwise block every successor
#: for fifteen minutes, which is indistinguishable from a hang for a host polling
#: `next`. Two minutes is short enough to recover inside a human's patience and
#: long enough to be safe: the fallback's heartbeat renews the lock every
#: ``min(60, stale/3)`` seconds, so a live holder refreshes three times inside
#: this window before a peer could consider it stale.
DRIVER_LOCK_STALE_FALLBACK_SECONDS = 120.0

#: Longest ``--agent-id`` accepted, and the cap applied when publishing one to a
#: peer. Shared so the validating and the sanitizing path cannot disagree about
#: how much of a caller-supplied id is allowed to travel.
MAX_AGENT_ID_LENGTH = 160

DEFAULT_MAX_ACTIONS = 12
DEFAULT_ACTION_TIMEOUT_SECONDS = 30 * 60
DEFAULT_TOTAL_TIMEOUT_SECONDS = 2 * 60 * 60
MAX_RESULT_BYTES = 64 * 1024
MAX_SUMMARY_LENGTH = 4000
MAX_ARTIFACTS = 256
MAX_SCOPE_IDS = 256
MAX_SCOPE_ID_LENGTH = 200
MAX_REASON_SLUGS = 5
AWAITING_REVIEW_TERMINAL_REASON = "All remaining questions await human review"
# Stable prefix a host may match to distinguish "the acquirer kept failing" from the other
# reasons a session ends blocked_on_sources. The machine-readable detail travels in the
# session_finished event's `exhausted_requests` map.
DELEGATED_EXHAUSTED_TERMINAL_REASON = "Delegated acquisition exhausted its attempts for every open source request"
MAX_ARTIFACT_PATH_LENGTH = 512
MAX_TRUSTED_STATIC_INPUT_BYTES = 32 * 1024 * 1024
MAX_TRUSTED_STATIC_INPUT_ENTRIES = 10_000
MAX_TRUSTED_STATIC_FINGERPRINT_BYTES = 8 * 1024 * 1024
MAX_TRUSTED_STATIC_PATH_LENGTH = 1024
MAX_TRUSTED_STATIC_INPUT_DIFFERENCES = 50
MAX_JSON_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_VERIFICATION_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_RAW_TREE_SNAPSHOT_BYTES = 2 * 1024 * 1024 * 1024
MAX_RAW_TREE_SNAPSHOT_ENTRIES = 10_000
MAX_SCOPE_GUARD_BYTES = 8 * 1024 * 1024
MAX_SCOPE_GUARD_ENTRIES = 10_000
MAX_WORK_ORDER_BYTES = 256 * 1024
#: Bound on the throwaway workspace a re-derivation that must run an external
#: normalizer adapter is confined to. It holds the trusted static inputs plus the one
#: record's own raw evidence, so a workspace whose adapter needs more than this is
#: refused rather than verified against the live tree.
MAX_REDERIVATION_SANDBOX_BYTES = 256 * 1024 * 1024
MAX_REDERIVATION_SANDBOX_FILES = 20_000

SESSION_FILENAME = "session.json"
EVENTS_FILENAME = "events.jsonl"
ANSWERS_FILENAME = "answers.json"
WORK_ORDERS_DIR = "work-orders"
WORK_RESULTS_DIR = "work-results"
TRUSTED_INPUTS_DIR = "trusted-inputs"
CONTROL_REPAIR_GUARDS_DIR = "orchestration-guards"

TRUSTED_STATIC_FILE_PATHS = (
    "research.yml",
    "workspace-system.yml",
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    ".gitignore",
)
TRUSTED_STATIC_TREE_PATHS = ("scripts", "skills", "docs")
#: Subtrees under the trusted trees that are inspected for unsafe entries but not
#: fingerprinted.
#:
#: ``scripts/__pycache__`` holds bytecode CPython generates from the very ``.py``
#: files this fingerprint already covers, so it carries no trust of its own -- and
#: treating it as a trusted input made the documented workflow refuse itself. A
#: work order tells an agent to run ``scripts/*.py`` directly; doing so writes
#: ``.pyc`` files; the following ``submit`` then reported the workspace as tampered
#: and listed the bytecode as the evidence. Only this controller passed ``-B``, so
#: only this controller was exempt.
#:
#: Excluded is not uninspected: ``validate_trusted_static_carveouts`` still walks
#: this path and refuses a symlink, a special file, or a multiply-linked entry
#: inside it. What changes is that its contents do not move the fingerprint, so a
#: genuine edit to any ``scripts/*.py`` is still detected exactly as before.
TRUSTED_STATIC_EXCLUDED_SUBTREES: frozenset[str] = frozenset({"scripts/__pycache__"})

RECOVERY_NONE = "none"
RECOVERY_RECONCILE = "reconcile_required"
RECOVERY_FINALIZING = "finalizing_submission"
RECOVERY_STATES = frozenset({RECOVERY_NONE, RECOVERY_RECONCILE, RECOVERY_FINALIZING})

ACTIVE_STATUS = "active"
PAUSED_STATUS = "paused"
TERMINAL_STATUSES = frozenset({"complete", "blocked_on_sources", "no_ship", "failed"})
RESULT_OUTCOMES = frozenset({"completed", "blocked", "failed"})
REVIEWABLE_CANDIDATE_STATES = frozenset({"new", "proposed", "discovered", "reviewed", "deferred"})
DISCOVERY_APPEND_CANDIDATE_STATES = frozenset({"new", "proposed", "discovered"})
PHASES = frozenset(
    {
        "planning",
        "research",
        "discovery",
        "candidate_review",
        "acquisition",
        "verification",
        "complete",
        "blocked_on_sources",
        "no_ship",
        "failed",
        "paused",
    }
)
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

_SIBLING_CACHE: dict[str, ModuleType] = {}


class OrchestrationControllerError(Exception):
    """A refused orchestration operation with a stable machine error code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        exit_code: int = EXIT_INVALID,
        recoverable: bool | None = None,
        remediation: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.exit_code = exit_code
        self.recoverable = recoverable
        self.remediation = remediation
        self.details = details


def load_sibling_module(stem: str) -> ModuleType:
    if stem not in _SIBLING_CACHE:
        _SIBLING_CACHE[stem] = load_workspace_module(_SCRIPT_DIR, stem)
    return _SIBLING_CACHE[stem]


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_wait_seconds(value: str) -> float:
    """Parse a non-negative wait, the float analogue of ``parse_positive_int``.

    Zero is admitted and is the default: "do not wait" is the whole point of the
    flag this parses, so it cannot be spelled as an error. Negative values are
    refused rather than clamped, because a caller that typed ``-30`` meant
    something the controller cannot honour and should hear so at the CLI boundary
    instead of silently receiving the immediate-refusal behaviour. Infinities and
    NaN are refused for the same reason ``float("inf")`` is not a timeout: they
    would turn a bounded wait into a hang, and NaN would make every deadline
    comparison false.
    """
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a non-negative number of seconds") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative number of seconds")
    return parsed


DRIVER_WAIT_HELP = (
    "Wait up to SECONDS for another driver to release the session before refusing with "
    "ORCHESTRATION_DRIVER_BUSY. Default 0: refuse immediately."
)


def add_driver_wait_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--driver-wait-seconds",
        type=parse_wait_seconds,
        default=0.0,
        metavar="SECONDS",
        help=DRIVER_WAIT_HELP,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage durable EvidenceWiki orchestration sessions.")
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Create one parent orchestration session.")
    start.add_argument("--orchestration-id", default=None)
    start.add_argument("--agent-id", required=True)
    start.add_argument("--max-actions", type=parse_positive_int, default=DEFAULT_MAX_ACTIONS)
    start.add_argument(
        "--action-timeout-seconds",
        type=parse_positive_int,
        default=DEFAULT_ACTION_TIMEOUT_SECONDS,
    )
    start.add_argument(
        "--total-timeout-seconds",
        type=parse_positive_int,
        default=DEFAULT_TOTAL_TIMEOUT_SECONDS,
    )
    add_driver_wait_argument(start)
    start.add_argument("--format", choices=("text", "json"), default="text")

    next_parser = subparsers.add_parser("next", help="Issue or replay one persisted work order.")
    next_parser.add_argument("--orchestration-id", required=True)
    next_parser.add_argument("--agent-id", default=None)
    next_parser.add_argument("--resume", action="store_true")
    add_driver_wait_argument(next_parser)
    next_parser.add_argument("--format", choices=("text", "json"), default="text")

    submit = subparsers.add_parser("submit", help="Submit one structured work result.")
    submit.add_argument("--orchestration-id", required=True)
    submit.add_argument("--action-id", required=True)
    submit.add_argument("--result-file", required=True)
    submit.add_argument("--agent-id", default=None)
    add_driver_wait_argument(submit)
    submit.add_argument("--format", choices=("text", "json"), default="text")

    status = subparsers.add_parser("status", help="Read a parent orchestration session.")
    status.add_argument("--orchestration-id", default=None)
    status.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args(argv)


def require_safe_id(value: Any, label: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or not SAFE_ID_RE.fullmatch(normalized) or ".." in normalized:
        raise OrchestrationControllerError(
            "ORCHESTRATION_ID_INVALID" if label == "orchestration_id" else "ACTION_ID_INVALID",
            f"{label} must be a filename-safe identifier",
            details={label: value},
        )
    return normalized


def require_agent_id(value: Any) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if (
        not normalized
        or len(normalized) > MAX_AGENT_ID_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise OrchestrationControllerError("AGENT_ID_INVALID", "--agent-id must be a non-empty string")
    return normalized


def generated_orchestration_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    return f"orch-{stamp}-{uuid.uuid4().hex[:8]}"


def orchestration_root(project_root: Path) -> Path:
    return project_root / "runs" / "orchestrations"


def session_dir(project_root: Path, orchestration_id: str) -> Path:
    return orchestration_root(project_root) / orchestration_id


def session_path(project_root: Path, orchestration_id: str) -> Path:
    return session_dir(project_root, orchestration_id) / SESSION_FILENAME


def events_path(project_root: Path, orchestration_id: str) -> Path:
    return session_dir(project_root, orchestration_id) / EVENTS_FILENAME


def session_lock_path(project_root: Path, orchestration_id: str) -> Path:
    return session_dir(project_root, orchestration_id) / ".locks" / "session.lock"


#: Identity of the driver currently holding this process's session lock, or
#: ``None`` outside a ``driver_session_lock`` block.
#:
#: Module state rather than a threaded parameter because the two events that
#: record it (``action_issued``, ``action_completed``) are appended from deep
#: inside call chains that have no business growing a driver argument, and the
#: controller runs one command per process: there is exactly one driver identity
#: in flight at a time, established at the lock and torn down with it. Reading it
#: outside a lock block yields ``None``, so a direct call to ``record_event`` --
#: as tests make -- produces exactly the event it always did.
_ACTIVE_DRIVER: dict[str, Any] | None = None


def driver_hostname() -> str:
    """Return this host's name, or a placeholder when the OS will not say.

    ``gethostname`` is a syscall and can fail on a misconfigured container. This
    value is diagnostic only -- it names a lock holder in a refusal message -- so
    a failure to resolve it must not be allowed to abort the command that is
    merely trying to identify itself.
    """
    try:
        name = socket.gethostname()
    except OSError:  # pragma: no cover - platform dependent
        return "<unknown host>"
    return name or "<unknown host>"


def publishable_agent_id(agent_id: Any) -> str | None:
    """Reduce ``--agent-id`` to something safe to publish to a peer process.

    This *sanitizes*; it does not validate. ``next`` and ``submit`` accept the
    flag as optional and check ownership only after ``load_session``, inside the
    lock, so rejecting a bad id here would reorder those refusals and change
    which error code a caller sees. But the value reaches a sidecar that a
    *different* process reads and renders into its own stderr, and by then
    ``require_agent_id`` has not run on either side -- ``start`` passes an
    already-validated id while ``next`` and ``submit`` pass argv verbatim.

    So the published form drops C0/C1 control characters, which would otherwise
    let an id carry terminal escape sequences into a peer's refusal message, and
    caps length the way ``require_agent_id`` does. What survives is a hint about
    who is busy, never an authorization claim, and a value that renders as
    itself. An id that is blank, non-string, or entirely control characters
    publishes as ``null`` -- "the holder did not record an agent".
    """
    if not isinstance(agent_id, str):
        return None
    printable = "".join(
        character
        for character in agent_id.strip()
        if not (ord(character) < 32 or 127 <= ord(character) <= 159)
    )
    return printable[:MAX_AGENT_ID_LENGTH] or None


def driver_identity(command: str, agent_id: Any) -> dict[str, Any]:
    """Build the holder block published beside a held session lock.

    ``acquired_at`` is stamped when this is called, and the only caller calls it
    *after* the lock is held -- see ``driver_session_lock``. Stamping it at
    attempt time instead would under-report a queued driver's start by the whole
    wait, and this field is exactly what an operator or a supervisor thresholds
    on to decide whether a holder is stuck.
    """
    return {
        "agent_id": publishable_agent_id(agent_id),
        "pid": os.getpid(),
        "hostname": driver_hostname(),
        "command": command,
        "acquired_at": timestamp_utc(),
    }


def event_driver_block() -> dict[str, Any] | None:
    """Return the audit block naming the driver that appended an event.

    Post-hoc visibility only (CR-8 D14). It records *who wrote this*, so that an
    interleaving that slipped past the per-invocation lock -- two hosts taking
    turns under a long ``--driver-wait-seconds``, say -- is legible afterwards in
    ``events.jsonl`` instead of being reconstructed from timestamps. It is not a
    fencing token and nothing reads it back to decide whether a write is allowed;
    span-level enforcement is deliberately deferred.
    """
    if _ACTIVE_DRIVER is None:
        return None
    block: dict[str, Any] = {
        "pid": _ACTIVE_DRIVER["pid"],
        "hostname": _ACTIVE_DRIVER["hostname"],
    }
    agent_id = _ACTIVE_DRIVER.get("agent_id")
    if agent_id:
        block["agent_id"] = agent_id
    return block


def event_data_with_driver(data: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Return ``data`` with the active driver's audit block folded in.

    Returns ``data`` untouched when no driver lock is held, so an event appended
    outside a driver command carries exactly the payload it always carried.
    """
    driver = event_driver_block()
    if driver is None:
        return data
    return {**(data or {}), "driver": driver}


def rendered_holder_field(holder: dict[str, Any], key: str, fallback: str) -> str:
    """Render one holder field for a human-readable refusal, never raising.

    The holder sidecar is written by a *peer* process, so this treats it as
    untrusted input to a message: a missing key, a null, an empty string, or a
    value of the wrong type all fall back rather than propagate, and anything
    long is truncated so a corrupt sidecar cannot turn a one-line refusal into a
    wall of text. The refusal must render even when nothing about the holder is
    knowable -- a controller that crashed while explaining a failure tells the
    caller nothing at all.
    """
    value = holder.get(key)
    if value is None or value == "" or isinstance(value, (dict, list)):
        return fallback
    # Control characters are stripped, not just escaped: this string is written
    # to a terminal by a process that did not author it, and an id carrying an
    # escape sequence would otherwise drive the reader's terminal. The writing
    # side sanitizes too; doing it again here is what makes the guarantee hold
    # for a sidecar written by any other version, or by hand.
    text = "".join(
        character for character in str(value) if not (ord(character) < 32 or 127 <= ord(character) <= 159)
    )
    return text if len(text) <= 120 else f"{text[:117]}..."


def bounded_holder_details(holder: dict[str, Any] | None) -> dict[str, Any] | None:
    """Reduce a peer-written holder to a bounded block safe to hand a host.

    ``rendered_holder_field`` bounds what reaches the *message*; this bounds what
    reaches ``details``, which the library copies onto the typed exception for
    hosts to log and index. Without it the two disagree: a refusal whose message
    is one careful line would still carry an arbitrarily large, arbitrarily
    nested document written by another process.

    Only the block's documented keys survive, and only when the peer actually
    recorded them -- a key this process invented would be a claim, not a report.
    Types are preserved rather than rendered: ``pid`` stays an ``int`` because
    hosts branch on it, while strings are truncated and stripped of control
    characters the way the message is. Anything else, including a nested
    structure, is dropped. A holder that could not be read at all stays ``None``
    -- "unknown", which is not the same claim as "nobody".
    """
    if not isinstance(holder, dict):
        return None
    bounded: dict[str, Any] = {}
    for key in ("agent_id", "pid", "hostname", "command", "acquired_at"):
        if key not in holder:
            continue
        value = holder[key]
        if value is None:
            # The peer recorded the field as absent, which is itself a report --
            # ``agent_id`` is documented ``str | null`` for exactly this.
            bounded[key] = None
        elif key == "pid":
            # ``bool`` is a subclass of ``int``, so it has to be excluded
            # explicitly or a peer writing ``pid: true`` would satisfy the type a
            # host branches on.
            if isinstance(value, int) and not isinstance(value, bool):
                bounded[key] = value
        elif isinstance(value, str):
            bounded[key] = rendered_holder_field(holder, key, "")
        # Everything else -- a bool, a number where a string belongs, a nested
        # structure -- is omitted rather than coerced. A host reading this block
        # should never have to guess whether a value came from the peer or from
        # this renderer, and an omitted key says "not reported" honestly.
    return bounded


def holder_process_is_running(holder: dict[str, Any] | None) -> bool | None:
    """Report whether the recorded holder process still exists, if knowable.

    ``None`` means "cannot tell" and is the common answer: a holder on another
    host, a malformed pid, or a platform where this probe is unreliable. Only a
    definite ``False`` -- this host, a well-formed pid, and no such process --
    is worth acting on, because that is the case where the refusal would
    otherwise name a dead driver and tell the caller to wait for it.
    """
    if not isinstance(holder, dict):
        return None
    hostname = holder.get("hostname")
    if not isinstance(hostname, str) or hostname != driver_hostname():
        return None
    pid = holder.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if os.name != "posix" or not hasattr(os, "kill"):
        # Signal 0 does not answer this question portably off POSIX.
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive, owned by another user.
        return True
    except OSError:
        return None
    return True


def driver_busy_error(lock_path: Path, orchestration_id: str) -> OrchestrationControllerError:
    """Translate a contended session lock into the stable driver-busy refusal.

    The holder sidecar is read *here*, after acquisition has already failed, and
    never before. A holder read while this process holds the lock would describe
    this process; a holder read speculatively before attempting acquisition would
    describe a state that the attempt itself may have invalidated. Reading it
    only on the failure path means the block, when present, described a live
    owner at the moment the refusal was composed.

    ``read_lock_holder`` is best-effort and never raises: ``None`` means "could
    not be read", not "no one holds the lock". Both the message and
    ``details.holder`` are built to survive that -- the winner publishes its
    sidecar just after acquiring, so a loser refused inside that narrow window
    legitimately sees nothing and still gets a truthful, actionable refusal.

    The sidecar is a *record*, not proof of life. A holder killed between
    writing it and releasing the lock leaves it behind, and a successor that has
    not yet published its own would otherwise be described by its predecessor's.
    When the recorded process can be shown not to exist, the refusal says the
    record is stale instead of instructing the caller to wait for a driver that
    exited -- advice that never comes true, and that points an operator at a pid
    the OS may since have reassigned to something unrelated.
    """
    holder = read_lock_holder(lock_path)
    reported = holder if isinstance(holder, dict) else {}
    identity = (
        f"agent: {rendered_holder_field(reported, 'agent_id', '<unrecorded agent>')}, "
        f"pid: {rendered_holder_field(reported, 'pid', 'unrecorded')}, "
        f"since: {rendered_holder_field(reported, 'acquired_at', 'unrecorded')}"
    )
    holder_running = holder_process_is_running(holder)
    if holder_running is False:
        message = (
            f"session {orchestration_id} is locked, but the recorded holder is no longer running "
            f"({identity}); the lock is held by another process or the record is stale"
        )
        remediation = (
            "Retry: the recorded holder has exited, so this lock is either held by a driver that "
            "has not published its own record yet or is being released. Do not act on the pid above "
            "-- it may since belong to an unrelated process. If refusals persist, inspect the lock "
            "under runs/orchestrations/<id>/.locks/ before removing anything."
        )
    else:
        message = f"another driver holds session {orchestration_id} ({identity})"
        remediation = (
            "Retry after the holder's call completes, or serialize drivers host-side; "
            "status polling never requires this lock."
        )
    return OrchestrationControllerError(
        "ORCHESTRATION_DRIVER_BUSY",
        message,
        exit_code=EXIT_DRIVER_BUSY,
        recoverable=True,
        remediation=remediation,
        details={
            "holder": bounded_holder_details(holder),
            "holder_running": holder_running,
            "orchestration_id": orchestration_id,
        },
    )


@contextmanager
def driver_session_lock(
    project_root: Path,
    orchestration_id: str,
    *,
    command: str,
    agent_id: Any,
    wait_seconds: float = 0.0,
) -> Iterator[Any]:
    """Hold the session lock for one driver command, refusing a busy session loudly.

    This is the whole of CR-8's behaviour change. The lock itself is unchanged --
    same file, same module, same scope -- but losing the race is now an outcome a
    host can act on instead of a ten-second pause followed by an interleaved
    write. ``wait_seconds`` defaults to 0, a single non-blocking attempt; a host
    that genuinely wants queueing asks for it with ``--driver-wait-seconds``.

    Contention is detected by reading ``contended`` off the exception rather than
    by ``isinstance`` on a subclass. Workspace scripts load siblings by file path,
    so several copies of ``_workspace_locks`` -- and therefore several distinct
    ``LockUnavailableError`` classes -- can coexist in one interpreter, and a
    class check across copies silently never fires. ``getattr(..., False)`` also
    degrades correctly against an older vendored module that predates the flag:
    unknown contention reports as "not contended" and takes the pre-existing
    ``LOCK_UNAVAILABLE`` path, which is the conservative answer.

    A non-contended failure means no backend could be established at all -- the
    filesystem, not a peer, is the problem -- and is re-raised untouched so the
    existing ``LOCK_UNAVAILABLE`` handler keeps reporting it. The two conditions
    stay distinguishable by a host precisely because retrying fixes one and never
    fixes the other.

    Nothing durable is written before the lock is held, so a refusal from here
    leaves the session exactly as it found it: no ``session.json``, no appended
    event, not even a directory. Only the acquisition is inside the ``try``; a
    ``LockUnavailableError`` raised by nested code *within* the guarded body (a
    child run lock, for instance) belongs to that lock and must not be reported
    as this session's driver being busy.
    """
    global _ACTIVE_DRIVER

    lock_path = session_lock_path(project_root, orchestration_id)
    # Built lazily so ``acquired_at`` is stamped when the lock is actually taken.
    # A driver that queued behind a peer for most of its wait would otherwise
    # publish a start time older than its real one by the whole wait, and that
    # field is what an operator -- or a supervisor watching for a stuck holder --
    # thresholds on. Captured once so the block the sidecar carries and the block
    # the audit events carry are the same object.
    published: dict[str, Any] | None = None

    def build_holder() -> dict[str, Any]:
        nonlocal published
        published = driver_identity(command, agent_id)
        return published

    with ExitStack() as stack:
        try:
            handle = stack.enter_context(
                workspace_lock(
                    lock_path,
                    timeout_seconds=max(float(wait_seconds), 0.0),
                    purpose=f"orchestration {orchestration_id}",
                    stale_exclusive_after_seconds=DRIVER_LOCK_STALE_FALLBACK_SECONDS,
                    holder=build_holder,
                )
            )
        except LockUnavailableError as error:
            if not getattr(error, "contended", False):
                raise
            raise driver_busy_error(lock_path, orchestration_id) from error

        previous = _ACTIVE_DRIVER
        _ACTIVE_DRIVER = published if published is not None else driver_identity(command, agent_id)
        stack.callback(_restore_active_driver, previous)
        yield handle


def _restore_active_driver(previous: dict[str, Any] | None) -> None:
    global _ACTIVE_DRIVER
    _ACTIVE_DRIVER = previous


def work_order_path(project_root: Path, orchestration_id: str, action_id: str) -> Path:
    return session_dir(project_root, orchestration_id) / WORK_ORDERS_DIR / f"{action_id}.json"


def work_result_path(project_root: Path, orchestration_id: str, action_id: str) -> Path:
    return session_dir(project_root, orchestration_id) / WORK_RESULTS_DIR / f"{action_id}.json"


def trusted_static_input_path(project_root: Path, orchestration_id: str, action_id: str) -> Path:
    return session_dir(project_root, orchestration_id) / TRUSTED_INPUTS_DIR / f"{action_id}.json"


def scope_integrity_baseline_path(project_root: Path, orchestration_id: str, action_id: str) -> Path:
    return session_dir(project_root, orchestration_id) / TRUSTED_INPUTS_DIR / f"{action_id}-scope-baseline.json"


def control_repair_path(project_root: Path, orchestration_id: str) -> Path:
    return project_root / "runs" / CONTROL_REPAIR_GUARDS_DIR / f"{orchestration_id}.json"


def answers_path(project_root: Path, orchestration_id: str) -> Path:
    return session_dir(project_root, orchestration_id) / ANSWERS_FILENAME


def default_recovery_state() -> dict[str, Any]:
    return {
        "state": RECOVERY_NONE,
        "action_id": None,
        "attempt": None,
        "reason_code": None,
        "recorded_at": None,
    }


def result_digest(document: dict[str, Any]) -> str:
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def contained_path(
    path: Path,
    *,
    containment_root: Path,
    error_code: str,
    label: str,
    missing_ok: bool,
) -> Path | None:
    """Re-anchor a lexical child path beneath a canonical, link-free root."""
    lexical_root = Path(os.path.abspath(containment_root))
    lexical_path = Path(os.path.abspath(path))
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError as exc:
        raise OrchestrationControllerError(error_code, f"{label} escapes the workspace") from exc

    root = lexical_root.resolve()
    anchored = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts[:-1]:
        current /= part
        try:
            ancestor = current.lstat()
        except FileNotFoundError:
            if missing_ok:
                return None
            raise OrchestrationControllerError(error_code, f"{label} is missing: {anchored}") from None
        except OSError as exc:
            raise OrchestrationControllerError(
                error_code,
                f"could not inspect {label} ancestor: {current}",
            ) from exc
        if not stat.S_ISDIR(ancestor.st_mode) or path_is_link_like(current, ancestor):
            raise OrchestrationControllerError(
                error_code,
                f"{label} ancestor is not a real directory: {current}",
            )
    return anchored


def bounded_regular_bytes(
    path: Path,
    *,
    max_bytes: int,
    error_code: str,
    label: str,
    missing_ok: bool = False,
    containment_root: Path | None = None,
) -> bytes | None:
    """Read one bounded, singly linked regular file without following links."""
    if containment_root is not None:
        path = contained_path(
            path,
            containment_root=containment_root,
            error_code=error_code,
            label=label,
            missing_ok=missing_ok,
        )
        if path is None:
            return None
    try:
        before = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise OrchestrationControllerError(error_code, f"{label} is missing: {path}") from None
    except OSError as exc:
        raise OrchestrationControllerError(error_code, f"could not inspect {label}: {path}: {exc}") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or path_is_link_like(path, before)
        or int(getattr(before, "st_nlink", 1) or 1) != 1
    ):
        raise OrchestrationControllerError(error_code, f"{label} is not a singly linked regular file: {path}")
    if before.st_size > max_bytes:
        raise OrchestrationControllerError(error_code, f"{label} exceeds the {max_bytes}-byte limit: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or int(getattr(opened, "st_nlink", 1) or 1) != 1
                or (before.st_dev, before.st_ino, before.st_size)
                != (opened.st_dev, opened.st_ino, opened.st_size)
            ):
                raise OSError(f"{label} changed while it was opened")
            chunks: list[bytes] = []
            observed = 0
            while observed <= max_bytes:
                chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - observed))
                if not chunk:
                    break
                chunks.append(chunk)
                observed += len(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise OrchestrationControllerError(error_code, f"could not safely read {label}: {path}: {exc}") from exc
    if (
        observed > max_bytes
        or observed != before.st_size
        or (opened.st_dev, opened.st_ino, opened.st_size) != (after.st_dev, after.st_ino, after.st_size)
    ):
        raise OrchestrationControllerError(error_code, f"{label} changed while it was read: {path}")
    return b"".join(chunks)


def file_digest(
    path: Path,
    *,
    max_bytes: int = MAX_VERIFICATION_ARTIFACT_BYTES,
    containment_root: Path | None = None,
) -> str | None:
    if containment_root is not None:
        path = contained_path(
            path,
            containment_root=containment_root,
            error_code="ORCHESTRATION_POSTCONDITION_FAILED",
            label="verification artifact",
            missing_ok=True,
        )
        if path is None:
            return None
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OrchestrationControllerError(
            "ORCHESTRATION_POSTCONDITION_FAILED",
            f"could not inspect verification artifact: {path}",
        ) from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or path_is_link_like(path, before)
        or int(getattr(before, "st_nlink", 1) or 1) != 1
        or before.st_size > max_bytes
    ):
        raise OrchestrationControllerError(
            "ORCHESTRATION_POSTCONDITION_FAILED",
            f"verification artifact is unsafe or exceeds the {max_bytes}-byte limit: {path}",
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or int(getattr(opened, "st_nlink", 1) or 1) != 1
                or (before.st_dev, before.st_ino, before.st_size)
                != (opened.st_dev, opened.st_ino, opened.st_size)
            ):
                raise OSError("verification artifact changed while it was opened")
            digest = hashlib.sha256()
            observed = 0
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                observed += len(chunk)
                if observed > max_bytes:
                    raise OSError("verification artifact exceeded its size limit while being read")
                digest.update(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise OrchestrationControllerError(
            "ORCHESTRATION_POSTCONDITION_FAILED",
            f"could not safely hash verification artifact: {path}: {exc}",
        ) from exc
    if (
        observed != before.st_size
        or (opened.st_dev, opened.st_ino, opened.st_size) != (after.st_dev, after.st_ino, after.st_size)
    ):
        raise OrchestrationControllerError(
            "ORCHESTRATION_POSTCONDITION_FAILED",
            f"verification artifact changed while it was hashed: {path}",
        )
    return f"sha256:{digest.hexdigest()}"


def trusted_static_input_error(message: str, *, details: dict[str, Any] | None = None) -> OrchestrationControllerError:
    return OrchestrationControllerError(
        "ORCHESTRATION_TRUSTED_INPUT_UNSAFE",
        message,
        recoverable=True,
        remediation=(
            "Replace links or special files with bounded regular files/directories under the trusted workspace "
            "inputs, then retry. Keep generated research output under its documented writable paths."
        ),
        details=details,
    )


def path_is_link_like(path: Path, metadata: os.stat_result) -> bool:
    if stat.S_ISLNK(metadata.st_mode):
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is None:
        return False
    try:
        return bool(is_junction())
    except OSError:
        return True


def portable_mode(metadata: os.stat_result) -> int:
    """Return only portable rwx permission bits for semantic comparisons."""
    return stat.S_IMODE(metadata.st_mode) & 0o777


def require_real_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise trusted_static_input_error(f"cannot inspect trusted input ancestor {label}: {exc}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or path_is_link_like(path, metadata):
        raise trusted_static_input_error(f"trusted input ancestor {label} is not a real directory")


def validate_trusted_static_ancestors(project_root: Path, path: Path, label: str) -> None:
    try:
        relative = path.relative_to(project_root)
    except ValueError as exc:  # pragma: no cover - paths are assembled internally
        raise trusted_static_input_error(f"trusted input {label} escapes the workspace") from exc
    require_real_directory(project_root, ".")
    current = project_root
    for part in relative.parts[:-1]:
        current /= part
        require_real_directory(current, current.relative_to(project_root).as_posix())


def validate_trusted_static_carveouts(project_root: Path) -> None:
    """Reject unsafe entries in writable control-tree carveouts without fingerprinting their contents."""
    inspected = 0

    def visit(path: Path, label: str) -> None:
        nonlocal inspected
        inspected += 1
        if inspected > MAX_TRUSTED_STATIC_INPUT_ENTRIES:
            raise trusted_static_input_error(
                f"writable trusted-input carveouts exceed {MAX_TRUSTED_STATIC_INPUT_ENTRIES} entries"
            )
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise trusted_static_input_error(f"cannot inspect writable trusted-input carveout {label}: {exc}") from exc
        if path_is_link_like(path, metadata):
            raise trusted_static_input_error(f"writable trusted-input carveout {label} is a symbolic link or junction")
        if stat.S_ISDIR(metadata.st_mode):
            try:
                children = sorted(path.iterdir(), key=lambda child: child.name)
            except OSError as exc:
                raise trusted_static_input_error(
                    f"cannot enumerate writable trusted-input carveout {label}: {exc}"
                ) from exc
            for child in children:
                visit(child, f"{label}/{child.name}")
            return
        if not stat.S_ISREG(metadata.st_mode):
            raise trusted_static_input_error(f"writable trusted-input carveout {label} contains a special file")
        if metadata.st_nlink > 1:
            raise trusted_static_input_error(f"writable trusted-input carveout {label} is multiply linked")

    for relative in TRUSTED_STATIC_EXCLUDED_SUBTREES:
        path = project_root.joinpath(*PurePosixPath(relative).parts)
        validate_trusted_static_ancestors(project_root, path, relative)
        visit(path, relative)


def trusted_static_input_fingerprint(project_root: Path) -> dict[str, Any]:
    """Capture a bounded, deterministic semantic fingerprint of trusted static workspace inputs."""
    project_root = project_root.resolve()
    validate_trusted_static_carveouts(project_root)
    entries: list[dict[str, Any]] = []
    total_bytes = 0

    def visit(path: Path, relative: PurePosixPath) -> None:
        nonlocal total_bytes
        label = relative.as_posix()
        if label in TRUSTED_STATIC_EXCLUDED_SUBTREES:
            return
        if len(entries) >= MAX_TRUSTED_STATIC_INPUT_ENTRIES:
            raise trusted_static_input_error(
                f"trusted static inputs exceed {MAX_TRUSTED_STATIC_INPUT_ENTRIES} entries"
            )
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            entries.append({"path": label, "kind": "missing", "mode": 0, "size": 0, "sha256": None})
            return
        except OSError as exc:
            raise trusted_static_input_error(f"cannot inspect trusted static input {label}: {exc}") from exc
        if path_is_link_like(path, metadata):
            raise trusted_static_input_error(f"trusted static input {label} is a symbolic link or junction")
        mode = portable_mode(metadata)
        if stat.S_ISDIR(metadata.st_mode):
            entries.append({"path": label, "kind": "directory", "mode": mode, "size": 0, "sha256": None})
            try:
                children = sorted(path.iterdir(), key=lambda child: child.name)
            except OSError as exc:
                raise trusted_static_input_error(f"cannot enumerate trusted static input {label}: {exc}") from exc
            for child in children:
                visit(child, relative / child.name)
            return
        if not stat.S_ISREG(metadata.st_mode):
            raise trusted_static_input_error(f"trusted static input {label} is not a regular file or directory")
        if metadata.st_nlink > 1:
            raise trusted_static_input_error(f"trusted static input {label} is multiply linked")
        declared_size = int(metadata.st_size)
        if declared_size > MAX_TRUSTED_STATIC_INPUT_BYTES - total_bytes:
            raise trusted_static_input_error(
                f"trusted static inputs exceed the {MAX_TRUSTED_STATIC_INPUT_BYTES}-byte limit"
            )
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            try:
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink > 1
                    or (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino)
                ):
                    raise trusted_static_input_error(f"trusted static input {label} changed while it was opened")
                digest = hashlib.sha256()
                observed_size = 0
                while True:
                    chunk = os.read(descriptor, 64 * 1024)
                    if not chunk:
                        break
                    observed_size += len(chunk)
                    if observed_size > declared_size:
                        raise trusted_static_input_error(f"trusted static input {label} changed while it was read")
                    digest.update(chunk)
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
        except OrchestrationControllerError:
            raise
        except OSError as exc:
            raise trusted_static_input_error(f"cannot read trusted static input {label}: {exc}") from exc
        if (
            observed_size != declared_size
            or (opened.st_dev, opened.st_ino, opened.st_size) != (after.st_dev, after.st_ino, after.st_size)
            or portable_mode(after) != mode
        ):
            raise trusted_static_input_error(f"trusted static input {label} changed while it was inspected")
        total_bytes += observed_size
        entries.append(
            {
                "path": label,
                "kind": "file",
                "mode": mode,
                "size": observed_size,
                "sha256": f"sha256:{digest.hexdigest()}",
            }
        )

    roots = (*TRUSTED_STATIC_FILE_PATHS, *TRUSTED_STATIC_TREE_PATHS)
    for relative in roots:
        path = project_root.joinpath(*PurePosixPath(relative).parts)
        validate_trusted_static_ancestors(project_root, path, relative)
        visit(path, PurePosixPath(relative))
    entries.sort(key=lambda item: item["path"])
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_TRUSTED_STATIC_FINGERPRINT_BYTES:
        raise trusted_static_input_error(
            f"trusted static fingerprint exceeds the {MAX_TRUSTED_STATIC_FINGERPRINT_BYTES}-byte limit"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "algorithm": "sha256",
        "fingerprint": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
        "entry_count": len(entries),
        "total_bytes": total_bytes,
        "entries": entries,
    }


def valid_trusted_static_input_fingerprint(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "algorithm",
        "fingerprint",
        "entry_count",
        "total_bytes",
        "entries",
    }:
        return False
    entries = value.get("entries")
    entry_count = value.get("entry_count")
    total_bytes = value.get("total_bytes")
    if (
        not isinstance(entries, list)
        or not isinstance(entry_count, int)
        or isinstance(entry_count, bool)
        or entry_count != len(entries)
        or entry_count < 0
        or entry_count > MAX_TRUSTED_STATIC_INPUT_ENTRIES
        or not isinstance(total_bytes, int)
        or isinstance(total_bytes, bool)
        or total_bytes < 0
        or total_bytes > MAX_TRUSTED_STATIC_INPUT_BYTES
    ):
        return False
    seen_paths: set[str] = set()
    observed_bytes = 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "kind", "mode", "size", "sha256"}:
            return False
        path_value = entry.get("path")
        if (
            not isinstance(path_value, str)
            or not path_value
            or "\x00" in path_value
            or len(path_value) > MAX_TRUSTED_STATIC_PATH_LENGTH
        ):
            return False
        relative = PurePosixPath(path_value)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "\\" in path_value
            or relative.as_posix() != path_value
            or path_value in seen_paths
        ):
            return False
        allowed = path_value in TRUSTED_STATIC_FILE_PATHS or relative.parts[:1] in {
            (root,) for root in TRUSTED_STATIC_TREE_PATHS
        }
        if not allowed:
            return False
        seen_paths.add(path_value)
        kind = entry.get("kind")
        mode = entry.get("mode")
        size = entry.get("size")
        digest = entry.get("sha256")
        if kind not in {"missing", "directory", "file"}:
            return False
        if not isinstance(mode, int) or isinstance(mode, bool) or mode < 0 or mode > 0o777:
            return False
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return False
        if kind == "file":
            if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
                return False
            observed_bytes += size
        elif size != 0 or digest is not None:
            return False
        if kind == "missing" and path_value not in {*TRUSTED_STATIC_FILE_PATHS, *TRUSTED_STATIC_TREE_PATHS}:
            return False
    if [entry["path"] for entry in entries] != sorted(seen_paths) or observed_bytes != total_bytes:
        return False
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_TRUSTED_STATIC_FINGERPRINT_BYTES:
        return False
    expected_digest = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    return (
        value.get("schema_version") == SCHEMA_VERSION
        and value.get("algorithm") == "sha256"
        and value.get("fingerprint") == expected_digest
    )


def valid_pending_trusted_static_inputs(value: Any) -> bool:
    if value is None:
        return True
    return (
        isinstance(value, dict)
        and set(value) == {"action_id", "fingerprint", "entry_count", "total_bytes"}
        and isinstance(value.get("action_id"), str)
        and bool(value["action_id"])
        and isinstance(value.get("fingerprint"), str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", value["fingerprint"]) is not None
        and isinstance(value.get("entry_count"), int)
        and not isinstance(value.get("entry_count"), bool)
        and 0 <= value["entry_count"] <= MAX_TRUSTED_STATIC_INPUT_ENTRIES
        and isinstance(value.get("total_bytes"), int)
        and not isinstance(value.get("total_bytes"), bool)
        and 0 <= value["total_bytes"] <= MAX_TRUSTED_STATIC_INPUT_BYTES
    )


def trusted_static_input_differences(expected: dict[str, Any], current: dict[str, Any]) -> list[str]:
    before = {entry["path"]: entry for entry in expected.get("entries", []) if isinstance(entry, dict)}
    after = {entry["path"]: entry for entry in current.get("entries", []) if isinstance(entry, dict)}
    differences: list[str] = []
    for path in sorted(set(before) | set(after)):
        old = before.get(path)
        new = after.get(path)
        if old == new:
            continue
        if old is None:
            reason = "created"
        elif new is None:
            reason = "removed"
        else:
            changed: list[str] = []
            if old.get("kind") != new.get("kind"):
                changed.append(f"kind {old.get('kind')}->{new.get('kind')}")
            if old.get("mode") != new.get("mode"):
                changed.append(f"mode {old.get('mode'):03o}->{new.get('mode'):03o}")
            if old.get("size") != new.get("size") or old.get("sha256") != new.get("sha256"):
                changed.append("content")
            reason = ", ".join(changed) or "semantic state"
        differences.append(f"{path} [{reason}]")
    return differences


def verify_pending_trusted_static_inputs(
    project_root: Path,
    session: dict[str, Any],
    work_order: dict[str, Any],
    *,
    allow_legacy_unbound: bool = False,
) -> None:
    """Fail closed on static-input drift and explicitly migrate legacy pending actions."""
    if session.get("pending_action_id") != work_order.get("action_id"):
        return
    if "pending_trusted_static_inputs" not in session:
        # Version 0.2.0 sessions predate controller-owned static fingerprints.
        if allow_legacy_unbound:
            return
        raise OrchestrationControllerError(
            "ORCHESTRATION_LEGACY_ACTION_UNBOUND",
            "legacy pending action has not yet been bound to the current trusted static inputs",
            recoverable=True,
            remediation=(
                "Replay the pending action with evidence-wiki orchestrate next --resume, or use managed "
                "evidence-wiki orchestrate resume, before submitting a result. The replay binds a controller-owned "
                "fingerprint before any worker is launched."
            ),
            details={"action_id": work_order.get("action_id")},
        )
    retained = session.get("pending_trusted_static_inputs")
    if not valid_pending_trusted_static_inputs(retained) or retained is None:
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "new pending action is missing its trusted static-input fingerprint",
            recoverable=False,
        )
    if retained.get("action_id") != work_order.get("action_id"):
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "trusted static-input fingerprint does not belong to the pending action",
            recoverable=False,
        )
    snapshot_path = trusted_static_input_path(
        project_root,
        session["orchestration_id"],
        work_order["action_id"],
    )
    expected = load_json_object(
        snapshot_path,
        error_code="ORCHESTRATION_STATE_INVALID",
        label="trusted static-input fingerprint",
    )
    if not valid_trusted_static_input_fingerprint(expected):
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "persisted trusted static-input fingerprint is invalid",
            recoverable=False,
        )
    if (
        retained.get("fingerprint") != expected.get("fingerprint")
        or retained.get("entry_count") != expected.get("entry_count")
        or retained.get("total_bytes") != expected.get("total_bytes")
    ):
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "parent session does not match its trusted static-input fingerprint",
            recoverable=False,
        )
    current = trusted_static_input_fingerprint(project_root)
    if expected.get("fingerprint") == current.get("fingerprint"):
        return
    differences = trusted_static_input_differences(expected, current)
    shown = differences[:MAX_TRUSTED_STATIC_INPUT_DIFFERENCES]
    omitted = max(0, len(differences) - len(shown))
    raise OrchestrationControllerError(
        "ORCHESTRATION_TRUSTED_INPUT_CHANGED",
        "trusted static workspace inputs changed after the action was issued",
        recoverable=True,
        remediation=(
            "Restore the issued static inputs and retry the same pending action. If the change was intentional, "
            "start a new orchestration session from the updated workspace instead of editing parent state."
        ),
        details={
            "action_id": work_order.get("action_id"),
            "expected_fingerprint": expected.get("fingerprint"),
            "current_fingerprint": current.get("fingerprint"),
            "changed_paths": shown,
            "omitted_changed_path_count": omitted,
        },
    )


def relative_workspace_path(project_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError as exc:
        raise OrchestrationControllerError(
            "ARTIFACT_PATH_INVALID",
            f"path escapes the workspace: {path}",
        ) from exc


def write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8", newline="\n")
        temporary.replace(path)
    except OSError as exc:
        raise OrchestrationControllerError(
            "ORCHESTRATION_WRITE_FAILED",
            f"could not persist {path}: {exc}",
            recoverable=True,
            remediation="Restore workspace write access or free space, then retry the idempotent command.",
        ) from exc


def load_json_object(
    path: Path,
    *,
    error_code: str,
    label: str,
    max_bytes: int = MAX_JSON_DOCUMENT_BYTES,
    containment_root: Path | None = None,
) -> dict[str, Any]:
    try:
        content = bounded_regular_bytes(
            path,
            max_bytes=max_bytes,
            error_code=error_code,
            label=label,
            containment_root=containment_root,
        )
        if content is None:  # pragma: no cover - missing_ok is false
            raise OrchestrationControllerError(error_code, f"{label} is missing: {path}")
        document = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OrchestrationControllerError(error_code, f"invalid JSON in {label}: {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise OrchestrationControllerError(error_code, f"{label} must contain a JSON object: {path}")
    return document


def enforce_control_repair_gate(project_root: Path, orchestration_id: str) -> None:
    """Prevent protocol replay/submission while the host repair marker is required."""
    path = control_repair_path(project_root, orchestration_id)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            f"could not inspect control-repair marker: {exc}",
            recoverable=False,
        ) from exc
    if not stat.S_ISREG(metadata.st_mode) or path_is_link_like(path, metadata) or metadata.st_nlink > 1:
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "control-repair marker is not a singly linked regular file",
            recoverable=False,
        )
    marker = load_json_object(
        path,
        error_code="ORCHESTRATION_STATE_INVALID",
        label="control-repair marker",
        max_bytes=MAX_RESULT_BYTES,
        containment_root=project_root,
    )
    required_keys = {
        "schema_version",
        "artifact_type",
        "orchestration_id",
        "status",
        "reason_code",
        "detected_at",
        "acknowledged_at",
        "attempt_ids",
        "expected_control_fingerprint",
    }
    attempt_ids = marker.get("attempt_ids")
    if (
        set(marker) != required_keys
        or marker.get("schema_version") != SCHEMA_VERSION
        or marker.get("artifact_type") != "orchestration_control_repair"
        or marker.get("orchestration_id") != orchestration_id
        or marker.get("status") not in {"required", "acknowledged"}
        or marker.get("reason_code") != "CONTROL_ARTIFACT_TAMPERED"
        or not isinstance(marker.get("detected_at"), str)
        or len(marker["detected_at"]) > 64
        or not isinstance(attempt_ids, list)
        or not attempt_ids
        or len(attempt_ids) > 64
        or len(attempt_ids) != len(set(attempt_ids))
        or any(not isinstance(value, str) or SAFE_ID_RE.fullmatch(value) is None for value in attempt_ids)
        or not isinstance(marker.get("expected_control_fingerprint"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", marker["expected_control_fingerprint"]) is None
        or (
            marker["status"] == "required"
            and marker.get("acknowledged_at") is not None
        )
        or (
            marker["status"] == "acknowledged"
            and (
                not isinstance(marker.get("acknowledged_at"), str)
                or not marker["acknowledged_at"]
                or len(marker["acknowledged_at"]) > 64
            )
        )
    ):
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "control-repair marker is invalid",
            recoverable=False,
        )
    if marker["status"] == "required":
        raise OrchestrationControllerError(
            "ORCHESTRATION_CONTROL_REPAIR_REQUIRED",
            "managed control drift must be repaired and acknowledged before replay or submission",
            recoverable=True,
            remediation=(
                "Inspect the retained attempt and quarantine, restore the issued state, then run managed "
                "orchestrate resume with --acknowledge-control-repair."
            ),
            details={"attempt_ids": attempt_ids},
        )


def valid_pending_submission(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict) or set(value) != {
        "action_id",
        "accepted_at",
        "result",
        "result_digest",
        "next_phase",
        "completion_reason",
    }:
        return False
    result = value.get("result")
    return (
        isinstance(value.get("action_id"), str)
        and bool(value["action_id"])
        and isinstance(value.get("accepted_at"), str)
        and bool(value["accepted_at"])
        and isinstance(result, dict)
        and valid_stored_result_shape(result, value["action_id"])
        and isinstance(value.get("result_digest"), str)
        and value["result_digest"] == result_digest(result)
        and (value.get("next_phase") is None or value["next_phase"] in PHASES)
        and (value.get("completion_reason") is None or isinstance(value["completion_reason"], str))
    )


def valid_recovery_state(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict) or set(value) != {
        "state",
        "action_id",
        "attempt",
        "reason_code",
        "recorded_at",
    }:
        return False
    return (
        value.get("state") in RECOVERY_STATES
        and (value.get("action_id") is None or isinstance(value["action_id"], str))
        and (
            value.get("attempt") is None
            or (isinstance(value["attempt"], int) and not isinstance(value["attempt"], bool) and value["attempt"] > 0)
        )
        and (value.get("reason_code") is None or isinstance(value["reason_code"], str))
        and (value.get("recorded_at") is None or isinstance(value["recorded_at"], str))
    )


def valid_session_acquisition(document: dict[str, Any]) -> bool:
    """Validate the optional frozen acquisition posture on a session document.

    Every field is optional so pre-delegation sessions still load, but a session that
    declares delegation must name its acquirer: routing and work-order addressing both
    read it, and a delegated session without one would fail later and less legibly.
    """
    mode = document.get("acquisition_mode")
    if mode is not None and mode not in ACQUISITION_MODES:
        return False
    acquirer = document.get("acquirer_agent_id")
    if acquirer is not None and not valid_agent_id(acquirer):
        return False
    if mode == ACQUISITION_MODE_DELEGATED and acquirer is None:
        return False
    attempts = document.get("max_attempts_per_request")
    if attempts is not None and (
        isinstance(attempts, bool)
        or not isinstance(attempts, int)
        or attempts < 1
        or attempts > MAX_MAX_ATTEMPTS_PER_REQUEST
    ):
        return False
    return True


def load_session(project_root: Path, orchestration_id: str) -> dict[str, Any]:
    path = session_path(project_root, orchestration_id)
    if not path.is_file():
        raise OrchestrationControllerError(
            "ORCHESTRATION_UNKNOWN",
            f"unknown orchestration id: {orchestration_id}",
            details={"orchestration_id": orchestration_id},
        )
    document = load_json_object(path, error_code="ORCHESTRATION_STATE_INVALID", label="orchestration session")
    if (
        document.get("schema_version") != SCHEMA_VERSION
        or document.get("artifact_type") != SESSION_ARTIFACT_TYPE
        or document.get("orchestration_id") != orchestration_id
        or document.get("status") not in {ACTIVE_STATUS, PAUSED_STATUS, *TERMINAL_STATUSES}
        or document.get("phase") not in PHASES
        or not isinstance(document.get("child_run_ids"), list)
        or not isinstance(document.get("limits"), dict)
        or not valid_pending_submission(document.get("pending_submission"))
        or not valid_recovery_state(document.get("recovery"))
        or (
            "pending_trusted_static_inputs" in document
            and not valid_pending_trusted_static_inputs(document.get("pending_trusted_static_inputs"))
        )
        or not valid_session_acquisition(document)
    ):
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            f"invalid orchestration session shape: {relative_workspace_path(project_root, path)}",
            recoverable=False,
        )
    return document


def compact_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def append_event(project_root: Path, orchestration_id: str, event: dict[str, Any]) -> None:
    path = events_path(project_root, orchestration_id)
    existing: list[dict[str, Any]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise OrchestrationControllerError(
                    "ORCHESTRATION_EVENTS_INVALID",
                    f"invalid retained orchestration event JSON: {exc}",
                    recoverable=False,
                ) from exc
            if not isinstance(item, dict):
                raise OrchestrationControllerError(
                    "ORCHESTRATION_EVENTS_INVALID",
                    "retained orchestration event is not a JSON object",
                    recoverable=False,
                )
            existing.append(item)
    path.parent.mkdir(parents=True, exist_ok=True)
    event["event_id"] = f"evt-{len(existing) + 1:04d}"
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text("".join(compact_json(item) + "\n" for item in [*existing, event]), encoding="utf-8", newline="\n")
    temporary.replace(path)


def record_event(
    project_root: Path,
    session: dict[str, Any],
    event_type: str,
    message: str,
    *,
    action_id: str | None = None,
    data: dict[str, Any] | None = None,
    event_key: str | None = None,
) -> None:
    event = {
        "schema_version": SCHEMA_VERSION,
        "orchestration_id": session["orchestration_id"],
        "occurred_at": timestamp_utc(),
        "agent_id": session["agent_id"],
        "event_type": event_type,
        "action_id": action_id,
        "phase": session["phase"],
        "message": message,
        "data": data or {},
    }
    if event_key is not None:
        event["event_key"] = event_key
    append_event(
        project_root,
        session["orchestration_id"],
        event,
    )


def event_key_exists(
    project_root: Path,
    orchestration_id: str,
    event_key: str,
    *,
    event_type: str,
    action_id: str | None,
) -> bool:
    path = events_path(project_root, orchestration_id)
    if not path.is_file():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise OrchestrationControllerError(
            "ORCHESTRATION_EVENTS_INVALID",
            f"could not read retained orchestration events: {exc}",
            recoverable=False,
        ) from exc
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OrchestrationControllerError(
                "ORCHESTRATION_EVENTS_INVALID",
                f"invalid retained orchestration event JSON: {exc}",
                recoverable=False,
            ) from exc
        if isinstance(event, dict):
            if event.get("event_key") == event_key:
                return True
            if event.get("event_type") == event_type and event.get("action_id") == action_id:
                return True
    return False


def record_event_once(
    project_root: Path,
    session: dict[str, Any],
    event_type: str,
    message: str,
    *,
    action_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    event_key = f"{event_type}:{action_id or 'session'}"
    if event_key_exists(
        project_root,
        session["orchestration_id"],
        event_key,
        event_type=event_type,
        action_id=action_id,
    ):
        return
    record_event(
        project_root,
        session,
        event_type,
        message,
        action_id=action_id,
        data=data,
        event_key=event_key,
    )


def load_config(project_root: Path) -> dict[str, Any]:
    path = project_root / "research.yml"
    if not path.is_file():
        raise OrchestrationControllerError("CONFIG_MISSING", f"Missing research.yml: {path}")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise OrchestrationControllerError("CONFIG_INVALID", f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise OrchestrationControllerError("CONFIG_INVALID", f"research.yml must contain a mapping: {path}")
    return document


def safe_registered_ids(phase: str) -> tuple[str, ...]:
    """Return the ids installed registrations supply, ``()`` if enumeration fails.

    This policy is recomputed on every replay, so an environment that cannot be
    enumerated must degrade to "supplies nothing" rather than crash the controller and
    block every session. The fallback only narrows the accepted set, so it can refuse a
    registered id but never admit one.
    """
    try:
        return registered_ids(phase)
    except Exception:  # noqa: BLE001 - a broken environment must not break the controller
        return ()


def provider_policy(config: dict[str, Any]) -> dict[str, Any]:
    integrations = config.get("integrations") if isinstance(config.get("integrations"), dict) else {}
    policy: dict[str, Any] = {}
    for phase in ("discovery", "acquisition"):
        block = integrations.get(phase) if isinstance(integrations.get(phase), dict) else {}
        try:
            validated = validate_provider_ids(
                block.get("providers"),
                phase=phase,
                registered=safe_registered_ids(phase),
            )
        except ProviderNotRegisteredError as exc:
            # The code stays CONFIG_INVALID because an unresolvable id is indistinguishable
            # from a typo -- no registration exists either way -- and hosts have parsed that
            # code for this input since before registration existed. The distinction is
            # carried in the remediation and details instead, which cost a host nothing and
            # tell an operator the one thing the generic sentence cannot: that installing a
            # distribution is a fix. Being loud about authorized-but-not-installed is smoke's
            # job (it refuses the workspace) and doctor's (it names the distribution).
            #
            # Recomputing this policy is also how a mid-session uninstall is caught:
            # verify_provider_policy_unchanged calls back into here, so a distribution that
            # vanishes under a running session refuses on the next call instead of being
            # reported as an allow-list the operator narrowed and must restore.
            raise OrchestrationControllerError(
                "CONFIG_INVALID",
                f"research.yml integrations.{phase}.providers {exc}",
                remediation=(
                    "Correct the provider id, or install a distribution registering it under the "
                    "evidence_wiki.acquisition_providers or evidence_wiki.discovery_providers "
                    "entry-point group, or remove the id from research.yml."
                ),
                details={"phase": phase, "unresolved_provider_ids": list(exc.provider_ids)},
            ) from exc
        except ProviderListError as exc:
            raise OrchestrationControllerError(
                "CONFIG_INVALID",
                f"research.yml integrations.{phase}.providers {exc}",
            ) from exc
        providers = sorted(validated.providers)
        policy[phase] = {
            # Strategy aliases remain readable for one compatibility release,
            # but only concrete providers grant permission to contact a
            # transport.  An alias-only list therefore has no effective route.
            "enabled": block.get("enabled") is True and bool(providers),
            "providers": providers,
        }
    return policy


def acquisition_policy(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve the declared acquisition mode, refusing a contradictory posture.

    Delegation says an external acquirer fulfils source requests; enabled workspace
    acquisition providers say this workspace fetches them itself. A workspace declaring
    both has not stated who acquires, and guessing either way is silent: preferring
    providers would never address the acquirer, preferring delegation would leave
    authorized providers unused. Both are refused here, before durable state exists.
    """
    try:
        declared = orchestration_config(config)
    except OrchestrationConfigError as exc:
        raise OrchestrationControllerError(
            exc.error_code,
            exc.message,
            recoverable=False,
            remediation=exc.remediation,
        ) from exc
    if declared["acquisition_mode"] == ACQUISITION_MODE_DELEGATED:
        policy = provider_policy(config)
        if policy["acquisition"]["enabled"]:
            raise OrchestrationControllerError(
                "CONFIG_INVALID",
                (
                    "research.yml declares orchestration.acquisition: delegated while "
                    "integrations.acquisition is enabled; exactly one of them acquires evidence"
                ),
                remediation=(
                    "Disable integrations.acquisition.providers, or remove "
                    "orchestration.acquisition: delegated."
                ),
                details={"acquisition_providers": policy["acquisition"]["providers"]},
            )
    return declared


def session_acquisition_policy(session: dict[str, Any]) -> dict[str, Any]:
    """Read one session's frozen acquisition posture.

    Sessions created before delegation existed carry none of these fields; they ran under
    the only mode there was, so a missing mode reads as ``providers`` rather than as a
    malformed session.
    """
    mode = session.get("acquisition_mode")
    return {
        "acquisition_mode": mode if mode in ACQUISITION_MODES else ACQUISITION_MODE_PROVIDERS,
        "acquirer_agent_id": session.get("acquirer_agent_id"),
        "max_attempts_per_request": (
            session.get("max_attempts_per_request")
            if isinstance(session.get("max_attempts_per_request"), int)
            and not isinstance(session.get("max_attempts_per_request"), bool)
            else DEFAULT_MAX_ATTEMPTS_PER_REQUEST
        ),
    }


def verify_runtime_guards(
    project_root: Path,
    session: dict[str, Any],
    work_order: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-check mutable safety and authorization state before replay/submit."""
    # The workspace status and configuration readers load code from the
    # workspace.  Detect drift in those trusted inputs before importing or
    # executing any of them.
    if work_order is not None:
        verify_pending_trusted_static_inputs(
            project_root,
            session,
            work_order,
        )
    status = fresh_workspace_status(project_root)
    health = status.get("workspace_health") if isinstance(status.get("workspace_health"), dict) else {}
    readiness = status.get("readiness") if isinstance(status.get("readiness"), dict) else {}
    if not health.get("materially_valid", False) or readiness.get("verdict") == "attention_required":
        # Which code this is depends on whether a work order exists, because that is what
        # decides whether anything *changed*. With one, the baseline moved after the order
        # was issued and replaying cannot succeed — a *_CHANGED condition beside
        # ORCHESTRATION_DELEGATION_CHANGED / _PROVIDER_POLICY_CHANGED. Without one (this
        # function is reachable as a direct guard check, `work_order` defaulting to None),
        # nothing moved: the workspace is simply unsafe to act in now, which is the
        # repair-and-retry condition the other seven WORKSPACE_UNSAFE sites report.
        #
        # Emitting the *_CHANGED code for both would name a change that did not happen and
        # tell the operator to abandon a session they do not have.
        details = {
            "workspace_health": health,
            "readiness_verdict": readiness.get("verdict"),
            "readiness_reasons": readiness.get("reasons", []),
        }
        if work_order is not None:
            raise OrchestrationControllerError(
                "ORCHESTRATION_WORKSPACE_HEALTH_CHANGED",
                "workspace health or HIGH validation findings changed after the work order was issued",
                recoverable=False,
                remediation=(
                    "Preserve the session for audit and start a fresh orchestration once the reported "
                    "workspace findings are repaired; replaying this action cannot succeed."
                ),
                details=details,
            )
        raise OrchestrationControllerError(
            "ORCHESTRATION_WORKSPACE_UNSAFE",
            "workspace health or HIGH validation findings make this workspace unsafe to act in",
            remediation=(
                "Resolve the reported health or validation findings, then run this check again."
            ),
            details=details,
        )
    verify_provider_policy_unchanged(project_root, session, work_order)
    verify_delegation_unchanged(project_root, session)
    return status


def verify_provider_policy_unchanged(
    project_root: Path,
    session: dict[str, Any],
    work_order: dict[str, Any] | None = None,
) -> None:
    """Safely compare YAML provider authorization without executing workspace code."""
    current = provider_policy(load_config(project_root))
    expected = work_order.get("provider_policy") if isinstance(work_order, dict) else session.get("provider_policy")
    expected = expected if isinstance(expected, dict) else {}
    removed: dict[str, list[str]] = {}
    for phase in ("discovery", "acquisition"):
        expected_phase = expected.get(phase) if isinstance(expected.get(phase), dict) else {}
        current_phase = current.get(phase) if isinstance(current.get(phase), dict) else {}
        expected_providers = {
            value for value in expected_phase.get("providers", []) if isinstance(value, str) and value
        }
        current_providers = {
            value for value in current_phase.get("providers", []) if isinstance(value, str) and value
        }
        missing = sorted(expected_providers - current_providers)
        if expected_phase.get("enabled") is True and current_phase.get("enabled") is not True:
            missing = sorted(expected_providers or {"<phase-disabled>"})
        if missing:
            removed[phase] = missing
    if removed:
        raise OrchestrationControllerError(
            "ORCHESTRATION_PROVIDER_POLICY_CHANGED",
            "provider authorization was narrowed after the work order was issued",
            recoverable=False,
            remediation=(
                "Restore the research.yml provider authorization this session started under, or preserve the session for audit and start a new orchestration."
            ),
            details={"removed_providers": removed, "current_provider_policy": current},
        )


def verify_delegation_unchanged(project_root: Path, session: dict[str, Any]) -> None:
    """Refuse a session whose declared acquirer changed after it started.

    A pending action is already protected: ``research.yml`` is a trusted static input, so
    editing it under one is ``ORCHESTRATION_TRUSTED_INPUT_CHANGED``. This guard covers the
    planning gaps between actions, where the file is not pinned. Who acquires evidence
    decides which work orders a session may issue and who may execute them, so a session
    that started under one answer must not silently continue under another.
    """
    expected = session_acquisition_policy(session)
    current = acquisition_policy(load_config(project_root))
    changed = {
        field: {"expected": expected[field], "current": current[field]}
        for field in ("acquisition_mode", "acquirer_agent_id")
        if expected[field] != current[field]
    }
    if changed:
        raise OrchestrationControllerError(
            "ORCHESTRATION_DELEGATION_CHANGED",
            "the declared acquisition mode or acquirer changed after the session started",
            recoverable=False,
            remediation=(
                "Restore the research.yml orchestration: section this session started under, "
                "or start a new session under the new declaration."
            ),
            details={"changed": changed},
        )


def bind_legacy_pending_trusted_inputs(
    project_root: Path,
    session: dict[str, Any],
    work_order: dict[str, Any],
) -> None:
    """Bind one pre-0.2.1 pending action before workspace code is executed."""
    if "pending_trusted_static_inputs" in session:
        return
    action_id = require_safe_id(work_order.get("action_id"), "action_id")
    if session.get("pending_action_id") != action_id:
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "legacy trusted-input binding does not match the pending action",
            recoverable=False,
        )
    fingerprint_path = trusted_static_input_path(project_root, session["orchestration_id"], action_id)
    if fingerprint_path.exists():
        fingerprint = load_json_object(
            fingerprint_path,
            error_code="ORCHESTRATION_STATE_INVALID",
            label="legacy trusted static-input fingerprint",
        )
        if not valid_trusted_static_input_fingerprint(fingerprint):
            raise OrchestrationControllerError(
                "ORCHESTRATION_STATE_INVALID",
                "legacy trusted static-input fingerprint is invalid",
                recoverable=False,
            )
        current = trusted_static_input_fingerprint(project_root)
        if fingerprint.get("fingerprint") != current.get("fingerprint"):
            differences = trusted_static_input_differences(fingerprint, current)
            shown = differences[:MAX_TRUSTED_STATIC_INPUT_DIFFERENCES]
            raise OrchestrationControllerError(
                "ORCHESTRATION_TRUSTED_INPUT_CHANGED",
                "trusted static workspace inputs changed while legacy binding was being finalized",
                recoverable=True,
                remediation=(
                    "Restore the static inputs recorded by the retained fingerprint, then replay the same action."
                ),
                details={
                    "action_id": action_id,
                    "expected_fingerprint": fingerprint.get("fingerprint"),
                    "current_fingerprint": current.get("fingerprint"),
                    "changed_paths": shown,
                    "omitted_changed_path_count": max(0, len(differences) - len(shown)),
                },
            )
    else:
        fingerprint = trusted_static_input_fingerprint(project_root)
        write_json_atomic(fingerprint_path, fingerprint)
    session["pending_trusted_static_inputs"] = {
        "action_id": action_id,
        "fingerprint": fingerprint["fingerprint"],
        "entry_count": fingerprint["entry_count"],
        "total_bytes": fingerprint["total_bytes"],
    }
    session["updated_at"] = timestamp_utc()
    write_json_atomic(session_path(project_root, session["orchestration_id"]), session)
    record_event_once(
        project_root,
        session,
        "trusted_inputs_bound",
        "Bound a legacy pending action to controller-owned trusted static inputs.",
        action_id=action_id,
    )


def fresh_workspace_status(project_root: Path) -> dict[str, Any]:
    status = load_sibling_module("workspace_status")
    return status.build_status_document(project_root)


def open_requests(project_root: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    source_requests = load_sibling_module("source_requests")
    try:
        records = source_requests.load_requests(source_requests.requests_path(project_root, config))
    except SystemExit as exc:
        raise OrchestrationControllerError("SOURCE_REQUESTS_INVALID", str(exc)) from exc
    selected = [record for record in records if isinstance(record, dict) and record.get("status") == "open"]
    return sorted(
        selected,
        key=lambda item: (
            PRIORITY_ORDER.get(str(item.get("priority", "medium")), 1),
            str(item.get("created_at") or ""),
            str(item.get("request_id") or ""),
        ),
    )


def candidate_store_path(project_root: Path, config: dict[str, Any]) -> Path:
    integrations = config.get("integrations") if isinstance(config.get("integrations"), dict) else {}
    discovery = integrations.get("discovery") if isinstance(integrations.get("discovery"), dict) else {}
    value = discovery.get("candidate_store_path", "sources/discovery/candidates.jsonl")
    if not isinstance(value, str) or not value.strip():
        value = "sources/discovery/candidates.jsonl"
    path = PurePosixPath(value.strip().replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or path.parts[:1] != ("sources",):
        raise OrchestrationControllerError(
            "CONFIG_INVALID",
            "integrations.discovery.candidate_store_path must be workspace-relative under sources/",
        )
    return project_root / path.as_posix()


def safe_snapshot_relative_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    if not normalized or normalized.startswith("/") or WINDOWS_ABSOLUTE_RE.match(value):
        return False
    path = PurePosixPath(normalized)
    return not path.is_absolute() and ".." not in path.parts and path.as_posix() == normalized


def configured_raw_source_roots(config: dict[str, Any]) -> list[PurePosixPath]:
    """The workspace-relative raw roots every raw-evidence baseline fingerprints.

    One reader for the one list, so "pinned by the raw-tree guard" means the same set of
    paths to the guard that takes the fingerprint and to the check that refuses to
    re-derive from an input the fingerprint never covered.
    """
    raw = config.get("raw") if isinstance(config.get("raw"), dict) else {}
    configured = raw.get("source_roots") if isinstance(raw.get("source_roots"), list) else []
    roots: list[PurePosixPath] = []
    for value in configured:
        if not isinstance(value, str) or not value.strip():
            continue
        relative = PurePosixPath(value.strip().replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("raw",):
            raise OrchestrationControllerError(
                "CONFIG_INVALID",
                "raw.source_roots must contain workspace-relative paths under raw/",
            )
        roots.append(relative)
    return roots


def raw_tree_snapshot(
    project_root: Path,
    config: dict[str, Any],
    *,
    include_entries: bool = False,
) -> dict[str, Any]:
    """Return a bounded content fingerprint for configured immutable raw roots."""
    roots: list[Path] = [
        project_root / relative.as_posix() for relative in configured_raw_source_roots(config)
    ]
    records: list[str] = []
    entries: dict[str, str] = {}
    total_bytes = 0
    seen: set[str] = set()

    def raw_error(message: str) -> OrchestrationControllerError:
        return OrchestrationControllerError(
            "ORCHESTRATION_WORKSPACE_UNSAFE",
            message,
            recoverable=True,
            remediation="Replace links or special files in raw/ and keep the immutable evidence tree bounded.",
        )

    def visit(path: Path) -> None:
        nonlocal total_bytes
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise raw_error(f"could not inspect immutable raw evidence: {path}: {exc}") from exc
        if path_is_link_like(path, metadata):
            raise raw_error(f"immutable raw evidence contains a symbolic link or junction: {path}")
        if stat.S_ISDIR(metadata.st_mode):
            try:
                children = sorted(path.iterdir(), key=lambda child: child.name)
            except OSError as exc:
                raise raw_error(f"could not enumerate immutable raw evidence: {path}: {exc}") from exc
            for child in children:
                visit(child)
            return
        if not stat.S_ISREG(metadata.st_mode) or int(getattr(metadata, "st_nlink", 1) or 1) != 1:
            raise raw_error(f"immutable raw evidence is not a singly linked regular file: {path}")
        relative = relative_workspace_path(project_root, path)
        if relative in seen:
            return
        seen.add(relative)
        if len(records) >= MAX_RAW_TREE_SNAPSHOT_ENTRIES:
            raise raw_error(f"immutable raw evidence exceeds {MAX_RAW_TREE_SNAPSHOT_ENTRIES} files")
        declared_size = int(metadata.st_size)
        if declared_size > MAX_RAW_TREE_SNAPSHOT_BYTES - total_bytes:
            raise raw_error(f"immutable raw evidence exceeds {MAX_RAW_TREE_SNAPSHOT_BYTES} bytes")
        try:
            digest = file_digest(
                path,
                max_bytes=declared_size,
                containment_root=project_root,
            )
        except OrchestrationControllerError as exc:
            raise raw_error(f"could not fingerprint immutable raw evidence: {relative}: {exc}") from exc
        if digest is None:
            raise raw_error(f"immutable raw evidence changed while it was fingerprinted: {relative}")
        total_bytes += declared_size
        records.append(f"{relative}\0{declared_size}\0{digest}")
        entries[relative] = digest

    for root in sorted(set(roots), key=lambda path: path.as_posix()):
        try:
            relative_root = root.relative_to(project_root)
        except ValueError as exc:  # pragma: no cover - roots are assembled above
            raise raw_error(f"immutable raw root escapes the workspace: {root}") from exc
        current = project_root
        root_exists = True
        for part in relative_root.parts:
            current /= part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                root_exists = False
                break
            except OSError as exc:
                raise raw_error(f"could not inspect immutable raw root: {current}: {exc}") from exc
            if not stat.S_ISDIR(metadata.st_mode) or path_is_link_like(current, metadata):
                raise raw_error(f"immutable raw root ancestor is not a real directory: {current}")
        if root_exists:
            visit(root)
    records.sort()
    digest = hashlib.sha256("\n".join(records).encode()).hexdigest()
    snapshot = {
        "algorithm": "sha256-content-v1",
        "file_count": len(records),
        "total_bytes": total_bytes,
        "fingerprint": f"sha256:{digest}",
    }
    if include_entries:
        snapshot["entries"] = dict(sorted(entries.items()))
    return snapshot


def evidence_manifest_digest(project_root: Path) -> str | None:
    return file_digest(
        project_root / "sources" / "manifest.jsonl",
        max_bytes=MAX_MANIFEST_SNAPSHOT_BYTES,
        containment_root=project_root,
    )


def valid_sha256_fingerprint(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def valid_raw_tree_snapshot(value: Any, *, include_entries: bool = False) -> bool:
    required = {"algorithm", "file_count", "total_bytes", "fingerprint"}
    if include_entries:
        required.add("entries")
    if not isinstance(value, dict) or set(value) != required:
        return False
    if (
        value.get("algorithm") != "sha256-content-v1"
        or isinstance(value.get("file_count"), bool)
        or not isinstance(value.get("file_count"), int)
        or not 0 <= value["file_count"] <= MAX_RAW_TREE_SNAPSHOT_ENTRIES
        or isinstance(value.get("total_bytes"), bool)
        or not isinstance(value.get("total_bytes"), int)
        or not 0 <= value["total_bytes"] <= MAX_RAW_TREE_SNAPSHOT_BYTES
        or not valid_sha256_fingerprint(value.get("fingerprint"))
    ):
        return False
    if not include_entries:
        return True
    entries = value.get("entries")
    return (
        isinstance(entries, dict)
        and len(entries) == value["file_count"]
        and len(entries) <= MAX_RAW_TREE_SNAPSHOT_ENTRIES
        and all(
            isinstance(path, str)
            and path.startswith("raw/")
            and len(path) <= MAX_ARTIFACT_PATH_LENGTH
            and safe_snapshot_relative_path(path)
            and valid_sha256_fingerprint(fingerprint)
            for path, fingerprint in entries.items()
        )
    )


def require_immutability_baselines(work_order: dict[str, Any]) -> None:
    """Fail closed when a pending action predates pre-action immutability guards."""
    phase = work_order.get("phase")
    if phase not in {"discovery", "candidate_review"}:
        return
    checks = {
        item.get("check"): item
        for item in work_order.get("required_postconditions", [])
        if isinstance(item, dict) and isinstance(item.get("check"), str)
    }
    manifest_check = "discovery_never_fetches" if phase == "discovery" else "selection_does_not_fetch"
    raw_before = checks.get("raw_tree_unchanged", {}).get("before")
    manifest_guard = checks.get(manifest_check, {})
    manifest_digest = manifest_guard.get("manifest_digest_before")
    if valid_raw_tree_snapshot(raw_before) and (
        manifest_digest is None or valid_sha256_fingerprint(manifest_digest)
    ) and "manifest_digest_before" in manifest_guard:
        return
    raise OrchestrationControllerError(
        "ORCHESTRATION_IMMUTABILITY_BASELINE_UNAVAILABLE",
        f"pending {phase} work predates the raw/manifest immutability baseline and cannot be replayed safely",
        recoverable=False,
        remediation=(
            "Preserve this orchestration for audit and start a fresh orchestration session from the current "
            "workspace state. Never bind raw or manifest digests after worker execution."
        ),
        details={"action_id": work_order.get("action_id")},
    )


def require_research_question_baseline(work_order: dict[str, Any]) -> None:
    """Refuse legacy research replay when no trustworthy pre-action state exists."""
    if work_order.get("phase") != "research":
        return
    guard = next(
        (
            item
            for item in work_order.get("required_postconditions", [])
            if isinstance(item, dict) and item.get("check") == "workspace_readiness_changed"
        ),
        None,
    )
    baseline = guard.get("scoped_questions_before") if isinstance(guard, dict) else None
    question_files_before = (
        guard.get("question_file_fingerprints_before") if isinstance(guard, dict) else None
    )
    source_requests_before = (
        guard.get("source_request_record_fingerprints_before") if isinstance(guard, dict) else None
    )
    if (
        isinstance(baseline, dict)
        and baseline
        and valid_question_file_fingerprint_snapshot(question_files_before)
        and valid_record_fingerprint_snapshot(source_requests_before)
        and all(f"{slug}.md" in question_files_before for slug in baseline)
    ):
        return
    raise OrchestrationControllerError(
        "ORCHESTRATION_RESEARCH_BASELINE_UNAVAILABLE",
        "pending research work predates the scoped-question baseline and cannot be replayed safely",
        recoverable=False,
        remediation=(
            "Preserve this orchestration for audit and start a fresh orchestration session from the current "
            "workspace state. Do not hand-edit the retained work order or infer a baseline after worker execution."
        ),
        details={"action_id": work_order.get("action_id")},
    )


def require_discovery_candidate_baseline(work_order: dict[str, Any]) -> None:
    """Refuse discovery replay when newly created candidate IDs cannot be proven."""
    if work_order.get("phase") != "discovery":
        return
    guard = next(
        (
            item
            for item in work_order.get("required_postconditions", [])
            if isinstance(item, dict) and item.get("check") == "request_scoped_candidates_increased"
        ),
        None,
    )
    baseline = guard.get("candidate_states_before") if isinstance(guard, dict) else None
    record_baseline = guard.get("candidate_record_fingerprints_before") if isinstance(guard, dict) else None
    before = guard.get("before") if isinstance(guard, dict) else None
    if (
        valid_candidate_state_baseline(baseline)
        and valid_record_fingerprint_snapshot(record_baseline)
        and set(baseline) <= set(record_baseline)
        and isinstance(before, int)
        and not isinstance(before, bool)
        and before == len(baseline)
    ):
        return
    raise OrchestrationControllerError(
        "ORCHESTRATION_DISCOVERY_BASELINE_UNAVAILABLE",
        "pending discovery work predates the bounded candidate-state baseline and cannot be replayed safely",
        recoverable=False,
        remediation=(
            "Preserve this orchestration for audit and start a fresh orchestration session from the current "
            "workspace state. Do not hand-edit the retained work order or infer candidate creation after execution."
        ),
        details={"action_id": work_order.get("action_id")},
    )


def require_action_baselines(
    work_order: dict[str, Any],
    project_root: Path | None = None,
) -> dict[str, Any]:
    resolved = hydrate_integrity_baselines(project_root, work_order) if project_root is not None else work_order
    require_immutability_baselines(resolved)
    require_research_question_baseline(resolved)
    require_discovery_candidate_baseline(resolved)
    require_candidate_review_selection_baseline(resolved)
    require_acquisition_evidence_baselines(resolved)
    return resolved


def valid_candidate_state_baseline(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and len(value) <= MAX_SCOPE_IDS
        and all(
            isinstance(candidate_id, str)
            and bool(candidate_id)
            and len(candidate_id) <= MAX_SCOPE_ID_LENGTH
            and "\x00" not in candidate_id
            and isinstance(state, str)
            and len(state) <= 64
            and re.fullmatch(r"[a-z][a-z0-9_-]*", state) is not None
            for candidate_id, state in value.items()
        )
    )


def valid_scope_id_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= MAX_SCOPE_IDS
        and all(
            isinstance(item, str)
            and bool(item)
            and len(item) <= MAX_SCOPE_ID_LENGTH
            and "\x00" not in item
            for item in value
        )
        and len(value) == len(set(value))
    )


def valid_blocked_question_baseline(value: Any) -> bool:
    if not isinstance(value, dict) or len(value) > MAX_SCOPE_IDS:
        return False
    for slug, snapshot in value.items():
        if (
            not isinstance(slug, str)
            or not slug
            or len(slug) > MAX_SCOPE_ID_LENGTH
            or "\x00" in slug
            or not isinstance(snapshot, dict)
            or set(snapshot) != {"status", "blocking_request_ids", "source_ids_before"}
            or snapshot.get("status") != "blocked"
            or not valid_scope_id_list(snapshot.get("blocking_request_ids"))
            or not snapshot.get("blocking_request_ids")
            or not valid_scope_id_list(snapshot.get("source_ids_before"))
        ):
            return False
    return True


def require_acquisition_evidence_baselines(work_order: dict[str, Any]) -> None:
    """Refuse acquisition replay when exact pre-action reconciliation cannot be proven."""
    if work_order.get("phase") != "acquisition":
        return
    question_guard = next(
        (
            item
            for item in work_order.get("required_postconditions", [])
            if isinstance(item, dict) and item.get("check") == "linked_blocked_questions_reopened"
        ),
        None,
    )
    manifest_guard = next(
        (
            item
            for item in work_order.get("required_postconditions", [])
            if isinstance(item, dict) and item.get("check") == "manifest_records_increased"
        ),
        None,
    )
    blocked_questions_before = (
        question_guard.get("blocked_questions_before") if isinstance(question_guard, dict) else None
    )
    matching_source_ids_before = (
        manifest_guard.get("matching_source_ids_before") if isinstance(manifest_guard, dict) else None
    )
    matching_source_records_before = (
        manifest_guard.get("matching_source_records_before") if isinstance(manifest_guard, dict) else None
    )
    # Additive and optional, the way `acquisition_mode` is: an order issued before reuse of
    # un-normalized evidence existed carries no key, replays with that reuse unavailable, and
    # is not refused for it. Present-but-malformed is still refused, which is why absence
    # defaults to an empty list rather than skipping the check.
    reusable_source_ids_before = (
        manifest_guard.get("reusable_source_ids_before", []) if isinstance(manifest_guard, dict) else None
    )
    manifest_records_before = (
        manifest_guard.get("manifest_record_fingerprints_before") if isinstance(manifest_guard, dict) else None
    )
    raw_tree_before = manifest_guard.get("raw_tree_before") if isinstance(manifest_guard, dict) else None
    candidate_records_before = (
        manifest_guard.get("candidate_record_fingerprints_before") if isinstance(manifest_guard, dict) else None
    )
    candidate_audit_records_before = (
        manifest_guard.get("candidate_audit_record_fingerprints_before")
        if isinstance(manifest_guard, dict)
        else None
    )
    source_requests_before = (
        manifest_guard.get("source_request_record_fingerprints_before") if isinstance(manifest_guard, dict) else None
    )
    normalized_files_before = (
        manifest_guard.get("normalized_file_fingerprints_before") if isinstance(manifest_guard, dict) else None
    )
    question_files_before = (
        manifest_guard.get("question_file_fingerprints_before") if isinstance(manifest_guard, dict) else None
    )
    if valid_blocked_question_baseline(blocked_questions_before) and valid_scope_id_list(
        matching_source_ids_before
    ) and valid_matching_source_record_snapshot(matching_source_records_before) and (
        set(matching_source_ids_before) == set(matching_source_records_before)
    ) and valid_record_fingerprint_snapshot(manifest_records_before) and (
        set(matching_source_ids_before) <= set(manifest_records_before)
    ) and valid_unnormalized_reuse_baseline(
        reusable_source_ids_before, matching_source_ids_before, manifest_records_before
    ) and valid_raw_tree_snapshot(raw_tree_before, include_entries=True) and valid_record_fingerprint_snapshot(
        candidate_records_before
    ) and valid_record_fingerprint_snapshot(candidate_audit_records_before) and valid_record_fingerprint_snapshot(
        source_requests_before
    ) and valid_file_fingerprint_snapshot(
        normalized_files_before,
        prefix="sources/",
    ) and valid_question_file_fingerprint_snapshot(question_files_before):
        return
    raise OrchestrationControllerError(
        "ORCHESTRATION_ACQUISITION_BASELINE_UNAVAILABLE",
        "pending acquisition work predates the bounded question/evidence baseline and cannot be replayed safely",
        recoverable=False,
        remediation=(
            "Preserve this orchestration for audit and start a fresh orchestration session from the current "
            "workspace state. Do not infer question transitions or matching evidence after worker execution."
        ),
        details={"action_id": work_order.get("action_id")},
    )


def require_candidate_review_selection_baseline(work_order: dict[str, Any]) -> None:
    if work_order.get("phase") != "candidate_review":
        return
    guard = next(
        (
            item
            for item in work_order.get("required_postconditions", [])
            if isinstance(item, dict) and item.get("check") == "selected_candidate_for_request"
        ),
        None,
    )
    baseline = guard.get("selected_candidate_ids_before") if isinstance(guard, dict) else None
    record_baseline = guard.get("candidate_record_fingerprints_before") if isinstance(guard, dict) else None
    before = guard.get("selected_before") if isinstance(guard, dict) else None
    if (
        valid_scope_id_list(baseline)
        and valid_record_fingerprint_snapshot(record_baseline)
        and set(baseline) <= set(record_baseline)
        and isinstance(before, int)
        and not isinstance(before, bool)
        and before == len(baseline)
    ):
        return
    raise OrchestrationControllerError(
        "ORCHESTRATION_CANDIDATE_REVIEW_BASELINE_UNAVAILABLE",
        "pending candidate-review work predates the bounded selected-candidate baseline and cannot be replayed safely",
        recoverable=False,
        remediation=(
            "Preserve this orchestration for audit and start a fresh orchestration session from the current "
            "workspace state. Do not infer which candidate selections occurred after execution."
        ),
        details={"action_id": work_order.get("action_id")},
    )


def load_candidates(project_root: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    path = candidate_store_path(project_root, config)
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OrchestrationControllerError(
                "CANDIDATE_STORE_INVALID",
                f"invalid candidate JSONL at line {line_number}: {exc}",
            ) from exc
        if isinstance(item, dict):
            records.append(item)
    return records


def canonical_json_fingerprint(value: Any, *, label: str) -> tuple[str, int]:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise OrchestrationControllerError(
            "ORCHESTRATION_SCOPE_INVALID",
            f"{label} cannot be canonically fingerprinted: {exc}",
            recoverable=False,
        ) from exc
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}", len(encoded)


def record_fingerprint_snapshot(
    records: list[dict[str, Any]],
    *,
    id_field: str,
    label: str,
) -> dict[str, str]:
    """Capture all existing record identities without retaining record content."""
    snapshot: dict[str, str] = {}
    total_bytes = 0
    for record in records:
        record_id = record.get(id_field) if isinstance(record, dict) else None
        if (
            not isinstance(record_id, str)
            or not record_id
            or len(record_id) > MAX_SCOPE_ID_LENGTH
            or "\x00" in record_id
        ):
            raise OrchestrationControllerError(
                "ORCHESTRATION_SCOPE_INVALID",
                f"{label} contains a record without a bounded {id_field}",
                recoverable=False,
            )
        if record_id in snapshot:
            raise OrchestrationControllerError(
                "ORCHESTRATION_SCOPE_INVALID",
                f"{label} contains duplicate id: {record_id}",
                recoverable=False,
            )
        if len(snapshot) >= MAX_SCOPE_GUARD_ENTRIES:
            raise OrchestrationControllerError(
                "ORCHESTRATION_SCOPE_EXCEEDED",
                f"{label} exceeds the {MAX_SCOPE_GUARD_ENTRIES}-record integrity-guard limit",
                recoverable=False,
                remediation=f"Archive or split {label} before starting another managed action.",
            )
        fingerprint, encoded_bytes = canonical_json_fingerprint(record, label=label)
        total_bytes += encoded_bytes
        if total_bytes > MAX_SCOPE_GUARD_BYTES:
            raise OrchestrationControllerError(
                "ORCHESTRATION_SCOPE_EXCEEDED",
                f"{label} exceeds the {MAX_SCOPE_GUARD_BYTES}-byte integrity-guard limit",
                recoverable=False,
                remediation=f"Archive or split {label} before starting another managed action.",
            )
        snapshot[record_id] = fingerprint
    return dict(sorted(snapshot.items()))


def valid_record_fingerprint_snapshot(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and len(value) <= MAX_SCOPE_GUARD_ENTRIES
        and all(
            isinstance(record_id, str)
            and bool(record_id)
            and len(record_id) <= MAX_SCOPE_ID_LENGTH
            and "\x00" not in record_id
            and valid_sha256_fingerprint(fingerprint)
            for record_id, fingerprint in value.items()
        )
    )


def fingerprint_scope_violations(
    before: dict[str, str],
    after: dict[str, str],
    *,
    mutable_ids: set[str],
    allowed_new_ids: set[str] | None = None,
) -> dict[str, list[str]]:
    """Describe bounded identity changes outside an explicitly mutable scope."""
    allowed_new = allowed_new_ids if allowed_new_ids is not None else set()
    removed = sorted(set(before) - set(after))
    added = sorted(set(after) - set(before) - allowed_new)
    changed = sorted(
        record_id
        for record_id in set(before) & set(after)
        if record_id not in mutable_ids and before[record_id] != after[record_id]
    )
    return {
        "removed": removed[:MAX_TRUSTED_STATIC_INPUT_DIFFERENCES],
        "added_outside_scope": added[:MAX_TRUSTED_STATIC_INPUT_DIFFERENCES],
        "changed_outside_scope": changed[:MAX_TRUSTED_STATIC_INPUT_DIFFERENCES],
    }


def candidate_record_fingerprint_snapshot(candidates: list[dict[str, Any]]) -> dict[str, str]:
    return record_fingerprint_snapshot(candidates, id_field="candidate_id", label="candidate store")


def source_request_record_fingerprint_snapshot(
    project_root: Path,
    config: dict[str, Any],
) -> dict[str, str]:
    source_requests = load_sibling_module("source_requests")
    records = source_requests.load_requests(source_requests.requests_path(project_root, config))
    return record_fingerprint_snapshot(records, id_field="request_id", label="source-request store")


def file_tree_fingerprint_snapshot(
    project_root: Path,
    root: Path,
    *,
    label: str,
) -> dict[str, str]:
    """Fingerprint a bounded regular-file tree without following links or junctions."""
    snapshot: dict[str, str] = {}
    total_bytes = 0

    def fail(message: str) -> OrchestrationControllerError:
        return OrchestrationControllerError(
            "ORCHESTRATION_WORKSPACE_UNSAFE",
            f"{label} {message}",
            recoverable=True,
            remediation=f"Replace links or special files and keep the {label} tree bounded.",
        )

    def visit(path: Path) -> None:
        nonlocal total_bytes
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise fail(f"could not be inspected: {path}: {exc}") from exc
        if path_is_link_like(path, metadata):
            raise fail(f"contains a symbolic link or junction: {path}")
        if stat.S_ISDIR(metadata.st_mode):
            try:
                children = sorted(path.iterdir(), key=lambda item: item.name)
            except OSError as exc:
                raise fail(f"could not be enumerated: {path}: {exc}") from exc
            for child in children:
                visit(child)
            return
        if not stat.S_ISREG(metadata.st_mode) or int(getattr(metadata, "st_nlink", 1) or 1) != 1:
            raise fail(f"contains a non-regular or multiply linked file: {path}")
        if len(snapshot) >= MAX_SCOPE_GUARD_ENTRIES:
            raise OrchestrationControllerError(
                "ORCHESTRATION_SCOPE_EXCEEDED",
                f"{label} exceeds the {MAX_SCOPE_GUARD_ENTRIES}-file integrity-guard limit",
                recoverable=False,
            )
        size = int(metadata.st_size)
        total_bytes += size
        if total_bytes > MAX_SCOPE_GUARD_BYTES:
            raise OrchestrationControllerError(
                "ORCHESTRATION_SCOPE_EXCEEDED",
                f"{label} exceeds the {MAX_SCOPE_GUARD_BYTES}-byte integrity-guard limit",
                recoverable=False,
            )
        relative = relative_workspace_path(project_root, path)
        fingerprint = file_digest(path, max_bytes=size, containment_root=project_root)
        if fingerprint is None:
            raise fail(f"changed while it was fingerprinted: {relative}")
        snapshot[relative] = fingerprint

    visit(root)
    return dict(sorted(snapshot.items()))


def record_declares_structured_view(record_path: Path) -> bool:
    """Whether a normalized record binds a structured-view sidecar beside itself.

    Normalization writes the sidecar *before* the record so the record can bind its digest,
    which means an action that produces a structured source adds **two** files under the
    normalized root: the record, and a companion the package itself wrote. The scope guards
    below have to allow that companion, and this decides when it is legitimate to.

    Gated on the record's own declaration rather than allowed unconditionally. The
    controller runs no record-contract validation — it fingerprints files and nothing else —
    so nothing in an acquisition flow would otherwise refuse an *undeclared* sidecar, and
    `_normalized_contract` calls that "a breach in the other direction … the one that would
    otherwise be invisible". Allowing the path only when the record asks for it keeps the
    guard's reach one file wider, not one directory wider.

    Fails closed: a record that cannot be read or parsed authorizes nothing beside itself.
    """
    contract = load_sibling_module("_normalized_contract")
    try:
        text = record_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    frontmatter, _, error = contract.split_record(text)
    if error is not None or not isinstance(frontmatter, dict):
        return False
    return isinstance(frontmatter.get("structured_view"), dict)


def allowed_normalized_paths_for_record(
    project_root: Path,
    record_path: Path,
    normalize_sources: ModuleType,
) -> set[str]:
    """Every normalized path one record legitimately accounts for.

    The record, plus the structured-view sidecar beside it when the record declares one.
    Single-sourced so the three postcondition paths cannot drift: they answer the same
    question about the same tree, and one of them allowing a companion the others refuse is
    how a delivered source becomes fulfillable through one route and not another.
    """
    paths = {relative_workspace_path(project_root, record_path)}
    if record_declares_structured_view(record_path):
        paths.add(
            relative_workspace_path(
                project_root, normalize_sources.structured_view_path_for_record(record_path)
            )
        )
    return paths


def normalized_output_scope(
    project_root: Path,
    normalized_root: Path,
    expected_new_source_ids: Iterable[str],
    by_source_id: dict[str, Any],
    normalize_sources: ModuleType,
) -> tuple[set[str], set[str]]:
    """The normalized paths an action may create, and those it must.

    Returns ``(allowed, required)``. The record is both — an action that fulfils a source
    owes exactly one record. Its structured-view sidecar is allowed and never required,
    because a source binding no view writes only the record; demanding it would refuse
    every non-structured fulfilment, which is how a naive fix to this breaks the other
    direction.

    Shaped after the raw-evidence check in the same functions, which has always allowed
    `<file>.provenance.yml` beside a delivery for the same reason.
    """
    allowed: set[str] = set()
    required: set[str] = set()
    for source_id in expected_new_source_ids:
        record = by_source_id.get(source_id)
        if not isinstance(record, dict):
            continue
        record_path = normalize_sources.normalized_output_path_for_record(record, normalized_root)
        allowed |= allowed_normalized_paths_for_record(project_root, record_path, normalize_sources)
        required.add(relative_workspace_path(project_root, record_path))
    return allowed, required


class RederivationSandboxError(RuntimeError):
    """The bounded workspace copy a re-derivation must run inside could not be built."""


def reusable_unnormalized_record_kind(record: Any, normalize_sources: ModuleType) -> bool:
    """Whether un-normalized reuse of this record can ever be verified.

    Issuance and verification must agree on this or the order they produce cannot be
    completed: ``acquisition_reuse_baselines`` would admit the source,
    ``normalized_output_scope`` would then *require* its normalized output, and the
    derivation check would refuse that output unconditionally -- a refusal naming nothing
    the acquirer can repair. One predicate, asked by both sides.

    A ``codebase:`` record normalizes from an artifact bundle under the configured
    codebase output directory, which no baseline fingerprints, so re-deriving it would
    confirm the body against an input the acquirer can write.
    """
    return isinstance(record, dict) and not normalize_sources.is_codebase_record(record)


def declared_record_input_paths(record: dict[str, Any], normalize_sources: ModuleType) -> list[str]:
    """Every workspace path a record declares as an input to its own normalization.

    Deliberately the normalizer's own reader rather than a second list here. Both the
    unpinned-input refusal and the re-derivation sandbox take their completeness from this,
    so a record kind that later declares a path field the controller's private copy had
    never heard of would leave the check confirming a reused body against bytes no baseline
    pins -- a security guard quietly narrowing rather than visibly breaking.
    """
    return list(normalize_sources.raw_paths(record, []))


def unpinned_record_input_paths(
    project_root: Path,
    config: dict[str, Any],
    record: dict[str, Any],
    normalize_sources: ModuleType,
) -> list[str]:
    """Inputs this record normalizes from that no raw-evidence baseline fingerprints.

    ``raw_tree_snapshot`` walks the configured ``raw.source_roots`` and nothing else,
    while a record's own path fields resolve through ``safe_workspace_path``, which
    accepts any workspace-relative path. So a record may name ``raw_pdf``, a raw path or a
    ``latex_root`` that sits outside every fingerprinted root -- and re-deriving from it
    would confirm the reused body against bytes the order never pinned, which is exactly
    the set a *failed* prior order leaves behind.

    This is ``is_codebase_record``'s early-out in its general form: the answer is to refuse
    the reuse and name the path, not to check it against an input the acquirer can write.
    Purely lexical, so a record naming a path that does not exist is refused for the same
    reason rather than for a different one.
    """
    roots = configured_raw_source_roots(config)
    unpinned: set[str] = set()
    for value in declared_record_input_paths(record, normalize_sources):
        if normalize_sources.safe_workspace_path(project_root, value) is None:
            unpinned.add(value)
            continue
        relative = PurePosixPath(value.strip().replace("\\", "/"))
        if not any(relative == root or root in relative.parents for root in roots):
            unpinned.add(value)
    return sorted(unpinned)


def adapter_workspace_command_paths(project_root: Path, config: dict[str, Any], record: Any) -> list[str]:
    """Workspace paths the configured adapter's own argv names.

    The adapter command is operator-written and validated as an argv list, not as a path
    under any particular tree: ``["python3", "tools/structured_adapter.py"]`` is as valid as
    one under ``scripts/``. The sandbox has to carry whatever of it lives in the workspace,
    or a perfectly good adapter would run under ``normalize_sources.py`` and fail only under
    verification -- refused as evidence that could not be re-derived, with a remediation
    about hand-editing and no way for the operator to reconcile the two.
    """
    normalize_sources = load_sibling_module("normalize_sources")
    kind = record.get("kind") if isinstance(record, dict) else None
    adapters = tuple(normalize_sources.normalization_config(config)["adapters"])
    adapter = normalize_sources.adapter_for_kind(adapters, kind)
    if adapter is None:
        return []
    named: list[str] = []
    for value in getattr(adapter, "command", ()):
        if not isinstance(value, str) or not value.strip():
            continue
        candidate = PurePosixPath(value.strip().replace("\\", "/"))
        if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
            continue
        if project_root.joinpath(*candidate.parts).exists():
            named.append(candidate.as_posix())
    return named


@contextmanager
def rederivation_root(
    project_root: Path,
    item: Any,
    normalize_sources: ModuleType,
    input_relatives: Iterable[str],
) -> Iterator[Path]:
    """The workspace root a re-derivation runs against.

    Every built-in extractor reads bytes and returns text: nothing it does can leave a
    mark on the workspace, so it re-derives in place. An external normalizer adapter is
    different -- it is an arbitrary program the controller runs with the workspace as its
    working directory, *inside* the guard that adjudicates who wrote what. Pinning its
    argv in ``research.yml`` pins what runs, not what it does, and anything it writes on
    the way past lands in the post-state the next comparison reads: a scratch file under
    ``raw/`` is reported as the acquirer changing immutable evidence, one under the
    normalized tree as the acquirer rewriting evidence, one under ``docs/`` as a tampered
    trusted input. The operator deletes it, resubmits, and verification writes it again --
    a loop whose diagnostic names the wrong party every time round.

    So an adapter re-derivation gets a throwaway copy instead: the trusted static inputs
    it may need to run at all, plus this one record's own raw evidence, and nothing else.
    Writes land there and are discarded with it. The copy is bounded, and a record whose
    inputs do not fit is refused rather than verified against the live tree.
    """
    if item.method != normalize_sources.ADAPTER_METHOD:
        yield project_root
        return
    with tempfile.TemporaryDirectory(prefix="evidence-wiki-rederive-") as temporary:
        sandbox = Path(temporary)
        copied = {"files": 0, "bytes": 0}

        def copy_entry(relative: PurePosixPath) -> None:
            if relative.as_posix() in TRUSTED_STATIC_EXCLUDED_SUBTREES:
                return
            source = project_root.joinpath(*relative.parts)
            try:
                metadata = source.lstat()
            except FileNotFoundError:
                return
            except OSError as exc:
                raise RederivationSandboxError(f"could not inspect {relative.as_posix()}: {exc}") from exc
            if path_is_link_like(source, metadata):
                raise RederivationSandboxError(f"{relative.as_posix()} is a symbolic link or junction")
            destination = sandbox.joinpath(*relative.parts)
            if stat.S_ISDIR(metadata.st_mode):
                destination.mkdir(parents=True, exist_ok=True)
                try:
                    children = sorted(source.iterdir(), key=lambda child: child.name)
                except OSError as exc:
                    raise RederivationSandboxError(
                        f"could not enumerate {relative.as_posix()}: {exc}"
                    ) from exc
                for child in children:
                    copy_entry(relative / child.name)
                return
            # Singly linked, not merely regular: every sibling integrity guard requires it
            # (`file_tree_fingerprint_snapshot`, `file_digest`), because a second name for
            # the same inode makes "which path was verified" unanswerable. A sandbox that
            # copied one would re-derive from bytes no baseline fingerprinted under that
            # name.
            if not stat.S_ISREG(metadata.st_mode) or int(getattr(metadata, "st_nlink", 1) or 1) != 1:
                raise RederivationSandboxError(
                    f"{relative.as_posix()} is not a singly linked regular file"
                )
            copied["files"] += 1
            copied["bytes"] += int(metadata.st_size)
            if (
                copied["files"] > MAX_REDERIVATION_SANDBOX_FILES
                or copied["bytes"] > MAX_REDERIVATION_SANDBOX_BYTES
            ):
                raise RederivationSandboxError(
                    "the inputs this re-derivation needs exceed the bounded verification sandbox"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                # copy2, not copyfile: an adapter may be an executable script under a
                # trusted tree, and a copy stripped of its mode bits fails to exec for a
                # reason that has nothing to do with the evidence.
                shutil.copy2(source, destination)
            except OSError as exc:
                raise RederivationSandboxError(f"could not copy {relative.as_posix()}: {exc}") from exc

        for value in (*TRUSTED_STATIC_FILE_PATHS, *TRUSTED_STATIC_TREE_PATHS, *input_relatives):
            copy_entry(PurePosixPath(value.strip().replace("\\", "/")))
        yield sandbox


def stamped_pdf_extractor(
    frontmatter: dict[str, Any],
    normalize_sources: ModuleType,
) -> tuple[dict[str, Any] | None, Any]:
    """Resolve the extractor a PDF record names, through this package's own allowlist.

    The record is the file the acquirer wrote, so ``pdf_extractor.name`` in it is
    untrusted text. ``normalize_selected_record`` takes ``pdf_extractor or
    resolve_pdf_extractor(...)``, and a bare ``str`` there has one historical meaning --
    *a resolved pdftotext executable path* -- which becomes ``argv[0]`` of a subprocess.
    Forwarding the stamped name unchecked therefore let the record choose the program
    postcondition verification runs, in the middle of the trust boundary itself.

    It also never worked in the direction it was written for: a record this package
    produces stamps ``pypdf``, and there is no executable by that name, so every
    legitimate PDF reuse failed as "could not be re-derived".

    Resolving through ``resolve_pdf_extractor`` fixes both. The name must be one of
    ``PDF_EXTRACTORS``, and what reaches the extractor is a real ``PdfExtractor`` whose
    executable, for poppler, is found on PATH exactly as an ordinary run finds it. An
    unknown name and an unavailable extractor are each a refusal with its own reason,
    never an attempt to execute something.
    """
    stamped = frontmatter.get("pdf_extractor")
    name = stamped.get("name") if isinstance(stamped, dict) else None
    if not isinstance(name, str) or name not in normalize_sources.PDF_EXTRACTORS:
        return (
            {
                "reason": "normalized evidence names a PDF extractor this package does not implement",
                "pdf_extractor": name if isinstance(name, str) else None,
            },
            None,
        )
    try:
        return None, normalize_sources.resolve_pdf_extractor(name)
    except SystemExit as exc:
        return (
            {
                "reason": "the PDF extractor the normalized evidence names is unavailable on this host",
                "pdf_extractor": name,
                "error": str(exc),
            },
            None,
        )


def carry_version_stamps(expected: dict[str, Any], stamped: dict[str, Any]) -> None:
    """Take the two version stamps from the file instead of from this installation.

    ``normalizer.version`` and ``pdf_extractor.version`` are stamped from whatever is
    installed at the moment of the run, and both land in the compared bytes. Comparing
    them would bind this verdict to the host rather than to the evidence: an
    ``evidence-wiki upgrade`` between issuance and submission, a different virtualenv, or
    an order replayed across a version bump would each turn a legitimate reuse into
    "normalized evidence is not what normalizing the raw evidence produces" -- with a
    remediation that blames hand-editing.

    That would also contradict the package's own model of the same fields:
    ``normalize_sources.is_stale`` deliberately calls extractor versions "provenance
    rather than an implicit rewrite trigger". They are read back here for the same reason
    ``created``, ``updated`` and ``normalized_at`` are: they record who did the work and
    when, and nothing downstream grounds a claim in them.

    Only the versions travel. Both *names* stay derived, so a record still cannot claim a
    producer that did not produce it, and every other field, the digests and the whole
    body still have to render byte-identically.
    """
    for key in ("normalizer", "pdf_extractor"):
        derived = expected.get(key)
        recorded = stamped.get(key)
        if isinstance(derived, dict) and "version" in derived and isinstance(recorded, dict):
            derived["version"] = recorded.get("version")


def normalizer_identity_failure(
    expected: dict[str, Any],
    stamped: dict[str, Any],
) -> dict[str, Any] | None:
    """Refuse a record naming a producer other than the one configured for it, by name.

    The other half of ``carry_version_stamps``. Versions travel from the file, so a host
    upgrade is not a forgery; names stay derived, so a record cannot claim a producer that
    did not produce it. What was missing was any *statement* about the name: a stamped
    ``normalizer.name`` that disagrees with the configured one is a difference in the
    rendered bytes like any other, so it fell through to "normalized evidence is not what
    normalizing the raw evidence produces" -- whose remediation is about a hand-edited body.
    An operator reading that goes looking for an edit to the prose, while the two lines that
    actually disagree sit in the frontmatter and are never quoted back.

    So the comparison is made explicitly and reported with both sides in their own keys, the
    way ``stamped_pdf_extractor`` already reports the extractor it refused. Names only: the
    versions have been carried by the time this runs, and comparing them here would undo the
    one thing that helper exists to allow.

    Absent on both sides is not a disagreement -- a record for a kind that stamps no producer
    block has nothing to be wrong about, and the byte comparison still has the last word.

    The reason says "does not name" rather than "names another", because a record whose
    ``normalizer`` block was deleted or corrupted into something that is not a producer
    object reaches here too, and reporting that one as naming a different normalizer would
    assert something the file did not do. Either way ``normalizer`` carries what the record
    names -- ``None`` when it names nothing usable -- beside the identity that was expected.
    """
    derived = expected.get("normalizer")
    recorded = stamped.get("normalizer")
    derived_name = derived.get("name") if isinstance(derived, dict) else None
    recorded_name = recorded.get("name") if isinstance(recorded, dict) else None
    if derived_name == recorded_name:
        return None
    return {
        "reason": "normalized evidence does not name the normalizer configured for its kind",
        "normalizer": recorded_name if isinstance(recorded_name, str) else None,
        "configured_normalizer": derived_name if isinstance(derived_name, str) else None,
    }


def normalized_output_derivation_failure(
    project_root: Path,
    config: dict[str, Any],
    record: dict[str, Any],
    normalized_path: Path,
    manifest_records: list[dict[str, Any]],
    normalize_sources: ModuleType,
) -> dict[str, Any] | None:
    """Why a normalized record is not what this package's normalizer makes of its raw bytes.

    Reuse of a source nothing had normalized authorizes a *new* normalized file, and "new"
    is a statement about a path, not about a body. Without this the acquirer would author
    the body itself: the raw bytes, the sidecar and the manifest record all stay under
    guards that pin them byte-for-byte, and the one artifact left over would be the one
    everything downstream actually reads and quotes.

    So the verifier re-derives it. Every input it re-derives from is pinned: the raw bytes
    by the raw-tree guard, the manifest record by reconciliation, and ``research.yml`` --
    which names the adapter this may run -- by the trusted-input guard that already
    protects a pending order. That is what makes normalization reproducible here at all,
    and what makes a mismatch mean the body is not derived from them rather than that
    something moved underneath. It is also why one kind is turned away below: a
    ``codebase:`` record normalizes from an artifact bundle under the configured
    codebase output directory, which no baseline fingerprints, so re-deriving it would
    confirm the body against an input the acquirer can write.

    Six fields are read back out of the file instead of recomputed. ``created``,
    ``updated`` and ``normalized_at`` are stamps rather than content: they record when the
    work happened and nothing downstream grounds a claim in them, and ``normalizer.version``
    and ``pdf_extractor.version`` are the same kind of thing about the tools -- see
    ``carry_version_stamps`` for why comparing them would bind this verdict to the host.
    ``references_source_ids`` resolves this record's bibliography against *other* manifest
    records, so it answers to the manifest as it stands rather than to these raw bytes, and
    re-deriving it would refuse a reuse for a delivery that arrived beside it; it is
    admitted only as source ids the manifest actually holds, because ``query_index.py``
    reads it as citation-graph edges. Everything else -- every other frontmatter field,
    both producer *names*, the structured-view digest, the whole body -- has to render
    byte-identically, and the sidecar's own bytes are compared directly.

    Fails closed, and closed the whole way: a record that cannot be read, parsed,
    configured or re-derived is a failure, not a pass. The answer to "we could not check"
    is the same as the answer to "it did not match", because reuse is the affordance being
    granted and the fallback -- deliver it again under its own raw path, or record an
    attempt failure -- is still there. A normalizer adapter whose output is not
    reproducible from the same bytes therefore withholds this reuse rather than widening
    it, which is the correct direction for a check that exists to bind content.
    """
    if not reusable_unnormalized_record_kind(record, normalize_sources):
        return {"reason": "the reused source normalizes from artifacts this order does not fingerprint"}
    unpinned_inputs = unpinned_record_input_paths(project_root, config, record, normalize_sources)
    if unpinned_inputs:
        return {
            "reason": "the reused source normalizes from workspace paths no raw-evidence baseline pins",
            "unpinned_input_paths": unpinned_inputs[:MAX_TRUSTED_STATIC_INPUT_DIFFERENCES],
        }
    contract = load_sibling_module("_normalized_contract")
    try:
        manifest_relative, normalized_relative = normalize_sources.source_paths(config)
    except SystemExit as exc:
        return {"reason": "the workspace source paths this re-derivation needs are unusable", "error": str(exc)}
    normalized_root = project_root / normalized_relative
    payload = bounded_regular_bytes(
        normalized_path,
        max_bytes=MAX_VERIFICATION_ARTIFACT_BYTES,
        error_code="ORCHESTRATION_POSTCONDITION_FAILED",
        label="normalized evidence",
        missing_ok=True,
        containment_root=project_root,
    )
    if payload is None:
        return {"reason": "normalized evidence is missing"}
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        return {"reason": "normalized evidence is not valid UTF-8", "error": str(exc)}
    frontmatter, _, error = contract.split_record(text)
    if error is not None or not isinstance(frontmatter, dict):
        return {"reason": "normalized evidence has no readable frontmatter"}
    stamps = {key: frontmatter.get(key) for key in ("updated", "normalized_at")}
    if not all(isinstance(value, str) and value for value in stamps.values()):
        return {"reason": "normalized evidence lacks the timestamps its rendering is stamped with"}
    # Deep-copied because normalization is allowed to enrich the record it is handed, and
    # the caller's copy is the one every fingerprint in this verification is taken from.
    subject = copy.deepcopy(record)
    try:
        adapters = normalize_sources.normalization_config(config)["adapters"]
        eligible = normalize_sources.eligible_records(project_root, [subject], adapters)
        if not eligible:
            return {"reason": "this package's normalizer does not handle the reused source's kind"}
        item = eligible[0]
        # The extractor the record says produced it, so a run under an explicit
        # `--pdf-extractor` re-derives under the same one instead of under the default --
        # resolved through the allowlist, never forwarded as the raw stamped string.
        extractor: Any = None
        if item.method == "pdf":
            extractor_failure, extractor = stamped_pdf_extractor(frontmatter, normalize_sources)
            if extractor_failure is not None:
                return extractor_failure
        with rederivation_root(
            project_root,
            item,
            normalize_sources,
            [
                *declared_record_input_paths(record, normalize_sources),
                *adapter_workspace_command_paths(project_root, config, record),
            ],
        ) as derivation_root:
            source = normalize_sources.normalize_selected_record(
                derivation_root,
                config,
                item,
                extractor,
            )
        structured_view: dict[str, str] | None = None
        if source.structured is not None:
            expected_bytes = normalize_sources.render_structured_view(source.structured)
            sidecar = normalize_sources.expected_structured_path(
                normalized_root, normalize_sources.record_id(subject)
            )
            actual_bytes = bounded_regular_bytes(
                sidecar,
                max_bytes=MAX_VERIFICATION_ARTIFACT_BYTES,
                error_code="ORCHESTRATION_POSTCONDITION_FAILED",
                label="structured-view sidecar",
                missing_ok=True,
                containment_root=project_root,
            )
            if actual_bytes != expected_bytes:
                return {
                    "reason": "the structured-view sidecar is not what normalizing the raw evidence produces",
                    "path": relative_workspace_path(project_root, sidecar),
                }
            structured_view = {
                "path": normalize_sources.declared_structured_path(sidecar, project_root),
                "content_hash": normalize_sources.structured_view_content_hash(expected_bytes),
            }
        expected_frontmatter = normalize_sources.frontmatter_for(
            source,
            manifest_relative,
            normalized_path,
            stamps["updated"],
            manifest_records=manifest_records,
            project_root=project_root,
            normalized_at=stamps["normalized_at"],
            structured_view=structured_view,
        )
        # Read back rather than re-derived, but not taken on trust: these are edges in the
        # citation graph `query_index.py` builds, so they are admitted only as ids the
        # manifest this verification already fingerprints actually holds.
        stamped_references = frontmatter.get("references_source_ids")
        if stamped_references is not None:
            manifest_ids = {
                entry.get("id") for entry in manifest_records if isinstance(entry, dict)
            }
            if not isinstance(stamped_references, list) or not all(
                isinstance(value, str) and value in manifest_ids for value in stamped_references
            ):
                return {"reason": "normalized evidence cites source ids the evidence manifest does not hold"}
        expected_frontmatter["references_source_ids"] = stamped_references
        carry_version_stamps(expected_frontmatter, frontmatter)
        # Beside the version carry, and deliberately: that helper's whole subject is these
        # two producer blocks, and the name is the half it does *not* carry. Answered here,
        # before rendering, because after `render_markdown` the difference is only bytes and
        # the byte verdict blames hand-editing the body. `return` rather than `raise`: the
        # blanket clause below would fold a raise back into the generic re-derivation reason.
        identity_failure = normalizer_identity_failure(expected_frontmatter, frontmatter)
        if identity_failure is not None:
            return identity_failure
        expected_text = normalize_sources.render_markdown(source, expected_frontmatter)
    except OrchestrationControllerError:
        raise
    except RederivationSandboxError as exc:
        return {"reason": "the bounded workspace this re-derivation runs in could not be prepared", "error": str(exc)}
    # Split out of the blanket clause for the reason `RederivationSandboxError` above it is:
    # the adapter protocol raises this to say something precise -- most sharply, that the
    # program it ran reported an identity `research.yml` does not authorize -- and folding
    # that into "could not be re-derived" buries the one sentence the operator can act on
    # inside `error`. Read off the module that raises it, so the class is the same object
    # rather than a second copy of it loaded through another path.
    except getattr(normalize_sources, "AdapterError", ()) as exc:
        return {
            "reason": "the normalizer adapter this workspace authorizes did not produce a record to compare against",
            "error": str(exc),
        }
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - any re-derivation failure is the same verdict
        return {"reason": "normalized evidence could not be re-derived from the raw evidence", "error": str(exc)}
    if expected_text.encode("utf-8") != payload:
        return {"reason": "normalized evidence is not what normalizing the raw evidence produces"}
    return None


#: Re-derivation verdicts already reached during one ``submit``, keyed by the source and
#: by the exact bytes each verdict is a statement about. ``None`` -- the state outside a
#: submit, and the state every other caller sees -- disables memoisation entirely.
_DERIVATION_VERDICTS: dict[tuple[str, str, str], dict[str, Any] | None] | None = None

#: Inventory-derived raw attribution already computed during one ``submit``, keyed by the
#: raw-tree content fingerprint *and* the directory set under those same roots -- together,
#: the tree the attribution is a statement about. Same lifecycle and rationale as
#: ``_DERIVATION_VERDICTS``: ``submit_result`` verifies up to three times, and one
#: derivation pass re-hashes every raw file.
_RAW_ATTRIBUTION_MEMO: dict[str, dict[str, dict[str, Any]]] | None = None


@contextmanager
def derivation_verdict_memo() -> Iterator[None]:
    """Reach each re-derivation verdict once per submit rather than once per pass.

    ``submit_result`` verifies the same workspace three times -- to prepare the submission,
    to confirm the prepared phase still holds, and again to apply effects -- and the
    reconciliation loop runs on all three. Re-derivation is by far the most expensive check
    the controller has: an external adapter run, or two ``pdftotext`` passes, per reused
    source, bounded only by ``MAX_SCOPE_IDS`` and held throughout inside the driver session
    lock that peers acquire with ``wait_seconds=0``.

    The key is the source id plus the two digests the verdict is about, so a memoised
    answer is only ever returned for bytes that have not moved between passes. Scoped to
    one submit and discarded with it, because between submits the workspace is free to
    change.
    """
    global _DERIVATION_VERDICTS, _RAW_ATTRIBUTION_MEMO
    previous = _DERIVATION_VERDICTS
    previous_attribution = _RAW_ATTRIBUTION_MEMO
    _DERIVATION_VERDICTS = {}
    _RAW_ATTRIBUTION_MEMO = {}
    try:
        yield
    finally:
        _DERIVATION_VERDICTS = previous
        _RAW_ATTRIBUTION_MEMO = previous_attribution


def raw_tree_directory_digest(project_root: Path, config: dict[str, Any]) -> str:
    """Digest the directory set under the immutable raw roots, hashing no file bytes.

    Completes the raw-attribution memo key, which a raw-tree content fingerprint cannot
    complete on its own: ``raw_tree_snapshot`` records an entry per regular file and none
    for a directory, so that fingerprint cannot tell an unchanged tree from one that gained
    an empty directory -- and an empty directory is something ``source_inventory`` reads.
    An empty ``.git/`` is one of the markers that makes a directory a local repository.

    Directory names only: no ``stat`` of the files beneath them and no digest of their
    bytes, so this costs one walk of a tree the caller's own fingerprint has already
    walked. Tolerant by design -- anything unreadable or link-like under ``raw/`` is the
    raw-tree guards' business and they refuse it on their own terms. This returns a digest,
    never a verdict.

    Scoped to ``configured_raw_source_roots``, deliberately the same roots the fingerprint
    it completes is taken over, and read beside that fingerprint rather than under the
    acquisition barrier the derivation takes. Neither is the derivation's own reach:
    ``integrations.codebase_analysis.source_roots`` may name a root outside
    ``raw.source_roots``, and such a root is outside the raw-tree fingerprint just as it is
    outside this digest -- a gap in what the raw baselines cover at all, which composing
    this key neither widens nor closes. Between the two the key can only go stale in the
    direction of an extra derivation pass, never a reused answer.
    """
    directories: set[str] = set()
    for relative_root in configured_raw_source_roots(config):
        root = project_root / relative_root.as_posix()
        for current, _dirnames, _filenames in os.walk(root):
            try:
                walked = PurePosixPath(Path(current).relative_to(root).as_posix())
            except ValueError:  # pragma: no cover - os.walk yields only paths under root
                continue
            directories.add((relative_root / walked).as_posix())
    # NUL separates because it is the one byte a POSIX path component cannot contain, so
    # no directory set can be spelled as another set's joined form. A newline separator
    # would rest that on paths never containing one, which is a convention rather than a
    # rule -- and this digest exists precisely so the key determines the value.
    # ``surrogateescape`` because ``os.walk`` hands back undecodable bytes as surrogates
    # and a plain ``encode`` raises on them. An empty directory whose name is not valid
    # UTF-8 reaches here without the snapshot having seen it -- the snapshot joins file
    # paths only -- and a digest that raised there would be a verdict, which this is not.
    return hashlib.sha256("\0".join(sorted(directories)).encode("utf-8", "surrogateescape")).hexdigest()


def derived_raw_attribution(
    project_root: Path,
    config: dict[str, Any],
    *,
    memo_key: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Re-run inventory derivation over the delivered raw tree, writing nothing.

    Returns ``source_id -> {"raw_paths": [...], "files": {...}}`` where ``raw_paths`` is
    the list inventory derives for that record over the tree as delivered, and ``files``
    is the set of snapshot-level entries those paths denote: a regular-file entry denotes
    itself, a directory-shaped entry (arXiv/LaTeX bundle, local code repo) denotes every
    regular file beneath it, and every entry additionally denotes its
    ``<entry>.provenance.yml`` sidecar.

    This is the single predicate both raw-scope questions are answered from: which new
    raw files a completed acquisition may create (the union of ``files`` over the
    admitted record ids), and whether a new manifest record's declared ``raw_paths`` is
    what inventory itself would derive (list equality against ``raw_paths``).

    ``source_inventory.build_records`` writes neither the manifest nor the activity log,
    which is what makes it usable as a verification predicate -- but it is not literally
    read-only. Taking the acquisition barrier creates ``raw/.locks/`` and writes a holder
    file into it, so this call does touch the workspace. Every shipped ``raw.source_roots``
    lists the evidence subtrees (``raw/papers``, ``raw/web``, ...) and never bare ``raw``,
    so the barrier sits outside every snapshot root and no fingerprint sees it. Nothing
    enforces that: ``configured_raw_source_roots`` accepts bare ``raw``, and under such a
    config the holder file is inside a snapshot root, its ``pid`` and ``created_at`` move
    the raw-tree fingerprint, and the verification passes report the acquirer as having
    changed raw evidence it never touched. Closing that means rejecting the root or
    excluding ``raw/.locks`` from the snapshot, not moving this call off the barrier.
    ``memo_key``
    should be the current raw-tree content fingerprint, and is not the whole memo key.
    That fingerprint is a claim about regular files and nothing more, so on its own it does
    not say what it reads as -- "the tree the derivation saw is unchanged".
    ``raw_tree_snapshot`` records an entry per regular file and none for a directory, so
    creating an empty directory -- a ``.git/`` marker, an empty bundle folder -- leaves it
    byte-identical while changing what ``build_records`` derives from the same tree. What
    is memoised on is therefore that fingerprint composed with
    ``raw_tree_directory_digest``, so the directory set has to be unchanged too before an
    answer is reused. Composed here rather than at the call sites, which keep passing the
    one fingerprint they already hold.

    Widening ``raw_tree_snapshot`` to record directories would answer the same question and
    must not be done: those entries are also the raw-scope guards' universe, so a directory
    appearing among them would surface as an unexpected new raw path and refuse legitimate
    deliveries.
    """
    cache = _RAW_ATTRIBUTION_MEMO
    entry_key = (
        f"{memo_key}\0{raw_tree_directory_digest(project_root, config)}"
        if cache is not None and memo_key is not None
        else None
    )
    if cache is not None and entry_key is not None and entry_key in cache:
        return cache[entry_key]
    source_inventory = load_sibling_module("source_inventory")
    try:
        records, _warnings, _summary = source_inventory.build_records(project_root, config, {})
    except OrchestrationControllerError:
        raise
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - any derivation failure is the same verdict
        # Losing the acquisition barrier lock to a live writer is not a broken raw tree,
        # and telling the operator to repair one would be advice they cannot follow --
        # nothing is wrong with the evidence and the next attempt may simply win. Read
        # ``contended`` by attribute rather than catching the class: workspace scripts
        # load siblings by path, so several copies of ``_workspace_locks`` coexist and an
        # ``isinstance`` check does not survive across them (``_workspace_locks.py:97``).
        if getattr(exc, "contended", False):
            raise OrchestrationControllerError(
                "ORCHESTRATION_POSTCONDITION_FAILED",
                "delivered raw evidence could not be re-derived while another writer held the workspace",
                recoverable=True,
                remediation="Wait for the concurrent workspace writer to finish, then resubmit the same action id.",
                details={"error": str(exc)},
            ) from exc
        raise OrchestrationControllerError(
            "ORCHESTRATION_POSTCONDITION_FAILED",
            "delivered raw evidence could not be re-derived by source inventory rules",
            recoverable=True,
            remediation="Repair the raw tree so source_inventory.py --report succeeds, then resubmit.",
            details={"error": str(exc)},
        ) from exc
    attribution: dict[str, dict[str, Any]] = {}
    for record in records:
        source_id = record.get("id") if isinstance(record, dict) else None
        raw_paths = record.get("raw_paths") if isinstance(record, dict) else None
        if not isinstance(source_id, str) or not isinstance(raw_paths, list):
            continue
        files: set[str] = set()
        for raw_path in raw_paths:
            if not (
                isinstance(raw_path, str)
                and raw_path.startswith("raw/")
                and safe_snapshot_relative_path(raw_path)
            ):
                continue
            target = project_root / raw_path
            try:
                is_directory = target.is_dir() and not target.is_symlink()
            except OSError:
                is_directory = False
            if is_directory:
                # Inventory attributes the whole subtree to this one record (no member
                # enumeration exists in the record), so the subtree is the record's unit
                # of admission -- exactly what the snapshot's per-file entries will show.
                for member in sorted(target.rglob("*")):
                    try:
                        metadata = member.lstat()
                        # The raw-tree snapshot's own rule for what counts as a file, so
                        # this expansion admits exactly the entries that snapshot shows:
                        # it refuses a link-like entry outright and refuses any regular
                        # file whose link count is not one, while ``Path.is_file()``
                        # resolves symlinks and accepts hardlinks.
                        if path_is_link_like(member, metadata):
                            continue
                        if not stat.S_ISREG(metadata.st_mode):
                            continue
                        if int(getattr(metadata, "st_nlink", 1) or 1) != 1:
                            continue
                        files.add(relative_workspace_path(project_root, member))
                    except OSError:
                        continue
            else:
                files.add(raw_path)
            files.add(f"{raw_path}.provenance.yml")
        attribution[source_id] = {
            "raw_paths": [path for path in raw_paths if isinstance(path, str)],
            "files": files,
        }
    if cache is not None and entry_key is not None:
        cache[entry_key] = attribution
    return attribution


def raw_attribution_mismatches(
    attribution: dict[str, dict[str, Any]],
    new_records_by_id: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Each new record whose declared ``raw_paths`` is not what inventory derives.

    List (pinned-order) equality, deliberately: inventory's output is deterministic --
    the raw walk is sorted and the two multi-path merges append in that order -- and the
    only sanctioned route to a manifest record is running inventory, so a legitimate
    delivery reproduces the derived list byte for byte. Set equality would additionally
    admit only reorderings and duplicates, and no shipped tool produces either. An id
    inventory derives nothing for reports ``derived_raw_paths: null``: such a record is
    not inventory-derivable and may not be created by an acquisition.

    Each mismatch names three things, because "these two lists are not equal" does not say
    which path is the unaccounted one: both lists, and ``declared_not_derived`` -- the
    declared paths inventory accounts for none of, in declared order. An id inventory
    derives nothing for reports every declared path there.

    That third field supplements the equality test and never replaces it, and it is
    one-sided on purpose: it answers "what did this record claim that inventory does not
    account for", so it is empty whenever every declared path is accounted for, however the
    two lists disagree -- a reorder, a duplicate, or a declared list that simply omits a
    path inventory derives. Read an empty difference as "nothing surplus was declared", not
    as "the lists agree"; what the lists do is the equality test's answer, printed beside
    it. A ``raw_paths`` that is not a list reports ``null`` for both the declared list and
    the difference, there being no list to subtract from.
    """
    mismatches: dict[str, dict[str, Any]] = {}
    for source_id in sorted(new_records_by_id):
        record = new_records_by_id[source_id]
        declared = record.get("raw_paths") if isinstance(record, dict) else None
        entry = attribution.get(source_id)
        derived = entry["raw_paths"] if entry is not None else None
        if not isinstance(declared, list) or declared != derived:
            derived_present = (
                {path for path in derived if isinstance(path, str)} if isinstance(derived, list) else set()
            )
            mismatches[source_id] = {
                "declared_raw_paths": declared if isinstance(declared, list) else None,
                "derived_raw_paths": derived,
                "declared_not_derived": (
                    [path for path in declared if not (isinstance(path, str) and path in derived_present)]
                    if isinstance(declared, list)
                    else None
                ),
            }
    return mismatches


def attributed_raw_paths(
    attribution: dict[str, dict[str, Any]],
    record_ids: set[str],
) -> set[str]:
    """The snapshot entries inventory attributes to the given record ids."""
    allowed: set[str] = set()
    for source_id in record_ids:
        entry = attribution.get(source_id)
        if entry is not None:
            allowed |= entry["files"]
    return allowed


RAW_ATTRIBUTION_REMEDIATION = (
    "Run source_inventory.py --report so every new record's raw_paths is exactly what "
    "inventory derives from the delivered files; never hand-edit raw_paths."
)


def memoised_derivation_failure(
    project_root: Path,
    config: dict[str, Any],
    source_id: str,
    record: dict[str, Any],
    normalized_path: Path,
    record_fingerprint: str | None,
    normalized_fingerprint: str | None,
    manifest_records: list[dict[str, Any]],
    normalize_sources: ModuleType,
) -> dict[str, Any] | None:
    """``normalized_output_derivation_failure`` once per (source, record, output) per submit."""
    cache = _DERIVATION_VERDICTS
    key = (source_id, str(record_fingerprint), str(normalized_fingerprint))
    if cache is not None and key in cache:
        return copy.deepcopy(cache[key])
    verdict = normalized_output_derivation_failure(
        project_root, config, record, normalized_path, manifest_records, normalize_sources
    )
    if cache is not None:
        cache[key] = copy.deepcopy(verdict)
    return verdict


# The terms are one sentence for both arms; the recourse is not, for the same reason
# `REUSE_SCOPE_*` splits below and with the same shape -- terms once, then a pointer at the
# per-failure `repair`. The rewrite command this used to name for both arms is correct for
# exactly one of them, and `RECONCILIATION_ARM_REPAIRS` is where it now lives.
RECONCILIATION_TERMS = (
    "Reuse a pre-existing source only on the terms this order recorded for it: its manifest record "
    "byte-identical to what the order fingerprinted, and its normalized output either byte-identical to "
    "what the order fingerprinted or, where the order recorded none, the record normalize_sources.py "
    "produces from the unchanged raw evidence. How that is repaired depends on which of the two arms this "
    "source was held to and on what the check found, and reconciliation_failures[].repair names the one "
    "this source needs"
)

# One repair per state that has its own, because they do not share one. The rewrite clause
# was written for the arm where the order recorded no normalized output -- there it is
# exactly right, and named exactly: plain `--force` selects nothing, because selection
# defaults to the pending set, and `--all --force` rewrites every record in the workspace
# straight into a `changed_outside_scope` refusal, so `--source-id <id> --force` is the one
# form that repairs the record this refusal is about and leaves the rest of the manifest
# alone.
#
# Emitted for the other arm it was advice that can never succeed. A source the order
# fingerprinted normalized reconciles on those exact bytes, and `normalized_at` is
# second-resolution and restamped by every run: re-normalizing rewrites the one field that
# has to come back unchanged, so the operator who follows it is refused again, identically,
# for having followed it. What is actually recoverable there is the bytes themselves, and
# nothing else is -- which that arm's repair now says, instead of pointing at a fresh
# session as though one could be reached from here. It cannot be reached cleanly: the only
# outcome that ends this action without re-running these checks is `failed`, and
# `prepare_submission` takes that branch without calling `verify_action_postconditions` at
# all, so it accepts the fulfilment this guard is refusing rather than withdrawing it. The
# request stays `fulfilled` with its `source_id` in the store, no later order sees it
# again (`open_requests` selects on status), and evidence the controller refused to verify
# is in the workspace permanently. That is a worse end than the refusal, so the repair
# names its cost rather than its command.
#
# The third key is the same failure one level down, inside the arm the rewrite *does* serve:
# a re-derivation that could not be performed is refused before the record's bytes are ever
# compared, so rewriting them is the same dead end. `UNVERIFIABLE_DERIVATION_REASONS` below
# is which verdicts those are.
RECONCILIATION_ARM_REPAIRS = {
    "scoped_match": (
        "This order fingerprinted both this source's manifest record and its normalized output at "
        "issuance, so only those exact bytes reconcile, and re-normalizing cannot reproduce them: every "
        "run restamps normalized_at, which is part of what was fingerprinted. Restore the record and the "
        "normalized output as the order fingerprinted them; that is the only repair this refusal has. If "
        "the rewrite is what this workspace should keep, this order cannot be satisfied from this source "
        "at all, and it has no clean way to end either: ending the action with a failed outcome is "
        "accepted without any evidence or scope check running at all, leaves the fulfilment and its "
        "source id recorded as if verified, and leaves behind a request no later order can see, because "
        "routing scopes open requests only. Take that as abandoning the session, never as a way past "
        "this refusal."
    ),
    "authorized_unnormalized": (
        "This order recorded no normalized output for this source and authorizes the single record it "
        "owes, re-derived from the unchanged raw evidence. A hand-edited record is not stale, so plain "
        "normalize_sources.py skips it: rewrite that one record with normalize_sources.py --source-id "
        "<id> --force, which leaves every record outside this order's scope untouched. Its manifest "
        "record still has to be byte-identical to what the order fingerprinted."
    ),
    "authorized_unnormalized_unverifiable": (
        "The re-derivation this reuse rests on could not be performed at all, so rewriting the record "
        "cannot change the verdict; derivation_failure.reason says what stopped it. Where that names this "
        "host -- a normalizer adapter that would not run, or a bounded workspace that could not be "
        "prepared -- repair the host and resubmit the same action id, leaving the record as it is. Where "
        "it names what this order pinned -- a source normalized from artifacts or paths no baseline "
        "fingerprints, or a kind this package does not normalize -- no reuse of this source can be "
        "verified under this order at all. Where it says only that the re-derivation could not be "
        "performed, derivation_failure.error carries what was raised: normalizing this source is broken "
        "here for a reason neither the record nor this order can name, and rewriting the record runs the "
        "same normalizer into the same failure."
    ),
}

# The arm-(b) verdicts the rewrite above cannot clear, split out for the reason the arms
# themselves are: advice that reads as a repair and returns the identical refusal is the
# defect. Each of these is reached before the record's own bytes are ever compared -- the
# check could not run, or the reuse is structurally out of this order's reach -- so
# `normalize_sources.py --source-id <id> --force` either fails in the same place the
# verification did or rewrites a record whose content was never the problem.
#
# The blanket verdict is here too, and was the one omission that reproduced the very defect
# this set exists to stop. `normalized_output_derivation_failure` ends in
# `except (Exception, SystemExit)`, so any re-derivation that *crashes* is reported as
# "normalized evidence could not be re-derived from the raw evidence" -- a check that could
# not run, not a record that did not match. Left out of this set it fell through to the
# rewrite, whose `normalize_sources.py --source-id <id> --force` re-enters the same
# normalizer that just raised. The named crash reasons above were split out of this same
# clause precisely so their operators would not be sent there; the residue deserves the same
# answer, and the repair's last sentence is written for it.
#
# Deliberately not here: "normalized evidence names a PDF extractor this package does not
# implement" and "...is unavailable on this host". Those *are* record-side. The rewrite
# re-derives under the extractor `research.yml` configures and restamps the record with it,
# which is exactly the repair, and adding them would send a fixable state to the "repair the
# host" text instead.
UNVERIFIABLE_DERIVATION_REASONS = frozenset(
    {
        "the reused source normalizes from artifacts this order does not fingerprint",
        "the reused source normalizes from workspace paths no raw-evidence baseline pins",
        "the workspace source paths this re-derivation needs are unusable",
        "this package's normalizer does not handle the reused source's kind",
        "the bounded workspace this re-derivation runs in could not be prepared",
        "the normalizer adapter this workspace authorizes did not produce a record to compare against",
        "normalized evidence could not be re-derived from the raw evidence",
    }
)

# Every refusal that carries this tail is computed over the acquirer's *fulfilled* list, so
# the request it speaks about is already fulfilled by the very source being refused. That is
# what made the escapes these remediations used to name unfollowable rather than merely
# clumsy, and both were walked before being removed: `source_requests.py
# record-attempt-failure` refuses a fulfilled request outright, and a second delivery "under
# its own raw path" inventories cleanly and is then refused by `fulfill`, which will not
# relink a fulfilled request to a different source. Acquiring through another candidate ends
# at the same relink refusal.
#
# So none of them is named. What is named is the fact underneath all three -- a fulfilled
# request has no second route -- and, where the per-source repair cannot be performed, that
# this order has none either. "There is no route from here" is worse news than a command and
# better advice than one that is refused for having been followed.
NO_SECOND_ROUTE_FOR_A_FULFILLED_REQUEST = (
    "There is no second route out of this refusal: this action already fulfilled the request from the "
    "source being refused, and a fulfilled request accepts neither a recorded attempt failure nor a "
    "relink to a later delivery. Only the per-source repair changes this verdict, and where the source "
    "has none, this order has no route from here"
)

RECONCILIATION_REMEDIATION = (
    f"{RECONCILIATION_TERMS}. {NO_SECOND_ROUTE_FOR_A_FULFILLED_REQUEST}."
)

# Selection is the whole of this advice, which is why it is one constant rather than the
# same sentence typed into both arms. `--all` considers every eligible record in the
# workspace, and an acquisition order authorizes normalized output for the sources it
# scopes and nothing else: the extras are refused as `unexpected_new_normalized`, and any
# unrelated record `is_stale` happens to consider stale is rewritten and refused again by
# a scope guard whose `mutable_ids` is empty. Following it lands on a refusal for having
# followed it. `--source-id` normalizes exactly the sources this order named.
MISSING_NORMALIZED_REMEDIATION = (
    "Run normalize_sources.py --source-id <id> once per source named here, so every fulfilled source has a "
    "normalized record and no record outside this order's scope is rewritten."
)

PROVIDER_RECONCILIATION_REMEDIATION = (
    f"{RECONCILIATION_TERMS}. {NO_SECOND_ROUTE_FOR_A_FULFILLED_REQUEST}. Acquiring the evidence again "
    "through another selected candidate produces a source this request cannot be relinked to either."
)

# Both arms name the same terms, and differ only in which dead escape their own acquirer
# would otherwise reach for: an attempt failure recorded against this action for the
# delegated arm, another selected candidate for the provider arm. Neither is a route,
# because this refusal is computed over `fulfilled` -- see
# `NO_SECOND_ROUTE_FOR_A_FULFILLED_REQUEST` above -- so each arm names the one its reader
# would try and says why it is refused, rather than advising it.
REUSE_SCOPE_TERMS = (
    "Reuse only a source this order named at issuance: one whose .provenance.yml already named this request, "
    "unchanged since, either carrying a normalized record then or normalized inside this order. The repair "
    "differs per source and details[].repair names the one this source needs"
)

REUSE_SCOPE_REMEDIATION = (
    f"{REUSE_SCOPE_TERMS}. {NO_SECOND_ROUTE_FOR_A_FULFILLED_REQUEST}."
)

PROVIDER_REUSE_SCOPE_REMEDIATION = (
    f"{REUSE_SCOPE_TERMS}. {NO_SECOND_ROUTE_FOR_A_FULFILLED_REQUEST}. Acquiring the evidence again "
    "through another selected candidate produces a source this request cannot be relinked to either."
)

# One repair per cause, because the causes do not share one. The sentence that used to
# serve all three told the acquirer its source satisfied every clause of the reuse
# terms — true for two of them, and precisely wrong for the third, where the source does
# satisfy them and the order simply predates the authorization it would have needed.
#
# None of the three ends with a command, and that is the honest shape rather than an
# oversight. The failure set is built from sources that are in `manifest_records_before`
# and in neither reuse baseline, and those baselines were fixed at issuance, so nothing
# done during the order can move a source into one. The request is fulfilled by the source
# besides, so the second delivery two of these used to advise cannot be linked to it. What
# each repair owes its reader is therefore the truth about its own cause plus a plain
# statement that this order has no route -- the rewritten-record cause included, whose
# restore the manifest-scope guard downstream requires and which still leaves this refusal
# standing, under whichever of the other two causes was true all along.
REUSE_SCOPE_CAUSE_REPAIRS = {
    "provenance_names_no_scoped_request": (
        "This source's .provenance.yml names another request or none. Correlation is acquirer-written, so "
        "restamping it authorizes nothing and never makes it reusable, and this order has no repair that "
        "does: the request it was used for is already fulfilled by it, so a fresh delivery under its own "
        "raw path with its own sidecar cannot be linked to that request either. Such a delivery is what a "
        "later order needs; it is not a way out of this one."
    ),
    "manifest_record_changed_after_issuance": (
        "This source's manifest record was rewritten after the order was issued, so what it says now is no "
        "evidence of what it said then, and the rewrite has to be undone whatever else follows: restore the "
        "record exactly as the order recorded it. Restoring it does not make this source reusable, though. "
        "Every source this refusal reports was outside both of the order's reuse baselines before the "
        "rewrite as well, and those baselines were fixed at issuance, so what restoring the record changes "
        "is only which of the other two causes this refusal reports — and neither of those has a repair "
        "inside this order either."
    ),
    "no_reuse_authorization_at_issuance": (
        "This source is correlated and unchanged; the order simply recorded no reuse authorization for it, "
        "and nothing done inside the order can add one. Leave it alone and let a later order — issued while "
        "it is correlated and un-normalized — reuse it. This order has no repair for it: the request it was "
        "used for is already fulfilled by it, so a fresh delivery under its own raw path cannot be linked "
        "to that request either."
    ),
}


def preexisting_reuse_scope_failures(
    fulfilled: list[dict[str, Any]],
    by_source_id: dict[str, Any],
    scoped_requests: set[str],
    matching_source_records_before: dict[str, Any],
    reusable_source_ids_before: set[str],
    manifest_records_before: dict[str, Any],
    current_manifest_fingerprints: dict[str, str],
    scoped_candidates: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Pre-existing sources a fulfilment reuses that its order authorized no reuse of.

    Reuse of evidence the workspace already held is admitted only on terms fixed at
    issuance, by the controller, from state the acquirer had not touched:
    ``matching_source_records_before`` for a source that already carried a normalized
    record, ``reusable_source_ids_before`` for one this order's own scoped request named
    and nothing had normalized yet. A source outside both can never reconcile against
    either. Saying so here is the whole point — without this refusal the checks below speak
    first, and the first of them tells the acquirer to stamp the sidecar and re-inventory,
    which is then refused four more times over (manifest, reconciliation, normalized and raw
    scope in turn) because ``raw/`` is immutable and re-inventorying rewrites a record this
    order may not touch. Four unrelated-sounding refusals for obeying the first one's advice.

    Three different things put a source outside both, and the message must not guess which,
    because their repairs differ: a sidecar naming no scoped request — the case reuse
    deliberately does **not** cover, because correlation is acquirer-written and a predicate
    over it authorizes nothing; a correctly named sidecar on an order issued before
    un-normalized reuse existed; and a record rewritten since issuance, which makes what it
    says now no evidence of what it said then. ``details`` carries the cause per source.

    Shared by both postcondition arms, so the provider arm reports reuse in the same words
    with the same remediation instead of folding it into a generic correlation failure.
    """
    failures: list[dict[str, Any]] = []
    for source_id in sorted(
        {
            str(request.get("source_id"))
            for request in fulfilled
            if str(request.get("source_id")) in manifest_records_before
            and str(request.get("source_id")) not in matching_source_records_before
            and str(request.get("source_id")) not in reusable_source_ids_before
        }
    ):
        record = by_source_id.get(source_id)
        provenance = record.get("provenance") if isinstance(record, dict) else None
        provenance_request_id = provenance.get("request_id") if isinstance(provenance, dict) else None
        record_unchanged = current_manifest_fingerprints.get(source_id) == manifest_records_before[source_id]
        if not record_unchanged:
            cause = "manifest_record_changed_after_issuance"
        # The same predicate the baselines were built with, candidate scope included. The
        # provider arm correlates on request *and* candidate, so asking about the request
        # alone would report a source disqualified by its candidate id as merely
        # unauthorized -- and that cause's repair is "let a later order reuse it", which
        # for this source never comes true.
        elif correlated_source_scope_match(record, scoped_requests, scoped_candidates) is not None:
            cause = "no_reuse_authorization_at_issuance"
        else:
            cause = "provenance_names_no_scoped_request"
        failures.append(
            {
                "source_id": source_id,
                "cause": cause,
                "repair": REUSE_SCOPE_CAUSE_REPAIRS[cause],
                "provenance_request_id": provenance_request_id,
                "record_unchanged": record_unchanged,
            }
        )
    return failures


def reused_source_reconciliation_failure(
    project_root: Path,
    config: dict[str, Any],
    source_id: str,
    *,
    record: Any,
    manifest_records: list[dict[str, Any]],
    normalized_root: Path,
    matching_source_records_before: dict[str, Any],
    reusable_source_ids_before: set[str],
    manifest_records_before: dict[str, Any],
    current_record_fingerprint: str | None,
    normalized_files_before: dict[str, Any],
    normalize_sources: ModuleType,
) -> dict[str, Any] | None:
    """Hold one pre-existing fulfilled source to the terms its order fixed at issuance.

    One function so the delegated and provider arms cannot drift into two answers about the
    same question. Which arm applies is read off the order's own baselines, never inferred
    from what the action left behind -- otherwise deleting a file would be a way to pick the
    weaker arm.

    Arm (a), a source that already carried a normalized record: both digests byte-identical,
    exactly as before reuse of any other kind existed. Arm (b), a source this order
    correlated to a scoped request that nothing had normalized yet: the manifest record
    byte-identical, and the normalized output both newly written -- there was nothing there
    to overwrite -- and re-derivable from the raw evidence by this package's own normalizer.
    Anything else is a failure with the arm it was held to and the obligation it missed.
    """
    normalized_path = (
        normalize_sources.normalized_output_path_for_record(record, normalized_root)
        if isinstance(record, dict)
        else None
    )
    normalized_fingerprint = (
        file_digest(
            normalized_path,
            max_bytes=MAX_VERIFICATION_ARTIFACT_BYTES,
            containment_root=project_root,
        )
        if isinstance(normalized_path, Path) and normalized_path.is_file()
        else None
    )
    expected = matching_source_records_before.get(source_id)
    derivation: dict[str, Any] | None = None
    derivation_checked = False
    if isinstance(expected, dict):
        record_unchanged = current_record_fingerprint == expected.get("record_fingerprint")
        normalized_as_authorized = normalized_fingerprint == expected.get("normalized_fingerprint")
    elif source_id in reusable_source_ids_before:
        record_unchanged = current_record_fingerprint == manifest_records_before.get(source_id)
        normalized_relative = (
            relative_workspace_path(project_root, normalized_path)
            if isinstance(normalized_path, Path)
            else None
        )
        normalized_as_authorized = (
            normalized_fingerprint is not None
            and normalized_relative is not None
            and normalized_relative not in normalized_files_before
        )
        if record_unchanged and normalized_as_authorized and isinstance(record, dict):
            derivation = memoised_derivation_failure(
                project_root,
                config,
                source_id,
                record,
                normalized_path,
                current_record_fingerprint,
                normalized_fingerprint,
                manifest_records,
                normalize_sources,
            )
            derivation_checked = True
            normalized_as_authorized = derivation is None
    else:  # pragma: no cover - preexisting_reuse_scope_failures refuses these first
        raise OrchestrationControllerError(
            "ORCHESTRATION_POSTCONDITION_FAILED",
            f"reused source is in neither reuse baseline this order recorded: {source_id}",
            recoverable=True,
        )
    if record_unchanged and normalized_as_authorized:
        return None
    # Only the checks that ran are reported. A reuse whose record already drifted never
    # reaches the derivation, and saying `derivation_failure: None` there would read as
    # "the body was examined and matched" -- sending the operator to repair the record and
    # fail again on a body nothing looked at.
    #
    # The arm is also the repair, and is named once so the booleans and the advice cannot
    # come apart. Both arms used to be told to re-normalize the record, which is the recourse
    # for exactly one of them, and not for every failure even of that one:
    # `RECONCILIATION_ARM_REPAIRS` carries who can follow the rewrite and what the rest do
    # instead.
    scoped_match = isinstance(expected, dict)
    if scoped_match:
        arm = "scoped_match"
    elif isinstance(derivation, dict) and derivation.get("reason") in UNVERIFIABLE_DERIVATION_REASONS:
        arm = "authorized_unnormalized_unverifiable"
    else:
        arm = "authorized_unnormalized"
    failure: dict[str, Any] = {
        "source_id": source_id,
        "was_scoped_match": scoped_match,
        "was_authorized_unnormalized": not scoped_match,
        "repair": RECONCILIATION_ARM_REPAIRS[arm],
        "record_unchanged": record_unchanged,
        "normalized_unchanged": normalized_as_authorized,
        "derivation_checked": derivation_checked,
    }
    if derivation_checked:
        failure["derivation_failure"] = derivation
    return failure


def normalized_output_scope_failures(
    allowed: set[str],
    required: set[str],
    files_before: dict[str, str],
    files_now: dict[str, str],
) -> tuple[list[str], list[str]]:
    """New paths no expected source authorizes, and expected records that never appeared.

    The missing-record half preserves an obligation the exact-equality check used to carry
    and is deliberately kept, but it is **defence in depth, not the primary enforcement**:
    the earlier `missing_normalized` guard refuses a fulfilled request with no normalized
    record long before the scope comparison runs. What is left to this half is the narrower
    anomaly of a source counted as newly fulfilled whose record is not newly created, which
    the suite cannot reach without mocking — recorded here rather than left to read as
    tested coverage.
    """
    actual_new = set(files_now) - set(files_before)
    return sorted(actual_new - allowed), sorted(required - actual_new)


def normalized_file_fingerprint_snapshot(
    project_root: Path,
    config: dict[str, Any],
) -> dict[str, str]:
    normalize_sources = load_sibling_module("normalize_sources")
    _, normalized_relative = normalize_sources.source_paths(config)
    return file_tree_fingerprint_snapshot(
        project_root,
        project_root / normalized_relative,
        label="normalized evidence",
    )


def valid_file_fingerprint_snapshot(value: Any, *, prefix: str | None = None) -> bool:
    return (
        isinstance(value, dict)
        and len(value) <= MAX_SCOPE_GUARD_ENTRIES
        and all(
            isinstance(path, str)
            and len(path) <= MAX_ARTIFACT_PATH_LENGTH
            and safe_snapshot_relative_path(path)
            and (prefix is None or path.startswith(prefix))
            and valid_sha256_fingerprint(fingerprint)
            for path, fingerprint in value.items()
        )
    )


def question_file_fingerprint_snapshot(
    project_root: Path,
    config: dict[str, Any],
) -> dict[str, str]:
    """Capture every question file so research cannot mutate work outside its scope."""
    question_status = load_sibling_module("question_status")
    questions_dir = question_status.questions_directory(project_root, config)
    snapshot: dict[str, str] = {}
    total_bytes = 0
    if not questions_dir.exists():
        return snapshot
    try:
        paths = sorted(questions_dir.glob("*.md"), key=lambda item: item.name)
    except OSError as exc:
        raise OrchestrationControllerError(
            "ORCHESTRATION_WORKSPACE_UNSAFE",
            f"could not enumerate question files: {exc}",
            recoverable=True,
        ) from exc
    for path in paths:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise OrchestrationControllerError(
                "ORCHESTRATION_WORKSPACE_UNSAFE",
                f"could not inspect question file {path.name}: {exc}",
                recoverable=True,
            ) from exc
        if (
            path_is_link_like(path, metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or int(getattr(metadata, "st_nlink", 1) or 1) != 1
        ):
            raise OrchestrationControllerError(
                "ORCHESTRATION_WORKSPACE_UNSAFE",
                f"question integrity guard requires a singly linked regular file: {path.name}",
                recoverable=True,
            )
        if len(snapshot) >= MAX_SCOPE_GUARD_ENTRIES:
            raise OrchestrationControllerError(
                "ORCHESTRATION_SCOPE_EXCEEDED",
                "question store exceeds the bounded integrity-guard entry limit",
                recoverable=False,
            )
        declared_size = int(metadata.st_size)
        total_bytes += declared_size
        if total_bytes > MAX_SCOPE_GUARD_BYTES:
            raise OrchestrationControllerError(
                "ORCHESTRATION_SCOPE_EXCEEDED",
                "question store exceeds the bounded integrity-guard byte limit",
                recoverable=False,
            )
        fingerprint = file_digest(
            path,
            max_bytes=declared_size,
            containment_root=project_root,
        )
        if fingerprint is None:
            raise OrchestrationControllerError(
                "ORCHESTRATION_WORKSPACE_UNSAFE",
                f"question file changed while it was fingerprinted: {path.name}",
                recoverable=True,
            )
        snapshot[path.name] = fingerprint
    return snapshot


def valid_question_file_fingerprint_snapshot(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and len(value) <= MAX_SCOPE_GUARD_ENTRIES
        and all(
            isinstance(filename, str)
            and len(filename) <= MAX_ARTIFACT_PATH_LENGTH
            and PurePosixPath(filename).name == filename
            and filename.endswith(".md")
            and valid_sha256_fingerprint(fingerprint)
            for filename, fingerprint in value.items()
        )
    )


def candidate_request_id(candidate: dict[str, Any]) -> str | None:
    for field in ("source_request_id", "selected_for_request_id", "selected_request_id", "request_id"):
        value = candidate.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def candidate_state(candidate: dict[str, Any]) -> str:
    value = candidate.get("lifecycle_state") or candidate.get("status") or "new"
    return value.strip().lower() if isinstance(value, str) and value.strip() else "new"


def scoped_question_snapshot(
    project_root: Path,
    config: dict[str, Any],
    slugs: list[str],
) -> dict[str, dict[str, Any]]:
    """Capture the bounded lifecycle fields needed to prove research progress."""
    question_status = load_sibling_module("question_status")
    questions_dir = question_status.questions_directory(project_root, config)
    records = {
        str(record.get("slug")): record
        for record in question_status.collect_questions(questions_dir)
        if isinstance(record, dict) and isinstance(record.get("slug"), str)
    }
    snapshot: dict[str, dict[str, Any]] = {}
    for slug in slugs:
        record = records.get(slug)
        if record is None:
            continue
        snapshot[slug] = {
            "status": str(record.get("status") or "unknown"),
            "blocking_request_ids": sorted(
                value
                for value in record.get("blocking_request_ids", [])
                if isinstance(value, str) and value
            ),
            "answer_page": str(record.get("answer_page") or ""),
        }
    return snapshot


def scoped_question_evidence_snapshot(
    project_root: Path,
    config: dict[str, Any],
    slugs: list[str],
) -> dict[str, dict[str, Any]]:
    """Capture lifecycle, blocking links, and source links for bounded question scope."""
    question_status = load_sibling_module("question_status")
    questions_dir = question_status.questions_directory(project_root, config)
    snapshot: dict[str, dict[str, Any]] = {}
    for slug in slugs:
        path = questions_dir / f"{slug}.md"
        if not path.is_file():
            continue
        frontmatter = question_status.load_frontmatter(path)
        if not isinstance(frontmatter, dict) or frontmatter.get("type") != "question":
            continue
        snapshot[slug] = {
            "status": str(frontmatter.get("status") or "unknown"),
            "blocking_request_ids": sorted(
                {
                    value
                    for value in frontmatter.get("blocking_request_ids", [])
                    if isinstance(value, str) and value
                }
            )
            if isinstance(frontmatter.get("blocking_request_ids"), list)
            else [],
            "source_ids": sorted(
                {
                    value
                    for value in frontmatter.get("source_ids", [])
                    if isinstance(value, str) and value
                }
            )
            if isinstance(frontmatter.get("source_ids"), list)
            else [],
        }
    return snapshot


def linked_blocked_questions_snapshot(
    project_root: Path,
    config: dict[str, Any],
    slugs: list[str],
    request_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Capture only blocked scoped questions linked to the scoped source requests."""
    request_scope = set(request_ids)
    linked: dict[str, dict[str, Any]] = {}
    for slug, snapshot in scoped_question_evidence_snapshot(project_root, config, slugs).items():
        blocking_request_ids = [
            request_id
            for request_id in snapshot.get("blocking_request_ids", [])
            if request_id in request_scope
        ]
        if snapshot.get("status") != "blocked" or not blocking_request_ids:
            continue
        linked[slug] = {
            "status": "blocked",
            "blocking_request_ids": blocking_request_ids,
            "source_ids_before": list(snapshot.get("source_ids", [])),
        }
    if not valid_blocked_question_baseline(linked):
        raise OrchestrationControllerError(
            "ORCHESTRATION_SCOPE_EXCEEDED",
            "linked blocked-question baseline exceeds the bounded orchestration contract",
            recoverable=False,
            remediation="Split or repair the oversized question/request linkage before starting acquisition.",
        )
    return linked


def correlated_source_scope_match(
    record: Any,
    request_scope: set[str],
    candidate_scope: set[str] | None,
) -> str | None:
    """The manifest source id a record correlates to this acquisition scope, or ``None``.

    The one correlation predicate both issuance-time reuse baselines apply, so "already
    correlated to a scoped request" cannot come to mean one thing in the map that
    fingerprints normalized reuse and another in the list that authorizes un-normalized
    reuse. Whether anything normalized the source yet is deliberately not part of it: that
    is what the two baselines differ on, and the only thing they differ on.

    ``candidate_scope`` of ``None`` correlates on the source request alone, which is what
    delegated acquisition has.
    """
    if not isinstance(record, dict):
        return None
    provenance = record.get("provenance")
    source_id = record.get("id")
    if (
        not isinstance(provenance, dict)
        or provenance.get("request_id") not in request_scope
        or (candidate_scope is not None and provenance.get("candidate_id") not in candidate_scope)
        or not isinstance(source_id, str)
        or not source_id
    ):
        return None
    return source_id


def acquisition_reuse_baselines(
    project_root: Path,
    config: dict[str, Any],
    request_ids: list[str],
    candidate_ids: list[str] | None,
) -> tuple[dict[str, dict[str, str]], list[str], dict[str, str]]:
    """Both reuse baselines and the manifest fingerprint snapshot, from one read.

    They are complements: both ask ``correlated_source_scope_match`` the same question —
    does this delivery's own sidecar name a request this order scopes? — and they part on
    whether a normalized record already exists. The map fingerprints the source that has
    one, because its reconciliation terms are byte-identity against *two* digests. The list
    carries ids for the source that does not, because there is nothing yet to be identical
    to; the record's own digest is in the fingerprint snapshot this same pass returns, and
    storing it twice would create two answers that can disagree.

    Computing them separately made the complement a race rather than a property. Between
    two passes over two loads of the manifest, a normalized file appearing puts a source in
    neither — surfacing later as a misleading ``no_reuse_authorization_at_issuance`` — and
    one disappearing puts it in *both*, which the disjointness clause of
    ``valid_unnormalized_reuse_baseline`` rejects, making every submission of the order
    fail ``require_acquisition_evidence_baselines`` with ``recoverable=False``: an order
    the controller issued dead on arrival. The manifest fingerprint snapshot is returned
    from the same read for the same reason: every baseline check demands both reuse
    baselines be subsets of it, so a record removed between two reads produced that same
    un-completable order from the other direction. One read, three answers that cannot
    disagree, instead of three reads and three chances to.

    ``candidate_ids`` of ``None`` correlates on the source request alone. Delegated
    acquisition has no candidate store — the acquirer chooses how to obtain the evidence —
    so requiring a candidate id would make these baselines permanently empty and silently
    remove the reuse path the provider mode has: an unchanged source delivered before this
    order was issued can satisfy a scoped request without being fetched again.

    A source stamped for another request, or stamped for none, is in neither. Correlation
    is what the acquirer writes into the sidecar, so a predicate that admitted a source on
    any other basis would be a predicate over acquirer-controlled metadata, which authorizes
    nothing. Reuse of such a source needs an authorization from a party this package does
    not have; until it does, the answer stays a second delivery under its own raw path.

    Un-normalized reuse additionally asks ``reusable_unnormalized_record_kind`` and
    ``unpinned_record_input_paths`` — both of the questions the derivation check answers at
    verification before it re-derives anything. Admitting a source either of them refuses
    would issue an order that cannot be completed: ``normalized_output_scope`` would
    *require* that source's normalized output while the derivation check refused the same
    output unconditionally, and no repair the acquirer can make changes either answer.

    Computed once, at issuance, from state the acquirer has not touched, and persisted in
    the protected baseline sidecar. Nothing written during the order can add an entry.
    """
    request_scope = set(request_ids)
    candidate_scope = set(candidate_ids) if candidate_ids is not None else None
    normalize_sources = load_sibling_module("normalize_sources")
    manifest_relative, normalized_relative = normalize_sources.source_paths(config)
    records = normalize_sources.load_manifest(project_root / manifest_relative)
    normalized_root = project_root / normalized_relative
    matching: dict[str, dict[str, str]] = {}
    reusable: set[str] = set()
    for record in records:
        source_id = correlated_source_scope_match(record, request_scope, candidate_scope)
        normalized_path = (
            normalize_sources.normalized_output_path_for_record(record, normalized_root)
            if isinstance(record, dict)
            else None
        )
        if (
            source_id is None
            or source_id in matching
            or source_id in reusable
            or not isinstance(normalized_path, Path)
        ):
            continue
        if not normalized_path.is_file():
            if reusable_unnormalized_record_kind(record, normalize_sources) and not (
                unpinned_record_input_paths(project_root, config, record, normalize_sources)
            ):
                reusable.add(source_id)
            if len(reusable) > MAX_SCOPE_IDS:
                raise OrchestrationControllerError(
                    "ORCHESTRATION_SCOPE_EXCEEDED",
                    "un-normalized reuse baseline exceeds the bounded orchestration contract",
                    recoverable=False,
                    remediation="Normalize or archive correlated evidence before starting acquisition.",
                )
            continue
        record_fingerprint, _ = canonical_json_fingerprint(record, label="evidence manifest")
        normalized_fingerprint = file_digest(
            normalized_path,
            max_bytes=MAX_VERIFICATION_ARTIFACT_BYTES,
            containment_root=project_root,
        )
        if normalized_fingerprint is None:
            raise OrchestrationControllerError(
                "ORCHESTRATION_WORKSPACE_UNSAFE",
                f"matching normalized evidence is unreadable or oversized: {source_id}",
                recoverable=True,
                remediation="Repair the normalized evidence record before starting acquisition.",
            )
        matching[source_id] = {
            "record_fingerprint": record_fingerprint,
            "normalized_fingerprint": normalized_fingerprint,
        }
        if len(matching) > MAX_SCOPE_IDS:
            raise OrchestrationControllerError(
                "ORCHESTRATION_SCOPE_EXCEEDED",
                "matching normalized-source baseline exceeds the bounded orchestration contract",
                recoverable=False,
                remediation="Split or repair duplicate evidence provenance before starting acquisition.",
            )
    if not valid_scope_id_list(sorted(matching)):
        raise OrchestrationControllerError(
            "ORCHESTRATION_SCOPE_INVALID",
            "matching normalized-source baseline contains an invalid source id",
            recoverable=False,
            remediation="Repair invalid manifest source ids before starting acquisition.",
        )
    if not valid_scope_id_list(sorted(reusable)):
        raise OrchestrationControllerError(
            "ORCHESTRATION_SCOPE_INVALID",
            "un-normalized reuse baseline contains an invalid source id",
            recoverable=False,
            remediation="Repair invalid manifest source ids before starting acquisition.",
        )
    return (
        dict(sorted(matching.items())),
        sorted(reusable),
        record_fingerprint_snapshot(records, id_field="id", label="evidence manifest"),
    )


def valid_matching_source_record_snapshot(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and len(value) <= MAX_SCOPE_IDS
        and valid_scope_id_list(sorted(value))
        and all(
            isinstance(snapshot, dict)
            and set(snapshot) == {"record_fingerprint", "normalized_fingerprint"}
            and valid_sha256_fingerprint(snapshot.get("record_fingerprint"))
            and valid_sha256_fingerprint(snapshot.get("normalized_fingerprint"))
            for snapshot in value.values()
        )
    )


def valid_unnormalized_reuse_baseline(
    reusable_source_ids_before: Any,
    matching_source_ids_before: Any,
    manifest_records_before: Any,
) -> bool:
    """Whether the un-normalized reuse baseline is bounded, known, and unambiguous.

    Three properties, checked together because a verifier that consumes this list needs all
    three before it may act on any of it, and because one predicate is one thing to wire
    into each arm rather than three things to wire into some of them.

    *Bounded and well formed*, like every scope list in the contract. *Named by the manifest
    baseline*: an id outside it would be checked by nothing at all — reconciliation only
    walks sources the manifest snapshot holds — so an allowlist that reaches past it can
    only have been tampered with. *Disjoint from the scoped-match map*: the two carry
    opposite reuse terms, and a source in both would let whichever arm answered first decide
    which terms applied.
    """
    if (
        not isinstance(reusable_source_ids_before, list)
        or len(reusable_source_ids_before) > MAX_SCOPE_IDS
        or not valid_scope_id_list(reusable_source_ids_before)
        or not isinstance(matching_source_ids_before, list)
        or not isinstance(manifest_records_before, dict)
    ):
        return False
    reusable = set(reusable_source_ids_before)
    return reusable <= set(manifest_records_before) and reusable.isdisjoint(matching_source_ids_before)


def candidate_provider(candidate: dict[str, Any]) -> str | None:
    value = candidate.get("provider")
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    paper = candidate.get("paper") if isinstance(candidate.get("paper"), dict) else {}
    value = paper.get("provider")
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def acquisition_route(candidate: dict[str, Any], enabled: set[str]) -> str | None:
    """Return the executable provider chosen by the canonical acquisition planner."""
    if not enabled:
        return None
    source_requests = load_sibling_module("source_requests")
    route = source_requests.candidate_acquisition_route(
        candidate,
        {"enabled": True, "providers": sorted(enabled)},
        candidate_request_id(candidate) or "route-check",
    )
    provider = route.get("provider") if isinstance(route, dict) else None
    if (
        route.get("provider_backed") is not True
        or route.get("allowed_by_config") is not True
        or not isinstance(provider, str)
        or provider not in enabled
    ):
        return None
    return provider


def composable_discovery_providers(policy: dict[str, Any]) -> list[str]:
    discovery = policy.get("discovery") if isinstance(policy.get("discovery"), dict) else {}
    acquisition = policy.get("acquisition") if isinstance(policy.get("acquisition"), dict) else {}
    if discovery.get("enabled") is not True or acquisition.get("enabled") is not True:
        return []
    acquisition_ids = {
        value for value in acquisition.get("providers", []) if isinstance(value, str) and value
    }
    composable: list[str] = []
    for provider in discovery.get("providers", []):
        if not isinstance(provider, str):
            continue
        if provider in {"arxiv", "openalex"} and acquisition_ids & {"arxiv", "openalex"}:
            composable.append(provider)
        elif provider == "github" and "github" in acquisition_ids:
            composable.append(provider)
        elif provider == "search" and acquisition_ids:
            # Generic search can propose provider-neutral academic, GitHub, or
            # web candidates. Candidate-level postconditions decide whether a
            # returned record actually composes with the enabled acquisition
            # adapters.
            composable.append(provider)
        elif (provider == "standards" or provider.startswith("standards:")) and "web" in acquisition_ids:
            composable.append(provider)
    return sorted(set(composable))


def request_candidates(candidates: list[dict[str, Any]], request_id: str) -> list[dict[str, Any]]:
    return [candidate for candidate in candidates if candidate_request_id(candidate) == request_id]


def summarize_reason_slugs(slugs: list[str]) -> str:
    """Render a bounded slug list for a terminal reason sentence."""
    shown = slugs[:MAX_REASON_SLUGS]
    summary = ", ".join(shown)
    remaining = len(slugs) - len(shown)
    if remaining > 0:
        summary += f", and {remaining} more"
    return summary


def bounded_scope_ids(
    values: list[Any],
    label: str,
    *,
    truncate: bool = False,
) -> list[str]:
    """Normalize an order scope without ever persisting a host-invalid ID array."""
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > MAX_SCOPE_ID_LENGTH
            or "\x00" in value
        ):
            raise OrchestrationControllerError(
                "ORCHESTRATION_SCOPE_INVALID",
                f"{label} contains an invalid scoped id",
                recoverable=False,
                remediation=f"Repair malformed {label} workspace records before resuming orchestration.",
            )
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
        if truncate and len(normalized) == MAX_SCOPE_IDS:
            break
    if len(normalized) > MAX_SCOPE_IDS:
        raise OrchestrationControllerError(
            "ORCHESTRATION_SCOPE_EXCEEDED",
            f"{label} exceeds the {MAX_SCOPE_IDS}-id work-order limit",
            recoverable=False,
            remediation=f"Split or reduce {label} before resuming orchestration.",
        )
    return normalized


def selected_candidate_id_snapshot(
    candidates: list[dict[str, Any]],
    request_ids: list[str],
) -> list[str]:
    """Capture bounded historical selections without snapshotting unrelated candidates."""
    request_scope = set(request_ids)
    selected = [
        candidate.get("candidate_id")
        for candidate in candidates
        if candidate_request_id(candidate) in request_scope and candidate_state(candidate) == "selected"
    ]
    return sorted(bounded_scope_ids(selected, "selected candidate baseline"))


def request_candidate_state_snapshot(
    candidates: list[dict[str, Any]],
    request_ids: list[str],
) -> dict[str, str]:
    """Capture a bounded identity/state baseline for request-scoped candidates."""
    request_scope = set(request_ids)
    snapshot: dict[str, str] = {}
    for candidate in candidates:
        if candidate_request_id(candidate) not in request_scope:
            continue
        candidate_id = candidate.get("candidate_id")
        if (
            not isinstance(candidate_id, str)
            or not candidate_id
            or len(candidate_id) > MAX_SCOPE_ID_LENGTH
            or "\x00" in candidate_id
        ):
            raise OrchestrationControllerError(
                "CANDIDATE_STORE_INVALID",
                "request-scoped discovery candidate lacks a bounded candidate_id",
                recoverable=False,
            )
        if candidate_id in snapshot:
            raise OrchestrationControllerError(
                "CANDIDATE_STORE_INVALID",
                f"request-scoped discovery candidate id is duplicated: {candidate_id}",
                recoverable=False,
            )
        if len(snapshot) >= MAX_SCOPE_IDS:
            raise OrchestrationControllerError(
                "ORCHESTRATION_SCOPE_EXCEEDED",
                f"request-scoped candidate baseline exceeds {MAX_SCOPE_IDS} records",
                recoverable=False,
                remediation="Resolve, supersede, or split the source request before starting another discovery action.",
            )
        state = candidate_state(candidate)
        if len(state) > 64 or not re.fullmatch(r"[a-z][a-z0-9_-]*", state):
            raise OrchestrationControllerError(
                "CANDIDATE_STORE_INVALID",
                f"request-scoped discovery candidate {candidate_id} has an invalid lifecycle state",
                recoverable=False,
            )
        snapshot[candidate_id] = state
    return dict(sorted(snapshot.items()))


def standards_discovery_route(candidate: dict[str, Any]) -> str | None:
    standards = candidate.get("standards") if isinstance(candidate.get("standards"), dict) else None
    if standards is None:
        return None
    registry = standards.get("registry_provider")
    if not isinstance(registry, str) or not registry.strip():
        return None
    registry = registry.strip().lower()
    if registry == "iso-open-data":
        return "iso-open-data"
    if registry in {"your-europe", "eu-harmonised-standards", "eur-lex"}:
        return "eu-product-requirements"
    if registry == "uk-geospatial-register":
        return "uk-geospatial-register"
    if registry in {"nist", "nist-standards-info", "nist-csrc"}:
        return "nist"
    return None


def candidate_uses_permitted_discovery_provider(
    candidate: dict[str, Any],
    permitted: set[str],
) -> bool:
    standards_route = standards_discovery_route(candidate)
    if standards_route is not None:
        return "standards" in permitted or f"standards:{standards_route}" in permitted
    recorded = candidate.get("discovery_providers")
    if isinstance(recorded, list):
        providers = {
            value.strip().lower()
            for value in recorded
            if isinstance(value, str) and value.strip()
        }
    else:
        provider = candidate_provider(candidate)
        providers = {provider} if provider is not None else set()
    return bool(providers) and providers <= permitted


def eligible_new_discovery_candidates(
    candidates: list[dict[str, Any]],
    request_ids: list[str],
    candidate_states_before: dict[str, str],
    discovery_providers: set[str],
    acquisition_providers: set[str],
) -> list[dict[str, Any]]:
    request_scope = set(request_ids)
    return [
        candidate
        for candidate in candidates
        if candidate_request_id(candidate) in request_scope
        and isinstance(candidate.get("candidate_id"), str)
        and candidate.get("candidate_id") not in candidate_states_before
        and candidate_state(candidate) in DISCOVERY_APPEND_CANDIDATE_STATES
        and candidate_uses_permitted_discovery_provider(candidate, discovery_providers)
        and acquisition_route(candidate, acquisition_providers) is not None
    ]


def safe_relative_artifact(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_ARTIFACT_PATH_LENGTH or "\x00" in value:
        raise OrchestrationControllerError("RESULT_INVALID", "result artifacts contain an invalid path")
    normalized = value.replace("\\", "/")
    if normalized.startswith(("/", "\\")) or WINDOWS_ABSOLUTE_RE.match(value):
        raise OrchestrationControllerError("RESULT_INVALID", "result artifacts must be workspace-relative")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise OrchestrationControllerError("RESULT_INVALID", "result artifacts must not escape the workspace")
    return path.as_posix()


def is_parent_orchestration_artifact(value: str) -> bool:
    parts = tuple(part.casefold().rstrip(" .") for part in PurePosixPath(value.replace("\\", "/")).parts)
    return len(parts) >= 2 and parts[:2] == ("runs", "orchestrations")


def valid_stored_result_shape(document: dict[str, Any], action_id: str) -> bool:
    expected_fields = {"schema_version", "action_id", "outcome", "summary", "artifacts"}
    if set(document) != expected_fields:
        return False
    if document.get("schema_version") != SCHEMA_VERSION or document.get("action_id") != action_id:
        return False
    if document.get("outcome") not in RESULT_OUTCOMES:
        return False
    summary = document.get("summary")
    if (
        not isinstance(summary, str)
        or summary != summary.strip()
        or not summary
        or len(summary) > MAX_SUMMARY_LENGTH
    ):
        return False
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) > MAX_ARTIFACTS:
        return False
    try:
        normalized_artifacts = [safe_relative_artifact(value) for value in artifacts]
    except OrchestrationControllerError:
        return False
    return (
        artifacts == normalized_artifacts
        and len(normalized_artifacts) == len(set(normalized_artifacts))
        and not any(is_parent_orchestration_artifact(value) for value in normalized_artifacts)
    )


def load_result(path: Path, action_id: str, project_root: Path) -> dict[str, Any]:
    try:
        path.lstat()
    except OSError as exc:
        raise OrchestrationControllerError("RESULT_UNREADABLE", f"could not read result file: {path}") from exc
    document = load_json_object(
        path,
        error_code="RESULT_INVALID",
        label="orchestration result",
        max_bytes=MAX_RESULT_BYTES,
    )
    expected_fields = {"schema_version", "action_id", "outcome", "summary", "artifacts"}
    if set(document) != expected_fields:
        raise OrchestrationControllerError(
            "RESULT_INVALID",
            "result fields must be exactly schema_version, action_id, outcome, summary, artifacts",
            details={
                "missing": sorted(expected_fields - set(document)),
                "unsupported": sorted(set(document) - expected_fields),
            },
        )
    if document.get("schema_version") != SCHEMA_VERSION or document.get("action_id") != action_id:
        raise OrchestrationControllerError("RESULT_INVALID", "result schema_version or action_id does not match")
    if document.get("outcome") not in RESULT_OUTCOMES:
        raise OrchestrationControllerError("RESULT_INVALID", "result outcome must be completed, blocked, or failed")
    summary = document.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_SUMMARY_LENGTH:
        raise OrchestrationControllerError("RESULT_INVALID", "result summary must contain 1 to 4000 characters")
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) > MAX_ARTIFACTS:
        raise OrchestrationControllerError("RESULT_INVALID", "result artifacts must be a bounded list")
    normalized_artifacts = [safe_relative_artifact(value) for value in artifacts]
    if len(normalized_artifacts) != len(set(normalized_artifacts)):
        raise OrchestrationControllerError("RESULT_INVALID", "result artifact paths must be unique")
    if any(is_parent_orchestration_artifact(value) for value in normalized_artifacts):
        raise OrchestrationControllerError(
            "RESULT_INVALID",
            "result artifacts may not reference controller-owned runs/orchestrations state",
        )
    missing = [value for value in normalized_artifacts if not (project_root / value).exists()]
    if missing:
        raise OrchestrationControllerError(
            "ORCHESTRATION_POSTCONDITION_FAILED",
            "reported result artifacts do not exist in the workspace",
            recoverable=True,
            details={"missing_artifacts": missing},
        )
    return {**document, "summary": summary.strip(), "artifacts": normalized_artifacts}


def child_args(
    run_id: str,
    agent_id: str,
    *,
    to_state: str | None = None,
    final_verdict: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        run_id=run_id,
        agent_id=agent_id,
        to_state=to_state,
        final_verdict=final_verdict,
        reason=f"Orchestration advanced child run to {to_state or final_verdict}.",
        questions_processed_this_run=None,
        source_requests_opened_this_run=None,
        releases_this_run=None,
        discovery_results_this_run=None,
        acquisition_downloads_this_run=None,
        github_archive_bytes_this_run=None,
        academic_provider_requests_this_run=None,
        web_downloads_this_run=None,
        manual_url_deliveries_this_run=None,
    )


def new_child_run(project_root: Path, session: dict[str, Any]) -> dict[str, Any]:
    controller = load_sibling_module("run_controller")
    sequence = len(session["child_run_ids"]) + 1
    run_id = require_safe_id(f"run-{session['orchestration_id']}-{sequence:03d}", "action_id")
    session["child_run_ids"].append(run_id)
    session["active_run_id"] = run_id
    session["updated_at"] = timestamp_utc()
    # Persist intent before creating the child. If the process stops on either
    # side of run_start, active_child can deterministically create/reload this
    # exact id instead of minting an orphaned second child.
    write_json_atomic(session_path(project_root, session["orchestration_id"]), session)
    return controller.run_start(project_root, child_args(run_id, session["agent_id"]))


def active_child(project_root: Path, session: dict[str, Any]) -> dict[str, Any] | None:
    run_id = session.get("active_run_id")
    if not isinstance(run_id, str):
        return None
    controller = load_sibling_module("run_controller")
    try:
        document = controller.load_run_state(project_root, run_id)
    except Exception as exc:
        if getattr(exc, "error_code", None) == "RUN_UNKNOWN":
            # Recover a parent-persisted child creation intent after a crash
            # between the session write and run_controller.run_start.
            return controller.run_start(project_root, child_args(run_id, session["agent_id"]))
        raise
    if document.get("state", {}).get("current") in controller.TERMINAL_STATES:
        session["active_run_id"] = None
        return None
    return document


def advance_child(project_root: Path, session: dict[str, Any], desired_state: str) -> str:
    controller = load_sibling_module("run_controller")
    document = active_child(project_root, session)
    paths = {
        "discovering": ["planned", "discovering"],
        "candidates_ready": ["planned", "discovering", "candidates_ready"],
        "fetching": ["planned", "discovering", "candidates_ready", "fetch_planned", "fetching"],
        "answering": ["planned", "answering"],
        "verifying": ["planned", "answering", "verifying"],
    }
    if desired_state not in paths:
        raise OrchestrationControllerError("ORCHESTRATION_STATE_INVALID", f"unsupported child target: {desired_state}")
    if document is None:
        document = new_child_run(project_root, session)
    current = document["state"]["current"]
    target_path = paths[desired_state]
    if current == desired_state:
        return document["run_id"]
    if current == "evidence_ready" and desired_state in {"answering", "verifying"}:
        target_path = ["answering"] + (["verifying"] if desired_state == "verifying" else [])
    elif current in target_path:
        target_path = target_path[target_path.index(current) + 1 :]
    elif current == "initialized":
        pass
    else:
        # An active child in an unrelated forward-only branch is retained but
        # cannot be repurposed. Close it honestly and create a fresh child.
        allowed = set(document.get("state", {}).get("allowed_next_states") or [])
        final = "blocked_on_sources" if "blocked_on_sources" in allowed else "failed"
        controller.run_finish(project_root, child_args(document["run_id"], session["agent_id"], final_verdict=final))
        session["active_run_id"] = None
        document = new_child_run(project_root, session)
        current = "initialized"
    for state in target_path:
        if document["state"]["current"] == state:
            continue
        document = controller.run_transition(
            project_root,
            child_args(document["run_id"], session["agent_id"], to_state=state),
        )
    return document["run_id"]


def finish_active_child(project_root: Path, session: dict[str, Any], verdict: str) -> None:
    document = active_child(project_root, session)
    if document is None:
        return
    allowed = set(document.get("state", {}).get("allowed_next_states") or [])
    chosen = verdict if verdict in allowed else "failed" if "failed" in allowed else None
    if chosen is None:
        return
    controller = load_sibling_module("run_controller")
    controller.run_finish(
        project_root,
        child_args(document["run_id"], session["agent_id"], final_verdict=chosen),
    )
    session["active_run_id"] = None


def work_order_budgets(status: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    run = status.get("run") if isinstance(status.get("run"), dict) else {}
    allowed = {
        key: value
        for key, value in run.items()
        if key.startswith("max_") and isinstance(value, int) and not isinstance(value, bool)
    }
    allowed["action_timeout_seconds"] = int(session["limits"]["action_timeout_seconds"])
    return allowed


PHASE_BUDGET_STOP_REASONS = {
    "research": {"questions_exhausted", "source_requests_exhausted"},
    "discovery": {"discovery_results_exhausted", "academic_provider_requests_exhausted"},
    "acquisition": {
        "acquisition_downloads_exhausted",
        "github_archive_bytes_exhausted",
        "academic_provider_requests_exhausted",
        "web_downloads_exhausted",
        "manual_url_deliveries_exhausted",
    },
}


def rollover_exhausted_child_for_phase(
    project_root: Path,
    status: dict[str, Any],
    session: dict[str, Any],
    phase: str,
) -> bool:
    """Close an exhausted immutable child before issuing same-phase work in a fresh run."""
    relevant = PHASE_BUDGET_STOP_REASONS.get(phase, set())
    active_run_id = session.get("active_run_id")
    if not relevant or not isinstance(active_run_id, str) or not active_run_id:
        return False
    run_controller_status = (
        status.get("run_controller") if isinstance(status.get("run_controller"), dict) else {}
    )
    if run_controller_status.get("run_id") != active_run_id or run_controller_status.get("terminal") is True:
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "workspace status did not bind phase budgets to the parent session's active child run",
            remediation="Repair conflicting run-controller state before resuming the orchestration.",
            details={
                "phase": phase,
                "active_run_id": active_run_id,
                "status_run_id": run_controller_status.get("run_id"),
                "status_run_terminal": run_controller_status.get("terminal"),
            },
        )
    readiness = status.get("readiness") if isinstance(status.get("readiness"), dict) else {}
    budget = readiness.get("budget_state") if isinstance(readiness.get("budget_state"), dict) else {}
    stop_reasons = {
        value for value in budget.get("stop_reasons", []) if isinstance(value, str)
    }
    exhausted = sorted(stop_reasons & relevant)
    if not exhausted:
        return False
    document = active_child(project_root, session)
    if document is None:
        return False
    allowed = set(document.get("state", {}).get("allowed_next_states") or [])
    verdict = "no_ship" if "no_ship" in allowed else "blocked_on_sources" if "blocked_on_sources" in allowed else None
    if verdict is None:
        reason = (
            f"active child {active_run_id} exhausted {', '.join(exhausted)} but has no safe terminal transition"
        )
        session["status"] = PAUSED_STATUS
        session["phase"] = "paused"
        session["verdict"] = "paused"
        session["pause_reason"] = reason
        session["updated_at"] = timestamp_utc()
        write_json_atomic(session_path(project_root, session["orchestration_id"]), session)
        record_event(project_root, session, "session_paused", reason)
        return True
    finish_active_child(project_root, session, verdict)
    record_event(
        project_root,
        session,
        "child_run_budget_exhausted",
        "Closed a bounded child run after an artifact-derived phase budget was exhausted; subsequent work uses a fresh child run.",
        data={"run_id": active_run_id, "phase": phase, "stop_reasons": exhausted},
    )
    return True


def research_question_scope_limit(
    project_root: Path,
    status: dict[str, Any],
    session: dict[str, Any],
) -> int:
    """Return the current child run's remaining question budget, rolling over at zero."""
    run = status.get("run") if isinstance(status.get("run"), dict) else {}
    configured = run.get("max_questions_per_run", 25)
    if isinstance(configured, bool) or not isinstance(configured, int) or configured < 1:
        configured = 25
    configured = min(configured, MAX_SCOPE_IDS)

    active_run_id = session.get("active_run_id")
    if not isinstance(active_run_id, str) or not active_run_id:
        return configured
    run_controller = status.get("run_controller") if isinstance(status.get("run_controller"), dict) else {}
    if run_controller.get("run_id") != active_run_id or run_controller.get("terminal") is True:
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "workspace status did not bind the research budget to the parent session's active child run",
            remediation="Re-run orchestration status, repair conflicting active child runs, and resume the session.",
            details={
                "active_run_id": active_run_id,
                "status_run_id": run_controller.get("run_id"),
                "status_run_terminal": run_controller.get("terminal"),
            },
        )
    readiness = status.get("readiness") if isinstance(status.get("readiness"), dict) else {}
    budget = readiness.get("budget_state") if isinstance(readiness.get("budget_state"), dict) else {}
    remaining = budget.get("questions_remaining_this_run")
    if isinstance(remaining, bool) or not isinstance(remaining, int) or remaining < 0:
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "active child run is missing a valid artifact-derived remaining-question budget",
            remediation="Repair the run-controller budget state before resuming this orchestration session.",
            details={"active_run_id": active_run_id, "questions_remaining_this_run": remaining},
        )
    if remaining > 0:
        return min(configured, remaining)

    if run_controller.get("state") != "answering":
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "only an answering child run can roll over an exhausted question budget",
            remediation="Repair the active child run state before resuming this orchestration session.",
            details={"active_run_id": active_run_id, "state": run_controller.get("state")},
        )

    finish_active_child(project_root, session, "no_ship")
    record_event(
        project_root,
        session,
        "child_run_budget_exhausted",
        "Closed a bounded child run after its question budget was exhausted; the next action uses a fresh child run.",
        data={"run_id": active_run_id, "budget": "max_questions_per_run"},
    )
    return configured


def delegated_request_partition(
    project_root: Path,
    config: dict[str, Any],
    session: dict[str, Any],
    requests: list[dict[str, Any]],
    max_attempts: int,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Split open requests into ones worth delegating again and ones this session retired.

    Exhaustion is derived from the durable attempt audit rather than a counter, so the
    decision survives a crashed or replaced session and stays checkable from artifacts.
    Only this session's events count: a new session gets a fresh look at every request,
    which is the supported way to retry after fixing a host-side cause. Editing the
    append-only audit is not.
    """
    source_requests = load_sibling_module("source_requests")
    events = request_attempt_audit_events(project_root, config, error_code="SOURCE_REQUESTS_INVALID")
    failures = source_requests.attempt_failures_by_request(
        events,
        orchestration_id=session["orchestration_id"],
    )

    routable: list[dict[str, Any]] = []
    exhausted: dict[str, str] = {}
    for request in requests:
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            continue
        attempts = failures.get(request_id, [])
        if not attempts:
            routable.append(request)
            continue
        # The audit is append-only, so file order is chronological and the last entry is
        # the most recent attempt.
        last_code = str(attempts[-1].get("failure_code") or "")
        if not is_retryable_attempt_failure_code(last_code):
            # A standing refusal — authorization, site policy, a pending human decision —
            # will answer the same way next time, so it retires the request immediately
            # instead of spending the remaining budget proving it.
            exhausted[request_id] = last_code
        elif len(attempts) >= max_attempts:
            exhausted[request_id] = last_code
        else:
            routable.append(request)
    return routable, exhausted


def delegated_acquisition_route(
    project_root: Path,
    config: dict[str, Any],
    session: dict[str, Any],
    status: dict[str, Any],
    posture: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    """Route one delegated acquisition order, or retire the session.

    Delegation replaces the provider walk rather than extending it: there are no
    candidates to review and no discovery route to compose, because the acquirer chooses
    how to obtain the evidence. One order carries every routable request, so a host with
    parallel connectors is not forced through one protocol round trip per request.
    """
    requests = open_requests(project_root, config)
    routable, exhausted = delegated_request_partition(
        project_root,
        config,
        session,
        requests,
        posture["max_attempts_per_request"],
    )

    if not routable:
        if exhausted:
            reason = (
                f"{DELEGATED_EXHAUSTED_TERMINAL_REASON}: "
                f"{len(exhausted)} request(s) ({summarize_reason_slugs(sorted(exhausted))}). "
                "Fix the acquirer-side cause, then start a new session."
            )
        else:
            # Readiness says blocked-on-sources but no open request explains it. Lint
            # reports a blocked question without a linked open request as HIGH, which
            # normally flips the verdict first; reaching here means the workspace
            # disagrees with itself, so say that rather than blaming the acquirer.
            reason = (
                "Delegated acquisition has no open source request to fulfil while the workspace "
                "reports blocked_on_sources. Reconcile the blocked questions with their source requests."
            )
        return None, {
            "terminal_status": "blocked_on_sources",
            "reason": reason,
            "workspace_status": status,
            "event_data": {"exhausted_requests": dict(sorted(exhausted.items()))},
        }

    # Truncation is not a loss: requests dropped by the cap stay open and are scoped by
    # the next order once this one completes.
    request_ids = bounded_scope_ids(
        [request.get("request_id") for request in routable],
        "delegated acquisition request scope",
        truncate=True,
    )
    scoped = set(request_ids)
    question_slugs = bounded_scope_ids(
        [
            slug
            for request in routable
            if request.get("request_id") in scoped
            for slug in request.get("question_slugs", [])
            if isinstance(slug, str)
        ],
        "question scope for delegated acquisition",
        truncate=True,
    )
    if rollover_exhausted_child_for_phase(project_root, status, session, "acquisition"):
        if session.get("status") == PAUSED_STATUS:
            return None, {"paused": True, "workspace_status": status}
    return "acquisition", {
        "status": status,
        "scope": {
            "question_slugs": question_slugs,
            "request_ids": request_ids,
            # Delegated acquisition has no candidate store: the acquirer decides how to
            # obtain the evidence, and the controller verifies what was delivered.
            "candidate_ids": [],
        },
        "delegated": True,
        "acquirer_agent_id": posture["acquirer_agent_id"],
    }


def choose_route(
    project_root: Path,
    session: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    config = load_config(project_root)
    policy = provider_policy(config)
    session["provider_policy"] = policy
    status = fresh_workspace_status(project_root)
    health = status.get("workspace_health") if isinstance(status.get("workspace_health"), dict) else {}
    readiness = status.get("readiness") if isinstance(status.get("readiness"), dict) else {}
    verdict = readiness.get("verdict")

    if not health.get("materially_valid", False) or verdict == "attention_required":
        return None, {
            "terminal_status": "no_ship",
            "reason": "Workspace health or HIGH validation findings require operator attention.",
            "workspace_status": status,
        }

    if verdict == "in_progress":
        questions = status.get("questions") if isinstance(status.get("questions"), dict) else {}
        slugs = questions.get("actionable_slugs") if isinstance(questions.get("actionable_slugs"), list) else []
        awaiting_review = int(readiness.get("questions_awaiting_review", 0) or 0)
        if not slugs and awaiting_review:
            # Under review.escalation_scope: question, a workspace whose only remaining work is
            # pending human review reports in_progress with an empty actionable scope. Issuing a
            # research work order here would scope it to no questions at all. The session cannot
            # ship; the host collects the reviews and starts a new session.
            review_slugs = bounded_scope_ids(
                [value for value in questions.get("human_review_slugs", []) if isinstance(value, str)],
                "human review question scope",
                truncate=True,
            )
            return None, {
                "terminal_status": "no_ship",
                "reason": (
                    f"{AWAITING_REVIEW_TERMINAL_REASON}: {awaiting_review} question(s) "
                    f"({summarize_reason_slugs(review_slugs)}). Record the outstanding reviews, "
                    "then start a new session."
                ),
                "workspace_status": status,
                "event_data": {
                    "questions_awaiting_review": awaiting_review,
                    "question_slugs": review_slugs,
                },
            }
        if rollover_exhausted_child_for_phase(project_root, status, session, "research"):
            if session.get("status") == PAUSED_STATUS:
                return None, {"paused": True, "workspace_status": status}
        scope_limit = research_question_scope_limit(project_root, status, session)
        return "research", {
            "status": status,
            "scope": {
                "question_slugs": [str(value) for value in slugs[:scope_limit]],
                "request_ids": [],
                "candidate_ids": [],
            },
        }

    if verdict == "blocked_on_sources":
        posture = session_acquisition_policy(session)
        if posture["acquisition_mode"] == ACQUISITION_MODE_DELEGATED:
            # Returns before the provider walk rather than around it: delegation and
            # enabled workspace providers are mutually exclusive (refused at start), so
            # the candidate and discovery arms below have nothing to say about a
            # delegated workspace and must not run for one.
            return delegated_acquisition_route(project_root, config, session, status, posture)
        requests = open_requests(project_root, config)
        candidates = load_candidates(project_root, config)
        acquisition = policy["acquisition"]
        acquisition_providers = set(acquisition["providers"]) if acquisition["enabled"] else set()
        discovery_providers = composable_discovery_providers(policy)
        route_failures: list[dict[str, Any]] = []
        for request in requests:
            raw_request_id = request.get("request_id")
            if not isinstance(raw_request_id, str) or not raw_request_id:
                continue
            request_id = bounded_scope_ids([raw_request_id], "source request scope")[0]
            question_slugs = bounded_scope_ids(
                [value for value in request.get("question_slugs", []) if isinstance(value, str)],
                f"question scope for source request {request_id}",
            )
            scoped = request_candidates(candidates, request_id)
            routable = [
                candidate
                for candidate in scoped
                if candidate_state(candidate) == "selected" and acquisition_route(candidate, acquisition_providers)
            ]
            if routable:
                # Candidate stores retain provider ranking. Acquire one selected
                # candidate at a time so retries never authorize duplicate
                # downloads for every historical selection on a request.
                candidate_ids = bounded_scope_ids(
                    [routable[0].get("candidate_id")],
                    f"acquisition candidate scope for source request {request_id}",
                )
                if rollover_exhausted_child_for_phase(project_root, status, session, "acquisition"):
                    if session.get("status") == PAUSED_STATUS:
                        return None, {"paused": True, "workspace_status": status}
                return "acquisition", {
                    "status": status,
                    "scope": {
                        "question_slugs": question_slugs,
                        "request_ids": [request_id],
                        "candidate_ids": candidate_ids,
                    },
                    "request": request,
                }
            reviewable = [
                candidate
                for candidate in scoped
                if candidate_state(candidate) in REVIEWABLE_CANDIDATE_STATES
                and acquisition_route(candidate, acquisition_providers) is not None
            ]
            if reviewable:
                candidate_ids = bounded_scope_ids(
                    [item.get("candidate_id") for item in reviewable],
                    f"candidate-review scope for source request {request_id}",
                    truncate=True,
                )
                return "candidate_review", {
                    "status": status,
                    "scope": {
                        "question_slugs": question_slugs,
                        "request_ids": [request_id],
                        "candidate_ids": candidate_ids,
                    },
                    "request": request,
                }
            if discovery_providers:
                if len(scoped) >= MAX_SCOPE_IDS:
                    route_failures.append(
                        {
                            "request_id": request_id,
                            "reason": (
                                "request candidate history exhausted the bounded discovery baseline; review, "
                                "supersede, or split the request before further discovery"
                            ),
                            "candidate_count": len(scoped),
                        }
                    )
                    continue
                if rollover_exhausted_child_for_phase(project_root, status, session, "discovery"):
                    if session.get("status") == PAUSED_STATUS:
                        return None, {"paused": True, "workspace_status": status}
                return "discovery", {
                    "status": status,
                    "scope": {
                        "question_slugs": question_slugs,
                        "request_ids": [request_id],
                        "candidate_ids": [],
                    },
                    "request": request,
                    "candidate_count_before": len(scoped),
                    "discovery_providers": discovery_providers,
                }
            if scoped:
                route_failures.append(
                    {
                        "request_id": request_id,
                        "reason": "existing candidates have no remaining explicitly enabled acquisition route",
                        "candidate_ids": [
                            str(item.get("candidate_id")) for item in scoped if item.get("candidate_id")
                        ],
                    }
                )
                continue
            route_failures.append(
                {
                    "request_id": request_id,
                    "reason": "no discovery provider composes with an enabled acquisition provider",
                    "discovery_providers": policy["discovery"]["providers"],
                    "acquisition_providers": policy["acquisition"]["providers"],
                }
            )
        return None, {
            "terminal_status": "blocked_on_sources",
            "reason": (
                "No permitted end-to-end provider route can satisfy the open source requests. Enable a composable "
                "discovery/acquisition pair in research.yml, refine or replace exhausted candidates, or deliver "
                "reviewed evidence manually. "
                f"Effective discovery providers: {', '.join(policy['discovery']['providers']) or 'none'}; "
                f"effective acquisition providers: {', '.join(policy['acquisition']['providers']) or 'none'}; "
                f"blocked request ids: {', '.join(str(item.get('request_id')) for item in route_failures) or 'none'}."
            ),
            "workspace_status": status,
            "route_failures": route_failures,
        }

    if verdict == "complete":
        return "verification", {
            "status": status,
            "scope": {"question_slugs": [], "request_ids": [], "candidate_ids": []},
        }

    return None, {
        "terminal_status": "failed",
        "reason": f"Unsupported workspace readiness verdict: {verdict!r}.",
        "workspace_status": status,
    }


def action_spec(
    project_root: Path,
    session: dict[str, Any],
    route: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    status = context["status"]
    scope = context["scope"]
    config = load_config(project_root)
    candidates_input = relative_workspace_path(project_root, candidate_store_path(project_root, config))
    effective_policy = session["provider_policy"]
    budgets = work_order_budgets(status, session)
    run_id: str | None
    skill: str
    inputs = ["research.yml", "AGENTS.md"]
    postconditions: list[dict[str, Any]]
    if route == "research":
        scoped_questions_before = scoped_question_snapshot(
            project_root,
            config,
            [value for value in scope.get("question_slugs", []) if isinstance(value, str)],
        )
        run_id = advance_child(project_root, session, "answering")
        skill = "research-run"
        inputs.extend(["wiki/questions", "sources/normalized", f"runs/{run_id}/run-state.json"])
        postconditions = [
            {
                "check": "workspace_readiness_changed",
                "allowed_verdicts": ["in_progress", "blocked_on_sources", "complete"],
                "scoped_questions_before": scoped_questions_before,
                "question_file_fingerprints_before": question_file_fingerprint_snapshot(
                    project_root,
                    config,
                ),
                "source_request_record_fingerprints_before": source_request_record_fingerprint_snapshot(
                    project_root,
                    config,
                ),
            },
            {"check": "child_run_state", "expected": "answering"},
        ]
    elif route == "discovery":
        permitted = list(context.get("discovery_providers") or [])
        candidates_before = load_candidates(project_root, config)
        candidate_states_before = request_candidate_state_snapshot(
            candidates_before,
            [value for value in scope.get("request_ids", []) if isinstance(value, str)],
        )
        run_id = advance_child(project_root, session, "discovering")
        skill = "research-discover"
        inputs.extend(["sources/source-requests.jsonl", candidates_input])
        effective_policy = {
            "discovery": {"enabled": bool(permitted), "providers": permitted},
            "acquisition": dict(session["provider_policy"]["acquisition"]),
        }
        postconditions = [
            {
                "check": "request_scoped_candidates_increased",
                "before": len(candidate_states_before),
                "candidate_states_before": candidate_states_before,
                "candidate_record_fingerprints_before": candidate_record_fingerprint_snapshot(
                    candidates_before
                ),
            },
            {
                "check": "discovery_never_fetches",
                "manifest_records_before": int(status.get("sources", {}).get("manifest_records", 0) or 0),
                "manifest_digest_before": evidence_manifest_digest(project_root),
            },
            {"check": "raw_tree_unchanged", "before": raw_tree_snapshot(project_root, config)},
        ]
        remaining_candidate_capacity = MAX_SCOPE_IDS - len(candidate_states_before)
        budgets["max_discovery_results_per_run"] = min(
            int(budgets.get("max_discovery_results_per_run", remaining_candidate_capacity) or 0),
            remaining_candidate_capacity,
        )
    elif route == "candidate_review":
        candidates_before = load_candidates(project_root, config)
        selected_candidate_ids_before = selected_candidate_id_snapshot(
            candidates_before,
            [value for value in scope.get("request_ids", []) if isinstance(value, str)],
        )
        run_id = advance_child(project_root, session, "candidates_ready")
        skill = "research-discover"
        inputs.extend(["sources/source-requests.jsonl", candidates_input])
        postconditions = [
            {
                "check": "selected_candidate_for_request",
                "selected_before": len(selected_candidate_ids_before),
                "selected_candidate_ids_before": selected_candidate_ids_before,
                "candidate_record_fingerprints_before": candidate_record_fingerprint_snapshot(
                    candidates_before
                ),
            },
            {
                "check": "selection_does_not_fetch",
                "manifest_records_before": int(status.get("sources", {}).get("manifest_records", 0) or 0),
                "manifest_digest_before": evidence_manifest_digest(project_root),
            },
            {"check": "raw_tree_unchanged", "before": raw_tree_snapshot(project_root, config)},
        ]
    elif route == "acquisition":
        delegated = bool(context.get("delegated"))
        scoped_question_slugs = [
            value for value in scope.get("question_slugs", []) if isinstance(value, str)
        ]
        scoped_request_ids = [value for value in scope.get("request_ids", []) if isinstance(value, str)]
        scoped_candidate_ids = [value for value in scope.get("candidate_ids", []) if isinstance(value, str)]
        blocked_questions_before = linked_blocked_questions_snapshot(
            project_root,
            config,
            scoped_question_slugs,
            scoped_request_ids,
        )
        # Both reuse baselines, in one pass over one manifest: the map of sources this
        # order may reconcile digest-for-digest, and — one clause wider — the ids of
        # sources its own scoped requests already name that nothing has normalized yet.
        # Reuse of the latter is the affordance the scoped-match map cannot carry, because
        # it has no second digest to hold them to. Computed here, from pre-existing state
        # only, so nothing the acquirer writes during the order can add an id to either.
        (
            matching_source_records_before,
            reusable_source_ids_before,
            manifest_records_before,
        ) = acquisition_reuse_baselines(
            project_root,
            config,
            scoped_request_ids,
            # Delegated orders correlate pre-existing evidence by request alone; there is
            # no candidate store to name.
            None if delegated else scoped_candidate_ids,
        )
        matching_source_ids_before = sorted(matching_source_records_before)
        raw_tree_before = raw_tree_snapshot(project_root, config, include_entries=True)
        candidate_records_before = candidate_record_fingerprint_snapshot(load_candidates(project_root, config))
        run_id = advance_child(project_root, session, "fetching")
        # The child run is the same audit continuity for a delegated acquirer as for a
        # managed worker: one bounded attempt, one run state, one rollover budget.
        if delegated:
            skill = "research-acquire-delegated"
            # No candidate store to read; the attempt audit is where a failed attempt is
            # recorded, so name it as an input the acquirer is expected to write.
            inputs.extend(
                [
                    "sources/source-requests.jsonl",
                    "sources/manifest.jsonl",
                    relative_workspace_path(
                        project_root,
                        load_sibling_module("source_requests").request_attempt_audit_path(project_root, config),
                    ),
                ]
            )
        else:
            skill = "research-acquire"
            inputs.extend(["sources/source-requests.jsonl", candidates_input, "sources/manifest.jsonl"])
        postconditions = [
            {"check": "request_fulfilled_with_normalized_source"},
            {
                "check": "linked_blocked_questions_reopened",
                "blocked_questions_before": blocked_questions_before,
            },
            {
                "check": "manifest_records_increased",
                "before": int(status.get("sources", {}).get("manifest_records", 0) or 0),
                "matching_source_ids_before": matching_source_ids_before,
                "matching_source_records_before": matching_source_records_before,
                "reusable_source_ids_before": reusable_source_ids_before,
                "manifest_record_fingerprints_before": manifest_records_before,
                "raw_tree_before": raw_tree_before,
                "candidate_record_fingerprints_before": candidate_records_before,
                "candidate_audit_record_fingerprints_before": (
                    candidate_audit_record_fingerprint_snapshot(project_root, config)
                ),
                # Delegated acquisition proves a failed attempt by appending to this audit,
                # so submission needs the exact pre-action set to tell a new event from a
                # rewritten one. Captured for both modes: the provider arm's baseline is
                # simply empty, and one shape keeps the replay guard and the sidecar
                # externalization from needing a mode branch.
                "request_attempt_audit_record_fingerprints_before": (
                    request_attempt_audit_record_fingerprint_snapshot(project_root, config)
                ),
                "source_request_record_fingerprints_before": source_request_record_fingerprint_snapshot(
                    project_root,
                    config,
                ),
                "normalized_file_fingerprints_before": normalized_file_fingerprint_snapshot(
                    project_root,
                    config,
                ),
                "question_file_fingerprints_before": question_file_fingerprint_snapshot(
                    project_root,
                    config,
                ),
            },
        ]
    elif route == "verification":
        run_id = advance_child(project_root, session, "verifying")
        skill = "research-verify"
        inputs.extend(["wiki/questions", "sources/normalized", "sources/manifest.jsonl"])
        evaluation_root = f"runs/{run_id}/evaluation"
        verification_paths = [
            f"{evaluation_root}/citation-verification.json",
            f"{evaluation_root}/export.json",
            f"{evaluation_root}/lint.json",
            f"{evaluation_root}/publication-readiness.json",
        ]
        postconditions = [
            {
                "check": "fresh_verification_bundle",
                "paths": verification_paths,
                "before": {
                    path: file_digest(project_root / path, containment_root=project_root)
                    for path in verification_paths
                },
            },
            {"check": "publication_readiness", "expected": "ship"},
        ]
    else:  # pragma: no cover - internal guard
        raise OrchestrationControllerError("ORCHESTRATION_STATE_INVALID", f"unknown route: {route}")
    spec: dict[str, Any] = {
        "phase": route,
        "skill": skill,
        "run_id": run_id,
        "scope": scope,
        "provider_policy": effective_policy,
        "budgets": budgets,
        "inputs": sorted(set(inputs)),
        "required_postconditions": postconditions,
    }
    if route == "acquisition" and context.get("delegated"):
        # Additive, and only on the orders they describe: an order without them is a
        # provider-mode order, which is what every pre-delegation order is.
        spec["acquisition_mode"] = ACQUISITION_MODE_DELEGATED
        spec["assigned_agent_id"] = context["acquirer_agent_id"]
    return spec


INTEGRITY_BASELINE_FIELDS = frozenset(
    {
        "scoped_questions_before",
        "question_file_fingerprints_before",
        "source_request_record_fingerprints_before",
        "candidate_states_before",
        "candidate_record_fingerprints_before",
        "candidate_audit_record_fingerprints_before",
        "request_attempt_audit_record_fingerprints_before",
        "selected_candidate_ids_before",
        "blocked_questions_before",
        "matching_source_ids_before",
        "matching_source_records_before",
        "reusable_source_ids_before",
        "manifest_record_fingerprints_before",
        "raw_tree_before",
        "normalized_file_fingerprints_before",
    }
)


def baseline_value_count(value: Any) -> int:
    if isinstance(value, (dict, list)):
        return len(value)
    return 1


def externalize_integrity_baselines(
    project_root: Path,
    work_order: dict[str, Any],
) -> None:
    """Move large pre-action guards into one protected controller-owned artifact."""
    extracted: list[dict[str, Any]] = []
    field_count = 0
    entry_count = 0
    for postcondition in work_order.get("required_postconditions", []):
        if not isinstance(postcondition, dict):
            continue
        fields = {
            field: postcondition.pop(field)
            for field in sorted(INTEGRITY_BASELINE_FIELDS & set(postcondition))
        }
        if not fields:
            continue
        field_count += len(fields)
        entry_count += sum(baseline_value_count(value) for value in fields.values())
        extracted.append({"check": postcondition.get("check"), "fields": fields})
    if not extracted:
        return
    orchestration_id = require_safe_id(work_order.get("orchestration_id"), "orchestration_id")
    action_id = require_safe_id(work_order.get("action_id"), "action_id")
    document = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "orchestration_integrity_baseline",
        "orchestration_id": orchestration_id,
        "action_id": action_id,
        "phase": work_order.get("phase"),
        "postconditions": extracted,
    }
    encoded_baseline = (json.dumps(document, indent=2, sort_keys=False) + "\n").encode("utf-8")
    if len(encoded_baseline) > MAX_SCOPE_GUARD_BYTES:
        raise OrchestrationControllerError(
            "ORCHESTRATION_SCOPE_EXCEEDED",
            "scope-integrity baseline exceeds the protected 8 MiB artifact limit",
            recoverable=False,
            remediation="Archive historical workspace records or reduce the bounded action scope before retrying.",
            details={"encoded_bytes": len(encoded_baseline), "max_bytes": MAX_SCOPE_GUARD_BYTES},
        )
    baseline_path = scope_integrity_baseline_path(project_root, orchestration_id, action_id)
    write_json_atomic(baseline_path, document)
    fingerprint = file_digest(
        baseline_path,
        max_bytes=MAX_SCOPE_GUARD_BYTES,
        containment_root=project_root,
    )
    if fingerprint is None:
        raise OrchestrationControllerError(
            "ORCHESTRATION_WRITE_FAILED",
            "could not fingerprint the persisted scope-integrity baseline",
            recoverable=True,
        )
    work_order["required_postconditions"].append(
        {
            "check": "controller_integrity_baseline",
            "path": relative_workspace_path(project_root, baseline_path),
            "fingerprint": fingerprint,
            "field_count": field_count,
            "entry_count": entry_count,
        }
    )


def hydrate_integrity_baselines(
    project_root: Path,
    work_order: dict[str, Any],
) -> dict[str, Any]:
    """Validate and merge one protected baseline artifact into an in-memory work order."""
    phase = work_order.get("phase")
    if phase == "verification":
        return work_order
    guards = [
        item
        for item in work_order.get("required_postconditions", [])
        if isinstance(item, dict) and item.get("check") == "controller_integrity_baseline"
    ]
    if len(guards) != 1:
        return work_order
    guard = guards[0]
    orchestration_id = require_safe_id(work_order.get("orchestration_id"), "orchestration_id")
    action_id = require_safe_id(work_order.get("action_id"), "action_id")
    expected_path = scope_integrity_baseline_path(project_root, orchestration_id, action_id)
    expected_relative = relative_workspace_path(project_root, expected_path)
    if (
        guard.get("path") != expected_relative
        or not valid_sha256_fingerprint(guard.get("fingerprint"))
        or isinstance(guard.get("field_count"), bool)
        or not isinstance(guard.get("field_count"), int)
        or not 1 <= guard["field_count"] <= len(INTEGRITY_BASELINE_FIELDS)
        or isinstance(guard.get("entry_count"), bool)
        or not isinstance(guard.get("entry_count"), int)
        or not 0 <= guard["entry_count"] <= MAX_SCOPE_GUARD_ENTRIES * len(INTEGRITY_BASELINE_FIELDS)
    ):
        raise OrchestrationControllerError(
            "ORCHESTRATION_INTEGRITY_BASELINE_INVALID",
            "work order contains an invalid controller integrity-baseline reference",
            recoverable=False,
            remediation="Preserve the orchestration for audit and start a fresh session.",
        )
    actual_fingerprint = file_digest(
        expected_path,
        max_bytes=MAX_SCOPE_GUARD_BYTES,
        containment_root=project_root,
    )
    if actual_fingerprint != guard["fingerprint"]:
        raise OrchestrationControllerError(
            "ORCHESTRATION_INTEGRITY_BASELINE_CHANGED",
            "controller-owned scope-integrity baseline is missing or changed",
            recoverable=False,
            remediation="Restore the protected baseline exactly or preserve this session and start a fresh one.",
        )
    document = load_json_object(
        expected_path,
        error_code="ORCHESTRATION_INTEGRITY_BASELINE_INVALID",
        label="scope-integrity baseline",
        max_bytes=MAX_SCOPE_GUARD_BYTES,
        containment_root=project_root,
    )
    if (
        set(document)
        != {"schema_version", "artifact_type", "orchestration_id", "action_id", "phase", "postconditions"}
        or document.get("schema_version") != SCHEMA_VERSION
        or document.get("artifact_type") != "orchestration_integrity_baseline"
        or document.get("orchestration_id") != orchestration_id
        or document.get("action_id") != action_id
        or document.get("phase") != phase
        or not isinstance(document.get("postconditions"), list)
    ):
        raise OrchestrationControllerError(
            "ORCHESTRATION_INTEGRITY_BASELINE_INVALID",
            "controller-owned scope-integrity baseline identity or shape is invalid",
            recoverable=False,
        )
    hydrated = json.loads(json.dumps(work_order))
    hydrated["required_postconditions"] = [
        item
        for item in hydrated["required_postconditions"]
        if item.get("check") != "controller_integrity_baseline"
    ]
    by_check = {
        item.get("check"): item
        for item in hydrated["required_postconditions"]
        if isinstance(item, dict) and isinstance(item.get("check"), str)
    }
    observed_fields = 0
    observed_entries = 0
    seen_checks: set[str] = set()
    for item in document["postconditions"]:
        if not isinstance(item, dict) or set(item) != {"check", "fields"}:
            raise OrchestrationControllerError(
                "ORCHESTRATION_INTEGRITY_BASELINE_INVALID",
                "scope-integrity baseline contains an invalid postcondition entry",
                recoverable=False,
            )
        check = item.get("check")
        fields = item.get("fields")
        target = by_check.get(check) if isinstance(check, str) else None
        if (
            not isinstance(target, dict)
            or check in seen_checks
            or not isinstance(fields, dict)
            or not fields
            or not set(fields) <= INTEGRITY_BASELINE_FIELDS
            or set(fields) & set(target)
        ):
            raise OrchestrationControllerError(
                "ORCHESTRATION_INTEGRITY_BASELINE_INVALID",
                "scope-integrity baseline does not match retained work-order postconditions",
                recoverable=False,
            )
        seen_checks.add(check)
        target.update(fields)
        observed_fields += len(fields)
        observed_entries += sum(baseline_value_count(value) for value in fields.values())
    if observed_fields != guard["field_count"] or observed_entries != guard["entry_count"]:
        raise OrchestrationControllerError(
            "ORCHESTRATION_INTEGRITY_BASELINE_INVALID",
            "scope-integrity baseline summary does not match its protected content",
            recoverable=False,
        )
    return hydrated


def issue_work_order(project_root: Path, session: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    action_number = int(session["action_count"]) + 1
    action_id = f"action-{action_number:04d}"
    lease_seconds = int(session["limits"]["action_timeout_seconds"])
    work_order = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": WORK_ORDER_ARTIFACT_TYPE,
        "orchestration_id": session["orchestration_id"],
        "action_id": action_id,
        "issued_at": format_timestamp(now),
        "phase": spec["phase"],
        "skill": spec["skill"],
        "run_id": spec["run_id"],
        # The session owner, unchanged: the acquirer is who the order is addressed to,
        # not who may drive the protocol. Single-driver ownership is not renegotiated by
        # delegation.
        "agent_id": session["agent_id"],
        "scope": spec["scope"],
        "provider_policy": spec["provider_policy"],
        "budgets": spec["budgets"],
        "inputs": spec["inputs"],
        "required_postconditions": spec["required_postconditions"],
        "lease": {
            "duration_seconds": lease_seconds,
            "expires_at": format_timestamp(now + timedelta(seconds=lease_seconds)),
            "attempt": 1,
        },
    }
    for field in ("acquisition_mode", "assigned_agent_id"):
        if field in spec:
            work_order[field] = spec[field]
    externalize_integrity_baselines(project_root, work_order)
    encoded_order = (json.dumps(work_order, indent=2, sort_keys=False, ensure_ascii=False) + "\n").encode("utf-8")
    if len(encoded_order) > MAX_WORK_ORDER_BYTES:
        raise OrchestrationControllerError(
            "ORCHESTRATION_SCOPE_EXCEEDED",
            "work order exceeds the managed-run 256 KiB size limit after baseline externalization",
            recoverable=False,
            remediation="Reduce the bounded action scope before resuming orchestration.",
            details={"encoded_bytes": len(encoded_order), "max_bytes": MAX_WORK_ORDER_BYTES},
        )
    static_fingerprint = trusted_static_input_fingerprint(project_root)
    write_json_atomic(
        trusted_static_input_path(project_root, session["orchestration_id"], action_id),
        static_fingerprint,
    )
    write_json_atomic(work_order_path(project_root, session["orchestration_id"], action_id), work_order)
    session["phase"] = spec["phase"]
    session["pending_action_id"] = action_id
    session["pending_submission"] = None
    session["pending_trusted_static_inputs"] = {
        "action_id": action_id,
        "fingerprint": static_fingerprint["fingerprint"],
        "entry_count": static_fingerprint["entry_count"],
        "total_bytes": static_fingerprint["total_bytes"],
    }
    session["recovery"] = default_recovery_state()
    session["action_count"] = action_number
    session["window_action_count"] = int(session.get("window_action_count", 0)) + 1
    session["updated_at"] = timestamp_utc()
    write_json_atomic(session_path(project_root, session["orchestration_id"]), session)
    record_event(
        project_root,
        session,
        "action_issued",
        f"Issued {spec['phase']} work order.",
        action_id=action_id,
        data=event_data_with_driver(),
    )
    return work_order


def replay_work_order(
    project_root: Path,
    session: dict[str, Any],
    *,
    resume: bool,
    retained_order: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action_id = require_safe_id(session.get("pending_action_id"), "action_id")
    path = work_order_path(project_root, session["orchestration_id"], action_id)
    if retained_order is None:
        work_order = load_json_object(path, error_code="WORK_ORDER_INVALID", label="work order")
        verify_runtime_guards(project_root, session, work_order)
    else:
        work_order = retained_order
    lease = work_order.get("lease") if isinstance(work_order.get("lease"), dict) else {}
    expires_at = parse_timestamp(lease.get("expires_at"))
    if resume and expires_at is not None and expires_at <= datetime.now(timezone.utc):
        now = datetime.now(timezone.utc)
        duration = int(lease.get("duration_seconds", session["limits"]["action_timeout_seconds"]) or 0)
        attempt = int(lease.get("attempt", 1) or 1) + 1
        work_order["issued_at"] = format_timestamp(now)
        work_order["lease"] = {
            "duration_seconds": duration,
            "expires_at": format_timestamp(now + timedelta(seconds=duration)),
            "attempt": attempt,
        }
        write_json_atomic(path, work_order)
        session["recovery"] = {
            "state": RECOVERY_RECONCILE,
            "action_id": action_id,
            "attempt": attempt,
            "reason_code": "result_absent_after_interruption",
            "recorded_at": timestamp_utc(),
        }
        session["updated_at"] = timestamp_utc()
        write_json_atomic(session_path(project_root, session["orchestration_id"]), session)
        record_event(
            project_root,
            session,
            "action_reissued",
            "Reissued expired work order for same-action reconciliation.",
            action_id=action_id,
            data={"lease_attempt": attempt, "recovery_mode": "reconcile"},
        )
    return work_order


def pause_if_limited(project_root: Path, session: dict[str, Any]) -> bool:
    limits = session["limits"]
    reason: str | None = None
    if int(session.get("window_action_count", 0)) >= int(limits["max_actions"]):
        reason = "max_actions reached for this orchestration window"
    started = parse_timestamp(session.get("window_started_at")) or datetime.now(timezone.utc)
    elapsed = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
    if elapsed >= int(limits["total_timeout_seconds"]):
        reason = "total_timeout_seconds reached for this orchestration window"
    if reason is None:
        return False
    session["status"] = PAUSED_STATUS
    session["phase"] = "paused"
    session["verdict"] = "paused"
    session["pause_reason"] = reason
    session["updated_at"] = timestamp_utc()
    write_json_atomic(session_path(project_root, session["orchestration_id"]), session)
    record_event(project_root, session, "session_paused", reason)
    return True


def resume_session(project_root: Path, session: dict[str, Any]) -> None:
    if session["status"] != PAUSED_STATUS:
        return
    session["status"] = ACTIVE_STATUS
    session["phase"] = "planning"
    session["verdict"] = None
    session["pause_reason"] = None
    session["window_action_count"] = 0
    session["window_started_at"] = timestamp_utc()
    session["updated_at"] = timestamp_utc()
    write_json_atomic(session_path(project_root, session["orchestration_id"]), session)
    record_event(project_root, session, "session_resumed", "Started a fresh bounded orchestration window.")


def finish_session(
    project_root: Path,
    session: dict[str, Any],
    status: str,
    reason: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in TERMINAL_STATUSES:
        raise OrchestrationControllerError("ORCHESTRATION_STATE_INVALID", f"invalid terminal status: {status}")
    if status in {"blocked_on_sources", "no_ship", "failed"}:
        child_verdict = "blocked_on_sources" if status == "blocked_on_sources" else status
        finish_active_child(project_root, session, child_verdict)
    session["status"] = status
    session["phase"] = status
    session["verdict"] = status
    session["pending_action_id"] = None
    if "pending_trusted_static_inputs" in session:
        session["pending_trusted_static_inputs"] = None
    session["pause_reason"] = reason if status != "complete" else None
    session["updated_at"] = timestamp_utc()
    session["completed_at"] = session["updated_at"]
    write_json_atomic(session_path(project_root, session["orchestration_id"]), session)
    record_event(project_root, session, "session_finished", reason, data=data)
    return session


def start_session(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    agent_id = require_agent_id(args.agent_id)
    orchestration_id = require_safe_id(args.orchestration_id or generated_orchestration_id(), "orchestration_id")
    # Refuse before creating durable state when the workspace contract cannot be read.
    status = fresh_workspace_status(project_root)
    health = status.get("workspace_health") if isinstance(status.get("workspace_health"), dict) else {}
    if not health.get("materially_valid", False):
        raise OrchestrationControllerError(
            "WORKSPACE_UNREADABLE",
            "workspace health rejected the research contract",
            details={"findings": health.get("findings", [])},
        )
    with driver_session_lock(
        project_root,
        orchestration_id,
        command="start",
        agent_id=agent_id,
        wait_seconds=getattr(args, "driver_wait_seconds", 0.0),
    ):
        path = session_path(project_root, orchestration_id)
        if path.exists():
            raise OrchestrationControllerError(
                "ORCHESTRATION_EXISTS",
                f"orchestration already exists: {orchestration_id}",
            )
        now = timestamp_utc()
        config = load_config(project_root)
        # Resolved before the session document and its directories are created, so a
        # contradictory or malformed declaration leaves no session behind. (The lock file
        # this block already took remains, as it does for every in-lock refusal.)
        declared_acquisition = acquisition_policy(config)
        project = config.get("project") if isinstance(config.get("project"), dict) else {}
        handoff = project.get("handoff") if isinstance(project.get("handoff"), dict) else None
        session: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "artifact_type": SESSION_ARTIFACT_TYPE,
            "orchestration_id": orchestration_id,
            "started_at": now,
            "updated_at": now,
            "completed_at": None,
            "agent_id": agent_id,
            "handoff": handoff,
            "status": ACTIVE_STATUS,
            "phase": "planning",
            "verdict": None,
            "pause_reason": None,
            "pending_action_id": None,
            "pending_submission": None,
            "pending_trusted_static_inputs": None,
            "recovery": default_recovery_state(),
            "last_completed_action_id": None,
            "active_run_id": None,
            "child_run_ids": [],
            "action_count": 0,
            "completed_action_count": 0,
            "window_action_count": 0,
            "window_started_at": now,
            "limits": {
                "max_actions": args.max_actions,
                "action_timeout_seconds": args.action_timeout_seconds,
                "total_timeout_seconds": args.total_timeout_seconds,
            },
            "provider_policy": provider_policy(config),
            # Frozen at start like provider_policy, and for the same reason: a session
            # must not have who-acquires changed underneath the work orders it issued.
            # Written explicitly in both modes so a providers-mode session created now is
            # distinguishable from one created before delegation existed.
            **declared_acquisition,
            "failure_records": [],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        (path.parent / WORK_ORDERS_DIR).mkdir(parents=True, exist_ok=True)
        (path.parent / WORK_RESULTS_DIR).mkdir(parents=True, exist_ok=True)
        (path.parent / TRUSTED_INPUTS_DIR).mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, session)
        record_event(project_root, session, "session_started", "Orchestration session created.")
        return session


def next_work(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    orchestration_id = require_safe_id(args.orchestration_id, "orchestration_id")
    with driver_session_lock(
        project_root,
        orchestration_id,
        command="next",
        agent_id=args.agent_id,
        wait_seconds=getattr(args, "driver_wait_seconds", 0.0),
    ):
        session = load_session(project_root, orchestration_id)
        enforce_control_repair_gate(project_root, orchestration_id)
        if args.agent_id is not None and require_agent_id(args.agent_id) != session["agent_id"]:
            raise OrchestrationControllerError(
                "ORCHESTRATION_OWNER_MISMATCH",
                "--agent-id does not own this orchestration session",
                recoverable=False,
            )
        pending_submission = session.get("pending_submission")
        if pending_submission is not None:
            action_id = require_safe_id(pending_submission.get("action_id"), "action_id")
            order = load_json_object(
                work_order_path(project_root, orchestration_id, action_id),
                error_code="WORK_ORDER_INVALID",
                label="work order",
            )
            require_action_baselines(order, project_root)
            verify_runtime_guards(project_root, session, order)
            session = finalize_pending_submission(project_root, session, order)
        repair_last_completion_events(project_root, session)
        if session["status"] in TERMINAL_STATUSES:
            return session
        if args.resume:
            resume_session(project_root, session)
        elif session["status"] == PAUSED_STATUS:
            return session
        if session.get("pending_action_id"):
            action_id = require_safe_id(session["pending_action_id"], "action_id")
            order = load_json_object(
                work_order_path(project_root, orchestration_id, action_id),
                error_code="WORK_ORDER_INVALID",
                label="work order",
            )
            require_action_baselines(order, project_root)
            legacy_unbound = "pending_trusted_static_inputs" not in session
            if legacy_unbound:
                # Only parse the declarative YAML authorization before the
                # migration snapshot exists. Bind all trusted workspace code
                # before fresh_workspace_status imports or executes it.
                verify_provider_policy_unchanged(project_root, session, order)
                bind_legacy_pending_trusted_inputs(project_root, session, order)
            verify_runtime_guards(project_root, session, order)
            return replay_work_order(project_root, session, resume=args.resume, retained_order=order)
        if pause_if_limited(project_root, session):
            return session
        # Routing re-reads research.yml and adopts the current provider policy, because a
        # fresh order is issued under whatever authorization exists now. The acquisition
        # mode is deliberately not adopted that way: who acquires is a property of the
        # session, frozen at start, and silently switching it mid-session would change
        # which orders the session may issue and who may execute them. Every other path
        # reaches this check through verify_runtime_guards; a first `next` does not.
        verify_delegation_unchanged(project_root, session)
        route, context = choose_route(project_root, session)
        if route is None:
            if session.get("status") == PAUSED_STATUS:
                return session
            return finish_session(
                project_root,
                session,
                context["terminal_status"],
                context["reason"],
                context.get("event_data"),
            )
        spec = action_spec(project_root, session, route, context)
        return issue_work_order(project_root, session, spec)


def selected_candidates_for_scope(
    project_root: Path,
    config: dict[str, Any],
    request_ids: list[str],
    candidate_ids: list[str],
) -> list[dict[str, Any]]:
    request_scope = set(request_ids)
    candidate_scope = set(candidate_ids)
    return [
        candidate
        for candidate in load_candidates(project_root, config)
        if candidate_request_id(candidate) in request_scope
        and candidate.get("candidate_id") in candidate_scope
        and candidate_state(candidate) == "selected"
    ]


def selected_candidates_outside_scope(
    project_root: Path,
    config: dict[str, Any],
    request_ids: list[str],
    candidate_ids: list[str],
    selected_candidate_ids_before: list[str] | None = None,
) -> list[dict[str, Any]]:
    request_scope = set(request_ids)
    candidate_scope = set(candidate_ids)
    historical_selected = set(selected_candidate_ids_before or [])
    return [
        candidate
        for candidate in load_candidates(project_root, config)
        if candidate_request_id(candidate) in request_scope
        and candidate.get("candidate_id") not in candidate_scope
        and candidate.get("candidate_id") not in historical_selected
        and candidate_state(candidate) == "selected"
    ]


def strip_generated_timestamps(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_generated_timestamps(child)
            for key, child in value.items()
            if key != "generated_at"
        }
    if isinstance(value, list):
        return [strip_generated_timestamps(child) for child in value]
    return value


def citation_results_by_source(citation: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    selected: dict[str, list[dict[str, Any]]] = {}
    results = citation.get("results") if isinstance(citation.get("results"), list) else []
    for result in results:
        if not isinstance(result, dict):
            continue
        source_id = result.get("source_id")
        if isinstance(source_id, str) and source_id.strip():
            selected.setdefault(source_id.strip(), []).append(dict(result))
    return selected


def export_with_authoritative_citations(
    export: dict[str, Any],
    citation: dict[str, Any],
) -> dict[str, Any]:
    normalized = json.loads(json.dumps(export))
    by_source = citation_results_by_source(citation)
    questions = normalized.get("questions") if isinstance(normalized.get("questions"), list) else []
    for question in questions:
        if not isinstance(question, dict):
            continue
        source_ids = question.get("source_ids") if isinstance(question.get("source_ids"), list) else []
        question["citation_verification"] = [
            dict(result)
            for source_id in source_ids
            if isinstance(source_id, str)
            for result in by_source.get(source_id, [])
        ]
    return normalized


def build_authoritative_verification(
    project_root: Path,
    run_id: str,
) -> dict[str, dict[str, Any]]:
    """Recompute all publication inputs in memory without trusting or writing worker JSON."""
    try:
        config = load_config(project_root)
        status_module = load_sibling_module("workspace_status")
        lint_module = load_sibling_module("lint")
        export_module = load_sibling_module("export_answers")
        citation_module = load_sibling_module("verify_citations")
        readiness_module = load_sibling_module("publication_readiness")

        run_state = project_root / "runs" / run_id / "run-state.json"
        status = status_module.build_status_document(project_root, run_id=run_id if run_state.is_file() else None)
        lint_report = lint_module.run_checks(project_root, config)
        citation = citation_module.build_report(
            project_root,
            SimpleNamespace(source_id=None, live=False, provider=None),
        )
        authoritative_by_source = citation_results_by_source(citation)
        original_loader = export_module.load_citation_verification_by_source

        def load_authoritative_citations(_root: Path, _warnings: list[str]) -> dict[str, list[dict[str, Any]]]:
            return authoritative_by_source

        export_module.load_citation_verification_by_source = load_authoritative_citations
        try:
            export = export_module.build_export(project_root, None)
        finally:
            export_module.load_citation_verification_by_source = original_loader
        export = export_with_authoritative_citations(export, citation)
        publication = readiness_module.build_readiness_document(
            project_root,
            embedded_inputs={
                "status": status,
                "lint": lint_report,
                "export": export,
                "citation_verification": citation,
            },
        )
    except OrchestrationControllerError:
        raise
    except (Exception, SystemExit) as exc:
        raise OrchestrationControllerError(
            "ORCHESTRATION_POSTCONDITION_FAILED",
            f"could not recompute authoritative verification inputs: {exc}",
            recoverable=True,
            remediation="Repair the workspace artifacts, regenerate the verification bundle, and retry the action.",
        ) from exc
    return {
        "citation-verification.json": citation,
        "lint.json": lint_report,
        "export.json": export,
        "publication-readiness.json": publication,
    }


def verification_semantic_value(
    name: str,
    document: dict[str, Any],
    citation: dict[str, Any],
) -> Any:
    value: Any = document
    if name == "export.json":
        value = export_with_authoritative_citations(document, citation)
    elif name == "publication-readiness.json":
        value = {
            key: child
            for key, child in document.items()
            if key not in {"generated_at", "workspace_status"}
        }
    return strip_generated_timestamps(value)


def normalized_source_quality_failure(
    project_root: Path,
    path: Path,
    record: dict[str, Any],
) -> dict[str, Any] | None:
    """Return why a normalized source is unusable for acquisition fulfillment."""
    payload = bounded_regular_bytes(
        path,
        max_bytes=MAX_VERIFICATION_ARTIFACT_BYTES,
        error_code="ORCHESTRATION_POSTCONDITION_FAILED",
        label="normalized evidence",
        containment_root=project_root,
    )
    if payload is None:  # pragma: no cover - missing_ok is false
        return {"reason": "normalized evidence is missing"}
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        return {"reason": "normalized evidence is not valid UTF-8", "error": str(exc)}
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return {"reason": "normalized evidence lacks YAML frontmatter"}
    closing_index = next(
        (index for index in range(1, len(lines)) if lines[index].strip() == "---"),
        None,
    )
    if closing_index is None:
        return {"reason": "normalized evidence has unterminated YAML frontmatter"}
    try:
        frontmatter = yaml.safe_load("\n".join(lines[1:closing_index])) or {}
    except yaml.YAMLError as exc:
        return {"reason": "normalized evidence has invalid YAML frontmatter", "error": str(exc)}
    source_id = record.get("id")
    if (
        not isinstance(frontmatter, dict)
        or frontmatter.get("type") != "normalized_source"
        or frontmatter.get("source_id") != source_id
    ):
        return {
            "reason": "normalized evidence frontmatter does not identify the manifest source",
            "expected_source_id": source_id,
            "actual_source_id": frontmatter.get("source_id")
            if isinstance(frontmatter, dict)
            else None,
        }
    status = frontmatter.get("status")
    if not isinstance(status, str) or not status.strip():
        return {"reason": "normalized evidence lacks a bounded extraction status"}
    if status.strip().lower() in {"failed", "stubbed"}:
        return {"reason": f"normalized evidence has unusable extraction status {status!r}"}
    if frontmatter.get("evidence_usable") is not True:
        return {
            "reason": "normalized evidence is not explicitly marked usable",
            "evidence_usable": frontmatter.get("evidence_usable"),
        }

    is_pdf = (
        record.get("kind") == "pdf"
        or isinstance(record.get("raw_pdf"), str)
        or frontmatter.get("source_kind") == "pdf"
        or frontmatter.get("extraction_method") == "pdf_text"
    )
    if is_pdf:
        body = "\n".join(lines[closing_index + 1 :])
        extracted = re.search(
            r"(?ms)^## Extracted Text[ \t]*\n+(.*?)(?=^##[ \t]+|\Z)",
            body,
        )
        extracted_text = extracted.group(1).strip() if extracted is not None else ""
        if not extracted_text or extracted_text.casefold() == "none extracted.":
            return {"reason": "normalized PDF evidence contains no extracted text"}
    return None


def candidate_failure_audit_events(
    project_root: Path,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Read the bounded append-only candidate audit used to prove route failure."""
    discover_sources = load_sibling_module("discover_sources")
    path = discover_sources.candidate_audit_path(project_root, config)
    payload = bounded_regular_bytes(
        path,
        max_bytes=MAX_SCOPE_GUARD_BYTES,
        error_code="ORCHESTRATION_POSTCONDITION_FAILED",
        label="candidate lifecycle audit",
        missing_ok=True,
        containment_root=project_root,
    )
    if payload is None:
        return []
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise OrchestrationControllerError(
            "ORCHESTRATION_POSTCONDITION_FAILED",
            f"candidate lifecycle audit is not valid UTF-8: {exc}",
            recoverable=True,
        ) from exc
    if len(lines) > MAX_SCOPE_GUARD_ENTRIES:
        raise OrchestrationControllerError(
            "ORCHESTRATION_SCOPE_EXCEEDED",
            "candidate lifecycle audit exceeds the bounded integrity-guard limit",
            recoverable=False,
        )
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OrchestrationControllerError(
                "ORCHESTRATION_POSTCONDITION_FAILED",
                f"candidate lifecycle audit contains invalid JSON at line {line_number}: {exc}",
                recoverable=True,
            ) from exc
        if not isinstance(event, dict):
            raise OrchestrationControllerError(
                "ORCHESTRATION_POSTCONDITION_FAILED",
                f"candidate lifecycle audit entry {line_number} is not an object",
                recoverable=True,
            )
        events.append(event)
    return events


def candidate_audit_record_fingerprint_snapshot(
    project_root: Path,
    config: dict[str, Any],
) -> dict[str, str]:
    return record_fingerprint_snapshot(
        candidate_failure_audit_events(project_root, config),
        id_field="event_id",
        label="candidate lifecycle audit",
    )


def request_attempt_audit_events(
    project_root: Path,
    config: dict[str, Any],
    *,
    error_code: str = "ORCHESTRATION_POSTCONDITION_FAILED",
) -> list[dict[str, Any]]:
    """Read the bounded append-only request-attempt audit used to prove attempt failure.

    One reader with one set of safety properties — bounded, containment-checked, and fatal
    on a malformed line — serves both callers. ``error_code`` lets each report in its own
    vocabulary: routing reads the audit as a workspace input, while submission reads it as
    a postcondition guard.
    """
    source_requests = load_sibling_module("source_requests")
    path = source_requests.request_attempt_audit_path(project_root, config)
    payload = bounded_regular_bytes(
        path,
        max_bytes=MAX_SCOPE_GUARD_BYTES,
        error_code=error_code,
        label="source request attempt audit",
        missing_ok=True,
        containment_root=project_root,
    )
    if payload is None:
        return []
    events: list[dict[str, Any]] = []
    for line in payload.decode("utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise OrchestrationControllerError(
                error_code,
                f"invalid source request attempt audit JSON: {exc}",
                recoverable=True,
                remediation="Restore the append-only attempt audit before submitting this action.",
            ) from exc
        if not isinstance(event, dict) or not isinstance(event.get("event_id"), str):
            raise OrchestrationControllerError(
                error_code,
                "source request attempt audit contains an event without a stable event_id",
                recoverable=True,
                remediation="Restore the append-only attempt audit before submitting this action.",
            )
        events.append(event)
        if len(events) > MAX_SCOPE_GUARD_ENTRIES:
            raise OrchestrationControllerError(
                error_code,
                "source request attempt audit exceeds the bounded entry guard",
                recoverable=False,
                remediation="Archive the workspace; the attempt audit has outgrown the bounded verification guard.",
            )
    return events


def request_attempt_audit_record_fingerprint_snapshot(
    project_root: Path,
    config: dict[str, Any],
) -> dict[str, str]:
    return record_fingerprint_snapshot(
        request_attempt_audit_events(project_root, config),
        id_field="event_id",
        label="source request attempt audit",
    )


def delegated_fulfilment_correlation_failures(
    fulfilled: list[dict[str, Any]],
    by_source_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return fulfilments whose evidence is not linked to the request it claims to satisfy.

    Delegated acquisition has no candidate store, so the correlation is the provenance
    sidecar's ``request_id`` alone. That field only reaches a manifest record from a
    sidecar delivered beside the artifact, which is what makes "the source carries a
    provenance sidecar" checkable rather than merely asserted.

    **CR-4 seam.** When a source request grows a structured ``scope``, ``--match-scope``
    verification is one added predicate in this function — compare the request's declared
    scope keys against the record's delivery/provenance metadata — with no change to the
    verifier arm that calls it.
    """
    failures: list[dict[str, Any]] = []
    for request in fulfilled:
        request_id = str(request.get("request_id") or "")
        source_id = str(request.get("source_id") or "")
        record = by_source_id.get(source_id)
        provenance = record.get("provenance") if isinstance(record, dict) else None
        provenance_request_id = provenance.get("request_id") if isinstance(provenance, dict) else None
        if not isinstance(provenance, dict) or provenance_request_id != request_id:
            failures.append(
                {
                    "request_id": request_id,
                    "source_id": source_id,
                    "has_provenance": isinstance(provenance, dict),
                    "provenance_request_id": provenance_request_id,
                }
            )
    return failures


def load_order_claims(
    project_root: Path,
    orchestration_id: str,
    action_id: str,
    order_claims: ModuleType,
) -> dict[str, Any]:
    """Read one action's claim ledger, failing closed and bounded like every other input.

    A ledger the controller cannot read is never reported as "no claims". Both arms rely on
    the opposite reading -- the completed arm to know what to commit, the blocked arm to
    refuse an attempt that claimed anything -- so degrading to empty would turn an
    unreadable ledger into a silently accepted submission.
    """
    try:
        claims = order_claims.load_claims(
            order_claims.claims_path(project_root, orchestration_id, action_id)
        )
    except order_claims.OrderClaimError as exc:
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_UNREADABLE",
            f"delegated acquisition claims could not be read: {exc.message}",
            recoverable=True,
            remediation="Restore the orchestration control tree; unreadable claims cannot authorize bookkeeping.",
        ) from exc
    entries = len(claims.get("fulfilments", {})) + len(claims.get("reopens", {}))
    if entries > MAX_SCOPE_GUARD_ENTRIES:
        raise OrchestrationControllerError(
            "ORCHESTRATION_SCOPE_EXCEEDED",
            f"delegated acquisition claimed {entries} bookkeeping entries, over the {MAX_SCOPE_GUARD_ENTRIES} limit",
            recoverable=False,
        )
    return claims


def commit_delegated_bookkeeping(
    project_root: Path,
    config: dict[str, Any],
    fulfilment_claims: dict[str, Any],
    reopen_claims: dict[str, Any],
    committed_slugs: set[str],
) -> None:
    """Write the bookkeeping the acquirer claimed, now that the controller has accepted it.

    Called from the wet pass only, and only after every ``require`` above it has passed, so
    a refused submission leaves the request store and the question pages exactly as it
    found them. It is deliberately the *last* thing the delegated arm does: the twin-pass
    check compares the phase each pass returns, and a pass that wrote before deciding could
    only be compared against a workspace it had already changed.

    Idempotent in both halves, because finalization can be interrupted and replayed.
    """
    source_requests = load_sibling_module("source_requests")
    question_resolve = load_sibling_module("question_resolve")
    now = timestamp_utc()
    # Reopens first, and the order matters. Finalization is replayable, so a crash can land
    # between the two halves. Reopening first leaves questions open with no blocking link --
    # a state the health guard does not inspect. Fulfilling first would leave a *blocked*
    # question whose blocking request reads fulfilled, which lint reports and readiness
    # turns into ORCHESTRATION_WORKSPACE_HEALTH_CHANGED, refused as unrecoverable before the
    # replay ever reaches finalization. The cheaper half-state is the one to leave behind.
    for slug, claim in sorted(reopen_claims.items()):
        if slug in committed_slugs:
            continue
        source_ids = [value for value in claim.get("source_ids", []) if isinstance(value, str)]
        question_resolve.commit_reopen_claim(project_root, config, slug, source_ids, now)
    if fulfilment_claims:
        path = source_requests.requests_path(project_root, config)
        with source_requests.workspace_lock(
            source_requests.source_requests_lock_path(path), purpose="source request mutation"
        ):
            records = source_requests.load_requests(path)
            changed = False
            for record in records:
                claim = fulfilment_claims.get(str(record.get("request_id")))
                if not isinstance(claim, dict):
                    continue
                if record.get("status") == "fulfilled" and record.get("source_id") == claim["source_id"]:
                    continue
                record["status"] = "fulfilled"
                record["source_id"] = str(claim["source_id"])
                record["updated_at"] = now
                changed = True
            if changed:
                source_requests._write_requests_unlocked(path, records)


def verify_delegated_acquisition_postconditions(
    project_root: Path,
    session: dict[str, Any],
    work_order: dict[str, Any],
    *,
    config: dict[str, Any],
    status: dict[str, Any],
    run_id: Any,
    current: Any,
    controller: ModuleType,
    apply_effects: bool,
) -> tuple[str | None, str | None]:
    """Verify one completed delegated acquisition action from durable artifacts.

    A sibling of the provider arm rather than a branch inside it: the two share most of
    their evidence checks but differ in what they are allowed to expect, and interleaving
    them would make it impossible to see at a glance that provider verification is
    unchanged.

    The differences are exactly five. Per-request outcomes replace all-must-be-fulfilled,
    because one order carries the whole routable backlog. Evidence correlates by request
    alone, because there is no candidate. A question is reopened only when *every* request
    blocking it was fulfilled. Candidate records may not change at all. And a request the
    acquirer could not satisfy must be accounted for by a new, append-only attempt-failure
    event naming this action.
    """
    scope = work_order.get("scope") if isinstance(work_order.get("scope"), dict) else {}
    request_ids = [value for value in scope.get("request_ids", []) if isinstance(value, str)]
    scoped_requests = set(request_ids)
    action_id = work_order.get("action_id")

    def require(
        condition: bool,
        message: str,
        details: dict[str, Any] | None = None,
        remediation: str | None = None,
    ) -> None:
        if not condition:
            raise OrchestrationControllerError(
                "ORCHESTRATION_POSTCONDITION_FAILED",
                message,
                recoverable=True,
                remediation=remediation or "Complete the persisted work order and resubmit the same action id.",
                details=details,
            )

    def recorded_postcondition(check: str) -> dict[str, Any]:
        return next(
            (
                item
                for item in work_order.get("required_postconditions", [])
                if isinstance(item, dict) and item.get("check") == check
            ),
            {},
        )

    require(
        bool(scoped_requests) and valid_scope_id_list(request_ids),
        "delegated acquisition lacks a bounded request scope",
    )
    require(
        not [value for value in scope.get("candidate_ids", []) if isinstance(value, str) and value],
        "delegated acquisition work order carries a candidate scope",
        remediation="Start a fresh orchestration session; delegated acquisition has no candidate store.",
    )

    manifest_guard = recorded_postcondition("manifest_records_increased")
    before_manifest = int(manifest_guard.get("before", 0) or 0)
    matching_source_ids_before = manifest_guard.get("matching_source_ids_before")
    matching_source_records_before = manifest_guard.get("matching_source_records_before")
    # Absent on every order issued before un-normalized reuse existed. Those replay with it
    # unavailable rather than being refused for a field they could not have carried.
    reusable_source_ids_before = manifest_guard.get("reusable_source_ids_before", [])
    manifest_records_before = manifest_guard.get("manifest_record_fingerprints_before")
    raw_tree_before = manifest_guard.get("raw_tree_before")
    candidate_records_before = manifest_guard.get("candidate_record_fingerprints_before")
    attempt_audit_before = manifest_guard.get("request_attempt_audit_record_fingerprints_before")
    source_requests_before = manifest_guard.get("source_request_record_fingerprints_before")
    normalized_files_before = manifest_guard.get("normalized_file_fingerprints_before")
    question_files_before = manifest_guard.get("question_file_fingerprints_before")
    require(
        valid_scope_id_list(matching_source_ids_before)
        and valid_matching_source_record_snapshot(matching_source_records_before)
        and set(matching_source_ids_before) == set(matching_source_records_before)
        and valid_record_fingerprint_snapshot(manifest_records_before)
        and set(matching_source_ids_before) <= set(manifest_records_before)
        and valid_unnormalized_reuse_baseline(
            reusable_source_ids_before, matching_source_ids_before, manifest_records_before
        )
        and before_manifest == len(manifest_records_before)
        and valid_raw_tree_snapshot(raw_tree_before, include_entries=True)
        and valid_record_fingerprint_snapshot(candidate_records_before)
        and valid_record_fingerprint_snapshot(attempt_audit_before)
        and valid_record_fingerprint_snapshot(source_requests_before)
        and valid_file_fingerprint_snapshot(normalized_files_before, prefix="sources/")
        and valid_question_file_fingerprint_snapshot(question_files_before),
        "delegated acquisition work order lacks a valid bounded evidence integrity baseline",
        remediation="Start a fresh orchestration session; never infer matching evidence after execution.",
    )

    source_requests = load_sibling_module("source_requests")
    all_requests = source_requests.load_requests(source_requests.requests_path(project_root, config))
    requests_by_id = {
        str(item.get("request_id")): item
        for item in all_requests
        if isinstance(item, dict) and isinstance(item.get("request_id"), str)
    }
    require(
        scoped_requests <= set(requests_by_id),
        "delegated acquisition removed scoped source requests",
        {"missing_request_ids": sorted(scoped_requests - set(requests_by_id))},
        "Restore the source-request store; an attempted request is never deleted.",
    )

    # --- contingent bookkeeping -------------------------------------------------------
    # What the acquirer *claims* it did. The request store and the question pages are
    # frozen for the duration of the order, so this ledger -- not the store -- is what says
    # which request was fulfilled by which source. Everything downstream reads the
    # projection; nothing downstream reads a status the acquirer wrote.
    order_claims = load_sibling_module("_order_claims")
    claims = load_order_claims(
        project_root,
        require_safe_id(session["orchestration_id"], "orchestration_id"),
        action_id,
        order_claims,
    )
    fulfilment_claims = {
        str(key): value
        for key, value in claims.get("fulfilments", {}).items()
        if isinstance(value, dict) and isinstance(value.get("source_id"), str)
    }
    reopen_claims = {
        str(key): value
        for key, value in claims.get("reopens", {}).items()
        if isinstance(value, dict)
    }
    require(
        set(fulfilment_claims) <= scoped_requests,
        "delegated acquisition claimed a fulfilment for a request this order does not scope",
        {"request_ids": sorted(set(fulfilment_claims) - scoped_requests)},
        "Fulfil only request ids named by this work order.",
    )
    projected_requests_by_id = dict(requests_by_id)
    for claimed_id, claim in fulfilment_claims.items():
        projected = dict(requests_by_id[claimed_id])
        projected["status"] = "fulfilled"
        projected["source_id"] = str(claim["source_id"])
        projected_requests_by_id[claimed_id] = projected
    # A claim whose durable record already carries it is one the controller committed on a
    # previous, interrupted finalization. Those ids -- and only those -- may differ from the
    # frozen baseline; everything else in the store must be byte-identical.
    committed_request_ids = {
        claimed_id
        for claimed_id, claim in fulfilment_claims.items()
        if requests_by_id[claimed_id].get("status") == "fulfilled"
        and requests_by_id[claimed_id].get("source_id") == claim["source_id"]
    }

    # --- per-request outcomes ---------------------------------------------------------
    current_attempt_events = request_attempt_audit_events(project_root, config)
    current_attempt_fingerprints = record_fingerprint_snapshot(
        current_attempt_events,
        id_field="event_id",
        label="source request attempt audit",
    )
    new_attempt_event_ids = set(current_attempt_fingerprints) - set(attempt_audit_before)
    attempt_audit_violations = fingerprint_scope_violations(
        attempt_audit_before,
        current_attempt_fingerprints,
        mutable_ids=set(),
        allowed_new_ids=new_attempt_event_ids,
    )
    require(
        not any(attempt_audit_violations.values()),
        "delegated acquisition rewrote or removed recorded acquisition attempts",
        {"attempt_audit_violations": attempt_audit_violations},
        "Restore the append-only attempt audit; a recorded attempt is evidence and is never edited.",
    )

    failed_requests: dict[str, list[dict[str, Any]]] = {}
    unattributable_events: list[dict[str, Any]] = []
    for event in current_attempt_events:
        if event.get("event_id") not in new_attempt_event_ids:
            continue
        event_request_id = event.get("request_id")
        failure_code = event.get("failure_code")
        if (
            event_request_id not in scoped_requests
            or event.get("action_id") != action_id
            or not is_attempt_failure_code(failure_code)
        ):
            unattributable_events.append(
                {
                    "event_id": event.get("event_id"),
                    "request_id": event_request_id,
                    "action_id": event.get("action_id"),
                    "failure_code": failure_code,
                }
            )
            continue
        failed_requests.setdefault(str(event_request_id), []).append(event)
    require(
        not unattributable_events,
        "delegated acquisition recorded attempt failures outside this action's request scope",
        {"unattributable_events": unattributable_events},
        (
            "Record an attempt failure only for a scoped request, with this work order's action id and a "
            "documented failure code."
        ),
    )

    fulfilled = [
        projected_requests_by_id[request_id]
        for request_id in request_ids
        if projected_requests_by_id[request_id].get("status") == "fulfilled"
    ]
    fulfilled_request_ids = {str(item.get("request_id")) for item in fulfilled}
    failed_request_ids = set(failed_requests)
    require(
        not (fulfilled_request_ids & failed_request_ids),
        "a scoped request is recorded as both fulfilled and failed by this action",
        {"request_ids": sorted(fulfilled_request_ids & failed_request_ids)},
        "A fulfilled request has evidence; remove the contradictory attempt failure or the fulfilment.",
    )
    unaccounted = scoped_requests - fulfilled_request_ids - failed_request_ids
    require(
        not unaccounted,
        "delegated acquisition left scoped requests with neither a fulfilment nor a recorded attempt failure",
        {"request_ids": sorted(unaccounted)},
        (
            "Fulfil each scoped request, or record why the attempt produced nothing with "
            "source_requests.py record-attempt-failure using this action id."
        ),
    )
    require(
        all(item.get("source_id") for item in fulfilled),
        "fulfilled request lacks a manifest source id",
        {"request_ids": sorted(
            str(item.get("request_id")) for item in fulfilled if not item.get("source_id")
        )},
    )

    # --- delivered evidence -----------------------------------------------------------
    normalize_sources = load_sibling_module("normalize_sources")
    manifest_relative, normalized_relative = normalize_sources.source_paths(config)
    manifest_records = normalize_sources.load_manifest(project_root / manifest_relative)
    by_source_id = normalize_sources.records_by_source_id(manifest_records)
    current_manifest_fingerprints = record_fingerprint_snapshot(
        manifest_records,
        id_field="id",
        label="evidence manifest",
    )

    reuse_scope_failures = preexisting_reuse_scope_failures(
        fulfilled,
        by_source_id,
        scoped_requests,
        matching_source_records_before,
        set(reusable_source_ids_before),
        manifest_records_before,
        current_manifest_fingerprints,
    )
    require(
        not reuse_scope_failures,
        "a fulfilled request reuses pre-existing evidence that was not a scoped reconciliation match "
        "when the order was issued",
        {
            "reuse_scope_failures": reuse_scope_failures,
            "matching_source_ids_before": matching_source_ids_before,
            "reusable_source_ids_before": sorted(reusable_source_ids_before),
        },
        REUSE_SCOPE_REMEDIATION,
    )
    normalized_root = project_root / normalized_relative
    missing_normalized: list[str] = []
    unusable_normalized: list[dict[str, Any]] = []
    for request in fulfilled:
        source_id = str(request.get("source_id") or "")
        record = by_source_id.get(source_id)
        normalized_path = (
            normalize_sources.normalized_output_path_for_record(record, normalized_root)
            if isinstance(record, dict)
            else None
        )
        if not isinstance(normalized_path, Path) or not normalized_path.is_file():
            missing_normalized.append(source_id)
            continue
        quality_failure = normalized_source_quality_failure(project_root, normalized_path, record)
        if quality_failure is not None:
            unusable_normalized.append({"source_id": source_id, **quality_failure})
    require(
        not missing_normalized,
        "fulfilled source requests do not have normalized evidence",
        {"source_ids": missing_normalized},
    )
    require(
        not unusable_normalized,
        "fulfilled source requests do not have usable normalized evidence",
        {"quality_failures": unusable_normalized},
        (
            "Normalize the delivered source successfully before fulfillment; failed or stubbed records and "
            "PDFs without extracted text cannot satisfy a source request."
        ),
    )
    correlation_failures = delegated_fulfilment_correlation_failures(fulfilled, by_source_id)
    require(
        not correlation_failures,
        "fulfilled evidence is not linked to its source request by a provenance sidecar",
        {"correlation_failures": correlation_failures},
        (
            # The first sentence is the live repair, and it is live only for a source this
            # action delivered: its raw path and its manifest record are both new, so the
            # scope guards admit the restamp and the re-inventory. For a source the manifest
            # already held it is not, which the rest used to answer with a second delivery --
            # dead for the same reason every other escape on this path is, since this guard
            # also reads the acquirer's `fulfilled` list. Say so rather than sending the
            # acquirer to `fulfill` to find out.
            "Stamp request_id into each source's .provenance.yml as you deliver it, naming the request it "
            "fulfils, then re-run source_inventory.py before fulfilling. Raw evidence is immutable, so a "
            "source the manifest already holds cannot be given one afterwards, and a second delivery does "
            "not rescue it either: this request is already fulfilled by the source being refused and will "
            "not relink to one. A fresh delivery under its own raw path, with its own sidecar, is what a "
            "later order needs."
        ),
    )

    # --- question transitions ---------------------------------------------------------
    question_guard = recorded_postcondition("linked_blocked_questions_reopened")
    blocked_questions_before = question_guard.get("blocked_questions_before")
    require(
        valid_blocked_question_baseline(blocked_questions_before),
        "delegated acquisition work order lacks a valid blocked-question baseline",
        remediation="Start a fresh orchestration session; never infer question transitions after execution.",
    )
    fulfilled_by_request_id = {str(item.get("request_id")): item for item in fulfilled}
    durable_question_evidence = scoped_question_evidence_snapshot(
        project_root,
        config,
        list(blocked_questions_before),
    )
    # The page is frozen too, so what a reopen *did* is read from its claim projected onto
    # the frozen page -- exactly the edit the controller will commit.
    current_question_evidence = dict(durable_question_evidence)
    committed_question_slugs: set[str] = set()
    for slug, claim in reopen_claims.items():
        durable = durable_question_evidence.get(slug)
        if not isinstance(durable, dict):
            continue
        claimed_sources = [value for value in claim.get("source_ids", []) if isinstance(value, str)]
        projected_sources = sorted(set(durable.get("source_ids", [])) | set(claimed_sources))
        if durable.get("status") == "open" and not durable.get("blocking_request_ids"):
            # Already committed by an interrupted finalization; the page may differ from the
            # frozen baseline for this slug alone.
            committed_question_slugs.add(slug)
            continue
        current_question_evidence[slug] = {
            "status": "open",
            "blocking_request_ids": [],
            "source_ids": projected_sources,
        }
    # A question is unblocked only when every request blocking it was fulfilled. One
    # unfulfilled blocker — scoped and failed, or outside this order entirely — leaves the
    # question exactly as it was.
    fully_unblocked = {
        slug
        for slug, before in blocked_questions_before.items()
        if set(before.get("blocking_request_ids", [])) <= fulfilled_request_ids
        and before.get("blocking_request_ids")
    }
    unauthorized_reopen_claims = sorted(set(reopen_claims) - fully_unblocked)
    require(
        not unauthorized_reopen_claims,
        "delegated acquisition changed a question that was not fully unblocked by this action",
        {
            "question_slugs": unauthorized_reopen_claims,
            "fully_unblocked": sorted(fully_unblocked),
        },
        "Restore every question with a remaining unfulfilled blocking request; reopen only fully unblocked ones.",
    )
    question_transition_failures: list[dict[str, Any]] = []
    for slug in sorted(fully_unblocked):
        before = blocked_questions_before[slug]
        expected_source_ids = {
            str(fulfilled_by_request_id[request_id].get("source_id"))
            for request_id in before.get("blocking_request_ids", [])
        }
        current_question = current_question_evidence.get(slug, {})
        required_source_ids = set(before.get("source_ids_before", [])) | expected_source_ids
        if (
            current_question.get("status") != "open"
            or set(current_question.get("blocking_request_ids", []))
            or not required_source_ids <= set(current_question.get("source_ids", []))
        ):
            question_transition_failures.append(
                {
                    "question_slug": slug,
                    "before": before,
                    "after": current_question or None,
                    "expected_source_ids": sorted(required_source_ids),
                }
            )
    require(
        not question_transition_failures,
        "delegated acquisition did not reopen every fully unblocked question with its fulfilled source",
        {"question_transition_failures": question_transition_failures},
        (
            "Use question_resolve.py reopen for each question whose blocking requests were all fulfilled, so it "
            "is exactly open, carries the fulfilled source id, and has no remaining blocking links."
        ),
    )

    # --- bounded scope: nothing outside this action's outcomes may change --------------
    current_source_request_fingerprints = record_fingerprint_snapshot(
        all_requests,
        id_field="request_id",
        label="source-request store",
    )
    request_scope_violations = fingerprint_scope_violations(
        source_requests_before,
        current_source_request_fingerprints,
        # The store is frozen for the duration of the order: fulfilment is a claim the
        # controller commits, so nothing should have written here at all. The only
        # tolerated difference is a claim this controller already committed on an
        # interrupted finalization, which is a replay of its own write, not the acquirer's.
        mutable_ids=committed_request_ids,
    )
    require(
        not any(request_scope_violations.values()),
        "delegated acquisition changed source requests outside the fulfilled request scope",
        {"source_request_scope_violations": request_scope_violations},
        (
            "The request store is frozen while an order is pending. Fulfil through "
            "source_requests.py fulfill, which files a claim the controller commits; restore any "
            "request edited by hand."
        ),
    )
    current_question_files = question_file_fingerprint_snapshot(project_root, config)
    question_scope_violations = fingerprint_scope_violations(
        question_files_before,
        current_question_files,
        # Frozen on the same terms as the request store, and tolerant of the same replay.
        mutable_ids={f"{slug}.md" for slug in committed_question_slugs},
    )
    require(
        not any(question_scope_violations.values()),
        "delegated acquisition changed a question that was not fully unblocked by this action",
        {"question_scope_violations": question_scope_violations},
        (
            "Question pages are frozen while an order is pending. Reopen through "
            "question_resolve.py reopen, which files a claim the controller commits; restore any page "
            "edited by hand."
        ),
    )

    fulfilled_source_ids = {
        str(item.get("source_id"))
        for item in fulfilled
        if isinstance(item.get("source_id"), str) and item.get("source_id")
    }
    manifest_scope_violations = fingerprint_scope_violations(
        manifest_records_before,
        current_manifest_fingerprints,
        mutable_ids=set(),
        allowed_new_ids=fulfilled_source_ids,
    )
    require(
        not any(manifest_scope_violations.values()),
        "delegated acquisition changed, removed, or added evidence-manifest records outside fulfilled source scope",
        {
            "manifest_scope_violations": manifest_scope_violations,
            "fulfilled_source_ids": sorted(fulfilled_source_ids),
        },
        "Restore existing and out-of-scope manifest records; only fulfilled scoped sources may be appended.",
    )
    expected_new_source_ids = fulfilled_source_ids - set(manifest_records_before)
    actual_new_source_ids = set(current_manifest_fingerprints) - set(manifest_records_before)
    require(
        actual_new_source_ids == expected_new_source_ids,
        "fulfilled sources are not exactly accounted for by pre-existing matches or new manifest ids",
        {
            "expected_new_source_ids": sorted(expected_new_source_ids),
            "actual_new_source_ids": sorted(actual_new_source_ids),
            "matching_source_ids_before": matching_source_ids_before,
        },
    )
    # Both post-action snapshots are read *before* the reconciliation loop, which is the
    # only check in either arm that runs workspace code. What the acquirer left behind is
    # the state as the acquirer left it, never that state plus whatever verification did on
    # its way to reading it -- otherwise a normalizer adapter dropping one scratch file is
    # reported as the acquirer changing evidence it never touched, and the operator's
    # deletion of that file is undone by the very verification that flags it.
    current_normalized_files = normalized_file_fingerprint_snapshot(project_root, config)
    current_raw_tree = raw_tree_snapshot(project_root, config, include_entries=True)

    reconciliation_failures: list[dict[str, Any]] = []
    reused_unnormalized_source_ids = fulfilled_source_ids & set(reusable_source_ids_before)
    for source_id in sorted(fulfilled_source_ids & set(manifest_records_before)):
        failure = reused_source_reconciliation_failure(
            project_root,
            config,
            source_id,
            record=by_source_id.get(source_id),
            manifest_records=manifest_records,
            normalized_root=normalized_root,
            matching_source_records_before=matching_source_records_before,
            reusable_source_ids_before=reused_unnormalized_source_ids,
            manifest_records_before=manifest_records_before,
            current_record_fingerprint=current_manifest_fingerprints.get(source_id),
            normalized_files_before=normalized_files_before,
            normalize_sources=normalize_sources,
        )
        if failure is not None:
            reconciliation_failures.append(failure)
    require(
        not reconciliation_failures,
        "pre-existing fulfilled evidence is not an unchanged exact scoped reconciliation match",
        {"reconciliation_failures": reconciliation_failures},
        RECONCILIATION_REMEDIATION,
    )

    # A reused source the order recorded as not yet normalized owes exactly the record a
    # newly delivered source owes, so it joins the same set: its output becomes both allowed
    # and required. A reused source that *was* normalized at issuance stays out of it, or the
    # byte-identity its arm rests on would quietly become optional.
    allowed_new_normalized_paths, required_new_normalized_paths = normalized_output_scope(
        project_root,
        normalized_root,
        expected_new_source_ids | reused_unnormalized_source_ids,
        by_source_id,
        normalize_sources,
    )
    normalized_scope_violations = fingerprint_scope_violations(
        normalized_files_before,
        current_normalized_files,
        mutable_ids=set(),
        allowed_new_ids=allowed_new_normalized_paths,
    )
    require(
        not any(normalized_scope_violations.values()),
        "delegated acquisition changed normalized evidence outside newly fulfilled source scope",
        {"normalized_scope_violations": normalized_scope_violations},
        "Restore existing normalized evidence and keep new outputs limited to newly fulfilled "
        "sources and the structured-view sidecars their records declare.",
    )
    unexpected_new_normalized, missing_new_normalized = normalized_output_scope_failures(
        allowed_new_normalized_paths,
        required_new_normalized_paths,
        normalized_files_before,
        current_normalized_files,
    )
    require(
        not unexpected_new_normalized,
        "normalized outputs appeared that no newly fulfilled source authorizes",
        {
            "unexpected_new_normalized_paths": unexpected_new_normalized,
            "allowed_new_normalized_paths": sorted(allowed_new_normalized_paths),
        },
        "Remove normalized outputs no fulfilled source owns; a structured-view sidecar is "
        "allowed only when its record declares one.",
    )
    require(
        not missing_new_normalized,
        "newly fulfilled sources did not each produce a normalized record",
        {
            "missing_new_normalized_paths": missing_new_normalized,
            "actual_new_normalized_paths": sorted(
                set(current_normalized_files) - set(normalized_files_before)
            ),
        },
        MISSING_NORMALIZED_REMEDIATION,
    )

    before_raw_entries = raw_tree_before["entries"]
    current_raw_entries = current_raw_tree["entries"]
    raw_existing_changes = fingerprint_scope_violations(
        before_raw_entries,
        current_raw_entries,
        mutable_ids=set(),
        allowed_new_ids=set(current_raw_entries) - set(before_raw_entries),
    )
    # One inventory-derivation pass over the delivered tree answers both raw-scope
    # questions: whether each new record's raw_paths is what inventory itself derives
    # (refusing the hand-edited raw_paths route), and which snapshot entries those
    # records attribute (admitting a directory-shaped bundle's member files).
    if expected_new_source_ids:
        raw_attribution = derived_raw_attribution(
            project_root, config, memo_key=current_raw_tree.get("fingerprint")
        )
    else:
        raw_attribution = {}
    attribution_mismatches = raw_attribution_mismatches(
        raw_attribution,
        {source_id: by_source_id.get(source_id) for source_id in expected_new_source_ids},
    )
    require(
        not attribution_mismatches,
        "delegated acquisition manifest raw_paths do not match inventory-derived attribution",
        {"raw_attribution_mismatches": attribution_mismatches},
        RAW_ATTRIBUTION_REMEDIATION,
    )
    allowed_new_raw_paths = attributed_raw_paths(raw_attribution, expected_new_source_ids)
    actual_new_raw_paths = set(current_raw_entries) - set(before_raw_entries)
    unexpected_new_raw_paths = sorted(actual_new_raw_paths - allowed_new_raw_paths)
    require(
        not any(raw_existing_changes.values()) and not unexpected_new_raw_paths,
        "delegated acquisition changed raw evidence outside newly fulfilled manifest source scope",
        {
            "raw_scope_violations": raw_existing_changes,
            "unexpected_new_raw_paths": unexpected_new_raw_paths[:MAX_TRUSTED_STATIC_INPUT_DIFFERENCES],
            "allowed_new_raw_paths": sorted(allowed_new_raw_paths)[:MAX_TRUSTED_STATIC_INPUT_DIFFERENCES],
        },
        "Restore existing raw evidence and remove deliveries not referenced by newly fulfilled scoped sources.",
    )

    current_candidate_fingerprints = candidate_record_fingerprint_snapshot(
        load_candidates(project_root, config)
    )
    candidate_scope_violations = fingerprint_scope_violations(
        candidate_records_before,
        current_candidate_fingerprints,
        # Delegated acquisition never touches the candidate store: no candidate was
        # selected for it, so any change came from somewhere this order does not authorize.
        mutable_ids=set(),
    )
    require(
        not any(candidate_scope_violations.values()),
        "delegated acquisition changed candidate records",
        {"candidate_scope_violations": candidate_scope_violations},
        "Restore the candidate store; delegated acquisition has no candidate to transition.",
    )

    require(current in {"fetching", "evidence_ready"}, "delegated acquisition child run is in an invalid state")
    if apply_effects:
        commit_delegated_bookkeeping(
            project_root,
            config,
            fulfilment_claims,
            reopen_claims,
            committed_question_slugs,
        )
    if not fulfilled:
        # Every scoped request failed, and each failure is recorded. The action itself is
        # complete: the acquirer did what the order asked and proved it. Return to planning
        # so routing re-reads the audit and either retries within budget or retires the
        # session. The child run stays in `fetching`, which the next acquisition order
        # reuses idempotently.
        return "planning", (
            f"delegated acquisition recorded {len(failed_request_ids)} attempt failure(s) and no fulfilment"
        )
    if apply_effects and current == "fetching":
        controller.run_transition(project_root, child_args(run_id, session["agent_id"], to_state="evidence_ready"))
    return "research", None


def answered_grounded_slugs(export: dict[str, Any]) -> list[str]:
    """The answered questions that declared grounding, and so have something to verify.

    The test is that ``grounding`` is a non-empty list — never what an entry inside it
    holds. A claim anchored to a field of its source's structured view is grounded exactly
    as a quoted claim is, and a question grounded only by anchors must reach the verifier
    like any other; deciding here which forms count would put a second, quieter opinion
    about what grounding is next to the verifier's.
    """
    return [
        str(question.get("slug"))
        for question in export.get("questions", [])
        if isinstance(question, dict)
        and question.get("status") == "answered"
        and isinstance(question.get("slug"), str)
        and isinstance(question.get("grounding"), list)
        and bool(question["grounding"])
    ]


def empty_quote_verification_report() -> dict[str, Any]:
    """The grounding report persisted when no answered question declared any grounding.

    Shaped key for key like `verify_quotes.build_report`'s, so
    ``runs/<id>/evaluation/quote-verification.json`` has one schema whichever branch wrote
    it. A consumer reading ``counts.by_form`` to measure a workspace's migration off
    quote-only grounding must not have to discover that the file sometimes omits it,
    and read the omission as zero anchors rather than as no questions.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": timestamp_utc(),
        "network_io_executed": False,
        "questions": [],
        "counts": {
            "questions": 0,
            "grounding_entries": 0,
            "verified": 0,
            "failed": 0,
            "missing_grounding": 0,
            "by_form": {"quote": 0, "anchor": 0},
        },
        "overall_result": "verified",
    }


def verify_action_postconditions(
    project_root: Path,
    session: dict[str, Any],
    work_order: dict[str, Any],
    *,
    apply_effects: bool = False,
) -> tuple[str | None, str | None]:
    work_order = require_action_baselines(work_order, project_root)
    phase = work_order.get("phase")
    scope = work_order.get("scope") if isinstance(work_order.get("scope"), dict) else {}
    request_ids = [value for value in scope.get("request_ids", []) if isinstance(value, str)]
    config = load_config(project_root)
    status = fresh_workspace_status(project_root)
    verdict = status.get("readiness", {}).get("verdict")
    controller = load_sibling_module("run_controller")
    run_id = work_order.get("run_id")
    run_state = controller.load_run_state(project_root, run_id) if isinstance(run_id, str) else None
    current = run_state.get("state", {}).get("current") if isinstance(run_state, dict) else None

    def require(
        condition: bool,
        message: str,
        details: dict[str, Any] | None = None,
        remediation: str | None = None,
    ) -> None:
        if not condition:
            raise OrchestrationControllerError(
                "ORCHESTRATION_POSTCONDITION_FAILED",
                message,
                recoverable=True,
                remediation=remediation or "Complete the persisted work order and resubmit the same action id.",
                details=details,
            )

    def recorded_postcondition(check: str) -> dict[str, Any]:
        return next(
            (
                item
                for item in work_order.get("required_postconditions", [])
                if isinstance(item, dict) and item.get("check") == check
            ),
            {},
        )

    def require_raw_unchanged() -> None:
        expected = recorded_postcondition("raw_tree_unchanged").get("before")
        actual = raw_tree_snapshot(project_root, config)
        require(
            isinstance(expected, dict) and actual == expected,
            f"{phase} changed the immutable raw evidence tree",
            {"before": expected, "after": actual},
            "Restore raw/ to its pre-action state; discovery and review may only mutate the candidate store.",
        )

    if phase == "research":
        require(
            verdict in {"in_progress", "blocked_on_sources", "complete"},
            "research produced an invalid readiness verdict",
        )
        research_guard = recorded_postcondition("workspace_readiness_changed")
        before_questions = research_guard.get("scoped_questions_before")
        question_files_before = research_guard.get("question_file_fingerprints_before")
        require(
            isinstance(before_questions, dict)
            and bool(before_questions)
            and valid_question_file_fingerprint_snapshot(question_files_before),
            "research work order lacks a scoped question baseline",
            remediation=(
                "This legacy pending action cannot be rebound safely. Preserve it for inspection and start a "
                "fresh orchestration session after upgrading the workspace."
            ),
        )
        scoped_slugs = [value for value in scope.get("question_slugs", []) if isinstance(value, str)]
        current_question_files = question_file_fingerprint_snapshot(project_root, config)
        question_scope_violations = fingerprint_scope_violations(
            question_files_before,
            current_question_files,
            mutable_ids={f"{slug}.md" for slug in scoped_slugs},
        )
        require(
            not any(question_scope_violations.values()),
            "research changed a question file outside the persisted work-order scope",
            {"question_scope_violations": question_scope_violations},
            "Restore every out-of-scope question file and process only question slugs named by this work order.",
        )
        source_requests_before = research_guard.get("source_request_record_fingerprints_before")
        require(
            valid_record_fingerprint_snapshot(source_requests_before),
            "research work order lacks a source-request integrity baseline",
            remediation="Start a fresh orchestration session; never infer source-request creation after execution.",
        )
        source_requests = load_sibling_module("source_requests")
        current_source_request_records = source_requests.load_requests(
            source_requests.requests_path(project_root, config)
        )
        current_source_request_fingerprints = record_fingerprint_snapshot(
            current_source_request_records,
            id_field="request_id",
            label="source-request store",
        )
        scoped_slug_set = set(scoped_slugs)
        allowed_new_request_ids = {
            str(record.get("request_id"))
            for record in current_source_request_records
            if isinstance(record.get("request_id"), str)
            and record.get("request_id") not in source_requests_before
            and record.get("status") == "open"
            and isinstance(record.get("question_slugs"), list)
            and bool(record.get("question_slugs"))
            and {
                slug for slug in record.get("question_slugs", []) if isinstance(slug, str)
            }
            <= scoped_slug_set
        }
        request_scope_violations = fingerprint_scope_violations(
            source_requests_before,
            current_source_request_fingerprints,
            mutable_ids=set(),
            allowed_new_ids=allowed_new_request_ids,
        )
        require(
            not any(request_scope_violations.values()),
            "research changed source requests outside append-only scoped-question creation",
            {"source_request_scope_violations": request_scope_violations},
            "Restore existing requests and keep each new open request linked only to scoped questions.",
        )
        after_questions = scoped_question_snapshot(project_root, config, scoped_slugs)
        terminal_statuses = {"answered", "human_review", "blocked", "deferred", "rejected"}
        progressed_slugs = sorted(
            slug
            for slug, before in before_questions.items()
            if isinstance(before, dict)
            and before.get("status") in {"open", "in_progress"}
            and after_questions.get(slug, {}).get("status") in terminal_statuses
        )
        require(
            bool(progressed_slugs),
            "research completed without terminally processing a scoped question",
            {
                "question_slugs": scoped_slugs,
                "before": before_questions,
                "after": after_questions,
            },
            (
                "Claim and resolve at least one scoped question as answered, blocked, deferred, or rejected; "
                "a claim-only or unchanged backlog is not completed research."
            ),
        )
        invalid_blocked_links: list[dict[str, Any]] = []
        blocked_progressed_slugs = [
            slug for slug in progressed_slugs if after_questions.get(slug, {}).get("status") == "blocked"
        ]
        if blocked_progressed_slugs:
            requests_by_id = {
                str(item.get("request_id")): item
                for item in current_source_request_records
                if isinstance(item, dict) and isinstance(item.get("request_id"), str)
            }
        else:
            requests_by_id = {}
        for slug in blocked_progressed_slugs:
            question = after_questions.get(slug, {})
            linked_ids = question.get("blocking_request_ids", [])
            valid_ids = [
                request_id
                for request_id in linked_ids
                if isinstance(request_id, str)
                and isinstance(requests_by_id.get(request_id), dict)
                and requests_by_id[request_id].get("status") == "open"
                and slug in requests_by_id[request_id].get("question_slugs", [])
            ]
            if not valid_ids:
                invalid_blocked_links.append(
                    {"question_slug": slug, "blocking_request_ids": list(linked_ids)}
                )
        require(
            not invalid_blocked_links,
            "blocked research questions lack open request artifacts linked to the same scoped question",
            {"invalid_blocked_links": invalid_blocked_links},
            (
                "Create a structured source request for each blocked scoped question, then block the question "
                "with that request id before resubmitting."
            ),
        )
        if verdict == "blocked_on_sources":
            require(
                current in {"answering", "blocked_on_sources"},
                "research child run is not answering or durably blocked",
            )
            if apply_effects and current == "answering":
                finish_active_child(project_root, session, "blocked_on_sources")
            elif apply_effects:
                session["active_run_id"] = None
            return "planning", None
        if verdict == "complete":
            require(current in {"answering", "verifying"}, "research child run cannot advance to verification")
            if apply_effects and current == "answering":
                controller.run_transition(project_root, child_args(run_id, session["agent_id"], to_state="verifying"))
            return "verification", None
        require(current == "answering", "research child run is no longer in answering state")
        return "research", None

    if phase == "discovery":
        all_candidates = load_candidates(project_root, config)
        candidates = [
            candidate
            for candidate in all_candidates
            if candidate_request_id(candidate) in set(request_ids)
        ]
        candidate_guard = recorded_postcondition("request_scoped_candidates_increased")
        before_candidates = int(candidate_guard.get("before", 0) or 0)
        candidate_states_before = candidate_guard.get("candidate_states_before")
        candidate_records_before = candidate_guard.get("candidate_record_fingerprints_before")
        require(
            valid_candidate_state_baseline(candidate_states_before)
            and valid_record_fingerprint_snapshot(candidate_records_before)
            and set(candidate_states_before) <= set(candidate_records_before)
            and len(candidate_states_before) == before_candidates,
            "discovery work order lacks a valid bounded candidate-state baseline",
            remediation="Start a fresh orchestration session; never infer candidate creation after execution.",
        )
        current_candidate_records = candidate_record_fingerprint_snapshot(all_candidates)
        new_in_scope_ids = {
            str(candidate.get("candidate_id"))
            for candidate in candidates
            if isinstance(candidate.get("candidate_id"), str)
            and candidate.get("candidate_id") not in candidate_records_before
        }
        discovery_scope_violations = fingerprint_scope_violations(
            candidate_records_before,
            current_candidate_records,
            mutable_ids=set(),
            allowed_new_ids=new_in_scope_ids,
        )
        require(
            not any(discovery_scope_violations.values()),
            "discovery changed candidate records outside append-only request scope",
            {"candidate_scope_violations": discovery_scope_violations},
            "Restore all pre-existing and out-of-scope candidates; discovery may only append scoped candidates.",
        )
        current_candidate_states = request_candidate_state_snapshot(candidates, request_ids)
        historical_candidate_changes = {
            candidate_id: {"before": state, "after": current_candidate_states.get(candidate_id)}
            for candidate_id, state in candidate_states_before.items()
            if current_candidate_states.get(candidate_id) != state
        }
        require(
            not historical_candidate_changes,
            "discovery changed or removed a candidate that existed before the action",
            {"historical_candidate_changes": historical_candidate_changes},
            "Restore the pre-action candidate records; discovery may append new candidates but not review old ones.",
        )
        require(
            len(current_candidate_states) > before_candidates,
            "discovery produced no new request-scoped candidate",
            {"request_ids": request_ids, "before": before_candidates, "after": len(current_candidate_states)},
            "Refine the source request or enable a different composable discovery/acquisition provider pair.",
        )
        before_manifest = int(recorded_postcondition("discovery_never_fetches").get("manifest_records_before", 0) or 0)
        current_manifest = int(status.get("sources", {}).get("manifest_records", 0) or 0)
        require(current_manifest == before_manifest, "discovery changed the evidence manifest")
        before_manifest_digest = recorded_postcondition("discovery_never_fetches").get("manifest_digest_before")
        require(
            evidence_manifest_digest(project_root) == before_manifest_digest,
            "discovery changed existing evidence-manifest content",
        )
        require_raw_unchanged()
        acquisition = work_order.get("provider_policy", {}).get("acquisition", {})
        enabled_acquisition = set(acquisition.get("providers", [])) if acquisition.get("enabled") is True else set()
        discovery = work_order.get("provider_policy", {}).get("discovery", {})
        enabled_discovery = set(discovery.get("providers", [])) if discovery.get("enabled") is True else set()
        newly_appended = [
            candidate
            for candidate in candidates
            if isinstance(candidate.get("candidate_id"), str)
            and candidate.get("candidate_id") not in candidate_records_before
        ]
        invalid_new_candidates = [
            {
                "candidate_id": candidate.get("candidate_id"),
                "state": candidate_state(candidate),
                "provider": candidate_provider(candidate),
            }
            for candidate in newly_appended
            if candidate_state(candidate) not in DISCOVERY_APPEND_CANDIDATE_STATES
            or not candidate_uses_permitted_discovery_provider(candidate, enabled_discovery)
        ]
        require(
            not invalid_new_candidates,
            "discovery appended candidates outside the enabled discovery-provider policy",
            {"invalid_new_candidates": invalid_new_candidates},
            "Remove candidates from disabled providers or invalid lifecycle states and replay scoped discovery.",
        )
        eligible_new = eligible_new_discovery_candidates(
            candidates,
            request_ids,
            candidate_states_before,
            enabled_discovery,
            enabled_acquisition,
        )
        require(
            bool(eligible_new),
            "discovery produced no newly added, reviewable candidate through permitted end-to-end providers",
            {
                "request_ids": request_ids,
                "new_candidate_ids": sorted(set(current_candidate_states) - set(candidate_states_before)),
                "enabled_discovery_providers": sorted(enabled_discovery),
                "enabled_acquisition_providers": sorted(enabled_acquisition),
            },
            "Enable a matching acquisition provider, refine discovery, or deliver reviewed evidence manually.",
        )
        require(current in {"discovering", "candidates_ready"}, "discovery child run is in an invalid state")
        if apply_effects and current == "discovering":
            controller.run_transition(project_root, child_args(run_id, session["agent_id"], to_state="candidates_ready"))
        return "candidate_review", None

    if phase == "candidate_review":
        before_manifest = int(recorded_postcondition("selection_does_not_fetch").get("manifest_records_before", 0) or 0)
        current_manifest = int(status.get("sources", {}).get("manifest_records", 0) or 0)
        require(current_manifest == before_manifest, "candidate review changed the evidence manifest")
        before_manifest_digest = recorded_postcondition("selection_does_not_fetch").get("manifest_digest_before")
        require(
            evidence_manifest_digest(project_root) == before_manifest_digest,
            "candidate review changed existing evidence-manifest content",
        )
        require_raw_unchanged()
        require(current == "candidates_ready", "candidate-review child run is not in candidates_ready state")
        candidate_ids = [value for value in scope.get("candidate_ids", []) if isinstance(value, str)]
        selection_guard = recorded_postcondition("selected_candidate_for_request")
        selected_candidate_ids_before = selection_guard.get("selected_candidate_ids_before")
        candidate_records_before = selection_guard.get("candidate_record_fingerprints_before")
        selected_before = selection_guard.get("selected_before")
        require(
            valid_scope_id_list(selected_candidate_ids_before)
            and valid_record_fingerprint_snapshot(candidate_records_before)
            and set(selected_candidate_ids_before) <= set(candidate_records_before)
            and isinstance(selected_before, int)
            and not isinstance(selected_before, bool)
            and selected_before == len(selected_candidate_ids_before),
            "candidate-review work order lacks a valid selected-candidate baseline",
            remediation="Start a fresh orchestration session; never infer selection changes after execution.",
        )
        current_candidate_records = candidate_record_fingerprint_snapshot(load_candidates(project_root, config))
        review_scope_violations = fingerprint_scope_violations(
            candidate_records_before,
            current_candidate_records,
            mutable_ids=set(candidate_ids),
        )
        require(
            not any(review_scope_violations.values()),
            "candidate review changed records outside the persisted candidate scope",
            {"candidate_scope_violations": review_scope_violations},
            "Restore every out-of-scope candidate and review only candidate ids named by this work order.",
        )
        selected = selected_candidates_for_scope(project_root, config, request_ids, candidate_ids)
        historical_selected = set(selected_candidate_ids_before)
        newly_selected = [
            candidate
            for candidate in selected
            if candidate.get("candidate_id") not in historical_selected
        ]
        selected_outside_scope = selected_candidates_outside_scope(
            project_root,
            config,
            request_ids,
            candidate_ids,
            selected_candidate_ids_before,
        )
        require(
            not selected_outside_scope,
            "candidate review selected a candidate outside the persisted work-order scope",
            {
                "scoped_candidate_ids": candidate_ids,
                "out_of_scope_candidate_ids": sorted(
                    str(candidate.get("candidate_id"))
                    for candidate in selected_outside_scope
                    if candidate.get("candidate_id")
                ),
            },
            "Reject or defer out-of-scope selections and select only a candidate id named by this work order.",
        )
        require(
            bool(newly_selected),
            "candidate review did not newly select a candidate from the persisted work-order scope",
        )
        policy = provider_policy(config)
        enabled = set(policy["acquisition"]["providers"]) if policy["acquisition"]["enabled"] else set()
        routable = [candidate for candidate in newly_selected if acquisition_route(candidate, enabled) is not None]
        require(
            bool(routable),
            "selected candidates have no explicitly enabled acquisition route",
            {"request_ids": request_ids, "enabled_acquisition_providers": sorted(enabled)},
        )
        return "acquisition", None

    if phase == "acquisition":
        if work_order.get("acquisition_mode") == ACQUISITION_MODE_DELEGATED:
            return verify_delegated_acquisition_postconditions(
                project_root,
                session,
                work_order,
                config=config,
                status=status,
                run_id=run_id,
                current=current,
                controller=controller,
                apply_effects=apply_effects,
            )
        requests = open_requests(project_root, config)
        still_open = {str(item.get("request_id")) for item in requests}
        require(not set(request_ids) & still_open, "acquisition did not fulfill the scoped source request")
        source_requests = load_sibling_module("source_requests")
        all_requests = source_requests.load_requests(source_requests.requests_path(project_root, config))
        fulfilled = [
            item
            for item in all_requests
            if item.get("request_id") in set(request_ids) and item.get("status") == "fulfilled"
        ]
        require(
            bool(fulfilled) and all(item.get("source_id") for item in fulfilled),
            "fulfilled request lacks a manifest source id",
        )
        normalize_sources = load_sibling_module("normalize_sources")
        manifest_relative, normalized_relative = normalize_sources.source_paths(config)
        manifest_records = normalize_sources.load_manifest(project_root / manifest_relative)
        by_source_id = normalize_sources.records_by_source_id(manifest_records)
        current_manifest_fingerprints = record_fingerprint_snapshot(
            manifest_records,
            id_field="id",
            label="evidence manifest",
        )
        normalized_root = project_root / normalized_relative
        missing_normalized: list[str] = []
        unusable_normalized: list[dict[str, Any]] = []
        for request in fulfilled:
            source_id = str(request.get("source_id") or "")
            record = by_source_id.get(source_id)
            normalized_path = (
                normalize_sources.normalized_output_path_for_record(record, normalized_root)
                if isinstance(record, dict)
                else None
            )
            if not isinstance(normalized_path, Path) or not normalized_path.is_file():
                missing_normalized.append(source_id)
                continue
            quality_failure = normalized_source_quality_failure(project_root, normalized_path, record)
            if quality_failure is not None:
                unusable_normalized.append({"source_id": source_id, **quality_failure})
        require(
            not missing_normalized,
            "fulfilled source requests do not have normalized evidence",
            {"source_ids": missing_normalized},
        )
        require(
            not unusable_normalized,
            "fulfilled source requests do not have usable normalized evidence",
            {"quality_failures": unusable_normalized},
            (
                "Normalize the acquired source successfully before fulfillment; failed or stubbed records and "
                "PDFs without extracted text cannot satisfy a source request."
            ),
        )
        scoped_candidate_ids = {
            value for value in scope.get("candidate_ids", []) if isinstance(value, str) and value
        }
        manifest_guard = recorded_postcondition("manifest_records_increased")
        before_manifest = int(manifest_guard.get("before", 0) or 0)
        matching_source_ids_before = manifest_guard.get("matching_source_ids_before")
        matching_source_records_before = manifest_guard.get("matching_source_records_before")
        # Additive and optional here for the same reason as in the delegated arm: an order
        # issued before un-normalized reuse existed replays with it simply unavailable.
        reusable_source_ids_before = manifest_guard.get("reusable_source_ids_before", [])
        manifest_records_before = manifest_guard.get("manifest_record_fingerprints_before")
        raw_tree_before = manifest_guard.get("raw_tree_before")
        candidate_records_before = manifest_guard.get("candidate_record_fingerprints_before")
        source_requests_before = manifest_guard.get("source_request_record_fingerprints_before")
        normalized_files_before = manifest_guard.get("normalized_file_fingerprints_before")
        question_files_before = manifest_guard.get("question_file_fingerprints_before")
        require(
            valid_scope_id_list(matching_source_ids_before)
            and valid_matching_source_record_snapshot(matching_source_records_before)
            and set(matching_source_ids_before) == set(matching_source_records_before)
            and valid_record_fingerprint_snapshot(manifest_records_before)
            and set(matching_source_ids_before) <= set(manifest_records_before)
            and valid_unnormalized_reuse_baseline(
                reusable_source_ids_before, matching_source_ids_before, manifest_records_before
            )
            and before_manifest == len(manifest_records_before)
            and valid_raw_tree_snapshot(raw_tree_before, include_entries=True)
            and valid_record_fingerprint_snapshot(candidate_records_before)
            and valid_record_fingerprint_snapshot(source_requests_before)
            and valid_file_fingerprint_snapshot(normalized_files_before, prefix="sources/")
            and valid_question_file_fingerprint_snapshot(question_files_before),
            "acquisition work order lacks a valid bounded evidence integrity baseline",
            remediation="Start a fresh orchestration session; never infer matching evidence after execution.",
        )
        # The same refusal the delegated arm gives, in the same words, from the same
        # function -- and, like there, before anything else can speak about a reused source.
        # Two of this refusal's three causes are also seen by later guards: candidate
        # correlation refuses a sidecar naming no scoped request, and the manifest-scope
        # guard refuses a record rewritten since issuance. Whichever speaks first is the one
        # the acquirer acts on, and only this one names reuse, says which of the three
        # causes applies to which source, and carries the repair that cause actually has.
        reuse_scope_failures = preexisting_reuse_scope_failures(
            fulfilled,
            by_source_id,
            {value for value in scope.get("request_ids", []) if isinstance(value, str)},
            matching_source_records_before,
            set(reusable_source_ids_before),
            manifest_records_before,
            current_manifest_fingerprints,
            scoped_candidate_ids,
        )
        require(
            not reuse_scope_failures,
            "a fulfilled request reuses pre-existing evidence that was not a scoped reconciliation match "
            "when the order was issued",
            {
                "reuse_scope_failures": reuse_scope_failures,
                "matching_source_ids_before": matching_source_ids_before,
                "reusable_source_ids_before": sorted(reusable_source_ids_before),
            },
            PROVIDER_REUSE_SCOPE_REMEDIATION,
        )
        all_candidates = load_candidates(project_root, config)
        candidates_by_id = {
            str(candidate.get("candidate_id")): candidate
            for candidate in all_candidates
            if isinstance(candidate.get("candidate_id"), str)
        }
        correlation_failures: list[dict[str, Any]] = []
        for request in fulfilled:
            request_id = str(request.get("request_id") or "")
            source_id = str(request.get("source_id") or "")
            record = by_source_id.get(source_id)
            provenance = record.get("provenance") if isinstance(record, dict) else None
            provenance_request_id = provenance.get("request_id") if isinstance(provenance, dict) else None
            provenance_candidate_id = provenance.get("candidate_id") if isinstance(provenance, dict) else None
            candidate = (
                candidates_by_id.get(provenance_candidate_id)
                if isinstance(provenance_candidate_id, str)
                else None
            )
            if (
                provenance_request_id != request_id
                or provenance_candidate_id not in scoped_candidate_ids
                or not isinstance(candidate, dict)
                or candidate_request_id(candidate) != request_id
                or candidate_state(candidate) != "fetched"
                or candidate.get("fetched_source_id") != source_id
            ):
                correlation_failures.append(
                    {
                        "request_id": request_id,
                        "source_id": source_id,
                        "provenance_request_id": provenance_request_id,
                        "provenance_candidate_id": provenance_candidate_id,
                        "candidate_state": candidate_state(candidate) if isinstance(candidate, dict) else None,
                        "candidate_source_id": candidate.get("fetched_source_id")
                        if isinstance(candidate, dict)
                        else None,
                    }
                )
        require(
            not correlation_failures,
            "acquired evidence is not linked from scoped request and candidate provenance to fetched source state",
            {
                "scoped_candidate_ids": sorted(scoped_candidate_ids),
                "correlation_failures": correlation_failures,
            },
            (
                "Acquire a scoped selected candidate with both --request-id and --candidate-id, inventory and "
                "normalize it, then transition that candidate to fetched with the fulfilled manifest source id."
            ),
        )
        fulfilled_by_request_id = {
            str(item.get("request_id")): item
            for item in fulfilled
            if isinstance(item.get("request_id"), str)
        }
        question_guard = recorded_postcondition("linked_blocked_questions_reopened")
        blocked_questions_before = question_guard.get("blocked_questions_before")
        require(
            valid_blocked_question_baseline(blocked_questions_before),
            "acquisition work order lacks a valid blocked-question baseline",
            remediation="Start a fresh orchestration session; never infer question transitions after execution.",
        )
        current_question_evidence = scoped_question_evidence_snapshot(
            project_root,
            config,
            list(blocked_questions_before),
        )
        question_transition_failures: list[dict[str, Any]] = []
        for slug, before in blocked_questions_before.items():
            linked_request_ids = list(before.get("blocking_request_ids", []))
            linked_fulfilled = [
                fulfilled_by_request_id.get(request_id) for request_id in linked_request_ids
            ]
            expected_source_ids = {
                str(request.get("source_id"))
                for request in linked_fulfilled
                if isinstance(request, dict) and isinstance(request.get("source_id"), str)
            }
            current_question = current_question_evidence.get(slug, {})
            current_source_ids = set(current_question.get("source_ids", []))
            current_blocking_ids = set(current_question.get("blocking_request_ids", []))
            required_source_ids = set(before.get("source_ids_before", [])) | expected_source_ids
            if (
                any(not isinstance(request, dict) for request in linked_fulfilled)
                or current_question.get("status") != "open"
                or current_blocking_ids
                or not required_source_ids <= current_source_ids
            ):
                question_transition_failures.append(
                    {
                        "question_slug": slug,
                        "before": before,
                        "after": current_question or None,
                        "expected_source_ids": sorted(required_source_ids),
                        "fulfilled_request_ids": sorted(
                            request_id
                            for request_id in linked_request_ids
                            if isinstance(fulfilled_by_request_id.get(request_id), dict)
                        ),
                    }
                )
        require(
            not question_transition_failures,
            "acquisition did not reopen every scoped blocked question with fulfilled request/source linkage",
            {"question_transition_failures": question_transition_failures},
            (
                "Fulfill each scoped request, then use question_resolve.py reopen so every baseline-blocked "
                "question is exactly open, has the fulfilled source id, and has no remaining blocking links."
            ),
        )
        linked_question_slugs = {
            str(slug)
            for request in fulfilled
            for slug in request.get("question_slugs", [])
            if isinstance(slug, str) and slug
        }
        blocked_slugs = set(status.get("questions", {}).get("blocked_slugs", []))
        require(
            not linked_question_slugs & blocked_slugs,
            "questions linked to fulfilled evidence remain blocked",
            {"question_slugs": sorted(linked_question_slugs & blocked_slugs)},
        )
        current_source_request_fingerprints = record_fingerprint_snapshot(
            all_requests,
            id_field="request_id",
            label="source-request store",
        )
        request_scope_violations = fingerprint_scope_violations(
            source_requests_before,
            current_source_request_fingerprints,
            mutable_ids=set(request_ids),
        )
        require(
            not any(request_scope_violations.values()),
            "acquisition changed source requests outside the persisted request scope",
            {"source_request_scope_violations": request_scope_violations},
            "Restore every out-of-scope request and fulfill only request ids named by this work order.",
        )
        current_question_files = question_file_fingerprint_snapshot(project_root, config)
        acquisition_question_scope_violations = fingerprint_scope_violations(
            question_files_before,
            current_question_files,
            mutable_ids={f"{slug}.md" for slug in scope.get("question_slugs", []) if isinstance(slug, str)},
        )
        require(
            not any(acquisition_question_scope_violations.values()),
            "acquisition changed question files outside the persisted question scope",
            {"question_scope_violations": acquisition_question_scope_violations},
            "Restore every out-of-scope question and reopen only questions named by this work order.",
        )
        fulfilled_source_ids = {
            str(item.get("source_id"))
            for item in fulfilled
            if isinstance(item.get("source_id"), str) and item.get("source_id")
        }
        manifest_scope_violations = fingerprint_scope_violations(
            manifest_records_before,
            current_manifest_fingerprints,
            mutable_ids=set(),
            allowed_new_ids=fulfilled_source_ids,
        )
        require(
            not any(manifest_scope_violations.values()),
            "acquisition changed, removed, or added evidence-manifest records outside fulfilled source scope",
            {
                "manifest_scope_violations": manifest_scope_violations,
                "fulfilled_source_ids": sorted(fulfilled_source_ids),
            },
            "Restore existing and out-of-scope manifest records; only fulfilled scoped sources may be appended.",
        )
        expected_new_source_ids = fulfilled_source_ids - set(manifest_records_before)
        actual_new_source_ids = set(current_manifest_fingerprints) - set(manifest_records_before)
        require(
            actual_new_source_ids == expected_new_source_ids,
            "fulfilled sources are not exactly accounted for by pre-existing matches or new manifest ids",
            {
                "expected_new_source_ids": sorted(expected_new_source_ids),
                "actual_new_source_ids": sorted(actual_new_source_ids),
                "matching_source_ids_before": matching_source_ids_before,
            },
        )
        preexisting_fulfilled = fulfilled_source_ids & set(manifest_records_before)
        # Both post-action snapshots are read before the reconciliation loop, for the
        # reason the delegated arm states: the loop is the one check that runs workspace
        # code, and what it does on its way to an answer must not be folded into what the
        # acquirer is held to.
        current_normalized_files = normalized_file_fingerprint_snapshot(project_root, config)
        current_raw_tree = raw_tree_snapshot(project_root, config, include_entries=True)
        reconciliation_failures: list[dict[str, Any]] = []
        reused_unnormalized_source_ids = fulfilled_source_ids & set(reusable_source_ids_before)
        for source_id in sorted(preexisting_fulfilled):
            failure = reused_source_reconciliation_failure(
                project_root,
                config,
                source_id,
                record=by_source_id.get(source_id),
                manifest_records=manifest_records,
                normalized_root=normalized_root,
                matching_source_records_before=matching_source_records_before,
                reusable_source_ids_before=reused_unnormalized_source_ids,
                manifest_records_before=manifest_records_before,
                current_record_fingerprint=current_manifest_fingerprints.get(source_id),
                normalized_files_before=normalized_files_before,
                normalize_sources=normalize_sources,
            )
            if failure is not None:
                reconciliation_failures.append(failure)
        require(
            not reconciliation_failures,
            "pre-existing fulfilled evidence is not an unchanged exact scoped reconciliation match",
            {"reconciliation_failures": reconciliation_failures},
            PROVIDER_RECONCILIATION_REMEDIATION,
        )

        # Same terms as the delegated arm: a reused source the order recorded as not yet
        # normalized owes exactly the record a newly acquired source owes, and one that was
        # already normalized authorizes no new file at all.
        allowed_new_normalized_paths, required_new_normalized_paths = normalized_output_scope(
            project_root,
            normalized_root,
            expected_new_source_ids | reused_unnormalized_source_ids,
            by_source_id,
            normalize_sources,
        )
        normalized_scope_violations = fingerprint_scope_violations(
            normalized_files_before,
            current_normalized_files,
            mutable_ids=set(),
            allowed_new_ids=allowed_new_normalized_paths,
        )
        require(
            not any(normalized_scope_violations.values()),
            "acquisition changed normalized evidence outside newly fulfilled source scope",
            {"normalized_scope_violations": normalized_scope_violations},
            "Restore existing normalized evidence and keep new outputs limited to newly fulfilled "
            "sources and the structured-view sidecars their records declare.",
        )
        unexpected_new_normalized, missing_new_normalized = normalized_output_scope_failures(
            allowed_new_normalized_paths,
            required_new_normalized_paths,
            normalized_files_before,
            current_normalized_files,
        )
        require(
            not unexpected_new_normalized,
            "normalized outputs appeared that no newly fulfilled source authorizes",
            {
                "unexpected_new_normalized_paths": unexpected_new_normalized,
                "allowed_new_normalized_paths": sorted(allowed_new_normalized_paths),
            },
            "Remove normalized outputs no fulfilled source owns; a structured-view sidecar is "
            "allowed only when its record declares one.",
        )
        require(
            not missing_new_normalized,
            "newly fulfilled sources did not each produce a normalized record",
            {
                "missing_new_normalized_paths": missing_new_normalized,
                "actual_new_normalized_paths": sorted(
                    set(current_normalized_files) - set(normalized_files_before)
                ),
            },
            MISSING_NORMALIZED_REMEDIATION,
        )

        before_raw_entries = raw_tree_before["entries"]
        current_raw_entries = current_raw_tree["entries"]
        raw_existing_changes = fingerprint_scope_violations(
            before_raw_entries,
            current_raw_entries,
            mutable_ids=set(),
            allowed_new_ids=set(current_raw_entries) - set(before_raw_entries),
        )
        # Same predicate as the delegated arm, from the same single derivation pass:
        # raw_paths must be inventory-derived, and attribution decides admission.
        if expected_new_source_ids:
            raw_attribution = derived_raw_attribution(
                project_root, config, memo_key=current_raw_tree.get("fingerprint")
            )
        else:
            raw_attribution = {}
        attribution_mismatches = raw_attribution_mismatches(
            raw_attribution,
            {source_id: by_source_id.get(source_id) for source_id in expected_new_source_ids},
        )
        require(
            not attribution_mismatches,
            "acquisition manifest raw_paths do not match inventory-derived attribution",
            {"raw_attribution_mismatches": attribution_mismatches},
            RAW_ATTRIBUTION_REMEDIATION,
        )
        allowed_new_raw_paths = attributed_raw_paths(raw_attribution, expected_new_source_ids)
        actual_new_raw_paths = set(current_raw_entries) - set(before_raw_entries)
        unexpected_new_raw_paths = sorted(actual_new_raw_paths - allowed_new_raw_paths)
        require(
            not any(raw_existing_changes.values()) and not unexpected_new_raw_paths,
            "acquisition changed raw evidence outside newly fulfilled manifest source scope",
            {
                "raw_scope_violations": raw_existing_changes,
                "unexpected_new_raw_paths": unexpected_new_raw_paths[:MAX_TRUSTED_STATIC_INPUT_DIFFERENCES],
                "allowed_new_raw_paths": sorted(allowed_new_raw_paths)[:MAX_TRUSTED_STATIC_INPUT_DIFFERENCES],
            },
            "Restore existing raw evidence and remove deliveries not referenced by newly fulfilled scoped sources.",
        )

        current_candidate_fingerprints = candidate_record_fingerprint_snapshot(all_candidates)
        candidate_scope_violations = fingerprint_scope_violations(
            candidate_records_before,
            current_candidate_fingerprints,
            mutable_ids=scoped_candidate_ids,
        )
        require(
            not any(candidate_scope_violations.values()),
            "acquisition changed candidate records outside the persisted candidate scope",
            {"candidate_scope_violations": candidate_scope_violations},
            "Restore every out-of-scope candidate and transition only the scoped candidate to fetched.",
        )
        require(current in {"fetching", "evidence_ready"}, "acquisition child run is in an invalid state")
        if apply_effects and current == "fetching":
            controller.run_transition(project_root, child_args(run_id, session["agent_id"], to_state="evidence_ready"))
        return "research", None

    if phase == "verification":
        require(current in {"verifying", "complete"}, "verification child run is in an invalid state")
        evaluation_dir = project_root / "runs" / str(run_id) / "evaluation"
        expected_relative_paths = [
            f"runs/{run_id}/evaluation/citation-verification.json",
            f"runs/{run_id}/evaluation/export.json",
            f"runs/{run_id}/evaluation/lint.json",
            f"runs/{run_id}/evaluation/publication-readiness.json",
        ]
        bundle_postcondition = recorded_postcondition("fresh_verification_bundle")
        recorded_paths = bundle_postcondition.get("paths")
        require(
            isinstance(recorded_paths, list) and recorded_paths == expected_relative_paths,
            "verification work order does not name the canonical bundle artifacts",
        )
        actual_digests = {
            path: file_digest(project_root / path, containment_root=project_root)
            for path in expected_relative_paths
        }
        missing_bundle = [path for path, digest in actual_digests.items() if digest is None]
        require(
            not missing_bundle,
            "fresh verification bundle is incomplete",
            {"missing_artifacts": missing_bundle},
        )
        before_digests = bundle_postcondition.get("before")
        if isinstance(before_digests, dict) and any(value is not None for value in before_digests.values()):
            require(
                any(actual_digests.get(path) != before_digests.get(path) for path in expected_relative_paths),
                "verification bundle was not refreshed after the work order was issued",
                {"paths": expected_relative_paths},
            )
        labels = {
            "citation-verification.json": "fresh citation verification",
            "export.json": "fresh answer export",
            "lint.json": "fresh lint report",
            "publication-readiness.json": "fresh publication readiness",
        }
        worker_documents = {
            name: load_json_object(
                evaluation_dir / name,
                error_code="ORCHESTRATION_POSTCONDITION_FAILED",
                label=label,
                max_bytes=MAX_VERIFICATION_ARTIFACT_BYTES,
                containment_root=project_root,
            )
            for name, label in labels.items()
        }
        authoritative = build_authoritative_verification(project_root, str(run_id))
        authoritative_citation = authoritative["citation-verification.json"]
        for name in labels:
            worker_semantics = verification_semantic_value(
                name,
                worker_documents[name],
                authoritative_citation,
            )
            authoritative_semantics = verification_semantic_value(
                name,
                authoritative[name],
                authoritative_citation,
            )
            require(
                worker_semantics == authoritative_semantics,
                f"worker verification artifact does not match authoritative recomputation: {name}",
                {"artifact": f"runs/{run_id}/evaluation/{name}"},
                "Regenerate the deterministic verification bundle from current workspace artifacts and retry.",
            )
        citation = authoritative_citation
        lint = authoritative["lint.json"]
        export = authoritative["export.json"]
        readiness = authoritative["publication-readiness.json"]
        answered_slugs = answered_grounded_slugs(export)
        if answered_slugs:
            quotes = load_sibling_module("verify_quotes").build_report(
                project_root,
                SimpleNamespace(slug=answered_slugs),
            )
        else:
            quotes = empty_quote_verification_report()
        coverage = status.get("coverage") if isinstance(status.get("coverage"), dict) else {}
        coverage_report = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": timestamp_utc(),
            "network_io_executed": False,
            "coverage": coverage,
        }
        citation_counts = citation.get("counts") if isinstance(citation.get("counts"), dict) else {}
        citation_total = int(citation_counts.get("total", 0) or 0)
        require(
            citation_total == 0 or citation.get("overall_result") == "verified",
            "fresh citation verification did not verify every selected academic source",
            {"overall_result": citation.get("overall_result"), "counts": citation_counts},
        )
        lint_counts = lint.get("stats", {}).get("issue_counts", {}) if isinstance(lint.get("stats"), dict) else {}
        require(int(lint_counts.get("HIGH", 0) or 0) == 0, "fresh lint report contains HIGH findings")
        required_coverage = coverage.get("required_question_counts")
        required_coverage = required_coverage if isinstance(required_coverage, dict) else {}
        require(
            all(int(required_coverage.get(key, 0) or 0) == 0 for key in ("blocked", "pending", "missing", "invalid")),
            "fresh coverage summary contains unresolved required coverage",
            {"required_question_counts": required_coverage},
        )
        require(quotes.get("overall_result") == "verified", "fresh quote verification did not pass")
        require(
            readiness.get("verdict") == "ship",
            "fresh publication readiness is not ship",
            {"verdict": readiness.get("verdict")},
        )
        if apply_effects:
            for name in labels:
                write_json_atomic(evaluation_dir / name, authoritative[name])
            write_json_atomic(evaluation_dir / "quote-verification.json", quotes)
            write_json_atomic(evaluation_dir / "coverage-summary.json", coverage_report)
            write_json_atomic(answers_path(project_root, session["orchestration_id"]), export)
            if current == "verifying":
                controller.run_finish(project_root, child_args(run_id, session["agent_id"], final_verdict="complete"))
            session["active_run_id"] = None
        return "complete", "Fresh publication readiness returned ship and answers were exported."

    raise OrchestrationControllerError("ORCHESTRATION_STATE_INVALID", f"unsupported submitted phase: {phase}")


def verify_blocked_delegated_acquisition_postconditions(
    project_root: Path,
    work_order: dict[str, Any],
) -> tuple[str, str | None]:
    """Classify a blocked delegated acquisition: the attempt ran, and changed nothing.

    The provider path has a second bounded outcome — an audited candidate route failure
    completes the attempt — because a candidate is the unit that can be exhausted there.
    Delegated acquisition has no candidates, and its equivalent of "this attempt produced
    nothing" is a *completed* result whose scoped requests carry recorded attempt failures.
    So `blocked` keeps exactly one meaning here: the attempt was aborted before it changed
    anything durable, and `resume` replays the same order.

    That makes verification a strict no-change check rather than the provider path's
    partial-delivery allowance. Nothing is lost by it: a delegate that already delivered
    evidence can fulfil the request, and one that already knows a request failed can record
    that failure — both are `completed`, which is the honest description of an action that
    changed the workspace.
    """
    config = load_config(project_root)
    scope = work_order.get("scope") if isinstance(work_order.get("scope"), dict) else {}
    request_ids = [value for value in scope.get("request_ids", []) if isinstance(value, str)]
    request_scope = set(request_ids)

    def require(
        condition: bool,
        message: str,
        details: dict[str, Any] | None = None,
        remediation: str | None = None,
    ) -> None:
        if not condition:
            raise OrchestrationControllerError(
                "ORCHESTRATION_POSTCONDITION_FAILED",
                message,
                recoverable=True,
                remediation=remediation or (
                    "Restore the workspace to its pre-action state and replay the same action, or report the "
                    "work actually done as completed."
                ),
                details=details,
            )

    require(
        bool(request_scope) and valid_scope_id_list(request_ids),
        "blocked delegated acquisition lacks a bounded request scope",
    )
    require(
        not [value for value in scope.get("candidate_ids", []) if isinstance(value, str) and value],
        "blocked delegated acquisition work order carries a candidate scope",
        remediation="Start a fresh orchestration session; delegated acquisition has no candidate store.",
    )

    postconditions = {
        item.get("check"): item
        for item in work_order.get("required_postconditions", [])
        if isinstance(item, dict) and isinstance(item.get("check"), str)
    }
    manifest_guard = postconditions.get("manifest_records_increased", {})
    manifest_records_before = manifest_guard.get("manifest_record_fingerprints_before")
    raw_tree_before = manifest_guard.get("raw_tree_before")
    candidate_records_before = manifest_guard.get("candidate_record_fingerprints_before")
    attempt_audit_before = manifest_guard.get("request_attempt_audit_record_fingerprints_before")
    source_requests_before = manifest_guard.get("source_request_record_fingerprints_before")
    normalized_files_before = manifest_guard.get("normalized_file_fingerprints_before")
    question_files_before = manifest_guard.get("question_file_fingerprints_before")
    before_manifest = manifest_guard.get("before")
    require(
        valid_record_fingerprint_snapshot(manifest_records_before)
        and isinstance(before_manifest, int)
        and not isinstance(before_manifest, bool)
        and before_manifest == len(manifest_records_before)
        and valid_raw_tree_snapshot(raw_tree_before, include_entries=True)
        and valid_record_fingerprint_snapshot(candidate_records_before)
        and valid_record_fingerprint_snapshot(attempt_audit_before)
        and valid_record_fingerprint_snapshot(source_requests_before)
        and request_scope <= set(source_requests_before)
        and valid_file_fingerprint_snapshot(normalized_files_before, prefix="sources/")
        and valid_question_file_fingerprint_snapshot(question_files_before),
        "blocked delegated acquisition lacks its exact pre-action integrity baseline",
        remediation="Preserve this action for audit and start a fresh orchestration; do not infer a baseline.",
    )

    def require_unchanged(
        before: dict[str, str],
        after: dict[str, str],
        message: str,
        detail_key: str,
        remediation: str,
    ) -> None:
        require(
            after == before,
            message,
            {detail_key: fingerprint_scope_violations(before, after, mutable_ids=set())},
            remediation,
        )

    # Read before the six workspace snapshots below, and never degraded to "no claims":
    # the store and the pages are frozen inside this order, so their no-change assertions
    # are satisfied by a claim rather than disproved by one. The ledger is the only place a
    # blocked attempt's bookkeeping can now show up.
    order_claims = load_sibling_module("_order_claims")
    claims = load_order_claims(
        project_root,
        require_safe_id(work_order.get("orchestration_id"), "orchestration_id"),
        require_safe_id(work_order.get("action_id"), "action_id"),
        order_claims,
    )
    require(
        not claims.get("fulfilments"),
        "blocked delegated acquisition changed the source-request store",
        {"claimed_request_ids": sorted(claims.get("fulfilments", {}))},
        "Restore every request; a blocked attempt cannot fulfill a request. Report a fulfilment as completed.",
    )
    require(
        not claims.get("reopens"),
        "blocked delegated acquisition changed question files",
        {"claimed_question_slugs": sorted(claims.get("reopens", {}))},
        "Restore every question; a blocked attempt cannot reopen a question.",
    )
    require_unchanged(
        source_requests_before,
        source_request_record_fingerprint_snapshot(project_root, config),
        "blocked delegated acquisition changed the source-request store",
        "source_request_scope_violations",
        "Restore every request; a blocked attempt cannot fulfill a request. Report a fulfilment as completed.",
    )
    require_unchanged(
        question_files_before,
        question_file_fingerprint_snapshot(project_root, config),
        "blocked delegated acquisition changed question files",
        "question_scope_violations",
        "Restore every question; a blocked attempt cannot reopen a question.",
    )
    require_unchanged(
        attempt_audit_before,
        record_fingerprint_snapshot(
            request_attempt_audit_events(project_root, config),
            id_field="event_id",
            label="source request attempt audit",
        ),
        "blocked delegated acquisition recorded an acquisition attempt",
        "attempt_audit_violations",
        (
            "A recorded attempt failure is durable evidence that the action ran, so report it as completed "
            "rather than blocked."
        ),
    )
    require_unchanged(
        candidate_records_before,
        candidate_record_fingerprint_snapshot(load_candidates(project_root, config)),
        "blocked delegated acquisition changed candidate records",
        "candidate_scope_violations",
        "Restore the candidate store; delegated acquisition has no candidate to transition.",
    )

    normalize_sources = load_sibling_module("normalize_sources")
    manifest_relative, _ = normalize_sources.source_paths(config)
    require_unchanged(
        manifest_records_before,
        record_fingerprint_snapshot(
            normalize_sources.load_manifest(project_root / manifest_relative),
            id_field="id",
            label="evidence manifest",
        ),
        "blocked delegated acquisition changed the evidence manifest",
        "manifest_scope_violations",
        "Remove the inventoried delivery, or fulfil the request it satisfies and report the action as completed.",
    )
    require_unchanged(
        normalized_files_before,
        normalized_file_fingerprint_snapshot(project_root, config),
        "blocked delegated acquisition changed normalized evidence",
        "normalized_scope_violations",
        "Remove the normalized output, or fulfil the request it satisfies and report the action as completed.",
    )
    require_unchanged(
        raw_tree_before["entries"],
        raw_tree_snapshot(project_root, config, include_entries=True)["entries"],
        "blocked delegated acquisition changed raw evidence",
        "raw_scope_violations",
        "Remove the delivered files, or inventory and fulfil them and report the action as completed.",
    )

    controller = load_sibling_module("run_controller")
    run_id = work_order.get("run_id")
    run_state = controller.load_run_state(project_root, run_id) if isinstance(run_id, str) else None
    current_child_state = (
        run_state.get("state", {}).get("current") if isinstance(run_state, dict) else None
    )
    require(
        current_child_state in {"fetching", "evidence_ready"},
        "blocked delegated acquisition child run is in an invalid state",
        {"child_state": current_child_state},
    )
    return PAUSED_STATUS, "The delegated acquisition changed nothing and can be replayed after resume."


def verify_blocked_action_postconditions(
    project_root: Path,
    session: dict[str, Any],
    work_order: dict[str, Any],
) -> tuple[str, str | None]:
    """Classify a blocked action without trusting its human-readable summary.

    Most blocked actions remain pending and are replayed after an explicit
    resume. Acquisition has one additional bounded outcome: an audited failure
    of the scoped selected candidate completes that route attempt so planning
    can continue with another candidate.
    """
    work_order = require_action_baselines(work_order, project_root)
    if work_order.get("phase") != "acquisition":
        return PAUSED_STATUS, None
    if work_order.get("acquisition_mode") == ACQUISITION_MODE_DELEGATED:
        return verify_blocked_delegated_acquisition_postconditions(project_root, work_order)

    config = load_config(project_root)
    scope = work_order.get("scope") if isinstance(work_order.get("scope"), dict) else {}
    request_ids = [value for value in scope.get("request_ids", []) if isinstance(value, str)]
    candidate_ids = [value for value in scope.get("candidate_ids", []) if isinstance(value, str)]
    request_scope = set(request_ids)
    candidate_scope = set(candidate_ids)

    def require(
        condition: bool,
        message: str,
        details: dict[str, Any] | None = None,
        remediation: str | None = None,
    ) -> None:
        if not condition:
            raise OrchestrationControllerError(
                "ORCHESTRATION_POSTCONDITION_FAILED",
                message,
                recoverable=True,
                remediation=remediation or "Restore the persisted acquisition baseline and replay the same action.",
                details=details,
            )

    require(
        bool(request_scope)
        and bool(candidate_scope)
        and valid_scope_id_list(request_ids)
        and valid_scope_id_list(candidate_ids),
        "blocked acquisition lacks a bounded request and candidate scope",
    )
    postconditions = {
        item.get("check"): item
        for item in work_order.get("required_postconditions", [])
        if isinstance(item, dict) and isinstance(item.get("check"), str)
    }
    manifest_guard = postconditions.get("manifest_records_increased", {})
    manifest_records_before = manifest_guard.get("manifest_record_fingerprints_before")
    raw_tree_before = manifest_guard.get("raw_tree_before")
    candidate_records_before = manifest_guard.get("candidate_record_fingerprints_before")
    candidate_audit_records_before = manifest_guard.get(
        "candidate_audit_record_fingerprints_before"
    )
    source_requests_before = manifest_guard.get("source_request_record_fingerprints_before")
    normalized_files_before = manifest_guard.get("normalized_file_fingerprints_before")
    question_files_before = manifest_guard.get("question_file_fingerprints_before")
    before_manifest = manifest_guard.get("before")
    require(
        valid_record_fingerprint_snapshot(manifest_records_before)
        and isinstance(before_manifest, int)
        and not isinstance(before_manifest, bool)
        and before_manifest == len(manifest_records_before)
        and valid_raw_tree_snapshot(raw_tree_before, include_entries=True)
        and valid_record_fingerprint_snapshot(candidate_records_before)
        and candidate_scope <= set(candidate_records_before)
        and valid_record_fingerprint_snapshot(candidate_audit_records_before)
        and valid_record_fingerprint_snapshot(source_requests_before)
        and request_scope <= set(source_requests_before)
        and valid_file_fingerprint_snapshot(normalized_files_before, prefix="sources/")
        and valid_question_file_fingerprint_snapshot(question_files_before),
        "blocked acquisition lacks its exact pre-action integrity baseline",
        remediation="Preserve this action for audit and start a fresh orchestration; do not infer a baseline.",
    )

    current_source_requests = source_request_record_fingerprint_snapshot(project_root, config)
    require(
        current_source_requests == source_requests_before,
        "blocked acquisition changed the source-request store",
        {
            "source_request_scope_violations": fingerprint_scope_violations(
                source_requests_before,
                current_source_requests,
                mutable_ids=set(),
            )
        },
        "Restore every request to its pre-action state; a blocked attempt cannot fulfill a request.",
    )
    current_question_files = question_file_fingerprint_snapshot(project_root, config)
    require(
        current_question_files == question_files_before,
        "blocked acquisition changed question files",
        {
            "question_scope_violations": fingerprint_scope_violations(
                question_files_before,
                current_question_files,
                mutable_ids=set(),
            )
        },
        "Restore every question to its pre-action state; a blocked attempt cannot reopen a question.",
    )

    all_candidates = load_candidates(project_root, config)
    candidates_by_id = {
        str(candidate.get("candidate_id")): candidate
        for candidate in all_candidates
        if isinstance(candidate.get("candidate_id"), str)
    }
    current_candidate_fingerprints = candidate_record_fingerprint_snapshot(all_candidates)
    candidate_scope_violations = fingerprint_scope_violations(
        candidate_records_before,
        current_candidate_fingerprints,
        mutable_ids=candidate_scope,
    )
    require(
        not any(candidate_scope_violations.values()),
        "blocked acquisition changed candidate records outside the persisted candidate scope",
        {"candidate_scope_violations": candidate_scope_violations},
        "Restore every out-of-scope candidate and mutate only the selected candidate named by this work order.",
    )
    scoped_candidates = [candidates_by_id.get(candidate_id) for candidate_id in candidate_ids]
    candidate_correlation_failures = [
        candidate_id
        for candidate_id, candidate in zip(candidate_ids, scoped_candidates, strict=True)
        if not isinstance(candidate, dict) or candidate_request_id(candidate) not in request_scope
    ]
    require(
        not candidate_correlation_failures,
        "blocked acquisition lost its request-to-candidate correlation",
        {"candidate_ids": candidate_correlation_failures},
    )
    candidate_states = {
        candidate_id: candidate_state(candidate)
        for candidate_id, candidate in zip(candidate_ids, scoped_candidates, strict=True)
        if isinstance(candidate, dict)
    }
    require(
        set(candidate_states.values()) <= {"selected", "failed"}
        and len(set(candidate_states.values())) == 1,
        "blocked acquisition must leave every scoped candidate selected or transition it to failed",
        {"candidate_states": candidate_states},
        (
            "Leave retryable candidates selected, or use discover_sources.py candidates transition with "
            "--expected-state selected --to-state failed for a candidate-specific route failure."
        ),
    )
    route_failed = bool(candidate_states) and next(iter(candidate_states.values())) == "failed"
    audit_events = candidate_failure_audit_events(project_root, config)
    current_audit_fingerprints = record_fingerprint_snapshot(
        audit_events,
        id_field="event_id",
        label="candidate lifecycle audit",
    )
    new_audit_event_ids = set(current_audit_fingerprints) - set(candidate_audit_records_before)
    audit_scope_violations = fingerprint_scope_violations(
        candidate_audit_records_before,
        current_audit_fingerprints,
        mutable_ids=set(),
        allowed_new_ids=new_audit_event_ids,
    )
    require(
        not any(audit_scope_violations.values()),
        "blocked acquisition changed existing candidate lifecycle audit records",
        {"candidate_audit_scope_violations": audit_scope_violations},
        "Restore the append-only candidate lifecycle audit and replay the same action.",
    )
    if not route_failed:
        changed_selected = [
            candidate_id
            for candidate_id in candidate_ids
            if current_candidate_fingerprints.get(candidate_id)
            != candidate_records_before.get(candidate_id)
        ]
        require(
            not changed_selected,
            "retryable blocked acquisition changed its selected candidate record",
            {"changed_candidate_ids": changed_selected},
            "Restore the selected candidate record, then resume and replay the same action.",
        )
        require(
            not new_audit_event_ids,
            "retryable blocked acquisition appended candidate lifecycle events",
            {"new_candidate_audit_event_ids": sorted(new_audit_event_ids)},
            "Remove the unexpected lifecycle events, leave the scoped candidate selected, and replay the action.",
        )
    else:
        run_id = work_order.get("run_id")
        require(
            len(new_audit_event_ids) == len(candidate_ids),
            "candidate-specific acquisition failure did not append exactly one audit event per scoped candidate",
            {
                "candidate_ids": candidate_ids,
                "new_candidate_audit_event_ids": sorted(new_audit_event_ids),
            },
        )
        new_audit_events = [
            event for event in audit_events if event.get("event_id") in new_audit_event_ids
        ]
        invalid_failures: list[dict[str, Any]] = []
        for candidate_id, candidate in zip(candidate_ids, scoped_candidates, strict=True):
            if not isinstance(candidate, dict):  # pragma: no cover - correlation was checked above
                invalid_failures.append(
                    {
                        "candidate_id": candidate_id,
                        "record_is_valid": False,
                        "audit_matches": False,
                    }
                )
                continue
            request_id = candidate_request_id(candidate)
            reason = candidate.get("failure_reason")
            actor = candidate.get("failed_by")
            failed_at = candidate.get("failed_at")
            record_is_valid = (
                current_candidate_fingerprints.get(candidate_id)
                != candidate_records_before.get(candidate_id)
                and candidate.get("fetch_status") == "failed"
                and candidate.get("selection_status") == "selected"
                and isinstance(reason, str)
                and bool(reason.strip())
                and isinstance(actor, str)
                and bool(actor.strip())
                and isinstance(failed_at, str)
                and bool(failed_at.strip())
                and candidate.get("lifecycle_reason") == reason
                and candidate.get("lifecycle_updated_by") == actor
                and candidate.get("lifecycle_updated_at") == failed_at
                and candidate.get("lifecycle_run_id") == run_id
            )
            audit_matches = any(
                event.get("event_type") == "candidate_transition"
                and event.get("event") == "transition"
                and event.get("candidate_id") == candidate_id
                and event.get("prior_state") == "selected"
                and event.get("new_state") == "failed"
                and event.get("request_id") == request_id
                and event.get("run_id") == run_id
                and event.get("actor") == actor
                and event.get("reason") == reason
                and event.get("at") == failed_at
                for event in new_audit_events
            )
            if not record_is_valid or not audit_matches:
                invalid_failures.append(
                    {
                        "candidate_id": candidate_id,
                        "record_is_valid": record_is_valid,
                        "audit_matches": audit_matches,
                    }
                )
        require(
            not invalid_failures,
            "candidate-specific acquisition failure lacks its canonical selected-to-failed audit",
            {"invalid_candidate_failures": invalid_failures},
            (
                "Record the route failure with discover_sources.py candidates transition using the work-order "
                "candidate id, request id, run id, and expected selected state."
            ),
        )

    normalize_sources = load_sibling_module("normalize_sources")
    manifest_relative, normalized_relative = normalize_sources.source_paths(config)
    manifest_records = normalize_sources.load_manifest(project_root / manifest_relative)
    current_manifest_fingerprints = record_fingerprint_snapshot(
        manifest_records,
        id_field="id",
        label="evidence manifest",
    )
    actual_new_source_ids = set(current_manifest_fingerprints) - set(manifest_records_before)
    manifest_scope_violations = fingerprint_scope_violations(
        manifest_records_before,
        current_manifest_fingerprints,
        mutable_ids=set(),
        allowed_new_ids=actual_new_source_ids,
    )
    require(
        not any(manifest_scope_violations.values()),
        "blocked acquisition changed existing evidence-manifest records",
        {"manifest_scope_violations": manifest_scope_violations},
        "Restore every pre-action manifest record; partial acquisition may only append scoped records.",
    )
    correlated_records: list[dict[str, Any]] = []
    uncorrelated_new_sources: list[str] = []
    for record in manifest_records:
        source_id = record.get("id") if isinstance(record, dict) else None
        provenance = record.get("provenance") if isinstance(record, dict) else None
        request_id = provenance.get("request_id") if isinstance(provenance, dict) else None
        candidate_id = provenance.get("candidate_id") if isinstance(provenance, dict) else None
        candidate = candidates_by_id.get(candidate_id) if isinstance(candidate_id, str) else None
        correlated = (
            request_id in request_scope
            and candidate_id in candidate_scope
            and isinstance(candidate, dict)
            and candidate_request_id(candidate) == request_id
        )
        if correlated:
            correlated_records.append(record)
        elif source_id in actual_new_source_ids:
            uncorrelated_new_sources.append(str(source_id))
    require(
        not uncorrelated_new_sources,
        "blocked acquisition appended manifest records outside its request/candidate correlation",
        {"source_ids": sorted(uncorrelated_new_sources)},
        "Remove uncorrelated manifest additions and retain only records tied to the scoped request and candidate.",
    )

    normalized_root = project_root / normalized_relative
    # Partial outputs may include the structured-view sidecar its record declares, for the
    # same reason a completed fulfilment may: normalization wrote both.
    allowed_new_normalized_paths: set[str] = set()
    for record in correlated_records:
        allowed_new_normalized_paths |= allowed_normalized_paths_for_record(
            project_root,
            normalize_sources.normalized_output_path_for_record(record, normalized_root),
            normalize_sources,
        )
    current_normalized_files = normalized_file_fingerprint_snapshot(project_root, config)
    actual_new_normalized_paths = set(current_normalized_files) - set(normalized_files_before)
    normalized_scope_violations = fingerprint_scope_violations(
        normalized_files_before,
        current_normalized_files,
        mutable_ids=set(),
        allowed_new_ids=actual_new_normalized_paths,
    )
    unexpected_normalized_paths = sorted(
        actual_new_normalized_paths - allowed_new_normalized_paths
    )
    require(
        not any(normalized_scope_violations.values()) and not unexpected_normalized_paths,
        "blocked acquisition changed normalized evidence outside correlated partial outputs",
        {
            "normalized_scope_violations": normalized_scope_violations,
            "unexpected_new_normalized_paths": unexpected_normalized_paths,
        },
        "Restore existing normalized evidence and remove outputs not correlated to the scoped acquisition.",
    )

    current_raw_tree = raw_tree_snapshot(project_root, config, include_entries=True)
    before_raw_entries = raw_tree_before["entries"]
    current_raw_entries = current_raw_tree["entries"]
    actual_new_raw_paths = set(current_raw_entries) - set(before_raw_entries)
    raw_scope_violations = fingerprint_scope_violations(
        before_raw_entries,
        current_raw_entries,
        mutable_ids=set(),
        allowed_new_ids=actual_new_raw_paths,
    )
    # Same predicate as the completed arms, over the correlated record set: a newly
    # appended correlated record's raw_paths must be what inventory derives, and the
    # files inventory attributes to correlated records are the partial-delivery scope.
    correlated_ids = {
        str(record.get("id"))
        for record in correlated_records
        if isinstance(record.get("id"), str)
    }
    correlated_new_records = {
        str(record.get("id")): record
        for record in correlated_records
        if isinstance(record.get("id"), str) and record.get("id") in actual_new_source_ids
    }
    by_correlated_id = {
        str(record.get("id")): record
        for record in correlated_records
        if isinstance(record.get("id"), str)
    }
    if correlated_new_records:
        raw_attribution = derived_raw_attribution(
            project_root, config, memo_key=current_raw_tree.get("fingerprint")
        )
    else:
        raw_attribution = {}
    attribution_mismatches = raw_attribution_mismatches(raw_attribution, correlated_new_records)
    require(
        not attribution_mismatches,
        "blocked acquisition manifest raw_paths do not match inventory-derived attribution",
        {"raw_attribution_mismatches": attribution_mismatches},
        RAW_ATTRIBUTION_REMEDIATION,
    )
    # Attribution expands a directory entry to its whole subtree, so it may only widen
    # admission for records this action created. Handing it every correlated record --
    # which the pre-expansion allowlist could safely do, because a bare directory string
    # is never a snapshot entry and so admitted nothing -- would let a partial delivery
    # write new files into a *pre-existing* correlated bundle's subtree. The completed
    # arms pass their new ids alone for the same reason.
    allowed_new_raw_paths = attributed_raw_paths(raw_attribution, set(correlated_new_records))
    for source_id in correlated_ids - set(correlated_new_records):
        record = by_correlated_id.get(source_id)
        raw_paths = record.get("raw_paths") if isinstance(record, dict) else None
        if not isinstance(raw_paths, list):
            continue
        for raw_path in raw_paths:
            if isinstance(raw_path, str) and raw_path.startswith("raw/") and safe_snapshot_relative_path(raw_path):
                allowed_new_raw_paths.add(raw_path)
                allowed_new_raw_paths.add(f"{raw_path}.provenance.yml")
    unexpected_raw_paths = sorted(actual_new_raw_paths - allowed_new_raw_paths)
    require(
        not any(raw_scope_violations.values()) and not unexpected_raw_paths,
        "blocked acquisition changed raw evidence outside correlated partial deliveries",
        {
            "raw_scope_violations": raw_scope_violations,
            "unexpected_new_raw_paths": unexpected_raw_paths[:MAX_TRUSTED_STATIC_INPUT_DIFFERENCES],
            "allowed_new_raw_paths": sorted(allowed_new_raw_paths)[:MAX_TRUSTED_STATIC_INPUT_DIFFERENCES],
        },
        "Restore existing raw evidence and remove deliveries not referenced by a correlated manifest record.",
    )

    controller = load_sibling_module("run_controller")
    run_id = work_order.get("run_id")
    run_state = controller.load_run_state(project_root, run_id) if isinstance(run_id, str) else None
    current_child_state = (
        run_state.get("state", {}).get("current") if isinstance(run_state, dict) else None
    )
    require(
        current_child_state in {"fetching", "evidence_ready"},
        "blocked acquisition child run is in an invalid state",
        {"child_state": current_child_state},
    )
    if route_failed:
        return "planning", "The scoped candidate route failed; planning may continue with remaining routes."
    return PAUSED_STATUS, "The scoped acquisition remains pending and can be replayed after resume."


def prepare_submission(
    project_root: Path,
    session: dict[str, Any],
    work_order: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    if result["outcome"] == "completed":
        next_phase, completion_reason = verify_action_postconditions(
            project_root,
            session,
            work_order,
            apply_effects=False,
        )
    elif result["outcome"] == "blocked":
        next_phase, completion_reason = verify_blocked_action_postconditions(
            project_root,
            session,
            work_order,
        )
        completion_reason = completion_reason or result["summary"]
    else:
        next_phase, completion_reason = "failed", result["summary"]
    pending = {
        "action_id": result["action_id"],
        "accepted_at": timestamp_utc(),
        "result": result,
        "result_digest": result_digest(result),
        "next_phase": next_phase,
        "completion_reason": completion_reason,
    }
    session["pending_submission"] = pending
    lease = work_order.get("lease") if isinstance(work_order.get("lease"), dict) else {}
    session["recovery"] = {
        "state": RECOVERY_FINALIZING,
        "action_id": result["action_id"],
        "attempt": int(lease.get("attempt", 1) or 1),
        "reason_code": "accepted_result_pending_finalization",
        "recorded_at": timestamp_utc(),
    }
    session["updated_at"] = timestamp_utc()
    write_json_atomic(session_path(project_root, session["orchestration_id"]), session)
    return pending


def retained_result(
    project_root: Path,
    orchestration_id: str,
    action_id: str,
) -> dict[str, Any] | None:
    path = work_result_path(project_root, orchestration_id, action_id)
    if not path.is_file():
        return None
    return load_result(path, action_id, project_root)


def ensure_completion_events(
    project_root: Path,
    session: dict[str, Any],
    result: dict[str, Any],
) -> None:
    action_id = result["action_id"]
    record_event_once(
        project_root,
        session,
        "action_completed",
        result["summary"],
        action_id=action_id,
        data=event_data_with_driver({"artifacts": result["artifacts"], "outcome": result["outcome"]}),
    )
    if session.get("status") in TERMINAL_STATUSES:
        reason = session.get("pause_reason") or result["summary"]
        record_event_once(
            project_root,
            session,
            "session_finished",
            str(reason),
            data={"status": session["status"]},
        )


def finalize_pending_submission(
    project_root: Path,
    session: dict[str, Any],
    work_order: dict[str, Any],
) -> dict[str, Any]:
    pending = session.get("pending_submission")
    if not valid_pending_submission(pending) or pending is None:
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "parent session does not contain a valid accepted submission",
            recoverable=False,
        )
    action_id = require_safe_id(pending["action_id"], "action_id")
    result = pending["result"]
    if session.get("pending_action_id") != action_id or work_order.get("action_id") != action_id:
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "accepted submission does not match the pending work order",
            recoverable=False,
        )
    verify_pending_trusted_static_inputs(project_root, session, work_order)
    expected_phase = pending.get("next_phase")
    completion_reason = pending.get("completion_reason")
    if result["outcome"] == "blocked":
        verified_phase, verified_reason = verify_blocked_action_postconditions(
            project_root,
            session,
            work_order,
        )
        if verified_phase != expected_phase:
            raise OrchestrationControllerError(
                "ORCHESTRATION_STATE_INVALID",
                "accepted blocked submission no longer verifies to its prepared next phase",
            )
        completion_reason = verified_reason or completion_reason

    existing = retained_result(project_root, session["orchestration_id"], action_id)
    if existing is not None and existing != result:
        raise OrchestrationControllerError(
            "RESULT_CONFLICT",
            f"action {action_id} already has a different retained result",
            recoverable=False,
        )
    if result["outcome"] == "blocked" and expected_phase == PAUSED_STATUS:
        if existing is not None:
            raise OrchestrationControllerError(
                "ORCHESTRATION_STATE_INVALID",
                "retryable blocked action already has a retained completion result",
                recoverable=False,
            )
        session["pending_submission"] = None
        session["recovery"] = default_recovery_state()
        session["status"] = PAUSED_STATUS
        session["phase"] = PAUSED_STATUS
        session["verdict"] = PAUSED_STATUS
        session["pause_reason"] = result["summary"]
        session["completed_at"] = None
        session["updated_at"] = timestamp_utc()
        write_json_atomic(session_path(project_root, session["orchestration_id"]), session)
        record_event_once(
            project_root,
            session,
            "action_paused",
            result["summary"],
            action_id=action_id,
            data={"outcome": result["outcome"], "resume_replays_action": True},
        )
        return session
    if existing is None:
        write_json_atomic(work_result_path(project_root, session["orchestration_id"], action_id), result)

    if result["outcome"] == "completed":
        verified_phase, verified_reason = verify_action_postconditions(
            project_root,
            session,
            work_order,
            apply_effects=False,
        )
        if verified_phase != expected_phase:
            raise OrchestrationControllerError(
                "ORCHESTRATION_STATE_INVALID",
                "accepted submission no longer verifies to its prepared next phase",
            )
        finalized_phase, finalized_reason = verify_action_postconditions(
            project_root,
            session,
            work_order,
            apply_effects=True,
        )
        if finalized_phase != expected_phase:
            raise OrchestrationControllerError(
                "ORCHESTRATION_STATE_INVALID",
                "action finalization changed the verified next phase",
            )
        completion_reason = finalized_reason or verified_reason or completion_reason
    elif result["outcome"] == "blocked":
        pass
    else:
        finish_active_child(project_root, session, "failed")
        if not any(record.get("action_id") == action_id for record in session["failure_records"]):
            session["failure_records"].append(
                {"recorded_at": timestamp_utc(), "action_id": action_id, "summary": result["summary"]}
            )

    if session.get("last_completed_action_id") != action_id:
        session["completed_action_count"] = int(session["completed_action_count"]) + 1
    session["pending_action_id"] = None
    session["pending_submission"] = None
    if "pending_trusted_static_inputs" in session:
        session["pending_trusted_static_inputs"] = None
    session["last_completed_action_id"] = action_id
    session["recovery"] = default_recovery_state()
    session["updated_at"] = timestamp_utc()
    if expected_phase in TERMINAL_STATUSES:
        session["status"] = expected_phase
        session["phase"] = expected_phase
        session["verdict"] = expected_phase
        session["pause_reason"] = None if expected_phase == "complete" else str(completion_reason or result["summary"])
        session["completed_at"] = session["updated_at"]
    else:
        session["status"] = ACTIVE_STATUS
        session["phase"] = expected_phase or "planning"
        session["verdict"] = None
        session["pause_reason"] = None
    write_json_atomic(session_path(project_root, session["orchestration_id"]), session)
    ensure_completion_events(project_root, session, result)
    return session


def repair_last_completion_events(project_root: Path, session: dict[str, Any]) -> None:
    action_id = session.get("last_completed_action_id")
    if not isinstance(action_id, str) or not action_id:
        return
    result = retained_result(project_root, session["orchestration_id"], action_id)
    if result is not None:
        ensure_completion_events(project_root, session, result)


def submit_result(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    orchestration_id = require_safe_id(args.orchestration_id, "orchestration_id")
    action_id = require_safe_id(args.action_id, "action_id")
    result = load_result(Path(args.result_file).expanduser().resolve(), action_id, project_root)
    with driver_session_lock(
        project_root,
        orchestration_id,
        command="submit",
        agent_id=args.agent_id,
        wait_seconds=getattr(args, "driver_wait_seconds", 0.0),
    ):
        session = load_session(project_root, orchestration_id)
        enforce_control_repair_gate(project_root, orchestration_id)
        if args.agent_id is not None and require_agent_id(args.agent_id) != session["agent_id"]:
            raise OrchestrationControllerError("ORCHESTRATION_OWNER_MISMATCH", "--agent-id does not own this session")
        retained = retained_result(project_root, orchestration_id, action_id)
        if retained is not None and retained != result:
            raise OrchestrationControllerError(
                "RESULT_CONFLICT",
                f"action {action_id} already has a different retained result",
                recoverable=False,
            )
        order = load_json_object(
            work_order_path(project_root, orchestration_id, action_id),
            error_code="WORK_ORDER_INVALID",
            label="work order",
        )
        require_action_baselines(order, project_root)
        verify_runtime_guards(project_root, session, order)
        pending_submission = session.get("pending_submission")
        if pending_submission is not None:
            if pending_submission.get("action_id") != action_id or pending_submission.get("result") != result:
                raise OrchestrationControllerError(
                    "RESULT_CONFLICT",
                    f"action {action_id} already has a different accepted submission",
                    recoverable=False,
                )
            with derivation_verdict_memo():
                return finalize_pending_submission(project_root, session, order)
        if retained is not None and session.get("pending_action_id") != action_id:
            if (
                session.get("last_completed_action_id") != action_id
                or int(session.get("completed_action_count", 0) or 0) < 1
            ):
                raise OrchestrationControllerError(
                    "ORCHESTRATION_STATE_INVALID",
                    f"retained result {action_id} is not proven completed by the parent session",
                    recoverable=False,
                )
            ensure_completion_events(project_root, session, retained)
            return session
        if session.get("pending_action_id") != action_id:
            raise OrchestrationControllerError(
                "ACTION_NOT_PENDING",
                f"action {action_id} is not the pending action",
                details={"pending_action_id": session.get("pending_action_id")},
            )
        with derivation_verdict_memo():
            prepare_submission(project_root, session, order, result)
            return finalize_pending_submission(project_root, session, order)


def select_session(project_root: Path, orchestration_id: str | None) -> dict[str, Any]:
    if orchestration_id is not None:
        return load_session(project_root, require_safe_id(orchestration_id, "orchestration_id"))
    root = orchestration_root(project_root)
    if not root.is_dir():
        raise OrchestrationControllerError("ORCHESTRATION_UNKNOWN", "no orchestration sessions exist")
    sessions: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / SESSION_FILENAME).is_file():
            try:
                sessions.append(load_session(project_root, child.name))
            except OrchestrationControllerError:
                continue
    if not sessions:
        raise OrchestrationControllerError("ORCHESTRATION_UNKNOWN", "no readable orchestration sessions exist")
    active = [item for item in sessions if item.get("status") not in TERMINAL_STATUSES]
    selected = active or sessions
    return sorted(
        selected,
        key=lambda item: (str(item.get("updated_at") or ""), item["orchestration_id"]),
        reverse=True,
    )[0]


def status_session(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    return select_session(project_root, args.orchestration_id)


def render_text(document: dict[str, Any]) -> str:
    if document.get("artifact_type") == WORK_ORDER_ARTIFACT_TYPE:
        return f"{document['orchestration_id']} {document['action_id']}: {document['phase']}\n"
    return (
        f"{document.get('orchestration_id')}: {document.get('status')} "
        f"({document.get('phase')}, actions={document.get('action_count', 0)})\n"
    )


def command_document(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "start":
        return start_session(project_root, args)
    if args.command == "next":
        return next_work(project_root, args)
    if args.command == "submit":
        return submit_result(project_root, args)
    if args.command == "status":
        return status_session(project_root, args)
    raise OrchestrationControllerError("VALUE_INVALID", f"unknown command: {args.command}")


def exit_code_for(document: dict[str, Any]) -> int:
    status = document.get("status")
    if status == "blocked_on_sources":
        return EXIT_BLOCKED
    if status == PAUSED_STATUS:
        return EXIT_PAUSED
    if status in {"no_ship", "failed"}:
        return EXIT_INVALID
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    args = parse_args(raw_argv)
    json_mode = json_mode_requested(raw_argv, default_json=args.format == "json")
    project_root = Path(args.project_root).expanduser().resolve()
    try:
        document = command_document(project_root, args)
    except OrchestrationControllerError as error:
        if json_mode:
            emit_error(
                str(error),
                json_mode=True,
                error_code=error.error_code,
                recoverable=error.recoverable,
                remediation=error.remediation,
                details=error.details,
            )
        else:
            print(f"refused ({error.error_code}): {error}", file=sys.stderr)
        return error.exit_code
    except LockUnavailableError as error:
        if json_mode:
            emit_error(
                str(error),
                json_mode=True,
                error_code=error.error_code,
                remediation=error.remediation,
                details=error.details,
            )
        else:
            print(f"refused ({error.error_code}): {error}", file=sys.stderr)
        return EXIT_INVALID
    except SystemExit as exc:
        return handle_system_exit(exc, json_mode=json_mode, default_exit_code=EXIT_INVALID)

    if args.format == "json":
        print(json.dumps(document, indent=2, sort_keys=False))
    else:
        sys.stdout.write(render_text(document))
    return exit_code_for(document)


if __name__ == "__main__":
    raise SystemExit(main())
