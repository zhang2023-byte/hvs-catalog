"""Model/step configuration, offline prompt previews and frozen extraction inputs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from ..llm import ModelConfig, RuntimeConfig, TokenDanceAdapter
from ..llm.prompts import (
    PrecedentInput,
    PromptBlueprint,
    PromptGap,
    PromptInput,
    PromptQuestion,
    PromptTarget,
    assemble_blueprint,
)
from ..records.common import ArtifactRef, Config, RecordRef, sha256_of
from .request import ExtractionRequest, StepModel


class ExtractionConfig(Config):
    """Daily and evaluation settings; each step override is a complete model config."""

    method: Literal["direct", "candidate_review"] = "direct"
    mode: Literal["daily", "evaluation", "synthetic"] = "daily"
    expert_adjudication: bool = True
    default_model: ModelConfig = Field(
        default_factory=lambda: ModelConfig(model="deepseek-v4.1-flash")
    )
    step_models: dict[str, ModelConfig] = Field(default_factory=dict)
    examples: dict[str, list[str]] = Field(default_factory=dict)
    use_model_supplements: bool = True
    precedents: list[PrecedentInput] = Field(default_factory=list)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)

    @model_validator(mode="after")
    def _supported(self):
        TokenDanceAdapter().validate_runtime(self.runtime)
        if self.mode == "daily" and not self.expert_adjudication:
            raise ValueError(
                "daily extraction requires expert adjudication; AI-only settings belong to evaluation or synthetic runs"
            )
        from .methods import DEFAULT_REGISTRY

        steps = DEFAULT_REGISTRY.get(self.method).spec.step_ids()
        allowed = set(steps) - {"finalize"}
        for name in {*self.step_models, *self.examples}:
            if name not in allowed:
                raise ValueError(f"unknown or non-model step for {self.method}: {name}")
        for model in [self.default_model, *self.step_models.values()]:
            if model.adapter != "tokendance":
                raise ValueError(
                    "extraction configuration currently supports Token Dance profiles only"
                )
            ModelConfig.model_validate(model.model_dump())
        ids = [p.precedent_id for p in self.precedents]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate precedent identifiers")
        return self

    def model_steps(self) -> tuple[str, ...]:
        from .methods import DEFAULT_REGISTRY

        return tuple(
            s
            for s in DEFAULT_REGISTRY.get(self.method).spec.step_ids()
            if s != "finalize"
        )

    def model_for(self, step_id: str) -> ModelConfig:
        if step_id not in self.model_steps():
            raise ValueError(f"unknown or non-model step: {step_id}")
        return ModelConfig.model_validate(
            self.step_models.get(step_id, self.default_model).model_dump()
        )

    def blueprints(self) -> dict[str, PromptBlueprint]:
        return {
            step: assemble_blueprint(
                step,
                model=self.model_for(step),
                expert_adjudication=self.expert_adjudication,
                example_ids=self.examples.get(step),
                use_model_supplements=self.use_model_supplements,
                precedents=self.precedents,
            )
            for step in self.model_steps()
        }

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
            expert_adjudication=self.expert_adjudication,
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
                "explanations": b.explanation_ids,
                "definitions": b.definition_groups,
                "examples": [e["id"] for e in b.examples],
            }
            for step, b in blueprints.items()
        ],
    }


def preview_extraction_prompt(
    config: ExtractionConfig, step_id: str, inputs: PromptInput
) -> dict:
    config = ExtractionConfig.model_validate(config.model_dump())
    model = config.model_for(step_id)
    blueprint = config.blueprints()[step_id]
    request = blueprint.request(model, inputs)
    # Reuse the provider conversion/checker without transport or credentials.
    prepared = TokenDanceAdapter().prepare(request)
    return {
        "step_id": step_id,
        "request": request.model_dump(mode="json"),
        "prepared": prepared,
        "request_sha256": sha256_of(request.model_dump(mode="json")),
        "examples": blueprint.examples,
        "precedents": [p.model_dump() for p in blueprint.precedents],
    }


def freeze_configuration(space, config: ExtractionConfig) -> dict:
    """Publish content-addressed inputs before execution creation, under its lock."""
    config = ExtractionConfig.model_validate(config.model_dump())
    blueprints = {
        step: b.model_dump(mode="json", by_alias=True)
        for step, b in config.blueprints().items()
    }
    examples = {step: b["examples"] for step, b in blueprints.items()}

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
        "example_refs": [publish("examples", examples)],
    }


def stored_configuration(snapshot) -> ExtractionConfig | None:
    raw = snapshot.options.get("extraction_config")
    return ExtractionConfig.model_validate_json(raw) if raw else None


def validate_configuration_snapshot(snapshot) -> ExtractionConfig | None:
    config = stored_configuration(snapshot)
    if config is None:
        return None
    expected = config.request(snapshot.source_refs)
    if (
        expected.method != snapshot.method
        or expected.expert_adjudication != snapshot.expert_adjudication
        or [m.model_dump() for m in expected.models]
        != [m.model_dump() for m in snapshot.models]
        or len(snapshot.prompt_refs) != 1
        or len(snapshot.example_refs) != 1
    ):
        raise ValueError("frozen extraction configuration does not match its snapshot")
    return config


def configured_request(ctx, step_id: str, request: dict, state: dict) -> dict:
    config = stored_configuration(ctx.execution.input_snapshot)
    if config is None or "call_request" in request:
        return request
    blueprints = json.loads(
        ctx.space.read_file(ctx.execution.input_snapshot.prompt_refs[0])
    )
    blueprint = PromptBlueprint.model_validate(blueprints[step_id])
    source = ctx.store.read_record(ctx.execution.input_snapshot.source_refs[0])
    blocks = []
    for file in source.data.files:
        text = ctx.space.read_file(
            ArtifactRef(path=file.path, sha256=file.sha256)
        ).decode("utf-8")
        blocks.append(
            f"FILE {file.file_id}\n"
            + "\n".join(f"{i}|{line}" for i, line in enumerate(text.splitlines(), 1))
        )
    targets = []
    questions = []
    question = None
    if request.get("question_ref"):
        question = ctx.store.read_record(
            RecordRef.model_validate(request["question_ref"])
        )
        from ..records.submission import InlineSource

        evidence = []
        for ref in question.data.evidence_refs:
            data = ctx.store.read_record(ref).data
            evidence.append(
                InlineSource(
                    file_id=data.file_id, locator=data.locator, excerpt=data.excerpt
                )
            )
        questions = [
            PromptQuestion(
                question_id=question.record_id,
                prompt=question.data.prompt,
                category=question.data.category,
                target_ids=[t.item_id for t in question.data.target_refs if t.item_id],
                gap_ids=[g.gap_id for g in question.data.gap_refs],
                evidence=evidence,
            )
        ]
    if step_id == "review_candidates":
        if question is not None:
            for target in question.data.target_refs:
                record = ctx.store.read_record(
                    RecordRef(record_id=target.record_id, revision=target.revision)
                )
                if record.RECORD_NAME == "roster":
                    targets = [
                        PromptTarget(
                            item_id=e.item_id,
                            identifiers=[i.value for i in e.identifiers],
                        )
                        for e in record.data.entries
                        if e.item_id == target.item_id
                    ]
                    break
        else:
            candidate = next(
                c
                for c in state["candidates"]
                if c["candidate_id"] == request["candidate_id"]
            )
            targets = [
                PromptTarget(
                    item_id=candidate["candidate_id"],
                    identifiers=[candidate["identifier"]],
                )
            ]
    elif step_id == "quantities":
        roster = ctx.store.read_record_current(state["roster_id"])
        targets = [
            PromptTarget(
                object_id=e.item_id, identifiers=[i.value for i in e.identifiers]
            )
            for e in roster.data.entries
            if e.disposition == "included"
        ]
    inputs = PromptInput(
        manuscript="\n\n".join(blocks),
        targets=targets,
        open_questions=questions,
        material_gaps=[
            PromptGap.model_validate(g.model_dump(exclude={"mention_evidence_refs"}))
            for g in source.data.gaps
        ],
    )
    call = blueprint.request(config.model_for(step_id), inputs)
    return {
        **request,
        "call_request": call.model_dump(mode="json"),
        "call_prepared": TokenDanceAdapter().prepare(call),
    }


def start_configured_extraction(
    space, *, configuration: ExtractionConfig, source_refs, request_key: str
):
    from .entry import start_extraction

    configuration = ExtractionConfig.model_validate(configuration.model_dump())
    return start_extraction(
        space,
        request=configuration.request(source_refs),
        request_key=request_key,
        configuration=configuration,
    )
