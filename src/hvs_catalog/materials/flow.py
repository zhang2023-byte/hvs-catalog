"""材料准备流程：固定输入下的展开、正文、表格、范围与收尾。

复用 ``runtime`` 的执行／检查点／恢复机制（``DefaultFlowAdapter``），
不自建状态机。步骤按表隔离失败：单个表转换失败记入缺口，不阻断其余
材料。歧义编号范围在开启专家裁决时经 R05 共同接口提问并中断等待；
决定只应用一次（运行层回执保证），未决范围不当作完整名单。
"""

from __future__ import annotations

import json
import re
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..records.common import ArtifactRef, ItemRef, RecordRef, sha256_of
from ..records.questions import RulesRef
from ..records.source import AffectedTargets, Gap, SourceFile
from ..review import ReviewService
from ..review.service import QuestionContent
from ..runtime import (
    ApplicationPlan,
    DefaultFlowAdapter,
    RuntimeContext,
    append_run_revision,
    commit_step_result,
)
from ..runtime.execution import find_step_result
from ..workspace import CommitRequest, WorkspaceIntegrityError
from . import convert as conversion
from . import tables as table_scan
from .cleaning import render_manuscript_view
from .expansion import MaterialExpansionError, expand_source_package
from .ranges import expand_range_notation
from .sandbox import ConverterRunner, SandboxedConverterRunner
from .texgraph import resolve_manuscript

FLOW_ID = "hvs-materials-prepare"
FLOW_VERSION = "2"

STEP_EXPAND = "expand_source"
STEP_BODY = "organize_body"
STEP_SCAN = "scan_tables"
STEP_CONVERT = "convert_tables"
STEP_RANGES = "resolve_ranges"
STEP_FINALIZE = "finalize"

STEPS = (
    STEP_EXPAND,
    STEP_BODY,
    STEP_SCAN,
    STEP_CONVERT,
    STEP_RANGES,
    STEP_FINALIZE,
)

#: 决定答复可给出的"非对象编号"选项代码；其余代码来自请求候选解释。
RANGE_OPTION_NOT_OBJECTS = "not-objects"

_UNPARSED_MEDIA_SUFFIXES = (
    ".pdf",
    ".fits",
    ".png",
    ".jpg",
    ".jpeg",
    ".eps",
    ".ps",
    ".gif",
    ".dat",
)


class MaterialsState(TypedDict, total=False):
    """准备流程的检查点状态（纯 JSON 可序列化）。"""

    execution_id: str
    request: dict
    members: list[dict]
    body: dict
    table_blocks: list[dict]
    conversions: list[dict]
    gaps: list[dict]
    ranges: list[dict]
    prepared_source_ref: dict
    conversion_diagnostics: list[dict]
    native_range_observations: list[dict]
    bundle: dict
    summary: dict
    raw_sha256: str
    package_format: str


def parser_fingerprint() -> str:
    """Freeze shipped parser code, shared reader and the native observer."""
    import hashlib
    from pathlib import Path

    package = Path(__file__).resolve().parent.parent
    paths = sorted((package / "materials").glob("*.py"))
    paths.extend(
        [(package / "materials/source_trace.ltxml"), package / "records/tabular.py"]
    )
    return sha256_of(
        {
            path.relative_to(package).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in paths
        }
    )


def _ref(payload: dict) -> RecordRef:
    return RecordRef.model_validate(payload)


_EXTERNAL_RESOURCE_RE = re.compile(
    r"\\(?:url|href)\s*\{(https?://[^{}]+)\}", re.IGNORECASE
)


def _derived_name(kind: str, digest: str, suffix: str) -> str:
    """派生文件的确定性唯一名：内容寻址，不依赖可碰撞的扁平路径名。"""
    return f"outputs/{kind}/{digest[:16]}{suffix}"


