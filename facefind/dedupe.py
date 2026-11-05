from __future__ import annotations

import hashlib

import cv2
import numpy as np


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def phash(img: np.ndarray, hash_size: int = 8, highfreq_factor: int = 4) -> int:
    """64 位感知哈希（DCT pHash）。"""
    size = hash_size * highfreq_factor
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (size, size), interpolation=cv2.INTER_AREA).astype(np.float32)
    dct = cv2.dct(small)
    low = dct[:hash_size, :hash_size].copy()
    med = float(np.median(low[1:].flatten()))  # 去掉 DC 项
    bits = (low > med).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bool(bit))
    return value


def hamming(a: int, b: int) -> int:
    return ((a ^ b) & 0xFFFFFFFFFFFFFFFF).bit_count()


def hamming_distance(a: int, b: int) -> int:
    return ((a ^ b) & 0xFFFFFFFFFFFFFFFF).bit_count()


def dhash(img: np.ndarray, hash_size: int = 8) -> int:
    """64 位差异哈希（横向梯度），作为 pHash 的辅助判据。"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (hash_size + 1, hash_size), interpolation=cv2.INTER_AREA)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bool(bit))
    return value


def center_crop(img: np.ndarray, ratio: float) -> np.ndarray:
    h, w = img.shape[:2]
    ch, cw = int(h * ratio), int(w * ratio)
    y0, x0 = (h - ch) // 2, (w - cw) // 2
    return img[y0:y0 + ch, x0:x0 + cw]


def phash_variants(img: np.ndarray, crop_tolerance: bool = True, base: int | None = None) -> list[int]:
    """全图 + 中心裁剪多尺度 pHash，用于容忍裁剪/边框差异。

    base 为已算好的全图 pHash，传入可省一次重复计算。
    """
    values = [phash(img) if base is None else base]
    if crop_tolerance:
        for ratio in (0.8, 0.6):
            values.append(phash(center_crop(img, ratio)))
    out: list[int] = []
    for v in values:
        if v not in out:
            out.append(v)
    return out


def to_signed64(v: int) -> int:
    v &= 0xFFFFFFFFFFFFFFFF
    return v - (1 << 64) if v >= (1 << 63) else v


def from_signed64(v: int) -> int:
    return v & 0xFFFFFFFFFFFFFFFF
