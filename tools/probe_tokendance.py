"""Authorized R12 synthetic connectivity verification and separate RMB budget.

Not a product billing feature. Uses pinned official-route public prices and only
confirms costs from trace-matched platform usage records. Unknown costs retain
their conservative reservation. Never prints keys or unrelated account records.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

import requests

from hvs_catalog.cli_llm import synthetic_request
from hvs_catalog.llm import (
    PROFILES,
    ModelConfig,
    RuntimeConfig,
    TokenDanceAdapter,
    load_credential,
    resume_call,
    start_call,
)
from hvs_catalog.llm.transport import _retry_after
from hvs_catalog.records.common import ArtifactRef
from hvs_catalog.workspace import initialize_workspace

BUDGET_MICRO = 20_000_000
MAX_GROUPS = 24


def publish(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


class Portal:
    def __init__(self, credential):
        self._key = credential
        self._session = requests.Session()
        self._session.trust_env = False
        self._last_usage = 0

    def get(self, path, *, params=None, authenticated=False):
        headers = (
            {"Authorization": "Bearer " + self._key.get_secret_value()}
            if authenticated
            else {}
        )
        for index in range(3):
            if path.endswith("/usage"):
                # Space completed reads; network latency and other account clients
                # can otherwise collide with the portal's one-read-per-second gate.
                time.sleep(max(0, 1.1 - (time.monotonic() - self._last_usage)))
            response = self._session.get(
                "https://tokendance.space/portal/api" + path,
                params=params,
                headers=headers,
                timeout=30,
                allow_redirects=False,
            )
            self._last_usage = time.monotonic()
            if response.status_code == 429 and index < 2:
                delay = _retry_after(response.headers.get("Retry-After"))
                response.close()
                time.sleep(delay if delay is not None else 2**index)
                continue
            if response.status_code != 200:
                raise RuntimeError(f"portal read refused: HTTP {response.status_code}")
            return response.json()
        raise AssertionError("unreachable")

    def price(self, model):
        metadata = self.get(f"/models/{model}/endpoints/stats")
        profile = PROFILES[model]
        endpoint = next(
            p for p in metadata["endpoints"] if p["slug"] == profile.provider
        )
        pricing = endpoint["pricing"]
        collections = [pricing["items"]]
        timing = pricing.get("time_pricing") or {}
        if timing:
            collections.append(timing.get("base_items", []))
            collections.extend(option["items"] for option in timing.get("options", []))
        rates = {}
        for items in collections:
            for item in items:
                if not item["id"].startswith("openai:chat-completions:"):
                    continue
                for plan in item["plans"]:
                    if plan["unit"] != "millionTokens":
                        raise ValueError("unsupported price unit")
                    rate = Decimal(plan["rate"])
                    if not rate.is_finite() or rate < 0:
                        raise ValueError("invalid price")
                    suffix = item["id"].split(":")[-1]
                    rates[suffix] = max(rates.get(suffix, rate), rate)
        if not {"input_tokens", "completion_tokens"} <= rates.keys():
            raise ValueError("incomplete official route price")
        return {
            "model_id": metadata["model_id"],
            "provider": profile.provider,
            "provider_name": endpoint["display_name"],
            "checked_at": datetime.now(UTC).isoformat(),
            "rates_rmb_per_million": {k: str(v) for k, v in rates.items()},
        }


def reserve_micro(request, runtime, price):
    payload = TokenDanceAdapter().prepare(request)["payload"]
    # UTF-8 bytes plus explicit overhead is conservative for these short English
    # synthetic messages/tool schemas; it is not a general tokenizer service.
    input_bound = len(json.dumps(payload, ensure_ascii=False).encode()) + 4096
    profile = PROFILES[request.model.model]
    output_bound = payload.get(profile.output_parameter, profile.maximum_output)
    if profile.provider == "bigmodel":
        output_bound = max(output_bound, profile.maximum_output)
    if profile.provider == "alibaba" and payload.get("enable_thinking", True):
        output_bound += payload.get("thinking_budget", 262144)
    rates = price["rates_rmb_per_million"]
    amount = Decimal(input_bound) * Decimal(rates["input_tokens"]) + Decimal(
        output_bound
    ) * Decimal(rates["completion_tokens"])
    return int(
        (amount * runtime.max_attempts).to_integral_value(rounding=ROUND_CEILING)
    )


def used_micro(report):
    return sum(
        g["cost_micro"]
        if g["cost_micro"] is not None and not g.get("completion_unknown", False)
        else g["reserved_micro"]
        for g in report["groups"]
    )


def confirm_bill(portal, baseline, call_report, space, price):
    ids = []
    for attempt in call_report["attempts"]:
        if not attempt["response"]:
            return None, []
        envelope = json.loads(
            space.read_file(ArtifactRef.model_validate(attempt["response"]))
        )
        exchange = envelope.get("exchange") or {}
        headers = exchange.get("headers") or {}
        result = envelope.get("result") or {}
        candidates = {
            v
            for v in (
                result.get("trace_id"),
                result.get("service_request_id"),
                headers.get("x-trace-id"),
                headers.get("trace-id"),
                headers.get("x-request-id"),
            )
            if v
        }
        ids.append(candidates)
    if not ids or any(not values for values in ids):
        return None, []
    # start_id is an older-page cursor, not a lower bound for new usage. A recent
    # bounded read plus exact trace matching cannot attribute unrelated calls.
    # If usage has aged out of this window, retain the reservation as unknown.
    params = {"limit": 500, "model_id": price["model_id"], "window": "1h"}
    for _ in range(3):
        rows = portal.get("/v1/usage", params=params, authenticated=True)["items"]
        matched = []
        for candidates in ids:
            candidates_rows = [
                r
                for r in rows
                if r.get("id") != baseline
                and (r.get("trace_id") in candidates or r.get("id") in candidates)
            ]
            if len(candidates_rows) != 1:
                break
            row = candidates_rows[0]
            if (
                row.get("model_id") != price["model_id"]
                or row.get("provider_name") != price["provider_name"]
            ):
                raise RuntimeError(
                    "billed model/provider differs from the pinned official route"
                )
            if type(row.get("cost")) is not int or row["cost"] < 0:
                break
            matched.append(
                {
                    k: row.get(k)
                    for k in (
                        "id",
                        "trace_id",
                        "model_id",
                        "provider_name",
                        "cost",
                        "pricing_version",
                        "input_tokens",
                        "output_tokens",
                        "cached_tokens",
                    )
                }
            )
        if len(matched) == len(ids) and len({row["id"] for row in matched}) == len(
            matched
        ):
            return sum(row["cost"] for row in matched), matched
        time.sleep(1.1)
    return None, []


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--space", type=Path, required=True)
    parser.add_argument("--quota-store", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--models", nargs="+", choices=list(PROFILES), default=list(PROFILES)
    )
    parser.add_argument(
        "--cases",
        nargs="+",
        choices=("plain", "tool", "maximum"),
        default=("plain", "tool", "maximum"),
    )
    args = parser.parse_args(argv)
    if args.report.resolve().is_relative_to(args.space.resolve()):
        raise ValueError("budget report must stay outside the product workspace")
    runtime = RuntimeConfig(quota_store=args.quota_store.resolve())
    credential = load_credential(runtime, env_file=args.env_file)
    portal = Portal(credential)
    if args.report.exists():
        report = json.loads(args.report.read_text())
        if report["budget_micro"] != BUDGET_MICRO or len(report["groups"]) > MAX_GROUPS:
            raise ValueError("verification budget state invalid")
    else:
        items = portal.get("/v1/usage", params={"limit": 1}, authenticated=True)[
            "items"
        ]
        report = {
            "budget_micro": BUDGET_MICRO,
            "baseline_id": items[0]["id"] if items else None,
            "groups": [],
        }
        publish(args.report, report)
    space = initialize_workspace(args.space)
    try:
        for model in args.models:
            for case in args.cases:
                if len(report["groups"]) >= MAX_GROUPS:
                    raise RuntimeError("24-group verification limit reached")
                price = portal.price(model)
                config = ModelConfig(model=model, stream=case != "plain")
                if case != "maximum":
                    values = dict(config.parameters)
                    values[PROFILES[model].output_parameter] = 4096
                    config = ModelConfig(
                        model=model, parameters=values, stream=case != "plain"
                    )
                request = synthetic_request(config, structured=case != "plain")
                reservation = reserve_micro(request, runtime, price)
                if used_micro(report) + reservation > BUDGET_MICRO:
                    raise RuntimeError(
                        "20 RMB reservation budget would be exceeded; no new request sent"
                    )
                group = {
                    "model": model,
                    "case": case,
                    "reserved_micro": reservation,
                    "cost_micro": None,
                    "price": price,
                    "state": "reserved",
                    "execution_id": None,
                    "bills": [],
                }
                report["groups"].append(group)
                publish(args.report, report)
                key = f"r12:{model}:{case}:{len(report['groups'])}"
                handle = start_call(
                    space, request, runtime, request_key=key, credential=credential
                )
                group["execution_id"] = handle.execution_id
                publish(args.report, report)
                result = resume_call(space, handle.execution_id, credential=credential)
                group["state"] = result["state"]
                group["attempt_count"] = result["attempt_count"]
                group["completion_unknown"] = any(
                    a["state"] == "pending" for a in result["attempts"]
                )
                group["error_kinds"] = sorted({e["kind"] for e in result["errors"]})
                group["cost_micro"], group["bills"] = confirm_bill(
                    portal, report["baseline_id"], result, space, price
                )
                if result["state"] == "finished":
                    actual = result["response"]["result"]
                    group["output_valid"] = (
                        actual["parsed"] == {"id": "SYN-1", "value": 42}
                        if case != "plain"
                        else actual["content"].strip() == "SYN-1=42"
                    )
                    group["returned_model"] = actual["returned_model"]
                    group["usage"] = actual["usage"]
                publish(args.report, report)
                print(
                    json.dumps(
                        {
                            "model": model,
                            "case": case,
                            "state": group["state"],
                            "output_valid": group.get("output_valid"),
                            "attempts": group["attempt_count"],
                            "cost_micro": group["cost_micro"],
                            "reserved_or_billed_micro": used_micro(report),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
    finally:
        space.close()
    latest = {(g["model"], g["case"]): g for g in report["groups"]}
    return (
        0
        if all(
            g["state"] == "finished" and g.get("output_valid") for g in latest.values()
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
