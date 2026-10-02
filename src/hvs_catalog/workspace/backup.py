"""Backup/restore: locked snapshots, complete manifests, fresh targets, and consistency
checks.

Use SQLite backups for business/checkpoint databases under workspace locks, copy all
referenced source/run files and required configuration, then write completion markers
and publish only after validation. Restore in sibling staging directories after
hash/database/all-reference checks, publishing fresh openable targets while retaining
logical identities/execution IDs.

Verify archives at every historical revision/request/response by saved hashes and
checkpoint execution/state bindings per thread. Completed runs also require final
checkpoint IDs saved atomically with completion revisions, checked through public
framework interfaces independently of specific flows. Backup/restore/completion share
validators.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..records.common import RecordRef
from .errors import (
    WorkspaceConflictError,
    WorkspaceIntegrityError,
)
from .layout import (
    BUSINESS_DB,
    CHECKPOINT_DB,
    CONFIG_FILENAME,
    WORKSPACE_FILENAME,
    Workspace,
    _fsync_dir,
    open_workspace,
    resolve_within,
)
from .store import verify_artifact_refs, walk_artifact_refs

BACKUP_MANIFEST = "backup-manifest.json"
BACKUP_COMPLETE = "complete"

_MANIFEST_FORMAT = "hvs-catalog-backup"
_MANIFEST_VERSION = 1

FRAMEWORK_FINAL_META_PREFIX = "framework_final_checkpoint:"


def framework_final_key(execution_id: str) -> str:
    """Business meta key for final checkpoint-ID completion credentials."""
    return f"{FRAMEWORK_FINAL_META_PREFIX}{execution_id}"


def framework_final_checkpoint(space: Workspace, execution_id: str) -> str | None:
    return space.store().get_meta(framework_final_key(execution_id))


@dataclass
class BackupReport:
    backup_dir: Path
    files: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _referenced_files(space: Workspace) -> list[str]:
    """Collect files referenced by saved records/checkpoints relative to the root.

    Supplement conventional rule archives from run manifests because record data stores
    hashes without paths. Include standard {path, sha256} checkpoint attachments such as
    body/ECSV/input views so restored consumers can read everything.
    """
    from ..runtime.execution import rules_archive_rel

    paths: list[str] = []
    for record in space.store().list_records():
        for ref in walk_artifact_refs(record.model_dump(mode="json")):
            paths.append(ref["path"])
    for row in space.store().list_executions():
        paths.append(rules_archive_rel(row.execution_id))
    paths.extend(_checkpoint_artifact_paths(space))
    return sorted(set(paths))


def _checkpoint_artifact_paths(space: Workspace) -> list[str]:
    """Read archive paths referenced by execution checkpoint state without treating them as
    records.
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = space.checkpoint_connection()
    paths: list[str] = []
    try:
        saver = SqliteSaver(conn)
        for tup in saver.list(None):
            values = (
                dict(tup.checkpoint.get("channel_values") or {})
                if tup.checkpoint is not None
                else {}
            )
            for ref in walk_artifact_refs(values):
                paths.append(ref["path"])
    finally:
        conn.close()
    return paths


def verify_record_artifacts(space: Workspace) -> list[str]:
    """Verify all archives against saved hashes, covering historical revisions and every
    request/response.
    """
    return verify_artifact_refs(space.store().list_records(), space.root)


def walk_state_refs(value: object) -> list[RecordRef]:
    """Collect record_id/revision-shaped references from state payloads."""
    refs: list[RecordRef] = []
    if isinstance(value, dict):
        if set(value) >= {"record_id", "revision"} and "item_id" not in value:
            try:
                refs.append(RecordRef.model_validate(value))
            except Exception:  # noqa: BLE001 - skip non-reference shapes
                for item in value.values():
                    refs.extend(walk_state_refs(item))
        else:
            for item in value.values():
                refs.extend(walk_state_refs(item))
    elif isinstance(value, list):
        for item in value:
            refs.extend(walk_state_refs(item))
    return refs


