#!/usr/bin/env python3
"""Serve a research workspace over stdio Model Context Protocol.

This is an optional tool-native face over the canonical workspace scripts. It
implements the stdio JSON-RPC subset needed by MCP clients to initialize,
discover tools, and call read/append-only research tools. The CLI scripts remain
the source of truth for schemas and behavior; tool results return the same JSON
payloads in ``structuredContent`` plus a serialized JSON text fallback.

Supported protocol version: ``2025-06-18``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any, TextIO

SCHEMA_VERSION = "1.0"
MCP_PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "evidence-wiki"
SERVER_VERSION = "0.1.0"

JSONRPC_VERSION = "2.0"
JSONRPC_ID_MISSING = object()
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
MAX_JSONRPC_LINE_BYTES = 4 * 1024 * 1024
JSONRPC_OVERSIZED_MESSAGE = "JSON-RPC message exceeds maximum size"
_LINE_DRAIN_CHUNK_BYTES = 64 * 1024
DEFAULT_CLIENT_ERROR_MESSAGE = "Workspace tool failed before completing."
CLIENT_ERROR_MESSAGES = {
    "CONFIG_MISSING": "Workspace configuration is missing.",
    "CONFIG_INVALID": "Workspace configuration is invalid.",
    "DEPENDENCY_MISSING": "A required workspace dependency is missing.",
    "MANIFEST_MISSING": "Workspace source manifest is missing.",
    "MANIFEST_INVALID": "Workspace source manifest is invalid.",
    "TOOLING_MISSING": "Workspace tooling is missing or incomplete.",
    "QUERY_MISSING": "Provide one or more query terms.",
    "INTAKE_TOTAL_CAP_EXCEEDED": "Question intake would exceed the open-question limit.",
    "INTAKE_RATE_LIMITED": "Question intake is temporarily rate-limited.",
    "INTAKE_FIELD_TOO_LONG": "Question intake contains fields that exceed size limits.",
    "INTAKE_BATCH_TOO_LARGE": "Question intake batch is too large.",
    "HANDOFF_SIGNATURE_INVALID": "Handoff signature verification failed.",
    "WORKSPACE_UNREADABLE": DEFAULT_CLIENT_ERROR_MESSAGE,
}
TOOL_ARGUMENT_KEYS = {
    "workspace_status": frozenset(),
    "question_status": frozenset({"status"}),
    "query_index": frozenset({"query", "scope", "limit", "index_path"}),
    "intake_questions": frozenset({"batch", "dry_run"}),
    "export_answers": frozenset({"status"}),
    "source_requests_list": frozenset({"status"}),
}

_SIBLING_CACHE: dict[str, ModuleType] = {}
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _intake_limits import mcp_intake_batch_limit
from _script_errors import classify_error_code, error_envelope
from _workspace_module_loader import load_workspace_module


class ToolExecutionError(Exception):
    """Business-level tool failure returned as an MCP tool error result."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.details = details


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve a research workspace over stdio MCP.")
    parser.add_argument(
        "--target",
        "--project-root",
        dest="target",
        default=".",
        help="Research workspace root. Defaults to the current directory.",
    )
    return parser.parse_args(argv)


def load_sibling_module(stem: str) -> ModuleType:
    """Load a sibling workspace script as a module so its logic is reused directly."""
    if stem not in _SIBLING_CACHE:
        _SIBLING_CACHE[stem] = load_workspace_module(_SCRIPT_DIR, stem)
    return _SIBLING_CACHE[stem]


def text_content(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2, sort_keys=False)}],
        "structuredContent": payload,
        "isError": False,
    }


def error_content(message: str, *, error_code: str | None = None, details: dict[str, Any] | None = None) -> dict[str, Any]:
    code = error_code or classify_error_code(message)
    payload = error_envelope(code, message, details=details)
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2, sort_keys=False)}],
        "structuredContent": payload,
        "isError": True,
    }


def system_exit_message(exc: SystemExit) -> str:
    if isinstance(exc.code, str):
        return exc.code
    if exc.code is None:
        return "Tool exited without a status code."
    return f"Tool exited with status {exc.code}."


