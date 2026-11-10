"""An unknown semantic advisory cannot release a claim after three native passes.

The test runs a complete native research drive, so this module forms a suite group of its own.
"""

from __future__ import annotations

from evidence_wiki import Workspace
from evidence_wiki.qualification_calibration import digest
from evidence_wiki.semantic_contracts import DIMENSIONS
from tests._semantic_fixture import ORIGINAL, native_fixture


def test_public_unknown_advisory_cannot_release_after_three_native_passes(tmp_path, monkeypatch):
    root, policy, execution, context = native_fixture(tmp_path, monkeypatch, semantic_advisory="unknown")
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="semantic-unknown-advisory", execution=execution) as session:
            result = session.run_until_terminal(request_id="drive", expected_basis_id=session.current()["basis_id"], max_steps=64)
            assert result["terminal"] and result["outcome"] == "abstained"
            assert result["reason"] == "review_disagreement"
            records = execution.records(session)
            applications = [row["data"] for row in records if row["kind"] == "execution_application" and "semantic_assessment" in row["data"]]
            assert len([row for row in applications if row["operation"] == "evaluate" and row["passed"]]) == 3
            assert len([row for row in applications if row["operation"] == "adjudicate"]) == 1
            assert not session.current()["canonical_release"]["research_complete"]
            report = session.semantic_report()
            assert report["dimensions"]["support"]["unknown"] == 1
            assert report["dimensions"]["support"]["score"] is None
            for dimension in DIMENSIONS[3:]:
                assert report["dimensions"][dimension]["denominator"] == 0
            assert report["originals"][0]["original_text_sha256"] == "sha256:" + digest(ORIGINAL)
