"""A saved zero-correction experiment can consume its missing bounded repairs."""

from contextlib import closing

import pytest
import test_pdf_quantities as f
from pydantic import SecretStr

from hvs_catalog.llm.managed import CallService
from hvs_catalog.pipeline import (
    continue_format_corrections,
    refresh_saved_partials,
    resume_extraction,
)
from hvs_catalog.runtime import get_execution
from hvs_catalog.workspace import open_workspace
from hvs_catalog.workspace.backup import backup_workspace, restore_workspace

pytestmark = pytest.mark.integration


@pytest.fixture
def prepared(tmp_path):
    g = f.prepared_pdf.__wrapped__(tmp_path)
    s, p, c = next(g)
    yield s, p, c.model_copy(update={"max_format_corrections": 0})
    try:
        next(g)
    except StopIteration:
        pass


def bad(tool, _):
    raw = f.quantities(f.object_id(tool))
    if f.object_id(tool).endswith("-2"):
        raw["measurements"][0]["interval_kind"] = "credible"
    return raw


def test_continuation_only_repairs_failure_and_survives_backup(prepared, tmp_path):
    s, _, c = prepared
    h, r, _ = f.start(prepared, f.Adapter(bad))
    assert r.outcome == "failed"
    old = s.store().read_record(get_execution(s, h.execution_id).run_ref)
    good = [
        x
        for x in old.data.steps
        if x.state == "finished" and x.step_id.startswith("quantities-")
    ]
    intent = continue_format_corrections(s, h.execution_id, request_key="append")
    assert len(intent["targets"]) == 1
    assert continue_format_corrections(s, h.execution_id, request_key="append")[
        "reused"
    ]
    with pytest.raises(ValueError, match="cannot reset"):
        continue_format_corrections(s, h.execution_id, request_key="other")
    backup_workspace(s, tmp_path / "backup")
    restore_workspace(tmp_path / "backup", tmp_path / "restored")
    with closing(open_workspace(tmp_path / "restored")) as restored:

        def answer(tool, _):
            assert tool["name"] == "repair_quantities"
            assert f.object_id(tool).endswith("-2")
            return {k: None for k in tool["parameters"]["properties"]}

        a = f.Adapter(answer)
        service = CallService(c.runtime, credential=SecretStr("synthetic"), adapter=a)
        r = resume_extraction(restored, h.execution_id, service=service)
        assert r.outcome == "finished", r.errors
        assert len(a.seen) == 1
        new = restored.store().read_record(
            get_execution(restored, h.execution_id).run_ref
        )
        for x in good:
            assert next(y for y in new.data.steps if y.step_id == x.step_id) == x
        # Successful closure removes progress revisions, retaining every submission.
        from hvs_catalog.workspace.retention import retained_result

        assert restored.store().read_record(old.ref()) is None
        assert len(retained_result(restored, h.execution_id)["calls"]) == sum(
            len(step.attempts) for step in new.data.steps
        )
        assert (
            resume_extraction(restored, h.execution_id, service=service).outcome
            == "finished"
        )
        assert len(a.seen) == 1


def test_failed_corrections_do_not_restart_initial_or_reset_budget(prepared):
    s, _, c = prepared
    h, r, _ = f.start(prepared, f.Adapter(bad))
    assert r.outcome == "failed"
    continue_format_corrections(s, h.execution_id, request_key="bounded")
    a = f.Adapter(lambda tool, _: {k: 123 for k in tool["parameters"]["properties"]})
    service = CallService(c.runtime, credential=SecretStr("synthetic"), adapter=a)
    r = resume_extraction(s, h.execution_id, service=service)
    assert r.outcome == "failed" and len(a.seen) == 2
    assert all(x["tools"][0]["function"]["name"] == "repair_quantities" for x in a.seen)
    assert continue_format_corrections(s, h.execution_id, request_key="bounded")[
        "reused"
    ]
    assert resume_extraction(s, h.execution_id, service=service).outcome == "failed"
    assert len(a.seen) == 2


