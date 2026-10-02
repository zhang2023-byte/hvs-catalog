"""Token Dance HTTP/SSE adapter, with no implicit retries or fallbacks.

Reuses the raw transport and SSE reconstruction approach of Stella HVS
llm_batch.py / structured_output.py, MIT, Copyright (c) 2026 Zhang Jian.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import urlsplit

import requests
from jsonschema import Draft202012Validator, ValidationError
from pydantic import SecretStr
from referencing.exceptions import Unresolvable
from urllib3.exceptions import NewConnectionError
from urllib3.util import Timeout

from .config import BASE_URL, CallRequest, CallResult, RuntimeConfig, parameter_manifest


def strict_json(text: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("non-finite JSON value")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def redact(value, secret: SecretStr):
    if isinstance(value, str):
        return value.replace(secret.get_secret_value(), "[REDACTED]")
    if isinstance(value, dict):
        return {redact(str(k), secret): redact(v, secret) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secret) for v in value]
    return value


class TransportFailure(Exception):
    def __init__(
        self, kind, *, uncertain=False, retryable=False, exchange=None, retry_after=None
    ):
        super().__init__(kind)
        self.kind = kind
        self.uncertain = uncertain
        self.retryable = retryable
        self.exchange = exchange or {}
        self.retry_after = retry_after


class OutputFailure(ValueError):
    pass


class ModelAdapter(Protocol):
    """Trusted program object; future pi-ai bridges implement this boundary."""

    name: str

    def validate_runtime(self, runtime: RuntimeConfig) -> None: ...

    def prepare(self, request: CallRequest) -> dict: ...
    def send(
        self, payload: dict, runtime: RuntimeConfig, credential: SecretStr
    ) -> dict: ...
    def parse(self, exchange: dict, request: CallRequest) -> CallResult: ...


def token_usage(body: dict) -> dict:
    usage = body.get("usage")
    usage = usage if isinstance(usage, dict) else {}

    def count(*values):
        return next((v for v in values if type(v) is int and v >= 0), None)

    inputs = count(usage.get("prompt_tokens"), usage.get("input_tokens"))
    outputs = count(usage.get("completion_tokens"), usage.get("output_tokens"))
    total = count(usage.get("total_tokens"))
    if total is None and inputs is not None and outputs is not None:
        total = inputs + outputs
    details = usage.get("prompt_tokens_details") or {}
    completion = usage.get("completion_tokens_details") or {}
    return {
        "tokens": total,
        "input_tokens": inputs,
        "output_tokens": outputs,
        "cached_tokens": count(
            details.get("cached_tokens") if isinstance(details, dict) else None,
            usage.get("prompt_cache_hit_tokens"),
        ),
        "reasoning_tokens": count(
            completion.get("reasoning_tokens") if isinstance(completion, dict) else None
        ),
    }


def _retry_after(value):
    if not value:
        return None
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(
                0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
            )
        except (ValueError, TypeError):
            return None


def _merge_sse(events: list[str]) -> tuple[dict, bool]:
    body, message, tools = {}, {"content": "", "reasoning_content": ""}, {}
    finish = None
    done = False
    for event in events:
        if event == "[DONE]":
            done = True
            continue
        part = strict_json(event)
        if not isinstance(part, dict) or "error" in part:
            raise OutputFailure("invalid_stream_event")
        for k in ("id", "model", "usage", "created"):
            if k in part:
                body[k] = part[k]
        choices = part.get("choices", [])
        if not isinstance(choices, list) or len(choices) > 1:
            raise OutputFailure("invalid_choices")
        if not choices:
            continue
        choice = choices[0]
        if choice.get("index", 0) != 0:
            raise OutputFailure("invalid_choice_index")
        finish = choice.get("finish_reason") or finish
        delta = choice.get("delta", {})
        for k in ("content", "reasoning_content"):
            value = delta.get(k)
            if value is not None:
                if not isinstance(value, str):
                    raise OutputFailure("invalid_delta")
                message[k] += value
        for tool in delta.get("tool_calls") or []:
            index = tool.get("index", 0)
            current = tools.setdefault(
                index,
                {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                },
            )
            if tool.get("id"):
                current["id"] = tool["id"]
            for k in ("name", "arguments"):
                value = (tool.get("function") or {}).get(k)
                if value is not None:
                    current["function"][k] += value
    if tools:
        message["tool_calls"] = [tools[k] for k in sorted(tools)]
    body["choices"] = [{"index": 0, "message": message, "finish_reason": finish}]
    return body, done


class TokenDanceAdapter:
    name = "tokendance"

    @staticmethod
    def validate_runtime(runtime: RuntimeConfig) -> None:
        url = urlsplit(runtime.base_url)
        if runtime.base_url.rstrip("/") != BASE_URL and not (
            url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1")
        ):
            raise ValueError(
                "Token Dance adapter requires its official URL or loopback"
            )

    def prepare(self, request: CallRequest) -> dict:
        config = request.model
        # Revalidate even if a mutable Pydantic instance was edited by a caller.
        config = type(config).model_validate(config.model_dump())
        payload = {
            "model": config.model,
            "messages": [m.wire() for m in request.messages],
            "stream": config.stream,
            "provider": {"only": [config.provider], "allow_fallbacks": False},
        }
        parameters = dict(config.parameters or {})
        thinking = {}
        if "thinking_type" in parameters:
            thinking["type"] = parameters.pop("thinking_type")
        if "clear_thinking" in parameters:
            thinking["clear_thinking"] = parameters.pop("clear_thinking")
        if thinking:
            payload["thinking"] = thinking
        payload.update(parameters)
        if config.stream:
            payload["stream_options"] = {"include_usage": True}
        if request.tool is not None:
            payload["tools"] = [
                {"type": "function", "function": request.tool.model_dump()}
            ]
            # Reasoning routes use auto; local validation still requires exactly one call.
            payload["tool_choice"] = "auto"
        return {
            "payload": payload,
            "parameters": parameter_manifest(config),
            "protocol": config.protocol,
            "adapter": self.name,
        }

    def send(
        self, payload: dict, runtime: RuntimeConfig, credential: SecretStr
    ) -> dict:
        self.validate_runtime(runtime)
        started = time.monotonic()
        expired = threading.Event()
        response_holder = []
        exchange = {
            "status": None,
            "headers": {},
            "events": [],
            "body": None,
            "text": "",
        }

        def expire():
            expired.set()
            if response_holder:
                raw = response_holder[0].raw
                # Abort a trickling response at the wall deadline, not merely idle timeout.
                sock = getattr(
                    getattr(
                        getattr(getattr(raw, "_fp", None), "fp", None), "raw", None
                    ),
                    "_sock",
                    None,
                )
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        timer = threading.Timer(runtime.total_timeout, expire)
        timer.daemon = True
        timer.start()
        session = requests.Session()
        session.trust_env = False
        response = None
        raw_bytes = bytearray()
        try:
            response = session.post(
                runtime.base_url.rstrip("/") + "/chat/completions",
                json=payload,
                headers={"Authorization": "Bearer " + credential.get_secret_value()},
                stream=True,
                allow_redirects=False,
                timeout=Timeout(
                    total=runtime.total_timeout,
                    connect=runtime.connect_timeout,
                    read=runtime.idle_timeout,
                ),
            )
            response_holder.append(response)
            exchange["status"] = response.status_code
            exchange["headers"] = {
                k.lower(): v
                for k, v in response.headers.items()
                if k.lower()
                in (
                    "content-type",
                    "x-request-id",
                    "request-id",
                    "x-trace-id",
                    "trace-id",
                    "retry-after",
                    "tokendance-recovery-action",
                )
            }
            for chunk in response.iter_content(chunk_size=1):
                if (
                    expired.is_set()
                    or time.monotonic() - started >= runtime.total_timeout
                ):
                    raise TransportFailure("completion_unknown", uncertain=True)
                raw_bytes.extend(chunk)
                if len(raw_bytes) > runtime.max_response_bytes:
                    raise TransportFailure("response_limit", uncertain=True)
            if expired.is_set():
                raise TransportFailure("completion_unknown", uncertain=True)
            text = raw_bytes.decode("utf-8")
            exchange["text"] = text
            if response.status_code != 200:
                try:
                    exchange["body"] = strict_json(text)
                except (ValueError, TypeError):
                    pass
                status = response.status_code
                raise TransportFailure(
                    "rate_limited" if status == 429 else "http_error",
                    uncertain=status >= 500,
                    retryable=status == 429,
                    retry_after=_retry_after(response.headers.get("Retry-After")),
                )
            if payload.get("stream"):
                events, data = [], []
                # SSE recognizes CR/LF; Unicode separators inside JSON strings
                # are payload characters, not event boundaries.
                for line in (part.rstrip("\r") for part in text.split("\n") + [""]):
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip(" "))
                    elif line == "" and data:
                        events.append("\n".join(data))
                        data = []
                exchange["events"] = events
                body, done = _merge_sse(events)
                exchange["body"] = body
                if not done:
                    raise TransportFailure("stream_incomplete", uncertain=True)
            else:
                exchange["body"] = strict_json(text)
            if not isinstance(exchange["body"], dict):
                raise OutputFailure("invalid_response")
            return redact(exchange, credential)
        except TransportFailure as exc:
            exchange["text"] = raw_bytes.decode("utf-8", errors="replace")
            exc.exchange = redact(exchange, credential)
            raise
        except requests.ConnectTimeout:
            raise TransportFailure(
                "connection_failed",
                retryable=True,
                exchange=redact(exchange, credential),
            ) from None
        except requests.ConnectionError as exc:
            reason = getattr(exc.args[0], "reason", None) if exc.args else None
            known_unsent = isinstance(reason, NewConnectionError)
            exchange["text"] = raw_bytes.decode("utf-8", errors="replace")
            raise TransportFailure(
                "connection_failed" if known_unsent else "completion_unknown",
                uncertain=not known_unsent,
                retryable=known_unsent,
                exchange=redact(exchange, credential),
            ) from None
        except requests.RequestException:
            exchange["text"] = raw_bytes.decode("utf-8", errors="replace")
            raise TransportFailure(
                "completion_unknown",
                uncertain=True,
                exchange=redact(exchange, credential),
            ) from None
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
            exchange["text"] = raw_bytes.decode("utf-8", errors="replace")
            raise TransportFailure(
                "invalid_response", exchange=redact(exchange, credential)
            ) from None
        finally:
            timer.cancel()
            if response is not None:
                response.close()
            session.close()

    def parse(self, exchange: dict, request: CallRequest) -> dict:
        body = exchange.get("body")
        if not isinstance(body, dict):
            raise OutputFailure("invalid_response")
        choices = body.get("choices")
        if (
            not isinstance(choices, list)
            or len(choices) != 1
            or not isinstance(choices[0], dict)
        ):
            raise OutputFailure("invalid_choices")
        choice = choices[0]
        finish = choice.get("finish_reason")
        if finish == "length":
            raise OutputFailure("truncated")
        if finish not in ("stop", "tool_calls"):
            raise OutputFailure("invalid_finish_reason")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise OutputFailure("invalid_message")
        content = message.get("content")
        tool_calls = message.get("tool_calls") or []
        parsed = None
        if request.tool is not None:
            if not isinstance(tool_calls, list) or len(tool_calls) != 1:
                raise OutputFailure("missing_or_multiple_tool_calls")
            function = tool_calls[0].get("function") or {}
            if function.get("name") != request.tool.name:
                raise OutputFailure("wrong_tool")
            try:
                parsed = strict_json(function.get("arguments", ""))
                Draft202012Validator(request.tool.parameters).validate(parsed)
            except (ValueError, TypeError, ValidationError, Unresolvable):
                raise OutputFailure("invalid_tool_arguments") from None
        elif not isinstance(content, str) or not content.strip():
            raise OutputFailure("empty_response")
        return {
            "content": content,
            "reasoning_content": message.get("reasoning_content"),
            "tool_calls": tool_calls,
            "parsed": parsed,
            "finish_reason": finish,
            "returned_model": body.get("model"),
            "usage": token_usage(body),
            "service_request_id": exchange.get("headers", {}).get("x-request-id")
            or body.get("id"),
            "trace_id": exchange.get("headers", {}).get("x-trace-id")
            or exchange.get("headers", {}).get("trace-id"),
        }
