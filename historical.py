"""Resumable historical evaluation, isolated from genuine pre-race predictions."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime, timedelta, timezone
import json
import hashlib
import os
from pathlib import Path
import threading
import time

from flask import jsonify, request
import model
from historical_archive import archive_url, decode_archive, parse_daily_results, previous_history, MAX_COMPRESSED
from state_store import (atomic_state, atomic_write_json, database_url, state_connection,
                         storage_paused, require_storage_writable)

JST = timezone(timedelta(hours=9))
PATH = Path(__file__).with_name('historical_state.json')
# 017-020 are reserved for live state, live runner, status setup and migration.
LEASE = 781042021
BATCH_SIZE = 3
_schema_lock = threading.Lock()
_schema_url = None
_context_lock = threading.Lock()
_context_cache = None
_start_lock = threading.Lock()
_worker = None
SCHEMA = '''
CREATE TABLE IF NOT EXISTS public.boat_historical_job (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS public.boat_historical_days (
    date DATE PRIMARY KEY,
    summary JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE public.boat_historical_job ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.boat_historical_days ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN
        REVOKE ALL ON public.boat_historical_job, public.boat_historical_days FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN
        REVOKE ALL ON public.boat_historical_job, public.boat_historical_days FROM authenticated;
    END IF;
END $$;
'''


def now_jst():
    return datetime.now(JST)


def date_window(clock=now_jst):
    end = clock().date() - timedelta(days=1)
    try:
        anniversary = end.replace(year=end.year - 1)
    except ValueError:
        anniversary = end.replace(year=end.year - 1, day=28)
    return (anniversary + timedelta(days=1)).strftime('%Y%m%d'), end.strftime('%Y%m%d')


def empty_stats():
    return {category: {'settled': 0, 'wins': 0, 'bet_count': 0, 'investment': 0, 'payout': 0}
            for category in ('gachi', 'roman', 'oni')}


def new_job(start, end):
    return {'version': 1, 'revision': 0, 'start_date': start, 'end_date': end, 'cursor': start,
            'phase': 'queued', 'enabled': True, 'days_done': 0, 'visited': 0,
            'evaluated': 0, 'missing_odds': 0, 'missing_features': 0, 'excluded_results': 0,
            'model': copy.deepcopy(model.DEFAULT), 'day_model': None,
            'history': [], 'results': None, 'position': 0,
            'category_stats': empty_stats(), 'day_stats': empty_stats(), 'recent': [],
            'failures': 0, 'retry_at': None, 'started_at': now_jst().isoformat(timespec='seconds'),
            'updated_at': None, 'error': None, 'model_applied_at': None}


def ensure_schema():
    global _schema_url
    url = database_url()
    if not url or url == _schema_url:
        return
    with _schema_lock:
        if url != _schema_url:
            require_storage_writable()
            with state_connection() as connection:
                connection.execute(SCHEMA)
                connection.commit()
            _schema_url = url


def context_key(state, has_results):
    return (state['start_date'], state['end_date'], state['cursor'], has_results)


def remember_context(state):
    global _context_cache
    with _context_lock:
        _context_cache = (context_key(state, state['results'] is not None),
                          copy.deepcopy({'history': state['history'], 'results': state['results']}))


def read_job(include_context=True):
    global _context_cache
    if database_url():
        ensure_schema()
        with state_connection() as connection:
            # Avoid sending four days of results over the network on every status
            # poll and checkpoint. Context is immutable until the date advances.
            row = connection.execute("SELECT state - 'history' - 'results', state->'results' <> 'null'::jsonb "
                                     'FROM public.boat_historical_job WHERE id=1').fetchone()
            if not row:
                return None
            state, has_results = row
            if include_context:
                key = context_key(state, bool(has_results))
                with _context_lock:
                    if _context_cache is None or _context_cache[0] != key:
                        context = connection.execute("SELECT state->'history', state->'results' "
                            "FROM public.boat_historical_job WHERE id=1 AND state->>'cursor'=%s "
                            "AND (state->'results' <> 'null'::jsonb)=%s", (state['cursor'], bool(has_results))).fetchone()
                        if context is None:
                            raise RuntimeError('Historical checkpoint changed while reading its context; retry safely')
                        _context_cache = (key, {'history': context[0], 'results': context[1]})
                    state.update(copy.deepcopy(_context_cache[1]))
            return state
    if PATH.exists():
        return json.loads(PATH.read_text(encoding='utf-8'))
    return None


def write_job(state, daily=None):
    require_storage_writable()
    state['updated_at'] = now_jst().isoformat(timespec='seconds')
    if database_url():
        ensure_schema()
        with state_connection() as connection:
            if daily is not None:
                connection.execute('''INSERT INTO public.boat_historical_days (date, summary)
                    VALUES (%s, %s::jsonb) ON CONFLICT (date) DO UPDATE
                    SET summary=EXCLUDED.summary, updated_at=NOW()''',
                    (daily['date'], json.dumps(daily, ensure_ascii=False)))
            connection.execute('''INSERT INTO public.boat_historical_job (id, state) VALUES (1, %s::jsonb)
                ON CONFLICT (id) DO UPDATE SET state=EXCLUDED.state, updated_at=NOW()''',
                (json.dumps(state, ensure_ascii=False),))
            connection.commit()
        remember_context(state)
    else:
        atomic_write_json(PATH, state)


def checkpoint(previous, updated, daily=None):
    # Never hold a database transaction during an official-site request.
    with atomic_state():
        current = read_job(include_context=False)
        if not current or current['revision'] != previous['revision'] or not current['enabled']:
            return False
        updated['revision'] = previous['revision'] + 1
        write_job(updated, daily)
    return True


def rates(stats):
    return {category: dict(record, races=record['settled'], pending=0,
                           profit=record['payout'] - record['investment'],
                           roi=record['payout'] / record['investment'] * 100 if record['investment'] else None,
                           hit_rate=record['wins'] / record['settled'] * 100 if record['settled'] else None)
            for category, record in stats.items()}


def public_status(state=None):
    state = read_job(include_context=False) if state is None else state
    if state is None:
        start, end = date_window()
        return {'ok': True, 'exists': False, 'start_date': start, 'end_date': end,
                'category_stats': rates(empty_stats())}
    total = (datetime.strptime(state['end_date'], '%Y%m%d')
             - datetime.strptime(state['start_date'], '%Y%m%d')).days + 1
    result = {key: state.get(key) for key in ('start_date', 'end_date', 'cursor', 'phase', 'enabled',
              'days_done', 'visited', 'evaluated', 'missing_odds', 'missing_features', 'excluded_results',
              'started_at', 'updated_at', 'retry_at', 'error', 'model_applied_at')}
    result.update(ok=True, exists=True, total_days=total,
                  progress=round(state['days_done'] / total * 100, 2),
                  samples=state['model']['samples'], first_hits=state['model']['hits'],
                  category_stats=rates(state['category_stats']), recent=state['recent'],
                  evaluation_basis='previous_day_model_and_closing_odds')
    return result


@contextmanager
def lease():
    if not database_url():
        yield True
        return
    import psycopg
    with psycopg.connect(database_url(), connect_timeout=5, autocommit=True) as connection:
        acquired = connection.execute('SELECT pg_try_advisory_lock(%s)', (LEASE,)).fetchone()[0]
        try:
            yield bool(acquired)
        finally:
            if acquired:
                connection.execute('SELECT pg_advisory_unlock(%s)', (LEASE,))


class HistoricalRunner:
    def __init__(self, core, fetch_results=None, fetch_html=None):
        self.core = core
        self.fetch_results = fetch_results or self.download_results
        self.fetch_html = fetch_html or self.download_html

    def download_html(self, url):
        # One historical request at a time leaves slots for the live runner.
        time.sleep(.25)
        return self.core.get(url, timeout=15)

    def download_results(self, date):
        time.sleep(.25)
        with self.core._official_slots:
            with self.core.official_session().get(archive_url(date), headers=self.core.HEAD,
                                                  timeout=15, stream=True) as response:
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_content(65536):
                    content.extend(chunk)
                    if len(content) > MAX_COMPRESSED:
                        raise ValueError('公式結果ファイルのサイズが上限を超えました。')
        return parse_daily_results(decode_archive(bytes(content), date), date)

    def prepare_day(self, state):
        updated = copy.deepcopy(state)
        date = state['cursor']
        if not updated['history']:
            target = datetime.strptime(date, '%Y%m%d')
            updated['history'] = [{'date': day.strftime('%Y%m%d'),
                                   'results': self.fetch_results(day.strftime('%Y%m%d'))}
                                  for day in (target - timedelta(days=i) for i in (3, 2, 1))]
        updated['results'] = self.fetch_results(date)
        updated['day_model'] = copy.deepcopy(updated['model'])
        updated['position'] = 0
        updated['day_stats'] = empty_stats()
        updated['day_counts'] = {'visited': 0, 'samples': 0, 'evaluated': 0,
                                 'missing_odds': 0, 'missing_features': 0, 'excluded_results': 0}
        updated.update(phase='running', error=None, failures=0, retry_at=None)
        return checkpoint(state, updated)

    def forecast(self, date, result, hist, day_model):
        jcd, race = result['stadium'], result['race']
        prefix = f'{self.core.BASE}'
        query = f'?hd={date}&jcd={jcd}&rno={race:02d}'
        entry_html = self.fetch_html(prefix + 'racelist' + query)
        before_html = self.fetch_html(prefix + 'beforeinfo' + query)
        if '出走表' not in entry_html or '直前情報' not in before_html:
            raise ValueError('公式の出走表・直前情報のページを確認できませんでした。')
        raw = self.core.boats_from(entry_html)
        before = self.core.parse_before(before_html)
        if (set(raw) != set(range(1, 7))
                or set(before['exhibition']) != set(range(1, 7))
                or set(before['exhibition_st']) != set(range(1, 7))):
            return None
        boats = self.core.analyze(raw, 'none', before, hist, model=day_model)
        if any(row[key] is None for row in boats for key in ('nation', 'local', 'motor', 'st')):
            return None
        html = self.fetch_html(prefix + 'odds3t' + query)
        if 'データはありません' in html or 'データがありません' in html:
            odds = None
        else:
            # A parser failure is retried, not silently counted as missing historical odds.
            odds = self.core.parse_odds(html)
            if ('締切時オッズ' not in html
                    or sum(value is not None and value > 0 for value in odds.values()) != 120):
                odds = None
        bets = (self.core.build_bets(boats, odds, 'none', self.core.scenario(boats, before), model=day_model)
                if odds is not None else None)
        return {'main': max(boats, key=lambda row: row['score'])['boat'],
                'features': self.core.feature_snapshot(boats), 'bets': bets}

    def process_batch(self, state):
        updated = copy.deepcopy(state)
        updated['phase'] = 'running'
        date = state['cursor']
        hist_cache = {}
        targets = state['results'][state['position']:state['position'] + BATCH_SIZE]
        for official in targets:
            jcd = official['stadium']
            hist_cache[jcd] = previous_history(state['history'], date, jcd)
        forecasts = {}
        # Live predictions get priority during race hours. Overnight, up to three
        # races can use the shared three-request cap with the same frozen model.
        workers = 1 if 7 <= now_jst().hour < 23 else 3
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [(index, pool.submit(self.forecast, date, official,
                       hist_cache[official['stadium']], state['day_model']))
                       for index, official in enumerate(targets) if not official.get('excluded')]
            for index, future in futures:
                forecasts[index] = future.result()
        for index, official in enumerate(targets):
            updated['visited'] += 1
            updated['day_counts']['visited'] += 1
            jcd = official['stadium']
            if official.get('excluded'):
                updated['excluded_results'] += 1
                updated['day_counts']['excluded_results'] += 1
            else:
                prediction = forecasts[index]
                if prediction is None:
                    updated['missing_features'] += 1
                    updated['day_counts']['missing_features'] += 1
                else:
                    actual = int(official['combo'][0])
                    features = prediction['features']
                    # Keep the evaluation model frozen for the whole day; learn for the next day.
                    model.learn_from_features(updated['model'], features[str(prediction['main'])], actual,
                                              prediction['main'], features[str(actual)], persist=False)
                    updated['day_counts']['samples'] += 1
                    if prediction['bets'] is None:
                        updated['missing_odds'] += 1
                        updated['day_counts']['missing_odds'] += 1
                    else:
                        updated['evaluated'] += 1
                        updated['day_counts']['evaluated'] += 1
                        row = {'performance_version': 2, 'bets': prediction['bets'], 'settled': True,
                               'actual_combo': official['combo'], 'official_payout': official['payout']}
                        for category, record in self.core.category_race_results(row).items():
                            for stats in (updated['category_stats'][category], updated['day_stats'][category]):
                                stats['settled'] += int(record['settled'])
                                stats['wins'] += int(record['hit'])
                                stats['bet_count'] += record['bet_count']
                                stats['investment'] += record['investment']
                                stats['payout'] += record['payout']
                    updated['recent'].append({'date': date, 'stadium': jcd, 'race': official['race'],
                                              'main': prediction['main'], 'actual_combo': official['combo'],
                                              'official_payout': official['payout'],
                                              'evaluated': prediction['bets'] is not None})
                    updated['recent'] = updated['recent'][-12:]
            updated['position'] += 1
        daily = None
        if updated['position'] == len(updated['results']):
            daily = dict(date=date, counts=updated['day_counts'], category_stats=updated['day_stats'],
                         model_samples_before=state['day_model']['samples'],
                         model_samples_after=updated['model']['samples'], source=archive_url(date),
                         model_before=state['day_model'], model_after=updated['model'],
                         result_sha256=hashlib.sha256(json.dumps(updated['results'], sort_keys=True).encode()).hexdigest(),
                         evaluation_basis='previous_day_model_and_closing_odds')
            updated['history'] = (updated['history'] + [{'date': date, 'results': updated['results']}])[-3:]
            updated['results'] = None
            updated['day_model'] = None
            updated['position'] = 0
            updated['days_done'] += 1
            if date == updated['end_date']:
                updated.update(phase='completed', enabled=False)
            else:
                updated['cursor'] = (datetime.strptime(date, '%Y%m%d') + timedelta(days=1)).strftime('%Y%m%d')
        updated.update(error=None, failures=0, retry_at=None)
        return checkpoint(state, updated, daily)

    def run_once(self):
        state = read_job()
        if not state or not state['enabled'] or storage_paused():
            return False
        if state.get('retry_at') and now_jst() < datetime.fromisoformat(state['retry_at']):
            return False
        try:
            return self.prepare_day(state) if state['results'] is None else self.process_batch(state)
        except Exception:
            self.core.app.logger.exception('Historical processing will resume at its saved checkpoint')
            updated = copy.deepcopy(state)
            updated['failures'] += 1
            stopped = updated['failures'] >= 5
            updated.update(phase='error' if stopped else 'retry', enabled=not stopped,
                           error='公式データまたは保存先との通信を確認できませんでした。保存済みの位置から再開できます。',
                           retry_at=(now_jst() + timedelta(seconds=min(900, 60 * 2 ** (updated['failures'] - 1))))
                                    .isoformat(timespec='seconds'))
            checkpoint(state, updated)
            return False

    def run_forever(self):
        while True:
            worked = False
            try:
                if not storage_paused():
                    with lease() as acquired:
                        if acquired:
                            worked = self.run_once()
            except Exception:
                self.core.app.logger.exception('Historical worker is waiting for database recovery')
            time.sleep(.5 if worked else 30)


def control(core, action):
    with atomic_state():
        state = read_job()
        if action == 'start' and state is None:
            state = new_job(*date_window())
        elif state is None:
            raise ValueError('先に過去1年の検証を開始してください。')
        elif action in ('start', 'resume'):
            if state['phase'] == 'completed':
                return public_status(state)
            state.update(enabled=True, phase='queued', failures=0, retry_at=None, error=None)
        elif action == 'pause':
            if state['phase'] != 'completed':
                state.update(enabled=False, phase='paused')
        elif action == 'apply_model':
            if state['phase'] != 'completed' or state['model']['samples'] < 1000:
                raise ValueError('1年分の処理完了後、学習対象が1,000レース以上ある場合に適用できます。')
            if not state.get('model_applied_at'):
                live = core.load_model()
                state['model_backup'] = copy.deepcopy(live)
                live['weights'] = copy.deepcopy(state['model']['weights'])
                live['temperature'] = state['model']['temperature']
                live['historical_training'] = {'samples': state['model']['samples'],
                    'start_date': state['start_date'], 'end_date': state['end_date'],
                    'applied_at': now_jst().isoformat(timespec='seconds')}
                # Existing live counters are preserved; overlapping races are never added to them.
                core.save_model(live)
                state['model_applied_at'] = live['historical_training']['applied_at']
        else:
            raise ValueError('開始・停止・再開・モデル適用のいずれかを指定してください。')
        state['revision'] += 1
        write_job(state)
    return public_status(state)


def register_historical(core):
    @core.app.get('/api/historical')
    def status():
        response = jsonify(public_status())
        response.headers['Cache-Control'] = 'no-store'
        return response

    @core.app.post('/api/historical')
    def update():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(ok=False, error='JSONで操作を指定してください。'), 400
        try:
            return jsonify(control(core, data.get('action', 'start')))
        except ValueError as error:
            return jsonify(ok=False, error=str(error)), 400

    global _worker
    if (not os.getenv('RENDER_SERVICE_ID') or not database_url()
            or os.getenv('BOAT_HISTORY_RUNNER', os.getenv('BOAT_AUTO_RUNNER', '1')) == '0'):
        return
    with _start_lock:
        if _worker is None:
            import psycopg
            _worker = HistoricalRunner(core)
            threading.Thread(target=_worker.run_forever, daemon=True, name='boat-history-runner').start()
