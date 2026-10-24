"""Cumulative and local recoveries preserve science and reject unbound provenance."""

import json
import shutil

import pytest
from catalog_input_helpers import cumulative_repair, local_repair, save
from test_catalog_delivery import file_ref, publish_final

from hvs_catalog.catalogue import build_catalog, verify_catalog
from hvs_catalog.catalogue.delivery import selection_from_results
from hvs_catalog.catalogue.inputs import load_paper
from hvs_catalog.catalogue.recovery import recover_arguments
from hvs_catalog.records.catalog import CatalogSelection


@pytest.mark.parametrize("partial", [False, True])
def test_local_recovery_preserves_accepted_groups_and_final_delivery(
    prepared, tmp_path, partial
):
    selected, _ = local_repair(prepared, partial=partial)
    loaded = load_paper(selected)
    values = {
        q.quantity: [v.value for v in q.values]
        for q in loaded.science.stars[0].quantities
    }
    assert values == (
        {"distance": ["8"], "radial_velocity": ["520"]}
        if partial
        else {"radial_velocity": ["520"]}
    )
    assert loaded.source.result_status == "complete"
    assert loaded.source.objects[0].status == "complete"
    assert loaded.source.objects[0].repair_stage_id
    root = tmp_path / "delivered"
    history = root / "history/extraction"
    history.parent.mkdir(parents=True)
    shutil.copytree(prepared[0].root, history)
    publish_final(root, history, selected.run_ref)
    spec = selection_from_results(root, "local-synthetic")
    output = tmp_path / "catalog"
    manifest = build_catalog(spec, output)
    assert manifest == verify_catalog(output)
    star = json.loads((output / manifest.stars[0].file.path).read_bytes())
    assert {
        q["quantity"]: [v["value"] for v in q["values"]] for q in star["quantities"]
    } == values
    assert all(
        v["source"] in star["sources"] for q in star["quantities"] for v in q["values"]
    )


def rebind_recovery(selected, folder, data):
    """Reseal the container so deeper provenance checks, not only hashes, are tested."""
    root = folder.parent.parent
    save(folder / "recoveries.json", data)
    run_path = root / selected.run_ref.path
    run = json.loads(run_path.read_bytes())
    run["recoveries_ref"] = file_ref(root, folder / "recoveries.json").model_dump()
    retained_path = root / run["outputs"]["retained-prediction.json"]["path"]
    retained = json.loads(retained_path.read_bytes())
    retained["provenance"]["network_repair"]["recoveries_ref"] = run["recoveries_ref"]
    save(retained_path, retained)
    run["outputs"]["retained-prediction.json"] = file_ref(
        root, retained_path
    ).model_dump()
    save(run_path, run)
    selected.run_ref = file_ref(root, run_path)


@pytest.mark.parametrize(
    "fault",
    [
        "facts",
        "baseline",
        "arguments",
        "receipt",
        "source",
        "existing",
        "fragment",
        "duplicate",
        "wrong_target",
    ],
)
def test_local_recovery_rejects_changed_provenance(prepared, fault):
    selected, folder = local_repair(prepared)
    if fault in {"facts", "baseline"}:
        path = folder / ("source-facts.json" if fault == "facts" else "baselines.json")
        data = json.loads(path.read_bytes())
        paper = next(iter(data))
        if fault == "facts":
            data[paper]["quantities"]["0"]["data"]["status"] = "complete"
        else:
            data[paper]["content"]["stars"][0]["quantities"][0]["values"][0][
                "value"
            ] = "999"
        save(path, data)
    else:
        data = json.loads((folder / "recoveries.json").read_bytes())
        item = data["targets"][0]
        if fault == "arguments":
            item["effective_arguments"] = item["effective_arguments"].replace(
                "520", "999"
            )
        elif fault == "receipt":
            item["receipt"]["scalar_sequence_sha256"] = "0" * 64
        elif fault == "source":
            item["source_response"] = item["source_request"]
        elif fault == "existing":
            item["preserved_existing_groups"] = []
        elif fault == "fragment":
            item["fragment"]["values"][0]["values"][0]["value"] = "999"
        elif fault == "duplicate":
            data["targets"].append(item.copy())
        else:
            item["source_index"] = 42
        rebind_recovery(selected, folder, data)
    with pytest.raises(ValueError):
        load_paper(selected)


@pytest.mark.parametrize("recover", [False, True])
def test_cumulative_repair_reuses_roster_and_previous_science(
    prepared, tmp_path, recover
):
    selected, _ = cumulative_repair(prepared, recover=recover)
    loaded = load_paper(selected)
    assert len(loaded.science.stars) == 2
    assert [s.quantities[0].values[0].value for s in loaded.science.stars] == [
        "480",
        "530",
    ]
    assert [o.source_index for o in loaded.source.objects] == [0, 1]
    assert [o.status for o in loaded.source.objects] == ["complete", "complete"]
    assert loaded.source.roster_status == "complete"
    manifest = build_catalog(
        CatalogSelection(data_version="synthetic", papers=[selected]),
        tmp_path / "catalog",
    )
    assert len(manifest.stars) == 2
    verify_catalog(tmp_path / "catalog")


@pytest.mark.parametrize(
    "fault", ["previous_hash", "previous_roster", "recovery_response", "retained_stage"]
)
def test_cumulative_repair_rejects_changed_lineage(prepared, fault):
    selected, folder = cumulative_repair(prepared)
    root = prepared[0].root
    run_path = root / selected.run_ref.path
    run = json.loads(run_path.read_bytes())
    if fault.startswith("previous"):
        path = folder / "previous-jobs.json"
        jobs = json.loads(path.read_bytes())
        jobs = [j for j in jobs if j["kind"] != "roster"]
        save(path, jobs)
        if fault == "previous_roster":
            from hvs_catalog.records.common import sha256_of

            manifest = json.loads((folder / "manifest.json").read_bytes())
            manifest["previous_jobs_sha256"] = sha256_of(jobs)
            save(folder / "manifest.json", manifest)
            run["manifest_ref"] = file_ref(root, folder / "manifest.json").model_dump()
    else:
        retained_path = root / run["outputs"]["retained-prediction.json"]["path"]
        retained = json.loads(retained_path.read_bytes())
        if fault == "retained_stage":
            retained["provenance"]["network_repair"]["stages"][0]["fragment"][
                "status"
            ] = "partial"
        else:
            path = folder / "structure-recovery.json"
            recovery = json.loads(path.read_bytes())
            recovery["accepted"][0]["source_response"] = recovery["accepted"][0][
                "source_request"
            ]
            save(path, recovery)
            retained["provenance"]["network_repair"]["structure_recovery_ref"] = (
                file_ref(root, path).model_dump()
            )
        save(retained_path, retained)
        run["outputs"]["retained-prediction.json"] = file_ref(
            root, retained_path
        ).model_dump()
    save(run_path, run)
    selected.run_ref = file_ref(root, run_path)
    with pytest.raises(ValueError):
        load_paper(selected)


@pytest.mark.parametrize(
    "arguments",
    [
        '{"quantities":[],"notes":"","extra":7}',
        '{"quantities":[{"quantity":"distance","values":[{"value":"8"}]}],"notes":"","notes":"duplicate"}',
        "not JSON",
    ],
)
def test_recovery_refuses_to_discard_content(arguments):
    with pytest.raises(ValueError):
        recover_arguments(arguments)
