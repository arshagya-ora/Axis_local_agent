"""UI seam tests: real orchestrator, transactional API, and controlled race fixtures."""
import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from axis.models import AxisConfig, AxisEvent, AxisResult
from axis.ownership import RuntimeOwnership
from axis.ui_runner import Runner, public_activity
from axis.ui_service import create_app, MessageInput, extension_id
from axis.ui_store import Store
from test_axis_agent import Script, make, CLICK

TOKEN = 'local-test-credential-with-at-least-32-characters'
ORIGIN = 'chrome-extension://' + 'a'*32
HEADERS = {'Authorization': f'Bearer {TOKEN}', 'Origin': ORIGIN}


def wait_for(predicate, timeout=3):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError('Timed out waiting for runner state')


class ControlledOrchestrator:
    def __init__(self, config, on_event):
        self.config, self.on_event = config, on_event
        self.state = None
        self.starts = self.continuations = self.loops = self.max_loops = 0
        self.complete = threading.Event()
        self.blocked = threading.Event()
        self.release = threading.Event()
        self.block_next = False
        self.complete_status = 'completed'

    async def start_task(self, text, *, task_id=None):
        self.starts += 1
        self.state = self.config.new_memory(text)
        self.state.task_id = task_id
        return await self.run_loop()

    async def continue_task(self, text):
        self.continuations += 1
        self.state.add_followup(text)
        self.state.status = 'active'
        return await self.run_loop()

    async def resume_run(self):
        assert self.state.status == 'paused'
        self.state.status = 'active'
        return await self.run_loop()

    async def extend_budget(self, requests, *, steps=0, actions=0):
        assert self.state.status == 'limit_reached'
        self.state.extra_model_requests += requests
        self.state.extra_steps += steps
        self.state.extra_browser_actions += actions
        self.state.status = 'active'
        self.complete.clear()
        return await self.run_loop()

    def pause(self):
        if self.state:
            self.state.status = 'paused'

    def cancel(self):
        if self.state:
            self.state.status = 'cancelled'

    async def run_loop(self):
        self.loops += 1
        self.max_loops = max(self.loops, self.max_loops)
        try:
            self.on_event(AxisEvent(kind='status', detail={'phase': 'planner_started', 'secret': 'not-public'}))
            if self.block_next:
                self.blocked.set()
                self.release.wait(3)  # deliberately synchronous, like a bridge call
            while self.state.status == 'active' and not self.complete.is_set():
                await asyncio.sleep(.005)
            status = self.complete_status if self.state.status == 'active' else self.state.status
            self.state.status = status
            return AxisResult(status=status, answer='Actual fixture outcome' if status == 'completed' else None,
                              reason=status, browser_actions=self.state.counters.browser_actions)
        finally:
            self.loops -= 1


@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path/'history.sqlite3')
    runner = Runner(store, AxisConfig.load(), factory=ControlledOrchestrator)
    app = create_app(store, runner, token=TOKEN, origin=ORIGIN)
    with TestClient(app, base_url='http://127.0.0.1:8766', headers=HEADERS) as client:
        yield client, runner, store


def conversation(client):
    result = client.post('/api/conversations', json={'id': uuid4().hex})
    assert result.status_code == 201
    return result.json()['id']


def submit(client, conversation_id, **kwargs):
    return client.post(f'/api/conversations/{conversation_id}/messages', json={
        'text': 'Read the local fixture', 'intent': 'new_task', 'client_request_id': uuid4().hex, **kwargs})


def test_authentication_origin_host_and_body_limits(service):
    client, _, _ = service
    for path in ['/api/status', '/api/events', '/api/conversations', '/api/settings', '/api/requests/test']:
        assert client.get(path, headers={'Authorization': ''}).status_code == 401
        assert client.get(path, headers={'Origin': 'https://evil.example'}).status_code == 403
        assert client.get(path, headers={'Host': 'evil.example'}).status_code == 403
    assert client.post('/api/conversations', content='x'*33000).status_code == 413
    assert client.options('/api/status', headers={'Authorization': ''}).status_code == 200
    assert client.get('/api/status').headers['access-control-allow-origin'] == ORIGIN
    assert len(extension_id()) == 32


def test_duplicate_busy_and_active_delete(service):
    client, runner, store = service
    cid = conversation(client)
    request_id = uuid4().hex
    accepted = submit(client, cid, client_request_id=request_id)
    assert accepted.status_code == 202
    task_id = accepted.json()['task']['id']
    wait_for(lambda: runner.orchestrator and runner.orchestrator.state)
    duplicate = submit(client, cid, client_request_id=request_id)
    assert duplicate.status_code == 200
    assert duplicate.json()['task']['id'] == task_id
    assert runner.orchestrator.starts == 1
    assert submit(client, cid).json()['code'] == 'busy'
    assert client.delete(f'/api/conversations/{cid}').json()['code'] == 'busy'
    assert runner.orchestrator.state.task_id == task_id
    assert submit(client, cid, client_request_id=request_id, text='Different request').status_code == 409


