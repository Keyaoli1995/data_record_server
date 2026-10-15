import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from data_record_server.worksite_timezone import WorksiteTimezone


def record(timestamp=1000, **changes):
    result = {"gpsTime": timestamp, "GNSSPosStat": "4",
              "GNSSlat": 31.2304, "GNSSlon": 121.4737}
    result.update(changes)
    return result


class WorksiteTimezoneTest(unittest.TestCase):
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
                    self.assertEqual("9.0.0", resolver.audit["version"])
                    self.assertEqual("3.2026.4", resolver.audit.get("data_version"))

    def test_new_york_dst_uses_iana_rules(self):
        resolver = WorksiteTimezone()
        for timestamp in (1000, 2000, 3000):
            resolver.observe(record(timestamp, GNSSlat=40.7128, GNSSlon=-74.006))
        zone = ZoneInfo(resolver.name)
        before = datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc).astimezone(zone)
        after = datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc).astimezone(zone)
        self.assertEqual((1, 59, -18000), (before.hour, before.minute, before.utcoffset().total_seconds()))
        self.assertEqual((3, 0, -14400), (after.hour, after.minute, after.utcoffset().total_seconds()))


# Principal locations and canonical zone names from IANA tzdb 2024b:
# https://github.com/eggert/tz/blob/2024b/zone1970.tab
# Decimal coordinates below convert that independent table's DDMM[SS] values.
# January/July UTC offsets (minutes) follow the corresponding IANA regional
# rules, not values obtained from the timezonefinder implementation under test.
GLOBAL_LOCATIONS = (
    ("Shanghai", 31 + 14 / 60, 121 + 28 / 60, "Asia/Shanghai", 480, 480),
    ("Singapore", 1 + 17 / 60, 103 + 51 / 60, "Asia/Singapore", 480, 480),
    ("Kolkata", 22 + 32 / 60, 88 + 22 / 60, "Asia/Kolkata", 330, 330),
    ("Kathmandu", 27 + 43 / 60, 85 + 19 / 60, "Asia/Kathmandu", 345, 345),
    ("New York", 40 + 42 / 60 + 51 / 3600, -(74 + 23 / 3600),
     "America/New_York", -300, -240),
    ("Chicago", 41 + 51 / 60, -(87 + 39 / 60), "America/Chicago", -360, -300),
    ("Denver", 39 + 44 / 60 + 21 / 3600, -(104 + 59 / 60 + 3 / 3600),
     "America/Denver", -420, -360),
    ("Los Angeles", 34 + 3 / 60 + 8 / 3600, -(118 + 14 / 60 + 34 / 3600),
     "America/Los_Angeles", -480, -420),
    ("Phoenix", 33 + 26 / 60 + 54 / 3600, -(112 + 4 / 60 + 24 / 3600),
     "America/Phoenix", -420, -420),
    ("Anchorage", 61 + 13 / 60 + 5 / 3600, -(149 + 54 / 60 + 1 / 3600),
     "America/Anchorage", -540, -480),
    ("Honolulu", 21 + 18 / 60 + 25 / 3600, -(157 + 51 / 60 + 30 / 3600),
     "Pacific/Honolulu", -600, -600),
    ("London", 51 + 30 / 60 + 30 / 3600, -(7 / 60 + 31 / 3600),
     "Europe/London", 0, 60),
    ("Berlin", 52 + 30 / 60, 13 + 22 / 60, "Europe/Berlin", 60, 120),
    ("Paris", 48 + 52 / 60, 2 + 20 / 60, "Europe/Paris", 60, 120),
    ("Sao Paulo", -(23 + 32 / 60), -(46 + 37 / 60),
     "America/Sao_Paulo", -180, -180),
    ("Santiago", -(33 + 27 / 60), -(70 + 40 / 60),
     "America/Santiago", -180, -240),
    ("Johannesburg", -(26 + 15 / 60), 28, "Africa/Johannesburg", 120, 120),
    ("Lagos", 6 + 27 / 60, 3 + 24 / 60, "Africa/Lagos", 60, 60),
    ("Nairobi", -(1 + 17 / 60), 36 + 49 / 60, "Africa/Nairobi", 180, 180),
    ("Sydney", -(33 + 52 / 60), 151 + 13 / 60, "Australia/Sydney", 660, 600),
    ("Adelaide", -(34 + 55 / 60), 138 + 35 / 60,
     "Australia/Adelaide", 630, 570),
    ("Lord Howe", -(31 + 33 / 60), 159 + 5 / 60,
     "Australia/Lord_Howe", 660, 630),
    ("Auckland", -(36 + 52 / 60), 174 + 46 / 60,
     "Pacific/Auckland", 780, 720),
    ("Fiji", -(18 + 8 / 60), 178 + 25 / 60, "Pacific/Fiji", 720, 720),
    ("Apia", -(13 + 50 / 60), -(171 + 44 / 60), "Pacific/Apia", 780, 780),
    ("Kiritimati", 1 + 52 / 60, -(157 + 20 / 60),
     "Pacific/Kiritimati", 840, 840),
    ("Pago Pago", -(14 + 16 / 60), -(170 + 42 / 60),
     "Pacific/Pago_Pago", -660, -660),
)


