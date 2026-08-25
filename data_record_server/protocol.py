"""Streaming validation for device JSONL records."""

import json
from dataclasses import dataclass
from typing import Optional, Union


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


@dataclass(frozen=True)
class FeedResult:
    """The accepted and rejected records produced by one parser operation."""

    records: tuple[AcceptedRecord, ...]
    rejections: tuple[RecordRejected, ...]
    close_connection: bool = False

    @classmethod
    def empty(cls) -> "FeedResult":
        return cls((), ())


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

    def feed(self, data: bytes) -> FeedResult:
        """Consume bytes and report all complete records in the input buffer."""
        self._buffer.extend(data)
        records = []
        rejections = []

        while True:
            newline_index = self._buffer.find(b"\n")
            if newline_index < 0:
                if len(self._buffer) > self._max_line_bytes:
                    self._buffer.clear()
                    return FeedResult((), (RecordRejected("line_too_long"),), True)
                return FeedResult(tuple(records), tuple(rejections))

            raw_line = bytes(self._buffer[: newline_index + 1])
            del self._buffer[: newline_index + 1]
            if len(raw_line) - 1 > self._max_line_bytes:
                self._buffer.clear()
                return FeedResult((), (RecordRejected("line_too_long"),), True)

            parsed_line = self._parse_line(raw_line)
            if isinstance(parsed_line, RecordRejected):
                rejections.append(parsed_line)
            else:
                records.append(parsed_line)

    def finish(self) -> FeedResult:
        """Report whether the stream ended on a record boundary."""
        if self._buffer:
            return FeedResult((), (RecordRejected("incomplete_line"),))
        return FeedResult.empty()

    def _parse_line(self, raw_line: bytes) -> Union[AcceptedRecord, RecordRejected]:
        try:
            decoded_line = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            return RecordRejected("invalid_utf8")

        try:
            value = json.loads(
                decoded_line, parse_constant=_reject_non_standard_json_constant
            )
        except (json.JSONDecodeError, ValueError):
            return RecordRejected("invalid_json")

        if not isinstance(value, dict):
            return RecordRejected("json_not_object")

        device_id = value.get("deviceId")
        if type(device_id) is not str or not device_id:
            return RecordRejected("missing_device_id")

        if self._device_id is None:
            self._device_id = device_id
        elif device_id != self._device_id:
            return RecordRejected("device_id_changed")

        return AcceptedRecord(device_id=device_id, raw_line=raw_line)
