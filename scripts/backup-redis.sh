#!/usr/bin/env sh
# 可选外部 Redis 的快照备份（单体拓扑不再内置 redis 服务）。
# 前置：本机有 redis-cli，并已设置 REDIS_URL（例如 redis://user:pass@host:6379/0）。
set -eu

BACKUP_DIR=${REDIS_BACKUP_DIR:-./backups/redis}
RETENTION_DAYS=${REDIS_BACKUP_RETENTION_DAYS:-14}

if ! command -v redis-cli >/dev/null 2>&1; then
  echo "缺少 redis-cli：请先安装（macOS: brew install redis；Debian: apt-get install redis-tools）" >&2
  exit 1
fi
: "${REDIS_URL:?需要设置 REDIS_URL（外部 Redis 连接串，例如 redis://host:6379/0）}"

mkdir -p "$BACKUP_DIR"
timestamp="$(date +%Y%m%d-%H%M%S)"
target="$BACKUP_DIR/redis-$timestamp.rdb"

redis-cli -u "$REDIS_URL" SAVE >/dev/null
redis-cli -u "$REDIS_URL" --rdb "$target" >/dev/null
gzip -f "$target"
find "$BACKUP_DIR" -type f -name 'redis-*.rdb.gz' -mtime "+$RETENTION_DAYS" -delete
echo "Redis backup written to $target.gz"
