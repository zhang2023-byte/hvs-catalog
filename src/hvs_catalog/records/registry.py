"""Record types used by preparation, roster extraction, and the runtime."""

from .common import Record
from .objects import ExternalRecord
from .quantities import QuantityRecord
from .roster import RosterRecord
from .runs import RunRecord
from .source import EvidenceRecord, SourcePackageRecord

AnyRecord = (
    ExternalRecord
    | SourcePackageRecord
    | EvidenceRecord
    | RosterRecord
    | QuantityRecord
    | RunRecord
)
RECORD_MODELS: dict[str, type[Record]] = {
    model.RECORD_NAME: model
    for model in (
        ExternalRecord,
        SourcePackageRecord,
        EvidenceRecord,
        RosterRecord,
        QuantityRecord,
        RunRecord,
    )
}
RECORD_TYPE_NAMES = tuple(sorted(RECORD_MODELS))
