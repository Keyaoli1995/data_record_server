"""Retention must not remove a directory while a session is being opened."""
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from data_record_server.storage import Storage


class StorageCreationRaceTest(unittest.TestCase):
    def test_cleanup_waits_for_pending_session_creation(self):
        with tempfile.TemporaryDirectory() as root:
            registered = threading.Event()
            release = threading.Event()
            scanning = threading.Event()
            errors = []
            reporting = Mock()
            def register(*args):
                registered.set()
                if not release.wait(5):
                    raise TimeoutError('test did not release registration')
            reporting.register.side_effect = register
            now = datetime.now(timezone.utc)
            storage = Storage(Path(root), clock=lambda: now, reporting=reporting)
            directory = Path(root) / 'devices/device-D1'
            def directories():
                scanning.set()
                yield directory
            storage._device_directories = directories
            def append():
                try:
                    storage.append_record(('127.0.0.1', 1234), 'D1', b'{"deviceId":"D1"}\n')
                except Exception as exc:
                    errors.append(exc)
            writer = threading.Thread(target=append)
            writer.start()
            self.assertTrue(registered.wait(2))
            cleaner = threading.Thread(target=storage._cleanup_device_archives,
                args=(365, now, 'test', set()))
            cleaner.start()
            self.assertTrue(scanning.wait(2))
            cleaner.join(.2)
            release.set()
            writer.join(3)
            cleaner.join(3)
            self.assertFalse(writer.is_alive())
            self.assertFalse(cleaner.is_alive())
            self.assertEqual([], errors)
            self.assertEqual(1, len(list(directory.glob('*.jsonl'))))
            storage.close_all_sessions()
