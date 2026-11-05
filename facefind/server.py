from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import shutil
import threading
import time
import traceback
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from fastapi import Body, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps

from . import __version__, browser, secrets
from .backup import backup_path, maybe_backup_index
from .blocklist import BlockList
from .calibrate import collect
from .config import Config, PersonConfig
from .dedupe import dhash, phash, sha256_bytes
from .exclude import ExcludeIndex, ScanReport, build_skip, last_scan_decision
from .export import build_overview
from .face import FaceEngine
from .index import ImageIndex
from .jobs import JobManager
from .net import HttpClient
from .people import discover_references
from .pipeline import _save_raw, _write_person_metadata
from .quality import decode, measure, sniff_ext
from .settings import SettingsStore
from .sites import collect_run_history
from .sr import FACE_MODELS as SR_FACE_MODELS
from .sr import MODELS as SR_MODELS
from .sr import SuperResolver, read_image, write_image

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
logger = logging.getLogger(__name__)
ENGINE_LOCK = threading.Lock()
_ENGINE: FaceEngine | None = None
_REF_CACHE: dict[str, dict] = {}
_REF_LOCK = threading.Lock()
_THUMB_LOCK = threading.Lock()
_EXCLUDE_STATE: dict[str, Any] = {"running": False, "report": "", "started_at": ""}
_LOGIN_STATE: dict[str, dict] = {}
_LOGIN_THREADS: dict[str, threading.Thread] = {}


class _SrCancelled(Exception):
    """超分任务被用户取消（在分块回调中抛出，尽快中断当前图片）。"""


def _engine(cfg: Config) -> FaceEngine:
    global _ENGINE
    with ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = FaceEngine(
                model=cfg.face.model,
                device=cfg.face.device,
                det_size=cfg.face.det_size,
                min_face_px=cfg.face.min_face_px,
                allow_multiple_faces=cfg.face.allow_multiple_faces,
            ).load()
        _ENGINE.min_face_px = cfg.face.min_face_px
        _ENGINE.allow_multiple_faces = cfg.face.allow_multiple_faces
        return _ENGINE


def _safe_name(name: str) -> str:
    bad = '<>:"/\\|?*\n\r\t'
    return "".join("_" if c in bad else c for c in name).strip() or "unnamed"


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(1, 1000):
        cand = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not cand.exists():
            return cand
    return path.with_name(f"{path.stem}_{datetime.now():%H%M%S%f}{path.suffix}")


def _recycle_dir(cfg: Config) -> Path:
    """回收目录：只增不删（自动清理默认关闭）。"""
    return cfg.root / "data" / "recycle"


def _move_to_recycle(cfg: Config, person: str, src: Path, digest: str = "") -> Path:
    """成品移入回收目录，文件名为「原名__hash_时间戳.后缀」，不物理删除。"""
    dest_dir = _recycle_dir(cfg) / _safe_name(person)
    dest_dir.mkdir(parents=True, exist_ok=True)
    h = (digest or hashlib.sha256(src.name.encode("utf-8")).hexdigest())[:8]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return _unique(dest_dir / f"{src.stem}__{h}_{ts}{src.suffix}")


def _image_hashes(path: Path) -> tuple[int | None, int | None]:
    try:
        img = decode(path.read_bytes())
    except Exception:
        return None, None
    if img is None:
        return None, None
    return phash(img), dhash(img)


# ---------------- 手动收录（把 rejected 改为 saved） ----------------
REFERERS = {
    "baidu": "https://image.baidu.com",
    "weibo": "https://weibo.com",
    "xiaohongshu": "https://www.xiaohongshu.com",
}
REJECT_TEXT = {
    "framing": "构图不合格", "low_similarity": "相似度低", "no_face": "无脸",
    "face_too_small": "人脸过小", "too_small": "分辨率过低", "below_accept": "低于收录阈值",
    "blocked": "屏蔽", "blocked_url": "屏蔽", "downloaded_before": "重复",
    "excluded_dir": "排除目录", "undecodable": "无法解码",
    "duplicate": "重复", "excluded": "排除目录", "discarded": "已丢弃", "failed": "下载失败",
}


def _reject_text(row) -> str:
    """把索引里的拒绝原因转成可读中文（保留原始值便于回溯）。"""
    raw = (row["dedupe_stage"] or row["status"] or "rejected")
    if raw.startswith("framing_"):
        return f"构图不合格（{raw[len('framing_'):]}）"
    if raw.startswith("blocked_"):
        return "屏蔽（内容命中黑名单）"
    if raw.startswith("phash(") or raw.startswith("face("):
        return f"重复（{raw}）"
    return REJECT_TEXT.get(raw, raw)


def _manual_log(cfg: Config, action: str, row, **extra: Any) -> None:
    """手动收录/撤销单独写日志，便于与自动运行事件区分回溯。"""
    cfg.log_path.mkdir(parents=True, exist_ok=True)
    rec = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "type": "manual_accept" if action == "accept" else "manual_undo",
        "action": action,
        "person": row["person"] or "",
        "source": row["source"] or "",
        "image_url": row["url"] or "",
        "reject_reason": row["dedupe_stage"] or "",
        "manual_accept": action == "accept",
    }
    rec.update({k: v for k, v in extra.items() if v is not None})
    with (cfg.log_path / "manual_accept.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _manual_bytes(cfg: Config, row) -> tuple[bytes, str, str]:
    """优先记录中的 raw_path；缺失时按 image_url 重新下载（限速+重试）。"""
    raw = Path(row["raw_path"]) if row["raw_path"] else None
    if raw and raw.exists():
        return raw.read_bytes(), str(raw), "raw"
    url = (row["url"] or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("记录没有可用的图片地址（image_url 为空）")
    source = row["source"] or "baidu"
    referer = row["page_url"] or REFERERS.get(source, "")
    client = HttpClient(cfg.network, source)
    data = client.get_bytes(url, referer=referer or None)
    return data, "", "download"


def _manual_file(path: Path) -> dict:
    return {
        "path": str(path),
        "filename": path.name,
        "image_url": _file_url(path),
        "thumb_url": _thumb_url(path),
    }



def _payload_ids(payload: dict) -> list[int]:
    raw = payload.get("ids")
    if raw is None and payload.get("id") is not None:
        raw = [payload.get("id")]
    if not isinstance(raw, (list, tuple)):
        return []
    ids: list[int] = []
    for v in raw:
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(ids))


def _block_list(cfg: Config) -> BlockList:
    """独立文件（data/blocklist.json）里的屏蔽名单：图片级 + 人员级。"""
    return BlockList(cfg.path("data/blocklist.json"))


def _block_record(row, index: ImageIndex | None = None) -> dict:
    """把索引行转成屏蔽名单条目（带 URL/指纹/文件名，绝不只靠文件名）。"""
    path = Path(row["file_path"]) if row["file_path"] else None
    ph, dh = None, None
    if path is not None and path.exists():
        try:
            ph, dh = _image_hashes(path)
        except Exception:
            ph, dh = None, None
    keys = row.keys()
    ph = ph if ph is not None else row["phash"]
    dh = dh if dh is not None else (row["dhash"] if "dhash" in keys else None)
    return {
        "id": int(row["id"]),
        "person": row["person"] or "",
        "filename": path.name if path is not None else "",
        "url": row["url"] or "",
        "sha256": row["sha256"] or "",
        "phash": ph, "dhash": dh,
        "source": row["source"] or "",
    }


def _blocked_payload(row, index: ImageIndex) -> dict:
    """黑名单条目：附带缩略图（若原文件仍在）。"""
    file_path = ""
    if row["url"]:
        for img in index.conn.execute(
            "SELECT file_path FROM images WHERE url=? AND status != 'deleted' ORDER BY id DESC",
            (row["url"],),
        ):
            if img["file_path"] and Path(img["file_path"]).exists():
                file_path = img["file_path"]
                break
    return {
        "id": row["id"],
        "url": row["url"],
        "phash": f"{row['phash'] & 0xFFFFFFFFFFFFFFFF:016x}" if row["phash"] is not None else "",
        "dhash": f"{row['dhash'] & 0xFFFFFFFFFFFFFFFF:016x}" if row["dhash"] is not None else "",
        "person": row["person"] or "",
        "source_site": row["source_site"] or "",
        "reason": row["reason"] or "",
        "created_at": row["created_at"] or "",
        "origin": row["origin"] or "",
        "file_path": file_path,
        "thumb_url": _thumb_url(file_path) if file_path else "",
    }


def _metrics(path: Path) -> dict:
    """目录/文件占用：大小、文件数、最近修改时间。"""
    path = Path(path)
    if not path.exists():
        return {"size": 0, "files": 0, "mtime": ""}
    if path.is_file():
        try:
            st = path.stat()
            return {"size": st.st_size, "files": 1,
                    "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")}
        except OSError:
            return {"size": 0, "files": 0, "mtime": ""}
    size = 0
    files = 0
    mtime = 0.0
    for p in path.rglob("*"):
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        size += st.st_size
        files += 1
        mtime = max(mtime, st.st_mtime)
    return {
        "size": size, "files": files,
        "mtime": datetime.fromtimestamp(mtime).isoformat(timespec="seconds") if mtime else "",
    }


def _sum_metrics(items: list[dict]) -> dict:
    size = sum(int(x.get("size") or 0) for x in items)
    files = sum(int(x.get("files") or 0) for x in items)
    mtimes = [x["mtime"] for x in items if x.get("mtime")]
    return {"size": size, "files": files, "mtime": max(mtimes) if mtimes else ""}


def _thumb_cache_paths(thumb_dir: Path, files: list[Path]) -> list[Path]:
    """按 /api/thumb 的缓存键规则找出这些原图的缩略图缓存。"""
    out: list[Path] = []
    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        for w in (96, 160, 320, 640, 1024):
            key = hashlib.sha1(
                f"{p.resolve()}|{st.st_mtime_ns}|{st.st_size}|{w}".encode("utf-8")
            ).hexdigest()
            cand = thumb_dir / f"{key}.jpg"
            if cand.exists():
                out.append(cand)
    return out


