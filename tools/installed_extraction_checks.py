"""Exercise configuration/prompts and saved mock recovery from an installed wheel."""

from __future__ import annotations

import json
import os
import subprocess

DRIVER = r"""
import json, sys
from pathlib import Path
from hvs_catalog.llm import ModelConfig
from hvs_catalog.llm.prompts import PromptInput
from pydantic import ValidationError
from hvs_catalog.pipeline import (
    PAPER_A, PAPER_B, ExtractionConfig, PipelineMockService, check_extraction_config,
    get_extraction, import_mock_source, preview_extraction_prompt, resume_extraction,
    start_configured_extraction,
)
from hvs_catalog.workspace import initialize_workspace, open_workspace

root, phase = Path(sys.argv[1]), sys.argv[2]
if phase == "create":
    try:
        ExtractionConfig(precedents=[{"precedent_id": "SYN-P1", "version": "synthetic-1", "explanation": "Record the explicit author synthesis.", "conditions": "An explicit synthesis is present.", "boundaries": "Do not infer it by counting models."}])
    except ValidationError:
        pass
    else:
        raise AssertionError("A precedent requires an explicit approved declaration.")
    config = ExtractionConfig(step_models={
        "roster": ModelConfig(model="qwen3.8-flash"),
        "quantities": ModelConfig(model="mimo-v2.6-flash"),
    })
    assert len(check_extraction_config(config)["steps"]) == 2
    preview = preview_extraction_prompt(ExtractionConfig(), "roster", PromptInput(manuscript="FILE synthetic.tex\n1|Synthetic A is unbound."))
    assert preview["prepared"]["payload"]["provider"]["only"] == ["deepseek"]
    assert "S07" in preview["request"]["messages"][0]["content"]
    (root.parent / "config.json").write_text(config.model_dump_json())
    (root.parent / "input.json").write_text('{"manuscript":"FILE synthetic.tex\\n1|A is unbound."}')
    space = initialize_workspace(root)
    source = import_mock_source(space, PAPER_B)
    handle = start_configured_extraction(space, configuration=config, source_refs=[source], request_key="installed-combination")
    assert resume_extraction(space, handle.execution_id, service=PipelineMockService()).outcome == "finished"
    status = get_extraction(space, handle.execution_id)
    assert status["usage"]["tokens"] == 320
    source = import_mock_source(space, PAPER_A)
    pending = start_configured_extraction(space, configuration=ExtractionConfig(), source_refs=[source], request_key="installed-pause")
    assert resume_extraction(space, pending.execution_id, service=PipelineMockService()).outcome == "finished"
    print(json.dumps({"execution_id": pending.execution_id}))
    space.close()
else:
    space = open_workspace(root)
    eid = sys.argv[3]
    assert get_extraction(space, eid)["questions"] == []
    assert resume_extraction(space, eid, service=PipelineMockService()).outcome == "finished"
    assert get_extraction(space, eid)["usage"]["tokens"] == 320
    space.close()
"""


