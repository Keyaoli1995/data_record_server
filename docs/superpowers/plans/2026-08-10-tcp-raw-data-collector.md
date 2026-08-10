# TCP Raw Data Collector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Dockerized TCP service that listens on port 30050, preserves each connection's exact byte stream, and records connection metadata as JSONL.

**Architecture:** A standard-library Python package separates environment configuration, thread-safe persistence, and the threaded TCP server. Docker Compose publishes the configured port and bind-mounts `data/` so captured bytes survive container replacement and host restarts.

**Tech Stack:** Python 3.12 standard library, `unittest`, Docker, Docker Compose

---

## File map

- `data_record_server/config.py`: validated environment configuration.
- `data_record_server/storage.py`: raw `.bin` files and synchronized JSONL event output.
- `data_record_server/server.py`: concurrent TCP listener, connection lifecycle, and signal handling.
- `data_record_server/__main__.py`: executable entry point for `python -m data_record_server`.
- `tests/test_config.py`: configuration defaults, overrides, and invalid boundaries.
- `tests/test_storage.py`: exact-byte persistence and event schema.
- `tests/test_server.py`: real localhost TCP integration tests and shutdown behavior.
- `tests/test_deployment.py`: deployment-file contract tests.
- `Dockerfile`, `compose.yaml`, `.dockerignore`: reproducible long-running container deployment.
- `.gitignore`: excludes captured device data and Python artifacts.
- `README.md`: local testing, server deployment, operation, and data inspection.

## Task 1: Configuration contract

**Files:**
- Create: `.gitignore`
- Create: `tests/__init__.py`
- Create: `tests/test_config.py`
- Create: `data_record_server/__init__.py`
- Create: `data_record_server/config.py`

- [ ] **Step 1: [Red Stage] Write configuration tests**

Create `tests/__init__.py` as an empty file and create `tests/test_config.py`:

```python
import os
import unittest
from pathlib import Path
from unittest import mock

from data_record_server.config import Config


class ConfigTest(unittest.TestCase):
    def test_uses_documented_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            config = Config.from_environ()

        self.assertEqual("0.0.0.0", config.host)
        self.assertEqual(30050, config.port)
        self.assertEqual(Path("/data"), config.data_dir)
        self.assertEqual(4096, config.read_buffer_bytes)

    def test_reads_environment_overrides(self):
        environment = {
            "TCP_HOST": "127.0.0.1",
            "TCP_PORT": "40123",
            "DATA_DIR": "/tmp/collector-data",
            "READ_BUFFER_BYTES": "8192",
        }

        with mock.patch.dict(os.environ, environment, clear=True):
            config = Config.from_environ()

        self.assertEqual("127.0.0.1", config.host)
        self.assertEqual(40123, config.port)
        self.assertEqual(Path("/tmp/collector-data"), config.data_dir)
        self.assertEqual(8192, config.read_buffer_bytes)

    def test_rejects_invalid_ports(self):
        for value in ("0", "65536", "not-a-number"):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"TCP_PORT": value}, clear=True):
                    with self.assertRaisesRegex(ValueError, "TCP_PORT"):
                        Config.from_environ()

    def test_rejects_non_positive_read_buffer(self):
        with mock.patch.dict(
            os.environ, {"READ_BUFFER_BYTES": "0"}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "READ_BUFFER_BYTES"):
                Config.from_environ()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and verify the expected failure**

Run:

```bash
python3 -m unittest tests.test_config -v
```

Expected: `ERROR` with `ModuleNotFoundError: No module named 'data_record_server'`.

- [ ] **Step 3: Commit the Red stage**

```bash
git add tests/__init__.py tests/test_config.py
git commit -m "test: init cases for TCP server configuration"
```

- [ ] **Step 4: [Green Stage] Implement the minimum configuration module**

Create `data_record_server/__init__.py` as an empty file and create `data_record_server/config.py`:

```python
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


