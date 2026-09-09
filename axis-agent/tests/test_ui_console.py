"""Terminal trace parity without exposing diagnostic detail through the UI API."""
from contextlib import nullcontext
import json
from pathlib import Path
import sys

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from axis.models import AxisConfig, AxisEvent, AxisResult
from axis import ui_runner, ui_service
from axis.ui_runner import Runner
from axis.ui_store import Store
from test_ui_service import ControlledOrchestrator, conversation, submit, wait_for, TOKEN, ORIGIN, HEADERS


class TraceOrchestrator(ControlledOrchestrator):
    async def start_task(self, text, *, task_id=None):
        self.state = self.config.new_memory(text)
        self.state.task_id = task_id
        for kind, detail in [
            ('status', {'phase': 'tools_changed', 'capture': True, 'diagnose': False}),
            ('status', {'phase': 'planner_started', 'steps_since_plan': 0}),
            ('planner_decision', {'decision': 'browse', 'reason': 'terminal-only-planner-detail',
                                  'next_goal': 'Read fixture', 'evidence': ['Planner fixture evidence']}),
            ('status', {'phase': 'navigator_step_started', 'goal': 'Read fixture'}),
            ('navigator_step', {'status': 'goal_reached', 'summary': 'terminal-only-navigator-detail',
                                'actions': 1, 'duration_ms': 250, 'browser_ms': 100}),
            ('browser_action', {'tool': 'browser_act', 'operation': 'click', 'skipped': True,
                                'args': {'target': 'terminal-only-tool-argument'}, 'error': 'Fixture policy refusal'}),
            ('final', {'status': 'completed', 'at_ms': 1200}),
        ]:
            self.on_event(AxisEvent(kind=kind, detail=detail))
        return AxisResult(status='completed', answer='Full fixture answer Ω', evidence=['Final fixture evidence'],
                          limitations=['Fixture limitation'], total_steps=2, planner_passes=3,
                          browser_actions=4, model_requests=5, input_tokens=77, output_tokens=11,
                          duration_ms=1200, model_ms=800, browser_ms=200)


@pytest.mark.parametrize('flags, mode', [([], 'quiet'), (['--verbose'], 'verbose'), (['--verbose', '--debug'], 'debug')])
def test_service_command_selects_cli_trace_and_keeps_public_events_separate(tmp_path, monkeypatch, capsys, flags, mode):
    runners = []

    def factory(store, config, **options):
        runner = Runner(store, config, factory=TraceOrchestrator, **options)
        runners.append(runner)
        return runner

    def serve(app, **options):
        assert options['log_level'] == ('debug' if mode == 'debug' else 'warning')
        assert options['access_log'] is False
        pairing = json.loads((tmp_path/'pairing.json').read_text())
        headers = {'Authorization': f"Bearer {pairing['token']}", 'Origin': pairing['origin']}
        with TestClient(app, base_url='http://127.0.0.1:8766', headers=headers) as client:
            cid = conversation(client)
            accepted = submit(client, cid)
            assert accepted.status_code == 202
            wait_for(lambda: runners[0].future.done())
            snapshot = client.get(f'/api/conversations/{cid}').json()
            assert snapshot['tasks'][0]['status'] == 'completed'
            assert snapshot['messages'][-1]['text'] == 'Full fixture answer Ω'
            public = json.dumps(snapshot) + json.dumps(runners[0].store.events(0))
            assert 'terminal-only-' not in public
            assert 'Fixture policy refusal' not in public

    monkeypatch.setattr(ui_service, 'Runner', factory)
    monkeypatch.setattr(ui_service, 'RuntimeOwnership', nullcontext)
    monkeypatch.setattr(ui_service, 'configure_console', lambda: None)
    monkeypatch.setattr(ui_service.uvicorn, 'run', serve)
    ui_service.main(['--data-dir', str(tmp_path), '--extension-id', 'a'*32, *flags])
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert json.loads((tmp_path/'pairing.json').read_text())['token'] not in output
    if mode == 'quiet':
        assert 'terminal-only-' not in output
        assert 'Full fixture answer' not in output
    else:
        for expected in ('terminal-only-planner-detail', 'terminal-only-navigator-detail',
                         'terminal-only-tool-argument', 'Fixture policy refusal', 'Full fixture answer Ω',
                         'Final fixture evidence', 'Limitation: Fixture limitation',
                         'steps=2 planner_passes=3 browser_actions=4 model_requests=5',
                         'tokens: input=77 output=11', 'total=1.2s model=0.8s browser=0.2s other=0.2s'):
            assert expected in output
        if mode == 'debug':
            assert '>> PLANNER' in captured.out
            assert '>> NAVIGATOR STEP' in captured.out
            assert '[SKIP] browser_act.click' in captured.out
            assert '[TOOLS] capture=True diagnose=False' in captured.out
            assert '== FINAL  status=completed' in captured.out
            assert 'operation=start' in captured.out
        else:
            assert 'planner_decision:' in captured.err
            assert '>> PLANNER' not in output


def test_broken_debug_output_does_not_break_history_or_execution(tmp_path, monkeypatch, caplog):
    def broken_output(event):
        raise BrokenPipeError('output pipe closed')

    monkeypatch.setattr(ui_runner, 'print_debug_event', broken_output)
    store = Store(tmp_path/'history.sqlite3')
    runner = Runner(store, AxisConfig.load(), factory=TraceOrchestrator, debug=True)
    with TestClient(ui_service.create_app(store, runner, token=TOKEN, origin=ORIGIN),
                    base_url='http://127.0.0.1:8766', headers=HEADERS) as client:
        cid = conversation(client)
        task_id = submit(client, cid).json()['task']['id']
        wait_for(lambda: runner.future.done())
        assert store.task(task_id)['status'] == 'completed'
        assert runner.storage_error is None
        assert store.events(0)
        assert 'terminal output is unavailable' in caplog.text


@pytest.mark.parametrize('debug', [False, True])
def test_unexpected_failure_traceback_is_opt_in_and_terminal_only(tmp_path, caplog, debug):
    def unavailable_provider(*args, **kwargs):
        raise RuntimeError('LOCAL_ONLY_PROVIDER_DIAGNOSTIC')

    store = Store(tmp_path/'failure.sqlite3')
    runner = Runner(store, AxisConfig.load(), factory=unavailable_provider, debug=debug)
    with TestClient(ui_service.create_app(store, runner, token=TOKEN, origin=ORIGIN),
                    base_url='http://127.0.0.1:8766', headers=HEADERS) as client:
        cid = conversation(client)
        task_id = submit(client, cid).json()['task']['id']
        wait_for(lambda: runner.future.done())
        snapshot = client.get(f'/api/conversations/{cid}').json()
        assert store.task(task_id)['status'] == 'failed'
        assert 'LOCAL_ONLY_PROVIDER_DIAGNOSTIC' not in json.dumps(snapshot)
        assert 'LOCAL_ONLY_PROVIDER_DIAGNOSTIC' not in json.dumps(store.events(0))
        record = next(record for record in caplog.records if record.message.startswith('AXIS UI runner failed'))
        assert bool(record.exc_info) is debug
        assert ('LOCAL_ONLY_PROVIDER_DIAGNOSTIC' in caplog.text) is debug
