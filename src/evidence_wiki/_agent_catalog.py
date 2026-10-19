"""Closed installed-resource names and source-relative distribution anchors."""

from .onboarding_schemas import schema_ids

CATALOG_PATH = "workspace-template/docs/agent-resources/catalog.json"
STRICT_SCHEMAS = tuple(f"evidence-strict-{name}/v{version}" for name, version in (
    ("policy", 1), ("claims", 1), ("review", 1), ("result", 1), ("publication", 1), ("action", 1),
    ("claims", 2), ("review", 2), ("result", 2), ("publication", 2),
))
STRICT_SCHEMAS += ("evidence-machine-review-policy/v1", "evidence-strict-review-status/v1")
STRICT_SCHEMAS += ("evidence-strict-policy/v2", "evidence-strict-authorship/v2",
    "evidence-strict-review/v3", "evidence-strict-result/v3", "evidence-strict-publication/v3",
    "evidence-strict-review-status/v2", "evidence-review-dependency-basis/v1",
    "research-dependency-review-material/v1", "evidence-review-delivery-binding/v1",
    "evidence-review-producer/v1", "evidence-review-producer-manifest/v1",
    "evidence-review-producer-inventory/v1")
COMPUTATION_SCHEMAS = ("evidence-computation-definition/v1", "evidence-computation-result/v1")
RESOLUTION_SCHEMAS = ("research-question-resolution/v1", "research-question-resolution/v2")
REVIEW_WIRE_SCHEMAS = ("research-review-native-bytes/v1",)
COMMAND_SCHEMAS = ("research-command-request/v1", "research-command-result/v1")
SEMANTIC_SCHEMAS = ("research-semantic-judging-plan/v1", "research-semantic-judging-input/v1",
    "research-semantic-findings/v1", "research-semantic-adjudication/v1", "research-semantic-judging-report/v1")
PERFORMANCE_SCHEMAS = ("research-performance-selection/v1", "research-performance-audit/v1", "research-performance-qualification/v1")
REFRESH_SCHEDULE_SCHEMAS = ("research-refresh-schedule/v1", "research-refresh-notice/v1", "research-refresh-schedule-status/v1", "research-refresh-schedule-cursor/v1")
REFRESH_SCHEMAS = ("research-refresh-result/v1",)
BRIEF_SCHEMAS = ("research-current-brief/v1", "research-brief-markdown/v1")
READABILITY_SCHEMAS = ("research-readability-input/v1", "research-readability-issues/v1", "research-readability-plan/v1", "research-readability-input-handoff/v1")
REPAIR_SCHEMAS = ("research-repair-diff/v1", "research-repair-progress/v1", "research-service-repair-binding/v1", "research-native-input-handoff/v1")
SERVICE_SCHEMAS = ("research-review-service-plan/v1", "research-review-service-status/v1", "research-review-service-result/v1")
HOST_QUALIFICATION_SCHEMAS = ("research-host-qualification/v1", "research-host-qualification/v2", "research-protected-host-qualification/v1")
RECOVERY_SCHEMAS = ("research-recovery-status/v1",)
CALCULATION_SCHEMAS = ("evidence-calculation-proposal/v1", "evidence-calculation-selection/v1", "research-calculation-context/v1")
REVIEW_PACKET_SCHEMAS = ("research-review-delivery/v1", "research-review-page/v1", "research-paged-review/v1",
    "research-review-packet/v1", "research-host-assembled-review/v1", "research-complete-review-delivery/v1",
    "research-review-sections/v1", "research-blinded-review-input/v1",
    "research-review-delivery/v2", "research-paged-review/v2", "research-review-packet/v2",
    "research-host-assembled-review/v2", "research-complete-review-delivery/v2",
    "research-review-sections/v2", "research-blinded-review-input/v2")
REVIEW_PACKET_SCHEMAS += tuple("research-" + name + "/v3" for name in (
    "review-delivery", "paged-review", "review-packet", "host-assembled-review",
    "complete-review-delivery", "review-sections", "blinded-review-input"))
GUIDES = {
    "bootstrap": "docs/installed-agent.md", "pack-authoring": "skills/domain-pack-create.md",
    "initialization": "skills/research-init.md", "init-profile": "docs/workspace-init-profile.md",
    "contracts": "docs/agent-contracts.md", "strict-evidence": "docs/strict-evidence.md",
    "computation": "docs/declarative-computation.md", "research": "skills/research-run.md",
    "verification": "skills/research-verify.md", "acquisition": "skills/research-acquire.md",
    "discovery": "skills/research-discover.md",
    "frameworks": "docs/frameworks.md",
    "agent-host": "docs/agent-host.md",
    "agent-generator": "docs/agent-generator.md",
    "integration": "docs/integration.md",
    "semantic-judging": "docs/semantic-judging.md",
}
COMPUTATION_SCRIPTS = (
    "_computation_contract", "_computation_expression", "_computation_runtime", "_computation_schedule",
    "_computation_effects", "_computation_service", "_computation_cli",
    "aggregate_records", "evaluate_formulas", "verify_assertions", "schedule_milestones",
)


DELIVERY_SCHEMAS = ("research-artifact-delivery-plan/v1", "research-artifact-delivery-input/v1",
    "research-artifact-delivery-proposal/v1", "research-artifact-delivery-result/v1")

def resource_paths() -> dict[str, str]:
    """Return fixed IDs; caller data can never add an inventory entry."""
    paths = {f"guide/{name}/v1": f"workspace-template/{path}" for name, path in GUIDES.items()}
    paths["guide/orchestrator/v1"] = "orchestrator/skills/research-orchestrate.md"
    for schema_id in (*schema_ids(), *STRICT_SCHEMAS, *COMPUTATION_SCHEMAS, *CALCULATION_SCHEMAS, *RECOVERY_SCHEMAS, *REVIEW_PACKET_SCHEMAS, *REVIEW_WIRE_SCHEMAS, *SERVICE_SCHEMAS, *HOST_QUALIFICATION_SCHEMAS, *REPAIR_SCHEMAS, *RESOLUTION_SCHEMAS, *BRIEF_SCHEMAS, *REFRESH_SCHEMAS, *REFRESH_SCHEDULE_SCHEMAS, *PERFORMANCE_SCHEMAS, *COMMAND_SCHEMAS, *READABILITY_SCHEMAS, *DELIVERY_SCHEMAS, *SEMANTIC_SCHEMAS):
        paths[schema_id] = "workspace-template/docs/agent-resources/" + schema_id.replace("/", "--") + ".json"
    for stem in (*COMPUTATION_SCRIPTS, "_strict_contract", "_strict_evidence", "strict_evidence"):
        paths[f"script/{stem}/v1"] = f"workspace-template/scripts/{stem}.py"
    for name in ("strict-policy", "init-profile", "pack"):
        paths[f"example/{name}/v1"] = f"workspace-template/docs/agent-resources/{name}.json"
    for name in ("sample-benchmark", "sample-portfolio", "sample-filing", "unattended-host", "review-cases", "agent-host", "integration-docs"):
        paths[f"example/{name}/v1"] = f"workspace-template/docs/agent-resources/{name}.json"
    paths.update({
        "framework/compatibility/v1": "workspace-template/docs/frameworks/compatibility.json",
        "framework/bundle/v1": "workspace-template/docs/agent-resources/framework-bundle.json",
        "evidence-framework-call/v1": "workspace-template/docs/agent-resources/framework-call.json",
        "evidence-framework-call/v2": "workspace-template/docs/agent-resources/framework-call--v2.json",
        "evidence-framework-result/v1": "workspace-template/docs/agent-resources/framework-result.json",
    })
    return paths
