"""Public workspace errors.

Distinguish
busy/unknown-format/corruption/stale-revision/inconsistent-recovery
cases and same-operation-key conflicts. Call sites supply execution, step, or record
references in diagnostics.
"""

from __future__ import annotations


class WorkspaceError(Exception):
    """Base workspace error."""


class WorkspaceBusyError(WorkspaceError):
    """Another executor holds the workspace lock; this operation did not run."""


class UnknownWorkspaceError(WorkspaceError):
    """Directory is uninitialized or has an unknown workspace marker."""


class WorkspaceConflictError(WorkspaceError):
    """Existing workspace contents conflict, such as reserved names, existing targets, or
    different overwrite contents.
    """


class WorkspaceIntegrityError(WorkspaceError):
    """Corrupted records/materials/structure prevent continuation from saved state."""


class RevisionStaleError(WorkspaceError):
    """Expected current revision differs from actual; no change committed."""


class RecoveryInconsistentError(WorkspaceError):
    """Fixed input/checkpoints/business records disagree, blocking recovery."""


class OperationKeyConflictError(WorkspaceError):
    """The operation key belongs to different contents; retries need identical contents or
    a new key.
    """
