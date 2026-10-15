import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from data_record_server import reporting_models


class Zone:
    name = None
    audit = {}
    def observe(self, record):
        pass


class FileStatsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'a.jsonl'
        self.stats = reporting_models.FileStats('D1', Zone())

    def write(self, records):
        raw = b''.join((json.dumps(r) + '\n').encode() for r in records)
        self.path.write_bytes(raw); return raw

    def test_raw_counts_hash_minmax_and_incremental_reads(self):
        raw = self.write([{'deviceId':'D1','gpsTime':2000}, {'deviceId':'D1','gpsTime':1000}, {'deviceId':'D1'}])
        self.stats.scan(self.path, final=False)
        self.stats.scan(self.path, final=False)
        self.assertEqual(self.stats.snapshot()['recordCount'], 3)
        with self.path.open('ab') as stream: stream.write(b'{"deviceId":"D1","gpsTime":3000}\n')
        self.stats.scan(self.path, final=True)
        result = self.stats.snapshot()
        self.assertEqual(result['recordCount'], 4)
        self.assertEqual(result['firstGpsTime'], 1000)
        self.assertEqual(result['lastGpsTime'], 3000)
        self.assertEqual(result['sizeBytes'], self.path.stat().st_size)
        self.assertEqual(self.stats.sha256, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertTrue(self.stats.valid)

    def test_partial_line_deferred_and_aborted_at_close(self):
        self.path.write_bytes(b'{"deviceId":"D1","gpsTime":1000}\n{"deviceId":')
        self.stats.scan(self.path, final=False)
        self.assertEqual(self.stats.snapshot()['recordCount'], 1)
        self.stats.scan(self.path, final=True)
        self.assertFalse(self.stats.valid)
        self.assertEqual(self.stats.sha256, hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_partial_line_completed_before_close(self):
        self.path.write_bytes(b'{"deviceId":"D1",')
        self.stats.scan(self.path, final=False)
        with self.path.open('ab') as f: f.write(b'"gpsTime":1000}\n')
        self.stats.scan(self.path, final=True)
        self.assertTrue(self.stats.valid)
        self.assertEqual(self.stats.snapshot()['recordCount'], 1)

    def test_bad_gps_is_not_replaced_with_server_time(self):
        self.write([{'deviceId':'D1', 'gpsTime':v} for v in (True, -1, '1000', 1e300, None)])
        self.stats.scan(self.path, final=True)
        self.assertEqual(self.stats.snapshot()['recordCount'], 5)
        self.assertIsNone(self.stats.snapshot()['firstGpsTime'])
        self.assertFalse(self.stats.valid)

    def test_mixed_device_and_invalid_json_are_aborted(self):
        for raw in (b'{"deviceId":"OTHER","gpsTime":1000}\n', b'not json\n', b'{}\n', b'\xff\n'):
            self.path.write_bytes(raw)
            stats = reporting_models.FileStats('D1', Zone()); stats.scan(self.path, final=True)
            self.assertFalse(stats.valid)

    def test_replaced_or_truncated_input_rejected(self):
        self.write([{'deviceId':'D1','gpsTime':1000}]); self.stats.scan(self.path, final=False)
        self.path.write_bytes(b'')
        with self.assertRaises(ValueError): self.stats.scan(self.path, final=True)

    def test_location_does_not_fabricate_accuracy_and_does_not_regress(self):
        records = [{'deviceId':'D1','gpsTime':t,'GNSSlat':lat,'GNSSlon':112.8,'GNSSPosStat':'4'}
                   for t,lat in [(2000,28.2),(1000,28.1)]]
        self.write(records); self.stats.scan(self.path, final=True)
        loc = self.stats.snapshot()['currentLocation']
        self.assertEqual(loc['latitude'], 28.2)
        self.assertNotIn('accuracyMeters', loc)
        self.assertEqual(loc['coordinateMode'], 'WGS84')

    def test_large_accepted_json_line_is_not_corruption(self):
        self.write([{'deviceId':'D1','gpsTime':1000,'extension':'x'*70000}])
        self.stats.scan(self.path, final=True)
        self.assertTrue(self.stats.valid)
        self.assertEqual(self.stats.snapshot()['recordCount'],1)

    def test_gps_upper_bound_preserves_millisecond(self):
        self.assertEqual(reporting_models.gps_iso(253402300799999),'9999-12-31T23:59:59.999Z')
