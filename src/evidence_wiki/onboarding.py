"""Explicit scoped lifecycle handles over shared package and workspace owners."""

from __future__ import annotations

import json
from pathlib import Path

from ._onboarding_scope import Scope, scoped_operation
from ._pack_io import canonical, json_document
from .errors import ConfigError
from .pack_discovery import owner
from .pack_migrations import _call
from .planning_contracts import refuse
from .runtime_identity import generation

READ = frozenset({"bootstrap", "resources", "resource", "recipes", "inspect", "plan", "check_plan",
    "revision_plan", "revision_status", "migration_plan", "composition_plan", "fleet_plan", "transition_plan",
    "research_next", "research_export", "computation.check", "computation.aggregate", "computation.evaluate",
    "computation.verify", "computation.schedule", "instructions_plan", "contracts", "pack_list", "pack_show", "pack_decide"})
WRITE = frozenset({"apply", "capture", "revision_apply", "migration_apply", "reevaluate", "composition_apply",
    "fleet_apply", "transition_apply", "research.start", "research.resume", "research.heartbeat", "research.ingest",
    "computation.write", "computation.apply-warnings", "computation.dispatch", "instructions_apply", "instructions_remove", "pack_scaffold", "pack_derive"})

_PROCESS_GENERATION = None


def document(value):
    try:
        raw = value if isinstance(value, bytes) else canonical(value)
    except (TypeError, ValueError, RecursionError):
        refuse("onboarding_document_invalid")
    return raw, json_document(raw)


