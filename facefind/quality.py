from __future__ import annotations

import io
import math

import cv2
import numpy as np
from PIL import Image

from .models import ImageMetrics


def decode(data: bytes) -> np.ndarray | None:
    arr = np.frombuffer(data, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def sniff_ext(data: bytes) -> str:
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = (im.format or "").lower()
    except Exception:
        fmt = ""
    return {"jpeg": ".jpg", "png": ".png", "webp": ".webp", "gif": ".gif", "bmp": ".bmp"}.get(
        fmt, ".jpg"
    )


def compression_score(data: bytes) -> float:
    """用 JPEG 量化表估计压缩痕迹，越接近 1 越好。"""
    try:
        with Image.open(io.BytesIO(data)) as im:
            if (im.format or "").upper() != "JPEG":
                return 1.0
            tables = getattr(im, "quantization", None) or {}
            vals = [v for tbl in tables.values() for v in tbl]
            if not vals:
                return 0.8
            return max(0.0, 1.0 - min(1.0, (sum(vals) / len(vals)) / 60.0))
    except Exception:
        return 0.8


def sharpness_of(gray: np.ndarray) -> float:
    h, w = gray.shape[:2]
    scale = 1024.0 / max(h, w)
    if scale < 1.0:
        gray = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def measure(img: np.ndarray, data: bytes) -> ImageMetrics:
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return ImageMetrics(
        width=w,
        height=h,
        area=w * h,
        sharpness=sharpness_of(gray),
        compression=compression_score(data),
    )


def clarity(m: ImageMetrics, face_ratio: float | None, weights: dict[str, float]) -> float:
    """综合清晰度评分（0-100）：分辨率、锐度、人脸占比、压缩痕迹。"""
    components: dict[str, float] = {
        "resolution": min(1.0, math.sqrt(m.area / 1_000_000.0)),
        "sharpness": min(1.0, m.sharpness / 1500.0),
        "compression": min(1.0, m.compression),
    }
    if face_ratio is not None:
        components["face_ratio"] = min(1.0, face_ratio / 0.30)

    used = {k: weights.get(k, 0.0) for k in components}
    total_w = sum(used.values()) or 1.0
    score = sum(components[k] * used[k] for k in components) / total_w
    return round(score * 100.0, 2)