def checkpoint_state_problems(
    space: Workspace, execution_id: str | None = None
) -> list[str]:
    """Shared checkpoint checks compare execution bindings/state references with business
    data.

    Equal thread IDs do not prove content equality. Read each state and allow legitimate
    committed-business/lagging-framework stages, rejecting unexplained mismatches.
    _framework_completion_problems separately proves actual terminal framework nodes.
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    store = space.store()
    executions = (
        {execution_id}
        if execution_id is not None
        else {row.execution_id for row in store.list_executions()}
    )
    conn = space.checkpoint_connection()
    problems: list[str] = []
    try:
        saver = SqliteSaver(conn)
        threads = (
            {execution_id}
            if execution_id is not None
            else {tup.config["configurable"]["thread_id"] for tup in saver.list(None)}
        )
        for thread_id in sorted(threads):
            if thread_id not in executions:
                problems.append(f"检查点线程 {thread_id} 没有对应的执行记录")
                continue
            tup = saver.get_tuple({"configurable": {"thread_id": thread_id}})
            values = (
                dict(tup.checkpoint.get("channel_values") or {})
                if tup is not None and tup.checkpoint is not None
                else {}
            )
            if not values:
                problems.append(f"检查点线程 {thread_id} 没有可读状态")
                continue
            bound = values.get("execution_id")
            if isinstance(bound, str) and bound != thread_id:
                problems.append(f"检查点状态绑定执行 {bound}，当前执行为 {thread_id}")
            # Verify {path, sha256} attachments against saved hashes through shared checks;
            # Shared consumption/recovery/completion/backup checks prevent completed status from bypassing
            # checks.
            for ref in walk_artifact_refs(values):
                path = space.root / ref["path"]
                try:
                    import hashlib

                    actual = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    problems.append(
                        f"检查点附件缺失或不可读：{ref['path']}（执行 {thread_id}）"
                    )
                    continue
                if actual != ref["sha256"]:
                    problems.append(
                        f"检查点附件与保存哈希不符：{ref['path']}"
                        f"（期望 {ref['sha256'][:12]}，实际 {actual[:12]}）"
                    )
            for ref in walk_state_refs(values):
                record = store.read_record(ref)
                if record is None:
                    problems.append(
                        f"检查点引用的业务结果缺失：{ref.record_id} v{ref.revision}"
                    )
                elif (
                    record.execution_id is not None and record.execution_id != thread_id
                ):
                    problems.append(
                        f"检查点引用了其他执行的记录：{ref.record_id}"
                        f"（属于 {record.execution_id}）"
                    )
    finally:
        conn.close()
    for missing_id in sorted(executions - threads):
        problems.append(f"执行 {missing_id} 没有对应的检查点线程")
    problems.extend(_framework_completion_problems(space, execution_id))
    return problems


def _framework_completion_problems(
    space: Workspace, execution_id: str | None = None
) -> list[str]:
    """Completion credentials must match latest public checkpoint IDs; never fabricate
    missing credentials.
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    from ..records.runs import RunRecord

    store = space.store()
    conn = space.checkpoint_connection()
    problems: list[str] = []
    try:
        saver = SqliteSaver(conn)
        rows = (
            [store.get_execution(execution_id)]
            if execution_id is not None
            else store.list_executions()
        )
        for row in rows:
            if row is None:
                problems.append(f"执行 {execution_id} 缺失")
                continue
            run = store.read_record_current(row.run_record_id)
            if not isinstance(run, RunRecord) or run.data.state != "finished":
                continue
            final_id = framework_final_checkpoint(space, row.execution_id)
            if final_id is None:
                problems.append(
                    f"执行 {row.execution_id} 标记完成，但缺少框架完成凭据；"
                    "不能直接视为已完成，不自动补记"
                )
                continue
            tup = saver.get_tuple({"configurable": {"thread_id": row.execution_id}})
            latest = (
                str(tup.checkpoint.get("id"))
                if tup is not None and tup.checkpoint
                else None
            )
            if latest is None:
                problems.append(f"执行 {row.execution_id} 标记完成，但没有框架检查点")
            elif latest != final_id:
                problems.append(
                    f"执行 {row.execution_id} 标记完成，但框架检查点已偏离"
                    f"完成时状态（完成凭据 {final_id}，最新 {latest}）；"
                    "仍有待执行节点的执行不能按已完成接受"
                )
    finally:
        conn.close()
    return problems


def _verify_space_consistency(space: Workspace) -> list[str]:
    """Shared backup/restore checks for collections, materials, and checkpoint bindings."""
    from ..records.validate import CollectionValidationError, validate_collection

    problems: list[str] = []
    store = space.store()
    records = store.list_records()
    try:
        validate_collection(records, space.root)
    except CollectionValidationError as exc:
        problems.append(f"记录集合校验失败：{exc}")
    except Exception as exc:  # noqa: BLE001 - report corrupted databases as issues
        problems.append(f"记录集合无法读取：{exc}")
    # Verify referenced files against original hashes before building backup manifests.
    problems.extend(verify_record_artifacts(space))
    # Reject corrupted fixed inputs during backup so restored executions can continue.
    from ..runtime.execution import get_execution, verify_execution_inputs

    for row in store.list_executions():
        try:
            handle = get_execution(space, row.execution_id)
        except WorkspaceIntegrityError as exc:
            problems.append(str(exc))
            continue
        problems.extend(verify_execution_inputs(space, handle))
    # Beyond thread identity, verify execution bindings and state references.
    problems.extend(checkpoint_state_problems(space))
    return problems


