"""Bounded byte evidence for native cases; serialized evidence grants no authority."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import weakref
from pathlib import Path

from tools import _review_dependency_accounting as accounting
from tools._review_dependency_case_contracts import CASE_IDS, case_manifest, case_plan
from tools._review_dependency_workload import _POPEN_TYPE, OwnedChildren, canonical, digest, retain

SCHEMA = "native-review-case-index/v1"
INDEX_BYTES = 1_048_576
DETAIL_BYTES = 16_777_216
STATUSES = {"not_executed", "passed", "passed_limited_scope", "earlier_owner_refusal", "failed"}
_MEMBERS = weakref.WeakSet()
_MINT = object()
_RUNS = weakref.WeakKeyDictionary()


def _begin_native_run(children, source, selected, fixture_sha256):
    """Issue an opaque receipt before the runner launches any owned process."""
    _require(type(children) is OwnedChildren and not children.closed and not children.owned
        and children not in _RUNS, "case_evidence_run_start_invalid")
    ticket = object()
    _RUNS[children] = {"ticket": ticket, "source": canonical(source), "selected": tuple(selected),
        "fixture": fixture_sha256, "outcomes": {}, "shards": [], "sealed": None}
    return ticket


def _record_native_outcome(children, ticket, case_id):
    state = _RUNS.get(children)
    _require(state is not None and state["ticket"] is ticket and not children.closed
        and state["sealed"] is None and case_id in state["selected"]
        and case_id not in state["outcomes"] and children.owned, "case_evidence_run_outcome_invalid")
    state["outcomes"][case_id] = tuple(children.owned)


def _record_native_shard(children, ticket, process, pin, selected):
    state = _RUNS.get(children)
    _require(state is not None and state["ticket"] is ticket and not children.closed
        and state["sealed"] is None and type(process) is _POPEN_TYPE
        and any(process is child for child in children.owned) and process.returncode == 0,
        "case_evidence_run_shard_invalid")
    state["shards"].append((process, canonical(pin), tuple(selected)))


def _seal_native_run(children, ticket, pin):
    state = _RUNS.get(children)
    _require(state is not None and state["ticket"] is ticket and children.closed
        and state["sealed"] is None, "case_evidence_run_seal_invalid")
    index = read_index(pin, roots=[Path(pin["path"]).parent])
    _require(canonical(index["source"]) == state["source"]
        and tuple(index["selected_case_ids"]) == state["selected"]
        and index["fixture_sha256"] == state["fixture"], "case_evidence_run_binding_invalid")
    if state["shards"]:
        _require(tuple(case for _, _, cases in state["shards"] for case in cases) == state["selected"]
            and tuple(process for process, _, _ in state["shards"]) == tuple(children.owned)
            and [canonical(item) for item in read_manifest(index["shards_manifest_pin"], roots=[Path(pin["path"]).parent])]
                == [item for _, item, _ in state["shards"]], "case_evidence_run_shards_invalid")
    else:
        _require(set(state["outcomes"]) == set(state["selected"])
            and all(all(any(process is owned for owned in children.owned) for process in processes)
                for processes in state["outcomes"].values()), "case_evidence_run_outcomes_incomplete")
    state["sealed"] = (canonical(pin), tuple(children.owned))


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def source_identity():
    """Observe exact candidate files and the separately frozen native worker."""
    from tools.verify_acceptance_paths import candidate_manifest
    root = Path(__file__).resolve().parents[1]
    return {"candidate_sha256": digest(candidate_manifest(root)),
        "worker_sha256": hashlib.sha256((root / "examples/review-cases/review_case_worker.py").read_bytes()).hexdigest(),
        "case_manifest_sha256": digest(case_manifest()), "case_plan_sha256": digest(case_plan())}


def _tree(value):
    pending, nodes = [(value, 1)], 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        _require(depth <= 16 and nodes <= 65_536, "case_evidence_tree_bound")
        if type(item) is dict:
            _require(len(item) <= 4096, "case_evidence_collection_bound")
            pending.extend((part, depth + 1) for pair in item.items() for part in pair)
        elif type(item) is list:
            _require(len(item) <= 4096, "case_evidence_collection_bound")
            pending.extend((part, depth + 1) for part in item)
        else:
            _require(item is None or type(item) in {str, int, float, bool}, "case_evidence_value_invalid")



def existing_pin(path, *, maximum=INDEX_BYTES):
    """Observe a new local artifact with a bounded no-follow descriptor read."""
    path = Path(path).absolute()
    _require(path.resolve(strict=True) == path, "case_evidence_path_invalid")
    before = path.lstat()
    _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= maximum,
        "case_evidence_file_bound")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        def identity(info):
            return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        _require(identity(os.fstat(descriptor)) == identity(before), "case_evidence_file_changed")
        chunks, total = [], 0
        while chunk := os.read(descriptor, min(65_536, maximum + 1 - total)):
            total += len(chunk)
            _require(total <= maximum, "case_evidence_file_bound")
            chunks.append(chunk)
        _require(total == before.st_size and identity(os.fstat(descriptor)) == identity(before), "case_evidence_file_changed")
        return {"path": str(path), "size_bytes": total, "raw_sha256": hashlib.sha256(b"".join(chunks)).hexdigest()}
    finally:
        os.close(descriptor)

def read(pin, *, roots, maximum=INDEX_BYTES):
    """Check physical bytes before strict bounded JSON interpretation."""
    raw = accounting.read_pin(pin, roots=roots, maximum=maximum)
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "case_evidence_duplicate_key")
            result[key] = value
        return result
    def constant(_value):
        raise ValueError("case_evidence_nonfinite")
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        _tree(value)
        canonical(value)
    except (RecursionError, OverflowError) as error:
        raise ValueError("case_evidence_tree_bound") from error
    return value


def _pin(value):
    return {key: value[key] for key in ("path", "raw_sha256", "size_bytes")}


def _manifest(directory, label, pins):
    _require(len(pins) <= 131_072, "case_evidence_manifest_bound")
    pages = []
    for offset in range(0, len(pins), 128):
        pages.append(retain(directory / (label + "-" + str(offset // 128) + ".json"),
            {"schema_version": "native-review-pin-page/v1", "pins": [_pin(pin) for pin in pins[offset:offset + 128]]}))
    _require(len(pages) <= 1024, "case_evidence_manifest_bound")
    return retain(directory / (label + ".json"), {"schema_version": "native-review-pin-manifest/v1",
        "pin_count": len(pins), "pages": pages})


def freeze_bindings(bindings):
    """Read supplied frozen recipe bytes before native execution starts."""
    value = {} if bindings is None else bindings
    _require(type(value) is dict and set(value) <= {"frozen_manifest_pin", "prospective_manifest_pin", "recipes_pin"},
        "case_evidence_bindings_invalid")
    for supplied in value.values():
        read(supplied, roots=[Path(supplied["path"]).parent])
    return json.loads(canonical(value))

def write_index(directory, report, *, source_before, source_after, refresh, bindings=None):
    """Write a thin inventory; detail and original bytes remain separately pinned."""
    directory = Path(directory)
    directory.mkdir(mode=0o700)
    _require(source_before == source_after, "case_evidence_source_changed")
    rows = []
    frozen_rows = {row["case_id"]: row for row in case_manifest()}
    for row in report["cases"]:
        detail = retain(directory / (row["case_id"] + ".json"),
            {key: value for key, value in row.items() if key not in {"assertions", "evidence"}})
        assertions = retain(directory / (row["case_id"] + "-assertions.json"), row["assertions"])
        evidence = retain(directory / (row["case_id"] + "-evidence.json"), row["evidence"])
        rows.append({"case_id": row["case_id"], "status": row["status"],
            "native_assertion_scope": row["native_assertion_scope"],
            "frozen_assertion_scope": frozen_rows[row["case_id"]]["native_assertion_scope"], "detail_pin": detail,
            "assertions_pin": assertions, "evidence_pin": evidence})
    index = {"schema_version": SCHEMA, "qualification": False, "authority": "byte_evidence_only",
        "source": source_before, "source_after": source_after, "fixture_sha256": report["fixture_sha256"],
        "bindings": bindings or {}, "mode": report["mode"], "refresh": refresh,
        "inventory_count": 99, "selected_case_ids": report["selected_case_ids"],
        "coverage": "all_selected" if set(report["selected_case_ids"]) == set(CASE_IDS) else "partial_inventory",
        "status": report["status"], "cases": rows,
        "originals_manifest_pin": _manifest(directory, "originals", report["originals"]),
        "shards_manifest_pin": _manifest(directory, "shards", [shard["report"] for shard in report.get("shards", [])]),
        "cleanup_pin": retain(directory / "cleanup.json",
            {key: value for key, value in report["cleanup"].items() if key != "shards"}
            if type(report["cleanup"]) is dict else report["cleanup"]),
        "failure_pin": retain(directory / "failure.json", report.get("failure")),
        "watchdog": report["watchdog"]}
    if "extended_selection" in report:
        from tools._review_dependency_case_budget import (
            COLLECTION_INDEX,
            INDEX,
            LONG_INDEX,
            LONG_SELECTION,
            OWNER_INDEX,
            OWNER_SELECTION,
            collection_selected,
        )
        index.update(schema_version=OWNER_INDEX if report["extended_selection"]["schema_version"] == OWNER_SELECTION else LONG_INDEX if report["extended_selection"]["schema_version"] in {LONG_SELECTION, OWNER_SELECTION} else COLLECTION_INDEX if collection_selected(report["extended_selection"]) else INDEX, extended_selection=report["extended_selection"])
    validate_index(index)
    for supplied in index["bindings"].values():
        read(supplied, roots=[Path(supplied["path"]).parent])
    _require(len(canonical(index)) <= INDEX_BYTES, "case_evidence_index_bound")
    return retain(directory / "index.json", index)


def validate_index(value, *, candidate_sha256=None):
    """Validate an inert index, never promote caller DTOs to native execution."""
    fields = {"schema_version", "qualification", "authority", "source", "source_after", "fixture_sha256", "bindings",
        "mode", "refresh", "inventory_count", "selected_case_ids", "coverage", "status", "cases",
        "originals_manifest_pin", "shards_manifest_pin", "cleanup_pin", "failure_pin", "watchdog"}
    from tools._review_dependency_case_budget import (
        COLLECTION_INDEX,
        INDEX,
        LONG_INDEX,
        LONG_SELECTION,
        OWNER_INDEX,
        OWNER_SELECTION,
    )
    extended = type(value) is dict and value.get("schema_version") in {INDEX, COLLECTION_INDEX, LONG_INDEX, OWNER_INDEX}
    if extended:
        fields.add("extended_selection")
    _require(type(value) is dict and set(value) == fields and value["schema_version"] in ({INDEX, COLLECTION_INDEX, LONG_INDEX, OWNER_INDEX} if extended else {SCHEMA})
        and value["qualification"] is False and value["authority"] == "byte_evidence_only", "case_evidence_index_invalid")
    _tree(value)
    _require(len(canonical(value)) <= INDEX_BYTES, "case_evidence_index_bound")
    source = value["source"]
    _require(type(source) is dict and set(source) == {"candidate_sha256", "worker_sha256", "case_manifest_sha256", "case_plan_sha256"}
        and source == value["source_after"] and all(type(part) is str and len(part) == 64
        and all(char in "0123456789abcdef" for char in part) for part in source.values()), "case_evidence_source_invalid")
    _require(source["case_manifest_sha256"] == digest(case_manifest()) and source["case_plan_sha256"] == digest(case_plan()), "case_evidence_recipe_changed")
    _require(candidate_sha256 is None or source["candidate_sha256"] == candidate_sha256, "case_evidence_candidate_changed")
    selected = value["selected_case_ids"]
    _require(type(selected) is list and selected and all(type(case) is str for case in selected)
        and len(selected) == len(set(selected)) and set(selected) <= set(CASE_IDS), "case_evidence_selection_invalid")
    _require(value["inventory_count"] == 99 and value["coverage"] == ("all_selected" if set(selected) == set(CASE_IDS) else "partial_inventory")
        and value["mode"] in {"legacy_full", "selective_reuse"} and type(value["refresh"]) is bool, "case_evidence_scope_invalid")
    _require(type(value["bindings"]) is dict and set(value["bindings"]) <= {"frozen_manifest_pin", "prospective_manifest_pin", "recipes_pin"}, "case_evidence_bindings_invalid")
    for pin in value["bindings"].values():
        accounting.validate("Pin", pin)
    _require(type(value["fixture_sha256"]) is str and len(value["fixture_sha256"]) == 64
        and all(char in "0123456789abcdef" for char in value["fixture_sha256"]), "case_evidence_fixture_invalid")
    _require(value["status"] in {"started", "failed", "completed", "completed_with_limits", "incomplete"}, "case_evidence_status_invalid")
    rows = value["cases"]
    _require(type(rows) is list and len(rows) == 99, "case_evidence_inventory_invalid")
    for row, frozen in zip(rows, case_manifest(), strict=True):
        _require(type(row) is dict and set(row) == {"case_id", "status", "native_assertion_scope", "frozen_assertion_scope", "detail_pin", "assertions_pin", "evidence_pin"}
            and row["case_id"] == frozen["case_id"] and row["frozen_assertion_scope"] == frozen["native_assertion_scope"]
            and (row["native_assertion_scope"] == frozen["native_assertion_scope"]
                or (row["case_id"] == "dependency-basis-byte-bound"
                    and row["native_assertion_scope"] == "physical_workspace_earlier_native_owner_refusal"))
            and row["status"] in STATUSES and (row["case_id"] in selected or row["status"] == "not_executed"), "case_evidence_case_invalid")
        for name in ("detail_pin", "assertions_pin", "evidence_pin"):
            accounting.validate("Pin", row[name])
    if value["status"] in {"completed", "completed_with_limits"}:
        outcomes = {row["status"] for row in rows if row["case_id"] in selected}
        _require(outcomes <= {"passed", "passed_limited_scope", "earlier_owner_refusal"}
            and (value["status"] != "completed" or outcomes == {"passed"}), "case_evidence_completion_invalid")
    if extended:
        from tools._review_dependency_case_budget import validate_selection, watchdog
        selection = value["extended_selection"]
        from tools._review_journal_artifacts import selected as collection_selected
        _require((value["schema_version"] in {COLLECTION_INDEX, LONG_INDEX, OWNER_INDEX}) == collection_selected(selection)
            and (value["schema_version"] in {LONG_INDEX, OWNER_INDEX}) == (selection["schema_version"] in {LONG_SELECTION, OWNER_SELECTION})
            and (value["schema_version"] == OWNER_INDEX) == (selection["schema_version"] == OWNER_SELECTION), "case_evidence_extended_binding_invalid")
        validate_selection(selection)
        _require(selection["source"] == source and selection["fixture_sha256"] == value["fixture_sha256"]
            and selection["mode"] == value["mode"] and selection["refresh"] == value["refresh"]
            and set(selected) <= set(selection["selected_case_ids"]), "case_evidence_extended_binding_invalid")
        if value["schema_version"] in {LONG_INDEX, OWNER_INDEX}:
            from tools._review_control_ceiling import exact
            _require(exact(value["watchdog"], watchdog(selection, selected)), "case_evidence_watchdog_binding_invalid")
        _require(value["watchdog"] == watchdog(selection, selected), "case_evidence_watchdog_binding_invalid")
    else:
        budget = value["watchdog"]
        _require(type(budget) is dict and set(budget) <= {"fixture_reservations", "fixture_run_seconds", "fixture_overhead_seconds",
            "cleanup_seconds", "outer_overhead_seconds", "watchdog_seconds", "authority_renewal", "run_budget_extension", "shard_count"}
            and {"fixture_reservations", "fixture_run_seconds", "fixture_overhead_seconds", "cleanup_seconds", "outer_overhead_seconds",
                 "watchdog_seconds", "authority_renewal", "run_budget_extension"} <= set(budget)
            and budget["fixture_run_seconds"] == 5220 and budget["fixture_overhead_seconds"] == 600
            and budget["cleanup_seconds"] == 60 and budget["outer_overhead_seconds"] == 600
            and budget["authority_renewal"] == "forbidden" and budget["run_budget_extension"] == "forbidden"
            and type(budget["fixture_reservations"]) is int and 1 <= budget["fixture_reservations"] <= 112
            and type(budget["watchdog_seconds"]) is int and 5880 <= budget["watchdog_seconds"] <= 700_000,
            "case_evidence_watchdog_invalid")
        from tools._review_dependency_cases import case_watchdog
        expected_budget = case_watchdog(selected)
        if len(selected) > 8:
            budgets = [case_watchdog(selected[offset:offset + 8]) for offset in range(0, len(selected), 8)]
            expected_budget.update(fixture_reservations=sum(item["fixture_reservations"] for item in budgets),
                watchdog_seconds=sum(item["watchdog_seconds"] for item in budgets) + 600, shard_count=len(budgets))
        _require(budget == expected_budget, "case_evidence_watchdog_binding_invalid")
    for field in ("originals_manifest_pin", "shards_manifest_pin", "cleanup_pin", "failure_pin"):
        accounting.validate("Pin", value[field])
    return value


def read_index(pin, *, roots, candidate_sha256=None):
    """Read only bounded index bytes; no execution credit is conveyed."""
    return validate_index(read(pin, roots=roots), candidate_sha256=candidate_sha256)


def read_case(row, *, roots):
    """Read one native outcome, keeping large evidence outside the thin index."""
    detail = read(row["detail_pin"], roots=roots)
    _require(type(detail) is dict and all(detail[name] == row[name]
        for name in ("case_id", "status", "native_assertion_scope")), "case_evidence_detail_binding_invalid")
    assertions = read(row["assertions_pin"], roots=roots)
    evidence = read(row["evidence_pin"], roots=roots, maximum=DETAIL_BYTES)
    _require(type(assertions) is list and all(type(item) is str for item in assertions)
        and type(evidence) is list, "case_evidence_detail_invalid")
    return dict(detail, assertions=assertions, evidence=evidence)


def read_manifest(pin, *, roots):
    """Read a finite paged pin inventory without importing original DTOs."""
    value = read(pin, roots=roots)
    _require(type(value) is dict and set(value) == {"schema_version", "pin_count", "pages"}
        and value["schema_version"] == "native-review-pin-manifest/v1"
        and type(value["pin_count"]) is int and 0 <= value["pin_count"] <= 131_072
        and type(value["pages"]) is list and len(value["pages"]) <= 1024, "case_evidence_manifest_invalid")
    pins = []
    for page_pin in value["pages"]:
        page = read(page_pin, roots=roots)
        _require(type(page) is dict and set(page) == {"schema_version", "pins"}
            and page["schema_version"] == "native-review-pin-page/v1"
            and type(page["pins"]) is list and 1 <= len(page["pins"]) <= 128, "case_evidence_page_invalid")
        for original in page["pins"]:
            accounting.validate("Pin", original)
        pins.extend(page["pins"])
    _require(len(pins) == value["pin_count"], "case_evidence_manifest_count_invalid")
    return pins


class _LiveCaseRun:
    def __init__(self, token, pin, source):
        _require(token is _MINT, "case_evidence_live_mint_refused")
        self.pin, self.source = dict(pin), dict(source)
        self._original = canonical({"pin": self.pin, "source": self.source})
        _MEMBERS.add(self)

    def assert_original(self):
        _require(self in _MEMBERS and self._original == canonical({"pin": self.pin, "source": self.source}), "case_evidence_live_changed")


def _completed_run(pin, children, source_before, source_after):
    """Called by the live runner after its actual owned native process closure."""
    _require(type(children) is OwnedChildren and children.closed and children.owned
        and source_before == source_after and all(type(process) is _POPEN_TYPE and process.returncode is not None for process in children.owned), "case_evidence_live_custody_invalid")
    state = _RUNS.get(children)
    _require(state is not None and state["sealed"] == (canonical(pin), tuple(children.owned))
        and state["source"] == canonical(source_before), "case_evidence_live_execution_binding_invalid")
    index = read_index(pin, roots=[Path(pin["path"]).parent], candidate_sha256=source_before["candidate_sha256"])
    cleanup = read(index["cleanup_pin"], roots=[Path(pin["path"]).parent], maximum=DETAIL_BYTES)
    _require(cleanup["status"] == "confirmed" and cleanup["plan_sha256"] == children.plan_sha256
        and len(cleanup["children"]) == len(children.owned)
        and all(row["leader_reaped"] is True and row["group_absent"] is True for row in cleanup["children"])
        and index["status"] in {"completed", "completed_with_limits"}, "case_evidence_live_completion_invalid")
    _verify_native_retention(index, roots=[Path(pin["path"]).parent.parent], source=source_before)
    return _LiveCaseRun(_MINT, pin, source_before)


def _verify_native_retention(index, *, roots, source, depth=0):
    """Check frozen worker receipts along the actual run's retained shard tree."""
    _require(depth <= 1, "case_evidence_shard_depth_invalid")
    cleanup = read(index["cleanup_pin"], roots=roots, maximum=DETAIL_BYTES)
    _require(type(cleanup) is dict and set(cleanup) == {"status", "children", "plan_sha256"}
        and cleanup["status"] == "confirmed" and type(cleanup["children"]) is list
        and all(type(row) is dict and row.get("leader_reaped") is True
            and row.get("group_absent") is True for row in cleanup["children"]),
        "case_evidence_retained_cleanup_invalid")
    workers, spawns = 0, []
    for original in read_manifest(index["originals_manifest_pin"], roots=roots):
        if re.fullmatch(r"\d+-child-\d+-spawn\.json", Path(original["path"]).name):
            spawn = read(original, roots=roots)
            _require(type(spawn) is dict and spawn.get("plan_sha256") == cleanup["plan_sha256"]
                and type(spawn.get("pid")) is int and spawn["pid"] > 0
                and spawn.get("pgid") == spawn["pid"], "case_evidence_retained_spawn_invalid")
            spawns.append(spawn)
        if "native-case-worker-manifest" in Path(original["path"]).name:
            worker = read(original, roots=roots)
            _require(worker["worker_sha256"] == source["worker_sha256"]
                and read(worker["case_plan"], roots=roots) == case_plan(), "case_evidence_worker_changed")
            workers += 1
    _require(spawns and [row.get("pid") for row in cleanup["children"]] == [row["pid"] for row in spawns],
        "case_evidence_retained_cleanup_binding_invalid")
    shards = read_manifest(index["shards_manifest_pin"], roots=roots)
    from tools._review_dependency_case_budget import COLLECTION_INDEX, INDEX, LONG_INDEX, OWNER_INDEX
    extended = index["schema_version"] in {INDEX, COLLECTION_INDEX, LONG_INDEX, OWNER_INDEX}
    _require(len(shards) <= (99 if extended else 13), "case_evidence_shard_bound")
    if extended and shards:
        _require(depth == 0 and len(shards) == len(index["selected_case_ids"]), "case_evidence_shard_bound")
    selected = []
    for shard_pin in shards:
        shard = read_index(shard_pin, roots=roots, candidate_sha256=source["candidate_sha256"])
        _require(shard["source"] == source and shard["fixture_sha256"] == index["fixture_sha256"]
            and shard["mode"] == index["mode"] and shard["refresh"] == index["refresh"]
            and shard["status"] in {"completed", "completed_with_limits"}, "case_evidence_shard_binding_invalid")
        if extended:
            _require(shard["schema_version"] == index["schema_version"] and len(shard["selected_case_ids"]) == 1
                and shard["extended_selection"] == index["extended_selection"], "case_evidence_shard_binding_invalid")
        _verify_native_retention(shard, roots=roots, source=source, depth=depth + 1)
        selected.extend(shard["selected_case_ids"])
    if shards:
        _require(selected == index["selected_case_ids"], "case_evidence_shard_selection_invalid")
    else:
        _require(workers >= 1, "case_evidence_native_worker_missing")
    for row in index["cases"]:
        if row["case_id"] in index["selected_case_ids"]:
            read_case(row, roots=roots)
