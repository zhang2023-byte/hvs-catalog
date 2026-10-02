"""转换链合成测试：联合成功判定、回退顺序、backing CSV 与 ECSV 证据。"""

from __future__ import annotations

import pytest
from materials_helpers import (
    good_latexml_html,
)

from hvs_catalog.materials import convert as conversion
from hvs_catalog.materials.convert import (
    convert_pgf_backing_csv,
    parse_latex_table_excerpt,
)
from hvs_catalog.materials.sandbox import ProcessOutcome


def test_latexml_exit_zero_with_conversion_errors_is_failure():
    """缺宏时退出码 0 但 Status:conversion:2 且有错误节点：必须判失败。"""
    html = good_latexml_html([("FIC-A", "525", "10")]).replace(
        "FIC-A", '<span class="ltx_ERROR">undefined</span>'
    )
    outcome = ProcessOutcome(
        returncode=0,
        stdout=b"",
        stderr=b"... (Fatal:undefined:undefined macro) ...\nStatus:conversion:2\n",
    )
    parsed, problems, _ = conversion.latexml_conversion_verdict(outcome, html)
    assert parsed.status == "failed"
    assert any("Status:conversion:2" in p for p in problems)
    assert any("ltx_ERROR" in p for p in problems)


def test_latexml_missing_status_line_is_failure():
    outcome = ProcessOutcome(returncode=0, stdout=b"", stderr=b"no diagnostics\n")
    parsed, problems, _ = conversion.latexml_conversion_verdict(outcome, "<html/>")
    assert parsed.status == "failed"
    assert any("缺少 Status:conversion" in p for p in problems)


def test_latexml_multiple_output_tables_rejected():
    html = good_latexml_html([("FIC-A", "525", "10")])
    html_twice = html.replace("</body>", html[html.index("<table") :] + "</body>")
    outcome = ProcessOutcome(returncode=0, stdout=b"", stderr=b"Status:conversion:0\n")
    parsed, problems, _ = conversion.latexml_conversion_verdict(outcome, html_twice)
    assert parsed.status == "failed"
    assert any("2 张表" in p for p in problems)


def test_latexml_timeout_and_nonzero_exit_fail():
    outcome = ProcessOutcome(
        returncode=None, stdout=b"", stderr=b"", timed_out=True, error="超时"
    )
    parsed, problems, _ = conversion.latexml_conversion_verdict(outcome, "")
    assert parsed.status == "failed" and any("超时" in p for p in problems)
    outcome = ProcessOutcome(returncode=2, stdout=b"", stderr=b"Status:conversion:0\n")
    parsed, problems, _ = conversion.latexml_conversion_verdict(outcome, "<html/>")
    assert parsed.status == "failed" and any("非零" in p for p in problems)


def test_html_parser_handles_rowspan_colspan_and_multilevel_headers():
    html = """
    <html><body><table>
    <thead>
      <tr><th rowspan="2">Object</th><th colspan="2">Motion</th></tr>
      <tr><th>PM</th><th>Err</th></tr>
    </thead>
    <tbody>
      <tr><td>FIC-A</td><td>8.1</td><td>0.3</td></tr>
      <tr><td>FIC-B</td><td>9.4</td><td>0.2</td></tr>
    </tbody></table></body></html>
    """
    parsed, count = conversion.parse_html_tables(html, method="latexml")
    assert count == 1
    assert parsed.status == "success"
    # 合并格展开：第一行表头 3 列，第二行表头补齐 rowspan
    assert parsed.header_rows[0] == ["Object", "Motion", ""]
    assert parsed.header_rows[1] == ["Object", "PM", "Err"]
    assert parsed.data_rows[0] == ["FIC-A", "8.1", "0.3"]
    # 中性机器列名与多层表头保留
    assert [c["name"] for c in parsed.columns] == ["col_001", "col_002", "col_003"]
    assert parsed.columns[0]["original_header"] == "Object"
    assert parsed.columns[1]["original_header"] == "Motion | PM"


