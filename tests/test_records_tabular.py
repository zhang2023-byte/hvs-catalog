"""Synthetic CSV/ECSV logical-record reader and table_cell validation tests."""

from __future__ import annotations

import pytest

from hvs_catalog.records.tabular import (
    TabularReadError,
    read_csv_records,
    read_ecsv_records,
    read_table_records,
)

CSV_TEXT = (
    "object,rv,error,note\n"
    'FIC-A,525,10,"quoted, value"\n'
    'FIC-B,-430,12,"multi\nline note"\n'
    'FIC-A,525,10,"same name again"\n'
)

ECSV_TEXT = """# %ECSV 1.0.0
# ---
# delimiter: ','
# datatype:
# - {name: col_001, datatype: string, description: Object}
# - {name: col_002, datatype: string, description: "RV [km/s]"}
# - {name: col_003, datatype: string, description: "Error with, comma"}
# schema: astropy-2.0
col_001,col_002,col_003
FIC-A,525,10
FIC-B,-430,12
HVS01,+9.40,0.20
"""


def test_csv_logical_records_handle_quotes_multiline_and_duplicates():
    records = read_csv_records(CSV_TEXT.encode("utf-8"))
    assert records.columns == ("object", "rv", "error", "note")
    assert len(records.rows) == 3
    assert records.rows[0]["note"] == "quoted, value"
    assert records.rows[1]["note"] == "multi\nline note"
    # Retain both rows for same-named objects; never silently choose the first.
    assert [r["object"] for r in records.rows] == ["FIC-A", "FIC-B", "FIC-A"]
    # Preserve original strings, symbols, and digits.
    assert records.rows[2]["rv"] == "525"


def test_csv_requires_explicit_delimiter_shape():
    with pytest.raises(TabularReadError):
        read_csv_records(CSV_TEXT.encode("utf-8"), delimiter=";;")
    ragged = b"a,b\n1,2,3\n"
    with pytest.raises(TabularReadError, match="列数"):
        read_csv_records(ragged)
    with pytest.raises(TabularReadError, match="重复"):
        read_csv_records(b"a,a\n1,2\n")


def test_ecsv_keeps_col001_names_headers_and_precision():
    records = read_ecsv_records(ECSV_TEXT.encode("utf-8"))
    assert list(records.columns) == ["col_001", "col_002", "col_003"]
    assert records.column_descriptions["col_001"] == "Object"
    # Retain quoted complex descriptions, including commas.
    assert records.column_descriptions["col_003"] == "Error with, comma"
    assert records.rows[2]["col_001"] == "HVS01"
    assert (
        records.rows[2]["col_002"] == "+9.40"
    )  # Preserve significant digits and symbols.
    assert records.rows[1]["col_002"] == "-430"


def test_ecsv_rejects_missing_marker_or_rows():
    with pytest.raises(TabularReadError, match="ECSV"):
        read_ecsv_records(b"col1,col2\n1,2\n")
    with pytest.raises(TabularReadError, match="数据记录"):
        read_ecsv_records(
            b"# %ECSV 1.0.0\n# ---\n# datatype:\n# - {name: col1, datatype: string}\ncol1\n"
        )


def test_read_table_records_dispatches_by_format():
    csv = read_table_records(CSV_TEXT.encode(), media_type="text/csv")
    assert csv.format == "csv"
    ecsv = read_table_records(ECSV_TEXT.encode(), name="t.ecsv")
    assert ecsv.format == "ecsv"
    with pytest.raises(TabularReadError, match="未支持"):
        read_table_records(b"X", media_type="application/fits")


# ---- table_cell semantics through the shared records.validate interface ----


def _collection_with_table(
    tmp_path, locator: dict, table_text: bytes, name: str, excerpt: str = "525"
):
    """Build a record collection with one CSV/ECSV evidence item."""

    from hvs_catalog.records.common import build_record
    from hvs_catalog.records.source import (
        EvidenceRecord,
        SourceFile,
        SourcePackageData,
        SourcePackageRecord,
        SyntheticOrigin,
    )

    materials = tmp_path / "materials"
    materials.mkdir(exist_ok=True)
    (materials / name).write_bytes(table_text)
    import hashlib

    sha = hashlib.sha256(table_text).hexdigest()
    source = build_record(
        SourcePackageRecord,
        record_id="src-t",
        created_at=__import__("datetime").datetime(
            2026, 10, 1, tzinfo=__import__("datetime").UTC
        ),
        execution_id=None,
        data=SourcePackageData(
            paper_key="SYNTH-T",
            origin=SyntheticOrigin(kind="synthetic"),
            source_version="v1",
            files=[
                SourceFile(
                    file_id="table",
                    path=f"materials/{name}",
                    sha256=sha,
                    media_type="text/csv" if name.endswith(".csv") else "text/ecsv",
                    role="member",
                    read_state="ready",
                    derived_from=[],
                )
            ],
            preparation_issues=[],
        ),
    )
    evidence = build_record(
        EvidenceRecord,
        record_id="ev-t",
        created_at=__import__("datetime").datetime(
            2026, 10, 1, tzinfo=__import__("datetime").UTC
        ),
        execution_id=None,
        data={
            "paper_key": "SYNTH-T",
            "source_ref": {"record_id": "src-t", "revision": 1},
            "file_id": "table",
            "locator": locator,
            "excerpt": excerpt,
        },
    )
    return [source, evidence]


