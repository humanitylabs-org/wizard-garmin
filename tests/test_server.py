"""Tests for the Garmin connector against a fake Garmin Connect Personal API built from Garmin's official sources:
the Personal API Swagger (OpenAPI 3.0.1, developer.garmin.com/gc-developer-program/personal/swagger-docs/: paths,
schemas and their `example` values) and Garmin's own client github.com/garmin/connect-personal-api-python
(POST <token path> with `Authorization: ApiKey <key>` -> a raw JWT body, 24 h, no refresh tokens). Covers the key
test, token reuse and renewal, 401 = key ended, windowed paging (31-day sleep/HRV windows, one-year daily summaries,
one call per day for Body Battery at wake, paged activity search), the wizard.health-data/1 contract (normalized days,
paging, changed_since cursor, dataset id, box-only reads), the documented formulas, HTTP/MCP guards and disconnect.
Run: python3 -m unittest discover -s tests"""
import base64
import copy
import datetime as dt
import importlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
KEY = 'gcpk_TEST_0123456789abcdefABCDEF-key'
TODAY = dt.datetime.now(dt.timezone.utc).date()
DAYS = 40

# ---- sample payloads: field names and example values from the Personal API OpenAPI schemas ----
DOC_SUMMARY_V2 = {  # UserDailySummaryV2DTO (examples from HeartRateV2DTO, IntensityMinutesV2DTO, BodyBatteryV2DTO, ...)
    'uuid': '6f1c', 'userProfilePk': 1, 'calendarDate': '2026-08-31', 'source': 'garmin', 'includesWellnessData': True,
    'includesActivityData': True,
    'wellnessChronology': {'startTimestampGmt': '2026-08-31T04:00:00.0', 'endTimestampGmt': '2026-09-01T03:59:00.0', 'totalDurationInMillis': 57600000},
    'movement': {'steps': {'goal': 10000, 'value': 5000, 'distanceInMeters': 3810}},
    'calories': {'burnedResting': 1000, 'burnedActive': 500, 'burnedTotal': 1500, 'consumedGoal': 2000, 'consumedValue': 1200},
    'heartRate': {'minValue': 60, 'maxValue': 180, 'restingValue': 100, 'alertCount': 0},
    'intensityMinutes': {'goal': 150, 'moderate': 30, 'vigorous': 15},
    'stress': {'avgLevel': 50, 'maxLevel': 100, 'qualifier': 'very_stressful'},
    'bodyBattery': {'minValue': 10, 'maxValue': 85, 'chargedValue': 80, 'drainedValue': 20, 'latestValue': 60, 'endOfDayValue': 20},
    'respiration': {'avgValue': 15, 'maxValue': 20, 'minValue': 10}, 'pulseOx': {'avgValue': 95, 'minValue': 90}}
DOC_SLEEP = {  # DailySleepDTO + SleepScoreItem + SleepNeedDTO + DailyNapDTO
    'id': 1, 'userProfilePK': 1, 'calendarDate': '2026-08-31', 'sleepTimeSeconds': 25200, 'napTimeSeconds': 1200,
    'sleepWindowConfirmed': True, 'sleepWindowConfirmationType': 'enhanced_confirmed_final',
    'sleepStartTimestampGMT': 1788214800000, 'sleepEndTimestampGMT': 1788243600000,
    'deepSleepSeconds': 5400, 'lightSleepSeconds': 13800, 'remSleepSeconds': 6000, 'awakeSleepSeconds': 3600,
    'unmeasurableSleepSeconds': 0, 'deviceRemCapable': True, 'averageRespirationValue': 14.5, 'avgHeartRate': 52.0,
    'awakeCount': 2, 'avgSleepStress': 18.0, 'sleepQualityTypePK': 'good', 'sleepScoreFeedback': 'POSITIVE_LONG_AND_DEEP',
    'sleepScores': {'overall': {'value': 83, 'qualifierKey': 'GOOD'}, 'remPercentage': {'value': 24, 'qualifierKey': 'EXCELLENT'}},
    'sleepNeed': {'calendarDate': '2026-08-31', 'baseline': 480, 'actual': 490},
    'dailyNapDTOS': [{'calendarDate': '2026-08-31', 'napTimeSec': 1200, 'napStartTimestampGMT': '2026-08-31T07:00:00.0',
                      'napEndTimestampGMT': '2026-08-31T07:20:00.0', 'napSource': 0}]}
