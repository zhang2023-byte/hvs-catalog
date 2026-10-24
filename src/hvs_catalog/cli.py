"""CLI for Gold tools, saved full PDF extraction, program-only scoring and replay,
configuration/previews, offline persistence/recovery demos, and arXiv acquisition.
Real materials and Gold are used only within the operator's authorized scope.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from importlib.metadata import version


def get_version() -> str:
    """Return the installed package version, defined only in pyproject.toml."""
    return version("hvs-catalog")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hvs-catalog",
        description="高速星文献材料准备、名单与数值提取，以及保存恢复。",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"hvs-catalog {get_version()}",
    )
    commands = parser.add_subparsers(dest="command")
    gold = commands.add_parser(
        "gold",
        help="原PDF Gold标注校验、比较、保存与读取",
        description="正式Gold直接用于评分。保存须有具体人类批准，不执行Git。",
    )
    from .cli_gold import add_gold_commands

    add_gold_commands(gold.add_subparsers(dest="gold_command", required=True))
    extract = commands.add_parser(
        "extract",
        help="名单与数值提取、配置预览及保存恢复",
        description="使用固定论文材料和元数据执行提取。run 和 batch 默认执行完整流程。",
    )
    extract_commands = extract.add_subparsers(dest="extract_command", required=True)
    from .cli_extraction import add_extraction_commands

    add_extraction_commands(extract_commands)
    score = commands.add_parser(
        "score",
        help="纯程序评分：已存提取、Gold标注和冻结输入重评",
        description=(
            "独立评分入口：run 按明确清单读取已存提取与获准Gold，核对固定PDF；"
            "replay 核对输入指纹后离线重评。真实Gold须在获准私有空间使用。"
        ),
    )
    score_commands = score.add_subparsers(dest="score_command", required=True)
    from .cli_scoring import add_scoring_commands

    add_scoring_commands(score_commands)
    from .cli_acquisition import build_acquisition_parser
    from .cli_materials import build_materials_parser
    from .cli_preparation import build_preparation_parser

    build_acquisition_parser(commands)
    build_materials_parser(commands)
    build_preparation_parser(commands)
    from .cli_llm import add_model_commands

    add_model_commands(commands)
    from .cli_benchmark import add_benchmark_commands

    add_benchmark_commands(commands)
    from .cli_catalog import add_catalog_commands

    add_catalog_commands(commands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)  # --help and --version exit directly during parsing.
    if args.command is None:
        # Show help until another top-level operation is implemented.
        parser.print_help()
        return 0
    if args.command == "catalog":
        from .cli_catalog import run_catalog_command

        return run_catalog_command(args)
    if args.command == "gold":
        from .cli_gold import run_gold_command

        return run_gold_command(args)
    if args.command == "extract":
        from .cli_extraction import run_extraction_command

        return run_extraction_command(args)
    if args.command == "score":
        from .cli_scoring import run_scoring_command

        return run_scoring_command(args)
    if args.command == "benchmark":
        from .cli_benchmark import run_benchmark_command

        return run_benchmark_command(args)
    if args.command == "acquisition":
        from .cli_acquisition import EXIT_OK

        result = args.handler(args)
        assert isinstance(result, int)
        return EXIT_OK if result == 0 else result
    if args.command == "materials":
        from .cli_materials import run_materials_command

        return run_materials_command(args)
    if args.command == "prepare":
        from .cli_preparation import run_preparation_command

        return run_preparation_command(args)
    if args.command == "models":
        from .cli_llm import run_model_command

        return run_model_command(args)
    return 0
