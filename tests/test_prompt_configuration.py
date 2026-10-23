"""Step configuration, scientific prompt contents, language and selection boundaries."""

import copy
import json

import pytest
from pydantic import ValidationError

from hvs_catalog.llm import ModelConfig, prompts
from hvs_catalog.llm.prompts import (
    PromptGap,
    PromptInput,
    PromptTarget,
)
from hvs_catalog.pipeline import (
    ExtractionConfig,
    check_extraction_config,
    preview_extraction_prompt,
)


def test_defaults_and_complete_step_override():
    config = ExtractionConfig(
        step_models={"quantities": ModelConfig(model="mimo-v2.6-flash")}
    )
    assert (
        config.method == "roster" and "expert_adjudication" not in config.model_dump()
    )
    assert config.model_for("roster").parameters == {
        "max_tokens": 393216,
        "thinking_type": "enabled",
        "reasoning_effort": "max",
    }
    quantity = config.model_for("quantities")
    assert quantity.provider == "xiaomi" and quantity.stream
    assert quantity.parameters == {
        "max_completion_tokens": 131072,
        "thinking_type": "enabled",
    }
    assert config.model_steps() == ("roster", "quantities")


@pytest.mark.parametrize("step", ["finalize", "review_candidates", "typo"])
def test_unknown_inactive_and_program_steps_refused(step):
    with pytest.raises(ValidationError, match="non-model step"):
        ExtractionConfig(step_models={step: ModelConfig(model="glm-5.3-flash")})
    with pytest.raises(ValidationError, match="non-model step"):
        ExtractionConfig(examples={step: []})


def test_protocol_parameter_and_daily_ai_boundaries():
    with pytest.raises(ValidationError, match="unsupported Token Dance"):
        ExtractionConfig.model_validate(
            {
                "default_model": {
                    "model": "deepseek-v4.1-flash",
                    "protocol": "anthropic:messages",
                }
            }
        )
    with pytest.raises(ValidationError):
        ExtractionConfig.model_validate(
            {
                "step_models": {
                    "quantities": {
                        "model": "mimo-v2.6-flash",
                        "parameters": {"reasoning_effort": "high"},
                    }
                }
            }
        )
    with pytest.raises(ValidationError, match="retired"):
        ExtractionConfig(expert_adjudication=True)
    assert (
        len(check_extraction_config(ExtractionConfig(mode="evaluation"))["steps"]) == 2
    )


def test_science_briefs_full_explanations_and_selected_definitions():
    science = prompts.science_content()
    blueprints = ExtractionConfig().blueprints()
    roster, quantity = blueprints["roster"], blueprints["quantities"]
    for row in science["rules"]:
        assert row["summary"] in roster.system and row["summary"] in quantity.system
    for row in science["rules"]:
        if row["id"] in roster.explanation_ids:
            assert row["explanation"] in roster.system
        if row["id"] in quantity.explanation_ids:
            assert row["explanation"] in quantity.system
    assert len(science["quantities"]) == 18
    for row in science["quantities"]:
        assert row["definition"] in quantity.system
    for row in science["states"]:
        assert row["definition"] in roster.system
    for value in (
        science["version"],
        science["review_note"],
        "legacy_rules",
        "legacy_path",
        "review_status",
        "render_record_contracts",
        "resume_execution",
    ):
        assert value not in roster.system and value not in quantity.system
    assert "Return only the scientific fields requested for this step" in roster.system
    assert (
        "no_overall_conclusion"
        in roster.schema_["$defs"]["RosterEntrySubmission"]["properties"]["boundness"][
            "anyOf"
        ][0]["enum"]
    )


def test_fixed_related_examples_shared_across_steps():
    config = ExtractionConfig(
        examples={"roster": ["E13", "E19"], "quantities": ["E13", "E19"]}
    )
    plans = config.blueprints()
    assert plans["roster"].examples == plans["quantities"].examples
    assert [e["id"] for e in plans["quantities"].examples] == ["E13", "E19"]
    for ids in (["unknown"], ["E01"], ["E13", "E13"]):
        with pytest.raises(ValueError, match="example"):
            ExtractionConfig(examples={"quantities": ids}).blueprints()
    empty = ExtractionConfig(examples={"roster": [], "quantities": []}).blueprints()
    for step, blueprint in empty.items():
        assert not blueprint.examples
        assert (
            "Do not derive additional values or choose a best value" in blueprint.system
        )
        assert blueprint.schema_ == plans[step].schema_


