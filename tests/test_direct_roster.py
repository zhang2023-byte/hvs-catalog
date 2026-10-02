"""Prepared-material direct extraction: science fixtures, calls and recovery."""

from __future__ import annotations

import copy
import json
import subprocess
import sys

import pytest
from llm_helpers import completion, endpoint
from materials_helpers import FakeConverterRunner, make_tar_gz, prepared_space
from pydantic import SecretStr

from hvs_catalog.llm import RuntimeConfig, TokenDanceAdapter
from hvs_catalog.llm.managed import CallService
from hvs_catalog.llm.transport import TransportFailure
from hvs_catalog.materials import MaterialsService
from hvs_catalog.pipeline import (
    ExtractionConfig,
    get_extraction,
    resume_extraction,
    start_roster_extraction,
)
from hvs_catalog.records.common import RecordRef
from hvs_catalog.records.paper import PaperRecord
from hvs_catalog.records.questions import Actor, DecisionPatch, PatchChange
from hvs_catalog.records.roster import RosterRecord
from hvs_catalog.review import ReviewService
from hvs_catalog.review.service import DecisionSubmission
from hvs_catalog.runtime import get_execution
from hvs_catalog.workspace import (
    OperationKeyConflictError,
    RecoveryInconsistentError,
    open_workspace,
)

TEXT = """\\documentclass{article}
\\begin{document}
This paper independently selects A and concludes A is unbound from the Galaxy.
This paper selects B; under the single model, B is likely unbound from the Galaxy.
This paper selects C and favors a bound interpretation, but still accepts C as an unbound candidate under M2.
This paper selects D, then withdraws its candidacy and concludes D is bound.
This paper reobserves historical unbound candidate E and now concludes E is bound.
This paper obtains a new spectrum of historical unbound candidate F without discussing Galactic boundness.
This paper selects G, accepts bound and unbound models, and explicitly gives no overall conclusion for G.
H occurs only in a table labeled HVS; that label is not defined as Galactic unbound here.
This paper independently rediscovers previously named I and still accepts I as unbound.
This paper selects J and follows it up spectroscopically within this discovery study; J remains unbound.
The author summaries for K contradict one another and leave K's eligibility ambiguous.
The authors accept L as an unbound candidate, but their overall preference for L is ambiguous.
The authors say some members of stars 1--5 are unbound but do not identify them.
\\end{document}
"""


def evidence(line):
    return {
        "source": {
            "file_id": "member:main.tex",
            "locator": {"kind": "text", "start_line": line, "end_line": line},
            "excerpt": TEXT.splitlines()[line - 1],
        },
        "parts": [],
        "context": True,
    }


def entry(
    name,
    line,
    status=None,
    contribution_type="candidates_found",
    disposition="included",
):
    ev = evidence(line)
    result = {
        "identifiers": [{"value": name, "evidence": [ev]}],
        "disposition": disposition,
        "evidence": [ev],
    }
    if disposition == "included":
        result["contribution"] = {
            "summary": {"text": "This paper studies " + name + ".", "evidence": [ev]},
            "contribution_type": {"value": contribution_type, "evidence": [ev]},
            "boundness": {"status": status, "evidence": [ev]},
            "conditional_assessments": [],
        }
    else:
        if disposition == "pending":
            result["question"] = {
                "category": "inclusion",
                "prompt": "Is K still an eligible unbound candidate?",
                "evidence": [ev],
            }
    return result


def submission(entries=None):
    return {
        "coverage": {"state": "complete", "checked_file_ids": ["member:main.tex"]},
        "entries": entries if entries is not None else [entry("A", 3, "unbound")],
        "unresolved_groups": [],
    }


class ScriptedAdapter(TokenDanceAdapter):
    def __init__(self, responses):
        self.responses = responses
        self.seen = []

    def send(self, payload, runtime, credential):
        self.seen.append(copy.deepcopy(payload))
        response = self.responses[min(len(self.seen) - 1, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        if response == "truncated":
            finish, arguments = "length", "{}"
        else:
            finish = "tool_calls"
            arguments = response if isinstance(response, str) else json.dumps(response)
        body = {
            "id": "synthetic-call",
            "choices": [
                {
                    "finish_reason": finish,
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": payload["tools"][0]["function"]["name"],
                                    "arguments": arguments,
                                },
                            }
                        ]
                    },
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        }
        return {
            "status": 200,
            "headers": {},
            "body": body,
            "text": json.dumps(body),
            "events": [],
        }


