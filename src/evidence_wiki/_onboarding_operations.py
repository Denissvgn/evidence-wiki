"""Static public method and effect declarations for scoped lifecycle handles."""

METHODS = ("open", "close", "bootstrap", "resources", "resource", "recipes", "contracts", "inspect", "plan", "check_plan", "apply",
    "pack_list", "pack_show", "pack_decide", "pack_scaffold", "pack_derive", "capture", "revision_plan", "revision_apply",
    "revision_status", "migration_plan", "migration_apply", "reevaluate", "composition_plan", "composition_apply", "fleet_plan",
    "fleet_apply", "transition_plan", "transition_apply", "instructions_plan", "instructions_apply", "instructions_remove",
    "research_next", "research", "research_export", "computation")

_WRITES = {"apply", "pack_scaffold", "pack_derive", "capture", "revision_apply", "migration_apply", "reevaluate", "composition_apply", "fleet_apply", "transition_apply", "instructions_apply", "instructions_remove", "research", "computation"}
_SHELL = {"open": None, "close": None, "bootstrap": "agent summary / agent", "resources": "agent resources",
    "resource": "agent resource", "recipes": "agent recipes", "contracts": "agent extensions", "inspect": "agent inspect",
    "plan": "agent plan", "check_plan": "agent plan-check", "apply": "agent apply", "capture": "agent capture",
    "pack_list": "pack list", "pack_show": "pack show", "pack_decide": "pack decide", "pack_scaffold": "pack scaffold", "pack_derive": "pack derive",
    "revision_plan": "pack revision-plan", "revision_apply": "pack revision-apply", "revision_status": "pack revision-status",
    "migration_plan": "pack migration-plan", "migration_apply": "pack migration-apply", "reevaluate": "pack reevaluate",
    "composition_plan": "pack compose-plan", "composition_apply": "pack compose", "fleet_plan": "pack fleet-plan", "fleet_apply": "pack fleet-apply",
    "transition_plan": "agent transition-plan", "transition_apply": "agent transition-apply", "instructions_plan": "agent instructions-plan",
    "instructions_apply": "agent instructions-apply", "instructions_remove": "agent instructions-remove", "research_next": "agent next",
    "research": "agent start / resume / heartbeat / ingest", "research_export": "agent research-export", "computation": "computation"}


def rows():
    return tuple(("onboarding." + name, "evidence-wiki " + _SHELL[name] if _SHELL[name] else None,
        "explicit per-operation grant; canonical owner effects inside selected roots" if name in _WRITES else "no workspace writes; validation may allocate private temporary resources",
        "canonical lifecycle/coverage/source/host locks; root descriptors and generation checked on every call",
        "installed current-interpreter worker/controller for setup or host transition; no arbitrary runner or model"
        if name in {"apply", "transition_apply"} else "ingest may invoke a fixed native extractor; unqualified adapters refuse"
        if name == "research" else "none; no external provider or executable probes") for name in METHODS)
