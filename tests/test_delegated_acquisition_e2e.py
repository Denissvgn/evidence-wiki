"""CR-3 end to end: a host acquires, the workspace audits, and the loop closes.

Every other CR-3 suite tests one leg. This one walks the chain the change request
actually asks for, in a workspace built the way an operator builds one:

    question blocked on a request -> `orchestrate start` -> `next` issues an
      acquisition order addressed to the acquirer -> the acquirer delivers,
      normalizes, fulfils and reopens *inside that order* -> `submit` verifies it
      -> routing returns to research

The legs are load-bearing in sequence rather than individually. A delivered payload
is only evidence once it normalizes; a normalized record is what opens the reopen
gate; and the reopened question is what lets the session make progress at all. A
regression in any one of them shows up here as a broken chain rather than as a
passing unit test about a stage nobody can reach.

The evidence is a structured JSON payload normalized through the CR-2 adapter, so
this also demonstrates the composition the two change requests describe: CR-2 makes
a non-documentary payload citable, CR-3 lets an external host be the one to deliver
it.

`ClosedGateTests` asserts the behaviour CR-3 exists to remove — the same workspace,
without the declaration, has no route to acquisition at all — because a chain that
never showed the gate shut would not be demonstrating anything.
"""

import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import yaml

from tests._script_loader import load_script as load_script_module

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
ADAPTER_FIXTURE = FIXTURES / "normalizer-adapter"
STUB_ADAPTER = ADAPTER_FIXTURE / "stub_adapter.py"
PAYLOAD = ADAPTER_FIXTURE / "keepa-b0abc123.json"
PROFILE_FIXTURE_PATH = FIXTURES / "workspace-init-profile.yml"
LATEX_BUNDLE_FIXTURE = FIXTURES / "chain-handoff" / "delivery" / "raw" / "papers" / "arXiv-2601.00002v1"

QUESTION_SLUG = "needs-price-evidence"
ACQUIRER = "autoseller-orchestrator"
ORCHESTRATION_ID = "orch-delegated-e2e"
ADAPTER_NAME = "stub-normalize"
ADAPTER_VERSION = "1.0.0"


def synthetic_pdf(lines: list[str]) -> bytes:
    """A one-page PDF whose text `pypdf` actually extracts, built byte by byte.

    The delegated arm turns a PDF away unless its normalized record carries extracted
    text, so a fixture the extractor reads nothing out of cannot walk this leg: it is
    refused by the usable-evidence guard several checks before any reuse rule is
    consulted. `tests/fixtures/chain-handoff/.../2601.00002v1.pdf` is such a fixture — it
    draws its text with no `/Resources` font entry, so `pypdf` returns the empty string
    and the record lands `needs_ocr` — which is why this builds its own rather than
    copying it. Deterministic and dependency-free: the same bytes on every run, so the
    re-derivation this suite depends on compares like with like.
    """
    content = "BT\n/F1 12 Tf\n14 TL\n72 700 Td\n" + "".join(f"({line}) Tj T*\n" for line in lines) + "ET\n"
    stream = content.encode("ascii")
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
        b" /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n%sendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    document = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(document))
        document += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    startxref = len(document)
    document += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        document += b"%010d 00000 n \n" % offset
    document += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        startxref,
    )
    return bytes(document)


INIT = load_script_module("e2e_delegated_init", "init_research_workspace.py")
CONTROLLER = load_script_module("e2e_delegated_controller", "orchestration_controller.py")
INVENTORY = load_script_module("e2e_delegated_inventory", "source_inventory.py")
NORMALIZE = load_script_module("e2e_delegated_normalize", "normalize_sources.py")
CLAIM = load_script_module("e2e_delegated_question_claim", "question_claim.py")
RESOLVE = load_script_module("e2e_delegated_question_resolve", "question_resolve.py")
REQUESTS = load_script_module("e2e_delegated_source_requests", "source_requests.py")
LINT = load_script_module("e2e_delegated_lint", "lint.py")