@pytest.fixture
def prepared(tmp_path):
    space, imported = prepared_space(
        tmp_path,
        make_tar_gz(
            {
                "main.tex": TEXT.encode(),
            }
        ),
    )
    materials = MaterialsService(
        space,
        runner=FakeConverterRunner(),
    )
    preparation, report = materials.prepare_materials(
        request_key="prepare", paper=imported.canonical_id, root_file="main.tex"
    )
    assert report.outcome == "finished", report
    runtime = RuntimeConfig(quota_store=tmp_path / "quota.sqlite")
    config = ExtractionConfig(runtime=runtime)
    yield space, preparation.execution_id, config
    space.close()


def run(prepared, payloads, *, config=None, key="extract", hooks=None):
    space, prep, default = prepared
    config = config or default
    handle = start_roster_extraction(
        space, preparation_execution_id=prep, configuration=config, request_key=key
    )
    adapter = ScriptedAdapter(payloads)
    service = CallService(
        config.runtime,
        credential=SecretStr("synthetic-key"),
        adapter=adapter,
        hooks=hooks,
    )
    report = resume_extraction(space, handle.execution_id, service=service)
    return handle, report, adapter, service


def results(space, execution_id):
    status = get_extraction(space, execution_id)
    records = [
        space.store().read_record(RecordRef.model_validate(ref))
        for ref in status["result_refs"]
    ]
    return next(r for r in records if isinstance(r, RosterRecord)), next(
        r for r in records if isinstance(r, PaperRecord)
    )


def test_science_examples_five_states_and_contribution_types(prepared):
    entries = [
        entry("A", 3, "unbound"),
        entry("B", 4, "possibly_unbound"),
        entry("C", 5, "possibly_bound"),
        entry("D", 6, disposition="excluded"),
        entry("E", 7, "bound", "follow_up"),
        entry("F", 8, "no_overall_conclusion", "follow_up"),
        entry("G", 9, "no_overall_conclusion"),
        entry("H", 10, disposition="excluded"),
        entry("I", 11, "unbound"),
        entry("J", 12, "unbound"),
    ]
    handle, report, adapter, service = run(prepared, [submission(entries)])
    assert report.outcome == "finished", report.errors
    roster, paper = results(prepared[0], handle.execution_id)
    assert [
        (c.identifiers[0].value, c.contribution_type.value, c.paper_boundness.status)
        for c in paper.data.object_contributions
    ] == [
        ("A", "candidates_found", "unbound"),
        ("B", "candidates_found", "possibly_unbound"),
        ("C", "candidates_found", "possibly_bound"),
        ("E", "follow_up", "bound"),
        ("F", "follow_up", "no_overall_conclusion"),
        ("G", "candidates_found", "no_overall_conclusion"),
        ("I", "candidates_found", "unbound"),
        ("J", "candidates_found", "unbound"),
    ]
    assert [
        e.identifiers[0].value
        for e in roster.data.entries
        if e.disposition == "excluded"
    ] == ["D", "H"]
    assert all(
        v.state == "not_checked"
        for c in paper.data.object_contributions
        for v in c.quantity_coverage
    )
    assert len(adapter.seen) == 1
    assert "draft.tex" not in adapter.seen[0]["messages"][1]["content"]
    assert "figure.png" not in adapter.seen[0]["messages"][1]["content"]
    assert (
        resume_extraction(prepared[0], handle.execution_id, service=service).outcome
        == "finished"
    )
    assert len(adapter.seen) == 1


