"""Optional, dated LINE broadcast of the same frozen daily sample.

No prediction is generated here. The existing private LINE state (id=3) pins
the sample and commits a retry key before HTTP. Only the Japanese 200-message
free quota is accepted; paid/unlimited or unknown quota configurations stop.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import os
import threading
import uuid

import requests
from flask import jsonify

from line_bot import (
    JST, MIN_LEAD, LineClient, LineError, LineStore, configuration,
    designated_race, format_sample, frozen_sample, now_jst, timestamp,
)
from state_store import strict_state_reads

LEASE = timedelta(seconds=45)
MAX_RETRY_AGE = timedelta(minutes=10)  # Well within LINE's 24-hour retry window.
MAX_ATTEMPTS = 3
TERMINAL = {'accepted', 'failed', 'cancelled', 'unknown'}
_wake = threading.Event()
_start_lock = threading.Lock()
_runner = None


def enabled_config(config_provider=configuration):
    if os.getenv('BOAT_LINE_BROADCAST_ENABLED') != '1':
        return None
    config = config_provider()
    return config if config is not None and config.mode == 'live' else None


class BroadcastStore(LineStore):
    def read_day(self, now):
        return self.mutate(lambda state: deepcopy(self.day(state, now)))

    def claim_broadcast(self, candidate, now):
        if (candidate['date'] != now.astimezone(JST).strftime('%Y%m%d')
                or now >= timestamp(candidate['deadline']) - MIN_LEAD):
            return None
        def change(state):
            day = self.day(state, now)
            if day['sample'] is not None and day['sample'] != candidate:
                return None
            entry = day.get('broadcast')
            if entry is None:
                day['sample'] = deepcopy(candidate)
                entry = day['broadcast'] = {
                    'sample_id': candidate['id'], 'retry_key': str(uuid.uuid4()),
                    'payload': {'messages': [{'type': 'text',
                                             'text': format_sample(candidate)}]},
                    'first_attempt_at': now.isoformat(), 'attempts': 0,
                    'status': 'retry', 'next_attempt_at': now.isoformat(),
                }
            if entry['sample_id'] != candidate['id'] or entry['status'] in TERMINAL:
                return None
            if (entry['attempts'] >= MAX_ATTEMPTS
                    or now >= timestamp(entry['first_attempt_at']) + MAX_RETRY_AGE):
                entry['status'] = 'unknown'
                return None
            if now < timestamp(entry['next_attempt_at']):
                return None
            entry['attempts'] += 1
            entry['status'] = 'sending'
            entry['next_attempt_at'] = (now + LEASE).isoformat()
            return deepcopy(entry)
        return self.mutate(change)

    def finish_broadcast(self, entry, now, status):
        def change(state):
            day = self.day(state, now)
            current = day.get('broadcast')
            if current is None or current['retry_key'] != entry['retry_key']:
                return
            # A late failure must never undo an accepted API request.
            if current['status'] == 'accepted':
                return
            if status == 'accepted':
                current['status'] = 'accepted'
                current['accepted_at'] = now.isoformat()
            elif current['attempts'] == entry['attempts']:
                current['status'] = status
                if status == 'retry':
                    if current['attempts'] >= MAX_ATTEMPTS:
                        current['status'] = 'unknown'
                    else:
                        delay = 15 * (2 ** (current['attempts'] - 1))
                        current['next_attempt_at'] = (now + timedelta(seconds=delay)).isoformat()
        self.mutate(change)


class BroadcastClient(LineClient):
    def free_quota_available(self):
        try:
            values = []
            for path in ('quota', 'quota/consumption'):
                response = requests.get(
                    f'https://api.line.me/v2/bot/message/{path}',
                    headers=self.headers, timeout=(3, 5), allow_redirects=False)
                if response.status_code != 200:
                    raise LineError('LINE quota check failed')
                values.append(response.json())
            quota, usage = values
            count = usage.get('totalUsage')
            return (quota.get('type') == 'limited'
                    and type(quota.get('value')) is int and quota['value'] == 200
                    and type(count) is int and 0 <= count < 200)
        except (requests.RequestException, ValueError, AttributeError, TypeError):
            raise LineError('LINE quota check failed') from None

    def broadcast(self, payload, retry_key):
        try:
            response = requests.post(
                'https://api.line.me/v2/bot/message/broadcast',
                headers={**self.headers, 'X-Line-Retry-Key': retry_key},
                json=payload, timeout=(3, 8), allow_redirects=False)
            if response.status_code == 200 or (
                    response.status_code == 409
                    and response.headers.get('x-line-accepted-request-id')):
                return 'accepted'
            return 'retry' if 500 <= response.status_code < 600 else 'failed'
        except requests.RequestException:
            return 'retry'


class Publisher:
    def __init__(self, core, *, store=None, client_factory=BroadcastClient,
                 clock=now_jst, config_provider=configuration):
        self.core = core
        self.store = store if store is not None else BroadcastStore()
        self.client_factory = client_factory
        self.clock = clock
        self.config_provider = config_provider

    def candidate(self, target, now):
        rows = [row for row in self.core.load_ledger() if row.get('id') == target['id']]
        if len(rows) != 1:
            return None
        return frozen_sample(rows[0], self.core.STADIUMS, now)

    @strict_state_reads
    def run_once(self):
        config = enabled_config(self.config_provider)
        if config is None:
            return 'disabled'
        now = self.clock().astimezone(JST)
        target = designated_race(self.core.STADIUMS, now)
        if target is None:
            return 'waiting_for_target'
        day = self.store.read_day(now)
        previous = day.get('broadcast')
        if previous is not None and previous['status'] in TERMINAL:
            return previous['status']
        if (day['sample'] is not None and day['sample']['id'] != target['id']):
            return 'different_sample_pinned'
        candidate = self.candidate(target, now)
        if candidate is None:
            return 'waiting_for_prediction'
        if now >= timestamp(candidate['deadline']) - MIN_LEAD:
            return 'closed'
        if day['sample'] is not None and day['sample'] != candidate:
            return 'source_changed'
        if previous is not None and now < timestamp(previous['next_attempt_at']):
            return 'busy'
        client = self.client_factory(config.token)
        client.verify_account()
        if not client.free_quota_available():
            return 'quota_blocked'
        entry = self.store.claim_broadcast(candidate, self.clock().astimezone(JST))
        if entry is None:
            return 'busy'
        send_at = self.clock().astimezone(JST)
        # Recheck after storage and network waits, immediately before sending.
        if (enabled_config(self.config_provider) != config
                or designated_race(self.core.STADIUMS, send_at) != target
                or send_at.strftime('%Y%m%d') != candidate['date']
                or send_at >= timestamp(candidate['deadline']) - MIN_LEAD
                or self.candidate(target, send_at) != candidate):
            self.store.finish_broadcast(entry, now, 'cancelled')
            return 'cancelled'
        result = client.broadcast(entry['payload'], entry['retry_key'])
        self.store.finish_broadcast(entry, now, result)
        return result


def wake():
    """Called after a prediction is saved; never do HTTP on the model path."""
    _wake.set()


def run_worker(core, clock=now_jst, wait=None):
    publisher = Publisher(core, clock=clock)
    wait = wait or _wake.wait
    while True:
        if enabled_config() is None:
            return
        _wake.clear()
        try:
            publisher.run_once()
        except Exception:
            # SDK errors can contain authentication or message data.
            core.app.logger.warning('LINE broadcast waiting; check connection or storage')
        wait(20)


def start(core):
    global _runner
    if enabled_config() is None:
        return
    with _start_lock:
        if _runner is not None and _runner.is_alive():
            return
        _runner = threading.Thread(target=run_worker, args=(core,), daemon=True,
                                   name='line-sample-broadcast')
        _runner.start()


def register_broadcast_routes(core):
    @core.app.get('/api/line_publishing')
    def line_publishing_status():
        config = enabled_config()
        raw = os.getenv('BOAT_LINE_SAMPLE_RACE', '').strip()
        target = None
        try:
            selected_day = datetime.strptime(raw.split(':')[0], '%Y%m%d').replace(tzinfo=JST)
            target = designated_race(core.STADIUMS, selected_day)
        except (ValueError, IndexError):
            pass
        response = jsonify({
            'enabled': config is not None,
            'worker_running': _runner is not None and _runner.is_alive(),
            'target': target,
            'free_plan_only': True,
            'requires_user_message': False,
        })
        response.headers['Cache-Control'] = 'no-store'
        return response
