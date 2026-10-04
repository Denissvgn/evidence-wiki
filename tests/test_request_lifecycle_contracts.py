"""Closed strict lifecycle arguments cannot weaken native scope boundaries."""

import pytest

from evidence_wiki.mutation_contracts import MutationError, arguments


def test_strict_fulfillment_defaults_to_current_complete_scoped_evidence():
    value = arguments("source_requests.fulfill_current", {"request_id": "gap", "source_id": "source"})
    assert value["require_scope"] is True and value["needs_complete"] is True
    assert value["match_scope"] is None


@pytest.mark.parametrize("field,value", [("require_scope", False), ("require_scope", 1), ("callback", "execute"),
    ("authority", "approved"), ("source_revision", "sha256:" + "0" * 64)])
def test_strict_fulfillment_refuses_scope_downgrade_and_caller_authority(field, value):
    with pytest.raises(MutationError):
        arguments("source_requests.fulfill_current", {"request_id": "gap", "source_id": "source", field: value})


@pytest.mark.parametrize("operation,values", [
    ("source_requests.link_current", {"request_id": "gap", "question_slugs": ["question"], "actor": "other"}),
    ("questions.block_current", {"slug": "question", "blocking_request_ids": ["gap"], "reason": "Missing", "actor": "other"}),
    ("questions.reopen_current", {"slug": "question", "actor": "other"}),
])
def test_strict_lifecycle_cannot_select_an_actor_from_untrusted_arguments(operation, values):
    with pytest.raises(MutationError):
        arguments(operation, values)


def test_blocker_selector_bound_refuses_before_owner_dispatch():
    with pytest.raises(MutationError):
        arguments("questions.block_current", {"slug": "question", "blocking_request_ids": ["gap"] * 129,
            "reason": "Missing current evidence"})