@pytest.mark.parametrize(
    "responses, calls, expected",
    [
        (["truncated"], 1, "failed"),
        (["{bad", "{bad", "{bad"], 3, "failed"),
        (["{bad", submission()], 2, "finished"),
    ],
)
def test_bounded_format_correction_and_truncation(prepared, responses, calls, expected):
    handle, report, adapter, service = run(prepared, responses)
    assert report.outcome == expected, report.errors
    assert len(adapter.seen) == calls
    assert (
        resume_extraction(prepared[0], handle.execution_id, service=service).outcome
        == expected
    )
    assert len(adapter.seen) == calls
    assert (
        not get_extraction(prepared[0], handle.execution_id)["result_refs"]
        if expected == "failed"
        else True
    )


def test_successful_complete_empty_roster_has_explicit_coverage(prepared):
    handle, report, _, _ = run(prepared, [submission([])])
    assert report.outcome == "finished", report.errors
    roster, paper = results(prepared[0], handle.execution_id)
    assert roster.data.entries == [] and roster.data.coverage.state == "complete"
    assert paper.data.object_contributions == []


def test_material_rendering_is_frozen_and_attachments_verified(prepared, monkeypatch):
    space, prep, config = prepared
    handle = start_roster_extraction(
        space, preparation_execution_id=prep, configuration=config, request_key="freeze"
    )
    from hvs_catalog.pipeline import roster as module

    monkeypatch.setattr(
        module,
        "material_prompt",
        lambda *_: (_ for _ in ()).throw(
            AssertionError("do not regenerate a saved prompt")
        ),
    )
    adapter = ScriptedAdapter([submission()])
    service = CallService(
        config.runtime, credential=SecretStr("synthetic-key"), adapter=adapter
    )
    assert (
        resume_extraction(space, handle.execution_id, service=service).outcome
        == "finished"
    )
    assert (
        "This paper independently selects A"
        in adapter.seen[0]["messages"][1]["content"]
    )
    snapshot = get_execution(space, handle.execution_id).input_snapshot
    source = space.store().read_record(snapshot.source_refs[0])
    file = next(f for f in source.data.files if f.file_id == "member:main.tex")
    path = space.root / file.path
    original = path.read_bytes()
    path.write_bytes(original + b"changed material")
    with pytest.raises(RecoveryInconsistentError, match="哈希改变"):
        resume_extraction(space, handle.execution_id, service=service)
    path.write_bytes(original)
    assert len(adapter.seen) == 1


def test_unreferenced_draft_and_binary_are_omitted_but_missing_input_is_retained(
    tmp_path,
):
    space, imported = prepared_space(
        tmp_path,
        make_tar_gz(
            {
                "main.tex": TEXT.replace(
                    "\\end{document}", "\\input{missing}\n\\end{document}"
                ).encode(),
                "draft.tex": b"\\documentclass{article}\n\\begin{document}An unrelated obsolete draft.\\end{document}",
                "figure.png": b"\x89PNG\r\n\x1a\n",
            }
        ),
    )
    try:
        prep, report = MaterialsService(
            space, runner=FakeConverterRunner()
        ).prepare_materials(
            request_key="prepare", paper=imported.canonical_id, root_file="main.tex"
        )
        assert report.outcome == "finished"
        bundle = MaterialsService(space).get_prepared_input(prep.execution_id)
        source = space.store().read_record(
            RecordRef.model_validate(bundle["prepared_source_ref"])
        )
        payload = submission()
        payload["coverage"].update(
            state="partial", gap_ids=[g.gap_id for g in source.data.gaps]
        )
        config = ExtractionConfig(
            runtime=RuntimeConfig(quota_store=tmp_path / "quota.db")
        )
        handle, report, adapter, _ = run((space, prep.execution_id, config), [payload])
        assert report.outcome == "finished", report.errors
        material = adapter.seen[0]["messages"][1]["content"]
        assert "An unrelated obsolete draft" not in material
        assert "FILE member:figure.png" not in material
        roster, _ = results(space, handle.execution_id)
        assert roster.data.coverage.state == "partial" and roster.data.coverage.gap_refs
    finally:
        space.close()


