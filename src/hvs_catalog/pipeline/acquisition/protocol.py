"""arXiv 协议层：标识解析、查询构造、Atom 解析与源码下载入口。

本模块的解析与构造函数是纯函数，不产生网络或磁盘副作用；
:class:`ArxivApiService` 组合传输客户端完成实际请求，并向获取生命周
期层暴露结构化结果与错误分类：

- 空结果（合法 Atom、总数为 0）与请求失败是不同结果，失败抛异常。
- 分页异常（总数缺失/非法、起始序号不符、应有条目却为空、未到末页
  却被截断、条目重复、超出 API 检索上限、批次中出现未请求的条目）
  作为 :class:`PaginationAnomaly` 数据暴露，绝不静默抹平；覆盖是否完
  整由生命周期层根据这些数据判断。
- 论文标识永不编造：畸形标识、缺失版本、与请求不匹配的返回一律拒
  绝。新式标识（``2101.01234v2``）与旧式标识（``astro-ph/0507001v3``）
  都受支持；元数据同时保留无版本身份、具体版本号与 API 原始标识
  URL（溯源依据）。
- 查询必须显式给定首提日期范围（按 UTC 解释）与稳定排序，分页切片
  受 API 上限约束；分页窗口、单批标识数与请求 URL 长度在发送前
  受工程护栏检查，超出即拒绝并报告，不依赖服务端截断。
- 源码下载要求先冻结具体版本号，并在交付前严格校验最终 URL 与请
  求的论文/版本一一对应（仅接受官方 ``/e-print/``/``/src/`` 路径上
  的精确版本标识）；HTML 错误页与 PDF-only 以“不可用”区分上抛，
  不做归档解析（归档安全由后续组件负责）。Atom 条目中的
  ``http://arxiv.org/abs/...`` 是历史标识符形式，按标识符结构校
  验；仅适用于实际出站请求的 HTTPS/端口/凭据约束在传输层执行。
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

from .rate_limit import SystemClock
from .transport import ArxivHttpClient, ArxivHttpError, TransportResponse

#: 官方入口：API 查询与源码包下载（e-print）。
ARXIV_API_BASE_URL = "https://export.arxiv.org/api/query"
ARXIV_EPRINT_BASE_URL = "https://arxiv.org/e-print/"

#: API 分页限制：单页条目上限与检索总量上限（超出部分无法建立覆盖）。
MAX_PAGE_SIZE = 2000
RETRIEVAL_CEILING = 30000

#: 按标识批量查询的单批上限与请求 URL 长度上限。
#: 两者都是工程保守护栏，不是官方声明的能力上限：达到上限时拒绝发
#: 送并明确报告，由调用方拆小批次或缩小条件，绝不依赖服务端截断。
MAX_ID_LIST_LENGTH = 200
MAX_REQUEST_URL_BYTES = 8192

#: 生产环境的响应界限：API 响应与源码包的大小、超时。
API_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
SOURCE_MAX_RESPONSE_BYTES = 256 * 1024 * 1024
API_TIMEOUT = 60.0
SOURCE_TIMEOUT = 180.0

#: 源码下载的显式内容协商声明。
#:
#: 显式声明能够接收并分类的表示类型；不保证服务端接受请求——
#: tar/gzip 源码包是交付目标；PDF-only 与 HTML 错误页也会被接收后按
#: 内容类型分类（不支持/不可用），``*/*`` 兜底保证错误页不被内容协
#: 商挡在分类之前。传输编码声明 ``identity``：本地读取不做传输层解
#: 压，避免把压缩响应误当原始字节。不伪造浏览器身份、不换端点。
SOURCE_ACCEPT_HEADERS = {
    "Accept": (
        "application/x-eprint-tar, application/gzip, application/x-gzip, "
        "application/x-tar, application/pdf, */*"
    ),
    "Accept-Encoding": "identity",
}

ATOM_NS = "http://www.w3.org/2005/Atom"
ARXIV_SCHEMA_NS = "http://arxiv.org/schemas/atom"
OPENSEARCH_NS = "http://a9.com/-/spec/opensearch/1.1/"

_ABS_HOSTS = ("arxiv.org", "export.arxiv.org")

_MODERN_ID_PATTERN = re.compile(r"^(\d{4})\.(\d{4,5})(?:v(\d{1,5}))?$")
_LEGACY_ID_PATTERN = re.compile(r"^([a-z-]+(?:\.[A-Za-z-]+)?)/(\d{7})(?:v(\d{1,5}))?$")
_CATEGORY_PATTERN = re.compile(r"^[a-z]+(?:-[a-z]+)*(?:\.[A-Za-z-]+)?$")

_SORT_BY_VALUES = ("submittedDate", "lastUpdatedDate")
_SORT_ORDER_VALUES = ("ascending", "descending")

_LOOKUP_FOUND = "found"
_LOOKUP_NOT_FOUND = "not_found"
_LOOKUP_VERSION_MISMATCH = "version_mismatch"


class ArxivProtocolError(Exception):
    """协议层错误基类。"""


class ArxivIdentifierError(ArxivProtocolError, ValueError):
    """arXiv 标识畸形、缺版本或与请求不匹配。"""


class ArxivQueryError(ArxivProtocolError, ValueError):
    """检索请求不满足显式范围或取值约束。"""


class ArxivResponseError(ArxivProtocolError):
    """服务返回的内容不是可解析的 Atom 结果（含 HTML 错误页）。"""


class ArxivSourceError(ArxivProtocolError):
    """源码包下载内容异常（如空响应），不能当作成功。"""


class ArxivSourceUnavailableError(ArxivSourceError):
    """该版本没有可用的 LaTeX 源码包；``reason`` 说明不可用原因。

    ``reason`` 取值：``not_found``（该版本无源码）、``html_error``
    （返回 HTML 错误页）、``pdf_only``（只有 PDF 提交）。
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _normalize_space(text: str | None) -> str:
    return " ".join((text or "").split())


