"""Synthetic arXiv transport tests: per-hop preflight, leases, retries/cooldown, and
response bounds (lane A).

Use fake transport/clocks offline and temporary throttle state. Assertions fix
production defaults: three-second minimum, HTTPS validation, hop limit, timeouts,
response bounds, and bounded retries. Tests must not weaken them.
"""

from __future__ import annotations

import email.message
import email.utils
from io import BytesIO
from pathlib import Path

import pytest

from hvs_catalog.pipeline.acquisition.rate_limit import (
    MIN_REQUEST_INTERVAL,
    WAIT_RECHECK_SECONDS,
    RateLimitError,
    RateLimitGate,
    default_rate_gate_state_path,
)
from hvs_catalog.pipeline.acquisition.transport import (
    ARXIV_ALLOWED_HOSTS,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_TIMEOUT,
    MAX_REDIRECT_HOPS,
    REDIRECT_STATUSES,
    USER_AGENT,
    ArxivAuthError,
    ArxivConnectionError,
    ArxivHttpClient,
    ArxivHttpError,
    ArxivRateLimitedError,
    ArxivResponseSizeError,
    ArxivServerError,
    ArxivTimeoutError,
    ArxivTransportError,
    RetryPolicy,
    TransportResponse,
    UrllibTransport,
    _error_class_for_status,
    default_arxiv_client,
    parse_retry_after,
    read_bounded,
)

API_URL = "https://export.arxiv.org/api/query?x=1"
EPRINT_URL = "https://arxiv.org/e-print/2101.01234v2"


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
        self.calls.append({"url": url, "timeout": timeout, "max_bytes": max_bytes})
        assert self.outcomes, "意外的额外请求：传输被调用了超出脚本的次数"
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class CountingGate:
    """Wrap the real throttle gate and count connection leases."""

    def __init__(self, inner: RateLimitGate) -> None:
        self.inner = inner
        self.transfer_count = 0
        self.max_waits: list[float | None] = []

    def transfer(self, *, max_wait: float | None = None):
        self.transfer_count += 1
        self.max_waits.append(max_wait)
        return self.inner.transfer(max_wait=max_wait)


def ok_response(
    body: bytes = b"ok",
    *,
    status: int = 200,
    headers: dict | None = None,
    final_url: str | None = None,
    url: str = API_URL,
) -> TransportResponse:
    merged = {"content-type": "application/atom+xml"}
    if headers:
        merged.update(headers)
    return TransportResponse(
        status_code=status,
        headers=merged,
        body=body,
        request_url=url,
        final_url=final_url or url,
    )


def redirect_response(
    location: str, *, status: int = 302, url: str = API_URL, headers: dict | None = None
):
    assert status in REDIRECT_STATUSES
    merged = {"location": location}
    if headers:
        merged.update(headers)
    return ok_response(status=status, headers=merged, url=url)


def http_error(status: int, headers: dict | None = None, url: str = API_URL):
    error_class = _error_class_for_status(status)
    return error_class(
        f"HTTP {status}",
        url=url,
        status_code=status,
        headers=headers or {},
        body=b"",
    )


def make_client(
    transport: FakeTransport,
    clock: FakeClock,
    tmp_path: Path,
    *,
    policy: RetryPolicy | None = None,
) -> tuple[ArxivHttpClient, CountingGate]:
    gate = CountingGate(RateLimitGate(tmp_path / "gate.json", clock=clock))
    client = ArxivHttpClient(
        transport=transport, gate=gate, clock=clock, retry_policy=policy
    )
    return client, gate


# ---------------------------------------------------------------- Outbound preflight


@pytest.mark.parametrize(
    "url",
    [
        "http://export.arxiv.org/api/query",  # Non-HTTPS URL.
        "https://export.arxiv.org:8080/api/query",  # Nonstandard port.
        "https://evil.example/download",
        "https://user:pass@export.arxiv.org/api/query",
        "https://export.arxiv.org/api/query#frag",
        "https://export.arxiv.org/api /query",
        "not a url",
        "",
    ],
)
def test_unsafe_outgoing_urls_rejected_before_any_connection(tmp_path, url):
    clock = FakeClock()
    transport = FakeTransport([])
    client, _ = make_client(transport, clock, tmp_path)
    with pytest.raises(ArxivTransportError, match="拒绝"):
        client.get(url)
    assert transport.calls == []


