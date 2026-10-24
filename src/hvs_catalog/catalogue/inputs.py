"""Read exact extraction inputs and selected derived results without mutating workspaces."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ..records.catalog import (
    CatalogSelection,
    CatalogSource,
    PaperSelection,
    SourceObject,
)
from ..records.common import ArtifactRef, sha256_of
from ..records.evidence_audit import EvidenceAudit, quote_mismatches
from ..records.extraction_input import ExtractionInput
from ..records.extraction_run import ExtractionRun
from ..records.literature_contribution import LiteratureContribution
from ..workspace import open_workspace, resolve_within
from ..workspace.archives import read_json


@dataclass
class LoadedPaper:
    science: LiteratureContribution
    science_bytes: bytes
    audit: EvidenceAudit
    audit_bytes: bytes
    source: CatalogSource

    @property
    def key(self):
        return self.science.metadata.arxiv_id + self.science.metadata.version


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_bound(root, ref):
    ref = ArtifactRef.model_validate(ref)
    raw = resolve_within(Path(root).resolve(), ref.path).read_bytes()
    if digest(raw) != ref.sha256:
        raise ValueError("Input hash mismatch: " + ref.path)
    return raw


def json_bytes(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return (
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode()


def load_selection(path):
    path = Path(path).resolve()
    selection = CatalogSelection.model_validate_json(path.read_bytes())
    for item in selection.papers:
        item.workspace = str((path.parent / item.workspace).resolve())
    return selection


def selection_from_index(index, workspace, data_version):
    """Adapt both existing result-reference spellings; never scan for latest files."""
    raw = json.loads(Path(index).read_bytes())
    papers = []
    for item in raw["papers"]:
        refs = item["outputs"]

        def get(old, new, refs=refs):
            value = refs.get(old, refs.get(new))
            if value is None:
                raise ValueError("Execution index is missing " + old)
            return ArtifactRef.model_validate(value)

        papers.append(
            PaperSelection(
                workspace=str(Path(workspace).resolve()),
                run_ref=get("run_ref", "run.json"),
                result_ref=get("result_ref", "literature_contribution.json"),
                evidence_audit_ref=get("evidence_audit_ref", "evidence_audit.json"),
            )
        )
    return CatalogSelection(data_version=data_version, papers=papers)


def _diagnostics(record):
    return {
        "issues": [i.model_dump() for i in record.data.issues],
        "rejected": {
            k: [i.model_dump() for i in v] for k, v in record.data.rejected.items()
        },
    }


def _native(space, raw):
    from ..pipeline.evidence_audit import verify_result_audit

    run = ExtractionRun.model_validate(raw)
    if run.scope != "full" or run.status.result not in {"complete", "partial"}:
        raise ValueError("Select a delivered full extraction result.")
    if not run.result_ref or not run.evidence_audit_ref:
        raise ValueError("A result and its evidence audit are required.")
    document = read_json(space, run.input_ref)
    fixed = ExtractionInput.model_validate(document)
    science_bytes = space.read_file(run.result_ref)
    science = LiteratureContribution.model_validate_json(science_bytes)
    trace = read_json(space, run.trace_ref)
    for item in trace["snapshots"]:
        from ..records.common import RecordRef

        record = space.store().read_record(RecordRef.model_validate(item["ref"]))
        if (
            record is None
            or sha256_of(record.model_dump(mode="json", by_alias=True))
            != item["sha256"]
        ):
            raise ValueError("Extraction snapshot differs from its frozen trace.")
    if run.stages.roster.snapshot_ref is None:
        raise ValueError("A delivered result requires a retained roster snapshot.")
    roster = space.store().read_record(run.stages.roster.snapshot_ref)
    quantities = {
        stage.source_index: space.store().read_record(stage.snapshot_ref)
        for stage in run.stages.quantities
        if stage.snapshot_ref
    }
    if roster is None or len(quantities) != len(science.stars):
        raise ValueError("Retained source stages are incomplete.")
    verify_result_audit(space, run, document, science, roster, quantities)
    objects = []
    for position, index in enumerate(roster.data.source_indices):
        record = quantities[index]
        objects.append(
            SourceObject(
                result_index=position,
                source_index=index,
                status=record.data.status,
                snapshot_ref=record.ref(),
                source_paths=record.data.source_paths,
                evidence_audit=[],
                **_diagnostics(record),
            )
        )
    return (
        science_bytes,
        run.result_ref,
        run.evidence_audit_ref,
        fixed,
        run.input_ref,
        {
            "result_status": run.status.result,
            "roster_status": roster.data.status,
            "roster_issues": _diagnostics(roster)["issues"],
            "roster_rejected": _diagnostics(roster)["rejected"],
            "errors": run.errors,
            "objects": objects,
        },
    )


def _derived(space, raw):
    """Replay frozen repair fragments without scoring or reading Gold."""
    from ..benchmark.repair import repaired_prediction
    from .repairs import repair_inputs

    if (
        raw.get("version") != 1
        or not raw.get("delivered")
        or not raw.get("format_valid")
    ):
        raise ValueError("Unsupported or undelivered repair result.")
    refs = raw["outputs"]
    retained = json.loads(
        space.read_file(ArtifactRef.model_validate(refs["retained-prediction.json"]))
    )
    facts, baselines, jobs = repair_inputs(space, raw, retained)
    paper = raw["arxiv_id"]
    expected = repaired_prediction(baselines[paper], facts[paper], jobs)
    science_ref = ArtifactRef.model_validate(refs["literature_contribution.json"])
    science_bytes = space.read_file(science_ref)
    science = LiteratureContribution.model_validate_json(science_bytes)
    if science != expected.content or retained[
        "content"
    ] != expected.content.model_dump(mode="json"):
        raise ValueError("Derived science differs from validated repair fragments.")
    if raw["completion"] != expected.completion or expected.completion not in {
        "complete",
        "partial",
    }:
        raise ValueError("Derived completion differs from retained stages.")
    context = facts[paper]
    baseline = baselines[paper]["provenance"]
    fixed = ExtractionInput.model_validate(baseline["input"])
    input_ref = ArtifactRef.model_validate(baseline["run"]["input_ref"])
    if read_json(space, input_ref) != baseline["input"]:
        raise ValueError("Repair input differs from the frozen baseline.")
    roster = context["roster"]["data"] if context["roster"] else None
    roster_fragment = next((j["fragment"] for j in jobs if j["kind"] == "roster"), None)
    indices = (
        roster_fragment["indices"] if roster_fragment else roster["source_indices"]
    )
    rd = roster_fragment or roster
    objects = []
    for position, index in enumerate(indices):
        numerical = context["quantities"].get(str(index))
        job = next(
            (
                j
                for j in jobs
                if j["kind"] == "quantities"
                and j["source_index"] == index
                and j["fragment"]
            ),
            None,
        )
        data = job["fragment"] if job else numerical["data"]
        conflicts = expected.provenance["network_repair"]["conflicts"]
        status = (
            "partial"
            if job and any(c["step"] == job["step"] for c in conflicts)
            else data["status"]
        )
        objects.append(
            SourceObject(
                result_index=position,
                source_index=index,
                status=status,
                snapshot_ref=None
                if job
                else {k: numerical[k] for k in ("record_id", "revision")},
                repair_stage_id=job["id"] if job else None,
                issues=data["issues"],
                rejected=data["rejected"],
                source_paths=data.get("source_paths", []),
                evidence_audit=[],
            )
        )
    return (
        science_bytes,
        science_ref,
        ArtifactRef.model_validate(refs["evidence_audit.json"]),
        fixed,
        input_ref,
        {
            "result_status": expected.completion,
            "roster_status": rd["status"],
            "roster_issues": rd["issues"],
            "roster_rejected": rd["rejected"],
            "errors": [
                *[{"message": e} for e in expected.errors],
                *expected.provenance["network_repair"]["conflicts"],
            ],
            "objects": objects,
        },
    )


def load_paper(selection):
    if not selection.archive_ref:
        raw = json.loads(read_bound(Path(selection.workspace), selection.run_ref))
        if raw.get("format") == "hvs.final-literature-result":
            from .delivery import load_final

            return load_final(selection)
    base = open_workspace(Path(selection.workspace), read_only=True)
    space = base
    try:
        if selection.archive_ref:
            from ..benchmark.archive import ArchiveSpace

            space = ArchiveSpace(
                base, json.loads(base.read_file(selection.archive_ref))
            )
        raw = json.loads(space.read_file(selection.run_ref))
        loader = (
            _derived if raw.get("format") == "hvs.network-repair-result" else _native
        )
        science_bytes, result_ref, audit_ref, fixed, input_ref, statuses = loader(
            space, raw
        )
        if selection.result_ref and selection.result_ref != result_ref:
            raise ValueError("Index science reference differs from the selected run.")
        if selection.evidence_audit_ref and selection.evidence_audit_ref != audit_ref:
            raise ValueError("Index audit reference differs from the selected run.")
        science = LiteratureContribution.model_validate_json(science_bytes)
        if science.metadata != fixed.metadata:
            raise ValueError("Paper metadata differs from the frozen input.")
        audit_bytes = space.read_file(audit_ref)
        audit = EvidenceAudit.model_validate_json(audit_bytes)
        if (
            audit.metadata.result_ref != result_ref
            or audit.metadata.arxiv_id != science.metadata.arxiv_id
            or audit.metadata.arxiv_version != science.metadata.version
        ):
            raise ValueError("Audit is bound to a different result.")
        material = read_json(space, fixed.material_ref)
        if loader is _derived and audit.quote_mismatches != quote_mismatches(
            science.model_dump(mode="json"), material["citation_locations"]
        ):
            raise ValueError("Audit differs from the fixed scientific content.")
        if len(fixed.snapshot.source_refs) != 1:
            raise ValueError(
                "A paper result requires exactly one fixed source package."
            )
        source = space.store().read_record(fixed.snapshot.source_refs[0])
        if (
            source is None
            or source.data.paper_key != fixed.metadata.arxiv_id
            or source.data.source_version != fixed.metadata.version
        ):
            raise ValueError("Source package differs from the selected paper version.")
        saved_metadata = read_json(space, fixed.metadata_ref)
        if saved_metadata.get("metadata") != fixed.metadata.model_dump():
            raise ValueError(
                "Archived bibliographic metadata differs from the selected paper."
            )
        pdfs = [f for f in source.data.files if f.role == "pdf"]
        if len(pdfs) != 1:
            raise ValueError("An exact source PDF is required.")
        pdf = pdfs[0]
        space.read_file(ArtifactRef(path=pdf.path, sha256=pdf.sha256))
        for obj in statuses["objects"]:
            prefix = f"/stars/{obj.result_index}/"
            obj.evidence_audit = [
                a
                for a in audit.quote_mismatches
                if a.result_path.startswith(prefix)
                or a.result_path.startswith("/notes/")
            ]
        return LoadedPaper(
            science,
            science_bytes,
            audit,
            audit_bytes,
            CatalogSource(
                **science.metadata.model_dump(),
                paper_notes=science.notes,
                result_sha256=result_ref.sha256,
                run_sha256=selection.run_ref.sha256,
                input_sha256=input_ref.sha256,
                pdf_sha256=pdf.sha256,
                pdf_origin=source.data.origin.model_dump(mode="json"),
                pdf_url="https://arxiv.org/pdf/"
                + science.metadata.arxiv_id
                + science.metadata.version,
                **statuses,
            ),
        )
    finally:
        space.close()
        if space is not base:
            base.close()


def load_papers(selection):
    papers = {}
    for item in selection.papers:
        loaded = load_paper(item)
        paper_id = loaded.science.metadata.arxiv_id
        if paper_id in papers:
            if papers[paper_id].source != loaded.source:
                raise ValueError("Select exactly one fixed result per paper.")
        else:
            papers[paper_id] = loaded
    return [papers[k] for k in sorted(papers)]
