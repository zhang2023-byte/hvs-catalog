"""Extraction configuration, previews, prepared roster calls and mock runs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .llm.prompts import PromptInput
from .pipeline.configuration import (
    check_extraction_config,
    load_extraction_config,
    preview_extraction_prompt,
    start_configured_extraction,
)
from .workspace import WorkspaceError


def add_extraction_commands(commands):
    commands.add_parser("config", help="输出默认提取配置（JSON）")
    check = commands.add_parser("check", help="离线检查模型、步骤、选例与英文提示配置")
    check.add_argument("--config", type=Path)
    preview = commands.add_parser(
        "preview", help="离线预览完整提示、填写 Schema 与实际协议参数"
    )
    preview.add_argument("--config", type=Path)
    preview.add_argument("--step", required=True)
    preview.add_argument(
        "--input",
        type=Path,
        required=True,
        help="本步材料与必要已有结果的 JSON；不调用模型",
    )
    simulate = commands.add_parser(
        "simulate", help="用配置和合成材料运行模拟服务，保存提示与逐步用量"
    )
    simulate.add_argument("--config", type=Path)
    simulate.add_argument("--space", type=Path, required=True)
    simulate.add_argument(
        "--paper",
        choices=["FIC-R05-A", "FIC-R05-B", "FIC-CANDIDATE-C"],
        default="FIC-R05-B",
    )
    simulate.add_argument("--request-key", required=True)
    for name, help_text in (
        ("roster", "对固定准备输入执行真实名单提取"),
        ("run", "对固定PDF执行名单和18类数值提取"),
    ):
        roster = commands.add_parser(name, help=help_text)
        roster.add_argument("--config", type=Path)
        roster.add_argument("--space", type=Path, required=True)
        roster.add_argument(
            "--preparation", required=True, help="已完成的材料准备执行编号"
        )
        roster.add_argument("--request-key", required=True)
        roster.add_argument("--env-file", type=Path)
    batch = commands.add_parser(
        "batch", help="并行运行多篇固定PDF的名单和逐对象数值提取"
    )
    batch.add_argument("--space", type=Path, required=True)
    batch.add_argument("--config", type=Path)
    batch.add_argument("--request-key")
    batch.add_argument("--env-file", type=Path)
    batch.add_argument("--concurrency", type=int)
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
    retry = commands.add_parser(
        "retry-network", help="仅重试已证实未发送的网络失败，复用成功名单和对象"
    )
    retry.add_argument("--space", type=Path, required=True)
    retry.add_argument("--execution", required=True)
    retry.add_argument("--request-key", required=True)
    retry.add_argument("--object", action="append", dest="object_ids")
    retry.add_argument("--env-file", type=Path)
    objects = commands.add_parser(
        "retry-object", help="显式重提格式失败对象，保存新代次并复用成功对象"
    )
    objects.add_argument("--space", type=Path, required=True)
    objects.add_argument("--execution", required=True)
    objects.add_argument("--request-key", required=True)
    objects.add_argument("--object", action="append", dest="object_ids", required=True)
    objects.add_argument("--env-file", type=Path)
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
            )
        else:
            from .pipeline import (
                PipelineMockService,
                get_extraction,
                import_mock_source,
                resume_extraction,
                start_roster_extraction,
            )
            from .workspace import (
                WORKSPACE_FILENAME,
                initialize_workspace,
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
                        start_roster_extraction(
                            space,
                            configuration=config,
                            preparation_execution_id=prep,
                            request_key=args.request_key + ":" + prep,
                            full=True,
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
                    "implementation": "model-pdf",
                    "executions": [r.to_dict() for r in reports],
                }
            elif name in {"roster", "run"}:
                from .llm.managed import CallService

                config = load_extraction_config(args.config)
                space = open_workspace(args.space)
                handle = start_roster_extraction(
                    space,
                    configuration=config,
                    preparation_execution_id=args.preparation,
                    request_key=args.request_key,
                    full=name == "run",
                )
                result = resume_extraction(
                    space,
                    handle.execution_id,
                    service=CallService(config.runtime, env_file=args.env_file),
                ).to_dict()
                result["implementation"] = (
                    "model-pdf" if name == "run" else "model-roster"
                )
            elif name == "simulate":
                config = load_extraction_config(args.config)
                check_extraction_config(config)
                space = (
                    open_workspace(args.space)
                    if (args.space / WORKSPACE_FILENAME).exists()
                    else initialize_workspace(args.space)
                )
                source = import_mock_source(space, args.paper)
                handle = start_configured_extraction(
                    space,
                    configuration=config,
                    source_refs=[source],
                    request_key=args.request_key,
                )
                result = resume_extraction(
                    space, handle.execution_id, service=PipelineMockService()
                ).to_dict()
            else:
                space = open_workspace(args.space)
                from .runtime import get_execution

                handle = get_execution(space, args.execution)
                if handle.input_snapshot.options.get(
                    "extraction_config"
                ) is None or not handle.flow_id.startswith(
                    ("hvs-extraction-", "hvs-roster-", "hvs-pdf-")
                ):
                    raise ValueError("this command requires a configured extraction")
                actual_roster = handle.input_snapshot.options.get(
                    "extraction_scope"
                ) in {"roster", "full"}
                if actual_roster:
                    from .llm.managed import CallService
                    from .pipeline.configuration import stored_configuration

                    service = CallService(
                        stored_configuration(handle.input_snapshot).runtime,
                        env_file=args.env_file,
                    )
                else:
                    service = PipelineMockService()
                intent = None
                if name == "retry-network":
                    from .pipeline import retry_network_failures

                    intent = retry_network_failures(
                        space,
                        args.execution,
                        request_key=args.request_key,
                        object_ids=args.object_ids,
                    )
                elif name == "retry-object":
                    from .pipeline import retry_failed_objects

                    intent = retry_failed_objects(
                        space,
                        args.execution,
                        request_key=args.request_key,
                        object_ids=args.object_ids,
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
                result["implementation"] = (
                    (
                        "model-pdf"
                        if handle.input_snapshot.options.get("extraction_scope")
                        == "full"
                        else "model-roster"
                    )
                    if actual_roster
                    else "synthetic-mock"
                )
                if intent is not None:
                    result[
                        {
                            "retry-network": "network_retry",
                            "retry-object": "object_retry",
                            "continue-format": "format_continuation",
                        }[name]
                    ] = intent
            if name == "simulate":
                result["implementation"] = "synthetic-mock"
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
