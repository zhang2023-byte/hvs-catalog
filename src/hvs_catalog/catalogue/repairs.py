"""Read historical repair lineages without executing their orchestration scripts."""

import copy
import json

from ..benchmark.repair import validate_fragments
from ..benchmark.repair_source import make_job
from ..pipeline.configuration import ExtractionConfig
from ..records.common import ArtifactRef, sha256_of
from ..records.extraction_run import ExtractionRun
from ..workspace import open_workspace, resolve_within
from ..workspace.archives import read_json
from .recovery import replay_recovery, require


def _json(path):
    return json.loads(path.read_bytes())


def _frozen_pair(folder, manifest):
    facts = _json(resolve_within(folder, "source-facts.json"))
    baselines = _json(resolve_within(folder, "baselines.json"))
    require(
        sha256_of(facts) == manifest["source_facts_sha256"]
        and sha256_of(baselines) == manifest["baselines_sha256"],
        "Frozen repair source facts changed.",
    )
    return facts, baselines


def _local_context(space, raw, manifest, context, baseline, retained):
    from .inputs import _native

    ref = ArtifactRef.model_validate(raw["previous_outputs"]["run_ref"])
    require(
        manifest["protected_files"].get(ref.path) == ref.sha256,
        "Local recovery baseline is not bound by its manifest.",
    )
    original = read_json(space, ref)
    science_bytes, result_ref, audit_ref, fixed, _, _ = _native(space, original)
    run = ExtractionRun.model_validate(original)
    require(
        raw["previous_outputs"]["result_ref"] == result_ref.model_dump()
        and raw["previous_outputs"]["evidence_audit_ref"] == audit_ref.model_dump(),
        "Local recovery baseline references differ.",
    )
    trace = read_json(space, run.trace_ref)

    def snapshot(stage):
        record = (
            space.store().read_record(stage.snapshot_ref)
            if stage.snapshot_ref
            else None
        )
        return record.model_dump(mode="json", by_alias=True) if record else None

    expected = {
        **(
            {"evidence_policy": fixed.snapshot.options["evidence_policy"]}
            if "evidence_policy" in fixed.snapshot.options
            else {}
        ),
        "metadata": fixed.metadata.model_dump(mode="json"),
        "material": read_json(space, fixed.material_ref),
        "material_ref": fixed.material_ref.model_dump(),
        "blueprints": read_json(space, fixed.snapshot.prompt_refs[0]),
        "roster": snapshot(run.stages.roster),
        "quantities": {
            str(s.source_index): snapshot(s)
            for s in run.stages.quantities
            if s.snapshot_ref
        },
        "trace": trace,
        "configuration": ExtractionConfig.model_validate_json(
            fixed.snapshot.options["extraction_config"]
        ).model_dump(mode="json"),
        "summary": run.model_dump(mode="json"),
    }
    require(
        context == expected,
        "Local recovery facts differ from the frozen extraction snapshots.",
    )
    provenance = baseline["provenance"]
    require(
        baseline["content"] == json.loads(science_bytes)
        and baseline["completion"] == run.status.result
        and provenance == retained["provenance"]["baseline"]
        and provenance["generation_ref"] == ref.model_dump()
        and provenance["run"] == original
        and provenance["input"] == fixed.model_dump(mode="json")
        and provenance["trace"] == trace
        and all(
            baseline[k] == retained[k]
            for k in ("rules", "materials", "scope", "errors")
        ),
        "Local recovery baseline differs from its retained source.",
    )


def _local_jobs(space, raw, manifest, folder, retained):
    facts = _json(resolve_within(folder, "source-facts.json"))
    baselines = _json(resolve_within(folder, "baselines.json"))
    paper = raw["arxiv_id"]
    context, baseline = facts[paper], baselines[paper]
    _local_context(space, raw, manifest, context, baseline, retained)
    recovery = read_json(space, ArtifactRef.model_validate(raw["recoveries_ref"]))
    require(
        recovery["model_calls"] == raw["model_calls"] == 0,
        "Local recovery must not invent model calls.",
    )
    require(
        retained["provenance"]["network_repair"]["recoveries_ref"]
        == raw["recoveries_ref"],
        "Local recovery receipt binding differs.",
    )
    items = [item for item in recovery["targets"] if item["paper"] == paper]
    indices = [item["source_index"] for item in items]
    require(
        len(indices) == len(set(indices))
        and sorted(indices)
        == sorted(raw["source_indices"])
        == sorted(manifest["targets"][paper]),
        "Local recovery target coverage differs.",
    )
    config = ExtractionConfig.model_validate(context["configuration"])
    jobs = []
    for item in items:
        index = item["source_index"]
        record = context["quantities"][str(index)]
        require(
            record["data"]["status"] in {"partial", "failed"},
            "Local recovery replaces a successful stage.",
        )
        calls = [
            call
            for call in context["trace"]["calls"]
            if call["step"].endswith("-" + str(index))
        ]
        require(bool(calls), "Local recovery has no frozen source call.")
        call = max(calls, key=lambda c: c["attempt"])
        require(
            item["source_request"] == call["request"]
            and item["source_response"] == call["response"]
            and item["step"] == call["step"],
            "Local recovery is not bound to its source stage.",
        )
        prepared = read_json(space, ArtifactRef.model_validate(call["request"]))
        job = make_job(
            paper,
            call["step"],
            prepared,
            config.model_for("quantities"),
            call["round"],
            config.max_format_corrections,
            source_index=index,
        )
        job["fragment"] = replay_recovery(
            space, facts, job, item, existing=record["data"]["quantities"]
        )
        jobs.append(job)
    return facts, baselines, jobs


