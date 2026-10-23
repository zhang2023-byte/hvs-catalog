"""CLI for frozen scoring references, saved extractions and offline replay."""

import sys
from pathlib import Path


def add_scoring_commands(commands):
    run = commands.add_parser("run", help="核对固定参考答案与已存提取，输出 L0/L1/L2")
    run.add_argument(
        "--manifest", required=True, type=Path, help="显式评分输入清单 JSON"
    )
    run.add_argument("--output", required=True, type=Path, help="新建评分输出目录")
    replay = commands.add_parser("replay", help="校验冻结输入并离线重评")
    replay.add_argument("--inputs", required=True, type=Path)
    replay.add_argument("--output", type=Path, help="可选的新报告输出目录")


def _rate(value):
    return "NA" if value is None else f"{value:.4f}"


def run_scoring_command(args):
    from .evaluation.bundle import score_scoring_bundle
    from .evaluation.saved import export_scoring_report, score_saved_manifest
    from .workspace import WorkspaceError

    try:
        if args.score_command == "run":
            report = score_saved_manifest(args.manifest, args.output)
        else:
            report = score_scoring_bundle(args.inputs)
            if args.output is not None:
                export_scoring_report(report, args.output)
    except (ValueError, TypeError, OSError, WorkspaceError, KeyError) as exc:
        print(f"[拒绝] {exc}", file=sys.stderr)
        return 2
    for name, row in [(p.arxiv_id, p) for p in report.papers] + [
        ("汇总", report.micro)
    ]:
        print(
            f"[{name}] L0 交付 {row.l0.delivered}/{row.l0.planned}，格式合格 {row.l0.format_valid}/{row.l0.delivered}，状态 {row.l0.completion_counts}"
        )
        print(
            f"  L1 TP={row.l1.tp} FP={row.l1.fp} FN={row.l1.fn}，P={_rate(row.l1.precision)} R={_rate(row.l1.recall)} F1={_rate(row.l1.f1)}"
        )
        if row.l2:
            print(
                f"  L2 C={row.l2.correct} P={row.l2.predicted} G={row.l2.gold}，P={_rate(row.l2.precision)} R={_rate(row.l2.recall)} F1={_rate(row.l2.f1)}"
            )
        details = row.roster_details
        print(
            f"  已匹配星：贡献类型正确 {details.contribution_correct}/{details.matched_stars}，漏提标识 {details.missing_identifiers}，多提标识 {details.extra_identifiers}"
        )
    print(f"[输入摘要] {report.input_sha256}")
    print(
        "[口径] 仅严格匹配或已批准格式等价算正确；不评价条件、证据和备注的语义正确性。"
    )
    return 0
