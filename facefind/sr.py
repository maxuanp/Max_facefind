"""超分辨率引擎：多模型、分块推理、显存自适应、可选面部修复。

权重统一放在项目内持久目录 models/sr/，不写入用户临时目录。
推理依赖 torch + spandrel（spandrel 已在本机可用）；缺失时给出明确提示，
不影响 facefind 主链路。
"""
from __future__ import annotations

import gc
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

GPU_LOCK = threading.Lock()
_FACE_TEMPLATE = np.array(
    [
        [192.98138, 239.94708],
        [318.90277, 240.1936],
        [256.63416, 314.01935],
        [201.26117, 371.41043],
        [313.08905, 371.15118],
    ],
    dtype=np.float32,
)

MIN_TILE = 96
HARD_MAX_SIDE = 8192  # 未显式设置长边上限时的内存保护


@dataclass(frozen=True)
class SrModel:
    id: str
    label: str
    filename: str
    short: str
    family: str
    tier: str
    tile: int
    note: str
    source: str
    max_side: int = 0
    optional: bool = False


MODELS: tuple[SrModel, ...] = (
    SrModel(
        id="realesrgan_x4plus",
        label="Real-ESRGAN x4plus（通用照片）",
        filename="realesrgan_x4plus.pth",
        short="realesrgan",
        family="ESRGAN",
        tier="balanced",
        tile=512,
        note="通用照片默认基线；速度快、纹理自然，脸部仍需配合面部修复。",
        source="https://github.com/xinntao/Real-ESRGAN/releases/tag/v0.1.0",
    ),
    SrModel(
        id="realesrgan_anime",
        label="Real-ESRGAN anime 6B（插画/动漫）",
        filename="realesrgan_x4plus_anime_6b.pth",
        short="anime",
        family="ESRGAN",
        tier="fast",
        tile=512,
        note="偏插画/二次元；用于照片会过度平滑，真人照片不建议。",
        source="https://github.com/xinntao/Real-ESRGAN/releases/tag/v0.2.2.4",
    ),
    SrModel(
        id="swinir_l_x4",
        label="SwinIR-L x4 GAN（Transformer）",
        filename="swinir_l_x4_gan.pth",
        short="swinir",
        family="Transformer",
        tier="quality",
        tile=256,
        note="细节与文字边缘更锐，速度明显慢于 Real-ESRGAN；显存占用较低。",
        source="https://github.com/JingyunLiang/SwinIR/releases/tag/v0.0",
    ),
    SrModel(
        id="hat_real_gan_x4",
        label="HAT Real-GAN x4（Transformer 大模型）",
        filename="hat_real_gan_x4.pth",
        short="hat",
        family="Transformer",
        tier="max_quality",
        tile=256,
        max_side=2048,
        note="细节最强，但显存随面积增长很快；12GB 显卡实测长边超过约 2048 会严重换页，"
             "因此本模型强制限制长边 ≤2048，单图耗时仍可达数秒至分钟级，适合精选图。",
        source="https://huggingface.co/anchuang/Real_HAT_GAN_SRx4_sharper",
    ),
    SrModel(
        id="supir_f4",
        label="SUPIR（生成式，可选档）",
        filename="",
        short="supir",
        family="Diffusion",
        tier="optional",
        tile=512,
        note="画质上限最高，但需额外约 12–16GB 权重与 diffusers/torch 依赖，"
             "显存与耗时远高于本机 12GB 可承受的批量场景，默认未安装。",
        source="https://github.com/Fanghua-Yu/SUPIR",
        optional=True,
    ),
)

FACE_MODELS = {
    "none": {"label": "关闭", "filename": ""},
    "gfpgan": {
        "label": "GFPGAN v1.4",
        "filename": "gfpgan_v1.4.pth",
        "source": "https://github.com/TencentARC/GFPGAN/releases/tag/v1.3.4",
    },
}


