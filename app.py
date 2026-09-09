import os
import json
from urllib.parse import urlencode, urlparse
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import re
import threading
import time
import hashlib
import hmac
import ipaddress
import requests
import uuid
from copy import deepcopy
from dateutil import parser as date_parser

from flask import Flask, request, jsonify, redirect, send_from_directory, session, abort, Response
from flask_cors import CORS
from dotenv import load_dotenv

# Resolve everything relative to app.py, not the shell/Antigravity working directory.
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / 'data'
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Load environment before importing services; service modules may read env at import time.
load_dotenv(BASE_DIR / 'api.env')

# Localhost-only OAuth development flags. Keep these before google-auth-oauthlib is used.
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

from email.utils import parseaddr
from services.whisper_service import whisper_service
from services.google_service import get_auth_url, handle_callback, get_user_profile, get_gmail_threads, get_gmail_history_id, get_gmail_thread_changes, get_gmail_message_ids, get_gmail_message, mark_gmail_message_read, mark_gmail_message_important, trash_gmail_message, get_gmail_permissions, GmailInsufficientPermissionError, send_gmail_direct, send_gmail_draft, find_sent_message_by_rfc_id
from services.calendar_service import list_events as calendar_list_events, create_event as calendar_create_event, update_event as calendar_update_event, delete_event as calendar_delete_event, find_event as calendar_find_event, access_status as calendar_access_status, upsert_ai_deadline_event as calendar_upsert_ai_deadline
from services.ai_service import get_dashboard_overview, chat_with_kyle, generate_kyle_agent_reply
from services.work_agent_service import work_agent_service
from services.privacy_gate import PrivacyGate
from services.system_context_service import system_context_service
from services.automation_service import automation_service
from services.calendar_conflicts import calculate_conflicts
from services.agent.inference_broker import inference_broker
from services.agent.token_budget import approximate_tokens, bounded_payload
from services.mail_context_service import mail_context_service
from services.kyle_agent_planner import plan_kyle_turn
from services.mail_sync_service import MailSyncService
from services.elevenlabs_service import ElevenLabsError, signed_agent_url, status as elevenlabs_status, synthesize as elevenlabs_synthesize
from services.secure_storage import DataEncryptionError, read_encrypted_json, write_encrypted_json

app = Flask(__name__, static_folder=None)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'default-dev-secret-key-123')
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax'
)
CORS(app)

# Initialize local Whisper STT in background
if str(os.getenv('MAILMATE_DISABLE_WHISPER_INIT', '')).strip().lower() not in {'1', 'true', 'yes', 'on'}:
    whisper_service.initialize()

APP_TIMEZONE = os.getenv('APP_TIMEZONE', 'Asia/Kolkata')

def _load_app_timezone():
    try:
        return ZoneInfo(APP_TIMEZONE)
    except Exception as exc:
        if APP_TIMEZONE in {'Asia/Kolkata', 'Asia/Calcutta'}:
            print(f"[Calendar] timezone database unavailable ({exc}); using fixed IST UTC+05:30")
            return timezone(timedelta(hours=5, minutes=30), name='IST')
        return timezone.utc

APP_TZ = _load_app_timezone()
CACHE_SYNC_SECONDS = max(60, int(os.getenv('CACHE_SYNC_SECONDS', '300')))
CACHE_REPROCESS_SECONDS = max(CACHE_SYNC_SECONDS, int(os.getenv('CACHE_REPROCESS_SECONDS', '1800')))
AI_CALENDAR_SYNC_SECONDS = max(30, int(os.getenv('AI_CALENDAR_SYNC_SECONDS', '180')))
_ai_calendar_last_sync = {}
CALENDAR_DISMISSALS_FILE = DATA_DIR / 'calendar_dismissals.json'
_mail_send_lock = threading.Lock()
_mail_send_operations = {}
_calendar_dismissal_lock = threading.Lock()
_mailbox_snapshot_lock = threading.Lock()
_mailbox_snapshots = {}


def _run_scheduled_kyle_goal(automation):
    user_id = str((automation or {}).get('user_id') or 'default')
    run = work_agent_service.create_automation_run(user_id, automation)
    run_id = run['id']
    try:
        threads, _ = get_gmail_threads()
        work_agent_service.add_automation_run_step(run_id, user_id, 'Checked current Gmail context')
        overview = get_dashboard_overview(threads or [])
        attention = list(overview.get('needs_attention') or [])
        work_agent_service.add_automation_run_step(
            run_id,
            user_id,
            f'Found {len(attention)} item{"s" if len(attention) != 1 else ""} needing attention',
        )

        calendar_events, conflict_pairs = calculate_conflicts(calendar_list_events(limit=100) or [])
        real_events = [event for event in calendar_events if event.get('source') == 'google']
        conflicts = len(conflict_pairs)
        work_agent_service.add_automation_run_step(run_id, user_id, 'Checked Google Calendar and schedule clashes')

        goal = str(((automation or {}).get('action') or {}).get('goal') or '').strip()
        summary = (
            f'Kyle completed "{goal}" ' if goal else 'Kyle completed the scheduled workspace check. '
        )
        summary += (
            f'{len(threads or [])} recent Gmail thread{"s" if len(threads or []) != 1 else ""}, '
            f'{len(attention)} attention item{"s" if len(attention) != 1 else ""}, and '
            f'{len(real_events)} Google Calendar event{"s" if len(real_events) != 1 else ""} were checked.'
        )
        if conflicts:
            summary += f' {conflicts} genuine schedule clash{"es" if conflicts != 1 else ""} need review.'
        checklist = [
            str(item.get('description') or item.get('title') or 'Review attention item')[:240]
            for item in attention[:6]
        ]
        work_agent_service.finish_automation_run(run_id, user_id, summary, checklist=checklist)
        system_context_service.increment_version()
        return run_id
    except Exception as exc:
        work_agent_service.finish_automation_run(run_id, user_id, 'The scheduled run could not finish.', error=str(exc))
        system_context_service.increment_version()
        raise


automation_service.set_runner(_run_scheduled_kyle_goal)
automation_service.start()


def _calendar_account_key(profile=None):
    profile = profile or {}
    return str(profile.get('email') or profile.get('id') or 'default').strip().lower()


def _read_calendar_dismissals():
    try:
        data = read_encrypted_json(CALENDAR_DISMISSALS_FILE, 'calendar_dismissals', default={})
        return data if isinstance(data, dict) else {}
    except DataEncryptionError:
        raise
    except Exception:
        return {}


def _calendar_dismissed_markers(profile=None):
    account = _calendar_account_key(profile)
    with _calendar_dismissal_lock:
        data = _read_calendar_dismissals()
        values = data.get(account) or []
        return {str(value) for value in values if value}


def _remember_calendar_dismissal(profile, marker):
    marker = str(marker or '').strip()
    if not marker:
        return False
    account = _calendar_account_key(profile)
    with _calendar_dismissal_lock:
        data = _read_calendar_dismissals()
        values = {str(value) for value in (data.get(account) or []) if value}
        values.add(marker)
        data[account] = sorted(values)
        write_encrypted_json(CALENDAR_DISMISSALS_FILE, data, 'calendar_dismissals')
    return True


def _attention_source_id(item):
    return str(
        (item or {}).get('source_message_id')
        or (item or {}).get('message_id')
        or (item or {}).get('email_id')
        or ''
    ).strip()


def _resolved_message_ids_from_threads(threads):
    """An inbound message is resolved when its Gmail thread has a later sent reply."""
    resolved = set()
    for thread in threads or []:
        messages = list((thread or {}).get('messages') or [])
        last_outbound_index = -1
        for index, message in enumerate(messages):
            labels = {str(label).upper() for label in (message.get('labels') or [])}
            if (
                str(message.get('direction') or '').lower() == 'outbound'
                or message.get('is_sent_by_me') is True
                or 'SENT' in labels
            ):
                last_outbound_index = index
        if last_outbound_index < 0:
            continue
        for message in messages[:last_outbound_index]:
            labels = {str(label).upper() for label in (message.get('labels') or [])}
            outbound = (
                str(message.get('direction') or '').lower() == 'outbound'
                or message.get('is_sent_by_me') is True
                or 'SENT' in labels
            )
            if outbound:
                continue
            message_id = str(message.get('id') or message.get('gmail_id') or '')
            if message_id:
                resolved.add(message_id)
    return resolved


def _prune_payload_to_live_gmail(payload, live_ids):
    """Remove messages/tasks that no longer exist in active Gmail (e.g. moved to Trash)."""
    result = dict(payload or {})
    live_ids = {str(value) for value in (live_ids or set()) if value}
    before_emails = list(result.get('emails') or [])
    result['emails'] = [
        email for email in before_emails
        if str(email.get('id') or email.get('gmail_id') or '') in live_ids
    ]

    removed_ids = {
        str(email.get('id') or email.get('gmail_id') or '')
        for email in before_emails
        if str(email.get('id') or email.get('gmail_id') or '') not in live_ids
    }

    for key in ('needs_attention', 'waiting_on_others'):
        cleaned = []
        for item in result.get(key) or []:
            source_id = _attention_source_id(item)
            if source_id and source_id in removed_ids:
                continue
            cleaned.append(item)
        result[key] = cleaned

    metrics = dict(result.get('metrics') or {})
    metrics['emails'] = len(result['emails'])
    metrics['important'] = len(result.get('needs_attention') or [])
    metrics['actions'] = len(result.get('needs_attention') or [])
    result['metrics'] = metrics
    result['gmail_pruned_count'] = len(removed_ids)
    return result


def _prune_resolved_attention(payload, resolved_source_ids):
    """Keep completed Gmail work visible in Inbox, but out of active attention."""
    result = dict(payload or {})
    resolved = {str(value) for value in (resolved_source_ids or set()) if value}
    if not resolved:
        return result

    for key in ('needs_attention', 'waiting_on_others'):
        result[key] = [
            item for item in (result.get(key) or [])
            if _attention_source_id(item) not in resolved
        ]

    emails = list(result.get('emails') or [])
    for email in emails:
        message_id = str(email.get('id') or email.get('gmail_id') or '')
        if message_id not in resolved:
            continue
        scores = dict(email.get('context_scores') or {})
        scores.update({
            'attention_allowed': False,
            'requires_reply': False,
            'work_allowed': False,
            'resolution': 'completed_work',
        })
        email['context_scores'] = scores
    result['emails'] = emails

    metrics = dict(result.get('metrics') or {})
    metrics['important'] = len(result.get('needs_attention') or [])
    metrics['actions'] = len(result.get('needs_attention') or [])
    result['metrics'] = metrics
    return result

# MAILMATE_TAILNET_COMPUTE_ONLY_GUARD
@app.before_request
def _mailmate_tailnet_compute_only():
    """Tailnet peers may use compute only; Priyam's Gmail/session routes stay local."""
    remote = str(request.remote_addr or "").strip()
    try:
        ip = ipaddress.ip_address(remote)
    except ValueError:
        return None

    if ip.is_loopback:
        return None
    if ip in ipaddress.ip_network("100.64.0.0/10"):
        if request.path.startswith("/api/compute/"):
            return None
        return jsonify({"error": "tailnet_compute_only"}), 403
    return None


@app.route('/')
def index():
    return send_from_directory(str(BASE_DIR), 'index.html')


@app.route('/dashboard.html')
def dashboard_page():
    if not get_user_profile():
        return redirect('/')
    return send_from_directory(str(BASE_DIR), 'dashboard.html')


@app.route('/assets/<path:filename>')
def serve_assets(filename):
    assets_dir = BASE_DIR / 'assets'
    if not (assets_dir / filename).is_file():
        abort(404)
    return send_from_directory(str(assets_dir), filename)


@app.route('/<path:filename>')
def serve_project_file(filename):
    # Serve real project files only. Never return index.html for missing
    # CSS/JS/image requests, because the browser will reject the wrong MIME type.
    candidate = (BASE_DIR / filename).resolve()
    try:
        candidate.relative_to(BASE_DIR)
    except ValueError:
        abort(404)

    if candidate.is_file():
        return send_from_directory(str(BASE_DIR), filename)

    abort(404)

@app.route('/api/health')
def health():
    supabase = mail_context_service.status()
    elevenlabs = elevenlabs_status()
    return jsonify({
        "ok": True,
        "googleClientConfigured": bool(os.getenv('GOOGLE_CLIENT_ID')),
        "geminiConfigured": bool(os.getenv('GEMINI_API_KEY')),
        "geminiFallbackEnabled": str(os.getenv('MAILMATE_CLOUD_FALLBACK', '0')).lower() in {'1', 'true', 'yes', 'on'},
        "localModelConfigured": bool(os.getenv('LM_STUDIO_BASE_URL', 'http://127.0.0.1:2806/v1')),
        "whisper": whisper_service.get_status(),
        "supabaseConfigured": supabase["configured"],
        "supabase": supabase,
        "privacyGate": {"enabled": True, "mode": "deterministic-local", "centralRetention": "derived-only-when-user-scoped"},
        "cachePolicy": {"syncCheckSeconds": CACHE_SYNC_SECONDS, "reprocessSeconds": CACHE_REPROCESS_SECONDS},
        "elevenLabsConfigured": elevenlabs["configured"],
        "elevenLabs": elevenlabs,
        "calendar": calendar_access_status(),
        "calendarReadWrite": calendar_access_status().get("writable", False),
        "gmailWrite": get_gmail_permissions().get("can_write", False),
        "gmailPermissions": get_gmail_permissions(),
        "appTimezone": APP_TIMEZONE,
        "voiceInput": "local-whisper-with-browser-fallback" if whisper_service.get_status().get("available") else "browser-speech-recognition",
        "staticFiles": {
            "index.html": (BASE_DIR / "index.html").is_file(),
            "styles.css": (BASE_DIR / "styles.css").is_file(),
            "script.js": (BASE_DIR / "script.js").is_file(),
            "dashboard.html": (BASE_DIR / "dashboard.html").is_file(),
            "dashboard.css": (BASE_DIR / "dashboard.css").is_file(),
            "dashboard.js": (BASE_DIR / "dashboard.js").is_file(),
            "cs_logo.png": (BASE_DIR / "assets" / "images" / "cs_logo.png").is_file(),
            "logo.svg": (BASE_DIR / "assets" / "images" / "logo.svg").is_file()
        },
        "projectRoot": str(BASE_DIR),
        "message": "Flask Backend Running!"
    })


