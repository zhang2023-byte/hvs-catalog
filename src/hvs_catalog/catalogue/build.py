"""Associate fixed source results and assemble complete, reproducible stellar JSON files."""

from importlib.metadata import version
from itertools import pairwise
from pathlib import Path

from ..records.catalog import (
    CatalogManifest,
    CatalogStar,
    SkyPosition,
)
from ..records.common import sha256_of
from .association import Node, associate
from .evidence_conflicts import source_conflicts
from .identity import (
    coordinate_warnings,
    display_name,
    gaia_conflicts,
    names,
)
from .inputs import digest, json_bytes, load_papers
from .provenance import paper_origin
from .publication import artifact, publish, verify_catalog
from .simbad import (
    FrozenSimbad,
    combine_snapshots,
    coordinate_associations,
    resolve_simbad,
    selected_candidates,
)


def node_key(key, index):
    return f"{key}#/stars/{index}"


def prepare_groups(selection):
    """Compute identities from the complete selected extraction results."""
    papers = {p.key: p for p in sorted(load_papers(selection), key=lambda p: p.key)}
    nodes = [
        Node(
            node_key(key, i), star, origin=paper_origin(paper.source, paper.science, i)
        )
        for key, paper in papers.items()
        for i, star in enumerate(paper.science.stars)
    ]
    groups = associate(
        nodes, selection.explicit_associations, conflicts=source_conflicts(nodes)
    )
    return papers, groups