def _read_int(
    environment: Mapping[str, str], name: str, default: int
) -> int:
    raw_value = environment.get(name, str(default))
    try:
        return int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    data_dir: Path
    read_buffer_bytes: int

    @classmethod
    def from_environ(
        cls, environment: Optional[Mapping[str, str]] = None
    ) -> "Config":
        values = os.environ if environment is None else environment
        port = _read_int(values, "TCP_PORT", 30050)
        read_buffer_bytes = _read_int(values, "READ_BUFFER_BYTES", 4096)

        if not 1 <= port <= 65535:
            raise ValueError("TCP_PORT must be between 1 and 65535")
        if read_buffer_bytes <= 0:
            raise ValueError("READ_BUFFER_BYTES must be greater than zero")

        return cls(
            host=values.get("TCP_HOST", "0.0.0.0"),
            port=port,
            data_dir=Path(values.get("DATA_DIR", "/data")),
            read_buffer_bytes=read_buffer_bytes,
        )
```

Create `.gitignore`:

```gitignore
__pycache__/
*.py[cod]
.coverage
.pytest_cache/
data/
```

- [ ] **Step 5: Run the configuration tests and verify Green**

Run:

```bash
python3 -m unittest tests.test_config -v
```

Expected: four tests pass and the command exits with status 0.

- [ ] **Step 6: Commit the Green stage**

```bash
git add .gitignore data_record_server/__init__.py data_record_server/config.py
git commit -m "feat: implement TCP server configuration and pass tests"
```

## Task 2: Raw bytes and JSONL persistence

**Files:**
- Create: `tests/test_storage.py`
- Create: `data_record_server/storage.py`

- [ ] **Step 1: [Red Stage] Write persistence tests**

Create `tests/test_storage.py`:

```python
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from data_record_server.storage import Storage


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def read_events(self):
        event_path = self.data_dir / "events.jsonl"
        return [json.loads(line) for line in event_path.read_text().splitlines()]

    def test_preserves_exact_bytes_and_records_lifecycle_events(self):
        timestamps = iter(
            [
                datetime(2026, 8, 10, 8, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 10, 8, 0, 1, tzinfo=timezone.utc),
                datetime(2026, 8, 10, 8, 0, 2, tzinfo=timezone.utc),
                datetime(2026, 8, 10, 8, 0, 3, tzinfo=timezone.utc),
            ]
        )
        storage = Storage(self.data_dir, clock=lambda: next(timestamps))

        recorder = storage.open_connection(("203.0.113.10", 51822))
        recorder.record_received(b"\x7e\x00")
        recorder.record_received(b"\xff\x0d\x0a")
        recorder.close()

        connection_files = list((self.data_dir / "connections").glob("*.bin"))
        self.assertEqual(1, len(connection_files))
        self.assertEqual(b"\x7e\x00\xff\x0d\x0a", connection_files[0].read_bytes())

        events = self.read_events()
        self.assertEqual(
            ["connected", "received", "received", "disconnected"],
            [event["event"] for event in events],
        )
        self.assertEqual("203.0.113.10", events[0]["client_ip"])
        self.assertEqual(51822, events[0]["client_port"])
        self.assertEqual("7e00", events[1]["hex"])
        self.assertEqual(2, events[1]["bytes"])
        self.assertEqual("ff0d0a", events[2]["hex"])
        self.assertEqual(5, events[3]["total_bytes"])
        self.assertTrue(events[0]["file"].startswith("connections/"))

    def test_records_connection_errors_and_closes_only_once(self):
        timestamps = iter(
            [
                datetime(2026, 8, 10, 8, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 10, 8, 0, 1, tzinfo=timezone.utc),
                datetime(2026, 8, 10, 8, 0, 2, tzinfo=timezone.utc),
            ]
        )
        storage = Storage(self.data_dir, clock=lambda: next(timestamps))
        recorder = storage.open_connection(("203.0.113.11", 51823))

        recorder.record_error(ConnectionResetError("peer reset"))
        recorder.close()
        recorder.close()

        events = self.read_events()
        self.assertEqual(["connected", "error", "disconnected"], [
            event["event"] for event in events
        ])
        self.assertEqual("ConnectionResetError", events[1]["error_type"])
        self.assertEqual("peer reset", events[1]["error"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and verify the expected failure**

Run:

```bash
python3 -m unittest tests.test_storage -v
```

Expected: `ERROR` with `ModuleNotFoundError: No module named 'data_record_server.storage'`.

- [ ] **Step 3: Commit the Red stage**

```bash
git add tests/test_storage.py
git commit -m "test: init cases for raw TCP data persistence"
```

- [ ] **Step 4: [Green Stage] Implement binary and JSONL storage**

Create `data_record_server/storage.py`:

```python
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Tuple


Clock = Callable[[], datetime]
ClientAddress = Tuple[str, int]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class JsonlEventWriter:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()

    def write(self, event: Dict[str, object]) -> None:
        encoded = json.dumps(
            event, ensure_ascii=False, separators=(",", ":")
        )
        with self._lock:
            with self._path.open("a", encoding="utf-8") as output:
                output.write(encoded + "\n")


class ConnectionRecorder:
    def __init__(
        self,
        data_dir: Path,
        client_address: ClientAddress,
        events: JsonlEventWriter,
        clock: Clock,
    ):
        self._client_ip, self._client_port = client_address
        self._events = events
        self._clock = clock
        self._total_bytes = 0
        self._closed = False

        opened_at = self._clock()
        timestamp = opened_at.strftime("%Y%m%dT%H%M%S.%fZ")
        safe_ip = self._client_ip.replace(":", "_")
        suffix = uuid.uuid4().hex[:8]
        filename = f"{timestamp}_{safe_ip}_{self._client_port}_{suffix}.bin"
        self.relative_path = Path("connections") / filename
        self.path = data_dir / self.relative_path
        self._stream = self.path.open("xb")

        self._write_event(
            "connected",
            opened_at,
            client_ip=self._client_ip,
            client_port=self._client_port,
        )

    def _write_event(
        self, event_name: str, occurred_at: datetime, **fields: object
    ) -> None:
        event = {
            "event": event_name,
            "time": format_time(occurred_at),
            "file": self.relative_path.as_posix(),
        }
        event.update(fields)
        self._events.write(event)

    def record_received(self, data: bytes) -> None:
        self._stream.write(data)
        self._stream.flush()
        self._total_bytes += len(data)
        self._write_event(
            "received",
            self._clock(),
            bytes=len(data),
            hex=data.hex(),
        )

    def record_error(self, error: Exception) -> None:
        self._write_event(
            "error",
            self._clock(),
            error_type=type(error).__name__,
            error=str(error),
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stream.close()
        self._write_event(
            "disconnected",
            self._clock(),
            total_bytes=self._total_bytes,
        )


class Storage:
    def __init__(self, data_dir: Path, clock: Clock = utc_now):
        self._data_dir = data_dir
        self._clock = clock
        (self._data_dir / "connections").mkdir(parents=True, exist_ok=True)
        self._events = JsonlEventWriter(self._data_dir / "events.jsonl")

    def open_connection(self, client_address: ClientAddress) -> ConnectionRecorder:
        return ConnectionRecorder(
            self._data_dir, client_address, self._events, self._clock
        )
```

- [ ] **Step 5: Run the persistence tests and verify Green**

Run:

```bash
python3 -m unittest tests.test_storage -v
```

Expected: two tests pass and the command exits with status 0.

- [ ] **Step 6: Commit the Green stage**

```bash
git add data_record_server/storage.py
git commit -m "feat: implement raw TCP data persistence and pass tests"
```

## Task 3: Concurrent TCP receiver and graceful shutdown

**Files:**
- Create: `tests/test_server.py`
- Create: `data_record_server/server.py`
- Create: `data_record_server/__main__.py`

- [ ] **Step 1: [Red Stage] Write real-socket integration tests**

Create `tests/test_server.py`:

```python
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


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary_directory.name)
        config = Config(
            host="127.0.0.1",
            port=0,
            data_dir=self.data_dir,
            read_buffer_bytes=4,
        )
        self.server = create_server(config)
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        self.address = self.server.server_address

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def send_payload(self, payload):
        with socket.create_connection(self.address, timeout=2) as client:
            client.sendall(payload)
            client.shutdown(socket.SHUT_WR)
            self.assertEqual(b"", client.recv(1))

    def wait_until(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("condition was not satisfied before timeout")

    def read_events(self):
        event_path = self.data_dir / "events.jsonl"
        if not event_path.exists():
            return []
        return [json.loads(line) for line in event_path.read_text().splitlines()]

    def test_records_complete_stream_and_receive_metadata(self):
        self.send_payload(b"abcdef")
        self.wait_until(
            lambda: any(
                event["event"] == "disconnected" for event in self.read_events()
            )
        )

        connection_files = list((self.data_dir / "connections").glob("*.bin"))
        self.assertEqual(1, len(connection_files))
        self.assertEqual(b"abcdef", connection_files[0].read_bytes())

        received_events = [
            event for event in self.read_events() if event["event"] == "received"
        ]
        reconstructed = b"".join(
            bytes.fromhex(event["hex"]) for event in received_events
        )
        self.assertEqual(b"abcdef", reconstructed)
        self.assertEqual(6, sum(event["bytes"] for event in received_events))

    def test_accepts_a_second_client_after_the_first_disconnects(self):
        self.send_payload(b"first")
        self.send_payload(b"second")
        self.wait_until(
            lambda: len(list((self.data_dir / "connections").glob("*.bin"))) == 2
        )

        contents = sorted(
            path.read_bytes()
            for path in (self.data_dir / "connections").glob("*.bin")
        )
        self.assertEqual([b"first", b"second"], contents)

    @mock.patch("data_record_server.server.threading.Thread")
    def test_shutdown_handler_requests_shutdown_from_another_thread(
        self, thread_class
    ):
        handler = create_shutdown_handler(self.server)

        handler(15, None)

        thread_class.assert_called_once_with(
            target=self.server.shutdown, daemon=True
        )
        thread_class.return_value.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and verify the expected failure**

Run:

```bash
python3 -m unittest tests.test_server -v
```

Expected: `ERROR` with `ModuleNotFoundError: No module named 'data_record_server.server'`.

- [ ] **Step 3: Commit the Red stage**

```bash
git add tests/test_server.py
git commit -m "test: init cases for concurrent TCP receiving"
```

- [ ] **Step 4: [Green Stage] Implement the TCP server**

Create `data_record_server/server.py`:

```python
import logging
import signal
import socketserver
import threading
from types import FrameType
from typing import Callable, Optional

from data_record_server.config import Config
from data_record_server.storage import ConnectionRecorder, Storage


LOGGER = logging.getLogger(__name__)
SignalHandler = Callable[[int, Optional[FrameType]], None]


class CollectorRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        recorder: Optional[ConnectionRecorder] = None
        try:
            recorder = self.server.storage.open_connection(self.client_address)
            while True:
                data = self.request.recv(self.server.read_buffer_bytes)
                if not data:
                    break
                recorder.record_received(data)
        except Exception as error:
            if recorder is not None:
                recorder.record_error(error)
            LOGGER.exception("TCP connection failed for %s", self.client_address)
        finally:
            if recorder is not None:
                recorder.close()


class CollectorServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, storage: Storage, read_buffer_bytes: int):
        self.storage = storage
        self.read_buffer_bytes = read_buffer_bytes
        super().__init__(address, CollectorRequestHandler)


def create_server(config: Config) -> CollectorServer:
    storage = Storage(config.data_dir)
    return CollectorServer(
        (config.host, config.port), storage, config.read_buffer_bytes
    )


def create_shutdown_handler(server: CollectorServer) -> SignalHandler:
    def request_shutdown(
        signal_number: int, unused_frame: Optional[FrameType]
    ) -> None:
        LOGGER.info("received signal %s; stopping TCP server", signal_number)
        threading.Thread(target=server.shutdown, daemon=True).start()

    return request_shutdown


def run_server(config: Config) -> None:
    with create_server(config) as server:
        shutdown_handler = create_shutdown_handler(server)
        previous_sigint = signal.signal(signal.SIGINT, shutdown_handler)
        previous_sigterm = signal.signal(signal.SIGTERM, shutdown_handler)
        LOGGER.info("listening on %s:%s", *server.server_address)
        try:
            server.serve_forever()
        finally:
            signal.signal(signal.SIGINT, previous_sigint)
            signal.signal(signal.SIGTERM, previous_sigterm)
```

Create `data_record_server/__main__.py`:

```python
import logging

from data_record_server.config import Config
from data_record_server.server import run_server


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_server(Config.from_environ())


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run all Python tests and verify Green**

Run:

```bash
python3 -m unittest discover -s tests -v
```

Expected: all configuration, storage, and server tests pass.

- [ ] **Step 6: Commit the Green stage**

```bash
git add data_record_server/server.py data_record_server/__main__.py
git commit -m "feat: implement concurrent TCP receiver and pass tests"
```

## Task 4: Container deployment contract

**Files:**
- Create: `tests/test_deployment.py`
- Create: `Dockerfile`
- Create: `compose.yaml`
- Create: `.dockerignore`

- [ ] **Step 1: [Red Stage] Write deployment-file tests**

Create `tests/test_deployment.py`:

```python
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DeploymentTest(unittest.TestCase):
    def test_dockerfile_runs_the_python_module(self):
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()
        self.assertIn("FROM python:3.12-slim", dockerfile)
        self.assertIn('CMD ["python", "-m", "data_record_server"]', dockerfile)
        self.assertIn("EXPOSE 30050", dockerfile)

    def test_compose_publishes_and_persists_collector_data(self):
        compose = (PROJECT_ROOT / "compose.yaml").read_text()
        self.assertIn('${TCP_PORT:-30050}:${TCP_PORT:-30050}/tcp', compose)
        self.assertIn("./data:/data", compose)
        self.assertIn("restart: unless-stopped", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertIn("cap_drop:", compose)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the deployment tests and verify Red**

Run:

```bash
python3 -m unittest tests.test_deployment -v
```

Expected: both tests fail with `FileNotFoundError` because `Dockerfile` and `compose.yaml` do not exist.

- [ ] **Step 3: Commit the Red stage**

```bash
git add tests/test_deployment.py
git commit -m "test: init cases for Docker collector deployment"
```

- [ ] **Step 4: [Green Stage] Add the Docker image and Compose service**

Create `Dockerfile`:

```dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TCP_HOST=0.0.0.0 \
    TCP_PORT=30050 \
    DATA_DIR=/data \
    READ_BUFFER_BYTES=4096

WORKDIR /app
COPY data_record_server/ /app/data_record_server/

EXPOSE 30050

CMD ["python", "-m", "data_record_server"]
```

Create `compose.yaml`:

```yaml
services:
  collector:
    build:
      context: .
    environment:
      TCP_HOST: 0.0.0.0
      TCP_PORT: ${TCP_PORT:-30050}
      DATA_DIR: /data
      READ_BUFFER_BYTES: ${READ_BUFFER_BYTES:-4096}
    ports:
      - "${TCP_PORT:-30050}:${TCP_PORT:-30050}/tcp"
    volumes:
      - ./data:/data
    restart: unless-stopped
    init: true
    read_only: true
    tmpfs:
      - /tmp
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
```

Create `.dockerignore`:

```dockerignore
.git
__pycache__
*.py[cod]
data
docs
tests
```

- [ ] **Step 5: Validate tests and Compose syntax**

Run:

```bash
python3 -m unittest discover -s tests -v
docker compose config --quiet
docker build -t data-record-server:test .
```

Expected: all tests pass, Compose validation exits with status 0, and the image builds successfully.

- [ ] **Step 6: Commit the Green stage**

```bash
git add Dockerfile compose.yaml .dockerignore
git commit -m "feat: implement Docker collector deployment and pass tests"
```

## Task 5: Refactor under test protection

**Files:**
- Modify: `tests/test_server.py`
- Modify: `data_record_server/server.py`

- [ ] **Step 1: [Refactor Stage] Review duplication and type boundaries**

Run:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q data_record_server tests
```

Expected: all tests pass and compilation exits with status 0 before refactoring.

- [ ] **Step 2: Extract the repeated connection completion condition**

In `tests/test_server.py`, add this method below `read_events`:

```python
    def disconnected_count(self):
        return sum(
            event["event"] == "disconnected" for event in self.read_events()
        )
```

Replace the first test's `wait_until` call with:

```python
        self.wait_until(lambda: self.disconnected_count() == 1)
```

Replace the second test's `wait_until` call with:

```python
        self.wait_until(lambda: self.disconnected_count() == 2)
```

- [ ] **Step 3: Make the server's injected dependencies explicit**

In `data_record_server/server.py`, add this import:

```python
from typing import Tuple
```

Add this alias after `SignalHandler`:

```python
ServerAddress = Tuple[str, int]
```

Change the `CollectorServer.__init__` signature to:

```python
    def __init__(
        self,
        address: ServerAddress,
        storage: Storage,
        read_buffer_bytes: int,
    ):
```

- [ ] **Step 4: Verify the refactor remains Green**

Run:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q data_record_server tests
```

Expected: all tests still pass, compilation exits with status 0, and no runtime behavior changes.

- [ ] **Step 5: Commit the Refactor stage**

```bash
git add tests/test_server.py data_record_server/server.py
git commit -m "refactor: optimize TCP collector test and type boundaries"
```

## Task 6: Operator documentation and full verification

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write the operator guide**

Create `README.md`:

````markdown
# TCP Data Record Server

This service listens for unknown-protocol TCP data and preserves the exact byte stream from every client connection.

## Run tests locally

```bash
python3 -m unittest discover -s tests -v
```

## Start with Docker Compose

```bash
mkdir -p data
docker compose up -d --build
docker compose ps
```

The default listener is `0.0.0.0:30050`. Configure the App with server `8.134.210.73`, TCP port `30050`, and the desired reporting interval.

To use another port, set the same value for the Compose command and the App:

```bash
TCP_PORT=30100 docker compose up -d --build
```

## Inspect operation and captured data

```bash
docker compose logs --tail=100 collector
ss -ltnp '( sport = :30050 )'
tail -n 20 data/events.jsonl
find data/connections -type f -name '*.bin' -printf '%TY-%Tm-%Td %TH:%TM:%TS %s %p\n'
xxd "$(find data/connections -type f -name '*.bin' | head -n 1)"
```

Each `.bin` file is the authoritative raw TCP byte stream for one connection. `events.jsonl` provides timestamps, client addresses, read sizes, hexadecimal chunks, disconnects, and connection errors.

## Restart and stop

```bash
docker compose restart collector
docker compose down
```

`docker compose down` removes the container but does not remove the bind-mounted `data/` directory. Back up `data/` before deleting or moving it.

The first version deliberately sends no application-level ACK because the device protocol is unknown. TCP transport acknowledgements still operate normally.
````

- [ ] **Step 2: Run the complete verification suite**

Run:

```bash
git diff --check
python3 -m unittest discover -s tests -v
python3 -m compileall -q data_record_server tests
docker compose config --quiet
docker build -t data-record-server:test .
```

Expected: no whitespace errors, every test passes, Python compilation succeeds, Compose validation succeeds, and the image builds.

- [ ] **Step 3: Run a local container smoke test**

Start the service:

```bash
mkdir -p data
docker compose up -d --build
```

Send a binary sample with Python:

```bash
python3 -c "import socket; s=socket.create_connection(('127.0.0.1', 30050), timeout=3); s.sendall(bytes.fromhex('7e0100ff0d0a')); s.close()"
```

Verify persistence:

```bash
python3 -c "from pathlib import Path; files=list(Path('data/connections').glob('*.bin')); assert files; assert any(path.read_bytes() == bytes.fromhex('7e0100ff0d0a') for path in files)"
tail -n 5 data/events.jsonl
docker compose down
```

Expected: the assertion succeeds, JSONL contains `connected`, `received`, and `disconnected`, and Compose stops without deleting `data/`.

- [ ] **Step 4: Commit documentation**

```bash
git add README.md
git commit -m "docs: add TCP collector operation guide"
```

- [ ] **Step 5: Confirm final repository state**

Run:

```bash
git status --short
git log --oneline --decorate -12
```

Expected: `git status --short` is empty and history contains separate Red, Green, Refactor, and documentation commits.
