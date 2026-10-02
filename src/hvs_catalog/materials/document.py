"""One full-author-context conversion, native table origins, bounded grids.

The installed source_trace.ltxml observer reads native locators only. No author
macros are reconstructed. Every executed occurrence has its own identity, even
when the same source block is included twice. Source candidates without a
provable executed counterpart remain explicit gaps.
"""

from __future__ import annotations

import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from . import convert as conversion
from .sandbox import MAX_CONVERTER_INPUT_BYTES, MAX_CONVERTER_OUTPUT_BYTES

NS = "{http://dlmf.nist.gov/LaTeXML}"
_LOCATION = re.compile(r"^(.*);(?: from)? line (\d+)(?: col (\d+))?")


def _text(node) -> str:
    if node.tag == NS + "note":
        return ""  # mark and body are retained separately, never appended to IDs
    if node.tag == NS + "Math" and node.get("tex") is not None:
        return node.get("tex")
    return (node.text or "") + "".join(
        _text(child) + (child.tail or "") for child in node
    )


def _parse_grid(node) -> conversion.ParsedTable:
    if any(child is not node and child.tag == NS + "tabular" for child in node.iter()):
        raise ValueError("嵌套表保留原件，不认证展平网格")
    rows, headers, changes = [], 0, []
    for section in node:
        if section.tag not in {NS + "thead", NS + "tbody", NS + "tfoot"}:
            continue
        for row in section:
            if row.tag != NS + "tr":
                continue
            if section.tag == NS + "thead":
                if headers != len(rows):
                    raise ValueError("原生表头不在数据之前")
                headers += 1
            cells = []
            for cell in row:
                if cell.tag not in {NS + "td", NS + "th"}:
                    continue
                for note in cell.iter(NS + "note"):
                    changes.append(
                        {
                            "kind": "inline-note-separated",
                            "native_row_index": len(rows),
                            "native_cell_index": len(cells),
                            "note_xml": ET.tostring(note, encoding="unicode"),
                        }
                    )
                cells.append(
                    (
                        _text(cell).strip(),
                        int(cell.get("rowspan", 1)),
                        int(cell.get("colspan", 1)),
                    )
                )
            rows.append(cells)
    values, spans = conversion.grid(rows)
    if any(
        span["row_index"] < headers < span["row_index"] + span["rowspan"]
        for span in spans
    ):
        raise ValueError("跨度跨越表头/数据分组边界")
    head = values[:headers]
    return conversion.ParsedTable(
        "success",
        method="latexml",
        columns=conversion._columns(head, len(values[0])),
        header_rows=head,
        data_rows=values[headers:],
        raw_rows=values,
        merged_cells=spans,
        presentation_changes=changes,
        warnings=[] if head else ["原生转换没有明确 thead；保留所有行，不猜表头"],
    )


def _origin(node, inputs: Path, texts: dict[str, str]) -> dict | None:
    match = _LOCATION.match(node.get("hvs-locator", ""))
    if not match:
        return None
    try:
        file = Path(match[1]).resolve().relative_to(inputs.resolve()).as_posix()
        line, column = int(match[2]), int(match[3] or 0)
        lines = texts[file].splitlines(keepends=True)
        if not 1 <= line <= len(lines) or column > len(lines[line - 1].rstrip("\n")):
            return None
        return {
            "file": file,
            "line": line,
            "column": column,
            "offset": sum(map(len, lines[: line - 1])) + column,
        }
    except (ValueError, KeyError):
        return None


