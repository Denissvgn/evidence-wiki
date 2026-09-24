"""Pinned member contracts cannot drift or silently weaken their compilation."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml

from evidence_wiki._filesystem import os
from evidence_wiki._pack_io import canonical
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.pack_composition import apply, plan
from evidence_wiki.pack_discovery import owner

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(os.open not in os.supports_dir_fd, reason="Lifecycle publication requires anchored filesystem operations.")


def request(tmp_path):
    roots = []
    for name in ("science-a", "science-b"):
        root = tmp_path / name
        shutil.copytree(ROOT / "domain-packs/general-science", root)
        path = root / "research.overlay.yml"
        value = yaml.safe_load(path.read_text().replace("general-science", name))
        value["domain_pack"].pop("selection", None)
        path.write_text(yaml.safe_dump(value, sort_keys=False))
        roots.append(root)
    return {"schema_version": "evidence-pack-composition-request/v1", "name": "combined-science", "version": "1.0",
            "scope": "Explicitly scoped related observations", "members": [
                {"path": str(root), "alias": alias, "applicability": "Only questions explicitly assigned to " + alias}
                for root, alias in zip(roots, ("north", "south"), strict=True)]}


def test_composition_uses_scoped_identifiers_and_validates_materialized_bytes(tmp_path):
    value = request(tmp_path)
    prepared = plan(canonical(value))
    result = apply(canonical(prepared), output=tmp_path / "compiled")
    candidate = Path(result["candidate"])
    overlay = yaml.safe_load((candidate / "research.overlay.yml").read_text())
    policies = overlay["domain_pack"]["policy_vocabularies"]["freshness_policy"]
    assert set(policies) == {"pack:combined-science/north.study-recency", "pack:combined-science/south.study-recency"}
    assert result["semantic_adequacy"] == "not_certified"
    owner("_domain_pack_lifecycle").file_inventory(candidate)
    (candidate / "members/north/claims.md").write_text("Changed member without a new composition.\n")
    with pytest.raises(Exception, match="pinned member contracts"):
        owner("_domain_pack_lifecycle").file_inventory(candidate)


def test_rehashed_compiled_policy_change_is_not_proof_of_member_compatibility(tmp_path):
    value = request(tmp_path)
    result = apply(canonical(plan(canonical(value))), output=tmp_path / "compiled")
    root = Path(result["candidate"])
    path = root / "research.overlay.yml"
    overlay = yaml.safe_load(path.read_text())
    overlay["domain_pack"]["policy_vocabularies"] = {}
    path.write_text(yaml.safe_dump(overlay, sort_keys=False))
    manifest = root / "composition.lock.json"
    lock = json.loads(manifest.read_text())
    lock["generated"]["research.overlay.yml"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest.write_bytes(canonical(lock))
    with pytest.raises(Exception, match="pinned member contracts"):
        owner("_domain_pack_lifecycle").file_inventory(root)


def test_member_config_conflict_and_changed_plan_are_refused(tmp_path):
    value = request(tmp_path)
    prepared = plan(canonical(value))
    root = Path(value["members"][1]["path"])
    path = root / "research.overlay.yml"
    changed = yaml.safe_load(path.read_text())
    changed["wiki"]["frontmatter_type_rules"]["claim"]["required_fields"].append("new_requirement")
    path.write_text(yaml.safe_dump(changed, sort_keys=False))
    with pytest.raises(EvidenceWikiError):
        apply(canonical(prepared), output=tmp_path / "uncreated")
    assert not (tmp_path / "uncreated").exists()