def _atom(name: str) -> str:
    return f"{{{ATOM_NS}}}{name}"


def _arxiv(name: str) -> str:
    return f"{{{ARXIV_SCHEMA_NS}}}{name}"


def _opensearch(name: str) -> str:
    return f"{{{OPENSEARCH_NS}}}{name}"


def _valid_month(yymm: str) -> bool:
    month = int(yymm[2:4])
    return 1 <= month <= 12


@dataclass(frozen=True)
class ArxivIdentifier:
    """解析后的 arXiv 论文标识：无版本身份 + 具体版本（可选）。

    ``canonical_id`` 是无版本身份（新式 ``2101.01234`` 或旧式
    ``astro-ph/0507001``），同一论文的所有版本共享该身份；``version``
    为具体版本号，下载源码前必须固定。
    """

    canonical_id: str
    version: int | None
    is_legacy: bool

    @property
    def versioned_id(self) -> str:
        if self.version is None:
            return self.canonical_id
        return f"{self.canonical_id}v{self.version}"

    def abs_url(self) -> str:
        return f"http://arxiv.org/abs/{self.versioned_id}"

    def eprint_url(self) -> str:
        return f"{ARXIV_EPRINT_BASE_URL}{self.versioned_id}"


def parse_arxiv_identifier(text: str) -> ArxivIdentifier:
    """严格解析 arXiv 标识；畸形输入抛 :class:`ArxivIdentifierError`。

    接受带或不带版本号的新式标识（``2101.01234``、``2101.01234v2``）
    与旧式标识（``astro-ph/0507001``、``astro-ph/0507001v3``、
    ``math.AG/0309136``）。内部空白、错误版本号（如 ``v0``）、非法
    月份或任何其他形式一律拒绝，不做猜测。
    """
    if not isinstance(text, str):
        raise ArxivIdentifierError(f"arXiv 标识必须是字符串：{text!r}")
    value = text.strip()
    if not value or value != text or any(ch.isspace() for ch in value):
        raise ArxivIdentifierError(f"arXiv 标识包含空白或为空：{text!r}")
    modern = _MODERN_ID_PATTERN.match(value)
    if modern is not None:
        yymm, serial, version_text = modern.group(1), modern.group(2), modern.group(3)
        if not _valid_month(yymm):
            raise ArxivIdentifierError(f"arXiv 标识月份非法：{text!r}")
        version = int(version_text) if version_text else None
        if version is not None and version < 1:
            raise ArxivIdentifierError(f"arXiv 版本号从 1 开始：{text!r}")
        return ArxivIdentifier(
            canonical_id=f"{yymm}.{serial}", version=version, is_legacy=False
        )
    legacy = _LEGACY_ID_PATTERN.match(value)
    if legacy is not None:
        subject, digits, version_text = (
            legacy.group(1),
            legacy.group(2),
            legacy.group(3),
        )
        if not _valid_month(digits):
            raise ArxivIdentifierError(f"arXiv 标识月份非法：{text!r}")
        version = int(version_text) if version_text else None
        if version is not None and version < 1:
            raise ArxivIdentifierError(f"arXiv 版本号从 1 开始：{text!r}")
        return ArxivIdentifier(
            canonical_id=f"{subject}/{digits}", version=version, is_legacy=True
        )
    raise ArxivIdentifierError(f"无法识别的 arXiv 标识：{text!r}")


