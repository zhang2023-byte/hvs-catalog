"""材料准备合成测试辅助：合成源码包、假转换器与空间准备。

全部材料为虚构合成内容，不读取真实论文、Gold 或最终评价预留资料。
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from hvs_catalog.materials.sandbox import (
    ConverterAvailability,
    ConverterRunner,
    ProcessOutcome,
)
from hvs_catalog.pipeline.acquisition.lifecycle import AcquisitionService
from hvs_catalog.workspace import initialize_workspace

PAPER = "2501.01111"

MAIN_TEX = r"""\documentclass{article}
\usepackage{booktabs,longtable,multirow}
\input{macros}
\begin{document}
\title{Synthetic HVS survey}
\section{Introduction}
\input{sec1}
\section{Velocities}
\pgfplotstableread[col sep=comma]{backing.csv}\velocitytable
\input{tables/velocity}
\begin{deluxetable}{lcc}
\tablecaption{Deluxe synthetic survey}
\label{tab:deluxe}
\tablehead{\colhead{Obj} & \colhead{RV} & \colhead{Err}}
\startdata
FIC-C & 405 & 9 \\
FIC-D & 610 & 14 \\
\enddata
\tablenotetext{a}{Synthetic deluxetable note with $\sigma$ and text.}
\tablenotetext{b}{\emph{Second} synthetic note}
\end{deluxetable}
\begin{table}
\caption{Measured radial velocities}
\label{tab:rv}
\begin{tabular}{lcc}
\toprule
Object & RV & Error \\
\midrule
\objname{FIC-A} & 525 & 10 \\
FIC-B & $-430 \pm 12$ & ... \\
\bottomrule
\end{tabular}
\end{table}
\begin{longtable}{lcc}
\caption{Long survey table}\\
\label{tab:long}\\
Name & PM & PMerr \\
\hline
\endfirsthead
Name & PM & PMerr \\
\hline
\endhead
FIC-C & 8.1 & 0.3 \\
FIC-D & 9.4 & 0.2 \\
\hline
\end{longtable}
\appendix
\input{appendix}
\bibliography{refs}
\input{refs.bbl}
\end{document}
"""

MACROS_TEX = r"\newcommand{\objname}[1]{\textbf{#1}}"
MACROS_TEX += "\n\\newcommand{\\rvunit}{km~s$^{-1}$}"

SEC1_TEX = (
    "We confirm five high-velocity stars HVS1--HVS5 in the survey footprint.\n"
    "The candidates span 1-5 in our internal numbering; see the tables below.\n"
    "The subset 1,3,5 is discussed in the appendix.\n"
)

VELOCITY_TEX = (
    "\\begin{table}\n"
    "\\caption{pgfplotstable rendered velocities}\n"
    "\\label{tab:pgf}\n"
    "\\pgfplotstabletypeset[columns={name,rv},\n"
    "  columns/name/.style={column name=Object},\n"
    "  columns/rv/.style={column name=RV}]{\\velocitytable}\n"
    "\\end{table}\n"
)

APPENDIX_TEX = (
    "\\section{Appendix}\n"
    "Additional notes: \\verb|100% not a comment| stays literal.\n"
    "% this comment line is removed but keeps its line\n"
    "End of appendix.\n"
)

BACKING_CSV = b"name,rv,error\nFIC-A,525,10\nFIC-B,-430,12\n"

REFS_BBL = b"\\begin{thebibliography}{1}\n\\bibitem{synthetic} Synthetic reference.\n\\end{thebibliography}\n"

NOTES_TXT = b"synthetic notes; not a table\n"

FITS_BYTES = b"SIMPLE  =                    T / synthetic not-really-fits payload"

MEMBERS = {
    "main.tex": MAIN_TEX.encode("utf-8"),
    "macros.tex": MACROS_TEX.encode("utf-8"),
    "sec1.tex": SEC1_TEX.encode("utf-8"),
    "tables/velocity.tex": VELOCITY_TEX.encode("utf-8"),
    "appendix.tex": APPENDIX_TEX.encode("utf-8"),
    "backing.csv": BACKING_CSV,
    "refs.bbl": REFS_BBL,
    "notes.txt": NOTES_TXT,
    "payloads/data.fits": FITS_BYTES,
}

DRAFT_TEX = (
    "\\documentclass{article}\n\\begin{document}old draft, not referenced\n"
    "\\end{document}\n"
)


def make_tar_gz(members: dict[str, bytes]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def synthetic_package(extra: dict[str, bytes] | None = None) -> bytes:
    members = dict(MEMBERS)
    if extra:
        members.update(extra)
    return make_tar_gz(members)


def prepared_space(tmp_path: Path, payload: bytes | None = None, *, paper: str = PAPER):
    """初始化空间并导入合成包；返回 (space, ImportResult)。"""
    space_dir = tmp_path / "space"
    initialize_workspace(space_dir)
    from hvs_catalog.workspace import open_workspace

    space = open_workspace(space_dir)
    package = payload if payload is not None else synthetic_package()
    source_path = Path(tmp_path) / "source.tar.gz"
    source_path.write_bytes(package)
    result = AcquisitionService(space).import_local_package(
        source_path, arxiv_id=paper, version=1, provenance="synthetic R11 test"
    )
    return space, result


class FakeConverterRunner(ConverterRunner):
    """确定性假转换器：按脚本返回可用性并在 workdir 写出输出。"""

    def __init__(
        self,
        *,
        latexml_html: str | None = None,
        latexml_xml: str | None = None,
        latexml_stderr: str = "Status:conversion:0\n",
        latexml_returncode: int = 0,
        pandoc_html: str | None = None,
        available: dict[str, bool] | None = None,
        timeout_tools: set[str] | None = None,
    ) -> None:
        self.latexml_html = latexml_html
        self.latexml_xml = latexml_xml
        self.latexml_stderr = latexml_stderr
        self.latexml_returncode = latexml_returncode
        self.pandoc_html = pandoc_html
        self.available = (
            available
            if available is not None
            else {
                "latexmlc": True,
                "pandoc": True,
            }
        )
        self.timeout_tools = timeout_tools or set()
        self.runs: list[tuple[str, tuple[str, ...]]] = []

    def availability(self, tool: str) -> ConverterAvailability:
        if tool not in self.available:
            return ConverterAvailability(
                tool, available=False, reason=f"未知转换器：{tool}"
            )
        if not self.available[tool]:
            return ConverterAvailability(
                tool, available=False, reason="系统未安装（测试脚本）"
            )
        version = "0.8.8-test" if tool == "latexmlc" else "3.9-test"
        return ConverterAvailability(
            tool, available=True, executable=f"/fake/{tool}", version=version
        )

    def run(
        self,
        tool: str,
        command: list[str],
        *,
        workdir: Path,
        timeout: int = 120,
    ) -> ProcessOutcome:
        self.runs.append((tool, tuple(command)))
        if tool in self.timeout_tools:
            return ProcessOutcome(
                returncode=None,
                stdout=b"",
                stderr=b"timed out",
                timed_out=True,
                error=f"{tool} 超过 {timeout} 秒时间上限，进程被终止",
            )
        if tool == "latexmlc":
            if "--format=xml" in command:
                xml, loaded = self._native_fixture(command, workdir)
                (workdir / "document.xml").write_text(xml, encoding="utf-8")
                stderr = (loaded + self.latexml_stderr).encode()
            else:
                (workdir / "latexml.html").write_text(
                    self.latexml_html or "", encoding="utf-8"
                )
                stderr = self.latexml_stderr.encode()
            return ProcessOutcome(
                returncode=self.latexml_returncode, stdout=b"", stderr=stderr
            )
        if tool == "pandoc":
            destination = next(
                (
                    arg.split("=", 1)[1]
                    for arg in command
                    if arg.startswith("--output=")
                ),
                "pandoc.html",
            )
            (workdir / destination).write_text(self.pandoc_html or "", encoding="utf-8")
            return ProcessOutcome(returncode=0, stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected tool {tool}")

    def _native_fixture(self, command, workdir):
        """Scripted native-origin fixture, NOT an author-macro interpreter.

        Literal grids can be mechanically serialized. Macro cases require an
        explicit expected HTML/XML fixture; real context tests use LaTeXML.
        """
        import xml.etree.ElementTree as ET

        from bs4 import BeautifulSoup

        from hvs_catalog.materials.convert import (
            parse_html_tables,
            parse_latex_table_excerpt,
        )
        from hvs_catalog.materials.document import NS
        from hvs_catalog.materials.tables import scan_tables
        from hvs_catalog.materials.texgraph import resolve_manuscript

        inputs = workdir / "inputs"
        texts = {
            p.relative_to(inputs).as_posix(): p.read_text()
            for p in inputs.rglob("*")
            if p.is_file()
        }
        root = command[-1].removeprefix("inputs/")
        graph = resolve_manuscript(texts, root_file=root)
        loaded = "".join(
            f"(Processing content {inputs / entry.name}... 0 sec)\n"
            for entry in graph.files
        )
        if self.latexml_xml is not None:
            return self.latexml_xml.replace("@INPUTS@", str(inputs)), loaded
        document = ET.Element(NS + "document")
        for block in scan_tables(graph):
            if block.environment == "pgfplotstable":
                continue
            source = texts[block.file]
            offset = block.content_start_offset
            line = source.count("\n", 0, offset) + 1
            column = offset - (source.rfind("\n", 0, offset) + 1)
            table = ET.SubElement(
                document,
                NS + "tabular",
                {
                    "hvs-locator": f"{inputs / block.file}; from line {line} col {column} to line {line} col {column}"
                },
            )
            excerpt = source[block.content_start_offset : block.content_end_offset]
            parsed = parse_latex_table_excerpt(excerpt)
            if block.environment == "tabular" and self.latexml_html is not None:
                parsed, _ = parse_html_tables(self.latexml_html, method="fake")
                if BeautifulSoup(self.latexml_html, "html.parser").find(
                    class_="ltx_ERROR"
                ):
                    ET.SubElement(table, NS + "ERROR").text = "scripted undefined macro"
            if parsed.status != "success":
                ET.SubElement(table, NS + "ERROR").text = parsed.error
                continue
            for tag, rows in (
                ("thead", parsed.header_rows),
                ("tbody", parsed.data_rows),
            ):
                section = ET.SubElement(table, NS + tag)
                for values in rows:
                    row = ET.SubElement(section, NS + "tr")
                    for value in values:
                        ET.SubElement(row, NS + "td").text = value
        return ET.tostring(document, encoding="unicode"), loaded


def native_materials_probe(tmp_path, members):
    """Run the installed converter through the public API on synthetic inputs."""
    import pytest

    from hvs_catalog.materials.sandbox import SandboxedConverterRunner
    from hvs_catalog.materials.service import MaterialsService
    from hvs_catalog.records.common import ArtifactRef
    from hvs_catalog.records.tabular import read_table_records

    runner = SandboxedConverterRunner()
    if not runner.availability("latexmlc").available:
        pytest.skip("safe native LaTeXML not available on this platform")
    package = make_tar_gz(
        {
            name: value.encode() if isinstance(value, str) else value
            for name, value in members.items()
        }
    )
    space, imported = prepared_space(tmp_path, package)
    try:
        service = MaterialsService(space, runner=runner)
        handle, report = service.prepare_materials(
            request_key="native-context", paper=imported.canonical_id
        )
        assert report.outcome == "finished", report.to_dict()
        bundle = service.get_prepared_input(handle.execution_id)
        rows = []
        for table in bundle["tables"]:
            if table["status"] == "success":
                records = read_table_records(
                    space.read_file(
                        ArtifactRef(
                            path=table["ecsv_path"], sha256=table["ecsv_sha256"]
                        )
                    ),
                    name="table.ecsv",
                )
                rows.append(
                    [[row[name] for name in records.columns] for row in records.rows]
                )
        return bundle, rows, service.consume_prepared_input(handle.execution_id)
    finally:
        space.close()


def good_latexml_html(rows: list[tuple[str, str, str]]) -> str:
    body = "\n".join(
        f"<tr><td>{name}</td><td>{rv}</td><td>{err}</td></tr>" for name, rv, err in rows
    )
    return (
        "<html><body><table><thead><tr>"
        "<th>Object</th><th>RV</th><th>Error</th></tr></thead>"
        f"<tbody>{body}</tbody></table></body></html>"
    )
