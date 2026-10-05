"""Offline CLI configuration/check/preview and persisted synthetic simulation."""

import json

from hvs_catalog.cli import main


def test_default_config_check_and_complete_preview(tmp_path, capsys):
    assert main(["extract", "config"]) == 0
    default = json.loads(capsys.readouterr().out)
    assert "method" not in default
    assert default["default_model"]["model"] == "deepseek-v4.1-flash"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"step_models": {"quantities": {"model": "qwen3.8-flash"}}})
    )
    assert main(["extract", "check", "--config", str(config)]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["steps"][0]["model"]["model"] == "deepseek-v4.1-flash"
    assert checked["steps"][1]["model"]["model"] == "qwen3.8-flash"
    inputs = tmp_path / "input.json"
    inputs.write_text(
        json.dumps({"manuscript": "FILE synthetic.tex\n1|A is an unbound candidate."})
    )
    assert (
        main(
            [
                "extract",
                "preview",
                "--config",
                str(config),
                "--step",
                "roster",
                "--input",
                str(inputs),
            ]
        )
        == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert "A is an unbound candidate" in preview["request"]["messages"][1]["content"]
    assert preview["prepared"]["payload"]["provider"] == {
        "only": ["deepseek"],
        "allow_fallbacks": False,
    }
    assert not list(tmp_path.glob("*.db"))


def test_bad_step_and_untrusted_metadata_are_actionable_errors(tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"step_models": {"finalize": {"model": "deepseek-v4.1-flash"}}})
    )
    assert main(["extract", "check", "--config", str(config)]) == 2
    assert "finalize" in capsys.readouterr().err
    inputs = tmp_path / "input.json"
    inputs.write_text(
        json.dumps({"manuscript": "Synthetic paper.", "gold_path": "/private/answer"})
    )
    assert main(["extract", "preview", "--step", "roster", "--input", str(inputs)]) == 2
    assert "gold_path" in capsys.readouterr().err


def test_simulate_status_resume_use_persisted_configuration(tmp_path, capsys):
    root = tmp_path / "space"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "step_models": {"quantities": {"model": "mimo-v2.6-flash"}},
            }
        )
    )
    assert (
        main(
            [
                "extract",
                "simulate",
                "--config",
                str(config),
                "--space",
                str(root),
                "--request-key",
                "synthetic-combination",
            ]
        )
        == 0
    )
    run = json.loads(capsys.readouterr().out)
    assert run["outcome"] == "finished" and run["implementation"] == "synthetic-mock"
    config.write_text("{}")
    for command in ("status", "resume"):
        assert (
            main(
                [
                    "extract",
                    command,
                    "--space",
                    str(root),
                    "--execution",
                    run["execution_id"],
                ]
            )
            == 0
        )
        result = json.loads(capsys.readouterr().out)
        assert result["config"]["method"] == "roster"
        assert (
            result["config"]["configuration"]["step_models"]["quantities"]["model"]
            == "mimo-v2.6-flash"
        )
