"""One PDF and metadata document per paper, with explicit parser variants."""

import hashlib
import re
from pathlib import Path

from ..materials.package import read_material_package
from ..records.common import ArtifactRef, canonical_json
from ..workspace.layout import resolve_within

VARIANTS = ("mineru_local", "mineru_cloud")


def paper_id(value):
    if not re.fullmatch(r"\d{4}\.\d{4,5}", value):
        raise ValueError("Invalid arXiv paper ID.")
    return value


def round_id(value):
    if not re.fullmatch(r"exec\d{2,}", value):
        raise ValueError("Invalid benchmark round ID.")
    return value


def selected_package(root, paper, variant):
    if variant not in VARIANTS:
        raise ValueError("Select mineru_local or mineru_cloud explicitly.")
    directory = resolve_within(Path(root), f"materials/{paper_id(paper)}/{variant}")
    package = read_material_package(directory, root=root)
    if package.source.paper_key != paper:
        raise ValueError("Selected materials identify a different paper.")
    return directory, package


def selection(root, papers, variant):
    if not papers or len(set(papers)) != len(papers):
        raise ValueError("Select a nonempty list of distinct papers.")
    result = []
    for paper in papers:
        directory, package = selected_package(root, paper, variant)
        manifest = directory / "material.json"
        result.append(
            {
                "arxiv_id": paper,
                "source_version": package.source.source_version,
                "package": str(manifest.relative_to(root)),
                "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "pdf_sha256": next(
                    f.sha256 for f in package.source.files if f.role == "pdf"
                ),
            }
        )
    return result


def migrate_package(source, target, paper, variant):
    """Copy verified original bytes; change only root-relative manifest references."""
    package = read_material_package(source)
    paper_id(paper)
    if variant not in VARIANTS or package.source.paper_key != paper:
        raise ValueError("Material variant or paper identity mismatch.")
    prefix = f"materials/{paper}/{variant}"
    files = []
    for f in package.source.files:
        if f.role == "pdf":
            path = f"materials/{paper}/paper.pdf"
        else:
            names = {
                "parser_output": "result.zip",
                "derived_text": "manuscript.md",
                "pdf_index": "pdf-blocks.json",
            }
            path = (
                prefix
                + "/"
                + (
                    "images/" + Path(f.path).name
                    if f.role == "pdf_image"
                    else names.get(f.role, f.path)
                )
            )
        data = resolve_within(source, f.path).read_bytes()
        ref = target.publish_file(path, data)
        if ref.sha256 != f.sha256:
            raise ValueError("A source attachment changed during migration.")
        files.append(f.model_copy(update={"path": path}))
    metadata = target.publish_file(
        f"materials/{paper}/metadata.json",
        resolve_within(source, package.metadata.path).read_bytes(),
    )
    if metadata.sha256 != package.metadata.sha256:
        raise ValueError("Metadata changed during migration.")
    declared = {f.path for f in package.source.files} | {
        package.metadata.path,
        "material.json",
    }
    extras = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if (
            not path.is_file()
            or "mineru_cloud" in relative.parts
            or path.name == ".DS_Store"
        ):
            continue
        if relative.as_posix() not in declared:
            name = relative.as_posix()
            name = name if name.startswith("images/") else "raw/" + name
            extras.append(
                target.publish_file(
                    prefix + "/" + name, resolve_within(source, relative).read_bytes()
                )
            )
    updated = package.model_copy(
        update={
            "source": package.source.model_copy(update={"files": files}),
            "metadata": metadata,
            "parser_files": extras,
        }
    )
    target.publish_file(
        prefix + "/material.json",
        (canonical_json(updated.model_dump(mode="json")) + "\n").encode(),
    )
    selected_package(target.root, paper, variant)
    return updated


def material_map(root, paper, variant):
    _, package = selected_package(root, paper, variant)
    return {
        f.sha256: ArtifactRef(path=f.path, sha256=f.sha256)
        for f in package.source.files
    }