def _delete_tree(path: Path, keep_base: bool = True) -> tuple[int, int]:
    """删除目录下所有文件（不跟随符号链接），可选删除空目录；返回（文件数, 字节数）。"""
    freed = 0
    removed = 0
    if not path.exists():
        return 0, 0
    for p in list(path.rglob("*")):
        if p.is_symlink() or not p.is_file():
            continue
        try:
            st = p.stat()
            p.unlink()
            freed += st.st_size
            removed += 1
        except OSError:
            continue
    for d in sorted((x for x in path.rglob("*") if x.is_dir()),
                    key=lambda x: len(x.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    if not keep_base:
        try:
            path.rmdir()
        except OSError:
            pass
    return removed, freed


def _assert_clearable(cfg: Config, target: Path) -> Path:
    """规范化并校验清理目标：只允许项目内已知子目录，拒绝项目外/排除目录/受保护对象。"""
    real = Path(target).resolve()
    if real == cfg.root.resolve():
        raise HTTPException(403, "拒绝清理：不能清理项目根目录")
    allowed = (
        cfg.output_path.resolve(), cfg.raw_path.resolve(), _recycle_dir(cfg).resolve(),
        (cfg.root / "data" / "thumbs").resolve(), cfg.log_path.resolve(),
    )
    if not any(_within(real, base) for base in allowed):
        raise HTTPException(403, f"拒绝清理：目标不在允许的项目子目录内（{real}）")
    for protected in (cfg.index_path.resolve(), cfg.browser_profile_path.resolve()):
        if real == protected or _within(real, protected):
            raise HTTPException(403, "拒绝清理：索引库与浏览器档案不允许清理")
    for ex in cfg.exclude.dirs:
        try:
            p = Path(ex).resolve()
        except OSError:
            continue
        if _within(real, p) or _within(p, real):
            raise HTTPException(403, "拒绝清理：目标与用户排除目录重叠")
    return real


def _raw_dirs_for(cfg: Config, person: str) -> list[Path]:
    base = cfg.raw_path
    if not base.is_dir():
        return []
    if person:
        return [d / _safe_name(person) for d in base.iterdir()
                if d.is_dir() and (d / _safe_name(person)).is_dir()]
    return [d for d in base.iterdir() if d.is_dir()]


def _file_url(path: str | Path) -> str:
    """原图/成品 URL，同样带文件版本，避免替换后大图仍走浏览器缓存。"""
    p = Path(path)
    try:
        st = p.stat()
        version = f"&v={st.st_mtime_ns}-{st.st_size}"
    except OSError:
        version = ""
    return "/api/file?path=" + urllib.parse.quote(str(p)) + version


def _thumb_url(path: str | Path | None, width: int = 640) -> str:
    """缩略图 URL 带文件版本（mtime+size），文件被替换/撤销后 URL 自动变化，避免浏览器缓存旧图。"""
    if not path:
        return ""
    p = Path(path)
    try:
        st = p.stat()
        version = f"{st.st_mtime_ns}-{st.st_size}"
    except OSError:
        version = ""
    url = f"/api/thumb?w={width}&path=" + urllib.parse.quote(str(p))
    if version:
        url += "&v=" + version
    return url


def _purge_thumbs(cfg: Config, files: list[Path]) -> int:
    """删除这些原图对应的缩略图缓存文件（替换/撤销后调用）。"""
    thumb_dir = cfg.root / "data" / "thumbs"
    removed = 0
    for cache in _thumb_cache_paths(thumb_dir, [p for p in files if p]):
        try:
            cache.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _basic_ref(path: Path) -> dict:
    info = {
        "path": str(path),
        "name": path.name,
        "size": 0,
        "mtime": "",
        "width": 0,
        "height": 0,
        "url": _file_url(path),
        "checked": False,
        "ok": None,
        "issues": [],
    }
    try:
        st = path.stat()
        info["size"] = st.st_size
        info["mtime"] = datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
        with Image.open(path) as im:
            info["width"], info["height"] = im.size
    except Exception:
        info["issues"] = [{"code": "undecodable", "text": "无法解码"}]
        info["ok"] = False
        info["checked"] = True
        return info
    with _REF_LOCK:
        cached = _REF_CACHE.get(str(path))
    if cached and cached.get("mtime") == info["mtime"] and cached.get("size") == info["size"]:
        merged = dict(info)
        merged.update(cached.get("result") or {})
        merged.update({"path": str(path), "url": info["url"], "name": path.name})
        return merged
    return info


def _check_reference(path: Path, cfg: Config) -> dict:
    st = path.stat()
    data = path.read_bytes()
    img = decode(data)
    issues: list[dict] = []
    result: dict[str, Any] = {
        "path": str(path),
        "name": path.name,
        "size": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        "url": _file_url(path),
        "checked": True,
        "ok": True,
        "issues": issues,
        "width": 0,
        "height": 0,
        "sharpness": 0.0,
        "n_faces": 0,
        "face_px": 0,
        "face_ratio": 0.0,
    }
    if img is None:
        result["ok"] = False
        issues.append({"code": "undecodable", "text": "无法解码，不能作为参考照"})
        return result
    metrics = measure(img, data)
    result.update({
        "width": metrics.width, "height": metrics.height, "sharpness": round(metrics.sharpness, 1),
    })
    try:
        _, match = _engine(cfg).embed_largest(img)
    except Exception as exc:
        result["ok"] = False
        issues.append({"code": "face_error", "text": f"人脸检测失败：{exc}"})
        return result
    result.update({
        "n_faces": match.n_faces, "face_px": match.face_px, "face_ratio": match.face_ratio,
    })
    if match.n_faces == 0:
        issues.append({"code": "no_face", "text": "未检测到人脸"})
    elif match.n_faces > 1 and not cfg.face.allow_multiple_faces:
        issues.append({"code": "multi_face", "text": f"检测到 {match.n_faces} 张人脸（会污染比对，建议换单人人脸照）"})
    elif match.face_px < cfg.face.min_face_px:
        issues.append({"code": "face_too_small", "text": f"人脸仅 {match.face_px}px（低于 {cfg.face.min_face_px}px）"})
    if metrics.width < 160 or metrics.height < 160:
        issues.append({"code": "low_resolution", "text": "分辨率过低（小于 160×160）"})
    if metrics.sharpness < 60:
        issues.append({"code": "blurry", "text": "画面偏糊（锐度不足）"})
    blocking = {"undecodable", "no_face", "multi_face", "face_too_small", "low_resolution"}
    result["ok"] = not any(i["code"] in blocking for i in issues)
    with _REF_LOCK:
        _REF_CACHE[str(path)] = {"mtime": result["mtime"], "size": result["size"], "result": result}
    return result


def _person_payload(cfg: Config, index: ImageIndex, name: str, refs: list[str]) -> dict:
    pc = cfg.person_config(name)
    stats = index.stats_for(name)
    return {
        "name": name,
        "count": (pc.count if pc and pc.count is not None else cfg.search.count),
        "queries": (pc.queries if pc and pc.queries else [name]),
        "accept_threshold": (
            pc.accept_threshold if pc and pc.accept_threshold is not None else cfg.face.accept_threshold
        ),
        "review_threshold": (
            pc.review_threshold if pc and pc.review_threshold is not None else cfg.face.review_threshold
        ),
        "sources": (pc.sources if pc and pc.sources else cfg.search.sources),
        "saved": stats["saved"],
        "review": stats["review"],
        "last_run": index.last_activity(name),
        "refs": [_basic_ref(Path(p)) for p in refs if Path(p).exists()],
    }


def _ref_quality(refs: list[dict]) -> dict:
    ok = sum(1 for r in refs if r.get("checked") and r.get("ok"))
    bad = sum(1 for r in refs if r.get("checked") and not r.get("ok"))
    unchecked = sum(1 for r in refs if not r.get("checked"))
    return {"total": len(refs), "ok": ok, "bad": bad, "unchecked": unchecked}


def _person_logs(cfg: Config, name: str, limit: int = 120) -> list[str]:
    """读取该人最近一次运行的事件日志（data/logs/run_*_<人名>.jsonl）。"""
    safe = _safe_name(name)
    files = sorted(
        cfg.log_path.glob(f"run_*_{safe}.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        return []
    source = next((f for f in files if f.stat().st_size > 0), None)
    if source is None:
        return []
    lines: list[str] = []
    try:
        with source.open(encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                status = rec.get("status", "")
                typ = rec.get("type") or "image"
                t = str(rec.get("time", ""))[11:19]
                if typ == "site_summary":
                    continue
                if typ == "site":
                    lines.append(
                        f"[{t}] {rec.get('source', '')} 站点状态 {rec.get('state', '')}"
                        f"{('：' + rec.get('reason', '')) if rec.get('reason') else ''}"
                    )
                    continue
                if typ == "search":
                    unit = "轮" if str(rec.get("unit") or "") == "round" else "页"
                    lines.append(
                        f"[{t}] {rec.get('source', '')}/{rec.get('query', '')} "
                        f"搜索第 {rec.get('page')}/{rec.get('max_pages')} {unit}，返回 {rec.get('count')} 条"
                    )
                    continue
                if typ == "search_page":
                    unit = "轮滚动" if str(rec.get("unit") or "") == "round" else "页"
                    lines.append(
                        f"[{t}] {rec.get('source', '')}/{rec.get('query', '')} "
                        f"搜索完成：{rec.get('page')}/{rec.get('max_pages')} {unit}"
                    )
                    continue
                reason = rec.get("reason") or rec.get("dedupe_stage") or ""
                framing = rec.get("framing") or ""
                extra = " ".join(x for x in (reason, f"构图={framing}" if framing else "") if x)
                lines.append(
                    f"[{t}] {rec.get('source', '')}/{rec.get('query', '')} "
                    f"{status} {extra}".rstrip()
                )
    except OSError:
        return []
    return lines[-limit:]


def _index_summary(cfg: Config) -> dict:
    index = ImageIndex(cfg.index_path)
    try:
        return index.summary()
    finally:
        index.conn.close()


def create_app(config_path: str | Path = "config.yaml") -> FastAPI:
    config_path = Path(config_path).resolve()
    cfg = Config.load(config_path)
    store = SettingsStore(cfg)
    store.apply(cfg)
    jobs = JobManager(config_path)
    backup_msg = maybe_backup_index(cfg)
    app = FastAPI(title="facefind", version=__version__)

    # ---------------- 排除目录实时监听（服务内常驻线程） ----------------
    watch_holder: dict[str, Any] = {"worker": None, "error": ""}
    watch_lock = threading.Lock()

    def _stop_exclude_watcher() -> None:
        with watch_lock:
            worker = watch_holder.get("worker")
            watch_holder["worker"] = None
        if worker is not None:
            try:
                worker.stop()
            except Exception:
                logger.warning("[排除目录] 停止监听失败", exc_info=True)

    def _start_exclude_watcher(sync: bool = True) -> None:
        _stop_exclude_watcher()
        if not cfg.exclude.watch:
            watch_holder["error"] = "配置里已关闭 exclude.watch"
            return
        try:
            from .watcher import ExcludeWatcher
        except ImportError:
            watch_holder["error"] = "未安装 watchdog：python -m pip install watchdog"
            logger.warning("[排除目录] %s", watch_holder["error"])
            return
        try:
            worker = ExcludeWatcher(cfg)
            worker.start(sync=sync)
            watch_holder["error"] = ""
            with watch_lock:
                watch_holder["worker"] = worker
        except Exception as exc:
            watch_holder["error"] = f"{exc.__class__.__name__}: {exc}"
            logger.warning("[排除目录] 启动监听失败：%s", watch_holder["error"], exc_info=True)

    def _watch_status() -> dict[str, Any]:
        worker = watch_holder.get("worker")
        data: dict[str, Any] = {
            "config": {
                "watch": bool(cfg.exclude.watch),
                "watch_debounce_ms": int(cfg.exclude.watch_debounce_ms),
                "watch_skip": list(cfg.exclude.watch_skip),
                "watch_sync_on_start": bool(cfg.exclude.watch_sync_on_start),
            },
            "error": watch_holder.get("error") or "",
        }
        if worker is not None:
            data.update(worker.snapshot())
        else:
            data.update({"enabled": bool(cfg.exclude.watch), "running": False})
        return data

    app.add_event_handler("startup", _start_exclude_watcher)
    app.add_event_handler("shutdown", _stop_exclude_watcher)

    # ---------------- 超分状态（GPU 任务串行） ----------------
    sr_engine = SuperResolver(cfg.path("models/sr"))
    sr_lock = threading.RLock()
    sr_face_lock = threading.Lock()
    sr_job = {"thread": None, "stop": threading.Event()}
    sr_state: dict[str, Any] = {
        "state": "idle", "total": 0, "done": 0, "failed": 0, "current": "",
        "model": "", "scale": 0, "face_restore": "", "pct": 0.0,
        "started_at": "", "finished_at": "", "elapsed": 0.0, "error": "",
        "items": [], "logs": [],
    }

    def _sr_settings() -> dict:
        return store.describe().get("sr") or {}

    def _sr_busy() -> bool:
        thread = sr_job.get("thread")
        return bool(thread and thread.is_alive())

    def _sr_log(msg: str) -> None:
        with sr_lock:
            sr_state["logs"].append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "msg": msg,
            })
            if len(sr_state["logs"]) > 200:
                del sr_state["logs"][:-200]

    def _sr_snapshot() -> dict:
        with sr_lock:
            data = dict(sr_state)
            data["items"] = list(sr_state["items"][-100:])
            data["logs"] = list(sr_state["logs"][-100:])
            data["busy"] = _sr_busy()
            return data

    def _sr_params(payload: dict) -> dict:
        defaults = _sr_settings()
        model = str(payload.get("model") or defaults.get("model") or "realesrgan_x4plus")
        spec = next((m for m in SR_MODELS if m.id == model), None)
        if spec is None:
            raise HTTPException(400, f"未知超分模型：{model}")
        if not sr_engine.available(model):
            raise HTTPException(400, f"模型未安装：{spec.label}。请先下载权重到 models/sr/。")
        try:
            scale = int(payload.get("scale", defaults.get("scale") or 4))
        except (TypeError, ValueError):
            raise HTTPException(400, "倍率必须是 2 或 4")
        if scale not in (2, 4):
            raise HTTPException(400, "倍率只能是 2 或 4")
        face = str(payload.get("face_restore") or defaults.get("face_restore") or "none")
        if face not in SR_FACE_MODELS:
            raise HTTPException(400, "不支持的面部修复模型")
        info = SR_FACE_MODELS[face]
        if info.get("filename") and not (cfg.path("models/sr") / info["filename"]).exists():
            raise HTTPException(400, f"面部修复模型未安装：{info['label']}")
        try:
            strength = float(payload.get("face_strength", defaults.get("face_strength") or 0.8))
        except (TypeError, ValueError):
            strength = 0.8
        try:
            max_side = int(payload.get("max_side", defaults.get("max_side") or 0))
        except (TypeError, ValueError):
            max_side = 0
        try:
            tile = int(payload.get("tile", defaults.get("tile") or 0))
        except (TypeError, ValueError):
            tile = 0
        mode = str(payload.get("replace_mode") or "keep").strip().lower()
        if mode not in ("keep", "backup", "overwrite"):
            raise HTTPException(400, "replace_mode 只能是 keep / backup / overwrite")
        return {
            "model": model, "scale": scale, "face_restore": face,
            "face_strength": max(0.0, min(1.0, strength)),
            "max_side": max(0, max_side), "tile": max(0, tile),
            "jpeg_quality": int(defaults.get("jpeg_quality") or 95),
            "short": spec.short, "replace_mode": mode,
        }

    def _sr_detect(img):
        """复用主线程的人脸引擎；加锁避免与手动收录同时推理。"""
        with sr_face_lock:
            return _engine(cfg).detect(img)

    def _sr_output_path(index: ImageIndex, row, params: dict) -> Path:
        src = Path(row["file_path"])
        face_part = "" if params["face_restore"] == "none" else "_face"
        name = f"{src.stem}_sr{params['scale']}x_{params['short']}{face_part}.jpg"
        out_dir = cfg.path("data/sr") / _safe_name(row["person"])
        cand = out_dir / name
        owner = index.conn.execute(
            "SELECT id FROM images WHERE sr_path=? AND id<>? LIMIT 1",
            (str(cand), int(row["id"]),
        )).fetchone()
        # 同一条记录重跑允许覆盖自己的 SR 文件；其他记录或磁盘上无主同名文件则换名。
        same_row = str(cand) == str(row["sr_path"] or "")
        if owner is not None or (cand.exists() and not same_row):
            cand = _unique(cand)
        return cand

    def _sr_replace(index: ImageIndex, row, sr_path: Path, mode: str) -> dict:
        """按 replace_mode 处理「原图 vs SR 结果」。只返回索引字段，文件操作在这里完成。"""
        if mode == "keep":
            return {"sr_replaced": 0, "sr_replace_mode": "keep", "backup_path": "",
                    "replaced_at": "", "sr_original_path": ""}
        orig = Path(row["file_path"]) if row["file_path"] else None
        if orig is None or not orig.exists():
            raise RuntimeError(f"原图不存在，无法替换：{row['file_path']}")
        # 已经替换过的记录 file_path 指向 SR 文件，再替换会把成品自己移走/删掉，直接拒绝。
        if (row["sr_replace_mode"] or "") in ("backup", "overwrite") \
                and str(row["file_path"]) == str(row["sr_path"] or ""):
            raise RuntimeError("该图已完成超分替换；如需重跑请先撤销，或改用「超分」另存")
        now = datetime.now().isoformat(timespec="seconds")
        original = row["sr_original_path"] or str(orig)
        _purge_thumbs(cfg, [orig, Path(sr_path)])   # 替换后旧缩略图立即失效
        if mode == "backup":
            bak_dir = cfg.path("data/recycle/sr_backup") / _safe_name(row["person"] or "")
            bak_dir.mkdir(parents=True, exist_ok=True)
            bak = _unique(bak_dir / f"{orig.stem}_backup_{datetime.now():%Y%m%d_%H%M%S}{orig.suffix}")
            shutil.move(str(orig), str(bak))
            return {"file_path": str(sr_path), "sr_replaced": 1, "sr_replace_mode": "backup",
                    "backup_path": str(bak), "replaced_at": now, "sr_original_path": original}
        # overwrite：彻底覆盖，连同旧备份一起删除，无法恢复
        if row["backup_path"]:
            old = Path(row["backup_path"])
            if old.exists():
                try:
                    old.unlink()
                except OSError:
                    pass
        orig.unlink()
        return {"file_path": str(sr_path), "sr_replaced": 1, "sr_replace_mode": "overwrite",
                "backup_path": "", "replaced_at": now, "sr_original_path": original}

    def _sr_targets(payload: dict) -> tuple[list[dict], int]:
        """按 ids/person 汇总可超分条目；run 与 plan 共用，保证提示张数=执行张数。"""
        ids = _payload_ids(payload)
        person = str(payload.get("person") or "").strip()
        if not ids and not person:
            raise HTTPException(400, "缺少 ids 或 person（要超分的图片范围）")
        index = ImageIndex(cfg.index_path)
        mode = str(payload.get("replace_mode") or "keep")
        try:
            rows = index.rows_by_ids(ids) if ids else index.rows(person, ("saved", "review"))
            items: list[dict] = []
            skipped = 0
            seen: set[int] = set()
            for row in rows:
                rid = int(row["id"])
                if rid in seen:
                    continue
                seen.add(rid)
                if (row["status"] or "") not in ("saved", "review"):
                    skipped += 1
                    continue
                # 替换流程下，已完成替换的记录再跑会把成品自身移走/删掉
                if mode in ("backup", "overwrite") \
                        and (row["sr_replace_mode"] or "") in ("backup", "overwrite") \
                        and str(row["file_path"]) == str(row["sr_path"] or ""):
                    skipped += 1
                    continue
                src = Path(row["file_path"]) if row["file_path"] else None
                if src is None or not src.exists():
                    skipped += 1
                    continue
                items.append({"id": rid, "person": row["person"], "path": str(src),
                              "source": row["source"] or ""})
        finally:
            index.conn.close()
        return items, skipped

    def _sr_replace_log(row, mode: str, out_path: Path, fields: dict, quick: bool = False) -> None:
        """替换动作单独写 data/logs/sr_replace.jsonl；overwrite 记 replaced_hard=true。"""
        try:
            log_dir = cfg.log_path
            log_dir.mkdir(parents=True, exist_ok=True)
            rec = {
                "time": datetime.now().isoformat(timespec="seconds"),
                "type": "sr_replace",
                "id": int(row["id"]), "person": row["person"] or "",
                "mode": mode,
                "quick_action": bool(quick),
                "original_path": fields.get("sr_original_path", "") or (row["file_path"] or ""),
                "sr_path": str(out_path),
                "file_path": fields.get("file_path", row["file_path"] or ""),
                "backup_path": fields.get("backup_path", ""),
                "replaced_hard": mode == "overwrite",
            }
            with (log_dir / "sr_replace.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:  # 日志失败不影响主流程
            pass

    # ---------------- 状态 --------------
    @app.get("/api/state")
    def api_state():
        return {
            "app": "facefind",
            "version": __version__,
            "root": str(cfg.root),
            "settings": store.describe(),
            "job": jobs.status(),
            "summary": _index_summary(cfg),
            "backup": backup_msg or "",
            "backup_path": str(backup_path(cfg)),
            "browser": browser.info(cfg),
            "server_time": datetime.now().isoformat(timespec="seconds"),
        }

    # ---------------- 服务管理（托盘宿主） --------------
    def _sup_info() -> dict:
        path = os.environ.get("FACEINFO_SUPERVISOR_FILE") or ""
        if not path:
            return {}
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _sup_call(cmd: str) -> dict:
        info = _sup_info()
        port = int(info.get("control_port") or 0)
        token = str(info.get("token") or "")
        if not port or not token:
            raise HTTPException(409, "当前没有可用的托盘宿主")
        import socket as _socket

        try:
            with _socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
                sock.sendall((json.dumps({"cmd": cmd, "token": token}) + "\n").encode())
                raw = sock.recv(4096).decode("utf-8", "replace").strip()
                return json.loads(raw) if raw else {"ok": False}
        except Exception as exc:
            raise HTTPException(502, f"托盘宿主无响应：{exc}")

    def _exit_later(delay: float = 0.8) -> None:
        def _go() -> None:
            time.sleep(delay)
            fn = getattr(app.state, "shutdown", None)
            if callable(fn):
                fn()
            time.sleep(10)          # 兜底：graceful 关闭卡住时强制退出
            os._exit(0)
        threading.Thread(target=_go, daemon=True, name="facefind-exit").start()

    def _stop_running_job(force: bool) -> None:
        if not jobs.status().get("busy"):
            return
        if not force:
            raise HTTPException(409, "当前有任务正在运行，请先停止任务，或选择「强制中断」")
        jobs.stop()
        for _ in range(40):        # 最多等 10s，让浏览器/写盘收尾
            if not jobs.status().get("busy"):
                break
            time.sleep(0.25)

    @app.get("/api/admin/status")
    def api_admin_status():
        info = _sup_info()
        status = jobs.status()
        return {
            "managed": bool(info) and bool(info.get("control_port")),
            "state": str(info.get("state") or ""),
            "supervisor_pid": int(info.get("pid") or 0),
            "pid": os.getpid(),
            "busy": bool(status.get("busy")),
            "job_state": str(status.get("state") or ""),
            "url": str(info.get("url") or ""),
        }

    @app.post("/api/admin/restart")
    def api_admin_restart(payload: dict = Body(default={})):
        if not _sup_info():
            raise HTTPException(
                409, "当前为无宿主模式（--no-tray）启动，无法自动重启；请关闭后重新双击 start_ui.bat"
            )
        _stop_running_job(bool((payload or {}).get("force")))
        reply = _sup_call("restart")
        if not reply.get("ok"):
            raise HTTPException(502, f"托盘宿主拒绝重启：{reply.get('error') or reply}")
        _exit_later(1.0)
        return {"ok": True, "action": "restart", "reconnect_after": 10}

    @app.post("/api/admin/shutdown")
    def api_admin_shutdown(payload: dict = Body(default={})):
        if not _sup_info():
            raise HTTPException(409, "当前为无宿主模式（--no-tray）启动，页面无法退出整个程序")
        _stop_running_job(bool((payload or {}).get("force")))
        reply = _sup_call("shutdown")
        if not reply.get("ok"):
            raise HTTPException(502, f"托盘宿主拒绝退出：{reply.get('error') or reply}")
        _exit_later(1.0)
        return {"ok": True, "action": "shutdown"}

    @app.post("/api/admin/_exit")
    def api_admin_exit(payload: dict = Body(default={}), x_facefind_admin: str = Header(default="")):
        """宿主专用：请求子进程优雅退出（带 token，浏览器无法调用）。"""
        token = os.environ.get("FACEINFO_ADMIN_TOKEN", "")
        if not token or x_facefind_admin != token:
            raise HTTPException(403, "forbidden")
        _exit_later(0.5)
        return {"ok": True}

    # ---------------- 设置 --------------
    @app.get("/api/settings")
    def get_settings():
        return store.describe()

    @app.put("/api/settings")
    def put_settings(payload: dict = Body(...)):
        patch: dict[str, Any] = {}
        search: dict[str, Any] = {}
        face: dict[str, Any] = {}
        exclude: dict[str, Any] = {}
        framing: dict[str, Any] = {}
        if payload.get("profile"):
            patch["profile"] = str(payload["profile"])
        if "sources" in payload:
            search["sources"] = [str(s) for s in (payload["sources"] or [])]
        for key in ("count", "max_pages", "detail_budget"):
            if payload.get(key) is not None:
                search[key] = int(payload[key])
        for key in ("accept_threshold", "review_threshold", "min_face_px"):
            if payload.get(key) is not None:
                face[key] = payload[key]
        if payload.get("face_model") is not None:
            face["model"] = str(payload["face_model"] or "")
        if payload.get("framing_model") is not None:
            framing["model"] = str(payload["framing_model"] or "")
        if "exclude_dirs" in payload:
            exclude["dirs"] = [str(x) for x in (payload["exclude_dirs"] or [])]
        if "exclude_files" in payload:
            exclude["files"] = [str(x) for x in (payload["exclude_files"] or [])]
        if payload.get("exclude_phash_threshold") is not None:
            exclude["phash_threshold"] = int(payload["exclude_phash_threshold"])
        if payload.get("exclude_scan_on_run") is not None:
            mode = str(payload["exclude_scan_on_run"]).lower()
            if mode not in ("auto", "always", "never"):
                raise HTTPException(400, "exclude_scan_on_run 只能是 auto / always / never")
            exclude["scan_on_run"] = mode
        if payload.get("framing_mode") is not None:
            framing["mode"] = str(payload["framing_mode"])
        if payload.get("framing_edge_cut_tolerance") is not None:
            framing["edge_cut_tolerance"] = float(payload["framing_edge_cut_tolerance"])
        if payload.get("framing_min_person_ratio") is not None:
            framing["min_person_ratio"] = float(payload["framing_min_person_ratio"])
        if search:
            patch["search"] = search
        if face:
            patch["face"] = face
        if exclude:
            patch["exclude"] = exclude
        if framing:
            patch["framing"] = framing
        if isinstance(payload.get("sr"), dict):
            raw = payload["sr"]
            sr: dict[str, Any] = {}
            if raw.get("model"):
                sr["model"] = str(raw["model"])
            if raw.get("scale") is not None:
                scale = int(raw["scale"])
                if scale not in (2, 4):
                    raise HTTPException(400, "超分倍率只能是 2 或 4")
                sr["scale"] = scale
            if raw.get("face_restore") is not None:
                if str(raw["face_restore"]) not in SR_FACE_MODELS:
                    raise HTTPException(400, "不支持的面部修复模型")
                sr["face_restore"] = str(raw["face_restore"])
            if raw.get("face_strength") is not None:
                sr["face_strength"] = max(0.0, min(1.0, float(raw["face_strength"])))
            if raw.get("max_side") is not None:
                sr["max_side"] = max(0, int(raw["max_side"]))
            if raw.get("tile") is not None:
                sr["tile"] = max(0, int(raw["tile"]))
            if raw.get("jpeg_quality") is not None:
                sr["jpeg_quality"] = max(60, min(100, int(raw["jpeg_quality"])))
            # 原图始终保留：超分结果另存，绝不覆盖原文件。
            sr["keep_original"] = True
            patch["sr"] = sr
        for key in ("review_enabled", "output_dir", "proxy"):
            if key in payload:
                patch[key] = payload[key]
        store.update(patch)
        store.apply(cfg)
        if "exclude_dirs" in payload or "exclude_files" in payload:
            # 排除目录变了：重启监听（新目录立即开始索引，旧目录停止监听）
            _start_exclude_watcher()
        return store.describe()

    # ---------------- 人员 --------------
    @app.get("/api/people")
    def list_people():
        refs_map = discover_references(cfg, None)
        if cfg.people:
            for pc in cfg.people:
                refs_map.setdefault(pc.name, [])
        index = ImageIndex(cfg.index_path)
        try:
            people = [_person_payload(cfg, index, n, refs_map[n]) for n in sorted(refs_map)]
        finally:
            index.conn.close()
        return {"people": people, "defaults": {
            "count": cfg.search.count,
            "accept_threshold": cfg.face.accept_threshold,
            "review_threshold": cfg.face.review_threshold,
            "sources": cfg.search.sources,
            "min_face_px": cfg.face.min_face_px,
        }}

    @app.post("/api/people")
    def create_person(payload: dict = Body(...)):
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "人名不能为空")
        safe = _safe_name(name)
        path = cfg.faces_path / safe
        path.mkdir(parents=True, exist_ok=True)
        refs = discover_references(cfg, [name])
        index = ImageIndex(cfg.index_path)
        try:
            return _person_payload(cfg, index, name, refs.get(name, []))
        finally:
            index.conn.close()

    @app.post("/api/people/{name}/references")
    def upload_references(name: str, files: list[UploadFile] = File(...)):
        safe = _safe_name(name)
        folder = cfg.faces_path / safe
        folder.mkdir(parents=True, exist_ok=True)
        results: list[dict] = []
        for up in files:
            filename = _safe_name(Path(up.filename or "ref.jpg").name)
            if Path(filename).suffix.lower() not in IMAGE_SUFFIXES:
                filename = f"{Path(filename).stem or 'ref'}.jpg"
            dest = _unique(folder / filename)
            dest.write_bytes(up.file.read())
            try:
                results.append(_check_reference(dest, cfg))
            except Exception as exc:
                results.append({
                    "path": str(dest), "name": dest.name, "url": _file_url(dest),
                    "checked": True, "ok": False,
                    "issues": [{"code": "check_failed", "text": f"检查失败：{exc}"}],
                })
        return {"person": name, "results": results}

    @app.post("/api/people/{name}/check")
    def check_person(name: str):
        refs = discover_references(cfg, [name]).get(name, [])
        results = []
        for raw in refs:
            p = Path(raw)
            if p.exists():
                try:
                    results.append(_check_reference(p, cfg))
                except Exception as exc:
                    results.append({"path": str(p), "name": p.name, "checked": True, "ok": False,
                                    "issues": [{"code": "check_failed", "text": str(exc)}]})
        return {"person": name, "results": results}

    @app.put("/api/people/{name}")
    def update_person(name: str, payload: dict = Body(...)):
        data = store.load()
        people = data.setdefault("people", {})
        entry = people.setdefault(name, {})
        if "queries" in payload:
            q = payload["queries"]
            if isinstance(q, str):
                q = [x.strip() for x in q.replace("，", ",").split(",") if x.strip()]
            entry["queries"] = [str(x) for x in (q or [])]
        for key in ("count", "accept_threshold", "review_threshold"):
            if payload.get(key) is not None and payload.get(key) != "":
                entry[key] = payload[key]
        store.save(data)
        store.apply(cfg)
        refs = discover_references(cfg, [name]).get(name, [])
        index = ImageIndex(cfg.index_path)
        try:
            return _person_payload(cfg, index, name, refs)
        finally:
            index.conn.close()

    @app.delete("/api/ref")
    def delete_ref(path: str = Query(...)):
        p = Path(path)
        if not p.exists() or not _within(p, cfg.faces_path):
            raise HTTPException(400, "只能删除 faces 目录内的参考照")
        p.unlink()
        with _REF_LOCK:
            _REF_CACHE.pop(str(p), None)
        return {"ok": True}

    # ---------------- 任务 --------------
    @app.get("/api/job")
    def job_status():
        return jobs.status()

    @app.post("/api/job/start")
    def job_start(payload: dict = Body(default_factory=dict)):
        body = dict(payload or {})
        if body.get("names") == []:
            body["names"] = None
        if body.get("review_enabled") is None:
            body["review_enabled"] = bool(store.describe().get("review_enabled", True))
        try:
            return jobs.start(body)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/job/pause")
    def job_pause():
        return jobs.pause()

    @app.post("/api/job/resume")
    def job_resume():
        return jobs.resume()

    @app.post("/api/job/stop")
    def job_stop():
        return jobs.stop()

    @app.get("/api/job/logs")
    def job_logs(since: int = 0):
        return jobs.logs(since)

    @app.get("/api/run/status")
    def run_status():
        """站点实时统计别名（与 /api/job 相同，统一返回 sites 字段）。"""
        return jobs.status()

    @app.get("/api/job/history")
    def job_history(limit: int = Query(20, ge=1, le=100)):
        return {"runs": collect_run_history(cfg.log_path, limit)}

    @app.get("/api/run/history")
    def run_history(limit: int = Query(20, ge=1, le=100)):
        return {"runs": collect_run_history(cfg.log_path, limit)}

    # ---------------- 结果 --------------
    def _item(row) -> dict:
        path = Path(row["file_path"]) if row["file_path"] else None
        raw = Path(row["raw_path"]) if row["raw_path"] else None
        has_file = bool(path and path.exists())
        sr = Path(row["sr_path"]) if row["sr_path"] else None
        has_sr = bool(sr and sr.exists())
        replaced = int(row["sr_replaced"] or 0)
        # 缩略图/大图一律取「当前正式文件」：已替换走 SR 成品，撤销后回到原图；不热链远端 CDN
        current = sr if (replaced and has_sr) else (path if has_file else None)
        has_current = bool(current and current.exists())
        # 索引登记的正式文件不存在（且没有可用的 SR 替代）→ 前端明确标「文件缺失」，不静默显示
        missing = bool(row["file_path"]) and not has_file and not (replaced and has_sr)
        return {
            "id": row["id"],
            "person": row["person"],
            "source": row["source"],
            "query": row["query"],
            "url": row["url"],
            "page_url": row["page_url"],
            "file_path": row["file_path"],
            "filename": (current or path or raw).name if (current or path or raw) else "",
            "image_url": _file_url(current) if has_current else "",
            "thumb_url": (_thumb_url(current) if has_current else
                          (_thumb_url(raw) if raw and raw.exists() else "")),
            "raw_image_url": _file_url(raw) if raw and raw.exists() else "",
            "raw_thumb_url": _thumb_url(raw) if raw and raw.exists() else "",
            "width": row["width"],
            "height": row["height"],
            "similarity": row["similarity"],
            "clarity": row["clarity"],
            "framing": row["framing"],
            "person_ratio": row["person_ratio"],
            "framing_reason": row["framing_reason"],
            "reason": _reject_text(row),
            "reason_code": row["dedupe_stage"],
            "time": row["created_at"],
            "status": row["status"],
            "manual_accept": int(row["manual_accept"] or 0),
            "manual_accept_at": row["manual_accept_at"] or "",
            "manual_reason": row["manual_reason"] or "",
            "has_raw": bool(raw and raw.exists()),
            "file_missing": missing and row["status"] != "deleted",
            "sr_path": row["sr_path"] or "",
            "sr_model": row["sr_model"] or "",
            "sr_scale": int(row["sr_scale"] or 0),
            "sr_at": row["sr_at"] or "",
            "sr_face": row["sr_face"] or "",
            "sr_width": int(row["sr_width"] or 0),
            "sr_height": int(row["sr_height"] or 0),
            "has_sr": has_sr,
            "sr_image_url": _file_url(sr) if has_sr else "",
            "sr_thumb_url": _thumb_url(sr) if has_sr else "",
            "sr_replaced": int(row["sr_replaced"] or 0),
            "sr_replace_mode": row["sr_replace_mode"] or "",
            "backup_path": row["backup_path"] or "",
            "replaced_at": row["replaced_at"] or "",
            "can_undo_sr": bool(row["sr_replace_mode"] == "backup" and row["backup_path"]
                                and Path(row["backup_path"]).exists()),
        }

    @app.get("/api/results")
    def results(include_rejected: bool = Query(False), person: str = Query("")):
        index = ImageIndex(cfg.index_path)
        try:
            statuses = ("saved", "review") if not include_rejected else ("saved", "review", "rejected")
            marks = ",".join("?" * len(statuses))
            sql = f"SELECT DISTINCT person FROM images WHERE status IN ({marks})"
            params: list[Any] = list(statuses)
            if person:
                sql += " AND person=?"
                params.append(person)
            people = sorted({r["person"] for r in index.conn.execute(sql, params)})
            groups = []
            for name in people:
                saved = [_item(r) for r in index.rows(name, ("saved",))]
                saved = [x for x in saved if x["image_url"]]
                review = [_item(r) for r in index.rows(name, ("review",))]
                review = [x for x in review if x["image_url"]]
                group = {"person": name, "saved": saved, "review": review}
                if include_rejected:
                    rejected = [_item(r) for r in index.rows(name, ("rejected",))]
                    group["rejected"] = [x for x in rejected if x["framing"] or x["reason"]][:200]
                groups.append(group)
            return {"people": groups}
        finally:
            index.conn.close()

    @app.get("/api/gallery")
    def gallery(person: str = Query(""), status: str = Query("saved"),
                framing: str = Query(""), page: int = Query(1), page_size: int = Query(24),
                include_rejected: bool = Query(False)):
        """图片墙分页：每页只取缩略图，避免一次加载全部原图。

        屏蔽名单（data/blocklist.json）在这里生效：正常视图排除被屏蔽图片与人员，
        status=blocked 只返回被屏蔽图片（可在列表内解除）。
        """
        if status not in ("saved", "review", "rejected", "blocked"):
            raise HTTPException(400, "status 只能是 saved / review / rejected / blocked")
        page = max(1, int(page))
        page_size = max(1, min(200, int(page_size)))
        # 「已丢弃」视图合并 rejected / duplicate / excluded / discarded（重复、排除目录等）
        drop_statuses = ("rejected", "duplicate", "excluded", "discarded")
        statuses = drop_statuses if status == "rejected" else (status,)
        bl = _block_list(cfg)
        blocked_ids = bl.image_ids()
        blocked_people = bl.people()
        blocked_view = status == "blocked"
        if blocked_view:
            statuses = ("saved", "review", "rejected")
        index = ImageIndex(cfg.index_path)
        try:
            if blocked_view:
                ids_for_view = blocked_ids
                if not ids_for_view:
                    total, rows = 0, []
                else:
                    total = index.gallery_total(person, statuses, framing,
                                                include_ids=ids_for_view)
                    rows = index.gallery(person, statuses, framing,
                                         (page - 1) * page_size, page_size,
                                         include_ids=ids_for_view)
            else:
                total = index.gallery_total(person, statuses, framing,
                                            exclude_ids=blocked_ids,
                                            exclude_people=blocked_people)
                rows = index.gallery(person, statuses, framing, (page - 1) * page_size,
                                     page_size, exclude_ids=blocked_ids,
                                     exclude_people=blocked_people)
            rejected: list[dict] = []
            rejected_total = 0
            if include_rejected and not blocked_view and status != "rejected":
                rejected_total = index.gallery_total(person, drop_statuses, framing,
                                                     exclude_ids=blocked_ids,
                                                     exclude_people=blocked_people)
                rejected = [_item(r) for r in
                            index.gallery(person, drop_statuses, framing, 0, 60,
                                          exclude_ids=blocked_ids,
                                          exclude_people=blocked_people)]
            items = [_item(r) for r in rows]
            for it in items:
                it["blocked"] = int(it["id"]) in blocked_ids
            return {
                "person": person,
                "status": status,
                "page": page,
                "page_size": page_size,
                "total": total,
                "has_more": page * page_size < total,
                "items": items,
                "framing_counts": index.framing_counts(person, statuses,
                                                       exclude_ids=blocked_ids,
                                                       exclude_people=blocked_people),
                "rejected_total": rejected_total,
                "rejected": rejected,
                "blocked_total": len(blocked_ids),
                "blocked_people": sorted(blocked_people),
            }
        finally:
            index.conn.close()

    @app.get("/api/gallery/ids")
    def gallery_ids(person: str = Query(""), status: str = Query("saved"),
                    framing: str = Query("")):
        """跨页全选：返回当前筛选条件下的全部记录 id（上限 10000，超出时 truncated=true）。"""
        if status not in ("saved", "review", "rejected", "blocked"):
            raise HTTPException(400, "status 只能是 saved / review / rejected / blocked")
        drop_statuses = ("rejected", "duplicate", "excluded", "discarded")
        statuses = drop_statuses if status == "rejected" else (status,)
        bl = _block_list(cfg)
        blocked_ids = bl.image_ids()
        blocked_people = bl.people()
        if status == "blocked":
            statuses, include_ids = ("saved", "review", "rejected"), blocked_ids
            exclude_ids, exclude_people = None, None
        else:
            include_ids = None
            exclude_ids, exclude_people = blocked_ids, blocked_people
        index = ImageIndex(cfg.index_path)
        try:
            total = index.gallery_total(person, statuses, framing, exclude_ids=exclude_ids,
                                        exclude_people=exclude_people, include_ids=include_ids)
            ids = index.gallery_ids(person, statuses, framing, exclude_ids=exclude_ids,
                                    exclude_people=exclude_people, include_ids=include_ids)
        finally:
            index.conn.close()
        return {"ids": ids, "total": total, "truncated": len(ids) < total}

    # ---------------- 成品操作：删除（回收）/ 屏蔽 / 恢复 --------------
    @app.post("/api/gallery/delete")
    def gallery_delete(payload: dict = Body(...)):
        """把成品移入 data/recycle/<人名>/，索引标记 deleted；可选同时加入黑名单。"""
        ids = _payload_ids(payload)
        if not ids:
            raise HTTPException(400, "缺少 ids（要删除的图片记录 id）")
        do_block = bool(payload.get("block"))
        reason = str(payload.get("reason") or "手动删除")
        index = ImageIndex(cfg.index_path)
        deleted: list[dict] = []
        blocked: list[dict] = []
        records: list[dict] = []
        errors: list[str] = []
        try:
            for rid in ids:
                row = index.row_by_id(rid)
                if row is None:
                    errors.append(f"记录不存在：id={rid}")
                    continue
                if row["status"] == "deleted":
                    errors.append(f"记录已删除：id={rid}（{row['person']}）")
                    continue
                # 成品用 file_path；被丢弃的记录通常只在 _raw 里有文件，没有则仅改动索引
                src = Path(row["file_path"]) if row["file_path"] else None
                if src is not None and not src.exists():
                    src = None
                if src is None and row["raw_path"] and Path(row["raw_path"]).exists():
                    src = Path(row["raw_path"])
                keys = row.keys()
                ph = row["phash"]
                dh = row["dhash"] if "dhash" in keys else None
                if src is not None:
                    ph2, dh2 = _image_hashes(src)
                    ph = ph if ph is not None else ph2
                    dh = dh if dh is not None else dh2
                if src is None and not do_block and not row["url"] and ph is None and dh is None:
                    errors.append(f"文件与图片地址都缺失，无法删除：id={rid}（{row['person']}）")
                    continue
                recycle_path = ""
                if src is not None:
                    dest = _move_to_recycle(cfg, row["person"], src, row["sha256"] or "")
                    try:
                        shutil.move(str(src), str(dest))
                    except OSError as exc:
                        errors.append(f"移入回收目录失败：{src.name}（{exc}）")
                        continue
                    recycle_path = str(dest)
                index.mark_deleted(rid, recycle_path, prev_status=row["status"])
                deleted.append({"id": rid, "person": row["person"], "recycle_path": recycle_path,
                                "original_path": str(src) if src else ""})
                if do_block:
                    rec = _block_record(row)
                    rec["reason"] = reason
                    records.append(rec)
                    blocked.append({"id": rid, "url": row["url"]})
        finally:
            index.conn.close()
        if records:
            _block_list(cfg).add_images(records)
        if not deleted and errors:
            raise HTTPException(400, "；".join(errors))
        return {"deleted": deleted, "blocked": blocked, "errors": errors}

    @app.post("/api/gallery/block")
    def gallery_block(payload: dict = Body(...)):
        """保留文件，只把记录写入独立屏蔽名单；图片墙不再显示，后续采集不再拉回。"""
        ids = _payload_ids(payload)
        if not ids:
            raise HTTPException(400, "缺少 ids（要屏蔽的图片记录 id）")
        reason = str(payload.get("reason") or "手动屏蔽")
        index = ImageIndex(cfg.index_path)
        blocked: list[dict] = []
        errors: list[str] = []
        records: list[dict] = []
        try:
            for rid in ids:
                row = index.row_by_id(rid)
                if row is None:
                    errors.append(f"记录不存在：id={rid}")
                    continue
                if row["status"] == "deleted":
                    errors.append(f"记录已删除，请先恢复再屏蔽：id={rid}（{row['person']}）")
                    continue
                rec = _block_record(row)
                if not rec["url"] and rec["phash"] is None and rec["dhash"] is None:
                    errors.append(f"无法取得图片标识（文件缺失且无地址）：id={rid}")
                    continue
                rec["reason"] = reason
                records.append(rec)
                blocked.append({"id": rid, "url": row["url"]})
        finally:
            index.conn.close()
        if records:
            _block_list(cfg).add_images(records)
        if not blocked and errors:
            raise HTTPException(400, "；".join(errors))
        return {"blocked": blocked, "errors": errors}

    @app.post("/api/gallery/accept")
    def gallery_accept(payload: dict = Body(...)):
        """把 rejected / 其他丢弃记录手动改为通过：优先 raw_path，缺失时重新下载。"""
        ids = _payload_ids(payload)
        if not ids:
            raise HTTPException(400, "缺少 ids（要手动收录的图片记录 id）")
        raw_override = payload.get("manual_override_count", True)
        if isinstance(raw_override, str):
            override = raw_override.strip().lower() not in ("0", "false", "no", "off")
        else:
            override = bool(raw_override)
        index = ImageIndex(cfg.index_path)
        excl = ExcludeIndex(index, cfg.exclude)
        accepted: list[dict] = []
        errors: list[str] = []
        try:
            for rid in ids:
                row = index.row_by_id(rid)
                if row is None:
                    errors.append(f"记录不存在：id={rid}")
                    continue
                if row["status"] == "saved":
                    errors.append(f"记录已是成品，无需收录：id={rid}（{row['person']}）")
                    continue
                if row["status"] in ("deleted",):
                    errors.append(f"记录在回收站，请先恢复：id={rid}（{row['person']}）")
                    continue
                if not override:
                    task = cfg.build_task(row["person"])
                    if task.count and index.stats_for(row["person"])["saved"] >= int(task.count):
                        errors.append(
                            f"已达到 count 上限 {task.count}，本次未收录：id={rid}（{row['person']}）"
                        )
                        continue
                try:
                    data, raw_src, origin = _manual_bytes(cfg, row)
                except Exception as exc:
                    errors.append(f"读取原文件或重新下载失败：id={rid}（{row['person']}，{exc}）")
                    continue
                img = decode(data)
                if img is None:
                    errors.append(f"图片无法解码：id={rid}（{row['person']}）")
                    continue
                ph = phash(img)
                dh = dhash(img)
                sha = sha256_bytes(data)

                # 收录前再过一遍屏蔽 / 排除目录
                if index.blocked_url(row["url"]) is not None:
                    errors.append(f"该图片 URL 已在黑名单（屏蔽）：id={rid}（{row['person']}）")
                    continue
                bm = index.blocked_match(
                    ph, dh, cfg.dedupe.phash_threshold, cfg.exclude.dhash_threshold
                )
                if bm is not None:
                    _brow, method, dist = bm
                    errors.append(
                        f"内容与黑名单相似（{method} 距离 {dist}），拒绝收录：id={rid}（{row['person']}）"
                    )
                    continue
                hit = excl.lookup(sha, img, ph=ph, dh=dh)
                if hit is not None:
                    errors.append(
                        f"命中排除目录：{hit.path}（{hit.method}，距离 {hit.distance}），"
                        f"拒绝收录：id={rid}（{row['person']}）"
                    )
                    continue

                metrics = measure(img, data)
                dest_dir = cfg.output_path / row["person"]
                dest_dir.mkdir(parents=True, exist_ok=True)
                fname = (
                    f"{_safe_name(row['person'])}_{row['source']}_{ph:016x}_"
                    f"{metrics.width}x{metrics.height}{sniff_ext(data)}"
                )
                dest = _unique(dest_dir / fname)
                try:
                    if raw_src:
                        shutil.copyfile(raw_src, dest)
                    else:
                        dest.write_bytes(data)
                        raw_src = _save_raw(
                            cfg.raw_path, row["source"], row["person"], sha, data, metrics
                        )
                except OSError as exc:
                    errors.append(f"写入成品目录失败：{dest.name}（{exc}）")
                    continue

                now = datetime.now().isoformat(timespec="seconds")
                reason = _reject_text(row)
                index.update(
                    rid, status="saved", file_path=str(dest),
                    raw_path=raw_src or row["raw_path"], sha256=sha, phash=ph,
                    width=metrics.width, height=metrics.height, area=metrics.area,
                    sharpness=metrics.sharpness, compression=metrics.compression,
                    manual_accept=1, manual_accept_at=now, manual_reason=reason,
                )
                _write_person_metadata(row["person"], cfg.output_path / row["person"], index)
                _manual_log(
                    cfg, "accept", row, filename=dest.name, width=metrics.width,
                    height=metrics.height, from_raw=origin == "raw",
                    manual_reason=reason, manual_accept_at=now,
                )
                jobs.note_manual(row["person"], row["source"], 1)
                accepted.append({
                    "id": rid, "person": row["person"], "source": row["source"],
                    **_manual_file(dest), "width": metrics.width, "height": metrics.height,
                    "manual_reason": reason, "origin": origin,
                    "manual_accept_at": now,
                })
        finally:
            index.conn.close()
        if not accepted and errors:
            raise HTTPException(400, "；".join(errors))
        return {"accepted": accepted, "errors": errors}

    @app.post("/api/gallery/manual-undo")
    def gallery_manual_undo(payload: dict = Body(...)):
        """撤销手动收录：成品移入回收目录，索引改回 rejected，并清除 manual 标记。"""
        ids = _payload_ids(payload)
        if not ids:
            raise HTTPException(400, "缺少 ids（要撤销手动收录的图片记录 id）")
        index = ImageIndex(cfg.index_path)
        undone: list[dict] = []
        errors: list[str] = []
        try:
            for rid in ids:
                row = index.row_by_id(rid)
                if row is None:
                    errors.append(f"记录不存在：id={rid}")
                    continue
                if not int(row["manual_accept"] or 0):
                    errors.append(f"不是手动收录记录，无法撤销：id={rid}（{row['person']}）")
                    continue
                src = Path(row["file_path"]) if row["file_path"] else None
                if src is None or not src.exists():
                    errors.append(f"成品文件不存在，无法撤销：id={rid}（{row['file_path'] or '无路径'}）")
                    continue
                dest = _move_to_recycle(cfg, row["person"], src, row["sha256"] or "")
                try:
                    shutil.move(str(src), str(dest))
                except OSError as exc:
                    errors.append(f"移入回收目录失败：{src.name}（{exc}）")
                    continue
                now = datetime.now().isoformat(timespec="seconds")
                index.update(
                    rid, status="rejected", file_path="", recycle_path=str(dest),
                    manual_accept=0, manual_accept_at="", manual_reason="",
                )
                _write_person_metadata(row["person"], cfg.output_path / row["person"], index)
                _manual_log(
                    cfg, "undo", row, filename=dest.name, recycle_path=str(dest),
                    manual_accept_at=now,
                )
                jobs.note_manual(row["person"], row["source"], -1)
                undone.append({
                    "id": rid, "person": row["person"], "source": row["source"],
                    "recycle_path": str(dest), "filename": dest.name,
                })
        finally:
            index.conn.close()
        if not undone and errors:
            raise HTTPException(400, "；".join(errors))
        return {"undone": undone, "errors": errors}

    # ---------------- 超分：模型 / 任务 / 对比预览 ----------------
    sr_preview_lock = threading.Lock()

    @app.get("/api/sr/models")
    def sr_models():
        face_models = []
        for key, info in SR_FACE_MODELS.items():
            fname = info.get("filename") or ""
            path = cfg.path("models/sr") / fname if fname else None
            installed = bool(path and path.exists() and path.stat().st_size > 1_000_000)
            face_models.append({
                "id": key, "label": info.get("label", key),
                "installed": bool(installed or not fname),
                "size_mb": round(path.stat().st_size / 1024 / 1024, 1) if installed else 0,
                "source": info.get("source", ""),
            })
        return {
            "models": sr_engine.models(),
            "face_models": face_models,
            "weights_dir": str(cfg.path("models/sr")),
            "gpu": sr_engine.gpu_info(),
            "settings": _sr_settings(),
            "job": _sr_snapshot(),
            "output_dir": str(cfg.path("data/sr")),
        }

    @app.get("/api/sr/status")
    def sr_status():
        return _sr_snapshot()

    @app.post("/api/sr/plan")
    def sr_plan(payload: dict = Body(...)):
        """执行前统计：用于 UI 显示「将影响 N 张」，与 run 使用同一筛选逻辑。"""
        items, skipped = _sr_targets(payload)
        return {
            "count": len(items), "skipped": skipped,
            "items": [{"id": x["id"], "person": x["person"],
                       "filename": Path(x["path"]).name} for x in items],
        }

    @app.post("/api/sr/run")
    def sr_run(payload: dict = Body(...)):
        if jobs.busy:
            raise HTTPException(409, "下载任务正在运行，请等待完成后再进行超分（避免 GPU 争用）")
        if _sr_busy():
            raise HTTPException(409, "已有超分任务在运行，请等待完成或先取消")
        params = _sr_params(payload)
        items, skipped = _sr_targets(payload)
        if not items:
            raise HTTPException(400, "没有可超分的成品图片（仅支持 saved/review 且本地文件存在）")

        if params["replace_mode"] == "overwrite":
            need = "彻底覆盖" if len(items) == 1 else f"彻底覆盖{len(items)}"
            got = str(payload.get("confirm_word") or "").strip()
            if got != need:
                raise HTTPException(400, f"彻底覆盖不可恢复：请输入确认词「{need}」")

        stop = threading.Event()
        sr_job["stop"] = stop
        quick = bool(payload.get("quick_action"))
        with sr_lock:
            sr_state.update({
                "state": "running", "total": len(items), "done": 0, "failed": 0,
                "current": "", "pct": 0.0, "model": params["model"],
                "scale": params["scale"], "face_restore": params["face_restore"],
                "replace_mode": params["replace_mode"], "quick_action": quick,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "finished_at": "", "elapsed": 0.0, "error": "", "tile": "",
                "items": [], "logs": [], "skipped": skipped,
            })

        def work(batch: list[dict], prm: dict, stop_evt: threading.Event) -> None:
            index = ImageIndex(cfg.index_path)
            engine = SuperResolver(cfg.path("models/sr"))
            changed: set[str] = set()
            t0 = time.time()
            cancelled = False
            try:
                for i, item in enumerate(batch):
                    if stop_evt.is_set():
                        cancelled = True
                        break
                    idx = i
                    with sr_lock:
                        sr_state["current"] = f"{item['person']} · {Path(item['path']).name}"

                    def on_tile(done: int, total: int, _i=idx) -> None:
                        if stop_evt.is_set():
                            raise _SrCancelled()
                        with sr_lock:
                            frac = done / total if total else 1.0
                            sr_state["pct"] = round((_i + frac) / len(batch) * 100, 1)
                            sr_state["tile"] = f"{done}/{total}"

                    row = index.row_by_id(item["id"])
                    if row is None:
                        with sr_lock:
                            sr_state["failed"] += 1
                            sr_state["items"].append({"id": item["id"], "ok": False,
                                                      "error": "记录不存在", "person": item["person"]})
                        continue
                    try:
                        if prm["replace_mode"] in ("backup", "overwrite") \
                                and (row["sr_replace_mode"] or "") in ("backup", "overwrite") \
                                and str(row["file_path"]) == str(row["sr_path"] or ""):
                            raise RuntimeError("该图已完成超分替换；如需重跑请先撤销，或改用「超分」另存")
                        img = read_image(Path(item["path"]))
                        if img is None:
                            raise RuntimeError("图片无法解码")
                        detect = _sr_detect if prm["face_restore"] != "none" else None
                        result = engine.upscale(
                            img, prm["model"], scale=prm["scale"], tile=prm["tile"],
                            max_side=prm["max_side"], face_restore=prm["face_restore"],
                            face_strength=prm["face_strength"], detect_faces=detect,
                            on_tile=on_tile,
                        )
                        out_path = _sr_output_path(index, row, prm)
                        write_image(out_path, result["image"], quality=prm["jpeg_quality"])
                        replace_fields = _sr_replace(index, row, out_path, prm["replace_mode"])
                        now = datetime.now().isoformat(timespec="seconds")
                        index.update(
                            int(row["id"]), sr_path=str(out_path), sr_model=prm["model"],
                            sr_scale=prm["scale"], sr_at=now, sr_face=prm["face_restore"],
                            sr_width=result["width"], sr_height=result["height"],
                            **replace_fields,
                        )
                        _sr_replace_log(row, prm["replace_mode"], out_path, replace_fields,
                                        quick=quick)
                        changed.add(row["person"])
                        with sr_lock:
                            sr_state["done"] += 1
                            sr_state["items"].append({
                                "id": int(row["id"]), "ok": True, "person": row["person"],
                                "path": str(out_path), "image_url": _file_url(out_path),
                                "thumb_url": _thumb_url(out_path),
                                "width": result["width"], "height": result["height"],
                                "input_width": result["input_width"],
                                "input_height": result["input_height"],
                                "elapsed": result["elapsed"], "tile": result["tile"],
                                "peak_vram_gb": result["peak_vram_gb"],
                                "faces": result["faces"], "error": "",
                                "replace_mode": prm["replace_mode"],
                                "replaced": prm["replace_mode"] != "keep",
                            })
                            sr_state["pct"] = round((i + 1) / len(batch) * 100, 1)
                        _sr_log(f"完成 {row['person']} · {Path(item['path']).name}"
                                f" → {out_path.name}（{result['elapsed']}s，tile {result['tile']}）")
                    except _SrCancelled:
                        cancelled = True
                        break
                    except Exception as exc:
                        with sr_lock:
                            sr_state["failed"] += 1
                            sr_state["items"].append({
                                "id": item["id"], "ok": False, "person": item["person"],
                                "error": f"{exc.__class__.__name__}: {exc}",
                            })
                        _sr_log(f"失败 {item['person']} · {Path(item['path']).name}：{exc}")
            except BaseException as exc:  # 任务线程不能静默死掉
                with sr_lock:
                    sr_state["error"] = f"{exc.__class__.__name__}: {exc}"
                _sr_log(f"超分任务异常：{exc}")
            finally:
                for name in changed:
                    try:
                        _write_person_metadata(name, cfg.output_path / name, index)
                    except Exception as exc:
                        _sr_log(f"更新 metadata 失败（{name}）：{exc}")
                index.conn.close()
                engine.close()
                with sr_lock:
                    sr_state["elapsed"] = round(time.time() - t0, 2)
                    sr_state["finished_at"] = datetime.now().isoformat(timespec="seconds")
                    sr_state["current"] = ""
                    sr_state["pct"] = 100.0 if not cancelled and not sr_state["error"] else sr_state["pct"]
                    sr_state["state"] = "stopped" if cancelled else (
                        "error" if sr_state["error"] else "finished")

        thread = threading.Thread(target=work, args=([dict(x) for x in items], dict(params), stop),
                                  daemon=True, name="facefind-sr")
        sr_job["thread"] = thread
        thread.start()
        return _sr_snapshot()

    @app.post("/api/sr/cancel")
    def sr_cancel():
        if not _sr_busy():
            return {"cancelled": False, "message": "当前没有运行中的超分任务", "job": _sr_snapshot()}
        stop = sr_job.get("stop")
        if isinstance(stop, threading.Event):
            stop.set()
        with sr_lock:
            sr_state["state"] = "stopping"
        _sr_log("收到取消请求，将在当前分块结束后停止")
        return {"cancelled": True, "message": "已请求取消，当前分块结束后停止", "job": _sr_snapshot()}

    @app.post("/api/sr/undo")
    def sr_undo(payload: dict = Body(...)):
        """撤销 backup 档替换：原图回位、SR 文件回收，记录恢复成未超分状态。"""
        if _sr_busy():
            raise HTTPException(409, "超分任务运行中，请等待完成后再撤销")
        ids = _payload_ids(payload)
        if not ids:
            raise HTTPException(400, "缺少 ids（要撤销替换的记录）")
        index = ImageIndex(cfg.index_path)
        changed: set[str] = set()
        errors: list[dict] = []
        restored = 0
        try:
            for rid in ids:
                row = index.row_by_id(rid)
                if row is None:
                    errors.append({"id": rid, "error": "记录不存在"})
                    continue
                if (row["sr_replace_mode"] or "") != "backup" or not row["backup_path"]:
                    errors.append({"id": rid, "error": "该记录不是可撤销的备份替换"})
                    continue
                bak = Path(row["backup_path"])
                dest = Path(row["sr_original_path"]) if row["sr_original_path"] else None
                if not bak.exists():
                    errors.append({"id": rid, "error": "备份文件不存在，无法撤销"})
                    continue
                if dest is None or not dest.parent.exists():
                    errors.append({"id": rid, "error": "原图落点目录不存在，无法撤销"})
                    continue
                if dest.exists():
                    errors.append({"id": rid, "error": f"原路径已有文件，拒绝覆盖：{dest.name}"})
                    continue
                old_file = Path(row["file_path"]) if row["file_path"] else None
                sr = Path(row["sr_path"]) if row["sr_path"] else None
                # 缓存键含「路径+mtime+大小」，必须在移动前清，否则路径已失效算不出键
                _purge_thumbs(cfg, [p for p in (old_file, sr) if p])
                shutil.move(str(bak), str(dest))
                if sr and sr.exists():
                    dump = cfg.path("data/recycle/sr_discard") / _safe_name(row["person"] or "")
                    dump.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(sr), str(_unique(
                        dump / f"{sr.stem}_{datetime.now():%Y%m%d_%H%M%S}{sr.suffix}")))
                _purge_thumbs(cfg, [dest])   # 原图回位后清掉其同名缓存，回到原图缩略图
                index.update(
                    rid, file_path=str(dest), sr_path="", sr_model="", sr_scale=0,
                    sr_at="", sr_face="", sr_width=0, sr_height=0, sr_replaced=0,
                    sr_replace_mode="", backup_path="", replaced_at="", sr_original_path="",
                )
                changed.add(row["person"])
                restored += 1
            for name in changed:
                try:
                    _write_person_metadata(name, cfg.output_path / name, index)
                except Exception as exc:
                    errors.append({"person": name, "error": f"更新 metadata 失败：{exc}"})
        finally:
            index.conn.close()
        if restored == 0 and errors:
            raise HTTPException(400, errors[0].get("error") or "撤销失败")
        return {"ok": True, "restored": restored, "errors": errors}

    @app.post("/api/sr/preview")
    def sr_preview(payload: dict = Body(...)):
        """单图对比预览：不写入索引，只把结果放到 data/sr_preview 供并排查看。"""
        if jobs.busy:
            raise HTTPException(409, "下载任务正在运行，请等待完成后再做对比预览")
        if _sr_busy():
            raise HTTPException(409, "超分批量任务正在运行，请等待完成或先取消")
        ids = _payload_ids(payload)
        if len(ids) != 1:
            raise HTTPException(400, "对比预览一次只能指定 1 张图片")
        params = _sr_params(payload)
        index = ImageIndex(cfg.index_path)
        try:
            row = index.row_by_id(ids[0])
        finally:
            index.conn.close()
        if row is None:
            raise HTTPException(404, "记录不存在")
        src = Path(row["file_path"]) if row["file_path"] else None
        if src is None or not src.exists():
            raise HTTPException(400, "该记录没有本地成品文件，无法超分")
        img = read_image(src)
        if img is None:
            raise HTTPException(400, "图片无法解码")
        detect = _sr_detect if params["face_restore"] != "none" else None
        with sr_preview_lock:
            result = sr_engine.upscale(
                img, params["model"], scale=params["scale"], tile=params["tile"],
                max_side=params["max_side"], face_restore=params["face_restore"],
                face_strength=params["face_strength"], detect_faces=detect,
            )
        face_part = "" if params["face_restore"] == "none" else f"_{params['face_restore']}"
        out_dir = cfg.path("data/sr_preview")
        out_path = out_dir / f"id{row['id']}_{params['short']}_sr{params['scale']}x{face_part}.jpg"
        write_image(out_path, result["image"], quality=params["jpeg_quality"])
        return {
            "id": int(row["id"]), "person": row["person"],
            "model": params["model"], "scale": params["scale"],
            "face_restore": params["face_restore"],
            "original_url": _file_url(src), "original_thumb_url": _thumb_url(src),
            "sr_url": _file_url(out_path), "sr_path": str(out_path),
            "width": result["width"], "height": result["height"],
            "input_width": result["input_width"], "input_height": result["input_height"],
            "original_width": int(img.shape[1]), "original_height": int(img.shape[0]),
            "original_bytes": src.stat().st_size, "sr_bytes": out_path.stat().st_size,
            "elapsed": result["elapsed"], "peak_vram_gb": result["peak_vram_gb"],
            "tile": result["tile"], "faces": result["faces"],
            "face_boxes": result.get("face_boxes") or [],
        }

    @app.get("/api/blocked")
    def blocked_list():
        index = ImageIndex(cfg.index_path)
        try:
            items = [_blocked_payload(r, index) for r in index.blocked_rows()]
        finally:
            index.conn.close()
        return {"items": items, "total": len(items)}

    @app.post("/api/blocked/remove")
    def blocked_remove(payload: dict = Body(...)):
        index = ImageIndex(cfg.index_path)
        try:
            bid = payload.get("id")
            url = str(payload.get("url") or "")
            if bid is not None:
                ok = index.block_remove(int(bid))
            elif url:
                ok = index.block_remove_url(url)
            else:
                raise HTTPException(400, "缺少 id 或 url")
        finally:
            index.conn.close()
        if not ok:
            raise HTTPException(404, "黑名单中没有该条目")
        return {"ok": True}

    # ---------------- 屏蔽名单（独立文件，图片级 / 人员级） ----------------
    @app.get("/api/block/list")
    def block_list():
        bl = _block_list(cfg)
        index = ImageIndex(cfg.index_path)
        try:
            images = []
            for ent in bl.images():
                row = index.row_by_id(int(ent.get("id") or 0))
                item = _item(row) if row is not None else None
                images.append({
                    **{k: ent.get(k) for k in ("id", "person", "filename", "url", "reason", "blocked_at", "source")},
                    "status": (row["status"] if row is not None else "missing"),
                    "thumb_url": item["thumb_url"] if item else "",
                    "image_url": item["image_url"] if item else "",
                    "exists": bool(item and (item["image_url"] or item["thumb_url"])),
                })
            people = []
            for ent in bl.people_entries():
                st = index.stats_for(ent["person"])
                people.append({**ent, "saved": int(st["saved"]), "review": int(st["review"])})
        finally:
            index.conn.close()
        return {"images": images, "people": people,
                "total": len(images) + len(people),
                "file": str(cfg.path("data/blocklist.json"))}

    @app.post("/api/block/images")
    def block_images(payload: dict = Body(...)):
        """图片级屏蔽：只登记标识，不移动/删除文件。"""
        ids = _payload_ids(payload)
        if not ids:
            raise HTTPException(400, "缺少 ids（要屏蔽的图片记录 id）")
        reason = str(payload.get("reason") or "手动屏蔽")
        index = ImageIndex(cfg.index_path)
        records: list[dict] = []
        errors: list[str] = []
        try:
            for rid in ids:
                row = index.row_by_id(rid)
                if row is None:
                    errors.append(f"记录不存在：id={rid}")
                    continue
                if row["status"] == "deleted":
                    errors.append(f"记录已删除（在回收站），请先恢复再屏蔽：id={rid}")
                    continue
                rec = _block_record(row)
                if not rec["url"] and rec["phash"] is None and rec["dhash"] is None:
                    errors.append(f"无法取得图片标识：id={rid}")
                    continue
                rec["reason"] = reason
                records.append(rec)
        finally:
            index.conn.close()
        if not records and errors:
            raise HTTPException(400, "；".join(errors))
        added = _block_list(cfg).add_images(records)
        return {"blocked": [x["id"] for x in added], "errors": errors}

    @app.post("/api/block/people")
    def block_people(payload: dict = Body(...)):
        """人员级屏蔽：默认只影响显示与采集；with_files=true 时才把该人文件移入回收站。"""
        raw = payload.get("people") if "people" in payload else payload.get("person")
        if isinstance(raw, str):
            names = [raw]
        elif isinstance(raw, (list, tuple)):
            names = [str(x) for x in raw]
        else:
            names = []
        names = list(dict.fromkeys(n.strip() for n in names if n and n.strip()))
        if not names:
            raise HTTPException(400, "缺少 person / people（要屏蔽的人员）")
        with_files = bool(payload.get("with_files"))
        bl = _block_list(cfg)
        reason = str(payload.get("reason") or "手动屏蔽人员")
        added = bl.add_people(names, reason=reason)
        moved = 0
        errors: list[str] = []
        if with_files:
            index = ImageIndex(cfg.index_path)
            try:
                for name in names:
                    for row in index.rows(name, ("saved", "review")):
                        src = Path(row["file_path"]) if row["file_path"] else None
                        if src is None or not src.exists():
                            continue
                        try:
                            dest = _move_to_recycle(cfg, name, src, row["sha256"] or "")
                            shutil.move(str(src), str(dest))
                            index.mark_deleted(int(row["id"]), str(dest),
                                               prev_status=row["status"])
                            moved += 1
                        except OSError as exc:
                            errors.append(f"{name} · {src.name}：{exc}")
            finally:
                index.conn.close()
        return {"people": added, "moved": moved, "errors": errors}

    @app.post("/api/block/unblock")
    def block_unblock(payload: dict = Body(...)):
        """解除屏蔽：只删名单条目，不动文件；解除后图片立即恢复显示。"""
        bl = _block_list(cfg)
        if bool(payload.get("all")):
            images = bl.image_ids()
            people = bl.people()
            return {"removed_images": bl.remove_images(images),
                    "removed_people": bl.remove_people(people)}
        ids = _payload_ids(payload)
        raw = payload.get("people") if "people" in payload else payload.get("person")
        names = [raw] if isinstance(raw, str) else ([str(x) for x in raw] if isinstance(raw, (list, tuple)) else [])
        names = [n.strip() for n in names if n and n.strip()]
        if not ids and not names:
            raise HTTPException(400, "缺少 ids 或 people（要解除屏蔽的条目）")
        return {"removed_images": bl.remove_images(ids), "removed_people": bl.remove_people(names)}

    @app.get("/api/recycle")
    def recycle_list(person: str = Query("")):
        index = ImageIndex(cfg.index_path)
        try:
            items = []
            for r in index.deleted_rows(person):
                rec = Path(r["recycle_path"]) if r["recycle_path"] else None
                items.append({
                    "id": r["id"],
                    "person": r["person"],
                    "filename": Path(r["file_path"]).name if r["file_path"] else "",
                    "original_path": r["file_path"] or "",
                    "recycle_path": r["recycle_path"] or "",
                    "deleted_at": r["deleted_at"] or "",
                    "prev_status": r["prev_status"] or "",
                    "width": r["width"],
                    "height": r["height"],
                    "url": r["url"],
                    "exists": bool(rec and rec.exists()),
                    "thumb_url": _thumb_url(rec) if rec and rec.exists() else "",
                    "image_url": _file_url(rec) if rec and rec.exists() else "",
                })
        finally:
            index.conn.close()
        return {"items": items, "total": len(items)}

    @app.post("/api/recycle/restore")
    def recycle_restore(payload: dict = Body(...)):
        ids = _payload_ids(payload)
        if not ids:
            raise HTTPException(400, "缺少 ids（要恢复的图片记录 id）")
        index = ImageIndex(cfg.index_path)
        restored: list[dict] = []
        errors: list[str] = []
        try:
            for rid in ids:
                row = index.row_by_id(rid)
                if row is None:
                    errors.append(f"记录不存在：id={rid}")
                    continue
                if row["status"] != "deleted":
                    errors.append(f"记录未被删除，无需恢复：id={rid}")
                    continue
                src = Path(row["recycle_path"]) if row["recycle_path"] else None
                if src is None or not src.exists():
                    errors.append(f"回收目录中的文件不存在：id={rid}（{row['recycle_path'] or '无路径'}）")
                    continue
                dest_dir = cfg.output_path / _safe_name(row["person"])
                dest_dir.mkdir(parents=True, exist_ok=True)
                name = Path(row["file_path"]).name if row["file_path"] else src.name.split("__")[0] + src.suffix
                dest = _unique(dest_dir / name)
                try:
                    shutil.move(str(src), str(dest))
                except OSError as exc:
                    errors.append(f"恢复失败：{name}（{exc}）")
                    continue
                status = row["prev_status"] or "saved"
                index.restore_row(rid, str(dest), status=status)
                restored.append({"id": rid, "person": row["person"], "file_path": str(dest),
                                  "status": status})
        finally:
            index.conn.close()
        if not restored and errors:
            raise HTTPException(400, "；".join(errors))
        return {"restored": restored, "errors": errors}

    # ---------------- 存储容量 --------------
    @app.get("/api/storage")
    def storage_stats():
        thumb_dir = cfg.root / "data" / "thumbs"
        recycle = _recycle_dir(cfg)
        db_files = [cfg.index_path] + [p for p in cfg.index_path.parent.glob("index.db*") if p.is_file()]
        db_metrics = _sum_metrics([_metrics(p) for p in db_files])
        data_metrics = _sum_metrics([db_metrics, _metrics(cfg.log_path)])
        sections = [
            {**_metrics(cfg.output_path), "key": "downloads", "label": "成品（含待复核）",
             "path": str(cfg.output_path)},
            {**_metrics(cfg.raw_path), "key": "raw", "label": "原始下载 _raw",
             "path": str(cfg.raw_path)},
            {**_metrics(recycle), "key": "recycle", "label": "回收目录 recycle",
             "path": str(recycle)},
            {**_metrics(thumb_dir), "key": "thumbs", "label": "缩略图缓存",
             "path": str(thumb_dir)},
            {**_metrics(cfg.browser_profile_path), "key": "browser", "label": "浏览器档案",
             "path": str(cfg.browser_profile_path)},
            {**data_metrics, "key": "data", "label": "索引库与日志（db/logs）",
             "path": str(cfg.index_path.parent)},
        ]

        names: set[str] = {pc.name for pc in cfg.people if pc.name}
        out_dir = cfg.output_path
        if out_dir.is_dir():
            names |= {d.name for d in out_dir.iterdir() if d.is_dir() and d.name != "review"}
        review_dir = out_dir / "review"
        if review_dir.is_dir():
            names |= {d.name for d in review_dir.iterdir() if d.is_dir()}
        for d in _raw_dirs_for(cfg, ""):
            if d.is_dir():
                names |= {x.name for x in d.iterdir() if x.is_dir()}
        if recycle.is_dir():
            names |= {d.name for d in recycle.iterdir() if d.is_dir()}

        people = []
        for name in sorted(names):
            saved = _metrics(out_dir / _safe_name(name))
            review = _metrics(review_dir / _safe_name(name))
            raw = _sum_metrics([_metrics(d / _safe_name(name)) for d in _raw_dirs_for(cfg, "")])
            rec = _metrics(recycle / _safe_name(name))
            people.append({
                "name": name, "saved": saved, "review": review, "raw": raw, "recycle": rec,
                "total": _sum_metrics([saved, review, raw, rec]),
            })
        people.sort(key=lambda x: int(x["total"]["size"]), reverse=True)

        total = {
            "size": sum(int(s["size"]) for s in sections),
            "files": sum(int(s["files"]) for s in sections),
            "mtime": max((s["mtime"] for s in sections if s["mtime"]), default=""),
        }
        return {"root": str(cfg.root), "total": total, "sections": sections, "people": people}

    @app.post("/api/storage/clear")
    def storage_clear(payload: dict = Body(...)):
        """按类别/按人清理；仅允许项目内已知子目录，清理后同步索引。"""
        category = str(payload.get("category") or "")
        person = str(payload.get("person") or "")
        confirm = str(payload.get("confirm") or "")
        if person and (".." in person or person in (".",)):
            raise HTTPException(400, "人员名称不合法")
        index = ImageIndex(cfg.index_path)
        try:
            if category == "downloads":
                if not person:
                    raise HTTPException(400, "清理成品必须指定人员（不支持全局一键清空）")
                if confirm != person:
                    raise HTTPException(400, f"请完整输入人名「{person}」以确认清理")
                targets = [t for t in (cfg.output_path / _safe_name(person),
                                       cfg.output_path / "review" / _safe_name(person)) if t.exists()]
                if not targets:
                    raise HTTPException(400, f"该人员没有成品目录：{person}")
                for t in targets:
                    _assert_clearable(cfg, t)
                files = [p for t in targets for p in t.rglob("*") if p.is_file()]
                estimate = _sum_metrics([_metrics(t) for t in targets])
                freed_extra = 0
                for c in _thumb_cache_paths(cfg.root / "data" / "thumbs", files):
                    try:
                        freed_extra += c.stat().st_size
                        c.unlink()
                    except OSError:
                        pass
                removed, freed = 0, 0
                for t in targets:
                    n, b = _delete_tree(t, keep_base=False)
                    removed += n
                    freed += b
                rows = index.delete_person(person)
                return {"ok": True, "category": category, "person": person,
                        "files": removed, "freed": freed + freed_extra,
                        "estimate": estimate, "index_removed": rows,
                        "message": f"已清理「{person}」成品 {removed} 个文件，索引删除 {rows} 条"}

            if category == "raw":
                targets = _raw_dirs_for(cfg, person)
                if not targets:
                    raise HTTPException(400, "没有找到 _raw 文件" + (f"（{person}）" if person else ""))
                for t in targets:
                    _assert_clearable(cfg, t)
                rows = index.raw_rows()
                ids = [int(r["id"]) for r in rows
                       if r["raw_path"] and any(_within(Path(r["raw_path"]), t) for t in targets)]
                estimate = _sum_metrics([_metrics(t) for t in targets])
                removed, freed = 0, 0
                for t in targets:
                    n, b = _delete_tree(t, keep_base=False)
                    removed += n
                    freed += b
                cleared = index.clear_raw_paths(ids)
                return {"ok": True, "category": category, "person": person,
                        "files": removed, "freed": freed, "estimate": estimate,
                        "index_removed": 0, "index_cleared": cleared,
                        "message": f"已清空 _raw {removed} 个文件，索引 raw_path 清空 {cleared} 条"}

            if category == "recycle":
                if confirm != "永久删除":
                    raise HTTPException(400, "清空回收站为永久删除，请填写确认词「永久删除」")
                root_dir = _recycle_dir(cfg)
                target = root_dir / _safe_name(person) if person else root_dir
                if not target.exists():
                    raise HTTPException(400, "回收目录为空" + (f"（{person}）" if person else ""))
                _assert_clearable(cfg, target)
                rows = index.deleted_with_recycle()
                ids = [int(r["id"]) for r in rows
                       if r["recycle_path"] and _within(Path(r["recycle_path"]), target)]
                estimate = _metrics(target)
                removed, freed = _delete_tree(target, keep_base=not person)
                deleted = index.delete_ids(ids)
                return {"ok": True, "category": category, "person": person,
                        "files": removed, "freed": freed, "estimate": estimate,
                        "index_removed": deleted,
                        "message": f"已永久删除回收站 {removed} 个文件，索引删除 {deleted} 条"}

            if category == "thumbs":
                target = cfg.root / "data" / "thumbs"
                _assert_clearable(cfg, target)
                estimate = _metrics(target)
                removed, freed = _delete_tree(target, keep_base=True)
                return {"ok": True, "category": category, "files": removed, "freed": freed,
                        "estimate": estimate, "index_removed": 0,
                        "message": f"已清空缩略图缓存 {removed} 个文件（访问时按需重建）"}

            if category == "logs":
                target = cfg.log_path
                _assert_clearable(cfg, target)
                estimate = _metrics(target)
                removed, freed = _delete_tree(target, keep_base=True)
                return {"ok": True, "category": category, "files": removed, "freed": freed,
                        "estimate": estimate, "index_removed": 0,
                        "message": f"已清空日志 {removed} 个文件"}

            raise HTTPException(
                400, "不支持清理该类别；index.db 与浏览器档案不允许清理（会导致全量重扫/需重新登录）"
            )
        finally:
            index.conn.close()

    @app.get("/api/overview")
    def overview():
        """主页概览：人员统计、最近运行、账号与索引健康。"""
        refs_map = discover_references(cfg, None)
        if cfg.people:
            for pc in cfg.people:
                refs_map.setdefault(pc.name, [])
        index = ImageIndex(cfg.index_path)
        try:
            stats = index.people_overview()
            summary = index.summary()
        finally:
            index.conn.close()
        people = []
        for name in sorted(refs_map):
            refs = [_basic_ref(Path(p)) for p in refs_map[name] if Path(p).exists()]
            stat = stats.get(name) or {}
            people.append({
                "name": name,
                "saved": stat.get("saved", 0),
                "review": stat.get("review", 0),
                "rejected": stat.get("rejected", 0),
                "last_run": stat.get("last_run", ""),
                "refs": _ref_quality(refs),
            })
        return {
            "people": people,
            "summary": summary,
            "job": jobs.status(),
            "auth": _auth_payload(),
            "browser": browser.info(cfg),
            "index": {"path": str(cfg.index_path), "exists": cfg.index_path.exists()},
            "backup": backup_msg or "",
        }

    @app.get("/api/people/{name}/detail")
    def person_detail(name: str):
        refs = discover_references(cfg, [name]).get(name, [])
        index = ImageIndex(cfg.index_path)
        try:
            payload = _person_payload(cfg, index, name, refs)
        finally:
            index.conn.close()
        payload["logs"] = _person_logs(cfg, name)
        return payload

    @app.post("/api/review/decide")
    def review_decide(payload: dict = Body(...)):
        action = str(payload.get("action") or "")
        if action not in ("accept", "discard"):
            raise HTTPException(400, "action 只能是 accept 或 discard")
        items = payload.get("items") or []
        index = ImageIndex(cfg.index_path)
        review_root = cfg.output_path / "review"
        moved = 0
        errors: list[str] = []
        try:
            for it in items:
                person = str(it.get("person") or "").strip()
                raw = str(it.get("file_path") or "").strip()
                if not person or not raw:
                    continue
                src = Path(raw)
                if not src.exists() or not _within(src, review_root):
                    errors.append(f"跳过（不在 review 目录）：{raw}")
                    continue
                row = next(
                    (r for r in index.rows(person, ("review",))
                     if r["file_path"] and Path(r["file_path"]).resolve() == src.resolve()),
                    None,
                )
                if action == "accept":
                    dest_dir = cfg.output_path / person
                    status = "saved"
                else:
                    dest_dir = cfg.raw_path / "discarded" / person
                    status = "discarded"
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = _unique(dest_dir / src.name)
                try:
                    shutil.move(str(src), str(dest))
                except OSError as exc:
                    errors.append(f"移动失败：{src.name}（{exc}）")
                    continue
                if row is not None:
                    index.update(row["id"], status=status, file_path=str(dest))
                moved += 1
        finally:
            index.conn.close()
        return {"moved": moved, "errors": errors}

    @app.post("/api/review/rerun")
    def review_rerun(payload: dict = Body(...)):
        person = str(payload.get("person") or "").strip()
        if not person:
            raise HTTPException(400, "缺少人名")
        body = {
            "names": [person],
            "count": payload.get("count"),
            "review_enabled": True,
            "sources": payload.get("sources"),
        }
        try:
            return jobs.start(body)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/export")
    def export_person(payload: dict = Body(...)):
        person = str(payload.get("person") or "").strip()
        if not person:
            raise HTTPException(400, "缺少人名")
        index = ImageIndex(cfg.index_path)
        try:
            path = build_overview(person, cfg, index)
        finally:
            index.conn.close()
        return {"person": person, "path": str(path), "url": _file_url(path)}

    @app.get("/api/calibrate")
    def calibrate(person: str = Query(...), limit: int = 100):
        if not person:
            raise HTTPException(400, "缺少人名")
        return collect(person, cfg, limit=limit)

    # ---------------- 排除库 --------------
    @app.get("/api/exclude")
    def exclude_state():
        index = ImageIndex(cfg.index_path)
        try:
            rows = index.all_exclude_rows()
        finally:
            index.conn.close()
        roots = []
        for d in cfg.exclude.dirs:
            dpath = str(cfg.path(d) if not Path(d).is_absolute() else Path(d))
            norm = os.path.normcase(dpath)
            count = sum(1 for r in rows if os.path.normcase(r["path"]).startswith(norm))
            roots.append({"path": dpath, "raw": d, "files": count})
        total_size = sum(int(r["size"] or 0) for r in rows)
        return {
            "dirs": cfg.exclude.dirs,
            "files": cfg.exclude.files,
            "phash_threshold": cfg.exclude.phash_threshold,
            "dhash_threshold": cfg.exclude.dhash_threshold,
            "crop_tolerance": cfg.exclude.crop_tolerance,
            "indexed": len(rows),
            "roots": roots,
            "total_size": total_size,
            "db_path": str(cfg.index_path),
            "rebuild": _EXCLUDE_STATE,
            "watch": _watch_status(),
            "scan_on_run": cfg.exclude.scan_on_run,
            "last_decision": last_scan_decision(),
        }

    @app.post("/api/exclude/remove")
    def exclude_remove(payload: dict = Body(...)):
        """从排除配置里取消一项目录/文件引用，并清掉对应的排除索引行。

        只取消配置与索引引用，绝不删除磁盘上的目录或文件。
        """
        kind = str(payload.get("kind") or "")
        value = str(payload.get("value") or "").strip()
        if kind not in ("dir", "file"):
            raise HTTPException(400, "kind 只能是 dir 或 file")
        if not value:
            raise HTTPException(400, "缺少要移除的路径")
        data = store.load()
        excl = data.get("exclude") or {}
        key = "dirs" if kind == "dir" else "files"
        current = [str(x) for x in (excl.get(key) or [])]
        if value not in current:
            raise HTTPException(404, "该条目已不在配置中（可能已被其它页面移除）")
        rest = [x for x in current if x != value]
        store.update({"exclude": {key: rest}})
        store.apply(cfg)
        target = str(cfg.path(value) if not Path(value).is_absolute() else Path(value))
        index = ImageIndex(cfg.index_path)
        try:
            pruned = index.delete_exclude_rows(target, prefix=(kind == "dir"))
        finally:
            index.conn.close()
        _start_exclude_watcher()
        return {
            "ok": True,
            "removed": value,
            "pruned": pruned,
            "exclude_dirs": list(cfg.exclude.dirs),
            "exclude_files": list(cfg.exclude.files),
        }

    @app.post("/api/exclude/rebuild")
    def exclude_rebuild():
        if _EXCLUDE_STATE.get("running"):
            return {"started": False, "message": "重建正在进行中"}
        _EXCLUDE_STATE.update({"running": True, "report": "", "started_at": datetime.now().isoformat(timespec="seconds")})

        def work() -> None:
            _stop_exclude_watcher()   # 重建期间暂停监听，避免与全量扫描抢写
            try:
                index = ImageIndex(cfg.index_path)
                try:
                    scan = ExcludeIndex(index, cfg.exclude).scan(
                        [str(cfg.path(d) if not Path(d).is_absolute() else Path(d)) for d in cfg.exclude.dirs],
                        [str(cfg.path(f) if not Path(f).is_absolute() else Path(f)) for f in cfg.exclude.files],
                        rebuild=True,
                        skip=build_skip(cfg),
                    )
                finally:
                    index.conn.close()
                _EXCLUDE_STATE["report"] = scan.summary() + "".join(
                    "\n" + w for w in scan.warnings[:20]
                )
            except Exception as exc:
                _EXCLUDE_STATE["report"] = f"重建失败：{exc.__class__.__name__}: {exc}"
            finally:
                _EXCLUDE_STATE["running"] = False
                _start_exclude_watcher()   # 重建完成，恢复实时监听

        threading.Thread(target=work, daemon=True, name="facefind-exclude-rebuild").start()
        return {"started": True, "message": "已开始重建排除目录索引"}

    # ---------------- 账号 --------------
    def _login_log(cfg: Config, site: str, text: str, path: Path) -> None:
        """登录过程日志：只写状态与错误，不写 cookie 值。"""
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
        except OSError:
            pass

    def _start_login_log(cfg: Config, site: str) -> Path:
        cfg.log_path.mkdir(parents=True, exist_ok=True)
        path = cfg.log_path / f"login_{_safe_name(site)}_{datetime.now():%Y%m%d_%H%M%S}.log"
        return path

    def _auth_payload() -> dict:
        sites = {}
        for site in ("xiaohongshu", "weibo"):
            section = cfg.auth.get(site) or {}
            cached = browser.cached_status(site) or {}
            logged_in = bool(cached.get("browser_logged_in"))
            status = cached.get("status") or (
                "logged_in" if logged_in else ("logged_out" if cached else "unknown")
            )
            login = _LOGIN_STATE.get(site)
            sites[site] = {
                "enabled": bool(section.get("enabled")),
                "account": cached.get("account") or {"nickname": "", "uid": "", "source": ""},
                "cookie": secrets.status(site, cfg),
                "browser": cached or None,
                "browser_logged_in": logged_in,
                "confirmed": bool(cached.get("confirmed")),
                "status": status,
                "needs_enable": logged_in and not bool(section.get("enabled")),
                "login": login,
                "checked_at": cached.get("checked_at", ""),
                "last_check": cached.get("checked_at", ""),
            }
        return {"sites": sites, "browser": browser.info(cfg)}

    @app.get("/api/auth")
    def auth_state():
        return _auth_payload()

    @app.post("/api/auth/check")
    def auth_check(payload: dict = Body(default_factory=dict)):
        site = str(payload.get("site") or "all")
        targets = ["xiaohongshu", "weibo"] if site in ("all", "") else [site]
        results = []
        for s in targets:
            if s not in ("xiaohongshu", "weibo"):
                continue
            try:
                results.append(browser.check_login(s, cfg))
            except Exception as exc:
                results.append({"site": s, "browser_logged_in": False, "confirmed": False,
                                "status": "unknown",
                                "detail": f"检查失败：{exc.__class__.__name__}: {exc}",
                                "checked_at": datetime.now().isoformat(timespec="seconds")})
        return {"results": results, "state": _auth_payload()}

    @app.post("/api/auth/open-login")
    def auth_open_login(payload: dict = Body(...)):
        site = str(payload.get("site") or "")
        if site not in ("xiaohongshu", "weibo"):
            raise HTTPException(400, "site 只能是 xiaohongshu 或 weibo")
        thread = _LOGIN_THREADS.get(site)
        if thread and thread.is_alive():
            return {"started": False, "message": "登录窗口已打开，请完成登录", "state": _LOGIN_STATE.get(site)}
        if not browser.playwright_installed():
            raise HTTPException(400, "Playwright 未安装：请执行 python -m pip install playwright")

        log_path = _start_login_log(cfg, site)
        _login_log(cfg, site, f"open-login 请求：site={site}", log_path)
        _LOGIN_STATE[site] = {"state": "opening", "detail": "正在打开登录窗口…",
                              "status": "unknown", "log_path": str(log_path),
                              "started_at": datetime.now().isoformat(timespec="seconds")}

        def on_state(info: dict) -> None:
            detail = str(info.get("detail") or "")
            current = _LOGIN_STATE.get(site) or {}
            _LOGIN_STATE[site] = {
                "state": str(info.get("state") or "waiting"),
                "status": info.get("status") or "unknown",
                "detail": detail,
                "waited_seconds": info.get("waited_seconds", 0),
                "keep_open": info.get("keep_open", False),
                "removed_cookies": info.get("removed_cookies") or [],
                "log_path": str(log_path),
                "started_at": current.get("started_at", ""),
            }
            if current.get("detail") != detail:
                _login_log(cfg, site, f"等待中：{detail}", log_path)

        def work() -> None:
            try:
                result = browser.open_login_window(
                    site, cfg, on_state=on_state,
                    log=lambda m: _login_log(cfg, site, m, log_path),
                )
                _LOGIN_STATE[site] = {"state": "done", "log_path": str(log_path), **result}
                _login_log(cfg, site, (
                    f"完成：logged_in={result.get('logged_in')} status={result.get('status')} "
                    f"nickname={result.get('account', {}).get('nickname') or '-'} "
                    f"uid={result.get('account', {}).get('uid') or '-'} "
                    f"waited={result.get('waited_seconds')}s "
                    f"keep_open={result.get('keep_open')} auto_closed={result.get('auto_closed')} "
                    f"detail={result.get('detail')}"
                ), log_path)
            except Exception as exc:
                _LOGIN_STATE[site] = {"state": "error",
                                      "detail": f"{exc.__class__.__name__}: {exc}",
                                      "status": "unknown", "log_path": str(log_path),
                                      "checked_at": datetime.now().isoformat(timespec="seconds")}
                _login_log(cfg, site, f"失败：{exc.__class__.__name__}: {exc}", log_path)
                _login_log(cfg, site, traceback.format_exc(), log_path)

        thread = threading.Thread(target=work, daemon=True, name=f"facefind-login-{site}")
        _LOGIN_THREADS[site] = thread
        thread.start()
        return {
            "started": True,
            "log_path": str(log_path),
            "message": (
                "已在浏览器打开登录页，请完成登录；"
                + ("登录成功后窗口会保持，由你手动关闭。"
                   if site in (cfg.browser.login_keep_open or [])
                   else "仅在接口确认登录成功后窗口才会自动关闭。")
            ),
        }

    @app.post("/api/auth/toggle")
    def auth_toggle(payload: dict = Body(...)):
        site = str(payload.get("site") or "")
        if site not in ("xiaohongshu", "weibo"):
            raise HTTPException(400, "site 只能是 xiaohongshu 或 weibo")
        if payload.get("enabled") is None:
            raise HTTPException(400, "缺少 enabled")
        enabled = bool(payload.get("enabled"))
        data = store.load()
        data.setdefault("auth", {}).setdefault(site, {})["enabled"] = enabled
        store.save(data)
        store.apply(cfg)
        return {
            "site": site, "enabled": enabled,
            "message": "已启用该站点" if enabled else "已停用该站点，后续任务不会再访问它",
            "state": _auth_payload(),
        }

    @app.post("/api/auth/logout")
    def auth_logout(payload: dict = Body(...)):
        site = str(payload.get("site") or "")
        if site not in ("xiaohongshu", "weibo"):
            raise HTTPException(400, "site 只能是 xiaohongshu 或 weibo")
        try:
            result = browser.logout(site, cfg)
        except Exception as exc:
            raise HTTPException(500, f"退出失败：{exc.__class__.__name__}: {exc}") from exc
        cookie_status = secrets.status(site, cfg)
        if cookie_status.get("exists"):
            result["warning"] = (
                f"检测到 {cookie_status.get('file')} 仍存在：下次启动会自动注入该 Cookie。"
                "如需彻底切换账号，请自行移走该只读 cookie 文件（本工具不会修改或删除它）。"
            )
        return {**result, "state": _auth_payload()}

    @app.get("/api/auth/isolation")
    def auth_isolation(verify: bool = Query(False)):
        return browser.isolation_evidence(cfg, run_session=verify)

    # ---------------- 本地文件 --------------
    thumb_dir = cfg.root / "data" / "thumbs"

    @app.get("/api/thumb")
    def serve_thumb(path: str = Query(...), w: int = Query(640), v: str = Query("")):
        """按需生成并缓存缩略图（首次访问生成，之后直接读缓存）。"""
        p = Path(path)
        if not p.exists() or not p.is_file():
            raise HTTPException(404, "文件不存在")
        if not (_within(p, cfg.root) or _within(p, cfg.faces_path) or _within(p, cfg.output_path)):
            raise HTTPException(403, "只允许访问项目目录内的文件")
        width = max(64, min(1024, int(w)))
        st = p.stat()
        key = hashlib.sha1(
            f"{p.resolve()}|{st.st_mtime_ns}|{st.st_size}|{width}".encode("utf-8")
        ).hexdigest()
        cache = thumb_dir / f"{key}.jpg"
        if not cache.exists():
            with _THUMB_LOCK:
                if not cache.exists():
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        with Image.open(p) as im:
                            im = ImageOps.exif_transpose(im)
                            im.thumbnail((width, width), Image.LANCZOS)
                            if im.mode in ("RGBA", "LA", "P"):
                                rgba = im.convert("RGBA")
                                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                                bg.paste(rgba, mask=rgba.split()[-1])
                                im = bg
                            else:
                                im = im.convert("RGB")
                            tmp = cache.with_suffix(f".{threading.get_ident()}.tmp")
                            # 目标单张 <60KB：q80 起步，必要时逐级降质
                            for quality in (80, 72, 64, 56):
                                im.save(tmp, "JPEG", quality=quality, optimize=True, progressive=True)
                                if tmp.stat().st_size <= 60 * 1024:
                                    break
                            tmp.replace(cache)
                    except Exception as exc:
                        raise HTTPException(415, f"无法生成缩略图：{exc}") from exc
        # 带 v（文件版本）的 URL 可长期缓存；无 v 的旧 URL 不缓存，避免替换后仍显示旧图
        cache_ctl = "public, max-age=604800, immutable" if v else "no-cache"
        return FileResponse(cache, media_type="image/jpeg",
                            headers={"Cache-Control": cache_ctl})

    @app.get("/api/file")
    def serve_file(path: str = Query(...), v: str = Query("")):
        p = Path(path)
        if not p.exists() or not p.is_file():
            raise HTTPException(404, "文件不存在")
        if not (_within(p, cfg.root) or _within(p, cfg.faces_path) or _within(p, cfg.output_path)):
            raise HTTPException(403, "只允许访问项目目录、faces 与下载目录内的文件")
        cache_ctl = "public, max-age=604800, immutable" if v else "no-cache"
        return FileResponse(p, headers={"Cache-Control": cache_ctl})

    @app.get("/api/fs")
    def browse_fs(path: str = ""):
        if not path:
            if os.name == "nt":
                drives = [f"{chr(c)}:\\" for c in range(65, 91) if Path(f"{chr(c)}:\\").exists()]
            else:
                drives = ["/"]
            return {"path": "", "parent": "", "dirs": drives, "roots": drives}
        p = Path(path)
        if not p.exists() or not p.is_dir():
            raise HTTPException(400, "目录不存在")
        dirs = sorted(str(d) for d in p.iterdir() if d.is_dir() and not d.name.startswith("."))
        parent = str(p.parent) if p.parent != p else ""
        return {"path": str(p), "parent": parent, "dirs": dirs}

    @app.get("/api/health")
    def health():
        return {"ok": True, "time": datetime.now().isoformat(timespec="seconds")}

    ui_dir = cfg.root / "ui"
    if ui_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(ui_dir), html=True), name="ui")
    else:
        @app.get("/", response_class=HTMLResponse)
        def missing_ui():
            return "<h1>facefind</h1><p>未找到 ui/index.html</p>"

    return app