def test_https_default_port_and_subdomains_allowed(tmp_path):
    clock = FakeClock()
    transport = FakeTransport([ok_response(url="https://arxiv.org/e-print/x")])
    client, _ = make_client(transport, clock, tmp_path)
    response = client.get("https://arxiv.org/e-print/x")
    assert response.status_code == 200
    # Explicit port 443 is standard.
    transport2 = FakeTransport([ok_response(url="https://arxiv.org:443/y")])
    client2, _ = make_client(transport2, clock, tmp_path)
    assert client2.get("https://arxiv.org:443/y").status_code == 200


# ---------------------------------------------------------------- Redirects


def test_redirects_are_paced_and_validated_hop_by_hop(tmp_path):
    clock = FakeClock()
    target = "https://export.arxiv.org/api/query?x=2"
    transport = FakeTransport([redirect_response(target), ok_response(url=target)])
    client, gate = make_client(transport, clock, tmp_path)
    response = client.get(API_URL)
    assert response.status_code == 200
    assert [call["url"] for call in transport.calls] == [API_URL, target]
    # Every hop independently uses the throttle gate; unlocked wait steps total at least three seconds between hops.
    assert gate.transfer_count == 2
    assert sum(clock.sleeps) == pytest.approx(3.0)
    assert clock.sleeps and all(step <= WAIT_RECHECK_SECONDS for step in clock.sleeps)


@pytest.mark.parametrize("status", REDIRECT_STATUSES)
def test_all_redirect_statuses_followed(tmp_path, status):
    clock = FakeClock()
    target = "https://arxiv.org/e-print/2101.01234v2"
    transport = FakeTransport(
        [redirect_response(target, status=status), ok_response(url=target)]
    )
    client, _ = make_client(transport, clock, tmp_path)
    assert client.get(EPRINT_URL).status_code == 200


def test_redirect_with_retry_after_persists_cooldown_and_paces_next_hop(tmp_path):
    """Review reproducer: persist Retry-After on a 302 response before releasing the lease.

    A first hop at t=1000 with Retry-After:120 persists cooldown, so the next send
    starts at t>=1120 rather than the old defective 1003.
    """
    clock = FakeClock(start=1000.0)
    target = "https://export.arxiv.org/api/query?x=2"
    transport = FakeTransport(
        [
            redirect_response(target, headers={"retry-after": "120"}),
            ok_response(url=target),
        ]
    )
    client, gate = make_client(transport, clock, tmp_path)
    assert client.get(API_URL).status_code == 200
    assert len(transport.calls) == 2
    fresh = RateLimitGate(tmp_path / "gate.json", clock=FakeClock(1000.0))
    assert (
        fresh.snapshot().cooldown_until == 1120.0
    )  # Visible to other processes and after restart.
    assert clock.now() == pytest.approx(1120.0)  # Actual second-hop send time.
    assert gate.transfer_count == 2


def test_redirect_with_retry_after_http_date_persists(tmp_path):
    clock = FakeClock(start=1000.0)
    date_header = email.utils.formatdate(1010.0, usegmt=True)
    target = "https://export.arxiv.org/api/query?x=2"
    transport = FakeTransport(
        [
            redirect_response(target, headers={"retry-after": date_header}),
            ok_response(url=target),
        ]
    )
    client, _ = make_client(transport, clock, tmp_path)
    assert client.get(API_URL).status_code == 200
    fresh = RateLimitGate(tmp_path / "gate.json", clock=FakeClock(1000.0))
    assert fresh.snapshot().cooldown_until == pytest.approx(1010.0)
    assert clock.now() == pytest.approx(1010.0)


@pytest.mark.parametrize("header", ["soon", "inf", "nan", ""])
def test_redirect_with_malformed_retry_after_uses_normal_pacing(tmp_path, header):
    clock = FakeClock(start=1000.0)
    target = "https://export.arxiv.org/api/query?x=2"
    transport = FakeTransport(
        [
            redirect_response(target, headers={"retry-after": header}),
            ok_response(url=target),
        ]
    )
    client, _ = make_client(transport, clock, tmp_path)
    assert client.get(API_URL).status_code == 200
    fresh = RateLimitGate(tmp_path / "gate.json", clock=FakeClock(1000.0))
    assert fresh.snapshot().cooldown_until is None  # Do not persist malformed values.
    assert clock.now() == pytest.approx(1003.0)  # Ordinary three-second hop spacing.


