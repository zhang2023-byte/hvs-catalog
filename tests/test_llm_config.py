"""Configuration contracts and adapter isolation, without network access."""

import pytest
from pydantic import SecretStr, ValidationError

from hvs_catalog.cli_llm import synthetic_request
from hvs_catalog.llm import (
    ModelConfig,
    RuntimeConfig,
    TokenDanceAdapter,
    Tool,
    list_models,
    load_credential,
)
from hvs_catalog.llm.parameters import ParameterPolicyError


def test_profiles_have_distinct_wire_parameters_and_locked_routes():
    configs = {m["model"]: ModelConfig(model=m["model"]) for m in list_models()}
    wires = {
        k: TokenDanceAdapter().prepare(synthetic_request(v))["payload"]
        for k, v in configs.items()
    }
    assert wires["deepseek-v4.1-flash"]["reasoning_effort"] == "high"
    assert wires["glm-5.3-flash"]["reasoning_effort"] == "max"
    assert wires["mimo-v2.6-flash"]["max_completion_tokens"] == 131072
    assert "reasoning_effort" not in wires["mimo-v2.6-flash"]
    assert wires["qwen3.8-flash"]["thinking_budget"] == 131072
    assert "reasoning_effort" not in wires["qwen3.8-flash"]
    for k, v in wires.items():
        assert v["provider"] == {
            "only": [configs[k].provider],
            "allow_fallbacks": False,
        }
        assert v["tool_choice"] == "auto"
        assert v["tools"][0]["function"]["parameters"]["properties"]["value"] == {
            "type": "integer",
            "const": 42,
        }
        assert "temperature" not in v and "top_p" not in v


@pytest.mark.parametrize(
    "parameters",
    [
        {"max_tokens": True},
        {"max_tokens": 0},
        {"max_tokens": 393217},
        {"reasoning_effort": "ultra"},
        {"extra_body": {}},
        {"temperature": float("nan")},
        {"thinking_type": "disabled", "reasoning_effort": "high"},
    ],
)
def test_invalid_parameters_rejected_before_call(parameters):
    with pytest.raises((ValidationError, ParameterPolicyError)):
        ModelConfig(model="deepseek-v4.1-flash", parameters=parameters)


def test_qwen_mutex_and_glm_always_thinking():
    with pytest.raises(ValidationError):
        ModelConfig(
            model="qwen3.8-flash",
            parameters={"thinking_budget": 131072, "reasoning_effort": "xhigh"},
        )
    with pytest.raises((ValidationError, ParameterPolicyError)):
        ModelConfig(model="glm-5.3-flash", parameters={"thinking_type": "disabled"})
    with pytest.raises(ValidationError):
        ModelConfig(model="glm-5.3-flash", provider="baidu")


def test_explicit_omission_is_not_filled_from_profile():
    prepared = TokenDanceAdapter().prepare(
        synthetic_request(ModelConfig(model="deepseek-v4.1-flash", parameters={}))
    )
    assert (
        "max_tokens" not in prepared["payload"]
        and "thinking" not in prepared["payload"]
    )
    assert "max_tokens" in prepared["parameters"]["omitted"]
    assert prepared["parameters"]["unreported_server_effective_values"] == "unknown"


def test_mimo_sampling_ranges_follow_its_own_protocol():
    for parameters in ({"temperature": 1.6}, {"top_p": 0}):
        with pytest.raises((ValidationError, ParameterPolicyError)):
            ModelConfig(model="mimo-v2.6-flash", parameters=parameters)
    config = ModelConfig(
        model="mimo-v2.6-flash",
        parameters={"temperature": 1.5, "top_p": 0.01, "presence_penalty": -2},
    )
    wire = TokenDanceAdapter().prepare(synthetic_request(config))
    assert wire["payload"]["top_p"] == 0.01
    assert "temperature=1" in wire["parameters"]["notes"][-1]


@pytest.mark.parametrize(
    "model,parameters",
    [
        ("glm-5.3-flash", {"temperature": 1.01}),
        ("glm-5.3-flash", {"top_p": 0}),
        ("qwen3.8-flash", {"temperature": 2}),
        ("qwen3.8-flash", {"top_p": 0}),
    ],
)
def test_model_specific_closed_and_open_bounds(model, parameters):
    with pytest.raises((ValidationError, ParameterPolicyError)):
        ModelConfig(model=model, parameters=parameters)


def test_env_file_not_executed_and_environment_wins(tmp_path):
    env = tmp_path / "local.env"
    marker = tmp_path / "executed"
    env.write_text(f"UNRELATED=$(touch {marker})\nTOKENDANCE_API_KEY='local-value'\n")
    assert (
        load_credential(RuntimeConfig(), env_file=env, environ={}).get_secret_value()
        == "local-value"
    )
    assert (
        load_credential(
            RuntimeConfig(),
            env_file=env,
            environ={"TOKENDANCE_API_KEY": "environment-value"},
        ).get_secret_value()
        == "environment-value"
    )
    assert not marker.exists() and "environment-value" not in repr(
        SecretStr("environment-value")
    )


def test_runtime_url_and_account_constraints():
    for v in (
        {"base_url": "http://localhost:9/?key=secret"},
        {"concurrency": 101},
        {"rpm": 50001},
        {"connect_timeout": True},
    ):
        with pytest.raises(ValidationError):
            RuntimeConfig(**v)
    with pytest.raises(ValueError):
        TokenDanceAdapter.validate_runtime(
            RuntimeConfig(base_url="https://another.example/gateway/v1")
        )


def test_quota_store_is_frozen_across_working_directories(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.chdir(tmp_path)
    config = RuntimeConfig(quota_store=Path("quota.db"))
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    loaded = RuntimeConfig.model_validate_json(config.model_dump_json())
    assert loaded.quota_store == tmp_path / "quota.db"


def test_tool_schema_cannot_fetch_external_references():
    with pytest.raises(ValidationError):
        Tool(name="extract", parameters={"type": "not-a-json-type"})
    with pytest.raises(ValidationError):
        Tool(name="extract", parameters={"$ref": "https://example.org/schema"})
    assert Tool(
        name="extract",
        parameters={"$defs": {"value": {"type": "integer"}}, "$ref": "#/$defs/value"},
    )
