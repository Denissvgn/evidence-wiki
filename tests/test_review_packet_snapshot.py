"""One protected read preserves exact packet checks and current owner custody."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from evidence_wiki import Workspace
from evidence_wiki._pack_io import canonical, json_document
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.mutation_store import capture_revision
from evidence_wiki.pack_discovery import owner
from evidence_wiki.review_packet_execution import context_parameters, current_assembly
from evidence_wiki.review_page_session import assembled
from tests._execution_fixture import authenticate, closure
from tests._temporal_fixture import clock_claim
from tests._usage_fixture import UsageFixture
from tests.test_review_packet_execution import configured as configured
from tests.test_review_page_owner import draft_root as draft_root
from tests.test_review_page_owner import native_case as native_case
from tests.test_review_page_owner import protected as protected


@pytest.fixture
def packet_case(protected):
    return public_packet(protected)


def public_packet(protected):
    root, _normalized, claim, context, _authority, _body, operations = protected
    parameters = {"claim_id": claim["id"], "context_id": "context-" + "a" * 32,
        "task_role": "evaluator", "page_chars": 16384,
        "source_ids": [row["source_id"] for row in claim["evidence"]], "_order_seed": b"a" * 32}
    with context.activate():
        handle = operations.prepare(root, **parameters)
        header = handle.header()
        pages = [handle.page(row["page_id"]) for row in header["pages"]]
        packet = {"schema_version": "research-review-packet/v1", "header": header,
            "pages": pages, "complete": handle.complete(pages)}
    assert packet["complete"]["complete"]
    return protected, parameters, packet


@pytest.fixture
def temporal_case(native_case, tmp_path, monkeypatch, request):
    started = datetime.now(timezone.utc)
    usage = owner("_evidence_usage")

    class Clock:
        value = started

        @classmethod
        def now(cls, zone):
            return cls.value

    monkeypatch.setattr(usage, "datetime", Clock)
    original = UsageFixture.transact
    captured = {}

    def deposit(self, module, action, body=None, files=None):
        if action == "deposit":
            descriptor = json_document(files["source-record.json"])
            descriptor["temporal"] = {"schema_version": "evidence-temporal-source/v1", "asserting_principal": "runner",
                "measurement": None, "published_at": clock_claim((started - timedelta(minutes=1)).isoformat()),
                "available_at": clock_claim((started - timedelta(minutes=1)).isoformat()),
                "claimed_retrieved_at": clock_claim(None), "effective": None,
                "expires_at": clock_claim((started + timedelta(hours=1)).isoformat()),
                "supersedes": None, "record_path": None, "provenance_path": None}
            if request.param == "effective":
                descriptor["temporal"]["expires_at"] = clock_claim((started + timedelta(hours=2)).isoformat())
                descriptor["temporal"]["effective"] = {"start": clock_claim((started - timedelta(minutes=1)).isoformat()),
                    "end": clock_claim((started + timedelta(hours=1)).isoformat())}
            files["source-record.json"] = canonical(descriptor)
            body["source_revision"] = closure(files)
            body["grant"] = authenticate({**body["grant"]["payload"], "source_revision": body["source_revision"]}, "owner", "usage")
            body["scrub"] = authenticate({**body["scrub"]["payload"], "sanitized_revision": body["source_revision"]}, "runner", "scrubber")
            captured.update(body=deepcopy(body), files=deepcopy(files))
        return original(self, module, action, body, files)

    with monkeypatch.context() as construction:
        construction.setattr(UsageFixture, "transact", deposit)
        selected = protected.__wrapped__(native_case, tmp_path, monkeypatch)
    root, normalized, _claim, context, authority, body, _operations = selected
    if request.param == "correction":
        files = captured["files"]
        descriptor = json_document(files["source-record.json"])
        descriptor["temporal"]["supersedes"] = body["source_revision"]
        descriptor["temporal"]["available_at"] = clock_claim((started + timedelta(minutes=10)).isoformat())
        files["source-record.json"] = canonical(descriptor)
        files[normalized.relative_to(root).as_posix()] += b"\nUpcoming correction.\n"
        correction = deepcopy(body)
        correction["source_revision"] = closure(files)
        correction["grant"] = authenticate({**correction["grant"]["payload"], "source_revision": correction["source_revision"]}, "owner", "usage")
        correction["scrub"] = authenticate({**correction["scrub"]["payload"], "sanitized_revision": correction["source_revision"]}, "runner", "scrubber")
        with context.activate():
            authority.transact(usage, "deposit", correction, files)
    return public_packet(selected), Clock, started, request.param


def count_snapshots(monkeypatch, operations):
    actual = operations.snapshot
    calls = []

    @contextmanager
    def counted(*args, **kwargs):
        calls.append((args, kwargs))
        with actual(*args, **kwargs) as value:
            yield value

    monkeypatch.setattr(operations, "snapshot", counted)
    return calls


def test_combined_read_matches_public_packet_and_has_no_effect(packet_case, monkeypatch):
    protected, parameters, packet = packet_case
    root, _normalized, _claim, context, authority, _body, operations = protected
    before = capture_revision(root)["revision_id"]
    private_before = (authority.host / "evidence-state.json").read_bytes()
    expected = deepcopy(packet)
    calls = count_snapshots(monkeypatch, operations)
    with context.activate(), operations._current_packet(root, packet=packet, **parameters) as approval:
        assert approval is None
    assert len(calls) == 1 and packet == expected
    assert capture_revision(root)["revision_id"] == before
    assert (authority.host / "evidence-state.json").read_bytes() == private_before
    with context.activate():
        handle = operations.prepare(root, **parameters)
        assert handle.header() == packet["header"]
        assert handle.complete(packet["pages"]) == packet["complete"]
    assert len(calls) == 4


@pytest.mark.parametrize("change", ["missing", "text", "duplicate", "foreign", "header", "complete", "page_bound"])
def test_combined_read_rejects_altered_or_incomplete_delivery(packet_case, change):
    protected, parameters, packet = packet_case
    root, _normalized, _claim, context, _authority, _body, operations = protected
    if change == "missing":
        packet["pages"].pop()
    elif change == "text":
        packet["pages"][0]["text"] += "Changed decisive evidence."
    elif change == "duplicate":
        packet["pages"].append(deepcopy(packet["pages"][0]))
    elif change == "foreign":
        packet["pages"][0]["context_id"] = "context-" + "b" * 32
    elif change == "header":
        packet["header"]["basis_id"] = "sha256:" + "0" * 64
    elif change == "complete":
        packet["complete"]["accepted"] = True
    else:
        packet["pages"] = [packet["pages"][0]] * 513
    with context.activate(), pytest.raises(EvidenceWikiError):
        with operations._current_packet(root, packet=packet, **parameters):
            pytest.fail("Changed delivery escaped validation")


@pytest.mark.parametrize("grant", ["review_page_operations.prepare", "review_page_operations.page"])
def test_combined_read_requires_both_original_grants(packet_case, grant):
    protected, parameters, packet = packet_case
    root, _normalized, _claim, context, _authority, _body, operations = protected
    selected = replace(context, grants=context.grants - {grant})
    with selected.activate(), pytest.raises(EvidenceWikiError):
        with operations._current_packet(root, packet=packet, **parameters):
            pytest.fail("Missing source access grant reached packet comparison")


@pytest.mark.parametrize("change", ["material", "withdrawal", "expiry", "context"])
def test_combined_read_revalidates_after_projection_before_return(packet_case, monkeypatch, change):
    protected, parameters, packet = packet_case
    root, normalized, _claim, context, authority, body, operations = protected
    usage = owner("_evidence_usage")
    activation = None
    with context.activate():
        try:
            with pytest.raises((EvidenceWikiError, ValueError)):
                with operations._current_packet(root, packet=packet, **parameters):
                    if change == "material":
                        normalized.write_bytes(normalized.read_bytes() + b"\nChanged after projection.\n")
                    elif change == "withdrawal":
                        authority.revoke(usage, body, whole=True)
                    elif change == "expiry":
                        class ExpiredClock:
                            @classmethod
                            def now(cls, zone):
                                return datetime(2027, 1, 2, tzinfo=timezone.utc)
                        monkeypatch.setattr(usage, "datetime", ExpiredClock)
                    else:
                        activation = replace(context, actor_ref="other-owner").activate()
                        activation.__enter__()
        finally:
            if activation is not None:
                activation.__exit__(None, None, None)


@pytest.mark.parametrize("change", ["material", "withdrawal"])
def test_combined_read_reconstructs_fresh_custody_on_every_call(packet_case, change):
    protected, parameters, packet = packet_case
    root, normalized, _claim, context, authority, body, operations = protected
    with context.activate():
        with operations._current_packet(root, packet=packet, **parameters):
            pass
        if change == "material":
            normalized.write_bytes(normalized.read_bytes() + b"\nChanged between calls.\n")
        else:
            authority.revoke(owner("_evidence_usage"), body, whole=True)
        with pytest.raises((EvidenceWikiError, ValueError)):
            with operations._current_packet(root, packet=packet, **parameters):
                pytest.fail("Old packet approval survived a custody change")


def test_combined_read_rejects_verified_source_aging_after_projection(packet_case, monkeypatch):
    protected, parameters, packet = packet_case
    root, _normalized, claim, context, _authority, _body, operations = protected
    observed = datetime.fromisoformat(claim["evidence"][0]["observed_at"].replace("Z", "+00:00"))
    before = capture_revision(root)["revision_id"]
    with context.activate(), pytest.raises(EvidenceWikiError) as refused:
        with operations._current_packet(root, packet=packet, **parameters):
            monkeypatch.setattr(owner("_claim_diagnostics").sibling("_draft_admission"), "current_time", lambda: observed + timedelta(days=366))
    assert refused.value.details["reason"] == "source_age_invalid"
    assert capture_revision(root)["revision_id"] == before


@pytest.mark.parametrize("age_changes", [False, True])
def test_nonverified_diagnostics_use_current_owner_fallback_before_return(protected, monkeypatch, age_changes):
    root, _normalized, _claim, context, _authority, _body, operations = protected
    config = yaml.safe_load((root / "research.yml").read_bytes())
    config["strict_evidence"]["max_source_age_seconds"] = 3600
    (root / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    path = root / config["strict_evidence"]["claims_path"]
    inventory = json_document(path.read_bytes())
    evidence = inventory["claims"][0]["evidence"][0]
    evidence["quote"] = "A quote absent from the current source."
    path.write_bytes(canonical(inventory))
    _protected, parameters, packet = public_packet(protected)
    actual = operations.claim_diagnostics.current
    calls = []

    def current(*args, **kwargs):
        value = actual(*args, **kwargs)
        calls.append(value)
        return value

    monkeypatch.setattr(operations.claim_diagnostics, "current", current)
    before = capture_revision(root)["revision_id"]
    with context.activate():
        if age_changes:
            observed = datetime.fromisoformat(evidence["observed_at"].replace("Z", "+00:00"))
            with pytest.raises(EvidenceWikiError) as refused:
                with operations._current_packet(root, packet=packet, **parameters):
                    draft = owner("_claim_diagnostics").sibling("_draft_admission")
                    future = observed + timedelta(hours=2)
                    monkeypatch.setattr(draft, "current_time", lambda: future)
                    class Clock:
                        @classmethod
                        def now(cls, zone):
                            return future
                    monkeypatch.setattr(draft.sibling("_evidence_usage"), "datetime", Clock)
                    monkeypatch.setattr(owner("_claim_diagnostics").sibling("_evidence_usage"), "datetime", Clock)
                    monkeypatch.setattr(owner("_evidence_usage"), "datetime", Clock)
            assert refused.value.details["reason"] == "mechanical_context_changed"
            assert calls[-1]["claims"][0]["evidence"][0]["reason"] == "source_age_invalid"
        else:
            with operations._current_packet(root, packet=packet, **parameters):
                pass
    assert len(calls) == 3
    assert calls[0]["claims"][0]["evidence"][0]["reason"] == "mechanical_grounding_unmatched"
    assert capture_revision(root)["revision_id"] == before


@pytest.mark.parametrize("temporal_case", ["expiry", "effective", "correction"], indirect=True)
def test_combined_read_closes_native_temporal_validity_without_byte_changes(temporal_case):
    (protected, parameters, packet), clock, started, change = temporal_case
    root, _normalized, _claim, context, authority, _body, operations = protected
    before = capture_revision(root)["revision_id"]
    private_before = (authority.host / "evidence-state.json").read_bytes()
    with context.activate(), pytest.raises(EvidenceWikiError) as refused:
        with operations._current_packet(root, packet=packet, **parameters):
            clock.value = started + timedelta(hours=1, seconds=1)
    assert refused.value.details["reason"] == {"expiry": "strict_source_expired",
        "effective": "strict_source_outside_effective_interval", "correction": "assessment_source_superseded"}[change]
    assert capture_revision(root)["revision_id"] == before
    assert (authority.host / "evidence-state.json").read_bytes() == private_before


def test_current_assembly_uses_one_snapshot_and_retains_exact_public_projection(configured, monkeypatch):
    from evidence_wiki import review_page_operations as operations
    root, policy, execution, context, claim, _captured = configured
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="snapshot-packet", execution=execution) as session:
            delivered = assembled(session, request_id="complete-pages", claim_id=claim["id"], page_chars=16384)
            parameters = context_parameters(session, "complete-pages")
            handle = operations.prepare(root, claim_id=claim["id"], context_id=parameters["context_id"],
                source_ids=[row["source_id"] for row in session.current()["execution"]["source_bindings"]],
                page_chars=16384, _order_seed=parameters["order_seed"])
            assert handle.header() == delivered["packet"]["header"]
            assert handle.complete(delivered["packet"]["pages"]) == delivered["packet"]["complete"]
            before = execution.records(session)
            calls = count_snapshots(monkeypatch, operations)
            checked = current_assembly(session, "complete-pages", claim_id=claim["id"], task_role="evaluator")
            assert len(calls) == 1
            assert checked["packet"] == delivered["packet"] and checked["packet_sha256"] == delivered["packet_sha256"]
            assert execution.records(session) == before
            checked["packet"]["pages"].pop()
            assert current_assembly(session, "complete-pages", claim_id=claim["id"], task_role="evaluator")["packet"] == delivered["packet"]
            assert len(calls) == 2
