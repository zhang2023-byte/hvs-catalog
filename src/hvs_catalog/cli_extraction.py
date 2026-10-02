"""Offline extraction configuration, complete request previews and mock runs."""

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
    for name in ("status", "resume"):
        command = commands.add_parser(
            name, help="查询模拟执行" if name == "status" else "按固定输入恢复模拟执行"
        )
        command.add_argument("--space", type=Path, required=True)
        command.add_argument("--execution", required=True)


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
            )
            from .workspace import (
                WORKSPACE_FILENAME,
                initialize_workspace,
                open_workspace,
            )

            if name == "simulate":
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
                ) is None or not handle.flow_id.startswith("hvs-extraction-"):
                    raise ValueError(
                        "this command requires a configured synthetic-mock extraction"
                    )
                result = (
                    get_extraction(space, args.execution)
                    if name == "status"
                    else resume_extraction(
                        space, args.execution, service=PipelineMockService()
                    ).to_dict()
                )
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
