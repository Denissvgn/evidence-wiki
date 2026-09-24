"""Original DOCX bytes, exact scalar cells, bounded parsing and native retrieval."""

import contextlib
import hashlib
import io
import json
from datetime import datetime, timezone

import pytest
import yaml

from evidence_wiki import Workspace
from evidence_wiki.pack_discovery import owner
from tests._docx_fixture import document
from tests.test_pack_migrations import initialize


def test_docx_text_and_table_values_are_not_inferred_or_rounded():
    result = owner("_docx_capture").extract(document())
    assert result["text"].startswith("Northern observations remain limited.")
    assert result["structured"]["tables"][0]["rows"][1] == ["007", "0.10"]


@pytest.mark.parametrize("body", [
    '<w:p><w:r><w:drawing/></w:r></w:p>',
    '<w:p><w:del><w:r><w:t>Old claim</w:t></w:r></w:del></w:p>',
    '<w:p><w:r><w:instrText>INCLUDETEXT external</w:instrText></w:r></w:p>',
    '<w:p><w:r><w:t>Visible</w:t></w:r></w:p><w:sectPr><w:headerReference/></w:sectPr>',
    '<w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>Ambiguous</w:t></w:r></w:p></w:tc></w:tr></w:tbl>',
    '<w:p><w:hidden><w:t>Unknown visibility</w:t></w:hidden></w:p>',
    '<w:p><w:r><w:t>Visible</w:t></w:r></w:p><w:tbl><w:unknown><w:p><w:r><w:t>Omitted</w:t></w:r></w:p></w:unknown></w:tbl>',
])
def test_unsupported_layout_and_revision_content_does_not_become_evidence(body):
    with pytest.raises(ValueError):
        owner("_docx_capture").extract(document(body))


@pytest.mark.parametrize("extra", [
    {"../escape": "untrusted"}, {"word/vbaProject.bin": "macro"},
    {"word/document.xml": '<!DOCTYPE x [<!ENTITY injected "unsupported">]><x>&injected;</x>'},
    {"word/document.xml": b'\xff\xfe' + '<!DOCTYPE x [<!ENTITY injected "unsupported">]><x>&injected;</x>'.encode('utf-16le')},
])
def test_container_paths_macros_and_xml_entities_are_refused(extra):
    with pytest.raises(ValueError):
        owner("_docx_capture").extract(document(extra=extra))


def test_docx_inventory_normalization_verification_and_retrieval(tmp_path):
    target = initialize(tmp_path / "workspace")
    original = document()
    path = target / "raw/papers/observations.docx"
    path.write_bytes(original)
    path.with_name(path.name + ".provenance.yml").write_text(yaml.safe_dump({
        "checksum": "sha256:" + hashlib.sha256(original).hexdigest(), "retrieved_by": "local_setup",
        "retrieved_at": datetime.now(timezone.utc).isoformat(), "source_type": "local_file", "license": "MIT"}))
    with contextlib.redirect_stdout(io.StringIO()):
        assert owner("source_inventory").main(["--project-root", str(target), "--format", "json", "--report"]) == 0
        records = [json.loads(line) for line in (target / "sources/manifest.jsonl").read_text().splitlines()]
        record = next(row for row in records if row["kind"] == "docx")
        assert owner("normalize_sources").main(["--project-root", str(target), "--source-id", record["id"], "--format", "json"]) == 0
    with Workspace.open(target) as workspace:
        report = workspace.normalize.verify([record["id"]])
    assert report["overall_result"] == "verified", report
    result = owner("serve_mcp").ResearchWikiMcpServer(target).call_tool_payload("query_index", {"query": "Northern observations", "scope": "normalized", "limit": 5})
    assert "Northern observations" in json.dumps(result)
    assert path.read_bytes() == original
