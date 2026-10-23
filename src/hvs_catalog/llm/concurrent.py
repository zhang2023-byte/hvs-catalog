"""Network-only workers with archival callbacks on the coordinator thread."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from ..records.response import apply_response_policy
from .quota import AccountQuota
from .transport import OutputFailure, TransportFailure, network_retry_allowed


class ConcurrentCalls:
    """Keep business connections and all archival writes on one owning thread."""

    def __init__(self, concurrency):
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError("concurrency must be a positive integer")
        self.owner = threading.get_ident()
        self.pool = ThreadPoolExecutor(max_workers=concurrency)
        self.jobs = {}
        self.completed = set()
        self.peak = 0
        self.stop_reason = None

    def _owner(self):
        if threading.get_ident() != self.owner:
            raise RuntimeError("business coordination must stay on its owning thread")

    def submit(self, key, service, request, prepared, archive):
        self._owner()
        if key in self.jobs:
            raise ValueError("an identical logical call is already in flight")
        credential = service.credential()
        self.jobs[key] = (
            self.pool.submit(self._send, service, request, prepared, credential),
            archive,
        )
        self.peak = max(self.peak, len(self.jobs))

    def _send(self, service, request, prepared, credential):
        if self.stop_reason is not None:
            return {
                "exchange": {},
                "result": None,
                "error": self.stop_reason,
                "uncertain": False,
                "retryable": False,
            }
        try:
            quota = AccountQuota(service.runtime)
            lease = quota.acquire()
        except TimeoutError:
            return {
                "exchange": {},
                "result": None,
                "error": "quota_wait_expired",
                "uncertain": False,
                "retryable": False,
            }
        except (OSError, sqlite3.Error, ValueError) as exc:
            # Quota setup precedes adapter.send: no request reached the network.
            return {
                "exchange": {
                    "transport": {
                        "phase": "quota",
                        "exceptions": [{"type": type(exc).__name__}],
                    }
                },
                "result": None,
                "error": "quota_unavailable",
                "uncertain": False,
                "retryable": False,
            }
        try:
            try:
                exchange = service.adapter.send(
                    prepared["payload"], service.runtime, credential
                )
                service.hook("after_response")
                try:
                    apply_response_policy(exchange, prepared)
                    result = service.adapter.parse(exchange, request)
                    return {
                        "exchange": exchange,
                        "result": result,
                        "error": None,
                        "uncertain": False,
                        "retryable": False,
                    }
                except (
                    OutputFailure,
                    ValueError,
                    TypeError,
                    KeyError,
                    AttributeError,
                ) as exc:
                    kind = (
                        str(exc)
                        if isinstance(exc, OutputFailure)
                        else "invalid_response"
                    )
                    return {
                        "exchange": exchange,
                        "result": None,
                        "error": kind,
                        "uncertain": False,
                        "retryable": False,
                    }
            except TransportFailure as exc:
                envelope = {
                    "exchange": exc.exchange,
                    "result": None,
                    "error": exc.kind,
                    "uncertain": exc.uncertain,
                    "retryable": exc.retryable,
                    "retry_after": exc.retry_after,
                }
                if network_retry_allowed(envelope, service.runtime):
                    quota.cooldown(
                        exc.retry_after if exc.retry_after is not None else 1
                    )
                return envelope
        finally:
            quota.release(lease)

    def collect(self, *, wait_for_result=True):
        self._owner()
        if wait_for_result and self.jobs:
            wait(
                [job[0] for job in self.jobs.values()],
                timeout=1,
                return_when=FIRST_COMPLETED,
            )
        ready = []
        for key, (future, archive) in list(self.jobs.items()):
            if future.done():
                try:
                    envelope = future.result()
                except Exception as exc:  # noqa: BLE001 - preserve uncertain external completion
                    # Unexpected failures retain the durable pending request. The
                    # caller cannot infer whether the service received it.
                    envelope = {
                        "exchange": {},
                        "result": None,
                        "error": "worker_failure:" + type(exc).__name__,
                        "uncertain": True,
                        "retryable": False,
                    }
                if envelope.get("error") in {
                    "budget_insufficient",
                    "authentication",
                    "http_401",
                    "http_403",
                } or (envelope.get("exchange") or {}).get("status") in {401, 403}:
                    self.stop_reason = envelope["error"]
                archive(envelope)
                self.completed.add(key)
                del self.jobs[key]
                ready.append(key)
        return ready

    def close(self):
        self._owner()
        try:
            while self.jobs:
                self.collect()
        finally:
            self.pool.shutdown(wait=True)
