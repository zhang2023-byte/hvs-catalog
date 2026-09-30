"""New runs omit monetary fields; historical valued scientific run fields remain readable.

Retired ledger rejection is covered by test_demo_service, with byte preservation.
"""

from __future__ import annotations

import json

from hvs_catalog.pipeline import (
    PAPER_B,
    ExtractionRequest,
    StepModel,
    import_mock_source,
    resume_extraction,
    start_extraction,
)
from hvs_catalog.pipeline.mock import PipelineMockService
from hvs_catalog.records.serialize import parse_record, record_to_json
from hvs_catalog.workspace import initialize_workspace


def test_usage_omits_unfilled_money_and_keeps_values():
    from hvs_catalog.records.runs import Attempt, Usage

    fresh = Usage(tokens=5)
    payload = fresh.model_dump(mode="json")
    assert payload == {"tokens": 5}
    assert "cost" not in payload and "currency" not in payload
    # 显式 null（旧数据的写法）也能解析，序列化同样不输出
    explicit_null = Usage.model_validate(
        {"tokens": None, "cost": None, "currency": None}
    )
    assert explicit_null.model_dump(mode="json") == {"tokens": None}
    # 历史有值字段保留并可再输出
    valued = Usage.model_validate({"tokens": 3, "cost": 1.5, "currency": "USD"})
    assert valued.model_dump(mode="json") == {
        "tokens": 3,
        "cost": 1.5,
        "currency": "USD",
    }
    attempt = Attempt.model_validate(
        {
            "request": None,
            "response": None,
            "state": "failed",
            "started_at": None,
            "finished_at": None,
            "usage": None,
            "cost": 0.5,
            "currency": "EUR",
            "service_request_id": None,
        }
    )
    assert attempt.model_dump(mode="json")["cost"] == 0.5
    assert "currency" in attempt.model_dump(mode="json")


def test_old_run_record_with_costs_still_parses():
    payload = {
        "schema": {"name": "run", "version": 1},
        "record_id": "run-legacy-1",
        "revision": 1,
        "created_at": "2026-01-01T00:00:00+00:00",
        "execution_id": "exec-legacy-1",
        "data": {
            "execution_id": "exec-legacy-1",
            "mode": "synthetic",
            "input_snapshot": {
                "source_refs": [],
                "rules": {"version": "0.1.0", "sha256": "a" * 64},
                "method": "direct",
                "models": [],
                "code_version": "legacy",
                "dependency_versions": {},
                "expert_adjudication": True,
                "prompt_refs": [],
                "example_refs": [],
                "precedent_refs": [],
                "existing_decision_refs": [],
            },
            "state": "failed",
            "steps": [
                {
                    "step_id": "extract_roster",
                    "state": "failed",
                    "input_refs": [],
                    "attempts": [
                        {
                            "request": None,
                            "response": None,
                            "state": "failed",
                            "started_at": "2026-01-01T00:00:01+00:00",
                            "finished_at": "2026-01-01T00:00:02+00:00",
                            "usage": {
                                "tokens": 10,
                                "cost": 0.5,
                                "currency": "USD",
                            },
                            "cost": 0.5,
                            "currency": "USD",
                            "service_request_id": "legacy-1",
                        }
                    ],
                    "result_refs": [],
                    "error_refs": ["e1"],
                }
            ],
            "result_refs": [],
            "model_judgments": [],
            "usage": {"tokens": 10, "cost": 0.5, "currency": "USD"},
            "errors": [
                {"error_id": "e1", "kind": "service", "message": "legacy failure"}
            ],
        },
    }
    record = parse_record(payload)
    text = record_to_json(record)
    # 历史有值费用照常读取与输出
    assert '"cost": 0.5' in text and '"currency": "USD"' in text
    assert record.data.usage.cost == 0.5
    assert record.data.steps[0].attempts[0].usage.cost == 0.5


def test_new_extraction_run_records_contain_no_money(tmp_path):
    space = initialize_workspace(tmp_path / "space")
    try:
        source = import_mock_source(space, PAPER_B)
        handle = start_extraction(
            space,
            request=ExtractionRequest(
                mode="synthetic",
                method="direct",
                source_refs=[source],
                models=[StepModel(model="mock-default")],
                expert_adjudication=False,
            ),
            request_key="no-money-run",
        )
        report = resume_extraction(
            space, handle.execution_id, service=PipelineMockService()
        )
        assert report.outcome == "finished"
        from hvs_catalog.runtime import get_execution

        runtime_handle = get_execution(space, handle.execution_id)
        run = space.store().read_record(runtime_handle.run_ref)
        payload = json.dumps(run.model_dump(mode="json"), ensure_ascii=False)
        assert '"cost"' not in payload and '"currency"' not in payload
        assert '"tokens"' in payload  # 用量照常保存
    finally:
        space.close()