class MaterialsFlow(DefaultFlowAdapter):
    """一篇论文材料的确定性准备流程；``runner`` 可注入假转换器。"""

    flow_id = FLOW_ID
    flow_version = FLOW_VERSION
    mode = "daily"

    def __init__(
        self,
        *,
        runner: ConverterRunner | None = None,
        fault_hooks: dict[str, Any] | None = None,
    ):
        self.runner = runner
        self.fault_hooks = fault_hooks or {}

    def hook(self, name: str) -> None:
        callback = self.fault_hooks.get(name)
        if callback is not None:
            callback()

    # ---- 流程定义 ----

    def initial_state(self, ctx: RuntimeContext) -> dict:
        request = self.request_payload(ctx)
        return {
            "execution_id": ctx.execution.execution_id,
            "request": request,
            "members": [],
            "body": {},
            "table_blocks": [],
            "conversions": [],
            "gaps": [],
            "ranges": [],
        }

    @staticmethod
    def request_payload(ctx: RuntimeContext) -> dict:
        raw = ctx.execution.input_snapshot.options.get("materials_request")
        if not raw:
            raise WorkspaceIntegrityError("执行缺少固定的材料准备请求选项")
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise WorkspaceIntegrityError("固定的材料准备请求无法解析") from exc
        if not isinstance(payload, dict):
            raise WorkspaceIntegrityError("固定的材料准备请求结构非法")
        if payload.get("parser_fingerprint") != parser_fingerprint():
            raise WorkspaceIntegrityError(
                "材料解析代码／观测绑定与固定输入不符；须建立新执行"
            )
        return payload

    def build_graph(self, ctx: RuntimeContext):
        builder = StateGraph(MaterialsState)
        handlers = {
            STEP_EXPAND: self._expand_source,
            STEP_BODY: self._organize_body,
            STEP_SCAN: self._scan_tables,
            STEP_CONVERT: self._convert_tables,
            STEP_RANGES: self._resolve_ranges,
            STEP_FINALIZE: self._finalize,
        }
        previous = START
        for step_id in STEPS:
            handler = handlers[step_id]

            def node(state: dict, _handler=handler, _ctx=ctx) -> dict:
                return _handler(_ctx, state)

            builder.add_node(step_id, node)
            builder.add_edge(previous, step_id)
            previous = step_id
        builder.add_edge(previous, END)
        return builder.compile(checkpointer=ctx.checkpointer)

    # ---- 通用工具 ----

    def _step_digest(self, ctx: RuntimeContext, step_id: str, extra: dict) -> str:
        return sha256_of(
            {
                "execution_id": ctx.execution.execution_id,
                "step": step_id,
                "extra": extra,
            }
        )

    def _publish(self, ctx: RuntimeContext, rel: str, data: bytes) -> ArtifactRef:
        return ctx.space.publish_run_file(ctx.execution.execution_id, rel, data)

    # ---- 步骤：展开来源包 ----

    def _expand_source(self, ctx: RuntimeContext, state: dict) -> dict:
        digest = self._step_digest(ctx, STEP_EXPAND, {"input": state["request"]})
        cached = find_step_result(ctx, STEP_EXPAND, digest)
        if cached is not None and state.get("members"):
            return {}
        source_ref = _ref(state["request"]["source_ref"])
        record = ctx.space.store().read_record(source_ref)
        if record is None:
            raise WorkspaceIntegrityError(
                f"固定来源记录缺失：{source_ref.record_id} v{source_ref.revision}"
            )
        raw_entry = next(
            (f for f in record.data.files if f.role == "raw_package"), None
        )
        if raw_entry is None:
            raise WorkspaceIntegrityError("来源记录缺少原包文件条目")
        payload = ctx.space.read_file(
            ArtifactRef(path=raw_entry.path, sha256=raw_entry.sha256)
        )
        try:
            expanded = expand_source_package(
                ctx.space,
                payload,
                fixed_encoding=(
                    state["request"].get("member_encoding")
                    if state["request"].get("member_encoding") in ("utf-8", "latin-1")
                    else None
                ),
            )
        except MaterialExpansionError as exc:
            append_run_revision(
                ctx.space,
                ctx.execution,
                state="failed",
                errors=[f"来源包展开失败：{exc}"],
            )
            raise
        members: list[dict] = []
        gaps: list[dict] = []
        for member in expanded.members:
            entry = {
                "name": member.name,
                "sha256": member.sha256,
                "size": member.size,
                "media_type": member.media_type,
                "encoding": member.encoding,
                "text": member.text,
                "derived_sha256": member.derived_sha256,
                # Checkpoint backup must retain raw members before finalization.
                "raw_ref": {
                    "path": f"sources/materials/members/{member.sha256}.bin",
                    "sha256": member.sha256,
                },
            }
            if member.decode_note:
                # 成功编码转换（latin-1 显式派生）的说明留在元数据中，不
                # 是不可读事实；只有解码未成功（无文本、无派生）才登记
                # 不可读缺口（二次审核反例 latin-ready）。
                entry["decode_note"] = member.decode_note
                if member.text is None and member.derived_text is None:
                    gaps.append(
                        {
                            "gap_id": f"gap-member-{len(gaps) + 1:03d}",
                            "reason": "unreadable",
                            "description": f"{member.name}: {member.decode_note}",
                            "file": member.name,
                        }
                    )
            if member.derived_text is not None:
                published = self._publish(
                    ctx,
                    _derived_name("derived", member.derived_sha256, ".utf8.txt"),
                    member.derived_text.encode("utf-8"),
                )
                entry["derived_path"] = published.path
                entry["derived_published_sha256"] = published.sha256
                # 标准形状引用：检查点备份据此收集文件
                entry["derived_ref"] = {
                    "path": published.path,
                    "sha256": published.sha256,
                }
            members.append(entry)
        commit_step_result(
            ctx.space,
            ctx.execution,
            step_id=STEP_EXPAND,
            input_digest=digest,
            records=[],
            result_refs=[],
        )
        return {
            "members": members,
            "gaps": [
                *[
                    dict(
                        gap.model_dump(mode="json"),
                        file=(gap.file_id or "").removeprefix("member:"),
                    )
                    for gap in record.data.gaps
                ],
                *gaps,
            ],
            "raw_sha256": expanded.raw_sha256,
            "package_format": expanded.package_format,
        }

    # ---- 步骤：组织正文 ----

    def _organize_body(self, ctx: RuntimeContext, state: dict) -> dict:
        digest = self._step_digest(ctx, STEP_BODY, {"members": len(state["members"])})
        cached = find_step_result(ctx, STEP_BODY, digest)
        if cached is not None and state.get("body"):
            return {}
        texts = {
            m["name"]: m["text"] for m in state["members"] if m.get("text") is not None
        }
        hashes = {m["name"]: m["sha256"] for m in state["members"]}
        encodings = {m["name"]: m.get("encoding") for m in state["members"]}
        request = state["request"]
        graph = resolve_manuscript(
            texts,
            member_hashes=hashes,
            member_encodings=encodings,
            root_file=request.get("root_file"),
        )
        body_texts: dict[str, str] = {
            entry.name: entry.cleaned_text for entry in graph.files
        }
        body: dict = {
            "status": graph.status,
            "reason": graph.reason,
            "root_candidates": list(graph.root_candidates),
            "root": graph.root,
            "problems": [
                {
                    "code": p.code,
                    "file": p.file,
                    "target": p.target,
                    "detail": p.detail,
                }
                for p in graph.problems
            ],
            "notes": list(graph.notes),
            "non_tex_includes": [
                {"from": a, "target": b} for a, b in graph.non_tex_includes
            ],
            "excluded": list(graph.excluded),
            "files": [],
        }
        if graph.status == "organized":
            blocks: list[tuple[str, str]] = []
            for entry in graph.files:
                published = self._publish(
                    ctx,
                    _derived_name("body", entry.sha256, ".tex"),
                    entry.cleaned_text.encode("utf-8"),
                )
                body["files"].append(
                    {
                        "name": entry.name,
                        "sha256": entry.sha256,
                        "line_count": entry.line_count,
                        "encoding": entry.encoding,
                        "included_from": entry.included_from,
                        "included_at_line": entry.included_at_line,
                        "included_at_offset": entry.included_at_offset,
                        "cleaned_path": published.path,
                        "cleaned_sha256": published.sha256,
                        "cleaned_ref": {
                            "path": published.path,
                            "sha256": published.sha256,
                        },
                    }
                )
                blocks.append((entry.name, entry.cleaned_text))
            view = self._publish(
                ctx,
                "outputs/body-view.txt",
                render_manuscript_view(blocks).encode("utf-8"),
            )
            body["view_path"] = view.path
            body["view_sha256"] = view.sha256
            body["view_ref"] = {"path": view.path, "sha256": view.sha256}
            body["edges"] = {key: list(value) for key, value in graph.edges.items()}
        # 明确的包外数据资源提及：保留位置与提及，不下载、不推断科学含
        # 义；影响未知，作为缺口随输入交付（初审反例：链接保留为正文但
        # gaps 为空）。
        for name, text in body_texts.items():
            for match in _EXTERNAL_RESOURCE_RE.finditer(text):
                url = match.group(1)
                mention = text[max(0, match.start() - 160) : match.end() + 80]
                if not (
                    re.search(
                        r"\b(?:data|catalogue|catalog|table|supplement|resource)\b",
                        mention,
                        re.IGNORECASE,
                    )
                    or re.search(
                        r"\.(?:csv|ecsv|dat|fits|json|h5|pdf|zip|gz)(?:[?#]|$)",
                        url,
                        re.IGNORECASE,
                    )
                ):
                    continue
                line = text.count("\n", 0, match.start()) + 1
                state["gaps"].append(
                    {
                        "gap_id": f"gap-external-{len(state['gaps']) + 1:03d}",
                        "reason": "unparsed",
                        "description": (
                            f"包外数据资源提及：{url}（{name} 第 {line} 行）；"
                            "材料准备不下载包外资源，影响未知，保留原文与位置"
                        ),
                        "file": name,
                    }
                )
        reason_map = {
            "missing_included_tex": "missing",
            "cyclic_tex_include": "missing",
            "include_path_outside_package": "missing",
            "unsupported_include_directive": "unparsed",
            "conditional_tex_include_ambiguous": "unparsed",
            "repeated_tex_include": "unparsed",
        }
        for problem in graph.problems:
            state["gaps"].append(
                {
                    "gap_id": f"gap-include-{len(state['gaps']) + 1:03d}",
                    "reason": reason_map.get(problem.code, "unparsed"),
                    "description": problem.describe(),
                    "file": problem.file,
                }
            )
        if graph.status != "organized":
            reason_text = {
                "no_root_tex": "包内没有可识别的主稿（无 documentclass 与 begin{document}）",
                "multiple_root_tex_candidates": "存在多个有效主稿，必须显式选择 root_file："
                + ", ".join(graph.root_candidates),
                "selected_root_not_manuscript_root": "指定的 root_file 不是有效主稿",
            }.get(graph.reason or "", str(graph.reason))
            state["gaps"].append(
                {
                    "gap_id": f"gap-body-{len(state['gaps']) + 1:03d}",
                    "reason": "missing",
                    "description": f"正文不可用：{reason_text}",
                    "file": None,
                }
            )
        commit_step_result(
            ctx.space,
            ctx.execution,
            step_id=STEP_BODY,
            input_digest=digest,
            records=[],
            result_refs=[],
        )
        return {"body": body, "gaps": list(state["gaps"])}

    # ---- 步骤：扫描表块 ----

    def _scan_tables(self, ctx: RuntimeContext, state: dict) -> dict:
        digest = self._step_digest(
            ctx, STEP_SCAN, {"root": state.get("body", {}).get("root")}
        )
        cached = find_step_result(ctx, STEP_SCAN, digest)
        if cached is not None and state.get("table_blocks"):
            return {}
        blocks: list[dict] = []
        if state.get("body", {}).get("status") == "organized":
            cleaned_entries = []
            for entry in state["body"]["files"]:
                text = ctx.space.read_file(
                    ArtifactRef(
                        path=entry["cleaned_path"], sha256=entry["cleaned_sha256"]
                    )
                ).decode("utf-8")
                cleaned_entries.append(_ScanEntry(entry["name"], text))
            for block in table_scan.scan_tables(_ScanGraph(cleaned_entries)):
                blocks.append(
                    {
                        "table_id": block.table_id,
                        "file": block.file,
                        "environment": block.environment,
                        "start_line": block.start_line,
                        "end_line": block.end_line,
                        "content_start_line": block.content_start_line,
                        "start_offset": block.start_offset,
                        "end_offset": block.end_offset,
                        "content_start_offset": block.content_start_offset,
                        "content_end_offset": block.content_end_offset,
                        "label": block.label,
                        "caption": block.caption,
                        "footnotes": [
                            {"mark": mark, "text": note}
                            for mark, note in block.footnotes
                        ],
                        "notes": list(block.notes),
                        "typeset": (
                            {
                                "file": block.pgf_typeset.file,
                                "line": block.pgf_typeset.line,
                                "options": block.pgf_typeset.options,
                                "source_arg": block.pgf_typeset.source_arg,
                                "macro": block.pgf_typeset.macro,
                                "offset": block.pgf_typeset.offset,
                            }
                            if block.pgf_typeset is not None
                            else None
                        ),
                    }
                )
        commit_step_result(
            ctx.space,
            ctx.execution,
            step_id=STEP_SCAN,
            input_digest=digest,
            records=[],
            result_refs=[],
        )
        return {"table_blocks": blocks}

    # ---- 步骤：转换表块 ----

    def _convert_tables(self, ctx: RuntimeContext, state: dict) -> dict:
        digest = self._step_digest(
            ctx,
            STEP_CONVERT,
            {"blocks": [b["table_id"] for b in state.get("table_blocks", [])]},
        )
        cached = find_step_result(ctx, STEP_CONVERT, digest)
        if cached is not None and state.get("conversions"):
            return {}
        self.hook("before_convert")

        def read_member(name: str) -> bytes | None:
            member = next((m for m in state["members"] if m["name"] == name), None)
            if member is None:
                return None
            return ctx.space.read_file(
                ArtifactRef(
                    path=f"sources/materials/members/{member['sha256']}.bin",
                    sha256=member["sha256"],
                )
            )

        def publish(rel: str, data: bytes) -> ArtifactRef:
            return self._publish(ctx, rel, data)

        outcome = convert_all_tables(
            state,
            publish=publish,
            read_member=read_member,
            runner=self.runner,
        )
        conversions = outcome["tables"]
        for gap in outcome["gaps"]:
            state["gaps"].append(gap)
        for member in state["members"]:
            name = member["name"].lower()
            if name.endswith((".csv", ".ecsv")):
                # 包内独立 CSV／ECSV 附表：接入共享读取器准备（发现 11）。
                continue
            if member.get("text") is None and not member.get("derived_path"):
                # 实际未解析的附带材料：不以固定后缀清单筛选，未支持格
                # 式一律登记未处理范围与未知影响（二次审核反例 h5-gap）。
                state["gaps"].append(
                    {
                        "gap_id": f"gap-material-{len(state['gaps']) + 1:03d}",
                        "reason": "unparsed",
                        "description": (
                            f"{member['name']}（{member['media_type']}）是未支持"
                            "的附带材料格式，未做解析；保留文件与提及，影响未知"
                        ),
                        "file": member["name"],
                    }
                )
        attached = _prepare_attached_tables(
            state, publish=publish, read_member=read_member
        )
        conversions = [*conversions, *attached["tables"]]
        for gap in attached["gaps"]:
            state["gaps"].append(gap)
        # 准备来源修订在范围状态明确后（收尾步骤）提交，全部缺口随最终
        # 状态进入 prepared_source_ref（二次审核发现 14）。
        commit_step_result(
            ctx.space,
            ctx.execution,
            step_id=STEP_CONVERT,
            input_digest=digest,
            records=[],
            result_refs=[],
        )
        return {
            "conversions": conversions,
            "conversion_diagnostics": outcome.get("diagnostics", []),
            "native_range_observations": outcome.get("range_observations", []),
            "gaps": list(state["gaps"]),
        }

    # ---- 步骤：编号范围 ----

    def _resolve_ranges(self, ctx: RuntimeContext, state: dict) -> dict:
        request = state["request"]
        registered = list(request.get("range_notations") or [])
        expert = bool(request.get("expert_adjudication"))
        review = ReviewService(ctx.space)
        body_texts: dict[str, str] = {}
        for entry in state.get("body", {}).get("files", []):
            try:
                body_texts[entry["name"]] = ctx.space.read_file(
                    ArtifactRef(
                        path=entry["cleaned_path"], sha256=entry["cleaned_sha256"]
                    )
                ).decode("utf-8")
            except (KeyError, WorkspaceIntegrityError):  # pragma: no cover
                continue
        results: list[dict] = []
        registered_spans: list[tuple[str, int, int, str]] = []
        for item in registered:
            notation = str(item.get("notation") or "")
            entry = {
                "origin": "registered",
                "notation": notation,
                "file": item.get("file"),
                "start_line": item.get("start_line"),
                "end_line": item.get("end_line"),
                "status": "ambiguous",
                "identifiers": [],
                "rule": "",
                "remainder": "",
                "reason": "",
            }
            verification = _verify_registered_notation(body_texts, item, notation)
            if not verification.ok:
                entry["status"] = "invalid-registration"
                entry["reason"] = verification.reason
                state["gaps"].append(
                    {
                        "gap_id": f"gap-range-{len(state['gaps']) + 1:03d}",
                        "reason": "missing",
                        "description": (
                            f"显式登记的范围记法出处无效：{notation}"
                            f"（{item.get('file')} 第 {item.get('start_line')}"
                            f"-{item.get('end_line')} 行）：{verification.reason}"
                        ),
                        "file": item.get("file"),
                    }
                )
                results.append(entry)
                registered_spans.append(
                    (
                        str(item.get("file") or ""),
                        int(item.get("start_line") or 0),
                        int(item.get("end_line") or 0),
                        notation,
                    )
                )
                continue
            registered_spans.append(
                (
                    str(item.get("file") or ""),
                    int(item.get("start_line") or 0),
                    int(item.get("end_line") or 0),
                    notation,
                )
            )
            expansion = expand_range_notation(
                notation, object_context=verification.object_context
            )
            entry.update(
                {
                    "status": expansion.status,
                    "identifiers": list(expansion.identifiers),
                    "rule": expansion.rule,
                    "remainder": expansion.remainder,
                    "reason": expansion.reason,
                }
            )
            if expansion.remainder:
                state["gaps"].append(
                    {
                        "gap_id": f"gap-range-remainder-{len(state['gaps']) + 1}",
                        "reason": "unparsed",
                        "description": f"{notation} 的余项 {expansion.remainder} 未列清；仅保留已知成员",
                        "file": item.get("file"),
                    }
                )
            if expansion.status == "unsupported":
                # 显式登记的不支持形式同样保留缺口：来源修订与公开输入
                # 不能只有结果没有未知影响（二次审核发现 14）。
                state["gaps"].append(
                    {
                        "gap_id": f"gap-range-{len(state['gaps']) + 1:03d}",
                        "reason": "unparsed",
                        "description": (
                            f"登记的范围记法 {notation} 不能确定性展开"
                            f"（{item.get('file')} 第 {item.get('start_line')}"
                            f"-{item.get('end_line')} 行）："
                            f"{expansion.reason or '技术不支持'}，保留原文与位置"
                        ),
                        "file": item.get("file"),
                    }
                )
                results.append(entry)
                continue
            if expansion.status != "ambiguous":
                results.append(entry)
                continue
            candidates = list(item.get("candidates") or [])
            if not expert or not candidates:
                entry["reason"] = (
                    expansion.reason or "含义不明"
                ) + "；未开启专家裁决或未提供可应用的候选解释，保持未决"
                state["gaps"].append(
                    {
                        "gap_id": f"gap-range-{len(state['gaps']) + 1:03d}",
                        "reason": "unparsed",
                        "description": (
                            f"范围记法 {notation} 保持未决"
                            f"（{item.get('file')} 第 {item.get('start_line')}"
                            f"-{item.get('end_line')} 行）：不能确定对象成员，"
                            "不当作完整名单"
                        ),
                        "file": item.get("file"),
                    }
                )
                results.append(entry)
                continue
            question_ref = self._range_question(
                ctx, review, item, entry, candidates, state
            )
            self._await_range_decision(review, question_ref)
            entry.update(
                _range_decision_result(review, question_ref, candidates, notation)
            )
            entry["question_ref"] = question_ref.model_dump(mode="json")
            if entry["status"] not in {"expert-resolved", "not-objects"}:
                state["gaps"].append(
                    {
                        "gap_id": f"gap-range-decision-{len(state['gaps']) + 1}",
                        "reason": "unparsed",
                        "description": f"{notation} 的决定不能确定完整对象成员：{entry['reason']}",
                        "file": item.get("file"),
                    }
                )
            results.append(entry)
        # 有界发现：正文中已批准的明确对象编号记法（稳定前缀＋数字区间）。
        # 不是通用语义识别；发现结果登记实际原文位置，显式登记优先。
        for item in _discover_range_notations(body_texts, exclude=registered_spans):
            notation = item["notation"]
            expansion = expand_range_notation(
                notation, object_context=item["object_context"]
            )
            results.append(
                {
                    "origin": "discovered",
                    **item,
                    "status": expansion.status,
                    "identifiers": list(expansion.identifiers),
                    "rule": expansion.rule,
                    "remainder": expansion.remainder,
                    "reason": expansion.reason,
                }
            )
            if expansion.status == "expanded" and not expansion.remainder:
                continue
            state["gaps"].append(
                {
                    "gap_id": f"gap-range-{len(state['gaps']) + 1:03d}",
                    "reason": "unparsed",
                    "description": (
                        f"正文发现的对象编号记法 {notation} 不能确定性展开"
                        f"（{item['file']} 第 {item['start_line']} 行）："
                        f"{expansion.reason or '存在未列清余项'}，保留完整原文与位置"
                    ),
                    "file": item["file"],
                }
            )

        # A generated range visible in the actual converted body must not vanish
        # merely because the original source is a macro invocation. No fabricated
        # exact TeX span or guessed membership is supplied for that unsupported map.
        def notation_key(value):
            return re.sub(r"\s+", "", re.sub(r"--|[\u2013\u2014]", "-", value))

        for observation in state.get("native_range_observations", []):
            if any(
                notation_key(item["notation"]) == notation_key(observation["notation"])
                for item in results
            ):
                continue
            origin = observation.get("native_origin") or {}
            results.append(
                {
                    "origin": "native-rendered",
                    "notation": observation["notation"],
                    "file": origin.get("file"),
                    "native_origin": origin or None,
                    "status": "unsupported",
                    "identifiers": [],
                    "reason": "实际显示的范围没有可核验的原文字面记法／精确跨度；保留转换与未知影响",
                }
            )
            state["gaps"].append(
                {
                    "gap_id": f"gap-native-range-{len(state['gaps']) + 1}",
                    "reason": "unparsed",
                    "description": f"显示正文范围 {observation['notation']} 尚不能证明完整原件对应，保留未列清范围",
                    "file": origin.get("file"),
                }
            )
        commit_step_result(
            ctx.space,
            ctx.execution,
            step_id=STEP_RANGES,
            input_digest=self._step_digest(ctx, STEP_RANGES, {"notations": registered}),
            records=[],
            result_refs=[],
        )
        return {"ranges": results, "gaps": list(state["gaps"])}

    def _range_question(
        self,
        ctx: RuntimeContext,
        review: ReviewService,
        item: dict,
        entry: dict,
        candidates: list[dict],
        state: dict,
    ) -> RecordRef:
        snapshot = ctx.execution.input_snapshot
        source_ref = _ref(state["request"]["source_ref"])
        target = (
            _ref(state["prepared_source_ref"])
            if state.get("prepared_source_ref")
            else source_ref
        )
        lines = [
            "论文正文出现编号范围记法，程序无法确定其含义：",
            f"记法：{entry['notation']}",
            f"出处：{item.get('file')} 第 {item.get('start_line')}-{item.get('end_line')} 行",
            "",
            "候选解释（答复必须精确给出代码，不接受自由文本推断）：",
        ]
        for candidate in candidates:
            preview = candidate_preview(candidate, entry["notation"])
            suffix = f"（程序展开预览：{', '.join(preview)}）" if preview else ""
            lines.append(
                f"- {candidate.get('code')}: {candidate.get('description', '')}{suffix}"
            )
        lines.append(
            f"- {RANGE_OPTION_NOT_OBJECTS}: 该记法不是对象编号范围"
            "（如物理量区间或坐标符号），不展开成员"
        )
        content = QuestionContent(
            category="interpretation",
            target_refs=[
                ItemRef(
                    record_id=target.record_id,
                    revision=target.revision,
                    item_id=(
                        f"range:{item.get('file')}:{item.get('start_line')}:"
                        f"{entry['notation']}"
                    ),
                )
            ],
            source_refs=[source_ref],
            rules_ref=RulesRef(
                version=snapshot.rules.version, sha256=snapshot.rules.sha256
            ),
            prompt="\n".join(lines),
        )
        digest = sha256_of(
            {
                "notation": entry["notation"],
                "file": item.get("file"),
                "start_line": item.get("start_line"),
                "end_line": item.get("end_line"),
                "candidates": [
                    {
                        "code": str(c.get("code") or ""),
                        "kind": str(c.get("kind") or ""),
                        "prefix": str(c.get("prefix") or ""),
                    }
                    for c in candidates
                ],
            }
        )[:16]
        view = review.create_question(
            execution_id=ctx.execution.execution_id,
            step_id=STEP_RANGES,
            request_key=f"materials-range:{ctx.execution.execution_id}:{digest}",
            content=content,
        )
        return view.question_ref

    def _await_range_decision(
        self, review: ReviewService, question_ref: RecordRef
    ) -> None:
        while True:
            view = review.get_question(question_ref)
            if view.application is not None and view.application.phase == "applied":
                break
            interrupt(
                {
                    "question_ref": question_ref.model_dump(mode="json"),
                    "step_id": STEP_RANGES,
                    "prompt": view.record.data.prompt,
                }
            )

    def apply_decision(
        self, ctx: RuntimeContext, decision, question
    ) -> ApplicationPlan | None:
        if question.data.category != "interpretation":
            return None
        # 材料准备的决定不产生科学记录修订；应用回执由运行层写入，范围
        # 结果在步骤重入时由已应用决定确定性重建（不从自由文本推断）。
        return ApplicationPlan(
            records=[],
            expected_revisions={},
            result_refs=[],
            notes={"materials_range_decision": True},
        )

    def verify_state(self, ctx: RuntimeContext, state: dict) -> list[str]:
        problems: list[str] = []
        if state.get("request", {}).get("parser_fingerprint") != parser_fingerprint():
            problems.append("材料解析代码／观测绑定与固定输入不符；不混用新旧结果")
        bound = state.get("execution_id")
        if bound is not None and bound != ctx.execution.execution_id:
            problems.append(
                f"检查点状态绑定执行 {bound}，当前执行为 {ctx.execution.execution_id}"
            )
        return problems

    def waiting_step(self, interrupt_payload: dict) -> str | None:
        step_id = interrupt_payload.get("step_id")
        return str(step_id) if step_id else None

    # ---- 步骤：收尾 ----

    def _finalize(self, ctx: RuntimeContext, state: dict) -> dict:
        digest = self._step_digest(
            ctx, STEP_FINALIZE, {"tables": len(state.get("conversions", []))}
        )
        cached = find_step_result(ctx, STEP_FINALIZE, digest)
        if cached is not None and state.get("bundle"):
            return {}
        # One finalized collection is used by revision, bundle and every view.
        normalized, used = [], {}
        for original in state["gaps"]:
            gap = dict(original)
            base = str(gap["gap_id"])
            if base in used and used[base] == gap:
                continue
            suffix = 2
            while gap["gap_id"] in used:
                gap["gap_id"] = f"{base}#{suffix}"
                suffix += 1
            used[gap["gap_id"]] = gap
            normalized.append(gap)
        state["gaps"] = normalized
        record_ref = _commit_prepared_revision(
            ctx, state, conversions=state.get("conversions", [])
        )
        self.hook("after_revision_commit")
        state["prepared_source_ref"] = record_ref.model_dump(mode="json")
        bundle = build_bundle(state)
        payload = json.dumps(
            bundle, ensure_ascii=False, sort_keys=True, indent=2
        ).encode("utf-8")
        published = self._publish(ctx, "outputs/prepared-input.json", payload)
        commit_step_result(
            ctx.space,
            ctx.execution,
            step_id=STEP_FINALIZE,
            input_digest=digest,
            records=[],
            result_refs=[record_ref],
        )
        return {
            "bundle": {"path": published.path, "sha256": published.sha256},
            "prepared_source_ref": state["prepared_source_ref"],
            "summary": bundle["summary"],
            "gaps": normalized,
        }


