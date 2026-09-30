"""One current Gold pair, content-bound human approval, and manual crash rollback.

No version index, preparations, operation receipts, replay or automatic recovery.
Git history is an explicitly authorized operation outside this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field

from ..records.common import Config, canonical_json
from ..workspace.locking import SpaceLock
from .checking import GoldCheckReport
from .compact import (
    CompactGoldPair,
    check_compact_gold_pair,
    compact_answer_to_json,
    compact_metadata_to_json,
    read_compact_gold_pair,
)
from .errors import (
    GoldApprovalError,
    GoldCheckError,
    GoldIntegrityError,
    GoldTargetError,
)

TARGET_MARKER = "gold-target.json"
TARGET_FORMAT = {"format": "hvs-catalog-gold-current", "format_version": 1}
CURRENT_FILES = ("answer.json", "metadata.json", "approval.json")
PENDING = ".save-backup"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(value: object) -> str:
    return _sha256(canonical_json(value).encode())


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _fsync_dir(path.parent)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


def _safe_path(path: Path) -> Path:
    absolute = path.absolute()
    # A trusted operator may select a parent alias (e.g. macOS /var).
    # Check the target itself before resolving; approval binds the real root.
    if absolute.is_symlink():
        raise GoldTargetError(f"Unsafe symbolic link target: {absolute}")
    return absolute.resolve()


class SaveBinding(Config):
    """Read-only summary of canonical content and the authorized write premise."""

    target_path: str
    arxiv_id: str
    answer_sha256: str
    metadata_sha256: str
    expected_old: str | None
    rules_version: str
    rules_sha256: str
    science_pending: list[str]

    @property
    def sha256(self) -> str:
        return _digest(self.model_dump(mode="json"))

    @property
    def pair_sha256(self) -> str:
        return _digest([self.answer_sha256, self.metadata_sha256])


class SaveApproval(Config):
    """Human declaration for this exact binding, not metadata review alone."""

    approved_by: str = Field(min_length=1)
    approver_kind: Literal["human"]
    approved_at: AwareDatetime
    binding: SaveBinding
    binding_sha256: str
    basis: str = Field(min_length=1)
    scientific_review: bool = False
    accepted_pending: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class SaveCheck:
    binding: SaveBinding
    report: GoldCheckReport


class SaveResult(Config):
    target_path: str
    pair_sha256: str
    already_present: bool = False


class GoldTarget:
    """A single paper's directory, e.g. private-repo/data/2501.00001.

    Only new/empty directories or this exact layout are accepted. Draft parsers
    do not constitute an authorized-target reader.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = _safe_path(Path(root))

    def _verify_layout(self) -> bool:
        _safe_path(self.root)
        if not self.root.exists():
            return False
        if not self.root.is_dir():
            raise GoldTargetError("Gold target is not a directory")
        names = {p.name for p in self.root.iterdir()}
        if not names:
            return False
        marker = self.root / TARGET_MARKER
        allowed = {TARGET_MARKER, ".lock", *CURRENT_FILES, PENDING}
        # Atomic-write leftovers are not adopted, even in a marked directory.
        if names - allowed or not marker.is_file() or marker.is_symlink():
            raise GoldTargetError(
                "Unknown or old Gold target layout; migration requires separate approval"
            )
        if any(p.is_symlink() for p in self.root.iterdir()):
            raise GoldTargetError("Symbolic links in Gold target are unsupported")
        try:
            if json.loads(marker.read_bytes()) != TARGET_FORMAT:
                raise ValueError("incompatible marker")
        except (ValueError, OSError) as exc:
            raise GoldTargetError("Unknown or damaged Gold target marker") from exc
        for name in (TARGET_MARKER, ".lock", *CURRENT_FILES):
            p = self.root / name
            if p.exists() and not p.is_file():
                raise GoldTargetError(f"Gold target {name} is not a regular file")
        if (self.root / PENDING).exists():
            raise GoldIntegrityError(
                "Interrupted Gold save: .save-backup retained; reject reading/writing. "
                "Manually restore the complete old or new triplet before removing the backup; see Gold README."
            )
        return True

    def initialize(self) -> None:
        if self._verify_layout():
            return
        self.root.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.root / TARGET_MARKER, _json_bytes(TARGET_FORMAT))

    def read_current(self) -> tuple[CompactGoldPair, SaveApproval] | None:
        initialized = self._verify_layout()
        if not initialized:
            return None
        present = [(self.root / name).exists() for name in CURRENT_FILES]
        if not any(present):
            return None
        if not all(present):
            raise GoldIntegrityError(
                "Incomplete current Gold triplet; preserve files for manual recovery"
            )
        try:
            pair = read_compact_gold_pair(self.root)
            approval = SaveApproval.model_validate_json(
                (self.root / "approval.json").read_bytes()
            )
        except (ValueError, OSError) as exc:
            raise GoldIntegrityError(
                f"Invalid current Gold approval/pair: {exc}"
            ) from exc
        binding = _binding(
            pair, self, approval.binding.expected_old, approval.binding.science_pending
        )
        _verify_approval(approval, binding)
        # Require the stored canonical bytes too, not just parsed equivalence.
        if any(
            _sha256((self.root / name).read_bytes()) != digest
            for name, digest in (
                ("answer.json", binding.answer_sha256),
                ("metadata.json", binding.metadata_sha256),
            )
        ):
            raise GoldIntegrityError(
                "Current Gold bytes differ from human-approved content"
            )
        return pair, approval

    def fingerprint(self) -> str | None:
        current = self.read_current()
        return current[1].binding.pair_sha256 if current else None


