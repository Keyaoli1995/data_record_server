import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


MAX_JSON_LINE_BYTES = 65536
DEFAULT_DEVICE_RETENTION_DAYS = 365
DEFAULT_CONNECTION_RETENTION_DAYS = 30


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
