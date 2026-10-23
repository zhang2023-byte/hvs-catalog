"""Synthetic batch-stage and fresh-process recovery checks on an installed wheel."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

DRIVER = """
import os
from pathlib import Path
from hvs_catalog.cli import main
from hvs_catalog.materials.mineru_official import API_URL, MODEL
assert API_URL == "https://mineru.net/api/v4" and MODEL == "vlm"
import importlib.util
for module in ("_mineru_worker", "_mineru_api_worker", "_mineru_table_worker", "cloud_tables"):
    assert importlib.util.find_spec("hvs_catalog.materials." + module) is None
import hvs_catalog.cli_preparation as cli
from hvs_catalog.pipeline.preparation_batch import PreparationBatchService
from hvs_catalog.pipeline.acquisition import ArxivSearchPage
from hvs_catalog.pipeline.acquisition.protocol import ArxivPaperMeta
class Api:
    def search(self, request):
        assert request.categories == ()
        with Path('batch-calls.txt').open('a') as f:
            f.write(request.terms + '\\n')
        entry = ArxivPaperMeta('1804.00001', 1, 'http://arxiv.org/abs/1804.00001v1', 'Independent fixture', ('Author',), ('physics.gen-ph',), 'physics.gen-ph', '2018-04-25T00:00:00Z', '2026-10-11T00:00:00Z', 'Synthetic abstract', None, None)
        return ArxivSearchPage(request, request.to_request_url(), 1791676800., 1, 0, (entry,), ())
    def metadata_by_ids(self, *args):
        raise AssertionError('discovery must not select PDF versions')
    def download_pdf(self, *args):
        raise AssertionError('discovery must not download')
cli._build_service = lambda space: PreparationBatchService(space, api=Api())
args = ['prepare','discover','--space','batch-space','--batch','installed','--output','batch-reports','--init','--from','2018-04-25','--to','2018-04-30']
for term in ('hypervelocity stars','high velocity stars','unbound stars'):
    args.extend(['--term',term])
if os.environ['BATCH_MODE'] == 'first':
    args.extend(['--max-queries','1'])
assert main(args) == (3 if os.environ['BATCH_MODE'] == 'first' else 0)
"""


def verify_preparation_batches(python: Path, work_dir: Path, script: Path) -> list[str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    for mode in ("first", "resume"):
        result = subprocess.run(
            [str(python), "-c", DRIVER],
            cwd=work_dir,
            env={**env, "BATCH_MODE": mode},
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            return [f"批量检索 {mode}: {result.stdout}{result.stderr}"]
    result = subprocess.run(
        [
            str(script),
            "prepare",
            "status",
            "--space",
            "batch-space",
            "--batch",
            "installed",
            "--output",
            "batch-reports",
        ],
        cwd=work_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return [f"批量清单查询: {result.stdout}{result.stderr}"]
    manifest = json.loads(
        (work_dir / "batch-reports/preparation-manifest.json").read_text()
    )
    calls = (work_dir / "batch-calls.txt").read_text().splitlines()
    expected = ["hypervelocity stars", "high velocity stars", "unbound stars"]
    if (
        calls != expected
        or not manifest["discovery_complete"]
        or manifest["summary"]["unique_papers"] != 1
        or manifest["summary"]["duplicate_entries"] != 2
    ):
        return ["批量恢复／跨词去重不符合独立固定预期"]
    if manifest["summary"]["downloaded"] or manifest["summary"]["prepared"]:
        return ["检索阶段误执行了下载或解析"]
    return []
