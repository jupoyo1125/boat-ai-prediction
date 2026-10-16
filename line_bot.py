"""Reply to explicit LINE requests with one frozen sample race per JST day.

No predictions are generated here. Disabled by default; the initial enabled
mode only answers the operator's test user. State id=3 is retained by the
existing storage migration. No user IDs, reply tokens or message bodies are
stored in the database or logs.
"""
import base64
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
import re
import unicodedata

import requests
from flask import jsonify, request

from state_store import (
    database_url, require_storage_writable, state_connection,
    storage_paused, strict_state_reads,
)

JST = timezone(timedelta(hours=9))
STATE_ID = 3
EXPECTED_BASIC_ID = '@295yklhl'
MIN_LEAD = timedelta(seconds=90)
MAX_BODY = 65536
MAX_EVENTS = 20
MAX_DAILY_EVENTS = 5000
LABELS = (('gachi', '🎯 ガチガチ', 10),
          ('roman', '🚀 ロマン砲', 10),
          ('oni', '👹 鬼しぼり', 3))
COMMANDS = {'買い目', '予想', '今日の予想', '本日の予想', '今日の買い目',
            '本日の買い目', 'お試し', '今日のお試し', 'サンプル',
            'ガチガチ', 'ロマン砲', '鬼しぼり',
            'ガチガチの買い目', 'ロマン砲の買い目', '鬼しぼりの買い目'}
HELP = ('Golden Racer｜無料お試し\n'
        '「買い目」と送ると、本日のお試し1レースの3タイプをお届けします。\n'
        '対象レースは運営が日付ごとに指定し、全員共通の1日1レースです。\n'
        '指定レースの予想が保存されるまで準備中となります。\n'
        '締切直前・締切後は配信を終了します。全レースの指定には対応しません。\n'
        '20歳以上の方向けの参考情報で、的中や利益を保証するものではありません。')


def now_jst():
    return datetime.now(JST)


@dataclass(frozen=True)
class Config:
    secret: str = field(repr=False)
    token: str = field(repr=False)
    mode: str = 'test'
    test_user: str = field(default='', repr=False)


def configuration():
    if os.getenv('BOAT_LINE_ENABLED') != '1' or storage_paused():
        return None
    secret = os.getenv('LINE_CHANNEL_SECRET', '').strip()
    token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
    mode = os.getenv('BOAT_LINE_MODE', 'test')
    test_user = os.getenv('LINE_TEST_USER_ID', '').strip()
    if not secret or not token or mode not in ('test', 'live') or not database_url():
        return None
    if mode == 'test' and not re.fullmatch(r'U[0-9a-fA-F]{32}', test_user):
        return None
    return Config(secret, token, mode, test_user)


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('timezone required')
    return parsed.astimezone(JST)


def designated_race(venues, now):
    """Read the operator's dated target; never infer a substitute race."""
    value = os.getenv('BOAT_LINE_SAMPLE_RACE', '').strip()
    match = re.fullmatch(r'([0-9]{8}):([0-9]{2}):(0?[1-9]|1[0-2])', value)
    if match is None:
        return None
    date, stadium, race = match.groups()
    if date != now.astimezone(JST).strftime('%Y%m%d') or stadium not in venues:
        return None
    race = int(race)
    return {'id': f'auto:{date}:{stadium}:{race:02d}', 'date': date,
            'stadium': stadium, 'race': race, 'venue': venues[stadium]}


def valid_signature(body, signature, secret):
    if not isinstance(signature, str) or len(signature) > 128:
        return False
    expected = base64.b64encode(
        hmac.new(secret.encode('utf-8'), body, hashlib.sha256).digest()
    ).decode('ascii')
    try:
        return hmac.compare_digest(expected, signature)
    except TypeError:
        return False