@app.route('/api/voice/test', methods=['GET'])
def elevenlabs_voice_test():
    if not get_user_profile():
        return jsonify({'ok': False, 'error': 'Not authenticated'}), 401
    try:
        audio_bytes, mime = elevenlabs_synthesize('Kyle voice system is working.')
        return Response(
            audio_bytes,
            status=200,
            content_type=mime,
            headers={
                'Cache-Control': 'no-store',
                'X-Mailmate-Voice': 'elevenlabs',
            },
        )
    except ElevenLabsError as exc:
        return jsonify({
            'ok': False,
            'provider': 'elevenlabs',
            'error': str(exc),
        }), 502
    except Exception:
        app.logger.exception('ElevenLabs voice test failed')
        return jsonify({
            'ok': False,
            'provider': 'elevenlabs',
            'error': 'Voice generation failed.',
        }), 500


@app.route('/api/config')
def config():
    return jsonify({
        "googleClientId": os.getenv('GOOGLE_CLIENT_ID', ''),
        "backendAuthUrl": "/auth/google"
    })



# MAILMATE_EMBEDDED_COMPUTE_PROXY_START
MAILMATE_COMPUTE_MAX_BYTES = 2 * 1024 * 1024
_compute_health_lock = threading.Lock()
_compute_health_cache = {}
_compute_generation_lock = threading.Lock()
_compute_generation_cache = {}
_compute_generation_inflight = {}


def _env_truthy(name):
    return str(os.getenv(name, '')).strip().lower() in {'1', 'true', 'yes', 'on'}


def _worker_host_enabled():
    value = str(os.getenv('MAILMATE_WORKER_HOST', '')).strip().lower()
    return bool(value) and value not in {'0', 'false', 'no', 'off'}

def _mailmate_compute_authorized():
    # Same-machine and Tailscale devices may use the inference-only route.
    remote = str(request.remote_addr or "").strip()
    try:
        ip = ipaddress.ip_address(remote)
        if ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10"):
            return True
    except ValueError:
        pass

    # Optional bearer token remains supported for explicit non-Tailscale routes.
    expected = os.getenv("MAILMATE_WORKER_TOKEN", "").strip()
    if not expected:
        return False
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    supplied = header[7:].strip()
    return bool(supplied) and hmac.compare_digest(supplied, expected)

@app.route("/api/compute/health", methods=["GET"])
def embedded_compute_health():
    if not _mailmate_compute_authorized():
        return jsonify({"error": "unauthorized"}), 401
    local = _mailmate_probe_local_compute()
    ready = bool(local.get("available"))
    return jsonify({
        "ok": ready,
        "worker": "mailmate-embedded-compute",
        "transport": "tailscale",
        "lm_studio": local,
        "retention": "none",
    }), 200 if ready else 503

@app.route("/api/compute/v1/chat/completions", methods=["POST"])
def embedded_compute_completion():
    if not _mailmate_compute_authorized():
        return jsonify({"error": "unauthorized"}), 401
    if request.content_length and request.content_length > MAILMATE_COMPUTE_MAX_BYTES:
        return jsonify({"error": "request_too_large"}), 413

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "invalid_json"}), 400
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return jsonify({"error": "messages_required"}), 400
    if len(messages) > 64:
        return jsonify({"error": "too_many_messages"}), 400

    request_kind = request.headers.get('X-Mailmate-Request-Kind', 'work').strip().lower()
    if request_kind not in {'interactive', 'work', 'automation', 'background'}:
        request_kind = 'work'
    payload = bounded_payload(payload, max_input_tokens=3500)
    payload.setdefault('chat_template_kwargs', {})['enable_thinking'] = False
    payload['max_tokens'] = max(1, min(int(payload.get('max_tokens') or 500), 800))
    input_tokens = sum(approximate_tokens(item.get('content') or '') for item in payload['messages'])

    request_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    now = time.monotonic()
    with _compute_generation_lock:
        cached = _compute_generation_cache.get(request_hash)
        if cached and now - cached['at'] < 8:
            response = jsonify(cached['body'])
            response.headers['X-Mailmate-Input-Tokens-Approx'] = str(input_tokens)
            response.headers['X-Mailmate-Single-Flight'] = 'cache'
            return response, cached['status']
        flight = _compute_generation_inflight.get(request_hash)
        if flight is None:
            flight = threading.Event()
            _compute_generation_inflight[request_hash] = flight
            leader = True
        else:
            leader = False

    if not leader:
        flight.wait(timeout=int(os.getenv('MAILMATE_WORKER_TIMEOUT_SECONDS', '45')) + 5)
        with _compute_generation_lock:
            cached = _compute_generation_cache.get(request_hash)
        if not cached:
            return jsonify({'error': 'local_model_unavailable'}), 503
        response = jsonify(cached['body'])
        response.headers['X-Mailmate-Input-Tokens-Approx'] = str(input_tokens)
        response.headers['X-Mailmate-Single-Flight'] = 'shared'
        return response, cached['status']

    base = os.getenv("LM_STUDIO_BASE_URL", "http://127.0.0.1:2806/v1").rstrip("/")
    try:
        with inference_broker.slot(request_kind):
            upstream = requests.post(
                f"{base}/chat/completions",
                json=payload,
                timeout=int(os.getenv("MAILMATE_WORKER_TIMEOUT_SECONDS", "45")),
            )
        try:
            body = upstream.json()
            status = upstream.status_code
            if status < 400 and isinstance(body, dict):
                choices = body.get('choices') or []
                message = (choices[0].get('message') or {}) if choices else {}
                content = str(message.get('content') or '').strip()
                reasoning = str(message.get('reasoning_content') or '').strip()
                finish_reason = choices[0].get('finish_reason') if choices else None
                if not content and (reasoning or finish_reason == 'length'):
                    body, status = {
                        'error': 'invalid_model_output',
                        'detail': 'Local model reasoning consumed the response budget.',
                    }, 502
        except Exception:
            body, status = {'error': 'invalid_upstream_response', 'status': upstream.status_code}, 502
    except requests.RequestException:
        body, status = {'error': 'local_model_unavailable'}, 503
    finally:
        with _compute_generation_lock:
            if 'body' in locals():
                _compute_generation_cache.clear()
                _compute_generation_cache[request_hash] = {'at': time.monotonic(), 'body': body, 'status': status}
            event = _compute_generation_inflight.pop(request_hash, None)
            if event:
                event.set()

    response = jsonify(body)
    response.headers['X-Mailmate-Input-Tokens-Approx'] = str(input_tokens)
    response.headers['X-Mailmate-Single-Flight'] = 'leader'
    return response, status
# MAILMATE_EMBEDDED_COMPUTE_PROXY_END

# MAILMATE_REMOTE_COMPUTE_STATUS_START
def _cached_compute_probe(cache_key, probe, ttl=5):
    now = time.monotonic()
    with _compute_health_lock:
        cached = _compute_health_cache.get(cache_key)
        if cached and now - cached['at'] < ttl:
            return dict(cached['value'])
    value = probe()
    with _compute_health_lock:
        _compute_health_cache[cache_key] = {'at': now, 'value': dict(value)}
    return value


def _mailmate_probe_local_compute_uncached():
    base = os.getenv("LM_STUDIO_BASE_URL", "http://127.0.0.1:2806/v1").rstrip("/")
    started = time.perf_counter()
    try:
        response = requests.get(f"{base}/models", timeout=1.6)
        return {
            "configured": True,
            "available": bool(response.ok),
            "status": response.status_code,
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }
    except Exception:
        return {
            "configured": True,
            "available": False,
            "status": None,
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }


def _mailmate_probe_local_compute():
    return _cached_compute_probe('local', _mailmate_probe_local_compute_uncached)


def _mailmate_probe_remote_compute_uncached():
    base = os.getenv("MAILMATE_REMOTE_WORKER_URL", "http://100.114.2.88:5000/api/compute").strip().rstrip("/")
    if not base:
        return {
            "configured": False,
            "available": False,
            "status": None,
            "latency_ms": None,
        }

    started = time.perf_counter()
    try:
        worker_token = os.getenv("MAILMATE_WORKER_TOKEN", "").strip()
        headers = {"Authorization": f"Bearer {worker_token}"} if worker_token else {}
        response = requests.get(f"{base}/health", headers=headers, timeout=1.8)
        payload = response.json() if response.content else {}
        return {
            "configured": True,
            "available": bool(response.ok and payload.get("ok")),
            "status": response.status_code,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "worker": payload.get("worker"),
            "retention": payload.get("retention"),
        }
    except Exception:
        return {
            "configured": True,
            "available": False,
            "status": None,
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }


def _mailmate_probe_remote_compute():
    return _cached_compute_probe('remote', _mailmate_probe_remote_compute_uncached)


@app.route("/api/compute/status")
def mailmate_compute_status():
    is_host = _worker_host_enabled()
    client_local = _env_truthy('MAILMATE_CLIENT_LOCAL_LM')
    local = _mailmate_probe_local_compute() if is_host or client_local else {
        'configured': False, 'available': False, 'status': None, 'latency_ms': None,
    }
    remote = _mailmate_probe_remote_compute() if not is_host else {
        'configured': False, 'available': False, 'status': None, 'latency_ms': None,
    }

    ready = bool(local.get("available") or remote.get("available"))
    mode = (
        "local"
        if local.get("available")
        else "remote_local"
        if remote.get("available")
        else "unavailable"
    )

    label = os.getenv(
        "MAILMATE_COMPUTE_LABEL",
        "Priyam's Tailscale workstation"
    ).strip() or "Priyam's Tailscale workstation"

    role = "host" if is_host else "client"

    return jsonify({
        "ok": True,
        "ready": ready,
        "mode": mode,
        "role": role,
        "local": local,
        "remote": remote,
        "inference": inference_broker.status(),
        "connect_label": label,
        "requires_hotspot": bool(
            not local.get("available")
            and remote.get("configured")
            and not remote.get("available")
        ),
        "message": (
            "Kyle Work is ready."
            if ready
            else f"Connect Tailscale and make sure {label} is online."
            if remote.get("configured")
            else "Kyle Work needs a local or configured team workstation."
        ),
    })
# MAILMATE_REMOTE_COMPUTE_STATUS_END

def _oauth_origin():
    redirect_uri = os.getenv('GOOGLE_REDIRECT_URI', 'http://localhost:5000/auth/google/callback')
    parsed = urlparse(redirect_uri)
    return f"{parsed.scheme or 'http'}://{parsed.netloc or 'localhost:5000'}"


@app.before_request
def keep_oauth_on_one_host():
    # Flask session cookies are host-scoped. If auth starts on 127.0.0.1 but
    # Google redirects to localhost, OAuth state / PKCE verifier are lost.
    if request.path.startswith('/auth/google'):
        target_origin = _oauth_origin()
        target = urlparse(target_origin)
        if target.netloc and request.host != target.netloc:
            query = f"?{request.query_string.decode()}" if request.query_string else ''
            return redirect(f"{target_origin}{request.path}{query}")


@app.route('/auth/google')
def auth_google():
    auth_url, state, code_verifier = get_auth_url()
    session['oauth_state'] = state
    if code_verifier:
        session['code_verifier'] = code_verifier
    return redirect(auth_url)

@app.route('/auth/google/callback')
def auth_google_callback():
    state = session.get('oauth_state')
    code_verifier = session.get('code_verifier')

    if not state:
        return (
            'OAuth session state is missing. Open http://localhost:5000 and sign in again.',
            400,
        )

    try:
        handle_callback(request.url, state=state, code_verifier=code_verifier)
        profile = get_user_profile() or {}
    except Exception as exc:
        app.logger.exception('Google OAuth callback failed')
        return jsonify({
            'error': 'Google OAuth callback failed',
            'detail': str(exc),
            'hasState': bool(state),
            'hasCodeVerifier': bool(code_verifier),
            'callbackHost': request.host,
            'expectedOrigin': _oauth_origin(),
        }), 500
    finally:
        session.pop('oauth_state', None)
        session.pop('code_verifier', None)

    user_id = profile.get('id') or profile.get('sub') or profile.get('email') or ''
    # The worker is defined during module initialization and starts after auth;
    # it does not depend on the dashboard being opened.
    try:
        mail_sync_service.start(profile)
    except Exception as sync_exc:
        app.logger.info('Background Gmail sync will retry on first API request: %s', sync_exc)
    params = {
        'connected': 'true',
        'userId': user_id,
        'name': profile.get('name', ''),
        'picture': profile.get('picture', ''),
    }
    return redirect('/dashboard.html?' + urlencode(params))

@app.route('/api/user/profile')
def user_profile():
    profile = get_user_profile()
    if profile:
        if profile.get('picture'):
            session['user_picture'] = profile['picture']
        return jsonify(profile)
    return jsonify({"error": "Not authenticated"}), 401


@app.route('/api/user/avatar')
def user_avatar():
    profile = get_user_profile()
    picture_url = str((profile or {}).get('picture') or session.get('user_picture') or '').strip()
    if not picture_url:
        abort(404)
    try:
        upstream = requests.get(picture_url, timeout=8)
        upstream.raise_for_status()
        content_type = upstream.headers.get('Content-Type', 'image/jpeg')
        if not content_type.startswith('image/'):
            abort(502)
        return Response(
            upstream.content,
            content_type=content_type,
            headers={'Cache-Control': 'private, max-age=3600'},
        )
    except requests.RequestException:
        app.logger.exception('Google profile image proxy failed')
        abort(502)


@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    session.clear()
    try:
        from services.secure_storage import delete_encrypted_json
        delete_encrypted_json(DATA_DIR / 'google_credentials.json')
    except Exception as exc:
        app.logger.warning('Could not delete local Google credentials: %s', exc)
    return jsonify({"ok": True, "redirect": "/"})


def _utc_now():
    return datetime.now(timezone.utc)


def _parse_utc(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _deadline_target(item):
    raw = str((item or {}).get('deadline') or '').strip()
    if not raw:
        return None, False

    now = datetime.now(APP_TZ)
    lower = raw.lower()

    within = re.search(r'within\s+(\d+)\s*(minutes?|mins?|hours?|hrs?)', lower)
    if within:
        amount = int(within.group(1))
        minutes = amount * 60 if within.group(2).startswith(('hour', 'hr')) else amount
        return now + timedelta(minutes=minutes), True

    if 'tomorrow' in lower:
        target = (now + timedelta(days=1)).replace(hour=23, minute=59, second=0, microsecond=0)
        clock = _parse_clock(raw)
        if clock:
            target = target.replace(hour=clock[0], minute=clock[1])
            return target, True
        return target, False

    if 'today' in lower or 'tonight' in lower:
        target = now.replace(hour=23, minute=59, second=0, microsecond=0)
        clock = _parse_clock(raw)
        if clock:
            target = target.replace(hour=clock[0], minute=clock[1])
            return target, True
        return target, False

    try:
        parsed = date_parser.parse(raw, fuzzy=True, default=now)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=APP_TZ)
        else:
            parsed = parsed.astimezone(APP_TZ)
        has_time = bool(re.search(r'\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b', lower))
        if not has_time:
            parsed = parsed.replace(hour=23, minute=59, second=0, microsecond=0)
        return parsed, has_time
    except Exception:
        return None, False


