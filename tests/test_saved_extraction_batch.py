"""Cross-workspace CLI batching preserves fixed inputs and feeds program scoring."""

import json

import pytest
from llm_helpers import completion, endpoint
from materials_helpers import FakeMinerU, make_pdf
from test_acquisition_lifecycle import make_meta
from test_extraction_configuration_cli import cli
from test_quantities import quantities
from test_roster import TEXT, submission

from hvs_catalog.evaluation.adapt import prediction_from_execution
from hvs_catalog.evaluation.saved import score_saved_manifest
from hvs_catalog.llm import RuntimeConfig
from hvs_catalog.materials import MaterialsService, reuse_prepared_materials
from hvs_catalog.materials.metadata import archive_metadata
from hvs_catalog.pipeline import ExtractionConfig, start_extraction
from hvs_catalog.pipeline.acquisition import AcquisitionService
from hvs_catalog.pipeline.saved_batch import run_saved_batch
from hvs_catalog.records.rules import current_rules_ref
from hvs_catalog.workspace import initialize_workspace


@pytest.mark.integration
def test_cross_workspace_batch_cli_and_scoring(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENDANCE_API_KEY", "synthetic")

    def reply(request, number):
        body = completion(request)
        tool = request["tools"][0]["function"]["name"]
        body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
            json.dumps(submission() if tool == "submit_roster" else quantities())
        )
        return {"body": body}

    spaces, rows, scores = [], [], []
    with endpoint(reply) as (url, seen, _):
        try:
            config = ExtractionConfig(
                runtime=RuntimeConfig(base_url=url, quota_store=tmp_path / "quota.db")
            )
            for i, paper in enumerate(("2601.99998", "2601.99999")):
                source = initialize_workspace(tmp_path / f"original-{i}")
                spaces.append(source)
                pdf = tmp_path / f"paper-{i}.pdf"
                pdf.write_bytes(make_pdf(TEXT))
                AcquisitionService(source).import_local_pdf(
                    pdf, arxiv_id=paper + "v1", provenance="Synthetic batch fixture"
                )
                prep, report = MaterialsService(
                    source, runner=FakeMinerU()
                ).prepare_materials(request_key="prepare", paper=paper)
                assert report.outcome == "finished"
                target = initialize_workspace(tmp_path / f"reused-{i}")
                spaces.append(target)
                ref = reuse_prepared_materials(source, prep.execution_id, target)
                archive_metadata(target, make_meta(paper, 1), {"kind": "synthetic"})
                handle = start_extraction(
                    target,
                    prepared_source_ref=ref,
                    request_key="batch",
                    configuration=config,
                )
                rows.append(
                    {
                        "arxiv_id": paper,
                        "workspace": target.root.name,
                        "execution_id": handle.execution_id,
                    }
                )
            path = tmp_path / "batch.json"
            path.write_text(json.dumps({"tasks": rows}))
            check = cli("extract", "batch", "--manifest", path, "--check-only")
            assert check.returncode == 0, check.stderr
            assert json.loads(check.stdout)["outcome"] == "ready" and seen == []
            result = cli("extract", "batch", "--manifest", path, "--concurrency", 2)
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout)["outcome"] == "finished"
            assert len(seen) == 4
            assert cli("extract", "batch", "--manifest", path).returncode == 0
            assert len(seen) == 4
            for row, target in zip(rows, spaces[1::2], strict=True):
                pred = prediction_from_execution(target, row["execution_id"])
                evidence = [{"page": 1, "location": "Synthetic paragraph"}]
                # Independent expected science; only material binding comes from the fixture.
                answer = {
                    "metadata": {
                        "arxiv_id": row["arxiv_id"],
                        "version": "v1",
                        "title": "Synthetic reference",
                        "authors": ["A. Author"],
                        "rules": current_rules_ref(),
                        "material": {
                            "path": "paper.pdf",
                            "sha256": pred.materials.files[0].sha256,
                        },
                    },
                    "stars": [
                        {
                            "identifiers": [{"value": "A", "evidence": evidence}],
                            "contribution_type": {
                                "value": "candidates_found",
                                "evidence": evidence,
                            },
                            "quantities": [
                                {
                                    "quantity": "radial_velocity",
                                    "values": [
                                        {
                                            "value": "520",
                                            "unit": "km/s",
                                            "evidence": evidence,
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                    "notes": [],
                }
                reference = tmp_path / (row["arxiv_id"] + ".json")
                reference.write_text(json.dumps(answer))
                scores.append({**row, "reference": reference.name})
            manifest = tmp_path / "score.json"
            manifest.write_text(json.dumps({"papers": scores}))
            report = score_saved_manifest(manifest, tmp_path / "scored")
            assert report.micro.l1.tp == report.micro.l2.correct == 2
            assert report.micro.l2.f1 == 1 and len(seen) == 4
        finally:
            for space in spaces:
                space.close()


def test_bad_manifest_is_rejected_before_model_service(prepared, tmp_path, monkeypatch):
    from hvs_catalog.llm import managed

    space, prep, config = prepared
    handle = start_extraction(
        space, preparation_execution_id=prep, request_key="fixed", configuration=config
    )
    path = tmp_path / "batch.json"
    path.write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "arxiv_id": "2601.99999",
                        "workspace": str(space.root),
                        "execution_id": handle.execution_id,
                    }
                ]
            }
        )
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("A bad manifest must not construct a model service.")

    monkeypatch.setattr(managed, "CallService", forbidden)
    with pytest.raises(ValueError, match="different batch paper"):
        run_saved_batch(path)
    path.write_text('{"tasks": [], "tasks": []}')
    with pytest.raises(ValueError):
        run_saved_batch(path)