def frozen_sample(row, venues, now):
    """Validate saved input; never use results or re-run the prediction model."""
    try:
        date = now.astimezone(JST).strftime('%Y%m%d')
        race = row['race']
        stadium = row['stadium']
        if (isinstance(race, bool) or not isinstance(race, int)
                or not 1 <= race <= 12 or stadium not in venues
                or row.get('id') != f'auto:{date}:{stadium}:{race:02d}'
                or row.get('date') != date or row.get('performance_version') != 2
                or row.get('prediction_origin') != 'automatic'
                or row.get('settled') or row.get('learned') or row.get('actual_combo')):
            return None
        saved = timestamp(row['prediction_saved_at'])
        detected = timestamp(row['exhibition_detected_at'])
        deadline = timestamp(row['scheduled_deadline'])
        if (not detected <= saved <= now
                or saved >= deadline - timedelta(seconds=30)
                or saved.strftime('%Y%m%d') != date
                or deadline.strftime('%Y%m%d') != date):
            return None
        bets = row.get('bets')
        if not isinstance(bets, list) or not bets:
            return None
        groups = {category: [] for category, _, _ in LABELS}
        for bet in bets:
            category, combo = bet['category'], bet['bet']
            if (category not in groups or not isinstance(combo, str)
                    or not re.fullmatch(r'[1-6]-[1-6]-[1-6]', combo)
                    or len(set(combo.split('-'))) != 3 or combo in groups[category]):
                return None
            groups[category].append(combo)
        if any(len(groups[category]) > limit for category, _, limit in LABELS):
            return None
        return {
            'id': row['id'], 'date': date, 'stadium': stadium,
            'venue': venues[stadium], 'race': race, 'groups': groups,
            'saved_at': saved.isoformat(timespec='seconds'),
            'deadline': deadline.isoformat(timespec='seconds'),
        }
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def sample_label(sample):
    return f'{sample["venue"]}{sample["race"]}R'


def format_sample(sample):
    date = sample['date']
    lines = [f'【{int(date[4:6])}/{int(date[6:8])} {sample_label(sample)}｜無料お試し】',
             '独自サイトで保存した予想です。', '']
    for category, label, _ in LABELS:
        combos = sample['groups'][category]
        lines.extend([f'{label}｜{len(combos)}点',
                      ' / '.join(combos) if combos else '見送り（買い目なし）', ''])
    lines.extend([f'保存 {timestamp(sample["saved_at"]):%H:%M:%S}／'
                  f'締切予定 {timestamp(sample["deadline"]):%H:%M}',
                  '本日のサンプルは全員共通のこの1レースです。',
                  '参考情報です。的中・利益を保証するものではありません。'])
    text = '\n'.join(lines)
    if len(text) > 4500:
        raise ValueError('unexpected reply length')
    return text


class LineStore:
    """Use the existing private scheduler state and its PostgreSQL row lock."""

    def mutate(self, change):
        from automation import ensure_status_table
        require_storage_writable()
        ensure_status_table()
        with state_connection() as connection:
            connection.execute("SET LOCAL lock_timeout = '3s'")
            connection.execute('''INSERT INTO boat_automation_state(id, state)
                VALUES (%s, '{"version":1,"days":{}}'::jsonb)
                ON CONFLICT (id) DO NOTHING''', (STATE_ID,))
            raw = connection.execute(
                'SELECT state FROM boat_automation_state WHERE id=%s FOR UPDATE',
                (STATE_ID,)).fetchone()[0]
            state = json.loads(raw) if isinstance(raw, str) else deepcopy(raw)
            if state.get('version') != 1 or not isinstance(state.get('days'), dict):
                raise ValueError('unknown LINE state version')
            original = deepcopy(state)
            result = change(state)
            if state != original:
                connection.execute('''UPDATE boat_automation_state SET state=%s::jsonb,
                    updated_at=NOW() WHERE id=%s''',
                    (json.dumps(state, ensure_ascii=False), STATE_ID))
            connection.commit()
            return result

    @staticmethod
    def day(state, now):
        date = now.astimezone(JST).strftime('%Y%m%d')
        oldest = (now.astimezone(JST) - timedelta(days=14)).strftime('%Y%m%d')
        state['days'] = {key: value for key, value in state['days'].items()
                         if key >= oldest}
        day = state['days'].setdefault(date, {'sample': None, 'events': {}})
        if not isinstance(day, dict) or not isinstance(day.get('events'), dict):
            raise ValueError('invalid LINE day state')
        return day

    def sample(self, now):
        return self.mutate(lambda state: deepcopy(self.day(state, now)['sample']))

    def pin(self, candidate, now):
        def change(state):
            day = self.day(state, now)
            if day['sample'] is None:
                day['sample'] = deepcopy(candidate)
            return deepcopy(day['sample'])
        return self.mutate(change)

    def claim(self, event_id, now):
        key = hashlib.sha256(event_id.encode('utf-8')).hexdigest()
        def change(state):
            events = self.day(state, now)['events']
            if key in events:
                return False
            if len(events) >= MAX_DAILY_EVENTS:
                raise ValueError('LINE daily request capacity reached')
            events[key] = 'claimed'
            return True
        return self.mutate(change)

    def finish(self, event_id, now, status):
        key = hashlib.sha256(event_id.encode('utf-8')).hexdigest()
        def change(state):
            events = self.day(state, now)['events']
            if key in events:
                events[key] = status
        self.mutate(change)


