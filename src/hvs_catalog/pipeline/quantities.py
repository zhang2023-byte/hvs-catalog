"""Real fixed-PDF extraction with independent object submissions and durable results."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..llm.config import CallRequest, Message
from ..llm.managed import CallFailure, CallService, execute_model_call
from ..llm.prompts import PromptBlueprint, PromptTarget
from ..records import StepContext, convert_quantity_submission, validate_collection
from ..records.common import RecordRef, build_record, canonical_json, sha256_of
from ..records.paper import PaperRecord
from ..records.quantity_input import (
    bind_quantity_input,
    canonicalize_limit_operands,
    expand_object_quantity_input,
)
from ..records.questions import QuestionRecord, compute_question_fingerprint
from ..records.rules import quantity_paths
from ..records.runs import ModelJudgment
from ..records.submission import QuantitySubmission, SubmissionError, ValueSubmission
from ..review import ReviewService, derive_question_id
from ..runtime.deferred import WaitingForCall
from ..runtime.execution import append_run_revision
from ..workspace.archives import read_json
from .configuration import stored_configuration
from .network_retry import format_correction_limit, object_retry_token, retry_call_key
from .roster import (
    RosterFlow,
    RosterState,
    _argument_cleanup,
    _citation_repairs,
    _clean_citations,
    _format_details,
    _hydrate_sources,
    _prior_submission,
    _question_content,
    _replace_question_refs,
    _save_argument_cleanup,
    roster_result_refs,
    saved_roster,
)
from .roster_repair import Repair, apply_repairs, repair_tool, schema_repairs


def start_pdf_extraction(space, **kwargs):
    """Run the same roster followed by real quantities, using fixed preparation."""
    from .roster import start_roster_extraction

    return start_roster_extraction(space, full=True, **kwargs)


def refresh_saved_partials(space, execution_id, *, object_ids):
    """Replay selected failed objects' archived replies to retain validated partials.

    This performs no model requests, cannot consume missing corrections, leaves
    the run failed, and reassembles the bound paper. Old records and attempts stay
    immutable. Useful after fixing application validation/partial-saving bugs.
    """
    from ..runtime.execution import (
        RuntimeContext,
        append_run_revision,
        get_execution,
        verify_execution_inputs,
    )
    from .configuration import validate_configuration_snapshot

    class ArchiveOnly(CallService):
        def credential(self):
            # execute_model_call checks this before quota/attempt creation when
            # no reusable reply exists. No credential or network access is possible.
            raise CallFailure("Offline refresh requires an already archived reply")

    if not object_ids or len(object_ids) != len(set(object_ids)):
        raise ValueError("offline refresh requires unique selected objects")
    with space.lock():
        from ..workspace.retention import require_resumable

        require_resumable(space, execution_id)
        handle = get_execution(space, execution_id)
        verify_execution_inputs(space, handle)
        config = validate_configuration_snapshot(handle.input_snapshot)
        run = space.store().read_record(handle.run_ref)
        if (
            run.data.state != "failed"
            or handle.input_snapshot.expert_adjudication
            or handle.input_snapshot.options.get("extraction_scope") != "full"
        ):
            raise ValueError(
                "offline refresh requires a failed automatic full extraction"
            )
        flow = PDFExtractionFlow(mode=handle.mode)
        ctx = RuntimeContext(
            space=space,
            execution=handle,
            flow=flow,
            service=ArchiveOnly(config.runtime),
        )
        source, original, _ = flow._inputs(ctx)
        roster = saved_roster(space, execution_id)
        selected = [e for e in roster.data.entries if e.contribution_id in object_ids]
        if len(selected) != len(object_ids):
            raise ValueError("selected object is absent from the saved roster")
        for entry in selected:
            step_id = "quantities-" + entry.item_id
            step = next((s for s in run.data.steps if s.step_id == step_id), None)
            if step is None or step.state == "finished" or not step.attempts:
                raise ValueError(
                    "offline refresh only accepts previously attempted failed objects"
                )
        blueprint = PromptBlueprint.model_validate(
            json.loads(space.read_file(handle.input_snapshot.prompt_refs[0]))[
                "quantities"
            ]
        )
        errors = []
        for entry in selected:
            inputs = original.model_copy(
                update={
                    "targets": [
                        PromptTarget(
                            object_id=entry.contribution_id,
                            identifiers=[i.value for i in entry.identifiers],
                        )
                    ]
                }
            )
            request = blueprint.request(config.model_for("quantities"), inputs)
            request.tool.parameters["title"] = "Quantities for " + entry.contribution_id
            digest = sha256_of(request.model_dump(mode="json"))
            try:
                flow._submit(
                    ctx,
                    source,
                    inputs,
                    request,
                    "quantities-" + entry.item_id,
                    digest,
                    entry.contribution_id,
                )
            except (CallFailure, SubmissionError, ValueError) as exc:
                errors.append(str(exc))
        paper = flow._assemble(ctx)
        if (
            space.store().read_record_current(handle.run_ref.record_id).data.state
            != "failed"
        ):
            append_run_revision(space, handle, state="failed")
        return {
            "paper_ref": paper.ref().model_dump(mode="json"),
            "errors": errors,
            "new_model_calls": 0,
        }


class PDFExtractionFlow(RosterFlow):
    """Preserve each successful object before reviewing or failing another one."""

    defer_review = True
    defer_partial_failure = True

    def __init__(self, *, mode):
        super().__init__(mode=mode)
        self.flow_id = f"hvs-pdf-{mode}"

    def build_graph(self, ctx):
        # Reuse preflight and fixed prepared-input validation from the roster flow.
        super().build_graph(ctx)
        config = stored_configuration(ctx.execution.input_snapshot)
        if ctx.service.adapter.name != config.model_for("quantities").adapter:
            raise ValueError("quantity adapter does not match the frozen configuration")
        builder = StateGraph(RosterState)
        builder.add_node("roster", lambda state: self._extract(ctx, state))
        builder.add_node("quantities", lambda state: self._quantities(ctx, state))
        builder.add_node("finalize", lambda state: self._finalize(ctx, state))
        builder.add_edge(START, "roster")
        builder.add_edge("roster", "quantities")
        builder.add_edge("quantities", "finalize")
        builder.add_edge("finalize", END)
        return builder.compile(checkpointer=ctx.checkpointer)

    def _paper(self, ctx):
        refs = roster_result_refs(ctx.space, ctx.execution.execution_id)
        if refs:
            original = next(
                ref
                for ref in refs
                if isinstance(ctx.store.read_record(ref), PaperRecord)
            )
            return self._applied_record(ctx, original)
        return self._applied_record(
            ctx,
            RecordRef(
                record_id=f"roster-{ctx.execution.execution_id}-paper", revision=1
            ),
        )

    def _applied_record(self, ctx, original):
        """Follow only exact result references from this execution's decisions."""
        current = original
        applications = []
        for view in ReviewService(ctx.space).list_questions(
            execution_id=ctx.execution.execution_id, include_scientific_issues=True
        ):
            if not view.application or view.application.phase != "applied":
                continue
            for decision in view.decisions:
                patch = decision.data.result_patch
                if patch and patch.target.record_id == original.record_id:
                    for ref in view.application.result_refs:
                        if ref.record_id == original.record_id:
                            applications.append((patch.target.revision, ref))
        while True:
            next_refs = {
                canonical_json(ref.model_dump()): ref
                for revision, ref in applications
                if revision == current.revision
            }
            if not next_refs:
                break
            if len(next_refs) != 1:
                raise SubmissionError("Conflicting applied result revisions")
            following = next(iter(next_refs.values()))
            if following.revision != current.revision + 1:
                raise SubmissionError("Applied result revision chain is incomplete")
            current = following
        record = ctx.store.read_record(current)
        if ctx.store.read_record_current(current.record_id).ref() != current:
            raise SubmissionError(
                "Result revision has no matching applied decision receipt"
            )
        return record

    def _extract(self, ctx, state):
        raw = ctx.execution.input_snapshot.options.get("reused_roster")
        if raw is None:
            return super()._extract(ctx, state)
        from .roster_reuse import clone_roster

        frozen = json.loads(raw)
        digest = sha256_of(frozen)
        records = clone_roster(ctx)
        refs = [record.ref() for record in records]
        if ctx.reuse_step("roster", digest) is None:
            ctx.commit_step(
                step_id="roster", input_digest=digest, records=records, result_refs=refs
            )
        return {"result_refs": [ref.model_dump(mode="json") for ref in refs]}

    def _quantities(self, ctx, state):
        if state.get("roster_error"):
            # A bounded explicit recovery can complete the roster after this
            # node was checkpointed. Otherwise the cached partial makes no call.
            state = {**state, **super()._extract(ctx, state)}
        source, original, _ = self._inputs(ctx)
        snapshot = ctx.execution.input_snapshot
        config = stored_configuration(snapshot)
        blueprint = PromptBlueprint.model_validate(
            json.loads(ctx.space.read_file(snapshot.prompt_refs[0]))["quantities"]
        )
        errors = (
            ["Roster extraction incomplete: " + state["roster_error"]]
            if state.get("roster_error")
            else []
        )
        pending = False
        roster = saved_roster(ctx.space, ctx.execution.execution_id)
        for entry in roster.data.entries:
            if entry.disposition != "included":
                continue
            # Identity questions pause only this object's processing.
            if any(
                ctx.store.read_record(r).data.category in {"identity", "inclusion"}
                and ctx.store.read_record_current(r.record_id).data.status == "open"
                for r in entry.issue_refs
            ):
                continue
            target = PromptTarget(
                object_id=entry.contribution_id,
                identifiers=[i.value for i in entry.identifiers],
            )
            inputs = original.model_copy(update={"targets": [target]})
            request = blueprint.request(config.model_for("quantities"), inputs)
            spec = request.tool.parameters
            spec["title"] = "Quantities for " + entry.contribution_id
            step_id = "quantities-" + entry.item_id
            digest = sha256_of(request.model_dump(mode="json"))
            if ctx.reuse_step(step_id, digest) is not None:
                continue
            try:
                self._submit(
                    ctx, source, inputs, request, step_id, digest, entry.contribution_id
                )
            except WaitingForCall:
                pending = True
            except (CallFailure, SubmissionError, ValueError) as exc:
                errors.append(f"{entry.item_id}: {exc}")
                # Each response is already archived; continue independent objects.
        self._assemble(ctx)
        if pending:
            raise WaitingForCall()
        if errors:
            raise CallFailure("Quantity extraction incomplete: " + "; ".join(errors))
        if snapshot.expert_adjudication:
            review = ReviewService(ctx.space)
            for view in review.list_questions(execution_id=ctx.execution.execution_id):
                while True:
                    current = review.get_question(view.question_ref)
                    if current.application and current.application.phase == "applied":
                        break
                    interrupt(
                        {
                            "question_ref": view.question_ref.model_dump(mode="json"),
                            "step_id": "quantities",
                            "prompt": current.record.data.prompt,
                        }
                    )
            # A roster decision may have supplied new eligible objects. Reenter
            # the node with current revisions; successful object calls are reused.
            current_roster = ctx.store.read_record_current(roster.record_id)
            if current_roster.revision != roster.revision:
                return self._quantities(ctx, state)
        paper = self._assemble(ctx)
        digest = sha256_of(paper.data.model_dump(mode="json"))
        if ctx.reuse_step("quantities", digest) is None:
            ctx.commit_step(
                step_id="quantities",
                input_digest=digest,
                records=[],
                result_refs=[paper.ref()],
            )
        return {"result_refs": [paper.ref().model_dump(mode="json")]}

    def _convert(self, ctx, source, inputs, payload, item_id, digest, *, step_id):
        token = object_retry_token(ctx, step_id)
        if token or digest.endswith("-partial"):
            # Fresh generations and retained partials must never collide with
            # earlier immutable object/evidence records sharing a request digest.
            digest = sha256_of({"request_digest": digest, "object_retry": token})
        if "measurements" in payload:
            payload = expand_object_quantity_input(payload, item_id)
        if not ctx.execution.input_snapshot.expert_adjudication and payload.get(
            "questions", []
        ):
            raise SubmissionError(
                "Automatic extraction must record a model judgment, not request expert interpretation"
            )
        supplied = {v["file_id"] for v in inputs.citation_locations.values()}
        hydrated = _hydrate_sources(
            bind_quantity_input(payload, inputs.citation_locations),
            ctx.space,
            source,
            supplied,
        )
        for obj in hydrated["objects"]:
            for declaration in [
                obj["coverage"]["common"],
                *obj["coverage"]["exceptions"],
            ]:
                if not set(declaration["checked_file_ids"]) <= supplied:
                    raise SubmissionError(
                        "Quantity coverage cites a file not supplied to the model"
                    )
        # Merge exact repeated presentations without selecting between distinct scientific results.
        for obj in hydrated["objects"]:
            for group in obj["quantities"]:
                unique = {}
                for raw in group["values"]:
                    value = ValueSubmission.model_validate(raw).model_dump(mode="json")

                    def science(v):
                        if isinstance(v, dict):
                            return {
                                k: science(x) for k, x in v.items() if k != "evidence"
                            }
                        if isinstance(v, list):
                            return [science(x) for x in v]
                        return v

                    key = canonical_json(science(value))
                    if key in unique:

                        def merge(a, b):
                            if isinstance(a, dict):
                                for k in a:
                                    if k == "evidence":
                                        seen = {canonical_json(e) for e in a[k]}
                                        a[k].extend(
                                            e
                                            for e in b[k]
                                            if canonical_json(e) not in seen
                                        )
                                    else:
                                        merge(a[k], b[k])
                            elif isinstance(a, list):
                                for x, y in zip(a, b, strict=True):
                                    merge(x, y)

                        merge(unique[key], value)
                    else:
                        unique[key] = value
                group["values"] = list(unique.values())
        paper = self._paper(ctx)
        next_ref = RecordRef(
            record_id=f"quantity-{ctx.execution.execution_id}-{item_id}-{digest[:12]}-paper",
            revision=1,
        )
        context = StepContext(
            source=source,
            paper_ref=next_ref,
            created_at=ctx.now(),
            requested_quantities=quantity_paths(),
            object_ids=(item_id,),
            id_prefix=f"quantity-{ctx.execution.execution_id}-{item_id}-{digest[:12]}",
            execution_id=ctx.execution.execution_id,
            rules_ref=ctx.execution.input_snapshot.rules,
        )
        converted = convert_quantity_submission(
            context, QuantitySubmission.model_validate(hydrated)
        )
        obj = converted.objects[0]
        contributions = []
        found = False
        for contribution in paper.data.object_contributions:
            if contribution.item_id == item_id:
                found = True
                contribution = contribution.model_copy(
                    update={
                        "quantities": obj.quantities,
                        "quantity_conflicts": obj.quantity_conflicts,
                        "author_errors": obj.author_errors,
                        "quantity_coverage": obj.quantity_coverage,
                        "precedent_uses": obj.precedent_uses,
                    }
                )
            contributions.append(contribution)
        if not found:
            raise SubmissionError(
                "Resolved roster inclusion requires a complete contribution revision"
            )
        updated = paper.model_copy(
            update={
                "record_id": next_ref.record_id,
                "revision": next_ref.revision,
                "created_at": ctx.now(),
                "data": paper.data.model_copy(
                    update={"object_contributions": contributions}
                ),
            }
        )
        replacements = {
            q.record_id: derive_question_id(
                ctx.execution.execution_id,
                _question_content(q),
                space_id=ctx.space.identity.space_id,
            )
            for q in converted.questions
        }
        records = [
            _replace_question_refs(r, replacements)
            for r in [updated, *converted.evidence]
        ]
        questions = [
            _replace_question_refs(q, replacements) for q in converted.questions
        ]
        if ctx.execution.input_snapshot.expert_adjudication and len(questions) > 1:
            # One complete object revision must resolve its dependent questions
            # together; sequential patches against revision 1 would become stale.
            content = _question_content(questions[0])
            content = type(content)(
                category="interpretation",
                target_refs=content.target_refs,
                source_refs=content.source_refs,
                rules_ref=content.rules_ref,
                prompt="Resolve these linked numerical questions together in one complete object revision:\n"
                + "\n".join(
                    f"{i}. {q.data.prompt}" for i, q in enumerate(questions, 1)
                ),
                evidence_refs=list(
                    {
                        canonical_json(r.model_dump()): r
                        for q in questions
                        for r in q.data.evidence_refs
                    }.values()
                ),
                gap_refs=list(
                    {
                        canonical_json(r.model_dump()): r
                        for q in questions
                        for r in q.data.gap_refs
                    }.values()
                ),
                context_refs=[q.ref() for q in questions],
            )
            data = content.content_payload()
            data.update(
                fingerprint=compute_question_fingerprint(
                    category=content.category,
                    target_refs=content.target_refs,
                    evidence_refs=content.evidence_refs,
                    source_refs=content.source_refs,
                    rules_ref=content.rules_ref,
                ),
                status="open",
                decision_refs=[],
            )
            questions.append(
                build_record(
                    QuestionRecord,
                    record_id=derive_question_id(
                        ctx.execution.execution_id,
                        content,
                        space_id=ctx.space.identity.space_id,
                    ),
                    execution_id=ctx.execution.execution_id,
                    created_at=ctx.now(),
                    data=data,
                )
            )
        # Validate the real saved-reference closure, including prior roster evidence.
        from ..workspace.store import record_closure

        closure = record_closure([source, *records, *questions], ctx.store.read_record)
        validate_collection(closure, materials_root=ctx.space.root)
        return records, questions

    def _submit(self, ctx, source, inputs, initial, step_id, digest, item_id):
        request = CallRequest.model_validate(initial.model_dump())
        draft = None
        fixes = []
        max_corrections = format_correction_limit(ctx)
        for round_number in range(max_corrections + 1):
            call_key = retry_call_key(ctx, step_id, round_number)
            prepared = ctx.service.adapter.prepare(request)
            if round_number:
                run = ctx.store.read_record_current(ctx.execution.run_ref.record_id)
                step = next((s for s in run.data.steps if s.step_id == step_id), None)
                for attempt in step.attempts if step else []:
                    prefix = (
                        call_key + "-attempt-"
                        if object_retry_token(ctx, step_id)
                        else f"{step_id}-{round_number:02d}-"
                    )
                    if attempt.request and Path(attempt.request.path).name.startswith(
                        prefix
                    ):
                        archived = read_json(ctx.space, attempt.request)
                        frozen, rebuilt = (
                            copy.deepcopy(archived),
                            copy.deepcopy(prepared),
                        )
                        frozen["payload"]["messages"].pop()
                        rebuilt["payload"]["messages"].pop()
                        if frozen != rebuilt:
                            raise CallFailure(
                                "Archived correction belongs to different fixed input"
                            )
                        prepared = archived
                        request.messages = [
                            Message.model_validate(m)
                            for m in archived["payload"]["messages"]
                        ]
            envelope = execute_model_call(
                ctx,
                ctx.service,
                request,
                prepared,
                step_id=step_id,
                call_key=call_key,
            )
            error = envelope.get("error")
            body = envelope.get("exchange", {}).get("body") or {}
            prior = body.get("choices", [{}])[0].get("message", {})
            prior = {k: v for k, v in prior.items() if k in {"content", "tool_calls"}}
            proposed = _prior_submission(prior)
            cleanup = _argument_cleanup(prior)
            if cleanup and error == "invalid_tool_arguments":
                error = None
                _save_argument_cleanup(ctx, prior, step_id, call_key)
            if request.tool.name == "repair_quantities":
                if error is None:
                    draft = apply_repairs(draft, fixes, proposed)
            elif (
                proposed is not None
                and prior["tool_calls"][0]["function"].get("name") == initial.tool.name
            ):
                draft = proposed
            if draft is not None and (
                request.tool.name != "repair_quantities" or error is None
            ):
                draft, operand_changes = canonicalize_limit_operands(draft)
                if operand_changes:
                    artifact = ctx.space.publish_run_file(
                        ctx.execution.execution_id,
                        f"outputs/format-cleanups/{call_key}-limit-operands.json",
                        canonical_json(
                            {
                                "step_id": step_id,
                                "application_repair": "Canonical operand fields for explicitly selected one-sided limits; printed numbers, signs and scientific inequalities unchanged.",
                                "changes": operand_changes,
                            }
                        ).encode(),
                    )
                    append_run_revision(ctx.space, ctx.execution, artifacts=[artifact])
                fixes = schema_repairs(draft, initial.tool.parameters)
                if not fixes:
                    draft = _clean_citations(
                        ctx,
                        draft,
                        inputs,
                        source,
                        step_id,
                        call_key,
                    )
                    fixes = _citation_repairs(draft, inputs, source, ctx.space)
                error = "Correct the selected invalid fields." if fixes else None
            if error is None:
                try:
                    records, questions = self._convert(
                        ctx, source, inputs, draft, item_id, digest, step_id=step_id
                    )
                except ValueError as exc:
                    error = str(exc)
                    fixes = quantity_repairs(draft, initial.tool.parameters, error)
            if error is None:
                if questions:
                    ReviewService(ctx.space).create_questions(
                        execution_id=ctx.execution.execution_id,
                        step_id=step_id
                        if ctx.execution.input_snapshot.expert_adjudication
                        else "scientific-conflict:" + step_id,
                        request_key=f"{ctx.execution.execution_id}:{step_id}:questions"
                        + (
                            ":" + object_retry_token(ctx, step_id)
                            if object_retry_token(ctx, step_id)
                            else ""
                        ),
                        questions=questions,
                        records=records,
                        context_question_ids=tuple(q.record_id for q in questions[:-1])
                        if ctx.execution.input_snapshot.expert_adjudication
                        and len(questions) > 1
                        else (),
                    )
                    committed = []
                else:
                    committed = records
                ctx.commit_step(
                    step_id=step_id,
                    input_digest=digest,
                    records=committed,
                    result_refs=[r.ref() for r in [*records, *questions]],
                )
                for judgment in draft.get("model_judgments", []):
                    append_run_revision(
                        ctx.space,
                        ctx.execution,
                        model_judgment=ModelJudgment(
                            step_id=step_id, judgment=judgment, created_at=ctx.now()
                        ),
                    )
                return
            if round_number == max_corrections:
                self._retain_valid(ctx, source, inputs, draft, step_id, digest, item_id)
                raise CallFailure("Quantity format correction exhausted: " + str(error))
            prior_text = canonical_json(prior)
            if draft is not None:
                if not fixes:
                    self._retain_valid(
                        ctx, source, inputs, draft, step_id, digest, item_id
                    )
                    raise CallFailure("No safe local format repair: " + str(error))
                request.tool = repair_tool(
                    fixes, initial.tool.parameters, name="repair_quantities"
                )
                instruction = (
                    "Return only the requested R fields using repair_quantities. Keep every other scientific field unchanged.\n"
                    + canonical_json(
                        [
                            {
                                "id": f"R{i}",
                                "path": list(f.path),
                                "instruction": f.reason,
                            }
                            for i, f in enumerate(fixes, 1)
                        ]
                    )
                )
            else:
                request.tool = initial.tool
                instruction = "No valid JSON object could be read. Resubmit using submit_quantities."
            request.messages.append(
                Message(
                    role="user",
                    content=instruction
                    + "\nValidation: "
                    + str(error)
                    + "\n"
                    + _format_details(prior, request.tool)
                    + "\nPrevious output:\n"
                    + prior_text,
                )
            )

    def _retain_valid(self, ctx, source, inputs, draft, step_id, digest, item_id):
        """Preserve complete valid values without repairing or stripping bad errors."""
        if isinstance(draft, dict) and isinstance(draft.get("measurements"), list):
            draft = expand_object_quantity_input(draft, item_id)
        if not isinstance(draft, dict) or len(draft.get("objects", [])) != 1:
            return
        token = object_retry_token(ctx, step_id)
        original = draft["objects"][0]
        common = original.get("coverage", {}).get("common", {})
        checked = common.get("state") in {"complete", "partial"}
        payload = {
            "objects": [
                {
                    "object_id": item_id,
                    "quantities": [],
                    "quantity_conflicts": [],
                    "author_errors": [],
                    "coverage": {
                        "common": {
                            "state": "partial" if checked else "not_checked",
                        },
                        "exceptions": [],
                    },
                }
            ],
            "questions": [],
        }
        obj = payload["objects"][0]

        def raw_scope_matches(diagnostic, value):
            for field in ("condition", "source", "frame", "epoch"):
                expected = diagnostic.get(field)
                if field == "source" and expected not in (
                    "this_paper",
                    "prior_work",
                    "unclear",
                ):
                    continue
                if isinstance(expected, str) and expected != value.get(field):
                    return False
            return True

        def blocked(quantity, value):
            for field in ("author_errors", "quantity_conflicts"):
                diagnostics = original.get(field, [])
                if not isinstance(diagnostics, list):
                    return True
                for diagnostic in diagnostics:
                    if (
                        not isinstance(diagnostic, dict)
                        or diagnostic.get("quantity") not in quantity_paths()
                    ):
                        return True
                    if diagnostic["quantity"] != quantity:
                        continue
                    if field == "author_errors":
                        if raw_scope_matches(diagnostic, value):
                            return True
                    else:
                        alternatives = diagnostic.get("alternatives")
                        if not isinstance(alternatives, list) or not alternatives:
                            return True
                        if any(
                            not isinstance(alt, dict) or raw_scope_matches(alt, value)
                            for alt in alternatives
                        ):
                            return True
            return False

        for group in original.get("quantities", []):
            valid = []
            for value in group.get("values", []):
                # Even malformed diagnostics prevent affected values from leaking
                # through independent partial-value recovery.
                if blocked(group.get("quantity"), value):
                    continue
                candidate = copy.deepcopy(payload)
                candidate["objects"][0]["quantities"] = [
                    {"quantity": group.get("quantity"), "values": [value]}
                ]
                try:
                    self._convert(
                        ctx,
                        source,
                        inputs,
                        candidate,
                        item_id,
                        digest + "-partial",
                        step_id=step_id,
                    )
                except ValueError:
                    continue
                valid.append(value)
            if valid:
                obj["quantities"].append(
                    {"quantity": group["quantity"], "values": valid}
                )
        for conflict in original.get("quantity_conflicts", []):
            candidate = copy.deepcopy(payload)
            candidate["objects"][0]["quantity_conflicts"] = [conflict]
            try:
                self._convert(
                    ctx,
                    source,
                    inputs,
                    candidate,
                    item_id,
                    digest + "-partial",
                    step_id=step_id,
                )
            except ValueError:
                continue
            obj["quantity_conflicts"].append(conflict)
        for diagnostic in original.get("author_errors", []):
            candidate = copy.deepcopy(payload)
            candidate["objects"][0]["author_errors"] = [diagnostic]
            try:
                self._convert(
                    ctx,
                    source,
                    inputs,
                    candidate,
                    item_id,
                    digest + "-partial",
                    step_id=step_id,
                )
            except ValueError:
                continue
            obj["author_errors"].append(diagnostic)
        if not any(
            obj[field]
            for field in ("quantities", "quantity_conflicts", "author_errors")
        ):
            return
        # A later bounded repair can validate additional fields while the whole
        # object still fails. Preserve a new immutable snapshot, rather than
        # reusing the earlier partial merely because the original request is equal.
        partial_digest = sha256_of(
            {"request_digest": digest, "object_retry": token, "payload": payload}
        )
        run = ctx.store.read_record_current(ctx.execution.run_ref.record_id)
        prior = [
            s
            for s in run.data.steps
            if s.step_id.startswith("partial-quantities-")
            and s.step_id.endswith(step_id.removeprefix("quantities-"))
        ]
        for saved in prior:
            if ctx.reuse_step(saved.step_id, partial_digest) is not None:
                return
        partial_step = (
            "partial-quantities-"
            + (f"retry-{token}-" if token else "")
            + "snapshot-"
            + f"{len(prior) + 1:06d}-"
            + partial_digest[:12]
            + "-"
            + step_id.removeprefix("quantities-")
        )
        if ctx.reuse_step(partial_step, partial_digest) is not None:
            return
        records, questions = self._convert(
            ctx,
            source,
            inputs,
            payload,
            item_id,
            partial_digest + "-partial",
            step_id=step_id,
        )
        if questions:
            ReviewService(ctx.space).create_questions(
                execution_id=ctx.execution.execution_id,
                step_id=partial_step
                if ctx.execution.input_snapshot.expert_adjudication
                else "scientific-conflict:" + partial_step,
                request_key=f"{ctx.execution.execution_id}:{partial_step}:questions",
                questions=questions,
                records=records,
                context_question_ids=tuple(q.record_id for q in questions[:-1])
                if ctx.execution.input_snapshot.expert_adjudication
                and len(questions) > 1
                else (),
            )
            committed = []
        else:
            committed = records
        ctx.commit_step(
            step_id=partial_step,
            input_digest=partial_digest,
            records=committed,
            result_refs=[r.ref() for r in [*records, *questions]],
        )

    def _assemble(self, ctx):
        """Publish the explicitly processed object snapshots as one paper result.

        Review targets remain immutable object snapshots; unrelated object saves
        never make a pending decision stale. Only their bound, applied revisions
        contribute to the current aggregate.
        """
        base = self._paper(ctx)
        contributions = {c.item_id: c for c in base.data.object_contributions}
        run = ctx.store.read_record_current(ctx.execution.run_ref.record_id)

        # A completed object always supersedes retained partial snapshots, even
        # when its original step precedes those partial steps in run history.
        def partial_order(step):
            generation = re.search(r"-snapshot-(\d{6})-", step.step_id)
            timestamps = [
                r.created_at.timestamp()
                for ref in step.result_refs
                if isinstance(r := ctx.store.read_record(ref), PaperRecord)
            ]
            return (
                not step.step_id.startswith("partial-"),
                int(generation[1]) if generation else 0,
                max(timestamps, default=0),
            )

        for step in sorted(run.data.steps, key=partial_order):
            if (
                not step.step_id.startswith(("quantities-", "partial-quantities-"))
                or step.state != "finished"
            ):
                continue
            for ref in step.result_refs:
                record = ctx.store.read_record(ref)
                if isinstance(record, PaperRecord):
                    # Only this unit's object, never the other objects copied into its snapshot.
                    target = next(
                        c
                        for c in record.data.object_contributions
                        if step.step_id.endswith(c.roster_item_id)
                    )
                    current = self._applied_record(ctx, ref)
                    approved = next(
                        c
                        for c in current.data.object_contributions
                        if c.item_id == target.item_id
                    )
                    previous = contributions[target.item_id]
                    contributions[target.item_id] = previous.model_copy(
                        update={
                            "quantities": approved.quantities,
                            "quantity_conflicts": approved.quantity_conflicts,
                            "author_errors": approved.author_errors,
                            "quantity_coverage": approved.quantity_coverage,
                            "precedent_uses": approved.precedent_uses,
                        }
                    )
        data = base.data.model_copy(
            update={"object_contributions": list(contributions.values())}
        )
        digest = sha256_of(data.model_dump(mode="json"))
        saved = ctx.reuse_step("assemble", digest)
        if saved:
            return ctx.store.read_record(saved.result_refs[0])
        record_id = f"pdf-{ctx.execution.execution_id}-paper"
        previous = ctx.store.read_record_current(record_id)
        paper = base.model_copy(
            update={
                "record_id": record_id,
                "revision": previous.revision + 1 if previous else 1,
                "created_at": ctx.now(),
                "data": data,
            }
        )
        ctx.commit_step(
            step_id="assemble",
            input_digest=digest,
            records=[paper],
            result_refs=[paper.ref()],
        )
        return paper

    def _finalize(self, ctx, state):
        paper = self._assemble(ctx)
        digest = sha256_of(paper.ref().model_dump(mode="json"))
        if ctx.reuse_step("finalize", digest) is None:
            ctx.commit_step(
                step_id="finalize",
                input_digest=digest,
                records=[],
                result_refs=[paper.ref()],
            )
        return {"result_refs": [paper.ref().model_dump(mode="json")]}


