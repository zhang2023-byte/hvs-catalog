"""Material preparation: package expansion, body/table parsing, and prepared-input
delivery.

Public service entry points include prepare_materials, resume_preparation, and
get_prepared_input. flow orchestrates; sandbox isolates external converters; ranges
expands identifiers. Shared records.tabular reads CSV/ECSV logical records consistently
for saving, backup, and consumption.
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