DOC_HRV = {'calendarDate': '2022-08-10', 'weeklyAvg': 62, 'lastNightAvg': 58, 'lastNight5MinHigh': 81,
           'baseline': {'lowUpper': 48, 'balancedLow': 53, 'balancedUpper': 71, 'markerValue': 0.42},
           'status': 'BALANCED', 'feedbackPhrase': 'HRV_BALANCED_1', 'createTimeStamp': '2022-08-10T09:47:59.031'}
DOC_USER_SUMMARY = {'calendarDate': '2026-08-31', 'totalSteps': 5000, 'restingHeartRate': 52, 'lastSevenDaysAvgRestingHeartRate': 53,
                    'bodyBatteryAtWakeTime': 74, 'bodyBatteryDuringSleep': 51, 'bodyBatteryHighestValue': 85,
                    'bodyBatteryLowestValue': 10, 'bodyBatteryVersion': 2.0, 'moderateIntensityMinutes': 30,
                    'vigorousIntensityMinutes': 15}
DOC_ACTIVITY = {  # SummarizedActivityDTO (examples from the schema)
    'activityId': 21345678901, 'activityName': 'Morning Run', 'startTimeLocal': '2026-08-31 17:01:18',
    'startTimeGMT': '2026-08-31 22:01:18', 'endTimeGMT': '2026-08-31 22:41:22', 'beginTimestamp': 1788213678000,
    'activityType': {'typeId': 1, 'typeKey': 'running', 'parentTypeId': 17}, 'distance': 7210.0,
    'duration': 2400.931884765625, 'calories': 512.0, 'averageHR': 148.0, 'maxHR': 176.0, 'activityTrainingLoad': 92.4,
    'aerobicTrainingEffect': 3.1, 'anaerobicTrainingEffect': 1.2, 'trainingEffectLabel': 'TEMPO',
    'moderateIntensityMinutes': 10, 'vigorousIntensityMinutes': 25, 'differenceBodyBattery': -11,
    'hrTimeInZone_1': 6.001, 'hrTimeInZone_2': 30, 'hrTimeInZone_3': 793.001, 'hrTimeInZone_4': 1120.344, 'hrTimeInZone_5': 446.603,
    'ownerFullName': 'Someone Else', 'startLatitude': 13.7, 'startLongitude': 100.5}


def day(i):
    return (TODAY - dt.timedelta(days=i)).isoformat()


def jwt(exp):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip('=')
    return enc({'alg': 'RS256'}) + '.' + enc({'exp': exp, 'sub': 'user'}) + '.sig'


def ms(iso):
    return int(dt.datetime.fromisoformat(iso).timestamp() * 1000)


def make_data():
    D = {'summary': {}, 'sleep': {}, 'hrv': {}, 'wake': {}, 'activity': []}
    for i in range(DAYS):
        d = day(i)
        su = copy.deepcopy(DOC_SUMMARY_V2)
        su['calendarDate'] = d
        su['heartRate']['restingValue'] = 50 + i % 5
        su['intensityMinutes'].update(moderate=10 + i % 7, vigorous=i % 3)
        su['movement']['steps']['value'] = 6000 + 100 * i
        D['summary'][d] = su
        if i < 30:
            prev = day(i + 1)
            D['sleep'][d] = dict(copy.deepcopy(DOC_SLEEP), calendarDate=d, sleepStartTimestampGMT=ms(prev + 'T16:00:00+00:00'),
                                 sleepEndTimestampGMT=ms(d + 'T00:00:00+00:00'),
                                 sleepScores={'overall': {'value': 60 + i % 30, 'qualifierKey': 'FAIR'}})
            D['hrv'][d] = dict(DOC_HRV, calendarDate=d, lastNightAvg=50 + i % 9)
            D['wake'][d] = dict(DOC_USER_SUMMARY, calendarDate=d, bodyBatteryAtWakeTime=20 + (i * 7) % 80)
    for i in range(0, 30, 3):
        d = day(i)
        D['activity'].append(dict(DOC_ACTIVITY, activityId=1000 + i, startTimeLocal=d + ' 18:00:00', startTimeGMT=d + ' 11:00:00',
                                  endTimeGMT=d + ' 11:40:00'))
    return D


