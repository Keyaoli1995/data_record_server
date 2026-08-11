"""Raw TCP connection data and lifecycle event persistence."""

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Tuple

Clock = Callable[[], datetime]
ClientAddress = Tuple[str, int]


def utc_now() -> datetime:
    """Return the current timezone-aware UTC time."""
    return datetime.now(timezone.utc)


def format_time(value: datetime) -> str:
    """Format a datetime as an ISO 8601 UTC timestamp with a Z suffix."""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class JsonlEventWriter:
    """Append JSON event objects to one shared JSONL file safely."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def write(self, event: Dict[str, object]) -> None:
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with self._path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
                stream.flush()


class ConnectionRecorder:
    """Persist raw bytes and lifecycle events for one TCP connection."""

    def __init__(
        self,
        data_dir: Path,
        client_address: ClientAddress,
        events: JsonlEventWriter,
        clock: Clock,
    ) -> None:
        client_ip, client_port = client_address
        timestamp = format_time(clock())
        safe_ip = client_ip.replace(":", "_")
        filename = f"{timestamp}_{safe_ip}_{client_port}_{uuid.uuid4().hex[:8]}.bin"

        self.relative_path = (Path("connections") / filename).as_posix()
        self.path = Path(data_dir) / self.relative_path
        self._events = events
        self._clock = clock
        self._stream = self.path.open("xb")
        self._total_bytes = 0
        self._closed = False
        self._close_lock = threading.Lock()

        try:
            self._write_event(
                {
                    "event": "connected",
                    "time": timestamp,
                    "file": self.relative_path,
                    "client_ip": client_ip,
                    "client_port": client_port,
                }
            )
        except BaseException:
            try:
                self._stream.close()
            except BaseException:
                pass
            try:
                self.path.unlink()
            except BaseException:
                pass
            raise

    def record_received(self, data: bytes) -> None:
        self._stream.write(data)
        self._stream.flush()
        self._total_bytes += len(data)
        self._write_event(
            {
                "event": "received",
                "time": format_time(self._clock()),
                "file": self.relative_path,
                "bytes": len(data),
                "hex": data.hex(),
            }
        )

    def record_idle_timeout(self, idle_timeout_seconds: float) -> None:
        self._write_event(
            {
                "event": "idle_timeout",
                "time": format_time(self._clock()),
                "file": self.relative_path,
                "idle_timeout_seconds": idle_timeout_seconds,
                "total_bytes": self._total_bytes,
            }
        )

    def record_error(self, error: BaseException) -> None:
        self._write_event(
            {
                "event": "error",
                "time": format_time(self._clock()),
                "file": self.relative_path,
                "error_type": type(error).__name__,
                "error": str(error),
            }
        )

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._stream.close()
            self._write_event(
                {
                    "event": "disconnected",
                    "time": format_time(self._clock()),
                    "file": self.relative_path,
                    "total_bytes": self._total_bytes,
                }
            )

    def _write_event(self, event: Dict[str, object]) -> None:
        self._events.write(event)


class Storage:
    """Factory for per-connection raw-data recorders."""

    def __init__(self, data_dir: Path, clock: Clock = utc_now) -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        (self._data_dir / "connections").mkdir(exist_ok=True)
        self._events = JsonlEventWriter(self._data_dir / "events.jsonl")
        self._clock = clock

    def open_connection(self, client_address: ClientAddress) -> ConnectionRecorder:
        return ConnectionRecorder(
            self._data_dir, client_address, self._events, self._clock
        )
