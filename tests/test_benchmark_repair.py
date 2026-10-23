"""Synthetic network repair preserves original archives and bounded corrections."""

import copy
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from pydantic import SecretStr
from test_benchmark import material_fixture
from test_quantities import quantities, value
from test_roster import ScriptedAdapter, submission

from hvs_catalog.benchmark.experiments import prepare_experiment, run_experiment
from hvs_catalog.benchmark.materials import migrate_package
from hvs_catalog.benchmark.repair import (
    merge_quantities,
    repaired_prediction,
    score_repair,
    validate_fragments,
)
from hvs_catalog.benchmark.repair_runtime import RepairPolicy, _route_result, run_repair
from hvs_catalog.benchmark.repair_source import (
    file_hash,
    prepare_repair,
    read_file,
    request_from_prepared,
    verify_sources,
)
from hvs_catalog.llm.managed import CallService
from hvs_catalog.llm.transport import TransportFailure
from hvs_catalog.workspace import initialize_workspace


def network_failure(status=503):
    return TransportFailure(
        "http_error",
        uncertain=True,
        exchange={
            "status": status,
            "body": {"error": {"code": "no_endpoints_available"}},
            "transport": {"phase": "decode"},
        },
    )


def quick_policy(**kwargs):
    return RepairPolicy(
        preview_spacing=0,
        other_spacing=0,
        preview_concurrency=1,
        other_concurrency=1,
        backoff_base=0.001,
        backoff_cap=0.002,
        circuit_seconds=0.001,
        **kwargs,
    ).model_dump()


def closed_benchmark(prepared, tmp_path, responses, *, corrections=2):
    package, paper, config = material_fixture(prepared, tmp_path)
    config = config.model_copy(deep=True)
    config.max_format_corrections = corrections
    config.runtime.max_attempts = 1
    config.runtime.retry_post_send_network_errors = True
    root = tmp_path / "benchmark"
    space = initialize_workspace(root, benchmark=True)
    migrate_package(package, space, paper, "mineru_local")
    space.close()
    prepare_experiment(
        root,
        papers=[paper],
        variant="mineru_local",
        configuration=config,
        purpose="Synthetic network failure",
        name="exec01",
    )
    adapter = ScriptedAdapter(responses)
    run_experiment(
        root,
        "exec01",
        service_factory=lambda cfg: CallService(
            cfg.runtime, credential=SecretStr("synthetic"), adapter=adapter
        ),
    )
    # Uncertain network failures are explicitly ended, as in a closed real round.
    from hvs_catalog.benchmark.experiments import close_experiment

    close_experiment(root, "exec01")
    return root, paper, config


def test_repair_retries_503_and_preserves_original_archive(prepared, tmp_path):
    root, _, config = closed_benchmark(
        prepared, tmp_path, [submission(), network_failure()]
    )
    manifest = prepare_repair(root, "exec01", policy=quick_policy())
    assert len(manifest["original_target_ids"]) == 1
    before = copy.deepcopy(manifest["original_file_hashes"])
    adapter = ScriptedAdapter([network_failure(), quantities()])
    result = run_repair(
        root, "exec01", credential=SecretStr("synthetic"), adapter=adapter
    )
    assert result["complete"] == 1 and result["attempts"] == 2
    folder = root / "runs/exec01/network-repair-01"
    state = read_file(folder / "state.json")
    job = state["jobs"][0]
    assert job["submission_round"] == 0
    assert len(job["calls"]) == 2
    assert adapter.seen[0] == adapter.seen[1]
    assert job["fragment"]["values"][0]["values"][0]["value"] == "520"
    verify_sources(root, manifest)
    assert all(file_hash(root / p) == h for p, h in before.items())
    again = run_repair(
        root, "exec01", credential=SecretStr("synthetic"), adapter=adapter
    )
    assert again == result and len(adapter.seen) == 2
    assert (
        request_from_prepared(
            job["prepared"], config.model_for("quantities")
        ).model.model
        == config.model_for("quantities").model
    )


