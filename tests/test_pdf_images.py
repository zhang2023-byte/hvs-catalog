"""Original image input and explicitly unverified visual evidence, without live calls."""

import base64
import io
import json
import zipfile

import pytest
from llm_helpers import endpoint
from materials_helpers import FakeMinerU, prepared_space
from pydantic import SecretStr, ValidationError
from test_roster import entry, run, submission

from hvs_catalog.llm import ModelConfig, RuntimeConfig, TokenDanceAdapter
from hvs_catalog.llm.config import CallRequest, Message
from hvs_catalog.materials import MaterialsService
from hvs_catalog.materials.mineru import read_result
from hvs_catalog.pipeline import ExtractionConfig
from hvs_catalog.pipeline.roster import (
    material_prompt,
)
from hvs_catalog.records.common import RecordRef

# A tiny valid PNG; mock responses test provenance, not visual recognition quality.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jM1kAAAAASUVORK5CYII="
)
IMAGE = "images/figure.png"
CAPTION = "Figure 1. The labeled objects are unbound candidates."
HALLUCINATION = "Generated claim: the velocity is 999999 km/s."
TABLE = "<table><tr><td>A</td><td>42</td></tr></table>"


def image_zip(*, missing=False, caption=True, footnote=False):
    body = {
        "type": "chart_body",
        "bbox": [0, 0.2, 1, 0.5],
        "image_path": IMAGE,
        "content": HALLUCINATION,
    }
    blocks = [
        {
            "index": 0,
            "type": "paragraph",
            "bbox": [0, 0, 1, 0.1],
            "content": "A is unbound.",
        },
        {
            "index": 1,
            "type": "chart",
            "bbox": [0, 0.2, 1, 0.5],
            "content": [body]
            + (
                [
                    {
                        "type": "chart_caption",
                        "bbox": [0, 0.52, 1, 0.58],
                        "content": CAPTION,
                    }
                ]
                if caption
                else []
            ),
        },
        {
            "index": 2,
            "type": "table",
            "bbox": [0, 0.6, 1, 0.9],
            "content": [
                {"type": "table_body", "bbox": [0, 0.6, 1, 0.9], "content": TABLE}
            ]
            + (
                [
                    {
                        "type": "table_footnote",
                        "bbox": [0, 0.92, 1, 0.95],
                        "content": "RV in km/s.",
                    }
                ]
                if footnote
                else []
            ),
        },
    ]
    doc = {
        "schema": "docvortex.middle",
        "is_full_document": True,
        "pages": [{"page_idx": 0, "blocks": blocks}],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("middle.json", json.dumps(doc))
        z.writestr("paper.md", CAPTION + HALLUCINATION + TABLE)
        if not missing:
            z.writestr(IMAGE, PNG)
    return buffer.getvalue()


class ImageMinerU(FakeMinerU):
    def parse(self, path, page_count):
        raw = image_zip()
        return raw, read_result(raw, page_count)


@pytest.fixture
def image_space(tmp_path):
    space, imported = prepared_space(tmp_path)
    service = MaterialsService(space, runner=ImageMinerU())
    prep, report = service.prepare_materials(
        request_key="image", paper=imported.canonical_id
    )
    assert report.outcome == "finished", report.errors
    bundle = service.get_prepared_input(prep.execution_id)
    source = space.store().read_record(
        RecordRef.model_validate(bundle["prepared_source_ref"])
    )
    yield space, prep, bundle, source
    space.close()


def test_caption_and_table_preserved_but_generated_description_excluded():
    parsed = read_result(image_zip(), 1)
    assert parsed["blocks"][1]["text"] == CAPTION
    assert parsed["blocks"][2]["text"] == TABLE
    assert HALLUCINATION not in json.dumps(parsed["blocks"])
    assert parsed["blocks"][1]["image_files"] == [IMAGE]
    assert parsed["images"][IMAGE]["media_type"] == "image/png"
    uncaptained = read_result(image_zip(caption=False), 1)
    assert uncaptained["blocks"][1]["text"] == ""
    assert uncaptained["blocks"][1]["image_files"] == [IMAGE]
    missing = read_result(image_zip(missing=True), 1)
    assert any("absent" in gap for gap in missing["preparation_issues"])
    assert missing["blocks"][1]["image_files"] == []


def test_composite_regions_cover_author_text_without_moving_the_body():
    parsed = read_result(image_zip(footnote=True), 1)
    chart, table = parsed["blocks"][1:]
    assert chart["bbox"] == [0, 0.2, 1, 0.58]
    assert chart["body_bbox"] == [0, 0.2, 1, 0.5]
    assert chart["regions"] == [
        {
            "type": "chart_body",
            "bbox": [0, 0.2, 1, 0.5],
            "text": "",
            "image_files": [IMAGE],
        },
        {
            "type": "chart_caption",
            "bbox": [0, 0.52, 1, 0.58],
            "text": CAPTION,
            "image_files": [],
        },
    ]
    assert table["bbox"] == [0, 0.6, 1, 0.95]
    assert table["text"] == TABLE + "\nRV in km/s."
    assert table["regions"][1]["bbox"] == [0, 0.92, 1, 0.95]
    assert HALLUCINATION not in json.dumps(parsed["blocks"])


@pytest.mark.parametrize("bbox", [[0, 0, 2, 1], [0, 0.5, 1, 0.2], None])
def test_invalid_caption_coordinates_are_rejected(bbox):
    with zipfile.ZipFile(io.BytesIO(image_zip())) as z:
        doc = json.loads(z.read("middle.json"))
    doc["pages"][0]["blocks"][1]["content"][1]["bbox"] = bbox
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("middle.json", json.dumps(doc))
        z.writestr("paper.md", CAPTION)
        z.writestr(IMAGE, PNG)
    with pytest.raises(ValueError, match="bounding box"):
        read_result(buffer.getvalue(), 1)


@pytest.mark.parametrize(
    "model",
    ["deepseek-v4.1-flash", "glm-5.3-flash", "mimo-v2.6-flash", "qwen3.8-flash"],
)
def test_image_protocol_supports_profiles_and_rejects_local_links(model):
    url = "data:image/png;base64," + base64.b64encode(PNG).decode()
    message = Message(
        role="user",
        content=[
            {"type": "text", "text": "Read the figure."},
            {"type": "image_url", "image_url": {"url": url}},
        ],
    )
    request = CallRequest(model=ModelConfig(model=model), messages=[message])
    wire = TokenDanceAdapter().prepare(request)["payload"]
    assert wire["messages"][0]["content"][1]["image_url"]["url"] == url
    with pytest.raises(ValidationError, match="base64 data URL"):
        Message(
            role="user",
            content=[{"type": "image_url", "image_url": {"url": "images/figure.png"}}],
        )
    with pytest.raises(ValidationError, match="user message"):
        Message(role="system", content=message.content)


@pytest.mark.integration
@pytest.mark.parametrize(
    "model", ["deepseek-v4.1-flash", "glm-5.3-flash", "mimo-v2.6-flash"]
)
def test_image_bytes_reach_http_endpoint(image_space, tmp_path, model):
    space, _, bundle, source = image_space
    image = material_prompt(space, bundle, source).images[0]
    request = CallRequest(
        model=ModelConfig(model=model),
        messages=[
            Message(
                role="user",
                content=[
                    {"type": "text", "text": image.label},
                    {"type": "image_url", "image_url": image.image_url.model_dump()},
                ],
            )
        ],
    )
    adapter = TokenDanceAdapter()
    with endpoint() as (url, seen, _):
        runtime = RuntimeConfig(
            base_url=url, quota_store=tmp_path / "http-quota.sqlite"
        )
        exchange = adapter.send(
            adapter.prepare(request)["payload"], runtime, SecretStr("synthetic")
        )
        assert adapter.parse(exchange, request)["content"] == "SYN-1=42"
    received = seen[0]["messages"][0]["content"][1]["image_url"]["url"]
    assert base64.b64decode(received.split(",", 1)[1]) == PNG


def test_image_and_caption_evidence_saved_without_transcription(image_space, tmp_path):
    from hvs_catalog.pipeline.roster import saved_roster

    space, prep, _bundle, _source = image_space
    payload = submission([entry()])
    payload["stars"][0]["identifiers"][0]["evidence"] = [{"location": "I:p1-b1"}]
    payload["stars"][0]["contribution_type"]["evidence"] = [
        {"location": "T:p1-b1", "quote": CAPTION}
    ]
    config = ExtractionConfig(runtime=RuntimeConfig(quota_store=tmp_path / "quota.db"))
    handle, report, adapter, _ = run((space, prep.execution_id, config), [payload])
    assert report.outcome == "finished", report.errors
    star = saved_roster(space, handle.execution_id).data.stars[0]
    assert star.identifiers[0].evidence[0].model_dump() == {"location": "I:p1-b1"}
    sent = adapter.seen[0]["messages"][1]["content"]
    assert sent[1]["text"] == "LOCATION I:p1-b1"
    assert base64.b64decode(sent[2]["image_url"]["url"].split(",", 1)[1]) == PNG


def test_image_quote_is_rejected(image_space, tmp_path):
    from hvs_catalog.pipeline.roster import saved_roster

    space, prep, _, _ = image_space
    payload = submission([entry()])
    payload["stars"][0]["identifiers"][0]["evidence"] = [
        {"location": "I:p1-b1", "quote": "A"}
    ]
    config = ExtractionConfig(runtime=RuntimeConfig(quota_store=tmp_path / "quota.db"))
    handle, report, _, _ = run((space, prep.execution_id, config), [payload])
    assert report.outcome == "finished" and report.result_status == "failed"
    assert saved_roster(space, handle.execution_id).data.status == "failed"
