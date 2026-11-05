from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config
from .pipeline import RunOptions, run
from .settings import SettingsStore
from .sites import (SITE_LABELS, SITE_ORDER, LogWatcher, merge_site_stats,
                    new_site_stats)


class JobControl:
    """任务暂停/停止控制；pause() 后 pipeline 在检查点阻塞。"""

    def __init__(self) -> None:
        self._running = threading.Event()
        self._running.set()
        self._stop = threading.Event()

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    def stop(self) -> None:
        self._stop.set()
        self._running.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    def wait(self) -> None:
        """暂停时阻塞（200ms 轮询），停止后立即返回。"""
        while not self._running.wait(timeout=0.2):
            if self._stop.is_set():
                return


def _person_dict(p) -> dict:
    return {
        "person": p.person,
        "target": p.target,
        "saved_now": p.saved_now,
        "saved_added": p.saved_added,
        "review_added": p.review_added,
        "duplicates": p.duplicates,
        "replaced": p.replaced,
        "excluded": p.excluded,
        "rejected": p.rejected,
        "failed": p.failed,
        "blocked": p.blocked,
        "candidates": p.candidates,
        "pre_blocked": p.pre_blocked,
        "post_duplicates": p.post_duplicates,
        "elapsed": p.elapsed,
        "warnings": p.warnings,
        "sites": p.sites,
    }