@pytest.mark.parametrize(
    "case",
    [
        "invented_excerpt",
        "wrong_line",
        "unknown_file",
        "duplicate_identifier",
        "bound_new_candidate",
        "unsupplied_coverage",
    ],
)
def test_invalid_evidence_or_science_relationship_is_not_committed(prepared, case):
    payload = submission()
    if case == "invented_excerpt":
        payload["entries"][0]["evidence"][0]["source"]["excerpt"] = "An invented claim"
    elif case == "wrong_line":
        payload["entries"][0]["evidence"][0]["source"]["locator"]["start_line"] = 999
        payload["entries"][0]["evidence"][0]["source"]["locator"]["end_line"] = 999
    elif case == "unknown_file":
        payload["entries"][0]["evidence"][0]["source"]["file_id"] = "other-paper.tex"
    elif case == "duplicate_identifier":
        payload["entries"].append(copy.deepcopy(payload["entries"][0]))
    elif case == "bound_new_candidate":
        payload["entries"][0]["contribution"]["boundness"]["status"] = "bound"
    else:
        bundle = MaterialsService(prepared[0]).get_prepared_input(prepared[1])
        source = (
            prepared[0]
            .store()
            .read_record(RecordRef.model_validate(bundle["prepared_source_ref"]))
        )
        payload["coverage"]["checked_file_ids"] = [
            next(f.file_id for f in source.data.files if f.role == "raw_package")
        ]
    handle, report, adapter, _ = run(prepared, [payload])
    assert report.outcome == "failed", report
    assert len(adapter.seen) == 1
    assert get_extraction(prepared[0], handle.execution_id)["result_refs"] == []


def test_unknown_completion_is_not_resent(prepared):
    handle, report, adapter, service = run(
        prepared, [TransportFailure("completion_unknown", uncertain=True)]
    )
    assert report.outcome == "failed"
    assert (
        resume_extraction(prepared[0], handle.execution_id, service=service).outcome
        == "failed"
    )
    assert len(adapter.seen) == 1


def test_archived_response_reused_after_interruption(prepared):
    def fail():
        raise RuntimeError("synthetic interruption after archival")

    handle, report, adapter, service = run(
        prepared, [submission()], hooks={"after_archive": fail}
    )
    assert report.outcome == "failed"
    service.hooks.clear()
    assert (
        resume_extraction(prepared[0], handle.execution_id, service=service).outcome
        == "finished"
    )
    assert len(adapter.seen) == 1


def test_two_questions_are_bound_and_pending_preserved(prepared):
    pending = entry("K", 13, disposition="pending")
    included = entry("L", 14, "unbound", "follow_up")
    included["contribution"]["boundness"] = {
        "status": None,
        "question": {
            "category": "author_assessment",
            "prompt": "What is the overall assessment for L?",
            "evidence": [evidence(14)],
        },
    }
    handle, report, adapter, service = run(prepared, [submission([pending, included])])
    assert report.outcome == "paused", report.errors
    review = ReviewService(prepared[0])
    questions = review.list_questions(execution_id=handle.execution_id)
    assert len(questions) == 2
    assert all(q.step_id == "direct_roster" for q in questions)
    roster, paper = results(prepared[0], handle.execution_id)
    assert roster.data.entries[0].disposition == "pending"
    assert paper.data.object_contributions[0].paper_boundness.status is None
    q = questions[0]
    review.submit_decision(
        execution_id=handle.execution_id,
        step_id=q.step_id,
        request_key="unresolved",
        submission=DecisionSubmission(
            question_ref=q.question_ref,
            fingerprint=q.record.data.fingerprint,
            actor=Actor(kind="human", id="synthetic-expert"),
            outcome="unresolved",
            answer_original="Keep pending",
            answer_en="Keep pending",
            reason="The evidence remains ambiguous.",
        ),
    )
    assert (
        resume_extraction(prepared[0], handle.execution_id, service=service).outcome
        == "paused"
    )
    assert len(adapter.seen) == 1


