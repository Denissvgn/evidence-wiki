"""Actual native delivery uses reference processes and explicitly simulated meters."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from evidence_wiki import Workspace
from evidence_wiki import execution_session as native
from evidence_wiki.errors import UsageError
from tests.test_execution_session import fixture


@pytest.mark.parametrize("interruption", [None, "lost_return", "lost_usage", "unchecked_link"])
def test_current_native_brief_is_published_once_before_terminal(tmp_path, monkeypatch, interruption):
    root, policy, execution, context = fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param={
        "role_isolation": True, "simulated_model_usage": True, "disagreement": True, "artifact_delivery": True}))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="delivered", execution=execution) as session:
            before = session.current()
            append = native.append
            interrupted = []

            class LostReturn(BaseException):
                pass

            if interruption == "lost_usage":
                from evidence_wiki import artifact_release
                prepare = artifact_release.prepare
                probed = []
                def hostile_proposals(selected):
                    value = prepare(selected)
                    if not probed:
                        probed.append(True)
                        artifact = value["artifact"]
                        proposal = {"schema_version": "research-artifact-delivery-proposal/v1",
                            "artifact_id": artifact["artifact_id"], "artifact_sha256": artifact["artifact_sha256"],
                            "artifact_bytes": artifact["bytes"], "plan_sha256": value["plan_sha256"],
                            "destination_ref": value["approved_destination_refs"][0]}
                        for name, hostile, reason in (
                                ("output", proposal | {"artifact_id": "sha256:" + "b" * 64,
                                    "artifact_sha256": "sha256:" + "b" * 64}, "delivery_proposal_changed"),
                                ("destination", proposal | {"destination_ref": "unapproved"}, "destination_not_approved")):
                            saved = native.retained(selected)
                            with pytest.raises(UsageError) as caught:
                                selected.deliver(request_id="hostile-" + name, proposal=hostile)
                            assert caught.value.details["reason"] == reason
                            assert native.retained(selected) == saved
                            assert not (root / "runs/delivered/deliveries/brief.md").exists()
                    return value
                monkeypatch.setattr(artifact_release, "prepare", hostile_proposals)
            if interruption in {"lost_return", "lost_usage"}:
                def lose_return(selected, kind, identifier, data):
                    if kind == "execution_native_observation" and type(data.get("result")) is dict and data["result"].get("operation") == "artifact.deliver":
                        interrupted.append(data["observation"]["attempt_id"])
                        if interruption == "lost_usage":
                            unknown = selected.execution.native_observation(selected, interrupted[-1], seconds=0,
                                succeeded=True, invocation_observed=False)
                            append(selected, kind, identifier, data | {"observation": unknown,
                                "invocation_proof": "observed_owner_call_with_unsettled_publication", "active_execution_seconds": None})
                        raise LostReturn()
                    return append(selected, kind, identifier, data)
                monkeypatch.setattr(native, "append", lose_return)
            elif interruption == "unchecked_link":
                from evidence_wiki.artifact_publication import _PublicationWitness
                verify = _PublicationWitness.verify_link
                def lose_link_check(writer):
                    verify(writer)
                    interrupted.append(writer.store.intent["request_id"])
                    raise LostReturn()
                monkeypatch.setattr(_PublicationWitness, "verify_link", lose_link_check)
            if interruption is not None:
                with pytest.raises(LostReturn):
                    session.run_until_terminal(request_id="drive", expected_basis_id=before["basis_id"], max_steps=64)
                monkeypatch.setattr(native, "append", append)
                request_id = interrupted[0]
                output = root / "runs/delivered/deliveries/brief.md"
                raw = output.read_bytes()
                saved_records = native.retained(session)
                with pytest.raises(UsageError) as inspection:
                    workspace.mutations.inspect(request_id)
                assert inspection.value.error_code == "MUTATION_OWNER_PRIVATE_READ_REQUIRED"
                for operation in ("invoke", "poll", "cancel", "authenticate"):
                    monkeypatch.setattr(execution.executor, operation, lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Receipt recovery invoked host work.")))
                current = session.delivery_status(request_id=request_id)
                assert current["artifact"] is None and current["current_eligibility"] == "unavailable"
                assert native.retained(session) == saved_records
                settled = session.reconcile_delivery(request_id=request_id)
                recovered = native.retained(session)
                assert output.read_bytes() == raw
                if interruption == "unchecked_link":
                    assert settled["status"] == "commit_unknown" and settled["artifact"] is None
                    assert recovered == saved_records
                    assert session.reconcile_delivery(request_id=request_id) == settled
                    assert native.retained(session) == recovered
                    return
                observations = [row["data"] for row in recovered if row["kind"] == "execution_native_observation"
                    and row["data"]["observation"]["attempt_id"] == request_id]
                assert len(observations) == (2 if interruption == "lost_usage" else 1)
                final = observations[-1]
                assert final["observation"]["final_usage"]
                assert final["invocation_proof"] == "protected_committed_owner_receipt"
                assert final["elapsed_scope"] == "utc_lifetime_including_recovery_wait"
                assert final["observation"]["usage"]["tool_calls"] == 1
                assert final["observation"]["correction"] is (interruption == "lost_usage")
                session.reconcile_delivery(request_id=request_id)
                assert native.retained(session) == recovered
            terminal = session.run_until_terminal(request_id="drive", expected_basis_id=before["basis_id"], max_steps=64)
            assert terminal["terminal"] and terminal["outcome"] == "accepted", (terminal, [row["data"] for row in native.retained(session)
                if row["kind"] in {"execution_reservation_failed", "execution_application_failed"}])
            records = native.retained(session)
            proposals = [row for row in records if row["kind"] == "execution_application" and row["data"].get("operation") == "release"]
            assert len(proposals) == 1 and not proposals[0]["data"]["accepted"]
            observations = [row["data"] for row in records if row["kind"] == "execution_native_observation"
                and row["data"].get("result", {}).get("operation") == "artifact.deliver"]
            assert len(observations) == (2 if interruption == "lost_usage" else 1)
            assert observations[-1]["result"]["status"] == "committed", observations
            output = root / "runs/delivered/deliveries/brief.md"
            raw = output.read_bytes()
            assert "sha256:" + hashlib.sha256(raw).hexdigest() == proposals[0]["data"]["proposal"]["artifact_sha256"]
            assert raw.startswith(b"# Evidence brief")
            assert not list(output.parent.glob(".host-capture-*"))
            for operation in ("invoke", "poll", "cancel", "authenticate"):
                monkeypatch.setattr(execution.executor, operation, lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Terminal replay invoked host work.")))
            replay = session.run_until_terminal(request_id="drive", expected_basis_id=before["basis_id"], max_steps=64)
            assert replay["historical"] is True
            assert {key: value for key, value in replay.items() if key != "historical"} == {
                key: value for key, value in terminal.items() if key != "historical"}
            assert native.retained(session) == records and output.read_bytes() == raw
            if interruption == "lost_usage":
                from evidence_wiki.mutation_contracts import canonical
                from evidence_wiki.pack_discovery import owner
                from tests._execution_fixture import authenticate
                request_id = observations[-1]["observation"]["attempt_id"]
                usage = owner("_evidence_usage")
                config = owner("_strict_evidence").configuration(root)
                with usage.current_view(root, config) as view:
                    checkpoint = view.state.checkpoint
                    revision, source = next(iter(view.state.revisions.items()))
                usage.transact(root, config, authenticate({
                    "schema_version": "evidence-usage-command/v1", "state_id": usage.selection(config),
                    "workspace_binding": usage.workspace_binding(root), "request_id": "withdraw-delivered-source",
                    "expected_checkpoint": checkpoint, "action": "revoke",
                    "body": {"source_id": source["source_id"], "scope": "revision", "source_revision": revision,
                        "reason": "owner-withdrawal"}}, "owner", "revocation"))
                denied = session.delivery_status(request_id=request_id)
                assert denied["status"] == "unavailable" and denied["current_eligibility"] == "unavailable"
                assert all(denied[name] is None for name in ("artifact", "destination_ref", "receipt_ref"))
                assert session.reconcile_delivery(request_id=request_id)["reasons"] == ["terminal_accounting_immutable"]
                assert native.retained(session) == records and output.read_bytes() == raw
                authority = json.loads(context.authority_file.read_bytes())
                authority["revoked_keys"] = ["owner-key"]
                context.authority_file.write_bytes(canonical(authority))
                for operation in (session.delivery_status, session.reconcile_delivery):
                    with pytest.raises(UsageError) as withdrawn:
                        operation(request_id=request_id)
                    assert withdrawn.value.error_code == "RESEARCH_BINDING_CHANGED"
                assert native.retained(session) == records and output.read_bytes() == raw
