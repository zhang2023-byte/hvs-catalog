"""arXiv HTTP transport: connection leases, per-hop preflight, and bounded retries.

Every API/metadata/source/redirect/retry request passes ArxivHttpClient. Preflight
HTTPS, approved hosts, standard ports, absent credentials/fragments, then acquire shared
throttle leases. Disable automatic redirects; validate each bounded hop independently
before connecting. At most one local same-user connection exists.

For retryable 429/5xx failures, persist valid Retry-After seconds/HTTP dates before
deciding retries, including final attempts; ignore malformed/nonfinite values. Persist
valid headers on successful responses/redirects before releasing leases too, so future
hops/calls/processes/restarts respect cooldown. Never sleep while holding connections;
next leases reread/wait in steps and reject waits above policy limits after persisting
cooldown. 401/403 fail immediately without retries. Bound body reads and check declared
lengths; short/inconsistent transfers cannot succeed. Close errors before releasing
permits. Defaults establish production timeout/size/retry/hop behavior; fake
transports/clocks never weaken them.
"""

from __future__ import annotations

import email.utils
import http.client
import math
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from typing import Protocol

import requests
from urllib3.exceptions import HTTPError as RawHTTPError
from urllib3.exceptions import ReadTimeoutError as RawReadTimeoutError

from .rate_limit import Clock, RateLimitGate, SystemClock, default_rate_gate_state_path

#: Default User-Agent; callers may explicitly override it.
USER_AGENT = "hvs-catalog/0.1"

#: Default transport timeout in seconds and response byte bound.
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024

#: Approved request/redirect hosts, including subdomains.
ARXIV_ALLOWED_HOSTS = ("arxiv.org", "export.arxiv.org")

#: Redirect status codes and maximum per-request redirect hops.
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
MAX_REDIRECT_HOPS = 5

_READ_CHUNK = 64 * 1024
_PREVIEW_LIMIT = 4096


class ArxivTransportError(Exception):
    """Base transport failure carrying the failing URL."""

    def __init__(self, message: str, *, url: str) -> None:
        super().__init__(message)
        self.url = url


class ArxivConnectionError(ArxivTransportError):
    """Retryable connection failure, interrupted transfer, or declared/actual length
    mismatch.
    """


class ArxivTimeoutError(ArxivTransportError):
    """Retryable request timeout."""


class ArxivResponseSizeError(ArxivTransportError):
    """Response exceeds byte limits; never retry or return truncated contents."""


class ArxivHttpError(ArxivTransportError):
    """Base HTTP-status error."""

    def __init__(
        self,
        message: str,
        *,
        url: str,
        status_code: int,
        headers: Mapping[str, str],
        body: bytes = b"",
    ) -> None:
        super().__init__(message, url=url)
        self.status_code = status_code
        self.headers = {str(k).lower(): str(v) for k, v in headers.items()}
        self.body = bytes(body)


class ArxivAuthError(ArxivHttpError):
    """401/403 access denial: stop immediately without retries."""


class ArxivRateLimitedError(ArxivHttpError):
    """429 throttling: cooldown is registered; retry within bounded waits or propagate."""


class ArxivServerError(ArxivHttpError):
    """Retryable transient 5xx server failure."""


@dataclass(frozen=True)
class TransportResponse:
    """Complete successful single-hop HTTP response.

    Return 3xx+Location unchanged for client per-hop decisions; final_url identifies
    this hop itself.
    """

    status_code: int
    headers: dict[str, str]
    body: bytes
    request_url: str
    final_url: str


