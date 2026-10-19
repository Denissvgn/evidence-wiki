"""Immutable approved review-service routes; inert declarations confer no authority."""

from dataclasses import dataclass
from decimal import Decimal

from evidence_wiki.agent_execution import ExecutionError, ExecutionProfile, ExecutionRoleLimit, _ref
from evidence_wiki.mutation_contracts import detached, digest

SERVICE_PLAN = "research-review-service-plan/v1"
SERVICE_POLICY = "research-run-policy/v3"


@dataclass(frozen=True)
class ReviewServiceRoute:
    slot_ref: str
    task_role: str
    config_refs: tuple[str, ...]
    limit: ExecutionRoleLimit

    def __post_init__(self):
        _ref(self.slot_ref)
        if type(self.task_role) is not str or self.task_role not in {"evaluator", "critic"}:
            raise ExecutionError("service_task_role_invalid")
        if type(self.config_refs) not in {list, tuple} or not 1 <= len(self.config_refs) <= 3:
            raise ExecutionError("service_route_bound")
        refs = tuple(_ref(value) for value in self.config_refs)
        if refs[0] != self.slot_ref or len(set(refs)) != len(refs):
            raise ExecutionError("service_route_identity_invalid")
        if type(self.limit) is not ExecutionRoleLimit or self.limit.role != "evaluator":
            raise ExecutionError("service_route_limit_invalid")
        object.__setattr__(self, "config_refs", refs)

    def document(self):
        return {"slot_ref": self.slot_ref, "task_role": self.task_role,
            "config_refs": list(self.config_refs), "limit": self.limit.document()}