def test_repair_does_not_reset_used_format_rounds(prepared, tmp_path):
    bad = quantities()
    bad["quantities"][0]["values"][0]["evidence"][0]["location"] = "T:p9-b9"
    root, _, _ = closed_benchmark(
        prepared, tmp_path, [submission(), bad, bad, network_failure()]
    )
    prepare_repair(root, "exec01", policy=quick_policy())
    adapter = ScriptedAdapter([bad, quantities()])
    result = run_repair(
        root, "exec01", credential=SecretStr("synthetic"), adapter=adapter
    )
    state = read_file(root / "runs/exec01/network-repair-01/state.json")
    assert state["jobs"][0]["submission_round"] == 2
    assert len(adapter.seen) == 1
    assert result["complete"] == 0


def test_complete_and_format_failed_stages_are_not_network_targets(prepared, tmp_path):
    root, _, _ = closed_benchmark(prepared, tmp_path, [submission(), quantities()])
    manifest = prepare_repair(root, "exec01", policy=quick_policy())
    assert manifest["original_target_ids"] == []
    adapter = ScriptedAdapter([quantities()])
    assert (
        run_repair(root, "exec01", credential=SecretStr("synthetic"), adapter=adapter)[
            "attempts"
        ]
        == 0
    )
    assert not adapter.seen
    broken = copy.deepcopy(manifest)
    path = next(iter(broken["original_file_hashes"]))
    broken["original_file_hashes"][path] = "0" * 64
    with pytest.raises(ValueError, match="Original artifact changed"):
        verify_sources(root, broken)


def test_network_missing_roster_only_spawns_its_own_quantities(prepared, tmp_path):
    root, _, _ = closed_benchmark(prepared, tmp_path, [network_failure()])
    prepare_repair(root, "exec01", policy=quick_policy())
    adapter = ScriptedAdapter([submission(), quantities()])
    result = run_repair(
        root, "exec01", credential=SecretStr("synthetic"), adapter=adapter
    )
    jobs = read_file(root / "runs/exec01/network-repair-01/state.json")["jobs"]
    assert result["complete"] == 2 and len(adapter.seen) == 2
    assert [j["kind"] for j in jobs] == ["roster", "quantities"]
    assert jobs[1]["source"] == {"repaired_roster_job": jobs[0]["id"]}
    assert json.loads(adapter.seen[1]["messages"][2]["content"]) == {
        "identifiers": ["A"]
    }


def test_partial_group_preservation_does_not_choose_new_values():
    old = quantities()["quantities"]
    changed = quantities([value("530")])["quantities"]
    new = {"quantity": "distance", "values": [{**value("4"), "unit": "kpc"}]}
    merged, conflicts = merge_quantities(old, [*changed, new])
    assert merged == [*old, new]
    assert conflicts == ["radial_velocity"]
    assert old[0]["values"][0]["value"] == "520"


def test_backoff_retry_after_and_circuit_do_not_restart_failed_route():
    policy = RepairPolicy()
    assert [policy.delay(i, rng=lambda a, b: b) for i in range(1, 8)] == [
        10,
        20,
        40,
        80,
        160,
        300,
        300,
    ]
    assert policy.delay(1, 700, rng=lambda a, b: a) == 700
    route = {}
    env = {"error": "http_error", "exchange": {"status": 503}}
    for _ in range(5):
        _route_result(route, env, policy, 100)
    assert route["open_until"] == 400
    for i in range(3):
        route["half_open"] = True
        _route_result(route, env, policy, 400 + 300 * i)
    assert route["stopped"] == "circuit_exhausted"
    route = {}
    _route_result(
        route,
        {
            "error": "rate_limited",
            "exchange": {"status": 429, "body": {"code": "insufficient_quota"}},
        },
        policy,
        0,
    )
    assert route["stopped"] == "insufficient_quota"


