"""Inert early refusals cannot establish native historical permissions."""

import copy

import pytest

import evidence_wiki.repair_history_permissions as history


@pytest.mark.parametrize("loss", ["missing", "record", "closure", "scope", "ambiguous"])
def test_changed_dependency_hides_historical_commitment_before_native_lookup(loss, monkeypatch):
    digest = "sha256:" + "a" * 64
    packet = {"source_id": "retained-source", "record_sha256": digest,
        "source_binding": {"source_id": "retained-source", "extent_ref": "/selected/raw", "closure_id": digest}}
    row = {"attempt": {"input": {"sources": [packet], "repair_scope": {"claims": [{
        "evidence": [{"source_id": "retained-source", "record_sha256": digest}]}]}}}}
    current = [copy.deepcopy(packet)]
    if loss == "missing":
        current.clear()
    elif loss == "record":
        current[0]["record_sha256"] = "sha256:" + "b" * 64
    elif loss == "closure":
        current[0]["source_binding"]["closure_id"] = "sha256:" + "b" * 64
    elif loss == "scope":
        current[0]["source_binding"]["extent_ref"] = "/foreign/raw"
    else:
        row["attempt"]["input"]["repair_scope"]["claims"][0]["evidence"].append({
            "source_id": "retained-source", "record_sha256": "sha256:" + "b" * 64})
    monkeypatch.setattr(history, "owner", lambda *_args: pytest.fail("Unsafe historical dependency reached native lookup"))
    assert history.permitted(None, row, None, current) is False