def _deadline_urgency(target):
    if not target:
        return 'normal'
    remaining = (target - datetime.now(APP_TZ)).total_seconds()
    if remaining <= 6 * 3600:
        return 'critical'
    if remaining <= 24 * 3600:
        return 'urgent'
    return 'normal'


def _deadline_marker(item):
    stable = (
        (item or {}).get('source_message_id')
        or (item or {}).get('message_id')
        or (item or {}).get('email_id')
    )
    if stable:
        return f"gmail-{stable}"
    raw = '|'.join([
        str((item or {}).get('subject') or ''),
        str((item or {}).get('title') or ''),
        str((item or {}).get('description') or (item or {}).get('reason') or ''),
        str((item or {}).get('deadline') or ''),
    ])
    return 'derived-' + hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]


def _deadline_title(item):
    title = (
        (item or {}).get('subject')
        or (item or {}).get('title')
        or _short_task_description(item)
        or 'Email deadline'
    )
    title = re.sub(r'\s+', ' ', str(title)).strip()
    if len(title) > 72:
        title = title[:69].rstrip() + '...'
    return title


def _source_email_for_attention(item, payload):
    source_id = str(
        (item or {}).get('source_message_id')
        or (item or {}).get('message_id')
        or (item or {}).get('email_id')
        or ''
    ).strip()
    if not source_id:
        return None
    for email in (payload or {}).get('emails') or []:
        ids = {
            str(email.get('id') or ''),
            str(email.get('gmail_id') or ''),
        }
        if source_id in ids:
            return email
    return None


def _calendar_write_decision(item, payload, profile=None):
    """Deterministic boundary for automatic email-derived Calendar writes."""
    source = _source_email_for_attention(item, payload)
    if not source:
        return False, 'ambiguous_source'

    direction = str(source.get('direction') or '').strip().lower()
    if direction == 'outbound' or 'SENT' in (source.get('labels') or []):
        return False, 'outbound_email'
    if direction != 'inbound':
        return False, 'ambiguous_direction'

    owner = str((item or {}).get('owner') or '').strip().lower()
    current_email = str((profile or {}).get('email') or '').strip().lower()
    current_names = {
        'me', 'myself', 'current_user', 'current user', 'user',
        current_email,
        str((profile or {}).get('name') or '').strip().lower(),
    }
    current_names.discard('')
    if owner not in current_names:
        return False, 'not_assigned_to_current_user' if owner else 'ambiguous_owner'

    return True, 'inbound_assigned_to_current_user'


def _reconcile_ai_calendar(payload, profile=None, force=False):
    """
    Persist high-confidence email deadlines into the currently connected
    Google Calendar. Managed markers make this safe to run repeatedly.
    """
    status = calendar_access_status()
    if not status.get('authenticated') or status.get('writable') is False:
        return {"created_or_updated": 0, "skipped": 0, "enabled": False}

    account_key = str((profile or {}).get('email') or (profile or {}).get('id') or 'default')
    now_ts = time.time()
    if not force and now_ts - _ai_calendar_last_sync.get(account_key, 0) < AI_CALENDAR_SYNC_SECONDS:
        return {"created_or_updated": 0, "skipped": 0, "enabled": True, "throttled": True}

    _ai_calendar_last_sync[account_key] = now_ts
    changed = 0
    skipped = 0
    skip_reasons = {}

    for item in (payload or {}).get('needs_attention') or []:
        if not item.get('deadline'):
            continue

        may_write, policy_reason = _calendar_write_decision(item, payload, profile=profile)
        item['calendar_write'] = {'allowed': may_write, 'reason': policy_reason}
        if not may_write:
            skipped += 1
            skip_reasons[policy_reason] = skip_reasons.get(policy_reason, 0) + 1
            continue

        target, has_time = _deadline_target(item)
        if not target:
            skipped += 1
            continue

        urgency = _deadline_urgency(target)
        marker = _deadline_marker(item)
        if marker in _calendar_dismissed_markers(profile):
            skipped += 1
            skip_reasons['user_deleted'] = skip_reasons.get('user_deleted', 0) + 1
            continue
        description = str(
            item.get('description')
            or item.get('reason')
            or 'Automatically created from an email deadline by Mailmate.'
        ).strip()

        if has_time:
            # A deadline is represented as a short block ending at the due time.
            start = target - timedelta(minutes=30)
            end = target
            event_payload = {
                "title": _deadline_title(item),
                "description": description + "\n\nCreated automatically by Mailmate from Gmail.",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "all_day": False,
                "urgency": urgency,
            }
        else:
            event_payload = {
                "title": _deadline_title(item),
                "description": description + "\n\nCreated automatically by Mailmate from Gmail.",
                "start": target.date().isoformat(),
                "end": (target.date() + timedelta(days=1)).isoformat(),
                "all_day": True,
                "urgency": urgency,
            }

        try:
            calendar_upsert_ai_deadline(marker, event_payload)
            changed += 1
        except Exception as exc:
            app.logger.warning('AI calendar reconcile skipped %s: %s', marker, exc)
            skipped += 1

    return {
        "created_or_updated": changed,
        "skipped": skipped,
        "skip_reasons": skip_reasons,
        "enabled": True,
    }


def _mailbox_snapshot(profile, force=False):
    """Use Gmail historyId as the cheap no-change path; raw data stays in process RAM."""
    account_key = str(profile.get('id') or profile.get('sub') or profile.get('email') or 'default')
    try:
        history_id = get_gmail_history_id()
    except Exception:
        history_id = None
    with _mailbox_snapshot_lock:
        cached = _mailbox_snapshots.get(account_key)
    if not cached:
        # Restore only minimized active context and the Gmail checkpoint. Raw mail
        # is always fetched from Gmail and is never hydrated from Supabase.
        mail_context_service.hydrate(account_key)
        persisted_sync = mail_context_service.load_sync_state(account_key)
    else:
        persisted_sync = {}
    with _mailbox_snapshot_lock:
        cached = _mailbox_snapshots.get(account_key)
        if not force and cached and history_id and cached.get('history_id') == history_id:
            return deepcopy(cached['threads']), deepcopy(cached['emails']), True, False, history_id

    # A temporary history/quota failure must not blank an already loaded inbox.
    if not force and cached and not history_id:
        return deepcopy(cached['threads']), deepcopy(cached['emails']), True, False, cached.get('history_id')

    if not force and cached and cached.get('history_id') and history_id:
        try:
            delta = get_gmail_thread_changes(
                cached['history_id'],
                deepcopy(cached['threads']),
                my_email=profile.get('email') or '',
            )
            if not delta.get('full_sync_required'):
                threads = delta.get('threads') or []
                emails = delta.get('emails') or []
                resolved_history_id = delta.get('history_id') or history_id
                with _mailbox_snapshot_lock:
                    _mailbox_snapshots[account_key] = {
                        'history_id': resolved_history_id,
                        'threads': deepcopy(threads),
                        'emails': deepcopy(emails),
                    }
                changed = bool(delta.get('changed_message_ids'))
                return threads, emails, not changed, changed, resolved_history_id
        except Exception as exc:
            app.logger.info('Gmail incremental sync fell back to a bounded full scan: %s', exc)

    try:
        threads, emails = get_gmail_threads()
    except Exception:
        if cached:
            app.logger.warning('Gmail full scan failed; serving the last in-memory mailbox snapshot')
            return deepcopy(cached['threads']), deepcopy(cached['emails']), True, False, cached.get('history_id')
        raise
    with _mailbox_snapshot_lock:
        _mailbox_snapshots[account_key] = {
            'history_id': history_id,
            'threads': deepcopy(threads),
            'emails': deepcopy(emails),
        }
    prior_history_id = persisted_sync.get('last_history_id')
    source_changed = not prior_history_id or str(prior_history_id) != str(history_id or '')
    return threads, emails, False, source_changed, history_id


def _build_live_dashboard(profile, force_ai=False):
    threads, emails, mailbox_cached, source_changed, history_id = _mailbox_snapshot(profile, force=force_ai)
    if not threads:
        raise RuntimeError('No Gmail threads are available')

    # Two-Plane Architecture: Annotate all emails with Privacy Gate for Display Plane
    for email in emails:
        email['privacy_gate'] = PrivacyGate.evaluate(email)

    user_id = profile.get('id') or profile.get('sub') or profile.get('email') or ''
    context_result = mail_context_service.update(user_id, emails)
    replied_source_ids = _resolved_message_ids_from_threads(threads)
    if replied_source_ids:
        mail_context_service.delete_context(user_id, replied_source_ids)
    mail_context_service.save_sync_state(
        user_id,
        history_id,
        full_scan=not mailbox_cached and bool(source_changed),
    )
    context_by_message = {row['gmail_message_id']: row for row in context_result['rows']}
    for email in emails:
        email['context_scores'] = context_by_message.get(str(email.get('id') or email.get('gmail_id') or ''), {})
    for thread in threads:
        for message in thread.get('messages') or []:
            message['context_scores'] = context_by_message.get(str(message.get('id') or message.get('gmail_id') or ''), {})

    ai_shielded = sum(1 for e in emails if not e.get('privacy_gate', {}).get('ai_allowed', True))
    work_active = sum(
        1 for e in emails
        if e.get('privacy_gate', {}).get('work_agent_allowed', False)
        and e.get('context_scores', {}).get('work_allowed', False)
    )
    privacy_summary = {
        "display_plane_total": len(emails),
        "ai_allowed_count": len(emails) - ai_shielded,
        "ai_shielded_count": ai_shielded,
        "work_active_count": work_active,
        "central_retention": "derived-only; raw mail stays transient"
    }

    # Gmail -> database is prohibited. Raw mail remains transient in process/browser RAM.
    overview = get_dashboard_overview(threads)
    overview['emails'] = emails
    overview['privacy_summary'] = privacy_summary
    overview['user'] = profile
    overview['user_id'] = profile.get('email') or profile.get('id') or ''
    overview['cached'] = mailbox_cached
    overview['source_changed'] = source_changed
    overview['gmail_history_id'] = history_id
    overview['context_sync'] = {
        'changed_messages': context_result['changed_count'],
        'classifier_version': 1,
        'storage': context_result['persistence'],
    }
    overview['calendar_dismissed_markers'] = sorted(_calendar_dismissed_markers(profile))
    overview = _prune_resolved_attention(overview, replied_source_ids)
    overview['ai_calendar_sync'] = _reconcile_ai_calendar(overview, profile=profile, force=False)

    return overview


mail_sync_service = MailSyncService(
    snapshot_builder=_build_live_dashboard,
    enqueue_work=work_agent_service.sync_and_enqueue,
    interval_seconds=CACHE_SYNC_SECONDS,
)


@app.route('/api/dashboard/overview')
def dashboard_overview():
    try:
        profile = get_user_profile()
        if not profile:
            return jsonify({"error": "Not authenticated"}), 401

        force = str(request.args.get('refresh', '')).lower() in {'1', 'true', 'yes'}
        live_payload = _build_live_dashboard(profile, force_ai=force)
        user_id = profile.get('email') or profile.get('id')
        mail_sync_service.start(profile)
        try:
            if force:
                # Manual refresh is reconciliation-only: fetch current Gmail state
                # and resolve/delete existing work without starting new AI work.
                work_agent_service.reconcile_jobs_with_gmail(user_id)
            else:
                # Job IDs are deterministic, so this safely fills an empty Work
                # queue without duplicating work on subsequent dashboard reads.
                work_agent_service.sync_and_enqueue(user_id, live_payload)
        except Exception as sync_exc:
            app.logger.debug('Work sync notice: %s', sync_exc)
        live_payload = _prune_resolved_attention(
            live_payload,
            work_agent_service.resolved_source_message_ids(user_id),
        )
        return jsonify(live_payload)
    except Exception as exc:
        app.logger.exception('Dashboard processing failed')
        return jsonify({"error": str(exc)}), 500


@app.route('/api/work/jobs', methods=['GET'])
def list_work_jobs():
    profile = get_user_profile()
    if not profile:
        return jsonify({"error": "Not authenticated"}), 401
    user_id = profile.get('email') or profile.get('id')
    reconcile = str(request.args.get('reconcile', '')).lower() in {'1', 'true', 'yes'}
    jobs = work_agent_service.list_jobs(user_id, reconcile=reconcile)

    ensure = str(request.args.get('ensure', '')).lower() in {'1', 'true', 'yes'}
    if ensure:
        try:
            # Always reconcile the current dashboard into Work when explicitly
            # requested. Job IDs are deterministic, so this adds newly actionable
            # mail without duplicating existing Work history.
            live_payload = _build_live_dashboard(profile, force_ai=False)
            work_agent_service.sync_and_enqueue(user_id, live_payload)
            jobs = work_agent_service.list_jobs(user_id, reconcile=False)
            system_context_service.increment_version()
        except Exception as ensure_exc:
            app.logger.debug('Work ensure sync notice: %s', ensure_exc)

    return jsonify(jobs)


