"""Separately authorized discovery, PDF download, and explicit parser stages."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .pipeline.acquisition import ArxivSearchRequest
from .pipeline.preparation_batch import PreparationBatchService, export_manifest
from .workspace import initialize_workspace, open_workspace


def build_preparation_parser(commands: argparse._SubParsersAction) -> None:
    group = commands.add_parser(
        "prepare", help="批量准备：分阶段检索、下载、MinerU 解析与清单"
    )
    sub = group.add_subparsers(dest="preparation_command", required=True)
    for name, help_text in (
        ("init", "离线初始化文献目录与固定检索范围，不访问外部服务"),
        ("discover", "只检索与去重，不下载、不解析"),
        ("download", "下载批次 PDF，首次获取时固定最新版"),
        ("parse", "批量上传固定 PDF 至 MinerU 官方精准 API（vlm）"),
        ("status", "从已保存记录导出准备清单，不触网、不解析"),
    ):
        command = sub.add_parser(name, help=help_text, description=help_text)
        command.add_argument("--space", required=True, type=Path, help="独立业务空间")
        command.add_argument("--batch", required=True, help="批次标识（固定范围）")
        command.add_argument(
            "--output",
            type=Path,
            help="JSON／CSV 清单目录；新文献空间默认使用 reports/",
        )
        if name in {"init", "discover"}:
            if name == "discover":
                command.add_argument(
                    "--init",
                    action="store_true",
                    help="兼容旧命令：初始化业务空间并检索；离线准备请用 prepare init",
                )
            command.add_argument(
                "--from",
                dest="submitted_from",
                required=name == "init",
                type=date.fromisoformat,
            )
            command.add_argument(
                "--to",
                dest="submitted_to",
                required=name == "init",
                type=date.fromisoformat,
            )
            command.add_argument(
                "--term",
                action="append",
                required=name == "init",
                help="短语关键词，可重复；检索时省略范围参数则使用已保存批次",
            )
            command.add_argument(
                "--round", type=int, default=1, help="查询轮次序号，默认 1"
            )
            command.add_argument(
                "--max-queries",
                type=int,
                help="本次最多推进多少个月／关键词查询，供分段接续",
            )
        elif name in {"download", "parse"}:
            command.add_argument(
                "--id",
                action="append",
                help="只处理这些无版本号 arXiv ID；未指定时处理整个固定批次",
            )
            if name == "parse":
                command.add_argument(
                    "--literature",
                    type=Path,
                    help="仅旧空间可显式导出解析副本；新文献空间直接保存 papers/",
                )
                credentials = command.add_mutually_exclusive_group()
                credentials.add_argument(
                    "--mineru-api-key-file",
                    type=Path,
                    help="官方 API Token 的本地私密文本文件；未指定时读取 MINERU_TOKEN",
                )
                credentials.add_argument(
                    "--env-file",
                    type=Path,
                    help="从指定 .env 读取 MINERU_TOKEN，进程环境变量优先",
                )
                command.add_argument(
                    "--retry-failed",
                    action="store_true",
                    help="为显式 --id 的失败论文建立当前解析器的新执行；保留成功结果和旧失败诊断",
                )


def _build_service(space, *, runner=None):
    return PreparationBatchService(space, runner=runner)


def _progress(event: dict) -> None:
    label = event.get("paper") or f"{event['from']}…{event['to']} {event['terms']}"
    suffix = f"，{event['papers']} 篇" if "papers" in event else ""
    print(
        f"[{event['stage']} {event['current']}/{event['total']}] {label}：{event['state']}{suffix}",
        file=sys.stderr,
        flush=True,
    )


def run_preparation_command(args: argparse.Namespace) -> int:
    space = service = manifest = None
    exit_code = 0
    try:
        defining = args.preparation_command == "init" or (
            args.preparation_command == "discover"
            and any((args.submitted_from, args.submitted_to, args.term))
        )
        if defining:
            if not all((args.submitted_from, args.submitted_to, args.term)):
                raise ValueError("检索范围必须同时提供 --from、--to 和 --term")
            for term in args.term:
                if not ArxivSearchRequest(
                    args.submitted_from, args.submitted_to, term
                ).terms:
                    raise ValueError("检索词不能为空")
            if args.round < 1 or (
                args.max_queries is not None and args.max_queries < 1
            ):
                raise ValueError("轮次及查询推进上限必须是正整数")
        if args.preparation_command == "init":
            space = initialize_workspace(args.space, literature=True)
        elif args.preparation_command == "discover" and args.init:
            if not defining:
                raise ValueError("初始化检索需要完整范围；离线初始化请用 prepare init")
            space = initialize_workspace(args.space)
        else:
            space = open_workspace(args.space)
        if space.is_literature:
            if args.output is None:
                args.output = space.root / "reports"
            if getattr(args, "literature", None) is not None:
                raise ValueError("新文献空间直接保存 papers/，不另行导出材料副本")
        elif args.output is None and args.preparation_command != "parse":
            raise ValueError("旧空间需要显式指定 --output")
        if args.preparation_command == "parse":
            from .materials.mineru_official import OfficialMinerU

            service = _build_service(
                space,
                runner=OfficialMinerU(
                    space,
                    api_key_file=args.mineru_api_key_file,
                    env_file=args.env_file,
                ),
            )
        else:
            service = _build_service(space)
        if defining:
            service.create_batch(
                args.batch,
                submitted_from=args.submitted_from,
                submitted_to=args.submitted_to,
                terms=args.term,
                round_seq=args.round,
            )
        if args.preparation_command == "discover":
            service.discover(
                args.batch, max_queries=args.max_queries, progress=_progress
            )
        elif args.preparation_command in {"download", "parse"}:
            action = getattr(service, args.preparation_command)
            options = {"identifiers": args.id, "progress": _progress}
            if args.preparation_command == "parse":
                options["retry_failed"] = args.retry_failed
            action(args.batch, **options)
    except KeyboardInterrupt:
        print(
            "已中断；保留已保存的查询、版本及执行，可用同一批次接续。", file=sys.stderr
        )
        exit_code = 130
    except Exception as exc:  # noqa: BLE001 -- CLI boundary retains and exports saved facts.
        print(f"批量准备失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        exit_code = 1
    finally:
        try:
            if service is not None:
                manifest = service.get_manifest(args.batch)
                paths = (
                    list(export_manifest(manifest, args.output)) if args.output else []
                )
                if args.preparation_command == "parse" and args.literature is not None:
                    service.materials.export_literature(manifest, args.literature)
                    paths.append(args.literature / "summary.json")
                print(
                    json.dumps(
                        {
                            "batch_id": args.batch,
                            "discovery_complete": manifest["discovery_complete"],
                            "summary": manifest["summary"],
                            "reports": [str(path.resolve()) for path in paths],
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
        except Exception as exc:  # noqa: BLE001 -- Report failures must preserve the original exit code.
            print(f"清单导出失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            exit_code = exit_code or 1
        finally:
            if space is not None:
                space.close()
    if exit_code or manifest is None:
        return exit_code or 1
    if args.preparation_command == "init":
        return 0
    if not manifest["discovery_complete"]:
        return 3
    if args.preparation_command in {"download", "parse"}:
        selected = set(args.id) if args.id is not None else None
        papers = [
            p
            for p in manifest["papers"]
            if selected is None or p["arxiv_id"] in selected
        ]
        if args.preparation_command == "download":
            ready = all(
                p["download"]["state"] in {"downloaded", "imported"} for p in papers
            ) and not any(
                p["issue"] and p["issue"]["stage"] == "download" for p in papers
            )
        else:
            ready = all(p["preparation"]["state"] == "finished" for p in papers)
        return 0 if ready else 3
    return 0
