"""Validate complete snapshots and publish a new directory without overwriting old data."""

import fcntl
import os
import shutil
import tempfile
from pathlib import Path

from ..records.catalog import CatalogManifest, CatalogStar
from ..records.evidence_audit import EvidenceAudit
from ..records.literature_contribution import LiteratureContribution
from .inputs import LoadedPaper, digest, read_bound
from .provenance import verify_associations


def verify_catalog(root):
    root = Path(root).resolve()
    manifest = CatalogManifest.model_validate_json(
        (root / "manifest.json").read_bytes()
    )
    ids, display_names, nodes = set(), set(), set()
    papers = {}
    expected_files = {"manifest.json"}
    for key, item in manifest.papers.items():
        science_raw = read_bound(root, item.science)
        science = LiteratureContribution.model_validate_json(science_raw)
        audit = EvidenceAudit.model_validate_json(read_bound(root, item.audit))
        if key != science.metadata.arxiv_id + science.metadata.version:
            raise ValueError("Manifest paper key is inconsistent.")
        if (
            item.source.result_sha256 != item.science.sha256
            or audit.metadata.result_ref != item.science
        ):
            raise ValueError("Paper science and audit binding is inconsistent.")
        if science.metadata.model_dump() != {
            k: getattr(item.source, k) for k in type(science.metadata).model_fields
        }:
            raise ValueError("Published paper metadata is inconsistent.")
        if science.notes != item.source.paper_notes:
            raise ValueError("Published paper notes changed.")
        if len(item.source.objects) != len(science.stars):
            raise ValueError("Source-object completeness mapping is incomplete.")
        papers[key] = science
        expected_files.update((item.science.path, item.audit.path))
    for item in manifest.stars:
        star = CatalogStar.model_validate_json(read_bound(root, item.file))
        if (
            star.catalog_id != item.catalog_id
            or star.display_name != item.display_name
            or star.data_version != manifest.data_version
            or item.file.path != "stars/" + star.catalog_id + ".json"
        ):
            raise ValueError("Star index differs from the stellar document.")
        if (
            star.catalog_id in ids
            or star.display_name in display_names
            or star.catalog_id in manifest.retired_ids
        ):
            raise ValueError("Duplicate or retired identity in the snapshot.")
        verify_associations(star, manifest, papers)
        ids.add(star.catalog_id)
        display_names.add(star.display_name)
        expected_files.add(item.file.path)
        expected_identifiers, expected_types, expected_quantities = [], [], {}
        local_nodes = []
        for key, source in star.sources.items():
            if key not in manifest.papers:
                raise ValueError("Star names a paper outside the manifest.")
            paper_source = manifest.papers[key].source
            if source.model_dump(exclude={"objects"}) != paper_source.model_dump(
                exclude={"objects"}
            ):
                raise ValueError("Embedded source metadata changed.")
            for obj in source.objects:
                node = f"{key}#/stars/{obj.result_index}"
                if node in nodes or obj not in paper_source.objects:
                    raise ValueError("Duplicate or inconsistent source-object mapping.")
                nodes.add(node)
                local_nodes.append(node)
                original = papers[key].stars[obj.result_index]
                expected_identifiers.extend(
                    {**v.model_dump(mode="json"), "source": key}
                    for v in original.identifiers
                )
                expected_types.append(
                    {
                        **original.contribution_type.model_dump(mode="json"),
                        "source": key,
                    }
                )
                for q in original.quantities:
                    expected_quantities.setdefault(q.quantity, []).extend(
                        {**v.model_dump(mode="json"), "source": key} for v in q.values
                    )
        if sorted(local_nodes) != sorted(item.nodes):
            raise ValueError("Manifest node index differs from embedded sources.")
        if (
            [v.model_dump(mode="json") for v in star.identifiers]
            != expected_identifiers
            or [v.model_dump(mode="json") for v in star.contribution_types]
            != expected_types
            or {
                q.quantity: [v.model_dump(mode="json") for v in q.values]
                for q in star.quantities
            }
            != expected_quantities
        ):
            raise ValueError(
                "Scientific values or evidence changed during catalog assembly."
            )
    expected_nodes = {
        f"{key}#/stars/{i}"
        for key, paper in papers.items()
        for i in range(len(paper.stars))
    }
    if nodes != expected_nodes:
        raise ValueError("The catalog lost or duplicated source contributions.")
    actual_files = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
    if actual_files != expected_files:
        raise ValueError("Unexpected or missing files in the catalog snapshot.")
    allocated = [int(k.rsplit("-", 1)[1]) for k in ids | set(manifest.retired_ids)]
    if allocated and manifest.next_id <= max(allocated):
        raise ValueError("The next catalog ID would reuse an allocated number.")
    return manifest


def load_previous(root):
    manifest = verify_catalog(root)
    root = Path(root)
    papers = [
        LoadedPaper(
            LiteratureContribution.model_validate_json(read_bound(root, p.science)),
            read_bound(root, p.science),
            EvidenceAudit.model_validate_json(read_bound(root, p.audit)),
            read_bound(root, p.audit),
            p.source,
        )
        for p in manifest.papers.values()
    ]
    stars = [
        CatalogStar.model_validate_json(read_bound(root, s.file))
        for s in manifest.stars
    ]
    return manifest, papers, stars


def publish(root, files, *, hook=None):
    """Publish only to a new destination; a failed build leaves existing snapshots intact."""
    root = Path(root).resolve()
    root.parent.mkdir(parents=True, exist_ok=True)
    lock_path = root.parent / ("." + root.name + ".lock")
    with lock_path.open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if root.exists():
            raise ValueError(
                "Catalog destination already exists; choose a new snapshot directory."
            )
        staging = Path(tempfile.mkdtemp(prefix="." + root.name + "-", dir=root.parent))
        try:
            for relative, raw in files.items():
                path = staging / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            verify_catalog(staging)
            if hook:
                hook("before_publish", staging)
            os.rename(staging, root)
            descriptor = os.open(root.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def artifact(path, raw):
    return {"path": path, "sha256": digest(raw)}
