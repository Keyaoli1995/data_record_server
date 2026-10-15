"""Incrementally inspect recorded bytes without changing the original file."""

import hashlib
import json
import math
import os
import stat
from datetime import datetime, timezone


def valid_gps(value):
    return type(value) is int and 0 <= value <= 253402300799999


def gps_iso(value):
    seconds, milliseconds = divmod(value, 1000)
    return datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=milliseconds * 1000).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class FileStats:
    """One file's scanner. A fresh scanner performs full final verification."""
    def __init__(self, device_id, zone):
        self.device_id = device_id
        self.zone = zone
        self._offset = 0
        self._count = 0
        self._first = None
        self._last = None
        self._location = None
        self._location_time = -1
        self._hash = hashlib.sha256()
        self._identity = None
        self._bad = False
        self._complete = False

    @property
    def valid(self):
        return self._complete and not self._bad and self._count > 0 and self._first is not None

    @property
    def sha256(self):
        return self._hash.hexdigest()

    def scan(self, path, final=False):
        # Do not follow a substituted final path. Worker separately checks root containment.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError('not a regular file')
            identity = (before.st_dev, before.st_ino)
            if self._identity is not None and self._identity != identity:
                raise ValueError('recording file replaced')
            self._identity = identity
            if before.st_size < self._offset:
                raise ValueError('recording file truncated')
            stream.seek(self._offset)
            remaining = before.st_size - self._offset
            # Read only the snapshot observed on entry; active files may continue growing.
            while remaining:
                raw = stream.readline(remaining)
                if not raw:
                    raise ValueError('recording changed during scan')
                if not raw.endswith(b'\n') and not final:
                    break
                remaining -= len(raw)
                self._offset += len(raw)
                self._hash.update(raw)
                if not raw.endswith(b'\n'):
                    self._bad = True
                    continue
                self._count += 1
                self._record(raw)
            after = os.fstat(stream.fileno())
            if final and (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('closed recording changed during scan')
            self._complete = final and remaining == 0

    def _record(self, raw):
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get('deviceId') != self.device_id:
                self._bad = True
                return
        except (ValueError, UnicodeError):
            self._bad = True
            return
        gps = value.get('gpsTime')
        if valid_gps(gps):
            self._first = gps if self._first is None else min(self._first, gps)
            self._last = gps if self._last is None else max(self._last, gps)
            lat, lon = value.get('GNSSlat'), value.get('GNSSlon')
            if (gps >= self._location_time and value.get('GNSSPosStat') in ('4', 4)
                    and finite_number(lat) and finite_number(lon)
                    and -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0, 0)):
                self._location_time = gps
                self._location = dict(latitude=lat, longitude=lon, coordinateMode='WGS84', recordedAt=gps_iso(gps))
                altitude = value.get('GNSShigh')
                if finite_number(altitude):
                    self._location['altitudeMeters'] = altitude
        self.zone.observe(value)

    def snapshot(self):
        result = dict(recordCount=self._count, sizeBytes=self._offset,
                      firstGpsTime=self._first, lastGpsTime=self._last)
        if self._last is not None:
            result['lastReportedDataAt'] = gps_iso(self._last)
        if self._location:
            result['currentLocation'] = self._location.copy()
        if self.zone.name:
            result['timezone'] = self.zone.name
        return result