def test_question_batch_saved_before_step_interruption_is_reused(prepared, monkeypatch):
    original = ReviewService.create_questions

    def save_then_fail(self, **kwargs):
        original(self, **kwargs)
        raise RuntimeError("synthetic interruption after atomic question batch")

    monkeypatch.setattr(ReviewService, "create_questions", save_then_fail)
    payload = submission(
        [entry("K", 13, disposition="pending"), entry("L", 14, disposition="pending")]
    )
    handle, report, adapter, service = run(prepared, [payload])
    assert report.outcome == "failed"
    review = ReviewService(prepared[0])
    assert len(review.list_questions(execution_id=handle.execution_id)) == 2
    monkeypatch.setattr(ReviewService, "create_questions", original)
    assert (
        resume_extraction(prepared[0], handle.execution_id, service=service).outcome
        == "paused"
    )
    assert len(review.list_questions(execution_id=handle.execution_id)) == 2
    assert len(adapter.seen) == 1


def test_changed_same_key_configuration_rejected(prepared):
    space, prep, config = prepared
    start_roster_extraction(
        space, preparation_execution_id=prep, configuration=config, request_key="key"
    )
    changed = config.model_copy(update={"examples": {"direct_roster": []}})
    with pytest.raises(OperationKeyConflictError):
        start_roster_extraction(
            space,
            preparation_execution_id=prep,
            configuration=changed,
            request_key="key",
        )


@pytest.mark.parametrize("changed", [False, True])
def test_format_correction_preserves_existing_scientific_fields(prepared, changed):
    invalid = submission()
    invalid["entries"][0]["unknown_field"] = "Remove this field."
    corrected = submission()
    if changed:
        corrected["entries"][0]["contribution"]["boundness"]["status"] = (
            "possibly_unbound"
        )
    handle, report, adapter, _ = run(prepared, [invalid, corrected])
    assert len(adapter.seen) == 2
    assert report.outcome == ("failed" if changed else "finished"), report.errors
    assert bool(get_extraction(prepared[0], handle.execution_id)["result_refs"]) == (
        not changed
    )


def test_correction_reports_missing_source_wrapper_and_preserves_its_facts(prepared):
    invalid = submission()
    ev = invalid["entries"][0]["evidence"][0]
    ev.update(ev.pop("source"))
    _handle, report, adapter, _ = run(prepared, [invalid, submission()])
    assert report.outcome == "finished", report.errors
    assert len(adapter.seen) == 2
    feedback = adapter.seen[1]["messages"][-1]["content"]
    assert "entries.0.evidence.0: required; required fields: source" in feedback


def test_partial_group_and_conditional_judgment_preserved_without_expert(prepared):
    selected = entry("B", 4, "possibly_unbound")
    selected["contribution"]["conditional_assessments"] = [
        {
            "condition": "Under the single model",
            "statement": "Likely unbound from the Galaxy",
            "evidence": [evidence(4)],
        }
    ]
    payload = submission([selected, entry("K", 13, disposition="pending")])
    payload["coverage"]["state"] = "partial"
    payload["unresolved_groups"] = [
        {
            "original_range": "stars 1--5",
            "reason": "The text does not identify which members are unbound.",
            "member_identifier_values": [],
            "evidence": [evidence(15)],
        }
    ]
    config = prepared[2].model_copy(
        update={"mode": "evaluation", "expert_adjudication": False}
    )
    handle, report, adapter, service = run(prepared, [payload], config=config)
    assert report.outcome == "finished", report.errors
    roster, paper = results(prepared[0], handle.execution_id)
    assert roster.data.coverage.state == "partial"
    assert roster.data.entries[1].disposition == "pending"
    assert len(roster.data.unresolved_groups) == 1
    assert (
        paper.data.object_contributions[0].conditional_assessments[0].condition
        == "Under the single model"
    )
    assert (
        get_extraction(prepared[0], handle.execution_id)["questions"][0]["status"]
        == "open"
    )
    judgments = get_extraction(prepared[0], handle.execution_id)["model_judgments"]
    assert len(judgments) == 1
    assert json.loads(judgments[0]["judgment"])["kind"] == "roster_model_submission"
    assert (
        resume_extraction(prepared[0], handle.execution_id, service=service).outcome
        == "finished"
    )
    assert len(get_extraction(prepared[0], handle.execution_id)["model_judgments"]) == 1
    assert len(adapter.seen) == 1


