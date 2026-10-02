"""Direct model roster extraction from frozen prepared source packages.

The shared runtime, model-call ledger, submission conversion and review service
own persistence. This flow ends at the roster boundary; quantities stay unchecked.
The bounded format-correction and source-coordinate approach is informed by
Stella HVS (MIT; see llm/STELLA_LICENSE.txt), adapted to current contracts.
"""

from __future__ import annotations

import copy
import json
from typing import TypedDict

from jsonschema import Draft202012Validator
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from ..llm.config import CallRequest, Message
from ..llm.managed import CallFailure, CallService, execute_model_call
from ..llm.prompts import PromptBlueprint, PromptGap, PromptInput
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
from ..records.runs import ModelJudgment, RunRecord
from ..records.source import SourcePackageRecord
from ..records.submission import RosterSubmission, SubmissionError
from ..records.validate import validate_collection
from ..review import ReviewService, derive_question_id
from ..review.service import QuestionContent
from ..runtime import DefaultFlowAdapter
from ..runtime.execution import append_run_revision
from .configuration import stored_configuration, validate_configuration_snapshot

STEP = "direct_roster"
MAX_FORMAT_CORRECTIONS = 2
MAX_CORRECTION_BYTES = 128 * 1024


def _science_values(payload):
    """Keep supplied contract fields, even when the submission is incomplete.

    Correction may add omitted fields or remove unknown fields. It cannot replace
    a supplied judgment, object, evidence coordinate or coverage claim. Invalid
    JSON provides no safely recoverable structured baseline.
    """
    schema = RosterSubmission.model_json_schema()
    values = {}

    def walk(value, spec, path):
        while "$ref" in spec:
            spec = schema["$defs"][spec["$ref"].rsplit("/", 1)[1]]
        if "anyOf" in spec and value is not None:
            spec = next(s for s in spec["anyOf"] if s.get("type") != "null")
            return walk(value, spec, path)
        if isinstance(value, dict) and "properties" in spec:
            # A misplaced source wrapper is a format error, but its already
            # supplied source facts must also survive correction unchanged.
            if "source" in spec["properties"] and "source" not in value:
                misplaced = {
                    k: value[k] for k in ("file_id", "locator", "excerpt") if k in value
                }
                if misplaced:
                    walk(misplaced, spec["properties"]["source"], (*path, "source"))
            for key, child in spec["properties"].items():
                if key in value:
                    walk(value[key], child, (*path, key))
        elif isinstance(value, list) and "items" in spec:
            values[(*path, "length")] = len(value)
            for index, item in enumerate(value):
                walk(item, spec["items"], (*path, index))
        else:
            values[path] = value

    walk(payload, schema, ())
    return values


