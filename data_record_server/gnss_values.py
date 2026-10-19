"""Normalize device numeric strings for analysis without altering recorded bytes."""

import math
import re

_INTEGER = re.compile(r'[0-9]+', re.ASCII)
_NUMBER = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?', re.ASCII)


def normalize_record(record):
    """Return a copy with supported numeric strings converted.

    Invalid field values become None, leaving existing range and fix-quality
    checks responsible for accepting each observation. GPS integers are never
    converted through float, avoiding timestamp rounding.
    """
    result = record.copy()
    value = result.get('gpsTime')
    if isinstance(value, str):
        value = value.strip()
        try:
            value = int(value) if _INTEGER.fullmatch(value) else None
        except ValueError:
            value = None
        result['gpsTime'] = value
    for key in ('GNSSlat', 'GNSSlon', 'GNSShigh'):
        value = result.get(key)
        if isinstance(value, str):
            value = value.strip()
            value = float(value) if _NUMBER.fullmatch(value) else None
            result[key] = value if value is not None and math.isfinite(value) else None
    return result
