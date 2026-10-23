"""Read immutable extraction archives and select interrupted network stages."""

import copy
import hashlib
import json
import re
from pathlib import Path

from ..llm.config import CallRequest, Message, Tool, ToolCall
from ..llm.prompts import PromptBlueprint, PromptInput
from ..llm.transport import TokenDanceAdapter, network_retry_allowed
from ..pipeline.configuration import ExtractionConfig
from ..records.common import ArtifactRef, sha256_of
from ..records.extraction_input import ExtractionInput
from ..records.material import supplied_locations
from ..workspace.archives import read_json
from ..workspace.layout import _write_file_published
from .archive import archive_path, open_archive
from .experiments import read_experiment
from .materials import round_id


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_file_published(
        path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    )


def read_file(path):
    return json.loads(Path(path).read_bytes())


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def repair_folder(root, name, label):
    round_id(name)
    if not re.fullmatch(r"network-repair-\d{2,}", label):
        raise ValueError("Use a distinct network-repair-NN label.")
    return Path(root).resolve() / "runs" / name / label


def request_from_prepared(prepared, model):
    """Reconstruct typed messages while preserving the exact archived wire body."""
    payload = prepared["payload"]
    messages = []
    for raw in payload["messages"]:
        item = copy.deepcopy(raw)
        item["tool_calls"] = [
            ToolCall(
                id=c["id"],
                name=c["function"]["name"],
                arguments=c["function"]["arguments"],
            )
            for c in item.get("tool_calls", [])
        ]
        messages.append(Message.model_validate(item))
    tools = payload.get("tools", [])
    request = CallRequest(
        model=model,
        messages=messages,
        tool=Tool.model_validate(tools[0]["function"]) if tools else None,
    )
    if TokenDanceAdapter().prepare(request)["payload"] != payload:
        raise ValueError("Archived payload differs from its fixed model configuration.")
    return request


def retryable_network(envelope, runtime):
    return network_retry_allowed(envelope, runtime) or envelope.get("error") in {
        "upload_queue_expired",
        "quota_wait_expired",
    }


def source_context(root, name, paper):
    """Return only extraction inputs and facts; never read Gold or scoring inputs."""
    space = open_archive(root, name, paper)
    try:
        return execution_context(space, name + "/" + paper)
    finally:
        space.close()


def execution_context(space, execution_id):
    """Read fixed extraction facts from an already-open workspace, without Gold."""
    from ..pipeline.results import current_result, saved_quantities
    from ..pipeline.roster import saved_roster

    _, summary = current_result(space, execution_id)
    trace = read_json(space, summary.trace_ref)
    document = ExtractionInput.model_validate(read_json(space, summary.input_ref))
    roster = saved_roster(space, execution_id)
    quantities = saved_quantities(space, execution_id, roster) if roster else {}
    return {
        **(
            {"evidence_policy": document.snapshot.options["evidence_policy"]}
            if "evidence_policy" in document.snapshot.options
            else {}
        ),
        "metadata": document.metadata.model_dump(mode="json"),
        "material": read_json(space, document.material_ref),
        "material_ref": document.material_ref.model_dump(),
        "blueprints": read_json(space, document.snapshot.prompt_refs[0]),
        "roster": roster.model_dump(mode="json", by_alias=True) if roster else None,
        "quantities": {
            str(i): q.model_dump(mode="json", by_alias=True)
            for i, q in quantities.items()
        },
        "trace": trace,
        "configuration": ExtractionConfig.model_validate_json(
            document.snapshot.options["extraction_config"]
        ).model_dump(mode="json"),
        "summary": summary.model_dump(mode="json"),
    }


def archive_value(root, ref):
    from ..workspace import open_workspace

    space = open_workspace(root, read_only=True)
    try:
        return read_json(space, ArtifactRef.model_validate(ref))
    finally:
        space.close()