def test_redirect_with_huge_retry_after_fails_bounded(tmp_path):
    """A redirect cooldown above the per-attempt wait limit raises after persisting
    cooldown and opening only one connection.
    """
    clock = FakeClock(start=1000.0)
    target = "https://export.arxiv.org/api/query?x=2"
    transport = FakeTransport(
        [redirect_response(target, headers={"retry-after": "4000"})]
    )
    client, _ = make_client(transport, clock, tmp_path)
    with pytest.raises(RateLimitError):
        client.get(API_URL)
    assert len(transport.calls) == 1
    fresh = RateLimitGate(tmp_path / "gate.json", clock=FakeClock(1000.0))
    assert fresh.snapshot().cooldown_until == 5000.0


def test_persisted_huge_cooldown_blocks_new_call_before_connection(tmp_path):
    """Previously persisted excessive cooldown rejects a new call within bounds before any
    connection.
    """
    clock = FakeClock(start=1000.0)
    first = FakeTransport([http_error(429, headers={"retry-after": "4000"})])
    client, _ = make_client(first, clock, tmp_path)
    with pytest.raises(ArxivRateLimitedError):
        client.get(API_URL)  # Cooldown 5000 is persisted.
    second = FakeTransport([ok_response()])
    client2, _ = make_client(second, clock, tmp_path)
    with pytest.raises(RateLimitError):
        client2.get(API_URL)
    assert second.calls == []  # No connection was opened.


def test_client_admission_total_budget_bounded_under_repeated_extensions(tmp_path):
    """Client review scenario: repeated shared-cooldown extensions remain bounded by
    RetryPolicy.max_retry_wait as the attempt's total admission budget. Reject before
    connecting and retain persisted cooldown.
    """
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    seed_gate = RateLimitGate(state, clock=clock)
    seed_gate.record_cooldown(1002.0)
    original_sleep = clock.sleep

    def interleaved(seconds: float) -> None:
        original_sleep(seconds)
        other = RateLimitGate(state, clock=clock)
        other.record_cooldown(clock.now() + 2.0)

    clock.sleep = interleaved  # type: ignore[method-assign]
    try:
        transport = FakeTransport([])
        client, gate = make_client(transport, clock, tmp_path)
        with pytest.raises(RateLimitError, match="总等待预算"):
            client.get(API_URL)
        assert transport.calls == []  # No network activity: no connections opened.
        assert gate.max_waits == [
            300.0
        ]  # The client passes the policy limit as the total budget.
        total_slept = sum(clock.sleeps)
    finally:
        clock.sleep = original_sleep
    assert total_slept <= 300.0  # Waiting consumes only the fixed budget.
    fresh = RateLimitGate(state, clock=FakeClock(1000.0))
    assert fresh.snapshot().next_allowed_at is None  # No permit is granted.
    assert (
        fresh.snapshot().cooldown_until > 1299.0
    )  # Extended cooldown remains persisted.


def test_client_late_admission_rejects_before_any_fetch(tmp_path):
    """Client late-admission check: state-reading delay above the policy budget rejects
    before any connection; transport.calls stays empty, cooldown survives, and no permit
    is granted.
    """
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    seed = RateLimitGate(state, clock=clock)
    seed.record_cooldown(1001.0)
    transport = FakeTransport([])  # Any fetch would fail an assertion.
    client, gate = make_client(
        transport, clock, tmp_path, policy=RetryPolicy(max_retry_wait=1.0)
    )
    original_read = gate.inner._read_state_file

    def slow_read():
        clock.sleep(
            0.2
        )  # Simulate state I/O delay and count each read toward the total budget.
        return original_read()

    gate.inner._read_state_file = slow_read  # type: ignore[method-assign]
    try:
        with pytest.raises(RateLimitError, match="签发许可前已超出"):
            client.get(API_URL)
        assert transport.calls == []  # No connection was opened.
        assert gate.max_waits == [
            1.0
        ]  # Pass the policy ceiling as total budget without retrying.
    finally:
        gate.inner._read_state_file = original_read  # type: ignore[method-assign]
    fresh = RateLimitGate(state, clock=FakeClock(1000.0))
    assert fresh.snapshot().next_allowed_at is None
    assert fresh.snapshot().cooldown_until == 1001.0  # Preserve cooldown verbatim.