@dataclass(frozen=True)
class ReviewServicePlan:
    routes: tuple[ReviewServiceRoute, ...]

    def __post_init__(self):
        if (type(self.routes) not in {list, tuple} or not 1 <= len(self.routes) <= 8
                or any(type(row) is not ReviewServiceRoute for row in self.routes)):
            raise ExecutionError("service_plan_invalid")
        routes = tuple(self.routes)
        refs = [value for row in routes for value in row.config_refs]
        if len(refs) > 8 or len(set(refs)) != len(refs):
            raise ExecutionError("service_routes_overlap")
        object.__setattr__(self, "routes", tuple(sorted(routes, key=lambda row: row.slot_ref)))

    def document(self):
        return {"schema_version": SERVICE_PLAN, "routes": [row.document() for row in self.routes],
            "continuation": "known_predispatch_or_authenticated_final_service_failure",
            "unknown_external_work": "reservation_retained_no_failover",
            "semantic_verdict_failure": "no_service_failover"}

    @property
    def sha256(self):
        return digest(self.document())

    def route(self, slot_ref):
        _ref(slot_ref)
        matches = [row for row in self.routes if row.slot_ref == slot_ref]
        if len(matches) != 1:
            raise ExecutionError("service_slot_unavailable")
        return matches[0]

    def check_profile(self, profile):
        if type(profile) is not ExecutionProfile or profile.role_scopes is None:
            raise ExecutionError("service_scoped_profile_required")
        if profile.disagreement is not None and any(len(route.config_refs) > 1 for route in self.routes):
            declaration = getattr(profile, "service_repair", None)
            if declaration is None:
                raise ExecutionError("service_disagreement_alternates_unsupported")
            from .service_repair_binding import ServiceRepairBinding
            if type(declaration) is not ServiceRepairBinding:
                raise ExecutionError("service_repair_binding_invalid")
            declaration.check(profile)
        controllers = []
        models = []
        workers = {role.controller_ref for role in profile.roles if role.role == "host_worker"}
        for route in self.routes:
            for ref in route.config_refs:
                selected = profile.role(ref)
                if (selected.role != "evaluator" or profile.role_scopes.scope(ref).task_role != route.task_role):
                    raise ExecutionError("service_scope_changed")
                profile.role_scopes.scope(ref).require("model")
                controllers.append(selected.controller_ref)
                models.append(selected.model_ref)
            if route.limit.controller_ref != profile.role(route.slot_ref).controller_ref:
                raise ExecutionError("service_controller_changed")
            if profile.schedule is not None:
                limit = profile.schedule.limit("evaluator", route.limit.controller_ref)
                if any(getattr(route.limit, name) > getattr(limit, name) for name in ("model_calls", "tool_calls", "tokens", "seconds")) or Decimal(route.limit.cost_limit) > Decimal(limit.cost_limit):
                    raise ExecutionError("service_limit_exceeds_role")
        if len(set(controllers)) != len(controllers) or set(controllers) & workers:
            raise ExecutionError("service_controllers_not_distinct")
        if "model" in profile.required_diversity:
            worker_models = {role.model_ref for role in profile.roles if role.role == "host_worker"}
            if len(set(models)) != len(models) or set(models) & worker_models:
                raise ExecutionError("service_model_configuration_not_distinct")

    def check(self, profile, frozen_policy):
        """Validate inert bindings; caller must authenticate the frozen native policy."""
        if type(profile) is not ExecutionProfile or type(frozen_policy) is not dict:
            raise ExecutionError("service_binding_invalid")
        self.check_profile(profile)
        expected = {row["profile_ref"]: row for row in frozen_policy["required_evaluators"]}
        if set(expected) != {row.slot_ref for row in self.routes}:
            raise ExecutionError("service_required_slots_changed")
        if (frozen_policy.get("schema_version") not in {SERVICE_POLICY, "research-run-policy/v4"}
                or frozen_policy.get("review_services") != self.document()):
            raise ExecutionError("service_immutable_policy_required")
        caps = frozen_policy["budgets"]
        for route in self.routes:
            if expected[route.slot_ref]["role"] != route.task_role:
                raise ExecutionError("service_task_role_changed")
            primary = profile.role(route.slot_ref)
            if (primary.controller_ref != expected[route.slot_ref]["controller_ref"]
                    or route.limit.controller_ref != primary.controller_ref):
                raise ExecutionError("service_controller_changed")
            for name in ("model_calls", "tool_calls", "tokens", "seconds"):
                if getattr(route.limit, name) > caps[name]:
                    raise ExecutionError("service_limit_exceeds_policy")
            if Decimal(route.limit.cost_limit) > Decimal(caps["cost_limit"]):
                raise ExecutionError("service_limit_exceeds_policy")
            for ref in route.config_refs:
                selected = profile.role(ref)
                if (selected.role != "evaluator" or profile.role_scopes is None
                        or profile.role_scopes.scope(ref).task_role != route.task_role):
                    raise ExecutionError("service_scope_changed")
                profile.role_scopes.scope(ref).require("model")
        controllers = [profile.role(ref).controller_ref for route in self.routes for ref in route.config_refs]
        if len(set(controllers)) != len(controllers):
            raise ExecutionError("service_controllers_not_distinct")

    @classmethod
    def restore(cls, value):
        if type(value) is not dict or set(value) != {"schema_version", "routes", "continuation", "unknown_external_work", "semantic_verdict_failure"}:
            raise ExecutionError("service_plan_document_invalid")
        routes = []
        if type(value["routes"]) is not list or not 1 <= len(value["routes"]) <= 8:
            raise ExecutionError("service_plan_invalid")
        for row in value["routes"]:
            if type(row) is not dict or set(row) != {"slot_ref", "task_role", "config_refs", "limit"}:
                raise ExecutionError("service_route_document_invalid")
            limit = row["limit"]
            if type(limit) is not dict or set(limit) != {"role", "controller_ref", "budgets"}:
                raise ExecutionError("service_route_limit_invalid")
            budget = limit["budgets"]
            if type(budget) is not dict or set(budget) != {"model_calls", "tool_calls", "tokens", "cost", "seconds"}:
                raise ExecutionError("service_route_limit_invalid")
            values = {**budget, "cost_limit": budget["cost"]}
            values.pop("cost")
            routes.append(ReviewServiceRoute(row["slot_ref"], row["task_role"], row["config_refs"],
                ExecutionRoleLimit(role=limit["role"], controller_ref=limit["controller_ref"], **values)))
        restored = cls(routes)
        if restored.document() != detached(value):
            raise ExecutionError("service_plan_document_invalid")
        return restored


