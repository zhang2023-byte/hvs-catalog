"""Current CLI previews and explicit guards run before any external action."""

import json
import subprocess
import sys

import pytest

from hvs_catalog.llm.prompts import PromptInput, assemble_blueprint
from hvs_catalog.pipeline import ExtractionConfig, preview_extraction_prompt
from hvs_catalog.records.rules import load_rules


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "hvs_catalog", *map(str, args)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_default_config_check_and_complete_preview(tmp_path):
    config = cli("extract", "config")
    assert config.returncode == 0, config.stderr
    defaults = ExtractionConfig.model_validate_json(config.stdout)
    assert defaults.model_for("roster").parameters["reasoning_effort"] == "max"
    assert defaults.model_for("quantities").parameters["reasoning_effort"] == "low"
    # Explicit historical configurations retain their recorded stage inheritance.
    historical = ExtractionConfig(step_models={})
    assert historical.model_for("quantities").parameters["reasoning_effort"] == "max"
    assert len(json.loads(cli("extract", "check").stdout)["steps"]) == 2
    material = tmp_path / "input.json"
    material.write_text(
        json.dumps({"manuscript": "LOCATION T:p1-b0 (text)\nA is a candidate."})
    )
    preview = cli("extract", "preview", "--step", "roster", "--input", material)
    assert preview.returncode == 0, preview.stderr
    payload = json.loads(preview.stdout)
    assert (
        payload["request"]["messages"][1]["content"]
        == json.loads(material.read_text())["manuscript"]
    )
    system = payload["request"]["messages"][0]["content"]
    assert system == assemble_blueprint("roster").system
    assert [r.id for r in load_rules().rules] == ["S01", "S02", "S03", "S04"]
    assert len(load_rules().quantities) == 11
    for rule in load_rules().rules:
        if rule.id != "S03":
            assert rule.text in system
        else:
            assert rule.title not in system
    assert system.count("\n# ") == 3
    assert (
        payload["request"]["tool"] == assemble_blueprint("roster").tool().model_dump()
    )


@pytest.mark.parametrize("command", [("extract", "run"), ("extract", "simulate")])
def test_old_extraction_cannot_create_a_workspace(tmp_path, command):
    result = cli(
        *command,
        "--space",
        tmp_path / "absent",
        "--request-key",
        "blocked",
        "--preparation",
        "missing",
    )
    assert result.returncode == 2
    assert not (tmp_path / "absent").exists()


def test_numeric_preview_requires_target_and_gold_requires_subcommand(tmp_path):
    with pytest.raises(ValueError, match="target star identifiers"):
        preview_extraction_prompt(
            ExtractionConfig(), "quantities", PromptInput(manuscript="A")
        )
    for command in ["gold"]:
        # Parser help remains available. Executable operations stop before file access.
        result = cli(command, "demo", "--space", tmp_path / "absent")
        assert result.returncode == 2
        assert not (tmp_path / "absent").exists()


def test_numeric_preview_has_two_user_messages():
    result = preview_extraction_prompt(
        ExtractionConfig(), "quantities", PromptInput(manuscript="A"), ["A", "Alias-A"]
    )
    messages = result["request"]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "user"]
    assert messages[1]["content"] == "A"
    assert json.loads(messages[2]["content"]) == {"identifiers": ["A", "Alias-A"]}


def test_retired_object_retry_stops_before_opening_workspace(tmp_path):
    result = cli(
        "extract",
        "retry-object",
        "--space",
        tmp_path / "absent",
        "--execution",
        "absent",
        "--request-key",
        "blocked",
        "--object",
        "A",
    )
    assert result.returncode == 2 and "invalid choice" in result.stderr
    assert not (tmp_path / "absent").exists()


@pytest.mark.integration
def test_roster_and_batch_cli_use_current_contract(prepared, tmp_path, monkeypatch):
    from llm_helpers import completion, endpoint
    from test_roster import submission

    space, prep, default = prepared

    def reply(payload, number):
        body = completion(payload)
        body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
            json.dumps(submission())
        )
        return {"body": body}

    monkeypatch.setenv("TOKENDANCE_API_KEY", "synthetic-key")
    with endpoint(reply) as (url, seen, _):
        config = default.model_copy(
            update={"runtime": default.runtime.model_copy(update={"base_url": url})}
        )
        path = tmp_path / "config.json"
        path.write_text(config.model_dump_json())
        for command in ("roster", "batch"):
            result = cli(
                "extract",
                command,
                "--space",
                space.root,
                "--preparation",
                prep,
                "--request-key",
                command,
                "--config",
                path,
                *(["--scope", "roster"] if command == "batch" else []),
            )
            assert result.returncode == 0, result.stderr
            output = json.loads(result.stdout)
            assert output["outcome"] == "finished"
            assert output["implementation"] == "model-extraction"
            if command == "roster":
                status = cli(
                    "extract",
                    "status",
                    "--space",
                    space.root,
                    "--execution",
                    output["execution_id"],
                )
                assert json.loads(status.stdout)["roster_status"] == "complete"
        assert len(seen) == 2
        assert all(p["tools"][0]["function"]["name"] == "submit_roster" for p in seen)
