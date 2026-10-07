"""Separate bounded stdio server over explicit host-scoped lifecycle handles."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from datetime import datetime, timedelta, timezone

from . import __version__
from .errors import EvidenceWikiError
from .onboarding import READ, WRITE, Onboarding
from .onboarding_contract import _matches
from .onboarding_tools import PROTOCOL_VERSION, manifest, specifications
from .research_command_contracts import REQUEST
from .research_command_definitions import READ_OPERATIONS, WRITE_OPERATIONS
from .research_tools import manifest as research_manifest
from .runtime_configuration import HostRuntime
from .workspace import Workspace

MAX_LINE_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 4_194_304


def read_line(stream):
    source = getattr(stream, "buffer", stream)
    raw = source.readline(MAX_LINE_BYTES + 2)
    if not raw:
        return None
    newline = b"\n" if isinstance(raw, bytes) else "\n"
    terminated = raw.endswith(newline)
    payload = raw[:-1] if terminated else raw
    size = len(payload) if isinstance(payload, bytes) else len(payload.encode("utf-8"))
    if size > MAX_LINE_BYTES:
        while not terminated:
            chunk = source.readline(65536)
            terminated = not chunk or chunk.endswith(newline)
        return "", True
    return raw.decode("utf-8") if isinstance(raw, bytes) else raw, False


def error_payload(error):
    return {"error_code": error.error_code, "message": str(error), "recoverable": error.recoverable,
            "remediation": error.remediation, "details": error.details, "exit_code": error.exit_code}


def rpc_error(identifier, code, message):
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


def valid_request(message):
    return (type(message) is dict and not set(message) - {"jsonrpc", "id", "method", "params"}
        and message.get("jsonrpc") == "2.0" and type(message.get("method")) is str
        and ("id" not in message or type(message["id"]) in {int, str}))


class OnboardingMcpServer:
    """The host owns this handle; client messages cannot grant roots or operations."""

    def __init__(self, *, allowed_roots=(), allow=(), runtime=None, research_allow=()):
        if (runtime is not None and type(runtime) is not HostRuntime
                or type(research_allow) not in {list, tuple, set, frozenset}
                or any(type(name) is not str for name in research_allow)
                or not set(research_allow) <= READ_OPERATIONS | WRITE_OPERATIONS):
            from .errors import ConfigError
            raise ConfigError("RESEARCH_EXECUTION_CONFIG_INVALID", "Invalid startup research selection.", recoverable=False)
        self.handle = Onboarding.open(allowed_roots=allowed_roots, allow=allow)
        self.runtime = runtime
        self.research_allowed = READ_OPERATIONS | frozenset(research_allow)
        self.research_operations = {"research_" + name.replace("-", "_"): name for name in self.research_allowed}
        self.allowed = READ | frozenset(allow)
        self.initialized = False
        self.ready = False
        self.operations = {"onboarding_" + name.replace(".", "_").replace("-", "_"): (name, spec)
                           for name, spec in specifications().items() if name in self.allowed}

    def close(self):
        self.handle.close()

    def call_tool(self, name, arguments, *, operation_control=None):
        if name in self.research_operations:
            self.handle._check_lifetime()
            if type(arguments) is not dict or "target" not in arguments:
                from .errors import UsageError
                raise UsageError("RESEARCH_COMMAND_ARGUMENTS_INVALID", "Research tools require a target and operation arguments.", recoverable=False)
            selected = dict(arguments)
            target = self.handle._scope.path(selected.pop("target"), write=self.research_operations[name] in WRITE_OPERATIONS)
            with contextlib.redirect_stdout(sys.stderr), Workspace.open(target) as workspace:
                result = workspace.research.command({"schema_version": REQUEST, "operation": self.research_operations[name],
                    "arguments": selected}, runtime=self.runtime, operation_control=operation_control)
            self.handle._check_lifetime()
            return result
        if name not in self.operations:
            raise ValueError("Unknown or ungranted tool")
        _, specification = self.operations[name]
        _matches(arguments, specification["schema"])
        # Methods and fixed arguments come only from our closed declaration.
        with contextlib.redirect_stdout(sys.stderr):
            return getattr(self.handle, specification["method"])(**arguments, **specification["fixed"])

    def is_research_request(self, message):
        return (type(message) is dict and type(message.get("id")) in {str, int}
            and message.get("method") == "tools/call" and type(message.get("params")) is dict
            and type(message["params"].get("name")) is str
            and message["params"].get("name") in self.research_operations)

    def handle_message(self, message, *, cancellation=None):
        if not valid_request(message):
            return rpc_error(None, -32600, "Invalid request")
        identifier, method, params = message.get("id"), message["method"], message.get("params", {})
        if not isinstance(params, dict):
            return rpc_error(identifier, -32602, "Expected object parameters") if "id" in message else None
        if "id" not in message:
            if method == "notifications/initialized" and self.initialized and not params:
                self.ready = True
            return None
        if method == "initialize":
            if (self.initialized or set(params) - {"protocolVersion", "capabilities", "clientInfo"}
                    or not isinstance(params.get("protocolVersion"), str)
                    or not isinstance(params.get("capabilities"), dict) or not isinstance(params.get("clientInfo"), dict)):
                return rpc_error(identifier, -32602, "Invalid initialization or session already initialized")
            self.initialized = True
            result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False}},
                "serverInfo": {"name": "evidence-wiki-onboarding", "version": __version__},
                "instructions": "Use installed resources and explicit plans. Scoped-research-tools/v1 uses startup-selected runtime, roots and grants; tools/call cannot provision credentials or qualification. No sampling, elicitation or routine approval is requested. Reconnect through pending/recovery tools using the same business IDs and retained budgets. Tool access conveys no host-enforced evidence assurance."}
        elif method == "ping":
            result = {}
        elif not self.ready:
            return rpc_error(identifier, -32002, "Initialize and send notifications/initialized first")
        else:
            try:
                self.handle._check("bootstrap")
                if method == "tools/list" and not params:
                    result = {"tools": manifest(self.allowed) + research_manifest(self.research_allowed)}
                elif method == "tools/call" and set(params) <= {"name", "arguments"} and isinstance(params.get("name"), str):
                    try:
                        control = None
                        if cancellation is not None:
                            from .operation_control import OperationControl
                            seconds = min(600, self.runtime.policy.to_dict()["budgets"]["seconds"]) if self.runtime else 600
                            control = OperationControl(deadline_utc=(datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat(),
                                timeout_seconds=seconds, cancel_requested=cancellation.is_set)
                        payload = self.call_tool(params["name"], params.get("arguments", {}), operation_control=control)
                        failed = payload.get("status") == "refused"
                    except EvidenceWikiError as error:
                        payload, failed = error_payload(error), True
                    finally:
                        if control is not None:
                            control.close()
                    result = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, allow_nan=False)}], "isError": failed}
                elif method == "resources/list" and not params:
                    result = {"resources": [{"uri": "evidence-wiki://resource/" + row["id"], "name": row["id"], "mimeType": row["media_type"]}
                        for row in self.handle.resources()["resources"]]}
                elif method == "resources/read" and set(params) == {"uri"} and isinstance(params["uri"], str):
                    prefix = "evidence-wiki://resource/"
                    if not params["uri"].startswith(prefix):
                        return rpc_error(identifier, -32602, "Unknown installed resource URI")
                    resource = self.handle.resource(params["uri"][len(prefix):])
                    result = {"contents": [{"uri": params["uri"], "mimeType": resource["media_type"], "text": resource["content"]}]}
                elif method in {"tools/list", "tools/call", "resources/list", "resources/read"}:
                    return rpc_error(identifier, -32602, "Invalid parameters")
                else:
                    return rpc_error(identifier, -32601, "Method not found")
            except ValueError:
                return rpc_error(identifier, -32602, "Unknown or ungranted tool")
            except EvidenceWikiError as error:
                return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32000, "message": "Scoped operation refused", "data": error_payload(error)}}
        return {"jsonrpc": "2.0", "id": identifier, "result": result}

    def serve(self, input_stream, output_stream):
        from .scoped_stdio import serve
        return serve(self, input_stream, output_stream)



def main(argv=None):
    parser = argparse.ArgumentParser(prog="evidence-wiki serve-onboarding-mcp", description=__doc__)
    parser.add_argument("--allow-root", action="append", default=[])
    parser.add_argument("--allow-operation", action="append", choices=sorted(WRITE), default=[])
    parser.add_argument("--research-runtime", help="Startup-only owner-private HostRuntime file; never client-selected.")
    parser.add_argument("--allow-research-operation", action="append", choices=sorted(WRITE_OPERATIONS), default=[])
    args = parser.parse_args(argv)
    try:
        runtime = HostRuntime.from_file(args.research_runtime) if args.research_runtime else None
        server = OnboardingMcpServer(allowed_roots=args.allow_root, allow=args.allow_operation,
            runtime=runtime, research_allow=args.allow_research_operation)
        server.serve(sys.stdin, sys.stdout)
    except (EvidenceWikiError, OSError):
        print("Cannot open the explicitly selected onboarding scope.", file=sys.stderr)
        return 2
    return 0
