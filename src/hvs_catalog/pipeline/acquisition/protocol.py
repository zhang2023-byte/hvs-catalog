"""arXiv protocol: identifiers, query construction, Atom parsing, and source-download entry
points.

Parsing/building functions have no network/disk effects. ArxivApiService composes
transport and exposes lifecycle results/classified errors. Empty valid Atom results
differ from failed requests, which raise. Expose missing/invalid totals, wrong start
offsets, unexpectedly empty/truncated/duplicate pages, ceiling excess, and unrequested
batch entries as PaginationAnomaly without hiding them; lifecycle determines coverage.

Never invent paper IDs; reject malformed/missing-version/mismatched returns. Support
modern 2101.01234v2 and legacy astro-ph/0507001v3, retaining unversioned identity,
version, and original API URL provenance. Queries require initial-submission UTC dates
and stable sorting. Validate pagination windows, batch counts, and URL lengths before
sending, with reported engineering limits rather than server truncation.

Freeze download versions and strictly verify exact official /e-print/ or /src/ final URL
correspondence before delivering bytes. Classify HTML/PDF-only as unavailable; later
components inspect archives. Atom http://arxiv.org/abs identifiers are historical
identity strings, structurally validated here; outbound HTTPS/port/credential rules
apply in transport, not historical identifiers.
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

#: Official API query and e-print source-download endpoints.
ARXIV_API_BASE_URL = "https://export.arxiv.org/api/query"
ARXIV_EPRINT_BASE_URL = "https://arxiv.org/e-print/"

#: API page-size/retrieval ceilings; coverage beyond the total ceiling cannot be established.
MAX_PAGE_SIZE = 2000
RETRIEVAL_CEILING = 30000

#: Identifier batch-count and request-URL length limits.
#: Both are conservative engineering limits, not official capability ceilings; reject excessive requests
#: and report bounds so callers can split batches/narrow scope, never relying on server truncation.
MAX_ID_LIST_LENGTH = 200
MAX_REQUEST_URL_BYTES = 8192

#: Production size/time bounds for API and source-package responses.
API_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
SOURCE_MAX_RESPONSE_BYTES = 256 * 1024 * 1024
API_TIMEOUT = 60.0
SOURCE_TIMEOUT = 180.0

#: Explicit source-download content negotiation.
#:
#: Declare representations that can be accepted/classified without guaranteeing server acceptance.
#: tar/gzip packages are deliverable; accept PDF-only/HTML responses to classify unsupported/unavailable
#: contents. */* prevents negotiation from hiding error pages before classification.
#: Request identity transfer encoding because local reads do not decompress transport encodings,
#: avoiding confusion with original bytes. Never spoof browsers or change endpoints.
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
    """Base protocol error."""


class ArxivIdentifierError(ArxivProtocolError, ValueError):
    """Malformed, unversioned-required, or mismatched arXiv identifier."""


class ArxivQueryError(ArxivProtocolError, ValueError):
    """Search request violates explicit scope/value constraints."""


class ArxivResponseError(ArxivProtocolError):
    """Server content is not parseable Atom, including HTML errors."""


class ArxivSourceError(ArxivProtocolError):
    """Invalid source-download contents, such as empty responses, cannot count as success."""


class ArxivSourceUnavailableError(ArxivSourceError):
    """No usable LaTeX source package for this version, with reason.

    Reasons: not_found for absent source, html_error for returned HTML error pages, and
    pdf_only for PDF-only submissions.
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
    """Parsed arXiv identity with optional exact version.

    canonical_id is the shared unversioned modern/legacy paper identity. Fix version
    before source download.
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
    """Strictly parse arXiv identifiers; malformed inputs raise ArxivIdentifierError.

    Accept modern and legacy forms with/without versions, including 2101.01234,
    astro-ph/0507001, and math.AG/0309136. Reject internal whitespace, invalid versions
    such as v0, invalid months, and other forms without guessing.
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
    """Parse Atom id URLs with strict identity structure.

    Accept historical http/https official-host /abs/exact-ID forms with corresponding
    default/80/443 ports. Reject other schemes, credentials, nonstandard ports, extra
    paths, queries, fragments, and whitespace. These are identifiers, not outbound
    requests, so historical HTTP remains valid while transport enforces outbound HTTPS.
    Validate independently of injected transport/client network checks.
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
    """Explicit bounded search: initial-submission UTC date range, terms, categories, and
    pagination.

    Dates include both submitted_from/submitted_to days. Fix submittedDate (default) or
    lastUpdatedDate sorting and explicit direction for traceable/resumable pagination.
    Require terms or categories.
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
        # Normalize terms into one phrase; treat input quotes as whitespace to preserve phrase syntax.
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
        # Reject pagination beyond retrieval coverage before sending; callers must narrow pages
        # or split date ranges explicitly because the API cannot establish coverage beyond the ceiling.
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
    """Pagination/batch anomalies exposed to lifecycle coverage decisions."""

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
    """Paper metadata: unversioned identity, exact version, and original API identifier
    provenance.

    published/updated preserve initial/current-revision ISO-8601 timestamps from the
    API. Optional doi/journal_ref remain metadata only; extraction still uses fixed
    arXiv versions.
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
    """Search-page results and anomalies."""

    request: ArxivSearchRequest
    response_url: str
    fetched_at: float
    total_results: int | None
    returned_start_index: int | None
    entries: tuple[ArxivPaperMeta, ...]
    anomalies: tuple[PaginationAnomaly, ...]

    @property
    def ceiling_exceeded(self) -> bool:
        """Whether total results exceed the API retrieval ceiling, preventing complete
        coverage.
        """
        return PaginationAnomaly.CEILING_EXCEEDED in self.anomalies

    @property
    def has_more(self) -> bool | None:
        """Whether pages remain, or None if coverage cannot be established.

        Unknown totals or any pagination anomaly return None: anomalous pages never
        prove complete coverage. Lifecycle must handle them first. has_more=False is the
        sole complete-page signal, requiring known totals, consistent entry counts, and
        no anomalies.
        """
        if self.total_results is None or self.anomalies:
            return None
        return self.request.start + len(self.entries) < self.total_results


@dataclass(frozen=True)
class ArxivIdLookup:
    """Per-identifier metadata lookup result.

    found includes meta; not_found means absent; version_mismatch means another version
    was returned instead of the requested exact version.
    """

    requested: ArxivIdentifier
    status: str
    meta: ArxivPaperMeta | None = None


@dataclass(frozen=True)
class ArxivIdBatchResult:
    """Identifier-batch results and anomalies."""

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
    """Successfully retrieved fixed-version source-package bytes, unparsed."""

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
    """Parse Atom search pages and expose contradictions through anomalies.

    Include missing/invalid totals, wrong offsets, empty/truncated/duplicate pages,
    retrieval ceiling excess, negative totals, counts above totals, and counts above
    requested page sizes. Anomalies make has_more=None, never complete coverage; retain
    parsed entries for partial use.
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
            # Negative/noninteger totals contradict coverage assertions and are invalid.
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
            # Returned entries exceed declared totals, including nonempty results with total=0.
            anomalies.append(PaginationAnomaly.RESULTS_EXCEED_TOTAL)
        expected = total - request.start
        if expected > 0 and count == 0:
            anomalies.append(PaginationAnomaly.EMPTY_PAGE_WITH_RESULTS)
        if 0 < count < request.page_size and request.start + count < total:
            anomalies.append(PaginationAnomaly.TRUNCATED_PAGE)
    if count > request.page_size:
        # More entries than the requested page size cannot establish trustworthy pagination.
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
    """Parse identifier-batch Atom responses with per-identifier status.

    Error entries for absent/unparseable IDs become MALFORMED_ENTRY_ID anomalies and are
    excluded; corresponding requests get not_found. Unrequested returns become
    unrequested_entry anomalies. Different versions yield version_mismatch without
    silent substitution.
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
    """Check request-URL length before sending using a conservative engineering limit, not
    an official ceiling.

    Reject excess explicitly so callers narrow conditions; never rely on server
    truncation.
    """
    if len(url.encode("utf-8")) > MAX_REQUEST_URL_BYTES:
        raise ArxivQueryError(
            f"请求 URL 长度 {len(url.encode('utf-8'))} 超过工程上限 "
            f"{MAX_REQUEST_URL_BYTES} 字节；请缩小检索条件或拆小批次"
        )


def _verify_source_final_url(final_url: str, identifier: ArxivIdentifier) -> None:
    """Verify exact final-source URL correspondence to the requested paper/version.

    Independently validate HTTPS, official host, default/443 port, and absent
    credentials/query/fragment, then require percent-decoded paths exactly matching
    official /e-print/ or /src/ versioned identifiers. Reject absent/different versions,
    extra paths/components, nonstandard schemes/ports, or unofficial hosts rather than
    guessing.
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
    """arXiv API/source protocol service; every request uses the shared transport client.

    client is ArxivHttpClient with throttling/retries, defaulting to production.
    api_base_url defaults to the official API. clock supplies fetched_at timestamps,
    defaulting to the system clock. This service handles protocol/transport only;
    lifecycle owns persistence, progress, and recovery.
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
        """Execute one explicitly bounded search-page request."""
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
        """Query metadata for explicit identifiers with optional versions.

        Reject malformed IDs before any request and batches above MAX_ID_LIST_LENGTH so
        callers split them, without server truncation. Preserve request order and return
        found/not_found/version_mismatch per identifier.
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

    def download_pdf(self, identifier: ArxivIdentifier | str) -> ArxivSourceDownload:
        """Download only an explicitly frozen arXiv PDF version."""
        from urllib.parse import unquote, urlsplit

        from ...records.pdf import pdf_page_count

        ident = (
            parse_arxiv_identifier(identifier)
            if isinstance(identifier, str)
            else identifier
        )
        if ident.version is None:
            raise ArxivIdentifierError("PDF download requires a fixed version")
        url = "https://arxiv.org/pdf/" + ident.versioned_id
        response = self._client.get(
            url,
            headers={"Accept": "application/pdf"},
            timeout=SOURCE_TIMEOUT,
            max_bytes=SOURCE_MAX_RESPONSE_BYTES,
        )
        final = urlsplit(response.final_url)
        path = unquote(final.path).removesuffix(".pdf")
        if (
            final.scheme != "https"
            or final.hostname not in {"arxiv.org", "export.arxiv.org"}
            or final.port not in (None, 443)
            or final.username
            or final.password
            or final.query
            or final.fragment
            or path != "/pdf/" + ident.versioned_id
        ):
            raise ArxivSourceError(
                "PDF response does not match the fixed arXiv version"
            )
        try:
            pdf_page_count(response.body)
        except ValueError as exc:
            raise ArxivSourceError(str(exc)) from exc
        return ArxivSourceDownload(
            identifier=ident,
            final_url=response.final_url,
            content_type="application/pdf",
            payload=response.body,
            byte_count=len(response.body),
            fetched_at=self._clock.now(),
        )

    def download_source(self, identifier: ArxivIdentifier | str) -> ArxivSourceDownload:
        """Download fixed-version source bytes without archive parsing.

        Require already frozen versions; unversioned callers must query metadata first.
        Strictly verify exact official /e-print/ or /src/ final URL correspondence
        before returning, refusing uncertainty. Classify HTML errors, PDF-only, and 404
        as ArxivSourceUnavailableError; empty bodies fail. payload retains raw unparsed
        bytes for later archive safety checks.
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
