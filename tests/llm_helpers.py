"""Loopback-only endpoint with actual HTTP/SSE bytes and request counts."""

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def completion(payload, *, usage=True):
    message = {"role": "assistant", "content": "SYN-1=42"}
    finish = "stop"
    if payload.get("tools"):
        message["content"] = None
        message["tool_calls"] = [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": payload["tools"][0]["function"]["name"],
                    "arguments": '{"id":"SYN-1","value":42}',
                },
            }
        ]
        finish = "tool_calls"
    body = {
        "id": "synthetic-request-1",
        "model": payload["model"],
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }
    if usage:
        body["usage"] = {
            "prompt_tokens": 7,
            "completion_tokens": 3,
            "total_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 2},
            "completion_tokens_details": {"reasoning_tokens": 1},
        }
    return body


def sse(body, *, done=True):
    message = body["choices"][0]["message"]
    deltas = []
    if message.get("content"):
        deltas += [
            {"content": message["content"][:3]},
            {"content": message["content"][3:]},
        ]
    for call in message.get("tool_calls") or []:
        function = call["function"]
        deltas += [
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": call["id"],
                        "function": {
                            "name": function["name"],
                            "arguments": function["arguments"][:9],
                        },
                    }
                ]
            },
            {
                "tool_calls": [
                    {"index": 0, "function": {"arguments": function["arguments"][9:]}}
                ]
            },
        ]
    deltas.insert(0, {"reasoning_content": "Synthetic Ω reasoning."})
    parts = [
        {
            "id": body["id"],
            "model": body["model"],
            "choices": [{"index": 0, "delta": d, "finish_reason": None}],
        }
        for d in deltas
    ]
    parts.append(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": body["choices"][0]["finish_reason"],
                }
            ]
        }
    )
    if "usage" in body:
        parts.append({"choices": [], "usage": body["usage"]})
    data = b": keepalive\n\n" + b"".join(
        ("data: " + json.dumps(p, ensure_ascii=False) + "\n\n").encode() for p in parts
    )
    return data + (b"data: [DONE]\n\n" if done else b"")


@contextmanager
def endpoint(action=None):
    seen = []
    active = {"count": 0, "peak": 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                seen.append(payload)
                active["count"] += 1
                active["peak"] = max(active["peak"], active["count"])
                number = len(seen)
            try:
                reply = action(payload, number) if action else {}
                if reply.get("delay"):
                    time.sleep(reply["delay"])
                body = reply.get("body", completion(payload))
                data = reply.get(
                    "bytes",
                    sse(body) if payload.get("stream") else json.dumps(body).encode(),
                )
                self.send_response(reply.get("status", 200))
                self.send_header(
                    "Content-Type",
                    "text/event-stream"
                    if payload.get("stream")
                    else "application/json",
                )
                self.send_header("X-Request-ID", "local-request-" + str(number))
                self.send_header("X-Trace-ID", "local-trace-" + str(number))
                for k, v in reply.get("headers", {}).items():
                    self.send_header(k, v)
                self.send_header(
                    "Content-Length", str(reply.get("declared_length", len(data)))
                )
                self.end_headers()
                for piece in reply.get("pieces", [data]):
                    self.wfile.write(piece)
                    self.wfile.flush()
                    time.sleep(reply.get("piece_delay", 0))
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with lock:
                    active["count"] -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen, active
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
