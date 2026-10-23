"""Archive bibliographic facts for an exact paper version without model inference."""

from dataclasses import asdict
from datetime import UTC, datetime

from ..records.common import ArtifactRef, sha256_of
from ..records.literature_contribution import LiteratureMetadata
from ..workspace.archives import publish_json, read_json

PREFIX = "paper_metadata:"


def prepare_metadata(space, metadata, provenance):
    """Publish immutable metadata and return facts for the caller's transaction."""
    from ..records.common import build_record
    from ..records.objects import ExternalRecord

    value = LiteratureMetadata(
        arxiv_id=metadata.canonical_id,
        version=f"v{metadata.version}",
        title=metadata.title,
        authors=list(metadata.authors),
    )
    document = {
        "format": "hvs.paper-metadata",
        "version": 1,
        "metadata": value.model_dump(),
        "parsed_source": asdict(metadata),
        "provenance": provenance,
    }
    key = PREFIX + value.arxiv_id + value.version
    existing = space.store().get_meta(key)
    if existing:
        ref = ArtifactRef.model_validate_json(existing)
        if read_json(space, ref)["metadata"] != value.model_dump():
            raise ValueError("Conflicting metadata for the fixed paper version.")
        return ref, [], {}
    digest = sha256_of(document)
    ref = publish_json(space, f"sources/metadata/{digest}.json", document)
    now = datetime.now(UTC)
    record = build_record(
        ExternalRecord,
        record_id="paper-metadata-" + digest,
        created_at=now,
        execution_id=None,
        data={
            "service": "arxiv-metadata",
            "catalog_version": value.version,
            "queried_at": now,
            "query": {"arxiv_id": value.arxiv_id, "version": value.version},
            "raw_response": ref,
            "entries": [],
        },
    )
    return ref, [record], {key: ref.model_dump_json()}


def archive_metadata(space, metadata, provenance):
    from ..workspace.store import CommitRequest

    with space.lock():
        ref, records, updates = prepare_metadata(space, metadata, provenance)
        if records:
            space.store().commit(
                CommitRequest(
                    operation_key="metadata:" + ref.sha256,
                    kind="metadata",
                    new_records=records,
                    meta_updates=updates,
                )
            )
        return ref


def fixed_metadata(space, source):
    key = PREFIX + source.data.paper_key + source.data.source_version
    raw = space.store().get_meta(key)
    if not raw:
        raise ValueError(
            "Fixed paper metadata is missing. Prepare metadata before extraction."
        )
    ref = ArtifactRef.model_validate_json(raw)
    document = read_json(space, ref)
    if document.get("format") != "hvs.paper-metadata" or document.get("version") != 1:
        raise ValueError("Unsupported paper metadata archive.")
    value = LiteratureMetadata.model_validate(document["metadata"])
    if (
        value.arxiv_id != source.data.paper_key
        or value.version != source.data.source_version
    ):
        raise ValueError("Metadata differs from the fixed paper version.")
    return ref, value
