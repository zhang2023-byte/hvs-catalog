"""Verify installed roster correction, process recovery, backups, and capability guards."""

from __future__ import annotations

import os
import subprocess

from installed_pdf_fixture import PDF_FIXTURE

DRIVER = (
    PDF_FIXTURE
    + r'''
import hashlib,subprocess,sys,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pydantic import SecretStr
from hvs_catalog.workspace import initialize_workspace,open_workspace,backup_workspace,restore_workspace
from hvs_catalog.pipeline.acquisition import AcquisitionService
from hvs_catalog.materials import MaterialsService
from hvs_catalog.pipeline import ExtractionConfig,start_extraction,resume_extraction
from hvs_catalog.pipeline.roster import saved_roster,roster_targets
from hvs_catalog.llm import RuntimeConfig
from hvs_catalog.llm.managed import CallService
from hvs_catalog.workspace.archives import read_json
from hvs_catalog.runtime import get_execution
import hvs_catalog.pipeline.roster as module
root=Path(sys.argv[1]); root.mkdir(parents=True)
script=Path(sys.argv[2]); seen=[]
text='SYN-A and SYN-001 identify the same unbound candidate.'
ev=[{'location':'T:p1-b0','quote':text}]
payload={'stars':[{'identifiers':[{'value':x,'evidence':ev} for x in ('SYN-A','SYN-001')],'contribution_type':{'value':'candidates_found','evidence':ev}}],'notes':''}
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        seen.append(request)
        name=request['tools'][0]['function']['name']
        result=({**payload,'notes':None} if len(seen)<3 else payload) if name=='submit_roster' else {'quantities':[{'quantity':'radial_velocity','values':[{'value':'520','evidence':[{'location':'T:p1-b0','quote':'Installed warning quote'}]}]}],'notes':'Numerical note.'}
        delta={'reasoning_content':'Installed synthetic reasoning.','tool_calls':[{'index':0,'id':f'call-{len(seen)}','function':{'name':name,'arguments':json.dumps(result)}}]}
        frame={'id':'wheel-call','choices':[{'index':0,'delta':delta,'finish_reason':'tool_calls'}],'usage':{'total_tokens':10}}
        raw=('data: '+json.dumps(frame)+'\n\n').replace('\\n','\n').encode()
        # Construct actual protocol delimiters, without relying on literal escapes.
        raw=('data: '+json.dumps(frame)).encode()+bytes([10,10])+b'data: [DONE]'+bytes([10,10])
        self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
try:
    space=initialize_workspace(root/'space');pdf=root/'paper.pdf';pdf.write_bytes(make_pdf(text))
    imported=AcquisitionService(space).import_local_pdf(pdf,arxiv_id='2501.03333',version=1,provenance='Synthetic installed roster fixture')
    archive_fixture_metadata(space)
    prep,report=MaterialsService(space,runner=FakeMinerU()).prepare_materials(request_key='prepare',paper=imported.canonical_id)
    assert report.outcome=='finished',report.errors
    runtime=RuntimeConfig(base_url=f'http://127.0.0.1:{server.server_port}',quota_store=root/'quota.db')
    config=ExtractionConfig(runtime=runtime)
    from hvs_catalog.records.common import RecordRef
    original=space
    space=initialize_workspace(root/'reused')
    copied=subprocess.run([str(script),'materials','reuse','--source-space',str(original.root),'--preparation',prep.execution_id,'--space',str(space.root)],check=False,capture_output=True,text=True,timeout=30)
    assert copied.returncode==0,copied.stderr
    source_ref=RecordRef.model_validate(json.loads(copied.stdout))
    archive_fixture_metadata(space)
    metadata=subprocess.run([str(script),'acquisition','metadata','--space',str(space.root),'--id','2501.03333v1'],check=False,capture_output=True,text=True,timeout=30)
    assert metadata.returncode==0,metadata.stderr
    packaged=subprocess.run([str(script),'materials','export','--space',str(space.root),'--source-ref',source_ref.record_id+'@'+str(source_ref.revision),'--output',str(root/'material')],check=False,capture_output=True,text=True,timeout=30)
    assert packaged.returncode==0,packaged.stderr
    space.close()
    space=initialize_workspace(root/'portable')
    loaded=subprocess.run([str(script),'materials','import','--space',str(space.root),'--package',str(root/'material')],check=False,capture_output=True,text=True,timeout=30)
    assert loaded.returncode==0,loaded.stderr
    source_ref=RecordRef.model_validate(json.loads(loaded.stdout))
    assert space.store().list_executions()==[]
    handle=start_extraction(space,prepared_source_ref=source_ref,request_key='extract',configuration=config)
    batch=root/'extraction-batch.json'
    batch.write_text(json.dumps({'tasks':[{'arxiv_id':'2501.03333','workspace':str(space.root),'execution_id':handle.execution_id}]}))
    checked=subprocess.run([str(script),'extract','batch','--manifest',str(batch),'--check-only'],check=False,capture_output=True,text=True,timeout=30)
    assert checked.returncode==0,checked.stderr
    assert seen==[]
    original.close()
    code="""
import os,sys
from pathlib import Path
from pydantic import SecretStr
from hvs_catalog.workspace import open_workspace
from hvs_catalog.runtime import get_execution
from hvs_catalog.pipeline import resume_extraction
from hvs_catalog.pipeline.configuration import stored_configuration
from hvs_catalog.llm.managed import CallService
space=open_workspace(Path(sys.argv[1]));eid=sys.argv[2];count=[0]
def archived():
    count[0]+=1
    if count[0]==2: os._exit(70)
resume_extraction(space,eid,service=CallService(stored_configuration(get_execution(space,eid).input_snapshot).runtime,credential=SecretStr('wheel-synthetic'),hooks={'after_archive':archived}))
"""
    child=subprocess.run([sys.executable,'-c',code,str(space.root),handle.execution_id],check=False,capture_output=True,text=True,timeout=40)
    assert child.returncode==70,child.stderr
    raw_files=list((space.root/'runs'/handle.execution_id).rglob('*.raw'))
    assert len(raw_files)==2
    raw_before={p.relative_to(space.root):p.read_bytes() for p in raw_files}
    report=resume_extraction(space,handle.execution_id,service=CallService(runtime,credential=SecretStr('wheel-synthetic')))
    assert report.outcome=='finished',report.errors
    assert [len(p['messages']) for p in seen]==[2,5,8,3]
    assert seen[2]['messages'][2]['reasoning_content']=='Installed synthetic reasoning.'
    assert seen[2]['messages'][5]['reasoning_content']=='Installed synthetic reasoning.'
    feedback=json.loads(seen[1]['messages'][3]['content'])
    assert feedback['errors']==[{'kind':'schema','path':'/notes','rule':'type','constraint':'string'}]
    assert 'stars and notes' in seen[1]['messages'][4]['content']
    assert 'END LOCATION T:p1-b0' in seen[0]['messages'][1]['content']
    assert saved_roster(space,handle.execution_id).data.stars[0].model_dump()==payload['stars'][0]
    from hvs_catalog.pipeline.results import current_result
    _, manifest=current_result(space,handle.execution_id)
    science=json.loads(space.read_file(manifest.result_ref))
    assert science['notes']==['SYN-A: Numerical note.']
    assert science['stars'][0]['quantities'][0]['values'][0]['value']=='520'
    assert manifest.usage.attempts==4
    audit=json.loads(space.read_file(manifest.evidence_audit_ref))
    assert audit['metadata']['result_ref']==manifest.result_ref.model_dump()
    assert len(audit['quote_mismatches'])==1
    assert audit['quote_mismatches'][0]['quote']=='Installed warning quote'
    assert audit['quote_mismatches'][0]['reason']=='unknown'
    targets=roster_targets(space,handle.execution_id)
    assert targets['targets'][0]['identifiers']==['SYN-A','SYN-001']
    backup_workspace(space,root/'backup');restore_workspace(root/'backup',root/'restored')
    restored=open_workspace(root/'restored')
    assert {p.relative_to(restored.root):p.read_bytes() for p in restored.root.rglob('*.raw')}==raw_before
    assert roster_targets(restored,handle.execution_id)==targets
    assert resume_extraction(restored,handle.execution_id).outcome=='finished'
    assert len(seen)==4
    from hvs_catalog.evaluation.adapt import prediction_from_execution
    from hvs_catalog.evaluation.gold import write_annotation
    from hvs_catalog.records.pdf_contribution import GoldAnnotation
    from hvs_catalog.records.rules import current_rules_ref
    predicted=prediction_from_execution(restored,handle.execution_id)
    pdf_ev=[{'page':1,'location':'Synthetic candidate paragraph'}]
    metadata={**science['metadata'],'rules':current_rules_ref(),'material':{'path':'paper.pdf','sha256':hashlib.sha256(pdf.read_bytes()).hexdigest()}}
    gold=GoldAnnotation.model_validate({'metadata':metadata,'stars':[{'identifiers':[{'value':x,'evidence':pdf_ev} for x in ('SYN-A','SYN-001')],'contribution_type':{'value':'candidates_found','evidence':pdf_ev},'quantities':[{'quantity':'radial_velocity','values':[{'value':'520','evidence':pdf_ev}]}]}],'notes':[]})
    draft=root/'gold-draft';draft.mkdir()
    import shutil
    shutil.copyfile(pdf,draft/'paper.pdf')
    write_annotation(gold,draft/'annotation.json')
    (root/'gold/data').mkdir(parents=True)
    target=root/'gold/data/2501.03333'
    def gold_cli(*args):
        child=subprocess.run([str(script),'gold',*map(str,args)],check=False,capture_output=True,text=True,timeout=30)
        assert child.returncode==0,child.stdout+child.stderr
        return json.loads(child.stdout)
    assert gold_cli('check','--annotation',draft/'annotation.json')['ready_for_review']
    inspected=gold_cli('inspect-save','--annotation',draft/'annotation.json','--target',target)
    saved=gold_cli('save','--annotation',draft/'annotation.json','--target',target,'--expected-new',inspected['annotation_sha256'])
    assert gold_cli('read','--target',target)['annotation_sha256']==saved['annotation_sha256']
    assert {p.name for p in target.iterdir()}=={'annotation.json','paper.pdf'}
    (root/'score.json').write_text(json.dumps({'format':'hvs.scoring-manifest','version':1,'papers':[{'arxiv_id':'2501.03333','reference':str(target/'annotation.json'),'workspace':str(restored.root),'execution_id':handle.execution_id}]}))
    scored=subprocess.run([str(script),'score','run','--manifest',str(root/'score.json'),'--output',str(root/'scored')],check=False,capture_output=True,text=True,timeout=30)
    assert scored.returncode==0,scored.stdout+scored.stderr
    replay=subprocess.run([str(script),'score','replay','--inputs',str(root/'scored/scoring-inputs.json')],check=False,capture_output=True,text=True,timeout=30)
    assert replay.returncode==0,replay.stdout+replay.stderr
    assert scored.stdout==replay.stdout and 'C=1 P=1 G=1' in replay.stdout
    assert len(seen)==4
    bench = root/'benchmark'
    def benchmark_cli(*args):
        child=subprocess.run([str(script),'benchmark','--root',str(bench),*map(str,args)],check=False,capture_output=True,text=True,timeout=30)
        assert child.returncode==0,child.stdout+child.stderr
        return json.loads(child.stdout)
    benchmark_cli('init')
    benchmark_cli('import-materials','--package',root/'material','--paper','2501.03333','--variant','mineru_cloud')
    config_path=root/'benchmark-config.json'
    config_path.write_text(json.dumps(config.model_dump(mode='json')))
    benchmark_cli('preview','--variant','mineru_cloud','--papers','2501.03333','--config',config_path,'--name','offline')
    state=benchmark_cli('prepare','--variant','mineru_cloud','--papers','2501.03333','--config',config_path,'--purpose','Installed offline fixture')
    assert state['id']=='exec01' and state['material_variant']=='mineru_cloud'
    closed=benchmark_cli('close','--round','exec01','--end')
    assert closed['state']=='archived' and closed['papers'][0]['usage']['attempts']==0
    assert benchmark_cli('check','--round','exec01')==closed
    assert not (bench/'sources').exists() and not (bench/'backups').exists()
    assert len(seen)==4
    digest=hashlib.sha256(b'Installed synthetic reasoning.').hexdigest()
    assert (restored.root/'runs/inputs/shared'/f'{digest}.txt').read_text()=='Installed synthetic reasoning.'
    for args in [('gold','--help'),('extract','run','--space',str(root/'absent'),'--preparation','absent','--request-key','guard')]:
        child=subprocess.run([str(script),*args],check=False,capture_output=True,text=True,timeout=30)
        if args[0]=='extract':
            assert child.returncode==2,child.stdout+child.stderr
            assert not (root/'absent').exists()
    assert Path(module.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    restored.close();space.close()
    print('ROSTER_WHEEL_OK')
finally:
    server.shutdown();server.server_close();thread.join()
'''
)


def verify_installed_extraction(venv_dir, work_dir, script):
    python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "TOKENDANCE_API_KEY"):
        env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    result = subprocess.run(
        [str(python), "-c", DRIVER, str(work_dir / "roster"), str(script)],
        cwd=work_dir,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode or "ROSTER_WHEEL_OK" not in result.stdout:
        return [
            f"Installed roster check failed ({result.returncode}): {result.stdout}{result.stderr}"
        ]
    return []
