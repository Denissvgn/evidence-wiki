"""A deterministic subprocess peer for adversarial transport cases."""

import json
import subprocess
import sys
import time
from pathlib import Path


def main():
    if "--version" in sys.argv:
        print("0.87.0")
        return
    state = {"sessionId": "fixed", "model": {"provider": "fixture", "id": "fixture"},
             "isStreaming": False, "isCompacting": False, "pendingMessageCount": 0}
    clear_response = None
    for line in sys.stdin.buffer:
        request = json.loads(line)
        kind = request["type"]
        response = {"type": "response", "id": request["id"], "command": kind, "success": True}
        if kind == "get_state":
            response["data"] = state
        if kind == "clear_queue":
            clear_response = response
            continue
        if kind == "abort" and clear_response is not None:
            print(json.dumps(response), flush=True)
            print(json.dumps(clear_response), flush=True)
            clear_response = None
            continue
        if kind == "prompt":
            message = request["message"]
            if message == "eof-before-ack":
                return
            if message == "spawn-child":
                directory = Path(__file__).parent
                program = "import signal,sys,time;from pathlib import Path;" + \
                    "root=Path(sys.argv[1]);signal.signal(signal.SIGTERM,lambda *_:(root.joinpath('child-stopped').write_text('stopped'),sys.exit(0)));" + \
                    "root.joinpath('child-ready').write_text('ready');time.sleep(60)"
                subprocess.Popen([sys.executable, "-c", program, str(directory)])
                deadline = time.monotonic() + 3
                while not (directory / "child-ready").is_file() and time.monotonic() < deadline:
                    time.sleep(.01)
            if message == "rejected":
                response["success"] = False
                print(json.dumps(response), flush=True)
                continue
            if message == "wrong-id":
                response["id"] = "unrelated"
            print(json.dumps(response), flush=True)
            if message == "eof":
                return
            if message in {"hanging", "wrong-id"}:
                continue
            if message == "session-change":
                state["sessionId"] = "changed"
            for event in ({"type": "agent_start"},
                          {"type": "message_end", "message": {"stopReason": "error" if message == "failed" else "stop", "text": "not a receipt"}},
                          {"type": "agent_end", "messages": [], "willRetry": False}, {"type": "agent_settled"}):
                print(json.dumps(event), flush=True)
            continue
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
