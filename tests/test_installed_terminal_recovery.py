"""Canonical terminal recovery retains only fully settled historical success."""

import copy
import json
from pathlib import Path

import pytest

from evidence_wiki.errors import EvidenceWikiError
from tools._installed_receipts import PARENT_STAGES
from tools.probe_installed_public import checked_adapter_result

FIXTURE = Path(__file__).parent / "fixtures/installed-public/terminal-caller-result.json"


def result():
    """Public result clone with only temporary filesystem roots made portable."""
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("pending_kind", ["settled", "absent", "requested_race"])
def test_retained_public_result_accepts_exact_terminal_owner_views(pending_kind):
    observed = result()
    pending = observed["journey"]["recovery"]["result"]["pending"]
    if pending_kind == "absent":
        observed["journey"]["recovery"]["result"]["pending"] = None
    elif pending_kind == "requested_race":
        pending["cancellation_requested"] = True
    assert checked_adapter_result(observed, "current-caller") is observed


@pytest.mark.parametrize("state", ["running", "unknown", "failed", "cancelled", "expired", "unavailable", "dispatch_unknown"])
def test_terminal_transport_cannot_hide_unsettled_or_failed_historical_work(state):
    observed = result()
    observed["journey"]["recovery"]["result"]["pending"]["state"] = state
    with pytest.raises((ValueError,EvidenceWikiError)):
        checked_adapter_result(observed, "current-caller")


@pytest.mark.parametrize("field,value", [
    ("applied",False), ("applied",1), ("reservation_occupied",True), ("reservation_occupied",0),
    ("observation_ref",None), ("observation_ref",""), ("observation_ref","sha256:bad"),
    ("cancellation_confirmed",True), ("cancellation_confirmed",0), ("cancellation_requested",None),
    ("cancellation_requested",1), ("cancellation_requested","false"), ("undeclared",False),
    ("deadline_utc","not-a-clock"), ("attempt_id",None),
])
def test_historical_pending_requires_exact_schema_and_settlement(field,value):
    observed = result()
    observed["journey"]["recovery"]["result"]["pending"][field] = value
    with pytest.raises((ValueError,EvidenceWikiError)):
        checked_adapter_result(observed, "current-caller")


@pytest.mark.parametrize("field", ["applied","observation_ref","reservation_occupied","cancellation_requested","cancellation_confirmed"])
def test_historical_pending_cannot_drop_required_fields(field):
    observed = result()
    observed["journey"]["recovery"]["result"]["pending"].pop(field)
    with pytest.raises((ValueError,EvidenceWikiError)):
        checked_adapter_result(observed, "current-caller")


@pytest.mark.parametrize("field,value", [("terminal",False),("status","reconcilable"),("run_id","other"),
    ("binding_id","sha256:"+"0"*64),("authorized",True),("evidence_accepted",True),("undeclared",False)])
def test_settled_history_does_not_relax_current_run_identity_or_authority(field,value):
    observed = result()
    observed["journey"]["recovery"]["result"][field] = value
    with pytest.raises((ValueError,EvidenceWikiError)):
        checked_adapter_result(observed, "current-caller")


def test_exact_parent_inventory_uses_one_canonical_deploy_label():
    assert len(PARENT_STAGES) == 37
    assert PARENT_STAGES.count("evidence-wiki deploy --target") == 1
    assert "evidence-wiki deploy" not in PARENT_STAGES
    # Real parent records also carry the leading selected-runtime observation.
    expected = ["artifacts/selected-runtime-identity", *["wheel/"+name for name in PARENT_STAGES]]
    assert len(expected) == 38
    changed = copy.deepcopy(expected)
    changed[changed.index("wheel/evidence-wiki deploy --target")] = "wheel/evidence-wiki deploy"
    assert changed != expected
