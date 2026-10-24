"""Project construction state into readable publications and load either generation."""

import copy
import json
from itertools import pairwise

from ..records.catalog import CatalogManifest, CatalogSource, CatalogStar
from ..records.catalog_public import (
    CatalogRelease,
    LegacyStellarRecord,
    PaperIndex,
    StarIndex,
)
from ..records.common import sha256_of
from ..records.evidence_audit import EvidenceAudit
from ..records.literature_contribution import LiteratureContribution
from .inputs import json_bytes, read_bound
from .provenance import paper_origin


def _ref(path):
    return {"$ref": path}


def _pointer_key(key):
    return key.replace("~", "~0").replace("/", "~1")


def _plain_node(node):
    return node.split(":", 1)[1] if node.startswith("@") else node


def _optional(value):
    """Omit absent administrative fields, never scientific value or evidence fields."""
    return {k: v for k, v in value.items() if v is not None and v != [] and v != {}}


def _incomplete(source):
    result = {}
    for field in ("result_status", "roster_status"):
        if getattr(source, field) != "complete":
            result[field] = getattr(source, field)
    for field in ("roster_issues", "roster_rejected"):
        if getattr(source, field):
            result[field] = getattr(source, field)
    records = {}
    for obj in source.objects:
        entry = _optional({"issues": obj.issues, "rejected": obj.rejected})
        if obj.status != "complete":
            entry["status"] = obj.status
        if entry:
            records[f"/stars/{obj.result_index}"] = entry
    if records:
        result["records"] = records
    return result


def _warnings(star):
    result = []
    coordinate_warning = "Association based on coordinates only; no epoch propagation."
    for warning in star.warnings:
        if warning in {
            "Some evidence quotes have audit warnings; inspect sources.",
            "Some source extraction results are incomplete; inspect sources.",
        }:
            continue
        if "coordinates only" in warning:
            warning = coordinate_warning
        result.append(warning)
    for key, source in star.sources.items():
        for obj in source.objects:
            record = f"{key}#/stars/{obj.result_index}"
            if obj.status != "complete":
                result.append(
                    f"{record}: extraction is {obj.status}; missing values are not evidence of absence in the paper."
                )
            if obj.issues or obj.rejected:
                result.append(
                    f"{record}: retained extraction diagnostics are recorded in papers/index.json."
                )
            for audit in obj.evidence_audit:
                if (
                    audit.reason == "whitespace_difference"
                    or not audit.result_path.startswith(f"/stars/{obj.result_index}/")
                ):
                    continue
                result.append(
                    f"{key}#{audit.result_path}: quote audit {audit.reason} at {audit.location}; check the paper evidence."
                )
    return list(dict.fromkeys(result))


class _Evidence:
    """Store each external candidate and query once within the same stellar file."""

    def __init__(self, snapshot):
        self.selected = (
            snapshot.selected.model_dump(mode="json") if snapshot.selected else None
        )
        self.alternatives, self.queries, self.matches = [], [], []
        self.related_queries, self.related_matches = [], []
        self.primary = True

    def candidate(self, value):
        if value == self.selected:
            return _ref("#/simbad/selected")
        if value not in self.alternatives:
            self.alternatives.append(value)
        return _ref(f"#/simbad/alternatives/{self.alternatives.index(value)}")

    def query(self, value):
        if value in self.queries:
            return _ref(f"#/simbad/queries/{self.queries.index(value)}")
        field = "queries" if self.primary else "related_queries"
        values = getattr(self, field)
        if value not in values:
            values.append(copy.deepcopy(value))
        return _ref(f"#/simbad/{field}/{values.index(value)}")

    def match(self, value):
        value = copy.deepcopy(value)
        for field in ("candidate", "rejected_candidate"):
            if field in value:
                value[field] = self.candidate(value[field])
        if value in self.matches:
            return _ref(f"#/simbad/matches/{self.matches.index(value)}")
        field = "matches" if self.primary else "related_matches"
        values = getattr(self, field)
        if value not in values:
            values.append(value)
        return _ref(f"#/simbad/{field}/{values.index(value)}")

    def details(self, value):
        value = copy.deepcopy(value)
        value.pop("manifest_sha256", None)
        if "query" in value:
            value["query"] = self.query(value["query"])
        if "candidates" in value:
            value["candidates"] = [self.match(c) for c in value["candidates"]]
        for item in value.get("simbad_evidence", []):
            item["node"] = _plain_node(item["node"])
            item["candidate"] = self.candidate(item["candidate"])
            item["queries"] = [self.query(q) for q in item["queries"]]
        return value


