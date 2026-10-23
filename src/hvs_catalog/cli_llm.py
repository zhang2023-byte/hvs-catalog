"""CLI for model profiles, configuration and bounded synthetic connectivity probes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .llm import (
    CallConfig,
    CallRequest,
    Message,
    ModelConfig,
    Tool,
    get_call,
    list_models,
    load_config,
    load_credential,
    resume_call,
    start_call,
)
from .workspace import WorkspaceError, initialize_workspace, open_workspace


def synthetic_request(model: ModelConfig, *, structured=True) -> CallRequest:
    text = "Synthetic object SYN-1 has value 42. Return only the requested result."
    tool = (
        Tool(
            name="record_probe_result",
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "const": "SYN-1"},
                    "value": {"type": "integer", "const": 42},
                },
                "required": ["id", "value"],
                "additionalProperties": False,
            },
        )
        if structured
        else None
    )
    if tool:
        text += " Use record_probe_result exactly once."
    else:
        text += " Reply exactly: SYN-1=42."
    return CallRequest(
        model=model, messages=[Message(role="user", content=text)], tool=tool
    )


def add_model_commands(commands):
    parser = commands.add_parser("models", help="模型配置、Token Dance 合成调用与状态")
    sub = parser.add_subparsers(dest="model_command", required=True)
    sub.add_parser("list", help="列出固定模型、锁定路由、参数与来源")
    check = sub.add_parser("check", help="离线检查非敏感 JSON 配置")
    check.add_argument("--config", type=Path, required=True)
    probe = sub.add_parser("probe", help="付费真实调用：仅英文 SYN-1 合成任务")
    probe.add_argument("--config", type=Path)
    probe.add_argument("--model", choices=[p["model"] for p in list_models()])
    probe.add_argument("--space", type=Path, required=True)
    probe.add_argument("--request-key", required=True)
    probe.add_argument("--env-file", type=Path)
    probe.add_argument("--quota-store", type=Path)
    probe.add_argument("--plain", action="store_true")
    probe.add_argument("--non-stream", action="store_true")
    probe.add_argument(
        "--max-output", type=int, help="验证用缩小上限；省略使用生产配置"
    )
    for name in ("status", "resume"):
        p = sub.add_parser(
            name,
            help="查询调用" if name == "status" else "按固定输入恢复；未知完成不重发",
        )
        p.add_argument("--space", type=Path, required=True)
        p.add_argument("--execution", required=True)
        if name == "resume":
            p.add_argument("--env-file", type=Path)


def run_model_command(args):
    space = None
    try:
        if args.model_command == "list":
            result = list_models()
        elif args.model_command == "check":
            result = load_config(args.config).model_dump(mode="json")
        elif args.model_command in ("status", "resume"):
            space = open_workspace(args.space)
            result = (
                get_call(space, args.execution)
                if args.model_command == "status"
                else resume_call(space, args.execution, env_file=args.env_file)
            )
        else:
            if bool(args.config) == bool(args.model):
                raise ValueError("specify exactly one of --config and --model")
            config = (
                load_config(args.config)
                if args.config
                else CallConfig(model=ModelConfig(model=args.model))
            )
            data = config.model_dump(mode="python")
            if args.quota_store:
                data["runtime"]["quota_store"] = args.quota_store
            if args.non_stream:
                data["model"]["stream"] = False
            if args.max_output is not None:
                from .llm.config import PROFILES

                data["model"]["parameters"][
                    PROFILES[config.model.model].output_parameter
                ] = args.max_output
            config = CallConfig.model_validate(data)
            credential = load_credential(config.runtime, env_file=args.env_file)
            request = synthetic_request(config.model, structured=not args.plain)
            space = initialize_workspace(args.space)
            handle = start_call(
                space,
                request,
                config.runtime,
                request_key=args.request_key,
                credential=credential,
            )
            result = resume_call(space, handle.execution_id, credential=credential)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if isinstance(result, dict) and result.get("state") == "failed" else 0
    except (ValueError, OSError, WorkspaceError) as exc:
        print(f"model operation refused: {type(exc).__name__}", file=sys.stderr)
        return 2
    finally:
        if space is not None:
            space.close()
