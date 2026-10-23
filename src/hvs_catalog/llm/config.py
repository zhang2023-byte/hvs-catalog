"""Fixed Token Dance profiles and explicitly declared, non-secret configuration."""

from __future__ import annotations

import base64
import binascii
import json
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, TypedDict
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, SchemaError
from pydantic import Field, SecretStr, model_validator

from ..records.common import Config
from ..records.pdf import image_media_type
from .parameters import ParameterPolicy, ParameterSpec

BASE_URL = "https://tokendance.space/gateway/v1"
PROTOCOL = "openai:chat-completions"
InputMode = Literal["text_and_images", "text_only"]


def _spec(name, types=(int, float), minimum=None, maximum=None, choices=None, **bounds):
    return ParameterSpec(
        name, types, minimum=minimum, maximum=maximum, choices=choices, **bounds
    )


@dataclass(frozen=True)
class ModelProfile:
    model: str
    provider: str
    output_parameter: str
    maximum_output: int
    defaults: dict
    policy: ParameterPolicy
    sources: tuple[str, ...]
    output_accounting: str
    maximum_images: int = 600

    def describe(self) -> dict:
        return {
            "model": self.model,
            "provider": self.provider,
            "adapter": "tokendance",
            "protocol": PROTOCOL,
            "default_parameters": dict(self.defaults),
            "maximum_output": self.maximum_output,
            "maximum_images": self.maximum_images,
            "input_modalities": ["text", "image"] if self.maximum_images else ["text"],
            "output_accounting": self.output_accounting,
            "parameters": self.policy.describe(),
            "sources": list(self.sources),
        }


def _profile(model, provider, cap, defaults, extra, source, accounting):
    output = "max_completion_tokens" if provider == "xiaomi" else "max_tokens"
    common = (
        _spec(output, (int,), 1, cap),
        _spec(
            "temperature",
            minimum=0,
            maximum={"deepseek": 2, "bigmodel": 1, "xiaomi": 1.5, "alibaba": 2}[
                provider
            ],
            exclusive_maximum=provider == "alibaba",
        ),
        _spec(
            "top_p",
            minimum=0.01 if provider in ("xiaomi", "bigmodel") else 0,
            maximum=1,
            exclusive_minimum=provider == "alibaba",
        ),
    )
    return ModelProfile(
        model,
        provider,
        output,
        cap,
        {output: cap, **defaults},
        ParameterPolicy(common + tuple(extra)),
        (source,),
        accounting,
        50 if provider == "bigmodel" else 600,
    )


