"""Computer discovery and streaming indexing; scans only isolated temporary drives."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
import os
import json
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
from db import DB
from scanner import Scanner, file_hash, probe
from sources import source_details


class AutoScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="photonest-scan-")
        self.folder = Path(self.temp.name)
        self.drive = self.folder / "DriveC"
        self.drive.mkdir()
        self.db = DB(str(self.folder / "data/library.db"))
        self.scanner = Scanner(self.db, str(self.folder / "data/cache"))

    def tearDown(self):
        self.scanner.stop(wait=True)
        self.db.close()
        self.temp.cleanup()

    def photo(self, relative):
        path = self.drive / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (80, 60), "orange").save(path)
        return path

    def wait(self):
        self.scanner._thread.join(10)
        self.assertFalse(self.scanner.snapshot()["running"])

    def test_discovers_fixed_disks_priority_folders_and_avoids_overlapping_ranges(self):
        a = self.photo("Pictures/first.jpg")
        self.photo("Travel/second.png")
        self.photo("Windows/icon.png")
        self.photo("Users/A/AppData/cache.png")
        self.photo("node_modules/icon.png")
        self.photo("_internal/asset.png")
        self.photo(".personal/hidden-photo.png")
        self.db.add_root(str(self.drive / "Pictures"))
        self.scanner._process(str(a.parent), str(a))
        self.db.set_favorite([1], True)
        with patch("scanner.fixed_drives", return_value=[str(self.drive)]), \
                patch("scanner.priority_folders", return_value=[str(a.parent)]):
            self.assertTrue(self.scanner.start(discover=True, initial=True))
            self.wait()
        self.assertEqual(self.db.counts()["active"], 3)
        self.assertEqual(self.scanner.snapshot()["total"], 3)
        self.assertEqual(self.db.get_asset(1)["path"], str(a))
        self.assertEqual(self.db.get_asset(1)["root"], str(a.parent))
        self.assertTrue(self.db.get_asset(1)["favorite"])
        self.assertEqual(sum(r["count"] for r in self.db.list_roots()), 3)
        self.assertEqual(self.db.get_setting("initial_scan_complete"), "1")

    def test_processes_first_photo_before_enumerating_rest(self):
        first, second = self.photo("a.jpg"), self.photo("b.jpg")
        self.db.add_root(str(self.drive))
        def stream(roots):
            yield str(self.drive), str(first)
            self.assertIsNotNone(self.db.find_by_path(str(first)))
            yield str(self.drive), str(second)
        with patch.object(self.scanner, "_iter_files", stream):
            self.scanner.start()
            self.wait()
        self.assertEqual(self.db.counts()["active"], 2)

    def test_pause_and_resume_skip_completed_files_and_preserve_user_state(self):
        for i in range(4):
            self.photo(f"Pictures/{i}.jpg")
        process = self.scanner._process
        def pause_after_first(root, path):
            result = process(root, path)
            self.scanner.stop()
            return result
        self.db.set_setting("initial_scan_complete", "0")
        with patch("scanner.fixed_drives", return_value=[str(self.drive)]), \
                patch("scanner.priority_folders", return_value=[]), \
                patch.object(self.scanner, "_process", pause_after_first):
            self.scanner.start(discover=True, initial=True)
            self.wait()
        self.assertEqual(self.scanner.snapshot()["phase"], "paused")
        self.assertEqual(self.db.get_setting("initial_scan_complete"), "0")
        self.db.set_favorite([1], True)
        with patch("scanner.priority_folders", return_value=[]):
            self.scanner.start(initial=True)
            self.wait()
        self.assertEqual(self.db.counts()["active"], 4)
        self.assertTrue(self.db.get_asset(1)["favorite"])
        self.assertEqual(self.scanner.snapshot()["skipped"], 1)
        self.assertEqual(self.db.get_setting("initial_scan_complete"), "1")

    def test_excludes_own_data_and_directory_links(self):
        self.photo("Pictures/real.jpg")
        own = self.photo("PhotoNestData/cache.jpg").parent
        self.scanner.exclude_paths += (str(own),)
        self.db.add_root(str(self.drive))
        with patch("scanner.priority_folders", return_value=[]), \
                patch("scanner.os.path.islink", side_effect=lambda p: str(p).endswith("Linked")):
            self.photo("Linked/repeat.jpg")
            self.scanner.start()
            self.wait()
        self.assertEqual(self.db.counts()["active"], 1)

    def test_discovery_failure_can_retry_and_cancelled_hash_returns_early(self):
        with patch("scanner.fixed_drives", return_value=[]):
            self.scanner.start(discover=True, initial=True)
            self.wait()
        self.assertEqual(self.scanner.snapshot()["phase"], "error")
        self.assertNotEqual(self.db.get_setting("initial_scan_complete"), "1")
        photo = self.photo("a.jpg")
        stop = threading.Event()
        stop.set()
        self.assertEqual(file_hash(str(photo), photo.stat().st_size, stop), "")
        checks = iter((False, True))
        interrupted = SimpleNamespace(is_set=lambda: next(checks, True))
        with patch('scanner.HASH_LIMIT', 0):
            self.assertEqual(file_hash(str(photo), photo.stat().st_size, interrupted), '',
                             'Sampled large files must also yield to pending changes')

    def test_cloud_only_placeholder_is_not_opened_or_downloaded(self):
        self.photo("local.jpg")
        cloud = self.photo("cloud.jpg")
        original_stat = os.stat
        def file_stat(path, **kwargs):
            if str(path) == str(cloud):
                return SimpleNamespace(st_file_attributes=0x1000, st_reparse_tag=0)
            return original_stat(path, **kwargs)
        self.db.add_root(str(self.drive))
        with patch("scanner.priority_folders", return_value=[]), patch("scanner.os.stat", file_stat):
            self.scanner.start()
            self.wait()
        self.assertEqual(self.db.counts()["active"], 1)
        self.assertIsNone(self.db.find_by_path(str(cloud)))

    def test_targeted_update_only_checks_requested_photo_and_empty_start_does_not_walk(self):
        first = self.photo('Pictures/new.jpg')
        self.photo('Unrelated/not-indexed.jpg')
        self.db.add_root(str(self.drive))
        with patch.object(self.scanner, '_iter_files', side_effect=AssertionError('Unexpected directory traversal')):
            self.assertTrue(self.scanner.start(paths=[str(first)]))
            self.wait()
            self.assertTrue(self.scanner.start(paths=[]))
            self.wait()
        self.assertEqual(self.db.counts()['active'], 1)
        self.assertEqual(self.db.find_by_path(str(first))['width'], 80)
        self.assertGreater(self.scanner.revision, 0)

    def test_external_delete_and_reappearance_preserve_favorite_without_trashing(self):
        photo = self.photo('Pictures/keep.jpg')
        self.db.add_root(str(self.drive))
        self.scanner._process(str(self.drive), str(photo))
        asset = self.db.find_by_path(str(photo))
        self.db.set_favorite([asset['id']], True)
        photo.unlink()
        self.scanner.start(paths=[str(photo)])
        self.wait()
        self.assertTrue(self.db.get_asset(asset['id'])['missing'])
        self.assertTrue(self.db.get_asset(asset['id'])['favorite'])
        self.assertEqual(self.db.counts()['active'], 0)
        self.assertEqual(self.db.counts()['trash'], 0)
        self.photo('Pictures/keep.jpg')
        self.scanner.start(paths=[str(photo)])
        self.wait()
        restored = self.db.get_asset(asset['id'])
        self.assertFalse(restored['missing'])
        self.assertTrue(restored['favorite'])

    def test_rename_reuses_asset_and_directory_identities(self):
        photo = self.photo('Album/first.jpg')
        self.db.add_root(str(self.drive))
        with patch('scanner.priority_folders', return_value=[]):
            self.scanner.start()
            self.wait()
        asset = self.db.find_by_path(str(photo))
        self.db.set_favorite([asset['id']], True)
        directory_identity = f'{photo.parent.stat().st_dev}:{photo.parent.stat().st_ino}'
        moved = self.drive/'Moved'
        self.assertTrue(moved.resolve().is_relative_to(self.drive.resolve()))
        photo.parent.rename(moved)
        self.scanner.start(paths=[str(photo.parent), str(moved)])
        self.wait()
        current = self.db.get_asset(asset['id'])
        self.assertEqual(current['path'], str(moved/photo.name))
        self.assertFalse(current['missing'])
        self.assertTrue(current['favorite'])
        self.assertEqual(self.db.counts()['active'], 1)
        self.assertEqual(self.db.directory_by_identity(directory_identity)['path'], str(moved))

    def test_unreadable_subtree_does_not_mark_photos_missing(self):
        photo = self.photo('Locked/keep.jpg')
        self.db.add_root(str(self.drive))
        self.scanner._process(str(self.drive), str(photo))
        def denied(source, *, onerror, **kwargs):
            onerror(PermissionError(13, 'denied fixture', str(photo.parent)))
            return iter(())
        with patch('scanner.os.walk', side_effect=denied):
            self.scanner.start(paths=[str(photo.parent)])
            self.wait()
        self.assertFalse(self.db.find_by_path(str(photo))['missing'])
        self.assertEqual(self.scanner.snapshot()['errors'], 1)

    def test_lightweight_index_metadata_and_hash_use_bounded_transactions(self):
        for index in range(260):
            self.photo(f'Pictures/{index}.jpg')
        self.db.add_root(str(self.drive))
        commits = []
        self.db.conn.set_trace_callback(lambda sql: commits.append(sql) if sql == 'COMMIT' else None)
        with patch('scanner.priority_folders', return_value=[]):
            self.scanner.start(initial=True)
            self.wait()
        self.db.conn.set_trace_callback(None)
        self.assertLess(len(commits), 40, 'Scan writes should commit batches, not individual files')
        self.assertTrue(self.scanner.snapshot()['baseline_ready'])
        self.assertEqual(self.db.get_setting('initial_scan_complete'), '1')
        self.assertEqual(self.db.q1('SELECT COUNT(*) AS n FROM asset WHERE width=80 AND hash IS NOT NULL')['n'], 260)

    def test_pending_photo_preempts_hash_and_baseline_is_ready_before_hash_finishes(self):
        photo = self.photo('Pictures/first.jpg')
        self.db.add_root(str(self.drive))
        self.scanner._process(str(self.drive), str(photo))
        ready, proceed = threading.Event(), threading.Event()
        original_hash = file_hash
        blocked = []
        def hashing(path, size, stop):
            if not blocked:
                blocked.append(path)
                ready.set()
                proceed.wait(3)
                self.assertTrue(stop.is_set(), 'Queued filesystem changes should interrupt hashing')
            return original_hash(path, size, stop)
        with patch('scanner.file_hash', side_effect=hashing), \
                patch.object(self.scanner, '_iter_files', side_effect=AssertionError('Unexpected full scan')):
            self.scanner.start(paths=[], initial=True)
            self.assertTrue(ready.wait(3))
            run_id = self.scanner.snapshot()['run_id']
            self.assertTrue(self.scanner.snapshot()['baseline_ready'])
            self.assertEqual(self.db.get_setting('initial_scan_complete'), '1')
            new = self.photo('Pictures/new.jpg')
            self.assertTrue(self.scanner.start(paths=[str(new)]))
            self.assertEqual(self.scanner.snapshot()['run_id'], run_id)
            proceed.set()
            self.wait()
        self.assertEqual(self.db.find_by_path(str(new))['width'], 80)
        self.assertEqual(self.db.counts()['active'], 2)
        self.assertEqual(self.scanner.snapshot()['errors'], 0)
        self.assertFalse(self.scanner.snapshot()['retry_paths'], 'Normal hash preemption must not become failed work')

    def test_targeted_paths_cannot_cross_an_intermediate_link_or_cloud_directory(self):
        photo = self.photo('Linked/Deep/image.jpg')
        self.db.add_root(str(self.drive))
        self.scanner._refresh_scopes()
        real_lstat = os.lstat
        ancestor = os.path.normcase(str(self.drive/'Linked'))
        for attributes, tag in ((0x400, 0xA0000003), (0x1000, 0)):
            def info(path):
                if os.path.normcase(os.fspath(path)) == ancestor:
                    return SimpleNamespace(st_mode=0o040755, st_file_attributes=attributes, st_reparse_tag=tag)
                return real_lstat(path)
            with self.subTest(attributes=attributes), patch('scanner.os.lstat', side_effect=info):
                self.assertFalse(self.scanner.accepts_path(str(photo)))
                self.scanner.start(paths=[str(photo)])
                self.wait()
        self.assertIsNone(self.db.find_by_path(str(photo)))

    def test_source_grouping_does_not_widen_an_explicit_scan_scope(self):
        photo = self.photo('Only/image.jpg')
        outside = self.photo('Other/image.jpg')
        self.db.add_root(str(photo.parent))
        self.db.remember_media_root(str(self.drive), '相机照片')
        self.scanner._refresh_scopes()
        self.assertFalse(self.scanner.accepts_path(str(outside)))
        with patch('scanner.sources.source_details', return_value=('相机照片', str(self.drive))):
            self.scanner.start(paths=[str(photo)])
            self.wait()
        self.assertEqual(self.db.find_by_path(str(photo))['source_root'], str(photo.parent))
        self.assertEqual(self.db.counts()['active'], 1)

    def test_pending_import_stays_visible_when_outer_baseline_reconciles(self):
        first = self.photo('Album/first.jpg')
        last = self.photo('Later/last.jpg')
        self.db.add_root(str(self.drive))
        created = []
        def stream(roots):
            yield str(self.drive), str(first)
            new = self.photo('Album/arrived-during-scan.jpg')
            created.append(new)
            self.assertTrue(self.scanner.start(paths=[str(new)]))
            yield str(self.drive), str(last)
        with patch.object(self.scanner, '_iter_files', side_effect=stream):
            self.scanner.start(initial=True)
            self.wait()
        row = self.db.find_by_path(str(created[0]))
        self.assertIsNotNone(row)
        self.assertFalse(row['missing'], 'The outer baseline must retain observations made by its priority targets')
        self.assertEqual(self.db.counts()['active'], 3)

    def test_pending_inaccessible_scope_protects_outer_baseline_reconciliation(self):
        first, last = self.photo('first.jpg'), self.photo('last.jpg')
        retained = self.photo('Protected/keep.jpg')
        self.db.add_root(str(self.drive))
        self.scanner._process(str(self.drive), str(retained))
        def walk(source, *, onerror, **kwargs):
            if os.path.normcase(source) == os.path.normcase(str(retained.parent)):
                onerror(PermissionError(13, 'denied fixture', str(retained.parent)))
                return
            yield str(self.drive), [], [first.name]
            self.assertTrue(self.scanner.start(paths=[str(retained.parent)]))
            yield str(self.drive), [], [last.name]
        with patch('scanner.priority_folders', return_value=[]), patch('scanner.os.walk', side_effect=walk):
            self.scanner.start(initial=True)
            self.wait()
        self.assertFalse(self.db.find_by_path(str(retained))['missing'])
        self.assertEqual(self.db.counts()['active'], 3)
        self.assertEqual(self.scanner.snapshot()['errors'], 1)
        self.assertEqual(self.scanner.snapshot()['retry_paths'], [str(retained.parent)])

    def test_missing_file_at_volume_root_is_not_confused_with_offline_volume(self):
        # Only the isolated DB is written; the synthetic root-level filename and
        # volume stat are mocked, and directory traversal is forbidden below.
        volume = self.folder.drive+os.sep
        target = os.path.join(volume, self.folder.name+'-root-boundary.jpg')
        self.db.add_root(volume)
        asset_id = self.db.insert_asset(dict(path=target, root=volume, name=os.path.basename(target),
            ext='.jpg', size=1, mtime=1, media_type='image', width=1, height=1, hash='already-hashed',
            deleted=0, favorite=0))
        volume_info = os.stat(volume)
        for online in (True, False):
            self.db.mark_missing(asset_id, False)
            def file_stat(path, **kwargs):
                if os.path.normcase(os.fspath(path)) == os.path.normcase(volume) and online:
                    return volume_info
                raise FileNotFoundError('synthetic missing path')
            with self.subTest(online=online), patch('scanner.os.stat', side_effect=file_stat), \
                    patch('scanner.os.lstat', side_effect=file_stat), \
                    patch.object(self.scanner, '_iter_files', side_effect=AssertionError('Unexpected root traversal')):
                self.scanner.start(paths=[target])
                self.wait()
            self.assertEqual(bool(self.db.get_asset(asset_id)['missing']), online, self.scanner.snapshot())
            self.assertFalse(self.scanner.snapshot()['retry_paths'], 'Missing files and offline volumes need no failure rescan')
            if online:
                self.assertEqual(self.scanner.snapshot()['errors'], 0)
            else:
                self.assertGreater(self.scanner.snapshot()['errors'], 0)

    def test_retry_paths_distinguish_access_failure_from_policy_and_reset_per_run(self):
        photo = self.photo('Allowed/keep.jpg')
        excluded = self.photo('Windows/ignored.jpg')
        self.db.add_root(str(self.drive))
        self.assertEqual(self.scanner.snapshot()['run_id'], 0)
        self.assertFalse(self.scanner.snapshot()['retry_paths'])
        def denied(source, *, onerror, **kwargs):
            onerror(PermissionError(13, 'denied fixture', str(photo.parent)))
            return iter(())
        with patch('scanner.os.walk', side_effect=denied):
            self.scanner.start(paths=[str(photo.parent), str(excluded.parent)])
            self.wait()
        self.assertEqual(self.scanner.snapshot()['run_id'], 1)
        self.assertEqual(self.scanner.snapshot()['retry_paths'], [str(photo.parent)])
        with patch.object(self.scanner, '_iter_files', side_effect=AssertionError('Unexpected traversal')):
            self.scanner.start(paths=[])
            self.wait()
        self.assertEqual(self.scanner.snapshot()['run_id'], 2)
        self.assertFalse(self.scanner.snapshot()['retry_paths'])

    def test_metadata_and_empty_hash_io_failures_remain_specific_retry_paths(self):
        metadata = self.photo('Pictures/metadata.jpg')
        hashing = self.photo('Pictures/hashing.jpg')
        unsupported = self.drive/'Pictures/unsupported.jpg'
        unsupported.write_bytes(b'not a supported photo')
        self.db.add_root(str(self.drive))
        for path in (metadata, hashing, unsupported):
            self.scanner._process(str(self.drive), str(path))
        row = self.db.find_by_path(str(metadata))
        self.db.update_asset(row['id'], width=0, height=0, hash='already-hashed')
        image_open = Image.open
        def guarded_open(path, *args, **kwargs):
            if os.fspath(path) == str(metadata):
                raise PermissionError(13, 'temporary metadata access failure')
            return image_open(path, *args, **kwargs)
        def guarded_hash(path, size, stop):
            return '' if path == str(hashing) else file_hash(path, size, stop)
        with patch('scanner.Image.open', side_effect=guarded_open), \
                patch('scanner.file_hash', side_effect=guarded_hash):
            self.scanner.start(paths=[])
            self.wait()
        self.assertEqual(set(self.scanner.snapshot()['retry_paths']), {str(metadata), str(hashing)})
        self.assertEqual(self.scanner.snapshot()['errors'], 2)

    @unittest.skipUnless(os.name == 'nt', 'Windows paths are case insensitive')
    def test_target_path_case_variants_are_one_scope_not_mutual_exclusions(self):
        photo = self.photo('Album/Original.JPG')
        self.db.add_root(str(self.drive))
        self.scanner.start(paths=[str(photo), str(photo).swapcase()])
        self.wait()
        self.assertIsNotNone(self.db.find_by_path(str(photo)))
        self.assertEqual(self.db.counts()['active'], 1)
        self.assertEqual(self.scanner.snapshot()['errors'], 0)

    def test_custom_and_resource_exclusions_do_not_admit_or_enrich_ignored_images(self):
        kept = [self.photo(path) for path in (
            'Pictures/keep.jpg', 'Pictures/Ignored-sibling/public.jpg',
            'Steam/userdata/123/760/remote/42/screenshots/user.jpg',
            'Tencent Files/123/nt_qq/nt_data/Pic/user.jpg')]
        ignored = [self.photo(path) for path in (
            'Pictures/Ignored/private.jpg', 'Steam/steamapps/common/Game/assets/icon.png',
            'Tencent Files/123/nt_qq/nt_data/Cache/generated.png')]
        self.db.add_root(str(self.drive))
        self.db.set_setting('scan_exclude_paths', json.dumps([str(ignored[0].parent)]))
        with patch('scanner.priority_folders', return_value=[]), \
                patch('scanner.probe', wraps=probe) as metadata, \
                patch('scanner.file_hash', wraps=file_hash) as hashes:
            self.scanner.start()
            self.wait()
        touched = {os.path.normcase(call.args[0]) for call in [*metadata.call_args_list, *hashes.call_args_list]}
        self.assertEqual(self.db.counts()['active'], len(kept))
        for path in kept:
            self.assertIsNotNone(self.db.find_by_path(str(path)))
        for path in ignored:
            self.assertIsNone(self.db.find_by_path(str(path)))
            self.assertNotIn(os.path.normcase(str(path)), touched)
        self.assertEqual(self.scanner.snapshot()['errors'], 0)

    def test_existing_resource_and_custom_excluded_rows_do_not_resume_metadata_or_hash(self):
        resource = self.photo('Steam/appcache/librarycache/old-resource.jpg')
        custom = self.photo('Pictures/Ignored/old-photo.jpg')
        self.db.add_root(str(self.drive))
        for path in (resource, custom):
            info = path.stat()
            self.db.insert_asset(dict(path=str(path), root=str(self.drive), name=path.name, ext='.jpg',
                size=info.st_size, mtime=info.st_mtime, width=0, height=0, hash=None,
                media_type='image', deleted=0, favorite=1, excluded=0))
        self.db.set_setting('scan_exclude_paths', json.dumps([str(custom.parent)]))
        with patch.object(self.scanner, '_iter_files', side_effect=AssertionError('Unexpected traversal')), \
                patch('scanner.probe', side_effect=AssertionError('Excluded metadata read')), \
                patch('scanner.file_hash', side_effect=AssertionError('Excluded hash read')):
            self.scanner.start(paths=[])
            self.wait()
        for path in (resource, custom):
            row = self.db.find_by_path(str(path))
            self.assertTrue(row['favorite'])
            self.assertFalse(row['deleted'])
            self.assertFalse(row['missing'])
            self.assertIsNone(row['hash'])
            self.assertTrue(path.exists())
        self.assertFalse(self.scanner.snapshot()['retry_paths'])
        self.assertEqual(self.scanner.snapshot()['errors'], 0)

    def test_changed_exclusion_interrupts_hash_and_can_readmit_without_losing_favorite(self):
        photo = self.photo('Pictures/Private/photo.jpg')
        self.db.add_root(str(self.drive))
        self.scanner._process(str(self.drive), str(photo))
        asset_id = self.db.find_by_path(str(photo))['id']
        self.db.set_favorite([asset_id], True)
        ready, proceed = threading.Event(), threading.Event()
        def hashing(path, size, stop):
            ready.set()
            proceed.wait(3)
            self.assertTrue(stop.is_set())
            return file_hash(path, size, stop)
        with patch('scanner.file_hash', side_effect=hashing):
            self.scanner.start(paths=[])
            self.assertTrue(ready.wait(3))
            self.db.set_setting('scan_exclude_paths', json.dumps([str(photo.parent)]))
            self.db.update_asset(asset_id, excluded=1)
            self.scanner.refresh_exclusions()
            proceed.set()
            self.wait()
        row = self.db.get_asset(asset_id)
        self.assertIsNone(row['hash'])
        self.assertTrue(row['favorite'])
        self.assertFalse(row['missing'])
        self.assertFalse(self.scanner.snapshot()['retry_paths'])
        self.db.set_setting('scan_exclude_paths', '[]')
        self.scanner.refresh_exclusions()
        self.scanner.start(paths=[str(photo)])
        self.wait()
        restored = self.db.get_asset(asset_id)
        self.assertFalse(restored['excluded'])
        self.assertTrue(restored['favorite'])
        self.assertTrue(restored['hash'])

    def test_exclusion_changed_during_processing_cannot_restore_or_insert_photo(self):
        self.db.add_root(str(self.drive))
        for state in ('unchanged', 'changed', 'new'):
            with self.subTest(state=state):
                self.db.set_scan_exclude_paths([])
                self.scanner.refresh_exclusions()
                photo = self.photo(f'Pictures/{state}/photo.jpg')
                original = None
                if state != 'new':
                    self.scanner._process(str(self.drive), str(photo))
                    original = self.db.find_by_path(str(photo))
                    self.db.set_favorite([original['id']], True)
                if state == 'unchanged':
                    # Policy is persisted before its cached scanner rules refresh.
                    self.db.set_scan_exclude_paths([str(photo.parent)])
                    def refreshed_source(path):
                        self.scanner.refresh_exclusions()
                        return source_details(path)
                    with patch('scanner.sources.source_details', side_effect=refreshed_source):
                        outcome = self.scanner._process(str(self.drive), str(photo))
                else:
                    if state == 'changed':
                        Image.new('RGB', (120, 90), 'blue').save(photo)
                    def exclude_during_probe(path, ext):
                        metadata = probe(path, ext)
                        self.db.set_scan_exclude_paths([str(photo.parent)])
                        self.scanner.refresh_exclusions()
                        return metadata
                    with patch('scanner.probe', side_effect=exclude_during_probe):
                        outcome = self.scanner._process(str(self.drive), str(photo))
                self.assertEqual(outcome, 'skipped')
                current = self.db.find_by_path(str(photo))
                if original:
                    self.assertTrue(current['excluded'])
                    self.assertTrue(current['favorite'])
                    self.assertEqual(current['size'], original['size'])
                else:
                    self.assertIsNone(current)
                self.assertTrue(photo.exists())

    def test_resource_filter_switch_can_be_disabled_and_bad_exclusions_are_ignored(self):
        photo = self.photo('Steam/appcache/librarycache/user-kept.jpg')
        self.db.add_root(str(self.drive))
        self.db.set_setting('scan_filter_resources', '0')
        self.db.set_setting('scan_exclude_paths', '{"invalid": "not a path list"}')
        self.scanner.start(paths=[str(photo)])
        self.wait()
        self.assertIsNotNone(self.db.find_by_path(str(photo)))
        self.assertFalse(self.scanner.is_excluded(str(photo)))
        self.assertEqual(self.scanner.snapshot()['errors'], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
