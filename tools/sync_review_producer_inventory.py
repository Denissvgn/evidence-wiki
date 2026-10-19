#!/usr/bin/env python3
"""Maintain the complete path-only inventory of shipped review producer files."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evidence_wiki import _review_producer as producer  # noqa: E402

OUTPUT = ROOT / "src/evidence_wiki/_review_producer_inventory.json"


def inventory_bytes():
    roots = (("package/evidence_wiki", ROOT / "src/evidence_wiki"),
             *(("assets/" + name, ROOT / name) for name in producer.ASSET_NAMES))
    observed = producer.observe_roots(roots, prospective_inventory=True)
    paths = sorted(observed[0], key=lambda path: path.encode("utf-8"))
    value = {"schema_version": "evidence-review-producer-inventory/v1", "paths": paths}
    raw = producer.canonical(value)
    producer.inventory_document(raw)
    sizes = sum(row[4] for path, row in observed[0].items() if row is not None and path != producer.INVENTORY_PATH)
    if sizes + len(raw) > producer.MAX_TOTAL_BYTES:
        raise ValueError("producer inventory exceeds complete byte bound")
    return raw


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true")
    action.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        expected = inventory_bytes()
        if args.write:
            if OUTPUT.is_symlink():
                raise ValueError("inventory output must not be linked")
            OUTPUT.write_bytes(expected)
        elif OUTPUT.read_bytes() != expected:
            raise ValueError("review producer inventory differs from complete source membership")
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