def repair_inputs(space, raw, retained):
    """Return verified source facts and all effective stages of the selected lineage."""
    manifest = read_json(space, ArtifactRef.model_validate(raw["manifest_ref"]))
    require(manifest.get("version") == 1, "Unsupported repair manifest version.")
    folder = resolve_within(space.root, raw["manifest_ref"]["path"]).parent
    if manifest.get("format") == "hvs.original-168-format-repair":
        facts, baselines, jobs = _local_jobs(space, raw, manifest, folder, retained)
    else:
        require(
            manifest.get("format") == "hvs.network-repair",
            "Unsupported repair manifest format.",
        )
        facts, baselines = _frozen_pair(folder, manifest)
        state = _json(resolve_within(folder, "state.json"))
        require(state["finished"] is not None, "Repair is not finished.")
        paper = raw["arxiv_id"]
        jobs = copy.deepcopy([j for j in state["jobs"] if j["paper"] == paper])
        validate_fragments(folder, facts, jobs)
        if "prior_manifest_ref" in manifest:
            prior_ref = ArtifactRef.model_validate(manifest["prior_manifest_ref"])
            prior_manifest = read_json(space, prior_ref)
            prior_folder = resolve_within(space.root, prior_ref.path).parent
            prior_facts, _ = _frozen_pair(prior_folder, prior_manifest)
            previous = _json(resolve_within(folder, "previous-jobs.json"))
            require(
                sha256_of(previous) == manifest["previous_jobs_sha256"],
                "Previous repair jobs changed.",
            )
            previous = [j for j in previous if j["paper"] == paper]
            validate_fragments(prior_folder, prior_facts, previous)
            overlay = retained["provenance"]["network_repair"]
            recovery = read_json(
                space, ArtifactRef.model_validate(overlay["structure_recovery_ref"])
            )
            require(
                not recovery["rejected"] and recovery["model_calls"] == 0,
                "Unresolved local recovery records.",
            )
            selected = {j["id"]: j for j in jobs}
            receipts = [r for r in recovery["accepted"] if r["paper"] == paper]
            require(
                len({r["id"] for r in receipts}) == len(receipts),
                "Duplicate recovery target.",
            )
            calls = open_workspace(resolve_within(folder, ".calls"), read_only=True)
            try:
                for item in receipts:
                    require(
                        item["id"] in selected,
                        "Recovery target is absent from the selected stages.",
                    )
                    job = selected[item["id"]]
                    require(
                        job["calls"]
                        and item["source_request"] == job["calls"][-1]["request"]
                        and item["source_response"] == job["calls"][-1]["response"],
                        "Recovery is not bound to the final stage response.",
                    )
                    require(
                        not (job["fragment"] or {}).get("values"),
                        "Local recovery would replace accepted stage values.",
                    )
                    job["fragment"] = replay_recovery(calls, facts, job, item)
            finally:
                calls.close()
            jobs = [j for j in previous if j["id"] not in selected] + jobs
    require(
        jobs and len({j["id"] for j in jobs}) == len(jobs),
        "Repair stages are absent or duplicated.",
    )
    require(
        facts[raw["arxiv_id"]]["summary"]["execution_id"] == raw["source_execution_id"],
        "Repair source execution does not match its paper.",
    )
    keys = ("id", "step", "source_index", "kind", "fragment")
    actual = [{k: j.get(k) for k in keys} for j in jobs]
    saved = [
        {k: j.get(k) for k in keys}
        for j in retained["provenance"]["network_repair"]["stages"]
    ]
    require(
        actual == saved, "Effective repair stages differ from the retained provenance."
    )
    return facts, baselines, jobs
