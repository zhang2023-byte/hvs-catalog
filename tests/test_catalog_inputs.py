"""Exercise real saved-extraction interfaces with existing synthetic pipeline fixtures."""

import json

import pytest
from test_quantities import quantities, run_full, value
from test_roster import submission

from hvs_catalog.catalogue import build_catalog, verify_catalog
from hvs_catalog.catalogue.inputs import load_paper, selection_from_index
from hvs_catalog.cli import main
from hvs_catalog.pipeline.results import current_result
from hvs_catalog.records.catalog import CatalogSelection, PaperSelection


def test_native_saved_result_and_cli(prepared, tmp_path, capsys):
    space = prepared[0]
    handle, report, _, _ = run_full(
        prepared, [submission(), quantities([value("520", "A and Alias-A")])]
    )
    assert report.result_status == "complete"
    run_ref, summary = current_result(space, handle.execution_id)
    selection = PaperSelection(workspace=str(space.root), run_ref=run_ref)
    before = space.read_file(run_ref)
    loaded = load_paper(selection)
    assert loaded.source.objects[0].status == "complete"
    assert loaded.source.pdf_origin["kind"] == "arxiv"
    assert loaded.source.pdf_sha256
    assert loaded.science_bytes == space.read_file(summary.result_ref)
    output = tmp_path / "public"
    spec = CatalogSelection(data_version="synthetic", papers=[selection, selection])
    manifest = build_catalog(spec, output)
    assert len(manifest.papers) == 1 and len(manifest.stars) == 1
    assert space.read_file(run_ref) == before
    assert main(["catalog", "verify", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["valid"]
    assert verify_catalog(output) == manifest


@pytest.mark.parametrize("derived", [False, True])
def test_index_both_reference_spellings(tmp_path, derived):
    old = ("run_ref", "result_ref", "evidence_audit_ref")
    new = ("run.json", "literature_contribution.json", "evidence_audit.json")
    outputs = {
        key: {"path": name, "sha256": "a" * 64}
        for key, name in zip(new if derived else old, new)
    }
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"papers": [{"outputs": outputs}]}))
    spec = selection_from_index(path, tmp_path / "workspace", "fictional")
    assert spec.papers[0].run_ref.path == "run.json"


def test_derived_saved_result_replays_without_gold(prepared, tmp_path):
    from pydantic import SecretStr
    from test_benchmark_repair import network_failure, quick_policy
    from test_roster import ScriptedAdapter

    from hvs_catalog.benchmark.repair import (
        export_repair_prediction,
        repaired_prediction,
    )
    from hvs_catalog.benchmark.repair_runtime import run_prepared_repair
    from hvs_catalog.benchmark.repair_source import prepare_execution_repair
    from hvs_catalog.catalogue.inputs import digest, json_bytes
    from hvs_catalog.records.common import ArtifactRef

    space = prepared[0]
    prepared[2].runtime.max_attempts = 1
    prepared[2].runtime.retry_post_send_network_errors = True
    handle, _, _, _ = run_full(prepared, [submission(), network_failure()])
    folder = space.root / ".state/network-repair-01"
    prepare_execution_repair(
        space.root,
        [handle.execution_id],
        folder,
        policy=quick_policy(initial_probe=True),
    )
    run_prepared_repair(
        space.root,
        folder,
        credential=SecretStr("synthetic"),
        adapter=ScriptedAdapter([quantities()]),
    )
    facts = json.loads((folder / "source-facts.json").read_bytes())
    baselines = json.loads((folder / "baselines.json").read_bytes())
    paper = next(iter(facts))
    jobs = json.loads((folder / "state.json").read_bytes())["jobs"]
    prediction = repaired_prediction(baselines[paper], facts[paper], jobs)
    target = space.root / "runs" / handle.execution_id / "derived"
    export_repair_prediction(space.root, target, prediction, facts[paper])

    def ref(path):
        return ArtifactRef(
            path=str(path.relative_to(space.root)), sha256=digest(path.read_bytes())
        )

    outputs = {
        name: ref(target / name).model_dump()
        for name in (
            "retained-prediction.json",
            "overlay.json",
            "literature_contribution.json",
            "evidence_audit.json",
        )
    }
    run = {
        "format": "hvs.network-repair-result",
        "version": 1,
        "arxiv_id": paper,
        "source_execution_id": handle.execution_id,
        "completion": prediction.completion,
        "delivered": True,
        "format_valid": True,
        "manifest_ref": ref(folder / "manifest.json").model_dump(),
        "outputs": outputs,
    }
    (target / "run.json").write_bytes(json_bytes(run))
    selected = PaperSelection(
        workspace=str(space.root), run_ref=ref(target / "run.json")
    )
    loaded = load_paper(selected)
    assert loaded.science == prediction.content
    assert loaded.source.objects[0].status == "complete"
    assert loaded.source.objects[0].repair_stage_id
    build_catalog(
        CatalogSelection(data_version="derived", papers=[selected]),
        tmp_path / "derived-catalog",
    )
