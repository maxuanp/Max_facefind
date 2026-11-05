from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import Config
from .exclude import IMAGE_SUFFIXES, ExcludeIndex, build_skip, iter_images, norm_path, skip_roots
from .index import ImageIndex

logger = logging.getLogger(__name__)

# 事件去抖后再等这么久确认文件大小/时间戳稳定（防止索引半成品）
STABILITY_MS = 200


def _norm(path: str | Path) -> str:
    return norm_path(path)


# ---------------- 进程内当前活跃监听器（供任务前扫描决策查询） ----------------
_ACTIVE: "ExcludeWatcher | None" = None
_ACTIVE_LOCK = threading.Lock()


def set_active(watcher: "ExcludeWatcher") -> None:
    global _ACTIVE
    with _ACTIVE_LOCK:
        _ACTIVE = watcher


def clear_active(watcher: "ExcludeWatcher") -> None:
    global _ACTIVE
    with _ACTIVE_LOCK:
        if _ACTIVE is watcher:
            _ACTIVE = None


def get_active() -> "ExcludeWatcher | None":
    with _ACTIVE_LOCK:
        return _ACTIVE


def active_health(cfg: Config, dirs: list[str], files: list[str]) -> tuple[bool, str]:
    """当前监听是否可用于「跳过任务前扫描」。"""
    watcher = get_active()
    if watcher is None:
        return False, "监听未运行（UI 服务未启动或监听未开启）"
    if not watcher.cfg.exclude.watch:
        return False, "配置里已关闭 exclude.watch"
    if not watcher.matches_targets(dirs, files):
        return False, "本轮排除目标与监听目标不一致（可能传入了额外的排除目录/文件）"
    return watcher.health()


@dataclass
class _Task:
    action: str                 # upsert | delete | delete_tree | scan_tree
    ready_at: float             # 去抖到期时间（monotonic）
    path: str = ""              # 事件原始路径（保留大小写）
    snap: tuple[int, float] | None = None   # (size, mtime) 首次观测，用于稳定性判定


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: "ExcludeWatcher"):
        self.w = watcher

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self.w.submit(event.src_path, "upsert")

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self.w.submit(event.src_path, "upsert")

    def on_deleted(self, event: FileSystemEvent) -> None:
        self.w.submit(event.src_path, "delete_tree" if event.is_directory else "delete")

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            self.w.submit(event.src_path, "delete_tree")
            self.w.submit(event.dest_path, "scan_tree")
            return
        self.w.submit(event.src_path, "delete")
        self.w.submit(event.dest_path, "upsert")


