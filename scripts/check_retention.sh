#!/usr/bin/env bash
# 只读检查：确认采集器实际保留期、设备归档的真实 mtime、以及到期时间。
# 用法：ssh root@<host> 'bash -s' < scripts/check_retention.sh
set -uo pipefail

DATA_DIR="${DATA_DIR:-/opt/manual_raking_trajectory/data_record_server/data}"
CONTAINER="${CONTAINER:-data_record_server-collector-1}"

# 设备归档与拒绝连接 .bin 使用各自的保留期；默认与 Config 一致。
if [ -z "${DEVICE_RETENTION_DAYS:-}" ]; then
  DEVICE_RETENTION_DAYS="$(
    docker inspect "$CONTAINER" \
      --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
      | sed -n 's/^DEVICE_RETENTION_DAYS=//p'
  )"
  DEVICE_RETENTION_DAYS="${DEVICE_RETENTION_DAYS:-365}"
fi

echo "=== 1. 当前时间 / 时区 ==="
date; cat /etc/timezone 2>/dev/null

echo
echo "=== 2. 容器生效的保留期 ==="
docker inspect "$CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
  | grep -E 'RETENTION_DAYS|IDLE_TIMEOUT_SECONDS'

echo
echo "=== 3. 容器启动时间（决定 startup 清理是否发生）==="
docker inspect "$CONTAINER" --format 'Started={{.State.StartedAt}} RestartCount={{.RestartCount}}' 2>/dev/null

echo
echo "=== 4. 各设备归档的真实 mtime ==="
find "$DATA_DIR/devices" -name '*.jsonl' -printf '%TY-%Tm-%Td %TH:%TM:%TS  %s  %p\n' 2>/dev/null | sort

echo
echo "=== 5. 每个设备归档的到期日（mtime + ${DEVICE_RETENTION_DAYS} 天）==="
find "$DATA_DIR/devices" -name '*.jsonl' -printf '%T@ %p\n' 2>/dev/null | sort -n | while read -r epoch path; do
  expire=$(date -d "@$(awk -v e="$epoch" -v d="$DEVICE_RETENTION_DAYS" 'BEGIN{printf "%.0f", e + d*86400}')" '+%Y-%m-%d %H:%M:%S %Z')
  printf '到期 %s  %s\n' "$expire" "$path"
done

echo
echo "=== 6. 历史清理记录（最近 10 次）==="
grep -E 'device_retention_cleanup|connection_retention_cleanup' "$DATA_DIR/events.jsonl" 2>/dev/null | tail -10

echo
echo "=== 7. 磁盘占用 ==="
du -sh "$DATA_DIR" 2>/dev/null; df -h "$DATA_DIR" 2>/dev/null