def test_models_share_task_instructions_and_keep_protocol_parameters():
    default = ExtractionConfig()
    alternate = ExtractionConfig(
        step_models={"quantities": ModelConfig(model="mimo-v2.6-flash")}
    )
    assert alternate.blueprints() == default.blueprints()
    assert alternate.model_for("quantities") != default.model_for("quantities")


@pytest.mark.parametrize("enabled", [True, False])
def test_retired_model_guidance_is_read_only(enabled):
    raw = {
        **ExtractionConfig().model_dump(mode="json"),
        "use_model_supplements": enabled,
    }
    with pytest.raises(ValidationError, match="Extra inputs"):
        ExtractionConfig.model_validate(raw)
    from types import SimpleNamespace

    from hvs_catalog.pipeline.configuration import stored_configuration

    historical = stored_configuration(
        SimpleNamespace(options={"extraction_config": json.dumps(raw)})
    )
    assert historical.use_model_supplements is enabled
    assert historical.blueprints() == ExtractionConfig().blueprints()


def test_missing_and_non_english_instruction_sources_fail(monkeypatch):
    source = copy.deepcopy(prompts.prompt_resources())
    monkeypatch.setattr(prompts, "prompt_resources", lambda: source)
    for value in ("", "请提取所有对象"):
        source["steps"]["roster"]["task"] = value
        with pytest.raises(ValueError, match="English"):
            check_extraction_config(ExtractionConfig())
    del source["steps"]["roster"]["task"]
    with pytest.raises(ValueError, match="missing English content: roster.task"):
        check_extraction_config(ExtractionConfig())


@pytest.mark.parametrize(
    ("method", "step"),
    [
        ("roster", "roster"),
        ("roster", "quantities"),
    ],
)
def test_daily_and_evaluation_share_automatic_input(method, step):
    daily = ExtractionConfig().blueprints()[step]
    evaluation = ExtractionConfig(mode="evaluation").blueprints()[step]
    assert daily == evaluation
    serialized = json.dumps(daily.schema_).lower()
    assert "question" not in serialized and "precedent" not in serialized
    for text in ("expert", "precedent", "gold", "benchmark", "budget"):
        assert text not in daily.system.lower()
    assert "Source errors require" in daily.system
    assert "Provided or assigned by the application" not in daily.system


@pytest.mark.parametrize(
    ("with_target", "with_gap"),
    [(False, False), (True, False), (False, True), (True, True)],
)
def test_nonempty_context_and_original_material_are_preserved(with_target, with_gap):
    gap = PromptGap(
        gap_id="gap-supplement",
        description="The supplement containing the distance measurement is missing.",
        reason="missing",
        file_id="supplement",
        affected_targets={
            "scope": "known",
            "items": [
                {
                    "item_id": "object-1",
                    "quantity": "observed_phase_space.distance",
                    "judgment": None,
                }
            ],
        },
    )
    inputs = PromptInput(
        manuscript="FILE paper\n1|原文：distance = 10 kpc",
        targets=[PromptTarget(object_id="object-1", identifiers=["A"])]
        if with_target
        else [],
        material_gaps=[gap] if with_gap else [],
    )
    preview = preview_extraction_prompt(
        ExtractionConfig(), "quantities" if with_target else "roster", inputs
    )
    text = preview["request"]["messages"][1]["content"]
    assert inputs.manuscript in text
    marker = "===== SUPPLIED CONTEXT =====\n"
    if with_target or with_gap:
        context = json.loads(text.split(marker, 1)[1])
        assert set(context) == (
            ({"targets"} if with_target else set())
            | ({"material_gaps"} if with_gap else set())
        )
        if with_target:
            assert context["targets"] == [
                {"object_id": "object-1", "identifiers": ["A"]}
            ]
        if with_gap:
            assert context["material_gaps"] == [
                {
                    "gap_id": "gap-supplement",
                    "description": "The supplement containing the distance measurement is missing.",
                    "reason": "missing",
                    "file_id": "supplement",
                    "affected_targets": {
                        "scope": "known",
                        "items": [
                            {
                                "item_id": "object-1",
                                "quantity": "observed_phase_space.distance",
                            }
                        ],
                    },
                }
            ]
    else:
        assert text == "===== PAPER MATERIALS =====\n" + inputs.manuscript
    with pytest.raises(ValidationError, match="duplicate supplied material gaps"):
        PromptInput(manuscript="Synthetic paper.", material_gaps=[gap, gap])


