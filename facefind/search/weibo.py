from __future__ import annotations

from collections.abc import Iterator

from ..config import Config
from .. import browser, secrets
from ..models import SearchItem
from ..net import HttpClient
from .base import AuthRequired, BaseSource, SourceError


class WeiboSource(BaseSource):
    """微博图片搜索：优先复用 Playwright 浏览器会话，失败时回退 Cookie API。

    Cookie 来源（只读，不写入索引/日志）：
      - config.yaml auth.weibo.cookie
      - secrets/weibo.txt（一行 `name=value; ...`，含 SUB/SUBP/SSOLoginState）
    """

    name = "weibo"
    display_name = "微博"
    requires_login = True
    API = "https://m.weibo.cn/api/container/getIndex"

    def __init__(self, cfg: Config | None = None, enabled: bool = False, cookie: str = ""):
        self.cfg = cfg
        self.enabled = enabled
        self.cookie = cookie.strip()
        if not self.cookie and cfg is not None:
            self.cookie = secrets.cookie_header("weibo", cfg)

    def search(self, client: HttpClient, query: str, *, page_size: int = 30,
               max_pages: int = 5) -> list[SearchItem]:
        if not self.enabled:
            raise AuthRequired(
                "微博未开启：请在任务配置中勾选微博站点（或设置 auth.weibo.enabled=true）。"
            )
        if self.cfg is not None and browser.playwright_installed():
            try:
                return list(browser.search("weibo", self.cfg, query, max_pages=max_pages, page_size=page_size))
            except browser.RiskControlError as exc:
                raise SourceError(str(exc)) from exc
            except browser.LoginRequiredError:
                if not self.cookie:
                    raise AuthRequired(
                        "微博浏览器会话未登录，且未找到 secrets/weibo.txt。"
                        "请点击“打开登录窗口”登录，或提供包含 SUB 的 Cookie 文件。"
                    ) from None
            except browser.BrowserError as exc:
                if not self.cookie:
                    raise AuthRequired(
                        f"微博浏览器路径不可用（{exc}），且未找到 Cookie 文件 secrets/weibo.txt。"
                    ) from exc

        if not self.cookie:
            raise AuthRequired(
                "微博需要登录态：请点击“打开登录窗口”登录一次，"
                "或在 secrets/weibo.txt 写入一行 `name=value; ...`（需包含 SUB）。"
            )
        client.session.headers["Cookie"] = self.cookie
        container = f"100103type=1&q={query}"
        out: list[SearchItem] = []
        for page in range(1, max_pages + 1):
            params = {"containerid": container, "page_type": "searchall", "page": str(page)}
            resp = client.get(self.API, params=params, referer="https://m.weibo.cn/")
            try:
                data = resp.json()
            except Exception as exc:
                raise SourceError(f"微博返回不是 JSON：{exc}") from exc
            if data.get("ok") != 1:
                raise AuthRequired("微博接口拒绝访问：登录态失效或触发风控，请重新登录后更新 Cookie。")
            cards = (data.get("data") or {}).get("cards") or []
            if not cards:
                break
            for card in cards:
                for mblog in _iter_mblogs(card):
                    for pic in mblog.get("pics") or []:
                        url = (pic.get("large") or {}).get("url") or pic.get("url")
                        if not url:
                            continue
                        out.append(SearchItem(
                            url=url,
                            source="weibo",
                            query=query,
                            title=(mblog.get("text") or "")[:80],
                            page_url=f"https://m.weibo.cn/detail/{mblog.get('idstr') or mblog.get('id') or ''}",
                            page=page,
                        ))
        self.last_page_done = page
        return out


def _iter_mblogs(card: dict) -> Iterator[dict]:
    if card.get("mblog"):
        yield card["mblog"]
    for sub in card.get("card_group") or []:
        if sub.get("mblog"):
            yield sub["mblog"]
