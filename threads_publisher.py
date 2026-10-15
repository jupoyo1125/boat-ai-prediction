"""Publish frozen, exhibition-backed Heiwajima forecasts once, before close.

Disabled by default. Supports Buffer or the official Threads API with separate
server credentials; ChatGPT's Metricool connection cannot be reused here.
"""
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import math
import os
import re
import threading
from uuid import uuid4

import requests

from state_store import state_connection, require_storage_writable, strict_state_reads

JST = timezone(timedelta(hours=9))
API = 'https://graph.threads.net/v1.0'
USERNAME = 'ton123kyotei456'
STADIUM = '04'
STATE_ID = 2  # id=1 remains exclusively owned by automation.write_status.
MIN_LEAD = timedelta(seconds=120)
LEASE = timedelta(seconds=90)
_wake = threading.Event()
_start_lock = threading.Lock()
_runner = None


def now_jst():
    return datetime.now(JST)


@dataclass(frozen=True)
class Config:
    date: str
    first_race: int
    token: str = field(repr=False)
    provider: str = 'threads'
    channel_id: str = ''


def configuration():
    if os.getenv('BOAT_THREADS_ENABLED') != '1':
        return None
    date = os.getenv('BOAT_THREADS_DATE', '')
    try:
        if not re.fullmatch(r'\d{8}', date):
            return None
        datetime.strptime(date, '%Y%m%d')
        first = int(os.getenv('BOAT_THREADS_FROM_RACE', ''))
        if not 1 <= first <= 12:
            return None
    except (TypeError, ValueError):
        return None
    provider = os.getenv('BOAT_THREADS_PROVIDER', 'threads')
    if provider == 'buffer':
        token = os.getenv('BUFFER_API_KEY', '').strip()
        channel = os.getenv('BOAT_BUFFER_CHANNEL_ID', '').strip()
        if token and channel and len(channel) <= 256:
            return Config(date, first, token, provider, channel)
        return None
    if provider != 'threads':
        return None  # Never fall back to a different account or provider.
    token = os.getenv('THREADS_ACCESS_TOKEN', '').strip()
    return Config(date, first, token) if token else None


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('timezone required')
    return parsed.astimezone(JST)


def complete_exhibition(prediction):
    boats = prediction.get('boats') or []
    if len(boats) != 6 or {b.get('boat') for b in boats} != set(range(1, 7)):
        return False
    for boat in boats:
        for key, low, high in [('exhibition', 5, 9), ('exhibition_st', -1, 1)]:
            value = boat.get(key)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not low < value < high):
                return False
    return True


def caption(row):
    """Copy saved rankings and all three buy lists; never re-run the model."""
    prediction = row['prediction']
    boats = {b['boat']: b for b in prediction['boats']}
    marks = ' ①②③④⑤⑥'
    picks = [prediction.get(key) for key in ('main', 'second', 'hole')]
    if any(isinstance(n, bool) or n not in boats for n in picks) or len(set(picks)) != 3:
        raise ValueError('invalid saved picks')
    lines = []
    for label, number in zip(('本命', '相手軸', '穴'), picks):
        boat = boats[number]
        # Preserve both names even if their separator is an ASCII space.
        # Prefecture/birthplace details are identified by their slash.
        name_part = re.split(r'\s+\S+/\S+', boat['name'].strip(), maxsplit=1)[0]
        name = re.sub(r'\s+', '', name_part)
        if not name or len(name) > 12:
            raise ValueError('invalid racer name')
        st = boat['exhibition_st']
        st_text = ('F' if st < 0 else '') + f'{abs(st):.2f}'
        lines.append(f'{label}{marks[number]}{name}（展示{boat["exhibition"]:.2f}／ST{st_text}）')
    groups = []
    bets = row.get('bets') or []
    if not bets or any(b.get('category') not in ('gachi', 'roman', 'oni') for b in bets):
        raise ValueError('invalid saved categories')
    for key, label in [('gachi', '🎯ガチガチ'), ('roman', '🚀ロマン砲'), ('oni', '👹鬼しぼり')]:
        selected = [b['bet'] for b in bets if b['category'] == key]
        if len(selected) > (3 if key == 'oni' else 10):
            raise ValueError('unexpected category size')
        for bet in selected:
            if not re.fullmatch(r'[1-6]-[1-6]-[1-6]', bet) or len(set(bet.split('-'))) != 3:
                raise ValueError('invalid saved bet')
        groups.append(f'{label}{len(selected)}点\n' + (' / '.join(selected) if selected else '見送り'))
    date = row['date']
    text = (f'【{int(date[4:6])}/{int(date[6:8])} 平和島{row["race"]}R｜展示後AI予想🚤】\n'
            '独自作成のAI予想サイトでは、以下の予想です。\n\n'
            + '\n'.join(lines) + '\n\n' + '\n\n'.join(groups)
            + f'\n\n{timestamp(row["prediction_saved_at"]):%H:%M}保存／'
              f'締切{timestamp(row["scheduled_deadline"]):%H:%M}\n展示タイム・展示ST反映済み')
    if len(text) > 500 or re.search(r'https?://|www\.|onrender\.com', text, re.I):
        raise ValueError('post text exceeds the allowed format')
    return text