PROFILES = {
    p.model: p
    for p in (
        *(
            _profile(
                model,
                "deepseek",
                393216,
                {"thinking_type": "enabled", "reasoning_effort": "max"},
                (
                    _spec("thinking_type", (str,), choices=("enabled", "disabled")),
                    _spec(
                        "reasoning_effort",
                        (str,),
                        choices=("none", "low", "high", "max"),
                    ),
                    _spec("frequency_penalty", minimum=-2, maximum=2),
                    _spec("presence_penalty", minimum=-2, maximum=2),
                ),
                "https://api-docs.deepseek.com/api/create-chat-completion/",
                "Combined completion limit; retain provider reasoning usage separately.",
            )
            for model in ("deepseek-v4.1-flash", "deepseek-v4.1-flash-1017")
        ),
        _profile(
            "glm-5.3-flash",
            "bigmodel",
            131072,
            {"thinking_type": "enabled", "reasoning_effort": "max"},
            (
                _spec("thinking_type", (str,), choices=("enabled",)),
                _spec("reasoning_effort", (str,), choices=("low", "high", "max")),
                _spec("clear_thinking", (bool,)),
                _spec("tool_stream", (bool,)),
            ),
            "https://docs.z.ai/api-reference/llm/chat-completion",
            "Published API output limit; do not substitute self-hosted benchmark limits.",
        ),
        _profile(
            "mimo-v2.6-flash",
            "xiaomi",
            131072,
            {"thinking_type": "enabled"},
            (
                _spec("thinking_type", (str,), choices=("enabled", "disabled")),
                _spec("frequency_penalty", minimum=-2, maximum=2),
                _spec("presence_penalty", minimum=-2, maximum=2),
            ),
            "https://mimo.mi.com/docs/en-US/api/chat/openai-api",
            "max_completion_tokens includes visible output and reasoning tokens.",
        ),
        _profile(
            "qwen3.8-flash",
            "alibaba",
            131072,
            {"enable_thinking": True, "thinking_budget": 131072},
            (
                _spec("enable_thinking", (bool,)),
                _spec("thinking_budget", (int,), 0, 262144),
                _spec(
                    "reasoning_effort",
                    (str,),
                    choices=(
                        "none",
                        "minimal",
                        "low",
                        "medium",
                        "high",
                        "xhigh",
                        "max",
                    ),
                ),
                _spec("preserve_thinking", (bool,)),
                _spec("presence_penalty", minimum=-2, maximum=2),
                _spec("top_k", (int,), 0),
                _spec("seed", (int,), 0, 2147483647),
                _spec("tool_stream", (bool,)),
            ),
            "https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions",
            "Visible output limit and thinking_budget are separate controls.",
        ),
        ModelProfile(
            model="deepseek-v4-flash-0731",
            provider="alibaba",
            output_parameter="max_tokens",
            maximum_output=393216,
            defaults={
                "max_tokens": 393216,
                "enable_thinking": True,
                "reasoning_effort": "max",
            },
            policy=ParameterPolicy(
                (
                    _spec("max_tokens", (int,), 1, 393216),
                    _spec("enable_thinking", (bool,)),
                    _spec("reasoning_effort", (str,), choices=("low", "high", "max")),
                )
            ),
            sources=(
                "https://tokendance.space/portal/api/models/deepseek-v4-flash-0731/endpoints/stats",
                "https://help.aliyun.com/zh/model-studio/deepseek-api",
            ),
            output_accounting=(
                "Alibaba documents a shared 393216-token output/reasoning ceiling. "
                "The gateway route and effective backend version require live verification."
            ),
            maximum_images=0,
        ),
        ModelProfile(
            model="deepseek-v4-flash",
            provider="bocha",
            output_parameter="max_tokens",
            maximum_output=393216,
            defaults={
                "max_tokens": 393216,
                "thinking_type": "enabled",
                "reasoning_effort": "max",
            },
            policy=ParameterPolicy(
                (
                    _spec("max_tokens", (int,), 1, 393216),
                    _spec("thinking_type", (str,), choices=("enabled", "disabled")),
                    _spec("reasoning_effort", (str,), choices=("high", "max")),
                )
            ),
            sources=(
                "https://tokendance.space/portal/api/models/deepseek-v4-flash/endpoints/stats",
                "https://www.deepseek.com/news/v4-preview/",
            ),
            output_accounting=(
                "Preview declares high/max, not low. The 393216-token local ceiling "
                "and reasoning controls are not a verified Bocha service guarantee. "
                "The catalog label does not attest the backend checkpoint."
            ),
            maximum_images=0,
        ),
    )
}


