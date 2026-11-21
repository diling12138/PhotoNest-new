"""File actions use an isolated fake bin; never modify the user's library."""
import os
from pathlib import Path
from types import SimpleNamespace, MethodType
import tempfile
import ctypes
import sys
import struct
import stat
import time
from contextlib import nullcontext
import unittest
from unittest.mock import patch
from PIL import Image
from app import PhotoNestApp
from db import DB
from scanner import Scanner
from sources import source_for
import desktop
import thumbs


class PhotoActionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="PhotoNest-actions-")
        self.root = Path(self.temp.name)
        self.original = self.root / "Tencent Files" / "nt_qq" / "中文, 照片.jpg"
        self.original.parent.mkdir(parents=True)
        Image.new("RGB", (1200, 800), "green").save(self.original)
        self.db = DB(str(self.root / "data/library.db"))
        self.db.add_root(str(self.root))
        self.scanner = Scanner(self.db, str(self.root / "cache"))
        self.scanner._process(str(self.root), str(self.original))
        self.errors = []
        self.app = SimpleNamespace(db=self.db, cache_dir=str(self.root / "cache"),
            data_dir=str(self.root / "data"), lb_window=None, lb=None,
            _file_busy=False,
            _file_errors=lambda e: self.errors.extend(e), _refresh_after_file_action=lambda: None,
            _write_album_json=lambda _: None)
        for name in ("_delete_photos", "_restore_photos", "_purge_assets", "_change_photo_file"):
            setattr(self.app, name, MethodType(getattr(PhotoNestApp, name), self.app))
        def run(ids, mode):
            for asset_id in ids:
                try:
                    self.app._change_photo_file(self.db.get_asset(asset_id), mode)
                except Exception as error:
                    self.errors.append(str(error))
        self.app._run_photo_files = run

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_failed_recycle_keeps_original_and_active_record(self):
        with patch("desktop.recycle", side_effect=OSError("unavailable")):
            self.app._delete_photos([1], confirm=False)
        self.assertTrue(self.original.exists())
        self.assertEqual(self.db.get_asset(1)["deleted"], 0)
        self.assertTrue(self.errors)

    def test_delete_restore_and_purge_only_after_shell_success(self):
        target = self.root / "isolated-bin.jpg"
        def recycle(path, **kwargs):
            os.replace(path, target)
            return str(target)
        with patch("desktop.recycle", side_effect=recycle):
            self.app._delete_photos([1], confirm=False)
        self.assertFalse(self.original.exists())
        self.assertEqual(self.db.get_asset(1)["recycle_token"], str(target))
        with patch("desktop.restore", side_effect=lambda token, path: os.replace(token, path)):
            self.app._restore_photos([1])
        self.assertTrue(self.original.exists())
        self.assertFalse(self.db.get_asset(1)["deleted"])
        with patch("desktop.recycle", side_effect=recycle):
            self.app._delete_photos([1], confirm=False)
        with patch("desktop.purge", side_effect=OSError("locked")):
            self.app._purge_assets([1])
        self.assertIsNotNone(self.db.get_asset(1))
        self.assertTrue(target.exists())
        with patch("desktop.purge", side_effect=lambda token: Path(token).unlink()) as purge:
            self.app._purge_assets([1])
        purge.assert_called_once_with(str(target))
        self.assertIsNone(self.db.get_asset(1))

    def test_native_shell_receives_unicode_path_and_not_command_text(self):
        with patch("desktop.shell.SHParseDisplayName", return_value=([b"file-pidl"], 0)) as parse, \
                patch("desktop.shell.SHOpenFolderAndSelectItems") as show:
            desktop.reveal(str(self.original))
        parse.assert_called_once_with(str(self.original.resolve()), 0)
        show.assert_called_once_with([b"file-pidl"], (), 0)
        with patch("desktop.shell.ShellExecuteEx") as launch:
            desktop.open_file(str(self.original))
        self.assertEqual(launch.call_args.kwargs["lpFile"], str(self.original.resolve()))
        self.assertEqual(launch.call_args.kwargs["lpDirectory"], str(self.original.parent.resolve()))

    def test_sources_and_lazy_thumbnail(self):
        self.assertEqual(source_for(str(self.original)), "QQ")
        self.assertEqual(source_for(r"D:\XWeChat_Files\user\msg\a.png"), "微信")
        self.assertEqual(source_for(r"C:\Users\A\Downloads\a.jpg"), "浏览器下载")
        self.assertEqual(self.db.query_assets(source_app="QQ")["total"], 1)
        self.assertEqual(self.db.query_assets(source_app="微信")["total"], 0)
        self.assertFalse(Path(thumbs.thumb_path(self.app.cache_dir, 1)).exists())
        self.assertTrue(thumbs.ensure(str(self.original), 1, self.app.cache_dir, "thumb"))
        self.assertFalse(Path(thumbs.preview_path(self.app.cache_dir, 1)).exists())

    def test_frozen_shell_resets_and_restores_runtime_search_paths(self):
        before = os.environ.get("PATH", "")
        kernel = ctypes.WinDLL("kernel32")
        kernel.GetDllDirectoryW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p]
        old = ctypes.create_unicode_buffer(32768)
        kernel.GetDllDirectoryW(len(old), old)
        with patch.object(sys, "_MEIPASS", str(self.root), create=True), \
                patch.dict(os.environ, {"PATH": str(self.root)+os.pathsep+before}):
            with self.assertRaisesRegex(RuntimeError, "fixture"):
                with desktop._com():
                    current = ctypes.create_unicode_buffer(32768)
                    kernel.GetDllDirectoryW(len(current), current)
                    self.assertEqual(current.value, "")
                    self.assertEqual(os.environ["PATH"], before)
                    raise RuntimeError("fixture")
            self.assertEqual(os.environ["PATH"], str(self.root)+os.pathsep+before)
        restored = ctypes.create_unicode_buffer(32768)
        kernel.GetDllDirectoryW(len(restored), restored)
        self.assertEqual(restored.value, old.value)

    def test_modern_shell_flags_and_success_hresult_are_not_rejected(self):
        progress = desktop._Progress(True)
        self.assertEqual(progress.PreDeleteItem(0x202, None), 0)
        created = SimpleNamespace(GetDisplayName=lambda _: "recycle-bin-item")
        progress.PostDeleteItem(0x202, None, 0x0027000c, created)
        self.assertTrue(progress.completed)
        self.assertEqual(progress.token, "recycle-bin-item")

    def test_incomplete_shell_result_is_not_success_for_restore_or_purge(self):
        def incomplete(progress):
            progress.PostDeleteItem(0, None, 0x00270008, None)
            return progress
        operation = SimpleNamespace(SetOperationFlags=lambda flags: None,
            DeleteItem=lambda *args: None, MoveItem=lambda *args: None,
            PerformOperations=lambda: None, GetAnyOperationsAborted=lambda: False)
        with patch('desktop.pythoncom.CoCreateInstance', return_value=operation), \
                patch('desktop.pythoncom.WrapObject', side_effect=lambda progress, iid: incomplete(progress)), \
                patch('desktop._item'):
            for kwargs in ({}, {'destination': str(self.root)}):
                with self.subTest(kwargs=kwargs), self.assertRaises(OSError):
                    desktop._operation(None, **kwargs)

    def test_shell_can_request_elevation_for_one_protected_file(self):
        flags = []
        operation = SimpleNamespace(SetOperationFlags=flags.append,
            DeleteItem=lambda *args: None, MoveItem=lambda *args: None,
            PerformOperations=lambda: None, GetAnyOperationsAborted=lambda: False)
        def complete(progress, iid):
            progress.PostDeleteItem(0, None, 0, None)
            return progress
        with patch('desktop.pythoncom.CoCreateInstance', return_value=operation), \
                patch('desktop.pythoncom.WrapObject', side_effect=complete):
            desktop._operation(None, recycle=True)
        self.assertTrue(flags[0] & 0x00040000, 'Allow the native UAC prompt instead of requiring an elevated app')
        self.assertFalse(flags[0] & 0x10000000, 'Do not require elevation for ordinary photos')

    def test_recycled_item_matches_filesystem_name_despite_namespace_name(self):
        token = r'C:\$Recycle.Bin\S-1-5-21-fixture\$Rphoto.jpg'
        item = SimpleNamespace(GetDisplayName=lambda flag: (
            token.upper() if flag == desktop.shellcon.SIGDN_FILESYSPATH else 'shell:namespace-item'))
        folder = SimpleNamespace(EnumObjects=lambda *args: [[b'child']])
        root = SimpleNamespace(BindToObject=lambda *args: folder)
        with patch('desktop.shell.SHQueryRecycleBin'), \
                patch('desktop.shell.SHGetSpecialFolderLocation', return_value=[b'bin']), \
                patch('desktop.shell.SHGetDesktopFolder', return_value=root), \
                patch('desktop.shell.SHCreateItemFromIDList', return_value=item):
            self.assertIs(desktop._recycled_item(token), item)

    def test_purge_already_removed_photo_clears_stale_record_but_denied_access_does_not(self):
        import win32api, win32security
        handle = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
        try:
            sid = win32security.ConvertSidToStringSid(win32security.GetTokenInformation(handle, win32security.TokenUser)[0])
        finally:
            handle.Close()
        token = os.path.join(self.original.drive+os.sep, '$Recycle.Bin', sid, '$Rmissing.jpg')
        def missing(path):
            if os.fspath(path) == token:
                raise FileNotFoundError('missing fixture')
            return SimpleNamespace()
        def denied(path):
            if os.fspath(path) == token:
                raise PermissionError('denied fixture')
            return SimpleNamespace()
        with patch('desktop._com', return_value=nullcontext()), \
                patch('desktop._recycled_item', return_value=None), \
                patch('desktop.os.stat', side_effect=missing), \
                patch('desktop._operation') as operation:
            desktop.purge(token)
            operation.assert_not_called()
        with patch('desktop._com', return_value=nullcontext()), \
                patch('desktop._recycled_item', return_value=None), \
                patch('desktop.os.stat', side_effect=denied):
            with self.assertRaises(PermissionError):
                desktop.purge(token)

    def test_fallback_receipt_requires_recent_valid_record_and_complete_photo(self):
        folder = self.root/'isolated-bin'
        folder.mkdir()
        record, payload = folder/'$Iphoto.jpg', folder/'$Rphoto.jpg'
        size = self.original.stat().st_size
        started = time.time()-1
        original = (str(self.original)+'\0').encode('utf-16-le')
        def write_record(*, version=2, deleted=None):
            timestamp = int(((time.time() if deleted is None else deleted)+11644473600)*10000000)
            record.write_bytes(struct.pack('<QQQI', version, size, timestamp, len(original)//2)+original)
        with patch('desktop._recycle_bin_folders', return_value=[str(folder)]):
            write_record()
            self.assertIsNone(desktop._recycled_token(str(self.original), size, started),
                              'Missing recycled photo must not count as a valid receipt')
            payload.write_bytes(b'x')
            self.assertIsNone(desktop._recycled_token(str(self.original), size, started))
            payload.write_bytes(self.original.read_bytes())
            write_record(deleted=started-60)
            self.assertIsNone(desktop._recycled_token(str(self.original), size, started),
                              'An older deletion must not confirm the current operation')
            write_record(version=99)
            self.assertIsNone(desktop._recycled_token(str(self.original), size, started))
            write_record()
            self.assertEqual(desktop._recycled_token(str(self.original), size, started), str(payload))

    def test_shell_without_receipt_restores_original_bytes(self):
        before = self.original.read_bytes()
        def bad_shell(*args, **kwargs):
            self.original.unlink()
            raise OSError("Shell provided no recoverable item")
        with patch("desktop._operation", side_effect=bad_shell), patch("desktop._item"):
            with self.assertRaisesRegex(OSError, "原文件已保留或恢复"):
                desktop.recycle(str(self.original), recovery_dir=str(self.root/'data/recovery'))
        self.assertEqual(self.original.read_bytes(), before)
        self.assertFalse(list(self.original.parent.glob(".photonest-*.recovery")))
        self.assertFalse(list((self.root/'data/recovery').iterdir()))

    def test_recycle_does_not_create_safety_files_in_protected_photo_folder(self):
        real_copy = desktop.shutil.copy2
        recycled = self.root/'windows-bin-fixture.jpg'
        def protected_copy(source, destination, *args, **kwargs):
            if Path(destination).parent == self.original.parent:
                raise PermissionError('Protected photo folder rejects new safety files')
            return real_copy(source, destination, *args, **kwargs)
        def shell_recycle(item, **kwargs):
            os.replace(self.original, recycled)
            return 'verified-bin-item'
        with patch('desktop.os.link', side_effect=PermissionError('Protected photo folder')) as link, \
                patch('desktop.shutil.copy2', side_effect=protected_copy), \
                patch('desktop._com', return_value=nullcontext()), patch('desktop._item'), \
                patch('desktop._operation', side_effect=shell_recycle):
            self.app._change_photo_file(self.db.get_asset(1), 'recycle')
        link.assert_not_called()
        self.assertTrue(recycled.exists())
        self.assertFalse(self.original.exists())
        self.assertEqual(self.db.get_asset(1)['recycle_token'], 'verified-bin-item')
        self.assertFalse(list(self.original.parent.glob('.photonest-*')))

    def test_failed_recycle_restores_without_same_volume_rename(self):
        before = self.original.read_bytes()
        original_time = self.original.stat().st_mtime_ns
        def bad_shell(*args, **kwargs):
            self.original.unlink()
            raise OSError('No valid recycle receipt')
        with patch('desktop._com', return_value=nullcontext()), patch('desktop._item'), \
                patch('desktop._operation', side_effect=bad_shell), \
                patch('desktop.os.rename', side_effect=OSError(17, 'different volume')):
            with self.assertRaisesRegex(OSError, '原文件已保留或恢复'):
                desktop.recycle(str(self.original), recovery_dir=str(self.root/'data/recovery'))
        self.assertEqual(self.original.read_bytes(), before)
        self.assertEqual(self.original.stat().st_mtime_ns, original_time)
        self.assertFalse(list((self.root/'data/recovery').iterdir()))

    def test_restore_denied_keeps_recovery_copy_and_original_location_record(self):
        before = self.original.read_bytes()
        def bad_shell(*args, **kwargs):
            self.original.unlink()
            raise OSError('No valid recycle receipt')
        with patch('desktop._com', return_value=nullcontext()), patch('desktop._item'), \
                patch('desktop._operation', side_effect=bad_shell), \
                patch('desktop._restore_recovery', side_effect=PermissionError('source folder denied')):
            with self.assertRaisesRegex(OSError, '安全副本保留于'):
                self.app._change_photo_file(self.db.get_asset(1), 'recycle')
        copies = list((self.root/'data/recovery').glob('*.recovery'))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), before)
        record = __import__('json').loads(Path(str(copies[0])+'.json').read_text(encoding='utf-8'))
        self.assertEqual(record['original_path'], str(self.original))
        self.assertFalse(self.db.get_asset(1)['deleted'])

    def test_backup_failure_never_starts_windows_delete(self):
        before = self.original.read_bytes()
        with patch('desktop.shutil.copy2', side_effect=PermissionError('data directory denied')), \
                patch('desktop._operation') as operation:
            with self.assertRaises(PermissionError):
                self.app._change_photo_file(self.db.get_asset(1), 'recycle')
        operation.assert_not_called()
        self.assertEqual(self.original.read_bytes(), before)
        self.assertFalse(self.db.get_asset(1)['deleted'])


