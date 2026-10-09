"""Fresh host provenance, scoped exact metrics and hostile outcome imports."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import ROUND_UP, Inexact, localcontext

import pytest

from evidence_wiki import ObservationSource, ObservationVerifier, OutcomeIngestion
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from evidence_wiki.outcome_contracts import BATCH, CLASSES, REPORT, RETAINED, VERIFICATION, validate_document
from evidence_wiki.telemetry_contracts import digest


class SourceOwner:
    """Check independently retained source bytes and current producer withdrawal."""

    def __init__(self):
        self.originals = {}
        self.withdrawn = False
        self.calls = 0
        self.alter = lambda value: value

    def retain(self, batch):
        self.originals[digest(batch)] = deepcopy(batch)

    def verify(self, request):
        self.calls += 1
        if self.withdrawn or self.originals.get(request["batch_sha256"]) != request["batch"]:
            raise ValueError("Original producer observation unavailable.")
        batch = request["batch"]
        now = datetime.now(timezone.utc)
        return self.alter({"schema_version": VERIFICATION, "batch_sha256": digest(batch),
            "scope_id": batch["scope_id"], "binding": batch["binding"], "source": batch["source"],
            "source_receipt_sha256": batch["source_receipt"]["sha256"], "controller_ref": "source-owner",
            "authentication_ref": "protected-original-receipt", "verified_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=60)).isoformat()})


@pytest.fixture
def selected(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    host = HostContext(allowed_roots=(root,), actor_ref="measurement-host",
                       grants=frozenset({"outcomes.ingest", "outcomes.report"}))
    owner = SourceOwner()
    sources = (
        ObservationSource("customers", "protected-customer-events", "externally_observed", "customer"),
        ObservationSource("human", "protected-human-work", "externally_observed", "human"),
        ObservationSource("payments", "protected-payments", "externally_observed", "business"),
        ObservationSource("incidents", "protected-incidents", "externally_observed", "incident"),
        ObservationSource("checks", "protected-checks", "deterministic_verified", "automated"),
        ObservationSource("reviews", "protected-reviews", "agent_adjudicated", "agent"),
        ObservationSource("simulation", "simulation-events", "simulated", "simulation"),
    )
    with host.activate():
        plan = OutcomeIngestion(root, "observed-scope", "sha256:" + "a" * 64, sources,
                                ObservationVerifier("source-owner", owner.verify))
    return host, owner, plan


def batch(plan, *, metric="customer_adoption", source=0, coverage="complete", denominator=10,
          currency=None, values=("1",)):
    metadata = {"customer_adoption": ("usage", "count", "customers"), "sessions": ("usage", "count", "sessions"),
        "human_interventions": ("intervention", "count", "interventions"),
        "human_work_seconds": ("intervention", "seconds", "work_records"),
        "payments": ("business", "currency", "payments"), "incidents": ("incident", "count", "incidents"),
        "technical_success": ("usage", "count", "cases")}
    kind, unit, population = metadata[metric]
    return {"schema_version": BATCH, "batch_id": "batch-" + metric, "scope_id": plan.scope_id, "binding": plan.binding,
        "source": plan.sources[source].document(), "source_receipt": {"ref": "protected/original", "sha256": digest(metric)},
        "period": {"started_at": "2026-10-01T00:00:00Z", "ended_at": "2026-10-02T00:00:00Z"},
        "record_kind": kind, "metric": metric, "unit": unit, "currency": currency,
        "denominator": None if denominator is None else {"value": denominator, "unit": population, "population_ref": "observed-population"},
        "coverage": coverage, "gaps": [], "exclusions": [],
        "records": [{"observation_id": "obs-" + str(index), "entity_id": "entity-" + str(index),
            "subject_id": "entity-" + str(index), "occurred_at": "2026-10-01T12:00:00Z", "value": value}
            for index, value in enumerate(values)]}


def retain(selected, value):
    host, owner, plan = selected
    owner.retain(value)
    with host.activate():
        return plan.ingest(value)


def report(selected, *values):
    with selected[0].activate():
        return selected[2].report(list(values))


def external(result):
    return result["evidence_classes"]["externally_observed"]


def test_empty_inputs_are_technical_neutral_and_never_claim_actual_business(selected):
    result = report(selected)
    assert set(result["evidence_classes"]) == set(CLASSES)
    assert result["statuses"]["adoption"] == "adoption_unobserved"
    assert result["statuses"]["business_value"] == "business_value_unobserved"
    assert result["statuses"]["human_work"] == "unobserved"
    assert not result["technical_qualification_affected"] and not result["evidence_accepted"] and not result["authorized"]
    assert selected[1].calls == 0
    assert validate_document(REPORT, result) == result


def test_complete_original_customer_observations_have_only_scoped_exact_counts(selected):
    value = batch(selected[2], values=("1", "1"))
    observed = retain(selected, value)
    result = report(selected, observed)
    row = external(result)[0]
    assert row["total"] == "2" and row["rate"]["denominator"] == 10
    assert row["rate"]["numerator"] == "2"
    assert result["statuses"]["adoption"] == "adoption_observed"
    assert result["statuses"]["business_value"] == "business_value_unobserved"
    assert selected[1].calls == 3
    value["records"][0]["value"] = None
    assert observed["batch"]["records"][0]["value"] == "1"


@pytest.mark.parametrize("coverage,denominator,values", [
    ("partial", 10, ("1",)), ("unknown", 10, ("1",)), ("complete", None, ("1",)),
    ("complete", 10, (None,)), ("partial", 10, ()),
])
def test_unknown_or_partial_observations_never_supply_total_rate_or_population_zero(selected, coverage, denominator, values):
    observed = retain(selected, batch(selected[2], coverage=coverage, denominator=denominator, values=values))
    result = report(selected, observed)
    row = external(result)[0]
    assert row["total"] is None and row["rate"] is None
    assert result["statuses"]["adoption"] != "adoption_observed"


def test_complete_authenticated_empty_inventory_is_scoped_zero_only(selected):
    observed = retain(selected, batch(selected[2], metric="human_interventions", source=1, values=()))
    result = report(selected, observed)
    assert external(result)[0]["total"] == "0"
    assert result["statuses"]["human_interventions"] == "observed"
    assert result["statuses"]["human_work"] == "unobserved"
    assert result["statuses"]["adoption"] == "adoption_unobserved"


@pytest.mark.parametrize("source", [4, 5, 6])
def test_automated_and_simulated_classes_cannot_become_customer_or_human_activity(selected, source):
    with pytest.raises(EvidenceWikiError):
        retain(selected, batch(selected[2], source=source))
    with pytest.raises(EvidenceWikiError):
        retain(selected, batch(selected[2], source=source, metric="human_work_seconds", values=("17",)))
    observed = retain(selected, batch(selected[2], source=source, metric="technical_success"))
    result = report(selected, observed)
    assert result["evidence_classes"][selected[2].sources[source].evidence_class][0]["total"] == "1"
    assert not external(result) and result["statuses"]["adoption"] == "adoption_unobserved"


def test_mislabeled_synthetic_record_cannot_match_original_producer(selected):
    original = batch(selected[2], metric="sessions", source=6)
    selected[1].retain(original)
    mislabeled = deepcopy(original)
    mislabeled["source"] = selected[2].sources[0].document()
    with selected[0].activate(), pytest.raises(EvidenceWikiError, match="Outcome"):
        selected[2].ingest(mislabeled)


@pytest.mark.parametrize("field", ["binding", "scope_id", "source", "source_receipt_sha256", "batch_sha256", "controller_ref"])
def test_forged_or_misbound_verification_is_refused(selected, field):
    def alter(value):
        if field == "binding":
            value[field]["candidate_sha256"] = "sha256:" + "b" * 64
        elif field == "source":
            value[field]["origin"] = "unrelated-original"
        elif field.endswith("sha256"):
            value[field] = "sha256:" + "b" * 64
        else:
            value[field] = "unrelated"
        return value
    selected[1].alter = alter
    with pytest.raises(EvidenceWikiError):
        retain(selected, batch(selected[2]))


@pytest.mark.parametrize("clock", ["expired", "future", "excessive"])
def test_expiry_future_certificates_and_unbounded_validity_refused(selected, clock):
    def alter(value):
        now = datetime.now(timezone.utc)
        if clock == "expired":
            value.update(verified_at=(now - timedelta(minutes=2)).isoformat(), expires_at=(now - timedelta(minutes=1)).isoformat())
        elif clock == "future":
            value.update(verified_at=(now + timedelta(minutes=1)).isoformat(), expires_at=(now + timedelta(minutes=2)).isoformat())
        else:
            value["expires_at"] = (now + timedelta(hours=1)).isoformat()
        return value
    selected[1].alter = alter
    with pytest.raises(EvidenceWikiError):
        retain(selected, batch(selected[2]))


def test_serialized_prior_authentication_cannot_override_current_revocation(selected):
    observed = retain(selected, batch(selected[2]))
    selected[1].withdrawn = True
    with pytest.raises(EvidenceWikiError):
        report(selected, observed)


def test_withdrawal_at_closing_check_refuses_report(selected):
    observed = retain(selected, batch(selected[2]))
    original_verify = selected[1].verify
    def verify(request):
        if selected[1].calls == 2:
            selected[1].withdrawn = True
        return original_verify(request)
    object.__setattr__(selected[2].verifier, "verify", verify)
    with pytest.raises(EvidenceWikiError):
        report(selected, observed)


def test_duplicate_sessions_with_renamed_observation_and_batch_ids_are_idempotent(selected):
    original = batch(selected[2], metric="sessions")
    original["records"].append({**original["records"][0], "observation_id": "renamed-observation"})
    retained = retain(selected, original)
    alias = deepcopy(original)
    alias["batch_id"] = "renamed-batch"
    alias["records"] = [alias["records"][0]]
    alias["records"][0]["observation_id"] = "another-name"
    another = retain(selected, alias)
    result = report(selected, retained, retained, another)
    assert len(external(result)) == 1 and external(result)[0]["total"] == "1"


def test_customer_adoption_counts_unique_customer_identity_not_session_events(selected):
    value = batch(selected[2])
    value["records"].append({**value["records"][0], "observation_id": "other-session"})
    assert external(report(selected, retain(selected, value)))[0]["total"] == "1"
    value["records"][1]["entity_id"] = "other-session"
    with pytest.raises(EvidenceWikiError):
        retain(selected, value)


def test_conflicting_entity_aliases_and_reused_receipts_fail(selected):
    first = batch(selected[2], metric="sessions")
    altered = deepcopy(first)
    altered["batch_id"] = "another"
    altered["records"][0]["subject_id"] = "other-person"
    a, b = retain(selected, first), retain(selected, altered)
    with pytest.raises(EvidenceWikiError):
        report(selected, a, b)
    altered["records"][0]["entity_id"] = "another-session"
    b = retain(selected, altered)
    with pytest.raises(EvidenceWikiError):
        report(selected, a, b)


@pytest.mark.parametrize("change", ["gap", "exclusion", "denominator", "zero_denominator", "outside_period", "empty_period", "unknown_metric", "boolean", "customer_count"])
def test_unsupported_coverage_metrics_and_record_shapes_are_refused(selected, change):
    value = batch(selected[2])
    if change == "gap":
        value["gaps"] = ["missing-day"]
    elif change == "exclusion":
        value["exclusions"] = ["some-customers"]
    elif change == "denominator":
        value["denominator"]["unit"] = "agent-cases"
    elif change == "zero_denominator":
        value["denominator"]["value"] = 0
    elif change == "outside_period":
        value["records"][0]["occurred_at"] = value["period"]["ended_at"]
    elif change == "empty_period":
        value["period"]["ended_at"] = value["period"]["started_at"]
    elif change == "unknown_metric":
        value["metric"] = "willingness_to_pay"
    elif change == "boolean":
        value["authenticated"] = True
    else:
        value["records"][0]["value"] = "4"
    with pytest.raises(EvidenceWikiError):
        retain(selected, value)


def test_partial_gaps_exclusions_and_unknown_work_are_retained_without_promotion(selected):
    value = batch(selected[2], metric="human_work_seconds", source=1, coverage="partial", values=("7.25", None))
    value["gaps"], value["exclusions"] = ["unobserved-hour"], ["outside-selected-team"]
    result = report(selected, retain(selected, value))
    row = external(result)[0]
    assert row["known_subtotal"] == "7.25" and row["total"] is None and row["unknown_records"] == 1
    assert row["gaps"] == value["gaps"] and row["exclusions"] == value["exclusions"]
    assert result["statuses"]["human_hours_saved"] == "unobserved"


def test_payments_and_human_work_do_not_establish_business_value_willingness_or_savings(selected):
    pay = retain(selected, batch(selected[2], metric="payments", source=2, currency="USD", values=("0.123456789", "0.876543211")))
    work = retain(selected, batch(selected[2], metric="human_work_seconds", source=1, values=("3600.25",)))
    with localcontext() as decimal_context:
        decimal_context.prec = 2
        decimal_context.rounding = ROUND_UP
        decimal_context.traps[Inexact] = True
        result = report(selected, pay, work)
    rows = {row["metric"]: row for row in external(result)}
    assert rows["payments"]["total"] == "1" and rows["human_work_seconds"]["total"] == "3600.25"
    assert rows["payments"]["rate"] is None
    assert result["statuses"]["business_value"] == "business_value_unobserved"
    assert result["statuses"]["willingness_to_pay"] == result["statuses"]["human_hours_saved"] == "unobserved"


def test_absent_host_or_grant_and_arbitrary_verifier_json_cannot_ingest(selected):
    with pytest.raises(EvidenceWikiError):
        selected[2].ingest(batch(selected[2]))
    denied = HostContext(allowed_roots=(selected[2].root,), actor_ref="ungranted", grants=frozenset())
    with denied.activate(), pytest.raises(EvidenceWikiError):
        selected[2].report()
    with selected[0].activate(), pytest.raises(EvidenceWikiError):
        OutcomeIngestion(selected[2].root, "scope", selected[2].candidate_sha256, selected[2].sources,
                         {"authenticated": True})


def test_root_candidate_and_scope_binding_cannot_be_substituted(selected):
    value = batch(selected[2])
    selected[1].retain(value)
    for mutate in (lambda item: item.update(scope_id="other-scope"),
                   lambda item: item["binding"].update(candidate_sha256="sha256:" + "b" * 64),
                   lambda item: item["binding"]["workspace"].update(inode="0")):
        altered = deepcopy(value)
        mutate(altered)
        with selected[0].activate(), pytest.raises(EvidenceWikiError):
            selected[2].ingest(altered)


def test_cyclic_and_overlarge_record_inputs_terminate_without_callbacks(selected):
    value = batch(selected[2])
    value["records"].append(value)
    with selected[0].activate(), pytest.raises(EvidenceWikiError):
        selected[2].ingest(value)
    value = batch(selected[2])
    value["records"] *= 513
    with selected[0].activate(), pytest.raises(EvidenceWikiError):
        selected[2].ingest(value)
    assert selected[1].calls == 0


@pytest.mark.parametrize("kind", ["batch", "source", "record", "verification", "retained", "report"])
def test_closed_shapes_reject_unknown_fields_before_consumption(selected, kind):
    value = batch(selected[2])
    if kind in {"batch", "source", "record"}:
        target = value if kind == "batch" else value["source"] if kind == "source" else value["records"][0]
        target["authenticated"] = True
        with selected[0].activate(), pytest.raises(EvidenceWikiError):
            selected[2].ingest(value)
        assert selected[1].calls == 0
    elif kind == "verification":
        def alter(item):
            item["authenticated"] = True
            return item
        selected[1].alter = alter
        with pytest.raises(EvidenceWikiError):
            retain(selected, value)
    elif kind == "retained":
        observed = retain(selected, value)
        observed["authorized"] = True
        with pytest.raises(EvidenceWikiError):
            report(selected, observed)
    else:
        result = report(selected)
        result["business_value_verified"] = True
        with pytest.raises(EvidenceWikiError):
            validate_document(REPORT, result)


@pytest.mark.parametrize("field,replacement", [("records", {}), ("source", []), ("binding", None),
    ("denominator", {"value": True, "unit": "customers", "population_ref": "population"}),
    ("batch_id", "has a space"), ("period", {"started_at": "2026-02-30T00:00:00Z", "ended_at": "2026-03-01T00:00:00Z"})])
def test_malformed_closed_batch_structures_fail_before_verifier(selected, field, replacement):
    value = batch(selected[2])
    value[field] = replacement
    with selected[0].activate(), pytest.raises(EvidenceWikiError):
        selected[2].ingest(value)
    assert selected[1].calls == 0


def test_retained_batch_or_verification_tampering_never_reaches_host(selected):
    observed = retain(selected, batch(selected[2]))
    observed["batch"]["records"][0]["value"] = None
    with pytest.raises(EvidenceWikiError):
        report(selected, observed)
    assert selected[1].calls == 1
    observed = retain(selected, batch(selected[2]))
    del observed["verification"]["authentication_ref"]
    with pytest.raises(EvidenceWikiError):
        validate_document(RETAINED, observed)


@pytest.mark.parametrize("source", [ObservationSource([], "origin", "simulated", "simulation"),
    ObservationSource("id", "origin", "simulated", "customer"),
    ObservationSource("id", "origin", "unknown", "customer")])
def test_host_source_configuration_is_closed_and_class_consistent(selected, source):
    with selected[0].activate(), pytest.raises(EvidenceWikiError):
        OutcomeIngestion(selected[2].root, "scope", selected[2].candidate_sha256, (source,), selected[2].verifier)


def test_conflicting_inventory_and_overlapping_windows_fail_without_totals(selected):
    original = batch(selected[2], metric="sessions")
    changed = deepcopy(original)
    changed["batch_id"] = "different-inventory"
    changed["source_receipt"]["sha256"] = digest("different-original")
    changed["records"] = []
    a, b = retain(selected, original), retain(selected, changed)
    with pytest.raises(EvidenceWikiError):
        report(selected, a, b)
    changed["period"]["ended_at"] = "2026-10-03T00:00:00Z"
    b = retain(selected, changed)
    with pytest.raises(EvidenceWikiError):
        report(selected, a, b)


def test_incompatible_currency_and_population_periods_remain_separate_rows(selected):
    original = batch(selected[2], metric="payments", source=2, currency="USD", values=("3.50",))
    changed = deepcopy(original)
    changed["batch_id"] = "next-period"
    changed["currency"] = "EUR"
    changed["source_receipt"]["sha256"] = digest("second-period")
    changed["denominator"]["population_ref"] = "another-population"
    changed["period"] = {"started_at": "2026-10-03T00:00:00Z", "ended_at": "2026-10-04T00:00:00Z"}
    changed["records"] = [{**changed["records"][0], "entity_id": "another-payment", "occurred_at": "2026-10-03T12:00:00Z", "value": "2"}]
    a, b = retain(selected, original), retain(selected, changed)
    rows = external(report(selected, a, b))
    assert {(row["currency"], row["total"]) for row in rows} == {("USD", "3.5"), ("EUR", "2")}
    assert len(rows) == 2


@pytest.mark.parametrize("metric,source,currency,values", [("payments", 2, "USD", ("2.50", "3.50")),
    ("human_work_seconds", 1, None, ("2.50", "3.50")), ("customer_adoption", 0, None, ("1", "1"))])
def test_record_population_denominator_cannot_be_smaller_than_unique_inventory(selected, metric, source, currency, values):
    value = batch(selected[2], metric=metric, source=source, currency=currency, values=values, denominator=1)
    with selected[0].activate(), pytest.raises(EvidenceWikiError):
        selected[2].ingest(value)
    assert selected[1].calls == 0


def test_workspace_replacement_during_verification_fails_closing_scope(selected):
    original_verify = selected[1].verify
    def verify(request):
        result = original_verify(request)
        selected[2].root.rename(selected[2].root.with_name("old-workspace"))
        selected[2].root.mkdir()
        return result
    object.__setattr__(selected[2].verifier, "verify", verify)
    with pytest.raises(EvidenceWikiError):
        retain(selected, batch(selected[2]))


def test_ingest_checks_expiry_after_closing_scope_check(selected, monkeypatch):
    import evidence_wiki.outcome_observations as module

    fixed_now = datetime.now(timezone.utc)
    class HostClock:
        calls = 0
        @classmethod
        def now(cls, _zone):
            cls.calls += 1
            return fixed_now + timedelta(minutes=2 if cls.calls > 1 else 0)
    def alter(value):
        value.update(verified_at=(fixed_now - timedelta(seconds=1)).isoformat(),
                     expires_at=(fixed_now + timedelta(seconds=1)).isoformat())
        return value
    selected[1].alter = alter
    monkeypatch.setattr(module, "datetime", HostClock)
    with pytest.raises(EvidenceWikiError):
        retain(selected, batch(selected[2]))
