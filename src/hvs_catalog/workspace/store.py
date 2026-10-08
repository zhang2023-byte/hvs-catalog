"""Business database stores original records, current indexes, execution/step/question
bindings, and receipts.

Record JSON is authoritative science; indexes can rebuild. Validate complete candidate
collections, then append revisions/update indexes/save receipts in one transaction.
Operation keys/digests/execution-step/pre-post references/stages prevent repeated
effects and support recovery; back up receipts, never treat them as disposable caches.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..records.common import RecordRef, canonical_json, sha256_of
from ..records.file_checks import file_digest, scoped_verification
from ..records.paper import PaperRecord
from ..records.registry import AnyRecord
from ..records.roster import RosterRecord
from ..records.serialize import parse_record
from ..records.validate import (
    CollectionValidationError,
    iter_record_refs,
    validate_collection,
)
from .errors import (
    OperationKeyConflictError,
    RevisionStaleError,
    WorkspaceIntegrityError,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _record_payload(record: AnyRecord) -> str:
    return canonical_json(record.model_dump(mode="json", by_alias=True))


@dataclass(frozen=True)
class Receipt:
    operation_key: str
    kind: str
    content_digest: str
    execution_id: str | None
    step_id: str | None
    result_refs: tuple[RecordRef, ...]
    links: dict[str, object]
    created_at: str
    replayed: bool = False


@dataclass
class CommitRequest:
    """Complete business change request.

    content supplies replay digests excluding generated IDs/times/receipts, otherwise
    use new record contents. Check expected_revisions inside transactions and roll back
    mismatches. question_steps saves bindings together with questions; unbound questions
    never exist. meta_updates saves runtime/completion credentials atomically,
    preventing revision/credential separation.
    """

    operation_key: str
    kind: str
    new_records: list[AnyRecord] = field(default_factory=list)
    expected_revisions: dict[str, int] = field(default_factory=dict)
    result_refs: list[RecordRef] = field(default_factory=list)
    execution_id: str | None = None
    step_id: str | None = None
    input_digest: str | None = None
    links: dict[str, object] = field(default_factory=dict)
    content: dict[str, object] | None = None
    question_steps: dict[str, str] = field(default_factory=dict)
    meta_updates: dict[str, str] = field(default_factory=dict)


def commit_request_digest(request: CommitRequest) -> str:
    if request.content is not None:
        return sha256_of(request.content)
    return sha256_of(
        {
            "kind": request.kind,
            "expected_revisions": request.expected_revisions,
            "records": [
                [
                    record.record_id,
                    record.revision,
                    record.RECORD_NAME,
                    record.model_dump(mode="json", by_alias=True)["data"],
                ]
                for record in request.new_records
            ],
            "result_refs": [ref.model_dump(mode="json") for ref in request.result_refs],
            "execution_id": request.execution_id,
            "step_id": request.step_id,
            "input_digest": request.input_digest,
            "links": request.links,
            "question_steps": request.question_steps,
            "meta_updates": request.meta_updates,
        }
    )


@dataclass(frozen=True)
class ReceiptRow:
    operation_key: str
    kind: str
    content_digest: str
    execution_id: str | None
    step_id: str | None
    result_refs: tuple[RecordRef, ...]
    links: dict[str, object]
    created_at: str

    def to_receipt(self, *, replayed: bool = False) -> Receipt:
        return Receipt(
            operation_key=self.operation_key,
            kind=self.kind,
            content_digest=self.content_digest,
            execution_id=self.execution_id,
            step_id=self.step_id,
            result_refs=self.result_refs,
            links=dict(self.links),
            created_at=self.created_at,
            replayed=replayed,
        )


@dataclass(frozen=True)
class StepResultRow:
    execution_id: str
    step_id: str
    input_digest: str
    operation_key: str
    result_refs: tuple[RecordRef, ...]
    created_at: str


@dataclass(frozen=True)
class ExecutionRow:
    execution_id: str
    request_key: str
    input_digest: str
    run_record_id: str
    flow_id: str
    flow_version: str
    mode: str
    state: str
    created_at: str


@dataclass(frozen=True)
class QuestionIndexRow:
    question_id: str
    execution_id: str
    step_id: str | None
    latest_revision: int
    status: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
    record_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    schema_name TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    execution_id TEXT,
    PRIMARY KEY (record_id, revision)
);
CREATE TABLE IF NOT EXISTS current_revisions (
    record_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    operation_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    execution_id TEXT,
    step_id TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    request_key TEXT NOT NULL UNIQUE,
    input_digest TEXT NOT NULL,
    run_record_id TEXT NOT NULL,
    flow_id TEXT NOT NULL,
    flow_version TEXT NOT NULL,
    mode TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS step_results (
    execution_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    result_refs_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (execution_id, step_id, input_digest)
);
CREATE TABLE IF NOT EXISTS questions (
    question_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    step_id TEXT,
    latest_revision INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


class StoreTransaction:
    """Transaction operations after BEGIN IMMEDIATE, obtained through transaction()."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ---- Raw reads and writes ----

    def current_revision(self, record_id: str) -> int | None:
        row = self.conn.execute(
            "SELECT revision FROM current_revisions WHERE record_id = ?", (record_id,)
        ).fetchone()
        return int(row[0]) if row else None

    def record_payload(self, record_id: str, revision: int) -> str | None:
        row = self.conn.execute(
            "SELECT payload FROM records WHERE record_id = ? AND revision = ?",
            (record_id, revision),
        ).fetchone()
        return str(row[0]) if row else None

    def all_records(self) -> list[AnyRecord]:
        rows = self.conn.execute(
            "SELECT payload FROM records ORDER BY record_id, revision"
        ).fetchall()
        return [parse_record(row[0]) for row in rows]

    def insert_record(self, record: AnyRecord) -> None:
        self.conn.execute(
            "INSERT INTO records"
            " (record_id, revision, schema_name, payload, created_at, execution_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                record.record_id,
                record.revision,
                record.RECORD_NAME,
                _record_payload(record),
                record.created_at.isoformat(),
                record.execution_id,
            ),
        )
        self.conn.execute(
            "INSERT INTO current_revisions (record_id, revision) VALUES (?, ?)"
            " ON CONFLICT(record_id) DO UPDATE SET revision = excluded.revision",
            (record.record_id, record.revision),
        )
        if record.RECORD_NAME == "question":
            self.conn.execute(
                "INSERT INTO questions"
                " (question_id, execution_id, step_id, latest_revision, status)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(question_id) DO UPDATE SET"
                " latest_revision = excluded.latest_revision,"
                " status = excluded.status",
                (
                    record.record_id,
                    record.execution_id or "",
                    None,
                    record.revision,
                    record.data.status,
                ),
            )

    def set_question_step(self, question_id: str, step_id: str) -> None:
        self.conn.execute(
            "UPDATE questions SET step_id = ? WHERE question_id = ?",
            (step_id, question_id),
        )

    def find_receipt(self, operation_key: str) -> ReceiptRow | None:
        row = self.conn.execute(
            "SELECT operation_key, kind, content_digest, execution_id, step_id,"
            " payload_json, created_at FROM receipts WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()
        return _receipt_from_row(row) if row else None

    def find_receipts(
        self,
        *,
        kind: str | None = None,
        execution_id: str | None = None,
    ) -> list[ReceiptRow]:
        clauses, params = [], []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if execution_id is not None:
            clauses.append("execution_id = ?")
            params.append(execution_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            "SELECT operation_key, kind, content_digest, execution_id, step_id,"
            f" payload_json, created_at FROM receipts{where} ORDER BY created_at",
            params,
        ).fetchall()
        return [_receipt_from_row(row) for row in rows]

    def insert_receipt(self, receipt: Receipt) -> None:
        self.conn.execute(
            "INSERT INTO receipts"
            " (operation_key, kind, content_digest, execution_id, step_id,"
            " payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                receipt.operation_key,
                receipt.kind,
                receipt.content_digest,
                receipt.execution_id,
                receipt.step_id,
                canonical_json(
                    {
                        "result_refs": [
                            ref.model_dump(mode="json") for ref in receipt.result_refs
                        ],
                        "links": receipt.links,
                    }
                ),
                receipt.created_at,
            ),
        )

    def insert_step_result(
        self,
        *,
        execution_id: str,
        step_id: str,
        input_digest: str,
        operation_key: str,
        result_refs: list[RecordRef],
    ) -> None:
        self.conn.execute(
            "INSERT INTO step_results"
            " (execution_id, step_id, input_digest, operation_key,"
            " result_refs_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                execution_id,
                step_id,
                input_digest,
                operation_key,
                canonical_json([ref.model_dump(mode="json") for ref in result_refs]),
                _now(),
            ),
        )

    def find_step_result(
        self, execution_id: str, step_id: str, input_digest: str
    ) -> StepResultRow | None:
        row = self.conn.execute(
            "SELECT execution_id, step_id, input_digest, operation_key,"
            " result_refs_json, created_at FROM step_results"
            " WHERE execution_id = ? AND step_id = ? AND input_digest = ?",
            (execution_id, step_id, input_digest),
        ).fetchone()
        if not row:
            return None
        return StepResultRow(
            execution_id=row[0],
            step_id=row[1],
            input_digest=row[2],
            operation_key=row[3],
            result_refs=tuple(
                RecordRef.model_validate(item) for item in json.loads(row[4])
            ),
            created_at=row[5],
        )

    def insert_execution(self, row: ExecutionRow) -> None:
        self.conn.execute(
            "INSERT INTO executions"
            " (execution_id, request_key, input_digest, run_record_id, flow_id,"
            " flow_version, mode, state, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.execution_id,
                row.request_key,
                row.input_digest,
                row.run_record_id,
                row.flow_id,
                row.flow_version,
                row.mode,
                row.state,
                row.created_at,
            ),
        )

    def find_execution_by_request(self, request_key: str) -> ExecutionRow | None:
        row = self.conn.execute(
            "SELECT execution_id, request_key, input_digest, run_record_id, flow_id,"
            " flow_version, mode, state, created_at FROM executions"
            " WHERE request_key = ?",
            (request_key,),
        ).fetchone()
        return _execution_from_row(row) if row else None

    def get_execution(self, execution_id: str) -> ExecutionRow | None:
        row = self.conn.execute(
            "SELECT execution_id, request_key, input_digest, run_record_id, flow_id,"
            " flow_version, mode, state, created_at FROM executions"
            " WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        return _execution_from_row(row) if row else None

    def update_execution_state(self, execution_id: str, state: str) -> None:
        self.conn.execute(
            "UPDATE executions SET state = ? WHERE execution_id = ?",
            (state, execution_id),
        )

    def list_executions(self) -> list[ExecutionRow]:
        rows = self.conn.execute(
            "SELECT execution_id, request_key, input_digest, run_record_id, flow_id,"
            " flow_version, mode, state, created_at FROM executions"
            " ORDER BY created_at"
        ).fetchall()
        return [_execution_from_row(row) for row in rows]

    def next_id(self, prefix: str) -> str:
        row = self.conn.execute(
            "SELECT value FROM counters WHERE name = ?", (prefix,)
        ).fetchone()
        value = (int(row[0]) if row else 0) + 1
        self.conn.execute(
            "INSERT INTO counters (name, value) VALUES (?, ?)"
            " ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (prefix, value),
        )
        return f"{prefix}-{value:06d}"

    def _flush_commit(self) -> None:
        """Commit the transaction; tests may replace this method to simulate pre-commit
        exit.
        """
        self.conn.commit()

    def _rollback(self) -> None:
        self.conn.rollback()


def _receipt_from_row(row: sqlite3.Row | tuple) -> ReceiptRow:
    payload = json.loads(row[5])
    return ReceiptRow(
        operation_key=row[0],
        kind=row[1],
        content_digest=row[2],
        execution_id=row[3],
        step_id=row[4],
        result_refs=tuple(
            RecordRef.model_validate(item) for item in payload.get("result_refs", [])
        ),
        links=dict(payload.get("links", {})),
        created_at=row[6],
    )


def _execution_from_row(row: sqlite3.Row | tuple) -> ExecutionRow:
    return ExecutionRow(
        execution_id=row[0],
        request_key=row[1],
        input_digest=row[2],
        run_record_id=row[3],
        flow_id=row[4],
        flow_version=row[5],
        mode=row[6],
        state=row[7],
        created_at=row[8],
    )


class RecordStore:
    """Workspace business database handle."""

    def __init__(
        self,
        db_path: Path,
        *,
        create: bool = False,
        space_id: str | None = None,
        materials_root: Path | None = None,
        read_only: bool = False,
    ) -> None:
        self.db_path = Path(db_path)
        self.read_only = read_only
        self.materials_root = Path(materials_root) if materials_root else None
        if read_only and create:
            raise WorkspaceIntegrityError("只读存储不能初始化数据库")
        self._conn = (
            sqlite3.connect(
                self.db_path.resolve().as_uri() + "?mode=ro",
                uri=True,
                isolation_level=None,
            )
            if read_only
            else sqlite3.connect(self.db_path, isolation_level=None)
        )
        self._conn.row_factory = sqlite3.Row
        if read_only:
            self._conn.execute("PRAGMA query_only=ON")
        else:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript(_SCHEMA)
        self._conn.execute("PRAGMA busy_timeout=5000")
        if create:
            if space_id is None:
                raise WorkspaceIntegrityError("初始化业务数据库必须提供空间身份")
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES ('space_id', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (space_id,),
            )
        elif space_id is not None:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'space_id'"
            ).fetchone()
            if row is None or row[0] != space_id:
                raise WorkspaceIntegrityError("业务数据库身份与请求不符")

    def close(self) -> None:
        self._conn.close()

    def compact_run_history(self, record_id, revision, execution_id) -> set[str]:
        """Remove closed-run progress revisions; science records remain immutable."""
        if self.read_only:
            raise WorkspaceIntegrityError("只读存储不能精简运行历史")
        from .backup import walk_state_refs

        protected = {revision}
        with self.transaction() as tx:
            # Exact revisions referenced by another result/input remain available.
            for row in tx.conn.execute(
                "SELECT r.payload FROM records r LEFT JOIN current_revisions c"
                " ON r.record_id=c.record_id AND r.revision=c.revision"
                " WHERE r.record_id <> ? AND (r.schema_name <> 'run' OR c.record_id IS NOT NULL)",
                (record_id,),
            ):
                value = json.loads(row[0])
                reused = (
                    (value.get("data", {}).get("input_snapshot") or {})
                    .get("options", {})
                    .get("reused_roster")
                )
                if reused:
                    value["reused_roster"] = json.loads(reused)
                protected.update(
                    ref.revision
                    for ref in walk_state_refs(value)
                    if ref.record_id == record_id
                )
            tx.conn.execute(
                "DELETE FROM receipts WHERE execution_id = ? AND kind IN ('run_update','create_execution')",
                (execution_id,),
            )
            tx.conn.execute(
                "DELETE FROM records WHERE record_id = ? AND revision NOT IN ("
                + ",".join("?" for _ in protected)
                + ")",
                (record_id, *sorted(protected)),
            )
            paths = {
                ref["path"]
                for row in tx.conn.execute(
                    "SELECT payload FROM records WHERE record_id=?", (record_id,)
                )
                for ref in walk_artifact_refs(json.loads(row[0]))
            }
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._conn.execute("VACUUM")
        return paths

    @contextmanager
    def transaction(self) -> Iterator[StoreTransaction]:
        """One business transaction: commit on success, roll back on exceptions."""
        self._conn.execute("BEGIN" if self.read_only else "BEGIN IMMEDIATE")
        tx = StoreTransaction(self._conn)
        try:
            yield tx
            tx._flush_commit()
        except BaseException:
            tx._rollback()
            raise

    # ---- Read committed facts with read-only transactions, without write exclusion ----

    def read_record(self, ref: RecordRef) -> AnyRecord | None:
        def fn(tx: StoreTransaction) -> AnyRecord | None:
            payload = tx.record_payload(ref.record_id, ref.revision)
            return parse_record(payload) if payload else None

        return self.transaction_read(fn)

    def read_record_current(self, record_id: str) -> AnyRecord | None:
        with self.transaction() as tx:
            revision = tx.current_revision(record_id)
            if revision is None:
                return None
            payload = tx.record_payload(record_id, revision)
            return parse_record(payload) if payload else None

    def current_revision(self, record_id: str) -> int | None:
        with self.transaction() as tx:
            return tx.current_revision(record_id)

    def list_records(self, *, execution_id: str | None = None) -> list[AnyRecord]:
        with self.transaction() as tx:
            if execution_id is None:
                return tx.all_records()
            rows = tx.conn.execute(
                "SELECT payload FROM records WHERE execution_id = ?"
                " ORDER BY record_id, revision",
                (execution_id,),
            ).fetchall()
            return [parse_record(row[0]) for row in rows]

    def find_receipt(self, operation_key: str) -> ReceiptRow | None:
        with self.transaction() as tx:
            return tx.find_receipt(operation_key)

    def find_receipts(
        self, *, kind: str | None = None, execution_id: str | None = None
    ) -> list[ReceiptRow]:
        with self.transaction() as tx:
            return tx.find_receipts(kind=kind, execution_id=execution_id)

    def find_step_result(
        self, execution_id: str, step_id: str, input_digest: str
    ) -> StepResultRow | None:
        with self.transaction() as tx:
            return tx.find_step_result(execution_id, step_id, input_digest)

    def get_execution(self, execution_id: str) -> ExecutionRow | None:
        with self.transaction() as tx:
            return tx.get_execution(execution_id)

    def find_execution_by_request(self, request_key: str) -> ExecutionRow | None:
        with self.transaction() as tx:
            return tx.find_execution_by_request(request_key)

    def list_executions(self) -> list[ExecutionRow]:
        with self.transaction() as tx:
            return tx.list_executions()

    def list_questions(
        self, *, execution_id: str | None = None
    ) -> list[QuestionIndexRow]:
        with self.transaction() as tx:
            return _list_questions(tx, execution_id)

    def transaction_read(self, fn):
        """Run queries inside read-only transactions."""
        self._conn.execute("BEGIN DEFERRED")
        tx = StoreTransaction(self._conn)
        try:
            result = fn(tx)
            self._conn.commit()
            return result
        except BaseException:
            self._conn.rollback()
            raise

    # ---- Runtime credentials in meta, included in full business backup/restore ----

    def set_meta(self, key: str, value: str) -> None:
        """Write one credential separately; use CommitRequest for atomic revision-bound
        writes.
        """
        self.set_meta_many({key: value})

    def set_meta_many(self, entries: dict[str, str]) -> None:
        """Write multiple metadata values in one transaction."""
        if not entries:
            return
        with self.transaction() as tx:
            for key, value in entries.items():
                tx.conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    def get_meta(self, key: str) -> str | None:
        def fn(tx: StoreTransaction) -> str | None:
            row = tx.conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
            return str(row[0]) if row is not None else None

        return self.transaction_read(fn)

    # ---- Commit protocol ----

    def commit(self, request: CommitRequest) -> Receipt:
        """Commit record batches by operation key with replay, expected-revision,
        collection, and receipt checks.
        """
        digest = commit_request_digest(request)
        with self.transaction() as tx:
            existing = tx.find_receipt(request.operation_key)
            if existing is not None:
                if existing.content_digest == digest:
                    return existing.to_receipt(replayed=True)
                raise OperationKeyConflictError(
                    f"操作键 {request.operation_key!r} 已用于不同内容"
                    f"（{existing.kind}，{existing.created_at}）；"
                    "重试须携带相同内容，或为新内容更换操作键"
                )
            for record_id, expected in request.expected_revisions.items():
                actual = tx.current_revision(record_id)
                if actual != expected:
                    raise RevisionStaleError(
                        f"记录 {record_id} 的预期当前修订为 {expected}，"
                        f"实际为 {actual if actual is not None else '不存在'}；"
                        "本次修改未提交"
                    )
            pending: list[AnyRecord] = []
            revisions: dict[str, int] = {}
            for record in request.new_records:
                payload = tx.record_payload(record.record_id, record.revision)
                if payload is not None:
                    if payload != _record_payload(record):
                        raise WorkspaceIntegrityError(
                            f"记录 {record.record_id} 修订 {record.revision}"
                            " 已存在且内容不同；相同编号与修订只能保存相同内容"
                        )
                    continue
                previous = revisions.get(record.record_id)
                if previous is None:
                    previous = tx.current_revision(record.record_id)
                if previous is not None and record.revision <= previous:
                    raise WorkspaceIntegrityError(
                        f"记录 {record.record_id} 修订 {record.revision} 未递增"
                        f"（当前或本批前一修订为 {previous}）"
                    )
                revisions[record.record_id] = record.revision
                pending.append(record)
            _check_question_bindings(tx, pending, request.question_steps)
            if pending:
                candidate = record_closure(
                    pending,
                    lambda ref: (
                        parse_record(payload)
                        if (payload := tx.record_payload(ref.record_id, ref.revision))
                        else None
                    ),
                )
                try:
                    validate_collection(
                        candidate,
                        self.materials_root if self.materials_root else None,
                    )
                except CollectionValidationError as exc:
                    raise WorkspaceIntegrityError(
                        f"候选记录集合未通过校验：{exc}"
                    ) from exc
                _check_run_artifacts(candidate, self.materials_root)
                _check_current_outputs(tx, pending)
                for record in pending:
                    tx.insert_record(record)
                for question_id, step_id in request.question_steps.items():
                    tx.set_question_step(question_id, step_id)
                if request.kind == "step_result" and request.input_digest is not None:
                    tx.insert_step_result(
                        execution_id=request.execution_id or "",
                        step_id=request.step_id or "",
                        input_digest=request.input_digest,
                        operation_key=request.operation_key,
                        result_refs=list(request.result_refs),
                    )
            receipt = Receipt(
                operation_key=request.operation_key,
                kind=request.kind,
                content_digest=digest,
                execution_id=request.execution_id,
                step_id=request.step_id,
                result_refs=tuple(request.result_refs),
                links=dict(request.links),
                created_at=_now(),
                replayed=not pending,
            )
            tx.insert_receipt(receipt)
            for key, value in request.meta_updates.items():
                tx.conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
            return receipt


