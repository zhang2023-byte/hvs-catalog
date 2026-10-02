"""Full-context conversion, native origins and public completeness regressions.

Synthetic inputs only. Native tests exercise the installed safe converter, not
an independently reimplemented TeX state machine.
"""

from __future__ import annotations

import json

import pytest
from materials_helpers import (
    FakeConverterRunner,
    make_tar_gz,
    native_materials_probe,
    prepared_space,
)

from hvs_catalog.materials.service import MaterialsService
from hvs_catalog.records.common import ArtifactRef, RecordRef
from hvs_catalog.records.source import AffectedItem, AffectedTargets, Gap
from hvs_catalog.records.tabular import read_table_records
from hvs_catalog.runtime.execution import get_execution

TABLE = r"\begin{tabular}{lc}\obj & 525 \\ \end{tabular}"


def document(body, preamble=r"\newcommand{\obj}{FIC-A}"):
    return (
        r"\documentclass{article}"
        + preamble
        + r"\begin{document}"
        + body
        + r"\end{document}"
    )


@pytest.mark.parametrize(
    "prefix",
    [
        r"{\renewcommand{\obj}{FIC-B}}",
        r"\begingroup\renewcommand{\obj}{FIC-B}\endgroup",
        r"\iffalse\renewcommand{\obj}{FIC-B}\fi",
        r"\iftrue\else\renewcommand{\obj}{FIC-B}\fi",
        r"\newcommand{\unused}{\renewcommand{\obj}{FIC-B}}",
    ],
)
def test_native_context_does_not_leak_inactive_or_local_definitions(tmp_path, prefix):
    bundle, rows, consumer = native_materials_probe(
        tmp_path, {"main.tex": document(prefix + TABLE)}
    )
    assert rows == [[["FIC-A", "525"]]]
    assert bundle["gaps"] == []
    assert consumer["ok"] and consumer["materials_complete"]


def test_native_same_line_order_and_unlabelled_tables(tmp_path):
    source = document(TABLE + r"\renewcommand{\obj}{FIC-B}" + TABLE)
    bundle, rows, _ = native_materials_probe(tmp_path, {"main.tex": source})
    assert rows == [[["FIC-A", "525"]], [["FIC-B", "525"]]]
    tables = bundle["tables"]
    assert len({t["table_id"] for t in tables}) == 2
    assert (
        tables[0]["mapping"]["native_origin"]["offset"]
        != tables[1]["mapping"]["native_origin"]["offset"]
    )
    assert all(t["mapping"]["granularity"] == "table-block" for t in tables)
    assert bundle["gaps"] == []


def test_native_repeated_include_retains_each_execution(tmp_path):
    main = document(r"\input{rows}\renewcommand{\obj}{FIC-B}\input{rows}")
    bundle, rows, _ = native_materials_probe(
        tmp_path, {"main.tex": main, "rows.tex": TABLE}
    )
    assert rows == [[["FIC-A", "525"]], [["FIC-B", "525"]]]
    tables = bundle["tables"]
    assert len({t["table_id"] for t in tables}) == 2
    assert [t["mapping"]["occurrence"] for t in tables] == [1, 2]
    assert (
        tables[0]["mapping"]["native_origin"] == tables[1]["mapping"]["native_origin"]
    )
    assert bundle["gaps"] == []


def test_native_two_content_tables_in_one_float_keep_distinct_origins(tmp_path):
    body = (
        r"\begin{table}\caption[Short]{Full synthetic caption}"
        + TABLE
        + r"\renewcommand{\obj}{FIC-C}"
        + TABLE
        + r"\end{table}"
    )
    bundle, rows, _ = native_materials_probe(tmp_path, {"main.tex": document(body)})
    assert rows == [[["FIC-A", "525"]], [["FIC-C", "525"]]]
    assert len({t["table_id"] for t in bundle["tables"]}) == 2
    assert all(t["caption"] == "Full synthetic caption" for t in bundle["tables"])
    assert bundle["gaps"] == []