class ExcludeWatcher:
    """排除目录的实时索引（UI 服务内常驻 daemon 线程）。

    - watchdog 的 ReadDirectoryChangesW 后端（Windows），事件进内存队列；
    - 去抖 + 稳定判定后才读文件算哈希，避免索引写入中的半成品；
    - 新增/修改/删除/改名都落到同一张 exclude_files 表；
    - 程序自身输出目录与临时文件按 skip 清单跳过，不参与判重。
    """

    def __init__(self, cfg: Config, *, root: Path | None = None,
                 on_change: Callable[[str, str], None] | None = None):
        self.cfg = cfg
        self.root = Path(root or cfg.root)
        self.debounce = max(200, int(cfg.exclude.watch_debounce_ms)) / 1000.0
        self.stability = STABILITY_MS / 1000.0
        self.on_change = on_change

        self._skip = build_skip(cfg)
        self._skip_roots = skip_roots(cfg)
        self._dirs = self._resolve_dirs()
        self._file_parents = self._resolve_file_parents()

        self._pending: dict[str, _Task] = {}
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._observer: Observer | None = None
        self._worker: threading.Thread | None = None
        self._sync_thread: threading.Thread | None = None
        self._db_lock = threading.Lock()
        self._sync_done = threading.Event()   # 启动对账未完成时暂停事件写库，避免与 prune 抢同一文件
        self._sync_done.set()
        self._index: ImageIndex | None = None
        self._scanner: ExcludeIndex | None = None

        self.stats: dict[str, Any] = {
            "events": 0, "indexed": 0, "deleted": 0, "unchanged": 0,
            "skipped": 0, "errors": 0, "last_event_at": "", "last_error": "",
            "last_indexed_path": "",
        }
        self.sync_state: dict[str, Any] = {"at": "", "summary": "", "running": False}
        self.interruptions = 0     # 自本实例启动以来监听被中断的次数
        self.sync_ok = False       # 启动对账是否成功完成
        self._inflight = 0         # 正在处理的事件任务数
        self._consecutive_errors = 0

    # ---------------- 路径规则 ----------------
    def skip_roots(self) -> list[str]:
        return list(self._skip_roots)

    def should_skip(self, path: str | Path) -> bool:
        return self._skip(path)

    def _resolve_dirs(self) -> list[str]:
        out: list[str] = []
        for d in self.cfg.exclude.dirs:
            p = Path(d)
            if not p.is_absolute():
                p = self.root / p
            out.append(str(p))
        return out

    def _resolve_file_parents(self) -> list[str]:
        out: list[str] = []
        for f in self.cfg.exclude.files:
            p = Path(f)
            if not p.is_absolute():
                p = self.root / p
            out.append(str(p.parent))
        return out

    def _target_keys(self) -> set[str]:
        return ({_norm(d) for d in self._dirs}
                | {_norm(f) for f in self._resolve_files()})

    def matches_targets(self, dirs: list[str], files: list[str]) -> bool:
        """本轮任务的排除目标是否与监听目标完全一致（多/少一个都会退回扫描）。"""
        return ({_norm(d) for d in dirs} | {_norm(f) for f in files}) == self._target_keys()

    def flush(self, timeout: float = 2.0) -> bool:
        """等待已入队的事件处理完（含正在处理的那条），返回是否已清空。"""
        end = time.monotonic() + timeout
        while True:
            with self._lock:
                idle = not self._pending and self._inflight == 0 and self._sync_done.is_set()
            if idle:
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(0.02)

    def health(self) -> tuple[bool, str]:
        """监听是否健康到可以跳过任务前扫描。返回 (ok, 原因)。"""
        if not self.flush(2.0):
            return False, "仍有未处理事件（积压）"
        if not self._sync_done.is_set():
            return False, "启动对账尚未完成"
        if not self.sync_ok:
            return False, "启动对账未成功完成"
        obs = self._observer
        if obs is None or not obs.is_alive():
            return False, "监听线程未运行"
        if self.interruptions:
            return False, f"监听自对账以来中断过 {self.interruptions} 次"
        if self._consecutive_errors:
            return False, f"监听最近出错（连续 {self._consecutive_errors} 次）：{self.stats['last_error']}"
        return True, "ok"

    # ---------------- 事件入口 ----------------
    def submit(self, path: str, action: str) -> None:
        if action in ("upsert", "delete") and not self._is_image_path(path):
            return
        if self.should_skip(path):
            if action != "delete_tree":
                with self._lock:
                    self.stats["skipped"] += 1
                return
        key = _norm(path)
        now = time.monotonic()
        with self._cond:
            self._pending[key] = _Task(action=action, ready_at=now + self.debounce, path=path)
            self.stats["events"] += 1
            self.stats["last_event_at"] = datetime.now().isoformat(timespec="seconds")
            self._cond.notify()

    def _is_image_path(self, path: str) -> bool:
        return Path(path).suffix.lower() in IMAGE_SUFFIXES

    # ---------------- 生命周期 ----------------
    def start(self, sync: bool | None = None) -> dict[str, Any]:
        do_sync = self.cfg.exclude.watch_sync_on_start if sync is None else sync
        if do_sync:
            self._sync_done.clear()
            self.sync_state["running"] = True
            self._sync_thread = threading.Thread(
                target=self._sync_once, daemon=True, name="facefind-exclude-sync"
            )
            self._sync_thread.start()

        obs = Observer(timeout=0.2)
        watched: list[str] = []
        watched_norm: list[str] = []
        for d in self._dirs + self._file_parents:
            nd = _norm(d)
            if nd in watched_norm:
                continue
            if self.cfg.exclude.recursive and any(
                nd == w or nd.startswith(w + os.sep) for w in watched_norm
            ):
                continue    # 已被上级递归监听覆盖
            if not Path(d).exists():
                logger.warning("[排除目录] 监听跳过不存在的路径：%s", d)
                continue
            if any(_norm(d) == r or _norm(d).startswith(r + os.sep) for r in self._skip_roots):
                logger.warning("[排除目录] 监听目录整体在跳过清单内，将不会索引：%s", d)
            try:
                obs.schedule(_Handler(self), d, recursive=self.cfg.exclude.recursive)
                watched.append(d)
                watched_norm.append(nd)
            except Exception as exc:  # 权限/路径异常不应拖垮服务
                self.stats["errors"] += 1
                self.stats["last_error"] = f"{d}: {exc.__class__.__name__}: {exc}"
                logger.warning("[排除目录] 监听目录失败：%s（%s）", d, exc)
        if watched:
            obs.daemon = True
            obs.start()
        self._observer = obs
        self._worker = threading.Thread(target=self._loop, daemon=True, name="facefind-exclude-watch")
        self._worker.start()
        set_active(self)
        logger.info("[排除目录] 实时监听已启动：%s（去抖 %.0fms）", watched, self.debounce * 1000)
        return {"running": bool(watched), "watched": watched}

    def stop(self, timeout: float = 5.0) -> None:
        obs = self._observer
        if obs is not None and obs.is_alive():
            self.interruptions += 1     # 记录一次中断：下次任务前必须完整扫描
        self._stop.set()
        self._sync_done.set()
        with self._cond:
            self._cond.notify_all()
        if obs is not None:
            try:
                obs.stop()
                obs.join(timeout=timeout)
            except Exception:
                logger.debug("observer stop failed", exc_info=True)
        if self._worker is not None:
            self._worker.join(timeout=timeout)
        if self._sync_thread is not None and self._sync_thread.is_alive():
            logger.info("[排除目录] 后台对账扫描仍在运行，退出时不再等待（daemon 线程）")
        with self._db_lock:
            if self._index is not None:
                try:
                    self._index.conn.close()
                except Exception:
                    pass
                self._index = None
                self._scanner = None
        logger.info("[排除目录] 实时监听已停止")
        clear_active(self)

    # ---------------- 数据库 ----------------
    def _db(self) -> ExcludeIndex:
        with self._db_lock:
            if self._scanner is None:
                index = ImageIndex(self.cfg.index_path)
                index.conn.execute("PRAGMA busy_timeout=5000")
                self._index = index
                self._scanner = ExcludeIndex(index, self.cfg.exclude)
            return self._scanner

    def _sync_once(self) -> None:
        """启动对账：后台跑一次增量扫描，补齐服务停机期间漏掉的改动。"""
        started = time.monotonic()
        report = None
        try:
            index = ImageIndex(self.cfg.index_path)
            index.conn.execute("PRAGMA busy_timeout=5000")
            try:
                scanner = ExcludeIndex(index, self.cfg.exclude)
                report = scanner.scan(self._dirs, self._resolve_files(), skip=self.should_skip)
            finally:
                index.conn.close()
            self.sync_state.update({
                "at": datetime.now().isoformat(timespec="seconds"),
                "summary": report.summary(),
                "seconds": round(time.monotonic() - started, 2),
            })
            self.sync_ok = True
            logger.info("[排除目录] 启动对账完成：%s", report.summary())
        except Exception as exc:
            self.sync_ok = False
            self.stats["errors"] += 1
            self.stats["last_error"] = f"启动对账失败：{exc.__class__.__name__}: {exc}"
            logger.warning("[排除目录] 启动对账失败", exc_info=True)
        finally:
            self.sync_state["running"] = False
            self._sync_done.set()

    def _resolve_files(self) -> list[str]:
        out: list[str] = []
        for f in self.cfg.exclude.files:
            p = Path(f)
            if not p.is_absolute():
                p = self.root / p
            out.append(str(p))
        return out

    # ---------------- 处理循环 ----------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._cond:
                self._cond.wait(timeout=0.15)
                if self._stop.is_set():
                    break
                if not self._sync_done.is_set():
                    continue    # 对账扫描期间先攒事件，结束后再统一处理
                now = time.monotonic()
                due = [k for k, t in self._pending.items() if t.ready_at <= now]
                tasks = [(k, self._pending.pop(k)) for k in due]
            for key, task in tasks:
                if self._stop.is_set():
                    break
                with self._lock:
                    self._inflight += 1
                try:
                    self._process(key, task)
                    self._consecutive_errors = 0
                except Exception as exc:
                    self._consecutive_errors += 1
                    self.stats["errors"] += 1
                    self.stats["last_error"] = f"{key}: {exc.__class__.__name__}: {exc}"
                    logger.warning("[排除目录] 监听处理失败：%s", key, exc_info=True)
                finally:
                    with self._lock:
                        self._inflight -= 1

    def _rearm(self, key: str, task: _Task) -> None:
        with self._cond:
            existing = self._pending.get(key)
            if existing is not None and existing.ready_at >= task.ready_at:
                return
            self._pending[key] = task

    def _process(self, key: str, task: _Task) -> None:
        path = Path(task.path or key)
        if task.action == "delete":
            removed = self._db().remove_file(path)
            self.stats["deleted"] += int(removed)
            if self.on_change:
                self.on_change("delete", str(path))
            return
        if task.action == "delete_tree":
            removed = self._db().remove_tree(path)
            self.stats["deleted"] += int(removed)
            if self.on_change:
                self.on_change("delete_tree", str(path))
            return
        if task.action == "scan_tree":
            files, warns, _skipped = iter_images(str(path), self.cfg.exclude.recursive, self.should_skip)
            for f in files:
                self._upsert(f)
            for w in warns[:5]:
                logger.warning(w)
            return

        # upsert：先确认文件大小/时间戳稳定，再索引
        if self.should_skip(path):
            return
        try:
            st = path.stat()
        except OSError:
            removed = self._db().remove_file(path)
            self.stats["deleted"] += int(removed)
            return
        snap = (int(st.st_size), round(float(st.st_mtime), 3))
        if task.snap is None or task.snap != snap:
            task.snap = snap
            task.ready_at = time.monotonic() + self.stability
            self._rearm(key, task)
            return
        self._upsert(path)

    def _upsert(self, path: Path) -> None:
        status = self._db().refresh_file(path)
        if status == "unchanged":
            self.stats["unchanged"] += 1
            return
        if status in ("added", "updated"):
            self.stats["indexed"] += 1
            self.stats["last_indexed_path"] = str(path)
            if self.on_change:
                self.on_change(status, str(path))

    # ---------------- 状态 ----------------
    def snapshot(self) -> dict[str, Any]:
        obs = self._observer
        return {
            "enabled": True,
            "running": bool(obs is not None and obs.is_alive()),
            "dirs": list(self._dirs + self._file_parents),
            "skip": list(self._skip_roots),
            "debounce_ms": int(self.debounce * 1000),
            "pending": len(self._pending),
            "sync": dict(self.sync_state),
            "sync_ok": self.sync_ok,
            "interruptions": self.interruptions,
            "inflight": self._inflight,
            "consecutive_errors": self._consecutive_errors,
            **dict(self.stats),
        }
