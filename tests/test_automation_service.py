import json
import time
from datetime import datetime, timezone

from services.automation_service import AutomationService, next_run_for


def test_daily_schedule_moves_to_the_next_day_after_boundary():
    now = datetime(2026, 9, 7, 3, 0, tzinfo=timezone.utc)  # 08:30 in Kolkata
    next_run = next_run_for({'type': 'daily', 'time': '08:00', 'timezone': 'Asia/Kolkata'}, now)
    assert next_run == datetime(2026, 9, 8, 2, 30, tzinfo=timezone.utc)


def test_weekly_schedule_uses_requested_weekday_and_timezone():
    now = datetime(2026, 9, 7, 1, 0, tzinfo=timezone.utc)  # Monday 06:30 in Kolkata
    next_run = next_run_for({'type': 'weekly', 'weekday': 0, 'time': '09:00', 'timezone': 'Asia/Kolkata'}, now)
    assert next_run == datetime(2026, 9, 7, 3, 30, tzinfo=timezone.utc)


def test_automation_crud_persists_per_user(tmp_path):
    path = tmp_path / 'automations.json'
    service = AutomationService(path)
    created = service.create('person@example.com', {
        'name': 'Morning brief',
        'schedule': {'type': 'daily', 'time': '08:00', 'timezone': 'Asia/Kolkata'},
        'action': {'goal': 'Check important Gmail and Calendar.'},
    })
    assert service.get(created['id'], 'other@example.com') is None
    assert AutomationService(path).get(created['id'], 'person@example.com')['name'] == 'Morning brief'

    updated = service.update(created['id'], 'person@example.com', {'enabled': False})
    assert updated['enabled'] is False
    assert updated['next_run'] is None
    assert service.delete(created['id'], 'person@example.com') is True
    assert service.list('person@example.com') == []


def test_run_now_invokes_runner_and_records_last_status(tmp_path):
    calls = []
    service = AutomationService(tmp_path / 'automations.json')
    service.set_runner(lambda automation: calls.append(automation['id']))
    created = service.create('person@example.com', {
        'name': 'Deadline watch',
        'schedule': {'type': 'interval', 'minutes': 60},
        'action': {'goal': 'Check deadlines.'},
    })
    assert service.run_now(created['id'], 'person@example.com')['queued'] is True
    for _ in range(50):
        saved = service.get(created['id'], 'person@example.com')
        if saved.get('last_status'):
            break
        time.sleep(0.01)
    assert calls == [created['id']]
    assert saved['last_status'] == 'completed'
    assert saved['last_run']


def test_due_once_schedule_restores_after_restart_and_runs(tmp_path):
    path = tmp_path / 'automations.json'
    first = AutomationService(path)
    created = first.create('person@example.com', {
        'name': 'One-time brief',
        'schedule': {'type': 'once', 'at': '2099-09-08T08:00:00+05:30'},
        'action': {'goal': 'Prepare the brief.'},
    })
    records = first._read()
    records[0]['next_run'] = '2026-09-07T00:00:00+00:00'
    first._write(records)

    calls = []
    restored = AutomationService(path)
    restored.set_runner(lambda automation: calls.append(automation['id']))
    due = restored.tick(datetime(2026, 9, 7, 1, 0, tzinfo=timezone.utc))
    assert [item['id'] for item in due] == [created['id']]
    for _ in range(50):
        saved = restored.get(created['id'], 'person@example.com')
        if saved.get('last_status'):
            break
        time.sleep(0.01)
    assert calls == [created['id']]
    assert saved['enabled'] is False
    assert saved['next_run'] is None
