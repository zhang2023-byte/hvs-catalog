"""命令行入口，当前提供帮助、版本查询、离线持久化恢复演示与 Gold 工具。

命令行只列出已经实现的操作；其余命令随对应功能交付时补充。
"""

from __future__ import annotations

import argparse
import json
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

    gold = commands.add_parser(
        "gold",
        help="Gold 迁移、草稿检查与受控保存工具（合成材料验证）",
        description=(
            "Gold 标注工具：读取旧 contribution Gold v4 生成迁移草稿与逐项报告，"
            "检查迁移或全新草稿，准备并按批准保存正式答案，查询与恢复保存操作。"
            "旧输入只读；草稿工作目录与正式目标分别指定；保存不执行 Git 操作。"
            "正式答案和含答案报告不进入提取输入、主仓测试或发行包。"
        ),
    )
    gold_commands = gold.add_subparsers(dest="gold_command", required=True)

    migrate_command = gold_commands.add_parser(
        "migrate", help="读取旧 v4 文档，生成迁移草稿与逐项报告（旧输入只读）"
    )
    migrate_command.add_argument(
        "--legacy", required=True, type=Path, help="旧文档路径"
    )
    migrate_command.add_argument(
        "--draft-dir",
        required=True,
        type=Path,
        help="草稿工作目录（输出 draft.json 与报告）",
    )
    migrate_command.add_argument(
        "--document-id", default=None, help="可选：目标文档标识，缺省按论文编号生成"
    )
    migrate_command.add_argument(
        "--known-pdf",
        action="append",
        default=None,
        type=Path,
        help="可选：已知论文 PDF（可重复；第一个作为旧证据绑定的论文 PDF）",
    )
    migrate_command.add_argument(
        "--material-root",
        default=None,
        type=Path,
        help="可选：材料根目录，提供时同步检查",
    )

    check_command = gold_commands.add_parser(
        "check", help="检查迁移或全新草稿：结构、材料定位与科学待审分别报告"
    )
    check_command.add_argument("--draft", required=True, type=Path, help="草稿文档路径")
    check_command.add_argument(
        "--material-root",
        required=True,
        type=Path,
        help="材料根目录（固定 PDF 与 LaTeX 文件/资料包所在目录）",
    )
    check_command.add_argument(
        "--report-json", default=None, type=Path, help="可选：机器可读报告输出路径"
    )

    compare_command = gold_commands.add_parser(
        "compare", help="比较固定的旧输入与草稿/新答案，逐项报告差异（只读）"
    )
    compare_command.add_argument(
        "--legacy", required=True, type=Path, help="旧文档路径"
    )
    compare_command.add_argument(
        "--current", required=True, type=Path, help="草稿或新答案路径"
    )
    compare_command.add_argument(
        "--report-json", default=None, type=Path, help="可选：机器可读报告输出路径"
    )

    prepare_command = gold_commands.add_parser(
        "prepare", help="完成确定性整理并固定待批准的完整内容清单（不更新正式答案）"
    )
    prepare_command.add_argument(
        "--draft", required=True, type=Path, help="草稿文档路径"
    )
    prepare_command.add_argument(
        "--material-root", required=True, type=Path, help="材料根目录"
    )
    prepare_command.add_argument(
        "--target",
        required=True,
        type=Path,
        help="受控目标目录，通常为 hvs-catalog-gold/data；核对入口并按需初始化目录",
    )
    prepare_command.add_argument(
        "--work-dir", required=True, type=Path, help="草稿工作目录（准备材料写到这里）"
    )
    prepare_command.add_argument(
        "--expected-version",
        type=int,
        default=None,
        help="修订时的预期旧版本号；缺省表示新建",
    )

    save_command = gold_commands.add_parser(
        "save", help="按批准内容保存正式答案（锁内复核；同操作标识重试幂等）"
    )
    save_command.add_argument("--draft", required=True, type=Path, help="草稿文档路径")
    save_command.add_argument(
        "--material-root", required=True, type=Path, help="材料根目录"
    )
    save_command.add_argument(
        "--target",
        required=True,
        type=Path,
        help="受控正式目标目录，通常为 hvs-catalog-gold/data；勿传仓库根",
    )
    save_command.add_argument(
        "--preparation",
        required=True,
        type=Path,
        help="准备材料目录（preparations/<id>）",
    )
    save_command.add_argument(
        "--approval", required=True, type=Path, help="批准记录 JSON 文件"
    )
    save_command.add_argument("--operation-id", required=True, help="稳定操作标识")

    query_command = gold_commands.add_parser(
        "query", help="查询保存操作：先核对实际入口与文件，再报告状态"
    )
    query_command.add_argument("--operation-id", required=True, help="操作标识")
    query_command.add_argument(
        "--target",
        required=True,
        type=Path,
        help="受控正式目标目录，通常为 hvs-catalog-gold/data；勿传仓库根",
    )
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


