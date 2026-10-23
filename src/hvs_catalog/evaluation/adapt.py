"""Read exact saved extraction facts without resuming or rewriting an execution."""

from ..llm.prompts import PromptInput
from ..pipeline.evidence_audit import retained_mismatches, verify_result_audit
from ..pipeline.results import PREFIX, saved_quantities
from ..pipeline.roster import saved_roster
from ..records.common import ArtifactRef, RecordRef, sha256_of
from ..records.evidence_audit import uses_quote_warnings
from ..records.extraction_input import ExtractionInput
from ..records.extraction_run import ExtractionRun
from ..records.literature_contribution import LiteratureContribution
from ..records.quantities import QuantityRecord
from ..records.roster import RosterRecord
from ..records.roster_validation import strict_json
from ..records.source import SourcePackageRecord
from ..records.validate import validate_collection
from ..runtime import get_execution
from ..workspace.archives import read_json
from ..workspace.store import record_closure, verify_artifact_refs
from .scoring import Fingerprint, MaterialBinding, MaterialFile, Prediction


def prediction_from_execution(space, execution_id) -> Prediction:
    """Freeze a published generation, or current saved stages if none was delivered."""
    with space.lock():
        return _prediction(space, execution_id)


def _prediction(space, execution_id):
    handle = get_execution(space, execution_id)
    run = space.store().read_record(handle.run_ref)
    input_ref = ArtifactRef.model_validate_json(
        handle.input_snapshot.options["extraction_input"]
    )
    document = ExtractionInput.model_validate(read_json(space, input_ref))
    expected = handle.input_snapshot.model_dump(mode="json")
    expected["options"].pop("extraction_input")
    if (
        document.snapshot.model_dump(mode="json") != expected
        or len(document.snapshot.source_refs) != 1
    ):
        raise ValueError("The saved extraction input differs from its execution.")
    source = space.store().read_record(document.snapshot.source_refs[0])
    if not isinstance(source, SourcePackageRecord):
        raise TypeError("The fixed input must reference a source package.")
    if (source.data.paper_key, source.data.source_version) != (
        document.metadata.arxiv_id,
        document.metadata.version,
    ):
        raise ValueError("The source and fixed metadata identify different papers.")
    prompt = PromptInput.model_validate(read_json(space, document.material_ref))
    metadata = read_json(space, document.metadata_ref)
    if (
        metadata.get("format") != "hvs.paper-metadata"
        or metadata.get("version") != 1
        or metadata.get("metadata") != document.metadata.model_dump()
    ):
        raise ValueError("The archived metadata differs from the fixed input.")
    # Extraction code may have changed since this run. Only stored content and exact
    # bindings are checked here; scoring does not use the resume compatibility gate.
    roster = saved_roster(space, execution_id)
    if roster is not None and not isinstance(roster, RosterRecord):
        raise ValueError("The saved roster reference must identify a roster snapshot.")
    quantities = saved_quantities(space, execution_id, roster) if roster else {}
    summary, trace, generation = None, None, None
    marker = space.store().get_meta(PREFIX + execution_id)
    science = None
    format_error = None
    if marker:
        generation = ArtifactRef.model_validate_json(marker)
        summary = ExtractionRun.model_validate(
            strict_json(space.read_file(generation).decode())
        )
        if (
            summary.execution_id != execution_id
            or summary.input_ref != input_ref
            or summary.scope != document.snapshot.options["extraction_scope"]
        ):
            raise ValueError(
                "The committed result generation belongs to another input or execution."
            )
        trace = read_json(space, summary.trace_ref)
        if (
            trace.get("execution_id") != execution_id
            or trace.get("format") != "hvs.extraction-trace"
            or trace.get("version") != 1
        ):
            raise ValueError(
                "The result trace has an unsupported version or execution binding."
            )
        trace_records = {}
        for item in trace["snapshots"]:
            record = space.store().read_record(RecordRef.model_validate(item["ref"]))
            if (
                record is None
                or sha256_of(record.model_dump(mode="json", by_alias=True))
                != item["sha256"]
            ):
                raise ValueError(
                    "A published snapshot differs from the committed trace."
                )
            trace_records[(record.record_id, record.revision)] = record

        def selected(stage, model):
            if stage.snapshot_ref is None:
                return None
            ref = stage.snapshot_ref
            record = trace_records.get((ref.record_id, ref.revision))
            if (
                not isinstance(record, model)
                or record.data.status != stage.status
                or record.execution_id != stage.source_execution
                or record.data.source_round != stage.source_round
                or record.data.fallback != stage.fallback
                or (record.data.notes is not None) != stage.notes_available
            ):
                raise ValueError("A published stage is not bound to its snapshot.")
            return record

        published_roster = selected(summary.stages.roster, RosterRecord)
        published_quantities = {}
        for stage in summary.stages.quantities:
            record = selected(stage, QuantityRecord)
            if record:
                if (
                    stage.source_index != record.data.source_index
                    or stage.source_index in published_quantities
                ):
                    raise ValueError(
                        "A published numerical stage has an invalid target."
                    )
                published_quantities[stage.source_index] = record
        if summary.result_ref or (
            summary.scope == "roster" and summary.status.result is not None
        ):
            roster, quantities = published_roster, published_quantities
        if summary.result_ref:
            raw = space.read_file(summary.result_ref)
            try:
                science = LiteratureContribution.model_validate(
                    strict_json(raw.decode())
                )
            except ValueError as exc:
                format_error = str(exc)
    seeds = [source, *([roster] if roster else []), *quantities.values()]
    closure = record_closure(seeds, space.store().read_record)
    validate_collection(closure, space.root)
    problems = verify_artifact_refs(
        [
            *closure,
            {
                "record_id": execution_id,
                "revision": run.revision,
                "input": document.model_dump(mode="json"),
                "trace": trace,
            },
        ],
        space.root,
    )
    if problems:
        raise ValueError(
            "Saved extraction artifacts failed validation: " + "; ".join(problems)
        )
    if roster and (
        roster.data.source_ref != source.ref()
        or roster.data.material_ref != document.material_ref
    ):
        raise ValueError("The roster differs from the fixed source or material.")
    if roster and roster.execution_id != execution_id:
        imported = document.snapshot.options.get("roster_ref")
        if imported is None or RecordRef.model_validate_json(imported) != roster.ref():
            raise ValueError(
                "The roster belongs to another execution without explicit reuse."
            )
    for index, record in quantities.items():
        if (
            not isinstance(record, QuantityRecord)
            or not roster
            or record.data.roster_ref != roster.ref()
            or record.data.source_index != index
            or index not in roster.data.source_indices
            or record.data.material_ref != document.material_ref
            or record.execution_id != execution_id
        ):
            raise ValueError(
                "A numerical snapshot belongs to another target or execution."
            )
    notes = [roster.data.notes] if roster and roster.data.notes else []
    stars = []
    for index, star in (
        zip(roster.data.source_indices, roster.data.stars, strict=True)
        if roster
        else []
    ):
        numerical = quantities.get(index)
        stars.append(
            {
                **star.model_dump(mode="json"),
                "quantities": [
                    q.model_dump(mode="json") for q in numerical.data.quantities
                ]
                if numerical
                else [],
            }
        )
        if numerical and numerical.data.notes:
            notes.append(star.identifiers[0].value + ": " + numerical.data.notes)
    retained = LiteratureContribution.model_validate(
        {"metadata": document.metadata.model_dump(), "stars": stars, "notes": notes}
    )
    if science is not None and science != retained:
        raise ValueError("The science file differs from its exact stage snapshots.")
    errors = [e.message for e in run.data.errors]
    if format_error:
        errors.append("Invalid final scientific format: " + format_error)
    scope = document.snapshot.options["extraction_scope"]
    completion = "unfinished"
    if scope == "roster" and roster:
        completion = roster.data.status
    elif roster and roster.data.status == "failed":
        completion = "failed"
    elif summary and summary.result_ref:
        completion = summary.status.result
        expected_status = (
            "complete"
            if roster
            and roster.data.status == "complete"
            and all(q.data.status == "complete" for q in quantities.values())
            else "partial"
        )
        if (
            not roster
            or len(quantities) != len(roster.data.stars)
            or completion != expected_status
        ):
            raise ValueError("The result status differs from its retained stages.")
    mismatches = retained_mismatches(
        space, retained, roster, quantities, prompt.citation_locations
    )
    warning_mode = uses_quote_warnings(document.snapshot.options.get("evidence_policy"))
    if summary:
        verify_result_audit(
            space,
            summary,
            document.model_dump(mode="json"),
            science,
            roster,
            quantities,
        )
    delivered = (
        bool(summary and summary.result_ref)
        if scope == "full"
        else bool(roster and roster.data.status != "failed")
    )
    return Prediction(
        content=retained,
        rules=Fingerprint(**document.snapshot.rules.model_dump()),
        materials=MaterialBinding(
            files=[
                MaterialFile(role=f.role, sha256=f.sha256)
                for f in source.data.files
                if f.role in {"pdf", "supplement"}
            ],
            material_sha256=document.material_ref.sha256,
        ),
        scope=scope,
        delivered=delivered,
        format_valid=delivered and format_error is None,
        completion=completion,
        execution_state=summary.status.execution
        if summary and summary.result_ref
        else run.data.state,
        errors=errors,
        evidence_checks={
            "invalid": 0,
            "checked": _evidence_count(retained.model_dump(mode="json")),
            **({"quote_mismatches": len(mismatches)} if warning_mode else {}),
        },
        provenance={
            "execution_id": execution_id,
            "generation_ref": generation.model_dump() if generation else None,
            "run": summary.model_dump(mode="json") if summary else None,
            "execution_record": run.model_dump(mode="json", by_alias=True),
            "input": document.model_dump(mode="json"),
            "trace": trace,
            "snapshots": [r.model_dump(mode="json", by_alias=True) for r in seeds],
            "citation_locations": prompt.citation_locations,
        },
    )


def _evidence_count(value):
    if isinstance(value, list):
        return sum(_evidence_count(item) for item in value)
    if isinstance(value, dict):
        return sum(
            len(item) if key == "evidence" else _evidence_count(item)
            for key, item in value.items()
        )
    return 0