def record_closure(seeds: list[AnyRecord], read_record) -> list[AnyRecord]:
    """Build recursive exact-revision closures; shared collection validation reports
    missing references.
    """
    records = {(record.record_id, record.revision): record for record in seeds}
    queue = list(records.values())
    for record in queue:
        for ref in iter_record_refs(record.data):
            key = (ref.record_id, ref.revision)
            if key in records:
                continue
            dependency = read_record(ref)
            if dependency is not None:
                records[key] = dependency
                queue.append(dependency)
    return [records[key] for key in sorted(records)]


def walk_artifact_refs(value: object) -> list[dict]:
    """Collect payload dictionaries shaped as file references with path/sha256."""
    found: list[dict] = []
    if isinstance(value, dict):
        if set(value) >= {"path", "sha256"} and isinstance(value.get("path"), str):
            found.append(value)
        else:
            for key, item in value.items():
                # Fixed preparation options are serialized ArtifactRefs. Read
                # only these explicit keys, never arbitrary JSON-valued strings.
                if key in {
                    "prepared_input",
                    "prepared_prompt",
                    "llm_input",
                } and isinstance(item, str):
                    from ..records.common import ArtifactRef

                    item = ArtifactRef.model_validate_json(item).model_dump(mode="json")
                found.extend(walk_artifact_refs(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(walk_artifact_refs(item))
    return found


@scoped_verification
def verify_artifact_refs(records: list[AnyRecord], root: Path) -> list[str]:
    """Verify all referenced archive existence, root-contained paths, and hashes.

    Cover every request/response/prompt/example/precedent/source {path, sha256}
    reference, including historical runs. Commits, backups, recovery, and completion
    share this function.
    """
    from .layout import resolve_within

    problems: list[str] = []
    for record in records:
        payload = record if isinstance(record, dict) else record.model_dump(mode="json")
        record_id = (
            record["record_id"] if isinstance(record, dict) else record.record_id
        )
        revision = record["revision"] if isinstance(record, dict) else record.revision
        for ref in walk_artifact_refs(payload):
            where = f"记录 {record_id} v{revision} 引用的 {ref['path']}"
            path = root / ref["path"]
            try:
                resolved = resolve_within(root, ref["path"])
            except WorkspaceIntegrityError as exc:
                problems.append(f"{where}：{exc}")
                continue
            if resolved.is_symlink() or not resolved.is_file():
                problems.append(f"{where} 缺失或不可读")
                continue
            digest = file_digest(path)
            if digest != ref.get("sha256"):
                problems.append(f"{where} 哈希与记录不符")
            else:
                from .archives import shared_dependencies

                try:
                    for dependency in shared_dependencies(root, ref):
                        target = resolve_within(root, dependency["path"])
                        if file_digest(target) != dependency["sha256"]:
                            problems.append(f"{where} 的共用输入哈希不符")
                except (OSError, ValueError) as exc:
                    problems.append(f"{where} 的共用输入不可读：{exc}")
    return problems


def _check_question_bindings(
    tx: StoreTransaction, pending: list[AnyRecord], question_steps: dict[str, str]
) -> None:
    """Initial question saves require atomic execution/step bindings; unbound questions
    cannot exist.

    Later revisions reuse unchanged original bindings without redeclaration.
    """
    for record in pending:
        if record.RECORD_NAME != "question":
            continue
        row = tx.conn.execute(
            "SELECT step_id FROM questions WHERE question_id = ?",
            (record.record_id,),
        ).fetchone()
        if row is None and record.record_id not in question_steps:
            raise WorkspaceIntegrityError(
                f"问题记录必须与步骤绑定一起提交，缺少绑定：{record.record_id}"
            )
    for question_id, step_id in question_steps.items():
        if not isinstance(step_id, str) or not step_id:
            raise WorkspaceIntegrityError(
                f"问题 {question_id} 的步骤绑定必须是非空字符串"
            )


def _check_run_artifacts(pending: list[AnyRecord], root: Path | None) -> None:
    """Verify all new-record archives before commit; shared material checks do not cover
    run archives.
    """
    if root is None:
        return
    problems = verify_artifact_refs(pending, root)
    if problems:
        raise WorkspaceIntegrityError(
            "新记录引用的归档文件核验未通过：" + "；".join(problems)
        )


def _list_questions(
    tx: StoreTransaction, execution_id: str | None
) -> list[QuestionIndexRow]:
    if execution_id is None:
        rows = tx.conn.execute(
            "SELECT question_id, execution_id, step_id, latest_revision, status"
            " FROM questions ORDER BY question_id"
        ).fetchall()
    else:
        rows = tx.conn.execute(
            "SELECT question_id, execution_id, step_id, latest_revision, status"
            " FROM questions WHERE execution_id = ? ORDER BY question_id",
            (execution_id,),
        ).fetchall()
    return [
        QuestionIndexRow(
            question_id=row[0],
            execution_id=row[1],
            step_id=row[2],
            latest_revision=row[3],
            status=row[4],
        )
        for row in rows
    ]


def _check_current_outputs(tx: StoreTransaction, pending: list[AnyRecord]) -> None:
    """Current step outputs must correspond: current papers reference current roster
    revisions.

    New current rosters require same-batch updates of papers still referencing old
    rosters. Historical paper references cannot let incorrect new rosters pass.
    """
    rosters = {r.record_id: r.revision for r in pending if isinstance(r, RosterRecord)}
    if not rosters:
        return
    papers = {r.record_id: r for r in pending if isinstance(r, PaperRecord)}
    for roster_id, revision in rosters.items():
        # Read only current papers referencing affected rosters, not all history.
        rows = tx.conn.execute(
            "SELECT r.payload FROM records r JOIN current_revisions c"
            " ON r.record_id = c.record_id AND r.revision = c.revision"
            " WHERE r.schema_name = 'paper'"
            " AND json_extract(r.payload, '$.data.roster_ref.record_id') = ?",
            (roster_id,),
        ).fetchall()
        affected = {r.record_id: r for r in (parse_record(row[0]) for row in rows)}
        affected.update(papers)
        for paper in affected.values():
            ref = paper.data.roster_ref
            if ref.record_id == roster_id and ref.revision != revision:
                raise WorkspaceIntegrityError(
                    f"声明的当前输出不一致：论文 {paper.record_id} 当前修订"
                    f" v{paper.revision} 仍引用名单 {roster_id} v{ref.revision}"
                    f"（当前修订 v{revision}）；新名单必须与引用它的论文贡献一起更新"
                )
