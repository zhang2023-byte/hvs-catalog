"""命令行入口，当前提供帮助、版本查询、离线持久化恢复演示、Gold
工具，以及合成提取与独立评分演示。

命令行只列出已经实现的操作；真实 direct/candidate_review 提取
（R14–R16）与正式评分尚未提供，演示入口不冒充这些能力。其余命令随
对应功能交付时补充。
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
        description=(
            "高速星文献提取与星表系统（当前提供帮助、版本查询、持久化恢复"
            "演示、Gold 工具，以及合成提取与独立评分演示）。"
        ),
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
        "--mapping",
        default=None,
        type=Path,
        help="比较 compact v2 answer.json 时必填：外置旧条目对应 JSON；不得按 cand 顺序推断旧索引",
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
    command = gold_commands.add_parser(
        "git-refresh", help="检查并刷新原 Gold 文件，修订历史由 Git 保存"
    )
    command.add_argument("--target", required=True, type=Path)
    command.add_argument("--document-id", required=True)
    command.add_argument("--material-root", required=True, type=Path)
    command.add_argument("--revised-by", required=True)
    command.add_argument(
        "--decided-by", required=True, help="已授权本次修正的人类决定者"
    )
    command.add_argument("--basis", required=True, help="既有科学决定的来源与修订原因")

    methods = commands.add_parser(
        "methods",
        help="查询提取入口登记的方法与步骤（当前仅合成模拟实现）",
        description=(
            "列出共享提取入口当前实际可用的方法、步骤与实现标记。"
            "当前只登记 synthetic-mock 模拟实现；真实 direct/"
            "candidate_review 提取留待后续关卡，此处不会冒称可运行。"
        ),
    )
    methods_commands = methods.add_subparsers(dest="methods_command")
    methods_list = methods_commands.add_parser(
        "list", help="列出登记的方法（默认操作）"
    )
    methods_list.add_argument(
        "--json", action="store_true", help="以机器可读 JSON 输出"
    )

    extract = commands.add_parser(
        "extract",
        help="共享提取入口（当前提供合成 demo；真实提取尚未实现）",
        description=(
            "日常与评测共用的提取入口。当前只提供合成材料演示："
            "用模拟服务走名单→数值→收尾全链路，支持专家裁决开关与"
            "暂停后模拟决定恢复；不调用真实模型，不产生货币费用。"
            "真实 direct/candidate_review 提取留待后续关卡。"
        ),
    )
    extract_commands = extract.add_subparsers(dest="extract_command", required=True)
    extract_demo = extract_commands.add_parser(
        "demo",
        help="合成共享提取演示：执行、暂停/决定/恢复并保存输出",
    )
    extract_demo.add_argument(
        "--space", required=True, type=Path, help="演示工作空间目录（独立于仓库）"
    )
    extract_demo.add_argument(
        "--paper",
        required=True,
        choices=["FIC-R05-A", "FIC-R05-B", "FIC-CANDIDATE-C"],
        help="合成论文标识（虚构材料）",
    )
    extract_demo.add_argument(
        "--method",
        default="direct",
        choices=["direct", "candidate_review"],
        help="名单方法（均为 synthetic-mock 实现）；默认 direct",
    )
    extract_demo.add_argument(
        "--mode",
        default="synthetic",
        choices=["daily", "evaluation", "synthetic"],
        help="运行模式；默认 synthetic（演示用）",
    )
    extract_demo.add_argument(
        "--adjudication",
        default="on",
        choices=["on", "off"],
        help="运行中专家裁决开关；默认 on（与入口默认一致）",
    )
    extract_demo.add_argument(
        "--decision",
        default="auto",
        choices=["auto", "no_overall_conclusion", "include", "keep_pending"],
        help=(
            "开启组暂停时的模拟人类决定；auto 按问题类别选择固定模拟决定。"
            "决定内容来自合成材料，不来自 Gold 或评分"
        ),
    )
    extract_demo.add_argument(
        "--output",
        required=True,
        type=Path,
        help="输出目录：保存提取报告与可重评的评分输入集 JSON",
    )

    score = commands.add_parser(
        "score",
        help="独立评分（当前提供合成 demo；正式评分未授权）",
        description=(
            "独立评分入口：只读取评分输入集，不参与提取。当前只提供"
            "合成演示：核对输入指纹后离线重评，可在新进程重复执行。"
            "正式评分需要获准的私有评分空间与 Gold 核对，尚未授权。"
        ),
    )
    score_commands = score.add_subparsers(dest="score_command", required=True)
    score_demo = score_commands.add_parser(
        "demo", help="合成独立评分演示：指纹核对、离线重评并输出 L0/L1/L2"
    )
    score_demo.add_argument(
        "--inputs",
        required=True,
        type=Path,
        help="评分输入集 JSON（extract demo 保存）",
    )
    score_demo.add_argument(
        "--output", default=None, type=Path, help="可选：评分报告 JSON 输出路径"
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
        check_compact_gold_pair,
        check_gold_annotation,
        compare_legacy_with_current,
        migrate_legacy_annotation,
        prepare_save,
        query_operation,
        read_compact_gold_pair,
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
            if (
                args.draft.name == "answer.json"
                and (args.draft.parent / "metadata.json").is_file()
            ):
                pair = read_compact_gold_pair(args.draft)
                report = check_compact_gold_pair(pair, material_root=args.material_root)
                identity, kind = pair.answer.arxiv_id, "answer-v2"
            else:
                annotation = read_gold_annotation(args.draft)
                report = check_gold_annotation(
                    annotation, material_root=args.material_root
                )
                identity, kind = annotation.document_id, annotation.kind
            _dump_json(args.report_json, report.model_dump(mode="json"))
            print(f"[检查] 文档 {identity}（{kind}）")
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
            print("[结论] 无阻断问题；检查不代替科学裁定，请按适用保存或修订流程操作")
            return 0
        if args.gold_command == "compare":
            report = compare_legacy_with_current(
                legacy_path=args.legacy,
                current_path=args.current,
                mapping_path=args.mapping,
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
        if args.gold_command == "git-refresh":
            from .evaluation.git_revision import refresh_gold_in_place

            entry = refresh_gold_in_place(
                target=GoldTarget(args.target),
                document_id=args.document_id,
                material_root=args.material_root,
                revised_by=args.revised_by,
                decided_by=args.decided_by,
                basis=args.basis,
            )
            print(
                json.dumps(entry.model_dump(mode="json"), ensure_ascii=False, indent=2)
            )
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


def _run_methods(args: argparse.Namespace) -> int:
    from hvs_catalog.pipeline import list_methods

    methods = list_methods()
    if getattr(args, "json", False):
        print(json.dumps(methods, ensure_ascii=False, indent=2))
        return 0
    for method in methods:
        realizable = "可运行" if method["real_extraction"] else "仅合成模拟"
        print(
            f"[方法] {method['method_id']}（{method['implementation']}，"
            f"{realizable}，v{method['version']}）"
        )
        for step in method["steps"]:
            print(f"  - {step['step_id']}［{step['kind']}］{step['title']}")
    print(
        "[说明] 当前只登记 synthetic-mock 模拟实现；"
        "真实 direct/candidate_review 提取留待后续关卡，未在本入口冒称可运行。"
    )
    return 0


def _run_extract_demo(args: argparse.Namespace) -> int:
    from hvs_catalog.evaluation.adapt import prediction_from_execution
    from hvs_catalog.evaluation.bundle import save_scoring_bundle
    from hvs_catalog.evaluation.scoring import protocol_only_config
    from hvs_catalog.evaluation.synthetic_gold import (
        build_experiment_plan,
        synthetic_gold_for_paper,
    )
    from hvs_catalog.pipeline import (
        PAPER_C,
        ExtractionRequest,
        PipelineRequestError,
        StepModel,
        get_extraction,
        import_mock_source,
        list_methods,
        resume_extraction,
        start_extraction,
        submit_expert_decision,
    )
    from hvs_catalog.pipeline.mock import PipelineMockService
    from hvs_catalog.pipeline.registry import RegistryError
    from hvs_catalog.records.common import RecordRef
    from hvs_catalog.workspace import WorkspaceError

    expert = args.adjudication == "on"
    if args.decision != "auto" and not expert:
        print(
            "[拒绝] --adjudication off 的运行不请求专家，不接受 --decision",
            file=sys.stderr,
        )
        return 2
    if args.paper != PAPER_C and args.decision == "include":
        print(
            "[拒绝] include 决定只适用于候选处置问题（FIC-CANDIDATE-C）",
            file=sys.stderr,
        )
        return 2
    try:
        from hvs_catalog.workspace import (
            WORKSPACE_FILENAME,
            initialize_workspace,
            open_workspace,
        )

        if (args.space / WORKSPACE_FILENAME).is_file():
            space = open_workspace(args.space)
        else:
            space = initialize_workspace(args.space)
        method_ids = [item["method_id"] for item in list_methods()]
        if args.method not in method_ids:
            print(
                f"[拒绝] 方法 {args.method!r} 未登记；可用：{', '.join(method_ids)}",
                file=sys.stderr,
            )
            return 2
        source = import_mock_source(space, args.paper)
        service = PipelineMockService(
            journal_path=args.output / "service-journal.jsonl"
        )
        request = ExtractionRequest(
            mode=args.mode,
            method=args.method,
            source_refs=[source],
            models=[StepModel(model="mock-default")],
            expert_adjudication=expert,
        )
        request_key = (
            f"cli-demo:{args.paper}:{args.method}:{args.mode}:{args.adjudication}"
        )
        handle = start_extraction(space, request=request, request_key=request_key)
        print(
            f"[执行] {handle.execution_id}（方法 {handle.method}，模式 "
            f"{handle.mode}，专家裁决 {'开' if expert else '关'}，"
            f"输入摘要 {handle.input_digest[:12]}…）"
        )
        report = resume_extraction(space, handle.execution_id, service=service)
        decisions = 0
        while report.outcome == "paused":
            pending = report.pending_question
            if pending is None or not expert:
                print("[错误] 暂停但没有可处理的待办问题", file=sys.stderr)
                return 2
            choice = args.decision
            if choice == "auto":
                from hvs_catalog.pipeline import pending_questions

                views = [
                    view
                    for view in pending_questions(space, handle.execution_id)
                    if view.question_ref
                    == RecordRef.model_validate(pending["question_ref"])
                ]
                category = views[0].record.data.category
                choice = {
                    "author_assessment": "no_overall_conclusion",
                    "inclusion": "include",
                }.get(category)
                if choice is None:
                    print(
                        f"[拒绝] 模拟决定不支持问题类别 {category!r}", file=sys.stderr
                    )
                    return 2
            submit_expert_decision(
                space,
                execution_id=handle.execution_id,
                question_ref=RecordRef.model_validate(pending["question_ref"]),
                request_key=f"{request_key}:decision:{decisions}",
                choice=choice,
            )
            decisions += 1
            print(f"[决定] 模拟人类决定：{choice}")
            report = resume_extraction(space, handle.execution_id, service=service)
        status = get_extraction(space, handle.execution_id)
        if report.outcome != "finished":
            for error in report.errors:
                print(f"[失败] {error}", file=sys.stderr)
            print(
                f"[失败] 执行未完成（outcome={report.outcome}，"
                f"状态 {status['state']}）",
                file=sys.stderr,
            )
            return 2
        usage = status["usage"]
        print(
            f"[完成] 状态 {status['state']}；问题 {len(status['questions'])} 项，"
            f"专家决定 {decisions} 次，模型判断 {len(status['model_judgments'])} 条；"
            f"调用 {usage['attempts']} 次，tokens "
            f"{'未知(null)' if usage['tokens'] is None else usage['tokens']}"
        )
        prediction = prediction_from_execution(space, handle.execution_id)
        gold = synthetic_gold_for_paper(args.paper)
        plan = build_experiment_plan([args.paper], [gold])
        args.output.mkdir(parents=True, exist_ok=True)
        extraction_payload = {
            "schema": "hvs.evaluation.extraction-demo-report",
            "version": 1,
            "handle": handle.to_dict(),
            "report": report.to_dict(),
            "status": status,
        }
        (args.output / "extraction-report.json").write_text(
            json.dumps(extraction_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        digests = save_scoring_bundle(
            args.output / "evaluation-inputs.json",
            plan=plan,
            gold=[gold],
            predictions=[prediction],
            auxiliary=protocol_only_config(),
        )
        print(f"[保存] 提取报告 {args.output / 'extraction-report.json'}")
        print(
            f"[保存] 评分输入集 {args.output / 'evaluation-inputs.json'}"
            f"（gold 摘要 {digests['gold'][:12]}…，可用 score demo 重评）"
        )
        return 0
    except (
        PipelineRequestError,
        RegistryError,
        WorkspaceError,
        ValueError,
    ) as exc:
        print(f"[拒绝] {exc}", file=sys.stderr)
        return 2


def _run_score_demo(args: argparse.Namespace) -> int:
    from hvs_catalog.evaluation.bundle import ScoringBundleError, score_scoring_bundle

    try:
        report = score_scoring_bundle(args.inputs)
    except ScoringBundleError as exc:
        print(f"[拒绝] {exc}", file=sys.stderr)
        return 2
    for paper in report.papers:
        l0, l1, l2 = paper.l0, paper.l1, paper.l2
        print(
            f"[论文] {paper.paper_key}（readiness={paper.readiness}）"
            + (f"：{paper.scope_note}" if paper.scope_note else "")
        )
        print(
            f"  L0 交付 {l0.delivered}/{l0.planned}，合格 {l0.format_valid}/"
            f"{l0.delivered}，完成分布 {dict(l0.completion_counts)}"
        )
        print(
            f"  L1 TP={l1.tp} FP={l1.fp} FN={l1.fn}，"
            f"P={_rate(l1.precision)} R={_rate(l1.recall)} F1={_rate(l1.f1)}"
        )
        print(
            f"  L2 C={l2.correct} P={l2.predicted} G={l2.gold}，"
            f"P={_rate(l2.precision)} R={_rate(l2.recall)} F1={_rate(l2.f1)}"
        )
        for diagnostic in paper.diagnostics:
            print(f"  [诊断] {diagnostic.code}：{diagnostic.message}")
    micro = report.micro
    print(
        f"[汇总] L1 TP={micro.l1.tp} FP={micro.l1.fp} FN={micro.l1.fn}；"
        f"L2 C={micro.l2.correct} P={micro.l2.predicted} G={micro.l2.gold}"
    )
    state = "暂定" if report.batch_provisional else "final"
    print(f"[结论] 整批 {state}；Gold 摘要 {report.digests['gold'][:12]}…")
    for note in report.notes:
        print(f"[说明] {note}")
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        print(f"[保存] 评分报告 {args.output}")
    return 0


def _rate(value: float | None) -> str:
    return "NA" if value is None else f"{value:.4f}"


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
    if args.command == "methods":
        return _run_methods(args)
    if args.command == "extract":
        return _run_extract_demo(args)
    if args.command == "score":
        return _run_score_demo(args)
    return 0
