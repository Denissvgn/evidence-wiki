"""Public mechanical anchor diagnostics reuse actual native structured records."""

import copy
import hashlib
from datetime import datetime, timezone

import pytest

from evidence_wiki import Workspace, claim_diagnostics
from evidence_wiki.host_context import HostContext
from evidence_wiki.onboarding_contract import _matches
from evidence_wiki.pack_discovery import owner
from tests import test_draft_admission as admission
from tests.test_native_capture_revision import metadata


@pytest.fixture
def scoped(tmp_path, monkeypatch):
    value = admission.draft_root.__wrapped__(tmp_path, monkeypatch)
    grants = admission.GRANTS | {"claim_diagnostics.current", "source_capture_operations.deliver",
        "source_inventory.run_inventory_report", "normalize_sources.run_normalization_report"}
    context = HostContext([value[0]], grants, "caller", [value[0]], mutation_control_directory=value[1])
    return value, context


@pytest.mark.parametrize("format,raw,pointer", [("json", b'{"value":3,"unit":"ratio"}', "/data/value"),
    ("csv", b"value,unit\n3,ratio\n", "/rows/0/value")])
@pytest.mark.parametrize("fault", ["none", "wrong_value", "missing_pointer"])
def test_actual_public_structured_anchor_result_never_accepts_claim(scoped, format, raw, pointer, fault):
    (root, _control, proposal, _record), context = scoped
    with context.activate(), Workspace.open(root) as workspace:
        observed_at = datetime.now(timezone.utc).isoformat()
        delivery = workspace.sources.capture_bytes_document(raw, metadata(format, retrieved_at=observed_at,
            scope={"subject":"Synthetic study"}))
        prepared = workspace.sources.prepare_capture(request_id="structured-capture", path="raw/web/structured." + format,
            delivery=delivery)
        captured = workspace.sources.capture_revision(prepared)
        source_id = captured["result"]["source_id"]
        workspace.mutations.submit(workspace.mutations.prepare(request_id="structured-inventory", operation="sources.inventory", arguments={}))
        workspace.normalize.sources(source_ids=[source_id])
        normalized = root / "sources/normalized" / (owner("normalize_sources").safe_source_id(source_id) + ".md")
        proposal = copy.deepcopy(proposal)
        evidence = proposal["inventory"]["claims"][0]["evidence"][0]
        evidence.update(source_id=source_id, record_sha256="sha256:" + hashlib.sha256(normalized.read_bytes()).hexdigest(),
            observed_at=observed_at, quote=None, location_hint=None,
            anchor={"pointer":pointer if fault != "missing_pointer" else "/missing", "expected":"3" if fault != "wrong_value" else "4"})
        before = owner("_evidence_revision").capture_workspace(root).revision_id
        result = workspace.strict.diagnose(proposal)
        _matches(result, claim_diagnostics.schema_document())
        assert owner("_evidence_revision").capture_workspace(root).revision_id == before
        item = result["claims"][0]["evidence"][0]
        assert item["form"] == "anchor" and item["location_hint"] is None
        assert item["anchor_pointer"] == evidence["anchor"]["pointer"]
        assert item["mechanical"]["match_type"] is None and item["mechanical"]["occurrence_count"] is None
        assert item["result"] == ("verified" if fault == "none" else "failed")
        assert item["mechanical"]["result"] == {"none":"verified", "wrong_value":"anchor_value_mismatch",
            "missing_pointer":"anchor_pointer_not_found"}[fault]
        assert "resolved" not in item["mechanical"] and "expected" not in item["mechanical"]
        assert result["accepted"] is False and result["published"] is False
        assert result["claims"][0]["accepted"] is False
