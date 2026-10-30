"""Independent actual capture extraction and current consumption attacks."""

import os

import pytest

from evidence_wiki import Workspace
from tests import test_native_capture_normalization as mechanics
from tests.test_mutation_operations import roots as roots

pytestmark = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd,
    reason="Native descriptor-relative normalization custody is unavailable.")


@pytest.mark.parametrize("format,raw", [
    ("csv", b"error,status_code\nAccess denied,403\n"),
    ("json", b'{"response":{"error":"Access denied","status_code":403}}'),
])
def test_structured_authentication_error_is_not_useful_source(roots, format, raw):
    source_id, _result, _verify = mechanics.normalize(roots, raw, format)
    with mechanics.selected(*roots).activate(), Workspace.open(roots[0]) as workspace:
        observed = workspace.sources.usability([source_id])
    row = observed["sources"][0]
    assert row["usability"] != "usable", row
    assert not row["complete"], row


def test_pdf_authentication_error_is_not_useful_source(roots):
    raw = mechanics.pdf_bytes("Access denied\nPlease sign in to continue.\n" * 12)
    source_id, _result, _verify = mechanics.normalize(roots, raw, "pdf")
    with mechanics.selected(*roots).activate(), Workspace.open(roots[0]) as workspace:
        observed = workspace.sources.usability([source_id])
    row = observed["sources"][0]
    assert row["usability"] != "usable", row
    assert not row["complete"], row