def test_internal_parser_accepts_simple_tabular_only():
    excerpt = (
        "\\begin{tabular}{lcc}\n\\hline\nObj & RV & Err \\\\\n\\hline\n"
        "FIC-A & 525 & 10 \\\\\nFIC-B & \\pm 430 & ... \\\\\n\\hline\\end{tabular}\n"
    )
    parsed = conversion.parse_latex_table_excerpt(excerpt)
    assert parsed.status == "success"
    assert parsed.data_rows[-2] == ["FIC-A", "525", "10"]
    assert parsed.data_rows[0] == ["Obj", "RV", "Err"]  # no guessed header removal
    assert parsed.data_rows[-1][1] == r"\pm 430"  # source string retained
    assert parsed.columns[0]["name"] == "col_001"
    bad = conversion.parse_latex_table_excerpt("no table here")
    assert bad.status == "failed"


def test_deluxetable_internal_parse():
    excerpt = (
        "\\begin{deluxetable}{lcc}\n"
        "\\tablecaption{Synthetic deluxetable}\\label{tab:d}\n"
        "\\tablehead{\\colhead{Obj} & \\colhead{RV} & \\colhead{Err}}\n"
        "\\startdata\nFIC-A & 525 & 10 \\\\\nFIC-B & -430 & 12 \\\\\n"
        "\\enddata\n\\end{deluxetable}\n"
    )
    parsed = conversion.parse_latex_table_excerpt(excerpt)
    assert parsed.status == "success"
    assert parsed.environment == "deluxetable"
    assert parsed.header_rows[0] == ["Obj", "RV", "Err"]
    assert len(parsed.data_rows) == 2


def test_ecsv_roundtrip_preserves_strings_and_metadata():
    parsed = conversion.parse_latex_table_excerpt(
        "\\begin{tabular}{lcc}\n\\hline\nObj & RV & Err \\\\\n\\hline\n"
        "FIC-A & 525 & 10 \\\\\nHVS01 & +9.40 & 0.20 \\\\\n\\hline\\end{tabular}\n"
    )
    assert parsed.status == "success"
    ecsv = conversion.table_ecsv_text(parsed)
    assert "col_001" in ecsv
    problems = conversion.verify_ecsv_roundtrip(ecsv, parsed)
    assert problems == []
    # 有效位数与符号逐字保留
    from hvs_catalog.records.tabular import read_ecsv_records

    records = read_ecsv_records(ecsv.encode("utf-8"))
    assert records.rows[-1]["col_002"] == "+9.40"
    assert records.rows[1]["col_001"] == "FIC-A"
    assert records.rows[0]["col_001"] == "Obj"


def test_pgf_backing_csv_direct_read(tmp_path):
    from hvs_catalog.materials.tables import PgfTypeset

    manuscript = (
        "\\pgfplotstableread[col sep=comma]{backing.csv}\\velocitytable\n"
        "\\begin{table}\n\\pgfplotstabletypeset[columns={name,rv},\n"
        "columns/name/.style={column name=Object},\n"
        "columns/rv/.style={column name=RV [km/s]}]{\\velocitytable}\n"
        "\\end{table}\n"
    )
    typeset = PgfTypeset(
        file="main.tex",
        line=4,
        options="columns={name,rv},columns/name/.style={column name=Object},columns/rv/.style={column name=RV [km/s]}",
        source_arg="\\velocitytable",
        macro="velocitytable",
    )
    backing = b"name,rv,error\nFIC-A,525,10\nFIC-B,-430,12\n"
    parsed, attempt, _unexplained = conversion.convert_pgf_backing_csv(
        typeset,
        manuscript_texts={"main.tex": manuscript},
        document_order=["main.tex"],
        read_member=lambda name: backing if name == "backing.csv" else None,
        referring_file="main.tex",
    )
    assert parsed.status == "success"
    assert attempt.status == "success"
    assert [c["original_header"] for c in parsed.columns] == ["Object", "RV [km/s]"]
    assert parsed.data_rows == [["FIC-A", "525"], ["FIC-B", "-430"]]


