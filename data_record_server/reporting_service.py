"""Collector-facing lifecycle adapter with exclusive process ownership."""

import fcntl
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from .reporting_client import ReportClient
from .reporting_store import ReportStore
from .reporting_worker import ReportWorker

LOGGER = logging.getLogger(__name__)


class ReportingService:
    def __init__(self, root, config, clock=None):
        self.root = Path(root)
        self.config = config
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        directory = self.root / 'reporting'
        directory.mkdir(parents=True, exist_ok=True)
        self._lease = (directory / 'collector.lock').open('a')
        self._closed = False
        self._pending = {}
        self._pending_lock = threading.Lock()
        self.store = None
        try:
            try:
                fcntl.flock(self._lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('another reporting process owns DATA_DIR') from None
            self.store = ReportStore(directory / 'outbox.sqlite3', config.environment, config.base_url)
            self.store.recover(self.clock().isoformat().replace('+00:00','Z'), config.shutdown_reason)
            self.worker = ReportWorker(self.root, self.store, config, ReportClient(config), clock=self.clock)
            self.worker.before_prepare = self.flush_closures
        except BaseException:
            if self.store:
                self.store.close()
            self._lease.close()
            raise

    def register(self, path, device_id, opened_at):
        return self.store.register(path, device_id, opened_at)

    def closed(self, path, reason, when):
        mapped = {'idle_timeout':'IDLE_TIMEOUT', 'server_shutdown':self.config.shutdown_reason}.get(reason, 'ABORTED')
        with self._pending_lock:
            self._pending[path] = (mapped, when)
        self.flush_closures()

    def flush_closures(self):
        with self._pending_lock:
            for path, (reason, when) in list(self._pending.items()):
                try:
                    self.store.mark_closed(path, reason, when)
                except Exception:
                    LOGGER.error('Could not persist recording closure; will retry locally')
                else:
                    del self._pending[path]

    def protected_paths(self):
        return self.store.protected_paths()

    def start(self):
        self.worker.start()

    def stop(self):
        if self._closed:
            return
        self.flush_closures()
        if not self.worker.stop():
            # Keep exclusive ownership until the daemon exits; never close SQLite under a live worker.
            LOGGER.error('Reporting worker still stopping; durable notifications remain pending')
            return
        self.store.close()
        self._lease.close()
        self._closed = True