def make_job(
    paper,
    step,
    prepared,
    model,
    round_number,
    limit,
    *,
    source=None,
    baseline=None,
    source_index=None,
):
    request_from_prepared(prepared, model)
    return {
        "id": sha256_of({"paper": paper, "step": step})[:24],
        "paper": paper,
        "step": step,
        "source_index": source_index,
        "kind": "roster" if step == "roster" else "quantities",
        "prepared": prepared,
        "model": model.model_dump(mode="json"),
        "submission_round": round_number,
        "format_limit": limit,
        "source": source,
        "baseline": baseline,
        "state": "ready",
        "network_attempt": 0,
        "next_due": 0,
        "version_started": None,
        "call_execution": None,
        "calls": [],
        "validations": [],
        "fragment": None,
    }


def network_jobs(root, paper, context):
    """Select only archived network failures, preserving the used format rounds."""
    config = ExtractionConfig.model_validate(context["configuration"])
    jobs = []
    by_step = {}
    for call in context["trace"]["calls"]:
        by_step.setdefault(call["step"], []).append(call)
    for step, calls in by_step.items():
        last = max(calls, key=lambda c: c["attempt"])
        index = None if step == "roster" else int(step.rsplit("-", 1)[1])
        baseline = (
            context["roster"]
            if index is None
            else context["quantities"].get(str(index))
        )
        if baseline and baseline["data"]["status"] == "complete":
            continue
        if not last.get("response") or not last.get("request"):
            continue
        envelope = archive_value(root, last["response"])
        if not retryable_network(envelope, config.runtime):
            continue
        prepared = archive_value(root, last["request"])
        if envelope.get("request_sha256") != sha256_of(prepared):
            raise ValueError("Source response is bound to another request.")
        jobs.append(
            make_job(
                paper,
                step,
                prepared,
                config.model_for("roster" if index is None else "quantities"),
                last["round"],
                config.max_format_corrections,
                source={
                    "request": last["request"],
                    "response": last["response"],
                    "error": envelope.get("error"),
                },
                baseline=baseline,
                source_index=index,
            )
        )
    return jobs


def prepare_repair(root, name, *, label="network-repair-01", policy=None):
    from ..pipeline.inputs import code_fingerprint
    from .repair_runtime import RepairPolicy

    root = Path(root).resolve()
    folder = repair_folder(root, name, label)
    spec = read_experiment(root, name)
    config = ExtractionConfig.model_validate(spec["configuration"])
    policy = RepairPolicy.model_validate(policy or {}).model_dump(mode="json")
    if (folder / "manifest.json").exists():
        manifest = read_file(folder / "manifest.json")
        if manifest["policy"] != policy or manifest[
            "original_experiment_sha256"
        ] != file_hash(root / "runs" / name / "experiment.json"):
            raise ValueError("Repair label is already bound to different conditions.")
        verify_sources(root, manifest)
        return manifest
    jobs, facts, hashes = [], {}, {}
    for item in spec["materials"]["papers"]:
        paper = item["arxiv_id"]
        facts[paper] = context = source_context(root, name, paper)
        for path in [
            root / archive_path(name, paper),
            root / "runs" / name / paper / "run.json",
            root / "runs" / name / paper / "trace/trace.json",
            root / "runs" / name / paper / "literature_contribution.json",
            root / "runs" / name / paper / "evidence_audit.json",
        ]:
            if path.exists():
                hashes[str(path.relative_to(root))] = file_hash(path)
        jobs.extend(network_jobs(root, paper, context))
    for path in [
        root / "runs" / name / "experiment.json",
        *sorted((root / "runs" / name / "scoring").glob("*")),
    ]:
        if path.is_file():
            hashes[str(path.relative_to(root))] = file_hash(path)
    manifest = {
        "format": "hvs.network-repair",
        "version": 1,
        "round": name,
        "label": label,
        "original_experiment_sha256": file_hash(
            root / "runs" / name / "experiment.json"
        ),
        "original_file_hashes": hashes,
        "policy": policy,
        "configuration": config.model_dump(mode="json"),
        "source_facts_sha256": sha256_of(facts),
        "original_target_ids": [j["id"] for j in jobs],
        "repair_code_sha256": code_fingerprint(),
    }
    write_json(folder / "source-facts.json", facts)
    write_json(
        folder / "state.json",
        {"jobs": jobs, "started": None, "finished": None, "route": {}, "next_start": 0},
    )
    write_json(folder / "manifest.json", manifest)
    verify_sources(root, manifest)
    return manifest