def test_pgf_backing_unexplained_transform_keeps_gap():
    from hvs_catalog.materials.tables import PgfTypeset

    manuscript = "\\pgfplotstableread{backing.csv}\\vt\n\\pgfplotstabletypeset[columns={name},columns/name/.style={column name=Obj,string type},discard if={rv}{400}]{\\vt}\n"
    typeset = PgfTypeset(
        file="main.tex",
        line=2,
        options="columns={name},columns/name/.style={column name=Obj,string type},discard if={rv}{400}",
        source_arg="\\vt",
        macro="vt",
    )
    parsed, _attempt, unexplained = conversion.convert_pgf_backing_csv(
        typeset,
        manuscript_texts={"main.tex": manuscript},
        document_order=["main.tex"],
        read_member=lambda name: b"name\nFIC-A\n" if name == "backing.csv" else None,
        referring_file="main.tex",
    )
    assert parsed.status == "failed"
    assert "过滤" in parsed.error
    assert any("string type" in u or "discard if" in u for u in unexplained)


def test_pgf_backing_missing_macro_or_file_fails_honestly():
    from hvs_catalog.materials.tables import PgfTypeset

    typeset = PgfTypeset(
        file="main.tex", line=2, options="", source_arg="\\vt", macro="vt"
    )
    parsed, _, _ = conversion.convert_pgf_backing_csv(
        typeset,
        manuscript_texts={"main.tex": "no read here"},
        document_order=["main.tex"],
        read_member=lambda name: None,
        referring_file="main.tex",
    )
    assert parsed.status == "failed"
    assert "pgfplotstableread" in parsed.error
    typeset2 = PgfTypeset(
        file="main.tex", line=2, options="", source_arg="\\vt", macro="vt"
    )
    parsed2, _, _ = conversion.convert_pgf_backing_csv(
        typeset2,
        manuscript_texts={"main.tex": "\\pgfplotstableread{outside.csv}\\vt\n"},
        document_order=["main.tex"],
        read_member=lambda name: None,
        referring_file="main.tex",
    )
    assert parsed2.status == "failed"
    assert "缺失或越界" in parsed2.error


# ---- 初审反例回归：内部解析器边界、表头对应与逐表上下文 ----


def test_internal_rejects_unresolved_macro_instead_of_warning():
    """未解析宏的表不能被内部回退认证成功（初审反例 unresolved）。"""
    excerpt = "\\begin{tabular}{cc}\n\\hline A & B \\\\\n\\hline \\unknownFIC & 5 \\\\\n\\hline\n\\end{tabular}\n"
    parsed = parse_latex_table_excerpt(excerpt)
    assert parsed.status == "failed"
    assert "未解析宏" in parsed.error


@pytest.mark.integration
def test_native_expands_author_macro_while_internal_does_not_guess(tmp_path):
    from materials_helpers import native_materials_probe

    excerpt = r"\begin{tabular}{cc}\objname{FIC-A} & 525 \\ \end{tabular}"
    assert parse_latex_table_excerpt(excerpt).status == "failed"
    main = r"\documentclass{article}\newcommand{\objname}[1]{\textbf{#1}}\begin{document}\input{sec}\end{document}"
    bundle, rows, _ = native_materials_probe(
        tmp_path, {"main.tex": main, "sec.tex": excerpt}
    )
    assert rows == [[["FIC-A", "525"]]] and bundle["gaps"] == []


def test_internal_rejects_ragged_rows_without_multicolumn():
    """非合并格列数不一致不能补齐认证（初审反例 ragged）。"""
    excerpt = (
        "\\begin{tabular}{ccc}\n\\hline A & B & C \\\\\n\\hline FIC-A & 525 & 10 \\\\\n"
        "FIC-B & 430 \\\\\n\\hline\n\\end{tabular}\n"
    )
    parsed = parse_latex_table_excerpt(excerpt)
    assert parsed.status == "failed"
    assert "行列数不一致" in parsed.error


def test_internal_rejects_two_content_tables_in_one_block():
    """同一表块两份内容表不能取首张（初审反例 two-tables）。"""
    excerpt = (
        "\\begin{table}\n\\begin{tabular}{cc}\nA & B \\\\\nFIC-A & 1 \\\\\n\\end{tabular}\n"
        "\\begin{tabular}{cc}\nC & D \\\\\nFIC-B & 2 \\\\\n\\end{tabular}\n\\end{table}\n"
    )
    parsed = parse_latex_table_excerpt(excerpt)
    assert parsed.status == "failed"
    assert "不能唯一定位" in parsed.error


