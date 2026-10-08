"""Share immutable input text while preserving exact reconstructed JSON values."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

from ..records.common import ArtifactRef, canonical_json
from .layout import resolve_within

FORMAT = "hvs.shared-json"
TEXT_KEY = "$hvs_text"
IMAGE_KEY = "$hvs_image"


def publish_json(space, relative: str, value) -> ArtifactRef:
    """Externalize repeated long strings; publication never changes existing inputs."""
    image_files = None

    def image_ref(data):
        nonlocal image_files
        digest = hashlib.sha256(data).hexdigest()
        if image_files is None:

            def collect(tx):
                found = {}
                for row in tx.conn.execute(
                    "SELECT payload FROM records WHERE schema_name='source_package'"
                ):
                    for file in json.loads(row[0])["data"]["files"]:
                        if file.get("role") == "pdf_image":
                            found[file["sha256"]] = file["path"]
                return found

            image_files = space.store().transaction_read(collect)
        if digest in image_files:
            ref = ArtifactRef(path=image_files[digest], sha256=digest)
            if space.read_file(ref) != data:
                raise ValueError("image source differs from request")
            return ref
        return space.publish_file(f"sources/shared-images/{digest}.bin", data)

    def pack(item):
        if isinstance(item, str):
            match = re.fullmatch(r"(data:image/[^;,]+;base64,)([A-Za-z0-9+/=]+)", item)
            if match:
                data = base64.b64decode(match[2], validate=True)
                if base64.b64encode(data).decode() == match[2]:
                    return {
                        IMAGE_KEY: {
                            "ref": image_ref(data).model_dump(),
                            "prefix": match[1],
                        }
                    }
        if isinstance(item, str) and len(item.encode()) >= 1024:
            data = item.encode()
            digest = hashlib.sha256(data).hexdigest()
            ref = space.publish_file(f"runs/inputs/shared/{digest}.txt", data)
            return {TEXT_KEY: ref.model_dump(mode="json")}
        if isinstance(item, dict):
            if TEXT_KEY in item or IMAGE_KEY in item:
                raise ValueError("reserved shared-input marker in JSON")
            return {key: pack(child) for key, child in item.items()}
        if isinstance(item, list):
            return [pack(child) for child in item]
        return item

    payload = {"format": FORMAT, "version": 1, "value": pack(value)}
    return space.publish_file(relative, canonical_json(payload).encode())


def read_json(space, ref: ArtifactRef):
    """Read legacy inline JSON or reconstruct shared input with verified text hashes."""
    payload = json.loads(space.read_file(ref))
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        return payload
    if payload.get("version") != 1 or set(payload) != {"format", "version", "value"}:
        raise ValueError("unsupported shared JSON archive")

    def unpack(item):
        if isinstance(item, dict):
            if set(item) == {TEXT_KEY}:
                return space.read_file(
                    ArtifactRef.model_validate(item[TEXT_KEY])
                ).decode()
            if set(item) == {IMAGE_KEY}:
                image = item[IMAGE_KEY]
                data = space.read_file(ArtifactRef.model_validate(image["ref"]))
                return image["prefix"] + base64.b64encode(data).decode()
            return {key: unpack(child) for key, child in item.items()}
        if isinstance(item, list):
            return [unpack(child) for child in item]
        return item

    return unpack(payload["value"])


def shared_dependencies(root: Path, ref: dict) -> list[dict]:
    """Discover text dependencies only in the explicitly versioned input format."""
    path = resolve_within(root, ref["path"])
    if path.suffix != ".json":
        return []
    with path.open("rb") as stream:
        prefix = stream.read(100)
    if b'"hvs.shared-json"' not in prefix:
        return []
    payload = json.loads(path.read_bytes())
    if payload.get("format") != FORMAT:
        return []
    if payload.get("version") != 1:
        raise ValueError("unsupported shared JSON archive")
    result = []

    def walk(item):
        if isinstance(item, dict):
            if set(item) == {TEXT_KEY}:
                result.append(ArtifactRef.model_validate(item[TEXT_KEY]).model_dump())
            elif set(item) == {IMAGE_KEY}:
                result.append(
                    ArtifactRef.model_validate(item[IMAGE_KEY]["ref"]).model_dump()
                )
            else:
                for child in item.values():
                    walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(payload["value"])
    return result
