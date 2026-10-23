"""Strict JSON and the frozen compatibility policy for one model route."""

from __future__ import annotations

import copy
import hashlib
import json
import math

SINGLE_TRAILING_BRACE = "deepseek_single_trailing_brace_v1"


def response_policy(adapter, protocol, model, provider):
    if (adapter, protocol, provider) == (
        "tokendance",
        "openai:chat-completions",
        "deepseek",
    ) and model in ("deepseek-v4.1-flash", "deepseek-v4.1-flash-1017"):
        return SINGLE_TRAILING_BRACE
    return None


def strict_json(text: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key: " + key)
            result[key] = value
        return result

    def constant(value):
        raise ValueError("Non-finite JSON value: " + value)

    def number(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("Non-finite JSON number: " + value)
        return parsed

    return json.loads(
        text, object_pairs_hook=pairs, parse_constant=constant, parse_float=number
    )


def _single_trailing_brace(arguments):
    """Remove one extra closing brace after a complete strict JSON object."""
    if not isinstance(arguments, str):
        return None
    try:
        strict_json(arguments)
    except json.JSONDecodeError as exc:
        offset = len(arguments.rstrip(" \t\r\n")) - 1
        if (
            exc.msg != "Extra data"
            or exc.pos != offset
            or arguments[offset:].strip(" \t\r\n") != "}"
        ):
            return None
    except (TypeError, ValueError):
        return None
    else:
        return None
    effective = arguments[:offset] + arguments[offset + 1 :]
    try:
        if not isinstance(strict_json(effective), dict):
            return None
    except (TypeError, ValueError):
        return None
    return {
        "policy": SINGLE_TRAILING_BRACE,
        "path": "/choices/0/message/tool_calls/0/function/arguments",
        "original_sha256": hashlib.sha256(arguments.encode("utf-8")).hexdigest(),
        "removed_offset": offset,
        "removed_character": "}",
        "effective_arguments": effective,
    }


def response_message(envelope, *, effective=False):
    exchange = envelope.get("exchange") or {}
    body = exchange.get("body")
    choices = body.get("choices") if isinstance(body, dict) else None
    if (
        not isinstance(choices, list)
        or len(choices) != 1
        or not isinstance(choices[0], dict)
    ):
        return {"invalid_response": body}
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return {"invalid_response": body}
    record = exchange.get("tool_arguments_normalization")
    if effective and record is not None:
        try:
            calls = message["tool_calls"]
            if len(calls) != 1 or choices[0].get("finish_reason") not in (
                "stop",
                "tool_calls",
            ):
                raise ValueError("Invalid normalization target.")
            expected = _single_trailing_brace(calls[0]["function"]["arguments"])
            if expected is None or record != expected:
                raise ValueError(
                    "The normalization record differs from the original arguments."
                )
        except (KeyError, TypeError, IndexError) as exc:
            raise ValueError("Invalid normalization target.") from exc
        message = copy.deepcopy(message)
        message["tool_calls"][0]["function"]["arguments"] = expected[
            "effective_arguments"
        ]
    return message


def argument_normalization(exchange, prepared):
    """Derive an audit record only for the explicitly frozen route and policy."""
    payload = prepared.get("payload", {})
    if (
        prepared.get("response_policy") != SINGLE_TRAILING_BRACE
        or response_policy(
            prepared.get("adapter"),
            prepared.get("protocol"),
            payload.get("model"),
            "deepseek",
        )
        != SINGLE_TRAILING_BRACE
        or payload.get("provider") != {"only": ["deepseek"], "allow_fallbacks": False}
    ):
        return None
    transport = exchange.get("transport") or {}
    if (
        transport.get("response_body_complete") is False
        or transport.get("sse_done") is False
        and payload.get("stream")
        or transport.get("raw_response_truncated") is True
    ):
        return None
    message = response_message({"exchange": exchange})
    calls = message.get("tool_calls")
    tools = payload.get("tools")
    if (
        not isinstance(calls, list)
        or len(calls) != 1
        or not isinstance(tools, list)
        or len(tools) != 1
    ):
        return None
    try:
        call = calls[0]
        if (
            exchange["body"]["choices"][0]["finish_reason"]
            not in ("stop", "tool_calls")
            or call.get("type", "function") != "function"
            or call["function"]["name"] != tools[0]["function"]["name"]
        ):
            return None
        return _single_trailing_brace(call["function"]["arguments"])
    except (KeyError, TypeError, AttributeError):
        return None


def apply_response_policy(exchange, prepared):
    record = argument_normalization(exchange, prepared)
    if record is not None:
        exchange["tool_arguments_normalization"] = record
