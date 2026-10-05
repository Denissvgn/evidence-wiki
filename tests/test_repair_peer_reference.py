"""Native permitted peers retain their genuine earlier producer during repair."""

import json

from evidence_wiki import Workspace
from evidence_wiki import execution_session as native
from evidence_wiki.counter_authority import require_current_producer
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.pack_discovery import owner
from tests._repair_fixture import PEER_DETAIL, two_sources


def test_permitted_unrelated_peer_is_parent_only_preserved_and_not_repair_authored(tmp_path, monkeypatch):
    root, policy, execution, context, added = two_sources.__wrapped__(tmp_path, monkeypatch)
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="peer-preserving-repair", execution=execution) as session:
            final = session.run_until_terminal(request_id="repair", expected_basis_id=session.current()["basis_id"], max_steps=64)
            assert final["outcome"] == "accepted", final
            records = native.retained(session)
            repaired = next(row["data"] for row in records if row["kind"] == "execution_attempt" and row["data"]["purpose"] == "repair")
            attempt = execution.attempt(repaired["attempt"])
            import hashlib
            delivery = [row["data"] for row in records if row["kind"] == "execution_input_handoff"
                and row["data"]["attempt_id"] == attempt.attempt_id]
            assert delivery == [{"attempt_id": attempt.attempt_id, "job_sha256": attempt.job_sha256,
                "input_sha256": attempt.input_sha256, "worker_sha256": hashlib.sha256(native.canonical(attempt.worker_document())).hexdigest(),
                "boundary": "native_stdin_complete", "accepted": False}]
            assert PEER_DETAIL not in json.dumps(attempt.worker_document())
            assert "coverage" not in attempt.input_document
            assert len(repaired["repair_parent_inventory"]["claims"]) == 2
            assert added["sid"] in {row["source_id"] for row in repaired["repair_parent_sources"]}
            assert added["sid"] not in {row["source_id"] for row in attempt.input_document["sources"]}
            application = next(row["data"] for row in records if row["kind"] == "execution_application" and row["data"]["attempt_id"] == attempt.attempt_id)
            peer_before = next(c for c in repaired["repair_parent_inventory"]["claims"] if c["id"] == "unrelated-peer")
            peer_after = next(c for c in application["proposal"]["inventory"]["claims"] if c["id"] == "unrelated-peer")
            assert peer_after == peer_before
            peer_attempt = execution.attempt(next(row["data"]["attempt"] for row in reversed(records)
                if row["kind"] == "execution_attempt" and row["data"].get("claim_id") == "unrelated-peer"))
            prepared = owner("strict_evidence").prepare_review(root, "unrelated-peer")
            producer_ref = require_current_producer(session, peer_attempt, prepared["snapshot"]["claim"])
            generated = next(row["data"] for row in records if row["kind"] == "execution_attempt" and row["data"]["purpose"] == "generate")
            assert producer_ref == digest(generated["attempt"])
            assert producer_ref != digest(repaired["attempt"])
            progress = session.repair_progress()
            assert progress["rows"][0]["diff"] is not None
            assert progress["rows"][0]["fresh_independent_reviews"] == "complete"
            assert progress["rows"][0]["current_review"]["native_accepted"]
            replay = session.advance(request_id=final["request_id"], expected_basis_id=final["before_basis_id"])
            assert replay["historical"] and native.retained(session) == records
