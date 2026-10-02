"""``hvs-catalog materials`` 子命令组：材料准备生命周期。

设计与边界：

- 状态查询不触网、不隐式初始化；工作空间必须已经存在。
- ``prepare`` 以获取阶段保存的准确来源修订建立（或同请求复用）准备
  执行；多个有效主稿必须显式 ``--root-file`` 选择，不按名称猜。
- ``resume`` 在歧义编号范围等待专家决定后继续；专家决定经 R05 共同
  接口（review CLI／Python）提交，本组不提供裁决入口。
- 退出码：0 成功；1 工程错误；2 参数错误；3 暂停等待决定；4 无可用
  材料；5 已完成（幂等重入）。准备完成但存在缺口仍是成功：缺口随
  ``--json`` 输出如实呈现，不被记成"论文未报告"或"成功空名单"。
"""

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
EXIT_PAUSED = 3
EXIT_NO_MATERIAL = 4
EXIT_ALREADY_FINISHED = 5


def build_materials_parser(commands: argparse._SubParsersAction) -> None:
    """注册 materials 子命令组。"""
    group = commands.add_parser(
        "materials",
        help="材料准备：来源包展开、正文／表格解析与准备输入交付",
    )
    sub = group.add_subparsers(dest="materials_command", required=True)

    prepare = sub.add_parser("prepare", help="准备一篇论文的材料输入")
    prepare.add_argument("--space", required=True, help="工作空间路径")
    prepare.add_argument("--paper", help="arXiv 论文标识（使用当前选定材料）")
    prepare.add_argument(
        "--version", type=int, help="指定材料版本；默认使用当前选定版本"
    )
    prepare.add_argument("--request-key", required=True, help="请求键（同输入复用）")
    prepare.add_argument(
        "--root-file",
        help="多个有效主稿时的显式选择（包内相对路径）",
    )
    prepare.add_argument(
        "--expert-adjudication",
        action="store_true",
        help="歧义编号范围经 R05 接口提问并等待专家",
    )
    prepare.add_argument(
        "--encoding",
        choices=["utf-8", "latin-1"],
        help="非 UTF-8 成员的操作者固定解码选择（进入固定输入；"
        "未指定且未声明时保持未处理，不猜编码）",
    )
    prepare.add_argument(
        "--range",
        action="append",
        default=[],
        metavar="NOTATION|FILE|START|END",
        help="编号范围记号及其出处（可重复）：记法|文件|起始行|结束行；"
        "歧义记号可再加 |candidates:代码=前缀 形式的候选解释"
        "（如 |candidates:A=HVS）",
    )
    prepare.add_argument("--json", action="store_true", help="以 JSON 输出结果")

    status = sub.add_parser("status", help="查询准备执行状态")
    status.add_argument("--space", required=True, help="工作空间路径")
    status.add_argument("--execution", help="具体执行标识；缺省列出全部")
    status.add_argument("--json", action="store_true", help="以 JSON 输出结果")

    resume = sub.add_parser("resume", help="继续暂停的准备执行")
    resume.add_argument("--space", required=True, help="工作空间路径")
    resume.add_argument("--execution", required=True, help="执行标识")
    resume.add_argument("--json", action="store_true", help="以 JSON 输出结果")

    consume = sub.add_parser("consume", help="无模型核对已完成的准备输入")
    consume.add_argument("--space", required=True, help="工作空间路径")
    consume.add_argument("--execution", required=True, help="执行标识")
    consume.add_argument("--json", action="store_true", help="以 JSON 输出结果")


def _parse_range(value: str) -> dict:
    parts = value.split("|")
    if len(parts) < 4:
        raise ValueError(f"--range 需要 记法|文件|起始行|结束行 四段：{value!r}")
    notation, file, start, end = parts[:4]
    entry = {
        "notation": notation,
        "file": file,
        "start_line": int(start),
        "end_line": int(end),
    }
    for extra in parts[4:]:
        if not extra.startswith("candidates:"):
            raise ValueError(f"--range 的附加段必须是 candidates: 前缀：{extra!r}")
        candidates = []
        for item in extra[len("candidates:") :].split(","):
            if not item:
                continue
            code, _, prefix = item.partition("=")
            candidates.append(
                {
                    "code": code,
                    "kind": "object_range",
                    "prefix": prefix,
                    "description": f"对象编号区间，前缀 {prefix}",
                }
            )
        entry["candidates"] = candidates
    return entry


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
        ranges = [_parse_range(item) for item in args.range]
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2
    try:
        space = _open_space(args.space)
        service = MaterialsService(space)
        handle, report = service.prepare_materials(
            request_key=args.request_key,
            paper=args.paper,
            version=args.version,
            root_file=args.root_file,
            range_notations=ranges,
            expert_adjudication=args.expert_adjudication,
            encoding=args.encoding,
        )
    except MaterialsPreparationError as exc:
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
        if report.pending_question:
            print(
                "等待专家决定（经 R05 接口提交后 resume）："
                f"{report.pending_question['question_ref']['record_id']}"
            )
    if report.outcome == "paused":
        return EXIT_PAUSED
    if report.mode == "already-finished":
        # prepare 的幂等重放：同请求同输入已完成，与 resume 的幂等重入
        # 同一退出码（文档语义对齐，二次审核发现 24）。
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
                    if row.flow_id == "hvs-materials-prepare"
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
    if report.outcome == "paused":
        return EXIT_PAUSED
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
