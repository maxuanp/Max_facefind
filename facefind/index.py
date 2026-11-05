from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from .dedupe import from_signed64, to_signed64

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    query TEXT DEFAULT '',
    page_url TEXT DEFAULT '',
    url TEXT NOT NULL,
    sha256 TEXT DEFAULT '',
    phash INTEGER,
    width INTEGER DEFAULT 0,
    height INTEGER DEFAULT 0,
    area INTEGER DEFAULT 0,
    sharpness REAL DEFAULT 0,
    compression REAL DEFAULT 0,
    face_ratio REAL DEFAULT 0,
    clarity REAL DEFAULT 0,
    similarity REAL,
    matched_ref TEXT DEFAULT '',
    n_faces INTEGER DEFAULT 0,
    framing TEXT DEFAULT '',
    person_ratio REAL DEFAULT 0,
    framing_reason TEXT DEFAULT '',
    status TEXT DEFAULT '',
    raw_path TEXT DEFAULT '',
    file_path TEXT DEFAULT '',
    embedding BLOB,
    dedupe_stage TEXT DEFAULT '',
    url_key TEXT DEFAULT '',
    exclude_path TEXT DEFAULT '',
    exclude_distance INTEGER,
    exclude_method TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(person, url)
);
CREATE INDEX IF NOT EXISTS ix_images_person_status ON images(person, status);
CREATE INDEX IF NOT EXISTS ix_images_person_phash ON images(person, phash);
CREATE INDEX IF NOT EXISTS ix_images_sha ON images(person, sha256);

