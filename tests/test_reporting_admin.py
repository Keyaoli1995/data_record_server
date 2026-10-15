import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from data_record_server import reporting_admin
from data_record_server.config import ReportingConfig
from data_record_server.reporting_service import ReportingService


class AdminTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.config=ReportingConfig(True,'https://example.test','test','Bearer secret')

    def test_import_is_explicit_idempotent_and_does_not_send(self):
        path=self.root/'devices/device-D1/old.jsonl';path.parent.mkdir(parents=True)
        path.write_bytes(b'{"deviceId":"D1","gpsTime":1000}\n')
        args=['import','--path','devices/device-D1/old.jsonl','--device-id','D1',
              '--opened-at','2026-10-15T00:00:00Z','--closed-at','2026-10-15T00:01:00Z','--reason','IDLE_TIMEOUT']
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(reporting_admin.run(args,self.root,self.config),0)
            self.assertEqual(reporting_admin.run(args,self.root,self.config),0)
        service=ReportingService(self.root,self.config);self.addCleanup(service.stop)
        self.assertEqual(len(service.store.recordings()),1)
        self.assertEqual(service.store.recordings()[0]['state'],'FINALIZED')
        self.assertEqual(service.store.due(0)[0]['phase'],'create')
        self.assertIn('old.jsonl', next(iter(service.protected_paths())))

    def test_status_is_readonly_and_allowed_while_collector_runs(self):
        service=ReportingService(self.root,self.config);self.addCleanup(service.stop)
        service.register('devices/device-D1/a.jsonl','D1','2026-10-15T00:00:00Z')
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(reporting_admin.run(['status'],self.root,self.config),0)
        self.assertEqual(service.store.recordings()[0]['state'],'OPEN')
        self.assertNotIn('secret',output.getvalue())

    def test_import_rejects_live_service_and_invalid_time(self):
        service=ReportingService(self.root,self.config);self.addCleanup(service.stop)
        with self.assertRaises(RuntimeError):
            reporting_admin.run(['retry','--file-key','abc'],self.root,self.config)
        service.stop()
        with self.assertRaises(ValueError):
            reporting_admin.run(['import','--path','../bad','--device-id','D1',
               '--opened-at','bad','--closed-at','bad','--reason','ABORTED'],self.root,self.config)

    def test_retry_does_not_reopen_remote_terminal_task(self):
        service=ReportingService(self.root,self.config);self.addCleanup(service.stop)
        row=service.register('devices/device-D1/a.jsonl','D1','2026-10-15T00:00:00Z')
        item=service.store.due(0)[0]
        service.store.failed(item['id'],'RakingRecordingTaskTimedOut',None)
        with self.assertRaises(ValueError):service.store.retry_blocked(row['file_key'])
