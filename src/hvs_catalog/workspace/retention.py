"""Close verified extraction generations without retaining recovery-only checkpoints."""

from ..records.common import ArtifactRef, RecordRef
from ..records.file_checks import scoped_verification
from .archives import read_json
from .errors import WorkspaceIntegrityError

PREFIX = "retained_result:"


def require_resumable(space, execution_id):
    if space.store().get_meta(PREFIX + execution_id) is not None:
        raise ValueError("执行已明确结束；请以新请求键创建新实验")


@scoped_verification
def retained_result(space, execution_id):
    from ..pipeline.results import current_result

    raw = space.store().get_meta(PREFIX + execution_id)
    if raw is None:
        return None
    ref = ArtifactRef.model_validate_json(raw)
    current = current_result(space, execution_id)
    if current is None or current[0] != ref or current[1].status.resumable:
        raise WorkspaceIntegrityError(
            "Closed result differs from the published generation."
        )
    manifest = current[1]
    trace = read_json(space, manifest.trace_ref)
    # Return views of the same archives, without a second persisted result document.
    return {
        "execution_id": execution_id,
        "status": {
            "state": manifest.status.execution,
            "errors": [e["message"] for e in manifest.errors],
        },
        "manifest": manifest.model_dump(mode="json"),
        "resumable": False,
        "calls": [
            {
                **c,
                "response": read_json(space, ArtifactRef.model_validate(c["response"]))
                if c["response"]
                else None,
            }
            for c in trace["calls"]
        ],
        "records": [
            space.store()
            .read_record(RecordRef.model_validate(s["ref"]))
            .model_dump(mode="json", by_alias=True)
            for s in trace["snapshots"]
        ],
    }


@scoped_verification
def close_extraction(space, execution_id, *, end=False):
    from langgraph.checkpoint.sqlite import SqliteSaver

    from ..pipeline.results import publish_result, result_status
    from ..records.validate import validate_collection
    from ..runtime import get_execution
    from ..runtime.execution import verify_execution_inputs
    from .store import record_closure, verify_artifact_refs

    with space.lock():
        existing = retained_result(space, execution_id)
        handle = get_execution(space, execution_id)
        run = space.store().read_record_current(handle.run_ref.record_id)
        if existing is None:
            if (
                run.data.state != "finished"
                or result_status(space, execution_id) != "complete"
            ) and not end:
                raise ValueError("部分、失败或中断现场须明确结束后才能精简")
            records = record_closure([run], space.store().read_record)
            problems = verify_execution_inputs(space, handle) + verify_artifact_refs(
                records, space.root
            )
            validate_collection(records, space.root)
            if problems:
                raise WorkspaceIntegrityError("; ".join(problems))
            if run.data.state == "finished":
                from .backup import checkpoint_state_problems

                problems = checkpoint_state_problems(space, execution_id)
                if problems:
                    raise WorkspaceIntegrityError("; ".join(problems))
            publish_result(space, execution_id, closed=True)
            retained_result(space, execution_id)
        run = space.store().read_record_current(handle.run_ref.record_id)
        space.store().compact_run_history(run.record_id, run.revision, execution_id)
        conn = space.checkpoint_connection()
        try:
            saver = SqliteSaver(conn)
            saver.setup()
            saver.delete_thread(execution_id)
        finally:
            conn.close()
        return retained_result(space, execution_id)
