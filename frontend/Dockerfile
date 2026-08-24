FROM nginx:1.27-alpine

COPY static /usr/share/nginx/html
# 官方 nginx 镜像启动时会用 envsubst 渲染 /etc/nginx/templates/*.template，
# 把运行时的 API_TOKEN 注入反代 Authorization 头
COPY templates /etc/nginx/templates