@app.route('/api/work/jobs/<job_id>', methods=['GET'])
def get_work_job(job_id):
    profile = get_user_profile()
    if not profile:
        return jsonify({"error": "Not authenticated"}), 401
    user_id = profile.get('email') or profile.get('id')
    job = work_agent_service.get_job(job_id, user_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route('/api/work/jobs/<job_id>/run', methods=['POST'])
def run_work_job(job_id):
    profile = get_user_profile()
    if not profile:
        return jsonify({"error": "Not authenticated"}), 401
    user_id = profile.get('email') or profile.get('id')
    job = work_agent_service.run_job(job_id, user_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route('/api/work/jobs/<job_id>/cancel', methods=['POST'])
def cancel_work_job(job_id):
    profile = get_user_profile()
    if not profile:
        return jsonify({"error": "Not authenticated"}), 401
    user_id = profile.get('email') or profile.get('id')
    job = work_agent_service.cancel_job(job_id, user_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route('/api/work/jobs/<job_id>/approve', methods=['POST'])
def approve_work_job(job_id):
    profile = get_user_profile()
    if not profile:
        return jsonify({"error": "Not authenticated"}), 401
    user_id = profile.get('email') or profile.get('id')
    data = request.get_json(silent=True) or {}
    edited_reply = data.get('reply')
    try:
        job = work_agent_service.approve_job(job_id, user_id, edited_reply=edited_reply)
        return jsonify({"ok": True, "job": job})
    except GmailInsufficientPermissionError as exc:
        app.logger.warning("Gmail approval permission error: %s", exc)
        return jsonify({
            "ok": False,
            "error": "Google account needs reconnection to grant Gmail draft & send permissions (gmail.modify).",
            "code": "insufficient_scopes",
            "reconnect_url": "/auth/google"
        }), 403
    except Exception as e:
        app.logger.exception("Job approval failed")
        err_str = str(e).lower()
        if "insufficient" in err_str or "permission" in err_str or "403" in err_str:
            return jsonify({
                "ok": False,
                "error": "Google account needs reconnection to grant Gmail draft & send permissions (gmail.modify).",
                "code": "insufficient_scopes",
                "reconnect_url": "/auth/google"
            }), 403
        return jsonify({"error": str(e)}), 500


@app.route('/api/work/settings', methods=['GET', 'POST'])
def work_settings():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        settings = work_agent_service.update_settings(data)
        return jsonify({"ok": True, "settings": settings})
    return jsonify(work_agent_service.get_settings())


def _automation_user_id():
    profile = get_user_profile()
    return (profile or {}).get('email') or (profile or {}).get('id')


@app.route('/api/automations', methods=['GET', 'POST'])
def automations_collection():
    user_id = _automation_user_id()
    if not user_id:
        return jsonify({'error': 'Not authenticated'}), 401
    try:
        if request.method == 'POST':
            return jsonify(automation_service.create(user_id, request.get_json(silent=True) or {})), 201
        return jsonify(automation_service.list(user_id))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400


@app.route('/api/automations/<automation_id>', methods=['GET', 'PATCH', 'DELETE'])
def automation_detail(automation_id):
    user_id = _automation_user_id()
    if not user_id:
        return jsonify({'error': 'Not authenticated'}), 401
    try:
        if request.method == 'DELETE':
            if not automation_service.delete(automation_id, user_id):
                return jsonify({'error': 'Automation not found'}), 404
            return jsonify({'ok': True, 'deleted': automation_id})
        if request.method == 'PATCH':
            automation = automation_service.update(automation_id, user_id, request.get_json(silent=True) or {})
        else:
            automation = automation_service.get(automation_id, user_id)
        if not automation:
            return jsonify({'error': 'Automation not found'}), 404
        return jsonify(automation)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400


@app.route('/api/automations/<automation_id>/run', methods=['POST'])
def automation_run_now(automation_id):
    user_id = _automation_user_id()
    if not user_id:
        return jsonify({'error': 'Not authenticated'}), 401
    result = automation_service.run_now(automation_id, user_id)
    if not result:
        return jsonify({'error': 'Automation not found'}), 404
    return jsonify(result), 202


@app.route('/api/work/jobs/<job_id>/cancel-countdown', methods=['POST'])
def cancel_work_countdown(job_id):
    profile = get_user_profile()
    if not profile:
        return jsonify({"error": "Not authenticated"}), 401
    user_id = profile.get('email') or profile.get('id')
    job = work_agent_service.cancel_countdown(job_id, user_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"ok": True, "job": job})


@app.route('/api/work/jobs/<job_id>/save-draft', methods=['POST'])
def save_work_draft(job_id):
    profile = get_user_profile()
    if not profile:
        return jsonify({"error": "Not authenticated"}), 401
    user_id = profile.get('email') or profile.get('id')
    data = request.get_json(silent=True) or {}
    edited_reply = data.get('reply', '')
    try:
        job = work_agent_service.save_draft(job_id, user_id, edited_reply)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        return jsonify({"ok": True, "job": job})
    except GmailInsufficientPermissionError as exc:
        app.logger.warning("Gmail save-draft permission error: %s", exc)
        return jsonify({
            "ok": False,
            "error": "Google account needs reconnection to grant Gmail draft & send permissions (gmail.modify).",
            "code": "insufficient_scopes",
            "reconnect_url": "/auth/google"
        }), 403
    except Exception as e:
        app.logger.exception("Save draft failed")
        err_str = str(e).lower()
        if "insufficient" in err_str or "permission" in err_str or "403" in err_str:
            return jsonify({
                "ok": False,
                "error": "Google account needs reconnection to grant Gmail draft & send permissions (gmail.modify).",
                "code": "insufficient_scopes",
                "reconnect_url": "/auth/google"
            }), 403
        return jsonify({"error": str(e)}), 500




@app.route('/api/work/jobs/<job_id>/artifacts/<filename>', methods=['GET'])
def get_work_artifact(job_id, filename):
    profile = get_user_profile()
    if not profile:
        return jsonify({"error": "Not authenticated"}), 401
    job_dir = BASE_DIR / 'workspaces' / job_id
    artifact_path = job_dir / filename
    if not artifact_path.is_file():
        abort(404)
    return send_from_directory(str(job_dir), filename)


@app.route('/api/system/context', methods=['GET'])
def system_context_endpoint():
    profile = get_user_profile()
    if not profile:
        return jsonify({"error": "Not authenticated"}), 401
    user_id = profile.get('email') or profile.get('id') or 'default'
    force_reconcile = str(request.args.get('reconcile', 'true')).lower() in {'1', 'true', 'yes'}
    ctx = system_context_service.build(user_id=user_id, force_reconcile=force_reconcile)
    return jsonify(ctx)


@app.route('/api/work/reconcile', methods=['POST'])
def work_reconcile_endpoint():
    profile = get_user_profile()
    if not profile:
        return jsonify({"error": "Not authenticated"}), 401
    user_id = profile.get('email') or profile.get('id') or 'default'
    reconciled = work_agent_service.reconcile_jobs_with_gmail(user_id)
    jobs = work_agent_service.list_jobs(user_id, reconcile=False)
    system_context_service.increment_version()
    return jsonify({
        "ok": True,
        "reconciled": reconciled,
        "reconciled_count": len(reconciled),
        "jobs": jobs,
        "context_version": system_context_service.get_version()
    })


@app.route('/api/cache/status')
def cache_status():
    profile = get_user_profile()
    if not profile:
        return jsonify({"authenticated": False, "supabase": {"enabled": False, "configured": False, "ready": False}}), 401

    supabase_status = mail_context_service.status()
    return jsonify({
        "authenticated": True,
        "supabase": supabase_status,
        "cache": {
            "available": bool(supabase_status.get('ready')),
            "mode": "encrypted-derived-context" if supabase_status.get('ready') else "live-gmail",
            "retention": "encrypted-derived-context-only",
        },
        "policy": {
            "sync_check_seconds": CACHE_SYNC_SECONDS,
            "reprocess_seconds": CACHE_REPROCESS_SECONDS,
        }
    })


@app.route('/api/gmail/messages/<message_id>', methods=['DELETE'])
def gmail_message_delete(message_id):
    profile = get_user_profile()
    if not profile:
        return jsonify({'error': 'Not authenticated'}), 401
    try:
        result = trash_gmail_message(message_id)
        return jsonify(result)
    except Exception as exc:
        app.logger.exception('Gmail trash failed')
        detail = str(exc)
        status = 403 if ('insufficient' in detail.lower() or 'permission' in detail.lower() or 'scope' in detail.lower()) else 500
        return jsonify({
            'error': detail,
            'hint': 'Reconnect Google once so Mailmate can receive the gmail.modify scope.' if status == 403 else None,
        }), status


@app.route('/api/gmail/messages/<message_id>', methods=['GET'])
def gmail_message_detail(message_id):
    if not get_user_profile():
        return jsonify({'error': 'Not authenticated'}), 401
    try:
        return jsonify(get_gmail_message(message_id))
    except Exception as exc:
        app.logger.exception('Gmail message fetch failed')
        return jsonify({'error': str(exc)}), 500


@app.route('/api/gmail/messages/<message_id>/read', methods=['POST'])
def gmail_message_read(message_id):
    if not get_user_profile():
        return jsonify({'error': 'Not authenticated'}), 401
    try:
        return jsonify(mark_gmail_message_read(message_id))
    except Exception as exc:
        app.logger.exception('Gmail mark-read failed')
        detail = str(exc)
        status = 403 if ('insufficient' in detail.lower() or 'permission' in detail.lower() or 'scope' in detail.lower()) else 500
        return jsonify({
            'error': detail,
            'hint': 'Reconnect Google once so Mailmate can receive the gmail.modify scope.' if status == 403 else None,
        }), status


@app.route('/api/gmail/messages/<message_id>/important', methods=['POST'])
def gmail_message_important(message_id):
    if not get_user_profile():
        return jsonify({'error': 'Not authenticated'}), 401
    try:
        return jsonify(mark_gmail_message_important(message_id))
    except Exception as exc:
        app.logger.exception('Gmail mark-important failed')
        detail = str(exc)
        status = 403 if ('insufficient' in detail.lower() or 'permission' in detail.lower() or 'scope' in detail.lower()) else 500
        return jsonify({
            'error': detail,
            'hint': 'Reconnect Google once so Mailmate can receive the gmail.modify scope.' if status == 403 else None,
        }), status


@app.route('/api/mail/send', methods=['POST'])
def send_mail_endpoint():
    profile = get_user_profile()
    if not profile:
        return jsonify({'error': 'Not authenticated'}), 401

    data = request.get_json(silent=True) or {}
    operation_id = str(data.get('operation_id') or '').strip()
    to = str(data.get('to') or data.get('recipient') or '').strip()
    subject = str(data.get('subject') or 'No Subject').strip()
    body = str(data.get('body') or '').strip()
    thread_id = data.get('thread_id')
    in_reply_to = data.get('in_reply_to')
    draft_id = data.get('draft_id')

    _, recipient = parseaddr(to)
    recipient = recipient.strip().lower()
    if not re.fullmatch(r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}', recipient, re.I):
        return jsonify({'code': 'invalid_recipient', 'error': 'A valid recipient email is required.'}), 400
    if not body:
        return jsonify({'code': 'empty_body', 'error': 'Message body is required.'}), 400
    if not operation_id or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', operation_id):
        return jsonify({'code': 'invalid_operation_id', 'error': 'A valid operation_id is required.'}), 400

    user_id = str(profile.get('email') or profile.get('id') or 'default').lower()
    operation_key = f'{user_id}:{operation_id}'
    payload_digest = hashlib.sha256(json.dumps({
        'to': recipient,
        'subject': subject,
        'body': body,
        'thread_id': thread_id,
        'in_reply_to': in_reply_to,
        'draft_id': draft_id,
    }, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    message_id_header = f'<mailmate.{operation_id}@mailmate.local>'

    with _mail_send_lock:
        cutoff = time.time() - 1800
        for key, value in list(_mail_send_operations.items()):
            if value.get('updated_at', 0) < cutoff:
                _mail_send_operations.pop(key, None)
        existing = _mail_send_operations.get(operation_key)
        if existing and existing.get('digest') != payload_digest:
            return jsonify({'code': 'operation_conflict', 'error': 'This send operation was already used for different content.'}), 409
        if existing and existing.get('status') == 'sent':
            return jsonify({'ok': True, 'status': 'sent', 'message_id': existing.get('message_id'), 'idempotent_replay': True})
        if existing and existing.get('status') == 'pending':
            return jsonify({'ok': False, 'status': 'sending', 'operation_id': operation_id}), 202

    if existing and existing.get('status') == 'unknown' and not draft_id:
        try:
            reconciled = find_sent_message_by_rfc_id(message_id_header)
        except Exception:
            reconciled = None
        if reconciled:
            with _mail_send_lock:
                _mail_send_operations[operation_key] = {
                    'digest': payload_digest, 'status': 'sent', 'message_id': reconciled.get('id'), 'updated_at': time.time(),
                }
            return jsonify({'ok': True, 'status': 'sent', 'message_id': reconciled.get('id'), 'reconciled': True})
        return jsonify({'ok': False, 'status': 'unknown', 'operation_id': operation_id, 'code': 'send_unconfirmed'}), 202

    with _mail_send_lock:
        _mail_send_operations[operation_key] = {
            'digest': payload_digest, 'status': 'pending', 'message_id': None, 'updated_at': time.time(),
        }

    try:
        if draft_id:
            result = send_gmail_draft(draft_id)
        else:
            result = send_gmail_direct(
                to=recipient,
                subject=subject,
                body=body,
                thread_id=thread_id,
                in_reply_to=in_reply_to,
                message_id_header=message_id_header,
            )
        msg_id = result.get('id') if isinstance(result, dict) else (result if isinstance(result, str) else None)
        with _mail_send_lock:
            _mail_send_operations[operation_key] = {
                'digest': payload_digest, 'status': 'sent', 'message_id': msg_id, 'updated_at': time.time(),
            }
        return jsonify({'ok': True, 'status': 'sent', 'result': result, 'message_id': msg_id})
    except GmailInsufficientPermissionError as exc:
        with _mail_send_lock:
            _mail_send_operations[operation_key] = {
                'digest': payload_digest, 'status': 'error', 'message_id': None, 'updated_at': time.time(),
            }
        return jsonify({'code': 'reconnect_google', 'error': str(exc)}), 403
    except Exception as exc:
        app.logger.exception('Send mail failed')
        reconciled = None
        if not draft_id:
            try:
                reconciled = find_sent_message_by_rfc_id(message_id_header)
            except Exception:
                pass
        if reconciled:
            with _mail_send_lock:
                _mail_send_operations[operation_key] = {
                    'digest': payload_digest, 'status': 'sent', 'message_id': reconciled.get('id'), 'updated_at': time.time(),
                }
            return jsonify({'ok': True, 'status': 'sent', 'message_id': reconciled.get('id'), 'reconciled': True})
        with _mail_send_lock:
            _mail_send_operations[operation_key] = {
                'digest': payload_digest, 'status': 'unknown', 'message_id': None, 'updated_at': time.time(),
            }
        return jsonify({
            'code': 'send_unconfirmed',
            'error': "Couldn't confirm send; checking Gmail is required.",
            'status': 'unknown',
            'operation_id': operation_id,
        }), 503


@app.route('/api/stt/status', methods=['GET'])
def stt_status():
    return jsonify(whisper_service.get_status())


@app.route('/api/stt/transcribe', methods=['POST'])
def stt_transcribe():
    if 'audio' not in request.files:
        return jsonify({'error': 'No audio file provided'}), 400
    audio_file = request.files['audio']
    if not audio_file.filename:
        return jsonify({'error': 'Empty audio file'}), 400

    import tempfile
    ext = os.path.splitext(audio_file.filename)[1] or '.webm'
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp_path = tmp.name
        audio_file.save(tmp_path)

    try:
        result = whisper_service.transcribe(tmp_path)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


@app.route('/api/calendar/ai-sync', methods=['POST'])
def calendar_ai_sync():
    profile = get_user_profile()
    if not profile:
        return jsonify({"error": "Not authenticated"}), 401

    try:
        threads, _emails, _cached, _changed, _history_id = _mailbox_snapshot(profile, force=False)
        if not threads:
            return jsonify({"ok": True, "created_or_updated": 0, "reason": "No Gmail threads"})
        overview = get_dashboard_overview(threads)
        force = str(request.args.get('refresh', '')).lower() in {'1', 'true', 'yes'}
        result = _reconcile_ai_calendar(overview, profile=profile, force=force)
        return jsonify({"ok": True, **result})
    except Exception as exc:
        app.logger.warning('Calendar AI sync deferred: %s', exc)
        return jsonify({"ok": False, "created_or_updated": 0, "reason": str(exc)}), 503


@app.route('/api/calendar/events', methods=['GET', 'POST'])
def calendar_events():
    try:
        if request.method == 'POST':
            payload = request.get_json(silent=True) or {}
            return jsonify(calendar_create_event(payload)), 201

        events = calendar_list_events(
            start=request.args.get('start'),
            end=request.args.get('end'),
            limit=request.args.get('limit', 250),
        )
        return jsonify(events)
    except Exception as exc:
        app.logger.exception('Calendar request failed')
        return jsonify({"error": str(exc)}), 500


@app.route('/api/calendar/events/delete-batch', methods=['POST'])
def calendar_delete_batch():
    payload = request.get_json(silent=True) or {}
    event_ids = list(dict.fromkeys(str(value).strip() for value in (payload.get('event_ids') or []) if str(value).strip()))[:20]
    if not event_ids:
        return jsonify({'error': 'event_ids is required'}), 400
    profile = get_user_profile() or {}
    deleted = []
    failed = []
    for event_id in event_ids:
        try:
            result = calendar_delete_event(event_id)
            marker = str((result or {}).get('marker') or '').strip()
            if marker:
                _remember_calendar_dismissal(profile, marker)
                result['dismissed_marker'] = marker
            deleted.append(result)
        except Exception as exc:
            failed.append({'id': event_id, 'error': str(exc)})
    system_context_service.increment_version()
    return jsonify({
        'ok': not failed,
        'deleted': deleted,
        'deleted_count': len(deleted),
        'failed': failed,
        'failed_count': len(failed),
    }), 200 if deleted else 500


@app.route('/api/calendar/deadlines/dismiss', methods=['POST'])
def dismiss_calendar_deadline():
    payload = request.get_json(silent=True) or {}
    marker = str(payload.get('marker') or '').strip()
    if not marker:
        return jsonify({'error': 'marker is required'}), 400
    profile = get_user_profile()
    if not profile:
        return jsonify({'error': 'Not authenticated'}), 401
    _remember_calendar_dismissal(profile, marker)
    system_context_service.increment_version()
    return jsonify({'ok': True, 'dismissed_marker': marker})


@app.route('/api/calendar/events/<event_id>', methods=['PATCH', 'DELETE'])
def calendar_event_detail(event_id):
    try:
        if request.method == 'DELETE':
            result = calendar_delete_event(event_id)
            marker = str((result or {}).get('marker') or '').strip()
            if marker:
                profile = get_user_profile() or {}
                _remember_calendar_dismissal(profile, marker)
                result['dismissed_marker'] = marker
            system_context_service.increment_version()
            return jsonify(result)
        payload = request.get_json(silent=True) or {}
        return jsonify(calendar_update_event(event_id, payload))
    except Exception as exc:
        app.logger.exception('Calendar mutation failed')
        return jsonify({"error": str(exc)}), 500


@app.route('/api/calendar/sync')
def calendar_sync():
    """Small endpoint used by the frontend/Kyle to force a current read."""
    try:
        return jsonify({
            "events": calendar_list_events(
                start=request.args.get('start'),
                end=request.args.get('end'),
                limit=request.args.get('limit', 250),
            ),
            "synced_at": datetime.now(APP_TZ).isoformat(),
            "timezone": APP_TIMEZONE,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

def _day_window_from_text(text):
    now = datetime.now(APP_TZ)
    lower = (text or '').lower()
    if 'tomorrow' in lower:
        day = now + timedelta(days=1)
    elif 'today' in lower or 'tonight' in lower:
        day = now
    else:
        weekdays = {
            'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
            'friday': 4, 'saturday': 5, 'sunday': 6,
        }
        target = next((n for name, n in weekdays.items() if name in lower), None)
        if target is None:
            day = now
        else:
            delta = (target - now.weekday()) % 7
            if delta == 0 and 'next ' in lower:
                delta = 7
            day = now + timedelta(days=delta)

    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = day.replace(hour=23, minute=59, second=59, microsecond=0)
    return start, end


def _parse_clock(text):
    lower = (text or '').lower()
    m = re.search(r'\b(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', lower)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        if hour == 12:
            hour = 0
        if m.group(3) == 'pm':
            hour += 12
        return hour, minute

    m = re.search(r'\bat\s+([01]?\d|2[0-3]):([0-5]\d)\b', lower)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _extract_event_title(text):
    raw = (text or '').strip()
    called = re.search(r'\bcalled\s+(.+?)(?:\s+(?:today|tomorrow|on\s+\w+|at\s+\d)|$)', raw, re.I)
    if called:
        return called.group(1).strip(' .')

    # "schedule C2C test tomorrow at 5 pm"
    m = re.search(
        r'\b(?:add|create|schedule|book)\s+(?:an?\s+)?(?:event\s+)?(.+?)(?=\s+(?:today|tomorrow|on\s+\w+|at\s+\d)|$)',
        raw,
        re.I,
    )
    if m:
        title = m.group(1).strip(' .')
        title = re.sub(r'^(?:for\s+)?', '', title).strip()
        if title:
            return title
    return 'New event'


def _natural_event_payload(text):
    day_start, _ = _day_window_from_text(text)
    clock = _parse_clock(text)
    if not clock:
        return None

    hour, minute = clock
    start = day_start.replace(hour=hour, minute=minute)
    duration = 60
    dm = re.search(r'\bfor\s+(\d+)\s*(minutes?|mins?|hours?|hrs?)\b', text or '', re.I)
    if dm:
        amount = int(dm.group(1))
        duration = amount * 60 if dm.group(2).lower().startswith(('hour', 'hr')) else amount

    return {
        "title": _extract_event_title(text),
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=max(15, duration))).isoformat(),
        "description": "Created by Kyle from Agent Harness.",
    }


def _event_brief_item(event):
    start = event.get('start') or ''
    label = start
    try:
        if 'T' in start:
            dt = datetime.fromisoformat(start.replace('Z', '+00:00')).astimezone(APP_TZ)
            label = dt.strftime('%a %d %b - %-I:%M %p') if os.name != 'nt' else dt.strftime('%a %d %b - %#I:%M %p')
        elif start:
            dt = datetime.fromisoformat(start[:10])
            label = dt.strftime('%a %d %b - all day')
    except Exception:
        pass
    return {
        "title": event.get('title') or 'Event',
        "meta": label,
        "conflict": bool(event.get('conflict')),
        "id": event.get('id'),
    }


def _short_task_description(item):
    text = str(item.get('description') or item.get('reason') or item.get('subject') or 'your highest-priority task').strip()
    text = re.sub(r'\s+', ' ', text)
    return text[:120].rstrip(' ,.;')


def _kyle_fast_path(message, context, selected_event_id=None):
    lower = (message or '').lower().strip()

    preference = None
    if re.search(r'\b(?:use|switch to|turn on|enable)\s+(?:the\s+)?dark\s+mode\b', lower):
        preference = ('theme', 'dark', 'Dark mode is on.')
    elif re.search(r'\b(?:use|switch to|turn on|enable)\s+(?:the\s+)?(?:light|white)\s+mode\b', lower):
        preference = ('theme', 'light', 'Light mode is on.')
    elif re.search(r'\b(?:mute|turn off|disable)\s+(?:(?:your|kyle(?:\'s)?)\s+)?(?:voice|audio|speech)\b', lower):
        preference = ('voice', 'muted', 'Kyle voice is muted.')
    elif re.search(r'\b(?:unmute|turn on|enable)\s+(?:(?:your|kyle(?:\'s)?)\s+)?(?:voice|audio|speech)\b', lower):
        preference = ('voice', 'enabled', 'Kyle voice is on.')
    elif re.search(r'\b(?:turn on|enable)\s+(?:the\s+)?developer\s+mode\b', lower):
        preference = ('developer_mode', 'enabled', 'Developer mode is on. Diagnostics are available in Settings.')
    elif re.search(r'\b(?:turn off|disable)\s+(?:the\s+)?developer\s+mode\b', lower):
        preference = ('developer_mode', 'disabled', 'Developer mode is off.')

    if preference:
        key, value, reply = preference
        return {
            "reply": reply,
            "voice": reply,
            "command": {"type": "set_preference", "key": key, "value": value},
            "handled": True,
        }

    if re.search(r'\b(open|show)\s+(my\s+)?calendar\b', lower) and not re.search(r'\b(today|tomorrow|week|schedule|what|events?)\b', lower):
        return {
            "reply": "Calendar opened.",
            "voice": "Calendar opened.",
            "command": {"type": "open_page", "page": "calendar"},
            "handled": True,
        }

    if re.search(r'\b(drafts?|pending drafts?|waiting for review|waiting approval|prepared work|what have you prepared|any drafts)\b', lower):
        work_section = (context or {}).get('work') or {}
        waiting = work_section.get('waiting_approval') or []
        active = work_section.get('active') or []
        if not waiting and not active:
            return {
                "reply": "There are no drafts waiting for your review right now. (Any thread you replied to in Gmail has been marked resolved.)",
                "voice": "No drafts are waiting for your review. You are all caught up.",
                "command": {"type": "open_page", "page": "work"},
                "handled": True,
            }
        elif waiting:
            first = waiting[0]
            subj = (first.get('source') or {}).get('subject') or first.get('clean_title') or first.get('title') or 'Email'
            plural = "s" if len(waiting) != 1 else ""
            return {
                "reply": f"You have {len(waiting)} draft{plural} ready for review, including '{subj}'.",
                "voice": f"You have {len(waiting)} draft ready for review. First is {subj}.",
                "command": {"type": "open_page", "page": "work"},
                "handled": True,
            }

    if re.search(r'\b(clash(es)?|conflicts?|overlapping|overlap)\b', lower) and not re.search(r'\b(add|create|delete|remove)\b', lower):
        cal_section = (context or {}).get('calendar') or {}
        conflict_count = cal_section.get('conflict_count', 0)
        if conflict_count == 0:
            return {
                "reply": "No schedule clashes detected. Your calendar has no overlapping commitments.",
                "voice": "No schedule clashes detected. Your calendar is clear of overlaps.",
                "command": {"type": "open_page", "page": "calendar"},
                "handled": True,
            }
        else:
            plural = "es" if conflict_count != 1 else ""
            return {
                "reply": f"You have {conflict_count} schedule clash{plural} this week.",
                "voice": f"You have {conflict_count} schedule clash{plural} this week.",
                "command": {"type": "open_page", "page": "calendar"},
                "handled": True,
            }

    # Calendar reads should never spend a Gemini token.
    if (
        ('calendar' in lower or 'schedule' in lower)
        and re.search(r'\b(what|show|list|have|on|free|busy|today|tomorrow|week)\b', lower)
        and not re.search(r'\b(add|create|schedule\s+an?\s+event|book|move|change|delete|remove)\b', lower)
    ):
        if 'week' in lower:
            start, _ = _day_window_from_text('today')
            start = start - timedelta(days=start.weekday())
            end = start + timedelta(days=7) - timedelta(seconds=1)
        else:
            start, end = _day_window_from_text(lower)
        events = calendar_list_events(start=start.isoformat(), end=end.isoformat(), limit=100)
        items = [_event_brief_item(e) for e in events[:8]]
        conflicts = sum(1 for e in events if e.get('conflict'))

        deadline_items = []
        task_source = (context or {}).get('mail', {}).get('needs_attention') or (context or {}).get('needs_attention') or []
        for task in task_source:
            deadline = str(task.get('deadline') or '').lower()
            if not deadline:
                continue
            relevant = True
            if 'tomorrow' in lower:
                relevant = 'tomorrow' in deadline
            elif 'today' in lower or 'tonight' in lower:
                relevant = ('today' in deadline or 'tonight' in deadline or 'within' in deadline)
            if relevant:
                deadline_items.append({
                    "title": task.get('subject') or task.get('title') or 'Email deadline',
                    "meta": _short_task_description(task),
                    "conflict": False,
                })

        items = (deadline_items + items)[:8]
        total = len(events) + len(deadline_items)
        if total == 0:
            voice = "Your calendar and deadlines are clear."
            reply = "No calendar events or email deadlines in that window."
        elif deadline_items and not events:
            voice = f"No meetings, but you have {len(deadline_items)} deadline{'s' if len(deadline_items) != 1 else ''}."
            reply = f"{len(deadline_items)} email deadline{'s' if len(deadline_items) != 1 else ''} need attention."
        else:
            first = events[0]
            voice = f"You have {len(events)} calendar event{'s' if len(events) != 1 else ''}"
            if deadline_items:
                voice += f" and {len(deadline_items)} deadline{'s' if len(deadline_items) != 1 else ''}"
            voice += f". First: {first.get('title', 'event')}."
            if conflicts:
                voice += f" {conflicts} clash{'es' if conflicts != 1 else ''}."
            reply = f"{total} scheduled item{'s' if total != 1 else ''} found."
        return {
            "reply": reply,
            "voice": voice,
            "brief": {"title": "Calendar", "items": items},
            "command": {"type": "open_page", "page": "calendar"},
            "handled": True,
        }

    # Deterministic creation for common voice commands: no Gemini call.
    if re.search(r'\b(add|create|schedule|book)\b', lower) and ('event' in lower or 'calendar' in lower or _parse_clock(lower)):
        payload = _natural_event_payload(message)
        if payload:
            event = calendar_create_event(payload)
            item = _event_brief_item(event)
            return {
                "reply": f"Added {event.get('title')} to Google Calendar.",
                "voice": f"Done. {event.get('title')} is on your calendar.",
                "brief": {"title": "Added to calendar", "items": [item]},
                "command": {"type": "calendar_refresh"},
                "handled": True,
            }

    # Selected-event mutations.
    if selected_event_id and re.search(r'\b(delete|remove|cancel)\b', lower):
        return {
            "reply": "Please review and confirm the selected event in the Kyle action window before deleting it.",
            "voice": "I need your confirmation before deleting that event.",
            "handled": True,
        }

    if selected_event_id and re.search(r'\b(move|change|reschedule)\b', lower):
        clock = _parse_clock(lower)
        if clock:
            start_day, _ = _day_window_from_text(lower)
            hour, minute = clock
            new_start = start_day.replace(hour=hour, minute=minute)
            # Keep the default mutation small; the UI can edit exact duration.
            event = calendar_update_event(selected_event_id, {
                "start": new_start.isoformat(),
                "end": (new_start + timedelta(hours=1)).isoformat(),
            })
            return {
                "reply": f"Moved {event.get('title')} on Google Calendar.",
                "voice": "Done. I moved it.",
                "brief": {"title": "Calendar updated", "items": [_event_brief_item(event)]},
                "command": {"type": "calendar_refresh"},
                "handled": True,
            }

    # "What should I work on?" is primarily local context, not an LLM problem.
    if re.search(r'\b(what should i (work on|do)|what do i (work on|do)|what(?:\'s| is) next|priority)\b', lower):
        attention = list((context or {}).get('needs_attention') or [])
        start, end = _day_window_from_text('today')
        try:
            events = calendar_list_events(start=start.isoformat(), end=end.isoformat(), limit=50)
        except Exception:
            events = []

        items = []
        if attention:
            top = attention[0]
            task = _short_task_description(top)
            items.append({
                "title": top.get('subject') or 'Do this first',
                "meta": task,
                "conflict": False,
            })
            voice = f"Do this first: {task}."
        elif events:
            first = events[0]
            items.append(_event_brief_item(first))
            voice = f"Your next commitment is {first.get('title', 'an event')}."
        else:
            voice = "Nothing urgent is blocking you. Pick one focused task."

        for event in events[:3]:
            items.append(_event_brief_item(event))

        return {
            "reply": voice,
            "voice": voice[:220],
            "brief": {"title": "What to focus on", "items": items[:5]},
            "handled": True,
        }

    return None


def _compact_context(context):
    context = context or {}
    tasks = []
    task_items = (
        (context.get('mail') or {}).get('needs_attention')
        or context.get('needs_attention')
        or []
    )
    for item in task_items[:3]:
        tasks.append({
            "task": _short_task_description(item),
            "deadline": item.get('deadline'),
        })

    calendar_events = []
    cal_items = (
        (context.get('calendar') or {}).get('events')
        or context.get('calendarEvents')
        or []
    )
    for event in cal_items[:5]:
        calendar_events.append({
            "title": event.get('title'),
            "start": event.get('start'),
            "conflict": event.get('conflict'),
        })

    work_items = []
    jobs = (
        (context.get('work') or {}).get('all')
        or context.get('workJobs')
    )
    if jobs is None:
        user_id = context.get('user_id') or 'default'
        try:
            jobs = work_agent_service.list_jobs(user_id)
        except Exception:
            jobs = []

    for job in (jobs or [])[:4]:
        work_items.append({
            "id": job.get('id'),
            "title": job.get('title'),
            "status": job.get('status'),
            "sender": (job.get('source') or {}).get('sender'),
            "subject": (job.get('source') or {}).get('subject'),
            "artifacts": [a.get('name') for a in (job.get('artifacts') or [])]
        })

    work_section = context.get('work') or {}
    active_count = len(work_section.get('active') or [j for j in (jobs or []) if j.get('status') in {'queued', 'reading_context', 'planning', 'researching', 'generating', 'drafting_reply', 'creating_files', 'verifying', 'preparing', 'working'}])
    waiting_count = len(work_section.get('waiting_approval') or [j for j in (jobs or []) if j.get('status') in {'waiting_approval', 'auto_send_countdown'}])
    conflict_count = (context.get('calendar') or {}).get('conflict_count', sum(1 for e in cal_items if e.get('conflict')))

    return {
        "tasks": tasks,
        "calendar": calendar_events,
        "work_items": work_items,
        "active_work_count": active_count,
        "waiting_approval_count": waiting_count,
        "calendar_conflict_count": conflict_count,
    }


def _compact_voice(text, max_chars=190):
    clean = re.sub(r'\s+', ' ', str(text or '')).strip()
    if not clean:
        return ''
    sentences = re.split(r'(?<=[.!?])\s+', clean)
    spoken = sentences[0]
    if len(spoken) < 70 and len(sentences) > 1:
        spoken += ' ' + sentences[1]
    if len(spoken) > max_chars:
        spoken = spoken[:max_chars].rsplit(' ', 1)[0].rstrip(' ,;:') + '.'
    return spoken


def _agent_text(value, limit=240):
    return re.sub(r'\s+', ' ', str(value or '')).strip()[:limit]


def _agent_reference(value):
    value = value or {}
    ref_type = _agent_text(value.get('type'), 48)
    ref_id = _agent_text(value.get('id'), 180)
    if not ref_type or not ref_id:
        return None
    metadata = {}
    for key, item in list((value.get('metadata') or {}).items())[:8]:
        if item is not None and item != '':
            metadata[_agent_text(key, 48)] = _agent_text(item)
    return {
        'type': ref_type,
        'id': ref_id,
        'label': _agent_text(value.get('label') or ref_id),
        'page': _agent_text(value.get('page'), 48),
        'metadata': metadata,
    }


def _agent_ui_context(value):
    value = value or {}
    refs = value.get('references') or {}
    return {
        'page': _agent_text(value.get('page') or 'overview', 48),
        'selected': _agent_reference(value.get('selected')),
        'open': _agent_reference(value.get('open')),
        'hovered': _agent_reference(value.get('hovered')),
        'focused': _agent_reference(value.get('focused')),
        'lastClicked': _agent_reference(value.get('lastClicked')),
        'selectedText': _agent_text(value.get('selectedText'), 280),
        'selectedTextSource': _agent_reference(value.get('selectedTextSource')),
        'references': {
            'lastMentioned': _agent_reference(refs.get('lastMentioned')),
            'lastOpened': _agent_reference(refs.get('lastOpened')),
            'lastCreated': _agent_reference(refs.get('lastCreated')),
            'lastModified': _agent_reference(refs.get('lastModified')),
            'lastManipulated': _agent_reference(refs.get('lastManipulated')),
        },
        'visibleObjects': [
            item for item in (_agent_reference(ref) for ref in (value.get('visibleObjects') or [])[:12]) if item
        ],
    }


def _agent_known_references(ui_context, resolved):
    candidates = [
        ui_context.get('selected'), ui_context.get('open'), ui_context.get('hovered'),
        ui_context.get('focused'), ui_context.get('lastClicked'),
        ui_context.get('selectedTextSource'),
        *(ui_context.get('references') or {}).values(),
        *(ui_context.get('visibleObjects') or []),
        *resolved,
    ]
    return {f"{ref['type']}:{ref['id']}": ref for ref in candidates if ref}


def _sanitize_agent_action(action, known):
    action = action or {}
    tool = _agent_text(action.get('tool'), 64)
    args = action.get('args') or {}
    pages = {'overview', 'inbox', 'work', 'calendar', 'automations', 'status', 'integrations', 'settings'}
    filters = {'all', 'important', 'action', 'unread'}
    if tool == 'navigation.open' and args.get('page') in pages:
        return {'tool': tool, 'args': {'page': args['page']}}
    if tool == 'inbox.set_filter' and args.get('filter') in filters:
        return {'tool': tool, 'args': {'filter': args['filter']}}
    if tool == 'ui.toast':
        message = _agent_text(args.get('message'), 180)
        return {'tool': tool, 'args': {'message': message}} if message else None
    if tool == 'calendar.refresh':
        return {'tool': tool, 'args': {}}
    if tool == 'calendar.delete_prepare':
        requested = args.get('references') or [args.get('reference') or args]
        references = []
        for item in requested[:20]:
            reference = _agent_reference(item)
            if not reference or reference.get('type') != 'calendar-event':
                continue
            exact = known.get(f"calendar-event:{reference['id']}")
            if exact and exact not in references:
                references.append(exact)
        return {'tool': tool, 'args': {'references': references}} if references else None
    if tool == 'calendar.preview_create':
        payload = args.get('payload') or {}
        title = _agent_text(payload.get('title'), 160)
        start = _agent_text(payload.get('start'), 80)
        end = _agent_text(payload.get('end'), 80)
        if not title or not start or not end:
            return None
        return {'tool': tool, 'args': {'payload': {
            'title': title,
            'start': start,
            'end': end,
            'description': _agent_text(payload.get('description'), 600),
        }}}

    if tool == 'mail.compose':
        return {
            'tool': tool,
            'args': {
                'recipient': _agent_text(args.get('recipient'), 160),
                'to': _agent_text(args.get('to'), 160),
                'subject': _agent_text(args.get('subject') or 'No Subject', 200),
                'body': _agent_text(args.get('body'), 5000),
            }
        }
    if tool == 'mail.reply':
        return {
            'tool': tool,
            'args': {
                'recipient': _agent_text(args.get('recipient'), 160),
                'to': _agent_text(args.get('to'), 160),
                'subject': _agent_text(args.get('subject') or 'Re: Update', 200),
                'body': _agent_text(args.get('body'), 5000),
                'thread_id': _agent_text(args.get('thread_id'), 120),
                'in_reply_to': _agent_text(args.get('in_reply_to'), 120),
            }
        }
    if tool == 'mail.update_draft':
        return {
            'tool': tool,
            'args': {
                'subject': _agent_text(args.get('subject'), 200) if args.get('subject') else None,
                'body': _agent_text(args.get('body'), 5000),
            }
        }
    if tool == 'mail.send_draft':
        return {'tool': tool, 'args': {'explicit_send': bool(args.get('explicit_send'))}}
    if tool == 'mail.close_composer':
        return {'tool': tool, 'args': {}}

    expected = {
        'inbox.open_email': 'email',
        'calendar.open_event': 'calendar-event',
        'calendar.inspect_event': 'calendar-event',
        'calendar.preview_move': 'calendar-event',
        'work.focus': 'work-item',
        'automation.run_now': 'automation',
        'automation.enable': 'automation',
        'automation.disable': 'automation',
        'ui.highlight': None,
        'ui.scroll_to': None,
        'ui.annotate': None,
    }
    if tool not in expected:
        return None
    reference = _agent_reference(args.get('reference') or args)
    if not reference or (expected[tool] and reference['type'] != expected[tool]):
        return None
    exact = known.get(f"{reference['type']}:{reference['id']}")
    if not exact:
        return None
    clean_args = {'reference': exact}
    if tool == 'calendar.preview_move':
        clean_args['start'] = _agent_text(args.get('start'), 80)
        clean_args['end'] = _agent_text(args.get('end'), 80)
        if not clean_args['start']:
            return None
    if tool == 'ui.annotate':
        clean_args['text'] = _agent_text(args.get('text'), 90)
    return {'tool': tool, 'args': clean_args}


def _infer_agent_actions(message, resolved, context=None):
    lower = message.lower()
    reference = resolved[0] if resolved else None
    actions = []
    context = context or {}
    pages = [
        ('calendar', r'\b(open|go to|show)\s+(my\s+)?calendar\b'),
        ('inbox', r'\b(open|go to|show)\s+(my\s+)?inbox\b'),
        ('work', r'\b(?:(open|go to|show)\s+(?:my\s+)?work|what did you prepare|what have you prepared|prepared work|review work)\b'),
        ('overview', r'\b(open|go to|show)\s+(the\s+)?overview\b'),
        ('status', r'\b(open|go to|show)\s+(the\s+)?status\b'),
    ]
    for page, pattern in pages:
        if re.search(pattern, lower):
            actions.append({'tool': 'navigation.open', 'args': {'page': page}})
            break

    filter_name = 'unread' if re.search(r'\bunread\b', lower) else (
        'important' if re.search(r'\bimportant\b', lower) else (
            'action' if re.search(r'\b(requires action|action items?)\b', lower) else None
        )
    )
    if filter_name and re.search(r'\b(show|filter|open|find)\b', lower):
        if not any(action.get('args', {}).get('page') == 'inbox' for action in actions):
            actions.append({'tool': 'navigation.open', 'args': {'page': 'inbox'}})
        actions.append({'tool': 'inbox.set_filter', 'args': {'filter': filter_name}})

    calendar_references = [item for item in resolved if item.get('type') == 'calendar-event']
    if calendar_references and re.search(r'\b(delete|remove|cancel)\b', lower):
        if not any(action.get('args', {}).get('page') == 'calendar' for action in actions):
            actions.append({'tool': 'navigation.open', 'args': {'page': 'calendar'}})
        actions.append({'tool': 'calendar.delete_prepare', 'args': {'references': calendar_references[:20]}})
        return actions

    put_on_calendar = bool(re.search(r'\b(put|add|save|schedule)\b.*\b(calendar|schedule)\b', lower))
    if reference and reference['type'] == 'email' and put_on_calendar:
        source = next((email for email in (context.get('emails') or []) if str(email.get('id') or email.get('gmail_id')) == reference['id']), None)
        attention = next((item for item in (context.get('needs_attention') or []) if str(item.get('source_message_id') or item.get('message_id') or item.get('email_id')) == reference['id']), None)
        target, has_time = _deadline_target(attention or {})
        if source and target:
            if has_time:
                start = target - timedelta(minutes=60)
                payload = {
                    'title': _agent_text(source.get('subject') or reference.get('label') or 'Email follow-up', 160),
                    'start': start.isoformat(),
                    'end': (start + timedelta(hours=1)).isoformat(),
                    'all_day': False,
                    'description': _agent_text(source.get('snippet') or '', 600),
                }
            else:
                payload = {
                    'title': _agent_text(source.get('subject') or reference.get('label') or 'Email follow-up', 160),
                    'start': target.date().isoformat(),
                    'end': (target.date() + timedelta(days=1)).isoformat(),
                    'all_day': True,
                    'description': _agent_text(source.get('snippet') or '', 600),
                }
            actions.extend([
                {'tool': 'navigation.open', 'args': {'page': 'calendar'}},
                {'tool': 'calendar.preview_create', 'args': {'payload': payload}},
            ])
    elif reference and reference['type'] == 'email' and re.search(r'\b(open|show|read|reply|respond|recent|latest|newest|find)\b', lower):
        if not any(action.get('args', {}).get('page') == 'inbox' for action in actions):
            actions.append({'tool': 'navigation.open', 'args': {'page': 'inbox'}})
        actions.append({'tool': 'inbox.open_email', 'args': {'reference': reference}})
        actions.append({'tool': 'ui.highlight', 'args': {'reference': reference}})
    elif reference and reference['type'] == 'calendar-event' and re.search(r'\b(open|show|edit|move|reschedule|change)\b', lower):
        moving = bool(re.search(r'\b(move|reschedule|change)\b', lower))
        clock = _parse_clock(lower)
        if not clock and moving:
            short_clock = re.search(r'\b(?:to|at)\s+([1-9]|1[0-2])(?::([0-5]\d))?\b', lower)
            if short_clock:
                hour = int(short_clock.group(1))
                if hour <= 7:
                    hour += 12
                clock = (hour, int(short_clock.group(2) or 0))
        current = next((event for event in (context.get('calendarEvents') or []) if str(event.get('id')) == reference['id']), None)
        if moving and clock and current and current.get('start'):
            old_start = date_parser.parse(str(current['start']))
            if old_start.tzinfo is None:
                old_start = old_start.replace(tzinfo=APP_TZ)
            old_end = date_parser.parse(str(current.get('end') or current['start']))
            if old_end.tzinfo is None:
                old_end = old_end.replace(tzinfo=old_start.tzinfo)
            duration = max(timedelta(minutes=15), old_end - old_start)
            if re.search(r'\b(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b', lower):
                target_day, _ = _day_window_from_text(lower)
            else:
                target_day = old_start.astimezone(APP_TZ)
            next_start = target_day.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
            actions.extend([
                {'tool': 'navigation.open', 'args': {'page': 'calendar'}},
                {'tool': 'calendar.preview_move', 'args': {
                    'reference': reference,
                    'start': next_start.isoformat(),
                    'end': (next_start + duration).isoformat(),
                }},
            ])
        else:
            actions.append({'tool': 'calendar.open_event', 'args': {'reference': reference}})
    elif reference and reference['type'] == 'work-item' and re.search(r'\b(open|show|do|work|focus)\b', lower):
        actions.append({'tool': 'work.focus', 'args': {'reference': reference}})
    elif reference and reference['type'] == 'automation':
        if re.search(r'\b(run|start|do)\b', lower):
            actions.append({'tool': 'automation.run_now', 'args': {'reference': reference}})
        elif re.search(r'\b(enable|turn on)\b', lower):
            actions.append({'tool': 'automation.enable', 'args': {'reference': reference}})
        elif re.search(r'\b(disable|turn off|pause)\b', lower):
            actions.append({'tool': 'automation.disable', 'args': {'reference': reference}})

    if reference and re.search(r'\b(where|which|highlight|point)\b', lower):
        actions.extend([
            {'tool': 'ui.scroll_to', 'args': {'reference': reference}},
            {'tool': 'ui.highlight', 'args': {'reference': reference}},
        ])
    return actions


def _email_timestamp(email):
    if not isinstance(email, dict):
        return 0
    internal = email.get('internal_date') or email.get('internalDate')
    if internal:
        try:
            val = int(internal)
            if val > 0:
                return val
        except (ValueError, TypeError):
            pass
    ts = email.get('timestamp') or email.get('date')
    if ts:
        try:
            val = int(ts)
            return val if val > 1000000000000 else val * 1000
        except (ValueError, TypeError):
            pass
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(str(ts))
            return int(dt.timestamp() * 1000)
        except Exception:
            pass
    return 0


def _find_contacts_by_name(query, emails):
    query_clean = str(query or '').strip().lower()
    if not query_clean:
        return []
    contacts = {}
    for email in emails or []:
        sender = email.get('sender') or ''
        name, addr = parseaddr(sender)
        if not addr:
            continue
        key = addr.lower()
        if key not in contacts:
            contacts[key] = {
                'name': name or addr.split('@')[0],
                'email': addr,
                'full': sender
            }
    matched = []
    query_tokens = [t for t in re.split(r'[\s._+-]+', query_clean) if t]
    for key, c in contacts.items():
        c_name = c['name'].lower()
        c_email = c['email'].lower()
        if query_clean in c_name or query_clean in c_email:
            matched.append(c)
        elif query_tokens and all(token in c_name or token in c_email for token in query_tokens):
            matched.append(c)
    return matched


def _explicit_email_address(text):
    match = re.search(r'(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63})(?![\w.-])', str(text or ''), re.I)
    return match.group(1).lower() if match else ''


def _email_display_name(address):
    local = str(address or '').split('@', 1)[0]
    words = [word for word in re.split(r'[._+-]+', local) if word]
    return ' '.join(word.capitalize() for word in words) or address


def _substantive_mail_body(body):
    content = re.sub(r'\s+', ' ', str(body or '')).strip()
    content = re.sub(r'^(?:hi|hello|dear)(?:\s+[^,.!?]{1,80})?[,!]\s*', '', content, count=1, flags=re.I)
    content = re.sub(r'\s+\b(?:best regards|best|regards|sincerely|thanks)[,\s]+[^,.!?]{1,80}\s*$', '', content, flags=re.I)
    return len(re.sub(r'\W+', '', content)) >= 24


def _mail_body_fallback(recipient_name, intent, user_name, reply=False):
    first_name = str(recipient_name or 'there').split()[0]
    request = re.sub(r'\s+', ' ', str(intent or '')).strip().rstrip('.!?')
    if reply:
        message = f"Thank you for your message. {request[:1].upper() + request[1:] if request else 'I have noted the request and will follow up shortly'}."
    else:
        message = request[:1].upper() + request[1:] if request else 'I wanted to share a quick update with you.'
        message = message.rstrip('.') + '.'
    return f"Hi {first_name},\n\n{message}\n\nBest regards,\n{user_name}"


def _normalize_generated_mail_body(body, recipient_name, user_name):
    """Keep model-written copy while enforcing a readable email shape."""
    text = str(body or '').replace('\r\n', '\n').replace('\r', '\n').strip()
    first_name = str(recipient_name or 'there').split()[0]
    greeting = f"Hi {first_name},"
    text = re.sub(
        r'^(?:hi|hello|dear)(?:\s+[^,\n]+)?[,!]\s*',
        f'{greeting}\n\n',
        text,
        count=1,
        flags=re.I,
    )
    if not re.match(r'^(?:hi|hello|dear)\b', text, re.I):
        text = f'{greeting}\n\n{text}'

    signoff = re.compile(
        rf'\s+(?:best regards|best|regards|sincerely|thanks),?\s+{re.escape(str(user_name))}\s*$',
        re.I,
    )
    if signoff.search(text):
        text = signoff.sub(f'\n\nBest,\n{user_name}', text)
    elif not re.search(r'(?im)^(?:best regards|best|regards|sincerely|thanks),?\s*$', text):
        text = f'{text.rstrip()}\n\nBest,\n{user_name}'
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _kyle_clock_reply(message):
    if not re.search(r'\b(?:what(?:\s+is|\'s)?\s+the\s+time|what\s+time\s+is\s+it|current\s+time|time\s+now)\b', str(message or ''), re.I):
        return None
    now = datetime.now(APP_TZ)
    hour = now.strftime('%I').lstrip('0') or '12'
    return f"It's {hour}:{now.strftime('%M %p')} on {now.strftime('%A, %B')} {now.day}."


def _handle_mail_intent(message, active_draft, selected_email, context_emails, user_profile):
    lower = message.lower().strip()
    user_name = user_profile.get('name') or 'Priyam'
    sorted_context = sorted(context_emails or [], key=_email_timestamp, reverse=True)

    # Case 1: Active draft editing or sending in contextual composer
    if active_draft and active_draft.get('body'):
        if re.search(r'\b(send\s*(?:it|draft|email|now)?|looks\s+good,?\s+send)\b', lower):
            return {
                'reply': 'Sending the email now.',
                'actions': [{'tool': 'mail.send_draft', 'args': {'explicit_send': True}}],
                'mode': 'mail_composer'
            }

        if re.search(r'\b(cancel|close|dismiss|nevermind|discard)\b', lower):
            return {
                'reply': 'I closed the draft.',
                'actions': [{'tool': 'mail.close_composer', 'args': {}}],
                'mode': 'composer_closed'
            }

        edit_words = {'short', 'shorter', 'brief', 'briefer', 'concise', 'long', 'longer', 'casual',
                      'informal', 'formal', 'professional', 'friendly', 'polite', 'tone', 'change',
                      'edit', 'rewrite', 'modify', 'add', 'remove', 'replace', 'fix', 'tonight', 'tomorrow'}
        if any(w in lower for w in edit_words) or (len(lower.split()) <= 15 and not re.search(r'\b(open|show|go to|calendar|inbox|work)\b', lower)):
            current_sub = active_draft.get('subject') or ''
            current_body = active_draft.get('body') or ''
            prompt = (
                f"You are Kyle, editing an email draft for {user_name}.\n"
                f"User instruction: {message}\n"
                f"Current Subject: {current_sub}\n"
                f"Current Body:\n{current_body}\n\n"
                f"Return ONLY the revised email body text. Keep the greeting and sign-off consistent."
            )
            revised_body = chat_with_kyle(prompt).strip()
            if not _substantive_mail_body(revised_body):
                revised_body = current_body
            return {
                'reply': "I've updated the draft for you.",
                'actions': [{
                    'tool': 'mail.update_draft',
                    'args': {'body': revised_body}
                }],
                'mode': 'mail_composer'
            }

    # Case 2: Reply intent
    # Matches: "reply to Rupayan saying I'll send it tonight", "reply saying ...", "reply to this"
    if re.search(r'^\s*(?:please\s+)?reply\b|\breply\s+to\b', lower):
        target_name = None
        explicit_address = _explicit_email_address(message)
        name_match = re.search(r'\breply\s+to\s+([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|[A-Za-z]+(?:\s+[A-Za-z]+)*?)(?:\s+(?:saying|that|to\s+say|\.|$))', lower)
        if not name_match:
            name_match = re.search(r'\breply\s+to\s+([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|[A-Za-z]+(?:\s+[A-Za-z]+)*)\b', lower)
        if name_match and name_match.group(1).lower() not in {'this', 'that', 'the', 'it', 'me'}:
            target_name = name_match.group(1).strip()

        target_contact = None
        thread_id = None
        in_reply_to = None
        thread_subject = 'Update'

        if explicit_address:
            contacts = _find_contacts_by_name(explicit_address, sorted_context)
            target_contact = contacts[0] if contacts else {
                'name': _email_display_name(explicit_address),
                'email': explicit_address,
                'full': explicit_address,
            }
        elif target_name:
            contacts = _find_contacts_by_name(target_name, sorted_context)
            if len(contacts) > 1:
                options_str = ", ".join([f"{c['name']} ({c['email']})" for c in contacts])
                return {
                    'reply': f"Which {target_name.capitalize()}? I found: {options_str}",
                    'actions': [],
                    'mode': 'contact_disambiguation',
                    'contacts': contacts
                }
            elif len(contacts) == 1:
                target_contact = contacts[0]
                matching_email = next((e for e in sorted_context if target_contact['email'] in (e.get('sender') or '').lower()), None)
                if matching_email:
                    thread_id = matching_email.get('threadId') or matching_email.get('thread_id') or matching_email.get('id')
                    in_reply_to = matching_email.get('rfc_message_id') or None
                    thread_subject = matching_email.get('subject') or thread_subject
            else:
                return {
                    'reply': f"What's {target_name.capitalize()}'s email address?",
                    'actions': [],
                    'mode': 'recipient_required',
                }
        elif selected_email:
            s_name, s_addr = parseaddr(selected_email.get('sender') or '')
            target_contact = {'name': s_name or 'Sender', 'email': s_addr, 'full': selected_email.get('sender') or ''}
            thread_id = selected_email.get('threadId') or selected_email.get('thread_id') or selected_email.get('id')
            in_reply_to = selected_email.get('rfc_message_id') or None
            thread_subject = selected_email.get('subject') or thread_subject

        if target_contact:
            saying_m = re.search(r'\b(?:saying|that|to\s+say)\s+(.*)$', message, re.I)
            user_saying = saying_m.group(1).strip() if saying_m else message

            clean_sub = f"Re: {thread_subject}" if not thread_subject.lower().startswith('re:') else thread_subject
            recipient_display = target_contact['name']
            first_name = recipient_display.split()[0] if recipient_display else 'there'

            prompt = (
                f"You are Kyle, an AI assistant drafting a polite email reply from {user_name}.\n"
                f"Recipient: {recipient_display} <{target_contact['email']}>\n"
                f"Subject: {clean_sub}\n"
                f"Instruction: {user_saying}\n\n"
                f"Write a complete, concise and natural reply. Turn the instruction into polished prose rather than repeating command wording. "
                f"Use 2-5 useful sentences, greeting ('Hi {first_name},'), and sign-off ('Regards,\n{user_name}').\n"
                f"Return ONLY the email body text."
            )
            body = chat_with_kyle(prompt).strip()
            if not _substantive_mail_body(body):
                body = _mail_body_fallback(recipient_display, user_saying, user_name, reply=True)
            body = _normalize_generated_mail_body(body, recipient_display, user_name)

            return {
                'reply': f"I've prepared a reply to {first_name}. It's ready here for you to review or edit.",
                'actions': [{
                    'tool': 'mail.reply',
                    'args': {
                        'recipient': target_contact.get('full') or recipient_display,
                        'to': target_contact['email'],
                        'subject': clean_sub,
                        'body': body,
                        'thread_id': thread_id,
                        'in_reply_to': in_reply_to
                    }
                }],
                'mode': 'mail_composer'
            }

    # Case 3: Compose new email intent
    # Matches: "Email Aarush and ask if he finished the report", "write an email to Aarush asking..."
    explicit_address = _explicit_email_address(message)
    compose_match = re.search(r'\b(?:email|write\s+(?:an?\s+)?email\s+to|compose\s+(?:an?\s+)?email\s+to|send\s+(?:an?\s+)?email\s+to)\s+([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|[A-Za-z]+(?:\s+[A-Za-z]+)*?)(?:\s+(?:and\s+ask|asking|about|saying|that|\.|$))', lower)
    if not compose_match:
        compose_match = re.search(r'\b(?:email|write\s+(?:an?\s+)?email\s+to|compose\s+(?:an?\s+)?email\s+to|send\s+(?:an?\s+)?email\s+to)\s+([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|[A-Za-z]+(?:\s+[A-Za-z]+)*)\b', lower)
    if compose_match:
        target_name = compose_match.group(1).strip()
        if target_name.lower() not in {'this', 'that', 'the', 'it', 'me'}:
            contacts = _find_contacts_by_name(explicit_address or target_name, sorted_context)
            if len(contacts) > 1:
                options_str = ", ".join([f"{c['name']} ({c['email']})" for c in contacts])
                return {
                    'reply': f"Which {target_name.capitalize()}? I found: {options_str}",
                    'actions': [],
                    'mode': 'contact_disambiguation',
                    'contacts': contacts
                }
            if contacts:
                target_contact = contacts[0]
            elif explicit_address:
                target_contact = {
                    'name': _email_display_name(explicit_address),
                    'email': explicit_address,
                    'full': explicit_address,
                }
            else:
                return {
                    'reply': f"What's {target_name.capitalize()}'s email address?",
                    'actions': [],
                    'mode': 'recipient_required',
                }

            first_name = target_contact['name'].split()[0]
            intent_m = re.search(r'\b(?:and\s+ask|asking|about|saying|that|to\s+say)\s+(.*)$', message, re.I)
            if intent_m:
                user_intent = intent_m.group(1).strip()
            elif explicit_address:
                user_intent = message.lower().split(explicit_address.lower(), 1)[1].strip(' ,:.-')
            else:
                user_intent = ''

            if not user_intent:
                return {
                    'reply': f"What would you like the email to {first_name} to say?",
                    'actions': [],
                    'mode': 'message_required',
                }

            prompt = (
                f"You are Kyle, an AI assistant composing a new email from {user_name}.\n"
                f"Recipient: {target_contact['name']} <{target_contact['email']}>\n"
                f"User instruction: {user_intent}\n\n"
                f"Turn the instruction into a finished, natural email rather than copying command wording. "
                f"Return JSON with 'subject' (specific, concise 3-6 words) and 'body' (greeting, 2-5 useful sentences, sign-off 'Best,\n{user_name}').\n"
                f"Format: {{\"subject\": \"...\", \"body\": \"...\"}}"
            )
            raw = chat_with_kyle(prompt).strip()
            subject = 'Update'
            body = f"Hi {first_name},\n\n{user_intent}\n\nBest,\n{user_name}"
            try:
                clean_json = re.sub(r'^```json\s*|\s*```$', '', raw, flags=re.MULTILINE).strip()
                parsed = json.loads(clean_json)
                subject = parsed.get('subject') or subject
                body = parsed.get('body') or body
            except Exception:
                pass
            if not _substantive_mail_body(body):
                body = _mail_body_fallback(target_contact['name'], user_intent, user_name)
            body = _normalize_generated_mail_body(body, target_contact['name'], user_name)

            explicit_send = bool(re.search(r'\bsend\s+(?:an?\s+)?(?:email|mail)\s+to\b', lower))
            actions = [{
                    'tool': 'mail.compose',
                    'args': {
                        'recipient': target_contact.get('full') or target_contact['name'],
                        'to': target_contact['email'],
                        'subject': subject,
                        'body': body
                    }
                }]
            if explicit_send:
                actions.append({'tool': 'mail.send_draft', 'args': {'explicit_send': True}})
            return {
                'reply': (f"I'm sending that email to {first_name}." if explicit_send else
                          f"I've drafted that email to {first_name}. It's ready here whenever you want to send it."),
                'actions': actions,
                'mode': 'mail_composer'
            }

    return None


@app.route('/api/kyle/agent', methods=['POST'])
def kyle_agent_endpoint():
    data = request.get_json(silent=True) or {}
    message = _agent_text(data.get('message'), 1200)
    if not message:
        return jsonify({'error': 'message is required'}), 400

    profile = get_user_profile() or {}
    user_id = profile.get('email') or profile.get('id') or 'default'
    system_ctx = system_context_service.build(user_id=user_id, force_reconcile=False)

    ui_context = _agent_ui_context(data.get('uiContext'))
    resolved = [
        item for item in (_agent_reference(ref) for ref in (data.get('resolvedReferences') or [])[:4]) if item
    ]
    selected_calendar_id = _agent_text(data.get('selectedCalendarEventId'), 160)
    if selected_calendar_id and not any(item.get('type') == 'calendar-event' and item.get('id') == selected_calendar_id for item in resolved):
        browser_events = ((data.get('context') or {}).get('calendarEvents') or []) if isinstance(data.get('context'), dict) else []
        selected_event = next((event for event in browser_events if str(event.get('id')) == selected_calendar_id), None)
        resolved.append({
            'type': 'calendar-event',
            'id': selected_calendar_id,
            'label': _agent_text((selected_event or {}).get('title') or 'Selected calendar event', 180),
        })
    known = _agent_known_references(ui_context, resolved)

    active_draft = data.get('activeDraft')
    selected_email = data.get('selectedEmail')

    # Merge browser context with authoritative system_ctx
    merged_context = dict(system_ctx)
    if isinstance(data.get('context'), dict):
        for k, v in data['context'].items():
            if k not in merged_context:
                merged_context[k] = v

    # Resolve recent email or sender query into exact reference if not yet resolved
    if not any(item.get('type') == 'email' for item in resolved):
        lower_msg = message.lower()
        if re.search(r'\b(most\s+recent|latest|newest)\s+(?:e?mail|message)\b', lower_msg) or re.search(r'\bshow\s+(?:me\s+)?(?:the\s+)?recent\s+(?:e?mail|message)\b', lower_msg):
            ctx_emails = merged_context.get('emails') or []
            if ctx_emails:
                top_e = ctx_emails[0]
                resolved.append({
                    'type': 'email',
                    'id': str(top_e.get('id') or top_e.get('gmail_id')),
                    'label': _agent_text(top_e.get('subject') or 'Recent email', 160)
                })
        else:
            sender_m = re.search(r'\b(?:show\s+|find\s+)?(?:mails?|emails?|messages?)\s+from\s+([A-Za-z]+)\b', lower_msg)
            if sender_m and sender_m.group(1).lower() not in {'this', 'that', 'me', 'it'}:
                s_name = sender_m.group(1).lower()
                ctx_emails = merged_context.get('emails') or []
                matching = [e for e in ctx_emails if s_name in (e.get('sender') or '').lower()]
                if matching:
                    top_m = matching[0]
                    resolved.append({
                        'type': 'email',
                        'id': str(top_m.get('id') or top_m.get('gmail_id')),
                        'label': _agent_text(top_m.get('subject') or 'Email', 160)
                    })
    # Update known references with newly resolved references
    known = _agent_known_references(ui_context, resolved)
    for email in (merged_context.get('emails') or [])[:50]:
        email_id = str(email.get('id') or email.get('gmail_id') or '')
        if email_id:
            ref = {'type': 'email', 'id': email_id, 'label': _agent_text(email.get('subject') or 'Email'), 'page': 'inbox', 'metadata': {}}
            known[f'email:{email_id}'] = ref
    for job in ((merged_context.get('work') or {}).get('all') or [])[:50]:
        job_id = str(job.get('id') or '')
        if job_id:
            ref = {'type': 'work-item', 'id': job_id, 'label': _agent_text(job.get('clean_title') or job.get('title') or 'Work item'), 'page': 'work', 'metadata': {}}
            known[f'work-item:{job_id}'] = ref
    for event in ((merged_context.get('calendar') or {}).get('events') or [])[:100]:
        event_id = str(event.get('id') or '')
        if event_id:
            ref = {'type': 'calendar-event', 'id': event_id, 'label': _agent_text(event.get('title') or 'Calendar event'), 'page': 'calendar', 'metadata': {}}
            known[f'calendar-event:{event_id}'] = ref

    # The semantic planner is the primary brain. Deterministic handlers below are
    # retained only as an offline fallback and for hard validation.
    planner_context = dict(merged_context)
    planner_context.update({
        'ui': ui_context,
        'resolved': resolved,
        'active_draft': active_draft,
        'conversation': (data.get('conversation') or [])[-12:],
    })

    clock_reply = _kyle_clock_reply(message)
    if clock_reply:
        return jsonify({
            'reply': clock_reply,
            'text': clock_reply,
            'voice': clock_reply,
            'actions': [],
            'mode': 'deterministic-context',
            'context_version': system_ctx.get('context_version', 1),
        })

    # Mail commands have a deterministic recipient/action path. Resolve them
    # before any semantic planning so explicit sends never depend on LM Studio.
    if active_draft or re.search(r'\b(email|mail|reply|draft|compose|send)\b', message, re.I):
        mail_intent_res = _handle_mail_intent(
            message=message,
            active_draft=active_draft,
            selected_email=selected_email,
            context_emails=merged_context.get('emails') or [],
            user_profile=profile,
        )
        if mail_intent_res:
            direct_actions = []
            for candidate in mail_intent_res.get('actions') or []:
                sanitized = _sanitize_agent_action(candidate, known)
                if sanitized:
                    direct_actions.append(sanitized)
            reply = mail_intent_res.get('reply') or ''
            return jsonify({
                'reply': reply, 'text': reply, 'voice': _compact_voice(reply),
                'actions': direct_actions, 'mode': mail_intent_res.get('mode', 'mail_composer'),
                'contacts': mail_intent_res.get('contacts'),
                'context_version': system_ctx.get('context_version', 1),
            })
    try:
        planned = plan_kyle_turn(
            message,
            planner_context,
            selected_email=selected_email,
            fetch_message=get_gmail_message,
        )
        planned_actions = []
        for candidate in planned.get('actions') or []:
            sanitized = _sanitize_agent_action(candidate, known)
            if sanitized and sanitized not in planned_actions:
                planned_actions.append(sanitized)
        reply = planned.get('reply') or 'I am ready.'
        return jsonify({
            **planned,
            'reply': reply,
            'text': reply,
            'voice': planned.get('voice') or _compact_voice(reply),
            'actions': planned_actions,
            'mode': 'semantic-agent',
            'context_version': system_ctx.get('context_version', 1),
        })
    except Exception as planner_exc:
        app.logger.info('Kyle semantic planner unavailable; using guarded fallback: %s', planner_exc)

    # Contextual mail fallback. It is not the canonical intent engine.
    mail_intent_res = _handle_mail_intent(
        message=message,
        active_draft=active_draft,
        selected_email=selected_email,
        context_emails=merged_context.get('emails') or [],
        user_profile=profile
    )
    if mail_intent_res:
        reply = mail_intent_res.get('reply') or ''
        return jsonify({
            'reply': reply,
            'text': reply,
            'voice': _compact_voice(reply),
            'actions': mail_intent_res.get('actions') or [],
            'mode': mail_intent_res.get('mode', 'mail_composer'),
            'contacts': mail_intent_res.get('contacts'),
            'context_version': system_ctx.get('context_version', 1),
        })

    if re.search(r'\b(this|that|it|this one|that one|these|those)\b', message, re.I) and not resolved:
        return jsonify({
            'reply': 'Which item do you mean? Click it, then ask me again.',
            'voice': 'Which item do you mean? Click it, then ask me again.',
            'actions': [],
            'mode': 'clarification',
        })

    actions = []

    for candidate in _infer_agent_actions(message, resolved, context=merged_context):
        sanitized = _sanitize_agent_action(candidate, known)
        if sanitized and sanitized not in actions:
            actions.append(sanitized)

    compact = {
        'mail': _compact_context(merged_context),
        'ui': ui_context,
        'resolved': resolved,
        'plannedActions': actions,
        'context_version': system_ctx.get('context_version', 1),
    }
    reply = 'Working on that.' if actions else generate_kyle_agent_reply(message, compact)
    if not reply:
        if resolved:
            reply = f"I know you mean {resolved[0]['label']}. " + (
                'I opened the right place for it.' if actions else 'What would you like me to do with it?'
            )
        elif actions:
            reply = 'Done. I opened the right place.'
        else:
            reply = 'I am with you. What should we handle first?'

    return jsonify({
        'reply': reply,
        'text': reply,
        'voice': _compact_voice(reply),
        'actions': actions[:24],
        'mode': 'deterministic-context',
        'context_version': system_ctx.get('context_version', 1),
    })


@app.route('/api/voice/speak', methods=['POST'])
def elevenlabs_speak():
    if not get_user_profile():
        return jsonify({'error': 'Not authenticated'}), 401
    text = str((request.get_json(silent=True) or {}).get('text') or '').strip()
    try:
        content, content_type = elevenlabs_synthesize(text)
        return Response(content, status=200, content_type=content_type, headers={'Cache-Control': 'no-store'})
    except ElevenLabsError as exc:
        return jsonify({'error': str(exc), 'fallback': 'browser'}), 503


@app.route('/api/voice/agent/signed-url')
def elevenlabs_agent_signed_url():
    if not get_user_profile():
        return jsonify({'error': 'Not authenticated'}), 401
    try:
        return jsonify({'signed_url': signed_agent_url()})
    except ElevenLabsError as exc:
        return jsonify({'error': str(exc)}), 503


@app.route('/api/kyle/chat', methods=['POST'])
def kyle_chat_endpoint():
    data = request.get_json(silent=True) or {}
    msg = str(data.get('message') or '').strip()

    profile = get_user_profile() or {}
    user_id = profile.get('email') or profile.get('id') or 'default'
    system_ctx = system_context_service.build(user_id=user_id, force_reconcile=False)

    merged_context = dict(system_ctx)
    if isinstance(data.get('context'), dict):
        for k, v in data['context'].items():
            if k not in merged_context:
                merged_context[k] = v

    selected_event_id = data.get('selectedCalendarEventId')

    try:
        fast = _kyle_fast_path(msg, merged_context, selected_event_id=selected_event_id)
        if fast:
            fast['context_version'] = system_ctx.get('context_version', 1)
            return jsonify(fast)
    except Exception as exc:
        app.logger.warning('Kyle deterministic tool failed: %s', exc)

    # Fallback AI: give it only the tiny context that changes the answer.
    # The full inbox is already visible in the UI, so there is no reason to
    # send/voice it again.
    compact = _compact_context(merged_context)
    ai_prompt = (
        "You are Kyle inside Agent Harness. Answer naturally and directly. "
        "The UI already shows details, so the spoken answer should usually be 1-2 short sentences, <=35 words. "
        "Never claim the calendar is clear when the supplied context contains a deadline or event. "
        "Never claim drafts are waiting if waiting_approval_count is 0. "
        f"Context: {json.dumps(compact, ensure_ascii=False)}\n"
        f"User: {msg}"
    )
    text = chat_with_kyle(ai_prompt)
    return jsonify({
        "reply": text,
        "text": text,
        "voice": _compact_voice(text),
        "handled": False,
        "context_version": system_ctx.get('context_version', 1),
    })

if __name__ == '__main__':
    print('[Mailmate] Running with zero central mailbox retention (transient browser RAM only).')
    port = int(os.getenv('PORT', 5000))
    print(f"Flask server running on http://localhost:{port}")
    print(f"[Static] project root: {BASE_DIR}")
    print(f"[Static] styles.css: {(BASE_DIR / 'styles.css').is_file()}")
    app.run(port=port, host='0.0.0.0', debug=False, threaded=True)


