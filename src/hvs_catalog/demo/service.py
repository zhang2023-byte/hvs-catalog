"""模拟模型服务：离线合成响应，并用独立账单扮演远程服务端计费。

真实付费服务在返回响应之前，就已经把这次调用记入服务端账单；客户端
进程退出不会回滚这笔费用。本模块用独立的 SQLite 账单文件扮演服务端：
每次调用先写入账单、再返回响应。因此演示“收到响应但尚未保存就退出”
后，账单仍保留已计费记录，恢复时重新调用会再次计费。

响应内容由材料快照确定：运行开始时把当前合成材料快照存入流程状态
并随检查点保存，服务只按调用携带的快照生成响应——修改模块常量不
影响已开始的运行。账单属于模拟服务一方，演示程序只追加、不修改。
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

#: 模拟服务使用的“模型”名称，进入调用请求与操作输入摘要
MODEL_NAME = "synthetic-model-demo-v1"

# 合成单价（元/次），仅用于演示计费，不代表任何真实服务
STEP_PRICES: dict[str, float] = {"roster": 0.5, "values": 0.8}

PAPER_ID = "demo-001"
PAPER_TITLE = "虚构演示论文 demo-001（合成材料，不对应真实研究）"

# 名单步骤的合成响应：三名候选对象，其中一名来自引用
ROSTER_RESPONSE: dict = {
    "objects": [
        {"name": "J1234+5678", "role": "当前论文候选"},
        {"name": "J1234+5679", "role": "当前论文候选"},
        {"name": "J0456+7890", "role": "引用前文的候选"},
    ],
}

# 数值步骤的合成响应：每名候选各一条速度测量，来源与数值原样保留；
# 前两名候选的测量完全相同且命名相邻，构成对象身份的待裁决问题。
# 演示裁决不挑选任何测量值，与产品“不同来源测量分别保留”的边界一致。
VALUES_RESPONSE: dict = {
    "measurements": [
        {
            "object": "J1234+5678",
            "quantity": "速度",
            "value": 580.0,
            "error": 20.0,
            "unit": "km/s",
            "source": "当前论文",
        },
        {
            "object": "J1234+5679",
            "quantity": "速度",
            "value": 580.0,
            "error": 20.0,
            "unit": "km/s",
            "source": "当前论文",
        },
        {
            "object": "J0456+7890",
            "quantity": "速度",
            "value": 610.0,
            "error": 15.0,
            "unit": "km/s",
            "source": "引用前文",
        },
    ],
    "needs_decision": {
        "objects": ["J1234+5678", "J1234+5679"],
        "reason": "两名候选的速度测量完全相同且命名相邻，可能是同一天体的重复记录；程序无法确定，需人工裁决是否识别为同一对象。",
    },
}


def digest(parts: dict) -> str:
    """按规范 JSON 计算摘要；内容相同即得到相同摘要。"""
    canonical = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def default_material() -> dict:
    """返回当前合成材料的完整快照。

    快照包含论文元数据与决定服务应答的全部合成输入（名单与数值的
    响应内容），是“实际合成输入”的唯一来源：运行开始时存入流程状态
    并随检查点保存，恢复时服务按快照应答。
    """
    return {
        "paper_id": PAPER_ID,
        "title": PAPER_TITLE,
        "roster_response": copy.deepcopy(ROSTER_RESPONSE),
        "values_response": copy.deepcopy(VALUES_RESPONSE),
    }


def material_digest(material: dict) -> str:
    """返回材料快照的摘要，作为操作输入与分歧比较的依据。"""
    return digest(material)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ServiceCall:
    """一次模拟调用的结果：响应内容与本次计费。"""

    kind: str
    request_digest: str
    response: dict
    cost: float
    call_id: int


class MockModelService:
    """带持久账单的离线模拟服务。"""

    def __init__(self, ledger_path: Path) -> None:
        self._conn = sqlite3.connect(ledger_path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS service_calls (
                call_id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                response_digest TEXT NOT NULL,
                cost REAL NOT NULL,
                called_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @property
    def model_name(self) -> str:
        return MODEL_NAME

    def build_request(
        self,
        kind: str,
        *,
        rules: str,
        model: str,
        paper_digest: str,
        roster_names: list[str] | None = None,
    ) -> dict:
        """按调用类型与运行固定输入构造请求；内容确定即摘要确定。

        ``rules``、``model``、``paper_digest`` 使用运行开始时固定并保存
        在流程状态中的值，恢复时不随当前代码或材料的变化而改变。
        """
        if kind not in STEP_PRICES:
            raise ValueError(f"未知调用类型：{kind}")
        request: dict = {
            "kind": kind,
            "model": model,
            "paper_id": PAPER_ID,
            "paper_digest": paper_digest,
            "rules": rules,
        }
        if roster_names is not None:
            request["roster_names"] = list(roster_names)
        return request

    def billed_calls(self, request: dict) -> int:
        """返回同一请求此前已被计费的次数，用于识别重复调用。"""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM service_calls WHERE kind = ? AND request_digest = ?",
            (request["kind"], digest(request)),
        ).fetchone()
        return int(row[0])

    def call(self, request: dict, material: dict) -> ServiceCall:
        """执行一次调用：按运行固定的材料快照应答，先记账再返回。

        请求中的材料摘要必须与快照一致，保证响应登记在正确的运行
        输入之下；不一致说明请求与快照错配，拒绝执行。
        """
        if request.get("paper_digest") != material_digest(material):
            raise ValueError("请求携带的材料摘要与材料快照不一致，拒绝调用")
        kind = request["kind"]
        response = self._synthetic_response(kind, material)
        cursor = self._conn.execute(
            """
            INSERT INTO service_calls
                (kind, request_digest, response_digest, cost, called_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (kind, digest(request), digest(response), STEP_PRICES[kind], _now()),
        )
        self._conn.commit()
        if cursor.lastrowid is None:  # pragma: no cover - 自增主键总有值
            raise RuntimeError("模拟服务未能取得账单编号")
        return ServiceCall(
            kind, digest(request), response, STEP_PRICES[kind], cursor.lastrowid
        )

    def billing_summary(self) -> dict:
        """汇总服务端账单：总次数、总费用与分步明细。"""
        by_step: dict[str, dict] = {}
        rows = self._conn.execute(
            "SELECT kind, COUNT(*), SUM(cost) FROM service_calls GROUP BY kind ORDER BY kind"
        ).fetchall()
        for kind, calls, cost in rows:
            by_step[kind] = {"calls": int(calls), "cost": float(cost or 0.0)}
        total_calls = sum(item["calls"] for item in by_step.values())
        total_cost = sum(item["cost"] for item in by_step.values())
        return {"calls": total_calls, "total_cost": total_cost, "by_step": by_step}

    def _synthetic_response(self, kind: str, material: dict) -> dict:
        if kind == "roster":
            return copy.deepcopy(material["roster_response"])
        if kind == "values":
            return copy.deepcopy(material["values_response"])
        raise ValueError(f"未知调用类型：{kind}")
