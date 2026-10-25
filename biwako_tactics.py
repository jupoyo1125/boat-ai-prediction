"""Biwako tactical tendencies from official, prior-day race result archives.

This is a separate, read-only-to-the-forecast exploratory model. It never
modifies existing model weights or saved predictions.
"""
from collections import defaultdict, OrderedDict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import re
import threading
import time
import unicodedata

from historical_archive import archive_url, decode_archive, MAX_COMPRESSED
from racer_course_history import year_window
from state_store import (database_url, state_connection, atomic_write_json,
                         require_storage_writable, storage_paused)

BIWAKO = '11'
# Starts, 1st, 2nd, 3rd, 逃げ, 差され, 捲られ, 逃がし,
# 差し勝ち, 捲り勝ち, 逃げ時2着, 逃げ時3着.
FIELD_COUNT = 12
START, FIRST, SECOND, THIRD, ESCAPE, PASSED, SWEPT, LET_ESCAPE, SASHI, MAKURI, FOLLOW2, FOLLOW3 = range(FIELD_COUNT)
LEASE = 781042026
PATH = Path(__file__).with_name('biwako_tactic_days')
SCHEMA = """
CREATE TABLE IF NOT EXISTS public.boat_tactic_days (
  day TEXT PRIMARY KEY CHECK (day ~ '^[0-9]{8}$'),
  payload JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE public.boat_tactic_days ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.boat_tactic_days FROM PUBLIC;
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
  REVOKE ALL ON public.boat_tactic_days FROM anon;
 END IF;
 IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
  REVOKE ALL ON public.boat_tactic_days FROM authenticated;
 END IF;
END $$;
"""
_schema_url = None
_schema_lock = threading.Lock()
TACTIC_RE = re.compile(r'まくり\s*差\s*し|まくり|差\s*し|逃\s*げ|抜\s*き|恵\s*まれ')
HEADERS = re.compile(r'(?m)^\s*(\d{1,2})R\s+[^\n]*H\d{4}m[^\n]*$')
ROWS = re.compile(r'(?m)^\s{2}(\d{2}|[A-Z]\d?)\s+([1-6])\s+(\d{4,5})\s+([^\n]*)$')


def technique(text):
    """Only look in the race's header, never infer a winning move from payoffs."""
    compact = re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))
    match = TACTIC_RE.search(compact)
    if not match:
        return None
    mode = match.group()
    return ('sashi' if mode in ('差し', 'まくり差し') else
            'makuri' if mode == 'まくり' else
            'escape' if mode == '逃げ' else 'other')