def _prior_submission(prior):
    calls = prior.get("tool_calls") or []
    if len(calls) != 1:
        return None
    try:
        value = json.loads(calls[0]["function"]["arguments"])
    except (ValueError, KeyError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _format_details(prior, tool):
    """Bounded English schema diagnostics without dumping scientific values."""
    payload = _prior_submission(prior)
    if payload is None:
        return "Arguments must be one valid JSON object in the requested tool call."
    details = []

    def bounded():
        return "\n".join(details).encode()[:4096].decode("utf-8", errors="ignore")

    def leaves(error):
        if error.context:
            for child in error.context:
                yield from leaves(child)
        else:
            yield error

    for error in Draft202012Validator(tool.parameters).iter_errors(payload):
        for child in leaves(error):
            path = ".".join(str(p) for p in child.absolute_path) or "root"
            hint = child.validator
            if hint == "required":
                hint += "; required fields: " + ", ".join(child.validator_value)
            elif hint == "additionalProperties" and isinstance(child.instance, dict):
                unknown = set(child.instance) - set(child.schema.get("properties", {}))
                hint += "; unknown fields: " + ", ".join(sorted(unknown))
            detail = path + ": " + hint
            if detail not in details:
                details.append(detail)
            if len(details) >= 24:
                return bounded()
    return bounded()


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
    """Present referenced manuscript files, attached text, tables and mappings.

    Raw packages, images, diagnostic logs and unreferenced TeX drafts are not
    decoded as manuscript. Conversion views keep their declared original links.
    """
    body_names = {f["name"] for f in bundle["body"].get("files", [])}
    utf8_members = {
        f.derived_from[0] for f in source.data.files if f.file_id.startswith("utf8:")
    }
    blocks = []
    for file in source.data.files:
        if file.read_state != "ready" or file.role not in {
            "member",
            "derived_text",
            "derived_table",
            "derived_mapping",
        }:
            continue
        if file.file_id == "body-view" or file.file_id in utf8_members:
            continue
        name = file.file_id.removeprefix("member:").removeprefix("utf8:")
        if name.lower().endswith(".tex") and name not in body_names:
            continue
        if not (
            file.media_type.startswith("text/")
            or file.media_type in {"application/json", "application/x-latex"}
        ):
            continue
        text = space.read_file(ArtifactRef(path=file.path, sha256=file.sha256)).decode(
            "utf-8"
        )
        blocks.append(
            f"FILE {file.file_id} (role={file.role}; derived_from={','.join(file.derived_from)})\n"
            + "\n".join(f"{i}|{line}" for i, line in enumerate(text.splitlines(), 1))
        )
    if not blocks:
        raise ValueError("prepared source has no supported readable model material")
    gap_messages = {
        "missing": "Referenced package material is missing.",
        "unreadable": "Package material could not be read with a confirmed encoding.",
        "unparsed": "Package material has not been parsed completely; inspect available original material and retain the affected scope as incomplete.",
        "external_not_included": "Referenced external material is outside the supplied package and has not been processed.",
    }
    gaps = []
    for gap in source.data.gaps:
        # Preparation diagnostics are operator-facing Chinese. Model instructions
        # use deterministic English facts from the reason, identity and scope;
        # the complete original diagnostic remains in the frozen source record.
        description = gap_messages[gap.reason]
        if gap.gap_id == "gap-conversion-context":
            description = "Conversion of the complete author context could not establish completeness. Check supplied original source; unresolved scope remains incomplete."
        gaps.append(
            PromptGap(
                gap_id=gap.gap_id,
                description=description,
                reason=gap.reason,
                file_id=gap.file_id,
                affected_targets=gap.affected_targets,
            )
        )
    ranges = [
        {
            k: v
            for k, v in item.items()
            if k
            in {
                "notation",
                "file",
                "start_line",
                "end_line",
                "state",
                "status",
                "identifiers",
                "object_context",
                "members",
                "remainder",
                "source_ref",
                "evidence",
            }
        }
        for item in bundle.get("ranges", [])
    ]
    return PromptInput(
        manuscript='SOURCE COORDINATES: Each numbered line below is a physical source-file line. Every evidence item has this nested structure: {"source":{"file_id":"...","locator":{"kind":"text","start_line":1,"end_line":1},"excerpt":"..."},"parts":[],"context":true}. For text evidence, copy a contiguous verbatim substring from the stated line range, keeping original TeX syntax and whitespace. Do not insert ellipses, join separated fragments, paraphrase, or quote rendered prose instead of source. The program will attach the complete source lines.\n\n'
        + "\n\n".join(blocks)
        + "\n\nPREPARED RANGE OBSERVATIONS (parser suggestions, not author statements; unsupported observations may be single printed identifiers; check original evidence):\n"
        + canonical_json(ranges),
        material_gaps=gaps,
    )


def start_roster_extraction(
    space, *, preparation_execution_id: str, request_key: str, configuration=None
):
    """Create a direct roster execution through the common extraction entry."""
    from ..materials import MaterialsService
    from .configuration import ExtractionConfig
    from .entry import start_extraction

    config = configuration or ExtractionConfig()
    config = ExtractionConfig.model_validate(config.model_dump())
    if config.method != "direct":
        raise ValueError("model roster execution currently supports direct only")
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
    """Copy full source lines only after the submitted excerpt matches those lines.

    Scientific fields are unchanged. Short verbatim excerpts become the complete
    physical-line evidence required by saved records; invented excerpts fail.
    """
    result = copy.deepcopy(payload)
    files = {f.file_id: f for f in source.data.files}

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
                if loc["kind"] == "text":
                    file = files[fid]
                    lines = (
                        space.read_file(ArtifactRef(path=file.path, sha256=file.sha256))
                        .decode("utf-8")
                        .splitlines()
                    )
                    start, stop = loc["start_line"], loc["end_line"]
                    if not 1 <= start <= stop <= len(lines):
                        raise SubmissionError(
                            "evidence line coordinates are outside the fixed file"
                        )
                    actual = "\n".join(lines[start - 1 : stop])
                    if not value["excerpt"] or value["excerpt"] not in actual:
                        raise SubmissionError(
                            "evidence excerpt does not occur verbatim at the submitted coordinates"
                        )
                    value["excerpt"] = actual
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(result)
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


class DirectRosterFlow(DefaultFlowAdapter):
    """One roster node with fixed input and durable correction calls."""

    flow_version = "1"

    def __init__(self, *, mode):
        self.mode = mode
        self.flow_id = f"hvs-direct-roster-{mode}"

    def initial_state(self, ctx):
        return {"execution_id": ctx.execution.execution_id, "result_refs": []}

    def build_graph(self, ctx):
        config = validate_configuration_snapshot(ctx.execution.input_snapshot)
        if config is None or config.method != "direct":
            raise ValueError("direct roster requires a frozen direct configuration")
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
        prefix = f"direct-{ctx.execution.execution_id}"
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
        hydrated = _hydrate_sources(payload, ctx.space, source, supplied)
        if not set(hydrated["coverage"]["checked_file_ids"]) <= supplied:
            raise SubmissionError(
                "roster coverage claims a file that was not supplied to the model"
            )
        if (
            hydrated["coverage"]["state"] == "complete"
            and hydrated["unresolved_groups"]
        ):
            raise SubmissionError(
                "an unresolved membership group cannot be declared a complete roster"
            )
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
            request = initial
            baselines = []
            for round_number in range(MAX_FORMAT_CORRECTIONS + 1):
                prepared = ctx.service.adapter.prepare(request)
                envelope = execute_model_call(
                    ctx,
                    ctx.service,
                    request,
                    prepared,
                    step_id=STEP,
                    call_key=f"roster-{round_number:02d}",
                )
                error = envelope.get("error")
                if error is None:
                    payload = envelope["result"]["parsed"]
                    current = _science_values(payload)
                    if any(
                        current.get(path) != value or path not in current
                        for baseline in baselines
                        for path, value in baseline.items()
                    ):
                        raise SubmissionError(
                            "format correction changed a supplied scientific field; inspect the archived submissions"
                        )
                    try:
                        # Pydantic semantic constraints supplement the tool JSON Schema.
                        RosterSubmission.model_validate(payload)
                    except ValidationError as exc:
                        error = "submission_constraints_failed: " + "; ".join(
                            ".".join(str(p) for p in item["loc"])
                            + " ("
                            + item["type"]
                            + ")"
                            for item in exc.errors(include_input=False)
                        )
                if error is None:
                    records, questions = self._records(ctx, source, inputs, payload)
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
                request = CallRequest.model_validate(initial.model_dump())
                body = envelope.get("exchange", {}).get("body") or {}
                prior = body.get("choices", [{}])[0].get("message", {})
                prior = {
                    k: v for k, v in prior.items() if k in {"content", "tool_calls"}
                }
                prior_payload = _prior_submission(prior)
                if prior_payload is not None:
                    baselines.append(_science_values(prior_payload))
                prior_text = canonical_json(prior)
                if len(prior_text.encode("utf-8")) > MAX_CORRECTION_BYTES:
                    raise CallFailure(
                        "previous output exceeds the bounded format-correction input; inspect the archived output"
                    )
                request.messages.append(
                    Message(
                        role="user",
                        content="The previous submission failed format validation ("
                        + error
                        + "). Correct only the requested output format and field constraints. Preserve all scientific judgments, identifiers and evidence; do not resolve scientific uncertainty to satisfy the schema. Return exactly one requested tool call. Previous output:\n"
                        + prior_text
                        + "\nValidation details:\n"
                        + _format_details(prior, initial.tool),
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
