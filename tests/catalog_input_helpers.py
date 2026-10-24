"""Synthetic archived repairs produced through the common pipeline and validators."""

import copy
import io
import json

from pydantic import SecretStr
from test_benchmark_repair import network_failure, quick_policy
from test_catalog_delivery import file_ref
from test_quantities import quantities, value
from test_roster import ScriptedAdapter, entry, submission

from hvs_catalog.benchmark.repair import export_repair_prediction, repaired_prediction
from hvs_catalog.benchmark.repair_runtime import run_prepared_repair
from hvs_catalog.benchmark.repair_source import (
    execution_context,
    make_job,
    prepare_execution_repair,
)
from hvs_catalog.catalogue.inputs import json_bytes
from hvs_catalog.catalogue.recovery import recover_arguments
from hvs_catalog.evaluation.adapt import prediction_from_execution
from hvs_catalog.llm.managed import CallService
from hvs_catalog.pipeline import resume_extraction, start_extraction
from hvs_catalog.pipeline.results import current_result
from hvs_catalog.records.catalog import PaperSelection
from hvs_catalog.records.common import ArtifactRef, sha256_of
from hvs_catalog.records.literature_contribution import LiteratureQuantity
from hvs_catalog.workspace import open_workspace
from hvs_catalog.workspace.archives import read_json


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_bytes(data))


class CapturedAdapter(ScriptedAdapter):
    def send(self, payload, runtime, credential):
        exchange = super().send(payload, runtime, credential)
        exchange["transport"] = {
            "response_body_complete": True,
            "sse_done": True,
            "raw_response_truncated": False,
        }
        exchange["_raw_stream"] = io.BytesIO(json.dumps(exchange["body"]).encode())
        return exchange


def malformed(groups):
    # Omit a closing array bracket, while keeping every scalar and value object.
    return (
        '{"quantities":['
        + ",".join(json.dumps(g)[:-2] + "}" for g in groups)
        + '],"notes":""}'
    )


def native(prepared, replies):
    space, prep, config = prepared
    handle = start_extraction(
        space,
        preparation_execution_id=prep,
        request_key="archived-fixture",
        configuration=config,
    )
    report = resume_extraction(
        space,
        handle.execution_id,
        service=CallService(
            config.runtime,
            credential=SecretStr("synthetic"),
            adapter=CapturedAdapter(replies),
        ),
    )
    return handle, report


def receipt(space, job, call, groups, *, existing=None):
    env = read_json(space, ArtifactRef.model_validate(call["response"]))
    original = env["exchange"]["body"]["choices"][0]["message"]["tool_calls"][0][
        "function"
    ]["arguments"]
    effective, proof = recover_arguments(original)
    assert json.loads(effective) == {"quantities": groups, "notes": ""}
    groups = [
        LiteratureQuantity.model_validate(g).model_dump(mode="json") for g in groups
    ]
    paths = [
        f"/quantities/{q}/values/{v}"
        for q, g in enumerate(groups)
        for v, _ in enumerate(g["values"])
    ]
    additions = (
        groups
        if existing is None
        else [
            g for g in groups if g["quantity"] not in {x["quantity"] for x in existing}
        ]
    )
    fragment = {
        "status": "complete",
        "passed": True,
        "readable": True,
        "values": additions,
        "indices": paths,
        "rejected": {},
        "issues": [],
        "notes": "",
        "warnings": [],
        "local_structure_recovery": True,
    }
    item = {
        "id": job["id"],
        "paper": job["paper"],
        "source_index": job["source_index"],
        "step": job["step"],
        "source_request": call["request"],
        "source_response": call["response"],
        "raw_response": env["exchange"]["raw_response_ref"],
        "effective_arguments": effective,
        "receipt": proof,
        "fragment": fragment,
    }
    if existing is not None:
        fragment["accepted_existing_groups_omitted"] = sorted(
            g["quantity"] for g in existing
        )
        item.update(
            validated_full_groups=groups,
            preserved_existing_groups=existing,
            added_groups=additions,
        )
    return item


def export(
    space, folder, manifest_ref, facts, baseline, jobs, *, extras=None, overlay=None
):
    paper = facts["metadata"]["arxiv_id"]
    prediction = repaired_prediction(baseline, facts, jobs)
    prediction.provenance["network_repair"].update(overlay or {})
    export_repair_prediction(space.root, folder, prediction, facts)
    outputs = {
        name: file_ref(space.root, folder / name).model_dump()
        for name in (
            "retained-prediction.json",
            "overlay.json",
            "literature_contribution.json",
            "evidence_audit.json",
        )
    }
    run = dict(
        format="hvs.network-repair-result",
        version=1,
        arxiv_id=paper,
        source_execution_id=facts["summary"]["execution_id"],
        completion=prediction.completion,
        delivered=True,
        format_valid=True,
        manifest_ref=manifest_ref.model_dump(),
        outputs=outputs,
        **(extras or {}),
    )
    save(folder / "run.json", run)
    return PaperSelection(
        workspace=str(space.root), run_ref=file_ref(space.root, folder / "run.json")
    )


