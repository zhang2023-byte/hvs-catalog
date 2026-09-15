"""演示流程的进程内测试：暂停与恢复、结果复用、决定规则与重复计费。

重点是暂停后输入、材料或证据发生变化的完整流程回归：恢复一律使用
运行固定输入与材料快照；证据变化后重建的问题属于新版本，旧决定不被
应用。测试通过修改模块常量模拟真实变化，不手工伪造摘要。“实际终止
进程”的版本见 test_demo_cli.py 的独立子进程测试，这里用异常注入覆盖
同一机制（响应已返回、结果未保存即失败）。
"""

import copy
import json
import sqlite3

import pytest

from hvs_catalog.demo import graph as demo_graph
from hvs_catalog.demo import runner
from hvs_catalog.demo import service as demo_service
from hvs_catalog.demo import store as demo_store

_MERGE_GROUPS = [["J1234+5678", "J1234+5679"], ["J0456+7890"]]
_SEPARATE_GROUPS = [["J1234+5678"], ["J1234+5679"], ["J0456+7890"]]


def test_fresh_run_pauses_with_pending_question(tmp_path):
    report = runner.run(tmp_path / "space")
    assert report.mode == "fresh"
    assert report.end_state == "awaiting-decision"
    assert report.pending_question["question_id"] == demo_graph.QUESTION_ID
    assert report.pending_question["choices"] == ["merge", "keep-separate"]
    # 问题正文按当前证据生成，并展示对象、测量与来源
    assert "速度测量完全相同" in report.pending_question["prompt"]
    assert "580±20 km/s（来源：当前论文）" in report.pending_question["prompt"]
    # 问题携带实际证据：涉及对象与它们的测量记录
    evidence = report.pending_question["evidence"]
    assert evidence["objects"] == ["J1234+5678", "J1234+5679"]
    assert len(evidence["measurements"]) == 2
    assert [(ex.step, ex.reused) for ex in report.executions] == [
        ("extract_roster", False),
        ("extract_values", False),
    ]
    assert report.billing["calls"] == 2


def test_rerun_while_awaiting_decision_does_not_execute(tmp_path):
    space = tmp_path / "space"
    runner.run(space)
    again = runner.run(space)
    assert again.mode == "none"
    assert again.end_state == "awaiting-decision"
    assert again.executions == []
    assert again.billing["calls"] == 2


def test_decide_completes_with_result_reuse(tmp_path):
    space = tmp_path / "space"
    runner.run(space)
    report = runner.decide(space, "merge")
    assert report.decision_outcome == "saved"
    assert report.completed
    # 恢复让 extract_values 从头重跑，但输入相同即复用业务库结果，不新增调用
    assert report.executions and all(ex.reused for ex in report.executions)
    assert report.billing["calls"] == 2
    assert report.summary["object_groups"] == _MERGE_GROUPS
    assert len(report.summary["all_measurements"]) == 3  # 测量全部保留
    after = runner.run(space)
    assert after.end_state == "already-finished"
    assert after.billing["calls"] == 2


def test_decide_keep_separate_groups_all_candidates(tmp_path):
    space = tmp_path / "space"
    runner.run(space)
    report = runner.decide(space, "keep-separate")
    assert report.completed
    assert report.summary["object_groups"] == _SEPARATE_GROUPS
    assert len(report.summary["all_measurements"]) == 3


def test_same_decision_is_idempotent_after_completion(tmp_path):
    space = tmp_path / "space"
    runner.run(space)
    first = runner.decide(space, "merge")
    second = runner.decide(space, "merge")
    assert first.decision_outcome == "saved"
    assert second.decision_outcome == "already-in-effect"
    assert not second.completed
    assert second.billing["calls"] == 2
    assert second.summary["object_groups"] == _MERGE_GROUPS


def test_conflicting_decision_rejected(tmp_path):
    space = tmp_path / "space"
    runner.run(space)
    runner.decide(space, "merge")
    with pytest.raises(demo_store.DecisionConflictError):
        runner.decide(space, "keep-separate")
    status = runner.status(space)
    assert status.summary["choice"] == "merge"
    assert status.billing["calls"] == 2


def test_decide_without_pending_question_rejected(tmp_path):
    with pytest.raises(runner.NoPendingQuestionError):
        runner.decide(tmp_path / "space", "merge")


def test_invalid_choice_rejected(tmp_path):
    with pytest.raises(ValueError, match="无效的裁决选择"):
        runner.decide(tmp_path / "space", "middle")


def _boom(step: str) -> None:
    raise RuntimeError(f"{step} 收到响应后、保存结果前失败")


