"""Build citation bindings from fixed parser blocks and supplied image files."""

import json

from .common import ArtifactRef
from .file_checks import file_digest


def material_locations(source, root):
    files = {f.file_id: f for f in source.data.files}
    index_file = next(f for f in source.data.files if f.role == "pdf_index")
    index = json.loads((root / index_file.path).read_bytes())
    pdf_id = index["pdf_file_id"]
    locations = {}
    for block in index["blocks"]:
        locator = {"page": block["page"], "block_id": block["block_id"]}
        if block["text"]:
            locations["T:" + block["block_id"]] = {
                "file_id": pdf_id,
                "text": block["text"],
                "locator": {"kind": "pdf", **locator},
            }
        for image_id in block.get("image_files", []):
            file = files[image_id]
            if (
                file.role != "pdf_image"
                or pdf_id not in file.derived_from
                or file_digest(root / file.path) != file.sha256
            ):
                raise ValueError("Image differs from the fixed PDF binding")
            item = locations.setdefault(
                "I:" + block["block_id"],
                {
                    "file_id": pdf_id,
                    "locator": {"kind": "pdf_image", **locator},
                    "images": [],
                },
            )
            item["images"].append(
                ArtifactRef(path=file.path, sha256=file.sha256).model_dump()
            )
    return locations
