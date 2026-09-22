# 单体应用镜像：编排 + 渲染 + 查询代理（内嵌 ASGI）同进程运行。
# 基础镜像可经 IMAGE_REGISTRY 切换镜像加速站（由 docker compose 传入 BASE_IMAGE）
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 国内环境可设 APT_MIRROR 加速 LibreOffice 与 CJK 字体下载
ARG APT_MIRROR=http://deb.debian.org/debian
RUN if [ "${APT_MIRROR}" != "http://deb.debian.org/debian" ]; then \
        sed -i "s|http://deb.debian.org/debian|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends libreoffice-writer fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
# 国内环境可设 PIP_INDEX_URL 指向 PyPI 镜像加速构建期下载
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p -m 700 /var/log/edu-query \
    && mkdir -p -m 700 /var/lib/edu-query/notices \
    && mkdir -p -m 700 /var/log/jwxt \
    && chown -R 10001:10001 /var/log/edu-query /var/lib/edu-query /var/log/jwxt

COPY app ./app
RUN chown -R 10001:10001 /app

EXPOSE 8000

USER 10001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
