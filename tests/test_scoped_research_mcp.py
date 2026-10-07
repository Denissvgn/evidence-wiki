"""Actual stdio journeys retain public decisions, startup scope and native custody."""

import json
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from evidence_wiki import HostRuntime, OperationControl, Workspace
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.onboarding_mcp import OnboardingMcpServer
from evidence_wiki.research_command_contracts import REQUEST
from evidence_wiki.research_command_definitions import DEFINITIONS, WRITE_OPERATIONS
from tests.test_autonomous_commands import command, document, write_runtime
from tests.test_execution_session import fixture as fixture
from tests.test_onboarding_mcp import initialize, message


def configured(case):
    value = document(case)
    value['context']['grants'] = sorted(set(value['context']['grants']) | {
        'research_session.recovery_status', 'research_session.reconcile_recovery', 'history_operations.current'})
    return value


def direct(root, runtime, operation, arguments):
    with Workspace.open(root) as workspace:
        return workspace.research.command({'schema_version': REQUEST, 'operation': operation,
            'arguments': arguments}, runtime=runtime)


class Client:
    def __init__(self, root, runtime, *, allow=(), onboarding_allow=(), cwd=None):
        argv = [str(Path(sys.executable).with_name('evidence-wiki')), 'serve-onboarding-mcp', '--allow-root', str(root)]
        if runtime:
            argv += ['--research-runtime', str(runtime)]
        for operation in allow:
            argv += ['--allow-research-operation', operation]
        for operation in onboarding_allow:
            argv += ['--allow-operation', operation]
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', cwd=cwd)
        self.responses = queue.Queue()
        self.identifier = 1
        self.frames = []
        def read():
            for line in self.process.stdout:
                assert len(line.encode()) <= 4_194_304
                self.responses.put(json.loads(line))
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        self.send(initialize())
        initialized = self.receive()
        assert initialized['result']['capabilities'].keys() == {'tools', 'resources'}
        self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

    def send(self, value):
        self.process.stdin.write(json.dumps(value) + '\n')
        self.process.stdin.flush()

    def receive(self, timeout=120):
        response = self.responses.get(timeout=timeout)
        assert response.get('jsonrpc') == '2.0' and 'method' not in response
        self.frames.append(response)
        return response

    def rpc(self, method, params=None, timeout=120):
        self.identifier += 1
        self.send(message(self.identifier, method, params))
        response = self.receive(timeout)
        assert response['id'] == self.identifier, response
        return response

    def tool(self, operation, root, arguments=None, timeout=120):
        response = self.rpc('tools/call', {'name': 'research_' + operation.replace('-', '_'),
            'arguments': {'target': str(root), **(arguments or {})}}, timeout=timeout)
        if 'error' in response:
            return response
        return json.loads(response['result']['content'][0]['text'])

    def close(self):
        self.process.stdin.close()
        try:
            assert self.process.wait(timeout=30) == 0
            assert self.process.stderr.read() == ''
            self.reader.join(timeout=5)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()


def test_real_stdio_budget_reconnect_and_public_cli_parity(fixture, tmp_path):
    root, *_ = fixture
    value = configured(fixture)
    path = write_runtime(tmp_path, value)
    runtime = HostRuntime.from_file(path)
    client = Client(root, path, allow=['run-cycle', 'reconcile-recovery'], cwd=tmp_path)
    try:
        tools = client.rpc('tools/list')['result']['tools']
        assert 'research_run_cycle' in {row['name'] for row in tools}
        assert 'research_deliver' not in {row['name'] for row in tools}
        initial = client.tool('current', root, {'run_id': 'bounded-mcp'})
        assert initial['status'] == 'returned' and initial['result']['usage'], initial
        result = client.tool('run-cycle', root, {'run_id': 'bounded-mcp', 'request_id': 'drive', 'max_steps': 1})
        assert result['result']['outcome'] == 'stop_budget' and result['terminal']
        pending = client.tool('pending', root, {'run_id': 'bounded-mcp'})
        assert pending['result'] == {'pending': None}
        custody = client.tool('recovery-status', root, {'run_id': 'bounded-mcp'})
        assert custody['result']['status'] == 'terminal'
    finally:
        client.close()
    reconnect = Client(root, path, allow=['run-cycle', 'reconcile-recovery'])
    try:
        replay = reconnect.tool('run-cycle', root, {'run_id': 'bounded-mcp', 'request_id': 'drive', 'max_steps': 1})
        assert replay['result']['historical']
        assert replay['result']['step']['decision'] == result['result']['step']['decision']
        after = reconnect.tool('recovery-status', root, {'run_id': 'bounded-mcp'})
        assert after['result']['original_deadline_utc'] == custody['result']['original_deadline_utc']
        assert after['result']['steps_used'] == custody['result']['steps_used']
        reconciled = reconnect.tool('reconcile-recovery', root, {'run_id': 'bounded-mcp', 'request_id': 'recover',
            'expected_basis_id': after['result']['current_basis_id']})
        assert reconciled['result']['status'] == 'terminal'
    finally:
        reconnect.close()
    public = direct(root, runtime, 'run-cycle', {'run_id': 'bounded-mcp', 'request_id': 'drive', 'max_steps': 1})
    cli = command('run-cycle', '--target', str(root), '--runtime', str(path), '--run-id', 'bounded-mcp',
        '--request-id', 'drive', '--max-steps', '1')
    assert public['result']['step']['decision'] == json.loads(cli.stdout)['result']['step']['decision'] == result['result']['step']['decision']
    assert cli.returncode == public['exit_code'] == result['exit_code'] and cli.stderr == ''
    assert value['keys']['observer'] not in json.dumps(client.frames)


