"""Actual public current usability contract, permission and lifetime checks."""

import copy
import os

import pytest
import yaml

from evidence_wiki import Workspace, contract
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from evidence_wiki.onboarding_contract import _matches
from evidence_wiki.pack_discovery import owner
from evidence_wiki.source_contracts import schema_document
from tests._execution_fixture import identifier
from tests._usage_fixture import UsageFixture
from tests.test_mutation_operations import roots as roots
from tests.test_native_capture_normalization import normalize, normalized_record, selected

pytestmark = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd,
    reason="Native descriptor-relative normalization custody is unavailable.")


def test_runtime_schema_and_contract_are_closed_and_advertise_actual_readonly_method():
    published=contract()
    assert published["library_api"]["version"]=="29"
    assert "sources.usability" in published["library_api"]["surface"]
    assert published["source_usability"]["current_usability"]["schema"]=="evidence-source-usability-view/v1"
    assert "workspace-template/scripts/_host_capture_normalization.py" in published["required_asset_manifest"]["starter"]
    schema=schema_document("evidence-source-usability-view/v1")
    assert schema["additionalProperties"] is False
    assert schema["properties"]["authorized"]=={"const":False}
    assert schema["properties"]["claim_support_eligible"]=={"const":False}


def test_actual_view_obeys_published_schema_and_unqualified_legacy_boundary(roots):
    source_id,result,verify=normalize(roots,b'{"value":3,"unit":"ratio"}',"json")
    with selected(*roots).activate(),Workspace.open(roots[0]) as workspace:
        current=workspace.sources.usability([source_id])
    _matches(current,schema_document(current["schema_version"]))
    assert current["sources"][0]["usability"]=="usable"
    assert current["current_rights"][0]["state"]=="legacy_compatibility"
    for key,value in (("authorized",True),("semantic_adequacy","verified"),("claim_support_eligible",True)):
        forged=copy.deepcopy(current);forged[key]=value
        with pytest.raises(EvidenceWikiError):
            _matches(forged,schema_document(current["schema_version"]))


def test_actual_public_scope_requires_both_declared_read_grants(roots):
    source_id,result,verify=normalize(roots,b'{"value":3,"unit":"ratio"}',"json")
    base=selected(*roots)
    for grant in ("source_usability_operations.current","normalize_verify.run_verify"):
        context=HostContext([roots[0]],base.grants-{grant},"host",[roots[0]],mutation_control_directory=roots[1])
        with context.activate(),Workspace.open(roots[0]) as workspace,pytest.raises(EvidenceWikiError) as caught:
            workspace.sources.usability([source_id])
        assert caught.value.error_code=="HOST_CONTEXT_REFUSED"
        assert caught.value.details["reason"]=="host_operation_not_granted"


def test_closed_handle_refuses_before_source_observation(roots,monkeypatch):
    from evidence_wiki import source_usability_operations
    workspace=Workspace.open(roots[0]);sources=workspace.sources;workspace.close()
    monkeypatch.setattr(source_usability_operations,"current",lambda *a,**k:pytest.fail("Closed handle reached current owner"))
    with pytest.raises(EvidenceWikiError):
        sources.usability(["sample"])


def test_actual_signed_normalized_permission_is_qualified_then_revocation_current(roots,tmp_path,monkeypatch):
    source_id,result,verify=normalize(roots,b'{"value":3,"unit":"ratio"}',"json")
    root,control=roots
    directory=tmp_path/"permission-control";directory.mkdir()
    fixture=UsageFixture(directory,monkeypatch)
    fixture.root=root
    fixture.binding=identifier("evidence-host-workspace/v1",{"root":str(root)})
    config=yaml.safe_load((root/"research.yml").read_bytes());config.update(fixture.config)
    fixture.config=config;(root/"research.yml").write_text(yaml.safe_dump(config))
    native=owner("_evidence_usage");fixture.transact(native,"initialize")
    path,(frontmatter,body,error)=normalized_record(root,source_id)
    payload,files=fixture.source(source_id,normalized=path.read_bytes(),evidence={"original.json":(root/"raw/web/captured.json").read_bytes()})
    fixture.transact(native,"deposit",payload,files)
    context=HostContext([root],selected(*roots).grants,"host",[root],mutation_control_directory=control,
        authority_file=fixture.policy_path,state_directory=fixture.host)
    with context.activate(),Workspace.open(root) as workspace:
        current=workspace.sources.usability([source_id])
        assert current["sources"][0]["usability"]=="usable",current
        assert current["current_rights"][0]["state"]=="qualified",current
        assert current["current_rights"][0]["source_revision"]==payload["source_revision"]
        assert not current["authorized"] and not current["claim_support_eligible"]
        fixture.revoke(native,payload)
        revoked=workspace.sources.usability([source_id])
        assert revoked["current_rights"][0]["state"]=="ineligible",revoked
        assert not revoked["current_rights"][0]["observation"]["eligible"]
        assert revoked["sources"][0]["usability"]!="usable"
