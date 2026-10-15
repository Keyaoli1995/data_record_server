import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import Mock
from data_record_server import reporting_worker
from data_record_server.reporting_store import ReportStore
from data_record_server.reporting_client import ReportError
from data_record_server.config import ReportingConfig
from data_record_server.worksite_timezone import WorksiteTimezone

NOW = datetime(2026,10,15,tzinfo=timezone.utc)


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.store=ReportStore(self.root/'reporting/outbox.sqlite3','test','https://example.test')
        self.addCleanup(self.store.close)
        self.config=ReportingConfig(True,'https://example.test','test','Bearer secret')
        self.client=Mock()
        self.worker=reporting_worker.ReportWorker(self.root,self.store,self.config,self.client,
                         clock=lambda:NOW,zone_factory=lambda:WorksiteTimezone(lambda lat,lon:'Asia/Shanghai'), jitter=lambda:0)

    def file(self, path='devices/device-D1/a.jsonl', raw=None):
        row=self.store.register(path,'D1',NOW.isoformat())
        file=self.root/path;file.parent.mkdir(parents=True,exist_ok=True)
        file.write_bytes(raw if raw is not None else b''.join((json.dumps(dict(deviceId='D1',gpsTime=1000+i*1000,
               GNSSlat=28.2,GNSSlon=112.8,GNSSPosStat='4'))+'\n').encode() for i in range(3)))
        return row,file

    def test_update_and_close_payloads(self):
        row,file=self.file(); self.worker.prepare()
        self.worker.deliver(); self.worker.deliver()
        update=self.client.send.call_args[0][0]
        self.assertEqual(update['phase'],'update')
        self.assertEqual(update['body']['timezone'],'Asia/Shanghai')
        self.assertNotIn('firstGpsTime',update['body'])
        self.store.mark_closed(row['path'],'IDLE_TIMEOUT',NOW.isoformat())
        self.worker.prepare(); self.worker.deliver()
        close=self.client.send.call_args[0][0]
        self.assertEqual(close['phase'],'close')
        self.assertEqual(close['body']['sha256'],hashlib.sha256(file.read_bytes()).hexdigest())
        self.assertEqual(close['body']['firstGpsTime'],1000)
        self.assertEqual(close['body']['lastGpsTime'],3000)
        self.assertEqual(close['body']['closeReason'],'IDLE_TIMEOUT')
        self.assertNotIn('currentLocation',close['body'])
        self.assertEqual(self.store.protected_paths(),set())

    def test_no_change_no_extra_update_and_outage_coalesces(self):
        row,file=self.file()
        for _ in range(3): self.worker.prepare()
        self.worker.deliver();self.worker.deliver();self.worker.deliver()
        self.assertEqual(self.client.send.call_count,2)
        self.worker.prepare();self.worker.deliver()
        self.assertEqual(self.client.send.call_count,2)

    def test_recovered_file_missing_file_and_incomplete_tail(self):
        row,file=self.file(raw=b'{"deviceId":"D1","gpsTime":1000}\n{"deviceId":')
        missing=self.store.register('devices/device-D1/missing.jsonl','D1',NOW.isoformat())
        self.store.recover(NOW.isoformat(),'SERVER_SHUTDOWN')
        self.worker.prepare();self.worker.deliver();self.worker.deliver()
        close=self.client.send.call_args[0][0]
        self.assertEqual(close['body']['closeReason'],'ABORTED')
        self.assertTrue(file.read_bytes().endswith(b'{"deviceId":'))
        states={r['file_key']:r['state'] for r in self.store.recordings()}
        self.assertEqual(states[missing['file_key']],'ERROR')

    def test_retry_preserves_body_and_blocks_only_affected_file(self):
        row,file=self.file(); other,_=self.file('devices/device-D1/b.jsonl')
        def send(item):
            if item['file_key']==row['file_key']:raise ReportError('NETWORK_ERROR',True)
        self.client.send.side_effect=send
        self.worker.deliver()
        due=self.store.due(NOW.timestamp())
        self.assertEqual(due,[])
        self.assertEqual(self.client.send.call_count,2)
        self.assertEqual(len(self.store.due(NOW.timestamp()+1)),1)

    def test_unknown_timezone_and_no_gps_close_aborted(self):
        row,file=self.file(raw=b'{"deviceId":"D1"}\n')
        self.store.mark_closed(row['path'],'IDLE_TIMEOUT',NOW.isoformat())
        self.worker.prepare();self.worker.deliver();self.worker.deliver()
        body=self.client.send.call_args[0][0]['body']
        self.assertEqual(body['closeReason'],'ABORTED');self.assertIsNone(body['firstGpsTime'])
        self.assertNotIn('timezone',body)

    def test_symlink_escape_is_blocked(self):
        row,file=self.file(); file.unlink(); file.symlink_to('/etc/passwd')
        self.store.mark_closed(row['path'],'IDLE_TIMEOUT',NOW.isoformat())
        self.worker.prepare()
        self.assertEqual(self.store.recordings()[0]['state'],'ERROR')

    def test_remote_timeout_not_recreated(self):
        row,file=self.file();self.client.send.side_effect=ReportError('RakingRecordingTaskTimedOut')
        self.worker.deliver();self.worker.deliver()
        self.assertEqual(self.client.send.call_count,1)
        self.assertEqual(self.store.recordings()[0]['file_key'],row['file_key'])
        self.assertIn(row['path'],self.store.protected_paths())

    def test_timezone_lock_persisted_after_initial_unknown_snapshot(self):
        row,file=self.file(raw=b'{"deviceId":"D1","gpsTime":1000,"GNSSlat":28.2,"GNSSlon":112.8,"GNSSPosStat":"4"}\n')
        self.worker.prepare(); self.worker.deliver();self.worker.deliver()
        with file.open('ab') as f:
            for gps in (2000,3000):
                f.write((json.dumps(dict(deviceId='D1',gpsTime=gps,GNSSlat=28.2,GNSSlon=112.8,GNSSPosStat='4'))+'\n').encode())
        self.worker.prepare();self.worker.deliver()
        audit=json.loads(self.store.recordings()[0]['timezone_audit'])
        self.assertEqual(audit['name'],'Asia/Shanghai')
        self.store.mark_closed(row['path'],'IDLE_TIMEOUT',NOW.isoformat())
        restarted=reporting_worker.ReportWorker(self.root,self.store,self.config,self.client,
             clock=lambda:NOW,zone_factory=lambda:WorksiteTimezone(lambda lat,lon:None))
        restarted.prepare();restarted.deliver()
        self.assertEqual(self.client.send.call_args[0][0]['body']['timezone'],'Asia/Shanghai')
