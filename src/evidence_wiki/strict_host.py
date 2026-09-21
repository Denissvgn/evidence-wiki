"""A bounded macOS tool host with draft-only workers and checked final delivery.

This host supplies no model. Applications retain their own model integration
and must deliver only ``release`` results as accepted output. Arbitrary tools
or conversation outside this host are outside its enforcement boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

from ._script_host import load_packaged_script, shared_assets_root
from .errors import ConfigError
from .orchestration import _execute_bounded

MAX_OUTPUT = 1_048_576


def _refuse(reason):
    raise ConfigError("STRICT_HOST_REFUSED", "Strict host operation refused.", recoverable=False,
                      remediation="Use a supported protected host with current policy, evidence and authenticated review.",
                      details={"reason": reason})


def implementation_identity():
    """Bind package helpers as well as the host that calls them."""
    members = [(path.name, hashlib.sha256(path.read_bytes()).hexdigest())
               for path in sorted(Path(__file__).resolve().parent.glob("*.py"))]
    return hashlib.sha256(json.dumps(members, separators=(",", ":")).encode()).hexdigest()


class StrictResearchHost:
    """Execute explicit tool argv under OS restrictions; never forward raw output."""

    def __init__(self, root):
        if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
            _refuse("strict_host_platform_unsupported")
        self.root = Path(root).resolve()
        self.core = load_packaged_script(shared_assets_root(), "_strict_evidence")
        self.contract = self.core.sibling("_strict_contract")
        self.revisions = self.core.sibling("_evidence_revision")
        self.config = self.core.sibling("evidence_usage").configuration(self.root)
        self.policy = self.core.resolve_policy(self.root, self.config)
        if self.policy is None:
            _refuse("strict_policy_missing")
        self.producer = self.core.sibling("_selected_publication").producer_identity()
        self.implementation = implementation_identity()
        protected = []
        self.authority_paths = {}
        for variable in ("EVIDENCE_WIKI_AUTHORITY_FILE", "EVIDENCE_WIKI_STATE_DIR"):
            raw = os.environ.get(variable)
            if not raw or not Path(raw).is_absolute():
                _refuse("strict_host_authority_unavailable")
            path = Path(raw).resolve()
            self.authority_paths[variable] = str(path)
            protected.append(path if path.is_dir() else path.parent)
        self.protected = tuple(protected)
        self.read_roots = tuple(Path(path).resolve() for path in (
            self.root, sys.prefix, sys.base_prefix, Path(__file__).resolve().parents[1],
            "/System", "/usr/lib", "/usr/share", "/usr/bin", "/usr/sbin", "/bin", "/sbin",
            "/private/var/db/timezone", "/var/db/timezone",
        ))
        if any(self.root.is_relative_to(path) or path.is_relative_to(self.root) for path in self.protected):
            _refuse("strict_host_roots_overlap")
        self._probe()

    def _profile(self, draft_root):
        quoted = lambda path: json.dumps(str(Path(path).resolve()), ensure_ascii=False)
        roots = (*self.read_roots, Path(draft_root).resolve())
        ancestors = sorted({parent for root in roots for parent in root.parents})
        return "\n".join([
            "(version 1)", "(deny default)",
            '(allow file-read-data (literal "/"))',
            *[f"(allow file-read* (subpath {quoted(path)}))" for path in roots],
            *[f"(allow file-read-metadata (literal {quoted(path)}))" for path in ancestors],
            *[f"(allow process-exec (subpath {quoted(path)}))" for path in roots],
            '(allow file-read* (literal "/dev/null") (literal "/dev/urandom") (literal "/private/etc/localtime"))',
            "(allow sysctl-read)",
            '(allow file-write* (literal "/dev/null"))',
            f"(allow file-write* (subpath {quoted(draft_root)}))",
            *[f"(deny file-read* (subpath {quoted(path)}))" for path in self.protected],
        ])

    def _execute(self, draft_root, argv, stdin_text, timeout):
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 32
                or any(not isinstance(arg, str) or len(arg) > 8192 or "\0" in arg for arg in argv)
                or not Path(argv[0]).is_absolute() or type(timeout) is not int or not 1 <= timeout <= 600):
            _refuse("strict_worker_arguments_invalid")
        if len(stdin_text.encode("utf-8")) > MAX_OUTPUT:
            _refuse("strict_worker_input_bound_exceeded")
        # env -i clears inherited credentials before the sandboxed executable.
        # The existing process owner bounds pipes, time and descendant cleanup.
        process = _execute_bounded(
            ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE=1", "PYTHONNOUSERSITE=1",
             "/usr/bin/sandbox-exec", "-p", self._profile(draft_root), *argv],
            cwd=draft_root, stdin_text=stdin_text, timeout_seconds=timeout, capture_limit=MAX_OUTPUT,
        )
        if process.timed_out or process.stdout_truncated or process.stderr_truncated or process.returncode:
            _refuse("strict_worker_failed_or_unavailable")
        return process.stdout

    def _probe(self):
        """Prove an allowed write and denied protected read/write in the real kernel."""
        protected_file = str(Path(os.environ["EVIDENCE_WIKI_AUTHORITY_FILE"]).resolve())
        workspace_file = str(self.root / "research.yml")
        program = (
            "import pathlib,sys\n"
            "pathlib.Path('probe-ok').write_text('ok')\n"
            "denied=0\n"
            "for path,mode in ((sys.argv[1],'rb'),(sys.argv[2],'r+b')):\n"
            " try:\n"
            "  with open(path,mode): pass\n"
            " except PermissionError: denied+=1\n"
            "assert denied==2\n"
        )
        with tempfile.TemporaryDirectory(prefix="evidence-strict-probe-") as directory:
            self._execute(Path(directory), [sys.executable, "-B", "-c", program, protected_file, workspace_file], "", 10)
            if (Path(directory) / "probe-ok").read_text() != "ok":
                _refuse("strict_host_probe_failed")

    def action(self, operation, *, question_slugs=None, work_order=None):
        """Describe a scoped action without issuing a new parent work order."""
        if operation not in {"draft", "answer", "release"}:
            _refuse("strict_host_action_unsupported")
        # Parent intake currently refuses protected evidence. Do not imply that
        # a copied order or a policy edit creates a supported controller route.
        if work_order is not None:
            _refuse("strict_parent_orders_unavailable")
        if self.core.sibling("_selected_publication").producer_identity() != self.producer:
            _refuse("strict_host_implementation_changed")
        if implementation_identity() != self.implementation:
            _refuse("strict_host_implementation_changed")
        if any(str(Path(os.environ.get(variable, "")).resolve()) != path for variable, path in self.authority_paths.items()):
            _refuse("strict_host_authority_location_changed")
        current = self.core.sibling("evidence_usage").configuration(self.root)
        if current != self.config or self.core.resolve_policy(self.root, current) != self.policy:
            _refuse("strict_host_policy_changed")
        _capture, claims, basis = self.core.selection_inputs(self.root, current, self.policy)
        known = {q["slug"] for q in claims["questions"]}
        selected = sorted(known if question_slugs is None else self.core.sibling("_selected_publication").normalize_selection(question_slugs))
        if not set(selected) <= known:
            _refuse("strict_host_question_unknown")
        result = {"schema_version": "evidence-strict-action/v1", "operation": operation, "basis_id": basis,
                  "policy_id": self.contract.artifact_id(self.policy), "question_slugs": selected,
                  "parent_order_id": None, "host_implementation": self.implementation,
                  "preconditions": ["current_inputs", "frozen_policy", "protected_worker"],
                  "postconditions": (["workspace_unchanged", "draft_only"] if operation == "draft" else
                                     ["reviewed_answer_transition"] if operation == "answer" else ["accepted_claims_only"])}
        result = {**result, "action_id": self.revisions.content_id("strict-action", result)}
        self.contract.validate_shape(result, self.contract.schema_documents()[result["schema_version"]])
        return result

    def _validate_action(self, action, operation, work_order):
        if not isinstance(action, dict) or action.get("operation") != operation:
            _refuse("strict_host_action_invalid")
        expected = self.action(operation, question_slugs=action.get("question_slugs"), work_order=work_order)
        if action != expected:
            _refuse("strict_host_action_stale_or_changed")

    def draft(self, action, argv, *, work_order=None, timeout=60):
        """Return an unaccepted proposal; only the separate release owner can deliver."""
        self._validate_action(action, "draft", work_order)
        if type(timeout) is not int or not 1 <= timeout <= 600:
            _refuse("strict_worker_arguments_invalid")
        before = self.revisions.capture_workspace(self.root).revision_id
        with tempfile.TemporaryDirectory(prefix="evidence-strict-draft-") as directory:
            stdout = self._execute(Path(directory), argv, json.dumps(action), timeout)
            try:
                draft = self.contract.claims_document(self.core.sibling("_record_artifacts").json_document(stdout.encode()))
            except (ValueError, TypeError, KeyError):
                _refuse("strict_worker_result_invalid")
        if {q["slug"] for q in draft["questions"]} != set(action["question_slugs"]):
            _refuse("strict_worker_scope_expanded")
        self._validate_action(action, "draft", work_order)
        if self.revisions.capture_workspace(self.root).revision_id != before:
            _refuse("strict_worker_changed_workspace")
        result = {"action_id": action["action_id"], "state": "unaccepted_draft", "draft": draft}
        if len(self.revisions.canonical_bytes(result)) > MAX_OUTPUT:
            _refuse("strict_output_bound_exceeded")
        return result

    def answer(self, action, *, agent_id, answer_page, source_ids, work_order=None):
        """Resolve an already held claim through the canonical question owner."""
        self._validate_action(action, "answer", work_order)
        if len(action["question_slugs"]) != 1:
            _refuse("strict_host_answer_scope_invalid")
        from .workspace import Workspace
        with self.core.host_delivery(self.root), Workspace.open(self.root) as workspace:
            self._validate_action(action, "answer", work_order)
            return workspace.questions.answer(slug=action["question_slugs"][0], agent_id=agent_id,
                answer_page=answer_page, source_id=source_ids)

    def release(self, action, *, work_order=None):
        self._validate_action(action, "release", work_order)
        with self.core.host_delivery(self.root):
            result = self.core.publication(self.root, action["question_slugs"])
        if result["basis_id"] != action["basis_id"]:
            _refuse("strict_host_release_basis_changed")
        self._validate_action(action, "release", work_order)
        delivered = {"action_id": action["action_id"], "backend": "darwin-sbpl",
                "host_implementation": self.implementation, "result": result,
                "markdown": self.core.render_markdown(result)}
        if len(self.revisions.canonical_bytes(delivered)) > MAX_OUTPUT:
            _refuse("strict_output_bound_exceeded")
        return delivered
