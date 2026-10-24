"""Generate catalog field documentation from the shared record definitions."""

import json


def outputs():
    from ..records.catalog import (
        Association,
        AssociationOrigin,
        CatalogContributionType,
        CatalogIdentifier,
        CatalogQuantity,
        CatalogSelection,
        CatalogValue,
        FinalLiteratureResult,
        FinalResultSource,
        SimbadCandidate,
        SimbadQuery,
        SimbadResponse,
        SimbadSnapshot,
    )
    from ..records.catalog_public import (
        CatalogCounts,
        CatalogRelease,
        IdentifierAssociation,
        IdentityLink,
        PaperEntry,
        PaperIndex,
        PublishedSimbad,
        SimbadTrace,
        StarEntry,
        StarIndex,
        StarSource,
        StellarProvenance,
        StellarRecord,
    )
    from ..records.catalog_simbad import (
        SimbadCoordinateMatch,
        SimbadCoordinates,
        SimbadEllipse,
        SimbadMatchSummary,
        SimbadMeasurement,
        SimbadPhotometry,
        SimbadProperMotion,
        SimbadScience,
        SimbadSummary,
        SimbadValue,
        SimbadVelocity,
    )
    from ..records.generate import _model_table, _schema_json

    models = {
        "final_literature_result": FinalLiteratureResult,
        "catalog_selection": CatalogSelection,
        "catalog_manifest": CatalogRelease,
        "catalog_star": StellarRecord,
        "catalog_paper_index": PaperIndex,
        "catalog_star_index": StarIndex,
        "catalog_simbad_trace": SimbadTrace,
        "catalog_provenance": StellarProvenance,
        "simbad_query": SimbadQuery,
        "simbad_response": SimbadResponse,
    }
    result = {
        "schemas/" + name + ".json": _schema_json(model)
        for name, model in models.items()
    }
    lines = [
        "<!-- Generated from hvs_catalog.records.catalog and catalogue.contracts. -->",
        "# Fixed JSON catalog",
        "",
        "The build command is offline. It reads exact extraction generations and optional frozen",
        "SIMBAD responses. It never refreshes the service implicitly or modifies source workspaces.",
        "Select the complete intended paper versions explicitly. Every build starts from these inputs;",
        "no previous catalog, numbering, grouping or SIMBAD decision is an input.",
        "",
        "## Commands",
        "",
        "- `hvs-catalog catalog check-inputs --results LITERATURE_ROOT` verifies final results",
        "  and their historical provenance without associating or exporting stellar objects.",
        "- `hvs-catalog catalog select --results LITERATURE_ROOT --data-version VERSION --output SELECTION`",
        "  freezes the current runs/<bare-arxiv-id>/results files for this build.",
        "  This immutable build selection is not a separately maintained literature index.",
        "- `hvs-catalog catalog select --index INDEX --space SPACE --data-version VERSION --output SELECTION`",
        "  retains legacy execution-index compatibility without guessing latest results.",
        "- `hvs-catalog catalog build --selection SELECTION --output NEW_DIRECTORY [--simbad RESPONSES]`",
        "  publishes a new complete snapshot; an existing destination is never overwritten.",
        "- `hvs-catalog catalog verify CATALOG` validates hashes, references, source coverage,",
        "  and exact scientific values and evidence, without authenticating scientific correctness.",
        "- `hvs-catalog catalog query-plan --selection SELECTION --output PLAN`",
        "  prepares an explicit query list for review. The list includes identifier queries",
        "  and possible coordinate fallbacks; remove queries outside the approved scope.",
        "- `hvs-catalog catalog query --plan PLAN --output RESPONSES` explicitly contacts SIMBAD",
        "  for only that list. Existing responses are reused, including failures.",
        "  A refreshed response directory requires approval of the query scope. Rebuilding",
        "  uses only the supplied frozen responses; missing responses remain not_queried.",
        "",
        "## Final literature inputs",
        "",
        "Each final results directory contains exactly literature_contribution.json,",
        "evidence_audit.json and run.json. Old-style arXiv slashes become underscores in",
        "directory names only; the original ID and version remain in scientific metadata.",
        "Final run and audit references are relative to the delivery root. source.root is",
        "also relative to that root; source.run_ref is relative to the historical workspace.",
        "The final wrapper is not an execution and the delivery root needs no business database.",
        "Only explicitly referenced historical runs are read. Historical scripts are never run.",
        "Science bytes, paper/version, completion and audit observations must match the verified",
        "source; only the audit result path is rebound. The private build selection binds the",
        "final wrapper, historical run and delivered file hashes. Execution records are not published.",
        "Cumulative repairs retain validated prior stages plus the selected later replacements.",
        "Local container recoveries replay their saved receipts against exact archived responses,",
        "preserve every scalar and accepted group, and pass the original scientific validators.",
        "This read-only compatibility path does not extend the live response parser.",
        "Missing files, changed hashes, escaping/symlink paths and inconsistent lineage are refused.",
        "",
        "## Readable publication format (schema version 4)",
        "",
        "manifest.json contains the data version, counts, build fingerprints and two hashed index references.",
        "papers/index.json binds each versioned paper to its science, audit and PDF provenance.",
        "Paper titles, authors and notes already exist in the science file and are not repeated in this index.",
        "The optional incomplete field records only unfinished stages and retained scientific diagnostics.",
        "An absent incomplete field means that the retained extraction is complete, not scientifically verified.",
        "stars/index.json lists catalog IDs, display names and hashed files; fresh numbering starts at one.",
        "Each stellar sources entry contains title, authors, pdf_url and original /stars/N record pointers.",
        "The versioned source key identifies the exact paper; pointers address that paper's science file.",
        "Paper-wide notes remain in papers. Execution errors, snapshots and repair-stage IDs stay in the workspace.",
        "provenance/<catalog_id>_provenance.json delivers all associations, historical origins and the complete SIMBAD trace.",
        "Each top-level stellar provenance reference binds that file by path and SHA-256; paths are catalog-root relative.",
        "The complete catalog is portable and verifiable without the original build workspace or network.",
        "The reader accepts versions 1, 2, 3 and 4 for verification.",
        "Earlier version-four provenance files without the _provenance suffix remain readable.",
        "",
        "## Scientific and identity rules",
        "",
        "Each stellar JSON embeds all retained scientific values, evidence, and paper metadata.",
        "Every identifier, contribution type, and value has a source key: bare arXiv ID plus version.",
        "There is no contributions container and no duplicate complete-paper payload.",
        "Numerical strings, units, conditions, uncertainty forms, and source-specific duplicates",
        "are preserved. Paper-level notes retain their paper-wide scope in the original paper file.",
        "",
        "Files use unique hvs-catalog-NNNNNN IDs. Display names are readable, nonunique labels.",
        "Use the selected SIMBAD main_id for either identifier or coordinate matches.",
        "Otherwise use the shortest paper identifier containing a letter, after collapsing whitespace.",
        "Equal lengths prefer more distinct papers, then lexical order; no catalog-prefix whitelist applies.",
        "Generic local number labels (Star, Object, Obj, Source, ID, No, Row, Candidate or Target)",
        "append the bare arXiv ID, for example ID 7 (2601.00001). If several papers use the label,",
        "choose the first bare arXiv ID in lexical order. Compare label lengths before appending it.",
        "If no lettered identifier exists, use catalog_id. All original identifiers remain unchanged.",
        "Unresolved SIMBAD candidates are not selected for naming; paper labels remain available.",
        "Duplicate names are allowed. Neither names nor coordinate matches become identifier merge evidence.",
        "Coordinate-match warnings remain. Names are recalculated from current inputs without inheritance.",
        "",
        "Coordinates require a unique RA/Dec pair from one source object, linked by shared",
        "evidence locations or identical nonempty conditions. Descriptions need not be identical.",
        "Repeated identical coordinates share a query; different candidate values remain unused.",
        "Common hms/dms labels, sexagesimal typography and separate central/error-unit labels",
        "are parsed for queries only. All original scientific fields remain unchanged.",
        "Frames come from coordinate conditions, then coordinate quotes, then explicit Gaia",
        "coordinate provenance. Unlabelled modern equatorial coordinates may assume ICRS.",
        "Explicit incompatible or unsupported frames are never overridden by that assumption.",
        "Known frames may be transformed; coordinates are never propagated between epochs.",
        "SIMBAD queries and association coordinate_interpretation(s) record",
        "source nodes, original coordinate evidence, frame basis and whether ICRS was assumed.",
        "An assumed frame also adds an English warning, including when no candidate is found.",
        "Queries omit source provenance to retain reusable frozen response keys. Rules version 3",
        "adds automatic source-evidence conflict checks and fresh construction after version 2's coordinate policy.",
        "Rules version 4 changes display naming only; scientific identity and matching rules are unchanged.",
        "The association radius is 5 arcseconds. Every union checks the entire group's Gaia",
        "release identities, including all frozen representations of selected SIMBAD objects.",
        "SIMBAD merge edges retain the candidate and query evidence used by this check.",
        "Coordinate-only associations retain warnings.",
        "SIMBAD coordinate searches select the nearest candidate, warn for multiple candidates,",
        "and retain ambiguity for tied distances. Reliable conflicting identities block selection.",
        "",
        "SIMBAD values remain separate from literature quantities. selected groups returned science into",
        "coordinates, proper_motion, parallax, velocity, spectral_type and photometry, with units, errors,",
        "quality and bibliographic references beside each quantity. Missing data are omitted, not derived.",
        "No numerical value is rounded or converted. Coordinate and proper-motion error ellipses retain",
        "major, minor and angle with their returned units. Proper motion ra_cosdec includes cos(dec).",
        "velocity retains radial_velocity, redshift, reported_as and the original error with its own unit;",
        "the two velocity representations are not independent measurements. Photometry keeps its band,",
        "original system code (V=Vega, A=AB), value, error, quality and bibcode. A missing returned unit",
        "remains absent; the column name flux does not imply a physical flux unit.",
        "Nonempty unfamiliar fields remain in additional_fields. Empty strings, duplicate identity/position",
        "columns, precision codes and database metadata are kept only in the original trace snapshot.",
        "The stellar match summary lists successful identifiers or adopted coordinate source records,",
        "distances, radii, multiple-candidate counts and assumed frames. Unresolved identities list candidate",
        "names without selecting their science. last_queried_at is the latest primary query timestamp,",
        "not a promise that every selected field was refreshed then. reason is omitted when absent.",
        "Service errors are not no-match results. Transport XML remains frozen in the build workspace;",
        "the delivered trace preserves all parsed candidate fields, individual queries and their response hashes.",
        "Conflicts retain rejected candidates and query bindings in that delivered trace.",
        "Incompatible external identities remain conflicts instead of selecting one input.",
        "",
        "The stellar file keeps final source membership and warnings; all association details live in provenance.",
        "The provenance associations list retains accepted and rejected links, including their exact evidence.",
        "A shared identifier is recorded once with its source nodes, instead of all pairwise edges.",
        "Coordinate and paper correspondences retain their actual endpoints and evidence.",
        "Self-links, previous-build anchor links and duplicate associations are omitted.",
        "Current endpoints resolve through sources and the fixed paper files. Superseded origins are",
        "stored once in association_origins. Historical scientific associations retain their original versions;",
        "their provenance never adds superseded measurements to current scientific values.",
        "SIMBAD coordinate bindings live in the trace's simbad.bindings and retain the exact source node,",
        "coordinate evidence, query/response binding, candidate distances, radius and nearest selection.",
        "Within each trace, selected and distinct alternative frozen representations are stored once;",
        "matches and association evidence use local {$ref: '#/simbad/...'} references. related_queries and",
        "related_matches retain other or superseded snapshots used only by identity evidence.",
        "SIMBAD identity links share the same associations list as identifier, coordinate and paper links.",
        "Required historical origins are embedded once in association_origins. All local evidence references",
        "resolve inside the unified provenance file. There is no separate SIMBAD-only trace in version 4.",
        "Verification checks both hashes and the exact science/summary projection against the trace,",
        "then restores the full evidence for identity and source-coverage checks.",
        "References never replace literature measurements or their evidence. No numeric strings are rewritten.",
        "",
        "warnings is the final field and contains deduplicated English notices about the current star.",
        "Coordinate-only matching, assumed ICRS, multiple candidates, unresolved identity and failed",
        "enrichment remain visible. Incomplete object extraction names the exact source record.",
        "Quote-audit warnings name the source field, location and reason; whitespace-only differences",
        "remain in the original audit without a stellar warning. A warning does not prove a scientific error.",
        "",
        "Groups and conflict decisions precede numbering. IDs are assigned deterministically",
        "from the complete selected source records, without old-ID protection or identity revisions.",
        "The builder detects nearby same-paper records whose identifier evidence quotes contain",
        "one explicitly row-bound sexagesimal coordinate pair each, with positions more than five",
        "arcseconds apart and a source position agreeing with one row but contradicting the other.",
        "Incomplete, ambiguous, unbound or image-only quotes do not establish this contradiction.",
        "The conflicting pair remains separate under direct and transitive associations, including SIMBAD.",
        "Original values and quotes are retained. A contradiction is not a corrected position or a PDF verification.",
        "",
        "Complete fictional record: [catalog_star.json](examples/catalog_star.json).",
        "The example uses placeholder hashes and fabricated papers; it is not catalog data.",
        "",
        "## Fields",
        "",
    ]
    for model in (
        FinalLiteratureResult,
        FinalResultSource,
        CatalogSelection,
        CatalogRelease,
        CatalogCounts,
        PaperIndex,
        PaperEntry,
        StarIndex,
        StarEntry,
        StellarRecord,
        StarSource,
        IdentifierAssociation,
        IdentityLink,
        SimbadSummary,
        SimbadScience,
        SimbadValue,
        SimbadMeasurement,
        SimbadCoordinates,
        SimbadProperMotion,
        SimbadEllipse,
        SimbadVelocity,
        SimbadPhotometry,
        SimbadMatchSummary,
        SimbadCoordinateMatch,
        StellarProvenance,
        PublishedSimbad,
        CatalogIdentifier,
        CatalogContributionType,
        CatalogQuantity,
        CatalogValue,
        Association,
        AssociationOrigin,
        SimbadSnapshot,
        SimbadQuery,
        SimbadCandidate,
        SimbadResponse,
    ):
        lines.extend(_model_table(model))
    result["catalog-format.md"] = "\n".join(lines)
    from .provenance_publication import split_document

    example, trace_path, trace_bytes = split_document(star_example())
    result["examples/catalog_star.json"] = (
        json.dumps(example, ensure_ascii=False, indent=2) + "\n"
    )
    result["examples/" + trace_path] = trace_bytes.decode()
    return result


