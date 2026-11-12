#!/usr/bin/env sh
# 可选外部 Redis 的快照备份（单体拓扑不再内置 redis 服务）。
# 前置：本机有 redis-cli，并已设置 REDIS_URL（例如 redis://user:pass@host:6379/0）。
#
# 凭据安全：密码不经 argv —— `redis-cli -u "$REDIS_URL"` 会把含密码的连接串
# 暴露在 `ps`/进程参数里。这里从 REDIS_URL 解析凭据后改用 REDISCLI_AUTH 环境变量
# 传密码（可用 REDIS_PASSWORD 显式覆盖），连接串中的 userinfo 会被剥除后再传给 -u。
#
# 快照语义：--rdb 本身会触发一次 BGSAVE 式快照，无需再显式 SAVE
# （SAVE 是同步阻塞全库写，属于双倍开销）。
set -eu

BACKUP_DIR=${REDIS_BACKUP_DIR:-./backups/redis}
RETENTION_DAYS=${REDIS_BACKUP_RETENTION_DAYS:-14}

if ! command -v redis-cli >/dev/null 2>&1; then
  echo "缺少 redis-cli：请先安装（macOS: brew install redis；Debian: apt-get install redis-tools）" >&2
  exit 1
fi
: "${REDIS_URL:?需要设置 REDIS_URL（外部 Redis 连接串，例如 redis://host:6379/0）}"

scheme=${REDIS_URL%%://*}
rest=${REDIS_URL#*://}
if [ "$scheme" = "$REDIS_URL" ]; then
  echo "REDIS_URL 缺少 scheme（需要 redis:// 或 rediss://）" >&2
  exit 1
fi

# 拆 userinfo（若有）：redis://[user[:pass]@]host[:port][/db]
case "$rest" in
  *@*)
    userinfo=${rest%%@*}
    hostpart=${rest#*@}
    ;;
  *)
    userinfo=
    hostpart=$rest
    ;;
esac

redis_user=
redis_password=${REDIS_PASSWORD:-}
if [ -n "$userinfo" ] && [ -z "$redis_password" ]; then
  case "$userinfo" in
    *:*)
      redis_user=${userinfo%%:*}
      redis_password=${userinfo#*:}
      ;;
    *)
      redis_user=$userinfo
      ;;
  esac
fi
[ "$redis_user" = "default" ] && redis_user=

# 剥除 userinfo 后的无凭据连接串（主机/端口/库保留）
clean_url="${scheme}://${hostpart}"

if [ -n "$redis_password" ]; then
  export REDISCLI_AUTH="$redis_password"
fi

if [ -n "$redis_user" ] && [ -n "$redis_password" ]; then
  set -- --user "$redis_user"
else
  set --
fi

mkdir -p "$BACKUP_DIR"
timestamp="$(date +%Y%m%d-%H%M%S)"
target="$BACKUP_DIR/redis-$timestamp.rdb"

redis-cli "$@" -u "$clean_url" --rdb "$target" >/dev/null
gzip -f "$target"
find "$BACKUP_DIR" -type f -name 'redis-*.rdb.gz' -mtime "+$RETENTION_DAYS" -delete
echo "Redis backup written to $target.gz"
