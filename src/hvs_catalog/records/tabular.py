"""Shared CSV/ECSV logical-record readers for saving, backup, and consumption.

records.validate and material preparation share backing CSV/ECSV readers. Honor declared
delimiters, quotes, and multiline records; never substitute physical-line/whitespace
splitting or guess delimiters. Safely parse ECSV YAML comments after %ECSV and before
columns. Unknown types/fields/unreliable metadata fail explicitly. Base header tools
derive from legacy MIT code. Preserve all cells as strings, including missing markers,
significant digits, and signs; never infer numeric types. Used by preparation direct
backing reads/ECSV checks and table_cell validation alike.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field

import yaml

__all__ = [
    "EcsvStructure",
    "LogicalRecords",
    "TabularReadError",
    "read_csv_records",
    "read_ecsv_records",
    "read_table_records",
    "records_from_path",
]

#: Per-table decoded byte bound.
MAX_TABLE_FILE_BYTES = 64 * 1024 * 1024


class TabularReadError(ValueError):
    """Table cannot be read as declared logical records; retain original bytes for manual
    inspection.
    """


@dataclass(frozen=True)
class LogicalRecords:
    """Logical records with column names, column-addressed rows, and original header
    descriptions.
    """

    columns: tuple[str, ...]
    rows: tuple[dict[str, str], ...]
    # ECSV YAML descriptions default to column names when absent; CSV uses an empty mapping.
    column_descriptions: dict[str, str] = field(default_factory=dict)
    format: str = "csv"  # "csv" | "ecsv"
    column_metadata: dict[str, dict] = field(default_factory=dict)
    table_metadata: dict = field(default_factory=dict)

    def column_list(self) -> list[str]:
        return list(self.columns)


@dataclass(frozen=True)
class EcsvStructure:
    """ECSV structure for location/read-back checks."""

    columns: tuple[str, ...]
    column_row_line: int
    data_row_lines: tuple[int, ...]
    line_count: int
    sha256: str
    delimiter: str


def _decode(data: bytes) -> str:
    if len(data) > MAX_TABLE_FILE_BYTES:
        raise TabularReadError(f"表格文件超过读取上限 {MAX_TABLE_FILE_BYTES} 字节")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TabularReadError(f"表格文件不是可解码的 UTF-8：{exc}") from exc


def read_csv_records(
    data: bytes, *, delimiter: str = ",", has_header: bool = True
) -> LogicalRecords:
    """Read CSV logical records with explicit delimiters; csv handles quoting/multiline
    fields.

    Blank physical lines yielding empty records are ignored, but valid all-empty cells
    such as `,,` remain records.
    """
    if len(delimiter) != 1:
        raise TabularReadError(f"CSV 分隔符必须是单个字符：{delimiter!r}")
    text = _decode(data)
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter, strict=True)
        parsed = [row for row in reader if row]
    except csv.Error as exc:
        raise TabularReadError(f"CSV 记录无法解析：{exc}") from exc
    if not parsed:
        raise TabularReadError("CSV 没有任何逻辑记录")
    if not has_header:
        width = max(len(row) for row in parsed)
        # Headerless reads still require equal row widths; never pad trailing nulls to claim complete records
        # (second-review csv-ragged-no-header case). Preserve characters without invented values.
        ragged = [row for row in parsed if len(row) != width]
        if ragged:
            raise TabularReadError(
                f"CSV 记录宽度不一致（{[len(row) for row in ragged]} != {width}）；"
                "无表头读取不补空值认证"
            )
        columns = tuple(f"col_{index + 1:03d}" for index in range(width))
        rows = [
            {col: (row[i] if i < len(row) else "") for i, col in enumerate(columns)}
            for row in parsed
        ]
        return LogicalRecords(columns=columns, rows=tuple(rows), format="csv")
    header = parsed[0]
    if len(set(header)) != len(header):
        raise TabularReadError(f"CSV 表头列名重复：{header}")
    if not all(header):
        raise TabularReadError("CSV 表头存在空列名")
    columns = tuple(header)
    rows: list[dict[str, str]] = []
    for row in parsed[1:]:
        if len(row) != len(columns):
            raise TabularReadError(
                f"CSV 数据记录列数（{len(row)}）与表头（{len(columns)}）不一致"
            )
        rows.append(dict(zip(columns, row, strict=True)))
    return LogicalRecords(columns=columns, rows=tuple(rows), format="csv")


def _parse_column_headers_lenient(header_comment_lines: list[str]) -> dict[str, str]:
    """Tolerant fallback for header lines unsafe to parse as YAML, migrated under MIT."""
    headers: dict[str, str] = {}
    current: str | None = None

    def unquote(value: str) -> str:
        value = value.strip().rstrip("}").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            return value[1:-1]
        return value

    for raw in header_comment_lines:
        line = raw.lstrip("#").strip()
        if line.startswith("- name:"):
            current = line.split(":", 1)[1].strip()
            headers.setdefault(current, current)
        elif line.startswith("- {"):
            name_match = re.search(r"name:\s*([^,}\s]+)", line)
            if not name_match:
                continue
            current = name_match.group(1).strip().strip("'\"")
            description_match = re.search(r"description:\s*(.+)$", line)
            if description_match:
                headers[current] = unquote(description_match.group(1))
            else:
                headers.setdefault(current, current)
        elif line.startswith("description:") and current is not None:
            headers[current] = unquote(line.split(":", 1)[1])
    return headers


def _parse_column_headers(header_comment_lines: list[str]) -> dict[str, str]:
    """Read ECSV column metadata best-effort, falling back to tolerant parsing when unsafe.

    Unquoted descriptions containing commas/brackets may be silently truncated or create
    unknown flow-mapping keys in yaml.safe_load. Unknown column keys trigger line-based
    fallback preserving original descriptions.
    """
    body = "\n".join(
        line[2:] if line.startswith("# ") else line.removeprefix("#")
        for line in header_comment_lines
    )
    known_keys = {
        "name",
        "datatype",
        "description",
        "unit",
        "subtype",
        "meta",
        "format",
    }
    try:
        payload = yaml.safe_load(body)
    except yaml.YAMLError:
        return _parse_column_headers_lenient(header_comment_lines)
    if not isinstance(payload, dict):
        return _parse_column_headers_lenient(header_comment_lines)
    headers: dict[str, str] = {}
    columns = payload.get("datatype") or []
    if not isinstance(columns, list):
        return _parse_column_headers_lenient(header_comment_lines)
    for column in columns:
        if not isinstance(column, dict) or set(column) - known_keys:
            return _parse_column_headers_lenient(header_comment_lines)
        name = column.get("name")
        if not name:
            continue
        description = column.get("description")
        headers[str(name)] = str(description) if description else str(name)
    return headers


def _split_space_row(line: str) -> list[str]:
    """Split quoted space-delimited ECSV without treating repeated spaces as empty fields."""
    import shlex

    try:
        lexer = shlex.shlex(line, posix=True)
        lexer.whitespace_split = True
        lexer.whitespace += "\t"
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return [line]


def _ecsv_data_records(data_text: str, delimiter: str) -> list[list[str]]:
    """Read data-area logical records under ECSV quoting rules.

    Match Astropy: quoted fields can contain spaces/newlines/hash signs, doubled quotes
    escape quotes, and backslashes are literal. Use csv over the complete area, never
    shell splitting or physical-line processing that loses multiline cells
    (ecsv-escaped/ecsv-multiline/tabular-extra cases).
    """
    try:
        return [
            row
            for row in csv.reader(
                io.StringIO(data_text), delimiter=delimiter, strict=True
            )
            if row
        ]
    except csv.Error as exc:
        raise TabularReadError(f"ECSV 数据记录无法解析：{exc}") from exc


def _normalize_ecsv_delimiter(declared: object) -> str:
    """Normalize declared ECSV delimiters; absent declarations use standard default space.

    ECSV/Astropy default output uses spaces without delimiter declarations; commas/tabs
    require explicit declarations under APE 6/Astropy behavior.
    """
    if not isinstance(declared, str):
        return " "
    raw = declared.strip()
    value = raw.strip("'\"")
    if not value:
        # Declarations may contain bare/quoted whitespace; empty declarations mean absent.
        return " "
    if value in ("space", "whitespace"):
        return " "
    if value == "tab":
        return "\t"
    return value


def parse_ecsv_structure(data: bytes) -> EcsvStructure:
    """Mechanically verify decodable ECSV with column/data rows and return its structure."""
    text = _decode(data)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or not lines[0].startswith("# %ECSV"):
        raise TabularReadError("缺少 '# %ECSV' 头标记")
    delimiter = " "
    declared_names: list[str] | None = None
    # Read YAML only from the initial comment header, stopping at the first noncomment;
    # hash-prefixed text inside multiline cells is not metadata (tabular-extra case).
    header_lines: list[str] = []
    body_start = len(lines)
    for index, line in enumerate(lines[1:], start=1):
        if line.startswith("#"):
            header_lines.append(line[2:] if line.startswith("# ") else line[1:])
            continue
        body_start = index
        break
    header_meta = "\n".join(header_lines)
    try:
        meta = yaml.safe_load(header_meta) if header_meta else None
        if isinstance(meta, dict):
            delimiter = _normalize_ecsv_delimiter(meta.get("delimiter"))
            datatype = meta.get("datatype")
            if isinstance(datatype, list) and datatype:
                names = [
                    entry.get("name")
                    for entry in datatype
                    if isinstance(entry, dict) and isinstance(entry.get("name"), str)
                ]
                if names:
                    declared_names = names
    except yaml.YAMLError as exc:
        raise TabularReadError(f"ECSV YAML 头无法可靠读取：{exc}") from exc
    if (
        not isinstance(meta, dict)
        or not isinstance(meta.get("datatype"), list)
        or not meta["datatype"]
    ):
        raise TabularReadError("ECSV 缺少明确 YAML datatype 声明")
    scalar_types = {
        "string",
        "bool",
        "float32",
        "float64",
        "int8",
        "int16",
        "int32",
        "int64",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
    }
    known_fields = {
        "name",
        "datatype",
        "description",
        "unit",
        "subtype",
        "meta",
        "format",
    }
    if any(
        not isinstance(col, dict)
        or set(col) - known_fields
        or not isinstance(col.get("name"), str)
        or not col.get("name")
        or col.get("datatype") not in scalar_types
        or (col.get("subtype") is not None)
        or any(
            col.get(key) is not None and not isinstance(col[key], str)
            for key in ("description", "unit", "format")
        )
        for col in meta["datatype"]
    ):
        raise TabularReadError(
            "ECSV 含未支持的对象／数组／非标量类型；保留原件，不当纯字符串表认证"
        )
    if len(delimiter) != 1:
        raise TabularReadError(f"ECSV 分隔符必须是单个字符：{delimiter!r}")
    if body_start >= len(lines):
        raise TabularReadError("没有列名行")
    column_row_index = body_start
    column_row_text = lines[column_row_index]
    try:
        columns = tuple(next(csv.reader([column_row_text], delimiter=delimiter)))
        data_rows = [
            row
            for row in csv.reader(
                io.StringIO("\n".join(lines[column_row_index + 1 :])),
                delimiter=delimiter,
            )
            if row
        ]
    except csv.Error as exc:
        raise TabularReadError(f"ECSV 数据记录无法解析：{exc}") from exc
    if not columns:
        raise TabularReadError("列名行为空")
    if len(set(columns)) != len(columns):
        raise TabularReadError(f"ECSV 列名重复：{list(columns)}")
    if declared_names is not None and list(columns) != declared_names:
        # Match Astropy: YAML column names must equal tokens parsed from the column row.
        # Otherwise delimiter/structure disagree, such as undeclared comma data parsed as one
        # space-default token; reject instead of guessing.
        raise TabularReadError(
            f"ECSV 头部声明的列名（{declared_names}）与列名行拆出的记号"
            f"（{list(columns)}）不一致；分隔符与列结构不匹配，拒绝猜读"
        )
    if not data_rows:
        raise TabularReadError("没有数据记录")
    data_row_lines = tuple(
        index + 1
        for index, line in enumerate(lines[column_row_index + 1 :])
        if line.strip()
    )
    return EcsvStructure(
        columns=columns,
        column_row_line=column_row_index + 1,
        data_row_lines=data_row_lines,
        line_count=len(lines),
        sha256=hashlib.sha256(data).hexdigest(),
        delimiter=delimiter,
    )


def _metadata_mapping(value: object) -> dict:
    # YAML !!omap is the normal Astropy representation, not an array column.
    if value is None:
        return {}
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return value
    if (
        isinstance(value, list)
        and all(
            isinstance(pair, tuple) and len(pair) == 2 and isinstance(pair[0], str)
            for pair in value
        )
        and len({pair[0] for pair in value}) == len(value)
    ):
        return dict(value)
    raise TabularReadError(
        "ECSV 列／表 meta 必须是映射或明确的 YAML omap；不猜读元数据"
    )


def read_ecsv_records(data: bytes) -> LogicalRecords:
    """Read ECSV logical records with YAML metadata and standard data quoting."""
    structure = parse_ecsv_structure(data)
    text = _decode(data)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    data_text = "\n".join(lines[structure.column_row_line :])
    parsed = _ecsv_data_records(data_text, structure.delimiter)
    if not parsed:
        raise TabularReadError("ECSV 没有数据记录")
    header = list(structure.columns)
    rows: list[dict[str, str]] = []
    for row in parsed:
        if len(row) != len(header):
            raise TabularReadError(
                f"ECSV 数据记录列数（{len(row)}）与列名（{len(header)}）不一致"
            )
        rows.append(dict(zip(header, row, strict=True)))
    header_body = "\n".join(
        line[2:] if line.startswith("# ") else line.removeprefix("#")
        for line in lines[1 : structure.column_row_line - 1]
    )
    meta = yaml.safe_load(header_body)
    try:
        import json

        json.dumps(meta, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise TabularReadError("ECSV 元数据含未支持的非 JSON 值；保留原件") from exc
    table_meta = _metadata_mapping(meta.get("meta"))
    if "__serialized_columns__" in table_meta:
        raise TabularReadError(
            "ECSV 序列化对象／mixin 未支持；保留原件，不认证普通标量表"
        )
    metadata = {
        str(col["name"]): {key: value for key, value in col.items() if key != "name"}
        for col in meta["datatype"]
    }
    for column in metadata.values():
        if "meta" in column:
            column["meta"] = _metadata_mapping(column["meta"])
    return LogicalRecords(
        columns=structure.columns,
        rows=tuple(rows),
        column_descriptions={
            name: column.get("description") or name for name, column in metadata.items()
        },
        format="ecsv",
        column_metadata=metadata,
        table_metadata=table_meta,
    )


def read_table_records(
    data: bytes, *, media_type: str | None = None, name: str | None = None
) -> LogicalRecords:
    """Choose readers by media type/filename; explicitly reject unsupported formats."""
    hint = (media_type or "").lower()
    lowered = (name or "").lower()
    if hint in ("text/ecsv",) or lowered.endswith(".ecsv"):
        return read_ecsv_records(data)
    if hint in ("text/csv",) or lowered.endswith(".csv"):
        return read_csv_records(data)
    raise TabularReadError(
        f"未支持的表格材料格式：media_type={media_type!r} name={name!r}"
        "（当前支持 CSV 与 ECSV）"
    )


def records_from_path(path, *, delimiter: str = ",") -> LogicalRecords:
    """Read logical records from paths for shared consumers such as records.validate."""
    data = path.read_bytes()
    lowered = str(path).lower()
    if lowered.endswith(".ecsv"):
        return read_ecsv_records(data)
    return read_csv_records(data, delimiter=delimiter)
