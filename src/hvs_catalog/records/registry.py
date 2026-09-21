"""记录类型注册表：schema.name 到具体记录模型的唯一映射。"""

from __future__ import annotations

from .common import Record
from .objects import ExternalRecord, ObjectRecord
from .paper import PaperRecord
from .questions import DecisionRecord, QuestionRecord
from .roster import RosterRecord
from .runs import ReleaseRecord, RunRecord
from .source import EvidenceRecord, SourcePackageRecord

AnyRecord = (
    SourcePackageRecord
    | EvidenceRecord
    | PaperRecord
    | RosterRecord
    | ObjectRecord
    | ExternalRecord
    | QuestionRecord
    | DecisionRecord
    | RunRecord
    | ReleaseRecord
)

RECORD_MODELS: dict[str, type[Record[object]]] = {
    model.RECORD_NAME: model  # type: ignore[misc]
    for model in (
        SourcePackageRecord,
        EvidenceRecord,
        PaperRecord,
        RosterRecord,
        ObjectRecord,
        ExternalRecord,
        QuestionRecord,
        DecisionRecord,
        RunRecord,
        ReleaseRecord,
    )
}

RECORD_TYPE_NAMES: tuple[str, ...] = tuple(sorted(RECORD_MODELS))