def quantity_repairs(payload, schema, error):
    """Expose invalid numeric forms or their citations, never a complete object."""
    if not isinstance(payload, dict) or "measurements" not in payload:
        return []
    if "非纯区间不能填写区间解释" in error:
        return [
            Repair(
                ("measurements", index, "interval_kind"),
                {"type": "null"},
                "Confirm removal of the inapplicable interval_kind on this submitted point/limit expression. Preserve every printed numeric component, coverage/quantile report, condition and citation.",
                "delete",
            )
            for index, value in enumerate(payload["measurements"])
            if value.get("limit_kind", "none") in {"none", "lower_limit", "upper_limit"}
            and value.get("interval_kind") is not None
        ]
    if "不同条件下的结果应分别保存" in error:
        fixes = []
        # Delete backwards so indices keep referring to the original declarations.
        for index in reversed(range(len(payload.get("quantity_conflicts", [])))):
            conflict = payload["quantity_conflicts"][index]
            alternatives = conflict.get("alternatives", [])
            scopes = {
                (
                    v.get("condition"),
                    v.get("source", "this_paper"),
                    v.get("frame"),
                    v.get("epoch"),
                )
                for v in alternatives
            }
            if len(scopes) <= 1:
                continue
            fixes.append(
                Repair(
                    ("quantity_conflicts", index),
                    {"type": "null"},
                    "The alternatives explicitly declare different scopes. Under the supplied rules these are separate values, not a same-scope conflict. Confirm removing only this invalid container; the following fixed replacements preserve all alternatives verbatim.",
                    "delete",
                )
            )
            for value in alternatives:
                measurement = {"quantity": conflict["quantity"], **copy.deepcopy(value)}
                fixes.append(
                    Repair(
                        ("measurements",),
                        {"const": measurement},
                        "Confirm this unchanged alternative as a separate measurement. Every numeric expression, condition, source, frame, epoch and evidence must remain exactly as submitted; the application appends it without modifying existing measurements.",
                        "append",
                    )
                )
        return fixes
    return []
