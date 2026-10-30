"""An embedding host prepares retained evidence using only public handles."""

import base64

import pytest

from evidence_wiki import Onboarding, Workspace
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from tests.test_host_capture import profile
from tests.test_research_planning import request


def test_public_setup_capture_normalization_requests_and_coverage(tmp_path, capsys):
    selections = HostContext(
        [tmp_path], ["onboarding.plan", "onboarding.apply", "onboarding.capture", "workspace.open",
                    "source_inventory.run_inventory_report", "normalize_sources.run_normalization_report",
                    "source_requests.run_add_request", "source_requests.run_list_requests",
                    "source_requests.run_fulfill_request", "coverage_manifest.run_link",
                    "coverage_manifest.run_evaluate", "strict_evidence.run_operation.check",
                    "normalize_verify.run_verify"],
        "configured-worker", [tmp_path],
    )
    content = b"Measured solar reflectance was 0.74 for the retained surface, under the stated observation conditions.\r\n"
    root = tmp_path / "workspace"
    with Onboarding.open(allowed_roots=[tmp_path], allow=["apply", "capture"]) as setup, selections.activate():
        planned = setup.plan(request(tmp_path))
        applied = setup.apply(planned)
        assert applied["setup_ready"] and not applied["claims_verified"]
        with Workspace.open(root) as workspace:
            requested = workspace.source_requests.add(
                kind="web", query_or_identifier="https://example.org/study", rationale="Retain a declared evidence gap.",
                question_slugs=["q1"], scope={"facet_id": "primary", "jurisdiction": "Spain"},
            )
            request_id = requested["request"]["request_id"]
            captured = setup.capture(root, {
                "schema_version": "evidence-host-delivery/v1",
                "capture": profile(content, request_id=request_id,
                                   scope={"facet_id": "primary", "jurisdiction": "Spain"}),
                "content_base64": base64.b64encode(content).decode(),
            }, path="raw/web/capture.md")
            source_id = captured["source_id"]
            inventoried = workspace.sources.inventory(max_records=20)
            assert inventoried["total"] == 1
            normalized = workspace.normalize.sources([source_id], max_sources=20)
            assert normalized["summary"]["failed"] == 0
            checked = workspace.normalize.verify([source_id])
            assert checked["overall_result"] == "verified"
            fulfilled = workspace.source_requests.fulfill(
                request_id=request_id, source_id=source_id, require_scope=True,
                match_scope={"facet_id": "primary", "jurisdiction": "Spain"},
            )
            assert fulfilled["request"]["status"] == "fulfilled"
            linked = workspace.coverage.link("q1", "primary", accepted_source_ids=[source_id])
            assert linked["manifest"]["question_slug"] == "q1"
            coverage = workspace.coverage.evaluate("q1")
            assert coverage["manifest"]["question_slug"] == "q1"
            assert workspace.source_requests.list()["counts"]["fulfilled"] == 1
            with pytest.raises(EvidenceWikiError) as error:
                workspace.strict.check()
            assert error.value.error_code == "STRICT_EVIDENCE_REFUSED"
            assert error.value.details["reason"] == "strict_claims_missing_or_large"
    assert (root / "raw/web/capture.md").read_bytes() == content
    output = capsys.readouterr()
    assert output.out == output.err == ""
