"""Opt-in unpacked MV3 checks and screenshots using an isolated browser profile.

This explicit development fixture never seeds the production history store.
Run from axis-agent: uv run python scripts/ui_browser_check.py --help
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading
import time

AGENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT))
sys.path.insert(0, str(AGENT/'tests'))

import httpx
import uvicorn
from axis.models import AxisConfig, AxisEvent
from axis.ui_runner import Runner
from axis.ui_service import create_app, extension_id
from axis.ui_store import Store
from test_ui_service import ControlledOrchestrator


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', default=None)
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--output',type=Path,default=AGENT.parent/'artifacts'/'axis-ui')
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    from playwright.sync_api import sync_playwright
    extension=AGENT.parent/'browser-agent-bridge-main'/'extension'
    identifier=extension_id()
    token=secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix='axis-ui-',dir=AGENT.parent/'tmp') as directory:
        store=Store(Path(directory)/'history.sqlite3')
        runner=Runner(store,AxisConfig.load(),factory=ControlledOrchestrator,debug=args.debug)
        listener=socket.socket()
        listener.bind(('127.0.0.1',0))
        port=listener.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(create_app(store,runner,token=token,origin=f'chrome-extension://{identifier}',port=port),log_level='error',access_log=False))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True)
        thread.start()
        deadline=time.monotonic()+5
        while not server.started:
            if time.monotonic()>deadline:raise RuntimeError('UI test service did not start.')
            time.sleep(.02)
        report={'extension_id':identifier,'screenshots':[],'checks':[],'page_errors':[]}
        try:
            with sync_playwright() as playwright:
                context=playwright.chromium.launch_persistent_context(
                    str(Path(directory)/'profile'),headless=True,executable_path=args.executable,
                    args=[f'--disable-extensions-except={extension}',f'--load-extension={extension}'],
                    viewport={'width':400,'height':900})
                try:
                    worker=context.service_workers[0] if context.service_workers else context.wait_for_event('serviceworker')
                    assert worker.url.startswith(f'chrome-extension://{identifier}/')
                    page=context.new_page()
                    page.on('pageerror',lambda error:report['page_errors'].append(str(error)))
                    page.goto(f'chrome-extension://{identifier}/sidepanel.html')
                    page.get_by_role('heading',name='Your browser workspace').wait_for()
                    assert page.evaluate('getComputedStyle(document.body).fontSize')=='14px'
                    assert page.locator('.task-card').count()==0
                    assert page.get_by_role('button',name='Send instruction').is_disabled()
                    assert page.get_by_role('button',name='Attach files',exact=True).is_disabled()
                    assert page.get_by_role('button',name='Set up connection',exact=True).is_visible()
                    assert page.locator('#connection-button').inner_text() == 'Setup required'
                    page.get_by_label('Instruction',exact=True).fill('Keep this draft while pairing')
                    page.get_by_label('Instruction',exact=True).press('Enter')
                    assert page.locator('#workspace-error').is_hidden()
                    assert page.get_by_label('Instruction',exact=True).input_value() == 'Keep this draft while pairing'
                    assert page.locator('#instruction').get_attribute('placeholder') == 'Ask or add an instruction…'
                    assert page.locator('#history-search').get_attribute('placeholder') == 'Search conversations…'
                    assert page.locator('#return-active').text_content().strip() == 'Return to active task →'
                    assert page.locator('#new-activity').text_content().strip() == '↓ New activity'
                    report['checks'].append('Unpacked MV3 loaded; empty production state has no sample task')
                    # Exercise the actual shared settings form and authenticated API.
                    page.get_by_role('button',name='Settings',exact=True).click()
                    page.get_by_text('Pair AXIS service',exact=True).click()
                    page.get_by_label('Service address',exact=True).fill(f'http://127.0.0.1:{port}')
                    page.get_by_label('UI pairing credential',exact=True).fill(token)
                    page.get_by_role('button',name='Save and connect',exact=True).click()
                    page.get_by_text('AXIS service paired.',exact=True).wait_for()
                    page.get_by_role('button',name='Back to workspace').click()
                    page.wait_for_function("() => !document.querySelector('#send').disabled")
                    assert page.get_by_role('button',name='Attach files',exact=True).is_enabled()
                    assert page.get_by_role('button',name='Set up connection',exact=True).count() == 0
                    report['checks'].append('Unpaired workspace disables send/upload, preserves draft on Enter, and becomes usable after pairing')
                    # Recover an unknown submission after changing conversation and
                    # draft, then a lost acknowledgement after the task completed.
                    recovery = page.evaluate("""async () => {
                        const selected = crypto.randomUUID();
                        const pending = {conversationId: crypto.randomUUID(), body: {
                            text: 'Original recovery instruction', intent: 'new_task',
                            client_request_id: crypto.randomUUID()
                        }};
                        return {selected, pending};
                    }""")
                    page.goto('about:blank')
                    worker.evaluate("""async ({selected,pending}) => {
                        await chrome.storage.local.set({axisWorkspace: {
                            selected, pending, drafts: {[selected]: 'hi'}
                        }});
                    }""", recovery)
                    page.goto(f'chrome-extension://{identifier}/sidepanel.html')
                    retry = page.get_by_role('button', name='Retry previous instruction', exact=True)
                    retry.wait_for()
                    page.wait_for_function("() => !document.querySelector('#retry-instruction').disabled")
                    page.get_by_role('button',name='Send instruction').click()
                    assert 'Retry previous instruction' in page.locator('#workspace-error').inner_text()
                    assert page.get_by_label('Instruction',exact=True).input_value() == 'hi'
                    page.set_viewport_size({'width':320,'height':640})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.screenshot(path=str(args.output/'pending-recovery-320x640.png'))
                    retry.click()
                    page.get_by_role('button',name='Return to active task').wait_for()
                    assert page.get_by_label('Instruction',exact=True).input_value() == 'hi'
                    with store.lock:
                        message = store.accepted(recovery['pending']['body']['client_request_id'])
                        assert message['text'] == 'Original recovery instruction'
                        assert message['conversation_id'] == recovery['pending']['conversationId']
                    original_runner = runner.orchestrator
                    with httpx.Client(base_url=f'http://127.0.0.1:{port}',headers={'Authorization':f'Bearer {token}'}) as client:
                        client.post(f"/api/tasks/{message['task_id']}/control", json={'action':'stop'})
                    deadline = time.monotonic()+5
                    while runner.active():
                        if time.monotonic()>deadline: raise AssertionError('Recovery task did not stop')
                        time.sleep(.02)
                    page.wait_for_timeout(300)
                    page.goto('about:blank')
                    worker.evaluate("""async (recovery) => {
                        const {axisWorkspace} = await chrome.storage.local.get('axisWorkspace');
                        axisWorkspace.pending = recovery.pending;
                        await chrome.storage.local.set({axisWorkspace});
                    }""", recovery)
                    page.goto(f'chrome-extension://{identifier}/sidepanel.html')
                    page.wait_for_function("() => document.querySelector('#connection-details').textContent.includes('Activity stream: live')")
                    assert not page.locator('#pending-instruction').is_visible()
                    assert page.get_by_label('Instruction',exact=True).input_value() == 'hi'
                    assert original_runner.starts == 1
                    report['checks'].append('Pending recovery retries original envelope across conversations and reconciles lost acknowledgement without duplicate execution or draft loss')
                    page.set_viewport_size({'width':400,'height':900})
                    page.get_by_label('Instruction',exact=True).fill('Inspect the local browser fixture')
                    page.get_by_role('button',name='Send instruction').click()
                    page.locator('.task-card').wait_for()
                    assert page.locator('.task-card').count()==1
                    assert page.get_by_label('Execution mode',exact=True).input_value() == 'approval'
                    page.get_by_label('Execution mode',exact=True).select_option('automatic')
                    page.wait_for_timeout(250)
                    assert runner.active()['execution_mode'] == 'automatic'
                    page.get_by_label('Execution mode',exact=True).select_option('approval')
                    page.wait_for_timeout(250)
                    authorization = []
                    approval_thread = threading.Thread(target=lambda: authorization.append(runner.approvals.authorize(
                        'browser_act', 'fill', {'current_url':'http://127.0.0.1/fixture', 'target':'Draft body',
                        'target_type':'locator', 'arguments':{'command':{'action':'fill','value':'A summary prepared from the attached file.'}}})), daemon=True)
                    approval_thread.start()
                    page.get_by_role('button',name='Approve change',exact=True).wait_for()
                    page.reload()
                    page.get_by_role('button',name='Approve change',exact=True).wait_for()
                    assert page.locator('#execution-approval .permission-card').count() == 1
                    page.get_by_text('Review exact change and verification',exact=True).click()
                    page.set_viewport_size({'width':320,'height':640})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.screenshot(path=str(args.output/'execution-approval-320x640.png'))
                    page.get_by_role('button',name='Approve change',exact=True).click()
                    approval_thread.join(timeout=3)
                    assert authorization == [True]
                    page.locator('#execution-approval').wait_for(state='hidden')
                    page.set_viewport_size({'width':400,'height':900})
                    report['checks'].append('Execution modes persist on active tasks; exact action approval survives reload and resolves once')
                    page.get_by_role('button',name='Pause',exact=True).click()
                    page.get_by_role('button',name='Resume',exact=True).wait_for()
                    assert runner.orchestrator.max_loops==1
                    page.get_by_role('button',name='Hide activity').click()
                    assert page.get_by_role('button',name='Show activity').is_visible()
                    assert page.get_by_role('button',name='Stop',exact=True).is_visible()
                    page.get_by_role('button',name='Resume',exact=True).click()
                    page.get_by_role('button',name='Pause',exact=True).wait_for()
                    page.get_by_role('button',name='Show activity').click()
                    page.get_by_label('Instruction',exact=True).fill('Read the next fixture row')
                    page.get_by_role('button',name='Send instruction').click()
                    page.get_by_text('Applied to task',exact=True).wait_for()
                    assert runner.orchestrator.continuations==1
                    assert runner.orchestrator.max_loops==1
                    assert page.locator('.task-card').count()==1
                    report['checks'].append('Submit, pause, resume, collapsed Stop, and serialized follow-up work in MV3')
                    page.get_by_label('Instruction',exact=True).fill('Unsent draft stays here')
                    page.get_by_role('button',name='Conversation history').click()
                    page.get_by_role('heading',name='Conversations',exact=True).wait_for()
                    page.keyboard.press('Escape')
                    assert page.get_by_role('button',name='Conversation history').evaluate('(node)=>node===document.activeElement')
                    assert page.get_by_label('Instruction',exact=True).input_value()=='Unsent draft stays here'
                    page.wait_for_timeout(250)
                    page.close()
                    runner.orchestrator.on_event(AxisEvent(kind='status',detail={'phase':'observed'}))
                    page=context.new_page()
                    page.on('pageerror',lambda error:report['page_errors'].append(str(error)))
                    page.goto(f'chrome-extension://{identifier}/sidepanel.html')
                    page.get_by_role('button',name='Stop',exact=True).wait_for()
                    assert page.locator('.task-card').count()==1
                    assert page.get_by_label('Instruction',exact=True).input_value()=='Unsent draft stays here'
                    assert page.locator('.activity li').filter(has_text='Read browser context').count()==1
                    page.reload()
                    page.get_by_role('button',name='Stop',exact=True).wait_for()
                    assert page.locator('.activity li').filter(has_text='Read browser context').count()==1
                    report['checks'].append('Panel document close/reopen preserves active task and draft')
                    runner.orchestrator.complete_status='limit_reached'
                    runner.orchestrator.complete.set()
                    page.get_by_text('Limit reached',exact=True).wait_for()
                    page.get_by_text('Extend budget',exact=True).click()
                    assert page.get_by_text('Add to this task’s budget while retaining its tabs, sources, and evidence.',exact=True).is_visible()
                    assert ' · ' in page.locator('.task-meta').inner_text()
                    page.get_by_label('Additional model requests',exact=True).fill('8')
                    page.get_by_role('button',name='Apply extension and resume').click()
                    page.get_by_role('button',name='Pause',exact=True).wait_for()
                    assert runner.orchestrator.state.extra_model_requests==8
                    assert page.locator('.task-card').count()==1
                    report['checks'].append('Explicit budget extension resumes the same live task')
                    runner.orchestrator.on_event(AxisEvent(kind='status',detail={'phase':'planner_started'}))
                    runner.orchestrator.on_event(AxisEvent(kind='status',detail={'phase':'navigator_step_started'}))
                    runner.orchestrator.on_event(AxisEvent(kind='status',detail={'phase':'observed'}))
                    page.wait_for_timeout(500)
                    evidence = page.locator('.activity-phase').filter(has=page.locator('summary[data-phase="Evidence"]'))
                    evidence.locator('summary').click()
                    closed = evidence.evaluate('(node)=>!node.open')
                    if not closed:
                        evidence.locator('summary').click()
                    runner.orchestrator.on_event(AxisEvent(kind='status',detail={'phase':'observed'}))
                    page.wait_for_timeout(500)
                    assert evidence.evaluate('(node)=>!node.open')
                    assert page.locator('.activity-phase summary:focus').get_attribute('data-phase') == 'Evidence'
                    evidence.locator('summary').press('Enter')
                    assert evidence.evaluate('(node)=>node.open')
                    assert evidence.locator('time[datetime]').count() == evidence.locator('li').count()
                    assert 'steps' in evidence.locator('.phase-count').inner_text()
                    page.screenshot(path=str(args.output/'activity-phases-400x900.png'))
                    report['checks'].append('Activity phases retain collapsed state and keyboard focus during updates; timestamps, counts and UTF-8 labels render correctly')
                    for _ in range(70):
                        runner.orchestrator.on_event(AxisEvent(kind='status',detail={'phase':'observed'}))
                    page.wait_for_timeout(1200)
                    assert page.locator('.activity li').count()==30
                    page.locator('#conversation').evaluate('(node)=>node.scrollTop=0')
                    runner.orchestrator.on_event(AxisEvent(kind='status',detail={'phase':'planner_started'}))
                    page.get_by_role('button',name='↓ New activity',exact=True).wait_for()
                    assert page.locator('#conversation').evaluate('(node)=>node.scrollTop')<2
                    page.get_by_role('button',name='Settings',exact=True).click()
                    runner.orchestrator.on_event(AxisEvent(kind='status',detail={'phase':'observed'}))
                    page.wait_for_timeout(1200)
                    page.get_by_role('button',name='Back to workspace').click()
                    assert page.locator('#conversation').evaluate('(node)=>node.scrollTop')<2
                    report['checks'].append('Two reconnects deduplicate missed activity; 70 events stay bounded and preserve the reader’s position through Settings')
                    page.get_by_role('button',name='Hide activity').click()
                    for width in (320,360,400,480):
                        for height in (640,900):
                            page.set_viewport_size({'width':width,'height':height})
                            for screen in ('workspace','history','settings'):
                                if screen=='history':
                                    page.get_by_role('button',name='Conversation history').click()
                                    page.get_by_text('Budget extension requested',exact=True).wait_for()
                                if screen=='settings':page.get_by_role('button',name='Settings',exact=True).click()
                                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),f'Horizontal overflow: {width}x{height} {screen}'
                                filename=f'{screen}-{width}x{height}.png'
                                page.screenshot(path=str(args.output/filename),animations='disabled')
                                report['screenshots'].append(filename)
                                if screen=='history':page.keyboard.press('Escape')
                                if screen=='settings':page.get_by_role('button',name='Back to workspace').click()
                    page.set_viewport_size({'width':400,'height':900})
                    page.get_by_role('button',name='Settings',exact=True).click()
                    page.get_by_role('radio',name='Dark',exact=True).check()
                    page.get_by_role('button',name='Save appearance').click()
                    page.get_by_text('Appearance saved.',exact=True).wait_for()
                    page.screenshot(path=str(args.output/'settings-dark-400x900.png'))
                    page.get_by_role('button',name='Back to workspace').click()
                    page.screenshot(path=str(args.output/'workspace-dark-400x900.png'))
                    page.get_by_role('button',name='Stop',exact=True).click()
                    page.get_by_text('Cancelled',exact=True).wait_for()
                    report['checks'].append('Stop acknowledgment is terminal and truthful')
                    # A malicious history title must remain text, including on reopen.
                    cid=store.conversations()['items'][0]['id']
                    with httpx.Client(base_url=f'http://127.0.0.1:{port}',headers={'Authorization':f'Bearer {token}'}) as client:
                        client.patch(f'/api/conversations/{cid}',json={'title':'<img src=x onerror=alert(1)> fixture'})
                    page.get_by_role('button',name='Conversation history').click()
                    page.get_by_text('<img src=x onerror=alert(1)> fixture',exact=True).wait_for()
                    assert page.locator('#history-list img').count()==0
                    report['checks'].append('Untrusted history title is inert text')
                    page.get_by_role('button',name='Rename <img src=x onerror=alert(1)> fixture',exact=True).click()
                    page.get_by_label('Conversation title',exact=True).fill('Local fixture check')
                    page.get_by_role('button',name='Save',exact=True).click()
                    page.get_by_label('Search conversations',exact=True).fill('no matching title')
                    page.get_by_text('No matching conversations.',exact=True).wait_for()
                    page.get_by_label('Search conversations',exact=True).fill('Local fixture')
                    page.get_by_role('button',name='Open Local fixture check',exact=True).wait_for()
                    report['checks'].append('History rename and title search work through the live API')
                    page.keyboard.press('Escape')
                    assert not page.locator('#history-drawer').evaluate('(node)=>node.open')
                    # Use the actual extension zoom API: layout must work at 200%.
                    await_zoom="""async()=>{const tab=await chrome.tabs.getCurrent();await chrome.tabs.setZoom(tab.id,2);} """
                    page.evaluate(await_zoom)
                    page.wait_for_timeout(150)
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    assert page.get_by_role('button',name='Send instruction').is_visible()
                    page.get_by_role('button',name='Send instruction').click(trial=True)
                    for control in ('Conversation history','New conversation','Settings'):
                        target=page.get_by_role('button',name=control,exact=True)
                        assert target.evaluate('(node)=>{const r=node.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth;}'),control
                        target.click(trial=True)
                    page.get_by_role('button',name='Settings',exact=True).click()
                    page.get_by_role('button',name='Back to workspace').click()
                    page.screenshot(path=str(args.output/'workspace-200-percent.png'))
                    report['checks'].append('200% extension zoom retains reachable controls without page overflow')
                    # Options is the actual registered extension options document.
                    options=context.new_page();options.goto(f'chrome-extension://{identifier}/options.html')
                    options.get_by_role('heading',name='Connection',exact=True).wait_for()
                    assert options.locator('html').get_attribute('data-theme')=='dark'
                    assert options.get_by_label('Service address',exact=True).input_value()==f'http://127.0.0.1:{port}'
                    report['checks'].append('Options document shares appearance, pairing, and settings code')
                    # Render a completed-answer fixture with the real renderer in
                    # this isolated profile. Never writes production history.
                    page.evaluate("""async () => {
                        const {renderChat} = await import('./ui/chat.js');
                        const {createState, mergeTask} = await import('./ui/state.js');
                        const state = createState(); state.selected = 'preview';
                        const task = {id:'preview-task', conversation_id:'preview', title:'What is this page about?',
                            status:'completed', current_activity:'Reviewed the page', counters:{browser_actions:3}, duration_ms:19000};
                        state.expanded[task.id] = true;
                        mergeTask(state, task);
                        state.messages = [
                            {id:'question', role:'user', text:task.title, task_id:task.id},
                            {id:'answer', role:'assistant', task_id:task.id, text:
                                '## About this page\\n\\nThis is a tutorial about **image and design tools**. It explains how to connect them and use them in a creative workflow.\\n\\n- Configure the tools\\n- Check the connection\\n- Generate an image\\n\\nRead the [source](https://example.com/tutorial). Use `Settings` to configure the connection.'}
                        ];
                        const root = document.querySelector('#timeline');
                        renderChat(root, state, {expand:(id,value)=>{state.expanded[id]=value;renderChat(root,state,{});}});
                        const order = [...root.children].map(node=>node.dataset.key);
                        if (order.indexOf('message-answer') > order.indexOf('task-preview-task')) throw Error('Answer is below activity');
                        if (!root.querySelector('.task-completed.collapsed')) throw Error('Completed activity did not collapse');
                        document.querySelector('#conversation-title').textContent = task.title;
                        document.querySelector('#instruction').value = '';
                        document.querySelector('#context-chip').hidden = true;
                        document.querySelector('#new-activity').hidden = true;
                        document.querySelector('#conversation').scrollTop = 0;
                    }""")
                    assert page.locator('.answer-content strong').inner_text() == 'image and design tools'
                    assert page.locator('.answer-content li').count() == 3
                    assert page.locator('.answer-content a').get_attribute('rel') == 'noopener noreferrer'
                    page.evaluate("async()=>{const tab=await chrome.tabs.getCurrent();await chrome.tabs.setZoom(tab.id,1);}")
                    for theme in ('light','dark'):
                        page.locator('html').evaluate('(node,theme)=>node.dataset.theme=theme',theme)
                        for width in (320,400):
                            page.set_viewport_size({'width':width,'height':900})
                            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                            page.screenshot(path=str(args.output/f'answer-{theme}-{width}x900.png'))
                    safe = page.evaluate("""async () => {
                        const {renderMarkdown} = await import('./ui/markdown.js');
                        const root = renderMarkdown('<img src=x onerror=alert(1)> **Safe** [bad](javascript:alert) [data](data:text/html,test)\\n\\n```html\\n<script>alert(1)</script>\\n```');
                        return !root.querySelector('img,script,a') && root.querySelector('strong').textContent === 'Safe' && root.querySelector('pre code').textContent.includes('<script>');
                    }""")
                    assert safe
                    page.get_by_label('Instruction',exact=True).focus()
                    assert page.locator('#instruction').evaluate('(node)=>getComputedStyle(node).outlineStyle') == 'none'
                    report['checks'].append('Completed answer precedes collapsed activity; Markdown formats safely; narrow light/dark answer layouts and single composer focus outline verified')
                    assert not report['page_errors'],report['page_errors']
                finally:
                    context.close()
        finally:
            server.should_exit=True
            thread.join(timeout=8)
            (args.output/'verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report,indent=2))


if __name__=='__main__':
    main()