def _binding(
    pair: CompactGoldPair,
    target: GoldTarget,
    expected_old: str | None,
    pending: list[str],
) -> SaveBinding:
    return SaveBinding(
        target_path=str(target.root),
        arxiv_id=pair.answer.arxiv_id,
        answer_sha256=_sha256(compact_answer_to_json(pair.answer).encode()),
        metadata_sha256=_sha256(compact_metadata_to_json(pair.metadata).encode()),
        expected_old=expected_old,
        rules_version=pair.metadata.rules.version,
        rules_sha256=pair.metadata.rules.sha256,
        science_pending=sorted(set(pending)),
    )


def _verify_approval(approval: SaveApproval, binding: SaveBinding) -> None:
    # Revalidate even model_construct/model_copy inputs at this public boundary.
    try:
        approval = SaveApproval.model_validate(approval.model_dump(mode="json"))
    except ValueError as exc:
        raise GoldApprovalError(f"Invalid human approval: {exc}") from exc
    if not approval.approved_by.strip() or not approval.basis.strip():
        raise GoldApprovalError("Human identity and approval basis must be nonblank")
    if not approval.scientific_review:
        raise GoldApprovalError("Explicit human scientific_review is required")
    if approval.binding != binding or approval.binding_sha256 != binding.sha256:
        raise GoldApprovalError(
            "Approval content/target/premise binding changed; obtain new approval"
        )
    if sorted(approval.accepted_pending) != binding.science_pending:
        raise GoldApprovalError(
            "Approval must explicitly accept the exact science_pending items"
        )


def check_save(
    *,
    pair_path: str | Path,
    material_root: str | Path,
    target: GoldTarget,
    expected_old: str | None = None,
) -> SaveCheck:
    """Check an already organized compact pair, without creating any state."""
    pair = read_compact_gold_pair(pair_path)
    report = check_compact_gold_pair(pair, material_root=material_root)
    if report.blocking_count:
        details = "; ".join(
            f"{p.subject}: {p.message}"
            for p in report.problems
            if p.category in {"structure", "material"}
        )
        raise GoldCheckError(
            f"Gold has {report.blocking_count} blocking problems: {details}"
        )
    current = target.read_current()
    if (current[1].binding.pair_sha256 if current else None) != expected_old:
        raise GoldTargetError("Expected old content differs from current Gold")
    if current:
        previous = current[0].metadata
        if (
            previous.paper.arxiv_id != pair.metadata.paper.arxiv_id
            or previous.annotation.original_annotator
            != pair.metadata.annotation.original_annotator
            or previous.annotation.original_annotated_at
            != pair.metadata.annotation.original_annotated_at
            or previous.annotation.original_models
            != pair.metadata.annotation.original_models
        ):
            raise GoldTargetError(
                "Revision cannot replace original annotation identity/source"
            )
        revision = pair.metadata.revision
        if (
            revision is None
            or revision.previous_answer_sha256 != current[1].binding.answer_sha256
        ):
            raise GoldTargetError(
                "Revision needs basis/actors/date and the exact previous answer hash"
            )
    return SaveCheck(_binding(pair, target, expected_old, report.pending_ids()), report)