class SampleBot:
    def __init__(self, core, store, clock=now_jst):
        self.core, self.store, self.clock = core, store, clock

    @strict_state_reads
    def answer(self, text):
        text = re.sub(r'\s+', '', unicodedata.normalize('NFKC', text)).lower()
        requested = None
        if text not in COMMANDS:
            for stadium, venue in self.core.STADIUMS.items():
                match = re.fullmatch(
                    r'(?:(?:今日|本日)の?)?(?:ボートレース)?' + re.escape(venue)
                    + r'(1[0-2]|[1-9])r(?:の)?(?:買い目|予想)?', text)
                if match:
                    requested = stadium, int(match.group(1))
                    break
            if requested is None:
                return HELP, None
        now = self.clock().astimezone(JST)
        sample = self.store.sample(now)
        target = None if sample is not None else designated_race(self.core.STADIUMS, now)
        if sample is None and target is None:
            return ('本日の無料お試しレースは運営側で準備中です。'
                    '対象レースの指定後に、もう一度「買い目」と送ってください。'), None
        if (sample is None and requested is not None
                and requested != (target['stadium'], target['race'])):
            return (f'本日のお試しは{sample_label(target)}の1レースです。'
                    '「買い目」と送ると3タイプをまとめて確認できます。'), None
        rows = self.core.load_ledger()
        valid = {row['id']: candidate for row in rows
                 if (candidate := frozen_sample(row, self.core.STADIUMS, now)) is not None}
        if sample is None:
            candidate = valid.get(target['id'])
            if candidate is None:
                return (f'本日のお試しは{sample_label(target)}です。'
                        '指定レースの予想は準備中、または配信可能な予想がありません。'
                        '時間をおいて「買い目」と送ってください。'), None
            if now >= timestamp(candidate['deadline']) - MIN_LEAD:
                return (f'本日のお試しは{sample_label(target)}です。'
                        '締切直前・締切後のため配信を終了しました。'), None
            sample = self.store.pin(candidate, now)
        if (sample.get('date') != now.strftime('%Y%m%d')
                or now >= timestamp(sample['deadline']) - MIN_LEAD):
            return (f'本日のお試しは{sample_label(sample)}です。'
                    '締切直前・締切後のため配信を終了しました。'
                    '本日は別のレースに切り替えません。'), None
        if requested is not None and requested != (sample['stadium'], sample['race']):
            return (f'本日のお試しは{sample_label(sample)}の1レースです。'
                    '「買い目」と送ると3タイプをまとめて確認できます。'), None
        if valid.get(sample['id']) != sample:
            return ('保存済み予想を確認できないため、お試しの配信を停止しています。'
                    '本日は別のレースに切り替えません。'), None
        return format_sample(sample), timestamp(sample['deadline'])


class LineError(RuntimeError):
    pass


class LineClient:
    def __init__(self, token):
        self.headers = {'Authorization': f'Bearer {token}'}

    def verify_account(self):
        try:
            response = requests.get('https://api.line.me/v2/bot/info',
                                    headers=self.headers, timeout=(3, 5),
                                    allow_redirects=False)
            if response.status_code != 200:
                raise LineError('LINE account check failed')
            data = response.json()
            if data.get('basicId') != EXPECTED_BASIC_ID or not data.get('userId'):
                raise LineError('LINE destination does not match')
            return data['userId']
        except (requests.RequestException, ValueError, AttributeError) as error:
            raise LineError('LINE account check failed') from None

    def reply(self, reply_token, text):
        try:
            response = requests.post(
                'https://api.line.me/v2/bot/message/reply',
                headers=self.headers,
                json={'replyToken': reply_token,
                      'messages': [{'type': 'text', 'text': text}]},
                timeout=(3, 8), allow_redirects=False)
            if response.status_code != 200:
                raise LineError('LINE reply failed')
        except requests.RequestException:
            raise LineError('LINE reply failed') from None


