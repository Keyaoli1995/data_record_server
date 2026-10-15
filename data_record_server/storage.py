"""Device-scoped JSONL record persistence and lifecycle events."""

import json
import os
import stat
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Dict, Optional, Set, Tuple
from urllib.parse import quote_from_bytes

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
        line = json.dumps(event, ensure_ascii=True, separators=(",", ":"))
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
        reporting=None,
    ) -> None:
        self._reporting = reporting
        self.device_id = device_id
        self.relative_path = relative_path
        self._stream = stream
        self._events = events
        self._attached_clients: Set[ClientAddress] = set()
        self._total_bytes = 0
        self._last_valid_record_at: Optional[datetime] = None
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
            idle_error = None  # type: Optional[BaseException]
            try:
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
            except BaseException as error:
                idle_error = error
            try:
                self._close_locked("idle_timeout", now)
            except BaseException as error:
                if idle_error is None:
                    idle_error = error
            if idle_error is not None:
                raise idle_error
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
        close_error = None  # type: Optional[BaseException]
        try:
            if self._reporting is not None:
                self._stream.flush()
                os.fsync(self._stream.fileno())
        except BaseException as error:
            close_error = error
        try:
            self._stream.close()
        except BaseException as error:
            if close_error is None:
                close_error = error
        if self._reporting is not None:
            self._reporting.closed(self.relative_path,
                                   "handler_error" if close_error else reason,
                                   format_time(closed_at))
        try:
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
        except BaseException as error:
            if close_error is None:
                close_error = error
        if close_error is not None:
            raise close_error


