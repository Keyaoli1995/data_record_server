# TCP Idle Timeout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close each silent TCP client connection after 30 seconds by default, preserve its raw file, and record an explicit `idle_timeout` event before `disconnected`.

**Architecture:** `Config` owns a validated `idle_timeout_seconds` value. `CollectorServer` passes that value to each request handler, which uses socket read timeouts; `ConnectionRecorder` owns the new event schema. Docker/Compose expose the same default, while the README documents operational behavior.

**Tech Stack:** Python 3 standard library (`socket`, `socketserver`, `unittest`), Docker Compose, JSONL, Markdown.

---

## File structure

- `data_record_server/config.py`: parse and validate the idle-timeout environment variable.
- `data_record_server/server.py`: apply the timeout per accepted socket and handle only `socket.timeout` as an idle timeout.
- `data_record_server/storage.py`: append the structured `idle_timeout` JSONL event.
- `tests/test_config.py`: cover default, override, and invalid configuration values.
- `tests/test_server.py`: exercise real sockets for timeout closure and timeout reset after incoming data.
- `Dockerfile` and `compose.yaml`: publish a consistent `IDLE_TIMEOUT_SECONDS=30` runtime default.
- `tests/test_deployment.py`: make Docker and Compose runtime wiring part of the deployment contract.
- `README.md` and `DEPLOYMENT_GUIDE.md`: explain configuration and the `idle_timeout` event to operators.

### Task 1: Add validated idle-timeout configuration

**Files:**

- Modify: `tests/test_config.py`
- Modify: `data_record_server/config.py`

- [ ] **Step 1: Write failing configuration tests**

Add these assertions to `ConfigTest`:

```python
def test_uses_a_30_second_idle_timeout_by_default(self):
    with patch.dict(os.environ, {}, clear=True):
        config = Config.from_environ()

    self.assertEqual(30, config.idle_timeout_seconds)


def test_uses_idle_timeout_environment_override(self):
    config = Config.from_environ({"IDLE_TIMEOUT_SECONDS": "45"})

    self.assertEqual(45, config.idle_timeout_seconds)


def test_rejects_invalid_idle_timeout_values(self):
    for value in ("0", "-1", "not-a-number"):
        with self.subTest(value=value):
            with self.assertRaisesRegex(ValueError, "IDLE_TIMEOUT_SECONDS"):
                Config.from_environ({"IDLE_TIMEOUT_SECONDS": value})
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
python3 -m unittest \
  tests.test_config.ConfigTest.test_uses_a_30_second_idle_timeout_by_default \
  tests.test_config.ConfigTest.test_uses_idle_timeout_environment_override \
  tests.test_config.ConfigTest.test_rejects_invalid_idle_timeout_values -v
```

Expected: FAIL because `Config` has no `idle_timeout_seconds` attribute and does not reject invalid values.

- [ ] **Step 3: Commit the RED test**

```bash
git add tests/test_config.py
git commit -m "test: init cases for idle timeout configuration"
```

- [ ] **Step 4: Implement the minimum configuration change**

Extend the dataclass with a default so existing positional `Config(...)` calls stay compatible:

```python
@dataclass(frozen=True)
class Config:
    host: str
    port: int
    data_dir: Path
    read_buffer_bytes: int
    idle_timeout_seconds: int = 30
```

In `from_environ`, parse and validate the new value alongside `READ_BUFFER_BYTES`:

```python
idle_timeout_seconds = _parse_integer(
    environment.get("IDLE_TIMEOUT_SECONDS", "30"),
    "IDLE_TIMEOUT_SECONDS",
)

if idle_timeout_seconds <= 0:
    raise ValueError("IDLE_TIMEOUT_SECONDS must be positive")
```

Pass `idle_timeout_seconds=idle_timeout_seconds` when returning `Config`.

- [ ] **Step 5: Run the configuration suite and verify GREEN**

Run:

```bash
python3 -m unittest tests.test_config -v
```

Expected: all `ConfigTest` tests pass.

- [ ] **Step 6: Commit the configuration implementation**

```bash
git add data_record_server/config.py tests/test_config.py
git commit -m "feat: implement idle timeout configuration and pass tests"
```

### Task 2: Close silent client sockets and record the timeout

**Files:**

- Modify: `tests/test_server.py`
- Modify: `data_record_server/storage.py`
- Modify: `data_record_server/server.py`

- [ ] **Step 1: Write failing real-socket timeout tests**

Add a helper that creates a separate server with a short timeout for tests. It constructs `CollectorServer` directly so production `Config` can continue requiring whole-second environment values while the real-socket test remains fast:

```python
def _create_server_with_idle_timeout(self, idle_timeout_seconds):
    server = CollectorServer(
        server_address=("127.0.0.1", 0),
        storage=Storage(self._data_dir),
        read_buffer_bytes=4,
        idle_timeout_seconds=idle_timeout_seconds,
    )
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    return server, thread
```

Add a test that sends `b"abc"`, remains silent for a `0.15` second configured timeout, then verifies:

```python
self.assertEqual(
    ["connected", "received", "idle_timeout", "disconnected"],
    [event["event"] for event in events],
)
self.assertEqual(0.15, events[2]["idle_timeout_seconds"])
self.assertEqual(3, events[2]["total_bytes"])
self.assertEqual(b"abc", (self._data_dir / events[0]["file"]).read_bytes())
self.assertNotIn("error", [event["event"] for event in events])
```

Use existing `_wait_for` with a two-second deadline, and close the test server in a `finally` block using `shutdown()`, `server_close()`, and `thread.join(timeout=2)`.

