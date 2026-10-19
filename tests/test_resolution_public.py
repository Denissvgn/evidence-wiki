"""Public shape, lifetime and owning grants convey no execution authority."""

from dataclasses import replace

import pytest

from evidence_wiki import Workspace, contract
from evidence_wiki.errors import ConfigError, UsageError
from evidence_wiki.resolution_contracts import DEPENDENCY_RESOLUTION, RESOLUTION, checked, schemas
from evidence_wiki.resolution_projection import _project
from tests.test_research_session import files, host, policy
from tests.test_research_session import root as root
from tests.test_resolution_projection import inputs


def selected(root, tmp_path, *, owner_grant=True):
    value = host(root, tmp_path)
    grants = value.grants | {"research_session.resolution"}
    if owner_grant:
        grants |= {"resolution_operations.current"}
    return replace(value, grants=grants)


def test_public_schema_detached_current_and_workspace_lifetime(root, tmp_path):
    with selected(root, tmp_path).activate(), Workspace.open(root) as workspace:
        before = files(root)
        returned = workspace.strict.resolution_schemas()
        assert returned == schemas() and set(returned) == {RESOLUTION, DEPENDENCY_RESOLUTION}
        returned[RESOLUTION]["required"].clear()
        assert workspace.strict.resolution_schemas() == schemas()
        assert files(root) == before
    with pytest.raises(ConfigError):
        workspace.strict.resolution_schemas()
    description = contract()["research_resolution"]
    assert description["schema"] == RESOLUTION
    assert description["grants"] == ["research_session.resolution", "resolution_operations.current"]


def test_unbound_execution_is_refused_without_native_work(root, tmp_path):
    with selected(root, tmp_path).activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy(), run_id="read-only") as session:
            before = files(root)
            with pytest.raises(UsageError) as caught:
                session.resolution()
            assert caught.value.details["reason"] == "bound_execution_session_required"
            assert files(root) == before
        with pytest.raises(UsageError):
            session.resolution()


def test_resolution_requires_inner_owning_grant_before_read(root, tmp_path):
    with selected(root, tmp_path, owner_grant=False).activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy(), run_id="read-only") as session:
            before = files(root)
            with pytest.raises(ConfigError) as caught:
                session.resolution()
            assert caught.value.error_code == "HOST_CONTEXT_REFUSED"
            assert files(root) == before


def test_closed_projection_never_accepts_private_native_proof_or_reusable_publication():
    value = _project(**inputs())
    assert checked(value) == value
    assert value["original_outcomes"][0]["questions"][0]["current_checks"]["required_reviews"] == "current_owned_pass"
    value["publication_basis_id"] = "sha256:" + "a" * 64
    with pytest.raises(UsageError):
        checked(value)
    value = _project(**inputs())
    value["original_outcomes"][0]["eligible_questions"][0]["claims"][0]["verification"] = {"basis_id": "private"}
    with pytest.raises(UsageError):
        checked(value)
