from __future__ import annotations

from ..config import Config
from .base import AuthRequired, BaseSource, SourceError
from .baidu import BaiduSource
from .weibo import WeiboSource
from .xiaohongshu import XiaohongshuSource


def make_source(cfg: Config, name: str) -> BaseSource:
    """按名称构建站点适配器；登录态相关参数来自 config.yaml 的 auth 段。"""
    auth = cfg.auth or {}
    if name == "baidu":
        return BaiduSource()
    if name == "weibo":
        section = auth.get("weibo") or {}
        return WeiboSource(
            cfg=cfg, enabled=bool(section.get("enabled")), cookie=str(section.get("cookie") or "")
        )
    if name == "xiaohongshu":
        section = auth.get("xiaohongshu") or {}
        return XiaohongshuSource(
            cfg=cfg,
            enabled=bool(section.get("enabled")),
            cookie=str(section.get("cookie") or ""),
            profile_dir=str(section.get("profile_dir") or ""),
        )
    raise SourceError(f"未知站点：{name}（可选：baidu / weibo / xiaohongshu）")


__all__ = [
    "AuthRequired", "BaseSource", "SourceError", "make_source",
    "BaiduSource", "WeiboSource", "XiaohongshuSource",
]
