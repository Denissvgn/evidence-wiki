"""Generate native HTML fixture records from retained originals through their owner."""

import copy
import json
from pathlib import Path

from tests._script_loader import load_script


def normalize_html_fixture(root: Path, record: dict) -> Path:
    normalizer = load_script("html_fixture_normalizer", "normalize_sources.py")
    source = normalizer.normalize_html_record(root, copy.deepcopy(record))
    output = root / "sources/normalized" / (normalizer.safe_source_id(record["id"]) + ".md")
    output.parent.mkdir(parents=True, exist_ok=True)
    metadata = normalizer.frontmatter_for(source, "sources/manifest.jsonl", output, "2026-09-28")
    output.write_text(normalizer.render_markdown(source, metadata), encoding="utf-8")
    return output


def normalize_html_manifest(root: Path) -> None:
    for line in (root / "sources/manifest.jsonl").read_text().splitlines():
        if line.strip():
            record = json.loads(line)
            if record.get("kind") == "html":
                normalize_html_fixture(root, record)
