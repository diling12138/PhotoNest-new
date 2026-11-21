"""Offline integration checks; all library mutations use temporary directories."""
from pathlib import Path
import tempfile
import time
import threading
import hashlib
import queue
from collections import deque
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ai_store import AIStore
from ai_index import AIIndexer
from db import DB


def add_image(db, folder, name="图片.jpg"):
    path = Path(folder) / name
    Image.new("RGB", (300, 200), "white").save(path)
    info = path.stat()
    asset_id = db.insert_asset(dict(path=str(path), root=str(folder), dir=str(folder),
        rel_path=name, name=name, ext=path.suffix, size=info.st_size, mtime=info.st_mtime,
        width=300, height=200, media_type="image", deleted=0, favorite=0, is_screenshot=0))
    return db.get_asset(asset_id)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="photonest-ai-")
        self.db = DB(str(Path(self.folder.name) / "library.db"))
        self.store = AIStore(self.db)
        self.asset = add_image(self.db, self.folder.name)
        self.vector = np.array([1, 0, 0], dtype="<f4").tobytes()

    def tearDown(self):
        self.db.close()
        self.folder.cleanup()

    def save(self, text="会 议\n通知 ＡＢＣ１２３"):
        self.assertTrue(self.store.save(self.asset, "v1", text, [], self.vector, ["文档", "票据"]))

    def hash_asset(self, asset):
        digest = hashlib.blake2b(Path(asset['path']).read_bytes(), digest_size=16).hexdigest()
        self.db.update_asset(asset['id'], hash=digest)
        return self.db.get_asset(asset['id'])

    def test_text_search_corrections_and_freshness(self):
        self.save()
        for term in ("会议", "会议通知", "abc123", "通知"):
            self.assertIn(self.asset["id"], self.store.text_matches(term))
        self.assertEqual(self.store.text_matches('" OR * %_'), [])
        self.store.correct_text(self.asset["id"], "手工修正 项目报销")
        self.save("新的 OCR 结果")
        self.assertEqual(self.store.result(self.asset["id"])["text"], "手工修正 项目报销")
        self.assertIn(self.asset["id"], self.store.text_matches("项目报销"))
        self.assertEqual(self.store.pending("v1"), [])
        self.db.update_asset(self.asset["id"], size=self.asset["size"] + 1)
        self.assertIsNone(self.store.result(self.asset["id"]))
        self.assertEqual(self.store.text_matches("项目报销"), [])
        self.assertEqual(len(self.store.pending("v1")), 1)
        self.assertFalse(self.store.save(self.asset, "v1", "过期结果", [], self.vector))

    def test_manual_tags_ocr_refresh_and_retry(self):
        self.save()
        self.store.save(self.asset, "v1", "重新提取文字", [])
        self.assertEqual(set(self.db.tags_for(self.asset["id"])), {"文档", "票据"})
        self.store.set_tags(self.asset["id"], ["文档", "工作"])
        self.save()
        self.assertEqual(set(self.db.tags_for(self.asset["id"])), {"文档", "工作"})
        self.store.fail(self.asset, "v1", "重试错误")
        self.assertEqual(len(self.store.pending("v1")), 1)
        self.assertEqual(self.store.stats("v1")["errors"], 1)
        self.save()
        self.assertEqual(self.store.stats("v1")["done"], 1)

    def test_relevance_pagination_trash_and_purge(self):
        second = add_image(self.db, self.folder.name, "第二张.jpg")
        third = add_image(self.db, self.folder.name, "第三张.jpg")
        ids = [second["id"], self.asset["id"], third["id"]]
        self.assertEqual([r["id"] for r in self.db.query_assets(ranked_ids=ids, limit=2)["items"]], ids[:2])
        self.assertEqual([r["id"] for r in self.db.query_assets(ranked_ids=ids, limit=2, offset=2)["items"]], ids[2:])
        self.save()
        self.db.soft_delete([self.asset["id"]])
        self.assertEqual(self.store.text_matches("会议"), [])
        self.assertNotIn(self.asset["id"], [r["id"] for r in self.db.query_assets(ranked_ids=ids)["items"]])
        self.db.purge_assets([self.asset["id"]])
        self.assertEqual(self.db.q("SELECT * FROM ai_index"), [])
        self.assertEqual(self.db.q("SELECT * FROM ai_tag_reject"), [])
        if self.db.has_ai_fts:
            self.assertEqual(self.db.q("SELECT * FROM ai_fts"), [])

    def test_semantic_vectors_are_reused_until_database_changes(self):
        fake_engine = SimpleNamespace(encode_text=lambda texts: np.array([[1, 0, 0]], dtype=np.float32))
        self.save()
        with patch('ai_engine.model_manifest', return_value={'version': 'v1'}), \
                patch('ai_engine.Engine', return_value=fake_engine):
            worker = AIIndexer(self.db, self.folder.name)
            try:
                with patch.object(worker.store, 'vectors', wraps=worker.store.vectors) as read_vectors:
                    self.assertEqual(worker._search('会议', '画面内容'), [self.asset['id']])
                    self.assertEqual(worker._search('文档', '画面内容'), [self.asset['id']])
                    self.assertEqual(read_vectors.call_count, 1, 'Unchanged searches should reuse the library matrix')
                    replacement = np.array([0, 1, 0], dtype='<f4').tobytes()
                    self.assertTrue(self.store.save(self.asset, 'v1', '新的识别结果', [], replacement))
                    self.assertEqual(worker._search('会议', '画面内容'), [], 'Changed embeddings must replace stale scores')
                    self.assertEqual(read_vectors.call_count, 2)
            finally:
                worker.close()

    def test_duplicate_content_reuses_base_ocr_and_ai_tags_without_copying_user_edits(self):
        self.asset = self.hash_asset(self.asset)
        self.save('原始 OCR')
        self.store.correct_text(self.asset['id'], '源照片的人工纠正')
        self.db.execute("INSERT INTO tag(name) VALUES ('源手工标签')")
        self.db.execute("INSERT INTO asset_tag(asset_id,tag_id,source) "
                        "SELECT ?,id,'user' FROM tag WHERE name='源手工标签'", (self.asset['id'],))
        target = self.hash_asset(add_image(self.db, self.folder.name, '重复图片.jpg'))
        self.store.save(target, 'v1', '目标原 OCR', [])
        self.store.correct_text(target['id'], '目标自己的纠正')
        self.store.set_tags(target['id'], ['目标手工标签', '文档'])
        with patch('ai_engine.model_manifest', return_value={'version': 'v1'}):
            worker = AIIndexer(self.db, self.folder.name)
        try:
            with patch.object(worker, '_load', side_effect=AssertionError('Duplicate must not load models')), \
                    patch('ai_engine.read_image', side_effect=AssertionError('Duplicate must not decode an image')):
                worker._process(target)
            result = self.store.result(target['id'])
            self.assertEqual(result['ocr_text'], '原始 OCR')
            self.assertEqual(result['text'], '目标自己的纠正')
            self.assertEqual(result['embedding'], self.vector)
            self.assertEqual(set(self.db.tags_for(target['id'])), {'文档', '票据', '目标手工标签'})
            provenance = self.db.q1("SELECT at.source FROM asset_tag at JOIN tag t ON t.id=at.tag_id "
                                    "WHERE at.asset_id=? AND t.name='文档'", (target['id'],))
            self.assertEqual(provenance['source'], 'user')
        finally:
            worker.close()

    def test_content_reuse_requires_current_successful_full_hash_and_model_version(self):
        from scanner import HASH_LIMIT
        self.asset = self.hash_asset(self.asset)
        self.save()
        target = self.hash_asset(add_image(self.db, self.folder.name, '另一份图片.jpg'))
        self.assertIsNotNone(self.store.content_match(target, 'v1'))
        self.assertIsNone(self.store.content_match(dict(target, hash=None), 'v1'))
        self.assertIsNone(self.store.content_match(dict(target, hash='legacy-sampled-hash'), 'v1'))
        self.assertIsNone(self.store.content_match(dict(target, size=HASH_LIMIT+1), 'v1'))
        self.assertIsNone(self.store.content_match(target, 'other-model-version'))
        for field in ('deleted', 'missing', 'excluded'):
            with self.subTest(field=field):
                self.db.update_asset(self.asset['id'], **{field: 1})
                self.assertIsNone(self.store.content_match(target, 'v1'))
                self.db.update_asset(self.asset['id'], **{field: 0})
        self.db.update_asset(self.asset['id'], mtime=self.asset['mtime']+5)
        self.assertIsNone(self.store.content_match(target, 'v1'))
        self.db.update_asset(self.asset['id'], mtime=self.asset['mtime'])
        self.store.fail(self.asset, 'v1', 'unfinished inference')
        self.assertIsNone(self.store.content_match(target, 'v1'))

    def test_changed_target_is_not_given_an_old_duplicate_result(self):
        self.asset = self.hash_asset(self.asset)
        self.save()
        target = self.hash_asset(add_image(self.db, self.folder.name, '待更新图片.jpg'))
        with patch('ai_engine.model_manifest', return_value={'version': 'v1'}):
            worker = AIIndexer(self.db, self.folder.name)
        try:
            changed = SimpleNamespace(st_size=target['size'], st_mtime=target['mtime']+.5,
                                      st_mtime_ns=round((target['mtime']+.5)*1e9))
            with patch('ai_index.os.stat', return_value=changed), \
                    patch.object(worker, '_load', side_effect=AssertionError('Stale target must not load models')):
                with self.assertRaisesRegex(ValueError, '文件已变化'):
                    worker._process(target)
            self.assertIsNone(self.store.result(target['id']))
        finally:
            worker.close()

    def test_source_with_manually_edited_classification_is_not_reused(self):
        self.asset = self.hash_asset(self.asset)
        self.save('原始 OCR')
        target = self.hash_asset(add_image(self.db, self.folder.name, '重复待分类.jpg'))
        self.assertIsNotNone(self.store.content_match(target, 'v1'))
        # Retaining every AI label still rewrites its provenance as user supplied.
        self.store.set_tags(self.asset['id'], ['文档', '票据', '源手工分类'])
        self.assertEqual(self.db.q('SELECT * FROM ai_tag_reject'), [])
        self.assertIsNone(self.store.content_match(target, 'v1'))
        # A rejected label must also forbid reuse if other AI labels remain.
        self.db.execute("UPDATE asset_tag SET source='ai' WHERE asset_id=? AND tag_id IN "
                        "(SELECT id FROM tag WHERE name='文档')", (self.asset['id'],))
        self.db.execute("INSERT INTO ai_tag_reject(asset_id,name) VALUES (?,?)", (self.asset['id'], '票据'))
        self.assertIsNone(self.store.content_match(target, 'v1'))

    def test_same_size_five_millisecond_change_invalidates_all_cached_result_paths(self):
        self.asset = self.hash_asset(self.asset)
        self.save('会议通知')
        duplicate = self.hash_asset(add_image(self.db, self.folder.name, '另一份.jpg'))
        self.assertIsNotNone(self.store.content_match(duplicate, 'v1'))
        self.db.update_asset(self.asset['id'], mtime=self.asset['mtime']+.005)
        self.assertIn(self.asset['id'], [row['id'] for row in self.store.pending('v1')])
        self.assertIsNone(self.store.result(self.asset['id']))
        self.assertFalse(self.store.vectors('v1'))
        self.assertIsNone(self.store.content_match(duplicate, 'v1'))
        self.assertEqual(self.store.stats('v1')['done'], 0)
        self.assertFalse(self.store.text_matches('会议通知'))
        self.assertFalse(self.store.text_matches('会议'))
        self.assertFalse(any(row['path'] == self.asset['path'] for row in self.store.export_text()))
        self.assertFalse(self.store.save(self.asset, 'v1', '过期写入', [], self.vector))