def parse_day(text, date):
    text = unicodedata.normalize('NFKC', text)
    if not text.startswith('STARTK') or not text.rstrip().endswith('FINALK'):
        raise ValueError('公式結果ファイルが不完全です。')
    dates = {f'{int(y):04d}{int(m):02d}{int(d):02d}'
             for y, m, d in re.findall(r'(\d{4})/\s*(\d{1,2})/\s*(\d{1,2})', text)}
    if dates != {date}:
        raise ValueError('公式結果の集計日が一致しません。')
    racers = defaultdict(lambda: [0] * FIELD_COUNT)
    venue_rates = defaultdict(lambda: [0] * FIELD_COUNT)
    seen, valid = set(), 0
    for venue in re.finditer(r'(?m)^(\d{2})KBGN\s*\n([\s\S]*?)^\1KEND', text):
        jcd, body = venue.groups()
        headers = list(HEADERS.finditer(body))
        for i, header in enumerate(headers):
            race = int(header.group(1))
            key = (jcd, race)
            if key in seen or not 1 <= race <= 12:
                raise ValueError('公式結果に重複レースがあります。')
            seen.add(key)
            block = body[header.end():headers[i + 1].start() if i + 1 < len(headers) else len(body)]
            entries = list(ROWS.finditer(block))
            if len(entries) != 6:
                # Refunds and cancellations must not enter denominators.
                continue
            if {m.group(2) for m in entries} != set('123456'):
                continue
            positions = {}
            for match in entries:
                finish, boat, racer, rest = match.groups()
                entry = re.search(r'(?:^|\s)\d\.[0-9]{2}\s+([1-6])(?:\s|$)', rest)
                if not entry:
                    break
                positions[int(boat)] = (int(racer), int(entry.group(1)), int(finish) if finish.isdigit() else 0)
            if len(positions) != 6 or {v[1] for v in positions.values()} != set(range(1, 7)):
                continue
            winners = [(boat, info) for boat, info in positions.items() if info[2] == 1]
            if len(winners) != 1:
                continue
            # The official result's second header line contains the deciding
            # method. Do not scan racer names or payout lines for it.
            lead = header.group(0) + '\n' + block[:entries[0].start()]
            mode = technique(lead)
            if mode is None:
                continue
            winner_boat, (_, winner_course, _) = winners[0]
            was_escape = winner_course == 1 and mode == 'escape'
            for boat, (racer, course, finish) in positions.items():
                for target in (racers[(racer, course)],
                               venue_rates[course] if jcd == BIWAKO else None):
                    if target is None:
                        continue
                    target[START] += 1
                    if 1 <= finish <= 3:
                        target[finish] += 1
                    if course == 1:
                        if was_escape and boat == winner_boat:
                            target[ESCAPE] += 1
                        elif finish != 1 and mode == 'sashi':
                            target[PASSED] += 1
                        elif finish != 1 and mode == 'makuri':
                            target[SWEPT] += 1
                    else:
                        if was_escape:
                            target[LET_ESCAPE] += 1
                            if finish == 2:
                                target[FOLLOW2] += 1
                            if finish == 3:
                                target[FOLLOW3] += 1
                        if finish == 1 and mode == 'sashi':
                            target[SASHI] += 1
                        if finish == 1 and mode == 'makuri':
                            target[MAKURI] += 1
            valid += 1
    if not seen:
        raise ValueError('公式結果のレースがありません。')
    return {'format_version': 1, 'date': date, 'races': valid,
            'racer': [[racer, course, *values]
                      for (racer, course), values in sorted(racers.items())],
            'biwako': [[course, *values] for course, values in sorted(venue_rates.items())]}


def ensure_schema():
    global _schema_url
    url = database_url()
    if not url or _schema_url == url:
        return
    with _schema_lock:
        if _schema_url != url:
            require_storage_writable()
            with state_connection() as conn:
                conn.execute(SCHEMA)
                conn.commit()
            _schema_url = url


def known_days(wanted):
    if database_url():
        ensure_schema()
        with state_connection() as conn:
            conn.execute("SET LOCAL statement_timeout = '6s'")
            return {row[0] for row in conn.execute(
                "SELECT day FROM public.boat_tactic_days WHERE day = ANY(%s) "
                "AND payload->>'format_version' = '1'", (wanted,)).fetchall()}
    return {d for d in wanted if (PATH / (d + '.json')).exists()}


def read_days(wanted):
    if database_url():
        ensure_schema()
        with state_connection() as conn:
            conn.execute("SET LOCAL statement_timeout = '6s'")
            rows = conn.execute(
                'SELECT payload FROM public.boat_tactic_days WHERE day = ANY(%s)',
                (wanted,)).fetchall()
        return [json.loads(r[0]) if isinstance(r[0], str) else r[0] for r in rows]
    return [json.loads((PATH / (d + '.json')).read_text())
            for d in wanted if (PATH / (d + '.json')).exists()]


def write_day(day):
    require_storage_writable()
    if database_url():
        ensure_schema()
        with state_connection() as conn:
            conn.execute("INSERT INTO public.boat_tactic_days (day, payload) VALUES (%s, %s::jsonb) "
                         "ON CONFLICT (day) DO NOTHING",
                         (day['date'], json.dumps(day, separators=(',', ':'))))
            conn.commit()
    else:
        atomic_write_json(PATH / (day['date'] + '.json'), day)