def test_exit_after_response_leads_to_duplicate_billing(tmp_path):
    space = tmp_path / "space"
    with pytest.raises(RuntimeError, match="extract_roster"):
        runner.run(space, after_response=_boom)
    crashed = runner.status(space)
    assert crashed.started
    assert crashed.checkpoint_next == ("extract_roster",)
    assert crashed.results == []  # 响应未保存
    assert crashed.billing["calls"] == 1  # 账单已产生
    assert crashed.billing["total_cost"] == pytest.approx(0.5)

    resumed = runner.run(space)
    assert resumed.mode == "resumed-after-exit"
    roster = next(ex for ex in resumed.executions if ex.step == "extract_roster")
    assert not roster.reused
    assert roster.billed_before == 1  # 同一请求此前已计费，本次为重复调用
    assert resumed.end_state == "awaiting-decision"
    assert resumed.billing["calls"] == 3
    assert resumed.billing["total_cost"] == pytest.approx(1.8)
    assert any("重复计费" in line for line in resumed.lines())

    finished = runner.decide(space, "keep-separate")
    assert finished.completed
    assert finished.billing["calls"] == 3  # 恢复复用结果，不再新增调用
    assert finished.summary["object_groups"] == _SEPARATE_GROUPS


# ---- 暂停后输入、材料或证据变化的完整流程回归 ----


def test_rules_change_after_pause_completes_on_pinned_inputs(tmp_path, monkeypatch):
    """规则版本变化后恢复：沿用固定输入，不重算、不重复调用。

    这是对首轮验收探针的回归——此前实现会在恢复时用新规则重新计算
    并应用旧决定；现在状态中的固定输入使重建的问题与暂停时一致。
    """
    space = tmp_path / "space"
    runner.run(space)
    monkeypatch.setattr(demo_graph, "RULES_VERSION", "demo-rules-v2")

    report = runner.decide(space, "keep-separate")
    assert report.completed
    assert not report.superseded
    assert report.billing["calls"] == 2  # 沿用固定输入复用结果，无新调用
    assert report.summary["object_groups"] == _SEPARATE_GROUPS
    assert any("固定输入" in line for line in report.lines())
    assert any(
        "规则 demo-rules-v1 → 当前 demo-rules-v2" in line for line in report.lines()
    )

    status = runner.status(space)
    assert status.values["rules_version"] == "demo-rules-v1"  # 运行固定输入未变


def test_material_change_after_pause_completes_on_pinned_inputs(tmp_path, monkeypatch):
    """合成材料变化后恢复：沿用材料快照完成，并给出分歧提示。"""
    space = tmp_path / "space"
    runner.run(space)
    monkeypatch.setattr(demo_service, "PAPER_TITLE", "虚构演示论文 demo-001（修订版）")

    report = runner.decide(space, "merge")
    assert report.completed
    assert not report.superseded
    assert report.billing["calls"] == 2
    assert any("材料摘要" in line for line in report.lines())


def test_unsaved_response_recovery_answers_from_material_snapshot(
    tmp_path, monkeypatch
):
    """数值响应保存前退出：恢复按材料快照应答，不读取当前全局数据。

    复验探针回归——此前材料摘要只含论文标识与标题，重新调用读取修改
    后的全局合成数据，新值被登记在旧材料摘要之下且没有提示。现在
    材料以完整快照固定在运行状态中：恢复重新调用仍按快照应答。
    """
    space = tmp_path / "space"

    def crash_on_values(step: str) -> None:
        if step == demo_graph.STEP_VALUES:
            raise RuntimeError("extract_values 收到响应后、保存结果前退出")

    with pytest.raises(RuntimeError, match="extract_values"):
        runner.run(space, after_response=crash_on_values)
    assert runner.status(space).billing["calls"] == 2  # 名单已保存，数值已计费未保存

    mutated = copy.deepcopy(demo_service.VALUES_RESPONSE)
    mutated["measurements"][0]["value"] = 999.0
    monkeypatch.setattr(demo_service, "VALUES_RESPONSE", mutated)

    resumed = runner.run(space)
    assert resumed.mode == "resumed-after-exit"
    assert resumed.end_state == "awaiting-decision"
    values_exec = next(ex for ex in resumed.executions if ex.step == "extract_values")
    assert not values_exec.reused
    assert values_exec.billed_before == 1  # 数值步骤重新调用，重复计费
    assert resumed.billing["calls"] == 3
    prompt = resumed.pending_question["prompt"]
    assert "580±20" in prompt and "999" not in prompt  # 应答来自快照，不是全局数据
    assert any("材料摘要" in line for line in resumed.lines())  # 有分歧提示

    decided = runner.decide(space, "merge")
    assert decided.completed
    assert decided.summary["object_groups"] == _MERGE_GROUPS
    assert all(m["value"] != 999.0 for m in decided.summary["all_measurements"])