def parse_entry_id_url(url: str) -> ArxivIdentifier:
    """从 Atom 条目的 ``<id>`` URL 解析标识；结构不符即拒绝。

    严格结构：``http(s)://<官方主机>/abs/<精确的 arXiv 标识>``，仅
    接受 http/https 历史形式与对应标准端口（http→80/缺省、
    https→443/缺省）；拒绝其他协议（如 ftp）、凭据、非标准端口、
    额外路径段、查询、fragment 与空白。这些 URL 是标识符，不是出站
    请求：官方老源可合法使用 ``http://``，因此这里不施加仅适用于实
    际出站请求的 HTTPS 约束（见传输层出站预检）。本校验独立于注入
    的传输：不依赖客户端层是否执行了网络侧检查。
    """
    if not isinstance(url, str) or not url or any(ch.isspace() for ch in url):
        raise ArxivResponseError(f"条目 id 包含空白或非字符串：{url!r}")
    try:
        split = urllib.parse.urlsplit(url)
        port = split.port
    except ValueError as exc:
        raise ArxivResponseError(f"条目 id URL 无法解析：{url!r}") from exc
    scheme = split.scheme.lower()
    if scheme not in ("http", "https"):
        raise ArxivResponseError(f"条目 id 协议非法（{scheme!r}）：{url!r}")
    host = (split.hostname or "").lower()
    if host not in _ABS_HOSTS:
        raise ArxivResponseError(f"条目 id 不是官方 arXiv abs URL：{url!r}")
    if split.username or split.password:
        raise ArxivResponseError(f"条目 id 带有凭据：{url!r}")
    standard_port = 80 if scheme == "http" else 443
    if port is not None and port != standard_port:
        raise ArxivResponseError(
            f"条目 id 端口非标准（{port}，{scheme} 仅允许缺省或 {standard_port}）：{url!r}"
        )
    if split.query or split.fragment:
        raise ArxivResponseError(f"条目 id 带有查询或 fragment：{url!r}")
    path = split.path
    prefix = "/abs/"
    if not path.startswith(prefix):
        raise ArxivResponseError(f"条目 id 路径不是 /abs/<标识>：{url!r}")
    tail = path[len(prefix) :]
    try:
        return parse_arxiv_identifier(tail)
    except ArxivIdentifierError as exc:
        raise ArxivResponseError(f"条目 id 中的 arXiv 标识畸形：{url!r}") from exc


@dataclass(frozen=True)
class ArxivSearchRequest:
    """一次显式受限的检索：首提日期范围（UTC）、检索词、类别与分页。

    日期按 UTC 解释，对应 arXiv 的首提（initial submission）时间；范
    围闭区间覆盖 ``submitted_from`` 与 ``submitted_to`` 两天。排序显
    固定为 ``submittedDate``（默认）或 ``lastUpdatedDate``，方向显式
    给定，保证分页可回查、可恢复。检索词与类别至少提供其一。
    """

    submitted_from: date
    submitted_to: date
    terms: str = ""
    categories: tuple[str, ...] = ()
    sort_by: str = "submittedDate"
    sort_order: str = "ascending"
    start: int = 0
    page_size: int = 100

    def __post_init__(self) -> None:
        if not isinstance(self.submitted_from, date) or not isinstance(
            self.submitted_to, date
        ):
            raise ArxivQueryError("首提日期范围必须提供 date 对象")
        if self.submitted_from > self.submitted_to:
            raise ArxivQueryError(
                f"首提日期范围颠倒：{self.submitted_from} > {self.submitted_to}"
            )
        # 检索词归一为单个短语：输入中的引号视为普通空白，防止破坏短语语法。
        object.__setattr__(
            self, "terms", " ".join(self.terms.replace('"', " ").split())
        )
        normalized_categories: list[str] = []
        for category in self.categories:
            item = category.strip() if isinstance(category, str) else ""
            if not item or not _CATEGORY_PATTERN.match(item):
                raise ArxivQueryError(f"类别标记畸形：{category!r}")
            if item not in normalized_categories:
                normalized_categories.append(item)
        object.__setattr__(self, "categories", tuple(normalized_categories))
        if not self.terms and not self.categories:
            raise ArxivQueryError("检索词与类别至少提供其一")
        if self.sort_by not in _SORT_BY_VALUES:
            raise ArxivQueryError(
                f"排序字段必须是 {_SORT_BY_VALUES} 之一：{self.sort_by!r}"
            )
        if self.sort_order not in _SORT_ORDER_VALUES:
            raise ArxivQueryError(
                f"排序方向必须是 {_SORT_ORDER_VALUES} 之一：{self.sort_order!r}"
            )
        if not isinstance(self.start, int) or self.start < 0:
            raise ArxivQueryError(f"分页起始必须是非负整数：{self.start!r}")
        if (
            not isinstance(self.page_size, int)
            or not 1 <= self.page_size <= MAX_PAGE_SIZE
        ):
            raise ArxivQueryError(
                f"单页条目必须在 1..{MAX_PAGE_SIZE}：{self.page_size!r}"
            )
        # 请求前拒绝超出检索覆盖上限的分页窗口：API 在上限之外无法建
        # 立覆盖，缩小窗口或拆分日期范围是调用方的显式责任。
        if self.start + self.page_size > RETRIEVAL_CEILING:
            raise ArxivQueryError(
                f"分页窗口超出检索上限 {RETRIEVAL_CEILING}："
                f"start={self.start} + page_size={self.page_size}；"
                "请缩小单页条目数或拆分日期范围"
            )

    def search_query(self) -> str:
        parts: list[str] = []
        if self.terms:
            parts.append(f'all:"{self.terms}"')
        parts.append(
            "submittedDate:["
            f"{self.submitted_from:%Y%m%d}0000"
            " TO "
            f"{self.submitted_to:%Y%m%d}2359]"
        )
        if self.categories:
            categories = " OR ".join(f"cat:{item}" for item in self.categories)
            parts.append(f"({categories})")
        return " AND ".join(parts)

    def to_query_params(self) -> dict[str, str]:
        return {
            "search_query": self.search_query(),
            "start": str(self.start),
            "max_results": str(self.page_size),
            "sortBy": self.sort_by,
            "sortOrder": self.sort_order,
        }

    def to_request_url(self, base_url: str = ARXIV_API_BASE_URL) -> str:
        return f"{base_url}?{urllib.parse.urlencode(self.to_query_params())}"


