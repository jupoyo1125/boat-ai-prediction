"""Durable daily counts, shared annual snapshots and a resumable cache warmer."""
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading
import time

from historical_archive import archive_url, MAX_COMPRESSED
from racer_course_history import year_window, parse_course_archive, aggregate_days
from state_store import (database_url, state_connection, atomic_write_json,
                         storage_paused, require_storage_writable)

PATH = Path(__file__).with_name('racer_course_days')
LEASE = 781042024
SCHEMA = '''
CREATE TABLE IF NOT EXISTS public.boat_racer_course_days (
 day TEXT PRIMARY KEY CHECK (day ~ '^[0-9]{8}$'), payload JSONB NOT NULL,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE public.boat_racer_course_days ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.boat_racer_course_days FROM PUBLIC;
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN
  REVOKE ALL ON public.boat_racer_course_days FROM anon;
 END IF;
 IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN
  REVOKE ALL ON public.boat_racer_course_days FROM authenticated;
 END IF;
END $$;
'''
_schema_url = None
_schema_lock = threading.Lock()


class CourseHistoryPending(RuntimeError):
    def __init__(self, ready, total):
        self.ready, self.total = ready, total
        super().__init__(f'過去1年の選手別コース成績を準備中です（{ready}/{total}日）。準備後に再確認してください。')


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


def read_days(dates):
    if database_url():
        ensure_schema()
        with state_connection() as conn:
            conn.execute("SET LOCAL statement_timeout = '6s'")
            rows = conn.execute('SELECT payload FROM public.boat_racer_course_days WHERE day = ANY(%s)',
                                (dates,)).fetchall()
        return [json.loads(row[0]) if isinstance(row[0], str) else row[0] for row in rows]
    return [json.loads((PATH / f'{date}.json').read_text()) for date in dates
            if (PATH / f'{date}.json').exists()]


def available_days(dates):
    """Read small keys while warming; do not transfer a year on every poll."""
    if database_url():
        ensure_schema()
        with state_connection() as conn:
            conn.execute("SET LOCAL statement_timeout = '6s'")
            return {row[0] for row in conn.execute(
                "SELECT day FROM public.boat_racer_course_days WHERE day = ANY(%s) AND payload->>'format_version' = '1'",
                (dates,)).fetchall()}
    return {date for date in dates if (PATH / (date + '.json')).exists()}


def write_day(day):
    require_storage_writable()
    if database_url():
        ensure_schema()
        with state_connection() as conn:
            conn.execute('''INSERT INTO public.boat_racer_course_days (day, payload) VALUES (%s, %s::jsonb)
                         ON CONFLICT (day) DO NOTHING''', (day['date'], json.dumps(day)))
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


class CourseHistory:
    def __init__(self):
        self.lock = threading.RLock()
        self.snapshots = OrderedDict()
        self.pending = OrderedDict()
        self.failures = {}
        self.worker = None

    def request(self, date):
        year_window(date)  # Validate before enqueueing or forming a file path.
        with self.lock:
            self.pending[date] = True
            while len(self.pending) > 4:
                self.pending.popitem(last=False)

    def get(self, date):
        wanted = year_window(date)
        # Serialize only the initial database read/aggregation, never network I/O.
        with self.lock:
            if date in self.snapshots:
                self.snapshots.move_to_end(date)
                return self.snapshots[date]
            present = available_days(wanted)
            if len(present) != len(wanted):
                self.request(date)
                raise CourseHistoryPending(len(present), len(wanted))
            days = read_days(wanted)
            if len(days) != len(wanted):
                self.request(date)
                raise CourseHistoryPending(len(days), len(wanted))
            snapshot = aggregate_days(days, date)
            self.snapshots[date] = snapshot
            while len(self.snapshots) > 4:
                self.snapshots.popitem(last=False)
            self.pending.pop(date, None)
            return snapshot

    def prepare(self, core, date, limit=3):
        with self.lock:
            if date in self.snapshots:
                self.pending.pop(date, None)
                return False
        wanted = year_window(date)
        present = available_days(wanted)
        missing = [day for day in wanted if day not in present]
        eligible = [day for day in missing if self.failures.get(day, 0) <= time.monotonic()]
        for day in eligible[:limit]:
            if storage_paused():
                return False
            try:
                # The same three-request cap and low priority as historical jobs.
                with core._official_slots.slot(timeout=10, priority=1):
                    with core.official_session().get(archive_url(day), headers=core.HEAD, timeout=20) as response:
                        response.raise_for_status()
                        content = response.content
                if len(content) > MAX_COMPRESSED:
                    raise ValueError('公式結果ファイルが大きすぎます。')
                write_day(parse_course_archive(content, day))
                self.failures.pop(day, None)
            except Exception:
                self.failures[day] = time.monotonic() + 300
                core.app.logger.exception('Course history will retry date=%s', day)
        if not missing:
            self.get(date)
        # Return promptly between small batches so today's newly published day
        # always takes precedence over an older requested backtest window.
        return bool(eligible)

    def run(self, core):
        while True:
            worked = False
            try:
                if not storage_paused():
                    today = datetime.now(timezone(timedelta(hours=9))).strftime('%Y%m%d')
                    self.request(today)
                    with lease() as acquired:
                        if acquired:
                            with self.lock:
                                dates = [today] + [d for d in self.pending if d != today]
                            for date in dates:
                                if self.prepare(core, date):
                                    worked = True
                                    break
            except Exception:
                core.app.logger.exception('Course history cache is waiting for recovery')
            time.sleep(1 if worked else 30)

    def start(self, core):
        enabled = os.getenv('BOAT_COURSE_RUNNER', os.getenv('BOAT_AUTO_RUNNER', '1'))
        if enabled.lower() in ('0', 'false', 'no'):
            return
        if database_url():
            # Initialize the native driver before starting concurrent workers.
            import psycopg  # noqa: F401
        with self.lock:
            if not self.worker or not self.worker.is_alive():
                self.worker = threading.Thread(target=self.run, args=(core,), daemon=True, name='course-history')
                self.worker.start()


history = CourseHistory()
