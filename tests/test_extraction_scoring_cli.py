"""Integrated CLI tests: method queries, shared synthetic extraction demo, and independent
scoring demo (C08).

Cover help, success, invalid-input rejection, saved outputs, new-process rescoring, and
fingerprint changes. These synthetic cases do not demonstrate real roster extraction quality.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hvs_catalog.cli import main


def _clean_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return env


def _cli(args: list[object], cwd: Path) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "hvs_catalog", *[str(arg) for arg in args]]
    return subprocess.run(
        command, cwd=cwd, env=_clean_env(), capture_output=True, text=True, check=False
    )


def test_retired_method_selection_is_not_exposed():
    with pytest.raises(SystemExit):
        main(["methods"])


def test_extract_demo_runs_pauses_decides_and_saves_outputs(tmp_path, capsys):
    space = tmp_path / "space"
    output = tmp_path / "out"
    code = main(
        [
            "extract",
            "demo",
            "--space",
            str(space),
            "--paper",
            "FIC-CANDIDATE-C",
            "--mode",
            "evaluation",
            "--adjudication",
            "on",
            "--output",
            str(output),
        ]
    )
    assert code == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "[决定] 模拟人类决定：include" in out
    assert "[完成]" in out and "tokens" in out
    report = json.loads((output / "extraction-report.json").read_text(encoding="utf-8"))
    assert report["report"]["outcome"] == "finished"
    assert report["status"]["state"] == "finished"
    bundle_path = output / "evaluation-inputs.json"
    assert bundle_path.is_file()
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert bundle["schema"]["name"] == "hvs.evaluation.scoring-bundle"
    # Save extraction requests separately from scoring inputs; reports contain no Gold content.
    assert "gold" not in json.dumps(report)


def test_score_demo_scores_saved_bundle(tmp_path, capsys):
    bundle_path = _run_extract(tmp_path)
    code = main(["score", "demo", "--inputs", str(bundle_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "L1 TP=2 FP=0 FN=0" in out
    assert "L2 C=2 P=2 G=2" in out
    assert "[结论] 整批 final" in out
    report_path = tmp_path / "scoring-report.json"
    assert (
        main(
            [
                "score",
                "demo",
                "--inputs",
                str(bundle_path),
                "--output",
                str(report_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["papers"][0]["paper_key"] == "FIC-CANDIDATE-C"


def _run_extract(tmp_path) -> Path:
    code = main(
        [
            "extract",
            "demo",
            "--space",
            str(tmp_path / "space"),
            "--paper",
            "FIC-CANDIDATE-C",
            "--mode",
            "evaluation",
            "--adjudication",
            "on",
            "--output",
            str(tmp_path / "out"),
        ]
    )
    assert code == 0
    return tmp_path / "out" / "evaluation-inputs.json"


@pytest.mark.integration
def test_score_demo_new_process_rescore_is_stable(tmp_path):
    """A fresh process rescoring identical inputs produces identical summaries and results."""
    bundle_path = _run_extract(tmp_path)
    first = _cli(
        ["score", "demo", "--inputs", bundle_path, "--output", tmp_path / "r1.json"],
        tmp_path,
    )
    assert first.returncode == 0, first.stderr
    second = _cli(
        ["score", "demo", "--inputs", bundle_path, "--output", tmp_path / "r2.json"],
        tmp_path,
    )
    assert second.returncode == 0, second.stderr
    one = json.loads((tmp_path / "r1.json").read_text(encoding="utf-8"))
    two = json.loads((tmp_path / "r2.json").read_text(encoding="utf-8"))
    assert one["digests"] == two["digests"]
    assert one["papers"] == two["papers"]


def test_bundle_rejects_full_plan_tamper(tmp_path):
    """Bind the complete plan digest to inputs; reject rescoring after any plan field
    changes.
    """
    from hvs_catalog.evaluation.bundle import (
        ScoringBundleError,
        load_scoring_bundle,
    )

    bundle_path = _run_extract(tmp_path)
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))

    def _rewrite(payload):
        bundle_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    # Tamper with the program version.
    tampered = json.loads(json.dumps(bundle))
    tampered["plan"]["program_ref"]["version"] = "TAMPERED"
    _rewrite(tampered)
    with pytest.raises(ScoringBundleError, match="plan"):
        load_scoring_bundle(bundle_path)

    # Tamper with evaluation scope.
    tampered = json.loads(json.dumps(bundle))
    tampered["plan"]["scopes"][0]["quantity_paths"] = ["observed_phase_space.distance"]
    _rewrite(tampered)
    with pytest.raises(ScoringBundleError, match="plan"):
        load_scoring_bundle(bundle_path)

    # Untampered content still loads through an idempotent round trip.
    _rewrite(bundle)
    plan, _gold, _predictions, _auxiliary = load_scoring_bundle(bundle_path)
    assert plan.paper_keys == ["FIC-CANDIDATE-C"]


@pytest.mark.integration
def test_score_demo_new_process_rejects_plan_tamper(tmp_path):
    """A fresh process rejects plan tampering too, not just Gold/prediction/auxiliary
    changes.
    """
    bundle_path = _run_extract(tmp_path)
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["plan"]["program_ref"]["version"] = "TAMPERED"
    bundle_path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    result = _cli(["score", "demo", "--inputs", str(bundle_path)], tmp_path)
    assert result.returncode == 2
    assert "摘要不符" in result.stderr and "plan" in result.stderr


@pytest.mark.integration
def test_score_demo_rejects_tampered_inputs(tmp_path):
    bundle_path = _run_extract(tmp_path)
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    value = bundle["gold"][0]["objects"][0]["quantities"][0]["values"][0]["value"]
    value["value"] = "999"
    value["direct_evidence"][0]["raw_fragment"] = (
        "999"  # Keep the structure self-consistent.
    )
    bundle_path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    result = _cli(["score", "demo", "--inputs", str(bundle_path)], tmp_path)
    assert result.returncode == 2
    assert "摘要不符" in result.stderr


def test_extract_demo_rejects_invalid_inputs(tmp_path, capsys):
    # argparse rejects unknown papers.

    with pytest.raises(SystemExit) as caught:
        main(
            [
                "extract",
                "demo",
                "--space",
                str(tmp_path / "s"),
                "--paper",
                "NOT-A-PAPER",
                "--output",
                str(tmp_path / "o"),
            ]
        )
    assert caught.value.code == 2
    capsys.readouterr()
    # The expert-disabled group rejects --decision.
    code = main(
        [
            "extract",
            "demo",
            "--space",
            str(tmp_path / "s2"),
            "--paper",
            "FIC-R05-A",
            "--adjudication",
            "off",
            "--decision",
            "no_overall_conclusion",
            "--output",
            str(tmp_path / "o2"),
        ]
    )
    assert code == 2
    assert "不接受 --decision" in capsys.readouterr().err
    # argparse choices exclude unregistered methods.
    with pytest.raises(SystemExit) as caught:
        main(
            [
                "extract",
                "demo",
                "--space",
                str(tmp_path / "s3"),
                "--paper",
                "FIC-R05-A",
                "--method",
                "real_extraction",
                "--output",
                str(tmp_path / "o3"),
            ]
        )
    assert caught.value.code == 2


def test_extract_demo_automatic_daily_records_model_judgment(tmp_path, capsys):
    """Daily automatic runs use the same ambiguity policy as evaluation runs."""
    code = main(
        [
            "extract",
            "demo",
            "--space",
            str(tmp_path / "space-daily-off"),
            "--paper",
            "FIC-R05-A",
            "--mode",
            "daily",
            "--adjudication",
            "off",
            "--output",
            str(tmp_path / "out-daily-off"),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "专家决定 0 次，模型判断 1 条" in out
    report = json.loads(
        (tmp_path / "out-daily-off" / "extraction-report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["status"]["questions"] == []


@pytest.mark.integration
def test_help_texts_state_real_extraction_not_available():
    extract_help = _cli(["extract", "--help"], Path.cwd())
    assert extract_help.returncode == 0
    assert "真实名单提取使用 roster 子命令" in extract_help.stdout
    score_help = _cli(["score", "--help"], Path.cwd())
    assert score_help.returncode == 0
    assert "正式评分需要获准的私有评分空间" in score_help.stdout
    methods_help = _cli(["methods", "--help"], Path.cwd())
    assert methods_help.returncode == 2
