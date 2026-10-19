"""Protected reference scoring and parent-owned strict review attestations.

Execution receipts authenticate observations, not their correctness. The host
keeps reference labels separate from worker inputs and computes every metric.
Qualifications apply only to the frozen policy, execution identity and rubric;
they do not establish general model reliability or OS-level controller isolation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .agent_execution import ExecutionAttempt, ExecutionProfile, _digest, _json, _ref, _utc, validate_observation
from .run_policy import RunPolicy

QUALIFICATION = "research-agent-qualification/v1"
_CASE_FIELDS = {"case_id", "family_id", "input_document", "expected_label", "critical", "pair_id"}
_SUITE_FIELDS = {"suite_ref", "split_ref", "domain_ref", "rubric", "suite_sha256", "split_sha256", "domain_sha256"}
_COUNTS = {"reference_cases", "families", "critical_false_acceptances", "positive_cases",
           "positive_acceptances", "paired_cases", "paired_flips", "expected_cases", "unknown_cases"}
_RECORD_FIELDS = {"schema_version", "issuer_ref", "policy_id", "execution_profile_sha256", "scope_id", "role",
    "profile_ref", "controller_ref", "generator_controller_ref", "model_ref", "prompt_version", "assurance",
    "suite_ref", "split_ref", "domain_ref", "suite_sha256", "split_sha256", "domain_sha256", "rubric_id",
    "rubric_sha256", "issued_utc", "expires_utc", "independent", "complete", "counts", "metrics",
    "observation_refs", "qualification_ref", "authentication"}


class QualificationError(ValueError):
    """A reference, authenticated qualification or signing request was refused."""


def _fail(reason: str):
    raise QualificationError(reason)


def _policy(value):
    if type(value) is not RunPolicy:
        _fail("qualification_policy_invalid")
    return value.to_dict()


def _scope(policy):
    return "sha256:" + _digest(_policy(policy)["scope"])


def _cases(cases):
    if type(cases) not in (list, tuple) or not 1 <= len(cases) <= 10000:
        _fail("qualification_cases_invalid")
    result, ids, pairs = [], set(), {}
    for supplied in cases:
        if type(supplied) is not dict or set(supplied) != _CASE_FIELDS:
            _fail("qualification_case_invalid")
        case = json.loads(_json(supplied))
        _ref(case["case_id"])
        _ref(case["family_id"])
        if case["case_id"] in ids or type(case["input_document"]) is not dict:
            _fail("qualification_case_invalid")
        pending = [case["input_document"]]
        while pending:
            value = pending.pop()
            if type(value) is dict:
                if "expected_label" in value:
                    _fail("qualification_reference_label_exposed")
                pending.extend(value.values())
            elif type(value) is list:
                pending.extend(value)
        ids.add(case["case_id"])
        if type(case["expected_label"]) is not str or case["expected_label"] not in {"accept", "reject"} or type(case["critical"]) is not bool:
            _fail("qualification_case_invalid")
        if case["critical"] and case["expected_label"] != "reject":
            _fail("qualification_critical_reference_invalid")
        if case["pair_id"] is not None:
            _ref(case["pair_id"])
            pairs.setdefault(case["pair_id"], []).append(case)
        result.append(case)
    for pair in pairs.values():
        if (len(pair) != 2 or pair[0]["expected_label"] != pair[1]["expected_label"]
                or pair[0]["family_id"] != pair[1]["family_id"]
                or pair[0]["input_document"] == pair[1]["input_document"]):
            _fail("qualification_pair_invalid")
    _json(result)
    return result, pairs


def reference_suite(cases, *, suite_ref: str, split_ref: str, domain_ref: str, rubric: dict[str, Any]):
    """Bind protected references, held-out split, declared domain and exact rubric."""
    cases, _ = _cases(cases)
    for reference in (suite_ref, split_ref, domain_ref):
        _ref(reference)
    if type(rubric) is not dict or not rubric.get("id") or not rubric.get("revision"):
        _fail("qualification_rubric_invalid")
    _ref(rubric["id"])
    return {"suite_ref": suite_ref, "split_ref": split_ref, "domain_ref": domain_ref,
        "rubric": json.loads(_json(rubric)), "suite_sha256": _digest(cases),
        "split_sha256": _digest({"split_ref": split_ref, "case_ids": sorted(case["case_id"] for case in cases)}),
        "domain_sha256": _digest({"domain_ref": domain_ref, "families": sorted({case["family_id"] for case in cases})})}


def _ratio(numerator, denominator, *, upper=False):
    if not denominator:
        return None
    units, remainder = divmod(numerator * 1_000_000, denominator)
    if upper and remainder:
        units += 1
    whole, fraction = divmod(units, 1_000_000)
    return f"{whole}.{fraction:06d}".rstrip("0").rstrip(".")


def _qualified(record, frozen):
    counts, metrics, limits = record["counts"], record["metrics"], frozen["thresholds"]
    if not record["complete"] or any(value is None for value in metrics.values()):
        return None
    return bool(record["independent"] and record["assurance"] == frozen["assurance"]
        and counts["reference_cases"] >= limits["min_reference_cases"]
        and counts["families"] >= limits["min_families"]
        and counts["critical_false_acceptances"] <= limits["critical_false_acceptances_max"]
        and Decimal(metrics["positive_acceptance"]) >= Decimal(limits["positive_acceptance_min"])
        and Decimal(metrics["meaning_preserving_flip"]) <= Decimal(limits["meaning_preserving_flip_max"]))


@dataclass(frozen=True, init=False)
class QualificationAuthority:
    """Explicit host issuer; the key and reference labels stay outside workers."""

    issuer_ref: str
    generator_controller_ref: str
    _key: bytes = field(repr=False)

    def __init__(self, issuer_ref: str, key: bytes, generator_controller_ref: str):
        _ref(issuer_ref)
        _ref(generator_controller_ref)
        if type(key) is not bytes or not 32 <= len(key) <= 64:
            _fail("qualification_key_invalid")
        object.__setattr__(self, "issuer_ref", issuer_ref)
        object.__setattr__(self, "generator_controller_ref", generator_controller_ref)
        object.__setattr__(self, "_key", key)

    @property
    def sha256(self):
        return _digest({"issuer_ref": self.issuer_ref, "generator_controller_ref": self.generator_controller_ref,
                        "key_sha256": hashlib.sha256(self._key).hexdigest()})

    def issue(self, policy, profile, config_ref, suite, cases, observations, executor, *,
              expires_utc, scope_id=None, observed_utc=None):
        """Score actual authenticated held-out judgments against protected labels."""
        frozen = _policy(policy)
        if type(profile) is not ExecutionProfile or executor.profile.sha256 != profile.sha256:
            _fail("qualification_execution_profile_invalid")
        selected = profile.role(config_ref)
        if selected.model_ref.startswith("offline/"):
            _fail("qualification_offline_fixture_unsupported")
        from .review_service_policy import approved_profiles
        required = [row for row in approved_profiles(frozen, profile) if row["profile_ref"] == config_ref
                    and row["controller_ref"] == selected.controller_ref]
        if selected.role != "evaluator" or len(required) != 1 or required[0]["human_required"]:
            _fail("qualification_role_invalid")
        if profile.role_scopes is not None and profile.role_scopes.scope(config_ref).task_role != required[0]["role"]:
            _fail("qualification_role_scope_mismatch")
        cases, pairs = _cases(cases)
        if type(suite) is not dict or set(suite) != _SUITE_FIELDS:
            _fail("qualification_suite_invalid")
        expected = reference_suite(cases, **{key: suite[key] for key in ("suite_ref", "split_ref", "domain_ref", "rubric")})
        if suite != expected:
            _fail("qualification_reference_digest_mismatch")
        now = observed_utc or datetime.now(timezone.utc).isoformat()
        if not _utc(now) < _utc(expires_utc):
            _fail("qualification_validity_invalid")
        scope_id = scope_id or _scope(policy)
        if scope_id != _scope(policy):
            _fail("qualification_scope_invalid")
        if type(observations) not in (list, tuple) or len(observations) > len(cases):
            _fail("qualification_observations_invalid")
        lookup, seen, evidence, attempts = {case["case_id"]: case for case in cases}, {}, [], set()
        for entry in observations:
            if type(entry) not in (list, tuple) or len(entry) != 2 or type(entry[0]) is not ExecutionAttempt:
                _fail("qualification_observation_invalid")
            attempt, supplied = entry
            if (attempt.attempt_id in attempts or attempt.profile_sha256 != profile.sha256
                    or attempt.config_ref != config_ref or attempt.job["policy_id"] != policy.policy_id
                    or attempt.job["operation"] != "evaluate"
                    or set(attempt.job["scope"]) != set(frozen["scope"]["question_ids"])):
                _fail("qualification_attempt_mismatch")
            attempts.add(attempt.attempt_id)
            observed = validate_observation(executor.authenticate(supplied, attempt))
            if observed["reason"] == "offline_fixture":
                _fail("qualification_offline_fixture_unsupported")
            if any(observed[key] != value for key, value in {
                    "profile_sha256": profile.sha256, "attempt_id": attempt.attempt_id, "nonce": attempt.nonce,
                    "job_sha256": attempt.job_sha256, "input_sha256": attempt.input_sha256,
                    "config_ref": config_ref, "controller_ref": selected.controller_ref,
                    "model_ref": selected.model_ref, "prompt_version": selected.prompt_version}.items()):
                _fail("qualification_observation_binding_mismatch")
            output = observed.get("output")
            if (observed.get("state") != "succeeded" or observed.get("completed_utc") is None
                    or observed.get("final_accounting") is not True
                    or _utc(observed["observed_utc"]) > _utc(now)
                    or not _utc(observed["completed_utc"]) <= _utc(now)
                    or _utc(observed["completed_utc"]) > _utc(attempt.job["deadline_utc"])
                    or type(output) is not dict or set(output) != {"case_id", "label"}
                    or type(output["label"]) is not str or output["label"] not in {"accept", "reject", "unknown"}):
                _fail("qualification_judgment_invalid")
            case_id = output["case_id"]
            if type(case_id) is not str or case_id not in lookup or case_id in seen:
                _fail("qualification_case_mismatch")
            if attempt.input_document != lookup[case_id]["input_document"]:
                _fail("qualification_input_mismatch")
            seen[case_id] = output["label"]
            evidence.append({"case_id": case_id, "attempt_id": attempt.attempt_id,
                             "observation_sha256": _digest(observed), "input_sha256": attempt.input_sha256})
        positive = [case for case in cases if case["expected_label"] == "accept"]
        critical = [case for case in cases if case["critical"]]
        complete_pairs = [pair for pair in pairs.values() if all(seen.get(case["case_id"]) in {"accept", "reject"} for case in pair)]
        counts = {"reference_cases": len(seen), "families": len({lookup[name]["family_id"] for name in seen}),
            "critical_false_acceptances": sum(seen.get(case["case_id"]) == "accept" for case in critical),
            "positive_cases": len(positive), "positive_acceptances": sum(seen.get(case["case_id"]) == "accept" for case in positive),
            "paired_cases": len(complete_pairs), "paired_flips": sum(seen[pair[0]["case_id"]] != seen[pair[1]["case_id"]] for pair in complete_pairs),
            "expected_cases": len(cases), "unknown_cases": sum(label == "unknown" for label in seen.values())}
        record = {"schema_version": QUALIFICATION, "issuer_ref": self.issuer_ref, "policy_id": policy.policy_id,
            "execution_profile_sha256": profile.sha256, "scope_id": scope_id, "role": required[0]["role"],
            "profile_ref": config_ref, "controller_ref": selected.controller_ref, "generator_controller_ref": self.generator_controller_ref,
            "model_ref": selected.model_ref, "prompt_version": selected.prompt_version, "assurance": "artifact_checked",
            **{key: suite[key] for key in ("suite_ref", "split_ref", "domain_ref", "suite_sha256", "split_sha256", "domain_sha256")},
            "rubric_id": suite["rubric"]["id"], "rubric_sha256": _digest(suite["rubric"]), "issued_utc": now, "expires_utc": expires_utc,
            "independent": selected.controller_ref != self.generator_controller_ref and len({
                row["controller_ref"] for row in frozen["required_evaluators"]}) == len(frozen["required_evaluators"]),
            "complete": len(seen) == len(cases) and counts["unknown_cases"] == 0 and bool(positive and critical and pairs)
                        and len(complete_pairs) == len(pairs), "counts": counts,
            "metrics": {"positive_acceptance": _ratio(counts["positive_acceptances"], len(positive)),
                        "meaning_preserving_flip": _ratio(counts["paired_flips"], len(complete_pairs), upper=True)},
            "observation_refs": sorted(evidence, key=lambda row: row["case_id"])}
        record["qualification_ref"] = "qualification-" + _digest(record)
        record["authentication"] = hmac.new(self._key, b"research-qualification/v1\0" + _json(record), hashlib.sha256).hexdigest()
        return json.loads(_json(record))

    def verify(self, record):
        if type(record) is not dict or set(record) != _RECORD_FIELDS:
            _fail("qualification_record_invalid")
        value = json.loads(_json(record))
        signature = value.pop("authentication")
        if type(signature) is not str or not hmac.compare_digest(signature,
                hmac.new(self._key, b"research-qualification/v1\0" + _json(value), hashlib.sha256).hexdigest()):
            _fail("qualification_authentication_refused")
        qualification_ref = value.pop("qualification_ref")
        if (qualification_ref != "qualification-" + _digest(value) or value["schema_version"] != QUALIFICATION
                or value["issuer_ref"] != self.issuer_ref or value["generator_controller_ref"] != self.generator_controller_ref
                or type(value["counts"]) is not dict or set(value["counts"]) != _COUNTS):
            _fail("qualification_record_invalid")
        return json.loads(_json(record))


@dataclass(frozen=True, init=False)
class QualifiedEvaluators:
    """Immutable authenticated records; missing or incomplete evidence stays unknown."""

    _records: bytes = field(repr=False)
    authority: QualificationAuthority = field(repr=False)

    def __init__(self, records, authority):
        if type(authority) is not QualificationAuthority or type(records) not in (list, tuple) or len(records) > 8:
            _fail("qualification_registry_invalid")
        verified = [authority.verify(record) for record in records]
        keys = [(record["role"], record["profile_ref"]) for record in verified]
        if len(keys) != len(set(keys)):
            _fail("qualification_registry_duplicate")
        object.__setattr__(self, "_records", _json(verified))
        object.__setattr__(self, "authority", authority)

    @property
    def sha256(self):
        return _digest({"records_sha256": hashlib.sha256(self._records).hexdigest(), "authority_sha256": self.authority.sha256})

    def observations(self, policy, execution_profile, now_utc, rubric_id, *, rubric_sha256=None, scope_id=None):
        frozen = _policy(policy)
        scope_id = scope_id or _scope(policy)
        if type(execution_profile) is not ExecutionProfile or scope_id != _scope(policy):
            _fail("qualification_scope_invalid")
        records = json.loads(self._records)
        lookup = {(record["role"], record["profile_ref"]): self.authority.verify(record) for record in records}
        rows = []
        from .review_service_policy import approved_profiles
        for wanted in approved_profiles(frozen, execution_profile):
            row = {**wanted, "qualification_ref": None, "qualified": None, "independent": None,
                "assurance": frozen["assurance"], "reference_cases": None, "families": None,
                "critical_false_acceptances": None, "positive_acceptance": None, "meaning_preserving_flip": None}
            record = lookup.get((wanted["role"], wanted["profile_ref"]))
            if record is None:
                rows.append(row)
                continue
            role = execution_profile.role(wanted["profile_ref"])
            expected = {"policy_id": policy.policy_id, "execution_profile_sha256": execution_profile.sha256,
                "scope_id": scope_id, "controller_ref": wanted["controller_ref"], "model_ref": role.model_ref,
                "prompt_version": role.prompt_version, "rubric_id": rubric_id}
            if any(record[key] != value for key, value in expected.items()) or role.controller_ref != wanted["controller_ref"]:
                _fail("qualification_binding_mismatch")
            if not _utc(record["issued_utc"]) <= _utc(now_utc) < _utc(record["expires_utc"]):
                if getattr(execution_profile, "review_services", None) is not None:
                    rows.append(row)
                    continue
                _fail("qualification_expired")
            if rubric_sha256 is None:
                rows.append(row)
                continue
            if record["rubric_sha256"] != rubric_sha256:
                _fail("qualification_rubric_changed")
            row.update({"qualification_ref": record["qualification_ref"], "qualified": _qualified(record, frozen),
                        "independent": record["independent"], "assurance": record["assurance"],
                        **{key: record["counts"][key] for key in ("reference_cases", "families", "critical_false_acceptances")},
                        **record["metrics"]})
            if not record["complete"]:
                for key in ("critical_false_acceptances", "positive_acceptance", "meaning_preserving_flip"):
                    row[key] = None
            rows.append(row)
        return rows


@dataclass(frozen=True)
class SigningIdentity:
    """A parent-held signing credential selected by explicit host configuration."""

    config_ref: str
    principal: str
    key_id: str
    controller_ref: str
    role: str
    key: bytes = field(repr=False)

    def __post_init__(self):
        for value in (self.config_ref, self.principal, self.key_id, self.controller_ref):
            _ref(value)
        if type(self.role) is not str or self.role not in {"generator", "evaluator"} or type(self.key) is not bytes or not 32 <= len(self.key) <= 64:
            _fail("review_signing_identity_invalid")

    def metadata(self):
        return {name: getattr(self, name) for name in ("config_ref", "principal", "key_id", "controller_ref", "role")} | {
            "key_sha256": hashlib.sha256(self.key).hexdigest()}


@dataclass(frozen=True, init=False)
class HostReviewSigner:
    """Sign exact owner-prepared strict envelopes; signatures do not grant acceptance."""

    authority_policy_id: str
    authority_revision: str
    generator: SigningIdentity = field(repr=False)
    reviewers: tuple[SigningIdentity, ...] = field(repr=False)
    expires_utc: str
    not_before_utc: str

    def __init__(self, authority_policy_id, authority_revision, generator, reviewers, expires_utc,
                 not_before_utc="1970-01-01T00:00:00Z"):
        _ref(authority_policy_id)
        _ref(authority_revision)
        if type(generator) is not SigningIdentity or generator.role != "generator" or not isinstance(reviewers, Sequence):
            _fail("review_signer_invalid")
        reviewers = tuple(reviewers)
        if not 1 <= len(reviewers) <= 8 or any(type(identity) is not SigningIdentity or identity.role != "evaluator" for identity in reviewers):
            _fail("review_signer_invalid")
        if len({identity.config_ref for identity in reviewers}) != len(reviewers):
            _fail("review_signer_duplicate")
        if any(identity.controller_ref == generator.controller_ref or identity.key == generator.key for identity in reviewers):
            _fail("review_signer_not_independent")
        identities = (generator, *reviewers)
        if len({identity.key_id for identity in identities}) != len(identities):
            _fail("review_signer_duplicate")
        if len({identity.controller_ref for identity in identities}) != len(identities) or len({
                identity.key for identity in identities}) != len(identities):
            _fail("review_signer_not_independent")
        if not _utc(not_before_utc) < _utc(expires_utc):
            _fail("review_signer_validity_invalid")
        for name, value in (("authority_policy_id", authority_policy_id), ("authority_revision", authority_revision),
                ("generator", generator), ("reviewers", reviewers), ("expires_utc", expires_utc),
                ("not_before_utc", not_before_utc)):
            object.__setattr__(self, name, value)

    @property
    def sha256(self):
        return _digest({"authority_policy_id": self.authority_policy_id, "authority_revision": self.authority_revision,
            "generator": self.generator.metadata(), "reviewers": [identity.metadata() for identity in self.reviewers],
            "expires_utc": self.expires_utc, "not_before_utc": self.not_before_utc})

    def _sign(self, payload, identity, now):
        if not _utc(self.not_before_utc) <= _utc(now) < _utc(self.expires_utc):
            _fail("review_signer_expired")
        auth = {"schema_version": "evidence-authentication/v1", "scheme": "hmac-sha256", "principal": identity.principal,
            "key_id": identity.key_id, "role": identity.role, "policy_id": self.authority_policy_id,
            "policy_revision": self.authority_revision, "issued_at": now, "expires_at": self.expires_utc}
        unsigned = _json({"payload": payload, "authentication": auth})
        auth["signature"] = hmac.new(identity.key, b"evidence-attestation/v1\0" + unsigned
            + b"\n", hashlib.sha256).hexdigest()
        return {"payload": json.loads(unsigned)["payload"], "authentication": auth}

    def authorship(self, basis_id, claim_id, now_utc, *, claim_schema_version=None,
            observed_workspace_basis_id=None, semantic_material_id=None):
        _ref(claim_id)
        if type(basis_id) is not str or re.fullmatch(r"sha256:[a-f0-9]{64}", basis_id) is None:
            _fail("review_authorship_basis_invalid")
        payload = {"schema_version": "evidence-strict-authorship/v1", "basis_id": basis_id, "claim_id": claim_id}
        if claim_schema_version is not None:
            if claim_schema_version not in {"evidence-strict-claims/v1", "evidence-strict-claims/v2"} or any(
                    type(value) is not str or re.fullmatch(r"sha256:[a-f0-9]{64}", value) is None
                    for value in (observed_workspace_basis_id, semantic_material_id)):
                _fail("review_authorship_basis_invalid")
            payload.update(schema_version="evidence-strict-authorship/v2", claim_schema_version=claim_schema_version,
                observed_workspace_basis_id=observed_workspace_basis_id, semantic_material_id=semantic_material_id)
        elif observed_workspace_basis_id is not None or semantic_material_id is not None:
            _fail("review_authorship_basis_invalid")
        return self._sign(payload, self.generator, now_utc)

    def registration(self, prepared, verdicts, rationale, request_id, now_utc, *, config_ref=None, delivery=None):
        from .pack_discovery import owner

        contract = owner("_strict_contract")
        _ref(request_id)
        if (type(prepared) is not dict or type(verdicts) is not dict or set(verdicts) != set(contract.CHECKS)
                or any(type(value) is not str or value not in {"pass", "fail", "unknown"} for value in verdicts.values())
                or type(rationale) is not str or not 1 <= len(rationale) <= 65536):
            _fail("review_signing_request_invalid")
        prepared = json.loads(_json(prepared))
        identity = next((identity for identity in self.reviewers if identity.config_ref == config_ref), None)
        if identity is None and config_ref is None and len(self.reviewers) == 1:
            identity = self.reviewers[0]
        if identity is None:
            _fail("review_signing_profile_unavailable")
        try:
            return self._prepared_envelope(prepared, verdicts, rationale, request_id, now_utc, identity, contract, delivery=delivery)
        except QualificationError:
            raise
        except (KeyError, TypeError, IndexError, AttributeError, ValueError):
            _fail("review_prepared_invalid")

    def _prepared_envelope(self, prepared, verdicts, rationale, request_id, now_utc, identity, contract, *, delivery=None):
        command = prepared["registration"]
        if command["action"] != "register-strict-review" or prepared["snapshot"]["policy"]["human_review"]:
            _fail("review_human_role_incompatible")
        claim_id, basis_id = prepared["snapshot"]["claim"]["id"], prepared["result"]["basis_id"]
        selective = prepared["review_schema"] == "evidence-strict-review/v3"
        if selective:
            basis_id = prepared["claim_basis_id"]
            if type(delivery) is not dict or delivery.get("semantic_material_id") != prepared["semantic_material_id"]:
                _fail("review_delivery_required")
        elif delivery is not None:
            _fail("review_delivery_version_invalid")
        row = next((row for row in prepared["result"]["claims"] if row["claim"]["id"] == claim_id), None)
        if row is None or row["claim"] != prepared["snapshot"]["claim"]:
            _fail("review_prepared_claim_mismatch")
        authorship = ({"claim_schema_version": prepared["claim_schema_version"],
            "observed_workspace_basis_id": prepared["observed_workspace_basis_id"],
            "semantic_material_id": prepared["semantic_material_id"]} if selective else {})
        payload = {"schema_version": prepared["review_schema"], "basis_id": basis_id, "claim_id": claim_id,
            "generator": self.authorship(basis_id, claim_id, now_utc, **authorship), "verdicts": dict(verdicts), "rationale": rationale,
            "reviewed_at": now_utc, "observation": row["observation"], "snapshot": prepared["snapshot"]}
        if selective:
            payload.update(review_basis_mode="claim_dependencies_v1", claim_schema_version=prepared["claim_schema_version"],
                observed_workspace_basis_id=prepared["observed_workspace_basis_id"], dependency_basis=prepared["dependency_basis"],
                delivery=json.loads(_json(delivery)))
        contract.review_document(payload)
        command["request_id"] = request_id
        command["body"]["review"] = payload
        return self._sign(command, identity, now_utc)