def client_error_message(error_code: str) -> str:
    return CLIENT_ERROR_MESSAGES.get(error_code, DEFAULT_CLIENT_ERROR_MESSAGE)


def details_with_correlation_id(details: dict[str, Any] | None, correlation_id: str) -> dict[str, Any]:
    merged = dict(details or {})
    merged["correlation_id"] = correlation_id
    return merged


def log_tool_error(correlation_id: str, exc: BaseException, raw_message: str) -> None:
    print(f"MCP tool error {correlation_id}: {type(exc).__name__}: {raw_message}", file=sys.stderr)


def sanitized_error_content(
    exc: BaseException,
    raw_message: str,
    *,
    error_code: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    code = error_code or classify_error_code(raw_message)
    correlation_id = uuid.uuid4().hex
    log_tool_error(correlation_id, exc, raw_message)
    return error_content(
        client_error_message(code),
        error_code=code,
        details=details_with_correlation_id(details, correlation_id),
    )


def jsonrpc_parse_error_response(exc: json.JSONDecodeError) -> dict[str, Any]:
    correlation_id = uuid.uuid4().hex
    print(f"MCP parse error {correlation_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return jsonrpc_error(None, ERR_PARSE, "Parse error", {"correlation_id": correlation_id})


def jsonrpc_message_id(message: dict[str, Any]) -> Any:
    return message["id"] if "id" in message else JSONRPC_ID_MISSING


def is_valid_jsonrpc_id(message_id: Any) -> bool:
    if message_id is None or isinstance(message_id, str):
        return True
    if isinstance(message_id, bool):
        return False
    if isinstance(message_id, int):
        return True
    if isinstance(message_id, float):
        return math.isfinite(message_id)
    return False


def jsonrpc_safe_response_id(message: Any) -> Any:
    if not isinstance(message, dict):
        return None
    message_id = jsonrpc_message_id(message)
    if message_id is JSONRPC_ID_MISSING or not is_valid_jsonrpc_id(message_id):
        return None
    return message_id


def jsonrpc_response(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": message_id, "result": result}


def jsonrpc_error(message_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": message_id, "error": error}


def drain_binary_line(input_stream: Any) -> None:
    while True:
        chunk = input_stream.readline(_LINE_DRAIN_CHUNK_BYTES)
        if not chunk or chunk.endswith(b"\n"):
            return


def read_binary_jsonrpc_line(input_stream: Any, max_line_bytes: int) -> tuple[str, bool] | None:
    raw = input_stream.readline(max_line_bytes + 1)
    if raw == b"":
        return None
    payload = raw[:-1] if raw.endswith(b"\n") else raw
    oversized = len(payload) > max_line_bytes
    if oversized and not raw.endswith(b"\n"):
        drain_binary_line(input_stream)
    if oversized:
        return "", True
    return raw.decode("utf-8", errors="replace"), False


def read_text_jsonrpc_line(input_stream: TextIO, max_line_bytes: int) -> tuple[str, bool] | None:
    saw_input = False
    payload_bytes = 0
    chars: list[str] = []
    while True:
        char = input_stream.read(1)
        if char == "":
            break
        saw_input = True
        if char == "\n":
            chars.append(char)
            break
        payload_bytes += len(char.encode("utf-8"))
        if payload_bytes > max_line_bytes:
            while True:
                drain_char = input_stream.read(1)
                if drain_char in ("", "\n"):
                    break
            return "", True
        chars.append(char)
    if not saw_input:
        return None
    return "".join(chars), False


def read_jsonrpc_line(input_stream: TextIO, max_line_bytes: int) -> tuple[str, bool] | None:
    buffer = getattr(input_stream, "buffer", None)
    if buffer is not None:
        return read_binary_jsonrpc_line(buffer, max_line_bytes)
    return read_text_jsonrpc_line(input_stream, max_line_bytes)


def validate_string_list(value: Any, label: str, *, choices: tuple[str, ...] | None = None) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ToolExecutionError(f"{label} must be a list of strings")
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ToolExecutionError(f"{label} must be a list of non-empty strings")
        stripped = item.strip()
        if choices is not None and stripped not in choices:
            allowed = ", ".join(choices)
            raise ToolExecutionError(f"{label} must contain only: {allowed}")
        cleaned.append(stripped)
    return cleaned


def validate_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolExecutionError(f"{label} must be an object")
    return value


def reject_unknown_keys(value: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ToolExecutionError(
            f"{label} contains unknown field(s): {', '.join(unknown)}",
            error_code="VALUE_INVALID",
            details={"unknown_fields": unknown, "allowed_fields": sorted(allowed)},
        )


def positive_int(value: Any, label: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ToolExecutionError(f"{label} must be a positive integer")
    return value


def query_payload(project_root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    query_index = load_sibling_module("query_index")
    raw_query = arguments.get("query")
    if not isinstance(raw_query, str) or not raw_query.strip():
        raise ToolExecutionError("Provide one or more query terms.", error_code="QUERY_MISSING")
    query = " ".join(raw_query.split())
    raw_scope = arguments.get("scope", "all")
    if raw_scope not in query_index.SCOPES:
        choices = ", ".join(query_index.SCOPES)
        raise ToolExecutionError(f"scope must be one of: {choices}")
    limit = query_index.effective_query_limit(
        positive_int(arguments.get("limit"), "limit", query_index.DEFAULT_LIMIT)
    )
    raw_index_path = arguments.get("index_path")
    if raw_index_path is not None and not isinstance(raw_index_path, str):
        raise ToolExecutionError("index_path must be a string")

    config = query_index.load_config(project_root)
    index_path = query_index.resolve_index_path(project_root, raw_index_path)
    provider = query_index.retrieval_provider(config)
    engine = query_index.LEXICAL_ENGINE
    provider_result = None
    if provider is not None:
        provider_result = query_index.query_retrieval_provider(project_root, config, raw_scope, query, limit, provider)
    if provider_result is not None:
        results, indexed = provider_result
        engine = provider.name
    else:
        results, indexed = query_index.query_with_optional_fts(project_root, config, raw_scope, query, limit, index_path)
    results = query_index.add_engine(results, engine)
    results = query_index.enrich_related_source_ids(results, query_index.citation_relation_graph(project_root, config))
    unnormalized = query_index.unnormalized_source_ids(project_root, config)
    return {
        "query": query,
        "scope": raw_scope,
        "engine": engine,
        "indexed_documents": indexed,
        "result_count": len(results),
        "results": results,
        "unnormalized_source_count": len(unnormalized),
        "unnormalized_source_ids": unnormalized[: query_index.MAX_REPORTED_UNNORMALIZED],
    }


class ResearchWikiMcpServer:
    """Small stdio MCP server exposing workspace script contracts as tools."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).expanduser().resolve()

    def tool_definitions(self) -> list[dict[str, Any]]:
        query_index = load_sibling_module("query_index")
        return [
            {
                "name": "workspace_status",
                "title": "Workspace Status",
                "description": "Return the aggregate workspace status document.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "question_status",
                "title": "Question Status",
                "description": "Return deterministic question backlog counts and records.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "query_index",
                "title": "Query Index",
                "description": "Search wiki pages and normalized source records.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "scope": {"type": "string", "enum": ["wiki", "normalized", "all"]},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": query_index.MAX_QUERY_LIMIT,
                        },
                        "index_path": {"type": "string"},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "intake_questions",
                "title": "Intake Questions",
                "description": "Validate and inject a question batch into the workspace.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "batch": {"type": "object"},
                        "dry_run": {"type": "boolean"},
                    },
                    "required": ["batch"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "export_answers",
                "title": "Export Answers",
                "description": "Export structured question answers with citations.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "source_requests_list",
                "title": "List Source Requests",
                "description": "List structured source requests for fetch agents.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["open", "fulfilled"]},
                        },
                    },
                    "additionalProperties": False,
                },
            },
        ]

    def initialize_result(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": SERVER_NAME,
                "title": "EvidenceWiki",
                "version": SERVER_VERSION,
            },
            "instructions": (
                "This server exposes read/append-only research workspace tools. "
                "The CLI scripts remain the canonical schema contract. "
                "Restart this MCP process after `evidence-wiki upgrade` so "
                "refreshed workspace tooling is loaded."
            ),
        }

    def call_tool_payload(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "workspace_status":
            module = load_sibling_module("workspace_status")
            document = module.build_status_document(self.project_root)
            workspace_health = document.get("workspace_health")
            if isinstance(workspace_health, dict) and not workspace_health.get("materially_valid", False):
                finding_codes = workspace_health.get("finding_codes", [])
                research_config_missing = not self.project_root.is_dir() or any(
                    isinstance(item, dict)
                    and item.get("code") == "WORKSPACE_REQUIRED_FILE_MISSING"
                    and "research.yml" in item.get("artifacts", [])
                    for item in workspace_health.get("findings", [])
                )
                raise ToolExecutionError(
                    "Shared workspace health rejected the workspace contract.",
                    error_code="CONFIG_MISSING" if research_config_missing else "WORKSPACE_UNREADABLE",
                    details={"finding_codes": finding_codes},
                )
            return document

        if name == "question_status":
            module = load_sibling_module("question_status")
            status_filter = validate_string_list(arguments.get("status"), "status")
            config = module.load_config(self.project_root)
            questions_dir = module.questions_directory(self.project_root, config)
            records = module.collect_questions(questions_dir)
            if status_filter:
                wanted = set(status_filter)
                records = [record for record in records if record["status"] in wanted]
            report = module.build_report(records)
            try:
                questions_dir_label = questions_dir.relative_to(self.project_root).as_posix()
            except ValueError:
                questions_dir_label = questions_dir.as_posix()
            return {
                "generated_at": module.datetime.now(module.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "questions_dir": questions_dir_label,
                **report,
            }

        if name == "query_index":
            return query_payload(self.project_root, arguments)

        if name == "intake_questions":
            intake = load_sibling_module("intake_questions")
            batch = validate_object(arguments.get("batch"), "batch")
            raw_questions = batch.get("questions")
            if isinstance(raw_questions, list):
                config = intake.load_config(self.project_root)
                max_questions = mcp_intake_batch_limit(config)
                submitted_questions = len(raw_questions)
                if submitted_questions > max_questions:
                    raise ToolExecutionError(
                        "Intake batch too large: "
                        f"{submitted_questions} question(s) exceeds "
                        f"run.max_mcp_intake_batch_questions {max_questions}.",
                        error_code="INTAKE_BATCH_TOO_LARGE",
                        details={
                            "submitted_questions": submitted_questions,
                            "max_mcp_intake_batch_questions": max_questions,
                            "config_field": "run.max_mcp_intake_batch_questions",
                        },
                    )
            dry_run = arguments.get("dry_run", False)
            if not isinstance(dry_run, bool):
                raise ToolExecutionError("dry_run must be a boolean")
            return intake.run_intake_document(
                self.project_root,
                batch,
                dry_run=dry_run,
                from_file_label="mcp",
            )

        if name == "export_answers":
            module = load_sibling_module("export_answers")
            status_filter = validate_string_list(arguments.get("status"), "status")
            return module.build_export(self.project_root, status_filter)

        if name == "source_requests_list":
            module = load_sibling_module("source_requests")
            statuses = validate_string_list(arguments.get("status"), "status", choices=module.REQUEST_STATUSES)
            args = argparse.Namespace(project_root=str(self.project_root), status=statuses)
            return module.run_list(args)

        raise KeyError(name)

    def call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        reject_unknown_keys(params, frozenset({"name", "arguments"}), "tools/call params")
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise ToolExecutionError("tools/call params.name must be a non-empty string")
        raw_arguments = params.get("arguments", {})
        if raw_arguments is None:
            raw_arguments = {}
        arguments = validate_object(raw_arguments, "arguments")
        allowed_arguments = TOOL_ARGUMENT_KEYS.get(name)
        if allowed_arguments is not None:
            reject_unknown_keys(arguments, allowed_arguments, f"{name} arguments")
        try:
            payload = self.call_tool_payload(name, arguments)
        except KeyError:
            raise
        except ToolExecutionError as exc:
            return error_content(exc.message, error_code=exc.error_code, details=exc.details)
        except SystemExit as exc:
            raw_message = system_exit_message(exc)
            return sanitized_error_content(
                exc,
                raw_message,
                error_code=getattr(exc, "error_code", None),
                details=getattr(exc, "details", None),
            )
        except Exception as exc:  # pragma: no cover - defensive protocol boundary
            return sanitized_error_content(exc, str(exc))
        return text_content(payload)

    def handle_message(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return jsonrpc_error(None, ERR_INVALID_REQUEST, "JSON-RPC message must be an object")
        message_id = jsonrpc_message_id(message)
        if message_id is not JSONRPC_ID_MISSING and not is_valid_jsonrpc_id(message_id):
            return jsonrpc_error(None, ERR_INVALID_REQUEST, "id must be a string, finite number, or null")
        if message_id is JSONRPC_ID_MISSING:
            return None
        if message.get("jsonrpc") != JSONRPC_VERSION:
            return jsonrpc_error(message_id, ERR_INVALID_REQUEST, "jsonrpc must be '2.0'")
        method = message.get("method")
        if not isinstance(method, str):
            return jsonrpc_error(message_id, ERR_INVALID_REQUEST, "method must be a string")

        if method == "notifications/initialized":
            return None
        if method == "initialize":
            params = message.get("params", {})
            if params is None:
                params = {}
            if not isinstance(params, dict):
                return jsonrpc_error(message_id, ERR_INVALID_PARAMS, "initialize params must be an object")
            return jsonrpc_response(message_id, self.initialize_result(params))
        if method == "tools/list":
            params = message.get("params", {})
            if params is not None and not isinstance(params, dict):
                return jsonrpc_error(message_id, ERR_INVALID_PARAMS, "tools/list params must be an object")
            return jsonrpc_response(message_id, {"tools": self.tool_definitions()})
        if method == "tools/call":
            params = message.get("params", {})
            if not isinstance(params, dict):
                return jsonrpc_error(message_id, ERR_INVALID_PARAMS, "tools/call params must be an object")
            try:
                result = self.call_tool(params)
            except KeyError as exc:
                return jsonrpc_error(message_id, ERR_INVALID_PARAMS, f"Unknown tool: {exc.args[0]}")
            except ToolExecutionError as exc:
                return jsonrpc_response(
                    message_id,
                    error_content(exc.message, error_code=exc.error_code, details=exc.details),
                )
            return jsonrpc_response(message_id, result)
        return jsonrpc_error(message_id, ERR_METHOD_NOT_FOUND, f"Unknown method: {method}")

    def internal_error_response(self, message_id: Any, exc: Exception) -> dict[str, Any]:
        correlation_id = uuid.uuid4().hex
        print(f"MCP internal error {correlation_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return jsonrpc_error(message_id, ERR_INTERNAL, "Internal server error", {"correlation_id": correlation_id})

    def serve(
        self,
        input_stream: TextIO,
        output_stream: TextIO,
        *,
        max_line_bytes: int = MAX_JSONRPC_LINE_BYTES,
    ) -> None:
        while True:
            message_id = None
            try:
                line_result = read_jsonrpc_line(input_stream, max_line_bytes)
                if line_result is None:
                    break
                line, oversized = line_result
                if oversized:
                    response = jsonrpc_error(
                        None,
                        ERR_INVALID_REQUEST,
                        JSONRPC_OVERSIZED_MESSAGE,
                        {"max_line_bytes": max_line_bytes},
                    )
                elif not line.strip():
                    continue
                else:
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError as exc:
                        response = jsonrpc_parse_error_response(exc)
                    else:
                        message_id = jsonrpc_safe_response_id(message)
                        response = self.handle_message(message)
            except Exception as exc:  # pragma: no cover - defensive protocol boundary
                response = self.internal_error_response(message_id, exc)
            if response is None:
                continue
            output_stream.write(json.dumps(response, separators=(",", ":"), sort_keys=False) + "\n")
            output_stream.flush()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    server = ResearchWikiMcpServer(args.target)
    server.serve(sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
