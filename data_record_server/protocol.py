"""Streaming validation for device JSONL records."""

import json
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class AcceptedRecord:
    """A validated JSONL record paired with its device identifier."""

    device_id: str
    raw_line: bytes


class RecordRejected(ValueError):
    """A JSONL record was rejected for a stable protocol reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _reject_non_standard_json_constant(constant: str) -> None:
    raise ValueError(f"Non-standard JSON constant: {constant}")


class JsonlStreamParser:
    """Incrementally frame and validate JSONL records for one connection."""

    def __init__(self, max_line_bytes: int) -> None:
        if max_line_bytes <= 0:
            raise ValueError("max_line_bytes must be positive")
        self._max_line_bytes = max_line_bytes
        self._buffer = bytearray()
        self._device_id: Optional[str] = None

    def feed(self, data: bytes) -> list[AcceptedRecord]:
        """Consume bytes and return each complete, valid JSONL record."""
        self._buffer.extend(data)
        records = []

        while True:
            newline_index = self._buffer.find(b"\n")
            if newline_index < 0:
                if len(self._buffer) > self._max_line_bytes:
                    self._buffer.clear()
                    raise RecordRejected("line_too_long")
                return records

            raw_line = bytes(self._buffer[: newline_index + 1])
            del self._buffer[: newline_index + 1]
            if len(raw_line) - 1 > self._max_line_bytes:
                self._buffer.clear()
                raise RecordRejected("line_too_long")

            records.append(self._parse_line(raw_line))

    def finish(self) -> None:
        """Confirm that the stream ended on a record boundary."""
        if self._buffer:
            raise RecordRejected("incomplete_line")

    def _parse_line(self, raw_line: bytes) -> AcceptedRecord:
        try:
            decoded_line = raw_line.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RecordRejected("invalid_utf8") from error

        try:
            value = json.loads(
                decoded_line, parse_constant=_reject_non_standard_json_constant
            )
        except (json.JSONDecodeError, ValueError) as error:
            raise RecordRejected("invalid_json") from error

        if not isinstance(value, dict):
            raise RecordRejected("json_not_object")

        device_id = value.get("deviceId")
        if type(device_id) is not str or not device_id:
            raise RecordRejected("missing_device_id")

        if self._device_id is None:
            self._device_id = device_id
        elif device_id != self._device_id:
            raise RecordRejected("device_id_changed")

        return AcceptedRecord(device_id=device_id, raw_line=raw_line)
