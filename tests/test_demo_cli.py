"""New shared offline CLI, each command in a separate process."""

import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration


def demo(tmp_path, *args, code=0):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-m", "hvs_catalog", "demo", *map(str, args)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == code, result.stdout + result.stderr
    return json.loads(result.stdout) if code == 0 else result


def test_restart_reuse_idempotent_conflict(tmp_path):
    space = tmp_path / "space"
    paused = demo(tmp_path, "run", "--space", space)
    assert paused["report"]["outcome"] == "paused"
    assert paused["external_calls"]["calls"] == 2
    status = demo(tmp_path, "status", "--space", space)
    assert status["input_digest"] == paused["input_digest"]
    assert status["pending"][0]["status"] == "open"
    done = demo(
        tmp_path, "decide", "--space", space, "--choice", "no_overall_conclusion"
    )
    assert done["report"]["outcome"] == "finished"
    assert done["external_calls"]["calls"] == 2
    again = demo(
        tmp_path, "decide", "--space", space, "--choice", "no_overall_conclusion"
    )
    assert not again["first_save"]
    assert again["external_calls"]["calls"] == 2
    demo(tmp_path, "decide", "--space", space, "--choice", "keep_pending", code=2)


def test_response_window_actual_death_and_extra_call(tmp_path):
    space = tmp_path / "crash"
    result = demo(tmp_path, "run", "--space", space, "--crash-after-response", code=70)
    assert "尚未保存" in result.stdout
    assert demo(tmp_path, "status", "--space", space)["external_calls"]["calls"] == 1
    resumed = demo(tmp_path, "run", "--space", space)
    assert resumed["external_calls"]["calls"] == 3
    assert resumed["report"]["outcome"] == "paused"
    assert (
        demo(tmp_path, "decide", "--space", space, "--choice", "no_overall_conclusion")[
            "external_calls"
        ]["calls"]
        == 3
    )


def test_old_choices_and_reset_removed(tmp_path):
    demo(tmp_path, "reset", "--space", tmp_path / "space", code=2)
    demo(tmp_path, "decide", "--space", tmp_path / "space", "--choice", "merge", code=2)
