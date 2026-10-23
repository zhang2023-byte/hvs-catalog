"""Wheel-only loopback model/CLI verification, outside the source checkout."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from installed_pdf_fixture import PDF_FIXTURE

DRIVER = (
    PDF_FIXTURE
    + r"""
import io,json,sys,tarfile,threading,subprocess,os
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pydantic import SecretStr
import hvs_catalog.llm as llm
from hvs_catalog.cli_llm import synthetic_request
from hvs_catalog.workspace import initialize_workspace,open_workspace
root=Path(sys.argv[1]);root.mkdir(parents=True,exist_ok=True)
script=Path(sys.argv[2])
count=[]
roster_payload=None
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        req=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        count.append(req)
        message={'content':'SYN-1=42'}
        reason='stop'
        if req.get('tools'):
            name=req['tools'][0]['function']['name']
            arguments=json.dumps(roster_payload) if name=='submit_roster' else '{"id":"SYN-1","value":42}'
            message={'content':None,'tool_calls':[{'id':'call-1','type':'function','function':{'name':name,'arguments':arguments}}]}
            reason='tool_calls'
        body={'id':'wheel-request','model':req['model'],'choices':[{'index':0,'message':message,'finish_reason':reason}],'usage':{'prompt_tokens':8,'completion_tokens':2,'total_tokens':10}}
        if req['stream']:
            chunk={'id':body['id'],'model':body['model'],'choices':[{'index':0,'delta':message,'finish_reason':reason}],'usage':body['usage']}
            raw=('data: '+json.dumps(chunk)+chr(10)*2).encode()+b'data: [DONE]'+bytes([10,10])
        else:raw=json.dumps(body).encode()
        self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
try:
    runtime=llm.RuntimeConfig(base_url=f'http://127.0.0.1:{server.server_port}',quota_store=root/'quota.db')
    space=initialize_workspace(root/'space')
    profiles=llm.list_models()
    assert 'deepseek-v4.1-flash-1017' in {p['model'] for p in profiles}
    for p in profiles:
        req=synthetic_request(llm.ModelConfig(model=p['model']))
        handle=llm.start_call(space,req,runtime,request_key=p['model'],credential=SecretStr('wheel-test-key'))
        result=llm.resume_call(space,handle.execution_id,credential=SecretStr('wheel-test-key'))
        assert result['state']=='finished',result
        assert result['response']['result']['parsed']=={'id':'SYN-1','value':42}
        assert llm.resume_call(space,handle.execution_id)['attempt_count']==1
    assert len(count)==len(profiles)
    space.close()
    config=root/'config.json';config.write_text(llm.CallConfig(model=llm.ModelConfig(model='glm-5.3-flash'),runtime=runtime).model_dump_json())
    env_file=root/'local.env';env_file.write_text('TOKENDANCE_API_KEY=wheel-test-key'+chr(10))
    def cli(*args):
        child=subprocess.run([str(script),*map(str,args)],capture_output=True,text=True,check=False,timeout=30)
        assert child.returncode==0,child.stderr+' '+child.stdout
        return json.loads(child.stdout)
    assert len(cli('models','list'))==len(profiles)
    cli('models','check','--config',config)
    result=cli('models','probe','--config',config,'--space',root/'cli-space','--request-key','wheel-cli','--env-file',env_file)
    assert result['state']=='finished',result
    cli('models','status','--space',root/'cli-space','--execution',result['execution_id'])
    cli('models','resume','--space',root/'cli-space','--execution',result['execution_id'])
    assert len(count)==len(profiles)+1
    # The installed public CLI joins actual preparation and real HTTP model calls.
    # All source content and service output here are synthetic.
    from hvs_catalog.materials import MaterialsService
    from hvs_catalog.pipeline import ExtractionConfig,get_extraction
    from hvs_catalog.pipeline.acquisition.lifecycle import AcquisitionService
    from hvs_catalog.records.common import RecordRef
    from hvs_catalog.records.roster import RosterRecord
    text='This study selects SYN-A and explicitly concludes it is unbound from the Galaxy.'
    package=root/'paper.pdf';package.write_bytes(make_pdf(text))
    space=initialize_workspace(root/'roster-space')
    imported=AcquisitionService(space).import_local_pdf(package,arxiv_id='2501.03333',version=1,provenance='synthetic installed roster check')
    archive_fixture_metadata(space)
    preparation,report=MaterialsService(space,runner=FakeMinerU()).prepare_materials(request_key='roster-prepare',paper=imported.canonical_id)
    assert report.outcome=='finished',report.errors
    bundle=MaterialsService(space).get_prepared_input(preparation.execution_id)
    source=space.store().read_record(RecordRef.model_validate(bundle['prepared_source_ref']))
    evidence={'location':'T:p1-b0','quote':text}
    roster_payload={'stars':[{'identifiers':[{'value':'SYN-A','evidence':[evidence]}],'contribution_type':{'value':'candidates_found','evidence':[evidence]}}],'notes':''}
    prep_id=preparation.execution_id
    config=root/'roster-config.json';config.write_text(ExtractionConfig(default_model=llm.ModelConfig(model='deepseek-v4-flash-0731'),step_models={},runtime=runtime).model_dump_json())
    space.close()
    result=cli('extract','roster','--space',root/'roster-space','--preparation',prep_id,'--request-key','installed-roster','--config',config,'--env-file',env_file)
    assert result['outcome']=='finished' and result['implementation']=='model-extraction',result
    eid=result['execution_id']
    status=cli('extract','status','--space',root/'roster-space','--execution',eid)
    assert status['usage']['attempts']==1,status
    assert cli('extract','resume','--space',root/'roster-space','--execution',eid)['outcome']=='finished'
    assert len(count)==len(profiles)+2
    assert isinstance(count[-1]['messages'][1]['content'],str)
    assert count[-1]['provider']=={'only':['alibaba'],'allow_fallbacks':False}
    space=open_workspace(root/'roster-space')
    status=get_extraction(space,eid)
    records=[space.store().read_record(RecordRef.model_validate(r)) for r in status['result_refs']]
    roster=next(r for r in records if isinstance(r,RosterRecord))
    assert roster.data.stars[0].identifiers[0].value=='SYN-A'
    assert ExtractionConfig().model_steps()==('roster','quantities')
    space.close()
    assert Path(llm.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()),llm.__file__
    assert (Path(llm.__file__).parent/'STELLA_LICENSE.txt').is_file()
    print('LLM_WHEEL_OK')
finally:
    server.shutdown();server.server_close();thread.join()
"""
)


def verify_installed_llm(venv_dir: Path, work_dir: Path, script: Path) -> list[str]:
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "TOKENDANCE_API_KEY"):
        env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    result = subprocess.run(
        [str(python), "-c", DRIVER, str(work_dir / "llm"), str(script)],
        cwd=work_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0 or "LLM_WHEEL_OK" not in result.stdout:
        return [
            f"installed model/CLI loopback check failed ({result.returncode}): {result.stdout}{result.stderr}"
        ]
    return []
