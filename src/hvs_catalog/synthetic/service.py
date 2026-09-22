"""模拟模型服务：按固定材料快照确定性应答，账本独立于工作空间。

账本只增不改、放在空间外的独立文件，不随客户端事务回滚，也不进入
备份——客户端在“收到响应但尚未保存”时退出，账单事实仍然保留。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

MODEL_NAME = "synthetic-mock"
CALL_COST = 0.5


def request_digest(request: dict) -> str:
    from ..records.common import sha256_of

    return sha256_of(request)


class MockExtractionService:
    """合成提取服务；应答内容完全来自固定材料快照。"""

    def __init__(self, ledger_path: str | Path) -> None:
        self.ledger_path = Path(ledger_path)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.ledger_path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS calls ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " request_digest TEXT NOT NULL,"
            " step TEXT NOT NULL,"
            " cost REAL NOT NULL,"
            " created_at TEXT NOT NULL)"
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def call(self, *, step: str, request: dict, material: dict) -> dict:
        """记录一次调用并按材料快照返回确定性应答。"""
        digest = request_digest(request)
        self._conn.execute(
            "INSERT INTO calls (request_digest, step, cost, created_at)"
            " VALUES (?, ?, ?, ?)",
            (digest, step, CALL_COST, datetime.now(UTC).isoformat()),
        )
        self._conn.commit()
        if step == "extract_roster":
            return {
                "entries": [
                    {
                        "item_id": entry["item_id"],
                        "identifiers": [i["value"] for i in entry["identifiers"]],
                        "disposition": entry["disposition"],
                    }
                    for entry in material["roster_spec"]
                ]
            }
        if step == "extract_contribution":
            return {
                "contribution": material["contribution_spec"],
                "needs_expert": material["needs_expert"],
            }
        raise ValueError(f"未知的服务步骤：{step}")

    def billed_calls(self, request: dict) -> int:
        digest = request_digest(request)
        row = self._conn.execute(
            "SELECT COUNT(*) FROM calls WHERE request_digest = ?", (digest,)
        ).fetchone()
        return int(row[0])

    def calls_for_step(self, step: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM calls WHERE step = ?", (step,)
        ).fetchone()
        return int(row[0])

    def summary(self) -> dict:
        row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost), 0) FROM calls"
        ).fetchone()
        return {"calls": int(row[0]), "total_cost": float(row[1])}

    def dump(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT request_digest, step, cost, created_at FROM calls ORDER BY id"
        ).fetchall()
        return [
            {"request_digest": r[0], "step": r[1], "cost": r[2], "created_at": r[3]}
            for r in rows
        ]


def material_snapshot(paper: str) -> dict:
    """固定材料快照：模拟服务与流程节点共同使用的完整输入。"""
    from . import material as m

    entry_item = f"roster-{paper}-1"
    return {
        "paper": paper,
        "paper_key": m.PAPER_KEYS[paper],
        "object_name": m.OBJECT_NAMES[paper],
        "lines": list(m.LINES[paper]),
        "needs_expert": m.needs_expert(paper),
        "roster_spec": [
            {
                "item_id": entry_item,
                "identifiers": [{"value": m.OBJECT_NAMES[paper]}],
                "disposition": "included",
            }
        ],
        "contribution_spec": {
            "item_id": f"contribution-{paper}-1",
            "summary": (
                "Follow-up spectroscopy provides a new heliocentric radial"
                " velocity and reassesses the object's Galactic boundness."
            ),
            "type": "follow_up",
        },
        "expected_status": m.EXPECTED_STATUS[paper],
        "model": MODEL_NAME,
    }


def snapshot_json(material: dict) -> str:
    return json.dumps(material, ensure_ascii=False, sort_keys=True)