def service_action(plan, slot_ref, history, available_configs):
    """Reduce authenticated owner history; this does not reserve or dispatch work."""
    if type(plan) is not ReviewServicePlan or type(history) is not list or len(history) > 3:
        raise ExecutionError("service_history_invalid")
    route = plan.route(slot_ref)
    if (type(available_configs) not in {list, tuple} or any(type(value) is not str for value in available_configs)
            or len(set(available_configs)) != len(available_configs)
            or not set(available_configs) <= set(route.config_refs)):
        raise ExecutionError("service_available_scope_invalid")
    used = []
    for row in history:
        if type(row) is not dict or set(row) != {"config_ref", "state", "reservation_occupied", "authenticated", "final_accounting", "reason"}:
            raise ExecutionError("service_history_invalid")
        if (type(row["config_ref"]) is not str or type(row["state"]) is not str or type(row["reason"]) is not str
                or row["config_ref"] not in route.config_refs or row["config_ref"] in used
                or any(type(row[key]) is not bool for key in ("reservation_occupied", "authenticated", "final_accounting"))):
            raise ExecutionError("service_history_invalid")
        used.append(row["config_ref"])
        if row["reservation_occupied"] or not row["authenticated"] or not row["final_accounting"]:
            return {"action": "blocked", "reason": "external_work_unknown", "config_ref": None}
        if row["state"] == "succeeded":
            return {"action": "completed", "reason": "owned_service_result", "config_ref": row["config_ref"]}
        if row["state"] not in {"failed", "unavailable", "expired"} or row["reason"] not in {"executor_unavailable", "service_credentials_unavailable"}:
            return {"action": "blocked", "reason": "service_failure_not_retryable", "config_ref": None}
    for config in route.config_refs:
        if config not in used and config in available_configs:
            return {"action": "select", "reason": "approved_service_route", "config_ref": config}
    return {"action": "blocked", "reason": "approved_services_exhausted", "config_ref": None}


def approved_profiles(frozen_policy, profile):
    """Return actual configured targets only from explicit frozen declarations."""
    wanted = frozen_policy["required_evaluators"]
    plan = getattr(profile, "review_services", None)
    if plan is None:
        if "review_services" in frozen_policy:
            raise ExecutionError("service_profile_policy_mismatch")
        return detached(wanted)
    plan.check(profile, frozen_policy)
    rows = []
    for primary in wanted:
        route = plan.route(primary["profile_ref"])
        for config in route.config_refs:
            rows.append({**primary, "profile_ref": config, "controller_ref": profile.role(config).controller_ref})
    return detached(rows)


def schema_document():
    def obj(**fields):
        return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
    name = {"type": "string", "minLength": 1, "maxLength": 256}
    count = {"type": "integer", "minimum": 0, "maximum": 100000000}
    cost = {"type": "string", "pattern": r"^(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,12})?$(?![\s\S])"}
    budget = obj(model_calls=count, tool_calls=count, tokens=count, cost=cost,
        seconds={"type": "integer", "minimum": 0, "maximum": 86400})
    route = obj(slot_ref=name, task_role={"enum": ["evaluator", "critic"]},
        config_refs={"type": "array", "items": name, "minItems": 1, "maxItems": 3, "uniqueItems": True},
        limit=obj(role={"const": "evaluator"}, controller_ref=name, budgets=budget))
    return obj(schema_version={"const": SERVICE_PLAN}, routes={"type": "array", "items": route, "minItems": 1, "maxItems": 8},
        continuation={"const": "known_predispatch_or_authenticated_final_service_failure"},
        unknown_external_work={"const": "reservation_retained_no_failover"},
        semantic_verdict_failure={"const": "no_service_failover"})