class WorkerTests(unittest.TestCase):
    def test_backlog_dispatch_never_waits_for_commands_but_still_prioritizes_them(self):
        with tempfile.TemporaryDirectory(prefix='photonest-ai-schedule-') as folder:
            db = DB(str(Path(folder)/'library.db'))
            with patch('ai_engine.model_manifest', return_value={'version': 'schedule'}):
                worker = AIIndexer(db, folder)
            waits, trace = [], []
            class Commands(queue.Queue):
                def get(self, block=True, timeout=None):
                    waits.append((block, self.empty(), bool(worker._jobs)))
                    return super().get(block=False)
            worker.commands = Commands()
            worker.commands.put(('probe-command',))
            worker._indexing = True
            worker._jobs = deque(dict(id=i, size=1, mtime=1, name=str(i)) for i in (1, 2, 3))
            def process(asset):
                trace.append(asset['id'])
                if asset['id'] == 3:
                    worker._stop.set()
            try:
                with patch.object(worker, '_process', side_effect=process), \
                        patch.object(worker, '_handle', side_effect=lambda task: trace.append(task[0])):
                    worker._run()
                self.assertEqual(trace, ['probe-command', 1, 2, 3])
                self.assertFalse(any(block and empty and backlog for block, empty, backlog in waits),
                                 'Queued indexing work must not pay a fixed empty-command timeout per photo')
            finally:
                worker.close()
                db.close()

    def test_idle_or_paused_worker_blocks_instead_of_busy_spinning(self):
        with tempfile.TemporaryDirectory(prefix='photonest-ai-idle-') as folder:
            db = DB(str(Path(folder)/'library.db'))
            with patch('ai_engine.model_manifest', return_value={'version': 'schedule'}):
                worker = AIIndexer(db, folder)
            try:
                for paused in (False, True):
                    calls = []
                    class Commands(queue.Queue):
                        def get(self, block=True, timeout=None):
                            calls.append((block, timeout))
                            worker._stop.set()
                            raise queue.Empty
                    worker._stop.clear()
                    worker._indexing = paused
                    worker._pause.set() if paused else worker._pause.clear()
                    worker.commands = Commands()
                    worker._run()
                    self.assertEqual(calls, [(True, .3)])
            finally:
                worker.close()
                db.close()

    def test_cached_duplicate_backlog_completes_without_loading_models(self):
        with tempfile.TemporaryDirectory(prefix='photonest-ai-duplicates-') as folder:
            db = DB(str(Path(folder)/'library.db'))
            assets = [add_image(db, folder, f'duplicate-{i}.jpg') for i in range(3)]
            for asset in assets:
                db.update_asset(asset['id'], hash=hashlib.blake2b(Path(asset['path']).read_bytes(), digest_size=16).hexdigest())
            source = db.get_asset(assets[0]['id'])
            AIStore(db).save(source, 'duplicate', '会议通知', [], np.array([1, 0, 0], dtype='<f4').tobytes(), ['文档'])
            with patch('ai_engine.model_manifest', return_value={'version': 'duplicate'}):
                worker = AIIndexer(db, folder)
            try:
                with patch.object(worker, '_load', side_effect=AssertionError('Cached jobs must not load models')), \
                        patch('ai_engine.read_image', side_effect=AssertionError('Cached jobs must not decode photos')):
                    worker.pause()
                    worker._process(db.get_asset(assets[1]['id']))
                    self.assertIsNone(worker.store.result(assets[1]['id']), 'Pause also prevents cached-result writes')
                    worker.start()
                    deadline = time.monotonic()+5
                    while worker.snapshot()['running'] and time.monotonic() < deadline:
                        time.sleep(.01)
                state = worker.snapshot()
                self.assertFalse(state['running'], state)
                self.assertEqual((state['done'], state['total'], state['errors']), (2, 2, 0))
                self.assertEqual(worker.store.stats('duplicate')['done'], 3)
                self.assertIsNone(worker._engine)
            finally:
                worker.close()
                db.close()

    def test_new_photo_refresh_preempts_backlog_and_wakes_idle_worker(self):
        entered, proceed = threading.Event(), threading.Event()
        clock = [100.0]
        class LiveEngine:
            calls = 0
            def __init__(self, root):
                pass
            def ocr(self, image):
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    if not proceed.wait(5):
                        raise RuntimeError('test did not release first inference')
                return '会议通知', []
            def encode_image(self, image):
                return np.array([1, 0, 0], dtype=np.float32)
            def classify(self, vector):
                return ['文档']

        with tempfile.TemporaryDirectory(prefix='photonest-live-ai-') as folder:
            db = DB(str(Path(folder)/'library.db'))
            with patch('ai_engine.model_manifest', return_value={'version': 'live'}), \
                    patch('ai_engine.Engine', LiveEngine), \
                    patch('ai_index.time', SimpleNamespace(monotonic=lambda: clock[0])):
                worker = AIIndexer(db, folder)
                processed = []
                real_process = worker._process
                def process(asset):
                    processed.append(asset['id'])
                    return real_process(asset)
                def wait_idle():
                    deadline = time.monotonic()+5
                    while worker.snapshot()['running'] and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertFalse(worker.snapshot()['running'], worker.snapshot())
                try:
                    original = [add_image(db, folder, f'old-{i}.jpg') for i in range(4)]
                    with patch.object(worker, '_process', side_effect=process), \
                            patch.object(worker.store, 'pending', wraps=worker.store.pending) as read_pending:
                        self.assertTrue(worker.start())
                        self.assertTrue(entered.wait(5))
                        newest = add_image(db, folder, 'new-while-busy.jpg')
                        clock[0] += 2
                        writes_before = db.conn.total_changes
                        for _ in range(20):
                            self.assertFalse(worker.start(), 'A busy start should refresh the existing worker')
                        self.assertEqual(db.conn.total_changes, writes_before,
                                         'Refresh notifications must not invalidate the vector cache with settings writes')
                        proceed.set()
                        wait_idle()
                        self.assertEqual(processed[:2], [original[0]['id'], newest['id']])
                        self.assertEqual(len(processed), 5)
                        self.assertEqual(len(set(processed)), 5, 'Refresh must not duplicate the old backlog')
                        self.assertEqual(worker.store.stats('live')['done'], 5)
                        self.assertEqual(read_pending.call_count, 2, 'Burst notifications should merge into one refresh query')
                        idle_photo = add_image(db, folder, 'new-after-idle.jpg')
                        clock[0] += 2
                        self.assertTrue(worker.start())
                        wait_idle()
                        self.assertEqual(processed[-1], idle_photo['id'])
                        self.assertEqual(worker.store.stats('live')['done'], 6)
                        self.assertEqual(read_pending.call_count, 3)
                        self.assertEqual(worker.snapshot()['errors'], 0)
                finally:
                    proceed.set()
                    worker.close()
                    db.close()

    def test_stale_queued_asset_rechecks_deleted_missing_and_excluded(self):
        with tempfile.TemporaryDirectory(prefix='photonest-stale-ai-') as folder:
            db = DB(str(Path(folder)/'library.db'))
            with patch('ai_engine.model_manifest', return_value={'version': 'test'}):
                worker = AIIndexer(db, folder)
                try:
                    stale_asset = add_image(db, folder)
                    with patch.object(worker, '_load', side_effect=AssertionError('Hidden asset must not load inference')) as load:
                        for field in ('deleted', 'missing', 'excluded'):
                            with self.subTest(field=field):
                                db.update_asset(stale_asset['id'], **{field: 1})
                                worker._process(stale_asset)
                                db.update_asset(stale_asset['id'], **{field: 0})
                        load.assert_not_called()
                    self.assertIsNone(worker.store.result(stale_asset['id']))
                finally:
                    worker.close()
                    db.close()

    def test_backfill_resume_incremental_and_search(self):
        class FakeEngine:
            def __init__(self, root):
                pass
            def ocr(self, image):
                time.sleep(.03)
                return "会议通知", []
            def encode_image(self, image):
                return np.array([1, 0, 0], dtype=np.float32)
            def encode_text(self, texts):
                return np.array([[1, 0, 0] for _ in texts], dtype=np.float32)
            def classify(self, vector):
                return ["文档"]

        with tempfile.TemporaryDirectory(prefix="photonest-worker-") as folder:
            db = DB(str(Path(folder) / "library.db"))
            with patch("ai_engine.model_manifest", return_value={"version": "test"}), patch("ai_engine.Engine", FakeEngine):
                worker = AIIndexer(db, folder)
                try:
                    assets = [add_image(db, folder, f"图片{i}.jpg") for i in range(8)]
                    worker.start()
                    worker.pause()
                    time.sleep(.15)
                    self.assertFalse(worker.snapshot()["running"])
                    worker.start()
                    deadline = time.monotonic() + 5
                    while worker.snapshot()["phase"] == "loading" and time.monotonic() < deadline:
                        time.sleep(.01)
                    add_image(db, folder, "识别中新增.jpg")
                    worker.start()
                    deadline = time.monotonic() + 10
                    while worker.snapshot()["running"] and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertEqual(worker.store.stats("test")["done"], 9)
                    self.assertEqual(len(worker._search("会议", "文字匹配")), 9)
                    self.assertEqual(len(worker._search("文档", "画面内容")), 9)
                    self.assertEqual(worker.store.pending("test"), [])
                    add_image(db, folder, "新增.jpg")
                    worker.start()
                    deadline = time.monotonic() + 10
                    while worker.snapshot()["running"] and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertEqual(worker.store.stats("test")["done"], 10)
                    self.assertEqual(worker.snapshot()["total"], 1)
                    cached = worker.store.result(assets[0]["id"])
                    self.assertEqual(cached["text"], "会议通知")
                finally:
                    worker.close()
                    db.close()


