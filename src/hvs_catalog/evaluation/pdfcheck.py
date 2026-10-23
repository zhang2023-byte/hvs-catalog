"""Read-only synthetic PDF checks for existence, hash, opening, and file page bounds.

pypdf uses actual page trees/content streams for opening/page counts, not custom regex
heuristics. Headers/end markers are additional structural facts. Report
parser/readability failures accurately. Quote matching is auxiliary: matches cannot
prove object/science correctness, and unavailable text extraction is not itself a check
failure.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

_PDF_MAGIC = b"%PDF-"
_EOF_RE = re.compile(rb"%%EOF")


def _brief(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return text[:160] or exc.__class__.__name__


@dataclass(frozen=True)
class PdfAnalysis:
    """Structured PDF-byte analysis by an established parser, extracting text in page-tree
    order.
    """

    openable: bool
    page_count: int | None
    page_texts: list[str] = field(default_factory=list)
    notes: tuple[str, ...] = ()


def analyze_pdf(data: bytes, *, extract_text: bool = True) -> PdfAnalysis:
    """Open bytes, count actual pages, and optionally extract page text.

    Parser rejection, encryption, or missing EOF markers make documents unopenable with
    reasons. Text extraction failure affects auxiliary quote checks only, not opening
    status.
    """
    if not data.startswith(_PDF_MAGIC):
        return PdfAnalysis(openable=False, page_count=None, notes=("缺少 PDF 文件头",))
    notes: list[str] = []
    if not _EOF_RE.search(data[-1024:]):
        notes.append("文件缺少结束标记，可能被截断")
    try:
        reader = PdfReader(io.BytesIO(data))
    except (PdfReadError, ValueError, OSError) as exc:
        return PdfAnalysis(
            openable=False,
            page_count=None,
            notes=(*notes, f"成熟解析器无法打开：{_brief(exc)}"),
        )
    if reader.is_encrypted:
        return PdfAnalysis(
            openable=False, page_count=None, notes=(*notes, "文档已加密，无法检查")
        )
    try:
        page_count = len(reader.pages)
    except (PdfReadError, ValueError, OSError) as exc:
        return PdfAnalysis(
            openable=False,
            page_count=None,
            notes=(*notes, f"无法按页树确定页数：{_brief(exc)}"),
        )
    texts: list[str] = []
    if extract_text:
        for page in reader.pages:
            try:
                texts.append(page.extract_text() or "")
            except (PdfReadError, ValueError, OSError):
                texts.append("")
    return PdfAnalysis(
        openable=not notes,
        page_count=page_count,
        page_texts=texts,
        notes=tuple(notes),
    )


@dataclass(frozen=True)
class PdfInspection:
    """Objective PDF-file check results without scientific judgments."""

    name: str
    exists: bool
    sha256: str | None
    hash_matches: bool | None
    openable: bool | None
    page_count: int | None
    notes: tuple[str, ...] = ()

    @property
    def problems(self) -> list[str]:
        problems: list[str] = []
        if not self.exists:
            problems.append("文件不存在")
            return problems
        if self.hash_matches is False:
            problems.append("文件哈希与登记不符")
        if self.openable is False:
            problems.append("文件不是可打开的完整 PDF")
            problems.extend(self.notes)
        return problems


def inspect_pdf(path: Path, *, expected_sha256: str | None) -> PdfInspection:
    """Check a PDF and report all read failures rather than raising them."""
    path = Path(path)
    if not path.exists():
        return PdfInspection(
            name=path.name,
            exists=False,
            sha256=None,
            hash_matches=None,
            openable=None,
            page_count=None,
        )
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    analysis = analyze_pdf(data)
    hash_matches = None if expected_sha256 is None else digest == expected_sha256
    return PdfInspection(
        name=path.name,
        exists=True,
        sha256=digest,
        hash_matches=hash_matches,
        openable=analysis.openable,
        page_count=analysis.page_count,
        notes=analysis.notes,
    )


def extract_page_texts(data: bytes) -> list[str]:
    """Extract text best-effort in page-tree order; return an empty list if opening fails.

    Use only for auxiliary quote checks. Missing text means automatic verification
    unavailable, never evidence of an incorrect quote.
    """
    return analyze_pdf(data).page_texts


def quote_result(
    page_texts: list[str], *, page: int | None, quote: str
) -> tuple[str, str]:
    """Check quotes against extracted page text and return (result, explanation).

    matched finds text on the specified page, or anywhere if no ordinal is given;
    not_found means text was extractable but absent; not_extractable means unreliable
    extraction; skipped means no supplied quote.
    """
    if not quote.strip():
        return "skipped", "未提供引文，无从核对；空引文单独不判错"
    if not any(text.strip() for text in page_texts):
        return "not_extractable", "页面文字无法可靠提取，引文未自动核验"
    needle = quote.strip()
    if page is not None:
        if not 1 <= page <= len(page_texts):
            return "not_extractable", "页序号超出可提取范围，引文未自动核验"
        if not page_texts[page - 1].strip():
            return "not_extractable", f"第 {page} 页文字无法可靠提取，引文未自动核验"
        return (
            ("matched", f"第 {page} 页文本包含引文")
            if needle in page_texts[page - 1]
            else ("not_found", f"第 {page} 页文本未找到引文")
        )
    for index, text in enumerate(page_texts, start=1):
        if needle in text:
            return "matched", f"第 {index} 页文本包含引文"
    if any(not text.strip() for text in page_texts):
        return "not_extractable", "可提取页面未找到引文；部分页面无文本，全文未完成核验"
    return "not_found", "全文文本未找到引文"


def check_quote(data: bytes, *, page: int | None, quote: str) -> tuple[str, str]:
    """Parse bytes and run auxiliary quote checking; results are informational, not errors."""
    analysis = analyze_pdf(data)
    if not analysis.openable:
        return "not_extractable", "文件无法作为 PDF 打开，引文未自动核验"
    return quote_result(analysis.page_texts, page=page, quote=quote)


def read_material_bytes(path: Path) -> bytes:
    return Path(path).read_bytes()
