from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

SITE_ORDER = ("baidu", "weibo", "xiaohongshu")
SITE_LABELS = {"baidu": "百度图片", "weibo": "微博", "xiaohongshu": "小红书"}
REJECT_LABELS = {
    "framing": "构图不合格",
    "low_similarity": "相似度低",
    "face_too_small": "人脸过小/无脸",
    "blocked": "屏蔽",
    "duplicate": "重复",
    "excluded": "排除目录",
    "other": "其他",
}

# 站点状态优先级：越靠前越需要用户关注
STATUS_PRIORITY = ("auth", "risk", "error", "running", "stopped", "done", "skipped", "pending", "idle")


def new_site_stats(site: str) -> dict[str, Any]:
    return {
        "site": site,
        "label": SITE_LABELS.get(site, site),
        "status": "idle",
        "status_reason": "",
        "candidates": 0,
        "processed": 0,
        "downloaded": 0,
        "saved": 0,
        "review": 0,
        "rejects": {k: 0 for k in REJECT_LABELS},
        "rejected": 0,
        "failed": 0,
        "pre_blocked": 0,
        "post_duplicates": 0,
        "page": 0,
        "pages": 0,
        "unit": "",
        "elapsed": 0.0,
        "first_time": "",
        "last_time": "",
    }


def _seconds_between(first: str, last: str) -> float:
    if not first or not last:
        return 0.0
    try:
        a = datetime.fromisoformat(first)
        b = datetime.fromisoformat(last)
        return max(0.0, round((b - a).total_seconds(), 2))
    except ValueError:
        return 0.0


def _reject_bucket(rec: dict) -> str:
    reason = str(rec.get("reason") or "")
    stage = str(rec.get("dedupe_stage") or "")
    if reason == "framing" or stage.startswith("framing_"):
        return "framing"
    if reason in ("low_similarity", "below_accept"):
        return "low_similarity"
    if reason in ("face_too_small", "no_face") or stage in ("face_too_small", "no_face"):
        return "face_too_small"
    return "other"


class SiteAggregator:
    """把 run_*.jsonl 决策事件聚合成「人员 × 站点」统计。"""

    def __init__(self) -> None:
        self._people: dict[str, dict[str, dict]] = {}

    def _slot(self, person: str, site: str) -> dict:
        sites = self._people.setdefault(person, {})
        if site not in sites:
            sites[site] = new_site_stats(site)
        return sites[site]

    def feed(self, rec: dict) -> None:
        person = str(rec.get("person") or "")
        site = str(rec.get("source") or "")
        if not person or site not in SITE_LABELS:
            return
        st = self._slot(person, site)
        t = str(rec.get("time") or "")
        if t:
            if not st["first_time"]:
                st["first_time"] = t
            st["last_time"] = t
            st["elapsed"] = _seconds_between(st["first_time"], st["last_time"])

        typ = str(rec.get("type") or "image")
        if typ == "search":
            st["candidates"] += int(rec.get("count") or 0)
            page = int(rec.get("page") or 0)
            if page:
                st["page"] = max(st["page"], page)
            total = int(rec.get("max_pages") or 0)
            if total:
                st["pages"] = max(st["pages"], total)
            if st["status"] in ("idle", "pending"):
                st["status"] = "running"
            return
        if typ == "search_page":
            # 搜索结束时如实回报的"实际完成页/轮次 + 本次上限"
            unit = str(rec.get("unit") or "")
            if unit:
                st["unit"] = unit
            page = int(rec.get("page") or 0)
            if page:
                st["page"] = max(st["page"], page)
            total = int(rec.get("max_pages") or 0)
            if total:
                st["pages"] = max(st["pages"], total)
            return
        if typ == "site":
            state = str(rec.get("state") or "running")
            st["status"] = state
            reason = str(rec.get("reason") or "")
            if reason:
                st["status_reason"] = reason
            return
        if typ in ("summary", "site_summary", "pre_download_block", "post_download_duplicate"):
            return

        status = str(rec.get("status") or "")
        stage = str(rec.get("dedupe_stage") or "")
        st["processed"] += 1
        if status == "failed":
            st["failed"] += 1
        elif status == "saved":
            st["saved"] += 1
            st["downloaded"] += 1
        elif status == "review":
            st["review"] += 1
            st["downloaded"] += 1
        elif status == "blocked":
            st["rejects"]["blocked"] += 1
            st["rejected"] += 1
            if stage == "blocked_url" or rec.get("dedupe_phase") == "pre_download":
                st["pre_blocked"] += 1
            else:
                st["downloaded"] += 1
        elif status == "duplicate":
            st["rejects"]["duplicate"] += 1
            st["rejected"] += 1
            if rec.get("dedupe_phase") == "pre_download" or stage in ("url_seen", "url_variant"):
                st["pre_blocked"] += 1
            else:
                st["post_duplicates"] += 1
                st["downloaded"] += 1
        elif status == "excluded":
            st["rejects"]["excluded"] += 1
            st["rejected"] += 1
            st["downloaded"] += 1
        elif status == "rejected":
            st["rejects"][_reject_bucket(rec)] += 1
            st["rejected"] += 1
            st["downloaded"] += 1
        if st["status"] in ("idle",):
            st["status"] = "running"

    def snapshot(self) -> dict[str, dict[str, dict]]:
        out: dict[str, dict[str, dict]] = {}
        for person, sites in self._people.items():
            out[person] = {}
            for site, st in sites.items():
                item = dict(st)
                item["rejects"] = dict(st["rejects"])
                out[person][site] = item
        return out

    def person_sites(self, person: str) -> dict[str, dict]:
        return self.snapshot().get(person, {})