def test_native_author_style_and_bbl_are_actual_dependencies(tmp_path):
    main = document(r"\input{refs.bbl}" + TABLE, r"\usepackage{synthetic}")
    bundle, rows, _ = native_materials_probe(
        tmp_path,
        {
            "main.tex": main,
            "synthetic.sty": r"\ProvidesPackage{synthetic}\newcommand{\obj}{FIC-A}",
            "refs.bbl": r"\renewcommand{\obj}{FIC-B}",
            "unused.tex": "Unrelated draft without a manuscript root.",
        },
    )
    assert rows == [[["FIC-B", "525"]]]
    sources = bundle["tables"][0]["context_sources"]
    assert {"synthetic.sty", "refs.bbl", "main.tex"} <= set(sources)
    assert "unused.tex" not in sources and bundle["gaps"] == []


def test_native_default_arguments_and_explicit_argument_keep_both_rows(tmp_path):
    main = document(
        r"\begin{tabular}{lc}\obj & 525 \\ \obj[FIC-B] & 610 \\ \end{tabular}",
        r"\newcommand{\obj}[1][FIC-A]{#1}",
    )
    bundle, rows, _ = native_materials_probe(tmp_path, {"main.tex": main})
    assert rows == [[["FIC-A", "525"], ["FIC-B", "610"]]] and bundle["gaps"] == []


def test_native_undefined_object_is_a_gap_not_empty_identity_success(tmp_path):
    bundle, rows, consumer = native_materials_probe(
        tmp_path,
        {
            "main.tex": document(TABLE, ""),
            "good.csv": "name,rv\nFIC-C,610\n",
        },
    )
    assert bundle["tables"][0]["status"] == "failed"
    assert rows == [[["FIC-C", "610"]]]
    assert bundle["gaps"] and consumer["ok"]
    assert not consumer["materials_complete"]
    assert all(
        gap.get("affected_targets", {}).get("scope", "unknown") == "unknown"
        for gap in bundle["gaps"]
    )


def test_shadowed_local_style_cannot_silently_certify_wrong_object(tmp_path):
    main = document(TABLE, r"\newcommand{\obj}{FIC-A}\usepackage{booktabs}")
    bundle, rows, consumer = native_materials_probe(
        tmp_path,
        {
            "main.tex": main,
            "booktabs.sty": r"\ProvidesPackage{booktabs}\renewcommand{\obj}{FIC-B}",
        },
    )
    # Either the actual author file was loaded, or its absence is explicit.
    assert (rows == [[["FIC-B", "525"]]] and not bundle["gaps"]) or (
        bundle["gaps"] and not consumer["materials_complete"]
    )
    assert not (rows == [[["FIC-A", "525"]]] and not bundle["gaps"])


def _prepare(tmp_path, members, *, runner=None, **options):
    space, imported = prepared_space(
        tmp_path,
        make_tar_gz(
            {
                name: value.encode() if isinstance(value, str) else value
                for name, value in members.items()
            }
        ),
    )
    service = MaterialsService(space, runner=runner or FakeConverterRunner())
    handle, report = service.prepare_materials(
        request_key="source", paper=imported.canonical_id, **options
    )
    assert report.outcome == "finished", report.to_dict()
    return space, service, handle, service.get_prepared_input(handle.execution_id)


def test_missing_native_locator_does_not_guess_first_table(tmp_path):
    xml = '<document xmlns="http://dlmf.nist.gov/LaTeXML"><tabular><tbody><tr><td>FIC-A</td><td>525</td></tr></tbody></tabular></document>'
    space, service, handle, bundle = _prepare(
        tmp_path,
        {"main.tex": document(TABLE)},
        runner=FakeConverterRunner(latexml_xml=xml),
    )
    try:
        assert not any(t["status"] == "success" for t in bundle["tables"])
        assert (
            bundle["gaps"]
            and not service.consume_prepared_input(handle.execution_id)[
                "materials_complete"
            ]
        )
    finally:
        space.close()


