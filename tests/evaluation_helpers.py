"""Gold 工具测试驱动：合成旧文档、合成 PDF、全新草稿与独立进程中窗口。

合成材料全部虚构（SYN-* 编号），不涉及真实论文、真实答案或模型输出。
子进程场景配合信号文件定位故障窗口后 ``os._exit``，等价于进程死亡：
已写入文件保留，未完成部分丢弃。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from hvs_catalog.evaluation.legacy import legacy_canary
from hvs_catalog.records.rules import load_rules

WAIT_TIMEOUT = 60.0

MIGRATION_PROTOCOL = "contribution_migration_ai_assisted_v1"


def _evidence(location: str, quote: str = "") -> dict:
    return {"location": location, "quote": quote}


def build_legacy_v4_doc(
    *,
    arxiv_id: str = "2501.00001",
    status: str | None = None,
    contributions: list[dict] | None = None,
    reviewed_exclusions: list[dict] | None = None,
) -> dict:
    """构造一份有效的旧 contribution Gold v4 文档（合成材料）。"""
    if contributions is None:
        contributions = [build_legacy_contribution()]
    if status is None:
        status = "contributions_found" if contributions else "no_contributions"
    document = {
        "schema": {"name": "benchmark.hvs_contribution_annotation", "version": 4},
        "arxiv_id": arxiv_id,
        "annotator": "synthetic-annotator",
        "annotated_at": "2026-08-01T00:00:00+00:00",
        "guideline_version": "synthetic-guideline-0",
        "evidence_basis": "pdf",
        "annotation_process": {
            "protocol": MIGRATION_PROTOCOL,
            "preannotation_agent": "synthetic-pre-annotator",
            "preannotation_model": "synthetic-model-a",
            "reconciliation_agent": "synthetic-reconciler",
            "reconciliation_model": "synthetic-model-b",
            "expert_review_scope": "paper_level",
            "process_note": "synthetic process",
        },
        "canary": "",
        "status": status,
        "contributions": contributions,
        "reviewed_exclusions": reviewed_exclusions or [],
    }
    document["canary"] = legacy_canary(document)
    return document


def build_legacy_contribution(
    *,
    name: str = "SYN-A",
    summary: str = "Reports a new hypervelocity star candidate with radial velocity.",
    boundness_status: str = "unbound",
    quantities: list[dict] | None = None,
    conflicts: list[dict] | None = None,
    contribution_type: str = "candidates_found",
) -> dict:
    if quantities is None:
        quantities = [
            {
                "quantity": "observed_phase_space.radial_velocity",
                "values": [
                    {
                        "value": "612.0",
                        "error": "8.0",
                        "unit": "km/s",
                        "limit_kind": "none",
                        "condition": "heliocentric, adopted solution",
                        "source": "this_paper",
                        "evidence": [_evidence("Table 2, row SYN-A")],
                    }
                ],
            }
        ]
    return {
        "identifiers": [
            {"value": name, "evidence": [_evidence(f"Table 2, row {name}")]}
        ],
        "contribution_type": contribution_type,
        "contribution_summary": summary,
        "contribution_evidence": [_evidence("Section 4.1")],
        "paper_boundness": {
            "status": boundness_status,
            "evidence": []
            if boundness_status == "not_assessed"
            else [_evidence("Section 5, concluding remarks")],
        },
        "quantities": quantities,
        "quantity_conflicts": conflicts or [],
    }


def build_legacy_range_value(
    quantity: str = "observed_phase_space.distance",
    *,
    lower: str = "55.0",
    upper: str = "75.0",
    unit: str = "kpc",
) -> dict:
    return {
        "value": "",
        "error": "",
        "lower_error": "",
        "upper_error": "",
        "unit": unit,
        "limit_kind": "range",
        "range_lower": lower,
        "range_upper": upper,
        "condition": "90% confidence interval",
        "source": "this_paper",
        "evidence": [_evidence("Table 3")],
        "interval_kind": "confidence",
        "coverage": {
            "value": "90",
            "unit": "%",
            "evidence": [_evidence("Table 3 caption")],
        },
    }


def _pdf_text_line(line: str) -> bytes:
    """一行显示文本：圆括号转义后包装为 Tj 操作（3.11 兼容写法）。"""
    escaped = line.replace("(", "\\(").replace(")", "\\)")
    return f"({escaped}) Tj T*".encode()


def build_synthetic_pdf(pages: list[list[str]] | None = None) -> bytes:
    """构造一份结构完整、每页带可提取文本的合成 PDF。

    页对象声明 /Resources 字体，使成熟解析器能按页树定位并提取文本；
    内容流未压缩。不追求通用阅读器排版兼容。
    """
    if pages is None:
        pages = [
            ["Synthetic Hypervelocity Star Survey"],
            ["Table 2: candidate kinematics", "SYN-A 612.0 +- 8.0 km/s"],
            ["Section 5: conclusions"],
        ]
    objects: list[bytes] = []
    page_object_numbers: list[int] = []
    # 1: catalog, 2: pages tree, 3: font, then per page: page object + content
    next_number = 4
    for lines in pages:
        page_object_numbers.append(next_number)
        next_number += 2
    kids = " ".join(f"{number} 0 R" for number in page_object_numbers)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for number, lines in zip(page_object_numbers, pages, strict=True):
        content = (
            b"BT /F1 12 Tf 72 720 Td\n"
            + b"\n".join(_pdf_text_line(line) for line in lines)
            + b"\nET"
        )
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /Contents {number + 1} 0 R"
                " /Resources << /Font << /F1 3 0 R >> >> >>"
            ).encode()
        )
        objects.append(
            f"<< /Length {len(content)} >>\nstream\n".encode()
            + content
            + b"\nendstream"
        )
    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    body = bytearray()
    offsets = [0]
    for number, payload in enumerate(objects, start=1):
        offsets.append(len(header) + len(body))
        body += f"{number} 0 obj\n".encode() + payload + b"\nendobj\n"
    xref_offset = len(header) + len(body)
    xref = bytearray(b"xref\n0 ")
    xref += f"{len(objects) + 1}\n".encode()
    xref += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        xref += f"{offset:010d} 00000 n \n".encode()
    trailer = (
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n"
        f"{xref_offset}\n%%EOF\n"
    ).encode()
    return header + bytes(body) + bytes(xref) + trailer


def write_synthetic_pdf(path: Path, pages: list[list[str]] | None = None) -> str:
    data = build_synthetic_pdf(pages)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    (path.parent / "paper.tex").write_bytes(b"Synthetic source material.")
    import hashlib

    return hashlib.sha256(data).hexdigest()


def add_synthetic_metadata(document):
    """Explicit synthetic attribution/material facts, never production defaults."""
    import hashlib

    document["paper"]["version"] = "synthetic-v1"
    document["provenance"]["prepared_by"] = {
        "actor": {"actor_id": "synthetic-maker", "kind": "human", "model": None},
        "prepared_at": "2026-09-22T00:00:00+00:00",
        "action": "migration"
        if document["provenance"]["origin"] == "migration"
        else "initial_draft",
        "description": "Synthetic preparation for offline tests.",
    }
    document["material_correspondence"] = {
        "status": "checked",
        "pdf_file_ids": [x["file_id"] for x in document["files"]],
        "latex": {
            "name": "paper.tex",
            "sha256": hashlib.sha256(b"Synthetic source material.").hexdigest(),
            "version": "synthetic-v1",
        },
        "checked_by": {"actor_id": "synthetic-checker", "kind": "human"},
        "checked_at": "2026-09-22T00:00:00+00:00",
        "basis": "Synthetic matching materials.",
        "differences": [],
    }
    return document


def build_fresh_draft(
    *,
    document_id: str = "gold-synth-001",
    pdf_name: str = "paper.pdf",
    pdf_sha256: str | None = None,
    value: str = "612.0",
    error: str = "8.0",
    include_quote: bool = True,
    page: int | None = 2,
) -> dict:
    """构造一份全新草稿（合成材料，不需要旧答案）。

    ``pdf_sha256`` 为 None 时材料哈希待补（草稿允许），正式保存前须
    固定。
    """
    rules = load_rules()
    evidence = {
        "file_id": "pdf-1",
        "location": "Table 2, row SYN-A",
        "page": page,
        "quote": "SYN-A 612.0 +- 8.0 km/s" if include_quote else "",
    }
    return add_synthetic_metadata(
        {
            "schema": {"name": "evaluation.hvs_contribution_gold", "version": 1},
            "kind": "draft",
            "document_id": document_id,
            "paper": {"paper_key": "synth-001", "arxiv_id": "2501.00001"},
            "rules": {"version": rules.version, "sha256": rules.sha256},
            "files": [{"file_id": "pdf-1", "name": pdf_name, "sha256": pdf_sha256}],
            "status": "contributions_found",
            "contributions": [
                {
                    "contribution_id": "c1",
                    "legacy_index": None,
                    "identifiers": [{"value": "SYN-A", "evidence": [dict(evidence)]}],
                    "contribution_type": "candidates_found",
                    "contribution_summary": (
                        "Reports a new hypervelocity star candidate with radial velocity."
                    ),
                    "contribution_evidence": [
                        {"file_id": "pdf-1", "location": "Section 4.1", "page": 1}
                    ],
                    "paper_boundness": {
                        "status": "unbound",
                        "legacy_status": None,
                        "evidence": [
                            {
                                "file_id": "pdf-1",
                                "location": "Section 5, concluding remarks",
                                "page": 3,
                            }
                        ],
                    },
                    "quantities": [
                        {
                            "quantity": "observed_phase_space.radial_velocity",
                            "values": [
                                {
                                    "value": value,
                                    "error": error,
                                    "lower_error": None,
                                    "upper_error": None,
                                    "unit": "km/s",
                                    "limit_kind": "none",
                                    "range_lower": None,
                                    "range_upper": None,
                                    "coordinate_format": None,
                                    "condition": "heliocentric, adopted solution",
                                    "interval_kind": None,
                                    "coverage": None,
                                    "lower_quantile": None,
                                    "upper_quantile": None,
                                    "error_components": [],
                                    "representation": None,
                                    "source": "this_paper",
                                    "source_note": None,
                                    "evidence": [dict(evidence)],
                                    "context_evidence": [],
                                }
                            ],
                        }
                    ],
                    "quantity_conflicts": [],
                }
            ],
            "reviewed_exclusions": [],
            "provenance": {
                "origin": "new_annotation",
                "legacy": None,
                "note": "synthetic fresh draft",
            },
            "pending": [],
        }
    )


def build_approval_payload(
    preparation,
    *,
    approved_by: str = "synthetic-expert",
    basis: str = "Synthetic review of the prepared content.",
    accepted_pending: list[str] | None = None,
    manifest_sha256: str | None = None,
) -> dict:
    from hvs_catalog.evaluation import manifest_sha256 as hash_of

    manifest = preparation.manifest
    return {
        "approved_by": approved_by,
        "approver_kind": "human",
        "approved_at": "2026-09-22T00:00:00+00:00",
        "document_id": manifest.document_id,
        "target_path": manifest.target_path,
        "manifest_sha256": manifest_sha256 or hash_of(manifest),
        "expected_old": (
            None
            if manifest.expected_old is None
            else manifest.expected_old.model_dump(mode="json")
        ),
        "rules_version": manifest.rules_version,
        "rules_sha256": manifest.rules_sha256,
        "basis": basis,
        "accepted_pending": (
            accepted_pending
            if accepted_pending is not None
            else list(manifest.science_pending)
        ),
    }


# ---- 子进程场景 ----


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + WAIT_TIMEOUT
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"等待信号文件超时：{path}")
        time.sleep(0.02)


def _exit9() -> None:
    sys.stdout.flush()
    os._exit(9)


def _target(root: str):
    from hvs_catalog.evaluation import GoldTarget

    return GoldTarget(root)


def _load_approval(path: str):
    from hvs_catalog.evaluation import SaveApproval

    return SaveApproval.model_validate(json.loads(Path(path).read_text("utf-8")))


def scenario_save(
    draft: str,
    material_root: str,
    target_root: str,
    preparation: str,
    approval: str,
    operation_id: str,
) -> dict:
    from hvs_catalog.evaluation import approved_save

    receipt = approved_save(
        draft_path=draft,
        material_root=material_root,
        target=_target(target_root),
        preparation_dir=preparation,
        approval=_load_approval(approval),
        operation_id=operation_id,
    )
    return {
        "operation_id": receipt.operation_id,
        "version": receipt.version,
        "replayed": receipt.replayed,
        "recovered": receipt.recovered,
        "manifest": receipt.manifest_sha256,
    }


def scenario_save_exit(
    draft: str,
    material_root: str,
    target_root: str,
    preparation: str,
    approval: str,
    operation_id: str,
    window: str,
) -> dict:
    """在指定故障窗口硬退出：保存链路中真实进程终止。"""
    from hvs_catalog.evaluation import approved_save

    approved_save(
        draft_path=draft,
        material_root=material_root,
        target=_target(target_root),
        preparation_dir=preparation,
        approval=_load_approval(approval),
        operation_id=operation_id,
        fault_hooks={window: _exit9},
    )
    raise AssertionError("故障窗口场景不应运行到这里")


def scenario_hold_target_lock(target_root: str, signal: str, release: str) -> dict:
    """持有目标锁直到放行；持锁事实用信号文件告知父进程。"""
    target = _target(target_root)
    with target.lock():
        Path(signal).write_text("held", encoding="utf-8")
        _wait_for(Path(release))
    return {"held": True}


def scenario_query(target_root: str, operation_id: str) -> dict:
    from hvs_catalog.evaluation import query_operation

    status = query_operation(operation_id=operation_id, target=_target(target_root))
    return {"state": status.state, "detail": status.detail}


SCENARIOS = {
    "save": scenario_save,
    "save-exit": scenario_save_exit,
    "hold-target-lock": scenario_hold_target_lock,
    "query": scenario_query,
}


def main(argv: list[str]) -> None:
    scenario = argv[1]
    result = SCENARIOS[scenario](*argv[2:])
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv)