class PaginationAnomaly(Enum):
    """分页或批次结果中的异常信号，暴露给生命周期层判断覆盖情况。"""

    TOTAL_MISSING = "total_missing"
    TOTAL_INVALID = "total_invalid"
    START_INDEX_MISMATCH = "start_index_mismatch"
    EMPTY_PAGE_WITH_RESULTS = "empty_page_with_results"
    TRUNCATED_PAGE = "truncated_page"
    DUPLICATE_ENTRY = "duplicate_entry"
    CEILING_EXCEEDED = "ceiling_exceeded"
    RESULTS_EXCEED_TOTAL = "results_exceed_total"
    RESULTS_EXCEED_PAGE = "results_exceed_page"
    MALFORMED_ENTRY_ID = "malformed_entry_id"
    UNREQUESTED_ENTRY = "unrequested_entry"


@dataclass(frozen=True)
class ArxivPaperMeta:
    """一条论文元数据：无版本身份、具体版本与 API 原始标识（溯源）。

    ``published`` 是首次提交时间、``updated`` 是当前版本修订时间（均
    为 API 返回的 ISO-8601 原文）；``doi``/``journal_ref`` 为可选的期
    刊引用信息，仅作元数据保留，提取依据仍是固定的 arXiv 版本。
    """

    canonical_id: str
    version: int
    api_id_url: str
    title: str
    authors: tuple[str, ...]
    categories: tuple[str, ...]
    primary_category: str | None
    published: str
    updated: str
    abstract: str
    doi: str | None
    journal_ref: str | None


@dataclass(frozen=True)
class ArxivSearchPage:
    """一次检索分页的结果与异常信号。"""

    request: ArxivSearchRequest
    response_url: str
    fetched_at: float
    total_results: int | None
    returned_start_index: int | None
    entries: tuple[ArxivPaperMeta, ...]
    anomalies: tuple[PaginationAnomaly, ...]

    @property
    def ceiling_exceeded(self) -> bool:
        """结果总数是否超出 API 检索上限（此时无法建立完整覆盖）。"""
        return PaginationAnomaly.CEILING_EXCEEDED in self.anomalies

    @property
    def has_more(self) -> bool | None:
        """是否还有未取分页；覆盖无法确立时返回 ``None``。

        总数未知**或存在任何分页异常**时返回 ``None``：异常分页不能
        被解读为“已覆盖完整”，生命周期层必须先处理异常。因此
        ``has_more is False`` 当且仅当总数已知、条目数与声明一致且
        无异常，是唯一的“本页覆盖完整”信号。
        """
        if self.total_results is None or self.anomalies:
            return None
        return self.request.start + len(self.entries) < self.total_results


@dataclass(frozen=True)
class ArxivIdLookup:
    """按标识请求元数据时单个标识的结果状态。

    ``status``：``found``（找到，``meta`` 有值）、``not_found``（服务
    未返回该标识）、``version_mismatch``（返回了其他版本而不是请求
    的具体版本）。
    """

    requested: ArxivIdentifier
    status: str
    meta: ArxivPaperMeta | None = None


