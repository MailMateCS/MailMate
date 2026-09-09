import threading
import time
import os
import json
import base64
import html
import re
from pathlib import Path
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from dotenv import load_dotenv

from services.secure_storage import read_encrypted_json, write_encrypted_json

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / 'api.env')

CREDENTIALS_FILE = BASE_DIR / 'data' / 'google_credentials.json'
TOKEN_URI = 'https://oauth2.googleapis.com/token'


def _credential_data(default=None):
    return read_encrypted_json(CREDENTIALS_FILE, 'google_credentials', default=default)

class GmailInsufficientPermissionError(RuntimeError):
    """Raised when Gmail API returns 403 due to missing gmail.modify or write scopes."""
    def __init__(self, message="Google account needs reconnection to grant Gmail draft & send permissions (gmail.modify)."):
        super().__init__(message)
        self.status_code = 403
        self.code = "insufficient_scopes"
        self.reconnect_url = "/auth/google"

# gmail.modify includes Gmail read access and is required for moving messages to Trash and creating/sending drafts.
# Existing users with an older gmail.readonly token must reconnect Google once.
SCOPES = [
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/calendar',
]



def _parse_expiry(value):
    if not value:
        return None
    try:
        expiry = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if expiry.tzinfo is not None:
            expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
        return expiry
    except Exception:
        return None


def get_google_config():
    client_id = os.getenv('GOOGLE_CLIENT_ID')
    client_secret = os.getenv('GOOGLE_CLIENT_SECRET')
    if not client_id or not client_secret:
        try:
            d = _credential_data({}) or {}
            client_id = client_id or d.get('client_id')
            client_secret = client_secret or d.get('client_secret')
        except Exception:
            pass
    return {
        'web': {
            'client_id': client_id,
            'client_secret': client_secret,
            'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': TOKEN_URI,
            'redirect_uris': [os.getenv('GOOGLE_REDIRECT_URI', 'http://localhost:5000/auth/google/callback')],
        }
    }


def get_gmail_permissions():
    """Returns whether the active credentials have write/draft/send scopes."""
    creds = get_credentials()
    if not creds:
        return {'connected': False, 'can_write': False, 'scopes': []}
    scopes = set(creds.scopes or [])
    can_write = bool(scopes.intersection({
        'https://mail.google.com/',
        'https://www.googleapis.com/auth/gmail.modify',
        'https://www.googleapis.com/auth/gmail.compose',
        'https://www.googleapis.com/auth/gmail.send'
    }))
    return {
        'connected': True,
        'can_write': can_write,
        'scopes': sorted(list(scopes))
    }



def get_credentials():
    try:
        data = _credential_data(None)
        if not data:
            return None
        creds = Credentials(
            token=data.get('token'),
            refresh_token=data.get('refresh_token'),
            id_token=data.get('id_token'),
            token_uri=data.get('token_uri', TOKEN_URI),
            client_id=data.get('client_id', os.getenv('GOOGLE_CLIENT_ID')),
            client_secret=data.get('client_secret', os.getenv('GOOGLE_CLIENT_SECRET')),
            scopes=data.get('scopes') or SCOPES,
            expiry=_parse_expiry(data.get('expiry')),
        )
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            save_credentials(creds)
        return creds
    except Exception as exc:
        print('Error loading credentials:', exc)
        return None


def save_credentials(creds):
    previous = _credential_data({}) or {}
    data = json.loads(creds.to_json())
    if not data.get('refresh_token') and previous.get('refresh_token'):
        data['refresh_token'] = previous['refresh_token']
    data.setdefault('client_id', os.getenv('GOOGLE_CLIENT_ID', ''))
    data.setdefault('client_secret', os.getenv('GOOGLE_CLIENT_SECRET', ''))
    data.setdefault('token_uri', TOKEN_URI)
    write_encrypted_json(CREDENTIALS_FILE, data, 'google_credentials')


def get_auth_url():
    flow = Flow.from_client_config(get_google_config(), scopes=SCOPES)
    flow.redirect_uri = get_google_config()['web']['redirect_uris'][0]
    auth_url, state = flow.authorization_url(
        access_type='offline',
        prompt='consent',
        include_granted_scopes='true',
    )
    return auth_url, state, getattr(flow, 'code_verifier', None)