def test_same_bytes_at_two_attached_paths_keep_two_source_identities(tmp_path):
    space, _, _, bundle = _prepare(
        tmp_path,
        {
            "main.tex": document("", ""),
            "a/data.csv": "name,rv\nFIC-A,00525\n",
            "b/data.csv": "name,rv\nFIC-A,00525\n",
        },
    )
    try:
        assert len(bundle["tables"]) == 2
        assert len({t["table_id"] for t in bundle["tables"]}) == 2
        assert len({t["source_file_id"] for t in bundle["tables"]}) == 2
        prepared = space.store().read_record(RecordRef(**bundle["prepared_source_ref"]))
        for table in bundle["tables"]:
            source = next(
                f for f in prepared.data.files if f.file_id == table["source_file_id"]
            )
            assert source.derived_from == ["member:" + table["file"]]
    finally:
        space.close()


def test_attached_ecsv_units_descriptions_meta_and_raw_digits_survive(tmp_path):
    ecsv = """# %ECSV 1.0
# ---
# datatype:
# - {name: object, datatype: string}
# - {name: rv, datatype: float64, unit: km / s, description: Original heliocentric RV, meta: {frame: heliocentric}}
# delimiter: ','
# meta: {synthetic: true}
# schema: astropy-2.0
object,rv
FIC-A,00525.00
"""
    space, _, _, bundle = _prepare(
        tmp_path, {"main.tex": document("", ""), "data.ecsv": ecsv}
    )
    try:
        table = bundle["tables"][0]
        assert table["status"] == "success", table
        output = space.read_file(
            ArtifactRef(path=table["ecsv_path"], sha256=table["ecsv_sha256"])
        )
        records = read_table_records(output, name="table.ecsv")
        assert records.rows[0]["rv"] == "00525.00"
        assert records.column_metadata["rv"]["unit"] == "km / s"
        assert (
            records.column_metadata["rv"]["description"] == "Original heliocentric RV"
        )
        assert records.column_metadata["rv"]["meta"] == {"frame": "heliocentric"}
        assert records.table_metadata == {"synthetic": True}
        mapping = json.loads(
            space.read_file(
                ArtifactRef(path=table["mapping_path"], sha256=table["mapping_sha256"])
            )
        )
        assert mapping["columns"][1]["datatype"] == "float64"
    finally:
        space.close()


def test_unsupported_ecsv_json_subtype_remains_unparsed(tmp_path):
    ecsv = '# %ECSV 1.0\n# ---\n# datatype:\n# - {name: object, datatype: string, subtype: json}\nobject\n"{""name"": ""FIC-A""}"\n'
    space, service, handle, bundle = _prepare(
        tmp_path, {"main.tex": document("", ""), "data.ecsv": ecsv}
    )
    try:
        assert bundle["tables"][0]["status"] == "failed" and bundle["gaps"]
        assert not service.consume_prepared_input(handle.execution_id)[
            "materials_complete"
        ]
    finally:
        space.close()


@pytest.mark.parametrize(
    ("body", "notation", "status", "members", "gap"),
    [
        (
            "The stars HVS1 to HVS3,5 are listed.",
            "HVS1 to HVS3,5",
            "expanded",
            ["HVS1", "HVS2", "HVS3", "HVS5"],
            False,
        ),
        ("The stars HVS1-3,5A are unresolved.", "HVS1-3,5A", "unsupported", [], True),
        (
            "The stars HVS1-3 and others are listed.",
            "HVS1-3 and others",
            "expanded",
            ["HVS1", "HVS2", "HVS3"],
            True,
        ),
        (
            "Temperature T10-20 K is the physical interval.",
            "T10-20",
            "ambiguous",
            [],
            True,
        ),
        (r"\ifnum1=0 The stars HVS1-3 are here.\fi", "HVS1-3", "ambiguous", [], True),
    ],
)
def test_discovered_ranges_require_context_and_keep_full_unknown_scope(
    tmp_path, body, notation, status, members, gap
):
    space, service, handle, bundle = _prepare(
        tmp_path, {"main.tex": document(body, "")}
    )
    try:
        result = next(item for item in bundle["ranges"] if item["notation"] == notation)
        assert result["status"] == status and result["identifiers"] == members
        assert bool(bundle["gaps"]) == gap
        prepared = space.store().read_record(RecordRef(**bundle["prepared_source_ref"]))
        assert {g.gap_id for g in prepared.data.gaps} == {
            g["gap_id"] for g in bundle["gaps"]
        }
        consumer = service.consume_prepared_input(handle.execution_id)
        assert consumer["gaps"] == bundle["gaps"]
        assert service.get_preparation(handle.execution_id)["summary"][
            "gap_count"
        ] == len(bundle["gaps"])
    finally:
        space.close()


