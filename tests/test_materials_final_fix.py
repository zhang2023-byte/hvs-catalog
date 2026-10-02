"""R11 final regressions for second-review counterexamples; all materials are synthetic."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from materials_helpers import (
    FakeConverterRunner,
    make_tar_gz,
    native_materials_probe,
    prepared_space,
)

from hvs_catalog.materials.service import MaterialsService

# ---- Findings 1/2/3: macro event order, equivalent subset, and preamble cycles ----


@pytest.mark.integration
def test_context_parent_after_child_uses_before_include_definition(tmp_path):
    texts = {
        "main.tex": r"\documentclass{article}\newcommand{\obj}{FIC-A}\begin{document}\input{sec}\renewcommand{\obj}{FIC-B}\end{document}",
        "sec.tex": r"\begin{tabular}{ll}\obj & 525 \\ \end{tabular}",
    }
    bundle, rows, consumer = native_materials_probe(tmp_path, texts)
    assert rows == [[["FIC-A", "525"]]]
    assert bundle["gaps"] == [] and consumer["materials_complete"]


@pytest.mark.integration
def test_context_local_after_include_wins(tmp_path):
    texts = {
        "main.tex": r"\documentclass{article}\begin{document}\input{macros}\renewcommand{\obj}{FIC-A}\begin{tabular}{ll}\obj & 525 \\ \end{tabular}\end{document}",
        "macros.tex": r"\newcommand{\obj}{FIC-Z}",
    }
    bundle, rows, _ = native_materials_probe(tmp_path, texts)
    assert rows == [[["FIC-A", "525"]]] and bundle["gaps"] == []


@pytest.mark.integration
def test_commented_redefinition_not_in_context(tmp_path):
    main = "\\documentclass{article}\n\\newcommand{\\obj}{FIC-A}\n\\begin{document}\n% \\renewcommand{\\obj}{FIC-B}\n\\begin{tabular}{ll}\\obj & 525 \\\\ \\end{tabular}\n\\end{document}"
    bundle, rows, _ = native_materials_probe(tmp_path, {"main.tex": main})
    assert rows == [[["FIC-A", "525"]]] and bundle["gaps"] == []


@pytest.mark.integration
@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        (r"\newcommand{\obj}{FIC-B}\providecommand{\obj}{FIC-A}", "FIC-B"),
        (r"\providecommand{\obj}{FIC-A}\providecommand{\obj}{FIC-Z}", "FIC-A"),
        (r"\def\obj{FIC-B}\renewcommand{\obj}{FIC-A}", "FIC-A"),
        (r"\newcommand{\obj}[1][FIC-X]{#1}", "FIC-X"),
    ],
)
def test_provide_def_order_and_native_default_parameters(
    tmp_path, definition, expected
):
    main = (
        r"\documentclass{article}"
        + definition
        + r"\begin{document}\begin{tabular}{ll}\obj & 525 \\ \end{tabular}\end{document}"
    )
    bundle, rows, _ = native_materials_probe(tmp_path, {"main.tex": main})
    assert rows == [[[expected, "525"]]] and bundle["gaps"] == []


def test_preamble_cycle_prepares_without_recursion_crash(tmp_path):
    """Register a context gap for preamble cycles instead of crashing recursively
    (e2e-cycle-preamble).
    """
    main = (
        "\\documentclass{article}\n"
        "\\input{macros}\n"
        "\\begin{document}\n"
        "\\begin{tabular}{ll}FIC-A & 525 \\\\\n\\end{tabular}\n"
        "\\end{document}\n"
    )
    members = {"main.tex": main.encode(), "macros.tex": b"\\input{main}\n"}
    space, imported = prepared_space(tmp_path, make_tar_gz(members))
    service = MaterialsService(space, runner=FakeConverterRunner())
    handle, report = service.prepare_materials(
        request_key="rk", paper=imported.canonical_id
    )
    assert report.outcome == "finished"
    bundle = service.get_prepared_input(handle.execution_id)
    assert bundle["summary"]["table_success"] == 1
    space.close()


# ---- Finding 4: shared include resolution and repeated-include registration ----


def test_graph_resolves_subdir_root_relative_include():
    """Resolve input{macros} in a subdirectory main document relative to the package root
    (graph-subdir).
    """
    from hvs_catalog.materials.texgraph import resolve_manuscript

    members = {
        "paper/main.tex": (
            b"\\documentclass{article}\n\\begin{document}\n"
            b"\\input{macros}\n\\end{document}\n"
        ),
        "macros.tex": b"x\n",
    }
    graph = resolve_manuscript({name: data.decode() for name, data in members.items()})
    assert graph.status == "organized"
    assert [f.name for f in graph.files] == ["paper/main.tex", "macros.tex"]
    assert graph.problems == []


def test_repeated_include_keeps_edges_for_native_execution():
    """Save each original file once; native conversion separately locates each actual
    execution.
    """
    from hvs_catalog.materials.texgraph import resolve_manuscript

    members = {
        "main.tex": (
            "\\documentclass{article}\n\\begin{document}\n"
            "\\input{rows}\n\\input{rows}\n\\end{document}\n"
        ),
        "rows.tex": "a & b \\\\\n",
    }
    graph = resolve_manuscript(members)
    assert graph.problems == []
    assert graph.edges["main.tex"] == ["rows.tex", "rows.tex"]
    assert any("再次包含" in note for note in graph.notes)


# ---- Findings 5/22: standard ECSV logical records and CSV width ----


def test_ecsv_preserves_backslash_quotes_multiline_and_hash():
    from hvs_catalog.records.tabular import read_ecsv_records

    space_ecsv = (
        "# %ECSV 1.0\n# ---\n# datatype:\n"
        "# - {name: object, datatype: string}\n"
        "# - {name: note, datatype: string}\n"
        "# schema: astropy-2.0\nobject note\n"
        "FIC-A A\\B\n"
        'FIC-B "say ""yes"""\n'
    )
    records = read_ecsv_records(space_ecsv.encode())
    assert records.rows[0]["note"] == "A\\B"
    assert records.rows[1]["note"] == 'say "yes"'

    multiline = space_ecsv.replace("FIC-A A\\B", 'FIC-A "alpha\nbeta"').replace(
        'FIC-B "say ""yes"""', "FIC-B tail"
    )
    records2 = read_ecsv_records(multiline.encode())
    assert records2.rows[0]["note"] == "alpha\nbeta"

    hashed = (
        "# %ECSV 1.0\n# ---\n# delimiter: ','\n# datatype:\n"
        "# - {name: object, datatype: string}\n"
        "# - {name: note, datatype: string}\n"
        "# schema: astropy-2.0\nobject,note\n"
        'FIC-A,"alpha\n# beta"\n'
    )
    records3 = read_ecsv_records(hashed.encode())
    assert records3.rows == ({"object": "FIC-A", "note": "alpha\n# beta"},)

    empty_first = (
        "# %ECSV 1.0\n# ---\n# delimiter: ','\n# datatype:\n"
        "# - {name: object, datatype: string}\n"
        "# - {name: note, datatype: string}\n"
        "# schema: astropy-2.0\nobject,note\n,\nFIC-A,x\n"
    )
    records4 = read_ecsv_records(empty_first.encode())
    assert records4.rows[0] == {"object": "", "note": ""}


