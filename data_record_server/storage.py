"""Device-scoped JSONL record persistence and lifecycle events."""

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Dict, Set, Tuple
from urllib.parse import quote

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


class DeviceSession:
    """One open JSONL stream shared by all clients of one device."""

    def __init__(
        self,
        device_id: str,
        relative_path: str,
        stream: BinaryIO,
        events: JsonlEventWriter,
        clock: Clock,
    ) -> None:
        self.device_id = device_id
        self.relative_path = relative_path
        self._stream = stream
        self._events = events
        self._clock = clock
        self._attached_clients: Set[ClientAddress] = set()
        self._total_bytes = 0
        self._last_valid_record_at = None  # type: datetime
        self._closed = False
        self._lock = threading.Lock()

    def append(
        self, client_address: ClientAddress, raw_line: bytes, recorded_at: datetime
    ) -> None:
        """Append one valid record, recording a client's first attachment."""
        with self._lock:
            if self._closed:
                raise RuntimeError("cannot append to a closed device session")
            if client_address not in self._attached_clients:
                client_ip, client_port = client_address
                self._events.write(
                    {
                        "event": "session_attached",
                        "time": format_time(recorded_at),
                        "file": self.relative_path,
                        "device_id": self.device_id,
                        "client_ip": client_ip,
                        "client_port": client_port,
                    }
                )
                self._attached_clients.add(client_address)
            self._stream.write(raw_line)
            self._stream.flush()
            self._total_bytes += len(raw_line)
            self._last_valid_record_at = recorded_at

    def close_if_expired(
        self, now: datetime, idle_timeout_seconds: float
    ) -> bool:
        """Close the session if its last record is at least the idle timeout old."""
        with self._lock:
            if self._closed or self._last_valid_record_at is None:
                return False
            timeout = timedelta(seconds=idle_timeout_seconds)
            if now - self._last_valid_record_at < timeout:
                return False
            self._events.write(
                {
                    "event": "session_idle_timeout",
                    "time": format_time(now),
                    "file": self.relative_path,
                    "device_id": self.device_id,
                    "idle_timeout_seconds": idle_timeout_seconds,
                    "total_bytes": self._total_bytes,
                }
            )
            self._close_locked("idle_timeout", now)
            return True

    def close(self, reason: str, closed_at: datetime) -> bool:
        """Close once and persist the close event. Return whether it closed."""
        with self._lock:
            if self._closed:
                return False
            self._close_locked(reason, closed_at)
            return True

    def abandon(self) -> None:
        """Close a newly created stream without recording a lifecycle event."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stream.close()

    def _close_locked(self, reason: str, closed_at: datetime) -> None:
        self._closed = True
        self._stream.close()
        self._events.write(
            {
                "event": "session_closed",
                "time": format_time(closed_at),
                "file": self.relative_path,
                "device_id": self.device_id,
                "total_bytes": self._total_bytes,
                "reason": reason,
            }
        )


class Storage:
    """Maintain active device sessions and record their lifecycle events."""

    def __init__(self, data_dir: Path, clock: Clock = utc_now) -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._events = JsonlEventWriter(self._data_dir / "events.jsonl")
        self._clock = clock
        self._sessions: Dict[str, DeviceSession] = {}
        self._sessions_lock = threading.Lock()

    def append_record(
        self, client_address: ClientAddress, device_id: str, raw_line: bytes
    ) -> DeviceSession:
        """Append a valid JSONL record to its device's active session."""
        with self._sessions_lock:
            session = self._sessions.get(device_id)
            if session is None:
                session = self._create_session(device_id)
                self._sessions[device_id] = session
            session.append(client_address, raw_line, self._clock())
            return session

    def record_rejected(self, client_address: ClientAddress, reason: str) -> None:
        """Persist a diagnostic for an invalid record without opening a session."""
        client_ip, client_port = client_address
        self._events.write(
            {
                "event": "record_rejected",
                "time": format_time(self._clock()),
                "client_ip": client_ip,
                "client_port": client_port,
                "reason": reason,
            }
        )

    def expire_sessions(self, idle_timeout_seconds: float) -> None:
        """Close and remove device sessions that have reached their idle timeout."""
        now = self._clock()
        with self._sessions_lock:
            expired_device_ids = []
            for device_id, session in self._sessions.items():
                if session.close_if_expired(now, idle_timeout_seconds):
                    expired_device_ids.append(device_id)
            for device_id in expired_device_ids:
                del self._sessions[device_id]

    def close_all_sessions(self) -> None:
        """Close every active session once as part of server shutdown."""
        with self._sessions_lock:
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close("server_shutdown", self._clock())

    def _create_session(self, device_id: str) -> DeviceSession:
        escaped_device_id = quote(device_id, safe="")
        directory = self._data_dir / "devices" / ("device-" + escaped_device_id)
        timestamp = format_time(self._clock())
        filename = "{}_{}_{}.jsonl".format(
            timestamp, escaped_device_id, uuid.uuid4().hex[:8]
        )
        path = directory / filename
        relative_path = path.relative_to(self._data_dir).as_posix()

        directory.mkdir(parents=True, exist_ok=True)
        stream = path.open("xb")
        session = DeviceSession(
            device_id, relative_path, stream, self._events, self._clock
        )
        try:
            self._events.write(
                {
                    "event": "session_started",
                    "time": timestamp,
                    "file": relative_path,
                    "device_id": device_id,
                }
            )
        except BaseException:
            session.abandon()
            try:
                path.unlink()
            except OSError:
                pass
            try:
                directory.rmdir()
            except OSError:
                pass
            try:
                directory.parent.rmdir()
            except OSError:
                pass
            raise
        return session
