"""Public lifetime, exact read grants and malformed formats run no native research."""

from dataclasses import replace

import pytest

from evidence_wiki import Workspace, contract
from evidence_wiki.brief_contracts import schemas
from evidence_wiki.errors import ConfigError, UsageError
from evidence_wiki.readability_contracts import schemas as critic_schemas
from evidence_wiki.readability_plan import schemas as plan_schemas
from tests.test_research_session import files, host, policy
from tests.test_research_session import root as root


def selected(root, tmp_path, *, inner=True):
    value = host(root, tmp_path)
    grants = value.grants | {"research_session.brief", "research_session.resolution", "resolution_operations.current"}
    if inner:
        grants |= {"brief_operations.current"}
    return replace(value, grants=grants)


def test_detached_schema_and_capability_discovery_respect_workspace_lifetime(root, tmp_path):
    with selected(root, tmp_path).activate(), Workspace.open(root) as workspace:
        before = files(root)
        value = workspace.strict.brief_schemas()
        assert value == {**schemas(), **critic_schemas(), **plan_schemas()}
        value.clear()
        assert workspace.strict.brief_schemas() == {**schemas(), **critic_schemas(), **plan_schemas()}
        assert files(root) == before
    with pytest.raises(ConfigError):
        workspace.strict.brief_schemas()
    assert contract()["research_brief"]["formats"] == ["structured", "markdown"]


@pytest.mark.parametrize("format", [{}, [], None, True, "html"])
def test_bad_format_is_owned_refusal_before_owner_reads(root, tmp_path, format):
    with selected(root, tmp_path).activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy(), run_id="brief-read") as session:
            before = files(root)
            with pytest.raises(UsageError) as caught:
                session.brief(format=format)
            assert caught.value.error_code == "RESEARCH_BRIEF_REFUSED"
            assert caught.value.details["reason"] == "format_invalid"
            assert files(root) == before


def test_unbound_and_missing_inner_grant_cannot_dispatch_or_read(root, tmp_path):
    for inner, error in ((False, ConfigError), (True, UsageError)):
        with selected(root, tmp_path, inner=inner).activate(), Workspace.open(root) as workspace:
            with workspace.research.session(policy(), run_id="brief-read") as session:
                before = files(root)
                with pytest.raises(error):
                    session.brief()
                assert files(root) == before
