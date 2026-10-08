"""Explicit, staged migration from Render's private DB to Supabase session mode.

prepare: all application writes/automatic processing are paused; wait for the
         deployment to replace old workers before changing to copy.
copy:    copy and verify all three tables, keep application writes paused.
supabase: verify the copy once, then use only the destination database.
The original DATABASE_URL is kept unchanged for recovery before activation.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from state_store import RUNNER_LOCK, STATE_LOCK, storage_phase

MIGRATION_ID = 'render-to-supabase-v1'
TABLES = {
    'performance_ledger': ('record', 'created_at'),
    'ai_model_state': ('state', 'updated_at'),
    'boat_automation_state': ('state', 'updated_at'),
}
MIGRATION_LOCK = 781042020
_report = {'phase': 'render'}


class MigrationError(RuntimeError):
    """Messages must not include connection strings or driver exception text."""


def destination_url():
    url = os.getenv('BOAT_SUPABASE_URL', '')
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme in ('postgres', 'postgresql')
                 and (parsed.hostname or '').endswith('.pooler.supabase.com')
                 and parsed.port == 5432 and parsed.username and parsed.password
                 and parsed.path == '/postgres'
                 and parse_qs(parsed.query).get('sslmode', [''])[0]
                 in ('require', 'verify-ca', 'verify-full'))
    except ValueError:
        valid = False
    if not valid:
        raise MigrationError('Use a TLS-protected Supabase Session pooler connection on port 5432')
    return url


def source_url():
    url = os.getenv('DATABASE_URL') or os.getenv('POSTGRES_URL')
    if not url:
        raise MigrationError('The original database connection is required')
    if url == os.getenv('BOAT_SUPABASE_URL'):
        raise MigrationError('Source and destination must be different databases')
    return url


def source_identity(url):
    parsed = urlsplit(url)
    # Credentials are deliberately excluded, including from the digest input.
    identity = [parsed.hostname, parsed.port or 5432, parsed.path]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def read_snapshot(connection):
    snapshot = {}
    for table, (payload, timestamp) in TABLES.items():
        rows = connection.execute(
            f'SELECT id, {payload}, {timestamp} FROM public.{table} ORDER BY id'
        ).fetchall()
        snapshot[table] = [(row_id, json.loads(value) if isinstance(value, str) else value,
                            stamp.astimezone(timezone.utc).isoformat(timespec='microseconds'))
                           for row_id, value, stamp in rows]
    return snapshot


def manifest(snapshot):
    return {table: {
        'count': len(rows),
        'sha256': hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True,
                                           separators=(',', ':'), allow_nan=False).encode()).hexdigest(),
    } for table, rows in snapshot.items()}


def summary(snapshot):
    models = snapshot['ai_model_state']
    if len(models) != 1 or models[0][0] != 1 or not isinstance(models[0][1], dict):
        raise MigrationError('A saved model is required; an empty/default model cannot be migrated')
    if not isinstance(models[0][1].get('weights'), dict) or not models[0][1]['weights']:
        raise MigrationError('The saved model has no weights')
    automatic = [row[1] for row in snapshot['performance_ledger']
                 if row[1].get('prediction_origin') == 'automatic']
    learned = sum(bool(row.get('learned')) for row in automatic)
    return {'records': len(snapshot['performance_ledger']),
            'model_samples': models[0][1].get('samples'),
            'automatic_predictions': len(automatic), 'automatic_learned': learned,
            'automatic_pending': len(automatic) - learned}


def read_receipt(connection):
    row = connection.execute(
        'SELECT receipt FROM public.boat_storage_migrations WHERE id=%s', (MIGRATION_ID,)
    ).fetchone()
    return (json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])) if row else None


def store_receipt(connection, receipt):
    from psycopg.types.json import Jsonb
    connection.execute('''INSERT INTO public.boat_storage_migrations (id, receipt)
        VALUES (%s, %s) ON CONFLICT (id) DO UPDATE SET receipt=EXCLUDED.receipt''',
        (MIGRATION_ID, Jsonb(receipt)))


def copy_snapshot(source, destination, identity):
    """Caller holds source runner lease and both databases' state locks."""
    from psycopg.types.json import Jsonb
    snapshot = read_snapshot(source)
    counts = summary(snapshot)
    expected = manifest(snapshot)
    receipt = read_receipt(destination)
    current = read_snapshot(destination)
    if receipt:
        if receipt.get('activated_at'):
            raise MigrationError('The destination is already active; copying again is prohibited')
        if receipt.get('source') != identity or receipt.get('tables') != expected:
            raise MigrationError('The source changed after the copy; keep writes paused')
        if manifest(current) != expected:
            raise MigrationError('The destination does not match the verified copy')
        return receipt
    if any(current.values()):
        raise MigrationError('The destination contains data; existing records will not be overwritten')
    for table, (payload, timestamp) in TABLES.items():
        for row_id, value, stamp in snapshot[table]:
            destination.execute(
                f'INSERT INTO public.{table} (id, {payload}, {timestamp}) VALUES (%s, %s, %s)',
                (row_id, Jsonb(value), datetime.fromisoformat(stamp)))
    if manifest(read_snapshot(destination)) != expected:
        raise MigrationError('Verification failed; the destination transaction will be rolled back')
    receipt = {'version': 1, 'source': identity, 'tables': expected, 'summary': counts,
               'copied_at': datetime.now(timezone.utc).isoformat(), 'activated_at': None}
    store_receipt(destination, receipt)
    return receipt


def activate_destination(connection):
    receipt = read_receipt(connection)
    if not receipt or receipt.get('version') != 1:
        raise MigrationError('A verified migration receipt is required before activation')
    if not receipt.get('activated_at'):
        snapshot = read_snapshot(connection)
        if manifest(snapshot) != receipt.get('tables'):
            raise MigrationError('The copied data changed before activation')
        summary(snapshot)
        receipt['activated_at'] = datetime.now(timezone.utc).isoformat()
        store_receipt(connection, receipt)
    return receipt


def initialize_storage():
    """Run before starting any application/background worker; default is a no-op."""
    global _report
    phase = storage_phase()
    _report = {'phase': phase}
    if phase in ('render', 'prepare'):
        return
    import psycopg
    target = destination_url()
    options = '-c lock_timeout=5000 -c statement_timeout=60000'
    try:
        with psycopg.connect(target, connect_timeout=10, options=options) as destination:
            with destination.transaction():
                destination.execute('SELECT pg_advisory_xact_lock(%s)', (MIGRATION_LOCK,))
                destination.execute('SELECT pg_advisory_xact_lock(%s)', (STATE_LOCK,))
                if phase == 'copy':
                    original = source_url()
                    with psycopg.connect(original, connect_timeout=10, options=options) as source:
                        with source.transaction():
                            source.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
                            acquired = source.execute(
                                'SELECT pg_try_advisory_xact_lock(%s)', (RUNNER_LOCK,)
                            ).fetchone()[0]
                            if not acquired:
                                raise MigrationError('An old automatic cycle is running; wait for prepare to finish')
                            source.execute('SELECT pg_advisory_xact_lock(%s)', (STATE_LOCK,))
                            destination.execute(Path(__file__).with_name('storage_schema.sql').read_text())
                            receipt = copy_snapshot(source, destination, source_identity(original))
                else:
                    receipt = activate_destination(destination)
        _report.update(verified=True, **receipt['summary'])
    except MigrationError:
        raise
    except Exception:
        # Driver exceptions may contain host names or credentials. Never log them.
        raise MigrationError('Storage migration/verification failed; no fallback database is used') from None


def storage_report():
    return dict(_report)
