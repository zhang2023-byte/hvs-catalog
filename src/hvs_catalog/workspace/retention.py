"""Close verified extraction generations without retaining recovery-only checkpoints."""

from ..records.common import ArtifactRef, RecordRef
from ..records.file_checks import scoped_verification
from .archives import read_json
from .errors import WorkspaceIntegrityError

PREFIX = "retained_result:"


def discard_literature_extractions(space, *, expected_execution_ids):
    """Discard explicitly selected extraction state, preserving all preparation history.

    This reset requires the complete current extraction set and an idle workspace.
    It does not rename executions, change materials, or restart any model calls.
    """
    import shutil

    from langgraph.checkpoint.sqlite import SqliteSaver

    from ..records.validate import validate_collection
    from .backup import _referenced_files
    from .layout import resolve_within

    expected = set(expected_execution_ids)
    if space.read_only or not space.is_literature or not expected:
        raise ValueError(
            "Provide a writable literature workspace and explicit executions."
        )
    with space.lock():
        executions = space.store().list_executions()
        actual = {e.execution_id for e in executions if e.flow_id == "hvs-extraction"}
        if actual != expected:
            raise ValueError(
                "The explicit selection must match all current extractions."
            )
        records = space.store().list_records()
        kept = [r for r in records if r.execution_id not in expected]
        validate_collection(kept, space.root)
        before = set(_referenced_files(space))
        directories = [
            f"{prefix}/{execution}"
            for execution in sorted(expected)
            for prefix in ("runs", ".state/executions")
        ]
        for directory in directories:
            resolve_within(space.root, directory)
        conn = space.checkpoint_connection()
        try:
            saver = SqliteSaver(conn)
            saver.setup()
            for execution in sorted(expected):
                saver.delete_thread(execution)
        finally:
            conn.close()
        with space.store().transaction() as tx:
            for execution in sorted(expected):
                owned = [
                    r[0]
                    for r in tx.conn.execute(
                        "SELECT DISTINCT record_id FROM records WHERE execution_id=?",
                        (execution,),
                    )
                ]
                for record_id in owned:
                    tx.conn.execute(
                        "DELETE FROM current_revisions WHERE record_id=?", (record_id,)
                    )
                    tx.conn.execute(
                        "DELETE FROM records WHERE record_id=?", (record_id,)
                    )
                for table in ("receipts", "step_results", "questions", "executions"):
                    tx.conn.execute(
                        f"DELETE FROM {table} WHERE execution_id=?", (execution,)
                    )
                for prefix in (
                    "extraction_result:",
                    "retained_result:",
                    "framework_final_checkpoint:",
                ):
                    tx.conn.execute(
                        "DELETE FROM meta WHERE key=?", (prefix + execution,)
                    )
        after = set(_referenced_files(space))
        if any(
            path.startswith(directory + "/")
            for path in after
            for directory in directories
        ):
            raise WorkspaceIntegrityError(
                "A retained execution still references extraction files."
            )
        removed = 0
        for relative in sorted(before - after):
            if relative.startswith(("runs/", ".state/inputs/", ".state/executions/")):
                path = resolve_within(space.root, relative)
                if path.is_file():
                    path.unlink()
                    removed += 1
        for directory in directories:
            path = resolve_within(space.root, directory)
            if path.is_dir():
                shutil.rmtree(path)
        return {
            "discarded_executions": len(expected),
            "retained_records": len(kept),
            "removed_referenced_files": removed,
        }


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
