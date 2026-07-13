#!/usr/bin/env python3
"""Score an agent-quality evaluation workspace from exported answer artifacts."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to score evaluation workspaces") from exc


SCHEMA_VERSION = "1.0"
ANSWER_WEIGHT = 0.7
CITATION_WEIGHT = 0.3
EXIT_OK = 0
EXIT_USAGE = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Score an export_answers.py JSON dump against an expected-answer key.",
    )
    parser.add_argument(
        "--export",
        required=True,
        help="Path to a JSON file produced by scripts/export_answers.py.",
    )
    parser.add_argument(
        "--expected",
        required=True,
        help="Path to the expected-answer key, in YAML or JSON.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="text",
        help="Report format. Defaults to text.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write the score report to this path instead of stdout.",
    )
    return parser.parse_args(argv)


def load_json_document(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Missing export file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in export file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid export file {path}: expected a JSON object")
    return data


def load_expected_key(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SystemExit(f"Missing expected-answer key: {path}") from exc
    try:
        data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise SystemExit(f"Invalid expected-answer key {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid expected-answer key {path}: expected a mapping")
    return data


def require_question_list(document: dict[str, Any], label: str) -> list[dict[str, Any]]:
    raw_questions = document.get("questions")
    if not isinstance(raw_questions, list):
        raise SystemExit(f"{label} must contain a questions list")
    questions: list[dict[str, Any]] = []
    for index, item in enumerate(raw_questions, start=1):
        if not isinstance(item, dict):
            raise SystemExit(f"{label} questions[{index}] must be a mapping")
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            raise SystemExit(f"{label} questions[{index}] must carry a non-empty slug")
        questions.append(item)
    return questions


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def text_field(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    return value.strip() if isinstance(value, str) else ""


def normalize_text(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())


def phrase_match_score(text: str, phrases: list[str]) -> tuple[float, list[str]]:
    if not phrases:
        return 1.0, []
    normalized = normalize_text(text)
    missing = [phrase for phrase in phrases if normalize_text(phrase) not in normalized]
    score = (len(phrases) - len(missing)) / len(phrases)
    return score, [f"missing required phrase: {phrase}" for phrase in missing]


def source_id_f1(expected_ids: list[str], actual_ids: list[str]) -> tuple[float, list[str]]:
    expected = set(expected_ids)
    actual = set(actual_ids)
    if not expected and not actual:
        return 1.0, []
    if not expected:
        return 0.0, [f"unexpected source_ids: {', '.join(sorted(actual))}"]
    true_positive = len(expected & actual)
    score = (2 * true_positive) / (len(expected) + len(actual)) if actual else 0.0
    findings: list[str] = []
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing:
        findings.append(f"missing expected source_ids: {', '.join(missing)}")
    if unexpected:
        findings.append(f"unexpected source_ids: {', '.join(unexpected)}")
    return score, findings


def round_score(value: float) -> float:
    return round(value + 0.000000001, 2)


def component(name: str, score: float, weight: float, findings: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "score": round_score(score),
        "weight": round_score(weight),
        "weighted_points": round_score(score * weight * 100),
        "findings": findings or [],
    }


def score_answered_question(expected: dict[str, Any], actual: dict[str, Any]) -> tuple[float, list[dict[str, Any]], list[str]]:
    findings: list[str] = []
    components: list[dict[str, Any]] = []
    status = text_field(actual, "status")
    if status != "answered":
        findings.append(f"expected status answered but export status is {status or 'missing'}")

    summary = text_field(actual, "answer_summary")
    answer_score, answer_findings = phrase_match_score(summary, string_list(expected.get("required_answer_phrases")))
    if status != "answered":
        answer_score = 0.0
    expected_page = text_field(expected, "answer_page")
    actual_page = text_field(actual, "answer_page")
    if expected_page and actual_page != expected_page:
        answer_findings.append(
            f"answer_page mismatch: expected {expected_page}, got {actual_page or 'missing'}"
        )
    components.append(component("answer_correctness", answer_score, ANSWER_WEIGHT, answer_findings))

    citation_score, citation_findings = source_id_f1(
        string_list(expected.get("expected_source_ids")),
        string_list(actual.get("source_ids")),
    )
    components.append(component("citation_precision", citation_score, CITATION_WEIGHT, citation_findings))
    findings.extend(citation_findings)
    findings.extend(answer_findings)
    points = (answer_score * ANSWER_WEIGHT + citation_score * CITATION_WEIGHT) * 100
    return points, components, findings


def score_blocked_question(expected: dict[str, Any], actual: dict[str, Any]) -> tuple[float, list[dict[str, Any]], list[str]]:
    findings: list[str] = []
    status = text_field(actual, "status")
    status_score = 1.0 if status == "blocked" else 0.0
    if status_score == 0.0:
        findings.append(f"expected status blocked but export status is {status or 'missing'}")

    reason_score, reason_findings = phrase_match_score(
        text_field(actual, "blocked_reason"),
        string_list(expected.get("required_blocked_phrases")),
    )
    no_answer_score = 1.0 if not actual.get("answer_page") and not actual.get("answer_summary") else 0.0
    no_answer_findings = []
    if no_answer_score == 0.0:
        no_answer_findings.append("blocked question should not carry an answer page or summary")

    components = [
        component("blocked_status", status_score, 1 / 3),
        component("blocked_reason", reason_score, 1 / 3, reason_findings),
        component("no_answer_artifact", no_answer_score, 1 / 3, no_answer_findings),
    ]
    findings.extend(reason_findings)
    findings.extend(no_answer_findings)
    points = ((status_score + reason_score + no_answer_score) / 3) * 100
    return points, components, findings


def score_missing_question(expected: dict[str, Any]) -> dict[str, Any]:
    slug = text_field(expected, "slug")
    return {
        "slug": slug,
        "expected_status": text_field(expected, "expected_status") or "answered",
        "actual_status": None,
        "points": 0.0,
        "max_points": 100.0,
        "percent": 0.0,
        "components": [],
        "findings": [f"expected question {slug} missing from export"],
    }


def score_question(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    expected_status = text_field(expected, "expected_status") or "answered"
    if expected_status == "blocked":
        points, components, findings = score_blocked_question(expected, actual)
    else:
        points, components, findings = score_answered_question(expected, actual)
    return {
        "slug": text_field(expected, "slug"),
        "expected_status": expected_status,
        "actual_status": text_field(actual, "status") or None,
        "points": round_score(points),
        "max_points": 100.0,
        "percent": round_score(points),
        "components": components,
        "findings": findings,
    }


def build_report(export_document: dict[str, Any], expected_key: dict[str, Any]) -> dict[str, Any]:
    expected_questions = require_question_list(expected_key, "expected-answer key")
    exported_questions = require_question_list(export_document, "answer export")
    exported_by_slug = {text_field(question, "slug"): question for question in exported_questions}
    expected_slugs = [text_field(question, "slug") for question in expected_questions]
    expected_slug_set = set(expected_slugs)

    question_reports: list[dict[str, Any]] = []
    missing_count = 0
    for expected in expected_questions:
        slug = text_field(expected, "slug")
        actual = exported_by_slug.get(slug)
        if actual is None:
            missing_count += 1
            question_reports.append(score_missing_question(expected))
        else:
            question_reports.append(score_question(expected, actual))

    unexpected_slugs = sorted(set(exported_by_slug) - expected_slug_set)
    warnings = [f"export contains unexpected question: {slug}" for slug in unexpected_slugs]
    points = round_score(sum(question["points"] for question in question_reports))
    max_points = round_score(100.0 * len(expected_questions))
    percent = round_score((points / max_points) * 100) if max_points else 0.0

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "score": {
            "points": points,
            "max_points": max_points,
            "percent": percent,
        },
        "counts": {
            "expected_questions": len(expected_questions),
            "exported_questions": len(exported_questions),
            "scored_questions": len(question_reports),
            "missing_questions": missing_count,
            "unexpected_questions": len(unexpected_slugs),
        },
        "warnings": warnings,
        "questions": question_reports,
    }


def render_text(report: dict[str, Any]) -> str:
    lines = [
        "Agent-quality evaluation score",
        f"Score: {report['score']['points']:.2f}/{report['score']['max_points']:.2f} "
        f"({report['score']['percent']:.2f}%)",
        (
            "Questions: "
            f"{report['counts']['scored_questions']} scored, "
            f"{report['counts']['missing_questions']} missing, "
            f"{report['counts']['unexpected_questions']} unexpected"
        ),
    ]
    if report["warnings"]:
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in report["warnings"])
    lines.append("Per-question:")
    for question in report["questions"]:
        lines.append(
            f"- {question['slug']}: {question['points']:.2f}/{question['max_points']:.2f} "
            f"({question['percent']:.2f}%)"
        )
        for finding in question["findings"]:
            lines.append(f"  - {finding}")
    return "\n".join(lines) + "\n"


def render_report(report: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(report, indent=2, sort_keys=False) + "\n"
    return render_text(report)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        export_document = load_json_document(Path(args.export).expanduser())
        expected_key = load_expected_key(Path(args.expected).expanduser())
        report = build_report(export_document, expected_key)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    rendered = render_report(report, args.format)
    if args.output:
        Path(args.output).expanduser().write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