class DelegatedWorkspace:
    """Workspace scaffolding and protocol drivers.

    Deliberately not a TestCase: subclassing one that carries tests would re-run the
    whole parent suite under every child class.
    """

    # -- construction ------------------------------------------------------------

    def init_workspace(self, root: Path, *, spare_question: bool = False) -> Path:
        target = root / "delegated acquisition workspace"
        profile = yaml.safe_load(PROFILE_FIXTURE_PATH.read_text(encoding="utf-8"))
        profile["workspace_init"]["target_path"] = str(target)
        questions = [
            {
                "id": QUESTION_SLUG,
                "question": "What does the supplier quote for B0ABC12345?",
                "priority": "high",
            }
        ]
        if spare_question:
            # Stays actionable, so routing prefers research over acquisition.
            questions.append(
                {
                    "id": "answerable-from-delivered-evidence",
                    "question": "What is already answerable here?",
                    "priority": "high",
                }
            )
        profile["workspace_init"]["questions"] = questions
        profile_path = root / "profile.yml"
        profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            INIT.main(["--profile", str(profile_path)])
        return target

    def configure(self, workspace: Path, *, delegated: bool) -> None:
        """What an operator writes by hand: the raw root, the adapter, the acquirer."""
        config_path = workspace / "research.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["raw"]["source_roots"] = sorted({*config["raw"]["source_roots"], "raw/data"})
        config["normalization"] = {
            "adapters": [
                {
                    "kinds": ["structured_data"],
                    "provider": "command",
                    "command": [sys.executable, str(STUB_ADAPTER)],
                    "name": ADAPTER_NAME,
                    "version": ADAPTER_VERSION,
                }
            ]
        }
        if delegated:
            config["orchestration"] = {
                "acquisition": "delegated",
                "acquirer_agent_id": ACQUIRER,
            }
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    def block_question_on_a_request(
        self,
        workspace: Path,
        *,
        slug: str = QUESTION_SLUG,
        query: str = "Live supplier quote for B0ABC12345",
    ) -> str:
        """Reach `blocked_on_sources` the way research does: claim, request, block.

        Parameterised by slug so a backlog spanning two questions is built by walking this
        same route twice rather than by writing a second helper. Routing reads the state
        this route produces -- a claimed question, an open request linked to it, and the
        question blocked on that request. A question blocked some other way can look the
        same in the fixture and route differently, and two helpers reaching one state
        drift apart as soon as either is edited.
        """
        self.run_script(CLAIM, ["claim", "--slug", slug, "--agent-id", "research-agent"], workspace)
        request = self.run_script(
            REQUESTS,
            [
                "add", "--kind", "other",
                "--query-or-identifier", query,
                "--rationale", "The question cannot be answered from delivered evidence.",
                "--priority", "high", "--question-slug", slug,
            ],
            workspace,
        )["request"]["request_id"]
        self.run_script(
            RESOLVE,
            [
                "block", "--slug", slug, "--agent-id", "research-agent",
                "--blocked-reason", "No supplier quote has been delivered.",
                "--request-id", request,
            ],
            workspace,
        )
        return request

    def make_workspace(
        self, root: Path, *, delegated: bool = True, spare_question: bool = False
    ) -> tuple[Path, str]:
        workspace = self.init_workspace(root, spare_question=spare_question)
        self.configure(workspace, delegated=delegated)
        return workspace, self.block_question_on_a_request(workspace)

    # -- drivers -----------------------------------------------------------------

    def run_script(self, module, argv: list[str], workspace: Path) -> dict:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = module.main(["--project-root", str(workspace), *argv, "--format", "json"])
        self.assertEqual(0, int(code or 0), stderr.getvalue() or stdout.getvalue())
        return json.loads(stdout.getvalue())

    def controller(self, workspace: Path, command: str, *args: str) -> tuple[int, dict]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = CONTROLLER.main(
                ["--project-root", str(workspace), command, *args, "--format", "json"]
            )
        raw = stdout.getvalue() or stderr.getvalue()
        return int(code or 0), json.loads(raw)

    def start(self, workspace: Path) -> dict:
        code, payload = self.controller(
            workspace, "start", "--orchestration-id", ORCHESTRATION_ID, "--agent-id", "pm-agent"
        )
        self.assertEqual(0, code, payload)
        return payload

    def next_action(self, workspace: Path) -> tuple[int, dict]:
        return self.controller(workspace, "next", "--orchestration-id", ORCHESTRATION_ID)

    def submit(self, workspace: Path, action_id: str, *, outcome: str = "completed", artifacts=()) -> tuple[int, dict]:
        result_path = workspace.parent / f"result-{action_id}.json"
        result_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "action_id": action_id,
                    "outcome": outcome,
                    "summary": "Delegated acquisition delivered the scoped supplier quote.",
                    "artifacts": list(artifacts),
                }
            ),
            encoding="utf-8",
        )
        return self.controller(
            workspace, "submit",
            "--orchestration-id", ORCHESTRATION_ID,
            "--action-id", action_id,
            "--result-file", str(result_path),
        )

    # -- the acquirer ------------------------------------------------------------

    def acquire(self, workspace: Path, request_id: str) -> str:
        """Everything the external acquirer does inside its pending work order.

        Its own fetching happens outside the workspace and is represented here by
        copying the fixture payload — the point of delegation is that this package
        neither performs nor authorizes that step.
        """
        destination = workspace / "raw" / "data"
        destination.mkdir(parents=True, exist_ok=True)
        payload = destination / PAYLOAD.name
        shutil.copy2(PAYLOAD, payload)

        # The sidecar carries request_id: under delegation it is the only link between
        # a delivered artifact and the request it fulfils.
        sidecar = yaml.safe_load(
            PAYLOAD.with_name(PAYLOAD.name + ".provenance.yml").read_text(encoding="utf-8")
        )
        sidecar["retrieved_by"] = ACQUIRER
        sidecar["request_id"] = request_id
        sidecar["checksum"] = f"sha256:{hashlib.sha256(payload.read_bytes()).hexdigest()}"
        (destination / (PAYLOAD.name + ".provenance.yml")).write_text(
            yaml.safe_dump(sidecar, sort_keys=False), encoding="utf-8"
        )

        self.run_script(INVENTORY, ["--report"], workspace)
        self.run_script(NORMALIZE, ["--all"], workspace)
        source_id = self.source_id_for(workspace, f"raw/data/{PAYLOAD.name}")

        self.run_script(
            REQUESTS, ["fulfill", "--request-id", request_id, "--source-id", source_id], workspace
        )
        self.run_script(
            RESOLVE,
            [
                "reopen", "--slug", QUESTION_SLUG, "--agent-id", ACQUIRER,
                "--source-id", source_id, "--request-id", request_id,
            ],
            workspace,
        )
        return source_id

    # -- reading durable state ---------------------------------------------------

    def manifest_records(self, workspace: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in (workspace / "sources" / "manifest.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]

    def source_id_for(self, workspace: Path, raw_path: str) -> str:
        for record in self.manifest_records(workspace):
            if raw_path in record.get("raw_paths", []):
                return str(record["id"])
        raise AssertionError(f"no manifest record for {raw_path}")

    def evidence_state(self, workspace: Path) -> dict[str, str]:
        """Fingerprint every durable artifact a delegated action is allowed to change."""
        state: dict[str, str] = {}
        for relative in ("sources/source-requests.jsonl", "sources/manifest.jsonl"):
            path = workspace / relative
            state[relative] = (
                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "absent"
            )
        for path in sorted((workspace / "wiki" / "questions").glob("*.md")):
            state[f"wiki/questions/{path.name}"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return state

    def events(self, workspace: Path) -> list[dict]:
        path = workspace / "runs" / "orchestrations" / ORCHESTRATION_ID / "events.jsonl"
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def work_order(self, workspace: Path, action_id: str) -> dict:
        path = (
            workspace / "runs" / "orchestrations" / ORCHESTRATION_ID / "work-orders" / f"{action_id}.json"
        )
        return json.loads(path.read_text(encoding="utf-8"))

    def question_status(self, workspace: Path) -> str:
        text = (workspace / "wiki" / "questions" / f"{QUESTION_SLUG}.md").read_text(encoding="utf-8")
        return next(
            line.split(":", 1)[1].strip()
            for line in text.splitlines()
            if line.startswith("status:")
        )

    SECOND_SLUG = "needs-competitor-evidence"

    def add_second_question(self, workspace: Path) -> None:
        """A second question in the profile's own shape, so a backlog can span two of them.

        Written by copying the one the profile created rather than by re-running init:
        every field but the slug and the prose is exactly what an initialized workspace
        carries, which is what keeps `workspace_status` reading it as an ordinary question.
        """
        questions = workspace / "wiki" / "questions"
        (questions / f"{self.SECOND_SLUG}.md").write_text(
            (questions / f"{QUESTION_SLUG}.md").read_text(encoding="utf-8")
            .replace(f"slug: {QUESTION_SLUG}", f"slug: {self.SECOND_SLUG}")
            .replace("What does the supplier quote for B0ABC12345?", "What do competitors charge?"),
            encoding="utf-8",
        )

    def build_backlog(self, root: Path) -> tuple[Path, dict[str, str]]:
        """Two blocked questions over three requests, none of them satisfiable yet.

        The *second* question carries two of the requests. That grouping is deliberate:
        a blocked question must have every one of its blocking requests still open —
        `workspace_status` reports a fulfilled blocker as a missing open link and flips the
        verdict to `attention_required`, which freezes the session. So the request that
        gets fulfilled has to be the sole blocker of its question, and the two that fail
        can share one. (That constraint predates delegation entirely: it reproduces with
        no session and no `orchestration:` section. Batching just makes it easier to walk
        into — see the follow-up in the backlog.)
        """
        workspace = self.init_workspace(root, spare_question=False)
        self.configure(workspace, delegated=True)
        self.add_second_question(workspace)

        requests: dict[str, str] = {}
        for name, slug, query in (
            ("fulfilled", QUESTION_SLUG, "Live supplier quote for B0ABC12345"),
            ("throttled", self.SECOND_SLUG, "Historic price series for B0ABC12345"),
            ("refused", self.SECOND_SLUG, "Competitor offer snapshot"),
        ):
            requests[name] = self.run_script(
                REQUESTS,
                [
                    "add", "--kind", "other", "--query-or-identifier", query,
                    "--rationale", "Not answerable from delivered evidence.",
                    "--priority", "high", "--question-slug", slug,
                ],
                workspace,
            )["request"]["request_id"]

        for slug, request_ids in (
            (QUESTION_SLUG, [requests["fulfilled"]]),
            (self.SECOND_SLUG, [requests["throttled"], requests["refused"]]),
        ):
            self.run_script(CLAIM, ["claim", "--slug", slug, "--agent-id", "research-agent"], workspace)
            argv = ["block", "--slug", slug, "--agent-id", "research-agent",
                    "--blocked-reason", "Evidence has not been delivered."]
            for request_id in request_ids:
                argv.extend(["--request-id", request_id])
            self.run_script(RESOLVE, argv, workspace)
        return workspace, requests

    def record_failure(self, workspace: Path, request_id: str, code: str, action_id: str) -> None:
        self.run_script(
            REQUESTS,
            [
                "record-attempt-failure", "--request-id", request_id, "--failure-code", code,
                "--orchestration-id", ORCHESTRATION_ID, "--action-id", action_id,
            ],
            workspace,
        )

    def deliver_for(self, workspace: Path, request_id: str, *, scope: dict | None = None) -> str:
        """One genuinely new delivery, inventoried and normalized inside the pending order.

        Normalized by source id rather than with `--all`, which is what an acquirer holding
        an order for one request may do: `--all` also rewrites records the order does not
        scope, and a fixture that happens to hold nothing else to normalize hides the
        difference — see `NormalizeAllInsideAnOrderTests` for the state where it does not.
        """
        destination = workspace / "raw" / "data"
        destination.mkdir(parents=True, exist_ok=True)
        payload = destination / PAYLOAD.name
        shutil.copy2(PAYLOAD, payload)
        sidecar = yaml.safe_load(
            PAYLOAD.with_name(PAYLOAD.name + ".provenance.yml").read_text(encoding="utf-8")
        )
        sidecar["retrieved_by"] = ACQUIRER
        sidecar["request_id"] = request_id
        if scope is not None:
            # Lets a caller deliver evidence whose declared scope disagrees with the
            # request's, which is the only way to exercise the pairing check.
            sidecar["scope"] = dict(scope)
        sidecar["checksum"] = f"sha256:{hashlib.sha256(payload.read_bytes()).hexdigest()}"
        (destination / (PAYLOAD.name + ".provenance.yml")).write_text(
            yaml.safe_dump(sidecar, sort_keys=False), encoding="utf-8"
        )
        self.run_script(INVENTORY, ["--report"], workspace)
        source_id = self.source_id_for(workspace, f"raw/data/{PAYLOAD.name}")
        self.run_script(NORMALIZE, ["--source-id", source_id], workspace)
        return source_id

    def question_frontmatter(self, workspace: Path, slug: str) -> str:
        return (workspace / "wiki" / "questions" / f"{slug}.md").read_text(encoding="utf-8")

    # -- evidence the workspace already held --------------------------------------

    def deliver_before_the_order(
        self, workspace: Path, request_id: str | None, *, normalize: bool = True
    ) -> str:
        """Put a source in the workspace before the session starts.

        `request_id` of `None` leaves the sidecar unstamped, which is what evidence
        acquired for some earlier purpose looks like: a real record, correlated to
        nothing. `normalize=False` stops after the inventory, which is where a workspace
        sits when a prior order delivered evidence and never got as far as normalizing it.
        """
        destination = workspace / "raw" / "data"
        destination.mkdir(parents=True, exist_ok=True)
        payload = destination / PAYLOAD.name
        shutil.copy2(PAYLOAD, payload)
        sidecar = yaml.safe_load(
            PAYLOAD.with_name(PAYLOAD.name + ".provenance.yml").read_text(encoding="utf-8")
        )
        sidecar["retrieved_by"] = ACQUIRER
        sidecar["checksum"] = f"sha256:{hashlib.sha256(payload.read_bytes()).hexdigest()}"
        sidecar.pop("request_id", None)
        if request_id is not None:
            sidecar["request_id"] = request_id
        (destination / (PAYLOAD.name + ".provenance.yml")).write_text(
            yaml.safe_dump(sidecar, sort_keys=False), encoding="utf-8"
        )
        self.run_script(INVENTORY, ["--report"], workspace)
        if normalize:
            self.run_script(NORMALIZE, ["--all"], workspace)
        return self.source_id_for(workspace, f"raw/data/{PAYLOAD.name}")

    def add_request(self, workspace: Path, query: str) -> str:
        """A source request with no question behind it: an earlier purpose, since served."""
        return self.run_script(
            REQUESTS,
            [
                "add", "--kind", "other", "--query-or-identifier", query,
                "--rationale", "Recorded before this session, for a purpose already served.",
                "--priority", "high",
            ],
            workspace,
        )["request"]["request_id"]

    UNNORMALIZED_BYSTANDER_NAME = "keepa-b0yyy54321.json"
    UNNORMALIZED_BYSTANDER_BODY = (
        '{\n  "asin": "B0YYY54321",\n  "supplier_quote": "9.40 EUR",\n  "offer_count": 4\n}\n'
    )

    def deliver_unnormalized_bystander(self, workspace: Path, request_id: str) -> str:
        """A second correlated-but-un-normalized delivery, for a request the order will not scope.

        Same shape as a source an order's reuse baseline admits — inventoried, sidecar
        stamped, nothing normalized — differing only in which request the sidecar names.
        That is the one field the baseline is allowed to key on, so it is the only
        difference the fixture may carry.
        """
        destination = workspace / "raw" / "data"
        destination.mkdir(parents=True, exist_ok=True)
        payload = destination / self.UNNORMALIZED_BYSTANDER_NAME
        payload.write_text(self.UNNORMALIZED_BYSTANDER_BODY, encoding="utf-8", newline="\n")
        (destination / (self.UNNORMALIZED_BYSTANDER_NAME + ".provenance.yml")).write_text(
            yaml.safe_dump(
                {
                    "origin_url": "https://api.keepa.test/product/B0YYY54321",
                    "license": "CC-BY-4.0",
                    "retrieved_at": "2026-08-08T12:00:00Z",
                    "retrieved_by": ACQUIRER,
                    "request_id": request_id,
                    "checksum": f"sha256:{hashlib.sha256(payload.read_bytes()).hexdigest()}",
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        self.run_script(INVENTORY, ["--report"], workspace)
        return self.source_id_for(workspace, f"raw/data/{self.UNNORMALIZED_BYSTANDER_NAME}")

    TABLE_NAME = "supplier-quotes.csv"
    TABLE_BODY = "supplier,currency,unit_price\nacme,EUR,12.50\nglobex,EUR,13.75\n"

    def deliver_table_before_the_order(self, workspace: Path, request_id: str) -> str:
        """The same state one file wider: a delivery whose record will bind a structured view.

        A well-formed CSV takes the native table path, so normalizing it writes two files
        rather than one. Inventoried and left un-normalized, it is the reuse case with the
        sidecar attached.
        """
        destination = workspace / "raw" / "data"
        destination.mkdir(parents=True, exist_ok=True)
        payload = destination / self.TABLE_NAME
        payload.write_text(self.TABLE_BODY, encoding="utf-8", newline="\n")
        (destination / (self.TABLE_NAME + ".provenance.yml")).write_text(
            yaml.safe_dump(
                {
                    "origin_url": "https://example.test/supplier-quotes.csv",
                    "license": "CC-BY-4.0",
                    "retrieved_at": "2026-08-17T12:00:00Z",
                    "retrieved_by": ACQUIRER,
                    "request_id": request_id,
                    "checksum": f"sha256:{hashlib.sha256(payload.read_bytes()).hexdigest()}",
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        self.run_script(INVENTORY, ["--report"], workspace)
        return self.source_id_for(workspace, f"raw/data/{self.TABLE_NAME}")

    def pending_order(self, workspace: Path) -> dict:
        code, order = self.next_action(workspace)
        self.assertEqual(0, code, order)
        self.assertEqual("acquisition", order["phase"])
        return order

    def fulfil_and_reopen(
        self, workspace: Path, request_id: str, source_id: str, *, slug: str = QUESTION_SLUG
    ) -> None:
        self.run_script(
            REQUESTS, ["fulfill", "--request-id", request_id, "--source-id", source_id], workspace
        )
        self.run_script(
            RESOLVE,
            [
                "reopen", "--slug", slug, "--agent-id", ACQUIRER,
                "--source-id", source_id, "--request-id", request_id,
            ],
            workspace,
        )

    def normalized_record_for(self, workspace: Path, source_id: str) -> Path:
        for path in sorted((workspace / "sources" / "normalized").glob("*.md")):
            if source_id in path.read_text(encoding="utf-8"):
                return path
        raise AssertionError(f"no normalized record names {source_id}")

    def evidence_bytes(self, workspace: Path) -> dict[str, str]:
        """Digest exactly what an accepted reuse is forbidden to write: raw, normalized, manifest."""
        state: dict[str, str] = {}
        for root in (workspace / "raw", workspace / "sources" / "normalized"):
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    state[path.relative_to(workspace).as_posix()] = hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
        manifest = workspace / "sources" / "manifest.jsonl"
        state["sources/manifest.jsonl"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        return state


class DelegatedStructuredSourceTests(DelegatedWorkspace, unittest.TestCase):
    """A fulfilment whose normalized record binds a structured-view sidecar (EW-BUG-004).

    The rest of this file delivers a payload that normalizes to exactly one file, which is
    the only shape the postcondition's allowed set was ever built for. A well-formed CSV
    takes the native table path and earns a structured view
    (``normalize_sources.table_structured_skip_reason``), so normalization writes *two*
    files — the record and the sidecar the package itself put beside it. Nothing else in the
    suite reaches that state, which is why the refusal went unnoticed.
    """

    CSV_NAME = "supplier-quotes.csv"
    CSV_BODY = "supplier,currency,unit_price\nacme,EUR,12.50\nglobex,EUR,13.75\n"

    def acquire_csv(self, workspace: Path, request_id: str) -> str:
        """The delegated acquirer's loop, delivering a table instead of a JSON payload."""
        destination = workspace / "raw" / "data"
        destination.mkdir(parents=True, exist_ok=True)
        payload = destination / self.CSV_NAME
        payload.write_text(self.CSV_BODY, encoding="utf-8", newline="\n")

        sidecar = {
            "origin_url": "https://example.test/supplier-quotes.csv",
            "license": "CC-BY-4.0",
            "retrieved_at": "2026-08-17T12:00:00Z",
            "retrieved_by": ACQUIRER,
            "request_id": request_id,
            "checksum": f"sha256:{hashlib.sha256(payload.read_bytes()).hexdigest()}",
        }
        (destination / (self.CSV_NAME + ".provenance.yml")).write_text(
            yaml.safe_dump(sidecar, sort_keys=False), encoding="utf-8"
        )

        self.run_script(INVENTORY, ["--report"], workspace)
        self.run_script(NORMALIZE, ["--all"], workspace)
        source_id = self.source_id_for(workspace, f"raw/data/{self.CSV_NAME}")
        self.run_script(
            REQUESTS, ["fulfill", "--request-id", request_id, "--source-id", source_id], workspace
        )
        self.run_script(
            RESOLVE,
            [
                "reopen",
                "--slug",
                QUESTION_SLUG,
                "--agent-id",
                ACQUIRER,
                "--source-id",
                source_id,
                "--request-id",
                request_id,
            ],
            workspace,
        )
        return source_id

    def test_a_structured_source_can_close_a_delegated_fulfilment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            code, order = self.next_action(workspace)
            self.assertEqual(0, code, order)
            action_id = order["action_id"]

            self.acquire_csv(workspace, request_id)

            normalized_root = workspace / "sources" / "normalized"
            sidecars = sorted(path.name for path in normalized_root.glob("*.structured.json"))
            self.assertEqual(
                1,
                len(sidecars),
                "the reproduction needs a record that binds a structured view; "
                f"normalized tree held {sorted(p.name for p in normalized_root.iterdir())}",
            )

            code, session = self.submit(
                workspace, action_id, artifacts=[f"raw/data/{self.CSV_NAME}"]
            )
            self.assertEqual(
                0,
                code,
                "the package must not refuse a sidecar its own normalizer wrote: "
                f"{session}",
            )
            self.assertEqual("research", session["phase"])
            self.assertEqual("open", self.question_status(workspace))

    def test_an_unauthorised_normalized_file_is_still_refused(self):
        """The guard grew by one declared companion per source, not by a directory.

        Fixing EW-BUG-004 relaxes a fail-closed check, so the refusal it still owes is the
        part worth pinning: a normalized output no fulfilled source accounts for.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            code, order = self.next_action(workspace)
            self.assertEqual(0, code, order)

            self.acquire_csv(workspace, request_id)
            intruder = workspace / "sources" / "normalized" / "not-from-any-source.md"
            intruder.write_text("---\nsource_id: invented\n---\n\nbody\n", encoding="utf-8")

            code, session = self.submit(
                workspace, order["action_id"], artifacts=[f"raw/data/{self.CSV_NAME}"]
            )
            self.assertEqual(2, code, session)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", session["error_code"])
            self.assertIn(
                "sources/normalized/not-from-any-source.md",
                json.dumps(session["details"]),
                "the refusal must name the file it refused",
            )

    def test_an_undeclared_structured_sidecar_is_still_refused(self):
        """A sidecar is allowed because a record *declares* it, not because it exists.

        The controller runs no record-contract validation, so if this path allowed every
        `.structured.json` unconditionally, an action could leave an unbound sidecar beside
        a record that never referenced it and nothing in the flow would object.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            code, order = self.next_action(workspace)
            self.assertEqual(0, code, order)

            source_id = self.acquire_csv(workspace, request_id)
            normalized_root = workspace / "sources" / "normalized"
            declared = next(normalized_root.glob("*.structured.json"))
            # A second sidecar, for a record that does not exist and declares nothing.
            orphan = normalized_root / "raw--orphan-0000000000.structured.json"
            orphan.write_text(declared.read_text(encoding="utf-8"), encoding="utf-8")

            code, session = self.submit(
                workspace, order["action_id"], artifacts=[f"raw/data/{self.CSV_NAME}"]
            )
            self.assertEqual(2, code, f"{source_id}: {session}")
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", session["error_code"])
            self.assertIn(orphan.name, json.dumps(session["details"]))

    def test_a_fulfilment_without_its_normalized_record_is_still_refused(self):
        """The record stays obligatory after the exact-equality check was relaxed.

        Replacing `==` with a subset test is what lets an optional sidecar through; done
        carelessly it would also drop the requirement that each fulfilled source produced a
        record at all, which the equality used to carry.

        The refusal that actually fires here is the earlier `missing_normalized` check —
        a fulfilled request with no normalized record never reaches the scope comparison.
        That is the primary enforcement; the scope check's own missing-record refusal sits
        behind it as defence in depth for the case where a record exists but is not new.
        Asserted on the outcome rather than on which of the two spoke, because the
        obligation is what matters and either refusal honours it.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            code, order = self.next_action(workspace)
            self.assertEqual(0, code, order)

            self.acquire_csv(workspace, request_id)
            normalized_root = workspace / "sources" / "normalized"
            record = next(normalized_root.glob("*.md"))
            record.unlink()

            code, session = self.submit(
                workspace, order["action_id"], artifacts=[f"raw/data/{self.CSV_NAME}"]
            )
            self.assertEqual(2, code, session)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", session["error_code"])
            self.assertIn(
                session["message"],
                {
                    "fulfilled source requests do not have normalized evidence",
                    "newly fulfilled sources did not each produce a normalized record",
                },
                session,
            )


class DelegatedAcquisitionChainTests(DelegatedWorkspace, unittest.TestCase):
    """CR-3 AC1, walked end to end."""

    def test_the_delegated_loop_closes_and_leaves_no_unaccounted_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace, request_id = self.make_workspace(root)

            # The workspace fetches nothing itself, and says so.
            self.assertEqual("blocked", self.question_status(workspace))
            session = self.start(workspace)
            self.assertEqual("delegated", session["acquisition_mode"])
            self.assertEqual(ACQUIRER, session["acquirer_agent_id"])
            self.assertEqual(
                {"enabled": False, "providers": []},
                session["provider_policy"]["acquisition"],
                "delegation is not a provider grant",
            )

            after_start = self.evidence_state(workspace)

            # 1. The controller issues an acquisition order for the open request.
            code, order = self.next_action(workspace)
            self.assertEqual(0, code, order)
            self.assertEqual("acquisition", order["phase"])
            self.assertEqual("delegated", order["acquisition_mode"])
            self.assertEqual(ACQUIRER, order["assigned_agent_id"])
            self.assertEqual("research-acquire-delegated", order["skill"])
            self.assertEqual([request_id], order["scope"]["request_ids"])
            self.assertEqual([QUESTION_SLUG], order["scope"]["question_slugs"])
            self.assertEqual([], order["scope"]["candidate_ids"])
            action_id = order["action_id"]

            after_issue = self.evidence_state(workspace)
            self.assertEqual(
                after_start, after_issue, "issuing an order must not touch evidence state"
            )

            # 2. The acquirer works inside that order.
            source_id = self.acquire(workspace, request_id)

            # 3. Submission verifies the durable result, not the summary.
            code, session = self.submit(
                workspace, action_id, artifacts=[f"raw/data/{PAYLOAD.name}"]
            )
            self.assertEqual(0, code, session)
            self.assertEqual("research", session["phase"], "a fulfilment returns to research")
            self.assertEqual(action_id, session["last_completed_action_id"])

            # 4. The question is answerable again, backed by normalized evidence.
            self.assertEqual("open", self.question_status(workspace))
            normalized = list((workspace / "sources" / "normalized").glob("*.md"))
            self.assertTrue(normalized, "the delivered payload produced a normalized record")
            self.assertIn(
                ADAPTER_NAME,
                normalized[0].read_text(encoding="utf-8"),
                "the CR-2 adapter is what made a structured payload citable",
            )

            after_submit = self.evidence_state(workspace)
            self.assertNotEqual(
                after_issue, after_submit, "the action is where the workspace changed"
            )

            # 5. The loop continues: the reopened question routes back to research.
            code, follow_on = self.next_action(workspace)
            self.assertEqual(0, code, follow_on)
            self.assertEqual("research", follow_on["phase"])
            self.assertEqual([QUESTION_SLUG], follow_on["scope"]["question_slugs"])
            self.assertEqual(
                after_submit,
                self.evidence_state(workspace),
                "issuing the next order must not touch evidence state either",
            )

            # 6. The audit accounts for every mutation. This is the CR's own wording —
            #    "no out-of-band mutation exists in the audit log" — as an assertion.
            self.assert_every_mutation_is_bracketed_by_an_order(workspace)

            # 7. And the workspace agrees: lint sees no unattributed fulfilment.
            report = LINT.run_checks(workspace, LINT.load_config(workspace))
            self.assertEqual(0, report["stats"]["delegated_unattributed_fulfilments"])
            self.assertEqual(
                [],
                [issue["category"] for issue in report["issues"] if issue["severity"] == "HIGH"],
            )
            self.assertEqual(source_id, self.fulfilled_source_id(workspace, request_id))

    def fulfilled_source_id(self, workspace: Path, request_id: str) -> str:
        for line in (
            workspace / "sources" / "source-requests.jsonl"
        ).read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record["request_id"] == request_id:
                self.assertEqual("fulfilled", record["status"])
                return str(record["source_id"])
        raise AssertionError(f"request {request_id} vanished from the store")

    def assert_every_mutation_is_bracketed_by_an_order(self, workspace: Path) -> None:
        """Every durable change is attributable to an issued, completed work order.

        Checked from the two artifacts a host can read after the fact: the event log
        says which actions were issued and completed, and each retained work order says
        what that action was allowed to touch. A fulfilment or reopening that no such
        order scopes is precisely the unaccounted-for mutation CR-3 exists to remove.
        """
        events = self.events(workspace)
        issued = {
            event["action_id"]: event
            for event in events
            if event["event_type"] == "action_issued" and event.get("action_id")
        }
        completed = {
            event["action_id"]
            for event in events
            if event["event_type"] == "action_completed" and event.get("action_id")
        }
        self.assertTrue(issued, "the session issued no work orders at all")

        # Every issued action is completed, except the one still pending — the follow-on
        # research order this chain ends on. An action that simply vanished would mean a
        # work order authorized changes nothing later accounted for.
        session = json.loads(
            (workspace / "runs" / "orchestrations" / ORCHESTRATION_ID / "session.json").read_text(
                encoding="utf-8"
            )
        )
        pending = session.get("pending_action_id")
        scoped_requests: set[str] = set()
        scoped_questions: set[str] = set()
        for action_id in issued:
            if action_id != pending:
                self.assertIn(action_id, completed, f"{action_id} was issued but never completed")
            scope = self.work_order(workspace, action_id)["scope"]
            scoped_requests.update(scope.get("request_ids", []))
            scoped_questions.update(scope.get("question_slugs", []))

        fulfilled = {
            record["request_id"]
            for line in (
                workspace / "sources" / "source-requests.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            if line.strip()
            for record in [json.loads(line)]
            if record.get("status") == "fulfilled"
        }
        self.assertTrue(fulfilled, "nothing was fulfilled, so the chain proved nothing")
        self.assertLessEqual(
            fulfilled,
            scoped_requests,
            "a request was fulfilled that no issued work order scoped",
        )

        # The question moved blocked -> open, so some order had to scope it.
        self.assertIn(
            QUESTION_SLUG,
            scoped_questions,
            "the reopened question was never in an issued work order's scope",
        )


class RefusedArtifactTests(DelegatedWorkspace, unittest.TestCase):
    """CR-3 AC2: a claimed fulfilment is only accepted when its artifacts exist.

    Each case takes the same working chain and breaks exactly one artifact, then requires
    the refusal to name what is missing rather than reporting a generic postcondition
    failure — an acquirer that cannot tell *which* artifact it owes cannot repair it.

    The unit suite already pins each refusal against a hand-built order. What this adds is
    the acceptance shape: the flaw is introduced by an acquirer doing real work in a real
    workspace, and the refusal must leave the session **replayable** rather than wedged,
    which the last case proves by repairing the flaw and submitting successfully.
    """

    def assert_refused(self, workspace: Path, action_id: str, fragment: str) -> dict:
        code, envelope = self.submit(workspace, action_id)
        self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
        self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", envelope["error_code"])
        self.assertIn(fragment, envelope["message"])
        # Recoverable, and the action is still the pending one: a refusal is a repair
        # request, not a lost session.
        self.assertTrue(envelope["recoverable"])
        session = json.loads(
            (workspace / "runs" / "orchestrations" / ORCHESTRATION_ID / "session.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(action_id, session["pending_action_id"])
        return envelope

    # -- the delivered evidence itself -------------------------------------------

    def test_a_fulfilment_whose_source_has_no_provenance_sidecar_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)
            source_id = self.deliver(workspace, request_id, sidecar=False)
            self.fulfil_and_reopen(workspace, request_id, source_id)

            envelope = self.assert_refused(
                workspace, order["action_id"], "not linked to its source request by a provenance sidecar"
            )

            failure = envelope["details"]["correlation_failures"][0]
            self.assertEqual(request_id, failure["request_id"])
            self.assertFalse(failure["has_provenance"])

    def test_a_sidecar_naming_a_different_request_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)
            source_id = self.deliver(workspace, request_id, sidecar_request_id="req-someone-elses")
            self.fulfil_and_reopen(workspace, request_id, source_id)

            envelope = self.assert_refused(
                workspace, order["action_id"], "not linked to its source request by a provenance sidecar"
            )

            failure = envelope["details"]["correlation_failures"][0]
            self.assertTrue(failure["has_provenance"])
            self.assertEqual("req-someone-elses", failure["provenance_request_id"])

    def test_a_fulfilment_without_a_normalized_record_is_refused_by_source_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)
            source_id = self.deliver(workspace, request_id)
            self.fulfil_and_reopen(workspace, request_id, source_id)
            for path in (workspace / "sources" / "normalized").glob("*.md"):
                path.unlink()

            envelope = self.assert_refused(
                workspace, order["action_id"], "do not have normalized evidence"
            )

            self.assertEqual([source_id], envelope["details"]["source_ids"])

    def test_a_fulfilment_whose_record_is_unusable_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)
            source_id = self.deliver(workspace, request_id)
            self.fulfil_and_reopen(workspace, request_id, source_id)
            record = next((workspace / "sources" / "normalized").glob("*.md"))
            record.write_text(
                record.read_text(encoding="utf-8").replace(
                    "status: content_extracted", "status: stubbed", 1
                ),
                encoding="utf-8",
            )

            envelope = self.assert_refused(
                workspace, order["action_id"], "do not have usable normalized evidence"
            )

            self.assertEqual(
                source_id, envelope["details"]["quality_failures"][0]["source_id"]
            )

    # -- the per-request outcome contract ----------------------------------------

    def test_a_scoped_request_with_no_outcome_at_all_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)

            envelope = self.assert_refused(
                workspace,
                order["action_id"],
                "neither a fulfilment nor a recorded attempt failure",
            )

            self.assertEqual([request_id], envelope["details"]["request_ids"])

    def test_editing_a_request_the_acquirer_failed_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)
            self.run_script(
                REQUESTS,
                [
                    "record-attempt-failure", "--request-id", request_id,
                    "--failure-code", "provider_throttled",
                    "--orchestration-id", ORCHESTRATION_ID, "--action-id", order["action_id"],
                ],
                workspace,
            )
            store = workspace / "sources" / "source-requests.jsonl"
            record = json.loads(store.read_text(encoding="utf-8").splitlines()[0])
            record["rationale"] = "quietly rewritten after the attempt failed"
            store.write_text(json.dumps(record) + "\n", encoding="utf-8")

            self.assert_refused(
                workspace, order["action_id"], "outside the fulfilled request scope"
            )

    def test_touching_the_candidate_store_during_a_delegated_action_is_refused(self):
        # Both directions, because they are enforced by different halves of the same
        # check: an addition is caught as out-of-scope, a modification as a changed
        # fingerprint. A delegated order authorizes neither.
        for case in ("added", "changed"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmpdir:
                workspace, request_id = self.make_workspace(Path(tmpdir))
                candidates = workspace / "sources" / "discovery" / "candidates.jsonl"
                candidates.parent.mkdir(parents=True, exist_ok=True)
                if case == "changed":
                    # Present before the order is issued, so it is in the baseline.
                    candidates.write_text(
                        json.dumps(self.candidate_record(request_id)) + "\n", encoding="utf-8"
                    )
                self.start(workspace)
                order = self.pending_order(workspace)
                source_id = self.deliver(workspace, request_id)
                self.fulfil_and_reopen(workspace, request_id, source_id)

                if case == "added":
                    candidates.write_text(
                        json.dumps(self.candidate_record(request_id)) + "\n", encoding="utf-8"
                    )
                else:
                    record = self.candidate_record(request_id)
                    record["selected_by"] = ACQUIRER
                    candidates.write_text(json.dumps(record) + "\n", encoding="utf-8")

                envelope = self.assert_refused(
                    workspace, order["action_id"], "changed candidate records"
                )
                violations = envelope["details"]["candidate_scope_violations"]
                key = "added_outside_scope" if case == "added" else "changed_outside_scope"
                self.assertEqual(["cand-not-this-orders-business"], violations[key])

    def candidate_record(self, request_id: str) -> dict:
        return {
            "schema_version": "1.0",
            "candidate_id": "cand-not-this-orders-business",
            "request_id": request_id,
            "selected_for_request_id": request_id,
            "provider": "arxiv",
            "source_type": "paper",
            "lifecycle_schema_version": "2.0",
            "lifecycle_state": "selected",
            "status": "selected",
            "selected_at": "2026-08-08T00:00:00Z",
            "selected_by": "someone-else",
        }

    # -- a refusal is a repair request -------------------------------------------

    def test_a_refused_action_is_replayed_and_accepted_once_repaired(self):
        """The property the `recoverable` flag promises, exercised rather than trusted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            self.start(workspace)
            order = self.pending_order(workspace)
            action_id = order["action_id"]
            source_id = self.deliver(workspace, request_id, sidecar=False)
            self.fulfil_and_reopen(workspace, request_id, source_id)
            self.assert_refused(
                workspace, action_id, "not linked to its source request by a provenance sidecar"
            )

            # `next` returns the same pending order, unchanged.
            code, replayed = self.next_action(workspace)
            self.assertEqual(0, code, replayed)
            self.assertEqual(action_id, replayed["action_id"])
            self.assertEqual(order, replayed)

            # The acquirer supplies the missing sidecar and re-inventories.
            self.write_sidecar(workspace, request_id)
            self.run_script(INVENTORY, ["--report"], workspace)
            self.run_script(NORMALIZE, ["--all"], workspace)

            code, session = self.submit(workspace, action_id)

            self.assertEqual(0, code, session)
            self.assertEqual("research", session["phase"])
            self.assertEqual("open", self.question_status(workspace))

    # -- helpers ------------------------------------------------------------------

    def write_sidecar(self, workspace: Path, request_id: str | None, *, name: str | None = None) -> None:
        destination = workspace / "raw" / "data"
        payload = destination / PAYLOAD.name
        sidecar = yaml.safe_load(
            PAYLOAD.with_name(PAYLOAD.name + ".provenance.yml").read_text(encoding="utf-8")
        )
        sidecar["retrieved_by"] = ACQUIRER
        sidecar["checksum"] = f"sha256:{hashlib.sha256(payload.read_bytes()).hexdigest()}"
        if request_id is not None:
            sidecar["request_id"] = request_id
        (destination / (PAYLOAD.name + ".provenance.yml")).write_text(
            yaml.safe_dump(sidecar, sort_keys=False), encoding="utf-8"
        )

    def deliver(
        self,
        workspace: Path,
        request_id: str,
        *,
        sidecar: bool = True,
        sidecar_request_id: str | None = None,
    ) -> str:
        destination = workspace / "raw" / "data"
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PAYLOAD, destination / PAYLOAD.name)
        if sidecar:
            self.write_sidecar(workspace, sidecar_request_id or request_id)
        self.run_script(INVENTORY, ["--report"], workspace)
        self.run_script(NORMALIZE, ["--all"], workspace)
        return self.source_id_for(workspace, f"raw/data/{PAYLOAD.name}")


class PreExistingEvidenceReuseTests(DelegatedWorkspace, unittest.TestCase):
    """Fulfilling a scoped request from evidence the workspace already held (EW-BUG-005).

    Everywhere else in this file the acquirer delivers a *new* artifact inside its order,
    so the reuse leg the delegated arm deliberately keeps — an unchanged source delivered
    before the order was issued can satisfy a scoped request without being fetched again,
    which is what `matching_normalized_source_records` exists to fingerprint — was never
    walked end to end. Both of its outcomes went unobserved as a result.

    Reuse is admitted on terms fixed at issuance: the source's sidecar named the request
    *and* the source already carried a normalized record, which together are what put it
    in the order's scoped-match baseline. A record outside every baseline the order wrote
    can never reconcile against one. The package used to discover that four guards later,
    having first told the operator to stamp the sidecar and re-run the inventory — advice
    that is itself refused, because `raw/` is immutable and re-inventorying rewrites a
    manifest record the order may not touch. Five refusals across four guards, and none of
    them named the constraint that was actually broken.

    This class owns the leg where the source was already normalized when the order was
    issued, and what that leg may still not touch. The one case the *controller* authorizes
    beyond it -- a correctly correlated source nothing had normalized yet -- belongs to
    `ControllerAuthorisedReuseTests` below, together with the cases that stay refused. What
    stays here is the boundary: a record rewritten after the order was issued is outside
    every baseline whatever the sidecar says, because what it says now is no evidence of
    what it said then.
    """

    REUSE_REFUSAL = (
        "reuses pre-existing evidence that was not a scoped reconciliation match "
        "when the order was issued"
    )

    def assert_reuse_refusal(self, envelope: dict, source_id: str, cause: str) -> dict:
        self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", envelope["error_code"])
        self.assertIn(self.REUSE_REFUSAL, envelope["message"], envelope)
        self.assertTrue(envelope["recoverable"])
        failures = envelope["details"]["reuse_scope_failures"]
        self.assertEqual([source_id], [item["source_id"] for item in failures], envelope)
        self.assertEqual(
            cause,
            failures[0]["cause"],
            "the refusal must report the cause it actually hit, not the one it assumed",
        )
        return failures[0]

    # -- the refusal the operator is owed ------------------------------------------

    def test_reopening_still_refuses_a_source_nothing_has_normalized(self):
        """Reuse never became an exemption from normalizing, and this is where that starts.

        A source inventoried by a prior order that never reached `normalize_sources.py` is
        reusable -- the order authorizes it and authorizes the record it owes -- but it is
        not yet evidence. `question_resolve.py reopen` says so before the action can be
        submitted at all, which is what makes "leave it un-normalized" not a route past the
        postcondition rather than merely a route the postcondition catches later.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            source_id = self.deliver_before_the_order(workspace, request_id, normalize=False)
            self.start(workspace)
            self.pending_order(workspace)
            self.run_script(
                REQUESTS, ["fulfill", "--request-id", request_id, "--source-id", source_id], workspace
            )

            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = RESOLVE.main(
                    [
                        "--project-root", str(workspace), "reopen", "--slug", QUESTION_SLUG,
                        "--agent-id", ACQUIRER, "--source-id", source_id,
                        "--request-id", request_id, "--format", "json",
                    ]
                )

            self.assertEqual(CONTROLLER.EXIT_INVALID, int(code or 0))
            self.assertEqual(
                "SOURCE_NOT_NORMALIZED",
                json.loads(stdout.getvalue() or stderr.getvalue())["error_code"],
            )

    def test_following_the_sidecar_remediation_reaches_the_same_single_refusal(self):
        """The bug as an operator meets it: the advice printed leads back here.

        The correlation refusal says to stamp `request_id` into the sidecar and re-run
        `source_inventory.py`. On a source `raw/` already holds, obeying it edits immutable
        raw evidence and rewrites a manifest record this order may not touch — so the
        obedient acquirer used to be refused again, by a different guard, with a message
        about manifest scope that says nothing about what it did wrong. The refusal is now
        the same one either way, and reports the rewrite rather than reading the sidecar
        the acquirer has just edited as though it proved what was true at issuance.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            source_id = self.deliver_before_the_order(workspace, None)
            self.start(workspace)
            order = self.pending_order(workspace)

            # Exactly what the correlation remediation asks for, done after the fact.
            self.deliver_before_the_order(workspace, request_id)
            self.fulfil_and_reopen(workspace, request_id, source_id)

            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            failure = self.assert_reuse_refusal(
                envelope, source_id, "manifest_record_changed_after_issuance"
            )
            self.assertFalse(failure["record_unchanged"])

    def assert_rebuilt_reuse_is_refused(self, workspace: Path, action_id: str, source_id: str) -> dict:
        code, envelope = self.submit(workspace, action_id)
        self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
        self.assertEqual(
            "pre-existing fulfilled evidence is not an unchanged exact scoped reconciliation match",
            envelope["message"],
            envelope,
        )
        failure = next(
            item
            for item in envelope["details"]["reconciliation_failures"]
            if item["source_id"] == source_id
        )
        self.assertTrue(failure["was_scoped_match"], failure)
        self.assertFalse(failure["normalized_unchanged"], failure)
        return envelope

    def rebuild_the_normalized_record(self, workspace: Path, source_id: str, stamp: str) -> None:
        """Exactly what the shared remediation used to print, with the clock pinned.

        `normalized_at` is second-resolution, so two rebuilds inside one second render
        identical bytes and the refusal being reproduced would not reproduce. That is a
        property of how fast a test runs, not of the guard, so the stamp is fixed here
        rather than left to the wall clock.
        """
        with mock.patch.object(NORMALIZE, "timestamp_utc", lambda: stamp):
            self.run_script(NORMALIZE, ["--source-id", source_id, "--force"], workspace)

    def test_rebuilding_a_fingerprinted_record_is_refused_and_only_restoring_it_helps(self):
        """The arm-(a) half of the bug above: printed advice the operator cannot follow.

        A source the order fingerprinted normalized reconciles on exactly those bytes. The
        shared remediation told its operator to rewrite the record with
        `normalize_sources.py --source-id <id> --force`, which is the right answer for the
        *other* arm — the one where the order recorded no normalized output and the acquirer
        owes a freshly derived record. Here it cannot work at all: the rebuild restamps
        `normalized_at` and changes nothing else, and that stamp is inside the fingerprint,
        so no number of rebuilds lands back on the bytes the order recorded.

        So the advice is walked rather than read. Rebuild, submit, rebuild again, submit
        again — the identical refusal both times, which is what an operator following the
        printed text actually experienced. Then the repair the failure now names is performed,
        and it is the one that ends it.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, _, source_id, order = self.arrive_at_reuse(Path(tmpdir))
            record = self.normalized_record_for(workspace, source_id)
            fingerprinted = record.read_bytes()

            self.rebuild_the_normalized_record(workspace, source_id, "2026-08-20T09:00:00Z")
            rebuilt = record.read_bytes()
            self.assertNotEqual(fingerprinted, rebuilt, "the rebuild rewrote nothing")
            self.assertEqual(
                [
                    line
                    for line in fingerprinted.decode("utf-8").splitlines()
                    if not line.startswith("normalized_at:")
                ],
                [
                    line
                    for line in rebuilt.decode("utf-8").splitlines()
                    if not line.startswith("normalized_at:")
                ],
                "the rebuild differs by more than the stamp, so this is not the arm-(a) trap",
            )

            envelope = self.assert_rebuilt_reuse_is_refused(workspace, order["action_id"], source_id)
            failure = next(
                item
                for item in envelope["details"]["reconciliation_failures"]
                if item["source_id"] == source_id
            )
            self.assertNotIn(
                "--force",
                failure["repair"],
                "arm (a) is still being sent to the rewrite that restamps what it must restore",
            )
            self.assertNotIn("--force", envelope["remediation"], envelope)

            # Following the old advice a second time. Different bytes, identical refusal.
            self.rebuild_the_normalized_record(workspace, source_id, "2026-08-20T09:00:01Z")
            self.assertNotEqual(rebuilt, record.read_bytes(), "the second rebuild rewrote nothing")
            again = self.assert_rebuilt_reuse_is_refused(workspace, order["action_id"], source_id)
            self.assertEqual(
                failure["repair"],
                next(
                    item
                    for item in again["details"]["reconciliation_failures"]
                    if item["source_id"] == source_id
                )["repair"],
                "the second rebuild reached a different refusal, so the loop is not reproduced",
            )

            # The repair the failure names, performed literally: the record restored to the
            # bytes the order fingerprinted.
            record.write_bytes(fingerprinted)

            code, session = self.submit(workspace, order["action_id"])

            self.assertEqual(
                0,
                code,
                f"restoring the fingerprinted bytes is the advised repair and must resolve it: {session}",
            )
            self.assertEqual("research", session["phase"])
            self.assertEqual(order["action_id"], session["last_completed_action_id"])

    # -- the reuse path that is allowed, and what it may still not touch ------------

    def test_the_documented_reuse_path_closes_the_loop(self):
        """The leg the delegated arm keeps on purpose, walked end to end for the first time.

        `matching_normalized_source_records` explains in its own docstring that correlating
        on a candidate id would "silently remove the reuse path the provider mode has".
        Nothing checked that the path it preserves actually reaches the end, and the cost
        is measurable: neutralising all four of this arm's scope guards -- manifest,
        reconciliation, normalized and raw -- on 0.5.1 leaves
        `tests/test_orchestration_controller.py` and this file passing, 197 of 197. A
        repair of EW-BUG-005 that widened those guards instead of naming the constraint
        would have landed green with nobody the wiser.

        Reuse is read-only, which is why the raw tree, the normalized tree and the manifest
        must be byte-identical afterwards -- the only durable changes an accepted reuse
        makes are to the request store and the question it unblocks.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            source_id = self.deliver_before_the_order(workspace, request_id)
            self.start(workspace)
            order = self.pending_order(workspace)
            self.assertEqual([request_id], order["scope"]["request_ids"])
            evidence_before = self.evidence_bytes(workspace)

            self.fulfil_and_reopen(workspace, request_id, source_id)
            code, session = self.submit(workspace, order["action_id"])

            self.assertEqual(
                0,
                code,
                "a source the order itself correlated to the request must satisfy it "
                f"without being fetched again: {session}",
            )
            self.assertEqual("research", session["phase"])
            self.assertEqual(order["action_id"], session["last_completed_action_id"])
            self.assertEqual("open", self.question_status(workspace))
            self.assertEqual(
                evidence_before,
                self.evidence_bytes(workspace),
                "reuse fetches nothing, so it writes nothing under raw/, normalized/ or the manifest",
            )

    def test_editing_a_reused_records_manifest_entry_is_still_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, _, source_id, order = self.arrive_at_reuse(Path(tmpdir))
            manifest = workspace / "sources" / "manifest.jsonl"
            records = [
                json.loads(line)
                for line in manifest.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            for record in records:
                if record["id"] == source_id:
                    record["provenance"]["retrieved_by"] = "tidied-up-after-the-fact"
            manifest.write_text(
                "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
            )

            envelope = self.assert_reuse_mutation_refused(workspace, order["action_id"])

            self.assertIn(
                "evidence-manifest records outside fulfilled source scope", envelope["message"]
            )
            self.assertEqual(
                [source_id],
                envelope["details"]["manifest_scope_violations"]["changed_outside_scope"],
            )

    def test_editing_a_reused_sources_raw_sidecar_is_still_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, _, _, order = self.arrive_at_reuse(Path(tmpdir))
            relative = f"raw/data/{PAYLOAD.name}.provenance.yml"
            sidecar = workspace / relative
            content = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
            content["note"] = "annotated after the order was issued"
            sidecar.write_text(yaml.safe_dump(content, sort_keys=False), encoding="utf-8")

            envelope = self.assert_reuse_mutation_refused(workspace, order["action_id"])

            # Named as what it is: an edit to raw evidence that existed at issuance, rather
            # than as a delivery outside the fulfilled source scope. The two used to share a
            # refusal, and the shared wording described only the other half.
            self.assertIn(
                "changed or removed raw evidence that existed when the order was issued",
                envelope["message"],
                envelope,
            )
            self.assertEqual(
                [relative], envelope["details"]["raw_scope_violations"]["changed_outside_scope"]
            )

    def test_editing_a_reused_sources_normalized_record_is_still_refused(self):
        """Two guards can answer here and either honours the obligation.

        Reconciliation compares the reused record's normalized digest against the order's
        own baseline and speaks first; the normalized-scope guard behind it says the same
        thing about the same file. Asserted on the pair, as elsewhere in this file, because
        the promise is that the edit is refused rather than that a named check is the one
        that noticed.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, _, source_id, order = self.arrive_at_reuse(Path(tmpdir))
            record = self.normalized_record_for(workspace, source_id)
            record.write_text(
                record.read_text(encoding="utf-8") + "\nAppended after the order was issued.\n",
                encoding="utf-8",
            )

            envelope = self.assert_reuse_mutation_refused(workspace, order["action_id"])

            self.assertIn(
                envelope["message"],
                {
                    "pre-existing fulfilled evidence is not an unchanged exact scoped reconciliation match",
                    "delegated acquisition changed normalized evidence outside newly fulfilled source scope",
                },
                envelope,
            )

    def test_editing_an_uninvolved_sources_normalized_record_is_still_refused(self):
        """The normalized-scope guard, pinned where reconciliation cannot reach it.

        Reconciliation only walks the fulfilled source ids, so a workspace holding other
        evidence relies entirely on `mutable_ids=set()` at the normalized-scope site to
        keep it out of an acquisition's reach. Nothing exercised that site before.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            bystander = self.deliver_bystander_before_the_order(workspace)
            source_id = self.deliver_before_the_order(workspace, request_id)
            self.start(workspace)
            order = self.pending_order(workspace)
            self.fulfil_and_reopen(workspace, request_id, source_id)

            record = self.normalized_record_for(workspace, bystander)
            record.write_text(
                record.read_text(encoding="utf-8") + "\nRewritten by an order that never scoped it.\n",
                encoding="utf-8",
            )

            envelope = self.assert_reuse_mutation_refused(workspace, order["action_id"])

            self.assertEqual(
                "delegated acquisition changed normalized evidence outside newly fulfilled source scope",
                envelope["message"],
                envelope,
            )
            self.assertEqual(
                [record.relative_to(workspace).as_posix()],
                envelope["details"]["normalized_scope_violations"]["changed_outside_scope"],
            )

    # -- helpers -------------------------------------------------------------------

    BYSTANDER_NAME = "keepa-b0zzz98765.json"
    BYSTANDER_BODY = (
        '{\n  "asin": "B0ZZZ98765",\n  "supplier_quote": "8.10 EUR",\n  "offer_count": 2\n}\n'
    )

    def deliver_bystander_before_the_order(self, workspace: Path) -> str:
        """Unrelated evidence the workspace happens to hold. No request ever names it."""
        destination = workspace / "raw" / "data"
        destination.mkdir(parents=True, exist_ok=True)
        payload = destination / self.BYSTANDER_NAME
        payload.write_text(self.BYSTANDER_BODY, encoding="utf-8", newline="\n")
        (destination / (self.BYSTANDER_NAME + ".provenance.yml")).write_text(
            yaml.safe_dump(
                {
                    "origin_url": "https://api.keepa.test/product/B0ZZZ98765",
                    "license": "CC-BY-4.0",
                    "retrieved_at": "2026-08-08T12:00:00Z",
                    "retrieved_by": "some-earlier-run",
                    "checksum": f"sha256:{hashlib.sha256(payload.read_bytes()).hexdigest()}",
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        self.run_script(INVENTORY, ["--report"], workspace)
        self.run_script(NORMALIZE, ["--all"], workspace)
        return self.source_id_for(workspace, f"raw/data/{self.BYSTANDER_NAME}")

    def arrive_at_reuse(self, root: Path) -> tuple[Path, str, str, dict]:
        """A correlated pre-existing source, fulfilled inside the order that correlated it."""
        workspace, request_id = self.make_workspace(root)
        source_id = self.deliver_before_the_order(workspace, request_id)
        self.start(workspace)
        order = self.pending_order(workspace)
        self.fulfil_and_reopen(workspace, request_id, source_id)
        return workspace, request_id, source_id, order

    def assert_reuse_mutation_refused(self, workspace: Path, action_id: str) -> dict:
        code, envelope = self.submit(workspace, action_id)
        self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
        self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", envelope["error_code"])
        self.assertTrue(envelope["recoverable"])
        return envelope


class ControllerAuthorisedReuseTests(DelegatedWorkspace, unittest.TestCase):
    """The one reuse the controller authorises, and the reuse it deliberately still refuses.

    The sibling class walks the leg that succeeds only when the pre-existing source was
    *both* stamped for the scoped request and already normalized when the order was issued.
    One workflow the CLI accepts is shut out by the second half of that and has no
    workaround: a source whose sidecar names the scoped request exactly, delivered and
    inventoried under an order that failed, timed out, or rolled over before it normalized
    anything. Its correlation is perfect. Re-delivering does not help, because the id is
    already in the manifest, so a second raw path is a different record and leaves the
    original fulfilling nothing.

    That case is now admitted, on terms the controller fixes at issuance: the order lists
    the source as correlated-but-un-normalized, and the acquirer may write the one
    normalized record it owes -- a record the verifier re-derives from the unchanged raw
    evidence and compares byte for byte, because "the file is new" is a statement about a
    path and the workspace quotes bodies, not paths.

    Everything else stays shut, and the tests below say so in the same class so the boundary
    is read in one place. Evidence stamped for another request, or for none at all, is not
    reusable and is not made reusable by the workspace declaring `--scope`: correlation is
    written by the acquirer, so a predicate over it authorizes nothing. The recourse for
    those is a second delivery under a fresh raw path, or an honest attempt failure -- and
    for an arXiv `paper:`, a `link:` or a GitHub `codebase:`, whose ids are stable across
    deliveries, the recourse is the attempt failure alone. That residual gap is open on
    purpose; closing it needs an authorization from a trusted party, which this package does
    not have.
    """

    REUSE_REFUSAL = (
        "reuses pre-existing evidence that was not a scoped reconciliation match "
        "when the order was issued"
    )

    def assert_reuse_refused(self, envelope: dict, source_id: str, cause: str) -> dict:
        self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", envelope["error_code"], envelope)
        self.assertIn(self.REUSE_REFUSAL, envelope["message"], envelope)
        failure = next(
            item
            for item in envelope["details"]["reuse_scope_failures"]
            if item["source_id"] == source_id
        )
        self.assertEqual(cause, failure["cause"], envelope)
        return failure

    def arrive_at_unnormalized_reuse(self, root: Path) -> tuple[Path, str, str, dict]:
        """The state a prior order leaves when it delivered and inventoried, then stopped."""
        workspace, request_id = self.make_workspace(root)
        source_id = self.deliver_before_the_order(workspace, request_id, normalize=False)
        self.start(workspace)
        order = self.pending_order(workspace)
        return workspace, request_id, source_id, order

    def normalize_and_close(self, workspace: Path, request_id: str, source_id: str) -> None:
        self.run_script(
            REQUESTS, ["fulfill", "--request-id", request_id, "--source-id", source_id], workspace
        )
        self.run_script(NORMALIZE, ["--all"], workspace)
        self.run_script(
            RESOLVE,
            [
                "reopen", "--slug", QUESTION_SLUG, "--agent-id", ACQUIRER,
                "--source-id", source_id, "--request-id", request_id,
            ],
            workspace,
        )

    def arrive_at_cross_request_reuse(self, root: Path) -> tuple[Path, str, str, str, dict]:
        """Evidence stamped for -- and already spent on -- a request this order does not scope."""
        workspace, scoped_request_id = self.make_workspace(root)
        earlier_request_id = self.add_request(workspace, "Supplier quote captured for an earlier purpose")
        # No session is live yet, so the delegation gate does not apply: this is the
        # workspace an operator hands to the acquirer, not a mutation inside an order.
        source_id = self.deliver_before_the_order(workspace, earlier_request_id)
        self.run_script(
            REQUESTS,
            ["fulfill", "--request-id", earlier_request_id, "--source-id", source_id],
            workspace,
        )
        self.start(workspace)
        order = self.pending_order(workspace)
        self.assertEqual(
            [scoped_request_id],
            order["scope"]["request_ids"],
            "the reproduction needs an order that scopes only the unserved request",
        )
        return workspace, scoped_request_id, earlier_request_id, source_id, order

    # -- the reuse this change opens ------------------------------------------------

    def test_a_correlated_source_normalized_inside_the_order_can_fulfil_it(self):
        """The documented reuse path's own primary use case: delivered, inventoried, dropped.

        An acquirer that delivered and inventoried under a prior order and then failed
        before normalizing leaves exactly this state. The sidecar names the request; only
        the normalized output is missing, and producing it is what the acquirer is being
        asked to do. There is no second raw path that helps, because the id is already in
        the manifest.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, source_id, order = self.arrive_at_unnormalized_reuse(Path(tmpdir))
            raw_before = self.evidence_bytes(workspace)

            self.normalize_and_close(workspace, request_id, source_id)
            code, session = self.submit(workspace, order["action_id"])

            self.assertEqual(
                0,
                code,
                "normalizing a source the order already correlated is the work the order "
                f"asked for, not an unauthorised write: {session}",
            )
            self.assertEqual("research", session["phase"])
            self.assertEqual("open", self.question_status(workspace))
            # Exactly one new normalized record, and nothing under raw/ or in the manifest.
            after = self.evidence_bytes(workspace)
            self.assertEqual(
                [self.normalized_record_for(workspace, source_id).relative_to(workspace).as_posix()],
                sorted(set(after) - set(raw_before)),
            )
            self.assertEqual(
                dict(raw_before.items()),
                {path: after[path] for path in raw_before},
                "normalizing rewrites neither the raw delivery nor its manifest record",
            )

    def test_the_order_lists_only_the_correlated_un_normalized_source(self):
        """What the baseline holds, read from the protected sidecar rather than inferred.

        The list is the whole authorization, so it is worth reading directly: one id, the
        source whose own sidecar names this order's request, and nothing else the manifest
        happens to contain.

        "Nothing else" needs a second candidate to mean anything, so the workspace also
        holds an un-normalized delivery correlated to a request this order does not scope —
        the same shape as the admitted source in every respect except which request its
        sidecar names. A baseline built from "un-normalized manifest records" rather than
        from "un-normalized manifest records this order's scope correlates" would list it,
        and would thereby authorize a normalized output for evidence the order never asked
        anyone to touch.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            elsewhere = self.add_request(workspace, "Delivered under an order that is already closed")
            bystander = self.deliver_unnormalized_bystander(workspace, elsewhere)
            # Fulfilled before the session exists, so routing leaves it out of the order's
            # scope: an *open* request with no question behind it is still scoped, and a
            # scoped request would put its source in the baseline legitimately.
            self.run_script(
                REQUESTS, ["fulfill", "--request-id", elsewhere, "--source-id", bystander], workspace
            )
            source_id = self.deliver_before_the_order(workspace, request_id, normalize=False)
            self.start(workspace)
            order = self.pending_order(workspace)
            self.assertEqual([request_id], order["scope"]["request_ids"], order)

            baseline = json.loads(
                CONTROLLER.scope_integrity_baseline_path(
                    workspace, ORCHESTRATION_ID, order["action_id"]
                ).read_text(encoding="utf-8")
            )
            fields = next(
                item["fields"]
                for item in baseline["postconditions"]
                if item["check"] == "manifest_records_increased"
            )

            self.assertEqual([source_id], fields["reusable_source_ids_before"])
            self.assertEqual({}, fields["matching_source_records_before"])
            self.assertIn(source_id, fields["manifest_record_fingerprints_before"])
            self.assertIn(
                bystander,
                fields["manifest_record_fingerprints_before"],
                "the reproduction needs the bystander to be in the manifest at issuance",
            )
            self.assertNotIn(bystander, fields["reusable_source_ids_before"])

    def test_a_reused_source_without_its_normalized_record_is_still_refused(self):
        """The other direction, which stays shut: reuse is not an exemption from normalizing.

        Admitting an un-normalized source into the reuse baseline authorizes its normalized
        output; it does not stop requiring one. So the acquirer that normalizes far enough
        to reopen the question and then loses the record still owes it, and the refusal has
        to name the source rather than the reuse.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, source_id, order = self.arrive_at_unnormalized_reuse(Path(tmpdir))
            self.normalize_and_close(workspace, request_id, source_id)
            self.normalized_record_for(workspace, source_id).unlink()

            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", envelope["error_code"])
            self.assertEqual(
                "fulfilled source requests do not have normalized evidence", envelope["message"]
            )
            self.assertEqual([source_id], envelope["details"]["source_ids"])

    # -- what an authorised reuse still may not do ---------------------------------

    def test_an_authorised_reuse_may_not_author_its_own_normalized_body(self):
        """The record has to be *derived*, not merely new.

        This is the whole difference between authorizing a path and authorizing a body. The
        raw bytes, the sidecar and the manifest record are all pinned byte-for-byte by
        guards this order never relaxes; the normalized record is the one artifact the reuse
        lets the acquirer create, and it is the artifact every downstream reader quotes. So
        the verifier re-normalizes the unchanged raw evidence itself and compares.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, source_id, order = self.arrive_at_unnormalized_reuse(Path(tmpdir))
            self.normalize_and_close(workspace, request_id, source_id)
            record = self.normalized_record_for(workspace, source_id)
            record.write_text(
                record.read_text(encoding="utf-8").replace(
                    "## Extracted Text", "## Extracted Text\n\nThe supplier quote is $1.00."
                ),
                encoding="utf-8",
            )

            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            self.assertEqual(
                "pre-existing fulfilled evidence is not an unchanged exact scoped reconciliation match",
                envelope["message"],
                envelope,
            )
            failure = next(
                item
                for item in envelope["details"]["reconciliation_failures"]
                if item["source_id"] == source_id
            )
            self.assertTrue(failure["was_authorized_unnormalized"], failure)
            self.assertTrue(failure["record_unchanged"], failure)
            self.assertEqual(
                "normalized evidence is not what normalizing the raw evidence produces",
                failure["derivation_failure"]["reason"],
                failure,
            )

    def reconciliation_derivation_failure(self, workspace: Path, action_id: str, source_id: str) -> dict:
        """Submit, insist the reuse was refused by re-derivation, and return that failure."""
        code, envelope = self.submit(workspace, action_id)
        self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
        self.assertEqual(
            "pre-existing fulfilled evidence is not an unchanged exact scoped reconciliation match",
            envelope["message"],
            envelope,
        )
        failure = next(
            item
            for item in envelope["details"]["reconciliation_failures"]
            if item["source_id"] == source_id
        )
        self.assertTrue(failure["was_authorized_unnormalized"], failure)
        self.assertTrue(failure["record_unchanged"], failure)
        self.assertTrue(failure["derivation_checked"], failure)
        return failure

    def test_a_reused_record_naming_another_normalizer_is_refused_by_that_name(self):
        """A producer identity the workspace does not configure is its own refusal.

        `carry_version_stamps` carries `normalizer.version` out of the file so a host upgrade
        between issuance and submission is not read as a forgery, and deliberately leaves
        `normalizer.name` derived so a record cannot claim a producer that did not produce it.
        Nothing said so, though: a stamped name that disagreed simply rendered different bytes
        and was reported as "normalized evidence is not what normalizing the raw evidence
        produces", whose remediation is about a hand-edited body. The operator was sent to
        re-read prose while the two lines that disagreed sat in the frontmatter, unquoted.

        Both sides are named now, which is the difference between a verdict and a diff an
        operator has to reconstruct.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, source_id, order = self.arrive_at_unnormalized_reuse(Path(tmpdir))
            self.normalize_and_close(workspace, request_id, source_id)
            record = self.normalized_record_for(workspace, source_id)
            stamped = record.read_text(encoding="utf-8")
            configured = f"normalizer:\n  name: {ADAPTER_NAME}\n"
            self.assertIn(configured, stamped, "the fixture no longer stamps the configured adapter")
            record.write_text(
                stamped.replace(configured, "normalizer:\n  name: retired-normalize\n", 1),
                encoding="utf-8",
                newline="\n",
            )

            failure = self.reconciliation_derivation_failure(
                workspace, order["action_id"], source_id
            )
            derivation = failure["derivation_failure"]

            self.assertEqual(
                "normalized evidence does not name the normalizer configured for its kind",
                derivation["reason"],
                derivation,
            )
            self.assertEqual("retired-normalize", derivation["normalizer"], derivation)
            self.assertEqual(
                ADAPTER_NAME,
                derivation["configured_normalizer"],
                "the refusal must name both identities, or the operator cannot see which moved",
            )
            # Record-side, so the rewrite is the repair: re-deriving restamps the configured
            # identity, which is precisely what this record no longer carries.
            self.assertEqual(
                CONTROLLER.RECONCILIATION_ARM_REPAIRS["authorized_unnormalized"],
                failure["repair"],
                failure,
            )

    def test_an_adapter_reporting_an_unauthorised_identity_is_reported_as_the_adapter(self):
        """The verifier's own tooling failing is not the acquirer having authored a body.

        `research.yml` names the adapter and is pinned by the trusted-input guard, but the
        program that name resolves to is not: an adapter redeployed on PATH between the
        acquirer's `normalize_sources.py` run and the verifier's re-derivation is outside
        everything this order fingerprints. The protocol catches it precisely — the run
        reports an identity `research.yml` does not authorize and `AdapterError` says exactly
        that — and the controller's blanket `except` then reported "normalized evidence could
        not be re-derived from the raw evidence" and buried the sentence in `error`. The
        acquirer's record is fine; the host is not, and only one of those is actionable.

        The reuse is still refused, and must be: an identity the workspace never authorized is
        not something to accept evidence on. What changes is that the refusal says whose
        problem it is.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, source_id, order = self.arrive_at_unnormalized_reuse(Path(tmpdir))
            self.normalize_and_close(workspace, request_id, source_id)

            # The redeploy: same argv, same research.yml, a program now reporting itself as
            # something the workspace never authorized.
            with mock.patch.dict(os.environ, {"EW_STUB_NAME": "stub-normalize-ng"}):
                failure = self.reconciliation_derivation_failure(
                    workspace, order["action_id"], source_id
                )
            derivation = failure["derivation_failure"]

            self.assertEqual(
                "the normalizer adapter this workspace authorizes did not produce a record to compare against",
                derivation["reason"],
                derivation,
            )
            self.assertIn("stub-normalize-ng", derivation["error"], derivation)
            self.assertIn("research.yml authorized", derivation["error"], derivation)
            # And the repair follows the verdict rather than the arm alone. Rewriting the
            # record runs the same adapter and fails in the same place, so this state is not
            # sent to the rewrite: the loop being closed here is the one the arm repairs close.
            self.assertEqual(
                CONTROLLER.RECONCILIATION_ARM_REPAIRS["authorized_unnormalized_unverifiable"],
                failure["repair"],
                failure,
            )
            self.assertNotIn("--force", failure["repair"], failure)

            # The control: with the authorized program back on PATH the same reuse is
            # accepted, so the refusal above is about the adapter and not about the record.
            code, session = self.submit(workspace, order["action_id"])

            self.assertEqual(0, code, session)
            self.assertEqual("research", session["phase"])

    def test_an_authorised_reuse_may_not_author_its_structured_view_sidecar(self):
        """The sidecar is bound by the same re-derivation, not by the record's own digest.

        A record that binds a structured view names the sidecar's digest in its own
        frontmatter, so an acquirer editing both together keeps the pair self-consistent and
        every check that reads only the record agrees. The binding that survives that is the
        one taken back to the raw bytes, which is why re-derivation renders the sidecar too.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            source_id = self.deliver_table_before_the_order(workspace, request_id)
            self.start(workspace)
            order = self.pending_order(workspace)
            self.normalize_and_close(workspace, request_id, source_id)
            record = self.normalized_record_for(workspace, source_id)
            sidecar = record.with_name(record.stem + ".structured.json")
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            payload["rows"] = []
            rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            sidecar.write_text(rendered, encoding="utf-8", newline="\n")
            # Restamp the record's own binding too, so every check that reads only the
            # record still agrees and only the re-derivation can tell. The nested
            # `content_hash:` under `structured_view:` carries *two* spaces of indent —
            # `yaml.safe_dump(default_flow_style=False)` indents a nested mapping by two —
            # and the guard is what keeps the record's own top-level `content_hash:` out of
            # the substitution.
            invented = "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()
            before = record.read_text(encoding="utf-8")
            restamped = "\n".join(
                f"{line.split('content_hash:', 1)[0]}content_hash: {invented}"
                if line.strip().startswith("content_hash:") and line.startswith("  ")
                else line
                for line in before.split("\n")
            )
            self.assertNotEqual(
                before,
                restamped,
                "the scenario needs the record's declared sidecar digest restamped; an "
                "indentation assumption that matches nothing rewrites the file byte-for-byte "
                "and leaves the record disagreeing with the sidecar, which a cheaper check "
                "than re-derivation would then catch",
            )
            record.write_text(restamped, encoding="utf-8", newline="\n")

            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            self.assertEqual(
                "pre-existing fulfilled evidence is not an unchanged exact scoped reconciliation match",
                envelope["message"],
                envelope,
            )
            failure = next(
                item
                for item in envelope["details"]["reconciliation_failures"]
                if item["source_id"] == source_id
            )
            self.assertEqual(
                "the structured-view sidecar is not what normalizing the raw evidence produces",
                failure["derivation_failure"]["reason"],
                failure,
            )

    def test_editing_the_manifest_record_of_an_authorised_reuse_is_still_refused(self):
        """The baseline names a source id; it does not bless whatever that id holds now.

        An authorised reuse is authorised on the manifest record the order fingerprinted at
        issuance, so rewriting that record afterwards has to be refused exactly as an
        unauthorised reuse is. Without it the baseline would be an opening rather than an
        authorization: the id would stay admitted while the provenance the correlation was
        read out of quietly moved underneath it.

        The guard that answers is the manifest-scope one, which compares every record
        against the issuance snapshot and speaks before reconciliation reaches this source
        at all. So the assertion is on that refusal, exactly: its message and the one id in
        its `changed_outside_scope` list. (The docstring used to name reconciliation's
        `record_unchanged` branch instead, and asserted only that the source id appeared
        somewhere in `details` -- which every acquisition refusal that lists a fulfilled
        source satisfies. Rewritten to the refusal that fires rather than made to fire the
        one it named, because the manifest guard is upstream by design: reconciliation
        re-derives content, and re-deriving against a manifest record this order may not
        have touched is exactly what the earlier guard exists to prevent.)
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, source_id, order = self.arrive_at_unnormalized_reuse(Path(tmpdir))
            self.normalize_and_close(workspace, request_id, source_id)
            manifest = workspace / "sources" / "manifest.jsonl"
            records = [
                json.loads(line)
                for line in manifest.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            for record in records:
                if record["id"] == source_id:
                    record["provenance"]["retrieved_by"] = "tidied-up-after-the-fact"
            manifest.write_text(
                "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
            )

            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", envelope["error_code"])
            self.assertEqual(
                "delegated acquisition changed, removed, or added evidence-manifest records "
                "outside fulfilled source scope",
                envelope["message"],
                envelope,
            )
            self.assertEqual(
                {
                    "removed": [],
                    "added_outside_scope": [],
                    "changed_outside_scope": [source_id],
                },
                envelope["details"]["manifest_scope_violations"],
                "the refusal must name the source whose record moved, and only that one",
            )

    def test_editing_the_raw_sidecar_of_an_authorised_reuse_is_still_refused(self):
        """`raw/` stays immutable for an authorised reuse exactly as for everything else."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, source_id, order = self.arrive_at_unnormalized_reuse(Path(tmpdir))
            self.normalize_and_close(workspace, request_id, source_id)
            relative = f"raw/data/{PAYLOAD.name}.provenance.yml"
            sidecar = workspace / relative
            content = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
            content["retrieved_by"] = "someone-else-entirely"
            sidecar.write_text(yaml.safe_dump(content, sort_keys=False), encoding="utf-8")

            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            # Named as what it is: an edit to raw evidence that existed at issuance, rather
            # than as a delivery outside the fulfilled source scope. The two used to share a
            # refusal, and the shared wording described only the other half.
            self.assertIn(
                "changed or removed raw evidence that existed when the order was issued",
                envelope["message"],
                envelope,
            )
            self.assertEqual(
                [relative], envelope["details"]["raw_scope_violations"]["changed_outside_scope"]
            )

    def test_an_authorised_reuse_may_not_write_a_second_unrelated_normalized_file(self):
        """One record for the source the order authorized, and nothing else beside it.

        The reuse widens what an action may *create* by exactly one record per reused
        source. A normalized file no fulfilled source accounts for is still refused, and the
        refusal still names the file, or the widening would have been a directory rather
        than a record.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, source_id, order = self.arrive_at_unnormalized_reuse(Path(tmpdir))
            self.normalize_and_close(workspace, request_id, source_id)
            stray = workspace / "sources" / "normalized" / "invented-beside-the-record.md"
            stray.write_text("---\ntype: normalized_source\n---\n\nInvented.\n", encoding="utf-8")

            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            self.assertEqual(
                "delegated acquisition changed normalized evidence outside newly fulfilled source scope",
                envelope["message"],
                envelope,
            )
            self.assertEqual(
                ["sources/normalized/invented-beside-the-record.md"],
                envelope["details"]["normalized_scope_violations"]["added_outside_scope"],
            )

    def test_a_reuse_normalized_at_issuance_authorises_no_new_normalized_file(self):
        """The arm boundary, asserted from the side that must not move.

        Authorizing a normalized output for a source the order recorded as *not* normalized
        is the whole of what this change adds. A source that already had one keeps answering
        to byte-identity and authorizes nothing new at all, so any file appearing under
        `sources/normalized/` during such an order is refused. If the two ever merged, the
        already-normalized reuse would quietly stop being pinned to its digest.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            source_id = self.deliver_before_the_order(workspace, request_id)
            self.start(workspace)
            order = self.pending_order(workspace)
            self.fulfil_and_reopen(workspace, request_id, source_id)
            stray = workspace / "sources" / "normalized" / "second-thoughts.md"
            stray.write_text("---\ntype: normalized_source\n---\n\nInvented.\n", encoding="utf-8")

            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            self.assertEqual(
                "delegated acquisition changed normalized evidence outside newly fulfilled source scope",
                envelope["message"],
                envelope,
            )
            self.assertEqual(
                ["sources/normalized/second-thoughts.md"],
                envelope["details"]["normalized_scope_violations"]["added_outside_scope"],
            )

    # -- the reuse this change deliberately leaves shut ----------------------------

    def test_evidence_stamped_for_an_earlier_request_is_still_refused(self):
        """A source id that cannot be earned twice, needed by a second request.

        The workspace holds the evidence and `source_requests.py fulfill` accepts it, so
        this reads like the reuse case above. It is not, and the difference is who wrote the
        link: `provenance.request_id` names the earlier request, and the only ways to make
        it name this one are to restamp the sidecar -- which the acquirer skill forbids,
        because it orphans the first request's link with nothing to detect it -- or to admit
        the source on some predicate other than correlation. Every field such a predicate
        could read is written by the same untrusted party, so it would authorize nothing.
        The refusal names the cause, and the recourse is a second delivery or an honest
        attempt failure.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, _, source_id, order = self.arrive_at_cross_request_reuse(
                Path(tmpdir)
            )
            evidence_before = self.evidence_bytes(workspace)

            self.fulfil_and_reopen(workspace, request_id, source_id)
            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            failure = self.assert_reuse_refused(envelope, source_id, "provenance_names_no_scoped_request")
            self.assertTrue(failure["record_unchanged"], failure)
            self.assertEqual([], envelope["details"]["reusable_source_ids_before"])
            self.assertEqual(
                evidence_before,
                self.evidence_bytes(workspace),
                "a refused reuse leaves the evidence exactly as it found it",
            )

    def test_a_record_with_no_provenance_at_all_is_still_refused(self):
        """Evidence correlated to nothing closes nothing.

        A file dropped into `raw/` and inventoried without a sidecar has a real manifest
        record and `provenance: null`. Nothing links it to any request, which is precisely
        why it may not satisfy one -- and a reuse rule that reads any other field would
        admit it, because there is no other field a delivery does not control.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            destination = workspace / "raw" / "data"
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "unattributed.json").write_text(
                json.dumps({"quote": "from nowhere in particular"}) + "\n", encoding="utf-8"
            )
            self.run_script(INVENTORY, ["--report"], workspace)
            self.run_script(NORMALIZE, ["--all"], workspace)
            source_id = self.source_id_for(workspace, "raw/data/unattributed.json")
            self.start(workspace)
            order = self.pending_order(workspace)
            evidence_before = self.evidence_bytes(workspace)

            self.fulfil_and_reopen(workspace, request_id, source_id)
            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            failure = self.assert_reuse_refused(envelope, source_id, "provenance_names_no_scoped_request")
            self.assertIsNone(failure["provenance_request_id"], failure)
            self.assertEqual(evidence_before, self.evidence_bytes(workspace))

    def test_declaring_scope_does_not_admit_a_source_that_omits_the_key(self):
        """A workspace that adopted `--scope` is protected by correlation, not by the scope.

        Scope agreement is a filter over what a request *contradicts*, and a source that
        declares no facet contradicts nothing, so a scope-declaring request agrees with
        uncorrelated evidence as readily as an undeclared one does. Recording that here
        because it is the reason reuse is not gated on scope agreement: the gate would read
        as protection and be vacuous by construction.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            requests_path = workspace / "sources" / "source-requests.jsonl"
            records = [
                json.loads(line)
                for line in requests_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            for record in records:
                if record["request_id"] == request_id:
                    record["scope"] = {"facet": "pricing"}
            requests_path.write_text(
                "".join(
                    json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
                    for record in records
                ),
                encoding="utf-8",
            )
            source_id = self.deliver_before_the_order(workspace, None)
            self.start(workspace)
            order = self.pending_order(workspace)

            self.fulfil_and_reopen(workspace, request_id, source_id)
            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            self.assert_reuse_refused(envelope, source_id, "provenance_names_no_scoped_request")

    def test_one_delivery_cannot_close_the_rest_of_the_backlog_in_a_later_order(self):
        """The escalation an acquirer can build entirely out of its own successful order.

        Nothing here is tampering. Order one fetches one source honestly and reports two
        honest retryable failures; both outcomes are accepted. That leaves the workspace
        holding a source the acquirer delivered, and order two is issued over the backlog
        that is left. If pre-existing evidence were reusable on anything but its own
        correlation, the acquirer would close the rest of the backlog with it, fetching
        nothing -- and the second question would end up open, citing a supplier-quote
        snapshot as its evidence for what competitors charge. That is wiki grounding
        corruption rather than request-store bookkeeping, which is why it is the case worth
        keeping a test on.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, requests = self.build_backlog(Path(tmpdir))
            self.start(workspace)
            code, first = self.next_action(workspace)
            self.assertEqual(0, code, first)

            source_id = self.deliver_for(workspace, requests["fulfilled"])
            self.run_script(
                REQUESTS,
                ["fulfill", "--request-id", requests["fulfilled"], "--source-id", source_id],
                workspace,
            )
            self.run_script(
                RESOLVE,
                [
                    "reopen", "--slug", QUESTION_SLUG, "--agent-id", ACQUIRER,
                    "--source-id", source_id, "--request-id", requests["fulfilled"],
                ],
                workspace,
            )
            for name in ("throttled", "refused"):
                self.record_failure(workspace, requests[name], "provider_throttled", first["action_id"])
            code, payload = self.submit(workspace, first["action_id"])
            self.assertEqual(0, code, payload)

            # Routing prefers research, so the reopened question is answered to get the
            # session back to acquisition -- the acquirer's own honest work, throughout.
            code, research = self.next_action(workspace)
            self.assertEqual("research", research["phase"], research)
            page = workspace / "wiki" / "synthesis" / "supplier-quote.md"
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text(
                "---\ntype: synthesis\ncreated: 2026-08-09\nupdated: 2026-08-09\n"
                f"source_ids:\n  - {source_id}\n"
                "summary: The delivered snapshot carries the supplier quote.\n---\n\n"
                "# Supplier quote\n\nThe delivered snapshot answers the question.\n",
                encoding="utf-8",
            )
            self.run_script(
                RESOLVE,
                [
                    "answer", "--slug", QUESTION_SLUG, "--agent-id", "research-agent",
                    "--answer-page", "wiki/synthesis/supplier-quote.md",
                    "--source-id", source_id, "--allow-unclaimed",
                ],
                workspace,
            )
            code, payload = self.submit(workspace, research["action_id"])
            self.assertEqual(0, code, payload)

            code, second = self.next_action(workspace)
            self.assertEqual("acquisition", second["phase"], second)
            evidence_before = self.evidence_bytes(workspace)
            argv = ["reopen", "--slug", self.SECOND_SLUG, "--agent-id", ACQUIRER]
            for retried in second["scope"]["request_ids"]:
                self.run_script(
                    REQUESTS, ["fulfill", "--request-id", retried, "--source-id", source_id], workspace
                )
                argv.extend(["--source-id", source_id, "--request-id", retried])
            self.run_script(RESOLVE, argv, workspace)

            code, envelope = self.submit(workspace, second["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            self.assert_reuse_refused(envelope, source_id, "provenance_names_no_scoped_request")
            self.assertEqual(evidence_before, self.evidence_bytes(workspace))
            self.assertIn(
                "status: blocked",
                self.question_frontmatter(workspace, self.SECOND_SLUG),
                "the second question stays blocked rather than opening on evidence for another request",
            )


class ReusedSourceKindTests(DelegatedWorkspace, unittest.TestCase):
    """The authorised reuse, walked once per extractor the package implements.

    `ControllerAuthorisedReuseTests` walks the leg with the adapter payload the rest of
    this file delivers, and its refusals are asserted against that one kind too. But the
    thing being authorised is *re-derivation*: the verifier re-normalizes the unchanged raw
    bytes and compares, and which code path that runs is chosen by the record's kind. A leg
    exercised on one kind therefore says almost nothing about the others, and the gap has a
    measured cost -- the PDF path shipped a hole in which the record under verification
    chose the program the verifier executed, in the middle of the trust boundary, and every
    reuse fixture in the suite went through the adapter or the HTML/CSV extractors instead.

    So each kind that can reach the end walks it here: delivered and inventoried under an
    order that stopped, normalized inside the order that scopes it, submitted. `link` is
    the one that cannot, and it is asserted as the refusal it is rather than left out, so
    the boundary is written down where the successes are read.
    """

    PDF_NAME = "supplier-quote.pdf"
    PDF_LINES = (
        "Supplier quote snapshot for B0ABC12345.",
        "The listed unit price is 12.50 EUR per unit.",
        "Quoted by ACME Supply on 2026-08-17.",
    )
    LINK_NAME = "supplier-quote.url"
    LINK_URL = "https://example.test/supplier/B0ABC12345"

    # -- deliveries a prior order left inventoried and un-normalized -----------------

    def write_sidecar_for(
        self, workspace: Path, relative: str, request_id: str, origin_url: str
    ) -> None:
        """The correlation the reuse is admitted on, and nothing else.

        A directory target gets no checksum: `source_inventory.py` cannot hash a tree, and
        recording one anyway only earns a `checksum_verified: false` warning that has
        nothing to do with what these tests are about.
        """
        target = workspace / relative
        sidecar: dict[str, object] = {
            "origin_url": origin_url,
            "license": "CC-BY-4.0",
            "retrieved_at": "2026-08-17T12:00:00Z",
            "retrieved_by": ACQUIRER,
            "request_id": request_id,
        }
        if target.is_file():
            sidecar["checksum"] = f"sha256:{hashlib.sha256(target.read_bytes()).hexdigest()}"
        (workspace / f"{relative}.provenance.yml").write_text(
            yaml.safe_dump(sidecar, sort_keys=False), encoding="utf-8"
        )

    def deliver_pdf_before_the_order(self, workspace: Path, request_id: str) -> str:
        """A PDF with no LaTeX bundle beside it, so the record takes the `pdf` path."""
        relative = f"raw/papers/{self.PDF_NAME}"
        destination = workspace / "raw" / "papers"
        destination.mkdir(parents=True, exist_ok=True)
        (destination / self.PDF_NAME).write_bytes(synthetic_pdf(list(self.PDF_LINES)))
        self.write_sidecar_for(
            workspace, relative, request_id, "https://example.test/supplier-quote.pdf"
        )
        self.run_script(INVENTORY, ["--report"], workspace)
        return self.source_id_for(workspace, relative)

    def deliver_latex_before_the_order(self, workspace: Path, request_id: str) -> str:
        """A LaTeX bundle: a directory record whose `latex_root` and `entrypoint` are inputs."""
        relative = f"raw/papers/{LATEX_BUNDLE_FIXTURE.name}"
        shutil.copytree(LATEX_BUNDLE_FIXTURE, workspace / relative)
        self.write_sidecar_for(
            workspace, relative, request_id, "https://example.org/chain-handoff/synthetic-benchmark"
        )
        self.run_script(INVENTORY, ["--report"], workspace)
        return self.source_id_for(workspace, relative)

    def deliver_link_before_the_order(self, workspace: Path, request_id: str) -> str:
        """A link list: one raw file whose inventoried record is the URL, not the file."""
        relative = f"raw/links/{self.LINK_NAME}"
        destination = workspace / "raw" / "links"
        destination.mkdir(parents=True, exist_ok=True)
        (destination / self.LINK_NAME).write_text(
            f"URL={self.LINK_URL}\n", encoding="utf-8", newline="\n"
        )
        self.write_sidecar_for(workspace, relative, request_id, self.LINK_URL)
        self.run_script(INVENTORY, ["--report"], workspace)
        return self.source_id_for(workspace, relative)

    # -- the acquirer's work inside the order ----------------------------------------

    def normalize_scoped_source(self, workspace: Path, request_id: str, source_id: str) -> None:
        """Fulfil, normalize *only* this order's source, reopen.

        `--source-id` rather than `--all` on purpose: `--all` is what the rest of this file
        uses and it is only safe because those fixtures hold nothing else to normalize --
        see `NormalizeAllInsideAnOrderTests` for the state where it is not.
        """
        self.run_script(
            REQUESTS, ["fulfill", "--request-id", request_id, "--source-id", source_id], workspace
        )
        self.run_script(NORMALIZE, ["--source-id", source_id], workspace)
        self.run_script(
            RESOLVE,
            [
                "reopen", "--slug", QUESTION_SLUG, "--agent-id", ACQUIRER,
                "--source-id", source_id, "--request-id", request_id,
            ],
            workspace,
        )

    def assert_reuse_closed_the_loop(
        self, workspace: Path, order: dict, source_id: str, evidence_before: dict[str, str]
    ) -> None:
        """Accepted, routed back to research, and read-only everywhere it is required to be."""
        code, session = self.submit(workspace, order["action_id"])

        self.assertEqual(
            0,
            code,
            "normalizing a source the order already correlated is the work the order asked "
            f"for: {session}",
        )
        self.assertEqual("research", session["phase"])
        self.assertEqual(order["action_id"], session["last_completed_action_id"])
        self.assertEqual("open", self.question_status(workspace))
        after = self.evidence_bytes(workspace)
        created = sorted(set(after) - set(evidence_before))
        record = self.normalized_record_for(workspace, source_id).relative_to(workspace).as_posix()
        self.assertIn(record, created, f"the reuse owed a new normalized record; created {created}")
        self.assertEqual(
            [path for path in created if path.startswith("sources/normalized/")],
            created,
            "the only artifacts an accepted reuse creates are normalized outputs",
        )
        self.assertEqual(
            dict(evidence_before.items()),
            {path: after[path] for path in evidence_before},
            "reuse fetches nothing, so the raw tree and the manifest are byte-identical after",
        )

    def arrive_at(
        self, root: Path, deliver: Callable[[Path, str], str]
    ) -> tuple[Path, str, str, dict, dict[str, str]]:
        """The arm-(b) starting state for one kind: correlated, inventoried, un-normalized."""
        workspace, request_id = self.make_workspace(root)
        source_id = deliver(workspace, request_id)
        self.start(workspace)
        order = self.pending_order(workspace)
        self.assertEqual([request_id], order["scope"]["request_ids"], order)
        return workspace, request_id, source_id, order, self.evidence_bytes(workspace)

    # -- the kinds that reach the end -------------------------------------------------

    def test_a_reused_pdf_normalized_inside_the_order_can_fulfil_it(self):
        """The leg the suite never walked, on the path that shipped an execution hole.

        A PDF reuse is the only one whose re-derivation reads a *tool* out of the record it
        is verifying: `pdf_extractor.name` chooses which extractor re-runs. Nothing here
        tampers with anything -- this is the honest acquirer's order, end to end -- and it
        is exactly the case that was broken in both directions at once. Forwarding the
        stamped name unchecked made it `argv[0]` of a subprocess, and because a record this
        package writes stamps `pypdf`, for which no executable of that name exists, every
        legitimate PDF reuse also failed as "could not be re-derived". A suite with no PDF
        reuse fixture could not see either.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, source_id, order, before = self.arrive_at(
                Path(tmpdir), self.deliver_pdf_before_the_order
            )

            self.normalize_scoped_source(workspace, request_id, source_id)
            record = self.normalized_record_for(workspace, source_id).read_text(encoding="utf-8")
            self.assertIn(
                "name: pypdf",
                record,
                "the reproduction needs a record that names an extractor the verifier resolves",
            )
            self.assertIn(
                self.PDF_LINES[0],
                record,
                "a PDF with no extracted text is refused before reuse is ever consulted, so "
                "the fixture has to be one the extractor actually reads",
            )

            self.assert_reuse_closed_the_loop(workspace, order, source_id, before)

    def test_a_reused_table_normalized_inside_the_order_can_fulfil_it(self):
        """The native table path, which writes two files rather than one.

        A well-formed CSV earns a structured view, so the reuse authorizes a record *and*
        the sidecar that record declares. `DelegatedStructuredSourceTests` covers the pair
        for a source acquired inside the order; this is the same shape on the arm where both
        files are new to a source the workspace already held, where the allowance is a
        widening of what may be created rather than a byte-identity against a baseline.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, source_id, order, before = self.arrive_at(
                Path(tmpdir), self.deliver_table_before_the_order
            )

            self.normalize_scoped_source(workspace, request_id, source_id)
            record = self.normalized_record_for(workspace, source_id)
            self.assertTrue(
                record.with_name(record.stem + ".structured.json").is_file(),
                "the reproduction needs the record to bind a structured view",
            )

            self.assert_reuse_closed_the_loop(workspace, order, source_id, before)

    def test_a_reused_latex_bundle_normalized_inside_the_order_can_fulfil_it(self):
        """A record whose inputs are a directory tree, not a single delivered file.

        `latex_root` and `entrypoint` are separate path fields, read by the check that
        refuses inputs no raw-evidence baseline pins and by the sandbox that materialises
        them for re-derivation. Both would silently pass a record whose only input is its
        `raw_paths` entry, which is every other kind in this class.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, source_id, order, before = self.arrive_at(
                Path(tmpdir), self.deliver_latex_before_the_order
            )

            self.normalize_scoped_source(workspace, request_id, source_id)

            self.assert_reuse_closed_the_loop(workspace, order, source_id, before)

    # -- the kind that cannot, and why that is not a gap -----------------------------

    def test_a_reused_link_record_is_refused_because_a_stub_is_not_evidence(self):
        """Reuse widened what may be *written*; it did not widen what counts as evidence.

        A link record normalizes to a stub -- the package fetches no network content, so
        the record it writes carries none -- and `status: stubbed` is refused for a
        delegated fulfilment however the source arrived. Asserted here beside the successes
        because the reuse baseline admits this source exactly as it admits the others: its
        sidecar names the scoped request and nothing had normalized it. If the widening ever
        started standing in for usability, this is the case that would go quietly green and
        let a question reopen citing a URL nobody fetched.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, source_id, order, before = self.arrive_at(
                Path(tmpdir), self.deliver_link_before_the_order
            )

            self.normalize_scoped_source(workspace, request_id, source_id)
            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", envelope["error_code"])
            self.assertEqual(
                "fulfilled source requests do not have usable normalized evidence",
                envelope["message"],
                envelope,
            )
            self.assertEqual(
                [
                    {
                        "source_id": source_id,
                        "reason": "normalized evidence has unusable extraction status 'stubbed'",
                    }
                ],
                envelope["details"]["quality_failures"],
                envelope,
            )
            self.assertTrue(envelope["recoverable"])
            self.assertEqual(
                dict(before.items()),
                {path: self.evidence_bytes(workspace)[path] for path in before},
                "a refused submission leaves the evidence it read exactly as it found it",
            )

    # -- the execution hole itself ----------------------------------------------------

    def submit_recording_spawns(
        self, workspace: Path, action_id: str
    ) -> tuple[int, dict, list[list[str]]]:
        """Submit, and record every process the verification spawns while it runs.

        `normalize_sources` reads `subprocess` from the one module object the interpreter
        caches, so replacing `run` there is also seen by the copy the controller loads for
        itself — which is the copy that re-derives.
        """
        spawned: list[list[str]] = []
        real_run = NORMALIZE.subprocess.run

        def recording_run(args, *rest, **kwargs):
            spawned.append(
                [str(item) for item in args] if isinstance(args, (list, tuple)) else [str(args)]
            )
            return real_run(args, *rest, **kwargs)

        NORMALIZE.subprocess.run = recording_run
        try:
            code, envelope = self.submit(workspace, action_id)
        finally:
            NORMALIZE.subprocess.run = real_run
        return code, envelope, spawned

    def test_a_reused_pdf_record_may_not_name_the_program_that_verifies_it(self):
        """The record under verification does not get to choose the verifier's subprocess.

        `normalize_selected_record` accepts a bare `str` for its extractor, and that has one
        historical meaning: a resolved `pdftotext` executable path, which becomes `argv[0]`
        of a subprocess. Forwarding `pdf_extractor.name` out of the normalized record into
        that parameter therefore handed the acquirer arbitrary program execution *inside the
        check that exists to adjudicate what the acquirer wrote* -- the worst possible place
        for it, because the order is pending, the workspace is unlocked, and the refusal
        that would have followed arrives after the program has already run.

        So the assertion is not only that the submission is refused. It is that nothing was
        spawned: `subprocess.run` is recorded for the duration, and the name the record
        stamps is a real executable that would leave a marker file behind if it ever ran. A
        repair that refused *after* resolving, or that resolved by probing PATH, would pass
        the refusal assertions and fail these.

        "Nothing was spawned" is only worth asserting if a spawn would have been seen, so
        the record is then restored and replayed: verifying an honest PDF reuse does run a
        process (the extractor), the same recorder catches it, and the submission is
        accepted. Without that half, a verifier that had stopped re-deriving PDFs at all
        would satisfy every assertion above.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace, request_id, source_id, order, _ = self.arrive_at(
                root, self.deliver_pdf_before_the_order
            )
            self.normalize_scoped_source(workspace, request_id, source_id)

            # Outside the workspace: running it must be detectable without the marker
            # itself tripping a scope guard and refusing the submission for another reason.
            marker = root / "the-extractor-ran"
            program = root / "pretend-pdftotext.sh"
            program.write_text(f'#!/bin/sh\n: > "{marker}"\n', encoding="utf-8", newline="\n")
            program.chmod(0o755)

            record = self.normalized_record_for(workspace, source_id)
            derived = record.read_bytes()
            stamped = derived.decode("utf-8")
            self.assertIn("\n  name: pypdf\n", stamped, "the fixture no longer stamps an extractor")
            record.write_bytes(
                stamped.replace("\n  name: pypdf\n", f"\n  name: {program}\n", 1).encode("utf-8")
            )

            code, envelope, spawned = self.submit_recording_spawns(workspace, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            self.assertEqual(
                "pre-existing fulfilled evidence is not an unchanged exact scoped reconciliation match",
                envelope["message"],
                envelope,
            )
            failure = next(
                item
                for item in envelope["details"]["reconciliation_failures"]
                if item["source_id"] == source_id
            )
            self.assertEqual(
                "normalized evidence names a PDF extractor this package does not implement",
                failure["derivation_failure"]["reason"],
                failure,
            )
            self.assertEqual(
                str(program),
                failure["derivation_failure"]["pdf_extractor"],
                "the refusal must name the extractor it refused, or the acquirer cannot repair it",
            )

            self.assertFalse(
                marker.exists(),
                "the program the normalized record named was executed by the verifier",
            )
            self.assertEqual(
                [],
                [argv for argv in spawned if str(program) in argv],
                f"the record's stamped extractor reached a subprocess: {spawned}",
            )

            # The control: restored to what normalizing the raw evidence produces, the same
            # reuse is accepted -- and the same recorder sees the extractor run. So the
            # refusal above is a program that was not executed, not a check that no longer
            # executes programs.
            record.write_bytes(derived)
            code, session, honest_spawns = self.submit_recording_spawns(
                workspace, order["action_id"]
            )

            self.assertEqual(0, code, session)
            self.assertEqual("research", session["phase"])
            self.assertEqual("open", self.question_status(workspace))
            self.assertTrue(
                honest_spawns,
                "verifying a PDF reuse spawns its extractor; a recorder that sees none of "
                "them cannot testify that the tampered record spawned nothing",
            )

    # -- the subtree a reused bundle does not open ------------------------------------

    PLANTED_MEMBER = "sections/planted-appendix.tex"

    def arrive_at_a_mixed_batch(self, root: Path) -> tuple[Path, dict[str, str], str, dict]:
        """One order over two requests: one to be delivered new, one a pre-existing bundle.

        Two questions with one blocking request each, because both requests are fulfilled
        here: a question left blocked on a fulfilled request is a missing open link, which
        `workspace_status` reports as `attention_required` and which freezes the session
        before submission is ever reached.

        The mixture is the whole point and neither half can be dropped. Attribution is only
        derived when the action created at least one manifest record, so a pure-reuse order
        computes no attribution at all and admits nothing from any subtree, expanded or not.
        The reused bundle is what makes an expanded subtree exist to be over-admitted.
        """
        workspace = self.init_workspace(root)
        self.configure(workspace, delegated=True)
        self.add_second_question(workspace)
        requests = {
            "reused": self.block_question_on_a_request(workspace),
            "delivered": self.block_question_on_a_request(
                workspace, slug=self.SECOND_SLUG, query="Competitor offer snapshot"
            ),
        }
        bundle_source_id = self.deliver_latex_before_the_order(workspace, requests["reused"])
        self.start(workspace)
        order = self.pending_order(workspace)
        self.assertEqual(
            sorted(requests.values()),
            sorted(order["scope"]["request_ids"]),
            f"the guard under test only runs on a batch carrying both kinds of fulfilment: {order}",
        )
        return workspace, requests, bundle_source_id, order

    def assert_the_planted_file_is_inert_to_the_normalizer(
        self, workspace: Path, source_id: str
    ) -> None:
        """The reused bundle still re-derives, with the planted file sitting inside it.

        Reconciliation runs several checks before raw scope does, and it re-normalizes the
        reused source from its raw evidence inside a sandbox that copies the whole
        `latex_root` subtree -- planted file included. A plant the LaTeX reader picked up
        would therefore be refused as a reuse that does not re-derive, and the raw-scope
        guard this test is about would never be consulted: the test would go green on a
        refusal that says nothing about admission scope at all.

        `main.tex` neither `\\input`s nor `\\include`s the plant and the entrypoint is chosen
        from the bundle's top level, so the reader never reaches it. Asserted rather than
        assumed, through the controller's own re-derivation predicate, so that a normalizer
        that later starts reading the whole tree fails here -- with the reason -- instead of
        quietly turning the test below into a different one.
        """
        records = self.manifest_records(workspace)
        record = next(item for item in records if item["id"] == source_id)
        self.assertIsNone(
            CONTROLLER.normalized_output_derivation_failure(
                workspace,
                CONTROLLER.load_config(workspace),
                record,
                self.normalized_record_for(workspace, source_id),
                records,
                NORMALIZE,
            ),
            "the planted file changed what normalizing the reused bundle produces, so this "
            "fixture is refused by reconciliation and proves nothing about raw scope",
        )

    def test_a_completed_batch_may_not_write_into_a_reused_pre_existing_bundle(self):
        """A bundle the action reused is not a subtree it may write into either.

        `test_a_blocked_partial_delivery_may_not_write_into_a_pre_existing_bundle` pins this
        for the blocked arm, and the blocked arm's own comment says the completed arms pass
        their newly created ids alone "for the same reason". Nothing said it here: widening
        the completed arm's allowed set from the new ids to every fulfilled id left the whole
        suite green, because no fixture in it ever fulfilled one request from a new delivery
        and another from a directory-shaped record the workspace already held.

        That is the shape the hole needs. Attribution expands a directory-valued `raw_paths`
        to every regular file beneath it, which is what makes a bundle deliverable at all and
        is safe for a record the action itself created -- the acquirer wrote the whole
        subtree. Applied to a record that already existed at issuance, the same expansion
        admits anything dropped anywhere inside somebody else's payload, because inventory
        attributes files to the record whose directory contains them and asks nothing about
        who put them there.

        The batch is mixed deliberately: the new delivery is what makes attribution get
        derived at all, and the reused bundle is what gives the expansion a subtree to reach
        into. So the refusal is asserted together with the allowed set, because "refused" on
        its own is also what a fixture that never reached this guard would produce. The
        allowed set being exactly the newly delivered record's own two paths -- with no
        member of the reused bundle in it -- is the guard's own evidence that it evaluated
        the ids the action created rather than everything it fulfilled.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, requests, bundle_source_id, order = self.arrive_at_a_mixed_batch(
                Path(tmpdir)
            )
            bundle = f"raw/papers/{LATEX_BUNDLE_FIXTURE.name}"

            # The honest half of the order: one request fetched, one served by normalizing
            # the correlated bundle the workspace already held.
            delivered_source_id = self.deliver_for(workspace, requests["delivered"])
            self.fulfil_and_reopen(
                workspace, requests["delivered"], delivered_source_id, slug=self.SECOND_SLUG
            )
            self.normalize_scoped_source(workspace, requests["reused"], bundle_source_id)
            before = self.evidence_bytes(workspace)

            planted = f"{bundle}/{self.PLANTED_MEMBER}"
            planted_path = workspace / planted
            planted_path.parent.mkdir(parents=True, exist_ok=True)
            planted_path.write_text(
                "\\section{Appendix}\nPlanted inside a bundle this action did not deliver.\n",
                encoding="utf-8",
                newline="\n",
            )
            self.assert_the_planted_file_is_inert_to_the_normalizer(workspace, bundle_source_id)

            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", envelope["error_code"], envelope)
            self.assertTrue(envelope["recoverable"], envelope)
            self.assertEqual(
                "delegated acquisition changed raw evidence outside newly fulfilled manifest source scope",
                envelope["message"],
                envelope,
            )
            details = envelope["details"]
            self.assertEqual(
                [planted],
                details["unexpected_new_raw_paths"],
                "the refusal must name the planted file, and name only it: the bundle's own "
                "members were in the issuance baseline, and the delivered payload is allowed",
            )
            self.assertEqual(
                [f"raw/data/{PAYLOAD.name}", f"raw/data/{PAYLOAD.name}.provenance.yml"],
                sorted(details["allowed_new_raw_paths"]),
                "only the record this action created may contribute an expanded subtree; a "
                "reused pre-existing bundle's members appearing here means the expansion "
                "reached a record the acquirer did not write",
            )
            self.assertFalse(
                any(details["raw_scope_violations"].values()),
                f"nothing that existed at issuance was changed; the plant is a new file: {details}",
            )
            after = self.evidence_bytes(workspace)
            self.assertEqual(
                [planted],
                sorted(set(after) - set(before)),
                "a refused submission writes nothing of its own",
            )
            self.assertEqual(
                dict(before.items()),
                {path: after[path] for path in before},
                "a refused submission leaves the evidence it read exactly as it found it",
            )

            # The control: the same batch with the plant removed is accepted. Without it a
            # fixture that never reached the raw-scope guard -- refused for some earlier
            # reason with an empty allowed set -- would satisfy every assertion above.
            planted_path.unlink()
            code, session = self.submit(workspace, order["action_id"])

            self.assertEqual(0, code, session)
            self.assertEqual("research", session["phase"])
            self.assertEqual("open", self.question_status(workspace))
            self.assertIn("status: open", self.question_frontmatter(workspace, self.SECOND_SLUG))


class NormalizeAllInsideAnOrderTests(DelegatedWorkspace, unittest.TestCase):
    """`normalize_sources.py --all` inside a work order, in a workspace that has a second record.

    Every delegated scenario in this file normalizes with `--all` while an order is pending,
    and every one of them is accepted -- but only because none of their fixtures holds a
    second manifest record waiting to be normalized. `--all` considers every eligible record
    in the workspace, while an acquisition order authorizes normalized output for the
    sources it scopes and nothing else, so the two agree only by accident of fixture shape.
    The accident is worth an explicit test rather than a comment, because it is the reason
    the controller's own remediation text names `--source-id`: an acquirer that reads `--all`
    out of a passing example and runs it on a real workspace is refused for having done so.

    The other tests keep using `--all` deliberately. Changing them would hide the hazard
    rather than encode it, and their fixtures make it harmless.
    """

    def test_normalizing_every_record_writes_output_the_order_does_not_authorise(self):
        """One order, two un-normalized records, and `--all` normalizes both.

        The state is ordinary: an earlier order delivered and inventoried two sources for
        two requests and stopped before normalizing either. This order scopes one of them.
        Nothing here is tampering and nothing is hand-written -- the extra normalized file
        is produced by this package's own normalizer, run with its own documented flag --
        which is what makes the refusal worth pinning: the acquirer is refused for work it
        was never told not to do, and the refusal has to name the file so it can undo it.

        Asserted on the scope guard rather than on the `unexpected_new_normalized` refusal
        that reads as this case's owner. Both are computed from the same allowed set and the
        same before/after snapshots, so the scope guard always speaks first and the second
        require is unreachable behind it; asserting the message that actually fires is what
        keeps this test honest about which check is doing the work.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            earlier = self.add_request(workspace, "Delivered by an order that never normalized it")
            unscoped = self.deliver_unnormalized_bystander(workspace, earlier)
            # Fulfilled before the session exists, so routing leaves it out of this order's
            # scope. An open request is scoped whether or not a question blocks on it, and a
            # scoped request would make its source's normalized output authorised.
            self.run_script(
                REQUESTS, ["fulfill", "--request-id", earlier, "--source-id", unscoped], workspace
            )
            scoped = self.deliver_before_the_order(workspace, request_id, normalize=False)

            self.start(workspace)
            order = self.pending_order(workspace)
            self.assertEqual([request_id], order["scope"]["request_ids"], order)

            self.run_script(
                REQUESTS, ["fulfill", "--request-id", request_id, "--source-id", scoped], workspace
            )
            self.run_script(NORMALIZE, ["--all"], workspace)
            unscoped_output = self.normalized_record_for(workspace, unscoped).relative_to(
                workspace
            ).as_posix()
            self.run_script(
                RESOLVE,
                [
                    "reopen", "--slug", QUESTION_SLUG, "--agent-id", ACQUIRER,
                    "--source-id", scoped, "--request-id", request_id,
                ],
                workspace,
            )

            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", envelope["error_code"])
            self.assertEqual(
                "delegated acquisition changed normalized evidence outside newly fulfilled source scope",
                envelope["message"],
                envelope,
            )
            self.assertEqual(
                {"removed": [], "added_outside_scope": [unscoped_output], "changed_outside_scope": []},
                envelope["details"]["normalized_scope_violations"],
                "the refusal must name the unscoped output, and only it: the scoped source's "
                "own record is exactly what this order authorised",
            )
            self.assertTrue(envelope["recoverable"])


class BetweenActionsDeliveryTests(DelegatedWorkspace, unittest.TestCase):
    """The mid-session delivery route the contract recommends, walked end to end.

    A capture that fulfils nothing — discovery residue, the snapshot a lookup step took on
    the way to the artifact a request actually asks for — has nowhere to go inside an
    acquisition order: a delegated acquisition may deliver nothing it does not fulfil, and
    three guards say so. `docs/source-delivery.md` ("Lookup steps and intermediate
    captures") and `skills/research-acquire-delegated.md` therefore send it *between*
    actions — after one submission is accepted, before the next order is issued — delivered
    **and inventoried together**, and **unstamped**. Every other fixture in this file
    delivers either before the session starts or inside a pending order, so the one route
    the shipped contract actively recommends was the one route nothing walked.

    Both qualifiers in that sentence are walked, because each one is the difference between
    an accepted order and a refused one. "Inventoried together": inventory is what turns
    delivered bytes into a manifest record, and issuance fingerprints the manifest it finds,
    so a capture inventoried between actions is *pre-existing evidence* to the next order,
    while the same capture left un-inventoried is first recorded by the next acquirer's own
    inventory run and lands inside that order as a new manifest record no scoped request
    fulfils. "Unstamped": a stamped capture correlates to the request it names, which is what
    puts it in that order's un-normalized reuse baseline and changes what the order permits.

    Nothing here normalizes the capture, and that choice is what forces the in-order leg to
    normalize with `--source-id`. `normalize_sources.py --all` — which `acquire()` and every
    fixture built on it run — would normalize the mid-session capture too, and its output
    belongs to no fulfilled source; the last test below measures that refusal rather than
    leaving it as a claim in prose. Normalizing the capture between actions is equally
    lawful and would make `--all` harmless again, but the un-normalized shape is the one the
    advice describes, and it is the shape that costs an acquirer a flag it will not think to
    pass. `NormalizeAllInsideAnOrderTests` pins the same hazard for a source the workspace
    already held; this is the same hazard reached by following the advice.

    The route's cost is asserted rather than argued away: a between-actions delivery is
    bracketed by no work order. It passes through no postcondition, nothing compares the
    workspace against the previous order's end state, and the next issuance simply adopts
    whatever it finds. `assert_every_mutation_is_bracketed_by_an_order` still passes over
    such a session — it accounts for fulfilments and question moves, and a capture that
    fulfils nothing makes neither — which is exactly why the manifest gaining a record while
    no order was live is asserted directly instead of being left to that audit.
    """

    SPARE_SLUG = "answerable-from-delivered-evidence"
    CAPTURE_NAME = "keepa-b0mid00001.json"
    CAPTURE_BODY = (
        '{\n  "asin": "B0MID00001",\n  "supplier_quote": "7.10 EUR",\n  "offer_count": 2\n}\n'
    )

    # -- reading durable state ---------------------------------------------------------

    def session_state(self, workspace: Path) -> dict:
        return json.loads(
            (workspace / "runs" / "orchestrations" / ORCHESTRATION_ID / "session.json").read_text(
                encoding="utf-8"
            )
        )

    def issuance_baseline(self, workspace: Path, action_id: str) -> dict:
        """What the order recorded about the manifest it found, from the protected sidecar.

        The fields are the order's own answer to "what was already here": which records
        existed at issuance, and which of them this order authorises reuse of. Read here
        rather than inferred, because the difference the stamp makes is a difference in this
        list before it is a difference in any submission's verdict.
        """
        baseline = json.loads(
            CONTROLLER.scope_integrity_baseline_path(
                workspace, ORCHESTRATION_ID, action_id
            ).read_text(encoding="utf-8")
        )
        return next(
            item["fields"]
            for item in baseline["postconditions"]
            if item["check"] == "manifest_records_increased"
        )

    def requests_fulfilled_by(self, workspace: Path, source_id: str) -> list[str]:
        return sorted(
            str(record["request_id"])
            for line in (
                workspace / "sources" / "source-requests.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            if line.strip()
            for record in [json.loads(line)]
            if record.get("status") == "fulfilled" and record.get("source_id") == source_id
        )

    def normalized_records_naming(self, workspace: Path, source_id: str) -> list[str]:
        return [
            path.name
            for path in sorted((workspace / "sources" / "normalized").glob("*.md"))
            if source_id in path.read_text(encoding="utf-8")
        ]

    # -- the session shape this route needs --------------------------------------------

    def complete_a_research_order(self, workspace: Path) -> str:
        """Issue and close the research order the spare question earns.

        The route is defined by where the delivery happens — after a submission is accepted
        and before the next order is issued — so the session has to have completed an action
        before the interesting one. Research is the cheap way there: a spare actionable
        question outranks the blocked one, and answering it is the acquirer's own honest
        work rather than a fixture edit.

        The answer is deliberately uncited (`--allow-uncited`). Grounding it would need
        evidence delivered before the session, which is the pre-session arm this class
        exists to be different from; the acquisition leg that follows is where cited
        evidence enters.
        """
        code, research = self.next_action(workspace)
        self.assertEqual(0, code, research)
        self.assertEqual("research", research["phase"], research)
        self.assertEqual([self.SPARE_SLUG], research["scope"]["question_slugs"], research)
        page = workspace / "wiki" / "synthesis" / "already-answerable.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            "---\ntype: synthesis\ncreated: 2026-08-19\nupdated: 2026-08-19\n"
            "summary: This question needed no evidence the workspace lacked.\n---\n\n"
            "# Already answerable\n\nThe question is answered from what the workspace states.\n",
            encoding="utf-8",
        )
        self.run_script(
            RESOLVE,
            [
                "answer", "--slug", self.SPARE_SLUG, "--agent-id", "research-agent",
                "--answer-page", "wiki/synthesis/already-answerable.md",
                "--allow-uncited", "--allow-unclaimed",
            ],
            workspace,
        )
        code, session = self.submit(workspace, research["action_id"])
        self.assertEqual(0, code, session)
        return str(research["action_id"])

    def write_the_capture(self, workspace: Path, request_id: str | None) -> None:
        """The bytes and the sidecar, with no command run over them yet.

        Separate from the inventory step because "delivered **and inventoried together**" is
        an instruction with two halves, and one test here omits the second one on purpose.
        """
        destination = workspace / "raw" / "data"
        destination.mkdir(parents=True, exist_ok=True)
        payload = destination / self.CAPTURE_NAME
        payload.write_text(self.CAPTURE_BODY, encoding="utf-8", newline="\n")
        sidecar: dict[str, object] = {
            "origin_url": "https://api.keepa.test/product/B0MID00001",
            "license": "CC-BY-4.0",
            "retrieved_at": "2026-08-18T12:00:00Z",
            "retrieved_by": ACQUIRER,
            "checksum": f"sha256:{hashlib.sha256(payload.read_bytes()).hexdigest()}",
        }
        if request_id is not None:
            sidecar["request_id"] = request_id
        (destination / (self.CAPTURE_NAME + ".provenance.yml")).write_text(
            yaml.safe_dump(sidecar, sort_keys=False), encoding="utf-8"
        )

    def deliver_between_actions(self, workspace: Path, request_id: str | None) -> str:
        """The mid-session capture: delivered and inventoried together, normalized never.

        `deliver_before_the_order` is this helper's pre-session sibling and takes the same
        `request_id | None` affordance for the same reason — an unstamped sidecar is what
        evidence acquired for no request looks like. What differs is only when it runs: here
        a session is live, no order is pending, and the two commands this uses are the two
        the delegation gate deliberately does not cover.
        """
        self.write_the_capture(workspace, request_id)
        self.run_script(INVENTORY, ["--report"], workspace)
        return self.source_id_for(workspace, f"raw/data/{self.CAPTURE_NAME}")

    def arrive_after_a_between_actions_delivery(
        self, root: Path, *, stamped: bool
    ) -> tuple[Path, str, str, dict]:
        """Research order completed, capture delivered in the gap, acquisition order issued.

        The window is checked rather than assumed: the session is live and holds no pending
        action while the delivery happens, and the manifest changes inside it. That pair is
        the route's whole tradeoff stated as measurements — the workspace gained durable
        evidence at a moment when no work order was accountable for it.
        """
        workspace, request_id = self.make_workspace(root, spare_question=True)
        self.start(workspace)
        self.complete_a_research_order(workspace)

        session = self.session_state(workspace)
        self.assertEqual("active", session["status"], session)
        self.assertIsNone(
            session["pending_action_id"],
            "the delivery below is only 'between actions' if no order is pending",
        )
        manifest_before = self.evidence_state(workspace)["sources/manifest.jsonl"]

        capture_id = self.deliver_between_actions(workspace, request_id if stamped else None)

        self.assertNotEqual(
            manifest_before,
            self.evidence_state(workspace)["sources/manifest.jsonl"],
            "the capture reached the manifest while no work order accounted for it",
        )
        order = self.pending_order(workspace)
        self.assertEqual([request_id], order["scope"]["request_ids"], order)
        return workspace, request_id, capture_id, order

    def acquire_with_a_scoped_normalize(self, workspace: Path, request_id: str) -> str:
        """`acquire()` with one flag changed: normalize this order's source, not every record.

        `--all` would also normalize the capture delivered between actions, whose output no
        fulfilled source owns. That is not a hypothetical difference — the last test in this
        class runs `acquire()` unchanged and collects the refusal — and `--source-id` is what
        the controller's own remediation tells a refused acquirer to use.
        """
        destination = workspace / "raw" / "data"
        destination.mkdir(parents=True, exist_ok=True)
        payload = destination / PAYLOAD.name
        shutil.copy2(PAYLOAD, payload)
        sidecar = yaml.safe_load(
            PAYLOAD.with_name(PAYLOAD.name + ".provenance.yml").read_text(encoding="utf-8")
        )
        sidecar["retrieved_by"] = ACQUIRER
        sidecar["request_id"] = request_id
        sidecar["checksum"] = f"sha256:{hashlib.sha256(payload.read_bytes()).hexdigest()}"
        (destination / (PAYLOAD.name + ".provenance.yml")).write_text(
            yaml.safe_dump(sidecar, sort_keys=False), encoding="utf-8"
        )
        self.run_script(INVENTORY, ["--report"], workspace)
        source_id = self.source_id_for(workspace, f"raw/data/{PAYLOAD.name}")
        self.run_script(
            REQUESTS, ["fulfill", "--request-id", request_id, "--source-id", source_id], workspace
        )
        self.run_script(NORMALIZE, ["--source-id", source_id], workspace)
        self.run_script(
            RESOLVE,
            [
                "reopen", "--slug", QUESTION_SLUG, "--agent-id", ACQUIRER,
                "--source-id", source_id, "--request-id", request_id,
            ],
            workspace,
        )
        return source_id

    def fulfil_the_order_with(self, workspace: Path, request_id: str, source_id: str) -> None:
        """Close the order on a source the workspace already holds, normalizing it here.

        Normalization sits between the two mutations because `reopen` refuses a source
        nothing has normalized; that ordering is the reuse leg's shape, not a preference.
        """
        self.run_script(
            REQUESTS, ["fulfill", "--request-id", request_id, "--source-id", source_id], workspace
        )
        self.run_script(NORMALIZE, ["--source-id", source_id], workspace)
        self.run_script(
            RESOLVE,
            [
                "reopen", "--slug", QUESTION_SLUG, "--agent-id", ACQUIRER,
                "--source-id", source_id, "--request-id", request_id,
            ],
            workspace,
        )

    # -- the route the contract recommends ---------------------------------------------

    def test_an_unstamped_capture_delivered_between_actions_survives_the_next_order(self):
        """The recommended route, from the residue landing to the next order closing.

        What has to hold for the advice to be worth giving: the capture survives the next
        order's issuance as ordinary pre-existing evidence, the acquisition that follows is
        accepted with it sitting there, and nothing in the order ever asks the acquirer to
        account for it. The last part is the one worth pinning: an unstamped capture
        correlates to no request, so the order records no reuse authorization for it, which
        means it neither may nor must acquire a normalized record inside the action. It
        fulfils nothing, and nothing requires it to.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, capture_id, order = self.arrive_after_a_between_actions_delivery(
                Path(tmpdir), stamped=False
            )

            fields = self.issuance_baseline(workspace, order["action_id"])
            self.assertIn(
                capture_id,
                fields["manifest_record_fingerprints_before"],
                "the next order adopted the capture as evidence that was already there",
            )
            self.assertEqual(
                [],
                fields["reusable_source_ids_before"],
                "an unstamped capture correlates to nothing, so this order authorises no "
                "reuse of it and demands no normalized record for it",
            )

            source_id = self.acquire_with_a_scoped_normalize(workspace, request_id)
            code, session = self.submit(workspace, order["action_id"])

            self.assertEqual(
                0,
                code,
                f"a capture delivered between actions must not fail the next submission: {session}",
            )
            self.assertEqual("research", session["phase"], session)
            self.assertEqual(order["action_id"], session["last_completed_action_id"])
            self.assertEqual("open", self.question_status(workspace))

            # The capture is still exactly what it was: correlated to nothing, spent on
            # nothing, and un-normalized because nothing ever required otherwise.
            self.assertEqual([request_id], self.requests_fulfilled_by(workspace, source_id))
            self.assertEqual([], self.requests_fulfilled_by(workspace, capture_id))
            self.assertEqual([], self.normalized_records_naming(workspace, capture_id))
            self.assertEqual(
                self.CAPTURE_BODY,
                (workspace / "raw" / "data" / self.CAPTURE_NAME).read_text(encoding="utf-8"),
            )

            # The audit passes, and the reason it passes is the point: it accounts for
            # fulfilments and question moves, and this delivery made neither. The manifest
            # record it did make was checked by no postcondition at all — asserted in
            # `arrive_after_a_between_actions_delivery`, where the window is still open.
            checker = DelegatedAcquisitionChainTests(
                "test_the_delegated_loop_closes_and_leaves_no_unaccounted_mutation"
            )
            checker.assert_every_mutation_is_bracketed_by_an_order(workspace)
            report = LINT.run_checks(workspace, LINT.load_config(workspace))
            self.assertEqual(0, report["stats"]["delegated_unattributed_fulfilments"])

    # -- why the advice says "and inventoried together" ----------------------------------

    def test_a_capture_left_un_inventoried_across_issuance_is_refused_inside_the_next_order(self):
        """The same delivery with the second command omitted, and the bill arrives late.

        Nothing about the bytes changes: same file, same unstamped sidecar, same window.
        Only the inventory run is missing, and issuance fingerprints the raw tree as it
        stands — so the stale capture is baselined into the raw tree and the raw-scope guard
        will never mention it. What it is not is a manifest record. The next acquirer must
        run inventory before it can fulfil anything, that run derives a record for the stale
        file too, and the record is new, fulfilled by nothing, and refused.

        The refusal is worth walking because of who receives it: an acquirer that did
        nothing wrong, told that it added a manifest record outside fulfilled source scope,
        for a delivery made in a window its own order knows nothing about. That is the whole
        reason the instruction is "delivered **and inventoried together**" rather than
        "delivered".
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir), spare_question=True)
            self.start(workspace)
            self.complete_a_research_order(workspace)
            self.write_the_capture(workspace, None)

            order = self.pending_order(workspace)
            fields = self.issuance_baseline(workspace, order["action_id"])
            self.assertIn(
                f"raw/data/{self.CAPTURE_NAME}",
                fields["raw_tree_before"]["entries"],
                "issuance baselined the delivered bytes, so the raw tree has nothing new in "
                "it and the raw-scope guard is not the one that speaks",
            )
            self.assertEqual(
                {},
                fields["manifest_record_fingerprints_before"],
                "and the capture is a manifest record for nobody yet, which is the whole "
                "difference between this test and the one above",
            )

            source_id = self.acquire_with_a_scoped_normalize(workspace, request_id)
            capture_id = self.source_id_for(workspace, f"raw/data/{self.CAPTURE_NAME}")
            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", envelope["error_code"], envelope)
            self.assertEqual(
                "delegated acquisition changed, removed, or added evidence-manifest records "
                "outside fulfilled source scope",
                envelope["message"],
                envelope,
            )
            self.assertEqual(
                {"removed": [], "added_outside_scope": [capture_id], "changed_outside_scope": []},
                envelope["details"]["manifest_scope_violations"],
                envelope,
            )
            self.assertEqual([source_id], envelope["details"]["fulfilled_source_ids"], envelope)
            self.assertTrue(envelope["recoverable"])

    # -- why the advice says "unstamped" ------------------------------------------------

    def test_stamping_the_same_capture_makes_the_next_order_expect_something_of_it(self):
        """The difference one sidecar field makes, measured on both sides of it.

        Same session, same bytes, same delivery point; the sidecar either names the request
        the next order will scope or names nothing. Stamped, the capture is un-normalized
        evidence correlated to a scoped request, which is precisely the shape
        `acquisition_reuse_baselines` admits: the order lists it as reusable, and an
        acquirer may close the order on it by normalizing it inside the action — fetching
        nothing, and citing a lookup-step snapshot as the evidence for the question.
        Unstamped, the identical work is refused as reuse of evidence the order never
        authorised.

        So the advice is not stylistic. A stamp on a capture that was never meant to answer
        anything changes what the next order permits, and the acquirer that follows the
        stamp is not doing anything the contract can distinguish from honest reuse.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, capture_id, order = self.arrive_after_a_between_actions_delivery(
                Path(tmpdir), stamped=True
            )

            self.assertEqual(
                [capture_id],
                self.issuance_baseline(workspace, order["action_id"])["reusable_source_ids_before"],
                "a stamped, un-normalized capture of a reusable kind joins the reuse baseline",
            )

            self.fulfil_the_order_with(workspace, request_id, capture_id)
            code, session = self.submit(workspace, order["action_id"])

            self.assertEqual(0, code, f"the stamp turned residue into an accepted fulfilment: {session}")
            self.assertEqual("research", session["phase"], session)
            self.assertEqual("open", self.question_status(workspace))
            self.assertEqual([request_id], self.requests_fulfilled_by(workspace, capture_id))
            self.assertEqual(
                1,
                len(self.normalized_records_naming(workspace, capture_id)),
                "the reuse arm both permits and requires the one normalized record it owes",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, capture_id, order = self.arrive_after_a_between_actions_delivery(
                Path(tmpdir), stamped=False
            )

            self.assertEqual(
                [],
                self.issuance_baseline(workspace, order["action_id"])["reusable_source_ids_before"],
            )

            self.fulfil_the_order_with(workspace, request_id, capture_id)
            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", envelope["error_code"], envelope)
            self.assertIn(
                "reuses pre-existing evidence that was not a scoped reconciliation match",
                envelope["message"],
                envelope,
            )
            failure = next(
                item
                for item in envelope["details"]["reuse_scope_failures"]
                if item["source_id"] == capture_id
            )
            self.assertEqual("provenance_names_no_scoped_request", failure["cause"], failure)
            self.assertIsNone(failure["provenance_request_id"], failure)
            # Recoverable and still pending: the refusal is a repair request. The question
            # is `open` here rather than `blocked`, because the acquirer's reopen was
            # sanctioned by the pending order and applied before submission adjudicated
            # anything — which is what the refusal now asks the acquirer to undo.
            self.assertTrue(envelope["recoverable"])
            self.assertEqual(
                order["action_id"], self.session_state(workspace)["pending_action_id"], envelope
            )

    # -- the flag the route costs the acquirer -------------------------------------------

    def test_normalizing_every_record_inside_the_order_is_refused_for_the_capture(self):
        """The hazard the recommended route hands the next acquirer, walked once.

        `acquire()` is this file's own depiction of the delegated loop and it normalizes
        with `--all`, which is also what an acquirer reading a passing example would run.
        Against a workspace that took the between-actions advice, `--all` reaches the
        mid-session capture as well, and the order authorises normalized output only for the
        source it fulfils. So the acquirer is refused for evidence a *previous* window
        delivered, with a message that names only the file it just wrote.

        This is the same refusal `NormalizeAllInsideAnOrderTests` pins for a source the
        workspace already held. It is asserted again here because the state is reached by
        following the shipped advice rather than by an accident of workspace history, and
        because the first test's `--source-id` normalize is only justified if this is what
        the alternative actually does.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, capture_id, order = self.arrive_after_a_between_actions_delivery(
                Path(tmpdir), stamped=False
            )

            self.acquire(workspace, request_id)
            capture_output = (
                self.normalized_record_for(workspace, capture_id).relative_to(workspace).as_posix()
            )
            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", envelope["error_code"], envelope)
            self.assertEqual(
                "delegated acquisition changed normalized evidence outside newly fulfilled source scope",
                envelope["message"],
                envelope,
            )
            self.assertEqual(
                {
                    "removed": [],
                    "added_outside_scope": [capture_output],
                    "changed_outside_scope": [],
                },
                envelope["details"]["normalized_scope_violations"],
                "the refusal names the capture's output and only it",
            )
            self.assertTrue(envelope["recoverable"])


class AuditAssertionTests(DelegatedWorkspace, unittest.TestCase):
    """The audit assertion above is load-bearing, not decoration.

    An acceptance criterion phrased as "no out-of-band mutation exists in the audit
    log" is only worth asserting if the assertion fails when one does. Weakening the
    assertion cannot demonstrate that — a looser check still passes a clean workspace —
    so this builds the contaminated state instead and requires the check to catch it.
    """

    def test_it_fails_when_a_fulfilment_no_order_scoped_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace, request_id = self.make_workspace(root)

            # A second request fulfilled before any session exists: the delegation gate
            # allows it (nothing is live to violate), and no work order will ever scope
            # it. This is precisely the residue CR-3 removes from the normal path.
            smuggled = self.run_script(
                REQUESTS,
                [
                    "add", "--kind", "other", "--query-or-identifier", "evidence fulfilled off-protocol",
                    "--rationale", "Fulfilled before the session existed.",
                ],
                workspace,
            )["request"]["request_id"]
            source_id = self.acquire_payload(workspace, request_id)
            self.run_script(
                REQUESTS, ["fulfill", "--request-id", smuggled, "--source-id", source_id], workspace
            )

            self.start(workspace)
            code, order = self.next_action(workspace)
            self.assertEqual(0, code, order)
            self.run_script(
                REQUESTS, ["fulfill", "--request-id", request_id, "--source-id", source_id], workspace
            )
            self.run_script(
                RESOLVE,
                [
                    "reopen", "--slug", QUESTION_SLUG, "--agent-id", ACQUIRER,
                    "--source-id", source_id, "--request-id", request_id,
                ],
                workspace,
            )
            code, _ = self.submit(workspace, order["action_id"])
            self.assertEqual(0, code)

            checker = DelegatedAcquisitionChainTests("test_the_delegated_loop_closes_and_leaves_no_unaccounted_mutation")
            with self.assertRaises(AssertionError) as caught:
                checker.assert_every_mutation_is_bracketed_by_an_order(workspace)
            self.assertIn("no issued work order scoped", str(caught.exception))

            # And lint says the same thing about the same request.
            report = LINT.run_checks(workspace, LINT.load_config(workspace))
            self.assertEqual(1, report["stats"]["delegated_unattributed_fulfilments"])
            self.assertIn(
                smuggled,
                next(
                    issue["message"]
                    for issue in report["issues"]
                    if issue["category"] == "delegated_fulfilment_unattributed"
                ),
            )

    def acquire_payload(self, workspace: Path, request_id: str) -> str:
        destination = workspace / "raw" / "data"
        destination.mkdir(parents=True, exist_ok=True)
        payload = destination / PAYLOAD.name
        shutil.copy2(PAYLOAD, payload)
        sidecar = yaml.safe_load(
            PAYLOAD.with_name(PAYLOAD.name + ".provenance.yml").read_text(encoding="utf-8")
        )
        sidecar["retrieved_by"] = ACQUIRER
        sidecar["request_id"] = request_id
        sidecar["checksum"] = f"sha256:{hashlib.sha256(payload.read_bytes()).hexdigest()}"
        (destination / (PAYLOAD.name + ".provenance.yml")).write_text(
            yaml.safe_dump(sidecar, sort_keys=False), encoding="utf-8"
        )
        self.run_script(INVENTORY, ["--report"], workspace)
        self.run_script(NORMALIZE, ["--all"], workspace)
        return self.source_id_for(workspace, f"raw/data/{PAYLOAD.name}")


class OutOfBandGateTests(DelegatedWorkspace, unittest.TestCase):
    """CR-3 AC3: with delegation on, a direct mutation an active session does not
    account for is refused.

    The unit suite pins each combination against the controller. What this adds is the
    contrast that gives the rule its meaning: the *same* command, in the *same* session
    states, on a workspace that does not delegate, is unaffected. A gate that changed
    behaviour for everyone would be a regression however well it protected delegation.
    """

    def try_fulfil(self, workspace: Path, request_id: str, source_id: str) -> tuple[int, dict]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = REQUESTS.main(
                [
                    "--project-root", str(workspace), "fulfill",
                    "--request-id", request_id, "--source-id", source_id, "--format", "json",
                ]
            )
        return int(code or 0), json.loads(stdout.getvalue() or stderr.getvalue())

    def try_reopen(self, workspace: Path, request_id: str, source_id: str) -> tuple[int, dict]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = RESOLVE.main(
                [
                    "--project-root", str(workspace), "reopen", "--slug", QUESTION_SLUG,
                    "--agent-id", ACQUIRER, "--source-id", source_id,
                    "--request-id", request_id, "--format", "json",
                ]
            )
        return int(code or 0), json.loads(stdout.getvalue() or stderr.getvalue())

    def deliver_only(self, workspace: Path, request_id: str) -> str:
        """Deliver and normalize without fulfilling: the state a gate decision is made in."""
        destination = workspace / "raw" / "data"
        destination.mkdir(parents=True, exist_ok=True)
        payload = destination / PAYLOAD.name
        shutil.copy2(PAYLOAD, payload)
        sidecar = yaml.safe_load(
            PAYLOAD.with_name(PAYLOAD.name + ".provenance.yml").read_text(encoding="utf-8")
        )
        sidecar["retrieved_by"] = ACQUIRER
        sidecar["request_id"] = request_id
        sidecar["checksum"] = f"sha256:{hashlib.sha256(payload.read_bytes()).hexdigest()}"
        (destination / (PAYLOAD.name + ".provenance.yml")).write_text(
            yaml.safe_dump(sidecar, sort_keys=False), encoding="utf-8"
        )
        self.run_script(INVENTORY, ["--report"], workspace)
        self.run_script(NORMALIZE, ["--all"], workspace)
        return self.source_id_for(workspace, f"raw/data/{PAYLOAD.name}")

    def reach_state(self, root: Path, state: str, *, delegated: bool) -> tuple[Path, str, str]:
        """Build a workspace in one of the session states the gate distinguishes."""
        spare_question = state == "session live, research order pending"
        workspace, request_id = self.make_workspace(
            root, delegated=delegated, spare_question=spare_question
        )
        source_id = self.deliver_only(workspace, request_id)
        if state == "no session":
            return workspace, request_id, source_id
        self.start(workspace)
        if state == "session live, order scopes the request":
            code, order = self.next_action(workspace)
            self.assertEqual(0, code, order)
            self.assertEqual(request_id, order["scope"]["request_ids"][0])
        elif state == "session live, research order pending":
            # A spare actionable question outranks the blocked one, so routing issues a
            # research order. It scopes a question, never this request.
            code, order = self.next_action(workspace)
            self.assertEqual(0, code, order)
            self.assertEqual("research", order["phase"])
            self.assertNotIn(request_id, order["scope"]["request_ids"])
        elif state == "every session terminal":
            session_path = (
                workspace / "runs" / "orchestrations" / ORCHESTRATION_ID / "session.json"
            )
            session = json.loads(session_path.read_text(encoding="utf-8"))
            session["status"] = "no_ship"
            session["pending_action_id"] = None
            session_path.write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")
        elif state == "session live, nothing pending":
            # `start` alone leaves the session active with no pending action, which is
            # exactly the window the AutoSeller workaround used.
            pass
        else:  # pragma: no cover - guard
            raise AssertionError(f"unknown state {state}")
        return workspace, request_id, source_id

    STATES = (
        "no session",
        "session live, nothing pending",
        "session live, research order pending",
        "session live, order scopes the request",
        "every session terminal",
    )

    def test_the_gate_only_refuses_what_no_pending_order_accounts_for(self):
        # Only a pending order that scopes *this request* sanctions the mutation. A
        # research order is pending work, but it is not this request's work — which is the
        # exact situation CR AC3 names.
        expected = {
            "no session": 0,
            "session live, nothing pending": CONTROLLER.EXIT_INVALID,
            "session live, research order pending": CONTROLLER.EXIT_INVALID,
            "session live, order scopes the request": 0,
            "every session terminal": 0,
        }
        for state in self.STATES:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmpdir:
                workspace, request_id, source_id = self.reach_state(
                    Path(tmpdir), state, delegated=True
                )

                code, payload = self.try_fulfil(workspace, request_id, source_id)

                self.assertEqual(expected[state], code, payload)
                if code == 0:
                    self.assertTrue(payload["updated"])
                else:
                    self.assertEqual("SOURCE_REQUEST_FULFILL_DELEGATED", payload["error_code"])
                    self.assertEqual(
                        [ORCHESTRATION_ID],
                        [item["orchestration_id"] for item in payload["details"]["live_sessions"]],
                    )

    def test_a_workspace_that_does_not_delegate_is_unaffected_in_every_state(self):
        # The differential that keeps the gate honest: identical command, every session
        # state such a workspace can reach, no declaration — and nothing changes.
        #
        # Its third state is not "an order scopes the request": without providers there is
        # no route to acquisition, so `next` terminates the session `blocked_on_sources`.
        # That *is* the pre-CR-3 world, and fulfilling out of band there was the only way
        # to close the question — so it is the case that must keep working untouched.
        for state in ("no session", "session live, nothing pending", "session blocked_on_sources"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                workspace, request_id = self.make_workspace(root, delegated=False)
                source_id = self.deliver_only(workspace, request_id)
                if state != "no session":
                    self.start(workspace)
                if state == "session blocked_on_sources":
                    code, terminal = self.next_action(workspace)
                    self.assertEqual(CONTROLLER.EXIT_BLOCKED, code, terminal)
                    self.assertEqual("blocked_on_sources", terminal["status"])

                code, payload = self.try_fulfil(workspace, request_id, source_id)

                self.assertEqual(0, code, payload)
                self.assertTrue(payload["updated"])
                self.assertEqual("fulfill", payload["action"])

    def test_reopening_is_gated_on_the_same_terms_as_fulfilling(self):
        for state, expected in (
            ("session live, nothing pending", CONTROLLER.EXIT_INVALID),
            ("session live, order scopes the request", 0),
        ):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmpdir:
                workspace, request_id, source_id = self.reach_state(
                    Path(tmpdir), state, delegated=True
                )
                # Fulfilment is a precondition of reopening, so it is done through
                # whichever path the state allows.
                if expected == 0:
                    self.run_script(
                        REQUESTS,
                        ["fulfill", "--request-id", request_id, "--source-id", source_id],
                        workspace,
                    )
                else:
                    store = workspace / "sources" / "source-requests.jsonl"
                    record = json.loads(store.read_text(encoding="utf-8").splitlines()[0])
                    record.update({"status": "fulfilled", "source_id": source_id})
                    store.write_text(json.dumps(record) + "\n", encoding="utf-8")

                code, payload = self.try_reopen(workspace, request_id, source_id)

                self.assertEqual(expected, code, payload)
                if code == 0:
                    self.assertEqual("open", payload["status"])
                else:
                    self.assertEqual("QUESTION_REOPEN_DELEGATED", payload["error_code"])

    def test_an_unreadable_session_refuses_both_verbs(self):
        # Corruption is not evidence that a mutation is sanctioned. Both gated verbs must
        # fail closed on control state they cannot read.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, source_id = self.reach_state(
                Path(tmpdir), "session live, order scopes the request", delegated=True
            )
            (
                workspace / "runs" / "orchestrations" / ORCHESTRATION_ID / "session.json"
            ).write_text("{ truncated", encoding="utf-8")

            fulfil_code, fulfil_payload = self.try_fulfil(workspace, request_id, source_id)
            reopen_code, reopen_payload = self.try_reopen(workspace, request_id, source_id)

            for code, payload in ((fulfil_code, fulfil_payload), (reopen_code, reopen_payload)):
                self.assertEqual(CONTROLLER.EXIT_INVALID, code, payload)
                self.assertEqual("ORCHESTRATION_STATE_UNREADABLE", payload["error_code"])

    def test_the_question_gate_keys_on_scope_rather_than_phase(self):
        """Why the rule is phase-agnostic, asserted at the predicate.

        A research order legitimately mutates its scoped questions, so gating questions on
        `phase == acquisition` would refuse a legal in-order transition. Today no routing
        path actually exercises that: research orders scope only `open`/`in_progress`
        questions while `reopen` requires `blocked`, so the allowance is defensive rather
        than load-bearing. It is asserted here so a future phase that does scope a blocked
        question is not locked out by an accident of implementation.
        """
        gate = load_script_module("e2e_delegated_gate", "_delegation_gate.py")
        research_order = {
            "phase": "research",
            "scope": {"question_slugs": [QUESTION_SLUG], "request_ids": ["req-scoped"]},
        }

        self.assertTrue(gate._sanctions_question(research_order, QUESTION_SLUG))
        self.assertFalse(gate._sanctions_question(research_order, "some-other-question"))
        # Requests remain acquisition-only: a research order may append requests, never
        # fulfil one.
        self.assertFalse(gate._sanctions_request(research_order, "req-scoped"))

    def test_only_an_acquisition_order_sanctions_a_fulfilment(self):
        """An order of any other phase must not vouch for a fulfilment.

        The acquisition mode no longer disqualifies an order here. Both arms freeze the
        request store for the duration of an acquisition order, so both have to be
        identifiable from the gate; who executes the order decides nothing about whether
        its bookkeeping is contingent on acceptance. What still disqualifies an order is
        its phase, which is what this asserts.

        Asserted at the predicate because the interesting states are awkward to reach and
        stay that way: a retained order from before a mode change is exactly the shape a
        phase check has to judge, and no end-to-end state can produce one without first
        defeating another guard.
        """
        gate = load_script_module("e2e_delegated_gate", "_delegation_gate.py")
        scope = {"question_slugs": [QUESTION_SLUG], "request_ids": ["req-scoped"]}

        delegated_order = {"phase": "acquisition", "acquisition_mode": "delegated", "scope": scope}
        self.assertTrue(gate._sanctions_request(delegated_order, "req-scoped"))
        self.assertFalse(gate._sanctions_request(delegated_order, "req-elsewhere"))

        # An acquisition order sanctions whatever its mode says, including none at all:
        # a pre-delegation order and a provider order both freeze the store the same way.
        for acquisition in (
            {"phase": "acquisition", "scope": scope},
            {"phase": "acquisition", "acquisition_mode": "providers", "scope": scope},
        ):
            self.assertTrue(gate._sanctions_request(acquisition, "req-scoped"), acquisition)

        for stale in (
            {"phase": "discovery", "acquisition_mode": "delegated", "scope": scope},
            {"phase": "candidate_review", "acquisition_mode": "delegated", "scope": scope},
        ):
            with self.subTest(order=f"{stale['phase']}/{stale.get('acquisition_mode')}"):
                self.assertFalse(gate._sanctions_request(stale, "req-scoped"))


class BatchRetryAndExhaustionTests(DelegatedWorkspace, unittest.TestCase):
    """One order carries the whole backlog; failures retire requests; a new session retries.

    The batch is the reason delegated acquisition exists in this shape: a host with
    parallel connectors should not be forced through one protocol round trip per request.
    So the interesting behaviour is what happens when one batch has three different
    outcomes at once, and what the *next* routing pass does with them.

    The chain here is:

        3 open requests in one order
          -> one fulfilled, one throttled, one refused outright
          -> next order scopes only the throttled one (a standing refusal is not retried)
          -> a second throttle exhausts its budget
          -> the session terminates with a machine-readable map of what is stuck
          -> a fresh session gets a fresh look at every request

    Two blocked questions keep the workspace in `blocked_on_sources` throughout, which is
    what lets routing return to acquisition rather than to research.
    """

    def test_a_batch_with_three_outcomes_retires_only_what_cannot_be_retried(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, requests = self.build_backlog(Path(tmpdir))
            self.start(workspace)

            # --- one order, the whole routable backlog --------------------------------
            code, first = self.next_action(workspace)
            self.assertEqual(0, code, first)
            self.assertEqual("acquisition", first["phase"])
            self.assertEqual(set(requests.values()), set(first["scope"]["request_ids"]))
            self.assertEqual(
                {QUESTION_SLUG, self.SECOND_SLUG}, set(first["scope"]["question_slugs"])
            )

            second_question_before = self.question_frontmatter(workspace, self.SECOND_SLUG)
            source_id = self.deliver_for(workspace, requests["fulfilled"])
            self.run_script(
                REQUESTS,
                ["fulfill", "--request-id", requests["fulfilled"], "--source-id", source_id],
                workspace,
            )
            self.run_script(
                RESOLVE,
                [
                    "reopen", "--slug", QUESTION_SLUG, "--agent-id", ACQUIRER,
                    "--source-id", source_id, "--request-id", requests["fulfilled"],
                ],
                workspace,
            )
            self.record_failure(
                workspace, requests["throttled"], "provider_throttled", first["action_id"]
            )
            self.record_failure(
                workspace, requests["refused"], "not_authorized", first["action_id"]
            )

            code, session = self.submit(workspace, first["action_id"])

            self.assertEqual(0, code, session)
            # A partial batch is a completed action, not a degraded one.
            self.assertEqual(first["action_id"], session["last_completed_action_id"])
            # The fully unblocked question reopened; the one whose requests both failed is
            # byte-identical to before the action.
            self.assertIn("status: open", self.question_frontmatter(workspace, QUESTION_SLUG))
            self.assertEqual(
                second_question_before, self.question_frontmatter(workspace, self.SECOND_SLUG)
            )

            # The reopened question is actionable, so routing prefers research — evidence
            # arriving is progress, and answering it comes before acquiring more. Research
            # dispositions it, and only then does the backlog route to acquisition again.
            code, research = self.next_action(workspace)
            self.assertEqual(0, code, research)
            self.assertEqual("research", research["phase"])
            self.assertEqual([QUESTION_SLUG], research["scope"]["question_slugs"])
            # Answered, not deferred: a deferral is operational debt, which flips the
            # verdict to `attention_required` for its own reasons and would freeze the
            # session before acquisition could resume. Answering from the delivered
            # evidence is also the honest depiction of what the acquisition achieved.
            answer_page = workspace / "wiki" / "synthesis" / "supplier-quote.md"
            answer_page.parent.mkdir(parents=True, exist_ok=True)
            answer_page.write_text(
                "---\ntype: synthesis\ncreated: 2026-08-09\nupdated: 2026-08-09\n"
                f"source_ids:\n  - {source_id}\n"
                "summary: The delivered snapshot carries the supplier quote.\n---\n\n"
                "# Supplier quote\n\nThe delivered snapshot answers the question.\n",
                encoding="utf-8",
            )
            self.run_script(
                RESOLVE,
                [
                    "answer", "--slug", QUESTION_SLUG, "--agent-id", "research-agent",
                    "--answer-page", "wiki/synthesis/supplier-quote.md",
                    "--source-id", source_id, "--allow-unclaimed",
                ],
                workspace,
            )
            code, session = self.submit(workspace, research["action_id"])
            self.assertEqual(0, code, session)

            # --- retryable is retried, a standing refusal is not ----------------------
            code, retry = self.next_action(workspace)
            self.assertEqual(0, code, retry)
            self.assertEqual("acquisition", retry["phase"])
            self.assertEqual(
                [requests["throttled"]],
                retry["scope"]["request_ids"],
                "not_authorized is a standing decision; the fulfilled request is closed",
            )

            self.record_failure(
                workspace, requests["throttled"], "provider_throttled", retry["action_id"]
            )
            code, session = self.submit(workspace, retry["action_id"])
            self.assertEqual(0, code, session)

            # --- the budget is spent, so the session retires --------------------------
            code, terminal = self.next_action(workspace)

            self.assertEqual(CONTROLLER.EXIT_BLOCKED, code, terminal)
            self.assertEqual("blocked_on_sources", terminal["status"])
            self.assertTrue(
                terminal["pause_reason"].startswith(
                    "Delegated acquisition exhausted its attempts for every open source request"
                ),
                terminal["pause_reason"],
            )
            finished = next(
                event for event in self.events(workspace) if event["event_type"] == "session_finished"
            )
            self.assertEqual(
                {
                    requests["throttled"]: "provider_throttled",
                    requests["refused"]: "not_authorized",
                },
                finished["data"]["exhausted_requests"],
                "a host branches on the map, not on the sentence",
            )

    def test_a_fresh_session_gets_a_fresh_look_at_every_request(self):
        """The D-6 rule: attempts are counted per session, so starting one is the retry.

        This is the recovery path for a host-side fix — rotated credentials, a lifted rate
        limit. Editing the append-only audit is not, which is why the earlier session's
        events must still be there afterwards.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, requests = self.build_backlog(Path(tmpdir))
            self.start(workspace)
            code, order = self.next_action(workspace)
            self.assertEqual(0, code, order)
            for name, failure in (
                ("fulfilled", "provider_throttled"),
                ("throttled", "provider_throttled"),
                ("refused", "not_authorized"),
            ):
                self.record_failure(workspace, requests[name], failure, order["action_id"])
            code, _ = self.submit(workspace, order["action_id"])
            self.assertEqual(0, code)

            # `not_authorized` retires its request immediately; the other two have one
            # attempt each against a budget of two, so they are still routable.
            code, retry = self.next_action(workspace)
            self.assertEqual(0, code, retry)
            self.assertEqual(
                {requests["fulfilled"], requests["throttled"]},
                set(retry["scope"]["request_ids"]),
            )
            for name in ("fulfilled", "throttled"):
                self.record_failure(workspace, requests[name], "provider_throttled", retry["action_id"])
            code, _ = self.submit(workspace, retry["action_id"])
            self.assertEqual(0, code)
            code, terminal = self.next_action(workspace)
            self.assertEqual(CONTROLLER.EXIT_BLOCKED, code, terminal)

            audit_before = (
                workspace / "sources" / "source-request-attempts.jsonl"
            ).read_text(encoding="utf-8")
            self.assertEqual(5, len(audit_before.strip().splitlines()))

            # A new session, over the same workspace and the same audit.
            fresh = "orch-second-attempt"
            code, _ = self.controller(
                workspace, "start", "--orchestration-id", fresh, "--agent-id", "pm-agent"
            )
            self.assertEqual(0, code)
            code, reopened = self.controller(workspace, "next", "--orchestration-id", fresh)

            self.assertEqual(0, code, reopened)
            self.assertEqual("acquisition", reopened["phase"])
            self.assertEqual(
                set(requests.values()),
                set(reopened["scope"]["request_ids"]),
                "every open request is routable again, including the one refused outright",
            )
            self.assertEqual(
                audit_before,
                (workspace / "sources" / "source-request-attempts.jsonl").read_text(encoding="utf-8"),
                "the earlier session's evidence is retained, not cleared",
            )


class ClosedGateTests(DelegatedWorkspace, unittest.TestCase):
    """The behaviour CR-3 removes, asserted so the chain above means something."""

    def test_without_the_declaration_the_same_workspace_cannot_acquire_at_all(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace, _ = self.make_workspace(root, delegated=False)
            self.start(workspace)

            code, payload = self.next_action(workspace)

            self.assertEqual(CONTROLLER.EXIT_BLOCKED, code, payload)
            self.assertEqual("blocked_on_sources", payload["status"])
            self.assertIn(
                "No permitted end-to-end provider route",
                payload["pause_reason"] or "",
            )
            self.assertEqual(
                "blocked",
                self.question_status(workspace),
                "the question stays blocked: there was no way to acquire its evidence",
            )

    def test_the_out_of_band_fulfilment_a_host_used_to_need_is_now_refused(self):
        # The workaround CR-3 replaces: fulfil after the protocol has nothing to offer.
        # Under delegation that is refused, which is what makes the work-order path the
        # only path rather than merely the recommended one.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace, request_id = self.make_workspace(root)
            self.start(workspace)
            code, order = self.next_action(workspace)
            self.assertEqual(0, code, order)
            source_id = self.acquire_without_fulfilling(workspace, request_id)

            # Finish the action honestly, then try the out-of-band mutation afterwards.
            self.run_script(
                REQUESTS, ["fulfill", "--request-id", request_id, "--source-id", source_id], workspace
            )
            self.run_script(
                RESOLVE,
                [
                    "reopen", "--slug", QUESTION_SLUG, "--agent-id", ACQUIRER,
                    "--source-id", source_id, "--request-id", request_id,
                ],
                workspace,
            )
            code, _ = self.submit(workspace, order["action_id"])
            self.assertEqual(0, code)

            second = self.run_script(
                REQUESTS,
                [
                    "add", "--kind", "other", "--query-or-identifier", "another gap",
                    "--rationale", "Opened after the action closed.",
                ],
                workspace,
            )["request"]["request_id"]

            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = REQUESTS.main(
                    [
                        "--project-root", str(workspace), "fulfill",
                        "--request-id", second, "--source-id", source_id, "--format", "json",
                    ]
                )

            self.assertNotEqual(0, int(code or 0))
            envelope = json.loads(stderr.getvalue())
            self.assertEqual("SOURCE_REQUEST_FULFILL_DELEGATED", envelope["error_code"])

    def acquire_without_fulfilling(self, workspace: Path, request_id: str) -> str:
        destination = workspace / "raw" / "data"
        destination.mkdir(parents=True, exist_ok=True)
        payload = destination / PAYLOAD.name
        shutil.copy2(PAYLOAD, payload)
        sidecar = yaml.safe_load(
            PAYLOAD.with_name(PAYLOAD.name + ".provenance.yml").read_text(encoding="utf-8")
        )
        sidecar["retrieved_by"] = ACQUIRER
        sidecar["request_id"] = request_id
        sidecar["checksum"] = f"sha256:{hashlib.sha256(payload.read_bytes()).hexdigest()}"
        (destination / (PAYLOAD.name + ".provenance.yml")).write_text(
            yaml.safe_dump(sidecar, sort_keys=False), encoding="utf-8"
        )
        self.run_script(INVENTORY, ["--report"], workspace)
        self.run_script(NORMALIZE, ["--all"], workspace)
        return self.source_id_for(workspace, f"raw/data/{PAYLOAD.name}")


if __name__ == "__main__":
    unittest.main()