def _dump_json(path: Path | None, payload: dict) -> None:
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _run_gold(args: argparse.Namespace) -> int:
    from hvs_catalog.evaluation import (
        GoldError,
        GoldTarget,
        approved_save,
        check_gold_annotation,
        compare_legacy_with_current,
        migrate_legacy_annotation,
        prepare_save,
        query_operation,
        read_gold_annotation,
    )

    try:
        if args.gold_command == "migrate":
            result = migrate_legacy_annotation(
                legacy_path=args.legacy,
                draft_dir=args.draft_dir,
                document_id=args.document_id,
                known_pdfs=args.known_pdf,
                material_root=args.material_root,
            )
            print(f"[迁移] 草稿已写入 {result.draft_path}")
            print(f"[迁移] 逐项报告已写入 {result.report_path}")
            for category, count in sorted(result.report.summary.items()):
                print(f"[报告] {category}: {count} 项")
            for item in result.report.open_items:
                print(f"[待办] {item.item_id}（{item.category}）{item.description}")
            if result.check is not None:
                blocking = result.check.blocking_count
                print(
                    f"[检查] 结构与材料问题 {blocking} 项，"
                    f"科学待核查 {len(result.check.pending_ids())} 项"
                )
            return 0
        if args.gold_command == "check":
            annotation = read_gold_annotation(args.draft)
            report = check_gold_annotation(annotation, material_root=args.material_root)
            _dump_json(args.report_json, report.model_dump(mode="json"))
            print(f"[检查] 文档 {annotation.document_id}（{annotation.kind}）")
            for category, label in (
                ("structure", "结构与引用"),
                ("material", "材料对应"),
                ("science_pending", "科学待核查"),
            ):
                items = report.problems_of(category)
                print(f"[检查] {label}：{len(items)} 项")
                for item in items:
                    print(f"  - {item.code}｜{item.subject}｜{item.message}")
            for verification in report.quotes:
                print(
                    f"[引文] {verification.subject}：{verification.result}"
                    f"（{verification.note}）"
                )
            for line in report.scope:
                print(f"[范围] {line}")
            if report.blocking_count:
                print(f"[结论] 存在 {report.blocking_count} 项阻断正式保存的问题")
                return 2
            print("[结论] 无阻断问题；科学审定与保存批准另行完成")
            return 0
        if args.gold_command == "compare":
            report = compare_legacy_with_current(
                legacy_path=args.legacy, current_path=args.current
            )
            _dump_json(args.report_json, report.model_dump(mode="json"))
            print(f"[比较] 旧输入 {report.legacy_file}（{report.legacy_sha256[:12]}…）")
            print(
                f"[比较] 当前内容 {report.current_file}（{report.current_sha256[:12]}…）"
            )
            print(
                f"[比较] 一致 {report.matched_count} 项，差异 {report.difference_count} 项"
            )
            for item in report.items:
                if item.status != "identical":
                    print(f"  - [{item.status}] {item.subject}：{item.description}")
            if report.unmatched_old:
                print(f"[比较] 未对应的旧条目：{report.unmatched_old}")
            if report.unmatched_new:
                print(f"[比较] 无旧输入对应的新条目：{report.unmatched_new}")
            return 0
        if args.gold_command == "prepare":
            preparation = prepare_save(
                draft_path=args.draft,
                material_root=args.material_root,
                work_dir=args.work_dir,
                target=GoldTarget(args.target),
                expected_old_version=args.expected_version,
            )
            manifest = preparation.manifest
            print(f"[准备] 清单标识 {manifest.prep_id}")
            print(
                f"[准备] 清单哈希 {preparation.manifest_sha256}（批准记录须绑定此值）"
            )
            print(
                f"[准备] 文档 {manifest.document_id} → v{manifest.new_version}"
                f"（预期旧版本 "
                f"{manifest.expected_old.version if manifest.expected_old else '无'}）"
            )
            print(f"[准备] 准备材料已写入 {preparation.directory}")
            print(f"[准备] 待批准的科学待核查项：{manifest.science_pending or '无'}")
            return 0
        if args.gold_command == "save":
            receipt = approved_save(
                draft_path=args.draft,
                material_root=args.material_root,
                target=GoldTarget(args.target),
                preparation_dir=args.preparation,
                approval=args.approval,
                operation_id=args.operation_id,
            )
            note = "（重试核对原结果）" if receipt.replayed else ""
            print(
                f"[保存] 操作 {receipt.operation_id}{note}：文档 "
                f"{receipt.document_id} v{receipt.version}，清单 "
                f"{receipt.manifest_sha256[:12]}…"
            )
            for item in receipt.files:
                print(f"[保存] {item.path}（{item.sha256[:12]}…）")
            return 0
        if args.gold_command == "query":
            status = query_operation(
                operation_id=args.operation_id, target=GoldTarget(args.target)
            )
            print(f"[查询] 操作 {status.operation_id}：{status.state}")
            print(f"[查询] {status.detail}")
            return 0
        raise ValueError(f"未知 gold 子命令：{args.gold_command}")
    except GoldError as exc:
        print(f"[拒绝] {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)  # --help 与 --version 在解析时直接退出
    if args.command is None:
        # 尚未实现其他顶层操作，先展示帮助
        parser.print_help()
        return 0
    if args.command == "demo":
        return _run_demo(args)
    if args.command == "gold":
        return _run_gold(args)
    return 0
