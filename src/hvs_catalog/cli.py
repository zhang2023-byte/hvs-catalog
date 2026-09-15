"""命令行入口，当前提供帮助、版本查询和离线持久化恢复演示。

命令行只列出已经实现的操作；其余命令随对应功能交付时补充。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path

# 与 hvs_catalog.demo.graph.CHOICES 保持一致；测试会核对两者不漂移
DEMO_CHOICES = ["merge", "keep-separate"]


def get_version() -> str:
    """返回已安装包的软件版本，版本只定义在 pyproject.toml 中。"""
    return version("hvs-catalog")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hvs-catalog",
        description="高速星文献提取与星表系统（当前提供帮助、版本查询与持久化恢复演示）。",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"hvs-catalog {get_version()}",
    )
    commands = parser.add_subparsers(dest="command")
    demo = commands.add_parser(
        "demo",
        help="LangGraph 持久化与恢复演示（离线、合成材料）",
        description=(
            "用虚构论文演示流程暂停、进程退出后的恢复、结果复用与裁决决定规则"
            "（裁决只决定对象是否为同一天体，不挑选测量值；恢复沿用运行固定输入）。"
            "演示空间是自选的独立目录，内含业务库（应用保存结果与决定）、"
            "LangGraph 检查点（框架保存流程进度与固定输入）与模拟服务账单（扮演远程计费）。"
        ),
    )
    demo_commands = demo.add_subparsers(dest="demo_command", required=True)

    run_command = demo_commands.add_parser(
        "run",
        help="运行演示流程：新流程执行到裁决处暂停；上次未跑完则从检查点续跑",
    )
    run_command.add_argument(
        "--space",
        required=True,
        type=Path,
        help="演示空间目录（独立于仓库，存放业务库、检查点与服务账单）",
    )
    run_command.add_argument(
        "--crash-after-response",
        action="store_true",
        help="演示用：下一次收到服务响应后、保存结果前立即终止进程（退出码 70）",
    )

    status_command = demo_commands.add_parser(
        "status",
        help="查看流程进度、固定输入、业务数据、待办、汇总与服务账单",
    )
    status_command.add_argument(
        "--space", required=True, type=Path, help="演示空间目录"
    )

    decide_command = demo_commands.add_parser(
        "decide",
        help="保存裁决决定并恢复流程（同一决定只生效一次）",
    )
    decide_command.add_argument(
        "--space", required=True, type=Path, help="演示空间目录"
    )
    decide_command.add_argument(
        "--choice",
        required=True,
        choices=DEMO_CHOICES,
        help="对象身份：merge=识别为同一天体，keep-separate=保持两个候选",
    )
    decide_command.add_argument("--reason", default=None, help="可选：决定理由")

    reset_command = demo_commands.add_parser(
        "reset",
        help="清空演示空间，便于重新演示",
    )
    reset_command.add_argument("--space", required=True, type=Path, help="演示空间目录")
    return parser


def _run_demo(args: argparse.Namespace) -> int:
    from hvs_catalog.demo import runner
    from hvs_catalog.demo.store import DecisionConflictError

    if args.demo_command == "reset":
        runner.reset(args.space)
        print(f"[重置] 已清空演示空间 {args.space}")
        return 0
    try:
        if args.demo_command == "run":
            hook = (
                runner.crash_after_response_hook if args.crash_after_response else None
            )
            report = runner.run(args.space, after_response=hook)
        elif args.demo_command == "status":
            report = runner.status(args.space)
        else:
            report = runner.decide(args.space, args.choice, args.reason)
    except (DecisionConflictError, runner.NoPendingQuestionError) as exc:
        print(f"[拒绝] {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    for line in report.lines():
        print(line)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)  # --help 与 --version 在解析时直接退出
    if args.command is None:
        # 尚未实现其他顶层操作，先展示帮助
        parser.print_help()
        return 0
    if args.command == "demo":
        return _run_demo(args)
    return 0