def test_request_lookup_recovers_acceptance_outside_paging_and_deleted_history(service):
    client, runner, store = service
    cid = conversation(client)
    request_id = uuid4().hex
    path = f'/api/requests/{request_id}'
    assert client.get(path).json() == {'status': 'unknown'}
    assert runner.orchestrator is None
    task_id = submit(client, cid, client_request_id=request_id).json()['task']['id']
    wait_for(lambda: runner.orchestrator and runner.orchestrator.state)
    with store.transaction():
        for index in range(60):
            store.message(cid, task_id, f'Later outcome {index}', None, 'outcome', role='assistant')
    assert all(m['client_request_id'] != request_id for m in store.snapshot(cid)['messages'])
    found = client.get(path).json()
    assert found['status'] == 'accepted'
    assert found['message']['client_request_id'] == request_id
    assert found['task']['id'] == task_id
    assert runner.orchestrator.starts == 1
    client.post(f'/api/tasks/{task_id}/control', json={'action': 'stop'})
    wait_for(lambda: runner.future.done())
    assert client.delete(f'/api/conversations/{cid}').status_code == 200
    assert client.get(path).json() == {'status': 'retired'}


def test_pause_resume_and_followup_are_serial_and_keep_identity(service):
    client, runner, store = service
    cid = conversation(client)
    task_id = submit(client, cid).json()['task']['id']
    wait_for(lambda: runner.orchestrator and runner.orchestrator.state)
    runner.orchestrator.state.counters.browser_actions = 7
    assert client.post(f'/api/tasks/{task_id}/control', json={'action':'pause'}).json()['status'] == 'pausing'
    wait_for(lambda: runner.future.done())
    assert store.task(task_id)['status'] == 'paused'
    assert submit(client, cid).status_code == 409
    client.post(f'/api/tasks/{task_id}/control', json={'action':'resume'})
    wait_for(lambda: runner.orchestrator.loops == 1)
    response = submit(client, cid, intent='follow_up', task_id=task_id, text='Read the next row')
    assert response.status_code == 202
    assert response.json()['message']['delivery'] == 'accepted'
    wait_for(lambda: runner.orchestrator.continuations == 1)
    assert runner.orchestrator.state.task_id == task_id
    assert runner.orchestrator.max_loops == 1
    assert runner.orchestrator.state.counters.browser_actions == 7
    runner.orchestrator.complete.set()
    wait_for(lambda: runner.future.done())
    assert store.task(task_id)['status'] == 'completed'
    assert runner.active_id is None
    message = store.accepted(response.json()['message']['client_request_id'])
    assert message['delivery'] == 'applied'


def test_control_remains_responsive_during_sync_call_and_stop_discards_followup(service):
    client, runner, store = service
    original_factory = runner.factory
    def factory(*args, **kwargs):
        value = original_factory(*args, **kwargs)
        value.block_next = True
        return value
    runner.factory = factory
    cid = conversation(client)
    task_id = submit(client, cid).json()['task']['id']
    wait_for(lambda: runner.orchestrator and runner.orchestrator.blocked.is_set())
    response = submit(client, cid, intent='follow_up', task_id=task_id, text='This must not run')
    assert response.status_code == 202
    started = time.monotonic()
    assert client.get('/api/status').status_code == 200
    assert client.post(f'/api/tasks/{task_id}/control', json={'action':'stop'}).json()['status'] == 'stopping'
    assert time.monotonic()-started < .5
    runner.orchestrator.release.set()
    wait_for(lambda: runner.future.done())
    assert store.task(task_id)['status'] == 'cancelled'
    assert runner.orchestrator.continuations == 0
    assert store.accepted(response.json()['message']['client_request_id'])['delivery'] == 'not_applied'


