"""The embedding boundary contains process exits without hiding caller errors."""

import traceback
from types import SimpleNamespace

import pytest

from evidence_wiki._facades._base import call_seam
from evidence_wiki.errors import EvidenceWikiError


@pytest.mark.parametrize("status", [None, 0, 3, True, {"private": "do-not-expose"}])
def test_process_exit_does_not_terminate_the_embedding_host(status):
    def operation():
        raise SystemExit(status)

    with pytest.raises(EvidenceWikiError) as error:
        call_seam(lambda _: SimpleNamespace(run=operation), "owner", "run")
    assert error.value.error_code == "LIBRARY_PROCESS_EXIT"
    assert error.value.exit_code == 2
    assert error.value.recoverable is False
    assert "do-not-expose" not in str(error.value) + repr(error.value.details)
    rendered = "".join(traceback.format_exception(error.value))
    assert "do-not-expose" not in rendered
    assert call_seam(lambda _: SimpleNamespace(run=lambda: {"alive": True}), "owner", "run") == {"alive": True}


def test_refusal_renderer_cannot_exit_the_host():
    class Refusal(Exception):
        error_code = "CONFIG_MISSING"

        def to_envelope(self):
            raise SystemExit(3)

    def operation():
        raise Refusal("Configuration is missing.")

    with pytest.raises(EvidenceWikiError) as error:
        call_seam(lambda _: SimpleNamespace(run=operation), "owner", "run")
    assert error.value.error_code == "CONFIG_MISSING"


def test_programming_error_stays_a_programming_error():
    def operation():
        raise TypeError("invalid caller argument")

    with pytest.raises(TypeError, match="invalid caller argument"):
        call_seam(lambda _: SimpleNamespace(run=operation), "owner", "run")


def test_renderer_and_stringifier_cannot_exit_during_translation():
    class Refusal(Exception):
        error_code = "CONFIG_MISSING"

        def to_envelope(self):
            raise SystemExit(3)

        def __str__(self):
            raise SystemExit(7)

    def operation():
        raise Refusal()

    with pytest.raises(EvidenceWikiError) as error:
        call_seam(lambda _: SimpleNamespace(run=operation), "owner", "run")
    assert error.value.error_code == "LIBRARY_PROCESS_EXIT"
    assert error.value.details["owner_exit_status"] == 7
    assert "SystemExit" not in "".join(traceback.format_exception(error.value))


def test_exit_payload_conversion_is_not_executed():
    class Status(int):
        def __int__(self):
            raise SystemExit(99)

    def operation():
        raise SystemExit(Status(3))

    with pytest.raises(EvidenceWikiError) as error:
        call_seam(lambda _: SimpleNamespace(run=operation), "owner", "run")
    assert error.value.error_code == "LIBRARY_PROCESS_EXIT"
    assert error.value.details["owner_exit_status"] is None
