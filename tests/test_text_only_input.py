"""Text-only requests retain source material and reject unseen image evidence."""

import copy

import pytest
from pydantic import SecretStr, ValidationError
from test_image_selection import material
from test_pdf_images import CAPTION, TABLE
from test_pdf_images import image_space as image_space_fixture
from test_roster import ScriptedAdapter

from hvs_catalog.cli_llm import synthetic_request
from hvs_catalog.llm import ModelConfig, RuntimeConfig, TokenDanceAdapter
from hvs_catalog.llm.managed import CallService
from hvs_catalog.llm.prompts import assemble_blueprint
from hvs_catalog.pipeline import ExtractionConfig, resume_extraction, start_extraction
from hvs_catalog.pipeline.configuration import preview_extraction_prompt
from hvs_catalog.pipeline.results import saved_quantities
from hvs_catalog.pipeline.roster import material_prompt, saved_roster
from hvs_catalog.workspace import backup_workspace, open_workspace, restore_workspace


@pytest.fixture
def image_space(tmp_path):
    yield from image_space_fixture.__wrapped__(tmp_path)


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_0731_pins_alibaba_and_uses_its_thinking_parameter(effort):
    model = ModelConfig(
        model="deepseek-v4-flash-0731",
        parameters={
            "enable_thinking": True,
            "reasoning_effort": effort,
            "max_tokens": 393216,
        },
    )
    payload = TokenDanceAdapter().prepare(synthetic_request(model))["payload"]
    assert payload["provider"] == {"only": ["alibaba"], "allow_fallbacks": False}
    assert payload["enable_thinking"] is True
    assert payload["reasoning_effort"] == effort
    assert "thinking" not in payload
    assert model.input_mode == "text_only"


def test_preview_rejects_low_without_remapping_and_cannot_use_official_alias():
    with pytest.raises(ValidationError, match="reasoning_effort"):
        ModelConfig(model="deepseek-v4-flash", parameters={"reasoning_effort": "low"})
    with pytest.raises(ValidationError, match="pinned provider"):
        ModelConfig(model="deepseek-v4-flash", provider="deepseek")
    with pytest.raises(ValidationError, match="text input only"):
        ModelConfig(model="deepseek-v4-flash", input_mode="text_and_images")
    model = ModelConfig(model="deepseek-v4-flash")
    payload = TokenDanceAdapter().prepare(synthetic_request(model))["payload"]
    assert payload["provider"] == {"only": ["bocha"], "allow_fallbacks": False}
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "max"


@pytest.mark.parametrize(
    "model", ["deepseek-v4.1-flash", "deepseek-v4-flash-0731", "deepseek-v4-flash"]
)
def test_text_only_preview_keeps_text_and_reports_all_omitted_images(model):
    inputs = material(["image", "table"])
    before = inputs.model_dump()
    config = ExtractionConfig(
        default_model=ModelConfig(model=model, input_mode="text_only"), step_models={}
    )
    for stage, identifiers in [("roster", None), ("quantities", ["A"])]:
        result = preview_extraction_prompt(config, stage, inputs, identifiers)
        assert (
            result["prepared"]["payload"]["messages"][1]["content"] == inputs.manuscript
        )
        assert (
            "contains only text blocks from one paper."
            in result["request"]["messages"][0]["content"]
        )
        assert result["images"]["sent"] == 0
        assert result["images"]["omitted"] == ["LOCATION I:p1-b0", "LOCATION I:p1-b1"]
        assert result["images"]["omission_reason"] == "text_only"
        assert result["material_view"]["input_mode"] == "text_only"
        assert config.blueprints()[stage].schema_ == assemble_blueprint(stage).schema_
        assert inputs.model_dump() == before


