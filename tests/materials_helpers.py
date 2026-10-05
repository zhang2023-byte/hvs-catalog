"""Fictional PDF fixtures and a deterministic parser at the external-tool boundary."""

from __future__ import annotations

import io
import json
import zipfile

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from hvs_catalog.pipeline.acquisition import AcquisitionService
from hvs_catalog.workspace import initialize_workspace

PAPER = "2501.01111"


def make_pdf(text="Synthetic S5-HVS1 study. RV = -430 +/- 12 km/s.", pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): writer._add_object(font)}
                )
            }
        )
        stream = DecodedStreamObject()
        escaped = lambda line: (
            line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        )
        content = (
            "BT /F1 9 Tf 25 760 Td 12 TL "
            + " ".join("(" + escaped(line) + ") Tj T*" for line in text.splitlines())
            + " ET"
        )
        stream.set_data(content.encode("ascii", errors="replace"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_metadata({"/FixtureText": text})
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def mineru_zip(text, pages=1, *, full=True):
    blocks = [
        {
            "index": i,
            "type": "paragraph",
            "bbox": [0.03, 0.02 + i * 0.01, 0.98, 0.03 + i * 0.01],
            "content": line,
        }
        for i, line in enumerate(text.splitlines())
        if line
    ]
    document = {
        "schema": "docvortex.middle",
        "is_full_document": full,
        "pages": [{"page_idx": i, "blocks": blocks} for i in range(pages)],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("middle.json", json.dumps(document))
        archive.writestr("manuscript.md", text)
    return buffer.getvalue()


class FakeMinerU:
    def __init__(self, *, gaps=None):
        self.calls = 0
        self.gaps = gaps or []

    def fingerprint(self):
        return {
            "name": "synthetic-parser",
            "version": "4.0-test",
            "tier": "standard",
            "pages": "all",
            "adapter": "1",
        }

    def parse(self, path, page_count):
        from hvs_catalog.materials.mineru import read_result

        self.calls += 1
        text = PdfReader(path).metadata["/FixtureText"]
        raw = mineru_zip(text, page_count)
        parsed = read_result(raw, page_count)
        parsed["gaps"] += self.gaps
        return raw, parsed


def prepared_space(tmp_path, payload=None):
    space = initialize_workspace(tmp_path / "space")
    path = tmp_path / "paper.pdf"
    path.write_bytes(payload or make_pdf())
    imported = AcquisitionService(space).import_local_pdf(
        path, arxiv_id=PAPER + "v1", provenance="synthetic PDF fixture"
    )
    return space, imported
