"""Production single-hop adapter, all responses synthetic and offline."""

import io

import pytest
import requests
from urllib3.exceptions import ProtocolError, ReadTimeoutError

from hvs_catalog.pipeline.acquisition.transport import (
    USER_AGENT,
    ArxivConnectionError,
    ArxivHttpClient,
    ArxivHttpError,
    ArxivRateLimitedError,
    ArxivTimeoutError,
    ArxivTransportError,
    RequestsTransport,
)

URL = "https://arxiv.org/e-print/2101.01234v2"


class Raw(io.BytesIO):
    decode_content = True

    def __init__(self, body=b"abc", error=None):
        super().__init__(body)
        self.read_sizes = []
        self.error = error

    def read(self, size=-1, *, decode_content=False):
        self.read_sizes.append(size)
        assert size > 0
        assert not decode_content
        if self.error:
            raise self.error
        return super().read(size)

    def release_conn(self):
        pass


@pytest.fixture
def wire(monkeypatch):
    calls = []
    responses = []

    def send(adapter, request, **kwargs):
        calls.append((request, kwargs, adapter.max_retries.total))
        value = responses.pop(0)
        if isinstance(value, Exception):
            raise value
        value.url = request.url
        return value

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return calls, responses


def response(status=200, headers=None, body=b"abc", error=None):
    value = requests.Response()
    value.status_code = status
    value.headers.update(headers or {})
    value.raw = Raw(body, error)
    return value


def test_requests_defaults_and_success(wire):
    calls, responses = wire
    value = response(headers={"Content-Length": "3"})
    responses.append(value)
    result = RequestsTransport().fetch(URL, max_bytes=3, timeout=7)
    assert result.body == b"abc"
    req, settings, retries = calls[0]
    assert req.headers["User-Agent"] == USER_AGENT
    assert req.headers["Accept-Encoding"] == "identity"
    assert settings["stream"] is True
    assert settings["timeout"] == 7
    assert settings["verify"] is not False
    assert retries == 0
    assert value.raw.closed
    assert not value.raw.decode_content
    assert isinstance(ArxivHttpClient()._transport, RequestsTransport)


def test_requests_redirect_body_never_read(wire):
    calls, responses = wire
    value = response(
        302,
        {"Location": "https://arxiv.org/src/2101.01234v2"},
        error=AssertionError("must not read redirect"),
    )
    responses.append(value)
    result = RequestsTransport().fetch(URL)
    assert result.status_code == 302
    assert result.body == b""
    assert len(calls) == 1
    assert value.raw.read_sizes == []
    assert value.raw.closed


@pytest.mark.parametrize(
    "status,expected", [(406, ArxivHttpError), (429, ArxivRateLimitedError)]
)
def test_requests_error_bounded_closed_preserves_cooldown(wire, status, expected):
    _, responses = wire
    value = response(status, {"Retry-After": "60"}, b"x" * 10000)
    responses.append(value)
    with pytest.raises(expected) as caught:
        RequestsTransport().fetch(URL)
    assert caught.value.status_code == status
    assert caught.value.headers["retry-after"] == "60"
    assert len(caught.value.body) <= 4096
    assert value.raw.closed
    assert value.raw.read_sizes == [4096]


@pytest.mark.parametrize(
    "body,length,max_bytes",
    [(b"abcd", None, 3), (b"ab", "3", 3), (b"abcd", "3", 9), (b"abc", "100", 3)],
)
def test_requests_size_and_declared_length(wire, body, length, max_bytes):
    _, responses = wire
    value = response(headers={"Content-Length": length} if length else {}, body=body)
    responses.append(value)
    with pytest.raises(ArxivTransportError):
        RequestsTransport().fetch(URL, max_bytes=max_bytes)
    assert value.raw.closed


@pytest.mark.parametrize(
    "error,expected",
    [
        (ReadTimeoutError(None, URL, "timed out"), ArxivTimeoutError),
        (ProtocolError("short body"), ArxivConnectionError),
    ],
)
def test_requests_raw_failure_closed(wire, error, expected):
    _, responses = wire
    value = response(error=error)
    responses.append(value)
    with pytest.raises(expected):
        RequestsTransport().fetch(URL)
    assert value.raw.closed


@pytest.mark.parametrize(
    "error,expected",
    [
        (requests.exceptions.ConnectTimeout(), ArxivTimeoutError),
        (requests.exceptions.SSLError(), ArxivConnectionError),
    ],
)
def test_requests_connect_failure(wire, error, expected):
    _, responses = wire
    responses.append(error)
    with pytest.raises(expected):
        RequestsTransport().fetch(URL)


def test_requests_headers_without_netrc(wire, monkeypatch):
    calls, responses = wire
    monkeypatch.setattr(
        requests.sessions,
        "get_netrc_auth",
        lambda *_: pytest.fail("implicit credentials forbidden"),
    )
    responses.append(response())
    RequestsTransport().fetch(URL, headers={"Accept": "application/gzip"})
    assert calls[0][0].headers["Accept"] == "application/gzip"
    assert "Authorization" not in calls[0][0].headers


def test_default_client_redirect_gate_and_host_guard(wire, tmp_path):
    from hvs_catalog.pipeline.acquisition.rate_limit import RateLimitGate

    calls, responses = wire
    redirected = response(302, {"Location": "https://outside.invalid/file"})
    responses.append(redirected)
    client = ArxivHttpClient(gate=RateLimitGate(tmp_path / "gate.json"))
    with pytest.raises(ArxivTransportError):
        client.get(URL)
    assert len(calls) == 1
    assert redirected.raw.closed
    assert client.gate.snapshot().in_flight_since is None


def test_default_client_persists_rate_limit(wire, tmp_path):
    from hvs_catalog.pipeline.acquisition.rate_limit import RateLimitGate
    from hvs_catalog.pipeline.acquisition.transport import RetryPolicy

    calls, responses = wire
    responses.append(response(429, {"Retry-After": "120"}))
    client = ArxivHttpClient(
        gate=RateLimitGate(tmp_path / "gate.json"),
        retry_policy=RetryPolicy(max_attempts=1),
    )
    with pytest.raises(ArxivRateLimitedError):
        client.get(URL)
    assert len(calls) == 1
    assert client.gate.snapshot().cooldown_until is not None
    assert client.gate.snapshot().in_flight_since is None
