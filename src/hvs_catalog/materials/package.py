"""Portable prepared inputs without a preparation database or extraction history."""

import hashlib
import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from ..pipeline.roster import material_prompt
from ..records.common import (
    ArtifactRef,
    Config,
    build_record,
    canonical_json,
    sha256_of,
)
from ..records.literature_contribution import LiteratureMetadata
from ..records.objects import ExternalRecord
from ..records.source import SourcePackageData, SourcePackageRecord
from ..records.validate import validate_collection
from ..workspace.archives import publish_json, read_json
from ..workspace.layout import resolve_within
from ..workspace.store import CommitRequest
from .metadata import PREFIX, fixed_metadata


class MaterialPackage(Config):
    format: Literal["hvs.prepared-materials"] = "hvs.prepared-materials"
    version: Literal[1] = 1
    source: SourcePackageData
    metadata: ArtifactRef
    parser_files: list[ArtifactRef] = Field(default_factory=list)


class _PackageFiles:
    def __init__(self, root):
        self.root = Path(root)

    def read_file(self, ref):
        content = resolve_within(self.root, ref.path).read_bytes()
        if hashlib.sha256(content).hexdigest() != ref.sha256:
            raise ValueError(f"Material file hash changed: {ref.path}")
        return content


def _package_payload(package):
    value = package.model_dump(mode="json")
    if not value["parser_files"]:
        value.pop("parser_files")
    return value


def _source_record(package):
    return build_record(
        SourcePackageRecord,
        record_id="imported-materials-" + sha256_of(_package_payload(package)),
        created_at=datetime.now(UTC),
        execution_id=None,
        data=package.source,
    )


def read_material_package(directory, *, root=None):
    """Verify attachment bytes, fixed identity, metadata, and model-readable inputs."""
    directory = Path(directory)
    package = MaterialPackage.model_validate_json(
        resolve_within(directory, "material.json").read_bytes()
    )
    root = Path(root) if root is not None else directory
    source = _source_record(package)
    for role in ("pdf", "pdf_index"):
        if sum(f.role == role for f in source.data.files) != 1:
            raise ValueError(f"A material package requires one {role}")
    if any(
        f.role in {"prepared_bundle", "preparation_provenance"}
        for f in source.data.files
    ):
        raise ValueError(
            "Material packages must not contain preparation execution state"
        )
    paths = [f.path for f in source.data.files] + [
        package.metadata.path,
        "material.json",
    ]
    if len(paths) != len(set(paths)):
        raise ValueError("Duplicate material file paths")
    validate_collection([source], root)
    reader = _PackageFiles(root)
    for ref in package.parser_files:
        reader.read_file(ref)
    document = json.loads(reader.read_file(package.metadata))
    if (
        not isinstance(document, dict)
        or document.get("format") != "hvs.paper-metadata"
        or document.get("version") != 1
    ):
        raise ValueError("Unsupported material metadata")
    metadata = LiteratureMetadata.model_validate(document.get("metadata"))
    if (metadata.arxiv_id, metadata.version) != (
        source.data.paper_key,
        source.data.source_version,
    ):
        raise ValueError("Metadata differs from the fixed paper version")
    index_file = next(f for f in source.data.files if f.role == "pdf_index")
    index = json.loads(
        reader.read_file(ArtifactRef(path=index_file.path, sha256=index_file.sha256))
    )
    pdf = next(f for f in source.data.files if f.role == "pdf")
    if not isinstance(index, dict) or index.get("pdf_file_id") != pdf.file_id:
        raise ValueError("PDF index differs from the fixed PDF")
    try:
        material_prompt(reader, {}, source)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("Invalid prepared PDF index") from exc
    return package


def _attachment_path(file):
    fixed = {
        "pdf": "paper.pdf",
        "parser_output": "mineru/result.zip",
        "derived_text": "prepared/manuscript.md",
        "pdf_index": "prepared/pdf-blocks.json",
    }
    if file.role == "pdf_image":
        return "mineru/images/" + Path(file.file_id).name
    if file.role not in fixed:
        raise ValueError(f"Unsupported prepared attachment role: {file.role}")
    return fixed[file.role]