def test_preamble_unused_macro_and_false_branch_ranges_are_not_discovered(tmp_path):
    body = r"\newcommand{\unused}{The stars HVS1-3}\iffalse The stars HVS4-6\fi The stars HVS7-8 are here."
    preamble = "The stars HVS9-10 "  # raw preamble text is not paper-body evidence
    space, _, _, bundle = _prepare(tmp_path, {"main.tex": document(body, preamble)})
    try:
        assert [item["notation"] for item in bundle["ranges"]] == ["HVS7-8"]
        assert bundle["ranges"][0]["identifiers"] == ["HVS7", "HVS8"]
    finally:
        space.close()


def test_table_ranges_use_identity_column_not_adjacent_physical_column(tmp_path):
    body = r"\begin{tabular}{ll}Object & RV \\ HVS1-3 & V10-20 \\ \end{tabular}"
    space, _, _, bundle = _prepare(tmp_path, {"main.tex": document(body, "")})
    try:
        ranges = {item["notation"]: item for item in bundle["ranges"]}
        assert ranges["HVS1-3"]["status"] == "expanded"
        assert ranges["V10-20"]["identifiers"] == []
    finally:
        space.close()


def test_inherited_known_gap_scope_is_shared_by_all_public_views(tmp_path):
    space, imported = prepared_space(
        tmp_path, make_tar_gz({"main.tex": document("", "").encode()})
    )
    try:
        ref = imported.material_ref
        source = space.store().read_record(ref)
        gap = Gap(
            gap_id="existing",
            reason="missing",
            description="Known quantity-specific source fact",
            file_id=source.data.files[0].file_id,
            mention_evidence_refs=[],
            affected_targets=AffectedTargets(
                scope="known",
                items=[AffectedItem(item_id=None, quantity="RV", judgment=None)],
            ),
        )
        replacement = source.model_copy(
            update={
                "revision": source.revision + 1,
                "data": source.data.model_copy(update={"gaps": [gap]}),
            }
        )
        from hvs_catalog.workspace import CommitRequest

        space.store().commit(
            CommitRequest(
                operation_key="known-source-gap",
                kind="source_gap",
                new_records=[replacement],
                expected_revisions={source.record_id: source.revision},
            )
        )
        service = MaterialsService(space, runner=FakeConverterRunner())
        handle, report = service.prepare_materials(
            request_key="known", source_ref=replacement.ref()
        )
        assert report.outcome == "finished"
        bundle = service.get_prepared_input(handle.execution_id)
        prepared = space.store().read_record(RecordRef(**bundle["prepared_source_ref"]))
        assert prepared.data.gaps[0] == gap
        assert bundle["gaps"][0]["affected_targets"] == gap.affected_targets.model_dump(
            mode="json"
        )
        assert (
            service.consume_prepared_input(handle.execution_id)["gaps"]
            == bundle["gaps"]
        )
        assert report.summary["gap_count"] == 1
    finally:
        space.close()


