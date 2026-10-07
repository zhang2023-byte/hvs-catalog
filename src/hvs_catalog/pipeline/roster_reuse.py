"""Freeze a validated roster as input to independent quantity branches."""

import json

from ..records.common import RecordRef
from ..records.paper import PaperRecord
from ..records.roster import RosterRecord
from ..runtime import get_execution
from .configuration import stored_configuration


def freeze_roster(space, execution_id, snapshot):
    source = get_execution(space, execution_id)
    run = space.store().read_record(source.run_ref)
    previous = source.input_snapshot
    if (
        run.data.state != "finished"
        or previous.options.get("extraction_scope") != "roster"
        or previous.expert_adjudication
        or snapshot.expert_adjudication
    ):
        raise ValueError("reuse requires a finished automatic roster execution")
    old_config, new_config = (
        stored_configuration(previous),
        stored_configuration(snapshot),
    )
    if (
        previous.source_refs != snapshot.source_refs
        or previous.rules != snapshot.rules
        or previous.options["prepared_input"] != snapshot.options["prepared_input"]
        or previous.options["prepared_prompt"] != snapshot.options["prepared_prompt"]
        or old_config.model_for("roster") != new_config.model_for("roster")
        or json.loads(space.read_file(previous.prompt_refs[0]))["roster"]
        != json.loads(space.read_file(snapshot.prompt_refs[0]))["roster"]
    ):
        raise ValueError(
            "reused roster differs in fixed material, rules, prompt or model"
        )
    step = next(s for s in run.data.steps if s.step_id == "roster")
    if step.state != "finished":
        raise ValueError("roster step is not committed")
    records = [space.store().read_record(ref) for ref in step.result_refs]
    paper = next(r for r in records if isinstance(r, PaperRecord))
    roster = next(r for r in records if isinstance(r, RosterRecord))
    if (
        paper.ref() != space.store().read_record_current(paper.record_id).ref()
        or roster.ref() != space.store().read_record_current(roster.record_id).ref()
    ):
        raise ValueError("roster results changed after execution")
    return {
        "execution_id": execution_id,
        "input_digest": source.input_digest,
        "run_ref": source.run_ref.model_dump(mode="json"),
        "paper_ref": paper.ref().model_dump(mode="json"),
        "roster_ref": roster.ref().model_dump(mode="json"),
        "result_refs": [r.model_dump(mode="json") for r in step.result_refs],
    }


def clone_roster(ctx):
    """Rebind application IDs only; keep scientific content and evidence unchanged."""
    frozen = json.loads(ctx.execution.input_snapshot.options["reused_roster"])
    if (
        freeze_roster(ctx.space, frozen["execution_id"], ctx.execution.input_snapshot)
        != frozen
    ):
        raise ValueError("frozen reused roster execution has changed")
    old_prefix = "roster-" + frozen["execution_id"] + "-"
    new_prefix = "roster-" + ctx.execution.execution_id + "-"

    id_fields = {
        "record_id",
        "execution_id",
        "item_id",
        "group_id",
        "contribution_id",
        "roster_item_id",
        "value_id",
        "error_id",
        "conflict_id",
        "known_member_item_ids",
    }

    def rebind(value, field=None):
        if isinstance(value, dict):
            return {key: rebind(child, key) for key, child in value.items()}
        if isinstance(value, list):
            return [rebind(child, field) for child in value]
        if isinstance(value, str) and field in id_fields:
            if value == frozen["execution_id"]:
                return ctx.execution.execution_id
            if value.startswith(old_prefix):
                return new_prefix + value[len(old_prefix) :]
        return value

    records = []
    for raw_ref in frozen["result_refs"]:
        record = ctx.store.read_record(RecordRef.model_validate(raw_ref))
        records.append(
            type(record).model_validate(rebind(record.model_dump(mode="json")))
        )
    return records
