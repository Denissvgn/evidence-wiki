#!/usr/bin/env python3
"""Read-only native extraction worker; the parent owns output publication."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

import normalize_sources as normalizer

MAX_MESSAGE_BYTES = 67_108_864


def main() -> int:
    request_path, output_path = map(Path, sys.argv[1:])
    if request_path.stat().st_size > MAX_MESSAGE_BYTES:
        return 2
    request = json.loads(request_path.read_text(encoding="utf-8"))
    root = Path(request["project_root"])
    item = normalizer.EligibleRecord(**request["item"])
    if item.method not in {"latex", "pdf", "link", "html", "docx", "table", "host_text", "codebase"}:
        return 2
    extractor = request["pdf_extractor"]
    if extractor is not None:
        extractor = normalizer.PdfExtractor(**extractor)
        if extractor.name != "pypdf":
            return 2
    with normalizer._bounded_inputs(root, max_entries=request["max_entries"],
                                    max_input_bytes=request["max_input_bytes"]):
        source = normalizer.normalize_selected_record(root, request["config"], item, extractor)
        normalizer._recheck_inputs()
        raw = json.dumps(asdict(source), ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > MAX_MESSAGE_BYTES:
            return 2
        output_path.write_bytes(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