def _validate(tmp_path, locator, table_text, name, excerpt="525"):
    from hvs_catalog.records.validate import (
        CollectionValidationError,
        validate_collection,
    )

    records = _collection_with_table(tmp_path, locator, table_text, name, excerpt)
    try:
        issues = validate_collection(records, materials_root=tmp_path)
        return issues
    except CollectionValidationError as exc:
        return exc.issues


def test_table_cell_unique_hit_without_row_number_passes(tmp_path):
    issues = _validate(
        tmp_path,
        {
            "kind": "table_cell",
            "row_key": {"object": "FIC-B"},
            "column": "rv",
            "row_number": None,
        },
        CSV_TEXT.encode(),
        "table.csv",
        excerpt="-430",
    )
    assert issues == []


def test_table_cell_zero_hit_fails(tmp_path):
    issues = _validate(
        tmp_path,
        {
            "kind": "table_cell",
            "row_key": {"object": "NOPE"},
            "column": "rv",
            "row_number": None,
        },
        CSV_TEXT.encode(),
        "table.csv",
    )
    assert any("零命中" in i.message for i in issues)


def test_table_cell_multi_hit_without_row_number_fails(tmp_path):
    issues = _validate(
        tmp_path,
        {
            "kind": "table_cell",
            "row_key": {"object": "FIC-A"},
            "column": "rv",
            "row_number": None,
        },
        CSV_TEXT.encode(),
        "table.csv",
    )
    assert any("多命中" in i.message and "不默取第一行" in i.message for i in issues)


def test_table_cell_multi_hit_with_row_number_selects_nth_hit(tmp_path):
    issues = _validate(
        tmp_path,
        {
            "kind": "table_cell",
            "row_key": {"object": "FIC-A"},
            "column": "rv",
            "row_number": 2,
        },
        CSV_TEXT.encode(),
        "table.csv",
    )
    assert issues == []
    # The ordinal is one-based among matches: the second match still says 525; verify notes in another column.
    issues = _validate(
        tmp_path,
        {
            "kind": "table_cell",
            "row_key": {"object": "FIC-A"},
            "column": "note",
            "row_number": 2,
        },
        CSV_TEXT.encode(),
        "table.csv",
    )
    # Excerpt 525 differs from the note: report quote mismatch, not locator failure.
    assert any("引文" in i.message for i in issues)


def test_table_cell_row_number_out_of_hits_fails(tmp_path):
    issues = _validate(
        tmp_path,
        {
            "kind": "table_cell",
            "row_key": {"object": "FIC-B"},
            "column": "rv",
            "row_number": 3,
        },
        CSV_TEXT.encode(),
        "table.csv",
    )
    assert any("超出命中行数" in i.message for i in issues)


def test_table_cell_ecsv_same_interface(tmp_path):
    issues = _validate(
        tmp_path,
        {
            "kind": "table_cell",
            "row_key": {"col_001": "HVS01"},
            "column": "col_002",
            "row_number": None,
        },
        ECSV_TEXT.encode(),
        "table.ecsv",
        excerpt="+9.40",
    )
    assert issues == []
    issues = _validate(
        tmp_path,
        {
            "kind": "table_cell",
            "row_key": {"col_001": "HVS02"},
            "column": "col_002",
            "row_number": None,
        },
        ECSV_TEXT.encode(),
        "table.ecsv",
    )
    assert any("零命中" in i.message for i in issues)


def test_table_cell_hash_change_detected(tmp_path):
    records = _collection_with_table(
        tmp_path,
        {
            "kind": "table_cell",
            "row_key": {"object": "FIC-B"},
            "column": "rv",
            "row_number": None,
        },
        CSV_TEXT.encode(),
        "table.csv",
    )
    from hvs_catalog.records.validate import (
        CollectionValidationError,
        validate_collection,
    )

    with pytest.raises(CollectionValidationError):
        validate_collection(records, materials_root=tmp_path)
    (tmp_path / "materials" / "table.csv").write_bytes(
        CSV_TEXT.replace("525", "526").encode()
    )
    with pytest.raises(CollectionValidationError, match="哈希"):
        validate_collection(records, materials_root=tmp_path)


def test_csv_keeps_legal_all_empty_record_not_as_blank_line():
    """Retain valid all-empty records (`,,,`) without changing row order or count."""
    records = read_csv_records(b"name,rv,err\nFIC-A,525,10\n,,\nFIC-B,...,12\n")
    assert [row["name"] for row in records.rows] == ["FIC-A", "", "FIC-B"]
    assert records.rows[1] == {"name": "", "rv": "", "err": ""}
    # Truly blank physical lines still are not logical records.
    records = read_csv_records(b"name,rv\nFIC-A,525\n\nFIC-B,430\n")
    assert len(records.rows) == 2


