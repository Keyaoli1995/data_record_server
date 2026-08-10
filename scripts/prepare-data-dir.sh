#!/bin/sh

set -eu

DATA_DIR=${DATA_DIR-./data}
COLLECTOR_UID=${COLLECTOR_UID:-10001}
COLLECTOR_GID=${COLLECTOR_GID:-10001}

case "$DATA_DIR" in
  '')
    echo "DATA_DIR must not be empty" >&2
    exit 1
    ;;
esac

case "$COLLECTOR_UID" in
  '' | *[!0-9]*)
    echo "COLLECTOR_UID must be a non-negative decimal integer" >&2
    exit 1
    ;;
esac

case "$COLLECTOR_GID" in
  '' | *[!0-9]*)
    echo "COLLECTOR_GID must be a non-negative decimal integer" >&2
    exit 1
    ;;
esac

if [ "$(id -u)" = "$COLLECTOR_UID" ] && [ "$(id -g)" = "$COLLECTOR_GID" ]; then
  mkdir -p "$DATA_DIR"
  chmod 0750 "$DATA_DIR"
  exit 0
fi

if [ "$(id -u)" != "0" ]; then
  echo "Cannot set $DATA_DIR ownership to ${COLLECTOR_UID}:${COLLECTOR_GID} as a non-root user." >&2
  echo "Run with sudo or set COLLECTOR_UID and COLLECTOR_GID to your current identity." >&2
  exit 1
fi

install -d -m 0750 -o "$COLLECTOR_UID" -g "$COLLECTOR_GID" "$DATA_DIR"
