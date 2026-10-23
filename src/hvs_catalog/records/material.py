"""Build citation bindings from fixed parser blocks and supplied image files."""

import base64
import hashlib
import json

from .common import ArtifactRef
from .file_checks import file_digest


def supplied_locations(locations, content):
    """Bind image evidence only to original bytes present in the material message."""
    supplied = {k: v for k, v in locations.items() if not k.startswith("I:")}
    if not isinstance(content, list):
        return supplied
    remaining = {
        k: list(v["images"]) for k, v in locations.items() if k.startswith("I:")
    }
    label = None
    for part in content:
        if part.get("type") == "text":
            text = part.get("text", "")
            label = (
                text.removeprefix("LOCATION ")
                if text.startswith("LOCATION I:")
                else None
            )
        elif part.get("type") == "image_url":
            if label not in remaining:
                raise ValueError("A supplied image has no fixed source location.")
            url = part["image_url"]["url"]
            try:
                encoded = url.split(";base64,", 1)[1]
                digest = hashlib.sha256(
                    base64.b64decode(encoded, validate=True)
                ).hexdigest()
            except (IndexError, ValueError) as exc:
                raise ValueError("A supplied image has invalid inline bytes.") from exc
            ref = next((r for r in remaining[label] if r["sha256"] == digest), None)
            if ref is None:
                raise ValueError("A supplied image differs from the fixed source.")
            remaining[label].remove(ref)
            if label not in supplied:
                supplied[label] = {**locations[label], "images": []}
            supplied[label]["images"].append(ref)
    return supplied


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
