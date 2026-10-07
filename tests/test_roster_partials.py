"""Independent roster failures retain valid objects and bounded recovery facts."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
import test_pdf_quantities as f
from pydantic import SecretStr
from test_roster import ScriptedAdapter, results

from hvs_catalog.evaluation.adapt import prediction_from_execution
from hvs_catalog.llm.managed import CallService
from hvs_catalog.pipeline import (
    continue_format_corrections,
    get_extraction,
    resume_extraction,
    start_pdf_extraction,
    start_roster_extraction,
)
from hvs_catalog.records.common import RecordRef
from hvs_catalog.records.roster import RosterRecord
from hvs_catalog.runtime import get_execution
from hvs_catalog.workspace import open_workspace
from hvs_catalog.workspace.backup import backup_workspace, restore_workspace

pytestmark = pytest.mark.integration


@pytest.fixture
def prepared(tmp_path):
    yield from f.prepared_pdf.__wrapped__(tmp_path)


class Replies(ScriptedAdapter):
    def __init__(self, respond):
        super().__init__([])
        self.respond = respond

    def send(self, payload, runtime, credential):
        self.seen.append(copy.deepcopy(payload))
        return ScriptedAdapter([self.respond(payload)]).send(
            payload, runtime, credential
        )


def begin(prepared, adapter, *, full=False, corrections=2):
    space, prep, config = prepared
    config = config.model_copy(update={"max_format_corrections": corrections})
    start = start_pdf_extraction if full else start_roster_extraction
    handle = start(
        space,
        preparation_execution_id=prep,
        configuration=config,
        request_key="extract",
    )
    service = CallService(
        config.runtime,
        credential=SecretStr("fixture-only-secret-roster"),
        adapter=adapter,
    )
    return (
        handle,
        resume_extraction(space, handle.execution_id, service=service),
        service,
    )


def target(payload):
    content = payload["messages"][1]["content"]
    text = content if isinstance(content, str) else content[0]["text"]
    return json.loads(text.split("===== SUPPLIED CONTEXT =====\n", 1)[1])["targets"][0]


def roster(space, execution_id):
    pred = prediction_from_execution(space, execution_id)
    refs = get_extraction(space, execution_id)["result_refs"]
    saved = [space.store().read_record(RecordRef.model_validate(r)) for r in refs]
    return pred, next(r for r in reversed(saved) if isinstance(r, RosterRecord))


def bad_citation():
    return {"location": "T:p1-b1", "quote": "A synthetic incorrect quotation"}


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_large_roster_preserves_draft_and_correction_budget(prepared, repair_succeeds):
    raw = f.roster()
    summary = "Retained synthetic summary. " * 6000 + "End of retained summary."
    raw["entries"][0]["summary"] = summary
    raw["entries"][1]["summary_evidence"] = [bad_citation()]
    original = copy.deepcopy(raw)
    assert len(json.dumps(raw).encode()) > 128 * 1024

    def respond(payload):
        tool = payload["tools"][0]["function"]
        if tool["name"] == "submit_roster":
            return raw
        assert tool["name"] == "repair_roster"
        previous = json.loads(
            payload["messages"][-1]["content"].split("Previous output:\n", 1)[1]
        )
        function = previous["tool_calls"][0]["function"]
        arguments = json.loads(function["arguments"])
        if function["name"] == "submit_roster":
            assert arguments == raw
        else:
            assert function["name"] == "repair_roster"
            assert arguments and all(
                value == bad_citation() for value in arguments.values()
            )
        return {
            key: f.cite() if repair_succeeds else bad_citation()
            for key in tool["parameters"]["properties"]
        }

    adapter = Replies(respond)
    handle, report, service = begin(prepared, adapter)
    expected = "finished" if repair_succeeds else "failed"
    assert report.outcome == expected, report.errors
    if not repair_succeeds:
        assert "correction exhausted" in str(report.errors)
    assert len(adapter.seen) == (2 if repair_succeeds else 3)
    assert raw == original
    _, saved = roster(prepared[0], handle.execution_id)
    _, saved_paper = results(prepared[0], handle.execution_id)
    assert saved_paper.data.object_contributions[0].contribution_summary.text == summary
    assert [entry.identifiers[0].value for entry in saved.data.entries] == (
        ["A", "B"] if repair_succeeds else ["A"]
    )
    assert (
        resume_extraction(prepared[0], handle.execution_id, service=service).outcome
        == expected
    )
    assert len(adapter.seen) == (2 if repair_succeeds else 3)


def test_schema_citation_and_source_error_share_one_correction(prepared):
    raw = json.loads(json.dumps(f.roster()))
    del raw["entries"][0]["conditional_assessments"]
    raw["entries"][0]["summary_evidence"] = [bad_citation()]
    raw["entries"][1]["author_errors"] = [
        {
            "reason": "Original identity is inconsistent.",
            "raw_values": ["B"],
            "evidence": [f.cite()],
        }
    ]
    # This required field becomes inapplicable after confirming pending.
    del raw["entries"][1]["conditional_assessments"]
    original = copy.deepcopy(raw)

    def respond(payload):
        tool = payload["tools"][0]["function"]
        if tool["name"] == "submit_roster":
            return raw
        assert tool["name"] == "repair_roster"
        result = {}
        for key, spec in tool["parameters"]["properties"].items():
            if "const" in spec:
                result[key] = spec["const"]
            elif spec.get("type") == "null":
                result[key] = None
            elif spec.get("type") == "array":
                result[key] = []
            else:
                result[key] = f.cite()
        return result

    adapter = Replies(respond)
    h, report, _ = begin(prepared, adapter)
    assert report.outcome == "finished", report.errors
    assert len(adapter.seen) == 2 and raw == original
    feedback = adapter.seen[1]["messages"][-1]["content"]
    assert '"conditional_assessments"' in feedback
    assert '"summary_evidence"' in feedback
    assert '"disposition"' in feedback
    assert '"path":["entries",1,"conditional_assessments"]' not in feedback
    _, saved = roster(prepared[0], h.execution_id)
    assert [e.disposition for e in saved.data.entries] == ["included", "pending"]


def test_missing_named_members_and_leaf_repairs_do_not_replace_existing_entries(
    prepared,
):
    raw = json.loads(json.dumps(f.roster(("A",))))
    del raw["entries"][0]["conditional_assessments"]
    raw["unresolved_groups"] = [
        {
            "original_range": "A, B and C, plus unnamed members",
            "evidence": [f.cite()],
            "member_identifier_values": ["A", "B", "C"],
            "reason": "The unnamed remainder is not identifiable.",
        }
    ]

    def respond(payload):
        tool = payload["tools"][0]["function"]
        if tool["name"] == "submit_roster":
            return raw
        result = {}
        for key, spec in tool["parameters"]["properties"].items():
            if spec.get("type") == "array":
                result[key] = []
            else:
                name = spec["allOf"][1]["properties"]["identifiers"]["contains"][
                    "properties"
                ]["value"]["const"]
                result[key] = f.roster((name,))["entries"][0]
        return result

    adapter = Replies(respond)
    h, result, _ = begin(prepared, adapter)
    assert result.outcome == "finished", result.errors
    assert len(adapter.seen) == 2
    _, saved = roster(prepared[0], h.execution_id)
    assert [e.identifiers[0].value for e in saved.data.entries] == ["A", "B", "C"]
    assert len(saved.data.unresolved_groups[0].known_member_item_ids) == 3


def test_failed_roster_continues_valid_quantity_and_terminal_restore(
    prepared, tmp_path
):
    raw = json.loads(json.dumps(f.roster()))
    raw["entries"][1]["boundness_evidence"] = [bad_citation()]

    def respond(payload):
        tool = payload["tools"][0]["function"]
        if tool["name"] == "submit_roster":
            return raw
        if tool["name"] == "repair_roster":
            return {key: bad_citation() for key in tool["parameters"]["properties"]}
        assert tool["name"] == "submit_quantities"
        assert target(payload)["identifiers"] == ["A"]
        return f.quantities(target(payload)["object_id"])

    adapter = Replies(respond)
    h, report, service = begin(prepared, adapter, full=True)
    assert report.outcome == "failed", report.errors
    pred, saved = roster(prepared[0], h.execution_id)
    assert pred.delivered and pred.format_valid and pred.completion == "partial"
    assert [o.identifiers for o in pred.objects] == [["A"]]
    assert saved.data.coverage.state == "partial"
    obj = f.paper(prepared[0], h.execution_id).data.object_contributions[0]
    assert obj.quantities[0].values[0].value == "-430"
    assert len(adapter.seen) == 4
    before = get_extraction(prepared[0], h.execution_id)
    assert (
        resume_extraction(prepared[0], h.execution_id, service=service).mode
        == "already-failed"
    )
    assert get_extraction(prepared[0], h.execution_id) == before
    assert len(adapter.seen) == 4
    backup_workspace(prepared[0], tmp_path / "backup")
    restore_workspace(tmp_path / "backup", tmp_path / "restored")
    script = """
