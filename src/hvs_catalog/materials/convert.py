"""Bounded structural readers and lossless ECSV presentation.

TeX execution belongs to LaTeXML (materials.document), not a macro interpreter
here. The internal reader accepts literal grids only. Shared grid validation
never guesses headers, drops empty cells, pads missing cells or clips spans.
Portions of the literal table/PGF helpers derive from stella_hvs (MIT); see
LICENSE-NOTICE.md.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from .tables import (
    PgfRead,
    PgfTypeset,
    _balanced_group,
    _find_environment,
    _first_argument,
    parse_pgf_reads,
    pgf_column_headers,
    pgf_delimiter,
    pgf_selected_columns,
    pgf_unexplained_transforms,
)

_MAX_TABLE_ROWS = 100_000
_MAX_TABLE_COLS = 1_000
_MAX_GRID_CELLS = 1_000_000
_MAX_SPAN = 1_000


@dataclass
class ParsedTable:
    status: str
    method: str = ""
    environment: str = ""
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    columns: list[dict] = field(default_factory=list)
    header_rows: list[list[str]] = field(default_factory=list)
    data_rows: list[list[str]] = field(default_factory=list)
    raw_rows: list[list[str]] = field(default_factory=list)
    merged_cells: list[dict] = field(default_factory=list)
    presentation_changes: list[dict] = field(default_factory=list)
    backing_member: str = ""
    backing_columns: list[str] = field(default_factory=list)
    table_metadata: dict = field(default_factory=dict)

    @property
    def row_count(self) -> int:
        return len(self.data_rows)

    @property
    def column_count(self) -> int:
        return len(self.columns)


@dataclass
class ConversionAttempt:
    method: str
    status: str
    command: list[str] = field(default_factory=list)
    error: str = ""
    diagnostics: dict[str, str] = field(default_factory=dict)


def _columns(headers: list[list[str]], width: int) -> list[dict]:
    return [
        {
            "name": f"col_{i + 1:03d}",
            "original_header": " | ".join(
                dict.fromkeys(row[i] for row in headers if row[i])
            ),
            "unit_text": "",
            "null_values": ["", "..."],
        }
        for i in range(width)
    ]


def grid(rows: list[list[tuple[str, int, int]]]) -> tuple[list[list[str]], list[dict]]:
    """Expand an actual cell grid; distinguish explicit empty cells from holes."""
    if len(rows) > _MAX_TABLE_ROWS:
        raise ValueError("表格行数超过有限边界")
    occupied: dict[tuple[int, int], str] = {}
    spans = []
    width = 0
    for r, cells in enumerate(rows):
        c = 0
        for text, rs, cs in cells:
            if not 1 <= rs <= _MAX_SPAN or not 1 <= cs <= _MAX_SPAN:
                raise ValueError("表格跨度超过有限边界或无效")
            while (r, c) in occupied:
                c += 1
            if c + cs > _MAX_TABLE_COLS or r + rs > len(rows):
                raise ValueError("表格跨度越界或列数超过有限边界")
            if len(occupied) + rs * cs > _MAX_GRID_CELLS:
                raise ValueError("表格网格超过有限边界")
            for dr in range(rs):
                for dc in range(cs):
                    key = r + dr, c + dc
                    if key in occupied:
                        raise ValueError("表格合并格重叠")
                    occupied[key] = text if dc == 0 else ""
            if rs != 1 or cs != 1:
                spans.append(
                    {
                        "row_index": r,
                        "col_index": c,
                        "rowspan": rs,
                        "colspan": cs,
                        "text": text,
                    }
                )
            c += cs
        width = max(width, c)
    if not width:
        raise ValueError("表格没有单元格")
    if width * len(rows) > _MAX_GRID_CELLS:
        raise ValueError("表格网格超过有限边界")
    if any((r, c) not in occupied for r in range(len(rows)) for c in range(width)):
        raise ValueError("表格行列数不一致：存在缺格，不补空值认证")
    return [[occupied[r, c] for c in range(width)] for r in range(len(rows))], spans


def split_latex_rows(text: str) -> list[str]:
    return _split_tex(text, "\\\\")


def split_latex_cells(text: str) -> list[str]:
    return _split_tex(text, "&")


def _split_tex(text: str, separator: str) -> list[str]:
    parts, depth, start, i = [], 0, 0, 0
    while i < len(text):
        if depth == 0 and text.startswith(separator, i):
            parts.append(text[start:i])
            i += len(separator)
            start = i
            continue
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth < 0:
                raise ValueError("表格花括号不平衡")
        i += 1
    if depth:
        raise ValueError("表格花括号不平衡")
    parts.append(text[start:])
    return parts


_RULE_RE = re.compile(r"\\(?:hline|toprule|midrule|bottomrule|botrule|tableline)\b")


def _literal_rows(text: str) -> tuple[list[list[str]], list[dict]]:
    rows = []
    for raw in split_latex_rows(text):
        raw = _RULE_RE.sub("", raw).strip()
        if not raw:
            continue  # a trailing separator/rule, not an explicit '&' row
        cells = []
        for raw_cell in split_latex_cells(raw):
            cell = raw_cell.strip()
            cs = 1
            if cell.startswith(r"\multicolumn"):
                match = re.fullmatch(
                    r"\\multicolumn\{(\d+)\}\{[^{}]*\}\{(.*)\}", cell, re.DOTALL
                )
                if not match:
                    raise ValueError("不能解释的 multicolumn")
                cs, cell = int(match[1]), match[2]
            colhead = re.fullmatch(r"\\colhead\{(.*)\}", cell, re.DOTALL)
            if colhead:
                cell = colhead[1]
            # Only mathematical strings can remain literal. No style/author
            # command is expanded or silently removed, even textbf.
            unknown = re.findall(r"\\([A-Za-z@]+)", cell)
            if any(
                name not in {"pm", "times", "sigma", "ldots", "cdots"}
                for name in unknown
            ):
                raise ValueError("未解析宏：内部读取不解释作者宏")
            cells.append((cell, 1, cs))
        rows.append(cells)
    return grid(rows)


def parse_latex_table_excerpt(excerpt: str) -> ParsedTable:
    """Read a literal source grid without executing macros or guessing headers."""
    from .cleaning import mask_verbatim, strip_tex_comments
    from .tables import LATEX_TABLE_ENVIRONMENTS

    text = strip_tex_comments(excerpt)
    scanned = mask_verbatim(text)
    found = []
    for env in LATEX_TABLE_ENVIRONMENTS:
        cursor = 0
        while (hit := _find_environment(scanned, env, start=cursor)) is not None:
            found.append((env, *hit))
            cursor = hit[1]
    if len(found) != 1:
        return ParsedTable(
            "failed", method="internal", error="不能唯一定位一个原表内容环境"
        )
    env, start, stop = found[0]
    try:
        cursor = start + len(r"\begin{" + env + "}")
        # tabular* has an additional width argument.
        nargs = 2 if env == "tabular*" else 1
        for _ in range(nargs):
            while cursor < stop and text[cursor].isspace():
                cursor += 1
            if text[cursor : cursor + 1] != "{":
                raise ValueError("表格列格式不能定位")
            end = _balanced_group(text, cursor)
            if end is None:
                raise ValueError("表格列格式损坏")
            cursor = end + 1
        body = text[cursor : stop - len(r"\end{" + env + "}")]
        headers = []
        if env.startswith("deluxetable"):
            head = _first_argument(body, "tablehead")
            if head:
                headers, _ = _literal_rows(head[0])
            if r"\startdata" not in body or r"\enddata" not in body:
                raise ValueError("deluxetable 缺少明确 startdata/enddata")
            body = body.split(r"\startdata", 1)[1].split(r"\enddata", 1)[0]
        elif env == "longtable":
            # Captions and labels are presentation metadata, not data rows.
            caption = _first_argument(body, "caption")
            if caption:
                body = body[: caption[1]] + body[caption[2] :]
                body = re.sub(r"^\s*\\\\\s*", "", body)
            body = re.sub(r"\\label\s*\{[^{}]*\}\s*(?:\\\\)?", "", body)
            # Only explicitly delimited headers/footers are removed.
            if r"\endfirsthead" in body:
                head, body = body.split(r"\endfirsthead", 1)
                headers, _ = _literal_rows(head)
            if r"\endhead" in body:
                head, body = body.split(r"\endhead", 1)
                if not headers:
                    headers, _ = _literal_rows(head)
            for marker in (r"\endfoot", r"\endlastfoot"):
                if marker in body:
                    body = body.split(marker, 1)[1]
        data, spans = _literal_rows(body)
        width = len(data[0])
        if any(len(row) != width for row in headers):
            raise ValueError("表头与数据宽度不一致")
        return ParsedTable(
            "success",
            method="internal",
            environment=env,
            columns=_columns(headers, width),
            header_rows=headers,
            data_rows=data,
            raw_rows=[*headers, *data],
            merged_cells=spans,
            warnings=[]
            if headers
            else ["原表没有明确结构化表头；保留所有行，不猜首行"],
        )
    except (ValueError, IndexError) as exc:
        return ParsedTable("failed", method="internal", environment=env, error=str(exc))


def _html_cell_text(cell) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(str(cell), "html.parser")
    for math in soup.find_all("math"):
        math.replace_with(str(math.get("alttext") or math.get_text()))
    return soup.get_text("", strip=False).strip()


def parse_html_tables(html: str, *, method: str) -> tuple[ParsedTable, int]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if len(tables) != 1:
        return ParsedTable(
            "failed",
            method=method,
            error=f"输出包含 {len(tables)} 张表，不能唯一定位一张 HTML 表",
        ), len(tables)
    table = tables[0]
    try:
        raw, head_count = [], 0
        for tr in table.find_all("tr"):
            if tr.find_parent("table") is not table:
                raise ValueError("嵌套表不能认证简单网格")
            cells = [
                (
                    _html_cell_text(cell),
                    int(cell.get("rowspan", 1)),
                    int(cell.get("colspan", 1)),
                )
                for cell in tr.find_all(["td", "th"], recursive=False)
            ]
            raw.append(cells)
            if tr.find_parent("thead") is not None:
                if head_count != len(raw) - 1:
                    raise ValueError("表头不在数据之前")
                head_count += 1
        values, spans = grid(raw)
        headers = values[:head_count]
        return ParsedTable(
            "success",
            method=method,
            columns=_columns(headers, len(values[0])),
            header_rows=headers,
            data_rows=values[head_count:],
            raw_rows=values,
            merged_cells=spans,
            warnings=[] if headers else ["HTML 没有明确 thead；全部行保留为数据"],
        ), 1
    except (ValueError, TypeError) as exc:
        return ParsedTable("failed", method=method, error=str(exc)), 1


def latexml_conversion_verdict(outcome, html: str, *, excerpt: str = ""):
    """Fragment helper: a successful process alone never proves correspondence."""
    problems = process_problems(outcome)
    if "ltx_ERROR" in html:
        problems.append("转换输出含 ltx_ERROR 错误节点")
    parsed, count = parse_html_tables(html, method="latexml")
    if parsed.status == "failed":
        problems.append(parsed.error)
    if excerpt and parsed.status == "success":
        problems.extend(verify_content_correspondence(excerpt, parsed))
    if problems:
        parsed = ParsedTable("failed", method="latexml", error="；".join(problems))
    return parsed, problems, count


def process_problems(outcome) -> list[str]:
    problems = []
    if outcome.error or outcome.timed_out or outcome.exceeded_output:
        problems.append(outcome.error or "转换超时或输出超限")
    if outcome.returncode != 0:
        problems.append(f"进程退出码 {outcome.returncode} 非零")
    statuses = re.findall(
        r"Status:conversion:(\d+)", outcome.stderr.decode("utf-8", "replace")
    )
    if not statuses:
        problems.append("缺少 Status:conversion 状态诊断")
    elif any(status != "0" for status in statuses):
        problems.append(
            "LaTeXML 转换诊断报错："
            + ", ".join(
                "Status:conversion:" + status for status in statuses if status != "0"
            )
        )
    return problems


def verify_content_correspondence(excerpt: str, parsed: ParsedTable) -> list[str]:
    """Whole row/cell comparison for literal tables, never a token-set check."""
    original = parse_latex_table_excerpt(excerpt)
    if original.status != "success":
        return ["转换对应核对失败：原表不能独立证明行列对应：" + original.error]
    return literal_correspondence(original, parsed)


def literal_correspondence(original: ParsedTable, parsed: ParsedTable) -> list[str]:
    source, target = (
        original.header_rows + original.data_rows,
        parsed.header_rows + parsed.data_rows,
    )
    if len(source) != len(target) or any(
        len(a) != len(b) for a, b in zip(source, target, strict=True)
    ):
        return ["原表完整行列对应不一致"]
    changes = []
    for r, (left, right) in enumerate(zip(source, target, strict=True)):
        for c, (a, b) in enumerate(zip(left, right, strict=True)):
            if a == b:
                continue
            math = re.fullmatch(r"\$(.*)\$", a, re.DOTALL)
            if math is None or re.sub(r"\s+", "", math[1]) != re.sub(r"\s+", "", b):
                return ["原表原字符串／数值配对不一致"]
            changes.append(
                {
                    "kind": "math-delimiter-spacing",
                    "row_index": r,
                    "col_index": c,
                    "source": a,
                    "presented": b,
                }
            )
    parsed.presentation_changes.extend(changes)
    return []


def convert_pgf_backing_csv(
    typeset: PgfTypeset,
    *,
    manuscript_texts: dict[str, str],
    document_order: list[str],
    read_member,
    referring_file: str,
    include_positions: dict[str, tuple[str | None, int | None]] | None = None,
) -> tuple[ParsedTable, ConversionAttempt, list[str]]:
    """Direct read of explicitly understood, literal PGF invocations."""
    from ..records.tabular import TabularReadError, read_csv_records
    from .cleaning import mask_macro_bodies, mask_verbatim, strip_tex_comments
    from .texgraph import _parent_dir

    attempt = ConversionAttempt("pgfplotstable_csv", "failed")

    def fail(message):
        return ParsedTable("failed", method=attempt.method, error=message), attempt, []

    if typeset.file not in document_order:
        return fail("表块不在正文包含流中")
    positions = include_positions or {name: (None, None) for name in document_order}
    if _brace_depth_at(manuscript_texts.get(typeset.file, ""), typeset.offset):
        return fail("PGF 调用位于局部分组中，不能按字面顺序认证")
    try:
        target_position = _document_position(
            typeset.file,
            typeset.line,
            positions,
            texts=manuscript_texts,
            offset=typeset.offset,
        )
    except ValueError as exc:
        return fail(str(exc))
    reads = []
    for name in document_order:
        raw = mask_verbatim(strip_tex_comments(manuscript_texts.get(name, "")))
        view = mask_macro_bodies(raw)
        if len(parse_pgf_reads(raw)) != len(parse_pgf_reads(view)):
            return fail("PGF 读入位于作者宏体中，不能重建执行状态")
        if re.search(
            r"\\(?:def|gdef|edef|xdef|newcommand|renewcommand|providecommand|let)\s*\{?\\"
            + re.escape(typeset.macro)
            + r"(?![A-Za-z@])",
            raw,
        ):
            return fail("PGF 数据宏被其他作者指令定义或赋值，不能按最近字面读入认证")
        # A direct read cannot determine conditional/grouped execution state.
        if re.search(r"\\(?:if[a-zA-Z]*|begingroup|endgroup)(?![A-Za-z@])", view):
            return fail("PGF 执行上下文不确定，不能按字面顺序认证")
        for item in parse_pgf_reads(view):
            if _brace_depth_at(view, item.offset):
                return fail("PGF 读入位于局部分组中，不能按字面顺序认证")
            if name != document_order[0] and positions.get(name, (None,))[0] is None:
                return fail("PGF 原文件的包含时刻不明，不按文件排序猜读入次序")
            try:
                pos = _document_position(
                    name,
                    item.line,
                    positions,
                    texts=manuscript_texts,
                    offset=item.offset,
                )
            except ValueError as exc:
                return fail(str(exc))
            if item.macro == typeset.macro and pos < target_position:
                reads.append(
                    (
                        pos,
                        PgfRead(
                            name,
                            item.line,
                            item.options,
                            item.path,
                            item.macro,
                            item.offset,
                        ),
                    )
                )
    if not reads:
        return fail(f"未找到表前宏 {typeset.macro} 的 pgfplotstableread 读入")
    read = max(reads, key=lambda pair: pair[0])[1]
    unexplained = pgf_unexplained_transforms(
        typeset.options
    ) + pgf_unexplained_transforms(read.options)
    if unexplained:
        message = "无法解释的过滤／变换：" + "；".join(unexplained)
        return (
            ParsedTable("failed", method=attempt.method, error=message),
            attempt,
            unexplained,
        )
    literal = _resolve_member_path(
        _parent_dir(read.file or referring_file), read.path, read_member
    )
    if literal is None:
        return fail(f"backing CSV 成员缺失或越界：{read.path}")
    try:
        delimiter = pgf_delimiter(read.options)
        if delimiter is None:
            # Legacy PGF's default is space, not guessed from file content.
            delimiter = " "
        records = read_csv_records(read_member(literal), delimiter=delimiter)
        fields = list(records.columns)
        selected = pgf_selected_columns(typeset.options, fields)
        if (
            not selected
            or len(set(selected)) != len(selected)
            or set(selected) - set(fields)
        ):
            return fail("PGF 声明选列缺失、重复或为空")
        headers = pgf_column_headers(typeset.options)
        header = [headers.get(name, name) for name in selected]
        columns = _columns([header], len(selected))
        parsed = ParsedTable(
            "success",
            method=attempt.method,
            environment="pgfplotstable",
            columns=columns,
            header_rows=[header],
            data_rows=[[row[col] for col in selected] for row in records.rows],
            backing_member=literal,
            backing_columns=selected,
        )
        return parsed, ConversionAttempt(attempt.method, "success"), []
    except (TabularReadError, ValueError) as exc:
        return fail(f"PGF 分隔符声明或 CSV 无法读取：{exc}")


def _brace_depth_at(text: str, stop: int) -> int:
    """A rejection guard for lexical grouping; never evaluates a TeX state."""
    depth, i = 0, 0
    while i < stop:
        if text[i] == "\\":
            i += 2
            continue
        depth += int(text[i] == "{") - int(text[i] == "}")
        i += 1
    return depth


def _document_position(
    file: str, line: int, includes: dict, *, texts: dict, offset: int
) -> list[int]:
    from .texgraph import SUPPORTED_INCLUDE_RE, _parent_dir, _resolve_member

    if offset == 0 and line > 1:
        offset = sum(
            map(len, texts.get(file, "").splitlines(keepends=True)[: line - 1])
        )
    sequence, seen = [offset], set()
    while file not in seen:
        seen.add(file)
        entry = includes.get(file, (None, None))
        parent = entry[0]
        if parent is None:
            return sequence
        from .cleaning import scan_view

        source = scan_view(texts.get(parent, ""))
        matches = [
            match.start()
            for match in SUPPORTED_INCLUDE_RE.finditer(source)
            if _resolve_member(texts, _parent_dir(parent), match[1]) == file
        ]
        if len(matches) != 1:
            raise ValueError("PGF 的包含出现不能唯一定位")
        include_offset = entry[2] if len(entry) > 2 else matches[0]
        sequence.insert(0, include_offset)
        file = parent
    raise ValueError("包含位置循环")


def _resolve_member_path(base_dir: str, target: str, read_member) -> str | None:
    if target.startswith(("/", "~", "\\")) or "\\" in target:
        return None
    for base in (base_dir, ""):
        parts = []
        for part in (base + "/" + target).split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if not parts:
                    break
                parts.pop()
            else:
                parts.append(part)
        else:
            candidate = "/".join(parts)
            if read_member(candidate) is not None:
                return candidate
    return None


def table_ecsv_text(parsed: ParsedTable) -> str:
    from astropy.io import ascii
    from astropy.table import Table

    names = [col["name"] for col in parsed.columns]
    table = Table(rows=parsed.data_rows, names=names, dtype=["str"] * len(names))
    table.meta.update(parsed.table_metadata)
    for name, col in zip(names, parsed.columns, strict=True):
        table[name].description = (
            col.get("description") or col.get("original_header") or name
        )
        if col.get("unit"):
            table[name].unit = col["unit"]
        table[name].meta.update(col.get("meta") or {})
        if col.get("unit_text") and not col.get("unit"):
            table[name].meta["hvs_unit_text"] = col["unit_text"]
    stream = io.StringIO()
    ascii.write(table, stream, format="ecsv", overwrite=True, delimiter=",")
    return stream.getvalue()


def verify_ecsv_roundtrip(ecsv_text: str, parsed: ParsedTable) -> list[str]:
    from ..records.tabular import TabularReadError, read_ecsv_records

    try:
        records = read_ecsv_records(ecsv_text.encode())
        names = [col["name"] for col in parsed.columns]
        rows = [[row[name] for name in records.columns] for row in records.rows]
        if list(records.columns) != names or rows != parsed.data_rows:
            return ["ECSV 回读原字符串／行列不一致"]
    except TabularReadError as exc:
        return [f"ECSV 回读失败：{exc}"]
    return []
