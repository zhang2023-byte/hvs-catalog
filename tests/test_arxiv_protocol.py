"""Synthetic arXiv protocol tests: identifiers, queries, pagination parsing, and service
entry points (lane A).

All tests are offline, using fake transport and clocks with synthetic Atom/HTML fixtures
and no real paper data.
"""

from __future__ import annotations

import urllib.parse
from datetime import date
from pathlib import Path

import pytest

from hvs_catalog.pipeline.acquisition.protocol import (
    API_MAX_RESPONSE_BYTES,
    API_TIMEOUT,
    MAX_ID_LIST_LENGTH,
    MAX_PAGE_SIZE,
    MAX_REQUEST_URL_BYTES,
    RETRIEVAL_CEILING,
    SOURCE_MAX_RESPONSE_BYTES,
    SOURCE_TIMEOUT,
    ArxivApiService,
    ArxivIdentifierError,
    ArxivQueryError,
    ArxivResponseError,
    ArxivSearchRequest,
    ArxivSourceError,
    ArxivSourceUnavailableError,
    PaginationAnomaly,
    parse_arxiv_identifier,
    parse_atom_id_list,
    parse_atom_search_page,
    parse_entry_id_url,
)
from hvs_catalog.pipeline.acquisition.rate_limit import RateLimitGate
from hvs_catalog.pipeline.acquisition.transport import (
    ArxivHttpClient,
    ArxivHttpError,
    ArxivRateLimitedError,
    ArxivTimeoutError,
    TransportResponse,
)

FIXTURES = Path(__file__).parent / "fixtures" / "arxiv_protocol"
REQUEST = ArxivSearchRequest(
    date(2021, 1, 1), date(2021, 12, 31), categories=("astro-ph.GA",)
)
API_URL = "https://export.arxiv.org/api/query?x=1"


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self._now = float(start)
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self._now += max(0.0, float(seconds))


class FakeTransport:
    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def fetch(self, url, *, headers=None, timeout=None, max_bytes=None):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers) if headers else None,
                "timeout": timeout,
                "max_bytes": max_bytes,
            }
        )
        assert self.outcomes, "意外的额外请求：传输被调用了超出脚本的次数"
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def atom_response(name: str, url: str = API_URL) -> TransportResponse:
    return TransportResponse(
        status_code=200,
        headers={"content-type": "application/atom+xml"},
        body=(FIXTURES / name).read_bytes(),
        request_url=url,
        final_url=url,
    )


def make_service(
    transport: FakeTransport, clock: FakeClock, tmp_path: Path
) -> ArxivApiService:
    client = ArxivHttpClient(
        transport=transport,
        gate=RateLimitGate(tmp_path / "gate.json", clock=clock),
        clock=clock,
    )
    return ArxivApiService(client, clock=clock)


# ---------------------------------------------------------------- Identifier parsing


@pytest.mark.parametrize(
    "text,canonical,version,legacy",
    [
        ("2101.01234", "2101.01234", None, False),
        ("2101.01234v2", "2101.01234", 2, False),
        ("2412.12345", "2412.12345", None, False),
        ("0704.0001v1", "0704.0001", 1, False),
        ("astro-ph/9101001", "astro-ph/9101001", None, True),
        ("astro-ph/0507001v3", "astro-ph/0507001", 3, True),
        ("math.AG/0309136", "math.AG/0309136", None, True),
        ("hep-th/9901001v1", "hep-th/9901001", 1, True),
        ("cond-mat/0703470", "cond-mat/0703470", None, True),
    ],
)
def test_parse_valid_identifiers(text, canonical, version, legacy):
    identifier = parse_arxiv_identifier(text)
    assert identifier.canonical_id == canonical
    assert identifier.version == version
    assert identifier.is_legacy is legacy
    if version is None:
        assert identifier.versioned_id == canonical
    else:
        assert identifier.versioned_id == f"{canonical}v{version}"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "2101.01234 ",
        " 2101.01234",
        "2101.0123456",
        "2101.01234v",
        "2101.01234v0",
        "2101.01234v02x",
        "2113.01234",
        "2100.01234",
        "astro-ph/9113001",
        "astro-ph/507001",
        "Astro-ph/0507001",
        "astro-ph/0507001v",
        "abs/2101.01234",
        "https://arxiv.org/abs/2101.01234",
        "2101.01234/../x",
        "2101.01234?v=2",
        "2101.01234%20",
        "astro_ph/0507001",
        123,
    ],
)
def test_malformed_identifiers_are_refused(text):
    with pytest.raises(ArxivIdentifierError):
        parse_arxiv_identifier(text)