def verify_installed_extraction(venv_dir, work_dir, script) -> list[str]:
    python = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "python"
    root = work_dir / "configured-extraction" / "space"
    root.parent.mkdir()
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    def run(args):
        result = subprocess.run(
            args,
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if result.returncode:
            raise ValueError(result.stderr or result.stdout)
        return json.loads(result.stdout) if result.stdout.strip() else None

    try:
        created = run([str(python), "-c", DRIVER, str(root), "create"])
        run([str(python), "-c", DRIVER, str(root), "resume", created["execution_id"]])
        default = run([str(script), "extract", "config"])
        assert default["default_model"]["model"] == "deepseek-v4.1-flash"
        checked = run(
            [
                str(script),
                "extract",
                "check",
                "--config",
                str(root.parent / "config.json"),
            ]
        )
        assert "method" not in checked["configuration"]
        preview = run(
            [
                str(script),
                "extract",
                "preview",
                "--step",
                "roster",
                "--input",
                str(root.parent / "input.json"),
            ]
        )
        assert preview["request"]["tool"]["name"] == "submit_roster"
        assert "A is unbound" in preview["request"]["messages"][1]["content"]
    except (ValueError, AssertionError, subprocess.TimeoutExpired) as exc:
        return [f"Installed extraction configuration/preview/recovery failed: {exc}"]
    return verify_installed_pdf(venv_dir, work_dir, script)


PDF_DRIVER = r"""
import io, json, os, sys, zipfile
from pathlib import Path
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from pydantic import SecretStr
from hvs_catalog.llm import RuntimeConfig, TokenDanceAdapter
from hvs_catalog.llm.managed import CallService
from hvs_catalog.materials import MaterialsService
from hvs_catalog.materials.mineru import read_result
from hvs_catalog.pipeline import ExtractionConfig, resume_extraction, start_pdf_extraction
from hvs_catalog.pipeline.acquisition import AcquisitionService
from hvs_catalog.evaluation.adapt import prediction_from_execution
from hvs_catalog.workspace import initialize_workspace, open_workspace
from hvs_catalog.workspace.backup import backup_workspace, restore_workspace

root, phase = Path(sys.argv[1]), sys.argv[2]
parallel = len(sys.argv)>3 and sys.argv[3]=='parallel'
expected_calls = 3 if parallel else 2
import threading
b_started = threading.Event()
text = ('This paper selects A and B as Galactic unbound candidates. A RV = -430 +/- 12 km/s.' if parallel else 'This paper selects A as a Galactic unbound candidate. A RV = -430 +/- 12 km/s.')
quote = {'location':'T:p1-b0', 'quote':text}
class Parser:
    def fingerprint(self):
        return {'name':'synthetic-parser','version':'4.0-test','tier':'standard','pages':'all','adapter':'1'}
    def parse(self, path, count):
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as z:
            z.writestr('middle.json',json.dumps({'schema':'docvortex.middle','is_full_document':True,'pages':[{'page_idx':0,'blocks':[{'index':0,'type':'paragraph','bbox':[0.1,0.1,0.9,0.2],'content':text}]}]}))
            z.writestr('manuscript.md',text)
        raw=buffer.getvalue()
        return raw,read_result(raw,count)
class Adapter(TokenDanceAdapter):
    def send(self,payload,runtime,credential):
        tool=payload['tools'][0]['function']
        if parallel and phase=='crash' and tool['name']=='submit_quantities':
            if tool['parameters']['title'].endswith('2'):
                b_started.set()
            else:
                assert b_started.wait(30), 'second object was not dispatched concurrently'
        with (root.parent/'pdf-calls.jsonl').open('a') as out:
            out.write(json.dumps({'tool':tool['name']})+'\n');out.flush();os.fsync(out.fileno())
        if parallel and phase=='crash' and tool['name']=='submit_quantities' and tool['parameters']['title'].endswith('2'):
            threading.Event().wait(60)
        if tool['name']=='submit_roster':
            result={'coverage':{'state':'complete','checked_file_ids':['raw']},'entries':[{'identifiers':[{'value':'A','evidence':[quote]}],'disposition':'included','evidence':[quote],'summary':'This paper selects A.','summary_evidence':[quote],'contribution_type':'candidates_found','type_evidence':[quote],'boundness':'unbound','boundness_evidence':[quote],'conditional_assessments':[]}],'unresolved_groups':[]}
        else:
            result={'measurements':[{'quantity':'observed_phase_space.radial_velocity','value':'-430','error':'12','unit':'km/s','limit_kind':'none','condition':'Reported heliocentric measurement','source':'this_paper','evidence':[quote]}],'quantity_conflicts':[],'coverage':{'common':{'state':'complete'},'exceptions':[]}}
        if parallel and tool['name']=='submit_roster':
            import copy
            other=copy.deepcopy(result['entries'][0]);other['identifiers'][0]['value']='B';other['summary']='This paper selects B.';result['entries'].append(other)
        arguments=json.dumps(result)+('}' if tool['name']=='submit_quantities' else '')
        body={'choices':[{'finish_reason':'tool_calls','message':{'tool_calls':[{'id':'synthetic','type':'function','function':{'name':tool['name'],'arguments':arguments}}]}}],'usage':{'prompt_tokens':20,'completion_tokens':10,'total_tokens':30}}
        return {'status':200,'headers':{},'body':body,'text':json.dumps(body),'events':[]}
if phase=='create':
    writer=PdfWriter();page=writer.add_blank_page(width=612,height=792)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    stream=DecodedStreamObject();stream.set_data(('BT /F1 8 Tf 20 750 Td ('+text+') Tj ET').encode())
    page[NameObject('/Contents')]=writer._add_object(stream)
    pdf=root.parent/'paper.pdf';writer.write(pdf)
    space=initialize_workspace(root)
    source=AcquisitionService(space).import_local_pdf(pdf,arxiv_id='2501.01111v1',provenance='Independent fictional installed-package fixture.')
    prep,report=MaterialsService(space,runner=Parser()).prepare_materials(request_key='prepare',paper=source.canonical_id)
    assert report.outcome=='finished',report.errors
    config=ExtractionConfig(mode='evaluation',runtime=RuntimeConfig(quota_store=root.parent/'pdf-quota.sqlite'))
    handle=start_pdf_extraction(space,preparation_execution_id=prep.execution_id,request_key='extract',configuration=config)
    (root.parent/'pdf-execution.json').write_text(json.dumps({'execution':handle.execution_id,'config':config.model_dump(mode='json')}))
    space.close()
elif phase=='backup':
    space=open_workspace(root)
    report=backup_workspace(space,root.parent/'pdf-backup')
    assert not report.problems,report.problems
    restored=restore_workspace(root.parent/'pdf-backup',root.parent/'restored')
    restored.close();space.close()
elif phase=='verify-backup':
    from hvs_catalog.runtime.execution import get_execution
    from hvs_catalog.records.common import ArtifactRef
    space=open_workspace(root.parent/'restored')
    report=backup_workspace(space,root.parent/'finished-backup')
    assert not report.problems,report.problems
    restored=restore_workspace(root.parent/'finished-backup',root.parent/'finished-restored')
    saved=json.loads((root.parent/'pdf-execution.json').read_text())
    handle=get_execution(restored,saved['execution'])
    restored.read_file(ArtifactRef.model_validate_json(handle.input_snapshot.options['prepared_prompt']))
    run=restored.store().read_record_current(handle.run_ref.record_id)
    assert len(run.data.artifacts)==1
    assert 'surplus root' in restored.read_file(run.data.artifacts[0]).decode()
    assert len((root.parent/'pdf-calls.jsonl').read_text().splitlines())==expected_calls
    restored.close();space.close()
else:
    saved=json.loads((root.parent/'pdf-execution.json').read_text());config=ExtractionConfig.model_validate(saved['config'])
    space=open_workspace(root if phase=='crash' else root.parent/'restored')
    def crash():
        if phase=='crash' and len((root.parent/'pdf-calls.jsonl').read_text().splitlines())==expected_calls:os._exit(73)
    service=CallService(config.runtime,credential=SecretStr('fixture-api-secret-6ab3b39c'),adapter=Adapter(),hooks={'after_archive':crash})
    report=resume_extraction(space,saved['execution'],service=service)
    assert report.outcome==('failed' if parallel else 'finished'),report.errors
    if parallel:assert any('completion_unknown' in e for e in report.errors)
    prediction=prediction_from_execution(space,saved['execution'])
    assert prediction.format_valid and len(prediction.objects)==(2 if parallel else 1)
    assert len((root.parent/'pdf-calls.jsonl').read_text().splitlines())==expected_calls
    final=space.store().read_record_current('pdf-'+saved['execution']+'-paper')
    value=final.data.object_contributions[0].quantities[0].values[0]
    assert (value.value,value.error,value.unit)==('-430','12','km/s')
    assert len(final.data.object_contributions[0].quantity_coverage)==18
    space.close()
"""


def verify_installed_pdf(venv_dir, work_dir, script) -> list[str]:
    python = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "python"
    root = work_dir / "pdf-extraction" / "space"
    root.parent.mkdir()
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    try:
        for phase, code in (
            ("create", 0),
            ("crash", 73),
            ("backup", 0),
            ("recover", 0),
            ("verify-backup", 0),
        ):
            result = subprocess.run(
                [str(python), "-c", PDF_DRIVER, str(root), phase],
                cwd=work_dir,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            if result.returncode != code:
                raise ValueError(result.stderr or result.stdout)
        result = subprocess.run(
            [str(script), "extract", "run", "--help"],
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if result.returncode or "--preparation" not in result.stdout:
            raise ValueError("Installed PDF CLI is missing")
        parallel_root = work_dir / "pdf-extraction-parallel" / "space"
        parallel_root.parent.mkdir()
        for phase, code in (
            ("create", 0),
            ("crash", 73),
            ("backup", 0),
            ("recover", 0),
            ("verify-backup", 0),
        ):
            checked = subprocess.run(
                [str(python), "-c", PDF_DRIVER, str(parallel_root), phase, "parallel"],
                cwd=work_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if checked.returncode != code:
                raise ValueError(checked.stderr or checked.stdout)
        checked = subprocess.run(
            [str(script), "extract", "batch", "--help"],
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if checked.returncode or "--concurrency" not in checked.stdout:
            raise ValueError("Installed parallel batch CLI is missing")

    except (ValueError, subprocess.TimeoutExpired) as exc:
        return [f"Installed PDF numerical extraction/process recovery failed: {exc}"]
    return []
