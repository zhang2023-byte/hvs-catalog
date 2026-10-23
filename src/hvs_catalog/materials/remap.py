"""Explicit, offline mappings from retained official page blocks."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _has_content(value):
    if isinstance(value, dict):
        return any(value.get(k) for k in ("html", "content", "image_path")) or any(
            _has_content(v) for v in value.values() if isinstance(v, (dict, list))
        )
    if isinstance(value, list):
        return any(_has_content(v) for v in value)
    return False


def _cross_page(value):
    if isinstance(value, dict):
        return bool(value.get("cross_page") or value.get("lines_deleted")) or any(
            _cross_page(v) for v in value.values() if isinstance(v, (dict, list))
        )
    return isinstance(value, list) and any(_cross_page(v) for v in value)


def _table_rows(block):
    """Read complete official cell strings solely to trace native block transfers."""
    from bs4 import BeautifulSoup

    if "blocks" in block:
        return [row for child in block["blocks"] for row in _table_rows(child)]
    html = "\n".join(
        span.get("html", "")
        for line in block.get("lines", [])
        for span in line.get("spans", [])
    )
    return [
        tuple(
            " ".join(cell.get_text(" ", strip=True).split())
            for cell in row.find_all(["td", "th"], recursive=False)
        )
        for row in BeautifulSoup(html, "html.parser").find_all("tr")
    ]


def _check_table_group(layout, selected):
    """Require both ends of evidenced row transfers; repeated headers are not links."""
    from collections import defaultdict

    native_rows = defaultdict(set)
    final_tables = []
    for page in layout["pdf_info"]:
        number = page["page_idx"] + 1
        final = page.get("para_blocks", [])
        for original in page.get("preproc_blocks", []):
            if original.get("type") != "table":
                continue
            matches = [
                i
                for i, block in enumerate(final)
                if all(
                    block.get(k) == original.get(k) for k in ("index", "type", "bbox")
                )
            ]
            location = (number, matches[0]) if len(matches) == 1 else (number, None)
            for row in _table_rows(original):
                if row:
                    native_rows[row].add(location)
        final_tables.extend(
            ((number, i), block)
            for i, block in enumerate(final)
            if block.get("type") == "table"
        )
    for destination, block in final_tables:
        for row in _table_rows(block):
            origins = native_rows.get(row, set())
            if len(origins) != 1:
                continue
            origin = next(iter(origins))
            if origin == destination:
                continue
            if (origin in selected) != (destination in selected):
                other = destination if origin in selected else origin
                raise ValueError(
                    f"Offline mapping omits linked table block on page {other[0]}, block {other[1]}"
                )


def _move_captions(pages, selected, moves):
    """Move whole native captions within a page after explicit PDF review.

    Child indices address the restored native blocks before any moves. Insert
    positions address destination children after all removals, in mapping order.
    """
    if not isinstance(moves, list):
        raise TypeError("Caption moves must be a list")
    planned, sources = [], set()
    for move in moves:
        if not isinstance(move, dict) or set(move) != {
            "page",
            "from_block",
            "child",
            "to_block",
            "insert_at",
        }:
            raise ValueError("Invalid caption move selection")
        if any(type(v) is not int or v < 0 for v in move.values()):
            raise ValueError("Caption move indices must be nonnegative integers")
        number, source, target = move["page"], move["from_block"], move["to_block"]
        if source == target or not {(number, source), (number, target)} <= selected:
            raise ValueError("Caption moves require two restored blocks on one page")
        blocks = pages[number][1]["para_blocks"]
        origin, destination = blocks[source], blocks[target]
        if origin["type"] != "table" or destination["type"] != "table":
            raise ValueError("Caption moves require table blocks")
        key = (number, source, move["child"])
        if key in sources:
            raise ValueError("Duplicate caption source")
        sources.add(key)
        children = origin.get("blocks", [])
        if move["child"] >= len(children):
            raise ValueError("Caption source is missing")
        caption = children[move["child"]]
        if caption.get("type") != "table_caption" or not _has_content(caption):
            raise ValueError("Only a complete nonempty table caption can move")
        if _cross_page(caption):
            raise ValueError("Caption source is not page-local")
        planned.append((move, origin, destination, caption))
    # Remove by identity so repeated text cannot select a different caption.
    for _, origin, _, caption in planned:
        origin["blocks"] = [c for c in origin["blocks"] if c is not caption]
    audit = []
    for move, origin, destination, caption in planned:
        children = destination["blocks"]
        if move["insert_at"] > len(children):
            raise ValueError("Caption destination position is out of range")
        children.insert(move["insert_at"], caption)
        change = {
            **move,
            "source_json_path": origin["source_mapping"]["json_path"]
            + f".blocks[{move['child']}]",
            "official_caption_sha256": _digest(caption),
            "destination_block_id": f"p{move['page']}-b{move['to_block']}",
        }
        for block in (origin, destination):
            block["source_mapping"].setdefault("caption_moves", []).append(change)
        audit.append(change)
    return audit


def remap_native_layout(layout, mapping):
    """Replace only explicitly selected blocks; never split or edit recognized text.

    Match the official block's index, type and geometry on the same physical page.
    A partial or ambiguous native match is an error, not a license to guess.
    """
    keys = {"version", "replacements", "recover_discarded_pages"}
    version = mapping.get("version")
    if version == 2:
        keys.add("caption_moves")
    if type(version) is not int or version not in {1, 2} or set(mapping) != keys:
        raise ValueError("Unsupported offline material mapping")
    if not isinstance(mapping["replacements"], list) or not isinstance(
        mapping["recover_discarded_pages"], list
    ):
        raise TypeError("Mapping selections must be lists")
    if not mapping["replacements"] and not mapping["recover_discarded_pages"]:
        raise ValueError("Offline material mapping must select content")
    if "pdf_info" not in layout:
        raise ValueError("Offline page mapping requires native official layout")
    document = deepcopy(layout)
    pages = {}
    for position, page in enumerate(document["pdf_info"]):
        number = page["page_idx"] + 1
        if number in pages:
            raise ValueError("Duplicate native PDF page")
        pages[number] = (position, page)
    changes, seen = [], set()
    for item in mapping["replacements"]:
        if not isinstance(item, dict) or set(item) != {"page", "block"}:
            raise ValueError("A replacement needs a page and block index")
        number, index = item["page"], item["block"]
        if type(number) is not int or number < 1 or type(index) is not int or index < 0:
            raise ValueError("Invalid mapping page or block index")
        if (number, index) in seen:
            raise ValueError("Duplicate block replacement")
        seen.add((number, index))
        if number not in pages:
            raise ValueError("Mapping selects a missing PDF page")
        position, page = pages[number]
        final = page.get("para_blocks", [])
        if index >= len(final):
            raise ValueError("Mapping selects a missing PDF block")
        old = final[index]
        key = (old.get("index"), old.get("type"), old.get("bbox"))
        matches = [
            (i, b)
            for i, b in enumerate(page.get("preproc_blocks", []))
            if (b.get("index"), b.get("type"), b.get("bbox")) == key
        ]
        if key[0] is None or len(matches) != 1:
            raise ValueError("Native block match is missing or ambiguous")
        original_index, original = matches[0]
        if not _has_content(original) or _cross_page(original):
            raise ValueError("Native replacement lacks unmerged page content")
        origin = {
            "collection": "preproc_blocks",
            "page": number,
            "block_index": original_index,
            "native_index": original["index"],
            "json_path": f"pdf_info[{position}].preproc_blocks[{original_index}]",
        }
        restored = deepcopy(original)
        restored["source_mapping"] = origin
        final[index] = restored
        changes.append(
            {
                "block_id": f"p{number}-b{index}",
                "source": origin,
                "previous_block_sha256": _digest(old),
                "official_block_sha256": _digest(original),
            }
        )
    _check_table_group(layout, seen)
    recovered = set()
    for number in mapping["recover_discarded_pages"]:
        if (
            type(number) is not int
            or number < 1
            or number not in pages
            or number in recovered
        ):
            raise ValueError("Invalid or duplicate discarded-content page")
        recovered.add(number)
        position, page = pages[number]
        blocks = page.setdefault("para_blocks", [])
        if _has_content(blocks):
            raise ValueError("Discarded-content recovery requires an empty body page")
        discarded = page.get("discarded_blocks", [])
        if not _has_content(discarded):
            raise ValueError("No official discarded content to recover")
        for i, original in enumerate(discarded):
            if not _has_content(original):
                continue
            if _cross_page(original):
                raise ValueError("Discarded block is not page-local")
            origin = {
                "collection": "discarded_blocks",
                "page": number,
                "block_index": i,
                "native_type": original["type"],
                "json_path": f"pdf_info[{position}].discarded_blocks[{i}]",
            }
            restored = deepcopy(original)
            restored["source_mapping"] = origin
            index = len(blocks)
            blocks.append(restored)
            changes.append(
                {
                    "block_id": f"p{number}-b{index}",
                    "source": origin,
                    "previous_block_sha256": None,
                    "official_block_sha256": _digest(original),
                }
            )
    caption_moves = _move_captions(pages, seen, mapping.get("caption_moves", []))
    report = {
        "format": "hvs.offline-material-mapping",
        "version": version,
        "selection": mapping,
        "changes": changes,
        "content_policy": "Verbatim official blocks; original classifications retained; no scientific correction.",
        "markdown_policy": "The archived Markdown is unchanged; the versioned index defines model input.",
    }
    if version == 2:
        report["caption_moves"] = caption_moves
        report["content_policy"] = (
            "Verbatim official blocks and captions; explicit caption ownership "
            "corrections; original classifications retained; no scientific correction."
        )
    return document, report