def test_identifier_equality_and_urls():
    assert parse_arxiv_identifier("2101.01234v2") == parse_arxiv_identifier(
        "2101.01234v2"
    )
    assert parse_arxiv_identifier("2101.01234v2") != parse_arxiv_identifier(
        "2101.01234v3"
    )
    assert parse_arxiv_identifier("2101.01234v2") != parse_arxiv_identifier(
        "2101.01234"
    )
    legacy = parse_arxiv_identifier("astro-ph/0507001v3")
    assert legacy.abs_url() == "http://arxiv.org/abs/astro-ph/0507001v3"
    assert legacy.eprint_url() == "https://arxiv.org/e-print/astro-ph/0507001v3"


def test_parse_entry_id_url_accepts_arxiv_hosts():
    modern = parse_entry_id_url("http://arxiv.org/abs/2101.01234v2")
    assert modern.canonical_id == "2101.01234" and modern.version == 2
    legacy = parse_entry_id_url("https://export.arxiv.org/abs/astro-ph/0507001v3")
    assert legacy.canonical_id == "astro-ph/0507001" and legacy.version == 3


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/abs/2101.01234v1",
        "http://arxiv.org/pdf/2101.01234v1",
        "http://arxiv.org/attacker/abs/2101.01234v1",
        "http://arxiv.org/abs/../abs/2101.01234v1",
        "http://arxiv.org/abs/2101.01234v1?injected=1",
        "http://arxiv.org/abs/2101.01234v1#frag",
        "http://arxiv.org/abs/",
        "http://arxiv.org/abs/2101.01234v1/extra",
        "not-a-url",
        # Protocols, ports, credentials, and whitespace; review reproduced acceptance of FTP and nonstandard ports.
        "ftp://arxiv.org/abs/2101.01234v1",
        "gopher://export.arxiv.org/abs/2101.01234v1",
        "http://user:pass@arxiv.org/abs/2101.01234v1",
        "https://arxiv.org:8080/abs/2101.01234v1",
        "http://arxiv.org:443/abs/2101.01234v1",
        "https://arxiv.org:80/abs/2101.01234v1",
        "https://arxiv.org:99999/abs/2101.01234v1",
        " https://arxiv.org/abs/2101.01234v1",
        "https://arxiv.org/abs/2101.0123 4v1",
    ],
)
def test_parse_entry_id_url_rejects_foreign_or_malformed(url):
    with pytest.raises(ArxivResponseError):
        parse_entry_id_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://arxiv.org:80/abs/2101.01234v2",  # HTTP uses standard port 80.
        "https://arxiv.org:443/abs/2101.01234v2",  # HTTPS uses standard port 443.
    ],
)
def test_parse_entry_id_url_accepts_scheme_standard_ports(url):
    identifier = parse_entry_id_url(url)
    assert identifier.canonical_id == "2101.01234"
    assert identifier.version == 2


def test_parse_entry_id_url_accepts_historical_http_identifier():
    """An Atom entry's historical http:// identifier is not an outbound request."""
    identifier = parse_entry_id_url("http://arxiv.org/abs/2101.01234v2")
    assert identifier.canonical_id == "2101.01234"
    assert identifier.version == 2


# ---------------------------------------------------------------- Query construction


def test_query_requires_explicit_dates_terms_or_categories():
    with pytest.raises(ArxivQueryError):
        ArxivSearchRequest(date(2026, 1, 1), date(2026, 1, 31))
    with pytest.raises(ArxivQueryError):
        ArxivSearchRequest(date(2026, 2, 1), date(2026, 1, 1), terms="star")
    with pytest.raises(ArxivQueryError):
        ArxivSearchRequest("2026-01-01", date(2026, 1, 31), terms="star")