def candidate(row, config, now):
    try:
        race = int(row.get('race', 0))
        if (now.strftime('%Y%m%d') != config.date or row.get('date') != config.date
                or row.get('stadium') != STADIUM or not config.first_race <= race <= 12
                or row.get('id') != f'auto:{config.date}:{STADIUM}:{race:02d}'
                or row.get('prediction_origin') != 'automatic' or row.get('settled')
                or row.get('actual_combo') or row.get('learned')):
            return None
        saved = timestamp(row['prediction_saved_at'])
        detected = timestamp(row['exhibition_detected_at'])
        deadline = timestamp(row['scheduled_deadline'])
        if not detected <= saved <= now < deadline - MIN_LEAD:
            return None
        if deadline.strftime('%Y%m%d') != config.date or not complete_exhibition(row['prediction']):
            return None
        return {'key': row['id'], 'deadline': deadline.isoformat(), 'text': caption(row),
                'saved_at': row['prediction_saved_at'], 'state': 'queued', 'attempts': 0,
                'provider': config.provider}
    except (KeyError, ValueError, TypeError, OverflowError):
        return None


class PostStore:
    """A small durable outbox in the existing, private scheduler state table.

    PostgreSQL's row lock only covers local JSON updates. No HTTP call is made
    while it is held. State id=2 is copied by the existing storage migration.
    """
    def mutate(self, change):
        from automation import ensure_status_table
        require_storage_writable()
        ensure_status_table()
        with state_connection() as connection:
            connection.execute("SET LOCAL lock_timeout = '3s'")
            connection.execute('''INSERT INTO boat_automation_state(id, state)
                VALUES (%s, '{"version":1,"posts":{}}'::jsonb)
                ON CONFLICT (id) DO NOTHING''', (STATE_ID,))
            raw = connection.execute('SELECT state FROM boat_automation_state WHERE id=%s FOR UPDATE',
                                     (STATE_ID,)).fetchone()[0]
            state = json.loads(raw) if isinstance(raw, str) else deepcopy(raw)
            if state.get('version') != 1 or not isinstance(state.get('posts'), dict):
                raise ValueError('unknown publisher state version')
            original = deepcopy(state)
            result = change(state)
            if state != original:
                connection.execute('''UPDATE boat_automation_state SET state=%s::jsonb,
                    updated_at=NOW() WHERE id=%s''', (json.dumps(state, ensure_ascii=False), STATE_ID))
            connection.commit()
            return result

    def enqueue(self, candidates):
        def change(state):
            for entry in candidates:
                state['posts'].setdefault(entry['key'], deepcopy(entry))
        self.mutate(change)

    def claim(self, config, now):
        def change(state):
            entries = sorted(state['posts'].values(), key=lambda p: p['deadline'])
            for entry in entries:
                if not entry['key'].startswith(f'auto:{config.date}:{STADIUM}:'):
                    continue
                race = int(entry['key'].rsplit(':', 1)[1])
                if not config.first_race <= race <= 12:
                    continue
                if entry.get('provider', 'threads') != config.provider:
                    continue
                if entry['state'] in ('publishing', 'withdrawing'):
                    if now - timestamp(entry['claimed_at']) > LEASE:
                        entry.update(state='uncertain', error='公開結果の確認が必要です。自動再投稿しません。')
                    continue
                if entry['state'] in ('creating', 'ready'):
                    if now - timestamp(entry['claimed_at']) <= LEASE:
                        continue
                elif entry['state'] != 'queued':
                    continue
                if now >= timestamp(entry['deadline']) - MIN_LEAD:
                    entry['state'] = 'expired'
                    continue
                entry.update(state='ready' if entry.get('container_id') else 'creating',
                             claim=str(uuid4()), claimed_at=now.isoformat(),
                             attempts=entry['attempts'] + 1)
                return deepcopy(entry)
            return None
        return self.mutate(change)

    def claim_confirmation(self, config, now):
        """Lease a read-only confirmation across workers, at most once a minute."""
        def change(state):
            for entry in sorted(state['posts'].values(), key=lambda p: p['deadline']):
                if (entry['state'] != 'submitted' or entry.get('provider') != 'buffer'
                        or not entry['key'].startswith(f'auto:{config.date}:{STADIUM}:')
                        or int(entry['key'].rsplit(':', 1)[1]) < config.first_race):
                    continue
                if entry.get('next_check_at') and now < timestamp(entry['next_check_at']):
                    continue
                entry.update(claim=str(uuid4()), claimed_at=now.isoformat(),
                             next_check_at=(now + timedelta(seconds=60)).isoformat())
                return deepcopy(entry)
            return None
        return self.mutate(change)

    def update(self, entry, expected, **values):
        def change(state):
            current = state['posts'].get(entry['key'])
            if not current or current.get('claim') != entry.get('claim') or current['state'] not in expected:
                return False
            current.update(values)
            return True
        return self.mutate(change)

    def read(self):
        from automation import ensure_status_table
        ensure_status_table()
        with state_connection() as connection:
            row = connection.execute('SELECT state FROM boat_automation_state WHERE id=%s',
                                     (STATE_ID,)).fetchone()
        if not row:
            return {'version': 1, 'posts': {}}
        return json.loads(row[0]) if isinstance(row[0], str) else row[0]


