#!/usr/bin/env python3
"""Closed declarations and lossless wire data for deterministic computation."""

from __future__ import annotations

import hashlib
import json
import math
import re
import string
from datetime import date, datetime
from pathlib import PurePosixPath

import yaml
from _computation_expression import (
    FUNCTIONS,
    MAX_DIGITS,
    NAME,
    ROUNDINGS,
    ComputationInvalid,
    arithmetic_policy,
    number,
    parse,
    references,
    require,
)
from _evidence_revision import canonical_bytes, content_id
from _strict_contract import validate_shape

SCHEMA = "evidence-computation-definition/v1"
RESULT_SCHEMA = "evidence-computation-result/v1"
MAX_BYTES = 1_048_576
MAX_RECORDS = 2048
MAX_SOURCES = 128
MAX_GROUPS = 128


def identifier(value):
    require(type(value) is str and NAME.fullmatch(value) is not None, "computation_identifier_invalid")
    return value


def bounded(value):
    pending, seen = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        seen += 1
        require(depth <= 24 and seen <= 65_536, "computation_document_bound")
        require(type(item) in {dict, list, str, int, bool, type(None)}, "computation_wire_type_invalid")
        if type(item) is dict:
            require(len(item) <= 4096 and all(type(key) is str for key in item), "computation_mapping_invalid")
            pending.extend((part, depth + 1) for pair in item.items() for part in pair)
        elif type(item) is list:
            require(len(item) <= 4096, "computation_array_bound")
            pending.extend((part, depth + 1) for part in item)
        elif type(item) is str:
            require(len(item) <= MAX_BYTES and not any(0xD800 <= ord(char) <= 0xDFFF for char in item), "computation_string_invalid")
        elif type(item) is int:
            require(item.bit_length() <= 53, "computation_wire_integer_requires_decimal_string")
    require(len(canonical_bytes(value)) <= MAX_BYTES, "computation_document_bound")
    return value


def path(value):
    require(type(value) is str and 0 < len(value) <= 512 and "\\" not in value
            and ":" not in value and not value.startswith("/")
            and all(ord(c) >= 32 for c in value), "computation_path_invalid")
    parsed = PurePosixPath(value)
    require(bool(parsed.parts) and parsed.as_posix() == value and all(part not in {".", ".."} and not part.startswith(".") for part in parsed.parts),
            "computation_path_invalid")
    return value


def pointer(value):
    require(type(value) is str and len(value) <= 512 and (not value or value.startswith("/"))
            and re.search(r"~(?![01])", value) is None, "computation_pointer_invalid")
    return value


