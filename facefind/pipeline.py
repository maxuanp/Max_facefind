from __future__ import annotations

import csv
import json
import logging
import shutil
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from .config import Config, TaskConfig
from .blocklist import BlockList
from .dedupe import dhash, phash, sha256_bytes
from .exclude import ExcludeHit, ExcludeIndex, ScanReport, build_skip, decide_scan, record_scan_decision
from .face import FaceEngine, PersonReferences, build_references, score_candidate
from .index import ImageIndex
from .models import FaceMatch, ImageMetrics, SearchItem
from .net import HttpClient
from .people import discover_references
from .quality import clarity, decode, measure, sniff_ext
from .framing import classify as classify_framing
from .search import make_source
from .search.base import AuthRequired, BaseSource, SourceError
from .sites import SiteAggregator
from .urlkey import canonical_url, note_key as url_note_key

logger = logging.getLogger(__name__)


# ---------------- 结果数据结构 ----------------
@dataclass
class PersonResult:
    person: str
    target: int
    saved_now: int = 0
    saved_added: int = 0
    review_added: int = 0
    duplicates: int = 0
    replaced: int = 0
    excluded: int = 0
    rejected: int = 0
    failed: int = 0
    blocked: int = 0
    candidates: int = 0
    pre_blocked: int = 0          # 下载前拦截（已入库 URL 变体 / 黑名单），未取字节
    post_duplicates: int = 0      # 下载后判重（字节 / pHash）
    elapsed: float = 0.0
    warnings: list[str] = field(default_factory=list)
    saved_files: list[str] = field(default_factory=list)
    sites: dict[str, dict] = field(default_factory=dict)


@dataclass
class RunReport:
    people: list[PersonResult] = field(default_factory=list)
    elapsed: float = 0.0
    scan: ScanReport | None = None
    device: str = "disabled"
    stopped: bool = False

    @property
    def totals(self) -> dict[str, int]:
        keys = ("saved_added", "review_added", "duplicates", "replaced", "excluded", "rejected", "failed",
                "blocked", "pre_blocked", "post_duplicates")
        return {k: sum(getattr(p, k) for p in self.people) for k in keys}


@dataclass
class RunOptions:
    names: list[str] | None = None
    count: int | None = None
    accept_threshold: float | None = None
    review_threshold: float | None = None
    mode: str | None = None
    sources: list[str] | None = None
    queries: list[str] | None = None
    faces_dir: str | None = None
    explicit_faces: dict[str, list[str]] | None = None
    out_dir: str | None = None
    use_face: bool | None = None
    max_pages: int | None = None
    min_width: int | None = None
    min_height: int | None = None
    proxy: str | None = None
    exclude_dirs: list[str] = field(default_factory=list)
    exclude_files: list[str] = field(default_factory=list)
    exclude_phash_threshold: int | None = None
    exclude_rebuild: bool = False
    review_enabled: bool | None = None
    control: Any | None = None          # JobControl：提供 wait() / stopped 检测
    run_id: str = ""                    # 运行标识，写入事件与摘要，便于按次回溯
    on_event_file: Callable[[str], None] | None = None  # 事件日志路径（供实时聚合）
    on_log: Callable[[str], None] | None = None
    on_progress: Callable[[dict], None] | None = None


def _log(cb: Callable[[str], None] | None, msg: str) -> None:
    logger.info(msg)
    if cb:
        cb(msg)


def _tick(opt: RunOptions) -> bool:
    """暂停时阻塞，返回 False 表示已请求停止。"""
    ctl = opt.control
    if ctl is None:
        return True
    ctl.wait()
    return not ctl.stopped


def _progress(opt: RunOptions, **payload: Any) -> None:
    if opt.on_progress is None:
        return
    try:
        opt.on_progress(payload)
    except Exception:  # 进度回调不应影响主流程
        logger.debug("progress callback failed", exc_info=True)


