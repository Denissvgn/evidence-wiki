"""AC-1: a session driven through the library API leaves the workspace the CLI would have left.

One full session -- ``start -> next -> (satisfy the order) -> submit -> status``
-- is driven twice over two identically built copies of the same workspace
shape: once entirely through ``evidence-wiki`` subprocesses, once entirely
through :class:`evidence_wiki.workspace.Workspace`. Both whole trees are then
walked and compared, path set included, so a file written on one side only is a
failure rather than an omission.

The stated interpretation of "byte-identical"
---------------------------------------------
Nothing in this package injects a clock: the controller and the workspace
scripts call ``datetime.now(timezone.utc)`` directly. Two *separate executions*
therefore cannot be literally byte-identical wherever a timestamp is written --
the criterion as literally worded is unsatisfiable rather than merely hard. What
is asserted here instead, and what the change request decided:

    **Byte-identical after masking timestamp values only, keyed by an explicit
    allowlist of timestamp field names. Any difference outside that allowlist
    fails.**

Two weaker readings were considered and rejected; do not reintroduce either.

* *Blanket regex masking of anything ISO-8601-shaped.* Content-blind, so real
  drift sitting next to or inside a timestamp-shaped token is silently forgiven
  -- the test would be weakest exactly where it looks strongest.
  ``MaskingContractTests`` pins the opposite behaviour: a timestamp-shaped value
  under a key that is *not* on the allowlist still has to match.
* *A test-only clock-override environment variable.* It would touch dozens of
  stamp sites including duration, lease and staleness arithmetic, where a frozen
  clock changes behaviour rather than just output; and in a package whose gates
  are claim staleness, lease expiry and quote freshness, an environment variable
  that stops time is a tamper surface that becomes a de-facto contract the
  moment it ships.

The allowlist below is therefore hard-coded, with the source of every key named,
and never derived at runtime from the data under test -- a list that adapts to
what it sees proves nothing.

Two further normalizations, both narrower than a shape regex
------------------------------------------------------------
* **The drives share one absolute path.** They run one after the other in the
  same directory, each snapshotted before the next begins, so absolute paths
  recorded inside artifacts (``workspace_health.project_root``) compare byte for
  byte instead of needing a path mask.
* **One clock-minted identifier is substituted, not masked by shape.**
  ``source_requests.generate_request_id`` derives the request id from a sha1
  over ``created_at``, so the two drives mint different ids from the same
  inputs. Rather than widen a regex, each drive's *own* request id -- the exact
  string that drive returned -- is replaced with a fixed placeholder in that
  drive's bytes. An exact-literal substitution keeps the structure honest: a
  side that linked a different request, or none, still fails.

Why the CLI side passes ``-B``
------------------------------
Only ``orchestration_controller.py`` sets ``sys.dont_write_bytecode``, and
:mod:`evidence_wiki.orchestration` spawns it with ``-B`` and
``PYTHONDONTWRITEBYTECODE=1``. The other workspace scripts do not. Running one
of them with a bytecode-writing interpreter between ``next`` and ``submit``
creates ``scripts/__pycache__``, which the controller's trusted-input
fingerprint reports as workspace drift and refuses the pending action with
``ORCHESTRATION_TRUSTED_INPUT_CHANGED``. The CLI drive therefore suppresses
bytecode exactly as the package's own controller spawn does. That is a real
asymmetry -- the API path cannot hit it at all, because facade calls load
packaged scripts from the install's asset root and never from
``<workspace>/scripts`` -- and it is recorded here rather than papered over.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evidence_wiki.orchestration_schemas import public_orchestration_schema_documents  # noqa: E402
from evidence_wiki.workspace import Workspace  # noqa: E402

AGENT_ID = "agent-equivalence"
ORCHESTRATION_ID = "orch-equivalence"
QUESTION_SLUG = "benchmarks"
PROJECT_NAME = "library-api-equivalence"
PROJECT_DESCRIPTION = "Workspace for the CLI/API byte-equivalence proof."
BLOCKED_REASON = "This run supplies no evidence."
REQUEST_QUERY = "Evidence needed for the benchmark question"
LOG_FILE = "log.md"

# ---------------------------------------------------------------------------
# The timestamp allowlist.
#
# Group 1 -- ``evidence_wiki.orchestration_schemas.public_orchestration_schema_documents()``.
# Every property whose subschema resolves to ``#/$defs/timestamp`` in a published
# orchestration artifact schema. ``AllowlistProvenanceTests`` re-derives this set
# from those schemas and fails if the two ever drift apart.
SCHEMA_TIMESTAMP_KEYS = frozenset(
    {
        "accepted_at",  # orchestration_session, $defs/pending_submission
        "completed_at",  # orchestration_session
        "expires_at",  # orchestration_work_order, $defs/lease
        "issued_at",  # orchestration_work_order
        "recorded_at",  # orchestration_session, $defs/failure_record and $defs/recovery
        "started_at",  # orchestration_session
        "updated_at",  # orchestration_session
        "window_started_at",  # orchestration_session
    }
)

# Group 2 -- ``workspace-template/docs/question-api.md``: the question-page
# frontmatter keys and report fields that document says hold a timestamp.
QUESTION_API_TIMESTAMP_KEYS = frozenset(
    {
        "claimed_at",  # "Successful terminal outcomes clear `claimed_by` and `claimed_at`"
        "human_review_requested_at",  # frontmatter stamped by the answer transition
        "reviewed_at",  # per-policy entry in the `human_reviews` list
        "approved_at",  # frontmatter written once every policy is accepted
        "grounding_verified_at",  # frontmatter written by `verify_quotes.py --write`
        "generated_at",  # intake and export report schemas: "UTC timestamp"
    }
)

# Group 3 -- added deliberately, and only after the first run of this test failed
# on each of them by name. Every key here is documented as a timestamp by the
# workspace artifact contract that writes it; none was inferred from the data.
RUN_ARTIFACT_TIMESTAMP_KEYS = frozenset(
    {
        # workspace-template/docs/run-controller.md, "Event Records": the
        # `occurred_at` field of an `events.jsonl` line.
        "occurred_at",
        # run-controller.md, `run_state` schema: the `state` object carries
        # `current`, `entered_at`, `allowed_next_states`, `blocking_reason`.
        "entered_at",
        # run-controller.md, `run_state.state_history`: "Ordered transition
        # summaries"; written as `changed_at` by run_controller.py.
        "changed_at",
        # run-controller.md, `run_state.workspace_baseline`: "Run-start
        # checkpoint references, such as status/report baseline paths and
        # generated timestamps"; written as `captured_at` by run_controller.py.
        "captured_at",
        # workspace-template/docs/workspace-status.md: "| `last_intake_at` |
        # string or null | Most recent timestamped intake batch time. |"
        "last_intake_at",
        # workspace-template/docs/source-delivery.md, "Source Requests": the
        # source-request record's `created_at`.
        "created_at",
    }
)

TIMESTAMP_KEYS = SCHEMA_TIMESTAMP_KEYS | QUESTION_API_TIMESTAMP_KEYS | RUN_ARTIFACT_TIMESTAMP_KEYS

MASKED_TIMESTAMP = "<timestamp>"
MASKED_REQUEST_ID = "<request-id>"

#: An ISO-8601 date, optionally with a time. Used **only** on ``log.md`` lines,
#: where prose carries the stamp and there is no key to hang the mask on.
ISO_8601 = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?")

CLI_LABEL = "CLI"
API_LABEL = "API"


# ---------------------------------------------------------------------------
# Masking and comparison


def mask_timestamps(node: Any) -> Any:
    """Replace values under allowlisted timestamp keys, and nothing else.

    Only *string* values are replaced. A key whose value is ``None`` on one side
    and a string on the other therefore still compares unequal, which is the
    point: the mask forgives the clock, not a change of shape.
    """
    if isinstance(node, dict):
        return {
            key: MASKED_TIMESTAMP if key in TIMESTAMP_KEYS and isinstance(value, str) else mask_timestamps(value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [mask_timestamps(item) for item in node]
    return node


def mask_log_line(line: str) -> str:
    """Mask ISO-8601 tokens in one append-only log line, leaving the rest intact."""
    return ISO_8601.sub(MASKED_TIMESTAMP, line)


def describe_difference(left: Any, right: Any, trail: str = "") -> str | None:
    """Return a readable path to the first difference between two documents, or ``None``."""
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            where = f"{trail}.{key}" if trail else key
            if key not in left:
                return f"{where}: present only on the {API_LABEL} side"
            if key not in right:
                return f"{where}: present only on the {CLI_LABEL} side"
            found = describe_difference(left[key], right[key], where)
            if found is not None:
                return found
        return None
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return f"{trail or '<document>'}: {len(left)} item(s) on {CLI_LABEL}, {len(right)} on {API_LABEL}"
        for index, (one, other) in enumerate(zip(left, right, strict=True)):
            found = describe_difference(one, other, f"{trail}[{index}]")
            if found is not None:
                return found
        return None
    if left != right:
        return f"{trail or '<document>'}: {left!r} ({CLI_LABEL}) != {right!r} ({API_LABEL})"
    return None


def split_frontmatter(text: str) -> tuple[str, str] | None:
    """Return ``(frontmatter, body)`` for a Markdown page that opens with YAML frontmatter."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 3)
    if end == -1:
        return None
    return text[4 : end + 1], text[end + 5 :]