def schemas():
    def obj(**properties):
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
    def text(maximum=4096):
        return {"type": "string", "minLength": 1, "maxLength": maximum}
    def nullable(shape):
        return {"anyOf": [shape, {"type": "null"}]}
    def mapping(shape, maximum=32):
        return {"type": "object", "maxProperties": maximum, "additionalProperties": shape}
    def array(shape, maximum=128):
        return {"type": "array", "items": shape, "maxItems": maximum}
    ident = {**text(64), "pattern": r"^[A-Za-z][A-Za-z0-9_]{0,63}$(?![\s\S])"}
    decimal_string = {**text(280), "description": "Lossless finite decimal string; numeric bounds are enforced by the owner."}
    selector = obj(source_glob=text(512), records_pointer={"type": "string", "maxLength": 512},
                   row_key=nullable(text(512)), allow_empty={"type": "boolean"})
    group = {"anyOf": [text(1024), {"type": "boolean"}, {"type": "null"}, obj(decimal=decimal_string)]}
    aggregation_ref = obj(kind={"const": "aggregation"}, id=ident, metric=ident, group=nullable(array(group, 4)),
                          reduce=nullable({"enum": ["sum", "avg", "min", "max"]}))
    graph_ref = obj(kind={"const": "graph"}, id=ident, output=ident)
    reference = {"anyOf": [aggregation_ref, graph_ref]}
    metric = obj(op={"enum": ["sum", "count", "avg", "min", "max", "ratio"]}, expr=nullable(text()),
                 denominator=nullable(text()), unit=text(128))
    fixed = obj(type={"const": "fixed_date"}, at=text(40))
    elapsed = obj(type={"const": "elapsed_units"}, anchor=text(40),
                  amount={"type": "integer", "minimum": 1, "maximum": 1_000_000},
                  unit={"enum": ["seconds", "minutes", "hours", "days", "months", "years"]},
                  calendar_policy={"enum": ["clamp", "refuse"]})
    cron = obj(type={"const": "cron"}, expression=text(256), day_match={"enum": ["and", "or"]})
    condition = obj(type={"const": "condition"}, expression=text(), inputs=mapping(reference))
    declaration = obj(
        version={"const": "1.0"},
        arithmetic=obj(mode={"enum": ["exact", "rounded"]}, precision={"type": "integer", "minimum": 8, "maximum": MAX_DIGITS},
                       scale=nullable({"type": "integer", "minimum": 0, "maximum": 18}), rounding={"enum": sorted(ROUNDINGS)}),
        clock=obj(as_of=nullable(text(40)), timezone=text(128), ambiguous={"enum": ["earlier", "later", "refuse"]},
                  nonexistent={"enum": ["skip", "refuse"]}, search_days={"type": "integer", "minimum": 1, "maximum": 366}),
        tables=mapping(array(obj(lower=decimal_string, upper=nullable(decimal_string), value=decimal_string))),
        aggregations=mapping(obj(description=text(), selector=selector, filter=nullable(text()), group_by=array(text(), 4),
                                 metrics=mapping(metric), output_target=nullable(text(512)))),
        graphs=mapping(obj(description=text(), constants=mapping(obj(value=decimal_string, unit=text(128)), 128),
                           inputs=mapping(reference, 128), nodes=mapping(obj(expr=text(), unit=text(128)), 128),
                           output_mapping=mapping(ident, 128), output_page=nullable(text(512)))),
        invariants=mapping(obj(description=text(), target={"enum": ["records", "computed"]}, selector=nullable(selector),
                               inputs=mapping(reference), filter=nullable(text()), assertion=text(),
                               severity={"enum": ["error", "warning"]}, failure_message=text()), 64),
        cadence=mapping(obj(description=text(), trigger={"anyOf": [fixed, elapsed, cron, condition]},
                            lead_alerts=array(obj(amount={"type": "integer", "minimum": 0, "maximum": 1_000_000},
                                                  unit={"enum": ["seconds", "minutes", "hours", "days"]}), 16),
                            action=obj(kind={"enum": ["evaluate_graph", "check_invariants", "status_flag"]}, target=ident))),
    )
    hashed = {**text(71), "pattern": r"^sha256:[0-9a-f]{64}$"}
    boolean = {"type": "boolean"}
    count = {"type": "integer", "minimum": 0, "maximum": 200_000}
    lineage = array(text(128), 4096)
    scalar = obj(type={"enum": ["decimal", "string", "boolean", "null"]},
                 value={"anyOf": [text(), boolean, {"type": "null"}]}, formatted=nullable(text(512)),
                 rounded=boolean, lineage=lineage, unit=text(128))
    source = obj(source_id=text(512), record_path=text(512), record_sha256=hashed, structured_path=text(512),
                 structured_sha256=hashed, source_revision=nullable(hashed), raw_files=mapping(hashed, 128), manifest_record_id=hashed)
    contributor = {"anyOf": [obj(**source["properties"], kind={"enum": ["record", "field"]}, pointer={"type": "string", "maxLength": 512}),
                  obj(kind={"enum": ["constant", "table", "rule"]}, definition_id=hashed, pointer=text(512))]}
    invariant = obj(id=ident, severity={"enum": ["error", "warning"]}, checked=count, excluded=count, failures=count,
                    passed=boolean, complete=boolean)
    finding = obj(invariant_id=ident, target=text(128), severity={"enum": ["error", "warning"]}, message=text(),
                  lineage=lineage, finding_id=hashed)
    schedule = obj(id=ident, state={"enum": ["completed", "due", "overdue", "pending", "inactive", "unknown"]},
                   due_at=nullable(text(40)), next_at=nullable(text(40)),
                   lead_alerts=array(obj(amount={"type": "integer", "minimum": 0, "maximum": 1_000_000}, unit={"enum": ["seconds", "minutes", "hours", "days"]}), 16),
                   occurrence_id=nullable(hashed), action=obj(kind={"enum": ["evaluate_graph", "check_invariants", "status_flag"]}, target=ident),
                   lineage=lineage, condition=nullable(boolean), catch_up={"const": "latest-occurrence-only"})
    result = obj(schema_version={"const": RESULT_SCHEMA}, definition_id=hashed, definition=declaration, configuration_id=hashed, input_id=hashed,
                 engine_id=hashed, clock=obj(as_of=nullable(text(40)), timezone=obj(name=text(128), provider=text(128), sha256=text(64)),
                                           schedules=array(schedule, 32)), status={"enum": ["passed", "failed"]},
                 sources=array(source, MAX_SOURCES), records=array(obj(id=hashed, source_id=text(512), pointer={"type": "string", "maxLength": 512}), MAX_RECORDS),
                 aggregations=mapping(obj(groups=array(obj(key=array(group, 4), metrics=mapping(scalar), record_ids=array(hashed, MAX_RECORDS)), MAX_GROUPS),
                                          selected_count=count, included_count=count, excluded=array(obj(record_id=hashed, reason={"const": "filter_false"}, lineage=lineage), MAX_RECORDS))),
                 graphs=mapping(obj(nodes=mapping(scalar, 128), inputs=mapping(scalar, 128), outputs=mapping(scalar, 128), order=array(ident, 128))),
                 invariants=array(invariant, 64), findings=array(finding, 128), limits=array(text(), 16), operations=count,
                 lineage=mapping(contributor, 4096), result_id=hashed)
    return {key: {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "urn:" + key.replace("/", ":"),
                  "x-evidence-wiki-functions": sorted(FUNCTIONS),
                  "x-evidence-wiki-limits": {"bytes": MAX_BYTES, "records": MAX_RECORDS, "sources": MAX_SOURCES,
                                            "groups": MAX_GROUPS, "expression_nodes": 256, "operations": 200_000}, **shape}
            for key, shape in ((SCHEMA, declaration), (RESULT_SCHEMA, result))}


