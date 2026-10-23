"""Token Dance HTTP/SSE adapter, with no implicit retries or fallbacks.

Reuses the raw transport and SSE reconstruction approach of Stella HVS
llm_batch.py / structured_output.py, MIT, Copyright (c) 2026 Zhang Jian.
"""

from __future__ import annotations

import json
import socket
import tempfile
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

from ..records.roster_validation import strict_json
from .config import BASE_URL, CallRequest, CallResult, RuntimeConfig, parameter_manifest
from .stream import (
    RawResponse,
    SSEBody,
    SSEDecoder,
    UploadAdapter,
    exception_facts,
    upload_gate,
)


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


def network_retry_allowed(envelope, runtime):
    """Retry archived transport failures only, retaining uncertain attempts."""
    if envelope.get("retryable") and not envelope.get("uncertain"):
        return True
    if not runtime.retry_post_send_network_errors:
        return False
    error = envelope.get("error")
    exchange = envelope.get("exchange") or {}
    transport = exchange.get("transport") or {}
    if error == "http_error":
        status = exchange.get("status")
        return type(status) is int and (status >= 500 or status in {408, 499})
    if error == "stream_incomplete":
        return transport.get("phase") == "decode"
    if error == "completion_unknown":
        return transport.get("phase") in {
            "connect",
            "upload",
            "response_headers",
            "read",
        } and bool(
            transport.get("exceptions") or transport.get("total_deadline_expired")
        )
    return False


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
    merged = SSEBody(strict_json)
    for event in events:
        merged.add(event)
    if merged.error:
        raise OutputFailure("invalid_stream_event")
    return merged.result(), merged.done


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
        request = CallRequest.model_validate(request.model_dump())
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
        has_images = any(isinstance(m.content, list) for m in request.messages)
        if has_images and config.model != "deepseek-v4.1-flash":
            raise ValueError(
                "Image input is currently configured only for deepseek-v4.1-flash; images will not be discarded"
            )
        if config.stream:
            payload["stream_options"] = {"include_usage": True}
        if request.tool is not None:
            payload["tools"] = [
                {"type": "function", "function": request.tool.model_dump()}
            ]
            # Reasoning routes use auto; local validation still requires exactly one call.
            payload["tool_choice"] = "auto"
        if has_images:
            image_count = sum(
                part.get("type") == "image_url"
                for message in payload["messages"]
                if isinstance(message.get("content"), list)
                for part in message["content"]
            )
            if (
                image_count > 600
                or len(json.dumps(payload).encode()) > 48 * 1024 * 1024
            ):
                raise ValueError(
                    "Image request exceeds the configured image count or body size limit"
                )
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
        deadline = started + runtime.total_timeout
        expired = threading.Event()
        sockets = []
        phase_started = started
        diagnostics = {
            "phase": "upload_queue",
            "phase_seconds": {},
            "request_bytes": 0,
            "request_started": False,
            "response_bytes": 0,
            "total_deadline_expired": False,
            "exceptions": [],
        }
        exchange = {
            "status": None,
            "headers": {},
            "events": [],
            "body": None,
            "text": "",
            "transport": diagnostics,
        }
        # Ownership transfers to the coordinator, which archives and closes it.
        spool = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115
        raw = RawResponse(spool, credential.get_secret_value())
        exchange["_raw_stream"] = spool
        merged = SSEBody(strict_json)
        decoder = SSEDecoder(merged.add)
        parse_error = None
        gate = upload_gate(runtime)
        held = False

        def phase(name):
            nonlocal phase_started
            now = time.monotonic()
            previous = diagnostics["phase"]
            times = diagnostics["phase_seconds"]
            times[previous] = times.get(previous, 0) + now - phase_started
            diagnostics["phase"] = name
            if name == "upload":
                diagnostics["request_started"] = True
            phase_started = now

        def release_upload():
            nonlocal held
            if held:
                held = False
                gate.release()

        def expire():
            expired.set()
            # Cover upload, waiting for headers and body reads, including trickles.
            for sock in sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        timer = threading.Timer(runtime.total_timeout, expire)
        timer.daemon = True
        session = requests.Session()
        session.trust_env = False
        adapter = UploadAdapter(runtime, phase, sockets, release_upload, deadline)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        response = None

        def snapshot():
            raw.write(b"", final=True)
            spool.flush()
            spool.seek(0)
            exchange["text"] = raw.preview.decode("utf-8", errors="replace")
            exchange["text_is_preview"] = diagnostics["response_bytes"] > 65536
            diagnostics["elapsed_seconds"] = time.monotonic() - started
            diagnostics["total_deadline_expired"] = expired.is_set()
            if payload.get("stream") and exchange["status"] == 200:
                exchange["body"] = merged.result()
                diagnostics["sse_events"] = merged.events
                diagnostics["sse_done"] = merged.done
            return redact(exchange, credential)

        timer.start()
        try:
            # Encoding once gives the exact upload byte count and Content-Length.
            data = json.dumps(payload, allow_nan=False).encode("utf-8")
            diagnostics["request_bytes"] = len(data)
            held = gate.acquire(timeout=max(0, deadline - time.monotonic()))
            if not held or expired.is_set():
                raise TransportFailure("upload_queue_expired")
            phase("connect")
            response = session.post(
                runtime.base_url.rstrip("/") + "/chat/completions",
                data=data,
                headers={
                    "Authorization": "Bearer " + credential.get_secret_value(),
                    "Content-Type": "application/json",
                },
                stream=True,
                allow_redirects=False,
                timeout=Timeout(
                    connect=runtime.connect_timeout, read=runtime.idle_timeout
                ),
            )
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
            phase("read")
            for chunk in response.iter_content(chunk_size=8192):
                if expired.is_set() or time.monotonic() >= deadline:
                    raise TransportFailure("completion_unknown", uncertain=True)
                if payload.get("stream") and response.status_code == 200:
                    raw.file = None
                raw.write(chunk)
                diagnostics["response_bytes"] += len(chunk)
                if diagnostics["response_bytes"] > runtime.max_response_bytes:
                    raise TransportFailure("response_limit", uncertain=True)
                if (
                    payload.get("stream")
                    and response.status_code == 200
                    and parse_error is None
                ):
                    try:
                        decoder.feed(chunk)
                    except (ValueError, TypeError, KeyError, AttributeError) as exc:
                        parse_error = exc
            if expired.is_set():
                raise TransportFailure("completion_unknown", uncertain=True)
            phase("decode")
            raw.write(b"", final=True)
            spool.flush()
            spool.seek(0)
            if response.status_code != 200:
                try:
                    exchange["body"] = json.load(spool)
                except (ValueError, TypeError):
                    pass
                status = response.status_code
                raise TransportFailure(
                    "rate_limited" if status == 429 else "http_error",
                    uncertain=status >= 500 or status in {408, 499},
                    retryable=status == 429,
                    retry_after=_retry_after(response.headers.get("Retry-After")),
                )
            if payload.get("stream"):
                if parse_error is None:
                    decoder.feed(b"", final=True)
                if parse_error is not None or merged.error:
                    raise ValueError("invalid_stream_event")
                if not merged.done:
                    raise TransportFailure("stream_incomplete", uncertain=True)
                exchange["body"] = merged.result()
            else:
                exchange["body"] = strict_json(spool.read().decode("utf-8"))
            if not isinstance(exchange["body"], dict):
                raise TypeError("invalid_response")
            phase("complete")
            return snapshot()
        except TransportFailure as exc:
            exc.exchange = snapshot()
            raise
        except requests.RequestException as exc:
            diagnostics["exceptions"] = exception_facts(exc)
            reason = getattr(exc.args[0], "reason", None) if exc.args else None
            known_unsent = (
                diagnostics["phase"] == "connect"
                and not diagnostics.get("request_started", False)
                and (
                    isinstance(exc, (requests.ConnectTimeout, requests.ReadTimeout))
                    or isinstance(reason, NewConnectionError)
                )
            )
            raise TransportFailure(
                "connection_failed" if known_unsent else "completion_unknown",
                uncertain=not known_unsent,
                retryable=known_unsent,
                exchange=snapshot(),
            ) from None
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError) as exc:
            diagnostics["exceptions"] = exception_facts(exc)
            raise TransportFailure("invalid_response", exchange=snapshot()) from None
        except BaseException:
            spool.close()
            raise
        finally:
            release_upload()
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
            if not isinstance(tool_calls[0], dict):
                raise OutputFailure("invalid_message")
            function = tool_calls[0].get("function")
            if not isinstance(function, dict):
                raise OutputFailure("invalid_tool_arguments")
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
