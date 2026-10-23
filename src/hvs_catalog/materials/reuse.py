"""Copy fixed prepared attachments without rerunning a parser or copying predictions."""

from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

from ..records.common import (
    ArtifactRef,
    RecordRef,
    build_record,
    canonical_json,
    sha256_of,
)
from ..records.source import SourceFile, SourcePackageRecord
from ..workspace.store import CommitRequest


def reuse_prepared_materials(source_space, preparation_id, target_space):
    """Publish a self-contained source record with the original attachment bytes."""
    from ..pipeline.roster import material_prompt, prepared_bundle
    from .service import MaterialsService

    if source_space.root == target_space.root:
        raise ValueError("Use a separate workspace for reused materials.")
    with ExitStack() as locks:
        for space in sorted((source_space, target_space), key=lambda s: str(s.root)):
            locks.enter_context(space.lock())
        bundle = MaterialsService(source_space).get_prepared_input(preparation_id)
        bundle, original = prepared_bundle(
            source_space,
            ArtifactRef.model_validate(bundle["bundle_ref"]),
            RecordRef.model_validate(bundle["prepared_source_ref"]),
        )
        material_prompt(source_space, bundle, original)
        provenance = {
            "format": "hvs.reused-materials",
            "version": 1,
            "source_space_id": source_space.identity.space_id,
            "preparation_id": preparation_id,
            "source_record": original.model_dump(mode="json"),
            "bundle": bundle,
        }
        digest = sha256_of(provenance)
        key = "reuse-materials:" + digest
        existing = target_space.store().find_receipt(key)
        if existing:
            record = target_space.store().read_record(existing.result_refs[0])
            for file in record.data.files:
                target_space.read_file(ArtifactRef(path=file.path, sha256=file.sha256))
            return record.ref()
        prefix = "sources/prepared/" + digest
        files = []
        for index, file in enumerate(original.data.files):
            content = source_space.read_file(
                ArtifactRef(path=file.path, sha256=file.sha256)
            )
            ref = target_space.publish_file(
                f"{prefix}/{index:04d}-{Path(file.path).name}", content
            )
            files.append(file.model_copy(update={"path": ref.path}))
        ref = target_space.publish_file(
            prefix + "/provenance.json", canonical_json(provenance).encode()
        )
        files.append(
            SourceFile(
                file_id="reuse-provenance-" + digest,
                path=ref.path,
                sha256=ref.sha256,
                media_type="application/json",
                role="preparation_provenance",
                read_state="ready",
                derived_from=[],
            )
        )
        record = build_record(
            SourcePackageRecord,
            record_id="reused-materials-" + digest,
            created_at=datetime.now(UTC),
            execution_id=None,
            data={**original.data.model_dump(mode="json"), "files": files},
        )
        target_space.store().commit(
            CommitRequest(
                operation_key=key,
                kind="reuse-materials",
                new_records=[record],
                result_refs=[record.ref()],
            )
        )
        return record.ref()