def test_csv_without_header_rejects_ragged_rows():
    from hvs_catalog.records.tabular import TabularReadError, read_csv_records

    with pytest.raises(TabularReadError, match="宽度不一致"):
        read_csv_records(b"FIC-A,525\nFIC-B\n", has_header=False)


# ---- Findings 6/8/21: grid checks, merged-cell locations, longtable sections, and finite bounds ----


def test_html_ragged_row_fails_and_empty_row_kept():
    from hvs_catalog.materials.convert import parse_html_tables

    ragged = (
        "<html><body><table><thead><tr><th>Obj</th><th>RV</th></tr></thead>"
        "<tbody><tr><td>FIC-A</td><td>525</td></tr>"
        "<tr><td>FIC-B</td></tr></tbody></table></body></html>"
    )
    parsed, _ = parse_html_tables(ragged, method="latexml")
    assert parsed.status == "failed"
    assert "行列数不一致" in parsed.error

    empty = (
        "<html><body><table><thead><tr><th>Obj</th><th>RV</th></tr></thead>"
        "<tbody><tr><td></td><td></td></tr>"
        "<tr><td>FIC-B</td><td>525</td></tr></tbody></table></body></html>"
    )
    parsed2, _ = parse_html_tables(empty, method="latexml")
    assert parsed2.status == "success"
    assert parsed2.data_rows[0] == ["", ""]


