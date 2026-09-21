"""记录与 JSON 的相互转换；读回内容不换算、不改写。"""

from __future__ import annotations

import json
from pathlib import Path

from .common import Record, RecordFormatError
from .registry import RECORD_MODELS, AnyRecord


def parse_record(payload: str | dict[str, object]) -> AnyRecord:
    """解析单份记录；未知记录类型或版本被拒绝。"""
    obj = json.loads(payload) if isinstance(payload, str) else payload
    if not isinstance(obj, dict):
        raise RecordFormatError("记录必须是 JSON 对象")
    tag = obj.get("schema")
    name = tag.get("name") if isinstance(tag, dict) else None
    if not isinstance(name, str) or name not in RECORD_MODELS:
        raise RecordFormatError(f"未知记录类型：{name!r}")
    return RECORD_MODELS[name].model_validate(obj)


def record_to_json(record: Record[object]) -> str:
    return (
        json.dumps(
            record.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2
        )
        + "\n"
    )


def parse_records(payload: str) -> list[AnyRecord]:
    """解析记录集合（JSON 数组）；外层结构或成员损坏时报错。"""
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
