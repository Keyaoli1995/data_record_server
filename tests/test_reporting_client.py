import json
import unittest
from datetime import datetime, timezone
from data_record_server import reporting_client
from data_record_server.config import ReportingConfig


class Response:
    def __init__(self, status, body, headers=None):
        self.status = status; self.body = json.dumps(body).encode(); self.headers = headers or {}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, size): return self.body[:size]


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.item = dict(file_key='file1',task_id='task1',key='key1',phase='create',body={'sequence':1})
        self.config = ReportingConfig(True, 'https://example.test', 'test', 'Bearer secret')

    def response(self, **changes):
        data = dict(fileKey='file1', rakingRecordingTaskId='task1', status='RECORDING', sequence=1)
        data.update(changes)
        return {'code':0, 'data': data}

    def client(self, status=200, body=None, headers=None):
        def send(request, timeout):
            self.request = request; self.timeout = timeout
            return Response(status, self.response() if body is None else body, headers)
        return reporting_client.ReportClient(self.config, send=send)

    def test_headers_paths_methods_and_success(self):
        self.client().send(self.item)
        self.assertEqual(self.request.full_url, 'https://example.test/api/v2.4/manual-raking/recording-files/')
        self.assertEqual(self.request.get_header('Idempotency-key'), 'key1')
        self.assertEqual(self.request.get_header('Authorization'), 'Bearer secret')
        self.assertEqual(self.request.get_header('X-environment'), 'test')
        self.assertEqual(self.request.method, 'POST')
        self.item['phase']='update'; self.item['body']={'sequence':2}
        self.client(body=self.response(acceptedSequence=2)).send(self.item)
        self.assertEqual(self.request.method, 'PATCH')
        self.assertTrue(self.request.full_url.endswith('/file1/'))

    def test_close_requires_matching_final_state(self):
        self.item['phase']='close'; self.item['body']={'closeReason':'IDLE_TIMEOUT','sequence':3}
        with self.assertRaises(reporting_client.ReportError): self.client().send(self.item)
        self.client(body=self.response(status='READY')).send(self.item)
        self.item['body']['closeReason']='ABORTED'
        self.client(body=self.response(status='ABORTED')).send(self.item)

    def test_invalid_identity_schema_sequence_and_business_code(self):
        for body in ({'code':True,'data':{}}, self.response(fileKey='other'), {'code':0},
                     {'code':'0','data':{}}, {'code':'RakingRecordingTaskTimedOut','msg':'secret'}):
            with self.subTest(body=body), self.assertRaises(reporting_client.ReportError) as cm:
                self.client(body=body).send(self.item)
            self.assertFalse(cm.exception.retryable)
            self.assertNotIn('secret', str(cm.exception))
        self.item.update(phase='update',body={'sequence':3})
        with self.assertRaises(reporting_client.ReportError):
            self.client(body=self.response(acceptedSequence=2)).send(self.item)

    def test_transient_and_permanent_http_failures(self):
        for status, retry in ((429,True),(500,True),(503,True),(401,False),(403,False),(302,False),(400,False)):
            with self.subTest(status=status), self.assertRaises(reporting_client.ReportError) as cm:
                self.client(status=status, body={}, headers={'Retry-After':'15'}).send(self.item)
            self.assertEqual(cm.exception.retryable,retry)
            if retry: self.assertEqual(cm.exception.retry_after,15)

    def test_network_error_redacted(self):
        def fail(*args, **kwargs): raise OSError('secret')
        with self.assertRaises(reporting_client.ReportError) as cm:
            reporting_client.ReportClient(self.config, send=fail).send(self.item)
        self.assertTrue(cm.exception.retryable)
        self.assertNotIn('secret', str(cm.exception))

    def test_permanent_business_error_takes_precedence_over_503(self):
        with self.assertRaises(reporting_client.ReportError) as cm:
            self.client(status=503,body={'code':'RakingRecordingTaskTimedOut'}).send(self.item)
        self.assertFalse(cm.exception.retryable)
        self.assertEqual(cm.exception.code,'RakingRecordingTaskTimedOut')

    def test_broken_http_response_is_retryable(self):
        from http.client import IncompleteRead
        def fail(*args, **kwargs): raise IncompleteRead(b'partial', 100)
        with self.assertRaises(reporting_client.ReportError) as cm:
            reporting_client.ReportClient(self.config, send=fail).send(self.item)
        self.assertTrue(cm.exception.retryable)
