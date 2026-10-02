"""Pure result patches sharing validation/application target and field rules.

Never write databases, allocate revisions, or mutate inputs. Runtime assigns revisions,
applies here, and commits with collection validation. Item targets alter item fields
only; record fields need explicit record targets.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError as PydanticValidationError

from .common import Record
from .paper import ObjectContribution, PaperRecord
from .questions import DecisionPatch
from .roster import RosterEntry, RosterRecord

UNSUPPORTED_ITEM_TARGET = object()


def locate_patch_item(record: Record, item_id: str) -> Any:
    """Resolve targeted item models, returning a sentinel for unsupported locations."""
    if isinstance(record, PaperRecord):
        return next(
            (
                contribution
                for contribution in record.data.object_contributions
                if contribution.item_id == item_id
            ),
            None,
        )
    if isinstance(record, RosterRecord):
        return next(
            (entry for entry in record.data.entries if entry.item_id == item_id), None
        )
    return UNSUPPORTED_ITEM_TARGET


def apply_item_changes(item: Any, changes: list) -> Any:
    """Merge into item copies and validate the whole result, not intermediate fields."""
    item_model = type(item)
    payload = item.model_dump(mode="json")
    for change in changes:
        if change.field not in item_model.model_fields:
            raise PatchApplicationError(
                [
                    (
                        f"补丁字段 {change.field!r} 不在目标条目模型"
                        f" {item_model.__name__} 中；条目目标不能修改记录级字段"
                    )
                ]
            )
        payload[change.field] = change.value
    try:
        return item_model.model_validate(payload)
    except PydanticValidationError as exc:
        fields = sorted(
            {str(error["loc"][0]) for error in exc.errors() if error["loc"]}
        )
        raise PatchApplicationError(
            [f"补丁修改后的条目不满足字段或跨字段约束（涉及 {fields}）"]
        ) from exc


class PatchApplicationError(ValueError):
    """Patch violates shared application definitions; reasons aggregates issues."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


def apply_decision_patch(record: Record, patch: DecisionPatch) -> Record:
    """Return patched copies with unchanged revision/time/execution bindings.

    Missing/unsupported targets raise PatchApplicationError. Callers check question
    scope, expected revisions, and complete collections.
    """
    item = locate_patch_item(record, patch.target.item_id)
    if item is UNSUPPORTED_ITEM_TARGET:
        raise PatchApplicationError(
            [
                (
                    f"补丁目标记录 {record.record_id}（{record.RECORD_NAME}）"
                    "不支持条目级修改"
                )
            ]
        )
    if item is None:
        raise PatchApplicationError(
            [
                (
                    f"补丁目标条目 {patch.target.item_id} 不在 "
                    f"{record.RECORD_NAME} 记录 {record.record_id} 中"
                )
            ]
        )
    patched_item = apply_item_changes(item, patch.changes)
    if isinstance(record, PaperRecord) and isinstance(patched_item, ObjectContribution):
        contributions = [
            patched_item if c.item_id == patched_item.item_id else c
            for c in record.data.object_contributions
        ]
        return record.model_copy(
            update={
                "data": record.data.model_copy(
                    update={"object_contributions": contributions}
                )
            }
        )
    if isinstance(record, RosterRecord) and isinstance(patched_item, RosterEntry):
        entries = [
            patched_item if e.item_id == patched_item.item_id else e
            for e in record.data.entries
        ]
        return record.model_copy(
            update={"data": record.data.model_copy(update={"entries": entries})}
        )
    raise PatchApplicationError(  # pragma: no cover - locate already restricts types
        [f"补丁目标条目类型不受支持：{type(patched_item).__name__}"]
    )
