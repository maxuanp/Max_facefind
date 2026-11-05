"""屏蔽名单：图片级 / 人员级「永久不再出现」。

独立 JSON 文件存储（默认 data/blocklist.json），与回收站、索引删除状态无关：
- 屏蔽只影响「图片墙显示」与「后续采集判定」，不移动、不删除任何文件；
- 记录图片的 URL / pHash / dHash / sha256 与记录 id，避免只靠文件名；
- 解除屏蔽后图片仍在，立刻恢复显示。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

_LOCK = threading.RLock()
_MASK64 = 0xFFFFFFFFFFFFFFFF


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm_hash(v: Any) -> int | None:
    try:
        if v is None or v == "":
            return None
        return int(v) & _MASK64
    except (TypeError, ValueError):
        return None


class BlockList:
    """data/blocklist.json 的读写封装；并发写用进程内锁串行化。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    # ---------- 读写 ----------
    def _load(self) -> dict:
        if not self.path.exists():
            return {"images": {}, "people": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"images": {}, "people": {}}
        if not isinstance(data, dict):
            return {"images": {}, "people": {}}
        data.setdefault("images", {})
        data.setdefault("people", {})
        if not isinstance(data["images"], dict):
            data["images"] = {}
        if not isinstance(data["people"], dict):
            data["people"] = {}
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def load(self) -> dict:
        with _LOCK:
            return self._load()

    # ---------- 查询 ----------
    def image_ids(self) -> set[int]:
        data = self._load()
        out: set[int] = set()
        for key in data["images"]:
            try:
                out.add(int(key))
            except (TypeError, ValueError):
                continue
        return out

    def people(self) -> set[str]:
        return {str(k) for k in self._load()["people"].keys()}

    def images(self) -> list[dict]:
        data = self._load()
        return [dict(v, id=int(k)) for k, v in data["images"].items()
                if str(k).lstrip("-").isdigit()]

    def people_entries(self) -> list[dict]:
        data = self._load()
        return [dict(v, person=str(k)) for k, v in data["people"].items()]

    def is_person_blocked(self, person: str) -> bool:
        return (person or "") in self._load()["people"]

    def match_url(self, url: str) -> dict | None:
        if not url:
            return None
        for ent in self._load()["images"].values():
            if ent.get("url") and ent["url"] == url:
                return ent
        return None

    def match_content(self, phash: int | None, dhash: int | None,
                      sha256: str = "", phash_threshold: int = 8,
                      dhash_threshold: int = 10) -> dict | None:
        """内容级命中：sha256 精确命中优先，其次 pHash / dHash 汉明距离。"""
        entries = self._load()["images"].values()
        ph = _norm_hash(phash)
        dh = _norm_hash(dhash)
        best: tuple[dict, str, int] | None = None
        for ent in entries:
            if sha256 and ent.get("sha256") and ent["sha256"] == sha256:
                return ent
            e_ph = _norm_hash(ent.get("phash"))
            e_dh = _norm_hash(ent.get("dhash"))
            if ph is not None and e_ph is not None:
                dist = (ph ^ e_ph).bit_count()
                if dist <= phash_threshold:
                    return ent
            if dh is not None and e_dh is not None:
                dist = (dh ^ e_dh).bit_count()
                if dist <= dhash_threshold and (best is None or dist < best[2]):
                    best = (ent, "dhash", dist)
        return best[0] if best else None

    # ---------- 写入 ----------
    def add_images(self, records: Iterable[dict]) -> list[dict]:
        """按记录 id 去重写入；已存在的条目更新指纹与时间。"""
        added: list[dict] = []
        with _LOCK:
            data = self._load()
            for rec in records:
                rid = rec.get("id")
                if rid is None:
                    continue
                key = str(int(rid))
                old = data["images"].get(key) or {}
                ent = {
                    "id": int(rid),
                    "person": rec.get("person", old.get("person", "")),
                    "filename": rec.get("filename", old.get("filename", "")),
                    "url": rec.get("url", old.get("url", "")),
                    "sha256": rec.get("sha256", old.get("sha256", "")),
                    "phash": _norm_hash(rec.get("phash")) if rec.get("phash") is not None
                    else old.get("phash"),
                    "dhash": _norm_hash(rec.get("dhash")) if rec.get("dhash") is not None
                    else old.get("dhash"),
                    "source": rec.get("source", old.get("source", "")),
                    "reason": rec.get("reason", old.get("reason", "")),
                    "blocked_at": old.get("blocked_at") or _now(),
                }
                data["images"][key] = ent
                added.append(ent)
            self._save(data)
        return added

    def add_people(self, names: Iterable[str], reason: str = "") -> list[str]:
        added: list[str] = []
        with _LOCK:
            data = self._load()
            for name in names:
                name = (name or "").strip()
                if not name:
                    continue
                if name not in data["people"]:
                    data["people"][name] = {"person": name, "reason": reason,
                                           "blocked_at": _now()}
                    added.append(name)
            self._save(data)
        return added

    def remove_images(self, ids: Iterable[int]) -> int:
        removed = 0
        with _LOCK:
            data = self._load()
            for rid in ids:
                if str(int(rid)) in data["images"]:
                    del data["images"][str(int(rid))]
                    removed += 1
            self._save(data)
        return removed

    def remove_people(self, names: Iterable[str]) -> int:
        removed = 0
        with _LOCK:
            data = self._load()
            for name in names:
                if name in data["people"]:
                    del data["people"][name]
                    removed += 1
            self._save(data)
        return removed

    def clear(self, what: str = "all") -> int:
        removed = 0
        with _LOCK:
            data = self._load()
            if what in ("all", "images"):
                removed += len(data["images"])
                data["images"] = {}
            if what in ("all", "people"):
                removed += len(data["people"])
                data["people"] = {}
            self._save(data)
        return removed


def from_cfg(cfg) -> BlockList:
    return BlockList(cfg.path("data/blocklist.json"))
