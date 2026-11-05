from __future__ import annotations

import json
import re
import urllib.parse

from ..models import SearchItem
from ..net import HttpClient
from .base import BaseSource, SourceError


def _parse_json(text: str) -> dict:
    text = text.strip().replace("\x00", "")
    for candidate in (text, re.sub(r",\s*([}\]])", r"\1", text)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise SourceError("百度返回内容不是合法 JSON（可能触发了反爬）")


def _item_from_dict(d: dict, query: str, page: int = 1) -> SearchItem | None:
    urls: list[str] = []
    page_url = ""
    for rep in d.get("replaceUrl") or []:
        if isinstance(rep, dict):
            u = rep.get("ObjURL") or rep.get("ObjUrl")
            if u and u.startswith("http"):
                urls.append(u)
            page_url = page_url or (rep.get("FromURL") or rep.get("FromUrl") or "")
    for key in ("middleURL", "thumbURL", "hoverURL"):
        u = d.get(key)
        if u and u.startswith("http"):
            urls.append(u)

    seen: set[str] = set()
    unique_urls = [u for u in urls if not (u in seen or seen.add(u))]
    if not unique_urls:
        return None
    return SearchItem(
        url=unique_urls[0],
        fallback_urls=unique_urls[1:],
        source="baidu",
        query=query,
        title=d.get("fromPageTitleEnc") or d.get("fromPageTitle") or "",
        width=int(d.get("width") or 0),
        height=int(d.get("height") or 0),
        page_url=page_url,
        page=page,
    )


class BaiduSource(BaseSource):
    name = "baidu"
    display_name = "百度图片"
    API = "https://image.baidu.com/search/acjson"
    REFERER = "https://image.baidu.com/"

    def search(self, client: HttpClient, query: str, *, page_size: int = 30,
               max_pages: int = 8) -> list[SearchItem]:
        out: list[SearchItem] = []
        for page in range(max_pages):
            pn = page * page_size
            params = {
                "tn": "resultjson_com", "logid": "1234567890", "ipn": "rj",
                "ct": "201326592", "is": "", "fp": "result", "fr": "", "word": query,
                "queryWord": query, "cl": "2", "lm": "-1", "ie": "utf-8", "oe": "utf-8",
                "adpicid": "", "st": "-1", "z": "", "ic": "", "hd": "", "latest": "",
                "copyright": "", "s": "", "se": "", "tab": "", "width": "", "height": "",
                "face": "0", "istype": "2", "qc": "", "nc": "1", "expermode": "", "nojc": "",
                "isAsync": "", "pn": str(pn), "rn": str(page_size), "gsm": format(pn + 30, "x"),
            }
            resp = client.get(self.API, params=params, referer=self.REFERER)
            data = _parse_json(resp.text)
            items = data.get("data") or []
            found = 0
            for d in items:
                if not isinstance(d, dict):
                    continue
                item = _item_from_dict(d, query, page + 1)
                if item is not None:
                    found += 1
                    out.append(item)
            if found == 0:
                break
        self.last_page_done = page + 1 if out else 0
        return out