@contextmanager
def lease():
    if not database_url():
        yield True
        return
    import psycopg
    with psycopg.connect(database_url(), connect_timeout=5, autocommit=True) as conn:
        acquired = conn.execute('SELECT pg_try_advisory_lock(%s)', (LEASE,)).fetchone()[0]
        try:
            yield acquired
        finally:
            if acquired:
                conn.execute('SELECT pg_advisory_unlock(%s)', (LEASE,))


def pct(record, index):
    if not record or record[START] == 0:
        return None
    return round(100 * record[index] / record[START], 1)


def modified_rate(base, delta, weight=1):
    if base is None or delta is None:
        return {'base': base, 'estimated': None, 'delta': None}
    estimated = round(min(100, max(0, base + max(-8, min(8, delta * weight)))), 1)
    return {'base': round(base, 1), 'estimated': estimated,
            'delta': round(estimated - base, 1)}


def tactical_rows(entries, before, course_snapshot, summary, readiness):
    venue = summary['biwako']
    competitors = {}
    for boat in range(1, 7):
        entry = entries.get(boat, {})
        course = before.get('entry_course', {}).get(boat)
        # A pre-exhibition lane is not confirmed; display separately.
        provisional = course is None
        course = course or boat
        racer = str(entry.get('racer_id') or '')
        record = summary['racer'].get((racer, course))
        typical = venue.get(course)
        base = (course_snapshot or {}).get('totals', {}).get((racer, course), [0, 0, 0, 0])
        baseline = [round(100 * base[i] / base[0], 1) if base[0] else None for i in (1, 2, 3)]
        competitors[boat] = {'boat': boat, 'name': entry.get('name') or '選手',
                             'racer_id': racer, 'course': course, 'provisional_course': provisional,
                             'starts': record[START] if record else 0,
                             'escape': pct(record, ESCAPE) if course == 1 else None,
                             'passed': pct(record, PASSED) if course == 1 else None,
                             'swept': pct(record, SWEPT) if course == 1 else None,
                             'let_escape': pct(record, LET_ESCAPE) if course > 1 else None,
                             'sashi': pct(record, SASHI) if course > 1 else None,
                             'makuri': pct(record, MAKURI) if course > 1 else None,
                             'baseline_starts': base[0],
                             '_record': record, '_typical': typical,
                             '_baseline': baseline}
    first_course = next((b for b in competitors.values() if b['course'] == 1), None)
    second_course = next((b for b in competitors.values() if b['course'] == 2), None)
    for b in competitors.values():
        actual, venue_course = b.pop('_record'), b.pop('_typical')
        baseline = b.pop('_baseline')
        eligible = (readiness['ready_days'] >= 45 and actual is not None
                    and venue_course is not None and actual[START] >= 12
                    and venue_course[START] >= 60 and b['baseline_starts'] >= 12)
        factor = actual[START] / (actual[START] + 30) if eligible else 0
        if eligible and b['course'] == 1:
            effect = (0.38 * ((pct(actual, ESCAPE) or 0) - (pct(venue_course, ESCAPE) or 0))
                      - 0.22 * ((pct(actual, PASSED) or 0) - (pct(venue_course, PASSED) or 0))
                      - 0.22 * ((pct(actual, SWEPT) or 0) - (pct(venue_course, SWEPT) or 0)))
            if second_course is not None and second_course['let_escape'] is not None:
                typical_2 = venue.get(2)
                if typical_2 and typical_2[START]:
                    effect += 0.24 * (second_course['let_escape'] - (pct(typical_2, LET_ESCAPE) or 0))
            first_delta, second_delta, third_delta = effect * factor, -effect * factor * .35, -effect * factor * .2
        elif eligible:
            effect = (0.5 * ((pct(actual, SASHI) or 0) - (pct(venue_course, SASHI) or 0))
                      + 0.5 * ((pct(actual, MAKURI) or 0) - (pct(venue_course, MAKURI) or 0))
                      - 0.18 * ((pct(actual, LET_ESCAPE) or 0) - (pct(venue_course, LET_ESCAPE) or 0)))
            if first_course and first_course['passed'] is not None:
                v1 = venue.get(1)
                if v1 and v1[START]:
                    effect += .15 * (first_course['passed'] - (pct(v1, PASSED) or 0))
            second_delta = .35 * ((pct(actual, FOLLOW2) or 0) - (pct(venue_course, FOLLOW2) or 0)) * factor
            third_delta = .35 * ((pct(actual, FOLLOW3) or 0) - (pct(venue_course, FOLLOW3) or 0)) * factor
            first_delta = effect * factor
        else:
            first_delta = second_delta = third_delta = None
        b['rates'] = {key: modified_rate(baseline[i], delta)
                      for i, (key, delta) in enumerate(zip(
                          ('first', 'second', 'third'),
                          (first_delta, second_delta, third_delta)))}
        b['sample_warning'] = b['starts'] < 12 or b['baseline_starts'] < 12
    return [competitors[i] for i in range(1, 7)]


