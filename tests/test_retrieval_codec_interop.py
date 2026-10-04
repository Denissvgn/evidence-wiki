"""Public schema preserves native numeric ranking scores."""

import os

import pytest

from evidence_wiki import retrieval_operations as retrieval
from evidence_wiki.onboarding_contract import _matches
from tests.test_mutation_operations import roots as roots
from tests.test_native_capture_normalization import normalize
from tests.test_retrieval_operations import selected

pytestmark = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd,
    reason="Native descriptor-relative retrieval custody is unavailable.")


def test_actual_returned_numeric_score_matches_unmodified_public_schema(roots):
    source_id, _result, _verify = normalize(roots, b"Measured reflectance was 0.74 ratio.\n", "plain_text")
    with selected(roots).activate():
        view = retrieval.search(roots[0], query="reflectance", source_ids=[source_id], engine="fts")
    assert type(view["results"][0]["score"]) is float
    _matches(view, retrieval.schemas()[retrieval.SCHEMA])
