"""正文图解析：主稿确定、包含跟随与缺件／循环／越界记录（迁入改造）。

基础算法迁自旧仓 ``stella_hvs/src/stella/lit/extraction/tex_graph.py``
（参考提交 8421c98，MIT 许可），按 R11 材料准备设计改造：

- 输入是包内已展开成员的内存映射（名称 → 文本），不是文件系统；
- 主稿唯一时自动确定；多个有效主稿必须由操作者显式选择（``root_file``），
  不按名称猜；未选择时正文保持不可用并记录全部候选；
- ``\\input``／``\\include`` 只跟随字面路径；缺件、循环、包外路径、
  动态或不支持的包含形式逐项记入 ``problems``，不使有效主稿整体失败；
- 未引用草稿保留在 ``excluded`` 清单中，不进入主稿视图，也不被断言为
  科学无关；非 TeX 的 ``\\input``（如 ``.bbl``）记入 ``non_tex_includes``；
- 正文组织使用清理后的文本（行数与原文件一致），指令扫描在去注释文本
  上进行，注释掉的包含不跟随。许可与作者信息见本目录
  ``LICENSE-NOTICE.md``（MIT）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from .cleaning import scan_view, strip_tex_comments

__all__ = [
    "BODY_UNAVAILABLE_MULTIPLE_ROOTS",
    "BODY_UNAVAILABLE_NO_ROOT",
    "IncludeProblem",
    "ManuscriptFile",
    "ManuscriptGraph",
    "resolve_manuscript",
]

BODY_UNAVAILABLE_NO_ROOT = "no_root_tex"
BODY_UNAVAILABLE_MULTIPLE_ROOTS = "multiple_root_tex_candidates"
BODY_UNAVAILABLE_BAD_SELECTION = "selected_root_not_manuscript_root"

_MISSING_INCLUDED = "missing_included_tex"
_CYCLIC_INCLUDE = "cyclic_tex_include"
_INCLUDE_OUTSIDE = "include_path_outside_package"
_UNSUPPORTED_DIRECTIVE = "unsupported_include_directive"
_CONDITIONAL_TEX = "conditional_tex_include_ambiguous"

SUPPORTED_INCLUDE_RE = re.compile(r"\\(?:input|include)\b\s*\{([^}]+)\}")
UNSUPPORTED_INCLUDE_RE = re.compile(
    r"\\(?:subfile|subfileinclude|import|subimport|includefrom"
    r"|subincludefrom)\b"
)
# 无花括号的动态包含（如 ``\\input\\jobname`` 或 ``\\input file``）：
# 无法确定字面目标，登记不支持的包含形式，不静默跳过。
DYNAMIC_INCLUDE_RE = re.compile(
    r"\\(?:input|include)\s*(?!\{)(?:\\[A-Za-z@]+|[A-Za-z@][^\s%\\{}]*)"
)
CONDITIONAL_INCLUDE_RE = re.compile(r"\\InputIfFileExists\s*\{([^}]+)\}")
DOCUMENTCLASS_RE = re.compile(r"\\documentclass(?:\[[^\]]*\])?\s*\{")
BEGIN_DOCUMENT_RE = re.compile(r"\\begin\{document\}")


@dataclass(frozen=True)
class IncludeProblem:
    """一个包含关系问题；按文件与指令定位，保留原始目标。"""

    code: str
    file: str
    target: str
    detail: str

    def describe(self) -> str:
        return f"{self.file}: {self.detail}（目标 {self.target!r}）"


@dataclass(frozen=True)
class ManuscriptFile:
    """主稿图中一个文件的登记：哈希、行数、编码与包含位置。"""

    name: str
    sha256: str
    line_count: int
    encoding: str | None
    cleaned_text: str
    # 被谁在哪一行包含；主稿本身为 None。
    included_from: str | None = None
    included_at_line: int | None = None
    included_at_offset: int | None = None


@dataclass
class ManuscriptGraph:
    """一次正文组织的结果。

    ``status`` 为 ``organized`` 或 ``unavailable``；不可用时 ``reason``
    给出代码，``root_candidates`` 保留候选（多主稿时供操作者选择）。
    """

    status: str
    reason: str | None = None
    root_candidates: list[str] = field(default_factory=list)
    root: str | None = None
    files: list[ManuscriptFile] = field(default_factory=list)
    edges: dict[str, list[str]] = field(default_factory=dict)
    excluded: list[str] = field(default_factory=list)
    non_tex_includes: list[tuple[str, str]] = field(default_factory=list)
    problems: list[IncludeProblem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def problem_summaries(self) -> list[str]:
        return [p.describe() for p in self.problems]


def _normalize_target(raw: str) -> str:
    target = raw.strip()
    if (
        "\\" in target
        or target.startswith(("/", "~"))
        or any(not part for part in target.split("/"))
    ):
        return ""
    return target


def _resolve_member(
    members: dict[str, str], referring_dir: str, raw_target: str
) -> str | None:
    """按字面路径解析包含目标；越界或缺失返回 None（不猜名称）。

    先按引用文件目录相对解析，再按包根相对解析，两步均检查包内
    边界并支持 .tex 后缀。普通 ../ 路径只在规范化后仍包内时允许。
    """
    target = _normalize_target(raw_target)
    if not target:
        return None
    for base in (referring_dir, ""):
        literal = _join(base, target)
        if literal.startswith("/"):
            continue
        candidates = [literal]
        if not literal.lower().endswith(".tex"):
            candidates.append(literal + ".tex")
        for candidate in candidates:
            if candidate in members:
                return candidate
    return None


def _join(base_dir: str, target: str) -> str:
    parts: list[str] = []
    for segment in (base_dir.split("/") if base_dir else []) + target.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if parts:
                parts.pop()
            else:
                return "/" + target  # 越出包根的哨兵值
        else:
            parts.append(segment)
    return "/".join(parts)


def resolve_manuscript(
    members: dict[str, str],
    *,
    member_hashes: dict[str, str] | None = None,
    member_encodings: dict[str, str | None] | None = None,
    root_file: str | None = None,
) -> ManuscriptGraph:
    """组织一篇主稿的正文图；问题记入 ``problems``，不按名称猜主稿。

    ``member_hashes`` 提供成员原字节哈希（未提供时退回对解码文本的哈
    希，仅供合成测试使用；正式链路始终传入展开阶段的原始字节哈希）。
    """
    member_hashes = member_hashes or {}
    member_encodings = member_encodings or {}
    tex_names = sorted(name for name in members if name.lower().endswith(".tex"))
    cleaned = {name: strip_tex_comments(members[name]) for name in tex_names}
    # 结构扫描用 verbatim／verb 掩蔽视图：字面包含指令不应来自展示代码。
    scanned = {name: scan_view(cleaned[name]) for name in tex_names}
    graph = ManuscriptGraph(status="unavailable")

    def is_root(name: str) -> bool:
        text = scanned[name]
        return bool(DOCUMENTCLASS_RE.search(text) and BEGIN_DOCUMENT_RE.search(text))

    roots = [name for name in tex_names if is_root(name)]
    graph.root_candidates = list(roots)
    if not roots:
        graph.reason = BODY_UNAVAILABLE_NO_ROOT
        graph.excluded = tex_names
        return graph
    if root_file is not None and root_file not in roots:
        graph.reason = BODY_UNAVAILABLE_BAD_SELECTION
        graph.excluded = tex_names
        return graph
    if len(roots) > 1:
        if root_file is None:
            graph.reason = BODY_UNAVAILABLE_MULTIPLE_ROOTS
            graph.excluded = tex_names
            return graph
        roots = [root_file]
        graph.notes.append(f"操作者显式选择主稿：{root_file}")
    root = roots[0]

    included: list[str] = []
    visiting: list[str] = []
    include_positions: dict[str, tuple[str | None, int | None, int | None]] = {
        root: (None, None, None)
    }

    def visit(name: str, *, from_file: str | None, at_line: int | None) -> None:
        visiting.append(name)
        included.append(name)
        text = scanned[name]
        begin = BEGIN_DOCUMENT_RE.search(text)
        for match in UNSUPPORTED_INCLUDE_RE.finditer(text):
            line_number = text.count("\n", 0, match.start()) + 1
            graph.problems.append(
                IncludeProblem(
                    code=_UNSUPPORTED_DIRECTIVE,
                    file=name,
                    target=match.group(0),
                    detail=f"不支持的文件包含指令（第 {line_number} 行；动态包含不跟随）",
                )
            )
        for match in DYNAMIC_INCLUDE_RE.finditer(text):
            if SUPPORTED_INCLUDE_RE.match(text[match.start() :]):
                continue
            line_number = text.count("\n", 0, match.start()) + 1
            graph.problems.append(
                IncludeProblem(
                    code=_UNSUPPORTED_DIRECTIVE,
                    file=name,
                    target=match.group(0),
                    detail=f"无花括号的动态包含（第 {line_number} 行），目标不能确定，不跟随",
                )
            )
        for match in SUPPORTED_INCLUDE_RE.finditer(text):
            raw_target = match.group(1).strip()
            if not raw_target:
                continue
            line_number = text.count("\n", 0, match.start()) + 1
            resolved = _resolve_member(members, _parent_dir(name), raw_target)
            if resolved is None:
                if begin is not None and match.start() < begin.start():
                    graph.problems.append(
                        IncludeProblem(
                            code=_MISSING_INCLUDED,
                            file=name,
                            target=raw_target,
                            detail=f"前言包含缺失（第 {line_number} 行），按缺件记录",
                        )
                    )
                    continue
                graph.problems.append(
                    IncludeProblem(
                        code=_MISSING_INCLUDED,
                        file=name,
                        target=raw_target,
                        detail=f"包含目标缺失或越界（第 {line_number} 行）",
                    )
                )
                continue
            if not resolved.lower().endswith(".tex"):
                graph.non_tex_includes.append((name, resolved))
                continue
            if resolved in visiting:
                cycle = " -> ".join(visiting + [resolved])
                graph.problems.append(
                    IncludeProblem(
                        code=_CYCLIC_INCLUDE,
                        file=name,
                        target=resolved,
                        detail=f"循环包含（第 {line_number} 行）：{cycle}",
                    )
                )
                continue
            graph.edges.setdefault(name, []).append(resolved)
            if resolved in included:
                graph.notes.append(
                    f"{name} 第 {line_number} 行再次包含 {resolved}：原件视图按文件保存一次，转换登记各实际出现"
                )
                continue
            include_positions[resolved] = (name, line_number, match.start())
            visit(resolved, from_file=name, at_line=line_number)
        for match in CONDITIONAL_INCLUDE_RE.finditer(text):
            raw_target = match.group(1).strip()
            line_number = text.count("\n", 0, match.start()) + 1
            resolved = _resolve_member(members, _parent_dir(name), raw_target)
            if resolved is None:
                graph.notes.append(
                    f"{name}: 第 {line_number} 行条件包含 {raw_target!r} 不存在，跳过"
                )
                continue
            if resolved.lower().endswith(".tex"):
                graph.problems.append(
                    IncludeProblem(
                        code=_CONDITIONAL_TEX,
                        file=name,
                        target=resolved,
                        detail=f"条件 TeX 包含语义有歧义（第 {line_number} 行），"
                        "不跟随",
                    )
                )
            else:
                graph.non_tex_includes.append((name, resolved))
        visiting.pop()

    visit(root, from_file=None, at_line=None)

    files: list[ManuscriptFile] = []
    for name in included:
        digest = (
            member_hashes.get(name)
            or hashlib.sha256(members[name].encode("utf-8")).hexdigest()
        )
        included_from, included_at, included_offset = include_positions.get(
            name, (None, None, None)
        )
        files.append(
            ManuscriptFile(
                name=name,
                sha256=digest,
                line_count=len(cleaned[name].split("\n")),
                encoding=member_encodings.get(name),
                cleaned_text=cleaned[name],
                included_from=included_from,
                included_at_line=included_at,
                included_at_offset=included_offset,
            )
        )
    graph.root = root
    graph.status = "organized"
    graph.files = files
    included_set = set(included)
    graph.excluded = [name for name in tex_names if name not in included_set]
    return graph


def _parent_dir(name: str) -> str:
    parts = name.split("/")
    return "/".join(parts[:-1]) if len(parts) > 1 else ""