class ThreadsError(RuntimeError):
    pass


class WrongAccount(ThreadsError):
    pass


class ThreadsClient:
    def __init__(self, token, session=None):
        self.session = session or requests.Session()
        self._headers = {'Authorization': f'Bearer {token}'}

    def request(self, method, path, **kwargs):
        response = self.session.request(method, API + path, headers=self._headers,
                                        timeout=15, allow_redirects=False, **kwargs)
        if not 200 <= response.status_code < 300:
            # Never log bodies, tokens, request headers, or credential-bearing URLs.
            raise ThreadsError(f'Threads API HTTP {response.status_code}')
        payload = response.json()
        if not isinstance(payload, dict) or 'error' in payload:
            raise ThreadsError('Threads API response invalid')
        return payload

    def verify_account(self):
        profile = self.request('GET', '/me', params={'fields': 'id,username'})
        if str(profile.get('username', '')).lower() != USERNAME:
            raise WrongAccount('Threads投稿先が指定アカウントと一致しません。')

    def create(self, text):
        value = self.request('POST', '/me/threads',
                             data={'media_type': 'TEXT', 'text': text, 'reply_control': 'everyone'})
        return self.identifier(value)

    def publish(self, container):
        value = self.request('POST', '/me/threads_publish', data={'creation_id': container})
        return self.identifier(value)

    def permalink(self, post):
        value = self.request('GET', '/' + post, params={'fields': 'permalink'})
        link = value.get('permalink', '')
        return link if re.match(r'^https://(?:www\.)?threads\.(?:com|net)/', link) else ''

    @staticmethod
    def identifier(value):
        result = str(value.get('id', ''))
        if not re.fullmatch(r'\d+', result):
            raise ThreadsError('Threads API id missing')
        return result


