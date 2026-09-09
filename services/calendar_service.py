import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from services.calendar_conflicts import calculate_conflicts
from services.secure_storage import read_encrypted_json, write_encrypted_json

BASE_DIR = Path(__file__).resolve().parent.parent
CREDENTIALS_FILE = BASE_DIR / 'data' / 'google_credentials.json'
APP_TIMEZONE = os.getenv('APP_TIMEZONE', 'Asia/Kolkata')
TOKEN_URI = 'https://oauth2.googleapis.com/token'

WRITE_SCOPES = {
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/calendar.events',
    'https://www.googleapis.com/auth/calendar.events.owned',
}


def access_status():
    try:
        info = read_encrypted_json(CREDENTIALS_FILE, 'google_credentials', default=None)
        if not info:
            return {'authenticated': False, 'writable': False, 'timezone': APP_TIMEZONE}
    except Exception:
        return {'authenticated': False, 'writable': False, 'timezone': APP_TIMEZONE}
    scopes = set(info.get('scopes') or [])
    return {
        'authenticated': bool(info.get('token')),
        'writable': (bool(scopes.intersection(WRITE_SCOPES)) if scopes else None),
        'scopes': sorted(scopes),
        'timezone': APP_TIMEZONE,
    }


class CalendarAuthError(RuntimeError):
    pass


def _require_write_scope():
    status = access_status()
    if status['writable'] is False:
        raise CalendarAuthError('Google Calendar write permission is not granted. Reconnect Google once with the calendar scope.')


def _tz():
    try:
        return ZoneInfo(APP_TIMEZONE)
    except Exception as exc:
        if APP_TIMEZONE in {'Asia/Kolkata', 'Asia/Calcutta'}:
            return timezone(timedelta(hours=5, minutes=30), name='IST')
        print(f'[Calendar] timezone {APP_TIMEZONE!r} unavailable ({exc}); using UTC')
        return timezone.utc


def _load_credentials():
    try:
        info = read_encrypted_json(CREDENTIALS_FILE, 'google_credentials', default=None)
        if not info:
            raise CalendarAuthError('Google credentials are not available')
    except Exception as exc:
        raise CalendarAuthError(f'Could not read Google credentials: {exc}') from exc
    if not info.get('token'):
        raise CalendarAuthError('Google access token is missing')

    expiry = None
    if info.get('expiry'):
        try:
            expiry = datetime.fromisoformat(str(info['expiry']).replace('Z', '+00:00'))
            if expiry.tzinfo is not None:
                expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            expiry = None

    credentials = Credentials(
        token=info.get('token'),
        refresh_token=info.get('refresh_token'),
        token_uri=info.get('token_uri') or TOKEN_URI,
        client_id=info.get('client_id') or os.getenv('GOOGLE_CLIENT_ID'),
        client_secret=info.get('client_secret') or os.getenv('GOOGLE_CLIENT_SECRET'),
        scopes=info.get('scopes'),
        expiry=expiry,
    )
    if credentials.expired:
        if not credentials.refresh_token:
            raise CalendarAuthError('Google token expired and has no refresh token')
        credentials.refresh(Request())
        _save_credentials(credentials, info)
    return credentials


def _save_credentials(credentials, previous=None):
    previous = previous or {}
    data = json.loads(credentials.to_json())
    if not data.get('refresh_token') and previous.get('refresh_token'):
        data['refresh_token'] = previous['refresh_token']
    data.setdefault('client_id', os.getenv('GOOGLE_CLIENT_ID', ''))
    data.setdefault('client_secret', os.getenv('GOOGLE_CLIENT_SECRET', ''))
    data.setdefault('token_uri', TOKEN_URI)
    write_encrypted_json(CREDENTIALS_FILE, data, 'google_credentials')


def _service():
    return build('calendar', 'v3', credentials=_load_credentials(), cache_discovery=False)


def _iso(value, *, end_of_day=False):
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if raw.endswith('Z'):
            raw = raw[:-1] + '+00:00'
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            try:
                dt = datetime.strptime(raw, '%Y-%m-%d')
            except ValueError:
                raise ValueError(f'Invalid datetime: {value}')
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz())
    if end_of_day and dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt.isoformat()


def _event_time(block):
    block = block or {}
    return block.get('dateTime') or block.get('date') or ''


