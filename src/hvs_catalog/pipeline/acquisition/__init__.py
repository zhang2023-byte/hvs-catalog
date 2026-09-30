"""arXiv 获取子系统的公共接口。

Lane A（协议/传输/限流）与 Lane B（生命周期/归档检查）的稳定公共面。
工程边界：

- 状态读取与列表不触网、不隐式初始化；网络只在显式执行
  发现/获取/修订检查时发生。
- 版本先冻结后下载；恢复绝不悄悄改选更新版本。
- 限流状态跨工作空间共享（本机范围）；出现
  :class:`~hvs_catalog.pipeline.acquisition.rate_limit.RateLimitUnconfirmedError`
  时需要人工协调恢复，任何入口都不会自动重置。
"""

from __future__ import annotations

from .archive import (
    MAX_ARCHIVE_MEMBERS,
    MAX_PACKAGE_BYTES,
    MAX_TOTAL_DECOMPRESSED_BYTES,
    PackageInspection,
    PackageIntegrityError,
    PackageMember,
    UnsupportedPackageError,
    inspect_package,
    sniff_package_format,
)
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
    "MAX_ARCHIVE_MEMBERS",
    "MAX_PACKAGE_BYTES",
    "MAX_TOTAL_DECOMPRESSED_BYTES",
    "AcquisitionError",
    "AcquisitionResult",
    "AcquisitionService",
    "ArxivApiService",
    "ArxivIdentifier",
    "ArxivSearchPage",
    "ArxivSearchRequest",
    "DiscoveryRoundStatus",
    "ImportResult",
    "PackageInspection",
    "PackageIntegrityError",
    "PackageMember",
    "PackageRejectionError",
    "RevisionCheckResult",
    "RevisionStatus",
    "SelectedMaterial",
    "SelectionStatus",
    "SourceUnavailableError",
    "UnsupportedPackageError",
    "discovery_round_id",
    "inspect_package",
    "parse_arxiv_identifier",
    "sniff_package_format",
]
