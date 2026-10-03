"""An embedding host coordinates canonical writes through public handles."""

import json

import pytest

from evidence_wiki import Workspace
from evidence_wiki.errors import ConfigError, EvidenceWikiError
from tests.test_mutation_operations import host, template
from tests.test_mutation_operations import roots as _roots


@pytest.fixture
def roots(tmp_path, monkeypatch):
    return _roots.__wrapped__(tmp_path, monkeypatch)


def test_public_mutation_round_trip_and_historical_replay(roots, capsys):
    root, _ = roots
    with host(roots).activate(), Workspace.open(root) as workspace:
        revision = workspace.mutations.revision()
        request = workspace.mutations.prepare(
            request_id="public-request", operation="coverage.init",
            arguments={"slug": "sample", "template": template()},
        )
        assert request["expected_revision"] == revision["revision_id"]
        receipt = workspace.mutations.submit(json.loads(json.dumps(request)))
        assert receipt["status"] == "committed"
        assert receipt["result"]["manifest"]["question_slug"] == "sample"
        before = (root / "sources/coverage/sample.yml").read_bytes()
        (root / "later-observation.txt").write_text("A later workspace revision.")
        assert workspace.mutations.submit(request) == receipt
        inspected = workspace.mutations.inspect("public-request")
        assert inspected["record"]["receipt"] == receipt
        assert workspace.mutations.reconcile("public-request") == receipt
        assert (root / "sources/coverage/sample.yml").read_bytes() == before
        assert receipt["historical"] and not receipt["authorized"] and not receipt["evidence_accepted"]
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("operation", ["revision", "prepare", "submit", "inspect", "reconcile"])
def test_every_mutation_method_checks_closed_handle_first(roots, operation):
    root, _ = roots
    workspace = Workspace.open(root)
    workspace.close()
    call = {
        "revision": lambda: workspace.mutations.revision(),
        "prepare": lambda: workspace.mutations.prepare(request_id="closed", operation="coverage.init", arguments={}),
        "submit": lambda: workspace.mutations.submit({}),
        "inspect": lambda: workspace.mutations.inspect("closed"),
        "reconcile": lambda: workspace.mutations.reconcile("closed"),
    }[operation]
    with pytest.raises(ConfigError) as error:
        call()
    assert error.value.error_code == "WORKSPACE_UNREADABLE"


def test_public_mutation_requires_explicit_host_selection_before_control_effects(roots):
    root, control = roots
    with Workspace.open(root) as workspace, pytest.raises(EvidenceWikiError) as error:
        workspace.mutations.revision()
    assert error.value.error_code == "MUTATION_AUTHORITY_REQUIRED"
    assert not list(control.iterdir())