def _torch():
    try:
        import torch
    except Exception as exc:  # pragma: no cover - 依赖缺失
        raise RuntimeError(
            "未安装 torch，无法使用超分。请先 `pip install torch`（CUDA 版）后重试。"
        ) from exc
    return torch


def _spandrel():
    try:
        from spandrel import ModelLoader
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "未安装 spandrel，无法加载超分模型。请执行 `pip install spandrel` 后重试。"
        ) from exc
    return ModelLoader()


def _size_mb(path: Path) -> float:
    try:
        return round(path.stat().st_size / 1024 / 1024, 1)
    except OSError:
        return 0.0


def read_image(path: Path) -> np.ndarray | None:
    """读取图片并按 EXIF 方向转正（BGR）。超分结果不携带 EXIF，必须预先转正。"""
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            arr = np.asarray(im)
        return np.ascontiguousarray(arr[:, :, ::-1])
    except Exception:
        try:
            data = np.fromfile(str(path), dtype=np.uint8)
            return cv2.imdecode(data, cv2.IMREAD_COLOR)
        except Exception:
            return None


def write_image(path: Path, img_bgr: np.ndarray, quality: int = 95) -> None:
    """原子写盘，支持中文路径。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".jpg", img_bgr, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise RuntimeError("JPEG 编码失败")
    tmp = path.with_name(f".{path.stem}.{threading.get_ident()}.tmp.jpg")
    tmp.write_bytes(buf.tobytes())
    tmp.replace(path)


def _scale_face(face, sx: float, sy: float):
    """把检测结果按预缩放比例映射，保持 kps/bbox 接口不变。"""
    from types import SimpleNamespace

    bbox = np.asarray(getattr(face, "bbox"), dtype=np.float32)
    kps = getattr(face, "kps", None)
    return SimpleNamespace(
        bbox=np.array([bbox[0] * sx, bbox[1] * sy, bbox[2] * sx, bbox[3] * sy], dtype=np.float32),
        kps=None if kps is None else np.asarray(kps, dtype=np.float32) * np.array([sx, sy], np.float32),
        det_score=getattr(face, "det_score", 1.0),
    )


def _align_face(img_bgr: np.ndarray, kps: np.ndarray | None, bbox) -> tuple[np.ndarray, np.ndarray]:
    """按 5 点关键点把脸部对齐到 512×512；关键点缺失时用人脸框方形裁剪兜底。"""
    if kps is not None and np.asarray(kps).shape == (5, 2):
        m, _ = cv2.estimateAffinePartial2D(
            np.asarray(kps, dtype=np.float32), _FACE_TEMPLATE, method=cv2.LMEDS
        )
        if m is not None and np.isfinite(m).all():
            crop = cv2.warpAffine(
                img_bgr, m, (512, 512), flags=cv2.INTER_LANCZOS4,
                borderMode=cv2.BORDER_REFLECT_101,
            )
            return crop, m
    x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    side = max(x2 - x1, y2 - y1) * 1.5
    scale = 512.0 / max(1.0, side)
    m = np.array([[scale, 0, 256.0 - cx * scale], [0, scale, 256.0 - cy * scale]],
                 dtype=np.float32)
    crop = cv2.warpAffine(
        img_bgr, m, (512, 512), flags=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_REFLECT_101,
    )
    return crop, m


# 面部贴回参数：椭圆遮罩外扩倍数、羽化 sigma 占人脸框短边比例、Lab 色彩匹配强度。
_PASTE_SCALE = 1.30          # 椭圆半径 = 人脸框半宽/半高 × 该倍数（贴回区域≈1.3 倍外接框）
_FEATHER_RATIO = 0.22        # 羽化 sigma = 人脸框短边 × 22%（用户建议 15%–25%）
_FEATHER_MIN = 6.0           # 羽化 sigma 下限（像素）
_COLOR_STRENGTH = 1.0        # Lab 均值/方差匹配强度


def _ellipse_mask(shape, cx: float, cy: float, rx: float, ry: float) -> np.ndarray:
    """生成椭圆实心遮罩（0/255），避免方形 mask 留下直角边。"""
    mask = np.zeros(shape[:2], np.uint8)
    cv2.ellipse(mask, (int(round(cx)), int(round(cy))),
                (max(1, int(round(rx))), max(1, int(round(ry)))), 0, 0, 360, 255, -1)
    return mask


def _lab_match(patch_bgr: np.ndarray, ref_pixels: np.ndarray, sel: np.ndarray,
               strength: float) -> np.ndarray:
    """把补丁的 Lab 均值/方差朝周围参考区域靠拢，消除色/亮度差造成的可见矩形。

    只做线性（均值+方差）迁移，不模糊、不改高频，因此不影响面部锐度；
    迁移幅度做限幅，避免人脸肤色被周围背景/头发带偏。
    """
    if strength <= 0 or ref_pixels.size < 300 or int(sel.sum()) < 300:
        return patch_bgr
    lab = cv2.cvtColor(patch_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    ref = cv2.cvtColor(ref_pixels.reshape(-1, 1, 3).astype(np.uint8), cv2.COLOR_BGR2LAB)
    ref = ref.reshape(-1, 3).astype(np.float32)
    src = lab[sel]
    mean_s, std_s = src.mean(0), np.maximum(src.std(0), 1.0)
    mean_r, std_r = ref.mean(0), np.maximum(ref.std(0), 1.0)

    # 均值偏移限幅：L 最多 ±10，a/b 最多 ±6，避免整张脸变色
    shift = np.clip((mean_r - mean_s) * strength, [-10.0, -6.0, -6.0], [10.0, 6.0, 6.0])
    ratio = np.clip(std_r / std_s, 0.75, 1.33)
    ratio = 1.0 + (ratio - 1.0) * strength
    lab[sel] = (src - mean_s) * ratio + mean_s + shift
    out = cv2.cvtColor(lab.clip(0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)
    return out


def _paste_face(out_bgr: np.ndarray, restored_bgr: np.ndarray, m: np.ndarray,
                bbox, strength: float, *, scale: float | None = None,
                feather: float | None = None, color: float | None = None,
                mode: str = "poisson") -> None:
    """把 512 修复结果逆变换贴回。

    与旧实现的差别：
    1) 遮罩由「512 方形 erode+模糊」改为以人脸框为中心的椭圆，横纵比跟随人脸框，
       不再外扩到整个 512 模板，因此不会覆盖到肩部/背景形成大矩形；
    2) 羽化半径按人脸框短边自适应（默认 22%，上下限保护），不再固定 sigma=14；
    3) 贴回前做 Lab 均值/方差匹配，消除色调/亮度差造成的可见色块；
    4) 默认用泊松（seamlessClone）做梯度域融合，低频按边界对齐后不再有台阶；
       泊松不可用时自动退回 alpha 羽化融合；
    5) 只在人脸附近的 ROI 内做 warp/融合，顺带减少大图上的无效计算。
    """
    h, w = out_bgr.shape[:2]
    k = float(_PASTE_SCALE if scale is None else scale)
    fr = float(_FEATHER_RATIO if feather is None else feather)
    cs = float(_COLOR_STRENGTH if color is None else color)

    if bbox is not None and np.asarray(bbox).size >= 4:
        x1, y1, x2, y2 = [float(v) for v in np.asarray(bbox).reshape(-1)[:4]]
    else:  # 兜底：用 512 模板四角逆变换的外接框
        corners = np.array([[0, 0], [511, 0], [0, 511], [511, 511]], np.float32)
        inv0 = cv2.invertAffineTransform(m)
        pts = cv2.transform(corners[None], inv0)[0]
        x1, y1 = pts.min(0)
        x2, y2 = pts.max(0)
    bw, bh = max(2.0, x2 - x1), max(2.0, y2 - y1)
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    rx, ry = bw / 2.0 * k, bh / 2.0 * k
    short = min(bw, bh)
    sigma = float(np.clip(fr * short, _FEATHER_MIN, 0.4 * short))

    pad = int(np.ceil(3.0 * sigma)) + 2
    ox, oy = rx * 1.35, ry * 1.35  # 参考环范围（略大于贴回椭圆）
    X0 = int(max(0, np.floor(cx - ox - pad)))
    X1 = int(min(w, np.ceil(cx + ox + pad) + 1))
    Y0 = int(max(0, np.floor(cy - oy - pad)))
    Y1 = int(min(h, np.ceil(cy + oy + pad) + 1))
    if X1 <= X0 or Y1 <= Y0:
        return

    inv = cv2.invertAffineTransform(m).astype(np.float32)
    inv[0, 2] -= X0
    inv[1, 2] -= Y0
    patch = cv2.warpAffine(restored_bgr, inv, (X1 - X0, Y1 - Y0), flags=cv2.INTER_LANCZOS4,
                           borderMode=cv2.BORDER_REFLECT_101)
    roi = out_bgr[Y0:Y1, X0:X1]

    lcx, lcy = cx - X0, cy - Y0
    mask = _ellipse_mask(roi.shape, lcx, lcy, rx, ry)
    alpha = cv2.GaussianBlur(mask.astype(np.float32) / 255.0, (0, 0), sigma)
    alpha *= max(0.0, min(1.0, float(strength)))
    if alpha.max() <= 0.0:
        return

    if cs > 0:
        ring = _ellipse_mask(roi.shape, lcx, lcy, rx * 1.30, ry * 1.30)
        ring = cv2.subtract(ring, mask)
        patch = _lab_match(patch, roi[ring > 0], mask > 0, cs)

    if mode == "poisson":
        try:
            center = (int(round(lcx)), int(round(lcy)))
            blended = cv2.seamlessClone(patch, roi.copy(), mask, center, cv2.NORMAL_CLONE)
            # 泊松只改低频；再用 alpha 收边，保证椭圆外完全不受影响
            out_bgr[Y0:Y1, X0:X1] = (roi.astype(np.float32) * (1.0 - alpha[..., None])
                                     + blended.astype(np.float32) * alpha[..., None]
                                     ).round().clip(0, 255).astype(np.uint8)
            return
        except Exception:
            pass  # 失败时退回 alpha 融合

    out_bgr[Y0:Y1, X0:X1] = (roi.astype(np.float32) * (1.0 - alpha[..., None])
                             + patch.astype(np.float32) * alpha[..., None]
                             ).round().clip(0, 255).astype(np.uint8)


class SuperResolver:
    """单例式超分引擎；一次只保留一个 SR 模型在显存中。"""

    def __init__(self, weights_dir: Path, device: str = "cuda"):
        self.weights_dir = Path(weights_dir)
        self.device_pref = device
        self._model_id = ""
        self._desc = None
        self._face_desc = None
        self._face_loaded = False

    # ---------- 状态 ----------
    def gpu_info(self) -> dict:
        try:
            torch = _torch()
            if not torch.cuda.is_available():
                return {"available": False, "device": "", "total_gb": 0, "free_gb": 0}
            free, total = torch.cuda.mem_get_info()
            return {
                "available": True,
                "device": torch.cuda.get_device_name(0),
                "total_gb": round(total / 1024 ** 3, 1),
                "free_gb": round(free / 1024 ** 3, 1),
            }
        except Exception as exc:
            return {"available": False, "device": "", "total_gb": 0, "free_gb": 0, "error": str(exc)}

    def models(self) -> list[dict]:
        out = []
        for spec in MODELS:
            path = self.weights_dir / spec.filename if spec.filename else None
            exists = bool(path and path.exists() and path.stat().st_size > 1_000_000)
            out.append({
                "id": spec.id, "label": spec.label, "family": spec.family,
                "tier": spec.tier, "tile": spec.tile, "note": spec.note,
                "source": spec.source, "optional": spec.optional,
                "max_side": spec.max_side,
                "installed": exists, "size_mb": _size_mb(path) if path and exists else 0,
                "path": str(path) if path else "",
            })
        return out

    def available(self, model_id: str) -> bool:
        spec = self.spec(model_id)
        if spec is None or not spec.filename:
            return False
        path = self.weights_dir / spec.filename
        return path.exists() and path.stat().st_size > 1_000_000

    def spec(self, model_id: str) -> SrModel | None:
        return next((m for m in MODELS if m.id == model_id), None)

    # ---------- 模型加载 ----------
    def _device(self):
        torch = _torch()
        if self.device_pref == "cpu":
            return torch.device("cpu")
        if not torch.cuda.is_available():
            return torch.device("cpu")
        return torch.device("cuda")

    def _load_sr(self, model_id: str):
        if self._model_id == model_id and self._desc is not None:
            return self._desc
        spec = self.spec(model_id)
        if spec is None:
            raise RuntimeError(f"未知超分模型：{model_id}")
        if not spec.filename or not self.available(model_id):
            raise RuntimeError(
                f"模型 {spec.label} 未安装或权重缺失（{self.weights_dir / (spec.filename or '')}）。"
            )
        torch = _torch()
        device = self._device()
        if device.type == "cpu":
            raise RuntimeError("当前未检测到可用 CUDA，超分模型在 CPU 上不可用（请检查 torch CUDA 安装）")
        self._unload_sr()
        desc = _spandrel().load_from_file(str(self.weights_dir / spec.filename))
        desc.model.to(device).eval()
        if bool(getattr(desc, "supports_half", False)):
            desc.model.half()
        self._desc = desc
        self._model_id = model_id
        return desc

    def _unload_sr(self) -> None:
        self._desc = None
        self._model_id = ""
        try:
            torch = _torch()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        gc.collect()

    def _load_face(self, model_id: str):
        if model_id == "none":
            return None
        info = FACE_MODELS.get(model_id)
        if not info or not info.get("filename"):
            raise RuntimeError(f"未知面部修复模型：{model_id}")
        path = self.weights_dir / info["filename"]
        if not path.exists() or path.stat().st_size < 1_000_000:
            raise RuntimeError(f"面部修复模型未安装：{path}")
        if self._face_desc is not None:
            return self._face_desc
        torch = _torch()
        device = self._device()
        if device.type == "cpu":
            raise RuntimeError("当前未检测到可用 CUDA，面部修复在 CPU 上不可用")
        desc = _spandrel().load_from_file(str(path))
        desc.model.to(device).eval()
        self._face_desc = desc
        return desc

    def _auto_tile(self, tile: int, spec: SrModel) -> int:
        hw = 0
        try:
            torch = _torch()
            if torch.cuda.is_available():
                free, _ = torch.cuda.mem_get_info()
                hw = round(free / 1024 ** 3, 1)
        except Exception:
            hw = 0
        base = int(tile or spec.tile)
        if hw:
            if hw < 3:
                base = min(base, 128)
            elif hw < 6:
                base = min(base, 256)
            elif hw < 9:
                base = min(base, 384)
        return max(MIN_TILE, base - base % 8)

    # ---------- 推理 ----------
    def _tiled_sr(self, img_rgb: np.ndarray, desc, scale: int, tile: int,
                  on_tile: Callable[[int, int], None] | None = None) -> np.ndarray:
        torch = _torch()
        device = self._device()
        half = bool(getattr(desc, "supports_half", False))
        dtype = torch.float16 if half else torch.float32
        h, w = img_rgb.shape[:2]
        out = np.zeros((h * scale, w * scale, 3), np.float32)
        overlap = max(8, (tile // 8) - (tile // 8) % 8)
        ys = list(range(0, h, tile))
        xs = list(range(0, w, tile))
        total = len(ys) * len(xs)
        done = 0
        for y0 in ys:
            for x0 in xs:
                y1, x1 = min(h, y0 + tile), min(w, x0 + tile)
                py0, px0 = max(0, y0 - overlap), max(0, x0 - overlap)
                py1, px1 = min(h, y1 + overlap), min(w, x1 + overlap)
                patch = np.ascontiguousarray(img_rgb[py0:py1, px0:px1].transpose(2, 0, 1)[None])
                t = torch.from_numpy(patch).to(device, dtype)
                with torch.inference_mode():
                    res = desc(t)
                    if isinstance(res, (tuple, list)):
                        res = res[0]
                    res = res.float().clamp(0, 1)[0].permute(1, 2, 0).cpu().numpy()
                sy0, sy1 = (y0 - py0) * scale, (y1 - py0) * scale
                sx0, sx1 = (x0 - px0) * scale, (x1 - px0) * scale
                out[y0 * scale:y1 * scale, x0 * scale:x1 * scale] = res[sy0:sy1, sx0:sx1]
                done += 1
                if on_tile:
                    on_tile(done, total)
        return out

    def _restore_faces(self, img_bgr: np.ndarray, faces: list, face_id: str,
                       strength: float) -> tuple[np.ndarray, int]:
        desc = self._load_face(face_id)
        if desc is None:
            return img_bgr, 0
        torch = _torch()
        device = self._device()
        out = img_bgr.copy()
        n = 0
        for face in faces:
            kps = getattr(face, "kps", None)
            bbox = getattr(face, "bbox", None)
            if bbox is None:
                continue
            crop_bgr, m = _align_face(img_bgr, kps, bbox)
            crop_rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
            t = torch.from_numpy(
                np.ascontiguousarray(crop_rgb.transpose(2, 0, 1)[None], dtype=np.float32) / 255.0
            ).to(device, torch.float32)
            with torch.inference_mode():
                res = desc(t)
                if isinstance(res, (tuple, list)):
                    res = res[0]
                res = res.float().clamp(0, 1)[0].permute(1, 2, 0).cpu().numpy()
            restored = cv2.cvtColor((res * 255).round().clip(0, 255).astype(np.uint8),
                                    cv2.COLOR_RGB2BGR)
            _paste_face(out, restored, m, bbox, strength)
            n += 1
        return out, n

    def upscale(
        self,
        img_bgr: np.ndarray,
        model_id: str,
        scale: int = 4,
        tile: int = 0,
        max_side: int = 0,
        face_restore: str = "none",
        face_strength: float = 0.8,
        detect_faces: Callable[[np.ndarray], list] | None = None,
        on_tile: Callable[[int, int], None] | None = None,
    ) -> dict:
        spec = self.spec(model_id)
        if spec is None or not spec.filename:
            raise RuntimeError(f"模型 {model_id} 不可用（可选档未安装）")
        if not self.available(model_id):
            raise RuntimeError(f"模型 {spec.label} 权重缺失：{self.weights_dir / spec.filename}")
        scale = 4 if int(scale) not in (2, 4) else int(scale)
        native = 4

        # 面部检测在原始分辨率上做（更稳），再按预缩放比例映射坐标。
        raw_h, raw_w = img_bgr.shape[:2]
        faces = []
        if face_restore and face_restore != "none" and detect_faces is not None:
            try:
                faces = [f for f in detect_faces(img_bgr) if getattr(f, "bbox", None) is not None]
                faces.sort(key=lambda f: float(
                    (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1])), reverse=True)
                faces = faces[:4]
            except Exception:
                faces = []

        # 输入预缩放：保证原生长边输出不超过上限，避免 4x 巨型缓冲撑爆内存。
        h, w = img_bgr.shape[:2]
        limit = int(max_side or 0)
        hard_cap = HARD_MAX_SIDE
        target_cap = min(limit, hard_cap) if limit else hard_cap
        if spec.max_side:
            target_cap = min(target_cap, spec.max_side)
        in_cap = max(256, int(target_cap / scale))
        sx = sy = 1.0
        if max(h, w) > in_cap:
            ratio = in_cap / float(max(h, w))
            img_bgr = cv2.resize(img_bgr, (max(1, int(w * ratio)), max(1, int(h * ratio))),
                                 interpolation=cv2.INTER_AREA)
            h, w = img_bgr.shape[:2]
            sx, sy = w / float(raw_w), h / float(raw_h)
            faces = [_scale_face(f, sx, sy) for f in faces]

        with GPU_LOCK:
            torch = _torch()
            if torch.cuda.is_available():
                torch.cuda.reset_peak_memory_stats()
            desc = self._load_sr(model_id)
            tile_size = self._auto_tile(tile, spec)
            img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
            started = time.time()
            while True:
                try:
                    out = self._tiled_sr(img_rgb, desc, native, tile_size, on_tile)
                    break
                except torch.cuda.OutOfMemoryError:
                    torch.cuda.empty_cache()
                    if tile_size <= MIN_TILE:
                        raise RuntimeError(
                            f"显存不足：{model_id} 在最小分块 {MIN_TILE}px 下仍 OOM，"
                            "请调小长边上限或换用更轻的模型"
                        )
                    tile_size = max(MIN_TILE, (tile_size // 2) - (tile_size // 2) % 8)
                    self._unload_sr()
                    desc = self._load_sr(model_id)
            sr_elapsed = time.time() - started
            # 先得到 native 4x 的 uint8，再做面部修复、2x 下采样、长边限制。
            out_u8 = (out * 255).round().clip(0, 255).astype(np.uint8)
            out_bgr = cv2.cvtColor(out_u8, cv2.COLOR_RGB2BGR)
            del out, out_u8
            face_n = 0
            if faces and torch.cuda.is_available():
                # 分块推理会留下较大的缓存块；先还给驱动，避免 GFPGAN 申请时被挤到换页。
                torch.cuda.empty_cache()
            if faces:
                out_bgr, face_n = self._restore_faces(
                    out_bgr, [_scale_face(f, native, native) for f in faces],
                    face_restore, face_strength,
                )
            if scale != native:
                nh, nw = h * scale, w * scale
                out_bgr = cv2.resize(out_bgr, (nw, nh), interpolation=cv2.INTER_LANCZOS4)
            h2, w2 = out_bgr.shape[:2]
            cap = int(max_side or 0) or HARD_MAX_SIDE
            if max(h2, w2) > cap:
                ratio = cap / float(max(h2, w2))
                out_bgr = cv2.resize(out_bgr, (max(1, int(w2 * ratio)), max(1, int(h2 * ratio))),
                                     interpolation=cv2.INTER_LANCZOS4)
            peak = 0.0
            if torch.cuda.is_available():
                peak = round(torch.cuda.max_memory_allocated() / 1024 ** 3, 2)
            elapsed = time.time() - started
            face_boxes: list[list[int]] = []
            if faces and out_bgr.shape[1] and out_bgr.shape[0]:
                # faces 在「预缩放后的输入坐标」空间；最终输出含 native 放大与可选下采样/长边限制
                fw = out_bgr.shape[1] / float(w)
                fh = out_bgr.shape[0] / float(h)
                for f in faces:
                    box = getattr(f, "bbox", None)
                    if box is None:
                        continue
                    x1, y1, x2, y2 = [float(v) for v in box]
                    face_boxes.append([round(x1 * fw), round(y1 * fh),
                                       round((x2 - x1) * fw), round((y2 - y1) * fh)])
        return {
            "image": out_bgr,
            "width": int(out_bgr.shape[1]),
            "height": int(out_bgr.shape[0]),
            "input_width": int(w),
            "input_height": int(h),
            "tile": int(tile_size),
            "elapsed": round(elapsed, 2),
            "sr_elapsed": round(sr_elapsed, 2),
            "peak_vram_gb": peak,
            "faces": face_n,
            "face_boxes": face_boxes,
            "model": model_id,
            "scale": scale,
        }

    def close(self) -> None:
        self._unload_sr()
        self._face_desc = None
        try:
            torch = _torch()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
