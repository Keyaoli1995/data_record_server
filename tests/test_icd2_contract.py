"""Contracts across the durable queue, runtime path and published Web path."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from data_record_server.config import ReportingConfig
from data_record_server.reporting_service import ReportingService


class PublishedPathTest(unittest.TestCase):
    def test_restart_keeps_create_and_close_uri_despite_root_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = ReportingConfig(True, 'https://example.test', 'test', storage_root='/shared/recordings')
            service = ReportingService(root, config)
            path = 'devices/device-D1/a.jsonl'
            try:
                row = service.register(path, 'D1', '2026-10-19T00:00:00Z')
                raw = root / path
                raw.parent.mkdir(parents=True)
                raw.write_text(json.dumps({'deviceId': 'D1', 'gpsTime': '1792368000123'}) + '\n')
                create = service.store.due(0)[0]
                service.store.ack(create['id'])
                service.store.mark_closed(path, 'IDLE_TIMEOUT', '2026-10-19T00:01:00Z')
            finally:
                service.stop()
            restarted = ReportingService(root, replace(config, storage_root='/new/root'))
            try:
                restarted.worker.prepare()
                close = restarted.store.due(0)[0]
                self.assertEqual('close', close['phase'])
                self.assertEqual('IDLE_TIMEOUT', close['body']['closeReason'])
                self.assertEqual('/shared/recordings/' + path, close['body']['storageUri'])
                self.assertEqual(create['body']['storageUri'], close['body']['storageUri'])
                self.assertEqual(1792368000123, close['body']['firstGpsTime'])
                self.assertEqual(row['file_key'], close['file_key'])
                self.assertEqual(row['task_id'], close['task_id'])
                self.assertNotIn('schemaVersion', close['body'])
            finally:
                restarted.stop()