def star_example():
    """Return a validated fictional record with two independent paper measurements."""
    from ..records.catalog import AssociationOrigin, CatalogStar, SimbadSnapshot
    from ..records.literature_contribution import LiteratureMetadata

    sources, identifiers, types, values = {}, [], [], []
    for number, velocity in ((1, "520"), (2, "530")):
        arxiv_id = f"2601.{number:05d}"
        key = arxiv_id + "v1"
        evidence = [{"location": "T:p1-b0", "quote": "Fictional source evidence."}]
        sources[key] = {
            "arxiv_id": arxiv_id,
            "version": "v1",
            "title": f"Fictional study {number}",
            "authors": ["A. Example"],
            "paper_notes": ["Fictional paper-wide note."],
            "result_status": "complete",
            "roster_status": "complete",
            "roster_issues": [],
            "roster_rejected": {},
            "errors": [],
            "result_sha256": "0" * 64,
            "run_sha256": "0" * 64,
            "input_sha256": "0" * 64,
            "pdf_sha256": "0" * 64,
            "pdf_url": f"https://arxiv.org/pdf/{key}",
            "pdf_origin": {"kind": "synthetic"},
            "objects": [
                {
                    "result_index": 0,
                    "source_index": 0,
                    "status": "complete",
                    "snapshot_ref": {"record_id": "synthetic-star", "revision": 1},
                    "issues": [],
                    "rejected": {},
                    "source_paths": ["/stars/0"],
                    "evidence_audit": [],
                }
            ],
        }
        identifiers.append(
            {
                "value": "Gaia DR3 1234567890123456789",
                "source": key,
                "evidence": evidence,
            }
        )
        types.append(
            {
                "value": "follow_up",
                "source": key,
                "evidence": evidence,
            }
        )
        values.append(
            {
                "value": velocity,
                "error": "10",
                "unit": "km/s",
                "condition": "Heliocentric",
                "source": key,
                "evidence": evidence,
            }
        )
    record = CatalogStar.model_validate(
        {
            "data_version": "fictional-example",
            "catalog_id": "hvs-catalog-000001",
            "display_name": "Gaia DR3 1234567890123456789",
            "sources": sources,
            "identifiers": identifiers,
            "contribution_types": types,
            "quantities": [{"quantity": "radial_velocity", "values": values}],
            "simbad": {
                "status": "not_queried",
                "reason": "Fictional example; no external service was contacted.",
            },
            "associations": [
                {
                    "left": "2601.00001v1#/stars/0",
                    "right": "2601.00002v1#/stars/0",
                    "method": "identifier",
                    "accepted": True,
                    "details": {"identifier": "Gaia DR3 1234567890123456789"},
                }
            ],
            "warnings": [],
        }
    )

    selected = {
        "main_id": "HD 999999",
        "aliases": ["Gaia DR3 1234567890123456789"],
        "position": {"ra_deg": 10.0, "dec_deg": 20.0, "frame": "icrs"},
        "fields": {
            "plx_value": {"value": 0.125, "unit": "mas"},
            "plx_err": {"value": 0.0625, "unit": "mas"},
            "plx_qual": {"value": "A"},
            "plx_bibcode": {"value": "FictionalReference"},
            "plx_prec": {"value": 4},
            "sp_type": {"value": "B2V"},
        },
        "photometry": [],
    }
    record.simbad = SimbadSnapshot.model_validate(
        {
            "status": "matched",
            "method": "identifier",
            "selected": selected,
            "queries": [
                {
                    "query": {
                        "method": "identifier",
                        "identifier": "Gaia DR3 1234567890123456789",
                        "position": None,
                        "radius_arcsec": 5.0,
                    },
                    "response_sha256": "0" * 64,
                    "queried_at": "2026-01-01T00:00:00+00:00",
                    "client_version": "fictional",
                    "service_version": None,
                    "error": None,
                }
            ],
            "candidates": [
                {
                    "main_id": "HD 999999",
                    "method": "identifier",
                    "identifier": "Gaia DR3 1234567890123456789",
                    "candidate": selected,
                }
            ],
        }
    )
    for edge in record.associations:
        for node in (edge.left, edge.right):
            key, index = node.split("#/stars/")
            source = record.sources[key]
            edge.origins[node] = AssociationOrigin(
                source=LiteratureMetadata.model_validate(
                    {k: getattr(source, k) for k in LiteratureMetadata.model_fields}
                ),
                result_sha256=source.result_sha256,
                pdf_sha256=source.pdf_sha256,
                pdf_url=source.pdf_url,
                pdf_origin=source.pdf_origin,
                result_index=int(index),
                identifiers=[
                    i.model_dump(exclude={"source"})
                    for i in record.identifiers
                    if i.source == key
                ],
            )
    from types import SimpleNamespace

    from .readable import stellar_document

    return stellar_document(
        record,
        {key: SimpleNamespace(source=source) for key, source in record.sources.items()},
    )