import json,sys
from hvs_catalog.workspace import open_workspace
from hvs_catalog.pipeline import get_extraction,resume_extraction
from hvs_catalog.llm.managed import CallService
from hvs_catalog.pipeline.configuration import stored_configuration
from hvs_catalog.runtime import get_execution
from hvs_catalog.llm.transport import TokenDanceAdapter
from pydantic import SecretStr
class NoCalls(TokenDanceAdapter):
    def send(self,*args,**kwargs): raise AssertionError('Unexpected new request')
from contextlib import closing
with closing(open_workspace(sys.argv[1])) as s:
    e=get_execution(s,sys.argv[2]);before=get_extraction(s,e.execution_id)
    result=resume_extraction(s,e.execution_id,service=CallService(stored_configuration(e.input_snapshot).runtime,credential=SecretStr('synthetic'),adapter=NoCalls()))
    assert result.mode=='already-failed' and get_extraction(s,e.execution_id)==before
    print(json.dumps(before))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "restored"), h.execution_id],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["state"] == "failed"


@pytest.mark.parametrize(
    "kind", ["first_bad", "duplicate", "all_bad", "malformed_global"]
)
def test_partial_never_selects_ambiguous_owner_or_fakes_empty(prepared, kind):
    raw = json.loads(json.dumps(f.roster()))
    expected = ["B"]
    if kind == "duplicate":
        raw["entries"].insert(0, copy.deepcopy(raw["entries"][0]))
    elif kind == "malformed_global":
        raw["coverage"] = {"state": 7, "checked_file_ids": ["unsupplied"]}
        expected = ["A", "B"]
    else:
        raw["entries"][0]["boundness"] = "invalid"
        if kind == "all_bad":
            raw["entries"][1]["boundness"] = "invalid"
            expected = []
    adapter = ScriptedAdapter([raw])
    h, report, _ = begin(prepared, adapter, corrections=0)
    assert report.outcome == "failed" and len(adapter.seen) == 1
    pred = prediction_from_execution(prepared[0], h.execution_id)
    assert [o.identifiers[0] for o in pred.objects] == expected
    assert pred.delivered == bool(expected)
    if expected:
        _, saved = roster(prepared[0], h.execution_id)
        if kind == "malformed_global":
            assert saved.data.coverage.state == "not_checked"
        else:
            index = 3 if kind == "duplicate" else 2
            assert saved.data.entries[0].item_id.endswith("-" + str(index))
            assert saved.data.entries[0].contribution_id.endswith("-" + str(index))


