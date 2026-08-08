#!/usr/bin/env python3
"""Minimal conforming normalizer adapter, used as the reference implementation in tests.

Reads one `normalizer_adapter_request` on stdin and writes one
`normalizer_adapter_result` on stdout. Renders each top-level key of the JSON payload as
a `###` facet section, which is what makes a value quotable by `verify_quotes.py`.

Behaviour can be perturbed for failure-path tests through EW_STUB_* environment
variables; with none set it is a well-behaved adapter.
"""

import json
import os
import sys


def main() -> int:
    request = json.loads(sys.stdin.read())

    mode = os.environ.get("EW_STUB_MODE", "ok")
    if mode == "garbage":
        sys.stdout.write("not json at all\n")
        return 0
    if mode == "trailing":
        sys.stdout.write('{"a": 1}\n{"b": 2}\n')
        return 0
    if mode == "nonzero":
        sys.stderr.write("stub adapter refused to run\n")
        return 3
    if mode == "hang":
        import time

        time.sleep(30)
        return 0

    project_root = request["project_root"]
    payload = {}
    for relative in request["raw_paths"]:
        with open(os.path.join(project_root, relative), encoding="utf-8") as handle:
            payload = json.load(handle)
        break

    # One facet section per top-level key. Every value considered is rendered
    # verbatim, so coverage is complete unless EW_STUB_CAP drops the tail.
    cap = int(os.environ.get("EW_STUB_CAP", "0")) or len(payload)
    sections = []
    outline = []
    coverage_sections = []
    for index, (key, value) in enumerate(payload.items()):
        if index < cap:
            outline.append([3, key])
            sections.append(f"### {key}\n\n- {key}: {value}\n")
            coverage_sections.append({"heading": key, "total": 1, "rendered": 1})
        else:
            # Capped away: still counted, so the ratio tells the truth about what a
            # reader can quote.
            coverage_sections.append(
                {"heading": key, "total": 1, "rendered": 0, "note": "dropped by EW_STUB_CAP"}
            )
    rendered_values = sum(section["rendered"] for section in coverage_sections)
    total_values = sum(section["total"] for section in coverage_sections)
    rendered_coverage = {
        "total_values": total_values,
        "rendered_values": rendered_values,
        "ratio": 1.0 if total_values == 0 else round(rendered_values / total_values, 4),
        "sections": [section for section in coverage_sections if section["rendered"] or "note" in section],
    }
    if mode == "bad_coverage":
        rendered_coverage["ratio"] = 0.01

    if mode == "level_two_heading":
        sections.append("## Links\n\nA heading that would collide with the record's sections.\n")

    result = {
        "schema_version": "1.0",
        "document_type": "normalizer_adapter_result",
        "adapter": {
            "name": os.environ.get("EW_STUB_NAME", "stub-normalize"),
            "version": os.environ.get("EW_STUB_VERSION", "1.0.0"),
        },
        "status": os.environ.get("EW_STUB_STATUS", "content_extracted"),
        "title": f"Stub rendering of {request['manifest_record']['id']}",
        "abstract": "Structured payload rendered by the stub adapter.",
        "outline": outline,
        "body_markdown": "\n".join(sections),
        "rendered_coverage": rendered_coverage,
        "warnings": json.loads(os.environ.get("EW_STUB_WARNINGS", "[]")),
    }
    if mode == "no_coverage":
        del result["rendered_coverage"]
    if mode == "stdout_noise":
        sys.stdout.write("progress: starting\n")
    if mode == "stderr_noise":
        sys.stderr.write("progress: starting\n")
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
