"""Bounded PDF validation; document interpretation belongs to the external parser."""

import hashlib
import json
from io import BytesIO

from pypdf import PdfReader

MAX_PDF_BYTES = 256 * 1024 * 1024
MAX_PDF_PAGES = 2000


def image_media_type(content: bytes) -> str:
    """Recognize supported image signatures without transforming source pixels."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("Unsupported image format; expected PNG, JPEG, GIF or WebP")


def pdf_evidence_block(
    source, root, file_id: str, page: int, block_id: str, *, require_image=False
) -> dict:
    """Validate the PDF and its fixed parser mapping, without claiming visual accuracy."""

    def read(file):
        path = (root / file.path).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError("PDF evidence file is missing or outside the workspace")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != file.sha256:
            raise ValueError("PDF evidence file hash changed")
        return content

    pdf = next(
        (f for f in source.data.files if f.file_id == file_id and f.role == "pdf"), None
    )
    indices = [
        f
        for f in source.data.files
        if f.role == "pdf_index" and file_id in f.derived_from
    ]
    if pdf is None or len(indices) != 1:
        raise ValueError("PDF evidence lacks an unambiguous parser mapping")
    mapping = json.loads(read(indices[0]))
    if mapping["pdf_sha256"] != pdf.sha256 or mapping["pdf_file_id"] != file_id:
        raise ValueError("PDF mapping refers to a different original")
    count = pdf_page_count(read(pdf))
    if mapping["page_count"] != count or not 1 <= page <= count:
        raise ValueError("PDF evidence page is out of bounds")
    blocks = [
        b for b in mapping["blocks"] if b["page"] == page and b["block_id"] == block_id
    ]
    if len(blocks) != 1:
        raise ValueError("PDF evidence block does not match its page")
    if require_image:
        image_ids = blocks[0].get("image_files", [])
        if not image_ids:
            raise ValueError("PDF image evidence block has no supplied image")
        for image_id in image_ids:
            image = next((f for f in source.data.files if f.file_id == image_id), None)
            if (
                image is None
                or image.role != "pdf_image"
                or file_id not in image.derived_from
            ):
                raise ValueError("PDF image evidence is not bound to this original")
            if image_media_type(read(image)) != image.media_type:
                raise ValueError("PDF image evidence MIME type is invalid")
    return blocks[0]


def pdf_page_count(payload: bytes) -> int:
    if not payload.startswith(b"%PDF-") or len(payload) > MAX_PDF_BYTES:
        raise ValueError("Expected a PDF within the supported size limit")
    try:
        reader = PdfReader(BytesIO(payload), strict=True)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs are not supported")
        count = len(reader.pages)
        if not 1 <= count <= MAX_PDF_PAGES:
            raise ValueError("PDF page count is outside the supported range")
        return count
    except Exception as exc:
        raise ValueError(f"Unreadable PDF: {exc}") from exc
