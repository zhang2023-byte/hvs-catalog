"""Apply approved, bound decisions and derive the publication review queue offline."""

import json
from collections import defaultdict
from itertools import combinations, pairwise, product
from pathlib import Path

from ..records.catalog import SimbadResponse
from ..records.catalog_review import CatalogReview
from ..records.common import sha256_of
from .corrections import corrected_items, corrected_papers, correction_node, marker
from .identity import gaia_conflicts, names, normalized_name
from .review_input import expand, fingerprint, load_review
from .simbad import query_key
from .units import standard_value


def value_paths(paper):
    for i, star in enumerate(paper.science.stars):
        for j, quantity in enumerate(star.quantities):
            for k, value in enumerate(quantity.values):
                yield f"/stars/{i}/quantities/{j}/values/{k}", quantity.quantity, value


class Review:
    def __init__(self, value, papers, frozen):
        stale = []
        if isinstance(value, (str, Path)):
            parts = load_review(value)
            value, stale = expand(parts, papers)
            self.fingerprint = fingerprint(parts)
        else:
            value = value or CatalogReview()
            self.fingerprint = sha256_of(value.model_dump(mode="json"))
        self.input = value
        self.papers = papers
        self.frozen = frozen
        self.valid, self.invalid, self.paper_reviews = [], list(stale), {}
        self.applied = {}
        self.responses = {}
        self.corrections = []
        nodes = {
            f"{key}#/stars/{i}"
            for key, p in papers.items()
            for i in range(len(p.science.stars))
        }
        for decision in self.input.decisions:
            if decision.type == "correct":
                reason = self._bindings({decision.paper: decision.science_sha256})
                if (
                    not reason
                    and papers[decision.paper].source.pdf_sha256 != decision.pdf_sha256
                ):
                    reason = "Fixed PDF hash changed."
                if reason:
                    self.invalidate(decision, reason)
                else:
                    self.corrections.append(decision)
                    self.applied[decision.id] = decision
                continue
            reason = self._bindings(decision.papers)
            if not reason and not {n for g in decision.groups for n in g} <= nodes:
                reason = "Source record no longer exists."
            for key, expected in decision.responses.items():
                response = self._response(key)
                if (
                    response is None
                    or sha256_of(response.model_dump(mode="json")) != expected
                ):
                    reason = "Frozen SIMBAD response changed or is missing."
            if reason:
                self.invalidate(decision, reason)
            else:
                self.valid.append(decision)
        for review in self.input.papers:
            reason = self._bindings({review.paper: review.science_sha256})
            if not reason:
                paths = {p for p, _, _ in value_paths(papers[review.paper])}
                if any(e.path not in paths for e in review.exceptions):
                    reason = "Reviewed value path no longer exists."
            if reason:
                self.invalid.append(
                    {
                        "kind": "paper_review",
                        "id": review.paper,
                        "reason": reason,
                        "status": "pending",
                    }
                )
            else:
                self.paper_reviews[review.paper] = review
        self.effective = corrected_papers(
            {k: p.science for k, p in papers.items()}, self.corrections
        )

    def _bindings(self, bindings):
        if any(
            key not in self.papers or self.papers[key].source.result_sha256 != digest
            for key, digest in bindings.items()
        ):
            return "Paper version or science hash changed or is missing."
        return None

    def _response(self, key):
        if key in self.responses:
            return self.responses[key]
        path = self.frozen.root / (key + ".json") if self.frozen.root else None
        if path is None or not path.exists():
            return None
        payload = json.loads(path.read_bytes())
        response = SimbadResponse.model_validate(payload["response"])
        if query_key(response.query) != key:
            raise ValueError("Review response key differs from the frozen query.")
        response = self.frozen.read(response.query)
        self.responses[key] = response
        return response

    def invalidate(self, decision, reason):
        self.invalid.append(
            {
                "kind": "decision",
                "id": decision.id,
                "groups": [[correction_node(decision)]]
                if decision.type == "correct"
                else decision.groups,
                "reason": reason,
                "status": "pending",
            }
        )

    def separation_pairs(self):
        result = {}
        for d in self.valid:
            if d.type != "separate":
                continue
            for a, b in combinations(d.groups, 2):
                for left, right in product(a, b):
                    result.setdefault(tuple(sorted((left, right))), []).append(d)
            self.applied[d.id] = d
        return result

    def merge(self, groups):
        for d in self.valid:
            if d.type != "merge":
                continue
            nodes = sorted(n for g in d.groups for n in g)
            for left, right in pairwise(nodes):
                if not groups.join(
                    left, right, "review", {"decision_id": d.id, "reason": d.reason}
                ):
                    raise ValueError(
                        "Review merge conflicts with identity evidence or separation: "
                        + d.id
                    )
            self.applied[d.id] = d

    def for_group(self, members, kind, *, final=True):
        matches = []
        for d in self.valid:
            if d.type != kind:
                continue
            target = set(d.groups[0])
            if target == set(members):
                matches.append(d)
            elif final and target & set(members):
                issue = {
                    "kind": "decision",
                    "id": d.id,
                    "groups": d.groups,
                    "reason": "Final object membership changed.",
                    "status": "pending",
                }
                if issue not in self.invalid:
                    self.invalid.append(issue)
        if len(matches) > 1:
            raise ValueError(
                "Conflicting review decisions for the same object and type."
            )
        if matches:
            self.applied[matches[0].id] = matches[0]
            return matches[0]
        return None

    def simbad(self, members, scientific, snapshot, *, final=True):
        d = self.for_group(members, "simbad", final=final)
        if d is None:
            return snapshot
        result = snapshot.model_copy(deep=True)
        if d.main_id is None:
            return result.model_copy(
                update={
                    "status": "ambiguous" if d.unresolved else "not_found",
                    "method": None,
                    "selected": None,
                    "reason": "Correspondence left unselected by review "
                    + d.id
                    + ": "
                    + d.reason,
                }
            )
        candidates = [
            c
            for key in d.responses
            for c in self.responses[key].candidates
            if c.main_id == d.main_id
        ]
        unique = {sha256_of(c.model_dump(mode="json")): c for c in candidates}
        if len(unique) != 1:
            raise ValueError(
                "Reviewed SIMBAD selection must identify one frozen representation: "
                + d.id
            )
        selected = next(iter(unique.values()))
        if gaia_conflicts(
            [
                selected.main_id,
                *selected.aliases,
                *(n for _, s in scientific for n in names(s)),
            ]
        ):
            raise ValueError(
                "Reviewed SIMBAD selection conflicts with reported Gaia identities: "
                + d.id
            )
        for key in d.responses:
            response = self.responses[key]
            entry = {
                "query": response.query.model_dump(mode="json"),
                "response_sha256": d.responses[key],
                "queried_at": response.queried_at,
                "client_version": response.client_version,
                "service_version": response.service_version,
                "error": response.error,
            }
            if not any(q["query"] == entry["query"] for q in result.queries):
                result.queries.append(entry)
        return result.model_copy(
            update={
                "status": "matched",
                "method": "review",
                "selected": selected,
                "reason": "Correspondence selected by review " + d.id + ": " + d.reason,
            }
        )

    def value(self, paper, path, quantity, value, correction=None):
        reviewed = self.paper_reviews.get(paper)
        exception = (
            next((e for e in reviewed.exceptions if e.path == path), None)
            if reviewed
            else None
        )
        derived = standard_value(quantity, value)
        return {
            **value,
            "source": paper,
            **({"correction": marker(correction)} if correction else {}),
            "attribution": exception.attribution if exception else "own",
            **({"attribution_reason": exception.reason} if exception else {}),
            **({"standard": derived} if derived else {}),
        }

    def items(self, paper, star_index):
        """Keep exception paths tied to original indices after any withdrawal."""
        science = self.papers[paper].science
        for kind, quantity, path, item, d in corrected_items(
            paper, science, star_index, self.corrections
        ):
            if item is None:
                continue
            if kind == "identifier":
                yield (
                    kind,
                    None,
                    {
                        **item,
                        "source": paper,
                        **({"correction": marker(d)} if d else {}),
                    },
                )
            else:
                yield kind, quantity, self.value(paper, path, quantity, item, d)

    def decisions_for(self, members):
        return [
            d
            for _, d in sorted(self.applied.items())
            if set(members)
            & (
                {correction_node(d)}
                if d.type == "correct"
                else {n for g in d.groups for n in g}
            )
        ]

    def report(self, groups, stars):
        roots = {
            n: s.catalog_id
            for s in stars
            for key, source in s.sources.items()
            for n in (f"{key}#/stars/{o.result_index}" for o in source.objects)
        }
        members = {
            s.catalog_id: sorted(n for n, root in roots.items() if root == s.catalog_id)
            for s in stars
        }
        pairs = defaultdict(list)
        labels = defaultdict(set)
        for node in sorted(groups.nodes):
            for label in names(groups.nodes[node].star):
                if normalized_name(label):
                    labels[normalized_name(label)].add(roots[node])
        for label, objects in sorted(labels.items()):
            for a, b in combinations(sorted(objects), 2):
                pairs[a, b].append({"kind": "same_name", "name": label})
        for edge in groups.edges:
            if edge.left not in roots or edge.right not in roots:
                continue
            a, b = sorted((roots[edge.left], roots[edge.right]))
            if a != b and not edge.accepted:
                pairs[a, b].append(
                    {
                        "kind": "rejected_association",
                        "method": edge.method,
                        "left": edge.left,
                        "right": edge.right,
                        "details": edge.details,
                    }
                )
        display = defaultdict(list)
        for star in stars:
            display[normalized_name(star.display_name)].append(star.catalog_id)
        for label, objects in display.items():
            for a, b in combinations(sorted(objects), 2):
                pairs[a, b].append({"kind": "duplicate_display_name", "name": label})
        counterparts = defaultdict(list)
        for star in stars:
            if star.simbad.status == "matched":
                counterparts[star.simbad.selected.main_id].append(star.catalog_id)
        for main_id, objects in sorted(counterparts.items()):
            for a, b in combinations(sorted(objects), 2):
                pairs[a, b].append({"kind": "same_simbad_object", "main_id": main_id})
        items = []
        separations = self.separation_pairs()
        for (a, b), reasons in sorted(pairs.items()):
            covered = {
                d.id: d
                for x, y in product(members[a], members[b])
                for d in separations.get(tuple(sorted((x, y))), [])
            }
            status = (
                "pending"
                if not covered
                else (
                    "unresolved"
                    if any(d.unresolved for d in covered.values())
                    else "resolved"
                )
            )
            target = [members[a], members[b]]
            items.append(
                {
                    "id": "identity-" + sha256_of(target)[:16],
                    "kind": "identity",
                    "groups": target,
                    "catalog_ids": [a, b],
                    "reasons": reasons,
                    "status": status,
                    "decisions": sorted(covered),
                }
            )
        for star in stars:
            snapshot = star.simbad
            if snapshot.status == "matched":
                selected = snapshot.selected.main_id
                coordinate_matches = [
                    c
                    for c in snapshot.candidates
                    if c.get("method") == "coordinates" and c["main_id"] == selected
                ]
                identifier_match = any(
                    c.get("method") == "identifier" and c["main_id"] == selected
                    for c in snapshot.candidates
                )
                distant = [
                    c for c in coordinate_matches if c["separation_arcsec"] > 1.0 + 1e-9
                ]
                if distant and not identifier_match:
                    recorded = [d for d in star.review_decisions if d.type == "simbad"]
                    items.append(
                        {
                            "id": "simbad-distance-"
                            + sha256_of(members[star.catalog_id])[:16],
                            "kind": "simbad",
                            "groups": [members[star.catalog_id]],
                            "catalog_ids": [star.catalog_id],
                            "reason": "Coordinate-only SIMBAD match exceeds 1 arcsec.",
                            "main_id": selected,
                            "separations_arcsec": sorted(
                                {c["separation_arcsec"] for c in distant}
                            ),
                            "status": "resolved" if recorded else "pending",
                            "decisions": [d.id for d in recorded],
                        }
                    )
            if star.simbad.status in {"conflict", "ambiguous"}:
                recorded = [
                    d
                    for d in star.review_decisions
                    if d.type == "simbad" and d.unresolved
                ]
                items.append(
                    {
                        "id": "simbad-" + sha256_of(members[star.catalog_id])[:16],
                        "kind": "simbad",
                        "groups": [members[star.catalog_id]],
                        "reason": star.simbad.reason,
                        "status": "unresolved" if recorded else "pending",
                        "decisions": [d.id for d in recorded],
                    }
                )
        for key, paper in self.papers.items():
            if paper.science.stars and key not in self.paper_reviews:
                items.append(
                    {
                        "id": key,
                        "kind": "unreviewed_paper",
                        "science_sha256": paper.source.result_sha256,
                        "status": "pending",
                    }
                )
        items.extend(self.invalid)
        return {
            "schema_version": 2,
            "input_fingerprint": self.fingerprint,
            "items": items,
            "counts": {
                status: sum(i["status"] == status for i in items)
                for status in ("pending", "unresolved", "resolved")
            },
        }