def stellar_document(star, papers):
    """Remove process history while retaining exact science and identity evidence."""
    raw = star.model_dump(mode="json")
    raw["schema_version"] = 2
    raw["sources"] = {
        key: {
            "title": source.title,
            "authors": source.authors,
            "pdf_url": source.pdf_url,
            "records": [f"/stars/{o.result_index}" for o in source.objects],
        }
        for key, source in star.sources.items()
    }
    evidence = _Evidence(star.simbad)
    for query in star.simbad.queries:
        evidence.query(query)
    for match in star.simbad.candidates:
        evidence.match(match)
    evidence.primary = False
    origins, links, identifiers, bindings = {}, {}, {}, {}
    for edge in star.associations:
        left, right = _plain_node(edge.left), _plain_node(edge.right)
        if edge.method == "previous" or left == right:
            continue
        missing = {}
        for node, origin in edge.origins.items():
            node = _plain_node(node)
            key = origin.source.arxiv_id + origin.source.version
            current = papers.get(key)
            if current and origin.result_sha256 == current.source.result_sha256:
                continue
            name = node + "@" + origin.result_sha256
            origins[name] = origin.model_dump(mode="json")
            missing[node] = _ref("#/association_origins/" + _pointer_key(name))
        historical = edge.historical or bool(missing)
        if edge.method == "identifier" and edge.accepted and not historical:
            identifier = edge.details["identifier"]
            identifiers.setdefault(identifier, set()).update((left, right))
            continue
        link = {
            "method": edge.method,
            "left": left,
            "right": right,
            "details": evidence.details(edge.details),
        }
        if not edge.accepted:
            link["accepted"] = False
        if historical:
            link["historical"] = True
        if missing:
            link["origins"] = missing
        target = bindings if right.startswith("simbad:") else links
        identity = sha256_of({k: v for k, v in link.items() if k != "historical"})
        if identity not in target or not historical:
            target[identity] = link
    raw["associations"] = [
        {"method": "identifier", "identifier": identifier, "nodes": sorted(nodes)}
        for identifier, nodes in sorted(identifiers.items())
    ] + list(links.values())
    simbad = _optional(
        {
            "status": star.simbad.status,
            "method": star.simbad.method,
            "selected": evidence.selected,
            "reason": star.simbad.reason,
            "queries": evidence.queries,
            "related_queries": evidence.related_queries,
            "matches": evidence.matches,
            "related_matches": evidence.related_matches,
            "alternatives": evidence.alternatives,
            "bindings": list(bindings.values()),
        }
    )
    raw["simbad"] = simbad
    raw.pop("warnings")
    if origins:
        raw["association_origins"] = origins
    raw["warnings"] = _warnings(star)
    LegacyStellarRecord.model_validate(raw)
    return raw


def export_files(manifest, stars, papers, files, *, schema_version=4):
    from .publication import artifact

    if schema_version == 3:
        from .simbad_publication import split_document
    elif schema_version == 4:
        from .provenance_publication import split_document
    elif schema_version != 2:
        raise ValueError("Unsupported readable catalog schema version.")

    paper_entries = {}
    for key, entry in manifest.papers.items():
        source = entry.source
        paper_entries[key] = _optional(
            {
                "science": entry.science.model_dump(),
                "audit": entry.audit.model_dump(),
                "original_audit_sha256": entry.original_audit_sha256,
                "pdf_url": source.pdf_url,
                "pdf_sha256": source.pdf_sha256,
                "pdf_origin": source.pdf_origin,
                "incomplete": _incomplete(source),
            }
        )
    index = {
        "schema_version": schema_version,
        "next_id": manifest.next_id,
        "retired_ids": manifest.retired_ids,
        "stars": [],
    }
    for star in sorted(stars, key=lambda s: s.catalog_id):
        path = "stars/" + star.catalog_id + ".json"
        document = stellar_document(star, papers)
        if schema_version in {3, 4}:
            document, trace_path, trace_bytes = split_document(document)
            files[trace_path] = trace_bytes
        files[path] = json_bytes(document)
        index["stars"].append(
            {
                "catalog_id": star.catalog_id,
                "display_name": star.display_name,
                "file": artifact(path, files[path]),
            }
        )
    paper_index = {"schema_version": schema_version, "papers": paper_entries}
    PaperIndex.model_validate(paper_index)
    StarIndex.model_validate(index)
    files["papers/index.json"] = json_bytes(paper_index)
    files["stars/index.json"] = json_bytes(index)
    release = {
        k: getattr(manifest, k)
        for k in (
            "data_version",
            "rules_version",
            "software_version",
            "input_fingerprint",
            "simbad_fingerprint",
            "previous_manifest_sha256",
        )
    }
    release.update(
        {
            "schema_version": schema_version,
            "counts": {
                "papers": len(papers),
                "stars": len(stars),
                "source_records": sum(len(p.science.stars) for p in papers.values()),
                "values": sum(len(q.values) for s in stars for q in s.quantities),
            },
            "papers": artifact("papers/index.json", files["papers/index.json"]),
            "stars": artifact("stars/index.json", files["stars/index.json"]),
        }
    )
    CatalogRelease.model_validate(release)
    files["manifest.json"] = json_bytes(release)


