"""Integrated controls and reversible filtering; owned fixtures only."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from app import PhotoNestApp
from ai_store import AIStore
from db import DB
from scanner import Scanner
from test_ui import make_library


class ExclusionTests(unittest.TestCase):
    def test_excluded_photos_leave_views_and_ai_without_deleting_or_losing_user_state(self):
        with tempfile.TemporaryDirectory(prefix='PhotoNest-filter-') as directory:
            root = Path(directory)
            db = make_library(root, 2)
            try:
                asset = db.get_asset(1)
                original = Path(asset['path'])
                sibling = original.parent.with_name(original.parent.name+'-other')
                db.update_asset(2, path=str(sibling/'kept.jpg'), dir=str(sibling))
                db.set_favorite([1], True)
                store = AIStore(db)
                store.save(db.get_asset(1), 'fixture', '个人照片', [], b'1234', ['人物'])
                db.set_scan_exclude_paths([str(original.parent)])
                self.assertTrue(original.exists())
                self.assertFalse(db.get_asset(1)['deleted'])
                self.assertFalse(db.get_asset(1)['missing'])
                self.assertTrue(db.get_asset(1)['excluded'])
                self.assertEqual(db.counts()['active'], 1)
                self.assertEqual(db.query_assets(view='trash')['total'], 0)
                self.assertEqual([row['id'] for row in store.pending('fixture')], [2])
                self.assertEqual(store.text_matches('个人照片'), [])
                self.assertFalse(store.save(db.get_asset(1), 'fixture', 'inflight result', []))
                db.set_scan_exclude_paths([])
                self.assertEqual(db.counts()['active'], 2)
                self.assertTrue(db.get_asset(1)['favorite'])
                self.assertEqual(store.result(1)['text'], '个人照片')
                self.assertIn('人物', db.tags_for(1))
            finally:
                db.close()

    def test_resource_filter_reclassifies_existing_records_and_can_be_disabled(self):
        with tempfile.TemporaryDirectory(prefix='PhotoNest-resource-') as directory:
            path = str(Path(directory)/'library.db')
            db = DB(path)
            resource = os.path.join(Path(directory).drive+os.sep, 'Windows', 'Web', 'Wallpaper', 'fixture.jpg')
            asset_id = db.insert_asset(dict(path=resource, name='fixture.jpg', ext='.jpg',
                size=5, mtime=1, deleted=0, media_type='image'))
            db.close()
            db = DB(path)
            try:
                self.assertTrue(db.get_asset(asset_id)['excluded'])
                self.assertEqual(db.counts()['active'], 0)
                db.set_setting('scan_filter_resources', '0')
                db.apply_scan_exclusions()
                self.assertFalse(db.get_asset(asset_id)['excluded'])
                self.assertEqual(db.counts()['active'], 1)
            finally:
                db.close()


class PipelineUITests(unittest.TestCase):
    def test_new_photos_start_ai_while_scanning_and_tray_pause_stops_both(self):
        with tempfile.TemporaryDirectory(prefix='PhotoNest-pipeline-ui-') as directory:
            root = Path(directory)
            db = make_library(root, 1)
            db.set_setting('initial_scan_complete', '1')
            db.set_setting('ai_enabled', '0')  # Upgrade no longer needs a separate switch.
            scanner = Scanner(db, str(root/'cache'))
            app = None
            with patch('app.LibrarySync') as sync, patch('app.TrayIcon'), \
                    patch('ai_index.AIIndexer.start', return_value=True) as ai_start:
                sync.return_value.snapshot.return_value = dict(paused=False, message='同步中')
                try:
                    app = PhotoNestApp(db, scanner, str(root/'cache'), str(root), background=True)
                    self.assertEqual(db.get_setting('ai_enabled'), '1')
                    scanner._set(phase='walking', running=True, baseline_ready=False)
                    scanner.revision += 1
                    app._hidden_to_tray = True
                    app._sync_ai_pipeline()
                    ai_start.assert_called_once()
                    app._app_events.put('pause')
                    app._poll_background()
                    sync.return_value.set_paused.assert_called_with(True)
                    self.assertTrue(scanner._stop.is_set())
                    self.assertEqual(db.get_setting('ai_paused'), '1')
                    self.assertEqual(db.get_setting('scan_paused'), '1')
                    scanner.revision += 1
                    app._sync_ai_pipeline()
                    ai_start.assert_called_once()
                    sync.return_value.snapshot.return_value = dict(paused=True, message='已暂停')
                    app._app_events.put('pause')
                    app._poll_background()
                    sync.return_value.set_paused.assert_called_with(False)
                    self.assertEqual(db.get_setting('scan_paused'), '0')
                    ai_start.assert_called_with(retry=True)
                finally:
                    if app:
                        app._on_close()
                    else:
                        db.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
