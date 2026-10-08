"""Verified terminal results replace resumable framework history after closure."""

from __future__ import annotations

import json

from ..records.common import ArtifactRef, canonical_json, sha256_of
from ..records.file_checks import scoped_verification
from .errors import WorkspaceIntegrityError

PREFIX = "retained_result:"
FORMAT = "hvs.extraction-result"
PENDING_PREFIX = "retention_pending:"


def require_resumable(space, execution_id):
    """Reject mutation of an execution whose terminal result is already retained."""
    if space.store().get_meta(PREFIX + execution_id) is not None:
        raise ValueError("执行已明确结束；请以新请求键创建新实验")


def _compact_calls(space, run):
    """Convert legacy inline calls without changing reconstructed requests/submissions."""
    from pathlib import Path

    from ..llm.managed import _archive_raw
    from .archives import publish_json, read_json
    from .store import CommitRequest, walk_artifact_refs

    changed = False
    steps = []
    old_files = set()
    for step in run.data.steps:
        attempts = []
        for attempt in step.attempts:
            updates = {}
            if attempt.request:
                encoded = json.loads(space.read_file(attempt.request))
                if encoded.get("format") != "hvs.shared-json":
                    value = read_json(space, attempt.request)
                    ref = publish_json(
                        space,
                        f"runs/{run.execution_id}/requests/compact/{Path(attempt.request.path).name}",
                        value,
                    )
                    if read_json(space, ref) != value:
                        raise WorkspaceIntegrityError("共用请求与原输入不一致")
                    updates["request"] = ref
                    old_files.add(attempt.request.path)
            if attempt.response:
                value = json.loads(space.read_file(attempt.response))
                original = canonical_json(value)
                raw = (value.get("exchange") or {}).get("raw_response_ref")
                if raw:
                    old_files.add(raw["path"])
                _archive_raw(None, value, None)
                if canonical_json(value) != original:
                    ref = space.publish_run_file(
                        run.execution_id,
                        f"responses/compact/{Path(attempt.response.path).name}",
                        canonical_json(value).encode(),
                    )
                    updates["response"] = ref
                    old_files.add(attempt.response.path)
            attempts.append(attempt.model_copy(update=updates))
            changed |= bool(updates)
        steps.append(step.model_copy(update={"attempts": attempts}))
    artifacts = [ref for ref in run.data.artifacts if not ref.path.endswith(".raw")]
    old_files.update(
        ref.path for ref in run.data.artifacts if ref.path.endswith(".raw")
    )
    if changed or artifacts != run.data.artifacts:
        replacement = run.model_copy(
            update={
                "revision": run.revision + 1,
                "data": run.data.model_copy(
                    update={"steps": steps, "artifacts": artifacts}
                ),
            }
        )
        pending_key = PENDING_PREFIX + run.execution_id
        pending = space.store().get_meta(pending_key)
        journal = (
            json.loads(pending)
            if pending
            else {
                "original_run_sha256": sha256_of(run.model_dump(mode="json")),
                "obsolete_files": [],
            }
        )
        journal["obsolete_files"] = sorted(set(journal["obsolete_files"]) | old_files)
        space.store().commit(
            CommitRequest(
                operation_key="compact-calls:"
                + run.execution_id
                + ":"
                + str(run.revision),
                kind="retention",
                new_records=[replacement],
                expected_revisions={run.record_id: run.revision},
                execution_id=run.execution_id,
                meta_updates={pending_key: canonical_json(journal)},
            )
        )
        run = replacement
    kept = {ref["path"] for ref in walk_artifact_refs(run.model_dump(mode="json"))}
    return run, sorted(old_files - kept)


@scoped_verification
def retained_result(space, execution_id):
    """Load a closed result and verify it against the retained current run."""
    marker = space.store().get_meta(PREFIX + execution_id)
    if marker is None:
        return None
    ref = ArtifactRef.model_validate_json(marker)
    result = json.loads(space.read_file(ref))
    row = space.store().get_execution(execution_id)
    run = space.store().read_record_current(row.run_record_id) if row else None
    if (
        result.get("format") != FORMAT
        or result.get("version") != 1
        or result.get("execution_id") != execution_id
        or run is None
        or result.get("run_sha256") != sha256_of(run.model_dump(mode="json"))
    ):
        raise WorkspaceIntegrityError("已结束执行的精简结果与运行记录不一致")
    from ..records.serialize import parse_record
    from .store import verify_artifact_refs

    def verify_records(tx):
        for payload in result["records"]:
            row = tx.conn.execute(
                "SELECT payload FROM records WHERE record_id=? AND revision=?",
                (payload["record_id"], payload["revision"]),
            ).fetchone()
            if row is None:
                raise WorkspaceIntegrityError("已结束执行的科学记录与精简结果不一致")
            saved = json.loads(row[0])
            if saved != payload:
                # Legacy normalization is supported. Unchanged sealed payloads
                # need no repeated scientific model construction on every read.
                try:
                    current = parse_record(row[0])
                except (ValueError, TypeError) as exc:
                    raise WorkspaceIntegrityError("已结束执行的科学记录损坏") from exc
                if current.model_dump(mode="json", by_alias=True) != payload:
                    raise WorkspaceIntegrityError(
                        "已结束执行的科学记录与精简结果不一致"
                    )

    space.store().transaction_read(verify_records)
    records = result["records"]
    problems = verify_artifact_refs(records, space.root)
    if problems:
        raise WorkspaceIntegrityError("已结束执行的引用损坏：" + "; ".join(problems))
    return result