def real_model_check(model_dir, output=None):
    """Exercises the same engine in source tests and the frozen executable."""
    from ai_engine import Engine, normalized
    engine = Engine(model_dir)
    image = Image.new("RGB", (1000, 450), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 54)
    draw.text((65, 45), "会议通知", font=font, fill="black")
    draw.text((65, 140), "项目报销发票", font=font, fill="black")
    draw.text((65, 240), "PhotoNest 2026", font=font, fill="black")
    start = time.perf_counter()
    text, blocks = engine.ocr(image)
    seconds = time.perf_counter() - start
    compact = "".join(text.split())
    assert "会议通知" in compact and "项目报销发票" in compact, repr(text)
    assert "photonest2026" in compact.lower(), repr(text)
    features = engine.encode_image(image)
    assert features.shape == (engine.manifest["clip_dim"],) and np.isfinite(features).all()
    assert abs(float(np.linalg.norm(features)) - 1) < 1e-4
    prompts = engine.encode_text(["会议通知", "项目报销发票", "白色文档上的文字", "蓝色大海的照片"])
    scores = prompts @ features
    assert max(scores[:3]) > scores[3], scores.tolist()
    crop_text, _ = engine.ocr(image.crop((45, 30, 420, 125)))
    assert "会议通知" in "".join(crop_text.split()), repr(crop_text)
    report = dict(ocr_text=text, ocr_blocks=len(blocks), ocr_seconds=round(seconds, 3),
        crop_text=crop_text, tags=engine.classify(features), semantic_scores=scores.tolist(),
        model_version=engine.version)
    if output:
        import json
        Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    import sys
    if "--models" in sys.argv:
        import json
        print(json.dumps(real_model_check(Path(__file__).parent / "models"), ensure_ascii=False, indent=2))
    else:
        unittest.main(verbosity=2)