class _ScanEntry:
    __slots__ = ("cleaned_text", "name")

    def __init__(self, name: str, cleaned_text: str) -> None:
        self.name = name
        self.cleaned_text = cleaned_text


class _ScanGraph:
    __slots__ = ("files",)

    def __init__(self, files: list[_ScanEntry]) -> None:
        self.files = files


# ---- 表块转换（按表隔离失败） ----


def convert_all_tables(
    state: dict,
    *,
    publish,
    read_member,
    runner: ConverterRunner | None = None,
) -> dict:
    """转换全部表块；按表隔离失败并产出缺口。"""
    from .document import convert_document

    resolved_runner = runner or SandboxedConverterRunner()
    availability = {
        tool: resolved_runner.availability(tool) for tool in ("latexmlc", "pandoc")
    }
    _verify_frozen_converters(state, availability)
    texts = {
        m["name"]: m["text"] for m in state["members"] if m.get("text") is not None
    }
    body_files = state.get("body", {}).get("files", [])
    order = [entry["name"] for entry in body_files]
    includes = {
        entry["name"]: (
            entry.get("included_from"),
            entry.get("included_at_line"),
            entry.get("included_at_offset"),
        )
        for entry in body_files
    }
    native = convert_document(
        state, publish=publish, runner=resolved_runner, availability=availability
    )
    results, gaps = [], []
    for record in native["tables"]:
        parsed = record.pop("parsed", None)
        if parsed is not None:
            record = _publish_success(
                publish, record, parsed, f"outputs/tables/{record['table_id']}"
            )
        results.append(record)
    for block in state.get("table_blocks", []):
        if block["table_id"] in native["observed"]:
            continue
        record = dict(
            block,
            status="failed",
            method="",
            error="原表没有可证明的执行/转换对应",
            warnings=[],
            diagnostic_refs=native["diagnostics"],
            attempts=[],
            mapping={"granularity": "table-block"},
        )
        if block["environment"] == "pgfplotstable":
            typeset = table_scan.PgfTypeset(**block["typeset"])
            parsed, attempt, _ = conversion.convert_pgf_backing_csv(
                typeset,
                manuscript_texts=texts,
                document_order=order,
                read_member=read_member,
                referring_file=block["file"],
                include_positions=includes,
            )
            record["attempts"] = [
                {
                    "method": attempt.method,
                    "status": parsed.status,
                    "error": parsed.error,
                }
            ]
            record["error"] = parsed.error
        elif not availability["latexmlc"].available:
            # A literal raw presentation is useful without an external tool;
            # it cannot override an actual failed/unknown native conversion.
            excerpt = texts[block["file"]][
                block["content_start_offset"] : block["content_end_offset"]
            ]
            parsed = conversion.parse_latex_table_excerpt(excerpt)
            record["attempts"] = [
                {"method": "latexml", "status": "skipped", "error": native["error"]},
                {"method": "internal", "status": parsed.status, "error": parsed.error},
            ]
            record["error"] = parsed.error
            if parsed.status == "success" and availability["pandoc"].available:
                rendered, refs = _pandoc_literal_fallback(
                    excerpt,
                    block=block,
                    runner=resolved_runner,
                    available=availability["pandoc"],
                    publish=publish,
                )
                record["diagnostic_refs"] = [*record["diagnostic_refs"], *refs]
                record["attempts"].append(
                    {
                        "method": "pandoc",
                        "status": rendered.status,
                        "error": rendered.error,
                    }
                )
                if rendered.status == "success":
                    parsed = rendered
        else:
            parsed = None
        if parsed is not None and parsed.status == "success":
            record = _publish_success(
                publish, record, parsed, f"outputs/tables/{record['table_id']}"
            )
        results.append(record)
    if native["error"] and (
        state.get("body", {}).get("status") == "organized" or native["diagnostics"]
    ):
        gaps.append(
            {
                "gap_id": "gap-conversion-context",
                "reason": "unparsed",
                "description": "完整作者上下文转换不能证明完整性：" + native["error"],
                "file": state["body"].get("root"),
            }
        )
    for record in results:
        if record["status"] != "success":
            gaps.append(
                {
                    "gap_id": f"gap-table-{record['table_id']}",
                    "reason": "unparsed",
                    "description": f"原表 {record['file']} 第 {record['start_line']}-{record['end_line']} 行未处理：{record['error']}",
                    "file": record["file"],
                }
            )
    return {
        "tables": results,
        "gaps": gaps,
        "diagnostics": native["diagnostics"],
        "range_observations": native.get("range_observations", []),
    }


