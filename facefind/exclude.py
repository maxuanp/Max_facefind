from __future__ import annotations

import json
import os
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .config import Config, ExcludeConfig
from .dedupe import dhash, phash_variants, sha256_bytes, to_signed64, from_signed64
from .index import ImageIndex

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}

# 写入中的临时文件后缀（浏览器/下载器/Office 常见命名）
TEMP_SUFFIXES = {
    ".tmp", ".part", ".partial", ".crdownload", ".download", ".filepart",
    ".opdownload", ".swp", ".lock", ".!ut", ".bf", ".aria2",
}


def norm_path(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def is_temp_name(path: str | Path) -> bool:
    p = Path(path)
    name = p.name.lower()
    return (
        name.startswith("~$")
        or name.startswith(".~")
        or name.endswith(".tmp")
        or p.suffix.lower() in TEMP_SUFFIXES
    )


def skip_roots(cfg: Config) -> list[str]:
    """要跳过的绝对路径前缀：程序自身输出目录 + 用户配置的 watch_skip。"""
    items: list[str] = []
    root = Path(cfg.root)
    for raw in (cfg.output_dir, cfg.raw_dir, str(Path(cfg.index_db).parent), *cfg.exclude.watch_skip):
        raw = str(raw or "").strip()
        if not raw:
            continue
        p = Path(raw)
        if not p.is_absolute():
            p = root / p
        try:
            p = p.resolve()
        except OSError:
            p = Path(os.path.abspath(str(p)))
        norm = norm_path(p)
        if norm not in items:
            items.append(norm)
    return items


def build_skip(cfg: Config) -> Callable[[Path], bool]:
    """返回「是否跳过」判定：临时文件（写入中）+ 输出目录/缓存目录。"""
    roots = skip_roots(cfg)

    def _skip(path: str | Path) -> bool:
        if is_temp_name(path):
            return True
        norm = norm_path(path)
        return any(norm == r or norm.startswith(r + os.sep) for r in roots)

    return _skip


# 进程内版本号：监听线程写入后自增，长期存活的 ExcludeIndex 实例据此惰性重载。
_VERSION = 0


def bump_version() -> None:
    global _VERSION
    _VERSION += 1


def version() -> int:
    return _VERSION


# 最近一次「任务前是否跳过扫描」的决策，供 UI / 日志回显
_LAST_DECISION: dict[str, Any] = {
    "at": "", "skipped": False, "reason": "", "mode": "", "seconds": 0.0,
}


def record_scan_decision(skipped: bool, reason: str, mode: str, seconds: float = 0.0) -> None:
    _LAST_DECISION.update({
        "at": datetime.now().isoformat(timespec="seconds"),
        "skipped": bool(skipped),
        "reason": reason,
        "mode": mode,
        "seconds": round(float(seconds), 3),
    })


def last_scan_decision() -> dict[str, Any]:
    return dict(_LAST_DECISION)


def decide_scan(cfg: Config, dirs: list[str], files: list[str], rebuild: bool = False) -> tuple[bool, str]:
    """任务开始时是否需要扫描排除目录。返回 (是否扫描, 原因)。

    - always：总是扫描
    - never：总是跳过（用户显式接受「只依赖监听」的风险）
    - auto：仅当监听健康、启动对账完成、无中断、无积压事件、排除目标与监听目标一致时跳过
    """
    mode = str(cfg.exclude.scan_on_run or "auto")
    if rebuild:
        return True, f"本次要求重建排除索引（scan_on_run={mode}），执行完整扫描"
    if mode == "always":
        return True, "scan_on_run=always：执行完整增量扫描"
    if mode == "never":
        return False, "scan_on_run=never：跳过扫描（仅依赖实时监听，风险自负）"
    try:
        from .watcher import active_health
    except ImportError:
        return True, "因监听不可用，本次执行完整增量扫描（未安装 watchdog）"
    ok, reason = active_health(cfg, dirs, files)
    if ok:
        return False, "实时监听健康（对账已完成、无中断、无积压），跳过任务前增量扫描"
    return True, f"因监听不可用，本次执行完整增量扫描（{reason}）"


@dataclass
class ScanReport:
    files_seen: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    undecodable: int = 0
    pruned: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = (
            f"排除目录索引：扫描 {self.files_seen} 个文件，新增 {self.added}，更新 {self.updated}，"
            f"未变 {self.unchanged}，损坏/不可解码 {self.undecodable}，清理失效 {self.pruned}"
        )
        if self.skipped:
            text += f"，跳过程序输出/临时文件 {self.skipped}"
        return text


@dataclass
class ExcludeHit:
    path: str
    distance: int
    method: str  # exact_hash | phash | dhash | phash_crop


def iter_images(target: str, recursive: bool,
                skip: Callable[[Path], bool] | None = None) -> tuple[list[Path], list[str], int]:
    """收集图片文件；目录不存在时返回告警，不抛异常。

    skip 为可选过滤器（如程序自身输出目录/临时文件），被跳过的数量单独返回。
    """
    p = Path(target).expanduser()
    warnings: list[str] = []
    if p.is_file():
        return [p], warnings, 0
    if not p.exists():
        warnings.append(f"[排除目录] 路径不存在，已跳过：{target}")
        return [], warnings, 0
    pattern = "**/*" if recursive else "*"
    files = [f for f in p.glob(pattern) if f.is_file() and f.suffix.lower() in IMAGE_SUFFIXES]
    skipped = 0
    if skip is not None:
        kept: list[Path] = []
        for f in files:
            if skip(f):
                skipped += 1
            else:
                kept.append(f)
        files = kept
    return sorted(files), warnings, skipped


class ExcludeIndex:
    """排除目录索引：只读扫描 + 持久化 + 候选图查重。

    该类只读取排除目录中的文件，绝不写入、移动或删除。
    """

    def __init__(self, index: ImageIndex, cfg: ExcludeConfig):
        self.index = index
        self.cfg = cfg
        self._sha_map: dict[str, str] = {}
        self._paths: list[str] = []
        self._ph = np.zeros(0, dtype=np.uint64)
        self._variants = np.zeros((0, 3), dtype=np.uint64)
        self._variant_mask = np.zeros((0, 3), dtype=bool)
        self._dh = np.zeros(0, dtype=np.uint64)
        self._has_dh = np.zeros(0, dtype=bool)
        self._loaded = False
        self._version = -1
        # 候选图哈希的进程内 LRU：同一内容（sha 相同）只算一次变体 pHash / dHash
        self._cand_cache: OrderedDict[str, tuple[list[int], int]] = OrderedDict()
        self._cand_cache_max = 512

    # ---------- 扫描 ----------
    def scan(self, dirs: list[str], files: list[str], rebuild: bool = False,
             skip: Callable[[Path], bool] | None = None) -> ScanReport:
        report = ScanReport()
        all_targets = list(dirs) + list(files)
        if not all_targets:
            self._load()
            return report

        if rebuild:
            self.index.clear_exclude_table()
            report.warnings.append("[排除目录] 已按 --exclude-rebuild 清空索引并重建")

        keep: set[str] = set()
        for target in all_targets:
            paths, warns, skipped = iter_images(target, self.cfg.recursive, skip)
            report.warnings.extend(warns)
            report.skipped += skipped
            for fp in paths:
                keep.add(str(fp.resolve()))
            for fp in paths:
                self._scan_file(fp, report)

        report.pruned = self.index.prune_exclude(keep)
        bump_version()
        self._load()
        return report

    def _scan_file(self, fp: Path, report: ScanReport) -> str:
        path = str(fp.resolve())
        report.files_seen += 1
        try:
            st = fp.stat()
        except OSError as exc:
            report.warnings.append(f"[排除目录] 读取属性失败，已跳过：{path}（{exc}）")
            return "skipped"

        row = self.index.exclude_row(path)
        if (
            row is not None
            and not self.cfg.rebuild
            and int(row["size"]) == st.st_size
            and abs(float(row["mtime"]) - st.st_mtime) <= 1.0
            and (row["phash"] is not None or int(row["size"]) < 4096)
        ):
            report.unchanged += 1
            return "unchanged"

        try:
            data = fp.read_bytes()
        except OSError as exc:
            report.warnings.append(f"[排除目录] 读取失败，已跳过：{path}（{exc}）")
            return "skipped"

        sha = sha256_bytes(data)
        img = None
        try:
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        except Exception:
            img = None

        variants: list[int] = []
        dh: int | None = None
        if img is None:
            report.undecodable += 1
            report.warnings.append(f"[排除目录] 无法解码，已跳过（仍记录字节哈希）：{path}")
        else:
            variants = phash_variants(img, self.cfg.crop_tolerance)
            dh = dhash(img)

        self.index.upsert_exclude(
            {
                "path": path,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "sha256": sha,
                "phash": variants[0] if variants else None,
                "dhash": dh,
                "variants": json.dumps(variants) if variants else "",
                "added_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        if row is None:
            report.added += 1
            return "added"
        report.updated += 1
        return "updated"

    # ---------- 监听写入（不走全量扫描，单文件粒度） ----------
    def refresh_file(self, fp: str | Path) -> str:
        """把单个文件刷新进排除索引（新增/修改/改名后的新路径）。

        返回 added / updated / unchanged / skipped；会原子 upsert，不留半成品行。
        """
        report = ScanReport()
        status = self._scan_file(Path(fp), report)
        if status in ("added", "updated"):
            bump_version()
        return status

    def remove_file(self, path: str | Path) -> int:
        """删除单个文件的排除索引行（文件被删除/改名离开时）。"""
        removed = self.index.delete_exclude_path(str(path))
        if removed:
            bump_version()
        return removed

    def remove_tree(self, target: str | Path) -> int:
        """删除某目录下所有排除索引行（整目录被删除/移走时）。"""
        removed = self.index.delete_exclude_rows(str(target), prefix=True)
        if removed:
            bump_version()
        return removed

    def reload(self) -> None:
        """重新从数据库载入内存数组（跳过扫描时确保 lookup 看到最新行）。"""
        self._load()

    # ---------- 载入缓存 ----------
    def _load(self) -> None:
        rows = self.index.all_exclude_rows()
        self._paths = [r["path"] for r in rows]
        self._sha_map = {r["sha256"]: r["path"] for r in rows if r["sha256"]}
        ph: list[int] = []
        dh: list[int] = []
        variants = np.zeros((len(rows), 3), dtype=np.uint64)
        mask = np.zeros((len(rows), 3), dtype=bool)
        has_dh = np.zeros(len(rows), dtype=bool)
        for i, r in enumerate(rows):
            ph.append(from_signed64(r["phash"]) if r["phash"] is not None else 0)
            if r["dhash"] is not None:
                dh.append(from_signed64(r["dhash"]))
                has_dh[i] = True
            else:
                dh.append(0)
            if r["variants"]:
                try:
                    vals = json.loads(r["variants"])
                except json.JSONDecodeError:
                    vals = []
                for j, v in enumerate(vals[:3]):
                    variants[i, j] = np.uint64(v & 0xFFFFFFFFFFFFFFFF)
                    mask[i, j] = True
        self._ph = np.array(ph, dtype=np.uint64)
        self._dh = np.array(dh, dtype=np.uint64)
        self._has_dh = has_dh
        self._variants = variants
        self._variant_mask = mask
        self._loaded = True
        self._version = version()

    # ---------- 查重 ----------
    def candidate_hashes(self, sha: str, img: np.ndarray,
                         ph: int | None = None, dh: int | None = None) -> tuple[list[int], int]:
        """候选图的（多尺度变体 pHash, dHash）；按内容 sha 缓存，重复内容不重算。

        ph/dh 为调用方已算好的全图 pHash / dHash，传入则复用，不重复计算。
        """
        got = self._cand_cache.get(sha)
        if got is not None:
            self._cand_cache.move_to_end(sha)
            return got
        variants = phash_variants(img, self.cfg.crop_tolerance, base=ph)
        dh_val = dhash(img) if dh is None else dh
        self._cand_cache[sha] = (variants, dh_val)
        if len(self._cand_cache) > self._cand_cache_max:
            self._cand_cache.popitem(last=False)
        return variants, dh_val

    def lookup(self, sha: str, img: np.ndarray | None = None,
               ph: int | None = None, dh: int | None = None) -> ExcludeHit | None:
        """精确哈希 → pHash（多尺度/中心裁剪）→ dHash。命中返回 ExcludeHit。

        ph/dh 为调用方已算好的全图 pHash / dHash；变体 pHash 仅在精确哈希未命中时才惰性计算。
        """
        if not self._loaded or self._version != version():
            self._load()
        if not self._paths:
            return None

        hit_path = self._sha_map.get(sha)
        if hit_path:
            return ExcludeHit(path=hit_path, distance=0, method="exact_hash")
        if img is None:
            return None

        cand_variants, cand_dh = self.candidate_hashes(sha, img, ph, dh)
        threshold = self.cfg.phash_threshold
        if self._ph.size:
            cand = np.array(cand_variants, dtype=np.uint64)
            # (n, k_candidate, 3_stored)
            xor = self._variants[:, None, :] ^ cand[None, :, None]
            bits = _bit_count(xor)
            bits = np.where(self._variant_mask[:, None, :], bits, np.iinfo(np.int32).max)
            per_row = bits.min(axis=(1, 2))
            idx = int(np.argmin(per_row))
            dist = int(per_row[idx])
            if dist <= threshold:
                method = "phash" if dist == _single_phash_distance(self._ph[idx], cand_variants[0]) else "phash_crop"
                return ExcludeHit(path=self._paths[idx], distance=dist, method=method)

        if self.cfg.dhash_threshold >= 0 and self._has_dh.any():
            cand_dh = np.uint64(cand_dh)
            xor = self._dh ^ cand_dh
            bits = _bit_count(xor)
            bits = np.where(self._has_dh, bits, np.iinfo(np.int32).max)
            idx = int(np.argmin(bits))
            dist = int(bits[idx])
            if dist <= self.cfg.dhash_threshold:
                return ExcludeHit(path=self._paths[idx], distance=dist, method="dhash")
        return None


def _bit_count(arr: np.ndarray) -> np.ndarray:
    try:
        return np.bitwise_count(arr).astype(np.int32)
    except AttributeError:  # 兼容旧 numpy
        import numpy as _np

        out = _np.zeros_like(arr, dtype=_np.int32)
        tmp = arr.copy()
        for _ in range(64):
            out += (tmp & _np.uint64(1)).astype(_np.int32)
            tmp >>= _np.uint64(1)
        return out


def _single_phash_distance(stored: np.uint64, candidate: int) -> int:
    return int(((int(stored) ^ (candidate & 0xFFFFFFFFFFFFFFFF)) & 0xFFFFFFFFFFFFFFFF).bit_count())
