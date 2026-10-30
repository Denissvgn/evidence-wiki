"""An over-budget logical journal must be refused before its next owner write."""

from types import SimpleNamespace

import pytest

from evidence_wiki import execution_session as native
from evidence_wiki.errors import UsageError
from evidence_wiki.research_journal_contracts import JOURNAL


def test_expanded_collection_budget_precedes_the_persistent_owner(tmp_path, monkeypatch):
    record = {"schema_version": JOURNAL, "kind": "observation", "binding_id": "sha256:" + "1" * 64,
        "request_id": "next", "sequence": 69, "previous_record_ref": None,
        "data": {"chunks": ["x" * 60000] * 4}, "authentication": None}
    prior = [{**record, "request_id": "prior-" + str(index), "sequence": index} for index in range(69)]
    monkeypatch.setattr(native, "retained", lambda _session: prior)
    writes = []
    from evidence_wiki import research_session
    monkeypatch.setattr(research_session, "_owner", lambda *args, **kwargs: writes.append(kwargs))
    session = SimpleNamespace(root=tmp_path, run_id="run", context=SimpleNamespace(actor_ref="host"),
        binding={}, execution=SimpleNamespace(seal_record=lambda *args, **kwargs: record))
    with pytest.raises(UsageError):
        native.append(session, "observation", "next", record["data"])
    assert writes == []