class Publisher:
    def __init__(self, core, config, store=None, client=None, clock=now_jst):
        self.core, self.config, self.clock = core, config, clock
        self.store = store or PostStore()
        if client is not None:
            self.client = client
        elif config.provider == 'buffer':
            from buffer_publisher import BufferClient
            self.client = BufferClient(config.token, config.channel_id)
        else:
            self.client = ThreadsClient(config.token)

    @strict_state_reads
    def run_once(self):
        from automation import configured
        now = self.clock()
        if now.strftime('%Y%m%d') != self.config.date or not configured():
            return
        if self.config.provider == 'buffer':
            self.confirm_buffer()
        entries = [entry for row in self.core.load_ledger()
                   if (entry := candidate(row, self.config, now))]
        self.store.enqueue(entries)
        while entry := self.store.claim(self.config, self.clock()):
            try:
                self.client.verify_account()
                container = entry.get('container_id') or self.client.create(entry['text'])
                if self.config.provider == 'buffer':
                    self.client.validate_draft(container, entry['text'])
                if not self.store.update(entry, ('creating', 'ready'), state='ready', container_id=container):
                    continue
                if self.clock() >= timestamp(entry['deadline']) - MIN_LEAD:
                    self.store.update(entry, ('ready',), state='expired')
                    continue
            except Exception as error:
                fatal = isinstance(error, WrongAccount) or entry['attempts'] >= 3
                self.store.update(entry, ('creating', 'ready'), state='error' if fatal else 'queued',
                                  error='Threads接続を確認してください。公開開始前に停止しました。')
                return  # Retry safe preparation on a later cycle, at most three times.
            # Persist the at-most-once boundary before the visible side effect.
            if not self.store.update(entry, ('ready',), state='publishing'):
                continue
            try:
                post = (self.client.publish(container, entry['text'])
                        if self.config.provider == 'buffer' else self.client.publish(container))
            except Exception:
                self.store.update(entry, ('publishing',), state='uncertain',
                                  error='公開成否が不明です。重複を避けるため自動再投稿しません。')
                return
            # If this write fails, the durable state stays publishing; restart
            # recovery marks it uncertain instead of blindly publishing again.
            if self.config.provider == 'buffer':
                self.store.update(entry, ('publishing', 'uncertain'), state='submitted',
                                  post_id=post, error='',
                                  next_check_at=(self.clock() + timedelta(seconds=20)).isoformat())
                continue  # Acceptance is not proof that Threads has published it.
            if self.store.update(entry, ('publishing', 'uncertain'), state='published', post_id=post, error=''):
                try:
                    link = self.client.permalink(post)
                    self.store.update(entry, ('published',), permalink=link)
                except Exception:
                    pass  # A missing link is never a reason to re-publish.

    def confirm_buffer(self):
        while entry := self.store.claim_confirmation(self.config, self.clock()):
            deadline = timestamp(entry['deadline'])
            try:
                post = self.client.publication(entry['post_id'], entry['text'])
                if post['status'] == 'sent':
                    sent = timestamp(post['sentAt'])
                    if not timestamp(entry['saved_at']) <= sent <= self.clock():
                        raise ValueError('invalid publication timestamp')
                    late = sent >= deadline
                    self.store.update(entry, ('submitted',),
                                      state='published_late' if late else 'published',
                                      published_at=sent.isoformat(),
                                      permalink=self.client.permalink(post),
                                      error='投稿先で締切後に公開されました。' if late else '')
                elif post['status'] in ('error', 'needs_approval', 'draft'):
                    self.store.update(entry, ('submitted',), state='error',
                                      error='Bufferで公開が停止しました。自動再投稿しません。')
                elif post['status'] == 'scheduled' and self.clock() >= deadline - MIN_LEAD:
                    if self.store.update(entry, ('submitted',), state='withdrawing'):
                        try:
                            self.client.withdraw(entry['post_id'])
                            self.store.update(entry, ('withdrawing',), state='expired',
                                              error='締切が近いためBufferの待機投稿を下書きへ戻しました。')
                        except Exception:
                            self.store.update(entry, ('withdrawing',), state='uncertain',
                                              error='Bufferでの待機解除を確認してください。自動再投稿しません。')
                elif self.clock() >= deadline + timedelta(minutes=5):
                    self.store.update(entry, ('submitted',), state='uncertain',
                                      error='Bufferの公開完了を確認できません。自動再投稿しません。')
            except Exception:
                self.store.update(entry, ('submitted',),
                                  state='uncertain' if self.clock() >= deadline else 'submitted',
                                  error='Bufferの公開状態を確認できません。自動再投稿しません。')


def wake():
    """A prediction-save notification; no network or DB work on the model path."""
    _wake.set()


def start(core):
    global _runner
    from automation import configured
    config = configuration()
    if not config or config.date != now_jst().strftime('%Y%m%d') or not configured():
        return
    with _start_lock:
        if _runner and _runner.is_alive():
            return
        def run():
            publisher = Publisher(core, config)
            while now_jst().strftime('%Y%m%d') == config.date:
                _wake.clear()
                try:
                    publisher.run_once()
                except Exception as error:
                    core.app.logger.error('Threads publisher paused (%s)', type(error).__name__)
                _wake.wait(20)
        _runner = threading.Thread(target=run, daemon=True, name='threads-publisher')
        _runner.start()


def public_status():
    config = configuration()
    from automation import configured
    enabled = bool(config and config.date == now_jst().strftime('%Y%m%d') and configured())
    result = {'ok': True, 'enabled': enabled, 'date': config.date if config else None,
              'stadium': STADIUM, 'username': USERNAME,
              'provider': config.provider if config else os.getenv('BOAT_THREADS_PROVIDER', 'threads'),
              'from_race': config.first_race if config else None}
    if not enabled:
        result['message'] = '投稿用接続・対象日・開始レースの設定待ち、または対象日終了です。'
        return result
    try:
        state = PostStore().read()
        result['posts'] = [{k: entry[k] for k in
                           ('key', 'provider', 'state', 'post_id', 'permalink', 'published_at', 'error')
                           if k in entry}
                           for entry in state['posts'].values()
                           if entry['key'].startswith(f'auto:{config.date}:{STADIUM}:')]
    except Exception:
        result.update(ok=False, enabled=False, message='投稿記録の保存先に接続できません。')
    return result