# ---------------- 主入口 ----------------
def run(cfg: Config, options: RunOptions | None = None) -> RunReport:
    opt = options or RunOptions()
    t_start = time.time()
    if opt.proxy is not None:
        cfg.network.proxy = opt.proxy
    if opt.out_dir:
        cfg.output_dir = opt.out_dir
    if opt.exclude_phash_threshold is not None:
        cfg.exclude.phash_threshold = max(0, min(16, opt.exclude_phash_threshold))

    refs_map = discover_references(cfg, opt.names, opt.explicit_faces, opt.faces_dir)
    if not refs_map:
        raise ValueError(
            "未发现任何人名/参考照。请把参考照放到 faces/<人名>/ 下，"
            "或使用 --face 姓名=图片或目录 指定。"
        )
    if opt.min_width:
        cfg.search.min_width = opt.min_width
    if opt.min_height:
        cfg.search.min_height = opt.min_height
    if opt.max_pages:
        cfg.search.max_pages = opt.max_pages

    index = ImageIndex(cfg.index_path)
    use_face = cfg.face.enabled if opt.use_face is None else opt.use_face
    engine: FaceEngine | None = None
    if use_face:
        root = cfg.face.root
        if root and not Path(root).is_absolute():
            root = str(cfg.path(root))
        engine = FaceEngine(
            model=cfg.face.model,
            device=cfg.face.device,
            det_size=cfg.face.det_size,
            det_thresh=cfg.face.det_thresh,
            min_face_px=cfg.face.min_face_px,
            allow_multiple_faces=cfg.face.allow_multiple_faces,
            root=root,
        ).load()
        _log(opt.on_log, f"人脸模型就绪：profile={cfg.profile} model={engine.model} device={engine.device} "
                         f"det={cfg.face.det_size} providers={engine.providers}")
        if engine.fallback_used:
            _log(opt.on_log, f"警告：{cfg.face.model} 加载失败，已回退 buffalo_l")
        _log(opt.on_log, f"最小人脸像素：{cfg.face.min_face_px}px（低于该值不参与比对）")
        _log(opt.on_log, f"构图模型：{cfg.framing.model} input={cfg.framing.input_size}")

    exclude_index = ExcludeIndex(index, cfg.exclude)
    exclude_dirs = list(dict.fromkeys(cfg.exclude.dirs + opt.exclude_dirs))
    exclude_files = list(dict.fromkeys(cfg.exclude.files + opt.exclude_files))
    abs_dirs = [str(cfg.root / d) if not Path(d).is_absolute() else d for d in exclude_dirs]
    abs_files = [str(cfg.root / f) if not Path(f).is_absolute() else f for f in exclude_files]
    rebuild = opt.exclude_rebuild or cfg.exclude.rebuild
    need_scan, reason = decide_scan(cfg, abs_dirs, abs_files, rebuild=rebuild)
    t_scan = time.perf_counter()
    if need_scan:
        scan = exclude_index.scan(abs_dirs, abs_files, rebuild=rebuild, skip=build_skip(cfg))
        elapsed = time.perf_counter() - t_scan
        _log(opt.on_log, f"[排除库] {reason}（耗时 {elapsed:.2f}s）")
        if exclude_dirs or exclude_files:
            _log(opt.on_log, scan.summary())
            for w in scan.warnings:
                _log(opt.on_log, w)
        record_scan_decision(False, reason, cfg.exclude.scan_on_run, elapsed)
    else:
        scan = None
        exclude_index.reload()      # 跳过扫描也要让本次运行的内存数组与库一致
        elapsed = time.perf_counter() - t_scan
        _log(opt.on_log, f"[排除库] {reason}（耗时 {elapsed * 1000:.0f}ms）")
        record_scan_decision(True, reason, cfg.exclude.scan_on_run, elapsed)

    report = RunReport(scan=scan, device=engine.device if engine else "disabled")
    _progress(opt, phase="start", people=list(refs_map), target=0, saved=0)
    for name in refs_map:
        if not _tick(opt):
            report.stopped = True
            break
        task = cfg.build_task(
            name,
            count=opt.count,
            threshold=opt.accept_threshold,
            review_threshold=opt.review_threshold,
            mode=opt.mode,
            sources=opt.sources,
            queries=opt.queries,
        )
        result = _run_person(cfg, task, refs_map.get(name, []), engine, index, exclude_index, opt)
        report.people.append(result)
        _progress(
            opt, phase="person_done", person=name, saved=result.saved_now, target=result.target,
            saved_added=result.saved_added, review_added=result.review_added, duplicates=result.duplicates,
            excluded=result.excluded, rejected=result.rejected, failed=result.failed,
        )
        if result.warnings:
            for w in result.warnings:
                _log(opt.on_log, w)

    report.elapsed = round(time.time() - t_start, 2)
    return report


