"""Scoring errors.

ScoringInputError means caller input violates the public contract, rejecting the batch
without silent overwrites. Distinguish auxiliary enablement and frozen-storage failures
for tests/reports.
"""

from __future__ import annotations


class ScoringError(Exception):
    """Base scoring error."""


class ScoringInputError(ScoringError):
    """Plan, Gold, or prediction violates the public contract; reject the batch."""


class AuxiliaryEnablementError(ScoringError):
    """Auxiliary scoring Agent fails enablement requirements and is denied by default."""


class FrozenStoreError(ScoringError):
    """Frozen judgment integrity or content-conflict error."""