def test_partial_group_preserves_printed_range_and_only_links_saved_members(prepared):
    raw = json.loads(json.dumps(f.roster()))
    raw["entries"][1]["boundness_evidence"] = [bad_citation()]
    raw["unresolved_groups"] = [
        {
            "original_range": "A and B, followed by unnamed objects",
            "evidence": [f.cite()],
            "member_identifier_values": ["A", "B"],
            "reason": "The unnamed remainder cannot be established.",
        }
    ]
    original = copy.deepcopy(raw)
    h, report, _ = begin(prepared, ScriptedAdapter([raw]), corrections=0)
    assert report.outcome == "failed" and raw == original
    _, saved = roster(prepared[0], h.execution_id)
    group = saved.data.unresolved_groups[0]
    assert group.original_range == original["unresolved_groups"][0]["original_range"]
    assert group.reason == original["unresolved_groups"][0]["reason"]
    assert group.known_member_item_ids == [saved.data.entries[0].item_id]


def test_bounded_continuation_keeps_saved_object_ids_and_values(prepared, tmp_path):
    raw = json.loads(json.dumps(f.roster()))
    del raw["entries"][1]["conditional_assessments"]

    def initial(payload):
        name = payload["tools"][0]["function"]["name"]
        return (
            raw
            if name == "submit_roster"
            else f.quantities(target(payload)["object_id"])
        )

    first = Replies(initial)
    h, report, _ = begin(prepared, first, full=True, corrections=0)
    assert report.outcome == "failed" and len(first.seen) == 2
    space, _, config = prepared
    old = space.store().read_record(get_execution(space, h.execution_id).run_ref)
    good = next(
        s
        for s in old.data.steps
        if s.step_id.startswith("quantities-") and s.state == "finished"
    )
    old_paper = f.paper(space, h.execution_id)
    continue_format_corrections(space, h.execution_id, request_key="remaining")
    backup_workspace(space, tmp_path / "backup")
    restore_workspace(tmp_path / "backup", tmp_path / "restored")
    with closing(open_workspace(tmp_path / "restored")) as restored:

        def remaining(payload):
            tool = payload["tools"][0]["function"]
            if tool["name"] == "repair_roster":
                return {key: [] for key in tool["parameters"]["properties"]}
            assert tool["name"] == "submit_quantities"
            assert target(payload)["identifiers"] == ["B"]
            return f.quantities(target(payload)["object_id"])

        adapter = Replies(remaining)
        service = CallService(
            config.runtime,
            credential=SecretStr("fixture-only-secret-roster"),
            adapter=adapter,
        )
        result = resume_extraction(restored, h.execution_id, service=service)
        assert result.outcome == "finished", result.errors
        assert len(adapter.seen) == 2
        current = restored.store().read_record(
            get_execution(restored, h.execution_id).run_ref
        )
        assert next(s for s in current.data.steps if s.step_id == good.step_id) == good
        assert restored.store().read_record(old.ref()) == old
        assert restored.store().read_record(old_paper.ref()) == old_paper
        final = f.paper(restored, h.execution_id)
        assert [c.identifiers[0].value for c in final.data.object_contributions] == [
            "A",
            "B",
        ]
        assert [
            c.item_id.rsplit("-", 1)[-1] for c in final.data.object_contributions
        ] == ["1", "2"]
        assert all(c.quantities for c in final.data.object_contributions)
        assert prediction_from_execution(restored, h.execution_id).format_valid


