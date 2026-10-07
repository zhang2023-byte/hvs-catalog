"""Prepared-material roster extraction: science fixtures, calls and recovery."""

from __future__ import annotations

import copy
import json
import subprocess
import sys

import pytest
from llm_helpers import completion, endpoint
from materials_helpers import FakeMinerU, make_pdf, prepared_space
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
from hvs_catalog.records.roster import RosterRecord
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
The author summaries for K explicitly accept and withdraw the same candidacy, with no explanation or temporal distinction.
The authors accept L as an unbound candidate, but their summaries say both bound and unbound under the same condition, without reconciliation.
The authors say some members of stars 1--5 are unbound but do not identify them.
\\end{document}
"""


def evidence(line):
    return {"location": f"T:p1-b{line - 1}", "quote": TEXT.splitlines()[line - 1]}


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
        result.update(
            {
                "summary": "This paper studies " + name + ".",
                "summary_evidence": [ev],
                "contribution_type": contribution_type,
                "type_evidence": [ev],
                "boundness": status,
                "boundness_evidence": [ev],
                "conditional_assessments": [],
            }
        )
    else:
        if disposition == "pending":
            result["author_errors"] = [
                {
                    "reason": "The original summaries explicitly accept and withdraw the same candidacy without reconciliation.",
                    "raw_values": [ev["quote"]],
                    "evidence": [ev],
                }
            ]
    return result


def submission(entries=None):
    return {
        "coverage": {"state": "complete", "checked_file_ids": ["raw"]},
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
        make_pdf(TEXT),
    )
    materials = MaterialsService(
        space,
        runner=FakeMinerU(),
    )
    preparation, report = materials.prepare_materials(
        request_key="prepare", paper=imported.canonical_id
    )
    assert report.outcome == "finished", report
    runtime = RuntimeConfig(quota_store=tmp_path / "quota.sqlite")
    config = ExtractionConfig(runtime=runtime, max_format_corrections=2)
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
    file = next(f for f in source.data.files if f.file_id == "raw")
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
    space, imported = prepared_space(tmp_path, make_pdf(TEXT))
    try:
        prep, report = MaterialsService(
            space, runner=FakeMinerU(gaps=["Missing page text"])
        ).prepare_materials(request_key="prepare", paper=imported.canonical_id)
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
        payload["entries"][0]["evidence"][0]["quote"] = "An invented claim"
    elif case == "wrong_line":
        payload["entries"][0]["evidence"][0]["location"] = "T:p999-b0"
    elif case == "unknown_file":
        payload["entries"][0]["evidence"][0]["location"] = "other-paper.tex"
    elif case == "duplicate_identifier":
        payload["entries"].append(copy.deepcopy(payload["entries"][0]))
    elif case == "bound_new_candidate":
        payload["entries"][0]["boundness"] = "bound"
    else:
        bundle = MaterialsService(prepared[0]).get_prepared_input(prepared[1])
        source = (
            prepared[0]
            .store()
            .read_record(RecordRef.model_validate(bundle["prepared_source_ref"]))
        )
        payload["coverage"]["checked_file_ids"] = [
            next(f.file_id for f in source.data.files if f.role == "parser_output")
        ]
    handle, report, adapter, _ = run(prepared, [payload])
    assert report.outcome == "failed", report
    assert len(adapter.seen) == (
        3 if case in {"invented_excerpt", "wrong_line", "unknown_file"} else 1
    )
    if case == "unsupplied_coverage":
        # The invalid global claim is not retained; the independently valid
        # object remains available with no certified material-check declaration.
        roster, paper = results(prepared[0], handle.execution_id)
        assert roster.data.coverage.state == "not_checked"
        assert roster.data.coverage.checked_file_ids == []
        assert [c.identifiers[0].value for c in paper.data.object_contributions] == [
            "A"
        ]
    else:
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


def test_roster_source_errors_bound_and_preserved_without_tasks(prepared):
    pending = entry("K", 13, disposition="pending")
    included = entry("L", 14, "unbound", "follow_up")
    included["boundness"] = None
    included["boundness_author_errors"] = [
        {
            "reason": "The same-condition author summaries give contradictory overall assessments without reconciliation.",
            "raw_values": [evidence(14)["quote"]],
            "evidence": [evidence(14)],
        }
    ]
    handle, report, adapter, service = run(prepared, [submission([pending, included])])
    assert report.outcome == "finished", report.errors
    roster, paper = results(prepared[0], handle.execution_id)
    assert (
        roster.data.entries[0].author_errors and roster.data.coverage.state == "partial"
    )
    assert paper.data.object_contributions[0].paper_boundness.status is None
    status = get_extraction(prepared[0], handle.execution_id)
    assert len(status["diagnostics"]) == 2 and status["questions"] == []
    assert status["science_completion"]["state"] == "partial"
    assert (
        resume_extraction(prepared[0], handle.execution_id, service=service).outcome
        == "finished"
    )
    assert len(adapter.seen) == 1


def test_diagnostic_batch_saved_before_interruption_is_reused(prepared, monkeypatch):
    from hvs_catalog.runtime.execution import RuntimeContext

    original = RuntimeContext.commit_step

    def save_then_fail(self, **kwargs):
        result = original(self, **kwargs)
        if kwargs["step_id"] == "roster":
            raise RuntimeError("synthetic interruption after atomic diagnostic batch")
        return result

    monkeypatch.setattr(RuntimeContext, "commit_step", save_then_fail)
    payload = submission([entry("K", 13, disposition="pending")])
    handle, report, adapter, service = run(prepared, [payload])
    assert report.outcome == "failed"
    roster, _ = results(prepared[0], handle.execution_id)
    assert roster.data.entries[0].author_errors
    monkeypatch.setattr(RuntimeContext, "commit_step", original)
    assert (
        resume_extraction(prepared[0], handle.execution_id, service=service).outcome
        == "finished"
    )
    assert len(adapter.seen) == 1


def test_changed_same_key_configuration_rejected(prepared):
    space, prep, config = prepared
    start_roster_extraction(
        space, preparation_execution_id=prep, configuration=config, request_key="key"
    )
    changed = config.model_copy(update={"examples": {"roster": []}})
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
    corrected = {"R1": None}
    if changed:
        corrected["entries"] = [entry("A", 3, "possibly_unbound")]
    handle, report, adapter, _ = run(prepared, [invalid, corrected])
    assert len(adapter.seen) == (3 if changed else 2)
    assert report.outcome == ("failed" if changed else "finished"), report.errors
    assert bool(get_extraction(prepared[0], handle.execution_id)["result_refs"]) == (
        not changed
    )


def test_correction_only_fills_missing_required_field(prepared):
    invalid = submission()
    del invalid["entries"][0]["conditional_assessments"]
    _handle, report, adapter, _ = run(prepared, [invalid, {"R1": []}])
    assert report.outcome == "finished", report.errors
    assert len(adapter.seen) == 2
    feedback = adapter.seen[1]["messages"][-1]["content"]
    assert '"path":["entries",0,"conditional_assessments"]' in feedback
    assert adapter.seen[1]["tools"][0]["function"]["name"] == "repair_roster"


def test_partial_group_and_conditional_judgment_preserved_without_expert(prepared):
    selected = entry("B", 4, "possibly_unbound")
    selected["conditional_assessments"] = [
        {
            "condition": "Under the single model",
            "statement": "Likely unbound from the Galaxy",
            "evidence": [evidence(4)],
        }
    ]
    payload = submission([selected, entry("K", 13, disposition="excluded")])
    payload["model_judgments"] = [
        "The most supported reading excludes K; the unnamed group remains unresolved."
    ]
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
    assert roster.data.entries[1].disposition == "excluded"
    assert len(roster.data.unresolved_groups) == 1
    assert (
        paper.data.object_contributions[0].conditional_assessments[0].condition
        == "Under the single model"
    )
    assert get_extraction(prepared[0], handle.execution_id)["questions"] == []
    judgments = get_extraction(prepared[0], handle.execution_id)["model_judgments"]
    assert len(judgments) == 1
    assert json.loads(judgments[0]["judgment"])["kind"] == "roster_model_submission"
    assert (
        resume_extraction(prepared[0], handle.execution_id, service=service).outcome
        == "finished"
    )
    assert len(get_extraction(prepared[0], handle.execution_id)["model_judgments"]) == 1
    assert len(adapter.seen) == 1


def test_model_interpretation_preserved_on_reentry_without_human_answer(prepared):
    selected = entry("G", 9, "no_overall_conclusion", "candidates_found")
    payload = submission([selected])
    payload["model_judgments"] = [
        "The author explicitly gives no overall preference for G in T:p1-b8; retain no_overall_conclusion under S04."
    ]
    handle, report, adapter, service = run(prepared, [payload])
    assert report.outcome == "finished", report.errors
    assert get_extraction(prepared[0], handle.execution_id)["questions"] == []
    assert report.model_judgments == 1
    assert (
        resume_extraction(prepared[0], handle.execution_id, service=service).outcome
        == "finished"
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


def test_citation_repair_preserves_science_and_reuses_saved_response(prepared):
    invalid = submission()
    invalid["entries"][0]["evidence"] = [
        {"location": "T:p1-b1", "quote": TEXT.splitlines()[2]}
    ]
    fixed = {"R1": evidence(3)}
    handle, report, adapter, service = run(prepared, [invalid, fixed])
    assert report.outcome == "finished", report.errors
    assert len(adapter.seen) == 2
    _, paper = results(prepared[0], handle.execution_id)
    assert (
        paper.data.object_contributions[0].contribution_summary.text
        == invalid["entries"][0]["summary"]
    )
    assert (
        resume_extraction(prepared[0], handle.execution_id, service=service).outcome
        == "finished"
    )
    assert len(adapter.seen) == 2


def test_second_correction_can_restore_a_changed_scientific_field(prepared):
    invalid = submission()
    invalid["entries"][0]["unknown"] = True
    changed = submission()
    changed["entries"][0]["boundness"] = "possibly_unbound"
    _handle, report, adapter, _ = run(prepared, [invalid, changed, {"R1": None}])
    assert report.outcome == "finished", report.errors
    assert len(adapter.seen) == 3
    assert "Use repair_roster" in adapter.seen[2]["messages"][-1]["content"]


@pytest.mark.parametrize("interrupted_call", [2, 3])
def test_correction_recovery_uses_its_archived_request(
    prepared, monkeypatch, interrupted_call
):
    from hvs_catalog.pipeline import roster as implementation

    invalid = submission()
    invalid["entries"][0]["unknown"] = True
    count = 0

    def fail_correction_archive():
        nonlocal count
        count += 1
        if count == interrupted_call:
            raise RuntimeError("interrupted after saved correction")

    handle, report, adapter, service = run(
        prepared,
        [invalid, "{second-invalid", {"R1": None}],
        hooks={"after_archive": fail_correction_archive},
    )
    assert report.outcome == "failed" and len(adapter.seen) == interrupted_call
    service.hooks.clear()
    monkeypatch.setattr(
        implementation, "_format_details", lambda *args: "Improved diagnostics"
    )
    resumed = resume_extraction(prepared[0], handle.execution_id, service=service)
    assert resumed.outcome == "finished", resumed.errors
    assert len(adapter.seen) == 3
    assert adapter.seen[2]["messages"][:-1] == adapter.seen[1]["messages"]
    assert "Improved diagnostics" not in adapter.seen[2]["messages"][2]["content"]


def test_original_range_reaches_model_without_generated_members(tmp_path):
    from hvs_catalog.pipeline.roster import material_prompt

    space, source = prepared_space(
        tmp_path, make_pdf("Table: HVS1,4-5 and others; S5-HVS1.")
    )
    try:
        handle, _ = MaterialsService(space, runner=FakeMinerU()).prepare_materials(
            request_key="ranges", paper=source.canonical_id
        )
        bundle = MaterialsService(space).get_prepared_input(handle.execution_id)
        record = space.store().read_record(
            RecordRef.model_validate(bundle["prepared_source_ref"])
        )
        prompt = material_prompt(space, bundle, record).manuscript
        assert "PAGE 1 BLOCK p1-b0" in prompt
        assert "HVS1,4-5 and others" in prompt
        assert "APPLICATION GROUP MEMBERS" not in prompt
        assert '"members"' not in prompt
        assert "S5-HVS1" in prompt
    finally:
        space.close()


def test_missing_named_group_entry_can_be_appended_without_rewriting_science(prepared):
    invalid = submission()
    invalid["coverage"]["state"] = "partial"
    invalid["unresolved_groups"] = [
        {
            "original_range": "A, B and others",
            "evidence": [evidence(4)],
            "member_identifier_values": ["A", "B"],
            "reason": "The remaining objects are unnamed.",
        }
    ]
    corrected = copy.deepcopy(invalid)
    corrected["entries"].append(entry("B", 4, "possibly_unbound"))
    corrected["unresolved_groups"][0]["reason"] += (
        " Named entries have now been supplied."
    )
    handle, report, adapter, _ = run(
        prepared, [invalid, {"R1": entry("B", 4, "possibly_unbound")}]
    )
    assert report.outcome == "finished", report.errors
    assert len(adapter.seen) == 2
    roster, _ = results(prepared[0], handle.execution_id)
    assert len(roster.data.entries) == 2
    assert roster.data.coverage.state == "partial"
    assert (
        roster.data.unresolved_groups[0].reason
        == invalid["unresolved_groups"][0]["reason"]
    )
    assert "Append the omitted entry" in adapter.seen[1]["messages"][-1]["content"]


def test_format_correction_cannot_append_an_unrequested_scientific_object(prepared):
    invalid = submission()
    invalid["entries"][0]["unknown"] = True
    changed = submission()
    changed["entries"].append(entry("B", 4, "possibly_unbound"))
    _handle, report, adapter, _ = run(prepared, [invalid, changed])
    assert report.outcome == "failed"
    assert len(adapter.seen) == 3
    assert "Use repair_roster" in adapter.seen[2]["messages"][-1]["content"]


@pytest.mark.parametrize("mode", ["daily", "evaluation"])
def test_model_submits_each_range_member_with_shared_original_evidence(
    tmp_path, monkeypatch, mode
):
    text = "This paper selects HVS4–6 and S5-HVS1 as unbound candidates, and mentions others without naming them."
    monkeypatch.setitem(evidence.__globals__, "TEXT", text)
    space, source = prepared_space(tmp_path, make_pdf(text))
    try:
        prep, _ = MaterialsService(space, runner=FakeMinerU()).prepare_materials(
            request_key="ranges", paper=source.canonical_id
        )
        config = ExtractionConfig(
            runtime=RuntimeConfig(quota_store=tmp_path / "quota.sqlite"),
            mode=mode,
        )
        payload = submission(
            [entry(name, 1, "unbound") for name in ("HVS4", "HVS5", "HVS6", "S5-HVS1")]
        )
        payload["coverage"]["state"] = "partial"
        payload["unresolved_groups"] = [
            {
                "original_range": "and mentions others without naming them",
                "evidence": [evidence(1)],
                "member_identifier_values": [],
                "reason": "The remaining objects are unnamed in the supplied material.",
            }
        ]
        handle, report, adapter, _ = run((space, prep.execution_id, config), [payload])
        assert report.outcome == "finished", report.errors
        roster, paper = results(space, handle.execution_id)
        assert [e.identifiers[0].value for e in roster.data.entries] == [
            "HVS4",
            "HVS5",
            "HVS6",
            "S5-HVS1",
        ]
        assert len(paper.data.object_contributions) == 4
        assert roster.data.coverage.state == "partial"
        assert len(roster.data.unresolved_groups) == 1
        assert len(adapter.seen) == 1
        assert "APPLICATION GROUP MEMBERS" not in str(adapter.seen[0])
    finally:
        space.close()
