"""Read-only Gold draft/answer checks report structure, material correspondence, and
pending science separately.

Structural/material problems must be absent before official save. Experts handle
scientific pending items and may explicitly approve unresolved states permitted by
rules. Check every draft item: structural/material gaps block, science items remain on
review lists and require explicit acceptance; none silently disappear during conversion.
Structural success is neither science approval nor a claim that unverifiable material
passed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import Field

from ..records.common import Config
from ..records.rules import author_state_ids
from ..workspace.errors import WorkspaceIntegrityError
from ..workspace.layout import resolve_within
from .models import GoldAnnotation, GoldContribution, iter_gold_evidence
from .pdfcheck import PdfAnalysis, analyze_pdf, quote_result

ProblemCategory = Literal["structure", "material", "science_pending"]


class CheckProblem(Config):
    category: ProblemCategory
    code: str
    subject: str
    message: str


class FileVerification(Config):
    file_id: str
    name: str
    sha256: str | None
    exists: bool
    hash_matches: bool | None
    openable: bool | None
    page_count: int | None
    notes: list[str] = Field(default_factory=list)


class QuoteVerification(Config):
    subject: str
    page: int | None
    quote: str
    result: Literal["matched", "not_found", "not_extractable", "skipped"]
    note: str


class GoldCheckReport(Config):
    document_sha256: str
    kind: str
    problems: list[CheckProblem] = Field(default_factory=list)
    files: list[FileVerification] = Field(default_factory=list)
    quotes: list[QuoteVerification] = Field(default_factory=list)
    scope: list[str] = Field(default_factory=list)

    def problems_of(self, category: ProblemCategory) -> list[CheckProblem]:
        return [item for item in self.problems if item.category == category]

    @property
    def blocking_count(self) -> int:
        """Count structural/material issues, which must be zero before official save."""
        return len(
            [
                item
                for item in self.problems
                if item.category in ("structure", "material")
            ]
        )

    def pending_ids(self) -> list[str]:
        return [
            item.code for item in self.problems if item.category == "science_pending"
        ]


@dataclass
class _Checker:
    material_root: Path
    problems: list[CheckProblem] = field(default_factory=list)
    files: list[FileVerification] = field(default_factory=list)
    quotes: list[QuoteVerification] = field(default_factory=list)
    # Analyze each material once with an established parser; None means missing or unreadable.
    pdf_analyses: dict[str, PdfAnalysis | None] = field(default_factory=dict)
    file_names: dict[str, str] = field(default_factory=dict)

    def problem(
        self, category: ProblemCategory, code: str, subject: str, message: str
    ) -> None:
        self.problems.append(
            CheckProblem(category=category, code=code, subject=subject, message=message)
        )

    def page_count(self, file_id: str) -> int | None:
        analysis = self.pdf_analyses.get(file_id)
        if analysis is None or not analysis.openable:
            return None
        return analysis.page_count


def check_gold_annotation(
    annotation: GoldAnnotation, *, material_root: Path
) -> GoldCheckReport:
    """Check migrated/fresh Gold drafts or official answers.

    Structural/material gaps must be zero before official save. Report science pending
    items and auxiliary quote checks accurately for both document types, without
    interpreting them as overall pass/fail. Process every draft item: science enters
    review lists; structural/material items block.
    """
    checker = _Checker(material_root=Path(material_root))
    raw = annotation.model_dump(mode="json", by_alias=True)
    document_sha256 = hashlib.sha256(
        json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()

    _check_files(annotation, checker)
    _check_metadata(annotation, checker)
    for subject, evidence in iter_gold_evidence(annotation):
        _check_evidence(evidence, subject, checker)

    for contribution in annotation.contributions:
        _check_contribution(contribution, checker)
    if annotation.reviewed_exclusions:
        checker.problem(
            "science_pending",
            "exclusions_pending_mapping",
            "reviewed_exclusions",
            "旧结构没有独立对象标识字段，排除记录保留为待对应项目，由专家对照原文处理",
        )
    if annotation.status == "no_contributions":
        checker.problem(
            "science_pending",
            "no_contributions_conclusion",
            "status",
            "保留旧标注结论（或全新标注的空名单声明）；不能据此声明新材料"
            "名单完整且 18 种物理量全部检查完成",
        )
    for item in annotation.pending:
        if item.category == "science_review":
            checker.problem(
                "science_pending",
                item.item_id,
                item.subject,
                f"草稿声明的科学待办（{item.item_id}）须由专家解决，或在批准中"
                f"明确接受后随答案保留：{item.description}",
            )
        else:
            checker.problem(
                "structure",
                f"pending_{item.category}",
                f"{item.subject}（{item.item_id}）",
                f"草稿声明的待办（{item.category}）属于结构或资料缺口，正式"
                f"保存前必须解决：{item.description}",
            )
    if annotation.kind == "answer":
        for item in annotation.pending:
            checker.problem(
                "structure",
                "answer_with_pending",
                item.item_id,
                f"正式答案不能携带待办条目：{item.description}",
            )

    scope = [
        "结构与引用：字段形状、物理量编码、状态词表、编号唯一性与文件引用",
        "材料对应：文件存在、哈希一致、可打开与页序号边界",
        "引文匹配为辅助结果，不用于证明科学内容正确",
        "科学待核查项由人类专家处理；本工具不进行科学审定",
        "已检查：标注身份、材料版本及对应记录完整性，固定 PDF 文件哈希",
        "未独立判定：引文语义、科学结论正确性和参考答案完整性",
    ]
    report = GoldCheckReport(
        document_sha256=document_sha256,
        kind=annotation.kind,
        problems=checker.problems,
        files=checker.files,
        quotes=checker.quotes,
        scope=scope,
    )
    return report


def _check_metadata(annotation: GoldAnnotation, checker: _Checker) -> None:
    if annotation.provenance.prepared_by is None:
        checker.problem(
            "structure",
            "missing_preparer",
            "provenance/prepared_by",
            "本版制作或修订者及时间待补",
        )
    if not annotation.paper.version or not annotation.paper.version.strip():
        checker.problem(
            "structure", "missing_paper_version", "paper/version", "论文版本待补"
        )
    if annotation.provenance.origin == "migration":
        original = annotation.provenance.original_expert
        if original is None or original.expert is None or original.annotated_at is None:
            checker.problem(
                "science_pending",
                "original_expert_unknown",
                "provenance/original_expert",
                "旧标注人类专家或时间尚未确认；保留原记录，不由当前审批者补猜",
            )
    correspondence = annotation.material_correspondence
    if correspondence.status != "checked":
        checker.problem(
            "material",
            "material_correspondence_pending",
            "material_correspondence",
            "固定PDF材料范围尚待专家核对",
        )
        return
    if set(correspondence.pdf_file_ids) != {item.file_id for item in annotation.files}:
        checker.problem(
            "material",
            "material_correspondence_incomplete",
            "material_correspondence/pdf_file_ids",
            "材料对应须覆盖本标注登记的全部 PDF",
        )
    if correspondence.differences:
        checker.problem(
            "science_pending",
            "material_differences",
            "material_correspondence/differences",
            "已知材料差异须纳入审批及后续评价范围："
            + "；".join(correspondence.differences),
        )


def _material_path(material_root: Path, name: str) -> Path:
    """Resolve material paths within the root; explicitly reject escapes and escaping
    symlinks.
    """
    try:
        return resolve_within(material_root, name)
    except WorkspaceIntegrityError as exc:
        raise WorkspaceIntegrityError(f"材料文件路径不安全（{name}）：{exc}") from exc


def _check_files(annotation: GoldAnnotation, checker: _Checker) -> None:
    if not annotation.files:
        checker.problem(
            "material",
            "no_registered_files",
            "files",
            "文档未登记任何 PDF 材料；专家证据无从绑定固定文件",
        )
    for item in annotation.files:
        checker.file_names[item.file_id] = item.name
        path = _material_path(checker.material_root, item.name)
        data = path.read_bytes() if path.is_file() else None
        if data is None:
            checker.pdf_analyses[item.file_id] = None
        else:
            checker.pdf_analyses[item.file_id] = analyze_pdf(data)
        analysis = checker.pdf_analyses[item.file_id]
        digest = hashlib.sha256(data).hexdigest() if data is not None else None
        hash_matches = None
        if item.sha256 is None:
            checker.problem(
                "material",
                "file_hash_pending",
                f"files/{item.file_id}",
                f"材料 {item.name} 尚未固定内容哈希，待补",
            )
        elif data is None:
            checker.problem(
                "material",
                "file_missing",
                f"files/{item.file_id}",
                f"材料文件缺失：{item.name}",
            )
        else:
            hash_matches = digest == item.sha256
            if hash_matches is False:
                checker.problem(
                    "material",
                    "file_hash_mismatch",
                    f"files/{item.file_id}",
                    f"材料 {item.name} 内容与登记哈希不符（实际 {digest}）",
                )
        if data is not None and analysis is not None and not analysis.openable:
            checker.problem(
                "material",
                "file_not_pdf",
                f"files/{item.file_id}",
                f"材料 {item.name} 不是可打开的完整 PDF：" + "；".join(analysis.notes),
            )
        checker.files.append(
            FileVerification(
                file_id=item.file_id,
                name=item.name,
                sha256=item.sha256 or digest,
                exists=data is not None,
                hash_matches=hash_matches,
                openable=None if analysis is None else analysis.openable,
                page_count=checker.page_count(item.file_id),
                notes=list(analysis.notes) if analysis is not None else [],
            )
        )


def _check_contribution(contribution: GoldContribution, checker: _Checker) -> None:
    subject = f"contributions/{contribution.contribution_id}"
    if not contribution.contribution_evidence:
        checker.problem(
            "material",
            "missing_contribution_evidence",
            subject,
            "贡献缺少必要证据（贡献摘要或类型所需的原文证据须核对）",
        )
    boundness = contribution.paper_boundness
    if boundness.status is None:
        checker.problem(
            "science_pending",
            "boundness_status_pending",
            f"{subject}/paper_boundness",
            "作者总体状态待定"
            + (
                f"（旧状态 {boundness.legacy_status!r} 保留待核查）"
                if boundness.legacy_status
                else ""
            ),
        )
    if boundness.legacy_status == "not_assessed":
        checker.problem(
            "science_pending",
            "legacy_not_assessed",
            f"{subject}/paper_boundness",
            "旧状态 not_assessed 涉及规则变化（当前词表已移除）；保留旧内容并列出待核查建议，不能由程序确认作者含义或推导 possibly_bound",
        )
    elif boundness.legacy_status == "no_overall_conclusion":
        checker.problem(
            "science_pending",
            "legacy_no_overall_conclusion",
            f"{subject}/paper_boundness",
            "旧状态 no_overall_conclusion 涉及规则定义变化；沿用时须由专家按当前规则核对含义",
        )
    if boundness.status is not None and boundness.status != "no_overall_conclusion":
        states = author_state_ids()
        if not boundness.evidence and boundness.status in states:
            checker.problem(
                "material",
                "missing_boundness_evidence",
                f"{subject}/paper_boundness",
                f"明确的作者判断 {boundness.status!r} 必须带有原文证据",
            )
    for identifier in contribution.identifiers:
        if not identifier.evidence:
            checker.problem(
                "material",
                "missing_identifier_evidence",
                f"{subject}/identifiers/{identifier.value}",
                f"名称 {identifier.value!r} 缺少证据",
            )
    for group in contribution.quantities:
        for index, value in enumerate(group.values):
            where = f"quantities/{group.quantity}#{index}"
            if not value.evidence:
                checker.problem(
                    "material",
                    "missing_value_evidence",
                    f"{subject}/{where}",
                    f"物理量 {group.quantity} 的数值缺少必要证据",
                )
            if value.condition is None or not value.condition.strip():
                checker.problem(
                    "material",
                    "missing_condition",
                    f"{subject}/{where}",
                    f"物理量 {group.quantity} 的数值缺少适用条件说明",
                )
    for conflict in contribution.quantity_conflicts:
        where = f"quantity_conflicts/{conflict.conflict_id}"
        if not conflict.alternatives:
            checker.problem(
                "science_pending",
                "conflict_structure_pending",
                f"{subject}/{where}",
                f"物理量 {conflict.quantity} 的冲突只有位置与原因，缺少双方完整数值结构；保留旧冲突，不猜测两组数值，由专家对照原文核查补全",
            )
        for index, alternative in enumerate(conflict.alternatives):
            if not alternative.evidence:
                checker.problem(
                    "material",
                    "missing_value_evidence",
                    f"{subject}/{where}/alternatives/{index}",
                    "冲突备选数值缺少必要证据",
                )


def _check_evidence(evidence, subject: str, checker: _Checker) -> None:
    if evidence.file_id is None:
        checker.problem(
            "material",
            "evidence_unbound",
            subject,
            f"证据未绑定 PDF 文件（位置 {evidence.location!r}），待补",
        )
        return
    page_count = checker.page_count(evidence.file_id)
    if (
        evidence.page is not None
        and page_count is not None
        and evidence.page > page_count
    ):
        checker.problem(
            "material",
            "page_out_of_range",
            subject,
            f"页序号 {evidence.page} 超出文件 "
            f"{checker.file_names.get(evidence.file_id)} 的 {page_count} 页",
        )
    analysis = checker.pdf_analyses.get(evidence.file_id)
    if analysis is not None and analysis.openable:
        result, note = quote_result(
            analysis.page_texts, page=evidence.page, quote=evidence.quote
        )
    else:
        result = "not_extractable"
        note = "文件缺失或无法作为 PDF 打开，引文未自动核验"
    checker.quotes.append(
        QuoteVerification(
            subject=subject,
            page=evidence.page,
            quote=evidence.quote,
            result=result,
            note=note,
        )
    )
