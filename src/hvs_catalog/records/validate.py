"""Validate fixed source references, runtime records, and immutable roster snapshots."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .common import ArtifactRef, GapRef, ItemRef, RecordRef
from .file_checks import checked_value, file_digest, scoped_verification
from .objects import ExternalRecord
from .quantities import QuantityRecord
from .registry import AnyRecord
from .roster import RosterRecord
from .runs import RunRecord
from .source import DataElementLocator, EvidenceRecord, SourcePackageRecord


@dataclass
class ValidationIssue:
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.where}: {self.message}"


class CollectionValidationError(ValueError):
    """Record collection failed validation; issues aggregates all problems."""

    def __init__(self, issues: list[ValidationIssue]) -> None:
        self.issues = issues
        super().__init__("; ".join(str(issue) for issue in issues))


class _Index:
    def __init__(self, records: list[AnyRecord]) -> None:
        self.by_id: dict[str, dict[int, AnyRecord]] = {}
        for record in records:
            self.by_id.setdefault(record.record_id, {})[record.revision] = record

    def resolve(self, ref: RecordRef) -> AnyRecord | None:
        revisions = self.by_id.get(ref.record_id)
        if revisions is None:
            return None
        return revisions.get(ref.revision)


def _iter_refs(value: Any):
    """Recursively traverse references, yielding (path, reference)."""
    if isinstance(value, (RecordRef, ItemRef, GapRef, ArtifactRef)):
        yield "", value
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from _iter_refs(getattr(value, name))
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_refs(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_refs(item)


def iter_record_refs(value: Any):
    """Reuse shared traversal to yield exact record references, including item/gap
    references.
    """
    for _, ref in _iter_refs(value):
        if isinstance(ref, (RecordRef, ItemRef)):
            yield RecordRef(record_id=ref.record_id, revision=ref.revision)
        elif isinstance(ref, GapRef):
            yield ref.source_ref


def _check_artifact(
    where: str, ref: ArtifactRef, materials_root: Path, issues: list[ValidationIssue]
) -> None:
    path = (materials_root / ref.path).resolve()
    try:
        path.relative_to(materials_root.resolve())
    except ValueError:
        issues.append(ValidationIssue(where, f"文件引用路径越界：{ref.path}"))
        return
    if not path.is_file():
        issues.append(ValidationIssue(where, f"引用文件缺失：{ref.path}"))
        return
    digest = file_digest(path)
    if digest != ref.sha256:
        issues.append(ValidationIssue(where, f"引用文件哈希改变：{ref.path}"))


def _check_locator(
    record: EvidenceRecord,
    index: _Index,
    materials_root: Path,
    issues: list[ValidationIssue],
) -> None:
    source = index.resolve(record.data.source_ref)
    if not isinstance(source, SourcePackageRecord):
        return
    file = next(
        (f for f in source.data.files if f.file_id == record.data.file_id), None
    )
    if file is None:
        return
    path = (materials_root / file.path).resolve()
    if not path.is_file():
        return
    locator = record.data.locator
    where = record.record_id
    if locator.kind in {"pdf", "pdf_image"}:
        from .pdf import pdf_evidence_block

        try:
            block = pdf_evidence_block(
                source,
                materials_root,
                file.file_id,
                locator.page,
                locator.block_id,
                require_image=locator.kind == "pdf_image",
            )
            if locator.kind == "pdf" and (
                not record.data.excerpt or record.data.excerpt not in block["text"]
            ):
                issues.append(
                    ValidationIssue(
                        where, "PDF excerpt differs from the fixed parser block"
                    )
                )
        except (ValueError, KeyError, OSError) as exc:
            issues.append(ValidationIssue(where, str(exc)))
    elif locator.kind == "text":
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        if not (1 <= locator.start_line <= locator.end_line <= len(lines)):
            issues.append(
                ValidationIssue(
                    where, f"行范围越界：{locator.start_line}-{locator.end_line}"
                )
            )
            return
        actual = "\n".join(lines[locator.start_line - 1 : locator.end_line])
        if actual != record.data.excerpt:
            issues.append(ValidationIssue(where, "引文与指定版本的原文行不一致"))
    elif locator.kind == "table_cell":
        # Share preparation's CSV/ECSV logical readers consistently across saving/backup/consumption.
        # Zero matches fail; unique matches need no ordinal; multiple matches require additional keys
        # or explicit ordinals, never silent first-row selection. row_number is one-based among row_key matches,
        # not a physical source line number.
        from .tabular import TabularReadError, read_table_records

        try:
            records = read_table_records(
                path.read_bytes(), media_type=file.media_type, name=file.path
            )
        except TabularReadError as exc:
            issues.append(ValidationIssue(where, f"表格文件无法按逻辑记录读取：{exc}"))
            return
        columns = records.column_list()
        if locator.column not in columns:
            issues.append(ValidationIssue(where, f"表格列不存在：{locator.column}"))
            return
        missing_key_columns = [key for key in locator.row_key if key not in columns]
        if missing_key_columns:
            issues.append(
                ValidationIssue(
                    where,
                    "行键列不存在："
                    + ", ".join(missing_key_columns)
                    + "（不能用不存在的列认证任意对象行）",
                )
            )
            return
        if any(value is None for value in locator.row_key.values()):
            issues.append(ValidationIssue(where, "行键含空值：不能与实际单元格比较"))
            return
        matched = [
            row
            for row in records.rows
            if all(
                str(row.get(key)) == str(value)
                for key, value in locator.row_key.items()
            )
        ]
        if not matched:
            issues.append(ValidationIssue(where, "行键零命中：定位不到对象行"))
            return
        if len(matched) > 1 and locator.row_number is None:
            issues.append(
                ValidationIssue(
                    where,
                    f"行键命中 {len(matched)} 行且未填 row_number："
                    "多命中必须补足行键或明确序号，不默取第一行",
                )
            )
            return
        row_number = locator.row_number or 1
        if not (1 <= row_number <= len(matched)):
            issues.append(
                ValidationIssue(
                    where,
                    f"行序号 {row_number} 超出命中行数 {len(matched)}",
                )
            )
            return
        cell = matched[row_number - 1].get(locator.column)
        if cell != record.data.excerpt:
            issues.append(ValidationIssue(where, "单元格引文与表格内容不一致"))
    elif locator.kind == "data_element":
        if not (
            isinstance(locator, DataElementLocator)
            and locator.selector.kind == "json_pointer"
        ):
            issues.append(
                ValidationIssue(
                    where,
                    f"未支持的材料定位选择器：{locator.selector.kind}"
                    "（材料未验证，不能视为已通过材料核对）",
                )
            )
            return
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            value = _resolve_json_pointer(document, locator.selector.value)
        except (json.JSONDecodeError, LookupError, ValueError, TypeError) as exc:
            issues.append(ValidationIssue(where, f"JSON 定位无法解析：{exc}"))
            return
        expected = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        )
        if expected != record.data.excerpt:
            issues.append(ValidationIssue(where, "数据元素引文与解码值不一致"))


def _resolve_json_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise ValueError(f"非法 JSON Pointer：{pointer}")
    value = document
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            if token not in value:
                raise LookupError(f"JSON Pointer 缺少键：{token}")
            value = value[token]
        elif isinstance(value, list):
            index = int(token) if token.isdigit() else None
            if index is None:
                raise TypeError(f"JSON Pointer 数组下标必须是数字：{token}")
            if index >= len(value):
                raise LookupError(f"JSON Pointer 数组越界：{index}")
            value = value[index]
        else:
            raise TypeError(f"JSON Pointer 无法继续解析：{token}")
    return value


@scoped_verification
def validate_collection(records, materials_root=None):
    issues = []
    index = _Index(records)
    snapshot_keys = (
        _snapshot_cache_keys(records, index, Path(materials_root))
        if materials_root is not None
        else {}
    )
    seen = set()
    for record in records:
        key = (record.record_id, record.revision)
        if key in seen:
            issues.append(
                ValidationIssue(record.record_id, "Duplicate record revision")
            )
        seen.add(key)
        for ref in iter_record_refs(record.data):
            if index.resolve(ref) is None:
                issues.append(
                    ValidationIssue(
                        record.record_id,
                        f"Missing record: {ref.record_id} v{ref.revision}",
                    )
                )
        source = (
            index.resolve(record.data.source_ref)
            if isinstance(record, (EvidenceRecord, RosterRecord, QuantityRecord))
            else None
        )
        if isinstance(
            record, (EvidenceRecord, RosterRecord, QuantityRecord)
        ) and not isinstance(source, SourcePackageRecord):
            issues.append(
                ValidationIssue(
                    record.record_id, "source_ref must identify a source package"
                )
            )
        if isinstance(record, RunRecord):
            for ref in record.data.input_snapshot.source_refs:
                if not isinstance(index.resolve(ref), SourcePackageRecord):
                    issues.append(
                        ValidationIssue(
                            record.record_id, "Run input must identify a source package"
                        )
                    )
        if (
            isinstance(record, EvidenceRecord)
            and isinstance(source, SourcePackageRecord)
            and (
                record.data.paper_key != source.data.paper_key
                or record.data.file_id not in {f.file_id for f in source.data.files}
            )
        ):
            issues.append(
                ValidationIssue(
                    record.record_id, "Evidence does not belong to the fixed source"
                )
            )
        if materials_root is not None:
            root = Path(materials_root)
            if isinstance(record, SourcePackageRecord):
                for file in record.data.files:
                    _check_artifact(
                        record.record_id,
                        ArtifactRef(path=file.path, sha256=file.sha256),
                        root,
                        issues,
                    )
            elif isinstance(record, ExternalRecord):
                for _, ref in _iter_refs(record.data):
                    if isinstance(ref, ArtifactRef):
                        _check_artifact(record.record_id, ref, root, issues)
            elif isinstance(record, EvidenceRecord):
                _check_locator(record, index, root, issues)
            elif isinstance(record, QuantityRecord) and isinstance(
                source, SourcePackageRecord
            ):
                try:
                    key = snapshot_keys.get((record.record_id, record.revision))
                    if key is None:
                        _check_quantity_snapshot(record, source, index, root)
                    else:
                        checked_value(
                            "quantity-snapshot",
                            key,
                            partial(
                                _check_quantity_snapshot, record, source, index, root
                            ),
                        )
                except (ValueError, KeyError, OSError, TypeError) as exc:
                    issues.append(ValidationIssue(record.record_id, str(exc)))
            elif isinstance(record, RosterRecord) and isinstance(
                source, SourcePackageRecord
            ):
                try:
                    _check_roster_snapshot(record, source, root)
                except (ValueError, KeyError, OSError, TypeError) as exc:
                    issues.append(ValidationIssue(record.record_id, str(exc)))
    if issues:
        raise CollectionValidationError(issues)
    return issues


def _snapshot_cache_keys(records, index, root):
    """Permit reuse only after rechecking all files used by unchanged snapshots."""
    from ..workspace import WorkspaceIntegrityError
    from ..workspace.layout import resolve_within
    from ..workspace.store import verify_artifact_refs

    snapshots = [r for r in records if isinstance(r, QuantityRecord)]
    if not snapshots:
        return {}
    keys, requests = {}, []
    try:
        for record in snapshots:
            source = index.resolve(record.data.source_ref)
            roster = index.resolve(record.data.roster_ref)
            if not isinstance(source, SourcePackageRecord) or not isinstance(
                roster, RosterRecord
            ):
                continue
            request = None
            if record.data.response_ref is not None:
                path = record.data.response_ref.path.replace(
                    "/responses/", "/requests/", 1
                )
                request = {
                    "path": path,
                    "sha256": file_digest(resolve_within(root, path)),
                }
                requests.append(request)
            keys[(record.record_id, record.revision)] = (
                root.absolute(),
                record.model_dump_json(),
                source.model_dump_json(),
                roster.model_dump_json(),
                request["sha256"] if request else None,
            )
        checked = [
            r
            for r in records
            if isinstance(r, (SourcePackageRecord, RosterRecord, QuantityRecord))
        ]
        checked.append(
            {"record_id": "snapshot-inputs", "revision": 1, "files": requests}
        )
        if verify_artifact_refs(checked, root):
            return {}
    except (OSError, ValueError, WorkspaceIntegrityError):
        # Retain the original validator's diagnostics for damaged inputs.
        return {}
    return keys


def _check_submission_binding(record, files, *, roster=None):
    from ..workspace.archives import read_json
    from .common import sha256_of
    from .roster_validation import strict_json

    data = record.data
    if data.response_ref is None:
        return
    step = (
        "roster"
        if roster is None
        else f"quantities-{sha256_of(roster.ref().model_dump())[:12]}-{data.source_index}"
    )
    folder = "trace/responses" if "/" in record.execution_id else "responses"
    prefix = f"runs/{record.execution_id}/{folder}/{step}-{data.source_round:02d}"
    if not re.fullmatch(
        re.escape(prefix) + r"(?:-retry-[a-f0-9]+)?-attempt-\d{3}\.json",
        data.response_ref.path,
    ):
        raise ValueError("The response differs from the snapshot target or round.")
    request_path = data.response_ref.path.replace("/responses/", "/requests/", 1)
    from ..workspace.layout import resolve_within

    request_ref = ArtifactRef(
        path=request_path, sha256=file_digest(resolve_within(files.root, request_path))
    )
    request = read_json(files, request_ref)
    response = read_json(files, data.response_ref)
    if response.get("request_sha256") != sha256_of(request):
        raise ValueError("The response differs from its archived request.")
    from .response import argument_normalization, response_message

    exchange = response.get("exchange") or {}
    if exchange.get("tool_arguments_normalization") is not None:
        if exchange["tool_arguments_normalization"] != argument_normalization(
            exchange, request
        ):
            raise ValueError(
                "The normalization record differs from the frozen request policy."
            )
        response_message(response, effective=True)
    if roster is not None:
        star = roster.data.stars[roster.data.source_indices.index(data.source_index)]
        target = strict_json(request["payload"]["messages"][2]["content"])
        if target != {"identifiers": [i.value for i in star.identifiers]}:
            raise ValueError("The request target differs from the fixed roster.")
    return request


def _check_roster_snapshot(record, source, root):
    from ..workspace.archives import read_json
    from ..workspace.layout import resolve_within
    from .material import material_locations, supplied_locations
    from .roster_validation import check_response, select_partial

    class Files:
        def read_file(self, ref):
            path = resolve_within(root, ref.path)
            if file_digest(path) != ref.sha256:
                raise ValueError("Artifact hash differs from the fixed reference")
            return path.read_bytes()

    space = Files()
    space.root = root
    request = _check_submission_binding(record, space)
    data = record.data
    material = read_json(space, data.material_ref)
    locations = material_locations(source, root)
    if material["citation_locations"] != locations:
        raise ValueError("Saved material map differs from the fixed source")
    if data.response_ref is None:
        if data.status != "failed" or data.stars or data.notes is not None:
            raise ValueError("A missing response cannot produce scientific content.")
        return
    locations = supplied_locations(
        locations, request["payload"]["messages"][1]["content"]
    )
    response = read_json(space, data.response_ref)
    check = check_response(response, locations)
    if data.status == "complete":
        if (
            not check.passed
            or {"stars": [s.model_dump() for s in data.stars], "notes": data.notes}
            != check.payload
        ):
            raise ValueError("Saved roster differs from the accepted submission")
        if data.source_indices != list(range(len(data.stars))):
            raise ValueError("Complete roster indices differ from the submission")
    elif check.readable:
        stars, indices, rejected, notes = select_partial(check, locations)
        if (data.stars, data.source_indices, data.rejected, data.notes) != (
            stars,
            indices,
            rejected,
            notes,
        ):
            raise ValueError(
                "Saved partial roster differs from the selected submission"
            )
    elif data.stars or data.source_indices or data.notes != check.notes:
        raise ValueError("Unreadable submissions cannot produce scientific content")


def _check_quantity_snapshot(record, source, index, root):
    from ..workspace.archives import read_json
    from ..workspace.layout import resolve_within
    from .material import material_locations, supplied_locations
    from .quantity_validation import check_response, select_partial

    class Files:
        def read_file(self, ref):
            path = resolve_within(root, ref.path)
            if file_digest(path) != ref.sha256:
                raise ValueError("Artifact hash differs from its reference.")
            return path.read_bytes()

    data = record.data
    roster = index.resolve(data.roster_ref)
    if (
        not isinstance(roster, RosterRecord)
        or data.source_index not in roster.data.source_indices
        or data.source_ref != roster.data.source_ref
        or data.material_ref != roster.data.material_ref
    ):
        raise ValueError("Numerical target differs from the fixed roster.")
    space = Files()
    space.root = root
    request = _check_submission_binding(record, space, roster=roster)
    locations = material_locations(source, root)
    if read_json(space, data.material_ref)["citation_locations"] != locations:
        raise ValueError("Saved material differs from the fixed source.")
    if data.response_ref is None:
        if data.status != "failed" or data.quantities or data.notes is not None:
            raise ValueError("A missing response cannot produce scientific content.")
        return
    locations = supplied_locations(
        locations, request["payload"]["messages"][1]["content"]
    )
    check = check_response(read_json(space, data.response_ref), locations)
    if data.status == "complete" and not check.passed:
        raise ValueError("The saved submission did not pass all checks.")
    if check.readable:
        values, paths, rejected, notes = select_partial(check, locations)
        if (data.quantities, data.source_paths, data.rejected, data.notes) != (
            values,
            paths,
            rejected,
            notes,
        ):
            raise ValueError(
                "The numerical snapshot differs from the selected submission."
            )
    elif data.quantities or data.source_paths or data.notes != check.notes:
        raise ValueError("An unreadable response cannot produce scientific content.")
