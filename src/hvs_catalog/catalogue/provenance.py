"""Identity-only endpoint provenance and independently checkable association bindings."""

from ..records.catalog import AssociationOrigin, SimbadQuery, SkyPosition
from ..records.literature_contribution import LiteratureMetadata
from .identity import coordinate, keys, separation
from .simbad import identifier_inputs


def paper_origin(source, science, index):
    """Embed identity evidence without duplicating source measurements."""
    return AssociationOrigin(
        source=LiteratureMetadata.model_validate(
            {k: getattr(source, k) for k in LiteratureMetadata.model_fields}
        ),
        result_sha256=source.result_sha256,
        pdf_sha256=source.pdf_sha256,
        pdf_url=source.pdf_url,
        pdf_origin=source.pdf_origin,
        result_index=index,
        identifiers=science.stars[index].identifiers,
    )


def verify_associations(star, manifest, papers):
    """Require every paper endpoint and external coordinate binding to be interpretable."""
    external_bindings = []
    for query in star.simbad.queries:
        for binding in query.get("identifier_inputs", []):
            node = binding["source_record"]
            paper, index = node.split("#/stars/")
            if (
                paper not in star.sources
                or not any(
                    o.result_index == int(index) for o in star.sources[paper].objects
                )
                or binding
                not in identifier_inputs([(node, papers[paper].stars[int(index)])]).get(
                    query["query"].get("identifier"), []
                )
            ):
                raise ValueError(
                    "SIMBAD identifier query differs from its source evidence."
                )
    decisions = {d.id: d for d in star.review_decisions}
    for d in decisions.values():
        if d.type == "correct":
            continue  # Original content and bindings were verified before constructing this view.
        if any(
            key not in manifest.papers or manifest.papers[key].science.sha256 != digest
            for key, digest in d.papers.items()
        ):
            raise ValueError("Applied review decision has stale paper bindings.")
        local = {
            f"{key}#/stars/{o.result_index}"
            for key, source in star.sources.items()
            for o in source.objects
        }
        target = {n for g in d.groups for n in g}
        if not local & target:
            raise ValueError("Review decision does not affect this object.")
        if d.type == "merge" and not target <= local:
            raise ValueError("Applied merge is absent from source membership.")
        if d.type in {"name", "simbad"} and target != local:
            raise ValueError("Review decision targets a different final object group.")
        if d.type == "name" and star.display_name != d.display_name:
            raise ValueError("Display name differs from its approved decision.")
        if d.type == "name" and star.aliases != d.aliases:
            raise ValueError("Display aliases differ from their approved decision.")
        if d.type == "separate" and sum(bool(set(g) & local) for g in d.groups) > 1:
            raise ValueError("Object violates an approved separation.")
    if star.aliases and not any(d.type == "name" for d in decisions.values()):
        raise ValueError("Display aliases lack an approved name decision.")
    if star.simbad.method == "review":
        matching = [
            d
            for d in decisions.values()
            if d.type == "simbad" and d.main_id == star.simbad.selected.main_id
        ]
        if len(matching) != 1:
            raise ValueError("Reviewed SIMBAD correspondence lacks its decision.")
    for edge in star.associations:
        endpoints = {edge.left, edge.right}
        paper_endpoints = {e for e in endpoints if not e.startswith("simbad:")}
        if set(edge.origins) != paper_endpoints:
            raise ValueError("Association origin coverage is incomplete.")
        for endpoint, origin in edge.origins.items():
            key = origin.source.arxiv_id + origin.source.version
            node = f"{key}#/stars/{origin.result_index}"
            if endpoint != node and not (
                endpoint.startswith("@") and endpoint.endswith(":" + node)
            ):
                raise ValueError("Association origin names another source object.")
            if not edge.historical and not endpoint.startswith("@"):
                if key not in papers or origin.result_index >= len(papers[key].stars):
                    raise ValueError(
                        "Association origin is outside the current papers."
                    )
                expected = paper_origin(
                    manifest.papers[key].source, papers[key], origin.result_index
                )
                if origin != expected:
                    raise ValueError(
                        "Association origin differs from its fixed source."
                    )
        if edge.method == "paper":
            paper_keys = {
                o.source.arxiv_id + o.source.version for o in edge.origins.values()
            }
            if edge.details.get("source") not in paper_keys or not edge.details.get(
                "evidence"
            ):
                raise ValueError("Paper association origin or evidence is missing.")
        if edge.method == "review":
            d = decisions.get(edge.details.get("decision_id"))
            if d is None or d.type != "merge" or edge.details.get("reason") != d.reason:
                raise ValueError(
                    "Review association lacks its approved merge decision."
                )
        if (
            edge.method == "identifier"
            and edge.accepted
            and any(
                edge.details.get("identifier") not in keys(origin)
                for origin in edge.origins.values()
            )
        ):
            raise ValueError("Identifier association is not supported by its origins.")
        if not edge.right.startswith("simbad:"):
            continue
        d = edge.details
        if edge.method != "coordinates" or edge.left not in edge.origins:
            raise ValueError("Unsupported external SIMBAD association.")
        required = {
            "left_position",
            "right_position",
            "separation_arcsec",
            "threshold_arcsec",
            "epoch_propagation",
            "query",
            "selection",
            "candidates",
        }
        if not required <= d.keys():
            raise ValueError("SIMBAD coordinate association evidence is incomplete.")
        left = SkyPosition.model_validate(d["left_position"])
        right = SkyPosition.model_validate(d["right_position"])
        query = SimbadQuery.model_validate(d["query"]["query"])
        if (
            query.method != "coordinates"
            or query.position != left.model_copy(update={"evidence": []})
            or not d["query"].get("response_sha256")
            or not d["query"].get("queried_at")
            or not left.evidence
            or d["threshold_arcsec"] != 5.0
            or d["epoch_propagation"] is not False
            or d["selection"] != "nearest"
            or abs(separation(left, right) - d["separation_arcsec"]) > 1e-8
            or d["separation_arcsec"] > 5.0 + 1e-9
        ):
            raise ValueError("SIMBAD coordinate association binding is inconsistent.")
        main_id = edge.right.removeprefix("simbad:")
        if not any(c.get("main_id") == main_id for c in d["candidates"]):
            raise ValueError("SIMBAD coordinate association lacks its candidate.")
        if not edge.historical and not edge.left.startswith("@"):
            origin = edge.origins[edge.left]
            key = origin.source.arxiv_id + origin.source.version
            if coordinate(papers[key].stars[origin.result_index]) != left:
                raise ValueError("SIMBAD association changed the source coordinates.")
        if edge.accepted:
            external_bindings.append(edge)
    if (
        star.simbad.status == "matched"
        and star.simbad.method == "coordinates"
        and not any(
            e.right == "simbad:" + star.simbad.selected.main_id
            and e.details["query"] in star.simbad.queries
            for e in external_bindings
        )
    ):
        raise ValueError("Matched coordinate SIMBAD snapshot lacks an association.")
