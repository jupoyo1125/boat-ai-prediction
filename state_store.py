"""Shared transactions for prediction records, learning and scheduler state."""
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
import json
import os
import tempfile
import threading

_local = threading.local()
_mutex = threading.RLock()
STATE_LOCK = 781042017
RUNNER_LOCK = 781042018


class StorageMaintenance(RuntimeError):
    pass


def storage_phase():
    phase = os.getenv('BOAT_STORAGE_PHASE', 'render').strip().lower()
    if phase not in ('render', 'prepare', 'copy', 'supabase'):
        raise RuntimeError('BOAT_STORAGE_PHASE is invalid')
    return phase


def storage_paused():
    return storage_phase() in ('prepare', 'copy')


def require_storage_writable():
    if storage_paused():
        raise StorageMaintenance('保存先の移行中です。データの更新を一時停止しています。')


def database_url():
    if storage_phase() == 'supabase':
        url = os.getenv('BOAT_SUPABASE_URL')
        if not url:
            raise RuntimeError('Supabase connection is not configured')
        return url
    return os.getenv('DATABASE_URL') or os.getenv('POSTGRES_URL')


def in_state_transaction():
    return bool(getattr(_local, 'depth', 0))


def storage_required():
    return bool(getattr(_local, 'strict_reads', False) or storage_phase() != 'render')


def strict_state_reads(function):
    """Automatic processing must stop if PostgreSQL reads fail."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        previous = storage_required()
        _local.strict_reads = True
        try:
            return function(*args, **kwargs)
        finally:
            _local.strict_reads = previous
    return wrapped


class _Connection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, *args, **kwargs):
        return self.connection.execute(*args, **kwargs)

    def commit(self):
        # The outer transaction commits both the ledger and model together.
        if not in_state_transaction():
            self.connection.commit()


@contextmanager
def state_connection(url=None):
    connection = getattr(_local, 'connection', None)
    if connection is not None:
        yield _Connection(connection)
        return
    import psycopg
    with psycopg.connect(url or database_url(), connect_timeout=5) as connection:
        yield _Connection(connection)


@contextmanager
def atomic_state():
    require_storage_writable()
    if in_state_transaction():
        _local.depth += 1
        try:
            yield
        finally:
            _local.depth -= 1
        return
    with _mutex:
        _local.depth = 1
        _local.ledger_snapshot = None
        try:
            url = database_url()
            if url:
                import psycopg
                with psycopg.connect(url, connect_timeout=5) as connection:
                    _local.connection = connection
                    with connection.transaction():
                        connection.execute('SELECT pg_advisory_xact_lock(%s)', (STATE_LOCK,))
                        yield
            else:
                # Local development only. Production automation requires PostgreSQL.
                yield
        finally:
            _local.connection = None
            _local.ledger_snapshot = None
            _local.depth = 0


def state_atomic(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with atomic_state():
            return function(*args, **kwargs)
    return wrapped


def remember_ledger(rows):
    if in_state_transaction():
        _local.ledger_snapshot = {
            str(row['id']): json.dumps(row, sort_keys=True, ensure_ascii=False)
            for row in rows if row.get('id')
        }


def ledger_snapshot():
    return getattr(_local, 'ledger_snapshot', None)


def atomic_write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def runner_lease():
    """Only one polling cycle may run across workers or a rolling deploy."""
    import psycopg
    with psycopg.connect(database_url(), connect_timeout=5, autocommit=True) as connection:
        acquired = connection.execute(
            'SELECT pg_try_advisory_lock(%s)', (RUNNER_LOCK,)
        ).fetchone()[0]
        try:
            yield bool(acquired)
        finally:
            if acquired:
                connection.execute('SELECT pg_advisory_unlock(%s)', (RUNNER_LOCK,))