def test_attempt_exhaustion_and_permanent_errors_do_not_use_format_allowance(
    prepared, tmp_path
):
    root, _, _ = closed_benchmark(prepared, tmp_path, [submission(), network_failure()])
    prepare_repair(root, "exec01", policy=quick_policy(other_attempts=2))
    adapter = ScriptedAdapter([network_failure()])
    result = run_repair(
        root, "exec01", credential=SecretStr("synthetic"), adapter=adapter
    )
    assert result["attempts"] == 2 and len(adapter.seen) == 2
    assert result["complete"] == 0
    prepare_repair(root, "exec01", label="network-repair-02", policy=quick_policy())
    adapter = ScriptedAdapter([network_failure(400)])
    result = run_repair(
        root,
        "exec01",
        label="network-repair-02",
        credential=SecretStr("synthetic"),
        adapter=adapter,
    )
    assert result["attempts"] == 1 and len(adapter.seen) == 1


def test_archived_response_is_consumed_after_coordinator_restart(prepared, tmp_path):
    from hvs_catalog.benchmark.repair_source import write_json

    root, _, _ = closed_benchmark(prepared, tmp_path, [submission(), network_failure()])
    prepare_repair(root, "exec01", policy=quick_policy())
    adapter = ScriptedAdapter([quantities()])
    run_repair(root, "exec01", credential=SecretStr("synthetic"), adapter=adapter)
    folder = root / "runs/exec01/network-repair-01"
    state = read_file(folder / "state.json")
    job = state["jobs"][0]
    job.update(
        call_execution=job["calls"][0]["execution_id"],
        calls=[],
        validations=[],
        fragment=None,
        state="running",
    )
    state["finished"] = None
    write_json(folder / "state.json", state)
    result = run_repair(
        root, "exec01", credential=SecretStr("synthetic"), adapter=adapter
    )
    assert result["attempts"] == 1 and result["complete"] == 1
    assert len(adapter.seen) == 1


def test_partial_values_are_frozen_when_repaired_reply_changes_them(prepared, tmp_path):
    from hvs_catalog.benchmark.archive import open_archive
    from hvs_catalog.evaluation.adapt import prediction_from_execution

    bad = quantities()
    bad["quantities"].append(
        {
            "quantity": "distance",
            "values": [{**value("4", quote="bad quote"), "unit": "kpc"}],
        }
    )
    bad["quantities"][1]["values"][0]["evidence"][0]["location"] = "T:p9-b9"
    failure = TransportFailure(
        "connection_failed",
        retryable=True,
        exchange={"transport": {"phase": "connect", "request_started": False}},
    )
    root, paper, _ = closed_benchmark(prepared, tmp_path, [submission(), bad, failure])
    prepare_repair(root, "exec01", policy=quick_policy())
    replacement = quantities([value("530")])
    replacement["quantities"].append(
        {"quantity": "distance", "values": [{**value("4"), "unit": "kpc"}]}
    )
    adapter = ScriptedAdapter([replacement])
    run_repair(root, "exec01", credential=SecretStr("synthetic"), adapter=adapter)
    folder = root / "runs/exec01/network-repair-01"
    jobs = read_file(folder / "state.json")["jobs"]
    facts = read_file(folder / "source-facts.json")
    space = open_archive(root, "exec01", paper)
    try:
        baseline = prediction_from_execution(space, "exec01/" + paper)
    finally:
        space.close()
    assert jobs[0]["baseline"]["data"]["status"] == "partial"
    derived = repaired_prediction(baseline, facts[paper], jobs)
    groups = derived.content.stars[0].quantities
    assert [g.values[0].value for g in groups] == ["520", "4"]
    assert (
        derived.provenance["network_repair"]["conflicts"][0]["quantity"]
        == "radial_velocity"
    )
    validate_fragments(folder, facts, jobs)
    jobs[0]["validations"][0]["values"][0]["values"][0]["value"] = "999"
    with pytest.raises(ValueError, match="differs from its archived reply"):
        validate_fragments(folder, facts, jobs)


