"""Offline native-content recovery and immutable material revisions."""

import base64
import io
import json
import os
import subprocess
import sys
import zipfile
from contextlib import closing
from copy import deepcopy

import pytest
from materials_helpers import FakeMinerU, make_pdf

from hvs_catalog.materials import MaterialsService, OfficialMinerU
from hvs_catalog.materials.mineru import read_result
from hvs_catalog.pipeline.acquisition import AcquisitionService
from hvs_catalog.pipeline.roster import material_prompt
from hvs_catalog.records.common import RecordRef
from hvs_catalog.workspace import (
    backup_workspace,
    initialize_workspace,
    open_workspace,
    restore_workspace,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a/e0AAAAASUVORK5CYII="
)
MAPPING = {
    "version": 1,
    "replacements": [{"page": 1, "block": 0}, {"page": 2, "block": 0}],
    "recover_discarded_pages": [3],
}
TABLES = [
    "<table><tr><td>Epoch</td><td>RV</td></tr><tr><td>71.25</td><td>−35.31 ± 10.99</td></tr></table>",
    "<table><tr><td>Epoch</td><td>RV</td></tr><tr><td>84.50</td><td>6.20 ± 0.50</td></tr></table>",
]


def native_text(text, kind="text", index=1):
    return {
        "type": kind,
        "index": index,
        "bbox": [10, 65, 90, 80],
        "lines": [{"spans": [{"content": text}]}],
    }


def layout():
    pages = []
    for i in range(2):
        table = {
            "type": "table",
            "index": 0,
            "bbox": [10, 10, 90, 60],
            "blocks": [
                {
                    "type": "table_body",
                    "index": 0,
                    "bbox": [10, 10, 90, 60],
                    "lines": [
                        {"spans": [{"html": TABLES[i], "image_path": f"p{i + 1}.png"}]}
                    ],
                },
                native_text(
                    f"Table {i + 1}. Instrument {'A' if i == 0 else 'B'}.",
                    "table_caption",
                ),
            ],
        }
        pages.append(
            {
                "page_idx": i,
                "page_size": [100, 100],
                "preproc_blocks": [deepcopy(table)],
                "para_blocks": [table],
            }
        )
    pages[0]["para_blocks"][0]["blocks"][0]["lines"][0]["spans"][0]["html"] = (
        "<table><tr><td>71.25Epoch</td><td>−35.31 ± 10.99RV</td></tr>"
        "<tr><td>84.50</td><td>6.20 ± 0.50</td></tr></table>"
    )
    pages[0]["para_blocks"][0]["blocks"].append(
        native_text("Table 2. Instrument B.", "table_footnote")
    )
    for block in pages[1]["para_blocks"][0]["blocks"]:
        block["lines"] = []
        block["lines_deleted"] = True
    for group in ("preproc_blocks", "para_blocks"):
        pages[0][group].append(native_text("Unchanged paragraph.", index=2))
    pages.append(
        {
            "page_idx": 2,
            "page_size": [100, 100],
            "preproc_blocks": [],
            "para_blocks": [],
            "discarded_blocks": [native_text("9. A retained reference.", "header")],
        }
    )
    return {"pdf_info": pages}


def archive(document=None):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("layout.json", json.dumps(document or layout()))
        z.writestr("full.md", "Original official Markdown, unchanged.")
        z.writestr("images/p1.png", PNG)
        z.writestr("images/p2.png", PNG)
    return out.getvalue()


class NativeParser(FakeMinerU):
    def parse(self, path, pages):
        self.calls += 1
        raw = archive()
        return raw, read_result(raw, pages)


def prepare(tmp_path, literature=True):
    space = initialize_workspace(tmp_path / "space", literature=literature)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(make_pdf(pages=3))
    source = AcquisitionService(space).import_local_pdf(
        pdf, arxiv_id="2501.01111v1", provenance="Synthetic"
    )
    runner = NativeParser()
    old, report = MaterialsService(space, runner=runner).prepare_materials(
        request_key="old", source_ref=source.material_ref
    )
    assert report.outcome == "finished", report.errors
    return space, old, runner


def record(space, execution_id):
    bundle = MaterialsService(space).get_prepared_input(execution_id)
    return bundle, space.store().read_record(
        RecordRef.model_validate(bundle["prepared_source_ref"])
    )


