"""``score_batch``：纯输入的整批 L0/L1/L2 评分入口。

接口是可 JSON 序列化的纯输入（计划、Gold、预测、辅助配置）；本函数
不读取文件、不调用真实服务。只有当辅助配置显式给出冻结目录并注入
同步适配器时，才会在该目录持久保存 Agent 判定。

计划中的指纹按规范 JSON/哈希对实际输入重算核对，过期指纹整批拒绝；
重复论文、计划外论文、重复内部记录 ID、错论文归属等同样拒绝，不默
默覆盖。缺失预测视作未交付并保留本篇 Gold 分母；部分覆盖 Gold 只在
预声明封闭子范围内给 scoped 指标，没有封闭范围时只报告准备缺口、
不出最终分数。影响 L2 计分的未决项或辅助技术失败令整批 provisional，
保留确定计数、最终 L2 比率置空。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from hvs_catalog.records.common import sha256_of

from .agent import (
    AgentAdapter,
    AuxiliaryAdjudicator,
    AuxiliaryScoringConfig,
    FrozenDecisionStore,
    ScoringAgentResolver,
    auxiliary_config_digest,
)
from .errors import ScoringInputError
from .layers import (
    ValueSide,
    match_objects,
    normalize_identifier,
    pair_quantity_values,
)
from .models import (
    GoldObjectEntry,
    GoldPaperEntry,
    PredictionObjectEntry,
    PredictionPaperEntry,
    ScoringPlan,
    gold_input_digest,
    predictions_input_digest,
)
from .results import (
    AuxiliarySummary,
    BatchMicro,
    Diagnostic,
    JudgmentSummary,
    L0Metrics,
    L1Metrics,
    L2Metrics,
    PairingDetail,
    PaperResult,
    QuantityBreakdown,
    ScoringReport,
    l1_rates,
    l2_rates,
    ratio,
)

_MISSING_PREDICTION_ERROR = "missing prediction for planned paper"


def _revalidate_inputs(
    plan: ScoringPlan,
    gold: list[GoldPaperEntry],
    predictions: list[PredictionPaperEntry],
) -> tuple[ScoringPlan, list[GoldPaperEntry], list[PredictionPaperEntry]]:
    """对当前实际嵌套内容做全量结构重校验并返回重建后的副本。

    在任何索引/摘要/Agent/缓存操作之前执行。模型约束在构造时检查；
    ``model_validate(instance)`` 对同类实例默认跳过重校验，且字段赋值
    或列表追加不会触发父级跨模型约束——构造合法后被变异的输入（如
    重复 record_id、重复 label/物理量、可序列化类型变异如 bytes 路径）
    必须在此显式拒绝。重建从 ``model_dump(mode="json")`` 的纯数据
    出发：后续索引、指纹、归属、Agent 上下文/请求、评分与报告统一
    使用返回的校验副本，不校验 A 却使用 B；副本不写回调用者。
    """
    rebuilt_gold: list[GoldPaperEntry] = []
    rebuilt_predictions: list[PredictionPaperEntry] = []
    for entry in (*gold, *predictions):
        try:
            rebuilt = type(entry).model_validate(entry.model_dump(mode="json"))
        except ValidationError as error:
            raise ScoringInputError(
                f"{entry.paper_key} 的当前输入内容未通过结构校验"
                f"（构造后可能被变异，含重复 record_id/label 等）：{error}"
            ) from error
        if isinstance(entry, GoldPaperEntry):
            rebuilt_gold.append(rebuilt)
        else:
            rebuilt_predictions.append(rebuilt)
    try:
        rebuilt_plan = ScoringPlan.model_validate(plan.model_dump(mode="json"))
    except ValidationError as error:
        raise ScoringInputError(
            f"评分计划当前内容未通过结构校验（构造后可能被变异）：{error}"
        ) from error
    return rebuilt_plan, rebuilt_gold, rebuilt_predictions


def score_batch(
    plan: ScoringPlan,
    gold: list[GoldPaperEntry],
    predictions: list[PredictionPaperEntry],
    *,
    auxiliary: AuxiliaryScoringConfig,
    agent_adapter: AgentAdapter | None = None,
) -> ScoringReport:
    """对整批计划论文输出逐论文与微汇总的 L0/L1/L2 报告。"""
    # 全部后续处理（索引、指纹、归属、Agent 请求、评分、报告）统一
    # 使用重校验后重建的副本；调用者原对象不被修改，也不参与计分。
    plan, gold, predictions = _revalidate_inputs(plan, gold, predictions)
    gold_map = _validate_inputs(plan, gold, predictions, auxiliary)
    pred_map = {entry.paper_key: entry for entry in predictions}
    resolver, adjudicator = _build_auxiliary(
        plan, gold, predictions, auxiliary, agent_adapter
    )

    papers: list[PaperResult] = []
    notes: list[str] = []
    provisional_reasons: list[str] = []
    blocked_papers: list[str] = []
    for paper_key in plan.paper_keys:
        result = _score_paper(
            plan,
            paper_key,
            gold_map.get(paper_key),
            pred_map.get(paper_key),
            resolver,
        )
        papers.append(result)
        if result.readiness == "scoped":
            notes.append(
                f"{paper_key} 按预声明封闭子范围评分，只输出 scoped 指标，"
                "不声称整论文准确率"
            )
        if result.readiness == "blocked":
            blocked_papers.append(paper_key)
            provisional_reasons.append(f"{paper_key}:readiness_gap")
            notes.append(f"{paper_key} 存在准备缺口，未进入最终评分；整批计数覆盖不全")
        if result.unresolved:
            provisional_reasons.extend(
                f"{paper_key}:{diagnostic.code}" for diagnostic in result.unresolved
            )
        if any(
            diagnostic.code == "ambiguous_object_alias"
            for diagnostic in result.diagnostics
        ):
            provisional_reasons.append(f"{paper_key}:ambiguous_object_alias")
            notes.append(f"{paper_key} 的 L1 身份歧义保留未决关系，相关层最终率置空")

    if resolver is not None and auxiliary.mode == "protocol_only":
        notes.append(
            "辅助判定运行在 protocol_only 模拟模式：协议与冻结行为已验证，"
            "不代表真实辅助模型能力已通过验证"
        )

    report = ScoringReport(
        digests={
            "gold": gold_input_digest(gold),
            "predictions": predictions_input_digest(predictions),
            "auxiliary": auxiliary_config_digest(auxiliary),
        },
        references={
            name: {
                "name": ref.name,
                "version": ref.version,
                "fingerprint": ref.fingerprint,
            }
            for name, ref in (
                ("gold", plan.gold_ref),
                ("materials", plan.materials_ref),
                ("science_rules", plan.science_rules_ref),
                ("scoring_scheme", plan.scoring_scheme_ref),
                ("program", plan.program_ref),
                ("auxiliary", plan.auxiliary_ref),
            )
        },
        papers=papers,
        micro=_micro(papers),
        batch_provisional=bool(provisional_reasons),
        readiness_blocked_papers=blocked_papers,
        provisional_reasons=sorted(set(provisional_reasons)),
        correct_empty_papers=[p.paper_key for p in papers if p.correct_empty],
        auxiliary=(_auxiliary_summary(resolver, adjudicator, auxiliary)),
        notes=notes,
    )
    return report


def _validate_inputs(
    plan: ScoringPlan,
    gold: list[GoldPaperEntry],
    predictions: list[PredictionPaperEntry],
    auxiliary: AuxiliaryScoringConfig,
) -> dict[str, GoldPaperEntry]:
    gold_keys = [entry.paper_key for entry in gold]
    duplicated = {key for key in gold_keys if gold_keys.count(key) > 1}
    if duplicated:
        raise ScoringInputError(f"Gold 中论文重复：{sorted(duplicated)}")
    unknown = [key for key in gold_keys if key not in set(plan.paper_keys)]
    if unknown:
        raise ScoringInputError(f"Gold 含计划外论文：{sorted(unknown)}")
    pred_keys = [entry.paper_key for entry in predictions]
    duplicated = {key for key in pred_keys if pred_keys.count(key) > 1}
    if duplicated:
        raise ScoringInputError(f"预测中论文重复：{sorted(duplicated)}")
    unknown = [key for key in pred_keys if key not in set(plan.paper_keys)]
    if unknown:
        raise ScoringInputError(f"预测含计划外论文：{sorted(unknown)}")
    actual_gold_digest = gold_input_digest(gold)
    if plan.gold_ref.fingerprint != actual_gold_digest:
        raise ScoringInputError("计划中的 Gold 指纹与实际输入不符，过期指纹不被信任")
    if plan.auxiliary_ref.fingerprint != auxiliary_config_digest(auxiliary):
        raise ScoringInputError("计划中的辅助配置指纹与实际配置不符，过期指纹不被信任")
    scopes = {scope.paper_key: scope for scope in plan.scopes}
    gold_map = {entry.paper_key: entry for entry in gold}
    for coverage in plan.scoped_coverages:
        scope = scopes[coverage.paper_key]
        extra = sorted(set(coverage.quantity_paths) - set(scope.quantity_paths))
        if extra:
            raise ScoringInputError(
                f"{coverage.paper_key} 的封闭子范围超出预声明评价范围：{extra}"
            )
        gold_paper = gold_map.get(coverage.paper_key)
        if gold_paper is not None and gold_paper.coverage == "complete":
            raise ScoringInputError(
                f"{coverage.paper_key} 的 Gold 已声明完整，不能再预声明封闭子范围"
            )
    return gold_map


def _build_auxiliary(
    plan: ScoringPlan,
    gold: list[GoldPaperEntry],
    predictions: list[PredictionPaperEntry],
    auxiliary: AuxiliaryScoringConfig,
    agent_adapter: AgentAdapter | None,
) -> tuple[ScoringAgentResolver | None, AuxiliaryAdjudicator | None]:
    if agent_adapter is None:
        return None, None
    store = None
    if auxiliary.store_directory:
        store = FrozenDecisionStore(Path(auxiliary.store_directory))
    adjudicator = AuxiliaryAdjudicator(
        config=auxiliary, adapter=agent_adapter, store=store
    )
    resolver = ScoringAgentResolver(
        adjudicator,
        context_digests={
            "gold": gold_input_digest(gold),
            "predictions": predictions_input_digest(predictions),
            # 完整计划（论文范围、scopes、scoped_coverages、证据注册表、
            # 全部引用指纹）参与冻结请求绑定：范围或注册表变化不可复用
            # 旧冻结判定；摘要由计划对象规范化计算，不可自报。
            "scoring_plan": sha256_of(plan.model_dump(mode="json")),
            "materials": plan.materials_ref.fingerprint,
            "science_rules": plan.science_rules_ref.fingerprint,
            "scoring_scheme": plan.scoring_scheme_ref.fingerprint,
            "program": plan.program_ref.fingerprint,
            "auxiliary": auxiliary_config_digest(auxiliary),
        },
        materials_name=plan.materials_ref.name,
        materials_fingerprint=plan.materials_ref.fingerprint,
    )
    return resolver, adjudicator


def _missing_prediction(paper_key: str) -> PredictionPaperEntry:
    return PredictionPaperEntry(
        paper_key=paper_key,
        delivered=False,
        format_valid=False,
        completion="unfinished",
        errors=[_MISSING_PREDICTION_ERROR],
        objects=[],
    )


def _scope_for(plan: ScoringPlan, paper_key: str):
    scope = next(s for s in plan.scopes if s.paper_key == paper_key)
    scoped = next((c for c in plan.scoped_coverages if c.paper_key == paper_key), None)
    return scope, scoped


def _scoped_confirmed(scoped, gold_paper: GoldPaperEntry) -> bool:
    for identifier in scoped.object_identifiers:
        for path in scoped.quantity_paths:
            if not any(
                normalize_identifier(identifier)
                in {normalize_identifier(item) for item in confirmed.object_identifiers}
                and path in confirmed.quantity_paths
                for confirmed in gold_paper.confirmed_ranges
            ):
                return False
    return True


def _score_paper(
    plan: ScoringPlan,
    paper_key: str,
    gold_paper: GoldPaperEntry | None,
    prediction: PredictionPaperEntry | None,
    resolver: ScoringAgentResolver | None,
) -> PaperResult:
    scope, scoped = _scope_for(plan, paper_key)
    if prediction is None:
        prediction = _missing_prediction(paper_key)
    l0 = _l0(prediction)
    gaps: list[str] = []
    if gold_paper is None:
        gaps.append("gold_missing")
    elif scoped is not None:
        if not _scoped_confirmed(scoped, gold_paper):
            gaps.append("scoped_coverage_not_confirmed")
        matched_objects = {
            normalize_identifier(name)
            for obj in gold_paper.objects
            for name in obj.identifiers
        } & {normalize_identifier(name) for name in scoped.object_identifiers}
        if not matched_objects:
            gaps.append("scoped_object_not_in_gold")
    elif gold_paper.coverage == "partial":
        gaps.append("partial_gold_without_predeclared_scope")
    if gaps:
        return PaperResult(
            paper_key=paper_key,
            readiness="blocked",
            readiness_gaps=gaps,
            l0=l0,
            correct_empty=False,
            prediction_errors=list(prediction.errors),
            diagnostics=[
                Diagnostic(code="readiness_gap", message=gap, refs=[paper_key])
                for gap in gaps
            ],
        )

    scoped_ids = (
        {normalize_identifier(name) for name in scoped.object_identifiers}
        if scoped is not None
        else None
    )
    effective_paths = (
        set(scope.quantity_paths)
        if scoped is None
        else set(scope.quantity_paths) & set(scoped.quantity_paths)
    )

    def _ids(entry) -> set[str]:
        return {normalize_identifier(name) for name in entry.identifiers}

    gold_in_scope = [
        obj
        for obj in (gold_paper.objects if gold_paper else [])
        if scoped_ids is None or _ids(obj) & scoped_ids
    ]
    gold_out_scope = [
        obj
        for obj in (gold_paper.objects if gold_paper else [])
        if not (scoped_ids is None or _ids(obj) & scoped_ids)
    ]
    evaluable = prediction.delivered and prediction.format_valid
    pred_objects = prediction.objects if evaluable else []
    pred_in_scope = [
        obj for obj in pred_objects if scoped_ids is None or _ids(obj) & scoped_ids
    ]
    pred_out_scope = [
        obj
        for obj in pred_objects
        if not (scoped_ids is None or _ids(obj) & scoped_ids)
    ]

    diagnostics: list[Diagnostic] = []
    pairings: list[PairingDetail] = []
    for obj in gold_out_scope:
        diagnostics.append(
            Diagnostic(
                code="out_of_scope_object",
                message="Gold 对象不在预声明封闭子范围内",
                refs=[obj.label],
            )
        )
        pairings.extend(_out_of_scope_rows(obj, effective_paths, side="gold"))
    for obj in pred_out_scope:
        diagnostics.append(
            Diagnostic(
                code="out_of_scope_object",
                message="预测对象不在预声明封闭子范围内",
                refs=[obj.label],
            )
        )
        pairings.extend(_out_of_scope_rows(obj, effective_paths, side="prediction"))

    l1_match = match_objects(gold_in_scope, pred_in_scope)
    pairings.extend(l1_match.pairings)
    for pred_obj in l1_match.ambiguous_pred:
        diagnostics.append(
            Diagnostic(
                code="ambiguous_object_alias",
                message="多个 Gold 对象共用该别名，身份歧义保留未决，不计 FP/FN",
                refs=[pred_obj.label],
            )
        )
    # 同一已唯一确认 Gold 身份的全部预测对象（TP + 重复）逐条检查独立
    # 诊断字段：贡献类型/作者判断不遍历池内每条预测会漏诊且受列表顺
    # 序影响；这些诊断仅定位实际不一致条目，绝不计入 L1/L2 质量。
    pooled_by_gold: dict[str, tuple[GoldObjectEntry, list[PredictionObjectEntry]]] = {}
    for gold_obj, pred_obj in l1_match.matched_pairs:
        pooled_by_gold[gold_obj.label] = (gold_obj, [pred_obj])
    for gold_obj, pred_obj in l1_match.duplicate_pairs:
        pooled_by_gold[gold_obj.label][1].append(pred_obj)
    for label in sorted(pooled_by_gold):
        gold_obj, pooled_preds = pooled_by_gold[label]
        for pooled_pred in pooled_preds:
            for field_name, code in (
                ("contribution_type", "contribution_type_mismatch"),
                ("author_judgment", "author_judgment_mismatch"),
            ):
                left = getattr(gold_obj, field_name)
                right = getattr(pooled_pred, field_name)
                if left is not None and right is not None and left != right:
                    diagnostics.append(
                        Diagnostic(
                            code=code,
                            message=f"{field_name} 诊断不一致（不计入质量分）",
                            refs=[gold_obj.label, pooled_pred.label],
                        )
                    )

    per_path: dict[str, dict[str, int]] = {}
    path_reasons: dict[str, list[str]] = {}

    def _tally(
        path: str, *, correct: int = 0, predicted: int = 0, gold: int = 0
    ) -> None:
        counts = per_path.setdefault(path, {"correct": 0, "predicted": 0, "gold": 0})
        counts["correct"] += correct
        counts["predicted"] += predicted
        counts["gold"] += gold

    # 记录池已在诊断段建立（pooled_by_gold）：同一身份的数值候选
    # 合并后按现有科学归属关系做记录级一对一，Gold 值仅消耗一次，
    # 所有预测值仍入 P；不由列表顺序/内部 label 挑唯一可比较者。
    for label in sorted(pooled_by_gold):
        gold_obj, pooled_preds = pooled_by_gold[label]
        gold_groups = {
            group.quantity_path: group.values for group in gold_obj.quantities
        }
        pred_groups: dict[str, list] = {}
        for pooled_pred in pooled_preds:
            for group in pooled_pred.quantities:
                pred_groups.setdefault(group.quantity_path, []).extend(group.values)
        pred_owner = {
            (group.quantity_path, entry.record_id): pooled_pred.label
            for pooled_pred in pooled_preds
            for group in pooled_pred.quantities
            for entry in group.values
        }
        for path in sorted(set(gold_groups) | set(pred_groups)):
            if path not in effective_paths:
                pairings.extend(
                    _out_of_scope_entries(
                        gold_obj.label, path, gold_groups.get(path, []), side="gold"
                    )
                )
                for pooled_pred in pooled_preds:
                    pairings.extend(
                        _out_of_scope_entries(
                            pooled_pred.label,
                            path,
                            [
                                entry
                                for group in pooled_pred.quantities
                                if group.quantity_path == path
                                for entry in group.values
                            ],
                            side="prediction",
                        )
                    )
                continue
            gold_sides = [
                ValueSide(
                    gold_obj.label,
                    path,
                    entry.record_id,
                    entry.value,
                    entry.evidence_fragments,
                )
                for entry in gold_groups.get(path, [])
            ]
            pred_sides = [
                ValueSide(
                    pred_owner[(path, entry.record_id)],
                    path,
                    entry.record_id,
                    entry.value,
                    entry.evidence_fragments,
                )
                for entry in pred_groups.get(path, [])
            ]
            pairing = pair_quantity_values(gold_sides, pred_sides, resolver=resolver)
            pairings.extend(pairing.pairings)
            _tally(
                path,
                correct=pairing.matched,
                predicted=pairing.predicted,
                gold=pairing.gold,
            )
            if pairing.reasons:
                path_reasons.setdefault(path, []).extend(pairing.reasons)

    for gold_obj in l1_match.unmatched_gold:
        for group in gold_obj.quantities:
            if group.quantity_path in effective_paths:
                _tally(group.quantity_path, gold=len(group.values))
                pairings.extend(
                    PairingDetail(
                        layer="l2",
                        status="object_omitted_value",
                        gold_ref=f"{gold_obj.label}|{group.quantity_path}|{entry.record_id}",
                    )
                    for entry in group.values
                )
            else:
                pairings.extend(
                    _out_of_scope_entries(
                        gold_obj.label, group.quantity_path, group.values, side="gold"
                    )
                )
    for pred_obj in l1_match.extra_pred:
        for group in pred_obj.quantities:
            if group.quantity_path in effective_paths:
                _tally(group.quantity_path, predicted=len(group.values))
                pairings.extend(
                    PairingDetail(
                        layer="l2",
                        status="extra_object_value",
                        prediction_ref=f"{pred_obj.label}|{group.quantity_path}|{entry.record_id}",
                    )
                    for entry in group.values
                )
            else:
                pairings.extend(
                    _out_of_scope_entries(
                        pred_obj.label,
                        group.quantity_path,
                        group.values,
                        side="prediction",
                    )
                )
    # 身份歧义对象：值计入实际 P/G 总数，但对应关系未决，不标漏报/多报
    for pred_obj in l1_match.ambiguous_pred:
        for group in pred_obj.quantities:
            if group.quantity_path in effective_paths:
                _tally(group.quantity_path, predicted=len(group.values))
                for entry in group.values:
                    ref = f"{pred_obj.label}|{group.quantity_path}|{entry.record_id}"
                    pairings.append(
                        PairingDetail(
                            layer="l2",
                            status="unresolved_object_identity",
                            prediction_ref=ref,
                        )
                    )
                    path_reasons.setdefault(group.quantity_path, []).append(
                        f"unresolved_object_identity:{ref}"
                    )
            else:
                pairings.extend(
                    _out_of_scope_entries(
                        pred_obj.label,
                        group.quantity_path,
                        group.values,
                        side="prediction",
                    )
                )
    for gold_obj in l1_match.pending_gold:
        for group in gold_obj.quantities:
            if group.quantity_path in effective_paths:
                _tally(group.quantity_path, gold=len(group.values))
                for entry in group.values:
                    ref = f"{gold_obj.label}|{group.quantity_path}|{entry.record_id}"
                    pairings.append(
                        PairingDetail(
                            layer="l2",
                            status="unresolved_object_identity",
                            gold_ref=ref,
                        )
                    )
                    path_reasons.setdefault(group.quantity_path, []).append(
                        f"unresolved_object_identity:{ref}"
                    )
            else:
                pairings.extend(
                    _out_of_scope_entries(
                        gold_obj.label,
                        group.quantity_path,
                        group.values,
                        side="gold",
                    )
                )

    tp, fp, fn = l1_match.tp, l1_match.fp, l1_match.fn
    l1_pending = l1_match.pending
    precision, recall, f1 = l1_rates(tp, fp, fn, l1_pending)
    correct = sum(counts["correct"] for counts in per_path.values())
    predicted = sum(counts["predicted"] for counts in per_path.values())
    gold_total = sum(counts["gold"] for counts in per_path.values())
    paper_l2_unresolved = bool(path_reasons)
    l2_precision, l2_recall, l2_f1 = (
        (None, None, None)
        if paper_l2_unresolved
        else l2_rates(correct, predicted, gold_total)
    )
    breakdown = []
    for path in sorted(per_path):
        counts = per_path[path]
        path_rates = (
            (None, None, None)
            if path in path_reasons
            else l2_rates(counts["correct"], counts["predicted"], counts["gold"])
        )
        breakdown.append(
            QuantityBreakdown(
                quantity_path=path,
                metrics=L2Metrics(
                    correct=counts["correct"],
                    predicted=counts["predicted"],
                    gold=counts["gold"],
                    precision=path_rates[0],
                    recall=path_rates[1],
                    f1=path_rates[2],
                ),
            )
        )
    unresolved = [
        Diagnostic(
            code=code_of(reason),
            message=reason,
            refs=[paper_key],
        )
        for reason in sorted({r for reasons in path_reasons.values() for r in reasons})
    ]
    evidence_verification = _evidence_verification(
        prediction, plan, effective_paths, diagnostics, pred_in_scope=pred_in_scope
    )
    correct_empty = (
        scoped is None
        and gold_paper is not None
        and gold_paper.coverage == "complete"
        and not gold_paper.objects
        and prediction.delivered
        and prediction.format_valid
        and prediction.completion == "complete"
        and not prediction.objects
    )
    return PaperResult(
        paper_key=paper_key,
        readiness="scoped" if scoped is not None else "ready",
        scoped=scoped is not None,
        scope_note=(
            f"papers=[{paper_key}]; objects={sorted(scoped.object_identifiers)}; "
            f"quantities={sorted(scoped.quantity_paths)}"
            if scoped is not None
            else None
        ),
        l0=l0,
        correct_empty=correct_empty,
        l1=L1Metrics(
            tp=tp,
            fp=fp,
            fn=fn,
            pending=l1_pending,
            precision=precision,
            recall=recall,
            f1=f1,
        ),
        l2=L2Metrics(
            correct=correct,
            predicted=predicted,
            gold=gold_total,
            precision=l2_precision,
            recall=l2_recall,
            f1=l2_f1,
        ),
        l2_provisional=paper_l2_unresolved,
        quantity_breakdown=breakdown,
        prediction_errors=list(prediction.errors),
        pairings=pairings,
        diagnostics=diagnostics,
        unresolved=unresolved,
        evidence_verification=evidence_verification,
    )


def _evidence_verification(
    prediction: PredictionPaperEntry,
    plan: ScoringPlan,
    effective_paths: set[str],
    diagnostics: list[Diagnostic],
    *,
    pred_in_scope: list,
) -> dict[str, int]:
    """预测值证据的独立核验状态；只作诊断，不影响 C。

    统计范围与 scoped L1/L2 一致：只覆盖封闭子范围内的对象与范围内
    物理量；范围外对象的证据状态单列（``out_of_scope``），不混入
    scope 统计。无可核对材料时明确 not_verified；提供证据注册表时
    判明悬空引用；片段绑定材料与计划不符时判 wrong_material。不以
    字段恰好相等推断证据可靠，也不给出"已验证"结论。
    """
    counts = {
        "values": 0,
        "not_verified": 0,
        "registered": 0,
        "dangling": 0,
        "wrong_material": 0,
        "out_of_scope": 0,
    }
    registry = set(plan.known_evidence_record_ids)
    in_scope_labels = {id(obj) for obj in pred_in_scope}
    for obj in prediction.objects:
        in_scope = id(obj) in in_scope_labels
        for group in obj.quantities:
            for entry in group.values:
                if not in_scope or group.quantity_path not in effective_paths:
                    # 范围外（对象或物理量）的证据状态单列，不混入 scope 统计
                    counts["out_of_scope"] += 1
                    continue
                counts["values"] += 1
                dangling = [
                    part.evidence_ref.record_id
                    for part in entry.value.direct_evidence
                    if part.evidence_ref.record_id not in registry
                ]
                wrong_material = any(
                    fragment.material_fingerprint != plan.materials_ref.fingerprint
                    or fragment.material_name != plan.materials_ref.name
                    for fragment in entry.evidence_fragments
                )
                if dangling and registry:
                    counts["dangling"] += 1
                    diagnostics.append(
                        Diagnostic(
                            code="dangling_evidence_ref",
                            message=f"证据引用不在提供注册表内：{sorted(set(dangling))}",
                            refs=[
                                f"{obj.label}|{group.quantity_path}|{entry.record_id}"
                            ],
                        )
                    )
                elif wrong_material:
                    counts["wrong_material"] += 1
                    diagnostics.append(
                        Diagnostic(
                            code="wrong_material_evidence",
                            message="原文片段绑定材料与计划声明不符",
                            refs=[
                                f"{obj.label}|{group.quantity_path}|{entry.record_id}"
                            ],
                        )
                    )
                elif entry.evidence_fragments:
                    counts["registered"] += 1
                else:
                    counts["not_verified"] += 1
    return counts


def code_of(reason: str) -> str:
    if reason.startswith("agent_technical_failure"):
        return "agent_technical_failure"
    if reason.startswith("unresolved_attribution"):
        return "unresolved_attribution"
    if reason.startswith("unresolved_value"):
        return "unresolved_value"
    if reason.startswith("unresolved_object_identity"):
        return "unresolved_object_identity"
    return "unresolved"


def _out_of_scope_rows(
    obj, effective_paths: set[str], *, side: str
) -> list[PairingDetail]:
    """范围外对象的全部数值都记为 out_of_scope_value。"""
    rows: list[PairingDetail] = []
    for group in obj.quantities:
        rows.extend(
            _out_of_scope_entries(
                obj.label, group.quantity_path, group.values, side=side
            )
        )
    return rows


def _out_of_scope_entries(
    owner: str, path: str, entries, *, side: str
) -> list[PairingDetail]:
    return [
        PairingDetail(
            layer="l2",
            status="out_of_scope_value",
            gold_ref=f"{owner}|{path}|{entry.record_id}" if side == "gold" else None,
            prediction_ref=(
                None if side == "gold" else f"{owner}|{path}|{entry.record_id}"
            ),
        )
        for entry in entries
    ]


def _l0(prediction: PredictionPaperEntry) -> L0Metrics:
    delivered = 1 if prediction.delivered else 0
    valid = 1 if prediction.delivered and prediction.format_valid else 0
    return L0Metrics(
        planned=1,
        delivered=delivered,
        format_valid=valid,
        completion_counts={prediction.completion: 1},
        delivered_over_planned=ratio(delivered, 1),
        valid_over_delivered=ratio(valid, delivered),
        valid_over_planned=ratio(valid, 1),
    )


def _micro(papers: list[PaperResult]) -> BatchMicro:
    planned = len(papers)
    delivered = sum(p.l0.delivered for p in papers)
    valid = sum(p.l0.format_valid for p in papers)
    completion: dict[str, int] = {}
    for paper in papers:
        for state, count in paper.l0.completion_counts.items():
            completion[state] = completion.get(state, 0) + count
    l1_papers = [p for p in papers if p.l1 is not None]
    l2_papers = [p for p in papers if p.l2 is not None]
    coverage: Literal["full", "partial"] = (
        "partial" if any(p.readiness == "blocked" for p in papers) else "full"
    )
    micro_l1 = None
    if l1_papers:
        tp = sum(p.l1.tp for p in l1_papers)  # type: ignore[union-attr]
        fp = sum(p.l1.fp for p in l1_papers)  # type: ignore[union-attr]
        fn = sum(p.l1.fn for p in l1_papers)  # type: ignore[union-attr]
        pending = sum(p.l1.pending for p in l1_papers)  # type: ignore[union-attr]
        precision, recall, f1 = l1_rates(tp, fp, fn, pending)
        if coverage == "partial":
            precision = recall = f1 = None
        micro_l1 = L1Metrics(
            tp=tp,
            fp=fp,
            fn=fn,
            pending=pending,
            precision=precision,
            recall=recall,
            f1=f1,
        )
    micro_l2 = None
    if l2_papers:
        correct = sum(p.l2.correct for p in l2_papers)  # type: ignore[union-attr]
        predicted = sum(p.l2.predicted for p in l2_papers)  # type: ignore[union-attr]
        gold_total = sum(p.l2.gold for p in l2_papers)  # type: ignore[union-attr]
        unresolved = any(p.l2_provisional for p in l2_papers)
        rates = (
            (None, None, None)
            if unresolved
            else l2_rates(correct, predicted, gold_total)
        )
        if coverage == "partial":
            rates = (None, None, None)
        micro_l2 = L2Metrics(
            correct=correct,
            predicted=predicted,
            gold=gold_total,
            precision=rates[0],
            recall=rates[1],
            f1=rates[2],
        )
    return BatchMicro(
        coverage=coverage,
        l0=L0Metrics(
            planned=planned,
            delivered=delivered,
            format_valid=valid,
            completion_counts=dict(sorted(completion.items())),
            delivered_over_planned=ratio(delivered, planned),
            valid_over_delivered=ratio(valid, delivered),
            valid_over_planned=ratio(valid, planned),
        ),
        l1=micro_l1,
        l2=micro_l2,
    )


def _auxiliary_summary(
    resolver: ScoringAgentResolver | None,
    adjudicator: AuxiliaryAdjudicator | None,
    auxiliary: AuxiliaryScoringConfig,
) -> AuxiliarySummary:
    if resolver is None or adjudicator is None:
        return AuxiliarySummary(
            enabled=False, protocol_only=auxiliary.mode == "protocol_only"
        )
    judgments = [
        JudgmentSummary(
            entry_key=use.entry_key,
            request_kind=use.request_kind,
            field_name=use.field_name,
            verdict=use.verdict,
            status=use.status,
            reused=use.reused,
        )
        for use in adjudicator.uses
    ]
    failures = sum(1 for use in adjudicator.uses if use.status == "technical_failure")
    return AuxiliarySummary(
        enabled=True,
        protocol_only=auxiliary.mode == "protocol_only",
        judgments=judgments,
        technical_failures=failures,
    )