class TacticHistory:
    def __init__(self):
        self.lock = threading.RLock()
        self.snapshots = OrderedDict()
        self.failures = {}
        self.worker = None

    def get(self, date):
        wanted = year_window(date)
        with self.lock:
            cached = self.snapshots.get(date)
            if cached and time.monotonic() - cached['loaded_at'] < 60:
                return cached['summary'], cached['readiness']
            present = known_days(wanted)
            totals = defaultdict(lambda: [0] * FIELD_COUNT)
            venue = defaultdict(lambda: [0] * FIELD_COUNT)
            for offset in range(0, len(wanted), 7):
                for day in read_days([x for x in wanted[offset:offset + 7] if x in present]):
                    if day.get('format_version') != 1 or day.get('date') not in wanted:
                        continue
                    for racer, course, *values in day['racer']:
                        dst = totals[(str(racer), int(course))]
                        for i, v in enumerate(values):
                            dst[i] += v
                    for course, *values in day['biwako']:
                        dst = venue[int(course)]
                        for i, v in enumerate(values):
                            dst[i] += v
            summary = {'racer': totals, 'biwako': venue}
            readiness = {'ready_days': len(present), 'total_days': len(wanted),
                         'provisional': len(present) != len(wanted),
                         'start_date': wanted[0], 'end_date': wanted[-1]}
            self.snapshots[date] = {'loaded_at': time.monotonic(), 'summary': summary,
                                    'readiness': readiness}
            while len(self.snapshots) > 2:
                self.snapshots.popitem(last=False)
            return summary, readiness

    def run(self, core):
        # Only archive yesterday or earlier. A read for today's race never
        # includes today's outcomes, even if this thread runs after settlement.
        while True:
            worked = False
            try:
                if not storage_paused():
                    today = datetime.now(timezone(timedelta(hours=9))).strftime('%Y%m%d')
                    wanted = year_window(today)
                    present = known_days(wanted)
                    missing = [d for d in reversed(wanted) if d not in present
                               and self.failures.get(d, 0) <= time.monotonic()]
                    with lease() as acquired:
                        if acquired:
                            for date in missing[:2]:
                                try:
                                    with core._official_slots.slot(timeout=10, priority=1):
                                        with core.official_session().get(
                                                archive_url(date), headers=core.HEAD, timeout=20) as response:
                                            response.raise_for_status()
                                            payload = response.content
                                    if len(payload) > MAX_COMPRESSED:
                                        raise ValueError('公式結果ファイルが大きすぎます。')
                                    write_day(parse_day(decode_archive(payload, date), date))
                                    self.failures.pop(date, None)
                                    worked = True
                                except Exception:
                                    self.failures[date] = time.monotonic() + 300
                                    core.app.logger.exception('Biwako tactics will retry %s', date)
            except Exception:
                core.app.logger.exception('Biwako tactics cache waiting for recovery')
            time.sleep(5 if worked else 60)

    def start(self, core):
        enabled = os.getenv('BOAT_TACTICS_RUNNER', os.getenv('BOAT_AUTO_RUNNER', '1'))
        if enabled.lower() in ('0', 'false', 'no'):
            return
        with self.lock:
            if not self.worker or not self.worker.is_alive():
                self.worker = threading.Thread(target=self.run, args=(core,), daemon=True,
                                               name='biwako-tactics')
                self.worker.start()


history = TacticHistory()
