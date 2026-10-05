"""Model roster extraction from frozen prepared source packages.

The shared runtime, model-call ledger, submission conversion and review service
own persistence. This flow ends at the roster boundary; quantities stay unchecked.
The bounded format-correction and source-coordinate approach is informed by
Stella HVS (MIT; see llm/STELLA_LICENSE.txt), adapted to current contracts.
"""

from __future__ import annotations

import base64
import copy
import json
import re
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..llm.config import CallRequest, Message
from ..llm.managed import CallFailure, CallService, execute_model_call
from ..llm.prompts import PromptBlueprint, PromptGap, PromptImage, PromptInput
from ..llm.transport import strict_json
from ..records import StepContext, convert_roster_submission
from ..records.common import (
    ArtifactRef,
    RecordRef,
    build_record,
    canonical_json,
    sha256_of,
)
from ..records.paper import PaperRecord
from ..records.questions import QuestionRecord
from ..records.roster import RosterRecord
from ..records.roster_input import bind_roster_input
from ..records.runs import ModelJudgment, RunRecord
from ..records.source import SourcePackageRecord
from ..records.submission import RosterSubmission, SubmissionError
from ..records.validate import validate_collection
from ..review import ReviewService, derive_question_id
from ..review.service import QuestionContent
from ..runtime import DefaultFlowAdapter
from ..runtime.execution import append_run_revision
from .configuration import stored_configuration, validate_configuration_snapshot
from .roster_repair import Repair, apply_repairs, repair_tool, schema_repairs

STEP = "roster"
MAX_FORMAT_CORRECTIONS = 2
MAX_CORRECTION_BYTES = 128 * 1024


class EvidenceLocationError(SubmissionError):
    """A bounded citation-format correction can locate unchanged scientific claims."""