@dataclass(frozen=True)
class ArxivIdBatchResult:
    """一次按标识批量查询的结果与异常信号。"""

    response_url: str
    fetched_at: float
    lookups: tuple[ArxivIdLookup, ...]
    anomalies: tuple[PaginationAnomaly, ...]

    def found_metas(self) -> tuple[ArxivPaperMeta, ...]:
        return tuple(
            lookup.meta
            for lookup in self.lookups
            if lookup.status == _LOOKUP_FOUND and lookup.meta is not None
        )


@dataclass(frozen=True)
class ArxivSourceDownload:
    """一次成功取回的固定版本源码包原始字节（未解析）。"""

    identifier: ArxivIdentifier
    final_url: str
    content_type: str | None
    payload: bytes
    byte_count: int
    fetched_at: float


def _parse_atom_root(payload: bytes) -> ET.Element:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ArxivResponseError(f"响应不是合法 XML：{exc}") from exc
    if root.tag != _atom("feed"):
        raise ArxivResponseError(f"响应根元素不是 Atom feed：{root.tag!r}")
    return root


def _required_iso(entry: ET.Element, name: str, entry_id: str) -> str:
    raw = _normalize_space(entry.findtext(_atom(name)))
    if not raw:
        raise ArxivResponseError(f"条目缺少 {name}：{entry_id!r}")
    try:
        datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ArxivResponseError(
            f"条目 {name} 不是 ISO-8601 时间：{raw!r}（{entry_id!r}）"
        ) from exc
    return raw


def _parse_entry(entry: ET.Element) -> ArxivPaperMeta:
    raw_id = (entry.findtext(_atom("id")) or "").strip()
    identifier = parse_entry_id_url(raw_id)
    if identifier.version is None:
        raise ArxivResponseError(f"条目 id 缺少具体版本号：{raw_id!r}")
    authors = tuple(
        name
        for name in (
            _normalize_space(author.findtext(_atom("name")))
            for author in entry.findall(_atom("author"))
        )
        if name
    )
    primary_element = entry.find(_arxiv("primary_category"))
    primary = (
        (primary_element.get("term") or "").strip()
        if primary_element is not None
        else ""
    ) or None
    categories: list[str] = [primary] if primary else []
    for category_element in entry.findall(_atom("category")):
        term = (category_element.get("term") or "").strip()
        if term and term not in categories:
            categories.append(term)
    return ArxivPaperMeta(
        canonical_id=identifier.canonical_id,
        version=identifier.version,
        api_id_url=raw_id,
        title=_normalize_space(entry.findtext(_atom("title"))),
        authors=authors,
        categories=tuple(categories),
        primary_category=primary,
        published=_required_iso(entry, "published", raw_id),
        updated=_required_iso(entry, "updated", raw_id),
        abstract=_normalize_space(entry.findtext(_atom("summary"))),
        doi=_normalize_space(entry.findtext(_arxiv("doi"))) or None,
        journal_ref=_normalize_space(entry.findtext(_arxiv("journal_ref"))) or None,
    )


def _parse_int_text(text: str | None) -> int | None:
    if text is None or not text.strip():
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


def _duplicate_anomalies(metas: Sequence[ArxivPaperMeta]) -> list[PaginationAnomaly]:
    seen: set[tuple[str, int]] = set()
    anomalies: list[PaginationAnomaly] = []
    for meta in metas:
        key = (meta.canonical_id, meta.version)
        if key in seen:
            if PaginationAnomaly.DUPLICATE_ENTRY not in anomalies:
                anomalies.append(PaginationAnomaly.DUPLICATE_ENTRY)
        else:
            seen.add(key)
    return anomalies


