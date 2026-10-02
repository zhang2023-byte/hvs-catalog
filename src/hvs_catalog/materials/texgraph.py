"""Organize body graphs: main selection, includes, missing members, cycles, and escapes.

Base algorithms derive from stella_hvs/src/stella/lit/extraction/tex_graph.py at 8421c98
under MIT, adapted for R11. Inputs are in-memory expanded package name/text mappings,
not filesystem paths. Select a unique main automatically; multiple valid candidates
require explicit root_file, otherwise retain unavailable body and candidates. Follow
literal input/include paths only; register missing/cyclic/external/dynamic/unsupported
includes in problems without failing a valid main globally. Keep unreferenced drafts
excluded without declaring scientific irrelevance; non-TeX includes such as .bbl enter
non_tex_includes. Use cleaned text preserving original lines and scan after comment
removal. See LICENSE-NOTICE.md for authorship/license.
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
# Unbraced dynamic includes such as input followed by jobname or a bare filename
# lack a determinate literal target; register unsupported includes rather than silently skipping.
DYNAMIC_INCLUDE_RE = re.compile(
    r"\\(?:input|include)\s*(?!\{)(?:\\[A-Za-z@]+|[A-Za-z@][^\s%\\{}]*)"
)
CONDITIONAL_INCLUDE_RE = re.compile(r"\\InputIfFileExists\s*\{([^}]+)\}")
DOCUMENTCLASS_RE = re.compile(r"\\documentclass(?:\[[^\]]*\])?\s*\{")
BEGIN_DOCUMENT_RE = re.compile(r"\\begin\{document\}")


@dataclass(frozen=True)
class IncludeProblem:
    """Include problem located by file/directive with original target retained."""

    code: str
    file: str
    target: str
    detail: str

    def describe(self) -> str:
        return f"{self.file}: {self.detail}（目标 {self.target!r}）"


@dataclass(frozen=True)
class ManuscriptFile:
    """One body-graph file with hash, line count, encoding, and inclusion location."""

    name: str
    sha256: str
    line_count: int
    encoding: str | None
    cleaned_text: str
    # Including file/line; None for the main document itself.
    included_from: str | None = None
    included_at_line: int | None = None
    included_at_offset: int | None = None


@dataclass
class ManuscriptGraph:
    """Body-organization result.

    status is organized or unavailable; reason supplies the unavailable code and
    root_candidates retains main-document choices.
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
    """Resolve literal include targets; return None for missing/escaping paths without
    filename guessing.

    Try relative to the referring file, then the package root, checking package bounds
    and .tex suffixes on both paths. Permit ordinary ../ only when normalization remains
    inside the package.
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
                return "/" + target  # Sentinel for escaping the package root.
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
    """Organize one main-document graph; record problems without guessing the main by name.

    member_hashes supplies original-byte hashes. Missing hashes fall back to
    decoded-text hashes only for synthetic tests; production always passes original
    expansion hashes.
    """
    member_hashes = member_hashes or {}
    member_encodings = member_encodings or {}
    tex_names = sorted(name for name in members if name.lower().endswith(".tex"))
    cleaned = {name: strip_tex_comments(members[name]) for name in tex_names}
    # Structural scans use verbatim/verb masks so display-code literals never become actual includes.
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