def _copy(st: dict) -> dict:
    item = dict(st)
    item["rejects"] = dict(st.get("rejects") or {})
    return item


def combine_status(stats: Iterable[dict]) -> tuple[str, str]:
    items = [s for s in stats if s]
    if not items:
        return "pending", ""
    seen: list[str] = [str(s.get("status") or "") for s in items]
    for bad in STATUS_PRIORITY:
        if bad in seen:
            reasons = list(dict.fromkeys(
                str(s.get("status_reason") or "") for s in items
                if str(s.get("status") or "") == bad and s.get("status_reason")
            ))
            return bad, "；".join(reasons)
    return seen[-1] or "pending", ""


def merge_site_stats(stats: Iterable[dict]) -> dict:
    items = [s for s in stats if s]
    if not items:
        return new_site_stats("")
    site = str(items[0].get("site") or "")
    merged = new_site_stats(site)
    for st in items:
        for key in ("candidates", "processed", "downloaded", "saved", "review", "rejected", "failed",
                    "pre_blocked", "post_duplicates"):
            merged[key] += int(st.get(key) or 0)
        for k in REJECT_LABELS:
            merged["rejects"][k] += int((st.get("rejects") or {}).get(k) or 0)
        merged["page"] = max(merged["page"], int(st.get("page") or 0))
        merged["pages"] = max(merged["pages"], int(st.get("pages") or 0))
        if st.get("unit"):
            merged["unit"] = str(st["unit"])
        merged["elapsed"] = round(merged["elapsed"] + float(st.get("elapsed") or 0), 2)
        first = str(st.get("first_time") or "")
        last = str(st.get("last_time") or "")
        if first and (not merged["first_time"] or first < merged["first_time"]):
            merged["first_time"] = first
        if last and last > merged["last_time"]:
            merged["last_time"] = last
    merged["status"], merged["status_reason"] = combine_status(items)
    return merged


class LogWatcher:
    """增量读取事件日志（按字节 offset），支持运行中实时聚合。"""

    def __init__(self) -> None:
        self.agg = SiteAggregator()
        self._state: dict[str, dict[str, Any]] = {}

    def update(self, paths: Iterable[str]) -> dict[str, dict[str, dict]]:
        for raw in paths:
            self._read_one(Path(raw))
        return self.agg.snapshot()

    def _read_one(self, path: Path) -> None:
        key = str(path)
        slot = self._state.setdefault(key, {"offset": 0, "buf": ""})
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size < slot["offset"]:  # 文件被截断/重写
            slot["offset"] = 0
            slot["buf"] = ""
        if size == slot["offset"]:
            return
        try:
            with path.open("rb") as fh:
                fh.seek(slot["offset"])
                chunk = fh.read()
        except OSError:
            return
        slot["offset"] += len(chunk)
        text = slot["buf"] + chunk.decode("utf-8", errors="replace")
        lines = text.split("\n")
        slot["buf"] = lines.pop() if not text.endswith("\n") else ""
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                self.agg.feed(rec)

    def snapshot(self) -> dict[str, dict[str, dict]]:
        return self.agg.snapshot()


def collect_run_history(log_dir: Path, limit: int = 20, max_files: int = 80) -> list[dict]:
    """读取日志里的 site_summary 摘要行，按 run_id 分组返回最近若干次运行。"""
    log_dir = Path(log_dir)
    if not log_dir.is_dir():
        return []
    files = sorted(
        (p for p in log_dir.glob("run_*.jsonl") if p.is_file()),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )[:max_files]
    runs: dict[str, dict] = {}
    for path in reversed(files):  # 旧到新，便于 started_at 取最早
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "site_summary" not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(rec, dict) or rec.get("type") != "site_summary":
                        continue
                    run_id = str(rec.get("run_id") or path.name.split("_")[1])
                    person = str(rec.get("person") or "")
                    t = str(rec.get("time") or "")
                    run = runs.setdefault(run_id, {
                        "run_id": run_id, "started_at": t, "finished_at": t,
                        "elapsed": 0.0, "people": [], "sites": {}, "sources": set(),
                    })
                    run["sources"].update(str(s) for s in (rec.get("sources") or []))
                    if t and (not run["started_at"] or t < run["started_at"]):
                        run["started_at"] = t
                    if t > run["finished_at"]:
                        run["finished_at"] = t
                    run["elapsed"] = round(run["elapsed"] + float(rec.get("elapsed") or 0), 2)
                    sites = rec.get("sites") or {}
                    if person and person not in run["people"]:
                        run["people"].append(person)
                    for site, st in sites.items():
                        if site not in SITE_LABELS or not isinstance(st, dict):
                            continue
                        merged = run["sites"].get(site)
                        run["sites"][site] = merge_site_stats([merged, st]) if merged else _copy(st)
        except OSError:
            continue
    out: list[dict] = []
    for run in sorted(runs.values(), key=lambda r: r["finished_at"], reverse=True):
        sites: list[dict] = []
        for site in SITE_ORDER:
            if site in run["sites"]:
                st = _copy(run["sites"][site])
                st["status"] = st["status"] if st["status"] != "pending" else "done"
            else:
                st = new_site_stats(site)
                if site in run["sources"]:
                    st["status"], st["status_reason"] = "skipped", "本次运行未产生该站点数据"
                else:
                    st["status"], st["status_reason"] = "not_selected", "本次任务未选择该站点"
            sites.append(st)
        out.append({
            "run_id": run["run_id"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "elapsed": run["elapsed"],
            "people": run["people"],
            "sites": sites,
        })
        if len(out) >= max(1, limit):
            break
    return out
