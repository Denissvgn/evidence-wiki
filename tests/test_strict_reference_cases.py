"""Replay frozen reference judgments through actual strict acceptance and delivery."""

import json
from pathlib import Path

import pytest

from tests.test_strict_evidence import CORE, review, save_claims
from tests.test_strict_evidence import host as host

REFERENCE = json.loads((Path(__file__).parent / "fixtures/strict-evidence/review-cases.json").read_text())


@pytest.mark.parametrize("trial", range(REFERENCE["trials"]))
@pytest.mark.parametrize("case", REFERENCE["cases"], ids=lambda case: case["id"])
def test_frozen_reference_judgment_has_the_expected_delivery_outcome(host, case, trial):
    claim = host.claims["claims"][0]
    claim.update(text=case["text"], qualification=case["qualification"])
    save_claims(host)
    verdicts = dict.fromkeys(CORE.sibling("_strict_contract").CHECKS, "pass")
    verdicts.update(case["verdicts"])
    review(host, changes={"verdicts": verdicts})
    result = CORE.publication(host.root)
    actual = result["questions"][0]["accepted"]
    assert actual is case["accepted"], (case["id"], trial, result)
    assert (result["verdict"] == "ship") is case["accepted"]
    if not actual:
        assert result["questions"][0]["claims"] == []
        assert result["questions"][0]["gaps"]