def _prior_submission(prior):
    calls = prior.get("tool_calls") or []
    if len(calls) != 1:
        return None
    try:
        value = strict_json(calls[0]["function"]["arguments"])
    except (ValueError, KeyError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _format_details(prior, tool):
    """Give exact JSON syntax locations without guessing or rewriting arguments."""
    calls = prior.get("tool_calls") or []
    if len(calls) != 1:
        return "Return exactly one call to the supplied tool."
    try:
        strict_json(calls[0]["function"]["arguments"])
    except json.JSONDecodeError as exc:
        return f"JSON syntax error at line {exc.lineno}, column {exc.colno} (character {exc.pos}): {exc.msg}."
    except (KeyError, TypeError):
        return "Tool arguments must be a JSON object."
    except ValueError as exc:
        return "Invalid JSON: " + str(exc)
    return "Follow the supplied tool schema and repair IDs exactly."


def _citation_repairs(payload, inputs, source, space):
    """Locate only failing citations; successful readings remain untouched."""
    from ..records.pdf import pdf_evidence_block

    fixes = []

    def walk(value, path):
        if isinstance(value, dict):
            if set(value) == {"location", "quote"}:
                location = inputs.citation_locations.get(value["location"])
                reason = None
                if location is None:
                    reason = "Select a location supplied in the fixed paper materials."
                else:
                    loc = location["locator"]
                    block = pdf_evidence_block(
                        source,
                        space.root,
                        location["file_id"],
                        loc["page"],
                        loc["block_id"],
                        require_image=loc["kind"] == "pdf_image",
                    )
                    compact = lambda text: re.sub(r"\s+", " ", text).strip()
                    if loc["kind"] == "pdf" and compact(value["quote"]) not in compact(
                        block["text"]
                    ):
                        reason = "The quote is not verbatim at this location. Correct only this citation against the supplied paper; keep its supported scientific claim unchanged."
                if reason:
                    fixes.append(
                        Repair(path, {"$ref": "#/$defs/InlineEvidence"}, reason)
                    )
                return
            for key, child in value.items():
                walk(child, (*path, key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, (*path, index))

    walk(payload, ())
    return fixes


def _gap_repairs(payload, source):
    allowed = {gap.gap_id for gap in source.data.gaps}
    fixes = []

    def walk(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                if (
                    key in {"gap_ids", "boundness_gap_ids"}
                    and not set(child) <= allowed
                ):
                    fixes.append(
                        Repair(
                            (*path, key),
                            {
                                "type": "array",
                                "items": {"type": "string", "enum": sorted(allowed)},
                            },
                            "Use only application-supplied gaps, or [] when none applies. Do not change coverage or scientific judgments.",
                        )
                    )
                else:
                    walk(child, (*path, key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, (*path, index))

    walk(payload, ())
    return fixes


def prepared_bundle(
    space, ref: ArtifactRef, source_ref: RecordRef
) -> tuple[dict, SourcePackageRecord]:
    """Verify the exact preparation bundle and its referenced source revision."""
    bundle = json.loads(space.read_file(ref))
    if RecordRef.model_validate(bundle["prepared_source_ref"]) != source_ref:
        raise ValueError("prepared bundle and fixed source revision disagree")
    source = space.store().read_record(source_ref)
    if not isinstance(source, SourcePackageRecord):
        raise TypeError("prepared source record is missing")
    if bundle.get("body", {}).get("status") != "organized":
        raise ValueError("source materials have no readable organized manuscript")
    if source.execution_id != bundle.get("execution_id"):
        raise ValueError("prepared source belongs to a different preparation execution")
    # Verify every source attachment, including material the model cannot read.
    for file in source.data.files:
        space.read_file(ArtifactRef(path=file.path, sha256=file.sha256))
    return bundle, source


def material_prompt(space, bundle: dict, source: SourcePackageRecord) -> PromptInput:
    """Present fixed PDF blocks without interpreting their object names."""
    index_file = next(f for f in source.data.files if f.role == "pdf_index")
    index = json.loads(
        space.read_file(ArtifactRef(path=index_file.path, sha256=index_file.sha256))
    )
    pdf_id = index["pdf_file_id"]
    if index.get("image_policy") != "original_images_without_generated_descriptions":
        raise ValueError(
            "Prepare this PDF again to remove generated descriptions and include original images"
        )
    files = {file.file_id: file for file in source.data.files}
    images = []
    locations = {}
    blocks = [f"FILE {pdf_id} (role=pdf; pages={index['page_count']})"]
    for block in index["blocks"]:
        text_location = "T:" + block["block_id"]
        locations[text_location] = {
            "file_id": pdf_id,
            "locator": {
                "kind": "pdf",
                "page": block["page"],
                "block_id": block["block_id"],
            },
        }
        blocks.append(
            f"LOCATION {text_location} PAGE {block['page']} BLOCK {block['block_id']} ({block['type']})\n"
            + block["text"]
        )
        for image_id in block.get("image_files", []):
            file = files[image_id]
            if file.role != "pdf_image" or pdf_id not in file.derived_from:
                raise ValueError("Image is not bound to the supplied PDF")
            data = space.read_file(ArtifactRef(path=file.path, sha256=file.sha256))
            image_location = "I:" + block["block_id"]
            locations[image_location] = {
                "file_id": pdf_id,
                "locator": {
                    "kind": "pdf_image",
                    "page": block["page"],
                    "block_id": block["block_id"],
                },
            }
            images.append(
                PromptImage(
                    label=f"LOCATION {image_location} FILE {pdf_id} PAGE {block['page']} BLOCK {block['block_id']} IMAGE {image_id}",
                    image_url={
                        "url": f"data:{file.media_type};base64,"
                        + base64.b64encode(data).decode("ascii")
                    },
                )
            )
    return PromptInput(
        images=images,
        citation_locations=locations,
        manuscript=(
            "The following blocks are parser transcriptions of the fixed PDF. Cite only a supplied location and quote. "
            "For text, use its T: location and quote a verbatim substring of that block. "
            "For an attached image, use its I: location and transcribe relevant printed text you can actually read. "
            "Image transcriptions are model readings, not automatically verified text. Author captions remain text evidence. "
            "Read the attached original figures and tables; parser-generated figure descriptions are omitted. "
            "The application attaches the full block and its PDF bounding box for checking. "
            "Parsed text is not independently verified original evidence; report suspected parsing errors.\n\n"
            + "\n\n".join(blocks)
        ),
        material_gaps=[
            PromptGap(
                gap_id=g.gap_id,
                description=g.description,
                reason=g.reason,
                file_id=g.file_id,
                affected_targets=g.affected_targets,
            )
            for g in source.data.gaps
        ],
    )


def start_roster_extraction(
    space, *, preparation_execution_id: str, request_key: str, configuration=None
):
    """Create a roster execution through the common extraction entry."""
    from ..materials import MaterialsService
    from .configuration import ExtractionConfig
    from .entry import start_extraction

    config = configuration or ExtractionConfig()
    config = ExtractionConfig.model_validate(config.model_dump())
    if config.method != "roster":
        raise ValueError(
            "model roster execution currently supports the roster flow only"
        )
    bundle = MaterialsService(space).get_prepared_input(preparation_execution_id)
    source_ref = RecordRef.model_validate(bundle["prepared_source_ref"])
    ref = ArtifactRef.model_validate(bundle["bundle_ref"])
    return start_extraction(
        space,
        request=config.request([source_ref]),
        request_key=request_key,
        configuration=config,
        prepared_input=ref,
    )


def _hydrate_sources(
    payload: dict, space, source: SourcePackageRecord, supplied_file_ids: set[str]
) -> dict:
    """Bind text excerpts to full blocks; preserve image readings as image evidence.

    Scientific fields are unchanged. Short verbatim excerpts become the complete
    block evidence required by saved records; image text is not string-verified.
    """
    result = copy.deepcopy(payload)
    files = {f.file_id: f for f in source.data.files}
    problems = set()

    def walk(value):
        if isinstance(value, dict):
            if {"file_id", "locator", "excerpt"} <= value.keys():
                fid = value["file_id"]
                if (
                    fid not in supplied_file_ids
                    or fid not in files
                    or files[fid].role == "derived_mapping"
                ):
                    raise SubmissionError(
                        "evidence refers to material not supplied as paper evidence"
                    )
                loc = value["locator"]
                if loc["kind"] not in {"pdf", "pdf_image"}:
                    problems.add(
                        "Use pdf or pdf_image page/block locators for every citation."
                    )
                    return
                from ..records.pdf import pdf_evidence_block

                try:
                    block = pdf_evidence_block(
                        source,
                        space.root,
                        fid,
                        loc["page"],
                        loc["block_id"],
                        require_image=loc["kind"] == "pdf_image",
                    )
                except ValueError as exc:
                    problems.add(f"{fid} {loc}: {exc}")
                    return
                if loc["kind"] == "pdf_image":
                    return
                actual = block["text"]
                compact = lambda text: re.sub(r"\s+", " ", text).strip()
                if not value["excerpt"] or compact(value["excerpt"]) not in compact(
                    actual
                ):
                    problems.add(
                        f"{fid} page {loc['page']} block {loc['block_id']}: excerpt is not verbatim at this block. Quote starts: {value['excerpt'][:100]!r}"
                    )
                value["excerpt"] = actual
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(result)
    if problems:
        raise EvidenceLocationError("\n".join(sorted(problems))[:6000])
    return result


def _question_content(question):
    d = question.data
    return QuestionContent(
        category=d.category,
        target_refs=d.target_refs,
        source_refs=d.source_refs,
        rules_ref=d.rules_ref,
        prompt=d.prompt,
        evidence_refs=d.evidence_refs,
        gap_refs=d.gap_refs,
        context_refs=d.context_refs,
    )


def _replace_question_refs(record, replacements):
    def walk(value):
        if isinstance(value, dict):
            return {
                k: replacements.get(v, v)
                if k == "record_id" and isinstance(v, str)
                else walk(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    return type(record).model_validate(
        walk(record.model_dump(mode="json", by_alias=True))
    )


class RosterState(TypedDict, total=False):
    execution_id: str
    result_refs: list[dict]


class RosterFlow(DefaultFlowAdapter):
    """One roster node with fixed input and durable correction calls."""

    flow_version = "1"

    def __init__(self, *, mode):
        self.mode = mode
        self.flow_id = f"hvs-roster-{mode}"

    def initial_state(self, ctx):
        return {"execution_id": ctx.execution.execution_id, "result_refs": []}

    def build_graph(self, ctx):
        config = validate_configuration_snapshot(ctx.execution.input_snapshot)
        if config is None or config.method != "roster":
            raise ValueError("roster requires a frozen roster configuration")
        if (
            not isinstance(ctx.service, CallService)
            or ctx.service.runtime != config.runtime
        ):
            raise ValueError("model call service does not match the frozen runtime")
        if ctx.service.adapter.name != config.model_for(STEP).adapter:
            raise ValueError("model adapter does not match the fixed configuration")
        ctx.service.adapter.validate_runtime(config.runtime)
        self._inputs(ctx)
        builder = StateGraph(RosterState)
        builder.add_node(STEP, lambda state: self._extract(ctx, state))
        builder.add_edge(START, STEP)
        builder.add_edge(STEP, END)
        return builder.compile(checkpointer=ctx.checkpointer)

    def _inputs(self, ctx):
        snapshot = ctx.execution.input_snapshot
        ref = ArtifactRef.model_validate_json(snapshot.options["prepared_input"])
        _, source = prepared_bundle(ctx.space, ref, snapshot.source_refs[0])
        inputs = PromptInput.model_validate_json(
            ctx.space.read_file(
                ArtifactRef.model_validate_json(snapshot.options["prepared_prompt"])
            )
        )
        blueprint = PromptBlueprint.model_validate(
            json.loads(ctx.space.read_file(snapshot.prompt_refs[0]))[STEP]
        )
        config = stored_configuration(snapshot)
        return source, inputs, blueprint.request(config.model_for(STEP), inputs)

    def _records(self, ctx, source, inputs, payload):
        prefix = f"roster-{ctx.execution.execution_id}"
        roster_ref = RecordRef(record_id=prefix + "-roster", revision=1)
        paper_ref = RecordRef(record_id=prefix + "-paper", revision=1)
        previous = ctx.store.read_record(roster_ref)
        created_at = previous.created_at if previous is not None else ctx.now()
        context = StepContext(
            source=source,
            paper_ref=paper_ref,
            created_at=created_at,
            id_prefix=prefix,
            execution_id=ctx.execution.execution_id,
            rules_ref=ctx.execution.input_snapshot.rules,
        )
        supplied = {
            line.split(" (role=", 1)[0][5:]
            for line in inputs.manuscript.splitlines()
            if line.startswith("FILE ")
        }
        hydrated = _hydrate_sources(
            bind_roster_input(payload, inputs.citation_locations),
            ctx.space,
            source,
            supplied,
        )
        if not set(hydrated["coverage"]["checked_file_ids"]) <= supplied:
            raise SubmissionError(
                "roster coverage claims a file that was not supplied to the model"
            )
        if (
            hydrated["coverage"]["state"] == "complete"
            and hydrated["unresolved_groups"]
        ):
            # Completion bookkeeping cannot contradict an explicitly saved unresolved group.
            hydrated["coverage"]["state"] = "partial"
        for entry in hydrated["entries"]:
            fields = entry.get("contribution")
            if (
                fields
                and fields["contribution_type"]["value"] == "candidates_found"
                and fields["boundness"].get("status") == "bound"
            ):
                raise SubmissionError(
                    "an explicitly bound object cannot be included as candidates_found"
                )
        if not ctx.execution.input_snapshot.expert_adjudication:
            for entry in hydrated["entries"]:
                fields = entry.get("contribution")
                if entry.get("question") or (
                    fields and fields["boundness"].get("question")
                ):
                    raise SubmissionError(
                        "Automatic extraction must form a model judgment, not an expert question"
                    )
                if entry["disposition"] == "pending" and not entry.get("gap_ids"):
                    raise SubmissionError(
                        "Only missing material may leave an automatic roster entry pending"
                    )
        conversion = convert_roster_submission(
            context, RosterSubmission.model_validate(hydrated)
        )
        roster = build_record(
            RosterRecord,
            record_id=roster_ref.record_id,
            created_at=created_at,
            execution_id=ctx.execution.execution_id,
            data=conversion.roster,
        )
        paper = build_record(
            PaperRecord,
            record_id=paper_ref.record_id,
            created_at=created_at,
            execution_id=ctx.execution.execution_id,
            data={
                "paper_key": source.data.paper_key,
                "metadata": {
                    "arxiv_id": getattr(source.data.origin, "arxiv_id", None),
                    "version": source.data.source_version,
                    "title": None,
                    "authors": [],
                },
                "source_ref": source.ref(),
                "roster_ref": roster.ref(),
                "object_contributions": conversion.contributions,
            },
        )
        replacements = {
            q.record_id: derive_question_id(
                ctx.execution.execution_id,
                _question_content(q),
                space_id=ctx.space.identity.space_id,
            )
            for q in conversion.questions
        }
        records = [
            _replace_question_refs(r, replacements)
            for r in [roster, paper, *conversion.evidence]
        ]
        questions = [
            _replace_question_refs(q, replacements) for q in conversion.questions
        ]
        validate_collection(
            [source, *records, *questions], materials_root=ctx.space.root
        )
        return records, questions

    def _extract(self, ctx, state):
        source, inputs, initial = self._inputs(ctx)
        digest = sha256_of(initial.model_dump(mode="json"))
        saved = ctx.reuse_step(STEP, digest)
        if saved is None:
            request = CallRequest.model_validate(initial.model_dump())
            draft = None
            fixes = []
            for round_number in range(MAX_FORMAT_CORRECTIONS + 1):
                prepared = ctx.service.adapter.prepare(request)
                if round_number:
                    # A saved correction is immutable even when validation diagnostics
                    # improve. Reuse it only within the same frozen original request.
                    run = ctx.store.read_record_current(ctx.execution.run_ref.record_id)
                    for step in run.data.steps:
                        if step.step_id != STEP:
                            continue
                        for attempt in step.attempts:
                            if attempt.request and Path(
                                attempt.request.path
                            ).name.startswith(f"roster-{round_number:02d}-"):
                                archived = json.loads(
                                    ctx.space.read_file(attempt.request)
                                )
                                frozen, rebuilt = (
                                    copy.deepcopy(archived),
                                    copy.deepcopy(prepared),
                                )
                                frozen["payload"]["messages"].pop()
                                rebuilt["payload"]["messages"].pop()
                                if frozen != rebuilt:
                                    raise CallFailure(
                                        "archived correction belongs to different fixed input"
                                    )
                                prepared = archived
                                # Continue from the exact conversation already sent,
                                # including feedback from every earlier correction.
                                request.messages = [
                                    Message.model_validate(message)
                                    for message in archived["payload"]["messages"]
                                ]
                envelope = execute_model_call(
                    ctx,
                    ctx.service,
                    request,
                    prepared,
                    step_id=STEP,
                    call_key=f"roster-{round_number:02d}",
                )
                error = envelope.get("error")
                body = envelope.get("exchange", {}).get("body") or {}
                prior = body.get("choices", [{}])[0].get("message", {})
                prior = {
                    k: v for k, v in prior.items() if k in {"content", "tool_calls"}
                }
                proposed = _prior_submission(prior)
                if request.tool.name == "repair_roster":
                    if error is None:
                        draft = apply_repairs(draft, fixes, proposed)
                elif (
                    proposed is not None
                    and prior["tool_calls"][0]["function"].get("name")
                    == initial.tool.name
                ):
                    draft = proposed
                if draft is not None and (
                    request.tool.name != "repair_roster" or error is None
                ):
                    fixes = schema_repairs(draft, initial.tool.parameters)
                    if not fixes:
                        fixes = _citation_repairs(draft, inputs, source, ctx.space)
                    error = (
                        "Submission has fields requiring correction." if fixes else None
                    )
                payload = draft
                if error is None:
                    try:
                        records, questions = self._records(ctx, source, inputs, payload)
                    except EvidenceLocationError as exc:
                        error = "PDF citation validation: " + str(exc)
                    except SubmissionError as exc:
                        if "输入中不存在的材料缺口" in str(exc):
                            fixes = _gap_repairs(payload, source)
                            error = "Use only the supplied gap identifiers."
                        elif "未列清范围的已明确成员" in str(exc):
                            identifiers = {
                                i["value"]
                                for entry in payload["entries"]
                                for i in entry["identifiers"]
                            }
                            missing = sorted(
                                {
                                    name
                                    for group in payload["unresolved_groups"]
                                    for name in group["member_identifier_values"]
                                    if name not in identifiers
                                }
                            )
                            entry_schema = {"$ref": "#/$defs/RosterEntrySubmission"}
                            fixes = [
                                Repair(
                                    ("entries",),
                                    {
                                        "allOf": [
                                            entry_schema,
                                            {
                                                "properties": {
                                                    "identifiers": {
                                                        "contains": {
                                                            "type": "object",
                                                            "properties": {
                                                                "value": {"const": name}
                                                            },
                                                            "required": ["value"],
                                                        }
                                                    }
                                                }
                                            },
                                        ]
                                    },
                                    f"Append the omitted entry for already named member {name!r}. Existing entries remain unchanged.",
                                    "append",
                                )
                                for name in missing
                            ]
                            error = "Named members require their own entries."
                        else:
                            raise
                if error is None:
                    if questions:
                        ReviewService(ctx.space).create_questions(
                            execution_id=ctx.execution.execution_id,
                            step_id=STEP,
                            request_key=f"{ctx.execution.execution_id}:roster-questions",
                            questions=questions,
                            records=records,
                        )
                        committed = []
                    else:
                        committed = records
                    refs = [r.ref() for r in [*records, *questions]]
                    ctx.commit_step(
                        step_id=STEP,
                        input_digest=digest,
                        records=committed,
                        result_refs=refs,
                    )
                    saved = ctx.reuse_step(STEP, digest)
                    break
                if round_number == MAX_FORMAT_CORRECTIONS:
                    raise CallFailure("roster format correction exhausted")
                prior_text = canonical_json(prior)
                if len(prior_text.encode("utf-8")) > MAX_CORRECTION_BYTES:
                    raise CallFailure(
                        "previous output exceeds the bounded correction input; inspect the archived output"
                    )
                if draft is not None:
                    if not fixes:
                        raise CallFailure(
                            "Submission violates a constraint without a safe local format repair: "
                            + str(error)
                        )
                    request.tool = repair_tool(fixes, initial.tool.parameters)
                    details = [
                        {
                            "id": f"R{i}",
                            "path": list(fix.path),
                            "instruction": fix.reason,
                        }
                        for i, fix in enumerate(fixes, 1)
                    ]
                    instruction = (
                        "Use repair_roster to return only the requested R fields. The application retains every other field unchanged. Repairs do not authorize revising scientific judgments, names, conditions or membership.\nRequested repairs: "
                        + canonical_json(details)
                    )
                else:
                    request.tool = initial.tool
                    instruction = "No valid JSON object could be read. Resubmit using the supplied submit_roster schema. "
                request.messages.append(
                    Message(
                        role="user",
                        content=instruction
                        + "\nValidation details: "
                        + str(error)
                        + "\n"
                        + _format_details(prior, request.tool)
                        + "\nPrevious output:\n"
                        + prior_text,
                    )
                )
        refs = list(saved.result_refs)
        if not ctx.execution.input_snapshot.expert_adjudication:
            # Retain the actual model submission as model work, including any
            # unresolved questions. Links preserve its interpretation and evidence
            # without copying a large response or creating an expert decision.
            run = ctx.store.read_record_current(ctx.execution.run_ref.record_id)
            if not isinstance(run, RunRecord):
                raise TypeError("roster run record is missing")
            step = next(s for s in run.data.steps if s.step_id == STEP)
            judgment = canonical_json(
                {
                    "kind": "roster_model_submission",
                    "response_ref": step.attempts[-1].response.model_dump(mode="json"),
                    "result_refs": [r.model_dump(mode="json") for r in refs],
                }
            )
            if not any(
                j.step_id == STEP and j.judgment == judgment
                for j in run.data.model_judgments
            ):
                append_run_revision(
                    ctx.space,
                    ctx.execution,
                    model_judgment=ModelJudgment(
                        step_id=STEP, judgment=judgment, created_at=ctx.now()
                    ),
                )
        if ctx.execution.input_snapshot.expert_adjudication:
            review = ReviewService(ctx.space)
            for ref in refs:
                if isinstance(ctx.store.read_record(ref), QuestionRecord):
                    while True:
                        view = review.get_question(ref)
                        if view.application and view.application.phase == "applied":
                            break
                        interrupt(
                            {
                                "question_ref": ref.model_dump(mode="json"),
                                "step_id": STEP,
                                "prompt": view.record.data.prompt,
                            }
                        )
        return {"result_refs": [r.model_dump(mode="json") for r in refs]}

    def apply_decision(self, ctx, decision, question):
        from .flow import ExtractionFlow

        return ExtractionFlow.apply_decision(self, ctx, decision, question)

    def waiting_step(self, payload):
        return payload.get("step_id")

    def verify_state(self, ctx, state):
        return (
            []
            if state.get("execution_id") == ctx.execution.execution_id
            else ["roster checkpoint belongs to a different execution"]
        )
