"""Public display categories must not expose model or browser arguments."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from axis.models import AxisEvent
from axis.ui_runner import public_phase


@pytest.mark.parametrize('kind,detail,expected', [
    ('planner_decision', {'decision': 'browse'}, 'Planning'),
    ('status', {'phase': 'planner_started'}, 'Planning'),
    ('status', {'phase': 'observed'}, 'Evidence'),
    ('status', {'phase': 'document_read'}, 'Evidence'),
    ('browser_action', {'tool': 'browser_capture_evidence'}, 'Evidence'),
    ('browser_action', {'tool': 'browser_act'}, 'Browsing'),
    ('navigator_step', {'status': 'continue'}, 'Browsing'),
    ('status', {'phase': 'tab_create'}, 'Browsing'),
    ('status', {'phase': 'unknown'}, 'Activity'),
])
def test_public_phase(kind, detail, expected):
    detail.update(reason='private reasoning', args={'secret': 'private argument'})
    assert public_phase(AxisEvent(kind=kind, detail=detail)) == expected
