"""合成 PDF 材料的只读检查：存在、哈希、可打开与文件页序号边界。

可打开性与页数由成熟解析器 pypdf 按实际页树和页内容流判定，不使用
自写正则特征；文件头与结束标记只作为附加的结构事实。解析器无法打开
或无法可靠提取文本时如实报告，不当作检查通过。引文匹配仍只是辅助
结果——命中不能证明对象或科学判断正确，无法提取也不算检查失败。
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
    """成熟解析器对一份 PDF 字节的结构化判定；文本按页树顺序提取。"""

    openable: bool
    page_count: int | None
    page_texts: list[str] = field(default_factory=list)
    notes: tuple[str, ...] = ()


def analyze_pdf(data: bytes) -> PdfAnalysis:
    """用成熟解析器打开字节流并按实际页树统计页数、提取每页文本。

    解析器拒绝、文档加密或缺少结束标记时判为不可打开并给出说明；页
    文本提取失败只影响引文辅助核对，不影响可打开性判定。
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
    """一次 PDF 文件检查的客观结果；不包含科学判断。"""

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
    """检查一个 PDF 文件；任何读取失败都如实报告，不抛出。"""
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
    """按实际页树顺序尽力提取文本；无法打开时返回空列表。

    只用于引文的辅助核对：提取不到文本时调用方据此报告“无法自动核
    验”，不能当作引文错误。
    """
    return analyze_pdf(data).page_texts


def quote_result(
    page_texts: list[str], *, page: int | None, quote: str
) -> tuple[str, str]:
    """在已提取的页文本中核对引文；返回（结果, 说明）。

    - matched：在指定页（未给页序号时全文）找到引文文本
    - not_found：能提取文本但未找到引文
    - not_extractable：页面文字无法可靠提取
    - skipped：未提供引文，无从核对
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
    """解析字节流后做引文辅助核对；结果为辅助信息，不构成错误。"""
    analysis = analyze_pdf(data)
    if not analysis.openable:
        return "not_extractable", "文件无法作为 PDF 打开，引文未自动核验"
    return quote_result(analysis.page_texts, page=page, quote=quote)


def read_material_bytes(path: Path) -> bytes:
    return Path(path).read_bytes()
