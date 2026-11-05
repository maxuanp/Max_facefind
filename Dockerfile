# facefind Docker 镜像
# 默认 GPU 版（CUDA 12.4 运行时 + cuDNN 9）。纯 CPU 用：
#   docker build --build-arg BASE=ubuntu:22.04 --build-arg INSTALL_GPU=0 -t facefind:cpu .
ARG BASE=nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04
FROM ${BASE}

ARG INSTALL_GPU=1
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Asia/Shanghai

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv python3-dev \
        build-essential cython3 \
        libgl1 libglib2.0-0 ca-certificates wget unzip \
    && rm -rf /var/lib/apt/lists/*
# insightface 在 Linux 上通常需要从源码编译（Cython + C++ 扩展），所以必须带 build-essential

WORKDIR /app

COPY requirements.txt requirements-gpu.txt requirements-cpu.txt ./
RUN python3 -m pip install --upgrade pip \
    && python3 -m pip install -r requirements.txt \
    && if [ "$INSTALL_GPU" = "1" ]; then \
         python3 -m pip uninstall -y onnxruntime && \
         python3 -m pip install -r requirements-gpu.txt; \
       fi

# Playwright 自带 Chromium：容器内没有宿主机的 Chrome，必须装一份
RUN python3 -m pip install playwright \
    && python3 -m playwright install --with-deps chromium \
    && chmod -R a+rx /ms-playwright

COPY facefind ./facefind
COPY ui ./ui
COPY scripts ./scripts
COPY tools ./tools
COPY config.example.yaml ./
COPY docker/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh \
    && useradd -m app \
    && mkdir -p /app/data /app/models /app/faces /app/downloads /app/secrets \
    && chown -R app /app

USER app
EXPOSE 8765

ENTRYPOINT ["/entrypoint.sh"]
# 容器内只能监听 0.0.0.0；宿主机请只发布到 127.0.0.1（见 docker-compose.yml）
CMD ["python3", "-m", "facefind", "--config", "config.yaml", "ui", \
     "--host", "0.0.0.0", "--port", "8765", "--no-browser"]
