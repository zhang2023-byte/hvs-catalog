"""Read final deliveries without opening them as execution workspaces."""

import json
from pathlib import Path

from ..records.catalog import CatalogSelection, FinalLiteratureResult, PaperSelection
from ..records.common import ArtifactRef
from ..records.evidence_audit import EvidenceAudit
from ..records.literature_contribution import LiteratureContribution
from ..workspace import resolve_within
from .inputs import LoadedPaper, digest, read_bound


def _read_final(root, run_ref):
    run = FinalLiteratureResult.model_validate_json(read_bound(root, run_ref))
    folder = "runs/" + run.arxiv_id.replace("/", "_") + "/results/"
    if (
        run_ref.path != folder + "run.json"
        or run.result_ref.path != folder + "literature_contribution.json"
        or run.evidence_audit_ref.path != folder + "evidence_audit.json"
    ):
        raise ValueError(
            "Final result references must identify the same paper directory."
        )
    directory = resolve_within(root, folder)
    if {p.name for p in directory.iterdir()} != {
        "run.json",
        "literature_contribution.json",
        "evidence_audit.json",
    }:
        raise ValueError(
            "A final results directory must contain exactly three JSON files."
        )
    science_bytes = read_bound(root, run.result_ref)
    audit_bytes = read_bound(root, run.evidence_audit_ref)
    science = LiteratureContribution.model_validate_json(science_bytes)
    audit = EvidenceAudit.model_validate_json(audit_bytes)
    if (
        science.metadata.arxiv_id != run.arxiv_id
        or science.metadata.version != run.arxiv_version
        or audit.metadata.arxiv_id != run.arxiv_id
        or audit.metadata.arxiv_version != run.arxiv_version
        or audit.metadata.result_ref != run.result_ref
    ):
        raise ValueError(
            "Final metadata or evidence audit differs from its selected result."
        )
    return run, science, science_bytes, audit, audit_bytes


def selection_from_results(root, data_version):
    """Freeze only the explicit final results, never historical or latest generations."""
    root = Path(root).resolve()
    runs = resolve_within(root, "runs")
    papers, seen = [], set()
    for directory in sorted(runs.iterdir()):
        run_path = resolve_within(
            root, directory.relative_to(root) / "results/run.json"
        )
        run_ref = ArtifactRef(
            path=str(run_path.relative_to(root)), sha256=digest(run_path.read_bytes())
        )
        run, *_ = _read_final(root, run_ref)
        if run.arxiv_id in seen:
            raise ValueError("Select exactly one final result per paper.")
        seen.add(run.arxiv_id)
        papers.append(
            PaperSelection(
                workspace=str(root),
                run_ref=run_ref,
                result_ref=run.result_ref,
                evidence_audit_ref=run.evidence_audit_ref,
            )
        )
    if not papers:
        raise ValueError("No final literature results were found.")
    return CatalogSelection(data_version=data_version, papers=papers)


def load_final(selection):
    from .inputs import load_paper

    root = Path(selection.workspace).resolve()
    run, science, science_bytes, audit, audit_bytes = _read_final(
        root, selection.run_ref
    )
    if (selection.result_ref and selection.result_ref != run.result_ref) or (
        selection.evidence_audit_ref
        and selection.evidence_audit_ref != run.evidence_audit_ref
    ):
        raise ValueError("Selection differs from the final result references.")
    history = resolve_within(root, run.source.root)
    source_raw = json.loads(read_bound(history, run.source.run_ref))
    if source_raw.get("format") not in {
        "hvs.extraction-run",
        "hvs.network-repair-result",
    }:
        raise ValueError(
            "A final result must reference a retained extraction or repair run."
        )
    original = load_paper(
        PaperSelection(workspace=str(history), run_ref=run.source.run_ref)
    )
    expected_audit = original.audit.model_copy(deep=True)
    expected_audit.metadata.result_ref = run.result_ref
    if (
        science_bytes != original.science_bytes
        or audit != expected_audit
        or run.completion != original.source.result_status
    ):
        raise ValueError("Final delivery differs from its verified historical source.")
    source = original.source.model_copy(deep=True)
    # The selected wrapper binds both the delivered files and the exact historical run.
    source.run_sha256 = selection.run_ref.sha256
    return LoadedPaper(science, science_bytes, audit, audit_bytes, source)


def inspect_results(root):
    """Verify every final input and its provenance without associating or exporting stars."""
    from .inputs import load_paper

    selection = selection_from_results(root, "input-check")
    counts = {
        "papers": 0,
        "empty_papers": 0,
        "objects": 0,
        "values": 0,
        "audit_observations": 0,
    }
    for selected in selection.papers:
        paper = load_paper(selected)
        counts["papers"] += 1
        counts["empty_papers"] += not paper.science.stars
        counts["objects"] += len(paper.science.stars)
        counts["values"] += sum(
            len(q.values) for star in paper.science.stars for q in star.quantities
        )
        counts["audit_observations"] += len(paper.audit.quote_mismatches)
    return counts