def test_client_slow_write_beyond_budget_rejects_before_fetch(tmp_path):
    """Client review reproducer: 0.12-second writes under a 0.02-second budget reject
    before any fetch and retain conservative preregistration.
    """
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    transport = FakeTransport([])  # Any fetch would fail an assertion.
    client, gate = make_client(
        transport, clock, tmp_path, policy=RetryPolicy(max_retry_wait=0.02)
    )
    original_write = gate.inner._write_state_file

    def slow_write(payload: dict) -> None:
        clock.sleep(0.12)  # Simulate blocking write/fsync delay.
        original_write(payload)

    gate.inner._write_state_file = slow_write  # type: ignore[method-assign]
    try:
        with pytest.raises(RateLimitError, match="许可预登记写入后已超出"):
            client.get(API_URL)
        assert transport.calls == []  # No connection was opened.
        assert gate.max_waits == [0.02]  # Admission failures are not retried.
    finally:
        gate.inner._write_state_file = original_write  # type: ignore[method-assign]
    fresh = RateLimitGate(state, clock=FakeClock(1000.0))
    snapshot = fresh.snapshot()
    assert snapshot.next_allowed_at == 1003.0  # Retain conservative preregistration.
    assert snapshot.cooldown_until is None
    assert snapshot.in_flight_since == 1000.0  # Retain the in-flight marker too.


def test_two_clients_actual_fetch_spacing_survives_slow_writes(tmp_path):
    """Two clients share gate state with four-second writes; actual fetch starts stay at
    least three seconds apart, using post-write send time rather than pre-write time as
    the reference.
    """
    state = tmp_path / "gate.json"
    clock = FakeClock(start=1000.0)
    fetch_times: list[float] = []

    class RecordingTransport(FakeTransport):
        def fetch(self, url, *, headers=None, timeout=None, max_bytes=None):
            fetch_times.append(clock.now())  # Actual send start times.
            return super().fetch(
                url, headers=headers, timeout=timeout, max_bytes=max_bytes
            )

    transport_a = RecordingTransport([ok_response()])
    transport_b = RecordingTransport([ok_response()])
    client_a, gate_a = make_client(transport_a, clock, tmp_path)
    client_b, _gate_b = make_client(transport_b, clock, tmp_path)
    # Mixed-speed review scenario: the first client has four-second writes; its successor is fast.
    original_write = gate_a.inner._write_state_file

    def slow_write(payload: dict) -> None:
        clock.sleep(4.0)
        original_write(payload)

    gate_a.inner._write_state_file = slow_write  # type: ignore[method-assign]
    try:
        client_a.get(API_URL)
        client_b.get(API_URL)
    finally:
        del gate_a.inner.__dict__[
            "_write_state_file"
        ]  # Restore the original class method.
    assert len(fetch_times) == 2
    assert (
        fetch_times[1] - fetch_times[0] >= MIN_REQUEST_INTERVAL
    )  # At least three seconds.
    fresh = RateLimitGate(state, clock=FakeClock(1000.0))
    snapshot = fresh.snapshot()
    assert snapshot.next_allowed_at >= fetch_times[1] + MIN_REQUEST_INTERVAL
    assert (
        snapshot.in_flight_since is None
    )  # No in-flight marker remains after normal completion.


def test_redirect_limit_error_still_persists_retry_after(tmp_path):
    """Persist redirect-response cooldown even when rejecting an excessive hop count."""
    clock = FakeClock(start=1000.0)
    chain = [
        redirect_response(
            f"https://export.arxiv.org/api/query?hop={n}",
            headers={"retry-after": "50"} if n == 5 else None,
        )
        for n in range(6)
    ]
    transport = FakeTransport(chain)
    client, _ = make_client(transport, clock, tmp_path)
    with pytest.raises(ArxivTransportError, match="重定向跳数"):
        client.get(API_URL)
    fresh = RateLimitGate(tmp_path / "gate.json", clock=FakeClock(1000.0))
    # Hop six at t=1015 with Retry-After:50 persists cooldown 1065.
    assert fresh.snapshot().cooldown_until == 1065.0


def test_final_response_retry_after_persisted(tmp_path):
    """Persist Retry-After from a successful final response too."""
    clock = FakeClock(start=1000.0)
    transport = FakeTransport([ok_response(headers={"retry-after": "30"})])
    client, _ = make_client(transport, clock, tmp_path)
    assert client.get(API_URL).status_code == 200
    fresh = RateLimitGate(tmp_path / "gate.json", clock=FakeClock(1000.0))
    assert fresh.snapshot().cooldown_until == 1030.0


def test_relative_redirect_location_resolved(tmp_path):
    clock = FakeClock()
    transport = FakeTransport(
        [
            redirect_response("/api/query?x=2"),
            ok_response(url="https://export.arxiv.org/api/query?x=2"),
        ]
    )
    client, _ = make_client(transport, clock, tmp_path)
    assert client.get(API_URL).status_code == 200
    assert transport.calls[1]["url"] == "https://export.arxiv.org/api/query?x=2"