def test_internal_multicolumn_positions_and_headerless_and_longtable():
    from hvs_catalog.materials.convert import parse_latex_table_excerpt

    spanned = parse_latex_table_excerpt(
        "\\begin{tabular}{lll}\n\\hline H1 & H2 & H3 \\\\\n\\hline "
        "\\multicolumn{2}{l}{FIC-A} & 525 \\\\\n\\hline\n\\end{tabular}\n"
    )
    assert spanned.status == "success"
    assert spanned.data_rows[-1] == ["FIC-A", "", "525"]
    assert spanned.data_rows[0] == ["H1", "H2", "H3"]

    headerless = parse_latex_table_excerpt(
        "\\begin{tabular}{ll}\nFIC-A & 525 \\\\\nFIC-B & 430 \\\\\n\\end{tabular}\n"
    )
    assert headerless.status == "success"
    assert headerless.header_rows == []
    assert headerless.data_rows[0] == ["FIC-A", "525"]

    longtable = parse_latex_table_excerpt(
        "\\begin{longtable}{ll}\n\\hline Object & RV \\\\\n\\hline\n"
        "\\endfirsthead\n\\hline Object & RV \\\\\n\\hline\n\\endhead\n"
        "\\hline cont \\\\\n\\hline\n\\endfoot\n"
        "FIC-A & 525 \\\\\n\\end{longtable}\n"
    )
    assert longtable.status == "success"
    assert longtable.header_rows == [["Object", "RV"]]
    assert longtable.data_rows == [["FIC-A", "525"]]


def test_grid_bounds_reject_huge_expansion():
    from hvs_catalog.materials.convert import parse_html_tables

    huge = (
        "<html><body><table><tbody>"
        '<tr><td colspan="999999999">x</td>'
        '<td colspan="999999999">y</td>'
        '<td colspan="999999999">z</td></tr>'
        "</tbody></table></body></html>"
    )
    parsed, _ = parse_html_tables(huge, method="latexml")
    assert parsed.status == "failed"
    assert "有限边界" in parsed.error


# ---- Finding 7: Pandoc content correspondence ----


def test_pandoc_object_loss_fails_correspondence():
    from hvs_catalog.materials.flow import _pandoc_verdict
    from hvs_catalog.materials.sandbox import ProcessOutcome

    excerpt = (
        "\\begin{tabular}{ll}\n\\hline Object & RV \\\\\n\\hline "
        "\\undefinedObj{FIC-A} & 525 \\\\\n\\hline\n\\end{tabular}\n"
    )
    html = (
        "<html><body><table><tr><td>Object</td><td>RV</td></tr>"
        "<tr><td></td><td>525</td></tr></table></body></html>"
    )
    outcome = ProcessOutcome(
        returncode=0,
        stdout=b"",
        stderr=b"",
        timed_out=False,
        exceeded_output=False,
        error="",
    )
    parsed, problems, count = _pandoc_verdict(outcome, html.encode(), excerpt=excerpt)
    assert count == 1
    assert any("转换对应核对失败" in p for p in problems)
    assert parsed.status == "failed"


# ---- Finding 9: backing CSV positional order, allowlist, and absolute paths ----


