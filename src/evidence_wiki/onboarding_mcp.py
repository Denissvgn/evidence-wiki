"""Separate bounded stdio server over explicit host-scoped lifecycle handles."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys

from . import __version__
from ._pack_io import json_document
from .errors import EvidenceWikiError
from .onboarding import READ, WRITE, Onboarding
from .onboarding_contract import _matches
from .onboarding_tools import PROTOCOL_VERSION, manifest, specifications

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


class OnboardingMcpServer:
    """The host owns this handle; client messages cannot grant roots or operations."""

    def __init__(self, *, allowed_roots=(), allow=()):
        self.handle = Onboarding.open(allowed_roots=allowed_roots, allow=allow)
        self.allowed = READ | frozenset(allow)
        self.initialized = False
        self.ready = False
        self.operations = {"onboarding_" + name.replace(".", "_").replace("-", "_"): (name, spec)
                           for name, spec in specifications().items() if name in self.allowed}

    def close(self):
        self.handle.close()

    def call_tool(self, name, arguments):
        if name not in self.operations:
            raise ValueError("Unknown or ungranted tool")
        _, specification = self.operations[name]
        _matches(arguments, specification["schema"])
        # Methods and fixed arguments come only from our closed declaration.
        with contextlib.redirect_stdout(sys.stderr):
            return getattr(self.handle, specification["method"])(**arguments, **specification["fixed"])

    def handle_message(self, message):
        if (not isinstance(message, dict) or set(message) - {"jsonrpc", "id", "method", "params"}
                or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str)
                or "id" in message and type(message["id"]) not in {int, str}):
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
                "instructions": "Use installed resources and explicit plans. Tool access conveys no host-enforced evidence assurance."}
        elif method == "ping":
            result = {}
        elif not self.ready:
            return rpc_error(identifier, -32002, "Initialize and send notifications/initialized first")
        else:
            try:
                self.handle._check("bootstrap")
                if method == "tools/list" and not params:
                    result = {"tools": manifest(self.allowed)}
                elif method == "tools/call" and set(params) <= {"name", "arguments"} and isinstance(params.get("name"), str):
                    try:
                        payload = self.call_tool(params["name"], params.get("arguments", {}))
                        failed = False
                    except EvidenceWikiError as error:
                        payload, failed = error_payload(error), True
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
        try:
            while True:
                identifier = None
                try:
                    line = read_line(input_stream)
                    if line is None:
                        break
                    raw, oversized = line
                    if oversized:
                        response = rpc_error(None, -32600, "Message exceeds byte bound")
                    elif not raw.strip():
                        continue
                    else:
                        try:
                            value = json_document(raw.encode("utf-8"))
                        except (EvidenceWikiError, UnicodeError):
                            response = rpc_error(None, -32700, "Invalid bounded JSON document")
                        else:
                            identifier = value.get("id") if isinstance(value, dict) and type(value.get("id")) in {str, int} else None
                            response = self.handle_message(value)
                except UnicodeError:
                    response = rpc_error(None, -32700, "Invalid UTF-8 document")
                except Exception:
                    print("Scoped onboarding request failed; no sensitive input is logged.", file=sys.stderr)
                    response = rpc_error(identifier, -32603, "Internal operation error; inspect owner state before retrying")
                if response is not None:
                    rendered = json.dumps(response, ensure_ascii=False, allow_nan=False)
                    if len(rendered.encode()) > MAX_RESPONSE_BYTES:
                        rendered = json.dumps(rpc_error(identifier, -32603, "Result exceeds byte bound; inspect owner state before retrying"))
                    output_stream.write(rendered + "\n")
                    output_stream.flush()
        finally:
            self.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="evidence-wiki serve-onboarding-mcp", description=__doc__)
    parser.add_argument("--allow-root", action="append", default=[])
    parser.add_argument("--allow-operation", action="append", choices=sorted(WRITE), default=[])
    args = parser.parse_args(argv)
    try:
        server = OnboardingMcpServer(allowed_roots=args.allow_root, allow=args.allow_operation)
        server.serve(sys.stdin, sys.stdout)
    except (EvidenceWikiError, OSError):
        print("Cannot open the explicitly selected onboarding scope.", file=sys.stderr)
        return 2
    return 0
