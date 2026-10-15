"""Bounded, authenticated HTTP transport for the Web ICD."""

import json
import math
import time
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ReportError(Exception):
    def __init__(self, code, retryable=False, retry_after=None):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def retry_delay(value):
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0, delay) if math.isfinite(delay) else None


class ReportClient:
    def __init__(self, config, send=None):
        self.config = config
        self._send = send or build_opener(NoRedirect()).open

    def send(self, item):
        path = '/api/v2.4/manual-raking/recording-files/'
        phase = item['phase']
        if phase != 'create':
            path += item['file_key'] + '/'
        if phase == 'close':
            path += 'close/'
        request = Request(self.config.base_url + path,
                          data=json.dumps(item['body'], ensure_ascii=True, allow_nan=False).encode(),
                          method='PATCH' if phase == 'update' else 'POST',
                          headers={'Content-Type':'application/json',
                                   'X-Environment':self.config.environment,
                                   'Idempotency-Key':item['key'],
                                   'Authorization':self.config.authorization})
        try:
            try:
                response = self._send(request, timeout=self.config.request_timeout)
            except HTTPError as error:
                response = error
            with response:
                status = response.status
                raw = response.read(1024 * 1024 + 1)
                delay = retry_delay(response.headers.get('Retry-After'))
        except (OSError, URLError, TimeoutError):
            # Do not persist exception messages: upstream URLs or response bodies may contain credentials.
            raise ReportError('NETWORK_ERROR', True) from None
        if len(raw) > 1024 * 1024:
            raise ReportError('RESPONSE_TOO_LARGE')
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            result = None
        # Permanent business errors take precedence even when a proxy returned 5xx.
        known = {'RakingRecordingTaskTimedOut','RakingFileAlreadyFinished',
                 'RakingUnauthorized','RakingFileValidationFailed','RakingEnvironmentMismatch',
                 'RakingFileSequenceConflict','RakingLocatorDisabled','RakingInvalidRequest'}
        code = result.get('code') if isinstance(result, dict) else None
        if isinstance(code, str) and code in known:
            raise ReportError(code)
        if not 200 <= status < 300:
            raise ReportError('HTTP_' + str(status), status == 429 or status >= 500, delay)
        if not isinstance(result, dict):
            raise ReportError('INVALID_RESPONSE')
        if type(code) is not int or code != 0:
            raise ReportError('BUSINESS_ERROR')
        data = result.get('data')
        if (not isinstance(data, dict) or data.get('fileKey') != item['file_key']
                or data.get('rakingRecordingTaskId') != item['task_id']):
            raise ReportError('RESPONSE_IDENTITY_MISMATCH')
        expected = {'RECORDING','PAUSED'} if phase != 'close' else {
            'ABORTED' if item['body']['closeReason'] == 'ABORTED' else 'READY'}
        if data.get('status') not in expected:
            raise ReportError('RESPONSE_STATUS_MISMATCH')
        if phase == 'update':
            accepted = data.get('acceptedSequence')
            if type(accepted) is not int or accepted < item['body']['sequence']:
                raise ReportError('RESPONSE_SEQUENCE_MISMATCH')
        return data
