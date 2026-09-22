"""可信流程适配器平等性回归（R3-02）。

共同基础接口不依赖具体流程身份：同一空间内两个不同 ``flow_id`` 的
合法适配器（其一为仅改 flow_id 的可信子类）经公共接口创建执行后，
首次完成、同适配器重复完成查询、备份、新进程恢复与完成核对都正常；
完成核对凭据由运行层在持流程完成时记录，不反向依赖合成模块。
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

from hvs_catalog.records.runs import ModelSpec
from hvs_catalog.runtime import (
    build_input_snapshot,
    create_execution,
    resume_execution,
)
from hvs_catalog.synthetic import MODEL_NAME, open_service, start_execution
from hvs_catalog.synthetic.flow import SyntheticFlow
from hvs_catalog.synthetic.runner import import_materials
from hvs_catalog.workspace import backup_workspace, initialize_workspace


class OtherTrustedFlow(SyntheticFlow):
    """仅流程身份不同的可信适配器；创建执行起身份保持一致。"""

    flow_id = "acceptance-other-trusted-flow"


@pytest.fixture()
def service(tmp_path):
    svc = open_service(tmp_path / "ledger.db")
    yield svc
    svc.close()


_RESTORE_DRIVER = """
import json
import sys
from pathlib import Path

from hvs_catalog.runtime import resume_execution
from hvs_catalog.synthetic.flow import SyntheticFlow
from hvs_catalog.workspace import restore_workspace


class OtherTrustedFlow(SyntheticFlow):
    flow_id = "acceptance-other-trusted-flow"


backup, target, first_id, second_id = (
    Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
)
restored = restore_workspace(backup, target)
out = {}
for execution_id, flow in (
    (first_id, SyntheticFlow(paper="B")),
    (second_id, OtherTrustedFlow(paper="B")),
):
    report = resume_execution(restored, execution_id, flow, service=None)
    out[execution_id] = [report.outcome, report.mode]
restored.close()
print(json.dumps(out))
"""


def test_two_flow_ids_finish_resume_backup_restore(tmp_path, service):
    """R3-02：两个不同 flow_id 的合法适配器在同一空间全链路互不误伤。"""
    space = initialize_workspace(tmp_path / "space")
    try:
        # 执行一：默认合成流程（B 篇，无歧义一次跑完）
        first, default_flow, _ = start_execution(
            space, paper="B", request_key="flow-default"
        )
        report = resume_execution(
            space, first.execution_id, default_flow, service=service
        )
        assert report.outcome == "finished"

        # 执行二：仅 flow_id 不同的可信子类，经公共 create_execution 建立
        source = import_materials(space, "B")
        snapshot = build_input_snapshot(
            source_refs=[source],
            method="direct",
            models=[
                ModelSpec(step_id=None, provider=None, model=MODEL_NAME, parameters={})
            ],
            expert_adjudication=True,
        )
        other = OtherTrustedFlow(paper="B")
        second, _ = create_execution(
            space, request_key="flow-other", input_snapshot=snapshot, flow=other
        )
        first_report = resume_execution(
            space, second.execution_id, other, service=service
        )
        assert first_report.outcome == "finished"

        # 同一适配器重复完成查询：不因合成重建能力受限而拒绝
        again = resume_execution(space, second.execution_id, other, service=service)
        assert again.outcome == "finished"
        assert again.mode == "already-finished"

        # 备份不传任何适配器：完成核对与流程身份无关
        backup_workspace(space, tmp_path / "backup")
        assert (tmp_path / "backup" / "backup-manifest.json").is_file()

        # 新进程恢复并逐一核对完成
        driver = tmp_path / "restore_driver.py"
        driver.write_text(textwrap.dedent(_RESTORE_DRIVER), encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(driver),
                str(tmp_path / "backup"),
                str(tmp_path / "restored"),
                first.execution_id,
                second.execution_id,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        out = json.loads(result.stdout.strip().splitlines()[-1])
        assert out[first.execution_id] == ["finished", "already-finished"]
        assert out[second.execution_id] == ["finished", "already-finished"]
    finally:
        space.close()
