"""Detached static declarations preserve every bounded per-input check."""

import hashlib
import json

import pytest

from evidence_wiki import run_policy as owner
from evidence_wiki.workflow_contracts import WorkflowContractError
from tests.test_run_policy import policy_document

SCHEMA_DIGESTS = {
    owner.POLICY: "b1ed6210fbb2c8da5b808f913667d1b8378c2d9a46484f4ba335ca17226559cd",
    owner.POLICY_V2: "6adae771a847d91dc8eccffd87679849e353f8564798785997ad41f9560407dd",
    owner.POLICY_V3: "81e5c698addbed6e7f87c0843e2e4d0be8be72dfba4bd591cdfea32e8cb563aa",
    owner.OBSERVATION: "55ab7cacb82f69b30979e520385343af41cf15b76bdda9cb5327a308a1357382",
    owner.OBSERVATION_V2: "70d4fe3e0272c9049928df6008403cfa15db53ed746cdd620be15a5a2c038def",
    owner.DECISION: "e8e34253dbfd2142b30eb10670d87261c09608d0d170a1fdf11c068c8f93dc32",
}


@pytest.mark.parametrize("name", SCHEMA_DIGESTS)
def test_original_schema_bytes_are_preserved(name):
    schema = owner.schema_document(name)
    raw = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    assert hashlib.sha256(raw).hexdigest() == SCHEMA_DIGESTS[name]


def test_schema_views_are_detached_and_inputs_remain_validated(monkeypatch):
    owner._schema_bytes.cache_clear()
    counts = {"build": 0, "bound": 0, "match": 0}
    for name, key in (("_build_schemas", "build"), ("_bounded", "bound"), ("_matches", "match")):
        original = getattr(owner, name)

        def counted(*args, _original=original, _key=key, **kwargs):
            counts[_key] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(owner, name, counted)
    try:
        original = owner.schemas()
        exposed = owner.schemas()
        for shape in exposed.values():
            shape["properties"].clear()
        owner.schema_document(owner.POLICY)["required"].clear()
        assert owner.schemas() == original
        for _ in range(5):
            assert owner.RunPolicy(policy_document()).policy_id.startswith("sha256:")
            with pytest.raises(WorkflowContractError):
                owner.RunPolicy(policy_document() | {"allowed_effects": ["grant.create"]})
        assert counts["build"] == 1
        assert counts["bound"] >= 10 and counts["match"] >= 10
        assert all(type(raw) is bytes for _name, raw in owner._schema_bytes())
        with pytest.raises(WorkflowContractError):
            owner.schema_document([])
    finally:
        owner._schema_bytes.cache_clear()