class Transport(Protocol):
    """Injectable transport responsible for single-hop connections and bounded reads."""

    def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        max_bytes: int | None = None,
    ) -> TransportResponse: ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Disable urllib automatic redirects; return 3xx for client per-hop handling."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def parse_retry_after(value: str | None, *, now: float) -> float | None:
    """Parse Retry-After seconds or HTTP dates into seconds from now.

    Empty/malformed/nonfinite values return None without affecting ordinary backoff;
    expired dates return 0.0.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        seconds = float(text)
        if not math.isfinite(seconds):
            return None
        return max(0.0, seconds)
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    seconds = parsed.timestamp() - now
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def read_bounded(
    stream,
    *,
    url: str,
    max_bytes: int,
    content_length: int | None = None,
) -> bytes:
    """Read complete bodies within bounds and verify declared lengths.

    Content-Length mismatches, including short EOF reads, raise ArxivConnectionError;
    partial transfers never succeed.
    """
    if max_bytes <= 0:
        raise ValueError(f"响应上限必须是正整数：{max_bytes!r}")
    if content_length is not None and content_length > max_bytes:
        raise ArxivResponseSizeError(
            f"响应声明大小 {content_length} 已超过上限 {max_bytes}：{url}", url=url
        )
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ArxivResponseSizeError(
                f"响应体超过字节上限 {max_bytes}：{url}", url=url
            )
        chunks.append(chunk)
    if content_length is not None and total != content_length:
        raise ArxivConnectionError(
            f"传输长度与声明不符：声明 {content_length} 字节，实际读到 "
            f"{total} 字节（{url}）",
            url=url,
        )
    return b"".join(chunks)


def _host_matches(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    return any(
        host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts
    )


def _require_safe_outgoing_url(url: str, allowed_hosts: tuple[str, ...]) -> str:
    """Preflight HTTPS, approved hosts, standard ports, absent credentials/fragments before
    every connection/redirect.

    Reject ambiguous/nonstandard URLs. Historical Atom HTTP identifiers are not outbound
    requests and are separately validated by protocol without this HTTPS requirement.
    """
    if not url or any(character.isspace() for character in url):
        raise ArxivTransportError(f"拒绝空白或含空白的出站 URL：{url!r}", url=url)
    try:
        split = urllib.parse.urlsplit(url)
        port = split.port
    except ValueError as exc:
        raise ArxivTransportError(f"URL 无法解析：{url!r}", url=url) from exc
    problems: list[str] = []
    if split.scheme != "https":
        problems.append(f"scheme {split.scheme!r} 不是 https")
    if split.username or split.password:
        problems.append("包含凭据")
    host = (split.hostname or "").lower()
    if not host or not _host_matches(host, allowed_hosts):
        problems.append(f"主机 {host!r} 不在允许列表 {allowed_hosts}")
    if port is not None and port != 443:
        problems.append(f"端口 {port} 非标准（仅允许缺省或 443）")
    if split.fragment:
        problems.append("包含 fragment")
    if problems:
        raise ArxivTransportError(
            f"拒绝非标准出站 URL：{'；'.join(problems)}：{url}", url=url
        )
    return url


def _is_timeout_reason(reason: object) -> bool:
    return isinstance(reason, TimeoutError) or (
        isinstance(reason, BaseException) and "timed out" in str(reason)
    )


def _error_class_for_status(status: int) -> type[ArxivHttpError]:
    if status in (401, 403):
        return ArxivAuthError
    if status == 429:
        return ArxivRateLimitedError
    if 500 <= status <= 599:
        return ArxivServerError
    return ArxivHttpError


class UrllibTransport:
    """Retained standard-library single-hop transport without automatic redirects.

    Return 3xx/Location unchanged for client validation/throttling. Close all
    response/error bodies before return or raise.
    """

    def __init__(
        self,
        *,
        default_timeout: float = DEFAULT_TIMEOUT,
        default_max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self.default_timeout = float(default_timeout)
        self.default_max_bytes = int(default_max_bytes)
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        max_bytes: int | None = None,
    ) -> TransportResponse:
        effective_timeout = self.default_timeout if timeout is None else float(timeout)
        effective_max = self.default_max_bytes if max_bytes is None else int(max_bytes)
        request_headers = {"User-Agent": USER_AGENT}
        if headers:
            request_headers.update(dict(headers))
        request = urllib.request.Request(url, headers=request_headers, method="GET")
        try:
            response = self._opener.open(request, timeout=effective_timeout)
        except urllib.error.HTTPError as exc:
            return self._handle_http_error(exc, url, max_bytes=effective_max)
        except urllib.error.URLError as exc:
            if _is_timeout_reason(exc.reason):
                raise ArxivTimeoutError(f"请求超时：{url}", url=url) from exc
            raise ArxivConnectionError(
                f"连接失败：{url}（{exc.reason}）", url=url
            ) from exc
        except TimeoutError as exc:
            raise ArxivTimeoutError(f"请求超时：{url}", url=url) from exc
        try:
            with response:
                final_url = str(response.geturl())
                status = int(getattr(response, "status", None) or response.getcode())
                headers_map = {k.lower(): v for k, v in response.headers.items()}
                if status in REDIRECT_STATUSES:
                    # Close unused redirect bodies before returning for client hop decisions.
                    return TransportResponse(
                        status_code=status,
                        headers=headers_map,
                        body=b"",
                        request_url=url,
                        final_url=final_url,
                    )
                content_length: int | None = None
                raw_length = headers_map.get("content-length")
                if raw_length is not None and raw_length.strip().isdigit():
                    content_length = int(raw_length.strip())
                body = read_bounded(
                    response,
                    url=url,
                    max_bytes=effective_max,
                    content_length=content_length,
                )
        except ArxivTransportError:
            raise
        except TimeoutError as exc:
            raise ArxivTimeoutError(f"读取响应超时：{url}", url=url) from exc
        except http.client.HTTPException as exc:
            raise ArxivConnectionError(
                f"响应传输中断：{url}（{exc}）", url=url
            ) from exc
        except OSError as exc:
            raise ArxivConnectionError(
                f"读取响应失败：{url}（{exc}）", url=url
            ) from exc
        return TransportResponse(
            status_code=status,
            headers=headers_map,
            body=body,
            request_url=url,
            final_url=final_url,
        )

    def _handle_http_error(
        self, exc: urllib.error.HTTPError, url: str, *, max_bytes: int
    ) -> TransportResponse:
        """Convert HTTPError into redirect responses or exceptions, reliably closing
        bodies.
        """
        try:
            headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
            status = int(exc.code)
            if status in REDIRECT_STATUSES:
                return TransportResponse(
                    status_code=status,
                    headers=headers,
                    body=b"",
                    request_url=url,
                    final_url=str(getattr(exc, "url", None) or url),
                )
            try:
                preview = exc.read(_PREVIEW_LIMIT)
            except Exception:  # noqa: BLE001 - failed preview reads must not hide the original status
                preview = b""
            snippet = preview.decode("utf-8", errors="replace").strip()
            message = f"HTTP {status}：{url}"
            if snippet:
                message = f"{message}（响应片段：{snippet[:200]}）"
            error_class = _error_class_for_status(status)
            error = error_class(
                message, url=url, status_code=status, headers=headers, body=preview
            )
        finally:
            exc.close()
        raise error


class RequestsTransport:
    """Single-hop requests transport without automatic redirects or transfer decoding.

    Use public HTTPAdapter.send instead of Session.get, which may read whole redirect
    bodies even with redirects disabled. Read bounded success bodies/error previews, no
    redirect bodies; close connections before leaving shared leases.
    """

    def __init__(
        self,
        *,
        default_timeout: float = DEFAULT_TIMEOUT,
        default_max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self.default_timeout = float(default_timeout)
        self.default_max_bytes = int(default_max_bytes)

    def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        max_bytes: int | None = None,
    ) -> TransportResponse:
        effective_timeout = self.default_timeout if timeout is None else float(timeout)
        effective_max = self.default_max_bytes if max_bytes is None else int(max_bytes)
        request_headers = {
            **requests.utils.default_headers(),
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "identity",
            **dict(headers or {}),
        }
        # Request.prepare avoids implicit netrc credentials and session cookies.
        prepared = requests.Request("GET", url, headers=request_headers).prepare()
        try:
            with requests.Session() as session:
                settings = session.merge_environment_settings(
                    prepared.url, {}, True, None, None
                )
                # Default adapter retries=0. Retry and redirect ownership stays
                # exclusively with ArxivHttpClient and its shared rate gate.
                with session.get_adapter(prepared.url).send(
                    prepared, timeout=effective_timeout, **settings
                ) as response:
                    status = response.status_code
                    hs = {k.lower(): v for k, v in response.headers.items()}
                    if status in REDIRECT_STATUSES:
                        body = b""
                    elif status >= 400:
                        try:
                            preview = response.raw.read(
                                _PREVIEW_LIMIT, decode_content=False
                            )
                        except Exception:  # noqa: BLE001 - preserve HTTP/cooldown
                            preview = b""
                        snippet = preview.decode("utf-8", errors="replace").strip()
                        message = f"HTTP {status}：{url}"
                        if snippet:
                            message += f"（响应片段：{snippet[:200]}）"
                        raise _error_class_for_status(status)(
                            message,
                            url=url,
                            status_code=status,
                            headers=hs,
                            body=preview,
                        )
                    else:
                        response.raw.decode_content = False
                        length = hs.get("content-length", "").strip()
                        body = read_bounded(
                            response.raw,
                            url=url,
                            max_bytes=effective_max,
                            content_length=int(length) if length.isdigit() else None,
                        )
                    return TransportResponse(
                        status_code=status,
                        headers=hs,
                        body=body,
                        request_url=url,
                        final_url=str(response.url),
                    )
        except ArxivTransportError:
            raise
        except (requests.exceptions.Timeout, RawReadTimeoutError, TimeoutError) as exc:
            raise ArxivTimeoutError(f"请求或读取超时：{url}", url=url) from exc
        except (requests.exceptions.RequestException, RawHTTPError, OSError) as exc:
            raise ArxivConnectionError(
                f"连接或读取失败：{url}（{exc}）", url=url
            ) from exc


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retry policy: total attempts, exponential backoff, and per-attempt wait
    limit.

    backoff_seconds uses one-based failed-attempt numbers. Longer Retry-After wins and
    persists before retry decisions, including final attempts. max_retry_wait bounds
    current waits; propagate excess for callers to arrange later retries.
    """

    max_attempts: int = 3
    backoff_base: float = 3.0
    backoff_cap: float = 60.0
    max_retry_wait: float = 300.0

    def __post_init__(self) -> None:
        if not 1 <= int(self.max_attempts) <= 10:
            raise ValueError(f"总尝试次数必须在 1..10：{self.max_attempts!r}")
        if not 0.0 <= float(self.backoff_base) <= float(self.backoff_cap):
            raise ValueError(
                f"退避基数必须不小于 0 且不超过上限：{self.backoff_base!r}"
            )
        if float(self.backoff_cap) <= 0:
            raise ValueError(f"退避上限必须是正数：{self.backoff_cap!r}")
        if float(self.max_retry_wait) <= 0:
            raise ValueError(f"单次重试等待上限必须是正数：{self.max_retry_wait!r}")

    def backoff_seconds(self, attempt: int) -> float:
        if attempt < 1:
            raise ValueError(f"尝试序号从 1 开始：{attempt!r}")
        return min(
            self.backoff_base * (2 ** (attempt - 1)),
            self.backoff_cap,
        )