# ---------------- 单人任务 ----------------
def _run_person(cfg: Config, task: TaskConfig, ref_paths: list[str], engine: FaceEngine | None,
                index: ImageIndex, exclude_index: ExcludeIndex, opt: RunOptions) -> PersonResult:
    t0 = time.time()
    res = PersonResult(person=task.name, target=task.count)
    block_list = BlockList(cfg.path("data/blocklist.json"))
    if block_list.is_person_blocked(task.name):
        res.warnings.append(f"[{task.name}] 该人员已被屏蔽，本次跳过采集（可在「排除库 → 屏蔽管理」解除）")
        _log(opt.on_log, f"[{task.name}] 已被屏蔽，跳过采集")
        res.elapsed = round(time.time() - t0, 2)
        return res
    out_root = cfg.output_path
    if out_root.name == _safe_name(task.name):
        out_root = out_root.parent
    out_dir = out_root / task.name
    review_dir = out_root / "review" / task.name
    out_dir.mkdir(parents=True, exist_ok=True)
    review_dir.mkdir(parents=True, exist_ok=True)
    log_dir = cfg.log_path
    log_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_name(task.name)
    event_path = log_dir / f"run_{datetime.now():%Y%m%d_%H%M%S}_{safe}.jsonl"

    # 跨运行：DB 为空但目录里已有 metadata.jsonl 时，先恢复索引
    if not index.rows(task.name):
        restored = index.import_metadata(task.name, out_dir / "metadata.jsonl")
        if restored:
            res.warnings.append(f"[{task.name}] 从 metadata.jsonl 恢复了 {restored} 条索引记录")

    refs: PersonReferences | None = None
    if engine is not None:
        refs, warns, _ = build_references(engine, task.name, ref_paths, task.similarity_mode, max_refs=20)
        res.warnings.extend(warns)
        if refs is None:
            res.warnings.append(
                f"[{task.name}] 没有可用参考照，已跳过该人（请检查参考照质量，或用 --no-face 关闭人脸过滤）"
            )
            res.elapsed = round(time.time() - t0, 2)
            return res
        _log(opt.on_log, f"[{task.name}] 参考照 {len(refs.refs)} 张，比对模式={task.similarity_mode}，"
                         f"阈值 accept={task.accept_threshold} review={task.review_threshold}")

    queries = list(dict.fromkeys([*(task.queries or [task.name]), *(opt.queries or [])]))
    review_on = True if opt.review_enabled is None else opt.review_enabled
    _log(opt.on_log, f"\n=== {task.name} === 目标 {task.count} 张，关键词：{queries}，站点：{task.sources}"
                     f"，review={'开' if review_on else '关'}")

    if opt.on_event_file is not None:
        try:
            opt.on_event_file(str(event_path))
        except Exception:  # 回调失败不影响任务
            logger.debug("on_event_file callback failed", exc_info=True)
    agg = SiteAggregator()

    stopped = False
    with event_path.open("w", encoding="utf-8") as events:
        def emit_raw(rec: dict) -> None:
            rec.setdefault("time", datetime.now().isoformat(timespec="milliseconds"))
            rec.setdefault("person", task.name)
            if opt.run_id:
                rec.setdefault("run_id", opt.run_id)
            for k, v in list(rec.items()):
                if isinstance(v, np.generic):
                    rec[k] = v.item()
            events.write(json.dumps(rec, ensure_ascii=False) + "\n")
            events.flush()  # 立即落盘，供运行页实时聚合
            agg.feed(rec)

        def emit(item: SearchItem | None = None, *, status: str, reason: str = "", **extra: Any) -> None:
            rec = _event(events, item=item, person=task.name, status=status, reason=reason,
                         run_id=opt.run_id, **extra)
            events.flush()
            agg.feed(rec)

        def emit_site(source: str, state: str, reason: str = "", **extra: Any) -> None:
            emit_raw({"type": "site", "source": source, "status": "site",
                      "state": state, "reason": reason, **extra})

        def emit_search(source: str, query: str, page: int, count: int, max_pages: int,
                        unit: str = "page") -> None:
            if count <= 0:
                return
            emit_raw({"type": "search", "source": source, "status": "search", "query": query,
                      "page": page, "count": count, "max_pages": max_pages, "unit": unit})

        def skip_rest(reason: str, start: int) -> None:
            for rest in task.sources[start:]:
                emit_site(rest, "skipped", reason)

        # ---- 下载前拦截所需的归一化集合（每人一次）----
        # 黑名单 URL 的归一化 key（同一张图的变体也应被拦）
        blocked_keys = {
            canonical_url(row[0]) for row in index.conn.execute(
                "select url from blocked where url is not null and url != ''"
            )
        }

        def pre_block(it: SearchItem, stage: str, reason: str) -> None:
            res.pre_blocked += 1
            res.candidates += 1
            emit(item=it, status="duplicate", reason=reason, dedupe_stage=stage,
                 dedupe_phase="pre_download")
            emit_raw({"type": "pre_download_block", "source": source_name,
                      "status": "pre_download_block", "reason": stage, "url": it.url,
                      "page_url": it.page_url, "person": task.name})

        for si, source_name in enumerate(task.sources):
            if not _tick(opt):
                stopped = True
                skip_rest("用户停止任务，未执行", si)
                break
            if _saved_now(index, task.name) >= task.count:
                skip_rest(f"已达到目标 {task.count} 张，未访问该站点", si)
                break
            try:
                source = make_source(cfg, source_name)
            except SourceError as exc:
                res.warnings.append(f"[{task.name}] {source_name}: {exc}")
                emit_site(source_name, "error", str(exc))
                continue

            client = HttpClient(cfg.network, source_name)
            default_referer = _default_referer(source_name)
            rate = cfg.network.site(source_name)
            stop_source = False
            emit_site(source_name, "running", f"开始搜索（{len(queries)} 个关键词）")
            for query in queries:
                if not _tick(opt):
                    stopped = True
                    break
                if stop_source or _saved_now(index, task.name) >= task.count:
                    break

                def note_page(it: SearchItem) -> None:
                    page = int(getattr(it, "page", 0) or 1)
                    emit_search(source_name, query, page, 1,
                                int(source.page_cap(cfg.search.max_pages)),
                                getattr(source, "page_unit", "page"))

                try:
                    items = source.search(
                        client, query, page_size=cfg.search.page_size, max_pages=cfg.search.max_pages
                    )
                    page_done = int(getattr(source, "last_page_done", 0) or 0)
                    if page_done:
                        # 如实回报本次搜索实际完成的页数/滚动轮次与本次上限（小红书是轮次，百度/微博是页码）
                        payload = {"type": "search_page", "source": source_name, "status": "search_page",
                                   "query": query, "page": page_done,
                                   "max_pages": int(source.page_cap(cfg.search.max_pages)),
                                   "unit": getattr(source, "page_unit", "page")}
                        stats = dict(getattr(source, "search_stats", {}) or {})
                        for key in ("notes", "stop"):
                            if stats.get(key):
                                payload[key] = stats[key]
                        emit_raw(payload)
                    expander = getattr(source, "expand_notes", None)
                    if callable(expander):
                        # 详情页只做节流：按 count 缺口限制本轮打开的笔记数，并用
                        # search.detail_budget 封顶（默认 12，控制站点风控额度）。
                        # 整篇笔记的图都已入库时精确跳过，把预算让给更靠后的新笔记。
                        gap = max(0, task.count - _saved_now(index, task.name))
                        budget = max(1, min(int(cfg.search.detail_budget), max(3, gap * 3)))
                        picked, skipped_notes = _pick_notes(index, task.name, items, budget)
                        candidates = len(items)
                        expanded, dstats = expander(picked, budget)
                        items = expanded
                        media_written = _record_note_media(index, task.name, source)
                        dstats["notes"] = len(picked)
                        dstats["skipped"] = len(skipped_notes)
                        dstats["media_written"] = media_written
                        emit_raw({"type": "detail", "source": source_name, "status": "detail",
                                  "query": query, **dstats})
                        for s in skipped_notes:
                            emit_raw({"type": "note_skip", "source": source_name,
                                      "status": "note_skip", "reason": "all_images_in_index",
                                      "note": s["note"], "page_url": s["page_url"],
                                      "total": s["total"], "downloaded": s["downloaded"]})
                        _log(opt.on_log,
                             f"[{task.name}] 小红书详情页：候选笔记 {candidates}, "
                             f"可选笔记 {dstats.get('notes')}（整篇已入库跳过 {dstats.get('skipped')} 篇），"
                             f"实际打开 {dstats.get('detail_pages')} 页，取图 {dstats.get('images')}"
                             f"（imageList 声明 {dstats.get('image_list')}），"
                             f"回退封面 {dstats.get('fallback_cover')}")
                    with ThreadPoolExecutor(max_workers=max(1, rate.concurrency)) as pool:
                        for batch in _batches(items, max(1, rate.concurrency)):
                            if not _tick(opt):
                                stopped = True
                                break
                            if _saved_now(index, task.name) >= task.count:
                                break
                            pending: list[SearchItem] = []
                            for it in batch:
                                note_page(it)
                                key = canonical_url(it.url)
                                # 下载前拦截：全部在取字节之前，节省带宽与站点风控额度
                                # 1) 黑名单 URL（原始 URL 或归一化后同图变体）
                                if (index.blocked_url(it.url) is not None or block_list.match_url(it.url)
                                        or (key and key in blocked_keys)):
                                    res.blocked += 1
                                    res.pre_blocked += 1
                                    res.candidates += 1
                                    emit(item=it, status="blocked", reason="blocked",
                                         dedupe_stage="blocked_url", dedupe_phase="pre_download")
                                    emit_raw({"type": "pre_download_block", "source": source_name,
                                              "status": "pre_download_block", "reason": "blocked_url",
                                              "url": it.url, "page_url": it.page_url,
                                              "person": task.name})
                                    continue
                                # 2) 历史 URL：精确命中 / 归一化后命中（尺寸与追踪参数变体）
                                seen_exact = index.has_url(task.name, it.url)
                                seen_variant = bool(key) and not seen_exact and \
                                    index.has_url_key(task.name, key) is not None
                                if seen_exact or seen_variant:
                                    stage = "url_seen" if seen_exact else "url_variant"
                                    res.duplicates += 1
                                    pre_block(it, stage, "downloaded_before")
                                    continue
                                pending.append(it)
                            if not pending:
                                continue
                            if not _tick(opt):
                                stopped = True
                                break
                            fetched = list(pool.map(lambda it: _fetch(client, it, default_referer), pending))
                            for item, data, err in fetched:
                                if not _tick(opt):
                                    stopped = True
                                    break
                                if _saved_now(index, task.name) >= task.count:
                                    break
                                res.candidates += 1
                                if data is None:
                                    res.failed += 1
                                    emit(item=item, status="failed", reason=err or "download_failed")
                                    _progress(opt, phase="candidate", person=task.name,
                                              saved=_saved_now(index, task.name), target=task.count,
                                              candidates=res.candidates, duplicate=res.duplicates,
                                              pre_blocked=res.pre_blocked,
                                              post_duplicates=res.post_duplicates,
                                              excluded=res.excluded, rejected=res.rejected, failed=res.failed)
                                    continue
                                status, info = _process_candidate(
                                    cfg, task, refs, engine, index, exclude_index, item, data, out_dir, review_dir,
                                    review_enabled=review_on,
                                )
                                emit(item=item, status=status, **info)
                                if status == "duplicate":
                                    res.post_duplicates += 1
                                    emit_raw({"type": "post_download_duplicate", "source": source_name,
                                              "status": "post_download_duplicate",
                                              "reason": info.get("dedupe_stage") or "duplicate",
                                              "url": item.url, "person": task.name})
                                _count(res, status, info)
                                _progress(opt, phase="candidate", person=task.name,
                                          saved=_saved_now(index, task.name), target=task.count,
                                          candidates=res.candidates, duplicate=res.duplicates,
                                          pre_blocked=res.pre_blocked,
                                          post_duplicates=res.post_duplicates,
                                          excluded=res.excluded, rejected=res.rejected, failed=res.failed)
                                if info.get("framing"):
                                    vis = info.get("framing_visible") or {}
                                    _log(opt.on_log,
                                         f"  [构图] {info.get('framing_cn')} "
                                         f"person_ratio={info.get('person_ratio')} "
                                         f"肩{vis.get('shoulders', 0)}/髋{vis.get('hips', 0)}/"
                                         f"膝{vis.get('knees', 0)}/踝{vis.get('ankles', 0)} "
                                         f"{'丢弃' if status == 'rejected' else '保留'}"
                                         f"{('：' + info.get('framing_reason', '')) if info.get('framing_reason') else ''}")
                                if status in ("saved", "review"):
                                    _log(opt.on_log, f"  [{status}] {Path(info.get('file_path', '')).name} "
                                                     f"sim={info.get('similarity')} clarity={info.get('clarity')}")
                except AuthRequired as exc:
                    res.warnings.append(f"[{task.name}] {source.display_name}: {exc}")
                    emit_site(source_name, "auth", str(exc))
                    stop_source = True
                except SourceError as exc:
                    msg = str(exc)
                    state = "risk" if any(t in msg for t in ("风控", "验证码", "滑块", "RiskControl", "安全验证")) else "error"
                    res.warnings.append(f"[{task.name}] {source.display_name} 搜索失败：{msg}")
                    emit_site(source_name, state, msg)
                    stop_source = True
                except Exception as exc:  # 单站点异常不影响其它站点
                    msg = f"{exc.__class__.__name__}: {exc}"
                    res.warnings.append(f"[{task.name}] {source.display_name} 异常：{msg}")
                    emit_site(source_name, "error", msg)
                    stop_source = True

            if stopped:
                emit_site(source_name, "stopped", "用户停止任务，已中断该站点")
                skip_rest("用户停止任务，未执行", si + 1)
                break
            if not stop_source:
                if _saved_now(index, task.name) >= task.count:
                    emit_site(source_name, "done", f"已达到目标 {task.count} 张")
                else:
                    emit_site(source_name, "done", "该站点搜索完成")

        person_sites = agg.snapshot().get(task.name, {})
        res.sites = person_sites
        emit_raw({"type": "site_summary", "status": "site_summary", "sites": person_sites,
                  "sources": list(task.sources), "elapsed": round(time.time() - t0, 2)})

    if stopped:
        _log(opt.on_log, f"[{task.name}] 已被用户停止，保留已完成的结果")
    _write_person_metadata(task.name, out_dir, index)
    res.saved_now = _saved_now(index, task.name)
    res.saved_files = [
        str(r["file_path"]) for r in index.rows(task.name, ("saved",)) if r["file_path"]
    ]
    res.elapsed = round(time.time() - t0, 2)
    _log(opt.on_log, f"[{task.name}] 完成：本次新增 saved={res.saved_added} review={res.review_added} "
                     f"替换={res.replaced} 重复={res.duplicates} 排除={res.excluded} 丢弃={res.rejected} "
                     f"屏蔽={res.blocked} 失败={res.failed}，当前累计 {res.saved_now}/{task.count}，"
                     f"耗时 {res.elapsed}s")
    return res


