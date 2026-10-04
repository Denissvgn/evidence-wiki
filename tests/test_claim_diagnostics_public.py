"""Public native diagnostics remain observations before strict admission."""

from dataclasses import replace

import pytest

from evidence_wiki import Workspace
from evidence_wiki.claim_diagnostics import schema_document
from evidence_wiki.errors import ConfigError
from evidence_wiki.onboarding_contract import _matches
from tests.test_draft_admission import draft_root as draft_root
from tests.test_draft_admission import host, observations


def selected(case):
    original=host(case)
    return replace(original,grants=original.grants|{"claim_diagnostics.current"})


def test_public_per_claim_current_diagnostics_match_schema_without_effects(draft_root):
    root,_control,proposal,_record=draft_root
    with selected(draft_root).activate(),Workspace.open(root) as workspace:
        before=observations(root)
        result=workspace.strict.diagnose(proposal)
        _matches(result,schema_document())
        assert result["claims"][0]["mechanical_result"]=="verified"
        assert result["claims"][0]["semantic_approval"]=="not_evaluated"
        assert not result["accepted"] and not result["published"]
        assert observations(root)==before
    with pytest.raises(ConfigError):workspace.strict.diagnose(proposal)


def test_public_diagnostics_require_owning_read_grant_before_native_owner(draft_root):
    root,_control,proposal,_record=draft_root
    before=observations(root)
    with host(draft_root).activate(),Workspace.open(root) as workspace:
        with pytest.raises(ConfigError) as caught:workspace.strict.diagnose(proposal)
        assert caught.value.error_code=="HOST_CONTEXT_REFUSED"
    assert observations(root)==before
