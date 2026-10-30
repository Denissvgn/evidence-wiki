"""Bounded framing with one serial research request and responsive cancellation."""

import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

from ._pack_io import json_document
from .errors import EvidenceWikiError


def serve(server, input_stream, output_stream):
    from .onboarding_mcp import MAX_RESPONSE_BYTES, read_line, rpc_error, valid_request

    frames = queue.Queue(maxsize=16)
    stop = threading.Event()

    def read():
        while not stop.is_set():
            failed = False
            try:
                frame = read_line(input_stream)
            except UnicodeError:
                frame = (None, False)
            except Exception:
                frame = ("", True)
                failed = True
            while not stop.is_set():
                try:
                    frames.put(frame, timeout=0.05)
                    break
                except queue.Full:
                    continue
            if frame is None:
                return
            if failed:
                while not stop.is_set():
                    try:
                        frames.put(None, timeout=0.05)
                        return
                    except queue.Full:
                        continue

    def emit(response):
        if response is None:
            return
        rendered = json.dumps(response, ensure_ascii=False, allow_nan=False)
        if len(rendered.encode()) + 1 > MAX_RESPONSE_BYTES:
            rendered = json.dumps(rpc_error(response.get("id"), -32603, "Result exceeds byte bound; inspect owner state before retrying"))
        output_stream.write(rendered + "\n")
        output_stream.flush()

    def handle(value, cancellation=None):
        try:
            return server.handle_message(value, cancellation=cancellation)
        except Exception:
            identifier = value.get("id") if type(value) is dict and type(value.get("id")) in {str, int} else None
            return rpc_error(identifier, -32603, "Internal operation error; inspect owner state before retrying")

    reader = threading.Thread(target=read, daemon=True, name="scoped-mcp-reader")
    active = None
    eof = False
    reader.start()
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="scoped-mcp-research") as executor:
            while not eof or active is not None:
                if active is not None and active[1].done():
                    _, future, cancellation = active
                    response = future.result()
                    if not cancellation.is_set():
                        emit(response)
                    active = None
                if eof:
                    if active is not None:
                        time.sleep(0.02)
                    continue
                try:
                    frame = frames.get(timeout=0.02)
                except queue.Empty:
                    continue
                if frame is None:
                    eof = True
                    continue
                raw, oversized = frame
                if raw is None:
                    emit(rpc_error(None, -32700, "Invalid UTF-8 document"))
                    continue
                if oversized:
                    emit(rpc_error(None, -32600, "Message exceeds byte bound"))
                    continue
                if not raw.strip():
                    continue
                try:
                    value = json_document(raw.encode("utf-8"))
                except (EvidenceWikiError, UnicodeError):
                    emit(rpc_error(None, -32700, "Invalid bounded JSON document"))
                    continue
                if not valid_request(value):
                    emit(handle(value))
                    continue
                if (type(value) is dict and value.get("jsonrpc") == "2.0" and "id" not in value
                        and set(value) <= {"jsonrpc", "method", "params"} and value.get("method") == "notifications/cancelled"):
                    params = value.get("params", {})
                    if (type(params) is dict and set(params) <= {"requestId", "reason"}
                            and type(params.get("requestId")) in {str, int}
                            and ("reason" not in params or type(params["reason"]) is str)
                            and active is not None and type(active[0]) is type(params["requestId"])
                            and active[0] == params["requestId"]):
                        active[2].set()
                    continue
                if server.is_research_request(value) and server.ready:
                    if active is not None:
                        emit(rpc_error(value.get("id"), -32001, "Research request in progress; reconcile retained state after it settles"))
                    else:
                        cancellation = threading.Event()
                        context = copy_context()
                        future = executor.submit(context.run, handle, value, cancellation)
                        active = (value["id"], future, cancellation)
                else:
                    # Existing lifecycle calls stay serial and cannot race research writes.
                    if active is not None and type(value) is dict and value.get("method") == "tools/call" and "id" in value:
                        emit(rpc_error(value["id"], -32001, "Research request in progress"))
                    else:
                        emit(handle(value))
    finally:
        stop.set()
        server.close()
