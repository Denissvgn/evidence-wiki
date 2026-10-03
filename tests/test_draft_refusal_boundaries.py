"""Admission preserves foreign refusal identity and precise error boundaries."""

from pathlib import Path

import pytest

from evidence_wiki.pack_discovery import owner


class ForeignRefusal(ValueError):
    error_code = "SOURCE_INPUT_LIMIT"
    text_line = "Retain the foreign refusal text."

    def to_envelope(self):
        return {"error_code": self.error_code, "message": str(self), "recoverable": False,
                "remediation": "Keep the original bound.", "details": {"origin": "foreign"}}


@pytest.fixture
def seam(request, monkeypatch):
    target = request.param
    admission = owner("_draft_admission")
    codec = owner("_draft_admission_contract")

    def invoke(error):
        def fail(*args, **kwargs):
            raise error
        if target == "decorator":
            return admission.typed(fail)()
        if target == "admit":
            monkeypatch.setattr(admission, "commit_admission", fail)
            return admission.run_admit_document(Path("."), {}, _plan={})
        if target == "validate":
            monkeypatch.setattr(codec, "_bounded", fail)
            return codec.validate_document(codec.PROPOSAL, {})
        monkeypatch.setattr(codec, "json_document", fail)
        return codec.decode_document(codec.PROPOSAL, b"{}")

    return target, invoke


@pytest.mark.parametrize("seam", ["decorator", "admit", "validate", "decode"], indirect=True)
def test_foreign_canonical_refusal_preserves_identity_envelope_and_text(seam):
    _, invoke = seam
    original = ForeignRefusal("A bounded foreign source refusal.")
    expected = original.to_envelope()
    with pytest.raises(ForeignRefusal) as error:
        invoke(original)
    assert error.value is original
    assert error.value.to_envelope() == expected
    assert error.value.text_line == "Retain the foreign refusal text."


@pytest.mark.parametrize("seam", ["decorator", "admit", "validate", "decode"], indirect=True)
def test_plain_malformed_data_retains_the_owning_admission_refusal(seam):
    target, invoke = seam
    with pytest.raises(Exception) as error:
        invoke(ValueError("Invalid ordinary data."))
    assert error.value.error_code == ("DRAFT_ADMISSION_REFUSED" if target in {"decorator", "admit"}
                                      else "DRAFT_ADMISSION_INVALID")
    expected_reason = {"decorator": "admission_input_invalid_or_unreadable",
                       "admit": "admission_input_or_publication_uncertain",
                       "validate": "document_invalid", "decode": "document_invalid"}[target]
    assert error.value.details["reason"] == expected_reason


@pytest.mark.parametrize("seam", ["decorator", "admit", "validate", "decode"], indirect=True)
@pytest.mark.parametrize("error", [RuntimeError("unexpected bug"), SystemExit(137)], ids=["bug", "process-control"])
def test_unexpected_bugs_and_owner_process_control_remain_untouched(seam, error):
    _, invoke = seam
    with pytest.raises(type(error)) as caught:
        invoke(error)
    assert caught.value is error
