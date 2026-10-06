"""Fixed PDF preparation with one parser call and durable shared-runtime results."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from ..records.common import (
    ArtifactRef,
    RecordRef,
    build_record,
    canonical_json,
    sha256_of,
)
from ..records.pdf import pdf_page_count
from ..records.source import SourceFile, SourcePackageRecord
from ..runtime import DefaultFlowAdapter
from .mineru import LocalMinerU

FLOW_ID = "hvs-pdf-prepare"
FLOW_VERSION = "1"
STEP = "parse_pdf"


def parser_fingerprint() -> str:
    return hashlib.sha256(
        Path(__file__).read_bytes() + Path(__file__).with_name("mineru.py").read_bytes()
    ).hexdigest()


class MaterialsState(TypedDict, total=False):
    execution_id: str
    bundle: dict
    result_refs: list[dict]


class MaterialsFlow(DefaultFlowAdapter):
    flow_id = FLOW_ID
    flow_version = FLOW_VERSION
    mode = "daily"

    def __init__(self, *, runner=None):
        self.runner = runner or LocalMinerU()

    def initial_state(self, ctx):
        return {"execution_id": ctx.execution.execution_id}

    def build_graph(self, ctx):
        graph = StateGraph(MaterialsState)
        graph.add_node(STEP, lambda state: self._prepare(ctx, state))
        graph.add_edge(START, STEP)
        graph.add_edge(STEP, END)
        return graph.compile(checkpointer=ctx.checkpointer)

    def _prepare(self, ctx, state):
        request = json.loads(ctx.execution.input_snapshot.options["materials_request"])
        if request["parser_fingerprint"] != parser_fingerprint():
            raise ValueError("PDF adapter code changed; create a new preparation")
        digest = sha256_of(request)
        saved = ctx.reuse_step(STEP, digest)
        if saved:
            record = ctx.store.read_record(saved.result_refs[0])
            file = next(f for f in record.data.files if f.role == "prepared_bundle")
            ref = ArtifactRef(path=file.path, sha256=file.sha256)
            ctx.space.read_file(ref)
            return {
                "bundle": ref.model_dump(mode="json"),
                "result_refs": [r.model_dump(mode="json") for r in saved.result_refs],
            }
        if self.runner.fingerprint() != request["parser"]:
            raise ValueError(
                "MinerU version/configuration changed; create a new preparation"
            )
        basis = ctx.store.read_record(RecordRef.model_validate(request["source_ref"]))
        if not isinstance(basis, SourcePackageRecord):
            raise TypeError("Fixed PDF source is missing")
        pdfs = [
            f
            for f in basis.data.files
            if f.role == "pdf" and f.media_type == "application/pdf"
        ]
        if len(pdfs) != 1:
            raise ValueError(
                "Prepare exactly one fixed PDF; LaTeX packages are not extraction inputs"
            )
        pdf = pdfs[0]
        pdf_bytes = ctx.space.read_file(ArtifactRef(path=pdf.path, sha256=pdf.sha256))
        pages = pdf_page_count(pdf_bytes)
        raw, parsed = self.runner.parse(ctx.space.root / pdf.path, pages)
        files = [pdf.model_copy(update={"read_state": "ready"})]

        def publish(name, content, media, role, derived):
            ref = ctx.space.publish_run_file(
                ctx.execution.execution_id, "outputs/" + name, content
            )
            file = SourceFile(
                file_id=name,
                path=ref.path,
                sha256=ref.sha256,
                media_type=media,
                role=role,
                read_state="ready",
                derived_from=derived,
            )
            files.append(file)
            return ref

        publish("mineru.zip", raw, "application/zip", "parser_output", [pdf.file_id])
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for name, image in parsed.get("images", {}).items():
                ref = publish(
                    name,
                    archive.read(name),
                    image["media_type"],
                    "pdf_image",
                    [pdf.file_id, "mineru.zip"],
                )
                if ref.sha256 != image["sha256"]:
                    raise ValueError("Parser image fingerprint changed")
        blocks = list(parsed["blocks"])
        publish(
            "manuscript.md",
            parsed["markdown"].encode(),
            "text/markdown",
            "derived_text",
            [pdf.file_id, "mineru.zip"],
        )
        index = {
            "pdf_file_id": pdf.file_id,
            "pdf_sha256": pdf.sha256,
            "page_count": pages,
            "parser": request["parser"],
            "blocks": blocks,
            "image_policy": "original_images_without_generated_descriptions",
            "evidence_basis": "parser transcription; original PDF remains authoritative",
        }
        publish(
            "pdf-blocks.json",
            canonical_json(index).encode(),
            "application/json",
            "pdf_index",
            [pdf.file_id, "mineru.zip"],
        )
        gaps = [
            *basis.data.model_dump(mode="json")["gaps"],
            *[
                {
                    "gap_id": f"pdf-gap-{i}",
                    "reason": "unparsed",
                    "description": message,
                    "file_id": pdf.file_id,
                    "mention_evidence_refs": [],
                    "affected_targets": {"scope": "unknown", "items": []},
                }
                for i, message in enumerate(parsed["gaps"], 1)
            ],
        ]
        prepared_ref = RecordRef(
            record_id="pdf-prepared-" + ctx.execution.execution_id, revision=1
        )
        summary = {
            "pages": pages,
            "blocks": len(parsed["blocks"]),
            "materials_complete": not gaps,
            "parser": request["parser"],
            "pdf_sha256": pdf.sha256,
        }
        bundle = {
            "execution_id": ctx.execution.execution_id,
            "prepared_source_ref": prepared_ref.model_dump(mode="json"),
            "body": {"status": "organized"},
            "pdf_file_id": pdf.file_id,
            "summary": summary,
            "gaps": gaps,
        }
        bundle_ref = publish(
            "prepared-input.json",
            canonical_json(bundle).encode(),
            "application/json",
            "prepared_bundle",
            [pdf.file_id],
        )
        data = basis.data.model_dump(mode="json")
        data.update(files=[f.model_dump(mode="json") for f in files], gaps=gaps)
        record = build_record(
            SourcePackageRecord,
            record_id=prepared_ref.record_id,
            created_at=ctx.now(),
            execution_id=ctx.execution.execution_id,
            data=data,
        )
        ctx.commit_step(
            step_id=STEP,
            input_digest=digest,
            records=[record],
            result_refs=[record.ref()],
        )
        return {
            "bundle": bundle_ref.model_dump(mode="json"),
            "result_refs": [record.ref().model_dump(mode="json")],
        }

    def verify_state(self, ctx, state):
        return (
            []
            if state.get("execution_id") == ctx.execution.execution_id
            else ["PDF preparation execution mismatch"]
        )
