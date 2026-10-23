"""Route limits select original images without accepting omitted visual evidence."""

import base64
import copy
import io
import json
import zipfile

import pytest
from materials_helpers import FakeMinerU, prepared_space
from pydantic import SecretStr
from test_pdf_images import IMAGE, PNG
from test_roster import ScriptedAdapter, entry, submission

from hvs_catalog.llm import ModelConfig, RuntimeConfig, TokenDanceAdapter
from hvs_catalog.llm.managed import CallService
from hvs_catalog.llm.prompts import PromptInput, assemble_blueprint
from hvs_catalog.materials import MaterialsService
from hvs_catalog.materials.mineru import read_result
from hvs_catalog.pipeline import ExtractionConfig, resume_extraction, start_extraction
from hvs_catalog.pipeline.configuration import preview_extraction_prompt
from hvs_catalog.pipeline.results import saved_quantities
from hvs_catalog.pipeline.roster import saved_roster
from hvs_catalog.records.material import supplied_locations
from hvs_catalog.workspace import backup_workspace, open_workspace, restore_workspace


def material(kinds):
    return PromptInput(
        manuscript="Synthetic manuscript.",
        images=[
            {
                "label": f"LOCATION I:p1-b{i}",
                "kind": kind,
                "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(PNG).decode()
                },
            }
            for i, kind in enumerate(kinds)
        ],
    )


@pytest.mark.parametrize(
    "kinds,expected",
    [
        (["image"] * 65 + ["table"] * 14, list(range(65, 79)) + list(range(36))),
        (["table"] * 79, list(range(50))),
        (["image"] * 79, list(range(50))),
        (["image"] * 4 + ["table"] * 2, list(range(6))),
        (["image"] * 50, list(range(50))),
    ],
)
def test_glm_prioritizes_tables_then_document_order(kinds, expected):
    inputs = material(kinds)
    config = ExtractionConfig(
        default_model=ModelConfig(model="glm-5.3-flash"), step_models={}
    )
    preview = preview_extraction_prompt(config, "roster", inputs)
    sent = preview["prepared"]["payload"]["messages"][1]["content"]
    assert [p["text"] for p in sent[1::2]] == [f"LOCATION I:p1-b{i}" for i in expected]
    assert preview["images"]["sent"] == len(expected)
    assert len(preview["images"]["omitted"]) == len(kinds) - len(expected)
    assert len(inputs.images) == len(kinds)


def test_stage_limits_are_independent_and_raw_overflow_is_rejected():
    inputs = material(["image"] * 79)
    config = ExtractionConfig(
        default_model=ModelConfig(model="glm-5.3-flash"),
        step_models={"quantities": ModelConfig(model="mimo-v2.6-flash")},
    )
    assert preview_extraction_prompt(config, "roster", inputs)["images"]["sent"] == 50
    numerical = preview_extraction_prompt(config, "quantities", inputs, ["A"])
    assert numerical["images"]["sent"] == 79
    request = assemble_blueprint("roster").request(
        ModelConfig(model="mimo-v2.6-flash"), inputs
    )
    request.model = ModelConfig(model="glm-5.3-flash")
    with pytest.raises(ValueError, match="image count"):
        TokenDanceAdapter().prepare(request)


def test_image_locations_follow_actual_bytes_within_a_shared_block():
    import hashlib

    digest = hashlib.sha256(PNG).hexdigest()
    locations = {
        "T:p1-b0": {"text": "Unchanged author text."},
        "I:p1-b1": {
            "images": [
                {"path": "first.png", "sha256": digest},
                {"path": "second.png", "sha256": digest},
            ]
        },
        "I:p1-b2": {"images": [{"path": "omitted.png", "sha256": digest}]},
    }
    content = [
        {"type": "text", "text": "LOCATION I:p1-b1"},
        {
            "type": "image_url",
            "image_url": {"url": material(["image"]).images[0].image_url.url},
        },
    ]
    selected = supplied_locations(locations, content)
    assert set(selected) == {"T:p1-b0", "I:p1-b1"}
    assert selected["I:p1-b1"]["images"] == locations["I:p1-b1"]["images"][:1]
    assert len(locations["I:p1-b1"]["images"]) == 2
    bad = copy.deepcopy(content)
    bad[1]["image_url"]["url"] = (
        "data:image/png;base64," + base64.b64encode(PNG + b"changed").decode()
    )
    with pytest.raises(ValueError, match="differs from the fixed source"):
        supplied_locations(locations, bad)
    bad[0]["text"] = "LOCATION I:unknown"
    with pytest.raises(ValueError, match="no fixed source location"):
        supplied_locations(locations, bad)