def _prepare_attached_tables(
    state: dict,
    *,
    publish,
    read_member,
) -> dict:
    """把包内独立 CSV／ECSV 附表接入共享读取器与准备附件（发现 11）。

    每个成员按媒体类型／后缀用共用逻辑记录读取器读取：成功则发布为
    附表 ECSV（保留原列名，不赋科学含义），登记为准备结果表；格式／
    方言不能确定或读取失败时保留缺口与原文件，不把成功解码当已处理。
    """
    from ..records.tabular import TabularReadError, read_table_records
    from . import convert as conversion

    tables: list[dict] = []
    gaps: list[dict] = []
    for member in state.get("members", []):
        name = member["name"].lower()
        if not name.endswith((".csv", ".ecsv")):
            continue
        payload = read_member(member["name"])
        if payload is None:  # pragma: no cover - 成员存在则可读
            continue
        record: dict = {
            "table_id": f"att-{sha256_of({'member': member['name'], 'sha256': member['sha256']})[:16]}",
            "file": member["name"],
            "environment": "attached",
            "start_line": 1,
            "end_line": 1,
            "content_start_line": 1,
            "label": "",
            "caption": "",
            "footnotes": [],
            "status": "failed",
            "method": "member_direct",
            "error": "",
            "warnings": [],
            "attempts": [{"method": "member_direct", "status": "failed", "error": ""}],
            "mapping": {"granularity": "table-block"},
            "diagnostic_refs": [],
            "source_member": f"member:{member['name']}",
        }
        try:
            records = read_table_records(
                payload, name=member["name"], media_type=member.get("media_type")
            )
        except TabularReadError as exc:
            record["error"] = f"独立附表不能按共用规则读取：{exc}；保留原文件与提及"
            gaps.append(
                {
                    "gap_id": f"gap-attached-{len(gaps) + 1:03d}",
                    "reason": "unparsed",
                    "description": (
                        f"包内独立附表 {member['name']} 不能确定格式或方言"
                        f"（{exc}）；保留原文件，影响未知"
                    ),
                    "file": member["name"],
                }
            )
            tables.append(record)
            continue
        parsed = conversion.ParsedTable(
            status="success",
            method="member_direct",
            environment="attached",
            columns=[
                {
                    "name": column,
                    "original_header": records.column_descriptions.get(column, column),
                    **records.column_metadata.get(column, {}),
                    "unit_text": records.column_metadata.get(column, {}).get(
                        "unit", ""
                    ),
                    "null_values": ["", "..."],
                }
                for column in records.columns
            ],
            header_rows=[list(records.columns)],
            table_metadata=records.table_metadata,
            data_rows=[
                [row[column] for column in records.columns] for row in records.rows
            ],
            warnings=[
                (
                    f"包内独立附表直读（{member['name']}，{len(records.rows)} 逻辑记录）；"
                    "不赋科学含义"
                )
            ],
        )
        record["attempts"] = [
            {"method": "member_direct", "status": "success", "error": ""}
        ]
        final = _publish_success(
            publish, record, parsed, f"outputs/tables/{record['table_id']}"
        )
        final["attempts"] = [
            {
                "method": "member_direct",
                "status": final["status"],
                "error": final["error"],
            }
        ]
        if final["status"] != "success":
            gaps.append(
                {
                    "gap_id": f"gap-attached-{record['table_id']}",
                    "reason": "unparsed",
                    "description": f"附表 {member['name']} 发布未完成：{final['error']}",
                    "file": member["name"],
                }
            )
        tables.append(final)
    return {"tables": tables, "gaps": gaps}