def structured_segments(relative_path: str, text: str) -> list[tuple[str, str, Any]] | None:
    """Split a structured artifact into ``(label, text, parsed)`` segments.

    ``None`` means "this is not a structured artifact"; compare it as raw bytes.
    A Markdown page splits into its YAML frontmatter and its body so that the
    body is never masked by a value that only the frontmatter authorized, and so
    that a difference in either is reported against a named part of the page.
    """
    suffix = Path(relative_path).suffix
    if suffix == ".json":
        return [("", text, json.loads(text))]
    if suffix == ".jsonl":
        return [("", text, [json.loads(line) for line in text.splitlines() if line.strip()])]
    if suffix in (".yml", ".yaml"):
        return [("", text, yaml.safe_load(text))]
    if suffix == ".md":
        split = split_frontmatter(text)
        if split is None:
            return None
        frontmatter, body = split
        return [("frontmatter", frontmatter, yaml.safe_load(frontmatter)), ("body", body, None)]
    return None


def segments_or_none(relative_path: str, text: str) -> list[tuple[str, str, Any]] | None:
    """``structured_segments``, degrading to raw comparison when a side does not parse.

    A file that parses on one side and not the other necessarily differs in
    bytes, so the raw fallback still reports it.
    """
    try:
        return structured_segments(relative_path, text)
    except (json.JSONDecodeError, yaml.YAMLError, UnicodeDecodeError):
        return None


