"""One background inference worker, shared by indexing, OCR and semantic queries."""
from __future__ import annotations

from collections import deque
import itertools
import json
import os
from pathlib import Path
import queue
import threading
import time

from ai_store import AIStore


class AIIndexer:
    def __init__(self, db, model_dir):
        self.db = db
        self.store = AIStore(db)
        self.model_dir = Path(model_dir)
        self.events = queue.Queue()
        self.commands = queue.Queue()
        self._counter = itertools.count(1)
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._engine = None
        self._vector_cache = None
        self._jobs = deque()
        self._attempted = set()
        self._refresh_pending = False
        self._refresh_queued = False
        self._retry_requested = False
        self._last_refresh = 0.0
        self._latest_search = None
        self._indexing = False
        self._status = {"phase": "idle", "running": False, "done": 0, "total": 0,
                        "errors": 0, "current": "", "message": "等待开始识别"}
        self.manifest = None
        try:
            from ai_engine import model_manifest
            self.manifest = model_manifest(self.model_dir)
            self.error = ""
        except (ImportError, OSError, KeyError, ValueError) as exc:
            self.error = str(exc)
            self._status.update(phase="unavailable", message="模型未就绪，请检查完整模型包")

    @property
    def ready(self):
        return self.manifest is not None

    @property
    def version(self):
        return self.manifest["version"] if self.manifest else "unavailable"

    def snapshot(self):
        with self._lock:
            return dict(self._status)

    def _set(self, **values):
        with self._lock:
            self._status.update(values)

    def _wake(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="photonest-ai", daemon=True)
            self._thread.start()

    def start(self, *, retry=False):
        if not self.ready or self._stop.is_set():
            return False
        was_paused = self._pause.is_set()
        self._pause.clear()
        with self._lock:
            busy = self._status["running"]
            self._refresh_pending = True
            self._retry_requested |= bool(retry or was_paused)
            queued = self._refresh_queued
            self._refresh_queued = True
            if not busy:
                self._status.update(running=True, phase="loading", message="正在准备本地识别…")
        # Automatic scan notifications must not generate writes (and invalidate
        # the semantic matrix) when these settings already have the right value.
        if self.db.get_setting("ai_enabled", "0") != "1":
            self.db.set_setting("ai_enabled", "1")
        if self.db.get_setting("ai_paused", "0") != "0":
            self.db.set_setting("ai_paused", "0")
        if not queued:
            self.commands.put(("refresh",))
        self._wake()
        return not busy

    def pause(self):
        self._pause.set()
        self.db.set_setting("ai_paused", "1")
        self._set(phase="paused", running=False, message="已暂停，可继续识别")

    def request_search(self, text, mode="all"):
        request = next(self._counter)
        self._latest_search = request
        self.commands.put(("search", request, text, mode))
        self._wake()
        return request

    def request_ocr(self, asset_id, crop=None, force=False):
        request = next(self._counter)
        self.commands.put(("ocr", request, asset_id, crop, force))
        self._wake()
        return request

    def close(self):
        self._stop.set()
        self.commands.put(("stop",))
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)

    def _load(self):
        if not self.ready:
            raise RuntimeError("模型未就绪，请检查程序旁的 models 文件夹")
        if self._engine is None:
            from ai_engine import Engine
            self._engine = Engine(self.model_dir)
        return self._engine

    def _run(self):
        while not self._stop.is_set():
            try:
                task = (self.commands.get_nowait() if self._indexing and not self._pause.is_set()
                        else self.commands.get(timeout=0.3))
            except queue.Empty:
                task = None
            if self._stop.is_set():
                break
            if task:
                if task[0] == "stop":
                    break
                self._handle(task)
                continue
            if self._refresh_pending and not self._pause.is_set():
                self._handle(("refresh",))
            if self._indexing and not self._pause.is_set():
                if not self._jobs:
                    with self._lock:
                        # A producer may notify after the final pending query.
                        # Leave that request for the worker instead of losing it
                        # in the transition from indexing to idle.
                        if self._refresh_pending:
                            continue
                        self._indexing = False
                        errors = self._status['errors']
                        self._status.update(running=False, phase="done", current="", message=(
                            f"识别完成 · {errors} 个失败，可重试" if errors else "识别完成"))
                    self.events.put(("finished", None))
                    continue
                asset = self._jobs.popleft()
                self._attempted.add(self._fingerprint(asset))
                self._set(current=asset["name"], phase="indexing", running=True,
                          message="正在识别照片…")
                try:
                    self._process(asset)
                except Exception as exc:
                    if not self._stop.is_set():
                        self.store.fail(asset, self.version, exc)
                        self._set(errors=self.snapshot()["errors"] + 1)
                if not self._stop.is_set():
                    with self._lock:
                        self._status['done'] += 1
                        if self._pause.is_set():
                            self._status.update(phase='paused', running=False, current='', message='已暂停，可继续识别')
                    self.events.put(("indexed", asset["id"]))

    def _refresh_jobs(self):
        with self._lock:
            if not self._refresh_pending or self._pause.is_set() or self._stop.is_set():
                return
            retry = self._retry_requested
            if (self._indexing and self._jobs and not retry
                    and time.monotonic()-self._last_refresh < 1):
                return  # Coalesce bursts of scan commits without delaying the current inference.
            self._refresh_pending = False
            self._refresh_queued = False
            self._retry_requested = False
            self._last_refresh = time.monotonic()
        reset = not self._indexing or retry
        if retry:
            self._attempted.clear()
        pending = self.store.pending(self.version)
        fingerprints = {asset['id']: self._fingerprint(asset) for asset in pending}
        self._attempted.intersection_update(fingerprints.values())
        if reset:
            self._jobs.clear()
        else:
            self._jobs = deque(asset for asset in self._jobs
                               if fingerprints.get(asset['id']) == self._fingerprint(asset))
        queued = {self._fingerprint(asset) for asset in self._jobs}
        fresh = [asset for asset in pending if self._fingerprint(asset) not in self._attempted
                 and self._fingerprint(asset) not in queued]
        # Newly discovered/changed images should not wait behind the old-library backlog.
        self._jobs.extendleft(reversed(fresh))
        self._indexing = True
        with self._lock:
            if reset:
                self._status.update(total=len(self._jobs), done=0, errors=0)
            else:
                self._status['total'] = self._status['done'] + len(self._jobs)
            self._status.update(phase="paused" if self._pause.is_set() else "indexing",
                                running=not self._pause.is_set())

    def _handle(self, task):
        try:
            if task[0] in ("start", "refresh"):
                self._refresh_jobs()
            elif task[0] == "search":
                _, request, text, mode = task
                if request != self._latest_search:
                    return
                ids = self._search(text, mode)
                if not self._stop.is_set():
                    self.events.put(("search", {"request": request, "q": text, "ids": ids, "error": ""}))
            elif task[0] == "ocr":
                _, request, asset_id, crop, force = task
                asset = self.db.get_asset(asset_id)
                if not asset:
                    raise ValueError("照片已从照片库移除")
                cached = self.store.result(asset_id) if crop is None and not force else None
                if cached and cached["ocr_done"]:
                    text, blocks = cached["text"], cached["blocks"]
                else:
                    from ai_engine import read_image
                    before = os.stat(asset["path"])
                    text, blocks = self._load().ocr(read_image(asset["path"], crop))
                    after = os.stat(asset["path"])
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise ValueError("照片在识别过程中发生变化，请重新扫描后再试")
                    if crop is None and not self._stop.is_set():
                        self.store.save(asset, self.version, text, blocks)
                if not self._stop.is_set():
                    self.events.put(("ocr", {"request": request, "asset_id": asset_id,
                                             "text": text, "blocks": blocks, "error": ""}))
        except Exception as exc:
            if self._stop.is_set():
                return
            if task[0] in ("start", "refresh"):
                self._indexing = False
                self._set(running=False, phase="error", message=f"识别未启动：{exc}")
                self.events.put(("finished", None))
            elif task[0] == "search":
                self.events.put(("search", {"request": task[1], "q": task[2],
                                             "ids": self.store.text_matches(task[2]), "error": str(exc)}))
            elif task[0] == "ocr":
                self.events.put(("ocr", {"request": task[1], "asset_id": task[2],
                                          "text": "", "blocks": [], "error": str(exc)}))

    def _process(self, asset):
        from ai_engine import read_image
        current = self.db.get_asset(asset['id'])
        if (not current or current.get('deleted') or current.get('missing') or current.get('excluded')
                or current.get('media_type') != 'image'):
            return
        asset = current
        self._attempted.add(self._fingerprint(asset))
        if self._stop.is_set() or self._pause.is_set():
            return
        before = os.stat(asset["path"])
        if before.st_size != asset["size"] or abs(before.st_mtime - asset["mtime"]) >= .000001:
            raise ValueError("文件已变化，请重新扫描照片库")
        cached = self.store.content_match(asset, self.version)
        if cached:
            after = os.stat(asset['path'])
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('照片在复用识别结果时发生变化，请重新扫描')
            if not self._stop.is_set() and not self._pause.is_set():
                self.store.save(asset, self.version, cached['ocr_text'], cached['blocks'],
                                cached['embedding'], cached['tags'])
            return
        if self._engine is None:
            self._set(phase='loading', message='正在加载本地识别模型…')
        engine = self._load()
        if self._stop.is_set() or self._pause.is_set():
            return
        self._set(phase='indexing', message='正在识别照片…')
        image = read_image(asset["path"])
        old = self.store.result(asset["id"])
        if old and old["ocr_done"]:
            text, blocks = old["ocr_text"], old["blocks"]
        else:
            text, blocks = engine.ocr(image)
        if self._stop.is_set() or self._pause.is_set():
            return
        vector = engine.encode_image(image)
        tags = engine.classify(vector)
        after = os.stat(asset["path"])
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("照片在识别过程中发生变化，请重新扫描")
        current = self.db.get_asset(asset['id'])
        if (not self._stop.is_set() and not self._pause.is_set() and current
                and not current.get('deleted') and not current.get('missing') and not current.get('excluded')
                and self._fingerprint(current) == self._fingerprint(asset)):
            self.store.save(asset, self.version, text, blocks, vector.astype("<f4").tobytes(), tags)

    @staticmethod
    def _fingerprint(asset):
        return asset["id"], asset["size"], asset["mtime"]

    def _search(self, text, mode):
        import numpy as np
        lexical = self.store.text_matches(text) if mode != "画面内容" else []
        if mode == "文字匹配":
            return lexical
        engine = self._load()
        query = engine.encode_text([text])[0]
        stamp = (self.version, self.db.conn.total_changes)
        if self._vector_cache is None or self._vector_cache[0] != stamp:
            vectors, ids = [], []
            for row in self.store.vectors(self.version):
                vector = np.frombuffer(row["embedding"], dtype="<f4")
                if len(vector) == len(query) and np.isfinite(vector).all():
                    vectors.append(vector)
                    ids.append(row["id"])
            matrix = np.vstack(vectors) if vectors else None
            self._vector_cache = (stamp, matrix, ids)
        _, matrix, ids = self._vector_cache
        if matrix is None:
            return lexical
        scores = matrix @ query
        order = np.argsort(-scores)[:200]
        semantic = [ids[i] for i in order if scores[i] >= 0.18]
        if mode == "画面内容":
            return semantic
        # Reciprocal-rank fusion keeps OCR/filename hits comparable with cosine-ranked matches.
        combined = {}
        for ranking, weight in ((lexical, 1.4), (semantic, 1.0)):
            for rank, asset_id in enumerate(ranking):
                combined[asset_id] = combined.get(asset_id, 0.0) + weight / (60 + rank + 1)
        return sorted(combined, key=combined.get, reverse=True)[:500]