def test_restart_history_replay_paging_search_rename_and_delete(tmp_path):
    path = tmp_path/'history.sqlite3'
    store = Store(path)
    cid = store.create_conversation()['id']
    task = dict(id=uuid4().hex,conversation_id=cid,title='Saved task',status='active',current_activity='Working')
    with store.transaction():
        store.save_task(task)
        store.message(cid,task['id'],'Remember this',uuid4().hex,'new_task')
        for index in range(80):
            store.update_task(task, 'status', {'text':f'Activity {index}'})
    cursor = store.snapshot(cid)['cursor']
    assert len(store.activity(task['id'])['items']) == 30
    assert store.activity(task['id'])['has_more']
    older = store.activity(task['id'],store.activity(task['id'])['items'][0]['sequence'])
    assert len(older['items']) == 30
    store.close()
    store = Store(path)
    store.interrupt()
    assert store.task(task['id'])['status'] == 'interrupted'
    assert store.events(cursor)[0]['payload']['task']['status'] == 'interrupted'
    runner = Runner(store,AxisConfig.load(),factory=ControlledOrchestrator)
    with TestClient(create_app(store,runner,token=TOKEN,origin=ORIGIN),base_url='http://127.0.0.1:8766',headers=HEADERS) as client:
        assert runner.active_id is None
        assert client.patch(f'/api/conversations/{cid}',json={'title':'Find me'}).status_code == 200
        assert len(client.get('/api/conversations?search=find').json()['items']) == 1
        assert client.get(f'/api/conversations/{cid}').json()['tasks'][0]['status'] == 'interrupted'
        assert client.delete(f'/api/conversations/{cid}').status_code == 200
        assert client.get(f'/api/tasks/{task["id"]}').status_code == 404


def test_real_orchestrator_runs_through_service_and_resume_preserves_budget(tmp_path):
    script = Script([
        {'decision':'browse','next_goal':'Read the search page','success_condition':'Page read'},
        {'decision':'complete','final_answer':'Fixture answer','reason':'Done'}
    ], [{'outcome': {'status':'goal_reached','summary':'Page read'}}])
    orchestrator, bridge, _ = make(script)
    def factory(config,on_event):
        orchestrator.on_event = on_event
        return orchestrator
    store = Store(tmp_path/'real.sqlite3')
    runner = Runner(store,orchestrator.config,factory=factory)
    with TestClient(create_app(store,runner,token=TOKEN,origin=ORIGIN),base_url='http://127.0.0.1:8766',headers=HEADERS) as client:
        cid=conversation(client)
        task_id=submit(client,cid).json()['task']['id']
        wait_for(lambda: runner.future.done())
        snapshot=client.get(f'/api/conversations/{cid}').json()
        assert snapshot['tasks'][0]['status']=='completed'
        assert snapshot['messages'][-1]['text']=='Fixture answer'
        assert orchestrator.state.task_id==task_id
        assert bridge.calls
    paused, _, _ = make(Script([{'decision':'complete','final_answer':'Resumed'}]))
    paused.pause()
    result=asyncio.run(paused.start_task('Read fixture',task_id='stable-task-id'))
    assert result.status=='paused'
    state=paused.state
    state.counters.browser_actions=paused.config.run.max_browser_actions
    result=asyncio.run(paused.resume_run())
    assert result.status=='limit_reached'
    assert paused.state is state
    assert paused.state.task_id=='stable-task-id'


def test_safe_mapping_validated_settings_and_ownership(service,tmp_path):
    client,runner,store=service
    assert 'secret' not in public_activity(AxisEvent(kind='browser_action',detail={'tool':'browser_act','operation':'click','success':True,'args':'secret','reason':'secret'}))
    assert public_activity(AxisEvent(kind='planner_decision',detail={'decision':'browse','reason':'private model reasoning'}))=='Browser step planned'
    assert client.patch('/api/settings',json={'run':{'max_total_steps':0}}).status_code==422
    assert client.patch('/api/settings',json={'run':{'max_total_steps':True}}).status_code==422
    assert client.patch('/api/settings',json={'provider':{'api_key':'secret'}}).status_code==422
    assert client.patch('/api/settings',json={'run':{'max_total_steps':12}}).status_code==200
    assert store.get_settings()['max_total_steps']==12
    with RuntimeOwnership(tmp_path/'owner.lock'):
        with pytest.raises(RuntimeError,match='owned'):
            with RuntimeOwnership(tmp_path/'owner.lock'):
                pass
    with RuntimeOwnership(tmp_path/'owner.lock'):
        pass


def test_storage_callback_failure_is_visible_and_final_is_authoritative(service,monkeypatch):
    client,runner,store=service
    cid=conversation(client)
    task_id=submit(client,cid).json()['task']['id']
    wait_for(lambda: runner.orchestrator and runner.orchestrator.state)
    original=store.update_task
    def fail(*args,**kwargs):
        import sqlite3
        raise sqlite3.OperationalError('disk full')
    monkeypatch.setattr(store,'update_task',fail)
    runner.loop.call_soon_threadsafe(runner._event,AxisEvent(kind='status',detail={'phase':'observed'}))
    wait_for(lambda: runner.storage_error)
    assert client.get('/api/status').json()['storage_error']
    monkeypatch.setattr(store,'update_task',original)
    wait_for(lambda: runner.future.done())


