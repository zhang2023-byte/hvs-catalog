"""Check and bind authorized corrections in the existing Gold files.

Git retains file history. The existing version index is refreshed in place and
marked as Git-maintained; historical operation and approval records stay intact.
"""

from __future__ import annotations

from pathlib import Path

from .compact import (
    CompactGoldAnswer,
    CompactGoldMetadata,
    CompactGoldPair,
    CompactRevision,
    check_compact_gold_pair,
    compact_answer_to_json,
    compact_metadata_to_json,
    read_compact_gold_pair,
)
from .errors import GoldCheckError, GoldIntegrityError, GoldTargetError
from .store import (
    FileEntry,
    GoldTarget,
    VersionEntry,
    _atomic_write,
    _atomic_write_json,
    _now,
    _resolve_in_target,
    _sha256_bytes,
)


def _paths(target: GoldTarget, entry: VersionEntry) -> tuple[Path, Path]:
    prefix = f"annotations/{entry.document_id}/v{entry.version}"
    expected = {f"{prefix}/answer.json", f"{prefix}/metadata.json"}
    if len(entry.files) != 2 or {f.path for f in entry.files} != expected:
        raise GoldTargetError(
            "In-place correction requires a compact Gold entry with unchanged paths"
        )
    return (
        _resolve_in_target(target.root, f"{prefix}/answer.json"),
        _resolve_in_target(target.root, f"{prefix}/metadata.json"),
    )


def validate_git_entry(target: GoldTarget, entry: VersionEntry) -> None:
    """Validate a corrected current pair without borrowing an old approval."""
    answer_path, _ = _paths(target, entry)
    pair = read_compact_gold_pair(answer_path)
    if entry.document_id != f"gold-{pair.answer.arxiv_id}":
        raise GoldIntegrityError("Corrected Gold identity differs from version index")
    if pair.metadata.revision is None:
        raise GoldIntegrityError("Git-maintained Gold needs an attributed correction")
    if pair.metadata.annotation.reviewer != pair.metadata.revision.decided_by:
        raise GoldIntegrityError(
            "Correction decision author differs from metadata reviewer"
        )


def refresh_gold_in_place(
    *,
    target: GoldTarget,
    document_id: str,
    material_root: str | Path,
    revised_by: str,
    decided_by: str,
    basis: str,
) -> VersionEntry:
    """Check existing edited files and refresh their index without creating a copy.

    ``decided_by`` identifies the human whose existing decision authorized the
    correction. ``revised_by`` identifies its preparer. This records that decision
    and never creates or rewrites a historical SaveApproval.
    """
    if not revised_by.strip() or not decided_by.strip() or not basis.strip():
        raise GoldTargetError(
            "Correction requires revised_by, decided_by and decision basis"
        )
    with target.lock():
        index = target.entry_path(document_id)
        if not index.is_file():
            raise GoldTargetError("In-place correction requires an existing Gold entry")
        entry = VersionEntry.model_validate_json(index.read_bytes())
        if entry.document_id != document_id:
            raise GoldIntegrityError("Gold index identity differs from document")
        answer_path, metadata_path = _paths(target, entry)
        answer = CompactGoldAnswer.model_validate_json(answer_path.read_bytes())
        metadata = CompactGoldMetadata.model_validate_json(metadata_path.read_bytes())
        if document_id != f"gold-{answer.arxiv_id}":
            raise GoldIntegrityError("Corrected Gold identity differs from document")
        answer_bytes = compact_answer_to_json(answer).encode()
        previous_hash = next(
            f.sha256 for f in entry.files if f.path.endswith("/answer.json")
        )
        metadata = metadata.model_copy(
            update={
                "review": metadata.review.model_copy(
                    update={"answer_sha256": _sha256_bytes(answer_bytes)}
                ),
                "annotation": metadata.annotation.model_copy(
                    update={"reviewer": decided_by}
                ),
                "revision": CompactRevision(
                    revised_by=revised_by,
                    decided_by=decided_by,
                    revised_at=_now(),
                    basis=basis,
                    previous_answer_sha256=previous_hash,
                ),
            }
        )
        pair = CompactGoldPair(answer=answer, metadata=metadata)
        report = check_compact_gold_pair(pair, material_root=material_root)
        if report.blocking_count:
            raise GoldCheckError(
                f"In-place Gold check failed: {report.blocking_count} blocking problems"
            )
        metadata_bytes = compact_metadata_to_json(metadata).encode()
        updated = entry.model_copy(
            update={
                "storage_mode": "git",
                "files": [
                    FileEntry(
                        path=str(answer_path.relative_to(target.root)),
                        sha256=_sha256_bytes(answer_bytes),
                    ),
                    FileEntry(
                        path=str(metadata_path.relative_to(target.root)),
                        sha256=_sha256_bytes(metadata_bytes),
                    ),
                ],
            }
        )
        _atomic_write(answer_path, answer_bytes)
        _atomic_write(metadata_path, metadata_bytes)
        _atomic_write_json(index, updated.model_dump(mode="json"))
        return updated
