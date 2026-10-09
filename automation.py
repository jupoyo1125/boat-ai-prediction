"""Poll official exhibition/results and freeze each forecast before its deadline."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
import re
import threading
import time
import unicodedata

from bs4 import BeautifulSoup
from state_store import (
    database_url, runner_lease, state_connection, strict_state_reads,
    storage_paused, require_storage_writable,
)

JST = timezone(timedelta(hours=9))
POLL_SECONDS = 60
PRODUCTION_SERVICE = 'srv-das6ofm0tbcc73e2id60'
_runner = None
_start_lock = threading.Lock()
_table_ready = False
_table_lock = threading.Lock()


def now_jst():
    return datetime.now(JST)


def configured():
    if storage_paused():
        return False
    requested = os.getenv('BOAT_AUTO_ENABLED', '1').lower() not in ('0', 'false', 'off')
    target = (os.getenv('RENDER_SERVICE_ID') == PRODUCTION_SERVICE
              or os.getenv('BOAT_AUTO_RUNNER') == '1')
    return bool(requested and target and database_url())


def ensure_status_table():
    global _table_ready
    with _table_lock:
        if _table_ready:
            return
        with state_connection() as connection:
            connection.execute('SELECT pg_advisory_xact_lock(%s)', (781042019,))
            connection.execute('''CREATE TABLE IF NOT EXISTS boat_automation_state (
                id INTEGER PRIMARY KEY, state JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
            connection.commit()
        _table_ready = True


def write_status(status):
    require_storage_writable()
    ensure_status_table()
    with state_connection() as connection:
        connection.execute('''INSERT INTO boat_automation_state (id, state, updated_at)
            VALUES (1, %s::jsonb, NOW()) ON CONFLICT (id) DO UPDATE
            SET state = EXCLUDED.state, updated_at = NOW()''',
            (json.dumps(status, ensure_ascii=False),))
        connection.commit()


def read_status():
    if storage_paused():
        return {'ok': True, 'enabled': False, 'phase': 'maintenance',
                'message': '保存先の移行中です。自動予想・学習は完了後に再開します。',
                'predicted': 0, 'settled': 0, 'pending': 0, 'last_checked_at': None}
    if not configured():
        return {'ok': True, 'enabled': False, 'phase': 'disabled',
                'message': '自動運転の稼働条件を確認中です。', 'predicted': 0,
                'settled': 0, 'pending': 0, 'last_checked_at': None}
    try:
        ensure_status_table()
        with state_connection() as connection:
            row = connection.execute('SELECT state FROM boat_automation_state WHERE id=1').fetchone()
        if row:
            result = json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])
            result.update(ok=True, enabled=True)
            return result
        return {'ok': True, 'enabled': True, 'phase': 'starting',
                'message': '展示データと確定結果の確認を開始します。'}
    except Exception:
        return {'ok': False, 'enabled': False, 'phase': 'storage_error',
                'message': '保存先に接続できないため、自動処理を一時停止しています。'}


def parse_deadlines(html, date):
    soup = BeautifulSoup(unicodedata.normalize('NFKC', html), 'html.parser')
    day = datetime.strptime(date, '%Y%m%d').replace(tzinfo=JST)
    for table in soup.find_all('table'):
        row = next((row for row in table.find_all('tr') if '締切予定時刻' in row.get_text()), None)
        if row is None:
            continue
        races = [int(match.group(1)) for link in table.select('thead a')
                 if (match := re.fullmatch(r'(\d{1,2})R', link.get_text(strip=True)))]
        cells = row.find_all(['th', 'td'])[1:]
        if len(races) != len(cells):
            continue
        result = {}
        for race, cell in zip(races, cells):
            match = re.fullmatch(r'(\d{1,2}):(\d{2})', cell.get_text(strip=True))
            if match and 1 <= race <= 12:
                hour, minute = map(int, match.groups())
                if hour < 24 and minute < 60:
                    result[race] = day.replace(hour=hour, minute=minute)
        return result
    return {}


def exhibition_ready(before):
    # Never confuse previous-race ST, blanks, or incomplete data with a new exhibition.
    return all(boat in before['exhibition'] and boat in before['exhibition_st']
               and 5 <= before['exhibition'][boat] <= 9
               and -1 < before['exhibition_st'][boat] < 1
               for boat in range(1, 7))


class AutoRunner:
    def __init__(self, core, clock=now_jst, persist=write_status):
        self.core = core
        self.clock = clock
        self.persist = persist
        self.schedule_cache = {}
        self.status_lock = threading.Lock()
        self.events = {}
        self.day = None

    def event(self, date, jcd, race, state, message=''):
        with self.status_lock:
            self.events[f'{date}:{jcd}:{race}'] = {
                'date': date, 'stadium': jcd, 'venue': self.core.STADIUMS.get(jcd, jcd),
                'race': race, 'state': state, 'message': message,
                'checked_at': self.clock().isoformat(timespec='seconds'),
            }

    def deadlines(self, date, jcd):
        key = date, jcd
        cached = self.schedule_cache.get(key)
        if cached and time.monotonic() - cached[0] < 120:
            return cached[1]
        html = self.core.get(f'{self.core.BASE}racelist?hd={date}&jcd={jcd}&rno=1', timeout=15)
        result = parse_deadlines(html, date)
        if not result:
            raise ValueError('締切時刻を取得できませんでした。')
        self.schedule_cache[key] = time.monotonic(), result
        return result

    def official_results(self, date, jcd):
        rows = self.core.cached_official_results(date, jcd, int(time.time() // 60))
        return {int(row['race']): row for row in rows
                if row.get('payout') and len(set(row.get('combo', ''))) == 3}

    def settle(self, date, jcd, rows, results):
        latest = {}
        for row in rows:
            if row.get('prediction_origin') == 'automatic':
                latest[self.core.race_key(row)] = row
        for (saved_date, stadium, race), row in latest.items():
            if saved_date != date or stadium != jcd or row.get('learned'):
                continue
            result = results.get(race)
            if result is None:
                self.event(date, jcd, race, 'result_waiting', '公式結果の確定待ちです。')
                continue
            payload, status = self.core.settle_saved_prediction(date, jcd, race, result)
            if status == 200 and payload.get('ok'):
                self.event(date, jcd, race, 'settled', '結果との照合・学習・実績反映が完了しました。')
            else:
                self.event(date, jcd, race, 'retry', payload.get('error', '次回再確認します。'))

    @strict_state_reads
    def process_venue(self, date, venue, rows):
        jcd = venue['stadium']
        try:
            deadlines = self.deadlines(date, jcd)
            results = self.official_results(date, jcd)
            self.settle(date, jcd, rows, results)
            frozen = {self.core.race_key(row)[2] for row in rows
                      if self.core.race_key(row)[:2] == (date, jcd)
                      and row.get('prediction_origin') == 'automatic'}
            for race, deadline in deadlines.items():
                now = self.clock()
                if race in frozen or race in results:
                    continue
                if now >= deadline - timedelta(seconds=90):
                    self.event(date, jcd, race, 'skipped', '締切前の予想を保存できなかったため、実績から除外します。')
                    continue
                if deadline - now > timedelta(minutes=45):
                    continue
                try:
                    before = self.core.parse_before(self.core.get(
                        f'{self.core.BASE}beforeinfo?hd={date}&jcd={jcd}&rno={race}', timeout=15))
                    if not exhibition_ready(before):
                        self.event(date, jcd, race, 'exhibition_waiting', '展示タイム・展示STの公表待ちです。')
                        continue
                    detected = self.clock()
                    prediction = self.core.predict_race(date, jcd, race, before=before)
                    if prediction.get('odds_count', 0) < 120:
                        self.event(date, jcd, race, 'retry', '3連単オッズがそろうまで再確認します。')
                        continue
                    saved = self.core.save_automatic_prediction(
                        date, jcd, race, prediction, deadline, detected, clock=self.clock)
                    if saved:
                        frozen.add(race)
                        self.event(date, jcd, race, 'saved', '展示後の予想を保存しました。結果確定後に自動で反映します。')
                    else:
                        self.event(date, jcd, race, 'skipped', '保存済み、または締切直前のため再予想しません。')
                except Exception:
                    self.core.app.logger.exception('Automatic prediction failed date=%s stadium=%s race=%s', date, jcd, race)
                    self.event(date, jcd, race, 'retry', '公式データまたは保存先との通信を次回再確認します。')
        except Exception:
            self.core.app.logger.exception('Automatic venue check failed date=%s stadium=%s', date, jcd)
            self.event(date, jcd, 0, 'retry', '開催情報・結果を次回再確認します。')

    @strict_state_reads
    def process_backlog(self, date, jcd, rows):
        try:
            self.settle(date, jcd, rows, self.official_results(date, jcd))
        except Exception:
            self.event(date, jcd, 0, 'retry', '保存済み予想の結果を次回再確認します。')

    @strict_state_reads
    def run_once(self):
        now = self.clock()
        date = now.strftime('%Y%m%d')
        if self.day != date:
            self.day = date
            self.events = {}
            self.schedule_cache.clear()
        if not 7 <= now.hour < 23:
            self.persist({'date': date, 'phase': 'waiting', 'message': '次の開催時間を待っています。',
                          'last_checked_at': now.isoformat(timespec='seconds')})
            return
        rows = self.core.load_ledger()
        venues = self.core.get_active_stadiums(date)
        self.persist({'date': date, 'phase': 'checking', 'message': '展示データと確定結果を確認しています。',
                      'predicted': sum(row.get('prediction_origin') == 'automatic' and self.core.race_key(row)[0] == date for row in rows),
                      'settled': sum(row.get('prediction_origin') == 'automatic' and self.core.race_key(row)[0] == date and bool(row.get('learned')) for row in rows),
                      'pending': sum(row.get('prediction_origin') == 'automatic' and self.core.race_key(row)[0] == date and not row.get('learned') for row in rows),
                      'last_checked_at': now.isoformat(timespec='seconds')})
        backlog = {(self.core.race_key(row)[0], self.core.race_key(row)[1]) for row in rows
                   if row.get('prediction_origin') == 'automatic' and not row.get('learned')
                   and (now - timedelta(days=7)).strftime('%Y%m%d') <= self.core.race_key(row)[0] < date}
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(self.process_venue, date, venue, rows) for venue in venues]
            futures += [pool.submit(self.process_backlog, saved_date, jcd, rows) for saved_date, jcd in backlog]
            for future in futures:
                future.result()
        fresh = self.core.load_ledger()
        automatic = [row for row in fresh if row.get('prediction_origin') == 'automatic' and self.core.race_key(row)[0] == date]
        settled = sum(bool(row.get('learned')) for row in automatic)
        with self.status_lock:
            events = list(self.events.values())
        self.persist({'date': date, 'phase': 'watching', 'message': '展示公表後に予想し、結果確定後に自動で照合・学習します。',
                      'last_checked_at': self.clock().isoformat(timespec='seconds'),
                      'predicted': len(automatic), 'settled': settled,
                      'pending': len(automatic) - settled, 'venues': len(venues),
                      'skipped': sum(event['state'] == 'skipped' for event in events),
                      'retries': sum(event['state'] == 'retry' for event in events),
                      'recent': sorted(events, key=lambda event: event['checked_at'], reverse=True)[:12]})

    def run_forever(self):
        while True:
            try:
                with runner_lease() as acquired:
                    if acquired:
                        self.run_once()
            except Exception:
                self.core.app.logger.exception('Automatic polling paused; will retry')
                try:
                    self.persist({'date': self.clock().strftime('%Y%m%d'), 'phase': 'retry',
                                  'message': '通信または保存先の復旧を待っています。次回再確認します。',
                                  'last_checked_at': self.clock().isoformat(timespec='seconds')})
                except Exception:
                    pass
            time.sleep(POLL_SECONDS)


def start_automation(core):
    global _runner
    if not configured():
        return
    with _start_lock:
        if _runner is not None:
            return
        # Load the driver on the app's import thread before a worker can use it.
        # A concurrent first import blocked the first production HTTP request.
        import psycopg
        _runner = AutoRunner(core)
        threading.Thread(target=_runner.run_forever, daemon=True, name='boat-auto-runner').start()