class ModelConfig(Config):
    adapter: str = "tokendance"
    protocol: str = PROTOCOL
    model: str
    provider: str | None = None
    # None selects the published profile; {} deliberately omits every parameter.
    parameters: dict[str, object] | None = None
    stream: bool = Field(default=True, strict=True)
    input_mode: InputMode | None = None

    @model_validator(mode="after")
    def _supported(self):
        if self.adapter != "tokendance":
            # Other adapters validate their own models through the trusted adapter object.
            return self
        if self.protocol != PROTOCOL or self.model not in PROFILES:
            raise ValueError("unsupported Token Dance model or protocol")
        p = PROFILES[self.model]
        if self.provider not in (None, p.provider):
            raise ValueError("this model must use its pinned provider")
        object.__setattr__(self, "provider", p.provider)
        if self.input_mode is None:
            object.__setattr__(
                self,
                "input_mode",
                "text_and_images" if p.maximum_images else "text_only",
            )
        if self.input_mode == "text_and_images" and not p.maximum_images:
            raise ValueError("this model supports text input only")
        values = p.defaults if self.parameters is None else self.parameters
        values = p.policy.validate(values, context=self.model)
        if "thinking_budget" in values and "reasoning_effort" in values:
            raise ValueError(
                "thinking_budget and reasoning_effort are mutually exclusive"
            )
        if values.get("reasoning_effort") == "none" and (
            values.get("thinking_type") == "enabled"
            or values.get("enable_thinking") is True
        ):
            raise ValueError("reasoning_effort=none conflicts with enabled thinking")
        if values.get("enable_thinking") is False and (
            "thinking_budget" in values
            or values.get("reasoning_effort") not in (None, "none")
        ):
            raise ValueError("thinking controls conflict with disabled thinking")
        if values.get("thinking_type") == "disabled" and values.get(
            "reasoning_effort"
        ) not in (None, "none"):
            raise ValueError("reasoning effort conflicts with disabled thinking")
        object.__setattr__(self, "parameters", dict(values))
        return self


class RuntimeConfig(Config):
    base_url: str = BASE_URL
    credential_env: str = "TOKENDANCE_API_KEY"
    quota_group: str = "tokendance-account"
    quota_store: Path = Field(
        default_factory=lambda: (
            Path.home() / ".local/state/hvs-catalog/tokendance.sqlite3"
        )
    )
    account_rpm: int = Field(default=50000, ge=1, strict=True)
    rpm: int = Field(default=50000, ge=1, strict=True)
    account_concurrency: int = Field(default=100, ge=1, strict=True)
    concurrency: int = Field(default=100, ge=1, strict=True)
    connect_timeout: float = Field(default=10, gt=0, allow_inf_nan=False, strict=True)
    upload_timeout: float = Field(default=120, gt=0, allow_inf_nan=False, strict=True)
    upload_concurrency: int = Field(default=4, ge=1, strict=True)
    idle_timeout: float = Field(default=180, gt=0, allow_inf_nan=False, strict=True)
    total_timeout: float = Field(default=600, gt=0, allow_inf_nan=False, strict=True)
    max_attempts: int = Field(default=3, ge=1, le=3, strict=True)
    retry_post_send_network_errors: bool = False
    max_response_bytes: int = Field(default=256 * 1024 * 1024, ge=1, strict=True)

    @model_validator(mode="after")
    def _valid(self):
        object.__setattr__(self, "quota_store", self.quota_store.expanduser().resolve())
        u = urlsplit(self.base_url)
        local = u.hostname in ("127.0.0.1", "localhost", "::1") and u.scheme == "http"
        if (
            not (u.scheme == "https" and u.hostname or local)
            or u.username
            or u.password
            or u.query
            or u.fragment
        ):
            raise ValueError(
                "model URL requires HTTPS or explicit loopback, without embedded credentials"
            )
        if self.rpm > self.account_rpm or self.concurrency > self.account_concurrency:
            raise ValueError(
                "requested rate/concurrency exceeds configured account limit"
            )
        if not self.quota_group.strip() or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", self.credential_env
        ):
            raise ValueError("invalid quota group or credential environment reference")
        return self


class ToolCall(Config):
    id: str
    name: str
    arguments: str