def parse_atom_search_page(
    payload: bytes,
    request: ArxivSearchRequest,
    *,
    response_url: str,
    fetched_at: float,
) -> ArxivSearchPage:
    """解析一次检索分页的 Atom 响应；矛盾信号进 ``anomalies``。

    除缺失/非法总数、起始序号不符、空页、截断、重复、超出检索上限
    外，还暴露自相矛盾的分页：负数总数、条目数超过声明总数、条目
    数超过请求页长。存在异常时 :attr:`ArxivSearchPage.has_more` 为
    ``None``，不能宣称覆盖完整；已解析的条目仍保留供部分使用。
    """
    root = _parse_atom_root(payload)
    anomalies: list[PaginationAnomaly] = []

    total_raw = root.findtext(_opensearch("totalResults"))
    total: int | None = None
    if total_raw is None or not total_raw.strip():
        anomalies.append(PaginationAnomaly.TOTAL_MISSING)
    else:
        parsed_total = _parse_int_text(total_raw)
        if parsed_total is None or parsed_total < 0:
            # 负数或非整数总数与覆盖声明自相矛盾，按非法处理。
            anomalies.append(PaginationAnomaly.TOTAL_INVALID)
        else:
            total = parsed_total

    returned_start = _parse_int_text(root.findtext(_opensearch("startIndex")))
    if returned_start != request.start:
        anomalies.append(PaginationAnomaly.START_INDEX_MISMATCH)

    entries = tuple(_parse_entry(element) for element in root.findall(_atom("entry")))
    anomalies.extend(_duplicate_anomalies(entries))

    if total is not None and total > RETRIEVAL_CEILING:
        anomalies.append(PaginationAnomaly.CEILING_EXCEEDED)

    count = len(entries)
    if total is not None:
        if request.start + count > total:
            # 返回条目超出声明总数的覆盖范围（含 total=0 却有条目）。
            anomalies.append(PaginationAnomaly.RESULTS_EXCEED_TOTAL)
        expected = total - request.start
        if expected > 0 and count == 0:
            anomalies.append(PaginationAnomaly.EMPTY_PAGE_WITH_RESULTS)
        if 0 < count < request.page_size and request.start + count < total:
            anomalies.append(PaginationAnomaly.TRUNCATED_PAGE)
    if count > request.page_size:
        # 服务返回了超过请求页长的条目，不能当作可信分页。
        anomalies.append(PaginationAnomaly.RESULTS_EXCEED_PAGE)

    return ArxivSearchPage(
        request=request,
        response_url=response_url,
        fetched_at=fetched_at,
        total_results=total,
        returned_start_index=returned_start,
        entries=entries,
        anomalies=tuple(anomalies),
    )


def parse_atom_id_list(
    payload: bytes,
    requested: Sequence[ArxivIdentifier],
    *,
    response_url: str,
    fetched_at: float,
) -> ArxivIdBatchResult:
    """解析按标识批量查询的 Atom 响应，逐标识给出结果状态。

    服务对不存在或无法解析的标识会返回 “Error” 条目：这类条目记为
    :attr:`PaginationAnomaly.MALFORMED_ENTRY_ID` 并从结果中排除，对应
    请求标识的状态为 ``not_found``；返回了未请求标识时记为
    ``unrequested_entry``。请求了具体版本而返回其他版本时状态为
    ``version_mismatch``，绝不静默替换。
    """
    root = _parse_atom_root(payload)
    anomalies: list[PaginationAnomaly] = []
    parsed: list[ArxivPaperMeta] = []
    for element in root.findall(_atom("entry")):
        try:
            parsed.append(_parse_entry(element))
        except ArxivResponseError:
            if PaginationAnomaly.MALFORMED_ENTRY_ID not in anomalies:
                anomalies.append(PaginationAnomaly.MALFORMED_ENTRY_ID)
    anomalies.extend(_duplicate_anomalies(parsed))

    requested_canonicals = {identifier.canonical_id for identifier in requested}
    if any(meta.canonical_id not in requested_canonicals for meta in parsed):
        anomalies.append(PaginationAnomaly.UNREQUESTED_ENTRY)

    lookups: list[ArxivIdLookup] = []
    for identifier in requested:
        candidates = [
            meta for meta in parsed if meta.canonical_id == identifier.canonical_id
        ]
        if not candidates:
            lookups.append(
                ArxivIdLookup(requested=identifier, status=_LOOKUP_NOT_FOUND, meta=None)
            )
            continue
        exact: ArxivPaperMeta | None = None
        if identifier.version is None:
            exact = max(candidates, key=lambda meta: meta.version)
        else:
            exact = next(
                (meta for meta in candidates if meta.version == identifier.version),
                None,
            )
        if exact is None:
            lookups.append(
                ArxivIdLookup(
                    requested=identifier, status=_LOOKUP_VERSION_MISMATCH, meta=None
                )
            )
        else:
            lookups.append(
                ArxivIdLookup(requested=identifier, status=_LOOKUP_FOUND, meta=exact)
            )

    return ArxivIdBatchResult(
        response_url=response_url,
        fetched_at=fetched_at,
        lookups=tuple(lookups),
        anomalies=tuple(anomalies),
    )


def _media_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    return content_type.split(";", 1)[0].strip().lower() or None


def _require_atom_response(response: TransportResponse) -> None:
    media = _media_type(response.headers.get("content-type"))
    if media in ("text/html", "application/xhtml+xml"):
        raise ArxivResponseError(
            f"API 返回 HTML 而不是 Atom（Content-Type: {media}）：{response.final_url}"
        )
    if not response.body:
        raise ArxivResponseError(f"API 返回空响应体：{response.final_url}")


