"""Publish closed executions with shared materials and portable immutable evidence."""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from ..pipeline.results import PREFIX, current_result
from ..records.common import ArtifactRef, RecordRef, canonical_json, sha256_of
from ..records.file_checks import scoped_verification
from ..records.serialize import parse_record
from ..records.validate import validate_collection
from ..runtime.execution import get_execution, snapshot_input_digest
from ..workspace import open_workspace
from ..workspace.archives import read_json
from ..workspace.layout import Workspace, resolve_within
from ..workspace.store import ExecutionRow, record_closure, verify_artifact_refs
from .materials import material_map, paper_id, round_id


class ArchiveStore:
    """Read exact records without retaining a writable per-paper database."""

    def __init__(self, payload):
        self.payload = payload
        self.records = {
            (r["record_id"], r["revision"]): parse_record(r) for r in payload["records"]
        }
        self.execution = ExecutionRow(**payload["execution"])

    def read_record(self, ref):
        return self.records.get((ref.record_id, ref.revision))

    def read_record_current(self, record_id):
        revisions = [r for (key, _), r in self.records.items() if key == record_id]
        return max(revisions, key=lambda r: r.revision) if revisions else None

    def get_execution(self, execution_id):
        return self.execution if execution_id == self.execution.execution_id else None

    def list_executions(self):
        return [self.execution]

    def list_records(self, *, execution_id=None):
        return [
            r
            for r in self.records.values()
            if execution_id is None or r.execution_id == execution_id
        ]

    def get_meta(self, key):
        if key in {
            PREFIX + self.execution.execution_id,
            "retained_result:" + self.execution.execution_id,
        }:
            return canonical_json(self.payload["run_ref"])
        return None

    def close(self):
        pass


class ArchiveSpace(Workspace):
    def __init__(self, base, payload):
        super().__init__(base.root, base.identity, read_only=True)
        self._store = ArchiveStore(payload)

    def checkpoint_connection(self):
        raise ValueError("Archived benchmark executions cannot be resumed.")


def archive_path(round_name, paper):
    return f".state/archives/{round_id(round_name)}/{paper_id(paper)}.json"


def open_archive(root, round_name, paper):
    base = open_workspace(Path(root), read_only=True)
    try:
        path = resolve_within(base.root, archive_path(round_name, paper))
        payload = json.loads(path.read_bytes())
        if (
            payload.get("format") != "hvs.benchmark-archive"
            or payload.get("version") != 1
        ):
            raise ValueError("Unsupported benchmark archive.")
        if payload["execution"]["execution_id"] != round_name + "/" + paper:
            raise ValueError("Archive identifies another experiment or paper.")
        return ArchiveSpace(base, payload)
    finally:
        base.close()


@scoped_verification
def verify_archive(root, round_name, paper):
    space = open_archive(root, round_name, paper)
    try:
        records = space.store().list_records()
        validate_collection(records, space.root)
        problems = verify_artifact_refs(records, space.root)
        if problems:
            raise ValueError("; ".join(problems))
        result = current_result(space, round_name + "/" + paper)
        if result is None or result[1].status.resumable:
            raise ValueError("Archive must identify a closed result.")
        return result[1]
    finally:
        space.close()


class Relocator:
    """Rewrite only storage references and execution identity; verify original bytes."""

    def __init__(self, source, target, execution_id, round_name, paper, variant):
        self.source, self.target = source, target
        self.old_id = execution_id
        self.new_id = round_id(round_name) + "/" + paper_id(paper)
        self.prefix = "runs/" + self.new_id
        self.materials = material_map(target.root, paper, variant)
        self.files, self.records, self.active = {}, {}, set()
        self.current_ref, self.current = current_result(source, execution_id)
        self.selected = {
            r.path
            for r in (self.current_ref, self.current.trace_ref, self.current.result_ref)
            if r
        }

    def record(self, ref):
        key = (ref["record_id"], ref["revision"])
        if key in self.records:
            return self.records[key]
        record = self.source.store().read_record(RecordRef.model_validate(ref))
        if record is None:
            raise ValueError("Missing source record during archive.")
        value = record.model_dump(mode="json", by_alias=True)
        if record.RECORD_NAME == "run":
            value["data"]["artifacts"] = [
                r
                for r in value["data"]["artifacts"]
                if not any(part in r["path"] for part in ("/results/", "/traces/"))
                or r["path"] in self.selected
            ]
        value = self.rewrite(value)
        self.records[key] = value
        return value

    def rewrite(self, value, key=None):
        if isinstance(value, list):
            return [self.rewrite(v) for v in value]
        if isinstance(value, str):
            if key in {"execution_id", "source_execution"} and value == self.old_id:
                return self.new_id
            if key in {
                "prepared_input",
                "prepared_prompt",
                "extraction_input",
                "llm_input",
            }:
                return canonical_json(self.rewrite(json.loads(value)))
            return value
        if not isinstance(value, dict):
            return value
        if isinstance(value.get("path"), str) and "sha256" in value:
            ref = self.file(ArtifactRef(path=value["path"], sha256=value["sha256"]))
            return {**value, **ref.model_dump()}
        result = {k: self.rewrite(v, k) for k, v in value.items()}
        if (
            isinstance(value.get("ref"), dict)
            and {"record_id", "revision"} <= value["ref"].keys()
            and "sha256" in value
        ):
            result["sha256"] = sha256_of(self.record(value["ref"]))
        return result

    def file(self, ref):
        key = (ref.path, ref.sha256)
        if key in self.files:
            return self.files[key]
        if key in self.active:
            raise ValueError("Cyclic archive references.")
        raw = self.source.read_file(ref)
        if self.source.root == self.target.root and ref.path not in self.selected:
            self.files[key] = ref
            return ref
        if ref.sha256 in self.materials:
            result = self.materials[ref.sha256]
            if self.target.read_file(result) != raw:
                raise ValueError(
                    "Shared material differs from original execution input."
                )
            self.files[key] = result
            return result
        self.active.add(key)
        try:
            value = None
            if ref.path.endswith(".json"):
                value = json.loads(raw)
                updated = self.rewrite(value)
                if ref.path == self.current_ref.path:
                    updated["status"]["resumable"] = False
                if updated != value:
                    raw = (canonical_json(updated) + "\n").encode()
            digest = hashlib.sha256(raw).hexdigest()
            if ref.path == self.current_ref.path:
                path = self.prefix + "/run.json"
            elif self.current.result_ref and ref.path == self.current.result_ref.path:
                path = self.prefix + "/literature_contribution.json"
            elif ref.path == self.current.trace_ref.path:
                path = self.prefix + "/trace/trace.json"
            elif (
                "/requests/" in ref.path
                or "/responses/" in ref.path
                or "/outputs/" in ref.path
            ):
                kind = next(
                    k
                    for k in ("requests", "responses", "outputs")
                    if f"/{k}/" in ref.path
                )
                path = self.prefix + "/trace/" + kind + "/" + Path(ref.path).name
            elif Path(ref.path).name == "rules.toml":
                path = self.prefix + "/trace/inputs/rules.toml"
            elif isinstance(value, dict) and (
                value.get("format") == "hvs.extraction-input"
                or isinstance(value.get("value"), dict)
                and value["value"].get("format") == "hvs.extraction-input"
            ):
                path = self.prefix + "/trace/input.json"
            else:
                path = ".state/shared/" + digest + Path(ref.path).suffix
            result = self.target.publish_file(path, raw)
            self.files[key] = result
            return result
        finally:
            self.active.remove(key)


