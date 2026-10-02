"""Shared quotas across actual processes and models, plus the 100-slot boundary."""

import json
import subprocess
import sys
import time

import pytest
from llm_helpers import endpoint

from hvs_catalog.llm import RuntimeConfig
from hvs_catalog.llm.quota import AccountQuota

pytestmark = pytest.mark.integration


def test_default_100_slots_and_next_request_waits(tmp_path):
    quota = AccountQuota(RuntimeConfig(quota_store=tmp_path / "quota.db"))
    leases = [quota.acquire() for _ in range(100)]
    with pytest.raises(TimeoutError):
        quota.acquire(timeout=0.03)
    quota.release(leases.pop())
    leases.append(quota.acquire(timeout=0.1))
    for lease in leases:
        quota.release(lease)


def test_shared_models_and_processes_hold_stream_permits(tmp_path):
    code = """
import sys,json
from pathlib import Path
from pydantic import SecretStr
from hvs_catalog.llm import ModelConfig,RuntimeConfig,TokenDanceAdapter
from hvs_catalog.cli_llm import synthetic_request
from hvs_catalog.llm.quota import AccountQuota
cfg=RuntimeConfig(base_url=sys.argv[1],quota_store=Path(sys.argv[2]),concurrency=2,total_timeout=20)
quota=AccountQuota(cfg)
adapter=TokenDanceAdapter()
req=synthetic_request(ModelConfig(model=sys.argv[3]))
with quota.permit():
    result=adapter.parse(adapter.send(adapter.prepare(req)['payload'],cfg,SecretStr('local-test-key')),req)
print(json.dumps(result['parsed']))
"""
    models = [
        "deepseek-v4.1-flash",
        "glm-5.3-flash",
        "mimo-v2.6-flash",
        "qwen3.8-flash",
    ]
    with endpoint(lambda *_: {"delay": 0.25}) as (url, seen, active):
        children = [
            subprocess.Popen(
                [sys.executable, "-c", code, url, str(tmp_path / "quota.db"), model],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for model in models
        ]
        for child in children:
            stdout, stderr = child.communicate(timeout=40)
            assert child.returncode == 0, stderr
            assert json.loads(stdout) == {"id": "SYN-1", "value": 42}
        assert len(seen) == 4 and active["peak"] == 2


def test_rate_and_cooldown_are_account_shared(tmp_path):
    cfg = RuntimeConfig(quota_store=tmp_path / "quota.db", rpm=1)
    first = AccountQuota(cfg)
    lease = first.acquire()
    first.release(lease)
    second = AccountQuota(cfg)
    with pytest.raises(TimeoutError):
        second.acquire(timeout=0.03)
    with pytest.raises(ValueError):
        AccountQuota(RuntimeConfig(quota_store=tmp_path / "quota.db", rpm=2))
    other = AccountQuota(RuntimeConfig(quota_store=tmp_path / "other.db"))
    other.cooldown(0.1)
    start = time.monotonic()
    lease = other.acquire(timeout=1)
    assert time.monotonic() - start >= 0.08
    other.release(lease)