def approved_save(
    *,
    pair_path: str | Path,
    material_root: str | Path,
    target: GoldTarget,
    approval: SaveApproval,
    fault_hooks: dict[str, Callable[[], None]] | None = None,
) -> SaveResult:
    """Check and save one authorized current pair. No Git writes or recovery replay.

    On interruption preserve .save-backup/{old,new} and refuse formal reading.
    A normal completed retry only verifies the exact current approval/content.
    """
    hooks = fault_hooks or {}
    # Reject malformed/AI approvals before creating a target or lock file.
    try:
        approval = SaveApproval.model_validate(approval.model_dump(mode="json"))
    except (AttributeError, ValueError) as exc:
        raise GoldApprovalError(f"Invalid human approval: {exc}") from exc
    pair = read_compact_gold_pair(pair_path)
    report = check_compact_gold_pair(pair, material_root=material_root)
    if report.blocking_count:
        raise GoldCheckError("Gold pair/material checks failed")
    binding = _binding(
        pair, target, approval.binding.expected_old, report.pending_ids()
    )
    _verify_approval(approval, binding)
    target.initialize()
    with SpaceLock(target.root):
        current = target.read_current()
        if (
            current
            and current[1] == approval
            and current[1].binding.pair_sha256 == binding.pair_sha256
        ):
            return SaveResult(
                target_path=str(target.root),
                pair_sha256=binding.pair_sha256,
                already_present=True,
            )
        # Full recheck inside the lock, including material files and old content.
        checked = check_save(
            pair_path=pair_path,
            material_root=material_root,
            target=target,
            expected_old=approval.binding.expected_old,
        )
        _verify_approval(approval, checked.binding)
        pair = read_compact_gold_pair(pair_path)
        if (
            _binding(pair, target, binding.expected_old, checked.report.pending_ids())
            != binding
        ):
            raise GoldApprovalError("Pair changed during save")
        payload = {
            "answer.json": compact_answer_to_json(pair.answer).encode(),
            "metadata.json": compact_metadata_to_json(pair.metadata).encode(),
            "approval.json": _json_bytes(approval.model_dump(mode="json")),
        }
        # Assemble complete rollback and replacement sets before touching current.
        temporary = target.root.parent / f".{target.root.name}.save-{uuid.uuid4().hex}"
        temporary.mkdir()
        for kind in ("old", "new"):
            (temporary / kind).mkdir()
        if current:
            for name in CURRENT_FILES:
                _atomic_write(
                    temporary / "old" / name, (target.root / name).read_bytes()
                )
        for name, data in payload.items():
            _atomic_write(temporary / "new" / name, data)
        _atomic_write(
            temporary / "README.txt",
            b"Restore all three files from old (or remove current files if old is empty), or all three from new. Verify content and approval before removing .save-backup. Never mix sets.\n",
        )
        _fsync_dir(temporary)
        os.rename(temporary, target.root / PENDING)
        _fsync_dir(target.root)
        if "after_backup" in hooks:
            hooks["after_backup"]()
        for name, data in payload.items():
            _atomic_write(target.root / name, data)
            if f"after_{name}" in hooks:
                hooks[f"after_{name}"]()
        # Pending blocks reads until the entire triplet has been verified.
        loaded = read_compact_gold_pair(target.root)
        if loaded.answer != pair.answer or loaded.metadata != pair.metadata:
            raise GoldIntegrityError("Saved pair differs; retain rollback")
        if (
            SaveApproval.model_validate_json(
                (target.root / "approval.json").read_bytes()
            )
            != approval
        ):
            raise GoldIntegrityError("Saved approval differs; retain rollback")
        shutil.rmtree(target.root / PENDING)
        _fsync_dir(target.root)
        if "after_complete" in hooks:
            hooks["after_complete"]()
        return SaveResult(target_path=str(target.root), pair_sha256=binding.pair_sha256)
