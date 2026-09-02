#!/usr/bin/env sh
set -eu

BACKUP_DIR=${REDIS_BACKUP_DIR:-./backups/redis}
RETENTION_DAYS=${REDIS_BACKUP_RETENTION_DAYS:-14}

mkdir -p "$BACKUP_DIR"
docker compose exec -T redis redis-cli SAVE >/dev/null
timestamp="$(date +%Y%m%d-%H%M%S)"
docker compose cp "redis:/data/dump.rdb" "$BACKUP_DIR/redis-$timestamp.rdb"
gzip -f "$BACKUP_DIR/redis-$timestamp.rdb"
find "$BACKUP_DIR" -type f -name 'redis-*.rdb.gz' -mtime "+$RETENTION_DAYS" -delete
echo "Redis backup written to $BACKUP_DIR/redis-$timestamp.rdb.gz"