def verify_sources(root, manifest):
    for path, expected in manifest["original_file_hashes"].items():
        if file_hash(Path(root) / path) != expected:
            raise ValueError("Original artifact changed: " + path)


def prepare_execution_repair(root, execution_ids, folder, *, policy=None):
    """Prepare derived repair from explicit terminal executions, without changing them."""
    from ..evaluation.adapt import prediction_from_execution
    from ..pipeline.inputs import code_fingerprint
    from ..runtime import get_execution
    from ..workspace import open_workspace
    from .repair_runtime import RepairPolicy

    root, folder = Path(root).resolve(), Path(folder).resolve()
    folder.relative_to(root)
    if not execution_ids or len(set(execution_ids)) != len(execution_ids):
        raise ValueError("Select distinct source executions.")
    if folder.exists():
        raise ValueError(
            "Use a new repair folder; resume an existing manifest unchanged."
        )
    policy = RepairPolicy.model_validate(policy or {}).model_dump(mode="json")
    facts, baselines, jobs, hashes = {}, {}, [], {}
    space = open_workspace(root, read_only=True)
    try:
        with space.lock():
            for execution_id in execution_ids:
                handle = get_execution(space, execution_id)
                if space.store().read_record(handle.run_ref).data.state not in {
                    "finished",
                    "failed",
                    "cancelled",
                }:
                    raise ValueError("End the source execution before derived repair.")
                baseline = prediction_from_execution(space, execution_id)
                paper = baseline.content.metadata.arxiv_id
                if paper in facts:
                    raise ValueError("Select only one source execution per paper.")
                facts[paper] = context = execution_context(space, execution_id)
                baselines[paper] = baseline.model_dump(mode="json")
                jobs.extend(network_jobs(root, paper, context))
                for path in (root / "runs" / execution_id).rglob("*"):
                    if path.is_file():
                        hashes[str(path.relative_to(root))] = file_hash(path)
    finally:
        space.close()
    configs = [c["configuration"] for c in facts.values()]
    if any(c != configs[0] for c in configs):
        raise ValueError("Repair sources must share the same frozen configuration.")
    manifest = {
        "format": "hvs.network-repair",
        "version": 1,
        "source_executions": list(execution_ids),
        "original_file_hashes": hashes,
        "policy": policy,
        "configuration": configs[0],
        "source_facts_sha256": sha256_of(facts),
        "baselines_sha256": sha256_of(baselines),
        "original_target_ids": [j["id"] for j in jobs],
        "repair_code_sha256": code_fingerprint(),
    }
    write_json(folder / "source-facts.json", facts)
    write_json(folder / "baselines.json", baselines)
    write_json(
        folder / "state.json",
        {"jobs": jobs, "started": None, "finished": None, "route": {}, "next_start": 0},
    )
    write_json(folder / "manifest.json", manifest)
    verify_sources(root, manifest)
    return manifest


def job_locations(facts, job):
    return supplied_locations(
        facts[job["paper"]]["material"]["citation_locations"],
        job["prepared"]["payload"]["messages"][1]["content"],
    )


def descendant_jobs(context, roster_job, fragment):
    """Only a repaired missing roster can authorize new numerical descendants."""
    if roster_job["kind"] != "roster" or roster_job["baseline"] is not None:
        raise ValueError("Only a previously missing roster may fan out.")
    config = ExtractionConfig.model_validate(context["configuration"])
    model = config.model_for("quantities")
    blueprint = PromptBlueprint.model_validate(context["blueprints"]["quantities"])
    material = PromptInput.model_validate(context["material"])
    result = []
    for index, star in zip(fragment["indices"], fragment["values"], strict=True):
        request = blueprint.request(
            model, material, [i["value"] for i in star["identifiers"]]
        )
        prepared = TokenDanceAdapter().prepare(request)
        if "evidence_policy" in roster_job["prepared"]:
            prepared["evidence_policy"] = roster_job["prepared"]["evidence_policy"]
        result.append(
            make_job(
                roster_job["paper"],
                "quantities-repaired-roster-" + str(index),
                prepared,
                model,
                0,
                config.max_format_corrections,
                source={"repaired_roster_job": roster_job["id"]},
                source_index=index,
            )
        )
    return result
