"""Exercise retained HTML through a selected CLI and its copied workspace scripts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from evidence_wiki.onboarding_contract import _matches
from evidence_wiki.source_contracts import schema_document

CASE_NAMES = (
    "gateway-body", "signin-body", "timeout-body", "password-form", "signin-javascript",
    "maintenance", "short-http-definition", "numeric-data", "authentication-guide", "article-login-form",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def snapshot(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def changed(before, after):
    return {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}


class Commands:
    """Use one interpreter/CLI pair, with bounded processes and no shell dispatch."""

    def __init__(self, cli, root, *, python=None):
        self.cli = str(cli)
        self.root = Path(root)
        self.python = str(python or sys.executable)
        self.environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    def run(self, argv, *, expected=0, json_output=True):
        result = subprocess.run(list(map(str, argv)), cwd=self.root.parent, env=self.environment,  # noqa: S603 -- fixed CLI/script operations
                                capture_output=True, text=True, encoding="utf-8", timeout=90, check=False)
        require(result.returncode == expected, f"{argv}: {result.returncode}: {result.stdout}\n{result.stderr}")
        return json.loads(result.stdout or result.stderr) if json_output else result.stdout

    def package(self, *arguments, **options):
        return self.run([self.cli, *arguments], **options)

    def script(self, name, *arguments, **options):
        return self.run([self.python, "-B", self.root / "scripts" / (name + ".py"),
                         "--project-root", self.root, *arguments], **options)


def read_record(path):
    _, header, body = path.read_text(encoding="utf-8").split("---", 2)
    return yaml.safe_load(header), body


def write_record(path, metadata, body):
    path.write_text("---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---" + body, encoding="utf-8", newline="\n")


def coverage_document(name, source_id):
    return {"schema_version": "1.0", "question_slug": name, "created_at": "2026-09-29T10:00:00Z",
            "updated_at": "2026-09-29T10:00:00Z", "coverage_profile": "official-observation", "coverage_verdict": "pending",
            "required_facets": [{"facet_id": "retained-observation", "description": "Retained official source.", "required": True,
                "evidence_path": "official_guidance", "source_policy": "official_primary", "freshness_policy": "no_staleness_check",
                "identity_policy": "none", "min_sources": 1, "accepted_source_ids": [source_id],
                "blocking_request_ids": [], "facet_verdict": "pending"}], "optional_facets": []}


def route_document(name, source_id):
    return {"schema_version": "evidence-source-routes/v1", "request_id": name, "requirements": [{
        "id": name, "question_ids": [name], "kind": "web", "source_request_id": None,
        "query_or_identifier": "https://example.org/" + name, "scope": {}, "output_format": "html",
        "content_kinds": ["primary"], "needs_complete": True, "source_ids": [source_id]}],
        "budget": {"max_requests": 0, "max_bytes": 10000, "max_cost_usd": "0"},
        "preferred_tools": [], "registered_requests": []}


def initialize(commands, pages):
    root = commands.root
    commands.package("init", "--target", root, "--project-name", "retained-html",
                     "--project-description", "Retain source qualifications across public operations.", json_output=False)
    for page in pages:
        path = root / "raw/web" / page["file_name"]
        require(path.parent == root / "raw/web", "HTML corpus filenames must stay in the selected raw directory")
        path.write_text(page["html"], encoding="utf-8", newline="\n")
        path.with_name(path.name + ".provenance.yml").write_text(yaml.safe_dump({
            "origin_url": "https://example.org/" + page["name"], "retrieved_at": "2026-09-29T10:00:00Z",
            "retrieved_by": "local_fixture", "source_type": "official_web", "license": "CC0-1.0",
            "checksum": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        }), encoding="utf-8", newline="\n")
    commands.script("source_inventory", "--format", "json", "--report")
    records = {Path(row["raw_paths"][0]).name: row for row in
               map(json.loads, (root / "sources/manifest.jsonl").read_text(encoding="utf-8").splitlines())}
    require(len(records) == len(pages), "Inventory must account for every retained HTML input")
    batch = root.parent / "questions.json"
    batch.write_text(json.dumps({"schema_version": "1.0", "questions": [
        {"question": page["name"], "priority": "high", "origin": "caller"} for page in pages]}), encoding="utf-8", newline="\n")
    commands.package("questions", "add", "--target", root, "--from-file", batch, "--format", "json")
    (root / "sources/jurisdictions.yml").write_text(yaml.safe_dump({"jurisdiction_profiles": [
        {"jurisdiction_id": "fixture-authority", "name": "Fixture authority", "official_domains": ["example.org"], "blocked_domains": []}]}), encoding="utf-8", newline="\n")
    (root / "sources/coverage").mkdir(parents=True, exist_ok=True)
    for page in pages:
        name, source_id = page["name"], records[page["file_name"]]["id"]
        (root / "sources/coverage" / (name + ".yml")).write_text(yaml.safe_dump(coverage_document(name, source_id)), encoding="utf-8", newline="\n")
    return records


def normalized_path(root, source_id):
    for path in (root / "sources/normalized").glob("*.md"):
        metadata, _ = read_record(path)
        if metadata.get("source_id") == source_id:
            return path
    raise ValueError("No normalized output for " + source_id)


def observe(commands, page, source_id, *, stale=False):
    root, name = commands.root, page["name"]
    route_path = root.parent / (name + "-routes.json")
    route_path.write_text(json.dumps(route_document(name, source_id)), encoding="utf-8", newline="\n")
    before = snapshot(root)
    verified = commands.package("normalize", "verify", "--target", root, "--source-id", source_id, "--format", "json")
    require(verified["overall_result"] == "verified", name + ": normalized format")
    inspection = commands.package("agent", "source-status", "--target", root, "--source-id", source_id)
    _matches(inspection, schema_document(inspection["schema_version"]))
    row = inspection["sources"][0]
    reasons = sorted([*page["expected_reasons"], *(["html_usability_recheck_required"] if stale else [])])
    usable = not reasons
    expected = {"extraction": "content_extracted", "evidence_usable": not page["expected_reasons"],
                "usability": "usable" if usable else "not_ready", "complete": True,
                "completeness_basis": "normalizer_status", "retrieval": "lexically_indexable",
                "semantic_adequacy": "not_evaluated", "evidence_accepted": False, "reasons": reasons}
    require({key: row.get(key) for key in expected} == expected, name + ": source contract: " + json.dumps(row))
    routed = commands.package("agent", "routes", "--target", root, "--from-file", route_path)
    _matches(routed, schema_document(routed["schema_version"]))
    local = next(route for route in routed["routes"] if route["kind"] == "local_source")
    route_state = "usable_for_caller_review" if usable else "blocked"
    require(local["state"] == route_state and not local["evidence_accepted"] and not local["network_executed"], name + ": local route")
    query = commands.script("query_index", page["extraction"]["extracted_text"], "--scope", "normalized", "--limit", "20", "--format", "json")
    require(any(source_id in hit["source_ids"] for hit in query["results"]), name + ": retained text retrieval")
    exported = commands.package("questions", "export", "--target", root, "--format", "json")
    question = next(row for row in exported["questions"] if row["slug"] == name)
    verdict = "pass" if usable else "blocked"
    require(question["coverage_verdict"] == verdict, name + ": read-only coverage")
    for reason in reasons:
        require(reason in json.dumps(question["coverage_facets"]), name + ": coverage reason " + reason)
    require(snapshot(root) == before, name + ": verification/inspection/routes/search/export wrote observed inputs")
    evaluated = commands.script("coverage_manifest", "evaluate", "--slug", name, "--format", "json")
    require(evaluated["coverage_verdict"] == verdict, name + ": evaluated coverage")
    require(changed(before, snapshot(root)) <= {f"sources/coverage/{name}.yml"}, name + ": coverage evaluation write scope")
    return {"case": name, "source_id": source_id, **expected, "route": route_state, "coverage": verdict}


def run(root, cli, corpus):
    root = Path(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    require(not root.exists(), "The HTML journey requires a fresh workspace path")
    cases = {page["name"]: page for page in json.loads(Path(corpus).read_text(encoding="utf-8"))}
    pages = [cases[name] for name in CASE_NAMES]
    commands = Commands(cli, root)
    records = initialize(commands, pages)
    observations = []
    for page in pages:
        source_id = records[page["file_name"]]["id"]
        before = snapshot(root)
        normalized = commands.script("normalize_sources", "--source-id", source_id, "--format", "json")
        require(normalized["summary"]["created"] == 1, page["name"] + ": selected creation")
        output = normalized_path(root, source_id)
        require(changed(before, snapshot(root)) == {output.relative_to(root).as_posix()}, page["name"] + ": selected write scope")
        metadata, _ = read_record(output)
        require(metadata["html_usability_version"] == 1, page["name"] + ": native revision")
        require(metadata["content_hash"] == page["extraction"]["content_hash"], page["name"] + ": extraction identity")
        require((metadata["unusable_evidence_reasons"] or []) == page["expected_reasons"], page["name"] + ": normalized reasons")
        observations.append(observe(commands, page, source_id))
        before = snapshot(root)
        replay = commands.script("normalize_sources", "--source-id", source_id, "--format", "json")
        require(replay["summary"]["skipped_existing"] == 1 and snapshot(root) == before, page["name"] + ": unchanged replay")
    page = cases["numeric-data"]
    source_id = records[page["file_name"]]["id"]
    path = normalized_path(root, source_id)
    metadata, body = read_record(path)
    del metadata["html_usability_version"]
    write_record(path, metadata, body)
    observe(commands, page, source_id, stale=True)
    before = snapshot(root)
    refreshed = commands.script("normalize_sources", "--source-id", source_id, "--format", "json")
    require(refreshed["summary"]["stale"] == refreshed["summary"]["updated"] == 1, "Selected legacy refresh")
    require(changed(before, snapshot(root)) == {path.relative_to(root).as_posix()}, "Legacy refresh scope")
    fresh, _ = read_record(path)
    require(fresh["content_hash"] == metadata["content_hash"] and fresh["raw_fingerprint"] == metadata["raw_fingerprint"], "Legacy refresh retained identities")
    observe(commands, page, source_id)
    before = snapshot(root)
    replay = commands.script("normalize_sources", "--source-id", source_id, "--format", "json")
    require(replay["summary"]["skipped_existing"] == 1 and snapshot(root) == before, "Legacy refresh settles")
    return {"html_cli_journeys": "passed", "cases": observations, "cached_refresh": "passed", "selected_replay": "passed"}


def legacy_status(commands, names, records, *, tooling):
    """Observe old producer claims without treating their cached pass as current."""
    before = snapshot(commands.root)
    for name in names:
        source_id = records[name + ".html"]["id"]
        result = commands.package("agent", "source-status", "--target", commands.root, "--source-id", source_id)
        _matches(result, schema_document(result["schema_version"]))
        row = result["sources"][0]
        require(row["evidence_usable"] is True and row["complete"] and row["retrieval"] == "lexically_indexable",
                name + ": historical extraction claim")
        require(row["usability"] == "not_ready" and row["reasons"] == ["html_usability_recheck_required"],
                name + ": legacy classification blocker")
        require(not row["evidence_accepted"] and row["semantic_adequacy"] == "not_evaluated", name + ": no legacy acceptance")
        checker = next(value for value in result["target"]["copied_checkers"] if value["id"] == "normalize_sources")
        require(checker["state"] == tooling, name + ": observed tooling identity")
    exported = commands.package("questions", "export", "--target", commands.root, "--format", "json")
    for row in exported["questions"]:
        if row["slug"] in names:
            require(row["coverage_verdict"] == "blocked" and "html_usability_recheck_required" in json.dumps(row["coverage_facets"]),
                    row["slug"] + ": cached coverage must be reevaluated")
    require(snapshot(commands.root) == before, "Legacy inspection/export must preserve evidence and cached verdicts")


def upgrade_records(root, cli, corpus):
    """Upgrade tools, explicitly refresh selected evidence, and preserve future claims.

    Existing raw/normalized/coverage bytes survive the tooling upgrade. Read-only
    owners reject legacy cached passes both before and after that upgrade. Only
    normalization migrates source qualifications; a future revision refuses the
    selected batch without writes, including with force.
    """
    root = Path(root)
    commands = Commands(cli, root)
    cases = {page["name"]: page for page in json.loads(Path(corpus).read_text(encoding="utf-8"))}
    names = ("gateway-body", "signin-body", "numeric-data")
    records = {Path(row["raw_paths"][0]).name: row for row in
               map(json.loads, (root / "sources/manifest.jsonl").read_text(encoding="utf-8").splitlines())}
    retained = {key: value for key, value in snapshot(root).items() if key.startswith(("raw/", "sources/", "wiki/"))}
    legacy_status(commands, names, records, tooling="different_bytes")
    before = snapshot(root)
    commands.package("upgrade", "--target", root, "--dry-run", json_output=False)
    require(snapshot(root) == before, "Upgrade preview must not write")
    commands.package("upgrade", "--target", root, json_output=False)
    after = snapshot(root)
    require(all(after.get(key) == value for key, value in retained.items()), "Tooling upgrade must preserve research artifacts")
    legacy_status(commands, names, records, tooling="matching_package_bytes")
    require((root / "scripts/_html_usability_profile.py").is_file(), "Upgrade must deliver the classifier helper")
    observations = []
    for index, name in enumerate(names):
        source_id = records[name + ".html"]["id"]
        path = normalized_path(root, source_id)
        metadata, _ = read_record(path)
        require("html_usability_version" not in metadata, name + ": tooling update is not record migration")
        before = snapshot(root)
        report = commands.script("normalize_sources", "--source-id", source_id, "--format", "json")
        require(report["summary"]["stale"] == report["summary"]["updated"] == 1, name + ": explicit legacy refresh")
        require(changed(before, snapshot(root)) == {path.relative_to(root).as_posix()}, name + ": scoped legacy refresh")
        fresh, _ = read_record(path)
        require(fresh["html_usability_version"] == 1 and fresh["content_hash"] == metadata["content_hash"]
                and fresh["raw_fingerprint"] == metadata["raw_fingerprint"], name + ": preserved text/original identity")
        observations.append(observe(commands, cases[name], source_id))
        if names[index + 1:]:
            legacy_status(commands, names[index + 1:], records, tooling="matching_package_bytes")
        before = snapshot(root)
        replay = commands.script("normalize_sources", "--source-id", source_id, "--format", "json")
        require(replay["summary"]["skipped_existing"] == 1 and snapshot(root) == before, name + ": refreshed replay")
    raw = {key: value for key, value in snapshot(root).items() if key.startswith("raw/")}
    require(raw == {key: value for key, value in retained.items() if key.startswith("raw/")}, "Upgrade/refresh altered originals")
    gateway_id = records["gateway-body.html"]["id"]
    gateway = normalized_path(root, gateway_id)
    metadata, body = read_record(gateway)
    metadata["html_usability_version"] = 2
    write_record(gateway, metadata, body)
    before = snapshot(root)
    refusal = commands.script("normalize_sources", "--source-id", records["numeric-data.html"]["id"],
                              "--source-id", gateway_id, "--force", "--format", "json", expected=2)
    require(refusal["error_code"] == "NORMALIZATION_PROFILE_UNSUPPORTED", "Future classification must refuse")
    require(snapshot(root) == before, "Future refusal must preserve the entire selected batch")
    row = commands.package("agent", "source-status", "--target", root, "--source-id", gateway_id)["sources"][0]
    require(row["usability"] == "not_ready" and "html_usability_profile_unsupported" in row["reasons"], "Future inspection must refuse")
    require(snapshot(root) == before, "Future inspection must remain read-only")
    return {"html_upgrade": "passed", "cases": observations, "future_profile_refusal": "passed", "originals_preserved": True}


def upgrade_fixture(root, cli, corpus, fixture):
    """Restore retained producer output and exercise supported tooling migration.

    The fixture contains actual older records, not historical executable code.
    Deliberate script-byte drift tests the observed mismatch and upgrade owner;
    executing an older installed producer is a separate compatibility observation.
    """
    root = Path(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    require(not root.exists(), "The upgrade journey requires a fresh workspace path")
    commands = Commands(cli, root)
    commands.package("init", "--target", root, "--project-name", "retained-html-upgrade",
                     "--project-description", "Refresh retained HTML qualifications.", json_output=False)
    value = json.loads(Path(fixture).read_text(encoding="utf-8"))
    require(value["fixture_version"] == 1, "Unsupported retained fixture format")
    for relative, content in value["files"].items():
        path = Path(relative)
        require(not path.is_absolute() and ".." not in path.parts and path.parts[0] in {"raw", "sources", "wiki"},
                "Retained fixture path must remain within research artifacts")
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    (root / "scripts/normalize_sources.py").write_text("# Tooling differs from the selected installation.\n", encoding="utf-8", newline="\n")
    (root / "scripts/_html_usability_profile.py").unlink()
    return {**upgrade_records(root, cli, corpus), "fixture_producer": value["producer"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--legacy-fixture", type=Path)
    args = parser.parse_args()
    result = (upgrade_fixture(args.root, args.cli, args.corpus, args.legacy_fixture)
              if args.legacy_fixture else run(args.root, args.cli, args.corpus))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