class GlobalGeographicTimezoneTest(unittest.TestCase):
    def resolve_location(self, lat, lon):
        resolver = WorksiteTimezone()
        for timestamp in (1000, 2000, 3000):
            resolver.observe(record(timestamp, GNSSlat=lat, GNSSlon=lon))
        return resolver.name

    def test_global_principal_locations_and_seasonal_offsets(self):
        for city, lat, lon, expected_zone, january, july in GLOBAL_LOCATIONS:
            with self.subTest(city=city):
                name = self.resolve_location(lat, lon)
                self.assertEqual(expected_zone, name)
                for month, expected_offset in ((1, january), (7, july)):
                    instant = datetime(2024, month, 15, 12, tzinfo=timezone.utc)
                    local = instant.astimezone(ZoneInfo(name))
                    self.assertEqual(expected_offset * 60,
                                     local.utcoffset().total_seconds())

    def test_date_line_neighbors_have_different_local_dates(self):
        instant = datetime(2024, 1, 15, 12, tzinfo=timezone.utc)
        for city, lat, lon, name, january, july in GLOBAL_LOCATIONS:
            if city not in ("Apia", "Pago Pago", "Kiritimati"):
                continue
            with self.subTest(city=city):
                zone = ZoneInfo(self.resolve_location(lat, lon))
                local = instant.astimezone(zone)
                self.assertEqual(15 if city == "Pago Pago" else 16, local.day)

    def test_southern_half_hour_dst_transition(self):
        # IANA australasia: LH switches at 02:00 local, first Sunday in April;
        # DST ends on 2024-04-07 by moving from UTC+11 to UTC+10:30.
        name = self.resolve_location(-(31 + 33 / 60), 159 + 5 / 60)
        zone = ZoneInfo(name)
        before = datetime(2024, 4, 6, 14, 59, tzinfo=timezone.utc).astimezone(zone)
        after = datetime(2024, 4, 6, 15, 0, tzinfo=timezone.utc).astimezone(zone)
        self.assertEqual((1, 59, 39600),
                         (before.hour, before.minute, before.utcoffset().total_seconds()))
        self.assertEqual((1, 30, 37800, 1),
                         (after.hour, after.minute, after.utcoffset().total_seconds(), after.fold))

    def test_open_oceans_and_invalid_polar_coordinates_remain_unknown(self):
        # Valid poles have no universal civil timezone. Only assert unknown for
        # open-ocean points and out-of-range poles, not an invented polar zone.
        for lat, lon in ((0, -140), (0, -30), (-40, 80),
                         (90.001, 0), (-90.001, 0), (0, 180.001),
                         (0, -180.001), (0, 0)):
            with self.subTest(lat=lat, lon=lon):
                self.assertIsNone(self.resolve_location(lat, lon))

    def test_alternating_boundary_candidates_never_lock(self):
        zones = iter(("America/Denver", "America/Phoenix") * 20)
        resolver = WorksiteTimezone(lambda lat, lon: next(zones))
        for timestamp in range(1000, 41000, 1000):
            resolver.observe(record(timestamp))
            self.assertIsNone(resolver.name)

    def test_coyhaique_uses_permanent_minus_three_since_2025(self):
        # IANA 2025b introduced this zone after Aysen stopped seasonal changes.
        # https://www.iana.org/time-zones/releases/2025b
        name = self.resolve_location(-(45 + 34 / 60), -(72 + 4 / 60))
        self.assertEqual("America/Coyhaique", name)
        for month in (1, 7, 10):
            instant = datetime(2026, month, 15, 12, tzinfo=timezone.utc)
            self.assertEqual(-10800,
                             instant.astimezone(ZoneInfo(name)).utcoffset().total_seconds())