def test_later_failed_correction_updates_partial_and_offline_refresh_is_idempotent(
    prepared,
):
    import copy

    from hvs_catalog.evaluation.adapt import prediction_from_execution

    s, _, c = prepared

    def initial(tool, _):
        draft = f.quantities(f.object_id(tool))
        draft["measurements"].extend(
            [
                {
                    "quantity": "observed_phase_space.distance",
                    "value": "2.4",
                    "unit": "kpc",
                    "limit_kind": "none",
                    "condition": "Synthetic distance",
                    "source": "this_paper",
                    "evidence": [
                        {"location": "T:p1-b1", "quote": "Incorrect citation"}
                    ],
                },
                {
                    "quantity": "observed_phase_space.radial_velocity",
                    "value": "510",
                    "unit": "km/s",
                    "limit_kind": "none",
                    "condition": "Synthetic other report",
                    "source": "this_paper",
                    "evidence": [
                        {"location": "T:p1-b2", "quote": "Another incorrect citation"}
                    ],
                },
            ]
        )
        return draft

    h, r, _ = f.start(prepared, f.Adapter(initial, names=("A",)))
    assert r.outcome == "failed"
    old = s.store().read_record(get_execution(s, h.execution_id).run_ref)
    old_partial = next(
        x for x in old.data.steps if x.step_id.startswith("partial-quantities-")
    )
    assert (
        sum(
            len(g.values)
            for o in prediction_from_execution(s, h.execution_id).objects
            for g in o.quantities
        )
        == 1
    )
    old_records = [
        copy.deepcopy(s.store().read_record(ref)) for ref in old_partial.result_refs
    ]
    continue_format_corrections(s, h.execution_id, request_key="more-valid-fields")

    def repair(tool, _):
        props = tool["parameters"]["properties"]
        return {
            k: (
                f.cite(1)
                if k == "R1" and len(props) == 2
                else {"location": "T:p1-b2", "quote": "Still an incorrect citation"}
            )
            for k in props
        }

    adapter = f.Adapter(repair)
    service = CallService(c.runtime, credential=SecretStr("synthetic"), adapter=adapter)
    assert resume_extraction(s, h.execution_id, service=service).outcome == "failed"
    assert len(adapter.seen) == 2

    prediction = prediction_from_execution(s, h.execution_id)
    assert sum(len(g.values) for o in prediction.objects for g in o.quantities) == 2
    saved = s.store().read_record(get_execution(s, h.execution_id).run_ref)
    attempts = copy.deepcopy(
        [x.attempts for x in saved.data.steps if x.step_id.startswith("quantities-")]
    )
    for _ in range(2):
        result = refresh_saved_partials(
            s, h.execution_id, object_ids=[prediction.objects[0].label]
        )
        assert result["new_model_calls"] == 0
        assert prediction_from_execution(s, h.execution_id) == prediction
        current = s.store().read_record(get_execution(s, h.execution_id).run_ref)
        assert current.data.state == "failed"
        assert [
            x.attempts
            for x in current.data.steps
            if x.step_id.startswith("quantities-")
        ] == attempts
    assert s.store().read_record(old.ref()) == old
    assert [
        s.store().read_record(ref) for ref in old_partial.result_refs
    ] == old_records
    assert len(adapter.seen) == 2


def test_roster_correction_then_only_unprocessed_descendants(prepared):
    s, _, c = prepared

    class InvalidRoster(f.Adapter):
        def send(self, payload, runtime, credential):
            result = super().send(payload, runtime, credential)
            if payload["tools"][0]["function"]["name"] == "submit_roster":
                import json

                args = f.roster()
                args["coverage"]["state"] = "wrong"
                result["body"]["choices"][0]["message"]["tool_calls"][0]["function"][
                    "arguments"
                ] = json.dumps(args)
            return result

    h, r, _ = f.start(
        prepared, InvalidRoster(lambda tool, _: f.quantities(f.object_id(tool)))
    )
    assert r.outcome == "failed"
    preserved = [
        step
        for step in s.store()
        .read_record(get_execution(s, h.execution_id).run_ref)
        .data.steps
        if step.step_id.startswith("quantities-") and step.state == "finished"
    ]
    assert len(preserved) == 2
    continue_format_corrections(s, h.execution_id, request_key="roster")
    calls = []

    class Repaired(f.Adapter):
        def send(self, payload, runtime, credential):
            name = payload["tools"][0]["function"]["name"]
            calls.append(name)
            if name == "repair_roster":
                tool = payload["tools"][0]["function"]
                return f.ScriptedAdapter(
                    [{k: "complete" for k in tool["parameters"]["properties"]}]
                ).send(payload, runtime, credential)
            return super().send(payload, runtime, credential)

    def answer(tool, _):
        if tool["name"] == "repair_roster":
            return {k: "complete" for k in tool["parameters"]["properties"]}
        return f.quantities(f.object_id(tool))

    a = Repaired(answer)
    r = resume_extraction(
        s,
        h.execution_id,
        service=CallService(c.runtime, credential=SecretStr("synthetic"), adapter=a),
    )
    assert r.outcome == "finished", r.errors
    assert calls == ["repair_roster"]
    current = s.store().read_record(get_execution(s, h.execution_id).run_ref)
    assert all(
        next(step for step in current.data.steps if step.step_id == old.step_id) == old
        for old in preserved
    )


def test_offline_refresh_cannot_send_or_create_missing_correction(prepared):
    from hvs_catalog.evaluation.adapt import prediction_from_execution
    from hvs_catalog.llm.managed import CallFailure

    s, _, c = prepared
    h, r, _ = f.start(prepared, f.Adapter(bad))
    assert r.outcome == "failed"
    continue_format_corrections(s, h.execution_id, request_key="not-yet-called")

    class Unsent(CallService):
        def credential(self):
            raise CallFailure("Synthetic failure before a new outbound attempt")

    assert (
        resume_extraction(s, h.execution_id, service=Unsent(c.runtime)).outcome
        == "failed"
    )
    before = prediction_from_execution(s, h.execution_id)
    saved = s.store().read_record(get_execution(s, h.execution_id).run_ref)
    attempts = [
        x.attempts for x in saved.data.steps if x.step_id.startswith("quantities-")
    ]
    target = next(o.label for o in before.objects if o.label.endswith("-2"))
    result = refresh_saved_partials(s, h.execution_id, object_ids=[target])
    assert result["new_model_calls"] == 0
    assert any("already archived reply" in error for error in result["errors"])
    assert prediction_from_execution(s, h.execution_id) == before
    current = s.store().read_record(get_execution(s, h.execution_id).run_ref)
    assert current.data.state == "failed"
    assert [
        x.attempts for x in current.data.steps if x.step_id.startswith("quantities-")
    ] == attempts
