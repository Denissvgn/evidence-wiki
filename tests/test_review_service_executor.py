"""Native no-start facts never replace unknown work after a launch boundary."""

import hashlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from evidence_wiki import (
    BoundedSubprocessExecutor,
    ExecutionAttempt,
    ExecutionProfile,
    ExecutionRole,
    ExecutionRoleLimit,
    ExecutionRoleRegistry,
    ExecutionRoleScope,
    ReviewServicePlan,
    ReviewServiceRoute,
)
from evidence_wiki.mutation_contracts import canonical
from evidence_wiki.workflow_contracts import JOB_V3
from tests.test_workflow_contracts import job


def configured(tmp_path, *, services=True, commands=None):
    from tests._usage_fixture import require_reference_executor
    require_reference_executor()
    roles = (ExecutionRole("producer", "host_worker", "producer-controller", "local", "prompt/v1"),
        ExecutionRole("review", "evaluator", "review-controller", "local", "prompt/v1"))
    registry = ExecutionRoleRegistry((ExecutionRoleScope("producer", "researcher", ("model",)),
        ExecutionRoleScope("review", "evaluator", ("model",))))
    plan = ReviewServicePlan((ReviewServiceRoute("review", "evaluator", ("review",),
        ExecutionRoleLimit("evaluator", "review-controller", 1, 1, 1000, "0", 60)),))
    profile = ExecutionProfile("controlled", 4, "EUR", "no-provider-charge", roles, 1, 1, 1000, "0",
        role_scopes=registry, review_services=plan if services else None)
    now = datetime.now(timezone.utc)
    value = job("evaluate")
    value.update(schema_version=JOB_V3, controller_ref="review-controller", prompt_version="prompt/v1",
        issued_utc=now.isoformat(), deadline_utc=(now+timedelta(seconds=10)).isoformat(), timeout_seconds=5,
        budgets=profile.document()["job_budgets"], expected_result_schema="research-execution-observation/v1", task_role="evaluator", required_capabilities=["model"],
        role_scope_sha256=registry.scope("review").sha256, context_id="neutral",
        packet_sha256=hashlib.sha256(canonical({})).hexdigest())
    attempt = ExecutionAttempt.create(attempt_id="controlled-attempt", nonce="nonce", run_id="run", binding_id="binding",
        profile=profile, config_ref="review", job=value, input_document={})
    return BoundedSubprocessExecutor(profile, commands=commands or {}, cwd=tmp_path, receipt_key=b"r"*32), attempt


def test_explicit_no_command_is_known_only_in_new_immutable_profile(tmp_path):
    host, attempt = configured(tmp_path)
    value = host.invoke(attempt)
    assert value["sequence"] == 1 and value["final_accounting"]
    assert value["observed_usage"] == {"seconds":0,"model_calls":0,"tool_calls":0,"tokens":0,"cost":"0"}
    assert host.authenticate(value, attempt) == value
    marker = tmp_path / "should-not-launch"
    host._commands["review"] = (sys.executable,"-c", "from pathlib import Path; Path("+repr(str(marker))+").write_text('bad')")
    assert host.invoke(attempt)["final_accounting"] and not marker.exists()
    assert host.poll(attempt)["final_accounting"] and host.cancel(attempt)["final_accounting"]
    legacy, old = configured(tmp_path, services=False)
    assert not legacy.invoke(old)["final_accounting"]
    assert legacy.invoke(old)["observed_usage"]["model_calls"] is None


def test_unknown_lost_poll_and_cancel_never_mint_zero(tmp_path):
    host, attempt = configured(tmp_path)
    for method in (host.poll, host.cancel):
        value = method(attempt)
        assert value["sequence"] == 0 and not value["final_accounting"]
        assert all(item is None for item in value["observed_usage"].values())


def test_postlaunch_oserror_is_unknown_despite_real_child_effect(tmp_path, monkeypatch):
    marker = tmp_path / "real-child-effect"
    host, attempt = configured(tmp_path, commands={"review":(sys.executable,"-c",
        "from pathlib import Path; Path("+repr(str(marker))+").write_text('observed')")})
    actual = subprocess.Popen
    children = []
    def after_launch(*args, **kwargs):
        child = actual(*args, **kwargs)
        children.append(child)
        child.wait(timeout=5)
        raise OSError("controlled postlaunch failure")
    monkeypatch.setattr(subprocess,"Popen",after_launch)
    try:
        value = host.invoke(attempt)
        assert marker.read_text() == "observed"
        assert not value["final_accounting"] and value["observed_usage"]["model_calls"] is None
        assert not host.poll(attempt)["final_accounting"]
    finally:
        for child in children:
            child.wait(timeout=5)
