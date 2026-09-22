"""Closed installed-resource names and source-relative distribution anchors."""

from .onboarding_schemas import schema_ids

CATALOG_PATH = "workspace-template/docs/agent-resources/catalog.json"
STRICT_SCHEMAS = tuple(f"evidence-strict-{name}/v{version}" for name, version in (
    ("policy", 1), ("claims", 1), ("review", 1), ("result", 1), ("publication", 1), ("action", 1),
    ("claims", 2), ("review", 2), ("result", 2), ("publication", 2),
))
COMPUTATION_SCHEMAS = ("evidence-computation-definition/v1", "evidence-computation-result/v1")
GUIDES = {
    "bootstrap": "docs/installed-agent.md", "pack-authoring": "skills/domain-pack-create.md",
    "initialization": "skills/research-init.md", "init-profile": "docs/workspace-init-profile.md",
    "contracts": "docs/agent-contracts.md", "strict-evidence": "docs/strict-evidence.md",
    "computation": "docs/declarative-computation.md", "research": "skills/research-run.md",
    "verification": "skills/research-verify.md", "acquisition": "skills/research-acquire.md",
    "discovery": "skills/research-discover.md",
}
COMPUTATION_SCRIPTS = (
    "_computation_contract", "_computation_expression", "_computation_runtime", "_computation_schedule",
    "_computation_effects", "_computation_service", "_computation_cli",
    "aggregate_records", "evaluate_formulas", "verify_assertions", "schedule_milestones",
)


def resource_paths() -> dict[str, str]:
    """Return fixed IDs; caller data can never add an inventory entry."""
    paths = {f"guide/{name}/v1": f"workspace-template/{path}" for name, path in GUIDES.items()}
    paths["guide/orchestrator/v1"] = "orchestrator/skills/research-orchestrate.md"
    for schema_id in (*schema_ids(), *STRICT_SCHEMAS, *COMPUTATION_SCHEMAS):
        paths[schema_id] = "workspace-template/docs/agent-resources/" + schema_id.replace("/", "--") + ".json"
    for stem in (*COMPUTATION_SCRIPTS, "_strict_contract", "_strict_evidence", "strict_evidence"):
        paths[f"script/{stem}/v1"] = f"workspace-template/scripts/{stem}.py"
    for name in ("strict-policy", "init-profile", "pack"):
        paths[f"example/{name}/v1"] = f"workspace-template/docs/agent-resources/{name}.json"
    for name in ("sample-benchmark", "sample-portfolio", "sample-filing"):
        paths[f"example/{name}/v1"] = f"workspace-template/docs/agent-resources/{name}.json"
    return paths
