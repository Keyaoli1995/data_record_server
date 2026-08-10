"""Concurrent TCP server for recording raw client byte streams."""

import logging
import signal
import socket
import socketserver
import threading
from typing import Callable

from .config import Config
from .storage import Storage


LOGGER = logging.getLogger(__name__)
_SHUTDOWN_SIGNALS = frozenset((signal.SIGINT, signal.SIGTERM))


class CollectorRequestHandler(socketserver.BaseRequestHandler):
    """Record every byte received from one TCP client connection."""

    def handle(self) -> None:
        recorder = None
        try:
            recorder = self.server.storage.open_connection(self.client_address)
            while True:
                data = self.request.recv(self.server.read_buffer_bytes)
                if not data:
                    break
                recorder.record_received(data)
        except Exception as error:
            if recorder is not None:
                try:
                    recorder.record_error(error)
                except Exception:
                    LOGGER.exception(
                        "Failed to record client connection error from %s",
                        self.client_address,
                    )
            LOGGER.exception(
                "Failed while recording client connection from %s", self.client_address
            )
        finally:
            if recorder is not None:
                try:
                    recorder.close()
                except Exception:
                    LOGGER.exception(
                        "Failed to close client connection recorder for %s",
                        self.client_address,
                    )


class CollectorServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Thread-per-connection TCP server with injected byte storage."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        storage: Storage,
        read_buffer_bytes: int,
    ) -> None:
        self.storage = storage
        self.read_buffer_bytes = read_buffer_bytes
        self._active_requests = set()
        self._active_requests_condition = threading.Condition()
        self._shutdown_worker = None
        self._shutdown_worker_lock = threading.RLock()
        super().__init__(server_address, CollectorRequestHandler)

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
        super().shutdown()
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

    def register_shutdown_worker(self, worker) -> bool:
        """Keep ownership of the first signal-triggered shutdown worker."""
        with self._shutdown_worker_lock:
            if self._shutdown_worker is not None:
                return False
            self._shutdown_worker = worker
            return True

    def wait_for_shutdown_worker(self) -> None:
        """Wait for the signal-triggered shutdown worker when called externally."""
        with self._shutdown_worker_lock:
            worker = self._shutdown_worker
        if worker is not None and worker is not threading.current_thread():
            worker.join()

    def clear_shutdown_worker(self, worker) -> None:
        """Release ownership when a registered worker cannot be started."""
        with self._shutdown_worker_lock:
            if self._shutdown_worker is worker:
                self._shutdown_worker = None


def create_server(config: Config) -> CollectorServer:
    """Create a collector bound to the configured address."""
    return CollectorServer(
        (config.host, config.port), Storage(config.data_dir), config.read_buffer_bytes
    )


def create_shutdown_handler(server: CollectorServer) -> Callable[[int, object], None]:
    """Create a signal handler that requests a server shutdown safely."""

    def shutdown_handler(signum: int, _frame: object) -> None:
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _SHUTDOWN_SIGNALS)
        try:
            LOGGER.info("Received signal %s; shutting down", signum)
            worker = threading.Thread(target=server.shutdown, daemon=True)
            register_shutdown_worker = getattr(server, "register_shutdown_worker", None)
            if register_shutdown_worker is None or register_shutdown_worker(worker):
                try:
                    worker.start()
                except BaseException:
                    clear_shutdown_worker = getattr(
                        server, "clear_shutdown_worker", None
                    )
                    if clear_shutdown_worker is not None:
                        clear_shutdown_worker(worker)
                    raise
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)

    return shutdown_handler


def run_server(config: Config) -> None:
    """Run a configured collector until it is stopped by a termination signal."""
    with create_server(config) as server:
        signals = (signal.SIGINT, signal.SIGTERM)
        previous_handlers = {
            received_signal: signal.getsignal(received_signal)
            for received_signal in signals
        }
        shutdown_handler = create_shutdown_handler(server)
        serve_forever_started = False

        try:
            for received_signal in signals:
                signal.signal(received_signal, shutdown_handler)
            LOGGER.info("Listening on %s:%s", *server.server_address)
            serve_forever_started = True
            server.serve_forever()
        finally:
            try:
                if serve_forever_started:
                    server.shutdown()
                    server.wait_for_shutdown_worker()
            finally:
                for received_signal, previous_handler in previous_handlers.items():
                    signal.signal(received_signal, previous_handler)
