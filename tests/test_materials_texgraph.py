"""正文组织（主稿、包含、注释与 verbatim）的合成测试。"""

from __future__ import annotations

from materials_helpers import DRAFT_TEX, synthetic_package

from hvs_catalog.materials.cleaning import strip_tex_comments
from hvs_catalog.materials.expansion import expand_source_package
from hvs_catalog.materials.texgraph import (
    BODY_UNAVAILABLE_MULTIPLE_ROOTS,
    BODY_UNAVAILABLE_NO_ROOT,
    resolve_manuscript,
)


def _members(space, package):
    expanded = expand_source_package(space, package)
    texts = {m.name: m.text for m in expanded.members if m.text is not None}
    hashes = {m.name: m.sha256 for m in expanded.members}
    return texts, hashes


def test_unique_root_and_include_positions(tmp_path):
    space, _imported = None, None
    from materials_helpers import prepared_space

    space, _imported = prepared_space(tmp_path)
    texts, hashes = _members(space, synthetic_package())
    graph = resolve_manuscript(texts, member_hashes=hashes)
    assert graph.status == "organized"
    assert graph.root == "main.tex"
    names = [f.name for f in graph.files]
    # 按包含顺序组织：main → macros(前言) → sec1 → tables/velocity → appendix
    assert names[0] == "main.tex"
    assert set(names) == {
        "main.tex",
        "macros.tex",
        "sec1.tex",
        "tables/velocity.tex",
        "appendix.tex",
    }
    by_name = {f.name: f for f in graph.files}
    assert by_name["sec1.tex"].included_from == "main.tex"
    assert by_name["sec1.tex"].included_at_line == 7
    # refs.bbl 是非 TeX include，保留为上下文材料
    assert ("main.tex", "refs.bbl") in graph.non_tex_includes
    # 未引用草稿没有进入本包；行数与原文件一致
    assert by_name["main.tex"].line_count == len(texts["main.tex"].split("\n"))


def test_commented_include_not_followed(tmp_path):
    from materials_helpers import prepared_space

    space, _ = prepared_space(tmp_path)
    texts, hashes = _members(
        space,
        synthetic_package(
            {
                "ignored.tex": b"\\section{Ignored}",
                "main.tex": (
                    b"\\documentclass{article}\n"
                    b"% \\input{ignored}\n"
                    b"\\begin{document}body\n\\end{document}\n"
                ),
            }
        ),
    )
    graph = resolve_manuscript(texts, member_hashes=hashes)
    assert graph.status == "organized"
    assert "ignored.tex" in graph.excluded
    assert not graph.problems


def test_multiple_roots_require_explicit_selection(tmp_path):
    from materials_helpers import prepared_space

    space, _ = prepared_space(tmp_path)
    texts, hashes = _members(
        space, synthetic_package({"draft/old.tex": DRAFT_TEX.encode()})
    )
    graph = resolve_manuscript(texts, member_hashes=hashes)
    assert graph.status == "unavailable"
    assert graph.reason == BODY_UNAVAILABLE_MULTIPLE_ROOTS
    assert set(graph.root_candidates) == {"main.tex", "draft/old.tex"}
    # 显式选择后按选择组织；草稿保留在 excluded，不断言科学无关
    chosen = resolve_manuscript(texts, member_hashes=hashes, root_file="draft/old.tex")
    assert chosen.status == "organized"
    assert chosen.root == "draft/old.tex"
    assert "main.tex" in chosen.excluded


def test_selected_root_must_be_a_root(tmp_path):
    from materials_helpers import prepared_space

    space, _ = prepared_space(tmp_path)
    texts, hashes = _members(space, synthetic_package())
    graph = resolve_manuscript(texts, member_hashes=hashes, root_file="sec1.tex")
    assert graph.status == "unavailable"
    assert graph.reason == "selected_root_not_manuscript_root"


def test_no_root_records_gap_not_crash(tmp_path):
    from materials_helpers import prepared_space

    space, _ = prepared_space(tmp_path)
    texts, hashes = _members(
        space,
        synthetic_package({"main.tex": b"no documentclass here\nfragment text\n"}),
    )
    graph = resolve_manuscript(texts, member_hashes=hashes)
    assert graph.status == "unavailable"
    assert graph.reason == BODY_UNAVAILABLE_NO_ROOT