def _is_retryable(exc: ArxivTransportError) -> bool:
    return isinstance(
        exc,
        (
            ArxivTimeoutError,
            ArxivConnectionError,
            ArxivRateLimitedError,
            ArxivServerError,
        ),
    )


class ArxivHttpClient:
    """Shared-gate arXiv client with per-hop preflight and bounded retries.

    Defaults use RequestsTransport, shared user-cache gate, production RetryPolicy, and
    SystemClock. allowed_hosts allowlists hops; max_redirect_hops excludes the initial
    hop. Bound connections by hops times attempts. Construction has no writes/network
    effects; first leases create state.
    """

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        gate: RateLimitGate | None = None,
        retry_policy: RetryPolicy | None = None,
        clock: Clock | None = None,
        allowed_hosts: tuple[str, ...] = ARXIV_ALLOWED_HOSTS,
        max_redirect_hops: int = MAX_REDIRECT_HOPS,
    ) -> None:
        self._transport: Transport = (
            transport if transport is not None else RequestsTransport()
        )
        self._gate = (
            gate if gate is not None else RateLimitGate(default_rate_gate_state_path())
        )
        self.retry_policy = retry_policy if retry_policy is not None else RetryPolicy()
        self._clock: Clock = clock if clock is not None else SystemClock()
        self.allowed_hosts = tuple(allowed_hosts)
        if not self.allowed_hosts:
            raise ValueError("allowed_hosts 不能为空")
        if not 0 <= int(max_redirect_hops) <= 20:
            raise ValueError(f"重定向上限必须在 0..20：{max_redirect_hops!r}")
        self.max_redirect_hops = int(max_redirect_hops)

    @property
    def gate(self) -> RateLimitGate:
        return self._gate

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        max_bytes: int | None = None,
    ) -> TransportResponse:
        """GET a URL with per-hop preflight, leases, and bounded retries."""
        current = _require_safe_outgoing_url(url, self.allowed_hosts)
        hops = 0
        while True:
            response = self._fetch_hop(
                current, headers=headers, timeout=timeout, max_bytes=max_bytes
            )
            if response.status_code in REDIRECT_STATUSES:
                location = response.headers.get("location")
                if not location:
                    raise ArxivTransportError(
                        f"重定向响应缺少 Location，无法确定下一跳：{current}",
                        url=current,
                    )
                hops += 1
                if hops > self.max_redirect_hops:
                    raise ArxivTransportError(
                        f"重定向跳数超过上限 {self.max_redirect_hops}：{location}",
                        url=current,
                    )
                target = urllib.parse.urljoin(current, location)
                current = _require_safe_outgoing_url(target, self.allowed_hosts)
                continue
            return response

    def _fetch_hop(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None,
        timeout: float | None,
        max_bytes: int | None,
    ) -> TransportResponse:
        """Request one hop with one lease per attempt and backoff outside leases.

        Persist valid exception/response Retry-After before release, including
        redirects. Future hops/retries reread waits through next leases without sleeping
        on connections. Excess max_retry_wait raises after cooldown persists for later
        processes/calls.
        """
        policy = self.retry_policy
        for attempt in range(1, policy.max_attempts + 1):
            wait: float | None = None
            with self._gate.transfer(max_wait=policy.max_retry_wait) as lease:
                try:
                    response = self._transport.fetch(
                        url, headers=headers, timeout=timeout, max_bytes=max_bytes
                    )
                except ArxivTransportError as exc:
                    retry_after: float | None = None
                    if isinstance(exc, ArxivHttpError):
                        retry_after = parse_retry_after(
                            exc.headers.get("retry-after"), now=self._clock.now()
                        )
                    if retry_after is not None and retry_after > 0:
                        # Persist valid cooldown, including final attempts, before deciding retries;
                        # expired zero-duration cooldown adds no constraint and needs no persistence.
                        lease.record_cooldown(self._clock.now() + retry_after)
                    if not _is_retryable(exc) or attempt >= policy.max_attempts:
                        raise
                    wait = max(retry_after or 0.0, policy.backoff_seconds(attempt))
                    if wait > policy.max_retry_wait:
                        raise
                else:
                    self._persist_response_cooldown(lease, response)
                    return response
            if wait is not None:
                # Backoff/cooldown waits happen outside leases; next leases reread latest state.
                self._clock.sleep(wait)
                continue
            raise AssertionError("不可达：成功响应已在租约内返回")
        raise AssertionError("不可达：重试循环必须返回或抛出")

    def _persist_response_cooldown(self, lease, response: TransportResponse) -> None:
        """Persist successful-response Retry-After inside the lease."""
        retry_after = parse_retry_after(
            response.headers.get("retry-after"), now=self._clock.now()
        )
        if retry_after is not None and retry_after > 0:
            lease.record_cooldown(self._clock.now() + retry_after)


def default_arxiv_client() -> ArxivHttpClient:
    """Production client: requests transport, user-shared throttle gate, and system clock."""
    return ArxivHttpClient()


__all__ = [
    "ARXIV_ALLOWED_HOSTS",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_TIMEOUT",
    "MAX_REDIRECT_HOPS",
    "REDIRECT_STATUSES",
    "USER_AGENT",
    "ArxivAuthError",
    "ArxivConnectionError",
    "ArxivHttpClient",
    "ArxivHttpError",
    "ArxivRateLimitedError",
    "ArxivResponseSizeError",
    "ArxivServerError",
    "ArxivTimeoutError",
    "ArxivTransportError",
    "RequestsTransport",
    "RetryPolicy",
    "SystemClock",
    "Transport",
    "TransportResponse",
    "UrllibTransport",
    "default_arxiv_client",
    "parse_retry_after",
    "read_bounded",
]
