"""Conservatively infer a worksite's IANA timezone from fixed GNSS samples."""

import copy
import logging
import math
import threading
from importlib.metadata import version
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_LOGGER = logging.getLogger(__name__)
_FINDER = None
_FINDER_LOCK = threading.Lock()
# Largest millisecond timestamp representable by Python's datetime (UTC).
_MAX_TIMESTAMP_MS = 253402300799999


class WorksiteTimezone:
    """Lock a zone after three agreeing fixed samples spanning two seconds.

    ``lookup`` accepts (latitude, longitude), returning an IANA land zone or
    None. The default shares lazily opened offline polygon files behind a lock.
    Callers must serialize observations on each WorksiteTimezone instance.
    """

    def __init__(self, lookup=None):
        self._lookup = lookup
        self._finder = None
        self._load_failed = False
        self._warned = False
        self._name = None
        self._candidate = None
        self._count = 0
        self._first_time = None
        self._last_time = None
        self._audit = {
            "name": None,
            "source": "injected" if lookup is not None else "timezonefinder",
            "version": None,
            "location": None,
            "gps_time_ms": None,
            "conflict_count": 0,
            "last_conflict": None,
            "lookup_error_count": 0,
        }

    @property
    def name(self):
        return self._name

    @property
    def audit(self):
        return copy.deepcopy(self._audit)

    def _reset_candidate(self):
        self._candidate = None
        self._count = 0
        self._first_time = None
        self._last_time = None

    @staticmethod
    def _eligible(record):
        status = record.get("GNSSPosStat")
        if not ((type(status) is int and status == 4) or status == "4"):
            return False
        timestamp = record.get("gpsTime")
        if type(timestamp) is not int or not 0 <= timestamp <= _MAX_TIMESTAMP_MS:
            return False
        lat, lon = record.get("GNSSlat"), record.get("GNSSlon")
        for coordinate, limit in ((lat, 90), (lon, 180)):
            if type(coordinate) not in (int, float):
                return False
            # Check range before isfinite to reject arbitrarily large JSON ints.
            if not -limit <= coordinate <= limit or not math.isfinite(coordinate):
                return False
        return (lat, lon) != (0, 0)

    def _find_zone(self, lat, lon):
        global _FINDER
        if self._lookup is not None:
            return self._lookup(lat, lon)
        if self._load_failed:
            return None
        # The shared disk-backed finder saves about 9 MiB per device. Its file
        # readers use seek(), so both creation and queries must be serialized.
        with _FINDER_LOCK:
            if self._finder is None:
                try:
                    from timezonefinder import TimezoneFinder

                    if _FINDER is None:
                        _FINDER = TimezoneFinder(in_memory=False)
                    self._finder = _FINDER
                    self._audit["version"] = version("timezonefinder")
                except Exception:
                    self._load_failed = True
                    raise
            # Verify polygon containment rather than accepting a shortcut-only
            # result. observe rejects ocean zones, matching land-only lookup.
            return self._finder.certain_timezone_at(lat=lat, lng=lon)

    def observe(self, record: dict) -> None:
        if not isinstance(record, dict) or not self._eligible(record):
            self._reset_candidate()
            return
        timestamp = record["gpsTime"]
        lat, lon = record["GNSSlat"], record["GNSSlon"]
        try:
            name = self._find_zone(lat, lon)
            if not isinstance(name, str) or name.startswith("Etc/"):
                name = None
            if name is not None:
                try:
                    ZoneInfo(name)
                except (ValueError, ZoneInfoNotFoundError):
                    name = None
        except Exception as error:
            self._audit["lookup_error_count"] += 1
            if not self._warned:
                _LOGGER.warning("Worksite timezone lookup unavailable: %s", error)
                self._warned = True
            name = None
        if name is None:
            self._reset_candidate()
            return
        if self._name is not None:
            if name != self._name:
                self._audit["conflict_count"] += 1
                self._audit["last_conflict"] = {
                    "name": name, "location": [lat, lon],
                    "gps_time_ms": timestamp,
                }
            return
        if self._last_time is not None and timestamp <= self._last_time:
            self._reset_candidate()
            return
        if name != self._candidate:
            self._reset_candidate()
            self._candidate = name
            self._first_time = timestamp
        self._last_time = timestamp
        self._count += 1
        if self._count >= 3 and timestamp - self._first_time >= 2000:
            self._name = name
            self._audit.update(name=name, location=[lat, lon], gps_time_ms=timestamp)
