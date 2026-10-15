import json
import os
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
from data_record_server.storage import Storage
from data_record_server.config import Config, ReportingConfig
from data_record_server.server import create_server
from data_record_server import reporting_service


class ReportingLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.now=datetime(2026,10,15,tzinfo=timezone.utc)
        self.config=ReportingConfig(True,'https://example.test','test','Bearer secret')
        self.service=reporting_service.ReportingService(self.root,self.config,clock=lambda:self.now)
        self.addCleanup(self.service.stop)
        self.storage=Storage(self.root,clock=lambda:self.now,reporting=self.service)

    def append(self):
        return self.storage.append_record(('127.0.0.1',1234),'D1',b'{"deviceId":"D1","gpsTime":1000}\n')

    def test_one_file_one_task_reconnect_and_idle(self):
        session=self.append();self.append()
        rows=self.service.store.recordings();self.assertEqual(len(rows),1)
        self.now+=timedelta(seconds=30);self.storage.expire_sessions(30)
        self.assertEqual(self.service.store.recordings()[0]['state'],'CLOSED')
        self.append();rows=self.service.store.recordings()
        self.assertEqual(len(rows),2)
        self.assertNotEqual(rows[0]['task_id'],rows[1]['task_id'])
        self.storage.close_all_sessions()
        self.assertEqual(self.service.store.recordings()[1]['close_reason'],'SERVER_SHUTDOWN')

    def test_unreported_files_protected_from_retention(self):
        session=self.append();self.storage.close_all_sessions()
        path=self.root/session.relative_path;os.utime(path,(0,0))
        self.storage.cleanup_expired_files(1,1,self.now,'test')
        self.assertTrue(path.exists())

    def test_restart_reconciles_active_file_same_identity(self):
        session=self.append();row=self.service.store.recordings()[0]
        self.service.stop()
        # Simulate process loss after the data flushed, without a lifecycle callback.
        session.abandon()
        recovered=reporting_service.ReportingService(self.root,self.config,clock=lambda:self.now)
        self.addCleanup(recovered.stop)
        self.assertEqual(recovered.store.recordings()[0]['file_key'],row['file_key'])
        self.assertEqual(recovered.store.recordings()[0]['state'],'CLOSED')

    def test_second_reporter_cannot_recover_live_process(self):
        with self.assertRaises(RuntimeError):
            reporting_service.ReportingService(self.root,self.config)

    def test_registration_failure_is_visible_and_not_silent(self):
        self.service.store.register=Mock(side_effect=OSError('disk full'))
        with self.assertRaises(OSError):self.append()
        self.assertEqual(list(self.root.glob('devices/*/*.jsonl')),[])

    def test_close_persistence_failure_retained_for_background_retry(self):
        self.append()
        real=self.service.store.mark_closed
        self.service.store.mark_closed=Mock(side_effect=OSError('disk full'))
        self.storage.close_all_sessions()
        self.assertEqual(self.service.store.recordings()[0]['state'],'OPEN')
        self.service.store.mark_closed=real
        self.service.flush_closures()
        self.assertEqual(self.service.store.recordings()[0]['state'],'CLOSED')

    def test_create_server_injects_service_and_close_releases_lock(self):
        other=self.root/'other'
        config=Config('127.0.0.1',0,other,4096,reporting=self.config)
        server=create_server(config)
        self.assertIsNotNone(server.storage.reporting)
        server.server_close()
        service=reporting_service.ReportingService(other,self.config)
        service.stop()

    def test_delayed_worker_exit_eventually_releases_lease(self):
        import threading
        from dataclasses import replace
        self.service.worker.config=replace(self.config,request_timeout=.01)
        entered=threading.Event();release=threading.Event()
        def slow():entered.set();release.wait(5)
        self.service.worker.prepare=slow
        self.service.start();self.assertTrue(entered.wait(2))
        self.service.stop()
        with self.assertRaises(RuntimeError):
            reporting_service.ReportingService(self.root,self.config)
        release.set()
        self.service.worker._thread.join(3)
        self.assertTrue(self.service.wait_stopped(3))
        replacement=reporting_service.ReportingService(self.root,self.config)
        replacement.stop()

    def test_normal_stop_builds_final_outbox_without_http(self):
        self.append();self.storage.close_all_sessions()
        self.service.stop()
        import sqlite3
        with sqlite3.connect(self.root/'reporting/outbox.sqlite3') as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox WHERE phase='close'").fetchone()[0],1)
