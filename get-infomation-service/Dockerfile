# 基础镜像可用 IMAGE_REGISTRY 环境变量切换镜像站（由 docker compose 传入 BASE_IMAGE）
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
# 国内环境可设 PIP_INDEX_URL 指向 PyPI 镜像加速构建期下载
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /var/log/jwxt \
    && touch /var/log/jwxt/jwxt-service.log \
    && chown -R appuser:appuser /app /var/log/jwxt

COPY --chown=appuser:appuser main.py jwxt_core.py jwxt_state.py jwxt_http.py jwxt_redis.py .

ENV JWXT_HOST=0.0.0.0 \
    JWXT_PORT=8766

EXPOSE 8766

USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8766/health', timeout=3)" || exit 1

CMD ["python", "main.py"]
