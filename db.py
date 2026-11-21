"""PhotoNest - SQLite persistence layer.

All timestamps are stored as local ISO strings: YYYY-MM-DDTHH:MM:SS.
"""
from __future__ import annotations

import os
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterable, Sequence
from sources import source_for

ISO = "%Y-%m-%dT%H:%M:%S"

SCHEMA = """
CREATE TABLE IF NOT EXISTS asset (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    path          TEXT UNIQUE NOT NULL,
    root          TEXT,
    dir           TEXT,
    rel_path      TEXT,
    name          TEXT,
    ext           TEXT,
    size          INTEGER DEFAULT 0,
    mtime         REAL DEFAULT 0,
    hash          TEXT,
    width         INTEGER DEFAULT 0,
    height        INTEGER DEFAULT 0,
    taken_at      TEXT,
    camera        TEXT,
    gps_lat       REAL,
    gps_lon       REAL,
    media_type    TEXT DEFAULT 'image',
    is_screenshot INTEGER DEFAULT 0,
    favorite      INTEGER DEFAULT 0,
    deleted       INTEGER DEFAULT 0,
    deleted_at    TEXT,
    created_at    TEXT,
    indexed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_asset_taken   ON asset(taken_at);
CREATE INDEX IF NOT EXISTS idx_asset_hash    ON asset(hash);
CREATE INDEX IF NOT EXISTS idx_asset_dir     ON asset(dir);
CREATE INDEX IF NOT EXISTS idx_asset_root    ON asset(root);
CREATE INDEX IF NOT EXISTS idx_asset_deleted ON asset(deleted);
CREATE INDEX IF NOT EXISTS idx_asset_path_nocase ON asset(path COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS album (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS album_asset (
    album_id TEXT NOT NULL,
    asset_id INTEGER NOT NULL,
    added_at TEXT,
    PRIMARY KEY (album_id, asset_id)
);
CREATE INDEX IF NOT EXISTS idx_album_asset_asset ON album_asset(asset_id);

CREATE TABLE IF NOT EXISTS tag (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS asset_tag (
    asset_id INTEGER NOT NULL,
    tag_id   INTEGER NOT NULL,
    source   TEXT DEFAULT 'user',
    PRIMARY KEY (asset_id, tag_id)
);

CREATE TABLE IF NOT EXISTS root (
    path     TEXT PRIMARY KEY,
    added_at TEXT
);

CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS media_root (
    path TEXT PRIMARY KEY COLLATE NOCASE,
    source_app TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_directory (
    file_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    root TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_index (
    asset_id INTEGER PRIMARY KEY,
    file_size INTEGER NOT NULL,
    file_mtime REAL NOT NULL,
    model_version TEXT NOT NULL,
    ocr_done INTEGER NOT NULL DEFAULT 0,
    ocr_text TEXT NOT NULL DEFAULT '',
    ocr_override TEXT,
    ocr_blocks TEXT NOT NULL DEFAULT '[]',
    embedding BLOB,
    error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_tag_reject (
    asset_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY(asset_id, name)
);
"""


def now_iso() -> str:
    return datetime.now().strftime(ISO)


def _like(text: str) -> str:
    """Escape a LIKE pattern using '!' as the escape character.

    Backslashes are deliberately left alone: Windows paths contain them and
    would otherwise collide with the default escape character.
    """
    return "%" + text.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