class RejectedConnectionRecorder:
    """Persist rejected records from one TCP connection in a diagnostic BIN."""

    def __init__(
        self,
        client_address: ClientAddress,
        relative_path: str,
        stream: BinaryIO,
        events: JsonlEventWriter,
        clock: Clock,
        on_close: Callable[[str], None],
    ) -> None:
        self.relative_path = relative_path
        self._client_address = client_address
        self._stream = stream
        self._events = events
        self._clock = clock
        self._on_close = on_close
        self._total_bytes = 0
        self._closed = False
        self._lock = threading.Lock()

    def record_rejected(self, reason: str, raw_bytes: bytes) -> None:
        """Append rejected bytes before recording their diagnostic event."""
        with self._lock:
            if self._closed:
                raise RuntimeError("cannot record to a closed rejected connection")
            self._stream.write(raw_bytes)
            self._stream.flush()
            self._total_bytes += len(raw_bytes)
            client_ip, client_port = self._client_address
            self._events.write(
                {
                    "event": "record_rejected",
                    "time": format_time(self._clock()),
                    "file": self.relative_path,
                    "client_ip": client_ip,
                    "client_port": client_port,
                    "reason": reason,
                    "rejected_bytes": len(raw_bytes),
                    "total_bytes": self._total_bytes,
                }
            )

    def close(self, reason: str) -> bool:
        """Close once, persist the close event, and release the active path."""
        with self._lock:
            if self._closed:
                return False
            self._closed = True
            close_error = None  # type: Optional[BaseException]
            try:
                self._stream.close()
            except BaseException as error:
                close_error = error
            try:
                self._events.write(
                    {
                        "event": "rejected_connection_closed",
                        "time": format_time(self._clock()),
                        "file": self.relative_path,
                        "total_bytes": self._total_bytes,
                        "reason": reason,
                    }
                )
            except BaseException as error:
                if close_error is None:
                    close_error = error
            finally:
                self._on_close(self.relative_path)
            if close_error is not None:
                raise close_error
            return True

    def abandon(self) -> None:
        """Close a newly created diagnostic stream without lifecycle events."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._stream.close()
            finally:
                self._on_close(self.relative_path)


class Storage:
    """Maintain active device sessions and record their lifecycle events."""

    def __init__(self, data_dir: Path, clock: Clock = utc_now, reporting=None) -> None:
        self.reporting = reporting
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._events = JsonlEventWriter(self._data_dir / "events.jsonl")
        self._clock = clock
        self._sessions: Dict[str, DeviceSession] = {}
        self._sessions_lock = threading.Lock()
        self._active_rejected_paths: Set[str] = set()
        self._rejected_paths_lock = threading.Lock()

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

    def open_rejected_connection(
        self, client_address: ClientAddress
    ) -> RejectedConnectionRecorder:
        """Create one diagnostic BIN for rejected records on a TCP connection."""
        client_ip, client_port = client_address
        created_at = self._clock()
        timestamp = created_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        safe_client_ip = client_ip.replace(":", "_")
        directory = self._data_dir / "connections"
        filename = "{}_{}_{}_{}.bin".format(
            timestamp, safe_client_ip, client_port, uuid.uuid4().hex[:8]
        )
        path = directory / filename
        relative_path = path.relative_to(self._data_dir).as_posix()

        directory.mkdir(parents=True, exist_ok=True)
        stream = path.open("xb")
        recorder = RejectedConnectionRecorder(
            client_address,
            relative_path,
            stream,
            self._events,
            self._clock,
            self._remove_active_rejected_path,
        )
        try:
            self._events.write(
                {
                    "event": "rejected_connection_started",
                    "time": format_time(created_at),
                    "file": relative_path,
                    "client_ip": client_ip,
                    "client_port": client_port,
                }
            )
        except BaseException:
            try:
                recorder.abandon()
            except BaseException:
                pass
            try:
                path.unlink()
            except OSError:
                pass
            try:
                directory.rmdir()
            except OSError:
                pass
            raise
        with self._rejected_paths_lock:
            self._active_rejected_paths.add(relative_path)
        return recorder

    def expire_sessions(self, idle_timeout_seconds: float) -> None:
        """Close and remove device sessions that have reached their idle timeout."""
        now = self._clock()
        first_error = None  # type: Optional[BaseException]
        with self._sessions_lock:
            expired_device_ids = []
            for device_id, session in self._sessions.items():
                try:
                    expired = session.close_if_expired(now, idle_timeout_seconds)
                except BaseException as error:
                    expired = True
                    if first_error is None:
                        first_error = error
                if expired:
                    expired_device_ids.append(device_id)
            for device_id in expired_device_ids:
                del self._sessions[device_id]
        if first_error is not None:
            raise first_error

    def close_all_sessions(self) -> None:
        """Close every active session once as part of server shutdown."""
        closed_at = self._clock()
        first_error = None  # type: Optional[BaseException]
        with self._sessions_lock:
            sessions = tuple(self._sessions.values())
            for session in sessions:
                try:
                    session.close("server_shutdown", closed_at)
                except BaseException as error:
                    if first_error is None:
                        first_error = error
            self._sessions.clear()
        if first_error is not None:
            raise first_error

    def cleanup_expired_files(
        self,
        device_retention_days: int,
        connection_retention_days: int,
        now: datetime,
        trigger: str,
    ) -> None:
        """Remove inactive archives per archive kind's own retention period."""
        with self._sessions_lock:
            active_session_paths = {
                session.relative_path for session in self._sessions.values()
            }
        with self._rejected_paths_lock:
            active_paths = active_session_paths | set(self._active_rejected_paths)

        if self.reporting is not None:
            active_paths |= self.reporting.protected_paths()
        self._cleanup_device_archives(device_retention_days, now, trigger, active_paths)
        self._cleanup_connection_archives(
            connection_retention_days, now, trigger, active_paths
        )

    def _cleanup_device_archives(
        self,
        retention_days: int,
        now: datetime,
        trigger: str,
        active_paths: Set[str],
    ) -> None:
        """Remove expired device JSONL archives and the directories they empty."""
        cutoff = now.timestamp() - retention_days * 86400
        deleted_files, reclaimed_bytes, failed_files = self._unlink_expired_files(
            self._device_archive_paths(), active_paths, cutoff, self._data_dir
        )

        for directory in self._device_directories():
            try:
                if stat.S_ISDIR(directory.lstat().st_mode):
                    directory.rmdir()
            except OSError:
                pass

        self._write_cleanup_event(
            "device_retention_cleanup",
            now,
            trigger,
            deleted_files,
            reclaimed_bytes,
            failed_files,
        )

    def _cleanup_connection_archives(
        self,
        retention_days: int,
        now: datetime,
        trigger: str,
        active_paths: Set[str],
    ) -> None:
        """Remove expired rejected connection diagnostics."""
        cutoff = now.timestamp() - retention_days * 86400
        deleted_files, reclaimed_bytes, failed_files = self._unlink_expired_files(
            self._connection_archive_paths(), active_paths, cutoff, self._data_dir
        )
        self._write_cleanup_event(
            "connection_retention_cleanup",
            now,
            trigger,
            deleted_files,
            reclaimed_bytes,
            failed_files,
        )

    def _write_cleanup_event(
        self,
        event: str,
        now: datetime,
        trigger: str,
        deleted_files: int,
        reclaimed_bytes: int,
        failed_files: int,
    ) -> None:
        self._events.write(
            {
                "event": event,
                "time": format_time(now),
                "trigger": trigger,
                "deleted_files": deleted_files,
                "reclaimed_bytes": reclaimed_bytes,
                "failed_files": failed_files,
            }
        )

    @staticmethod
    def _unlink_expired_files(
        paths, active_paths: Set[str], cutoff: float, data_dir: Path
    ) -> Tuple[int, int, int]:
        """Unlink inactive regular files whose modification time precedes the cutoff."""
        deleted_files = 0
        reclaimed_bytes = 0
        failed_files = 0
        for path in paths:
            relative_path = path.relative_to(data_dir).as_posix()
            if relative_path in active_paths:
                continue
            try:
                file_status = path.lstat()
                if not stat.S_ISREG(file_status.st_mode):
                    continue
                if file_status.st_mtime >= cutoff:
                    continue
                path.unlink()
            except OSError:
                failed_files += 1
                continue
            deleted_files += 1
            reclaimed_bytes += file_status.st_size
        return deleted_files, reclaimed_bytes, failed_files

    def _device_archive_paths(self):
        for directory in self._device_directories():
            try:
                if stat.S_ISDIR(directory.lstat().st_mode):
                    yield from directory.glob("*.jsonl")
            except OSError:
                continue

    def _connection_archive_paths(self):
        connections_directory = self._data_dir / "connections"
        if self._is_real_directory(connections_directory):
            yield from connections_directory.glob("*.bin")

    def _create_session(self, device_id: str) -> DeviceSession:
        escaped_device_id = quote_from_bytes(
            device_id.encode("utf-8", "surrogatepass"), safe=""
        )
        directory = self._data_dir / "devices" / ("device-" + escaped_device_id)
        timestamp = format_time(self._clock())
        filename = "{}_{}_{}.jsonl".format(
            timestamp, escaped_device_id, uuid.uuid4().hex[:8]
        )
        path = directory / filename
        relative_path = path.relative_to(self._data_dir).as_posix()

        directory.mkdir(parents=True, exist_ok=True)
        if self.reporting is not None:
            self.reporting.register(relative_path, device_id, timestamp)
        stream = path.open("xb")
        session = DeviceSession(device_id, relative_path, stream, self._events, self.reporting)
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
            try:
                session.abandon()
            except BaseException:
                pass
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

    def _remove_active_rejected_path(self, relative_path: str) -> None:
        """Remove a closed diagnostic file from the cleanup exclusion set."""
        with self._rejected_paths_lock:
            self._active_rejected_paths.discard(relative_path)

    def _device_directories(self):
        devices_directory = self._data_dir / "devices"
        if self._is_real_directory(devices_directory):
            yield from devices_directory.glob("device-*")

    @staticmethod
    def _is_real_directory(path: Path) -> bool:
        try:
            return stat.S_ISDIR(path.lstat().st_mode)
        except OSError:
            return False