def test_query_builds_explicit_range_categories_and_sort():
    request = ArxivSearchRequest(
        date(2026, 1, 1),
        date(2026, 1, 31),
        terms='hypervelocity "star"',
        categories=("astro-ph.GA", "astro-ph.GA", "astro-ph.SR"),
        sort_order="descending",
    )
    params = request.to_query_params()
    assert params["search_query"] == (
        'all:"hypervelocity star" AND '
        "submittedDate:[202601010000 TO 202601312359] AND "
        "(cat:astro-ph.GA OR cat:astro-ph.SR)"
    )
    assert params["sortBy"] == "submittedDate"
    assert params["sortOrder"] == "descending"
    assert params["start"] == "0"
    assert params["max_results"] == "100"
    url = request.to_request_url()
    assert url.startswith("https://export.arxiv.org/api/query?")
    decoded = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert decoded["search_query"] == params["search_query"]


def test_category_only_query_is_allowed():
    request = ArxivSearchRequest(
        date(2026, 3, 1), date(2026, 3, 31), categories=("astro-ph.GA",)
    )
    assert request.search_query() == (
        "submittedDate:[202603010000 TO 202603312359] AND (cat:astro-ph.GA)"
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"terms": "x", "page_size": 0},
        {"terms": "x", "page_size": MAX_PAGE_SIZE + 1},
        {"terms": "x", "start": -1},
        {"terms": "x", "sort_by": "relevance"},
        {"terms": "x", "sort_order": "random"},
        {"terms": "x", "categories": ("astro ph.GA",)},
        {"terms": "x", "categories": ("cat:astro-ph.GA",)},
    ],
)
def test_query_validation_rejects_unsafe_values(kwargs):
    with pytest.raises(ArxivQueryError):
        ArxivSearchRequest(date(2026, 1, 1), date(2026, 1, 31), **kwargs)


def test_query_allows_full_page_and_alternate_sort():
    request = ArxivSearchRequest(
        date(2026, 1, 1),
        date(2026, 1, 31),
        terms="star",
        sort_by="lastUpdatedDate",
        start=2000,
        page_size=MAX_PAGE_SIZE,
    )
    assert request.to_query_params()["max_results"] == str(MAX_PAGE_SIZE)


def test_query_window_beyond_retrieval_ceiling_is_rejected_before_request():
    """Reject pagination windows exceeding the retrieval ceiling during construction,
    before any request.
    """
    with pytest.raises(ArxivQueryError, match="检索上限"):
        ArxivSearchRequest(
            date(2026, 1, 1), date(2026, 1, 31), terms="star", start=30000
        )
    with pytest.raises(ArxivQueryError, match="检索上限"):
        ArxivSearchRequest(
            date(2026, 1, 1),
            date(2026, 1, 31),
            terms="star",
            start=29999,
            page_size=100,  # 29999 + 100 > 30000
        )
    # A window exactly at the ceiling remains valid: start + page_size == 30000.
    edge = ArxivSearchRequest(
        date(2026, 1, 1), date(2026, 1, 31), terms="star", start=29900, page_size=100
    )
    assert edge.to_query_params()["start"] == "29900"


# ---------------------------------------------------------------- Pagination parsing


def parse_page(name: str, request: ArxivSearchRequest = REQUEST):
    return parse_atom_search_page(
        (FIXTURES / name).read_bytes(),
        request,
        response_url=API_URL,
        fetched_at=123.0,
    )


def test_search_page_parses_modern_and_legacy_entries():
    page = parse_page("search_page.xml")
    assert page.total_results == 2
    assert page.returned_start_index == 0
    assert page.anomalies == ()
    first, legacy = page.entries
    assert first.canonical_id == "2101.01234" and first.version == 2
    assert first.api_id_url == "http://arxiv.org/abs/2101.01234v2"
    assert first.title == "Synthetic Hypervelocity Candidate Study"
    assert first.abstract == "A synthetic abstract used only for offline tests."
    assert first.authors == ("Alice Synth", "Bob Example")
    assert first.categories == ("astro-ph.GA", "astro-ph.IM")
    assert first.primary_category == "astro-ph.GA"
    assert first.published == "2021-01-08T09:00:00Z"
    assert first.updated == "2021-01-15T10:30:00Z"
    assert first.doi == "10.9999/synth.2101.01234"
    assert first.journal_ref == "Synth. J. 99, 1 (2021)"
    assert legacy.canonical_id == "astro-ph/0507001" and legacy.version == 3
    assert legacy.doi is None and legacy.journal_ref is None
    assert page.has_more is False