def _parse_dt(value):
    if not value or len(value) <= 10:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None


def _normalize(event):
    start_raw = _event_time(event.get('start'))
    end_raw = _event_time(event.get('end'))
    all_day = bool((event.get('start') or {}).get('date'))
    private_props = ((event.get('extendedProperties') or {}).get('private') or {})
    source = 'ai' if private_props.get('agent_harness_source') == 'ai_email_deadline' else 'google'
    return {
        'id': event.get('id'),
        'title': event.get('summary') or '(untitled event)',
        'description': event.get('description') or '',
        'start': start_raw,
        'end': end_raw,
        'all_day': all_day,
        'location': event.get('location') or '',
        'attendees': event.get('attendees') or [],
        'calendar_id': 'primary',
        'html_link': event.get('htmlLink') or '',
        'status': event.get('status') or 'confirmed',
        'color_id': event.get('colorId'),
        'recurring_event_id': event.get('recurringEventId'),
        'transparency': event.get('transparency') or 'opaque',
        'updated': event.get('updated') or '',
        'source': source,
        'blocking': source == 'google' and not all_day and event.get('status') != 'cancelled' and event.get('transparency', 'opaque') != 'transparent',
        'agent_harness': private_props,
        'urgency': private_props.get('agent_harness_urgency') or '',
    }


def _annotate_conflicts(events):
    annotated, _ = calculate_conflicts(events)
    return annotated


def list_events(start=None, end=None, limit=250):
    now = datetime.now(_tz())
    start_iso = _iso(start) if start else now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    end_iso = _iso(end, end_of_day=True) if end else (now + timedelta(days=30)).replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
    response = _service().events().list(
        calendarId='primary',
        timeMin=start_iso,
        timeMax=end_iso,
        maxResults=max(1, min(int(limit or 250), 500)),
        singleEvents=True,
        orderBy='startTime',
        showDeleted=False,
    ).execute()
    events = []
    for raw in response.get('items') or []:
        events.append(_normalize(raw))
    return _annotate_conflicts(events)


