"""Validate complete snapshots and publish a new directory without overwriting old data."""

import fcntl
import json
import os
import shutil
import tempfile
from pathlib import Path

from ..records.catalog import CatalogManifest, CatalogStar
from ..records.evidence_audit import EvidenceAudit
from ..records.literature_contribution import LiteratureContribution
from .corrections import corrected_items, corrected_papers, correction_node, marker
from .inputs import LoadedPaper, digest, read_bound
from .provenance import verify_associations


def verify_catalog(root):
    root = Path(root).resolve()
    manifest, documents = _read_snapshot(root)
    ids, nodes = set(), set()
    papers = {}
    expected_files = {"manifest.json"}
    if manifest.schema_version in {2, 3, 4, 5}:
        expected_files.update(("papers/index.json", "stars/index.json"))
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
    corrections = {}
    for star in documents.values():
        local = {
            f"{key}#/stars/{o.result_index}"
            for key, source in star.sources.items()
            for o in source.objects
        }
        for d in star.review_decisions:
            if d.type != "correct":
                continue
            if correction_node(d) not in local:
                raise ValueError("Correction is attached to another object.")
            if d.paper not in manifest.papers or (d.science_sha256, d.pdf_sha256) != (
                manifest.papers[d.paper].science.sha256,
                manifest.papers[d.paper].source.pdf_sha256,
            ):
                raise ValueError("Applied correction has stale source bindings.")
            if d.id in corrections and corrections[d.id] != d:
                raise ValueError("Conflicting correction provenance.")
            corrections[d.id] = d
    effective = corrected_papers(papers, corrections.values())
    for item in manifest.stars:
        star = documents[item.file.path]
        if (
            star.catalog_id != item.catalog_id
            or star.display_name != item.display_name
            or star.data_version != manifest.data_version
            or star.schema_version != manifest.schema_version
            or item.file.path != "stars/" + star.catalog_id + ".json"
        ):
            raise ValueError("Star index differs from the stellar document.")
        if star.catalog_id in ids or star.catalog_id in manifest.retired_ids:
            raise ValueError("Duplicate or retired identity in the snapshot.")
        verify_associations(star, manifest, effective)
        if manifest.schema_version == 5:
            from .units import standard_value

            for q in star.quantities:
                for value in q.values:
                    if value.standard != standard_value(q.quantity, value):
                        raise ValueError(
                            "Standard-unit derivation differs from the original value."
                        )
        ids.add(star.catalog_id)
        expected_files.add(item.file.path)
        if manifest.schema_version == 3:
            expected_files.add("provenance/simbad/" + star.catalog_id + ".json")
        elif manifest.schema_version in {4, 5}:
            # The reader has checked the current or legacy per-star filename.
            raw = json.loads(read_bound(root, item.file))
            expected_files.add(raw["provenance"]["path"])
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
                original = effective[key].stars[obj.result_index]
                expected_types.append(
                    {
                        **original.contribution_type.model_dump(mode="json"),
                        "source": key,
                    }
                )
                for kind, quantity, _, content, d in corrected_items(
                    key, papers[key], obj.result_index, corrections.values()
                ):
                    if content is None:
                        continue
                    content = {
                        **content,
                        "source": key,
                        **({"correction": marker(d)} if d else {}),
                    }
                    if kind == "identifier":
                        expected_identifiers.append(content)
                    else:
                        expected_quantities.setdefault(quantity, []).append(content)
        if sorted(local_nodes) != sorted(item.nodes):
            raise ValueError("Manifest node index differs from embedded sources.")
        if (
            [v.model_dump(mode="json") for v in star.identifiers]
            != expected_identifiers
            or [v.model_dump(mode="json") for v in star.contribution_types]
            != expected_types
            or {
                q.quantity: [
                    v.model_dump(
                        mode="json",
                        exclude={"attribution", "attribution_reason", "standard"},
                    )
                    for v in q.values
                ]
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


def _read_snapshot(root):
    if json.loads((root / "manifest.json").read_bytes()).get("schema_version") in {
        2,
        3,
        4,
        5,
    }:
        from .readable import read_snapshot

        return read_snapshot(root)
    manifest = CatalogManifest.model_validate_json(
        (root / "manifest.json").read_bytes()
    )
    return manifest, {
        s.file.path: CatalogStar.model_validate_json(read_bound(root, s.file))
        for s in manifest.stars
    }


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
    _, documents = _read_snapshot(root)
    stars = [documents[s.file.path] for s in manifest.stars]
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


def check_release(queue):
    """A release requires reviewed contributing papers and no pending review items."""
    pending = queue["counts"]["pending"]
    if pending:
        raise ValueError(f"Release has {pending} pending review items.")
    return queue
