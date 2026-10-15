"""Durable notification identities and per-file ordered outbox.

Only local transactions run under the lock. Never hold it during HTTP or file scans.
"""

import json
import sqlite3
import threading
import uuid
from pathlib import Path, PurePosixPath


def safe_relative_path(value):
    if (not isinstance(value, str) or not value or '\\' in value or '\x00' in value
            or PurePosixPath(value).is_absolute() or '..' in value.split('/')
            or '.' in value.split('/') or '' in value.split('/')):
        raise ValueError('storageUri must be a normalized relative path')
    return value


class ReportStore:
    def __init__(self, path, environment, origin):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute('PRAGMA journal_mode=WAL')
            self._db.execute('PRAGMA synchronous=FULL')
            self._db.executescript('''
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS recordings (
                    file_key TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE,
                    path TEXT NOT NULL UNIQUE, device_id TEXT NOT NULL, opened_at TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'OPEN', closed_at TEXT, close_reason TEXT,
                    sequence INTEGER NOT NULL DEFAULT 0, last_size INTEGER NOT NULL DEFAULT -1,
                    timezone_audit TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY, file_key TEXT NOT NULL, phase TEXT NOT NULL,
                    key TEXT NOT NULL UNIQUE, body TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
                    next_at REAL NOT NULL DEFAULT 0, error TEXT);
                CREATE INDEX IF NOT EXISTS outbox_file_order ON outbox(file_key,id,state);
            ''')
            with self._db:
                for key, value in (('environment', environment), ('origin', origin), ('schema', '1')):
                    old = self._db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
                    if old and old[0] != value:
                        raise ValueError('reporting database ' + key + ' does not match configuration')
                    self._db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', (key, value))
        except BaseException:
            self._db.close()
            raise

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def register(self, path, device_id, opened_at):
        safe_relative_path(path)
        with self._lock, self._db:
            row = self._db.execute('SELECT * FROM recordings WHERE path=?', (path,)).fetchone()
            if row:
                if row['device_id'] != device_id:
                    raise ValueError('file already registered to another device')
                return dict(row)
            file_key, task_id = uuid.uuid4().hex, uuid.uuid4().hex
            self._db.execute('INSERT INTO recordings(file_key,task_id,path,device_id,opened_at) VALUES (?,?,?,?,?)',
                             (file_key, task_id, path, device_id, opened_at))
            self._insert(file_key, 'create', dict(schemaVersion=1, rakingRecordingTaskId=task_id,
                         fileKey=file_key, rakingLocatorSn=device_id, sequence=1,
                         storageUri=path, openedAt=opened_at))
            return dict(self._db.execute('SELECT * FROM recordings WHERE file_key=?', (file_key,)).fetchone())

    def _insert(self, file_key, phase, body):
        self._db.execute('INSERT INTO outbox(file_key,phase,key,body) VALUES (?,?,?,?)',
                         (file_key, phase, uuid.uuid4().hex,
                          json.dumps(body, ensure_ascii=True, allow_nan=False, sort_keys=True)))

    def enqueue(self, file_key, phase, body, audit=None):
        if phase not in ('update', 'close'):
            raise ValueError('invalid notification phase')
        with self._lock, self._db:
            row = self._db.execute('SELECT * FROM recordings WHERE file_key=?', (file_key,)).fetchone()
            if row is None:
                raise ValueError('unknown file')
            if row['state'] in ('FINALIZED', 'DONE', 'ERROR'):
                return False
            # Keep at most one undelivered periodic snapshot; closing is never dropped.
            if phase == 'update' and self._db.execute(
                    "SELECT 1 FROM outbox WHERE file_key=? AND phase='update' AND state!='SENT'",
                    (file_key,)).fetchone():
                return False
            sequence = row['sequence'] + 1
            payload = dict(body, schemaVersion=1, sequence=sequence)
            self._insert(file_key, phase, payload)
            self._db.execute('UPDATE recordings SET sequence=?,last_size=?,timezone_audit=COALESCE(?,timezone_audit),state=? WHERE file_key=?',
                             (sequence, body.get('sizeBytes', row['last_size']),
                              json.dumps(audit) if audit else None,
                              'FINALIZED' if phase == 'close' else row['state'], file_key))
            return True

    def mark_closed(self, path, reason, closed_at):
        with self._lock, self._db:
            self._db.execute("UPDATE recordings SET state='CLOSED',close_reason=?,closed_at=? WHERE path=? AND state='OPEN'",
                             (reason, closed_at, path))

    def recover(self, closed_at, reason):
        with self._lock, self._db:
            self._db.execute("UPDATE recordings SET state='CLOSED',closed_at=?,close_reason=? WHERE state='OPEN'",
                             (closed_at, reason))

    def recordings(self):
        with self._lock:
            return [dict(r) for r in self._db.execute('SELECT * FROM recordings ORDER BY rowid')]

    def due(self, now):
        with self._lock:
            rows = self._db.execute('''SELECT o.*,r.task_id FROM outbox o JOIN recordings r USING(file_key)
                WHERE o.state='PENDING' AND o.next_at<=? AND r.state!='ERROR'
                AND NOT EXISTS(SELECT 1 FROM outbox p WHERE p.file_key=o.file_key AND p.id<o.id AND p.state!='SENT')
                ORDER BY o.id''', (now,)).fetchall()
            return [dict(r) | {'body': json.loads(r['body'])} for r in rows]

    def ack(self, item_id):
        with self._lock, self._db:
            self._db.execute("UPDATE outbox SET state='SENT',error=NULL WHERE id=?", (item_id,))
            row = self._db.execute('SELECT * FROM outbox WHERE id=?', (item_id,)).fetchone()
            if row and row['phase'] == 'close':
                self._db.execute("UPDATE recordings SET state='DONE' WHERE file_key=?", (row['file_key'],))

    def failed(self, item_id, error, next_at):
        with self._lock, self._db:
            self._db.execute('UPDATE outbox SET attempts=attempts+1,error=?,next_at=?,state=? WHERE id=?',
                             (error, next_at or 0, 'BLOCKED' if next_at is None else 'PENDING', item_id))

    def block_file(self, file_key, error):
        with self._lock, self._db:
            self._db.execute("UPDATE recordings SET state='ERROR',error=? WHERE file_key=?", (error, file_key))

    def retry_blocked(self, file_key):
        with self._lock, self._db:
            if self._db.execute("SELECT 1 FROM outbox WHERE file_key=? AND error IN ('RakingRecordingTaskTimedOut','RakingFileAlreadyFinished')", (file_key,)).fetchone():
                raise ValueError('remote terminal task requires Web reconciliation, not ordinary retry')
            if not self._db.execute('SELECT 1 FROM recordings WHERE file_key=?', (file_key,)).fetchone():
                raise ValueError('unknown fileKey')
            self._db.execute("UPDATE outbox SET state='PENDING',next_at=0 WHERE file_key=? AND state='BLOCKED'", (file_key,))

    def protected_paths(self):
        with self._lock:
            return {r[0] for r in self._db.execute("SELECT path FROM recordings WHERE state!='DONE'")}

    def summary(self):
        with self._lock:
            return [dict(r) for r in self._db.execute(
                'SELECT state,COUNT(*) AS count,MIN(id) AS oldest_id FROM outbox GROUP BY state')]