def test_repair_scores_new_bundle_and_reproduces_unchanged_original(prepared, tmp_path):
    from test_saved_scoring import saved_reference

    from hvs_catalog.benchmark.archive import open_archive
    from hvs_catalog.benchmark.experiments import score_experiment
    from hvs_catalog.evaluation.adapt import prediction_from_execution
    from hvs_catalog.evaluation.bundle import score_scoring_bundle

    root, paper, _ = closed_benchmark(
        prepared, tmp_path, [submission(), network_failure()]
    )
    source = open_archive(root, "exec01", paper)
    try:
        baseline = prediction_from_execution(source, "exec01/" + paper)
    finally:
        source.close()
    ref = tmp_path / "reference.json"
    ref.write_text(saved_reference(baseline).model_dump_json(indent=2))
    original = score_experiment(root, "exec01", {paper: ref})
    assert original.micro.l2.correct == 0
    original_path = root / "runs/exec01/scoring/scoring-report.json"
    original_hash = file_hash(original_path)
    prepare_repair(root, "exec01", policy=quick_policy())
    run_repair(
        root,
        "exec01",
        credential=SecretStr("synthetic"),
        adapter=ScriptedAdapter([quantities([value("520", "Repair warning")])]),
    )
    report = score_repair(root, "exec01")
    assert report.micro.l2.correct == 1 and report.micro.l2.f1 == 1
    repaired_folder = root / "runs/exec01/network-repair-01"
    frozen_input = read_file(repaired_folder / "scoring/scoring-inputs.json")
    assert baseline.evidence_checks["checked"] == 2
    assert frozen_input["predictions"][0]["evidence_checks"] == {
        "invalid": 0,
        "checked": 3,
        "quote_mismatches": 1,
    }
    assert read_file(repaired_folder / "trace.json")["calls_workspace"] == ".calls"
    audit = read_file(repaired_folder / paper / "evidence_audit.json")
    assert audit["quote_mismatches"][0]["quote"] == "Repair warning"
    assert (
        file_hash(root / audit["metadata"]["result_ref"]["path"])
        == audit["metadata"]["result_ref"]["sha256"]
    )
    assert file_hash(original_path) == original_hash
    assert (
        score_scoring_bundle(
            root / "runs/exec01/network-repair-01/scoring/scoring-inputs.json"
        )
        == report
    )


