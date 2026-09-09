import json
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from services.secure_storage import DataEncryptionError, read_encrypted_json, write_encrypted_json


BASE_DIR = Path(__file__).resolve().parent.parent
AUTOMATIONS_FILE = BASE_DIR / 'data' / 'automations.json'
SUPPORTED_SCHEDULES = {'once', 'daily', 'weekly', 'interval'}


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat() if value else None


def _parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _timezone(name):
    try:
        return ZoneInfo(str(name or 'Asia/Kolkata'))
    except Exception:
        return timezone(timedelta(hours=5, minutes=30), name='IST')


def next_run_for(schedule, now=None, last_run=None):
    now = (now or _now()).astimezone(timezone.utc)
    schedule = schedule or {}
    schedule_type = str(schedule.get('type') or 'daily').lower()
    if schedule_type == 'once':
        candidate = _parse_datetime(schedule.get('at'))
        return candidate if candidate and candidate > now else None
    if schedule_type == 'interval':
        minutes = max(5, min(int(schedule.get('minutes') or 60), 10080))
        base = _parse_datetime(last_run) or now
        candidate = base + timedelta(minutes=minutes)
        while candidate <= now:
            candidate += timedelta(minutes=minutes)
        return candidate

    zone = _timezone(schedule.get('timezone'))
    local_now = now.astimezone(zone)
    try:
        hour, minute = [int(part) for part in str(schedule.get('time') or '08:00').split(':', 1)]
    except (TypeError, ValueError):
        hour, minute = 8, 0
    hour, minute = max(0, min(hour, 23)), max(0, min(minute, 59))
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if schedule_type == 'weekly':
        weekday = max(0, min(int(schedule.get('weekday') or 0), 6))
        candidate += timedelta(days=(weekday - candidate.weekday()) % 7)
        if candidate <= local_now:
            candidate += timedelta(days=7)
    elif candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