def test_empty_page_is_a_valid_empty_result_not_an_error():
    page = parse_page("empty_page.xml")
    assert page.total_results == 0
    assert page.entries == ()
    assert page.anomalies == ()
    assert page.has_more is False


def test_ceiling_exceeded_is_exposed_not_flattened():
    page = parse_page("ceiling_page.xml")
    assert page.total_results == RETRIEVAL_CEILING + 1
    assert page.ceiling_exceeded is True
    assert PaginationAnomaly.CEILING_EXCEEDED in page.anomalies


def test_invalid_total_is_exposed():
    page = parse_page("bad_total.xml")
    assert page.total_results is None
    assert PaginationAnomaly.TOTAL_INVALID in page.anomalies
    assert page.has_more is None


def test_missing_total_is_exposed():
    page = parse_page("missing_total.xml")
    assert page.total_results is None
    assert PaginationAnomaly.TOTAL_MISSING in page.anomalies
    assert page.has_more is None


def test_start_index_mismatch_is_exposed():
    page = parse_page("bad_start_index.xml")
    assert page.returned_start_index == 7
    assert PaginationAnomaly.START_INDEX_MISMATCH in page.anomalies


def test_truncated_page_is_exposed_and_never_complete():
    page = parse_page("truncated_page.xml")
    assert len(page.entries) == 2
    assert page.total_results == 5
    assert PaginationAnomaly.TRUNCATED_PAGE in page.anomalies
    # Anomalies prevent a coverage conclusion: neither complete coverage nor definite continuation is established.
    assert page.has_more is None


def test_duplicate_entries_are_exposed_and_block_completeness():
    page = parse_page("duplicate_page.xml")
    assert PaginationAnomaly.DUPLICATE_ENTRY in page.anomalies
    assert len(page.entries) == 2
    assert page.has_more is None


def test_negative_total_is_invalid_and_blocks_completeness():
    page = parse_page("negative_total.xml")
    assert page.total_results is None
    assert PaginationAnomaly.TOTAL_INVALID in page.anomalies
    assert page.has_more is None
    assert len(page.entries) == 1  # Retain usable partial entries.


def test_entries_exceeding_declared_total_are_flagged():
    page = parse_page("exceed_total.xml")
    assert page.total_results == 0
    assert len(page.entries) == 1
    assert PaginationAnomaly.RESULTS_EXCEED_TOTAL in page.anomalies
    assert page.has_more is None


def test_entries_exceeding_requested_page_size_are_flagged():
    request = ArxivSearchRequest(
        date(2021, 1, 1),
        date(2021, 12, 31),
        categories=("astro-ph.GA",),
        page_size=2,
    )
    page = parse_atom_search_page(
        (FIXTURES / "overflow_page.xml").read_bytes(),
        request,
        response_url=API_URL,
        fetched_at=1.0,
    )
    assert len(page.entries) == 3
    assert PaginationAnomaly.RESULTS_EXCEED_PAGE in page.anomalies
    assert page.has_more is None


def test_broken_xml_raises_response_error():
    with pytest.raises(ArxivResponseError):
        parse_page("bad_xml.xml")


def test_non_atom_root_raises_response_error():
    with pytest.raises(ArxivResponseError):
        parse_atom_search_page(
            b"<root><child/></root>", REQUEST, response_url=API_URL, fetched_at=1.0
        )


def test_entry_without_version_is_refused():
    payload = (FIXTURES / "search_page.xml").read_bytes()
    payload = payload.replace(b"/2101.01234v2", b"/2101.01234")
    with pytest.raises(ArxivResponseError, match="具体版本"):
        parse_atom_search_page(payload, REQUEST, response_url=API_URL, fetched_at=1.0)


# ---------------------------------------------------------------- Batch parsing


