"""Discoverable bounded lifecycle documents and optional transport boundaries."""

from __future__ import annotations

from .onboarding_schemas import _nullable
from .planning_contracts import array, obj, string


def validate(raw, schema):
    from ._pack_io import json_document
    from .onboarding_contract import _matches
    from .source_commands import _no_secret_values

    value = json_document(raw)
    _matches(value, schemas()[schema])
    _no_secret_values(value)
    return value


def schemas():
    from . import fleet_revisions, host_transitions, native_instructions, pack_composition, pack_migrations
    from .pack_revision_contracts import PLAN, REEVALUATION
    from .pack_revision_contracts import schemas as revision_schemas
    from .planning_contracts import PLAN as SETUP
    from .planning_contracts import schemas as setup_schemas

    text, opaque = string(4096), {"type": "object", "additionalProperties": True}
    revision = revision_schemas()
    result = pack_migrations.schemas()
    member = obj(path=text, alias={**string(32), "pattern": r"^[a-z][a-z0-9-]{0,31}$"}, applicability=text)
    compose = obj(schema_version={"const": pack_composition.REQUEST}, name={**string(48), "pattern": r"^[a-z][a-z0-9-]{0,47}$"},
        version=string(64), scope=text, members=array(member, 8, 2))
    result[pack_composition.REQUEST] = compose
    result[pack_composition.PLAN] = obj(schema_version={"const": pack_composition.PLAN}, request=compose,
        members=array(obj(path=text, tree_sha256=string(128)), 8, 2), tree_sha256=string(128),
        semantic_adequacy={"const": "not_certified"}, plan_id=string(128))
    fleet = obj(schema_version={"const": fleet_revisions.REQUEST},
        candidate=obj(path=_nullable(text), catalog=_nullable(text), revision=_nullable(string(128))), rationale=text,
        workspaces=array(obj(target=text, keep_local=array(string(512), 256), accept_pack=array(string(512), 256)), 16, 1))
    result[fleet_revisions.REQUEST] = fleet
    result[fleet_revisions.PLAN] = obj(schema_version={"const": fleet_revisions.PLAN}, request=fleet,
        proposals=array(opaque, 16, 1), bounds=opaque, auto_propagation={"const": False}, plan_id=string(128))
    result[fleet_revisions.APPLY] = obj(schema_version={"const": fleet_revisions.APPLY}, plan=result[fleet_revisions.PLAN], targets=array(text, 16, 1))
    transition = obj(schema_version={"const": host_transitions.REQUEST}, transition_id=string(64), control_root=text, target=text,
        previous_session=_nullable(string(64)), next_session=obj(agent_id=string(64), orchestration_id=string(64)),
        setup_plan=_nullable(setup_schemas()[SETUP]),
        revision_plan={"anyOf": [revision[PLAN], result[pack_migrations.PLAN], {"type": "null"}]},
        reevaluations=array(revision[REEVALUATION], 300))
    result[host_transitions.REQUEST] = transition
    result[host_transitions.PLAN] = obj(schema_version={"const": host_transitions.PLAN}, request=transition, before=opaque,
        generation=opaque, assurance={"const": "artifact_checked"}, protected_parent={"const": "unsupported"},
        release_accepted={"const": False}, plan_id=string(128))
    native = obj(schema_version={"const": native_instructions.REQUEST}, framework={"enum": list(native_instructions.LOCATIONS)},
        version=string(128), scope={"enum": ["project", "user"]}, root=text)
    result[native_instructions.REQUEST] = native
    result[native_instructions.PLAN] = obj(schema_version={"const": native_instructions.PLAN}, request=native,
        generation=opaque, root_identity=opaque, relative_path=text, files=opaque, instruction_sha256=string(128),
        activation={"const": "host_discovery_and_trust_required"}, global_configuration_changed={"const": False}, plan_id=string(128))
    return {key: {"$schema": "https://json-schema.org/draft/2020-12/schema", **value} for key, value in result.items()}


def index():
    return {"capability": "scoped-onboarding/v1", "api": "evidence_wiki.Onboarding.open",
        "server": "evidence-wiki serve-onboarding-mcp", "discovery": "evidence-wiki agent extensions",
        "guide": "guide/contracts/v1", "schema_ids": list(schemas()), "allowed_roots_maximum": 16,
        "authority": "explicit host roots and operation grants; ordinary local observations, no OS isolation claim",
        "preserved_surfaces": ["Workspace", "serve-mcp"], "python_api_version": "13",
        "commands": ["pack migration-plan", "pack migration-apply", "pack compose-plan", "pack compose",
            "pack fleet-plan", "pack fleet-apply", "agent transition-plan", "agent transition-apply",
            "agent recipes", "agent instructions-plan", "agent instructions-apply", "agent instructions-remove"],
        "limits": {"fleet_workspaces": 16, "composition_members": 8, "input_bytes": 1048576},
        "platform": "POSIX anchored roots and mutation publishers; rootless content/bootstrap is independent",
        "host_session_schema": {"contract_key": "orchestration_host_session", "version": "1.1"},
        "completion": "Historical receipts never replace current strict review and research export."}


def contract():
    from .capability_recipes import recipes
    from .onboarding_tools import contract as tools_contract

    return {**index(), "schema_version": "evidence-onboarding-extensions/v1", "schemas": schemas(),
            "mcp": tools_contract(), "recipes": recipes()}
