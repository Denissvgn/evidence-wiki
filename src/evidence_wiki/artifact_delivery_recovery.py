"""One registered staged publication owns bytes; private source custody is required."""

from __future__ import annotations

from ._filesystem import os
from ._pack_io import json_document
from .artifact_delivery_contracts import PROPOSAL, checked, refuse
from .artifact_delivery_input import _ACTIVE
from .artifact_publication import _PublicationWitness
from .brief_projection import project, render
from .mutation_contracts import canonical, digest
from .mutation_store import MutationStore
from .resolution_operations import _HELD, _held_artifact_input, _OwnedArtifactInput
from .source_delivery import _directory, _publish

BASIS = "research-artifact-publication-basis/v1"


def _held(root, intent, store):
    admission, observed = _ACTIVE.get(), _HELD.get()
    if (admission is None or type(observed) is not _OwnedArtifactInput or type(store) is not MutationStore
            or not store._active or store._historical_readonly or store.intent != intent
            or intent["operation"] != "artifact.deliver" or store.workspace_root != root):
        refuse("publication_owned_scope_required")
    session = admission.session
    admission.held(session)
    observed.require(session)
    if admission.identifier != intent["request_id"] or canonical(intent["arguments"]) != admission.arguments:
        refuse("publication_request_changed")
    return session, observed


def prepare(root, intent, store):
    admission = _ACTIVE.get()
    if admission is None:
        refuse("publication_native_scope_required")
    with _held_artifact_input(admission.session, admission):
        result = _prepare_held(root, intent, store)
    return result


def _prepare_held(root, intent, store):
    session, observed = _held(root, intent, store)
    plan = session.execution.profile.artifact_delivery
    arguments = intent["arguments"]
    proposed = checked(PROPOSAL, arguments["proposal"])
    if arguments["plan_sha256"] != plan.sha256 or proposed["plan_sha256"] != plan.sha256:
        refuse("publication_plan_changed")
    state = observed.bind_readability() if session.execution.profile.readability is not None else None
    brief = project(observed._result, readability=state)
    if (brief["verdict"] not in {"accept", "partial"} or brief["global_blockers"]
            or not brief["accounting_complete"] or brief["readability"]["state"] in {"pending", "refused"}):
        refuse("publication_current_brief_ineligible")
    rendered = render(brief)
    raw = rendered["markdown"].encode()
    destination = plan.destination(proposed["destination_ref"])
    if (len(raw) > plan.max_output_bytes or proposed["artifact_id"] != rendered["markdown_sha256"]
            or proposed["artifact_sha256"] != rendered["markdown_sha256"] or proposed["artifact_bytes"] != len(raw)):
        refuse("publication_artifact_changed")
    path = "runs/" + session.run_id + "/deliveries/" + destination.filename
    session.context.require_path(session.root / path, write=True)
    output = {"schema_version": BASIS, "request_id": intent["request_id"], "input_sha256": intent["input_sha256"],
        "before_revision": intent["expected_revision"], "configuration_sha256": store.read()["owner_basis"]["configuration_sha256"],
        "plan_sha256": plan.sha256, "path": path, "proposal": proposed, "brief_sha256": digest(brief),
        "artifact": store.stage_blob("artifact-bytes", raw)}
    descriptor = store.stage_blob("artifact-plan", canonical(output))
    return {"kind": "artifact.deliver", "configuration_sha256": output["configuration_sha256"],
        "schema_version": BASIS, "plan": descriptor}


def invoke(root, intent, basis, store):
    admission = _ACTIVE.get()
    if admission is None:
        refuse("publication_native_scope_required")
    with _held_artifact_input(admission.session, admission):
        result = _invoke_held(root, intent, basis, store)
    return result


def _invoke_held(root, intent, basis, store):
    session, observed = _held(root, intent, store)
    if (type(basis) is not dict or set(basis) != {"kind", "configuration_sha256", "schema_version", "plan"}
            or basis["kind"] != "artifact.deliver" or basis["schema_version"] != BASIS):
        refuse("publication_staged_basis_invalid")
    plan = json_document(store.read_stage(basis["plan"]))
    if (set(plan) != {"schema_version", "request_id", "input_sha256", "before_revision", "configuration_sha256",
            "plan_sha256", "path", "proposal", "brief_sha256", "artifact"}
            or plan["schema_version"] != BASIS or plan["request_id"] != intent["request_id"]
            or plan["input_sha256"] != intent["input_sha256"] or plan["before_revision"] != intent["expected_revision"]
            or plan["configuration_sha256"] != basis["configuration_sha256"]
            or plan["plan_sha256"] != session.execution.profile.artifact_delivery.sha256
            or plan["proposal"] != intent["arguments"]["proposal"]):
        refuse("publication_staged_plan_invalid")
    raw = store.read_stage(plan["artifact"])
    state = observed.bind_readability() if session.execution.profile.readability is not None else None
    brief = project(observed._result, readability=state)
    if (brief["verdict"] not in {"accept", "partial"} or brief["global_blockers"] or not brief["accounting_complete"]
            or brief["readability"]["state"] in {"pending", "refused"}
            or digest(brief) != plan["brief_sha256"] or render(brief)["markdown"].encode() != raw):
        refuse("publication_current_artifact_changed")
    witness = _PublicationWitness(session, observed, store, plan["path"], raw)
    root_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    parent = None
    try:
        parent = _directory(root_descriptor, plan["path"].rsplit("/", 1)[0])
        session.context.require_path(root / plan["path"], write=True)
        _publish(parent, witness.name, raw, custody=witness)
        witness.verify_link()
        return {"schema_version": "research-artifact-publication-return/v1", "request_id": intent["request_id"],
            "path": plan["path"], "artifact_sha256": plan["proposal"]["artifact_sha256"], "bytes": len(raw),
            "destination_ref": plan["proposal"]["destination_ref"], "publication_inode": witness.proof,
            "basis_id": observed._basis_id, "boundary": "artifact_checked", "evidence_accepted": False}
    finally:
        if parent is not None:
            os.close(parent)
        os.close(root_descriptor)
