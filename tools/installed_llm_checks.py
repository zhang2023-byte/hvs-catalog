"""Wheel-only loopback model/CLI verification, outside the source checkout."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

DRIVER = r"""
import json,sys,threading,subprocess,os
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pydantic import SecretStr
import hvs_catalog.llm as llm
from hvs_catalog.cli_llm import synthetic_request
from hvs_catalog.workspace import initialize_workspace,open_workspace
root=Path(sys.argv[1]);root.mkdir(parents=True,exist_ok=True)
script=Path(sys.argv[2])
count=[]
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        req=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        count.append(req)
        message={'content':'SYN-1=42'}
        reason='stop'
        if req.get('tools'):
            message={'content':None,'tool_calls':[{'id':'call-1','type':'function','function':{'name':req['tools'][0]['function']['name'],'arguments':'{"id":"SYN-1","value":42}'}}]}
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
    for p in llm.list_models():
        req=synthetic_request(llm.ModelConfig(model=p['model']))
        handle=llm.start_call(space,req,runtime,request_key=p['model'],credential=SecretStr('wheel-test-key'))
        result=llm.resume_call(space,handle.execution_id,credential=SecretStr('wheel-test-key'))
        assert result['state']=='finished',result
        assert result['response']['result']['parsed']=={'id':'SYN-1','value':42}
        assert llm.resume_call(space,handle.execution_id)['attempt_count']==1
    assert len(count)==4
    space.close()
    config=root/'config.json';config.write_text(llm.CallConfig(model=llm.ModelConfig(model='glm-5.3-flash'),runtime=runtime).model_dump_json())
    env_file=root/'local.env';env_file.write_text('TOKENDANCE_API_KEY=wheel-test-key'+chr(10))
    def cli(*args):
        child=subprocess.run([str(script),*map(str,args)],capture_output=True,text=True,check=False,timeout=30)
        assert child.returncode==0,child.stderr+' '+child.stdout
        return json.loads(child.stdout)
    assert len(cli('models','list'))==4
    cli('models','check','--config',config)
    result=cli('models','probe','--config',config,'--space',root/'cli-space','--request-key','wheel-cli','--env-file',env_file)
    assert result['state']=='finished',result
    cli('models','status','--space',root/'cli-space','--execution',result['execution_id'])
    cli('models','resume','--space',root/'cli-space','--execution',result['execution_id'])
    assert len(count)==5
    assert Path(llm.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()),llm.__file__
    assert (Path(llm.__file__).parent/'STELLA_LICENSE.txt').is_file()
    print('LLM_WHEEL_OK')
finally:
    server.shutdown();server.server_close();thread.join()
"""


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
