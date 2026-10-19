"""Synthetic review judgments selected by immutable native context coordinates."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

CHECKS = ("support", "source_suitability", "scope", "time", "units", "counterevidence")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("native_case_duplicate_key")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("native_case_nonfinite_number")


def _read(raw):
    if not 0 < len(raw) <= 1_048_576:
        raise ValueError("native_case_document_bound")
    return json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)


def validate_plan(plan):
    if (type(plan) is not dict or set(plan) != {"schema_version", "rules", "default_verdict"}
            or plan["schema_version"] != "native-review-case-plan/v1" or plan["default_verdict"] != "pass"
            or type(plan["rules"]) is not list or len(plan["rules"]) != 6):
        raise ValueError("native_case_plan_invalid")
    coordinates = set()
    expected = {"context-0": "fail", "context-1": "unknown", "context-2": "pass"}
    for rule in plan["rules"]:
        if (type(rule) is not dict or set(rule) != {"config_ref", "context_id_prefix", "verdict"}
                or rule["config_ref"] not in {"evidence", "critic"}
                or rule["context_id_prefix"] not in expected
                or rule["verdict"] != expected[rule["context_id_prefix"]]):
            raise ValueError("native_case_rule_invalid")
        key = (rule["config_ref"], rule["context_id_prefix"])
        if key in coordinates:
            raise ValueError("native_case_rule_duplicate")
        coordinates.add(key)
    return plan


def decide(envelope, config_ref, plan):
    if type(envelope) is not dict or type(envelope.get("input")) is not dict:
        raise ValueError("native_case_input_invalid")
    data = envelope["input"]
    if "case_id" in data:
        if type(data.get("case_id")) is not str or type(data.get("supported")) is not bool:
            raise ValueError("native_case_reference_invalid")
        return {"case_id": data["case_id"], "label": "accept" if data["supported"] else "reject"}
    context_id = envelope.get("job", {}).get("context_id")
    if (config_ref not in {"evidence", "critic"} or type(context_id) is not str
            or re.fullmatch(r"context-[a-f0-9]{32}", context_id) is None):
        raise ValueError("native_case_coordinate_invalid")
    matched = [rule for rule in plan["rules"] if rule["config_ref"] == config_ref
               and context_id.startswith(rule["context_id_prefix"])]
    if len(matched) > 1:
        raise ValueError("native_case_coordinate_ambiguous")
    verdict = matched[0]["verdict"] if matched else plan["default_verdict"]
    return {"verdicts": dict.fromkeys(CHECKS, verdict),
            "rationale": "Synthetic fixture judgment from a fixed native context coordinate; model meaning is not assessed."}


def main():
    if len(sys.argv) != 3:
        raise ValueError("native_case_arguments_invalid")
    plan = validate_plan(_read(Path(sys.argv[2]).read_bytes()))
    envelope = _read(sys.stdin.buffer.read(1_048_577))
    print(json.dumps(decide(envelope, sys.argv[1], plan), ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
