"""Step configuration, scientific prompt contents, language and selection boundaries."""

import copy
import json

import pytest
from pydantic import ValidationError

from hvs_catalog.llm import ModelConfig, prompts
from hvs_catalog.llm.prompts import (
    PrecedentInput,
    PromptGap,
    PromptInput,
    PromptQuestion,
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
    assert config.method == "roster" and not config.expert_adjudication
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
    for method in ("roster",):
        assert (
            len(
                check_extraction_config(
                    ExtractionConfig(mode="evaluation", expert_adjudication=False)
                )["steps"]
            )
            >= 2
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


def test_optional_model_guidance_is_independent_of_science(monkeypatch):
    source = copy.deepcopy(prompts.prompt_resources())
    source["models"]["deepseek-v4.1-flash"] = {
        "text": "Use concise English phrasing in free-text explanations."
    }
    monkeypatch.setattr(prompts, "prompt_resources", lambda: source)
    enabled = ExtractionConfig().blueprints()["roster"]
    disabled = ExtractionConfig(use_model_supplements=False).blueprints()["roster"]
    assert "Use concise English phrasing" in enabled.system
    assert "Use concise English phrasing" not in disabled.system
    for row in prompts.science_content()["rules"]:
        assert row["summary"] in disabled.system
    assert enabled.examples == disabled.examples
    assert enabled.schema_ == disabled.schema_


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


def test_supplied_gap_scope_and_question_evidence_are_preserved():
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
    question = PromptQuestion(
        question_id="question-distance",
        prompt="Does this distance denote the heliocentric or Galactocentric distance?",
        category="interpretation",
        target_ids=["object-1"],
        evidence=[
            {
                "file_id": "paper",
                "locator": {"kind": "text", "start_line": 1, "end_line": 1},
                "excerpt": "原文：distance = 10 kpc",
            }
        ],
    )
    inputs = PromptInput(
        manuscript="FILE paper\n1|原文：distance = 10 kpc",
        targets=[PromptTarget(object_id="object-1", identifiers=["A"])],
        material_gaps=[gap],
        open_questions=[question],
    )
    with pytest.raises(ValueError, match="retired"):
        preview_extraction_prompt(ExtractionConfig(), "quantities", inputs)
    preview = preview_extraction_prompt(
        ExtractionConfig(),
        "quantities",
        inputs.model_copy(update={"open_questions": []}),
    )
    context = preview["request"]["messages"][1]["content"]
    assert "gap-supplement" in context and "question-distance" not in context
    assert '"quantity": "observed_phase_space.distance"' in context
    assert "原文：distance = 10 kpc" in context
    with pytest.raises(ValidationError, match="English"):
        PromptQuestion(question_id="bad", prompt="请判断距离")
    with pytest.raises(ValidationError, match="duplicate supplied material gaps"):
        PromptInput(manuscript="Synthetic paper.", material_gaps=[gap, gap])


def test_original_materials_preserved_and_precedent_maintenance_hidden():
    precedent = PrecedentInput(
        precedent_id="SYN-P1",
        version="synthetic-version-8",
        approved=True,
        explanation="Use the authors' explicit synthesis.",
        conditions="The synthesis applies to the supplied object.",
        boundaries="Do not transfer a sample maximum to every member.",
    )
    source = "FILE synthetic.tex\n1|原文：银河系候选；α = 10°"
    with pytest.raises(ValidationError, match="retired"):
        ExtractionConfig(precedents=[precedent])
    preview = preview_extraction_prompt(
        ExtractionConfig(), "roster", PromptInput(manuscript=source)
    )
    assert source in preview["request"]["messages"][1]["content"]
    assert preview["prepared"]["payload"]["model"] == "deepseek-v4.1-flash"
    for bad in ({"approved": False}, {"explanation": ""}, {"language": "zh"}):
        with pytest.raises(ValidationError):
            PrecedentInput.model_validate({**precedent.model_dump(), **bad})


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