# ---------------- 单张候选图处理 ----------------
def _process_candidate(cfg: Config, task: TaskConfig, refs: PersonReferences | None,
                       engine: FaceEngine | None, index: ImageIndex, exclude_index: ExcludeIndex,
                       item: SearchItem, data: bytes, out_dir: Path, review_dir: Path,
                       review_enabled: bool = True) -> tuple[str, dict]:
    sha = sha256_bytes(data)
    img = decode(data)
    base = {
        "url": item.url,
        "source": item.source,
        "query": item.query,
        "page_url": item.page_url,
        "title": item.title,
        "sha256": sha,
    }
    if img is None:
        index.upsert(_record(task, item, sha, status="rejected", dedupe_stage="undecodable"))
        return "rejected", {**base, "reason": "undecodable"}

    # 全图 pHash / dHash 只算这一次：屏蔽名单与排除库共用，排除库的变体 pHash 惰性复用 ph
    ph = phash(img)
    dh = dhash(img)

    # 阶段 0a-1：独立屏蔽名单（图片级）内容匹配，命中直接跳过
    block_list = BlockList(cfg.path("data/blocklist.json"))
    block_hit = block_list.match_content(ph, dh, sha, cfg.dedupe.phash_threshold,
                                         cfg.exclude.dhash_threshold)
    if block_hit is not None:
        return "blocked", {
            **base, "reason": "blocked", "dedupe_stage": "blocklist_content",
            "blocked_url": block_hit.get("url", ""), "blocked_person": block_hit.get("person", ""),
        }

    # 阶段 0a：内容级黑名单（pHash / dHash），命中直接跳过并记日志 blocked
    blocked_hit = index.blocked_match(
        ph, dh, cfg.dedupe.phash_threshold, cfg.exclude.dhash_threshold
    )
    if blocked_hit is not None:
        brow, method, dist = blocked_hit
        return "blocked", {
            **base, "reason": "blocked", "dedupe_stage": f"blocked_{method}",
            "block_method": method, "block_distance": dist,
            "blocked_url": brow["url"], "blocked_person": brow["person"] or "",
        }

    # 阶段 0：排除目录（字节哈希 / pHash / 多尺度裁剪容忍），下载后、落盘前判定
    hit: ExcludeHit | None = exclude_index.lookup(sha, img, ph=ph, dh=dh)
    if hit is not None:
        index.upsert(
            _record(
                task, item, sha, status="excluded", dedupe_stage="excluded_dir",
                exclude_path=hit.path, exclude_distance=hit.distance, exclude_method=hit.method,
            )
        )
        return "excluded", {
            **base, "reason": "excluded_dir", "dedupe_stage": "excluded_dir",
            "exclude_path": hit.path, "exclude_distance": hit.distance, "exclude_method": hit.method,
        }

    # 阶段 0b：与历史下载内容完全相同（不同 URL 的情况）
    prev = index.by_sha(task.name, sha)
    if prev is not None:
        index.upsert(_record(task, item, sha, status="duplicate", dedupe_stage="downloaded_before"))
        return "duplicate", {
            **base, "reason": "downloaded_before", "dedupe_stage": "downloaded_before",
            "matched_file": prev["file_path"] or prev["raw_path"],
        }

    metrics = measure(img, data)
    if metrics.width < task.min_width or metrics.height < task.min_height:
        index.upsert(
            _record(task, item, sha, status="rejected", dedupe_stage="too_small", metrics=metrics)
        )
        return "rejected", {**base, "reason": "too_small", "width": metrics.width, "height": metrics.height}

    raw_path = _save_raw(cfg.raw_path, item.source, task.name, sha, data, metrics)
    base_clarity = clarity(metrics, None, cfg.quality.weights)
    final_clarity = base_clarity

    dup_rows: list[tuple[Any, str]] = []
    if cfg.dedupe.enabled:
        for row, dist in index.near_dupes(task.name, ph, cfg.dedupe.phash_threshold):
            dup_rows.append((row, f"phash({dist})"))

    # 阶段 1+2 已省 GPU；阶段 3 人脸 embedding
    sim: float | None = None
    match: FaceMatch | None = None
    emb: np.ndarray | None = None
    framing: dict | None = None
    if engine is not None and refs is not None:
        emb, match = score_candidate(engine, refs, img)
        if match.reason == "no_face":
            index.upsert(_record(task, item, sha, status="rejected", dedupe_stage="no_face",
                                 metrics=metrics, phash=ph, clarity=base_clarity, raw_path=raw_path))
            return "rejected", {**base, "reason": "no_face", "n_faces": 0, "raw_path": raw_path}
        if match.reason == "face_too_small":
            index.upsert(_record(task, item, sha, status="rejected", dedupe_stage="face_too_small",
                                 metrics=metrics, phash=ph, clarity=base_clarity, raw_path=raw_path,
                                 face_ratio=match.face_ratio, n_faces=match.n_faces))
            return "rejected", {**base, "reason": "face_too_small", "face_px": match.face_px,
                                "n_faces": match.n_faces, "raw_path": raw_path}
        sim = match.similarity
        final_clarity = clarity(metrics, match.face_ratio, cfg.quality.weights)
        if sim < task.review_threshold:
            index.upsert(_record(task, item, sha, status="rejected", dedupe_stage="low_similarity",
                                 metrics=metrics, phash=ph, clarity=final_clarity, raw_path=raw_path,
                                 match=match, embedding=emb))
            return "rejected", {**base, "reason": "low_similarity", "similarity": sim,
                                "matched_ref": match.matched_ref, "clarity": final_clarity,
                                "n_faces": match.n_faces, "raw_path": raw_path}
        if not review_enabled and sim < task.accept_threshold:
            index.upsert(_record(task, item, sha, status="rejected", dedupe_stage="below_accept",
                                 metrics=metrics, phash=ph, clarity=final_clarity, raw_path=raw_path,
                                 match=match, embedding=emb))
            return "rejected", {**base, "reason": "below_accept", "similarity": sim,
                                "matched_ref": match.matched_ref, "clarity": final_clarity,
                                "n_faces": match.n_faces, "raw_path": raw_path}
        # 构图过滤：全身 / 完整上半身；按主脸所在人物判定
        framing = None
        try:
            framing = classify_framing(img, match.bbox, cfg)
        except Exception as exc:
            logger.debug("构图检测失败，放行：%s", exc)
        if framing and not framing["keep"]:
            index.upsert(_record(task, item, sha, status="rejected",
                                 dedupe_stage=f"framing_{framing['label']}", metrics=metrics,
                                 phash=ph, clarity=final_clarity, raw_path=raw_path,
                                 match=match, embedding=emb, framing=framing))
            return "rejected", {
                **base, "reason": "framing", "similarity": sim, "clarity": final_clarity,
                "n_faces": match.n_faces, "raw_path": raw_path, "framing": framing["label"],
                "framing_cn": framing["label_cn"], "person_ratio": framing["person_ratio"],
                "framing_reason": framing["reason"], "framing_visible": framing["visible"],
                "framing_edge_cut": framing["edge_cut"],
            }
        if cfg.dedupe.enabled:
            for row, vec in index.saved_embeddings(task.name):
                score = float(np.dot(vec, emb))
                if score >= task.dedupe_threshold:
                    dup_rows.append((row, f"face({score:.3f})"))
        status = "saved" if sim >= task.accept_threshold else "review"
        reason = "" if status == "saved" else "review_similarity"
    else:
        status = "saved"
        reason = ""

    # 去重：同图多版本只保留清晰度最高者，落选版本不删除，记日志
    dedupe_stage = ""
    if dup_rows:
        winner = max(dup_rows, key=lambda x: float(x[0]["clarity"] or 0))
        row, stage_desc = winner
        if final_clarity > float(row["clarity"] or 0):
            _supersede(cfg, index, task.name, row)
            dedupe_stage = f"replaced_{stage_desc}"
            replaced = True
        else:
            index.upsert(_record(task, item, sha, status="duplicate", dedupe_stage=stage_desc,
                                 metrics=metrics, phash=ph, clarity=final_clarity, raw_path=raw_path,
                                 match=match, embedding=emb, similarity=sim))
            return "duplicate", {**base, "reason": "downloaded_before", "dedupe_stage": stage_desc,
                                 "similarity": sim, "clarity": final_clarity,
                                 "matched_file": row["file_path"] or row["raw_path"], "raw_path": raw_path}
    else:
        replaced = False

    file_path = ""
    if status in ("saved", "review"):
        dest_dir = out_dir if status == "saved" else review_dir
        fname = f"{_safe_name(task.name)}_{item.source}_{ph:016x}_{metrics.width}x{metrics.height}{sniff_ext(data)}"
        dest = _unique_path(dest_dir / fname)
        shutil.copyfile(raw_path, dest)
        file_path = str(dest)

    index.upsert(
        _record(task, item, sha, status=status, dedupe_stage=dedupe_stage, metrics=metrics, phash=ph,
                clarity=final_clarity, raw_path=raw_path, file_path=file_path, match=match,
                embedding=emb, similarity=sim, framing=framing)
    )
    return status, {
        **base, "reason": reason, "similarity": sim,
        "matched_ref": match.matched_ref if match else "",
        "clarity": final_clarity, "width": metrics.width, "height": metrics.height,
        "phash": f"{ph:016x}", "n_faces": match.n_faces if match else 0,
        "face_ratio": match.face_ratio if match else 0.0,
        "dedupe_stage": dedupe_stage, "file_path": file_path, "raw_path": raw_path,
        "replaced": replaced,
        "framing": framing["label"] if framing else "",
        "framing_cn": framing["label_cn"] if framing else "",
        "person_ratio": framing["person_ratio"] if framing else 0.0,
        "framing_reason": framing["reason"] if framing else "",
        "framing_visible": framing["visible"] if framing else {},
    }


