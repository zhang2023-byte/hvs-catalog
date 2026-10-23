"""Explicit benchmark preparation, execution, read-only checks, and reporting."""

import json
import sys
from pathlib import Path


def add_benchmark_commands(commands):
    parser = commands.add_parser("benchmark", help="共享材料、固定实验、归档与比较")
    parser.add_argument(
        "--root", type=Path, required=True, help="完整 Benchmark 根目录"
    )
    sub = parser.add_subparsers(dest="benchmark_command", required=True)
    sub.add_parser("init", help="初始化空 Benchmark")
    imported = sub.add_parser(
        "import-materials", help="导入已解析材料，不调用解析或提取服务"
    )
    imported.add_argument("--package", required=True, type=Path)
    imported.add_argument("--paper", required=True)
    imported.add_argument(
        "--variant", required=True, choices=["mineru_local", "mineru_cloud"]
    )
    prepare = sub.add_parser("prepare", help="固定新实验的材料和配置，不调用模型")
    prepare.add_argument("--round")
    prepare.add_argument("--papers", required=True, nargs="+")
    prepare.add_argument(
        "--variant", required=True, choices=["mineru_local", "mineru_cloud"]
    )
    prepare.add_argument("--config", required=True, type=Path)
    prepare.add_argument("--purpose", required=True)
    prepare.add_argument("--scope", choices=["full", "roster"], default="full")
    run = sub.add_parser("run", help="执行或接续指定实验；此命令会请求模型")
    run.add_argument("--round", required=True)
    run.add_argument("--concurrency", type=int)
    run.add_argument("--env-file", type=Path)
    close = sub.add_parser("close", help="明确结束并归档未完成实验，不请求模型")
    close.add_argument("--round", required=True)
    close.add_argument("--end", action="store_true", required=True)
    preview = sub.add_parser("preview", help="离线预览所选材料，不创建实验")
    preview.add_argument(
        "--variant", required=True, choices=["mineru_local", "mineru_cloud"]
    )
    preview.add_argument("--papers", required=True, nargs="+")
    preview.add_argument("--config", required=True, type=Path)
    preview.add_argument("--name", required=True)
    for name in ("status", "check", "report"):
        child = sub.add_parser(name)
        child.add_argument("--round", required=True)
    score = sub.add_parser("score", help="按明确提供的 Gold 路径评分全部计划论文")
    score.add_argument("--round", required=True)
    score.add_argument(
        "--references",
        required=True,
        type=Path,
        help="arXiv ID 到 Gold 文件路径的 JSON 映射",
    )
    compare = sub.add_parser("compare", help="从已有评分和费用生成比较")
    compare.add_argument("--rounds", required=True, nargs="+")
    compare.add_argument("--name", required=True)
    migrate = sub.add_parser(
        "migrate", help="将旧 Benchmark 迁移到新的空目录；核验后再替换原目录"
    )
    migrate.add_argument("--output", required=True, type=Path)


def run_benchmark_command(args):
    from .benchmark.archive import verify_archive
    from .benchmark.experiments import (
        close_experiment,
        experiment_status,
        prepare_experiment,
        preview_materials,
        read_experiment,
        run_experiment,
        score_experiment,
        verify_selection,
    )
    from .benchmark.materials import migrate_package
    from .benchmark.reporting import compare_experiments, write_report
    from .workspace import initialize_workspace, open_workspace
    from .workspace.errors import WorkspaceError

    try:
        command = args.benchmark_command
        if command == "init":
            space = initialize_workspace(args.root, benchmark=True)
            space.close()
            result = {"root": str(args.root.resolve())}
        elif command == "import-materials":
            space = open_workspace(args.root)
            try:
                with space.lock():
                    package = migrate_package(
                        args.package, space, args.paper, args.variant
                    )
                result = {"paper": package.source.paper_key, "variant": args.variant}
            finally:
                space.close()
        elif command == "prepare":
            result = prepare_experiment(
                args.root,
                papers=args.papers,
                variant=args.variant,
                configuration=json.loads(args.config.read_text()),
                purpose=args.purpose,
                scope=args.scope,
                name=args.round,
            )
        elif command == "run":
            result = run_experiment(
                args.root,
                args.round,
                concurrency=args.concurrency,
                env_file=args.env_file,
            )
            write_report(args.root, args.round)
        elif command == "close":
            result = close_experiment(args.root, args.round)
            write_report(args.root, args.round)
        elif command == "preview":
            result = preview_materials(
                args.root,
                papers=args.papers,
                variant=args.variant,
                configuration=json.loads(args.config.read_text()),
                name=args.name,
            )
        elif command in {"status", "check"}:
            result = experiment_status(args.root, args.round)
            if command == "check":
                verify_selection(args.root, read_experiment(args.root, args.round))
                for paper in result["papers"]:
                    if paper["state"] == "archived":
                        verify_archive(args.root, args.round, paper["arxiv_id"])
        elif command == "report":
            result = {"report": str(write_report(args.root, args.round))}
        elif command == "score":
            references = json.loads(args.references.read_text())
            references = {
                k: (args.references.parent / v).resolve() for k, v in references.items()
            }
            result = score_experiment(args.root, args.round, references).model_dump(
                mode="json"
            )
            write_report(args.root, args.round)
        elif command == "compare":
            result = {
                "comparison": str(
                    compare_experiments(args.root, args.rounds, args.name)
                )
            }
        elif command == "migrate":
            from .benchmark.migration import migrate_benchmark

            result = migrate_benchmark(args.root, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, WorkspaceError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
