import os
import math
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional


MAX_JSON_LINE_BYTES = 65536
DEFAULT_DEVICE_RETENTION_DAYS = 365
DEFAULT_CONNECTION_RETENTION_DAYS = 30


@dataclass(frozen=True)
class ReportingConfig:
    enabled: bool = False
    base_url: str = ""
    environment: str = ""
    authorization: str = field(default="", repr=False)
    update_interval: float = 10.0
    request_timeout: float = 5.0
    shutdown_reason: str = "SERVER_SHUTDOWN"

    @classmethod
    def from_environ(cls, env):
        enabled = _boolean(env.get("WEB_REPORTING_ENABLED", "false"))
        if not enabled:
            return cls()
        base = env.get("WEB_BASE_URL", "").rstrip("/")
        url = urlsplit(base)
        allow_http = _boolean(env.get("WEB_ALLOW_HTTP", "false"))
        if (url.scheme not in (("https", "http") if allow_http else ("https",))
                or not url.hostname or url.username or url.password
                or url.path or url.query or url.fragment):
            raise ValueError("WEB_BASE_URL must be an HTTPS origin (HTTP requires WEB_ALLOW_HTTP)")
        for key in ("WEB_ENVIRONMENT", "WEB_AUTHORIZATION"):
            value = env.get(key, "")
            if not value.strip() or any(ord(c) < 32 or ord(c) > 126 for c in value):
                raise ValueError(key + " must be a nonempty ASCII header value")
        limits = []
        for key, default in (("WEB_UPDATE_INTERVAL_SECONDS", "10"),
                             ("WEB_REQUEST_TIMEOUT_SECONDS", "5")):
            try:
                value = float(env.get(key, default))
            except ValueError:
                raise ValueError(key + " must be finite and positive") from None
            if not math.isfinite(value) or value <= 0 or value > 3600:
                raise ValueError(key + " must be in (0, 3600]")
            limits.append(value)
        reason = env.get("WEB_SHUTDOWN_REASON", "SERVER_SHUTDOWN")
        if not reason or not all(c.isascii() and (c.isupper() or c == "_") for c in reason):
            raise ValueError("WEB_SHUTDOWN_REASON must be an uppercase enum")
        return cls(True, base, env["WEB_ENVIRONMENT"], env["WEB_AUTHORIZATION"],
                   *limits, reason)


def _boolean(value):
    if value.lower() not in ("true", "false"):
        raise ValueError("reporting boolean settings must be true or false")
    return value.lower() == "true"


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    data_dir: Path
    read_buffer_bytes: int
    idle_timeout_seconds: int = 30
    max_json_line_bytes: int = MAX_JSON_LINE_BYTES
    device_retention_days: int = DEFAULT_DEVICE_RETENTION_DAYS
    connection_retention_days: int = DEFAULT_CONNECTION_RETENTION_DAYS

    reporting: ReportingConfig = field(default_factory=ReportingConfig)

    @classmethod
    def from_environ(
        cls, environment: Optional[Mapping[str, str]] = None
    ) -> "Config":
        environment = os.environ if environment is None else environment
        port = _parse_integer(environment.get("TCP_PORT", "30050"), "TCP_PORT")
        read_buffer_bytes = _parse_integer(
            environment.get("READ_BUFFER_BYTES", "4096"), "READ_BUFFER_BYTES"
        )
        idle_timeout_seconds = _parse_integer(
            environment.get("IDLE_TIMEOUT_SECONDS", "30"), "IDLE_TIMEOUT_SECONDS"
        )
        max_json_line_bytes = _parse_integer(
            environment.get("MAX_JSON_LINE_BYTES", str(MAX_JSON_LINE_BYTES)),
            "MAX_JSON_LINE_BYTES",
        )
        device_retention_days = _parse_integer(
            environment.get(
                "DEVICE_RETENTION_DAYS", str(DEFAULT_DEVICE_RETENTION_DAYS)
            ),
            "DEVICE_RETENTION_DAYS",
        )
        connection_retention_days = _parse_integer(
            environment.get(
                "RETENTION_DAYS", str(DEFAULT_CONNECTION_RETENTION_DAYS)
            ),
            "RETENTION_DAYS",
        )

        if not 1 <= port <= 65535:
            raise ValueError("TCP_PORT must be between 1 and 65535")
        if read_buffer_bytes <= 0:
            raise ValueError("READ_BUFFER_BYTES must be positive")
        if idle_timeout_seconds <= 0:
            raise ValueError("IDLE_TIMEOUT_SECONDS must be positive")
        if max_json_line_bytes <= 0:
            raise ValueError("MAX_JSON_LINE_BYTES must be positive")
        if device_retention_days <= 0:
            raise ValueError("DEVICE_RETENTION_DAYS must be positive")
        if connection_retention_days <= 0:
            raise ValueError("RETENTION_DAYS must be positive")

        return cls(
            reporting=ReportingConfig.from_environ(environment),
            host=environment.get("TCP_HOST", "0.0.0.0"),
            port=port,
            data_dir=Path(environment.get("DATA_DIR", "/data")),
            read_buffer_bytes=read_buffer_bytes,
            idle_timeout_seconds=idle_timeout_seconds,
            max_json_line_bytes=max_json_line_bytes,
            device_retention_days=device_retention_days,
            connection_retention_days=connection_retention_days,
        )


def _parse_integer(value: str, variable_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{variable_name} must be an integer") from error
