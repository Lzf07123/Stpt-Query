# 基础镜像可经 IMAGE_REGISTRY 切换镜像加速站（由 docker compose 传入 BASE_IMAGE）
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

WORKDIR /app

COPY requirements.txt ./
# 国内环境可设 PIP_INDEX_URL 指向 PyPI 镜像加速构建期下载
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser

COPY app ./app
RUN chown -R 10001:10001 /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

USER 10001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