def eligible_event(event, config, now):
    if not isinstance(event, dict):
        return False
    source = event.get('source') or {}
    message = event.get('message') or {}
    if not isinstance(source, dict) or not isinstance(message, dict):
        return False
    event_id, token = event.get('webhookEventId'), event.get('replyToken')
    user = source.get('userId', '')
    stamp = event.get('timestamp')
    if (event.get('type') != 'message' or source.get('type') != 'user'
            or message.get('type') != 'text' or not isinstance(message.get('text'), str)
            or len(message['text']) > 500
            or not isinstance(event_id, str) or not 1 <= len(event_id) <= 256
            or not isinstance(token, str) or not 1 <= len(token) <= 256
            or not isinstance(user, str) or not re.fullmatch(r'U[0-9a-fA-F]{32}', user)
            or isinstance(stamp, bool) or not isinstance(stamp, (int, float))):
        return False
    age = now.timestamp() - stamp / 1000
    return (-5 <= age <= 50
            and (config.mode == 'live' or user == config.test_user))


def register_line_routes(core, *, store=None, client_factory=LineClient, clock=now_jst,
                         config_provider=configuration):
    store = store if store is not None else LineStore()
    bot = SampleBot(core, store, clock)

    @core.app.post('/api/line/webhook')
    def line_webhook():
        config = config_provider()
        if config is None:
            return jsonify({'ok': False, 'code': 'line_disabled'}), 503
        if request.content_length is not None and request.content_length > MAX_BODY:
            return jsonify({'ok': False, 'code': 'body_too_large'}), 413
        body = request.stream.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            return jsonify({'ok': False, 'code': 'body_too_large'}), 413
        if not valid_signature(body, request.headers.get('X-Line-Signature'),
                               config.secret):
            return jsonify({'ok': False, 'code': 'invalid_signature'}), 403
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            return jsonify({'ok': False, 'code': 'invalid_payload'}), 400
        if (not isinstance(payload, dict) or not isinstance(payload.get('events'), list)
                or len(payload['events']) > MAX_EVENTS):
            return jsonify({'ok': False, 'code': 'invalid_payload'}), 400
        try:
            client = client_factory(config.token)
            if payload.get('destination') != client.verify_account():
                raise LineError('LINE destination does not match')
            # LINE's verification request has an empty events list.
            for event in payload['events']:
                now = clock().astimezone(JST)
                if not eligible_event(event, config, now):
                    continue
                event_id = event['webhookEventId']
                # Commit before any HTTP reply. An ambiguous delivery is never
                # retried automatically with a redelivered event.
                if not store.claim(event_id, now):
                    continue
                try:
                    text, deadline = bot.answer(event['message']['text'])
                    send_at = clock().astimezone(JST)
                    if not eligible_event(event, config, send_at):
                        store.finish(event_id, now, 'expired')
                        continue
                    if (send_at.strftime('%Y%m%d') != now.strftime('%Y%m%d')
                            or (deadline is not None and send_at >= deadline - MIN_LEAD)):
                        text = '日付または締切時刻が変わったため、今回の予想の配信を終了しました。'
                    client.reply(event['replyToken'], text)
                    store.finish(event_id, now, 'sent')
                except Exception:
                    # Do not log SDK exceptions, headers, tokens, user IDs, or
                    # message content. A new user request has a new event ID.
                    store.finish(event_id, now, 'failed')
                    core.app.logger.warning('LINE sample reply failed; automatic retry disabled')
            return jsonify({'ok': True})
        except Exception:
            core.app.logger.warning('LINE webhook unavailable; check configuration or storage')
            return jsonify({'ok': False, 'code': 'line_unavailable'}), 503

