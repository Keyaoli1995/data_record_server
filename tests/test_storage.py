"""Tests for raw TCP data persistence."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from data_record_server.storage import Storage


class StorageTest(unittest.TestCase):
    def test_preserves_exact_bytes_and_records_lifecycle_events(self):
        timestamps = iter(
            datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
            + timedelta(seconds=offset)
            for offset in range(4)
        )

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(directory, clock=lambda: next(timestamps))
            recorder = storage.open_connection(("203.0.113.10", 51822))
            recorder.record_received(b"\x7e\x00")
            recorder.record_received(b"\xff\x0d\x0a")
            recorder.close()

            connection_files = list((Path(directory) / "connections").glob("*.bin"))
            self.assertEqual(1, len(connection_files))
            self.assertEqual(b"\x7e\x00\xff\x0d\x0a", connection_files[0].read_bytes())

            events = [
                json.loads(line)
                for line in (Path(directory) / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]

        self.assertEqual(
            ["connected", "received", "received", "disconnected"],
            [event["event"] for event in events],
        )
        self.assertEqual("203.0.113.10", events[0]["client_ip"])
        self.assertEqual(51822, events[0]["client_port"])
        self.assertEqual(2, events[1]["bytes"])
        self.assertEqual("7e00", events[1]["hex"])
        self.assertEqual("ff0d0a", events[2]["hex"])
        self.assertEqual(5, events[3]["total_bytes"])
        self.assertTrue(events[0]["file"].startswith("connections/"))

    def test_records_connection_errors_and_closes_only_once(self):
        timestamps = iter(
            datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
            + timedelta(seconds=offset)
            for offset in range(3)
        )

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(directory, clock=lambda: next(timestamps))
            recorder = storage.open_connection(("203.0.113.11", 51823))
            recorder.record_error(ConnectionResetError("peer reset"))
            recorder.close()
            recorder.close()

            events = [
                json.loads(line)
                for line in (Path(directory) / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]

        self.assertEqual(
            ["connected", "error", "disconnected"],
            [event["event"] for event in events],
        )
        self.assertEqual("ConnectionResetError", events[1]["error_type"])
        self.assertEqual("peer reset", events[1]["error"])


if __name__ == "__main__":
    unittest.main()
