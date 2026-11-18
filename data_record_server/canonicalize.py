"""Build the close-time canonical JSONL view from connection archives."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class CanonicalizeResult:
    record_count: int
    quarantined_count: int
    duplicate_count: int


def _gps_time_ms(value: object) -> int:
    """Parse the recorder's millisecond Unix timestamp without rounding floats."""
    if isinstance(value, bool):
        raise ValueError("boolean gpsTime")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        return int(value.strip())
    raise ValueError("invalid gpsTime")


def _atomic_write(path: Path, lines: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            for line in lines:
                stream.write(line)
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def canonicalize_jsonl_files(
    source_paths: Iterable[Path],
    output_path: Path,
    quarantine_path: Path,
    *,
    opened_at: datetime,
    closed_at: datetime,
    tolerance: timedelta,
) -> CanonicalizeResult:
    """Sort valid session records and atomically publish the canonical view.

    Source files are never modified. Lines with malformed JSON, invalid timestamps,
    or timestamps outside the session window are written to the quarantine file.
    Exact duplicate lines are omitted from the canonical output while preserving
    the first occurrence's stable order for equal timestamps.
    """
    lower_ms = int((opened_at - tolerance).astimezone(timezone.utc).timestamp() * 1000)
    upper_ms = int((closed_at + tolerance).astimezone(timezone.utc).timestamp() * 1000)
    accepted: list[tuple[int, int, str]] = []
    quarantined: list[str] = []
    seen: set[str] = set()
    duplicate_count = 0
    order = 0
    for source_path in source_paths:
        with Path(source_path).open("r", encoding="utf-8", newline="") as stream:
            for raw_line in stream:
                line = raw_line.rstrip("\r\n")
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    timestamp_ms = _gps_time_ms(record["gpsTime"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    quarantined.append(line)
                    order += 1
                    continue
                if not lower_ms <= timestamp_ms <= upper_ms:
                    quarantined.append(line)
                    order += 1
                    continue
                if line in seen:
                    duplicate_count += 1
                    order += 1
                    continue
                seen.add(line)
                accepted.append((timestamp_ms, order, line))
                order += 1

    accepted.sort(key=lambda item: (item[0], item[1]))
    # An input that has no usable gpsTime must not erase the live preview
    # file.  The caller may use that preview for diagnostic-only recordings.
    if accepted or not output_path.exists():
        _atomic_write(output_path, (line for _, _, line in accepted))
    _atomic_write(quarantine_path, quarantined)
    return CanonicalizeResult(
        record_count=len(accepted),
        quarantined_count=len(quarantined),
        duplicate_count=duplicate_count,
    )
