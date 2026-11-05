from __future__ import annotations

import html
import urllib.parse
from pathlib import Path

from .config import Config
from .index import ImageIndex

TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - 图片总览</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; padding:32px; background:#111318; color:#e8eaf0;
         font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif; }}
  h1 {{ font-size:22px; margin:0 0 6px; }}
  .sub {{ color:#8b93a7; font-size:13px; margin-bottom:24px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:16px; }}
  figure {{ margin:0; background:#1a1d25; border:1px solid #2a2f3d; border-radius:12px; overflow:hidden; }}
  img {{ width:100%; aspect-ratio:3/4; object-fit:cover; display:block; background:#0c0e12; }}
  figcaption {{ padding:10px 12px; font-size:12px; line-height:1.6; color:#aab2c5; }}
  figcaption b {{ color:#e8eaf0; font-weight:600; }}
  a {{ color:#7ea6ff; text-decoration:none; }}
  .empty {{ color:#8b93a7; }}
</style>
</head>
<body>
<h1>{title}</h1>
<div class="sub">成品 {count} 张 · 由 facefind 生成 · {stamp}</div>
{body}
</body>
</html>
"""


def build_overview(person: str, cfg: Config, index: ImageIndex | None = None,
                   stamp: str | None = None) -> Path:
    """把某人的成品生成缩略图总览 HTML（保存在 downloads/<人名>/overview.html）。"""
    idx = index or ImageIndex(cfg.index_path)
    rows = [r for r in idx.rows(person, ("saved",)) if r["file_path"] and Path(r["file_path"]).exists()]
    out_dir = cfg.output_path / person
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / "overview.html"

    figures: list[str] = []
    for row in rows:
        path = Path(row["file_path"])
        src = urllib.parse.quote(path.name)
        sim = f"{float(row['similarity']):.3f}" if row["similarity"] is not None else "-"
        clarity = f"{float(row['clarity'] or 0):.1f}"
        page = row["page_url"] or row["url"] or ""
        link = f'<a href="{html.escape(page)}" target="_blank" rel="noreferrer">来源</a>' if page else ""
        fig = (
            "<figure>"
            f'<a href="{html.escape(src)}" target="_blank"><img src="{html.escape(src)}" loading="lazy" alt=""></a>'
            "<figcaption>"
            f"<b>{html.escape(path.name)}</b><br>"
            f"{row['width']}×{row['height']} · sim {sim} · 清晰度 {clarity}<br>"
            f"{html.escape(row['source'] or '')} {link}"
            "</figcaption></figure>"
        )
        figures.append(fig)

    body = (
        f'<div class="grid">{"".join(figures)}</div>'
        if figures
        else '<p class="empty">该人员暂无成品图片。</p>'
    )
    from datetime import datetime

    content = TEMPLATE.format(
        title=html.escape(f"{person} · 图片总览"),
        count=len(rows),
        stamp=stamp or datetime.now().strftime("%Y-%m-%d %H:%M"),
        body=body,
    )
    dest.write_text(content, encoding="utf-8")
    return dest