def _verify_frozen_converters(state: dict, availability: dict) -> None:
    """转换器版本与固定输入不一致时拒绝转换（改输入须新执行）。"""
    frozen = state.get("request", {}).get("converters") or {}
    for tool, current in availability.items():
        recorded = frozen.get(tool) or {}
        recorded_version = recorded.get("version")
        current_version = current.version if current.available else None
        if recorded_version != current_version:
            raise WorkspaceIntegrityError(
                f"转换器 {tool} 与执行固定版本不一致：固定 "
                f"{recorded_version!r}，当前 {current_version!r}"
                f"（{'' if current.available else '不可用：' + str(current.reason)}）；"
                "改变输入或转换器版本须用新请求键建立新执行"
            )


def _pandoc_literal_fallback(excerpt, *, block, runner, available, publish):
    """Optional renderer of an already proven literal matrix, not macro recovery."""
    import tempfile
    from pathlib import Path

    from .sandbox import MAX_CONVERTER_OUTPUT_BYTES

    with tempfile.TemporaryDirectory(prefix="hvs-pandoc-") as temporary:
        workdir = Path(temporary)
        (workdir / "input.tex").write_text(excerpt, encoding="utf-8")
        command = [
            available.executable,
            "--from=latex",
            "--to=html",
            "--output=table.html",
            "input.tex",
        ]
        outcome = runner.run("pandoc", command, workdir=workdir)
        output = workdir / "table.html"
        data = b""
        if output.is_file():
            with output.open("rb") as stream:
                data = stream.read(MAX_CONVERTER_OUTPUT_BYTES + 1)
        diagnostics = []
        for name, payload in (
            ("html", data[:MAX_CONVERTER_OUTPUT_BYTES]),
            ("stderr.txt", outcome.stderr),
        ):
            ref = publish(f"outputs/tables/{block['table_id']}/pandoc.{name}", payload)
            diagnostics.append({"path": ref.path, "sha256": ref.sha256})
        parsed, _, _ = _pandoc_verdict(outcome, data, excerpt=excerpt)
        if len(data) > MAX_CONVERTER_OUTPUT_BYTES or outcome.exceeded_output:
            parsed = conversion.ParsedTable("failed", method="pandoc", error="输出超限")
        return parsed, diagnostics


