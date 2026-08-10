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
        allow_shutdown_to_finish = threading.Event()
        shutdown_notification_requested = threading.Event()
        run_server_returned = threading.Event()
        previous_handler = object()

        class ControlledServer:
            server_address = ("127.0.0.1", 40123)

            def __enter__(self):
                return self

            def __exit__(self, *_arguments):
                return False

            def start_shutdown_coordinator(self):
                return True

            def serve_forever(self):
                return None

            def notify_shutdown(self):
                shutdown_notification_requested.set()

            def wait_for_shutdown_coordinator(self):
                allow_shutdown_to_finish.wait(timeout=2)

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
                self.assertTrue(shutdown_notification_requested.wait(timeout=1))
                self.assertFalse(run_server_returned.wait(timeout=0.2))
            finally:
                allow_shutdown_to_finish.set()
                run_server_thread.join(timeout=1)
            self.assertTrue(run_server_returned.is_set())
            self.assertFalse(run_server_thread.is_alive())

    def test_run_server_preserves_the_first_signal_install_exception(self):
        install_error = ValueError("SIGINT install failed")
        cleanup_error = RuntimeError("unexpected restore")
        signal_calls = []

        class ControlledServer:
            server_address = ("127.0.0.1", 40123)

            def __enter__(self):
                return self

            def __exit__(self, *_arguments):
                return False

            def start_shutdown_coordinator(self):
                return True

            def cancel_shutdown_coordinator(self):
                return None

            def wait_for_shutdown_coordinator(self):
                return None

        def install_signal(received_signal, handler):
            signal_calls.append((received_signal, handler))
            if len(signal_calls) == 1:
                raise install_error
            raise cleanup_error

        with mock.patch(
            "data_record_server.server.create_server", return_value=ControlledServer()
        ), mock.patch(
            "data_record_server.server.signal.getsignal", return_value=object()
        ), mock.patch(
            "data_record_server.server.signal.signal", side_effect=install_signal
        ):
            with self.assertRaises(ValueError) as caught:
                from data_record_server.server import run_server

                run_server(
                    Config("127.0.0.1", 0, self._data_dir, 4)
                )

        self.assertIs(install_error, caught.exception)
        self.assertEqual(1, len(signal_calls))
        self.assertEqual(signal.SIGINT, signal_calls[0][0])

    def test_run_server_restores_only_installed_handlers_after_setup_failure(self):
        install_error = ValueError("SIGTERM install failed")
        cleanup_error = RuntimeError("SIGINT restore failed")
        previous_sigint_handler = object()
        signal_calls = []

        class ControlledServer:
            server_address = ("127.0.0.1", 40123)

            def __enter__(self):
                return self

            def __exit__(self, *_arguments):
                return False

            def start_shutdown_coordinator(self):
                return True

            def cancel_shutdown_coordinator(self):
                return None

            def wait_for_shutdown_coordinator(self):
                return None

        def install_signal(received_signal, handler):
            signal_calls.append((received_signal, handler))
            if len(signal_calls) == 1:
                return None
            if len(signal_calls) == 2:
                raise install_error
            raise cleanup_error

        with mock.patch(
            "data_record_server.server.create_server", return_value=ControlledServer()
        ), mock.patch(
            "data_record_server.server.signal.getsignal", side_effect=[
                previous_sigint_handler,
                object(),
            ]
        ), mock.patch(
            "data_record_server.server.signal.signal", side_effect=install_signal
        ), mock.patch("data_record_server.server.LOGGER.exception") as log_exception:
            with self.assertRaises(ValueError) as caught:
                from data_record_server.server import run_server

                run_server(
                    Config("127.0.0.1", 0, self._data_dir, 4)
                )

        self.assertIs(install_error, caught.exception)
        self.assertEqual(3, len(signal_calls))
        self.assertEqual(signal.SIGINT, signal_calls[0][0])
        self.assertEqual(signal.SIGTERM, signal_calls[1][0])
        self.assertEqual(
            (signal.SIGINT, previous_sigint_handler), signal_calls[2]
        )
        log_exception.assert_called_once()

    def test_shutdown_handler_only_notifies_the_server(self):
        with mock.patch.object(self._server, "notify_shutdown") as notify_shutdown, mock.patch(
            "data_record_server.server.threading.Thread"
        ) as thread_class:
            shutdown_handler = create_shutdown_handler(self._server)
            shutdown_handler(15, None)
            shutdown_handler(15, None)

        self.assertEqual(2, notify_shutdown.call_count)
        thread_class.assert_not_called()

    def test_sigterm_notifies_shutdown_handler_while_coordinator_lock_is_held(self):
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
        with server._shutdown_coordinator_lock:
            os.kill(os.getpid(), signal.SIGTERM)
        server.wait_for_shutdown_coordinator()
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

    def test_cross_thread_nested_sigterm_uses_one_shutdown_coordinator(self):
        child_code = """
import inspect
import os
import signal
import sys
import tempfile
import threading

import data_record_server.server as server_module
from data_record_server.server import CollectorServer, create_shutdown_handler
from data_record_server.storage import Storage


executions = []
injected = [False]
uses_coordinator = hasattr(CollectorServer, "start_shutdown_coordinator")
trace_function = (
    create_shutdown_handler
    if uses_coordinator
    else CollectorServer.register_shutdown_worker
)
source_lines, source_start_line = inspect.getsourcelines(trace_function)
trace_line = next(
    source_start_line + offset
    for offset, line in enumerate(source_lines)
    if (
        "server.notify_shutdown()" in line
        if uses_coordinator
        else "self._shutdown_worker = worker" in line
    )
)
background_ready = threading.Event()
request_nested_signal = threading.Event()
sent_nested_signal = threading.Event()
release_background = threading.Event()


def background():
    background_ready.set()
    request_nested_signal.wait(timeout=1)
    os.kill(os.getpid(), signal.SIGTERM)
    sent_nested_signal.set()
    release_background.wait(timeout=1)


def trace(frame, event, _argument):
    if (
        event == "line"
        and (
            frame.f_code.co_name == "shutdown_handler"
            if uses_coordinator
            else frame.f_code is trace_function.__code__
        )
        and frame.f_lineno == trace_line
        and not injected[0]
    ):
        injected[0] = True
        request_nested_signal.set()
        assert sent_nested_signal.wait(timeout=1)
    return trace


with tempfile.TemporaryDirectory() as directory:
    server = CollectorServer(("127.0.0.1", 0), Storage(directory), 4)
    try:
        if uses_coordinator:
            original_shutdown = server.shutdown

            def shutdown():
                executions.append(1)
                original_shutdown()

            server.shutdown = shutdown
            server.start_shutdown_coordinator()
            coordinator = server._shutdown_coordinator
            serve_thread = threading.Thread(target=server.serve_forever)
            serve_thread.start()
            for _ in range(100):
                with server._shutdown_coordinator_state:
                    if server._serve_loop_ready:
                        break
                    server._shutdown_coordinator_state.wait(timeout=0.01)
            else:
                raise AssertionError("serve loop did not become ready")
        else:
            server.shutdown = lambda: executions.append(1)
        background_thread = threading.Thread(target=background)
        background_thread.start()
        assert background_ready.wait(timeout=1)
        if not uses_coordinator:
            class SynchronousWorker:
                def __init__(self, *, target, daemon):
                    self.target = target

                def start(self):
                    self.target()

            server_module.threading.Thread = SynchronousWorker
            server_module.signal.pthread_sigmask = lambda _how, _mask: set()
        signal.signal(signal.SIGTERM, create_shutdown_handler(server))
        sys.settrace(trace)
        os.kill(os.getpid(), signal.SIGTERM)
        sys.settrace(None)
        assert injected[0]
        if uses_coordinator:
            server.wait_for_shutdown_coordinator()
            assert server._shutdown_coordinator is coordinator
            serve_thread.join(timeout=1)
            assert not serve_thread.is_alive()
        assert len(executions) == 1, len(executions)
    finally:
        release_background.set()
        if "background_thread" in locals():
            background_thread.join(timeout=1)
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
                self.fail("nested SIGTERM test process did not exit before the deadline")
            self.assertEqual(0, process.returncode, process.stderr.read())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            process.stderr.close()

    def test_run_server_from_a_non_main_thread_does_not_hang_before_serving(self):
        child_code = """
