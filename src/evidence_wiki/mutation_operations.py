"""Serialized direct-owner mutations with immutable historical replay receipts.

The transport journal owns request correlation only. Canonical owners retain all
research state, authority, locks and verdicts. Pending work never re-dispatches
merely because expected output files are absent or unchanged.
"""

from __future__ import annotations

import base64
import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .errors import EvidenceWikiError
from .host_context import current_context
from .mutation_contracts import (
    INSPECTION,
    INTENT,
    RECEIPT,
    RECONCILIATION,
    REQUEST,
    REVISION,
    STATE,
    TERMINAL,
    canonical,
    checked,
    detached,
    digest,
    make_intent,
    refuse,
    validate_request_id,
)
from .mutation_contracts import (
    arguments as checked_arguments,
)
from .operation_control import OperationControl, checkpoint

# This registry contains known dictionary-return owners, never user callables,
# module names, shell commands or model-provided argv.
OWNER_OPERATIONS = {
    "coverage.init": ("coverage_manifest", "run_init_inline"),
    "coverage.link": ("coverage_manifest", "run_link"),
    "coverage.evaluate": ("coverage_manifest", "run_evaluate_checked"),
    "coverage.revision": ("coverage_manifest", "run_revision"),
    "questions.add_batch": ("intake_questions", "run_intake_document"),
    "source_requests.add": ("source_requests", "run_add_request"),
    "source_requests.fulfill": ("source_requests", "run_fulfill_request"),
    "source_requests.link_current": ("request_lifecycle_operations", "link_current"),
    "source_requests.fulfill_current": ("request_lifecycle_operations", "fulfill_current"),
    "questions.block_current": ("request_lifecycle_operations", "block_current"),
    "questions.reopen_current": ("request_lifecycle_operations", "reopen_current"),
    "sources.inventory": ("source_inventory", "run_inventory_report"),
    "sources.capture": ("source_delivery", "deliver"),
    "sources.capture_revision": ("source_capture_operations", "deliver"),
    "artifact.deliver": ("artifact_delivery_operations", "deliver"),
    "sources.build_index": ("retrieval_operations", "build_index"),
    "strict.register_review": ("strict_evidence", "run_operation"),
    "strict.admit": ("strict_evidence", "run_admit_document"),
    "telemetry.record": ("run_controller", "run_telemetry_record"),
    "normalize.sources": ("normalize_sources", "run_normalization_report"),
    "requirements.revise": ("requirements_operations", "revise"),
}
CONTROL_EXCLUSIONS = (".locks", "raw/.locks")
STAGED_SOURCES = frozenset({"sources.inventory", "normalize.sources"})
STAGED_COVERAGE = frozenset({"coverage.revision", "coverage.evaluate"})
STAGED_DRAFTS = frozenset({"strict.admit"})
STAGED_REQUIREMENTS = frozenset({"requirements.revise"})
STAGED_CAPTURES = frozenset({"sources.capture_revision"})
STAGED_ARTIFACTS = frozenset({"artifact.deliver"})
STAGED_OPERATIONS = STAGED_SOURCES | STAGED_COVERAGE | STAGED_DRAFTS | STAGED_REQUIREMENTS | STAGED_CAPTURES | STAGED_ARTIFACTS


def _recovery_module(operation):
    if operation in STAGED_ARTIFACTS:
        from . import artifact_delivery_recovery
        return artifact_delivery_recovery
    if operation in STAGED_CAPTURES:
        from . import mutation_capture_recovery
        return mutation_capture_recovery
    if operation in STAGED_REQUIREMENTS:
        from . import mutation_requirements_recovery
        return mutation_requirements_recovery
    if operation in STAGED_DRAFTS:
        from . import mutation_draft_recovery
        return mutation_draft_recovery
    if operation in STAGED_SOURCES:
        from . import mutation_source_recovery
        return mutation_source_recovery
    from . import mutation_coverage_recovery
    return mutation_coverage_recovery


def _scope(project_root: str | Path, operation: str, *, write: bool = False):
    context = current_context()
    if context is None or context.mutation_control_directory is None:
        refuse("AUTHORITY_REQUIRED", "/host_context")
    context.require_grant("mutation_operations." + operation)
    try:
        root = Path(project_root).expanduser().resolve(strict=True)
    except (OSError, TypeError, ValueError, RuntimeError):
        refuse("WORKSPACE_UNAVAILABLE", "/workspace")
    context.require_path(root, write=write)
    try:
        control = context.mutation_control_directory.resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        refuse("HOST_CONTEXT_CONFLICT", "/host_context")
    for selected in (context.state_directory, context.authority_file.parent if context.authority_file is not None else None):
        if selected is not None:
            selected = selected.resolve()
            if control.is_relative_to(selected) or selected.is_relative_to(control):
                refuse("CONTROL_OVERLAP", "/host_context")
    return root, context.mutation_control_directory


def _host_selection() -> str:
    """Bind references, scope and custody, without serializing authority bytes."""
    from ._pack_io import identity, read_file, signature
    from .host_context import _root_descriptor

    context = current_context()
    def selected(path, *, contents=False):
        if path is None:
            return None
        try:
            if path.is_symlink():
                refuse("HOST_CONTEXT_CONFLICT", "/host_context")
            actual = path.resolve(strict=True)
            value = {"path": str(actual), **identity(actual)}
            if contents:
                before = actual.lstat()
                with _root_descriptor(actual.parent) as descriptor:
                    raw = read_file(descriptor, actual.name, 1_048_576, signature(before))
                if signature(actual.lstat()) != signature(before):
                    refuse("HOST_CONTEXT_CONFLICT", "/host_context")
                value["content_sha256"] = "sha256:" + hashlib.sha256(raw).hexdigest()
            return value
        except (OSError, ValueError, RuntimeError):
            refuse("HOST_CONTEXT_CONFLICT", "/host_context")
    roots, writes = [], []
    for bindings, rows in ((context._root_bindings, roots), (context._write_bindings, writes)):
        for path, expected in bindings:
            value = selected(path)
            if (value["device"], value["inode"]) != tuple(str(item) for item in expected):
                refuse("HOST_CONTEXT_CONFLICT", "/host_context")
            rows.append(value)
    selected_context = {"actor_ref": context.actor_ref, "grants": sorted(context.grants),
        "allowed_roots": sorted(roots, key=lambda row: row["path"]),
        "writable_roots": sorted(writes, key=lambda row: row["path"]),
        "controller_refs": [list(row) for row in context.controller_refs],
        "credential_refs": [list(row) for row in context.credential_refs],
        "authority_file": selected(context.authority_file, contents=True),
        "state_directory": selected(context.state_directory),
        "control_directory": selected(context.mutation_control_directory)}
    if context.role_controller_refs:
        selected_context["role_controller_refs"] = [list(row) for row in context.role_controller_refs]
    return digest(selected_context)