class DB:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._batch_depth = 0
        self._revision = 0
        self._counts_cache = None
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
            columns = {r[1] for r in self.conn.execute("PRAGMA table_info(asset)")}
            if "source_app" not in columns:
                self.conn.execute("ALTER TABLE asset ADD COLUMN source_app TEXT")
                self.conn.executemany("UPDATE asset SET source_app=? WHERE id=?",
                    [(source_for(r["path"]), r["id"]) for r in self.conn.execute("SELECT id,path FROM asset").fetchall()])
            if "recycle_token" not in columns:
                self.conn.execute("ALTER TABLE asset ADD COLUMN recycle_token TEXT")
            for name, definition in (("file_id", "TEXT"), ("missing", "INTEGER NOT NULL DEFAULT 0"),
                    ("source_root", "TEXT"), ("duration", "REAL NOT NULL DEFAULT 0"),
                    ("excluded", "INTEGER NOT NULL DEFAULT 0")):
                if name not in columns:
                    self.conn.execute(f"ALTER TABLE asset ADD COLUMN {name} {definition}")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_asset_identity ON asset(file_id)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_asset_source ON asset(source_app,deleted)")
            try:
                self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS ai_fts USING fts5(text, tokenize='trigram')")
                self.has_ai_fts = True
            except sqlite3.OperationalError:
                self.has_ai_fts = False
            self.conn.commit()
        self.apply_scan_exclusions()

    def scan_exclude_paths(self):
        try:
            paths = json.loads(self.get_setting('scan_exclude_paths', '[]'))
        except (TypeError, ValueError):
            return []
        if not isinstance(paths, list):
            return []
        return list({os.path.normcase(os.path.abspath(p)): os.path.abspath(p)
                     for p in paths if isinstance(p, str) and os.path.isabs(p)}.values())

    def set_scan_exclude_paths(self, paths):
        self.set_setting('scan_exclude_paths', json.dumps(paths, ensure_ascii=False))
        return self.apply_scan_exclusions()

    def apply_scan_exclusions(self):
        from sources import is_resource_path
        excluded = [os.path.normcase(os.path.abspath(p)) for p in self.scan_exclude_paths()]
        filter_resources = self.get_setting('scan_filter_resources', '1') == '1'
        changes = []
        for row in self.q('SELECT id,path,excluded FROM asset WHERE deleted=0'):
            path = os.path.normcase(os.path.abspath(row['path']))
            blocked = filter_resources and is_resource_path(path)
            for folder in excluded:
                try:
                    blocked = blocked or os.path.commonpath((path, folder)) == folder
                except ValueError:
                    pass
            if row['excluded'] != int(blocked):
                changes.append((int(blocked), row['id']))
        for offset in range(0, len(changes), 128):
            with self.batch():
                for values in changes[offset:offset+128]:
                    self.execute('UPDATE asset SET excluded=? WHERE id=?', values)
        return len(changes)

    # ---------------------------------------------------------------- helpers
    def q(self, sql: str, args: Sequence[Any] = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def q1(self, sql: str, args: Sequence[Any] = ()) -> dict | None:
        with self.lock:
            return _row(self.conn.execute(sql, args).fetchone())

    def execute(self, sql: str, args: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self._commit()
            return cur

    def _commit(self):
        if not self._batch_depth:
            self.conn.commit()
            self._revision += 1
            self._counts_cache = None

    @contextmanager
    def batch(self):
        with self.lock:
            outer = self._batch_depth == 0
            self._batch_depth += 1
            try:
                yield
            except BaseException:
                if outer:
                    self.conn.rollback()
                raise
            finally:
                self._batch_depth -= 1
            if outer:
                self._commit()

    def find_by_identity(self, file_id):
        if not file_id:
            return None
        return self.q1("SELECT * FROM asset WHERE file_id=? AND deleted=0 ORDER BY missing,id LIMIT 1", (file_id,))

    def mark_missing(self, asset_id, value=True):
        return bool(self.execute("UPDATE asset SET missing=? WHERE id=? AND deleted=0 AND missing<>?",
                                 (int(value), asset_id, int(value))).rowcount)

    def media_roots(self):
        return self.q("SELECT path,source_app FROM media_root ORDER BY length(path),path")

    def remember_media_root(self, path, source_app):
        self.execute("INSERT INTO media_root(path,source_app) VALUES (?,?) ON CONFLICT(path) "
                     "DO UPDATE SET source_app=excluded.source_app WHERE source_app<>excluded.source_app", (path, source_app))

    def remember_directory(self, file_id, path, root):
        self.execute("INSERT INTO scan_directory(file_id,path,root) VALUES (?,?,?) ON CONFLICT(file_id) "
                     "DO UPDATE SET path=excluded.path,root=excluded.root WHERE path<>excluded.path OR root<>excluded.root",
                     (file_id, path, root))

    def directory_by_identity(self, file_id):
        return self.q1("SELECT * FROM scan_directory WHERE file_id=?", (file_id,))

    def directories(self, root=None):
        return self.q("SELECT * FROM scan_directory" + (" WHERE root=?" if root else ""), (root,) if root else ())

    def reclassify_sources(self):
        from sources import source_details
        rows = self.q("SELECT id,path,root,source_app,source_root FROM asset")
        for start in range(0, len(rows), 128):
            updates = []
            for row in rows[start:start+128]:
                source, root = source_details(row["path"])
                if root and row.get('root'):
                    owner = os.path.normcase(os.path.abspath(row['root']))
                    try:
                        if os.path.commonpath((os.path.normcase(os.path.abspath(root)), owner)) != owner:
                            root = row['root']
                    except ValueError:
                        root = row['root']
                updates.append((row, source, root))
            with self.batch():
                for row, source, root in updates:
                    if (source, root) != (row["source_app"], row["source_root"]):
                        self.execute("UPDATE asset SET source_app=?,source_root=? WHERE id=?", (source, root, row["id"]))
                    if root:
                        self.remember_media_root(root, source)

    # ------------------------------------------------------------------ roots
    def root_paths(self):
        return [row['path'] for row in self.q('SELECT path FROM root ORDER BY added_at')]

    def list_roots(self) -> list[dict]:
        return self.q(
            """SELECT r.path, r.added_at, COUNT(a.id) AS count
               FROM root r
               LEFT JOIN asset a ON a.root = r.path AND a.deleted = 0 AND a.missing=0 AND a.excluded=0
               GROUP BY r.path, r.added_at
               ORDER BY r.added_at ASC"""
        )

    def add_root(self, path: str) -> None:
        self.execute("INSERT OR IGNORE INTO root(path, added_at) VALUES (?, ?)", (path, now_iso()))

    def remove_root(self, path: str) -> list[int]:
        ids = [r["id"] for r in self.q("SELECT id FROM asset WHERE root = ?", (path,))]
        with self.lock:
            self._remove_ai(ids)
            self.conn.execute(
                "DELETE FROM album_asset WHERE asset_id IN (SELECT id FROM asset WHERE root = ?)", (path,)
            )
            self.conn.execute("DELETE FROM asset_tag WHERE asset_id IN (SELECT id FROM asset WHERE root = ?)", (path,))
            self.conn.execute("DELETE FROM asset WHERE root = ?", (path,))
            self.conn.execute("DELETE FROM root WHERE path = ?", (path,))
            self._commit()
        return ids

    # ------------------------------------------------------------------ asset
    def find_by_path(self, path: str) -> dict | None:
        # Discovery and folder pickers may return different drive/folder casing on Windows.
        collation = " COLLATE NOCASE" if os.name == "nt" else ""
        return self.q1("SELECT * FROM asset WHERE path = ?" + collation, (path,))

    def get_asset(self, asset_id: int) -> dict | None:
        return self.q1("SELECT * FROM asset WHERE id = ?", (asset_id,))

    def insert_asset(self, data: dict) -> int:
        data = dict(data)
        data.setdefault("source_app", source_for(data["path"]))
        data.setdefault("missing", 0)
        data.setdefault("excluded", 0)
        data.setdefault("duration", 0)
        data.setdefault("created_at", now_iso())
        data["indexed_at"] = now_iso()
        cols = [
            "path", "root", "dir", "rel_path", "name", "ext", "size", "mtime", "hash",
            "width", "height", "taken_at", "camera", "gps_lat", "gps_lon", "media_type",
            "is_screenshot", "favorite", "deleted", "created_at", "indexed_at", "source_app",
            "file_id", "missing", "source_root", "duration", "excluded",
        ]
        cur = self.execute(
            f"INSERT INTO asset ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [data.get(c) for c in cols],
        )
        return int(cur.lastrowid)

    def update_asset(self, asset_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["indexed_at"] = now_iso()
        cols = list(fields.keys())
        sql = f"UPDATE asset SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?"
        self.execute(sql, [fields[c] for c in cols] + [asset_id])

    def set_favorite(self, ids: Iterable[int], value: bool) -> None:
        ids = list(ids)
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        self.execute(f"UPDATE asset SET favorite = ? WHERE id IN ({marks})", [1 if value else 0, *ids])

    def soft_delete(self, ids: Iterable[int]) -> None:
        ids = list(ids)
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        self.execute(
            f"UPDATE asset SET deleted = 1, deleted_at = ? WHERE id IN ({marks})",
            [now_iso(), *ids],
        )

    def restore(self, ids: Iterable[int]) -> None:
        ids = list(ids)
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        self.execute(
            f"UPDATE asset SET deleted = 0, missing=0, deleted_at = NULL, recycle_token = NULL WHERE id IN ({marks})",
            list(ids),
        )

    def purge_assets(self, ids: Iterable[int]) -> list[dict]:
        ids = list(ids)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self.q(f"SELECT * FROM asset WHERE id IN ({marks})", ids)
        with self.lock:
            self._remove_ai(ids)
            self.conn.execute(f"DELETE FROM album_asset WHERE asset_id IN ({marks})", ids)
            self.conn.execute(f"DELETE FROM asset_tag WHERE asset_id IN ({marks})", ids)
            self.conn.execute(f"DELETE FROM asset WHERE id IN ({marks})", ids)
            self._commit()
        return rows

    # --------------------------------------------------------------- querying
    def query_assets(
        self,
        *,
        view: str = "all",
        q: str = "",
        album_id: str | None = None,
        folder: str | None = None,
        type_: str | None = None,
        sort: str = "taken",
        offset: int = 0,
        limit: int = 120,
        tag: str | None = None,
        ranked_ids: list[int] | None = None,
        source_app: str | None = None,
        ids_only: bool = False,
    ) -> dict:
        where: list[str] = []
        args: list[Any] = []
        join = ""

        if view == "trash":
            where.append("a.deleted = 1")
        else:
            where.append("a.deleted = 0 AND a.missing=0 AND a.excluded=0")

        if view == "favorites":
            where.append("a.favorite = 1")
        if source_app:
            where.append("a.source_app = ?")
            args.append(source_app)

        if view == "album":
            join = "JOIN album_asset aa ON aa.asset_id = a.id"
            where.append("aa.album_id = ?")
            args.append(album_id)

        if folder:
            prefix = folder.rstrip("\\/") + os.sep
            where.append("(a.path = ? OR a.path LIKE ? ESCAPE '!')")
            args.extend([folder, _like(prefix)])

        if type_ == "image":
            where.append("a.media_type = 'image'")
        elif type_ == "video":
            where.append("a.media_type = 'video'")
        elif type_ == "gif":
            where.append("lower(a.ext) = '.gif'")
        elif type_ == "screenshot":
            where.append("a.is_screenshot = 1")

        if q:
            like = _like(q)
            where.append(
                "(a.name LIKE ? ESCAPE '!' OR a.rel_path LIKE ? ESCAPE '!' OR a.dir LIKE ? ESCAPE '!' "
                "OR EXISTS (SELECT 1 FROM ai_index ix WHERE ix.asset_id=a.id AND ix.file_size=a.size "
                "AND abs(ix.file_mtime-a.mtime)<0.000001 AND lower(COALESCE(ix.ocr_override,ix.ocr_text)) LIKE ? ESCAPE '!') "
                "OR EXISTS (SELECT 1 FROM asset_tag at JOIN tag t ON t.id=at.tag_id "
                "WHERE at.asset_id=a.id AND t.name LIKE ? ESCAPE '!'))"
            )
            args.extend([like, like, like, like, like])

        if tag:
            where.append("EXISTS (SELECT 1 FROM asset_tag at JOIN tag t ON t.id=at.tag_id WHERE at.asset_id=a.id AND t.name=?)")
            args.append(tag)
        if ranked_ids is not None:
            if not ranked_ids:
                return {"items": [], "total": 0, "has_more": False, "offset": offset, "limit": limit}
            marks = ','.join('?' for _ in ranked_ids)
            where.append(f"a.id IN ({marks})")
            args.extend(ranked_ids)

        order = {
            "taken": "a.taken_at DESC, a.id DESC",
            "added": "a.indexed_at DESC, a.id DESC",
            "name": "a.name COLLATE NOCASE ASC",
            "size": "a.size DESC",
        }.get(sort, "a.taken_at DESC, a.id DESC")
        if view == "trash":
            order = "a.deleted_at DESC, a.id DESC"
        if view == "recent":
            order = "a.indexed_at DESC, a.id DESC"
        rank_args = []
        if ranked_ids is not None:
            order = "instr(?, ',' || a.id || ',')"
            rank_args = [',' + ','.join(str(int(i)) for i in ranked_ids) + ',']

        clause = " AND ".join(where)
        total = self.q1(f"SELECT COUNT(*) AS c FROM asset a {join} WHERE {clause}", args)
        columns = "a.id" if ids_only else """a.id, a.name, a.ext, a.size, a.width, a.height, a.taken_at,
                       a.media_type, a.is_screenshot, a.favorite, a.dir, a.path,
                       a.mtime, a.indexed_at, a.deleted, a.source_app, a.source_root, a.duration"""
        rows = self.q(
            f"""SELECT {columns}
                FROM asset a {join}
                WHERE {clause}
                ORDER BY {order}
                LIMIT ? OFFSET ?""",
            [*args, *rank_args, limit, offset],
        )
        return {
            "items": rows,
            "total": int(total["c"]) if total else 0,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < (int(total["c"]) if total else 0),
        }

    def counts(self) -> dict:
        with self.lock:
            stamp = self.conn.total_changes
            if self._counts_cache and self._counts_cache[0] == stamp:
                return dict(self._counts_cache[1])
            result = self._count_values()
            self._counts_cache = (stamp, result)
            return dict(result)

    def _count_values(self) -> dict:
        row = self.q1(
            """SELECT
                 COUNT(*) AS total,
                 COALESCE(SUM(CASE WHEN deleted = 0 AND missing=0 AND excluded=0 THEN 1 ELSE 0 END), 0) AS active,
                 COALESCE(SUM(CASE WHEN deleted = 0 AND missing=0 AND excluded=0 AND favorite = 1 THEN 1 ELSE 0 END), 0) AS favorites,
                 COALESCE(SUM(CASE WHEN deleted = 1 THEN 1 ELSE 0 END), 0) AS trash,
                 COALESCE(SUM(CASE WHEN deleted = 0 AND missing=0 AND excluded=0 THEN size ELSE 0 END), 0) AS bytes,
                 COALESCE(SUM(CASE WHEN deleted = 0 AND missing=0 AND excluded=0 AND media_type = 'video' THEN 1 ELSE 0 END), 0) AS videos,
                 COALESCE(SUM(CASE WHEN deleted = 0 AND missing=0 AND excluded=0 AND is_screenshot = 1 THEN 1 ELSE 0 END), 0) AS screenshots,
                 COALESCE(SUM(CASE WHEN deleted=0 AND missing=0 AND excluded=0 AND lower(ext)='.gif' THEN 1 ELSE 0 END),0) AS gifs
               FROM asset"""
        )
        return row or {
            "total": 0, "active": 0, "favorites": 0, "trash": 0,
            "bytes": 0, "videos": 0, "screenshots": 0,
        }

    def source_stats(self):
        return self.q("SELECT COALESCE(source_app,'本地照片') AS name, COUNT(*) AS count "
                      "FROM asset WHERE deleted=0 AND missing=0 AND excluded=0 GROUP BY source_app ORDER BY count DESC, name")

    def folder_stats(self, limit: int = 200) -> list[dict]:
        return self.q(
            """SELECT dir, COUNT(*) AS count, SUM(size) AS bytes
               FROM asset WHERE deleted = 0 AND missing=0 AND excluded=0 AND dir IS NOT NULL
               GROUP BY dir ORDER BY count DESC LIMIT ?""",
            (limit,),
        )

    # ---------------------------------------------------------------- albums
    def albums(self) -> list[dict]:
        return self.q(
            """SELECT a.id, a.name, a.created_at, a.updated_at,
                      (SELECT COUNT(*) FROM album_asset x
                         JOIN asset z ON z.id = x.asset_id AND z.deleted = 0 AND z.missing=0 AND z.excluded=0
                        WHERE x.album_id = a.id) AS count,
                      (SELECT x.asset_id FROM album_asset x
                         JOIN asset z ON z.id = x.asset_id AND z.deleted = 0 AND z.missing=0 AND z.excluded=0
                        WHERE x.album_id = a.id ORDER BY x.added_at ASC LIMIT 1) AS cover
               FROM album a ORDER BY a.created_at ASC"""
        )

    def get_album(self, album_id: str) -> dict | None:
        return self.q1("SELECT * FROM album WHERE id = ?", (album_id,))

    def create_album(self, name: str) -> dict:
        album_id = uuid.uuid4().hex[:12]
        ts = now_iso()
        self.execute(
            "INSERT INTO album(id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (album_id, name, ts, ts),
        )
        return {"id": album_id, "name": name, "created_at": ts, "updated_at": ts, "count": 0, "cover": None}

    def rename_album(self, album_id: str, name: str) -> None:
        self.execute("UPDATE album SET name = ?, updated_at = ? WHERE id = ?", (name, now_iso(), album_id))

    def delete_album(self, album_id: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM album_asset WHERE album_id = ?", (album_id,))
            self.conn.execute("DELETE FROM album WHERE id = ?", (album_id,))
            self._commit()

    def add_to_album(self, album_id: str, ids: Iterable[int]) -> None:
        ids = list(ids)
        if not ids:
            return
        ts = now_iso()
        with self.lock:
            self.conn.executemany(
                "INSERT OR IGNORE INTO album_asset(album_id, asset_id, added_at) VALUES (?, ?, ?)",
                [(album_id, i, ts) for i in ids],
            )
            self.conn.execute("UPDATE album SET updated_at = ? WHERE id = ?", (ts, album_id))
            self._commit()

    def remove_from_album(self, album_id: str, ids: Iterable[int]) -> None:
        ids = list(ids)
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        self.execute(
            f"DELETE FROM album_asset WHERE album_id = ? AND asset_id IN ({marks})",
            [album_id, *ids],
        )
        self.execute("UPDATE album SET updated_at = ? WHERE id = ?", (now_iso(), album_id))

    def albums_for_asset(self, asset_id: int) -> list[dict]:
        return self.q(
            """SELECT a.id, a.name FROM album a
               JOIN album_asset x ON x.album_id = a.id
               WHERE x.asset_id = ? ORDER BY a.created_at""",
            (asset_id,),
        )

    # ---------------------------------------------------------------- tags
    def tags_for(self, asset_id: int) -> list[str]:
        rows = self.q(
            "SELECT t.name FROM tag t JOIN asset_tag x ON x.tag_id = t.id WHERE x.asset_id = ?",
            (asset_id,),
        )
        return [r["name"] for r in rows]

    # ------------------------------------------------------------ duplicates
    def duplicate_groups(self) -> list[dict]:
        groups: list[dict] = []
        heads = self.q(
            """SELECT hash, COUNT(*) AS count, SUM(size) AS bytes
               FROM asset WHERE deleted = 0 AND missing=0 AND excluded=0 AND hash IS NOT NULL AND hash <> ''
               GROUP BY hash HAVING count > 1 ORDER BY bytes DESC LIMIT 200"""
        )
        for head in heads:
            items = self.q(
                """SELECT id, name, size, taken_at, width, height, dir, path, media_type
                   FROM asset WHERE deleted = 0 AND missing=0 AND excluded=0 AND hash = ? ORDER BY taken_at ASC, id ASC""",
                (head["hash"],),
            )
            groups.append({"hash": head["hash"], "count": head["count"], "bytes": head["bytes"] or 0, "items": items})
        return groups

    # ------------------------------------------------------------- settings
    def _remove_ai(self, ids: list[int]) -> None:
        """Called inside the caller's transaction when assets are actually removed."""
        for asset_id in ids:
            self.conn.execute("DELETE FROM ai_index WHERE asset_id=?", (asset_id,))
            self.conn.execute("DELETE FROM ai_tag_reject WHERE asset_id=?", (asset_id,))
            if self.has_ai_fts:
                self.conn.execute("DELETE FROM ai_fts WHERE rowid=?", (asset_id,))

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.q1("SELECT value FROM setting WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO setting(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def close(self) -> None:
        with self.lock:
            self.conn.close()
