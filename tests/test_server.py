"""Integration tests for the concurrent raw TCP collector server."""

import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from data_record_server.config import Config
from data_record_server.server import create_server, create_shutdown_handler


class CollectorServerTest(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._temporary_directory.name)
        self._server = create_server(
            Config(
                host="127.0.0.1",
                port=0,
                data_dir=self._data_dir,
                read_buffer_bytes=4,
            )
        )
        self._server_thread = threading.Thread(target=self._server.serve_forever)
        self._server_thread.start()

    def tearDown(self):
        self._server.shutdown()
        self._server.server_close()
        self._server_thread.join(timeout=2)
        self.assertFalse(self._server_thread.is_alive())
        self._temporary_directory.cleanup()

    def test_records_complete_stream_and_receive_metadata(self):
        with socket.create_connection(self._server.server_address, timeout=2) as client:
            client.sendall(b"abcdef")
            client.shutdown(socket.SHUT_WR)
            self.assertEqual(b"", client.recv(1))

        self._wait_for(
            lambda: len(
                [
                    event
                    for event in self._read_events()
                    if event["event"] == "disconnected"
                ]
            )
            == 1
        )

        connection_files = list((self._data_dir / "connections").glob("*.bin"))
        self.assertEqual(1, len(connection_files))
        self.assertEqual(b"abcdef", connection_files[0].read_bytes())

        events = self._read_events()
        self.assertEqual(
            ["connected", "received", "received", "disconnected"],
            [event["event"] for event in events],
        )
        self.assertEqual("127.0.0.1", events[0]["client_ip"])
        self.assertIsInstance(events[0]["client_port"], int)
        received = [event for event in events if event["event"] == "received"]
        self.assertEqual(b"abcdef", bytes.fromhex("".join(event["hex"] for event in received)))
        self.assertEqual(6, sum(event["bytes"] for event in received))

    def test_accepts_a_second_client_after_the_first_disconnects(self):
        for payload in (b"first", b"second"):
            with socket.create_connection(
                self._server.server_address, timeout=2
            ) as client:
                client.sendall(payload)
                client.shutdown(socket.SHUT_WR)
                self.assertEqual(b"", client.recv(1))

        self._wait_for(
            lambda: len(
                [
                    event
                    for event in self._read_events()
                    if event["event"] == "disconnected"
                ]
            )
            == 2
        )

        connection_files = list((self._data_dir / "connections").glob("*.bin"))
        self.assertEqual(2, len(connection_files))
        self.assertEqual(
            {b"first", b"second"}, {path.read_bytes() for path in connection_files}
        )

    @mock.patch("data_record_server.server.threading.Thread")
    def test_shutdown_handler_requests_shutdown_from_another_thread(
        self, thread_class
    ):
        create_shutdown_handler(self._server)(15, None)

        thread_class.assert_called_once_with(target=self._server.shutdown, daemon=True)
        thread_class.return_value.start.assert_called_once_with()

    def _read_events(self):
        events_path = self._data_dir / "events.jsonl"
        if not events_path.exists():
            return []
        return [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
        ]

    def _wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("condition was not met before the deadline")


if __name__ == "__main__":
    unittest.main()