def test_batch_result_identity_is_preserved():
    requested = [
        parse_arxiv_identifier("2101.01234"),
        parse_arxiv_identifier("2101.99999"),
    ]
    result = parse_atom_id_list(
        (FIXTURES / "batch_page.xml").read_bytes(),
        requested,
        response_url=API_URL,
        fetched_at=1.0,
    )
    first, second = result.lookups
    assert first.status == "found"
    assert first.meta is not None and first.meta.version == 3
    assert first.meta.canonical_id == "2101.01234"
    assert second.status == "not_found" and second.meta is None
    assert PaginationAnomaly.MALFORMED_ENTRY_ID in result.anomalies
    assert result.found_metas() == (first.meta,)


def test_batch_version_mismatch_is_not_silently_substituted():
    requested = [parse_arxiv_identifier("2101.01234v2")]
    result = parse_atom_id_list(
        (FIXTURES / "batch_page.xml").read_bytes(),
        requested,
        response_url=API_URL,
        fetched_at=1.0,
    )
    (lookup,) = result.lookups
    assert lookup.status == "version_mismatch"
    assert lookup.meta is None


def test_batch_unrequested_entry_is_exposed():
    requested = [parse_arxiv_identifier("2101.01234")]
    result = parse_atom_id_list(
        (FIXTURES / "search_page.xml").read_bytes(),
        requested,
        response_url=API_URL,
        fetched_at=1.0,
    )
    assert PaginationAnomaly.UNREQUESTED_ENTRY in result.anomalies
    assert result.lookups[0].status == "found"


def test_batch_legacy_identifier_round_trip():
    requested = [parse_arxiv_identifier("astro-ph/0507001")]
    result = parse_atom_id_list(
        (FIXTURES / "search_page.xml").read_bytes(),
        requested,
        response_url=API_URL,
        fetched_at=1.0,
    )
    (lookup,) = result.lookups
    assert lookup.status == "found"
    assert lookup.meta is not None and lookup.meta.version == 3


# ---------------------------------------------------------------- Service entry points


def test_service_search_sends_explicit_params_and_records_provenance(tmp_path):
    transport = FakeTransport([atom_response("search_page.xml")])
    clock = FakeClock()
    service = make_service(transport, clock, tmp_path)
    page = service.search(REQUEST)
    (call,) = transport.calls
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(call["url"]).query))
    assert query["sortBy"] == "submittedDate"
    assert query["sortOrder"] == "ascending"
    assert "submittedDate:[" in query["search_query"]
    assert call["max_bytes"] == API_MAX_RESPONSE_BYTES
    assert call["timeout"] == API_TIMEOUT
    assert page.response_url == API_URL
    assert page.fetched_at == 1000.0
    assert len(page.entries) == 2


def test_service_rejects_html_error_payload(tmp_path):
    transport = FakeTransport(
        [
            TransportResponse(
                status_code=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=(FIXTURES / "html_error.html").read_bytes(),
                request_url=API_URL,
                final_url=API_URL,
            )
        ]
    )
    service = make_service(transport, FakeClock(), tmp_path)
    with pytest.raises(ArxivResponseError, match="HTML"):
        service.search(REQUEST)


def test_service_empty_atom_is_valid_empty_result(tmp_path):
    transport = FakeTransport([atom_response("empty_page.xml")])
    service = make_service(transport, FakeClock(), tmp_path)
    page = service.search(REQUEST)
    assert page.total_results == 0 and page.entries == ()


def test_service_metadata_by_ids_builds_id_list(tmp_path):
    transport = FakeTransport([atom_response("batch_page.xml")])
    service = make_service(transport, FakeClock(), tmp_path)
    result = service.metadata_by_ids(["2101.01234", "2101.99999"])
    (call,) = transport.calls
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(call["url"]).query))
    assert query["id_list"] == "2101.01234,2101.99999"
    assert query["max_results"] == "2"
    assert [lookup.status for lookup in result.lookups] == ["found", "not_found"]


def test_service_metadata_by_ids_refuses_malformed_before_request(tmp_path):
    transport = FakeTransport([])
    service = make_service(transport, FakeClock(), tmp_path)
    with pytest.raises(ArxivIdentifierError):
        service.metadata_by_ids(["not an id"])
    with pytest.raises(ArxivQueryError):
        service.metadata_by_ids([])
    assert transport.calls == []