def _require_request_url_bound(url: str) -> None:
    """请求前检查 URL 长度：工程保守护栏，非官方声明上限。

    超限时拒绝发送并明确报告，由调用方缩小条件，不依赖服务端截断。
    """
    if len(url.encode("utf-8")) > MAX_REQUEST_URL_BYTES:
        raise ArxivQueryError(
            f"请求 URL 长度 {len(url.encode('utf-8'))} 超过工程上限 "
            f"{MAX_REQUEST_URL_BYTES} 字节；请缩小检索条件或拆小批次"
        )


def _verify_source_final_url(final_url: str, identifier: ArxivIdentifier) -> None:
    """验证源码最终 URL 与请求的论文和具体版本一一对应。

    先做与注入传输无关的独立 URL 结构校验：仅 HTTPS、官方主机、缺省
    或 443 端口、无凭据/查询/fragment；再验证路径**精确等于**官方
    ``/e-print/`` 或 ``/src/`` 上请求的版本化标识（路径可经百分号解
    码）。无版本、不同版本、额外路径、非标准协议/端口/成分或非官方
    主机一律拒绝交付——无法确认对应关系时不做猜测。
    """
    try:
        split = urllib.parse.urlsplit(final_url)
        port = split.port
    except ValueError as exc:
        raise ArxivSourceError(
            f"源码最终 URL 无法解析：{final_url!r}（请求 {identifier.versioned_id!r}）"
        ) from exc
    if split.scheme.lower() != "https":
        raise ArxivSourceError(
            f"源码最终 URL 不是 HTTPS（{split.scheme!r}）：{final_url!r}"
            f"（请求 {identifier.versioned_id!r}）"
        )
    host = (split.hostname or "").lower()
    if host not in _ABS_HOSTS:
        raise ArxivSourceError(
            f"源码最终 URL 主机不可信：{final_url!r}（请求 {identifier.versioned_id!r}）"
        )
    if split.username or split.password:
        raise ArxivSourceError(
            f"源码最终 URL 带有凭据：{final_url!r}（请求 {identifier.versioned_id!r}）"
        )
    if port is not None and port != 443:
        raise ArxivSourceError(
            f"源码最终 URL 端口非标准（{port}，仅允许缺省或 443）：{final_url!r}"
            f"（请求 {identifier.versioned_id!r}）"
        )
    if split.query or split.fragment:
        raise ArxivSourceError(
            f"源码最终 URL 带有查询或 fragment：{final_url!r}"
            f"（请求 {identifier.versioned_id!r}）"
        )
    path = urllib.parse.unquote(split.path)
    expected = identifier.versioned_id
    if path not in (f"/e-print/{expected}", f"/src/{expected}"):
        raise ArxivSourceError(
            f"源码最终 URL 与请求的论文/版本不对应：{final_url!r}，"
            f"期望 /e-print/{expected} 或 /src/{expected}；拒绝交付字节"
        )


