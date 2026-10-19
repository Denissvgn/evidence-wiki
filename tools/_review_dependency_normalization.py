"""Native fixture replay proves the exact comparison-only coordinate translation."""

from __future__ import annotations

import copy
import hashlib
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType

import yaml

from evidence_wiki.pack_discovery import owner
from tools import _review_dependency_accounting as accounting
from tools._review_dependency_workload import _computation, fixture_capture_metadata

CLOCK_CONTRACT = {
    "capture_recipe": "bundled_native_capture_bytes_then_inventory_and_normalization",
    "generated_capture_clock": "profile_retrieved_at_only_from_actual_native_provision",
    "generated_inventory_clock": "detected_at_from_actual_native_inventory",
    "generated_normalization_clock": ["created", "updated", "normalized_at"],
    "generated_computation_clock": "configured_fixed_as_of_from_actual_native_provision",
    "temporal_gate": "actual_source_age_rights_and_review_expiry_at_each_mode_current_close",
    "translation_target": "immutable_fixture_source_observed_at",
    "configuration_coordinates": ["request.payload.target.writable_root", "request.payload.authority.writable_roots", "request.payload.authority.source_scope"],
    "mode_policy_difference": "native_policy_v1_full_or_native_policy_v2_claim_dependencies_only",
    "unknown_shape": "refuse",
}


def _value(node):
    return owner("_question_dependencies").typed_value(node)