def test_verbatim_page_tables_images_references_and_unchanged_blocks():
    raw = archive()
    old = read_result(raw, 3)
    result = read_result(raw, 3, mapping=MAPPING)
    assert old["preparation_issues"] and not result["preparation_issues"]
    blocks = {b["block_id"]: b for b in result["blocks"]}
    for page in (1, 2):
        b = blocks[f"p{page}-b0"]
        assert TABLES[page - 1] in b["text"]
        assert b["image_files"] == [f"images/p{page}.png"]
        assert b["source_mapping"]["page"] == page
        assert b["page"] == page
    assert "Table 2" not in blocks["p1-b0"]["text"]
    assert "71.25Epoch" not in str(result["blocks"])
    assert blocks["p1-b1"] == old["blocks"][1]
    assert blocks["p3-b0"]["type"] == "header"
    assert blocks["p3-b0"]["text"] == "9. A retained reference."
    assert blocks["p3-b0"]["source_mapping"]["collection"] == "discarded_blocks"
    assert result["markdown"] == old["markdown"]
    assert json.loads(zipfile.ZipFile(io.BytesIO(raw)).read("layout.json")) == layout()


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "ambiguous",
        "not_local",
        "empty",
        "discarded_body",
        "duplicate",
        "invalid",
        "schema",
    ],
)
def test_mapping_refuses_missing_ambiguous_or_unsafe_native_sources(failure):
    document, mapping = layout(), deepcopy(MAPPING)
    page = document["pdf_info"][0]
    if failure == "missing":
        page["preproc_blocks"] = []
    if failure == "ambiguous":
        page["preproc_blocks"].append(deepcopy(page["preproc_blocks"][0]))
    if failure == "not_local":
        page["preproc_blocks"][0]["cross_page"] = True
    if failure == "empty":
        page["preproc_blocks"][0]["blocks"] = []
    if failure == "discarded_body":
        mapping["recover_discarded_pages"] = [1]
    if failure == "duplicate":
        mapping["replacements"].append({"page": 1, "block": 0})
    if failure == "invalid":
        mapping["replacements"][0]["block"] = -1
    if failure == "schema":
        mapping["version"] = 9
    with pytest.raises(ValueError):
        read_result(archive(document), 3, mapping=mapping)


@pytest.mark.parametrize("literature", [True, False])
def test_offline_revision_preserves_old_files_binds_model_input_and_restores(
    tmp_path, monkeypatch, literature
):
    space, old, runner = prepare(tmp_path, literature)
    old_bundle, original = record(space, old.execution_id)
    original_bytes = {
        f.path: (space.root / f.path).read_bytes() for f in original.data.files
    }

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline rebuild contacted a parser")

    monkeypatch.setattr(OfficialMinerU, "parse", forbidden)
    service = MaterialsService(space)
    handle, report = service.rebuild_materials(
        old.execution_id, request_key="new", mapping=MAPPING
    )
    assert report.outcome == "finished", report.errors
    assert handle.execution_id != old.execution_id and runner.calls == 1
    bundle, revised = record(space, handle.execution_id)
    prompt = material_prompt(space, bundle, revised)
    assert len(prompt.images) == 2
    assert prompt.citation_locations["T:p2-b0"]["locator"]["page"] == 2
    assert "9. A retained reference." in prompt.manuscript
    assert "71.25Epoch" not in prompt.manuscript
    assert prompt.citation_locations["I:p2-b0"]["locator"]["page"] == 2
    before = {f.file_id: f for f in original.data.files}
    after = {f.file_id: f for f in revised.data.files}
    assert after["mineru.zip"].path == before["mineru.zip"].path
    assert after["manuscript.md"].path == before["manuscript.md"].path
    assert after["pdf-blocks.json"].path != before["pdf-blocks.json"].path
    assert after["material-mapping.json"].role == "preparation_provenance"
    if literature:
        assert "/versions/" in after["pdf-blocks.json"].path
        assert after["images/p2.png"].path == before["raw/images/p2.png"].path
    assert all(
        (space.root / path).read_bytes() == content
        for path, content in original_bytes.items()
    )
    assert service.get_prepared_input(old.execution_id) == old_bundle
    assert service.rebuild_materials(
        old.execution_id, request_key="new", mapping=MAPPING
    )[0].reused
    changed = {**MAPPING, "recover_discarded_pages": []}
    with pytest.raises(Exception, match="不同输入"):
        service.rebuild_materials(old.execution_id, request_key="new", mapping=changed)
    backup_workspace(space, tmp_path / "backup")
    space.close()
    restore_workspace(tmp_path / "backup", tmp_path / "restored")
    with closing(open_workspace(tmp_path / "restored")) as restored:
        assert MaterialsService(restored).consume_prepared_input(handle.execution_id)[
            "ok"
        ]
        assert MaterialsService(restored).consume_prepared_input(old.execution_id)["ok"]


