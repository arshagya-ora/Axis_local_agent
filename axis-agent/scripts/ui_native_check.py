"""Opt-in real MV3 native-host start/stop and approval restoration checks."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import shutil
import sys
import tempfile
import time

AGENT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(AGENT))
from axis.ui_service import extension_id
from browser_bridge_client import BrowserBridgeClient
from evals.run import fixture_server


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable',default=None)
    args=parser.parse_args()
    from playwright.sync_api import sync_playwright
    output=AGENT.parent/'artifacts'/'axis-ui'
    output.mkdir(parents=True,exist_ok=True)
    extension=AGENT.parent/'browser-agent-bridge-main'/'extension'
    identifier=extension_id()
    report={'checks':[]}
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1',0));native_port=reserve.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='axis-native-ui-',dir=AGENT.parent/'tmp') as directory,fixture_server('planner_decision_quality') as (origin,_),sync_playwright() as playwright:
        # Headless Chrome cannot display its optional-permission dialog. This
        # disposable copy pre-grants exactly those existing optional permissions;
        # every worker, settings, approval and native-host implementation is real.
        test_extension=Path(directory)/'extension'
        shutil.copytree(extension,test_extension,copy_function=shutil.copyfile)
        manifest=json.loads((test_extension/'manifest.json').read_text(encoding='utf-8'))
        manifest['permissions']+=manifest.pop('optional_permissions')
        (test_extension/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        report['permission_fixture']='Existing optional permissions pre-granted in a disposable test manifest; production manifest unchanged.'
        context=playwright.chromium.launch_persistent_context(str(Path(directory)/'profile'),headless=True,executable_path=args.executable,
            args=[f'--disable-extensions-except={test_extension}',f'--load-extension={test_extension}'],viewport={'width':400,'height':900})
        try:
            worker=context.service_workers[0] if context.service_workers else context.wait_for_event('serviceworker')
            # Let the unpacked extension finish its installation initialization.
            worker.evaluate("()=>chrome.storage.local.get('bridgeEnabled')")
            worker.evaluate('(port)=>chrome.storage.local.set({bridgePort:port})',native_port)
            panel=context.new_page();panel.goto(f'chrome-extension://{identifier}/sidepanel.html')
            panel.evaluate("""()=>{window.commandLog=[];const send=chrome.runtime.sendMessage.bind(chrome.runtime);chrome.runtime.sendMessage=(message,...rest)=>{window.commandLog.push(message.type);return send(message,...rest);};}""")
            panel.get_by_role('button',name='Settings',exact=True).click()
            panel.get_by_role('button',name='Start bridge',exact=True).click()
            deadline=time.monotonic()+15
            while panel.evaluate("()=>chrome.runtime.sendMessage({type:'GET_NATIVE_STATUS'})")['status']['state']!='connected':
                if time.monotonic()>deadline:raise RuntimeError('Native host did not connect after Start bridge.')
                panel.wait_for_timeout(100)
            report['checks'].append('Start bridge connected the real native host on an isolated port')
            report['after_start']=panel.evaluate("()=>chrome.runtime.sendMessage({type:'GET_NATIVE_STATUS'})")
            panel.get_by_role('button',name='Back to workspace').click()
            fixture=context.new_page();fixture.goto(origin+'/eval/planner_decision_quality')
            tab_id=worker.evaluate('(url)=>chrome.tabs.query({}).then(tabs=>tabs.find(tab=>tab.url===url).id)',fixture.url)
            client=BrowserBridgeClient(port=native_port,timeout=20)
            report['native_health']=client.health()
            report['before_approval']=panel.evaluate("()=>chrome.runtime.sendMessage({type:'GET_NATIVE_STATUS'})")
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending=pool.submit(client.rpc,'cookies.get',{'tabId':tab_id})
                deadline=time.monotonic()+15
                while not panel.get_by_role('heading',name='Browser bridge approval',exact=True).is_visible():
                    if pending.done():pending.result()
                    if time.monotonic()>deadline:raise RuntimeError('No in-panel approval appeared.')
                    panel.wait_for_timeout(100)
                panel.get_by_role('button',name='Allow once',exact=True).click()
                pending.result(timeout=10)
                report['checks'].append('In-panel approval resolved the real bridge prompt by its original ID')
                panel.close()
                with context.expect_page(timeout=15000) as popup_event:
                    pending=pool.submit(client.rpc,'cookies.get',{'tabId':tab_id})
                popup=popup_event.value
                popup.get_by_role('heading',name='Browser bridge approval',exact=True).wait_for()
                report['checks'].append('Existing fallback approval popup opened while panel was closed')
                panel=context.new_page();panel.goto(f'chrome-extension://{identifier}/sidepanel.html')
                panel.get_by_role('heading',name='Browser bridge approval',exact=True).wait_for()
                panel.screenshot(path=str(output/'approval-restored-400x900.png'),animations='disabled')
                popup.get_by_role('button',name='Allow for session',exact=True).click()
                pending.result(timeout=10)
                panel.get_by_role('heading',name='Browser bridge approval',exact=True).wait_for(state='hidden')
                report['checks'].append('Reopened panel restored the pending prompt and reconciled resolution in the popup')
                client.rpc('cookies.get',{'tabId':tab_id})
                assert not panel.get_by_role('heading',name='Browser bridge approval',exact=True).is_visible()
                report['checks'].append('Allow for session retained the bridge’s existing category semantics')
            panel.get_by_role('button',name='Settings',exact=True).click()
            panel.get_by_role('button',name='Stop bridge',exact=True).click()
            panel.get_by_role('button',name='Start bridge',exact=True).wait_for()
            status=panel.evaluate("()=>chrome.runtime.sendMessage({type:'GET_NATIVE_STATUS'})")
            assert status['status']['state']=='stopped'
            report['checks'].append('Stop bridge shut down the isolated native connection')
        except Exception as error:
            report['error']=str(error)
            if 'panel' in locals() and not panel.is_closed():
                report['commands']=panel.evaluate('()=>window.commandLog')
                report['native_status']=panel.evaluate("()=>chrome.runtime.sendMessage({type:'GET_NATIVE_STATUS'})")
                panel.screenshot(path=str(output/'native-check-failure.png'),animations='disabled')
            raise
        finally:
            context.close()
            (output/'native-verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