def _store(root: Path, control: Path, intent=None):
    from .mutation_store import MutationStore
    return MutationStore(root, control, intent)


def _capture(root: Path, input_paths=()):
    from .mutation_store import capture_revision
    return capture_revision(root, excluded=CONTROL_EXCLUSIONS, input_paths=input_paths)


def _generation():
    from .mutation_store import capture_owner_generation
    return capture_owner_generation()


def _selected_files(root, paths, *, revision_id=None):
    """Pin optional files and absence under one bounded canonical observation."""
    from ._pack_io import relative_path

    before = _capture(root)
    present = []
    for relative in paths:
        relative_path(relative)
        current_context().require_path(root / relative)
        if (root / relative).exists() or (root / relative).is_symlink():
            present.append(relative)
    after = _capture(root, sorted(set(present)))
    if after["revision_id"] != before["revision_id"] or revision_id is not None and before["revision_id"] != revision_id:
        refuse("REVISION_CONFLICT", "/owner_observation")
    return after


def _configuration(root, *, revision_id=None):
    from ._pack_io import yaml_document

    observed = _selected_files(root, ["research.yml"], revision_id=revision_id)
    config = yaml_document(observed["input_bytes"]["research.yml"])
    if type(config) is not dict:
        refuse("INPUT_INVALID", "/research.yml")
    return config, observed["revision_id"]


def _rows(raw):
    from ._pack_io import json_document

    if raw is None:
        return []
    lines = raw.splitlines()
    if len(lines) > 4096:
        refuse("LIMIT", "/owner_observation")
    records = [json_document(line) for line in lines if line.strip()]
    if any(type(row) is not dict for row in records):
        refuse("INPUT_INVALID", "/owner_observation")
    return records


def _prefix(raw):
    raw = raw or b""
    return {"bytes": len(raw), "sha256": "sha256:" + hashlib.sha256(raw).hexdigest()}


def _appended(raw, prefix, fragment):
    raw = raw or b""
    size = prefix["bytes"]
    return (len(raw) >= size and _prefix(raw[:size]) == prefix
            and fragment.encode("utf-8") in raw[size:])


@contextmanager
def _validator_view(files, workspace_root, config):
    """Materialize selected originals and bind every validator path to the view."""
    from ._pack_io import relative_path

    mapped = detached(config)
    wiki = mapped.get("wiki")
    if type(wiki) is dict and type(wiki.get("root")) is str:
        original = wiki["root"]
        if Path(original).is_absolute():
            actual = current_context().require_path(original)
            try:
                wiki["root"] = actual.relative_to(workspace_root).as_posix()
            except ValueError:
                refuse("INPUT_UNBOUND", "/owner_observation")
        if wiki["root"] != ".":
            relative_path(wiki["root"])
    sources = mapped.get("sources")
    if type(sources) is dict:
        for key in ("manifest_path", "source_requests_path", "coverage_dir", "normalized_dir"):
            original = sources.get(key)
            if type(original) is str:
                # These canonical readers require workspace-relative generated
                # paths. Mapping an absolute value would invent admissibility.
                if Path(original).is_absolute():
                    refuse("INPUT_UNBOUND", "/owner_observation")
                relative_path(original.replace("\\", "/"))
                sources[key] = Path(original.replace("\\", "/")).as_posix()
    with TemporaryDirectory(prefix="mutation-validation-") as selected:
        root = Path(selected).resolve()
        for relative, raw in files.items():
            relative_path(relative)
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        yield root, mapped


def _require_captured(files, paths):
    if any(path not in files for path in paths):
        refuse("CAPTURED_INPUT_MISSING", "/owner_observation")


def _question_table(text):
    lines = text.splitlines()
    if "## Questions" not in lines:
        return []
    start = lines.index("## Questions") + 1
    end = next((index for index in range(start, len(lines)) if lines[index].startswith("## ")), len(lines))
    return [line for line in lines[start:end] if line.startswith("|")]


def _intake_batch(root, value, captured, module):
    from ._pack_io import json_document, yaml_document

    relative = _input_paths(root, "questions.add_batch", value)[0]
    raw = captured["input_bytes"][relative]
    try:
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        bounded = text.encode("utf-8")
        batch = json_document(bounded) if Path(value["from_file"]).suffix.lower() == ".json" else yaml_document(bounded)
        batch = detached(batch)
        module.validate_batch_envelope(batch)
        return batch
    except (Exception, SystemExit):
        refuse("INPUT_INVALID", "/arguments/from_file")