@pytest.mark.integration
def test_offline_revision_recovers_after_process_exit_with_no_cloud_call(tmp_path):
    space, old, _ = prepare(tmp_path)
    space.close()
    driver = """
import json, os
from hvs_catalog.workspace import Workspace, open_workspace
from hvs_catalog.materials import MaterialsService, OfficialMinerU
from hvs_catalog.runtime import get_execution
def forbidden(*a, **k): raise AssertionError('Cloud access forbidden')
OfficialMinerU.parse = forbidden
original = Workspace.publish_file
def publish(self, name, payload):
    ref = original(self, name, payload)
    if '/versions/' in name and name.endswith('pdf-blocks.json') and os.environ.get('BREAK_REMAP') == '1':
        os._exit(92)
    return ref
Workspace.publish_file = publish
space = open_workspace(os.environ['REMAP_SPACE'])
row = space.store().find_execution_by_request('new')
service = MaterialsService(space)
if row:
    report = service.resume_preparation(row.execution_id)
else:
    _, report = service.rebuild_materials(os.environ['REMAP_PREVIOUS'], request_key='new', mapping=json.loads(os.environ['REMAP_SELECTION']))
assert report.outcome == 'finished', report.errors
space.close()
"""
    env = {
        **os.environ,
        "REMAP_SPACE": str(tmp_path / "space"),
        "REMAP_PREVIOUS": old.execution_id,
        "REMAP_SELECTION": json.dumps(MAPPING),
        "BREAK_REMAP": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", driver],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 92, result.stderr
    env["BREAK_REMAP"] = "0"
    result = subprocess.run(
        [sys.executable, "-c", driver],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    with closing(open_workspace(tmp_path / "space")) as restored:
        row = restored.store().find_execution_by_request("new")
        assert MaterialsService(restored).consume_prepared_input(row.execution_id)["ok"]
        assert MaterialsService(restored).consume_prepared_input(old.execution_id)["ok"]


def test_batch_selects_new_revision_and_preserves_predecessor_history(tmp_path):
    from dataclasses import replace

    from test_preparation_batch import SyntheticApi, create

    from hvs_catalog.pipeline.preparation_batch import PreparationBatchService

    class ThreePageApi(SyntheticApi):
        def download_pdf(self, identifier):
            result = super().download_pdf(identifier)
            content = make_pdf(pages=3)
            return replace(result, payload=content, byte_count=len(content))

    with closing(initialize_workspace(tmp_path / "batch", literature=True)) as space:
        batch = PreparationBatchService(
            space, api=ThreePageApi(), runner=NativeParser()
        )
        create(batch)
        batch.discover("sample")
        batch.download("sample")
        batch.parse("sample", identifiers=["1804.00001", "1804.00002"])
        papers = {p["arxiv_id"]: p for p in batch.get_manifest("sample")["papers"]}
        old = papers["1804.00001"]["preparation"]["execution_id"]
        service = MaterialsService(space)
        new, report = service.rebuild_materials(
            old, request_key="remapped", mapping=MAPPING
        )
        assert report.outcome == "finished", report.errors
        with pytest.raises(ValueError, match="同一固定"):
            batch.select_preparation("sample", "1804.00002", new.execution_id)
        batch.select_preparation("sample", "1804.00001", new.execution_id)
        batch.select_preparation("sample", "1804.00001", new.execution_id)
        papers = {p["arxiv_id"]: p for p in batch.get_manifest("sample")["papers"]}
        selected = papers["1804.00001"]["preparation"]
        assert selected["execution_id"] == new.execution_id
        assert selected["previous_execution_ids"] == [old]
        stale, _ = service.rebuild_materials(old, request_key="stale", mapping=MAPPING)
        with pytest.raises(ValueError, match="当前准备"):
            batch.select_preparation("sample", "1804.00001", stale.execution_id)


@pytest.mark.parametrize("remaining", [0, 1])
def test_table_group_rejects_recovering_only_one_side_of_a_transfer(remaining):
    mapping = {**MAPPING, "replacements": [MAPPING["replacements"][remaining]]}
    with pytest.raises(ValueError, match="omits linked table"):
        read_result(archive(), 3, mapping=mapping)


def test_following_revision_keeps_predecessor_recoveries(tmp_path):
    space, old, _ = prepare(tmp_path)
    try:
        service = MaterialsService(space)
        first, _ = service.rebuild_materials(
            old.execution_id,
            request_key="tables",
            mapping={**MAPPING, "recover_discarded_pages": []},
        )
        second, report = service.rebuild_materials(
            first.execution_id,
            request_key="references",
            mapping={"version": 1, "replacements": [], "recover_discarded_pages": [3]},
        )
        assert report.outcome == "finished", report.errors
        bundle, prepared = record(space, second.execution_id)
        prompt = material_prompt(space, bundle, prepared)
        assert len(prompt.images) == 2
        assert "71.25Epoch" not in prompt.manuscript
        assert "9. A retained reference." in prompt.manuscript
        assert bundle["rebuild"]["mapping"]["replacements"] == MAPPING["replacements"]
    finally:
        space.close()


def caption_layout():
    document = layout()
    page = document["pdf_info"][0]
    for key in ("preproc_blocks", "para_blocks"):
        caption = native_text("Table 7. Separate sample.", "table_caption", index=8)
        page[key][0]["blocks"].append(caption)
        separate = deepcopy(document["pdf_info"][1]["preproc_blocks"][0])
        separate["index"] = 3
        separate["bbox"] = [10, 82, 90, 95]
        separate["blocks"] = [separate["blocks"][0]]
        page[key].append(separate)
    return document


def caption_mapping():
    return {
        **deepcopy(MAPPING),
        "version": 2,
        "replacements": [*deepcopy(MAPPING["replacements"]), {"page": 1, "block": 2}],
        "caption_moves": [
            {"page": 1, "from_block": 0, "child": 2, "to_block": 2, "insert_at": 0}
        ],
    }


def test_caption_ownership_preserves_words_tables_geometry_and_original():
    from hvs_catalog.materials.remap import remap_native_layout

    document = caption_layout()
    original = deepcopy(document)
    mapped, audit = remap_native_layout(document, caption_mapping())
    assert document == original
    before = original["pdf_info"][0]["preproc_blocks"][0]["blocks"][2]
    first, _, second = mapped["pdf_info"][0]["para_blocks"]
    assert all(child != before for child in first["blocks"])
    assert second["blocks"][0] == before
    assert (
        second["blocks"][1] == original["pdf_info"][0]["preproc_blocks"][2]["blocks"][0]
    )
    assert (
        audit["caption_moves"][0]["source_json_path"]
        == "pdf_info[0].preproc_blocks[0].blocks[2]"
    )
    result = read_result(archive(document), 3, mapping=caption_mapping())
    blocks = {b["block_id"]: b for b in result["blocks"]}
    assert "Table 7." not in blocks["p1-b0"]["text"]
    assert blocks["p1-b2"]["text"].startswith("Table 7. Separate sample.")
    assert blocks["p1-b2"]["regions"][0]["type"] == "table_caption"
    assert TABLES[1] in blocks["p1-b2"]["text"]


@pytest.mark.parametrize(
    "changes,pattern",
    [
        ({"child": 0}, "caption"),
        ({"child": 100}, "missing"),
        ({"from_block": 2, "to_block": 0, "child": 1}, "missing"),
        ({"to_block": 0}, "two restored"),
        ({"page": 2}, "two restored"),
        ({"insert_at": 100}, "out of range"),
        ({"insert_at": True}, "integers"),
    ],
)
def test_caption_moves_reject_invalid_sources_destinations(changes, pattern):
    mapping = caption_mapping()
    mapping["caption_moves"][0].update(changes)
    with pytest.raises(ValueError, match=pattern):
        read_result(archive(caption_layout()), 3, mapping=mapping)


def test_caption_moves_reject_duplicate_sources():
    mapping = caption_mapping()
    mapping["caption_moves"] *= 2
    with pytest.raises(ValueError, match="Duplicate caption"):
        read_result(archive(caption_layout()), 3, mapping=mapping)


def test_caption_revision_survives_following_recovery_and_replay(tmp_path, monkeypatch):
    raw = archive(caption_layout())
    monkeypatch.setattr(sys.modules[__name__], "archive", lambda: raw)
    space, old, runner = prepare(tmp_path)
    try:
        service = MaterialsService(space)
        original_bundle, original_record = record(space, old.execution_id)
        mapping = {**caption_mapping(), "recover_discarded_pages": []}
        first, report = service.rebuild_materials(
            old.execution_id, request_key="caption", mapping=mapping
        )
        assert report.outcome == "finished", report.errors
        again, _ = service.rebuild_materials(
            old.execution_id, request_key="caption", mapping=mapping
        )
        assert again.execution_id == first.execution_id
        second, report = service.rebuild_materials(
            first.execution_id,
            request_key="later",
            mapping={"version": 1, "replacements": [], "recover_discarded_pages": [3]},
        )
        assert report.outcome == "finished", report.errors
        bundle, prepared = record(space, second.execution_id)
        saved = bundle["rebuild"]["mapping"]
        assert saved["version"] == 2
        assert saved["caption_moves"] == mapping["caption_moves"]
        prompt = material_prompt(space, bundle, prepared)
        assert prompt.manuscript.count("Table 7. Separate sample.") == 1
        assert "9. A retained reference." in prompt.manuscript
        assert record(space, old.execution_id) == (original_bundle, original_record)
        assert runner.calls == 1
    finally:
        space.close()
