"""Catch-up tests use synthetic journals and an isolated library, never host logs."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

from changefeed import JournalGap, JournalUnavailable, JournalCancelled
from db import DB
from sync import LibrarySync, _key
from scanner import Scanner
from PIL import Image


class FakeScanner:
    def __init__(self, root):
        self.root = str(root)
        self.calls = []
        self.state = dict(running=False, phase='done', errors=0, run_id=0)

    def accepts_path(self, path):
        return os.path.commonpath((os.path.abspath(path), self.root)) == self.root

    def snapshot(self):
        return dict(self.state)

    def start(self, **options):
        self.calls.append(options)
        if not self.state['running']:
            self.state['run_id'] += 1
        self.state['running'] = True
        return True

    def stop(self):
        self.state['running'] = False


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='PhotoNest-sync-')
        self.root = Path(self.temp.name)
        self.db = DB(str(self.root/'library.db'))
        self.db.add_root(str(self.root))
        self.db.set_setting('initial_scan_complete', '1')
        self.scanner = FakeScanner(self.root)
        self.sync = LibrarySync(self.db, self.scanner)
        self.volume = self.root.drive+os.sep
        self.saved = dict(volume=self.volume, device=42, journal_id=1, first_usn=0, next_usn=1)
        self.db.set_setting('sync_usn:'+_key(self.volume), json.dumps(self.saved))

    def tearDown(self):
        self.sync.close()
        self.db.close()
        self.temp.cleanup()

    def test_cursor_and_pending_work_survive_restart_before_scan(self):
        photo = str(self.root/'new photo.gif')
        next_state = dict(self.saved, next_usn=2)
        journal = SimpleNamespace(changes=lambda saved, **kw: ([dict(file_id=123, name='new photo.gif',
            is_dir=False, path=photo)], next_state), resolve=lambda event: photo)
        self.sync._journals[self.volume] = journal
        self.sync.catch_up()
        self.assertEqual(json.loads(self.db.get_setting('sync_pending')), [photo])
        self.assertEqual(json.loads(self.db.get_setting('sync_usn:'+_key(self.volume)))['next_usn'], 2)
        self.assertFalse(self.scanner.calls, 'Work must be durable before scanning starts')
        resumed = LibrarySync(self.db, self.scanner)
        self.assertEqual(resumed._pending, {photo})

    def test_journal_gap_preserves_cursor_and_requests_baseline(self):
        def gap(*args, **kw):
            raise JournalGap('old records removed')
        self.sync._journals[self.volume] = SimpleNamespace(changes=gap, snapshot=lambda: dict(self.saved, journal_id=2))
        self.sync.catch_up()
        self.assertTrue(self.sync._baseline)
        self.assertEqual(json.loads(self.db.get_setting('sync_usn:'+_key(self.volume))), self.saved)

    def test_unreadable_log_falls_back_after_one_reconciliation(self):
        def unavailable(*args, **kwargs):
            raise JournalUnavailable('snapshot supported but log read denied')
        self.sync._journals[self.volume] = SimpleNamespace(changes=unavailable,
            snapshot=lambda: dict(self.saved, next_usn=10))
        self.sync.catch_up()
        self.assertTrue(self.sync._baseline)
        self.assertNotIn(self.volume, self.sync._journals)
        self.sync.status['baseline_started'] = True
        self.scanner.state.update(run_id=1, phase='done', baseline_ready=True, retry_paths=[])
        with patch.object(self.sync, '_refresh_watches'):
            self.sync._step()
            self.sync._step()
        self.assertFalse(self.sync._baseline)
        self.assertFalse(self.scanner.calls, 'An unreadable log must not cause repeated full scans')

    def test_returning_folder_is_reconciled_before_only_watching_new_events(self):
        folder = self.root/'temporarily absent photos'
        folder.mkdir()
        watcher = SimpleNamespace(start=lambda: None, close=lambda: None)
        self.sync.watcher_factory = lambda *args: watcher
        self.sync._refresh_watches([str(folder)])
        folder.rmdir()
        self.sync._refresh_watches([str(folder)])
        folder.mkdir()
        self.sync._refresh_watches([str(folder)])
        self.assertIn(_key(folder), self.sync._pending)
        self.assertIn(_key(folder), json.loads(self.db.get_setting('sync_pending')))

    def test_live_changes_are_durable_while_paused_and_ignore_non_media_files(self):
        photo = str(self.root/'photo.jpg')
        self.sync.set_paused(True)
        self.sync.notify(dict(path=str(self.root/'unrelated.txt')))
        self.sync.notify(dict(path=photo))
        self.sync._step()
        self.assertEqual(self.sync._pending, {photo})
        self.assertFalse(self.scanner.calls)
        self.assertEqual(json.loads(self.db.get_setting('sync_pending')), [photo])

    def test_change_during_inflight_scan_is_not_lost_when_old_work_finishes(self):
        photo = str(self.root/'photo.jpg')
        self.sync._pending = {photo}
        self.sync._versions[photo] = 2
        self.sync._inflight = {photo: 1}
        self.sync._last_journal = 100
        with patch('sync.time.monotonic', return_value=100):
            self.sync._step()
        self.assertIn(photo, self.sync._pending)
        self.assertEqual(self.scanner.calls[-1]['paths'], [photo])

    def test_overflow_requests_range_reconciliation(self):
        self.sync._overflow.set()
        self.sync._drain_events()
        self.assertEqual(self.sync._pending, {str(self.root)})

    def test_overflow_during_inflight_root_scan_keeps_a_new_generation(self):
        scope = str(self.root)
        self.sync._pending = {scope}
        self.sync._versions[scope] = 1
        self.sync._inflight = {scope: 1}
        self.sync._overflow.set()
        self.sync._drain_events()
        self.assertEqual(self.sync._versions[scope], 2)
        self.sync._last_event = 0
        self.sync._last_journal = 100
        with patch('sync.time.monotonic', return_value=100):
            self.sync._step()
        self.assertIn(scope, self.sync._pending)
        self.assertEqual(self.sync._inflight[scope], 2)

    def test_unresolvable_new_directory_reconciles_known_parent(self):
        self.db.remember_directory('42:55', str(self.root), str(self.root))
        paths = self.sync._journal_paths(self.volume, SimpleNamespace(resolve=lambda event: None),
            [dict(file_id=123, parent_id=55, is_dir=True, name='moved photos', reason=0x2000)], self.saved)
        self.assertEqual(paths, {str(self.root)})

    def test_unresolvable_known_rename_reconciles_new_parent_and_old_name(self):
        old = str(self.root/'old.jpg')
        asset = dict(path=old, name='old.jpg', ext='.jpg', size=5, mtime=1,
                     deleted=0, media_type='image', file_id='42:123')
        self.db.insert_asset(asset)
        self.db.remember_directory('42:55', str(self.root), str(self.root))
        paths = self.sync._journal_paths(self.volume, SimpleNamespace(resolve=lambda event: None),
            [dict(file_id=123, parent_id=55, is_dir=False, name='new.jpg', reason=0x2000)], self.saved)
        self.assertEqual(paths, {str(self.root), old})

    def test_cancelled_baseline_does_not_confirm_abandoned_other_volume_work(self):
        photo = str(self.root/'other-volume.jpg')
        self.sync._baseline = True
        self.sync.status['baseline_started'] = True
        self.sync._pending = {photo}
        self.sync._versions[photo] = 1
        self.sync._inflight = {photo: 1}
        self.sync._save_pending()
        self.scanner.state.update(phase='paused', baseline_ready=False)
        self.sync._step()
        self.assertFalse(self.sync._inflight)
        self.assertIn(photo, self.sync._pending)
        self.assertEqual(json.loads(self.db.get_setting('sync_pending')), [photo])
        self.assertNotEqual(self.db.get_setting('sync_baseline'), '1')

    def test_baseline_persists_only_io_retry_paths_with_its_checkpoint(self):
        failed = _key(self.root/'temporarily locked')
        self.sync._baseline = True
        self.sync.status['baseline_started'] = True
        self.sync._bootstrap[self.volume] = self.saved
        self.sync._last_journal = 100
        self.scanner.state.update(running=True, phase='hashing', baseline_ready=True,
                                  run_id=1, errors=1, retry_paths=[failed])
        with patch('sync.time.monotonic', return_value=100), \
                patch.object(self.sync, '_refresh_watches'):
            self.sync._step()
            self.sync._step()
        self.assertEqual(json.loads(self.db.get_setting('sync_pending')), [failed])
        self.assertEqual(self.db.get_setting('sync_baseline'), '1')
        self.assertFalse(self.scanner.calls, 'Do not resubmit an I/O failure during the same scan')

    def test_one_failed_file_does_not_repeat_successful_target_scans(self):
        good, bad = _key(self.root/'good.jpg'), _key(self.root/'locked.jpg')
        self.sync._pending = {good, bad}
        self.sync._versions = {good: 1, bad: 1}
        self.sync._inflight = {good: 1, bad: 1}
        self.sync._last_journal = 100
        self.scanner.state.update(run_id=1, errors=1, retry_paths=[bad])
        with patch('sync.time.monotonic', return_value=100):
            self.sync._step()
            version = self.sync._versions[bad]
            self.sync._step()
        self.assertEqual(self.sync._pending, {bad})
        self.assertEqual(self.sync._versions[bad], version, 'Completion retries are harvested once')
        self.assertFalse(self.scanner.calls, 'Wait before retrying failed I/O')
        self.assertEqual(json.loads(self.db.get_setting('sync_pending')), [bad])

    def test_idle_path_validation_errors_are_durable_without_a_new_scan_run(self):
        self.sync._last_journal = 100
        with patch('sync.time.monotonic', return_value=100):
            self.sync._step()
            bad = _key(self.root/'temporarily unreadable.jpg')
            self.scanner.state['retry_paths'] = [bad]
            self.sync._step()
        self.assertEqual(json.loads(self.db.get_setting('sync_pending')), [bad])

    def test_directory_timestamp_record_does_not_rescan_entire_media_tree(self):
        self.db.remember_directory('42:123', str(self.root), str(self.root))
        journal = SimpleNamespace(resolve=lambda event: self.fail('Unneeded directory lookup'))
        paths = self.sync._journal_paths(self.volume, journal,
            [dict(file_id=123, is_dir=True, name='media', reason=0x8000)], self.saved)
        self.assertFalse(paths)

    def test_repeated_journal_writes_are_coalesced_before_resolving_paths(self):
        photo = str(self.root/'photo.jpg')
        with patch.object(self.db, 'find_by_identity', wraps=self.db.find_by_identity) as lookup:
            journal = SimpleNamespace(resolve=lambda event: photo)
            paths = self.sync._journal_paths(self.volume, journal,
                [dict(file_id=123, is_dir=False, name='photo.jpg', reason=1) for _ in range(20)], self.saved)
        self.assertEqual(paths, {photo})
        self.assertEqual(lookup.call_count, 1)

    def test_cancelled_journal_read_does_not_establish_a_new_cursor(self):
        def cancel(*args, **kw):
            raise JournalCancelled('stopping')
        self.sync._journals[self.volume] = SimpleNamespace(changes=cancel)
        self.sync.catch_up()
        self.assertFalse(self.sync._baseline)
        self.assertEqual(json.loads(self.db.get_setting('sync_usn:'+_key(self.volume))), self.saved)

    def test_unavailable_second_volume_does_not_skip_first_volume_history(self):
        other = 'Z:\\media'
        self.db.set_setting('sync_baseline', '1')
        self.db.set_setting('source_rules_version', 'test')
        current = dict(self.saved, next_usn=100)
        def factory(volume):
            if volume[0].casefold() == 'z':
                raise JournalUnavailable('unsupported test volume')
            return SimpleNamespace(snapshot=lambda: current)
        self.sync.journal_factory = factory
        with patch.object(self.sync, '_scopes', return_value=[str(self.root), other]), \
                patch.object(self.sync, '_refresh_watches'), patch.object(self.db, 'reclassify_sources'), \
                patch('sources.discover_media_roots', return_value=[]):
            self.sync._prepare()
        self.assertTrue(self.sync._baseline)
        self.assertNotIn(self.volume, self.sync._bootstrap)
        self.assertEqual(self.sync._baseline_paths, {other})
        self.assertEqual(json.loads(self.db.get_setting('sync_usn:'+_key(self.volume)))['next_usn'], 1)


class DatabaseSyncTests(unittest.TestCase):
    def test_batch_is_atomic_and_missing_is_separate_from_trash(self):
        with tempfile.TemporaryDirectory(prefix='PhotoNest-db-sync-') as directory:
            db = DB(str(Path(directory)/'library.db'))
            try:
                asset = dict(path=str(Path(directory)/'photo.gif'), name='photo.gif', ext='.gif',
                    size=5, mtime=1, deleted=0, favorite=1, media_type='image', file_id='42:123')
                with self.assertRaises(RuntimeError):
                    with db.batch():
                        db.insert_asset(asset)
                        raise RuntimeError('interrupted batch')
                self.assertEqual(db.counts()['active'], 0)
                asset_id = db.insert_asset(asset)
                db.mark_missing(asset_id)
                self.assertEqual(db.query_assets()['total'], 0)
                self.assertEqual(db.query_assets(view='trash')['total'], 0)
                self.assertTrue(db.find_by_identity('42:123')['favorite'])
                db.mark_missing(asset_id, False)
                self.assertEqual(db.query_assets(type_='gif')['total'], 1)
                self.assertEqual(db.counts()['gifs'], 1)
            finally:
                db.close()


class LiveAndOfflineIntegrationTests(unittest.TestCase):
    def test_live_notifications_then_offline_journal_catchup_without_full_walk(self):
        with tempfile.TemporaryDirectory(prefix='PhotoNest-live-offline-') as directory:
            root = Path(directory)
            photos = root/'photos'
            photos.mkdir()
            data = root/'data'
            db = DB(str(data/'library.db'))
            db.add_root(str(photos))
            db.set_setting('initial_scan_complete', '1')
            scanner = Scanner(db, str(data/'cache'), exclude_paths=[str(data)])
            log = []
            volume = photos.drive+os.sep
            device = photos.stat().st_dev
            class FixtureJournal:
                def __init__(self, volume):
                    pass
                def snapshot(self):
                    return dict(volume=volume, volume_identity='owned-fixture-volume', device=device,
                                journal_id=1, first_usn=0, next_usn=len(log)+1)
                def changes(self, saved, **kwargs):
                    current = self.snapshot()
                    return [event for event in log if saved['next_usn'] <= event['usn'] < current['next_usn']], current
                def resolve(self, event):
                    path = event.get('path')
                    return path if path and os.path.exists(path) else None
            def wait_for(condition):
                deadline = time.monotonic()+10
                while time.monotonic() < deadline:
                    if condition():
                        return
                    time.sleep(.03)
                self.fail('Timed out waiting for live/offline synchronization: '
                    f'scanner={scanner.snapshot()}, sync={(resumed or manager).snapshot()}, '
                    f'pending={db.get_setting("sync_pending")}, '
                    f'rows={db.q("SELECT id,name,missing,file_id FROM asset")}')
            manager = resumed = None
            try:
                with patch('sources.discover_media_roots', return_value=[]):
                    manager = LibrarySync(db, scanner, journal_factory=FixtureJournal)
                    manager.start()
                    wait_for(lambda: db.get_setting('sync_baseline') == '1' and not scanner.snapshot()['running'])
                    live = photos/'live.gif'
                    Image.new('RGB', (40,30), 'red').save(live)
                    wait_for(lambda: db.query_assets()['total'] == 1 and not scanner.snapshot()['running'])
                    row = db.find_by_path(str(live))
                    self.assertIsNotNone(row)
                    db.set_favorite([row['id']], True)
                    manager.close()
                    scanner.stop(wait=True)
                    old_id = int(row['file_id'].split(':')[1])
                    live.unlink()
                    offline = photos/'offline.jpg'
                    Image.new('RGB', (40,30), 'blue').save(offline)
                    log.extend([
                        dict(file_id=old_id, parent_id=photos.stat().st_ino, name=live.name,
                             is_dir=False, reason=0x200, usn=1),
                        dict(file_id=offline.stat().st_ino, parent_id=photos.stat().st_ino,
                             name=offline.name, is_dir=False, reason=0x100, usn=2, path=str(offline)),
                    ])
                    resumed = LibrarySync(db, scanner, journal_factory=FixtureJournal)
                    with patch.object(scanner, '_iter_files', wraps=scanner._iter_files) as walk:
                        resumed.start()
                        wait_for(lambda: db.find_by_path(str(offline)) is not None
                                 and db.get_asset(row['id'])['missing'] == 1
                                 and not scanner.snapshot()['running'])
                        self.assertFalse(walk.called, 'A valid file-only journal delta must not walk the whole library')
                    self.assertEqual(db.query_assets()['total'], 1)
                    self.assertTrue(db.get_asset(row['id'])['favorite'])
                    self.assertEqual(db.query_assets(view='trash')['total'], 0)
            finally:
                if manager:
                    manager.close()
                if resumed:
                    resumed.close()
                scanner.stop(wait=True)
                db.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
