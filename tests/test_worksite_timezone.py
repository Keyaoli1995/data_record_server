import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

try:
    from data_record_server.worksite_timezone import WorksiteTimezone
except ModuleNotFoundError:
    WorksiteTimezone = None


def record(timestamp=1000, **changes):
    result = {"gpsTime": timestamp, "GNSSPosStat": "4",
              "GNSSlat": 31.2304, "GNSSlon": 121.4737}
    result.update(changes)
    return result


class WorksiteTimezoneTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(WorksiteTimezone, "timezone component must exist")

    def test_locks_after_three_samples_spanning_two_seconds(self):
        resolver = WorksiteTimezone(lambda lat, lon: "Asia/Shanghai")
        resolver.observe(record(1000))
        resolver.observe(record(2000, GNSSPosStat=4))
        self.assertIsNone(resolver.name)
        resolver.observe(record(3000))
        self.assertEqual("Asia/Shanghai", resolver.name)
        self.assertEqual(3000, resolver.audit["gps_time_ms"])
        self.assertEqual([31.2304, 121.4737], resolver.audit["location"])
        self.assertEqual("injected", resolver.audit["source"])
        json.dumps(resolver.audit, allow_nan=False)

    def test_dense_samples_need_two_seconds(self):
        resolver = WorksiteTimezone(lambda lat, lon: "Asia/Shanghai")
        for timestamp in (1000, 1001, 1002, 2999):
            resolver.observe(record(timestamp))
        self.assertIsNone(resolver.name)
        resolver.observe(record(3000))
        self.assertEqual("Asia/Shanghai", resolver.name)

    def test_invalid_samples_reset_candidate(self):
        invalid = [{}, {"GNSSPosStat": True}, {"GNSSPosStat": 4.0},
                   {"GNSSPosStat": "5"}, {"GNSSPosStat": None},
                   {"GNSSlat": True}, {"GNSSlat": "31.2"},
                   {"GNSSlat": float("nan")}, {"GNSSlon": float("inf")},
                   {"GNSSlat": 91}, {"GNSSlon": -181},
                   {"GNSSlat": 0, "GNSSlon": 0},
                   {"gpsTime": True}, {"gpsTime": 2000.0},
                   {"gpsTime": "2000"}, {"gpsTime": -1},
                   {"gpsTime": 10 ** 100}]
        for changes in invalid:
            with self.subTest(changes=changes):
                resolver = WorksiteTimezone(lambda lat, lon: "Asia/Shanghai")
                resolver.observe(record(1000))
                resolver.observe(record(2000))
                resolver.observe(record(2500, **changes) if changes else {})
                resolver.observe(record(3000))
                self.assertIsNone(resolver.name)
                resolver.observe(record(4000))
                resolver.observe(record(5000))
                self.assertEqual("Asia/Shanghai", resolver.name)

    def test_non_increasing_timestamps_reset_candidate(self):
        for timestamp in (1000, 2000):
            with self.subTest(timestamp=timestamp):
                resolver = WorksiteTimezone(lambda lat, lon: "Asia/Shanghai")
                for value in (1000, 2000, timestamp, 3000):
                    resolver.observe(record(value))
                self.assertIsNone(resolver.name)

    def test_conflicting_and_unknown_results_reset_candidate(self):
        for value in ("Europe/London", None, "invalid/zone", "Etc/GMT+1", 4):
            with self.subTest(value=value):
                answers = iter(("Asia/Shanghai", "Asia/Shanghai", value,
                                "Asia/Shanghai", "Asia/Shanghai", "Asia/Shanghai"))
                resolver = WorksiteTimezone(lambda lat, lon: next(answers))
                for timestamp in (1000, 2000, 3000, 4000, 5000):
                    resolver.observe(record(timestamp))
                self.assertIsNone(resolver.name)
                resolver.observe(record(6000))
                self.assertEqual("Asia/Shanghai", resolver.name)

    def test_lock_is_immutable_and_conflicts_are_bounded_audit(self):
        resolver = WorksiteTimezone(
            lambda lat, lon: "Asia/Shanghai" if lon > 0 else "America/New_York")
        for timestamp in (1000, 2000, 3000):
            resolver.observe(record(timestamp))
        locked = resolver.audit
        for timestamp in range(4000, 104000, 1000):
            resolver.observe(record(timestamp, GNSSlat=40.7, GNSSlon=-74))
        self.assertEqual("Asia/Shanghai", resolver.name)
        self.assertEqual(100, resolver.audit["conflict_count"])
        self.assertEqual("America/New_York", resolver.audit["last_conflict"]["name"])
        self.assertEqual(locked["location"], resolver.audit["location"])
        self.assertEqual(locked["gps_time_ms"], resolver.audit["gps_time_ms"])
        resolver.audit["location"][0] = 0
        self.assertEqual(31.2304, resolver.audit["location"][0])

    def test_lookup_exceptions_warn_once_and_remain_unknown(self):
        def broken(lat, lon):
            raise RuntimeError("broken lookup")
        resolver = WorksiteTimezone(broken)
        with self.assertLogs("data_record_server.worksite_timezone", "WARNING") as logs:
            for timestamp in (1000, 2000, 3000):
                resolver.observe(record(timestamp))
        self.assertEqual(1, len(logs.output))
        self.assertIsNone(resolver.name)
        self.assertEqual(3, resolver.audit["lookup_error_count"])

    def test_default_is_lazy_and_missing_library_warns_once(self):
        with patch.dict("sys.modules", {"timezonefinder": None}):
            resolver = WorksiteTimezone()
            resolver.observe({})
            with self.assertLogs("data_record_server.worksite_timezone", "WARNING") as logs:
                for timestamp in (1000, 2000, 3000):
                    resolver.observe(record(timestamp))
        self.assertEqual(1, len(logs.output))
        self.assertIsNone(resolver.name)


class GeographicTimezoneTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(WorksiteTimezone, "timezone component must exist")

    def test_real_geography(self):
        for lat, lon, name in ((31.2304, 121.4737, "Asia/Shanghai"),
                               (1.3521, 103.8198, "Asia/Singapore"),
                               (40.7128, -74.006, "America/New_York"),
                               (51.5074, -0.1278, "Europe/London"),
                               (-33.8688, 151.2093, "Australia/Sydney"),
                               (0, -140, None)):
            with self.subTest(name=name):
                resolver = WorksiteTimezone()
                for timestamp in (1000, 2000, 3000):
                    resolver.observe(record(timestamp, GNSSlat=lat, GNSSlon=lon))
                self.assertEqual(name, resolver.name)
                if name:
                    self.assertEqual("timezonefinder", resolver.audit["source"])
                    self.assertEqual("6.5.9", resolver.audit["version"])

    def test_new_york_dst_uses_iana_rules(self):
        resolver = WorksiteTimezone()
        for timestamp in (1000, 2000, 3000):
            resolver.observe(record(timestamp, GNSSlat=40.7128, GNSSlon=-74.006))
        zone = ZoneInfo(resolver.name)
        before = datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc).astimezone(zone)
        after = datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc).astimezone(zone)
        self.assertEqual((1, 59, -18000), (before.hour, before.minute, before.utcoffset().total_seconds()))
        self.assertEqual((3, 0, -14400), (after.hour, after.minute, after.utcoffset().total_seconds()))
