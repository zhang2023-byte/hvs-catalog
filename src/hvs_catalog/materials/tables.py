"""Scan package table environments, source lines, identity, captions/labels, and backing
CSV links.

Scan comment-cleaned reachable main-document text with original line counts for
tabular/tabular*, longtable, deluxetable/deluxetable*, and outer table/table* floats.
Give each block stable generated identity/source lines; exclude verbatim.

Parse pgfplotstabletypeset and pgfplotstableread associations. Direct-read only explicit
backing paths/delimiters/columns/display headers; unsupported filtering/transforms leave
gaps, never equating full backing files with displayed tables. Base row/cell algorithms
derive from stella_hvs/src/stella/lit/catalog_extraction.py at 8421c98 under MIT,
adapted for exact line ranges and per-table isolation.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field

from .texgraph import ManuscriptGraph

__all__ = [
    "LATEX_TABLE_ENVIRONMENTS",
    "PgfRead",
    "PgfTypeset",
    "TableBlock",
    "scan_tables",
]

LATEX_TABLE_ENVIRONMENTS = (
    "tabular",
    "tabular*",
    "longtable",
    "deluxetable",
    "deluxetable*",
)
_FLOAT_ENVIRONMENTS = ("table", "table*")

_RULE_COMMAND_RE = re.compile(
    r"\\(?:hline|toprule|midrule|bottomrule|botrule|tableline)\b"
)


@dataclass(frozen=True)
class TableBlock:
    """Table registration: identity, environment, file, and line range."""

    table_id: str
    file: str
    environment: (
        str  # Content environment: tabular, longtable, deluxetable, or pgfplotstable.
    )
    start_line: int  # Block start: outer float or environment begin line.
    end_line: int  # Block end, including the end line.
    content_start_line: int  # Content-environment begin line.
    start_offset: int = 0
    end_offset: int = 0
    content_start_offset: int = 0
    content_end_offset: int = 0
    label: str = ""
    caption: str = ""
    # Footnotes such as tablenotetext preserve markers/text in occurrence order.
    footnotes: list[tuple[str, str]] = dataclass_field(default_factory=list)
    # pgfplotstable associations, populated only for those blocks.
    pgf_typeset: PgfTypeset | None = None
    # Public scan notes, such as uncertain caption correspondence.
    notes: list[str] = dataclass_field(default_factory=list)

    def source_span(self) -> dict:
        return {
            "file": self.file,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }


@dataclass(frozen=True)
class PgfTypeset:
    """Parsed pgfplotstabletypeset call."""

    file: str
    line: int
    options: str
    source_arg: str
    macro: str
    offset: int = 0


@dataclass(frozen=True)
class PgfRead:
    """Parsed pgfplotstableread call."""

    file: str
    line: int
    options: str
    path: str
    macro: str
    offset: int = 0


def _verbatim_spans(text: str) -> list[tuple[int, int]]:
    """Locate verbatim and inline verb character spans, including opening/closing
    directives.

    Literal table/PGF commands in inline verb are display text, not structure
    (second-review finding 23). Share masked views for body/table/PGF scans. Derive
    spans from character differences; unchanged whitespace may split spans, but
    nonwhitespace structural command positions remain covered, preserving membership
    checks.
    """
    from .cleaning import mask_verbatim

    masked = mask_verbatim(text)
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, (original, masked_char) in enumerate(zip(text, masked)):
        if original != masked_char:
            if start is None:
                start = index
        elif start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(text)))
    return runs


def _in_spans(spans: list[tuple[int, int]], position: int) -> bool:
    return any(start <= position < end for start, end in spans)


def _line_of(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def _find_environment(text: str, environment: str, *, start: int = 0):
    """Find a complete environment using same-name nesting counts; return (begin_start,
    end_end).
    """
    begin_re = re.compile(rf"\\begin\{{{re.escape(environment)}\}}")
    end_re = re.compile(rf"\\end\{{{re.escape(environment)}\}}")
    from .cleaning import mask_verbatim

    text = mask_verbatim(text)
    verbatim = []
    search_from = start
    while True:
        begin = begin_re.search(text, search_from)
        if begin is None or _in_spans(verbatim, begin.start()):
            if begin is None:
                return None
            search_from = begin.end()
            continue
        depth = 1
        cursor = begin.end()
        while depth > 0:
            next_begin = begin_re.search(text, cursor)
            if next_begin is not None and _in_spans(verbatim, next_begin.start()):
                next_begin = None
            next_end = end_re.search(text, cursor)
            if next_end is not None and _in_spans(verbatim, next_end.start()):
                next_end = None
            if next_end is None:
                return None
            if next_begin is not None and next_begin.start() < next_end.start():
                depth += 1
                cursor = next_begin.end()
                continue
            depth -= 1
            cursor = next_end.end()
        return begin.start(), cursor


def _balanced_group(text: str, open_index: int) -> int | None:
    """Find the exclusive end of matching braces, respecting escapes and nested brace
    counts.
    """
    if open_index >= len(text) or text[open_index] != "{":
        return None
    depth = 0
    index = open_index
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            index += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _first_argument(
    text: str, command: str, *, start: int = 0
) -> tuple[str, int, int] | None:
    """Return first argument, command start, and exclusive argument end, including
    short-caption syntax.
    """
    match = re.compile(rf"\\{re.escape(command)}\s*(?:\[[^\]]*\]\s*)?\{{").search(
        text, start
    )
    if match is None:
        return None
    close = _balanced_group(text, match.end() - 1)
    if close is None:
        return None
    return text[match.end() : close], match.start(), close + 1


def scan_tables(graph: ManuscriptGraph) -> list[TableBlock]:
    """Scan main-document graph tables in file/position order."""
    blocks: list[TableBlock] = []
    for entry in graph.files:
        from .cleaning import scan_view

        text = scan_view(entry.cleaned_text)
        verbatim = []
        blocks.extend(_scan_floats_and_standalone(text, entry.name, verbatim))
        blocks.extend(_scan_pgfplotstable(text, entry.name, verbatim))
    blocks.sort(key=lambda block: (block.file, block.start_line, block.table_id))
    return blocks


def _scan_floats_and_standalone(
    text: str, file: str, verbatim: list[tuple[int, int]]
) -> list[TableBlock]:
    blocks: list[TableBlock] = []
    covered: list[tuple[int, int]] = []
    for float_env in _FLOAT_ENVIRONMENTS:
        search_from = 0
        while True:
            found = _find_environment(text, float_env, start=search_from)
            if found is None:
                break
            begin_start, end_end = found
            search_from = end_end
            if _in_spans(verbatim, begin_start) or _in_spans(covered, begin_start):
                continue
            region = text[begin_start:end_end]
            # Register every content environment inside a float independently;
            # never keep only the first of two tables and silently discard the second.
            for env_name, rel_begin, rel_end in _all_content_environments(region):
                absolute_begin = begin_start + rel_begin
                blocks.append(
                    _make_block(
                        text,
                        file,
                        env_name,
                        begin_start,
                        end_end,
                        absolute_begin,
                        content_end=begin_start + rel_end,
                    )
                )
            covered.append((begin_start, end_end))
    for env in LATEX_TABLE_ENVIRONMENTS:
        search_from = 0
        while True:
            found = _find_environment(text, env, start=search_from)
            if found is None:
                break
            begin_start, end_end = found
            search_from = end_end
            if _in_spans(verbatim, begin_start) or _in_spans(covered, begin_start):
                continue
            blocks.append(
                _make_block(text, file, env, begin_start, end_end, begin_start)
            )
            covered.append((begin_start, end_end))
    return blocks


def _all_content_environments(region: str) -> list[tuple[str, int, int]]:
    """List every table content environment inside a float in occurrence order without
    dropping later ones.
    """
    found: list[tuple[str, int, int]] = []
    for env in LATEX_TABLE_ENVIRONMENTS:
        search_from = 0
        while True:
            hit = _find_environment(region, env, start=search_from)
            if hit is None:
                break
            begin_start, end_end = hit
            found.append((env, begin_start, end_end))
            search_from = end_end
    found.sort(key=lambda item: (item[1], item[2]))
    return found


def _make_block(
    text: str,
    file: str,
    environment: str,
    block_start: int,
    block_end: int,
    content_start: int,
    *,
    pgf: PgfTypeset | None = None,
    content_end: int | None = None,
) -> TableBlock:
    region = text[block_start:block_end]
    label_match = re.compile(r"\\label\s*\{([^{}]*)\}").search(region)
    caption = ""
    caption_found = _first_argument(region, "caption")
    if caption_found is not None:
        caption = caption_found[0]
    if not caption:
        # deluxetable uses tablecaption (second-review finding 19: previously omitted,
        # leaving public captions empty).
        tablecaption_found = _first_argument(region, "tablecaption")
        if tablecaption_found is not None:
            caption = tablecaption_found[0]
    footnotes = _table_footnotes(region)
    digest = hashlib.sha256(
        f"{file}:{block_start}:{block_end}:{environment}:{content_start}".encode()
    ).hexdigest()[:12]
    return TableBlock(
        table_id=f"tbl-{digest}",
        file=file,
        environment=environment,
        start_line=_line_of(text, block_start),
        end_line=_line_of(text, block_end - 1),
        content_start_line=_line_of(text, content_start),
        start_offset=block_start,
        end_offset=block_end,
        content_start_offset=content_start,
        content_end_offset=content_end if content_end is not None else block_end,
        label=label_match.group(1) if label_match else "",
        caption=caption,
        footnotes=footnotes,
        pgf_typeset=pgf,
    )


def _table_footnotes(region: str) -> list[tuple[str, str]]:
    """Extract literal tablenotetext footnote markers/text in occurrence order.

    Do not fabricate complex macro/commented forms; expose them accurately through gaps.
    """
    notes: list[tuple[str, str]] = []
    pattern = re.compile(r"\\tablenotetext\s*\{([^{}]*)\}\s*\{")
    for match in pattern.finditer(region):
        # Support nested braces in the second argument without guessing more complex expansion.
        depth = 1
        scan = match.end()
        text = ""
        while scan < len(region):
            char = region[scan]
            if char == "\\" and scan + 1 < len(region):
                text += region[scan : scan + 2]
                scan += 2
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    break
            text += char
            scan += 1
        notes.append((match.group(1), text.strip()))
    return notes


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _parse_pgf_command(text: str, command: str, start: int):
    """Parse command[opts]{arg} at an exact position; return (opts, arg, end)."""
    token = f"\\{command}"
    if not text.startswith(token, start):
        return None
    after = start + len(token)
    if after < len(text) and (text[after].isalnum() or text[after] == "@"):
        return None  # A prefix of a longer command is not this command.
    cursor = _skip_ws(text, after)
    options = ""
    if cursor < len(text) and text[cursor] == "[":
        close = text.find("]", cursor)
        if close == -1:
            return None
        options = text[cursor + 1 : close]
        cursor = _skip_ws(text, close + 1)
    if cursor < len(text) and text[cursor] == "\\":
        # Bare-macro forms such as pgfplotstabletypeset followed by a macro use the macro name as the argument.
        macro_match = re.compile(r"\\([A-Za-z@]+)").match(text, cursor)
        if macro_match is not None:
            return options, macro_match.group(0), macro_match.end()
        return None
    if cursor >= len(text) or text[cursor] != "{":
        return None
    close = _balanced_group(text, cursor)
    if close is None:
        return None
    return options, text[cursor + 1 : close], close + 1


def _scan_pgfplotstable(
    text: str, file: str, verbatim: list[tuple[int, int]]
) -> list[TableBlock]:
    blocks: list[TableBlock] = []
    search_from = 0
    while True:
        match = re.compile(r"\\pgfplotstabletypeset\b").search(text, search_from)
        if match is None:
            break
        search_from = match.end()
        if _in_spans(verbatim, match.start()):
            continue
        parsed = _parse_pgf_command(text, "pgfplotstabletypeset", match.start())
        if parsed is None:
            continue
        options, source_arg, end = parsed
        source_arg = source_arg.strip()
        macro = source_arg.removeprefix("\\")
        digest = hashlib.sha256(f"{file}:{match.start()}".encode()).hexdigest()[:12]
        # Associate PGF calls inside table floats with captions/labels/footnotes; when correspondence
        # cannot be established, leave fields empty and record uncertainty rather than guessing table numbers
        # (second-review finding 19).
        label = ""
        caption = ""
        footnotes: list[tuple[str, str]] = []
        notes: list[str] = []
        associated = False
        for float_env in _FLOAT_ENVIRONMENTS:
            float_from = 0
            while True:
                found = _find_environment(text, float_env, start=float_from)
                if found is None:
                    break
                begin_start, end_end = found
                float_from = end_end
                if not (begin_start <= match.start() < end_end):
                    continue
                region = text[begin_start:end_end]
                label_match = re.compile(r"\\label\s*\{([^{}]*)\}").search(region)
                label = label_match.group(1) if label_match else ""
                caption_found = _first_argument(region, "caption")
                caption = caption_found[0] if caption_found is not None else ""
                footnotes = _table_footnotes(region)
                associated = True
                break
            if associated:
                break
        if not associated:
            notes.append(
                "pgfplotstable 调用不在任何 table 浮动体内：标题／标签／"
                "脚注关联不确定，不猜表号"
            )
        block = TableBlock(
            table_id=f"tbl-{digest}",
            file=file,
            environment="pgfplotstable",
            start_line=_line_of(text, match.start()),
            end_line=_line_of(text, end - 1),
            content_start_line=_line_of(text, match.start()),
            start_offset=match.start(),
            end_offset=end,
            content_start_offset=match.start(),
            content_end_offset=end,
            label=label,
            caption=caption,
            footnotes=footnotes,
            notes=notes,
            pgf_typeset=PgfTypeset(
                file=file,
                line=_line_of(text, match.start()),
                options=options,
                source_arg=source_arg,
                macro=macro,
                offset=match.start(),
            ),
        )
        blocks.append(block)
    return blocks


def parse_pgf_reads(text: str, *, before_line: int | None = None) -> list[PgfRead]:
    """Parse pgfplotstableread[opts]{path} followed by its macro binding."""
    reads: list[PgfRead] = []
    search_from = 0
    while True:
        match = re.compile(r"\\pgfplotstableread\b").search(text, search_from)
        if match is None:
            break
        search_from = match.end()
        if before_line is not None and _line_of(text, match.start()) >= before_line:
            continue
        parsed = _parse_pgf_command(text, "pgfplotstableread", match.start())
        if parsed is None:
            continue
        options, path, end = parsed
        macro_match = re.compile(r"\s*\\([A-Za-z@]+)").match(text, end)
        if macro_match is None:
            continue
        reads.append(
            PgfRead(
                file="",
                line=_line_of(text, match.start()),
                options=options,
                path=path.strip(),
                macro=macro_match.group(1),
                offset=match.start(),
            )
        )
    return reads


def pgf_delimiter(options: str) -> str | None:
    """Declared pgfplotstable delimiter, or None if absent; never silently assume commas."""
    value = _latex_option_value(options, "col sep").strip().lower()
    if not value:
        return None
    if value == "comma":
        return ","
    if value == "semicolon":
        return ";"
    if value in {"space", "spaces"}:
        return " "
    if value == "tab":
        return "\t"
    if value == "pipe":
        return "|"
    raise ValueError(f"不支持的 col sep 声明：{value}")


def _latex_option_value(options: str, key: str) -> str:
    match = re.compile(rf"(?:^|,)\s*{re.escape(key)}\s*=\s*").search(options)
    if match is None:
        return ""
    cursor = match.end()
    if cursor < len(options) and options[cursor] == "{":
        close = _balanced_group(options, cursor)
        if close is None:
            return ""
        return options[cursor + 1 : close]
    end = options.find(",", cursor)
    return options[cursor:] if end == -1 else options[cursor:end]


def pgf_selected_columns(options: str, fieldnames: list[str]) -> list[str]:
    """Declared selected columns, or all columns when absent."""
    value = _latex_option_value(options, "columns")
    if not value:
        return list(fieldnames)
    return [part.strip() for part in value.split(",") if part.strip()]


def pgf_column_headers(options: str) -> dict[str, str]:
    """Extract column name display headers from columns/<name>/.style."""
    headers: dict[str, str] = {}
    for match in re.compile(r"columns/([^/\s{}]+)/\.style\s*=").finditer(options):
        cursor = _skip_ws(options, match.end())
        if cursor >= len(options) or options[cursor] != "{":
            continue
        close = _balanced_group(options, cursor)
        if close is None:
            continue
        style = options[cursor + 1 : close]
        header = _latex_option_value(style, "column name")
        if header:
            headers[match.group(1)] = header.strip()
    return headers


#: Allowlisted interpreted pgfplotstable top-level options; register all others as uninterpreted.
#: Avoid incomplete transform blacklists (second-review finding 9: sort/create on use/
#: string replace were previously ignored silently).
_PGF_KNOWN_TOP_OPTIONS = {"columns", "col sep"}
#: Interpreted column style keys for display headers and column types.
_PGF_KNOWN_STYLE_KEYS = {"column name", "column type", "string type", "numeric type"}


def _split_pgf_options(options: str) -> list[str]:
    """Split top-level options by brace depth, ignoring enclosed commas."""
    items: list[str] = []
    depth = 0
    current: list[str] = []
    for char in options:
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            items.append("".join(current))
            current = []
            continue
        current.append(char)
    items.append("".join(current))
    return items


def pgf_unexplained_transforms(options: str) -> list[str]:
    """List unparsed pgfplotstable filters/transforms outside the allowlist.

    Accept interpreted column selection, delimiters, display headers, and column types
    only. Register sorting/generated columns/row filtering/string replacement and other
    unknown transforms so callers retain gaps and never treat uninterpreted backing
    files as displayed tables.
    """
    unexplained: list[str] = []
    for item in _split_pgf_options(options):
        key = item.split("=", 1)[0].strip()
        if not key:
            continue
        if key in _PGF_KNOWN_TOP_OPTIONS:
            continue
        if key.startswith("columns/") and key.endswith("/.style"):
            continue
        unexplained.append(key)
    for match in re.compile(r"columns/([^/\s{}]+)/\.style\s*=").finditer(options):
        cursor = _skip_ws(options, match.end())
        if cursor >= len(options) or options[cursor] != "{":
            continue
        close = _balanced_group(options, cursor)
        if close is None:
            continue
        style = options[cursor + 1 : close]
        for style_item in _split_pgf_options(style):
            style_key = style_item.split("=", 1)[0].strip()
            if style_key and style_key not in _PGF_KNOWN_STYLE_KEYS:
                unexplained.append(f"columns/{match.group(1)}/.style 中的 {style_key}")
    return unexplained
