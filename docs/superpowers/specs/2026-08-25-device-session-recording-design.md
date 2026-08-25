# Device-Session Recording Design

## Goal

Store all valid JSONL data from one `deviceId` in one raw `.bin` file while
that device continues recording, even if its public IP, source port, or TCP
connection changes. A device session ends only after 30 consecutive seconds
without a valid record from that device.

## Scope and protocol contract

The collector becomes protocol-aware for its input acceptance rule. A record is
accepted only when it is one complete UTF-8 JSON line whose top-level value is
an object with a non-empty string `deviceId`. The original bytes of every
accepted line, including its LF or CRLF line ending, are appended unchanged to
the session file.

Incomplete data remains buffered until a line ending arrives. Invalid UTF-8,
invalid JSON, a non-object JSON value, a missing/empty `deviceId`, and a later
line in the same TCP connection whose `deviceId` differs from that connection's
first accepted device ID are rejected and never written to a `.bin` file. The
connection stays open so a later valid line can be accepted; each rejection is
recorded in `events.jsonl` with a reason and the connection metadata.

This intentionally removes support for recording arbitrary or non-JSONL byte
streams. Existing X8-M1 traffic satisfies this contract.

## Session model

`deviceId` is the sole identity and grouping key; client IP and port remain
diagnostic metadata only. `DeviceSessionManager` owns a process-local mapping
from `deviceId` to one active session. It serializes session lookup/creation and
uses a per-session lock for appending accepted records, so two briefly
overlapping reconnects for the same device cannot create two files or corrupt
the byte stream.

On the first accepted record for a device, the manager creates:

```text
data/devices/device-<URL-escaped-deviceId>/
  <UTC-session-start>_<URL-escaped-deviceId>_<random>.bin
```

The `device-` prefix prevents path traversal even for unusual IDs; the original
`deviceId` is also kept unchanged in lifecycle events. An accepted record from
any subsequent TCP connection for that ID is appended to the existing open
file and refreshes its last-valid-record time. Receive ordering is the order in
which the server serializes accepted lines; the service does not reorder by
device timestamps.

The manager runs a bounded periodic expiry check. It closes a session and
removes it from the active mapping only when `now - last_valid_record_at >=
IDLE_TIMEOUT_SECONDS`. The default remains 30 seconds. A record received
before expiry joins the existing session; a record received after expiry starts
a new session file. Server shutdown closes all active session files and records
their terminal events before process exit.

## Events and observability

`events.jsonl` remains diagnostic metadata; it is not an additional source of
raw data. It records TCP connection open/close/error events with client IP and
port, and session events with `device_id` and `file`:

- `session_started` — first valid record creates a session file.
- `session_attached` — a TCP connection first supplies valid data for an
  existing or newly created device session.
- `record_rejected` — invalid record or changed `deviceId`, including a stable
  machine-readable reason.
- `session_idle_timeout` and `session_closed` — device-level 30-second expiry
  and final byte total.

Connection events must not claim a file before the connection has supplied a
valid device record. Documentation and tests will describe the new event
schema and the `data/devices/` layout, replacing the old one-file-per-TCP-
connection guarantee.

## Error handling and limits

The line parser has a configurable, finite maximum buffered line size. A
connection that exceeds it before emitting a line writes a rejection event,
then closes to prevent unbounded memory use. File open/write failures are
logged and handled like existing storage failures; they must not leave a stale
active-session entry. An invalid connection must not create empty device
directories or `.bin` files.

The initial change stays process-local and intentionally does not merge data
across collector restarts. A restart closes the active session files; a device
that reconnects afterwards begins a new session.

## Tests and acceptance criteria

Tests must cover, before implementation:

1. Two TCP connections with different IP/ports but the same `deviceId` and a
   gap below 30 seconds append their exact JSONL bytes to one session file.
2. A gap at or above the configured idle timeout closes the first session and
   creates a second file for the next valid record.
3. Two device IDs remain isolated in separate directories/files, including
   concurrent connections.
4. Invalid, incomplete-at-close, oversized, and missing-ID input creates no
   `.bin` file and emits a rejection event; a changed ID on one connection is
   likewise rejected.
5. Shutdown closes an active device session once and writes its terminal event.
6. Existing configuration validation, container/deployment checks, and the
   full unit-test suite continue to pass.

Success means a 4G reconnect during a continuous X8-M1 recording is represented
by one device-session `.bin` file, independent of the public IP used by each
TCP connection.
