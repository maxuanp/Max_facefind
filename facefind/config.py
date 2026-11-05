from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# 内置档位：config.yaml 的 profiles 段可覆盖；显式写入 faces/framing 的键优先。
BUILTIN_PROFILES: dict[str, dict[str, dict[str, Any]]] = {
    "balanced": {
        "faces": {
            "model": "buffalo_l",
            "det_size": 640,
            "det_thresh": 0.5,
            "min_face_px": 80,
            "accept_threshold": 0.55,
            "review_threshold": 0.35,
            "dedupe_threshold": 0.92,
        },
        "framing": {"model": "yolov8n-pose", "input_size": 640},
    },
    "max_quality": {
        # 实测：antelopev2 检测结果与 buffalo_l 基本一致（去 1024 反而降检出率），
        # 提升主要来自 glintr100 识别 embedding；构图大模型在样本上无增益，仍用 yolov8n。
        "faces": {
            "model": "antelopev2",
            "det_size": 640,
            "det_thresh": 0.5,
            "min_face_px": 80,
            "accept_threshold": 0.55,
            "review_threshold": 0.35,
            "dedupe_threshold": 0.92,
        },
        "framing": {"model": "yolov8n-pose", "input_size": 640},
    },
}


@dataclass
class SiteRate:
    concurrency: int = 3
    delay_min_ms: int = 100
    delay_max_ms: int = 400


@dataclass
class NetworkConfig:
    proxy: str = ""
    timeout: float = 20.0
    retries: int = 3
    backoff_seconds: float = 1.0
    user_agent: str = DEFAULT_UA
    sites: dict[str, SiteRate] = field(default_factory=dict)

    def site(self, name: str) -> SiteRate:
        if name in self.sites:
            return self.sites[name]
        if name == "baidu":
            return SiteRate(3, 100, 400)
        return SiteRate(2, 300, 700)

    @property
    def proxies(self) -> dict[str, str] | None:
        return {"http": self.proxy, "https": self.proxy} if self.proxy else None


@dataclass
class FaceConfig:
    enabled: bool = True
    dir: str = "faces"
    map_file: str = "data/face_map.yaml"
    model: str = "buffalo_l"
    root: str = "models/insightface"   # insightface 模型根目录（项目内持久目录）
    device: str = "auto"  # auto | gpu | cpu
    det_size: int = 640
    det_thresh: float = 0.5
    allow_multiple_faces: bool = False
    similarity_mode: str = "max"  # max | mean | centroid
    accept_threshold: float = 0.55
    review_threshold: float = 0.35
    dedupe_threshold: float = 0.92
    min_face_px: int = 80


@dataclass
class QualityConfig:
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "resolution": 0.4,
            "sharpness": 0.3,
            "face_ratio": 0.2,
            "compression": 0.1,
        }
    )


@dataclass
class DedupeConfig:
    enabled: bool = True
    phash_threshold: int = 8


@dataclass
class ExcludeConfig:
    dirs: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    phash_threshold: int = 8
    dhash_threshold: int = 10
    recursive: bool = True
    crop_tolerance: bool = True
    rebuild: bool = False
    # ---- 实时监听（UI 服务内常驻线程）----
    watch: bool = True                 # 是否开启目录监听
    watch_debounce_ms: int = 600        # 事件去抖窗口（500–1000 为合理区间）
    watch_sync_on_start: bool = True    # 启动时后台补一次增量扫描（补齐停机期间的改动）
    # 任务开始时的排除索引扫描策略：auto（监听健康则跳过）/ always（总是扫描）/ never（从不扫描）
    scan_on_run: str = "auto"
    # 监听/扫描时跳过的子目录（相对项目根目录，也支持绝对路径）；
    # 程序自身输出目录（output_dir / raw_dir / data）总会自动跳过。
    watch_skip: list[str] = field(
        default_factory=lambda: [
            "downloads", "_raw", "data", "data/sr", "data/recycle", "data/thumbs",
            "data/sr_preview", "data/sr_bench", "data/logs", "data/reports",
            "data/browser_profile", "data/_tmp_sitecheck", "data/_ui_shots", "data/_lb_shots",
        ]
    )


@dataclass
class SearchConfig:
    sources: list[str] = field(default_factory=lambda: ["baidu"])
    count: int = 5
    page_size: int = 30
    max_pages: int = 8
    min_width: int = 200
    min_height: int = 200
    detail_budget: int = 12        # 单轮单人每个关键词最多打开的小红书详情页数（风控节流）


