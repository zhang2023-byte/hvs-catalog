"""Development budget arithmetic and attribution never invent billed costs."""

import json
from typing import ClassVar

import probe_tokendance as probe
import pytest
from pydantic import SecretStr

from hvs_catalog.cli_llm import synthetic_request
from hvs_catalog.llm import ModelConfig, RuntimeConfig


def test_initial_twelve_conservative_reservations_fit_twenty_rmb():
    rates = {
        "deepseek-v4.1-flash": ("2", "8"),
        "glm-5.3-flash": ("0.8", "2.8"),
        "mimo-v2.6-flash": ("1", "2"),
        "qwen3.8-flash": ("0.8", "2.7"),
    }
    total = 0
    for model, (input_rate, output_rate) in rates.items():
        for case in ("plain", "tool", "maximum"):
            config = ModelConfig(model=model, stream=case != "plain")
            if case != "maximum":
                values = dict(config.parameters)
                values[probe.PROFILES[model].output_parameter] = 4096
                config = ModelConfig(
                    model=model, parameters=values, stream=case != "plain"
                )
            total += probe.reserve_micro(
                synthetic_request(config, structured=case != "plain"),
                RuntimeConfig(),
                {
                    "rates_rmb_per_million": {
                        "input_tokens": input_rate,
                        "completion_tokens": output_rate,
                    }
                },
            )
    assert 15_000_000 < total < 20_000_000


def test_unmatched_or_uncertain_cost_keeps_reservation():
    report = {
        "groups": [
            {"cost_micro": None, "reserved_micro": 4_000_000},
            {"cost_micro": 100, "reserved_micro": 4_000_000},
            {
                "cost_micro": 100,
                "reserved_micro": 4_000_000,
                "completion_unknown": True,
            },
        ]
    }
    assert probe.used_micro(report) == 8_000_100


@pytest.mark.parametrize("recover", [True, False])
def test_portal_read_throttle_has_bounded_retries_and_retry_after(monkeypatch, recover):
    delays = []
    monkeypatch.setattr(probe.time, "sleep", delays.append)
    portal = probe.Portal(SecretStr("synthetic-portal-key"))
    calls = []

    class Reply:
        headers: ClassVar[dict[str, str]] = {"Retry-After": "0.25"}

        def __init__(self, status):
            self.status_code = status

        def close(self):
            pass

        def json(self):
            return {"items": []}

    def get(*args, **kwargs):
        calls.append(kwargs)
        return Reply(200 if recover and len(calls) == 2 else 429)

    monkeypatch.setattr(portal._session, "get", get)
    if recover:
        assert portal.get("/v1/usage", authenticated=True) == {"items": []}
        assert len(calls) == 2
    else:
        with pytest.raises(RuntimeError, match="HTTP 429"):
            portal.get("/v1/usage", authenticated=True)
        assert len(calls) == 3
    assert 0.25 in delays


@pytest.mark.parametrize(
    "trace,provider,expected",
    [
        ("our-trace", "DeepSeek", 7687),
        ("other-trace", "DeepSeek", None),
        ("our-trace", "Other", "error"),
    ],
)
def test_bill_requires_exact_trace_and_official_provider(
    monkeypatch, trace, provider, expected
):
    monkeypatch.setattr(probe.time, "sleep", lambda _: None)

    class Portal:
        def get(self, *args, **kwargs):
            return {
                "items": [
                    {
                        "id": "usage-1",
                        "trace_id": trace,
                        "model_id": "model-1",
                        "provider_name": provider,
                        "cost": 7687,
                    }
                ]
            }

    class Space:
        def read_file(self, ref):
            return json.dumps(
                {"exchange": {"headers": {"x-trace-id": "our-trace"}}, "result": {}}
            ).encode()

    report = {
        "attempts": [{"response": {"path": "responses/one.json", "sha256": "a" * 64}}]
    }
    if expected == "error":
        with pytest.raises(RuntimeError):
            probe.confirm_bill(
                Portal(),
                "baseline",
                report,
                Space(),
                {"model_id": "model-1", "provider_name": "DeepSeek"},
            )
    else:
        cost, _ = probe.confirm_bill(
            Portal(),
            "baseline",
            report,
            Space(),
            {"model_id": "model-1", "provider_name": "DeepSeek"},
        )
        assert cost == expected