def test_service_metadata_by_ids_rejects_oversized_batch_before_request(tmp_path):
    """Reject requests above the engineering batch limit before sending, without relying on
    server truncation.
    """
    transport = FakeTransport([])
    service = make_service(transport, FakeClock(), tmp_path)
    ids = [f"2101.9{i:04d}" for i in range(MAX_ID_LIST_LENGTH + 1)]
    with pytest.raises(ArxivQueryError, match="单批标识数") as excinfo:
        service.metadata_by_ids(ids)
    message = str(excinfo.value)
    assert (
        str(MAX_ID_LIST_LENGTH) in message
    )  # Include the limit in the error so callers can split batches.
    assert transport.calls == []


def test_service_metadata_by_ids_allows_batch_at_documented_limit(tmp_path):
    """Send requests exactly at the engineering batch limit normally."""
    transport = FakeTransport([atom_response("batch_page.xml")])
    service = make_service(transport, FakeClock(), tmp_path)
    ids = [f"2101.9{i:04d}" for i in range(MAX_ID_LIST_LENGTH)]
    result = service.metadata_by_ids(ids)
    (call,) = transport.calls
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(call["url"]).query))
    assert query["id_list"].count(",") == MAX_ID_LIST_LENGTH - 1
    assert query["max_results"] == str(MAX_ID_LIST_LENGTH)
    assert len(result.lookups) == MAX_ID_LIST_LENGTH


def test_service_rejects_oversized_request_url_before_network(tmp_path):
    """Reject request URLs above the engineering length limit before sending and report the
    limit.
    """
    transport = FakeTransport([])
    service = make_service(transport, FakeClock(), tmp_path)
    request = ArxivSearchRequest(
        date(2026, 1, 1), date(2026, 1, 31), terms="star" * 5000
    )
    with pytest.raises(ArxivQueryError, match="工程上限"):
        service.search(request)
    assert transport.calls == []
    assert len(request.to_request_url().encode("utf-8")) > MAX_REQUEST_URL_BYTES


def test_service_download_source_requires_frozen_version(tmp_path):
    transport = FakeTransport([])
    service = make_service(transport, FakeClock(), tmp_path)
    with pytest.raises(ArxivIdentifierError, match="具体版本"):
        service.download_source("2101.01234")
    assert transport.calls == []


def test_service_download_source_returns_raw_bytes(tmp_path):
    transport = FakeTransport(
        [
            TransportResponse(
                status_code=200,
                headers={"content-type": "application/x-e-print-tar"},
                body=b"SYNTH-TAR-BYTES",
                request_url="https://arxiv.org/e-print/2101.01234v2",
                final_url="https://arxiv.org/e-print/2101.01234v2",
            )
        ]
    )
    service = make_service(transport, FakeClock(), tmp_path)
    download = service.download_source("2101.01234v2")
    (call,) = transport.calls
    assert call["url"] == "https://arxiv.org/e-print/2101.01234v2"
    assert call["max_bytes"] == SOURCE_MAX_RESPONSE_BYTES
    assert call["timeout"] == SOURCE_TIMEOUT
    assert download.payload == b"SYNTH-TAR-BYTES"
    assert download.byte_count == len(b"SYNTH-TAR-BYTES")
    assert download.content_type == "application/x-e-print-tar"
    assert download.identifier.versioned_id == "2101.01234v2"
    assert download.fetched_at == 1000.0


def test_service_download_source_legacy_identifier_url(tmp_path):
    transport = FakeTransport(
        [
            TransportResponse(
                status_code=200,
                headers={"content-type": "application/gzip"},
                body=b"legacy-bytes",
                request_url="https://arxiv.org/e-print/astro-ph/0507001v3",
                final_url="https://arxiv.org/e-print/astro-ph/0507001v3",
            )
        ]
    )
    service = make_service(transport, FakeClock(), tmp_path)
    download = service.download_source("astro-ph/0507001v3")
    assert transport.calls[0]["url"] == "https://arxiv.org/e-print/astro-ph/0507001v3"
    assert download.payload == b"legacy-bytes"


# ------------------------------------------- Final source URL identity/version validation


