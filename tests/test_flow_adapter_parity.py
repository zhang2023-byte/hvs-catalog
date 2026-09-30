"""Runtime/backup work with shared extraction and an independent one-node flow."""

import json
import subprocess
import sys

from shared_scenarios import import_materials, open_service, start_execution
from tiny_flow import TinyFlow

from hvs_catalog.runtime import build_input_snapshot, create_execution, resume_execution
from hvs_catalog.workspace import backup_workspace, initialize_workspace

DRIVER = """
import json, sys
from hvs_catalog.runtime import resume_execution
from hvs_catalog.workspace import restore_workspace
from shared_scenarios import direct_flow
from tiny_flow import TinyFlow
space = restore_workspace(sys.argv[1], sys.argv[2])
reports = []
for execution_id, flow in [(sys.argv[3], direct_flow()), (sys.argv[4], TinyFlow())]:
    report = resume_execution(space, execution_id, flow, service=None)
    reports.append([report.outcome, report.mode])
space.close()
print(json.dumps(reports))
"""


def test_independent_adapter_finish_resume_backup_restore(tmp_path):
    space = initialize_workspace(tmp_path / "space")
    try:
        service = open_service(tmp_path / "calls.jsonl")
        first, flow, _ = start_execution(space, paper="B", request_key="extraction")
        assert (
            resume_execution(space, first.execution_id, flow, service=service).outcome
            == "finished"
        )
        second, _ = create_execution(
            space,
            request_key="acknowledgement",
            input_snapshot=build_input_snapshot(
                source_refs=[import_materials(space, "B")]
            ),
            flow=TinyFlow(),
        )
        before = service.summary()["calls"]
        assert (
            resume_execution(
                space, second.execution_id, TinyFlow(), service=service
            ).outcome
            == "finished"
        )
        assert (
            resume_execution(
                space, second.execution_id, TinyFlow(), service=service
            ).mode
            == "already-finished"
        )
        assert service.summary()["calls"] == before
        backup_workspace(space, tmp_path / "backup")
        driver = tmp_path / "restore.py"
        driver.write_text(DRIVER)
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
        assert json.loads(result.stdout) == [["finished", "already-finished"]] * 2
    finally:
        space.close()
