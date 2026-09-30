"""业务库：保存与按准确修订读取、事务原子性、操作键重放与当前输出一致（A05/A06/A14）。

记录内容来自共同记录的合成夹具（虚构材料），路径改写后发布到空间 sources/ 下。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from records_helpers import load_fixture
from workspace_helpers import publish_fixture_collection

from hvs_catalog.records import build_record, current_rules_ref
from hvs_catalog.records.runs import InputSnapshot, RunData, RunRecord, Usage
from hvs_catalog.workspace import (
    CommitRequest,
    OperationKeyConflictError,
    RevisionStaleError,
    WorkspaceIntegrityError,
    initialize_workspace,
)

STAMP = datetime(2026, 9, 21, tzinfo=UTC)


@pytest.fixture()
def space(tmp_path):
    space = initialize_workspace(tmp_path / "space")
    yield space
    space.close()


def _commit(space, records, key, **kwargs):
    # 问题记录必须与步骤绑定同事务提交；测试默认绑定到固定步骤名
    kwargs.setdefault(
        "question_steps",
        {r.record_id: "fixture-step" for r in records if r.RECORD_NAME == "question"},
    )
    request = CommitRequest(
        operation_key=key,
        kind=kwargs.pop("kind", "manual"),
        new_records=records,
        **kwargs,
    )
    return space.store().commit(request)


def _collection(space, names=("gap-conflict", "numeric-forms")):
    records: list = []
    for name in names:
        records.extend(publish_fixture_collection(space, load_fixture(name)))
    return records


def test_commit_and_read_back_all_ten_record_types_by_exact_revision(space):
    records = _collection(space)
    receipt = _commit(space, records, "op-init")
    assert not receipt.replayed
    assert receipt.operation_key == "op-init"
    store = space.store()
    types = {record.RECORD_NAME for record in records}
    assert {
        "source_package",
        "evidence",
        "paper",
        "roster",
        "question",
        "decision",
        "object",
        "external_record",
        "run",
        "release",
    } == types & {
        "source_package",
        "evidence",
        "paper",
        "roster",
        "question",
        "decision",
        "object",
        "external_record",
        "run",
        "release",
    }
    for record in records:
        loaded = store.read_record(record.ref())
        assert loaded == record
    latest: dict[str, int] = {}
    for record in records:
        latest[record.record_id] = max(latest.get(record.record_id, 0), record.revision)
    for record_id, revision in latest.items():
        assert store.current_revision(record_id) == revision
    # 查询不存在的修订不会隐式回退到最新修订
    missing = store.read_record(
        records[0].ref().model_copy(update={"revision": records[0].revision + 5})
    )
    assert missing is None


def test_old_revision_and_its_references_are_preserved_after_new_revision(space):
    records = _collection(space, names=("gap-conflict",))
    _commit(space, records, "op-init")
    store = space.store()
    roster = next(r for r in records if r.RECORD_NAME == "roster")
    papers = sorted(
        (r for r in records if r.RECORD_NAME == "paper"), key=lambda r: r.revision
    )
    paper = papers[-1]  # 夹具论文已含两个修订；在当前修订上继续
    paper_current = store.current_revision(paper.record_id)
    assert paper_current == paper.revision

    # 名单形成新修订；论文不随之更新时整体拒绝（当前输出不一致）
    roster_v2 = roster.model_copy(update={"revision": 2, "created_at": STAMP})
    with pytest.raises(WorkspaceIntegrityError, match="当前输出不一致"):
        _commit(
            space,
            [roster_v2],
            "op-roster-v2-alone",
            expected_revisions={roster.record_id: 1},
        )
    paper_v_next = paper.model_copy(
        update={
            "revision": paper_current + 1,
            "created_at": STAMP,
            "data": paper.data.model_copy(
                update={
                    "roster_ref": paper.data.roster_ref.model_copy(
                        update={"revision": 2}
                    )
                }
            ),
        }
    )
    receipt = _commit(
        space,
        [roster_v2, paper_v_next],
        "op-roster-v2-with-paper",
        expected_revisions={roster.record_id: 1, paper.record_id: paper_current},
    )
    assert not receipt.replayed

    # 旧修订原样保留：数值字符串与引文不因新修订被替换
    old_roster = store.read_record(roster.ref())
    assert old_roster == roster
    old_paper = store.read_record(paper.ref())
    assert old_paper.data.roster_ref.revision == paper.data.roster_ref.revision
    # 准确读取旧引用得到旧内容；不存在隐式“最新版本”替换
    assert store.read_record_current(roster.record_id).revision == 2
    assert store.read_record_current(paper.record_id).revision == paper_current + 1


def test_batch_is_atomic_when_validation_fails(space):
    records = _collection(space, names=("gap-conflict",))
    paper = next(r for r in records if r.RECORD_NAME == "paper")
    # 一个引用损坏的论文记录：引用不存在的名单修订
    broken = paper.model_copy(
        update={
            "record_id": "broken-paper",
            "revision": 1,
            "data": paper.data.model_copy(
                update={
                    "roster_ref": paper.data.roster_ref.model_copy(
                        update={"revision": 99}
                    )
                }
            ),
        }
    )
    broken_records = list(records) + [broken]
    with pytest.raises(WorkspaceIntegrityError, match="未通过校验"):
        _commit(space, broken_records, "op-broken")
    # 事务整体未提交：库里没有任何记录，也没有留下回执
    store = space.store()
    assert store.list_records() == []
    assert store.find_receipt("op-broken") is None


def test_expected_revision_stale_rejects_and_keeps_state(space):
    records = _collection(space, names=("gap-conflict",))
    _commit(space, records, "op-init")
    evidence = next(r for r in records if r.RECORD_NAME == "evidence")
    evidence_v2 = evidence.model_copy(update={"revision": 2, "created_at": STAMP})
    with pytest.raises(RevisionStaleError, match="预期当前修订"):
        _commit(
            space,
            [evidence_v2],
            "op-stale",
            expected_revisions={evidence.record_id: 5},
        )
    assert space.store().current_revision(evidence.record_id) == 1
    ok = _commit(
        space,
        [evidence_v2],
        "op-fresh",
        expected_revisions={evidence.record_id: 1},
    )
    assert not ok.replayed
    # 旧预期修订的并发更新（当前已是 2）被拒绝，旧内容不能覆盖新修订
    with pytest.raises(RevisionStaleError):
        _commit(
            space,
            [evidence.model_copy(update={"revision": 3, "created_at": STAMP})],
            "op-late",
            expected_revisions={evidence.record_id: 1},
        )


def test_same_operation_key_replay_returns_original_receipt(space):
    records = _collection(space, names=("gap-conflict",))
    first = _commit(space, records, "op-key-1")
    replay = _commit(space, records, "op-key-1")
    assert replay.replayed
    assert replay.created_at == first.created_at
    assert replay.content_digest == first.content_digest
    # 重放不增加新修订
    assert len(space.store().list_records()) == len(records)
    # 同键异内容拒绝
    changed = [records[0].model_copy(update={"record_id": "other"})]
    with pytest.raises(OperationKeyConflictError, match="不同内容"):
        _commit(space, changed, "op-key-1")
    # 同内容换操作键：记录已存在且内容相同，作为无变化提交返回回执
    duplicate = _commit(space, records, "op-key-2")
    assert duplicate.replayed
    assert len(space.store().list_records()) == len(records)
    # 相同编号与修订只能保存相同内容
    tampered = [
        record.model_copy(
            update={
                "data": record.data.model_copy(
                    update={
                        next(
                            field
                            for field in ("paper_key", "prompt", "execution_id")
                            if hasattr(record.data, field)
                        ): "tampered"
                    }
                )
            }
        )
        if record.record_id == records[0].record_id
        else record
        for record in records
    ]
    with pytest.raises(WorkspaceIntegrityError, match="相同内容"):
        _commit(space, tampered, "op-tamper")


def test_roster_revision_requires_referencing_paper_update(space):
    """A14 存储层：新名单必须与引用它的论文一起更新，缺一整体拒绝。"""
    records = _collection(space, names=("gap-conflict",))
    _commit(space, records, "op-init")
    roster = next(r for r in records if r.RECORD_NAME == "roster")

    roster_v2 = roster.model_copy(update={"revision": 2, "created_at": STAMP})
    with pytest.raises(WorkspaceIntegrityError, match="当前输出不一致"):
        _commit(
            space,
            [roster_v2],
            "op-missing-paper",
            expected_revisions={roster.record_id: 1},
        )
    # 整体拒绝：名单修订没有落库，不能靠保留旧记录绕过对应检查
    assert space.store().current_revision(roster.record_id) == 1
    assert space.store().find_receipt("op-missing-paper") is None


def test_run_record_artifact_references_are_verified(space):
    """运行归档文件引用由本层核验：缺失或哈希改变时拒绝提交。"""
    snapshot = InputSnapshot(
        source_refs=[],
        rules=current_rules_ref(),
        method=None,
        models=[],
        code_version="test-code",
        dependency_versions={},
        expert_adjudication=True,
        prompt_refs=[
            {
                "path": "runs/exec-x/missing-prompt.md",
                "sha256": "0" * 64,
            }
        ],
        example_refs=[],
        precedent_refs=[],
        existing_decision_refs=[],
    )
    run = build_record(
        RunRecord,
        record_id="run-broken",
        created_at=STAMP,
        execution_id="exec-x",
        data=RunData(
            execution_id="exec-x",
            mode="synthetic",
            input_snapshot=snapshot,
            state="created",
            steps=[],
            result_refs=[],
            model_judgments=[],
            usage=Usage(tokens=None, cost=None, currency=None),
            errors=[],
        ),
    )
    with pytest.raises(WorkspaceIntegrityError, match="缺失或不可读"):
        _commit(space, [run], "op-run-missing")
    # 文件存在但哈希不符
    ref = space.publish_run_file("exec-x", "missing-prompt.md", b"actual prompt")
    snapshot_ok = snapshot.model_copy(
        update={"prompt_refs": [ref.model_copy(update={"sha256": "1" * 64})]}
    )
    run_wrong_hash = run.model_copy(
        update={"data": run.data.model_copy(update={"input_snapshot": snapshot_ok})}
    )
    with pytest.raises(WorkspaceIntegrityError, match="哈希与记录不符"):
        _commit(space, [run_wrong_hash], "op-run-hash")
    good = run.model_copy(
        update={
            "data": run.data.model_copy(
                update={
                    "input_snapshot": snapshot.model_copy(update={"prompt_refs": [ref]})
                }
            )
        }
    )
    receipt = _commit(space, [good], "op-run-good")
    assert not receipt.replayed
