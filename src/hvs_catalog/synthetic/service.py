"""模拟模型服务：按固定材料快照确定性应答，账本独立于工作空间。

账本只增不改、放在空间外的独立文件，不随客户端事务回滚，也不进入
备份——客户端在“收到响应但尚未保存”时退出，账单事实仍然保留。

当前不再计算、保存或统计货币费用：新调用只记录调用事实与用量
（tokens 未知记 NULL，不伪造 0）。带 ``cost NOT NULL`` 的历史账本在
打开时经单次事务迁移为可空列，历史行及其费用值原样保留、仍可读取
（``dump``）；``summary`` 只报告调用数与 tokens，不再汇总费用。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

MODEL_NAME = "synthetic-mock"

#: 账本结构版本：2 = cost 可空、新增 tokens 列；1 = 旧 cost NOT NULL。
LEDGER_SCHEMA_VERSION = 2

_CREATE_CALLS_V2 = (
    "CREATE TABLE {name} ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " request_digest TEXT NOT NULL,"
    " step TEXT NOT NULL,"
    " cost REAL,"
    " tokens INTEGER,"
    " created_at TEXT NOT NULL)"
)


def request_digest(request: dict) -> str:
    from ..records.common import sha256_of

    return sha256_of(request)


def _column_flags(conn: sqlite3.Connection, table: str) -> dict[str, bool]:
    """返回 ``{列名: 是否 NOT NULL}``；表不存在时为空。"""
    return {
        str(row[1]): bool(row[3]) for row in conn.execute(f"PRAGMA table_info({table})")
    }


def _migrate_legacy_ledger(conn: sqlite3.Connection) -> None:
    """事务化迁移旧 ``cost NOT NULL`` 账本；历史行与费用值原样保留。

    迁移在一个 ``BEGIN IMMEDIATE`` 事务内重建表并整行复制，失败整体
    回滚；完成后以 ``PRAGMA user_version`` 记录结构版本，重开时核对
    不再重复迁移。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(_CREATE_CALLS_V2.format(name="calls_new"))
        conn.execute(
            "INSERT INTO calls_new (id, request_digest, step, cost, tokens,"
            " created_at) SELECT id, request_digest, step, cost, NULL,"
            " created_at FROM calls"
        )
        conn.execute("DROP TABLE calls")
        conn.execute("ALTER TABLE calls_new RENAME TO calls")
        conn.execute(f"PRAGMA user_version = {LEDGER_SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _open_ledger(ledger_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(ledger_path, isolation_level=None)  # 显式事务
    flags = _column_flags(conn, "calls")
    if not flags:
        conn.execute(_CREATE_CALLS_V2.format(name="calls"))
        conn.execute(f"PRAGMA user_version = {LEDGER_SCHEMA_VERSION}")
        return conn
    if flags.get("cost", False) or "tokens" not in flags:
        _migrate_legacy_ledger(conn)
    problems = conn.execute("PRAGMA integrity_check").fetchone()
    if problems is None or problems[0] != "ok":  # pragma: no cover - 磁盘损坏
        conn.close()
        raise sqlite3.DatabaseError(f"账本完整性检查失败：{ledger_path}")
    return conn


class MockExtractionService:
    """合成提取服务；应答内容完全来自固定材料快照，不产生货币费用。"""

    def __init__(self, ledger_path: str | Path) -> None:
        self.ledger_path = Path(ledger_path)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = _open_ledger(self.ledger_path)

    def close(self) -> None:
        self._conn.close()

    def call(self, *, step: str, request: dict, material: dict) -> dict:
        """记录一次调用并按材料快照返回确定性应答；不记录费用。"""
        digest = request_digest(request)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "INSERT INTO calls (request_digest, step, cost, tokens, created_at)"
                " VALUES (?, ?, NULL, NULL, ?)",
                (digest, step, datetime.now(UTC).isoformat()),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
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
        """同请求的既有调用次数（历史命名保留；不再表示计费）。"""
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
        """用量统计：调用数与 tokens；不汇总或比较货币费用。"""
        row = self._conn.execute("SELECT COUNT(*) FROM calls").fetchone()
        # 本服务不知道 tokens 用量：保持 null，不伪造 0
        return {"calls": int(row[0]), "tokens": None}

    def dump(self) -> list[dict]:
        """逐行读取账本；历史行的费用值原样保留（可读，不修改）。"""
        rows = self._conn.execute(
            "SELECT request_digest, step, cost, tokens, created_at FROM calls"
            " ORDER BY id"
        ).fetchall()
        return [
            {
                "request_digest": r[0],
                "step": r[1],
                "cost": r[2],
                "tokens": r[3],
                "created_at": r[4],
            }
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
                " velocity and reassesses the object's boundness."
            ),
            "type": "follow_up",
        },
        "expected_status": m.EXPECTED_STATUS[paper],
        "model": MODEL_NAME,
    }


def snapshot_json(material: dict) -> str:
    return json.dumps(material, ensure_ascii=False, sort_keys=True)
