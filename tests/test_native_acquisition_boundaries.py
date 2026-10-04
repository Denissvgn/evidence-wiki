"""Native fixture transports enforce declared domains and retained reservations."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from evidence_wiki.pack_discovery import owner
from tests.test_acquisition_transport import FakeResponse


def test_native_fixture_transport_keeps_checked_bytes_and_never_dials_network():
    transport = owner("_acquisition_transport")
    response = FakeResponse(b"Original scoped observation", headers={"Content-Type": "text/plain"})
    result = transport.execute_planned_request(SimpleNamespace(url="https://example.org/source", method="GET"),
        allowed_domains=["example.org"], credentials=[], env={}, timeout=1, max_bytes=128,
        opener=lambda _request, _timeout: response, resolve_hostnames=False, expected_content_types=["text/plain"])
    assert result.content == b"Original scoped observation" and result.tls_verified
    assert result.byte_count == len(result.content) and response.closed


def test_ungranted_domain_is_terminal_before_fixture_connector_invocation():
    transport = owner("_acquisition_transport")
    called = []
    with pytest.raises(transport.AcquisitionTransportError) as caught:
        transport.execute_planned_request(SimpleNamespace(url="https://foreign.example/source", method="GET"),
            allowed_domains=["example.org"], credentials=[], env={}, timeout=1, max_bytes=128,
            opener=lambda *args: called.append(True), resolve_hostnames=False)
    assert caught.value.error_code == "ACQUISITION_DOMAIN_NOT_DECLARED" and not called


def test_native_failed_attempt_reservation_remains_spent_and_cap_refuses_next(tmp_path):
    accounting, fetch = owner("_provider_accounting"), owner("fetch_sources")
    run = tmp_path / "runs" / "run"
    run.mkdir(parents=True)
    values = {"ledger_filename": fetch.REGISTERED_LEDGER_FILENAME, "lock_filename": fetch.REGISTERED_LEDGER_LOCK_FILENAME,
        "schema_version": fetch.ACCOUNTING_SCHEMA_VERSION, "per_run_max": 1}
    first = accounting.reserve(run, "web", 1, **values)
    assert first.per_run_used == 1
    transport = owner("_acquisition_transport")
    with pytest.raises(transport.AcquisitionTransportError):
        transport.execute_planned_request(SimpleNamespace(url="https://example.org/source"), allowed_domains=["example.org"],
            credentials=[], env={}, timeout=1, max_bytes=2, opener=lambda *_: FakeResponse(b"too large",
                headers={"Content-Type": "text/plain"}), resolve_hostnames=False)
    before = (run / fetch.REGISTERED_LEDGER_FILENAME).read_bytes()
    with pytest.raises(accounting.ProviderAccountingError) as caught:
        accounting.reserve(run, "web", 1, **values)
    assert caught.value.error_code == "ACQUISITION_PROVIDER_RATE_LIMITED"
    assert (run / fetch.REGISTERED_LEDGER_FILENAME).read_bytes() == before


def test_native_fixture_fetch_runs_without_holding_acquisition_commit_lock(tmp_path):
    fetch = owner("fetch_sources")
    locks = owner("_workspace_locks")
    root = Path(tmp_path)
    lock = fetch.acquisition_workspace_lock_path(root)
    with locks.workspace_lock(lock, timeout_seconds=0):
        transport = owner("_acquisition_transport")
        response = FakeResponse(b"Retained original", headers={"Content-Type": "text/plain"})
        result = transport.execute_planned_request(SimpleNamespace(url="https://example.org/source"),
            allowed_domains=["example.org"], credentials=[], env={}, timeout=1, max_bytes=128,
            opener=lambda *_: response, resolve_hostnames=False)
    assert result.content == b"Retained original"
