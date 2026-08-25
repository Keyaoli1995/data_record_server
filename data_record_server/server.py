"""Concurrent TCP server for recording raw client byte streams."""

import errno
import logging
import os
import signal
import socket
import socketserver
import sys
import threading
from typing import Callable, Tuple

from .config import Config
from .protocol import JsonlStreamParser
from .storage import Storage


LOGGER = logging.getLogger(__name__)


ServerAddress = Tuple[str, int]


class CollectorRequestHandler(socketserver.BaseRequestHandler):
    """Parse and persist JSONL records received from one TCP connection."""

    def handle(self) -> None:
        parser = JsonlStreamParser(self.server.max_json_line_bytes)
        try:
            self.request.settimeout(self.server.idle_timeout_seconds)
            while True:
                data = self.request.recv(self.server.read_buffer_bytes)
                if not data:
                    self._store_feed_result(parser.finish())
                    break
                result = parser.feed(data)
                self._store_feed_result(result)
                if result.close_connection:
                    return
        except socket.timeout:
            return
        except Exception:
            LOGGER.exception(
                "Failed while recording client connection from %s", self.client_address
            )

    def _store_feed_result(self, result) -> None:
        for record in result.records:
            self.server.storage.append_record(
                self.client_address, record.device_id, record.raw_line
            )
        for rejection in result.rejections:
            self.server.storage.record_rejected(self.client_address, rejection.reason)


class CollectorServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Thread-per-connection TCP server with injected byte storage."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: ServerAddress,
        storage: Storage,
        read_buffer_bytes: int,
        idle_timeout_seconds: float = 30,
        max_json_line_bytes: int = 65536,
    ) -> None:
        self.storage = storage
        self.read_buffer_bytes = read_buffer_bytes
        self.idle_timeout_seconds = idle_timeout_seconds
        self.max_json_line_bytes = max_json_line_bytes
        self._active_requests = set()
        self._active_requests_condition = threading.Condition()
        self._sessions_close_lock = threading.Lock()
        self._sessions_closed = False
        self._shutdown_coordinator = None
        self._shutdown_coordinator_lock = threading.Lock()
        self._shutdown_coordinator_state = threading.Condition()
        self._serve_loop_ready = False
        self._serve_loop_completed = False
        self._shutdown_coordinator_cancelled = False
        self._shutdown_pipe_close_lock = threading.Lock()
        self._shutdown_pipe_read_fd, self._shutdown_pipe_write_fd = os.pipe()
        self._construction_in_progress = True
        try:
            os.set_blocking(self._shutdown_pipe_write_fd, False)
            super().__init__(server_address, CollectorRequestHandler)
        except BaseException:
            self._close_shutdown_pipe_preserving_primary_error()
            raise
        self._construction_in_progress = False

    def process_request(self, request, client_address) -> None:
        """Track a request before starting its worker thread."""
        with self._active_requests_condition:
            self._active_requests.add(request)

        try:
            super().process_request(request, client_address)
        except BaseException:
            self.shutdown_request(request)
            raise

    def shutdown_request(self, request) -> None:
        """Close a handled request and mark its worker as finished."""
        try:
            super().shutdown_request(request)
        finally:
            with self._active_requests_condition:
                self._active_requests.discard(request)
                self._active_requests_condition.notify_all()

    def shutdown(self) -> None:
        """Stop accepting clients and finish cleanup for active connections."""
        primary_error = None
        try:
            super().shutdown()
        except BaseException as error:
            primary_error = error

        primary_error = self._run_cleanup_step(
            self._close_active_requests_and_wait,
            "Failed to close active TCP client requests during shutdown",
            primary_error,
        )
        primary_error = self._run_cleanup_step(
            self._close_all_sessions_once,
            "Failed to close active device sessions during shutdown",
            primary_error,
        )
        if primary_error is not None:
            raise primary_error

    def service_actions(self) -> None:
        """Expire inactive device sessions between request polls."""
        self.storage.expire_sessions(self.idle_timeout_seconds)
        super().service_actions()

    def _close_active_requests_and_wait(self) -> None:
        with self._active_requests_condition:
            active_requests = tuple(self._active_requests)

        for request in active_requests:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        with self._active_requests_condition:
            while self._active_requests:
                self._active_requests_condition.wait()

    def _close_all_sessions_once(self) -> None:
        with self._sessions_close_lock:
            if self._sessions_closed:
                return
            self._sessions_closed = True
        self.storage.close_all_sessions()

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        """Run the base server after making coordinator shutdown safe."""
        self._BaseServer__is_shut_down.clear()
        with self._shutdown_coordinator_state:
            self._serve_loop_ready = True
            self._serve_loop_completed = False
            self._shutdown_coordinator_state.notify_all()
        try:
            super().serve_forever(poll_interval)
        finally:
            with self._shutdown_coordinator_state:
                self._serve_loop_ready = False
                self._serve_loop_completed = True
                self._shutdown_coordinator_state.notify_all()

    def start_shutdown_coordinator(self) -> bool:
        """Start the single worker that coordinates signal-triggered shutdown."""
        with self._shutdown_coordinator_lock:
            if self._shutdown_coordinator is not None:
                return False
            worker = threading.Thread(
                target=self._run_shutdown_coordinator,
                daemon=True,
            )
            self._shutdown_coordinator = worker

        try:
            worker.start()
        except BaseException:
            with self._shutdown_coordinator_lock:
                if self._shutdown_coordinator is worker:
                    self._shutdown_coordinator = None
            raise
        return True

    def notify_shutdown(self) -> None:
        """Request shutdown without allocating or synchronizing in a signal handler."""
        write_fd = self._shutdown_pipe_write_fd
        if write_fd is None:
            return
        try:
            os.write(write_fd, b"\x00")
        except BlockingIOError:
            return
        except OSError as error:
            if error.errno == errno.EBADF:
                return
            raise

    def cancel_shutdown_coordinator(self) -> None:
        """Wake the coordinator without allowing a pre-serve shutdown call."""
        with self._shutdown_coordinator_state:
            self._shutdown_coordinator_cancelled = True
            self._shutdown_coordinator_state.notify_all()
        self.notify_shutdown()

    def wait_for_shutdown_coordinator(self) -> None:
        """Wait for the shutdown coordinator when called from another thread."""
        with self._shutdown_coordinator_lock:
            worker = self._shutdown_coordinator
        if worker is not None and worker is not threading.current_thread():
            worker.join()

    def server_close(self) -> None:
        """Stop the coordinator and close the self-pipe exactly once."""
        if self._construction_in_progress:
            primary_error = sys.exc_info()[1]
            tcp_close_error = None
            try:
                super().server_close()
            except BaseException as error:
                if primary_error is None:
                    tcp_close_error = error
                else:
                    self._log_cleanup_failure(
                        "Failed to close TCP socket during construction"
                    )
            try:
                self._close_shutdown_pipe()
            except BaseException:
                if primary_error is None and tcp_close_error is None:
                    raise
                self._log_cleanup_failure(
                    "Failed to close TCP shutdown pipe during construction"
                )
            if tcp_close_error is not None:
                raise tcp_close_error
            return
        primary_error = None
        primary_error = self._run_cleanup_step(
            self.cancel_shutdown_coordinator,
            "Failed to cancel TCP shutdown coordinator",
            primary_error,
        )
        primary_error = self._run_cleanup_step(
            self.wait_for_shutdown_coordinator,
            "Failed to wait for TCP shutdown coordinator",
            primary_error,
        )
        primary_error = self._run_cleanup_step(
            self._close_active_requests_and_wait,
            "Failed to close active TCP client requests during server close",
            primary_error,
        )
        primary_error = self._run_cleanup_step(
            self._close_all_sessions_once,
            "Failed to close active device sessions during server close",
            primary_error,
        )
        primary_error = self._run_cleanup_step(
            super().server_close,
            "Failed to close TCP server socket",
            primary_error,
        )
        primary_error = self._run_cleanup_step(
            self._close_shutdown_pipe,
            "Failed to close TCP shutdown pipe",
            primary_error,
        )
        if primary_error is not None:
            raise primary_error

    def _run_shutdown_coordinator(self) -> None:
        try:
            notification = os.read(self._shutdown_pipe_read_fd, 1)
        except OSError as error:
            if error.errno == errno.EBADF:
                return
            raise
        if not notification:
            return
        with self._shutdown_coordinator_state:
            while (
                not self._serve_loop_ready
                and not self._serve_loop_completed
                and not self._shutdown_coordinator_cancelled
            ):
                self._shutdown_coordinator_state.wait()
            if self._shutdown_coordinator_cancelled:
                return
        self.shutdown()

    def _close_shutdown_pipe(self) -> None:
        with self._shutdown_pipe_close_lock:
            read_fd = self._shutdown_pipe_read_fd
            write_fd = self._shutdown_pipe_write_fd
            self._shutdown_pipe_read_fd = None
            self._shutdown_pipe_write_fd = None
        first_error = None
        for file_descriptor in (write_fd, read_fd):
            if file_descriptor is None:
                continue
            try:
                os.close(file_descriptor)
            except OSError as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    def _close_shutdown_pipe_preserving_primary_error(self) -> None:
        try:
            self._close_shutdown_pipe()
        except BaseException:
            self._log_cleanup_failure(
                "Failed to close TCP shutdown pipe during construction"
            )

    def _run_cleanup_step(
        self, operation, message: str, primary_error: BaseException
    ) -> BaseException:
        try:
            operation()
        except BaseException as error:
            if primary_error is None:
                return error
            self._log_cleanup_failure(message)
        return primary_error

    @staticmethod
    def _log_cleanup_failure(message: str) -> None:
        try:
            LOGGER.exception(message)
        except BaseException:
            pass


