"""Integration tests for the concurrent raw TCP collector server."""

import json
import os
import signal
import socket
import subprocess
import sys
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

    def test_accepts_a_second_client_while_the_first_remains_connected(self):
        with socket.create_connection(
            self._server.server_address, timeout=2
        ) as first_client:
            first_client.sendall(b"first")
            self._wait_for(
                lambda: any(
                    event["event"] == "received" and event["hex"] == b"firs".hex()
                    for event in self._read_events()
                )
            )

            with socket.create_connection(
                self._server.server_address, timeout=2
            ) as second_client:
                second_client.sendall(b"second")
                second_client.shutdown(socket.SHUT_WR)
                self.assertEqual(b"", second_client.recv(1))

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
            completed_file = next(
                event["file"]
                for event in self._read_events()
                if event["event"] == "disconnected"
            )
            self.assertEqual(b"second", (self._data_dir / completed_file).read_bytes())

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
        events_by_file = {}
        for event in self._read_events():
            events_by_file.setdefault(event["file"], []).append(event)
        for relative_path, events in events_by_file.items():
            payload = (self._data_dir / relative_path).read_bytes()
            self.assertEqual("connected", events[0]["event"])
            self.assertEqual("disconnected", events[-1]["event"])
            self.assertEqual(
                payload,
                bytes.fromhex(
                    "".join(event["hex"] for event in events if event["event"] == "received")
                ),
            )
            self.assertEqual(len(payload), events[-1]["total_bytes"])

    def test_sigterm_closes_an_active_connection_before_process_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            port = self._find_free_tcp_port()
            environment = os.environ.copy()
            environment.update(
                {
                    "TCP_HOST": "127.0.0.1",
                    "TCP_PORT": str(port),
                    "DATA_DIR": str(data_dir),
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                }
            )
            process = subprocess.Popen(
                [sys.executable, "-m", "data_record_server"],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            client = None
            try:
                client = self._connect_to_process(port, process)
                client.sendall(b"abc")
                self._wait_for(
                    lambda: any(
                        event["event"] == "received" and event["hex"] == b"abc".hex()
                        for event in self._read_events_from(data_dir)
                    )
                )

                process.send_signal(signal.SIGTERM)
                process.wait(timeout=2)
                self.assertEqual(0, process.returncode)

                events = self._read_events_from(data_dir)
                self.assertEqual(
                    ["connected", "received", "disconnected"],
                    [event["event"] for event in events],
                )
                self.assertEqual(3, events[-1]["total_bytes"])
            finally:
                if client is not None:
                    client.close()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
                process.stderr.close()

    def test_run_server_waits_for_the_signal_shutdown_worker(self):
        shutdown_started = threading.Event()
        allow_shutdown_to_finish = threading.Event()
        synchronous_shutdown_finished = threading.Event()
        run_server_returned = threading.Event()
        previous_handler = object()
        test_case = self

        class ControlledServer:
            server_address = ("127.0.0.1", 40123)

            def __init__(self):
                self._shutdown_worker = None

            def __enter__(self):
                return self

            def __exit__(self, *_arguments):
                return False

            def serve_forever(self):
                signal_handlers[signal.SIGTERM](signal.SIGTERM, None)
                test_case.assertTrue(shutdown_started.wait(timeout=1))

            def shutdown(self):
                if self._shutdown_worker is None:
                    self._shutdown_worker = threading.current_thread()
                    shutdown_started.set()
                    allow_shutdown_to_finish.wait(timeout=2)
                    return
                synchronous_shutdown_finished.set()

            def wait_for_shutdown_worker(self):
                self._shutdown_worker.join(timeout=2)

        controlled_server = ControlledServer()
        signal_handlers = {}

        def install_signal(received_signal, handler):
            signal_handlers[received_signal] = handler

        def run():
            try:
                from data_record_server.server import run_server

                run_server(
                    Config(
                        host="127.0.0.1",
                        port=40123,
                        data_dir=self._data_dir,
                        read_buffer_bytes=4,
                    )
                )
            finally:
                run_server_returned.set()

        with mock.patch(
            "data_record_server.server.create_server", return_value=controlled_server
        ), mock.patch(
            "data_record_server.server.signal.getsignal", return_value=previous_handler
        ), mock.patch(
            "data_record_server.server.signal.signal", side_effect=install_signal
        ):
            run_server_thread = threading.Thread(target=run)
            run_server_thread.start()
            try:
                self.assertTrue(synchronous_shutdown_finished.wait(timeout=1))
                self.assertFalse(run_server_returned.wait(timeout=0.2))
            finally:
                allow_shutdown_to_finish.set()
                run_server_thread.join(timeout=1)
            self.assertTrue(run_server_returned.is_set())
            self.assertFalse(run_server_thread.is_alive())

    @mock.patch("data_record_server.server.threading.Thread")
    def test_shutdown_handler_does_not_replace_a_live_worker(self, thread_class):
        first_worker = mock.Mock()
        second_worker = mock.Mock()
        thread_class.side_effect = [first_worker, second_worker]

        shutdown_handler = create_shutdown_handler(self._server)
        shutdown_handler(15, None)
        shutdown_handler(15, None)

        first_worker.start.assert_called_once_with()
        second_worker.start.assert_not_called()

    def test_sigterm_reenters_shutdown_handler_while_worker_lock_is_held(self):
        child_code = """
import os
import signal
import tempfile

from data_record_server.server import CollectorServer, create_shutdown_handler
from data_record_server.storage import Storage

with tempfile.TemporaryDirectory() as directory:
    server = CollectorServer(("127.0.0.1", 0), Storage(directory), 4)
    try:
        server.shutdown = lambda: None
        signal.signal(signal.SIGTERM, create_shutdown_handler(server))
        with server._shutdown_worker_lock:
            os.kill(os.getpid(), signal.SIGTERM)
        server.wait_for_shutdown_worker()
    finally:
        server.server_close()
"""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        process = subprocess.Popen(
            [sys.executable, "-c", child_code],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
                self.fail("SIGTERM handler deadlocked while the worker lock was held")
            self.assertEqual(0, process.returncode, process.stderr.read())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            process.stderr.close()

    @mock.patch("data_record_server.server.threading.Thread")
    def test_shutdown_handler_clears_a_worker_when_start_fails(self, thread_class):
        first_worker = mock.Mock()
        first_worker.start.side_effect = RuntimeError("thread start failed")
        second_worker = mock.Mock()
        thread_class.side_effect = [first_worker, second_worker]
        shutdown_handler = create_shutdown_handler(self._server)

        with self.assertRaisesRegex(RuntimeError, "thread start failed"):
            shutdown_handler(15, None)
        shutdown_handler(15, None)

        self.assertIs(self._server._shutdown_worker, second_worker)
        second_worker.start.assert_called_once_with()

    @mock.patch("data_record_server.server.threading.Thread")
    def test_shutdown_handler_requests_shutdown_from_another_thread(
        self, thread_class
    ):
        create_shutdown_handler(self._server)(15, None)

        thread_class.assert_called_once_with(target=self._server.shutdown, daemon=True)
        thread_class.return_value.start.assert_called_once_with()

    def _read_events(self):
        return self._read_events_from(self._data_dir)

    @staticmethod
    def _find_free_tcp_port():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def _connect_to_process(self, port, process):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(f"collector process exited with {process.returncode}")
            try:
                return socket.create_connection(("127.0.0.1", port), timeout=0.2)
            except OSError:
                time.sleep(0.01)
        self.fail("collector process did not accept a connection before the deadline")

    @staticmethod
    def _read_events_from(data_dir):
        events_path = data_dir / "events.jsonl"
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