def test_pgf_read_selection_sorted_and_whitelist_and_absolute():
    from hvs_catalog.materials.convert import convert_pgf_backing_csv
    from hvs_catalog.materials.tables import PgfTypeset, pgf_unexplained_transforms

    assert pgf_unexplained_transforms("sort=true") == ["sort"]
    assert pgf_unexplained_transforms(
        "columns={name},columns/name/.style={create col/expr={1}}"
    ) == ["columns/name/.style 中的 create col/expr"]

    # Root reads b.csv after input; its later table uses b, not the included file's c.
    typeset = PgfTypeset(
        file="main.tex",
        line=6,
        options="",
        source_arg="\\t",
        macro="t",
    )
    texts = {
        "main.tex": (
            "\\input{sub}\n\\pgfplotstableread[col sep=comma]{b.csv}\\t\n\\pgfplotstabletypeset\\t\n"
        ),
        "sub.tex": "\\pgfplotstableread[col sep=comma]{c.csv}\\t\nx\n",
    }

    def read_member(name):
        return {
            "b.csv": b"name,rv\nFIC-b,525\n",
            "c.csv": b"name,rv\nFIC-c,525\n",
        }.get(name)

    parsed, _attempt, _unexplained = convert_pgf_backing_csv(
        typeset,
        manuscript_texts=texts,
        document_order=["main.tex", "sub.tex"],
        read_member=read_member,
        referring_file="main.tex",
        include_positions={"sub.tex": ("main.tex", 1)},
    )
    assert parsed.status == "success"
    assert parsed.data_rows[0][0] == "FIC-b"
    assert parsed.backing_member == "b.csv"

    # Absolute paths never map to package members.
    abs_typeset = PgfTypeset(
        file="main.tex",
        line=2,
        options="",
        source_arg="\\tt",
        macro="tt",
    )
    texts2 = {
        "main.tex": "\\pgfplotstableread{/etc/data.csv}\\tt\n"
        "\\pgfplotstabletypeset\\tt\n"
    }
    parsed2, _a2, _u2 = convert_pgf_backing_csv(
        abs_typeset,
        manuscript_texts=texts2,
        document_order=["main.tex"],
        read_member=lambda name: b"name,rv\nFIC-A,525\n",
        referring_file="main.tex",
    )
    assert parsed2.status == "failed"
    assert "缺失或越界" in parsed2.error or "不能确定" in parsed2.error


# ---- Findings 11/12: standalone supplementary tables and unresolved resources ----


def test_attached_tables_and_unparsed_member_and_link_gaps(tmp_path):
    main = (
        "\\documentclass{article}\n\\begin{document}\n"
        "Data in objects.ecsv and measurements.h5; "
        "mirror at \\url{https://example.org/objects.json}.\n"
        "\\end{document}\n"
    )
    members = {
        "main.tex": main.encode(),
        "objects.ecsv": (
            b"# %ECSV 1.0\n# ---\n# datatype:\n"
            b"# - {name: object, datatype: string}\n"
            b"# - {name: rv, datatype: string}\n"
            b"# schema: astropy-2.0\nobject rv\nFIC-A 525\n"
        ),
        "measurements.h5": b"\x89HDF\r\n\x1a\nbinary",
    }
    space, imported = prepared_space(tmp_path, make_tar_gz(members))
    service = MaterialsService(space, runner=FakeConverterRunner())
    handle, report = service.prepare_materials(
        request_key="rk", paper=imported.canonical_id
    )
    assert report.outcome == "finished"
    bundle = service.get_prepared_input(handle.execution_id)
    attached = [t for t in bundle["tables"] if t["environment"] == "attached"]
    assert len(attached) == 1
    assert attached[0]["file"] == "objects.ecsv"
    assert attached[0]["source_file_id"].startswith("ecsv:att-")
    descriptions = [g["description"] for g in bundle["gaps"]]
    assert any("measurements.h5" in d and "未支持" in d for d in descriptions)
    assert any("https://example.org/objects.json" in d for d in descriptions)
    # The consumer can read supplementary tables.
    result = service.consume_prepared_input(handle.execution_id)
    assert result["ok"], result["problems"]
    space.close()


