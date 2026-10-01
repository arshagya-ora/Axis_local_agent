"""Run one real provider/browser fixture through the UI HTTP and SSE contract."""
from __future__ import annotations

import json
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading
import time
from uuid import uuid4

AGENT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(AGENT))
import httpx
import uvicorn
from axis.bootstrap import build_orchestrator
from axis.models import AxisConfig
from axis.ownership import RuntimeOwnership
from axis.ui_runner import Runner
from axis.ui_service import create_app,extension_id
from axis.ui_store import Store
from browser_bridge_client import BrowserBridgeClient
from evals.run import fixture_server,ScopedBridge,fixture_config,check_fixture


def main():
    output=AGENT.parent/'artifacts'/'axis-ui'
    output.mkdir(parents=True,exist_ok=True)
    with RuntimeOwnership(),fixture_server('planner_decision_quality') as (origin,fixture_state),tempfile.TemporaryDirectory(dir=AGENT.parent/'tmp',prefix='axis-real-ui-') as directory:
        bridge=ScopedBridge(BrowserBridgeClient(timeout=30),origin,'planner_decision_quality')
        bridge.rpc('tabs.create',{'url':origin+'/eval/planner_decision_quality','active':False})
        config=fixture_config(AxisConfig.load(),origin,'planner_decision_quality')
        config.run.max_total_steps=8
        config.run.max_model_requests=16
        config.run.max_browser_actions=24
        store=Store(Path(directory)/'history.sqlite3')
        runner=Runner(store,config,factory=lambda cfg,on_event:build_orchestrator(cfg,bridge=bridge,on_event=on_event))
        token=secrets.token_urlsafe(32)
        listener=socket.socket();listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(create_app(store,runner,token=token,origin='chrome-extension://'+extension_id(),port=port),log_level='error',access_log=False))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True);thread.start()
        try:
            deadline=time.monotonic()+5
            while not server.started:
                if time.monotonic()>deadline:raise RuntimeError('Service did not start')
                time.sleep(.02)
            with httpx.Client(base_url=f'http://127.0.0.1:{port}',headers={'Authorization':f'Bearer {token}'},timeout=30) as client:
                cid=uuid4().hex
                client.post('/api/conversations',json={'id':cid}).raise_for_status()
                request={'text':'Read the current fixture page and report its exact heading. This is read-only; do not change or submit anything.','intent':'new_task','client_request_id':uuid4().hex}
                accepted=client.post(f'/api/conversations/{cid}/messages',json=request);accepted.raise_for_status();task_id=accepted.json()['task']['id']
                retry=client.post(f'/api/conversations/{cid}/messages',json=request);retry.raise_for_status();assert retry.json()['task']['id']==task_id
                events=[]
                with client.stream('GET','/api/events?after=0') as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if not line.startswith('data: '):continue
                        event=json.loads(line[6:]);events.append(event)
                        if event.get('type')=='final':break
                snapshot=client.get(f'/api/conversations/{cid}').json()
                task=client.get(f'/api/tasks/{task_id}').json()
                pages=bridge.read_pages()
                verified=check_fixture('planner_decision_quality',task['result'],fixture_state.snapshot(),pages,bridge.calls)
                report={'task':task,'events':events,'message_count':len(snapshot['messages']),'retry_same_task':True,'fixture_checks':verified,'bridge_methods':sorted({call['method'] for call in bridge.calls})}
                (output/'real-fixture-result.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
                print(json.dumps({'status':task['status'],'answer':task['result']['answer'],'fixture_checks':verified,'events':len(events),'task_id':task_id},indent=2),flush=True)
                assert task['status']=='completed',task['current_activity']
        finally:
            server.should_exit=True;thread.join(timeout=8);bridge.close()


if __name__=='__main__':main()