def test_scan_registers_both_tables_in_one_float():
    """扫描层登记同一浮动体内的两张表，不静默丢第二张。"""
    from hvs_catalog.materials.tables import scan_tables
    from hvs_catalog.materials.texgraph import resolve_manuscript

    members = {
        "main.tex": (
            "\\documentclass{article}\n\\begin{document}\n"
            "\\begin{table}\n\\begin{tabular}{cc}\nA & B \\\\\nFIC-A & 1 \\\\\n\\end{tabular}\n"
            "\\begin{tabular}{cc}\nC & D \\\\\nFIC-B & 2 \\\\\n\\end{tabular}\n\\end{table}\n"
            "\\end{document}\n"
        )
    }
    graph = resolve_manuscript(members)
    blocks = scan_tables(graph)
    assert len(blocks) == 2
    assert len({b.table_id for b in blocks}) == 2


def test_html_text_and_missing_data_rows_are_not_stolen_as_header():
    """明确 thead 时不按数字启发式迁移对象行（初审反例 html-text-data-row）。"""
    from hvs_catalog.materials.convert import parse_html_tables

    html = (
        "<html><body><table><thead><tr><th>Object</th><th>State</th></tr></thead>"
        "<tbody><tr><td>FIC-A</td><td>missing</td></tr>"
        "<tr><td>FIC-B</td><td>525</td></tr></tbody></table></body></html>"
    )
    parsed, count = parse_html_tables(html, method="latexml")
    assert count == 1
    assert parsed.status == "success"
    assert parsed.header_rows == [["Object", "State"]]
    assert parsed.data_rows == [["FIC-A", "missing"], ["FIC-B", "525"]]


def test_html_without_thead_keeps_all_rows_as_data_with_note():
    """无明确表头时全部行按数据保留并登记不确定性，不猜表头。"""
    from hvs_catalog.materials.convert import parse_html_tables

    html = (
        "<html><body><table><tr><td>Object</td><td>State</td></tr>"
        "<tr><td>FIC-A</td><td>missing</td></tr></table></body></html>"
    )
    parsed, _count = parse_html_tables(html, method="latexml")
    assert parsed.status == "success"
    assert parsed.header_rows == []
    assert len(parsed.data_rows) == 2
    assert any("thead" in warning for warning in parsed.warnings)


def test_mapping_preserves_headers_spans_and_presentation_changes():
    """映射附件保留原表头层次、合并格跨度与呈现差异（含 nodata 标记）。"""
    from hvs_catalog.materials.convert import parse_html_tables

    html = (
        "<html><body><table><thead>"
        '<tr><th colspan="2">Measured</th><th>Err</th></tr>'
        "<tr><th>Obj</th><th>RV</th><th>±</th></tr>"
        "</thead><tbody><tr><td>FIC-A</td><td>525</td><td>±10</td></tr>"
        "</tbody></table></body></html>"
    )
    parsed, _count = parse_html_tables(html, method="latexml")
    assert parsed.status == "success"
    assert any(span["colspan"] == 2 for span in parsed.merged_cells)
    assert parsed.data_rows == [["FIC-A", "525", "±10"]]
    assert parsed.presentation_changes == []  # preserve ± instead of rewriting it


