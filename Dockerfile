# 单容器镜像：nginx（唯一对外入口 8080）+ uvicorn（仅容器内回环 8000）双进程。
# 编排/渲染/查询代理（内嵌 ASGI）在 uvicorn 进程内运行；nginx 承担静态页托管、
# 反代 /api/v1/*、注入网关令牌与边缘安全头。
# 基础镜像可经 IMAGE_REGISTRY 切换镜像加速站（由 docker compose 传入 BASE_IMAGE）
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # 镜像默认固定令牌（与 compose/.env.example 同值）：Settings 类默认 true 会让
    # 裸 docker run 场景启动即生成随机令牌，固定 API_TOKEN 全部 401；
    # 多实例场景经 compose/环境显式传 AUTO_ROTATE_TOKEN=true 可覆盖
    AUTO_ROTATE_TOKEN=false

# 国内环境可设 APT_MIRROR 加速 nginx/LibreOffice 与 CJK 字体下载
ARG APT_MIRROR=http://deb.debian.org/debian
RUN if [ "${APT_MIRROR}" != "http://deb.debian.org/debian" ]; then \
        sed -i "s|http://deb.debian.org/debian|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends nginx gettext-base libreoffice-writer fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
# 国内环境可设 PIP_INDEX_URL 指向 PyPI 镜像加速构建期下载
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p -m 700 /var/log/edu-query \
    && mkdir -p -m 700 /var/lib/edu-query/notices \
    && mkdir -p -m 700 /var/lib/edu-query/calendar \
    && chown -R 10001:10001 /var/log/edu-query /var/lib/edu-query

COPY --chown=10001:10001 app ./app
# 节次时间表与学期基准（config/calendar.json）：非机密，随镜像发布
COPY --chown=10001:10001 config ./config

# ---- nginx 资产（frontend/ 目录 = nginx 资产目录，构建期并入单镜像）----
# 静态页（查询页/后台/PWA 资产）替代 apt nginx 的默认站点
COPY --chown=10001:10001 frontend/static /usr/share/nginx/html
COPY --chown=10001:10001 frontend/nginx.conf /etc/nginx/nginx.conf
# 模板由 entrypoint 用 envsubst 白名单渲染（注入 API_TOKEN / TRUSTED_PROXY_CIDR）
COPY --chown=10001:10001 frontend/templates /etc/nginx/templates
# 品牌文案环境变量 → brand-env.js（沿用 nginx 官方镜像的 entrypoint.d 约定）
COPY --chown=10001:10001 frontend/docker-entrypoint.d /docker-entrypoint.d
# 双进程入口：uvicorn（127.0.0.1:8000）+ nginx（8080 对外）
COPY --chown=10001:10001 docker-entrypoint.sh /app/docker-entrypoint.sh

EXPOSE 8080

USER 10001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=3)" || exit 1

# 1 号进程为 entrypoint（bash：wait -n 守双进程；任一退出即容器退出，交由编排重启）
CMD ["bash", "/app/docker-entrypoint.sh"]
