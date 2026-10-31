"""Garmin: the Garmin connector for a Wizard box (contract wizard.health-data/1).

Built on the Garmin Connect Personal API (https://developer.garmin.com/gc-developer-program/personal/), Garmin's
self-serve access for individual Garmin users to their own data. Setup happens on this app's own screen: the owner
creates a personal API key on Garmin Connect Web (Personal API Key page: Create Key, accept the terms, pick an expiry
of 30, 90 or 180 days or one year; the key is shown once) and pastes it here. The key is stored server-side (0600,
never sent back). Garmin APIs do not accept the key itself: the app trades it for an access token at
POST https://personal.connectapi.garmin.com/personalapitoken-service/personal-api/personal-api-key/token with
`Authorization: ApiKey <key>`. Tokens last 24 hours (or until the key expires) and there are no refresh tokens: the
app simply asks for a new token with the key. A 401 there means the key was mistyped, deleted or has expired.

The app keeps a local SQLite copy of daily summaries (resting heart rate, intensity minutes, steps, calories, Body
Battery, stress), sleep (score and stages), overnight HRV, Body Battery at wake and activities: a backfill after the
key is accepted, then an incremental sync every 20 minutes with a few days of overlap so late uploads are picked up.
Other apps on the box (the Health app) read the copy through the box-only health-data contract: GET /api/status and
GET /api/days (docs/health-data-contract.md). Every stored change bumps a sequence number, so readers can ask for only
the days changed since their cursor.

Stdlib only. Secrets live in DATA_DIR (0600 files, 0700 folder) and are never returned by any API or tool.
"""
import base64
import datetime as dt
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path(__file__).resolve().parent.parent if '__file__' in globals() else Path(os.environ.get('APP_ROOT', '.')).resolve()
DATA = Path(os.environ.get('DATA_DIR', ROOT / '.data'))
APP_ID = 'wizard-garmin'
NAME = 'Garmin'
VERSION = '0.1.0'
PREFIX = '/' + APP_ID
PORT = os.environ.get('PORT', '8838')
API_BASE = os.environ.get('GARMIN_API_BASE', 'https://personal.connectapi.garmin.com').rstrip('/')
TOKEN_PATH = '/personalapitoken-service/personal-api/personal-api-key/token'
KEY_PAGE_URL = 'https://connect.garmin.com/app/settings/personalApiToken'
DOCS_URL = 'https://developer.garmin.com/gc-developer-program/personal/personal-faq/'
SIGNUP_URL = os.environ.get('GARMIN_SIGNUP_URL', 'https://connect.garmin.com')
EXPIRY_CHOICES = [30, 90, 180, 365]   # what Garmin Connect Web offers when creating a key
SYNC_MINUTES = float(os.environ.get('GARMIN_SYNC_MINUTES', '20'))
OVERLAP_DAYS = int(os.environ.get('GARMIN_OVERLAP_DAYS', '4'))
HISTORY_DAYS = int(os.environ.get('GARMIN_HISTORY_DAYS', '364'))     # daily summaries take up to one year per call
WAKE_DAYS = int(os.environ.get('GARMIN_WAKE_DAYS', '90'))            # Body Battery at wake: one call per day, so a shorter backfill
WINDOW_DAYS = int(os.environ.get('GARMIN_WINDOW_DAYS', '31'))        # sleep: at most 31 days per call
ACTIVITY_PAGE = int(os.environ.get('GARMIN_ACTIVITY_PAGE', '99'))
REQUEST_GAP = float(os.environ.get('GARMIN_REQUEST_GAP', '0.3'))
RETRIES = int(os.environ.get('GARMIN_RETRIES', '4'))
BACKOFF = float(os.environ.get('GARMIN_BACKOFF', '5'))
TOKEN_MARGIN = 3600            # ask for a new token when the cached one has under an hour left (as Garmin's own client does)
UA = f'{APP_ID}/{VERSION} (+Wizard OS)'
STATIC = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml'}
FORWARDED = ('tailscale-user-login', 'x-forwarded-for', 'forwarded', 'x-real-ip', 'cf-connecting-ip')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
LOCK = threading.RLock()          # local files + database
SYNC_LOCK = threading.Lock()
WAKE = threading.Event()


