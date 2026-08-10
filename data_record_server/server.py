"""Concurrent TCP server for recording raw client byte streams."""

import logging
import signal
import socketserver
import threading
from typing import Callable

from .config import Config
from .storage import Storage


LOGGER = logging.getLogger(__name__)


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
                    LOGGER.exception("Failed to record client connection error")
            LOGGER.exception("Failed while recording client connection")
        finally:
            if recorder is not None:
                try:
                    recorder.close()
                except Exception:
                    LOGGER.exception("Failed to close client connection recorder")


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
        super().__init__(server_address, CollectorRequestHandler)


def create_server(config: Config) -> CollectorServer:
    """Create a collector bound to the configured address."""
    return CollectorServer(
        (config.host, config.port), Storage(config.data_dir), config.read_buffer_bytes
    )


def create_shutdown_handler(server: CollectorServer) -> Callable[[int, object], None]:
    """Create a signal handler that requests a server shutdown safely."""

    def shutdown_handler(signum: int, _frame: object) -> None:
        LOGGER.info("Received signal %s; shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

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

        try:
            for received_signal in signals:
                signal.signal(received_signal, shutdown_handler)
            LOGGER.info("Listening on %s:%s", *server.server_address)
            server.serve_forever()
        finally:
            for received_signal, previous_handler in previous_handlers.items():
                signal.signal(received_signal, previous_handler)