def source_response(final_url: str, *, body: bytes = b"payload") -> TransportResponse:
    return TransportResponse(
        status_code=200,
        headers={"content-type": "application/x-e-print-tar"},
        body=body,
        request_url="https://arxiv.org/e-print/2101.01234v2",
        final_url=final_url,
    )


def test_download_rejects_final_url_pointing_to_different_version(tmp_path):
    """Reject bytes when the final URL points to v3 but v2 was requested; do not label them
    v2.
    """
    transport = FakeTransport(
        [source_response("https://arxiv.org/e-print/2101.01234v3")]
    )
    service = make_service(transport, FakeClock(), tmp_path)
    with pytest.raises(ArxivSourceError, match="不对应"):
        service.download_source("2101.01234v2")


@pytest.mark.parametrize(
    "final_url",
    [
        # An unversioned URL cannot establish correspondence.
        "https://arxiv.org/e-print/2101.01234",
        # A completely different paper.
        "https://arxiv.org/e-print/2101.99999v2",
        # Unofficial paths and extra URL components.
        "https://arxiv.org/attacker/e-print/2101.01234v2",
        "https://arxiv.org/e-print/2101.01234v2?injected=1",
        "https://arxiv.org/e-print/2101.01234v2#frag",
        "https://evil.example/e-print/2101.01234v2",
        "https://user:pass@arxiv.org/e-print/2101.01234v2",
        "https://arxiv.org/e-print/2101.01234v2/extra",
        # Protocols/ports: review reproduced accepting FTP and nonstandard ports and delivering bytes.
        "ftp://arxiv.org/e-print/2101.01234v2",
        "http://arxiv.org/e-print/2101.01234v2",
        "https://arxiv.org:8080/e-print/2101.01234v2",
        "https://export.arxiv.org:8443/e-print/2101.01234v2",
    ],
)
def test_download_rejects_untrustworthy_final_urls(tmp_path, final_url):
    """Validate independently at the service boundary for injected clients, without relying
    on outbound transport preflight.
    """
    transport = FakeTransport([source_response(final_url)])
    service = make_service(transport, FakeClock(), tmp_path)
    with pytest.raises(ArxivSourceError):
        service.download_source("2101.01234v2")


def test_download_accepts_official_src_path_with_exact_identity(tmp_path):
    """Accept an exact versioned identifier on the official /src/ path."""
    transport = FakeTransport([source_response("https://arxiv.org/src/2101.01234v2")])
    service = make_service(transport, FakeClock(), tmp_path)
    download = service.download_source("2101.01234v2")
    assert download.payload == b"payload"
    assert download.identifier.versioned_id == "2101.01234v2"


def test_download_accepts_percent_encoded_legacy_identity(tmp_path):
    transport = FakeTransport(
        [
            TransportResponse(
                status_code=200,
                headers={"content-type": "application/gzip"},
                body=b"legacy",
                request_url="https://arxiv.org/e-print/astro-ph/0507001v3",
                final_url="https://arxiv.org/e-print/astro-ph%2F0507001v3",
            )
        ]
    )
    service = make_service(transport, FakeClock(), tmp_path)
    download = service.download_source("astro-ph/0507001v3")
    assert download.payload == b"legacy"


@pytest.mark.parametrize(
    "content_type,reason",
    [
        ("text/html; charset=utf-8", "html_error"),
        ("application/xhtml+xml", "html_error"),
        ("application/pdf", "pdf_only"),
    ],
)
def test_service_download_source_unavailable_reasons(tmp_path, content_type, reason):
    transport = FakeTransport(
        [
            TransportResponse(
                status_code=200,
                headers={"content-type": content_type},
                body=b"payload",
                request_url="https://arxiv.org/e-print/2101.01234v2",
                final_url="https://arxiv.org/e-print/2101.01234v2",
            )
        ]
    )
    service = make_service(transport, FakeClock(), tmp_path)
    with pytest.raises(ArxivSourceUnavailableError) as excinfo:
        service.download_source("2101.01234v2")
    assert excinfo.value.reason == reason