class PurgeVerificationTests(unittest.TestCase):
    """Exercise the real purge/_operation chain with an isolated simulated Shell."""
    def _purge(self, result, *, removed, namespace_gone=True, aborted=False, denied=False):
        token = r'C:\$Recycle.Bin\S-1-5-21-fixture\$Rowned.jpg'
        item = SimpleNamespace(GetDisplayName=lambda flag: token)
        state = {'performed': False, 'sink': None, 'item': None}
        def queue_delete(source, sink):
            state.update(item=source, sink=sink)
        def perform():
            state['performed'] = True
            state['sink'].PostDeleteItem(0, item, result, None)
        operation = SimpleNamespace(SetOperationFlags=lambda flags: None,
            DeleteItem=queue_delete, PerformOperations=perform,
            GetAnyOperationsAborted=lambda: aborted)
        def lookup(candidate):
            self.assertEqual(candidate, token)
            return None if state['performed'] and namespace_gone else item
        def info(path):
            if os.path.normcase(os.fspath(path)) == os.path.normcase(token) and state['performed']:
                if denied:
                    raise PermissionError('owned fixture denies verification')
                if removed:
                    raise FileNotFoundError('owned fixture was deleted')
            return SimpleNamespace(st_mode=stat.S_IFREG, st_size=20)
        with patch('desktop._com', return_value=nullcontext()), \
                patch('desktop._current_user_sid', return_value='S-1-5-21-fixture'), \
                patch('desktop._recycled_item', side_effect=lookup), \
                patch('desktop.pythoncom.CoCreateInstance', return_value=operation), \
                patch('desktop.pythoncom.WrapObject', side_effect=lambda progress, iid: progress), \
                patch('desktop.os.stat', side_effect=info), \
                patch('desktop.os.unlink', side_effect=AssertionError('Purge must use the exact Shell item')), \
                patch('desktop.shell.SHEmptyRecycleBin', side_effect=AssertionError('Never empty unrelated items')):
            desktop.purge(token)
        self.assertIs(state['item'], item, 'Delete only the item matched by the recorded token')

    def test_dont_process_children_counts_as_purged_only_after_verified_removal(self):
        for namespace_gone in (True, False):
            with self.subTest(namespace_gone=namespace_gone):
                self._purge(0x00270008, removed=True, namespace_gone=namespace_gone)

    def test_dont_process_children_with_existing_photo_still_reports_failure(self):
        with self.assertRaises(OSError):
            self._purge(0x00270008, removed=False, namespace_gone=False)

    def test_success_callback_and_empty_namespace_do_not_hide_remaining_photo(self):
        with self.assertRaises(OSError):
            self._purge(0, removed=False)

    def test_aborted_or_failed_operations_are_not_overridden_by_absence(self):
        for result, aborted in ((0x00270008, True), (-2147024891, False)):
            with self.subTest(result=result, aborted=aborted), self.assertRaises(OSError):
                self._purge(result, removed=True, aborted=aborted)

    def test_verification_access_denied_preserves_record(self):
        with self.assertRaises(OSError):
            self._purge(0, removed=True, denied=True)

    def test_unmatched_token_never_deletes_a_normal_file(self):
        with patch('desktop._com', return_value=nullcontext()), \
                patch('desktop._recycled_item', return_value=None), \
                patch('desktop._operation') as operation:
            with self.assertRaises(FileNotFoundError):
                desktop.purge(r'C:\Users\photo-outside-recycle-bin.jpg')
        operation.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