def expression(value, roots=None):
    parse(value)
    if roots is not None:
        require(all(ref.split(".")[0] in roots for ref in references(value)), "computation_expression_reference_unknown")


def topological(dependencies):
    require(all(set(edges) <= dependencies.keys() for edges in dependencies.values()), "computation_reference_unknown")
    pending, ordered = {key: set(value) for key, value in dependencies.items()}, []
    while pending:
        ready = sorted(key for key, edges in pending.items() if not edges)
        require(bool(ready), "computation_reference_cycle")
        for key in ready:
            ordered.append(key)
            pending.pop(key)
        for edges in pending.values():
            edges.difference_update(ready)
    return ordered


def validate_selector(value):
    candidate = path(value["source_glob"])
    parts = PurePosixPath(candidate).parts
    require(len(parts) >= 2 and not any("*" in part for part in parts[:-1])
            and re.fullmatch(r"[A-Za-z0-9_*.-]+\.md", parts[-1]) is not None
            and "**" not in candidate, "computation_selector_invalid")
    pointer(value["records_pointer"])
    if value["row_key"] is not None:
        pointer(value["row_key"])


def validate_reference(ref, definition):
    if ref["kind"] == "aggregation":
        require(ref["id"] in definition["aggregations"], "computation_reference_unknown")
        aggregation = definition["aggregations"][ref["id"]]
        require(ref["metric"] in aggregation["metrics"], "computation_reference_unknown")
        require((ref["group"] is None) != (ref["reduce"] is None), "computation_group_selection_required")
        if ref["group"] is not None:
            require(len(ref["group"]) == len(aggregation["group_by"]), "computation_group_arity")
            for part in ref["group"]:
                if type(part) is dict:
                    number(part["decimal"])
    else:
        require(ref["id"] in definition["graphs"]
                and ref["output"] in definition["graphs"][ref["id"]]["output_mapping"], "computation_reference_unknown")