class AutomationService:
    def __init__(self, store_path=AUTOMATIONS_FILE, poll_seconds=5):
        self.store_path = Path(store_path)
        self.poll_seconds = max(1, int(poll_seconds))
        self._lock = threading.RLock()
        self._runner = None
        self._stop = threading.Event()
        self._thread = None
        self._running_ids = set()

    def set_runner(self, runner):
        self._runner = runner

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._scheduler_loop, daemon=True, name='KyleAutomationScheduler')
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _read(self):
        data = read_encrypted_json(self.store_path, 'automations', default=[])
        if not isinstance(data, list):
            raise DataEncryptionError('Encrypted automation storage has an invalid shape')
        return data

    def _write(self, automations):
        write_encrypted_json(self.store_path, automations, 'automations')

    def _validate_schedule(self, raw):
        schedule = deepcopy(raw or {})
        schedule_type = str(schedule.get('type') or 'daily').lower()
        if schedule_type not in SUPPORTED_SCHEDULES:
            raise ValueError('Schedule type must be once, daily, weekly, or interval')
        schedule['type'] = schedule_type
        schedule['timezone'] = str(schedule.get('timezone') or 'Asia/Kolkata')
        if schedule_type in {'daily', 'weekly'}:
            value = str(schedule.get('time') or '08:00')
            try:
                hour, minute = [int(part) for part in value.split(':', 1)]
            except (TypeError, ValueError) as exc:
                raise ValueError('Schedule time must use HH:MM') from exc
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError('Schedule time must use HH:MM')
            schedule['time'] = f'{hour:02d}:{minute:02d}'
        if schedule_type == 'weekly':
            schedule['weekday'] = max(0, min(int(schedule.get('weekday') or 0), 6))
        if schedule_type == 'interval':
            schedule['minutes'] = max(5, min(int(schedule.get('minutes') or 60), 10080))
        if schedule_type == 'once':
            at = _parse_datetime(schedule.get('at'))
            if not at:
                raise ValueError('A valid date and time is required')
            schedule['at'] = _iso(at)
        return schedule

    def list(self, user_id):
        with self._lock:
            automations = [item for item in self._read() if item.get('user_id') == user_id]
        return sorted(automations, key=lambda item: (not item.get('enabled', True), item.get('name') or ''))

    def get(self, automation_id, user_id):
        return next((item for item in self.list(user_id) if item.get('id') == automation_id), None)

    def create(self, user_id, payload):
        name = str((payload or {}).get('name') or '').strip()[:100]
        goal = str(((payload or {}).get('action') or {}).get('goal') or '').strip()[:1200]
        if not name or not goal:
            raise ValueError('Automation name and Kyle goal are required')
        schedule = self._validate_schedule((payload or {}).get('schedule'))
        now = _now()
        automation = {
            'id': f'auto_{uuid.uuid4().hex[:12]}',
            'user_id': user_id,
            'name': name,
            'enabled': bool((payload or {}).get('enabled', True)),
            'schedule': schedule,
            'action': {'type': 'kyle_goal', 'goal': goal},
            'output': {'type': 'work_summary'},
            'created_at': _iso(now),
            'updated_at': _iso(now),
            'last_run': None,
            'last_status': None,
        }
        automation['next_run'] = _iso(next_run_for(schedule, now)) if automation['enabled'] else None
        with self._lock:
            automations = self._read()
            automations.append(automation)
            self._write(automations)
        return automation

    def update(self, automation_id, user_id, payload):
        with self._lock:
            automations = self._read()
            target = next((item for item in automations if item.get('id') == automation_id and item.get('user_id') == user_id), None)
            if not target:
                return None
            if 'name' in payload:
                target['name'] = str(payload.get('name') or '').strip()[:100]
            if 'enabled' in payload:
                target['enabled'] = bool(payload.get('enabled'))
            if 'schedule' in payload:
                target['schedule'] = self._validate_schedule(payload.get('schedule'))
            if 'action' in payload:
                goal = str((payload.get('action') or {}).get('goal') or '').strip()[:1200]
                if not goal:
                    raise ValueError('Kyle goal is required')
                target['action'] = {'type': 'kyle_goal', 'goal': goal}
            if not target.get('name'):
                raise ValueError('Automation name is required')
            target['updated_at'] = _iso(_now())
            target['next_run'] = _iso(next_run_for(target['schedule'], _now(), target.get('last_run'))) if target.get('enabled') else None
            self._write(automations)
            return deepcopy(target)

    def delete(self, automation_id, user_id):
        with self._lock:
            automations = self._read()
            kept = [item for item in automations if not (item.get('id') == automation_id and item.get('user_id') == user_id)]
            if len(kept) == len(automations):
                return False
            self._write(kept)
        return True

    def run_now(self, automation_id, user_id):
        automation = self.get(automation_id, user_id)
        if not automation:
            return None
        self._queue_run(automation)
        return {'ok': True, 'automation_id': automation_id, 'queued': True}

    def _queue_run(self, automation):
        automation_id = automation.get('id')
        with self._lock:
            if automation_id in self._running_ids:
                return False
            self._running_ids.add(automation_id)
        threading.Thread(target=self._execute, args=(deepcopy(automation),), daemon=True, name=f'Automation-{automation_id}').start()
        return True

    def _execute(self, automation):
        automation_id = automation['id']
        status = 'completed'
        try:
            if not self._runner:
                raise RuntimeError('Automation runner is not configured')
            self._runner(automation)
        except Exception as exc:
            status = 'failed'
            print(f'[Automations] {automation_id} failed: {exc}')
        finally:
            with self._lock:
                automations = self._read()
                target = next((item for item in automations if item.get('id') == automation_id), None)
                if target:
                    completed_at = _now()
                    target['last_run'] = _iso(completed_at)
                    target['last_status'] = status
                    if target.get('schedule', {}).get('type') == 'once':
                        target['enabled'] = False
                        target['next_run'] = None
                    elif target.get('enabled'):
                        target['next_run'] = _iso(next_run_for(target.get('schedule'), completed_at, target['last_run']))
                    self._write(automations)
                self._running_ids.discard(automation_id)

    def tick(self, now=None):
        now = (now or _now()).astimezone(timezone.utc)
        with self._lock:
            automations = self._read()
            due = []
            changed = False
            for item in automations:
                if not item.get('enabled'):
                    continue
                next_run = _parse_datetime(item.get('next_run'))
                if next_run is None:
                    next_run = next_run_for(item.get('schedule'), now, item.get('last_run'))
                    item['next_run'] = _iso(next_run)
                    changed = True
                if next_run and next_run <= now and item.get('id') not in self._running_ids:
                    due.append(deepcopy(item))
                    item['next_run'] = _iso(next_run_for(item.get('schedule'), now, _iso(now)))
                    changed = True
            if changed:
                self._write(automations)
        for item in due:
            self._queue_run(item)
        return due

    def _scheduler_loop(self):
        while not self._stop.wait(self.poll_seconds):
            try:
                self.tick()
            except Exception as exc:
                print(f'[Automations] Scheduler notice: {exc}')


automation_service = AutomationService()