@pytest.mark.parametrize(
    "location",
    [
        "http://export.arxiv.org/api/query",  # Downgrade to HTTP.
        "https://evil.example/steal",
        "https://export.arxiv.org:8080/api/query",
        "https://user:pass@export.arxiv.org/api/query",
    ],
)
def test_unsafe_redirect_target_rejected_before_second_connection(tmp_path, location):
    clock = FakeClock()
    transport = FakeTransport([redirect_response(location)])
    client, _ = make_client(transport, clock, tmp_path)
    with pytest.raises(ArxivTransportError, match="拒绝"):
        client.get(API_URL)
    # Only the first hop occurs; reject the external target before connecting.
    assert len(transport.calls) == 1


def test_redirect_without_location_is_rejected(tmp_path):
    clock = FakeClock()
    transport = FakeTransport([ok_response(status=302)])
    client, _ = make_client(transport, clock, tmp_path)
    with pytest.raises(ArxivTransportError, match="缺少 Location"):
        client.get(API_URL)


def test_redirect_hops_are_bounded(tmp_path):
    clock = FakeClock()
    chain = [
        redirect_response(f"https://export.arxiv.org/api/query?hop={n}")
        for n in range(7)
    ]
    transport = FakeTransport(chain)
    client, _ = make_client(transport, clock, tmp_path)
    with pytest.raises(ArxivTransportError, match="重定向跳数超过上限"):
        client.get(API_URL)
    # Reject after six connections: five allowed redirects plus the initial hop.
    assert len(transport.calls) == MAX_REDIRECT_HOPS + 1


def test_redirect_loop_within_limit_still_bounded_by_hops(tmp_path):
    clock = FakeClock()
    transport = FakeTransport([redirect_response(API_URL)] * 6)
    client, _ = make_client(transport, clock, tmp_path)
    with pytest.raises(ArxivTransportError, match="重定向跳数"):
        client.get(API_URL)
    assert len(transport.calls) == MAX_REDIRECT_HOPS + 1


# ---------------------------------------------------------------- Retries and cooldown


def test_success_uses_one_lease_and_returns_response(tmp_path):
    clock = FakeClock()
    transport = FakeTransport([ok_response()])
    client, gate = make_client(transport, clock, tmp_path)
    response = client.get(API_URL)
    assert response.status_code == 200
    assert response.body == b"ok"
    assert len(transport.calls) == 1
    assert gate.transfer_count == 1
    assert clock.sleeps == []


def test_503_retried_with_backoff_then_succeeds(tmp_path):
    clock = FakeClock()
    transport = FakeTransport([http_error(503), ok_response()])
    client, gate = make_client(transport, clock, tmp_path)
    response = client.get(API_URL)
    assert response.status_code == 200
    assert len(transport.calls) == 2
    assert gate.transfer_count == 2
    assert clock.sleeps == [3.0]


def test_429_retry_after_seconds_records_cooldown_and_waits(tmp_path):
    clock = FakeClock()
    transport = FakeTransport(
        [http_error(429, headers={"retry-after": "7"}), ok_response()]
    )
    client, _ = make_client(transport, clock, tmp_path)
    assert client.get(API_URL).status_code == 200
    assert clock.sleeps == [7.0]
    # Shared persistent cooldown is visible to a new gate/process, preventing restart bursts.
    fresh = RateLimitGate(tmp_path / "gate.json", clock=FakeClock(1000.0))
    assert fresh.snapshot().cooldown_until == 1007.0


def test_429_retry_after_http_date_is_respected(tmp_path):
    clock = FakeClock()
    date_header = email.utils.formatdate(1007.0, usegmt=True)
    transport = FakeTransport(
        [http_error(429, headers={"retry-after": date_header}), ok_response()]
    )
    client, _ = make_client(transport, clock, tmp_path)
    assert client.get(API_URL).status_code == 200
    assert clock.sleeps == [7.0]


def test_cooldown_persisted_before_retry_exhausted_single_attempt(tmp_path):
    """Persist Retry-After before raising from the last or only failed attempt."""
    clock = FakeClock()
    transport = FakeTransport([http_error(429, headers={"retry-after": "120"})])
    client, _ = make_client(
        transport, clock, tmp_path, policy=RetryPolicy(max_attempts=1)
    )
    with pytest.raises(ArxivRateLimitedError):
        client.get(API_URL)
    fresh = RateLimitGate(tmp_path / "gate.json", clock=FakeClock(1000.0))
    assert fresh.snapshot().cooldown_until == 1120.0


