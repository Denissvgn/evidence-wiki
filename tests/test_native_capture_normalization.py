"""Actual pilot normalization preserves originals and refuses incomplete sources."""

import hashlib
import os

import pytest

from evidence_wiki import Workspace
from evidence_wiki import source_capture_contracts as codec
from evidence_wiki import source_capture_operations as capture
from evidence_wiki.host_context import HostContext
from evidence_wiki.pack_discovery import owner
from tests.test_capture_public_api import metadata
from tests.test_mutation_operations import GRANTS
from tests.test_mutation_operations import roots as roots

pytestmark = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd,
    reason="Native descriptor-relative normalization custody is unavailable.")


def selected(root, control):
    return HostContext([root], GRANTS | {capture.GRANT, "source_usability_operations.current"}, "host", [root], mutation_control_directory=control)


def normalize(roots, raw, format, *, complete="complete"):
    root, control = roots
    with selected(root, control).activate(), Workspace.open(root) as workspace:
        supplied = metadata(format, completeness=complete)
        suffix = {"plain_text":"txt", "markdown":"md", "csv":"csv", "json":"json", "pdf":"pdf"}[format]
        path = "raw/web/captured."+suffix
        delivered = workspace.sources.capture_revision(workspace.sources.prepare_capture(request_id="capture", path=path, delivery=codec.from_bytes(raw, supplied)))
        source_id = delivered["result"]["source_id"]
        workspace.mutations.submit(workspace.mutations.prepare(request_id="inventory", operation="sources.inventory", arguments={}))
        result = workspace.mutations.submit(workspace.mutations.prepare(request_id="normalize", operation="normalize.sources", arguments={"source_ids":[source_id]}))
        verify = workspace.normalize.verify([source_id])
    assert (root / path).read_bytes() == raw and hashlib.sha256(raw).hexdigest() == delivered["result"]["content_sha256"][7:]
    return source_id, result, verify


@pytest.mark.parametrize("format,raw", [("plain_text",b"Measured reflectance was 0.74 for the selected specimen.\n"),
    ("json",b'{"value":3,"unit":"ratio","scope":"sample"}'), ("csv",b"value,unit,scope\n3,ratio,sample\n")])
def test_pilot_content_and_native_record_verify(roots,format,raw):
    source_id,result,verify = normalize(roots,raw,format)
    assert result["status"] == "committed",result
    assert verify["overall_result"] == "verified",verify
    row=verify["records"][0]
    assert row["source_id"]==source_id


def test_units_missing_is_partial_and_not_usable_complete(roots):
    source_id,result,verify=normalize(roots,b"value,scope\n3,sample\n","csv")
    assert result["status"] == "partial",result
    root,_=roots
    normalizer=owner("normalize_sources")
    path=root/"sources/normalized"/(normalizer.safe_source_id(source_id)+".md")
    metadata,_,error=owner("_normalized_contract").split_record(path.read_text())
    assert error is None and metadata["status"]=="partial" and metadata["evidence_usable"] is False
    assert "structured_measurement_units_missing" in metadata["unusable_evidence_reasons"]


def test_authentication_shell_keeps_original_but_cannot_be_full_evidence(roots):
    source_id,result,verify=normalize(roots,b"Access denied\nPlease sign in to continue.\n","plain_text")
    root,_=roots
    path=root/"sources/normalized"/(owner("normalize_sources").safe_source_id(source_id)+".md")
    metadata,body,error=owner("_normalized_contract").split_record(path.read_text())
    assert metadata["evidence_usable"] is False and metadata["status"]=="partial"
    assert "Access denied" in body