def convert_document(state: dict, *, publish, runner, availability) -> dict:
    """Return executed tables mapped to source blocks plus diagnostics/gaps."""
    avail = availability["latexmlc"]
    if not avail.available:
        return {
            "tables": [],
            "observed": set(),
            "diagnostics": [],
            "error": f"latexmlc 不可用：{avail.reason}",
        }
    root_file = state.get("body", {}).get("root")
    if not root_file:
        return {
            "tables": [],
            "observed": set(),
            "diagnostics": [],
            "error": "没有选定主稿",
        }
    available = {
        m["name"]: m["text"] for m in state["members"] if m.get("text") is not None
    }
    texts = {
        name: text
        for name, text in available.items()
        if name.lower().endswith((".tex", ".sty", ".cls", ".bbl", ".bib"))
    }
    from .tables import parse_pgf_reads
    from .texgraph import _parent_dir, _resolve_member

    needed = {entry["target"] for entry in state["body"].get("non_tex_includes", [])}
    for name, text in list(texts.items()):
        for read in parse_pgf_reads(text):
            if member := _resolve_member(available, _parent_dir(name), read.path):
                needed.add(member)
    texts.update({name: available[name] for name in needed if name in available})
    if root_file not in texts:
        return {
            "tables": [],
            "observed": set(),
            "diagnostics": [],
            "error": "选定主稿不可读",
        }
    if sum(len(t.encode()) for t in texts.values()) > MAX_CONVERTER_INPUT_BYTES:
        return {
            "tables": [],
            "observed": set(),
            "diagnostics": [],
            "error": "完整上下文超过转换输入字节上限",
        }
    diagnostics = []

    def save(name, data):
        ref = publish("outputs/conversion/" + name, data)
        diagnostics.append({"path": ref.path, "sha256": ref.sha256})

    with tempfile.TemporaryDirectory(prefix="hvs-document-") as temporary:
        workdir = Path(temporary)
        inputs = workdir / "inputs"
        inputs.mkdir()
        for name, text in texts.items():
            target = inputs / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode())
        trace = Path(__file__).with_name("source_trace.ltxml").resolve()
        command = [
            avail.executable,
            "--expire=-1",
            "--format=xml",
            "--novalidate",
            f"--preload={trace}",
            "--includestyles",
            "--nographicimages",
            "--nopictureimages",
            "--nosvg",
            "--path=inputs",
            f"--path=inputs/{Path(root_file).parent.as_posix()}",
            "--sourcedirectory=inputs",
            "--log=conversion.log",
            "--timeout=60",
            "--destination=document.xml",
            f"inputs/{root_file}",
        ]
        outcome = runner.run("latexmlc", command, workdir=workdir)
        save("stdout.txt", outcome.stdout)
        save("stderr.txt", outcome.stderr)
        save("observer.ltxml.txt", trace.read_bytes())
        payload = b""
        if (workdir / "document.xml").is_file():
            with (workdir / "document.xml").open("rb") as stream:
                payload = stream.read(MAX_CONVERTER_OUTPUT_BYTES + 1)
        save("document.xml", payload[:MAX_CONVERTER_OUTPUT_BYTES])
        errors = conversion.process_problems(outcome)
        if not payload or len(payload) > MAX_CONVERTER_OUTPUT_BYTES:
            errors.append("没有有界原生 XML 输出")
        if (
            outcome.returncode != 0
            or outcome.error
            or outcome.timed_out
            or outcome.exceeded_output
        ):
            return {
                "tables": [],
                "observed": set(),
                "diagnostics": diagnostics,
                "error": "；".join(errors),
            }
        try:
            if b"<!ENTITY" in payload or b"<!DOCTYPE" in payload:
                raise ValueError("转换 XML 不接受 DTD/实体声明")
            document = ET.fromstring(payload)
            if document.tag != NS + "document":
                raise ValueError("输出不是原生 LaTeXML document")
        except (ValueError, ET.ParseError) as exc:
            return {
                "tables": [],
                "observed": set(),
                "diagnostics": diagnostics,
                "error": str(exc),
            }
        # Actual author files loaded by the converter, not all supplied drafts.
        stderr = outcome.stderr.decode("utf-8", "replace")
        sources = {root_file}
        for path in re.findall(
            r"\((?:Processing (?:content|definitions)|Loading) (.*?)\.\.\.", stderr
        ):
            try:
                member = Path(path).resolve().relative_to(inputs.resolve()).as_posix()
            except ValueError:
                continue
            if member in texts:
                sources.add(member)
        from .cleaning import scan_view
        from .texgraph import _parent_dir, _resolve_member

        missing_local = []
        for name in sources:
            for declaration in re.finditer(
                r"\\(usepackage|RequirePackage|documentclass)\s*(?:\[[^\]]*\]\s*)?\{([^{}]+)\}",
                scan_view(texts[name]),
            ):
                suffix = ".cls" if declaration[1] == "documentclass" else ".sty"
                for package in declaration[2].split(","):
                    member = _resolve_member(
                        texts, _parent_dir(name), package.strip() + suffix
                    )
                    if member and member not in sources:
                        missing_local.append(member)
        if missing_local:
            errors.append(
                "作者本地包未被实际加载：" + ", ".join(sorted(set(missing_local)))
            )
        sources = sorted(sources)
        parents = {child: parent for parent in document.iter() for child in parent}
        results, observed, occurrences = [], set(), {}
        for node in document.iter(NS + "tabular"):
            origin = _origin(node, inputs, texts)
            matches = [
                block
                for block in state.get("table_blocks", [])
                if origin
                and block["file"] == origin["file"]
                and block.get("content_start_offset", -1)
                <= origin["offset"]
                < block.get("content_end_offset", -1)
            ]
            if len(matches) != 1:
                errors.append("已执行表的来源块不能唯一定位；不按表序或首表猜配")
                continue
            block = matches[0]
            base_id = block["table_id"]
            observed.add(base_id)
            occurrences[base_id] = occurrences.get(base_id, 0) + 1
            record = dict(block)
            if occurrences[base_id] > 1:
                record["table_id"] = f"{base_id}-occ-{occurrences[base_id]}"
            record.update(
                method="latexml",
                status="failed",
                error="",
                diagnostic_refs=diagnostics,
                mapping={
                    "granularity": "table-block",
                    "native_origin": origin,
                    "occurrence": occurrences[base_id],
                    "context_sources": sources,
                },
                context_sources=sources,
                warnings=[],
                attempts=[],
            )
            try:
                if missing_local or not re.search(r"Status:conversion:[02]\b", stderr):
                    raise ValueError("完整作者上下文／转换诊断不能证明，保留原件与缺口")
                if any(
                    child.tag == NS + "ERROR"
                    or "ltx_ERROR" in child.get("class", "").split()
                    for child in node.iter()
                ):
                    raise ValueError("原生表含错误节点／未知对象宏")
                parsed = _parse_grid(node)
                parsed.environment = block["environment"]
                # For simple literal source grids, independently check all rows,
                # cells and numeric pairing. Macro-generated tables are grounded
                # in this actual full-context conversion and block locator.
                excerpt = texts[block["file"]][
                    block["content_start_offset"] : block["content_end_offset"]
                ]
                original = conversion.parse_latex_table_excerpt(excerpt)
                if original.status == "success":
                    if problems := conversion.literal_correspondence(original, parsed):
                        raise ValueError("；".join(problems))
                    # Preserve source header provenance only when explicitly known.
                    if original.header_rows:
                        parsed.columns = original.columns
                ancestor = parents.get(node)
                while ancestor is not None and ancestor.tag not in {
                    NS + "table",
                    NS + "document",
                }:
                    ancestor = parents.get(ancestor)
                frame = (
                    ancestor
                    if ancestor is not None and ancestor.tag == NS + "table"
                    else node
                )
                caption = frame.find(NS + "caption")
                record["rendered_caption"] = (
                    _text(caption).strip() if caption is not None else ""
                )
                record["rendered_notes"] = [
                    {
                        "mark": _text(note.find(NS + "tag"))
                        if note.find(NS + "tag") is not None
                        else "",
                        "text": "".join(
                            _text(child) for child in note if child.tag != NS + "tag"
                        ).strip(),
                    }
                    for note in frame.iter(NS + "note")
                ]
                record["context_verified"] = not errors
                record["parsed"] = parsed
                record["status"] = "success"
            except (ValueError, TypeError) as exc:
                record["error"] = str(exc)
            record["attempts"] = [
                {
                    "method": "latexml",
                    "status": record["status"],
                    "error": record["error"],
                }
            ]
            results.append(record)
        from .flow import _DISCOVERY_RE
        from .ranges import object_context

        range_observations = []

        def prose(node):
            if node.tag in {
                NS + name for name in ("verbatim", "listing", "cite", "ref", "bibref")
            }:
                return ""
            return (node.text or "") + "".join(
                prose(child) + (child.tail or "") for child in node
            )

        for paragraph in document.iter(NS + "p"):
            text = prose(paragraph)
            for match in _DISCOVERY_RE.finditer(text):
                if object_context(text, match.start(), match.end()):
                    range_observations.append(
                        {
                            "notation": match[0],
                            "native_origin": _origin(paragraph, inputs, texts),
                            "context_verified": not errors,
                        }
                    )
        for record in results:
            record["context_verified"] = not errors
        return {
            "tables": results,
            "observed": observed,
            "diagnostics": diagnostics,
            "range_observations": range_observations,
            "error": "；".join(dict.fromkeys(errors)),
        }
