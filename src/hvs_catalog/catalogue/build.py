"""Associate fixed source results and assemble complete, reproducible stellar JSON files."""

from collections import Counter
from importlib.metadata import version
from itertools import pairwise
from pathlib import Path

from ..records.catalog import (
    CatalogManifest,
    CatalogStar,
)
from ..records.common import sha256_of
from .association import Node, associate
from .identity import display_name, gaia_conflicts, identifier_key, keys, names
from .inputs import digest, json_bytes, load_papers
from .provenance import paper_origin
from .publication import artifact, load_previous, publish
from .simbad import (
    FrozenSimbad,
    combine_snapshots,
    coordinate_associations,
    previous_identity_conflicts,
    resolve_simbad,
    selected_candidates,
)


def node_key(key, index):
    return f"{key}#/stars/{index}"


def prepare_groups(selection, previous=None):
    incoming = load_papers(selection)
    prior_manifest, prior_stars, previous_hash = None, [], None
    papers, prior_papers_by_key = {}, {}
    if previous is not None:
        prior_manifest, prior_papers, prior_stars = load_previous(previous)
        previous_hash = digest((Path(previous) / "manifest.json").read_bytes())
        papers = {p.source.arxiv_id: p for p in prior_papers}
        prior_papers_by_key = {p.key: p for p in prior_papers}
    papers.update({p.source.arxiv_id: p for p in incoming})
    papers = {p.key: p for _, p in sorted(papers.items())}
    if set(selection.simbad_refresh) - {s.catalog_id for s in prior_stars}:
        raise ValueError("SIMBAD refresh names an unknown previous object.")
    retired = dict(prior_manifest.retired_ids) if prior_manifest else {}
    retired.update(selection.retired_ids)
    if any(len(set(v)) != len(v) or k in v for k, v in retired.items()):
        raise ValueError("Invalid retired ID mapping.")
    assignments = selection.identity_assignments
    nodes = [
        Node(
            node_key(key, i),
            star,
            assignments.get(node_key(key, i)),
            origin=paper_origin(paper.source, paper.science, i),
        )
        for key, paper in papers.items()
        for i, star in enumerate(paper.science.stars)
    ]
    if set(assignments) - {n.key for n in nodes}:
        raise ValueError("Identity assignment names an unknown source object.")
    if set(assignments.values()) & set(retired):
        raise ValueError("A retired ID cannot be reassigned.")
    node_map = {n.key: n for n in nodes}
    previous_links = []
    for old in prior_stars:
        target_ids = retired.get(old.catalog_id, [old.catalog_id])
        if len(target_ids) != 1:
            if any(
                key not in assignments
                for key in node_map
                if key
                in {
                    node_key(k, o.result_index)
                    for k, s in old.sources.items()
                    for o in s.objects
                }
            ):
                raise ValueError(
                    "A split requires explicit assignments for retained source objects."
                )
            continue
        target_id = target_ids[0]
        anchor_keys = []
        for key, source in old.sources.items():
            prior_paper = prior_papers_by_key[key]
            for obj in source.objects:
                node = node_key(key, obj.result_index)
                anchor_key = "@" + old.catalog_id + ":" + node
                nodes.append(
                    Node(
                        anchor_key,
                        prior_paper.science.stars[obj.result_index],
                        target_id,
                        True,
                        old.catalog_id,
                        paper_origin(source, prior_paper.science, obj.result_index),
                    )
                )
                anchor_keys.append(anchor_key)
                if (
                    key in papers
                    and papers[key].source.result_sha256 == source.result_sha256
                    and (node not in assignments or assignments[node] == target_id)
                ):
                    node_map[node].catalog_id = target_id
                    previous_links.append(
                        (
                            anchor_key,
                            node,
                            {
                                "basis": "Unchanged source in the previous manifest.",
                                "manifest_sha256": previous_hash,
                            },
                        )
                    )
        for left, right in pairwise(anchor_keys):
            previous_links.append(
                (
                    left,
                    right,
                    {
                        "basis": "Source objects share one previous catalog ID.",
                        "catalog_id": old.catalog_id,
                        "manifest_sha256": previous_hash,
                    },
                )
            )
    groups = associate(nodes, selection.explicit_associations, previous_links)
    associate_previous_simbad(groups, prior_stars, previous_hash)
    return papers, groups, prior_manifest, prior_stars, previous_hash, retired


