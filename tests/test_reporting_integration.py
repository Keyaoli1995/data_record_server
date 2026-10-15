"""Real TCP input and local HTTP API, including an ambiguous lost response."""
import hashlib
import json
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime, timedelta, timezone
from data_record_server.config import ReportingConfig, Config
from data_record_server.reporting_service import ReportingService
from data_record_server.server import CollectorServer
from data_record_server.storage import Storage
from data_record_server.worksite_timezone import WorksiteTimezone


class ReportingIntegrationTest(unittest.TestCase):
    def test_tcp_recording_http_replay_and_restart(self):
        accepted=[]; seen={}; finished=threading.Event(); lose_first=[True]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self): self.handle_request()
            def do_PATCH(self): self.handle_request()
            def handle_request(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                key=self.headers['Idempotency-Key']
                if key in seen:
                    if seen[key][0] != body: raise AssertionError('retry mutated payload')
                    response=seen[key][1]
                else:
                    if self.path.endswith('/close/'):
                        file_key=self.path.split('/')[-3];status='READY'
                    elif self.command=='PATCH':
                        file_key=self.path.split('/')[-2];status='RECORDING'
                    else:
                        file_key=body['fileKey'];status='RECORDING'
                    task_id=body.get('rakingRecordingTaskId') or accepted[0][1]['rakingRecordingTaskId']
                    response=dict(code=0,data=dict(fileKey=file_key,rakingRecordingTaskId=task_id,
                        status=status,acceptedSequence=body['sequence']))
                    seen[key]=(body,response);accepted.append((self.path,body))
                if lose_first[0]:
                    lose_first[0]=False;self.close_connection=True;return
                encoded=json.dumps(response).encode();self.send_response(200)
                self.send_header('Content-Length',str(len(encoded)));self.end_headers();self.wfile.write(encoded)
                if self.path.endswith('/close/'):finished.set()
        http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        ht=threading.Thread(target=http.serve_forever,daemon=True);ht.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                root=Path(td);now=[datetime(2026,10,15,tzinfo=timezone.utc)]
                config=ReportingConfig(True,'http://127.0.0.1:'+str(http.server_port),'test','Bearer test',.01,1)
                report=ReportingService(root,config,clock=lambda:now[0])
                report.worker.zone_factory=lambda:WorksiteTimezone(lambda lat,lon:'Asia/Shanghai')
                storage=Storage(root,clock=lambda:now[0],reporting=report)
                server=CollectorServer(('127.0.0.1',0),storage,4096,idle_timeout_seconds=30)
                thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
                data=b''.join((json.dumps(dict(deviceId='D1',gpsTime=1000+i*1000,
                    GNSSlat=28.2,GNSSlon=112.8,GNSSPosStat='4'))+'\n').encode() for i in range(3))
                written=threading.Event();original=storage.append_record
                def append(*args):
                    result=original(*args)
                    if (root/result.relative_path).stat().st_size==len(data):written.set()
                    return result
                storage.append_record=append
                try:
                    with socket.create_connection(server.server_address,timeout=2) as sock:
                        sock.sendall(data)
                        self.assertTrue(written.wait(3),'TCP records not flushed')
                    report.worker.prepare();report.worker.deliver() # create accepted but reply lost
                    first_id=report.store.recordings()[0]['file_key']
                    server.shutdown();thread.join(3);server.server_close()
                finally:
                    if thread.is_alive():server.shutdown();thread.join(3)
                    server.server_close()
                now[0]+=timedelta(seconds=5)
                recovered=ReportingService(root,config,clock=lambda:now[0])
                recovered.worker.zone_factory=lambda:WorksiteTimezone(lambda lat,lon:'Asia/Shanghai')
                try:
                    self.assertEqual(recovered.store.recordings()[0]['file_key'],first_id)
                    recovered.worker.prepare()
                    for _ in range(4):recovered.worker.deliver()
                    self.assertTrue(finished.wait(2))
                    self.assertEqual(len(accepted),3)
                    close=accepted[-1][1]
                    self.assertEqual(close['sizeBytes'],len(data))
                    self.assertEqual(close['sha256'],hashlib.sha256(data).hexdigest())
                    self.assertEqual(close['recordCount'],3)
                    self.assertEqual(close['timezone'],'Asia/Shanghai')
                    self.assertEqual(close['firstGpsTime'],1000)
                    self.assertEqual(close['lastGpsTime'],3000)
                    self.assertEqual(recovered.store.protected_paths(),set())
                finally:recovered.stop()
        finally:
            http.shutdown();http.server_close();ht.join(3)
