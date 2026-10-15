"""Read recorded files and send durable notifications off the TCP thread."""

import json
import logging
import random
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .reporting_client import ReportError
from .reporting_models import FileStats
from .worksite_timezone import WorksiteTimezone

LOGGER = logging.getLogger(__name__)


class ReportWorker:
    def __init__(self, root, store, config, client, clock=None,
                 zone_factory=WorksiteTimezone, jitter=random.random):
        self.root = Path(root).resolve()
        self.store = store
        self.config = config
        self.client = client
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.zone_factory = zone_factory
        self.jitter = jitter
        self.before_prepare = lambda: None
        self._scanners = {}
        self._stop = threading.Event()
        self._thread = None

    def prepare(self):
        self.before_prepare()
        for row in self.store.recordings():
            if row['state'] not in ('OPEN', 'CLOSED'):
                self._scanners.pop(row['file_key'], None)
                continue
            try:
                self._prepare_file(row)
            except FileNotFoundError:
                # Registration intentionally precedes file creation.
                if row['state'] == 'CLOSED':
                    self.store.block_file(row['file_key'], 'FILE_MISSING')
            except (OSError, ValueError):
                self.store.block_file(row['file_key'], 'FILE_INVALID_OR_UNREADABLE')
                LOGGER.error('Recording scan blocked: fileKey=%s', row['file_key'])

    def _prepare_file(self, row):
        path = self.root / row['path']
        if not path.resolve().is_relative_to(self.root):
            raise ValueError('recording path leaves storage root')
        final = row['state'] == 'CLOSED'
        scanner = self._scanners.get(row['file_key'])
        if scanner is None:
            scanner = FileStats(row['device_id'], self.zone_factory())
            self._scanners[row['file_key']] = scanner
        if final:
            # Verify every original byte after closing, including bytes already scanned while active.
            scanner = FileStats(row['device_id'], scanner.zone)
        scanner.scan(path, final=final)
        snapshot = scanner.snapshot()
        # A restarted process must not send smaller statistics than a durable earlier snapshot.
        if snapshot['sizeBytes'] < row['last_size']:
            raise ValueError('recording is smaller than previously reported')
        saved_audit = json.loads(row['timezone_audit']) if row['timezone_audit'] else None
        if saved_audit and saved_audit.get('name'):
            snapshot['timezone'] = saved_audit['name']
        audit = scanner.zone.audit
        if saved_audit and saved_audit.get('name'):
            if audit.get('name') != saved_audit['name']:
                audit = saved_audit
            else:
                audit['conflict_count'] = max(audit.get('conflict_count', 0), saved_audit.get('conflict_count', 0))
        if final:
            payload = {key: snapshot[key] for key in
                       ('recordCount','sizeBytes','firstGpsTime','lastGpsTime')}
            payload.update(storageUri=row['path'],closedAt=row['closed_at'],sha256=scanner.sha256,
                           closeReason=row['close_reason'] if scanner.valid else 'ABORTED')
            if 'timezone' in snapshot:
                payload['timezone'] = snapshot['timezone']
            self.store.enqueue(row['file_key'], 'close', payload, audit)
            self._scanners.pop(row['file_key'], None)
        elif snapshot['sizeBytes'] > row['last_size'] and snapshot['recordCount']:
            snapshot.pop('firstGpsTime')
            snapshot.update(updatedAt=self.clock().isoformat().replace('+00:00','Z'), status='RECORDING')
            self.store.enqueue(row['file_key'], 'update', snapshot, audit)

    def deliver(self):
        for item in self.store.due(self.clock().timestamp()):
            if self._stop.is_set():
                return
            try:
                self.client.send(item)
            except ReportError as error:
                delay = min(60, 2 ** min(item['attempts'], 6) + self.jitter())
                if error.retry_after is not None:
                    delay = max(delay, error.retry_after)
                when = self.clock().timestamp() + delay if error.retryable else None
                self.store.failed(item['id'], error.code, when)
                LOGGER.warning('Web notification %s: fileKey=%s phase=%s retry=%s',
                               error.code, item['file_key'], item['phase'], error.retryable)
            else:
                self.store.ack(item['id'])

    def start(self):
        self._thread = threading.Thread(target=self._run, name='recording-web-reporter', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(self.config.request_timeout + 1)
            return not self._thread.is_alive()
        return True

    def _run(self):
        next_scan = 0
        next_summary = 0
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                if now >= next_scan:
                    self.prepare()
                    next_scan = now + self.config.update_interval
                self.deliver()
                if now >= next_summary:
                    LOGGER.info('Web notification queue: %s', self.store.summary())
                    next_summary = now + 60
            except Exception:
                # Do not expose remote response bodies/credentials in logs. Retry local queue operations.
                LOGGER.error('Web reporting iteration failed; durable state retained', exc_info=False)
            self._stop.wait(0.25)
