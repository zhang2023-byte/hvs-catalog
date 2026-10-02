"""包内表格环境扫描：行范围、身份、标题／标签与 backing CSV 关联。

在主稿可达文件的**去注释文本**（行数与原文件一致）上扫描首轮支持的
表环境：``tabular``／``tabular*``、``longtable``、``deluxetable``／
``deluxetable*``，含外层 ``table``／``table*`` 浮动体；每个表块得到程
序生成的稳定身份与原文行范围。verbatim 区域不参与扫描。

同时识别 ``pgfplotstabletypeset`` 命令及其 ``\\pgfplotstableread`` 读入
关联：backing CSV 的字面路径、分隔符、选列与显示表头可确定时直读，
不能解释的过滤／变换保留缺口，不把完整 backing 文件当成实际呈现表。

LaTeX 行／单元解析的基础工具迁自旧仓
``stella_hvs/src/stella/lit/catalog_extraction.py``（参考提交 8421c98，
MIT 许可），按新仓设计改造为行范围准确、按表隔离的结构扫描。
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
    """一个表块的登记：身份、环境、文件与行范围。"""

    table_id: str
    file: str
    environment: str  # 内容环境（tabular/longtable/deluxetable…/pgfplotstable）
    start_line: int  # 块起始（外层浮动体或环境本身的 \begin 行）
    end_line: int  # 块结束（含 \end 行）
    content_start_line: int  # 内容环境 \begin 行
    start_offset: int = 0
    end_offset: int = 0
    content_start_offset: int = 0
    content_end_offset: int = 0
    label: str = ""
    caption: str = ""
    # 脚注（\tablenotetext{a}{…} 等）：标记与原文按出现顺序保留。
    footnotes: list[tuple[str, str]] = dataclass_field(default_factory=list)
    # pgfplotstable 关联（仅 pgfplotstable 块填写）。
    pgf_typeset: PgfTypeset | None = None
    # 扫描层说明（如标题关联不确定），随表块公开。
    notes: list[str] = dataclass_field(default_factory=list)

    def source_span(self) -> dict:
        return {
            "file": self.file,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }


@dataclass(frozen=True)
class PgfTypeset:
    """一个 ``\\pgfplotstabletypeset`` 调用的解析结果。"""

    file: str
    line: int
    options: str
    source_arg: str
    macro: str
    offset: int = 0


@dataclass(frozen=True)
class PgfRead:
    """一个 ``\\pgfplotstableread`` 读入的解析结果。"""

    file: str
    line: int
    options: str
    path: str
    macro: str
    offset: int = 0


def _verbatim_spans(text: str) -> list[tuple[int, int]]:
    """标出 verbatim 环境与行内 ``\\verb`` 覆盖的字符区间（含起止指令）。

    行内 ``\\verb|...|`` 中的字面表环境／pgf 命令是展示文本，不是实
    际结构（二次审核发现 23）；掩蔽视图统一处理正文、表与 PGF 读入。
    以掩蔽视图与原文的逐字符差异推导区间；被掩蔽内容中的空白与掩蔽
    占位相同，区间可能在空白处断开——结构指令位置（非空白）始终落在
    区间内，成员判定不受影响。
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
    """按同名环境嵌套计数查找一个完整环境；返回 (begin_start, end_end)。"""
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
    """返回 ``{`` 对应 ``}`` 的结束位置（不含）；考虑转义与一层嵌套计数。"""
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
    """取首参数、命令起点和参数末尾（左闭右开）；兼容短标题。"""
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
    """扫描主稿图中的表块；按文件顺序与位置排序。"""
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
            # 浮动体内的每个内容环境各自登记为一个表块：同一浮动体
            # 放两张表时不能只取首张、静默丢第二张。
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
    """列出浮动体内全部表内容环境（按出现顺序；不取首张丢其余）。"""
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
        # deluxetable 的标准标题命令是 \tablecaption（二次审核发现 19：
        # 此前未读取，公开 caption 为空）。
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
    """提取 ``\\tablenotetext{a}{…}`` 脚注标记与原文（按出现顺序）。

    只处理字面命令；复杂宏或被注释的形式不伪造，交给缺口机制如实呈现。
    """
    notes: list[tuple[str, str]] = []
    pattern = re.compile(r"\\tablenotetext\s*\{([^{}]*)\}\s*\{")
    for match in pattern.finditer(region):
        # 第二个花括号参数支持一层嵌套，不猜测更复杂的展开。
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
    """在精确位置解析 ``\\command[opts]{arg}``；返回 (opts, arg, end)。"""
    token = f"\\{command}"
    if not text.startswith(token, start):
        return None
    after = start + len(token)
    if after < len(text) and (text[after].isalnum() or text[after] == "@"):
        return None  # 更长命令名的前缀，不是本命令
    cursor = _skip_ws(text, after)
    options = ""
    if cursor < len(text) and text[cursor] == "[":
        close = text.find("]", cursor)
        if close == -1:
            return None
        options = text[cursor + 1 : close]
        cursor = _skip_ws(text, close + 1)
    if cursor < len(text) and text[cursor] == "\\":
        # 裸宏形式（如 ``\\pgfplotstabletypeset\\\\btable``）：宏名作参数
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
        # PGF 调用位于外层 table 浮动体内时关联其标题／标签／脚注；不能
        # 证明关联时保持为空并在 notes 标注不确定，不猜表号（二次审核
        # 发现 19）。
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
    """解析 ``\\pgfplotstableread[opts]{path}\\macro`` 读入指令。"""
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
    """pgfplotstable 的声明分隔符；未声明返回 None（不静默猜逗号）。"""
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
    """pgfplotstable 声明的选列；未声明时为全部列。"""
    value = _latex_option_value(options, "columns")
    if not value:
        return list(fieldnames)
    return [part.strip() for part in value.split(",") if part.strip()]


def pgf_column_headers(options: str) -> dict[str, str]:
    """从 ``columns/<name>/.style`` 中提取 ``column name`` 显示表头。"""
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


#: pgfplotstable 已解释的顶层选项（白名单）：其余选项一律登记未解释，
#: 不再维护会漏掉新变换的黑名单（二次审核发现 9：sort／create on use／
#: string replace 等曾被静默忽略）。
_PGF_KNOWN_TOP_OPTIONS = {"columns", "col sep"}
#: columns/<name>/.style 内已解释的样式键（显示表头与列类型）。
_PGF_KNOWN_STYLE_KEYS = {"column name", "column type", "string type", "numeric type"}


def _split_pgf_options(options: str) -> list[str]:
    """按花括号深度拆分顶层选项（忽略括号内的逗号）。"""
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
    """列出未解释的 pgfplotstable 选项（白名单之外的过滤／变换）。

    只接受已经解释的选项（选列、分隔符、显示表头与列类型）；排序、
    生成列、行过滤、字符串替换等未解释变换一律登记，调用方保留缺口，
    不把未解释的 backing 文件当成实际呈现表。
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