class Fake:
    def __init__(self):
        self.data = make_data()
        self.tokens = set()
        self.calls = []
        self.token_calls = 0
        self.key_dead = False
        self.force_401 = 0
        self.force_429 = 0


FAKE = Fake()


class FakeGarmin(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body=None, headers=None, raw=None):
        b = raw if raw is not None else (b'' if body is None else json.dumps(body).encode())
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain' if raw is not None else 'application/json')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        u = urlsplit(self.path)
        if u.path == '/personalapitoken-service/personal-api/personal-api-key/token':
            FAKE.token_calls += 1
            auth = self.headers.get('Authorization') or ''
            if not auth:
                return self.reply(400)                      # what the real endpoint answers without a header
            if auth != 'ApiKey ' + KEY or FAKE.key_dead:
                return self.reply(401, headers={'WWW-Authenticate': 'Bearer'})   # what the real endpoint answers for a bad key
            tok = jwt(int(time.time()) + 24 * 3600)
            FAKE.tokens.add(tok)
            return self.reply(200, raw=tok.encode())
        self.reply(404)

    def do_GET(self):
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        FAKE.calls.append((u.path, q))
        tok = (self.headers.get('Authorization') or '')[7:]
        if tok not in FAKE.tokens:
            return self.reply(401)
        if FAKE.force_401:
            FAKE.force_401 -= 1
            FAKE.tokens.discard(tok)
            return self.reply(401)
        if FAKE.force_429:
            FAKE.force_429 -= 1
            return self.reply(429, headers={'Retry-After': '0'})
        D = FAKE.data

        def span(a, b, maxdays):
            a, b = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
            if b < a or (b - a).days + 1 > maxdays:
                raise ValueError
            return [(a + dt.timedelta(days=k)).isoformat() for k in range((b - a).days + 1)]
        try:
            if u.path == '/usersummary-service/v2/dailysummaries':
                return self.reply(200, {'data': [D['summary'][s] for s in span(q['startDate'], q['endDate'], 366) if s in D['summary']]})
            if u.path == '/sleep-service/sleep/dailySleepSummariesByDate':
                # a day without sleep still comes back as a bare record (no sleep time)
                return self.reply(200, [D['sleep'].get(s) or {'calendarDate': s, 'sleepTimeSeconds': None}
                                        for s in span(q['startDate'], q['endDate'], 31)])
            if u.path.startswith('/hrv-service/hrv/daily/'):
                a, b = u.path.split('/')[-2:]
                rows = [D['hrv'][s] for s in span(a, b, 31) if s in D['hrv']]
                return self.reply(200, {'hrvSummaries': rows, 'userProfilePk': 1}) if rows else self.reply(204)
            if u.path == '/usersummary-service/usersummary/daily':
                w = D['wake'].get(q['calendarDate'])
                return self.reply(200, w or {'calendarDate': q['calendarDate'], 'totalSteps': 0})
            if u.path == '/activitylist-service/activities/search/activities':
                a, b = q['startDate'], q['endDate']
                rows = sorted([x for x in D['activity'] if a <= x['startTimeLocal'][:10] <= b], key=lambda x: x['startTimeLocal'])
                st, lim = int(q.get('start', 0)), int(q.get('limit', 99))
                return self.reply(200, rows[st:st + lim])
        except (KeyError, ValueError):
            return self.reply(400)
        self.reply(404)


