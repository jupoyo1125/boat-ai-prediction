"""Reply to explicit LINE requests with one frozen sample race per JST day.

No predictions are generated here. Disabled by default; the initial enabled
mode only answers the operator's test user. State id=3 is retained by the
existing storage migration. Opted-in adults' recipient IDs are kept only in
private state for delivery. Reply tokens and incoming message bodies are never
stored, and recipient IDs are never written to logs or public status endpoints.
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
MAX_SUBSCRIPTIONS = 5000
AGE_YES = '20歳以上・配信希望'
AGE_NO = '20歳未満'
STOP_COMMANDS = {'配信停止', '停止', '登録解除'}
AGE_PROMPT = ('無料お試し予想の配信は20歳以上の方限定です。\n'
              '20歳以上で、自動配信を希望する方は\n'
              f'「{AGE_YES}」と送るか、下のボタンを選んでください。\n'
              f'20歳未満の方は「{AGE_NO}」を選んでください。\n'
              '配信に必要なLINEの識別IDと回答日時を非公開で保持します。'
              '氏名・生年月日の入力は不要です。\n'
              'いつでも「配信停止」で解除できます。')
AGE_QUICK_REPLY = {'items': [
    {'type': 'action', 'action': {'type': 'message', 'label': AGE_YES, 'text': AGE_YES}},
    {'type': 'action', 'action': {'type': 'message', 'label': AGE_NO, 'text': AGE_NO}},
]}
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
        '自動配信は「20歳以上・配信希望」と回答した方だけが対象です。\n'
        '停止するときは「配信停止」と送ってください。\n'
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

    @staticmethod
    def user_key(user):
        if not isinstance(user, str) or not re.fullmatch(r'U[0-9a-fA-F]{32}', user):
            raise ValueError('invalid LINE user')
        return hashlib.sha256(user.encode('utf-8')).hexdigest()

    @staticmethod
    def subscriptions(state, now=None):
        subscriptions = state.setdefault('subscriptions', {})
        if not isinstance(subscriptions, dict):
            raise ValueError('invalid LINE subscription state')
        if now is not None:
            oldest = now - timedelta(days=30)
            for key, profile in list(subscriptions.items()):
                if (isinstance(profile, dict) and profile.get('status') != 'adult'
                        and timestamp(profile['updated_at']) < oldest):
                    del subscriptions[key]
        return subscriptions

    @classmethod
    def eligible_subscriber(cls, key, profile, now):
        try:
            user = profile['user_id']
            return (profile.get('status') == 'adult'
                    and type(profile.get('consent_version')) is int
                    and profile['consent_version'] == 1
                    and cls.user_key(user) == key
                    and timestamp(profile['confirmed_at']) <= now)
        except (KeyError, TypeError, ValueError):
            return False

    @classmethod
    def recipient_list(cls, state, now):
        return sorted({profile['user_id'] for key, profile in cls.subscriptions(state, now).items()
                       if cls.eligible_subscriber(key, profile, now)})

    def recipients(self, now):
        return self.mutate(lambda state: self.recipient_list(state, now))

    def is_adult(self, user, now):
        key = self.user_key(user)
        return self.mutate(lambda state: self.eligible_subscriber(
            key, self.subscriptions(state, now).get(key, {}), now))

    def subscription_status(self, user):
        key = self.user_key(user)
        return self.mutate(lambda state: self.subscriptions(state).get(key, {}).get('status'))

    def set_subscription(self, user, status, now, event_ms=None):
        if status not in ('adult', 'underage', 'stopped', 'pending'):
            raise ValueError('invalid subscription status')
        key = self.user_key(user)
        stamp = event_ms if event_ms is not None else int(now.timestamp() * 1000)
        def change(state):
            subscriptions = self.subscriptions(state, now)
            previous = subscriptions.get(key)
            if previous is not None:
                old_stamp = previous.get('event_ms', -1)
                if stamp < old_stamp or (stamp == old_stamp and status == 'adult'
                                        and previous.get('status') != 'adult'):
                    return False
            elif len(subscriptions) >= MAX_SUBSCRIPTIONS:
                raise ValueError('LINE subscription capacity reached')
            profile = {'status': status, 'event_ms': stamp, 'updated_at': now.isoformat()}
            if status == 'adult':
                profile.update(user_id=user, consent_version=1, confirmed_at=now.isoformat())
            subscriptions[key] = profile
            if status != 'adult':
                # Cancel the whole frozen audience instead of editing a request
                # under an existing retry key. Remove retained recipient IDs.
                for day in state['days'].values():
                    publication = day.get('broadcast')
                    if not isinstance(publication, dict):
                        continue
                    payload = publication.get('payload', {})
                    if user in payload.get('to', []):
                        if publication.get('status') != 'accepted':
                            publication['status'] = 'cancelled'
                        payload.pop('to', None)
            return True
        return self.mutate(change)


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

    def reply(self, reply_token, text, quick_reply=None):
        message = {'type': 'text', 'text': text}
        if quick_reply is not None:
            message['quickReply'] = quick_reply
        try:
            response = requests.post(
                'https://api.line.me/v2/bot/message/reply',
                headers=self.headers,
                json={'replyToken': reply_token,
                      'messages': [message]},
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
    kind = event.get('type')
    if (kind not in ('message', 'follow', 'unfollow') or source.get('type') != 'user'
            or not isinstance(event_id, str) or not 1 <= len(event_id) <= 256
            or (kind != 'unfollow' and (not isinstance(token, str) or not 1 <= len(token) <= 256))
            or not isinstance(user, str) or not re.fullmatch(r'U[0-9a-fA-F]{32}', user)
            or isinstance(stamp, bool) or not isinstance(stamp, (int, float))):
        return False
    if kind == 'message' and (message.get('type') != 'text'
                             or not isinstance(message.get('text'), str)
                             or len(message['text']) > 500):
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
                    user = event['source']['userId']
                    kind = event['type']
                    command = unicodedata.normalize('NFKC', event.get('message', {}).get('text', '')).strip()
                    quick_reply, deadline, has_prediction = None, None, False
                    if kind == 'unfollow':
                        store.set_subscription(user, 'stopped', now, event['timestamp'])
                        store.finish(event_id, now, 'unfollowed')
                        continue
                    if kind == 'follow':
                        store.set_subscription(user, 'pending', now, event['timestamp'])
                        text, quick_reply = AGE_PROMPT, AGE_QUICK_REPLY
                    elif command in STOP_COMMANDS or command == AGE_NO:
                        status = 'underage' if command == AGE_NO else 'stopped'
                        changed = store.set_subscription(user, status, now, event['timestamp'])
                        if not changed:
                            text = ('新しい回答が先に届いているため、この回答は反映しませんでした。'
                                    '停止する場合は、もう一度「配信停止」と送ってください。')
                        else:
                            text = ('20歳未満の方には予想・買い目を配信しません。'
                                    if status == 'underage' else
                                    '予想の配信を停止しました。配信用のLINE識別IDを削除しました。')
                    elif command == AGE_YES:
                        changed = store.set_subscription(user, 'adult', now, event['timestamp'])
                        if changed and store.is_adult(user, now):
                            text = ('20歳以上で配信を希望する旨の回答を受け付けました。\n'
                                    '指定レースの展示後予想が保存されたら自動でお届けします。'
                                    '1日最大1レースです。準備状況・締切・無料枠により配信しない場合があります。\n'
                                    '本日の自動配信が済んでいる場合は、自動では届きません。'
                                    '締切前なら「買い目」で確認できます。'
                                    '停止は「配信停止」と送ってください。\n'
                                    '参考情報であり、的中・利益を保証するものではありません。')
                        else:
                            text, quick_reply = AGE_PROMPT, AGE_QUICK_REPLY
                    elif not store.is_adult(user, now):
                        if store.subscription_status(user) == 'underage':
                            text = '20歳未満の方には予想・買い目を配信しません。'
                        else:
                            text, quick_reply = AGE_PROMPT, AGE_QUICK_REPLY
                    else:
                        text, deadline = bot.answer(event['message']['text'])
                        has_prediction = deadline is not None
                    send_at = clock().astimezone(JST)
                    if not eligible_event(event, config, send_at):
                        store.finish(event_id, now, 'expired')
                        continue
                    if (send_at.strftime('%Y%m%d') != now.strftime('%Y%m%d')
                            or (deadline is not None and send_at >= deadline - MIN_LEAD)):
                        text = '日付または締切時刻が変わったため、今回の予想の配信を終了しました。'
                    if has_prediction and not store.is_adult(user, send_at):
                        text, quick_reply = AGE_PROMPT, AGE_QUICK_REPLY
                    if quick_reply is None:
                        client.reply(event['replyToken'], text)
                    else:
                        client.reply(event['replyToken'], text, quick_reply=quick_reply)
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


