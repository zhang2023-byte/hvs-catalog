"""LangGraph 演示流程：名单 → 数值 →（对象身份裁决中断）→ 汇总。

运行固定输入（规则版本、材料快照、模型）在流程开始时写入状态并随
检查点保存；恢复时节点一律使用状态中的固定输入重新计算，不随当前
代码或材料的变化而改变，对应设计规则“恢复时使用原来保存的输入；
输入改变后，记录为一次新的执行”。材料快照是完整的实际合成输入：
即使响应尚未保存就退出，恢复重新调用服务也按快照应答，不会把修改
后的全局数据登记到旧输入之下。

恢复与重入：``Command(resume=...)`` 与崩溃后续跑都会让所在节点从头
重新执行，节点先查业务数据库，输入相同即复用已保存结果、不再调用
服务。裁决问题由固定输入与实际证据确定性重建，摘要覆盖证据与选项，
正文按当前证据生成并展示对象、测量与来源；恢复载荷中的问题摘要与
重建结果不一致时，说明决定基于旧版本的问题，节点不应用该决定，
而是按当前问题重新提问。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from hvs_catalog.demo import service as demo_service
from hvs_catalog.demo import store as demo_store

RULES_VERSION = "demo-rules-v1"

STEP_ROSTER = "extract_roster"
STEP_VALUES = "extract_values"
STEP_FINALIZE = "finalize"

#: 演示中唯一的裁决问题：两名疑似重复记录的候选是否为同一天体。
#: 决定只影响演示摘要的对象分组，不挑选任何测量值。
QUESTION_ID = f"{demo_service.PAPER_ID}:object-identity"
CHOICES = ("merge", "keep-separate")


class DemoState(TypedDict, total=False):
    paper_id: str
    rules_version: str
    material: dict
    model: str
    roster: list[dict]
    values: dict
    question_id: str
    question_digest: str
    choice: str
    summary: dict


def initial_state() -> DemoState:
    """构造新运行的初始状态，固定本次运行的规则、材料快照与模型。"""
    return {
        "paper_id": demo_service.PAPER_ID,
        "rules_version": RULES_VERSION,
        "material": demo_service.default_material(),
        "model": demo_service.MODEL_NAME,
    }


def fingerprint(parts: dict) -> tuple[str, str]:
    """返回操作输入的规范 JSON 与摘要；输入变化即得到不同摘要。"""
    canonical = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return canonical, digest


@dataclass
class StepExecution:
    """一个服务步骤在本进程内的执行方式。

    ``reused`` 为真表示输入相同、复用了业务库已保存结果（未调用服务）；
    ``billed_before`` 是本次实际调用前同一请求已被计费的次数，大于零
    说明出现了“收到响应但尚未保存就退出”造成的重复调用。
    """

    step: str
    reused: bool
    billed_before: int


def question_evidence(values_payload: dict) -> dict:
    """取出裁决问题展示的实际证据：涉及的对象与它们的测量记录。"""
    needs = values_payload["needs_decision"]
    objects = list(needs["objects"])
    measurements = [
        item for item in values_payload["measurements"] if item["object"] in objects
    ]
    return {"objects": objects, "measurements": measurements}


def describe_measurements(measurements: list[dict]) -> str:
    """把测量记录描述为带对象、数值与来源的正文。"""
    return "；".join(
        f"{item['object']}：{item['value']:.0f}±{item['error']:.0f} {item['unit']}"
        f"（来源：{item['source']}）"
        for item in measurements
    )


def build_question(values_op_input: dict, values_payload: dict) -> dict:
    """根据数值步骤的固定输入与实际证据，确定性构造裁决问题。

    问题摘要覆盖：问题标识、数值步骤输入摘要、实际证据（涉及对象与
    测量记录）和选项。证据或选项变化都会得到不同的摘要，构成另一个
    版本的问题。正文按当前证据生成：测量是否相同由证据本身决定，
    并逐条展示对象、测量与来源。
    """
    _, input_digest = fingerprint(values_op_input)
    evidence = question_evidence(values_payload)
    _, question_digest = fingerprint(
        {
            "question_id": QUESTION_ID,
            "values_input_digest": input_digest,
            "evidence": evidence,
            "choices": list(CHOICES),
        }
    )
    distinct = (
        len({(m["value"], m["error"], m["unit"]) for m in evidence["measurements"]}) > 1
    )
    relation = "速度测量不同" if distinct else "速度测量完全相同"
    names = " 与 ".join(evidence["objects"])
    prompt = (
        f"候选 {names} {relation}且命名相邻"
        f"（证据：{describe_measurements(evidence['measurements'])}），"
        "是否识别为同一天体？决定只影响演示摘要的对象分组，全部测量与来源保留。"
    )
    return {
        "question_id": QUESTION_ID,
        "question_digest": question_digest,
        "prompt": prompt,
        "choices": list(CHOICES),
        "evidence": evidence,
    }


def build_graph(
    store: demo_store.BusinessStore,
    service: demo_service.MockModelService,
    checkpointer: BaseCheckpointSaver,
    *,
    after_response: Callable[[str], None] | None = None,
    executions: list[StepExecution] | None = None,
):
    """编译演示图。

    ``after_response`` 在“收到服务响应之后、保存结果之前”被调用，
    用于注入演示用的进程退出或异常。``executions`` 记录本进程内每个
    服务步骤的执行方式，供运行器报告复用与重复调用。
    """
    log = executions if executions is not None else []

    def run_service_step(
        step: str, op_input: dict, request: dict, material: dict
    ) -> dict:
        canonical, input_digest = fingerprint(op_input)
        saved = store.find_result(step, input_digest)
        if saved is not None:
            log.append(StepExecution(step, reused=True, billed_before=0))
            return saved
        billed_before = service.billed_calls(request)
        call = service.call(request, material)
        if after_response is not None:
            after_response(step)
        store.save_result(step, input_digest, canonical, call.response)
        log.append(StepExecution(step, reused=False, billed_before=billed_before))
        return call.response

    def extract_roster(state: DemoState) -> dict:
        paper_digest = demo_service.material_digest(state["material"])
        op_input = {
            "step": STEP_ROSTER,
            "paper_id": state["paper_id"],
            "paper_digest": paper_digest,
            "rules": state["rules_version"],
            "model": state["model"],
        }
        payload = run_service_step(
            STEP_ROSTER,
            op_input,
            service.build_request(
                "roster",
                rules=state["rules_version"],
                model=state["model"],
                paper_digest=paper_digest,
            ),
            state["material"],
        )
        return {"roster": payload["objects"]}

    def extract_values(state: DemoState) -> dict:
        paper_digest = demo_service.material_digest(state["material"])
        roster_names = [item["name"] for item in state["roster"]]
        op_input = {
            "step": STEP_VALUES,
            "paper_id": state["paper_id"],
            "paper_digest": paper_digest,
            "rules": state["rules_version"],
            "model": state["model"],
            "roster_names": roster_names,
        }
        payload = run_service_step(
            STEP_VALUES,
            op_input,
            service.build_request(
                "values",
                rules=state["rules_version"],
                model=state["model"],
                paper_digest=paper_digest,
                roster_names=roster_names,
            ),
            state["material"],
        )
        needs = payload.get("needs_decision")
        if not needs:
            return {"values": payload}
        question = build_question(op_input, payload)
        answer = interrupt(question)
        while answer.get("question_digest") != question["question_digest"]:
            # 提交的决定基于旧版本的问题（证据或输入已变化）：不应用，
            # 按当前重建的问题重新提问，直到收到与当前问题匹配的决定
            answer = interrupt(question)
        if answer.get("question_id") != QUESTION_ID:
            raise ValueError("恢复的裁决内容与当前待裁决问题不一致")
        choice = answer.get("choice")
        if choice not in CHOICES:
            raise ValueError(f"无效的裁决选择：{choice!r}")
        return {
            "values": payload,
            "question_id": QUESTION_ID,
            "question_digest": question["question_digest"],
            "choice": choice,
        }

    def finalize(state: DemoState) -> dict:
        values = state["values"]
        needs = values["needs_decision"]
        roster_names = [item["name"] for item in state["roster"]]
        merged = set(needs["objects"])
        if state["choice"] == "merge":
            groups = [sorted(merged)] + [
                [name] for name in roster_names if name not in merged
            ]
        else:
            groups = [[name] for name in roster_names]
        summary = {
            "paper_id": state["paper_id"],
            "question_id": state["question_id"],
            "question_digest": state["question_digest"],
            "choice": state["choice"],
            "object_groups": groups,
            "all_measurements": values["measurements"],
            "note": "决定只影响演示摘要的对象分组；全部测量与来源原样保留。",
        }
        # 同一论文的汇总只写一次：节点重入也不会产生第二份汇总
        store.save_summary(state["paper_id"], summary)
        return {"summary": summary}

    builder = StateGraph(DemoState)
    builder.add_node(STEP_ROSTER, extract_roster)
    builder.add_node(STEP_VALUES, extract_values)
    builder.add_node(STEP_FINALIZE, finalize)
    builder.add_edge(START, STEP_ROSTER)
    builder.add_edge(STEP_ROSTER, STEP_VALUES)
    builder.add_edge(STEP_VALUES, STEP_FINALIZE)
    builder.add_edge(STEP_FINALIZE, END)
    return builder.compile(checkpointer=checkpointer)