def handle_callback(url, state=None, code_verifier=None):
    flow = Flow.from_client_config(get_google_config(), scopes=SCOPES, state=state)
    flow.redirect_uri = get_google_config()['web']['redirect_uris'][0]
    if code_verifier:
        flow.code_verifier = code_verifier
    flow.fetch_token(authorization_response=url)
    creds = flow.credentials
    save_credentials(creds)
    return creds


_THREAD_LOCAL = threading.local()


def _reset_gmail_service():
    """Reset the thread-local Gmail client if a network, timeout, or SSL error occurs."""
    _THREAD_LOCAL.gmail_service = None
    _THREAD_LOCAL.gmail_token = None


def _gmail_service():
    creds = get_credentials()
    if not creds:
        _reset_gmail_service()
        return None

    current_token = getattr(creds, 'token', None)
    cached_service = getattr(_THREAD_LOCAL, 'gmail_service', None)
    cached_token = getattr(_THREAD_LOCAL, 'gmail_token', None)

    if cached_service and current_token and current_token == cached_token and getattr(creds, 'valid', True):
        return cached_service

    service = build('gmail', 'v1', credentials=creds, cache_discovery=False)
    _THREAD_LOCAL.gmail_service = service
    _THREAD_LOCAL.gmail_token = current_token
    return service


def get_user_profile():
    creds = get_credentials()
    if not creds:
        return None
    service = build('oauth2', 'v2', credentials=creds, cache_discovery=False)
    user_info = service.userinfo().get().execute()
    return {
        'id': user_info.get('id'),
        'name': user_info.get('name'),
        'email': user_info.get('email'),
        'picture': user_info.get('picture'),
    }


def get_calendar_events():
    creds = get_credentials()
    if not creds:
        return []
    service = build('calendar', 'v3', credentials=creds, cache_discovery=False)
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    events_result = service.events().list(
        calendarId='primary',
        timeMin=now,
        maxResults=30,
        singleEvents=True,
        orderBy='startTime',
    ).execute()
    parsed = []
    for event in events_result.get('items', []):
        parsed.append({
            'id': event['id'],
            'title': event.get('summary', 'No Title'),
            'description': event.get('description', ''),
            'start': event['start'].get('dateTime', event['start'].get('date')),
            'end': event['end'].get('dateTime', event['end'].get('date')),
            'html_link': event.get('htmlLink'),
        })
    return parsed


def _gmail_query():
    return (os.getenv('GMAIL_QUERY') or 'newer_than:30d').strip()


def get_gmail_history_id():
    """Return Gmail's cheap mailbox change cursor without fetching message data."""
    service = _gmail_service()
    if not service:
        return None
    profile = service.users().getProfile(userId='me').execute()
    return str(profile.get('historyId') or '') or None


def get_gmail_message_ids(limit=250):
    """Cheap current-mail inventory used to prune stale cached/deleted messages."""
    service = _gmail_service()
    if not service:
        return set()

    limit = max(1, min(int(limit or 250), 500))
    query = _gmail_query()
    ids = set()
    page_token = None
    while len(ids) < limit:
        kwargs = {
            'userId': 'me',
            'maxResults': min(100, limit - len(ids)),
            'includeSpamTrash': False,
        }
        if query:
            kwargs['q'] = query
        if page_token:
            kwargs['pageToken'] = page_token
        response = service.users().messages().list(**kwargs).execute()
        ids.update(str(item.get('id')) for item in (response.get('messages') or []) if item.get('id'))
        page_token = response.get('nextPageToken')
        if not page_token:
            break
    return ids


def trash_gmail_message(message_id):
    """Move one Gmail message to Trash. Requires gmail.modify."""
    if not message_id:
        raise ValueError('message_id is required')
    service = _gmail_service()
    if not service:
        raise RuntimeError('Google credentials are not available')
    result = service.users().messages().trash(userId='me', id=str(message_id)).execute()
    return {
        'trashed': True,
        'id': result.get('id') or str(message_id),
        'thread_id': result.get('threadId'),
        'labels': result.get('labelIds') or [],
    }


