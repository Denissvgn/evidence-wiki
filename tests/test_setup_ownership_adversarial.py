"""Actual protected requirements receipts justify only exact setup-owned changes."""

import os

import pytest

from evidence_wiki import mutation_operations as mutations
from evidence_wiki import requirements_operations as operations
from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_contracts import canonical
from evidence_wiki.planning import compile_plan
from evidence_wiki.setup_application import revalidate
from evidence_wiki.setup_store import snapshot
from tests.test_mutation_store import request_directory
from tests.test_requirements_owner import roots as roots
from tests.test_requirements_owner import setup as setup
from tests.test_requirements_owner import submit
from tests.test_research_planning import request

pytestmark = pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd or os.link not in os.supports_dir_fd,
    reason="Actual native storage is unavailable on this platform.",
)


@pytest.mark.parametrize("changed", ["none", "owned-mode", "unowned-mode", "old-directory-mode", "extra-file", "extra-directory", "output-content", "missing-receipt"])
def test_exact_owned_revision_snapshot_cannot_cover_other_changes(setup, changed):
    root, control, _, _, selected, _ = setup
    previous = snapshot(root)
    submit(setup)
    if changed == "owned-mode":
        (root / operations.REQUIREMENTS).chmod(0o777)
    elif changed == "unowned-mode":
        (root / "research.yml").chmod(0o777)
    elif changed == "old-directory-mode":
        (root / "sources").chmod(0o777)
    elif changed == "extra-file":
        (root / "unowned.txt").write_bytes(b"unowned bytes")
    elif changed == "extra-directory":
        (root / "unowned-dir").mkdir()
    elif changed == "output-content":
        (root / "index.md").write_bytes(b"changed outside the owner")
    elif changed == "missing-receipt":
        (request_directory(control, root, "append1") / "receipt.json").unlink()
    with selected.activate():
        if changed == "missing-receipt":
            with pytest.raises(ExecutionError, match="receipt_missing"):
                operations.setup_changes_owned(root, previous, snapshot(root))
        else:
            assert operations.setup_changes_owned(root, previous, snapshot(root)) is (changed == "none")


def test_complete_outputs_with_unreconciled_failed_owner_are_not_owned_setup_changes(setup, monkeypatch):
    root, _, proposal, envelope, selected, _ = setup
    previous = snapshot(root)
    writer = operations.owner("_usage_materialization")
    actual = writer.publish_file
    def interrupted(*args, **kwargs):
        result = actual(*args, **kwargs)
        if args[1] == operations.REQUIREMENTS:
            raise OSError("Controlled native interruption after last publication")
        return result
    with selected.activate():
        request = mutations.prepare_request(root, request_id=proposal["request_id"], operation="requirements.revise",
            arguments={"proposal": proposal, "envelope": envelope})
        with monkeypatch.context() as scoped:
            scoped.setattr(writer, "publish_file", interrupted)
            receipt = mutations.submit(root, request)
        assert receipt["status"] in {"partial", "commit_unknown"}
        with pytest.raises(ExecutionError, match="receipt_missing"):
            operations.setup_changes_owned(root, previous, snapshot(root))


def test_read_only_revalidation_does_not_remove_apply_worker_containment(tmp_path):
    writable = tmp_path / "writable"
    writable.mkdir()
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    selected = HostContext([writable], [], "host", [writable], mutation_control_directory=control)
    with selected.activate():
        plan = compile_plan(canonical(request(writable)))
        assert revalidate(plan, read_only=True)["plan_id"] == plan["plan_id"]
        with pytest.raises(EvidenceWikiError) as refused:
            revalidate(plan)
        assert refused.value.error_code == "ONBOARDING_ENVIRONMENT_INCOMPATIBLE"
        assert refused.value.details["field"] == "setup_explicit_protected_worker_bridge_unavailable"
