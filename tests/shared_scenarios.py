"""Integration scenarios use the registered extraction flow, not a second kernel.

Only input shorthand and low-level submission construction live here. Independent
runtime adapters are in tiny_flow.py. Subprocess tests import this test helper.
"""

from hvs_catalog.pipeline import ExtractionRequest, StepModel, mock, start_extraction
from hvs_catalog.pipeline.flow import ExtractionFlow
from hvs_catalog.pipeline.methods import ROSTER_METHOD
from hvs_catalog.records.common import RecordRef
from hvs_catalog.records.questions import Actor
from hvs_catalog.review import DecisionSubmission, ReviewService
from hvs_catalog.runtime import get_execution
from hvs_catalog.synthetic import material as inputs

MODEL_NAME = "synthetic-mock"


def direct_flow(*, fault_hooks=None):
    return ExtractionFlow(ROSTER_METHOD, mode="synthetic", fault_hooks=fault_hooks)


def import_materials(space, paper):
    return mock.import_mock_source(space, inputs.PAPER_KEYS[paper])


def start_execution(
    space, *, paper="A", request_key=None, expert_adjudication=True, fault_hooks=None
):
    source = import_materials(space, paper)
    handle = start_extraction(
        space,
        request=ExtractionRequest(
            source_refs=[source],
            method="roster",
            mode="synthetic",
            models=[StepModel(model=MODEL_NAME)],
            expert_adjudication=expert_adjudication,
        ),
        request_key=request_key or f"scenario:{paper}:{expert_adjudication}",
    )
    return (
        get_execution(space, handle.execution_id),
        direct_flow(fault_hooks=fault_hooks),
        handle.reused,
    )


def open_service(path):
    return mock.PipelineMockService(path)


def pending_questions(space, execution_id=None):
    return ReviewService(space).list_questions(execution_id=execution_id)


def submit_expert_decision(
    space,
    *,
    execution_id,
    question_ref,
    request_key,
    outcome="resolved",
    patch="default",
    answer_original="无总体结论：作者在两种模型下给出相反结论且未作总体判断。",
    answer_en="No overall conclusion: the author gives opposite conditional conclusions and defers an overall assessment.",
    reason="Synthetic expert decision fixed in the pipeline mock materials.",
):
    review = ReviewService(space)
    view = review.get_question(question_ref)
    return review.submit_decision(
        execution_id=execution_id,
        step_id=view.step_id,
        request_key=request_key,
        submission=DecisionSubmission(
            question_ref=question_ref,
            fingerprint=view.record.data.fingerprint,
            actor=Actor(kind="human", id=mock.PIPELINE_EXPERT_ID),
            outcome=outcome,
            answer_original=answer_original,
            answer_en=answer_en,
            reason=reason,
            evidence_refs=[],
            patch=(
                material.expert_decision_patch(
                    view.record.data.target_refs[0],
                    evidence_ref=view.record.data.evidence_refs[0],
                )
                if patch == "default" and outcome == "resolved"
                else None
                if patch == "default"
                else patch
            ),
        ),
    )


class ScenarioMaterial:
    SOURCE_IDS = inputs.SOURCE_IDS

    @staticmethod
    def paper_record_id(paper, execution_id):
        return mock.paper_record_id(inputs.PAPER_KEYS[paper], execution_id)

    @staticmethod
    def roster_record_id(paper, execution_id):
        return mock.roster_record_id(inputs.PAPER_KEYS[paper], execution_id)

    @staticmethod
    def object_record_id(paper, execution_id):
        return mock.object_record_id(
            inputs.PAPER_KEYS[paper], inputs.OBJECT_NAMES[paper], execution_id
        )

    @staticmethod
    def question_content(paper, *, source_ref, rules, paper_id):
        return mock.author_assessment_question(
            inputs.PAPER_KEYS[paper], paper_id=paper_id, source=source_ref, rules=rules
        )

    @staticmethod
    def expert_decision_patch(target, *, evidence_ref: RecordRef):
        return mock.expert_patch_for(
            "author_assessment", target=target, evidence=evidence_ref
        )


material = ScenarioMaterial()
