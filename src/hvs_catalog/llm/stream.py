"""Incremental SSE decoding and separate upload limits for HTTP requests."""

from __future__ import annotations

import codecs
import threading
import time

from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

_UPLOAD_LOCK = threading.Lock()
_UPLOAD_LIMITS = {}


def upload_gate(runtime):
    """Share an upload gate between adapters in this process, across model steps."""
    key = (str(runtime.quota_store), runtime.quota_group)
    limit = min(runtime.upload_concurrency, runtime.concurrency)
    with _UPLOAD_LOCK:
        previous = _UPLOAD_LIMITS.get(key)
        if previous is not None and previous[0] != limit:
            raise ValueError("concurrent upload configurations conflict")
        if previous is None:
            previous = (limit, threading.BoundedSemaphore(limit))
            _UPLOAD_LIMITS[key] = previous
        return previous[1]


class UploadAdapter(HTTPAdapter):
    """Keep connect/TLS, body upload and response waiting on separate clocks."""

    def __init__(self, runtime, phase, sockets, release_upload, deadline):
        class Connection:
            def _new_conn(self):
                sock = super()._new_conn()
                sockets.append(sock)
                return sock

            def connect(self):
                phase("connect")
                super().connect()
                sockets.append(self.sock)

            def request(self, *args, **kwargs):
                try:
                    if self.sock is None:
                        self.connect()
                    phase("upload")
                    self.timeout = min(
                        runtime.upload_timeout, max(0.001, deadline - time.monotonic())
                    )
                    super().request(*args, **kwargs)
                    phase("response_headers")
                finally:
                    # The request body is sent before waiting for model output.
                    release_upload()

        class HTTP(Connection, HTTPConnection):
            pass

        class HTTPS(Connection, HTTPSConnection):
            pass

        class HTTPPool(HTTPConnectionPool):
            ConnectionCls = HTTP

        class HTTPSPool(HTTPSConnectionPool):
            ConnectionCls = HTTPS

        super().__init__(max_retries=0)
        self.poolmanager.pool_classes_by_scheme = {
            "http": HTTPPool,
            "https": HTTPSPool,
        }


class SSEDecoder:
    """Decode UTF-8 and CR/LF event frames without retaining raw events."""

    def __init__(self, consume):
        self.decoder = codecs.getincrementaldecoder("utf-8")()
        self.line = ""
        self.data = []
        self.consume = consume
        self.after_cr = False

    def feed(self, chunk, *, final=False):
        # splitlines() would corrupt Unicode separators inside JSON strings.
        text = self.decoder.decode(chunk, final=final)
        start = 0
        for i, char in enumerate(text):
            if self.after_cr:
                self.after_cr = False
                if char == "\n":
                    start = i + 1
                    continue
            if char in "\r\n":
                self.line += text[start:i]
                self._line()
                start = i + 1
                self.after_cr = char == "\r"
        self.line += text[start:]
        if final:
            self._line()
            self._line()

    def _line(self):
        line, self.line = self.line, ""
        if line.startswith("data:"):
            self.data.append(line[5:].lstrip(" "))
        elif not line and self.data:
            self.consume("\n".join(self.data))
            self.data.clear()


class SSEBody:
    """Accumulate only semantic deltas, joining each output once at the end."""

    def __init__(self, strict_json):
        self.strict_json = strict_json
        self.body = {}
        self.message = {"content": [], "reasoning_content": []}
        self.tools = {}
        self.finish = None
        self.done = False
        self.error = None
        self.events = 0

    def add(self, event):
        self.events += 1
        if event == "[DONE]":
            self.done = True
            return
        try:
            self._add(self.strict_json(event))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            # Drain the response before reporting a known malformed completion.
            self.error = type(exc).__name__

    def _add(self, part):
        if not isinstance(part, dict) or "error" in part:
            raise ValueError("invalid_stream_event")
        for key in ("id", "model", "usage", "created"):
            if key in part:
                self.body[key] = part[key]
        choices = part.get("choices", [])
        if not isinstance(choices, list) or len(choices) > 1:
            raise ValueError("invalid_choices")
        if not choices:
            return
        choice = choices[0]
        if choice.get("index", 0) != 0:
            raise ValueError("invalid_choice_index")
        self.finish = choice.get("finish_reason") or self.finish
        delta = choice.get("delta", {})
        for key in self.message:
            value = delta.get(key)
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError("invalid_delta")
                self.message[key].append(value)
        for tool in delta.get("tool_calls") or []:
            current = self.tools.setdefault(
                tool.get("index", 0),
                {
                    "id": "",
                    "type": "function",
                    "function": {"name": [], "arguments": []},
                },
            )
            if tool.get("id"):
                current["id"] = tool["id"]
            for key in ("name", "arguments"):
                value = (tool.get("function") or {}).get(key)
                if value is not None:
                    if not isinstance(value, str):
                        raise ValueError("invalid_tool_delta")
                    current["function"][key].append(value)

    def result(self):
        message = {key: "".join(value) for key, value in self.message.items()}
        if self.tools:
            message["tool_calls"] = [
                {
                    **self.tools[key],
                    "function": {
                        field: "".join(chunks)
                        for field, chunks in self.tools[key]["function"].items()
                    },
                }
                for key in sorted(self.tools)
            ]
        return {
            **self.body,
            "choices": [{"index": 0, "message": message, "finish_reason": self.finish}],
        }


class RawResponse:
    """Redact secrets across arbitrary byte boundaries before writing the spool."""

    def __init__(self, file, secret):
        self.file = file
        self.secret = secret.encode()
        self.pending = b""
        self.preview = bytearray()

    def write(self, chunk, *, final=False):
        value = self.pending + chunk
        end = len(value) if final else max(0, len(value) - len(self.secret) + 1)
        parts, start = [], 0
        while start < end:
            found = value.find(self.secret, start)
            if found < 0 or found >= end:
                parts.append(value[start:end])
                start = end
            else:
                parts.extend((value[start:found], b"[REDACTED]"))
                start = found + len(self.secret)
        self.pending = value[start:]
        safe = b"".join(parts)
        self.file.write(safe)
        self.preview.extend(safe[: max(0, 65536 - len(self.preview))])


def exception_facts(exc):
    """Preserve exception types and errno, without URLs, headers or credentials."""
    result, seen = [], set()

    def walk(error):
        if not isinstance(error, BaseException) or id(error) in seen:
            return
        seen.add(id(error))
        result.append(
            {"type": type(error).__name__, "errno": getattr(error, "errno", None)}
        )
        for child in (
            *error.args,
            getattr(error, "reason", None),
            error.__cause__,
            error.__context__,
        ):
            walk(child)

    walk(exc)
    return result
