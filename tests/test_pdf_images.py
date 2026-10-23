"""Original image input and explicitly unverified visual evidence, without live calls."""

import base64
import copy
import hashlib
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
    EvidenceLocationError,
    _hydrate_sources,
    material_prompt,
)
from hvs_catalog.records.common import RecordRef
from hvs_catalog.records.file_checks import verification_scope
from hvs_catalog.records.pdf import pdf_evidence_block
from hvs_catalog.records.source import EvidenceRecord
from hvs_catalog.records.validate import validate_collection
from hvs_catalog.workspace import backup_workspace, open_workspace, restore_workspace

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
    assert any("absent" in gap for gap in missing["gaps"])
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


def test_caption_and_image_evidence_use_their_respective_regions(image_space):
    space, _, _, source = image_space
    text = pdf_evidence_block(source, space.root, "raw", 1, "p1-b1")
    image = pdf_evidence_block(
        source, space.root, "raw", 1, "p1-b1", require_image=True
    )
    assert text["bbox"] == [0, 0.52, 1, 0.58]
    assert text["text"] == CAPTION and text["image_files"] == [IMAGE]
    assert image["bbox"] == [0, 0.2, 1, 0.5]
    assert text["body_bbox"] == image["body_bbox"] == [0, 0.2, 1, 0.5]
    text["regions"][1]["bbox"][1] = 0
    assert pdf_evidence_block(source, space.root, "raw", 1, "p1-b1")["bbox"] == [
        0,
        0.52,
        1,
        0.58,
    ]


def test_legacy_composite_evidence_keeps_its_fixed_bbox(image_space):
    space, _, _, source = image_space
    source = source.model_copy(deep=True)
    file = next(f for f in source.data.files if f.role == "pdf_index")
    path = space.root / file.path
    mapping = json.loads(path.read_text())
    chart = mapping["blocks"][1]
    chart["bbox"] = chart.pop("body_bbox")
    chart.pop("regions")
    payload = json.dumps(mapping).encode()
    path.write_bytes(payload)
    file.sha256 = hashlib.sha256(payload).hexdigest()
    assert pdf_evidence_block(source, space.root, "raw", 1, "p1-b1")["bbox"] == [
        0,
        0.2,
        1,
        0.5,
    ]
    assert pdf_evidence_block(
        source, space.root, "raw", 1, "p1-b1", require_image=True
    )["bbox"] == [0, 0.2, 1, 0.5]


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


def test_image_bound_to_pdf_and_sent_as_image_not_markdown_link(image_space):
    space, _, bundle, source = image_space
    inputs = material_prompt(space, bundle, source)
    assert HALLUCINATION not in inputs.manuscript
    assert CAPTION in inputs.manuscript and TABLE in inputs.manuscript
    assert len(inputs.images) == 1
    assert base64.b64decode(inputs.images[0].image_url.url.split(",", 1)[1]) == PNG
    assert inputs.images[0].label == "LOCATION I:p1-b1"
    assert "LOCATION T:p1-b1 (chart)\n" + CAPTION in inputs.manuscript
    assert "PAGE 1 BLOCK" not in inputs.manuscript
    request = (
        ExtractionConfig()
        .blueprints()["roster"]
        .request(ModelConfig(model="deepseek-v4.1-flash"), inputs)
    )
    content = TokenDanceAdapter().prepare(request)["payload"]["messages"][1]["content"]
    assert [part["type"] for part in content] == ["text", "text", "image_url"]
    assert "SUPPLIED CONTEXT" not in content[0]["text"]
    assert content[1] == {"type": "text", "text": "LOCATION I:p1-b1"}
    assert content[2]["image_url"]["url"] == inputs.images[0].image_url.url
    assert inputs.citation_locations["I:p1-b1"] == {
        "file_id": "raw",
        "locator": {"kind": "pdf_image", "page": 1, "block_id": "p1-b1"},
    }
    assert pdf_evidence_block(source, space.root, "raw", 1, "p1-b1", require_image=True)
    with pytest.raises(ValueError, match="no supplied image"):
        pdf_evidence_block(source, space.root, "raw", 1, "p1-b0", require_image=True)
    image = next(f for f in source.data.files if f.role == "pdf_image")
    (space.root / image.path).write_bytes(PNG + b"changed")
    with pytest.raises(ValueError, match="hash changed"):
        pdf_evidence_block(source, space.root, "raw", 1, "p1-b1", require_image=True)


def test_one_validation_reuses_pdf_but_detects_changed_files_and_blocks(
    image_space, monkeypatch
):
    from hvs_catalog.records import pdf

    space, _, _, source = image_space
    original_reader = pdf.PdfReader
    parsed = []

    def reader(*args, **kwargs):
        parsed.append(True)
        return original_reader(*args, **kwargs)

    monkeypatch.setattr(pdf, "PdfReader", reader)
    with verification_scope():
        block = pdf_evidence_block(source, space.root, "raw", 1, "p1-b0")
        block["text"] = "Caller mutation must not modify fixed evidence"
        assert (
            pdf_evidence_block(source, space.root, "raw", 1, "p1-b0")["text"]
            == "A is unbound."
        )
        pdf_evidence_block(source, space.root, "raw", 1, "p1-b1", require_image=True)
        assert len(parsed) == 1
        image = next(f for f in source.data.files if f.role == "pdf_image")
        path = space.root / image.path
        unchanged = path.read_bytes()
        path.write_bytes(unchanged + b"changed")
        with pytest.raises(ValueError, match="hash changed"):
            pdf_evidence_block(
                source, space.root, "raw", 1, "p1-b1", require_image=True
            )
        path.write_bytes(unchanged)
    with verification_scope():
        pdf_evidence_block(source, space.root, "raw", 1, "p1-b0")
        assert len(parsed) == 2


