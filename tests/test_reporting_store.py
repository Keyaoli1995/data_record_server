import tempfile
import unittest
from pathlib import Path
from data_record_server import reporting_store


class ReportingStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'outbox.sqlite3'
        self.store = reporting_store.ReportStore(self.path, 'test', 'https://example.test')
        self.addCleanup(self.store.close)

    def register(self, path='devices/device-D1/a.jsonl'):
        return self.store.register(path, 'D1', '2026-10-15T00:00:00Z')

    def test_register_persisted_identity_and_create(self):
        one = self.register(); two = self.register()
        self.assertEqual(one['file_key'], two['file_key'])
        self.assertNotEqual(one['file_key'], one['task_id'])
        item = self.store.due(0)[0]
        self.assertEqual(item['phase'], 'create')
        self.assertEqual(item['body']['sequence'], 1)
        self.assertEqual(item['body']['storageUri'], one['path'])
        self.assertEqual(self.store.protected_paths(), {one['path']})
        self.store.close()
        self.store = reporting_store.ReportStore(self.path, 'test', 'https://example.test')
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.due(0)[0]['key'], item['key'])

    def test_stage_order_retry_and_other_file_independence(self):
        one = self.register(); self.store.enqueue(one['file_key'], 'update', {'recordCount': 2})
        self.store.enqueue(one['file_key'], 'close', {'closeReason': 'IDLE_TIMEOUT'})
        first = self.store.due(0)[0]
        self.store.failed(first['id'], 'NETWORK', 10)
        second = self.register('devices/device-D1/b.jsonl')
        self.assertEqual([x['file_key'] for x in self.store.due(1)], [second['file_key']])
        retry = [x for x in self.store.due(10) if x['file_key'] == one['file_key']][0]
        self.assertEqual(first['body'], retry['body']); self.assertEqual(first['key'], retry['key'])
        self.store.ack(first['id'])
        update = self.store.due(10)[0]; self.assertEqual(update['phase'], 'update')
        self.store.ack(update['id'])
        close = self.store.due(10)[0]; self.assertEqual(close['phase'], 'close')
        self.assertGreater(close['body']['sequence'], update['body']['sequence'])
        self.store.ack(close['id'])
        self.assertNotIn(one['path'], self.store.protected_paths())

    def test_blocked_item_retains_file_and_can_be_explicitly_retried(self):
        one = self.register(); item = self.store.due(0)[0]
        self.store.failed(item['id'], 'AUTH', None)
        self.assertEqual(self.store.due(100), [])
        self.assertIn(one['path'], self.store.protected_paths())
        self.store.retry_blocked(one['file_key'])
        self.assertEqual(self.store.due(0)[0]['key'], item['key'])

    def test_restart_closes_open_registrations_and_preserves_reason(self):
        one = self.register(); self.store.mark_closed(one['path'], 'IDLE_TIMEOUT', '2026-10-15T00:01:00Z')
        two = self.register('devices/device-D1/b.jsonl')
        self.store.recover('2026-10-15T00:02:00Z', 'SERVER_SHUTDOWN')
        rows = self.store.recordings()
        self.assertEqual(rows[0]['close_reason'], 'IDLE_TIMEOUT')
        self.assertEqual(rows[1]['close_reason'], 'SERVER_SHUTDOWN')
        self.assertTrue(all(r['state'] == 'CLOSED' for r in rows))

    def test_environment_or_origin_change_rejected(self):
        self.register()
        for env, origin in [('usa', 'https://example.test'), ('test', 'https://other.test')]:
            with self.assertRaises(ValueError):
                reporting_store.ReportStore(self.path, env, origin)

    def test_path_and_duplicate_device_rejected(self):
        for path in ('/etc/passwd', '../a', 'devices/../../a', 'a\\b', ''):
            with self.assertRaises(ValueError):
                self.store.register(path, 'D1', '2026-10-15T00:00:00Z')
        self.register()
        with self.assertRaises(ValueError):
            self.store.register('devices/device-D1/a.jsonl', 'D2', '2026-10-15T00:00:00Z')

    def test_queue_summary_exposes_age_and_payload_bytes(self):
        self.register()
        summary=self.store.summary()[0]
        self.assertIn('oldest_wait_seconds',summary)
        self.assertGreater(summary['payload_bytes'],0)
        self.assertGreaterEqual(summary['oldest_wait_seconds'],0)