def _pandoc_verdict(outcome, html_bytes: bytes, *, excerpt: str = ""):
    """Pandoc 回退与主路径相同的联合核对：进程、表身份、结构与内容对应。

    逐行逐格核对原字符串与数值配对；不允许词集合或弱结构认证。
    本路径只接收原件已经可以确定性读取的字面网格。
    """
    problems: list[str] = []
    if outcome.error:
        problems.append(outcome.error)
    if outcome.timed_out:
        problems.append("转换进程超时")
    if outcome.returncode != 0:
        problems.append(f"进程退出码 {outcome.returncode} 非零")
    html = html_bytes.decode("utf-8", "replace")
    if not html:
        problems.append("转换器没有产出 HTML")
        return (
            conversion.ParsedTable(
                status="failed", method="pandoc", error="；".join(problems)
            ),
            problems,
            0,
        )
    parsed, table_count = conversion.parse_html_tables(html, method="pandoc")
    if table_count != 1:
        problems.append(f"输出包含 {table_count} 张表，无法与原表块唯一对应")
    if parsed.status == "success" and excerpt:
        problems.extend(conversion.verify_content_correspondence(excerpt, parsed))
    if problems or parsed.status != "success" or table_count != 1:
        return (
            conversion.ParsedTable(
                status="failed",
                method="pandoc",
                error="；".join(problems) or "联合核对未通过",
            ),
            problems,
            table_count,
        )
    return parsed, problems, table_count


def _publish_success(publish, record: dict, parsed, base: str) -> dict:
    try:
        ecsv_text = conversion.table_ecsv_text(parsed)
        roundtrip = conversion.verify_ecsv_roundtrip(ecsv_text, parsed)
    except (ValueError, TypeError) as exc:
        roundtrip = [f"呈现元数据／ECSV 无法写出：{exc}"]
    if roundtrip:
        record["status"] = "failed"
        record["error"] = "ECSV 回读核对未通过：" + "；".join(roundtrip[:5])
        return record
    published = publish(f"{base}/table.ecsv", ecsv_text.encode("utf-8"))
    # 轻量工程映射附件：原表头层次、合并格跨度、呈现差异与输出对应
    # （表块粒度），不伪造精细行号。
    mapping = {
        "table_id": record.get("table_id"),
        "granularity": "table-block",
        "source": {
            "file": record.get("file"),
            "start_line": record.get("start_line"),
            "end_line": record.get("end_line"),
            "environment": parsed.environment or record.get("environment"),
        },
        "method": parsed.method,
        "columns": parsed.columns,
        "header_rows": parsed.header_rows,
        "raw_rows": parsed.raw_rows,
        "table_metadata": parsed.table_metadata,
        "row_count": parsed.row_count,
        "column_count": parsed.column_count,
        "merged_cells": parsed.merged_cells,
        "presentation_changes": parsed.presentation_changes,
        "backing": {
            "member": parsed.backing_member,
            "columns": list(parsed.backing_columns),
        },
        "warnings": list(parsed.warnings),
        "native": record.get("mapping", {}),
        "context_sources": record.get("context_sources", []),
    }
    mapping_payload = json.dumps(
        mapping, ensure_ascii=False, sort_keys=True, indent=2
    ).encode("utf-8")
    mapping_ref = publish(f"{base}/mapping.json", mapping_payload)
    record["ecsv_ref"] = {"path": published.path, "sha256": published.sha256}
    record["mapping_ref"] = {"path": mapping_ref.path, "sha256": mapping_ref.sha256}
    record.update(
        {
            "status": "success",
            "method": parsed.method,
            "environment": parsed.environment or record["environment"],
            "warnings": list(parsed.warnings),
            "row_count": parsed.row_count,
            "column_count": parsed.column_count,
            "ecsv_path": published.path,
            "ecsv_sha256": published.sha256,
            "mapping_path": mapping_ref.path,
            "mapping_sha256": mapping_ref.sha256,
            # backing 直读的结构化出处：实际数值来源成员与原选列
            # （二次审核发现 10）。
            "backing_member": parsed.backing_member,
            "backing_columns": list(parsed.backing_columns),
        }
    )
    return record


def candidate_preview(candidate: dict, notation: str) -> list[str]:
    """按候选解释对完整原记法确定性展开预览；不可应用返回空列表。"""
    return _apply_interpretation(notation, candidate) or []