def _expand(value, document, stack=()):
    if isinstance(value, list):
        return [_expand(v, document, stack) for v in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"$ref"}:
        ref = value["$ref"]
        if (
            not isinstance(ref, str)
            or not ref.startswith(("#/simbad/", "#/association_origins/"))
            or ref in stack
        ):
            raise ValueError("Invalid or cyclic catalog evidence reference.")
        target = document
        try:
            for key in ref[2:].split("/"):
                key = key.replace("~1", "/").replace("~0", "~")
                target = target[int(key)] if isinstance(target, list) else target[key]
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise ValueError("Missing catalog evidence reference: " + ref) from exc
        return _expand(target, document, (*stack, ref))
    return {k: _expand(v, document, stack) for k, v in value.items()}


def _source(entry, science, audit):
    incomplete = copy.deepcopy(entry.incomplete)
    records = incomplete.pop("records", {})
    if set(incomplete) - {
        "result_status",
        "roster_status",
        "roster_issues",
        "roster_rejected",
    }:
        raise ValueError("Unknown paper incompleteness field.")
    if set(records) - {f"/stars/{i}" for i in range(len(science.stars))}:
        raise ValueError("Incomplete record is outside the paper.")
    objects = []
    for i in range(len(science.stars)):
        record = records.get(f"/stars/{i}", {})
        if set(record) - {"status", "issues", "rejected"}:
            raise ValueError("Unknown record diagnostic field.")
        objects.append(
            {
                "result_index": i,
                "source_index": i,
                "status": "complete",
                "snapshot_ref": None,
                "issues": [],
                "rejected": {},
                "source_paths": [],
                "evidence_audit": [
                    a
                    for a in audit.quote_mismatches
                    if a.result_path.startswith(f"/stars/{i}/")
                ],
                **record,
            }
        )
    return CatalogSource(
        **science.metadata.model_dump(),
        paper_notes=science.notes,
        result_status=incomplete.get("result_status", "complete"),
        roster_status=incomplete.get("roster_status", "complete"),
        roster_issues=incomplete.get("roster_issues", []),
        roster_rejected=incomplete.get("roster_rejected", {}),
        errors=[],
        result_sha256=entry.science.sha256,
        run_sha256=None,
        input_sha256=None,
        pdf_sha256=entry.pdf_sha256,
        pdf_url=entry.pdf_url,
        pdf_origin=entry.pdf_origin,
        objects=objects,
    )