def _event_body(data, existing=None):
    existing = existing or {}
    body = {}
    if 'title' in data or 'summary' in data:
        body['summary'] = data.get('title') or data.get('summary') or '(untitled event)'
    if 'description' in data:
        body['description'] = data.get('description') or ''
    if 'location' in data:
        body['location'] = data.get('location') or ''
    if 'attendees' in data:
        attendees = []
        for item in data.get('attendees') or []:
            if isinstance(item, str):
                attendees.append({'email': item})
            elif isinstance(item, dict) and item.get('email'):
                attendees.append({'email': item['email']})
        body['attendees'] = attendees
    if 'color_id' in data and data.get('color_id'):
        body['colorId'] = str(data['color_id'])
    if 'extended_properties' in data and isinstance(data.get('extended_properties'), dict):
        body['extendedProperties'] = {'private': {str(k): str(v) for k, v in (data.get('extended_properties') or {}).items() if v is not None}}

    has_start = 'start' in data
    has_end = 'end' in data
    all_day = bool(data.get('all_day'))
    if has_start:
        start = str(data.get('start') or '').strip()
        if not start:
            raise ValueError('start is required')
        if all_day or (len(start) == 10 and start[4] == '-' and start[7] == '-'):
            start_date = start[:10]
            body['start'] = {'date': start_date}
            if has_end and data.get('end'):
                end_date = str(data['end'])[:10]
            else:
                end_date = (datetime.strptime(start_date, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
            body['end'] = {'date': end_date}
        else:
            start_iso = _iso(start)
            body['start'] = {'dateTime': start_iso, 'timeZone': APP_TIMEZONE}
            if has_end and data.get('end'):
                end_iso = _iso(data['end'])
            else:
                start_dt = datetime.fromisoformat(start_iso)
                end_iso = (start_dt + timedelta(hours=1)).isoformat()
            body['end'] = {'dateTime': end_iso, 'timeZone': APP_TIMEZONE}
    elif has_end:
        body['start'] = existing.get('start')
        end = str(data.get('end') or '').strip()
        if existing.get('start', {}).get('date'):
            body['end'] = {'date': end[:10]}
        else:
            body['end'] = {'dateTime': _iso(end), 'timeZone': APP_TIMEZONE}
    return body


def create_event(data):
    _require_write_scope()
    if not data.get('title') and not data.get('summary'):
        raise ValueError('title is required')
    if not data.get('start'):
        raise ValueError('start is required')
    created = _service().events().insert(
        calendarId='primary',
        body=_event_body(data),
        sendUpdates='all' if data.get('attendees') else 'none',
    ).execute()
    return _normalize(created)


def update_event(event_id, data):
    _require_write_scope()
    if not event_id:
        raise ValueError('event_id is required')
    service = _service()
    existing = service.events().get(calendarId='primary', eventId=event_id).execute()
    patch = _event_body(data, existing=existing)
    if not patch:
        return _normalize(existing)
    existing_all_day = bool((existing.get('start') or {}).get('date'))
    updated_all_day = bool((patch.get('start') or {}).get('date')) if 'start' in patch else existing_all_day
    method = service.events().patch
    body = patch
    if 'start' in patch and existing_all_day != updated_all_day:
        writable_fields = (
            'summary', 'description', 'location', 'attendees', 'colorId',
            'extendedProperties', 'reminders', 'recurrence', 'transparency',
            'visibility', 'guestsCanInviteOthers', 'guestsCanModify',
            'guestsCanSeeOtherGuests',
        )
        body = {key: existing[key] for key in writable_fields if key in existing}
        body.update(patch)
        method = service.events().update
    updated = method(
        calendarId='primary',
        eventId=event_id,
        body=body,
        sendUpdates='all' if 'attendees' in data else 'none',
    ).execute()
    return _normalize(updated)


def delete_event(event_id):
    """
    Idempotent delete. Returns the managed Mailmate marker when present so the
    Flask layer can remember that the user intentionally dismissed it and avoid
    recreating it from the same email deadline.
    """
    _require_write_scope()
    if not event_id:
        raise ValueError('event_id is required')
    service = _service()
    normalized = None
    try:
        existing = service.events().get(calendarId='primary', eventId=event_id).execute()
        normalized = _normalize(existing)
    except HttpError as exc:
        if getattr(exc.resp, 'status', None) != 404:
            raise
    try:
        service.events().delete(calendarId='primary', eventId=event_id, sendUpdates='all').execute()
    except HttpError as exc:
        if getattr(exc.resp, 'status', None) != 404:
            raise
    props = (normalized or {}).get('agent_harness') or {}
    return {
        'deleted': True,
        'id': event_id,
        'already_missing': normalized is None,
        'source': (normalized or {}).get('source'),
        'marker': props.get('agent_harness_marker'),
    }


def find_event(query, start=None, end=None):
    q = (query or '').strip().lower()
    if not q:
        return None
    events = list_events(start=start, end=end, limit=250)
    exact = [e for e in events if e['title'].lower() == q]
    if exact:
        return exact[0]
    contains = [e for e in events if q in e['title'].lower() or e['title'].lower() in q]
    return contains[0] if contains else None


def find_managed_event(marker, start=None, end=None):
    if not marker:
        return None
    now = datetime.now(_tz())
    time_min = _iso(start) if start else (now - timedelta(days=30)).isoformat()
    time_max = _iso(end) if end else (now + timedelta(days=365)).isoformat()
    result = _service().events().list(
        calendarId='primary',
        timeMin=time_min,
        timeMax=time_max,
        maxResults=20,
        singleEvents=True,
        showDeleted=False,
        privateExtendedProperty=[
            'agent_harness_source=ai_email_deadline',
            f'agent_harness_marker={marker}',
        ],
    ).execute()
    items = result.get('items') or []
    return _normalize(items[0]) if items else None


def upsert_ai_deadline_event(marker, data):
    _require_write_scope()
    if not marker:
        raise ValueError('marker is required')
    payload = dict(data or {})
    urgency = str(payload.pop('urgency', 'normal') or 'normal')
    payload['extended_properties'] = {
        'agent_harness_source': 'ai_email_deadline',
        'agent_harness_marker': marker,
        'agent_harness_urgency': urgency,
    }
    if not payload.get('color_id'):
        payload['color_id'] = '11' if urgency == 'critical' else '6' if urgency == 'urgent' else '5'
    existing = find_managed_event(marker)
    if existing:
        return update_event(existing['id'], payload)
    return create_event(payload)