def _apply_interpretation(notation: str, candidate: dict) -> list[str] | None:
    """把候选解释应用于**完整**原记法（不截首段）并确定性展开。

    对 object_range 候选：给记法中的裸数字补候选前缀后整体展开；列表、
    区间、前导零均按同一规则处理。不能完整确定性展开时返回 None（保持
    未决），不输出部分成员。
    """
    if candidate.get("kind") != "object_range":
        return None
    prefix = str(candidate.get("prefix") or "")
    if not prefix:
        return None
    rewritten = re.sub(
        r"(?<![A-Za-z0-9])(\d+)",
        lambda match: f"{prefix}{match.group(1)}",
        notation,
    )
    expansion = expand_range_notation(rewritten, object_context=True)
    return (
        list(expansion.identifiers)
        if expansion.expanded
        and all(item.startswith(prefix) for item in expansion.identifiers)
        else None
    )


def _range_decision_result(
    review: ReviewService,
    question_ref: RecordRef,
    candidates: list[dict],
    notation: str,
) -> dict:
    view = review.get_question(question_ref)
    if view.status != "answered" or not view.decisions:
        return {
            "status": "ambiguous",
            "reason": "问题未决：范围保持未列清，不当作完整名单",
        }
    decision = view.decisions[-1]
    answer = decision.data.answer_en.strip()
    if answer == RANGE_OPTION_NOT_OBJECTS:
        return {
            "status": "not-objects",
            "reason": "专家裁定该记法不是对象编号范围，不展开成员",
            "identifiers": [],
            "decision_ref": decision.ref().model_dump(mode="json"),
        }
    match = next((c for c in candidates if str(c.get("code")) == answer), None)
    if match is None:
        return {
            "status": "ambiguous",
            "reason": (
                f"决定答复 {answer!r} 不在候选解释代码内；不从自由文本推断，保持未决"
            ),
        }
    preview = _apply_interpretation(notation, match)
    if not preview:
        return {
            "status": "ambiguous",
            "reason": "候选解释不能对完整原记法确定性展开，保持未决",
        }
    return {
        "status": "expert-resolved",
        "identifiers": preview,
        "rule": f"decision-option:{answer}",
        "decision_ref": decision.ref().model_dump(mode="json"),
    }


class _RegistrationCheck:
    __slots__ = ("object_context", "ok", "reason")

    def __init__(
        self, ok: bool, reason: str = "", *, object_context: bool = False
    ) -> None:
        self.ok = ok
        self.reason = reason
        self.object_context = object_context


def _verify_registered_notation(body_texts: dict[str, str], item: dict, notation: str):
    """显式登记作为补充时核验文件、行范围与记法文本与固定材料对应。"""
    file = item.get("file")
    if not file:
        return _RegistrationCheck(False, "缺少出处文件")
    text = body_texts.get(str(file))
    if text is None:
        return _RegistrationCheck(False, f"出处文件 {file} 不在正文包含流中")
    start_line = item.get("start_line")
    end_line = item.get("end_line")
    if (
        not isinstance(start_line, int)
        or not isinstance(end_line, int)
        or start_line < 1
        or end_line < start_line
    ):
        return _RegistrationCheck(False, "行范围无效")
    lines = text.split("\n")
    if end_line > len(lines):
        return _RegistrationCheck(False, f"行范围越界（文件共 {len(lines)} 行）")
    from .cleaning import scan_view

    view = scan_view(text)
    if (begin := re.search(r"\\begin\{document\}", view)) is not None:
        view = (
            "".join("\n" if c == "\n" else " " for c in view[: begin.end()])
            + view[begin.end() :]
        )
    window = "\n".join(view.split("\n")[start_line - 1 : end_line])
    if notation not in window:
        return _RegistrationCheck(
            False, "记法文本不出现在声明的行范围内（不核验出处不展开）"
        )
    candidates = _discover_range_notations({str(file): text}, exclude=[])
    context = any(
        candidate["notation"] == notation
        and candidate["object_context"]
        and start_line <= candidate["start_line"] <= candidate["end_line"] <= end_line
        for candidate in candidates
    )
    return _RegistrationCheck(True, object_context=context)


_RANGE_ATOM = r"(?:\d(?:[A-Za-z0-9_.-]*[A-Za-z0-9])?|[A-Z][A-Za-z0-9_.-]*\d(?:[A-Za-z0-9_.-]*[A-Za-z0-9])?)"
_DISCOVERY_RE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9_-]{0,63}?\d{1,12}"
    rf"(?:\s*(?:--|\u2013|\u2014|-)\s*{_RANGE_ATOM}|\s+(?:to|through)\s+{_RANGE_ATOM}|\s*,\s*{_RANGE_ATOM})+"
    rf"(?:\s+and\s+(?:others|{_RANGE_ATOM}))?"
)


def _discover_range_notations(
    body_texts: dict[str, str],
    *,
    exclude: list[tuple[str, int, int, str]],
) -> list[dict]:
    """Find complete source expressions; establish prose/identity-cell context.

    Non-identity table columns never inherit an 'Object' word from a nearby
    header. Definitions, examples, preamble and provably false branches are
    excluded. Unknown candidates are retained with an explicit context flag.
    """
    from .cleaning import scan_view, unknown_conditional_spans
    from .ranges import object_context

    found = []
    for name, text in body_texts.items():
        view = scan_view(text)
        if (begin := re.search(r"\\begin\{document\}", view)) is not None:
            view = (
                "".join("\n" if c == "\n" else " " for c in view[: begin.end()])
                + view[begin.end() :]
            )
        uncertain = unknown_conditional_spans(text)
        tables = table_scan._all_content_environments(view)
        identity_spans = []
        for env, start, stop in tables:
            excerpt = view[start:stop]
            parsed = conversion.parse_latex_table_excerpt(excerpt)
            if parsed.status != "success":
                continue
            heads = (
                parsed.header_rows[-1] if parsed.header_rows else parsed.data_rows[0]
            )
            indices = [
                i
                for i, value in enumerate(heads)
                if value.strip().casefold()
                in {"object", "name", "star", "id", "obj", "identifier"}
            ]
            if len(indices) != 1:
                continue
            cursor = start + len(r"\begin{" + env + "}")
            for _ in range(2 if env == "tabular*" else 1):
                while cursor < stop and view[cursor].isspace():
                    cursor += 1
                end = table_scan._balanced_group(view, cursor)
                if end is None:
                    break
                cursor = end + 1
            if env.startswith("deluxetable") and r"\startdata" in view[cursor:stop]:
                cursor = view.index(r"\startdata", cursor, stop) + len(r"\startdata")
            body = view[cursor:stop]
            for raw_row in conversion.split_latex_rows(body):
                cells = conversion.split_latex_cells(raw_row)
                column_offset = cursor
                for i, cell in enumerate(cells):
                    if i == indices[0]:
                        identity_spans.append(
                            (column_offset, column_offset + len(cell))
                        )
                    column_offset += len(cell) + 1
                cursor += len(raw_row) + 2
        for match in _DISCOVERY_RE.finditer(view):
            start_line = view.count("\n", 0, match.start()) + 1
            end_line = view.count("\n", 0, match.end() - 1) + 1
            notation = match[0]
            if any(
                f == name
                and first <= start_line <= end_line <= last
                and exact == notation
                for f, first, last, exact in exclude
            ):
                continue
            in_table = any(start <= match.start() < stop for _, start, stop in tables)
            context = (
                any(
                    start <= match.start() and match.end() <= stop
                    for start, stop in identity_spans
                )
                if in_table
                else object_context(view, match.start(), match.end())
            )
            context = context and not any(
                start <= match.start() < stop for start, stop in uncertain
            )
            found.append(
                {
                    "notation": notation,
                    "file": name,
                    "start_line": start_line,
                    "end_line": end_line,
                    "object_context": context,
                }
            )
    return found