class Problem(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class ProviderError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ---------------- private storage ----------------

def _private():
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
    return DATA


def _write_secret(name, text):
    p = _private() / name
    tmp = p.with_name('.' + name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def _read(name):
    try:
        return json.loads((DATA / name).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _shred(name):
    p = DATA / name
    try:
        size = p.stat().st_size
        with open(p, 'r+b') as f:
            f.write(b'\0' * size)
            f.flush()
            os.fsync(f.fileno())
        p.unlink()
    except FileNotFoundError:
        pass


def _key():
    return _read('key.json')


def _set_key(**kv):
    with LOCK:
        k = _key()
        if k:
            k.update(kv)
            _write_secret('key.json', json.dumps(k))


def _tokens():
    return _read('tokens.json')


def _save_tokens(t):
    with LOCK:
        if t:
            _write_secret('tokens.json', json.dumps(t))
        else:
            _shred('tokens.json')


def _state():
    return _read('state.json')


def _update_state(**kv):
    with LOCK:
        s = _state()
        for k, v in kv.items():
            if v is None:
                s.pop(k, None)
            else:
                s[k] = v
        _write_secret('state.json', json.dumps(s))
        return s


def secure_files():
    """Runtipi loosens app-data permissions; tighten them again on every start."""
    db().close()
    for p in DATA.iterdir():
        if p.is_file():
            os.chmod(p, 0o600)


def db():
    path = _private() / 'garmin.sqlite3'
    fresh = not path.exists()
    c = sqlite3.connect(path, timeout=15)
    if fresh:
        os.chmod(path, 0o600)
    c.row_factory = sqlite3.Row
    c.executescript('''
      CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL, id TEXT NOT NULL, day TEXT, start TEXT, data TEXT NOT NULL,
        changed_seq INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (kind, id));
      CREATE INDEX IF NOT EXISTS records_day ON records(kind, day);
      CREATE INDEX IF NOT EXISTS records_changed ON records(changed_seq);
      CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, data TEXT NOT NULL);
    ''')
    return c


def _next_seq(c):
    n = int(json.loads((c.execute("SELECT data FROM meta WHERE key='seq'").fetchone() or ['0'])[0])) + 1
    c.execute("INSERT OR REPLACE INTO meta (key,data) VALUES ('seq',?)", (str(n),))
    return n


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(t):
    return t.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def _parse(s):
    """ISO text, 'yyyy-MM-dd HH:mm:ss' (GMT) or epoch milliseconds -> aware datetime (UTC when no offset)."""
    if s is None or s == '' or isinstance(s, bool):
        return None
    if isinstance(s, (int, float)):
        return dt.datetime.fromtimestamp(s / 1000, dt.timezone.utc)
    s = str(s).strip()
    if re.fullmatch(r'\d{11,14}', s):
        return dt.datetime.fromtimestamp(int(s) / 1000, dt.timezone.utc)
    try:
        t = dt.datetime.fromisoformat(s.replace('Z', '+00:00').replace(' ', 'T', 1))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _ago(iso):
    t = _parse(iso)
    return (_now() - t).total_seconds() if t else 1e12


# ---------------- Garmin HTTP ----------------

def _http(method, url, headers=None, timeout=45):
    h = {'User-Agent': UA, 'Accept': 'application/json'}
    h.update(headers or {})
    req = urllib.request.Request(url, data=b'' if method == 'POST' else None, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProviderError(0, 'unreachable', 'Garmin did not answer. Check the box is online; the app will retry.') from None


def _json(b):
    try:
        return json.loads(b or b'null')
    except ValueError:
        return None


def _jwt_exp(token):
    """The token's `exp` claim (seconds), read without verifying, as Garmin's own client does; None if absent."""
    try:
        part = token.split('.')[1]
        return float(json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))['exp'])
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def _exchange(api_key):
    """POST the key to Garmin's token endpoint. Returns (status, token or None)."""
    status, body, _ = _http('POST', API_BASE + TOKEN_PATH, headers={'Authorization': 'ApiKey ' + api_key, 'Accept': '*/*'})
    if status != 200:
        return status, None
    text = (body or b'').decode('utf-8', 'replace').strip()
    d = _json(body)
    if isinstance(d, dict):     # tolerate a JSON reply; Garmin's documented client reads the raw text body
        text = str(d.get('access_token') or d.get('accessToken') or d.get('token') or '')
    if not text or len(text) > 16384 or any(ch.isspace() for ch in text):
        return 502, None
    return 200, text


def _keep_token(tok):
    _save_tokens({'access_token': tok, 'expires_at': _jwt_exp(tok) or time.time() + 24 * 3600, 'issued_at': _iso(_now())})


def _token(force=False):
    """A valid access token, trading the saved key for a new one when the cached token is near its end."""
    t = _tokens()
    if not force and t.get('access_token') and (t.get('expires_at') or 0) - time.time() > TOKEN_MARGIN:
        return t['access_token']
    k = _key()
    if not k.get('api_key'):
        raise Problem(409, 'Not connected to Garmin. Open the Garmin app and paste your personal API key.')
    status, tok = _exchange(k['api_key'])
    if status in (401, 403):
        _key_rejected()
    if status == 429:
        _update_state(rate_limited_until=time.time() + 900)
        raise ProviderError(429, 'rate_limited', 'Garmin asked the app to slow down. Syncing resumes in about 15 minutes.')
    if status != 200 or not tok:
        raise ProviderError(status, 'token_failed', f'Garmin could not issue an access token ({status}). The app will retry.')
    _keep_token(tok)
    return tok


def _key_rejected():
    msg = ('Garmin no longer accepts your personal API key. It may have expired or been deleted on Garmin Connect. '
           'Create a new key and paste it in the Garmin app.')
    with LOCK:
        _save_tokens(None)
        _set_key(verified='invalid', checked_at=_iso(_now()))
        _update_state(auth='needs_login', last_error=msg)
    raise Problem(401, msg)


def test_key():
    """Trade the saved key for a token at Garmin's real endpoint: 200 = valid, 401/403 = wrong, deleted or expired."""
    k = _key()
    if not k.get('api_key'):
        raise Problem(409, 'Paste your Garmin personal API key first.')
    status, tok = _exchange(k['api_key'])
    if status == 200 and tok:
        result, msg = 'valid', 'Garmin accepted your key. Your data is downloading now.'
        _keep_token(tok)
    elif status in (400, 401, 403):
        result, msg = 'invalid', ('Garmin did not accept this key. Copy it again (Garmin shows it only once), or create a new '
                                  'key on Garmin Connect and paste that.')
    elif status == 429:
        result, msg = 'unknown', 'Garmin is busy right now. Try the test again in a minute.'
    else:
        result, msg = 'unknown', 'Garmin gave an unexpected answer, so the key could not be confirmed. Try again shortly.'
    with LOCK:
        _set_key(verified=result, checked_at=_iso(_now()))
        if result == 'valid':
            _update_state(auth='connected', last_error=None, connected_at=_iso(_now()))
        elif result == 'invalid':
            _save_tokens(None)
            _update_state(auth='needs_login' if _state().get('backfilled') else None)
    if result == 'valid':
        start_sync_thread(full=not _state().get('backfilled'))
    return {'result': result, 'message': msg}


def save_key(body):
    key = str(body.get('api_key') or '').strip()
    if key.lower().startswith('apikey '):
        key = key[7:].strip()
    if not re.fullmatch(r'[\x21-\x7e]{16,4096}', key):
        raise Problem(400, 'That does not look like a Garmin personal API key. Copy the whole key Garmin showed and paste it again.')
    try:
        days = int(body.get('expiry_days') or 365)
    except (TypeError, ValueError):
        raise Problem(400, 'Pick how long the key lasts: 30, 90, 180 days or one year.') from None
    if days not in EXPIRY_CHOICES:
        raise Problem(400, 'Pick how long the key lasts: 30, 90, 180 days or one year.')
    with LOCK:
        old = _key()
        _write_secret('key.json', json.dumps({'api_key': key, 'saved_at': _iso(_now()), 'expiry_days': days,
                                              'expires_about': (_now() + dt.timedelta(days=days)).date().isoformat(),
                                              'verified': None}))
        if old.get('api_key') != key:
            _save_tokens(None)   # tokens belong to the old key
    return setup_info()


def forget_key():
    with LOCK:
        _shred('key.json')
        _save_tokens(None)
        _update_state(auth=None)
    return setup_info()


def _mask(v):
    v = str(v or '')
    return ('•' * 6 + v[-4:]) if len(v) > 8 else '•' * 6


def setup_info():
    k = _key()
    return {'key_page_url': KEY_PAGE_URL, 'signup_url': SIGNUP_URL, 'docs_url': DOCS_URL, 'expiry_choices': EXPIRY_CHOICES,
            'key_saved': bool(k.get('api_key')), 'key_hint': _mask(k.get('api_key')) if k.get('api_key') else None,
            'expiry_days': k.get('expiry_days'), 'expires_about': k.get('expires_about'),
            'verified': k.get('verified'), 'checked_at': k.get('checked_at')}


def api_get(path, params=None):
    url = API_BASE + path + ('?' + urlencode(params) if params else '')
    status, forced = 0, False
    for attempt in range(RETRIES + 1):
        status, body, headers = _http('GET', url, headers={'Authorization': 'Bearer ' + _token()})
        if status == 200:
            return _json(body)
        if status in (204, 404):
            return None
        if status == 401 and not forced:   # the token ended early: trade the key again once
            forced = True
            _token(force=True)
            continue
        if status == 401:
            _key_rejected()
        if status == 403:
            raise ProviderError(403, 'forbidden', f'Garmin does not share {path} with this key.')
        if status == 400:
            raise ProviderError(400, 'bad_request', f'Garmin refused the request for {path}.')
        if status == 429 or status >= 500:
            hdr = {k.lower(): v for k, v in headers.items()}
            try:
                wait = float(str(hdr.get('retry-after') or '0').split(',')[0])
            except ValueError:
                wait = 0
            if (status == 429 and wait > 120) or attempt >= RETRIES:
                break
            time.sleep(min(120.0, max(wait, BACKOFF * (2 ** attempt))))
            continue
        raise ProviderError(status, 'api_error', f'Garmin answered {status} for {path}.')
    if status == 429:
        _update_state(rate_limited_until=time.time() + 900)
        raise ProviderError(429, 'rate_limited', 'Garmin asked the app to slow down. Syncing resumes in about 15 minutes.')
    raise ProviderError(status, 'api_error', f'Garmin answered {status} for {path}. The app will retry.')


# ---------------- sync ----------------

def _save(kind, rows, key):
    """Upsert Garmin records keyed by `key(r) -> (id, day, start)`. A row's changed_seq moves only when its data really
    changed, so an overlapping re-sync does not make every reader re-read the last few days."""
    n = 0
    with LOCK:
        c = db()
        with c:
            seq = _next_seq(c)
            for r in rows or []:
                if not isinstance(r, dict):
                    continue
                rid, day, start = key(r)
                if not rid or not day or not DATE_RE.match(str(day)):
                    continue
                c.execute('''INSERT INTO records (kind,id,day,start,data,changed_seq) VALUES (?,?,?,?,?,?)
                             ON CONFLICT(kind,id) DO UPDATE SET day=excluded.day, start=excluded.start,
                               changed_seq=CASE WHEN records.data IS excluded.data THEN records.changed_seq ELSE excluded.changed_seq END,
                               data=excluded.data''',
                          (kind, str(rid), day, start, json.dumps(r, sort_keys=True), seq))
                n += 1
        c.close()
    return n


def _date_key(r):
    v = str(r.get('calendarDate') or '')[:10] or None
    return v, v, None


def _sleep_key(r):
    d = str(r.get('calendarDate') or '')[:10] or None
    st = _parse(r.get('sleepStartTimestampGMT'))
    return d, d, _iso(st) if st else None


def _activity_key(r):
    st = _parse(r.get('startTimeGMT')) or _parse(r.get('beginTimestamp'))
    day = str(r.get('startTimeLocal') or '')[:10] or (st.date().isoformat() if st else None)
    return (str(r.get('activityId')) if r.get('activityId') is not None else None), day, _iso(st) if st else None


def _windows(start, end, size):
    """Inclusive date windows of at most `size` days, oldest first."""
    out, a = [], start
    while a <= end:
        b = min(end, a + dt.timedelta(days=size - 1))
        out.append((a, b))
        a = b + dt.timedelta(days=1)
    return out


def _today():
    return (_now() + dt.timedelta(hours=14)).date()   # the owner's 'today' can be ahead of UTC


def _progress(kind, n):
    _update_state(progress={'kind': kind, 'records': n})


def _pull_summaries(since, end):
    n = 0
    for a, b in _windows(since, end, 365):
        d = api_get('/usersummary-service/v2/dailysummaries', {'startDate': a.isoformat(), 'endDate': b.isoformat()})
        rows = d.get('data') if isinstance(d, dict) else (d if isinstance(d, list) else [])
        n += _save('summary', rows or [], _date_key)
        _progress('daily summaries', n)
        time.sleep(REQUEST_GAP)
    return n


def _pull_sleep(since, end):
    n = 0
    for a, b in _windows(since, end, WINDOW_DAYS):
        d = api_get('/sleep-service/sleep/dailySleepSummariesByDate', {'startDate': a.isoformat(), 'endDate': b.isoformat()})
        rows = [x for x in (d if isinstance(d, list) else []) if isinstance(x, dict) and (x.get('sleepTimeSeconds') or x.get('sleepScores'))]
        n += _save('sleep', rows, _sleep_key)
        _progress('sleep', n)
        time.sleep(REQUEST_GAP)
    return n


def _pull_hrv(since, end):
    n = 0
    for a, b in _windows(since, end, WINDOW_DAYS):
        d = api_get(f'/hrv-service/hrv/daily/{a.isoformat()}/{b.isoformat()}')
        rows = d.get('hrvSummaries') if isinstance(d, dict) else []
        n += _save('hrv', rows or [], _date_key)
        _progress('HRV', n)
        time.sleep(REQUEST_GAP)
    return n


WAKE_FIELDS = ('calendarDate', 'bodyBatteryAtWakeTime', 'bodyBatteryDuringSleep', 'bodyBatteryHighestValue',
               'bodyBatteryLowestValue', 'lastSevenDaysAvgRestingHeartRate', 'bodyBatteryVersion')


def _pull_wake(since, end):
    """Body Battery at wake time comes only from the one-day user summary: one call per day."""
    n, day = 0, max(since, end - dt.timedelta(days=WAKE_DAYS - 1))
    while day <= end:
        d = api_get('/usersummary-service/usersummary/daily', {'calendarDate': day.isoformat()})
        if isinstance(d, dict) and d.get('bodyBatteryAtWakeTime') is not None:
            keep = {k: d.get(k) for k in WAKE_FIELDS}
            keep['calendarDate'] = str(keep.get('calendarDate') or day.isoformat())[:10]
            n += _save('wake', [keep], _date_key)
        _progress('Body Battery', n)
        day += dt.timedelta(days=1)
        time.sleep(REQUEST_GAP)
    return n


KEEP_ACTIVITY = ('activityId', 'activityName', 'activityType', 'startTimeLocal', 'startTimeGMT', 'endTimeGMT', 'beginTimestamp',
                 'duration', 'distance', 'calories', 'averageHR', 'maxHR', 'steps', 'activityTrainingLoad',
                 'aerobicTrainingEffect', 'anaerobicTrainingEffect', 'trainingEffectLabel', 'moderateIntensityMinutes',
                 'vigorousIntensityMinutes', 'differenceBodyBattery', 'hrTimeInZone_1', 'hrTimeInZone_2', 'hrTimeInZone_3',
                 'hrTimeInZone_4', 'hrTimeInZone_5', 'sportTypeId', 'manufacturer', 'isManualActivity')


def _pull_activities(since, end):
    n, start = 0, 0
    while True:
        d = api_get('/activitylist-service/activities/search/activities',
                    {'start': start, 'limit': ACTIVITY_PAGE, 'startDate': since.isoformat(), 'endDate': end.isoformat(), 'sortOrder': 'asc'})
        rows = [r for r in (d if isinstance(d, list) else []) if isinstance(r, dict)]
        n += _save('activity', [{k: r.get(k) for k in KEEP_ACTIVITY if k in r} for r in rows], _activity_key)
        _progress('activities', n)
        time.sleep(REQUEST_GAP)
        if len(rows) < ACTIVITY_PAGE:
            return n
        start += ACTIVITY_PAGE


def sync(full=False):
    if not SYNC_LOCK.acquire(blocking=False):
        return dict(status_info(), note='A sync is already running.')
    try:
        st = _state()
        if (st.get('rate_limited_until') or 0) > time.time():
            raise ProviderError(429, 'rate_limited', 'Garmin asked the app to slow down. Syncing resumes shortly.')
        full = full or not st.get('backfilled')
        _update_state(syncing=_iso(_now()), sync_kind='backfill' if full else 'incremental')
        end = _today()
        if full:
            since = _now().date() - dt.timedelta(days=HISTORY_DAYS)
        else:
            since = ((_parse(st.get('last_sync')) or _now()) - dt.timedelta(days=OVERLAP_DAYS)).date()
        counts = {'summaries': _pull_summaries(since, end), 'sleep': _pull_sleep(since, end),
                  'hrv': _pull_hrv(since, end), 'body_battery': _pull_wake(since, end),
                  'activities': _pull_activities(since, end)}
        _update_state(last_sync=_iso(_now()), last_error=None, backfilled=True, auth='connected', syncing=None, progress=None,
                      sync_kind=None, last_counts=counts, rate_limited_until=None)
        return dict(status_info(), pulled=counts)
    except (Problem, ProviderError) as e:
        _update_state(last_error=e.message, syncing=None, progress=None, sync_kind=None)
        raise
    finally:
        SYNC_LOCK.release()


def _safe_sync(full=False):
    try:
        sync(full)
    except (Problem, ProviderError):
        pass
    except Exception:   # never let a bad record stop the scheduler
        _update_state(last_error='Sync failed unexpectedly; it will retry.', syncing=None, progress=None)


def start_sync_thread(full=False):
    threading.Thread(target=_safe_sync, args=(full,), daemon=True).start()


def _scheduler():
    time.sleep(5)
    while True:
        st, k = _state(), _key()
        if k.get('api_key') and k.get('verified') == 'valid' and st.get('auth') != 'needs_login':
            due = not st.get('backfilled') or _ago(st.get('last_sync')) >= SYNC_MINUTES * 60
            if due and (st.get('rate_limited_until') or 0) <= time.time():
                _safe_sync(full=not st.get('backfilled'))
        WAKE.wait(60)
        WAKE.clear()


# ---------------- the health-data contract (wizard.health-data/1, see docs/health-data-contract.md) ----------------

CONTRACT = 'wizard.health-data/1'
SOURCE = {'id': APP_ID, 'provider': 'garmin', 'name': NAME, 'app_path': PREFIX + '/'}
# Readiness = Garmin's own Body Battery level at wake time (0-100), passed through unchanged (formula
# garmin-body-battery-at-wake/1). The Personal API does not expose Training Readiness. No Body Battery at wake that
# day = no readiness; nothing is derived from HRV or sleep.
READINESS_LABEL = 'Body Battery at wake'
# Activity load = Garmin intensity minutes: moderate minutes + 2 x vigorous minutes, the way Garmin counts them toward
# its weekly goal (default 150). Open-ended, so `max` is null. Daily targets per recovery band keep WHOOP's shape on
# Garmin's own goal: Ready = the daily share of the weekly goal up to about double, So-so = a light day up to the share,
# Run down = under 10, and "too much" at more than 15 minutes above the band's top. scale_max = top of the drawing scale.
IM_WEEKLY_GOAL = 150
IM_SCALE_MAX = 60
IM_OVER = 15
LOAD_LABEL = 'Intensity minutes'


def im_targets(weekly_goal=None):
    g = weekly_goal if isinstance(weekly_goal, (int, float)) and not isinstance(weekly_goal, bool) and weekly_goal > 0 else IM_WEEKLY_GOAL
    share = max(10, int(round(g / 7)))
    return {'high': [share, share * 2], 'medium': [10, share], 'low': [0, 10]}


SCALES = {'readiness': {'label': READINESS_LABEL, 'min': 0, 'max': 100, 'available': True, 'formula': 'garmin-body-battery-at-wake/1',
                        'note': "Garmin's Body Battery level when you woke up. Garmin's Personal API does not share Training Readiness."},
          'sleep_score': {'label': 'Sleep score', 'min': 0, 'max': 100},
          'load': {'label': LOAD_LABEL, 'short_label': 'Intensity min', 'unit': 'min', 'min': 0, 'max': None,
                   'scale_max': IM_SCALE_MAX, 'weekly_goal': IM_WEEKLY_GOAL, 'targets': im_targets(), 'over_margin': IM_OVER,
                   'formula': 'garmin-intensity-minutes/1'}}


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _r(v, n=1):
    v = _num(v)
    if v is None:
        return None
    return round(v) if n == 0 else round(v, n)


def _hours(s):
    s = _num(s)
    return round(s / 3600, 2) if s is not None else None


def _band(score):
    if not isinstance(score, (int, float)):
        return None
    return 'high' if score >= 67 else 'medium' if score >= 34 else 'low'


def intensity_minutes(moderate, vigorous):
    """Garmin intensity minutes (formula garmin-intensity-minutes/1): moderate + 2 x vigorous; None when neither is given."""
    m, v = _num(moderate), _num(vigorous)
    if m is None and v is None:
        return None
    return int(round((m or 0) + 2 * (v or 0)))


def _load(value, weekly_goal=None):
    if value is None:
        return None
    return {'value': value, 'max': None, 'label': LOAD_LABEL, 'unit': 'min', 'scale_max': IM_SCALE_MAX,
            'targets': im_targets(weekly_goal), 'over_margin': IM_OVER}


def readiness_from(wake):
    v = _num((wake or {}).get('bodyBatteryAtWakeTime'))
    if v is None or not 0 <= v <= 100:
        return None
    v = int(round(v))
    return {'score': v, 'band': _band(v), 'state': 'scored', 'calibrating': False}


def _dataset():
    st = _state()
    if not st.get('dataset'):
        st = _update_state(dataset=secrets.token_hex(12))
    return st['dataset']


def sleep_score(s):
    """Garmin's overall sleep score (0-100) from DailySleepDTO.sleepScores (a map of SleepScoreItem; 'overall')."""
    scores = s.get('sleepScores') or {}
    if not isinstance(scores, dict):
        return None
    item = scores.get('overall')
    if not isinstance(item, dict):
        item = next((v for k, v in scores.items() if 'overall' in str(k).lower() and isinstance(v, dict)), None)
    v = _num((item or {}).get('value'))
    return int(round(v)) if v is not None and 0 <= v <= 100 else None


def _n_sleep(s):
    a, b = _parse(s.get('sleepStartTimestampGMT')), _parse(s.get('sleepEndTimestampGMT'))
    in_bed = (b - a).total_seconds() if a and b else None
    asleep = _num(s.get('sleepTimeSeconds'))
    need = (s.get('sleepNeed') or {}).get('actual') if isinstance(s.get('sleepNeed'), dict) else None
    naps = []
    for n in s.get('dailyNapDTOS') or []:
        if isinstance(n, dict):
            na, nb = _parse(n.get('napStartTimestampGMT')), _parse(n.get('napEndTimestampGMT'))
            naps.append({'start': _iso(na) if na else None, 'end': _iso(nb) if nb else None, 'asleep_h': _hours(n.get('napTimeSec'))})
    return {'start': _iso(a) if a else None, 'end': _iso(b) if b else None, 'asleep_h': _hours(asleep), 'in_bed_h': _hours(in_bed),
            'awake_h': _hours(s.get('awakeSleepSeconds')), 'light_h': _hours(s.get('lightSleepSeconds')),
            'deep_h': _hours(s.get('deepSleepSeconds')), 'rem_h': _hours(s.get('remSleepSeconds')),
            'score': sleep_score(s),
            'efficiency_pct': _r(100 * asleep / in_bed, 0) if asleep and in_bed else None,
            'respiratory_rate': _r(s.get('averageRespirationValue')),
            # SleepNeedDTO.actual: Garmin's sleep need for the night, in minutes (Garmin Connect's sleep coach)
            'need_h': round(need / 60, 2) if _num(need) and need > 0 else None, 'naps': naps}


def _zones(a):
    secs = [_num(a.get(f'hrTimeInZone_{i}')) for i in range(1, 6)]
    if all(x is None for x in secs):
        return None
    return [None] + [round(x / 60, 1) if x is not None else None for x in secs]   # Garmin zones 1-5; no zone 0


def _n_workout(a):
    st = _parse(a.get('startTimeGMT')) or _parse(a.get('beginTimestamp'))
    secs = _num(a.get('duration'))
    en = _parse(a.get('endTimeGMT')) or (st + dt.timedelta(seconds=secs) if st and secs is not None else None)
    t = a.get('activityType') or {}
    sport = str(t.get('typeKey') or a.get('activityName') or 'activity').replace('_', ' ').capitalize()
    im = intensity_minutes(a.get('moderateIntensityMinutes'), a.get('vigorousIntensityMinutes'))
    return {'id': str(a.get('activityId')), 'sport': sport, 'start': _iso(st) if st else None, 'end': _iso(en) if en else None,
            'minutes': round(secs / 60) if secs is not None else None, 'load': _load(im),
            'avg_hr_bpm': _r(a.get('averageHR'), 0), 'max_hr_bpm': _r(a.get('maxHR'), 0), 'calories_kcal': _r(a.get('calories'), 0),
            'energy_kj': None, 'distance_m': _r(a.get('distance'), 0), 'zones_min': _zones(a),
            'extras': {'garmin': {'name': a.get('activityName'), 'type': t.get('typeKey'),
                                  'training_load': _r(a.get('activityTrainingLoad')),
                                  'aerobic_te': _r(a.get('aerobicTrainingEffect')), 'anaerobic_te': _r(a.get('anaerobicTrainingEffect')),
                                  'training_effect_label': a.get('trainingEffectLabel'),
                                  'body_battery_change': a.get('differenceBodyBattery')}}}


def _n_day(day, rows):
    su, sl, hrv, wake = rows.get('summary') or {}, rows.get('sleep'), rows.get('hrv') or {}, rows.get('wake')
    acts = rows.get('activity') or []
    hr, im, bb = su.get('heartRate') or {}, su.get('intensityMinutes') or {}, su.get('bodyBattery') or {}
    cal, mv, stress = su.get('calories') or {}, (su.get('movement') or {}).get('steps') or {}, su.get('stress') or {}
    resp, ox = su.get('respiration') or {}, su.get('pulseOx') or {}
    load_v = intensity_minutes(im.get('moderate'), im.get('vigorous'))
    if load_v is None and acts:   # no daily summary yet: add up the day's activities
        ws = [intensity_minutes(a.get('moderateIntensityMinutes'), a.get('vigorousIntensityMinutes')) for a in acts]
        ws = [x for x in ws if x is not None]
        load_v = sum(ws) if ws else None
    readiness = readiness_from(wake)
    stamps = [x for x in [(su.get('wellnessChronology') or {}).get('endTimestampGmt'), (sl or {}).get('sleepEndTimestampGMT')] if x]
    stamps = [_iso(t) for t in (_parse(x) for x in stamps) if t]
    tl = [x for x in (_num(a.get('activityTrainingLoad')) for a in acts) if x is not None]
    base = hrv.get('baseline') or {}
    balanced = [base.get('balancedLow'), base.get('balancedUpper')]
    extras = {'readiness_formula': 'garmin-body-battery-at-wake/1' if readiness else None,
              'body_battery_at_wake': _num((wake or {}).get('bodyBatteryAtWakeTime')),
              'body_battery_during_sleep': _num((wake or {}).get('bodyBatteryDuringSleep')),
              'body_battery_high': _num(bb.get('maxValue')), 'body_battery_low': _num(bb.get('minValue')),
              'body_battery_charged': _num(bb.get('chargedValue')), 'body_battery_drained': _num(bb.get('drainedValue')),
              'hrv_status': hrv.get('status'), 'hrv_weekly_avg_ms': _num(hrv.get('weeklyAvg')),
              'hrv_5min_high_ms': _num(hrv.get('lastNight5MinHigh')),
              'hrv_balanced_range_ms': balanced if all(_num(x) is not None for x in balanced) else None,
              'resting_hr_7d_avg': _num((wake or {}).get('lastSevenDaysAvgRestingHeartRate')),
              'moderate_intensity_min': _num(im.get('moderate')), 'vigorous_intensity_min': _num(im.get('vigorous')),
              'intensity_minutes_weekly_goal': _num(im.get('goal')),
              'stress_avg': _num(stress.get('avgLevel')), 'stress_max': _num(stress.get('maxLevel')), 'stress_qualifier': stress.get('qualifier'),
              'active_calories_kcal': _num(cal.get('burnedActive')), 'steps_goal': _num(mv.get('goal')),
              'distance_m': _r(mv.get('distanceInMeters'), 0), 'training_load_activities': round(sum(tl), 1) if tl else None,
              'sleep_quality': (sl or {}).get('sleepQualityTypePK'), 'sleep_feedback': (sl or {}).get('sleepScoreFeedback')}
    return {
        'date': day, 'updated_at': max(stamps) if stamps else None, 'readiness': readiness,
        'hrv_ms': _num(hrv.get('lastNightAvg')), 'resting_hr_bpm': _num(hr.get('restingValue')),
        'spo2_pct': _r(ox.get('avgValue')), 'skin_temp_c': None, 'skin_temp_delta_c': None,
        'respiratory_rate': _r((sl or {}).get('averageRespirationValue')) or _r(resp.get('avgValue')),
        'sleep': _n_sleep(sl) if sl else None,
        'load': _load(load_v, im.get('goal')), 'calories_kcal': _r(cal.get('burnedTotal'), 0), 'energy_kj': None,
        'avg_hr_bpm': None, 'max_hr_bpm': _num(hr.get('maxValue')), 'steps': _num(mv.get('value')),
        'workouts': [_n_workout(a) for a in sorted(acts, key=lambda x: str(x.get('startTimeGMT') or ''))],
        'extras': {'garmin': extras},
    }


def _build_days(dates):
    if not dates:
        return []
    with LOCK:
        c = db()
        try:
            marks = ','.join('?' * len(dates))
            by = {}
            for x in c.execute(f'SELECT kind, day, data FROM records WHERE day IN ({marks}) ORDER BY start', list(dates)):
                d = by.setdefault(x['day'], {})
                if x['kind'] == 'activity':
                    d.setdefault('activity', []).append(json.loads(x['data']))
                else:
                    d[x['kind']] = json.loads(x['data'])
        finally:
            c.close()
    return [_n_day(day, by.get(day, {})) for day in dates]


def _page_token(d):
    return base64.urlsafe_b64encode(json.dumps(d, separators=(',', ':')).encode()).decode().rstrip('=')


def _untoken(s):
    try:
        d = json.loads(base64.urlsafe_b64decode(s + '=' * (-len(s) % 4)))
        if isinstance(d, dict):
            return d
    except (ValueError, TypeError):
        pass
    raise Problem(400, 'page is not a page token from this source.')


def _cursor_seq(s):
    if s in (None, ''):
        return 0
    m = re.fullmatch(r'g1\.(\d{1,15})', str(s))
    if not m:
        raise Problem(400, 'changed_since is not a cursor from this source.')
    return int(m.group(1))


def _seq_now(c):
    row = c.execute("SELECT data FROM meta WHERE key='seq'").fetchone()
    return int(json.loads(row['data'])) if row else 0


def _check_day(v, name):
    if v is None or v == '':
        return None
    if not DATE_RE.match(str(v)):
        raise Problem(400, f'{name} must be YYYY-MM-DD.')
    return str(v)


def contract_days(args):
    """GET /api/days: normalized day records, oldest first, paged; optionally only days changed since a cursor."""
    frm, to = _check_day(args.get('from'), 'from'), _check_day(args.get('to'), 'to')
    if frm and to and frm > to:
        raise Problem(400, 'from must be on or before to.')
    try:
        limit = max(1, min(500, int(args.get('limit') or 100)))
    except (TypeError, ValueError):
        raise Problem(400, 'limit must be a number.') from None
    page = _untoken(str(args['page'])) if args.get('page') else {}
    changed = 'changed_since' in args
    since = _cursor_seq(args.get('changed_since')) if changed else 0
    after = page.get('after') or ''
    with LOCK:
        c = db()
        try:
            hw = int(page.get('hw') or _seq_now(c))
            q, p = ['SELECT DISTINCT day FROM records WHERE day IS NOT NULL AND day>?'], [after]
            if frm:
                q.append('AND day>=?'); p.append(frm)
            if to:
                q.append('AND day<=?'); p.append(to)
            if changed:
                q.append('AND day IN (SELECT day FROM records WHERE changed_seq>? AND changed_seq<=?)')
                p += [since, hw]
            q.append('ORDER BY day LIMIT ?'); p.append(limit + 1)
            dates = [r['day'] for r in c.execute(' '.join(q), p)]
        finally:
            c.close()
    more = len(dates) > limit
    dates = dates[:limit]
    return {'contract': CONTRACT, 'source': APP_ID, 'dataset': _dataset(), 'days': _build_days(dates),
            'next_page': _page_token({'after': dates[-1], 'hw': hw}) if more else None, 'cursor': f'g1.{hw}'}


def _counts():
    with LOCK:
        c = db()
        out = {r['kind']: r['n'] for r in c.execute('SELECT kind, COUNT(*) n FROM records GROUP BY kind')}
        span = c.execute('SELECT MIN(day) a, MAX(day) b, COUNT(DISTINCT day) n FROM records').fetchone()
        c.close()
    return out, span['a'], span['b'], span['n']


def _days_left(k):
    """Days left on the key, from the expiry the owner picked when he created it (Garmin also emails a week before)."""
    try:
        return (dt.date.fromisoformat(k['expires_about']) - _now().date()).days
    except (KeyError, TypeError, ValueError):
        return None


def status_info():
    k, st = _key(), _state()
    saved, ver = bool(k.get('api_key')), k.get('verified')
    if not saved:
        state = 'not_configured'
    elif ver == 'invalid' or st.get('auth') == 'needs_login':
        state = 'needs_login' if st.get('backfilled') else 'not_configured'
    elif ver != 'valid' and not st.get('backfilled'):
        state = 'not_configured'
    elif st.get('syncing') and _ago(st.get('syncing')) < 7200:
        state = 'syncing'
    elif (st.get('rate_limited_until') or 0) > time.time():
        state = 'rate_limited'
    else:
        state = 'connected'
    counts, first, last, ndays = _counts()
    nxt = None
    if state in ('connected', 'rate_limited') and st.get('last_sync'):
        nxt = _iso(_parse(st['last_sync']) + dt.timedelta(minutes=SYNC_MINUTES))
    left = _days_left(k) if saved else None
    return {
        'contract': CONTRACT, 'source': SOURCE, 'status': state, 'version': VERSION,
        'account': None, 'dataset': _dataset(),
        'last_sync': st.get('last_sync'), 'next_sync': nxt, 'sync_every_minutes': SYNC_MINUTES,
        'history_from': first, 'latest_date': last,
        'counts': {'days': ndays, 'sleeps': counts.get('sleep', 0), 'workouts': counts.get('activity', 0)},
        'error': st.get('last_error'), 'scales': SCALES, 'provides': {'readiness': True},
        # Garmin setup state, for this app's own screen
        'setup': {'key_saved': saved, 'key_verified': ver, 'key_expires_about': k.get('expires_about') if saved else None,
                  'key_days_left': left, 'key_expiring': left is not None and left <= 7,
                  'backfilled': bool(st.get('backfilled')),
                  'sync_kind': st.get('sync_kind') if state == 'syncing' else None,
                  'progress': st.get('progress') if state == 'syncing' else None},
    }


def disconnect(forget=False):
    with LOCK:
        _save_tokens(None)
        for p in list(DATA.glob('garmin.sqlite3*')):
            p.unlink(missing_ok=True)
        _shred('state.json')     # also drops the dataset id, so readers know the copy is gone
        if forget:
            _shred('key.json')
        else:
            _set_key(verified=None)   # keep the key, but stop syncing until the owner tests it again
    return status_info()


# ---------------- MCP ----------------

_NONE = {'type': 'object', 'properties': {}, 'additionalProperties': False}
TOOLS = [
    {'name': 'health_data_status', 'description': "Garmin connector status (contract wizard.health-data/1): not_configured, needs_login (personal API key expired or deleted), syncing, connected or rate_limited; last/next sync, history span, record counts and roughly when the key runs out. Never returns the key or tokens. If not connected, send the owner the Garmin app link [Garmin](/wizard-garmin/); its own screen walks him through creating a personal API key on Garmin Connect. Never ask for the Garmin key or password in chat. For health questions use the Health app's tools instead.",
     'inputSchema': _NONE, 'annotations': {'readOnlyHint': True}},
    {'name': 'health_data_sync', 'description': 'Ask the Garmin connector to pull new data from Garmin now (it also syncs every 20 minutes). Returns at once; check health_data_status for progress. Reads Garmin only.',
     'inputSchema': _NONE, 'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True}},
    {'name': 'health_data_days', 'description': 'Raw contract read: normalized Garmin day records (readiness = Body Battery at wake, overnight HRV and HRV status, resting HR, sleep score and stages, intensity minutes as the load, steps, calories, activities, extras.garmin), oldest first, paged with next_page; pass changed_since=<cursor> to get only changed days. For owner questions prefer the Health app tools.',
     'inputSchema': {'type': 'object', 'properties': {
         'from': {'type': 'string', 'description': 'YYYY-MM-DD inclusive'}, 'to': {'type': 'string', 'description': 'YYYY-MM-DD inclusive'},
         'limit': {'type': 'integer', 'description': 'days per page, default 100, max 500'},
         'page': {'type': 'string', 'description': 'next_page from the previous reply'},
         'changed_since': {'type': 'string', 'description': 'cursor from an earlier reply; empty string = everything'}},
         'additionalProperties': False}, 'annotations': {'readOnlyHint': True}},
]


def request_sync(full=False):
    s = status_info()
    if s['status'] in ('not_configured', 'needs_login'):
        raise Problem(409, 'Garmin is not connected. The owner finishes setup in the Garmin app (/wizard-garmin/).')
    start_sync_thread(full=full)
    return dict(status_info(), note='Sync started.')


def call(name, a):
    if name == 'health_data_status':
        return status_info()
    if name == 'health_data_sync':
        return request_sync()
    if name == 'health_data_days':
        return contract_days(a)
    raise KeyError(name)


def rpc(msg):
    if not isinstance(msg, dict):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Expected a JSON-RPC object.'}}
    m, i, p = msg.get('method'), msg.get('id'), msg.get('params') or {}
    if 'id' not in msg:
        return None
    try:
        if m == 'initialize':
            r = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': APP_ID, 'version': VERSION}}
        elif m == 'tools/list':
            r = {'tools': TOOLS}
        elif m == 'tools/call':
            try:
                v = call(p.get('name'), p.get('arguments') or {})
            except KeyError:
                return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32602, 'message': 'Unknown tool.'}}
            r = {'content': [{'type': 'text', 'text': json.dumps(v, ensure_ascii=False)}], 'structuredContent': v, 'isError': False}
        elif m == 'ping':
            r = {}
        else:
            return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32601, 'message': 'Unknown method.'}}
    except (Problem, ProviderError) as e:
        r = {'content': [{'type': 'text', 'text': e.message}], 'isError': True}
    except (ValueError, TypeError, AttributeError):
        r = {'content': [{'type': 'text', 'text': 'Invalid arguments.'}], 'isError': True}
    return {'jsonrpc': '2.0', 'id': i, 'result': r}