def test_resolved_assessment_applies_once_and_resumes_without_model(prepared):
    selected = entry("L", 14, "unbound", "follow_up")
    selected["contribution"]["boundness"] = {
        "status": None,
        "question": {
            "category": "author_assessment",
            "prompt": "What is the author preference for L?",
            "evidence": [evidence(14)],
        },
    }
    handle, report, adapter, service = run(prepared, [submission([selected])])
    assert report.outcome == "paused", report.errors
    review = ReviewService(prepared[0])
    question = review.list_questions(execution_id=handle.execution_id)[0]
    target = question.record.data.target_refs[0]
    decision = DecisionSubmission(
        question_ref=question.question_ref,
        fingerprint=question.record.data.fingerprint,
        actor=Actor(kind="human", id="synthetic-expert"),
        outcome="resolved",
        answer_original="无总体倾向。",
        answer_en="No overall preference.",
        reason="Synthetic expert confirmation of the ambiguous author statement.",
        patch=DecisionPatch(
            target=target,
            changes=[
                PatchChange(
                    field="paper_boundness",
                    value={
                        "status": "no_overall_conclusion",
                        "evidence_refs": question.record.data.evidence_refs,
                        "issue_refs": [],
                        "gap_refs": [],
                    },
                )
            ],
        ),
    )
    review.submit_decision(
        execution_id=handle.execution_id,
        step_id=question.step_id,
        request_key="decide",
        submission=decision,
    )
    assert (
        resume_extraction(prepared[0], handle.execution_id, service=service).outcome
        == "finished"
    )
    assert (
        resume_extraction(prepared[0], handle.execution_id, service=service).outcome
        == "finished"
    )
    current = prepared[0].store().read_record_current(target.record_id)
    assert current.revision == 2
    assert (
        current.data.object_contributions[0].paper_boundness.status
        == "no_overall_conclusion"
    )
    assert len(adapter.seen) == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    "hook,calls,outcome",
    [
        ("after_pending", 0, "failed"),
        ("after_response", 1, "failed"),
        ("after_archive", 1, "finished"),
    ],
)
def test_real_process_exit_windows_and_sse_recovery(prepared, hook, calls, outcome):
    def reply(payload, number):
        body = completion(payload)
        body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
            json.dumps(submission())
        )
        return {"body": body}

    space, prep, default = prepared
    with endpoint(reply) as (url, seen, _):
        config = default.model_copy(
            update={"runtime": default.runtime.model_copy(update={"base_url": url})}
        )
        handle = start_roster_extraction(
            space,
            preparation_execution_id=prep,
            configuration=config,
            request_key="process",
        )
        root = space.root
        space.close()
        code = """
import os,sys
from pathlib import Path
from pydantic import SecretStr
from hvs_catalog.llm.managed import CallService
from hvs_catalog.pipeline import resume_extraction
from hvs_catalog.pipeline.configuration import stored_configuration
from hvs_catalog.runtime import get_execution
from hvs_catalog.workspace import open_workspace
space=open_workspace(Path(sys.argv[1]))
config=stored_configuration(get_execution(space,sys.argv[2]).input_snapshot)
service=CallService(config.runtime,credential=SecretStr('synthetic-key'),hooks={sys.argv[3]:lambda:os._exit(70)})
resume_extraction(space,sys.argv[2],service=service)
"""
        child = subprocess.run(
            [sys.executable, "-c", code, str(root), handle.execution_id, hook],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        assert child.returncode == 70, child.stderr
        resumed = open_workspace(root)
        try:
            service = CallService(config.runtime, credential=SecretStr("synthetic-key"))
            report = resume_extraction(resumed, handle.execution_id, service=service)
            assert report.outcome == outcome, report.errors
            assert len(seen) == calls
            if outcome == "finished":
                roster, _ = results(resumed, handle.execution_id)
                assert roster.data.entries[0].identifiers[0].value == "A"
                assert (
                    get_extraction(resumed, handle.execution_id)["usage"]["tokens"]
                    == 10
                )
        finally:
            resumed.close()
