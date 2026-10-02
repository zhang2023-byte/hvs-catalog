"""材料准备模块：来源包展开、正文／表格解析与准备输入交付。

公共入口见 ``service``（``MaterialsService.prepare_materials``／
``resume_preparation``／``get_prepared_input``）；流程编排见 ``flow``，
外部转换隔离见 ``sandbox``，编号范围展开见 ``ranges``，CSV／ECSV 逻辑
记录读取在共用记录包 ``records.tabular``（保存、备份与消费一致）。
"""

from __future__ import annotations

from .expansion import (
    ExpandedMember,
    ExpandedPackage,
    MaterialExpansionError,
    expand_source_package,
)
from .flow import (
    FLOW_ID,
    FLOW_VERSION,
    MaterialsFlow,
    build_bundle,
)
from .ranges import MAX_RANGE_MEMBERS, RangeExpansion, expand_range_notation
from .sandbox import (
    ConverterAvailability,
    ConverterRunner,
    SandboxedConverterRunner,
    sandbox_profile,
)
from .service import (
    MaterialsPreparationError,
    MaterialsService,
    PreparationHandle,
    PreparationReport,
)
from .texgraph import (
    BODY_UNAVAILABLE_MULTIPLE_ROOTS,
    BODY_UNAVAILABLE_NO_ROOT,
    ManuscriptGraph,
    resolve_manuscript,
)

__all__ = [
    "BODY_UNAVAILABLE_MULTIPLE_ROOTS",
    "BODY_UNAVAILABLE_NO_ROOT",
    "FLOW_ID",
    "FLOW_VERSION",
    "MAX_RANGE_MEMBERS",
    "ConverterAvailability",
    "ConverterRunner",
    "ExpandedMember",
    "ExpandedPackage",
    "ManuscriptGraph",
    "MaterialExpansionError",
    "MaterialsFlow",
    "MaterialsPreparationError",
    "MaterialsService",
    "PreparationHandle",
    "PreparationReport",
    "RangeExpansion",
    "SandboxedConverterRunner",
    "build_bundle",
    "expand_range_notation",
    "expand_source_package",
    "resolve_manuscript",
    "sandbox_profile",
]