def test_visual_quote_is_preserved_without_pretending_string_verification(image_space):
    space, _, _, source = image_space
    quote = {
        "file_id": "raw",
        "locator": {"kind": "pdf_image", "page": 1, "block_id": "p1-b1"},
        "excerpt": "A",
    }
    assert _hydrate_sources(quote, space, source, {"raw"}) == quote
    text_quote = copy.deepcopy(quote)
    text_quote["locator"]["kind"] = "pdf"
    with pytest.raises(EvidenceLocationError, match="not verbatim"):
        _hydrate_sources(text_quote, space, source, {"raw"})
    bad = copy.deepcopy(quote)
    bad["locator"]["block_id"] = "p1-b0"
    with pytest.raises(EvidenceLocationError, match="no supplied image"):
        _hydrate_sources(bad, space, source, {"raw"})


@pytest.mark.parametrize("corrections", [False, True])
def test_mock_roster_images_and_visual_evidence_survive_backup(
    image_space, tmp_path, corrections
):
    space, prep, _, _ = image_space
    item = entry("A", 3, "unbound")
    ev = item["identifiers"][0]["evidence"][0]
    ev.clear()
    ev.update({"location": "I:p1-b1", "quote": "A"})
    config = ExtractionConfig(
        runtime=RuntimeConfig(quota_store=tmp_path / "quota.sqlite"),
        max_format_corrections=2,
    )
    replies = (["{first-invalid", "{second-invalid"] if corrections else []) + [
        submission([item])
    ]
    _, report, adapter, _ = run((space, prep.execution_id, config), replies)
    assert report.outcome == "finished", report.errors
    content = adapter.seen[0]["messages"][1]["content"]
    assert isinstance(content, list)
    assert content[-1]["type"] == "image_url"
    original_images = [p for p in content if p["type"] == "image_url"]
    assert len(original_images) == 1
    assert (
        base64.b64decode(original_images[0]["image_url"]["url"].split(",", 1)[1]) == PNG
    )
    assert HALLUCINATION not in json.dumps(content)
    if corrections:
        assert len(adapter.seen) == 3
        initial, first_repair, second_repair = [r["messages"] for r in adapter.seen]
        assert first_repair[:-1] == initial
        assert second_repair[:-1] == first_repair
        assert "{first-invalid" in second_repair[2]["content"]
        assert "{second-invalid" in second_repair[3]["content"]
        assert "Validation details:" in second_repair[2]["content"]
        assert "Validation details:" in second_repair[3]["content"]
        assert sum(isinstance(m["content"], list) for m in second_repair) == 1
        assert sum(p["type"] == "image_url" for p in second_repair[1]["content"]) == 1
    records = space.store().list_records()
    evidence = [r for r in records if isinstance(r, EvidenceRecord)]
    assert evidence and all(
        r.data.locator.kind == "pdf_image" and r.data.excerpt == "A" for r in evidence
    )
    assert not validate_collection(records, materials_root=space.root)
    backup_workspace(space, tmp_path / "backup")
    restore_workspace(tmp_path / "backup", tmp_path / "restored")
    restored = open_workspace(tmp_path / "restored")
    try:
        assert not validate_collection(
            restored.store().list_records(), materials_root=restored.root
        )
    finally:
        restored.close()


def test_image_protocol_rejects_local_links_and_unconfigured_models():
    url = "data:image/png;base64," + base64.b64encode(PNG).decode()
    message = Message(
        role="user",
        content=[
            {"type": "text", "text": "Read the figure."},
            {"type": "image_url", "image_url": {"url": url}},
        ],
    )
    request = CallRequest(
        model=ModelConfig(model="deepseek-v4.1-flash"), messages=[message]
    )
    wire = TokenDanceAdapter().prepare(request)["payload"]
    assert wire["messages"][0]["content"][1]["image_url"]["url"] == url
    with pytest.raises(ValidationError, match="base64 data URL"):
        Message(
            role="user",
            content=[{"type": "image_url", "image_url": {"url": "images/figure.png"}}],
        )
    with pytest.raises(ValidationError, match="user message"):
        Message(role="system", content=message.content)
    request.model = ModelConfig(model="glm-5.3-flash")
    with pytest.raises(ValueError, match="will not be discarded"):
        TokenDanceAdapter().prepare(request)


@pytest.mark.integration
def test_image_bytes_reach_http_endpoint(image_space, tmp_path):
    space, _, bundle, source = image_space
    image = material_prompt(space, bundle, source).images[0]
    request = CallRequest(
        model=ModelConfig(model="deepseek-v4.1-flash"),
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