def test_two_flow_failures_keep_readable_run_and_can_resume(tmp_path, monkeypatch):
    from hvs_catalog.materials.flow import MaterialsFlow

    space, imported = prepared_space(
        tmp_path, make_tar_gz({"main.tex": document(TABLE).encode()})
    )
    original = MaterialsFlow._convert_tables

    def fail(self, ctx, state):
        raise RuntimeError("synthetic repeated converter failure")

    monkeypatch.setattr(MaterialsFlow, "_convert_tables", fail)
    service = MaterialsService(space, runner=FakeConverterRunner())
    try:
        handle, first = service.prepare_materials(
            request_key="fails", paper=imported.canonical_id
        )
        second = service.resume_preparation(handle.execution_id)
        assert first.outcome == second.outcome == "failed"
        execution = get_execution(space, handle.execution_id)
        run = space.store().read_record_current(execution.run_ref.record_id)
        assert (
            len(run.data.errors) == 2
            and len({e.error_id for e in run.data.errors}) == 2
        )
        assert service.get_preparation(handle.execution_id)["state"] == "failed"
        monkeypatch.setattr(MaterialsFlow, "_convert_tables", original)
        assert service.resume_preparation(handle.execution_id).outcome == "finished"
    finally:
        space.close()


def test_native_macro_generated_body_range_is_not_silently_omitted(tmp_path):
    bundle, _, consumer = native_materials_probe(
        tmp_path,
        {
            "main.tex": document(
                r"The stars \objects{} are listed.", r"\newcommand{\objects}{HVS1-3}"
            )
        },
    )
    generated = next(
        item for item in bundle["ranges"] if item["origin"] == "native-rendered"
    )
    assert generated["notation"] == "HVS1\u20133" or generated["notation"] == "HVS1-3"
    assert generated["status"] == "unsupported" and generated["identifiers"] == []
    assert bundle["gaps"] and not consumer["materials_complete"]


@pytest.mark.parametrize(
    "source",
    [
        r"\pgfplotstableread[col sep=comma]{a.csv}\t {\pgfplotstableread[col sep=comma]{b.csv}\t} \pgfplotstabletypeset{\t}",
        r"\pgfplotstableread[col sep=comma]{a.csv}\t \newcommand{\swap}{\pgfplotstableread[col sep=comma]{b.csv}\t}\swap\pgfplotstabletypeset{\t}",
        r"\pgfplotstableread[col sep=comma]{a.csv}\t \let\t\other \pgfplotstabletypeset{\t}",
    ],
)
def test_pgf_literal_path_does_not_rebuild_scoped_or_indirect_assignment(source):
    from hvs_catalog.materials.convert import convert_pgf_backing_csv
    from hvs_catalog.materials.tables import PgfTypeset

    point = source.index(r"\pgfplotstabletypeset")
    call = PgfTypeset(
        file="main.tex", line=1, options="", source_arg=r"\t", macro="t", offset=point
    )
    parsed, _, _ = convert_pgf_backing_csv(
        call,
        manuscript_texts={"main.tex": source},
        document_order=["main.tex"],
        read_member=lambda _: b"name,rv\nFIC-A,525\n",
        referring_file="main.tex",
    )
    assert parsed.status == "failed"


def test_pgf_default_whitespace_is_not_guessed_from_csv_content():
    from hvs_catalog.materials.convert import convert_pgf_backing_csv
    from hvs_catalog.materials.tables import PgfTypeset

    source = r"\pgfplotstableread{data.csv}\t \pgfplotstabletypeset{\t}"
    call = PgfTypeset(
        file="main.tex",
        line=1,
        options="",
        source_arg=r"\t",
        macro="t",
        offset=source.index(r"\pgfplotstabletypeset"),
    )
    parsed, _, _ = convert_pgf_backing_csv(
        call,
        manuscript_texts={"main.tex": source},
        document_order=["main.tex"],
        read_member=lambda _: b"name,rv\nFIC-A,00525\n",
        referring_file="main.tex",
    )
    assert parsed.status == "success"
    assert parsed.data_rows == [["FIC-A,00525"]] and len(parsed.columns) == 1
