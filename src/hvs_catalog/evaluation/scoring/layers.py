"""分层匹配引擎：L1 对象一一对应与 L2 科学归属/数值配对。

L1 只用本篇标识/明确别名做受限整理后的一一对应；同一标识的重复预测
消耗一次 TP、其余 FP；两个 Gold 共用别名的真实身份歧义保留未决关系
（``pending``），与确定的 FP/FN 分开，不凭数值消除，也不默认为错误。

L2 在每个（对象, 物理量）集合内构建完整关系图：先对每个 (Gold,
预测) 对确认科学归属（quantity_path、condition、source/source_note、
frame、epoch、coordinate_format、representation、purpose、
author_usage）——明确 ``different`` 是禁止边，明确 ``equivalent``
才允许比较内容，未知归属（含 Agent 材料不足/技术失败）保留为可能
边，绝不因数值相同跨条件认定正确。归属等价的边再按域（数值/单位）
比较内容；内容键仅用于确认安全的等价，键不同仍走按域三态，未支持
的表达是未知而不是证明不同。确定边（归属等价+内容等价）的最大一
对一匹配为 C；含未知边的上界判断 C 是否不确定；归属未知边若可能
改变对应关系同样保持未决。排序或重命名内部 ID 不改变 C/P/G，不把
null 单位当通配，具名误差分量按名称对应且不能消掉缺失分量。

只有程序 ``undecidable`` 的排版或条件关系才交给注入的辅助解析器，
且双方必须提供绑定材料、声明支持当前待判字段的原文片段；程序或
Agent 已确定的不同不能被推翻，转为确定的不匹配诊断。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from hvs_catalog.records.paper import MeasuredValue

from .agent import ScoringAgentResolver
from .models import EvidenceFragment, GoldObjectEntry, PredictionObjectEntry
from .printed import (
    Verdict,
    compare_condition,
    compare_number,
    compare_unit,
    is_supported_number_syntax,
    normalize_printed,
)
from .results import PairingDetail

ATTRIBUTION_FIELDS = (
    "source",
    "source_note",
    "frame",
    "epoch",
    "coordinate_format",
    "representation",
    "purpose",
    "author_usage",
)
NUMBER_CONTENT_FIELDS = (
    "value",
    "error",
    "lower_error",
    "upper_error",
    "range_lower",
    "range_upper",
)
UNIT_CONTENT_FIELDS = ("unit",)
REPORT_CONTENT_FIELDS = ("coverage", "lower_quantile", "upper_quantile")
COMPONENT_FIELDS = ("error", "lower_error", "upper_error")


def normalize_identifier(text: str) -> str:
    """标识的受限整理：NFC、Unicode 负号、空白折叠；不做其他归并。"""
    return " ".join(unicodedata.normalize("NFC", text).replace("−", "-").split())


def _attribution_summary(value: MeasuredValue) -> str:
    parts = [f"condition={value.condition}", f"source={value.source}"]
    for name in ATTRIBUTION_FIELDS:
        if name == "source":
            continue
        raw = getattr(value, name)
        if raw is not None:
            parts.append(f"{name}={raw}")
    return "; ".join(parts)


def compare_attribution(
    gold: MeasuredValue, prediction: MeasuredValue
) -> tuple[Verdict, list[str]]:
    """归属比较：非条件字段精确一致，条件走受限短语比较。"""
    mismatches = [
        name
        for name in ATTRIBUTION_FIELDS
        if getattr(gold, name) != getattr(prediction, name)
    ]
    if mismatches:
        return "different", mismatches
    verdict = compare_condition(gold.condition, prediction.condition)
    return verdict, (["condition"] if verdict == "different" else [])


def content_key(value: MeasuredValue) -> tuple | None:
    """内容键：受限排版整理且语法完整的完整科学字段；否则为 None。

    键相等蕴涵安全等价（同打印且语法完整）；任何数值字段含未知宏或
    未支持/畸形语法时返回 None——不得用键捷径把畸形语法确认为安全
    等价；键不同也不证明不同，由 ``compare_content`` 按域判定。
    """

    def _normalized_number(raw: str) -> str | None:
        normalized = normalize_printed(raw)
        if normalized is None or not is_supported_number_syntax(normalized):
            return None
        return normalized

    parts: list = []
    for name in (*NUMBER_CONTENT_FIELDS, *UNIT_CONTENT_FIELDS):
        raw = getattr(value, name)
        if raw is None:
            parts.append(None)
            continue
        if name in UNIT_CONTENT_FIELDS:
            normalized = normalize_printed(raw)
        else:
            normalized = _normalized_number(raw)
        if normalized is None:
            return None
        parts.append(normalized)
    parts.extend(
        [
            value.limit_kind,
            value.interval_kind,
            value.lower_inclusive,
            value.upper_inclusive,
        ]
    )
    for report_field in REPORT_CONTENT_FIELDS:
        report = getattr(value, report_field)
        if report is None:
            parts.append(None)
            continue
        value_norm = _normalized_number(report.value)
        unit_norm = normalize_printed(report.unit) if report.unit is not None else None
        if value_norm is None or (report.unit is not None and unit_norm is None):
            return None
        parts.append((value_norm, unit_norm))
    components = []
    for component in sorted(value.error_components, key=lambda item: item.name):
        entry: list = [
            component.name,
            component.error is not None,
            component.lower_error is not None,
            component.upper_error is not None,
        ]
        for name in COMPONENT_FIELDS:
            raw = getattr(component, name)
            if raw is None:
                entry.append(None)
                continue
            normalized = _normalized_number(raw)
            if normalized is None:
                return None
            entry.append(normalized)
        components.append(tuple(entry))
    parts.append(tuple(components))
    return tuple(parts)


def _compare_number_field(
    field_name: str, left: str, right: str, diff: list[str], undecided: list[str]
) -> None:
    verdict = compare_number(left, right)
    if verdict == "different":
        diff.append(field_name)
    elif verdict == "undecidable":
        undecided.append(field_name)


def _compare_unit_field(
    field_name: str, left: str, right: str, diff: list[str], undecided: list[str]
) -> None:
    verdict = compare_unit(left, right)
    if verdict == "different":
        diff.append(field_name)
    elif verdict == "undecidable":
        undecided.append(field_name)


def _compare_components(
    gold: MeasuredValue, prediction: MeasuredValue
) -> tuple[list[str], list[str]]:
    gold_names = {component.name for component in gold.error_components}
    pred_names = {component.name for component in prediction.error_components}
    if gold_names != pred_names:
        missing = sorted(gold_names - pred_names)
        extra = sorted(pred_names - gold_names)
        return [f"error_components(缺 {missing}，多 {extra})"], []
    diff: list[str] = []
    undecided: list[str] = []
    pred_by_name = {c.name: c for c in prediction.error_components}
    for component in gold.error_components:
        other = pred_by_name[component.name]
        for name in COMPONENT_FIELDS:
            left = getattr(component, name)
            right = getattr(other, name)
            if (left is None) != (right is None):
                diff.append(f"error_components.{component.name}.{name}")
                continue
            if left is None:
                continue
            _compare_number_field(
                f"error_components.{component.name}.{name}",
                left,
                right,
                diff,
                undecided,
            )
    return diff, undecided


def _compare_reports(
    gold: MeasuredValue, prediction: MeasuredValue
) -> tuple[list[str], list[str]]:
    diff: list[str] = []
    undecided: list[str] = []
    for report_field in REPORT_CONTENT_FIELDS:
        left = getattr(gold, report_field)
        right = getattr(prediction, report_field)
        if (left is None) != (right is None):
            diff.append(report_field)
            continue
        if left is None:
            continue
        _compare_number_field(
            f"{report_field}.value", left.value, right.value, diff, undecided
        )
        if (left.unit is None) != (right.unit is None):
            diff.append(f"{report_field}.unit")
        elif left.unit is not None:
            _compare_unit_field(
                f"{report_field}.unit", left.unit, right.unit, diff, undecided
            )
    return diff, undecided


def compare_content(
    gold: MeasuredValue, prediction: MeasuredValue
) -> tuple[Verdict, list[str], list[str]]:
    """内容比较：按域判定，返回判定、确定不同字段与无法判定字段。"""
    diff: list[str] = []
    undecided: list[str] = []
    for name in NUMBER_CONTENT_FIELDS:
        left = getattr(gold, name)
        right = getattr(prediction, name)
        if (left is None) != (right is None):
            diff.append(name)
            continue
        if left is None:
            continue
        _compare_number_field(name, left, right, diff, undecided)
    for name in UNIT_CONTENT_FIELDS:
        left = getattr(gold, name)
        right = getattr(prediction, name)
        if (left is None) != (right is None):
            diff.append(name)
            continue
        if left is None:
            continue
        _compare_unit_field(name, left, right, diff, undecided)
    for name in ("limit_kind", "interval_kind", "lower_inclusive", "upper_inclusive"):
        if getattr(gold, name) != getattr(prediction, name):
            diff.append(name)
    report_diff, report_undecided = _compare_reports(gold, prediction)
    diff.extend(report_diff)
    undecided.extend(report_undecided)
    component_diff, component_undecided = _compare_components(gold, prediction)
    diff.extend(component_diff)
    undecided.extend(component_undecided)
    if diff:
        return "different", diff, undecided
    if undecided:
        return "undecidable", [], undecided
    return "equivalent", [], []


@dataclass
class ValueSide:
    owner: str
    quantity_path: str
    record_id: str
    measured: MeasuredValue
    fragments: list[EvidenceFragment] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.owner}|{self.quantity_path}|{self.record_id}"


@dataclass
class QuantityPairing:
    pairings: list[PairingDetail] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    matched: int = 0
    predicted: int = 0
    gold: int = 0
    reported_mismatch: set[str] = field(default_factory=set)
    reported_unresolved: set[str] = field(default_factory=set)
    matched_gold_refs: set[str] = field(default_factory=set)


def _max_matching(
    edges: set[tuple[int, int]], gold_count: int, pred_count: int
) -> list[tuple[int, int]]:
    """确定性最大一对一匹配（Kuhn 增广路径；结果大小与输入顺序无关）。"""

    adjacency = [[] for _ in range(gold_count)]
    for gold_index, pred_index in edges:
        adjacency[gold_index].append(pred_index)
    for neighbors in adjacency:
        neighbors.sort()
    match_of_pred = [-1] * pred_count

    def try_augment(gold_index: int, seen: set[int]) -> bool:
        for pred_index in adjacency[gold_index]:
            if pred_index in seen:
                continue
            seen.add(pred_index)
            if match_of_pred[pred_index] == -1 or try_augment(
                match_of_pred[pred_index], seen
            ):
                match_of_pred[pred_index] = gold_index
                return True
        return False

    for gold_index in range(gold_count):
        try_augment(gold_index, set())
    return [
        (gold_index, pred_index)
        for pred_index, gold_index in enumerate(match_of_pred)
        if gold_index != -1
    ]


def _field_pair(
    gold_side: ValueSide, pred_side: ValueSide, field_name: str
) -> tuple[str | None, str | None]:
    """按未决字段路径提取真正待判的两侧原报字符串。

    - 普通字段（value/error/unit/...）直接取字段；
    - 报告字段（coverage/lower_quantile/upper_quantile）的 ``.value``
      取报告值、``.unit`` 取报告单位，不以覆盖值替代待判单位；
    - 具名误差分量（``error_components.<名称>.<子字段>``）按名称在
      各自侧取该分量的真正待判子字段。
    """
    parts = field_name.split(".")
    base = parts[0]
    if base in {"coverage", "lower_quantile", "upper_quantile"}:
        sub = parts[-1] if len(parts) > 1 else "value"
        left = _report_fragment(gold_side.measured, field_name, sub)
        right = _report_fragment(pred_side.measured, field_name, sub)
        return (left or None, right or None)
    if base == "error_components" and len(parts) == 3:
        component_name, sub = parts[1], parts[2]

        def _component_text(value: MeasuredValue) -> str | None:
            for component in value.error_components:
                if component.name == component_name:
                    raw = getattr(component, sub)
                    return None if raw is None else str(raw)
            return None

        return (
            _component_text(gold_side.measured),
            _component_text(pred_side.measured),
        )
    left = getattr(gold_side.measured, base)
    right = getattr(pred_side.measured, base)
    return (
        None if left is None else str(left),
        None if right is None else str(right),
    )


def _resolve_candidate(
    gold_side: ValueSide,
    pred_side: ValueSide,
    undecided_fields: list[str],
    resolver: ScoringAgentResolver | None,
    reasons: list[str],
) -> tuple[Verdict, list[str]]:
    """多字段未决的合并判定：任一必要组件明确 different 优先。

    逐字段收集确定不同/未知/技术失败，不因首个缺证据或失败提前返回；
    出现明确 different 即确定整条不匹配并停止无必要的后续调用。技术
    失败事实保留在冻结库与辅助摘要，仅在无确定不同时才注入未决原因。
    """
    if resolver is None:
        return "undecidable", []
    equivalent = True
    determined_diff: list[str] = []
    technical_failures: list[str] = []
    for field_name in undecided_fields:
        left, right = _field_pair(gold_side, pred_side, field_name)
        if left is None or right is None:
            equivalent = False
            continue
        resolution = resolver.resolve_printed(
            field_name=field_name,
            left=left,
            right=right,
            left_fragments=gold_side.fragments,
            right_fragments=pred_side.fragments,
            left_attribution=_attribution_summary(gold_side.measured),
            right_attribution=_attribution_summary(pred_side.measured),
        )
        if resolution.status == "insufficient_evidence":
            equivalent = False
            continue
        if resolution.verdict == "different":
            determined_diff.append(field_name)
            break  # 已可确定整条不匹配，停止无必要调用
        if resolution.verdict is None:
            technical_failures.append(resolution.entry_key)
            equivalent = False
            continue
        if resolution.verdict == "undecidable":
            equivalent = False
    if determined_diff:
        return "different", determined_diff
    if technical_failures:
        reasons.extend(f"agent_technical_failure:{key}" for key in technical_failures)
    return ("equivalent", []) if equivalent else ("undecidable", [])


def _report_fragment(value: MeasuredValue, field_name: str, part: str) -> str:
    report_field = field_name.split(".")[0]
    report = getattr(value, report_field)
    if report is None:
        return ""
    if part == "unit":
        return report.unit or ""
    return report.value


def pair_quantity_values(
    gold_sides: list[ValueSide],
    pred_sides: list[ValueSide],
    *,
    resolver: ScoringAgentResolver | None,
) -> QuantityPairing:
    """一个（对象, 物理量）集合内的完整关系图与确定性最大一对一匹配。

    根因式流程（不逐预测先选桶）：

    1. 先确认科学归属：对每个 (gold, 预测) 对做归属比较——明确
       ``different`` 是禁止边；明确 ``equivalent`` 才允许比较内容；
       未知归属（含 Agent 材料不足/技术失败）保留为可能边，绝不因
       数值相同跨条件认定正确（数值相同不是条件等价证据）。
    2. 只对归属已确认等价的边比较内容；内容键仅用于确认安全的等价
       （键相等蕴涵等价），键不同仍走按域三态判定——未支持的数值或
       单位表达是未知，不是证明不同。
    3. 确定边（归属等价+内容等价）的最大匹配为 C；含未知边的最大
       匹配上界判断 C 是否不确定；归属未知边若可能改变对应关系
       （存在于某个最大匹配中）同样保持未决。
    """
    result = QuantityPairing(predicted=len(pred_sides), gold=len(gold_sides))
    if not gold_sides and not pred_sides:
        return result
    golds = sorted(gold_sides, key=lambda side: side.ref)
    preds = sorted(pred_sides, key=lambda side: side.ref)
    gold_count, pred_count = len(golds), len(preds)
    # 技术失败事实延迟到未决判定后再决定是否注入原因：不影响最终匹配
    # 的失败保留在冻结库与辅助摘要，不据此把整批 provisional
    deferred_failures: list[str] = []

    attr_equivalent: set[tuple[int, int]] = set()
    attr_forbidden: dict[tuple[int, int], list[str]] = {}
    attr_unknown: set[tuple[int, int]] = set()
    for gold_index, gold_side in enumerate(golds):
        for pred_index, pred_side in enumerate(preds):
            verdict, fields = compare_attribution(
                gold_side.measured, pred_side.measured
            )
            if verdict == "equivalent":
                attr_equivalent.add((gold_index, pred_index))
            elif verdict == "different":
                attr_forbidden[(gold_index, pred_index)] = fields
            elif resolver is not None:
                resolution = resolver.resolve_condition(
                    left=gold_side.measured.condition,
                    right=pred_side.measured.condition,
                    left_fragments=gold_side.fragments,
                    right_fragments=pred_side.fragments,
                )
                if resolution.status == "insufficient_evidence":
                    attr_unknown.add((gold_index, pred_index))
                elif resolution.verdict == "equivalent":
                    attr_equivalent.add((gold_index, pred_index))
                elif resolution.verdict == "different":
                    attr_forbidden[(gold_index, pred_index)] = ["condition"]
                else:
                    attr_unknown.add((gold_index, pred_index))
                    deferred_failures.append(
                        f"agent_technical_failure:{resolution.entry_key}"
                    )
            else:
                attr_unknown.add((gold_index, pred_index))

    determined: set[tuple[int, int]] = set()
    content_mismatch: dict[tuple[int, int], list[str]] = {}
    content_unknown: dict[tuple[int, int], list[str]] = {}
    for gold_index, pred_index in sorted(attr_equivalent):
        gold_side, pred_side = golds[gold_index], preds[pred_index]
        gold_key = content_key(gold_side.measured)
        pred_key = content_key(pred_side.measured)
        if gold_key is not None and pred_key is not None and gold_key == pred_key:
            # 键相等只能确认安全等价，不用于证明不同
            determined.add((gold_index, pred_index))
            continue
        verdict, diff, undecided = compare_content(
            gold_side.measured, pred_side.measured
        )
        if verdict == "equivalent":
            determined.add((gold_index, pred_index))
            continue
        if verdict == "different":
            content_mismatch[(gold_index, pred_index)] = diff
            continue
        scratch_reasons: list[str] = []
        resolved_verdict, resolved_diff = _resolve_candidate(
            gold_side,
            pred_side,
            undecided,
            resolver,
            scratch_reasons,
        )
        deferred_failures.extend(scratch_reasons)
        if resolved_verdict == "equivalent":
            determined.add((gold_index, pred_index))
        elif resolved_verdict == "different":
            content_mismatch[(gold_index, pred_index)] = resolved_diff
        else:
            # 材料不足/技术失败/拒答：未知关系保留，不因存在适配器而变确定
            content_unknown[(gold_index, pred_index)] = undecided

    matched_pairs = _max_matching(determined, gold_count, pred_count)
    matched_gold_indices = {gold_index for gold_index, _ in matched_pairs}
    matched_pred_indices = {pred_index for _, pred_index in matched_pairs}
    for gold_index, pred_index in matched_pairs:
        result.matched += 1
        result.matched_gold_refs.add(golds[gold_index].ref)
        result.pairings.append(
            PairingDetail(
                layer="l2",
                status="matched",
                gold_ref=golds[gold_index].ref,
                prediction_ref=preds[pred_index].ref,
            )
        )

    possible_edges = set(content_unknown) | set(attr_unknown)
    upper_pairs = _max_matching(determined | possible_edges, gold_count, pred_count)
    score_uncertain = len(upper_pairs) > len(matched_pairs)
    correspondence_uncertain = False
    if not score_uncertain:
        # 归属未知边若能出现在某个最大匹配中，就可能改变"哪个 Gold 与
        # 哪个预测对应"的对应关系（即使 C 不变），保持未决
        for gold_index, pred_index in sorted(attr_unknown):
            reduced = {
                edge
                for edge in (determined | possible_edges)
                if edge[0] != gold_index and edge[1] != pred_index
            }
            reduced_size = len(_max_matching(reduced, gold_count, pred_count))
            if reduced_size >= len(upper_pairs) - 1:
                correspondence_uncertain = True
                break
    unresolved = score_uncertain or correspondence_uncertain
    if unresolved:
        # 只有确实影响得分/对应时，技术失败才作为未决原因注入
        result.reasons.extend(deferred_failures)

    if unresolved:
        for pred_index in sorted({index for _, index in attr_unknown}):
            candidate_refs = sorted(
                golds[gold_index].ref
                for gold_index, index in attr_unknown
                if index == pred_index
            )
            result.reported_unresolved.update(candidate_refs)
            result.pairings.append(
                PairingDetail(
                    layer="l2",
                    status="unresolved_attribution",
                    gold_ref=candidate_refs[0],
                    prediction_ref=preds[pred_index].ref,
                    fields=["condition"],
                )
            )
            result.reasons.append(
                "unresolved_attribution:"
                + preds[pred_index].ref
                + " vs "
                + ",".join(candidate_refs)
            )
        for gold_index, pred_index in sorted(content_unknown):
            gold_side, pred_side = golds[gold_index], preds[pred_index]
            result.reported_unresolved.add(gold_side.ref)
            result.pairings.append(
                PairingDetail(
                    layer="l2",
                    status="unresolved_value",
                    gold_ref=gold_side.ref,
                    prediction_ref=pred_side.ref,
                    fields=sorted(set(content_unknown[(gold_index, pred_index)])),
                )
            )
            result.reasons.append(f"unresolved_value:{gold_side.ref}|{pred_side.ref}")
    else:
        for pred_index, pred_side in enumerate(preds):
            if pred_index in matched_pred_indices:
                continue
            unknown_content_keys = sorted(
                key for key in content_unknown if key[1] == pred_index
            )
            if unknown_content_keys:
                result.pairings.append(
                    PairingDetail(
                        layer="l2",
                        status="unknown_candidate_extra",
                        gold_ref=None,
                        prediction_ref=pred_side.ref,
                        fields=sorted(
                            {
                                field
                                for key in unknown_content_keys
                                for field in content_unknown[key]
                            }
                        ),
                    )
                )
            unknown_attr_keys = sorted(
                key for key in attr_unknown if key[1] == pred_index
            )
            if unknown_attr_keys:
                result.pairings.append(
                    PairingDetail(
                        layer="l2",
                        status="unknown_relation",
                        gold_ref=golds[unknown_attr_keys[0][0]].ref,
                        prediction_ref=pred_side.ref,
                        fields=["condition"],
                    )
                )
            if unknown_content_keys or unknown_attr_keys:
                continue
            # 一对一约束：已与其他预测配对的 Gold 不能再作为错配对象；
            # 候选全部已被占用时，该预测按多报（extra_value）诊断
            mismatch_keys = sorted(
                key
                for key in content_mismatch
                if key[1] == pred_index and key[0] not in matched_gold_indices
            )
            if mismatch_keys:
                gold_index, _ = mismatch_keys[0]
                gold_side = golds[gold_index]
                result.reported_mismatch.add(gold_side.ref)
                result.pairings.append(
                    PairingDetail(
                        layer="l2",
                        status="value_mismatch",
                        gold_ref=gold_side.ref,
                        prediction_ref=pred_side.ref,
                        fields=sorted(set(content_mismatch[mismatch_keys[0]])),
                    )
                )
                continue
            forbidden_keys = sorted(
                key for key in attr_forbidden if key[1] == pred_index
            )
            has_equivalent_edge = any(key[1] == pred_index for key in attr_equivalent)
            if forbidden_keys and not has_equivalent_edge:
                gold_index, _ = forbidden_keys[0]
                result.pairings.append(
                    PairingDetail(
                        layer="l2",
                        status="attribution_mismatch",
                        gold_ref=golds[gold_index].ref,
                        prediction_ref=pred_side.ref,
                        fields=attr_forbidden[forbidden_keys[0]],
                    )
                )
                continue
            result.pairings.append(
                PairingDetail(
                    layer="l2",
                    status="extra_value",
                    gold_ref=None,
                    prediction_ref=pred_side.ref,
                )
            )

    for gold_index, gold_side in enumerate(golds):
        if gold_index in matched_gold_indices:
            continue
        if gold_side.ref in result.reported_mismatch:
            continue
        if gold_side.ref in result.reported_unresolved:
            continue
        result.pairings.append(
            PairingDetail(
                layer="l2",
                status="value_missing",
                gold_ref=gold_side.ref,
                prediction_ref=None,
                fields=[],
            )
        )
    return result


@dataclass
class L1Match:
    pairings: list[PairingDetail] = field(default_factory=list)
    matched_pairs: list[tuple[GoldObjectEntry, PredictionObjectEntry]] = field(
        default_factory=list
    )
    duplicate_pairs: list[tuple[GoldObjectEntry, PredictionObjectEntry]] = field(
        default_factory=list
    )
    unmatched_gold: list[GoldObjectEntry] = field(default_factory=list)
    extra_pred: list[PredictionObjectEntry] = field(default_factory=list)
    ambiguous_pred: list[PredictionObjectEntry] = field(default_factory=list)
    pending_gold: list[GoldObjectEntry] = field(default_factory=list)
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def pending(self) -> int:
        return len(self.ambiguous_pred) + len(self.pending_gold)


def match_objects(
    gold_objects: list[GoldObjectEntry],
    pred_objects: list[PredictionObjectEntry],
) -> L1Match:
    """L1：标识/别名受限整理后的一一对应。

    同一标识的重复预测消耗一次 TP、其余 FP（确定），并记录到
    ``duplicate_pairs`` 供 L2 以同一确认身份的记录池做一对一配对；
    两个及以上 Gold 共用别名的真实身份歧义保留未决（pending），不计
    FP/FN，不凭数值消除，也不纳入重复池。
    """
    match = L1Match()
    taken: set[str] = set()
    ambiguous_candidates: set[str] = set()
    for pred in pred_objects:
        pred_ids = {normalize_identifier(name) for name in pred.identifiers}
        candidates = [
            gold
            for gold in gold_objects
            if pred_ids & {normalize_identifier(name) for name in gold.identifiers}
        ]
        if not candidates:
            match.fp += 1
            match.extra_pred.append(pred)
            match.pairings.append(
                PairingDetail(layer="l1", status="extra", prediction_ref=pred.label)
            )
            continue
        if len(candidates) > 1:
            match.ambiguous_pred.append(pred)
            ambiguous_candidates.update(gold.label for gold in candidates)
            match.pairings.append(
                PairingDetail(layer="l1", status="ambiguous", prediction_ref=pred.label)
            )
            continue
        gold = candidates[0]
        if gold.label in taken:
            # 重复预测：身份已唯一确认指向同一 Gold，L1 记 FP，
            # L2 由 score.py 汇入同一身份的记录池参与数值一对一
            match.fp += 1
            match.duplicate_pairs.append((gold, pred))
            match.pairings.append(
                PairingDetail(
                    layer="l1",
                    status="duplicate",
                    gold_ref=gold.label,
                    prediction_ref=pred.label,
                )
            )
            continue
        taken.add(gold.label)
        match.tp += 1
        match.matched_pairs.append((gold, pred))
        match.pairings.append(
            PairingDetail(
                layer="l1",
                status="matched",
                gold_ref=gold.label,
                prediction_ref=pred.label,
            )
        )
    for gold in gold_objects:
        if gold.label in taken:
            continue
        if gold.label in ambiguous_candidates:
            match.pending_gold.append(gold)
            match.pairings.append(
                PairingDetail(layer="l1", status="pending", gold_ref=gold.label)
            )
        else:
            match.fn += 1
            match.unmatched_gold.append(gold)
            match.pairings.append(
                PairingDetail(layer="l1", status="omitted", gold_ref=gold.label)
            )
    return match