def compare_artifact(relative_path: str, left: bytes, right: bytes) -> str | None:
    """Compare one artifact from both trees. Returns a message, or ``None`` when equal."""
    if left == right:
        return None
    try:
        left_text = left.decode("utf-8")
        right_text = right.decode("utf-8")
    except UnicodeDecodeError:
        return f"{relative_path}: raw bytes differ ({len(left)} vs {len(right)} bytes)"

    if relative_path == LOG_FILE:
        return compare_append_only_log(relative_path, left_text, right_text)

    left_segments = segments_or_none(relative_path, left_text)
    right_segments = segments_or_none(relative_path, right_text)
    if left_segments is not None and right_segments is not None:
        return compare_structured(relative_path, left_segments, right_segments)

    return compare_raw_text(relative_path, left_text, right_text)


def compare_structured(
    relative_path: str,
    left_segments: list[tuple[str, str, Any]],
    right_segments: list[tuple[str, str, Any]],
) -> str | None:
    """Compare a structured artifact in two passes, values first and bytes second.

    **Pass one -- values, keyed by name.** The parsed documents are compared with
    only allowlisted keys masked. This is the load-bearing pass, and it is the
    one that must stay key-keyed: masking by *value* instead would be a trap,
    because artifacts written in one operation reuse one instant across several
    fields, so blanking the instant an allowlisted key holds would silently blank
    every other key holding it too. A segment with no parsed form -- a Markdown
    body -- takes the raw-bytes rule instead, unmasked.

    **Pass two -- serialization.** Only once every value has been proved equal is
    the raw text compared with ISO-8601 tokens masked by shape. A shape mask is
    unsafe as a primary check and harmless as a secondary one: pass one already
    required every non-allowlisted value to match, so nothing can hide behind it
    here, and what is left to catch is key order, indentation, separators and
    trailing newlines -- real divergence between two write paths that a
    parse-only comparison would forgive.
    """
    paired = list(zip(left_segments, right_segments, strict=True))

    for (label, left_text, left_document), (_, right_text, right_document) in paired:
        where = f"{relative_path}:{label}" if label else relative_path
        if left_document is None and right_document is None:
            found = compare_raw_text(where, left_text, right_text)
            if found is not None:
                return found
            continue
        found = describe_difference(mask_timestamps(left_document), mask_timestamps(right_document))
        if found is not None:
            return f"{where}: {found}"

    for (label, left_text, left_document), (_, right_text, right_document) in paired:
        if left_document is None and right_document is None:
            continue
        where = f"{relative_path}:{label}" if label else relative_path
        left_skeleton = ISO_8601.sub(MASKED_TIMESTAMP, left_text)
        right_skeleton = ISO_8601.sub(MASKED_TIMESTAMP, right_text)
        if left_skeleton != right_skeleton:
            return compare_raw_text(where, left_skeleton, right_skeleton)
    return None