def test_budget_extension_uses_live_state_and_cannot_double_grant(tmp_path):
    config=AxisConfig.load()
    config.run.max_model_requests=1
    script=Script([
        {'decision':'browse','next_goal':'Read fixture','success_condition':'Read page'},
        {'decision':'browse','next_goal':'Read fixture','success_condition':'Read page'},
        {'decision':'complete','final_answer':'Completed with retained sources'}
    ],[{'outcome':{'status':'goal_reached','summary':'Read page'}}])
    orchestrator,bridge,_=make(script,config=config)
    def factory(cfg,on_event):
        orchestrator.on_event=on_event
        return orchestrator
    store=Store(tmp_path/'extend.sqlite3')
    runner=Runner(store,config,factory=factory)
    with TestClient(create_app(store,runner,token=TOKEN,origin=ORIGIN),base_url='http://127.0.0.1:8766',headers=HEADERS) as client:
        cid=conversation(client)
        task_id=submit(client,cid).json()['task']['id']
        wait_for(lambda:runner.future.done())
        assert store.task(task_id)['status']=='limit_reached'
        assert client.get('/api/status').json()['active_task']['id']==task_id
        assert submit(client,cid).status_code==409
        assert submit(client,cid,intent='follow_up',task_id=task_id).status_code==409
        memory=orchestrator.state
        before_requests=int(memory.usage.requests)
        request={'action':'extend','requests':8,'steps':5,'actions':10,'client_request_id':uuid4().hex}
        response=client.post(f'/api/tasks/{task_id}/control',json=request)
        assert response.status_code==200,response.text
        duplicate=client.post(f'/api/tasks/{task_id}/control',json=request)
        assert duplicate.status_code==200,duplicate.text
        wait_for(lambda:runner.future.done())
        assert orchestrator.state is memory
        assert memory.task_id==task_id
        assert memory.extra_model_requests==8
        assert memory.extra_steps==5
        assert memory.extra_browser_actions==10
        assert int(memory.usage.requests)>before_requests
        assert store.task(task_id)['status']=='completed', store.task(task_id)['result']


def test_long_cited_result_survives_history_without_4000_character_clipping(service):
    client,runner,store=service
    cid=conversation(client)
    task_id=submit(client,cid).json()['task']['id']
    wait_for(lambda:runner.orchestrator and runner.orchestrator.state)
    runner.control(task_id,'pause')
    wait_for(lambda:runner.future.done())
    answer='Quoted source note [1]. '*1000+'\n[1] https://example.test/source'
    with runner.lock:
        runner._finish(task_id,AxisResult(status='completed',answer=answer,reason='Complete'))
    snapshot=client.get(f'/api/conversations/{cid}').json()
    assert snapshot['messages'][-1]['text']==answer
    assert snapshot['tasks'][0]['result']['answer']==answer


def test_stop_waits_for_real_synchronous_tool_before_releasing_owner(tmp_path):
    entered,release=threading.Event(),threading.Event()
    script=Script([{'decision':'browse','next_goal':'Click once','success_condition':'Clicked'}],
                  [{'calls':[CLICK],'outcome':{'status':'continue'}}])
    orchestrator,bridge,config=make(script)
    def browser_call():
        entered.set()
        release.wait(3)
        return {'whatChanged':{}}
    bridge.responses['locator.clickRef']=browser_call
    def factory(cfg,on_event):
        orchestrator.on_event=on_event
        return orchestrator
    store=Store(tmp_path/'sync-tool.sqlite3')
    runner=Runner(store,config,factory=factory)
    with TestClient(create_app(store,runner,token=TOKEN,origin=ORIGIN),base_url='http://127.0.0.1:8766',headers=HEADERS) as client:
        cid=conversation(client)
        task_id=submit(client,cid).json()['task']['id']
        approval = wait_for(lambda: runner.active().get('pending_approval'))
        assert client.post(f'/api/tasks/{task_id}/approvals/{approval["id"]}', json={'decision':'approve'}).status_code == 200
        assert entered.wait(2)
        client.post(f'/api/tasks/{task_id}/control',json={'action':'stop'})
        time.sleep(.05)
        assert not runner.future.done()
        assert client.get('/api/status').json()['active_task']['status']=='stopping'
        assert submit(client,cid).status_code==409
        release.set()
        wait_for(lambda:runner.future.done())
        assert store.task(task_id)['status']=='cancelled'
        assert store.task(task_id)['counters']['browser_actions']>=1