def associate_previous_simbad(groups, prior_stars, previous_hash):
    """Reuse fixed reliable identity links independently of enrichment refresh."""
    anchors = {}
    for key, node in sorted(groups.nodes.items()):
        if node.previous_id:
            anchors.setdefault(node.previous_id, key)
    reliable = {
        s.catalog_id: s.simbad
        for s in prior_stars
        if s.catalog_id in anchors
        and s.simbad.status == "matched"
        and s.simbad.method == "identifier"
    }
    aliases = {
        old_id: {
            key
            for candidate in selected_candidates(snapshot)
            for name in [candidate["main_id"], *candidate["aliases"]]
            if (key := identifier_key(name)) is not None
        }
        for old_id, snapshot in reliable.items()
    }
    for members in groups.groups():
        scientific = groups.scientific(members)
        reported = {key for _, star in scientific for key in keys(star)}
        matches = {
            old_id: sorted(reported & known)
            for old_id, known in aliases.items()
            if reported & known
        }
        if not matches:
            continue
        component = set().union(
            *(
                groups.members[groups.find(k)]
                for k in [members[0], *(anchors[old_id] for old_id in matches)]
            )
        )
        involved = sorted(
            {groups.nodes[k].previous_id for k in component} & reliable.keys()
        )
        evidence = [
            {
                "node": anchors[old_id],
                "candidate": candidate,
                "queries": reliable[old_id].queries,
            }
            for old_id in involved
            for candidate in selected_candidates(reliable[old_id])
        ]
        reason = None
        if len({reliable[k].selected.main_id for k in matches}) > 1:
            reason = "Frozen aliases identify different external objects."
        elif len({groups.nodes[k].catalog_id for k in component} - {None}) > 1:
            reason = "Frozen aliases identify distinct catalog IDs; explicit revision required."
        for old_id, matched in matches.items():
            left = next(key for key, star in scientific if keys(star) & set(matched))
            right = anchors[old_id]
            if groups.find(left) == groups.find(right):
                continue
            groups.join(
                left,
                right,
                "simbad",
                {
                    "main_id": reliable[old_id].selected.main_id,
                    "basis": "Reliable identifier in a frozen previous SIMBAD snapshot.",
                    "manifest_sha256": previous_hash,
                    "matched_identifiers": matched,
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
                rejection_reason=reason,
            )


def inherited_snapshots(groups, members, old_by_id, refresh):
    """Use original identities before retirement remaps the surviving catalog ID."""
    previous_ids = {
        groups.nodes[k].previous_id or groups.nodes[k].catalog_id for k in members
    } - {None}
    if previous_ids.intersection(refresh):
        return []
    return [
        old_by_id[k].simbad
        for k in sorted(previous_ids)
        if k in old_by_id and old_by_id[k].simbad.status != "not_queried"
    ]


def build_catalog(selection, output, *, previous=None, simbad=None, hook=None):
    output = Path(output).resolve()
    for paper in selection.papers:
        workspace = Path(paper.workspace).resolve()
        if (
            output == workspace
            or workspace in output.parents
            or output in workspace.parents
        ):
            raise ValueError("Publish the catalog outside the input workspace.")
    if previous and (
        output == Path(previous).resolve() or Path(previous).resolve() in output.parents
    ):
        raise ValueError("A new snapshot must be separate from the previous snapshot.")
    papers, groups, prior_manifest, prior_stars, previous_hash, retired = (
        prepare_groups(selection, previous)
    )
    frozen = FrozenSimbad(simbad)
    old_by_id = {s.catalog_id: s for s in prior_stars}
    snapshots = {}
    for members in groups.groups():
        scientific = groups.scientific(members)
        if not scientific:
            continue
        inherited = inherited_snapshots(
            groups, members, old_by_id, selection.simbad_refresh
        )
        snapshot = combine_snapshots(
            scientific,
            [
                *(inherited or [resolve_simbad(scientific, frozen)]),
                *previous_identity_conflicts(groups.edges, members),
            ],
        )
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
    allocated = {n.catalog_id for n in groups.nodes.values()} - {None}
    next_id = max(
        [
            prior_manifest.next_id if prior_manifest else 1,
            *[int(k.rsplit("-", 1)[1]) + 1 for k in allocated | set(retired)],
        ]
    )
    files, stars, star_index = {}, [], []
    for members in groups.groups():
        scientific = groups.scientific(members)
        if not scientific:
            continue
        ids = {groups.nodes[k].catalog_id for k in members} - {None}
        if len(ids) > 1:
            raise ValueError("Unresolved existing identity merge.")
        if ids:
            catalog_id = ids.pop()
        else:
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
        previous_members = {groups.nodes[k].previous_id for k in members} - {None}
        historical = [
            edge.model_copy(deep=True, update={"historical": True})
            for old in prior_stars
            if old.catalog_id in previous_members
            for edge in old.associations
        ]
        edges = list(
            {
                sha256_of(edge.model_dump(mode="json")): edge
                for edge in [*historical, *edges]
            }.values()
        )
        warnings = list(snapshot.warnings)
        if gaia_conflicts([name for _, star in scientific for name in names(star)]):
            warnings.append("Reported identifiers contain a Gaia release conflict.")
        if any(edge.accepted and edge.method == "coordinates" for edge in edges):
            warnings.append(
                "At least one association is based on coordinates only; no epoch propagation."
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
        previous_name = (
            old_by_id[catalog_id].display_name if catalog_id in old_by_id else None
        )
        stars.append(
            CatalogStar(
                data_version=selection.data_version,
                catalog_id=catalog_id,
                display_name=display_name(
                    scientific, snapshot, catalog_id, previous_name
                ),
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
    collisions = Counter(s.display_name for s in stars)
    live_ids = {s.catalog_id for s in stars}
    if len(live_ids) != len(stars):
        raise ValueError("An identity split requires explicit distinct catalog IDs.")
    for old in prior_stars:
        if old.catalog_id not in live_ids and old.catalog_id not in retired:
            raise ValueError(
                "A previous object disappeared; provide an explicit identity revision."
            )
    for old, successors in retired.items():
        if any(k not in live_ids and k not in retired for k in successors):
            raise ValueError("Retired ID successor is absent from this catalog.")
    for star in sorted(stars, key=lambda s: s.catalog_id):
        if collisions[star.display_name] > 1:
            star.display_name = star.catalog_id
            star.warnings.append("Display-name collision; using the catalog ID.")
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
        if (
            prior_manifest
            and key in prior_manifest.papers
            and prior_manifest.papers[key].source == paper.source
        ):
            original_audit_sha = prior_manifest.papers[key].original_audit_sha256
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
                "identity_assignments": selection.identity_assignments,
                "retired_ids": retired,
            }
        ),
        simbad_fingerprint=sha256_of(
            {
                "responses": frozen.used,
                "inherited": {
                    s.catalog_id: s.simbad.model_dump(mode="json") for s in prior_stars
                },
            }
        ),
        previous_manifest_sha256=previous_hash,
        papers=paper_index,
        stars=star_index,
        next_id=next_id,
        retired_ids=retired,
    )
    files["manifest.json"] = json_bytes(manifest)
    publish(output, files, hook=hook)
    return manifest