def _star(raw, papers):
    public = LegacyStellarRecord.model_validate(raw)
    result = {
        k: raw[k]
        for k in (
            "schema_version",
            "data_version",
            "catalog_id",
            "display_name",
            "identifiers",
            "contribution_types",
            "quantities",
            "warnings",
        )
    }
    sources = {}
    for key, source in public.sources.items():
        if key not in papers:
            raise ValueError("Star names a paper outside the manifest.")
        paper = papers[key]
        if (source.title, source.authors, source.pdf_url) != (
            paper.source.title,
            paper.source.authors,
            paper.source.pdf_url,
        ):
            raise ValueError("Embedded source metadata changed.")
        indices = [int(r.removeprefix("/stars/")) for r in source.records]
        if any(i >= len(paper.science.stars) for i in indices):
            raise ValueError("Source record is outside the paper.")
        sources[key] = paper.source.model_copy(
            deep=True, update={"objects": [paper.source.objects[i] for i in indices]}
        )
    result["sources"] = sources
    simbad = public.simbad
    result["simbad"] = {
        "status": simbad.status,
        "method": simbad.method,
        "selected": simbad.selected,
        "reason": simbad.reason,
        "queries": _expand(simbad.queries, raw),
        "candidates": _expand(simbad.matches, raw),
        "warnings": [
            w
            for w in raw["warnings"]
            if w.startswith(
                (
                    "Coordinate reference frame",
                    "Multiple SIMBAD",
                    "Some identifier queries",
                )
            )
        ],
    }
    edges = []
    for item in [
        *raw["associations"],
        *raw["simbad"].get("bindings", []),
    ]:
        if "nodes" in item:
            if len(set(item["nodes"])) != len(item["nodes"]):
                raise ValueError("Duplicate identifier association node.")
            links = [
                {
                    "method": "identifier",
                    "left": left,
                    "right": right,
                    "details": {"identifier": item["identifier"]},
                }
                for left, right in pairwise(item["nodes"])
            ]
        else:
            links = [item]
        for link in links:
            link = _expand(link, raw)
            origins = link.setdefault("origins", {})
            for endpoint in (link["left"], link["right"]):
                if endpoint.startswith("simbad:") or endpoint in origins:
                    continue
                try:
                    key, index = endpoint.split("#/stars/")
                    paper = papers[key]
                    origins[endpoint] = paper_origin(
                        paper.source, paper.science, int(index)
                    ).model_dump(mode="json")
                except (KeyError, IndexError, ValueError) as exc:
                    raise ValueError(
                        "Association origin is absent from the catalog."
                    ) from exc
            link.setdefault("accepted", True)
            edges.append(link)
    result["associations"] = edges
    return CatalogStar.model_validate(result)


def read_snapshot(root):
    """Return normalized construction records without restoring removed execution history."""
    from .inputs import LoadedPaper

    release = CatalogRelease.model_validate_json((root / "manifest.json").read_bytes())
    if (
        release.papers.path != "papers/index.json"
        or release.stars.path != "stars/index.json"
    ):
        raise ValueError("Catalog indexes must use the documented paths.")
    paper_index = PaperIndex.model_validate_json(read_bound(root, release.papers))
    star_index = StarIndex.model_validate_json(read_bound(root, release.stars))
    if (
        paper_index.schema_version != release.schema_version
        or star_index.schema_version != release.schema_version
    ):
        raise ValueError("Catalog index schema versions differ from the manifest.")
    papers, paper_entries = {}, {}
    for key, entry in paper_index.papers.items():
        science_raw, audit_raw = (
            read_bound(root, entry.science),
            read_bound(root, entry.audit),
        )
        science = LiteratureContribution.model_validate_json(science_raw)
        audit = EvidenceAudit.model_validate_json(audit_raw)
        source = _source(entry, science, audit)
        papers[key] = LoadedPaper(science, science_raw, audit, audit_raw, source)
        paper_entries[key] = {
            "source": source,
            "science": entry.science,
            "audit": entry.audit,
            "original_audit_sha256": entry.original_audit_sha256,
        }
    stars, star_entries = {}, []
    for entry in star_index.stars:
        raw = json.loads(read_bound(root, entry.file))
        if raw.get("schema_version") != release.schema_version:
            raise ValueError("Star schema version differs from the manifest.")
        if release.schema_version == 3:
            from .simbad_publication import restore_document

            raw = restore_document(raw, root)
        elif release.schema_version == 4:
            from .provenance_publication import restore_document

            raw = restore_document(raw, root)
        star = _star(raw, papers).model_copy(
            update={"schema_version": release.schema_version}
        )
        stars[entry.file.path] = star
        star_entries.append(
            {
                **entry.model_dump(),
                "nodes": [
                    f"{key}#/stars/{o.result_index}"
                    for key, source in star.sources.items()
                    for o in source.objects
                ],
            }
        )
    counts = {
        "papers": len(papers),
        "stars": len(star_entries),
        "source_records": sum(len(p.science.stars) for p in papers.values()),
        "values": sum(len(q.values) for s in stars.values() for q in s.quantities),
    }
    if counts != release.counts.model_dump():
        raise ValueError("Catalog counts differ from the data.")
    manifest = CatalogManifest(
        **{
            k: getattr(release, k)
            for k in (
                "schema_version",
                "data_version",
                "rules_version",
                "software_version",
                "input_fingerprint",
                "simbad_fingerprint",
                "previous_manifest_sha256",
            )
        },
        papers=paper_entries,
        stars=star_entries,
        next_id=star_index.next_id,
        retired_ids=star_index.retired_ids,
    )
    return manifest, stars