def http(port, method, path, body=None, headers=None):
    h = {'Host': f'127.0.0.1:{port}'}
    if body is not None:
        h['Content-Type'] = 'application/json'
    h.update(headers or {})
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class GarminTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = ThreadingHTTPServer(('127.0.0.1', 0), FakeGarmin)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        fp = cls.fake.server_address[1]
        cls.tmp = tempfile.mkdtemp()
        os.environ.update({'DATA_DIR': cls.tmp + '/private', 'GARMIN_API_BASE': f'http://127.0.0.1:{fp}', 'GARMIN_REQUEST_GAP': '0',
                           'GARMIN_BACKOFF': '0.01', 'GARMIN_ACTIVITY_PAGE': '4', 'PORT': '18997', 'WIZARD_TRUSTED_HOSTS': 'box.example.ts.net'})
        sys.path.insert(0, str(ROOT / 'app'))
        import server
        cls.S = importlib.reload(server)
        cls.app = ThreadingHTTPServer(('127.0.0.1', 0), cls.S.Handler)
        threading.Thread(target=cls.app.serve_forever, daemon=True).start()
        cls.port = cls.app.server_address[1]
        cls.S.TRUSTED_HOSTS.add(f'127.0.0.1:{cls.port}')

    @classmethod
    def tearDownClass(cls):
        cls.fake.shutdown()
        cls.app.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def files_text(self):
        return ''.join(p.read_bytes().decode('latin-1') for p in Path(self.tmp).rglob('*') if p.is_file())

    def wait_sync(self, timeout=30):
        t = time.time()
        while time.time() - t < timeout:
            if not self.S.SYNC_LOCK.locked() and self.S._state().get('last_sync'):
                time.sleep(0.05)
                if not self.S.SYNC_LOCK.locked():
                    return
            time.sleep(0.05)
        self.fail('sync did not finish')

    def test_flow(self):
        S = self.S
        self.assertEqual(S.status_info()['status'], 'not_configured')
        info = S.setup_info()
        self.assertEqual(info['key_page_url'], 'https://connect.garmin.com/app/settings/personalApiToken')
        self.assertEqual(info['expiry_choices'], [30, 90, 180, 365])
        with self.assertRaises(S.Problem):
            S.test_key()

        # ---- step 4: key through the HTTP API the form uses; never echoed; a wrong key is 'invalid'
        self.assertEqual(http(self.port, 'POST', '/api/key', {'api_key': 'short'})[0], 400)
        self.assertEqual(http(self.port, 'POST', '/api/key', {'api_key': KEY, 'expiry_days': 45})[0], 400)
        http(self.port, 'POST', '/api/key', {'api_key': 'not-the-right-key-0000000000', 'expiry_days': 90})
        code, body = http(self.port, 'POST', '/api/key/test', {})
        self.assertEqual(json.loads(body)['result'], 'invalid', body)
        self.assertEqual(S.status_info()['status'], 'not_configured')
        self.assertFalse(S.SYNC_LOCK.locked())
        FAKE.calls.clear()
        code, body = http(self.port, 'POST', '/api/key', {'api_key': '  ApiKey ' + KEY + ' ', 'expiry_days': 365})
        self.assertEqual(code, 200)
        self.assertNotIn(KEY.encode(), body)
        self.assertEqual(json.loads(body)['key_hint'], '••••••-key')
        self.assertEqual(oct(os.stat(S.DATA / 'key.json').st_mode & 0o777), '0o600')
        self.assertEqual(oct(os.stat(S.DATA).st_mode & 0o777), '0o700')
        code, body = http(self.port, 'POST', '/api/key/test', {})
        self.assertEqual(json.loads(body)['result'], 'valid', body)
        self.wait_sync()

        st = S.status_info()
        self.assertEqual(st['status'], 'connected', st)
        self.assertEqual(st['source'], {'id': 'wizard-garmin', 'provider': 'garmin', 'name': 'Garmin', 'app_path': '/wizard-garmin/'})
        self.assertEqual(st['scales']['load']['scale_max'], 60)
        self.assertEqual(st['scales']['load']['targets'], {'high': [21, 42], 'medium': [10, 21], 'low': [0, 10]})
        self.assertIsNone(st['scales']['load']['max'])
        self.assertEqual(st['scales']['readiness']['label'], 'Body Battery at wake')
        self.assertEqual(st['counts'], {'days': DAYS, 'sleeps': 30, 'workouts': 10}, st)
        self.assertEqual(st['setup']['key_days_left'], 365)
        self.assertFalse(st['setup']['key_expiring'])
        self.assertEqual(oct(os.stat(S.DATA / 'tokens.json').st_mode & 0o777), '0o600')
        # paging: one daily-summary call for the year, 31-day sleep/HRV windows, one wake call per day for 90 days
        summ = [q for p, q in FAKE.calls if p == '/usersummary-service/v2/dailysummaries']
        self.assertLessEqual(len(summ), 2)       # one year per call; 'today' may already be tomorrow in UTC+14
        self.assertEqual(summ[0]['startDate'], (TODAY - dt.timedelta(days=364)).isoformat())
        self.assertTrue(all((dt.date.fromisoformat(q['endDate']) - dt.date.fromisoformat(q['startDate'])).days <= 364 for q in summ))
        sl = [(dt.date.fromisoformat(q['startDate']), dt.date.fromisoformat(q['endDate'])) for p, q in FAKE.calls if p == '/sleep-service/sleep/dailySleepSummariesByDate']
        self.assertTrue(all((b - a).days <= 30 for a, b in sl))
        self.assertTrue(all(sl[k + 1][0] == sl[k][1] + dt.timedelta(days=1) for k in range(len(sl) - 1)))
        self.assertEqual(len([p for p, q in FAKE.calls if p.startswith('/hrv-service/hrv/daily/')]), len(sl))
        wake = [q['calendarDate'] for p, q in FAKE.calls if p == '/usersummary-service/usersummary/daily']
        self.assertEqual(len(wake), S.WAKE_DAYS)
        acts = [q for p, q in FAKE.calls if p == '/activitylist-service/activities/search/activities']
        self.assertEqual([int(q['start']) for q in acts], [0, 4, 8])        # 10 activities, pages of 4
        self.assertEqual(FAKE.token_calls, 2, 'one call for the bad key test, one for the good key, then reused')

        # ---- the contract: a complete day
        d0 = day(0)
        r = S.contract_days({'from': d0, 'to': d0})
        self.assertEqual((r['contract'], r['dataset']), ('wizard.health-data/1', st['dataset']))
        d = r['days'][0]
        check_day(self, d)
        self.assertEqual(d['readiness'], {'score': 20, 'band': 'low', 'state': 'scored', 'calibrating': False})
        self.assertEqual((d['hrv_ms'], d['resting_hr_bpm'], d['spo2_pct'], d['respiratory_rate']), (50, 50, 95, 14.5))
        s = d['sleep']
        self.assertEqual((s['asleep_h'], s['in_bed_h'], s['deep_h'], s['light_h'], s['rem_h'], s['awake_h']), (7.0, 8.0, 1.5, 3.83, 1.67, 1.0))
        self.assertEqual((s['score'], s['need_h'], s['efficiency_pct']), (60, 8.17, 88))
        self.assertEqual(s['end'], d0 + 'T00:00:00.000Z')
        self.assertEqual(s['naps'][0]['asleep_h'], 0.33)
        self.assertEqual(d['load']['value'], 10)                   # 10 moderate + 2 x 0 vigorous
        self.assertEqual((d['load']['max'], d['load']['unit'], d['load']['label'], d['load']['scale_max']), (None, 'min', 'Intensity minutes', 60))
        self.assertEqual((d['steps'], d['calories_kcal']), (6000, 1500))
        w = d['workouts'][0]
        self.assertEqual((w['sport'], w['minutes'], w['avg_hr_bpm'], w['distance_m'], w['calories_kcal']), ('Running', 40, 148, 7210, 512))
        self.assertEqual(w['start'], d0 + 'T11:00:00.000Z')
        self.assertEqual(w['load']['value'], 60)                   # 10 + 2 x 25
        g = d['extras']['garmin']
        self.assertEqual((g['hrv_status'], g['hrv_balanced_range_ms'], g['body_battery_at_wake']), ('BALANCED', [53, 71], 20))
        self.assertEqual(g['training_load_activities'], 92.4)
        self.assertNotIn('Someone Else', json.dumps(d))           # owner names and places are not copied
        # day 35: daily summary only (no sleep, HRV or Body Battery at wake): readiness null, never derived
        old = S.contract_days({'from': day(35), 'to': day(35)})['days'][0]
        self.assertIsNone(old['readiness'])
        self.assertIsNone(old['sleep'])
        self.assertIsNone(old['hrv_ms'])
        self.assertEqual(old['resting_hr_bpm'], 50)
        with self.assertRaises(S.Problem):
            S.contract_days({'from': 'yesterday'})
        with self.assertRaises(S.Problem):
            S.contract_days({'changed_since': 'p1.3'})

        # paging: pages of 15, oldest first, no gaps or repeats, same cursor on every page
        seen, page, cursors = [], None, set()
        while True:
            r = S.contract_days(dict({'limit': 15}, **({'page': page} if page else {})))
            seen += [x['date'] for x in r['days']]
            cursors.add(r['cursor'])
            page = r['next_page']
            if not page:
                break
        self.assertEqual(len(seen), DAYS)
        self.assertEqual(seen, sorted(set(seen)))
        self.assertEqual(len(cursors), 1)
        cursor = cursors.pop()

        # changed_since: an unchanged re-sync emits nothing; a re-scored night and a new activity do
        S.sync(full=True)
        self.assertEqual(S.contract_days({'changed_since': cursor})['days'], [], 'an unchanged re-sync must not re-emit days')
        FAKE.data['sleep'][day(3)]['sleepScores'] = {'overall': {'value': 41}}
        FAKE.data['activity'].append(dict(DOC_ACTIVITY, activityId=999, startTimeLocal=day(1) + ' 07:00:00', startTimeGMT=day(1) + ' 00:00:00',
                                          endTimeGMT=day(1) + ' 00:30:00', activityType={'typeKey': 'cycling'}))
        S.sync()
        ch = S.contract_days({'changed_since': cursor})
        self.assertEqual([x['date'] for x in ch['days']], [day(3), day(1)])
        self.assertEqual(ch['days'][0]['sleep']['score'], 41)
        self.assertIn('Cycling', [x['sport'] for x in ch['days'][1]['workouts']])
        self.assertEqual(S.contract_days({'changed_since': ch['cursor']})['days'], [])

        # incremental sync asks from last sync minus the overlap; a 429 is retried; the token is reused
        FAKE.calls.clear()
        FAKE.force_429 = 1
        before = FAKE.token_calls
        S.sync()
        summ = [q for p, q in FAKE.calls if p == '/usersummary-service/v2/dailysummaries']
        self.assertEqual(summ[-1]['startDate'], (TODAY - dt.timedelta(days=S.OVERLAP_DAYS)).isoformat())
        self.assertLessEqual(len([p for p, q in FAKE.calls if p == '/usersummary-service/usersummary/daily']), S.OVERLAP_DAYS + 2)
        self.assertEqual(FAKE.token_calls, before)

        # a token that ends early is renewed with the key once, without bothering the owner
        FAKE.force_401 = 1
        S.sync()
        self.assertEqual(FAKE.token_calls, before + 1)
        self.assertEqual(S.status_info()['status'], 'connected')

        # ---- HTTP + MCP
        code, body = http(self.port, 'POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        self.assertEqual([t['name'] for t in json.loads(body)['result']['tools']], ['health_data_status', 'health_data_sync', 'health_data_days'])
        tok = next(iter(FAKE.tokens))
        for name, args in [('health_data_status', {}), ('health_data_days', {'from': d0}), ('health_data_days', {'changed_since': ''})]:
            code, body = http(self.port, 'POST', '/mcp', {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}})
            res = json.loads(body)['result']
            self.assertFalse(res['isError'], (name, res))
            self.assertNotIn(KEY, body.decode())
        self.assertEqual(http(self.port, 'POST', '/mcp', {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'}, {'Tailscale-User-Login': 'a@b'})[0], 403)
        self.assertEqual(http(self.port, 'GET', '/api/status', headers={'Host': 'evil.example:18997'})[0], 403)
        self.assertEqual(http(self.port, 'GET', '/api/status', headers={'Host': 'box.example.ts.net:8444'})[0], 200)
        self.assertEqual(http(self.port, 'POST', '/api/sync', {}, {'Host': 'box.example.ts.net:8444', 'Origin': 'https://evil.example'})[0], 403)
        for path in ('/api/status', '/api/setup', '/api/days', '/api/days?changed_since=', '/wizard-garmin/api/status', '/healthz', '/', '/icon.svg'):
            code, body = http(self.port, 'GET', path)
            self.assertEqual(code, 200, path)
            for secret in [KEY] + list(FAKE.tokens):
                self.assertNotIn(secret, body.decode('latin-1'), path)
        for hdr in ('X-Forwarded-For', 'Tailscale-User-Login', 'Forwarded', 'X-Real-IP', 'CF-Connecting-IP'):
            self.assertEqual(http(self.port, 'GET', '/api/days', headers={hdr: 'x'})[0], 403, hdr)

        # ---- the key is deleted or runs out on Garmin: needs_login, data kept, scheduler stops
        while S.SYNC_LOCK.locked():
            time.sleep(0.05)
        FAKE.key_dead = True
        S._save_tokens(None)
        with self.assertRaises(S.Problem):
            S.sync()
        st = S.status_info()
        self.assertEqual(st['status'], 'needs_login')
        self.assertIn('new key', st['error'])
        self.assertEqual(st['counts']['days'], DAYS)
        with self.assertRaises(S.Problem):
            S.request_sync()
        FAKE.key_dead = False
        self.assertEqual(S.test_key()['result'], 'valid')
        self.wait_sync()
        while S.SYNC_LOCK.locked():
            time.sleep(0.05)
        self.assertEqual(S.status_info()['status'], 'connected')
        ds = S.status_info()['dataset']

        # ---- key close to its end: status warns
        S._set_key(expires_about=(TODAY + dt.timedelta(days=5)).isoformat())
        self.assertTrue(S.status_info()['setup']['key_expiring'])

        # ---- disconnect wipes the copy; forget wipes the key too
        code, body = http(self.port, 'POST', '/api/disconnect', {'forget_key': True})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['status'], 'not_configured')
        blob = self.files_text()
        self.assertNotIn(KEY, blob)
        for t in FAKE.tokens:
            self.assertNotIn(t, blob)
        st = S.status_info()
        self.assertEqual(st['counts'], {'days': 0, 'sleeps': 0, 'workouts': 0})
        self.assertNotEqual(st['dataset'], ds)


class MappingTest(unittest.TestCase):
    """The documented formulas and the exact sample payloads from Garmin's Personal API schemas."""
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / 'app'))
        import server
        cls.S = server

    def test_intensity_minutes(self):
        S = self.S
        self.assertEqual(S.intensity_minutes(30, 15), 60)        # IntensityMinutesV2DTO examples
        self.assertEqual(S.intensity_minutes(0, 0), 0)
        self.assertEqual(S.intensity_minutes(None, 5), 10)
        self.assertIsNone(S.intensity_minutes(None, None))
        self.assertEqual(S.im_targets(150), {'high': [21, 42], 'medium': [10, 21], 'low': [0, 10]})
        self.assertEqual(S.im_targets(300), {'high': [43, 86], 'medium': [10, 43], 'low': [0, 10]})
        self.assertEqual(S.im_targets(None), S.im_targets(150))

    def test_readiness_is_body_battery_unchanged(self):
        S = self.S
        for v, band in ((0, 'low'), (33, 'low'), (34, 'medium'), (66, 'medium'), (67, 'high'), (100, 'high')):
            self.assertEqual(S.readiness_from({'bodyBatteryAtWakeTime': v}), {'score': v, 'band': band, 'state': 'scored', 'calibrating': False})
        self.assertIsNone(S.readiness_from({}))
        self.assertIsNone(S.readiness_from({'bodyBatteryAtWakeTime': None}))
        self.assertIsNone(S.readiness_from({'bodyBatteryAtWakeTime': 140}))
        self.assertIsNone(S.readiness_from(None))

    def test_doc_samples(self):
        S = self.S
        d = S._n_day('2026-08-31', {'summary': copy.deepcopy(DOC_SUMMARY_V2), 'sleep': copy.deepcopy(DOC_SLEEP), 'hrv': copy.deepcopy(DOC_HRV),
                                    'wake': copy.deepcopy(DOC_USER_SUMMARY), 'activity': [copy.deepcopy(DOC_ACTIVITY)]})
        check_day(self, d)
        self.assertEqual(d['readiness']['score'], 74)
        self.assertEqual((d['hrv_ms'], d['resting_hr_bpm'], d['max_hr_bpm'], d['spo2_pct']), (58, 100, 180, 95))
        self.assertEqual((d['steps'], d['calories_kcal'], d['load']['value']), (5000, 1500, 60))
        s = d['sleep']
        self.assertEqual((s['start'], s['end']), ('2026-08-31T22:20:00.000Z', '2026-09-01T06:20:00.000Z'))
        self.assertEqual((s['asleep_h'], s['in_bed_h'], s['score'], s['need_h'], s['respiratory_rate']), (7.0, 8.0, 83, 8.17, 14.5))
        w = d['workouts'][0]
        self.assertEqual((w['start'], w['end'], w['minutes']), ('2026-08-31T22:01:18.000Z', '2026-08-31T22:41:22.000Z', 40))
        self.assertEqual(w['zones_min'], [None, 0.1, 0.5, 13.2, 18.7, 7.4])
        self.assertEqual(w['extras']['garmin']['training_load'], 92.4)
        self.assertEqual(d['extras']['garmin']['stress_avg'], 50)
        self.assertEqual(d['updated_at'], '2026-09-01T06:20:00.000Z')   # the later of wellness end and sleep end
        # an empty day: no invented values
        e = S._n_day('2026-08-30', {})
        check_day(self, e)
        self.assertEqual([e[k] for k in ('readiness', 'sleep', 'load', 'hrv_ms', 'resting_hr_bpm', 'steps')], [None] * 6)

    def test_sleep_score_variants(self):
        S = self.S
        self.assertEqual(S.sleep_score({'sleepScores': {'overall': {'value': 91}}}), 91)
        self.assertEqual(S.sleep_score({'sleepScores': {'overallScore': {'value': 77}}}), 77)
        self.assertIsNone(S.sleep_score({'sleepScores': {'remPercentage': {'value': 24}}}))
        self.assertIsNone(S.sleep_score({}))

    def test_parse_timestamps(self):
        S = self.S
        self.assertEqual(S._iso(S._parse(1788214800000)), '2026-08-31T22:20:00.000Z')
        self.assertEqual(S._iso(S._parse('2026-08-31 22:01:18')), '2026-08-31T22:01:18.000Z')
        self.assertEqual(S._iso(S._parse('2026-08-31T04:00:00.0')), '2026-08-31T04:00:00.000Z')
        self.assertEqual(S._jwt_exp(jwt(1800000000)), 1800000000.0)
        self.assertIsNone(S._jwt_exp('not-a-jwt'))