def test_missing_include_records_problem(tmp_path):
    from materials_helpers import prepared_space

    space, _ = prepared_space(tmp_path)
    texts, hashes = _members(
        space,
        synthetic_package(
            {
                "main.tex": (
                    b"\\documentclass{article}\n\\begin{document}\n"
                    b"\\input{gone}\nbody\n\\end{document}\n"
                )
            }
        ),
    )
    graph = resolve_manuscript(texts, member_hashes=hashes)
    assert graph.status == "organized"
    codes = [p.code for p in graph.problems]
    assert "missing_included_tex" in codes
    # 其余正文仍组织成功：问题按缺件记录，不整体失败


def test_cyclic_include_records_problem(tmp_path):
    from materials_helpers import prepared_space

    space, _ = prepared_space(tmp_path)
    texts, hashes = _members(
        space,
        synthetic_package(
            {
                "main.tex": (
                    b"\\documentclass{article}\n\\begin{document}\n\\input{a}\n"
                    b"\\end{document}\n"
                ),
                "a.tex": b"content a\n\\input{main}\n",
            }
        ),
    )
    graph = resolve_manuscript(texts, member_hashes=hashes)
    assert graph.status == "organized"
    assert any(p.code == "cyclic_tex_include" for p in graph.problems)


def test_outside_and_dynamic_includes_recorded(tmp_path):
    from materials_helpers import prepared_space

    space, _ = prepared_space(tmp_path)
    texts, hashes = _members(
        space,
        synthetic_package(
            {
                "main.tex": (
                    b"\\documentclass{article}\n\\begin{document}\n"
                    b"\\input{../outside}\n\\import{dir}{file}\n"
                    b"\\InputIfFileExists{sec1}{yes}{no}\n"
                    b"\\end{document}\n"
                ),
            }
        ),
    )
    graph = resolve_manuscript(texts, member_hashes=hashes)
    codes = {p.code for p in graph.problems}
    assert "missing_included_tex" in codes  # ../outside 越界按缺件记录
    assert "unsupported_include_directive" in codes  # \import 动态包含
    assert "conditional_tex_include_ambiguous" in codes  # 条件 TeX 包含歧义


def test_strip_comments_preserves_lines_and_verbatim():
    text = (
        "line one % comment\n"
        "\\begin{verbatim}\n"
        "100% literal in verbatim\n"
        "\\end{verbatim}\n"
        "after \\verb|x%y| tail % gone\n"
        "escaped \\% percent stays\n"
    )
    cleaned = strip_tex_comments(text)
    lines = cleaned.split("\n")
    assert len(lines) == len(text.split("\n"))
    assert lines[0].rstrip() == "line one"
    assert len(cleaned) == len(text)  # native source columns/offsets remain valid
    assert lines[2] == "100% literal in verbatim"
    assert lines[4].startswith("after \\verb|x%y| tail")
    assert lines[4].rstrip().endswith("tail")
    assert "escaped \\% percent stays" in lines[5]
    assert lines[1] == "\\begin{verbatim}"


def test_verbatim_and_dynamic_and_draft_gating(tmp_path):
    """verbatim 字面包含不算缺件；无花括号动态包含登记；草稿不污染主稿。"""
    members = {
        "main.tex": (
            "\\documentclass{article}\n"
            "\\begin{document}\n"
            "\\begin{verbatim}\n"
            "\\input{fake}\n"
            "\\end{verbatim}\n"
            "\\input\\jobname-extra\n"
            "\\input{sec}\n"
            "\\end{document}\n"
        ),
        "sec.tex": "\\section{S}\n",
        "draft.tex": "\\subfile{other}\n",
    }
    graph = resolve_manuscript(members)
    assert graph.status == "organized"
    codes = [(p.code, p.file) for p in graph.problems]
    # verbatim 内的 fake 不登记；动态包含登记；draft.tex 的 subfile 不登记。
    assert ("missing_included_tex", "main.tex") not in codes
    assert any(
        p.code == "unsupported_include_directive"
        and p.file == "main.tex"
        and "动态包含" in p.detail
        for p in graph.problems
    )
    assert not any(p.file == "draft.tex" for p in graph.problems)
    assert "draft.tex" in graph.excluded
    assert [f.name for f in graph.files] == ["main.tex", "sec.tex"]
