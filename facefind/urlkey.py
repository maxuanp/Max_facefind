# -*- coding: utf-8 -*-
"""URL 规范化：把同一张图的不同尺寸/追踪参数变体归一到同一个 key。

只做「同一个资源」的收敛，不做激进改写：
- 小红书 CDN：图片 id（如 1040g...）是稳定标识，前后的时间目录/哈希目录、
  `!nd_dft_wlteh_webp_3` 之类后缀、`imageView2` 尺寸参数、sign/t 全部忽略。
- 微博图床：`/large/` `/orlarge/` `/mw690/` `/thumbnail/` 等尺寸段忽略，保留文件名。
- 百度图片：`image_search` 的 `src` 参数解码后递归归一，忽略 refer/app/size/q。
- 其它：只去掉已知追踪参数与 `!` 后缀，保留路径与其余参数。
"""
from __future__ import annotations

import re
import urllib.parse

# 只删这些明确的追踪/来源参数；其余参数保留，避免把不同资源误判成同一张
TRACKING_PARAMS = {
    "from", "refer", "referer", "referrer", "sign", "sc", "ap", "t", "timestamp", "_",
    "traceid", "trace_id", "spm", "share", "share_source", "is_auto", "wfr", "fm",
    "source", "xsec_token", "xsec_source", "utm_source", "utm_medium", "utm_campaign",
    "utm_term", "utm_content", "app", "size", "q", "tags", "tag",
}

# 微博图床的尺寸目录（同一文件名的不同尺寸 = 同一张图）
SINA_SIZE_DIRS = {
    "large", "orlarge", "mw690", "mw1024", "bmiddle", "small", "thumbnail",
    "thumb150", "thumb180", "woriginal", "square", "original", "mw800",
}

_XHS_TOKEN = re.compile(r"(1\d{3}g[0-9a-z]+)", re.IGNORECASE)
_XHS_NOTE = re.compile(r"/(?:explore|search_result|discovery/item)/([0-9a-fA-F]{16,40})")


def note_key(url: str) -> str:
    """笔记页 URL 的稳定标识（小红书 note id）；非笔记页返回空串。"""
    m = _XHS_NOTE.search(url or "")
    return ("xhs-note://" + m.group(1).lower()) if m else ""


def _split(url: str) -> urllib.parse.SplitResult:
    raw = (url or "").strip()
    if not raw:
        return urllib.parse.urlsplit("")
    if "://" not in raw:
        raw = "https://" + raw
    return urllib.parse.urlsplit(raw)


def canonical_url(url: str) -> str:
    """返回图片 URL 的归一化 key；空 URL 返回空串。"""
    parts = _split(url)
    if not parts.netloc:
        return ""
    host = parts.netloc.lower().split(":")[0]
    path = parts.path
    query = parts.query

    # 小红书 CDN：优先取图片 id
    if host.endswith("xhscdn.com"):
        seg = path.rsplit("/", 1)[-1].split("!")[0]
        m = _XHS_TOKEN.search(seg) or _XHS_TOKEN.search(path)
        return "xhs://" + (m.group(1).lower() if m else seg.lower())

    # 微博图床：去掉尺寸目录段
    if host.endswith("sinaimg.cn"):
        segs = [s for s in path.split("/") if s]
        if segs and (segs[0].lower() in SINA_SIZE_DIRS or segs[0].lower().startswith("crop.")):
            segs = segs[1:]
        return "weibo://" + "/".join(segs).lower()

    # 百度图片代理：src 就是原始图地址（有时整段在 path 里，不是标准 query）
    if "baidu.com" in host and "image_search" in path:
        m = re.search(r"[?&/]src=([^&]+)", url or "", re.IGNORECASE)
        if m:
            inner = urllib.parse.unquote(m.group(1))
            nested = canonical_url(inner)
            if nested:
                return nested
            return "baidu://" + inner.lower()

    # 通用：去 fragment、去追踪参数、去 "!" 变体后缀
    segs = [s.split("!")[0] for s in path.split("/") if s]
    kept = []
    params = urllib.parse.parse_qsl(query, keep_blank_values=True)
    for k, v in params:
        if k.lower() in TRACKING_PARAMS:
            continue
        kept.append((k.lower(), v))
    kept.sort()
    tail = urllib.parse.urlencode(kept)
    return f"{host}/{('/'.join(segs)).lower()}" + (f"?{tail}" if tail else "")