DAY_KEYS = {'date', 'updated_at', 'readiness', 'hrv_ms', 'resting_hr_bpm', 'spo2_pct', 'skin_temp_c', 'skin_temp_delta_c', 'respiratory_rate',
            'sleep', 'load', 'calories_kcal', 'energy_kj', 'avg_hr_bpm', 'max_hr_bpm', 'steps', 'workouts', 'extras'}
SLEEP_KEYS = {'start', 'end', 'asleep_h', 'in_bed_h', 'awake_h', 'light_h', 'deep_h', 'rem_h', 'score', 'efficiency_pct', 'respiratory_rate', 'need_h', 'naps'}
WORKOUT_KEYS = {'id', 'sport', 'start', 'end', 'minutes', 'load', 'avg_hr_bpm', 'max_hr_bpm', 'calories_kcal', 'energy_kj', 'distance_m', 'zones_min', 'extras'}


def check_day(t, d):
    """The day record shape promised by docs/health-data-contract.md."""
    t.assertTrue(DAY_KEYS <= set(d), DAY_KEYS - set(d))
    t.assertRegex(d['date'], r'^\d{4}-\d{2}-\d{2}$')
    if d['readiness']:
        t.assertIn(d['readiness']['band'], ('high', 'medium', 'low'))
        t.assertIn(d['readiness']['state'], ('scored', 'pending', 'unscorable'))
        t.assertTrue(0 <= d['readiness']['score'] <= 100)
    if d['sleep']:
        t.assertTrue(SLEEP_KEYS <= set(d['sleep']), SLEEP_KEYS - set(d['sleep']))
    if d['load']:
        t.assertTrue({'value', 'max', 'label', 'scale_max', 'targets', 'over_margin', 'unit'} <= set(d['load']))
        t.assertGreaterEqual(d['load']['value'], 0)
    for w in d['workouts']:
        t.assertTrue(WORKOUT_KEYS <= set(w), WORKOUT_KEYS - set(w))
    t.assertEqual(set(d['extras']), {'garmin'})


class IconTest(unittest.TestCase):
    """Own icon: full-colour store square (icon.png from wizard.md) and a silhouette at web/icon.svg the shell tints.
    A generic round watch; never Garmin's logo, triangle or wordmark."""
    root = ROOT

    def test_icons(self):
        self.assertIn('\nicon: icon.png\n', (self.root / 'wizard.md').read_text())
        png = (self.root / 'icon.png').read_bytes()
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual((int.from_bytes(png[16:20], 'big'), int.from_bytes(png[20:24], 'big')), (1024, 1024))
        mask = (self.root / 'web' / 'icon.svg').read_text()
        self.assertIn('viewBox="0 0 512 512"', mask)
        self.assertNotRegex(mask, r'<text|<image|href=|<script')
        self.assertNotIn('garmin', (mask + (self.root / 'tools' / 'icon-source.svg').read_text()).lower())


if __name__ == '__main__':
    unittest.main()