def pdf_bytes(text):
    import io
    pypdf = pytest.importorskip("pypdf", reason="Optional PDF capability unavailable; no installation requested.")
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=600,height=800)
    font = DictionaryObject({NameObject("/Type"):NameObject("/Font"),NameObject("/Subtype"):NameObject("/Type1"),NameObject("/BaseFont"):NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):DictionaryObject({NameObject("/F1"):writer._add_object(font)})})
    stream = DecodedStreamObject()
    escaped=text.replace("\\","\\\\").replace("(","\\(").replace(")","\\)")
    stream.set_data(("BT /F1 12 Tf 50 700 Td ("+escaped+") Tj ET").encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    target=io.BytesIO();writer.write(target)
    return target.getvalue()


def test_readable_pdf_retains_page_location_and_does_not_reexecute_on_verification(roots,monkeypatch):
    import json
    text="Observed material reflectance was 0.74 ratio for the selected specimen. " * 4
    source_id,result,verify=normalize(roots,pdf_bytes(text),"pdf")
    assert result["status"]=="committed",result
    assert verify["overall_result"]=="verified",verify
    root,control=roots
    normalizer=owner("normalize_sources")
    file=root/"sources/normalized"/(normalizer.safe_source_id(source_id)+".md")
    metadata,body,error=owner("_normalized_contract").split_record(file.read_text())
    document=json.loads((root/metadata["structured_view"]["path"]).read_bytes())
    assert document["pages"][0]["page"]==1 and "0.74 ratio" in document["pages"][0]["text"]
    assert document["qualification"]=="mechanical_stored_consistency"
    assert metadata["title_source"]=="host_capture" and metadata["title_confidence"]=="low"
    monkeypatch.setattr(normalizer,"extract_pdf",lambda *a,**k:pytest.fail("Read-only verification executed PDF parser"))
    with selected(root,control).activate(),Workspace.open(root) as workspace:
        assert workspace.normalize.verify([source_id])["overall_result"]=="verified"


def test_partial_capture_never_becomes_complete_through_json_or_table_extraction(roots):
    source_id,result,verify=normalize(roots,b'{"value":3,"unit":"ratio"}',"json",complete="partial")
    assert result["status"]=="partial",result
    from evidence_wiki.source_inputs import SourceView
    from evidence_wiki.source_readiness import inspect_sources
    view=SourceView(roots[0])
    row=inspect_sources(view,source_ids=[source_id])[0]
    assert not row["complete"] and row["usability"] != "usable"
    view.finish()


def normalized_record(root, source_id):
    path=root/"sources/normalized"/(owner("normalize_sources").safe_source_id(source_id)+".md")
    return path,owner("_normalized_contract").split_record(path.read_text())


def test_malformed_pdf_records_finite_extraction_failure_without_metadata_fulltext(roots):
    source_id,result,verify=normalize(roots,b"%PDF-1.7\ninvalid bounded original\n%%EOF\n","pdf")
    assert result["status"]=="partial",result
    assert "capture_pdf_extraction_failed" in str(result)
    assert not normalized_record_exists(roots[0],source_id)
    assert verify["overall_result"]!="verified",verify


def normalized_record_exists(root,source_id):
    return (root/"sources/normalized"/(owner("normalize_sources").safe_source_id(source_id)+".md")).exists()


def test_image_only_pdf_remains_ocr_gap_and_stored_failure(roots):
    source_id,result,verify=normalize(roots,pdf_bytes(""),"pdf")
    assert result["status"]=="partial",result
    path,(frontmatter,body,error)=normalized_record(roots[0],source_id)
    assert error is None and frontmatter["needs_ocr"] is True and frontmatter["evidence_usable"] is False
    assert frontmatter["status"] in {"failed","partial"}
    assert "capture_pdf_text_incomplete_requires_ocr" in frontmatter["unusable_evidence_reasons"]
    from evidence_wiki.source_inputs import SourceView
    view=SourceView(roots[0])
    record=view.manifest()[source_id]
    view.finish()
    assert owner("_host_capture_normalization").issues(roots[0],record,frontmatter,body)==[]


@pytest.mark.parametrize("format,raw,pointer,expected",[
    ("json",b'{"specimen":{"value":0.74,"unit":"ratio"}}',"/data/specimen/value",0.74),
    ("csv",b"value,unit,scope\n007,ratio,\"two\nlines\"\n", "/rows/0/value","007"),
])
def test_original_structured_locations_are_exact_and_untyped_cells_preserved(roots,format,raw,pointer,expected):
    source_id,result,verify=normalize(roots,raw,format)
    assert result["status"]=="committed",result
    path,(frontmatter,body,error)=normalized_record(roots[0],source_id)
    structured=owner("_structured_view")
    sidecar=structured.load_sidecar(frontmatter,roots[0]/frontmatter["structured_view"]["path"])
    assert sidecar.ok
    resolved=structured.resolve_pointer(sidecar.document,pointer)
    assert resolved.ok and resolved.value==expected
    if format=="csv":
        assert sidecar.document["columns"]==["value","unit","scope"]
        assert sidecar.document["rows"][0]["scope"]=="two\nlines"


def test_arbitrary_numeric_fields_do_not_imply_measurement_units(roots):
    source_id,result,verify=normalize(roots,b'{"count":7,"configuration":{"version":2}}',"json")
    assert result["status"]=="committed",result
    assert verify["overall_result"]=="verified",verify


def test_public_usability_observation_is_read_only_and_does_not_grant_claim_support(roots):
    source_id,result,verify=normalize(roots,b'{"value":3,"unit":"ratio"}',"json")
    root,control=roots
    before=owner("_evidence_revision").capture_workspace(root).revision_id
    with selected(root,control).activate(),Workspace.open(root) as workspace:
        observed=workspace.sources.usability([source_id])
    assert observed["semantic_adequacy"]=="not_evaluated" and not observed["claim_support_eligible"]
    assert observed["sources"][0]["source_id"]==source_id
    assert observed["sources"][0]["usability"]=="usable" and observed["sources"][0]["complete"]
    assert observed["verification"]["overall_result"]=="verified"
    assert observed["current_rights"][0]["state"]=="legacy_compatibility"
    assert observed["current_rights"][0]["source_revision"] is None
    assert observed["sources"][0]["material_basis"]==observed["current_rights"][0]["material_basis"]
    assert owner("_evidence_revision").capture_workspace(root).revision_id==before


@pytest.mark.parametrize("changed",["original","normalized","sidecar"])
def test_current_original_body_or_structured_bytes_change_invalidates_verification(roots,changed):
    source_id,result,verify=normalize(roots,b'{"value":3,"unit":"ratio"}',"json")
    root,control=roots
    path,(frontmatter,body,error)=normalized_record(root,source_id)
    if changed=="original":
        target=root/"raw/web/captured.json"
        target.write_bytes(b'{"value":4,"unit":"ratio"}')
    elif changed=="normalized":
        path.write_text(path.read_text().replace('"value": 3','"value": 4'))
    else:
        (root/frontmatter["structured_view"]["path"]).write_bytes(b'{"data":{"value":4,"unit":"ratio"}}')
    with selected(root,control).activate(),Workspace.open(root) as workspace:
        assert workspace.normalize.verify([source_id])["overall_result"]!="verified"


@pytest.mark.parametrize("ids",[[],["same","same"],[{}],["source"]*33,"source"])
def test_usability_selection_refuses_before_any_native_owner_call(tmp_path,ids):
    from evidence_wiki import source_usability_operations
    from evidence_wiki.errors import UsageError
    with pytest.raises(UsageError,match="selection refused"):
        source_usability_operations.current(tmp_path,source_ids=ids)


def test_optional_pdf_capability_missing_refuses_without_installing_or_publishing(roots,monkeypatch):
    from evidence_wiki.errors import EvidenceWikiError
    normalizer=owner("normalize_sources")
    find=normalizer.importlib.util.find_spec
    monkeypatch.setattr(normalizer.importlib.util,"find_spec",lambda name,*args,**kwargs: None if name=="pypdf" else find(name,*args,**kwargs))
    with pytest.raises(EvidenceWikiError):
        normalize(roots,b"%PDF-1.7\nno parser requested\n%%EOF\n","pdf")
    assert not list((roots[0]/"sources/normalized").glob("*.md"))


def test_stored_pdf_backend_mismatch_cannot_pass_consistency(roots):
    import json
    source_id,result,verify=normalize(roots,pdf_bytes("Retained native scientific text. "*12),"pdf")
    root=roots[0]
    path,(frontmatter,body,error)=normalized_record(root,source_id)
    structured=owner("_structured_view")
    sidecar=root/frontmatter["structured_view"]["path"]
    document=json.loads(sidecar.read_bytes())
    document["backend_version"]="different-parser-version"
    data=json.dumps(document).encode()
    sidecar.write_bytes(data)
    frontmatter["structured_view"]["content_hash"]=structured.content_hash(data)
    from evidence_wiki.source_inputs import SourceView
    view=SourceView(root)
    record=view.manifest()[source_id]
    view.finish()
    assert owner("_host_capture_normalization").issues(root,record,frontmatter,body)==["capture_normalization_original_or_stored_consistency_invalid"]


def test_json_authentication_shell_is_not_full_evidence(roots):
    source_id,result,verify=normalize(roots,b'{"error":"Access denied","status_code":403}',"json")
    assert result["status"]=="partial",result
    path,(frontmatter,body,error)=normalized_record(roots[0],source_id)
    assert frontmatter["evidence_usable"] is False and "Access denied" in body
    assert "host_capture_authentication_or_error_shell" in frontmatter["unusable_evidence_reasons"]



def test_existing_capture_normalization_refresh_is_targeted_and_future_version_refused(tmp_path):
    normalizer=owner("normalize_sources")
    capture_native=owner("_host_capture_normalization")
    record={"id":"source", "kind":"host_capture", "provenance":{"host_capture":{"schema_version":"evidence-host-capture/v2"}}}
    assert capture_native.requires_refresh(record,{})
    assert not capture_native.requires_refresh(record,{"host_capture_normalization_version":1})
    assert not capture_native.requires_refresh({"kind":"host_capture","provenance":{"host_capture":{"schema_version":"evidence-host-capture/v1"}}},{})
    output=tmp_path/"normalized.md"
    output.write_text("---\nnormalizer:\n  name: normalize_sources\n  version: "+str(normalizer.NORMALIZER_VERSION)+"\n---\nBody\n")
    assert normalizer.is_stale(record,output)
    output.write_text("---\nhost_capture_normalization_version: 2\n---\nBody\n")
    with pytest.raises(Exception,match="normalization revision is unsupported"):
        normalizer.is_stale(record,output)
    for version in (True,0,"1"):
        with pytest.raises(Exception,match="normalization revision is unsupported"):
            capture_native.requires_refresh(record,{"host_capture_normalization_version":version})


@pytest.mark.parametrize("version,reason",[(None,"capture_normalization_refresh_required"),(2,"capture_normalization_revision_unsupported")])
def test_read_only_usability_surfaces_producer_currency_and_preserves_existing_bytes(roots,version,reason):
    source_id,result,verify=normalize(roots,b'{"value":3,"unit":"ratio"}',"json")
    root,control=roots
    path,(frontmatter,body,error)=normalized_record(root,source_id)
    if version is None:
        text=path.read_text().replace("host_capture_normalization_version: 1\n","")
    else:
        text=path.read_text().replace("host_capture_normalization_version: 1","host_capture_normalization_version: "+str(version))
    assert text != path.read_text()
    path.write_text(text)
    before=owner("_evidence_revision").capture_workspace(root).revision_id
    with selected(root,control).activate(),Workspace.open(root) as workspace:
        current=workspace.sources.usability([source_id])
    row=current["sources"][0]
    assert reason in row["reasons"] and row["usability"]!="usable"
    assert current["verification"]["overall_result"]!="verified"
    assert path.read_text()==text and owner("_evidence_revision").capture_workspace(root).revision_id==before



def test_normalized_permission_denial_is_not_hidden_by_legacy_or_declared_capture_rights(roots):
    source_id,result,verify=normalize(roots,b'{"value":3,"unit":"ratio"}',"json")
    root,control=roots
    path,(frontmatter,body,error)=normalized_record(root,source_id)
    path.write_text(path.read_text().replace("---\n","---\nretrieval_eligible: false\n",1))
    with selected(root,control).activate(),Workspace.open(root) as workspace:
        current=workspace.sources.usability([source_id])
    rights=current["current_rights"][0]
    assert rights["state"]=="ineligible" and not rights["observation"]["eligible"]
    assert "usage_permission_denied" in rights["observation"]["reasons"]
    assert not current["claim_support_eligible"]



def test_only_exact_owned_format_deferral_is_retired_and_other_refusals_survive(roots):
    import copy
    source_id,result,verify=normalize(roots,b'{"value":3,"unit":"ratio"}',"json")
    from evidence_wiki.source_inputs import SourceView
    view=SourceView(roots[0]);record=view.manifest()[source_id];view.finish()
    native=owner("_host_capture_normalization")
    assert native.format_deferral_only(record)
    for extra in (["host_capture_rights_denied"],["another_explicit_refusal"]):
        altered=copy.deepcopy(record)
        altered["unusable_evidence_reasons"]+=extra
        assert not native.format_deferral_only(altered)
        source=native.normalize(roots[0],altered)
        frontmatter=owner("normalize_sources").frontmatter_for(source,"sources/manifest.jsonl",roots[0]/"sources/normalized/check.md","2026-10-04")
        assert frontmatter["evidence_usable"] is False and extra[0] in frontmatter["unusable_evidence_reasons"]
    altered=copy.deepcopy(record)
    altered["metadata"]["unusable_evidence_reasons"]=[]
    assert not native.format_deferral_only(altered)
    source=native.normalize(roots[0],altered)
    assert source.record["metadata"]["evidence_usable"] is False
    assert "evidence_usable:false" in source.record["unusable_evidence_reasons"]



def test_invalid_normalized_utf8_reports_unestablished_consistency_without_exception_or_writes(roots):
    source_id,result,verify=normalize(roots,b'{"value":3,"unit":"ratio"}',"json")
    root,control=roots
    path,(frontmatter,body,error)=normalized_record(root,source_id)
    path.write_bytes(b"\xff\xfeinvalid normalized bytes")
    before=owner("_evidence_revision").capture_workspace(root).revision_id
    with selected(root,control).activate(),Workspace.open(root) as workspace:
        current=workspace.sources.usability([source_id])
    assert current["stored_consistency"]=="not_established"
    assert "normalized_utf8_invalid" in current["sources"][0]["reasons"]
    assert current["verification"]["overall_result"]=="not_checked"
    assert not current["claim_support_eligible"]
    assert owner("_evidence_revision").capture_workspace(root).revision_id==before


def test_extraction_failure_does_not_claim_checked_consistency(roots):
    source_id,result,verify=normalize(roots,b"%PDF-1.7\ninvalid bounded original\n%%EOF\n","pdf")
    with selected(*roots).activate(),Workspace.open(roots[0]) as workspace:
        current=workspace.sources.usability([source_id])
    assert current["stored_consistency"]=="not_established"
    assert current["sources"][0]["stored_consistency"]=="not_established"


@pytest.mark.parametrize("deny",["original","normalized"])
def test_selected_host_path_denial_precedes_native_verifier_even_if_row_observer_catches_it(roots,monkeypatch,deny):
    from evidence_wiki.errors import ConfigError
    source_id,result,verify=normalize(roots,b'{"value":3,"unit":"ratio"}',"json")
    root,control=roots
    path,(frontmatter,body,error)=normalized_record(root,source_id)
    forbidden=(root/"raw/web/captured.json") if deny=="original" else path
    original=HostContext.require_path
    seen=[]
    def checked(context,path,**kwargs):
        if path==forbidden:
            seen.append(path)
            raise ConfigError("HOST_CONTEXT_REFUSED","Selected path denied.",details={"reason":"host_path_outside_scope"})
        return original(context,path,**kwargs)
    monkeypatch.setattr(HostContext,"require_path",checked)
    monkeypatch.setattr(owner("normalize_verify"),"run_verify",lambda *a,**k:pytest.fail("Denied source reached native verifier"))
    with selected(root,control).activate(),Workspace.open(root) as workspace:
        with pytest.raises(ConfigError,match="Selected path denied"):
            workspace.sources.usability([source_id])
    assert seen


def test_mixed_selected_records_report_partial_consistency_not_complete_success(roots):
    source_id,result,verify=normalize(roots,b'{"value":3,"unit":"ratio"}',"json")
    from evidence_wiki.source_inputs import SourceView
    view=SourceView(roots[0]);other=next(identifier for identifier in view.manifest() if identifier != source_id);view.finish()
    with selected(*roots).activate(),Workspace.open(roots[0]) as workspace:
        current=workspace.sources.usability([source_id,other])
    assert current["stored_consistency"]=="partial"
    assert [row["stored_consistency"] for row in current["sources"]]==["native_contract_checked","not_established"]
    assert current["verification"]["overall_result"]!="verified"
    assert not current["claim_support_eligible"]



@pytest.mark.parametrize("format,raw",[
    ("json",b'{"response":{"value":3,"unit":"ratio"},"error":"Access denied"}'),
    ("csv",b"error,status_code,value,unit\nAccess denied,403,3,ratio\n"),
])
def test_mixed_measurement_data_is_not_whole_source_error_shell(roots,format,raw):
    source_id,result,verify=normalize(roots,raw,format)
    assert result["status"]=="committed",result
    with selected(*roots).activate(),Workspace.open(roots[0]) as workspace:
        current=workspace.sources.usability([source_id])
    assert current["sources"][0]["usability"]=="usable"
    assert current["semantic_adequacy"]=="not_evaluated"


def test_pdf_error_shell_cannot_be_relabelled_usable_or_complete_on_read(roots,monkeypatch):
    source_id,result,verify=normalize(roots,pdf_bytes("Access denied\nPlease sign in to continue.\n"*12),"pdf")
    assert result["status"]=="partial",result
    root=roots[0]
    path,(frontmatter,body,error)=normalized_record(root,source_id)
    frontmatter.update(evidence_usable=True,status="content_extracted",unusable_evidence_reasons=[])
    from evidence_wiki.source_inputs import SourceView
    view=SourceView(root);record=view.manifest()[source_id];view.finish()
    monkeypatch.setattr(owner("normalize_sources"),"extract_pdf",lambda *a,**k:pytest.fail("Read-only shell verification executed parser"))
    assert owner("_host_capture_normalization").issues(root,record,frontmatter,body)==["capture_normalization_original_or_stored_consistency_invalid"]