def test_cooldown_persisted_on_last_attempt_of_bounded_retries(tmp_path):
    clock = FakeClock()
    transport = FakeTransport([http_error(503, headers={"retry-after": "50"})] * 3)
    client, _ = make_client(
        transport, clock, tmp_path
    )  # Default max_attempts is three.
    with pytest.raises(ArxivServerError):
        client.get(API_URL)
    # Register cooldown before deciding whether to retry every failure; the last at t=1100 also leaves
    # cooldown_until=1150 immediately visible to other processes and after restart.
    fresh = RateLimitGate(tmp_path / "gate.json", clock=FakeClock(1000.0))
    assert fresh.snapshot().cooldown_until == 1150.0
    assert clock.sleeps == [50.0, 50.0]


@pytest.mark.parametrize("header", ["soon", "inf", "nan", "-5", ""])
def test_malformed_retry_after_ignored_safely(tmp_path, header):
    clock = FakeClock()
    transport = FakeTransport(
        [http_error(429, headers={"retry-after": header}), ok_response()]
    )
    client, _ = make_client(transport, clock, tmp_path)
    assert client.get(API_URL).status_code == 200
    fresh = RateLimitGate(tmp_path / "gate.json", clock=FakeClock(1000.0))
    assert fresh.snapshot().cooldown_until is None
    assert clock.sleeps == [3.0]  # Ordinary backoff is unaffected.


def test_huge_retry_after_fails_fast_but_persists_cooldown(tmp_path):
    clock = FakeClock()
    transport = FakeTransport([http_error(429, headers={"retry-after": "4000"})])
    client, _ = make_client(transport, clock, tmp_path)
    with pytest.raises(ArxivRateLimitedError):
        client.get(API_URL)
    assert len(transport.calls) == 1
    assert clock.sleeps == []
    fresh = RateLimitGate(tmp_path / "gate.json", clock=FakeClock(1000.0))
    assert fresh.snapshot().cooldown_until == 5000.0


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_never_retry(tmp_path, status):
    clock = FakeClock()
    transport = FakeTransport([http_error(status)])
    client, gate = make_client(transport, clock, tmp_path)
    with pytest.raises(ArxivAuthError):
        client.get(API_URL)
    assert len(transport.calls) == 1
    assert gate.transfer_count == 1
    assert clock.sleeps == []


def test_timeout_exhausts_bounded_attempts(tmp_path):
    clock = FakeClock()
    transport = FakeTransport([ArxivTimeoutError("timeout", url=API_URL)] * 3)
    client, _ = make_client(transport, clock, tmp_path)
    with pytest.raises(ArxivTimeoutError):
        client.get(API_URL)
    assert len(transport.calls) == 3
    assert clock.sleeps == [3.0, 6.0]


def test_backoff_progression_is_bounded_and_exponential(tmp_path):
    clock = FakeClock()
    transport = FakeTransport(
        [http_error(503), http_error(503), http_error(503), ok_response()]
    )
    client, _ = make_client(
        transport, clock, tmp_path, policy=RetryPolicy(max_attempts=4)
    )
    assert client.get(API_URL).status_code == 200
    assert clock.sleeps == [3.0, 6.0, 12.0]


def test_response_size_error_is_not_retried(tmp_path):
    clock = FakeClock()
    transport = FakeTransport([ArxivResponseSizeError("too big", url=API_URL)])
    client, _ = make_client(transport, clock, tmp_path)
    with pytest.raises(ArxivResponseSizeError):
        client.get(API_URL, max_bytes=10)
    assert len(transport.calls) == 1


def test_short_transfer_retried_as_connection_error_then_succeeds(tmp_path):
    """Handle transport short reads as retryable transport errors."""
    clock = FakeClock()
    transport = FakeTransport(
        [
            ArxivConnectionError(
                "传输长度与声明不符：声明 20 字节，实际读到 3 字节", url=EPRINT_URL
            ),
            ok_response(url=EPRINT_URL),
        ]
    )
    client, _ = make_client(transport, clock, tmp_path)
    assert client.get(EPRINT_URL).status_code == 200
    assert len(transport.calls) == 2
    assert clock.sleeps == [3.0]


# ---------------------------------------------------------------- Retry policy and parsing