def export_material_package(space, source_ref, directory):
    """Publish verified parsing materials and resolved metadata to a new directory."""
    directory = Path(directory)
    if directory.exists() or directory.is_symlink():
        raise FileExistsError(f"Material directory already exists: {directory}")
    directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix="." + directory.name + "-", dir=directory.parent)
    )
    try:
        with space.lock():
            source = space.store().read_record(source_ref)
            if not isinstance(source, SourcePackageRecord):
                raise TypeError("The source reference must identify prepared materials")
            material_prompt(space, {}, source)
            metadata_ref, _ = fixed_metadata(space, source)
            metadata = canonical_json(read_json(space, metadata_ref)).encode()
            (staging / "metadata.json").write_bytes(metadata)
            files = []
            for file in source.data.files:
                if file.role in {"prepared_bundle", "preparation_provenance"}:
                    continue
                relative = _attachment_path(file)
                target = resolve_within(staging, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(
                        space.read_file(ArtifactRef(path=file.path, sha256=file.sha256))
                    )
                files.append(file.model_copy(update={"path": relative}))
            package = MaterialPackage(
                source=source.data.model_copy(update={"files": files}),
                metadata=ArtifactRef(
                    path="metadata.json", sha256=hashlib.sha256(metadata).hexdigest()
                ),
            )
            (staging / "material.json").write_text(
                canonical_json(package.model_dump(mode="json")) + "\n"
            )
            read_material_package(staging)
            if directory.exists() or directory.is_symlink():
                raise FileExistsError(f"Material directory already exists: {directory}")
            staging.rename(directory)
        return package
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def import_material_package(directory, space, *, root=None, copy_files=True):
    """Register a verified package in an independent workspace without creating a run."""
    directory = Path(directory)
    package = read_material_package(directory, root=root)
    reader = _PackageFiles(root if root is not None else directory)
    if not copy_files and reader.root.resolve() != space.root.resolve():
        raise ValueError("Shared materials must be contained in the target workspace.")
    metadata_document = json.loads(reader.read_file(package.metadata))
    source = _source_record(package)
    key = "import-materials:" + sha256_of(_package_payload(package))
    metadata_key = PREFIX + source.data.paper_key + source.data.source_version
    with space.lock():
        existing = space.store().find_receipt(key)
        if existing:
            saved = space.store().read_record(existing.result_refs[0])
            validate_collection([saved], space.root)
            _, metadata = fixed_metadata(space, saved)
            if metadata.model_dump(mode="json") != metadata_document["metadata"]:
                raise ValueError("Imported metadata differs from the material package")
            return saved.ref()
        prior_metadata = space.store().get_meta(metadata_key)
        metadata_records = []
        if prior_metadata:
            ref = ArtifactRef.model_validate_json(prior_metadata)
            if read_json(space, ref)["metadata"] != metadata_document["metadata"]:
                raise ValueError("Conflicting metadata for the fixed paper version")
        else:
            ref = (
                package.metadata
                if not copy_files
                else publish_json(
                    space,
                    "sources/metadata/" + package.metadata.sha256 + ".json",
                    metadata_document,
                )
            )
            metadata_records.append(
                build_record(
                    ExternalRecord,
                    record_id="imported-metadata-" + package.metadata.sha256,
                    created_at=datetime.now(UTC),
                    execution_id=None,
                    data={
                        "service": "prepared-materials-metadata",
                        "catalog_version": source.data.source_version,
                        "queried_at": datetime.now(UTC),
                        "query": {"arxiv_id": source.data.paper_key},
                        "raw_response": ref,
                        "entries": [],
                    },
                )
            )
        prefix = "sources/prepared/" + sha256_of(_package_payload(package))
        files = []
        for file in source.data.files:
            artifact = (
                ArtifactRef(path=file.path, sha256=file.sha256)
                if not copy_files
                else space.publish_file(
                    prefix + "/" + file.path,
                    reader.read_file(ArtifactRef(path=file.path, sha256=file.sha256)),
                )
            )
            files.append(file.model_copy(update={"path": artifact.path}))
        source = source.model_copy(
            update={"data": source.data.model_copy(update={"files": files})}
        )
        material_prompt(space, {}, source)
        space.store().commit(
            CommitRequest(
                operation_key=key,
                kind="import-materials",
                new_records=[source, *metadata_records],
                result_refs=[source.ref()],
                meta_updates={metadata_key: ref.model_dump_json()},
            )
        )
        return source.ref()