# ---------------- HTTP ----------------

def _csv(name):
    return {x.strip().lower() for x in os.environ.get(name, '').split(',') if x.strip()}


_LOOPBACK = {f'127.0.0.1:{PORT}', f'localhost:{PORT}', f'{APP_ID}:{PORT}'}
TRUSTED_HOSTS = _csv('WIZARD_TRUSTED_HOSTS') | _LOOPBACK
TRUSTED_ORIGINS = _csv('WIZARD_TRUSTED_ORIGINS') | {'http://' + h for h in _LOOPBACK}
# A bare host name (no port) in WIZARD_TRUSTED_HOSTS is trusted on every shell port of that host.
TRUSTED_NAMES = {h.split(':')[0] for h in TRUSTED_HOSTS}
CSP = ("default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
       "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")


def _host_ok(host):
    host = (host or '').lower()
    return host in TRUSTED_HOSTS or host.split(':')[0] in TRUSTED_NAMES


class Handler(BaseHTTPRequestHandler):
    def refused(self, write=False):
        host = (self.headers.get('Host') or '').lower()
        if not _host_ok(host):
            self.send(403, {'error': 'Unknown host.'}); return True
        origin = (self.headers.get('Origin') or '').lower()
        if origin and origin not in TRUSTED_ORIGINS and urlsplit(origin).netloc != host:
            self.send(403, {'error': 'Cross-origin request refused.'}); return True
        if write and not origin and (self.headers.get('Sec-Fetch-Site') or '') == 'cross-site':
            self.send(403, {'error': 'Cross-origin request refused.'}); return True
        return False

    def forwarded(self):
        return any(self.headers.get(h) for h in FORWARDED)

    def send(self, code, body=None, kind='application/json', extra=None):
        b = body if isinstance(body, bytes) else b'' if body is None else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(b)

    def route(self):
        p = urlsplit(self.path).path
        return '/' if p == PREFIX else p[len(PREFIX):] if p.startswith(PREFIX + '/') else p

    def body(self):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise Problem(415, 'Use application/json.')
        size = int(self.headers.get('Content-Length', '0') or 0)
        if not 0 <= size <= 65536:
            raise Problem(413, 'Request too large.')
        try:
            d = json.loads(self.rfile.read(size) or b'{}')
        except ValueError:
            raise Problem(400, 'Invalid JSON.') from None
        if not isinstance(d, dict):
            raise Problem(400, 'Expected a JSON object.')
        return d

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.refused():
            return
        path = self.route()
        query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query, keep_blank_values=True).items()}
        try:
            if path in ('/healthz', '/api/health'):
                s = status_info()
                return self.send(200, {'ok': True, 'app': APP_ID, 'version': VERSION, 'status': s['status'],
                                       'connected': s['status'] in ('connected', 'syncing', 'rate_limited'), 'last_sync': s['last_sync']})
            if path == '/api/status':
                return self.send(200, status_info())
            if path == '/api/setup':
                return self.send(200, setup_info())
            if path == '/api/days':
                if self.forwarded():
                    return self.send(403, {'error': 'Health data reads are box-only.'})
                return self.send(200, contract_days(query))
            if path == '/openapi.json':
                return self.send(200, (ROOT / 'openapi.json').read_bytes())
            name = 'index.html' if path == '/' else path[1:]
            kind = STATIC.get(Path(name).suffix)
            if kind and '/' not in name and not name.startswith('.') and (ROOT / 'web' / name).is_file():
                return self.send(200, (ROOT / 'web' / name).read_bytes(), kind)
            self.send(405 if path == '/mcp' else 404, {'error': 'Use POST /mcp.' if path == '/mcp' else 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except (ValueError, TypeError):
            self.send(400, {'error': 'Invalid query.'})

    def do_POST(self):
        if self.refused(write=True):
            return
        path = self.route()
        try:
            if path == '/mcp':
                if self.forwarded():
                    return self.send(403, {'error': 'MCP is box-only.'})
                out = rpc(self.body())
                return self.send(202 if out is None else 200, out)
            b = self.body()
            if path == '/api/key':
                return self.send(200, save_key(b))
            if path == '/api/key/test':
                return self.send(200, test_key())
            if path == '/api/key/forget':
                return self.send(200, forget_key())
            if path == '/api/sync':
                return self.send(202, request_sync(full=bool(b.get('full'))))
            if path == '/api/disconnect':
                return self.send(200, disconnect(forget=bool(b.get('forget_key'))))
            self.send(404, {'error': 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except ProviderError as e:
            self.send(429 if e.code == 'rate_limited' else 502, {'error': e.message})
        except (ValueError, TypeError):
            self.send(400, {'error': 'Invalid request.'})

    def log_message(self, *a):
        pass


def main():
    secure_files()
    st = _state()
    if st.get('syncing'):
        _update_state(syncing=None, progress=None, sync_kind=None)   # a restart interrupted it; the scheduler resumes
    threading.Thread(target=_scheduler, daemon=True).start()
    ThreadingHTTPServer((os.environ.get('BIND', '0.0.0.0'), int(PORT)), Handler).serve_forever()


if __name__ == '__main__':
    main()