def test_retry_policy_validation():
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=11)
    with pytest.raises(ValueError):
        RetryPolicy(backoff_base=61.0, backoff_cap=60.0)
    with pytest.raises(ValueError):
        RetryPolicy(max_retry_wait=0)
    with pytest.raises(ValueError):
        RetryPolicy().backoff_seconds(0)
    policy = RetryPolicy()
    assert [policy.backoff_seconds(n) for n in (1, 2, 3, 4, 5)] == [
        3.0,
        6.0,
        12.0,
        24.0,
        48.0,
    ]
    capped = RetryPolicy(backoff_base=40.0, backoff_cap=60.0)
    assert capped.backoff_seconds(2) == 60.0


def test_parse_retry_after_forms():
    now = 1000.0
    assert parse_retry_after(None, now=now) is None
    assert parse_retry_after("", now=now) is None
    assert parse_retry_after("abc", now=now) is None
    assert parse_retry_after("inf", now=now) is None
    assert parse_retry_after("nan", now=now) is None
    assert parse_retry_after("0", now=now) == 0.0
    assert parse_retry_after("120", now=now) == 120.0
    assert (
        parse_retry_after("-5", now=now) == 0.0
    )  # Negative values are expired and must not extend cooldown.
    future = email.utils.formatdate(now + 30.0, usegmt=True)
    assert parse_retry_after(future, now=now) == pytest.approx(30.0)
    past = email.utils.formatdate(now - 3600.0, usegmt=True)
    assert parse_retry_after(past, now=now) == 0.0


def test_http_error_class_mapping():
    assert _error_class_for_status(401) is ArxivAuthError
    assert _error_class_for_status(403) is ArxivAuthError
    assert _error_class_for_status(429) is ArxivRateLimitedError
    assert _error_class_for_status(500) is ArxivServerError
    assert _error_class_for_status(503) is ArxivServerError
    assert _error_class_for_status(404) is ArxivHttpError


# ---------------------------------------------------------------- Response integrity


def test_read_bounded_returns_exact_body():
    stream = BytesIO(b"abcdef")
    assert read_bounded(stream, url=API_URL, max_bytes=10) == b"abcdef"
    assert (
        read_bounded(BytesIO(b"abc"), url=API_URL, max_bytes=10, content_length=3)
        == b"abc"
    )


def test_read_bounded_rejects_oversized_body():
    stream = BytesIO(b"x" * 11)
    with pytest.raises(ArxivResponseSizeError):
        read_bounded(stream, url=API_URL, max_bytes=10)


def test_read_bounded_rejects_declared_length_without_reading():
    stream = BytesIO(b"x" * 4096)
    with pytest.raises(ArxivResponseSizeError):
        read_bounded(stream, url=API_URL, max_bytes=10, content_length=4096)
    assert stream.tell() == 0  # Fail before reading any bytes.


@pytest.mark.parametrize(
    "body,declared",
    [
        (b"abc", 20),  # Short read: 20 declared, three received.
        (b"abcd", 3),  # Excess read: three declared, four received.
        (b"", 20),  # Empty stream with a nonzero declared length.
    ],
)
def test_read_bounded_rejects_length_mismatch(body, declared):
    with pytest.raises(ArxivConnectionError, match="长度与声明不符"):
        read_bounded(BytesIO(body), url=API_URL, max_bytes=100, content_length=declared)


def test_read_bounded_requires_positive_limit():
    with pytest.raises(ValueError):
        read_bounded(BytesIO(b""), url=API_URL, max_bytes=0)


# ------------------------------------------- Real transport boundary with an offline injected opener


class _FakeHTTPResponse:
    """Fake http.client response for offline UrllibTransport injection."""

    def __init__(
        self, body: bytes, headers: dict, *, status: int = 200, url: str = API_URL
    ):
        self._stream = BytesIO(body)
        self.headers = email.message.Message()
        for key, value in headers.items():
            self.headers[key] = value
        self.status = status
        self._url = url

    def geturl(self) -> str:
        return self._url

    def getcode(self) -> int:
        return self.status

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def close(self) -> None:
        self._stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class _FakeOpener:
    def __init__(self, response) -> None:
        self._response = response

    def open(self, request, timeout=None):
        return self._response


class _RaisingOpener:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    def open(self, request, timeout=None):
        raise self._error


def test_urllib_transport_enforces_declared_length_at_boundary():
    """Reject short reads of three bytes when 20 are declared at the real transport
    boundary.
    """
    transport = UrllibTransport()
    transport._opener = _FakeOpener(
        _FakeHTTPResponse(
            b"abc",
            {"Content-Type": "application/atom+xml", "Content-Length": "20"},
        )
    )
    with pytest.raises(ArxivConnectionError, match="长度与声明不符"):
        transport.fetch(API_URL)