def _basis(root, intent, captured):
    """Retain selected preconditions, never a replacement research state."""
    from ._facades._base import call_seam
    from ._pack_io import yaml_document
    from .workspace import Workspace

    operation, value = intent["operation"], intent["arguments"]
    if operation == "requirements.revise":
        current_context().require_grant("requirements.envelope." + digest(value["envelope"])[7:])
    config, revision_id = _configuration(root, revision_id=captured["revision_id"])
    basis = {"kind": operation, "configuration_sha256": digest(config)}
    with Workspace.open(root) as workspace:
        if operation in {"coverage.init", "coverage.link"}:
            module = workspace._script("coverage_manifest")
            module.validate_slug(value["slug"])
            path = module.manifest_path(root, config, value["slug"]).relative_to(root).as_posix()
            requests = workspace._script("source_requests")
            request_path = requests.requests_path(root, config).relative_to(root).as_posix()
            source_path = requests.manifest_path(root, config).relative_to(root).as_posix()
            question_path = (module.wiki_questions_dir(root, config) / (value["slug"] + ".md")).relative_to(root).as_posix()
            files = _selected_files(root, [path, request_path, source_path, question_path], revision_id=revision_id)["input_bytes"]
            basis["manifest_path"] = path
            if operation == "coverage.init":
                with _validator_view(files, root, config) as (snapshot, snapshot_config):
                    module.ensure_question_exists(snapshot, snapshot_config, value["slug"])
                template = None if value["template"] is None else module.normalize_template_document(
                    value["template"], policy_vocabularies=module.merged_policy_vocabularies(config))
                expected = module.build_manifest(value["slug"], value["coverage_profile"], template)
                basis["expected"] = {key: child for key, child in expected.items() if key not in {"created_at", "updated_at"}}
            else:
                if path not in files:
                    raise module.CoverageManifestError("COVERAGE_MANIFEST_INVALID", "Coverage manifest is unavailable.")
                document = yaml_document(files[path])
                module.validate_manifest(document, expected_slug=value["slug"], policy_vocabularies=module.merged_policy_vocabularies(config))
                facet = module.find_facet(document, value["facet_id"].strip())
                source_ids = module.unique_nonempty(value["accepted_source_ids"], "accepted_source_ids")
                request_ids = module.unique_nonempty(value["blocking_request_ids"], "blocking_request_ids")
                _require_captured(files, ([source_path] if source_ids else []) + ([request_path] if request_ids else []))
                with _validator_view(files, root, config) as (snapshot, snapshot_config):
                    module.validate_source_ids(snapshot, snapshot_config, source_ids)
                    module.validate_request_ids(snapshot, snapshot_config, value["slug"], request_ids)
                basis["prior_manifest"] = {key: child for key, child in document.items() if key != "updated_at"}
                basis["expected"] = {field: module.merge_unique(facet[field], module.unique_nonempty(value[key], key), clear=value[clear])
                    for field, key, clear in (("accepted_source_ids", "accepted_source_ids", "clear_accepted_source_ids"),
                                             ("blocking_request_ids", "blocking_request_ids", "clear_blocking_request_ids"))}
                basis["backfill_ids"] = basis["expected"]["blocking_request_ids"]
        elif operation in {"source_requests.add", "source_requests.fulfill"}:
            module = workspace._script("source_requests")
            path = module.requests_path(root, config).relative_to(root).as_posix()
            question_paths = []
            if operation.endswith(".add"):
                module.validate_kind(value["kind"], config)
                for slug in value["question_slugs"] or []:
                    if module.SAFE_OUTPUT_ID_RE.fullmatch(slug.strip()) is None:
                        module._argument_invalid("question_slugs")
                    question_paths.append((module.questions_directory(root, config) / (slug.strip() + ".md")).relative_to(root).as_posix())
            files = _selected_files(root, [path, "log.md", *question_paths], revision_id=revision_id)["input_bytes"]
            records = _rows(files.get(path))
            if operation.endswith(".add"):
                _require_captured(files, question_paths)
                with _validator_view(files, root, config) as (snapshot, snapshot_config):
                    cleaned_slugs = module.validate_question_slugs(snapshot, snapshot_config, value["question_slugs"] or [])
                basis["question_slugs"] = cleaned_slugs
                before = module.find_open_duplicate(records, value["kind"].strip(), value["query_or_identifier"].strip(), value["scope"])
                basis["needs_log"] = before is None or not set(cleaned_slugs).issubset(before.get("question_slugs") or [])
            else:
                matches = [row for row in records if row.get("request_id") == value["request_id"]]
                before = matches[0] if len(matches) == 1 else None
                basis["needs_log"] = not (before and before.get("status") == "fulfilled" and before.get("source_id") == value["source_id"])
            basis.update(requests_path=path, before_record=before, log_prefix=_prefix(files.get("log.md")))
        elif operation == "questions.add_batch" and not value["dry_run"]:
            module = workspace._script("intake_questions")
            batch = _intake_batch(root, value, captured, module)
            now = datetime.now(timezone.utc)
            preview = call_seam(workspace._script, "intake_questions", "run_intake_document", root, batch,
                dry_run=True, from_file_label=Path(value["from_file"]).name, _observed_at=now)
            files = _selected_files(root, ["log.md", "index.md"], revision_id=revision_id)["input_bytes"]
            index = files.get("index.md", b"").decode("utf-8")
            init = module.load_sibling_module("init_research_workspace")
            index_prefixes = []
            for row in preview["created"]:
                item = batch["questions"][row["item_index"]]
                summary = item.get("summary") or row["question"] if type(item) is dict else row["question"]
                index_prefixes.append(f"| [{row['path']}]({row['path']}) | {init.escape_table_cell(summary)} | ")
            basis.update(observed_at=now.isoformat(), preview=preview, log_prefix=_prefix(files.get("log.md")),
                log_fragment=module.render_log_entry(preview["created"], preview["skipped_duplicates"], preview["handoff"] or {},
                    Path(value["from_file"]).name, now), index_prefixes=index_prefixes,
                index_required=bool(_question_table(index)))
    if _capture(root)["revision_id"] != revision_id:
        refuse("REVISION_CONFLICT", "/owner_basis")
    return detached(basis)


def _input_paths(root: Path, operation: str, value: dict[str, Any]) -> list[str]:
    if operation != "questions.add_batch":
        return []
    try:
        path = Path(value["from_file"]).expanduser()
        path = path if path.is_absolute() else root / path
        path = current_context().require_path(path)
        relative = path.relative_to(root).as_posix()
    except (OSError, TypeError, ValueError, RuntimeError):
        refuse("INPUT_UNBOUND", "/arguments/from_file")
    if path.is_symlink() or ".." in Path(relative).parts:
        refuse("INPUT_UNBOUND", "/arguments/from_file")
    return [relative]


def _owner_grant(operation: str) -> str:
    if operation == "questions.add_batch":
        return "intake_questions.run_intake"
    if operation == "coverage.evaluate":
        return "coverage_manifest.run_evaluate"
    if operation == "sources.capture":
        return "onboarding.capture"
    if operation == "strict.register_review":
        return "strict_evidence.run_operation.review"
    stem, seam = OWNER_OPERATIONS[operation]
    return stem + "." + seam


def revision(project_root: str | Path) -> dict[str, Any]:
    """Observe the bounded current revision under a native workspace gate."""
    root, control = _scope(project_root, "revision")
    with _store(root, control).workspace_locked():
        observed = _capture(root)
        return checked(REVISION, {"schema_version": REVISION,
            **{key: observed[key] for key in ("revision_id", "workspace_binding", "input_files")}})


