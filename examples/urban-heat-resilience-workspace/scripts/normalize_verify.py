#!/usr/bin/env python3
"""Verify normalized records against the public contract in docs/normalized-source-format.md.

This is the entry point that makes the record format a contract a host can target
instead of an internal format it has to reverse-engineer: a record written by an
external normalizer is checked exactly as one this package wrote, by the same
validator, and every breach is reported with a stable machine-readable code.

The verifier is offline and deterministic. It reads records, the manifest, and
research.yml, and performs no network I/O and no writes. Contract breaches are report
content rather than fatal errors, so one malformed record never hides the rest; only a
workspace the verifier cannot read at all is fatal.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to read research.yml") from exc


SCHEMA_VERSION = "1.0"
DOCUMENT_TYPE = "normalize_verify_report"
EXIT_OK = 0
EXIT_NOT_VERIFIED = 1
EXIT_INVALID = 2
RESULT_VERIFIED = "verified"
RESULT_INVALID = "invalid"
RESULT_NOT_VERIFIED = "not_verified"
ORIGIN_NATIVE = "native"
ORIGIN_EXTERNAL = "external"

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import _normalized_contract as contract
from _script_errors import emit_error, handle_system_exit, json_mode_requested
from _workspace_module_loader import load_workspace_module

_SIBLING_CACHE: dict[str, ModuleType] = {}


class NormalizeVerifyError(Exception):
    """Fatal verifier error with a stable machine code."""

    def __init__(self, error_code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details or {}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify normalized source records against the published record contract.",
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--source-id",
        action="append",
        default=[],
        metavar="ID",
        help="Verify the record for one manifest source ID. Repeat to verify a selected subset.",
    )
    selection.add_argument(
        "--all",
        action="store_true",
        help="Verify every record under the normalized directory. This is the default.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Report format. Defaults to json.",
    )
    parser.add_argument("--output", default=None, help="Write the report to this path instead of stdout.")
    return parser.parse_args(argv)


def load_sibling_module(stem: str) -> ModuleType:
    if stem not in _SIBLING_CACHE:
        _SIBLING_CACHE[stem] = load_workspace_module(_SCRIPT_DIR, stem)
    return _SIBLING_CACHE[stem]


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config(project_root: Path) -> dict[str, Any]:
    path = project_root / "research.yml"
    if not path.is_file():
        raise SystemExit(f"Missing config: {path}")
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        raise SystemExit(f"Invalid config: {path}")
    return document


def workspace_relative(project_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.as_posix()


def unique_values(values: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def selected_paths(
    args: argparse.Namespace,
    normalized_root: Path,
    manifest_by_id: dict[str, dict[str, Any]],
) -> list[Path]:
    """Records to verify: the requested ids, or every record that exists."""
    requested = unique_values([value.strip() for value in (args.source_id or [])])
    if not requested:
        if not normalized_root.is_dir():
            return []
        return sorted(normalized_root.rglob("*.md"), key=lambda value: value.as_posix())

    paths: list[Path] = []
    for source_id in requested:
        path = contract.expected_record_path(normalized_root, source_id)
        if not path.is_file() and source_id not in manifest_by_id:
            # Neither a record nor a manifest entry: the id itself is wrong, which is a
            # usage error rather than a finding about workspace data.
            raise NormalizeVerifyError(
                "SOURCE_UNKNOWN",
                f"Unknown source id: {source_id}",
                details={"source_id": source_id},
            )
        paths.append(path)
    return paths


def record_origin(frontmatter: dict[str, Any] | None) -> str:
    if frontmatter is None:
        return ORIGIN_EXTERNAL
    return ORIGIN_NATIVE if contract.is_native_record(frontmatter) else ORIGIN_EXTERNAL


def verify_record(
    project_root: Path,
    path: Path,
    *,
    manifest_by_id: dict[str, dict[str, Any]],
    normalized_root: Path,
) -> dict[str, Any]:
    frontmatter: dict[str, Any] | None = None
    if path.is_file():
        try:
            frontmatter, _, _ = contract.split_record(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            frontmatter = None

    violations = contract.validate_record(
        path,
        manifest_by_id=manifest_by_id,
        normalized_root=normalized_root,
    )
    source_id = frontmatter.get("source_id") if isinstance(frontmatter, dict) else None
    normalizer = frontmatter.get("normalizer") if isinstance(frontmatter, dict) else None
    return {
        "source_id": source_id if isinstance(source_id, str) and source_id else None,
        "path": workspace_relative(project_root, path),
        "exists": path.is_file(),
        "origin": record_origin(frontmatter),
        "normalizer": normalizer if isinstance(normalizer, dict) else None,
        "normalized_format": contract.effective_format_version(frontmatter) if frontmatter is not None else None,
        "rendered_coverage": coverage_summary(frontmatter),
        "structured_view": structured_view_summary(frontmatter, normalized_root, violations),
        "result": RESULT_VERIFIED if not violations else RESULT_INVALID,
        "violations": [violation.to_dict() for violation in violations],
    }


def coverage_summary(frontmatter: dict[str, Any] | None) -> dict[str, Any] | None:
    """How much of the source's structured content this record's body renders.

    `null` when the record declares nothing, which is every record that is not a
    rendering of structured evidence. `capped_sections` names the facets that lost
    content: those are the parts a claim can cite but never quote, which is the whole
    reason a host asks for this.
    """
    if not isinstance(frontmatter, dict):
        return None
    block = frontmatter.get("rendered_coverage")
    if not isinstance(block, dict):
        return None

    sections = block.get("sections")
    capped: list[str] = []
    if isinstance(sections, list):
        for section in sections:
            if not isinstance(section, dict):
                continue
            total = section.get("total")
            rendered = section.get("rendered")
            heading = section.get("heading")
            if isinstance(total, int) and isinstance(rendered, int) and rendered < total:
                capped.append(heading if isinstance(heading, str) else "<unnamed>")
    return {
        "ratio": block.get("ratio"),
        "total_values": block.get("total_values"),
        "rendered_values": block.get("rendered_values"),
        "capped_sections": capped,
    }


def structured_view_summary(
    frontmatter: dict[str, Any] | None,
    normalized_root: Path,
    violations: list[Any],
) -> dict[str, Any] | None:
    """Whether this record binds a structured-view sidecar, and whether it resolves.

    `null` when the record declares nothing, which is every record that is not a
    rendering of structured evidence: a paper or a web page has no structured view to
    offer, and saying so is different from saying its sidecar is broken.

    Unlike `coverage_summary`, which reports only what the frontmatter claims, this
    reads the workspace. That departure is the point: a binding's whole value is whether
    the file it names is really there and really hashes to what the record says, which
    no amount of frontmatter can answer on its own.

    `verified` is read out of the violations the caller already computed rather than by
    re-running the contract check. Same answer from the same authority — the check ran
    once and its verdict is right here — without reading and SHA256-ing every sidecar in
    the workspace a second time on every run, and with no way for a later edit to move
    the summary and the violation list apart. It additionally requires the sidecar to
    have been found, so a record too broken to resolve one (a missing `source_id`, say)
    reports an unverified binding rather than a silent pass.
    """
    if not isinstance(frontmatter, dict):
        return None
    block = frontmatter.get("structured_view")
    if not isinstance(block, dict):
        return None

    declared = block.get("path")
    source_id = frontmatter.get("source_id")
    sidecar: Path | None = None
    if isinstance(source_id, str) and source_id.strip():
        sidecar = contract.expected_structured_path(normalized_root, source_id.strip())
    size: int | None = None
    try:
        if sidecar is not None and sidecar.is_file():
            size = sidecar.stat().st_size
    except OSError:
        size = None
    bound = not any(
        getattr(violation, "code", None) == contract.STRUCTURED_VIEW_INVALID for violation in violations
    )
    return {
        "declared": True,
        "path": declared if isinstance(declared, str) and declared.strip() else None,
        "verified": size is not None and bound,
        "bytes": size,
    }


def build_report(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    normalize = load_sibling_module("normalize_sources")
    config = load_config(project_root)
    manifest_rel, normalized_rel = normalize.source_paths(config)
    manifest_path = project_root / manifest_rel
    normalized_root = project_root / normalized_rel

    manifest_records = normalize.load_manifest(manifest_path)
    manifest_by_id: dict[str, dict[str, Any]] = {}
    for record in manifest_records:
        source_id = record.get("id")
        if isinstance(source_id, str) and source_id and source_id not in manifest_by_id:
            manifest_by_id[source_id] = record

    paths = selected_paths(args, normalized_root, manifest_by_id)
    records = [
        verify_record(
            project_root,
            path,
            manifest_by_id=manifest_by_id,
            normalized_root=normalized_root,
        )
        for path in paths
    ]

    invalid = [record for record in records if record["result"] != RESULT_VERIFIED]
    warnings: list[str] = []
    if not records:
        warnings.append(f"No normalized records found under {normalized_rel}.")

    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": DOCUMENT_TYPE,
        "generated_at": timestamp_utc(),
        "network_io_executed": False,
        "manifest_path": manifest_rel,
        "normalized_dir": normalized_rel,
        "contract": {
            "document": contract.CONTRACT_DOCUMENT,
            "written_version": contract.NORMALIZED_FORMAT_VERSION,
            "accepted_versions": sorted(contract.ACCEPTED_NORMALIZED_FORMATS),
        },
        "records": records,
        "counts": {
            "records": len(records),
            "verified": len(records) - len(invalid),
            "invalid": len(invalid),
            "native": len([record for record in records if record["origin"] == ORIGIN_NATIVE]),
            "external": len([record for record in records if record["origin"] == ORIGIN_EXTERNAL]),
            "with_structured_view": len([record for record in records if record["structured_view"]]),
            "violations": sum(len(record["violations"]) for record in records),
        },
        "warnings": warnings,
        # An empty workspace verifies: there is nothing that breaks the contract. A
        # caller that also requires evidence to exist reads counts.records.
        "overall_result": RESULT_VERIFIED if not invalid else RESULT_NOT_VERIFIED,
    }


def render_text(report: dict[str, Any]) -> str:
    counts = report["counts"]
    lines = [
        "Normalized Record Contract Verification",
        "=======================================",
        "",
        f"Contract: {report['contract']['document']} "
        f"(accepted versions: {', '.join(str(v) for v in report['contract']['accepted_versions'])})",
        f"Records: {counts['records']} "
        f"(native={counts['native']} external={counts['external']}) "
        f"verified={counts['verified']} invalid={counts['invalid']}",
        "",
    ]
    for record in report.get("records", []):
        label = record.get("source_id") or record.get("path")
        lines.append(f"- {record['result']}: {label} [{record['origin']}]")
        coverage = record.get("rendered_coverage")
        if coverage:
            capped = coverage.get("capped_sections") or []
            detail = f", capped: {', '.join(capped)}" if capped else ""
            lines.append(
                f"    rendered coverage: {coverage.get('ratio')} "
                f"({coverage.get('rendered_values')}/{coverage.get('total_values')} values{detail})"
            )
        structured = record.get("structured_view")
        if structured:
            size = structured.get("bytes")
            detail = f", {size} bytes" if isinstance(size, int) else ""
            lines.append(
                f"    structured view: {'verified' if structured.get('verified') else 'unverified'} "
                f"({structured.get('path') or 'no path declared'}{detail})"
            )
        for violation in record.get("violations", []):
            field = violation.get("field")
            suffix = f" (field: {field})" if field else ""
            lines.append(f"    {violation['code']}{suffix}: {violation['message']}")
            lines.append(f"      remediation: {violation['remediation']}")
    for warning in report.get("warnings", []):
        lines.append(f"- warning: {warning}")
    lines.append("")
    lines.append(f"Overall: {report['overall_result']}")
    return "\n".join(lines).rstrip() + "\n"


def render_report(report: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(report, indent=2, sort_keys=False) + "\n"
    return render_text(report)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    json_mode = json_mode_requested(argv, default_json=args.format == "json")
    project_root = Path(args.project_root).expanduser().resolve()
    try:
        report = build_report(project_root, args)
    except NormalizeVerifyError as exc:
        emit_error(str(exc), json_mode=json_mode, error_code=exc.error_code, details=exc.details)
        return EXIT_INVALID
    except SystemExit as exc:
        return handle_system_exit(exc, json_mode=json_mode, default_exit_code=EXIT_INVALID)

    rendered = render_report(report, args.format)
    if args.output:
        Path(args.output).expanduser().resolve().write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return EXIT_OK if report["overall_result"] == RESULT_VERIFIED else EXIT_NOT_VERIFIED


if __name__ == "__main__":
    raise SystemExit(main())