def compare_raw_text(relative_path: str, left_text: str, right_text: str) -> str | None:
    """Byte-for-byte comparison of an unstructured artifact, reported by line."""
    if left_text == right_text:
        return None
    left_lines, right_lines = left_text.splitlines(), right_text.splitlines()
    for number, (one, other) in enumerate(zip(left_lines, right_lines, strict=False), start=1):
        if one != other:
            return f"{relative_path}: line {number}: {one!r} ({CLI_LABEL}) != {other!r} ({API_LABEL})"
    return f"{relative_path}: {len(left_lines)} line(s) on {CLI_LABEL}, {len(right_lines)} on {API_LABEL}"


def compare_append_only_log(relative_path: str, left_text: str, right_text: str) -> str | None:
    """Compare ``log.md`` line by line, masking ISO-8601 tokens within each line."""
    left_lines = [mask_log_line(line) for line in left_text.splitlines()]
    right_lines = [mask_log_line(line) for line in right_text.splitlines()]
    for number, (one, other) in enumerate(zip(left_lines, right_lines, strict=False), start=1):
        if one != other:
            return f"{relative_path}: line {number}: {one!r} ({CLI_LABEL}) != {other!r} ({API_LABEL})"
    if len(left_lines) != len(right_lines):
        return f"{relative_path}: {len(left_lines)} line(s) on {CLI_LABEL}, {len(right_lines)} on {API_LABEL}"
    return None


def log_entries(text: str) -> list[str]:
    """Split ``log.md`` into its masked ``## `` entries, in order."""
    entries: list[list[str]] = []
    for line in text.splitlines():
        if line.startswith("## "):
            entries.append([mask_log_line(line)])
        elif entries:
            entries[-1].append(mask_log_line(line))
    return ["\n".join(entry).strip() for entry in entries]


def tree_paths(root: Path) -> dict[str, str]:
    """Every path under ``root``, mapped to what kind of entry it is."""
    kinds: dict[str, str] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            kinds[relative] = "symlink"
        elif path.is_dir():
            kinds[relative] = "directory"
        elif path.is_file():
            kinds[relative] = "file"
        else:  # pragma: no cover - fifos and sockets have no business here
            kinds[relative] = "other"
    return kinds


# ---------------------------------------------------------------------------
# Driving one session, twice


class DriveFailed(RuntimeError):
    """A setup command that had to succeed did not."""