import tempfile
import threading

from data_record_server.config import Config
from data_record_server.server import run_server

with tempfile.TemporaryDirectory() as directory:
    failures = []

    def run():
        try:
            run_server(Config("127.0.0.1", 0, directory, 4))
        except BaseException as error:
            failures.append(type(error).__name__)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert failures == ["ValueError"], failures
"""
        self._assert_child_exits_successfully(child_code)

    def test_server_close_cancels_the_coordinator_before_serving(self):
        child_code = """
import tempfile

from data_record_server.config import Config
from data_record_server.server import create_server

with tempfile.TemporaryDirectory() as directory:
    server = create_server(Config("127.0.0.1", 0, directory, 4))
    server.start_shutdown_coordinator()
    server.server_close()
"""
        self._assert_child_exits_successfully(child_code)

    @mock.patch("data_record_server.server.threading.Thread")
    def test_coordinator_start_failure_allows_a_later_retry(self, thread_class):
        first_worker = mock.Mock()
        first_worker.start.side_effect = RuntimeError("thread start failed")
        second_worker = mock.Mock()
        thread_class.side_effect = [first_worker, second_worker]

        with self.assertRaisesRegex(RuntimeError, "thread start failed"):
            self._server.start_shutdown_coordinator()
        self._server.start_shutdown_coordinator()

        self.assertIs(self._server._shutdown_coordinator, second_worker)
        second_worker.start.assert_called_once_with()

    def test_wait_for_shutdown_coordinator_avoids_joining_its_own_thread(self):
        self._server._shutdown_coordinator = threading.current_thread()

        self._server.wait_for_shutdown_coordinator()

    def _read_events(self):
        return self._read_events_from(self._data_dir)

    def _assert_child_exits_successfully(self, child_code):
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
                self.fail("child process did not exit before the deadline")
            self.assertEqual(0, process.returncode, process.stderr.read())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            process.stderr.close()

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
