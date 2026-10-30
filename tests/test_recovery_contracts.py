"""Recovery observations are closed and internally correlated inert metadata."""

import copy
import json

import pytest

from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.recovery_contracts import STATUS, checked


def document():
    return {"schema_version":STATUS, "run_id":"run", "binding_id":"sha256:" + "a" * 64,
        "current_basis_id":"sha256:" + "b" * 64, "journal_sha256":"sha256:" + "c" * 64,
        "terminal":False, "status":"not_started", "original_deadline_utc":None, "steps_used":0, "pending":None,
        "questions":[], "effects":[], "truncated":False, "next_sequence":0, "next_work":None,
        "authorized":False, "evidence_accepted":False}


def test_inert_recovery_roundtrip_and_detachment():
    value = document()
    decoded = checked(json.loads(json.dumps(value)))
    assert decoded == value
    decoded["questions"].append({})
    assert not value["questions"]


@pytest.mark.parametrize("change", [
    {"authorized":True}, {"evidence_accepted":True}, {"terminal":True}, {"extra":True},
    {"next_sequence":True}, {"steps_used":1}, {"original_deadline_utc":"2026-10-04"},
    {"pending":{}}, {"next_work":{}}, {"effects":[{"sequence":1}]},
    {"current_basis_id":"unknown"}, {"schema_version":"research-recovery-status/v2"},
])
def test_refuses_unknown_or_inconsistent_recovery_metadata(change):
    with pytest.raises(EvidenceWikiError):
        checked(document() | change)


def test_effect_cursor_must_be_ordered_unique_and_match_last_effect():
    value = document() | {"status":"reconcilable", "original_deadline_utc":"2026-10-04T00:00:00Z"}
    row = {"sequence":4, "attempt_id":"effect", "operation":"coverage.link", "arguments_sha256":"sha256:" + "d" * 64, "state":"unobserved"}
    value.update(effects=[row], next_sequence=4)
    checked(value)
    duplicated = copy.deepcopy(value)
    duplicated["effects"].append(copy.deepcopy(row))
    with pytest.raises(EvidenceWikiError):
        checked(duplicated)
    value["next_sequence"] = 3
    with pytest.raises(EvidenceWikiError):
        checked(value)


def test_installed_recovery_schema_is_in_the_complete_closed_catalog():
    from evidence_wiki._agent_catalog import resource_paths
    from evidence_wiki.agent_resources import MAX_INSTALLED_RESOURCES, MAX_RESOURCES, resource_document, resource_index
    from evidence_wiki.recovery_contracts import schema_document

    expected = set(resource_paths())
    assert len(expected) <= MAX_INSTALLED_RESOURCES
    entries, offset = [], 0
    while True:
        page = resource_index(offset=offset)
        returned, total = page["bounds"]["returned"], page["bounds"]["total"]
        assert total == len(expected)
        assert returned == len(page["resources"]) <= MAX_RESOURCES
        assert page["bounds"]["truncated"] is (returned < total)
        entries.extend(row["id"] for row in page["resources"])
        offset += returned
        if offset >= total:
            break
        assert returned > 0
    assert len(entries) == len(expected)
    assert set(entries) == expected
    assert json.loads(resource_document("research-recovery-status/v1")["content"]) == schema_document()
