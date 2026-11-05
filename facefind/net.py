from __future__ import annotations

import random
import threading
import time

import requests

from .config import NetworkConfig, DEFAULT_UA


class DownloadError(RuntimeError):
    pass


class HttpClient:
    """带重试、退避、随机延迟限速的 HTTP 客户端（每个站点一个实例）。"""

    def __init__(self, cfg: NetworkConfig, site: str = "baidu"):
        self.cfg = cfg
        self.site = site
        self.rate = cfg.site(site)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": cfg.user_agent or DEFAULT_UA,
                "Accept": "text/plain, */*; q=0.01",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        if cfg.proxies:
            self.session.proxies.update(cfg.proxies)
        self._lock = threading.Lock()
        self._last_request = 0.0

    def _throttle(self) -> None:
        lo = max(0, self.rate.delay_min_ms) / 1000.0
        hi = max(lo, self.rate.delay_max_ms) / 1000.0
        with self._lock:
            now = time.monotonic()
            wait = self._last_request + random.uniform(lo, hi) - now
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def get(self, url: str, *, params: dict | None = None, referer: str | None = None,
            timeout: float | None = None) -> requests.Response:
        headers = {"Referer": referer} if referer else {}
        last_exc: Exception | None = None
        for attempt in range(self.cfg.retries + 1):
            self._throttle()
            try:
                resp = self.session.get(
                    url, params=params, headers=headers, timeout=timeout or self.cfg.timeout
                )
                if resp.status_code in (403, 429) or resp.status_code >= 500:
                    raise DownloadError(f"HTTP {resp.status_code} for {url}")
                resp.raise_for_status()
                return resp
            except Exception as exc:  # 含网络错误 / 非 200
                last_exc = exc
                if attempt < self.cfg.retries:
                    time.sleep(self.cfg.backoff_seconds * (2 ** attempt) + random.uniform(0, 0.4))
        raise DownloadError(str(last_exc) if last_exc else f"failed: {url}")

    def get_bytes(self, url: str, *, referer: str | None = None) -> bytes:
        resp = self.get(url, referer=referer)
        if not resp.content:
            raise DownloadError(f"empty body: {url}")
        return resp.content
