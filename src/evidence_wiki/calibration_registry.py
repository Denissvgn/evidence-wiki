"""Explicit frozen calibration selection and pure current eligibility checks."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .agent_execution import ExecutionError, _json, _ref, _utc
from .finite_domain import DESCRIPTOR, DOMAIN
from .mutation_contracts import detached
from .qualification_calibration import CalibrationAuthority, calibration_suite, digest, hash_ref
from .reference_oracles import assess_references, program_identity


@dataclass(frozen=True, init=False)
class CalibrationSelection:
    """Pin actual protected references and effective configuration per reviewer."""

    config_ref: str
    _document_bytes: bytes = field(repr=False)

    def __init__(self, config_ref, suite, *, model_revision):
        _ref(config_ref)
        _ref(model_revision)
        suite = detached(suite)
        expected = calibration_suite(suite["cases"], **{key: suite[key] for key in
            ("suite_ref", "split_ref", "domains", "rubric", "model_settings", "drift_ref")})
        if suite != expected:
            raise ExecutionError("calibration_suite_changed")
        if suite.get("domains") != [DOMAIN]:
            raise ExecutionError("calibration_domain_unsupported")
        assessment = assess_references(suite["cases"])
        value = {"schema_version": "research-calibration-selection/v1", "config_ref": config_ref,
            "scorer_source_sha256": hashlib.sha256(Path(__file__).with_name("qualification_calibration.py").read_bytes()).hexdigest(),
            "classifier_source_sha256": hashlib.sha256(Path(__file__).with_name("finite_domain.py").read_bytes()).hexdigest(),
            "model_revision": model_revision, "model_settings": suite["model_settings"],
            "rubric": suite["rubric"], "rubric_sha256": digest(suite["rubric"]), "suite_sha256": suite["suite_sha256"],
            "split_ref": suite["split_ref"], "drift_ref": suite["drift_ref"],
            "domain_descriptor": DESCRIPTOR, "reference_assessment": assessment}
        hash_ref(value["suite_sha256"])
        object.__setattr__(self, "config_ref", config_ref)
        object.__setattr__(self, "_document_bytes", _json(value))

    def document(self):
        return json.loads(self._document_bytes)

    @property
    def sha256(self):
        return hashlib.sha256(self._document_bytes).hexdigest()


@dataclass(frozen=True)
class CalibrationPlan:
    """Complete explicit selection; never promote a legacy qualification."""

    selections: tuple[CalibrationSelection, ...]
    probe_seconds: float = 5.0
    probe_lifetime_seconds: int = 30

    def __post_init__(self):
        if (type(self.selections) not in (list, tuple) or not 1 <= len(self.selections) <= 8
                or any(type(row) is not CalibrationSelection for row in self.selections)
                or len({row.config_ref for row in self.selections}) != len(self.selections)
                or type(self.probe_seconds) not in (int, float) or not 0 < self.probe_seconds <= 30
                or type(self.probe_lifetime_seconds) is not int or not 1 <= self.probe_lifetime_seconds <= 30):
            raise ExecutionError("calibration_plan_invalid")
        object.__setattr__(self, "selections", tuple(sorted(self.selections, key=lambda row: row.config_ref)))

    def selection(self, config_ref):
        for row in self.selections:
            if row.config_ref == config_ref:
                return row
        raise ExecutionError("calibration_selection_unavailable")

    def document(self):
        return {"schema_version": "research-calibration-plan/v1",
            "selections": [row.document() for row in self.selections],
            "probe_seconds": self.probe_seconds, "probe_lifetime_seconds": self.probe_lifetime_seconds}


@dataclass(frozen=True, init=False)
class CalibratedEvaluators:
    """Protected scored records; current domain and host status are separate."""

    authority: CalibrationAuthority = field(repr=False)
    _records: bytes = field(repr=False)
    _legacy: object | None = field(default=None, repr=False)

    def __init__(self, records, authority, *, legacy=None):
        from .agent_qualification import QualifiedEvaluators
        if type(authority) is not CalibrationAuthority or type(records) not in (list, tuple) or len(records) > 9:
            raise ExecutionError("calibration_registry_invalid")
        if legacy is not None and (type(legacy) is not QualifiedEvaluators
                or legacy.authority.generator_controller_ref != authority.generator_controller_ref):
            raise ExecutionError("calibration_legacy_owner_invalid")
        verified = [authority.verify(row) for row in records]
        readability = [row for row in verified if row["binding"].get("qualification_purpose") == "readability"]
        if len(readability) > 1 or len(verified) - len(readability) > 8:
            raise ExecutionError("calibration_registry_invalid")
        if legacy is not None and (len(verified) != 1 or len(readability) != 1):
            raise ExecutionError("calibration_legacy_purpose_invalid")
        if len({row["binding"]["config_ref"] for row in verified}) != len(verified):
            raise ExecutionError("calibration_registry_duplicate")
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "_records", _json(verified))
        object.__setattr__(self, "_legacy", legacy)

    @property
    def sha256(self):
        value = {"records_sha256": hashlib.sha256(self._records).hexdigest(),
            "issuer_ref": self.authority.issuer_ref, "generator_controller_ref": self.authority.generator_controller_ref,
            "authority_key_sha256": hashlib.sha256(self.authority._key).hexdigest()}
        if self._legacy is not None:
            value["legacy_owner_sha256"] = self._legacy.sha256
        return digest(value)

    def observations(self, policy, profile, now_utc, rubric_id, *, rubric_sha256=None,
                     domain=None, probe=None, rubric=None, selected_profiles=None, auxiliary=False, **_):
        """Pure gate over a current canonical domain and retained host probe."""
        frozen = policy.to_dict()
        records = {row["binding"]["config_ref"]: self.authority.verify(row) for row in json.loads(self._records)}
        result = []
        from .review_service_policy import approved_profiles
        required_profiles = approved_profiles(frozen, profile)
        selected_profiles = required_profiles if selected_profiles is None else selected_profiles
        if self._legacy is not None:
            if profile.calibration is not None or profile.readability is None or auxiliary or selected_profiles != required_profiles:
                raise ExecutionError("calibration_legacy_scope_changed")
            profile.readability.selected_profile(policy, profile)
            return self._legacy.observations(policy, profile, now_utc, rubric_id, rubric_sha256=rubric_sha256)
        if auxiliary:
            expected = None if profile.disagreement is None else profile.disagreement.auxiliary_profile(policy, profile)
            if expected is None or selected_profiles != [expected]:
                raise ExecutionError("calibration_auxiliary_selection_invalid")
        elif selected_profiles != required_profiles:
            raise ExecutionError("calibration_required_selection_changed")
        for wanted in selected_profiles:
            row = {**wanted, "qualification_ref": None, "qualified": None, "independent": None,
                "assurance": frozen["assurance"], "reference_cases": None, "families": None,
                "critical_false_acceptances": None, "positive_acceptance": None, "meaning_preserving_flip": None}
            record = records.get(wanted["profile_ref"])
            if record is None or profile.calibration is None:
                result.append(row)
                continue
            selected = profile.calibration.selection(wanted["profile_ref"]).document()
            if (selected["reference_assessment"]["program"] != program_identity()
                    or selected["scorer_source_sha256"] != hashlib.sha256(Path(__file__).with_name("qualification_calibration.py").read_bytes()).hexdigest()
                    or selected["classifier_source_sha256"] != hashlib.sha256(Path(__file__).with_name("finite_domain.py").read_bytes()).hexdigest()):
                result.append(row)
                continue
            row.update(qualification_ref="calibration-" + record["record_ref"], independent=record["independent"])
            if (not domain or domain.get("state") != "observed" or domain.get("domain_ref") != DOMAIN
                    or not probe or probe.get("reasons")
                    or probe.get("configuration_reasons", {}).get(wanted["profile_ref"])
                    or probe.get("semantic_basis_id") != domain["semantic_basis_id"]
                    or probe.get("profile_sha256") != profile.sha256 or probe.get("registry_sha256") != self.sha256
                    or not _utc(probe["observed_utc"]) <= _utc(now_utc) < _utc(probe["expires_utc"])):
                result.append(row)
                continue
            configuration = probe.get("configurations", {}).get(wanted["profile_ref"])
            expected = {"execution_profile_sha256": profile.sha256, "config_ref": wanted["profile_ref"],
                "controller_ref": wanted["controller_ref"], "model_ref": profile.role(wanted["profile_ref"]).model_ref,
                "prompt_version": profile.role(wanted["profile_ref"]).prompt_version,
                "model_revision": selected["model_revision"], "model_settings_sha256": digest(selected["model_settings"]),
                "rubric_sha256": selected["rubric_sha256"], "suite_sha256": selected["suite_sha256"],
                "split_ref": selected["split_ref"], "reference_assessment_ref": selected["reference_assessment"]["assessment_ref"],
                "domain_descriptor_sha256": digest(selected["domain_descriptor"]), "active": True,
                "execution_kind": "model"}
            if (type(configuration) is not dict or type(configuration.get("active")) is not bool
                    or any(configuration.get(key) != value for key, value in expected.items())
                    or record["suite_sha256"] != selected["suite_sha256"] or record["split_ref"] != selected["split_ref"]
                    or record["binding"]["rubric_sha256"] != rubric_sha256
                    or record["binding"]["rubric_sha256"] != selected["rubric_sha256"]
                    or record["binding"].get("reference_assessment_ref") != selected["reference_assessment"]["assessment_ref"]
                    or record["binding"].get("model_revision") != selected["model_revision"]
                    or record["binding"].get("domain_descriptor_sha256") != digest(selected["domain_descriptor"])):
                result.append(row)
                continue
            current = self.authority.current(record, policy=policy, profile=profile, config_ref=wanted["profile_ref"],
                rubric=selected["rubric"] if rubric is None else rubric, model_settings=selected["model_settings"], domain_ref=DOMAIN, now_utc=now_utc,
                drift_ref=selected["drift_ref"], auxiliary=auxiliary)
            if current["state"] == "qualified":
                metrics = record["domains"][DOMAIN]
                row.update(qualified=True, reference_cases=metrics["counts"]["observed_cases"], families=metrics["families"],
                    critical_false_acceptances=metrics["all_references"]["counts"]["critical_false_acceptances"],
                    positive_acceptance=metrics["metrics"]["positive_acceptance"],
                    meaning_preserving_flip=metrics["axes"]["meaning"]["flip_rate"])
            result.append(row)
        return detached(result)

    def readability_observation(self, policy, profile, now_utc, *, domain, probe):
        """Distinct protected purpose over native current brief and held host probe."""
        return self._readability_at(policy, profile, now_utc, domain=domain, probe=probe, configuration_utc=now_utc)

    def readability_application_observation(self, policy, profile, now_utc, *, domain, probe, checked_utc):
        """Current issuer qualification, with historical execution configuration.

        The native application owner supplies its authenticated check time.
        This reports no current remote service availability and grants no
        authority to apply, dispatch, release, or consume source material.
        """
        if _utc(checked_utc) > _utc(now_utc):
            return {"state": "unknown", "qualified": None, "independent": None,
                "qualification_ref": None, "reason": "readability_qualification_unknown"}
        return self._readability_at(policy, profile, now_utc, domain=domain, probe=probe, configuration_utc=checked_utc)

    def _readability_at(self, policy, profile, now_utc, *, domain, probe, configuration_utc):
        from .readability_reference import DOMAIN as READABILITY_DOMAIN
        from .readability_reference import program_identity as readability_program
        plan = profile.readability
        result = {"state": "unknown", "qualified": None, "independent": None,
            "qualification_ref": None, "reason": "readability_qualification_unknown"}
        if plan is None:
            return result
        wanted = plan.selected_profile(policy, profile)
        selected = plan.selection.document()
        records = [self.authority.verify(row) for row in json.loads(self._records)
            if row["binding"].get("qualification_purpose") == "readability"
            and row["binding"]["config_ref"] == plan.config_ref]
        if len(records) != 1:
            return result
        record = records[0]
        if (selected["reference_assessment"]["program"] != readability_program()
                or selected["scorer_source_sha256"] != hashlib.sha256(Path(__file__).with_name("qualification_calibration.py").read_bytes()).hexdigest()
                or selected["classifier_source_sha256"] != hashlib.sha256(Path(__file__).with_name("readability_reference.py").read_bytes()).hexdigest()
                or not domain or domain.get("state") != "observed" or domain.get("domain_ref") != READABILITY_DOMAIN
                or not probe or probe.get("reasons") or probe.get("semantic_basis_id") != domain["semantic_basis_id"]
                or probe.get("profile_sha256") != profile.sha256 or probe.get("registry_sha256") != self.sha256
                or not _utc(probe["observed_utc"]) <= _utc(configuration_utc) < _utc(probe["expires_utc"])):
            return result
        role = profile.role(plan.config_ref)
        expected = {"execution_profile_sha256": profile.sha256, "config_ref": plan.config_ref,
            "controller_ref": role.controller_ref, "model_ref": role.model_ref, "prompt_version": role.prompt_version,
            "model_revision": selected["model_revision"], "model_settings_sha256": digest(selected["model_settings"]),
            "rubric_sha256": selected["rubric_sha256"], "suite_sha256": selected["suite_sha256"], "split_ref": selected["split_ref"],
            "reference_assessment_ref": selected["reference_assessment"]["assessment_ref"],
            "domain_descriptor_sha256": digest(selected["domain_descriptor"]), "active": True, "execution_kind": "model"}
        actual = probe.get("configuration")
        if (type(actual) is not dict or type(actual.get("active")) is not bool
                or any(actual.get(key) != value for key, value in expected.items())
                or record["suite_sha256"] != selected["suite_sha256"] or record["split_ref"] != selected["split_ref"]
                or any(record["binding"].get(key) != value for key, value in
                    {"model_revision": selected["model_revision"], "reference_assessment_ref": selected["reference_assessment"]["assessment_ref"],
                     "domain_descriptor_sha256": digest(selected["domain_descriptor"])}.items())):
            return result
        current = self.authority.current(record, policy=policy, profile=profile, config_ref=plan.config_ref,
            rubric=selected["rubric"], model_settings=selected["model_settings"], domain_ref=READABILITY_DOMAIN,
            now_utc=now_utc, drift_ref=selected["drift_ref"], readability=True)
        if current["state"] == "qualified" and record["independent"] is True and wanted["controller_ref"] == role.controller_ref:
            result.update(state="qualified", qualified=True, independent=True,
                qualification_ref="calibration-" + record["record_ref"], reason="current_readability_reference_purpose")
        return detached(result)
