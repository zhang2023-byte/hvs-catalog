"""既有 Gold 与提取输出到评分输入的纯内存适配（评分集成）。

适配方向与边界：

- ``adapt_compact_gold_pair`` / ``adapt_compact_answer`` /
  ``adapt_annotation``：把既有 Gold 共同定义对象（compact v2 正式
  答案配对、单份答案或 v1 标注答案）映射为评分侧 ``GoldPaperEntry``。
  原报数值、误差、单位、区间解释、条件与来源逐字保留，不改写 PDF
  标注格式；数值证据的 PDF 位置与引文进入 ``context_evidence`` 供诊
  断，不冒充已核对材料。配对适配先执行内存可完成的绑定校验
  （论文、scope、``answer_sha256``、证据文件声明）并保留绑定供报告
  与计划核对；再复用 Gold 完整检查器中不读文件的同一准备检查
  （``pair_preparation_problems``），材料缺口与科学待决都程序化阻止
  就绪输出。answer-only 输入不做 metadata 绑定核对，不构成正式就绪
  （严格条目入口拒绝并指向 ``adapt_compact_answer`` 非正式结构）。
- ``prediction_from_execution``：把一次共享提取执行保存的结果记录映
  射为 ``PredictionPaperEntry``；交付与完成状态来自运行清单实际状
  态和已保存记录（后续失败不抹已存名单），科学完成状态另见
  ``science_completion``。

适配是纯内存转换：不读文件、不调用服务、不修改输入对象，也不把
Gold 或评分诊断回写提取。Gold 值未给条件（null/空白）时拒绝适配并
报告准备缺口——字段缺失不等于“原文未报告条件”的科学断言；原已
明确填写并审定的条件说明逐字保留。真实 Gold 的读取与使用仍按获准
私有评分空间执行，本模块不提供真实仓扫描。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..pipeline import execution_result_refs, get_extraction
from ..records.common import RecordRef
from ..records.paper import (
    CoverageReport,
    DirectEvidencePart,
    ErrorComponent,
    MeasuredValue,
    PaperRecord,
    QuantileReport,
)
from ..records.roster import RosterRecord
from ..records.source import SourcePackageRecord
from ..runtime import get_execution as runtime_get_execution
from ..workspace import Workspace
from .compact import (
    CompactGoldAnswer,
    CompactGoldPair,
    pair_preparation_problems,
    validate_compact_gold_pair,
)
from .errors import GoldIntegrityError
from .models import GoldAnnotation
from .scoring import (
    ConfirmedRange,
    GoldObjectEntry,
    GoldPaperEntry,
    GoldQuantityEntry,
    GoldValueEntry,
    PredictionObjectEntry,
    PredictionPaperEntry,
    PredictionQuantityEntry,
    PredictionValueEntry,
)

__all__ = [
    "GoldAdaptation",
    "GoldAdaptationError",
    "adapt_annotation",
    "adapt_compact_answer",
    "adapt_compact_gold_pair",
    "gold_entries_from_annotation",
    "gold_entries_from_compact_answer",
    "gold_entries_from_compact_pair",
    "prediction_from_execution",
    "science_completion",
]


class GoldAdaptationError(ValueError):
    """Gold 或提取输出无法诚实映射为评分输入（准备缺口等）。"""


#: answer-only 适配的就绪边界说明：不阻断结构映射，但不构成正式就绪。
ANSWER_ONLY_LIMITATION = (
    "answer-only：未执行 metadata 绑定核对（answer_sha256/范围审定/材料"
    "声明），不构成正式就绪；正式评分请使用经检查的完整配对"
)


def _evidence_texts(evidences: list[Any]) -> list[str]:
    """逐字保留证据位置与可选引文（评分诊断用 sidecar）。"""
    texts: list[str] = []
    for evidence in evidences:
        location = str(evidence.location)
        quote = getattr(evidence, "quote", "") or ""
        texts.append(f"{location} — {quote}" if quote else location)
    return texts


def _ref(record_id: str) -> RecordRef:
    # 评分输入的证据引用只用于定位诊断；Gold 的 PDF 证据不是空间记录，
    # 使用派生定位标识，不冒充已核对的记录引用。
    return RecordRef(record_id=record_id, revision=1)


def _part(part: str, raw: str, evidence_id: str) -> DirectEvidencePart:
    return DirectEvidencePart(
        part=part, evidence_ref=_ref(evidence_id), raw_fragment=raw
    )


def _probability(report: Any, evidence_id: str) -> CoverageReport | None:
    if report is None:
        return None
    return CoverageReport(
        value=report.value,
        unit=report.unit,
        direct_evidence=[_part("value", report.value, evidence_id)],
    )


def _quantile(report: Any, evidence_id: str) -> QuantileReport | None:
    if report is None:
        return None
    return QuantileReport(
        value=report.value,
        unit=report.unit,
        direct_evidence=[_part("value", report.value, evidence_id)],
    )


def _measured_value(value: Any, *, value_id: str) -> MeasuredValue:
    """把一条 Gold 数值映射为 ``MeasuredValue``；原报内容逐字保留。"""
    condition = value.condition
    if condition is None or not condition.strip():
        raise GoldAdaptationError(
            f"Gold 数值 {value_id} 的条件为空：字段缺失不等于“原文未报告条件”"
            "的科学断言，不能为评分匹配自动补写；请补全条件或按准备缺口处理"
        )
    evidence_id = f"{value_id}:evidence"
    direct: list[DirectEvidencePart] = []
    for part_name in (
        "value",
        "error",
        "lower_error",
        "upper_error",
        "range_lower",
        "range_upper",
    ):
        raw = getattr(value, part_name)
        if raw is not None:
            direct.append(_part(part_name, raw, evidence_id))
    components: list[ErrorComponent] = []
    for index, component in enumerate(value.error_components):
        component_id = f"{value_id}:component:{index}"
        parts = [
            _part(part_name, getattr(component, part_name), component_id)
            for part_name in ("error", "lower_error", "upper_error")
            if getattr(component, part_name) is not None
        ]
        components.append(
            ErrorComponent(
                name=component.name,
                error=component.error,
                lower_error=component.lower_error,
                upper_error=component.upper_error,
                direct_evidence=parts,
            )
        )
    return MeasuredValue(
        value_id=value_id,
        value=value.value,
        error=value.error,
        lower_error=value.lower_error,
        upper_error=value.upper_error,
        unit=value.unit,
        limit_kind=value.limit_kind,
        range_lower=value.range_lower,
        range_upper=value.range_upper,
        lower_inclusive=None,
        upper_inclusive=None,
        interval_kind=value.interval_kind,
        coverage=_probability(value.coverage, f"{evidence_id}:coverage"),
        lower_quantile=_quantile(value.lower_quantile, f"{evidence_id}:lower_quantile"),
        upper_quantile=_quantile(value.upper_quantile, f"{evidence_id}:upper_quantile"),
        error_components=components,
        condition=value.condition,
        frame=None,
        epoch=None,
        coordinate_format=value.coordinate_format,
        representation=value.representation,
        purpose=None,
        source=value.source,
        source_note=value.source_note,
        author_usage=None,
        direct_evidence=direct,
        context_evidence_refs=[],
    )


def _gold_value_entry(value: Any, *, record_id: str) -> GoldValueEntry:
    return GoldValueEntry(
        record_id=record_id,
        value=_measured_value(value, value_id=record_id),
        context_evidence=_evidence_texts([*value.evidence, *value.context_evidence]),
    )


def _boundness_status(boundness: Any) -> str | None:
    status = getattr(boundness, "value", None)
    if status is None:
        status = getattr(boundness, "status", None)
    return status


def _contribution_type(contribution: Any) -> str | None:
    kind = getattr(contribution, "contribution_type", None)
    if kind is None:
        return None
    return str(getattr(kind, "value", kind))


def _objects_from_contributions(contributions: list[Any]) -> list[GoldObjectEntry]:
    objects: list[GoldObjectEntry] = []
    for contribution in contributions:
        contribution_id = contribution.contribution_id
        groups: dict[str, list[GoldValueEntry]] = {}
        for group in contribution.quantities:
            entries = groups.setdefault(group.quantity, [])
            for index, item in enumerate(group.values):
                entries.append(
                    _gold_value_entry(
                        item, record_id=f"{contribution_id}:{group.quantity}:{index}"
                    )
                )
        for conflict in contribution.quantity_conflicts:
            entries = groups.setdefault(conflict.quantity, [])
            for index, alternative in enumerate(conflict.alternatives):
                entries.append(
                    _gold_value_entry(
                        alternative,
                        record_id=f"{contribution_id}:{conflict.conflict_id}:{index}",
                    )
                )
        objects.append(
            GoldObjectEntry(
                label=contribution_id,
                identifiers=[
                    identifier.value for identifier in contribution.identifiers
                ],
                contribution_type=_contribution_type(contribution),
                author_judgment=_boundness_status(contribution.paper_boundness),
                quantities=[
                    GoldQuantityEntry(quantity_path=path, values=values)
                    for path, values in groups.items()
                ],
            )
        )
    return objects


def _confirmed_for(
    paper_key: str, declared: dict[str, list[ConfirmedRange]] | None
) -> list[ConfirmedRange]:
    if not declared:
        return []
    return list(declared.get(paper_key, []))


def _partial_needs_ranges(
    paper_key: str, declared: dict[str, list[ConfirmedRange]] | None
) -> list[ConfirmedRange]:
    ranges = _confirmed_for(paper_key, declared)
    if not ranges:
        raise GoldAdaptationError(
            f"论文 {paper_key} 的 Gold 只声明部分覆盖，但没有显式给出已确认"
            "完整的封闭子范围；范围说明是自由文本，不能由适配程序推导，"
            "请由获准的评分准备方按 Gold 范围说明显式声明"
        )
    return ranges


@dataclass(frozen=True)
class GoldAdaptation:
    """一次 Gold 适配的结果：条目、绑定与就绪状态。

    - ``binding``：配对适配保留的可核对绑定（论文、规则、材料声明、
      ``answer_sha256``、覆盖/scope 审定）；answer-only 适配为 ``None``。
    - ``formal_readiness``：仅当绑定校验通过且无影响评分就绪的缺口时
      为 ``True``；answer-only 输入恒为 ``False``。
    - ``readiness_gaps``：影响评分就绪的具体缺口（未决科学待办等）；
      非空的 Gold 不能当作就绪评分输入（严格封装会拒绝）。
    - ``limitations``：就绪保证边界说明，不阻断结构映射。
    """

    paper_key: str
    entries: list[GoldPaperEntry]
    binding: dict | None = None
    formal_readiness: bool = False
    readiness_gaps: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


def _pair_binding(pair: CompactGoldPair) -> dict:
    metadata = pair.metadata
    revision = metadata.revision
    return {
        "paper_key": pair.answer.arxiv_id,
        "paper_version": metadata.paper.version,
        "schema": {
            "name": metadata.schema_tag.name,
            "version": metadata.schema_tag.version,
        },
        "rules": {"version": metadata.rules.version, "sha256": metadata.rules.sha256},
        "files": [
            {"file_id": item.file_id, "name": item.name, "sha256": item.sha256}
            for item in metadata.files
        ],
        "coverage": pair.answer.coverage.value,
        "scope_review": metadata.review.scope.value,
        "answer_sha256": metadata.review.answer_sha256,
        "revision": (
            {"revised_by": revision.revised_by, "revised_at": revision.revised_at}
            if revision is not None
            else None
        ),
    }


def _preparation_gaps(
    pair: CompactGoldPair,
    *,
    confirmed_ranges: dict[str, list[ConfirmedRange]] | None,
) -> list[str]:
    """把共用纯内存准备检查映射为就绪缺口说明（分类诊断）。

    复用 Gold 完整检查器中不读文件的同一判定（``pair_preparation_
    problems``，前置 ``validate_compact_gold_pair`` 绑定校验），不改
    变其类别/代码/主题/消息。``partial_coverage`` 按既定规则处理：
    仅当获准的评分准备方为该论文显式声明了已确认完整的封闭子范围时
    才不算缺口，且只解决覆盖声明本身——其余材料/科学缺口照实保留。
    纯内存核验不冒充真实 PDF/材料核对（``formal_readiness`` 的保证
    范围见 ``GoldAdaptation``）。
    """
    ranges = _confirmed_for(pair.answer.arxiv_id, confirmed_ranges)
    gaps: list[str] = []
    for problem in pair_preparation_problems(pair):
        if problem.code == "partial_coverage" and ranges:
            continue
        label = (
            "科学待决缺口" if problem.category == "science_pending" else "材料准备缺口"
        )
        gaps.append(
            f"{label} {problem.category}/{problem.code}（{problem.subject}）："
            f"{problem.message}"
        )
    return gaps


def _answer_only_gaps(answer: CompactGoldAnswer) -> list[str]:
    """answer-only 可判定的未决科学待办；材料核对缺口由绑定缺失表达。"""
    return [
        f"科学待决缺口 science_pending/unresolved_{index + 1}"
        f"（{item.subject}）：{item.value}"
        for index, item in enumerate(answer.unresolved)
    ]


def _entries_from_answer(
    answer: CompactGoldAnswer,
    *,
    confirmed_ranges: dict[str, list[ConfirmedRange]] | None,
) -> list[GoldPaperEntry]:
    coverage = answer.coverage.value
    return [
        GoldPaperEntry(
            paper_key=answer.arxiv_id,
            coverage=coverage,
            confirmed_ranges=(
                _partial_needs_ranges(answer.arxiv_id, confirmed_ranges)
                if coverage == "partial"
                else []
            ),
            objects=_objects_from_contributions(answer.contributions),
        )
    ]


def adapt_compact_gold_pair(
    pair: CompactGoldPair,
    *,
    confirmed_ranges: dict[str, list[ConfirmedRange]] | None = None,
) -> GoldAdaptation:
    """compact v2 正式答案配对 → 评分 Gold 输入与绑定（纯内存）。

    先执行内存可完成的既有绑定校验（论文、scope、``answer_sha256``、
    证据文件声明），失败即拒绝适配；绑定保留在 ``binding`` 供报告与
    计划核对。再复用 Gold 完整检查器中不读文件的同一准备检查
    （``pair_preparation_problems``）：材料缺口（缺源对应、缺证据、
    缺条件）与科学待决（未决待办、未结构化冲突、在案材料差异）都进
    入 ``readiness_gaps`` 并阻止 ``formal_readiness``——完整覆盖声明
    不掩盖它们。已审定且结构完整的冲突双方不是待办，正常映射；部分
    覆盖按既定规则需显式封闭子范围。本函数不读取真实 PDF、不宣称为
    真实材料或科学审定完成。
    """
    try:
        validate_compact_gold_pair(pair)
    except GoldIntegrityError as exc:
        raise GoldAdaptationError(
            f"compact Gold 配对绑定校验失败，不能适配：{exc}"
        ) from exc
    gaps = _preparation_gaps(pair, confirmed_ranges=confirmed_ranges)
    return GoldAdaptation(
        paper_key=pair.answer.arxiv_id,
        entries=_entries_from_answer(pair.answer, confirmed_ranges=confirmed_ranges),
        binding=_pair_binding(pair),
        formal_readiness=not gaps,
        readiness_gaps=gaps,
    )


def adapt_compact_answer(
    answer: CompactGoldAnswer,
    *,
    confirmed_ranges: dict[str, list[ConfirmedRange]] | None = None,
) -> GoldAdaptation:
    """compact v2 单份答案（answer-only）→ 评分 Gold 输入。

    缺 metadata：不执行 ``answer_sha256``/范围审定/材料声明核对，不构
    成正式就绪（``formal_readiness`` 恒为 ``False``，边界见
    ``limitations``）。适用于调用方已在内存中另行完成核对的上下文；
    未决科学待办仍计为准备缺口。
    """
    gaps = _answer_only_gaps(answer)
    return GoldAdaptation(
        paper_key=answer.arxiv_id,
        entries=_entries_from_answer(answer, confirmed_ranges=confirmed_ranges),
        binding=None,
        formal_readiness=False,
        readiness_gaps=gaps,
        limitations=[ANSWER_ONLY_LIMITATION],
    )


def adapt_annotation(
    annotation: GoldAnnotation,
    *,
    confirmed_ranges: dict[str, list[ConfirmedRange]] | None = None,
) -> GoldAdaptation:
    """v1 标注对象 → 评分 Gold 输入（纯内存，自包含绑定）。

    v1 格式没有独立覆盖声明：仅接受无待办的正式答案并按完整覆盖映
    射；草稿或带待办的标注存在准备缺口，明确报错而不是默认完整。
    绑定取自标注自身携带的模式版本、规则与材料声明。
    """
    if annotation.kind != "answer":
        raise GoldAdaptationError(
            f"文档 {annotation.document_id} 是草稿（kind=draft），不能当作"
            "评分 Gold；请先完成审定与保存"
        )
    if annotation.pending:
        raise GoldAdaptationError(
            f"文档 {annotation.document_id} 带有 {len(annotation.pending)} 项"
            "未决待办，覆盖完整性未知，不能按完整 Gold 评分"
        )
    paper_key = annotation.paper.arxiv_id
    if not paper_key:
        raise GoldAdaptationError(
            f"文档 {annotation.document_id} 缺少 arXiv 编号，无法作为评分论文标识"
        )
    binding = {
        "paper_key": paper_key,
        "document_id": annotation.document_id,
        "schema": {
            "name": annotation.schema_tag.name,
            "version": annotation.schema_tag.version,
        },
        "rules": {
            "version": annotation.rules.version,
            "sha256": annotation.rules.sha256,
        },
        "files": [
            {"file_id": item.file_id, "name": item.name, "sha256": item.sha256}
            for item in annotation.files
        ],
        "status": annotation.status,
    }
    return GoldAdaptation(
        paper_key=paper_key,
        entries=[
            GoldPaperEntry(
                paper_key=paper_key,
                coverage="complete",
                confirmed_ranges=[],
                objects=_objects_from_contributions(annotation.contributions),
            )
        ],
        binding=binding,
        formal_readiness=True,
        readiness_gaps=[],
    )


def _require_ready(adaptation: GoldAdaptation) -> list[GoldPaperEntry]:
    """严格就绪门禁：绑定核对与准备状态缺一不可（程序保证）。"""
    if adaptation.binding is None:
        raise GoldAdaptationError(
            "Gold 输入缺少可核对的绑定（answer-only 未执行 metadata 绑定"
            "核对），不能作为就绪评分输入；如需非正式结构映射请使用 "
            "adapt_compact_answer（返回 binding=None、formal_readiness="
            "False 与 limitations 的诊断结构），正式评分请使用经检查的"
            "完整配对 gold_entries_from_compact_pair"
        )
    if adaptation.readiness_gaps or not adaptation.formal_readiness:
        raise GoldAdaptationError(
            "Gold 存在准备缺口，不能作为就绪评分输入："
            + "；".join(adaptation.readiness_gaps)
            + ("；就绪状态为 False" if not adaptation.readiness_gaps else "")
        )
    return adaptation.entries


def gold_entries_from_compact_pair(
    pair: CompactGoldPair,
    *,
    confirmed_ranges: dict[str, list[ConfirmedRange]] | None = None,
) -> list[GoldPaperEntry]:
    """compact v2 正式答案对 → 就绪评分 Gold 输入（严格封装）。

    先做配对绑定校验；未决科学待办等准备缺口拒绝映射，不能变 ready。
    """
    return _require_ready(
        adapt_compact_gold_pair(pair, confirmed_ranges=confirmed_ranges)
    )


def gold_entries_from_compact_answer(
    answer: CompactGoldAnswer,
    *,
    confirmed_ranges: dict[str, list[ConfirmedRange]] | None = None,
) -> list[GoldPaperEntry]:
    """compact v2 单份答案不能作为就绪评分输入（严格入口拒绝）。

    answer-only 输入缺 metadata：无法执行 ``answer_sha256``/范围审定/
    材料声明的绑定核对，即使结构可映射也不是正式就绪评分输入（已
    裁定）。本严格入口程序化拒绝，不做警告式放行；需要结构
    映射做诊断时使用 :func:`adapt_compact_answer`（返回 ``binding=
    None``、``formal_readiness=False`` 与 ``limitations`` 的非正式结
    构）。参数 ``confirmed_ranges`` 仅为兼容旧签名保留，不改变拒绝。
    """
    del confirmed_ranges
    raise GoldAdaptationError(
        "answer-only 输入缺少 metadata 绑定核对，严格入口拒绝返回可当"
        " ready 的裸条目；请改用 adapt_compact_answer 获取带 "
        "binding=None/formal_readiness=False/limitations 的非正式结构，"
        "或使用经检查的完整配对 gold_entries_from_compact_pair"
    )


def gold_entries_from_annotation(
    annotation: GoldAnnotation,
    *,
    confirmed_ranges: dict[str, list[ConfirmedRange]] | None = None,
) -> list[GoldPaperEntry]:
    """v1 标注答案 → 就绪评分 Gold 输入（严格封装）。

    绑定取自标注自身（schema/规则/材料声明），经同一就绪门禁；合法
    已绑定的正式答案不受影响，草稿与带待办标注在适配层已拒绝。
    """
    del confirmed_ranges  # v1 按完整覆盖映射，无部分覆盖声明
    return _require_ready(adapt_annotation(annotation))


# ---- 提取输出 → 评分预测 ----


def science_completion(status: dict, roster, paper) -> tuple[str, list[str]]:
    """科学完成状态（与运行图终止状态分开）。

    取舍顺序：仍有开放或已答仍未决的科学问题、名单未决条目、未列清
    的原文范围（``unresolved_groups``，按既定语义保留未决）或未决作
    者总体判断时为 ``pending``（另保留错误/失败等独立诊断）；无待决
    但存在运行错误、未到图终止、数值未产出、名单覆盖不完整/带材料缺
    口（``coverage.state`` 非 complete 或 ``gap_refs`` 非空）、作者总
    体判断仅因材料缺口未完成（``status=None`` 且只有 ``gap_refs``）或
    物理量覆盖未完成时为 ``partial``；真正完整且无待决才是
    ``complete``。没有任何有效输出时，失败为 ``failed``、其余为
    ``unfinished``。排除条目不算未完成；已 resolved 的问题不阻止
    complete。参数接受只读视图或结构相同的简单对象（duck typing），
    不修改它们。
    """
    reasons: list[str] = []
    questions = status.get("questions", [])
    open_count = sum(1 for item in questions if item.get("status") == "open")
    unresolved_answered = sum(
        1
        for item in questions
        if item.get("status") == "answered"
        and item.get("answered_outcome") == "unresolved"
    )
    pending_items = []
    unresolved_groups = []
    coverage = None
    if roster is not None:
        pending_items = [
            entry
            for entry in roster.data.entries
            if getattr(entry, "disposition", None) == "pending"
        ]
        unresolved_groups = list(getattr(roster.data, "unresolved_groups", None) or [])
        coverage = getattr(roster.data, "coverage", None)
    boundness_pending = False
    boundness_gap_only = False
    if paper is not None:
        for contribution in paper.data.object_contributions:
            boundness = contribution.paper_boundness
            if boundness.status is None:
                if boundness.issue_refs:
                    boundness_pending = True
                elif getattr(boundness, "gap_refs", None):
                    boundness_gap_only = True
    if (
        open_count
        or unresolved_answered
        or pending_items
        or unresolved_groups
        or boundness_pending
    ):
        if open_count:
            reasons.append(f"开放科学问题 {open_count} 项")
        if unresolved_answered:
            reasons.append(f"已答复仍未决的科学问题 {unresolved_answered} 项")
        if pending_items:
            reasons.append(
                "名单未决条目 " + ", ".join(entry.item_id for entry in pending_items)
            )
        if unresolved_groups:
            reasons.append(
                "存在未列清的原文范围 "
                + ", ".join(
                    getattr(group, "group_id", str(index))
                    for index, group in enumerate(unresolved_groups)
                )
            )
        if boundness_pending:
            reasons.append("存在未决的作者总体判断")
        return "pending", reasons
    has_output = paper is not None or roster is not None
    if not has_output:
        return ("failed" if status.get("state") == "failed" else "unfinished"), reasons
    if status.get("state") != "finished":
        # 仍处失败/未终止：错误是现存事实；已恢复到 finished 的历史错误
        # 保留在报告 failures 诊断里，但不再阻止科学 complete（既定裁
        # 定），完整性由当前有效结果、问题状态与覆盖检查证明
        if status.get("errors"):
            reasons.append(f"运行错误 {len(status['errors'])} 项")
        reasons.append(f"运行图状态为 {status.get('state')!r}，未到终止")
    if coverage is not None:
        state = getattr(coverage, "state", None)
        if state != "complete":
            reasons.append(f"名单覆盖状态为 {state!r}，材料范围未全部检查")
        gap_refs = getattr(coverage, "gap_refs", None) or []
        if gap_refs:
            reasons.append(f"名单覆盖存在未处理材料缺口 {len(gap_refs)} 项")
    if boundness_gap_only:
        reasons.append("作者总体判断因材料缺口未完成（无已决科学问题）")
    if paper is None:
        reasons.append("已保存名单但数值结果未产出")
    else:
        for contribution in paper.data.object_contributions:
            for item in contribution.quantity_coverage:
                if item.state != "complete":
                    reasons.append(f"物理量 {item.quantity} 完成状态为 {item.state}")
    if reasons:
        return "partial", reasons
    return "complete", []


def _l0_from_status(status: dict, roster, paper) -> dict:
    """评分输入的 L0 映射：交付按实际有效记录，完成按科学状态。

    已交付且科学状态非 complete 时用 ``partial``——评分输入模型
    （已审）不允许已交付论文标 pending，"部分结果"是如实下界；待决
    事实由 ``science_completion`` 在报告层单独表达。后续失败不抹已存
    名单；真正无有效结果的失败仍 failed，不冒充空名单成功。
    """
    errors = list(status["errors"])
    has_output = paper is not None or roster is not None
    science_state, _ = science_completion(status, roster, paper)
    if not has_output:
        return {
            "delivered": False,
            "format_valid": False,
            "completion": ("failed" if status["state"] == "failed" else "unfinished"),
            "errors": errors,
        }
    return {
        "delivered": True,
        "format_valid": True,
        "completion": "complete" if science_state == "complete" else "partial",
        "errors": errors,
    }


def _objects_from_paper(paper: PaperRecord) -> list[PredictionObjectEntry]:
    objects: list[PredictionObjectEntry] = []
    for contribution in paper.data.object_contributions:
        quantities = [
            PredictionQuantityEntry(
                quantity_path=group.quantity,
                values=[
                    PredictionValueEntry(record_id=value.value_id, value=value)
                    for value in group.values
                ],
            )
            for group in contribution.quantities
        ]
        objects.append(
            PredictionObjectEntry(
                paper_key=paper.data.paper_key,
                label=contribution.item_id,
                identifiers=[
                    identifier.value for identifier in contribution.identifiers
                ],
                contribution_type=contribution.contribution_type.value,
                author_judgment=contribution.paper_boundness.status,
                quantities=quantities,
            )
        )
    return objects


def _objects_from_roster(roster: RosterRecord) -> list[PredictionObjectEntry]:
    """尚无论文记录时，用名单的收录条目作为对象层预测（无数值）。"""
    return [
        PredictionObjectEntry(
            paper_key=roster.data.paper_key,
            label=entry.item_id,
            identifiers=[identifier.value for identifier in entry.identifiers],
            contribution_type=entry.contribution_id,
            author_judgment=None,
            quantities=[],
        )
        for entry in roster.data.entries
        if entry.disposition == "included"
    ]


def prediction_from_execution(
    space: Workspace, execution_id: str
) -> PredictionPaperEntry:
    """一次共享提取执行 → 评分预测输入（L0 状态来自运行清单）。

    数值直接复用记录中的 ``MeasuredValue``；名单中的未决与排除条目
    不进入预测对象（未决不是多报，排除不是遗漏）。
    """
    status = get_extraction(space, execution_id)
    paper: PaperRecord | None = None
    roster: RosterRecord | None = None
    for ref in execution_result_refs(space, execution_id):
        record = space.store().read_record_current(ref.record_id)
        if isinstance(record, PaperRecord) and paper is None:
            paper = record
        elif isinstance(record, RosterRecord) and roster is None:
            roster = record
    l0 = _l0_from_status(status, roster, paper)
    paper_key = _paper_key_from(space, execution_id, paper, roster)
    if not l0["delivered"] or not l0["format_valid"]:
        return PredictionPaperEntry(
            paper_key=paper_key,
            delivered=False,
            format_valid=False,
            completion=l0["completion"],
            errors=l0["errors"],
            objects=[],
        )
    objects = (
        _objects_from_paper(paper)
        if paper is not None
        else (_objects_from_roster(roster) if roster is not None else [])
    )
    return PredictionPaperEntry(
        paper_key=paper_key,
        delivered=True,
        format_valid=True,
        completion=l0["completion"],
        errors=l0["errors"],
        objects=objects,
    )


def _paper_key_from(
    space: Workspace,
    execution_id: str,
    paper: PaperRecord | None,
    roster: RosterRecord | None,
) -> str:
    if paper is not None:
        return paper.data.paper_key
    if roster is not None:
        return roster.data.paper_key
    # 没有任何输出记录时，从执行固定的来源包读取论文标识，保住分母
    handle = runtime_get_execution(space, execution_id)
    for ref in handle.input_snapshot.source_refs:
        record = space.store().read_record(ref)
        if isinstance(record, SourcePackageRecord):
            return record.data.paper_key
    raise GoldAdaptationError(
        f"执行 {execution_id} 没有任何可定位论文的名单或论文记录，无法确定"
        "预测的论文标识"
    )
