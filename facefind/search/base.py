from __future__ import annotations

from ..models import SearchItem


class SourceError(RuntimeError):
    """站点不可用（未配置登录态、被封禁、结构变更等）。"""


class AuthRequired(SourceError):
    """该站点需要用户提供登录态。"""


class BaseSource:
    name = ""
    display_name = ""
    requires_login = False
    # 进度单位：page=真实页码；round=滚动轮次（小红书这类无限滚动站点）
    page_unit = "page"
    # 最近一次 search() 实际完成的页数/轮次（由各站点实现回填，供 UI 如实展示）
    last_page_done = 0
    # 最近一次 search() 的附加统计（如滚动轮次、唯一笔记数、停止原因）
    search_stats: dict = {}
    # 最近一次 expand_notes() 得到的「笔记 → 图片清单」（仅支持详情页展开的站点）
    last_note_media: dict = {}

    def page_cap(self, max_pages: int) -> int:
        """本次 search 的页数/轮次上限（默认就是 max_pages）。"""
        return max(1, int(max_pages))

    def search(self, client, query: str, *, page_size: int = 30, max_pages: int = 8) -> list[SearchItem]:
        """返回**列表**（契约）：候选数量由 page_size × max_pages 封顶，调用方可以切片/取长度。

        不要返回生成器——上层会按 count 缺口做 `items[:budget]` 与 `len(notes)`。
        """
        raise NotImplementedError