def prepare_request(project_root: str | Path, *, request_id: str, operation: str,
                    arguments: Any) -> dict[str, Any]:
    """Freeze explicit arguments, input bytes, revision and installed owners."""
    root, control = _scope(project_root, "prepare_request")
    value = checked_arguments(operation, arguments)
    if operation in STAGED_ARTIFACTS:
        from .artifact_delivery_operations import require_scope
        require_scope(root, request_id, value)
    with _store(root, control).workspace_locked():
        observed = _capture(root, _input_paths(root, operation, value))
        result = {"schema_version": REQUEST, "request_id": request_id, "operation": operation,
                  "arguments": value, "expected_revision": observed["revision_id"],
                  "input_files": observed["input_files"], "owner_generation": _generation(),
                  "host_context_sha256": _host_selection()}
        return checked(REQUEST, result)


def _error(error: BaseException) -> dict[str, Any]:
    if isinstance(error, EvidenceWikiError):
        return {"error_code": error.error_code, "message": error.message[:4096],
                "recoverable": error.recoverable, "remediation": error.remediation[:4096],
                "details": detached(error.details)}
    envelope = getattr(error, "to_envelope", None)
    if callable(envelope):
        value = envelope()
        if type(value) is dict:
            return detached(value)
    return {"error_code": "MUTATION_OWNER_OUTCOME_UNKNOWN", "message": "The owner did not return a complete outcome.",
            "recoverable": False, "remediation": "Inspect canonical durable state before retrying.", "details": {}}


def _invoke(root: Path, intent: dict[str, Any], capture: dict[str, Any], *,
            operation_control: OperationControl | None = None) -> dict[str, Any]:
    from ._facades._base import call_seam
    from .workspace import Workspace

    operation, value = intent["operation"], intent["arguments"]
    checkpoint(operation_control, "before_owner_invocation")
    if operation in STAGED_OPERATIONS:
        options = {"operation_control": operation_control} if operation in STAGED_SOURCES and operation_control is not None else {}
        return _recovery_module(operation).invoke(root, intent, capture["owner_basis"], capture["mutation_store"], **options)
    if operation == "sources.capture":
        from .onboarding import Onboarding
        current_context().require_path(root / value["path"], write=True)
        current_context().require_path(root / (value["path"] + ".provenance.yml"), write=True)
        onboarding = Onboarding.open(allowed_roots=[root], allow=["capture"])
        try:
            return onboarding.capture(root, value["delivery"], path=value["path"])
        finally:
            onboarding.close()
    stem, seam = OWNER_OPERATIONS[operation]
    if stem == "request_lifecycle_operations":
        def load(_stem):
            from . import request_lifecycle_operations
            return request_lifecycle_operations
        return call_seam(load, stem, seam, root, **value)
    if stem == "retrieval_operations":
        def load(_stem):
            from . import retrieval_operations
            return retrieval_operations
        return call_seam(load, stem, seam, root, **value)
    with Workspace.open(root) as workspace:
        if operation == "questions.add_batch":
            module = workspace._script(stem)
            batch = _intake_batch(root, value, capture, module)
            observed_at = capture.get("owner_basis", {}).get("observed_at")
            clock = {"_observed_at": datetime.fromisoformat(observed_at)} if observed_at is not None else {}
            return call_seam(workspace._script, stem, seam, root, batch,
                             dry_run=value["dry_run"], from_file_label=Path(value["from_file"]).name, **clock)
        if operation == "telemetry.record":
            return call_seam(workspace._script, stem, seam, root, **value)
        namespace, method = operation.split(".", 1)
        return getattr(getattr(workspace, namespace), method)(**value)


