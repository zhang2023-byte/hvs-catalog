"""Fixed-input extraction creation, advancement, and read-only status."""

from dataclasses import asdict, dataclass, field

from ..llm.managed import CallService
from ..records.common import RecordRef
from ..runtime import create_execution, get_execution, resume_execution
from .configuration import ExtractionConfig, stored_configuration
from .flow import ExtractionFlow
from .inputs import freeze_input


class PipelineRequestError(ValueError):
    """The requested extraction input is invalid."""


@dataclass
class ExtractionReport:
    execution_id: str
    outcome: str
    mode: str
    state: str
    config: dict
    steps: list = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    result_refs: list[dict] = field(default_factory=list)
    service_summary: dict | None = None
    result_status: str | None = None

    def to_dict(self):
        return asdict(self)


def start_extraction(
    space,
    *,
    preparation_execution_id=None,
    roster_ref=None,
    roster_execution_id=None,
    request_key,
    configuration=None,
    scope="full",
):
    from .roster import saved_roster

    if roster_execution_id is not None:
        if roster_ref is not None:
            raise PipelineRequestError("Provide one roster selection.")
        roster = saved_roster(space, roster_execution_id)
        if roster is None:
            raise PipelineRequestError("No saved roster is available.")
        roster_ref = roster.ref()
    if isinstance(roster_ref, dict):
        roster_ref = RecordRef.model_validate(roster_ref)
    config = configuration or ExtractionConfig()
    config = ExtractionConfig.model_validate(config.model_dump())
    with space.lock():
        snapshot = freeze_input(
            space, config, preparation_execution_id, roster_ref, scope
        )
        handle, _ = create_execution(
            space,
            request_key=request_key,
            input_snapshot=snapshot,
            flow=ExtractionFlow(mode=config.mode),
        )
        from .results import PREFIX, publish_result

        if space.store().get_meta(PREFIX + handle.execution_id) is None:
            publish_result(space, handle.execution_id)
    return handle


def resume_extraction(space, execution_id, *, service=None):
    from .batch import ExtractionTask, resume_extractions

    return resume_extractions([ExtractionTask(space, execution_id, service)])[0]


def _resume_extraction_once(space, execution_id, *, service=None):
    from ..workspace.retention import PREFIX, close_extraction
    from .results import publish_result, result_status

    handle = get_execution(space, execution_id)
    config = stored_configuration(handle.input_snapshot)
    service = service or CallService(config.runtime)
    report = resume_execution(
        space, execution_id, ExtractionFlow(mode=handle.mode), service=service
    )
    if (
        report.outcome != "running"
        and space.store().get_meta(PREFIX + execution_id) is None
    ):
        publish_result(space, execution_id, hook=service.hook)
        if (
            report.outcome == "finished"
            and result_status(space, execution_id) == "complete"
        ):
            close_extraction(space, execution_id)
    status = get_extraction(space, execution_id)
    return ExtractionReport(
        execution_id=execution_id,
        outcome=report.outcome,
        mode=report.mode,
        state=status["state"],
        config=status["config"],
        steps=list(report.steps),
        errors=list(report.errors),
        result_refs=status["result_refs"],
        service_summary=report.service_summary,
        result_status=status["result_status"],
    )


def get_extraction(space, execution_id):
    from ..workspace.retention import PREFIX
    from .results import current_result, result_status
    from .roster import saved_roster

    handle = get_execution(space, execution_id)
    run = space.store().read_record_current(handle.run_ref.record_id)
    roster = saved_roster(space, execution_id)
    usage = [
        a.usage.tokens if a.usage else None for s in run.data.steps for a in s.attempts
    ]
    result = current_result(space, execution_id)
    status = result_status(space, execution_id)
    closed = space.store().get_meta(PREFIX + execution_id) is not None
    return {
        "execution_id": execution_id,
        "state": run.data.state,
        "config": stored_configuration(handle.input_snapshot).model_dump(mode="json"),
        "steps": [s.model_dump(mode="json") for s in run.data.steps],
        "result_refs": [r.model_dump() for r in run.data.result_refs],
        "result_status": status,
        "roster_status": roster.data.status if roster else "pending",
        "diagnostics": [i.model_dump() for i in roster.data.issues] if roster else [],
        "errors": [e.message for e in run.data.errors],
        "usage": {
            "attempts": len(usage),
            "tokens": sum(usage) if all(v is not None for v in usage) else None,
        },
        "input_digest": handle.input_digest,
        "science_completion": {
            "state": status or "incomplete",
            "reasons": [] if status == "complete" else ["extraction_incomplete"],
        },
        "run_ref": result[0].model_dump() if result else None,
        "retention": "closed" if closed else "open",
        "resumable": not closed,
    }


def execution_result_refs(space, execution_id):
    handle = get_execution(space, execution_id)
    return list(
        space.store().read_record_current(handle.run_ref.record_id).data.result_refs
    )