def test_original_materials_preserved_and_retired_inputs_rejected():
    source = "FILE synthetic.tex\n1|原文：银河系候选；α = 10°"
    with pytest.raises(ValidationError, match="retired"):
        ExtractionConfig(precedents=[{"precedent_id": "SYN-P1"}])
    with pytest.raises(ValidationError, match="Extra inputs"):
        PromptInput(manuscript=source, open_questions=[])
    preview = preview_extraction_prompt(
        ExtractionConfig(), "roster", PromptInput(manuscript=source)
    )
    assert source in preview["request"]["messages"][1]["content"]
    assert preview["prepared"]["payload"]["model"] == "deepseek-v4.1-flash"


def test_historical_empty_prompt_fields_preserve_requests():
    from hvs_catalog.llm.prompts import PromptBlueprint

    config = ExtractionConfig()
    inputs = PromptInput(manuscript="Synthetic paper with original source text.")
    blueprint = config.blueprints()["roster"]
    old_inputs = {**inputs.model_dump(), "open_questions": []}
    old_blueprint = {**blueprint.model_dump(by_alias=True), "precedents": []}
    historical = {"historical": True}
    restored_inputs = PromptInput.model_validate(old_inputs, context=historical)
    restored_blueprint = PromptBlueprint.model_validate(
        old_blueprint, context=historical
    )
    assert restored_blueprint.request(config.model_for("roster"), restored_inputs) == (
        blueprint.request(config.model_for("roster"), inputs)
    )
    with pytest.raises(ValidationError, match="retired"):
        PromptInput.model_validate(
            {**old_inputs, "open_questions": [{"question_id": "retired"}]},
            context=historical,
        )
    with pytest.raises(ValidationError, match="retired"):
        PromptBlueprint.model_validate(
            {**old_blueprint, "precedents": [{"precedent_id": "retired"}]},
            context=historical,
        )


def test_single_roster_and_quantity_contract():
    config = ExtractionConfig()
    source = PromptInput(
        manuscript="FILE pdf\nPAGE 1 BLOCK p1-b0\nA and B are discussed."
    )
    preview = preview_extraction_prompt(config, "roster", source)
    assert set(preview["request"]["tool"]["parameters"]["properties"]) == {
        "coverage",
        "entries",
        "unresolved_groups",
        "model_judgments",
    }
    with pytest.raises(ValueError, match="roster objects"):
        preview_extraction_prompt(config, "quantities", source)
    multiple = source.model_copy(
        update={
            "targets": [
                PromptTarget(object_id="object-1", identifiers=["A"]),
                PromptTarget(object_id="object-2", identifiers=["B"]),
            ]
        }
    )
    with pytest.raises(ValueError, match="exactly one supplied object"):
        preview_extraction_prompt(config, "quantities", multiple)
    for retired in ("direct_roster", "discover_candidates", "review_candidates"):
        with pytest.raises(ValueError, match="non-model step"):
            preview_extraction_prompt(config, retired, source)


def test_actual_prompt_has_one_scientific_rule_set():
    assert "ambiguity_policies" not in prompts.science_content()
    assert (
        ExtractionConfig().blueprints()
        == ExtractionConfig(mode="evaluation").blueprints()
    )


def test_schema_gap_ids_are_limited_to_supplied_materials():
    empty = preview_extraction_prompt(
        ExtractionConfig(), "roster", PromptInput(manuscript="PDF blocks")
    )
    defs = empty["request"]["tool"]["parameters"]["$defs"]
    assert defs["RosterCoverageSubmission"]["properties"]["gap_ids"]["maxItems"] == 0
