"""Prepare or reuse fixed PDF materials and inspect saved preparation state."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hvs_catalog.materials.service import (
    MaterialsPreparationError,
    MaterialsService,
)
from hvs_catalog.workspace import WorkspaceError, open_workspace

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_MATERIAL = 4
EXIT_ALREADY_FINISHED = 5


def build_materials_parser(commands: argparse._SubParsersAction) -> None:
    """Register materials subcommands."""
    group = commands.add_parser(
        "materials",
        help="材料准备：MinerU 官方解析、固定材料复用与证据定位",
    )
    sub = group.add_subparsers(dest="materials_command", required=True)

    export = sub.add_parser(
        "export", help="导出独立解析材料包，包含固定元数据，不带执行历史"
    )
    export.add_argument("--space", required=True, type=Path)
    export.add_argument(
        "--source-ref", required=True, help="固定来源引用 record_id@revision"
    )
    export.add_argument("--output", required=True, type=Path)

    imported = sub.add_parser(
        "import", help="从独立材料包导入固定输入，不下载、解析或启动提取"
    )
    imported.add_argument(
        "--space", required=True, type=Path, help="已初始化的目标空间"
    )
    imported.add_argument("--package", required=True, type=Path)
    imported.add_argument(
        "--material-root", type=Path, help="共享材料包声明的完整 Benchmark 根目录"
    )

    reuse = sub.add_parser("reuse", help="复制已准备材料到独立空间，不下载或重新解析")
    reuse.add_argument("--space", required=True, type=Path, help="已初始化的目标空间")
    reuse.add_argument("--source-space", required=True, type=Path)
    reuse.add_argument("--preparation", required=True)

    prepare = sub.add_parser("prepare", help="准备一篇论文的材料输入")
    prepare.add_argument("--space", required=True, help="工作空间路径")
    prepare.add_argument("--paper", help="arXiv 论文标识（使用当前选定材料）")
    prepare.add_argument(
        "--version", type=int, help="指定材料版本；默认使用当前选定版本"
    )
    prepare.add_argument("--request-key", required=True, help="请求键（同输入复用）")
    prepare.add_argument(
        "--source-ref", help="固定来源引用（record_id@revision，与 --paper 互斥）"
    )
    prepare.add_argument("--json", action="store_true", help="以 JSON 输出结果")

    status = sub.add_parser("status", help="查询准备执行状态")
    status.add_argument("--space", required=True, help="工作空间路径")
    status.add_argument("--execution", help="具体执行标识；缺省列出全部")
    status.add_argument("--json", action="store_true", help="以 JSON 输出结果")

    resume = sub.add_parser("resume", help="恢复未完成的准备执行")
    resume.add_argument("--space", required=True, help="工作空间路径")
    resume.add_argument("--execution", required=True, help="执行标识")
    resume.add_argument("--json", action="store_true", help="以 JSON 输出结果")

    consume = sub.add_parser("consume", help="无模型核对已完成的准备输入")
    consume.add_argument("--space", required=True, help="工作空间路径")
    consume.add_argument("--execution", required=True, help="执行标识")
    consume.add_argument("--json", action="store_true", help="以 JSON 输出结果")


def _parse_source_ref(value):
    if value is None:
        return None
    from .records import RecordRef

    identifier, revision = value.rsplit("@", 1)
    return RecordRef(record_id=identifier, revision=int(revision))


def _open_space(path: str):
    return open_workspace(Path(path))


def _run_prepare(args: argparse.Namespace) -> int:
    if args.version is not None and args.version < 1:
        print(
            f"参数错误：--version 必须是正整数（收到 {args.version}）",
            file=sys.stderr,
        )
        return 2
    try:
        space = _open_space(args.space)
        service = MaterialsService(space)
        handle, report = service.prepare_materials(
            request_key=args.request_key,
            paper=args.paper,
            version=args.version,
            source_ref=_parse_source_ref(args.source_ref),
        )
    except (MaterialsPreparationError, ValueError) as exc:
        print(f"材料不可用：{exc}", file=sys.stderr)
        return EXIT_NO_MATERIAL
    except WorkspaceError as exc:
        print(f"工程错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        if "space" in locals() and space is not None:
            space.close()
    if args.json:
        print(
            json.dumps(
                {
                    "handle": {
                        "execution_id": handle.execution_id,
                        "request_key": handle.request_key,
                        "reused": handle.reused,
                        "source_ref": handle.source_ref,
                        "input_digest": handle.input_digest,
                    },
                    "report": report.to_dict(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"执行 {handle.execution_id}（{'复用' if handle.reused else '新建'}）")
        print(f"结果：{report.outcome} / {report.state}")
        if report.summary:
            print(f"汇总：{json.dumps(report.summary, ensure_ascii=False)}")
    if report.mode == "already-finished":
        # Completed replay uses the same exit code as idempotent resume.
        return EXIT_ALREADY_FINISHED
    if report.outcome == "failed":
        for error in report.errors:
            print(f"错误：{error}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def _run_status(args: argparse.Namespace) -> int:
    try:
        space = _open_space(args.space)
    except WorkspaceError as exc:
        print(f"工程错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    try:
        service = MaterialsService(space)
        if args.execution:
            payload = service.get_preparation(args.execution)
        else:
            payload = {
                "executions": [
                    {
                        "execution_id": row.execution_id,
                        "state": row.state,
                        "request_key": row.request_key,
                    }
                    for row in space.store().list_executions()
                    if row.flow_id == "hvs-pdf-prepare"
                ]
            }
    except (WorkspaceError, MaterialsPreparationError) as exc:
        print(f"工程错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        space.close()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return EXIT_OK


def _run_resume(args: argparse.Namespace) -> int:
    try:
        space = _open_space(args.space)
        service = MaterialsService(space)
        report = service.resume_preparation(args.execution)
    except (WorkspaceError, MaterialsPreparationError) as exc:
        print(f"工程错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        if "space" in locals() and space is not None:
            space.close()
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"执行 {report.execution_id}：{report.outcome} / {report.state}")
        if report.summary:
            print(f"汇总：{json.dumps(report.summary, ensure_ascii=False)}")
    if report.mode == "already-finished":
        return EXIT_ALREADY_FINISHED
    if report.outcome == "failed":
        for error in report.errors:
            print(f"错误：{error}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def _run_consume(args: argparse.Namespace) -> int:
    try:
        space = _open_space(args.space)
        service = MaterialsService(space)
        result = service.consume_prepared_input(args.execution)
    except (WorkspaceError, MaterialsPreparationError) as exc:
        print(f"工程错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        if "space" in locals() and space is not None:
            space.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        status = "通过" if result["ok"] else "发现问题"
        print(f"准备输入核对{status}：执行 {args.execution}")
        for problem in result["problems"]:
            print(f"问题：{problem}", file=sys.stderr)
    return EXIT_OK if result["ok"] else EXIT_ERROR


def run_materials_command(args: argparse.Namespace) -> int:
    command = args.materials_command
    if command in {"export", "import"}:
        from .materials.package import export_material_package, import_material_package

        space = None
        try:
            space = open_workspace(args.space, read_only=command == "export")
            if command == "export":
                package = export_material_package(
                    space, _parse_source_ref(args.source_ref), args.output
                )
                print(package.model_dump_json())
            else:
                print(
                    import_material_package(
                        args.package, space, root=args.material_root
                    ).model_dump_json()
                )
            return EXIT_OK
        except (ValueError, TypeError, OSError, WorkspaceError) as exc:
            print(f"[拒绝] {exc}", file=sys.stderr)
            return EXIT_ERROR
        finally:
            if space is not None:
                space.close()
    if command == "reuse":
        from .materials import reuse_prepared_materials

        source = target = None
        try:
            source = open_workspace(args.source_space, read_only=True)
            target = open_workspace(args.space)
            ref = reuse_prepared_materials(source, args.preparation, target)
            print(ref.model_dump_json())
            return EXIT_OK
        except (ValueError, OSError, WorkspaceError, MaterialsPreparationError) as exc:
            print(f"[拒绝] {exc}", file=sys.stderr)
            return EXIT_ERROR
        finally:
            for space in (source, target):
                if space is not None:
                    space.close()
    if command == "prepare":
        return _run_prepare(args)
    if command == "status":
        return _run_status(args)
    if command == "resume":
        return _run_resume(args)
    if command == "consume":
        return _run_consume(args)
    print(f"未知 materials 子命令：{command}", file=sys.stderr)  # pragma: no cover
    return EXIT_ERROR
