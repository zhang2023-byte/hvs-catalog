"""Publish one annotation atomically; keep operational files outside data/."""

import fcntl
import hashlib
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from ..workspace import WorkspaceBusyError
from .gold import annotation_path, check_annotation, read_annotation


@contextmanager
def data_lock(data):
    """Serialize publication on the stable data directory without a lock file."""
    fd = os.open(Path(data), os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WorkspaceBusyError("Gold data directory is busy.") from None
        yield
    finally:
        os.close(fd)


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _target(target):
    target = Path(target).absolute()
    if target.is_symlink() or target.parent.is_symlink():
        raise ValueError("Gold targets must not be symbolic links.")
    if target.parent.name != "data" or not target.parent.is_dir():
        raise ValueError(
            "Target must be a paper directory directly inside existing data/."
        )
    return target.resolve()


def _current(target):
    if not target.exists():
        return None
    if not target.is_dir() or {p.name for p in target.iterdir()} != {
        "paper.pdf",
        "annotation.json",
    }:
        raise ValueError("Unknown or incomplete Gold layout; migrate explicitly.")
    for name in ("annotation.json", "paper.pdf"):
        if (target / name).is_symlink():
            raise ValueError("Gold files must not be symbolic links.")
    annotation = read_annotation(target)
    if annotation.metadata.arxiv_id.replace("/", "_") != target.name:
        raise ValueError("Annotation paper differs from the target directory.")
    if _digest(target / "paper.pdf") != annotation.metadata.material.sha256:
        raise ValueError("Current PDF fingerprint differs from its annotation.")
    return _digest(target / "annotation.json")


def inspect_save(*, annotation, target, expected_old=None):
    target = _target(target)
    source = annotation_path(annotation)
    value = read_annotation(source)
    report = check_annotation(value, material_root=source.parent)
    if not report.ready:
        raise ValueError("; ".join(report.errors))
    if value.metadata.arxiv_id.replace("/", "_") != target.name:
        raise ValueError("Annotation paper differs from the target directory.")
    new = _digest(source)
    old = _current(target)
    if old not in (expected_old, new):
        raise ValueError("Current annotation differs from expected_old.")
    if (
        old is not None
        and _digest(target / "paper.pdf") != value.metadata.material.sha256
    ):
        raise ValueError(
            "Changing the fixed PDF requires an explicit material migration."
        )
    return {
        "target": str(target),
        "annotation_sha256": new,
        "expected_old": old,
        "already_present": old == new,
    }


def read_saved(target):
    target = _target(target)
    with data_lock(target.parent):
        return {"annotation_sha256": _current(target)}


def save_annotation(
    *, annotation, target, expected_new, expected_old=None, fault_hooks=None
):
    """Save explicitly selected content; hashes guard changes, not human identity."""
    target = _target(target)
    hooks = fault_hooks or {}
    with data_lock(target.parent):
        result = inspect_save(
            annotation=annotation, target=target, expected_old=expected_old
        )
        if result["annotation_sha256"] != expected_new:
            raise ValueError("Candidate annotation differs from expected_new.")
        if result["already_present"]:
            return result
        work = target.parent.parent / ".work" / "gold-save"
        work.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(dir=work))
        try:
            source = annotation_path(annotation)
            shutil.copyfile(source, stage / "annotation.json")
            shutil.copyfile(source.parent / "paper.pdf", stage / "paper.pdf")
            # Revalidate the staged bytes so source changes cannot race publication.
            staged = inspect_save(
                annotation=stage / "annotation.json",
                target=target,
                expected_old=expected_old,
            )
            if staged["annotation_sha256"] != expected_new:
                raise ValueError("Candidate changed while staging.")
            for file in stage.iterdir():
                with file.open("rb") as stream:
                    os.fsync(stream.fileno())
            stage_fd = os.open(stage, os.O_RDONLY)
            try:
                os.fsync(stage_fd)
            finally:
                os.close(stage_fd)
            hooks.get("before_publish", lambda: None)()
            if target.exists():
                os.replace(stage / "annotation.json", target / "annotation.json")
            else:
                os.rename(stage, target)
            hooks.get("after_publish", lambda: None)()
            for directory in (target, target.parent):
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            return result
        finally:
            if stage.exists():
                shutil.rmtree(stage)
