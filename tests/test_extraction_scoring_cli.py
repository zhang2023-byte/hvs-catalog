"""集成 CLI：方法查询、合成共享提取 demo 与独立评分 demo（C08）。

覆盖入口帮助、成功路径、非法输入拒绝、输出保存、新进程重评与输入
指纹变更检测；不把真实 direct/candidate_review 冒充可运行。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hvs_catalog.cli import main
from hvs_catalog.pipeline import list_methods


def _clean_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return env


def _cli(args: list[object], cwd: Path) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "hvs_catalog", *[str(arg) for arg in args]]
    return subprocess.run(
        command, cwd=cwd, env=_clean_env(), capture_output=True, text=True, check=False
    )


def test_methods_list_shows_only_registered_implementation(capsys):
    assert main(["methods"]) == 0
    out = capsys.readouterr().out
    assert "synthetic-mock" in out
    assert "candidate_review" in out and "direct" in out
    # 不冒称真实提取可运行
    assert "仅合成模拟" in out
    assert "真实" in out
    assert main(["methods", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == list_methods()
    assert all(item["real_extraction"] is False for item in payload)


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
            "--method",
            "candidate_review",
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
    # 提取请求与评分输入分开保存：报告里没有 Gold 内容
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
            "--method",
            "candidate_review",
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


def test_score_demo_new_process_rescore_is_stable(tmp_path):
    """新进程重评：同一输入集给出相同摘要与结果。"""
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
    """F6：输入集绑定完整计划摘要；改计划任一字段拒绝重评。"""
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

    # 篡改程序版本
    tampered = json.loads(json.dumps(bundle))
    tampered["plan"]["program_ref"]["version"] = "TAMPERED"
    _rewrite(tampered)
    with pytest.raises(ScoringBundleError, match="plan"):
        load_scoring_bundle(bundle_path)

    # 篡改评价范围（scope）
    tampered = json.loads(json.dumps(bundle))
    tampered["plan"]["scopes"][0]["quantity_paths"] = ["observed_phase_space.distance"]
    _rewrite(tampered)
    with pytest.raises(ScoringBundleError, match="plan"):
        load_scoring_bundle(bundle_path)

    # 未篡改的原内容仍可加载（幂等往返）
    _rewrite(bundle)
    plan, _gold, _predictions, _auxiliary = load_scoring_bundle(bundle_path)
    assert plan.paper_keys == ["FIC-CANDIDATE-C"]


def test_score_demo_new_process_rejects_plan_tamper(tmp_path):
    """F6：新进程重评对计划篡改拒绝，不只核对 Gold/预测/辅助。"""
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


def test_score_demo_rejects_tampered_inputs(tmp_path):
    bundle_path = _run_extract(tmp_path)
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    value = bundle["gold"][0]["objects"][0]["quantities"][0]["values"][0]["value"]
    value["value"] = "999"
    value["direct_evidence"][0]["raw_fragment"] = "999"  # 保持结构自洽
    bundle_path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    result = _cli(["score", "demo", "--inputs", str(bundle_path)], tmp_path)
    assert result.returncode == 2
    assert "摘要不符" in result.stderr


def test_extract_demo_rejects_invalid_inputs(tmp_path, capsys):
    # 未知论文：argparse 拒绝

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
    # 关闭专家组不接受 --decision
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
    # 未登记方法：argparse 限定 choices
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


def test_extract_demo_off_group_daily_keeps_question_open(tmp_path, capsys):
    """日常关闭组：不请求专家、不做纯 AI 解释尝试，问题保持未决。"""
    code = main(
        [
            "extract",
            "demo",
            "--space",
            str(tmp_path / "space-daily-off"),
            "--paper",
            "FIC-R05-A",
            "--method",
            "direct",
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
    assert "专家决定 0 次，模型判断 0 条" in out
    report = json.loads(
        (tmp_path / "out-daily-off" / "extraction-report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["status"]["questions"][0]["status"] == "open"


def test_help_texts_state_real_extraction_not_available():
    extract_help = _cli(["extract", "--help"], Path.cwd())
    assert extract_help.returncode == 0
    assert "真实 direct/candidate_review 提取留待后续关卡" in extract_help.stdout
    score_help = _cli(["score", "--help"], Path.cwd())
    assert score_help.returncode == 0
    assert "正式评分需要获准的私有评分空间" in score_help.stdout
    methods_help = _cli(["methods", "--help"], Path.cwd())
    assert methods_help.returncode == 0
    assert "synthetic-mock" in methods_help.stdout