def build_catalog(selection, output, *, simbad=None, hook=None):
    output = Path(output).resolve()
    for paper in selection.papers:
        workspace = Path(paper.workspace).resolve()
        if (
            output == workspace
            or workspace in output.parents
            or output in workspace.parents
        ):
            raise ValueError("Publish the catalog outside the input workspace.")
    papers, groups = prepare_groups(selection)
    frozen = FrozenSimbad(simbad)
    snapshots = {}
    for members in groups.groups():
        scientific = groups.scientific(members)
        if not scientific:
            continue
        snapshot = resolve_simbad(scientific, frozen)
        snapshots[members[0]] = snapshot
    matches = {}
    for key, snapshot in snapshots.items():
        if snapshot.status == "matched" and snapshot.method == "identifier":
            matches.setdefault(snapshot.selected.main_id, []).append(key)
    for main_id, members in sorted(matches.items()):
        for left, right in pairwise(members):
            component = (
                groups.members[groups.find(left)] | groups.members[groups.find(right)]
            )
            evidence = [
                {
                    "node": key,
                    "candidate": candidate,
                    "queries": s.queries,
                }
                for key, s in snapshots.items()
                if key in component
                for candidate in selected_candidates(s)
            ]
            groups.join(
                left,
                right,
                "simbad",
                {
                    "main_id": main_id,
                    "basis": "Reliable identifier queries.",
                    "simbad_evidence": evidence,
                },
                external_names=[
                    name
                    for item in evidence
                    for name in [
                        item["candidate"]["main_id"],
                        *item["candidate"]["aliases"],
                    ]
                ],
            )
    next_id = 1
    files, stars, star_index = {}, [], []
    for members in groups.groups():
        scientific = groups.scientific(members)
        if not scientific:
            continue
        catalog_id = f"hvs-catalog-{next_id:06d}"
        next_id += 1
        relevant = [s for key, s in snapshots.items() if key in members]
        snapshot = combine_snapshots(scientific, relevant)
        sources, identifiers, types, quantities = {}, [], [], {}
        for node, star in scientific:
            key, index_text = node.split("#/stars/")
            paper = papers[key]
            if key not in sources:
                sources[key] = paper.source.model_copy(
                    deep=True, update={"objects": []}
                )
            sources[key].objects.append(paper.source.objects[int(index_text)])
            identifiers.extend(
                {**v.model_dump(mode="json"), "source": key} for v in star.identifiers
            )
            types.append(
                {**star.contribution_type.model_dump(mode="json"), "source": key}
            )
            for quantity in star.quantities:
                quantities.setdefault(quantity.quantity, []).extend(
                    {**v.model_dump(mode="json"), "source": key}
                    for v in quantity.values
                )
        edges = [
            edge
            for edge in groups.edges
            if edge.left in members or edge.right in members
        ]
        edges.extend(
            coordinate_associations(
                scientific, snapshot, {k: groups.nodes[k].origin for k, _ in scientific}
            )
        )
        warnings = list(snapshot.warnings)
        if gaia_conflicts([name for _, star in scientific for name in names(star)]):
            warnings.append("Reported identifiers contain a Gaia release conflict.")
        if any(edge.accepted and edge.method == "coordinates" for edge in edges):
            warnings.append(
                "At least one association is based on coordinates only; no epoch propagation."
            )
            for edge in edges:
                if edge.accepted and edge.method == "coordinates":
                    for side in ("left_position", "right_position"):
                        position = edge.details.get(side)
                        if position and position.get("evidence"):
                            warnings.extend(
                                coordinate_warnings(
                                    SkyPosition.model_validate(position)
                                )
                            )
        if any(not edge.accepted for edge in edges):
            warnings.append(
                "An identity association was rejected; inspect associations."
            )
        if snapshot.status in {"failed", "ambiguous", "conflict"}:
            warnings.append("SIMBAD enrichment is " + snapshot.status + ".")
        if any(
            source.result_status == "partial"
            or any(o.status != "complete" for o in source.objects)
            for source in sources.values()
        ):
            warnings.append(
                "Some source extraction results are incomplete; inspect sources."
            )
        if any(o.evidence_audit for source in sources.values() for o in source.objects):
            warnings.append(
                "Some evidence quotes have audit warnings; inspect sources."
            )
        stars.append(
            CatalogStar(
                data_version=selection.data_version,
                catalog_id=catalog_id,
                display_name=display_name(scientific, snapshot, catalog_id),
                sources=sources,
                identifiers=identifiers,
                contribution_types=types,
                quantities=[
                    {"quantity": q, "values": v} for q, v in quantities.items()
                ],
                simbad=snapshot,
                associations=edges,
                warnings=list(dict.fromkeys(warnings)),
            )
        )
    for star in sorted(stars, key=lambda s: s.catalog_id):
        path = "stars/" + star.catalog_id + ".json"
        raw = json_bytes(star)
        files[path] = raw
        star_index.append(
            {
                "catalog_id": star.catalog_id,
                "display_name": star.display_name,
                "file": artifact(path, raw),
                "nodes": [
                    node_key(k, o.result_index)
                    for k, s in star.sources.items()
                    for o in s.objects
                ],
            }
        )
    paper_index = {}
    for key, paper in papers.items():
        stem = key.replace("/", "_")
        folder = "papers/" + paper.source.arxiv_id.replace("/", "_") + "/"
        science_path = folder + stem + "__literature_contribution.json"
        audit_path = folder + stem + "__evidence_audit.json"
        science_ref = artifact(science_path, paper.science_bytes)
        audit = paper.audit.model_dump(mode="json")
        audit["metadata"]["result_ref"] = science_ref
        audit_raw = json_bytes(audit)
        files[science_path], files[audit_path] = paper.science_bytes, audit_raw
        original_audit_sha = digest(paper.audit_bytes)
        paper_index[key] = {
            "source": paper.source,
            "science": science_ref,
            "audit": artifact(audit_path, audit_raw),
            "original_audit_sha256": original_audit_sha,
        }
    manifest = CatalogManifest(
        data_version=selection.data_version,
        software_version=version("hvs-catalog"),
        input_fingerprint=sha256_of(
            {
                "papers": {
                    k: p.source.model_dump(mode="json") for k, p in papers.items()
                },
                "explicit_associations": [
                    e.model_dump(mode="json") for e in selection.explicit_associations
                ],
            }
        ),
        simbad_fingerprint=sha256_of({"responses": frozen.used}),
        previous_manifest_sha256=None,
        papers=paper_index,
        stars=star_index,
        next_id=next_id,
        retired_ids={},
    )
    from .readable import export_files

    export_files(manifest, stars, papers, files)
    publish(output, files, hook=hook)
    return verify_catalog(output)