def create_server(config: Config) -> CollectorServer:
    """Create a collector bound to the configured address."""
    return CollectorServer(
        (config.host, config.port),
        Storage(config.data_dir),
        config.read_buffer_bytes,
        config.idle_timeout_seconds,
        config.max_json_line_bytes,
    )


def create_shutdown_handler(server: CollectorServer) -> Callable[[int, object], None]:
    """Create a signal handler that requests a server shutdown safely."""

    def shutdown_handler(_signum: int, _frame: object) -> None:
        server.notify_shutdown()

    return shutdown_handler


def run_server(config: Config) -> None:
    """Run a configured collector until it is stopped by a termination signal."""
    server = create_server(config)
    primary_exception = None
    try:
        signals = (signal.SIGINT, signal.SIGTERM)
        previous_handlers = {
            received_signal: signal.getsignal(received_signal)
            for received_signal in signals
        }
        shutdown_handler = create_shutdown_handler(server)
        coordinator_started = False
        serve_forever_started = False
        installed_signals = []

        def restore_installed_handlers() -> None:
            for received_signal in reversed(installed_signals):
                try:
                    signal.signal(received_signal, previous_handlers[received_signal])
                except BaseException:
                    LOGGER.exception(
                        "Failed to restore signal handler for %s", received_signal
                    )

        try:
            server.start_shutdown_coordinator()
            coordinator_started = True
            for received_signal in signals:
                signal.signal(received_signal, shutdown_handler)
                installed_signals.append(received_signal)
            LOGGER.info("Listening on %s:%s", *server.server_address)
            serve_forever_started = True
            server.serve_forever()
        except BaseException:
            try:
                if coordinator_started:
                    if serve_forever_started:
                        server.notify_shutdown()
                    else:
                        server.cancel_shutdown_coordinator()
                    server.wait_for_shutdown_coordinator()
            except BaseException:
                LOGGER.exception("Failed while cleaning up TCP shutdown coordinator")
            restore_installed_handlers()
            raise
        else:
            try:
                if coordinator_started:
                    server.notify_shutdown()
                    server.wait_for_shutdown_coordinator()
            finally:
                restore_installed_handlers()
    except BaseException as error:
        primary_exception = error
        raise
    finally:
        try:
            server.server_close()
        except BaseException:
            if primary_exception is None:
                raise
            LOGGER.exception("Failed to close TCP server while preserving primary error")
