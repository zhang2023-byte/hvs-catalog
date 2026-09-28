"""费用兼容：停止新费用计算/保存/比较，兼容旧记录与旧账本（C11）。

- ``Usage``/``Attempt`` 的 cost/currency 可省略；新运行序列化不输出未
  填货币字段；历史有值数据照常读取并保留。
- 旧 ``cost NOT NULL`` SQLite 账本打开时事务化迁移，历史行与费用值
  原样保留；新调用只记调用与 tokens（未知为 NULL），统计不再汇总费用。
- 新提取运行的记录 JSON 不含 cost/currency。
- 持久化演示的模拟账单是既定架构例外，保持不变。
"""

from __future__ import annotations

import json
import sqlite3

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
from hvs_catalog.synthetic.service import MockExtractionService
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


def _legacy_ledger(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS calls ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " request_digest TEXT NOT NULL,"
        " step TEXT NOT NULL,"
        " cost REAL NOT NULL,"
        " created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO calls (request_digest, step, cost, created_at)"
        " VALUES ('d1', 'extract_roster', 0.5, '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO calls (request_digest, step, cost, created_at)"
        " VALUES ('d2', 'extract_contribution', 0.5, '2026-01-01T00:00:01+00:00')"
    )
    conn.commit()
    conn.close()


def test_legacy_ledger_migrates_and_preserves_history(tmp_path):
    ledger = tmp_path / "ledger.db"
    _legacy_ledger(ledger)
    material = {
        "roster_spec": [],
        "contribution_spec": {"item_id": "c1", "summary": "s", "type": "follow_up"},
        "needs_expert": False,
    }
    service = MockExtractionService(ledger)
    service.call(step="extract_roster", request={"n": 1}, material=material)
    rows = service.dump()
    # 历史行费用原样保留；新调用不填费用（NULL，不是 0）
    assert rows[0]["cost"] == 0.5 and rows[1]["cost"] == 0.5
    assert rows[2]["cost"] is None
    assert rows[2]["tokens"] is None
    # 统计只有调用数与 tokens，不汇总或比较货币费用
    summary = service.summary()
    assert summary == {"calls": 3, "tokens": None}
    assert "total_cost" not in summary and "cost" not in summary
    service.close()
    # 重开核对：迁移只发生一次，历史数据不受影响
    again = MockExtractionService(ledger)
    assert again.dump()[0]["cost"] == 0.5
    assert again.calls_for_step("extract_roster") == 2
    version = again._conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 2
    again.close()


def test_new_ledger_has_no_cost_column_constraint(tmp_path):
    service = MockExtractionService(tmp_path / "fresh.db")
    flags = {
        str(row[1]): bool(row[3])
        for row in service._conn.execute("PRAGMA table_info(calls)")
    }
    assert flags["cost"] is False  # 可空：新调用不填费用
    assert "tokens" in flags
    service.close()


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


def test_demo_simulated_billing_is_kept_as_exception():
    """持久化演示的模拟账单是既定架构例外：保持原样，不随费用取消。"""
    from hvs_catalog.demo import service as demo_service

    assert hasattr(demo_service, "MockModelService")
    source = __import__("inspect").getsource(demo_service)
    # 持久化演示的模拟账单与费用汇总保持原样（既定例外，不随费用取消）
    assert "cost REAL NOT NULL" in source
    assert "SUM(cost)" in source