class ArxivApiService:
    """arXiv API 与源码下载的协议入口；所有请求经统一传输客户端。

    参数：
        client: :class:`ArxivHttpClient`（含节流门与重试）；缺省为生
            产默认客户端。
        api_base_url: API 查询入口（默认官方地址）。
        clock: 提供 ``fetched_at`` 时间戳的时钟，默认系统时钟。

    该服务只做协议与传输，不保存任何状态；持久化、进度与恢复由生命
    周期层负责。
    """

    def __init__(
        self,
        client: ArxivHttpClient | None = None,
        *,
        api_base_url: str = ARXIV_API_BASE_URL,
        clock=None,
    ) -> None:
        self._client = client if client is not None else ArxivHttpClient()
        self._api_base_url = api_base_url
        self._clock = clock if clock is not None else SystemClock()

    @property
    def client(self) -> ArxivHttpClient:
        return self._client

    def search(self, request: ArxivSearchRequest) -> ArxivSearchPage:
        """执行一次显式受限的检索分页请求。"""
        url = request.to_request_url(self._api_base_url)
        _require_request_url_bound(url)
        response = self._client.get(
            url, timeout=API_TIMEOUT, max_bytes=API_MAX_RESPONSE_BYTES
        )
        _require_atom_response(response)
        return parse_atom_search_page(
            response.body,
            request,
            response_url=response.final_url,
            fetched_at=self._clock.now(),
        )

    def metadata_by_ids(
        self, identifiers: Sequence[ArxivIdentifier | str]
    ) -> ArxivIdBatchResult:
        """按显式标识（可含版本）批量查询元数据。

        畸形标识在发出任何请求之前即被拒绝；单批标识数也受限
        于 :data:`MAX_ID_LIST_LENGTH`（工程保守护栏），超出时拒绝发送
        并由调用方拆批，不依赖服务端截断。请求顺序保留在结果中，
        每个标识单独给出 ``found``/``not_found``/``version_mismatch``。
        """
        parsed = tuple(
            parse_arxiv_identifier(identifier)
            if isinstance(identifier, str)
            else identifier
            for identifier in identifiers
        )
        if not parsed:
            raise ArxivQueryError("至少提供一个论文标识")
        if len(parsed) > MAX_ID_LIST_LENGTH:
            raise ArxivQueryError(
                f"单批标识数 {len(parsed)} 超过工程上限 {MAX_ID_LIST_LENGTH}；"
                "请拆小批次后分批查询，不依赖服务端截断"
            )
        params = {
            "id_list": ",".join(identifier.versioned_id for identifier in parsed),
            "max_results": str(len(parsed)),
        }
        url = f"{self._api_base_url}?{urllib.parse.urlencode(params)}"
        _require_request_url_bound(url)
        response = self._client.get(
            url, timeout=API_TIMEOUT, max_bytes=API_MAX_RESPONSE_BYTES
        )
        _require_atom_response(response)
        return parse_atom_id_list(
            response.body,
            parsed,
            response_url=response.final_url,
            fetched_at=self._clock.now(),
        )

    def download_source(self, identifier: ArxivIdentifier | str) -> ArxivSourceDownload:
        """下载固定具体版本的源码包原始字节；不做任何归档解析。

        版本号必须已经冻结（不带版本号的标识会被拒绝：先查元数据确
        定版本再下载）。返回前严格校验最终 URL 与请求的论文和版本一
        一对应（仅接受官方 ``/e-print/`` 或 ``/src/`` 路径上的精确版
        本标识），无法确认对应关系时拒绝交付字节，不做猜测。HTML
        错误页、PDF-only 与 404 统一以
        :class:`ArxivSourceUnavailableError` 区分上抛；空响应体视为失
        败。``payload`` 是未经解析的原始字节，归档安全检查由后续组件
        负责。
        """
        ident = (
            parse_arxiv_identifier(identifier)
            if isinstance(identifier, str)
            else identifier
        )
        if ident.version is None:
            raise ArxivIdentifierError(
                f"下载源码包必须固定具体版本号（先查元数据再冻结版本）：{ident.canonical_id!r}"
            )
        url = ident.eprint_url()
        _require_request_url_bound(url)
        try:
            response = self._client.get(
                url,
                headers=SOURCE_ACCEPT_HEADERS,
                timeout=SOURCE_TIMEOUT,
                max_bytes=SOURCE_MAX_RESPONSE_BYTES,
            )
        except ArxivHttpError as exc:
            if exc.status_code == 404:
                raise ArxivSourceUnavailableError(
                    f"该版本没有可获取的源码包：{ident.versioned_id}",
                    reason="not_found",
                ) from exc
            raise
        _verify_source_final_url(response.final_url, ident)
        media = _media_type(response.headers.get("content-type"))
        if media in ("text/html", "application/xhtml+xml"):
            raise ArxivSourceUnavailableError(
                f"源码下载返回 HTML 错误页：{ident.versioned_id}", reason="html_error"
            )
        if media == "application/pdf":
            raise ArxivSourceUnavailableError(
                f"该版本只有 PDF，没有 LaTeX 源码包：{ident.versioned_id}",
                reason="pdf_only",
            )
        if not response.body:
            raise ArxivSourceError(
                f"源码下载返回空内容，不能当作成功：{ident.versioned_id}"
            )
        return ArxivSourceDownload(
            identifier=ident,
            final_url=response.final_url,
            content_type=media,
            payload=response.body,
            byte_count=len(response.body),
            fetched_at=self._clock.now(),
        )


__all__ = [
    "API_MAX_RESPONSE_BYTES",
    "API_TIMEOUT",
    "ARXIV_API_BASE_URL",
    "ARXIV_EPRINT_BASE_URL",
    "MAX_ID_LIST_LENGTH",
    "MAX_PAGE_SIZE",
    "MAX_REQUEST_URL_BYTES",
    "RETRIEVAL_CEILING",
    "SOURCE_MAX_RESPONSE_BYTES",
    "SOURCE_TIMEOUT",
    "ArxivApiService",
    "ArxivIdBatchResult",
    "ArxivIdLookup",
    "ArxivIdentifier",
    "ArxivIdentifierError",
    "ArxivPaperMeta",
    "ArxivProtocolError",
    "ArxivQueryError",
    "ArxivResponseError",
    "ArxivSearchPage",
    "ArxivSearchRequest",
    "ArxivSourceDownload",
    "ArxivSourceError",
    "ArxivSourceUnavailableError",
    "PaginationAnomaly",
    "parse_arxiv_identifier",
    "parse_atom_id_list",
    "parse_atom_search_page",
    "parse_entry_id_url",
]
