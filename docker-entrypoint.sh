#!/bin/bash
# 单容器双进程入口：uvicorn（仅容器内回环 8000）+ nginx（8080 对外唯一入口）。
# 必须以 bash 运行：`wait -n`（任一子进程退出即返回）是 bash 特性，dash 不支持。
#
# 进程模型：
# - 1 号进程为本脚本，`wait -n` 守护两个后台子进程——任一退出即以同码退出容器，
#   由编排（restart: unless-stopped / K8s restartPolicy）负责拉起，不做进程内重启。
# - SIGTERM/SIGINT（docker stop / K8s termination）转发给两个子进程：
#   uvicorn 优雅收尾在途查询（编排 stop_grace_period: 45s 兜底），
#   nginx 快速排空连接后退出。
#
# 运行前提（docker-compose.yml 已保证）：
# - /tmp 为 tmpfs：渲染输出落盘处（/tmp/nginx-conf.d）——read_only 容器里
#   /tmp 必然可写，不依赖 /etc/nginx/conf.d 的挂载形态
# - /var/log/nginx 为 tmpfs（apt nginx 默认日志路径）；pid/temp 路径已在
#   frontend/nginx.conf 重定向到 /tmp
set -e

# ---- 1. 品牌文案环境变量 → brand-env.js（沿用 nginx 官方镜像 entrypoint.d 约定）----
sh /docker-entrypoint.d/30-brand-env.sh

# ---- 2. envsubst 白名单渲染 nginx 站点模板 ----
# 模板仅引用 API_TOKEN / TRUSTED_PROXY_CIDR 两个变量（白名单防误替换应用配置）。
# 输出落到 /tmp/nginx-conf.d（nginx.conf 的 include 指向同一路径）：
# 曾因部署环境未给 /etc/nginx/conf.d 挂 tmpfs 而触发 Read-only file system，
# 移入 /tmp 后对任意容器只读策略与挂载形态均稳健。
mkdir -p /tmp/nginx-conf.d
envsubst '${API_TOKEN} ${TRUSTED_PROXY_CIDR}' \
  < /etc/nginx/templates/default.conf.template \
  > /tmp/nginx-conf.d/default.conf

# ---- 3. 双进程启动 ----
uvicorn app.main:app --host 127.0.0.1 --port 8000 &
UVICORN_PID=$!

nginx -g "daemon off;" &
NGINX_PID=$!

# ---- 4. 信号转发 + 守护等待 ----
term_handler() {
  kill -TERM "$UVICORN_PID" "$NGINX_PID" 2>/dev/null || true
}
trap term_handler TERM INT

# 任一子进程退出即容器退出（同码）：uvicorn 崩溃不能让 nginx 空转，
# nginx 崩溃也不能让 uvicorn 失去入口——统一交给编排重启拉起
set +e
wait -n "$UVICORN_PID" "$NGINX_PID"
STATUS=$?
set -e

term_handler
wait "$UVICORN_PID" "$NGINX_PID" 2>/dev/null || true
exit "$STATUS"