@scoped_verification
def archive_execution(source, target, execution_id, round_name, paper, variant):
    """Publish the archive marker last; retries verify and reuse committed archives."""
    destination = archive_path(round_name, paper)
    if (target.root / destination).exists():
        saved = json.loads((target.root / destination).read_bytes())
        original_ref, _ = current_result(source, execution_id)
        if (
            saved["origin"]["run_ref"] != original_ref.model_dump()
            or saved["origin"]["input_digest"]
            != get_execution(source, execution_id).input_digest
            or saved["material_variant"] != variant
        ):
            raise ValueError(
                "The archive target already identifies a different execution or material choice."
            )
        return verify_archive(target.root, round_name, paper)
    with source.lock(), target.lock():
        handle = get_execution(source, execution_id)
        current = source.store().read_record(handle.run_ref)
        converter = Relocator(source, target, execution_id, round_name, paper, variant)
        if (
            converter.current.status.execution != "finished"
            and converter.current.status.resumable
        ):
            raise ValueError("An unfinished extraction must retain its recovery state.")
        seeds = [
            current,
            *[
                r
                for r in source.store().list_records(execution_id=execution_id)
                if r.RECORD_NAME != "run"
            ],
        ]
        for record in record_closure(seeds, source.store().read_record):
            if record.RECORD_NAME == "run" and record.ref() != current.ref():
                continue
            converter.record(record.ref().model_dump())
        run_ref = converter.file(converter.current_ref)
        # Preserve the exact rule bytes even when their reference is implicit.
        from ..runtime.execution import rules_archive_rel

        rule_path = rules_archive_rel(execution_id, source)
        raw = resolve_within(source.root, rule_path).read_bytes()
        converter.file(
            ArtifactRef(path=rule_path, sha256=hashlib.sha256(raw).hexdigest())
        )
        row = asdict(source.store().get_execution(execution_id))
        row["execution_id"] = converter.new_id
        run_record = parse_record(
            converter.records[(current.record_id, current.revision)]
        )
        row["input_digest"] = snapshot_input_digest(
            run_record.data.input_snapshot,
            flow_id=row["flow_id"],
            flow_version=row["flow_version"],
        )
        payload = {
            "format": "hvs.benchmark-archive",
            "version": 1,
            "material_variant": variant,
            "execution": row,
            "run_ref": run_ref.model_dump(),
            "origin": {
                "execution_id": execution_id,
                "input_digest": handle.input_digest,
                "run_ref": converter.current_ref.model_dump(),
                "status": converter.current.status.model_dump(),
            },
            "records": [converter.records[k] for k in sorted(converter.records)],
        }
        candidate = ArchiveSpace(target, payload)
        validate_collection(candidate.store().list_records(), target.root)
        problems = verify_artifact_refs(candidate.store().list_records(), target.root)
        if problems:
            raise ValueError("; ".join(problems))
        current_result(candidate, converter.new_id)
        # Reconstructed request/response content must be identical after relocation.
        for (path, digest), new_ref in converter.files.items():
            if "/requests/" in path or "/responses/" in path:
                old_ref = ArtifactRef(path=path, sha256=digest)
                if path.endswith(".json"):
                    # Response envelopes can link a retained raw HTTP body. Its
                    # location changes, but its bytes and every model value do not.
                    if converter.rewrite(read_json(source, old_ref)) != read_json(
                        target, new_ref
                    ):
                        raise ValueError(
                            "Migration changed a model request or response."
                        )
                elif source.read_file(old_ref) != target.read_file(new_ref):
                    raise ValueError("Migration changed raw response bytes.")
        target.publish_file(destination, (canonical_json(payload) + "\n").encode())
        return verify_archive(target.root, round_name, paper)
