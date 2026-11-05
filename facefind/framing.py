"""构图检测：只收全身 / 完整上半身（YOLO pose ONNX）。

模型文件：models/<framing.model>.onnx（由 ultralytics 导出，推理走现有 onnxruntime-gpu）。
可用 yolov8n/m/x-pose、yolo11m/x-pose；配置缺失时回退 yolov8n-pose。
分类：
  full_body              髋+膝可见、踝在画面内、人物框未被下边缘截断
  upper_body_complete    双肩+髋(腰线)可见，躯干下缘未被图片边缘切割
  face_closeup           人物框过小或头部占框内大部分，无躯干
  upper_body_incomplete  肩/胸被边缘切断，或躯干完整性无法确认
  obscured_or_seated     坐/蹲/遮挡导致膝踝不可见，单独标记，不直接判不合格
  unknown                未检测到人物或模型不可用（保守放行，仅记录）
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import cv2
import numpy as np

from .config import Config

logger = logging.getLogger(__name__)

MODELS_DIR = Path(__file__).resolve().parents[1] / "models"
FALLBACK_MODEL = MODELS_DIR / "yolov8n-pose.onnx"
DEFAULT_INPUT_SIZE = 640
PERSON_CONF = 0.25
KPT_CONF = 0.30

LABELS_CN = {
    "full_body": "全身",
    "upper_body_complete": "完整上半身",
    "face_closeup": "脸部特写",
    "upper_body_incomplete": "上半身不完整",
    "obscured_or_seated": "坐姿/遮挡",
    "unknown": "未判定",
}

_LOCK = threading.Lock()
_SESSIONS: dict[str, object] = {}
_SESSION_FAILED: dict[str, bool] = {}
_ACTIVE_MODEL: dict[str, str] = {}


def model_path_for(cfg: Config) -> Path:
    """解析实际使用的姿态模型路径；配置缺失则回退 yolov8n-pose。"""
    raw = str(getattr(cfg.framing, "model_path", "") or "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (cfg.root / p)
    name = str(getattr(cfg.framing, "model", "") or "yolov8n-pose").strip() or "yolov8n-pose"
    return MODELS_DIR / f"{name}.onnx"


def _session(path: Path):
    key = str(path)
    with _LOCK:
        if key in _SESSIONS:
            return _SESSIONS[key]
        if _SESSION_FAILED.get(key):
            return None
        target = path
        if not target.exists():
            if FALLBACK_MODEL.exists() and path != FALLBACK_MODEL:
                logger.warning("姿态模型不存在，回退 %s：%s", FALLBACK_MODEL.name, path)
                target = FALLBACK_MODEL
            else:
                _SESSION_FAILED[key] = True
                logger.warning("姿态模型不存在，构图过滤将放行：%s", path)
                return None
        try:
            from .env import prepare_cuda_dlls

            prepare_cuda_dlls()
            import onnxruntime as ort

            so = ort.SessionOptions()
            so.log_severity_level = 3
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
            try:
                sess = ort.InferenceSession(str(target), sess_options=so, providers=providers)
            except Exception:
                sess = ort.InferenceSession(str(target), sess_options=so, providers=["CPUExecutionProvider"])
            _SESSIONS[key] = sess
            _ACTIVE_MODEL[key] = target.name
            logger.info("构图模型已加载 file=%s device=%s", target.name, sess.get_providers()[0])
            return sess
        except Exception as exc:
            _SESSION_FAILED[key] = True
            _ACTIVE_MODEL[key] = ""
            logger.warning("构图模型加载失败，将放行并记录：%s", exc)
            return None


def active_model_name(cfg: Config) -> str:
    """当前实际加载的姿态模型文件名（未加载时返回配置值）。"""
    p = model_path_for(cfg)
    return _ACTIVE_MODEL.get(str(p)) or p.name


def _letterbox(img: np.ndarray, input_size: int = DEFAULT_INPUT_SIZE) -> tuple[np.ndarray, float, int, int]:
    h, w = img.shape[:2]
    size = max(64, int(input_size))
    scale = min(size / w, size / h)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    pad_x, pad_y = (size - nw) // 2, (size - nh) // 2
    canvas[pad_y:pad_y + nh, pad_x:pad_x + nw] = resized
    blob = canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
    return np.ascontiguousarray(blob), scale, pad_x, pad_y


def _persons(img: np.ndarray, cfg: Config) -> list[dict]:
    path = model_path_for(cfg)
    sess = _session(path)
    if sess is None:
        return []
    input_size = int(getattr(cfg.framing, "input_size", DEFAULT_INPUT_SIZE) or DEFAULT_INPUT_SIZE)
    blob, scale, pad_x, pad_y = _letterbox(img, input_size)
    name = sess.get_inputs()[0].name
    out = sess.run(None, {name: blob})[0]
    if out.ndim != 3:
        return []
    pred = out[0].T if out.shape[1] == 56 else out[0]
    if pred.shape[1] != 56:
        logger.warning("构图模型输出维度异常：%s", out.shape)
        return []
    h, w = img.shape[:2]
    persons: list[dict] = []
    for row in pred:
        conf = float(row[4])
        if conf < PERSON_CONF:
            continue
        cx, cy, bw, bh = [float(v) for v in row[:4]]
        box_l = (cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2)
        box = (
            (box_l[0] - pad_x) / scale, (box_l[1] - pad_y) / scale,
            (box_l[2] - pad_x) / scale, (box_l[3] - pad_y) / scale,
        )
        box = (max(0.0, box[0]), max(0.0, box[1]), min(float(w), box[2]), min(float(h), box[3]))
        kpts = []
        raw = row[5:].reshape(17, 3)
        for x, y, kc in raw:
            kpts.append(((float(x) - pad_x) / scale, (float(y) - pad_y) / scale, float(kc)))
        persons.append({"box": box, "box_letterbox": box_l, "kpts": kpts, "conf": conf})
    return _nms(persons)


def _iou(a: tuple, b: tuple) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _nms(persons: list[dict], iou_thr: float = 0.5) -> list[dict]:
    """YOLO ONNX 导出默认不含 NMS，这里补一次类内 NMS，避免同一人被重复计数。"""
    kept: list[dict] = []
    for p in sorted(persons, key=lambda x: x["conf"], reverse=True):
        if all(_iou(p["box"], k["box"]) <= iou_thr for k in kept):
            kept.append(p)
    return kept


def _pick_person(persons: list[dict], face_bbox) -> dict | None:
    if not persons:
        return None
    if not face_bbox:
        return max(persons, key=lambda p: p["conf"])
    fx = (face_bbox[0] + face_bbox[2]) / 2.0
    fy = (face_bbox[1] + face_bbox[3]) / 2.0
    inside = [
        p for p in persons
        if p["box"][0] <= fx <= p["box"][2] and p["box"][1] <= fy <= p["box"][3]
    ]
    if inside:
        return max(inside, key=lambda p: (p["conf"], (p["box"][2] - p["box"][0]) * (p["box"][3] - p["box"][1])))
    return min(persons, key=lambda p: (abs((p["box"][0] + p["box"][2]) / 2 - fx) +
                                       abs((p["box"][1] + p["box"][3]) / 2 - fy)))


def _visible(kpt, w: int, h: int, thr: float = KPT_CONF) -> bool:
    x, y, c = kpt
    return c >= thr and 0 <= x <= w - 1 and 0 <= y <= h - 1


def classify(img: np.ndarray, face_bbox, cfg: Config) -> dict:
    """返回构图标签、人物框比例、关键点可见性与保留建议。"""
    h, w = img.shape[:2]
    persons = _persons(img, cfg)
    person = _pick_person(persons, face_bbox)
    if person is None:
        return {
            "label": "unknown", "label_cn": LABELS_CN["unknown"],
            "person_ratio": 0.0, "n_persons": 0, "keep": True,
            "reason": "未检测到人物（模型不可用或画面无人）", "box": [],
            "visible": {"shoulders": 0, "hips": 0, "knees": 0, "ankles": 0},
            "edge_cut": {},
        }
    x1, y1, x2, y2 = person["box"]
    bw, bh = max(1.0, x2 - x1), max(1.0, y2 - y1)
    ratio = round(bh / h, 3)
    tolerance = max(0.0, float(cfg.framing.edge_cut_tolerance))
    bottom_gap = (h - y2) / h
    top_gap = y1 / h
    bottom_cut = bottom_gap <= tolerance
    k = person["kpts"]
    # 关键点贴着画面边缘视为被裁切（模型常在边缘“猜”出看不见的膝/踝）
    edge_margin = max(2.0, h * tolerance)

    def _inside(i: int) -> bool:
        x, y, c = k[i]
        return c >= KPT_CONF and edge_margin <= x <= w - 1 - edge_margin and edge_margin <= y <= h - 1 - edge_margin

    def count(idx: tuple[int, ...]) -> int:
        return sum(1 for i in idx if _visible(k[i], w, h))

    def count_inside(idx: tuple[int, ...]) -> int:
        return sum(1 for i in idx if _inside(i))

    shoulders = count((5, 6))
    hips = count((11, 12))
    knees = count_inside((13, 14))
    ankles = count_inside((15, 16))
    hips_near_bottom = any(
        _visible(k[i], w, h) and k[i][1] >= h * (1 - tolerance) for i in (11, 12)
    )
    shoulders_near_top = any(
        _visible(k[i], w, h) and k[i][1] <= h * tolerance for i in (5, 6)
    )
    face_h = (face_bbox[3] - face_bbox[1]) if face_bbox else 0.0
    face_in_person = round(min(1.0, face_h / bh), 3) if face_h else 0.0

    label = "upper_body_incomplete"
    reason = ""
    if hips >= 1 and knees >= 1 and ankles >= 1 and not bottom_cut:
        label = "full_body"
    elif hips >= 1 and knees == 0 and ankles == 0 and not hips_near_bottom:
        label = "obscured_or_seated"
        reason = "髋可见但膝/踝不可见，疑似坐姿或遮挡（仅标记）"
    elif shoulders >= 2 and hips >= 1 and not hips_near_bottom and not shoulders_near_top:
        label = "upper_body_complete"
    elif hips == 0 and face_in_person >= 0.45:
        label = "face_closeup"
        reason = "头部占人物框大部分且无躯干"
    elif hips == 0 and knees == 0 and ratio < cfg.framing.min_person_ratio:
        label = "face_closeup"
        reason = f"人物框过小（高度占比 {ratio:.0%} < {cfg.framing.min_person_ratio:.0%}）"
    else:
        reasons = []
        if shoulders < 2:
            reasons.append("双肩未同时可见")
        if hips == 0:
            reasons.append("髋部/腰线不可见")
        if shoulders_near_top:
            reasons.append("肩部贴近上边缘")
        if hips_near_bottom:
            reasons.append("髋部贴近下边缘")
        if bottom_cut:
            reasons.append("人物框被下边缘截断")
        reason = "、".join(reasons) or "躯干完整性无法确认"

    mode = cfg.framing.mode
    if label == "unknown":
        keep = True
    elif mode == "strict_full_body":
        keep = label == "full_body"
    elif mode == "allow_upper_body":
        keep = label in ("full_body", "upper_body_complete", "upper_body_incomplete", "obscured_or_seated")
    else:
        keep = label in ("full_body", "upper_body_complete", "obscured_or_seated")

    return {
        "label": label,
        "label_cn": LABELS_CN.get(label, label),
        "person_ratio": ratio,
        "face_in_person": face_in_person,
        "n_persons": len(persons),
        "keep": bool(keep),
        "reason": reason,
        "box": [round(v, 1) for v in person["box"]],
        "visible": {"shoulders": shoulders, "hips": hips, "knees": knees, "ankles": ankles},
        "edge_cut": {
            "top": shoulders_near_top, "bottom": bottom_cut,
            "bottom_gap": round(bottom_gap, 3), "top_gap": round(top_gap, 3),
        },
    }
