"""Closed prospective discovery and formal ceiling selection."""

import copy

PROFILE = "control-observational-long"
OWNER_PROFILE = "control-observational-owner-long"
FIELDS = ("phase_ceiling_policy", "phase_ceiling_policy_sha256")
POLICY = {
    "schema_version": "review-control-phase-ceiling-policy/v1",
    "profile": PROFILE,
    "service_policy_schema": "review-observational-service-policy/v5",
    "discovery_execution_profile": "sixteen-hour",
    "discovery_phase_caps": {"setup": 900, "job": 3600, "mutation": 3600,
        "owner_read": 1200, "global_close": 1500, "cleanup": 60},
    "formal_phase_ceilings": {"setup": 1200, "job": 3600, "mutation": 4500,
        "owner_read": 3600, "global_close": 10800, "cleanup": 60},
}

OWNER_POLICY = copy.deepcopy(POLICY)
OWNER_POLICY.update(schema_version="review-control-phase-ceiling-policy/v2", profile=OWNER_PROFILE,
    service_policy_schema="review-observational-service-policy/v6")
OWNER_POLICY["discovery_phase_caps"].update(owner_read=3600, global_close=10800)
VERSION_PAIRS = {
    "review-dependency-observational-pilot-plan/v3": "review-dependency-observational-pilot-plan/v4",
    "review-dependency-observational-pilot-report/v3": "review-dependency-observational-pilot-report/v4",
    "review-dependency-workload-collection-plan/v2": "review-dependency-workload-collection-plan/v3",
    "review-dependency-workload-collection-observation/v2": "review-dependency-workload-collection-observation/v3",
    "selective-observational-pilot-plan/v3": "selective-observational-pilot-plan/v4",
    "selective-observational-pilot-report/v3": "selective-observational-pilot-report/v4",
    "selective-observational-pilot-measurement/v3": "selective-observational-pilot-measurement/v4",
    "selective-complete-pilot-measurement/v4": "selective-complete-pilot-measurement/v5",
    "review-dependency-workload-plan/v9": "review-dependency-workload-plan/v10",
    "review-dependency-workload-observation/v8": "review-dependency-workload-observation/v9",
    "native-review-case-extended-selection/v3": "native-review-case-extended-selection/v4",
    "native-review-case-extended-allocation/v3": "native-review-case-extended-allocation/v4",
    "native-review-case-run/v4": "native-review-case-run/v5",
    "native-review-case-index/v4": "native-review-case-index/v5",
}


def owner(value):
    return type(value) is dict and type(value.get("phase_ceiling_policy")) is dict and value["phase_ceiling_policy"].get("profile") == OWNER_PROFILE


def version(historical, value):
    return VERSION_PAIRS[historical] if owner(value) else historical


def exact(left, right):
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return left.keys() == right.keys() and all(exact(item, right[key]) for key, item in left.items())
    if type(left) is list:
        return len(left) == len(right) and all(exact(a, b) for a, b in zip(left, right, strict=True))
    return left == right


def binding(profile=PROFILE):
    from tools._review_dependency_workload import digest
    if profile not in (PROFILE, OWNER_PROFILE):
        raise ValueError("workload_phase_ceiling_policy_invalid")
    policy = OWNER_POLICY if profile == OWNER_PROFILE else POLICY
    return {FIELDS[0]: copy.deepcopy(policy), FIELDS[1]: digest(policy)}


def validate(value, *, required=None):
    """Authenticate both closed policy bytes and digest, preserving old absence."""
    present = type(value) is dict and any(key in value for key in FIELDS)
    if required is True and not present or required is False and present:
        raise ValueError("workload_phase_ceiling_policy_invalid")
    if present:
        schema = value.get("schema_version")
        if schema is not None and type(schema) is not str:
            raise ValueError("workload_phase_ceiling_policy_invalid")
        profile = OWNER_PROFILE if owner(value) else PROFILE
        if schema in VERSION_PAIRS and profile != PROFILE or schema in VERSION_PAIRS.values() and profile != OWNER_PROFILE:
            raise ValueError("workload_phase_ceiling_policy_invalid")
        if "service_policy" in value and (type(value["service_policy"]) is not dict or value["service_policy"].get("profile") != profile):
            raise ValueError("workload_phase_ceiling_policy_invalid")
        expected = binding(profile)
        if not all(key in value and exact(value[key], expected[key]) for key in FIELDS):
            raise ValueError("workload_phase_ceiling_policy_invalid")
        if "journal_collection_policy" in value:
            from tools._review_journal_artifacts import POLICY as collection_policy
            if not exact(value["journal_collection_policy"], collection_policy):
                raise ValueError("workload_phase_ceiling_policy_invalid")
        return expected
    return {}