# ---------------- 辅助 ----------------
def _record(task: TaskConfig, item: SearchItem, sha: str, *, status: str, dedupe_stage: str = "",
            metrics: ImageMetrics | None = None, phash: int | None = None, clarity: float = 0.0,
            raw_path: str = "", file_path: str = "", match: FaceMatch | None = None,
            embedding: np.ndarray | None = None, similarity: float | None = None,
            exclude_path: str = "", exclude_distance: int | None = None, exclude_method: str = "",
            face_ratio: float | None = None, n_faces: int | None = None,
            framing: dict | None = None) -> dict:
    return {
        "person": task.name,
        "source": item.source,
        "query": item.query,
        "page_url": item.page_url,
        "url": item.url,
        "sha256": sha,
        "phash": phash,
        "width": metrics.width if metrics else 0,
        "height": metrics.height if metrics else 0,
        "area": metrics.area if metrics else 0,
        "sharpness": metrics.sharpness if metrics else 0,
        "compression": metrics.compression if metrics else 0,
        "face_ratio": face_ratio if face_ratio is not None else (match.face_ratio if match else 0),
        "clarity": clarity,
        "similarity": similarity if similarity is not None else (match.similarity if match else None),
        "matched_ref": match.matched_ref if match else "",
        "n_faces": n_faces if n_faces is not None else (match.n_faces if match else 0),
        "framing": framing["label"] if framing else "",
        "person_ratio": framing["person_ratio"] if framing else 0.0,
        "framing_reason": framing["reason"] if framing else "",
        "status": status,
        "raw_path": raw_path,
        "file_path": file_path,
        "embedding": embedding.astype(np.float32).tobytes() if embedding is not None else None,
        "dedupe_stage": dedupe_stage,
        "exclude_path": exclude_path,
        "exclude_distance": exclude_distance,
        "exclude_method": exclude_method,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def _batches(it: Iterator[SearchItem], size: int) -> Iterator[list[SearchItem]]:
    batch: list[SearchItem] = []
    for x in it:
        batch.append(x)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _fetch(client: HttpClient, item: SearchItem, default_referer: str) -> tuple[SearchItem, bytes | None, str]:
    errors: list[str] = []
    for idx, url in enumerate(item.all_urls()):
        referer = item.page_url if idx == 0 and item.page_url else default_referer
        try:
            data = client.get_bytes(url, referer=referer)
            if data:
                return item, data, ""
        except Exception as exc:
            errors.append(str(exc))
    return item, None, errors[-1] if errors else "download_failed"


def _default_referer(source: str) -> str:
    return {
        "baidu": "https://image.baidu.com/",
        "weibo": "https://m.weibo.cn/",
        "xiaohongshu": "https://www.xiaohongshu.com/",
    }.get(source, "")


def _save_raw(raw_root: Path, source: str, person: str, sha: str, data: bytes,
              metrics: ImageMetrics) -> str:
    d = raw_root / source / _safe_name(person)
    d.mkdir(parents=True, exist_ok=True)
    path = _unique_path(d / f"{sha[:12]}_{metrics.width}x{metrics.height}{sniff_ext(data)}")
    path.write_bytes(data)
    return str(path)


def _safe_name(name: str) -> str:
    bad = '<>:"/\\|?*\n\r\t'
    return "".join("_" if c in bad else c for c in name).strip() or "unnamed"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 1000):
        cand = path.with_name(f"{stem}_{i}{suffix}")
        if not cand.exists():
            return cand
    return path.with_name(f"{stem}_{datetime.now():%H%M%S%f}{suffix}")