class ImageURL(Config):
    url: str
    detail: Literal["high"] = "high"

    @model_validator(mode="after")
    def _inline_image(self):
        match = re.fullmatch(
            r"data:(image/(?:png|jpeg|gif|webp));base64,(.+)", self.url
        )
        if not match or len(self.url) > 45 * 1024 * 1024:
            raise ValueError("Images require a bounded inline base64 data URL")
        try:
            data = base64.b64decode(match[2], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Invalid base64 image") from exc
        if len(data) > 32 * 1024 * 1024 or image_media_type(data) != match[1]:
            raise ValueError("Image size or MIME type is invalid")
        return self


class TextContent(Config):
    type: Literal["text"] = "text"
    text: str = Field(min_length=1)


class ImageContent(Config):
    type: Literal["image_url"] = "image_url"
    image_url: ImageURL


ContentPart = Annotated[TextContent | ImageContent, Field(discriminator="type")]


class Message(Config):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[ContentPart] | None = None
    reasoning_content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def _valid(self):
        if isinstance(self.content, list) and (not self.content or self.role != "user"):
            raise ValueError("Multimodal content requires a nonempty user message")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool messages require tool_call_id")
        if (
            self.tool_calls or self.reasoning_content is not None
        ) and self.role != "assistant":
            raise ValueError("tool calls and reasoning history require assistant role")
        if self.content is None and not self.tool_calls:
            raise ValueError("message requires content or tool calls")
        return self

    def wire(self):
        data = self.model_dump(exclude_none=True, exclude={"tool_calls"})
        if self.tool_calls:
            data["tool_calls"] = [
                {
                    "id": t.id,
                    "type": "function",
                    "function": {"name": t.name, "arguments": t.arguments},
                }
                for t in self.tool_calls
            ]
        return data


class Tool(Config):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
    description: str = "Return the requested structured result."
    parameters: dict

    @model_validator(mode="after")
    def _schema(self):
        try:
            Draft202012Validator.check_schema(self.parameters)
        except SchemaError:
            raise ValueError("invalid tool JSON Schema") from None

        def walk(value):
            if isinstance(value, dict):
                for k, v in value.items():
                    if (
                        k in ("$ref", "$dynamicRef")
                        and isinstance(v, str)
                        and not v.startswith("#")
                    ):
                        raise ValueError("tool schemas only support local references")
                    walk(v)
            elif isinstance(value, list):
                for v in value:
                    walk(v)

        walk(self.parameters)
        return self


class CallRequest(Config):
    model: ModelConfig
    messages: list[Message] = Field(min_length=1)
    tool: Tool | None = None


class CallConfig(Config):
    model: ModelConfig
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)


class CallResult(TypedDict):
    content: str | None
    reasoning_content: str | None
    tool_calls: list[dict]
    parsed: object
    finish_reason: str
    returned_model: str | None
    usage: dict[str, int | None]
    service_request_id: str | None
    trace_id: str | None


def load_config(path: Path) -> CallConfig:
    config = CallConfig.model_validate(
        json.loads(Path(path).read_text(encoding="utf-8"))
    )
    if config.model.adapter == "tokendance":
        from .transport import TokenDanceAdapter

        TokenDanceAdapter.validate_runtime(config.runtime)
    return config


def load_credential(
    runtime: RuntimeConfig,
    *,
    env_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> SecretStr:
    import os

    env = os.environ if environ is None else environ
    values = {}
    if env_file is not None:
        for line in Path(env_file).read_text(encoding="utf-8").splitlines():
            match = re.match(
                r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line
            )
            if match and match[1] == runtime.credential_env:
                try:
                    parts = shlex.split(match[2], comments=True, posix=True)
                except ValueError:
                    raise ValueError(
                        "invalid quoting in credential environment file"
                    ) from None
                if len(parts) != 1:
                    raise ValueError("invalid credential environment file value")
                values[match[1]] = parts[0]
    value = env.get(runtime.credential_env, values.get(runtime.credential_env, ""))
    if not value or "\n" in value or "\r" in value:
        raise ValueError(f"missing or invalid credential: {runtime.credential_env}")
    return SecretStr(value)


def list_models() -> list[dict]:
    return [p.describe() for p in PROFILES.values()]


def parameter_manifest(config: ModelConfig) -> dict:
    p = PROFILES[config.model]
    specified = dict(config.parameters or {})
    return {
        "specified": specified,
        "omitted": [s.name for s in p.policy.specs if s.name not in specified],
        "documented_profile": dict(p.defaults),
        "sources": list(p.sources),
        "unreported_server_effective_values": "unknown",
        "notes": [
            "Parameters ignored or normalized by a provider are not proof of effective values.",
            p.output_accounting,
            *(
                [
                    "MiMo documentation fixes temperature=1 and top_p=0.95 in thinking mode."
                ]
                if p.provider == "xiaomi"
                else []
            ),
        ],
    }
