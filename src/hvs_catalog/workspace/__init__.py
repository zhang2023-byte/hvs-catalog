"""工作空间模块：目录、锁、业务事务、操作回执与备份恢复。

公共入口见 ``layout``（初始化与打开）、``locking``（排他锁）、
``store``（记录与事务）、``backup``（备份与恢复）；错误类型见
``errors``。使用方式与设计约束见
[架构设计](../../../docs/design/architecture.md#工作空间与恢复接口方案)。
"""

from __future__ import annotations

from .backup import (
    BackupReport,
    backup_workspace,
    checkpoint_state_problems,
    read_backup_manifest,
    restore_workspace,
    verify_record_artifacts,
)
from .errors import (
    AdjudicationNotAllowedError,
    DecisionConflictError,
    OperationKeyConflictError,
    RecoveryInconsistentError,
    RevisionStaleError,
    UnknownWorkspaceError,
    WorkspaceBusyError,
    WorkspaceConflictError,
    WorkspaceError,
    WorkspaceIntegrityError,
)
from .layout import (
    BACKUPS_DIR,
    BUSINESS_DB,
    CHECKPOINT_DB,
    CONFIG_FILENAME,
    RUNS_DIR,
    SOURCE_DIR,
    WORKSPACE_FILENAME,
    SpaceIdentity,
    Workspace,
    initialize_workspace,
    open_workspace,
    resolve_within,
)
from .locking import SpaceLock
from .store import (
    CommitRequest,
    ExecutionRow,
    QuestionIndexRow,
    Receipt,
    ReceiptRow,
    RecordStore,
    StepResultRow,
    StoreTransaction,
    commit_request_digest,
    verify_artifact_refs,
    walk_artifact_refs,
)

__all__ = [
    "BACKUPS_DIR",
    "BUSINESS_DB",
    "CHECKPOINT_DB",
    "CONFIG_FILENAME",
    "RUNS_DIR",
    "SOURCE_DIR",
    "WORKSPACE_FILENAME",
    "AdjudicationNotAllowedError",
    "BackupReport",
    "CommitRequest",
    "DecisionConflictError",
    "ExecutionRow",
    "OperationKeyConflictError",
    "QuestionIndexRow",
    "Receipt",
    "ReceiptRow",
    "RecordStore",
    "RecoveryInconsistentError",
    "RevisionStaleError",
    "SpaceIdentity",
    "SpaceLock",
    "StepResultRow",
    "StoreTransaction",
    "UnknownWorkspaceError",
    "Workspace",
    "WorkspaceBusyError",
    "WorkspaceConflictError",
    "WorkspaceError",
    "WorkspaceIntegrityError",
    "backup_workspace",
    "checkpoint_state_problems",
    "commit_request_digest",
    "initialize_workspace",
    "open_workspace",
    "read_backup_manifest",
    "resolve_within",
    "restore_workspace",
    "verify_artifact_refs",
    "verify_record_artifacts",
    "walk_artifact_refs",
]
