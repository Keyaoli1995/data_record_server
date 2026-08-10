"""Tests for raw TCP data persistence."""

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

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

        relative_path = f"connections/{connection_files[0].name}"
        self.assertEqual(
            ["connected", "received", "received", "disconnected"],
            [event["event"] for event in events],
        )
        self.assertEqual(
            [
                "2026-08-10T12:00:00Z",
                "2026-08-10T12:00:01Z",
                "2026-08-10T12:00:02Z",
                "2026-08-10T12:00:03Z",
            ],
            [event["time"] for event in events],
        )
        self.assertEqual([relative_path] * 4, [event["file"] for event in events])
        self.assertEqual("203.0.113.10", events[0]["client_ip"])
        self.assertEqual(51822, events[0]["client_port"])
        self.assertEqual(2, events[1]["bytes"])
        self.assertEqual("7e00", events[1]["hex"])
        self.assertEqual("ff0d0a", events[2]["hex"])
        self.assertEqual(5, events[3]["total_bytes"])

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
            connection_files = list((Path(directory) / "connections").glob("*.bin"))

        self.assertEqual(1, len(connection_files))
        relative_path = f"connections/{connection_files[0].name}"
        self.assertEqual(
            ["connected", "error", "disconnected"],
            [event["event"] for event in events],
        )
        self.assertEqual(
            [
                "2026-08-10T12:00:00Z",
                "2026-08-10T12:00:01Z",
                "2026-08-10T12:00:02Z",
            ],
            [event["time"] for event in events],
        )
        self.assertEqual([relative_path] * 3, [event["file"] for event in events])
        self.assertEqual("ConnectionResetError", events[1]["error_type"])
        self.assertEqual("peer reset", events[1]["error"])

    def test_removes_file_when_connected_event_cannot_be_written(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(directory)
            with mock.patch.object(
                storage._events, "write", side_effect=OSError("disk full")
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    storage.open_connection(("203.0.113.12", 51824))

            self.assertEqual([], list((Path(directory) / "connections").glob("*.bin")))

    def test_concurrent_connections_preserve_files_and_valid_event_lifecycles(self):
        connection_count = 8
        payloads = {
            (f"203.0.113.{index}", 52000 + index): bytes([index, 0, 255])
            for index in range(connection_count)
        }
        failures = []

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(directory)
            start = threading.Barrier(connection_count)

            def record(client_address, payload):
                try:
                    start.wait()
                    recorder = storage.open_connection(client_address)
                    recorder.record_received(payload)
                    recorder.close()
                except BaseException as error:
                    failures.append(error)

            threads = [
                threading.Thread(target=record, args=(address, payload))
                for address, payload in payloads.items()
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            events = [
                json.loads(line)
                for line in (Path(directory) / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            connection_files = list((Path(directory) / "connections").glob("*.bin"))

            self.assertEqual([], failures)
            self.assertEqual(connection_count * 3, len(events))
            self.assertEqual(connection_count, len(connection_files))

            events_by_file = {}
            for event in events:
                events_by_file.setdefault(event["file"], []).append(event)

            self.assertEqual(
                {f"connections/{path.name}" for path in connection_files},
                set(events_by_file),
            )
            self.assertEqual(
                set(payloads.values()),
                {(Path(directory) / file).read_bytes() for file in events_by_file},
            )
            for file, file_events in events_by_file.items():
                self.assertEqual(
                    ["connected", "received", "disconnected"],
                    [event["event"] for event in file_events],
                )
                self.assertEqual(3, len(file_events))
                self.assertEqual(file_events[1]["bytes"], len((Path(directory) / file).read_bytes()))
                self.assertEqual(file_events[1]["hex"], (Path(directory) / file).read_bytes().hex())


if __name__ == "__main__":
    unittest.main()