def test_ecsv_spec_default_space_reads_astropy_written():
    """Read standard Astropy-default space-delimited ECSV without a delimiter declaration.

    Table.write(format='ascii.ecsv') defaults to spaces and omits delimiter. Follow the
    standard default instead of treating name rv as one column (review example).
    """
    text = (
        "# %ECSV 1.0\n"
        "# ---\n"
        "# datatype:\n"
        "# - {name: name, datatype: string}\n"
        "# - {name: rv, datatype: string}\n"
        "# schema: astropy-2.0\n"
        "name rv\n"
        "FIC-A 525\n"
    )
    records = read_ecsv_records(text.encode())
    assert list(records.columns) == ["name", "rv"]
    assert records.rows == ({"name": "FIC-A", "rv": "525"},)


def test_ecsv_undeclared_delimiter_comma_data_rejected():
    """Reject undeclared comma-delimited ECSV that conflicts with the space default,
    matching Astropy behavior.
    """
    text = (
        "# %ECSV 1.0\n"
        "# ---\n"
        "# datatype:\n"
        "# - {name: name, datatype: string}\n"
        "# - {name: rv, datatype: string}\n"
        "# schema: astropy-2.0\n"
        "name,rv\n"
        "FIC-A,525\n"
    )
    with pytest.raises(TabularReadError, match="分隔符与列结构不匹配"):
        read_ecsv_records(text.encode())


def test_ecsv_spec_default_space_supports_locator_and_consume(tmp_path):
    """Default-space ECSV uses the same save/location/consumption interface as explicitly
    delimited files.
    """
    text = (
        "# %ECSV 1.0\n"
        "# ---\n"
        "# datatype:\n"
        "# - {name: name, datatype: string}\n"
        "# - {name: rv, datatype: string}\n"
        "# schema: astropy-2.0\n"
        "name rv\n"
        "FIC-A 525\n"
    )
    issues = _validate(
        tmp_path,
        {
            "kind": "table_cell",
            "row_key": {"name": "FIC-A"},
            "column": "rv",
            "row_number": None,
        },
        text.encode(),
        "table.ecsv",
        excerpt="525",
    )
    assert issues == []
    issues = _validate(
        tmp_path,
        {
            "kind": "table_cell",
            "row_key": {"name": "FIC-Z"},
            "column": "rv",
            "row_number": None,
        },
        text.encode(),
        "table.ecsv",
    )
    assert any("零命中" in i.message for i in issues)


def test_ecsv_declared_space_delimiter_reads_with_quotes():
    """Read explicitly space-delimited ECSV with quoting semantics and no lost fields."""
    text = (
        "# %ECSV 1.0\n"
        "# ---\n"
        "# delimiter: ' '\n"
        "# datatype:\n"
        "# - {name: name, datatype: string}\n"
        "# - {name: note, datatype: string}\n"
        "# schema: astropy-2.0\n"
        "name note\n"
        'FIC-A "two words"\n'
        "FIC-B single\n"
    )
    records = read_ecsv_records(text.encode())
    assert list(records.columns) == ["name", "note"]
    assert records.rows[0]["note"] == "two words"
    assert records.rows[1]["note"] == "single"


def test_ecsv_duplicate_columns_and_bad_delimiter_rejected():
    dup = (
        "# %ECSV 1.0\n"
        "# ---\n"
        "# delimiter: ';;'\n"
        "# datatype:\n"
        "# - {name: id, datatype: string}\n"
        "# schema: astropy-2.0\n"
        "id\n"
        "FIC-B\n"
    )
    # Convert multicharacter delimiter errors to TabularReadError without leaking TypeError.
    with pytest.raises(TabularReadError, match="单个字符"):
        read_ecsv_records(dup.encode())
    duplicate_columns = (
        "# %ECSV 1.0\n"
        "# ---\n"
        "# delimiter: ','\n"
        "# datatype:\n"
        "# - {name: id, datatype: string}\n"
        "# - {name: id, datatype: string}\n"
        "# schema: astropy-2.0\n"
        "id,id\n"
        "FIC-B,FIC-A\n"
    )
    with pytest.raises(TabularReadError, match="列名重复"):
        read_ecsv_records(duplicate_columns.encode())


def test_table_cell_missing_row_key_column_fails(tmp_path):
    """Explicitly fail missing row-key columns; default None must not certify arbitrary
    objects.
    """
    issues = _validate(
        tmp_path,
        {
            "kind": "table_cell",
            "row_key": {"missing_identity_column": None},
            "column": "rv",
            "row_number": None,
        },
        CSV_TEXT.encode(),
        "table.csv",
    )
    assert any("行键列不存在" in issue.message for issue in issues)
    issues = _validate(
        tmp_path,
        {
            "kind": "table_cell",
            "row_key": {"object": None},
            "column": "rv",
            "row_number": None,
        },
        CSV_TEXT.encode(),
        "table.csv",
    )
    assert any("行键含空值" in issue.message for issue in issues)