Add a second test with a `0.15` second timeout that sends `b"a"`, sleeps `0.05` seconds, sends `b"b"`, sleeps `0.05` seconds, sends `b"c"`, then performs `shutdown(socket.SHUT_WR)`. Verify that its event list is `connected`, three `received` events, and `disconnected`, with no `idle_timeout`. This proves each received chunk resets the waiting period.

- [ ] **Step 2: Run the new server tests and verify RED**

Run:

```bash
python3 -m unittest \
  tests.test_server.CollectorServerTest.test_closes_a_silent_connection_after_its_idle_timeout \
  tests.test_server.CollectorServerTest.test_received_data_resets_the_idle_timeout -v
```

Expected: FAIL because a silent client remains blocked in `recv()` and no `idle_timeout` event exists.

- [ ] **Step 3: Commit the RED tests**

```bash
git add tests/test_server.py
git commit -m "test: init cases for TCP idle timeout"
```

- [ ] **Step 4: Implement structured timeout recording**

Add this method to `ConnectionRecorder` before `record_error`:

```python
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
```

- [ ] **Step 5: Apply the timeout to each accepted socket**

Add an optional defaulted argument to `CollectorServer.__init__` and retain backwards compatibility for existing direct constructors:

```python
def __init__(
    self,
    server_address: ServerAddress,
    storage: Storage,
    read_buffer_bytes: int,
    idle_timeout_seconds: float = 30,
) -> None:
    self.storage = storage
    self.read_buffer_bytes = read_buffer_bytes
    self.idle_timeout_seconds = idle_timeout_seconds
```

In `create_server`, pass `config.idle_timeout_seconds` as the fourth constructor argument.

In `CollectorRequestHandler.handle`, set the timeout after opening the recorder and catch it before the generic `Exception` branch:

```python
recorder = self.server.storage.open_connection(self.client_address)
self.request.settimeout(self.server.idle_timeout_seconds)
while True:
    data = self.request.recv(self.server.read_buffer_bytes)
    if not data:
        break
    recorder.record_received(data)
except socket.timeout:
    if recorder is not None:
        recorder.record_idle_timeout(self.server.idle_timeout_seconds)
```

Keep the existing generic exception handler and `finally: recorder.close()` unchanged. This makes the terminal timeout sequence `idle_timeout` followed by `disconnected`, without an `error` event.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run:

```bash
python3 -m unittest tests.test_server.CollectorServerTest -v
```

Expected: all `CollectorServerTest` tests pass, including the two new real-socket tests.

- [ ] **Step 7: Commit the timeout implementation**

```bash
git add data_record_server/server.py data_record_server/storage.py tests/test_server.py
git commit -m "feat: implement TCP idle timeout and pass tests"
```

### Task 3: Wire the default into deployment files and operator documentation

**Files:**

- Modify: `tests/test_deployment.py`
- Modify: `Dockerfile`
- Modify: `compose.yaml`
- Modify: `README.md`
- Modify: `DEPLOYMENT_GUIDE.md`

- [ ] **Step 1: Write the failing deployment contract test**

In `test_dockerfile_declares_the_collector_runtime_contract`, add this expected Docker environment variable:

```python
"IDLE_TIMEOUT_SECONDS": "30",
```

In `test_compose_file_declares_the_collector_runtime_contract`, add this expected Compose environment entry:

```python
"IDLE_TIMEOUT_SECONDS": "${IDLE_TIMEOUT_SECONDS:-30}",
```

- [ ] **Step 2: Run the deployment contract test and verify RED**

Run:

```bash
python3 -m unittest \
  tests.test_deployment.DeploymentFilesTest.test_dockerfile_declares_the_collector_runtime_contract \
  tests.test_deployment.DeploymentFilesTest.test_compose_file_declares_the_collector_runtime_contract -v
```

Expected: FAIL because neither runtime file declares `IDLE_TIMEOUT_SECONDS`.

- [ ] **Step 3: Commit the RED test**

```bash
git add tests/test_deployment.py
git commit -m "test: init cases for idle timeout deployment"
```

- [ ] **Step 4: Add the deployment variable**

Extend the existing `ENV` instruction in `Dockerfile` with:

```dockerfile
IDLE_TIMEOUT_SECONDS=30
```

Add this entry to `collector.environment` in `compose.yaml`:

```yaml
IDLE_TIMEOUT_SECONDS: ${IDLE_TIMEOUT_SECONDS:-30}
```

- [ ] **Step 5: Update operational documentation**

In `README.md` and `DEPLOYMENT_GUIDE.md`:

- State that the default deployment uses a 30-second idle timeout.
- Add `IDLE_TIMEOUT_SECONDS=30` to the complete custom `.env` example, while retaining the prior UID/GID/port fields.
- Explain that silent connections create `idle_timeout` followed by `disconnected`; normal connection closure does not create `idle_timeout`.
- Add the `idle_timeout` event and its `idle_timeout_seconds` and `total_bytes` fields to the events table.
- Explain that the configured value must be greater than the longest expected normal silent interval.

- [ ] **Step 6: Run focused deployment tests and verify GREEN**

Run:

```bash
python3 -m unittest tests.test_deployment -v
```

Expected: all deployment contract tests pass.

- [ ] **Step 7: Run final verification**

Run:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q data_record_server tests
git diff --check
```

Expected: all tests pass, compilation emits no errors, and `git diff --check` has no output.

- [ ] **Step 8: Commit deployment wiring and documentation**

```bash
git add Dockerfile compose.yaml README.md DEPLOYMENT_GUIDE.md tests/test_deployment.py
git commit -m "feat: implement idle timeout deployment and pass tests"
```
