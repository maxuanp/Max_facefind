from __future__ import annotations

from ..config import Config
from .. import browser
from ..models import SearchItem
from ..net import HttpClient
from .base import AuthRequired, BaseSource, SourceError


class XiaohongshuSource(BaseSource):
    """小红书图片搜索：Playwright 持久化浏览器会话（不拼 X-s/X-t 签名接口）。

    登录方式：
      1) UI / `python -m facefind login xiaohongshu` 打开登录窗口，手动扫码一次；
      2) 或提供 secrets/xhs.txt（一行 `name=value; ...`，含 a1、web_session）。
    遇到验证码/滑块/风控：停止该站点任务，不做绕过。
    """

    name = "xiaohongshu"
    display_name = "小红书"
    requires_login = True
    page_unit = "round"          # 小红书没有页码，只有滚动轮次

    def page_cap(self, max_pages: int) -> int:
        return browser.xhs_scroll_cap(max_pages)

    def __init__(self, cfg: Config | None = None, enabled: bool = False,
                 cookie: str = "", profile_dir: str = ""):
        self.cfg = cfg
        self.enabled = enabled
        self.cookie = cookie.strip()
        self.profile_dir = profile_dir

    def search(self, client: HttpClient, query: str, *, page_size: int = 30,
               max_pages: int = 3) -> list[SearchItem]:
        if not self.enabled:
            raise AuthRequired(
                "小红书未开启：请在任务配置中勾选小红书站点（或设置 auth.xiaohongshu.enabled=true）。"
            )
        if self.cfg is None:
            raise SourceError("小红书浏览器搜索缺少运行配置")
        try:
            items = browser.search(
                "xiaohongshu", self.cfg, query, max_pages=max_pages, page_size=page_size
            )
        except browser.RiskControlError as exc:
            raise SourceError(str(exc)) from exc
        except browser.LoginRequiredError as exc:
            hint = "请在“账号状态”里点击“打开登录窗口”，扫码登录一次后重试。"
            if self.cookie:
                hint = "浏览器会话未登录；可在 secrets/xhs.txt 提供 cookie 后重试。"
            raise AuthRequired(f"{exc} {hint}") from exc
        except browser.BrowserError as exc:
            raise SourceError(f"小红书浏览器路径不可用：{exc}") from exc
        self.last_page_done = int(browser.LAST_XHS_SCROLL.get("rounds") or 0)
        self.search_stats = dict(browser.LAST_XHS_SCROLL)
        return list(items)

    def expand_notes(self, notes: list[SearchItem], budget: int) -> tuple[list[SearchItem], dict[str, int]]:
        """只为准备下载的笔记打开详情页取原图；失败回退封面并标记 fallback_cover。"""
        if self.cfg is None:
            raise SourceError("小红书详情页展开缺少运行配置")
        try:
            expanded, stats = browser.expand_xhs_notes(self.cfg, notes, budget)
        except browser.RiskControlError as exc:
            raise SourceError(str(exc)) from exc
        except browser.LoginRequiredError as exc:
            raise AuthRequired(
                f"{exc} 请在“账号状态”里点击“打开登录窗口”，扫码登录一次后重试。"
            ) from exc
        except browser.BrowserError as exc:
            raise SourceError(f"小红书详情页不可用：{exc}") from exc
        self.last_note_media = dict(browser.LAST_XHS_NOTE_MEDIA)
        return expanded, stats
