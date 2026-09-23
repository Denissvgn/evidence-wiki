"""Run supplied read-only cases through installed canonical tool mappings."""

import hashlib
import json
import sys
from pathlib import Path

from evidence_wiki.frameworks import invoke


def main():
    cases_path, output_path = map(Path, sys.argv[1:])
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    results = []
    for case in cases:
        value = invoke(json.dumps(case["call"]).encode(), target=case["target"])
        results.append({"id": case["id"], "value": value,
                        "identity": hashlib.sha256(value["result_json"].encode()).hexdigest()})
    output_path.write_text(json.dumps(results, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": "completed", "cases": len(results)}))


if __name__ == "__main__":
    main()
