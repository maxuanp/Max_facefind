from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .config import Config, PersonConfig

DEFAULT: dict[str, Any] = {
    "profile": "",   # 留空 = 跟随 config.yaml
    "search": {"sources": ["baidu"], "count": 5, "max_pages": 8},
    "face": {"model": "", "accept_threshold": 0.55, "review_threshold": 0.35, "min_face_px": 80},
    "framing": {"model": "", "mode": "prefer_full_body", "edge_cut_tolerance": 0.03, "min_person_ratio": 0.45},
    "review_enabled": True,
    "output_dir": "downloads",
    "proxy": "",
    "sr": {
        "model": "realesrgan_x4plus",
        "scale": 4,
        "face_restore": "gfpgan",
        "face_strength": 0.8,
        "max_side": 4096,
        "tile": 0,
        "jpeg_quality": 95,
        "keep_original": True,
    },
    "exclude": {"dirs": [], "files": [], "phash_threshold": 8, "scan_on_run": "auto"},
    "auth": {},
    "people": {},
}


def _merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class SettingsStore:
    """UI 设置覆盖层，保存在 data/ui_settings.yaml，不改动 config.yaml。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.path = cfg.path("data/ui_settings.yaml")

    def load(self) -> dict:
        data = DEFAULT
        if self.path.exists():
            try:
                raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                raw = {}
            if isinstance(raw, dict):
                data = _merge(DEFAULT, raw)
        return data

    def save(self, data: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        merged = _merge(DEFAULT, data)
        self.path.write_text(
            yaml.safe_dump(merged, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        return merged

    def update(self, patch: dict) -> dict:
        return self.save(_merge(self.load(), patch or {}))

    def apply(self, cfg: Config) -> Config:
        data = self.load()
        profile = str(data.get("profile") or "").strip()
        if profile:
            try:
                cfg.apply_profile(profile)
            except ValueError:
                pass
        search = data.get("search") or {}
        if search.get("sources"):
            cfg.search.sources = [str(s) for s in search["sources"]]
        if search.get("count") is not None:
            cfg.search.count = int(search["count"])
        if search.get("max_pages") is not None:
            cfg.search.max_pages = int(search["max_pages"])
        if search.get("detail_budget") is not None:
            # 只在 ui_settings.yaml 显式写了才覆盖 config.yaml 的值
            cfg.search.detail_budget = max(1, int(search["detail_budget"]))
        face = data.get("face") or {}
        if face.get("model"):
            cfg.face.model = str(face["model"])
        if face.get("accept_threshold") is not None:
            cfg.face.accept_threshold = float(face["accept_threshold"])
        if face.get("review_threshold") is not None:
            cfg.face.review_threshold = float(face["review_threshold"])
        if face.get("min_face_px") is not None:
            cfg.face.min_face_px = max(0, int(face["min_face_px"]))
        if data.get("output_dir"):
            cfg.output_dir = str(data["output_dir"])
        if data.get("proxy") is not None:
            cfg.network.proxy = str(data["proxy"] or "")
        framing = data.get("framing") or {}
        if framing.get("model"):
            cfg.framing.model = str(framing["model"])
        if framing.get("mode"):
            cfg.framing.mode = str(framing["mode"])
        if framing.get("edge_cut_tolerance") is not None:
            cfg.framing.edge_cut_tolerance = max(0.0, min(0.2, float(framing["edge_cut_tolerance"])))
        if framing.get("min_person_ratio") is not None:
            cfg.framing.min_person_ratio = max(0.0, min(1.0, float(framing["min_person_ratio"])))
        for site, override in (data.get("auth") or {}).items():
            if not isinstance(override, dict):
                continue
            section = cfg.auth.setdefault(str(site), {})
            if override.get("enabled") is not None:
                section["enabled"] = bool(override["enabled"])
        excl = data.get("exclude") or {}
        cfg.exclude.dirs = [str(x) for x in (excl.get("dirs") or [])]
        cfg.exclude.files = [str(x) for x in (excl.get("files") or [])]
        if excl.get("phash_threshold") is not None:
            cfg.exclude.phash_threshold = max(0, min(16, int(excl["phash_threshold"])))
        mode = str(excl.get("scan_on_run") or "auto").lower()
        cfg.exclude.scan_on_run = mode if mode in ("auto", "always", "never") else "auto"

        for name, override in (data.get("people") or {}).items():
            if not isinstance(override, dict):
                continue
            pc = cfg.person_config(str(name))
            if pc is None:
                pc = PersonConfig(name=str(name))
                cfg.people.append(pc)
            if override.get("queries") is not None:
                pc.queries = [str(q) for q in override["queries"] if str(q).strip()]
            if override.get("count") is not None:
                pc.count = int(override["count"])
            if override.get("accept_threshold") is not None:
                pc.accept_threshold = float(override["accept_threshold"])
            if override.get("review_threshold") is not None:
                pc.review_threshold = float(override["review_threshold"])
            if override.get("sources"):
                pc.sources = [str(s) for s in override["sources"]]
        return cfg

    def describe(self) -> dict:
        data = self.load()
        return {
            "profile": data.get("profile") or self.cfg.profile,
            "face_model": data["face"].get("model") or "",
            "framing_model": data["framing"].get("model") or "",
            "sources": data["search"].get("sources") or [],
            "count": data["search"].get("count"),
            "max_pages": data["search"].get("max_pages"),
            "detail_budget": data["search"].get("detail_budget", self.cfg.search.detail_budget),
            "accept_threshold": data["face"].get("accept_threshold"),
            "review_threshold": data["face"].get("review_threshold"),
            "min_face_px": data["face"].get("min_face_px"),
            "framing_mode": data["framing"].get("mode"),
            "framing_edge_cut_tolerance": data["framing"].get("edge_cut_tolerance"),
            "framing_min_person_ratio": data["framing"].get("min_person_ratio"),
            "review_enabled": bool(data.get("review_enabled", True)),
            "output_dir": data.get("output_dir"),
            "proxy": data.get("proxy"),
            "exclude_dirs": data["exclude"].get("dirs") or [],
            "exclude_files": data["exclude"].get("files") or [],
            "exclude_phash_threshold": data["exclude"].get("phash_threshold"),
            "exclude_scan_on_run": data["exclude"].get("scan_on_run") or "auto",
            "sr": {
                "model": data["sr"].get("model"),
                "scale": data["sr"].get("scale"),
                "face_restore": data["sr"].get("face_restore"),
                "face_strength": data["sr"].get("face_strength"),
                "max_side": data["sr"].get("max_side"),
                "tile": data["sr"].get("tile"),
                "jpeg_quality": data["sr"].get("jpeg_quality"),
                "keep_original": True,
            },
            "auth": data.get("auth") or {},
            "people": data.get("people") or {},
            "path": str(self.path),
        }