def test_startup_permissions_and_injected_runtime_never_authorize(fixture, tmp_path):
    root, *_ = fixture
    runtime = HostRuntime.from_document(configured(fixture))
    server = OnboardingMcpServer(allowed_roots=[root], runtime=runtime)
    try:
        server.handle_message(initialize())
        server.handle_message({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        denied = server.handle_message(message(2, 'tools/call', {'name': 'research_run_cycle', 'arguments': {'target': str(root)}}))
        assert denied['error']['code'] == -32602
        server.close()
        server = OnboardingMcpServer(allowed_roots=[root], runtime=runtime, research_allow=['run-cycle'])
        for field in ('runtime', 'grants', 'policy', 'qualification_records', 'operation_control'):
            refused = server.call_tool('research_run_cycle', {'target': str(root), 'run_id': 'injected', 'request_id': 'drive', field: {}})
            assert refused['status'] == 'refused' and refused['error']['error_code'] == 'RESEARCH_COMMAND_ARGUMENTS_INVALID'
        outside = tmp_path / 'outside'; outside.mkdir()
        with pytest.raises(EvidenceWikiError):
            server.call_tool('research_current', {'target': str(outside)})
        assert not (root / 'runs/injected').exists()
    finally:
        server.close()


def test_missing_runtime_and_bad_arguments_return_same_terminal_data(fixture):
    root, *_ = fixture
    server = OnboardingMcpServer(allowed_roots=[root], research_allow=['run-cycle', 'advance'])
    try:
        args = {'run_id': 'unavailable', 'request_id': 'drive'}
        result = server.call_tool('research_run_cycle', {'target': str(root), **args})
        assert result == direct(root, None, 'run-cycle', args)
        assert result['terminal'] and result['status'] == 'refused'
        bad = server.call_tool('research_advance', {'target': str(root), 'run_id': 'x'})
        assert bad == direct(root, None, 'advance', {'run_id': 'x'})
    finally:
        server.close()


def test_full_installed_stdio_journey_and_refresh_without_elicitation(tmp_path, monkeypatch):
    case = fixture.__wrapped__(tmp_path, monkeypatch, SimpleNamespace(param={'planner': True}))
    root, *_ = case
    path = write_runtime(tmp_path, configured(case))
    runtime = HostRuntime.from_file(path)
    client = Client(root, path, allow=['run-cycle', 'refresh'])
    try:
        result = client.tool('run-cycle', root, {'run_id': 'complete-mcp', 'request_id': 'drive', 'max_steps': 64}, timeout=600)
        assert result['status'] == 'returned' and result['result']['outcome'] == 'accepted', result
        assert result['result']['current_release']['research_complete']
        brief = client.tool('brief', root, {'run_id': 'complete-mcp'})
        assert brief['status'] == 'returned' and not brief['authorized']
        refreshed = client.tool('refresh', root, {'run_id': 'refresh-mcp', 'request_id': 'refresh-drive',
            'previous_run_id': 'complete-mcp', 'planner_config_ref': 'planner', 'max_steps': 1}, timeout=180)
        assert refreshed['status'] == 'returned' and refreshed['result']['cycle']['outcome'] == 'stop_budget', refreshed
        assert refreshed['result']['previous_run_id'] == 'complete-mcp'
    finally:
        client.close()
    public = direct(root, runtime, 'refresh', {'run_id': 'refresh-mcp', 'request_id': 'refresh-drive',
        'previous_run_id': 'complete-mcp', 'planner_config_ref': 'planner', 'max_steps': 1})
    assert public['result']['cycle']['step']['decision'] == refreshed['result']['cycle']['step']['decision']
    assert public['result']['previous_usage'] == refreshed['result']['previous_usage']


def test_real_cancellation_suppresses_response_and_reconnect_retains_budget(fixture, tmp_path):
    from evidence_wiki.pack_discovery import owner
    root, *_ = fixture
    path = write_runtime(tmp_path, configured(fixture))
    client = Client(root, path, allow=['run-cycle', 'cancel-pending', 'reconcile-recovery'])
    try:
        client.send(message(20, 'tools/call', {'name': 'research_run_cycle', 'arguments': {
            'target': str(root), 'run_id': 'cancelled-mcp', 'request_id': 'drive', 'max_steps': 64}}))
        state = owner('run_controller').run_state_path(root, 'cancelled-mcp')
        end = time.monotonic() + 60
        while not state.exists() and time.monotonic() < end:
            time.sleep(0.02)
        assert state.exists()
        client.send({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': 20, 'reason': 'Stop further work'}})
        client.send({'jsonrpc': '1.0', 'id': 900, 'method': 'tools/call', 'params': {'name': 'research_current'}})
        malformed = client.receive()
        assert malformed['error']['code'] == -32600
        assert client.rpc('ping')['result'] == {}
        end = time.monotonic() + 60
        while True:
            custody = client.tool('recovery-status', root, {'run_id': 'cancelled-mcp'})
            if custody.get('jsonrpc') != '2.0' or time.monotonic() >= end:
                break
            assert custody['error']['code'] == -32001
            time.sleep(0.05)
        assert custody['status'] == 'returned', custody
        assert custody['result']['original_deadline_utc'] and custody['result']['status'] == 'reconcilable'
        assert all(row['id'] != 20 for row in client.frames)
    finally:
        client.close()
    reconnect = Client(root, path, allow=['cancel-pending', 'reconcile-recovery'])
    try:
        after = reconnect.tool('recovery-status', root, {'run_id': 'cancelled-mcp'})
        assert after['result']['original_deadline_utc'] == custody['result']['original_deadline_utc']
        assert after['result']['steps_used'] == custody['result']['steps_used']
        reconciled = reconnect.tool('reconcile-recovery', root, {'run_id': 'cancelled-mcp', 'request_id': 'reconcile',
            'expected_basis_id': after['result']['current_basis_id']})
        assert reconciled['status'] == 'returned', reconciled
    finally:
        reconnect.close()


def test_public_cancellation_before_effects_is_typed_and_request_data_cannot_select_control(fixture):
    root, *_ = fixture
    runtime = HostRuntime.from_document(configured(fixture))
    control = OperationControl(deadline_utc=(datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
        timeout_seconds=60, cancel_requested=lambda: True)
    with Workspace.open(root) as workspace:
        result = workspace.research.command({'schema_version': REQUEST, 'operation': 'run-cycle', 'arguments': {
            'run_id': 'cancel-before-start', 'request_id': 'drive'}}, runtime=runtime, operation_control=control)
    assert result['status'] == 'refused' and result['error']['error_code'] == 'OPERATION_CANCELLED'
    assert not (root / 'runs/cancel-before-start').exists()


def test_all_journey_tool_names_and_effect_grants_are_closed():
    from evidence_wiki.research_tools import manifest
    server = OnboardingMcpServer(research_allow=WRITE_OPERATIONS)
    try:
        assert set(server.research_operations.values()) == set(DEFINITIONS)
        rows = manifest(WRITE_OPERATIONS)
        assert all(not row['inputSchema']['additionalProperties'] for row in rows)
        assert all('runtime' not in row['inputSchema']['properties'] and 'policy' not in row['inputSchema']['properties'] for row in rows)
    finally:
        server.close()


def test_actual_stdio_setup_and_capture_need_no_shell_escape(tmp_path, monkeypatch):
    from tests.test_research_planning import request
    from tests.test_source_capabilities import capture_request
    monkeypatch.delenv('EVIDENCE_WIKI_AUTHORITY_FILE', raising=False)
    monkeypatch.delenv('EVIDENCE_WIKI_STATE_DIR', raising=False)
    client = Client(tmp_path, None, onboarding_allow=['apply', 'capture'])
    def call(name, arguments):
        response = client.rpc('tools/call', {'name': name, 'arguments': arguments})
        assert not response['result']['isError'], response
        return json.loads(response['result']['content'][0]['text'])
    try:
        plan = call('onboarding_plan', {'value': request(tmp_path)})
        result = call('onboarding_apply', {'value': plan})
        assert result['setup_ready'] and not result['research_complete']
        replay = call('onboarding_apply', {'value': plan})
        assert replay['plan_id'] == result['plan_id'] and replay['setup_ready']
        captured = call('onboarding_capture', {'target': str(tmp_path / 'workspace'),
            'value': capture_request(), 'path': 'raw/papers/observation.md'})
        assert (tmp_path / 'workspace/raw/papers/observation.md').exists()
        assert not captured.get('evidence_accepted')
    finally:
        client.close()


def test_completed_host_controls_release_idle_probes_and_remain_closed():
    before = {thread.ident for thread in threading.enumerate() if thread.name == 'evidence-operation-cancellation'}
    for _ in range(20):
        control = OperationControl(deadline_utc=(datetime.now(timezone.utc) + timedelta(seconds=600)).isoformat(),
            timeout_seconds=600, cancel_requested=lambda: False)
        control.checkpoint('ready')
        control.close()
        with pytest.raises(EvidenceWikiError) as error:
            control.checkpoint('after_close')
        assert error.value.error_code == 'OPERATION_CONTROL_CLOSED'
    assert {thread.ident for thread in threading.enumerate() if thread.name == 'evidence-operation-cancellation'} <= before