def test_latin1_declared_member_not_marked_unreadable(tmp_path):
    """Derive a latin-1-declared member without an unreadable gap (latin-ready)."""
    main = (
        "% !TeX encoding = latin-1\n"
        "\\documentclass{article}\n\\begin{document}\n"
        "Gravi%t f\xfcr \xf6laf\n\\end{document}\n"
    ).encode("latin-1")
    space, imported = prepared_space(tmp_path, make_tar_gz({"main.tex": main}))
    service = MaterialsService(space, runner=FakeConverterRunner())
    handle, report = service.prepare_materials(
        request_key="rk", paper=imported.canonical_id
    )
    assert report.outcome == "finished"
    bundle = service.get_prepared_input(handle.execution_id)
    assert bundle["body"]["status"] == "organized"
    assert not [g for g in bundle["gaps"] if g["reason"] == "unreadable"]
    space.close()


# ---- Findings 14/16: gap synchronization and operation-key recovery ----


def test_unsupported_range_gap_enters_prepared_revision(tmp_path):
    main = (
        "\\documentclass{article}\n\\begin{document}\n"
        "We list HVS1-100 in the sample.\n\\end{document}\n"
    )
    space, imported = prepared_space(tmp_path, make_tar_gz({"main.tex": main.encode()}))
    service = MaterialsService(space, runner=FakeConverterRunner())
    handle, report = service.prepare_materials(
        request_key="rk", paper=imported.canonical_id
    )
    assert report.outcome == "finished"
    bundle = service.get_prepared_input(handle.execution_id)
    assert any("HVS1-100" in g["description"] for g in bundle["gaps"])
    from hvs_catalog.records.common import RecordRef

    source = space.store().read_record(
        RecordRef.model_validate(bundle["prepared_source_ref"])
    )
    assert any("HVS1-100" in g.description for g in source.data.gaps)
    space.close()


def test_interleaved_recovery_reuses_own_receipt(tmp_path):
    """A crashes after commit, B finishes first, then A resumes: reuse receipts by each
    operation's own key.
    """
    import hvs_catalog.materials.service as service_module
    from hvs_catalog.materials.flow import MaterialsFlow

    space, imported = prepared_space(tmp_path)

    def crash() -> None:
        raise RuntimeError("synthetic crash after revision commit")

    flow_crash = MaterialsFlow(
        runner=FakeConverterRunner(),
        fault_hooks={"after_revision_commit": crash},
    )
    original = service_module.MaterialsFlow
    service_module.MaterialsFlow = lambda **kw: flow_crash  # type: ignore[assignment]
    service = MaterialsService(space, runner=FakeConverterRunner())
    try:
        handle_a, report_a = service.prepare_materials(
            request_key="rk-a", paper=imported.canonical_id
        )
    finally:
        service_module.MaterialsFlow = original  # type: ignore[assignment]
    assert report_a.outcome == "failed"
    # Complete same-source execution B serially.
    _handle_b, report_b = service.prepare_materials(
        request_key="rk-b", paper=imported.canonical_id
    )
    assert report_b.outcome == "finished"
    # Resume A: current belongs to B, but A must still reuse its own receipt.
    resumed = service.resume_preparation(handle_a.execution_id)
    assert resumed.outcome == "finished", resumed.errors
    bundle = service.get_prepared_input(handle_a.execution_id)
    assert bundle["prepared_source_ref"]["revision"] == (
        imported.material_ref.revision + 1
    )
    result = service.consume_prepared_input(handle_a.execution_id)
    assert result["ok"], result["problems"]
    space.close()


# ---- Finding 19: caption correspondence ----