def test_process_exit_after_partial_commit_preserves_roster_and_unknown_call(
    prepared, tmp_path
):
    space, prep, config = prepared
    backup_workspace(space, tmp_path / "before")
    restore_workspace(tmp_path / "before", tmp_path / "child")
    raw = json.loads(json.dumps(f.roster()))
    raw["entries"][1]["boundness_evidence"] = [bad_citation()]
    (tmp_path / "draft.json").write_text(json.dumps(raw))
    (tmp_path / "config.json").write_text(
        config.model_copy(update={"max_format_corrections": 0}).model_dump_json()
    )
    script = """
import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[3])
from test_roster import ScriptedAdapter
from hvs_catalog.workspace import open_workspace
from hvs_catalog.pipeline import ExtractionConfig,start_pdf_extraction,resume_extraction
from hvs_catalog.llm.managed import CallService
from pydantic import SecretStr
root=Path(sys.argv[1]);cfg=ExtractionConfig.model_validate_json((root/'config.json').read_text())
raw=json.loads((root/'draft.json').read_text());s=open_workspace(root/'child')
h=start_pdf_extraction(s,preparation_execution_id=sys.argv[2],configuration=cfg,request_key='fatal')
(root/'execution.txt').write_text(h.execution_id)
count=0
def terminate():
    global count
    count+=1
    if count==2:os._exit(73)
service=CallService(cfg.runtime,credential=SecretStr('fixture-only-secret-roster'),adapter=ScriptedAdapter([raw]),hooks={'after_pending':terminate})
resume_extraction(s,h.execution_id,service=service)
raise AssertionError('Fatal boundary was not reached')
"""
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path),
            prep,
            str(Path(f.__file__).parent),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 73, process.stderr
    execution_id = (tmp_path / "execution.txt").read_text()
    with closing(open_workspace(tmp_path / "child")) as restored:
        before = prediction_from_execution(restored, execution_id)
        assert [o.identifiers for o in before.objects] == [["A"]]
        adapter = ScriptedAdapter(["truncated"])
        service = CallService(
            config.runtime,
            credential=SecretStr("fixture-only-secret-roster"),
            adapter=adapter,
        )
        result = resume_extraction(restored, execution_id, service=service)
        assert result.outcome == "failed" and not adapter.seen
        after = prediction_from_execution(restored, execution_id)
        assert [o.identifiers for o in after.objects] == [["A"]]
        assert after.completion == "partial"