def _hash(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _date(value):
    accounting.require(type(value) in {str, date}, "accounting_recipe_date_invalid")
    return value.isoformat() if type(value) is date else value


def _record_replay(capability):
    """Reproduce all raw/provenance, full manifest and normalized bytes."""
    from evidence_wiki.source_capture_contracts import profile
    from evidence_wiki.source_capture_inputs import CaptureInput
    from evidence_wiki.source_capture_operations import provenance_profile

    capability.assert_original()
    sources = capability.native["sources"]
    accounting.require(set(sources) == {"shared", "demand", "unused"}, "accounting_recipe_source_denominator")
    files, records, fixed_files, fixed_profiles = {}, [], {}, {}
    fixture = capability.fixture
    for coordinate, source in sources.items():
        material = fixture["source_material"][coordinate]
        actual = _value(source["capture"])
        observed = actual["profile"]["retrieved_at"]
        expected_profile = profile(CaptureInput(material["raw"].encode(), fixture_capture_metadata(coordinate, observed)))
        accounting.require(actual["profile"] == expected_profile and actual["content"] == material["raw"]
            and actual["complete"] is True and not actual["unusable_reasons"], "accounting_capture_recipe_mismatch")
        accounting.require(source["descriptor"] == {"schema_version": "evidence-source-revision/v1", "source_id": source["source_id"],
            "parents": [], "normalized_path": source["record_path"], "evidence_root": "evidence", "temporal": {}}, "accounting_source_descriptor_recipe_mismatch")
        expected_raw = "raw/papers/" + coordinate + ".md"
        original_files = {path: bytes.fromhex(raw) for path, raw in source["files"].items()}
        expected_paths = {"source-record.json", source["record_path"], "evidence/" + expected_raw, "evidence/" + expected_raw + ".provenance.yml"}
        if coordinate in {"shared", "demand"}:
            expected_paths.add(str(Path(source["record_path"]).with_suffix(".structured.json")))
        accounting.require(set(original_files) == expected_paths and original_files["evidence/" + expected_raw] == material["raw"].encode(), "accounting_source_closure_recipe_mismatch")
        sidecar = yaml.safe_load(original_files["evidence/" + expected_raw + ".provenance.yml"])
        accounting.require(sidecar == provenance_profile(expected_profile), "accounting_provenance_recipe_mismatch")
        for path, raw in original_files.items():
            if path == "source-record.json":
                continue
            relative = path.removeprefix("evidence/")
            accounting.require(relative not in files or files[relative] == raw, "accounting_recipe_path_collision")
            files[relative] = raw
        records.append(_value(source["manifest"]))
        fixed_time = material["provenance"]["observed_at"]
        fixed_profile = profile(CaptureInput(material["raw"].encode(), fixture_capture_metadata(coordinate, fixed_time)))
        fixed_profiles[coordinate] = fixed_profile
        fixed_files[expected_raw] = material["raw"].encode()
        fixed_files[expected_raw + ".provenance.yml"] = yaml.safe_dump(provenance_profile(fixed_profile), sort_keys=False).encode()
    directories = {"."}
    for path in files:
        directories.update(str(parent) for parent in Path(path).parents)
    revision = owner("_evidence_revision").WorkspaceRevision(MappingProxyType(files), tuple(sorted(directories)))
    config = capability.native["config"]
    native_inventory = owner("source_inventory")
    normalizer = owner("normalize_sources")
    with revision.materialize() as root:
        rebuilt, warnings, _summary = native_inventory._build_records_unlocked(root, config,
            {row["id"]: row["detected_at"] for row in records}, observed_utc=records[0]["detected_at"])
        accounting.require(rebuilt == sorted(records, key=lambda row: row["id"]) and not warnings, "accounting_inventory_recipe_mismatch")
        for coordinate, source in sources.items():
            record = next(row for row in rebuilt if row["id"] == source["source_id"])
            metadata = _value(source["metadata"])
            normalized = owner("_host_capture_normalization").normalize(root, copy.deepcopy(record))
            frontmatter = normalizer.frontmatter_for(normalized, config["sources"]["manifest_path"], root / source["record_path"], _date(metadata["updated"]),
                manifest_records=rebuilt, project_root=root, normalized_at=metadata["normalized_at"], created_date=_date(metadata["created"]), read_created_date=False)
            rendered = normalizer.render_markdown(normalized, frontmatter)
            if coordinate in {"shared", "demand"}:
                path = str(Path(source["record_path"]).with_suffix(".structured.json"))
                expected_structured = accounting.canonical({"records": [{"id": coordinate, "amount": "6" if coordinate == "shared" else "4"}]})
                accounting.require(files[path] == expected_structured, "accounting_structured_recipe_mismatch")
                _opening, front, body = rendered.split("---", 2)
                parsed = yaml.safe_load(front)
                parsed["structured_view"] = {"path": path, "content_hash": _hash(expected_structured)}
                rendered = "---\n" + yaml.safe_dump(parsed, sort_keys=False) + "---" + body
            accounting.require(rendered.encode() == bytes.fromhex(source["record_bytes"]), "accounting_normalized_recipe_mismatch")
    fixed_dirs = {"."}
    for path in fixed_files:
        fixed_dirs.update(str(parent) for parent in Path(path).parents)
    fixed_revision = owner("_evidence_revision").WorkspaceRevision(MappingProxyType(fixed_files), tuple(sorted(fixed_dirs)))
    fixed_time = fixture["source_material"]["shared"]["provenance"]["observed_at"]
    accounting.require(all(row["provenance"]["observed_at"] == fixed_time for row in fixture["source_material"].values()), "accounting_fixture_clock_unsupported")
    fixed_sources = {}
    with fixed_revision.materialize() as root:
        normalized_records, warnings, _summary = native_inventory._build_records_unlocked(root, config, {}, observed_utc=fixed_time)
        accounting.require(not warnings, "accounting_fixed_recipe_unsupported")
        for coordinate in sources:
            record = copy.deepcopy(next(row for row in normalized_records if row["raw_paths"] == ["raw/papers/" + coordinate + ".md"]))
            record["id"] = coordinate
            normalized_path = "sources/normalized/" + coordinate + ".md"
            normalized = owner("_host_capture_normalization").normalize(root, copy.deepcopy(record))
            frontmatter = normalizer.frontmatter_for(normalized, config["sources"]["manifest_path"], root / normalized_path, fixed_time[:10],
                manifest_records=normalized_records, project_root=root, normalized_at=fixed_time, created_date=fixed_time[:10], read_created_date=False)
            rendered = normalizer.render_markdown(normalized, frontmatter)
            artifacts = {"evidence/" + path: data for path, data in fixed_files.items() if path.startswith("raw/papers/" + coordinate + ".md")}
            if coordinate in {"shared", "demand"}:
                structured_path = str(Path(normalized_path).with_suffix(".structured.json"))
                structured = accounting.canonical({"records": [{"id": coordinate, "amount": "6" if coordinate == "shared" else "4"}]})
                artifacts[structured_path] = structured
                _opening, front, body = rendered.split("---", 2)
                parsed = yaml.safe_load(front)
                parsed["structured_view"] = {"path": structured_path, "content_hash": _hash(structured)}
                rendered = "---\n" + yaml.safe_dump(parsed, sort_keys=False) + "---" + body
            artifacts[normalized_path] = rendered.encode()
            artifacts["source-record.json"] = accounting.canonical({"schema_version": "evidence-source-revision/v1", "source_id": coordinate,
                "parents": [], "normalized_path": normalized_path, "evidence_root": "evidence", "temporal": {}})
            fixed_sources[coordinate] = {"manifest": record, "revision": owner("_record_artifacts").closure_identity(artifacts),
                "files": {path: _hash(data) for path, data in artifacts.items()}}
    return fixed_sources


def _requirements(capability):
    value = _value(capability.native["requirements"])
    result = copy.deepcopy(value)
    payload = result["request"]["payload"]
    root = capability.root
    accounting.require(payload["target"] == {"writable_root": str(root.parent), "relative_path": "workspace"}
        and payload["authority"]["writable_roots"] == [str(root.parent)] and payload["authority"]["source_scope"] == [str(root / "raw")],
        "accounting_requirement_coordinate_unsupported")
    payload["target"]["writable_root"] = "/fixture"
    payload["authority"]["writable_roots"] = ["/fixture"]
    payload["authority"]["source_scope"] = ["/fixture/workspace/raw"]
    return result


def _configuration(capability, definition):
    value = copy.deepcopy(capability.native["config"])
    actual = value["strict_evidence"]
    expected = copy.deepcopy(capability.fixture["global_inputs"]["policy"])
    expected["assurance"] = "artifact_checked"
    expected["instructions"] = actual["instructions"]
    if capability.plan["mode"] == "selective_reuse":
        expected.update(schema_version="evidence-strict-policy/v2", review_basis_mode="claim_dependencies_v1")
    accounting.require(actual == expected, "accounting_effective_policy_recipe_mismatch")
    actual.pop("review_basis_mode", None)
    actual["schema_version"] = "evidence-strict-policy/v1"
    actual["instructions"]["docs/research-requirements.json"] = _hash(accounting.canonical(_requirements(capability)))
    value["computation"] = definition
    return value


def _computation_replay(capability, fixed_records, translations):
    actual = capability.native["computation"]
    owner("_computation_contract").validate_result(actual)
    paths = {key: row["record_path"] for key, row in capability.native["sources"].items()}
    clock = actual["definition"]["clock"]["as_of"]
    expected = _computation(paths, clock)
    accounting.require(actual["definition"] == expected and actual["definition"] == capability.native["config"]["computation"]
        and actual["status"] == "passed" and actual["invariants"] == [] and actual["findings"] == []
        and actual["clock"]["schedules"] == [] and actual["definition"]["tables"] == {}, "accounting_computation_recipe_unsupported")
    fixed_time = capability.fixture["source_material"]["shared"]["provenance"]["observed_at"]
    fixed_paths = {coordinate: "sources/normalized/" + coordinate + ".md" for coordinate in paths}
    definition = _computation(fixed_paths, fixed_time)
    definition_id = owner("_computation_contract").definition_id(definition)
    source_by_id = {row["source_id"]: (coordinate, row) for coordinate, row in capability.native["sources"].items()}
    normalized_sources, by_id = [], {}
    for source in actual["sources"]:
        coordinate, retained = source_by_id[source["source_id"]]
        fields = {"source_id", "record_path", "record_sha256", "structured_path", "structured_sha256", "source_revision", "raw_files", "manifest_record_id"}
        accounting.require(set(source) == fields and source["record_sha256"] == retained["record_sha256"] and source["source_revision"] == retained["revision"]
            and source["manifest_record_id"] == accounting.identity("computation-manifest-record/v1", _value(retained["manifest"])), "accounting_computation_source_mismatch")
        translation = next(row for row in translations if row["coordinate"] == coordinate)
        normalized = {**source, "source_id": coordinate, "record_path": fixed_paths[coordinate], "record_sha256": translation["fixture_record_sha256"],
            "structured_path": str(Path(fixed_paths[coordinate]).with_suffix(".structured.json")),
            "source_revision": fixed_records[coordinate]["revision"],
            "manifest_record_id": accounting.identity("computation-manifest-record/v1", fixed_records[coordinate]["manifest"])}
        normalized_sources.append(normalized)
        by_id[source["source_id"]] = normalized
    normalized_sources.sort(key=lambda row: row["source_id"])
    descriptors, ids = {}, {}
    for identifier, descriptor in actual["lineage"].items():
        if descriptor["kind"] in {"rule", "constant"}:
            accounting.require(set(descriptor) == {"kind", "definition_id", "pointer"} and descriptor["definition_id"] == actual["definition_id"], "accounting_computation_lineage_unsupported")
            normalized = {**descriptor, "definition_id": definition_id}
        else:
            accounting.require(descriptor["kind"] in {"record", "field"} and descriptor["source_id"] in by_id, "accounting_computation_lineage_unsupported")
            base = next(row for row in actual["sources"] if row["source_id"] == descriptor["source_id"])
            accounting.require(set(descriptor) == set(base) | {"kind", "pointer"} and all(descriptor[key] == value for key, value in base.items()), "accounting_computation_lineage_mismatch")
            normalized = {**by_id[descriptor["source_id"]], "kind": descriptor["kind"], "pointer": descriptor["pointer"]}
        replacement = accounting.identity("computation-contributor/v1", normalized)
        accounting.require(identifier == accounting.identity("computation-contributor/v1", descriptor) and replacement not in descriptors, "accounting_computation_contributor_mismatch")
        ids[identifier], descriptors[replacement] = replacement, normalized
    def values(value):
        if type(value) is list:
            return [values(child) for child in value]
        if type(value) is not dict:
            return value
        output = {}
        for key, child in value.items():
            if key in {"lineage", "record_ids"}:
                accounting.require(type(child) is list and all(identifier in ids for identifier in child), "accounting_computation_reference_unknown")
                output[key] = sorted(ids[identifier] for identifier in child)
            elif key == "record_id":
                accounting.require(child in ids, "accounting_computation_reference_unknown")
                output[key] = ids[child]
            else:
                output[key] = values(child)
        return output
    result = copy.deepcopy(actual)
    result.update(definition=definition, definition_id=definition_id,
        configuration_id=owner("_computation_contract").configuration_id(_configuration(capability, definition)), sources=normalized_sources,
        input_id=accounting.identity("computation-input/v1", normalized_sources), lineage=descriptors,
        records=sorted([{**row, "id": ids[row["id"]], "source_id": source_by_id[row["source_id"]][0]} for row in actual["records"]], key=lambda row: row["id"]),
        aggregations=values(actual["aggregations"]), graphs=values(actual["graphs"]), clock={**actual["clock"], "as_of": datetime.fromisoformat(fixed_time.replace("Z", "+00:00")).isoformat()})
    result.pop("result_id")
    result["result_id"] = accounting.identity("evidence-computation-result/v1", result)
    owner("_computation_contract").validate_result(result)
    return result


def _questions(capability, translations):
    sources = {row["native_source_id"]: row["fixture_source_id"] for row in translations}
    questions = copy.deepcopy(capability.native["questions"])
    for question in questions:
        mirrors = question["canonical_mirrors"]
        mirrors["source_ids"] = [sources[source] for source in mirrors["source_ids"]]
        for entry in mirrors["grounding"]:
            accounting.require(entry["source_id"] in sources, "accounting_question_source_unknown")
            entry["source_id"] = sources[entry["source_id"]]
    return questions


def compare_completed(legacy, selective, pair, originals):
    """Require a fresh pair close before translating historical execution data."""
    from tools._review_dependency_pair_closing import require_current_pair_closing
    from tools._review_dependency_runner import _CompletedMode, _mode_members, _read
    accounting.require(type(legacy) is _CompletedMode and type(selective) is _CompletedMode
        and legacy in _mode_members() and selective in _mode_members() and legacy is not selective,
        "accounting_completed_mode_owner_required")
    legacy.assert_original()
    selective.assert_original()
    accounting.validate_pair(pair)
    accounting.require(legacy.plan["mode"] == "legacy_full" and selective.plan["mode"] == "selective_reuse"
        and legacy.plan["pair_spec_id"] == selective.plan["pair_spec_id"] == accounting.document_id(pair)
        and legacy.plan["candidate_sha256"] == selective.plan["candidate_sha256"] == pair["candidate_sha256"]
        and legacy.plan["review_producer_id"] == selective.plan["review_producer_id"]
        and legacy.fixture == selective.fixture, "accounting_completed_pair_binding_invalid")
    accounting.require(_read(pair["clock_contract_pin"]) == CLOCK_CONTRACT
        and _read(pair["normalization_contract_pin"]) == accounting.NORMALIZATION_CONTRACT, "accounting_comparison_recipe_changed")
    return require_current_pair_closing(legacy, selective, originals,
        consume=lambda current_native: _prepare_current_normalization(legacy, selective, pair, originals, current_native))


def _prepare_current_normalization(legacy, selective, pair, originals, current_native):
    """Replay and append complete receipts inside the retained closing allocation."""
    from types import SimpleNamespace
    current_modes = []
    for capability in (legacy, selective):
        current_modes.append(SimpleNamespace(plan=capability.plan, report=capability.report,
            root=capability.root, fixture=capability.fixture,
            native=current_native[capability.plan["mode"]], assert_original=capability.assert_original))
    translations, calculations, normalized, publications, computation_results, questions = [], [], {}, {}, {}, {}
    for capability in current_modes:
        mode = capability.plan["mode"]
        fixed = _record_replay(capability)
        rows = []
        for coordinate, source in capability.native["sources"].items():
            fixture_source = capability.fixture["source_material"][coordinate]
            expected_digest = _hash(fixture_source["normalized"].encode())
            expected_clock = fixture_source["provenance"]["observed_at"]
            for claim in capability.fixture["inventory"]["claims"]:
                for evidence in claim["evidence"]:
                    if evidence["source_id"] == coordinate:
                        accounting.require(evidence["record_sha256"] == expected_digest and evidence["observed_at"] == expected_clock,
                                           "accounting_fixture_source_identity_invalid")
            observed = _value(source["capture"])["profile"]["retrieved_at"]
            accounting.require(source["usage"]["eligible"] is True and source["usage"]["complete"] is True, "accounting_source_close_ineligible")
            row = {"mode": mode, "coordinate": coordinate, "native_source_id": source["source_id"], "native_revision_id": source["revision"],
                "native_record_sha256": source["record_sha256"], "native_observed_at": observed, "fixture_source_id": coordinate,
                "fixture_record_sha256": expected_digest, "fixture_observed_at": expected_clock,
                "native_capture_pin": originals.append((mode + "-" + coordinate + "-capture", _value(source["capture"]))),
                "native_usage_deposit_pin": originals.append((mode + "-" + coordinate + "-admission", {"revision": source["revision"], "descriptor": source["descriptor"], "files": source["files"]})),
                "native_record_pin": originals.append((mode + "-" + coordinate + "-record", source)),
                "fixture_capture_recipe_pin": originals.append((mode + "-" + coordinate + "-recipe", {"fixture": fixture_source, "fixed_native": fixed[coordinate], "clock": CLOCK_CONTRACT})),
                "current_native_source_gate_pin": originals.append((mode + "-" + coordinate + "-source-gate", source["usage"]))}
            rows.append(accounting.validate("SourceTranslation", row))
        rows.sort(key=lambda row: row["coordinate"])
        translations.extend(rows)
        computed = _computation_replay(capability, fixed, rows)
        computation_results[mode] = computed
        result_ids = {item["result_id"] for claim in capability.fixture["inventory"]["claims"] for item in claim.get("calculations", [])}
        accounting.require(len(result_ids) == 1, "accounting_fixture_computation_identity_unsupported")
        calculation = {"mode": mode, "native_result_id": capability.native["computation"]["result_id"], "fixture_result_id": next(iter(result_ids)),
            "native_result_pin": originals.append((mode + "-native-computation", capability.native["computation"])),
            "fixture_definition_pin": originals.append((mode + "-fixture-definition", computed["definition"])),
            "complete_normalized_result_pin": originals.append((mode + "-normalized-computation", computed)),
            "complete_normalized_lineage_pin": originals.append((mode + "-normalized-lineage", computed["lineage"])),
            "current_native_computation_gate_pin": originals.append((mode + "-computation-gate", {"native_result_id": capability.native["computation"]["result_id"], "strict": capability.native["strict"]}))}
        calculations.append(accounting.validate("ComputationTranslation", calculation))
        claims = accounting.normalized_claims([row["claim"] for row in capability.native["strict"]["claims"]], claim_schema_version=capability.plan["claim_schema_version"], mode=mode,
                                             source_translations=rows, computation_translation=calculation)
        normalized[mode] = claims
        publications[mode] = accounting.normalized_publication(capability.native["publication"], claims)
        questions[mode] = _questions(capability, rows)
    accounting.require(computation_results["legacy_full"] == computation_results["selective_reuse"], "accounting_complete_computation_mismatch")
    accounting.require(normalized["legacy_full"] == normalized["selective_reuse"] and publications["legacy_full"] == publications["selective_reuse"]
        and questions["legacy_full"] == questions["selective_reuse"], "accounting_complete_useful_content_mismatch")
    evidence = {"schema_version": "review-dependency-differential-normalization-evidence/v2", "normalization_contract_pin": pair["normalization_contract_pin"],
        "normalization_contract_id": accounting.document_id(accounting.NORMALIZATION_CONTRACT), "pair_spec_id": accounting.document_id(pair),
        "source_translations": translations, "computation_translations": calculations}
    for capability, prefix in zip(current_modes, ("legacy", "selective"), strict=True):
        mode = capability.plan["mode"]
        evidence[prefix + "_native_strict_pin"] = originals.append((prefix + "-native-strict", capability.native["strict"]))
        evidence[prefix + "_native_publication_pin"] = originals.append((prefix + "-native-publication", capability.native["publication"]))
        evidence[prefix + "_normalized_claims_pin"] = originals.append((prefix + "-normalized-claims", normalized[mode]))
        evidence[prefix + "_normalized_publication_pin"] = originals.append((prefix + "-normalized-publication", publications[mode]))

    def mint(closing_pin, current_assert):
        evidence["current_pair_closing_pin"] = closing_pin
        return accounting._NativeNormalization(accounting._NORMALIZATION_MINT, evidence=evidence, claims=normalized, publications=publications,
            stages={cap.plan["mode"]: cap.report["stage_observations"] for cap in (legacy, selective)}, owners={cap.plan["mode"]: cap.native["closed_at"] for cap in (legacy, selective)},
            pair=pair, plans={cap.plan["mode"]: cap.plan for cap in (legacy, selective)}, reports={cap.plan["mode"]: cap.report for cap in (legacy, selective)}, current_assert=current_assert)
    return mint
