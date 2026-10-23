"""Record/JSON conversion without converting or rewriting read-back contents."""

from __future__ import annotations

import json
from pathlib import Path

from .common import Record, RecordFormatError
from .registry import RECORD_MODELS, AnyRecord


def parse_record(payload: str | dict[str, object]) -> AnyRecord:
    """Parse records and reject unknown types/versions."""
    obj = json.loads(payload) if isinstance(payload, str) else payload
    if not isinstance(obj, dict):
        raise RecordFormatError("记录必须是 JSON 对象")
    tag = obj.get("schema")
    name = tag.get("name") if isinstance(tag, dict) else None
    if not isinstance(name, str) or name not in RECORD_MODELS:
        raise RecordFormatError(f"未知记录类型：{name!r}")
    if name == "source_package" and "gaps" in obj.get("data", {}):
        # Read existing preparation metadata without changing its stored bytes.
        # Only parser descriptions survive in the current source view.
        data = dict(obj["data"])
        gaps = data.pop("gaps")
        data["preparation_issues"] = [g["description"] for g in gaps]
        obj = {**obj, "data": data}
    return RECORD_MODELS[name].model_validate(obj)


def record_to_json(record: Record[object]) -> str:
    return (
        json.dumps(
            record.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2
        )
        + "\n"
    )


def parse_records(payload: str) -> list[AnyRecord]:
    """Parse JSON record arrays, rejecting damaged envelopes/members."""
    obj = json.loads(payload)
    if not isinstance(obj, list):
        raise RecordFormatError("记录集合必须是 JSON 数组")
    records: list[AnyRecord] = []
    for item in obj:
        if isinstance(item, (str, dict)):
            records.append(parse_record(item))
        else:
            raise RecordFormatError("记录集合成员必须是 JSON 对象")
    return records


def records_to_json(records: list[Record[object]]) -> str:
    return (
        json.dumps(
            [record.model_dump(mode="json", by_alias=True) for record in records],
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def save_records(path: str | Path, records: list[Record[object]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(records_to_json(records), encoding="utf-8")


def load_records(path: str | Path) -> list[AnyRecord]:
    return parse_records(Path(path).read_text(encoding="utf-8"))
