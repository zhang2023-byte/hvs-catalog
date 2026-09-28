"""共享提取入口：来源与可信模拟材料登记的联合绑定（C05）。

成员资格不等于实际材料绑定：快照精确修订的来源标识、受支持修订、
论文标识、来源版本/origin 与材料文件清单/角色/指纹必须与可信 mock
登记一致。支持集内 paper_key 的真实来源错配（换 key、换内容、未登记
新版、别的来源编号）在任何服务调用前拒绝；不回退 v1、不改指其他来
源、不改写快照。合法登记材料照常产出且输出引用等于执行固定修订。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hvs_catalog.pipeline import (
    PAPER_A,
    PAPER_B,
    PAPER_C,
    ExtractionRequest,
    PipelineRequestError,
    StepModel,
    get_extraction,
    import_mock_source,
    resume_extraction,
    start_extraction,
)
from hvs_catalog.pipeline.flow import ExtractionFlow
from hvs_catalog.pipeline.methods import CANDIDATE_REVIEW_METHOD, DIRECT_METHOD
from hvs_catalog.pipeline.mock import PipelineMockService
from hvs_catalog.records.common import RecordRef, build_record
from hvs_catalog.records.paper import PaperRecord
from hvs_catalog.records.roster import RosterRecord
from hvs_catalog.records.runs import ModelSpec
from hvs_catalog.records.source import (
    SourceFile,
    SourcePackageData,
    SourcePackageRecord,
)
from hvs_catalog.runtime import build_input_snapshot, create_execution, resume_execution
from hvs_catalog.synthetic import material as m
from hvs_catalog.workspace import CommitRequest, initialize_workspace


@pytest.fixture()
def space(tmp_path):
    workspace = initialize_workspace(tmp_path / "space")
    yield workspace
    workspace.close()


class SpyService(PipelineMockService):
    def __init__(self):
        super().__init__()
        self.requests: list[dict] = []

    def call(self, *, step, request, material):
        self.requests.append({"step": step, "request": request})
        return super().call(step=step, request=request, material=material)


OTHER_CONTENT = (
    b"A different but correctly hashed synthetic manuscript about another"
    b" object sample.\n"
)


def _commit_new_revision(
    space,
    *,
    source_id: str,
    paper_key: str,
    content: bytes,
    file_rel: str,
    version: str = "fictional-v2",
) -> RecordRef:
    """给既有来源编号提交新修订（内容/标识可变，哈希真实固定）。"""
    file_ref = space.publish_source_file(file_rel, content)
    record = build_record(
        SourcePackageRecord,
        record_id=source_id,
        revision=2,
        created_at=datetime.now(UTC),
        execution_id=None,
        data=SourcePackageData(
            paper_key=paper_key,
            origin={"kind": "synthetic"},
            source_version=version,
            files=[
                SourceFile(
                    file_id="paper",
                    path=file_ref.path,
                    sha256=file_ref.sha256,
                    media_type="text/x-tex",
                    role="main_text",
                    read_state="ready",
                    derived_from=[],
                )
            ],
            gaps=[],
        ),
    )
    space.store().commit(
        CommitRequest(
            operation_key=f"revise:{source_id}:2:{file_ref.sha256[:8]}",
            kind="material_import",
            new_records=[record],
            expected_revisions={source_id: 1},
            result_refs=[record.ref()],
        )
    )
    return RecordRef(record_id=source_id, revision=2)


def _start(space, source_ref, *, request_key, method="direct", expert=False):
    request = ExtractionRequest(
        mode="synthetic",
        method=method,
        source_refs=[source_ref],
        models=[StepModel(model="mock-default")],
        expert_adjudication=expert,
    )
    return start_extraction(space, request=request, request_key=request_key)


def _bypass_execution(space, source_ref, *, method_spec, request_key):
    snapshot = build_input_snapshot(
        source_refs=[source_ref],
        method=method_spec.method_id,
        models=[
            ModelSpec(step_id=None, provider=None, model="mock-default", parameters={})
        ],
        expert_adjudication=False,
    )
    handle, _ = create_execution(
        space,
        request_key=request_key,
        input_snapshot=snapshot,
        flow=ExtractionFlow(method_spec, mode="daily"),
    )
    return handle


def _stored_source_refs(space, handle) -> list[dict]:
    run = space.store().read_record_current(handle.run_ref.record_id)
    return [ref.model_dump(mode="json") for ref in run.data.input_snapshot.source_refs]


# ---- 反例一：A 文件 + 受支持的 B paper_key（换 key 错配） ----


def test_identity_swap_entry_start_rejected(space):
    """src-r05-a v2 保留 A 文件但 paper_key=B：入口建立执行前拒绝。"""
    import_mock_source(space, PAPER_A)
    import_mock_source(space, PAPER_B)
    swapped = _commit_new_revision(
        space,
        source_id="src-r05-a",
        paper_key=PAPER_B,
        content=m.tex_bytes("A"),
        file_rel="r05/a/swapped.tex",
    )
    with pytest.raises(PipelineRequestError, match="登记"):
        _start(space, swapped, request_key="swap-entry")
    assert space.store().list_executions() == []


def test_identity_swap_direct_runtime_rejected_before_service(space):
    """同一错配经直接 runtime 恢复：failed、0 服务调用、快照不变。"""
    import_mock_source(space, PAPER_A)
    import_mock_source(space, PAPER_B)
    swapped = _commit_new_revision(
        space,
        source_id="src-r05-a",
        paper_key=PAPER_B,
        content=m.tex_bytes("A"),
        file_rel="r05/a/swapped.tex",
    )
    handle = _bypass_execution(
        space, swapped, method_spec=DIRECT_METHOD, request_key="swap-rt"
    )
    service = SpyService()
    report = resume_execution(
        space,
        handle.execution_id,
        ExtractionFlow(DIRECT_METHOD, mode="daily"),
        service=service,
    )
    assert report.outcome == "failed"
    assert any("登记" in error for error in report.errors)
    assert service.requests == []
    assert get_extraction(space, handle.execution_id)["result_refs"] == []
    assert _stored_source_refs(space, handle) == [
        {"record_id": "src-r05-a", "revision": 2}
    ]


def test_identity_swap_entry_resume_rejected(space):
    """旁路建立的错配执行经入口恢复：同样 failed 且零调用。"""
    import_mock_source(space, PAPER_A)
    import_mock_source(space, PAPER_B)
    swapped = _commit_new_revision(
        space,
        source_id="src-r05-a",
        paper_key=PAPER_B,
        content=m.tex_bytes("A"),
        file_rel="r05/a/swapped.tex",
    )
    handle = _bypass_execution(
        space, swapped, method_spec=CANDIDATE_REVIEW_METHOD, request_key="swap-resume"
    )
    service = SpyService()
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "failed"
    assert service.requests == []
    assert get_extraction(space, handle.execution_id)["result_refs"] == []


# ---- 反例二：paper_key 不变但文件换成另一份正确哈希内容 ----


def test_same_paper_new_content_direct_runtime_rejected(space):
    """src-r05-b v2 换另一份合成内容：不得回退 B v1 静态应答。"""
    import_mock_source(space, PAPER_B)
    revised = _commit_new_revision(
        space,
        source_id="src-r05-b",
        paper_key=PAPER_B,
        content=OTHER_CONTENT,
        file_rel="r05/b/revised.tex",
    )
    handle = _bypass_execution(
        space, revised, method_spec=DIRECT_METHOD, request_key="content-rt"
    )
    service = SpyService()
    report = resume_execution(
        space,
        handle.execution_id,
        ExtractionFlow(DIRECT_METHOD, mode="daily"),
        service=service,
    )
    assert report.outcome == "failed"
    assert any("登记" in error for error in report.errors)
    assert service.requests == []
    assert get_extraction(space, handle.execution_id)["result_refs"] == []
    # 不改写快照、不退回 v1
    assert _stored_source_refs(space, handle) == [
        {"record_id": "src-r05-b", "revision": 2}
    ]


def test_same_paper_new_content_entry_start_rejected(space):
    import_mock_source(space, PAPER_B)
    revised = _commit_new_revision(
        space,
        source_id="src-r05-b",
        paper_key=PAPER_B,
        content=OTHER_CONTENT,
        file_rel="r05/b/revised.tex",
    )
    with pytest.raises(PipelineRequestError, match="登记"):
        _start(space, revised, request_key="content-entry")
    assert space.store().list_executions() == []


# ---- 未登记修订：内容与登记完全相同也不得假装有静态应答 ----


def test_unregistered_revision_identical_content_rejected(space):
    """src-r05-b v2 与 v1 内容逐字相同：仍按未登记修订拒绝。"""
    import_mock_source(space, PAPER_B)
    revised = _commit_new_revision(
        space,
        source_id="src-r05-b",
        paper_key=PAPER_B,
        content=m.tex_bytes("B"),
        file_rel=m.source_relative_path("B"),
        version="fictional-v1",
    )
    handle = _bypass_execution(
        space, revised, method_spec=DIRECT_METHOD, request_key="identical-rt"
    )
    service = SpyService()
    report = resume_execution(
        space,
        handle.execution_id,
        ExtractionFlow(DIRECT_METHOD, mode="daily"),
        service=service,
    )
    assert report.outcome == "failed"
    assert any("修订" in error for error in report.errors)
    assert service.requests == []
    with pytest.raises(PipelineRequestError, match="登记"):
        _start(space, revised, request_key="identical-entry")


# ---- 同 paper_key 的其他来源身份 / 文件指纹错配 ----


def _fresh_source(space, *, record_id: str, paper_key: str, content: bytes, rel: str):
    file_ref = space.publish_source_file(rel, content)
    record = build_record(
        SourcePackageRecord,
        record_id=record_id,
        created_at=datetime.now(UTC),
        execution_id=None,
        data=SourcePackageData(
            paper_key=paper_key,
            origin={"kind": "synthetic"},
            source_version="fictional-v1",
            files=[
                SourceFile(
                    file_id="paper",
                    path=file_ref.path,
                    sha256=file_ref.sha256,
                    media_type="text/x-tex",
                    role="main_text",
                    read_state="ready",
                    derived_from=[],
                )
            ],
            gaps=[],
        ),
    )
    space.store().commit(
        CommitRequest(
            operation_key=f"import:{record_id}",
            kind="material_import",
            new_records=[record],
            result_refs=[record.ref()],
        )
    )
    return record.ref()


def test_other_source_identity_same_paper_key_rejected(space):
    """src-candidate-c-alt 冒用 C paper_key 与正确文件：来源身份不一致拒绝。"""
    import_mock_source(space, PAPER_C)
    from hvs_catalog.pipeline.mock import c_tex_bytes

    fake = _fresh_source(
        space,
        record_id="src-candidate-c-alt",
        paper_key=PAPER_C,
        content=c_tex_bytes(),
        rel="candidate/c-alt/paper.tex",
    )
    with pytest.raises(PipelineRequestError, match="来源标识"):
        _start(space, fake, request_key="alt-id")
    assert space.store().list_executions() == []


def test_file_fingerprint_mismatch_rejected(space):
    """文件换成另一份正确哈希内容：指纹与登记不一致拒绝。"""
    import_mock_source(space, PAPER_C)
    mismatched = _fresh_source(
        space,
        record_id="src-candidate-c-alt2",
        paper_key=PAPER_C,
        content=OTHER_CONTENT,
        rel="candidate/c-alt2/paper.tex",
    )
    with pytest.raises(PipelineRequestError, match="登记"):
        _start(space, mismatched, request_key="fingerprint")
    assert space.store().list_executions() == []


# ---- 合法登记材料：输出引用等于执行固定修订 ----


def test_legal_materials_bind_outputs_to_frozen_revision(space):
    """A/B/C 正常运行的名单与论文 source_ref 都等于执行固定修订。"""
    source_a = import_mock_source(space, PAPER_A)
    handle_a = _start(space, source_a, request_key="legal-a")
    report = resume_extraction(
        space, handle_a.execution_id, service=PipelineMockService()
    )
    assert report.outcome == "finished"
    frozen = {"record_id": "src-r05-a", "revision": 1}
    roster = space.store().read_record_current(
        f"roster-fic-r05-a-{handle_a.execution_id}"
    )
    paper = space.store().read_record_current(
        f"paper-fic-r05-a-{handle_a.execution_id}"
    )
    assert roster.data.source_ref.model_dump(mode="json") == frozen
    assert paper.data.source_ref.model_dump(mode="json") == frozen

    source_c = import_mock_source(space, PAPER_C)
    handle_c = _start(space, source_c, request_key="legal-c", method="candidate_review")
    report_c = resume_extraction(
        space, handle_c.execution_id, service=PipelineMockService()
    )
    assert report_c.outcome == "finished"
    frozen_c = {"record_id": "src-candidate-c", "revision": 1}
    roster_c = space.store().read_record_current(
        f"roster-fic-candidate-c-{handle_c.execution_id}"
    )
    paper_c = space.store().read_record_current(
        f"paper-fic-candidate-c-{handle_c.execution_id}"
    )
    assert roster_c.data.source_ref.model_dump(mode="json") == frozen_c
    assert paper_c.data.source_ref.model_dump(mode="json") == frozen_c
    assert isinstance(roster_c, RosterRecord) and isinstance(paper_c, PaperRecord)
