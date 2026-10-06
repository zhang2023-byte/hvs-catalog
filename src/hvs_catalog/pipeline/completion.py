"""Read scientific completion from saved outputs without Gold or scoring dependencies."""

from __future__ import annotations


def science_completion(status: dict, roster, paper) -> tuple[str, list[str]]:
    """Scientific completion independently of runtime graph termination.

    Return pending for open or answered-but-unresolved scientific questions, unresolved
    roster entries/groups, or unresolved overall author assessments; retain
    errors/failures separately. Otherwise partial applies to runtime
    errors/nontermination, missing values, incomplete/gapped roster coverage,
    material-only unfinished author assessments (status=None with gap_refs only), or
    incomplete quantity coverage. Only truly complete results without unresolved items
    are complete. With no valid output, return failed for failures and unfinished
    otherwise. Excluded entries and resolved questions do not block completion. Accept
    read-only views or structurally equivalent duck-typed objects without modifying
    them.
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
            and not getattr(entry, "author_errors", [])
        ]
        unresolved_groups = list(getattr(roster.data, "unresolved_groups", None) or [])
        coverage = getattr(roster.data, "coverage", None)
    boundness_pending = False
    boundness_gap_only = False
    source_errors = False
    if paper is not None:
        for contribution in paper.data.object_contributions:
            boundness = contribution.paper_boundness
            if boundness.status is None:
                if boundness.issue_refs:
                    boundness_pending = True
                elif getattr(boundness, "author_errors", None):
                    source_errors = True
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
        # Current failures/nontermination remain facts. Historical errors after recovery to finished remain
        # in report failure diagnostics but no longer block scientific complete under the established ruling;
        # current valid results, question state, and coverage checks prove completeness.
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
    if source_errors or (
        roster and any(getattr(e, "author_errors", []) for e in roster.data.entries)
    ):
        reasons.append("原文错误影响部分科学内容，诊断保留且未收录")
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
