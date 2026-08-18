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
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
ADAPTER_FIXTURE = FIXTURES / "normalizer-adapter"
STUB_ADAPTER = ADAPTER_FIXTURE / "stub_adapter.py"
PAYLOAD = ADAPTER_FIXTURE / "keepa-b0abc123.json"
PROFILE_FIXTURE_PATH = FIXTURES / "workspace-init-profile.yml"

QUESTION_SLUG = "needs-price-evidence"
ACQUIRER = "autoseller-orchestrator"
ORCHESTRATION_ID = "orch-delegated-e2e"
ADAPTER_NAME = "stub-normalize"
ADAPTER_VERSION = "1.0.0"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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

    def block_question_on_a_request(self, workspace: Path) -> str:
        """Reach `blocked_on_sources` the way research does: claim, request, block."""
        self.run_script(CLAIM, ["claim", "--slug", QUESTION_SLUG, "--agent-id", "research-agent"], workspace)
        request = self.run_script(
            REQUESTS,
            [
                "add", "--kind", "other",
                "--query-or-identifier", "Live supplier quote for B0ABC12345",
                "--rationale", "The question cannot be answered from delivered evidence.",
                "--priority", "high", "--question-slug", QUESTION_SLUG,
            ],
            workspace,
        )["request"]["request_id"]
        self.run_script(
            RESOLVE,
            [
                "block", "--slug", QUESTION_SLUG, "--agent-id", "research-agent",
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

    def source_id_for(self, workspace: Path, raw_path: str) -> str:
        for line in (workspace / "sources" / "manifest.jsonl").read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
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

    def pending_order(self, workspace: Path) -> dict:
        code, order = self.next_action(workspace)
        self.assertEqual(0, code, order)
        self.assertEqual("acquisition", order["phase"])
        return order

    def fulfil_and_reopen(self, workspace: Path, request_id: str, source_id: str) -> None:
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
    in the order's scoped-match baseline. A record outside that baseline can never
    reconcile against it. The package used to discover that four guards later, having
    first told the operator to stamp the sidecar and re-run the inventory — advice that is
    itself refused, because `raw/` is immutable and re-inventorying rewrites a manifest
    record the order may not touch. Five refusals across four guards, and none of them
    named the constraint that was actually broken.

    Three conditions land outside the baseline and their repairs differ, so each case here
    pins its own `cause`: a sidecar naming no scoped request, a correctly named sidecar on
    a source nothing had normalized yet, and a record rewritten after issuance. One
    message covering all three would be honest only by saying nothing; one message
    asserting the first would be a false accusation in the other two.
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

    def test_reusing_evidence_no_sidecar_correlated_is_refused_by_the_reuse_constraint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            source_id = self.deliver_before_the_order(workspace, None)
            self.start(workspace)
            order = self.pending_order(workspace)
            self.fulfil_and_reopen(workspace, request_id, source_id)

            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            failure = self.assert_reuse_refusal(
                envelope, source_id, "provenance_names_no_scoped_request"
            )
            self.assertIsNone(failure["provenance_request_id"])
            self.assertEqual([], envelope["details"]["matching_source_ids_before"])

    def test_a_correlated_source_nothing_normalized_yet_reports_that_cause(self):
        """The second way out of the baseline, which must not be reported as the first.

        `matching_normalized_source_records` admits a record only when its sidecar names a
        scoped request *and* its normalized output exists. A source inventoried by a prior
        order that never reached `normalize_sources.py` satisfies the first and fails the
        second, so it lands in the same refusal as an uncorrelated one — but its sidecar
        does name this request, and telling its operator otherwise sends them to re-deliver
        evidence they already have under a second raw path for no reason.

        The acquirer cannot avoid the state by leaving the source un-normalized either:
        `question_resolve.py reopen` refuses `SOURCE_NOT_NORMALIZED` before the action can
        be submitted at all. Both halves are asserted here because between them they say
        the dead end is genuinely closed, and closing it is design work rather than a
        message fix.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            source_id = self.deliver_before_the_order(workspace, request_id, normalize=False)
            self.start(workspace)
            order = self.pending_order(workspace)
            self.run_script(
                REQUESTS, ["fulfill", "--request-id", request_id, "--source-id", source_id], workspace
            )

            # Leaving it un-normalized is refused before the action is even submitted.
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

            # So the acquirer normalizes inside the order, and the reuse constraint speaks.
            self.run_script(NORMALIZE, ["--all"], workspace)
            self.run_script(
                RESOLVE,
                [
                    "reopen", "--slug", QUESTION_SLUG, "--agent-id", ACQUIRER,
                    "--source-id", source_id, "--request-id", request_id,
                ],
                workspace,
            )

            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            failure = self.assert_reuse_refusal(
                envelope, source_id, "no_normalized_output_at_issuance"
            )
            self.assertEqual(request_id, failure["provenance_request_id"])
            self.assertTrue(failure["record_unchanged"])

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

            self.assertIn("changed raw evidence outside newly fulfilled", envelope["message"])
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
    """Reuse of evidence the workspace already holds, on terms wider than one sidecar field.

    The reuse leg the sibling class walks succeeds only when the pre-existing source was
    *both* stamped for the scoped request and already normalized when the order was
    issued. Two workflows the CLI itself accepts are shut out by that, and neither has a
    workaround for a source whose id is stable across deliveries -- an arXiv paper, a
    `link:` source, a GitHub `codebase:` -- because re-delivering those bytes at a second
    path earns the same id and the same manifest record, not a new one.

    The first is evidence acquired for an earlier request. `source_requests.py fulfill`
    admits any manifest source whose provenance scope does not contradict the request, so
    a workspace can fulfil a request from what it already holds; the acquisition
    postcondition cannot, because the sidecar names some other request and one record
    cannot attest two.

    The second is worse, because it sits inside the reuse path this package documents: a
    source whose sidecar names the scoped request exactly, delivered and inventoried under
    an order that failed, timed out, or rolled over before it normalized anything. Its
    correlation is perfect and it is still shut out, by the normalized-output clause
    rather than by the request filter.

    Both cases turn on a decision the *controller* makes at issuance from state the
    acquirer cannot reach, which is why admitting them widens no mutable set: the classes
    above keep proving that a reused record, its raw sidecar and its normalized output are
    all still immutable for the duration of the order.
    """

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

    def test_evidence_stamped_for_an_earlier_request_can_fulfil_a_scoped_one(self):
        """A source id that cannot be earned twice, needed by a second request.

        The sidecar names the request this evidence was first acquired for and may not be
        restamped -- the acquirer skill forbids exactly that, and overwriting the field
        would orphan the first request's provenance link with nothing to detect it. So the
        workspace holds the evidence, the CLI accepts it, and the order refuses it.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id, earlier_request_id, source_id, order = (
                self.arrive_at_cross_request_reuse(Path(tmpdir))
            )
            evidence_before = self.evidence_bytes(workspace)

            self.fulfil_and_reopen(workspace, request_id, source_id)
            code, session = self.submit(workspace, order["action_id"])

            self.assertEqual(
                0,
                code,
                "an unchanged source the workspace already holds must be able to satisfy a "
                f"second request without being fetched again: {session}",
            )
            self.assertEqual("research", session["phase"])
            self.assertEqual("open", self.question_status(workspace))
            self.assertEqual(
                evidence_before,
                self.evidence_bytes(workspace),
                "reuse fetches nothing, so it writes nothing under raw/, normalized/ or the manifest",
            )
            # The first request keeps its evidence: nothing restamped the sidecar, so both
            # requests name the same unchanged source and neither link was orphaned.
            requests = {
                record["request_id"]: record
                for record in (
                    json.loads(line)
                    for line in (workspace / "sources" / "source-requests.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                    if line.strip()
                )
            }
            self.assertEqual(source_id, requests[earlier_request_id]["source_id"])
            self.assertEqual(source_id, requests[request_id]["source_id"])

    def test_a_correlated_source_normalized_inside_the_order_can_fulfil_it(self):
        """The documented reuse path's own primary use case: delivered, inventoried, dropped.

        An acquirer that delivered and inventoried under a prior order and then failed
        before normalizing leaves exactly this state. The sidecar names the request; only
        the normalized output is missing, and producing it is what the acquirer is being
        asked to do. There is no second raw path that helps, because the id is already in
        the manifest.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            source_id = self.deliver_before_the_order(workspace, request_id, normalize=False)
            self.start(workspace)
            order = self.pending_order(workspace)
            raw_before = self.evidence_bytes(workspace)

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
                {path: digest for path, digest in raw_before.items()},
                {path: after[path] for path in raw_before},
                "normalizing rewrites neither the raw delivery nor its manifest record",
            )

    def test_a_reused_source_without_its_normalized_record_is_still_refused(self):
        """The other direction, which stays shut: reuse is not an exemption from normalizing.

        Admitting an un-normalized source into the reuse baseline authorizes its normalized
        output; it does not stop requiring one. So the acquirer that normalizes far enough
        to reopen the question and then loses the record still owes it, and the refusal has
        to name the source rather than the reuse.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.make_workspace(Path(tmpdir))
            source_id = self.deliver_before_the_order(workspace, request_id, normalize=False)
            self.start(workspace)
            order = self.pending_order(workspace)
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
            self.normalized_record_for(workspace, source_id).unlink()

            code, envelope = self.submit(workspace, order["action_id"])

            self.assertEqual(CONTROLLER.EXIT_INVALID, code, envelope)
            self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", envelope["error_code"])
            self.assertEqual(
                "fulfilled source requests do not have normalized evidence", envelope["message"]
            )
            self.assertEqual([source_id], envelope["details"]["source_ids"])


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

    def test_only_a_delegated_acquisition_order_sanctions_a_fulfilment(self):
        """A stale providers-mode order must not vouch for a delegated fulfilment.

        Asserted at the predicate because the state is awkward to reach and stays that
        way: a session issues provider acquisition orders only while the workspace does
        *not* delegate, and declaring delegation afterwards is refused at the next
        `next`/`submit` with `ORCHESTRATION_DELEGATION_CHANGED`. The gate reads config and
        session files independently of that guard, so the retained order is exactly what a
        mode check has to disqualify — and no end-to-end state can demonstrate it without
        first defeating another guard.
        """
        gate = load_script_module("e2e_delegated_gate", "_delegation_gate.py")
        scope = {"question_slugs": [QUESTION_SLUG], "request_ids": ["req-scoped"]}

        delegated_order = {"phase": "acquisition", "acquisition_mode": "delegated", "scope": scope}
        self.assertTrue(gate._sanctions_request(delegated_order, "req-scoped"))
        self.assertFalse(gate._sanctions_request(delegated_order, "req-elsewhere"))

        for stale in (
            {"phase": "acquisition", "scope": scope},  # pre-delegation order: no mode field
            {"phase": "acquisition", "acquisition_mode": "providers", "scope": scope},
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

    SECOND_SLUG = "needs-competitor-evidence"

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

        profile_questions = workspace / "wiki" / "questions"
        second = profile_questions / f"{self.SECOND_SLUG}.md"
        second.write_text(
            (profile_questions / f"{QUESTION_SLUG}.md").read_text(encoding="utf-8")
            .replace(f"slug: {QUESTION_SLUG}", f"slug: {self.SECOND_SLUG}")
            .replace("What does the supplier quote for B0ABC12345?", "What do competitors charge?"),
            encoding="utf-8",
        )

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

    def deliver_for(self, workspace: Path, request_id: str) -> str:
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

    def question_frontmatter(self, workspace: Path, slug: str) -> str:
        return (workspace / "wiki" / "questions" / f"{slug}.md").read_text(encoding="utf-8")

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
