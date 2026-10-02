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
    assert config.method == "direct" and config.expert_adjudication
    assert config.model_for("direct_roster").parameters == {
        "max_tokens": 393216,
        "thinking_type": "enabled",
        "reasoning_effort": "high",
    }
    quantity = config.model_for("quantities")
    assert quantity.provider == "xiaomi" and quantity.stream
    assert quantity.parameters == {
        "max_completion_tokens": 131072,
        "thinking_type": "enabled",
    }
    assert config.model_steps() == ("direct_roster", "quantities")


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
    with pytest.raises(ValidationError, match="daily extraction"):
        ExtractionConfig(expert_adjudication=False)
    for method in ("direct", "candidate_review"):
        assert (
            len(
                check_extraction_config(
                    ExtractionConfig(
                        method=method, mode="evaluation", expert_adjudication=False
                    )
                )["steps"]
            )
            >= 2
        )


def test_science_briefs_full_explanations_and_selected_definitions():
    science = prompts.science_content()
    blueprints = ExtractionConfig().blueprints()
    roster, quantity = blueprints["direct_roster"], blueprints["quantities"]
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
        in roster.schema_["$defs"]["BoundnessSubmission"]["properties"]["status"][
            "anyOf"
        ][0]["enum"]
    )


def test_fixed_related_examples_shared_across_steps():
    config = ExtractionConfig(
        examples={"direct_roster": ["E13", "E19"], "quantities": ["E13", "E19"]}
    )
    plans = config.blueprints()
    assert plans["direct_roster"].examples == plans["quantities"].examples
    assert [e["id"] for e in plans["quantities"].examples] == ["E13", "E19"]
    for ids in (["unknown"], ["E01"], ["E13", "E13"]):
        with pytest.raises(ValueError, match="example"):
            ExtractionConfig(examples={"quantities": ids}).blueprints()
    empty = ExtractionConfig(
        examples={"direct_roster": [], "quantities": []}
    ).blueprints()
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
    enabled = ExtractionConfig().blueprints()["direct_roster"]
    disabled = ExtractionConfig(use_model_supplements=False).blueprints()[
        "direct_roster"
    ]
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
        source["steps"]["direct_roster"]["task"] = value
        with pytest.raises(ValueError, match="English"):
            check_extraction_config(ExtractionConfig())
    del source["steps"]["direct_roster"]["task"]
    with pytest.raises(ValueError, match="missing English content: direct_roster.task"):
        check_extraction_config(ExtractionConfig())


@pytest.mark.parametrize(
    ("method", "step"),
    [
        ("direct", "direct_roster"),
        ("direct", "quantities"),
        ("candidate_review", "discover_candidates"),
        ("candidate_review", "review_candidates"),
    ],
)
def test_ai_changes_adjudication_only_and_has_no_experiment_instructions(method, step):
    daily = ExtractionConfig(method=method).blueprints()[step]
    ai = ExtractionConfig(
        method=method, mode="evaluation", expert_adjudication=False
    ).blueprints()[step]
    assert daily.schema_ == ai.schema_
    assert daily.explanation_ids == ai.explanation_ids
    assert daily.definition_groups == ai.definition_groups
    assert "refer the question to an expert" in daily.system
    assert "ask an expert whether" not in ai.system
    assert "refer the question to an expert" not in ai.system
    assert "refer it to an expert" not in ai.system
    assert "Do not request or wait for an expert" in ai.system
    for text in (
        "Do not derive additional values or choose a best value",
        "Missing or unreadable material leaves the affected part incomplete",
    ):
        assert text in daily.system and text in ai.system
    assert "budget" not in ai.system and "benchmark" not in ai.system
    assert "gold" not in ai.system.lower()
    assert "version checks" not in daily.system and "version checks" not in ai.system
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
    preview = preview_extraction_prompt(ExtractionConfig(), "quantities", inputs)
    context = preview["request"]["messages"][1]["content"]
    assert "gap-supplement" in context and "question-distance" in context
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
    preview = preview_extraction_prompt(
        ExtractionConfig(precedents=[precedent]),
        "direct_roster",
        PromptInput(manuscript=source),
    )
    assert source in preview["request"]["messages"][1]["content"]
    assert precedent.explanation in preview["request"]["messages"][1]["content"]
    assert precedent.version not in json.dumps(preview["request"], ensure_ascii=False)
    assert preview["precedents"][0]["version"] == precedent.version
    assert preview["prepared"]["payload"]["model"] == "deepseek-v4.1-flash"
    for bad in ({"approved": False}, {"explanation": ""}, {"language": "zh"}):
        with pytest.raises(ValidationError):
            PrecedentInput.model_validate({**precedent.model_dump(), **bad})
    with pytest.raises(ValidationError, match="approved"):
        PrecedentInput.model_validate(precedent.model_dump(exclude={"approved"}))


def test_current_step_inputs_and_discovery_contract():
    config = ExtractionConfig(method="candidate_review")
    source = PromptInput(manuscript="FILE paper.tex\n1|A and B are discussed.")
    discovery = preview_extraction_prompt(config, "discover_candidates", source)
    props = discovery["request"]["tool"]["parameters"]["properties"]
    assert set(props) == {"coverage", "candidates", "unresolved_groups", "questions"}
    assert "contribution" not in props and "disposition" not in props
    with pytest.raises(ValueError, match="exactly one"):
        preview_extraction_prompt(config, "review_candidates", source)
    with pytest.raises(ValueError, match="roster objects"):
        preview_extraction_prompt(config, "quantities", source)
    source.targets = [PromptTarget(item_id="candidate-1", identifiers=["A"])]
    review = preview_extraction_prompt(config, "review_candidates", source)
    assert "candidate-1" in review["request"]["messages"][1]["content"]
    assert set(review["request"]["tool"]["parameters"]["properties"]) == {"judgments"}
