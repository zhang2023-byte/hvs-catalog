"""Model/step configuration, offline prompt previews and frozen extraction inputs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from ..llm import ModelConfig, RuntimeConfig, TokenDanceAdapter
from ..llm.prompts import (
    PromptBlueprint,
    PromptInput,
    assemble_blueprint,
)
from ..records.common import Config, sha256_of
from .request import ExtractionRequest, StepModel


class ExtractionConfig(Config):
    """Daily and evaluation settings; each step override is a complete model config."""

    @property
    def method(self) -> str:
        return "extraction"

    mode: Literal["daily", "evaluation", "synthetic"] = "daily"
    parallelism: int = Field(default=100, ge=1, strict=True)
    max_format_corrections: int = Field(default=2, ge=0, le=2, strict=True)
    default_model: ModelConfig = Field(
        default_factory=lambda: ModelConfig(
            model="deepseek-v4.1-flash",
            parameters={
                "max_tokens": 393216,
                "thinking_type": "enabled",
                "reasoning_effort": "max",
            },
        )
    )
    step_models: dict[str, ModelConfig] = Field(default_factory=dict)
    runtime: RuntimeConfig = Field(
        default_factory=lambda: RuntimeConfig(retry_post_send_network_errors=True)
    )

    @model_validator(mode="after")
    def _supported(self):
        TokenDanceAdapter().validate_runtime(self.runtime)
        allowed = set(self.model_steps())
        for name in set(self.step_models):
            if name not in allowed:
                raise ValueError(f"unknown or non-model step for {self.method}: {name}")
        for model in [self.default_model, *self.step_models.values()]:
            if model.adapter != "tokendance":
                raise ValueError(
                    "extraction configuration currently supports Token Dance profiles only"
                )
            ModelConfig.model_validate(model.model_dump())
        return self

    def model_steps(self) -> tuple[str, ...]:
        return ("roster", "quantities")

    def model_for(self, step_id: str) -> ModelConfig:
        if step_id not in self.model_steps():
            raise ValueError(f"unknown or non-model step: {step_id}")
        return ModelConfig.model_validate(
            self.step_models.get(step_id, self.default_model).model_dump()
        )

    def blueprints(self) -> dict[str, PromptBlueprint]:
        return {step: assemble_blueprint(step) for step in self.model_steps()}

    def request(self, source_refs) -> ExtractionRequest:
        def spec(model, step=None):
            return StepModel(
                step_id=step,
                provider=model.provider,
                model=model.model,
                parameters=model.parameters or {},
            )

        return ExtractionRequest(
            mode=self.mode,
            method=self.method,
            source_refs=source_refs,
            models=[
                spec(self.default_model),
                *[
                    spec(self.step_models[step], step)
                    for step in sorted(self.step_models)
                ],
            ],
        )


def load_extraction_config(path: Path | None = None) -> ExtractionConfig:
    return (
        ExtractionConfig()
        if path is None
        else ExtractionConfig.model_validate_json(path.read_bytes())
    )


def check_extraction_config(config: ExtractionConfig) -> dict:
    config = ExtractionConfig.model_validate(config.model_dump())
    blueprints = config.blueprints()
    return {
        "configuration": config.model_dump(mode="json"),
        "steps": [
            {
                "step_id": step,
                "model": config.model_for(step).model_dump(mode="json"),
            }
            for step, b in blueprints.items()
        ],
    }


def preview_extraction_prompt(
    config: ExtractionConfig, step_id: str, inputs: PromptInput, identifiers=None
) -> dict:
    config = ExtractionConfig.model_validate(config.model_dump())
    model = config.model_for(step_id)
    blueprint = config.blueprints()[step_id]
    request = blueprint.request(model, inputs, identifiers)
    # Reuse the provider conversion/checker without transport or credentials.
    prepared = TokenDanceAdapter().prepare(request)
    return {
        "step_id": step_id,
        "request": request.model_dump(mode="json"),
        "prepared": prepared,
        "request_sha256": sha256_of(request.model_dump(mode="json")),
    }


def freeze_configuration(space, config: ExtractionConfig) -> dict:
    """Publish content-addressed inputs before execution creation, under its lock."""
    config = ExtractionConfig.model_validate(config.model_dump())
    blueprints = {
        step: b.model_dump(mode="json", by_alias=True)
        for step, b in config.blueprints().items()
    }

    def publish(name, payload):
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        return space.publish_file(f"runs/inputs/{sha256_of(payload)}/{name}.json", raw)

    return {
        "options": {
            "extraction_config": json.dumps(
                config.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
            )
        },
        "prompt_refs": [publish("prompts", blueprints)],
        "example_refs": [],
    }


def stored_configuration(snapshot) -> ExtractionConfig | None:
    raw = snapshot.options.get("extraction_config")
    return ExtractionConfig.model_validate_json(raw) if raw else None


def validate_configuration_snapshot(snapshot) -> ExtractionConfig | None:
    config = stored_configuration(snapshot)
    if config is None:
        return None
    from ..records.rules import current_rules_ref

    if (
        snapshot.expert_adjudication
        or snapshot.rules.model_dump() != current_rules_ref()
    ):
        raise ValueError(
            "historical extraction is read-only; create a new automatic execution with current rules"
        )
    expected = config.request(snapshot.source_refs)
    if (
        expected.method != snapshot.method
        or [m.model_dump() for m in expected.models]
        != [m.model_dump() for m in snapshot.models]
        or len(snapshot.prompt_refs) != 1
        or snapshot.example_refs
    ):
        raise ValueError("frozen extraction configuration does not match its snapshot")
    return config
