"""Public arXiv acquisition interfaces for Lane A protocol/transport/throttling and Lane B
lifecycle/archive checks.

Status/list reads never access networks or initialize implicitly; explicit
discovery/fetch/revision checks do. Freeze versions before download and never silently
choose newer versions on recovery. Local-machine throttle state is shared across
workspaces. RateLimitUnconfirmedError requires coordinated manual recovery; no entry
point resets automatically.
"""

from __future__ import annotations

from .lifecycle import (
    AcquisitionError,
    AcquisitionResult,
    AcquisitionService,
    DiscoveryRoundStatus,
    ImportResult,
    PackageRejectionError,
    RevisionCheckResult,
    RevisionStatus,
    SelectedMaterial,
    SelectionStatus,
    SourceUnavailableError,
    discovery_round_id,
)
from .protocol import (
    ArxivApiService,
    ArxivIdentifier,
    ArxivSearchPage,
    ArxivSearchRequest,
    parse_arxiv_identifier,
)

__all__ = [
    "AcquisitionError",
    "AcquisitionResult",
    "AcquisitionService",
    "ArxivApiService",
    "ArxivIdentifier",
    "ArxivSearchPage",
    "ArxivSearchRequest",
    "DiscoveryRoundStatus",
    "ImportResult",
    "PackageRejectionError",
    "RevisionCheckResult",
    "RevisionStatus",
    "SelectedMaterial",
    "SelectionStatus",
    "SourceUnavailableError",
    "discovery_round_id",
    "parse_arxiv_identifier",
]
