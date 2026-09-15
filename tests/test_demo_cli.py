"""演示命令行的独立子进程测试：真实进程退出与重启恢复。

子进程在仓库外的临时目录运行，并清除 PYTHONPATH，确保不依赖源码
路径；``--crash-after-response`` 触发的退出码 70 是真实的进程终止。
"""

import os
import subprocess
import sys
from pathlib import Path

from hvs_catalog import cli
from hvs_catalog.demo import graph as demo_graph
from hvs_catalog.demo import runner


def _clean_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return env


def _demo(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "hvs_catalog", "demo", *[str(arg) for arg in args]]
    return subprocess.run(
        command,
        cwd=cwd,
        env=_clean_env(),
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_choices_match_graph_definitions():
    assert cli.DEMO_CHOICES == list(demo_graph.CHOICES)


def test_restart_recovers_pending_and_reuses_results(tmp_path):
    space = tmp_path / "space"

    first = _demo(["run", "--space", space], tmp_path)
    assert first.returncode == 0, first.stderr
    assert "[暂停]" in first.stdout and "[待办]" in first.stdout
    assert "是否识别为同一天体" in first.stdout
    assert "累计调用 2 次" in first.stdout

    # 新进程查看：待办与已保存结果都在，流程进度由检查点保存
    status = _demo(["status", "--space", space], tmp_path)
    assert status.returncode == 0, status.stderr
    assert "等待裁决" in status.stdout
    assert "[固定输入]" in status.stdout
    assert "已保存结果 2 项" in status.stdout
    assert "累计调用 2 次" in status.stdout

    decided = _demo(["decide", "--space", space, "--choice", "merge"], tmp_path)
    assert decided.returncode == 0, decided.stderr
    assert "[复用]" in decided.stdout  # 恢复时输入相同，复用已保存结果
    assert "[完成]" in decided.stdout
    assert "对象分组：2 组" in decided.stdout
    assert "测量 3 条全部保留" in decided.stdout
    assert "累计调用 2 次" in decided.stdout  # 没有重复调用

    final = _demo(["status", "--space", space], tmp_path)
    assert "流程已全部执行完毕" in final.stdout
    assert "对象分组 2 组（J1234+5678+J1234+5679、J0456+7890）" in final.stdout


def test_same_decision_once_and_conflict_rejected(tmp_path):
    space = tmp_path / "space"
    _demo(["run", "--space", space], tmp_path)
    _demo(["decide", "--space", space, "--choice", "merge"], tmp_path)

    again = _demo(["decide", "--space", space, "--choice", "merge"], tmp_path)
    assert again.returncode == 0, again.stderr
    assert "未重复生效" in again.stdout
    assert "累计调用 2 次" in again.stdout

    conflict = _demo(
        ["decide", "--space", space, "--choice", "keep-separate"], tmp_path
    )
    assert conflict.returncode == 2
    assert "[拒绝]" in conflict.stderr and "冲突" in conflict.stderr
    status = _demo(["status", "--space", space], tmp_path)
    assert "→ merge" in status.stdout  # 原决定未被替换


def test_real_exit_after_response_causes_duplicate_billing(tmp_path):
    space = tmp_path / "space"

    crashed = _demo(["run", "--space", space, "--crash-after-response"], tmp_path)
    assert crashed.returncode == runner.CRASH_EXIT_CODE  # 实际终止进程
    assert "尚未保存" in crashed.stdout

    after = _demo(["status", "--space", space], tmp_path)
    assert "待执行节点 extract_roster" in after.stdout
    assert "尚无已保存结果" in after.stdout
    assert "累计调用 1 次" in after.stdout  # 服务端账单已经存在

    resumed = _demo(["run", "--space", space], tmp_path)
    assert resumed.returncode == 0, resumed.stderr
    assert "重复计费" in resumed.stdout
    assert "同一请求此前已计费 1 次" in resumed.stdout
    assert "累计调用 3 次" in resumed.stdout
    assert resumed.stdout.count("累计调用") == 1

    decided = _demo(["decide", "--space", space, "--choice", "keep-separate"], tmp_path)
    assert decided.returncode == 0, decided.stderr
    assert "累计调用 3 次" in decided.stdout  # 恢复未新增调用

    status = _demo(["status", "--space", space], tmp_path)
    assert "对象分组 3 组" in status.stdout


def test_decide_without_pending_question_fails(tmp_path):
    result = _demo(
        ["decide", "--space", tmp_path / "space", "--choice", "merge"], tmp_path
    )
    assert result.returncode == 2
    assert "没有等待裁决的问题" in result.stderr


def test_invalid_choice_rejected_by_cli(tmp_path):
    result = _demo(
        ["decide", "--space", tmp_path / "space", "--choice", "middle"], tmp_path
    )
    assert result.returncode == 2


def test_reset_clears_space_for_new_demo(tmp_path):
    space = tmp_path / "space"
    _demo(["run", "--space", space], tmp_path)
    reset = _demo(["reset", "--space", space], tmp_path)
    assert reset.returncode == 0, reset.stderr
    assert "[重置]" in reset.stdout
    status = _demo(["status", "--space", space], tmp_path)
    assert "尚未开始" in status.stdout
    assert "累计调用 0 次" in status.stdout
