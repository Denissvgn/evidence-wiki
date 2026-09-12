#!/usr/bin/env python3
"""Report a fixed source inventory while retaining raw coverage and attribution limits."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from coverage import Coverage, CoverageData
from coverage.exceptions import CoverageException

SOURCE_ROOTS = ("src/evidence_wiki", "workspace-template/scripts")


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def source_snapshot(root: Path) -> dict:
    paths = sorted(path for name in SOURCE_ROOTS for path in (root / name).rglob("*.py"))
    if not paths or any(path.is_symlink() for path in paths):
        raise ValueError("canonical inventory is empty or contains a symbolic link")
    commit = subprocess.run(  # noqa: S603 - fixed read-only Git command.
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, encoding="utf-8", check=False,  # noqa: S607
    )
    return {
        "root": str(root),
        "commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "sources": {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths
        },
    }


def validate_shard(path: Path) -> CoverageData:
    # Inspect a preserved copy read-only: CoverageData can initialize an empty DB.
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as database:
        version = database.execute("SELECT version FROM coverage_schema").fetchall()
        if len(version) != 1:
            raise ValueError("missing coverage schema version; producer cause unestablished")
    data = CoverageData(basename=str(path))
    data.read()
    if not data.has_arcs():
        raise ValueError("no branch data; empty/statement-only shard receives no credit")
    if not data.measured_files():
        raise ValueError("empty branch shard; producer cause unestablished")
    return data


def generate_reports(root: Path, data_file: Path, output: Path, snapshot: dict) -> dict:
    """Preserve selected shards before filtering; never combine or consume originals."""
    output.mkdir(parents=True, exist_ok=False)
    raw = output / "raw"
    raw.mkdir()
    manifest = {
        "source_snapshot": snapshot,
        "python": platform.python_version(),
        "coverage_version": __import__("coverage").__version__,
        "selected_pattern": str(data_file) + ".*",
        "shards": [],
        "reports": {},
        "limits": [
            "Both views use every canonical Python source, including unexecuted files.",
            "Direct execution credits only exact canonical paths.",
            "Copy aliases are diagnostic only: coverage paths do not prove execution-time copy identity.",
            "Invalid/empty shards are preserved without credit; their producer cause is unestablished.",
            "Coverage measures execution, not correctness; consult the independent suite outcome.",
        ],
    }
    manifest_path = output / "manifest.json"
    write_json(manifest_path, manifest)
    try:
        selected = sorted(path for path in data_file.parent.glob(data_file.name + ".*") if path.is_file())
        if data_file.is_file():
            selected.insert(0, data_file)
        # Complete preservation and selection identity precede any data loading.
        for number, path in enumerate(selected):
            copied = raw / f"{number:05d}.coverage"
            shutil.copyfile(path, copied)
            manifest["shards"].append({
                "path": str(path), "preserved": str(copied.relative_to(output)),
                "sha256": hashlib.sha256(copied.read_bytes()).hexdigest(),
                "status": "unread", "size_bytes": copied.stat().st_size,
            })
        write_json(manifest_path, manifest)
        current = source_snapshot(root)
        if snapshot.get("root") != str(root) or snapshot.get("sources") != current["sources"]:
            raise ValueError("canonical sources changed or inventory differs from the pre-execution snapshot")
        canonical = {str(root / path): path for path in snapshot["sources"]}
        scripts = {Path(path).name: str(root / path) for path in snapshot["sources"]
                   if path.startswith("workspace-template/scripts/")}
        views = {name: CoverageData(basename=str(output / f"{name}.coverage"))
                 for name in ("direct", "with-copy-aliases")}
        for data in views.values():
            data.add_arcs({})
        valid = 0
        for entry in manifest["shards"]:
            try:
                data = validate_shard(output / entry["preserved"])
            except (CoverageException, sqlite3.Error, ValueError, OSError) as error:
                entry.update(status="excluded", reason=str(error))
                continue
            valid += 1
            direct, aliases, excluded = {}, {}, []
            for path in sorted(data.measured_files()):
                arcs = data.arcs(path) or []
                if path in canonical:
                    direct[path] = arcs
                    aliases.setdefault(path, []).extend(arcs)
                elif Path(path).parent.name == "scripts" and Path(path).name in scripts:
                    target = scripts[Path(path).name]
                    aliases.setdefault(target, []).extend(arcs)
                else:
                    excluded.append(path)
            views["direct"].add_arcs(direct)
            views["with-copy-aliases"].add_arcs(aliases)
            entry.update(status="included", direct_paths=sorted(direct), excluded_paths=excluded,
                         copy_paths=sorted(set(data.measured_files()) - set(direct) - set(excluded)))
        manifest["readable_shards"] = valid
        manifest["excluded_shards"] = len(selected) - valid
        write_json(manifest_path, manifest)
        if not valid:
            raise ValueError("no readable branch shards; inspect raw files and shard dispositions")
        morfs = sorted(canonical)
        for label, data in views.items():
            data.write()
            report_dir = output / label
            report_dir.mkdir()
            coverage = Coverage(data_file=data.data_filename(), config_file=False, branch=True)
            coverage.load()
            with (report_dir / "coverage.txt").open("w", encoding="utf-8", newline="\n") as stream:
                coverage.report(morfs=morfs, file=stream, show_missing=True, skip_empty=False)
            coverage.xml_report(morfs=morfs, outfile=str(report_dir / "coverage.xml"))
            coverage.html_report(morfs=morfs, directory=str(report_dir / "html"))
            coverage.json_report(morfs=morfs, outfile=str(report_dir / "coverage.json"))
            manifest["reports"][label] = json.loads((report_dir / "coverage.json").read_text(encoding="utf-8"))["totals"]
        for field in ("num_statements", "num_branches"):
            if manifest["reports"]["direct"][field] != manifest["reports"]["with-copy-aliases"][field]:
                raise ValueError(f"coverage views have different {field} denominators")
        if source_snapshot(root)["sources"] != snapshot["sources"]:
            raise ValueError("canonical sources changed during reporting")
        manifest["status"] = "reported"
    except Exception as error:
        manifest.update(status="failed", error=str(error))
        raise
    finally:
        write_json(manifest_path, manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("snapshot", "report"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--data-file", type=Path, default=Path(".coverage"))
    parser.add_argument("--output", type=Path, default=Path("coverage-evidence"))
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve()
        if args.action == "snapshot":
            write_json(args.source_manifest, source_snapshot(root))
        else:
            generate_reports(root, args.data_file.resolve(), args.output.resolve(),
                             json.loads(args.source_manifest.read_text(encoding="utf-8")))
    except (OSError, ValueError, CoverageException) as error:
        print(f"Coverage evidence failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
