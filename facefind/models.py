from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SearchItem:
    """一条搜索结果（站点无关）。"""

    url: str
    fallback_urls: list[str] = field(default_factory=list)
    source: str = ""
    query: str = ""
    title: str = ""
    width: int = 0
    height: int = 0
    page_url: str = ""
    page: int = 0
    variant: str = ""          # 原图变体：origin / default / preview / dom / cover
    fallback_cover: bool = False  # 详情页取不到原图时回退封面

    def all_urls(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for u in [self.url, *self.fallback_urls]:
            if u and u not in seen:
                seen.add(u)
                out.append(u)
        return out


@dataclass
class ImageMetrics:
    width: int
    height: int
    area: int
    sharpness: float
    compression: float = 1.0


@dataclass
class FaceMatch:
    """候选图的人脸比对结果。"""

    similarity: float = 0.0
    matched_ref: str = ""
    n_faces: int = 0
    face_px: int = 0
    face_ratio: float = 0.0
    reason: str = ""
    bbox: tuple[float, float, float, float] | None = None


@dataclass
class Candidate:
    item: SearchItem
    data: bytes
    sha256: str
    raw_path: str
    metrics: ImageMetrics
    phash: int
    clarity: float
    match: FaceMatch | None = None
    n_faces: int = 0