@pytest.mark.integration
def test_process_exit_after_response_archive_does_not_send_again(prepared, tmp_path):
    root, _, _ = closed_benchmark(prepared, tmp_path, [submission(), network_failure()])
    prepare_repair(root, "exec01", policy=quick_policy())
    child = tmp_path / "exit_after_reply.py"
    child.write_text(
        textwrap.dedent("""
        import os, sys
        sys.path.insert(0, sys.argv[2])
        from pathlib import Path
        from pydantic import SecretStr
        from test_quantities import quantities
        from test_roster import ScriptedAdapter
        from hvs_catalog.benchmark import repair_runtime
        def exit_before_consume(*args, **kwargs):
            os._exit(73)
        repair_runtime._consume = exit_before_consume
        repair_runtime.run_repair(Path(sys.argv[1]), 'exec01',
            credential=SecretStr('synthetic'), adapter=ScriptedAdapter([quantities()]))
    """)
    )
    result = subprocess.run(
        [sys.executable, str(child), str(root), str(Path(__file__).parent)],
        timeout=30,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 73, result.stderr
    adapter = ScriptedAdapter([quantities()])
    status = run_repair(
        root, "exec01", credential=SecretStr("synthetic"), adapter=adapter
    )
    assert status["complete"] == 1 and status["attempts"] == 1
    assert not adapter.seen


def test_workspace_repair_preserves_completed_stages_and_exports_by_paper(tmp_path):
    from materials_helpers import FakeMinerU, make_pdf
    from test_preparation_batch import meta
    from test_roster import TEXT, entry

    from hvs_catalog.benchmark.repair import export_repair_prediction
    from hvs_catalog.benchmark.repair_runtime import run_prepared_repair
    from hvs_catalog.benchmark.repair_source import prepare_execution_repair
    from hvs_catalog.llm import RuntimeConfig
    from hvs_catalog.materials import MaterialsService
    from hvs_catalog.materials.metadata import archive_metadata
    from hvs_catalog.pipeline import (
        ExtractionConfig,
        resume_extraction,
        start_extraction,
    )
    from hvs_catalog.pipeline.acquisition import AcquisitionService

    space = initialize_workspace(tmp_path / "literature", literature=True)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(make_pdf(TEXT))
    imported = AcquisitionService(space).import_local_pdf(
        pdf, arxiv_id="2401.01234v1", provenance="Synthetic repair fixture"
    )
    archive_metadata(space, meta("2401.01234"), {"kind": "synthetic"})
    prep_handle, report = MaterialsService(
        space, runner=FakeMinerU()
    ).prepare_materials(request_key="prepare", paper=imported.canonical_id)
    assert report.outcome == "finished"
    prep = prep_handle.execution_id
    config = ExtractionConfig(runtime=RuntimeConfig(quota_store=tmp_path / "quota.db"))
    config.runtime.max_attempts = 1
    config.runtime.retry_post_send_network_errors = True
    handle = start_extraction(
        space,
        preparation_execution_id=prep,
        request_key="daily-network",
        execution_id="2401.01234",
        configuration=config,
    )
    resume_extraction(
        space,
        handle.execution_id,
        service=CallService(
            config.runtime,
            credential=SecretStr("synthetic"),
            adapter=ScriptedAdapter(
                [
                    submission([entry("A"), entry("B", 2)]),
                    quantities(),
                    network_failure(),
                ]
            ),
        ),
    )
    folder = space.root / ".state/network-repair-01"
    manifest = prepare_execution_repair(
        space.root,
        [handle.execution_id],
        folder,
        policy=quick_policy(initial_probe=True),
    )
    state = read_file(folder / "state.json")
    assert len(state["jobs"]) == 1 and state["jobs"][0]["source_index"] == 1
    adapter = ScriptedAdapter([quantities()])
    result = run_prepared_repair(
        space.root, folder, credential=SecretStr("synthetic"), adapter=adapter
    )
    assert result["complete"] == 1 and len(adapter.seen) == 1
    assert json.loads(adapter.seen[0]["messages"][2]["content"]) == {
        "identifiers": ["B"]
    }
    facts = read_file(folder / "source-facts.json")
    baselines = read_file(folder / "baselines.json")
    paper = next(iter(facts))
    jobs = read_file(folder / "state.json")["jobs"]
    validate_fragments(folder, facts, jobs)
    derived = repaired_prediction(baselines[paper], facts[paper], jobs)
    assert derived.completion == "complete"
    assert (
        derived.content.stars[0].model_dump(mode="json")
        == baselines[paper]["content"]["stars"][0]
    )
    output = space.root / "runs" / handle.execution_id / "network-repair-01"
    export_repair_prediction(space.root, output, derived, facts[paper])
    assert (output / "literature_contribution.json").exists()
    verify_sources(space.root, manifest)
    assert (
        run_prepared_repair(
            space.root, folder, credential=SecretStr("synthetic"), adapter=adapter
        )
        == result
    )
    assert len(adapter.seen) == 1
    with pytest.raises(ValueError, match="new repair folder"):
        prepare_execution_repair(space.root, [handle.execution_id], folder)
    space.close()


def test_tls_circuit_stops_after_bounded_probes():
    policy = RepairPolicy(circuit_threshold=3, initial_probe=True)
    route = {}
    env = {
        "error": "completion_unknown",
        "exchange": {
            "transport": {
                "phase": "connect",
                "request_started": False,
                "exception_types": ["SSLError", "SSLEOFError"],
            }
        },
    }
    for _ in range(3):
        _route_result(route, env, policy, 100)
    assert route["open_until"] == 400
    assert not route.get("initial_probe_passed")
    for i in range(3):
        route["half_open"] = True
        _route_result(route, env, policy, 400 + 300 * i)
    assert route["stopped"] == "circuit_exhausted"
    healthy = {}
    _route_result(
        healthy,
        {"error": "invalid_tool_arguments", "exchange": {"status": 200}},
        policy,
        0,
    )
    assert healthy["initial_probe_passed"]
