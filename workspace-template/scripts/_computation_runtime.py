#!/usr/bin/env python3
"""Computation over captured normalized evidence using the existing usage owners."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import string
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path, PurePosixPath

import yaml
from _computation_contract import (
    MAX_BYTES,
    MAX_GROUPS,
    MAX_RECORDS,
    MAX_SOURCES,
    RESULT_SCHEMA,
    configuration_id,
    declaration,
    definition_id,
    topological,
    validate_result,
    validate_yaml,
)
from _computation_expression import (
    Budget,
    ComputationInvalid,
    Evaluator,
    Value,
    decimal_text,
    number,
    references,
    require,
)
from _computation_schedule import evaluate_schedules, validate_clock
from _evidence_revision import canonical_bytes, capture_workspace, content_id
from _publication_context import authorized_capture, captured_view
from _workspace_module_loader import load_workspace_module

SCRIPT_DIR = Path(__file__).resolve().parent
_SIBLINGS = {}
MAX_INPUT_BYTES = 8_388_608


def sibling(name):
    if name not in _SIBLINGS:
        _SIBLINGS[name] = load_workspace_module(SCRIPT_DIR, name)
    return _SIBLINGS[name]


_LOADED_PRODUCER_ID = sibling("_selected_publication").producer_identity()


def json_numbers(data):
    require(len(data) <= MAX_BYTES, "computation_record_bytes_bound")
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "computation_duplicate_json_key")
            result[key] = value
        return result
    def invalid(_value):
        raise ComputationInvalid("computation_nonfinite_number")
    try:
        result = json.loads(data.decode("utf-8"), parse_float=number, parse_int=number,
                            parse_constant=invalid, object_pairs_hook=pairs)
    except (UnicodeError, ValueError, RecursionError):
        raise ComputationInvalid("computation_record_json_invalid") from None
    pending, count = [(result, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        require(depth <= 24 and count <= 65_536, "computation_record_tree_bound")
        require(type(item) in {dict, list, str, bool, Decimal, type(None)}, "computation_record_type_invalid")
        if type(item) is dict:
            require(len(item) <= 128, "computation_record_mapping_bound")
            pending.extend((value, depth + 1) for value in item.values())
        elif type(item) is list:
            require(len(item) <= MAX_RECORDS, "computation_record_count_bound")
            pending.extend((value, depth + 1) for value in item)
        elif type(item) is str:
            require(len(item) <= 4096 and not any(0xD800 <= ord(char) <= 0xDFFF for char in item), "computation_record_string_bound")
    return result


def group_wire(value):
    raw = value.value if isinstance(value, Value) else value
    if type(raw) is Decimal:
        return {"decimal": decimal_text(raw)}
    require(type(raw) in {str, bool, type(None)}, "computation_group_scalar_required")
    return raw


def pointer_value(value, pointer):
    result = sibling("_structured_view").resolve_pointer(value, pointer)
    require(result.ok, "computation_record_pointer_missing")
    return result.value


def load_definition(config):
    if config.get("computation") is None:
        return None
    result = declaration(config["computation"], config)
    validate_clock(result)
    return result


class Computation:
    """One captured input generation and one shared expression execution budget."""

    def __init__(self, root, capture, config, definition, approved=None):
        self.root, self.capture, self.config, self.definition = root, capture, config, definition
        self.definition_id = definition_id(definition)
        self.approved = approved or {}
        self.evaluator = Evaluator(definition["arithmetic"], Budget(), definition["tables"])
        self.lineage = {"table:" + key: {"kind": "table", "definition_id": self.definition_id, "pointer": "/tables/" + key}
                        for key in definition["tables"]}
        self.selections, self.sources, self.selected_records = {}, {}, {}
        self.total_input_bytes = 0
        self.aggregations, self.graphs = {}, {}
        manifest_path = config.get("sources", {}).get("manifest_path", "sources/manifest.jsonl")
        self.manifest = {}
        for line in capture.files.get(manifest_path, b"").splitlines():
            if not line.strip():
                continue
            require(len(line) <= MAX_BYTES and len(self.manifest) < 4096, "computation_manifest_bound")
            record = sibling("_record_artifacts").json_document(line)
            require(type(record.get("id")) is str and 0 < len(record["id"]) <= 512
                    and record["id"] not in self.manifest, "computation_manifest_identity_invalid")
            self.manifest[record["id"]] = record

    def register(self, descriptor):
        identifier = content_id("computation-contributor/v1", descriptor)
        self.lineage[identifier] = descriptor
        require(len(self.lineage) <= 65_536, "computation_lineage_bound")
        return identifier

    def wrap(self, raw, descriptor, pointer):
        if type(raw) is dict and set(raw) == {"decimal"}:
            raw = number(raw["decimal"])
        if type(raw) is dict:
            result = {key: self.wrap(value, descriptor, pointer + "/" + key.replace("~", "~0").replace("/", "~1"))
                      for key, value in sorted(raw.items())}
            return Value(result)
        if type(raw) is list:
            return Value([self.wrap(value, descriptor, pointer + "/" + str(index)) for index, value in enumerate(raw)])
        return Value(raw, frozenset({self.register({**descriptor, "pointer": pointer})}))

    def select(self, selector):
        key = content_id("computation-selector/v1", selector)
        if key in self.selections:
            return self.selections[key]
        pattern = PurePosixPath(selector["source_glob"])
        matches = sorted(name for name in self.capture.files if PurePosixPath(name).parent == pattern.parent
                         and fnmatch.fnmatchcase(PurePosixPath(name).name, pattern.name))
        normalizer = sibling("_normalized_contract")
        expected = [(pattern.parent / (normalizer.safe_source_id(source_id) + ".md")).as_posix()
                    for source_id, record in self.manifest.items() if record.get("status") not in {"rejected", "superseded"}
                    and fnmatch.fnmatchcase(normalizer.safe_source_id(source_id) + ".md", pattern.name)]
        require(len(expected) <= MAX_SOURCES and len(expected) == len(set(expected)), "computation_selection_identity_bound")
        require(set(expected) <= set(matches), "computation_required_record_missing")
        require(len(matches) <= MAX_SOURCES and (matches or selector["allow_empty"]), "computation_required_selection_empty_or_large")
        records, seen_keys = [], set()
        views = sibling("_structured_view")
        verifier = sibling("verify_quotes")
        normalized_root = self.config.get("sources", {}).get("normalized_dir", "sources/normalized")
        for name in matches:
            self.evaluator.budget.tick()
            metadata, body = verifier.split_page(self.capture.files[name].decode("utf-8"))
            source_id = metadata.get("source_id")
            require(type(source_id) is str and source_id, "computation_normalized_identity_missing")
            expected_name = normalizer.safe_source_id(source_id) + ".md"
            require(name == (PurePosixPath(normalized_root) / expected_name).as_posix(), "computation_normalized_identity_mismatch")
            require(source_id in self.manifest and self.manifest[source_id].get("status") not in {"rejected", "superseded"},
                    "computation_source_unavailable")
            violations = normalizer.validate_document(self.root / name, metadata, body, manifest_by_id=self.manifest,
                normalized_root=self.root / normalized_root, project_root=self.root, config=self.config)
            require(not violations and metadata.get("evidence_usable") is not False, "computation_normalized_contract_invalid")
            binding = views.structured_view_binding(metadata)
            require(binding is not None, "computation_structured_view_missing")
            sidecar = (PurePosixPath(normalized_root) / (normalizer.safe_source_id(source_id) + views.SIDECAR_SUFFIX)).as_posix()
            require(binding[0] == sidecar and sidecar in self.capture.files, "computation_structured_view_path_mismatch")
            raw = self.capture.files[sidecar]
            require(views.content_hash(raw) == binding[1], "computation_structured_view_hash_mismatch")
            raw_files = {}
            for relative in metadata.get("raw_paths", []):
                sibling("_record_artifacts").artifact_path(relative)
                members = {raw_name: "sha256:" + hashlib.sha256(data).hexdigest() for raw_name, data in self.capture.files.items()
                           if raw_name == relative or raw_name.startswith(relative.rstrip("/") + "/")}
                require(bool(members), "computation_raw_input_missing")
                raw_files.update(members)
            require(bool(raw_files) and len(raw_files) <= 128, "computation_raw_input_bound")
            descriptor = {"source_id": source_id, "record_path": name,
                          "record_sha256": "sha256:" + hashlib.sha256(self.capture.files[name]).hexdigest(),
                          "structured_path": sidecar, "structured_sha256": binding[1],
                          "source_revision": self.approved.get(source_id), "raw_files": raw_files,
                          "manifest_record_id": content_id("computation-manifest-record/v1", self.manifest[source_id])}
            if source_id not in self.sources:
                self.total_input_bytes += len(raw)
                require(self.total_input_bytes <= MAX_INPUT_BYTES and len(self.sources) < MAX_SOURCES, "computation_input_bound")
                self.sources[source_id] = descriptor
            else:
                require(self.sources[source_id] == descriptor, "computation_source_identity_duplicate")
            selected = pointer_value(json_numbers(raw), selector["records_pointer"])
            if type(selected) is dict:
                entries = [(selector["records_pointer"], selected)]
            else:
                require(type(selected) is list, "computation_records_container_required")
                entries = [(selector["records_pointer"] + "/" + str(index), row) for index, row in enumerate(selected)]
            for pointer, row in entries:
                require(type(row) is dict, "computation_record_object_required")
                identity = self.register({**descriptor, "kind": "record", "pointer": pointer})
                if selector["row_key"] is not None:
                    value = pointer_value(row, selector["row_key"])
                    value = number(value["decimal"]) if type(value) is dict and set(value) == {"decimal"} else value
                    row_key = canonical_bytes(group_wire(value))
                    require(row_key not in seen_keys, "computation_duplicate_record_key")
                    seen_keys.add(row_key)
                wrapped = self.wrap(row, {**descriptor, "kind": "field"}, pointer)
                record = {"id": identity, "source_id": source_id, "pointer": pointer,
                          "record": Value(wrapped.value, frozenset({identity}))}
                records.append(record)
                self.selected_records[identity] = {"id": identity, "source_id": source_id, "pointer": pointer}
                require(len(records) <= MAX_RECORDS and len(self.selected_records) <= MAX_RECORDS, "computation_record_count_bound")
        require(records or selector["allow_empty"], "computation_required_selection_empty")
        self.selections[key] = records
        return records

    def truth(self, expression, environment):
        if expression is None:
            return Value(True)
        value = self.evaluator.evaluate(expression, environment)
        require(type(value.value) is bool, "computation_boolean_required")
        return value

    def reduce(self, operation, values):
        if operation in {"sum", "avg"}:
            total = Value(Decimal(0))
            for value in values:
                total = self.evaluator.add(total, value)
            return total if operation == "sum" else self.evaluator.divide(total, Value(Decimal(len(values)))) if values else Value(None)
        require(operation in {"min", "max"}, "computation_reducer_unsupported")
        if not values:
            return Value(None)
        require(all(type(value.value) is Decimal for value in values), "computation_decimal_required")
        return Value((min if operation == "min" else max)(value.value for value in values),
                     frozenset().union(*(value.lineage for value in values)), any(value.rounded for value in values))

    def aggregate(self):
        for identifier, item in sorted(self.definition["aggregations"].items()):
            records, groups, excluded = self.select(item["selector"]), {}, []
            if not item["group_by"]:
                groups[canonical_bytes([])] = {"key": [], "records": [], "selection_lineage": set()}
            for record in records:
                environment = {"record": record["record"]}
                passed = self.truth(item["filter"], environment)
                if not passed.value:
                    excluded.append({"record_id": record["id"], "reason": "filter_false", "lineage": sorted(passed.lineage)})
                    continue
                parts = [self.evaluator.evaluate(expression, environment) for expression in item["group_by"]]
                group = [group_wire(part) for part in parts]
                key = canonical_bytes(group)
                holder = groups.setdefault(key, {"key": group, "records": [], "selection_lineage": set()})
                require(len(groups) <= MAX_GROUPS, "computation_group_bound")
                holder["records"].append(record)
                holder["selection_lineage"].update(passed.lineage)
                for part in parts:
                    holder["selection_lineage"].update(part.lineage)
            rendered = []
            for key in sorted(groups):
                group = groups[key]
                values, metrics = {}, {}
                for name, metric in sorted(item["metrics"].items()):
                    if metric["op"] == "count":
                        result = Value(Decimal(len(group["records"])), frozenset(row["id"] for row in group["records"]))
                    else:
                        inputs = [self.evaluator.evaluate(metric["expr"], {"record": row["record"]}) for row in group["records"]]
                        if metric["op"] == "ratio":
                            denominators = [self.evaluator.evaluate(metric["denominator"], {"record": row["record"]}) for row in group["records"]]
                            result = self.evaluator.divide(self.reduce("sum", inputs), self.reduce("sum", denominators))
                        else:
                            result = self.reduce(metric["op"], inputs)
                    result = Value(result.value, result.lineage | frozenset(group["selection_lineage"]), result.rounded)
                    rule = self.register({"kind": "rule", "definition_id": self.definition_id, "pointer": "/aggregations/" + identifier})
                    result = Value(result.value, result.lineage | {rule}, result.rounded)
                    values[name] = result
                    metrics[name] = {**self.evaluator.render(result), "unit": metric["unit"]}
                group["values"] = values
                rendered.append({"key": group["key"], "metrics": metrics, "record_ids": [row["id"] for row in group["records"]]})
            self.aggregations[identifier] = {"groups": groups, "rendered": {"groups": rendered,
                "selected_count": len(records), "included_count": len(records) - len(excluded), "excluded": excluded}}

    def resolve_inputs(self, inputs):
        resolved = {}
        for key, ref in sorted(inputs.items()):
            if ref["kind"] == "graph":
                resolved[key] = self.graphs[ref["id"]]["output_values"][ref["output"]]
            else:
                groups = self.aggregations[ref["id"]]["groups"]
                if ref["group"] is not None:
                    group = []
                    for part in ref["group"]:
                        group.append({"decimal": decimal_text(number(part["decimal"]))} if type(part) is dict else part)
                    selected = canonical_bytes(group)
                    require(selected in groups, "computation_group_not_found")
                    resolved[key] = groups[selected]["values"][ref["metric"]]
                else:
                    resolved[key] = self.reduce(ref["reduce"], [groups[name]["values"][ref["metric"]] for name in sorted(groups)])
        return resolved

    def evaluate_graphs(self):
        definitions = self.definition["graphs"]
        order = topological({key: {ref["id"] for ref in item["inputs"].values() if ref["kind"] == "graph"}
                             for key, item in definitions.items()})
        for key in order:
            item = definitions[key]
            constants = {name: Value(number(value["value"]), frozenset({self.register({"kind": "constant",
                "definition_id": self.definition_id, "pointer": "/graphs/" + key + "/constants/" + name})}))
                         for name, value in item["constants"].items()}
            inputs, nodes = self.resolve_inputs(item["inputs"]), {}
            units = {name: value["unit"] for name, value in item["constants"].items()}
            units.update({name: value["unit"] for name, value in item["nodes"].items()})
            units.update({name: self.definition["aggregations"][ref["id"]]["metrics"][ref["metric"]]["unit"]
                          if ref["kind"] == "aggregation" else self.graphs[ref["id"]]["rendered"]["outputs"][ref["output"]]["unit"]
                          for name, ref in item["inputs"].items()})
            dependencies = {}
            for name, node in item["nodes"].items():
                refs = references(node["expr"])
                dependencies[name] = {ref.split(".")[1] if ref.startswith("nodes.") else ref.split(".")[0]
                                      for ref in refs if ref.startswith("nodes.") or ref.split(".")[0] in item["nodes"]}
            for name in topological(dependencies):
                environment = {**constants, **inputs, **nodes, "constants": Value(constants), "inputs": Value(inputs), "nodes": Value(nodes)}
                value = self.evaluator.evaluate(item["nodes"][name]["expr"], environment)
                rule = self.register({"kind": "rule", "definition_id": self.definition_id, "pointer": "/graphs/" + key + "/nodes/" + name})
                nodes[name] = Value(value.value, value.lineage | {rule}, value.rounded)
            values = {**constants, **inputs, **nodes}
            outputs = {name: values[target] for name, target in sorted(item["output_mapping"].items())}
            require(all(value.value is not None for value in outputs.values()), "computation_output_incomplete")
            self.graphs[key] = {"output_values": outputs, "rendered": {
                "nodes": {name: {**self.evaluator.render(value), "unit": item["nodes"][name]["unit"]} for name, value in sorted(nodes.items())},
                "inputs": {name: {**self.evaluator.render(value), "unit": units[name]} for name, value in sorted(inputs.items())},
                "outputs": {name: {**self.evaluator.render(value), "unit": units[item["output_mapping"][name]]} for name, value in outputs.items()},
                "order": topological(dependencies)}}

    def message(self, template, environment):
        pieces, lineage = [], set()
        try:
            parts = list(string.Formatter().parse(template))
        except ValueError:
            raise ComputationInvalid("computation_message_invalid") from None
        require(len(parts) <= 64, "computation_message_bound")
        for literal, field, spec, conversion in parts:
            pieces.append(literal)
            if field is not None:
                require(not spec and conversion is None and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*", field) is not None,
                        "computation_message_format_forbidden")
                value = self.evaluator.evaluate(field, environment)
                require(type(value.value) in {Decimal, str, bool, type(None)}, "computation_message_scalar_required")
                pieces.append(decimal_text(value.value) if type(value.value) is Decimal else str(value.value))
                lineage.update(value.lineage)
        result = "".join(pieces)
        require(len(result.encode("utf-8")) <= 4096, "computation_message_bound")
        return result, lineage

    def verify(self):
        findings, summaries = [], []
        for key, item in sorted(self.definition["invariants"].items()):
            targets = [(row["id"], {"record": row["record"]}) for row in self.select(item["selector"])] if item["selector"] is not None else [("computed", self.resolve_inputs(item["inputs"]))]
            checked = excluded = failures = 0
            for target, environment in targets:
                condition = self.truth(item["filter"], environment)
                if not condition.value:
                    excluded += 1
                    continue
                checked += 1
                result = self.truth(item["assertion"], environment)
                if result.value:
                    continue
                failures += 1
                message, interpolated = self.message(item["failure_message"], environment)
                lineage = sorted(result.lineage | condition.lineage | interpolated)
                finding = {"invariant_id": key, "target": target, "severity": item["severity"], "message": message, "lineage": lineage}
                rules = {**self.definition, "clock": {**self.definition["clock"], "as_of": None}}
                finding["finding_id"] = content_id("computation-finding/v1", {"rules": definition_id(rules), "invariant_id": key,
                    "target": target, "sources": sorted(self.sources.values(), key=lambda row: row["source_id"]), "message": message})
                findings.append(finding)
                require(len(findings) <= 128, "computation_finding_bound")
            summaries.append({"id": key, "severity": item["severity"], "checked": checked, "excluded": excluded,
                              "failures": failures, "passed": failures == 0, "complete": True})
        return summaries, findings

    def result(self, as_of=None, *, completed=frozenset()):
        self.aggregate()
        self.evaluate_graphs()
        invariants, findings = self.verify()
        inputs = sorted(self.sources.values(), key=lambda value: value["source_id"])
        input_id = content_id("computation-input/v1", inputs)
        clock = evaluate_schedules(self.definition, as_of, self.evaluator, self.resolve_inputs, input_id, completed=completed)
        result = {"schema_version": RESULT_SCHEMA, "definition_id": self.definition_id, "definition": self.definition, "input_id": input_id,
                  "configuration_id": configuration_id(self.config),
                  "engine_id": _LOADED_PRODUCER_ID, "clock": clock,
                  "status": "failed" if any(item["severity"] == "error" for item in findings) else "passed",
                  "sources": inputs, "records": sorted(self.selected_records.values(), key=lambda value: value["id"]),
                  "aggregations": {key: value["rendered"] for key, value in sorted(self.aggregations.items())},
                  "graphs": {key: value["rendered"] for key, value in sorted(self.graphs.items())},
                  "invariants": invariants, "findings": findings,
                  "limits": ["Arithmetic follows the declared rules; their domain meaning is not authenticated.",
                             "Units are declared annotations; dimensional correctness requires review.",
                             "Schedules describe actions and never authorize external execution."],
                  "operations": self.evaluator.budget.consumed}
        needed = set()
        def gather(value):
            if type(value) is dict:
                needed.update(value.get("lineage", []))
                for part in value.values():
                    gather(part)
            elif type(value) is list:
                for part in value:
                    gather(part)
        gather(result)
        needed.update(self.selected_records)
        result["lineage"] = {key: self.lineage[key] for key in sorted(needed)}
        result["result_id"] = content_id(RESULT_SCHEMA, result)
        return validate_result(result)


def evaluate(project_root, *, as_of=None, config=None, capture=None, view=None, completed=frozenset()):
    require(sibling("_selected_publication").producer_identity() == _LOADED_PRODUCER_ID, "computation_engine_changed")
    root = Path(project_root).resolve()
    capture = capture or capture_workspace(root)
    raw_config = capture.files.get("research.yml", b"").decode("utf-8")
    validate_yaml(raw_config)
    actual_config = yaml.safe_load(raw_config)
    require(type(actual_config) is dict and (config is None or actual_config == config), "computation_configuration_changed")
    config = actual_config
    definition = load_definition(config)
    require(definition is not None, "computation_not_configured")
    usage = sibling("_evidence_usage")
    manager = nullcontext(view) if view is not None else usage.current_view(root, config) if usage.configured(config) else nullcontext(None)
    with manager as current:
        selected = sibling("_selected_publication")
        readiness = sibling("publication_readiness")
        _validated, inputs = selected.validate_before_materialization(capture.files, readiness, usage_view=current,
            purpose="qa-export" if current else None, consumer="evidence-wiki" if current else None,
            expected_config=config if current else None)
        borrowed = captured_view(root, config)
        with (nullcontext(root) if borrowed is not None else capture.materialize()) as materialized, (
            authorized_capture(materialized, config, current) if current is not None and borrowed is None else nullcontext()
        ):
            result = Computation(materialized, capture, config, definition, inputs["selected"] if inputs else None).result(as_of, completed=completed)
        if current is not None:
            current.revalidate(root, config)
        require(capture_workspace(root).revision_id == capture.revision_id, "computation_inputs_changed")
        require(selected.producer_identity() == result["engine_id"], "computation_engine_changed")
        return result
