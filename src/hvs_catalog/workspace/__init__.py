"""Workspace directories, locks, business transactions, receipts, and backup/restore.

layout initializes/opens; locking provides exclusion; store handles
records/transactions; backup handles backup/restore; errors defines failures. See
../../../docs/pipeline.md for usage/constraints.
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