class ManyImages(FakeMinerU):
    def parse(self, path, page_count):
        blocks = [
            {
                "index": i,
                "type": "table" if i >= 65 else "chart",
                "bbox": [0, 0, 1, 1],
                "content": [
                    {
                        "type": "table_body" if i >= 65 else "chart_body",
                        "bbox": [0, 0, 1, 1],
                        "image_path": IMAGE,
                        "content": "",
                    }
                ],
            }
            for i in range(79)
        ]
        document = {
            "schema": "docvortex.middle",
            "is_full_document": True,
            "pages": [{"page_idx": 0, "blocks": blocks}],
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("middle.json", json.dumps(document))
            archive.writestr("paper.md", "")
            archive.writestr(IMAGE, PNG)
        raw = buffer.getvalue()
        return raw, read_result(raw, page_count)


@pytest.mark.parametrize(
    "quantity_model,expected", [("glm-5.3-flash", 50), ("mimo-v2.6-flash", 79)]
)
def test_selected_images_survive_corrections_both_stages_and_backup(
    tmp_path, quantity_model, expected
):
    space, imported = prepared_space(tmp_path)
    try:
        service = MaterialsService(space, runner=ManyImages())
        prep, report = service.prepare_materials(
            request_key="many-images", paper=imported.canonical_id
        )
        assert report.outcome == "finished", report.errors
        config = ExtractionConfig(
            default_model=ModelConfig(model="glm-5.3-flash"),
            step_models={"quantities": ModelConfig(model=quantity_model)},
            runtime=RuntimeConfig(quota_store=tmp_path / "quota.db"),
        )

        def roster(location):
            star = entry()
            star["identifiers"][0]["evidence"] = [{"location": location}]
            star["contribution_type"]["evidence"] = [{"location": location}]
            return submission([star])

        numerical = {
            "quantities": [
                {
                    "quantity": "radial_velocity",
                    "values": [
                        {
                            "value": "42",
                            "unit": "km/s",
                            "evidence": [{"location": "I:p1-b78"}],
                        }
                    ],
                }
            ],
            "notes": "",
        }
        adapter = ScriptedAdapter([roster("I:p1-b64"), roster("I:p1-b78"), numerical])
        handle = start_extraction(
            space,
            preparation_execution_id=prep.execution_id,
            configuration=config,
            request_key="limited",
        )
        calls = CallService(
            config.runtime, credential=SecretStr("synthetic"), adapter=adapter
        )
        report = resume_extraction(space, handle.execution_id, service=calls)
        assert report.outcome == "finished" and report.result_status == "complete", (
            report.errors
        )
        assert len(adapter.seen) == 3
        for payload, count in zip(adapter.seen, [50, 50, expected], strict=True):
            assert (
                sum(p["type"] == "image_url" for p in payload["messages"][1]["content"])
                == count
            )
        assert "reasoning_content" in adapter.seen[1]["messages"][2]
        retained_roster = saved_roster(space, handle.execution_id)
        assert retained_roster.data.source_round == 1
        assert (
            saved_quantities(space, handle.execution_id, retained_roster)[0].data.status
            == "complete"
        )
        before = len(adapter.seen)
        assert resume_extraction(space, handle.execution_id).outcome == "finished"
        assert len(adapter.seen) == before
        backup_workspace(space, tmp_path / "backup")
        restore_workspace(tmp_path / "backup", tmp_path / "restored")
        restored = open_workspace(tmp_path / "restored")
        try:
            assert saved_roster(restored, handle.execution_id).data.status == "complete"
        finally:
            restored.close()
    finally:
        space.close()