def _supersede(cfg: Config, index: ImageIndex, person: str, row: Any) -> None:
    """旧版本移入 _raw/superseded/，不删除文件，并更新索引状态。"""
    old = Path(row["file_path"]) if row["file_path"] else None
    dest_dir = cfg.raw_path / "superseded" / _safe_name(person)
    dest_dir.mkdir(parents=True, exist_ok=True)
    new_path = row["file_path"]
    if old and old.exists():
        dest = _unique_path(dest_dir / f"{old.stem}_r{row['id']}{old.suffix}")
        shutil.move(str(old), str(dest))
        new_path = str(dest)
    index.update(row["id"], status="superseded", file_path=new_path)


def _saved_now(index: ImageIndex, person: str) -> int:
    return int(index.stats_for(person)["saved"])


def _note_state(index: ImageIndex, person: str, page_url: str) -> tuple[str, int, int, str]:
    """笔记级去重判定：返回 (状态, 总图数, 已入库数, note_key)。

    只有「清单里记录的每张图都能按 canonical key 命中索引」且记录条数等于声明总数时
    才返回 done；信息不全（没记录过 / 张数少于声明 / 有失败或未下载的图）一律 unknown/partial，
    不做跳过，避免把没下载的图漏掉。
    """
    key = url_note_key(page_url)
    if not key:
        return "unknown", 0, 0, ""
    rec = index.note_media(person, key)
    total = int(rec.get("total") or 0)
    items = rec.get("items") or []
    if total <= 0:
        return "unknown", 0, 0, key
    hit = 0
    for r in items:
        if index.has_url_key(person, r["image_key"]) is not None:
            hit += 1
    if len(items) >= total and hit >= total:
        return "done", total, hit, key
    return "partial", total, hit, key


