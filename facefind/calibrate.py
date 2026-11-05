from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from .config import Config
from .index import ImageIndex


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    pos = (len(values) - 1) * q / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    frac = pos - lo
    return values[lo] * (1 - frac) + values[hi] * frac


def collect(person: str, cfg: Config, limit: int = 100) -> dict:
    """导出最近 N 张有相似度的候选图并给出建议阈值区间。

    人工标注来源：review 页的「收录/丢弃」决策（saved / discarded）。
    """
    index = ImageIndex(cfg.index_path)
    rows = index.rows(person)
    scored = [r for r in rows if r["similarity"] is not None]
    scored.sort(key=lambda r: int(r["id"]), reverse=True)
    scored = scored[: max(1, limit)]

    records: list[dict] = []
    positives: list[float] = []
    negatives: list[float] = []
    for r in scored:
        status = r["status"] or ""
        sim = float(r["similarity"])
        if status == "saved":
            label = "positive"
            positives.append(sim)
        elif status == "discarded":
            label = "negative"
            negatives.append(sim)
        elif status == "review":
            label = "unlabeled"
        elif status == "rejected" and (r["dedupe_stage"] or "") in ("low_similarity", "below_accept"):
            label = "auto_negative"
            negatives.append(sim)
        else:
            label = "other"
        records.append({
            "time": r["created_at"],
            "source": r["source"],
            "query": r["query"],
            "url": r["url"],
            "page_url": r["page_url"],
            "similarity": round(sim, 4),
            "status": status,
            "label": label,
            "clarity": r["clarity"],
            "width": r["width"],
            "height": r["height"],
            "n_faces": r["n_faces"],
        })

    current_accept = cfg.face.accept_threshold
    current_review = cfg.face.review_threshold
    note = ""
    if len(positives) >= 3:
        suggested_accept = round(_pct(positives, 10), 2)
    else:
        suggested_accept = round(current_accept, 2)
        note = "正样本不足 3 张，暂用当前 A 值；建议先人工收录一些 review 图。"
    if len(negatives) >= 3:
        suggested_review = round(min(_pct(negatives, 90), suggested_accept - 0.02), 2)
    else:
        suggested_review = round(max(0.0, min(suggested_accept - 0.15, current_review)), 2)
    suggested_review = max(0.0, suggested_review)
    if suggested_review > suggested_accept:
        suggested_review = round(max(0.0, suggested_accept - 0.02), 2)

    suggestion = {
        "accept_threshold": suggested_accept,
        "review_threshold": suggested_review,
        "positive_n": len(positives),
        "negative_n": len(negatives),
        "note": note,
        "current": {"accept": current_accept, "review": current_review},
    }

    out_dir = cfg.path("data/reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join("_" if c in '<>:"/\\|?*' else c for c in person) or "unnamed"
    csv_path = out_dir / f"calibrate_{safe}_{datetime.now():%Y%m%d_%H%M%S}.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(records[0].keys()) if records else [
            "time", "source", "query", "url", "page_url", "similarity", "status", "label",
            "clarity", "width", "height", "n_faces",
        ])
        writer.writeheader()
        for rec in records:
            writer.writerow(rec)
    return {"person": person, "records": records, "suggestion": suggestion, "csv_path": str(csv_path)}
