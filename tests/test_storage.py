"""Contracts for device-scoped JSONL recording sessions."""

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from data_record_server.storage import Storage


class MutableUtcClock:
    """A deterministic UTC clock whose time can be advanced by a test."""

    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class StorageTest(unittest.TestCase):
    def test_cleanup_removes_expired_archives_and_empty_device_directories(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            stale_connection = data_dir / "connections" / "stale.bin"
            fresh_connection = data_dir / "connections" / "fresh.bin"
            stale_device_file = data_dir / "devices" / "device-D1" / "stale.jsonl"
            for path, contents, age_days in (
                (stale_connection, b"old", 31),
                (fresh_connection, b"new", 1),
                (stale_device_file, b"archive", 31),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(contents)
                timestamp = (now - timedelta(days=age_days)).timestamp()
                os.utime(path, (timestamp, timestamp))

            storage = Storage(data_dir)
            storage.cleanup_expired_files(30, now, "startup")
            events = self._read_events(data_dir)

            self.assertFalse(stale_connection.exists())
            self.assertTrue(fresh_connection.exists())
            self.assertFalse(stale_device_file.exists())
            self.assertFalse((data_dir / "devices" / "device-D1").exists())

        self.assertEqual(
            {
                "event": "retention_cleanup",
                "time": "2026-08-10T12:00:00Z",
                "trigger": "startup",
                "deleted_files": 2,
                "reclaimed_bytes": 10,
                "failed_files": 0,
            },
            events[-1],
        )

    def test_cleanup_skips_active_archives_and_symbolic_links(self):
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            storage = Storage(data_dir, clock=lambda: now)
            session = storage.append_record(
                ("203.0.113.1", 5001), "D1", b'{"deviceId":"D1"}\n'
            )
            rejected = storage.open_rejected_connection(("203.0.113.2", 5002))
            symlink_target = data_dir / "outside.bin"
            symlink_target.write_bytes(b"outside")
            symlink = data_dir / "connections" / "expired-link.bin"
            symlink.symlink_to(symlink_target)
            stale_timestamp = (now - timedelta(days=31)).timestamp()
            for path in (
                data_dir / session.relative_path,
                data_dir / rejected.relative_path,
                symlink_target,
            ):
                os.utime(path, (stale_timestamp, stale_timestamp))
            os.utime(
                symlink,
                (stale_timestamp, stale_timestamp),
                follow_symlinks=False,
            )

            storage.cleanup_expired_files(30, now, "startup")
            cleanup_event = self._read_events(data_dir)[-1]
            rejected.close("eof")
            storage.close_all_sessions()

            self.assertTrue((data_dir / session.relative_path).exists())
            self.assertTrue((data_dir / rejected.relative_path).exists())
            self.assertTrue(symlink.is_symlink())
            self.assertTrue(symlink_target.exists())

        self.assertEqual(0, cleanup_event["deleted_files"])
        self.assertEqual(0, cleanup_event["reclaimed_bytes"])
        self.assertEqual(0, cleanup_event["failed_files"])

    def test_merges_device_records_from_multiple_clients_into_one_jsonl_session(self):
        clock = MutableUtcClock(datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc))
        first_line = b'{"deviceId":"D1"}\n'
        second_line = b'{"deviceId":"D1","n":2}\r\n'

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(directory, clock=clock)
            first = storage.append_record(("203.0.113.1", 5001), "D1", first_line)
            second = storage.append_record(("203.0.113.2", 5002), "D1", second_line)

            self.assertEqual(first.relative_path, second.relative_path)
            self.assertTrue(first.relative_path.startswith("devices/device-D1/"))
            self.assertTrue(first.relative_path.endswith(".jsonl"))
            self.assertEqual(
                first_line + second_line,
                (Path(directory) / first.relative_path).read_bytes(),
            )

            events = self._read_events(directory)
            storage.close_all_sessions()

        self.assertEqual(
            ["session_started", "session_attached", "session_attached"],
            [event["event"] for event in events],
        )
        self.assertEqual("2026-08-10T12:00:00Z", events[0]["time"])
        self.assertEqual(first.relative_path, events[0]["file"])
        self.assertEqual("D1", events[0]["device_id"])
        self.assertEqual(
            {("203.0.113.1", 5001), ("203.0.113.2", 5002)},
            {
                (event["client_ip"], event["client_port"])
                for event in events
                if event["event"] == "session_attached"
            },
        )

    def test_attaches_each_client_only_on_its_first_record_for_a_session(self):
        clock = MutableUtcClock(datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc))

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(directory, clock=clock)
            session = storage.append_record(
                ("203.0.113.3", 5003), "D1", b'{"deviceId":"D1","n":1}\n'
            )
            storage.append_record(
                ("203.0.113.3", 5003), "D1", b'{"deviceId":"D1","n":2}\n'
            )
            storage.append_record(
                ("203.0.113.4", 5004), "D1", b'{"deviceId":"D1","n":3}\n'
            )
            attachments = [
                event
                for event in self._read_events(directory)
                if event["event"] == "session_attached"
            ]
            storage.close_all_sessions()

        self.assertEqual(2, len(attachments))
        self.assertEqual({session.relative_path}, {event["file"] for event in attachments})
        self.assertEqual({"D1"}, {event["device_id"] for event in attachments})
        self.assertEqual(
            {("203.0.113.3", 5003), ("203.0.113.4", 5004)},
            {(event["client_ip"], event["client_port"]) for event in attachments},
        )

    def test_keeps_different_devices_in_separate_session_files(self):
        clock = MutableUtcClock(datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc))

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(directory, clock=clock)
            first = storage.append_record(
                ("203.0.113.5", 5005), "D1", b'{"deviceId":"D1"}\n'
            )
            second = storage.append_record(
                ("203.0.113.6", 5006), "D2", b'{"deviceId":"D2"}\n'
            )

            self.assertNotEqual(first.relative_path, second.relative_path)
            self.assertEqual(
                b'{"deviceId":"D1"}\n',
                (Path(directory) / first.relative_path).read_bytes(),
            )
            self.assertEqual(
                b'{"deviceId":"D2"}\n',
                (Path(directory) / second.relative_path).read_bytes(),
            )
            self.assertEqual(
                2, len(list((Path(directory) / "devices").glob("**/*.jsonl")))
            )
            storage.close_all_sessions()

    def test_concurrent_appends_share_one_complete_device_session(self):
        client_count = 8
        payloads = [
            f'{{"deviceId":"D1","sequence":{index}}}\n'.encode("ascii")
            for index in range(client_count)
        ]
        failures = []
        failures_lock = threading.Lock()

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(directory)
            start = threading.Barrier(client_count)

            def append(index):
                try:
                    start.wait(timeout=2)
                    storage.append_record(
                        (f"203.0.113.{index + 10}", 5010 + index),
                        "D1",
                        payloads[index],
                    )
                except Exception as error:
                    with failures_lock:
                        failures.append(error)

            threads = [
                threading.Thread(target=append, args=(index,))
                for index in range(client_count)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual([], failures)
            storage.close_all_sessions()
            session_files = list((Path(directory) / "devices").glob("**/*.jsonl"))
            self.assertEqual(1, len(session_files))
            content = session_files[0].read_bytes()
            self.assertEqual(set(payloads), set(content.splitlines(keepends=True)))
            self.assertEqual(client_count, len(content.splitlines(keepends=True)))

            closed_events = [
                event
                for event in self._read_events(directory)
                if event["event"] == "session_closed"
            ]

        self.assertEqual(1, len(closed_events))
        self.assertEqual(len(content), closed_events[0]["total_bytes"])
        self.assertEqual("D1", closed_events[0]["device_id"])
        self.assertEqual(
            f"devices/{session_files[0].parent.name}/{session_files[0].name}",
            closed_events[0]["file"],
        )

    def test_rejected_record_only_writes_a_diagnostic_event(self):
        clock = MutableUtcClock(datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc))

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(directory, clock=clock)
            storage.record_rejected(("203.0.113.7", 5007), "invalid_json")

            devices_directory = Path(directory) / "devices"
            self.assertFalse(devices_directory.exists())
            events = self._read_events(directory)

        self.assertEqual(1, len(events))
        self.assertEqual("record_rejected", events[0]["event"])
        self.assertEqual("2026-08-10T12:00:00Z", events[0]["time"])
        self.assertEqual("invalid_json", events[0]["reason"])
        self.assertEqual("203.0.113.7", events[0]["client_ip"])
        self.assertEqual(5007, events[0]["client_port"])

    def test_rejected_connection_appends_rejections_to_one_diagnostic_bin(self):
        clock = MutableUtcClock(datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc))

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(directory, clock=clock)
            recorder = storage.open_rejected_connection(("203.0.113.7", 5007))
            recorder.record_rejected("invalid_json", b"bad\n")
            recorder.record_rejected("invalid_utf8", b"\xff\n")
            recorder.close("eof")
            connection_files = list((Path(directory) / "connections").glob("*.bin"))
            connection_bytes = connection_files[0].read_bytes()
            events = self._read_events(directory)

        self.assertEqual(1, len(connection_files))
        self.assertEqual(b"bad\n\xff\n", connection_bytes)
        self.assertEqual(
            [
                "rejected_connection_started",
                "record_rejected",
                "record_rejected",
                "rejected_connection_closed",
            ],
            [event["event"] for event in events],
        )
        relative_path = "connections/" + connection_files[0].name
        self.assertEqual(relative_path, events[0]["file"])
        self.assertEqual("203.0.113.7", events[0]["client_ip"])
        self.assertEqual(5007, events[0]["client_port"])
        self.assertEqual("invalid_json", events[1]["reason"])
        self.assertEqual(relative_path, events[1]["file"])
        self.assertEqual(4, events[1]["rejected_bytes"])
        self.assertEqual(4, events[1]["total_bytes"])
        self.assertEqual("invalid_utf8", events[2]["reason"])
        self.assertEqual(relative_path, events[2]["file"])
        self.assertEqual(2, events[2]["rejected_bytes"])
        self.assertEqual(6, events[2]["total_bytes"])
        self.assertEqual(relative_path, events[3]["file"])
        self.assertEqual(6, events[3]["total_bytes"])
        self.assertEqual("eof", events[3]["reason"])

    def test_rejected_connection_uses_a_safe_ipv6_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(directory)
            recorder = storage.open_rejected_connection(("2001:db8::1", 5007))
            recorder.close("eof")
            connection_file = next((Path(directory) / "connections").glob("*.bin"))

        self.assertNotIn(":", connection_file.name)
        self.assertIn("2001_db8__1", connection_file.name)

    def test_does_not_create_connections_directory_without_a_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            Storage(directory)

            self.assertFalse((Path(directory) / "connections").exists())

    def test_rejected_connection_close_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(directory)
            recorder = storage.open_rejected_connection(("203.0.113.7", 5007))

            self.assertTrue(recorder.close("eof"))
            self.assertFalse(recorder.close("eof"))
            events = self._read_events(directory)

        self.assertEqual(
            ["rejected_connection_started", "rejected_connection_closed"],
            [event["event"] for event in events],
        )

    def test_rejected_connection_close_event_failure_releases_active_path_and_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(directory)
            recorder = storage.open_rejected_connection(("203.0.113.7", 5007))
            events = recorder._events
            failing_events = mock.Mock()

            def fail_only_close(event):
                if event["event"] == "rejected_connection_closed":
                    raise OSError("event write failed")
                events.write(event)

            failing_events.write.side_effect = fail_only_close
            recorder._events = failing_events

            with self.assertRaisesRegex(OSError, "event write failed"):
                recorder.close("eof")

            self.assertTrue(recorder._stream.closed)
            self.assertNotIn(recorder.relative_path, storage._active_rejected_paths)

    def test_expires_session_at_idle_timeout_boundary_and_removes_it_from_active_sessions(self):
        clock = MutableUtcClock(datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc))
        raw_line = b'{"deviceId":"D1"}\n'

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(directory, clock=clock)
            first_session = storage.append_record(
                ("203.0.113.8", 5008), "D1", raw_line
            )
            clock.advance(30)
            storage.expire_sessions(idle_timeout_seconds=30)
            events_after_expiry = self._read_events(directory)
            second_line = b'{"deviceId":"D1","n":2}\n'
            second_session = storage.append_record(
                ("203.0.113.8", 5008), "D1", second_line
            )
            storage.close_all_sessions()
            events = self._read_events(directory)
            session_files = list((Path(directory) / "devices").glob("**/*.jsonl"))
            first_content = (Path(directory) / first_session.relative_path).read_bytes()
            second_content = (
                Path(directory) / second_session.relative_path
            ).read_bytes()
            events_by_file = {}
            for event in events:
                events_by_file.setdefault(event["file"], []).append(event)

        self.assertEqual(
            [
                "session_started",
                "session_attached",
                "session_idle_timeout",
                "session_closed",
                "session_started",
                "session_attached",
                "session_closed",
            ],
            [event["event"] for event in events],
        )
        self.assertNotEqual(first_session.relative_path, second_session.relative_path)
        self.assertEqual(2, len(session_files))
        self.assertEqual(raw_line, first_content)
        self.assertEqual(second_line, second_content)
        self.assertEqual(
            [
                "session_started",
                "session_attached",
                "session_idle_timeout",
                "session_closed",
            ],
            [event["event"] for event in events_by_file[first_session.relative_path]],
        )
        self.assertEqual(
            ["session_started", "session_attached", "session_closed"],
            [event["event"] for event in events_by_file[second_session.relative_path]],
        )
        self.assertEqual(
            ["session_started", "session_attached", "session_idle_timeout", "session_closed"],
            [event["event"] for event in events_after_expiry],
        )
        self.assertEqual("2026-08-10T12:00:30Z", events[2]["time"])
        self.assertEqual("2026-08-10T12:00:30Z", events[3]["time"])
        for event in events[2:4]:
            self.assertEqual(first_session.relative_path, event["file"])
            self.assertEqual("D1", event["device_id"])
            self.assertEqual(len(raw_line), event["total_bytes"])
        self.assertEqual(30, events[2]["idle_timeout_seconds"])
        self.assertEqual(second_session.relative_path, events[-1]["file"])
        self.assertEqual(len(second_line), events[-1]["total_bytes"])
        self.assertEqual("D1", events[-1]["device_id"])

    def test_close_all_sessions_closes_each_active_session_only_once_for_shutdown(self):
        clock = MutableUtcClock(datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc))

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(directory, clock=clock)
            first = storage.append_record(
                ("203.0.113.9", 5009), "D1", b'{"deviceId":"D1"}\n'
            )
            second = storage.append_record(
                ("203.0.113.10", 5010), "D2", b'{"deviceId":"D2"}\n'
            )
            storage.close_all_sessions()
            storage.close_all_sessions()
            closed_events = [
                event
                for event in self._read_events(directory)
                if event["event"] == "session_closed"
            ]

        self.assertEqual(2, len(closed_events))
        self.assertEqual(
            {first.relative_path, second.relative_path},
            {event["file"] for event in closed_events},
        )
        self.assertEqual({"D1", "D2"}, {event["device_id"] for event in closed_events})
        self.assertEqual(
            {"server_shutdown"}, {event["reason"] for event in closed_events}
        )

    def test_records_a_lone_surrogate_device_id_using_a_safe_path_and_event(self):
        device_id = "\ud800"
        raw_line = b'{"deviceId":"surrogate"}\n'

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(directory)
            session = storage.append_record(
                ("203.0.113.11", 5011), device_id, raw_line
            )

            self.assertIn("device-%ED%A0%80", session.relative_path)
            self.assertTrue(session.relative_path.endswith(".jsonl"))
            self.assertEqual(
                raw_line, (Path(directory) / session.relative_path).read_bytes()
            )
            storage.close_all_sessions()
            events = self._read_events(directory)

        self.assertEqual(device_id, events[0]["device_id"])
        self.assertEqual(device_id, events[-1]["device_id"])

    def test_escapes_slashes_in_a_device_id_for_one_path_component(self):
        device_id = "a/b"
        raw_line = b'{"deviceId":"a/b"}\n'

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(directory)
            session = storage.append_record(
                ("203.0.113.16", 5016), device_id, raw_line
            )
            relative_path = Path(session.relative_path)
            device_directory = Path(directory) / "devices" / "device-a%2Fb"

            self.assertEqual(
                ("devices", "device-a%2Fb", relative_path.name),
                relative_path.parts,
            )
            self.assertIn("_a%2Fb_", relative_path.name)
            self.assertEqual(raw_line, (Path(directory) / relative_path).read_bytes())
            self.assertEqual([Path(directory) / relative_path], list(device_directory.iterdir()))
            storage.close_all_sessions()
            events = self._read_events(directory)

        self.assertEqual({device_id}, {event["device_id"] for event in events})

    def test_expire_removes_a_session_when_its_stream_close_fails(self):
        clock = MutableUtcClock(datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc))

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(directory, clock=clock)
            first = storage.append_record(
                ("203.0.113.12", 5012), "D1", b'{"deviceId":"D1"}\n'
            )
            original_stream = first._stream
            failing_stream = mock.Mock()

            def close_then_fail():
                original_stream.close()
                raise OSError("close failed")

            failing_stream.close.side_effect = close_then_fail
            first._stream = failing_stream
            clock.advance(30)

            with self.assertRaisesRegex(OSError, "close failed"):
                storage.expire_sessions(idle_timeout_seconds=30)

            replacement = storage.append_record(
                ("203.0.113.12", 5012), "D1", b'{"deviceId":"D1","n":2}\n'
            )
            storage.close_all_sessions()

        self.assertNotEqual(first.relative_path, replacement.relative_path)

    def test_close_all_continues_after_a_close_event_write_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(directory)
            first = storage.append_record(
                ("203.0.113.13", 5013), "D1", b'{"deviceId":"D1"}\n'
            )
            second = storage.append_record(
                ("203.0.113.14", 5014), "D2", b'{"deviceId":"D2"}\n'
            )
            events = first._events
            failing_events = mock.Mock()

            def fail_only_close(event):
                if event["event"] == "session_closed":
                    raise OSError("event write failed")
                events.write(event)

            failing_events.write.side_effect = fail_only_close
            first._events = failing_events
            second_stream = mock.Mock(wraps=second._stream)
            second._stream = second_stream

            with self.assertRaisesRegex(OSError, "event write failed"):
                storage.close_all_sessions()

            replacement = storage.append_record(
                ("203.0.113.14", 5014), "D2", b'{"deviceId":"D2","n":2}\n'
            )
            storage.close_all_sessions()

        second_stream.close.assert_called_once_with()
        self.assertNotEqual(second.relative_path, replacement.relative_path)

    def test_close_all_preserves_sessions_when_the_clock_fails(self):
        clock = MutableUtcClock(datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc))

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(directory, clock=clock)
            session = storage.append_record(
                ("203.0.113.15", 5015), "D1", b'{"deviceId":"D1"}\n'
            )

            def fail_clock():
                raise OSError("clock failed")

            storage._clock = fail_clock
            with self.assertRaisesRegex(OSError, "clock failed"):
                storage.close_all_sessions()

            self.assertIs(session, storage._sessions["D1"])
            self.assertFalse(session._closed)
            storage._clock = clock
            storage.close_all_sessions()
            events = self._read_events(directory)

        self.assertEqual("session_closed", events[-1]["event"])
        self.assertEqual("server_shutdown", events[-1]["reason"])

    @staticmethod
    def _read_events(directory):
        return [
            json.loads(line)
            for line in (Path(directory) / "events.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]


if __name__ == "__main__":
    unittest.main()
