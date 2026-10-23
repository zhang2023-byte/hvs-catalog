"""Record types used by preparation, roster extraction, and the runtime."""

from .common import Record
from .objects import ExternalRecord
from .quantities import QuantityRecord
from .questions import DecisionRecord, QuestionRecord
from .roster import RosterRecord
from .runs import ReleaseRecord, RunRecord
from .source import EvidenceRecord, SourcePackageRecord

AnyRecord = (
    ExternalRecord
    | SourcePackageRecord
    | EvidenceRecord
    | RosterRecord
    | QuantityRecord
    | RunRecord
    | ReleaseRecord
    | QuestionRecord
    | DecisionRecord
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
        ReleaseRecord,
        QuestionRecord,
        DecisionRecord,
    )
}
RECORD_TYPE_NAMES = tuple(sorted(RECORD_MODELS))
