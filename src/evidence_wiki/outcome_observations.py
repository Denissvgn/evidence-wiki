"""Authorized host ingestion of scoped, freshly authenticated outcome records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ._pack_io import identity
from .host_context import current_context
from .outcome_contracts import (
    BATCH,
    CLASSES,
    MAX_BATCHES,
    REPORT,
    RETAINED,
    VERIFICATION,
    clock,
    clone,
    refuse,
    validate_document,
    validate_source,
)
from .telemetry import _sum_values
from .telemetry_contracts import digest


@dataclass(frozen=True)
class ObservationSource:
    """Host-selected source origin and class, never a worker-selected trust route.

    This selection is not proof. The verifier must independently check the
    original producer, subject kind and measurement inventory. Retaining or
    re-signing simulated records cannot make them customer observations.
    """

    source_id: str
    origin: str
    evidence_class: str
    subject_kind: str

    def document(self):
        return {"source_id": self.source_id, "origin": self.origin,
                "evidence_class": self.evidence_class, "subject_kind": self.subject_kind}


@dataclass(frozen=True)
class ObservationVerifier:
    """Host-only finite callback checking actual protected source provenance.

    The callback receives the exact batch and source-receipt hash and must
    verify original producer authentication, origin/class, event identities,
    coverage, denominator and current revocation. Its return is a scoped
    verification document, never a caller authentication boolean. The host
    owns finite callback execution; core cannot interrupt arbitrary Python.
    """

    controller_ref: str
    verify: object = field(repr=False, compare=False)

    def __post_init__(self):
        if type(self.controller_ref) is not str or not 1 <= len(self.controller_ref) <= 256 or not callable(self.verify):
            refuse("VERIFIER_INVALID")


@dataclass(frozen=True)
class OutcomeIngestion:
    """Read-only host observation plan, independent of research acceptance.

    Ingest returns detached retention data; report freshly verifies it again.
    The application owns storage and verifier custody outside worker JSON.
    Reports attest scoped measurements at their closing verification time,
    not causal business value, customer truth beyond the verifier, or future
    eligibility. External observations never gate technical qualification.
    """

    root: Path
    scope_id: str
    candidate_sha256: str
    sources: tuple[ObservationSource, ...]
    verifier: ObservationVerifier = field(repr=False, compare=False)
    _binding: dict = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        if (type(self.sources) is not tuple or len(self.sources) > 32
                or any(type(source) is not ObservationSource for source in self.sources)
                or type(self.verifier) is not ObservationVerifier):
            refuse("PLAN_INVALID")
        root, _context = self._scope("ingest")
        object.__setattr__(self, "root", root)
        binding = {"workspace": {"path": str(root), **identity(root)}, "candidate_sha256": self.candidate_sha256}
        object.__setattr__(self, "_binding", binding)
        # The empty report schema bounds host selections without inventing events.
        self._empty_report(datetime.now(timezone.utc))
        source_ids, origins = set(), set()
        for source in self.sources:
            validate_source(source.document())
            if source.source_id in source_ids or source.origin in origins:
                refuse("SOURCE_DUPLICATE")
            source_ids.add(source.source_id)
            origins.add(source.origin)

    @property
    def binding(self):
        """Return the detached original workspace/candidate identity."""
        return clone(self._binding)

    def _scope(self, operation):
        context = current_context()
        if context is None:
            refuse("HOST_REQUIRED")
        context.require_grant("outcomes." + operation)
        root = context.require_path(Path(self.root).expanduser().absolute())
        if hasattr(self, "_binding") and {"path": str(root), **identity(root)} != self._binding["workspace"]:
            refuse("WORKSPACE_CHANGED")
        return root, context

    def _authenticate(self, batch):
        batch = validate_document(BATCH, batch)
        selected = next((source for source in self.sources if source.source_id == batch["source"]["source_id"]), None)
        if (selected is None or selected.document() != batch["source"] or batch["binding"] != self._binding
                or batch["scope_id"] != self.scope_id):
            refuse("SCOPE_CONFLICT")
        request = {"batch_sha256": digest(batch), "batch": clone(batch),
                   "controller_ref": self.verifier.controller_ref}
        try:
            verification = validate_document(VERIFICATION, self.verifier.verify(clone(request)))
        except (Exception, SystemExit):
            refuse("UNAUTHENTICATED")
        now = datetime.now(timezone.utc)
        if (verification["batch_sha256"] != request["batch_sha256"] or verification["binding"] != self._binding
                or verification["scope_id"] != self.scope_id or verification["source"] != selected.document()
                or verification["source_receipt_sha256"] != batch["source_receipt"]["sha256"]
                or verification["controller_ref"] != self.verifier.controller_ref
                or not clock(verification["verified_at"]) <= now < clock(verification["expires_at"])
                or clock(verification["expires_at"]) - clock(verification["verified_at"]) > timedelta(minutes=5)
                or clock(batch["period"]["ended_at"]) > now):
            refuse("UNAUTHENTICATED")
        return validate_document(RETAINED, {"schema_version": RETAINED, "batch": batch, "verification": verification})

    def ingest(self, batch):
        """Authenticate one exact batch under the currently granted host scope."""
        _root, context = self._scope("ingest")
        value = self._authenticate(batch)
        _root, closing = self._scope("ingest")
        if closing is not context:
            refuse("HOST_CHANGED")
        if clock(value["verification"]["expires_at"]) <= datetime.now(timezone.utc):
            refuse("EXPIRED")
        return value

    def _empty_report(self, now):
        return validate_document(REPORT, {"schema_version": REPORT, "scope_id": self.scope_id,
            "binding": self.binding, "evidence_classes": {name: [] for name in CLASSES}, "observations": [],
            "statuses": {"adoption": "adoption_unobserved", "business_value": "business_value_unobserved",
                "human_work": "unobserved", "human_interventions": "unobserved", "payments": "unobserved",
                "willingness_to_pay": "unobserved", "human_hours_saved": "unobserved"},
            "technical_qualification_affected": False, "authorized": False, "evidence_accepted": False,
            "measurement_authentication": "fresh_host_verification", "verified_at": now.isoformat(), "expires_at": None,
            "limitations": ["Authentication is only as strong as the host-selected original-source verifier.",
                "Counts and subtotals describe the exact selected source, period and population; they are not combined across scopes.",
                "Partial coverage, exclusions, gaps, unknown values and absent denominators do not establish totals or rates.",
                "Observed payments do not establish causal business value or willingness to pay.",
                "Agent and simulated work cannot establish customer adoption or human-hour savings.",
                "Verification is current only at the report closing check; serialized reports must be reverified."]})

    def report(self, observations=()):
        """Freshly authenticate retained batches and derive only scoped metrics.

        No input records is a valid report with unobserved adoption and business
        value. Declared or historical report flags are never trusted on reload.
        """
        _root, context = self._scope("report")
        if type(observations) not in (list, tuple) or len(observations) > MAX_BATCHES:
            refuse("LIMIT")
        retained = [validate_document(RETAINED, item) for item in observations]
        fresh = [self._authenticate(item["batch"]) for item in retained]
        report = self._empty_report(datetime.now(timezone.utc))
        groups, receipts, batch_ids, entities = {}, {}, {}, {}
        for observation in fresh:
            batch = observation["batch"]
            hashed = digest(batch)
            prior = batch_ids.setdefault(batch["batch_id"], hashed)
            if prior != hashed:
                refuse("BATCH_CONFLICT")
            source, period = batch["source"], batch["period"]
            key = (source["origin"], period["started_at"], period["ended_at"], batch["metric"], batch["currency"])
            descriptor = {name: batch[name] for name in ("source", "period", "metric", "unit", "currency",
                                                        "denominator", "coverage", "gaps", "exclusions")}
            unique = {}
            for record in batch["records"]:
                semantic = {name: value for name, value in record.items() if name != "observation_id"}
                entity = (source["origin"], batch["metric"], record["entity_id"])
                existing = entities.setdefault(entity, semantic)
                if existing != semantic:
                    refuse("RECORD_CONFLICT")
                unique[record["entity_id"]] = semantic
            signature = digest({"descriptor": descriptor, "records": [unique[name] for name in sorted(unique)]})
            receipt = batch["source_receipt"]["sha256"]
            prior_receipt = receipts.setdefault(receipt, signature)
            if prior_receipt != signature:
                refuse("RECEIPT_REUSED")
            existing_group = groups.get(key)
            if existing_group is not None:
                if existing_group["signature"] != signature:
                    refuse("INVENTORY_CONFLICT")
                if hashed not in existing_group["hashes"]:
                    existing_group["hashes"].append(hashed)
                continue
            for other in groups.values():
                prior_batch = other["descriptor"]
                if (prior_batch["source"]["origin"] == source["origin"] and prior_batch["metric"] == batch["metric"]
                        and clock(prior_batch["period"]["started_at"]) < clock(period["ended_at"])
                        and clock(period["started_at"]) < clock(prior_batch["period"]["ended_at"])):
                    refuse("PERIOD_OVERLAP")
            groups[key] = {"descriptor": descriptor, "signature": signature, "records": unique, "hashes": [hashed]}
        for group in groups.values():
            row = dict(group["descriptor"])
            records = list(group["records"].values())
            known = [record["value"] for record in records if record["value"] is not None]
            unknown = len(records) - len(known)
            subtotal = _sum_values(known)
            complete = row["coverage"] == "complete" and not unknown and row["denominator"] is not None
            status = "observed" if complete else "partial" if known else "unknown"
            row.update(observed_records=len(records), unknown_records=unknown, known_subtotal=subtotal,
                       total=subtotal if complete else None, rate=None, status=status, batch_sha256s=sorted(group["hashes"]))
            if complete and row["unit"] == "count":
                row["rate"] = {"numerator": subtotal, "denominator": row["denominator"]["value"],
                               "population_ref": row["denominator"]["population_ref"]}
            report["evidence_classes"][row["source"]["evidence_class"]].append(row)
        external = report["evidence_classes"]["externally_observed"]
        for metric, category in (("customer_adoption", "adoption"), ("human_work_seconds", "human_work"),
                                 ("human_interventions", "human_interventions"), ("payments", "payments")):
            rows = [row for row in external if row["metric"] == metric]
            status = "observed" if rows and all(row["status"] == "observed" for row in rows) else "partial" if any(
                row["observed_records"] > row["unknown_records"] for row in rows) else "unobserved"
            report["statuses"][category] = "adoption_" + status if category == "adoption" else status
        # A fresh closing call detects withdrawal during reduction; old receipts
        # and detached callback mutations cannot renew measurement eligibility.
        report["observations"] = [self._authenticate(item["batch"]) for item in fresh]
        _root, closing = self._scope("report")
        if closing is not context:
            refuse("HOST_CHANGED")
        now = datetime.now(timezone.utc)
        expiries = [clock(item["verification"]["expires_at"]) for item in report["observations"]]
        if expiries and min(expiries) <= now:
            refuse("EXPIRED")
        report["verified_at"] = now.isoformat()
        report["expires_at"] = min(expiries).isoformat() if expiries else None
        return validate_document(REPORT, report)