class JobManager:
    """单任务后台运行器：供 UI 启动、暂停、停止、读进度与日志。"""

    def __init__(self, config_path: str | Path):
        self.config_path = Path(config_path).resolve()
        self.control: JobControl | None = None
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._logs: deque[dict] = deque(maxlen=3000)
        self._seq = 0
        self._watcher = LogWatcher()
        self._event_files: list[str] = []
        self._run_id = ""
        self._site_meta: dict[str, dict] = {}
        self.state: dict[str, Any] = {"state": "idle", "people": {}, "counts": {}, "logs_count": 0}

    # ---------- 查询 ----------
    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        with self._lock:
            data = dict(self.state)
            data["people"] = {k: dict(v) for k, v in self.state.get("people", {}).items()}
            data["busy"] = self.busy
            data["logs_count"] = len(self._logs)
            data["run_id"] = self._run_id
            snap = self._watcher.update(list(self._event_files))
            data["sites"] = self._build_sites(snap, data)
            data["sites_order"] = list(SITE_ORDER)
            data["progress"] = self._overall_progress(data)
            return data

    def logs(self, since: int = 0) -> dict:
        with self._lock:
            items = [x for x in self._logs if x["seq"] > since]
            return {"items": items, "last": self._logs[-1]["seq"] if self._logs else 0}

    # ---------- 控制 ----------
    def pause(self) -> dict:
        if self.control and self.busy:
            self.control.pause()
            self._set_state("paused")
        return self.status()

    def resume(self) -> dict:
        if self.control and self.busy:
            self.control.resume()
            self._set_state("running")
        return self.status()

    def stop(self) -> dict:
        if self.control and self.busy:
            self.control.stop()
            self._set_state("stopping")
        return self.status()

    # ---------- 启动 ----------
    def start(self, payload: dict) -> dict:
        with self._lock:
            if self.busy:
                raise RuntimeError("已有任务在运行，请先停止或等待完成")
            cfg = Config.load(self.config_path)
            SettingsStore(cfg).apply(cfg)

            sources = [s for s in (payload.get("sources") or cfg.search.sources) if s]
            if not sources:
                raise ValueError("请至少选择一个站点")
            requested = list(dict.fromkeys(sources))
            # 账号状态页可一键停用站点；停用后任务不再访问该站点
            skipped = [
                s for s in sources
                if s in ("weibo", "xiaohongshu") and not bool((cfg.auth.get(s) or {}).get("enabled"))
            ]
            sources = [s for s in sources if s not in skipped]
            if not sources:
                raise ValueError(
                    "所选站点均已在「账号状态」停用，请在 UI 中重新启用后再启动任务"
                )

            names = payload.get("names") or None
            min_face_px = payload.get("min_face_px")
            if min_face_px is not None:
                cfg.face.min_face_px = max(0, int(min_face_px))

            run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            site_meta: dict[str, dict] = {}
            for site in SITE_ORDER:
                section = (cfg.auth or {}).get(site) or {}
                enabled = True if site == "baidu" else bool(section.get("enabled"))
                logged_in = False
                if site != "baidu":
                    try:
                        from . import browser
                        logged_in = bool((browser.cached_status(site) or {}).get("browser_logged_in"))
                    except Exception:
                        logged_in = False
                site_meta[site] = {
                    "site": site,
                    "label": SITE_LABELS[site],
                    "enabled": enabled,
                    "selected": site in requested,
                    "disabled": site in skipped,
                    "logged_in": logged_in,
                }

            opt = RunOptions(
                names=names,
                count=payload.get("count"),
                accept_threshold=payload.get("accept_threshold"),
                review_threshold=payload.get("review_threshold"),
                review_enabled=payload.get("review_enabled"),
                mode=payload.get("mode"),
                sources=sources,
                queries=payload.get("queries") or None,
                out_dir=payload.get("out_dir") or None,
                use_face=payload.get("use_face"),
                max_pages=payload.get("max_pages"),
                min_width=payload.get("min_width"),
                min_height=payload.get("min_height"),
                proxy=payload.get("proxy") or None,
                exclude_dirs=payload.get("exclude_dirs") or [],
                exclude_files=payload.get("exclude_files") or [],
                exclude_phash_threshold=payload.get("exclude_phash_threshold"),
                exclude_rebuild=bool(payload.get("exclude_rebuild")),
                control=JobControl(),
                run_id=run_id,
                on_event_file=self._register_event_file,
                on_log=self._log,
                on_progress=self._progress,
            )
            self.control = opt.control
            self._logs.clear()
            self._seq = 0
            self._watcher = LogWatcher()
            self._event_files = []
            self._run_id = run_id
            self._site_meta = site_meta
            self.state = {
                "state": "running",
                "run_id": run_id,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "finished_at": "",
                "elapsed": 0.0,
                "person": "",
                "names": [],
                "people": {},
                "counts": {},
                "sources": sources,
                "requested_sources": requested,
                "skipped_sites": skipped,
                "site_meta": site_meta,
                "settings": {
                    "count": payload.get("count"),
                    "accept_threshold": payload.get("accept_threshold"),
                    "review_threshold": payload.get("review_threshold"),
                    "review_enabled": payload.get("review_enabled"),
                    "exclude_dirs": payload.get("exclude_dirs") or [],
                    "out_dir": payload.get("out_dir") or cfg.output_dir,
                },
                "error": "",
                "report": {},
            }
            self._thread = threading.Thread(target=self._run, args=(cfg, opt), daemon=True, name="facefind-job")
            self._thread.start()
            if skipped:
                self._log("已跳过停用站点：" + "、".join(skipped))
            return self.status()

    # ---------- 内部 ----------
    def _run(self, cfg: Config, opt: RunOptions) -> None:
        t0 = time.time()
        try:
            report = run(cfg, opt)
            with self._lock:
                self.state["report"] = {
                    "elapsed": report.elapsed,
                    "device": report.device,
                    "stopped": report.stopped,
                    "totals": report.totals,
                    "people": [_person_dict(p) for p in report.people],
                    "scan": report.scan.summary() if report.scan else "",
                }
                self.state["counts"] = report.totals
                for p in report.people:
                    slot = self.state["people"].setdefault(p.person, {})
                    slot.update({
                        "saved": p.saved_now, "target": p.target, "candidates": p.candidates,
                        "duplicate": p.duplicates, "excluded": p.excluded, "rejected": p.rejected,
                        "failed": p.failed, "saved_added": p.saved_added,
                        "review_added": p.review_added,
                        "pre_blocked": p.pre_blocked, "post_duplicates": p.post_duplicates,
                    })
                self._set_state("stopped" if report.stopped else "finished", locked=True)
        except BaseException as exc:  # 任务线程不能静默死掉
            self._log(f"任务异常：{exc.__class__.__name__}: {exc}")
            with self._lock:
                self.state["error"] = f"{exc.__class__.__name__}: {exc}"
                if isinstance(exc, ValueError):
                    self.state["state"] = "finished"
                    self.state["error"] = str(exc)
                else:
                    self._set_state("error", locked=True)
        finally:
            with self._lock:
                self.state["elapsed"] = round(time.time() - t0, 2)
                self.state["finished_at"] = datetime.now().isoformat(timespec="seconds")
                if self.state["state"] in ("running", "paused", "stopping"):
                    self._set_state("finished", locked=True)

    def _log(self, msg: str) -> None:
        with self._lock:
            self._seq += 1
            self._logs.append({
                "seq": self._seq,
                "time": datetime.now().strftime("%H:%M:%S"),
                "msg": msg,
            })

    def _progress(self, payload: dict) -> None:
        with self._lock:
            phase = payload.get("phase")
            person = payload.get("person")
            if person:
                self.state["person"] = person
                slot = self.state["people"].setdefault(person, {
                    "saved": 0, "target": payload.get("target") or 0, "candidates": 0,
                    "duplicate": 0, "excluded": 0, "rejected": 0, "failed": 0,
                })
                for key in ("saved", "target", "candidates", "duplicate", "excluded", "rejected",
                            "failed", "saved_added", "review_added"):
                    if payload.get(key) is not None:
                        slot[key] = payload[key]
            if phase == "start":
                self.state["counts"] = {}
                self.state["names"] = list(payload.get("people") or [])

    # ---------- 站点统计 ----------
    def note_manual(self, person: str, source: str, delta: int = 1) -> None:
        """手动收录计数：只影响当前运行页的「手动收录」列，不改变运行日志口径。"""
        if not person:
            return
        with self._lock:
            manual = self.state.setdefault("manual", {})
            by_site = manual.setdefault(person, {})
            by_site[source] = max(0, int(by_site.get(source, 0)) + int(delta))

    def _register_event_file(self, path: str) -> None:
        with self._lock:
            if path not in self._event_files:
                self._event_files.append(path)

    def _fallback_state(self, site: str, meta: dict, state: dict) -> tuple[str, str]:
        if state.get("state") == "idle":
            return "idle", "尚未启动任务"
        if meta.get("disabled"):
            return "disabled", "站点已在「账号状态」停用（未启用），本次未访问"
        if not meta.get("selected"):
            return "not_selected", "本次任务未选择该站点"
        if state.get("state") in ("finished", "stopped", "error"):
            return "skipped", "本次运行未产生该站点数据（可能提前达标或未执行）"
        return "pending", "等待开始"

    def _build_sites(self, snap: dict[str, dict[str, dict]], state: dict) -> list[dict]:
        meta_all = state.get("site_meta") or self._site_meta or {}
        names = list(state.get("names") or [])
        for person in (state.get("people") or {}):
            if person not in names:
                names.append(person)
        for person in (state.get("manual") or {}):
            if person not in names:
                names.append(person)
        out: list[dict] = []
        for site in SITE_ORDER:
            meta = meta_all.get(site) or {"label": SITE_LABELS[site], "enabled": True,
                                          "selected": True, "disabled": False}
            fallback = new_site_stats(site)
            fallback["label"] = SITE_LABELS[site]
            fallback["status"], fallback["status_reason"] = self._fallback_state(site, meta, state)
            rows: list[dict] = []
            real: list[dict] = []
            manual_all = state.get("manual") or {}
            manual_total = 0
            for person in names:
                st = (snap.get(person) or {}).get(site)
                man = int((manual_all.get(person) or {}).get(site, 0))
                if st:
                    row = {**st, "person": person, "manual": man}
                    real.append(st)
                else:
                    row = {**fallback, "person": person, "rejects": dict(fallback["rejects"]),
                           "manual": man}
                rows.append(row)
                manual_total += man
            if real:
                merged = merge_site_stats(real)
            else:
                merged = {**fallback, "rejects": dict(fallback["rejects"])}
                merged["status"], merged["status_reason"] = self._fallback_state(site, meta, state)
            merged["site"] = site
            merged["label"] = SITE_LABELS[site]
            merged["enabled"] = bool(meta.get("enabled", True))
            merged["selected"] = bool(meta.get("selected", True))
            merged["logged_in"] = bool(meta.get("logged_in", False))
            merged["manual"] = manual_total
            merged["people"] = rows
            out.append(merged)
        return out

    def _overall_progress(self, state: dict) -> dict:
        people = state.get("people") or {}
        saved = sum(int(v.get("saved") or 0) for v in people.values())
        target = sum(int(v.get("target") or 0) for v in people.values())
        names = state.get("names") or []
        count = (state.get("settings") or {}).get("count")
        if count and names:
            target = max(target, int(count) * len(names))
        pct = round(min(100.0, saved / target * 100), 1) if target else 0.0
        return {"saved": saved, "target": target, "pct": pct, "people": len(names)}

    def _set_state(self, state: str, locked: bool = False) -> None:
        if not locked:
            with self._lock:
                self.state["state"] = state
                return
        self.state["state"] = state
