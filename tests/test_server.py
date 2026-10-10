"""Integration tests for the concurrent raw TCP collector server."""

import errno
import json
import os
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from data_record_server.config import Config
from data_record_server.server import (
    CollectorServer,
    create_server,
    create_shutdown_handler,
)
from data_record_server.storage import Storage


class MutableLocalClock:
    def __init__(self, value):
        self.value = value
        self.history = []

    def __call__(self):
        self.history.append(self.value)
        return self.value


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

    def test_supports_server_address_keyword_construction(self):
        server = CollectorServer(
            server_address=("127.0.0.1", 0),
            storage=Storage(self._data_dir),
            read_buffer_bytes=4,
        )
        try:
            self.assertEqual("127.0.0.1", server.server_address[0])
        finally:
            server.server_close()

    def test_service_actions_cleans_on_startup_and_once_per_local_date_change(self):
        local_clock = MutableLocalClock(
            datetime(2026, 8, 10, 23, 59, tzinfo=timezone(timedelta(hours=8)))
        )
        storage = mock.Mock(spec=Storage)
        server = CollectorServer(
            server_address=("127.0.0.1", 0),
            storage=storage,
            read_buffer_bytes=4,
            device_retention_days=365,
            connection_retention_days=14,
            local_clock=local_clock,
        )
        try:
            server.service_actions()
            server.service_actions()
            local_clock.value += timedelta(minutes=2)
            server.service_actions()
        finally:
            server.server_close()

        self.assertEqual(
            [
                mock.call(365, 14, local_clock.history[0], "startup"),
                mock.call(365, 14, local_clock.history[2], "midnight"),
            ],
            storage.cleanup_expired_files.call_args_list,
        )
        self.assertEqual(3, storage.expire_sessions.call_count)

    def test_create_server_uses_configured_retention_days(self):
        server = create_server(
            Config(
                "127.0.0.1",
                0,
                self._data_dir,
                4,
                device_retention_days=365,
                connection_retention_days=14,
            )
        )
        try:
            self.assertEqual(365, server.device_retention_days)
            self.assertEqual(14, server.connection_retention_days)
        finally:
            server.server_close()

    def test_constructor_closes_shutdown_pipe_when_nonblocking_setup_fails(self):
        setup_error = RuntimeError("nonblocking setup failed")
        captured_file_descriptors = []
        real_pipe = os.pipe

        def capture_pipe():
            file_descriptors = real_pipe()
            captured_file_descriptors.extend(file_descriptors)
            return file_descriptors

        try:
            with mock.patch(
                "data_record_server.server.os.pipe", side_effect=capture_pipe
            ), mock.patch(
                "data_record_server.server.os.set_blocking", side_effect=setup_error
            ):
                with self.assertRaises(RuntimeError) as caught:
                    CollectorServer(
                        ("127.0.0.1", 0), Storage(self._data_dir), 4
                    )

            self.assertIs(setup_error, caught.exception)
            self.assertEqual(2, len(captured_file_descriptors))
            self._assert_file_descriptors_closed(captured_file_descriptors)
        finally:
            self._close_file_descriptors(captured_file_descriptors)

    def test_constructor_closes_shutdown_pipe_when_tcp_bind_fails(self):
        captured_file_descriptors = []
        captured_bind_errors = []
        real_pipe = os.pipe
        real_server_bind = CollectorServer.server_bind

        def capture_pipe():
            file_descriptors = real_pipe()
            captured_file_descriptors.extend(file_descriptors)
            return file_descriptors

        def capture_bind_error(server):
            try:
                real_server_bind(server)
            except OSError as error:
                captured_bind_errors.append(error)
                raise

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied_socket:
                occupied_socket.bind(("127.0.0.1", 0))
                occupied_socket.listen()
                with mock.patch(
                    "data_record_server.server.os.pipe", side_effect=capture_pipe
                ), mock.patch.object(
                    CollectorServer, "server_bind", capture_bind_error
                ):
                    with self.assertRaises(OSError) as caught:
                        CollectorServer(
                            occupied_socket.getsockname(), Storage(self._data_dir), 4
                        )

            self.assertEqual(1, len(captured_bind_errors))
            self.assertIs(captured_bind_errors[0], caught.exception)
            self.assertEqual(2, len(captured_file_descriptors))
            self._assert_file_descriptors_closed(captured_file_descriptors)
        finally:
            self._close_file_descriptors(captured_file_descriptors)

    def test_constructor_preserves_bind_error_when_tcp_close_fails(self):
        bind_error = OSError("bind failed")
        close_error = OSError("TCP socket close failed")
        captured_file_descriptors = []
        close_attempts = []
        real_pipe = os.pipe
        real_tcp_server_close = socketserver.TCPServer.server_close

        def capture_pipe():
            file_descriptors = real_pipe()
            captured_file_descriptors.extend(file_descriptors)
            return file_descriptors

        def fail_bind(_server):
            raise bind_error

        def fail_after_tcp_close(server):
            close_attempts.append(1)
            real_tcp_server_close(server)
            raise close_error

        try:
            with mock.patch(
                "data_record_server.server.os.pipe", side_effect=capture_pipe
            ), mock.patch.object(
                CollectorServer, "server_bind", fail_bind
            ), mock.patch.object(
                socketserver.TCPServer, "server_close", fail_after_tcp_close
            ), mock.patch(
                "data_record_server.server.LOGGER.exception"
            ) as log_exception:
                with self.assertRaises(OSError) as caught:
                    CollectorServer(
                        ("127.0.0.1", 0), Storage(self._data_dir), 4
                    )

            self.assertIs(bind_error, caught.exception)
            self.assertEqual([1], close_attempts)
            log_exception.assert_called_once_with(
                "Failed to close TCP socket during construction"
            )
            self.assertEqual(2, len(captured_file_descriptors))
            self._assert_file_descriptors_closed(captured_file_descriptors)
        finally:
            self._close_file_descriptors(captured_file_descriptors)

    def test_appends_records_from_two_connections_to_one_device_session(self):
        first_record = b'{"deviceId":"D1","sequence":1}\n'
        second_record = b'{"deviceId":"D1","sequence":2}\n'

        for record in (first_record, second_record):
            with socket.create_connection(
                self._server.server_address, timeout=2
            ) as client:
                client.sendall(record)
                client.shutdown(socket.SHUT_WR)
                self.assertEqual(b"", client.recv(1))

        self._wait_for(
            lambda: len(self._device_files()) == 1
            and self._device_files()[0].read_bytes() == first_record + second_record
        )

        device_files = self._device_files()
        self.assertEqual(1, len(device_files))
        self.assertEqual(first_record + second_record, device_files[0].read_bytes())
        self.assertEqual([], self._connection_files())
        self.assertEqual(
            ["session_started", "session_attached", "session_attached"],
            [event["event"] for event in self._session_events()],
        )

    def test_writes_different_device_ids_to_separate_sessions(self):
        records = {
            "D1": b'{"deviceId":"D1","sequence":1}\n',
            "D2": b'{"deviceId":"D2","sequence":2}\n',
        }

        for record in records.values():
            with socket.create_connection(
                self._server.server_address, timeout=2
            ) as client:
                try:
                    client.sendall(record)
                except OSError:
                    pass

        self._wait_for(lambda: len(self._device_files()) == 2)

        self.assertEqual(set(records.values()), {path.read_bytes() for path in self._device_files()})
        self.assertEqual(
            {"D1", "D2"},
            {
                event["device_id"]
                for event in self._session_events()
                if event["event"] == "session_started"
            },
        )

    def test_records_a_device_id_with_a_slash(self):
        record = b'{"deviceId":"a/b","sequence":1}\n'
        with socket.create_connection(self._server.server_address, timeout=2) as client:
            client.sendall(record)
            client.shutdown(socket.SHUT_WR)
            self.assertEqual(b"", client.recv(1))

        self._wait_for(lambda: self._contains_payload(self._data_dir, record))

        device_files = self._device_files()
        self.assertEqual(1, len(device_files))
        self.assertEqual("device-a%2Fb", device_files[0].parent.name)
        self.assertIn("_a%2Fb_", device_files[0].name)
        self.assertEqual({"a/b"}, {event["device_id"] for event in self._session_events()})

    def test_records_rejected_bytes_in_one_connection_file_and_closes_at_eof(self):
        first_record = b'{"deviceId":"D1","sequence":1}\n'
        first_rejection = b'{"deviceId":\n'
        second_rejection = b'not-json\n'
        second_record = b'{"deviceId":"D1","sequence":2}\n'
        with socket.create_connection(self._server.server_address, timeout=2) as client:
            client.sendall(
                first_record + first_rejection + second_rejection + second_record
            )
            client.shutdown(socket.SHUT_WR)
            self.assertEqual(b"", client.recv(1))

        self._wait_for(
            lambda: [event["reason"] for event in self._rejection_events()]
            == ["invalid_json", "invalid_json"]
        )

        self.assertEqual([first_record + second_record], [
            path.read_bytes() for path in self._device_files()
        ])
        connection_files = sorted((self._data_dir / "connections").glob("*.bin"))
        self.assertEqual(1, len(connection_files))
        self.assertEqual(
            first_rejection + second_rejection, connection_files[0].read_bytes()
        )
        rejection_events = self._rejection_events()
        self.assertEqual(
            [event["file"] for event in rejection_events],
            [connection_files[0].relative_to(self._data_dir).as_posix()] * 2,
        )
        self._wait_for(
            lambda: any(
                event["event"] == "rejected_connection_closed"
                and event["reason"] == "eof"
                for event in self._read_events()
            )
        )
        self.assertEqual(
            ["invalid_json", "invalid_json"],
            [event["reason"] for event in rejection_events],
        )

    def test_abandons_a_recorder_when_its_first_rejected_write_fails(self):
        storage = self._server.storage
        original_open = storage.open_rejected_connection
        recorders = []

        def open_rejected_connection(client_address):
            recorder = original_open(client_address)
            recorder.record_rejected = mock.Mock(
                side_effect=OSError("rejected record write failed")
            )
            recorders.append(recorder)
            return recorder

        try:
            with mock.patch.object(
                storage,
                "open_rejected_connection",
                side_effect=open_rejected_connection,
            ), mock.patch("data_record_server.server.LOGGER.exception") as log_exception:
                with socket.create_connection(
                    self._server.server_address, timeout=2
                ) as client:
                    client.sendall(b"not-json\n")
                    client.shutdown(socket.SHUT_WR)
                    self.assertEqual(b"", client.recv(1))

            self._wait_for(lambda: len(recorders) == 1)
            recorder = recorders[0]
            self.assertTrue(recorder._stream.closed)
            self.assertNotIn(recorder.relative_path, storage._active_rejected_paths)
            rejected_connection_events = [
                event
                for event in self._read_events()
                if event["event"].startswith("rejected_connection_")
            ]
            self.assertEqual(
                ["rejected_connection_started"],
                [event["event"] for event in rejected_connection_events],
            )
            log_exception.assert_called_once()
        finally:
            for recorder in recorders:
                recorder.abandon()

    def test_rejects_an_incomplete_record_at_end_of_stream(self):
        with socket.create_connection(self._server.server_address, timeout=2) as client:
            client.sendall(b'{"deviceId":"D1"')
            client.shutdown(socket.SHUT_WR)
            self.assertEqual(b"", client.recv(1))

        self._wait_for(
            lambda: [event["reason"] for event in self._rejection_events()]
            == ["incomplete_line"]
        )

        self.assertEqual([], self._device_files())

    def test_rejections_from_feed_and_finish_share_one_connection_file(self):
        rejected_record = b"not-json\n"
        incomplete_record = b'{"deviceId":"D1"'
        with socket.create_connection(self._server.server_address, timeout=2) as client:
            client.sendall(rejected_record + incomplete_record)
            client.shutdown(socket.SHUT_WR)
            self.assertEqual(b"", client.recv(1))

        self._wait_for(
            lambda: [event["reason"] for event in self._rejection_events()]
            == ["invalid_json", "incomplete_line"]
        )
        connection_files = self._connection_files()
        self.assertEqual(1, len(connection_files))
        self.assertEqual(
            rejected_record + incomplete_record, connection_files[0].read_bytes()
        )
        self.assertEqual(
            1,
            len({event["file"] for event in self._rejection_events()}),
        )

    def test_rejects_an_incomplete_record_when_socket_times_out(self):
        incomplete_record = b'{"deviceId":"D1"'
        server, server_thread = self._create_server_with_idle_timeout(0.15)
        try:
            with socket.create_connection(server.server_address, timeout=2) as client:
                client.sendall(incomplete_record)
                self._wait_for(
                    lambda: [event["reason"] for event in self._rejection_events()]
                    == ["incomplete_line"]
                )
                client.settimeout(2)
                self.assertEqual(b"", client.recv(1))

            self.assertEqual([], self._device_files())
            self.assertEqual([], self._session_events())
            connection_files = self._connection_files()
            self.assertEqual(1, len(connection_files))
            self.assertEqual(incomplete_record, connection_files[0].read_bytes())
            self._wait_for(
                lambda: any(
                    event["event"] == "rejected_connection_closed"
                    and event["reason"] == "socket_timeout"
                    for event in self._read_events()
                )
            )
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
            self.assertFalse(server_thread.is_alive())

    def test_closes_a_too_long_rejected_connection_with_parser_reason(self):
        rejected_line = b"abcdef"
        server = create_server(
            Config(
                host="127.0.0.1",
                port=0,
                data_dir=self._data_dir,
                read_buffer_bytes=4,
                max_json_line_bytes=5,
            )
        )
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        try:
            with socket.create_connection(server.server_address, timeout=2) as client:
                client.sendall(rejected_line)
                client.settimeout(2)
                self.assertEqual(b"", client.recv(1))

            self._wait_for(
                lambda: any(
                    event["event"] == "rejected_connection_closed"
                    and event["reason"] == "line_too_long"
                    for event in self._read_events()
                )
            )
            connection_files = self._connection_files()
            self.assertEqual(1, len(connection_files))
            self.assertEqual(rejected_line, connection_files[0].read_bytes())
            self.assertEqual(["line_too_long"], [
                event["reason"] for event in self._rejection_events()
            ])
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
            self.assertFalse(server_thread.is_alive())

    def test_expires_a_device_session_after_its_idle_timeout(self):
        server, server_thread = self._create_server_with_idle_timeout(0.15)
        try:
            with socket.create_connection(server.server_address, timeout=2) as client:
                client.sendall(b'{"deviceId":"D1"}\n')
                client.shutdown(socket.SHUT_WR)
                self.assertEqual(b"", client.recv(1))

            self._wait_for(
                lambda: any(
                    event["event"] == "session_closed"
                    for event in self._read_events()
                )
            )
            events = self._session_events()
            self.assertEqual(
                [
                    "session_started",
                    "session_attached",
                    "session_idle_timeout",
                    "session_closed",
                ],
                [event["event"] for event in events],
            )
            self.assertEqual(0.15, events[2]["idle_timeout_seconds"])
            self.assertEqual(len(b'{"deviceId":"D1"}\n'), events[2]["total_bytes"])
            self.assertEqual("idle_timeout", events[3]["reason"])
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
            self.assertFalse(server_thread.is_alive())

    def test_shutdown_closes_an_active_device_session(self):
        record = b'{"deviceId":"D1"}\n'
        with socket.create_connection(self._server.server_address, timeout=2) as client:
            client.sendall(record)
            self._wait_for(lambda: self._contains_payload(self._data_dir, record))

            self._server.shutdown()
            client.settimeout(2)
            self.assertEqual(b"", client.recv(1))

        self._wait_for(
            lambda: any(
                event["event"] == "session_closed"
                and event["reason"] == "server_shutdown"
                for event in self._session_events()
            )
        )
        self.assertEqual(
            ["session_started", "session_attached", "session_closed"],
            [event["event"] for event in self._session_events()],
        )

    def test_shutdown_closes_an_active_rejected_connection_with_shutdown_reason(self):
        rejected_line = b"not-json\n"
        with socket.create_connection(self._server.server_address, timeout=2) as client:
            client.sendall(rejected_line)
            self._wait_for(lambda: len(self._connection_files()) == 1)

            self._server.shutdown()
            client.settimeout(2)
            self.assertEqual(b"", client.recv(1))

        self._wait_for(
            lambda: any(
                event["event"] == "rejected_connection_closed"
                and event["reason"] == "server_shutdown"
                for event in self._read_events()
            )
        )
        connection_files = self._connection_files()
        self.assertEqual(1, len(connection_files))
        self.assertEqual(rejected_line, connection_files[0].read_bytes())

    def test_serve_loop_failure_closes_active_connections_before_propagating(self):
        serve_error = RuntimeError("serve loop failed")
        record = b'{"deviceId":"D1"}\n'
        failure_requested = threading.Event()
        client_recorded = threading.Event()
        client_finished = threading.Event()
        allow_client_receive = threading.Event()
        client_result = []
        server = create_server(
            Config("127.0.0.1", 0, self._data_dir, 4)
        )

        def service_actions():
            if failure_requested.is_set():
                raise serve_error

        def run_client():
            try:
                with socket.create_connection(server.server_address, timeout=2) as client:
                    client.sendall(record)
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        if self._contains_payload(self._data_dir, record):
                            failure_requested.set()
                            client_recorded.set()
                            break
                        time.sleep(0.01)
                    else:
                        client_result.append(AssertionError("client bytes were not recorded"))
                        return
                    self.assertTrue(allow_client_receive.wait(timeout=5))
                    client.settimeout(1)
                    client_result.append(client.recv(1))
            except BaseException as error:
                client_result.append(error)
            finally:
                failure_requested.set()
                client_finished.set()

        server.service_actions = service_actions
        client_thread = threading.Thread(target=run_client)
        client_thread.start()
        try:
            with mock.patch(
                "data_record_server.server.create_server", return_value=server
            ), mock.patch(
                "data_record_server.server.signal.getsignal", return_value=object()
            ), mock.patch("data_record_server.server.signal.signal"):
                with self.assertRaises(RuntimeError) as caught:
                    from data_record_server.server import run_server

                    run_server(Config("127.0.0.1", 0, self._data_dir, 4))

            self.assertIs(serve_error, caught.exception)
            if not client_recorded.is_set():
                self.assertTrue(client_finished.wait(timeout=2))
                self.fail(f"client thread failed before recording bytes: {client_result!r}")
            self._wait_for(
                lambda: any(
                    event["event"] == "session_closed"
                    and event["reason"] == "server_shutdown"
                    for event in self._read_events()
                )
            )
            with server._active_requests_condition:
                self.assertFalse(server._active_requests)
            allow_client_receive.set()
            client_thread.join(timeout=2)
            self.assertFalse(client_thread.is_alive())
            self.assertTrue(client_finished.is_set())
            self.assertEqual([b""], client_result, f"client thread failed: {client_result!r}")
            server.wait_for_shutdown_coordinator()
        finally:
            allow_client_receive.set()
            client_thread.join(timeout=2)
            server.shutdown()
            server.server_close()

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

            def server_close(self):
                return None

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

            def server_close(self):
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

            def server_close(self):
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

    def test_run_server_preserves_setup_error_when_server_close_fails(self):
        install_error = ValueError("signal setup failed")
        close_error = OSError("pipe close failed")

        class ControlledServer:
            server_address = ("127.0.0.1", 40123)

            def __init__(self):
                self.close_calls = 0

            def __enter__(self):
                return self

            def __exit__(self, *_arguments):
                self.server_close()
                return False

            def start_shutdown_coordinator(self):
                return True

            def cancel_shutdown_coordinator(self):
                return None

            def wait_for_shutdown_coordinator(self):
                return None

            def server_close(self):
                self.close_calls += 1
                raise close_error

        server = ControlledServer()
        with mock.patch(
            "data_record_server.server.create_server", return_value=server
        ), mock.patch(
            "data_record_server.server.signal.getsignal", return_value=object()
        ), mock.patch(
            "data_record_server.server.signal.signal", side_effect=install_error
        ), mock.patch("data_record_server.server.LOGGER.exception") as log_exception:
            with self.assertRaises(ValueError) as caught:
                from data_record_server.server import run_server

                run_server(Config("127.0.0.1", 0, self._data_dir, 4))

        self.assertIs(install_error, caught.exception)
        self.assertEqual(1, server.close_calls)
        log_exception.assert_called_once()

    def test_run_server_propagates_server_close_error_without_primary_error(self):
        close_error = OSError("pipe close failed")

        class ControlledServer:
            server_address = ("127.0.0.1", 40123)

            def __init__(self):
                self.close_calls = 0

            def __enter__(self):
                return self

            def __exit__(self, *_arguments):
                self.server_close()
                return False

            def start_shutdown_coordinator(self):
                return True

            def serve_forever(self):
                return None

            def notify_shutdown(self):
                return None

            def wait_for_shutdown_coordinator(self):
                return None

            def server_close(self):
                self.close_calls += 1
                raise close_error

        server = ControlledServer()
        with mock.patch(
            "data_record_server.server.create_server", return_value=server
        ), mock.patch(
            "data_record_server.server.signal.getsignal", return_value=object()
        ), mock.patch("data_record_server.server.signal.signal"):
            with self.assertRaises(OSError) as caught:
                from data_record_server.server import run_server

                run_server(Config("127.0.0.1", 0, self._data_dir, 4))

        self.assertIs(close_error, caught.exception)
        self.assertEqual(1, server.close_calls)

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

    def _create_server_with_idle_timeout(self, idle_timeout_seconds):
        server = create_server(
            Config(
                host="127.0.0.1",
                port=0,
                data_dir=self._data_dir,
                read_buffer_bytes=4,
                idle_timeout_seconds=idle_timeout_seconds,
            )
        )
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.01},
        )
        thread.start()
        return server, thread

    def _assert_file_descriptors_closed(self, file_descriptors):
        for file_descriptor in file_descriptors:
            with self.assertRaises(OSError) as caught:
                os.fstat(file_descriptor)
            self.assertEqual(errno.EBADF, caught.exception.errno)

    @staticmethod
    def _close_file_descriptors(file_descriptors):
        for file_descriptor in file_descriptors:
            try:
                os.close(file_descriptor)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise

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
    def _read_events_from(data_dir):
        events_path = data_dir / "events.jsonl"
        if not events_path.exists():
            return []
        return [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
        ]

    @staticmethod
    def _contains_payload(data_dir, expected_payload):
        return any(
            path.read_bytes() == expected_payload
            for path in (data_dir / "devices").glob("**/*.jsonl")
        )

    def _device_files(self):
        return sorted((self._data_dir / "devices").glob("**/*.jsonl"))

    def _connection_files(self):
        return sorted((self._data_dir / "connections").glob("*.bin"))

    def _session_events(self):
        return [
            event
            for event in self._read_events()
            if event["event"].startswith("session_")
        ]

    def _rejection_events(self):
        return [
            event
            for event in self._read_events()
            if event["event"] == "record_rejected"
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
