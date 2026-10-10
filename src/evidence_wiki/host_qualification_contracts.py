"""Closed bounded host observation documents; no grants or asserted isolation."""

from .mutation_contracts import canonical, detached
from .onboarding_contract import _matches
from .source_contracts import array, obj, string

HOST = "research-host-qualification/v1"
PROTECTED = "research-protected-host-qualification/v1"


def schemas():
    from .review_service_contracts import STATUS
    from .review_service_contracts import schemas as service_schemas

    nullable = lambda shape: {"anyOf": [shape, {"type": "null"}]}
    enum = lambda *values: {"enum": list(values)}
    text = string(256)
    hashed = {"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    attempt = obj(attempt_id=text, config_ref=text, attempt_ref=hashed, controller_ref=text, model_ref=text,
        execution_state=enum("not_observed", "running", "succeeded", "failed", "cancelled", "expired", "unknown", "unavailable"),
        observation_ref=nullable(hashed), context_state=enum("not_observed", "current_immutable_packet", "not_current", "incomplete"),
        context_ref=nullable(hashed), attestation_state=enum("not_observed", "native_controller_verified", "current_bound_review"),
        attestation_ref=nullable(hashed), attestation_principal=nullable(text), attestation_key_id=nullable(text),
        application_state=enum("not_observed", "signed", "registered"), context_reason=nullable(text), lease_state=enum("current", "expired"))
    runtime = obj(os=text, os_release=text, machine=text, python_implementation=text, python_version=text,
        package_source_sha256=text, native_script_sha256=text, package_location_sha256=hashed, executable_sha256=text)
    artifact = obj(boundary={"const": "artifact_checked"}, state=enum("observed", "not_observed", "unsupported"),
        scope=string(512), executor=enum("trusted_bounded_subprocess", "configured_host_service"), semantic_eligibility=text)
    enforced = obj(boundary={"const": "host_enforced"}, state={"const": "unsupported"}, scope=string(512), reason=text)
    remote = obj(boundary={"const": "remote_model"}, state={"const": "not_observed"}, scope=string(512))
    host = obj(schema_version={"const": HOST}, observed_utc=string(40), run_id=text, binding_id=hashed, basis_id=hashed,
        policy_id=hashed, execution_profile_sha256=text, registry_sha256=text, journal_head_ref=nullable(hashed),
        runtime=runtime, credentials=service_schemas()[STATUS], attempts=array(attempt, 64),
        control=obj(authenticated_review_controller_refs=array(text, 8), authenticated_review_principals=array(text, 8),
            distinct_native_review_controllers=nullable({"type": "boolean"}), independent_credential_custody={"const": "not_observed"},
            isolated_reasoning={"const": "not_observed"}),
        capability_matrix={"type": "array", "minItems": 3, "maxItems": 3,
            "prefixItems": [artifact, enforced, remote], "items": {"anyOf": [artifact, enforced, remote]}},
        limitations=array(string(512), 8), authorized={"const": False}, evidence_accepted={"const": False})
    protected = obj(schema_version={"const": PROTECTED}, observed_utc=string(40), backend={"const": "darwin-sbpl"},
        probe_program_sha256=text, draft_write={"const": "allowed"}, protected_authority_read={"const": "denied"},
        workspace_write={"const": "denied"}, host_implementation=text, action_id=hashed, policy_id=hashed, basis_id=hashed,
        boundary={"const": "draft_worker"}, state={"const": "observed"}, semantic_host_enforced={"const": "unsupported"},
        limitations=array(string(512), 8), authorized={"const": False}, evidence_accepted={"const": False})
    return {key: {"$schema": "https://json-schema.org/draft/2020-12/schema", **value} for key, value in ((HOST, host), (PROTECTED, protected))}


def validate(identifier, value):
    from ._host_qualification import _refuse

    try:
        _matches(value, schemas()[identifier])
        if identifier == HOST and [row["boundary"] for row in value["capability_matrix"]] != ["artifact_checked", "host_enforced", "remote_model"]:
            _refuse("capability_matrix_selection_invalid")
        if len(canonical(value)) > 262144:
            _refuse("report_bound_exceeded")
        return detached(value)
    except (KeyError, TypeError, ValueError):
        _refuse("report_shape_invalid")
