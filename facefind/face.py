from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .models import FaceMatch

logger = logging.getLogger(__name__)


# ---------- 参考照 ----------
@dataclass
class Reference:
    path: str
    embedding: np.ndarray


@dataclass
class PersonReferences:
    name: str
    refs: list[Reference] = field(default_factory=list)
    mode: str = "max"
    centroid: np.ndarray | None = None

    def score(self, embedding: np.ndarray) -> tuple[float, str]:
        if not self.refs:
            return 0.0, ""
        sims = [float(np.dot(r.embedding, embedding)) for r in self.refs]
        best_idx = int(np.argmax(sims))
        if self.mode == "mean":
            return float(np.mean(sims)), self.refs[best_idx].path
        if self.mode == "centroid" and self.centroid is not None:
            return float(np.dot(self.centroid, embedding)), self.refs[best_idx].path
        return sims[best_idx], self.refs[best_idx].path


class FaceEngine:
    """insightface 封装：GPU 可用时优先 CUDA，否则回落 CPU。

    model 可切换 buffalo_l / antelopev2；root 指向项目内持久模型目录，
    避免把模型散落在用户主目录或临时路径。加载失败时自动回退 buffalo_l。
    """

    def __init__(self, model: str = "buffalo_l", device: str = "auto", det_size: int = 640,
                 min_face_px: int = 80, allow_multiple_faces: bool = False,
                 root: str | None = None, det_thresh: float = 0.5):
        self.model = model
        self.device_pref = device
        self.det_size = det_size
        self.min_face_px = min_face_px
        self.allow_multiple_faces = allow_multiple_faces
        self.root = root
        self.det_thresh = det_thresh
        self.app = None
        self.device = "unknown"
        self.providers: list[str] = []
        self.fallback_used = False

    def _create(self, model: str, use_gpu: bool):
        from insightface.app import FaceAnalysis

        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if use_gpu else ["CPUExecutionProvider"]
        kwargs = {"name": model, "providers": providers}
        if self.root:
            kwargs["root"] = self.root
        app = FaceAnalysis(**kwargs)
        app.prepare(ctx_id=0 if use_gpu else -1, det_size=(self.det_size, self.det_size),
                    det_thresh=self.det_thresh)
        return app

    def load(self) -> "FaceEngine":
        from .env import prepare_cuda_dlls

        prepare_cuda_dlls()
        logging.getLogger("insightface").setLevel(logging.ERROR)

        use_gpu = self.device_pref in ("auto", "gpu")
        tried: list[str] = []
        try:
            app = self._create(self.model, use_gpu)
        except Exception as exc:
            tried.append(f"{self.model}({'gpu' if use_gpu else 'cpu'}): {exc}")
            # GPU 失败先回落 CPU；模型本身不可用再回落 buffalo_l
            if use_gpu and self.device_pref != "gpu":
                try:
                    logger.warning("GPU 不可用，回落到 CPU：%s", exc)
                    app = self._create(self.model, False)
                except Exception as exc2:
                    tried.append(f"{self.model}(cpu): {exc2}")
                    app = None
            else:
                app = None
            if app is None:
                if self.model != "buffalo_l":
                    logger.warning("模型 %s 不可用，回退 buffalo_l", self.model)
                    self.fallback_used = True
                    self.model = "buffalo_l"
                    try:
                        app = self._create("buffalo_l", use_gpu)
                    except Exception as exc3:
                        tried.append(f"buffalo_l(cpu): {exc3}")
                        app = self._create("buffalo_l", False)
                elif self.device_pref == "gpu":
                    raise RuntimeError("GPU 加载失败且 device=gpu：" + "；".join(tried)) from exc
                else:
                    raise RuntimeError("人脸模型加载失败：" + "；".join(tried))
        self.app = app
        det = app.models.get("detection")
        self.providers = det.session.get_providers() if det is not None else []
        self.device = "cuda" if "CUDAExecutionProvider" in self.providers else "cpu"
        logger.info("人脸模型已加载 model=%s device=%s providers=%s det_size=%s det_thresh=%s",
                    self.model, self.device, self.providers, self.det_size, self.det_thresh)
        return self

    def detect(self, img: np.ndarray):
        if self.app is None:
            self.load()
        return self.app.get(img)

    def embed_largest(self, img: np.ndarray) -> tuple[np.ndarray | None, FaceMatch]:
        """检测并取最大人脸；返回 (embedding, FaceMatch)。"""
        faces = self.detect(img)
        h, w = img.shape[:2]
        if not faces:
            return None, FaceMatch(n_faces=0, reason="no_face")
        face = max(faces, key=lambda f: float((f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1])))
        x1, y1, x2, y2 = [float(v) for v in face.bbox]
        face_px = int(max(x2 - x1, y2 - y1))
        face_ratio = float(max(0.0, (x2 - x1) * (y2 - y1)) / float(w * h))
        emb = getattr(face, "normed_embedding", None)
        if emb is None:
            emb = face.embedding / (np.linalg.norm(face.embedding) + 1e-9)
        emb = np.asarray(emb, dtype=np.float32)
        match = FaceMatch(
            n_faces=len(faces), face_px=face_px, face_ratio=round(face_ratio, 4),
            similarity=0.0, matched_ref="", bbox=(x1, y1, x2, y2),
        )
        if face_px < self.min_face_px:
            match.reason = "face_too_small"
        return emb, match


