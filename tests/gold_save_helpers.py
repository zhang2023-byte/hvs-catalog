"""Synthetic current-pair maintenance inputs; no real Gold or services."""

import json
from datetime import UTC, datetime

from evaluation_helpers import build_fresh_draft, write_synthetic_pdf

from hvs_catalog.evaluation import (
    GoldTarget,
    SaveApproval,
    check_save,
    compact_gold_annotation,
    parse_gold_annotation,
    read_compact_gold_pair,
    write_compact_gold_pair,
)


def make_pair(tmp_path, *, raw=None, scope="complete", unresolved=None):
    material = tmp_path / "materials"
    digest = write_synthetic_pdf(material / "paper.pdf")
    draft = parse_gold_annotation(raw or build_fresh_draft(pdf_sha256=digest))
    pair = compact_gold_annotation(
        draft,
        title="Synthetic candidate paper",
        original_annotator="synthetic-expert",
        original_annotated_at="2026-09-22",
        original_models=["synthetic-assistant"],
        reviewer="synthetic-reviewer",
        material_review={
            "value": "checked",
            "basis": "Synthetic material check",
            "record_sha256": "a" * 64,
            "differences": draft.material_correspondence.differences,
        },
        scope_review={
            "value": scope,
            "basis": "Synthetic scope review",
            "record_sha256": "a" * 64,
        },
        scope_limitations=[
            {"subject": "Table 3", "value": "Synthetic table not covered"}
        ]
        if scope == "partial"
        else [],
        unresolved=unresolved,
        handled_pending_ids=[p.item_id for p in draft.pending],
    )
    write_compact_gold_pair(pair, tmp_path / "pair")
    return material, tmp_path / "pair", pair


def authorize(binding):
    return SaveApproval(
        approved_by="synthetic-human",
        approver_kind="human",
        approved_at=datetime.now(UTC),
        binding=binding,
        binding_sha256=binding.sha256,
        basis="Synthetic human review and save permission",
        scientific_review=True,
        accepted_pending=binding.science_pending,
    )


def flow(tmp_path, **kwargs):
    material, pair_path, pair = make_pair(tmp_path, **kwargs)
    target = GoldTarget(tmp_path / "private-repo" / "data" / pair.answer.arxiv_id)
    checked = check_save(pair_path=pair_path, material_root=material, target=target)
    return {
        "pair_path": pair_path,
        "material_root": material,
        "target": target,
        "approval": authorize(checked.binding),
    }


def revise(args, *, value="613.0", work=None):
    pair = read_compact_gold_pair(args["pair_path"])
    current = args["target"].read_current()
    answer = pair.answer.model_dump(mode="json")
    answer["contributions"][0]["quantities"][0]["values"][0]["value"] = value
    return edit_pair(
        args, answer=answer, revision=current[1].binding.answer_sha256, work=work
    )


def edit_pair(args, *, answer=None, metadata=None, revision=None, work=None):
    import hashlib

    from hvs_catalog.evaluation import (
        CompactGoldAnswer,
        CompactGoldMetadata,
        CompactGoldPair,
        compact_answer_to_json,
    )

    pair = read_compact_gold_pair(args["pair_path"])
    answer = (
        CompactGoldAnswer.model_validate(answer) if answer is not None else pair.answer
    )
    metadata = metadata or pair.metadata.model_dump(mode="json", by_alias=True)
    metadata["review"]["answer_sha256"] = hashlib.sha256(
        compact_answer_to_json(answer).encode()
    ).hexdigest()
    if revision:
        metadata["revision"] = {
            "revised_by": "synthetic-preparer",
            "decided_by": "synthetic-human",
            "revised_at": "2026-09-30",
            "basis": "Synthetic correction",
            "previous_answer_sha256": revision,
        }
    pair = CompactGoldPair(
        answer=answer, metadata=CompactGoldMetadata.model_validate(metadata)
    )
    path = work or args["pair_path"]
    write_compact_gold_pair(pair, path)
    result = dict(args, pair_path=path)
    return result


def approve_revision(args):
    checked = check_save(
        pair_path=args["pair_path"],
        material_root=args["material_root"],
        target=args["target"],
        expected_old=args["target"].fingerprint(),
    )
    return dict(args, approval=authorize(checked.binding))


def snapshot(root):
    return (
        {
            str(p.relative_to(root)): p.read_bytes()
            for p in root.rglob("*")
            if p.is_file()
        }
        if root.exists()
        else {}
    )


def write_approval(args, path):
    path.write_text(json.dumps(args["approval"].model_dump(mode="json")))
    return path
