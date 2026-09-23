"""Exercise installed public commands against explicit synthetic host evidence."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from tools._journey_cases import (
    INFERENCE,
    QUOTE,
    REFERENCE_QUOTE,
    canonical,
    facet,
    fixture_html,
    questions,
    require,
    setup_request,
)


class Environment:
    def __init__(self):
        self.before = {}

    def setenv(self, key, value):
        self.before.setdefault(key, os.environ.get(key))
        os.environ[key] = value

    def close(self):
        for key, value in self.before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class Journey:
    """Keep caller operations and the separately controlled reference reviewer distinct."""

    def __init__(self, root, case):
        from evidence_wiki import __version__

        self.root, self.case = Path(root).resolve(), case
        self.workspace = self.root / "workspace"
        self.root.mkdir(parents=True)
        self.version, self.commands = __version__, []
        self.quote = REFERENCE_QUOTE if case.get("reference") else QUOTE
        self.started = datetime.now(timezone.utc)
        self.environment = dict(os.environ, PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
        for key in ("PYTHONPATH", "PYTHONHOME", "EVIDENCE_WIKI_AUTHORITY_FILE", "EVIDENCE_WIKI_STATE_DIR"):
            self.environment.pop(key, None)

    def command(self, *args, expected=(0,), stdin=None, authority=False):
        env = dict(self.environment)
        if authority:
            env.update({key: os.environ[key] for key in ("EVIDENCE_WIKI_AUTHORITY_FILE", "EVIDENCE_WIKI_STATE_DIR")})
        started = datetime.now(timezone.utc).isoformat()
        argv = list(map(str, args))
        script = {"coverage": "coverage_manifest.py", "claim": "question_claim.py", "resolve": "question_resolve.py",
                  "requests": "source_requests.py", "run": "run_controller.py",
                  "inventory": "source_inventory.py", "normalize": "normalize_sources.py"}.get(argv[0])
        if argv[:2] == ["normalize", "verify"]:
            script = None
        if script:
            operation = [] if argv[0] in {"inventory", "normalize"} else ["claim"] if argv[0] == "claim" else [argv[1]]
            forwarded = argv[1:] if argv[0] in {"claim", "inventory", "normalize"} else argv[2:]
            index = forwarded.index("--target")
            target = forwarded.pop(index + 1); forwarded.pop(index)
            invocation = [sys.executable, "-B", str(self.workspace / "scripts" / script), "--project-root", target, *operation, *forwarded]
        else:
            invocation = [sys.executable, "-B", "-m", "evidence_wiki.cli", *argv]
        result = subprocess.run(invocation, cwd=self.root,  # noqa: S603 - fixed installed CLI and copied public owners.
            env=env, input=stdin, text=True, capture_output=True, encoding="utf-8", timeout=180)
        if len(result.stdout.encode()) + len(result.stderr.encode()) > 2_097_152:
            raise ValueError("journey_command_output_bound")
        value = None
        for output in (result.stdout, result.stderr):
            try:
                value = json.loads(output)
                break
            except ValueError:
                continue
        self.commands.append({"operation": argv[:2], "argv": invocation, "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(), "exit_code": result.returncode,
            "result": value, "output_sha256": hashlib.sha256((result.stdout + result.stderr).encode()).hexdigest()})
        if result.returncode not in expected or value is None:
            raise ValueError("journey_command_failed:" + str(args[:2]) + ":" + str(result.returncode) + ":" + str(value if value is not None else result.stderr)[:1500])
        return value

    def setup(self):
        self.command("agent", "bootstrap", "--target", self.workspace, "--format", "json")
        source = self.root / "observation.html"
        if self.case["source_mode"] == "local":
            source.write_bytes(fixture_html(self.case))
        request = setup_request(self.root, self.case, source)
        if self.case["id"] == "authored-revision":
            from tools._journey_authoring import author

            author(self)
            request["request"]["payload"]["scope"].append({"name": "region", "value": "north"})
        path = self.root / "request.json"; path.write_bytes(canonical(request))
        self.plan_path = self.root / "plan.json"
        plan = (self.command("pack", "resume", "--from-file", path, "--catalog", self.catalog, "--id", "scoped-one", "--output", self.plan_path)
                if hasattr(self, "catalog") else self.command("agent", "plan", "--from-file", path, "--output", self.plan_path))
        if self.case["id"] == "unsupported-domain":
            if plan["setup_ready"]:
                raise ValueError("unsupported_domain_scope_was_invented")
            return plan
        result = self.command("agent", "apply", "--from-file", self.plan_path, expected=(0, 3))
        if not result["setup_ready"] and not self.case["quantitative"]:
            raise ValueError("journey_setup_not_ready")
        self.command("agent", "apply", "--from-file", self.plan_path, expected=(0, 3))
        if self.case["source_mode"] == "local":
            # The fixture author owns these bytes and supplies their actual rights.
            # Local delivery itself must not invent a license for arbitrary files.
            for sidecar in (self.workspace / "raw").rglob("*.provenance.yml"):
                value = yaml.safe_load(sidecar.read_bytes())
                value.update(license="MIT", notes="Synthetic observations authored for this local conformance run.")
                sidecar.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8", newline="\n")
            self.command("inventory", "--target", self.workspace, "--format", "json", "--report")
            self.command("normalize", "--target", self.workspace, "--format", "json")
        return result

    def capture(self):
        data = (QUOTE + "\n\nRetained synthetic northern-area text, with explicit scope and provenance.\n").encode()
        request = self.command("requests", "add", "--target", self.workspace, "--kind", "web", "--query-or-identifier", "https://example.org/study",
            "--rationale", "Need the retained source", "--question-slug", "q1", "--format", "json")
        request_id = request["request"]["request_id"]
        self.command("claim", "--target", self.workspace, "--slug", "q1", "--agent-id", "caller", "--format", "json")
        self.command("resolve", "block", "--target", self.workspace, "--slug", "q1", "--agent-id", "caller",
            "--blocked-reason", "Await the declared host capture", "--request-id", request_id, "--format", "json")
        profile = {"schema_version": "evidence-host-capture/v1", "capture_id": "study", "tool_id": "fixture-browser", "tool_version": "1",
            "origin_url": "https://example.org/study", "title": "Synthetic retained source", "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "capture_method": "browser_visible_text", "content_format": "markdown", "content_kind": "primary", "completeness": "complete",
            "completeness_note": "Complete synthetic source bytes", "rights": {"status": "allowed", "license": "MIT", "terms_url": None,
            "note": "Authored synthetic source; no external access is claimed"}, "scope": {}, "request_id": request_id,
            "content_sha256": "sha256:" + hashlib.sha256(data).hexdigest(), "content_bytes": len(data)}
        value = {"schema_version": "evidence-host-delivery/v1", "capture": profile, "content_base64": base64.b64encode(data).decode()}
        path = self.root / "capture.json"; path.write_bytes(canonical(value))
        delivered = self.command("agent", "capture", "--target", self.workspace, "--from-file", path, "--path", "raw/web/capture.md")
        replay = self.command("agent", "capture", "--target", self.workspace, "--from-file", path, "--path", "raw/web/capture.md")
        require(delivered["status"] == "delivered" and replay["status"] == "already_present", 'journey_expectation:delivered["status"] == "delivered" and replay["status"] == "already_present"')
        self.command("agent", "start", "--target", self.workspace, "--agent-id", "caller", "--run-id", "capture")
        ingested = self.command("agent", "ingest", "--target", self.workspace, "--agent-id", "caller", "--run-id", "capture",
            "--request-id", request_id, "--source-path", "raw/web/capture.md")
        require(ingested["reopened"] and ingested["request"]["request"]["status"] == "fulfilled", 'journey_expectation:ingested["reopened"] and ingested["request"]["request"]["status"] == "fulfilled"')
        self.command("run", "finish", "--target", self.workspace, "--run-id", "capture", "--agent-id", "caller",
            "--final-verdict", "failed", "--reason", "Fixture capture stage closed before attaching host authority", "--format", "json")

    def provision(self):
        """The external host selects trust; the worker never creates its own approval."""
        from tests._execution_fixture import host_policy, identifier

        host = self.root / "authority"
        host.mkdir(mode=0o700)
        self.policy_path = host / "policy.json"
        self.trust = host_policy(self.policy_path)
        self.trust["principals"]["runner"]["roles"].append("scrubber")
        self.trust["principals"]["evaluator"]["roles"].append("human-review")
        self.config = yaml.safe_load((self.workspace / "research.yml").read_bytes())
        self.config.update(evidence_trust={"policy_id": "lab-authority", "policy_revision": "1"}, evidence_usage={"state_id": "laboratory"})
        (self.workspace / "research.yml").write_text(yaml.safe_dump(self.config, sort_keys=False), encoding="utf-8", newline="\n")
        self.binding = identifier("evidence-host-workspace/v1", {"root": str(self.workspace)})
        self.trust["strict_workspaces"] = {self.binding: self.config["strict_evidence"]}
        self.policy_path.write_bytes(canonical(self.trust)); self.policy_path.chmod(0o600)
        self.patch = Environment()
        self.patch.setenv("EVIDENCE_WIKI_AUTHORITY_FILE", str(self.policy_path))
        self.patch.setenv("EVIDENCE_WIKI_STATE_DIR", str(host))
        self.counter, self.checkpoint = 0, None
        self.usage("initialize", {})

    def usage(self, action, body, files=None):
        from tests._execution_fixture import authenticate

        self.counter += 1
        value = {"schema_version": "evidence-usage-command/v1", "state_id": "laboratory", "workspace_binding": self.binding,
                 "request_id": "journey-" + str(self.counter), "expected_checkpoint": self.checkpoint, "action": action, "body": body}
        request = {"command": authenticate(value, "owner", "usage"),
                   "artifacts": {key: base64.b64encode(data).decode() for key, data in (files or {}).items()}}
        result = self.command("usage", "transact", "--target", self.workspace, stdin=json.dumps(request), authority=True)
        self.checkpoint = result["checkpoint"]
        return result

    def prepare_evidence(self):
        from evidence_wiki.pack_discovery import owner
        from tests._usage_fixture import UsageFixture

        manifest = self.workspace / "sources/manifest.jsonl"
        records = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
        if self.case["source_mode"] == "unavailable":
            return []
        if len(records) != 1:
            raise ValueError("journey_source_accounting")
        record = records[0]
        verifier = owner("verify_quotes")
        path, _ = verifier.normalized_record_path(self.workspace, self.config, record["id"])
        metadata, _ = verifier.normalized_record_content(path)
        if self.case["quantitative"]:
            structured = path.with_suffix(".structured.json")
            structured.write_bytes(b'{"records":[{"id":"a","amount":0.1},{"id":"b","amount":0.2}]}\n')
            metadata["structured_view"] = {"path": structured.relative_to(self.workspace).as_posix(),
                "content_hash": "sha256:" + hashlib.sha256(structured.read_bytes()).hexdigest()}
            metadata["normalizer"] = {"name": "synthetic-host-extraction", "version": "1.0"}
            _, body_text = verifier.normalized_record_content(path)
            path.write_text("---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---\n" + body_text, encoding="utf-8", newline="\n")
        observed = metadata.get("provenance", {}).get("retrieved_at")
        if not observed:
            raise ValueError("journey_source_observation_time_missing")
        self.source_id, self.normalized, self.observed = record["id"], path, observed
        raw = {name: (self.workspace / name).read_bytes() for name in record["raw_paths"] if (self.workspace / name).is_file()}
        for name in list(raw):
            sidecar = name + ".provenance.yml"
            if (self.workspace / sidecar).is_file():
                raw[sidecar] = (self.workspace / sidecar).read_bytes()
        body, files = UsageFixture.source(self, record["id"], normalized=path.read_bytes(), evidence=raw)
        self.usage("deposit", body, files)
        self.command("normalize", "verify", "--target", self.workspace, "--source-id", record["id"], "--format", "json", authority=True)
        self.first_usable_seconds = (datetime.now(timezone.utc) - self.started).total_seconds()
        note = self.workspace / "wiki/sources" / path.name
        note.write_text("---\ntype: source\ncreated: 2026-09-23\nupdated: 2026-09-23\nsource_ids: "
            + json.dumps([record["id"]]) + "\n---\n\n# Retained observation\n\n" + QUOTE
            + "\n\nScope is limited; contradictory accounts remain unresolved.\n", encoding="utf-8", newline="\n")
        return records

    def calculate(self):
        result = self.command("computation", "check", "--target", self.workspace, expected=(0, 2, 3), authority=True)
        self.calculated = result
        expected = self.case["quantitative"]
        if expected == "missing":
            require(result.get("error_code", "").startswith("COMPUTATION_") or result.get("status") == "failed", 'journey_expectation:result.get("error_code", "").startswith("COMPUTATION_") or result.get("status") == "failed"')
        else:
            require(result["aggregations"]["observations"]["groups"][0]["metrics"]["total"]["value"] == "0.3", 'journey_expectation:result["aggregations"]["observations"]["groups"][0]["metrics"]["total"]["value"] == "0.3"')
            require((result["status"] == "failed") == (expected == "error"), 'journey_expectation:(result["status"] == "failed") == (expected == "error")')
        if expected == "warning":
            require(len(result["findings"]) == 1 and result["findings"][0]["severity"] == "warning", 'journey_expectation:len(result["findings"]) == 1 and result["findings"][0]["severity"] == "warning"')
            first = self.command("computation", "apply-warnings", "--target", self.workspace, "--expected-result-id", result["result_id"], "--request-id", "warning-one", authority=True)
            again = self.command("computation", "apply-warnings", "--target", self.workspace, "--expected-result-id", result["result_id"], "--request-id", "warning-two", authority=True)
            require(len(first["created"]) == 1 and not again["created"], 'journey_expectation:len(first["created"]) == 1 and not again["created"]')
            due = self.command("computation", "dispatch", "--target", self.workspace, "--expected-result-id", result["result_id"], "--request-id", "due-one", "--cadence-id", "inspect", authority=True)
            replay = self.command("computation", "dispatch", "--target", self.workspace, "--expected-result-id", result["result_id"], "--request-id", "due-two", "--cadence-id", "inspect", authority=True)
            require(replay["replayed"] and due["occurrence"] == replay["occurrence"], 'journey_expectation:replay["replayed"] and due["occurrence"] == replay["occurrence"]')

    def start(self, run_id):
        self.run_id = run_id
        self.command("agent", "start", "--target", self.workspace, "--agent-id", "caller", "--run-id", run_id, authority=True)
        self.command("agent", "resume", "--target", self.workspace, "--agent-id", "caller", "--run-id", run_id, authority=True)
        self.command("agent", "heartbeat", "--target", self.workspace, "--agent-id", "caller", "--run-id", run_id, authority=True)
        for state in ("planned", "answering"):
            self.command("run", "transition", "--target", self.workspace, "--agent-id", "caller", "--run-id", run_id,
                "--to-state", state, "--format", "json", authority=True)

    def finish(self, complete):
        self.command("run", "transition", "--target", self.workspace, "--agent-id", "caller", "--run-id", self.run_id,
            "--to-state", "verifying", "--format", "json", authority=True)
        self.command("run", "finish", "--target", self.workspace, "--agent-id", "caller", "--run-id", self.run_id,
            "--final-verdict", "complete" if complete else "blocked_on_sources", "--format", "json", authority=True)

    def draft(self, has_source):
        from evidence_wiki.pack_discovery import owner

        claims = []
        manifest_questions = []
        selected = [(q, outcome) for q, outcome in zip(questions(self.case), self.case["outcomes"], strict=True)]
        known = {q["id"] for q, _ in selected}
        for row in owner("question_status").collect_questions(self.workspace / "wiki/questions"):
            if row["slug"] not in known:
                selected.append(({"id": row["slug"], "text": row["question"]}, "insufficient_evidence"))
                template = self.root / "warning-coverage.yml"
                template.write_text(yaml.safe_dump({"coverage_profile": "manual", "required_facets": [facet()], "optional_facets": []}), encoding="utf-8", newline="\n")
                self.command("coverage", "init", "--target", self.workspace, "--slug", row["slug"], "--template", template,
                    "--format", "json", authority=True)
        for q, outcome in selected:
            slug = q["id"]
            manifest_questions.append({"slug": slug, "original_id": slug, "question": q["text"]})
            text = INFERENCE if outcome == "inference" else QUOTE if outcome in {"supported", "attributed"} else "The supplied accounts remain unresolved." if outcome == "contested" else "The requested evidence was not delivered."
            if self.case.get("reference"):
                text = self.case["reference"]["text"]
            claim = {"id": slug + "-claim", "question_slug": slug, "qualification": outcome, "text": text,
                "scope": "Supplied synthetic observation in the northern area only", "time": "Actual retained observation time",
                "units": "No numerical units inferred", "evidence": [], "premises": ["q1-claim"] if outcome == "inference" else [],
                "derivation": "A statement limited to north provides no southern observation." if outcome == "inference" else None,
                "limitations": ["Synthetic reference conformance; not a real-world conclusion."]}
            if has_source:
                claim["evidence"] = [{"source_id": self.source_id, "record_sha256": "sha256:" + hashlib.sha256(self.normalized.read_bytes()).hexdigest(),
                    "observed_at": self.observed, "capture": "primary", "quote": self.quote, "location_hint": None, "anchor": None}]
                self.command("coverage", "set-facet", "--target", self.workspace, "--slug", slug, "--facet-id", "retained",
                             "--accepted-source-id", self.source_id, "--format", "json", authority=True)
            self.command("coverage", "evaluate", "--target", self.workspace, "--slug", slug, "--format", "json", authority=True)
            answer = self.workspace / "wiki/synthesis" / (slug + ".md")
            answer.write_text("---\ntype: synthesis\ncreated: 2026-09-23\nupdated: 2026-09-23\nsource_ids: "
                + json.dumps([self.source_id] if has_source else []) + "\n---\n\n# Retained finding\n\n" + text + "\n", encoding="utf-8", newline="\n")
            claims.append(claim)
        if self.case["quantitative"]:
            for claim in claims:
                claim["calculations"] = []
                if claim["qualification"] == "inference":
                    claim.update(text="The supplied decimal observations sum to 0.3 units.", units="units",
                        derivation="Add the two retained decimal amounts using the declared exact aggregation.")
                    claim["calculations"] = [{"result_id": self.calculated["result_id"], "pointer": "/aggregations/observations/groups/0/metrics/total",
                        "expected": "0.3", "form": "value", "unit": "units", "rounded": False}]
        self.claims = {"schema_version": "evidence-strict-claims/v1", "questions": manifest_questions, "claims": claims}
        if self.case["quantitative"]:
            self.claims["schema_version"] = "evidence-strict-claims/v2"
        owner("_strict_contract").claims_document(self.claims)
        (self.workspace / self.config["strict_evidence"]["claims_path"]).write_bytes(canonical(self.claims))

    def reviews(self):
        from tests._execution_fixture import authenticate

        for claim in self.claims["claims"]:
            prepared = self.command("strict", "prepare-review", "--target", self.workspace, "--claim-id", claim["id"], authority=True)
            result = prepared["result"]
            row = next(row for row in result["claims"] if row["claim"]["id"] == claim["id"])
            verdicts = dict.fromkeys(("support", "source_suitability", "scope", "time", "units", "counterevidence"), "pass")
            if claim["qualification"] in {"contested", "insufficient_evidence"}:
                verdicts["support"] = "unknown"
            if self.case.get("reference"):
                verdicts.update(self.case["reference"]["verdicts"])
            payload = {"schema_version": prepared["review_schema"], "basis_id": result["basis_id"], "claim_id": claim["id"],
                "generator": authenticate({"schema_version": "evidence-strict-authorship/v1", "basis_id": result["basis_id"], "claim_id": claim["id"]}, "runner", "generator"),
                "verdicts": verdicts, "rationale": "Frozen synthetic reference judgment; no real human or production-domain review is asserted.",
                "reviewed_at": datetime.now(timezone.utc).isoformat(), "observation": row["observation"], "snapshot": prepared["snapshot"]}
            request = copy.deepcopy(prepared["registration"]); self.counter += 1; request["request_id"] = "review-" + str(self.counter)
            request["body"]["review"] = payload
            role = "human-review" if request["action"] == "register-strict-human-review" else "evaluator"
            envelope = authenticate(request, "evaluator", role)
            # Authentication must be issued after the simulated observation.
            import hmac

            from tests._execution_fixture import KEYS

            envelope["authentication"]["issued_at"] = datetime.now(timezone.utc).isoformat()
            auth = {k: v for k, v in envelope["authentication"].items() if k != "signature"}
            envelope["authentication"]["signature"] = hmac.new(bytes.fromhex(KEYS["evaluator"]), b"evidence-attestation/v1\0" + canonical({"payload": envelope["payload"], "authentication": auth}), hashlib.sha256).hexdigest()
            path = self.root / "review.json"; path.write_bytes(canonical(envelope))
            receipt = self.command("strict", "review", "--target", self.workspace, "--from-file", path, authority=True)
            self.checkpoint = receipt["checkpoint"]

    def answer(self, claim, *, refused=False):
        slug = claim["question_slug"]
        grounding = self.root / (slug + "-grounding.json")
        grounding.write_bytes(canonical({"grounding": [{"claim": claim["text"], "source_id": self.source_id, "quote": self.quote}]}))
        result = self.command("resolve", "answer", "--target", self.workspace, "--slug", slug, "--agent-id", "caller",
            "--answer-page", "wiki/synthesis/" + slug + ".md", "--source-id", self.source_id, "--grounding-file", grounding,
            "--format", "json", authority=True, expected=(2,) if refused else (0,))
        if refused:
            require(result.get("error_code") == "STRICT_EVIDENCE_REFUSED", "strict_answer_refusal_required")
            require(result["details"]["reason"] == "strict_claim_review_required", "strict_review_must_own_refusal")
        return result

    def resolve(self, has_source):
        for claim in self.claims["claims"]:
            slug = claim["question_slug"]
            self.command("claim", "--target", self.workspace, "--slug", slug, "--agent-id", "caller", "--format", "json", authority=True)
            reference_allows = not self.case.get("reference") or self.case["reference"]["accepted"]
            if has_source and not reference_allows:
                self.answer(claim, refused=True)
            if has_source and reference_allows and claim["qualification"] not in {"contested", "insufficient_evidence"}:
                result = self.answer(claim)
                if result.get("status") == "human_review" or result.get("result", {}).get("status") == "human_review":
                    self.command("resolve", "approve", "--target", self.workspace, "--slug", slug, "--reviewer", "fixture-evaluator", "--format", "json", authority=True)
            else:
                self.command("resolve", "block", "--target", self.workspace, "--slug", slug, "--agent-id", "caller",
                    "--blocked-reason", "Supplied evidence is missing or contested; no verified answer is available.", "--format", "json", authority=True)

    def run(self, *, protected_host=False):
        try:
            setup = self.setup()
            if self.case["id"] == "unsupported-domain":
                return {"case_id": self.case["id"], "outcome": "blocked_scope", "setup": setup, "commands": self.commands}
            if self.case["source_mode"] == "host":
                self.capture()
            self.provision()
            source = self.prepare_evidence()
            if self.case["quantitative"]:
                self.calculate()
            self.draft(bool(source))
            runnable = bool(source) and self.case["quantitative"] != "error"
            if runnable:
                self.start("research")
            before = self.command("agent", "research-export", "--target", self.workspace, "--allow-partial", expected=(0, 3), authority=True)
            if before["research_complete"] or any(row["accepted"] for row in before["original_outcomes"]):
                raise ValueError("unreviewed_journey_released")
            if runnable:
                first = self.claims["claims"][0]
                self.command("claim", "--target", self.workspace, "--slug", first["question_slug"], "--agent-id", "caller", "--format", "json", authority=True)
                self.answer(first, refused=True)
            if source and self.case["quantitative"] != "error":
                self.reviews()
            self.resolve(bool(source))
            if self.case["id"] == "authored-revision":
                from tools._journey_authoring import revise

                first = self.command("agent", "research-export", "--target", self.workspace, expected=(0,), authority=True)
                require(first["research_complete"], 'journey_expectation:first["research_complete"]')
                self.finish(True)
                self.revision = revise(self)
                self.draft(bool(source))
                self.start("research-revised")
                self.reviews()
                self.resolve(bool(source))
            export = self.command("agent", "research-export", "--target", self.workspace, "--allow-partial", expected=(0, 3), authority=True)
            actual = {row["original_id"]: row["accepted"] for row in export["original_outcomes"]}
            expected = {q["id"]: release for q, release in zip(questions(self.case), self.case["release_expected"], strict=True)}
            if actual != expected:
                raise ValueError("journey_original_outcome_mismatch:" + str(export)[:1200])
            if runnable:
                self.finish(export["research_complete"])
            host = {"status": "not_run"}
            if protected_host and self.case["source_mode"] != "unavailable" and self.case["quantitative"] != "error":
                from evidence_wiki.strict_host import StrictResearchHost

                controller = StrictResearchHost(self.workspace)
                targets = [str(self.workspace / "research.yml"), str(self.policy_path)]
                program = "import json,pathlib,sys\nfor name in json.loads(sys.argv[1]):\n try: pathlib.Path(name).write_text('tamper')\n except PermissionError: pass\n else: raise RuntimeError('protected write allowed')\nprint(pathlib.Path(sys.argv[2]).read_text())"
                drafted = controller.draft(controller.action("draft"), [sys.executable, "-B", "-c", program, json.dumps(targets),
                    str(self.workspace / self.config["strict_evidence"]["claims_path"])])
                require(drafted["state"] == "unaccepted_draft", 'journey_expectation:drafted["state"] == "unaccepted_draft"')
                released = controller.release(controller.action("release"))
                require({r["original_id"]: r["accepted"] for r in released["result"]["original_outcomes"] if r["original_id"] in expected} == expected, 'journey_expectation:{r["original_id"]: r["accepted"] for r in released["result"]["original_outcomes"] if r["original_id"] in expected} == expected')
                host = {"status": "passed", "assurance": released["result"]["assurance"], "platform": sys.platform}
            return {"case_id": self.case["id"], "outcome": "passed", "expected": expected, "actual": actual,
                    "export": export, "commands": self.commands, "grading": "frozen_reference_conformance", "host": host,
                    "first_usable_evidence_seconds": getattr(self, "first_usable_seconds", None),
                    "release_reason": self.case["release_reason"]}
        finally:
            if hasattr(self, "patch"):
                self.patch.close()