class Onboarding:
    """A host explicitly selects roots and mutation grants; workspace handles are unchanged.

    Resource extraction is borrowed from the shared package owner. Closing this
    handle releases its root descriptors, never another handle's assets. Restart
    after an installation/code change. Operations use the current interpreter;
    no model, arbitrary command, provider probe or hidden runner is selected.
    """

    def __init__(self, roots, allow):
        global _PROCESS_GENERATION

        if not isinstance(roots, (list, tuple)) or not isinstance(allow, (list, tuple, set, frozenset)) or any(not isinstance(v, str) for v in allow):
            refuse("onboarding_scope_or_grants_invalid")
        if not set(allow) <= READ | WRITE:
            refuse("onboarding_operation_grant_unknown")
        self._scope = Scope(tuple(roots))
        self._allow = READ | frozenset(allow)
        try:
            self._generation = generation()
            if _PROCESS_GENERATION is None:
                _PROCESS_GENERATION = self._generation
            elif self._generation != _PROCESS_GENERATION:
                refuse("onboarding_generation_changed_restart_required", "ONBOARDING_PLAN_STALE")
        except BaseException:
            self._scope.close()
            raise
        self.closed = False

    @classmethod
    def open(cls, *, allowed_roots=(), allow=()):
        return cls(allowed_roots, allow)

    def __enter__(self):
        self._check("bootstrap")
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if not self.closed:
            self._scope.close()
            self.closed = True

    def _check(self, operation):
        if self.closed:
            raise ConfigError("WORKSPACE_UNREADABLE", "Onboarding handle is closed.")
        if operation not in self._allow:
            refuse("onboarding_operation_not_granted", "ONBOARDING_AUTHORITY_REQUIRED")
        self._scope.verify()
        if generation() != self._generation:
            refuse("onboarding_generation_changed_restart_required", "ONBOARDING_PLAN_STALE")

    def _finish(self, value):
        self._scope.verify()
        if generation() != self._generation:
            refuse("onboarding_generation_changed_restart_required", "ONBOARDING_PLAN_STALE")
        if len(canonical(value)) > 2_097_152:
            refuse("onboarding_result_bound", "ONBOARDING_LIMIT")
        return value

    @scoped_operation
    def bootstrap(self, target=None, *, requirements=(), assurance="artifact_checked", request_id="bootstrap"):
        from . import agent
        from .onboarding_contract import encode_document

        self._check("bootstrap")
        if target is None:
            payload = agent.capabilities()
            agent._negotiate(payload, list(requirements), assurance)
            kind, version = "capabilities", "1"
        else:
            root = self._scope.path(target)
            payload = agent.bootstrap(str(root), requirements=list(requirements), assurance=assurance)
            kind, version = "bootstrap", "2"
        result = {"schema_version": version + ".0", "kind": kind, "request_id": request_id, "payload": payload}
        return self._finish(json.loads(encode_document("onboarding/" + kind + "/v" + version, result)))

    def resources(self):
        from .agent_resources import resource_index

        self._check("resources")
        return self._finish(resource_index())

    def resource(self, resource_id):
        from .agent_resources import resource_document

        self._check("resource")
        return self._finish(resource_document(resource_id))

    def recipes(self):
        from .capability_recipes import recipes

        self._check("recipes")
        return self._finish(recipes())

    def contracts(self):
        from .extension_contracts import contract

        self._check("contracts")
        return self._finish(contract())

    def _pack_scope(self, *, target=None, catalog=None, path=None):
        if target is not None:
            self._scope.path(target)
        if catalog is not None:
            self._scope.catalog(catalog)
        if path is not None:
            self._scope.path(path, installation=True)

    @scoped_operation
    def pack_list(self, *, target=None, catalog=None):
        from .pack_discovery import inventory

        self._check("pack_list")
        self._pack_scope(target=target, catalog=catalog)
        return self._finish(inventory(target=target, catalog=catalog))

    @scoped_operation
    def pack_show(self, selector=None, *, target=None, catalog=None, path=None, resource=None):
        from .pack_discovery import select

        self._check("pack_show")
        self._pack_scope(target=target, catalog=catalog, path=path)
        result, _ = select(selector, target=target, catalog=catalog, path=path, resource=resource)
        return self._finish(result)

    @scoped_operation
    def pack_decide(self, value, *, target=None, catalog=None):
        from .pack_decisions import decide

        self._check("pack_decide")
        self._pack_scope(target=target, catalog=catalog)
        raw, _ = document(value)
        return self._finish(decide(raw, target=target, catalog=catalog))

    @scoped_operation
    def pack_scaffold(self, value, *, output):
        from .pack_authoring import scaffold

        self._check("pack_scaffold")
        self._scope.path(output, write=True)
        raw, _ = document(value)
        return self._finish(scaffold(raw, output=output))

    @scoped_operation
    def pack_derive(self, value, *, output):
        from .pack_authoring import derive
        from .pack_authoring_contracts import DERIVE, decode

        self._check("pack_derive")
        self._scope.path(output, write=True)
        raw, _ = document(value)
        request = decode(raw, DERIVE)
        base = request["base"]
        self._pack_scope(target=base["target"], catalog=base["catalog"], path=base["path"])
        return self._finish(derive(raw, output=output))

    def inspect(self, target, *, source_ids=(), source_paths=(), host_tools=None):
        from .source_inspection import inspect

        self._check("inspect")
        root = self._scope.path(target)
        return self._finish(inspect(target=str(root), source_ids=list(source_ids), source_paths=list(source_paths), host_tools=host_tools))

    @scoped_operation
    def plan(self, value):
        from .planning import compile_plan

        self._check("plan")
        raw, _ = document(value)
        self._scope.setup(raw)
        return self._finish(compile_plan(raw))

    @scoped_operation
    def check_plan(self, value):
        from .planning import check_plan

        self._check("check_plan")
        raw, _ = document(value)
        self._scope.setup(raw)
        return self._finish(check_plan(raw))

    @scoped_operation
    def apply(self, value):
        from .setup_application import apply_plan

        self._check("apply")
        raw, _ = document(value)
        self._scope.setup(raw)
        return self._finish(apply_plan(raw))

    def capture(self, target, value, *, path):
        from .source_delivery import deliver

        self._check("capture")
        root = self._scope.builtin_workspace(target)
        self._scope.path(root, write=True)
        raw, _ = document(value)
        return self._finish(deliver(raw, target=str(root), path=path))

    @scoped_operation
    def _revision(self, operation, value):
        from . import pack_migrations, pack_revisions
        from .pack_revision_contracts import PLAN, REQUEST, decode

        self._check(operation)
        raw, _ = document(value)
        if operation.startswith("migration"):
            parsed = pack_migrations.decode(raw, pack_migrations.PLAN if operation.endswith("apply") else pack_migrations.REQUEST)
        else:
            parsed = decode(raw, PLAN if operation.endswith("apply") else REQUEST)
        request = parsed["request"] if operation.endswith("apply") else parsed
        self._scope.revision(request)
        self._scope.builtin_workspace(request["target"])
        module = pack_migrations if operation.startswith("migration") else pack_revisions
        call = module.apply if operation.endswith("apply") else module.plan
        result = _call(call, raw if module is pack_migrations or operation.endswith("apply") else parsed)
        return self._finish(result)

    def revision_plan(self, value):
        return self._revision("revision_plan", value)

    def revision_apply(self, value):
        return self._revision("revision_apply", value)

    def migration_plan(self, value):
        return self._revision("migration_plan", value)

    def migration_apply(self, value):
        return self._revision("migration_apply", value)

    def revision_status(self, target, *, evaluate=False):
        from .pack_revisions import status

        self._check("revision_status")
        self._scope.builtin_workspace(target)
        return self._finish(_call(status, target, evaluate=evaluate))

    def reevaluate(self, target, value):
        from .pack_revision_contracts import REEVALUATION, decode

        self._check("reevaluate")
        root = self._scope.builtin_workspace(target)
        self._scope.path(root, write=True)
        raw, _ = document(value)
        return self._finish(_call(owner("coverage_manifest").run_revision, root, decode(raw, REEVALUATION)))

    @scoped_operation
    def _composition(self, operation, value, output=None):
        from . import pack_composition

        self._check(operation)
        raw, _ = document(value)
        parsed = pack_composition.decode(raw) if operation.endswith("apply") else pack_composition.request(raw)
        request = parsed["request"] if operation.endswith("apply") else parsed
        for member in request["members"]:
            self._scope.path(member["path"], installation=True)
        if operation.endswith("apply"):
            self._scope.path(output, write=True)
            return self._finish(pack_composition.apply(raw, output=output))
        return self._finish(pack_composition.plan(raw))

    def composition_plan(self, value):
        return self._composition("composition_plan", value)

    def composition_apply(self, value, *, output):
        return self._composition("composition_apply", value, output)

    @scoped_operation
    def _fleet(self, operation, value):
        from . import fleet_revisions

        self._check(operation)
        raw, _ = document(value)
        parsed = fleet_revisions.decode(raw) if operation.endswith("apply") else fleet_revisions.request(raw)
        request = parsed["plan"]["request"] if operation.endswith("apply") else parsed
        for selected in request["workspaces"]:
            self._scope.revision({**request["candidate"], "target": selected["target"]})
            self._scope.builtin_workspace(selected["target"])
        if operation.endswith("apply"):
            for proposal in parsed["plan"]["proposals"]:
                if proposal["plan"] is not None:
                    self._scope.revision(proposal["plan"]["request"])
            return self._finish(fleet_revisions.apply(raw))
        return self._finish(fleet_revisions.plan(raw))

    def fleet_plan(self, value):
        return self._fleet("fleet_plan", value)

    def fleet_apply(self, value):
        return self._fleet("fleet_apply", value)

    @scoped_operation
    def _transition(self, operation, value):
        from . import host_transitions

        self._check(operation)
        raw, _ = document(value)
        parsed = host_transitions.decode(raw) if operation.endswith("apply") else host_transitions.request(raw)
        request = parsed["request"] if operation.endswith("apply") else parsed
        self._scope.path(request["target"], write=True)
        self._scope.path(Path(request["control_root"]) / "host-transitions", write=True)
        if request["setup_plan"] is not None:
            self._scope.setup(canonical(request["setup_plan"]))
        if request["revision_plan"] is not None:
            self._scope.revision(request["revision_plan"]["request"])
        return self._finish(host_transitions.apply(raw) if operation.endswith("apply") else host_transitions.plan(raw))

    @scoped_operation
    def _instructions(self, operation, value):
        from . import native_instructions

        self._check(operation)
        raw, _ = document(value)
        selected = native_instructions.request(raw) if operation == "instructions_plan" else native_instructions.decode(raw)["request"]
        self._scope.path(selected["root"], write=True)
        result = native_instructions.plan(raw) if operation == "instructions_plan" else native_instructions.apply(raw, remove=operation == "instructions_remove")
        return self._finish(result)

    def instructions_plan(self, value):
        return self._instructions("instructions_plan", value)

    def instructions_apply(self, value):
        return self._instructions("instructions_apply", value)

    def instructions_remove(self, value):
        return self._instructions("instructions_remove", value)

    def transition_plan(self, value):
        return self._transition("transition_plan", value)

    def transition_apply(self, value):
        return self._transition("transition_apply", value)

    def research_next(self, target, *, agent_id=None, run_id=None):
        from .research_actions import guidance

        self._check("research_next")
        self._scope.builtin_workspace(target)
        return self._finish(guidance(target, agent_id=agent_id, run_id=run_id))

    def research(self, target, operation, *, agent_id, run_id, request_id=None, source_id=None, source_path=None):
        from .research_operations import ingest, run_operation

        self._check("research." + operation)
        self._scope.builtin_workspace(target)
        self._scope.path(target, write=True)
        if operation == "ingest":
            result = ingest(target, agent_id=agent_id, run_id=run_id, request_id=request_id,
                            source_id=source_id, source_path=source_path)
        elif operation in {"start", "resume", "heartbeat"}:
            result = run_operation(target, operation, agent_id=agent_id, run_id=run_id)
        else:
            refuse("onboarding_research_operation_unknown")
        return self._finish(result)

    def research_export(self, target, *, allow_partial=False, run_id=None):
        from .research_completion import completion

        self._check("research_export")
        self._scope.builtin_workspace(target)
        return self._finish(completion(target, allow_partial=allow_partial, run_id=run_id))

    def computation(self, target, operation="check", *, as_of=None, expected_result_id=None, request_id=None, cadence_id=None, dry_run=False):
        self._check("computation." + operation)
        root = self._scope.builtin_workspace(target)
        if "computation." + operation in WRITE:
            self._scope.path(root, write=True)
        return self._finish(_call(owner("_computation_service").run, root, operation, as_of=as_of,
            expected_result_id=expected_result_id, request_id=request_id, cadence_id=cadence_id, dry_run=dry_run))
