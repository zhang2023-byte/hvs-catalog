"""arXiv HTTP 传输：连接租约、逐跳预检与有限重试。

所有对 arXiv 的请求（API 查询、元数据读取、源码包下载、重定向与重
试）都经过 :class:`ArxivHttpClient`：每一跳先做**出站预检**（仅
HTTPS、arXiv 允许主机、标准端口、无凭据、无 fragment），再通过共享
节流门的连接租约发送。自动重定向被禁用：每个重定向目标在连接之前
逐跳预检并单独过节流门，跳数有界；任意时刻本机同用户至多一个连接。

失败语义（C07）：429/5xx 等可重试失败在决定是否重试**之前**先把有
效 ``Retry-After``（秒数或 HTTP 日期；非法或非有限值忽略）写入共享
冷却，最后一次尝试也不例外；**正常返回的响应**（尤其是重定向响
应）携带的有效 ``Retry-After`` 同样在释放当次租约前持久化，下一跳
或后续任何调用（含其他进程/重启）都会遵守。持有连接时不睡眠：等
待全部由下一个租约重读状态后分段执行，且单次等待超过重试策略上
限时抛出（``RateLimitError``），冷却已持久化。401/403 立即失败、
绝不重试。响应体按字节上限读取并核对声明长度，短读或不一致立即
失败，部分传输绝不当作成功；错误响应可靠关闭后才释放许可。传输
默认值（超时、响应上限、重试次数、跳数上限）即生产行为；测试注入
假传输与假时钟，不削弱生产默认值。
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

#: 请求头中的 User-Agent；调用方可显式覆盖。
USER_AGENT = "hvs-catalog/0.1"

#: 传输默认超时（秒）与默认响应体上限（字节）。
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024

#: 请求与每个重定向目标允许的主机（含子域）。
ARXIV_ALLOWED_HOSTS = ("arxiv.org", "export.arxiv.org")

#: 视为重定向的状态码与单次请求允许的最大重定向跳数。
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
MAX_REDIRECT_HOPS = 5

_READ_CHUNK = 64 * 1024
_PREVIEW_LIMIT = 4096


class ArxivTransportError(Exception):
    """传输层失败的基类；携带出错的请求 URL。"""

    def __init__(self, message: str, *, url: str) -> None:
        super().__init__(message)
        self.url = url


class ArxivConnectionError(ArxivTransportError):
    """连接失败、传输中断或声明长度与实际不符（可重试）。"""


class ArxivTimeoutError(ArxivTransportError):
    """请求超时（可重试）。"""


class ArxivResponseSizeError(ArxivTransportError):
    """响应体超过字节上限（不重试；绝不返回截断内容）。"""


class ArxivHttpError(ArxivTransportError):
    """HTTP 状态码错误基类。"""

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
    """401/403：访问被拒绝，立即停止，绝不重试。"""


class ArxivRateLimitedError(ArxivHttpError):
    """429：被限流；冷却已登记，按有界等待重试或向上抛出。"""


class ArxivServerError(ArxivHttpError):
    """5xx：服务端瞬时故障（可重试）。"""


@dataclass(frozen=True)
class TransportResponse:
    """一次成功 HTTP 请求的完整响应。

    重定向响应（3xx + ``Location``）原样返回，由客户端决定是否逐跳
    跟随；因此本类型的 ``final_url`` 是该跳自身的 URL。
    """

    status_code: int
    headers: dict[str, str]
    body: bytes
    request_url: str
    final_url: str


class Transport(Protocol):
    """可注入的底层传输；实现方负责单跳请求的连接与读取上限。"""

    def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        max_bytes: int | None = None,
    ) -> TransportResponse: ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁用 urllib 自动重定向：3xx 原样交回客户端逐跳处理。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def parse_retry_after(value: str | None, *, now: float) -> float | None:
    """解析 ``Retry-After``：接受秒数或 HTTP 日期，返回距 ``now`` 的秒数。

    为空、非法或非有限（``inf``/``nan``）时返回 ``None``（视为无有效
    冷却，不影响常规退避）；日期已过期时返回 ``0.0``。
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
    """按上限读取完整响应体，并核对声明长度；不一致即失败。

    声明 ``Content-Length`` 大于实际读到 EOF 的字节数（短读）或与其
    不一致时抛 :class:`ArxivConnectionError`：短传输绝不能当作成功。
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
    """出站预检：仅 HTTPS、允许主机、标准端口、无凭据、无 fragment。

    在任何连接建立之前调用，包括每个重定向目标。拒绝模糊或非标准
    URL，不做猜测。注意：Atom 条目中的 ``http://arxiv.org/abs/...``
    是历史标识符形式，不是出站请求，由协议层单独校验，不受本函数
    的 HTTPS 约束。
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
    """保留的标准库传输：单跳一个连接，不自动跟随重定向。

    3xx 重定向响应原样返回（含 ``Location``），由
    :class:`ArxivHttpClient` 逐跳预检、过节流门后再连接。所有响应与
    错误体在返回或抛出前可靠关闭。
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
                    # 重定向体不需要；先关闭再交回客户端逐跳决定。
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
        """把 HTTPError 转换为重定向响应或异常；错误体可靠关闭。"""
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
            except Exception:  # noqa: BLE001 - 预览读取失败不掩盖原状态
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
    """requests 单跳传输；不自动重定向、不解码传输体。

    直接使用公开 HTTPAdapter.send，而非 Session.get：后者即使禁用
    自动跳转也可能为构造 next 请求读取整个重定向体。此处只读取有界
    成功体/错误预览，重定向体不读取；每个连接在离开共享租约前关闭。
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
    """有界重试策略：总尝试次数、指数退避与单次等待上限。

    ``backoff_seconds(attempt)`` 以已失败的尝试序号（从 1 开始）计算
    下一次尝试前的退避等待；服务端 ``Retry-After`` 比退避更久时以其
    为准。冷却在决定是否重试之前先写入共享节流状态（最后一次尝试
    也不例外）；只是当前请求内的等待不超过 ``max_retry_wait``，超过
    即向上抛出，由上层安排稍后重试。
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
    """经共享节流门发送 arXiv 请求的客户端（逐跳预检、有界重试）。

    参数：
        transport: 底层传输，默认 :class:`RequestsTransport`（生产）。
        gate: 共享节流门，默认使用用户缓存目录下的共享状态文件。
        retry_policy: 重试策略，默认 :class:`RetryPolicy` 的生产值。
        clock: 用于冷却计算与等待的时钟，默认 :class:`SystemClock`。
        allowed_hosts: 每一跳允许的主机白名单。
        max_redirect_hops: 重定向上限（不含首跳），默认
            :data:`MAX_REDIRECT_HOPS`。

    每一跳（含重定向）独立执行出站预检、取得连接租约、按策略重试；
    总连接数受跳数上限 × 尝试次数约束。构造本身没有任何磁盘写入或
    网络访问；状态文件在第一次取得租约时才创建。
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
        """对 ``url`` 执行 GET：逐跳预检 + 连接租约 + 有界重试。"""
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
        """单跳请求：每次尝试持有一个连接租约；退避在租约外等待。

        有效 ``Retry-After`` 无论来自异常还是**正常返回的响应**（尤其
        是重定向响应），都在释放当前租约之前解析并持久化；之后的等
        待（下一跳或重试的间距/冷却）由下一个租约重读后执行，不在
        持有连接时睡眠。单次等待超过 ``max_retry_wait`` 时抛出，冷却
        已持久化供后续/其他进程遵守。
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
                        # 先持久化有效冷却（含最后一次尝试），再决定是否
                        # 重试；已过期的零长度冷却不携带约束，无需落盘。
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
                # 退避/冷却等待在租约外进行；下一次租约会重读最新状态。
                self._clock.sleep(wait)
                continue
            raise AssertionError("不可达：成功响应已在租约内返回")
        raise AssertionError("不可达：重试循环必须返回或抛出")

    def _persist_response_cooldown(self, lease, response: TransportResponse) -> None:
        """在租约内持久化正常返回响应携带的有效 Retry-After。"""
        retry_after = parse_retry_after(
            response.headers.get("retry-after"), now=self._clock.now()
        )
        if retry_after is not None and retry_after > 0:
            lease.record_cooldown(self._clock.now() + retry_after)


def default_arxiv_client() -> ArxivHttpClient:
    """生产默认客户端：requests 传输 + 用户级共享节流门 + 系统时钟。"""
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
