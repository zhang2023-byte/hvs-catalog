"""Read-only legacy contribution Gold v4 migration to drafts and item reports.

Mechanically preserve objects, contribution types, summaries, numeric strings, and
evidence locations. Map empty fields to null. Retain old contents and pending checks for
legacy state meanings, conflict structure, excluded-object IDs, and incomplete
materials. Missing required fields remain pending rather than fabricated. Assign
reproducible c/k/x identifiers in legacy order. Before writing, check path/file identity
overlap with every legacy/PDF input and refuse different existing drafts/reports;
preserve original bytes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..records.common import Config
from ..records.rules import load_rules
from .checking import GoldCheckReport, check_gold_annotation
from .errors import GoldOutputConflictError
from .legacy import (
    LegacyAnnotation,
    LegacyContribution,
    LegacyErrorComponent,
    LegacyEvidence,
    LegacyIntervalProbability,
    LegacyReportedValue,
    read_legacy_annotation,
    verify_legacy_canary,
)
from .models import (
    GOLD_SCHEMA_NAME,
    GOLD_SCHEMA_VERSION,
    GoldAnnotation,
    GoldBoundness,
    GoldConflict,
    GoldContribution,
    GoldErrorComponent,
    GoldEvidence,
    GoldExclusion,
    GoldIdentifier,
    GoldLegacySource,
    GoldPaper,
    GoldPdfFile,
    GoldPendingItem,
    GoldProbability,
    GoldProvenance,
    GoldQuantityGroup,
    GoldRulesRef,
    GoldSchemaTag,
    GoldValue,
    gold_annotation_to_json,
)

DRAFT_FILENAME = "draft.json"
REPORT_FILENAME = "migration-report.json"

MigrationCategory = Literal[
    "preserved", "format_adjusted", "missing_info", "science_review"
]


class MigrationReportItem(Config):
    item_id: str
    category: MigrationCategory
    old_path: str
    new_path: str | None = None
    description: str


class MigrationReport(Config):
    legacy_file: str
    legacy_sha256: str
    legacy_schema: str
    canary_present: bool
    canary_verified: bool
    known_files: list[str]
    items: list[MigrationReportItem]
    summary: dict[str, int]

    @property
    def open_items(self) -> list[MigrationReportItem]:
        """Item awaiting additional material or scientific review."""
        return [
            item
            for item in self.items
            if item.category in ("missing_info", "science_review")
        ]


@dataclass
class MigrationResult:
    draft: GoldAnnotation
    report: MigrationReport
    draft_path: Path
    report_path: Path
    check: GoldCheckReport | None


class _ReportBuilder:
    def __init__(self) -> None:
        self.items: list[MigrationReportItem] = []
        self._counter = 0

    def add(
        self,
        category: MigrationCategory,
        *,
        old_path: str,
        new_path: str | None,
        description: str,
    ) -> None:
        self._counter += 1
        self.items.append(
            MigrationReportItem(
                item_id=f"m{self._counter:03d}",
                category=category,
                old_path=old_path,
                new_path=new_path,
                description=description,
            )
        )


def _map_evidence(evidence: LegacyEvidence, *, file_id: str | None) -> GoldEvidence:
    """Preserve old evidence verbatim; never infer page ordinals from location text or
    invent missing quotes.
    """
    return GoldEvidence(
        file_id=file_id,
        location=evidence.location,
        page=None,
        quote=evidence.quote,
    )


def _map_probability(
    item: LegacyIntervalProbability, *, file_id: str | None
) -> GoldProbability:
    return GoldProbability(
        value=item.value,
        unit=item.unit,
        evidence=[_map_evidence(entry, file_id=file_id) for entry in item.evidence],
    )


def _map_component(
    item: LegacyErrorComponent, *, file_id: str | None
) -> GoldErrorComponent:
    return GoldErrorComponent(
        name=item.name,
        error=item.error or None,
        lower_error=item.lower_error or None,
        upper_error=item.upper_error or None,
        evidence=[_map_evidence(entry, file_id=file_id) for entry in item.evidence],
    )


def _map_value(item: LegacyReportedValue, *, file_id: str | None) -> GoldValue:
    return GoldValue(
        value=item.value or None,
        error=item.error or None,
        lower_error=item.lower_error or None,
        upper_error=item.upper_error or None,
        unit=item.unit or None,
        limit_kind=item.limit_kind,
        range_lower=item.range_lower or None,
        range_upper=item.range_upper or None,
        coordinate_format=item.coordinate_format,
        condition=item.condition or None,
        interval_kind=item.interval_kind,
        coverage=(
            _map_probability(item.coverage, file_id=file_id)
            if item.coverage is not None
            else None
        ),
        lower_quantile=(
            _map_probability(item.lower_quantile, file_id=file_id)
            if item.lower_quantile is not None
            else None
        ),
        upper_quantile=(
            _map_probability(item.upper_quantile, file_id=file_id)
            if item.upper_quantile is not None
            else None
        ),
        error_components=[
            _map_component(component, file_id=file_id)
            for component in item.error_components
        ],
        representation=item.representation,
        source=item.source,
        source_note=item.source_note or None,
        evidence=[_map_evidence(entry, file_id=file_id) for entry in item.evidence],
        context_evidence=[
            _map_evidence(entry, file_id=file_id) for entry in item.context_evidence
        ],
    )


_STATUS_NOTES = {
    "not_assessed": (
        "旧状态 not_assessed 在当前规则中已移除；保留旧内容并列出待核查建议，"
        "不能由程序确认作者含义或推导 possibly_bound"
    ),
    "no_overall_conclusion": (
        "旧状态 no_overall_conclusion 涉及规则定义变化；沿用时须按当前规则由"
        "专家核对含义"
    ),
}


def _map_contribution(
    contribution: LegacyContribution,
    *,
    index: int,
    conflict_offset: int,
    file_id: str | None,
    pending: list[GoldPendingItem],
    builder: _ReportBuilder,
) -> GoldContribution:
    contribution_id = f"c{index + 1}"
    old = f"contributions[{index}]"
    new = f"contributions/{contribution_id}"
    builder.add(
        "preserved",
        old_path=old,
        new_path=new,
        description="对象名称、贡献类型与研究摘要原样进入草稿，分配可复现的本地"
        "技术编号；不根据名称相似合并对象，不自动改写摘要",
    )
    builder.add(
        "preserved",
        old_path=f"{old}/identifiers",
        new_path=f"{new}/identifiers",
        description="对象名称及其证据原样保留并记录旧条目位置",
    )
    legacy_status = contribution.paper_boundness.status
    status_note = _STATUS_NOTES.get(
        legacy_status,
        "旧状态保留为迁移来源；新词表状态待专家按当前规则确认后填写",
    )
    builder.add(
        "science_review",
        old_path=f"{old}/paper_boundness/status",
        new_path=f"{new}/paper_boundness/legacy_status",
        description=status_note,
    )
    pending.append(
        GoldPendingItem(
            item_id=f"p-bound-{contribution_id}",
            category="science_review",
            subject=f"contributions/{contribution_id}/paper_boundness",
            description=status_note,
        )
    )
    for group_index, group in enumerate(contribution.quantities):
        builder.add(
            "preserved",
            old_path=f"{old}/quantities[{group_index}]",
            new_path=f"{new}/quantities/{group.quantity}",
            description=f"物理量 {group.quantity} 保留原始字符串、分组及全部"
            f"{len(group.values)}个多值；不补数字、不换算单位，也不根据空条件"
            "推断原文未报告条件",
        )
        for value_index, value in enumerate(group.values):
            empty_fields = [
                name
                for name in (
                    "value",
                    "error",
                    "lower_error",
                    "upper_error",
                    "unit",
                    "range_lower",
                    "range_upper",
                    "condition",
                    "source_note",
                )
                if getattr(value, name) == ""
            ]
            if empty_fields:
                builder.add(
                    "format_adjusted",
                    old_path=f"{old}/quantities[{group_index}].values[{value_index}]",
                    new_path=f"{new}/quantities/{group.quantity}/{value_index}",
                    description=f"明确表示缺值的空字段转为新格式空值：{empty_fields}",
                )
            nested = [
                name
                for name in (
                    "interval_kind",
                    "coverage",
                    "lower_quantile",
                    "upper_quantile",
                    "error_components",
                    "representation",
                )
                if getattr(value, name) not in (None, [])
            ]
            if nested:
                builder.add(
                    "preserved",
                    old_path=f"{old}/quantities[{group_index}].values[{value_index}]",
                    new_path=f"{new}/quantities/{group.quantity}/{value_index}",
                    description="已填写的区间解释、覆盖概率、分位点、具名误差分量与"
                    f"表示方式原样保留：{sorted(nested)}；不推算包含关系、置信"
                    "水平或总误差",
                )
    for conflict_index, conflict in enumerate(contribution.quantity_conflicts):
        conflict_id = f"k{conflict_offset + conflict_index + 1}"
        builder.add(
            "science_review",
            old_path=f"{old}/quantity_conflicts[{conflict_index}]",
            new_path=f"{new}/quantity_conflicts/{conflict_id}",
            description=f"物理量 {conflict.quantity} 的冲突保留原因与全部旧证据；"
            "旧格式没有要求冲突双方的完整数值结构，不能凭原因文字编造双方"
            "数值，待专家对照原文核查补全",
        )
        pending.append(
            GoldPendingItem(
                item_id=f"p-conflict-{conflict_id}",
                category="structural_gap",
                subject=f"contributions/{contribution_id}/quantity_conflicts/{conflict_id}",
                description="冲突缺少双方完整数值结构，保留旧证据待补",
            )
        )
    return GoldContribution(
        contribution_id=contribution_id,
        legacy_index=index,
        identifiers=[
            GoldIdentifier(
                value=identifier.value,
                evidence=[
                    _map_evidence(entry, file_id=file_id)
                    for entry in identifier.evidence
                ],
            )
            for identifier in contribution.identifiers
        ],
        contribution_type=contribution.contribution_type,
        contribution_summary=contribution.contribution_summary,
        contribution_evidence=[
            _map_evidence(entry, file_id=file_id)
            for entry in contribution.contribution_evidence
        ],
        paper_boundness=GoldBoundness(
            status=None,
            legacy_status=legacy_status,
            evidence=[
                _map_evidence(entry, file_id=file_id)
                for entry in contribution.paper_boundness.evidence
            ],
        ),
        quantities=[
            GoldQuantityGroup(
                quantity=group.quantity,
                values=[_map_value(value, file_id=file_id) for value in group.values],
            )
            for group in contribution.quantities
        ],
        quantity_conflicts=[
            GoldConflict(
                conflict_id=f"k{conflict_offset + conflict_index + 1}",
                quantity=conflict.quantity,
                reason=conflict.reason,
                evidence=[
                    _map_evidence(entry, file_id=file_id) for entry in conflict.evidence
                ],
                alternatives=[],
            )
            for conflict_index, conflict in enumerate(contribution.quantity_conflicts)
        ],
    )


def _legacy_source(
    annotation: LegacyAnnotation, *, file_name: str, sha256: str
) -> GoldLegacySource:
    return GoldLegacySource(
        schema_name=annotation.schema_tag.name,
        schema_version=annotation.schema_tag.version,
        file_name=file_name,
        sha256=sha256,
        arxiv_id=annotation.arxiv_id,
        annotator=annotation.annotator,
        annotated_at=annotation.annotated_at,
        guideline_version=annotation.guideline_version,
        annotation_process=annotation.annotation_process.model_dump(mode="json"),
        canary=annotation.canary,
    )


def _paths_coincide(left: Path, right: Path) -> bool:
    """Determine whether paths identify one file, including equal paths, symlinks, and
    hard-link aliases.
    """
    if left == right:
        return True
    try:
        if left.exists() and right.exists():
            stat_left, stat_right = left.stat(), right.stat()
            if (stat_left.st_dev, stat_left.st_ino) == (
                stat_right.st_dev,
                stat_right.st_ino,
            ):
                return True
    except OSError:
        pass
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def _write_output_protected(
    path: Path, data: bytes, *, label: str, inputs: list[Path]
) -> None:
    """Check overlap with all read-only inputs and existing outputs before migration
    writes.

    Reject output aliasing legacy/PDF inputs through paths, symlinks, or hard links.
    Never silently overwrite different same-name drafts/reports; identical contents
    count as retries and leave files unchanged.
    """
    for source in inputs:
        if _paths_coincide(path, source):
            raise GoldOutputConflictError(
                f"{label}输出 {path} 与只读输入 {source} 指同一文件"
                "（路径、符号链接或硬链接重合）；拒绝写入，旧输入保持不变"
            )
    if path.exists() or path.is_symlink():
        if path.is_file():
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise GoldOutputConflictError(
                    f"{label}输出 {path} 已存在且无法读取（{exc}）；"
                    "拒绝覆盖，换用新的输出目录"
                ) from exc
            if existing == data:
                return  # Identical output retry: do not rewrite matching contents.
            raise GoldOutputConflictError(
                f"{label}输出 {path} 已存在且内容不同；不会静默截断或覆盖，"
                "换用新的输出目录，或人工确认后移走旧文件"
            )
        raise GoldOutputConflictError(
            f"{label}输出 {path} 已存在且不是普通文件；拒绝写入，已有资料保持不变"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def migrate_legacy_annotation(
    *,
    legacy_path: str | Path,
    draft_dir: str | Path,
    document_id: str | None = None,
    known_pdfs: list[str | Path] | None = None,
    material_root: str | Path | None = None,
) -> MigrationResult:
    """Read legacy v4 and produce migration drafts/item reports without modifying input.

    known_pdfs supplies fixed paper PDFs; bind old evidence to the first, hashing actual
    files. Without them, evidence remains unbound with pending material requests. When
    material_root is supplied, run checks and return their results too.
    """
    legacy_path = Path(legacy_path)
    raw_text = legacy_path.read_text(encoding="utf-8")
    payload = json.loads(raw_text)
    annotation = read_legacy_annotation(legacy_path)
    legacy_sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    canary_verified = verify_legacy_canary(annotation, document=payload)

    builder = _ReportBuilder()
    pending: list[GoldPendingItem] = []
    files: list[GoldPdfFile] = []
    file_id: str | None = None
    known_names: list[str] = []
    for index, pdf_path in enumerate(known_pdfs or []):
        pdf_path = Path(pdf_path)
        entry_id = f"pdf-{index + 1}"
        digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        files.append(GoldPdfFile(file_id=entry_id, name=pdf_path.name, sha256=digest))
        known_names.append(pdf_path.name)
        if index == 0:
            file_id = entry_id
    if file_id is None:
        builder.add(
            "missing_info",
            old_path="evidence (pdf)",
            new_path=None,
            description="未提供已知材料清单；旧证据的位置与引文原样保留，对固定 "
            "PDF 文件的引用待补，不虚构文件哈希或页码",
        )
    else:
        builder.add(
            "format_adjusted",
            old_path="evidence (pdf)",
            new_path=f"files/{file_id}",
            description="旧证据按标注依据绑定到已知论文 PDF；旧位置说明与可选引文"
            "逐字保留，不通过正则猜出页序号",
        )

    doc_id = document_id or f"gold-{annotation.arxiv_id}"
    builder.add(
        "preserved",
        old_path="arxiv_id/annotator/annotated_at/guideline_version/annotation_process",
        new_path="provenance/legacy",
        description="论文编号、标注者、时间、规则版本和标注过程保留为原始标注与"
        "迁移来源；旧规则版本、旧批准记录不能替代新规则和本次批准",
    )
    builder.add(
        "preserved",
        old_path="canary",
        new_path="provenance/legacy/canary",
        description=f"旧 canary 归档信息保留并在读取时核对完整性（一致："
        f"{canary_verified}）；不当用作科学审定凭据，不复制为新答案的批准标记",
    )

    contributions = []
    conflict_offset = 0
    for index, contribution in enumerate(annotation.contributions):
        contributions.append(
            _map_contribution(
                contribution,
                index=index,
                conflict_offset=conflict_offset,
                file_id=file_id,
                pending=pending,
                builder=builder,
            )
        )
        conflict_offset += len(contribution.quantity_conflicts)
    exclusions = [
        GoldExclusion(
            exclusion_id=f"x{index + 1}",
            reason=item.reason,
            evidence=[_map_evidence(entry, file_id=file_id) for entry in item.evidence],
        )
        for index, item in enumerate(annotation.reviewed_exclusions)
    ]
    if exclusions:
        builder.add(
            "science_review",
            old_path="reviewed_exclusions",
            new_path="reviewed_exclusions",
            description="旧结构没有独立对象标识字段；排除原因与证据完整保留为待对"
            "应项目，不能根据自由文本自动制造被排除对象",
        )
        pending.append(
            GoldPendingItem(
                item_id="p-exclusions",
                category="science_review",
                subject="reviewed_exclusions",
                description="排除记录待专家对照原文建立对象对应",
            )
        )
    builder.add(
        "preserved",
        old_path="status",
        new_path="status",
        description=f"旧标注结论 {annotation.status!r} 保留；不能据此声明新材料名单"
        "完整或 18 种物理量全部检查完成",
    )
    if annotation.status == "no_contributions":
        pending.append(
            GoldPendingItem(
                item_id="p-no-contributions",
                category="science_review",
                subject="status",
                description="旧结论为 no_contributions，新材料检查范围未给出，保留"
                "旧结论待核查",
            )
        )

    rules = load_rules()
    for subject, description in (
        ("provenance/prepared_by", "本次迁移制作或修订者及时间待补；不由工具猜测"),
        ("paper/version", "论文版本待补；旧编号不能自动补出版本"),
        ("material_correspondence", "PDF 与 LaTeX 固定材料及专家对应核对记录待补"),
    ):
        builder.add(
            "missing_info",
            old_path="annotation metadata",
            new_path=subject,
            description=description,
        )
    draft = GoldAnnotation(
        schema_tag=GoldSchemaTag(name=GOLD_SCHEMA_NAME, version=GOLD_SCHEMA_VERSION),
        kind="draft",
        document_id=doc_id,
        paper=GoldPaper(
            paper_key=f"legacy-{annotation.arxiv_id}", arxiv_id=annotation.arxiv_id
        ),
        rules=GoldRulesRef(version=rules.version, sha256=rules.sha256),
        files=files,
        status=annotation.status,
        contributions=contributions,
        reviewed_exclusions=exclusions,
        provenance=GoldProvenance(
            origin="migration",
            legacy=_legacy_source(
                annotation, file_name=legacy_path.name, sha256=legacy_sha
            ),
            note="机械迁移草稿；科学含义未经审定",
        ),
        pending=pending,
    )

    categories: dict[str, int] = {}
    for item in builder.items:
        categories[item.category] = categories.get(item.category, 0) + 1
    report = MigrationReport(
        legacy_file=legacy_path.name,
        legacy_sha256=legacy_sha,
        legacy_schema=f"{annotation.schema_tag.name} v{annotation.schema_tag.version}",
        canary_present=bool(annotation.canary),
        canary_verified=canary_verified,
        known_files=known_names,
        items=builder.items,
        summary=categories,
    )

    draft_dir = Path(draft_dir)
    draft_path = draft_dir / DRAFT_FILENAME
    report_path = draft_dir / REPORT_FILENAME
    draft_data = gold_annotation_to_json(draft).encode("utf-8")
    report_data = (
        json.dumps(
            report.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n"
    ).encode("utf-8")
    inputs = [legacy_path, *(Path(item) for item in (known_pdfs or []))]
    _write_output_protected(draft_path, draft_data, label="迁移草稿", inputs=inputs)
    _write_output_protected(report_path, report_data, label="迁移报告", inputs=inputs)
    check = (
        check_gold_annotation(draft, material_root=Path(material_root))
        if material_root is not None
        else None
    )
    return MigrationResult(
        draft=draft,
        report=report,
        draft_path=draft_path,
        report_path=report_path,
        check=check,
    )
