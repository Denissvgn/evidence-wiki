"""Private originals retain exact bytes and fail closed on malformed custody."""

import copy
from types import SimpleNamespace

import pytest

from evidence_wiki._review_dependency_custody import _decoded, _encoded, _hint
from evidence_wiki._review_dependency_inputs import assert_current, current_inputs
from evidence_wiki.errors import EvidenceWikiError


@pytest.mark.parametrize("raw", [b"", b"\x00\xff\r\nraw\n", "Exact café\r\n".encode(), b"x" * 100000])
def test_private_original_codec_round_trips_exact_bytes_with_bounded_chunks(raw):
    encoded = _encoded(raw)
    assert _decoded(encoded) == raw
    assert all(0 < len(chunk) <= 65536 and len(chunk) % 4 == 0 for chunk in encoded["raw_base64"])
    assert all(len(chunk) == 65536 for chunk in encoded["raw_base64"][:-1])


@pytest.mark.parametrize("change", [
    lambda row: row.update(bytes=row["bytes"] + 1),
    lambda row: row.update(sha256="sha256:" + "0" * 64),
    lambda row: row.update(raw_base64=["eA==\n"]),
    lambda row: row.update(raw_base64=["eA==="]),
    lambda row: row.update(raw_base64=["eB=="]),
    lambda row: row.update(raw_base64=["eA==", "eA=="]),
    lambda row: row.update(raw_base64="eA=="),
    lambda row: row.update(extra="unadmitted"),
])
def test_private_original_codec_rejects_forgery_noncanonical_and_ambiguous_chunks(change):
    encoded = copy.deepcopy(_encoded(b"x"))
    change(encoded)
    with pytest.raises(EvidenceWikiError):
        _decoded(encoded)


def test_public_configuration_shape_cannot_mint_an_owner_holder():
    assert current_inputs() is None
    with pytest.raises(EvidenceWikiError):
        assert_current(SimpleNamespace(public_profile=lambda: {}, role_semantics=lambda: []))


def test_binding_hint_refuses_a_caller_control_directory_without_active_owner(tmp_path):
    fake = SimpleNamespace(context=SimpleNamespace(mutation_control_directory=tmp_path))
    with pytest.raises(EvidenceWikiError):
        _hint(fake, "sha256:" + "0" * 64, value={"schema_version": "evidence-review-delivery-binding/v1"})
    assert list(tmp_path.iterdir()) == []