def _pick_notes(index: ImageIndex, person: str, items: list[SearchItem],
                budget: int) -> tuple[list[SearchItem], list[dict]]:
    """从候选池里挑出本轮要开详情页的笔记：整篇图都已入库的跳过，用后面的笔记补位。"""
    picked: list[SearchItem] = []
    skipped: list[dict] = []
    for it in items:
        if not it.page_url:
            continue
        state, total, hit, key = _note_state(index, person, it.page_url)
        if state == "done" and key:
            skipped.append({"note": key, "total": total, "downloaded": hit,
                            "page_url": it.page_url})
            continue
        if len(picked) >= budget:
            break
        picked.append(it)
    return picked, skipped


def _record_note_media(index: ImageIndex, person: str, source: BaseSource) -> int:
    """把本次详情页展开得到的「笔记 → 全部图片清单」写入索引，供下一轮精确跳过。"""
    media_map = dict(getattr(source, "last_note_media", {}) or {})
    written = 0
    for key, info in media_map.items():
        images = [{"url": m.get("url") or "", "key": m.get("key") or ""}
                  for m in (info.get("images") or [])]
        written += index.record_note_media(
            person, key, int(info.get("total") or len(images)), images
        )
    return written


def _count(res: PersonResult, status: str, info: dict) -> None:
    if status == "saved":
        if info.get("replaced"):
            res.replaced += 1
        else:
            res.saved_added += 1
    elif status == "review":
        if info.get("replaced"):
            res.replaced += 1
        else:
            res.review_added += 1
    elif status == "duplicate":
        res.duplicates += 1
    elif status == "blocked":
        res.blocked += 1
    elif status == "excluded":
        res.excluded += 1
    else:
        res.rejected += 1