def test_pgf_read_selection_uses_assignment_before_typeset(tmp_path):
    """表块之后（含其后文件）的重赋值不作用于该表（初审反例 after.csv）。"""
    from hvs_catalog.materials.tables import PgfTypeset

    typeset = PgfTypeset(
        file="main.tex",
        line=6,
        options="",
        source_arg="\\velocitytable",
        macro="velocitytable",
    )
    manuscript = (
        "\\pgfplotstableread[col sep=comma]{before.csv}\\velocitytable\n"
        "\\input{tables/velocity}\n"
        "\\pgfplotstableread[col sep=comma]{after.csv}\\velocitytable\n"
        "\\pgfplotstabletypeset\\velocitytable\n"
    )
    texts = {"main.tex": manuscript, "tables/velocity.tex": "x\n"}
    before = b"name,rv\nFIC-A,525\n"
    after = b"name,rv\nFIC-B,610\n"

    def read_member(name: str) -> bytes | None:
        return {"before.csv": before, "after.csv": after}.get(name)

    parsed, _attempt, _unexplained = convert_pgf_backing_csv(
        typeset,
        manuscript_texts=texts,
        document_order=["main.tex", "tables/velocity.tex"],
        read_member=read_member,
        referring_file="main.tex",
    )
    # 表块在第 6 行（第二个 typeset）：其之前最近的读入是 after.csv，
    # 其前的表块应取 before.csv。这里 typeset 行 6 恰在 after 读入之后，
    # 选 after（FIC-B）才是位置正确；再验证前一个表块位置取 before。
    assert parsed.status == "success"
    assert parsed.data_rows == [["FIC-B", "610"]]
    early = PgfTypeset(
        file="tables/velocity.tex",
        line=1,
        options="",
        source_arg="\\velocitytable",
        macro="velocitytable",
    )
    parsed2, _a2, _u2 = convert_pgf_backing_csv(
        early,
        manuscript_texts=texts,
        document_order=["main.tex", "tables/velocity.tex"],
        read_member=read_member,
        referring_file="tables/velocity.tex",
        include_positions={"tables/velocity.tex": ("main.tex", 2)},
    )
    assert parsed2.status == "success"
    assert parsed2.data_rows == [["FIC-A", "525"]]


def test_pgf_unknown_row_filter_option_keeps_gap():
    """skip rows between index 等未知过滤必须留缺口（初审反例）。"""
    from hvs_catalog.materials.tables import pgf_unexplained_transforms

    unexplained = pgf_unexplained_transforms(
        "columns/rv/.style={column name=RV, skip rows between index={3}{4}}"
    )
    assert any("skip rows between index" in item for item in unexplained)


def test_pgf_declared_delimiter_is_used_and_unknown_value_fails():
    """不按内容猜分隔符，依照作者声明或 PGF 已知默认。"""
    from hvs_catalog.materials.tables import PgfTypeset

    typeset = PgfTypeset(
        file="main.tex",
        line=2,
        options="",
        source_arg="\\tbl",
        macro="tbl",
    )
    texts = {
        "main.tex": "\\pgfplotstableread[col sep=semicolon]{data.csv}\\tbl\n\\pgfplotstabletypeset\\tbl\n"
    }
    semis = b"name;rv\nFIC-A;525\nFIC-B;430\n"
    parsed, _a, _u = convert_pgf_backing_csv(
        typeset,
        manuscript_texts=texts,
        document_order=["main.tex"],
        read_member=lambda name: semis if name == "data.csv" else None,
        referring_file="main.tex",
    )
    assert parsed.status == "success"
    assert parsed.data_rows[0] == ["FIC-A", "525"]
    mixed = b"name,rv;err\nFIC-A,525;10\n"
    texts["main.tex"] = texts["main.tex"].replace("col sep=semicolon", "col sep=auto")
    parsed2, _a2, _u2 = convert_pgf_backing_csv(
        typeset,
        manuscript_texts=texts,
        document_order=["main.tex"],
        read_member=lambda name: mixed if name == "data.csv" else None,
        referring_file="main.tex",
    )
    assert parsed2.status == "failed"
    assert "分隔符" in parsed2.error


@pytest.mark.integration
def test_native_subdir_root_and_body_renewcommand(tmp_path):
    from materials_helpers import native_materials_probe

    texts = {
        "paper/main.tex": r"\documentclass{article}\input{macros}\begin{document}\renewcommand{\objname}[1]{FIC-B}\input{sec}\end{document}",
        "macros.tex": r"\newcommand{\objname}[1]{FIC-A}",
        "paper/sec.tex": r"\begin{tabular}{cc}\objname{FIC-A} & 525 \\ \end{tabular}",
    }
    bundle, rows, _ = native_materials_probe(tmp_path, texts)
    assert rows == [[["FIC-B", "525"]]] and bundle["gaps"] == []