def declaration(value, config=None):
    bounded(value)
    try:
        validate_shape(value, schemas()[SCHEMA])
    except ValueError:
        raise ComputationInvalid("computation_schema_invalid") from None
    arithmetic_policy(value["arithmetic"])
    for section in ("tables", "aggregations", "graphs", "invariants", "cadence"):
        for key in value[section]:
            identifier(key)
    destinations = []
    for table in value["tables"].values():
        require(bool(table), "computation_table_empty")
        previous = None
        for index, row in enumerate(table):
            lower, upper = number(row["lower"]), None if row["upper"] is None else number(row["upper"])
            number(row["value"])
            require(index == 0 or previous is not None and lower == previous, "computation_table_gap_or_overlap")
            require(upper is None and index == len(table) - 1 or upper is not None and lower < upper, "computation_table_interval_invalid")
            previous = upper
    for item in value["aggregations"].values():
        validate_selector(item["selector"])
        require(bool(item["metrics"]), "computation_metrics_empty")
        for formula in [item["filter"], *item["group_by"]]:
            if formula is not None:
                expression(formula, {"record"})
        for key, metric in item["metrics"].items():
            identifier(key)
            require((metric["expr"] is None) == (metric["op"] == "count"), "computation_metric_expression_invalid")
            require((metric["denominator"] is not None) == (metric["op"] == "ratio"), "computation_ratio_invalid")
            for formula in (metric["expr"], metric["denominator"]):
                if formula is not None:
                    expression(formula, {"record"})
        if item["output_target"] is not None:
            destinations.append(path(item["output_target"]))
    graph_edges = {}
    total_nodes = 0
    for graph_id, item in value["graphs"].items():
        sets = [set(item[key]) for key in ("constants", "inputs", "nodes")]
        names = set.union(*sets)
        require(sum(map(len, sets)) == len(names) and not names & {"record", "constants", "inputs", "nodes"}, "computation_name_collision")
        for key in names:
            identifier(key)
        for constant in item["constants"].values():
            number(constant["value"])
        for ref in item["inputs"].values():
            validate_reference(ref, value)
        graph_edges[graph_id] = {ref["id"] for ref in item["inputs"].values() if ref["kind"] == "graph"}
        node_edges = {}
        for key, node in item["nodes"].items():
            expression(node["expr"], names | {"constants", "inputs", "nodes"})
            deps = set()
            for ref in references(node["expr"]):
                parts = ref.split(".")
                if parts[0] in {"constants", "inputs", "nodes"}:
                    require(len(parts) == 2 and parts[1] in item[parts[0]], "computation_reference_unknown")
                    if parts[0] == "nodes":
                        deps.add(parts[1])
                elif parts[0] in item["nodes"]:
                    deps.add(parts[0])
            node_edges[key] = deps
        topological(node_edges)
        require(bool(item["output_mapping"]) and set(item["output_mapping"].values()) <= names, "computation_output_reference_unknown")
        for key in item["output_mapping"]:
            identifier(key)
        if item["output_page"] is not None:
            destinations.append(path(item["output_page"]))
        total_nodes += len(item["nodes"])
    require(total_nodes <= 1024, "computation_graph_bound")
    topological(graph_edges)
    for item in value["invariants"].values():
        require((item["target"] == "records") == (item["selector"] is not None), "computation_invariant_target_invalid")
        if item["selector"] is not None:
            validate_selector(item["selector"])
            require(not item["inputs"], "computation_record_inputs_invalid")
        for key, ref in item["inputs"].items():
            identifier(key)
            validate_reference(ref, value)
        roots = {"record"} if item["target"] == "records" else set(item["inputs"])
        expression(item["assertion"], roots)
        if item["filter"] is not None:
            expression(item["filter"], roots)
        try:
            parts = list(string.Formatter().parse(item["failure_message"]))
        except ValueError:
            raise ComputationInvalid("computation_message_invalid") from None
        require(len(parts) <= 64, "computation_message_bound")
        for _literal, field, spec, conversion in parts:
            if field is not None:
                require(not spec and conversion is None and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*", field) is not None,
                        "computation_message_format_forbidden")
                expression(field, roots)
    for item in value["cadence"].values():
        trigger = item["trigger"]
        if trigger["type"] == "condition":
            for key, ref in trigger["inputs"].items():
                identifier(key)
                validate_reference(ref, value)
            expression(trigger["expression"], set(trigger["inputs"]))
        action = item["action"]
        if action["kind"] != "status_flag":
            section = "graphs" if action["kind"] == "evaluate_graph" else "invariants"
            require(action["target"] in value[section], "computation_action_target_unknown")
    require(len(destinations) == len(set(destinations)), "computation_output_collision")
    if config is not None:
        normalized = path(config.get("sources", {}).get("normalized_dir", "sources/normalized"))
        wiki = path(config.get("wiki", {}).get("root", "wiki"))
        output_root = path(config.get("outputs", {}).get("default_dir", wiki + "/outputs"))
        forbidden = [normalized, config.get("sources", {}).get("cards_dir", "sources/cards"), wiki + "/questions",
                     *config.get("raw", {}).get("source_roots", ["raw"])]
        for item in [*value["aggregations"].values(), *value["invariants"].values()]:
            if item["selector"] is not None:
                require(PurePosixPath(item["selector"]["source_glob"]).parent.as_posix() == normalized, "computation_selector_outside_normalized")
        for output in destinations:
            require(output.startswith(output_root + "/") and output_root.startswith(wiki + "/")
                    and not any(output == root or output.startswith(root.rstrip("/") + "/") for root in forbidden)
                    and PurePosixPath(output).suffix in {".md", ".json"}, "computation_output_outside_derived_scope")
    return json.loads(canonical_bytes(value))