@dataclass
class BrowserConfig:
    profile_dir: str = "data/browser_profile"   # Playwright 持久化会话目录（登录一次后复用）
    channel: str = ""                            # "" = 自动（优先 chrome，其次 msedge）
    headless: bool = True                        # 搜索/检查用无头；打开登录窗口时强制有头
    login_timeout: int = 300                     # 打开登录窗口后等待扫码的秒数
    login_keep_open: list[str] = field(default_factory=lambda: ["xiaohongshu"])  # 登录成功后不自动关窗的站点
    cookie_dir: str = "secrets"                  # 只读 cookie 文件目录：xhs.txt / weibo.txt


@dataclass
class FramingConfig:
    """构图过滤：只收全身 / 完整上半身。"""

    mode: str = "prefer_full_body"   # prefer_full_body | allow_upper_body | strict_full_body
    edge_cut_tolerance: float = 0.03  # 人物框距图片边缘的容差（占画面比例）
    min_person_ratio: float = 0.45    # 人物框高度 / 画面高度；过小视为特写或远景
    model: str = "yolov8n-pose"       # models/<model>.onnx；可用 yolov8n/m/x-pose、yolo11m/x-pose
    model_path: str = ""              # 显式指定模型路径（优先级高于 model）
    input_size: int = 640             # 推理输入边长


@dataclass
class PersonConfig:
    name: str
    count: int | None = None
    queries: list[str] = field(default_factory=list)
    reference_images: list[str] = field(default_factory=list)
    sources: list[str] | None = None
    accept_threshold: float | None = None
    review_threshold: float | None = None
    similarity_mode: str | None = None
    dedupe_threshold: float | None = None
    min_width: int | None = None
    min_height: int | None = None


@dataclass
class TaskConfig:
    """把全局配置 + 单人覆盖合并后的可执行配置。"""

    name: str
    count: int
    queries: list[str]
    sources: list[str]
    accept_threshold: float
    review_threshold: float
    similarity_mode: str
    dedupe_threshold: float
    min_width: int
    min_height: int
    reference_images: list[str]


