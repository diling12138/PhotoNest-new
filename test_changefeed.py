"""Protocol tests use synthetic journal bytes; native watch touches owned files only."""
import os
from pathlib import Path
import queue
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import changefeed as feed


def record(name='照片.jpg', *, major=2, file_id=42, parent_id=9, usn=100, reason=0x100, is_dir=False):
    encoded = name.encode('utf-16-le', 'surrogatepass')
    header = 60 if major == 2 else 76
    length = (header+len(encoded)+7)//8*8
    id_size = 8 if major == 2 else 16
    raw = struct.pack('<IHH', length, major, 0)
    raw += file_id.to_bytes(id_size, 'little')+parent_id.to_bytes(id_size, 'little')
    raw += struct.pack('<qQIIIIHH', usn, 0, reason, 0, 0, 0x10 if is_dir else 0, len(encoded), header)
    return (raw+encoded).ljust(length, b'\0')


def snapshot(**updates):
    value = dict(volume='C:\\', volume_identity='volume-fixture', device=123,
                 journal_id=77, first_usn=0, lowest_valid_usn=0, next_usn=100)
    value.update(updates)
    return value


class FakeVolume:
    def __init__(self):
        self.states = [snapshot(next_usn=200), snapshot(next_usn=300)]
        self.reads = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def snapshot(self):
        return self.states.pop(0)

    def read(self, cursor, journal_id):
        self.reads.append((cursor, journal_id))
        return struct.pack('<q', 250)+record(usn=100)+record(usn=230)


class JournalTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows USN journal API required')
    def test_native_existing_journal_tracks_and_resolves_owned_file(self):
        with tempfile.TemporaryDirectory(prefix='PhotoNest-usn-owned-') as folder:
            root = Path(folder)
            journal = feed.Journal(root.drive+os.sep)
            try:
                saved = journal.snapshot()
                owned = root/'owned-usn-fixture.bin'
                owned.write_bytes(b'PhotoNest-owned-USN-test')
                info = owned.stat()
                self.assertEqual(saved['device'], info.st_dev)
                events, current = journal.changes(saved)
                self.assertGreater(current['next_usn'], saved['next_usn'])
                matches = [event for event in events if event['file_id'] == info.st_ino]
                self.assertTrue(matches, 'The bounded USN batch must include the test-owned file ID')
                resolved = journal.resolve(matches[-1])
                self.assertIsNotNone(resolved, 'The test-owned file must resolve while it still exists')
                self.assertEqual(os.path.normcase(resolved), os.path.normcase(str(owned.resolve())))
            except feed.JournalGap:
                raise  # A malformed stream or lost cursor must not be hidden as an unavailable feature.
            except feed.JournalUnavailable as error:
                code = getattr(error, 'winerror', None)
                if code in (1, 5, 50, 1178, 1179, 1314):
                    self.skipTest(f'Existing Windows USN journal unavailable (Windows {code}); no journal was created')
                raise

    def test_v2_v3_ids_unicode_and_directory_flags(self):
        large_id = (1 << 120)+57
        data = struct.pack('<q', 400)+record(name='中文😀.gif')+record(
            name='聊天目录', major=3, file_id=large_id, parent_id=large_id-1, usn=220, is_dir=True)
        cursor, events = feed.parse_usn_buffer(data)
        self.assertEqual(cursor, 400)
        self.assertEqual(events[0]['name'], '中文😀.gif')
        self.assertEqual(events[0]['reason'], 0x100)
        self.assertEqual(events[1]['file_id'], large_id)
        self.assertEqual(events[1]['parent_id'], large_id-1)
        self.assertTrue(events[1]['is_dir'])

    def test_invalid_and_unknown_layouts_require_reconciliation(self):
        bad_length = bytearray(record())
        struct.pack_into('<I', bad_length, 0, 0)
        bad_name = bytearray(record())
        struct.pack_into('<H', bad_name, 56, 3)
        unknown = bytearray(record())
        struct.pack_into('<H', unknown, 4, 4)
        for data in (b'', struct.pack('<q', 200)+bytes(bad_length),
                     struct.pack('<q', 200)+bytes(bad_name), struct.pack('<q', 200)+bytes(unknown),
                     struct.pack('<q', 200)+record()[:-1]):
            with self.subTest(data=data[:16]), self.assertRaises(feed.JournalGap):
                feed.parse_usn_buffer(data)

    def test_chasing_hot_volume_stops_at_original_watermark(self):
        volume = FakeVolume()
        with patch('changefeed._Volume', return_value=volume):
            events, state = feed.Journal('C:\\').changes(snapshot())
        self.assertEqual([event['usn'] for event in events], [100])
        self.assertEqual(state['next_usn'], 200)
        self.assertEqual(volume.reads, [(100, 77)])
        self.assertTrue(volume.closed)

    def test_explicit_target_does_not_advance_past_requested_cursor(self):
        volume = FakeVolume()
        with patch('changefeed._Volume', return_value=volume):
            events, state = feed.Journal('C:\\').changes(snapshot(), target=snapshot(next_usn=150))
        self.assertEqual(state['next_usn'], 150)
        self.assertEqual(len(events), 1)

    def test_recreated_journal_or_recycled_history_rejects_saved_cursor(self):
        for current in (snapshot(next_usn=200, journal_id=88), snapshot(next_usn=200, first_usn=101),
                        snapshot(next_usn=200, lowest_valid_usn=101), snapshot(next_usn=200, device=456)):
            volume = FakeVolume()
            volume.states = [current]
            with self.subTest(current=current), patch('changefeed._Volume', return_value=volume):
                with self.assertRaises(feed.JournalGap):
                    feed.Journal('C:\\').changes(snapshot())
            self.assertFalse(volume.reads)

    def test_permission_failure_and_cancellation_never_advance_cursor(self):
        saved = snapshot()
        with patch('changefeed._Volume', side_effect=PermissionError('denied')):
            with self.assertRaises(feed.JournalUnavailable):
                feed.Journal('C:\\').snapshot()
        stop = threading.Event()
        stop.set()
        with patch('changefeed._Volume') as volume:
            with self.assertRaises(feed.JournalCancelled):
                feed.Journal('C:\\').changes(saved, stop=stop)
        volume.assert_not_called()
        self.assertEqual(saved['next_usn'], 100)

    def test_gap_during_read_or_journal_replacement_is_not_committed(self):
        volume = FakeVolume()
        volume.states[-1] = snapshot(next_usn=300, journal_id=99)
        with patch('changefeed._Volume', return_value=volume):
            with self.assertRaises(feed.JournalGap):
                feed.Journal('C:\\').changes(snapshot())
        volume = FakeVolume()
        volume.read = lambda *args: struct.pack('<q', 100)
        with patch('changefeed._Volume', return_value=volume):
            with self.assertRaises(feed.JournalGap):
                feed.Journal('C:\\').changes(snapshot())

    def test_long_offline_batch_is_bounded_without_advancing_saved_cursor(self):
        saved = snapshot()
        for setting, value in (('MAX_USN_EVENTS', 0), ('MAX_USN_BYTES', 8)):
            with self.subTest(setting=setting), patch('changefeed._Volume', return_value=FakeVolume()), \
                    patch('changefeed.'+setting, value):
                with self.assertRaises(feed.JournalGap):
                    feed.Journal('C:\\').changes(saved)
        self.assertEqual(saved['next_usn'], 100)


