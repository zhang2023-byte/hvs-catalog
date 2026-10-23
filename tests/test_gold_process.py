"""Actual process interruption, concurrent publication and direct scoring CLI."""

import json
import selectors
import shutil
import subprocess
import sys

import pytest
from gold_save_helpers import flow, revision

from hvs_catalog.evaluation.store import read_saved, save_annotation
from hvs_catalog.workspace import WorkspaceBusyError

pytestmark = pytest.mark.integration


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "hvs_catalog", *map(str, args)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def run_cli(*args):
    result = cli(*args)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("hook", ["before_publish", "after_publish"])
@pytest.mark.parametrize("existing", [False, True])
def test_process_exit_leaves_complete_old_or_new_annotation(tmp_path, hook, existing):
    args = flow(tmp_path)
    old = None
    if existing:
        save_annotation(**args)
        old = args["expected_new"]
        args = revision(args, tmp_path / "revision")
    code = """
import json,os,sys
from hvs_catalog.evaluation.store import save_annotation
args=json.loads(sys.argv[1])
save_annotation(**args,fault_hooks={sys.argv[2]:lambda:os._exit(73)})
"""
    result = subprocess.run(
        [sys.executable, "-c", code, json.dumps(args, default=str), hook],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 73, result.stderr
    observed = read_saved(args["target"])["annotation_sha256"]
    assert observed == (args["expected_new"] if hook == "after_publish" else old)
    if args["target"].exists():
        assert {p.name for p in args["target"].iterdir()} == {
            "annotation.json",
            "paper.pdf",
        }
    assert save_annotation(**args)["annotation_sha256"] == args["expected_new"]


def test_other_process_cannot_read_or_save_while_locked(tmp_path):
    args = flow(tmp_path)
    save_annotation(**args)
    code = """
import sys
from hvs_catalog.evaluation.store import data_lock
with data_lock(sys.argv[1]):
    print("LOCKED",flush=True)
    sys.stdin.readline()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(args["target"].parent)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as poll:
            poll.register(child.stdout, selectors.EVENT_READ)
            assert poll.select(10), "Child did not acquire lock"
        assert child.stdout.readline().strip() == "LOCKED"
        with pytest.raises(WorkspaceBusyError):
            read_saved(args["target"])
        with pytest.raises(WorkspaceBusyError):
            save_annotation(**args)
    finally:
        child.communicate("release\n", timeout=10)
    assert child.returncode == 0
    assert save_annotation(**args)["already_present"]


def test_cli_check_compare_save_direct_score_and_source_free_replay(tmp_path):
    args = flow(tmp_path)
    assert run_cli("gold", "check", "--annotation", args["annotation"])[
        "ready_for_review"
    ]
    assert (
        run_cli(
            "gold",
            "compare",
            "--before",
            args["annotation"],
            "--after",
            args["annotation"],
        )["changes"]
        == []
    )
    inspected = run_cli(
        "gold",
        "inspect-save",
        "--annotation",
        args["annotation"],
        "--target",
        args["target"],
    )
    run_cli(
        "gold",
        "save",
        "--annotation",
        args["annotation"],
        "--target",
        args["target"],
        "--expected-new",
        inspected["annotation_sha256"],
    )
    assert (
        run_cli("gold", "read", "--target", args["target"])["annotation_sha256"]
        == args["expected_new"]
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "papers": [
                    {
                        "arxiv_id": "2601.99999",
                        "reference": str(args["target"] / "annotation.json"),
                    }
                ]
            }
        )
    )
    scored = cli(
        "score", "run", "--manifest", manifest, "--output", tmp_path / "scored"
    )
    assert scored.returncode == 0, scored.stderr
    assert "C=0 P=0 G=2" in scored.stdout
    shutil.rmtree(args["target"])
    shutil.rmtree(args["annotation"].parent)
    replay = cli("score", "replay", "--inputs", tmp_path / "scored/scoring-inputs.json")
    assert replay.returncode == 0 and replay.stdout == scored.stdout
    assert cli("gold", "export").returncode == 2
    assert cli("gold", "migrate").returncode == 2