def _receipt(intent: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    result = state["result"]
    if state["status"] == "reconciled" and result is None:
        result = {"original_outcome_unknown": True, "postconditions": state["reconciliation"]}
    return checked(RECEIPT, {
        "schema_version": RECEIPT, "request_id": intent["request_id"], "input_sha256": intent["input_sha256"],
        "operation": intent["operation"], "status": state["status"], "before_revision": state["before_revision"],
        "after_revision": state["after_revision"], "result": result, "error": state["error"],
        "historical": True, "authorized": False, "evidence_accepted": False,
    })


def _terminal_state(store, intent, state):
    result = _receipt(intent, state)
    store.commit_receipt(result)
    return result


def _completed_status(operation, result):
    if operation == "normalize.sources" and (
            result.get("summary", {}).get("failed", 0) or result.get("summary", {}).get("partial", 0)):
        return "partial"
    return "committed"


def _recover_owner_return(store, intent, state):
    # This retained result was published only after the canonical owner returned.
    # It proves historical completion, never current eligibility or acceptance.
    state["status"] = _completed_status(intent["operation"], state["result"])
    store.write(checked(STATE, state), expected_sha256=store.state_sha256())
    return _terminal_state(store, intent, state)


def submit(project_root: str | Path, request: Any, *,
           operation_control: OperationControl | None = None) -> dict[str, Any]:
    """Dispatch once, preserving exact duplicate outcomes before live preconditions.

    Owner refusal may follow writes. Such refusals and any missing completion
    receipt retain partial/unknown outcomes and never permit blind redispatch.
    """
    root, control = _scope(project_root, "submit", write=True)
    request = checked(REQUEST, request)
    if request["operation"] in STAGED_ARTIFACTS:
        from .artifact_delivery_operations import require_scope
        require_scope(root, request["request_id"], request["arguments"])
    if _host_selection() != request["host_context_sha256"]:
        refuse("HOST_CONTEXT_CONFLICT", "/host_context_sha256")
    info = root.stat()
    intent = make_intent(request, {"path": str(root), "device": str(info.st_dev), "inode": str(info.st_ino)})
    with _store(root, control, intent).locked() as store:
        store.prepare(intent)  # Exact immutable conflict check precedes live capture.
        saved = store.receipt()
        if saved is not None:
            return checked(RECEIPT, saved)
        state = store.read()
        if state is not None:
            state = checked(STATE, state)
            if state["status"] in TERMINAL:
                return _terminal_state(store, intent, state)
            if state["status"] == "owner_completed":
                return _recover_owner_return(store, intent, state)
            # An executing/prepared checkpoint is inspectable, never re-run.
            if state["status"] in {"prepared", "executing"}:
                refuse("OUTCOME_UNKNOWN", "/state")
        checkpoint(operation_control, "before_mutation_capture")
        captured = _capture(root, _input_paths(root, request["operation"], request["arguments"]))
        if captured["revision_id"] != request["expected_revision"]:
            refuse("REVISION_CONFLICT", "/expected_revision")
        if captured["input_files"] != request["input_files"]:
            refuse("INPUT_CONFLICT", "/input_files")
        if _generation() != request["owner_generation"]:
            refuse("GENERATION_CONFLICT", "/owner_generation")
        current_context().require_grant(_owner_grant(request["operation"]))
        current_context().require_grant("workspace.open")
        try:
            checkpoint(operation_control, "before_owner_basis")
            owner_basis = _basis(root, intent, captured)
        except (Exception, SystemExit) as error:
            from ._facades._base import _translated
            if isinstance(error, EvidenceWikiError):
                raise
            translated = _translated(error)
            if translated is not None:
                raise translated from None
            refuse("OWNER_PRECONDITION_INVALID", "/arguments")
        captured["owner_basis"] = owner_basis
        state = {"schema_version": STATE, "request_id": intent["request_id"], "input_sha256": intent["input_sha256"],
                 "status": "prepared", "before_revision": captured["revision_id"], "after_revision": None,
                 "result": None, "error": None, "owner_basis": owner_basis, "reconciliation": None}
        checkpoint(operation_control, "before_prepared_checkpoint")
        store.write(checked(STATE, state), expected_sha256=None)
        if request["operation"] in STAGED_OPERATIONS:
            try:
                checkpoint(operation_control, "before_owner_preparation")
                options = {"operation_control": operation_control} if request["operation"] in STAGED_SOURCES and operation_control is not None else {}
                owner_basis = _recovery_module(request["operation"]).prepare(root, intent, store, **options)
                checkpoint(operation_control, "after_owner_preparation")
            except (Exception, SystemExit) as error:
                from ._facades._base import _translated
                if isinstance(error, EvidenceWikiError):
                    raise
                translated = _translated(error)
                if translated is not None:
                    raise translated from None
                refuse("STAGING_INCOMPLETE", "/owner_basis")
            state = store.read()
            state["owner_basis"] = owner_basis
            store.write(checked(STATE, state), expected_sha256=store.state_sha256())
            captured["owner_basis"] = owner_basis
            if (_capture(root)["revision_id"] != request["expected_revision"]
                    or _generation() != request["owner_generation"]
                    or _host_selection() != request["host_context_sha256"]):
                refuse("STAGING_INPUTS_CHANGED", "/owner_basis")
        checkpoint(operation_control, "before_executing_checkpoint")
        state["status"] = "executing"
        store.write(checked(STATE, state), expected_sha256=store.state_sha256())
        # This checkpoint is durable before any owner invocation.
        try:
            captured["mutation_store"] = store
            options = {} if operation_control is None else {"operation_control": operation_control}
            result = _invoke(root, intent, captured, **options)
            if type(result) is not dict:
                refuse("OWNER_RESULT_INVALID", "/result")
            state["result"] = detached(result)
        except (Exception, SystemExit) as error:
            state["error"] = _error(error)
            state["status"] = "partial" if state["error"]["error_code"] in {
                "INVENTORY_CHECKSUM_REQUIRED", "INVENTORY_CHECKSUM_MISMATCH",
            } else "commit_unknown"
        else:
            state["status"] = "owner_completed"
            # Persist exact completion before the later snapshot/receipt steps.
            # Store failures propagate, retaining executing/completed checkpoints.
            store.write(checked(STATE, state), expected_sha256=store.state_sha256())
            state["status"] = _completed_status(request["operation"], state["result"])
        try:
            after = _capture(root, _input_paths(root, request["operation"], request["arguments"]))
            state["after_revision"] = after["revision_id"]
            if (after["input_files"] != request["input_files"] or _generation() != request["owner_generation"]
                    or _host_selection() != request["host_context_sha256"]):
                state["status"] = "commit_unknown"
                state["error"] = {"error_code": "MUTATION_INPUTS_CHANGED", "message": "Inputs changed during owner execution.",
                                  "recoverable": False, "remediation": "Inspect the recorded owner effects before retrying.", "details": {}}
        except (Exception, SystemExit) as error:
            state["status"], state["error"] = "commit_unknown", _error(error)
        store.write(checked(STATE, state), expected_sha256=store.state_sha256())
        return _terminal_state(store, intent, state)


def inspect(project_root: str | Path, request_id: str) -> dict[str, Any]:
    """Read detached retained state without dispatch or current acceptance."""
    root, control = _scope(project_root, "inspect")
    request_id = validate_request_id(request_id)
    store = _store(root, control)
    value = store.inspect(request_id)
    if value is not None and value["intent"]["operation"] in STAGED_ARTIFACTS:
        refuse("OWNER_PRIVATE_READ_REQUIRED", "/operation")
    if value is not None and _host_selection() != value["intent"]["host_context_sha256"]:
        refuse("HOST_CONTEXT_CONFLICT", "/host_context_sha256")
    return checked(INSPECTION, {"schema_version": INSPECTION, "request_id": request_id,
            "record": value, "historical": True, "authorized": False, "evidence_accepted": False})


def _capture_proof(root, value, config, revision_id):
    from .pack_discovery import owner
    from .source_contracts import DELIVERY, decode, reject_execution_and_secrets
    from .source_delivery import known_credentials
    from .source_inputs import SourceView

    decoded = decode(DELIVERY, canonical(value["delivery"]))
    capture = owner("_host_capture")
    profile = capture.validate(decoded["capture"])
    reject_execution_and_secrets(profile, credential_values=known_credentials())
    content = base64.b64decode(decoded["content_base64"], validate=True)
    path = value["path"]
    from ._pack_io import relative_path
    from .onboarding import Onboarding
    relative_path(path)
    if (not path.startswith("raw/") or path.startswith("raw/links/")
            or Path(path).suffix.lower() not in capture.SUFFIXES
            or (profile["content_format"] == "plain_text") != (Path(path).suffix.lower() == ".txt")):
        refuse("INPUT_UNBOUND", "/capture")
    onboarding = Onboarding.open(allowed_roots=[root], allow=["capture"])
    try:
        onboarding._check("capture")
        onboarding._scope.builtin_workspace(root)
        for selected in (root, root / path, root / (path + ".provenance.yml")):
            onboarding._scope.path(selected, write=True)
    finally:
        onboarding.close()
    sidecar = {"host_capture": profile, "checksum": profile["content_sha256"], "origin_url": profile["origin_url"],
        "retrieved_at": profile["retrieved_at"], "retrieved_by": "host:" + profile["tool_id"], "title": profile["title"],
        "source_type": "host_text_capture", "license": profile["rights"]["license"], "terms_note": profile["rights"]["note"]}
    for key, item in (("scope", profile["scope"]), ("request_id", profile["request_id"]), ("terms_url", profile["rights"]["terms_url"])):
        if item:
            sidecar[key] = item
    paths = [path, path + ".provenance.yml"]
    files = _selected_files(root, paths, revision_id=revision_id)["input_bytes"]
    observation = {"path": path, "present": [name for name in paths if name in files],
                   "content_matches": files.get(path) == content,
                   "sidecar_matches": files.get(paths[1]) == canonical(sidecar)}
    if not observation["content_matches"] or not observation["sidecar_matches"]:
        return bool(observation["present"]), False, observation
    record = {"kind": "host_capture", "raw_paths": [path], "provenance": sidecar}
    capture.inspect_record(root, record, read_input=lambda _root, relative, maximum: files[relative])
    view = SourceView(root)
    if view.workspace != "present" or view.config != config:
        refuse("REVISION_CONFLICT", "/capture")
    roots = config.get("raw", {}).get("source_roots", [])
    if not isinstance(roots, list) or not any(type(raw_root) is str and path.startswith(raw_root.rstrip("/") + "/") for raw_root in roots):
        refuse("INPUT_UNBOUND", "/capture")
    owner("_usage_gate").require_host_intake(config, [record])
    orchestration = owner("_orchestration_config")
    delegated = orchestration.is_delegated(orchestration.orchestration_config(config))
    owner("_delegation_gate").require_sanctioned_mutation(root, delegated, request_id=profile["request_id"],
        error_code="ONBOARDING_OWNERSHIP_CONFLICT", subject="Host capture reconciliation",
        remediation="Use the current acquisition order's request and delivery scope.")
    view.finish()
    return True, True, observation


def _coverage_proof(root, value, basis, config, revision_id, workspace):
    from ._pack_io import yaml_document

    module = workspace._script("coverage_manifest")
    path = basis.get("manifest_path")
    if path is None:
        return False, False, {"reason": "before_manifest_basis_missing"}
    request_module = workspace._script("source_requests")
    requests_path = request_module.requests_path(root, config).relative_to(root).as_posix()
    source_manifest = request_module.manifest_path(root, config).relative_to(root).as_posix()
    question_path = (module.wiki_questions_dir(root, config) / (value["slug"] + ".md")).relative_to(root).as_posix()
    files = _selected_files(root, [path, requests_path, source_manifest, question_path], revision_id=revision_id)["input_bytes"]
    if path not in files:
        return False, False, {"manifest_path": path, "present": False}
    document = yaml_document(files[path])
    module.validate_manifest(document, expected_slug=value["slug"], policy_vocabularies=module.merged_policy_vocabularies(config))
    observation = {"manifest_path": path, "manifest": document}
    if basis["kind"] == "coverage.init":
        _require_captured(files, [question_path])
        with _validator_view(files, root, config) as (snapshot, snapshot_config):
            module.ensure_question_exists(snapshot, snapshot_config, value["slug"])
        matches = all(document.get(key) == child for key, child in basis["expected"].items())
        return True, matches, observation
    module.find_facet(document, value["facet_id"].strip())
    expected = detached(basis["prior_manifest"])
    expected_facet = module.find_facet(expected, value["facet_id"].strip())
    expected_facet.update(basis["expected"])
    actual = {key: child for key, child in document.items() if key != "updated_at"}
    matches = actual == expected
    _require_captured(files, ([source_manifest] if basis["expected"]["accepted_source_ids"] else []) +
                      ([requests_path] if basis["expected"]["blocking_request_ids"] else []))
    with _validator_view(files, root, config) as (snapshot, snapshot_config):
        module.validate_source_ids(snapshot, snapshot_config, basis["expected"]["accepted_source_ids"])
        module.validate_request_ids(snapshot, snapshot_config, value["slug"], basis["expected"]["blocking_request_ids"])
    records = _rows(files.get(requests_path))
    missing = []
    for identifier in basis["backfill_ids"]:
        selected = [row for row in records if row.get("request_id") == identifier]
        if not selected or any(module.normalize_scope(row.get("scope")).get(module.FACET_SCOPE_KEY) != value["facet_id"].strip()
                               for row in selected):
            missing.append(identifier)
    observation["missing_request_scope_ids"] = missing
    return True, matches and not missing, observation


def _request_proof(root, value, basis, config, revision_id, workspace):
    module = workspace._script("source_requests")
    path = basis.get("requests_path")
    if path is None:
        return False, False, {"reason": "before_request_basis_missing"}
    manifest_path = module.manifest_path(root, config).relative_to(root).as_posix()
    question_paths = []
    if basis["kind"] == "source_requests.add":
        module.validate_kind(value["kind"], config)
        directory = module.questions_directory(root, config)
        for slug in value["question_slugs"] or []:
            if module.SAFE_OUTPUT_ID_RE.fullmatch(slug.strip()) is None:
                module._argument_invalid("question_slugs")
            question_paths.append((directory / (slug.strip() + ".md")).relative_to(root).as_posix())
    files = _selected_files(root, [path, "log.md", manifest_path, *question_paths], revision_id=revision_id)["input_bytes"]
    _require_captured(files, [path, *question_paths])
    if question_paths:
        with _validator_view(files, root, config) as (snapshot, snapshot_config):
            module.validate_question_slugs(snapshot, snapshot_config, value["question_slugs"])
    records = _rows(files.get(path))
    before = basis["before_record"]
    if basis["kind"] == "source_requests.add":
        selected = [row for row in records if row.get("status") == "open" and row.get("kind") == value["kind"].strip()
            and type(row.get("query_or_identifier")) is str
            and module.normalize_query(row["query_or_identifier"]) == module.normalize_query(value["query_or_identifier"])
            and module.scope_equal(row.get("scope"), value["scope"])]
    else:
        selected = [row for row in records if row.get("request_id") == value["request_id"]]
    if len(selected) != 1:
        return bool(selected), False, {"matches": len(selected)}
    row = selected[0]
    if basis["kind"] == "source_requests.add":
        fields = before if before is not None else {
            "kind": value["kind"].strip(), "query_or_identifier": value["query_or_identifier"].strip(),
            "priority": value["priority"], "rationale": value["rationale"].strip()}
        matches = all(row.get(key) == fields.get(key) for key in ("kind", "query_or_identifier", "priority", "rationale"))
        questions = list(dict.fromkeys([*((before or {}).get("question_slugs") or []), *basis["question_slugs"]]))
        matches = matches and row.get("question_slugs") == questions
        event_questions = basis["question_slugs"]
        fragment = (f"- Request: \u0060{row['request_id']}\u0060 ({row['kind']}, {row['priority']}).\n"
                    f"- Needs: {value['query_or_identifier'].strip()}\n"
                    f"- Questions: {', '.join(event_questions) if event_questions else 'none'}.\n")
    else:
        matches = row.get("status") == "fulfilled" and row.get("source_id") == value["source_id"].strip()
        if before is not None:
            matches = matches and all(row.get(key) == child for key, child in before.items()
                if key not in {"status", "source_id", "updated_at"})
        manifest = _rows(files.get(manifest_path))
        sources = [source for source in manifest if source.get("id") == value["source_id"].strip()]
        if len(sources) != 1:
            matches = False
        else:
            provenance = sources[0].get("provenance", {})
            scope = provenance.get("scope", {}) if type(provenance) is dict else {}
            module.check_fulfill_scope(row, value["source_id"].strip(), scope,
                                      value["match_scope"] or {}, require_scope=value["require_scope"])
        fragment = (f"- Request: \u0060{value['request_id']}\u0060 fulfilled by \u0060{value['source_id'].strip()}\u0060.\n"
                    f"- Questions: {', '.join(row.get('question_slugs') or []) or 'none'}.\n")
    log_complete = not basis["needs_log"] or _appended(files.get("log.md"), basis["log_prefix"], fragment)
    return True, matches and log_complete, {"request": row, "activity_complete": log_complete, "fields_match": matches}


def _intake_proof(root, basis, revision_id):
    if "preview" not in basis:
        return False, False, {"reason": "before_intake_basis_missing"}
    rows = basis["preview"]["created"]
    paths = [row["path"] for row in rows] + ["index.md", "log.md"]
    if len(paths) > 64:
        refuse("LIMIT", "/owner_observation")
    files = _selected_files(root, paths, revision_id=revision_id)["input_bytes"]
    missing = [row["path"] for row in rows if files.get(row["path"]) != row["content"].encode("utf-8")]
    log_complete = not rows or _appended(files.get("log.md"), basis["log_prefix"], basis["log_fragment"])
    index = _question_table(files.get("index.md", b"").decode("utf-8"))
    index_complete = not basis["index_required"] or all(any(line.startswith(prefix) and line.endswith(" | |")
        for line in index) for prefix in basis["index_prefixes"])
    return bool(rows and any(row["path"] in files for row in rows)), not missing and log_complete and index_complete, {
        "missing_or_changed_pages": missing, "activity_complete": log_complete, "index_complete": index_complete}


def _strict_proof(root, value, config, workspace):
    from .host_context import checked_operation

    envelope = value["envelope"]
    identifier = envelope["payload"].get("request_id")
    current_context().require_grant("evidence_usage.run_status")
    with checked_operation("evidence_usage", "run_status", (root,), {"request_id": identifier}) as revalidate:
        module = workspace._script("strict_evidence")
        usage = module.sibling("_evidence_usage")
        revalidate()
        with usage.current_view(root, config) as view:
            event = view.state.requests.get(identifier)
            if event is None:
                return False, False, {"request_id": identifier, "receipt": None}
            matches = event["command"] == envelope and event["artifacts"] == {}
            return True, matches, {"request_id": identifier, "receipt": view.state.receipt(event),
                                  "exact_signed_command": matches, "current_acceptance": "not_evaluated"}


def _unproved_observation(root, intent, config, revision_id, workspace):
    """Observe selected durable artifacts without an unsupported verifier route."""
    from ._pack_io import yaml_document

    operation, value = intent["operation"], intent["arguments"]
    if operation in {"sources.inventory", "normalize.sources"}:
        module = workspace._script("source_requests")
        path = module.manifest_path(root, config).relative_to(root).as_posix()
        files = _selected_files(root, [path], revision_id=revision_id)["input_bytes"]
        rows = _rows(files.get(path))
        if operation == "normalize.sources" and value["source_ids"] is not None:
            wanted = set(value["source_ids"])
            rows = [row for row in rows if row.get("id") in wanted]
        selected = [{"source_id": row.get("id"), "status": row.get("status"),
            "normalized_path": row.get("normalized_path"), "raw_paths": row.get("raw_paths")} for row in rows[:256]]
        outputs = []
        for row in selected:
            output = row["normalized_path"]
            if type(output) is str:
                outputs.append(output)
        current = _selected_files(root, outputs, revision_id=revision_id)
        captured = {row["path"]: row for row in current["input_files"]}
        for row in selected:
            row["normalized_observation"] = captured.get(row["normalized_path"]) if type(row["normalized_path"]) is str else None
        return {"manifest_path": path, "manifest_present": path in files,
            "selected_records_observed": len(rows), "records": selected, "records_truncated": len(rows) > 256,
            "reason": "artifact_presence_cannot_reconstruct_extraction_or_inventory_completion"}
    module = workspace._script("coverage_manifest")
    slug = value.get("slug") or value.get("request", {}).get("slug")
    if type(slug) is not str:
        return {"reason": "canonical_revision_request_has_no_selected_manifest"}
    module.validate_slug(slug)
    path = module.manifest_path(root, config, slug).relative_to(root).as_posix()
    files = _selected_files(root, [path], revision_id=revision_id)["input_bytes"]
    if path not in files:
        return {"manifest_path": path, "present": False, "reason": "selected_manifest_missing"}
    document = yaml_document(files[path])
    module.validate_manifest(document, expected_slug=slug, policy_vocabularies=module.merged_policy_vocabularies(config))
    return {"manifest_path": path, "manifest": document,
            "reason": "current_manifest_cannot_reconstruct_original_evaluation_or_revision"}


def reconcile(project_root: str | Path, request_id: str, *,
              operation_control: OperationControl | None = None) -> dict[str, Any]:
    """Inspect owning postconditions while preserving every earlier receipt.

    A retained owner return proves historical completion. Exact selected durable
    postconditions prove desired state without reconstructing original timing.
    Neither proof permits redispatch or accepts evidence at the current clock.
    """
    root, control = _scope(project_root, "reconcile", write=True)
    request_id = validate_request_id(request_id)
    store = _store(root, control)
    value = store.inspect(request_id)
    if value is None:
        refuse("REQUEST_UNKNOWN", "/request_id")
    intent = checked(INTENT, value["intent"])
    if intent["operation"] in STAGED_ARTIFACTS:
        refuse("OWNER_PRIVATE_READ_REQUIRED", "/operation")
    if _host_selection() != intent["host_context_sha256"]:
        refuse("HOST_CONTEXT_CONFLICT", "/host_context_sha256")
    if value.get("receipt") is not None and value["receipt"]["status"] in {"committed", "refused"}:
        return checked(RECEIPT, value["receipt"])
    with _store(root, control, intent).locked() as store:
        store.prepare(intent)
        state = store.read()
        if state is None:
            refuse("OUTCOME_UNKNOWN", "/state")
        if state["status"] == "owner_completed":
            return _recover_owner_return(store, intent, state)
        if state["status"] in {"committed", "refused"}:
            return _terminal_state(store, intent, state)
        checkpoint(operation_control, "before_reconciliation_capture")
        config, revision_id = _configuration(root)
        completion, desired, supported, present, kind = False, False, False, False, "none"
        outcome = "original_unknown"
        owner_observation = {"reason": "owner_completion_requires_retained_return"}
        operation, arguments, basis = intent["operation"], intent["arguments"], state["owner_basis"] or {}
        if state["result"] is not None:
            completion, supported, kind = True, True, "owner_return"
            outcome = "owner_return_success"
            if _completed_status(operation, state["result"]) == "partial":
                outcome = "owner_return_partial"
            if state["error"] is not None:
                outcome = "owner_return_with_inputs_changed" if state["error"].get("error_code") == "MUTATION_INPUTS_CHANGED" else "owner_return_with_transport_uncertainty"
            owner_observation = {"retained_result_sha256": digest(state["result"]),
                "retained_error_code": state["error"].get("error_code") if state["error"] else None,
                "current_acceptance": "not_evaluated"}
        else:
            if _generation() != intent["owner_generation"]:
                refuse("GENERATION_CONFLICT", "/owner_generation")
            if basis.get("configuration_sha256") != digest(config):
                refuse("CONFIGURATION_CONFLICT", "/owner_basis/configuration_sha256")
            current_context().require_grant(_owner_grant(operation))
            from .workspace import Workspace
            with Workspace.open(root) as workspace:
                try:
                    if operation in STAGED_OPERATIONS:
                        supported = True
                        kind = ("capture_pair" if operation in STAGED_CAPTURES else
                                "requirements_postconditions" if operation in STAGED_REQUIREMENTS else
                                "draft_postconditions" if operation in STAGED_DRAFTS else
                                "source_postconditions" if operation in STAGED_SOURCES else "coverage_postconditions")
                        options = {"operation_control": operation_control} if operation in STAGED_SOURCES and operation_control is not None else {}
                        result = _recovery_module(operation).prove(root, intent, basis, store, **options)
                        present, desired, completion = result["present"], result["desired"], result["completion"]
                        owner_observation = result["observation"]
                        outcome = result["operation_outcome"]
                    elif operation == "sources.capture":
                        supported, kind = True, "capture_pair"
                        present, desired, owner_observation = _capture_proof(root, arguments, config, revision_id)
                    elif operation in {"coverage.init", "coverage.link"}:
                        supported, kind = True, "coverage_postconditions"
                        present, desired, owner_observation = _coverage_proof(root, arguments, basis, config, revision_id, workspace)
                    elif operation in {"source_requests.add", "source_requests.fulfill"}:
                        supported, kind = True, "request_postconditions"
                        present, desired, owner_observation = _request_proof(root, arguments, basis, config, revision_id, workspace)
                    elif operation == "questions.add_batch" and "preview" in basis:
                        supported, kind = True, "request_postconditions"
                        present, desired, owner_observation = _intake_proof(root, basis, revision_id)
                    elif operation == "strict.register_review":
                        supported, kind = True, "strict_checkpoint"
                        present, completion, owner_observation = _strict_proof(root, arguments, config, workspace)
                    elif operation in {"sources.inventory", "normalize.sources", "coverage.evaluate", "coverage.revision"}:
                        owner_observation = _unproved_observation(root, intent, config, revision_id, workspace)
                except (Exception, SystemExit) as error:
                    owner_observation = {"owner_error": _error(error), "postconditions_proved": False}
        checkpoint(operation_control, "before_reconciliation_validation")
        if (_capture(root)["revision_id"] != revision_id
                or _host_selection() != intent["host_context_sha256"]
                or state["result"] is None and _generation() != intent["owner_generation"]):
            refuse("REVISION_CONFLICT", "/reconciliation")
        if desired:
            outcome = "desired_state_only"
        elif completion and kind == "strict_checkpoint":
            outcome = "authenticated_event"
        status = "reconciled" if desired or completion and outcome in {"owner_return_success", "authenticated_event"} else "partial" if present or completion else "commit_unknown"
        proof = {"current_revision": revision_id, "owner_observation": owner_observation,
            "completion_proved": completion, "desired_state_proved": desired, "recovery_supported": supported,
            "proof_kind": kind, "operation_outcome": outcome}
        checkpoint(operation_control, "before_reconciliation_checkpoint")
        state["reconciliation"] = proof
        state["status"] = status
        store.write(checked(STATE, state), expected_sha256=store.state_sha256())
        return checked(RECONCILIATION, {"schema_version": RECONCILIATION,
                "request_id": request_id, "status": status, **proof, "historical": True,
                "authorized": False, "evidence_accepted": False})
