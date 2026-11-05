#!/bin/sh
set -e

# 首次启动时用示例配置生成 config.yaml（挂载只读时请提前在宿主机准备好）
if [ ! -f /app/config.yaml ]; then
  if [ -f /app/config.example.yaml ]; then
    cp /app/config.example.yaml /app/config.yaml
    echo "[facefind] config.yaml not found, created from config.example.yaml"
    echo "[facefind] edit ./config.yaml on the host, then restart the container"
  else
    echo "[facefind] ERROR: /app/config.yaml missing" >&2
    exit 1
  fi
fi

exec "$@"