def notification(name, action=1, step=0):
    encoded = name.encode('utf-16-le')
    return struct.pack('<III', step, action, len(encoded))+encoded


class WatcherTests(unittest.TestCase):
    def test_nested_changes_rename_and_overflow_are_delivered_as_hints(self):
        root = os.path.abspath('fixture-root')
        old = notification(os.path.join('聊天', 'old.gif'), 4)
        step = (len(old)+3)//4*4
        old = notification(os.path.join('聊天', 'old.gif'), 4, step).ljust(step, b'\0')
        events = feed.parse_notify_buffer(old+notification(os.path.join('聊天', 'new.gif'), 5), root)
        self.assertEqual([event['action'] for event in events], ['renamed_old', 'renamed_new'])
        self.assertEqual(events[1]['path'], os.path.normcase(os.path.join(root, '聊天', 'new.gif')))
        self.assertEqual(feed.parse_notify_buffer(b'', root)[0]['rescan'], True)

    def test_corrupt_notification_or_outside_path_needs_rescan(self):
        root = os.path.abspath('fixture-root')
        for data in (b'x', notification('../outside.jpg'), notification('a.jpg', 99), notification('a.jpg')[:-1]):
            with self.subTest(data=data), self.assertRaises(feed.JournalGap):
                feed.parse_notify_buffer(data, root)

    def test_overlapping_roots_share_one_reader_and_close_before_start_is_safe(self):
        root = os.path.abspath('fixture-root')
        watcher = feed.NativeDirectoryWatcher([root, os.path.join(root, 'a'), root], lambda event: None)
        self.assertEqual(watcher.paths, [os.path.normcase(root)])
        watcher.close()
        watcher.close()
        watcher.start()
        self.assertFalse(watcher._threads)

    def test_unavailable_directory_reports_rescan_instead_of_silently_stopping(self):
        events = []
        watcher = feed.NativeDirectoryWatcher([os.path.abspath('not-a-real-watch-root')], events.append)
        with patch('changefeed._kernel', side_effect=feed.JournalUnavailable('permission denied')):
            watcher.start()
            self.assertTrue(watcher.ready.wait(2))
            watcher.close()
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]['rescan'])
        self.assertEqual(events[0]['action'], 'unavailable')
        self.assertTrue(watcher.errors)

    @unittest.skipUnless(os.name == 'nt', 'Windows directory notification API required')
    def test_native_nested_notifications_and_cancellable_close(self):
        with tempfile.TemporaryDirectory(prefix='PhotoNest-watch-owned-') as folder:
            root = Path(folder)
            nested = root/'嵌套目录'
            nested.mkdir()
            events = queue.Queue()
            watcher = feed.NativeDirectoryWatcher([str(root), str(nested)], events.put)
            try:
                watcher.start()
                self.assertTrue(watcher.ready.wait(3))
                photo = nested/'临时照片.gif'
                photo.write_bytes(b'owned-test-data')
                expected = os.path.normcase(str(photo))
                observed = []
                deadline = time.monotonic()+5
                while time.monotonic() < deadline:
                    try:
                        event = events.get(timeout=.2)
                    except queue.Empty:
                        continue
                    observed.append(event)
                    if event['path'] == expected and not event['rescan']:
                        break
                self.assertTrue(any(event['path'] == expected and not event['rescan'] for event in observed), observed)
            finally:
                started = time.monotonic()
                watcher.close()
                self.assertLess(time.monotonic()-started, 3)
                self.assertTrue(all(not thread.is_alive() for thread in watcher._threads))


if __name__ == '__main__':
    unittest.main(verbosity=2)