@scoped_verification
def close_extraction(space, execution_id, *, end=False):
    """Publish and verify submissions/results before removing recovery-only history.

    Unsuccessful or interrupted runs require explicit end=True. A closed execution
    remains queryable and scoreable; advancing it cannot initiate more model calls.
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    from ..pipeline.entry import execution_result_refs, get_extraction
    from ..runtime import get_execution
    from ..runtime.execution import verify_execution_inputs
    from .store import record_closure, verify_artifact_refs

    with space.lock():
        existing = retained_result(space, execution_id)
        handle = get_execution(space, execution_id)
        run = space.store().read_record_current(handle.run_ref.record_id)
        if existing is None:
            if run.data.state != "finished" and not end:
                raise ValueError("失败或中断现场须明确结束后才能精简")
            if handle.input_snapshot.options.get("extraction_scope") not in {
                "roster",
                "full",
            }:
                raise ValueError("精简只用于固定PDF提取执行")
            seeds = [run]
            for ref in execution_result_refs(space, execution_id):
                record = space.store().read_record(ref)
                if record is None:
                    raise WorkspaceIntegrityError("最终结果记录缺失，拒绝精简")
                seeds.append(record)
            records = record_closure(seeds, space.store().read_record)
            problems = verify_execution_inputs(space, handle)
            problems.extend(verify_artifact_refs(records, space.root))
            if problems:
                raise WorkspaceIntegrityError("精简前核对失败：" + "; ".join(problems))
            if run.data.state == "finished":
                from .backup import checkpoint_state_problems

                problems = checkpoint_state_problems(space, execution_id)
                if problems:
                    raise WorkspaceIntegrityError(
                        "完成状态核对失败：" + "; ".join(problems)
                    )
            original_run_sha = sha256_of(run.model_dump(mode="json"))
            run, obsolete_files = _compact_calls(space, run)
            pending = space.store().get_meta(PENDING_PREFIX + execution_id)
            if pending:
                journal = json.loads(pending)
                original_run_sha = journal["original_run_sha256"]
                obsolete_files = sorted(
                    set(obsolete_files) | set(journal["obsolete_files"])
                )
            handle = get_execution(space, execution_id)
            records = record_closure([run, *seeds[1:]], space.store().read_record)
            calls = []
            for step in run.data.steps:
                for number, attempt in enumerate(step.attempts, 1):
                    response = (
                        json.loads(space.read_file(attempt.response))
                        if attempt.response
                        else None
                    )
                    if response:
                        from ..llm.managed import _archive_raw

                        _archive_raw(None, response, None)
                    calls.append(
                        {
                            "step_id": step.step_id,
                            "attempt": number,
                            "facts": attempt.model_dump(mode="json"),
                            "response": response,
                        }
                    )
            result = {
                "format": FORMAT,
                "version": 1,
                "execution_id": execution_id,
                "input_digest": handle.input_digest,
                "status": get_extraction(space, execution_id),
                "run_sha256": sha256_of(run.model_dump(mode="json")),
                "original_run_sha256": original_run_sha,
                "obsolete_files": obsolete_files,
                "calls": calls,
                "records": [r.model_dump(mode="json", by_alias=True) for r in records],
                "resumable": False,
            }
            ref = space.publish_run_file(
                execution_id, "result.json", canonical_json(result).encode()
            )
            if json.loads(space.read_file(ref)) != result:
                raise WorkspaceIntegrityError("精简结果读取核对失败")
            # Publication precedes the marker; a crash cannot expose an incomplete result.
            space.store().set_meta(PREFIX + execution_id, ref.model_dump_json())
            retained_result(space, execution_id)

        # The verified marker makes cleanup repeatable after a process interruption.
        kept_paths = space.store().compact_run_history(
            run.record_id, run.revision, execution_id
        )
        conn = space.checkpoint_connection()
        try:
            saver = SqliteSaver(conn)
            saver.setup()
            saver.delete_thread(execution_id)
            conn.execute("VACUUM")
        finally:
            conn.close()
        from .layout import resolve_within

        result = retained_result(space, execution_id)
        for relative in result.get("obsolete_files", []):
            if relative in kept_paths:
                continue
            path = resolve_within(space.root, relative)
            if not relative.startswith(f"runs/{execution_id}/"):
                raise WorkspaceIntegrityError("精简删除范围超出本执行")
            path.unlink(missing_ok=True)
        return retained_result(space, execution_id)
