"""Gold error types distinguish reading, checking, saving, and recovery failures."""

from __future__ import annotations


class GoldError(ValueError):
    """Base failure type for Gold operations."""


class GoldFormatError(GoldError):
    """Gold document violates structural definitions, such as unknown versions/fields or
    invalid field shapes.
    """


class LegacyFormatError(GoldFormatError):
    """Legacy contribution Gold lies outside supported reading scope."""


class GoldCheckError(GoldError):
    """Checking found save-blocking structural, material, or locator issues."""


class GoldApprovalError(GoldError):
    """Approval bindings disagree with specific pair contents, target preconditions, or
    rule versions.
    """


class GoldTargetError(GoldError):
    """Official target fails save prerequisites, such as unmanaged contents or changed
    state.
    """


class GoldOutputConflictError(GoldError):
    """Migration output overlaps read-only input or existing results; refuse writes to
    protect originals.
    """


class GoldIntegrityError(GoldError):
    """Save state has unexplained inconsistency; preserve it and stop without overwriting
    while claiming recovery.
    """