def backup_workspace(space: Workspace, target: Path) -> BackupReport:
    """Build complete backups under lock; publish completion markers only after every check
    passes.
    """
    target = Path(target)
    if target.exists():
        raise WorkspaceConflictError(f"备份目标已存在，拒绝覆盖：{target}")
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f".backup-tmp-{uuid.uuid4().hex[:12]}"

    with space.lock():
        problems = _verify_space_consistency(space)
        if problems:
            raise WorkspaceIntegrityError(
                "空间一致性核对未通过，不生成备份：" + "；".join(problems)
            )
        staging.mkdir()
        entries: list[dict] = []
        try:
            for db_name in (BUSINESS_DB, CHECKPOINT_DB):
                source = sqlite3.connect(
                    f"file:{space.root / db_name}?mode=ro", uri=True
                )
                try:
                    snapshot = sqlite3.connect(staging / db_name)
                    source.backup(snapshot)
                    snapshot.close()
                finally:
                    source.close()
                entries.append(
                    {
                        "path": db_name,
                        "size": (staging / db_name).stat().st_size,
                        "sha256": _sha256_file(staging / db_name),
                        "role": "database",
                    }
                )
            for rel in _referenced_files(space):
                # Resolve each component within the root; reject intermediate-directory symlinks too.
                source = resolve_within(space.root, rel)
                if not source.is_file():
                    raise WorkspaceIntegrityError(f"引用文件不可读：{rel}")
                dest = staging / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, dest)
                entries.append(
                    {
                        "path": rel,
                        "size": dest.stat().st_size,
                        "sha256": _sha256_file(dest),
                        "role": "material",
                    }
                )
            for name in (WORKSPACE_FILENAME, CONFIG_FILENAME):
                source = space.root / name
                if source.is_file():
                    shutil.copyfile(source, staging / name)
                    entries.append(
                        {
                            "path": name,
                            "size": source.stat().st_size,
                            "sha256": _sha256_file(staging / name),
                            "role": "space_config",
                        }
                    )
            manifest = {
                "format": _MANIFEST_FORMAT,
                "format_version": _MANIFEST_VERSION,
                "space": space.identity.marker_payload(),
                "files": entries,
                "state": "incomplete",
            }
            (staging / BACKUP_MANIFEST).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            # Recheck every file against its recorded hash.
            for entry in entries:
                actual = _sha256_file(staging / entry["path"])
                if actual != entry["sha256"]:
                    raise WorkspaceIntegrityError(f"备份文件复核失败：{entry['path']}")
            manifest["state"] = BACKUP_COMPLETE
            (staging / BACKUP_MANIFEST).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            _fsync_dir(staging)
            os.replace(staging, target)
            _fsync_dir(parent)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return BackupReport(backup_dir=target, files=entries)


def read_backup_manifest(backup_dir: Path) -> dict:
    path = Path(backup_dir) / BACKUP_MANIFEST
    if path.is_symlink():
        raise WorkspaceIntegrityError(f"备份清单是符号链接，拒绝读取：{path}")
    if not path.is_file():
        raise WorkspaceIntegrityError(
            f"{backup_dir} 没有备份清单：无完整标记的备份不可用"
        )
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceIntegrityError(f"备份清单无法读取：{exc}") from exc
    if manifest.get("state") != BACKUP_COMPLETE:
        raise WorkspaceIntegrityError(
            f"{backup_dir} 的备份没有完成标记：无完整标记的备份不可用"
        )
    return manifest


def restore_workspace(backup_dir: Path, target: Path) -> Workspace:
    """Restore complete backups to fresh directories, validating before publication."""
    backup_dir = Path(backup_dir)
    manifest = read_backup_manifest(backup_dir)
    target = Path(target)
    if target.exists():
        raise WorkspaceConflictError(f"恢复目标已存在，拒绝覆盖：{target}")
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f".restore-tmp-{uuid.uuid4().hex[:12]}"
    staging.mkdir()

    seen: set[str] = set()
    try:
        for entry in manifest["files"]:
            rel = entry["path"]
            if rel in seen:
                raise WorkspaceIntegrityError(f"备份清单出现重复目标路径：{rel}")
            seen.add(rel)
            # Verify each component for symlinks/escapes, preventing reads outside backup roots;
            # checking only leaf files misses intermediate-directory escapes.
            source = resolve_within(backup_dir, rel)
            if not source.is_file():
                raise WorkspaceIntegrityError(f"备份文件缺失或不是普通文件：{rel}")
            digest = _sha256_file(source)
            if digest != entry["sha256"]:
                raise WorkspaceIntegrityError(f"备份文件哈希不符：{rel}")
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if (
                dest.is_symlink()
            ):  # pragma: no cover - target lies inside a newly created directory
                raise WorkspaceIntegrityError(f"恢复目标包含符号链接：{rel}")
            shutil.copyfile(source, dest)

        for db_name in (BUSINESS_DB, CHECKPOINT_DB):
            conn = sqlite3.connect(f"file:{staging / db_name}?mode=ro", uri=True)
            try:
                row = conn.execute("PRAGMA integrity_check").fetchone()
                if row is None or row[0] != "ok":
                    raise WorkspaceIntegrityError(f"数据库完整性检查未通过：{db_name}")
            finally:
                conn.close()

        # Preserve logical workspace/execution IDs; recreate locks at the new location.
        lock_path = staging / ".lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        os.close(fd)

        restored = open_workspace(staging)
        try:
            problems = _verify_space_consistency(restored)
            if problems:
                raise WorkspaceIntegrityError(
                    "恢复内容一致性核对未通过，不发布可打开空间：" + "；".join(problems)
                )
        finally:
            restored.close()
        os.replace(staging, target)
        _fsync_dir(parent)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return open_workspace(target)
