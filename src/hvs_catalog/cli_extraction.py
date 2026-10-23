"""Roster configuration, previews, extraction, and durable recovery commands."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .llm.prompts import PromptInput
from .pipeline.configuration import (
    check_extraction_config,
    load_extraction_config,
    preview_extraction_prompt,
)
from .workspace import WorkspaceError


def add_extraction_commands(commands):
    commands.add_parser("config", help="输出默认提取配置（JSON）")
    check = commands.add_parser("check", help="离线检查名单模型、步骤与英文提示配置")
    check.add_argument("--config", type=Path)
    preview = commands.add_parser(
        "preview", help="离线预览完整提示、填写 Schema 与实际协议参数"
    )
    preview.add_argument("--config", type=Path)
    preview.add_argument("--step", required=True, choices=("roster", "quantities"))
    preview.add_argument("--identifier", action="append", dest="identifiers")
    preview.add_argument(
        "--input",
        type=Path,
        required=True,
        help="本步材料与必要已有结果的 JSON；不调用模型",
    )
    for name, help_text in (
        ("roster", "对固定准备输入执行真实名单提取"),
        ("run", "执行名单、数值与最终结果组装"),
    ):
        roster = commands.add_parser(name, help=help_text)
        roster.add_argument("--config", type=Path)
        roster.add_argument("--space", type=Path, required=True)
        selection = roster.add_mutually_exclusive_group(required=True)
        selection.add_argument("--preparation", help="已完成的材料准备执行编号")
        if name == "run":
            selection.add_argument("--roster-execution", help="复用固定名单执行")
        roster.add_argument("--request-key", required=True)
        roster.add_argument("--env-file", type=Path)
    batch = commands.add_parser("batch", help="并行运行多篇固定PDF的名单提取")
    batch.add_argument("--space", type=Path, required=True)
    batch.add_argument("--config", type=Path)
    batch.add_argument("--request-key")
    batch.add_argument("--env-file", type=Path)
    batch.add_argument("--concurrency", type=int)
    batch.add_argument("--scope", choices=("roster", "full"), default="full")
    selection = batch.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--preparation", action="append", help="重复指定准备执行编号，创建新提取"
    )
    selection.add_argument(
        "--execution", action="append", help="重复指定已固定提取执行编号，恢复批次"
    )
    for name in ("status", "resume"):
        command = commands.add_parser(
            name, help="查询提取执行" if name == "status" else "按固定输入恢复提取执行"
        )
        command.add_argument("--space", type=Path, required=True)
        command.add_argument("--execution", required=True)
        command.add_argument("--env-file", type=Path)
    close = commands.add_parser("close", help="验证并精简提取结果，明确结束恢复现场")
    close.add_argument("--space", type=Path, required=True)
    close.add_argument("--execution", required=True)
    close.add_argument("--end", action="store_true", help="明确结束失败或中断执行")
    retry = commands.add_parser(
        "retry-network", help="仅重试已证实未发送的名单网络失败，复用前轮提交"
    )
    retry.add_argument("--space", type=Path, required=True)
    retry.add_argument("--execution", required=True)
    retry.add_argument("--request-key", required=True)
    retry.add_argument(
        "--source-index", action="append", type=int, dest="source_indices"
    )
    retry.add_argument("--env-file", type=Path)
    correction = commands.add_parser(
        "continue-format", help="接续未用完的格式补正，不重发初次提取"
    )
    correction.add_argument("--space", type=Path, required=True)
    correction.add_argument("--execution", required=True)
    correction.add_argument("--request-key", required=True)
    correction.add_argument("--maximum", type=int, choices=(1, 2), default=2)
    correction.add_argument("--env-file", type=Path)


def run_extraction_command(args) -> int:
    space = None
    try:
        name = args.extract_command
        if name == "config":
            result = load_extraction_config().model_dump(mode="json")
        elif name == "check":
            result = check_extraction_config(load_extraction_config(args.config))
        elif name == "preview":
            result = preview_extraction_prompt(
                load_extraction_config(args.config),
                args.step,
                PromptInput.model_validate_json(args.input.read_bytes()),
                args.identifiers,
            )
        else:
            from .pipeline import (
                get_extraction,
                resume_extraction,
                start_extraction,
            )
            from .workspace import (
                open_workspace,
            )

            if name == "batch":
                from .llm.managed import CallService
                from .pipeline import ExtractionTask, resume_extractions
                from .pipeline.configuration import stored_configuration
                from .runtime import get_execution

                space = open_workspace(args.space)
                if args.preparation:
                    if not args.request_key:
                        raise ValueError("batch creation requires --request-key")
                    config = load_extraction_config(args.config)
                    if args.concurrency is not None:
                        config = type(config).model_validate(
                            {**config.model_dump(), "parallelism": args.concurrency}
                        )
                    executions = [
                        start_extraction(
                            space,
                            configuration=config,
                            preparation_execution_id=prep,
                            request_key=args.request_key + ":" + prep,
                            scope=args.scope,
                        ).execution_id
                        for prep in args.preparation
                    ]
                else:
                    if args.config is not None or args.request_key is not None:
                        raise ValueError(
                            "batch recovery uses saved configurations and request keys"
                        )
                    executions = args.execution
                tasks = []
                for execution in executions:
                    config = stored_configuration(
                        get_execution(space, execution).input_snapshot
                    )
                    tasks.append(
                        ExtractionTask(
                            space,
                            execution,
                            CallService(config.runtime, env_file=args.env_file),
                        )
                    )
                reports = resume_extractions(tasks, concurrency=args.concurrency)
                result = {
                    "outcome": "failed"
                    if any(r.outcome == "failed" for r in reports)
                    else "paused"
                    if any(r.outcome == "paused" for r in reports)
                    else "finished",
                    "implementation": "model-extraction",
                    "executions": [r.to_dict() for r in reports],
                }
            elif name in {"roster", "run"}:
                from .llm.managed import CallService

                config = load_extraction_config(args.config)
                space = open_workspace(args.space)
                handle = start_extraction(
                    space,
                    configuration=config,
                    preparation_execution_id=args.preparation,
                    roster_execution_id=getattr(args, "roster_execution", None),
                    request_key=args.request_key,
                    scope="full" if name == "run" else "roster",
                )
                result = resume_extraction(
                    space,
                    handle.execution_id,
                    service=CallService(config.runtime, env_file=args.env_file),
                ).to_dict()
                result["implementation"] = "model-extraction"
            elif name == "close":
                from .workspace.retention import close_extraction

                space = open_workspace(args.space)
                result = close_extraction(space, args.execution, end=args.end)["status"]
                result = {**result, "retention": "closed", "resumable": False}
            else:
                space = open_workspace(args.space)
                from .runtime import get_execution

                handle = get_execution(space, args.execution)
                if handle.input_snapshot.options.get(
                    "extraction_config"
                ) is None or not handle.flow_id.startswith(("hvs-extraction",)):
                    raise ValueError("this command requires a configured extraction")
                if handle.input_snapshot.options.get("extraction_scope") not in {
                    "roster",
                    "full",
                }:
                    raise ValueError("Unsupported extraction execution.")
                from .llm.managed import CallService
                from .pipeline.configuration import stored_configuration

                service = CallService(
                    stored_configuration(handle.input_snapshot).runtime,
                    env_file=args.env_file,
                )
                intent = None
                if name == "retry-network":
                    from .pipeline import retry_network_failures

                    intent = retry_network_failures(
                        space,
                        args.execution,
                        request_key=args.request_key,
                        source_indices=args.source_indices,
                    )
                elif name == "continue-format":
                    from .pipeline import continue_format_corrections

                    intent = continue_format_corrections(
                        space,
                        args.execution,
                        request_key=args.request_key,
                        maximum=args.maximum,
                    )
                result = (
                    get_extraction(space, args.execution)
                    if name == "status"
                    else resume_extraction(
                        space, args.execution, service=service
                    ).to_dict()
                )
                result["implementation"] = "model-extraction"
                if intent is not None:
                    result[
                        {
                            "retry-network": "network_retry",
                            "continue-format": "format_continuation",
                        }[name]
                    ] = intent
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return (
            1 if isinstance(result, dict) and result.get("outcome") == "failed" else 0
        )
    except (ValueError, OSError, WorkspaceError) as exc:
        print(f"[拒绝] {exc}", file=sys.stderr)
        return 2
    finally:
        if space is not None:
            space.close()
