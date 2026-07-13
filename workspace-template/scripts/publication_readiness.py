#!/usr/bin/env python3
"""Evaluate whether a workspace is ready for publication claims.

This command is stricter than ``workspace_status.py``. A workspace can be useful
and even complete while still being unsuitable for a public demo or report. The
publication gate composes existing local artifacts only: workspace status, lint,
coverage/export summaries, discovery candidate lifecycle, optional citation
verification, and a conservative secret scan. It performs no network I/O.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to evaluate publication readiness") from exc


SCHEMA_VERSION = "1.0"
EXIT_READY = 0
EXIT_NOT_READY = 1
EXIT_UNREADABLE = 2
VERDICT_SHIP = "ship"
VERDICT_NO_SHIP = "no_ship"
VERDICT_BLOCKED = "blocked_on_sources"
VERDICT_ATTENTION = "attention_required"
REASON_CATEGORIES = (
    "coverage",
    "source_quality",
    "discovery_quality",
    "citation_identity",
    "grounding",
    "contradiction",
    "currentness",
    "curation",
    "safety",
)
COVERAGE_LINT_CATEGORIES = {
    "question_coverage_missing",
    "question_coverage_blocked",
    "question_coverage_invalid",
}
CURATION_LINT_CATEGORIES = {
    "curation_missing_terms_license",
    "curation_missing_source_note",
    "curation_missing_origin_url",
    "curation_missing_checksum",
    "curation_missing_candidate_id",
}
GROUNDING_LINT_CATEGORIES = {
    "question_grounding_missing",
    "question_grounding_self_verified",
}
CONTRADICTION_LINT_CATEGORIES = {"claim_conflict"}
REASON_POLICY = {
    "coverage": "required_facet_coverage",
    "source_quality": "resolved_questions_and_usable_sources",
    "discovery_quality": "candidate_lifecycle_integrity",
    "citation_identity": "citation_identity_quorum",
    "grounding": "retained_quote_evidence",
    "contradiction": "contradiction_adjudication",
    "currentness": "declared_freshness_policy",
    "curation": "publication_license_and_provenance",
    "safety": "publication_safety_review",
}
REASON_ARTIFACTS = {
    "coverage": ["sources/coverage/", "wiki/questions/"],
    "source_quality": ["wiki/questions/", "sources/manifest.jsonl"],
    "discovery_quality": ["sources/discovery/candidates.jsonl"],
    "citation_identity": ["citation-verification.json"],
    "grounding": ["wiki/questions/", "sources/normalized/"],
    "contradiction": ["wiki/claims/", "sources/normalized/"],
    "currentness": ["sources/coverage/", "sources/manifest.jsonl"],
    "curation": ["sources/manifest.jsonl", "raw/**/*.provenance.yml"],
    "safety": ["wiki/questions/", "runs/"],
}
REASON_REMEDIATION = {
    "coverage": "Satisfy the named required facet and rerun coverage/export before publication.",
    "source_quality": "Resolve the named question or repair the affected retained source artifact, then rerun readiness.",
    "discovery_quality": "Repair the candidate lifecycle record with an explicit audited transition and rerun readiness.",
    "citation_identity": "Correct or reacquire the exact cited work, rerun citation verification, and retain the verification artifact.",
    "grounding": "Correct the quote and page/section anchor against retained normalized evidence, then rerun quote verification.",
    "contradiction": "Adjudicate the conflicting retained claims and record the disposition without deleting counter-evidence.",
    "currentness": "Replace or date-qualify stale evidence according to the named freshness policy, then rerun coverage.",
    "curation": "Record the missing license, terms, source note, checksum, and candidate provenance before publication.",
    "safety": "Complete the required human/safety review or remove the unsafe retained output before publication.",
}
SECRET_NAME_PATTERNS = (
    "OPENALEX_API_KEY",
    "GITHUB_TOKEN",
    "EVIDENCE_WIKI_HANDOFF_SECRET",
)
HIGH_RISK_SECRET_PATTERNS = {
    "bearer_token": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    "api_key_query_param": re.compile(r"\bapi[_-]?key=[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    "token_assignment": re.compile(r"\b(?:token|secret|password|credential)\s*[:=]\s*[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
}
SCAN_ROOTS = ("research.yml", "log.md", "runs", "sources", "wiki", "raw")
_SIBLING_CACHE: dict[str, ModuleType] = {}
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _script_errors import handle_system_exit, json_mode_requested
from _workspace_health import evaluate_workspace_health
from _workspace_module_loader import load_workspace_module


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate local publication readiness for a research workspace.")
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    parser.add_argument("--format", choices=("json",), default="json", help="Output format. Defaults to json.")
    parser.add_argument("--output", default=None, help="Write the JSON report to this path instead of stdout.")
    parser.add_argument(
        "--citation-verification",
        default=None,
        help="Optional citation verification JSON path to include in the gate.",
    )
    subparsers = parser.add_subparsers(dest="command")
    bundle = subparsers.add_parser("bundle", help="Write deterministic evaluation inputs under runs/<run-id>/evaluation/.")
    bundle.add_argument("--run-id", required=True, help="Run id used for runs/<run-id>/evaluation/.")
    return parser.parse_args(argv)


def load_sibling_module(stem: str) -> ModuleType:
    if stem not in _SIBLING_CACHE:
        _SIBLING_CACHE[stem] = load_workspace_module(_SCRIPT_DIR, stem)
    return _SIBLING_CACHE[stem]


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def workspace_relative(project_root: Path, path: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def validate_run_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise SystemExit(f"Invalid run id: {value}")
    return value


def configured_path(config: dict[str, Any], section: str, key: str, default: str) -> str:
    section_value = config.get(section)
    raw = section_value.get(key) if isinstance(section_value, dict) else default
    if not isinstance(raw, str) or not raw.strip():
        return default
    normalized = raw.strip().replace("\\", "/")
    parsed = PurePosixPath(normalized)
    if parsed.is_absolute() or ".." in parsed.parts or "://" in normalized:
        return default
    return parsed.as_posix()


def load_config(project_root: Path) -> dict[str, Any]:
    path = project_root / "research.yml"
    if not path.is_file():
        raise SystemExit(f"Missing config: {path}")
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        raise SystemExit(f"Invalid config: {path}")
    return document


def empty_reasons() -> dict[str, list[str]]:
    return {category: [] for category in REASON_CATEGORIES}


def append_reason(reasons: dict[str, list[str]], category: str, message: str) -> None:
    if message not in reasons[category]:
        reasons[category].append(message)


def structured_reason_contract(category: str, message: str) -> dict[str, Any]:
    policy_match = re.search(r"\bpolicy\s+([A-Za-z0-9_./:-]+)", message)
    source_ids = re.findall(r"\b(?:paper|web|repo|dataset|source):[A-Za-z0-9._:/-]+", message)
    artifacts = list(REASON_ARTIFACTS.get(category, ["publication-readiness.json"]))
    artifacts.extend(source_id for source_id in source_ids if source_id not in artifacts)
    return {
        "artifacts": artifacts,
        "policy": policy_match.group(1) if policy_match else REASON_POLICY.get(category, "publication_readiness"),
        "remediation": REASON_REMEDIATION.get(category, "Repair the affected artifact and rerun readiness."),
    }


def normalize_structured_reason(
    item: dict[str, Any],
    *,
    fallback_category: str,
    fallback_severity: str,
) -> dict[str, Any]:
    normalized = dict(item)
    category = normalized.get("category")
    if not isinstance(category, str) or category not in REASON_CATEGORIES:
        category = fallback_category
    message = normalized.get("message")
    if not isinstance(message, str) or not message:
        message = str(normalized.get("code") or fallback_severity)
    contract = structured_reason_contract(category, message)
    normalized.setdefault("code", category)
    normalized["category"] = category
    normalized.setdefault("severity", fallback_severity)
    normalized["message"] = message
    artifacts = normalized.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        normalized["artifacts"] = contract["artifacts"]
    normalized.setdefault("policy", contract["policy"])
    remediation = normalized.get("remediation")
    if not isinstance(remediation, str) or not remediation.strip():
        normalized["remediation"] = contract["remediation"]
    return normalized


def structured_verdict_reasons(verdict: str, reasons: dict[str, list[str]], status: dict[str, Any]) -> list[dict[str, Any]]:
    structured: list[dict[str, Any]] = []
    readiness = status.get("readiness") if isinstance(status.get("readiness"), dict) else {}
    for item in readiness.get("verdict_reasons", []) if isinstance(readiness.get("verdict_reasons"), list) else []:
        if isinstance(item, dict):
            structured.append(
                normalize_structured_reason(
                    item,
                    fallback_category="source_quality",
                    fallback_severity=verdict,
                )
            )
    for category, messages in reasons.items():
        for message in messages:
            structured.append(
                normalize_structured_reason(
                    {
                        "code": category,
                        "category": category,
                        "severity": verdict,
                        "message": message,
                    },
                    fallback_category=category,
                    fallback_severity=verdict,
                )
            )
    if not structured:
        structured.append(
            {
                "code": verdict,
                "category": "source_quality",
                "severity": verdict,
                "message": verdict,
                "artifacts": ["publication-readiness.json"],
                "policy": "publication_readiness",
                "remediation": "No remediation required." if verdict == VERDICT_SHIP else "Rerun readiness after repairing blockers.",
            }
        )
    deduplicated: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in structured:
        marker = (str(item.get("code")), str(item.get("category")), str(item.get("message")))
        if marker not in seen:
            seen.add(marker)
            deduplicated.append(item)
    return deduplicated


def lint_issue_category(issue: dict[str, Any]) -> str:
    value = issue.get("category")
    return value if isinstance(value, str) else "unknown"


def lint_issue_message(issue: dict[str, Any]) -> str:
    value = issue.get("message")
    return value if isinstance(value, str) and value else json.dumps(issue, sort_keys=True)


def classify_lint_issues(lint_report: dict[str, Any], reasons: dict[str, list[str]]) -> tuple[bool, bool]:
    """Return (no_ship, attention) from lint issues."""
    no_ship = False
    attention = False
    issues = lint_report.get("issues") if isinstance(lint_report.get("issues"), list) else []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        severity = issue.get("severity")
        category = lint_issue_category(issue)
        if category in COVERAGE_LINT_CATEGORIES:
            append_reason(reasons, "coverage", lint_issue_message(issue))
            continue
        if category in CURATION_LINT_CATEGORIES:
            append_reason(reasons, "curation", lint_issue_message(issue))
            if severity == "HIGH":
                no_ship = True
            elif severity == "MEDIUM":
                attention = True
            continue
        if category in GROUNDING_LINT_CATEGORIES:
            append_reason(reasons, "grounding", lint_issue_message(issue))
            if severity == "HIGH":
                no_ship = True
            elif severity == "MEDIUM":
                attention = True
            continue
        if category in CONTRADICTION_LINT_CATEGORIES:
            append_reason(reasons, "contradiction", lint_issue_message(issue))
            if severity == "HIGH":
                no_ship = True
            elif severity == "MEDIUM":
                attention = True
            continue
        if severity == "HIGH":
            no_ship = True
            append_reason(reasons, "source_quality", lint_issue_message(issue))
        elif severity == "MEDIUM":
            attention = True
            append_reason(reasons, "source_quality", lint_issue_message(issue))
    return no_ship, attention


def classify_workspace_status(status: dict[str, Any], reasons: dict[str, list[str]]) -> tuple[bool, bool, bool]:
    """Return (no_ship, blocked, attention) from aggregate workspace status."""
    readiness = status.get("readiness") if isinstance(status.get("readiness"), dict) else {}
    verdict = readiness.get("verdict")
    status_reasons = readiness.get("reasons") if isinstance(readiness.get("reasons"), list) else []
    if verdict == "blocked_on_sources":
        for reason in status_reasons:
            append_reason(reasons, "coverage", str(reason))
        return False, True, False
    if verdict == "in_progress":
        append_reason(reasons, "source_quality", "Workspace still has actionable research questions.")
        return False, False, True
    if verdict == "attention_required":
        for reason in status_reasons:
            text = str(reason)
            if "coverage" in text.lower():
                append_reason(reasons, "coverage", text)
            else:
                append_reason(reasons, "source_quality", text)
        return False, False, True
    return False, False, False


def classify_export(export: dict[str, Any], reasons: dict[str, list[str]]) -> tuple[bool, bool, bool]:
    no_ship = False
    blocked = False
    attention = False
    questions = export.get("questions") if isinstance(export.get("questions"), list) else []
    for question in questions:
        if not isinstance(question, dict):
            continue
        slug = question.get("slug", "<unknown>")
        human_review = question.get("human_review") if isinstance(question.get("human_review"), dict) else {}
        if human_review.get("pending") is True or question.get("status") == "human_review":
            no_ship = True
            append_reason(reasons, "safety", f"{slug} is pending required human review approval.")
        evidence_strength = question.get("evidence_strength")
        if evidence_strength in {"contested", "contradicted"}:
            no_ship = True
            append_reason(
                reasons,
                "contradiction",
                f"{slug} retains {evidence_strength} evidence without a publication-safe adjudication.",
            )
        coverage_required = question.get("coverage_required") is True
        coverage_status = question.get("coverage_status")
        if coverage_required and coverage_status != "pass":
            append_reason(reasons, "coverage", f"{slug} required coverage is {coverage_status}.")
            if coverage_status in {"blocked", "missing"}:
                blocked = True
            else:
                no_ship = True
        grounding_verification = question.get("grounding_verification")
        if coverage_required and isinstance(grounding_verification, dict) and not grounding_verification.get("all_verified"):
            no_ship = True
            grounding_results = (
                grounding_verification.get("grounding")
                if isinstance(grounding_verification.get("grounding"), list)
                else []
            )
            if not grounding_results:
                append_reason(reasons, "grounding", f"{slug} has no verified grounding entries.")
            for result in grounding_results:
                if not isinstance(result, dict) or result.get("result") == "verified":
                    continue
                append_reason(
                    reasons,
                    "grounding",
                    (
                        f"{slug} grounding claim {result.get('claim', '<unknown>')} "
                        f"from {result.get('source_id', '<unknown>')} returned {result.get('result', '<unknown>')}."
                    ),
                )
        facets = question.get("coverage_facets") if isinstance(question.get("coverage_facets"), list) else []
        for facet in facets:
            if not isinstance(facet, dict):
                continue
            for result in facet.get("policy_results", []) if isinstance(facet.get("policy_results"), list) else []:
                if not isinstance(result, dict):
                    continue
                verdict = result.get("verdict")
                policy = str(result.get("policy", "unknown"))
                if verdict == "pass":
                    continue
                message = f"{slug} facet {facet.get('facet_id')} policy {policy} returned {verdict}."
                policy_reasons = result.get("reasons") if isinstance(result.get("reasons"), list) else []
                if policy_reasons:
                    detail = "; ".join(str(reason) for reason in policy_reasons[:3])
                    message = f"{message} Reasons: {detail}"
                category = "currentness" if policy.startswith("current_") else "coverage"
                append_reason(reasons, category, message)
                if verdict == "fail":
                    if policy.startswith("current_"):
                        no_ship = True
                    else:
                        blocked = True
                elif verdict == "contradicted":
                    no_ship = True
                    append_reason(reasons, "contradiction", message)
                elif verdict == "manual_review":
                    attention = True
        citations = question.get("citations") if isinstance(question.get("citations"), list) else []
        for citation in citations:
            if not isinstance(citation, dict):
                continue
            evidence_areas = citation.get("supported_evidence_areas")
            if not isinstance(evidence_areas, list) or "current_legal_figure" not in evidence_areas:
                continue
            date_metadata = citation.get("date_metadata")
            if isinstance(date_metadata, dict) and date_metadata:
                continue
            no_ship = True
            append_reason(
                reasons,
                "currentness",
                f"{slug} cites current legal figure source {citation.get('source_id', '<unknown>')} without date_metadata.",
            )
    return no_ship, blocked, attention


def classify_candidates(status: dict[str, Any], reasons: dict[str, list[str]]) -> tuple[bool, bool]:
    no_ship = False
    attention = False
    candidates = status.get("candidates") if isinstance(status.get("candidates"), dict) else {}
    invalid = int(candidates.get("invalid_records", 0) or 0)
    if invalid:
        no_ship = True
        append_reason(reasons, "discovery_quality", f"Candidate store contains {invalid} invalid record(s).")
    selection = candidates.get("selection") if isinstance(candidates.get("selection"), dict) else {}
    selected_without_request = int(selection.get("selected_without_request", 0) or 0)
    if selected_without_request:
        attention = True
        append_reason(
            reasons,
            "discovery_quality",
            f"{selected_without_request} selected candidate(s) are not linked to source requests.",
        )
    rejections = candidates.get("rejections") if isinstance(candidates.get("rejections"), dict) else {}
    missing_reason = int(rejections.get("missing_reason", 0) or 0)
    if missing_reason:
        no_ship = True
        append_reason(reasons, "discovery_quality", f"{missing_reason} rejected candidate(s) lack a rejection reason.")
    return no_ship, attention


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc
    return document if isinstance(document, dict) else {}


def classify_citation_verification(report: dict[str, Any], reasons: dict[str, list[str]]) -> bool:
    if not report:
        return False
    no_ship = False
    results = report.get("results") if isinstance(report.get("results"), list) else []
    blocking_result_seen = False
    for result in results:
        if not isinstance(result, dict):
            continue
        value = result.get("result")
        if value in {"mismatch", "not_found", "insufficient_metadata", "skipped_no_live"}:
            no_ship = True
            blocking_result_seen = True
            append_reason(
                reasons,
                "citation_identity",
                f"Citation verification for {result.get('source_id', '<unknown>')} returned {value}.",
            )
    source_scope = report.get("source_scope") if isinstance(report.get("source_scope"), dict) else {}
    selected_source_ids = source_scope.get("source_ids") if isinstance(source_scope.get("source_ids"), list) else []
    if report.get("overall_result") == "no_ship" and (results or selected_source_ids):
        no_ship = True
        if not blocking_result_seen:
            append_reason(
                reasons,
                "citation_identity",
                "Citation verification artifact returned no_ship without a verified identity result.",
            )
    artifact_status = report.get("artifact_status") or report.get("status")
    if (
        report.get("mode") == "live"
        and report.get("overall_result") != "verified"
        and (artifact_status in {"failed", "error", "incomplete"} or not results)
    ):
        no_ship = True
        append_reason(
            reasons,
            "citation_identity",
            f"Live citation verification artifact is {artifact_status or 'incomplete'} and cannot support publication.",
        )
    return no_ship


def should_scan_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in {"", ".md", ".json", ".jsonl", ".yml", ".yaml", ".txt", ".html"}


def scan_text_for_secrets(label: str, text: str, reasons: dict[str, list[str]]) -> bool:
    found = False
    for name in SECRET_NAME_PATTERNS:
        if name in text:
            found = True
            append_reason(reasons, "safety", f"{label} contains configured secret name {name}.")
    for pattern_name, pattern in HIGH_RISK_SECRET_PATTERNS.items():
        if pattern.search(text):
            found = True
            append_reason(reasons, "safety", f"{label} matches high-risk secret pattern {pattern_name}.")
    return found


def scan_workspace_for_secrets(project_root: Path, reasons: dict[str, list[str]]) -> bool:
    found = False
    for relative in SCAN_ROOTS:
        root = project_root / relative
        if not root.exists():
            continue
        files = [root] if root.is_file() else sorted(path for path in root.rglob("*") if should_scan_file(path))
        for path in files:
            if not should_scan_file(path):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            found = scan_text_for_secrets(workspace_relative(project_root, path), text, reasons) or found
    return found


def source_request_summary(project_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    source_requests = load_sibling_module("source_requests")
    try:
        records = source_requests.load_requests(source_requests.requests_path(project_root, config))
    except SystemExit as exc:
        return {"error": str(exc), "total": 0, "by_status": {}}
    by_status: dict[str, int] = {}
    for record in records:
        status = record.get("status") if isinstance(record, dict) else None
        key = status if isinstance(status, str) and status else "unknown"
        by_status[key] = by_status.get(key, 0) + 1
    return {"total": len(records), "by_status": dict(sorted(by_status.items())), "requests": records}


def local_citation_verification(project_root: Path) -> dict[str, Any]:
    verify = load_sibling_module("verify_citations")
    args = argparse.Namespace(source_id=None, live=False, provider=None)
    return verify.build_report(project_root, args)


def build_readiness_document(
    project_root: Path,
    *,
    citation_verification_path: Path | None = None,
    embedded_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    workspace_health = evaluate_workspace_health(project_root)
    if not workspace_health["materially_valid"]:
        reasons = empty_reasons()
        invalid_findings = [
            item for item in workspace_health["findings"] if item["readiness_effect"] == "invalid"
        ]
        structured_invalid_findings = [
            normalize_structured_reason(
                item,
                fallback_category="source_quality",
                fallback_severity=VERDICT_NO_SHIP,
            )
            for item in invalid_findings
        ]
        for item in invalid_findings:
            append_reason(reasons, "source_quality", f"{item['code']}: {item['message']}")
        status = {
            "schema_version": SCHEMA_VERSION,
            "workspace_health": workspace_health,
            "readiness": {
                "verdict": "attention_required",
                "reasons": reasons["source_quality"],
                "verdict_reasons": structured_invalid_findings,
            },
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": timestamp_utc(),
            "project": {"name": None, "handoff": None},
            "network_io_executed": False,
            "verdict": VERDICT_NO_SHIP,
            "reasons": reasons,
            "verdict_reasons": structured_invalid_findings,
            "workspace_status": status,
            "workspace_health": workspace_health,
            "lint_summary": {"issue_counts": {"HIGH": len(invalid_findings)}, "issues": []},
            "coverage_summary": {},
            "candidate_summary": {},
            "citation_verification": None,
            "export_summary": {"counts": {}, "warnings": []},
        }
    config = load_config(project_root)
    status_module = load_sibling_module("workspace_status")
    lint_module = load_sibling_module("lint")
    export_module = load_sibling_module("export_answers")

    status = embedded_inputs.get("status") if embedded_inputs else None
    if not isinstance(status, dict):
        status = status_module.build_status_document(project_root)
    lint_report = embedded_inputs.get("lint") if embedded_inputs else None
    if not isinstance(lint_report, dict):
        lint_report = lint_module.run_checks(project_root, config)
    export = embedded_inputs.get("export") if embedded_inputs else None
    if not isinstance(export, dict):
        export = export_module.build_export(project_root, None)
    citation_report = embedded_inputs.get("citation_verification") if embedded_inputs else None
    if not isinstance(citation_report, dict):
        citation_report = load_json_file(citation_verification_path) if citation_verification_path else {}

    reasons = empty_reasons()
    no_ship = False
    blocked = False
    attention = False

    if workspace_health["publication_blocked"]:
        no_ship = True
        for item in workspace_health["findings"]:
            if item["readiness_effect"] == "publication_blocked":
                append_reason(reasons, "source_quality", f"{item['code']}: {item['message']}")

    status_no_ship, status_blocked, status_attention = classify_workspace_status(status, reasons)
    no_ship = no_ship or status_no_ship
    blocked = blocked or status_blocked
    attention = attention or status_attention

    lint_no_ship, lint_attention = classify_lint_issues(lint_report, reasons)
    no_ship = no_ship or lint_no_ship
    attention = attention or lint_attention

    export_no_ship, export_blocked, export_attention = classify_export(export, reasons)
    no_ship = no_ship or export_no_ship
    blocked = blocked or export_blocked
    attention = attention or export_attention

    candidate_no_ship, candidate_attention = classify_candidates(status, reasons)
    no_ship = no_ship or candidate_no_ship
    attention = attention or candidate_attention

    no_ship = classify_citation_verification(citation_report, reasons) or no_ship
    no_ship = scan_workspace_for_secrets(project_root, reasons) or no_ship

    if no_ship:
        verdict = VERDICT_NO_SHIP
    elif blocked:
        verdict = VERDICT_BLOCKED
    elif attention:
        verdict = VERDICT_ATTENTION
    else:
        verdict = VERDICT_SHIP

    project = config.get("project") if isinstance(config.get("project"), dict) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": timestamp_utc(),
        "project": {
            "name": project.get("name") if isinstance(project.get("name"), str) else None,
            "handoff": project.get("handoff") if isinstance(project.get("handoff"), dict) else None,
        },
        "network_io_executed": False,
        "verdict": verdict,
        "reasons": reasons,
        "verdict_reasons": structured_verdict_reasons(verdict, reasons, status),
        "workspace_status": status,
        "workspace_health": workspace_health,
        "lint_summary": {
            "issue_counts": lint_report.get("stats", {}).get("issue_counts", {})
            if isinstance(lint_report.get("stats"), dict)
            else {},
            "issues": lint_report.get("issues", []),
        },
        "coverage_summary": status.get("coverage", {}) if isinstance(status.get("coverage"), dict) else {},
        "candidate_summary": status.get("candidates", {}) if isinstance(status.get("candidates"), dict) else {},
        "citation_verification": citation_report or None,
        "export_summary": {
            "counts": export.get("counts", {}),
            "warnings": export.get("warnings", []),
        },
    }


def write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def build_bundle(project_root: Path, run_id: str) -> dict[str, Any]:
    run_id = validate_run_id(run_id)
    config = load_config(project_root)
    status_module = load_sibling_module("workspace_status")
    lint_module = load_sibling_module("lint")
    export_module = load_sibling_module("export_answers")

    run_state = project_root / "runs" / run_id / "run-state.json"
    status = status_module.build_status_document(project_root, run_id=run_id if run_state.is_file() else None)
    lint_report = lint_module.run_checks(project_root, config)
    export = export_module.build_export(project_root, None)
    citation = local_citation_verification(project_root)
    source_requests = source_request_summary(project_root, config)
    candidate_summary = status.get("candidates", {}) if isinstance(status.get("candidates"), dict) else {}
    publication = build_readiness_document(
        project_root,
        embedded_inputs={
            "status": status,
            "lint": lint_report,
            "export": export,
            "citation_verification": citation,
        },
    )

    bundle_dir = project_root / "runs" / run_id / "evaluation"
    artifacts = {
        "status.json": status,
        "publication-readiness.json": publication,
        "export.json": export,
        "lint.json": lint_report,
        "citation-verification.json": citation,
        "candidate-summary.json": candidate_summary,
        "source-request-summary.json": source_requests,
    }
    for name, document in artifacts.items():
        write_json(bundle_dir / name, document)
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "bundle",
        "run_id": run_id,
        "bundle_dir": workspace_relative(project_root, bundle_dir),
        "artifacts": {name: workspace_relative(project_root, bundle_dir / name) for name in artifacts},
        "publication_readiness": publication,
    }


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    json_mode = json_mode_requested(argv, default_json=True)
    project_root = Path(args.project_root).expanduser().resolve()
    try:
        if args.command == "bundle":
            document = build_bundle(project_root, args.run_id)
            exit_code = EXIT_READY if document["publication_readiness"]["verdict"] == VERDICT_SHIP else EXIT_NOT_READY
        else:
            citation_path = Path(args.citation_verification).expanduser().resolve() if args.citation_verification else None
            document = build_readiness_document(project_root, citation_verification_path=citation_path)
            workspace_health = document.get("workspace_health")
            if isinstance(workspace_health, dict) and not workspace_health.get("materially_valid", False):
                exit_code = EXIT_UNREADABLE
            else:
                exit_code = EXIT_READY if document["verdict"] == VERDICT_SHIP else EXIT_NOT_READY
    except SystemExit as exc:
        return handle_system_exit(exc, json_mode=json_mode, default_exit_code=EXIT_UNREADABLE)

    output = render(document)
    if args.output:
        Path(args.output).expanduser().resolve().write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