class _ReadableHtmlParser(HTMLParser):
    """Extract readable text while ignoring executable and styling content."""

    _BLOCK_TAGS = {'br', 'div', 'p', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'blockquote'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._ignored_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {'script', 'style', 'noscript'}:
            self._ignored_depth += 1
        elif not self._ignored_depth and tag in self._BLOCK_TAGS:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {'script', 'style', 'noscript'} and self._ignored_depth:
            self._ignored_depth -= 1
        elif not self._ignored_depth and tag in self._BLOCK_TAGS:
            self.parts.append('\n')

    def handle_comment(self, data):
        # Suppress all HTML comments, including MSO/Outlook conditional
        # comments like <!--[if !mso]-->, <!--[if false]-->, <!-->.
        pass

    def handle_data(self, data):
        if not self._ignored_depth:
            self.parts.append(data)


class _SafeEmailHtmlParser(HTMLParser):
    """Preserve useful email layout without scripts, handlers, or remote tracking."""

    _TAGS = {
        'a', 'b', 'blockquote', 'br', 'code', 'div', 'em', 'h1', 'h2', 'h3',
        'h4', 'h5', 'h6', 'i', 'li', 'ol', 'p', 'pre', 'span', 'strong',
        'table', 'tbody', 'td', 'tfoot', 'th', 'thead', 'tr', 'u', 'ul',
    }
    _VOID_TAGS = {'br'}
    _STYLE_PROPERTIES = {
        'background-color', 'border', 'border-collapse', 'color', 'display',
        'font-family', 'font-size', 'font-style', 'font-weight', 'height',
        'line-height', 'margin', 'margin-bottom', 'margin-left', 'margin-right',
        'margin-top', 'max-height', 'max-width', 'min-height', 'min-width',
        'opacity', 'overflow', 'overflow-x', 'overflow-y', 'padding',
        'padding-bottom', 'padding-left', 'padding-right', 'padding-top',
        'text-align', 'text-decoration', 'vertical-align', 'visibility',
        'white-space', 'width', 'word-break',
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._ignored_depth = 0

    def _safe_style(self, value):
        declarations = []
        for declaration in str(value or '').split(';'):
            if ':' not in declaration:
                continue
            name, raw_value = declaration.split(':', 1)
            name, raw_value = name.strip().lower(), raw_value.strip()
            if name not in self._STYLE_PROPERTIES:
                continue
            if re.search(r'(?i)(javascript|expression|behavior|url\s*\(|@import)', raw_value):
                continue
            declarations.append(f'{name}: {raw_value}')
        return '; '.join(declarations)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {'script', 'style', 'noscript', 'iframe', 'object', 'embed', 'form'}:
            self._ignored_depth += 1
            return
        if self._ignored_depth or tag not in self._TAGS:
            return
        safe_attrs = []
        for name, value in attrs:
            name, value = str(name).lower(), str(value or '')
            if name == 'href' and re.match(r'^(https?|mailto):', value, re.I):
                safe_attrs.extend([('href', value), ('target', '_blank'), ('rel', 'noopener noreferrer')])
            elif name in {'alt', 'title', 'width', 'height', 'colspan', 'rowspan', 'align', 'valign', 'border', 'cellpadding', 'cellspacing', 'bgcolor', 'role'}:
                safe_attrs.append((name, value))
            elif name == 'style':
                safe_style = self._safe_style(value)
                if safe_style:
                    safe_attrs.append(('style', safe_style))
        rendered = ''.join(f' {name}="{html.escape(value, quote=True)}"' for name, value in safe_attrs)
        self.parts.append(f'<{tag}{rendered}>')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {'script', 'style', 'noscript', 'iframe', 'object', 'embed', 'form'} and self._ignored_depth:
            self._ignored_depth -= 1
        elif not self._ignored_depth and tag in self._TAGS and tag not in self._VOID_TAGS:
            self.parts.append(f'</{tag}>')

    def handle_data(self, data):
        if not self._ignored_depth:
            self.parts.append(html.escape(data))

    def handle_comment(self, data):
        pass


def _repair_mojibake(value):
    """Repair common UTF-8-as-Windows-1252 artifacts without altering valid text."""
    text = str(value or '')
    replacements = {
        '\u00c3\u00a2\u00e2\u201a\u00ac\u00c2\u00a2': '\u2022',
        '\u00e2\u20ac\u00a2': '\u2022',
        '\u00c2\u00b7': '\u00b7',
        '\u00e2\u20ac\u201c': '\u2013',
        '\u00e2\u20ac\u201d': '\u2014',
        '\u00e2\u20ac\u00a6': '\u2026',
        '\u00e2\u20ac\u2122': '\u2019',
        '\u00e2\u20ac\u02dc': '\u2018',
        '\u00e2\u20ac\u0153': '\u201c',
        '\u00e2\u20ac\u009d': '\u201d',
        '\u00e2\u20ac\u0152': '',
        '\u00e2\u20ac\u008d': '',
        '\u00c2 ': ' ',
    }
    for broken, repaired in replacements.items():
        text = text.replace(broken, repaired)
    return text


def _decode_header_value(value):
    try:
        value = str(make_header(decode_header(str(value or ''))))
    except (LookupError, UnicodeError):
        value = str(value or '')
    return _repair_mojibake(value)


def _decode_gmail_body(data):
    if not data:
        return ''
    padded = str(data) + '=' * (-len(str(data)) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8', errors='replace')
        return _repair_mojibake(decoded)
    except Exception:
        return ''


def _message_body_parts(payload):
    plain = []
    rich = []

    def visit(part):
        mime_type = str(part.get('mimeType') or '').lower()
        filename = str(part.get('filename') or '').strip()
        data = (part.get('body') or {}).get('data')
        if data and not filename:
            decoded = _decode_gmail_body(data)
            if mime_type == 'text/plain':
                plain.append(decoded)
            elif mime_type == 'text/html':
                rich.append(decoded)
        for child in part.get('parts') or []:
            visit(child)

    visit(payload or {})
    return plain, rich


def _clean_message_text(value):
    value = html.unescape(_repair_mojibake(value)).replace('\r\n', '\n').replace('\r', '\n')
    value = re.sub(r'[\u200b-\u200d\ufeff]', '', value)
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    value = re.sub(r'\n{3,}', '\n\n', value)
    return value.strip()


def _strip_mso_conditionals(html_text):
    """Remove Outlook/Word MSO conditional comment blocks that leak raw comment
    markers (<!--[if !mso]-->, <!--[if false]-->, <!--[endif]-->, etc.) into
    the extracted plain text."""
    # Remove full conditional blocks: <!--[if ...]>...</[endif]-->
    html_text = re.sub(r'<!--\[if[^\]]*\]>.*?<!\[endif\]-->', '', html_text, flags=re.DOTALL | re.IGNORECASE)
    # Remove standalone MSO markers that do not wrap a block
    html_text = re.sub(r'<!--\[if[^>]*>.*?-->', '', html_text, flags=re.DOTALL | re.IGNORECASE)
    # Remove bare close-comment shorthand used by Google mail (<!-->)
    html_text = re.sub(r'<!-->', '', html_text)
    return html_text


def _readable_message_body(payload, fallback=''):
    plain, rich = _message_body_parts(payload)
    if plain:
        return _clean_message_text('\n\n'.join(part for part in plain if part.strip()))
    if rich:
        clean_html = _strip_mso_conditionals('\n'.join(rich))
        parser = _ReadableHtmlParser()
        parser.feed(clean_html)
        parser.close()
        return _clean_message_text(''.join(parser.parts))
    return _clean_message_text(fallback)


def _safe_message_html(payload):
    _, rich = _message_body_parts(payload)
    if not rich:
        return ''
    parser = _SafeEmailHtmlParser()
    parser.feed(_strip_mso_conditionals('\n'.join(rich)))
    parser.close()
    return ''.join(parser.parts).strip()


def get_gmail_message(message_id):
    """Fetch one full Gmail message only when the user opens it."""
    if not message_id:
        raise ValueError('message_id is required')
    service = _gmail_service()
    if not service:
        raise RuntimeError('Google credentials are not available')
    message = service.users().messages().get(userId='me', id=str(message_id), format='full').execute()
    payload = message.get('payload') or {}
    headers = {
        str(item.get('name') or ''): _decode_header_value(item.get('value'))
        for item in payload.get('headers') or []
    }
    sender_raw = headers.get('From', '')
    sender_name, sender_email = parseaddr(sender_raw)
    labels = message.get('labelIds') or []
    return {
        'id': message.get('id') or str(message_id),
        'gmail_id': message.get('id') or str(message_id),
        'thread_id': message.get('threadId'),
        'sender': sender_raw,
        'from': {'name': sender_name, 'email': sender_email.lower()},
        'to': _address_list(headers.get('To', '')),
        'cc': _address_list(headers.get('Cc', '')),
        'subject': headers.get('Subject') or 'No Subject',
        'rfc_message_id': headers.get('Message-ID') or headers.get('Message-Id') or '',
        'date': headers.get('Date', ''),
        'timestamp': headers.get('Date', ''),
        'snippet': _repair_mojibake(message.get('snippet') or ''),
        'body': _readable_message_body(payload, message.get('snippet') or ''),
        'body_html': _safe_message_html(payload),
        'is_read': 'UNREAD' not in labels,
        'is_starred': 'STARRED' in labels,
        'labels': labels,
    }


def mark_gmail_message_read(message_id):
    """Remove Gmail's UNREAD label from one exact message."""
    if not message_id:
        raise ValueError('message_id is required')
    service = _gmail_service()
    if not service:
        raise RuntimeError('Google credentials are not available')
    result = service.users().messages().modify(
        userId='me',
        id=str(message_id),
        body={'removeLabelIds': ['UNREAD']},
    ).execute()
    return {
        'id': result.get('id') or str(message_id),
        'thread_id': result.get('threadId'),
        'is_read': True,
        'labels': result.get('labelIds') or [],
    }


def mark_gmail_message_important(message_id):
    """Add Gmail's IMPORTANT and STARRED labels to one exact message."""
    if not message_id:
        raise ValueError('message_id is required')
    service = _gmail_service()
    if not service:
        raise RuntimeError('Google credentials are not available')
    result = service.users().messages().modify(
        userId='me',
        id=str(message_id),
        body={'addLabelIds': ['IMPORTANT', 'STARRED']},
    ).execute()
    return {
        'id': result.get('id') or str(message_id),
        'thread_id': result.get('threadId'),
        'is_important': True,
        'labels': result.get('labelIds') or [],
    }


def _address_list(raw):
    return [{'name': name, 'email': email} for name, email in getaddresses([raw or '']) if email]


def _batch_fetch_threads(service, refs):
    payloads = {}
    errors = {}

    def callback(request_id, response, exception):
        if exception is not None:
            errors[str(request_id)] = exception
        elif response is not None:
            payloads[str(request_id)] = response

    try:
        batch = service.new_batch_http_request()
        for ref in refs:
            thread_id = str(ref['id'])
            request = service.users().threads().get(
                userId='me',
                id=thread_id,
                format='metadata',
                metadataHeaders=['From', 'To', 'Cc', 'Subject', 'Date', 'Message-ID'],
            )
            batch.add(request, callback=callback, request_id=thread_id)
        if refs:
            batch.execute()
        return payloads
    except Exception as exc:
        print('[Gmail] batch fetch failed; falling back to sequential metadata fetch:', exc)
        payloads = {}
        for ref in refs:
            thread_id = str(ref['id'])
            try:
                payloads[thread_id] = service.users().threads().get(
                    userId='me',
                    id=thread_id,
                    format='metadata',
                    metadataHeaders=['From', 'To', 'Cc', 'Subject', 'Date'],
                ).execute()
            except Exception as item_exc:
                print(f'[Gmail] thread {thread_id} skipped:', item_exc)
    return payloads


def _parse_gmail_thread(thread_data, my_email):
    thread_id = str(thread_data.get('id') or '')
    messages = []
    thread_subject = 'No Subject'
    for item in thread_data.get('messages') or []:
        headers = {str(h.get('name') or ''): str(h.get('value') or '') for h in (item.get('payload') or {}).get('headers', [])}
        sender_raw = headers.get('From', '')
        sender_name, sender_email = parseaddr(sender_raw)
        sender_email = sender_email.lower()
        subject = headers.get('Subject') or thread_subject
        if subject and subject != 'No Subject':
            thread_subject = subject
        labels = item.get('labelIds', []) or []
        is_sent_by_me = ('SENT' in labels) or (bool(my_email) and sender_email == my_email)
        message = {
            'id': item['id'],
            'gmail_id': item['id'],
            'thread_id': thread_id,
            'sender': sender_raw,
            'from': {'name': sender_name, 'email': sender_email},
            'to': _address_list(headers.get('To', '')),
            'cc': _address_list(headers.get('Cc', '')),
            'subject': subject,
            'rfc_message_id': headers.get('Message-ID') or headers.get('Message-Id') or '',
            'date': headers.get('Date', ''),
            'timestamp': headers.get('Date', ''),
            'snippet': item.get('snippet', '') or '',
            'body': item.get('snippet', '') or '',
            'direction': 'outbound' if is_sent_by_me else 'inbound',
            'is_sent_by_me': is_sent_by_me,
            'is_read': 'UNREAD' not in labels,
            'is_starred': 'STARRED' in labels,
            'labels': labels,
        }
        messages.append(message)
    return {
        'thread_id': thread_id,
        'subject': thread_subject,
        'messages': messages,
        'latest_direction': messages[-1]['direction'] if messages else 'inbound',
    }, messages


def get_gmail_thread_changes(start_history_id, cached_threads, my_email='', limit=None):
    """Apply Gmail history changes to a recent in-memory thread snapshot."""
    service = _gmail_service()
    if not service or not start_history_id:
        return {'full_sync_required': True}
    changed_thread_ids = set()
    changed_message_ids = set()
    page_token = None
    latest_history_id = str(start_history_id)
    try:
        while True:
            kwargs = {'userId': 'me', 'startHistoryId': str(start_history_id), 'maxResults': 500}
            if page_token:
                kwargs['pageToken'] = page_token
            response = service.users().history().list(**kwargs).execute()
            latest_history_id = str(response.get('historyId') or latest_history_id)
            for entry in response.get('history') or []:
                candidates = list(entry.get('messages') or [])
                for key in ('messagesAdded', 'messagesDeleted', 'labelsAdded', 'labelsRemoved'):
                    candidates.extend(value.get('message') or {} for value in (entry.get(key) or []))
                for message in candidates:
                    if message.get('id'):
                        changed_message_ids.add(str(message['id']))
                    if message.get('threadId'):
                        changed_thread_ids.add(str(message['threadId']))
            page_token = response.get('nextPageToken')
            if not page_token:
                break
    except HttpError as exc:
        if getattr(exc.resp, 'status', None) == 404:
            return {'full_sync_required': True, 'history_id': latest_history_id}
        raise

    existing = {str(item.get('thread_id') or ''): item for item in (cached_threads or [])}
    if not changed_thread_ids:
        return {
            'full_sync_required': False,
            'history_id': latest_history_id,
            'threads': list(existing.values()),
            'emails': [message for thread in existing.values() for message in (thread.get('messages') or [])],
            'changed_message_ids': [],
        }

    refs = [{'id': thread_id} for thread_id in changed_thread_ids]
    payloads = _batch_fetch_threads(service, refs)
    changed_threads = []
    for thread_id in changed_thread_ids:
        existing.pop(thread_id, None)
        payload = payloads.get(thread_id)
        if payload:
            parsed, _ = _parse_gmail_thread(payload, str(my_email or '').lower())
            changed_threads.append(parsed)
    max_threads = max(1, min(int(limit or os.getenv('GMAIL_FETCH_LIMIT', '50') or 50), 50))
    threads = (changed_threads + list(existing.values()))[:max_threads]
    return {
        'full_sync_required': False,
        'history_id': latest_history_id,
        'threads': threads,
        'emails': [message for thread in threads for message in (thread.get('messages') or [])],
        'changed_message_ids': sorted(changed_message_ids),
    }


def get_gmail_threads():
    """
    Fetch recent Gmail context with one thread-list call plus a batched metadata fetch.
    This is much faster than issuing one full HTTP request per thread sequentially.
    """
    service = _gmail_service()
    if not service:
        return [], []

    try:
        profile = service.users().getProfile(userId='me').execute()
    except Exception as exc:
        _reset_gmail_service()
        service = _gmail_service()
        if not service:
            return [], []
        try:
            profile = service.users().getProfile(userId='me').execute()
        except Exception:
            return [], []
    my_email = str(profile.get('emailAddress') or '').strip().lower()
    limit = max(1, min(int(os.getenv('GMAIL_FETCH_LIMIT', '50') or 50), 50))
    query = _gmail_query()

    kwargs = {'userId': 'me', 'maxResults': limit, 'includeSpamTrash': False}
    if query:
        kwargs['q'] = query
    refs = service.users().threads().list(**kwargs).execute().get('threads', [])
    payloads = _batch_fetch_threads(service, refs)

    results, emails = [], []
    for ref in refs:
        thread_data = payloads.get(str(ref.get('id') or ''))
        if not thread_data:
            continue
        parsed, parsed_messages = _parse_gmail_thread(thread_data, my_email)
        results.append(parsed)
        emails.extend(parsed_messages)

    return results, emails


def create_gmail_draft(to, subject, body, thread_id=None, in_reply_to=None):
    """Create an actual Gmail draft tied to a thread so it appears in Gmail and can be reviewed."""
    from email.message import EmailMessage
    service = _gmail_service()
    if not service:
        raise RuntimeError("Google account not connected")

    msg = EmailMessage()
    msg.set_content(body or '')
    msg['To'] = to or ''
    msg['Subject'] = subject or 'Re: Update'
    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
        msg['References'] = in_reply_to

    encoded_message = base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')
    body_payload = {
        'message': {
            'raw': encoded_message
        }
    }
    if thread_id:
        body_payload['message']['threadId'] = thread_id

    try:
        draft = service.users().drafts().create(userId='me', body=body_payload).execute()
        return {
            'id': draft.get('id'),
            'message_id': (draft.get('message') or {}).get('id'),
            'thread_id': (draft.get('message') or {}).get('threadId') or thread_id
        }
    except HttpError as err:
        if err.resp.status == 403 or 'insufficient' in str(err).lower():
            raise GmailInsufficientPermissionError()
        raise err


def update_gmail_draft(draft_id, to, subject, body, thread_id=None, in_reply_to=None):
    """Update an existing Gmail draft with new or edited content before sending."""
    from email.message import EmailMessage
    service = _gmail_service()
    if not service:
        raise RuntimeError("Google account not connected")

    msg = EmailMessage()
    msg.set_content(body or '')
    msg['To'] = to or ''
    msg['Subject'] = subject or 'Re: Update'
    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
        msg['References'] = in_reply_to

    encoded_message = base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')
    body_payload = {
        'id': draft_id,
        'message': {
            'raw': encoded_message
        }
    }
    if thread_id:
        body_payload['message']['threadId'] = thread_id

    try:
        draft = service.users().drafts().update(userId='me', id=draft_id, body=body_payload).execute()
        return {
            'id': draft.get('id'),
            'message_id': (draft.get('message') or {}).get('id'),
            'thread_id': (draft.get('message') or {}).get('threadId') or thread_id
        }
    except HttpError as err:
        if err.resp.status == 403 or 'insufficient' in str(err).lower():
            raise GmailInsufficientPermissionError()
        raise err


def send_gmail_draft(draft_id):
    """Send an exact existing draft after explicit human approval."""
    service = _gmail_service()
    if not service:
        raise RuntimeError("Google account not connected")
    try:
        sent = service.users().drafts().send(userId='me', body={'id': draft_id}).execute()
        return {
            'id': sent.get('id'),
            'thread_id': sent.get('threadId'),
            'labels': sent.get('labelIds', [])
        }
    except HttpError as err:
        if err.resp.status == 403 or 'insufficient' in str(err).lower():
            raise GmailInsufficientPermissionError()
        raise


def send_gmail_direct(to, subject, body, thread_id=None, in_reply_to=None, message_id_header=None):
    """Send an exact email message directly via Gmail API users.messages.send."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg.set_content(body or '')
    msg['To'] = to or ''
    msg['Subject'] = subject or 'No Subject'
    if message_id_header:
        msg['Message-ID'] = message_id_header
    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
        msg['References'] = in_reply_to

    encoded_message = base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')
    body_payload = {
        'raw': encoded_message
    }
    if thread_id:
        body_payload['threadId'] = thread_id

    service = _gmail_service()
    if not service:
        raise RuntimeError("Google account not connected")
    try:
        sent = service.users().messages().send(userId='me', body=body_payload).execute()
        return {
            'id': sent.get('id'),
            'thread_id': sent.get('threadId'),
            'labels': sent.get('labelIds', [])
        }
    except HttpError as err:
        if err.resp.status == 403 or 'insufficient' in str(err).lower():
            raise GmailInsufficientPermissionError()
        raise


def find_sent_message_by_rfc_id(message_id_header):
    """Reconcile an ambiguous send without issuing another send request."""
    value = str(message_id_header or '').strip()
    if not value:
        return None
    service = _gmail_service()
    if not service:
        return None
    result = service.users().messages().list(
        userId='me',
        q=f'in:sent rfc822msgid:{value}',
        maxResults=1,
    ).execute()
    matches = result.get('messages') or []
    if not matches:
        return None
    message = matches[0]
    return {'id': message.get('id'), 'thread_id': message.get('threadId')}


def get_gmail_thread(thread_id):
    """
    Fetch a single Gmail thread with parsed messages, directions, and timestamps.
    Returns None if not found or credentials missing.
    """
    service = _gmail_service()
    if not service or not thread_id:
        return None
    try:
        profile = service.users().getProfile(userId='me').execute()
        my_email = str(profile.get('emailAddress') or '').strip().lower()
    except Exception:
        my_email = ""

    try:
        t_data = service.users().threads().get(userId='me', id=thread_id, format='metadata').execute()
    except Exception as e:
        print(f"[Gmail] Failed to fetch thread {thread_id}: {e}")
        return None

    messages = []
    thread_subject = 'No Subject'
    for m in t_data.get('messages', []):
        headers = {str(h.get('name') or ''): str(h.get('value') or '') for h in (m.get('payload') or {}).get('headers', [])}
        sender_raw = headers.get('From', '')
        sender_name, sender_email = parseaddr(sender_raw)
        sender_email = sender_email.lower()
        subject = headers.get('Subject') or thread_subject
        if subject and subject != 'No Subject':
            thread_subject = subject
        date = headers.get('Date', '')
        labels = m.get('labelIds', []) or []
        is_sent_by_me = ('SENT' in labels) or (bool(my_email) and sender_email == my_email)
        direction = 'outbound' if is_sent_by_me else 'inbound'
        snippet = m.get('snippet', '') or ''
        internal_date = m.get('internalDate', '')

        messages.append({
            'id': m['id'],
            'gmail_id': m['id'],
            'thread_id': thread_id,
            'sender': sender_raw,
            'from': {'name': sender_name, 'email': sender_email},
            'to': _address_list(headers.get('To', '')),
            'subject': subject,
            'rfc_message_id': headers.get('Message-ID') or headers.get('Message-Id') or '',
            'date': date,
            'internal_date': internal_date,
            'timestamp': date,
            'snippet': snippet,
            'direction': direction,
            'is_sent_by_me': is_sent_by_me,
            'labels': labels,
        })

    return {
        'thread_id': thread_id,
        'subject': thread_subject,
        'messages': messages,
        'latest_direction': messages[-1]['direction'] if messages else 'inbound',
    }


def get_gmail_draft(draft_id):
    """Fetch an existing Gmail draft by ID. Returns None if it no longer exists."""
    if not draft_id:
        return None
    service = _gmail_service()
    if not service:
        return None
    try:
        draft = service.users().drafts().get(userId='me', id=draft_id).execute()
        return draft
    except HttpError as err:
        if err.resp.status == 404:
            return None
        print(f"[Gmail] get_gmail_draft error ({draft_id}): {err}")
        return None
    except Exception as e:
        _reset_gmail_service()
        print(f"[Gmail] get_gmail_draft error ({draft_id}): {e}")
        return None


def delete_gmail_draft(draft_id):
    """
    Safely delete an exact Mailmate-created Gmail draft.
    Does nothing if draft does not exist or account is disconnected.
    """
    if not draft_id:
        return False
    service = _gmail_service()
    if not service:
        return False
    try:
        service.users().drafts().delete(userId='me', id=draft_id).execute()
        return True
    except HttpError as err:
        if err.resp.status == 404:
            return True
        if err.resp.status == 403 or 'insufficient' in str(err).lower():
            raise GmailInsufficientPermissionError()
        print(f"[Gmail] delete_gmail_draft error ({draft_id}): {err}")
        return False
    except Exception as e:
        _reset_gmail_service()
        print(f"[Gmail] delete_gmail_draft error ({draft_id}): {e}")
        return False