def definition_id(value):
    return content_id(SCHEMA, value)


def configuration_id(config):
    """Bind effective configuration without making YAML order or comments semantic."""
    visited = 0
    def encode(value, depth=0, ancestors=frozenset()):
        nonlocal visited
        visited += 1
        require(depth <= 32 and visited <= 65_536, "computation_configuration_bound")
        kind = type(value)
        if kind in {dict, list}:
            require(id(value) not in ancestors, "computation_configuration_cycle")
            descendants = ancestors | {id(value)}
            if kind is list:
                return ["sequence", [encode(part, depth + 1, descendants) for part in value]]
            require(all(type(key) is str for key in value), "computation_configuration_key_invalid")
            return ["mapping", [[key, encode(value[key], depth + 1, descendants)] for key in sorted(value)]]
        if kind is float:
            require(math.isfinite(value), "computation_configuration_nonfinite")
            return ["float", value.hex()]
        if kind in {datetime, date}:
            return [kind.__name__, value.isoformat()]
        if kind is bytes:
            return ["bytes", hashlib.sha256(value).hexdigest()]
        require(kind in {str, bool, int, type(None)}, "computation_configuration_type_invalid")
        return [kind.__name__, str(value) if kind is int else value]
    return content_id("computation-configuration/v1", encode(config))


def validate_result(value):
    bounded(value)
    try:
        validate_shape(value, schemas()[RESULT_SCHEMA])
    except ValueError:
        raise ComputationInvalid("computation_result_schema_invalid") from None
    require(value["result_id"] == content_id(RESULT_SCHEMA, {key: part for key, part in value.items() if key != "result_id"}),
            "computation_result_identity_invalid")
    return value


def validate_yaml(text):
    """Reject duplicate or aliased computation data before YAML construction loses it."""
    require(len(text.encode("utf-8")) <= MAX_BYTES, "computation_configuration_bound")
    try:
        tree = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        raise ComputationInvalid("computation_yaml_invalid") from None
    pending, inspected, seen_inside = [(tree, False, 0, frozenset())], 0, set()
    while pending:
        node, inside, depth, ancestors = pending.pop()
        if node is None:
            continue
        inspected += 1
        if not inside and id(node) in ancestors:
            continue
        require(inspected <= 65_536 and depth <= 32 and id(node) not in ancestors, "computation_yaml_bound")
        if inside:
            require(id(node) not in seen_inside, "computation_yaml_alias_forbidden")
            seen_inside.add(id(node))
        ancestors = ancestors | {id(node)}
        if isinstance(node, yaml.MappingNode):
            seen = set()
            for key, child in node.value:
                if inside:
                    require(isinstance(key, yaml.ScalarNode) and key.tag == "tag:yaml.org,2002:str"
                            and key.value != "<<", "computation_yaml_key_invalid")
                label = key.value if isinstance(key, yaml.ScalarNode) else None
                if inside or label == "computation":
                    require(label not in seen, "computation_yaml_duplicate_key")
                seen.add(label)
                pending.append((child, inside or label == "computation", depth + 1, ancestors))
        elif isinstance(node, yaml.SequenceNode):
            pending.extend((child, inside, depth + 1, ancestors) for child in node.value)
