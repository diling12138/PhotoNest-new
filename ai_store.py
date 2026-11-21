"""Persistence for local OCR, image embeddings and user tag corrections."""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime


def searchable(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).lower())


class AIStore:
    def __init__(self, db):
        self.db = db

    def pending(self, version):
        return self.db.q(
            """SELECT a.* FROM asset a LEFT JOIN ai_index ix ON ix.asset_id=a.id
               WHERE a.deleted=0 AND a.missing=0 AND a.excluded=0 AND a.media_type='image' AND
               (ix.asset_id IS NULL OR ix.model_version<>? OR ix.embedding IS NULL OR ix.error<>''
                OR ix.file_size<>a.size OR abs(ix.file_mtime-a.mtime)>=0.000001)
               ORDER BY a.id""", (version,))

    def result(self, asset_id):
        row = self.db.q1(
            """SELECT ix.* FROM ai_index ix JOIN asset a ON a.id=ix.asset_id
               WHERE a.id=? AND ix.file_size=a.size AND abs(ix.file_mtime-a.mtime)<0.000001""",
            (asset_id,))
        if row:
            row["text"] = row["ocr_override"] if row["ocr_override"] is not None else row["ocr_text"]
            row["blocks"] = json.loads(row["ocr_blocks"])
        return row

    def content_match(self, asset, version):
        """Reuse successful uncorrected OCR/features only for complete content hashes."""
        from scanner import HASH_LIMIT
        digest, size = asset.get('hash'), asset.get('size', 0)
        if (not isinstance(digest, str) or len(digest) != 32
                or any(char not in '0123456789abcdefABCDEF' for char in digest)
                or not 0 < size <= HASH_LIMIT):
            return None  # Larger files use sampled hashes, which cannot prove identical content.
        row = self.db.q1("""SELECT a.id AS source_id,ix.ocr_text,ix.ocr_blocks,ix.embedding
            FROM asset a JOIN ai_index ix ON ix.asset_id=a.id
            WHERE a.id<>? AND a.hash=? AND a.size=? AND a.deleted=0 AND a.missing=0 AND a.excluded=0
              AND a.media_type='image' AND ix.model_version=? AND ix.ocr_done=1
              AND ix.embedding IS NOT NULL AND length(ix.embedding)>0 AND ix.error=''
              AND ix.file_size=a.size AND abs(ix.file_mtime-a.mtime)<0.000001
              AND NOT EXISTS (SELECT 1 FROM ai_tag_reject r WHERE r.asset_id=a.id)
              AND (EXISTS (SELECT 1 FROM asset_tag at WHERE at.asset_id=a.id AND at.source='ai')
                   OR NOT EXISTS (SELECT 1 FROM asset_tag at WHERE at.asset_id=a.id AND at.source='user'))
            ORDER BY a.id LIMIT 1""", (asset['id'], digest, size, version))
        if not row:
            return None
        try:
            row['blocks'] = json.loads(row.pop('ocr_blocks'))
        except (TypeError, ValueError):
            return None
        row['tags'] = [tag['name'] for tag in self.db.q("""SELECT t.name FROM asset_tag at
            JOIN tag t ON t.id=at.tag_id WHERE at.asset_id=? AND at.source='ai' ORDER BY t.id""",
            (row['source_id'],))]
        return row

    def save(self, asset, version, text, blocks, embedding=None, tags=(), error=""):
        db = self.db
        asset_id = asset["id"]
        update_tags = embedding is not None and not error
        with db.lock, db.conn:
            current = db.conn.execute("SELECT size,mtime FROM asset WHERE id=? AND deleted=0 "
                                      "AND missing=0 AND excluded=0", (asset_id,)).fetchone()
            if current is None or current[0] != asset["size"] or abs(current[1] - asset["mtime"]) >= 0.000001:
                return False
            old = self.result(asset_id)
            override = old["ocr_override"] if old else None
            if embedding is None and old and old["model_version"] == version:
                embedding = old["embedding"]
            db.conn.execute(
                """INSERT OR REPLACE INTO ai_index
                (asset_id,file_size,file_mtime,model_version,ocr_done,ocr_text,ocr_override,
                 ocr_blocks,embedding,error,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (asset_id, asset["size"], asset["mtime"], version, 1, text, override,
                 json.dumps(blocks, ensure_ascii=False), embedding, error,
                 datetime.now().isoformat(timespec="seconds")))
            if update_tags:
                db.conn.execute("DELETE FROM asset_tag WHERE asset_id=? AND source='ai'", (asset_id,))
                for name in tags:
                    if db.conn.execute("SELECT 1 FROM ai_tag_reject WHERE asset_id=? AND name=?", (asset_id, name)).fetchone():
                        continue
                    db.conn.execute("INSERT OR IGNORE INTO tag(name) VALUES (?)", (name,))
                    db.conn.execute("""INSERT OR IGNORE INTO asset_tag(asset_id,tag_id,source)
                                    SELECT ?,id,'ai' FROM tag WHERE name=?""", (asset_id, name))
            self._fts(asset_id, override if override is not None else text)
            db.conn.commit()
        return True

    def fail(self, asset, version, error):
        old = self.result(asset["id"])
        self.save(asset, version, old["ocr_text"] if old else "", old["blocks"] if old else [],
                  error=str(error)[:400])
        if not old:
            self.db.execute("UPDATE ai_index SET ocr_done=0 WHERE asset_id=?", (asset["id"],))

    def _fts(self, asset_id, text):
        if self.db.has_ai_fts:
            self.db.conn.execute("DELETE FROM ai_fts WHERE rowid=?", (asset_id,))
            self.db.conn.execute("INSERT INTO ai_fts(rowid,text) VALUES (?,?)", (asset_id, searchable(text)))

    def correct_text(self, asset_id, text):
        with self.db.lock, self.db.conn:
            self.db.conn.execute("UPDATE ai_index SET ocr_override=? WHERE asset_id=?", (text, asset_id))
            self._fts(asset_id, text)
            self.db.conn.commit()

    def set_tags(self, asset_id, names):
        names = list(dict.fromkeys(n.strip()[:40] for n in names if n.strip()))[:30]
        old = set(self.db.tags_for(asset_id))
        with self.db.lock, self.db.conn:
            for removed in old - set(names):
                self.db.conn.execute("INSERT OR IGNORE INTO ai_tag_reject VALUES (?,?)", (asset_id, removed))
            self.db.conn.execute("DELETE FROM asset_tag WHERE asset_id=?", (asset_id,))
            for name in names:
                self.db.conn.execute("DELETE FROM ai_tag_reject WHERE asset_id=? AND name=?", (asset_id, name))
                self.db.conn.execute("INSERT OR IGNORE INTO tag(name) VALUES (?)", (name,))
                self.db.conn.execute("INSERT INTO asset_tag SELECT ?,id,'user' FROM tag WHERE name=?", (asset_id, name))
            self.db.conn.commit()

    def tag_counts(self):
        return self.db.q("""SELECT t.name,COUNT(*) AS count FROM tag t JOIN asset_tag at ON at.tag_id=t.id
                             JOIN asset a ON a.id=at.asset_id WHERE a.deleted=0 AND a.missing=0 AND a.excluded=0
                             GROUP BY t.id ORDER BY count DESC,t.name LIMIT 40""")

    def stats(self, version):
        return self.db.q1("""SELECT COUNT(*) AS total,
            COALESCE(SUM(CASE WHEN ix.file_size=a.size AND abs(ix.file_mtime-a.mtime)<0.000001
              AND ix.model_version=? AND ix.embedding IS NOT NULL AND ix.error='' THEN 1 ELSE 0 END),0) AS done,
            COALESCE(SUM(CASE WHEN ix.error<>'' AND ix.file_size=a.size
              AND abs(ix.file_mtime-a.mtime)<0.000001 THEN 1 ELSE 0 END),0) AS errors
            FROM asset a LEFT JOIN ai_index ix ON ix.asset_id=a.id
            WHERE a.deleted=0 AND a.missing=0 AND a.excluded=0 AND a.media_type='image'""", (version,))

    def vectors(self, version):
        return self.db.q("""SELECT a.id,ix.embedding FROM ai_index ix JOIN asset a ON a.id=ix.asset_id
            WHERE a.deleted=0 AND a.missing=0 AND a.excluded=0 AND ix.model_version=? AND ix.embedding IS NOT NULL
            AND ix.file_size=a.size AND abs(ix.file_mtime-a.mtime)<0.000001""", (version,))

    def text_matches(self, query, limit=500):
        # Trigram indexes handle Chinese phrases; one/two-character queries use an exact substring fallback.
        compact = searchable(query)
        ocr = []
        if compact and self.db.has_ai_fts and len(compact) >= 3:
            phrase = '"' + compact.replace('"', '""') + '"'
            ocr = self.db.q("""SELECT a.id FROM ai_fts f JOIN ai_index ix ON ix.asset_id=f.rowid
                JOIN asset a ON a.id=f.rowid WHERE ai_fts MATCH ? AND a.deleted=0 AND a.missing=0 AND a.excluded=0
                AND ix.file_size=a.size AND abs(ix.file_mtime-a.mtime)<0.000001 LIMIT ?""", (phrase, limit))
        elif compact and self.db.has_ai_fts:
            ocr = self.db.q("""SELECT a.id FROM ai_fts f JOIN ai_index ix ON ix.asset_id=f.rowid
                JOIN asset a ON a.id=f.rowid
                WHERE a.deleted=0 AND a.missing=0 AND a.excluded=0 AND ix.file_size=a.size AND abs(ix.file_mtime-a.mtime)<0.000001
                AND instr(f.text,?)>0
                LIMIT ?""", (compact, limit))
        elif compact:
            rows = self.db.q("""SELECT a.id,COALESCE(ix.ocr_override,ix.ocr_text) AS text
                FROM ai_index ix JOIN asset a ON a.id=ix.asset_id
                WHERE a.deleted=0 AND a.missing=0 AND a.excluded=0 AND ix.file_size=a.size AND abs(ix.file_mtime-a.mtime)<0.000001""")
            ocr = [r for r in rows if compact in searchable(r["text"])][:limit]
        text = self.db.query_assets(q=query, limit=limit)["items"]
        return list(dict.fromkeys([r["id"] for r in text] + [r["id"] for r in ocr]))[:limit]

    def export_text(self):
        return self.db.q("""SELECT a.name,a.path,COALESCE(ix.ocr_override,ix.ocr_text) AS text
            FROM ai_index ix JOIN asset a ON a.id=ix.asset_id WHERE a.deleted=0 AND a.missing=0 AND a.excluded=0 AND ix.ocr_done=1
            AND ix.file_size=a.size AND abs(ix.file_mtime-a.mtime)<0.000001 ORDER BY a.name""")
