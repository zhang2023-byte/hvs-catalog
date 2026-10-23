"""Fixed roster requests, full correction history, and immutable saved results."""

from __future__ import annotations

import base64
import json

from ..llm.config import Message, ToolCall
from ..llm.prompts import PromptImage, PromptInput
from ..records.common import (
    ArtifactRef,
    RecordRef,
    canonical_json,
)
from ..records.source import SourcePackageRecord
from ..runtime import get_execution
from .feedback import submission_feedback

CORRECTION = "Correct the errors in the latest tool result.\nCall submit_roster once with the complete corrected result."


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
    blocks = []
    for block in index["blocks"]:
        text_location = "T:" + block["block_id"]
        if block["text"]:
            locations[text_location] = {
                "file_id": pdf_id,
                "text": block["text"],
                "locator": {
                    "kind": "pdf",
                    "page": block["page"],
                    "block_id": block["block_id"],
                },
            }
            blocks.append(
                f"LOCATION {text_location} ({block['type']})\n"
                + block["text"]
                + f"\nEND LOCATION {text_location}"
            )
        for image_id in block.get("image_files", []):
            file = files[image_id]
            if file.role != "pdf_image" or pdf_id not in file.derived_from:
                raise ValueError("Image is not bound to the supplied PDF")
            data = space.read_file(ArtifactRef(path=file.path, sha256=file.sha256))
            image_location = "I:" + block["block_id"]
            location = locations.setdefault(
                image_location,
                {
                    "file_id": pdf_id,
                    "locator": {
                        "kind": "pdf_image",
                        "page": block["page"],
                        "block_id": block["block_id"],
                    },
                    "images": [],
                },
            )
            location["images"].append(
                ArtifactRef(path=file.path, sha256=file.sha256).model_dump(mode="json")
            )
            images.append(
                PromptImage(
                    label=f"LOCATION {image_location}",
                    kind="table" if block["type"] == "table" else "image",
                    image_url={
                        "url": f"data:{file.media_type};base64,"
                        + base64.b64encode(data).decode("ascii")
                    },
                )
            )
    if not blocks and not images:
        raise ValueError("The fixed source has no readable material")
    return PromptInput(
        images=images, citation_locations=locations, manuscript="\n\n".join(blocks)
    )


def start_roster_extraction(
    space, *, preparation_execution_id, request_key, configuration=None
):
    from .entry import start_extraction

    return start_extraction(
        space,
        preparation_execution_id=preparation_execution_id,
        request_key=request_key,
        configuration=configuration,
        scope="roster",
    )


def correction_messages(
    prior, issues, *, tool="submit_roster", locations=None, effective_prior=None
):
    """Use real tool call identifiers or preserve an invalid envelope as assistant text."""
    raw_calls = prior.get("tool_calls")
    calls = raw_calls if isinstance(raw_calls, list) else []
    ids = [call.get("id") for call in calls if isinstance(call, dict)]
    parsed = []
    invalid = not calls
    for call in calls:
        try:
            if (
                not isinstance(call.get("id"), str)
                or not call["id"]
                or ids.count(call["id"]) != 1
                or call.get("type", "function") != "function"
            ):
                raise ValueError("Invalid tool call identifier")
            parsed.append(ToolCall(id=call["id"], **call["function"]))
        except (ValueError, KeyError, TypeError, AttributeError):
            invalid = True
    feedback = canonical_json(
        submission_feedback(
            effective_prior if effective_prior is not None else prior,
            issues,
            tool=tool,
            locations=locations or {},
        )
    )
    reasoning = prior.get("reasoning_content")
    reasoning = reasoning if isinstance(reasoning, str) else None
    content = prior.get("content")
    if invalid or not (content is None or isinstance(content, str)):
        # Keep malformed output readable without inventing tool call identifiers.
        content = canonical_json(
            {k: v for k, v in prior.items() if k != "reasoning_content"}
        )
        invalid = True
    result = [
        Message(
            role="assistant",
            content=content,
            reasoning_content=reasoning,
            tool_calls=parsed,
        )
    ]
    result.extend(
        Message(role="tool", tool_call_id=call.id, content=feedback) for call in parsed
    )
    correction = (
        feedback + "\nCorrect the errors in this message.\n"
        "Call submit_roster once with the complete corrected result."
        if invalid
        else CORRECTION
    )
    result.append(
        Message(role="user", content=correction.replace("submit_roster", tool))
    )
    return result


def roster_result_refs(space, execution_id):
    handle = get_execution(space, execution_id)
    imported = handle.input_snapshot.options.get("roster_ref")
    if imported:
        return [RecordRef.model_validate_json(imported)]
    run = space.store().read_record_current(handle.run_ref.record_id)
    step = next((s for s in run.data.steps if s.step_id == "roster"), None)
    return list(step.result_refs) if step else []


def saved_roster(space, execution_id):
    return next(
        (space.store().read_record(r) for r in roster_result_refs(space, execution_id)),
        None,
    )


def roster_targets(space, execution_id):
    """Return fixed per-star targets without starting the quantity workflow."""
    record = saved_roster(space, execution_id)
    if record is None:
        raise ValueError("No saved roster is available")
    from ..records.validate import validate_collection
    from ..workspace.store import record_closure

    validate_collection(record_closure([record], space.store().read_record), space.root)
    return {
        "roster_ref": record.ref().model_dump(),
        "status": record.data.status,
        "skip_quantities": record.data.status == "complete" and not record.data.stars,
        "targets": [
            {
                "roster_ref": record.ref().model_dump(),
                "source_index": index,
                "identifiers": [i.value for i in star.identifiers],
                "source_ref": record.data.source_ref.model_dump(),
                "material_ref": record.data.material_ref.model_dump(),
            }
            for index, star in zip(
                record.data.source_indices, record.data.stars, strict=True
            )
        ],
    }