def test_raw_images_and_mismatched_prompts_fail_before_transport():
    inputs = material(["image"])
    request = assemble_blueprint("roster").request(
        ModelConfig(model="deepseek-v4.1-flash"), inputs
    )
    request.model = ModelConfig(model="deepseek-v4-flash-0731")
    with pytest.raises(ValueError, match="Text-only input"):
        TokenDanceAdapter().prepare(request)
    with pytest.raises(ValueError, match="input mode differs"):
        assemble_blueprint("roster").request(request.model, inputs)
    inputs.manuscript = ""
    with pytest.raises(ValueError, match="requires readable text"):
        assemble_blueprint("roster", "text_only").request(request.model, inputs)
    with pytest.raises(ValidationError, match="disabled thinking"):
        ModelConfig(
            model="deepseek-v4-flash-0731",
            parameters={"enable_thinking": False, "reasoning_effort": "max"},
        )


def test_input_mode_can_differ_between_stages():
    config = ExtractionConfig(
        default_model=ModelConfig(model="deepseek-v4-flash-0731"),
        step_models={"quantities": ModelConfig(model="deepseek-v4.1-flash")},
    )
    inputs = material(["image"])
    assert preview_extraction_prompt(config, "roster", inputs)["images"]["sent"] == 0
    assert (
        preview_extraction_prompt(config, "quantities", inputs, ["A"])["images"]["sent"]
        == 1
    )


def test_unseen_images_fail_both_stages_and_text_results_survive_restore(
    image_space, tmp_path
):
    space, prep, bundle, source = image_space
    original = material_prompt(space, bundle, source)
    config = ExtractionConfig(
        default_model=ModelConfig(model="deepseek-v4-flash-0731"),
        step_models={
            "quantities": ModelConfig(
                model="deepseek-v4-flash-0731",
                parameters={
                    "enable_thinking": True,
                    "reasoning_effort": "low",
                    "max_tokens": 393216,
                },
            )
        },
        runtime=RuntimeConfig(quota_store=tmp_path / "quota.db"),
    )
    evidence = [{"location": "T:p1-b0", "quote": "A is unbound."}]
    roster = {
        "stars": [
            {
                "identifiers": [{"value": "A", "evidence": evidence}],
                "contribution_type": {
                    "value": "candidates_found",
                    "evidence": evidence,
                },
            }
        ],
        "notes": "",
    }
    unseen_roster = copy.deepcopy(roster)
    unseen_roster["stars"][0]["identifiers"][0]["evidence"] = [{"location": "I:p1-b1"}]
    numerical = {
        "quantities": [
            {
                "quantity": "radial_velocity",
                "values": [
                    {
                        "value": "42",
                        "unit": "km/s",
                        "evidence": [{"location": "T:p1-b2", "quote": TABLE}],
                    }
                ],
            }
        ],
        "notes": "",
    }
    unseen_numerical = copy.deepcopy(numerical)
    unseen_numerical["quantities"][0]["values"][0]["evidence"] = [
        {"location": "I:p1-b1"}
    ]
    adapter = ScriptedAdapter([unseen_roster, roster, unseen_numerical, numerical])
    handle = start_extraction(
        space,
        preparation_execution_id=prep.execution_id,
        configuration=config,
        request_key="text-only",
    )
    service = CallService(
        config.runtime, credential=SecretStr("synthetic"), adapter=adapter
    )
    result = resume_extraction(space, handle.execution_id, service=service)
    assert result.outcome == "finished" and result.result_status == "complete", (
        result.errors
    )
    assert len(adapter.seen) == 4
    for payload, effort in zip(adapter.seen, ["max", "max", "low", "low"], strict=True):
        assert payload["messages"][1]["content"] == original.manuscript
        assert CAPTION in payload["messages"][1]["content"]
        assert TABLE in payload["messages"][1]["content"]
        assert payload["reasoning_effort"] == effort
    retained = saved_roster(space, handle.execution_id)
    assert retained.data.source_round == 1
    assert (
        saved_quantities(space, handle.execution_id, retained)[0].data.source_round == 1
    )
    assert material_prompt(space, bundle, source) == original
    assert original.images
    assert resume_extraction(space, handle.execution_id).outcome == "finished"
    assert len(adapter.seen) == 4
    backup_workspace(space, tmp_path / "backup")
    restore_workspace(tmp_path / "backup", tmp_path / "restored")
    restored = open_workspace(tmp_path / "restored")
    try:
        assert saved_roster(restored, handle.execution_id).data.status == "complete"
    finally:
        restored.close()