CREATE TABLE IF NOT EXISTS exclude_files (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    sha256 TEXT DEFAULT '',
    phash INTEGER,
    dhash INTEGER,
    variants TEXT DEFAULT '',
    added_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_exclude_sha ON exclude_files(sha256);

CREATE TABLE IF NOT EXISTS blocked (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    phash INTEGER,
    dhash INTEGER,
    person TEXT DEFAULT '',
    source_site TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    origin TEXT DEFAULT 'manual'
);
CREATE INDEX IF NOT EXISTS ix_blocked_phash ON blocked(phash);
CREATE INDEX IF NOT EXISTS ix_blocked_dhash ON blocked(dhash);

-- 笔记级图片清单：记录某篇笔记一共有多少张图、每张图的归一化 key，
-- 用于「整篇笔记的图都已入库」时精确跳过详情页，避免重复打开同一批笔记。
CREATE TABLE IF NOT EXISTS note_meta (
    person TEXT NOT NULL,
    note_key TEXT NOT NULL,
    total INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (person, note_key)
);
CREATE TABLE IF NOT EXISTS note_images (
    person TEXT NOT NULL,
    note_key TEXT NOT NULL,
    image_key TEXT NOT NULL,
    url TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (person, note_key, image_key)
);
CREATE INDEX IF NOT EXISTS ix_note_images_note ON note_images(person, note_key);
"""

COLUMNS = (
    "person", "source", "query", "page_url", "url", "sha256", "phash", "width", "height",
    "area", "sharpness", "compression", "face_ratio", "clarity", "similarity", "matched_ref",
    "n_faces", "framing", "person_ratio", "framing_reason", "status", "raw_path", "file_path",
    "embedding", "dedupe_stage", "url_key", "exclude_path", "exclude_distance", "exclude_method",
    "created_at",
)


class ImageIndex:
    """跨运行索引：URL 去重、pHash 去重、人脸 embedding 去重的数据来源。"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """旧库增量补列，不重建表、不影响已有数据。"""
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(images)")}
        for col, ddl in (
            ("framing", "TEXT DEFAULT ''"),
            ("person_ratio", "REAL DEFAULT 0"),
            ("framing_reason", "TEXT DEFAULT ''"),
            ("deleted_at", "TEXT DEFAULT ''"),
            ("recycle_path", "TEXT DEFAULT ''"),
            ("prev_status", "TEXT DEFAULT ''"),
            ("manual_accept", "INTEGER DEFAULT 0"),
            ("manual_accept_at", "TEXT DEFAULT ''"),
            ("manual_reason", "TEXT DEFAULT ''"),
            ("sr_path", "TEXT DEFAULT ''"),
            ("sr_model", "TEXT DEFAULT ''"),
            ("sr_scale", "INTEGER DEFAULT 0"),
            ("sr_at", "TEXT DEFAULT ''"),
            ("sr_face", "TEXT DEFAULT ''"),
            ("sr_width", "INTEGER DEFAULT 0"),
            ("sr_height", "INTEGER DEFAULT 0"),
            ("sr_replaced", "INTEGER DEFAULT 0"),
            ("sr_replace_mode", "TEXT DEFAULT ''"),
            ("backup_path", "TEXT DEFAULT ''"),
            ("replaced_at", "TEXT DEFAULT ''"),
            ("sr_original_path", "TEXT DEFAULT ''"),
            ("url_key", "TEXT DEFAULT ''"),
        ):
            if col not in existing:
                self.conn.execute(f"ALTER TABLE images ADD COLUMN {col} {ddl}")

        # 老库回填 url_key（一次性，只算已有 URL）
        rows = self.conn.execute(
            "SELECT id, url FROM images WHERE COALESCE(url_key, '') = '' AND COALESCE(url, '') != ''"
        ).fetchall()
        if rows:
            from .urlkey import canonical_url

            with self.conn:
                for r in rows:
                    self.conn.execute(
                        "UPDATE images SET url_key=? WHERE id=?", (canonical_url(r["url"]), r["id"])
                    )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_images_person_urlkey ON images(person, url_key)"
        )

    # ---------- 查询 ----------
    def has_url(self, person: str, url: str) -> bool:
        row = self.conn.execute(
            "SELECT status FROM images WHERE person=? AND url=? LIMIT 1", (person, url)
        ).fetchone()
        return bool(row) and row["status"] not in ("failed",)

    def has_url_key(self, person: str, key: str) -> sqlite3.Row | None:
        """按归一化 key 命中历史记录（同一张图的不同尺寸/参数变体）。"""
        if not person or not key:
            return None
        return self.conn.execute(
            "SELECT * FROM images WHERE person=? AND url_key=? AND status != 'failed' LIMIT 1",
            (person, key),
        ).fetchone()

    def note_media(self, person: str, note_key: str) -> dict:
        """某篇笔记已记录的图片清单：{"total": 声明总数, "items": [row, ...]}。"""
        if not person or not note_key:
            return {"total": 0, "items": []}
        meta = self.conn.execute(
            "SELECT total FROM note_meta WHERE person=? AND note_key=?", (person, note_key)
        ).fetchone()
        items = self.conn.execute(
            "SELECT image_key, url FROM note_images WHERE person=? AND note_key=?",
            (person, note_key),
        ).fetchall()
        return {"total": int(meta["total"]) if meta else 0, "items": items}

    def record_note_media(self, person: str, note_key: str, total: int,
                          images: list[dict]) -> int:
        """写入/更新笔记图片清单；images 每项含 key(url_key) 与 url。返回写入条数。"""
        if not person or not note_key:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        written = 0
        with self.conn:
            self.conn.execute(
                "INSERT INTO note_meta (person, note_key, total, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(person, note_key) DO UPDATE SET total=excluded.total, "
                "updated_at=excluded.updated_at",
                (person, note_key, max(0, int(total)), now),
            )
            for it in images:
                key = str(it.get("key") or "")
                if not key:
                    continue
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO note_images (person, note_key, image_key, url, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (person, note_key, key, str(it.get("url") or ""), now),
                )
                written += cur.rowcount
        return written

    def by_sha(self, person: str, sha: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM images WHERE person=? AND sha256=? AND status != 'failed' LIMIT 1",
            (person, sha),
        ).fetchone()

    def near_dupes(self, person: str, phash: int, threshold: int) -> list[tuple[sqlite3.Row, int]]:
        rows = self.conn.execute(
            "SELECT * FROM images WHERE person=? AND status IN ('saved','review') AND phash IS NOT NULL"
            " AND COALESCE(manual_accept,0)=0",
            (person,),
        ).fetchall()
        target = from_signed64(phash)
        out: list[tuple[sqlite3.Row, int]] = []
        for row in rows:
            dist = ((from_signed64(row["phash"]) ^ target) & 0xFFFFFFFFFFFFFFFF).bit_count()
            if dist <= threshold:
                out.append((row, dist))
        return sorted(out, key=lambda x: x[1])

    def saved_embeddings(self, person: str) -> list[tuple[sqlite3.Row, list[float]]]:
        import numpy as np

        rows = self.conn.execute(
            "SELECT * FROM images WHERE person=? AND status='saved' AND embedding IS NOT NULL"
            " AND COALESCE(manual_accept,0)=0",
            (person,),
        ).fetchall()
        out = []
        for row in rows:
            vec = np.frombuffer(row["embedding"], dtype=np.float32)
            if vec.size:
                out.append((row, vec))
        return out

    def rows(self, person: str, statuses: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
        if statuses:
            marks = ",".join("?" * len(statuses))
            return self.conn.execute(
                f"SELECT * FROM images WHERE person=? AND status IN ({marks}) ORDER BY id",
                (person, *statuses),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM images WHERE person=? ORDER BY id", (person,)
        ).fetchall()

    def _gallery_clause(self, person: str, statuses: tuple[str, ...], framing: str = "",
                        exclude_ids: set[int] | None = None,
                        exclude_people: set[str] | None = None,
                        include_ids: set[int] | None = None) -> tuple[str, list[Any]]:
        """图片墙公共筛选条件（屏蔽名单在这里排除，屏蔽记录 id 不会出现在正常视图）。"""
        marks = ",".join("?" * len(statuses))
        sql = f"status IN ({marks})"
        params: list[Any] = list(statuses)
        if person:
            sql += " AND person=?"
            params.append(person)
        if framing:
            sql += " AND framing=?"
            params.append(framing)
        if include_ids is not None:
            ids = sorted(int(x) for x in include_ids)
            if not ids:
                return "1=0", []
            sql += f" AND id IN ({','.join('?' * len(ids))})"
            params.extend(ids)
        if exclude_ids:
            ids = sorted(int(x) for x in exclude_ids)
            sql += f" AND id NOT IN ({','.join('?' * len(ids))})"
            params.extend(ids)
        if exclude_people:
            names = sorted(str(x) for x in exclude_people)
            sql += f" AND person NOT IN ({','.join('?' * len(names))})"
            params.extend(names)
        return sql, params

    def gallery(self, person: str, statuses: tuple[str, ...], framing: str = "",
                offset: int = 0, limit: int = 24, exclude_ids: set[int] | None = None,
                exclude_people: set[str] | None = None,
                include_ids: set[int] | None = None) -> list[sqlite3.Row]:
        """分页取图：按 id 倒序（新图在前），可按构图标签过滤。person 为空表示全部人员。"""
        where, params = self._gallery_clause(person, statuses, framing,
                                             exclude_ids, exclude_people, include_ids)
        sql = f"SELECT * FROM images WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?"
        return self.conn.execute(sql, (*params, limit, offset)).fetchall()

    def gallery_ids(self, person: str, statuses: tuple[str, ...], framing: str = "",
                    limit: int = 10000, exclude_ids: set[int] | None = None,
                    exclude_people: set[str] | None = None,
                    include_ids: set[int] | None = None) -> list[int]:
        """当前筛选条件下的全部记录 id（用于跨页全选），按 id 倒序。"""
        where, params = self._gallery_clause(person, statuses, framing,
                                             exclude_ids, exclude_people, include_ids)
        sql = f"SELECT id FROM images WHERE {where} ORDER BY id DESC LIMIT ?"
        return [int(r["id"]) for r in self.conn.execute(sql, (*params, int(limit)))]

    def gallery_total(self, person: str, statuses: tuple[str, ...], framing: str = "",
                      exclude_ids: set[int] | None = None,
                      exclude_people: set[str] | None = None,
                      include_ids: set[int] | None = None) -> int:
        where, params = self._gallery_clause(person, statuses, framing,
                                             exclude_ids, exclude_people, include_ids)
        sql = f"SELECT COUNT(*) c FROM images WHERE {where}"
        return int(self.conn.execute(sql, params).fetchone()["c"])

    def framing_counts(self, person: str, statuses: tuple[str, ...],
                       exclude_ids: set[int] | None = None,
                       exclude_people: set[str] | None = None) -> dict[str, int]:
        where, params = self._gallery_clause(person, statuses, "",
                                             exclude_ids, exclude_people, None)
        sql = f"SELECT framing, COUNT(*) c FROM images WHERE {where} GROUP BY framing"
        return {(r["framing"] or "unknown"): int(r["c"]) for r in self.conn.execute(sql, params)}

    def people_overview(self) -> dict[str, dict]:
        """每人成品/复核/丢弃数量与最近活动时间（来自索引，服务重启后依然可用）。"""
        out: dict[str, dict] = {}
        sql = (
            "SELECT person, status, COUNT(*) c, MAX(created_at) t FROM images "
            "GROUP BY person, status"
        )
        for r in self.conn.execute(sql):
            item = out.setdefault(r["person"], {"saved": 0, "review": 0, "rejected": 0,
                                                "total": 0, "last_run": ""})
            status = r["status"] or "unknown"
            if status == "deleted":
                continue
            if status in ("saved", "review"):
                item[status] += int(r["c"])
            elif status in ("rejected", "duplicate", "discarded", "failed"):
                item["rejected"] += int(r["c"])
            item["total"] += int(r["c"])
            if (r["t"] or "") > item["last_run"]:
                item["last_run"] = r["t"] or ""
        return out

    def summary(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) c FROM images").fetchone()["c"]
        by_status = {
            r["status"] or "unknown": r["c"]
            for r in self.conn.execute("SELECT status, COUNT(*) c FROM images GROUP BY status")
        }
        by_person = {
            r["person"]: r["c"]
            for r in self.conn.execute(
                "SELECT person, COUNT(*) c FROM images WHERE status IN ('saved','review') GROUP BY person"
            )
        }
        return {"total": total, "by_status": by_status, "by_person": by_person, "db": str(self.path)}

    # ---------- 写入 ----------
    def upsert(self, record: dict) -> int:
        if record.get("url"):
            from .urlkey import canonical_url

            record["url_key"] = canonical_url(record["url"])
        record = {k: record.get(k) for k in COLUMNS}
        if not record.get("created_at"):
            record["created_at"] = datetime.now().isoformat(timespec="seconds")
        record["phash"] = to_signed64(record["phash"]) if record.get("phash") is not None else None
        sql = (
            f"INSERT INTO images ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))}) "
            "ON CONFLICT(person, url) DO UPDATE SET "
            + ",".join(f"{c}=excluded.{c}" for c in COLUMNS if c not in ("person", "url", "created_at"))
        )
        with self.conn:
            cur = self.conn.execute(sql, [record[c] for c in COLUMNS])
        row = self.conn.execute(
            "SELECT id FROM images WHERE person=? AND url=?", (record["person"], record["url"])
        ).fetchone()
        return int(row["id"]) if row else int(cur.lastrowid)

    def update(self, row_id: int, **fields) -> None:
        if not fields:
            return
        if "phash" in fields and fields["phash"] is not None:
            fields["phash"] = to_signed64(fields["phash"])
        sets = ",".join(f"{k}=?" for k in fields)
        with self.conn:
            self.conn.execute(f"UPDATE images SET {sets} WHERE id=?", (*fields.values(), row_id))

    def last_activity(self, person: str) -> str:
        row = self.conn.execute(
            "SELECT MAX(created_at) t FROM images WHERE person=?", (person,)
        ).fetchone()
        return (row["t"] or "") if row else ""

    def stats_for(self, person: str) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM images WHERE person=? AND status='saved'", (person,)
        ).fetchone()
        review = self.conn.execute(
            "SELECT COUNT(*) c FROM images WHERE person=? AND status='review'", (person,)
        ).fetchone()
        return {"saved": row["c"], "review": review["c"]}

    # ---------- 删除（回收目录）/ 屏蔽 ----------
    def row_by_id(self, row_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM images WHERE id=?", (int(row_id),)).fetchone()

    def rows_by_ids(self, ids: list[int]) -> list[sqlite3.Row]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return self.conn.execute(
            f"SELECT * FROM images WHERE id IN ({marks}) ORDER BY id", tuple(int(i) for i in ids)
        ).fetchall()

    def mark_deleted(self, row_id: int, recycle_path: str, prev_status: str = "") -> None:
        """成品移入回收目录：保留原记录，只改状态与回收路径。"""
        self.update(
            row_id,
            status="deleted",
            recycle_path=str(recycle_path),
            deleted_at=datetime.now().isoformat(timespec="seconds"),
            prev_status=prev_status or "",
        )

    def restore_row(self, row_id: int, file_path: str, status: str = "saved") -> None:
        self.update(
            row_id,
            status=status or "saved",
            file_path=str(file_path),
            recycle_path="",
            deleted_at="",
        )

    def deleted_rows(self, person: str = "") -> list[sqlite3.Row]:
        sql = "SELECT * FROM images WHERE status='deleted'"
        params: list[Any] = []
        if person:
            sql += " AND person=?"
            params.append(person)
        sql += " ORDER BY id DESC"
        return self.conn.execute(sql, params).fetchall()

    def block_add(self, record: dict) -> int:
        cols = ("url", "phash", "dhash", "person", "source_site", "reason", "created_at", "origin")
        rec = {k: record.get(k) for k in cols}
        rec["phash"] = to_signed64(rec["phash"]) if rec.get("phash") is not None else None
        rec["dhash"] = to_signed64(rec["dhash"]) if rec.get("dhash") is not None else None
        if not rec.get("created_at"):
            rec["created_at"] = datetime.now().isoformat(timespec="seconds")
        if not rec.get("origin"):
            rec["origin"] = "manual"
        sql = (
            f"INSERT INTO blocked ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
            "ON CONFLICT(url) DO UPDATE SET "
            + ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("url", "created_at"))
        )
        with self.conn:
            self.conn.execute(sql, [rec[c] for c in cols])
        row = self.conn.execute("SELECT id FROM blocked WHERE url=?", (rec["url"],)).fetchone()
        return int(row["id"]) if row else 0

    def blocked_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM blocked ORDER BY id DESC").fetchall()

    def blocked_url(self, url: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM blocked WHERE url=? LIMIT 1", (url,)).fetchone()

    def blocked_match(self, phash: int | None, dhash: int | None,
                      phash_threshold: int = 8, dhash_threshold: int = 10
                      ) -> tuple[sqlite3.Row, str, int] | None:
        """内容级黑名单：pHash / dHash 任一命中即返回（优先 pHash）。"""
        if phash is None and dhash is None:
            return None
        rows = self.blocked_rows()
        best: tuple[sqlite3.Row, str, int] | None = None
        target_ph = from_signed64(to_signed64(phash)) if phash is not None else None
        target_dh = from_signed64(to_signed64(dhash)) if dhash is not None else None
        for row in rows:
            if target_ph is not None and row["phash"] is not None:
                dist = (from_signed64(row["phash"]) ^ target_ph).bit_count()
                if dist <= phash_threshold:
                    return row, "phash", dist
            if target_dh is not None and row["dhash"] is not None:
                dist = (from_signed64(row["dhash"]) ^ target_dh).bit_count()
                if dist <= dhash_threshold and (best is None or dist < best[2]):
                    best = (row, "dhash", dist)
        return best

    def block_remove(self, block_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM blocked WHERE id=?", (int(block_id),))
        return cur.rowcount > 0

    def block_remove_url(self, url: str) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM blocked WHERE url=?", (url,))
        return cur.rowcount > 0

    # ---------- 存储清理：索引同步 ----------
    def delete_ids(self, ids: list[int]) -> int:
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self.conn:
            cur = self.conn.execute(f"DELETE FROM images WHERE id IN ({marks})", tuple(int(i) for i in ids))
        return cur.rowcount

    def delete_person(self, person: str) -> int:
        with self.conn:
            cur = self.conn.execute("DELETE FROM images WHERE person=?", (person,))
        return cur.rowcount

    def raw_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, raw_path FROM images WHERE raw_path != ''"
        ).fetchall()

    def clear_raw_paths(self, ids: list[int]) -> int:
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self.conn:
            cur = self.conn.execute(
                f"UPDATE images SET raw_path='' WHERE id IN ({marks})", tuple(int(i) for i in ids)
            )
        return cur.rowcount

    def deleted_with_recycle(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, recycle_path FROM images WHERE status='deleted' AND recycle_path != ''"
        ).fetchall()

    # ---------- 排除目录索引（只读用途，绝不改动文件） ----------
    def exclude_row(self, path: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM exclude_files WHERE path=?", (path,)).fetchone()

    def all_exclude_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM exclude_files").fetchall()

    def upsert_exclude(self, record: dict) -> None:
        record = dict(record)
        record["phash"] = to_signed64(record["phash"]) if record.get("phash") is not None else None
        record["dhash"] = to_signed64(record["dhash"]) if record.get("dhash") is not None else None
        cols = ("path", "size", "mtime", "sha256", "phash", "dhash", "variants", "added_at")
        sql = (
            f"INSERT INTO exclude_files ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
            "ON CONFLICT(path) DO UPDATE SET "
            + ",".join(f"{c}=excluded.{c}" for c in cols if c != "path")
        )
        with self.conn:
            self.conn.execute(sql, [record.get(c) for c in cols])

    def clear_exclude_table(self) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM exclude_files")

    def delete_exclude_path(self, path: str) -> int:
        """删除单个文件的排除索引行（监听删除/改名事件用）；不动磁盘文件。"""
        with self.conn:
            cur = self.conn.execute("DELETE FROM exclude_files WHERE path=?", (path,))
        return int(cur.rowcount or 0)

    def delete_exclude_rows(self, target: str, prefix: bool = False) -> int:
        """按目录（前缀）或单个文件删除排除索引行；只删数据库记录，不动磁盘文件。"""
        base = os.path.normcase(str(Path(target).resolve()))
        victims: list[str] = []
        for row in self.conn.execute("SELECT path FROM exclude_files"):
            p = os.path.normcase(str(Path(row["path"])))
            if p == base or (prefix and p.startswith(base + os.sep)):
                victims.append(row["path"])
        with self.conn:
            for p in victims:
                self.conn.execute("DELETE FROM exclude_files WHERE path=?", (p,))
        return len(victims)

    def prune_exclude(self, keep_paths: set[str]) -> int:
        """删除索引中已不存在的文件记录（只删数据库行，不动文件）。"""
        removed = 0
        with self.conn:
            for row in self.conn.execute("SELECT path FROM exclude_files"):
                if row["path"] not in keep_paths:
                    self.conn.execute("DELETE FROM exclude_files WHERE path=?", (row["path"],))
                    removed += 1
        return removed

    # ---------- 从每人目录的 JSONL 恢复索引 ----------
    def import_metadata(self, person: str, jsonl_path: Path) -> int:
        if not jsonl_path.exists():
            return 0
        imported = 0
        with jsonl_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                url = item.get("image_url") or item.get("url")
                if not url:
                    continue
                file_path = item.get("file_path") or ""
                if not file_path and item.get("filename"):
                    file_path = str(Path(jsonl_path).parent / str(item["filename"]))
                row_id = self.upsert(
                    {
                        "person": person,
                        "source": item.get("source", ""),
                        "query": item.get("query", ""),
                        "page_url": item.get("page_url", ""),
                        "url": url,
                        "sha256": item.get("sha256", ""),
                        "phash": item.get("phash"),
                        "width": item.get("width", 0),
                        "height": item.get("height", 0),
                        "area": item.get("area", 0),
                        "sharpness": item.get("sharpness", 0),
                        "compression": item.get("compression", 0),
                        "face_ratio": item.get("face_ratio", 0),
                        "clarity": item.get("clarity", 0),
                        "similarity": item.get("similarity"),
                        "matched_ref": item.get("matched_ref", ""),
                        "n_faces": item.get("n_faces", 0),
                        "framing": item.get("framing", ""),
                        "person_ratio": item.get("person_ratio", 0),
                        "framing_reason": item.get("framing_reason", ""),
                        "status": item.get("status", "saved"),
                        "raw_path": item.get("raw_path", ""),
                        "file_path": file_path,
                        "embedding": None,
                        "dedupe_stage": item.get("dedupe_stage", ""),
                        "created_at": item.get("time") or datetime.now().isoformat(timespec="seconds"),
                    }
                )
                if item.get("manual_accept"):
                    self.update(
                        row_id,
                        manual_accept=1,
                        manual_accept_at=item.get("manual_accept_at", ""),
                        manual_reason=item.get("manual_reason", ""),
                    )
                if item.get("sr_path"):
                    self.update(
                        row_id,
                        sr_path=item.get("sr_path", ""),
                        sr_model=item.get("sr_model", ""),
                        sr_scale=int(item.get("sr_scale") or 0),
                        sr_at=item.get("sr_at", ""),
                        sr_face=item.get("sr_face", ""),
                        sr_width=int(item.get("sr_width") or 0),
                        sr_height=int(item.get("sr_height") or 0),
                    )
                imported += 1
        return imported