@dataclass
class Config:
    root: Path
    output_dir: str = "downloads"
    raw_dir: str = "_raw"
    index_db: str = "data/index.db"
    log_dir: str = "data/logs"
    profile: str = "balanced"
    profiles: dict[str, Any] = field(default_factory=dict)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    face: FaceConfig = field(default_factory=FaceConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    dedupe: DedupeConfig = field(default_factory=DedupeConfig)
    exclude: ExcludeConfig = field(default_factory=ExcludeConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    framing: FramingConfig = field(default_factory=FramingConfig)
    people: list[PersonConfig] = field(default_factory=list)
    auth: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | os.PathLike) -> "Config":
        p = Path(path).resolve()
        raw: dict[str, Any] = {}
        if p.exists():
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        root = p.parent
        cfg = cls(root=root)
        for key in ("output_dir", "raw_dir", "index_db", "log_dir"):
            if key in raw:
                setattr(cfg, key, str(raw[key]))

        profiles_raw = raw.get("profiles") or {}
        profile = str(raw.get("profile") or "balanced").strip() or "balanced"
        if profile not in BUILTIN_PROFILES and profile not in profiles_raw:
            profile = "balanced"
        cfg.profile = profile
        cfg.profiles = profiles_raw
        face_defaults = _profile_section(profile, profiles_raw, "faces")
        framing_defaults = _profile_section(profile, profiles_raw, "framing")

        net = raw.get("network") or {}
        sites_raw = net.get("sites") or {}
        cfg.network = NetworkConfig(
            proxy=str(net.get("proxy") or ""),
            timeout=float(net.get("timeout", 20)),
            retries=int(net.get("retries", 3)),
            backoff_seconds=float(net.get("backoff_seconds", 1.0)),
            user_agent=str(net.get("user_agent") or DEFAULT_UA),
            sites={
                k: SiteRate(
                    concurrency=int(v.get("concurrency", 3)),
                    delay_min_ms=int(v.get("delay_min_ms", 100)),
                    delay_max_ms=int(v.get("delay_max_ms", 400)),
                )
                for k, v in sites_raw.items()
            },
        )

        face = raw.get("faces") or {}
        cfg.face = FaceConfig(
            enabled=bool(face.get("enabled", True)),
            dir=str(face.get("dir", "faces")),
            map_file=str(face.get("map_file", "data/face_map.yaml")),
            model=str(_pick(face, face_defaults, "model", "buffalo_l")),
            root=str(face.get("root") or "models/insightface"),
            device=str(face.get("device", "auto")),
            det_size=int(_pick(face, face_defaults, "det_size", 640)),
            det_thresh=float(_pick(face, face_defaults, "det_thresh", 0.5)),
            allow_multiple_faces=bool(face.get("allow_multiple_faces", False)),
            similarity_mode=str(face.get("similarity_mode", "max")),
            accept_threshold=float(_pick(face, face_defaults, "accept_threshold", 0.55)),
            review_threshold=float(_pick(face, face_defaults, "review_threshold", 0.35)),
            dedupe_threshold=float(_pick(face, face_defaults, "dedupe_threshold", 0.92)),
            min_face_px=int(_pick(face, face_defaults, "min_face_px", 80)),
        )

        quality = raw.get("quality") or {}
        if quality.get("weights"):
            cfg.quality = QualityConfig(weights={k: float(v) for k, v in quality["weights"].items()})

        dedupe = raw.get("dedupe") or {}
        cfg.dedupe = DedupeConfig(
            enabled=bool(dedupe.get("enabled", True)),
            phash_threshold=int(dedupe.get("phash_threshold", 8)),
        )

        exclude = raw.get("exclude") or {}
        cfg.exclude = ExcludeConfig(
            dirs=[str(p) for p in (exclude.get("dirs") or [])],
            files=[str(p) for p in (exclude.get("files") or [])],
            phash_threshold=max(0, min(16, int(exclude.get("phash_threshold", 8)))),
            dhash_threshold=max(0, min(16, int(exclude.get("dhash_threshold", 10)))),
            recursive=bool(exclude.get("recursive", True)),
            crop_tolerance=bool(exclude.get("crop_tolerance", True)),
            rebuild=bool(exclude.get("rebuild", False)),
            watch=bool(exclude.get("watch", True)),
            watch_debounce_ms=max(200, min(3000, int(exclude.get("watch_debounce_ms", 600)))),
            watch_sync_on_start=bool(exclude.get("watch_sync_on_start", True)),
            scan_on_run=str(exclude.get("scan_on_run", "auto")).lower()
            if str(exclude.get("scan_on_run", "auto")).lower() in ("auto", "always", "never") else "auto",
            watch_skip=[str(p) for p in exclude["watch_skip"]] if isinstance(exclude.get("watch_skip"), list)
            else ExcludeConfig().watch_skip,
        )

        search = raw.get("search") or {}
        cfg.search = SearchConfig(
            sources=[str(s) for s in (search.get("sources") or ["baidu"])],
            count=int(search.get("count", 5)),
            page_size=int(search.get("page_size", 30)),
            max_pages=int(search.get("max_pages", 8)),
            min_width=int(search.get("min_width", 200)),
            min_height=int(search.get("min_height", 200)),
            detail_budget=max(1, int(search.get("detail_budget", 12))),
        )

        browser = raw.get("browser") or {}
        keep_raw = browser.get("login_keep_open", ["xiaohongshu"])
        cfg.browser = BrowserConfig(
            profile_dir=str(browser.get("profile_dir", "data/browser_profile")),
            channel=str(browser.get("channel") or ""),
            headless=bool(browser.get("headless", True)),
            login_timeout=int(browser.get("login_timeout", 300)),
            login_keep_open=[str(s) for s in keep_raw] if isinstance(keep_raw, list) else [],
            cookie_dir=str(browser.get("cookie_dir", "secrets")),
        )

        framing = raw.get("framing") or {}
        cfg.framing = FramingConfig(
            mode=str(framing.get("mode", "prefer_full_body")),
            edge_cut_tolerance=float(framing.get("edge_cut_tolerance", 0.03)),
            min_person_ratio=float(framing.get("min_person_ratio", 0.45)),
            model=str(_pick(framing, framing_defaults, "model", "yolov8n-pose")),
            model_path=str(framing.get("model_path") or ""),
            input_size=int(_pick(framing, framing_defaults, "input_size", 640)),
        )

        cfg.auth = raw.get("auth") or {}
        for item in raw.get("people") or []:
            cfg.people.append(
                PersonConfig(
                    name=str(item["name"]),
                    count=int(item["count"]) if item.get("count") is not None else None,
                    queries=[str(q) for q in (item.get("queries") or [])],
                    reference_images=[str(x) for x in (item.get("reference_images") or [])],
                    sources=[str(s) for s in item["sources"]] if item.get("sources") else None,
                    accept_threshold=_opt_float(item.get("accept_threshold")),
                    review_threshold=_opt_float(item.get("review_threshold")),
                    similarity_mode=str(item["similarity_mode"]) if item.get("similarity_mode") else None,
                    dedupe_threshold=_opt_float(item.get("dedupe_threshold")),
                    min_width=int(item["min_width"]) if item.get("min_width") is not None else None,
                    min_height=int(item["min_height"]) if item.get("min_height") is not None else None,
                )
            )
        return cfg

    def apply_profile(self, name: str) -> "Config":
        """切换档位（CLI/UI 显式覆盖时使用）。"""
        if name not in BUILTIN_PROFILES and name not in self.profiles:
            raise ValueError(f"未知档位：{name}（可用 balanced / max_quality 或自定义 profiles）")
        fs = _profile_section(name, self.profiles, "faces")
        fr = _profile_section(name, self.profiles, "framing")
        self.profile = name
        for key in ("model", "det_size", "det_thresh", "min_face_px",
                    "accept_threshold", "review_threshold", "dedupe_threshold"):
            if key in fs:
                setattr(self.face, key, fs[key])
        for key in ("model", "input_size"):
            if key in fr:
                setattr(self.framing, key, fr[key])
        return self

    # ---- 路径 ----
    def path(self, rel: str) -> Path:
        return (self.root / rel).resolve()

    @property
    def output_path(self) -> Path:
        return self.path(self.output_dir)

    @property
    def raw_path(self) -> Path:
        return self.path(self.raw_dir)

    @property
    def index_path(self) -> Path:
        return self.path(self.index_db)

    @property
    def log_path(self) -> Path:
        return self.path(self.log_dir)

    @property
    def faces_path(self) -> Path:
        return self.path(self.face.dir)

    @property
    def browser_profile_path(self) -> Path:
        return self.path(self.browser.profile_dir)

    @property
    def secrets_path(self) -> Path:
        return self.path(self.browser.cookie_dir)

    # ---- 单人配置 ----
    def person_config(self, name: str) -> PersonConfig | None:
        return next((p for p in self.people if p.name == name), None)

    def build_task(self, name: str, *, count: int | None = None, threshold: float | None = None,
                   review_threshold: float | None = None, mode: str | None = None,
                   sources: list[str] | None = None, queries: list[str] | None = None,
                   reference_images: list[str] | None = None) -> TaskConfig:
        pc = self.person_config(name)
        return TaskConfig(
            name=name,
            count=count if count is not None else (pc.count if pc and pc.count else self.search.count),
            queries=(queries or (pc.queries if pc and pc.queries else [name])) or [name],
            sources=sources or (pc.sources if pc and pc.sources else self.search.sources),
            accept_threshold=threshold if threshold is not None else (
                pc.accept_threshold if pc and pc.accept_threshold is not None else self.face.accept_threshold
            ),
            review_threshold=review_threshold if review_threshold is not None else (
                pc.review_threshold if pc and pc.review_threshold is not None else self.face.review_threshold
            ),
            similarity_mode=mode or (pc.similarity_mode if pc and pc.similarity_mode else self.face.similarity_mode),
            dedupe_threshold=(
                pc.dedupe_threshold if pc and pc.dedupe_threshold is not None else self.face.dedupe_threshold
            ),
            min_width=(pc.min_width if pc and pc.min_width is not None else self.search.min_width),
            min_height=(pc.min_height if pc and pc.min_height is not None else self.search.min_height),
            reference_images=reference_images or (pc.reference_images if pc else []),
        )


def _profile_section(profile: str, profiles_raw: dict[str, Any], section: str) -> dict[str, Any]:
    """合并内置档位与 config.yaml 中同名档位定义。"""
    out = dict(BUILTIN_PROFILES.get(profile, {}).get(section, {}))
    custom = profiles_raw.get(profile) or {}
    if isinstance(custom, dict):
        extra = custom.get(section) or {}
        if isinstance(extra, dict):
            out.update(extra)
    return out


def _pick(section: dict[str, Any], defaults: dict[str, Any], key: str, fallback: Any) -> Any:
    """显式配置优先；留空/缺省时跟随档位。"""
    v = section.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        v = defaults.get(key, fallback)
    return v


def _opt_float(v: Any) -> float | None:
    return float(v) if v is not None else None