def subprocess_environment() -> dict[str, str]:
    """The environment every CLI-side subprocess runs under.

    ``PYTHONPATH`` is pinned to this checkout's ``src`` because the development
    virtualenv may carry an editable install pointing somewhere else, and
    ``PYTHONDONTWRITEBYTECODE`` keeps workspace scripts from writing
    ``scripts/__pycache__`` -- see the module docstring.
    """
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = f"{SRC_ROOT}{os.pathsep}{existing}" if existing else str(SRC_ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def run_checked(argv: list[str]) -> str:
    """Run one subprocess that must succeed, and return its stdout."""
    completed = subprocess.run(argv, capture_output=True, text=True, check=False, env=subprocess_environment())
    if completed.returncode != 0:
        raise DriveFailed(f"{argv} exited {completed.returncode}\n{completed.stdout}\n{completed.stderr}")
    return completed.stdout


def run_cli(*args: str) -> str:
    """Run one ``evidence-wiki`` command as a real subprocess."""
    return run_checked([sys.executable, "-B", "-m", "evidence_wiki.cli", *args])


def run_workspace_script(target: Path, stem: str, *args: str) -> str:
    """Run one of the workspace's own deployed scripts as a real subprocess."""
    script = target / "scripts" / f"{stem}.py"
    return run_checked([sys.executable, "-B", str(script), "--project-root", str(target), *args])


def build_workspace(target: Path, batch_file: Path) -> None:
    """Create the workspace shape both drives start from.

    Construction is deliberately identical on both sides -- it is the constant
    the session drive varies against, not part of what is being compared.
    """
    run_cli(
        "init",
        "--target",
        str(target),
        "--project-name",
        PROJECT_NAME,
        "--project-description",
        PROJECT_DESCRIPTION,
    )
    batch_file.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "questions": [
                    {
                        "id": QUESTION_SLUG,
                        "question": "Which benchmarks matter for this decision?",
                        "priority": "high",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    run_cli("questions", "add", "--target", str(target), "--from-file", str(batch_file), "--format", "json")


def open_source_request(target: Path) -> str:
    """Record the open source request the question is blocked on, and return its id.

    ``source_requests`` has no counterpart in the declared library API surface,
    so both drives reach it the same way -- through the workspace's own deployed
    script. It is here because it is what makes ``submit`` a real verification:
    the controller reads the question page rather than trusting the result
    document's claim about it.
    """
    payload = run_workspace_script(
        target,
        "source_requests",
        "add",
        "--kind",
        "paper",
        "--query-or-identifier",
        REQUEST_QUERY,
        "--rationale",
        BLOCKED_REASON,
        "--priority",
        "high",
        "--question-slug",
        QUESTION_SLUG,
        "--format",
        "json",
    )
    return str(json.loads(payload)["request"]["request_id"])


def result_document(action_id: str) -> dict[str, Any]:
    """The one result document both drives submit."""
    return {
        "schema_version": "1.0",
        "action_id": action_id,
        "outcome": "completed",
        "summary": "Blocked the question on an open source request.",
        "artifacts": [],
    }


def drive_through_cli(target: Path, scratch: Path) -> str:
    """Drive the whole session with ``evidence-wiki`` and the workspace scripts."""
    run_cli(
        "orchestrate",
        "start",
        "--target",
        str(target),
        "--agent-id",
        AGENT_ID,
        "--orchestration-id",
        ORCHESTRATION_ID,
        "--format",
        "json",
    )
    order = json.loads(
        run_cli(
            "orchestrate",
            "next",
            "--target",
            str(target),
            "--orchestration-id",
            ORCHESTRATION_ID,
            "--format",
            "json",
        )
    )
    action_id = str(order["action_id"])

    run_workspace_script(
        target, "question_claim", "claim", "--slug", QUESTION_SLUG, "--agent-id", AGENT_ID, "--format", "json"
    )
    request_id = open_source_request(target)
    run_workspace_script(
        target,
        "question_resolve",
        "block",
        "--slug",
        QUESTION_SLUG,
        "--agent-id",
        AGENT_ID,
        "--blocked-reason",
        BLOCKED_REASON,
        "--request-id",
        request_id,
        "--format",
        "json",
    )

    result_file = scratch / "result.json"
    result_file.write_text(json.dumps(result_document(action_id)), encoding="utf-8")
    run_cli(
        "orchestrate",
        "submit",
        "--target",
        str(target),
        "--orchestration-id",
        ORCHESTRATION_ID,
        "--action-id",
        action_id,
        "--result-file",
        str(result_file),
        "--agent-id",
        AGENT_ID,
        "--format",
        "json",
    )
    run_cli(
        "orchestrate",
        "status",
        "--target",
        str(target),
        "--orchestration-id",
        ORCHESTRATION_ID,
        "--format",
        "json",
    )
    return request_id


def drive_through_api(target: Path) -> str:
    """Drive the same session in-process through :class:`Workspace`."""
    with Workspace.open(target) as workspace:
        session = workspace.orchestrate.start(AGENT_ID, orchestration_id=ORCHESTRATION_ID)
        order = session.next()
        action_id = str(order["action_id"])

        workspace.questions.claim(slug=QUESTION_SLUG, agent_id=AGENT_ID)
        request_id = open_source_request(target)
        workspace.questions.block(
            slug=QUESTION_SLUG,
            agent_id=AGENT_ID,
            blocked_reason=BLOCKED_REASON,
            request_id=[request_id],
        )

        session.submit(action_id, result_document(action_id), agent_id=AGENT_ID)
        session.status()
    return request_id


class Snapshot:
    """One finished drive: its tree, and the clock-minted id it produced."""

    def __init__(self, root: Path, request_id: str) -> None:
        self.root = root
        self.request_id = request_id

    def read(self, relative_path: str) -> bytes:
        """Return one artifact's bytes with this drive's own request id substituted."""
        raw = (self.root / relative_path).read_bytes()
        return raw.replace(self.request_id.encode("utf-8"), MASKED_REQUEST_ID.encode("utf-8"))

    def text(self, relative_path: str) -> str:
        return self.read(relative_path).decode("utf-8")

    def mentions_request_id(self) -> bool:
        """Whether the raw tree actually contains the id, so substitution is not vacuous."""
        needle = self.request_id.encode("utf-8")
        return any(path.is_file() and needle in path.read_bytes() for path in self.root.rglob("*"))


# ---------------------------------------------------------------------------
# Suites


class SessionEquivalenceTests(unittest.TestCase):
    """AC-1 itself: both drives, both whole trees, compared.

    Both drives run in the *same* directory, one after the other, each
    snapshotted before the next begins. That costs a copy and buys byte-for-byte
    comparison of every absolute path an artifact records.
    """

    cli: Snapshot
    api: Snapshot

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        scratch = Path(cls._tmp.name)
        cls.assert_subprocesses_import_this_checkout()

        target = scratch / "workspace"

        build_workspace(target, scratch / "batch.json")
        cli_request_id = drive_through_cli(target, scratch)
        cli_root = scratch / "snapshot-cli"
        shutil.copytree(target, cli_root, symlinks=True)
        shutil.rmtree(target)

        build_workspace(target, scratch / "batch.json")
        api_request_id = drive_through_api(target)
        api_root = scratch / "snapshot-api"
        shutil.copytree(target, api_root, symlinks=True)

        cls.cli = Snapshot(cli_root, cli_request_id)
        cls.api = Snapshot(api_root, api_request_id)

    @classmethod
    def assert_subprocesses_import_this_checkout(cls) -> None:
        """Refuse to prove anything about the wrong ``src``, on either side.

        The development virtualenv carries an editable install that may point at
        a different checkout entirely, and a green run against someone else's
        package would be worse than a red one.
        """
        import evidence_wiki

        if not str(Path(evidence_wiki.__file__).resolve()).startswith(str(SRC_ROOT)):
            raise DriveFailed(f"the API drive would use {evidence_wiki.__file__}, not {SRC_ROOT}")
        located = run_checked([sys.executable, "-B", "-c", "import evidence_wiki; print(evidence_wiki.__file__)"])
        if not located.strip().startswith(str(SRC_ROOT)):
            raise DriveFailed(f"CLI subprocesses would import {located.strip()}, not {SRC_ROOT}")

    def compare_trees(self, prefix: str = "") -> list[str]:
        """Every difference between the two trees under ``prefix``, one message each.

        Walks the *union* of both file sets rather than one side's, so a file
        that exists on only one side is reported as such instead of raising a
        ``FileNotFoundError`` that says nothing useful.
        """
        cli_files = {name for name, kind in tree_paths(self.cli.root).items() if kind == "file"}
        api_files = {name for name, kind in tree_paths(self.api.root).items() if kind == "file"}
        differences: list[str] = []
        for relative in sorted(name for name in cli_files | api_files if name.startswith(prefix)):
            if relative not in cli_files:
                differences.append(f"{relative}: present only on the {API_LABEL} side")
            elif relative not in api_files:
                differences.append(f"{relative}: present only on the {CLI_LABEL} side")
            else:
                message = compare_artifact(relative, self.cli.read(relative), self.api.read(relative))
                if message is not None:
                    differences.append(message)
        return differences

    def assert_same_paths(self, cli_paths: dict[str, str], api_paths: dict[str, str]) -> None:
        """Assert two path maps agree, naming the offending paths rather than dumping both maps."""
        self.assertEqual(sorted(set(cli_paths) - set(api_paths)), [], f"present only on the {CLI_LABEL} side")
        self.assertEqual(sorted(set(api_paths) - set(cli_paths)), [], f"present only on the {API_LABEL} side")
        kind_changes = [
            f"{name}: {cli_paths[name]} ({CLI_LABEL}) != {api_paths[name]} ({API_LABEL})"
            for name in sorted(set(cli_paths) & set(api_paths))
            if cli_paths[name] != api_paths[name]
        ]
        self.assertEqual([], kind_changes, "\n".join(kind_changes))

    def test_both_drives_produced_the_same_set_of_paths(self):
        """A file created on one side only is a failure, not something to skip over."""
        self.assert_same_paths(tree_paths(self.cli.root), tree_paths(self.api.root))

    def test_the_clock_minted_request_id_is_actually_present_in_both_trees(self):
        """Guards the substitution against silently becoming a no-op."""
        self.assertTrue(self.cli.mentions_request_id(), "the CLI drive recorded no source request")
        self.assertTrue(self.api.mentions_request_id(), "the API drive recorded no source request")

    def test_every_artifact_in_both_trees_matches_once_timestamps_are_masked(self):
        """The criterion. Every path in both trees, not only the ones expected to change."""
        differences = self.compare_trees()

        self.assertEqual([], differences, "\n".join(differences))

    def test_the_audit_log_entries_correspond_one_to_one(self):
        """``log.md`` is append-only: same entries, same order, same text."""
        cli_entries = log_entries(self.cli.text(LOG_FILE))
        api_entries = log_entries(self.api.text(LOG_FILE))

        self.assertEqual(len(cli_entries), len(api_entries), "the two drives appended a different number of entries")
        self.assertNotEqual([], cli_entries, "the drive appended no log entries at all")
        for index, (one, other) in enumerate(zip(cli_entries, api_entries, strict=True)):
            with self.subTest(entry=index):
                self.assertEqual(one, other)

    def test_the_orchestration_run_artifacts_correspond_one_to_one(self):
        """Everything under ``runs/orchestrations/<id>/``: same paths, same content."""
        prefix = f"runs/orchestrations/{ORCHESTRATION_ID}/"
        cli_paths = {name: kind for name, kind in tree_paths(self.cli.root).items() if name.startswith(prefix)}
        api_paths = {name: kind for name, kind in tree_paths(self.api.root).items() if name.startswith(prefix)}

        self.assertNotEqual({}, cli_paths, "the drive wrote no orchestration artifacts")
        self.assert_same_paths(cli_paths, api_paths)
        differences = self.compare_trees(prefix)
        self.assertEqual([], differences, "\n".join(differences))

    def test_both_drives_really_satisfied_the_work_order(self):
        """Guards against proving equivalence between two sessions that did nothing.

        ``submit`` only accepts the result because the controller re-read the
        question page and found it blocked on an open source request, which it
        records by advancing the child run. An equivalence test over two drives
        that both refused early would pass and mean nothing.
        """
        session_path = f"runs/orchestrations/{ORCHESTRATION_ID}/session.json"
        order_path = f"runs/orchestrations/{ORCHESTRATION_ID}/work-orders/action-0001.json"
        for label, snapshot in ((CLI_LABEL, self.cli), (API_LABEL, self.api)):
            with self.subTest(drive=label):
                session = json.loads(snapshot.text(session_path))
                self.assertEqual("action-0001", session["last_completed_action_id"])
                self.assertEqual(1, session["completed_action_count"])

                run_id = json.loads(snapshot.text(order_path))["run_id"]
                run_state = json.loads(snapshot.text(f"runs/{run_id}/run-state.json"))
                self.assertEqual("blocked_on_sources", run_state["state"]["current"])


class AllowlistProvenanceTests(unittest.TestCase):
    """The hard-coded allowlist has to keep matching the source it was read from."""

    def timestamp_properties(self, schema: Any) -> set[str]:
        names: set[str] = set()

        def refs_timestamp(node: Any) -> bool:
            if not isinstance(node, dict):
                return False
            if node.get("$ref") == "#/$defs/timestamp":
                return True
            return any(refs_timestamp(item) for item in node.get("anyOf", []))

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                properties = node.get("properties")
                if isinstance(properties, dict):
                    names.update(name for name, sub in properties.items() if refs_timestamp(sub))
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(schema)
        return names

    def test_the_schema_group_is_exactly_what_the_orchestration_schemas_declare(self):
        """Not derived at runtime for the comparison -- derived here to pin drift.

        If a published schema grows or loses a timestamp field, this fails and
        the hard-coded list has to be updated deliberately, which is the whole
        point of hard-coding it.
        """
        declared: set[str] = set()
        for schema in public_orchestration_schema_documents().values():
            declared |= self.timestamp_properties(schema)

        self.assertEqual(set(SCHEMA_TIMESTAMP_KEYS), declared)

    def test_the_three_groups_do_not_overlap_or_hide_each_other(self):
        """Each key is justified once, by one named source."""
        self.assertEqual(frozenset(), SCHEMA_TIMESTAMP_KEYS & QUESTION_API_TIMESTAMP_KEYS)
        self.assertEqual(frozenset(), SCHEMA_TIMESTAMP_KEYS & RUN_ARTIFACT_TIMESTAMP_KEYS)
        self.assertEqual(frozenset(), QUESTION_API_TIMESTAMP_KEYS & RUN_ARTIFACT_TIMESTAMP_KEYS)


class MaskingContractTests(unittest.TestCase):
    """What the masking forgives, and -- more importantly -- what it does not."""

    def test_only_allowlisted_keys_lose_their_value(self):
        document = {"started_at": "2026-08-10T08:45:18Z", "agent_id": "agent-equivalence"}

        self.assertEqual(
            {"started_at": MASKED_TIMESTAMP, "agent_id": "agent-equivalence"},
            mask_timestamps(document),
        )

    def test_a_timestamp_shaped_value_under_another_key_still_has_to_match(self):
        """The property the rejected blanket-regex reading would have thrown away.

        ``blocked_reason`` is free text that happens to contain a stamp. A regex
        over ISO-8601 shapes would forgive a changed one; keying on the field
        name does not.
        """
        left = {"blocked_reason": "waiting since 2026-08-10T08:45:18Z"}
        right = {"blocked_reason": "waiting since 2026-08-10T09:99:99Z"}

        found = describe_difference(mask_timestamps(left), mask_timestamps(right))

        self.assertIsNotNone(found)
        self.assertIn("blocked_reason", found)

    def test_a_difference_beside_a_masked_timestamp_is_still_reported(self):
        """Masking one field must not swallow its neighbours."""
        left = {"lease": {"expires_at": "2026-08-10T09:15:18Z", "attempt": 1}}
        right = {"lease": {"expires_at": "2026-08-10T09:15:21Z", "attempt": 2}}

        found = describe_difference(mask_timestamps(left), mask_timestamps(right))

        self.assertIsNotNone(found)
        self.assertIn("lease.attempt", found)

    def test_a_timestamp_key_whose_shape_changed_is_not_forgiven(self):
        """``None`` on one side and a stamp on the other is a real difference."""
        left = {"completed_at": None}
        right = {"completed_at": "2026-08-10T08:45:20Z"}

        found = describe_difference(mask_timestamps(left), mask_timestamps(right))

        self.assertIsNotNone(found)
        self.assertIn("completed_at", found)

    def test_a_key_present_on_one_side_only_is_named(self):
        for label, left, right, expected in (
            (API_LABEL, {}, {"extra": 1}, f"present only on the {API_LABEL} side"),
            (CLI_LABEL, {"extra": 1}, {}, f"present only on the {CLI_LABEL} side"),
        ):
            with self.subTest(side=label):
                found = describe_difference(left, right)
                self.assertIsNotNone(found)
                self.assertIn("extra", found)
                self.assertIn(expected, found)

    def test_the_log_mask_keeps_every_token_that_is_not_a_timestamp(self):
        masked = mask_log_line("- Created at: 2026-08-10T08:45:17Z. Request: `req-7ed9112455`.")

        self.assertEqual(f"- Created at: {MASKED_TIMESTAMP}. Request: `req-7ed9112455`.", masked)

    def test_the_log_comparison_reports_a_dropped_entry(self):
        """The failure a skipped audit append would produce."""
        full = "## [2026-08-10] intake | one\n\n- Created at: 2026-08-10T08:45:17Z.\n"
        truncated = "## [2026-08-10] intake | one\n"

        self.assertIsNone(compare_append_only_log(LOG_FILE, full, full.replace("45:17", "45:20")))
        self.assertIsNotNone(compare_append_only_log(LOG_FILE, full, truncated))

    def test_two_documents_that_parse_alike_but_serialize_differently_still_fail(self):
        """"Byte-identical" has to keep meaning bytes, not "equal after json.loads"."""
        compact = b'{"orchestration_id":"orch-equivalence","status":"active"}'
        indented = b'{\n  "orchestration_id": "orch-equivalence",\n  "status": "active"\n}'

        found = compare_artifact("runs/session.json", compact, indented)

        self.assertIsNotNone(found)
        self.assertIn("runs/session.json", found)

    def test_another_key_holding_the_same_instant_is_still_compared(self):
        """The trap a value-keyed mask falls into, pinned so nobody rebuilds it.

        Artifacts written in one operation reuse one instant across several
        fields. Blanking that instant wherever it appears would forgive every
        field holding it, allowlisted or not. Keying on the field name does not.
        """
        stamp, later = "2026-08-10T08:45:18Z", "2026-08-10T08:45:21Z"
        left = f'{{"started_at": "{stamp}", "first_seen": "{stamp}"}}'.encode()
        right = f'{{"started_at": "{later}", "first_seen": "{later}"}}'.encode()

        found = compare_artifact("runs/state.json", left, right)

        self.assertIsNotNone(found)
        self.assertIn("first_seen", found)

    def test_a_markdown_body_is_compared_as_raw_text(self):
        """The body is not a structured artifact; nothing about it is masked."""
        stamp = "2026-08-10T08:45:18Z"
        page = f'---\nclaimed_at: "{stamp}"\n---\n\nClaimed at {stamp}.\n'
        edited = f'---\nclaimed_at: "2026-08-10T08:45:21Z"\n---\n\nClaimed at {stamp} sharp.\n'

        found = compare_artifact("wiki/questions/benchmarks.md", page.encode(), edited.encode())

        self.assertIsNotNone(found)
        self.assertIn("body", found)

    def test_a_question_page_is_compared_through_its_frontmatter_and_its_body(self):
        page = '---\nslug: benchmarks\nclaimed_at: "2026-08-10T08:45:18Z"\n---\n\nBody text.\n'
        later = '---\nslug: benchmarks\nclaimed_at: "2026-08-10T08:45:21Z"\n---\n\nBody text.\n'
        edited = '---\nslug: benchmarks\nclaimed_at: "2026-08-10T08:45:18Z"\n---\n\nOther text.\n'

        self.assertIsNone(compare_artifact("wiki/questions/benchmarks.md", page.encode(), later.encode()))
        found = compare_artifact("wiki/questions/benchmarks.md", page.encode(), edited.encode())
        self.assertIsNotNone(found)
        self.assertIn("body", found)


if __name__ == "__main__":
    unittest.main()
