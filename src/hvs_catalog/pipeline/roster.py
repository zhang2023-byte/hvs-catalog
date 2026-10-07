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

from jsonschema import Draft202012Validator
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
from ..records.file_checks import scoped_verification
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
from .network_retry import format_correction_limit, retry_call_key
from .roster_repair import (
    Repair,
    apply_repairs,
    merge_repairs,
    repair_tool,
    roster_invariant_repairs,
    schema_repairs,
)

STEP = "roster"
PARTIAL_STEP = "partial-roster"
MAX_FORMAT_CORRECTIONS = 2


class EvidenceLocationError(SubmissionError):
    """A bounded citation-format correction can locate unchanged scientific claims."""


def _prior_submission(prior):
    calls = prior.get("tool_calls") or []
    if len(calls) != 1:
        return None
    try:
        arguments = calls[0]["function"]["arguments"]
        try:
            value = strict_json(arguments)
        except json.JSONDecodeError:
            # Only discard one surplus root brace after a complete object. The
            # prefix is rechecked strictly: duplicate keys/nonfinite values fail.
            text = arguments.lstrip()
            value, end = json.JSONDecoder().raw_decode(text)
            if not isinstance(value, dict) or text[end:].strip() != "}":
                return None
            value = strict_json(text[:end])
    except (ValueError, KeyError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _argument_cleanup(prior):
    calls = prior.get("tool_calls") or []
    if len(calls) != 1 or _prior_submission(prior) is None:
        return None
    try:
        strict_json(calls[0]["function"]["arguments"])
    except json.JSONDecodeError:
        return (
            "Application discarded one surplus root closing brace after one complete strict JSON object. "
            "The raw response remains archived; no scientific fields were rewritten."
        )
    return None


def _save_argument_cleanup(ctx, prior, step_id, call_key):
    cleanup = _argument_cleanup(prior)
    if cleanup:
        ref = ctx.space.publish_run_file(
            ctx.execution.execution_id,
            f"outputs/format-cleanups/{call_key}.json",
            canonical_json(
                {
                    "step_id": step_id,
                    "call_key": call_key,
                    "application_repair": cleanup,
                    "raw_arguments_sha256": sha256_of(
                        prior["tool_calls"][0]["function"]["arguments"]
                    ),
                    "parsed_payload_sha256": sha256_of(_prior_submission(prior)),
                }
            ).encode(),
        )
        append_run_revision(ctx.space, ctx.execution, artifacts=[ref])


@scoped_verification
def _clean_citations(ctx, payload, inputs, source, step_id, call_key):
    """Resolve annotated IDs and redundant escaping only against fixed material."""
    from ..records.pdf import pdf_evidence_block

    result = copy.deepcopy(payload)
    changes = []
    compact = lambda text: re.sub(r"\s+", " ", text).strip()

    def walk(value, path):
        if isinstance(value, dict):
            if {"location", "quote"} <= value.keys():
                if not all(isinstance(value[k], str) for k in ("location", "quote")):
                    return
                original = copy.deepcopy(value)
                location = value["location"]
                if location not in inputs.citation_locations:
                    match = re.fullmatch(r"([^\s()]+)\s+\([^\n]*\)", location)
                    if match and match[1] in inputs.citation_locations:
                        value["location"] = match[1]
                supplied = inputs.citation_locations.get(value["location"])
                if supplied and supplied["locator"]["kind"] == "pdf":
                    loc = supplied["locator"]
                    block = pdf_evidence_block(
                        source,
                        ctx.space.root,
                        supplied["file_id"],
                        loc["page"],
                        loc["block_id"],
                    )
                    quote = value["quote"]
                    unescaped = quote.replace("\\\\", "\\")
                    if compact(quote) not in compact(block["text"]) and compact(
                        unescaped
                    ) in compact(block["text"]):
                        value["quote"] = unescaped
                if value != original:
                    changes.append(
                        {
                            "path": list(path),
                            "before": original,
                            "after": copy.deepcopy(value),
                        }
                    )
                return
            for key, child in value.items():
                walk(child, (*path, key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, (*path, index))

    walk(result, ())
    if changes:
        ref = ctx.space.publish_run_file(
            ctx.execution.execution_id,
            f"outputs/format-cleanups/{call_key}-citations.json",
            canonical_json(
                {
                    "step_id": step_id,
                    "call_key": call_key,
                    "application_repair": "Known location IDs with appended annotations and redundant backslash escaping verified against the exact fixed text only.",
                    "changes": changes,
                }
            ).encode(),
        )
        append_run_revision(ctx.space, ctx.execution, artifacts=[ref])
    return result


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


@scoped_verification
def _citation_repairs(payload, inputs, source, space):
    """Locate only failing citations; successful readings remain untouched."""
    from ..records.pdf import pdf_evidence_block

    fixes = []

    def walk(value, path):
        if isinstance(value, dict):
            if set(value) == {"location", "quote"}:
                if not all(isinstance(value[k], str) for k in ("location", "quote")):
                    return  # Schema repair owns malformed citation components.
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
                    quote = value["quote"]
                    if loc["kind"] == "pdf" and not any(
                        compact(q) in compact(block["text"])
                        for q in (quote, quote.replace("\\\\", "\\"))
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
                    and isinstance(child, list)
                    and all(isinstance(g, str) for g in child)
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
        if block["text"]:
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
            "Copy the bare location ID exactly (for example T:p3-b11 or I:p3-b11); put no descriptions or annotations in the location string. "
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
    space,
    *,
    preparation_execution_id: str,
    request_key: str,
    configuration=None,
    full=False,
    roster_execution_id=None,
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
        extraction_scope="full" if full else "roster",
        roster_execution_id=roster_execution_id,
    )


@scoped_verification
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
    roster_error: str


def roster_result_refs(space, execution_id):
    """Read the exact committed full roster, or its latest validated partial."""
    from ..runtime import get_execution

    handle = get_execution(space, execution_id)
    run = space.store().read_record(handle.run_ref)
    for step_id in (STEP, PARTIAL_STEP):
        step = next((s for s in run.data.steps if s.step_id == step_id), None)
        if step is not None and step.state == "finished":
            return list(step.result_refs)
    return []


def saved_roster(space, execution_id):
    """Select the roster from exact execution-bound results, including partials."""
    return next(
        (
            record
            for ref in roster_result_refs(space, execution_id)
            if isinstance(record := space.store().read_record(ref), RosterRecord)
        ),
        None,
    )


class RosterFlow(DefaultFlowAdapter):
    """One roster node with fixed input and durable correction calls."""

    flow_version = "1"
    terminal_failures = True

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

    def _records(
        self, ctx, source, inputs, payload, *, partial_digest=None, entry_indices=None
    ):
        prefix = f"roster-{ctx.execution.execution_id}"
        scientific_prefix = (
            prefix + "-partial-" + partial_digest[:20] if partial_digest else prefix
        )
        previous = ctx.store.read_record_current(scientific_prefix + "-roster")
        previous_paper = ctx.store.read_record_current(scientific_prefix + "-paper")
        roster_ref = RecordRef(
            record_id=scientific_prefix + "-roster",
            revision=previous.revision + 1 if previous else 1,
        )
        paper_ref = RecordRef(
            record_id=scientific_prefix + "-paper",
            revision=previous_paper.revision + 1 if previous_paper else 1,
        )
        created_at = previous.created_at if previous is not None else ctx.now()
        context = StepContext(
            source=source,
            paper_ref=paper_ref,
            created_at=created_at,
            id_prefix=scientific_prefix,
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
                if (
                    entry["disposition"] == "pending"
                    and not entry.get("gap_ids")
                    and not entry.get("author_errors")
                ):
                    raise SubmissionError(
                        "A blocked automatic roster entry requires missing material or source-error diagnostics"
                    )
        conversion = convert_roster_submission(
            context, RosterSubmission.model_validate(hydrated)
        )
        run = ctx.store.read_record_current(ctx.execution.run_ref.record_id)
        if partial_digest or any(s.step_id == PARTIAL_STEP for s in run.data.steps):
            # Positions refer to the unchanged raw submission, including failed
            # entries. Later bounded continuation cannot renumber saved objects.
            indices = entry_indices or list(range(len(payload["entries"])))
            mapping = {}
            for item, index in zip(conversion.roster.entries, indices, strict=True):
                mapping[item.item_id] = f"{prefix}-roster-{index + 1}"
                if item.contribution_id:
                    mapping[item.contribution_id] = f"{prefix}-contribution-{index + 1}"
            for item in conversion.roster.entries:
                item.item_id = mapping[item.item_id]
                if item.contribution_id:
                    item.contribution_id = mapping[item.contribution_id]
            for group in conversion.roster.unresolved_groups:
                group.known_member_item_ids = [
                    mapping[item] for item in group.known_member_item_ids
                ]
            for contribution in conversion.contributions:
                contribution.item_id = mapping[contribution.item_id]
                contribution.roster_item_id = mapping[contribution.roster_item_id]
        roster = build_record(
            RosterRecord,
            record_id=roster_ref.record_id,
            revision=roster_ref.revision,
            created_at=created_at,
            execution_id=ctx.execution.execution_id,
            data=conversion.roster,
        )
        paper = build_record(
            PaperRecord,
            record_id=paper_ref.record_id,
            revision=paper_ref.revision,
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

    def _repairs(self, ctx, source, inputs, payload, schema):
        """Collect independent safe repairs together, without changing science."""
        from ..records.roster_input import CONTRIBUTION_FIELDS

        invariants = roster_invariant_repairs(payload)
        blocked = {
            fix.path[1]
            for fix in invariants
            if len(fix.path) == 3 and fix.path[-1] == "disposition"
        }
        structural = []
        for fix in schema_repairs(payload, schema):
            if (
                len(fix.path) >= 3
                and fix.path[0] == "entries"
                and fix.path[1] in blocked
            ):
                field = fix.path[2]
                if field in CONTRIBUTION_FIELDS:
                    continue  # The source-error repair removes these claims.
                if field == "evidence" and len(fix.path) == 3:
                    entry = payload["entries"][fix.path[1]]
                    base = schema["$defs"]["RosterEntrySubmission"]["properties"][field]
                    if field not in entry or Draft202012Validator(
                        {**base, "$defs": schema["$defs"]}
                    ).is_valid(entry[field]):
                        continue
            structural.append(fix)
        fixes = [
            structural,
            _citation_repairs(payload, inputs, source, ctx.space),
            invariants,
            _gap_repairs(payload, source),
        ]
        entries = payload.get("entries")
        groups = payload.get("unresolved_groups")
        if isinstance(entries, list) and isinstance(groups, list):
            identifiers = {
                item.get("value")
                for entry in entries
                if isinstance(entry, dict)
                and isinstance(entry.get("identifiers"), list)
                for item in entry["identifiers"]
                if isinstance(item, dict) and isinstance(item.get("value"), str)
            }
            missing = sorted(
                {
                    name
                    for group in groups
                    if isinstance(group, dict)
                    and isinstance(group.get("member_identifier_values"), list)
                    for name in group["member_identifier_values"]
                    if isinstance(name, str) and name not in identifiers
                }
            )
            fixes.append(
                [
                    Repair(
                        ("entries",),
                        {
                            "allOf": [
                                {"$ref": "#/$defs/RosterEntrySubmission"},
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
            )
        return merge_repairs(*fixes)

    @scoped_verification
    def _retain_roster(self, ctx, source, inputs, initial, draft, error, failure_key):
        """Retain only whole, independently validated entries from a failed draft."""
        if not isinstance(draft, dict) or not isinstance(draft.get("entries"), list):
            return None
        schema = initial.tool.parameters
        definitions = schema["$defs"]

        def valid(value, definition):
            return Draft202012Validator(
                {"$ref": "#/$defs/" + definition, "$defs": definitions}
            ).is_valid(value)

        partial = {
            "coverage": {
                "state": "not_checked",
                "checked_file_ids": [],
                "gap_ids": [],
            },
            "entries": [],
            "unresolved_groups": [],
            "model_judgments": [],
        }
        coverage = draft.get("coverage")
        if valid(coverage, "RosterCoverageSubmission"):
            supplied = {v["file_id"] for v in inputs.citation_locations.values()}
            gaps = {g.gap_id for g in source.data.gaps}
            if (
                set(coverage["checked_file_ids"]) <= supplied
                and set(coverage.get("gap_ids", [])) <= gaps
            ):
                partial["coverage"] = copy.deepcopy(coverage)
                partial["coverage"]["state"] = (
                    "partial" if coverage["checked_file_ids"] else "not_checked"
                )
        try:
            self._records(ctx, source, inputs, partial)
        except ValueError:
            # Invalid global bookkeeping cannot certify material checks. Keep
            # the raw declaration archived and make no checked-material claim.
            partial["coverage"] = {
                "state": "not_checked",
                "checked_file_ids": [],
                "gap_ids": [],
            }
        # A duplicate identity blocks all owners, including an otherwise valid
        # entry. Never choose the first row or treat it as an established object.
        owners = {}
        for index, entry in enumerate(draft["entries"]):
            if not isinstance(entry, dict) or not isinstance(
                entry.get("identifiers"), list
            ):
                continue
            for identifier in entry["identifiers"]:
                if isinstance(identifier, dict) and isinstance(
                    identifier.get("value"), str
                ):
                    owners.setdefault(identifier["value"], set()).add(index)
        ambiguous = {
            i for indices in owners.values() if len(indices) > 1 for i in indices
        }
        kept = []
        rejected = []
        for index, entry in enumerate(draft["entries"]):
            reason = None
            if index in ambiguous:
                reason = "Identifier also belongs to another submitted entry."
            elif not valid(entry, "RosterEntrySubmission"):
                reason = "Entry does not satisfy the original submission schema."
            else:
                probe = {**partial, "entries": [entry]}
                try:
                    self._records(ctx, source, inputs, probe)
                except (ValueError, KeyError, TypeError) as exc:
                    reason = str(exc)
            if reason:
                rejected.append({"index": index, "error": reason})
            else:
                kept.append(index)
                partial["entries"].append(copy.deepcopy(entry))
        if not kept:
            return None  # Invalid entries cannot become a successful empty roster.
        available = {
            identifier["value"]
            for entry in partial["entries"]
            for identifier in entry["identifiers"]
        }
        groups = draft.get("unresolved_groups")
        if isinstance(groups, list):
            for group in groups:
                if not valid(group, "UnresolvedGroupSubmission"):
                    continue
                candidate = copy.deepcopy(group)
                candidate["member_identifier_values"] = [
                    name
                    for name in candidate.get("member_identifier_values", [])
                    if name in available
                ]
                probe = {**partial, "unresolved_groups": [candidate]}
                try:
                    self._records(ctx, source, inputs, probe)
                except (ValueError, KeyError, TypeError):
                    continue
                partial["unresolved_groups"].append(candidate)
        judgments = draft.get("model_judgments", [])
        if isinstance(judgments, list) and all(isinstance(s, str) for s in judgments):
            partial["model_judgments"] = copy.deepcopy(judgments)
        digest = sha256_of(
            {
                "request": initial.model_dump(mode="json"),
                "draft": draft,
                "partial": partial,
                "entry_indices": kept,
            }
        )
        saved = ctx.reuse_step(PARTIAL_STEP, failure_key)
        if saved is None:
            records, questions = self._records(
                ctx,
                source,
                inputs,
                partial,
                partial_digest=digest,
                entry_indices=kept,
            )
            if questions:
                raise SubmissionError(
                    "Automatic partial rosters cannot create questions"
                )
            ref = ctx.space.publish_run_file(
                ctx.execution.execution_id,
                f"outputs/roster-partials/{failure_key}.json",
                canonical_json(
                    {
                        "error": str(error),
                        "retained_entry_indices": kept,
                        "rejected_entries": rejected,
                        "draft_sha256": sha256_of(draft),
                        "membership_unverified_outside_retained_entries": True,
                    }
                ).encode(),
            )
            append_run_revision(ctx.space, ctx.execution, artifacts=[ref])
            ctx.commit_step(
                step_id=PARTIAL_STEP,
                input_digest=failure_key,
                records=records,
                result_refs=[r.ref() for r in records],
            )
            saved = ctx.reuse_step(PARTIAL_STEP, failure_key)
        return [r.model_dump(mode="json") for r in saved.result_refs]

    def _extract(self, ctx, state):
        source, inputs, initial = self._inputs(ctx)
        digest = sha256_of(initial.model_dump(mode="json"))
        limit = format_correction_limit(ctx)
        failure_key = sha256_of(
            {
                "request": digest,
                "corrections": limit,
                "call_keys": [retry_call_key(ctx, STEP, i) for i in range(limit + 1)],
            }
        )
        draft_state = {}
        try:
            if (
                ctx.store.find_step_result(ctx.execution.execution_id, STEP, digest)
                is None
            ):
                partial = ctx.store.find_step_result(
                    ctx.execution.execution_id, PARTIAL_STEP, failure_key
                )
                if partial is not None:
                    path = (
                        ctx.space.root
                        / "runs"
                        / ctx.execution.execution_id
                        / "outputs/roster-partials"
                        / (failure_key + ".json")
                    )
                    ref = next(
                        a
                        for a in ctx.store.read_record_current(
                            ctx.execution.run_ref.record_id
                        ).data.artifacts
                        if a.path == path.relative_to(ctx.space.root).as_posix()
                    )
                    error = json.loads(ctx.space.read_file(ref))["error"]
                    if self.defer_partial_failure:
                        return {
                            "result_refs": [
                                r.model_dump(mode="json") for r in partial.result_refs
                            ],
                            "roster_error": error,
                        }
                    raise CallFailure(error)
            return {
                **self._extract_complete(ctx, state, draft_state),
                "roster_error": "",
            }
        except (CallFailure, ValueError) as exc:
            if ctx.execution.input_snapshot.expert_adjudication:
                raise
            refs = self._retain_roster(
                ctx, source, inputs, initial, draft_state.get("draft"), exc, failure_key
            )
            if refs and self.defer_partial_failure:
                return {"result_refs": refs, "roster_error": str(exc)}
            raise

    defer_partial_failure = False

    def _extract_complete(self, ctx, state, draft_state):
        source, inputs, initial = self._inputs(ctx)
        digest = sha256_of(initial.model_dump(mode="json"))
        saved = ctx.reuse_step(STEP, digest)
        if saved is None:
            request = CallRequest.model_validate(initial.model_dump())
            draft = None
            fixes = []
            max_corrections = format_correction_limit(ctx)
            for round_number in range(max_corrections + 1):
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
                    call_key=retry_call_key(ctx, STEP, round_number),
                )
                error = envelope.get("error")
                body = envelope.get("exchange", {}).get("body") or {}
                prior = body.get("choices", [{}])[0].get("message", {})
                prior = {
                    k: v for k, v in prior.items() if k in {"content", "tool_calls"}
                }
                proposed = _prior_submission(prior)
                cleanup = _argument_cleanup(prior)
                if cleanup and error == "invalid_tool_arguments":
                    error = None
                    _save_argument_cleanup(
                        ctx, prior, STEP, f"roster-{round_number:02d}"
                    )
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
                    draft = _clean_citations(
                        ctx, draft, inputs, source, STEP, f"roster-{round_number:02d}"
                    )
                    fixes = self._repairs(
                        ctx, source, inputs, draft, initial.tool.parameters
                    )
                    error = (
                        "Submission has fields requiring correction." if fixes else None
                    )
                draft_state["draft"] = draft
                payload = draft
                if error is None:
                    try:
                        records, questions = self._records(ctx, source, inputs, payload)
                    except EvidenceLocationError as exc:
                        error = "PDF citation validation: " + str(exc)
                    except SubmissionError as exc:
                        fixes = self._repairs(
                            ctx, source, inputs, payload, initial.tool.parameters
                        )
                        error = str(exc)
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
                if round_number == max_corrections:
                    raise CallFailure("roster format correction exhausted")
                prior_text = canonical_json(prior)
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
        if ctx.execution.input_snapshot.expert_adjudication and not getattr(
            self, "defer_review", False
        ):
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
