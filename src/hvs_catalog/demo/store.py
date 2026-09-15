"""业务数据库：保存演示流程的实际结果、裁决决定与汇总。

与 LangGraph 检查点的分工：检查点由框架保存，只记录流程执行到哪里
与中断现场；本数据库由应用负责，保存业务事实。恢复时流程按检查点
续跑，节点用操作标识加输入摘要查询本库：输入相同则复用已保存结果、
不再调用服务；输入变化视为一次新的执行，不误用旧行。

决定属于特定版本的问题：每条决定按（问题标识， 问题摘要）保存，
问题摘要覆盖实际证据与选项。证据或规则变化后重建的问题摘要不同，
属于另一个版本的问题，需要另行保存决定；旧版本的决定保留在库中
备查，不会被应用到新问题上。同一版本的问题重复提交同一决定只生效
一次，提交不同决定被拒绝。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class DecisionConflictError(Exception):
    """同一版本的问题已保存不同决定，本次提交被拒绝。"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class StoredDecision:
    question_id: str
    question_digest: str
    choice: str
    reason: str | None
    decided_at: str


class BusinessStore:
    """演示空间的业务数据库。"""

    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS results (
                op_id TEXT NOT NULL,
                input_digest TEXT NOT NULL,
                input_json TEXT NOT NULL,
                output_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (op_id, input_digest)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS decisions (
                question_id TEXT NOT NULL,
                question_digest TEXT NOT NULL,
                question_json TEXT NOT NULL,
                choice TEXT NOT NULL,
                reason TEXT,
                decided_at TEXT NOT NULL,
                PRIMARY KEY (question_id, question_digest)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS summaries (
                paper_id TEXT PRIMARY KEY,
                summary_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- 步骤结果 ----

    def find_result(self, op_id: str, input_digest: str) -> dict | None:
        row = self._conn.execute(
            "SELECT output_json FROM results WHERE op_id = ? AND input_digest = ?",
            (op_id, input_digest),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def save_result(
        self, op_id: str, input_digest: str, input_json: str, output: dict
    ) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO results
                (op_id, input_digest, input_json, output_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                op_id,
                input_digest,
                input_json,
                json.dumps(output, ensure_ascii=False),
                _now(),
            ),
        )
        self._conn.commit()

    def list_results(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT op_id, input_digest, created_at FROM results ORDER BY created_at, op_id"
        ).fetchall()
        return [
            {"op_id": op_id, "input_digest": digest, "created_at": created_at}
            for op_id, digest, created_at in rows
        ]

    # ---- 裁决决定 ----

    def find_decision(
        self, question_id: str, question_digest: str
    ) -> StoredDecision | None:
        """查找属于指定版本问题的决定。"""
        row = self._conn.execute(
            "SELECT question_id, question_digest, choice, reason, decided_at"
            " FROM decisions WHERE question_id = ? AND question_digest = ?",
            (question_id, question_digest),
        ).fetchone()
        if row is None:
            return None
        return StoredDecision(row[0], row[1], row[2], row[3], row[4])

    def save_decision(
        self,
        question_id: str,
        question_digest: str,
        question: dict,
        choice: str,
        reason: str | None,
    ) -> str:
        """保存属于该版本问题的决定；返回 "saved" 或 "already-saved"。

        同一版本的问题已保存不同决定时拒绝；不同摘要属于另一个版本的
        问题，另行保存，不相互覆盖。
        """
        existing = self.find_decision(question_id, question_digest)
        if existing is None:
            self._conn.execute(
                """
                INSERT INTO decisions
                    (question_id, question_digest, question_json, choice, reason, decided_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    question_id,
                    question_digest,
                    json.dumps(question, ensure_ascii=False),
                    choice,
                    reason,
                    _now(),
                ),
            )
            self._conn.commit()
            return "saved"
        if existing.choice != choice:
            raise DecisionConflictError(
                f"问题 {question_id}（摘要 {question_digest}）已保存决定"
                f" {existing.choice}，本次提交的 {choice} 与之冲突，被拒绝"
            )
        return "already-saved"

    def list_decisions(self) -> list[StoredDecision]:
        rows = self._conn.execute(
            "SELECT question_id, question_digest, choice, reason, decided_at"
            " FROM decisions ORDER BY decided_at"
        ).fetchall()
        return [StoredDecision(*row) for row in rows]

    # ---- 汇总 ----

    def save_summary(self, paper_id: str, summary: dict) -> str:
        """保存汇总；同一论文只生效一次，返回 "saved" 或 "already-saved"。"""
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO summaries (paper_id, summary_json, created_at)"
            " VALUES (?, ?, ?)",
            (paper_id, json.dumps(summary, ensure_ascii=False), _now()),
        )
        self._conn.commit()
        return "saved" if cursor.rowcount else "already-saved"

    def find_summary(self, paper_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT summary_json FROM summaries WHERE paper_id = ?", (paper_id,)
        ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])