def _commit_prepared_revision(
    ctx: RuntimeContext, state: dict, *, conversions: list[dict] | None = None
) -> RecordRef:
    """提交准备后的来源包新修订：成员、派生文件与全部已知缺口。"""
    store = ctx.space.store()
    source_ref = _ref(state["request"]["source_ref"])
    basis = store.read_record(source_ref)
    if basis is None:
        raise WorkspaceIntegrityError("固定来源记录缺失，不能提交准备修订")
    # 事务重入按本执行的操作键查已保存回执，不借 current 修订猜测：同
    # 来源的后续执行 B 先完成时，current 已属于 B，A 恢复仍应复用 A 的
    # 回执（二次审核反例 interleave）。
    operation_key = f"materials-prepare:{ctx.execution.execution_id}:revision"
    receipt = store.find_receipt(operation_key)
    if (
        receipt is not None
        and receipt.kind == "materials_prepared_revision"
        and receipt.result_refs
    ):
        saved = store.read_record(receipt.result_refs[0])
        if saved is not None:
            return saved.ref()
    record = store.read_record_current(basis.record_id)
    if record is None:  # pragma: no cover - basis 存在则 current 存在
        record = basis
    raw_entry = next((f for f in basis.data.files if f.role == "raw_package"), None)
    files: list[SourceFile] = [raw_entry] if raw_entry is not None else []
    body = state.get("body", {})
    if body.get("view_path") and body.get("view_sha256"):
        root_member = f"member:{body.get('root')}" if body.get("root") else None
        files.append(
            SourceFile(
                file_id="body-view",
                path=body["view_path"],
                sha256=body["view_sha256"],
                media_type="text/plain; charset=utf-8",
                role="derived_text",
                read_state="ready",
                derived_from=[root_member] if root_member else [],
            )
        )
    member_ids = {m["name"] for m in state["members"]}
    for member in state["members"]:
        files.append(
            SourceFile(
                file_id=f"member:{member['name']}",
                path=f"sources/materials/members/{member['sha256']}.bin",
                sha256=member["sha256"],
                media_type=member["media_type"],
                role="member",
                read_state="ready" if member.get("text") is not None else "unparsed",
                derived_from=[raw_entry.file_id] if raw_entry is not None else [],
            )
        )
        if member.get("derived_path"):
            files.append(
                SourceFile(
                    file_id=f"utf8:{member['name']}",
                    path=member["derived_path"],
                    sha256=member["derived_published_sha256"],
                    media_type="text/plain; charset=utf-8",
                    role="derived_text",
                    read_state="ready",
                    derived_from=[f"member:{member['name']}"],
                )
            )
    for conversion_record in (
        conversions if conversions is not None else state.get("conversions", [])
    ):
        if conversion_record.get("status") != "success":
            continue
        source_member = f"member:{conversion_record.get('file')}"
        # backing CSV 直读的实际数值来源成员也进入派生关系（双来源，
        # 二次审核发现 10）。
        derived_from = list(
            dict.fromkeys(
                [
                    source_member,
                    *[
                        f"member:{name}"
                        for name in conversion_record.get("context_sources", [])
                    ],
                ]
            )
        )
        if conversion_record.get("backing_member"):
            backing_id = f"member:{conversion_record['backing_member']}"
            if backing_id not in derived_from:
                derived_from.append(backing_id)
        if conversion_record.get("ecsv_path"):
            files.append(
                SourceFile(
                    file_id=f"ecsv:{conversion_record['table_id']}",
                    path=conversion_record["ecsv_path"],
                    sha256=conversion_record["ecsv_sha256"],
                    media_type="text/ecsv",
                    role="derived_table",
                    read_state="ready",
                    derived_from=derived_from,
                )
            )
        if conversion_record.get("mapping_path"):
            files.append(
                SourceFile(
                    file_id=f"mapping:{conversion_record['table_id']}",
                    path=conversion_record["mapping_path"],
                    sha256=conversion_record["mapping_sha256"],
                    media_type="application/json",
                    role="derived_mapping",
                    read_state="ready",
                    derived_from=derived_from,
                )
            )
        # 转换诊断与上下文附件接入来源修订：可消费的引用而非文件名约定
        # （二次审核发现 10）；同一表的多份诊断按序号区分。
        for diag_index, diagnostic in enumerate(
            conversion_record.get("diagnostic_refs", [])
        ):
            files.append(
                SourceFile(
                    file_id=(
                        f"diagnostic:{conversion_record['table_id']}:{diag_index:02d}"
                    ),
                    path=diagnostic["path"],
                    sha256=diagnostic["sha256"],
                    media_type="application/octet-stream",
                    role="diagnostic",
                    read_state="ready",
                    derived_from=[source_member],
                )
            )
    for index, diagnostic in enumerate(state.get("conversion_diagnostics", [])):
        files.append(
            SourceFile(
                file_id=f"conversion-diagnostic:{index}",
                path=diagnostic["path"],
                sha256=diagnostic["sha256"],
                media_type="application/octet-stream",
                role="diagnostic",
                read_state="ready",
                derived_from=[f"member:{name}" for name in member_ids],
            )
        )
    preparation_gaps = [
        Gap(
            gap_id=gap["gap_id"],
            reason=gap["reason"],
            description=gap["description"],
            file_id=(
                f"member:{gap['file']}"
                if gap.get("file") and gap["file"] in member_ids
                else gap.get("file_id")
                if gap.get("file_id") in {f.file_id for f in files}
                else None
            ),
            mention_evidence_refs=gap.get("mention_evidence_refs", []),
            affected_targets=AffectedTargets.model_validate(
                gap.get("affected_targets") or {"scope": "unknown", "items": []}
            ),
        )
        for gap in state["gaps"]
    ]
    gaps = preparation_gaps
    new_revision = record.revision + 1
    new_record = basis.model_copy(
        update={
            "revision": new_revision,
            "created_at": ctx.now(),
            "execution_id": ctx.execution.execution_id,
            "data": basis.data.model_copy(update={"files": files, "gaps": gaps}),
        }
    )
    store.commit(
        CommitRequest(
            operation_key=operation_key,
            kind="materials_prepared_revision",
            new_records=[new_record],
            expected_revisions={record.record_id: record.revision},
            result_refs=[new_record.ref()],
            execution_id=ctx.execution.execution_id,
        )
    )
    return new_record.ref()


def build_bundle(state: dict) -> dict:
    """组装准备输入附件（轻量工程 JSON，非独立科学业务记录）。"""
    body = dict(state.get("body", {}))
    body_files = body.pop("files", None)
    # 正文视图引用保留在公开输入中（可消费的引用，二次审核发现 10）。
    tables = []
    for record in state.get("conversions", []):
        entry = {
            key: value
            for key, value in record.items()
            if key
            not in (
                "columns",
                "header_rows",
                "raw_rows",
                "merged_cells",
                "presentation_changes",
            )
        }
        if record.get("status") == "success":
            entry["source_file_id"] = f"ecsv:{record['table_id']}"
        tables.append(entry)
    members = [
        {
            key: value
            for key, value in member.items()
            if key
            in (
                "name",
                "sha256",
                "size",
                "media_type",
                "encoding",
                "decode_note",
                "derived_path",
                "derived_published_sha256",
            )
        }
        for member in state.get("members", [])
    ]
    if body_files is None:
        body_files = [
            {key: value for key, value in entry.items() if key != "cleaned_ref"}
            for entry in state.get("body", {}).get("files", [])
        ]
    return {
        "execution_id": state.get("execution_id"),
        "request": state.get("request"),
        "raw_sha256": state.get("raw_sha256"),
        "package_format": state.get("package_format"),
        "prepared_source_ref": state.get("prepared_source_ref"),
        "body": {**body, "files": body_files},
        "tables": tables,
        "conversion_diagnostics": state.get("conversion_diagnostics", []),
        "members": members,
        "gaps": state.get("gaps", []),
        "ranges": state.get("ranges", []),
        "summary": {
            "body_status": body.get("status"),
            "materials_complete": body.get("status") == "organized"
            and not state.get("gaps")
            and all(t.get("status") == "success" for t in tables),
            "table_count": len(tables),
            "table_success": sum(1 for t in tables if t.get("status") == "success"),
            "gap_count": len(state.get("gaps", [])),
            "range_count": len(state.get("ranges", [])),
        },
    }
