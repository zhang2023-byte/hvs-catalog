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
from ..runtime.execution import append_run_revision
from .mineru import MinerUError, read_result
from .mineru_official import OfficialMinerU

FLOW_ID = "hvs-pdf-prepare"
FLOW_VERSION = "2"
STEP = "parse_pdf"


def parser_fingerprint() -> str:
    return hashlib.sha256(
        b"".join(
            Path(__file__).with_name(name).read_bytes()
            for name in ("flow.py", "mineru.py", "mineru_official.py", "remap.py")
        )
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
        self.runner = runner

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
        rebuild = request.get("rebuild")
        if self.runner is None and not rebuild:
            self.runner = OfficialMinerU(ctx.space)
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
        if not rebuild and self.runner.fingerprint() != request["parser"]:
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
        previous_files = {}
        try:
            if rebuild:
                prior = ctx.store.read_record(
                    RecordRef.model_validate(rebuild["prepared_source_ref"])
                )
                previous_files = {file.file_id: file for file in prior.data.files}
                original_pdf = previous_files.get(pdf.file_id)
                if (
                    original_pdf is None
                    or original_pdf.sha256 != pdf.sha256
                    or original_pdf.role != "pdf"
                ):
                    raise ValueError(
                        "Offline rebuild PDF does not match its previous preparation"
                    )
                archive = previous_files["mineru.zip"]
                ref = ArtifactRef.model_validate(rebuild["archive"])
                if archive.path != ref.path or archive.sha256 != ref.sha256:
                    raise ValueError(
                        "Offline rebuild archive does not match its previous preparation"
                    )
                raw = ctx.space.read_file(ref)
                parsed = read_result(raw, pages, mapping=rebuild["mapping"])
            else:
                raw, parsed = self.runner.parse(ctx.space.root / pdf.path, pages)
        except MinerUError as exc:
            artifacts = []
            for name, content in exc.artifacts.items():
                digest = hashlib.sha256(content).hexdigest()
                artifacts.append(
                    ctx.space.publish_run_file(
                        ctx.execution.execution_id,
                        f"outputs/parser-diagnostics/{digest}-{name}",
                        content,
                    )
                )
            if artifacts:
                append_run_revision(ctx.space, ctx.execution, artifacts=artifacts)
            raise
        files = [pdf.model_copy(update={"read_state": "ready"})]

        def publish(name, content, media, role, derived):
            old = previous_files.get(name)
            if old is None and role == "pdf_image":
                old = previous_files.get("raw/" + name)
            if old is not None and old.sha256 == hashlib.sha256(content).hexdigest():
                ref = ArtifactRef(path=old.path, sha256=old.sha256)
                ctx.space.read_file(ref)
            elif ctx.space.is_literature and role != "prepared_bundle":
                filename = "result.zip" if name == "mineru.zip" else name
                prefix = "mineru/"
                if rebuild:
                    prefix += "versions/" + ctx.execution.execution_id + "/"
                ref = ctx.space.publish_file(
                    ctx.space.paper_path(basis.data.paper_key, prefix + filename),
                    content,
                )
            else:
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
        for name, (content, media) in parsed.get("additional_outputs", {}).items():
            publish(name, content, media, "parser_output", [pdf.file_id])
        for name, (content, media) in parsed.get("provenance_outputs", {}).items():
            publish(
                name,
                content,
                media,
                "preparation_provenance",
                [pdf.file_id, "mineru.zip"],
            )
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
            if ctx.space.is_literature:
                for member in archive.infolist():
                    if (
                        member.is_dir()
                        or member.filename in parsed.get("images", {})
                        or member.filename.endswith((".md", "_origin.pdf"))
                    ):
                        continue
                    publish(
                        "raw/" + member.filename,
                        archive.read(member),
                        "application/octet-stream",
                        "parser_output",
                        [pdf.file_id, "mineru.zip"],
                    )
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
        if rebuild:
            index["rebuild"] = rebuild
        publish(
            "pdf-blocks.json",
            canonical_json(index).encode(),
            "application/json",
            "pdf_index",
            [pdf.file_id, "mineru.zip"],
        )
        preparation_issues = [
            *basis.data.preparation_issues,
            *parsed["preparation_issues"],
        ]
        prepared_ref = RecordRef(
            record_id="pdf-prepared-" + ctx.execution.execution_id, revision=1
        )
        summary = {
            "pages": pages,
            "blocks": len(parsed["blocks"]),
            "materials_complete": not preparation_issues,
            "parser": request["parser"],
            "pdf_sha256": pdf.sha256,
        }
        bundle = {
            "execution_id": ctx.execution.execution_id,
            "prepared_source_ref": prepared_ref.model_dump(mode="json"),
            "body": {"status": "organized"},
            "pdf_file_id": pdf.file_id,
            "summary": summary,
            "preparation_issues": preparation_issues,
        }
        if rebuild:
            bundle["rebuild"] = rebuild
        bundle_ref = publish(
            "prepared-input.json",
            canonical_json(bundle).encode(),
            "application/json",
            "prepared_bundle",
            [pdf.file_id],
        )
        data = basis.data.model_dump(mode="json")
        data.update(
            files=[f.model_dump(mode="json") for f in files],
            preparation_issues=preparation_issues,
        )
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