def test_deluxetable_tablecaption_and_pgf_float_association():
    from hvs_catalog.materials.tables import scan_tables
    from hvs_catalog.materials.texgraph import resolve_manuscript

    members = {
        "main.tex": (
            "\\documentclass{article}\n\\begin{document}\n"
            "\\begin{deluxetable}{ll}\n"
            "\\tablecaption{Stellar sample\\label{tab:deluxe}}\n"
            "\\tablehead{Obj & RV}\n\\startdata\nFIC-A & 525 \\\\\n"
            "\\enddata\n\\end{deluxetable}\n"
            "\\begin{table}\n\\caption{Velocities\\label{tab:v}}\n"
            "\\pgfplotstabletypeset{\\vt}\n\\end{table}\n"
            "\\end{document}\n"
        )
    }
    blocks = scan_tables(resolve_manuscript(members))
    by_env = {b.environment: b for b in blocks}
    assert "Stellar sample" in by_env["deluxetable"].caption
    assert by_env["deluxetable"].label == "tab:deluxe"
    assert "Velocities" in by_env["pgfplotstable"].caption
    assert by_env["pgfplotstable"].label == "tab:v"


# ---- Finding 20: forbidden binding reads include subdirectories; real sandbox-exec, skipped off macOS ----


@pytest.mark.integration
def test_binding_deny_covers_nested_directories(tmp_path):
    pytest.importorskip("hvs_catalog.materials.sandbox")
    from hvs_catalog.materials.sandbox import (
        SandboxedConverterRunner,
        _sandbox_ready,
    )

    if _sandbox_ready() is not None:
        pytest.skip(_sandbox_ready())
    runner = SandboxedConverterRunner()
    workdir = tmp_path / "conv"
    workdir.mkdir()
    (workdir / "bindings").mkdir()
    (workdir / "bindings" / "test.pm").write_text("SYNTHETIC", encoding="utf-8")
    script = "cat bindings/test.pm"
    outcome = runner.run("pandoc", ["/bin/sh", "-c", script], workdir=workdir)
    assert outcome.stdout == b"", "子目录绑定文件必须拒读"


# ---- Findings 17/18: output checks on every exit and residual process-group cleanup in a real sandbox ----


@pytest.mark.integration
def test_fast_exit_nested_output_counted_and_orphan_group_cleaned(tmp_path):
    from hvs_catalog.materials.sandbox import (
        MAX_CONVERTER_OUTPUT_BYTES,
        SandboxedConverterRunner,
        _sandbox_ready,
    )

    if _sandbox_ready() is not None:
        pytest.skip(_sandbox_ready())
    runner = SandboxedConverterRunner()
    workdir = tmp_path / "out"
    workdir.mkdir()
    count = MAX_CONVERTER_OUTPUT_BYTES // 1048576 + 2
    script = (
        "mkdir -p nested && "
        f"dd if=/dev/zero of=nested/large.bin bs=1048576 count={count}; "
        "exit 0"
    )
    outcome = runner.run("pandoc", ["/bin/sh", "-c", script], workdir=workdir)
    assert outcome.exceeded_output is True
    assert "超过" in outcome.error

    # Orphan child writes after normal parent exit; clean it up before returning.
    workdir2 = tmp_path / "orphan"
    workdir2.mkdir()
    orphan_script = "(sleep 1 && echo marker > marker.txt) & exit 0"
    outcome2 = runner.run("pandoc", ["/bin/sh", "-c", orphan_script], workdir=workdir2)
    assert outcome2.returncode == 0
    import time

    time.sleep(1.5)
    assert not (workdir2 / "marker.txt").exists(), "后代进程组必须被清理"


# ---- Finding 24: CLI boundary ----


def test_cli_prepare_version_must_be_positive(capsys):
    import argparse

    from hvs_catalog.cli_materials import _run_prepare, build_materials_parser

    parser = argparse.ArgumentParser()
    build_materials_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        [
            "materials",
            "prepare",
            "--space",
            "/tmp/unused-space",
            "--paper",
            "2501.09999",
            "--version",
            "-1",
            "--request-key",
            "rk",
        ]
    )
    rc = _run_prepare(args)
    assert rc == 2
    assert "正整数" in capsys.readouterr().err