def test_urllib_transport_accepts_consistent_declared_length():
    transport = UrllibTransport()
    transport._opener = _FakeOpener(
        _FakeHTTPResponse(
            b"x" * 20,
            {"Content-Type": "application/atom+xml", "Content-Length": "20"},
        )
    )
    response = transport.fetch(API_URL)
    assert response.body == b"x" * 20
    assert response.status_code == 200


def test_urllib_transport_returns_redirect_without_following():
    """Return 3xx responses unchanged, including Location, without following redirects;
    close the body.
    """
    import urllib.error

    target = "https://export.arxiv.org/api/query?x=2"
    body = BytesIO(b"")
    error = urllib.error.HTTPError(API_URL, 302, "Found", {"Location": target}, body)
    transport = UrllibTransport()
    transport._opener = _RaisingOpener(error)
    response = transport.fetch(API_URL)
    assert response.status_code == 302
    assert response.headers["location"] == target
    assert response.body == b""
    assert body.closed  # Close error/redirect bodies reliably before returning.


def test_urllib_transport_closes_http_error_body():
    import urllib.error

    body = BytesIO(b"forbidden")
    error = urllib.error.HTTPError(
        API_URL, 403, "Forbidden", {"Retry-After": "5"}, body
    )
    transport = UrllibTransport()
    transport._opener = _RaisingOpener(error)
    with pytest.raises(ArxivAuthError) as excinfo:
        transport.fetch(API_URL)
    assert excinfo.value.status_code == 403
    assert excinfo.value.headers["retry-after"] == "5"
    assert body.closed  # Close error bodies reliably before raising.


# ---------------------------------------------------------------- Production defaults


def test_production_defaults_are_safe(tmp_path):
    assert MIN_REQUEST_INTERVAL == 3.0
    assert ARXIV_ALLOWED_HOSTS == ("arxiv.org", "export.arxiv.org")
    assert MAX_REDIRECT_HOPS == 5
    assert REDIRECT_STATUSES == (301, 302, 303, 307, 308)
    transport = UrllibTransport()
    assert transport.default_timeout == DEFAULT_TIMEOUT == 60.0
    assert transport.default_max_bytes == DEFAULT_MAX_RESPONSE_BYTES == 64 * 1024 * 1024
    assert USER_AGENT.startswith("hvs-catalog/")
    policy = RetryPolicy()
    assert policy.max_attempts == 3
    assert policy.max_retry_wait == 300.0


def test_default_gate_state_path_is_outside_workspaces(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("HVS_CATALOG_ARXIV_RATE_STATE", raising=False)
    path = default_rate_gate_state_path()
    assert "arxiv-rate-gate.json" in path.name
    assert "hvs-catalog" in path.parts
    assert tmp_path in path.parents
    assert Path.cwd().resolve() not in path.resolve().parents


def test_default_gate_state_path_env_override(monkeypatch, tmp_path):
    override = tmp_path / "custom-state.json"
    monkeypatch.setenv("HVS_CATALOG_ARXIV_RATE_STATE", str(override))
    assert default_rate_gate_state_path() == override


def test_default_client_constructs_without_touching_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("HVS_CATALOG_ARXIV_RATE_STATE", raising=False)
    client = default_arxiv_client()
    assert not client.gate.state_path.exists()
    assert not list(tmp_path.rglob("arxiv-rate-gate.json"))


class _CapturingOpener:
    """Opener that captures request objects to verify constructed headers."""

    def __init__(self, response) -> None:
        self._response = response
        self.captured = None

    def open(self, request, timeout=None):
        self.captured = request
        return self._response


def test_urllib_transport_builds_user_agent_with_caller_headers():
    """Merge caller headers, including Accept/encoding declarations, over the transport's
    default User-Agent.
    """
    transport = UrllibTransport()
    opener = _CapturingOpener(
        _FakeHTTPResponse(b"x", {"Content-Type": "application/gzip"})
    )
    transport._opener = opener
    transport.fetch(
        API_URL,
        headers={
            "Accept": "application/x-eprint-tar, */*",
            "Accept-Encoding": "identity",
        },
    )
    assert opener.captured is not None
    assert opener.captured.get_header("User-agent") == USER_AGENT
    assert opener.captured.get_header("Accept") == "application/x-eprint-tar, */*"
    assert opener.captured.get_header("Accept-encoding") == "identity"