def _event(fh, *, item: SearchItem | None, person: str, status: str, reason: str = "",
           run_id: str = "", **extra: Any) -> dict:
    rec = {
        "time": datetime.now().isoformat(timespec="milliseconds"),
        "type": "image",
        "person": person,
        "source": item.source if item is not None else "",
        "query": item.query if item is not None else "",
        "url": item.url if item is not None else "",
        "page_url": item.page_url if item is not None else "",
        "title": item.title if item is not None else "",
        "status": status,
        "reason": reason,
    }
    if item is not None:
        if item.variant:
            rec["image_variant"] = item.variant
        if item.fallback_cover:
            rec["fallback_cover"] = True
        if item.width or item.height:
            rec["planned_width"] = item.width
            rec["planned_height"] = item.height
    if run_id:
        rec["run_id"] = run_id
    for k, v in extra.items():
        if isinstance(v, np.generic):
            v = v.item()
        rec[k] = v
    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def _write_person_metadata(person: str, out_dir: Path, index: ImageIndex) -> None:
    rows = index.rows(person)
    out_dir.mkdir(parents=True, exist_ok=True)
    fields = [
        "status", "filename", "source", "query", "page_url", "image_url", "width", "height",
        "phash", "similarity", "clarity", "matched_ref", "n_faces", "framing", "person_ratio",
        "framing_reason", "dedupe_stage", "exclude_path", "exclude_distance", "exclude_method",
        "manual_accept", "manual_accept_at", "manual_reason", "time",
        "sr_path", "sr_model", "sr_scale", "sr_at", "sr_face", "sr_width", "sr_height",
        "sr_replaced", "sr_replace_mode", "backup_path", "replaced_at",
    ]
    jsonl_path = out_dir / "metadata.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for r in rows:
            rec = {
                "status": r["status"],
                "filename": Path(r["file_path"]).name if r["file_path"] else "",
                "source": r["source"],
                "query": r["query"],
                "page_url": r["page_url"],
                "image_url": r["url"],
                "width": r["width"],
                "height": r["height"],
                "phash": r["phash"],
                "similarity": r["similarity"],
                "clarity": r["clarity"],
                "matched_ref": r["matched_ref"],
                "n_faces": r["n_faces"],
                "framing": r["framing"],
                "person_ratio": r["person_ratio"],
                "framing_reason": r["framing_reason"],
                "dedupe_stage": r["dedupe_stage"],
                "exclude_path": r["exclude_path"],
                "exclude_distance": r["exclude_distance"],
                "exclude_method": r["exclude_method"],
                "manual_accept": int(r["manual_accept"] or 0),
                "manual_accept_at": r["manual_accept_at"] or "",
                "manual_reason": r["manual_reason"] or "",
                "time": r["created_at"],
                "raw_path": r["raw_path"],
                "file_path": r["file_path"],
                "sr_path": (r["sr_path"] or "") if "sr_path" in r.keys() else "",
                "sr_model": (r["sr_model"] or "") if "sr_model" in r.keys() else "",
                "sr_scale": (r["sr_scale"] or 0) if "sr_scale" in r.keys() else 0,
                "sr_at": (r["sr_at"] or "") if "sr_at" in r.keys() else "",
                "sr_face": (r["sr_face"] or "") if "sr_face" in r.keys() else "",
                "sr_width": (r["sr_width"] or 0) if "sr_width" in r.keys() else 0,
                "sr_height": (r["sr_height"] or 0) if "sr_height" in r.keys() else 0,
                "sr_replaced": bool(r["sr_replaced"]) if "sr_replaced" in r.keys() else False,
                "sr_replace_mode": (r["sr_replace_mode"] or "") if "sr_replace_mode" in r.keys() else "",
                "backup_path": (r["backup_path"] or "") if "backup_path" in r.keys() else "",
                "replaced_at": (r["replaced_at"] or "") if "replaced_at" in r.keys() else "",
            }
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    with (out_dir / "metadata.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            writer.writerow(
                {
                    "status": r["status"],
                    "filename": Path(r["file_path"]).name if r["file_path"] else "",
                    "source": r["source"],
                    "query": r["query"],
                    "page_url": r["page_url"],
                    "image_url": r["url"],
                    "width": r["width"],
                    "height": r["height"],
                    "phash": r["phash"],
                    "similarity": r["similarity"],
                    "clarity": r["clarity"],
                    "matched_ref": r["matched_ref"],
                    "n_faces": r["n_faces"],
                    "framing": r["framing"],
                    "person_ratio": r["person_ratio"],
                    "framing_reason": r["framing_reason"],
                    "dedupe_stage": r["dedupe_stage"],
                    "exclude_path": r["exclude_path"],
                    "exclude_distance": r["exclude_distance"],
                    "exclude_method": r["exclude_method"],
                    "manual_accept": int(r["manual_accept"] or 0),
                    "manual_accept_at": r["manual_accept_at"] or "",
                    "manual_reason": r["manual_reason"] or "",
                    "time": r["created_at"],
                    "sr_path": (r["sr_path"] or "") if "sr_path" in r.keys() else "",
                    "sr_model": (r["sr_model"] or "") if "sr_model" in r.keys() else "",
                    "sr_scale": (r["sr_scale"] or 0) if "sr_scale" in r.keys() else 0,
                    "sr_at": (r["sr_at"] or "") if "sr_at" in r.keys() else "",
                    "sr_face": (r["sr_face"] or "") if "sr_face" in r.keys() else "",
                    "sr_width": (r["sr_width"] or 0) if "sr_width" in r.keys() else 0,
                    "sr_height": (r["sr_height"] or 0) if "sr_height" in r.keys() else 0,
                    "sr_replaced": "true" if ("sr_replaced" in r.keys() and r["sr_replaced"]) else "false",
                    "sr_replace_mode": (r["sr_replace_mode"] or "") if "sr_replace_mode" in r.keys() else "",
                    "backup_path": (r["backup_path"] or "") if "backup_path" in r.keys() else "",
                    "replaced_at": (r["replaced_at"] or "") if "replaced_at" in r.keys() else "",
                }
            )