def _read_image(path: Path) -> np.ndarray | None:
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception:
        return None


def build_references(engine: FaceEngine, name: str, paths: list[str], mode: str = "max",
                     max_refs: int = 20) -> tuple[PersonReferences | None, list[str], list[str]]:
    """提取多张参考照 embedding。返回 (references, warnings, used_paths)。"""
    refs = PersonReferences(name=name, mode=mode)
    warnings: list[str] = []
    used: list[str] = []
    for raw in paths[:max_refs]:
        p = Path(raw)
        if not p.exists():
            warnings.append(f"[参考照] 文件不存在，已跳过：{raw}")
            continue
        img = _read_image(p)
        if img is None:
            warnings.append(f"[参考照] 无法解码，已跳过：{raw}")
            continue
        emb, match = engine.embed_largest(img)
        if match.n_faces == 0:
            warnings.append(f"[参考照] 未检测到人脸，已跳过：{p.name}")
            continue
        if match.n_faces > 1 and not engine.allow_multiple_faces:
            warnings.append(f"[参考照] 检测到 {match.n_faces} 张人脸，已跳过：{p.name}")
            continue
        if match.n_faces > 1:
            warnings.append(f"[参考照] 检测到 {match.n_faces} 张人脸，已取最大人脸：{p.name}")
        if match.reason == "face_too_small":
            warnings.append(f"[参考照] 人脸过小（{match.face_px}px），已跳过：{p.name}")
            continue
        assert emb is not None
        refs.refs.append(Reference(path=str(p), embedding=emb))
        used.append(str(p))

    if refs.mode == "centroid" and refs.refs:
        centroid = np.mean([r.embedding for r in refs.refs], axis=0)
        norm = float(np.linalg.norm(centroid))
        refs.centroid = centroid / norm if norm else centroid
    return (refs if refs.refs else None), warnings, used


def score_candidate(engine: FaceEngine, refs: PersonReferences,
                    img: np.ndarray) -> tuple[np.ndarray | None, FaceMatch]:
    """候选图与某人的多张参考照比对；返回 (候选 embedding, FaceMatch)。

    similarity_mode=max 时取与各参考照的最高相似度；mean / centroid 按配置计算。
    """
    emb, match = engine.embed_largest(img)
    if emb is None:
        return None, match
    if match.reason == "face_too_small":
        return emb, match
    sim, ref_path = refs.score(emb)
    match.similarity = round(float(sim), 4)
    match.matched_ref = ref_path
    return emb, match
