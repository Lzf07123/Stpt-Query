# 基础镜像可经 IMAGE_REGISTRY 切换镜像加速站（由 docker compose 传入 BASE_IMAGE）
ARG BASE_IMAGE=nginxinc/nginx-unprivileged:1.27-alpine
FROM ${BASE_IMAGE}

COPY --chown=101:101 nginx.conf /etc/nginx/nginx.conf
COPY --chown=101:101 static /usr/share/nginx/html
# 官方 nginx 镜像启动时会用 envsubst 渲染 /etc/nginx/templates/*.template，
# 把运行时的 API_TOKEN 注入反代 Authorization 头
COPY templates /etc/nginx/templates
# nginx 官方入口脚本目录：启动前按环境变量生成 brand-env.js（品牌文案覆盖）
COPY --chown=101:101 docker-entrypoint.d /docker-entrypoint.d