def test_pending_question_describes_actual_evidence(tmp_path, monkeypatch):
    """证据不一致时，问题正文如实描述“速度测量不同”并展示每条测量。"""
    mutated = copy.deepcopy(demo_service.VALUES_RESPONSE)
    mutated["measurements"][0]["value"] = 999.0
    monkeypatch.setattr(demo_service, "VALUES_RESPONSE", mutated)

    report = runner.run(tmp_path / "space")
    prompt = report.pending_question["prompt"]
    assert "速度测量不同" in prompt
    assert "999±20 km/s（来源：当前论文）" in prompt
    assert "580±20 km/s（来源：当前论文）" in prompt


def test_reask_when_saved_result_diverges_from_paused_question(tmp_path):
    """业务库结果与暂停时的问题不一致：不应用旧决定，按当前证据重问。

    这是重问机制的防御测试：正常流程中材料快照使重建的问题与暂停时
    一致；当已保存结果被外部改动（与暂停时证据不符）时，恢复提交的
    决定所依据的问题摘要与重建结果不匹配，机制应拒绝应用并重问。
    """
    space = tmp_path / "space"
    runner.run(space)
    paused_digest = runner.status(space).pending_question["question_digest"]

    tampered = copy.deepcopy(demo_service.VALUES_RESPONSE)
    tampered["measurements"][0]["value"] = 999.0
    conn = sqlite3.connect(space / runner.BUSINESS_DB)
    conn.execute(
        "UPDATE results SET output_json = ? WHERE op_id = ?",
        (json.dumps(tampered, ensure_ascii=False), demo_graph.STEP_VALUES),
    )
    conn.commit()
    conn.close()

    stale = runner.decide(space, "merge")
    assert stale.decision_outcome == "saved"
    assert not stale.completed
    assert stale.superseded  # 旧决定基于暂停时的问题，未被应用
    assert any("未被应用" in line for line in stale.lines())

    new_question = runner.status(space).pending_question
    assert new_question["question_digest"] != paused_digest
    assert "速度测量不同" in new_question["prompt"]  # 重问按当前证据如实描述
    assert "999±20" in new_question["prompt"]

    final = runner.decide(space, "merge")
    assert final.decision_outcome == "saved"  # 新版本问题的决定另行保存
    assert final.completed
    assert any(m["value"] == 999.0 for m in final.summary["all_measurements"])
    digests = {item.question_digest for item in runner.status(space).decisions}
    assert digests == {paused_digest, new_question["question_digest"]}


def test_question_digest_covers_evidence_and_choices():
    """问题摘要覆盖实际证据与选项（首轮验收探针回归）。

    相同操作输入下证据变化，或相同证据下输入或选项变化，都必须得到
    不同摘要——否则摘要无法识别基于旧证据、旧选项的决定。
    """
    op_input = {
        "step": demo_graph.STEP_VALUES,
        "paper_id": demo_service.PAPER_ID,
        "rules": "demo-rules-v1",
        "model": demo_service.MODEL_NAME,
        "roster_names": ["J1234+5678", "J1234+5679", "J0456+7890"],
    }
    payload = copy.deepcopy(demo_service.VALUES_RESPONSE)
    base = demo_graph.build_question(op_input, payload)

    changed_evidence = copy.deepcopy(payload)
    changed_evidence["measurements"][0]["value"] = 999.0
    assert (
        demo_graph.build_question(op_input, changed_evidence)["question_digest"]
        != base["question_digest"]
    )

    changed_input = dict(op_input, rules="demo-rules-v2")
    assert (
        demo_graph.build_question(changed_input, payload)["question_digest"]
        != base["question_digest"]
    )


def test_question_digest_changes_with_choices(monkeypatch):
    op_input = {"step": demo_graph.STEP_VALUES, "rules": "demo-rules-v1"}
    payload = copy.deepcopy(demo_service.VALUES_RESPONSE)
    base = demo_graph.build_question(op_input, payload)
    monkeypatch.setattr(demo_graph, "CHOICES", ("merge", "keep-separate", "other"))
    assert (
        demo_graph.build_question(op_input, payload)["question_digest"]
        != base["question_digest"]
    )


def test_input_fingerprint_separates_executions():
    base = {"step": "extract_roster", "paper": "demo-001", "rules": "v1"}
    _, same = demo_graph.fingerprint(dict(base))
    _, again = demo_graph.fingerprint(dict(base))
    assert same == again
    changed = dict(base, rules="v2")
    assert demo_graph.fingerprint(changed)[1] != same
    assert demo_graph.fingerprint(dict(base, model="other"))[1] != same


def test_reset_clears_space(tmp_path):
    space = tmp_path / "space"
    runner.run(space)
    runner.reset(space)
    status = runner.status(space)
    assert not status.started
    assert status.results == []
    assert status.billing["calls"] == 0