def local_repair(prepared, *, partial=True):
    space, _, config = prepared
    config.max_format_corrections = 1 if partial else 0
    distance = {"quantity": "distance", "values": [{**value("8"), "unit": "kpc"}]}
    radial = quantities()["quantities"][0]
    groups = [distance, radial] if partial else [radial]
    replies = [submission()]
    if partial:
        replies.append(
            {
                "quantities": [
                    distance,
                    {
                        "quantity": "radial_velocity",
                        "values": [
                            {
                                "value": "99",
                                "evidence": [
                                    {"location": "T:p99-b0", "quote": "Invalid"}
                                ],
                            }
                        ],
                    },
                ],
                "notes": "",
            }
        )
    replies.append(malformed(groups))
    handle, report = native(prepared, replies)
    assert report.result_status == "partial"
    original_ref, summary = current_result(space, handle.execution_id)
    facts = execution_context(space, handle.execution_id)
    baseline = prediction_from_execution(space, handle.execution_id).model_dump(
        mode="json"
    )
    paper = facts["metadata"]["arxiv_id"]
    call = max(
        (c for c in facts["trace"]["calls"] if c["step"].endswith("-0")),
        key=lambda c: c["attempt"],
    )
    prepared_call = read_json(space, ArtifactRef.model_validate(call["request"]))
    job = make_job(
        paper,
        call["step"],
        prepared_call,
        config.model_for("quantities"),
        call["round"],
        config.max_format_corrections,
        source_index=0,
    )
    kept = facts["quantities"]["0"]["data"]["quantities"]
    item = receipt(space, job, call, groups, existing=kept)
    job["fragment"] = item["fragment"]
    folder = space.root / ".state/local-recovery"
    save(folder / "source-facts.json", {paper: facts})
    save(folder / "baselines.json", {paper: baseline})
    save(folder / "recoveries.json", {"model_calls": 0, "targets": [item]})
    recovery_ref = file_ref(space.root, folder / "recoveries.json").model_dump()
    manifest = {
        "format": "hvs.original-168-format-repair",
        "version": 1,
        "protected_files": {original_ref.path: original_ref.sha256},
        "targets": {paper: [0]},
    }
    save(folder / "manifest.json", manifest)
    previous = {
        "run_ref": original_ref.model_dump(),
        "result_ref": summary.result_ref.model_dump(),
        "evidence_audit_ref": summary.evidence_audit_ref.model_dump(),
    }
    selected = export(
        space,
        space.root / "runs/local-derived",
        file_ref(space.root, folder / "manifest.json"),
        facts,
        baseline,
        [job],
        extras={
            "purpose": "local structure repair without model calls",
            "model_calls": 0,
            "recoveries_ref": recovery_ref,
            "source_indices": [0],
            "previous_outputs": previous,
        },
        overlay={"recoveries_ref": recovery_ref},
    )
    return selected, folder


def cumulative_repair(prepared, *, recover=True):
    space, _, config = prepared
    config.max_format_corrections = 0
    config.runtime.max_attempts = 1
    config.runtime.retry_post_send_network_errors = True
    handle, _ = native(prepared, [network_failure()])
    old = space.root / ".state/first-repair"
    prepare_execution_repair(
        space.root, [handle.execution_id], old, policy=quick_policy(initial_probe=True)
    )
    run_prepared_repair(
        space.root,
        old,
        credential=SecretStr("synthetic"),
        adapter=CapturedAdapter(
            [
                submission([entry("A"), entry("B", 2, "follow_up")]),
                quantities([value("480")]),
                "not JSON",
            ]
        ),
    )
    facts = json.loads((old / "source-facts.json").read_bytes())
    baselines = json.loads((old / "baselines.json").read_bytes())
    previous = json.loads((old / "state.json").read_bytes())["jobs"]
    paper = next(iter(facts))
    target = next(
        j for j in previous if j["kind"] == "quantities" and j["source_index"] == 1
    )
    assert not target["fragment"]
    new_job = make_job(
        paper,
        target["step"],
        target["prepared"],
        config.model_for("quantities"),
        0,
        0,
        source_index=1,
    )
    folder = space.root / ".state/following-repair"
    manifest = json.loads((old / "manifest.json").read_bytes())
    manifest.update(
        prior_manifest_ref=file_ref(space.root, old / "manifest.json").model_dump(),
        previous_jobs_sha256=sha256_of(previous),
        original_target_ids=[new_job["id"]],
    )
    save(folder / "manifest.json", manifest)
    save(folder / "source-facts.json", facts)
    save(folder / "baselines.json", baselines)
    save(folder / "previous-jobs.json", previous)
    save(
        folder / "state.json",
        {
            "jobs": [new_job],
            "started": None,
            "finished": None,
            "route": {},
            "next_start": 0,
        },
    )
    groups = quantities(
        [value("530", "B is a previous unbound candidate studied here.")]
    )["quantities"]
    groups[0]["values"][0]["evidence"][0]["location"] = "T:p1-b1"
    answer = malformed(groups) if recover else {"quantities": groups, "notes": ""}
    run_prepared_repair(
        space.root,
        folder,
        credential=SecretStr("synthetic"),
        adapter=CapturedAdapter([answer]),
    )
    jobs = json.loads((folder / "state.json").read_bytes())["jobs"]
    accepted = []
    if recover:
        calls = open_workspace(folder / ".calls", read_only=True)
        try:
            item = receipt(calls, jobs[0], jobs[0]["calls"][-1], groups)
            accepted.append(item)
            jobs[0]["fragment"] = item["fragment"]
        finally:
            calls.close()
    save(
        folder / "structure-recovery.json",
        {"accepted": accepted, "rejected": [], "model_calls": 0},
    )
    all_jobs = [copy.deepcopy(j) for j in previous if j["id"] != jobs[0]["id"]] + jobs
    selected = export(
        space,
        space.root / "runs/cumulative-derived",
        file_ref(space.root, folder / "manifest.json"),
        facts[paper],
        baselines[paper],
        all_jobs,
        overlay={
            "structure_recovery_ref": file_ref(
                space.root, folder / "structure-recovery.json"
            ).model_dump()
        },
    )
    return selected, folder