def test_service_download_source_404_maps_to_not_found(tmp_path):
    transport = FakeTransport(
        [
            ArxivHttpError(
                "HTTP 404",
                url="https://arxiv.org/e-print/2101.01234v9",
                status_code=404,
                headers={},
                body=b"",
            )
        ]
    )
    service = make_service(transport, FakeClock(), tmp_path)
    with pytest.raises(ArxivSourceUnavailableError) as excinfo:
        service.download_source("2101.01234v9")
    assert excinfo.value.reason == "not_found"


def test_service_download_source_empty_body_is_failure(tmp_path):
    transport = FakeTransport(
        [
            TransportResponse(
                status_code=200,
                headers={"content-type": "application/x-e-print-tar"},
                body=b"",
                request_url="https://arxiv.org/e-print/2101.01234v2",
                final_url="https://arxiv.org/e-print/2101.01234v2",
            )
        ]
    )
    service = make_service(transport, FakeClock(), tmp_path)
    with pytest.raises(ArxivSourceError, match="空内容"):
        service.download_source("2101.01234v2")


# ------------------------------------------------ R live-fix: negotiation and error classification


def test_service_download_source_sends_explicit_accept_headers(tmp_path):
    """Declare source-download content negotiation explicitly, correcting the client-side
    cause of a live 406.

    Previously no Accept header was sent. Require supported representations and identity
    transfer encoding, without spoofing a browser or changing endpoints.
    """
    transport = FakeTransport(
        [
            TransportResponse(
                status_code=200,
                headers={"content-type": "application/x-e-print-tar"},
                body=b"accept-bytes",
                request_url="https://arxiv.org/e-print/2101.01234v2",
                final_url="https://arxiv.org/e-print/2101.01234v2",
            )
        ]
    )
    service = make_service(transport, FakeClock(), tmp_path)
    service.download_source("2101.01234v2")
    (call,) = transport.calls
    assert call["headers"] is not None
    accept = call["headers"].get("Accept", "")
    # Deliver tar/gzip source formats; accept PDF/HTML responses for content-type classification too.
    for expected in (
        "application/x-eprint-tar",
        "application/gzip",
        "application/x-tar",
        "application/pdf",
        "*/*",
    ):
        assert expected in accept, (expected, accept)
    assert call["headers"].get("Accept-Encoding") == "identity"


def test_service_download_source_406_preserves_http_error(tmp_path):
    """Propagate HTTP 406 and other non-404 responses as transport HTTP errors.

    Only 404 means no obtainable source package for that version. Do not guess causes
    for 406/403; let CLI transport diagnostics report them.
    """
    transport = FakeTransport(
        [
            ArxivHttpError(
                "HTTP 406：https://arxiv.org/e-print/2101.01234v2",
                url="https://arxiv.org/e-print/2101.01234v2",
                status_code=406,
                headers={"content-type": "text/html"},
            )
        ]
    )
    service = make_service(transport, FakeClock(), tmp_path)
    with pytest.raises(ArxivHttpError) as excinfo:
        service.download_source("2101.01234v2")
    assert excinfo.value.status_code == 406


def test_service_download_source_429_propagates_rate_limited(tmp_path):
    """Propagate persistent 429 after bounded client retries as a rate-limit error for CLI
    classification.
    """
    rate_error = ArxivRateLimitedError(
        "HTTP 429：rate exceeded",
        url="https://arxiv.org/e-print/2101.01234v2",
        status_code=429,
        headers={},
    )
    transport = FakeTransport(
        [rate_error] * 3
    )  # The default policy allows three bounded retries.
    service = make_service(transport, FakeClock(), tmp_path)
    with pytest.raises(ArxivRateLimitedError):
        service.download_source("2101.01234v2")
    assert len(transport.calls) == 3


def test_service_download_source_timeout_propagates(tmp_path):
    """Propagate transport timeouts with their classification, without unbounded retries."""
    timeout_error = ArxivTimeoutError(
        "请求超时：https://arxiv.org/e-print/2101.01234v2",
        url="https://arxiv.org/e-print/2101.01234v2",
    )
    transport = FakeTransport([timeout_error] * 3)
    service = make_service(transport, FakeClock(), tmp_path)
    with pytest.raises(ArxivTimeoutError):
        service.download_source("2101.01234v2")
