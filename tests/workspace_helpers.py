"""Workspace helpers publish synthetic fixtures and bind source/external-response paths."""

from __future__ import annotations

from records_helpers import FIXTURES

from hvs_catalog.workspace import Workspace


def publish_fixture_collection(space: Workspace, records: list) -> list:
    """Publish fixture materials under sources/, rewrite paths, and return committable
    records.

    Cover both source-package files and raw external-data responses.
    """
    from hvs_catalog.records.objects import ExternalRecord

    out = []
    for record in records:
        if record.RECORD_NAME == "source_package":
            files = []
            for file in record.data.files:
                data = (FIXTURES / file.path).read_bytes()
                ref = space.publish_source_file(file.path, data)
                files.append(file.model_copy(update={"path": ref.path}))
            record = record.model_copy(
                update={"data": record.data.model_copy(update={"files": files})}
            )
        elif isinstance(record, ExternalRecord):
            raw = record.data.raw_response
            ref = space.publish_source_file(
                raw.path, (FIXTURES / raw.path).read_bytes()
            )
            record = record.model_copy(
                update={"data": record.data.model_copy(update={"raw_response": ref})}
            )
        out.append(record)
    return out
