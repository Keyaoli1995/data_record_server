#!/bin/sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
DATA_DIR=$PROJECT_ROOT/data
COLLECTOR_UID=${COLLECTOR_UID:-10001}
COLLECTOR_GID=${COLLECTOR_GID:-10001}

case "$DATA_DIR" in
  '' | / | . | ..)
    echo "Refusing unsafe data directory path: $DATA_DIR" >&2
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

if [ -L "$DATA_DIR" ]; then
  echo "Refusing data directory symlink: $DATA_DIR" >&2
  exit 1
fi

if [ -e "$DATA_DIR" ] && [ ! -d "$DATA_DIR" ]; then
  echo "Data path is not a directory: $DATA_DIR" >&2
  exit 1
fi

if [ "$(id -u)" = "$COLLECTOR_UID" ] && [ "$(id -g)" = "$COLLECTOR_GID" ]; then
  mkdir -p -- "$DATA_DIR"
  if [ -n "$(find -P "$DATA_DIR" \( ! -uid "$COLLECTOR_UID" -o ! -gid "$COLLECTOR_GID" \) -print -quit)" ]; then
    echo "Existing data files are not owned by ${COLLECTOR_UID}:${COLLECTOR_GID}." >&2
    echo "Run with sudo to repair ownership before starting the collector." >&2
    exit 1
  fi
  chmod 0750 -- "$DATA_DIR"
  exit 0
fi

if [ "$(id -u)" != "0" ]; then
  echo "Cannot set $DATA_DIR ownership to ${COLLECTOR_UID}:${COLLECTOR_GID} as a non-root user." >&2
  echo "Run with sudo or set COLLECTOR_UID and COLLECTOR_GID to your current identity." >&2
  exit 1
fi

mkdir -p -- "$DATA_DIR"
chmod 0750 -- "$DATA_DIR"
chown -R -h -- "$COLLECTOR_UID:$COLLECTOR_GID" "$DATA_DIR"
