"""Read-only Windows change feeds. No journal creation or filesystem mutation.

USN batches stop at a fixed watermark; directory notifications are hints that
must be reconciled after overflow. Parsing helpers also work outside Windows.
"""
from __future__ import annotations

import ctypes
from contextlib import contextmanager
from functools import lru_cache
import os
import struct
import threading
import time


class JournalUnavailable(OSError):
    """The volume/journal cannot currently be read; use a scoped scan instead."""


class JournalGap(JournalUnavailable):
    """A saved cursor, record layout, or journal identity cannot be trusted."""


class JournalCancelled(JournalUnavailable):
    """The caller cancelled; no cursor from this batch should be committed."""


QUERY_USN = 0x000900F4
READ_USN = 0x000900BB
SHARE_ALL = 7
BACKUP_SEMANTICS = 0x02000000
OVERLAPPED_FLAG = 0x40000000
INVALID_HANDLE = ctypes.c_void_p(-1).value
# Long offline periods may contain mostly unrelated system activity. Bound the
# in-memory batch and let the caller reconcile its media scopes instead.
MAX_USN_EVENTS = 100000
MAX_USN_BYTES = 32*1024*1024


def parse_usn_buffer(data):
    """Return (next USN, raw events) from an FSCTL_READ_USN_JOURNAL buffer."""
    if len(data) < 8:
        raise JournalGap("USN 返回数据缺少游标")
    next_usn = struct.unpack_from('<q', data)[0]
    if next_usn < 0:
        raise JournalGap("USN 游标无效")
    events, offset = [], 8
    while offset < len(data):
        if len(data)-offset < 8:
            raise JournalGap("USN 记录头被截断")
        length, major, minor = struct.unpack_from('<IHH', data, offset)
        header = {2: 60, 3: 76}.get(major)
        if header is None:
            raise JournalGap(f"不支持 USN 记录版本 {major}.{minor}")
        if length < header or offset+length > len(data):
            raise JournalGap("USN 记录长度无效")
        id_size = 8 if major == 2 else 16
        file_id = int.from_bytes(data[offset+8:offset+8+id_size], 'little')
        parent_id = int.from_bytes(data[offset+8+id_size:offset+8+2*id_size], 'little')
        field = offset+8+2*id_size
        usn = struct.unpack_from('<q', data, field)[0]
        reason, _, _, attributes, name_length, name_offset = struct.unpack_from('<IIIIHH', data, field+16)
        if (name_length % 2 or name_offset < header or name_offset+name_length > length
                or usn < 0 or usn >= next_usn):
            raise JournalGap("USN 文件名或记录编号无效")
        name = data[offset+name_offset:offset+name_offset+name_length].decode('utf-16-le', 'surrogatepass')
        if '\0' in name or '\\' in name or '/' in name:
            raise JournalGap("USN 文件名包含非法分隔符")
        events.append(dict(file_id=file_id, parent_id=parent_id, name=name, usn=usn,
                           reason=reason, is_dir=bool(attributes & 0x10), major_version=major))
        offset += length
    return next_usn, events


class _Overlapped(ctypes.Structure):
    _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                ('Offset', ctypes.c_uint32), ('OffsetHigh', ctypes.c_uint32), ('hEvent', ctypes.c_void_p)]


class _FileIdUnion(ctypes.Union):
    _fields_ = [('FileId', ctypes.c_longlong), ('ExtendedFileId', ctypes.c_ubyte*16)]


class _FileIdDescriptor(ctypes.Structure):
    _fields_ = [('dwSize', ctypes.c_uint32), ('Type', ctypes.c_uint32), ('Id', _FileIdUnion)]


@lru_cache(maxsize=1)
def _kernel():
    if os.name != 'nt':
        raise JournalUnavailable("此平台没有 Windows 文件变更接口")
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    pointer = ctypes.c_void_p
    dword = ctypes.c_uint32
    definitions = {
        'CreateFileW': (pointer, [ctypes.c_wchar_p, dword, dword, pointer, dword, dword, pointer]),
        'CloseHandle': (ctypes.c_int, [pointer]),
        'DeviceIoControl': (ctypes.c_int, [pointer, dword, pointer, dword, pointer, dword, pointer, pointer]),
        'GetVolumePathNameW': (ctypes.c_int, [ctypes.c_wchar_p, ctypes.c_wchar_p, dword]),
        'GetVolumeNameForVolumeMountPointW': (ctypes.c_int, [ctypes.c_wchar_p, ctypes.c_wchar_p, dword]),
        'OpenFileById': (pointer, [pointer, pointer, dword, dword, pointer, dword]),
        'GetFinalPathNameByHandleW': (dword, [pointer, ctypes.c_wchar_p, dword, dword]),
        'CreateEventW': (pointer, [pointer, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]),
        'ResetEvent': (ctypes.c_int, [pointer]),
        'ReadDirectoryChangesW': (ctypes.c_int, [pointer, pointer, dword, ctypes.c_int, dword, pointer, pointer, pointer]),
        'WaitForSingleObject': (dword, [pointer, dword]),
        'GetOverlappedResult': (ctypes.c_int, [pointer, pointer, pointer, ctypes.c_int]),
        'CancelIoEx': (ctypes.c_int, [pointer, pointer]),
    }
    for name, (result, arguments) in definitions.items():
        function = getattr(kernel, name)
        function.restype, function.argtypes = result, arguments
    return kernel


def _native_error(operation):
    code = ctypes.get_last_error()
    kind = JournalGap if code == 1181 else JournalUnavailable  # ERROR_JOURNAL_ENTRY_DELETED
    error = kind(f"{operation}失败（Windows {code}）")
    error.winerror = code
    return error


class _Volume:
    def __init__(self, path):
        self.kernel = _kernel()
        root, identity = ctypes.create_unicode_buffer(32768), ctypes.create_unicode_buffer(128)
        if not self.kernel.GetVolumePathNameW(path, root, len(root)):
            raise _native_error('定位卷')
        self.root = root.value
        if not self.kernel.GetVolumeNameForVolumeMountPointW(self.root, identity, len(identity)):
            raise _native_error('读取卷身份')
        self.identity = identity.value.casefold().rstrip('\\')
        self.device = int(os.stat(self.root).st_dev)
        self.handle = self.kernel.CreateFileW(identity.value.rstrip('\\'), 0x80000000,
            SHARE_ALL, None, 3, 0, None)
        if self.handle in (None, INVALID_HANDLE):
            raise _native_error('打开只读卷句柄')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.kernel.CloseHandle(self.handle)

    def ioctl(self, code, payload=b'', size=1024*1024):
        output, returned = ctypes.create_string_buffer(size), ctypes.c_uint32()
        input_buffer = ctypes.create_string_buffer(payload) if payload else None
        if not self.kernel.DeviceIoControl(self.handle, code, input_buffer, len(payload),
                output, len(output), ctypes.byref(returned), None):
            raise _native_error('读取 USN 日志')
        return output.raw[:returned.value]

    def snapshot(self):
        data = self.ioctl(QUERY_USN, size=128)
        if len(data) < 56:
            raise JournalGap("USN 日志信息被截断")
        journal_id, first, next_usn, lowest, maximum, capacity, allocation = struct.unpack_from('<QqqqqQQ', data)
        if first < 0 or next_usn < first or lowest < 0:
            raise JournalGap("USN 日志边界无效")
        return dict(volume=self.root, volume_identity=self.identity, device=self.device,
                    journal_id=journal_id, first_usn=first, next_usn=next_usn,
                    lowest_valid_usn=lowest, max_usn=maximum, capacity=capacity)

    def read(self, cursor, journal_id):
        # V1 requests only V2/V3 records; timeout/bytes-to-wait are zero so reads
        # never wait for future writes on a busy or idle volume.
        payload = struct.pack('<qIIQQQHH4x', cursor, 0xffffffff, 0, 0, 0, journal_id, 2, 3)
        return self.ioctl(READ_USN, payload)

    def resolve(self, event):
        value = int(event['file_id'])
        if not 0 <= value < 1 << 128:
            return None
        descriptor = _FileIdDescriptor()
        descriptor.dwSize = ctypes.sizeof(descriptor)
        if event.get('major_version') == 3 or value >= 1 << 64:
            descriptor.Type = 2
            descriptor.Id.ExtendedFileId[:] = value.to_bytes(16, 'little')
        else:
            descriptor.Type = 0
            descriptor.Id.FileId = ctypes.c_longlong(value).value
        handle = self.kernel.OpenFileById(self.handle, ctypes.byref(descriptor), 0x80,
                                         SHARE_ALL, None, BACKUP_SEMANTICS)
        if handle in (None, INVALID_HANDLE):
            return None  # Deleted/inaccessible files are resolved by the caller's old ID map.
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            length = self.kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
            if not length or length >= len(buffer):
                return None
            path = buffer.value
            return '\\\\'+path[8:] if path.startswith('\\\\?\\UNC\\') else path.removeprefix('\\\\?\\')
        finally:
            self.kernel.CloseHandle(handle)


def _stopped(stop):
    return stop is not None and (stop.is_set() if hasattr(stop, 'is_set') else bool(stop()))


class Journal:
    def __init__(self, volume):
        self.volume = os.path.abspath(os.fspath(volume))

    @contextmanager
    def _open(self):
        try:
            with _Volume(self.volume) as volume:
                yield volume
        except JournalUnavailable:
            raise
        except OSError as error:
            raise JournalUnavailable(f"卷日志暂不可用：{error}") from error

    def snapshot(self):
        with self._open() as volume:
            return volume.snapshot()

    @staticmethod
    def _validate(saved, current):
        try:
            same = (saved['volume_identity'], int(saved['device']), int(saved['journal_id'])) == (
                current['volume_identity'], int(current['device']), int(current['journal_id']))
            cursor = int(saved['next_usn'])
            if not same or not max(current['first_usn'], current.get('lowest_valid_usn', 0)) <= cursor <= current['next_usn']:
                raise JournalGap("卷或 USN 日志已变化，或者历史记录已回收")
            return cursor
        except (KeyError, TypeError, ValueError) as error:
            raise JournalGap("保存的 USN 游标无效") from error

    def changes(self, saved_snapshot, stop=None, target=None):
        if _stopped(stop):
            raise JournalCancelled("USN 同步已取消")
        with self._open() as volume:
            current = volume.snapshot()
            cursor = self._validate(saved_snapshot, current)
            target = dict(current if target is None else target)
            cutoff = self._validate(target, current)
            if cutoff < cursor:
                raise JournalGap("USN 截止位置早于已保存游标")
            events = []
            bytes_read = 0
            while cursor < cutoff:
                if _stopped(stop):
                    raise JournalCancelled("USN 同步已取消")
                raw = volume.read(cursor, current['journal_id'])
                bytes_read += len(raw)
                if bytes_read > MAX_USN_BYTES:
                    raise JournalGap("USN 变化过多，改用媒体范围核对")
                next_usn, batch = parse_usn_buffer(raw)
                if next_usn <= cursor:
                    raise JournalGap("USN 读取没有前进，需核对卷索引")
                events.extend(event for event in batch if cursor <= event['usn'] < cutoff)
                if len(events) > MAX_USN_EVENTS:
                    raise JournalGap("USN 事件过多，改用媒体范围核对")
                cursor = min(next_usn, cutoff)
            after = volume.snapshot()
            if (after['volume_identity'], after['journal_id']) != (current['volume_identity'], current['journal_id']):
                raise JournalGap("读取期间 USN 日志已被替换")
            if _stopped(stop):
                raise JournalCancelled("USN 同步已取消")
            return events, target

    def resolve(self, event):
        if event.get('path'):
            return os.path.abspath(event['path'])
        try:
            with _Volume(self.volume) as volume:
                return volume.resolve(event)
        except (JournalUnavailable, OSError):
            return None


def _within(path, root):
    path, root = (os.path.normcase(os.path.abspath(p)) for p in (path, root))
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def _merge_roots(paths):
    unique = sorted({os.path.normcase(os.path.abspath(os.fspath(p))) for p in paths}, key=len)
    result = []
    for path in unique:
        if not any(_within(path, root) for root in result):
            result.append(path)
    return result


def parse_notify_buffer(data, root):
    root = os.path.normcase(os.path.abspath(root))
    actions = {1: 'created', 2: 'deleted', 3: 'modified', 4: 'renamed_old', 5: 'renamed_new'}
    events, offset = [], 0
    if not data:
        return [dict(path=root, root=root, rescan=True, action='overflow')]
    while offset < len(data):
        if len(data)-offset < 12:
            raise JournalGap("目录通知记录被截断")
        step, action, size = struct.unpack_from('<III', data, offset)
        if size % 2 or offset+12+size > len(data) or action not in actions:
            raise JournalGap("目录通知记录无效")
        name = data[offset+12:offset+12+size].decode('utf-16-le', 'surrogatepass')
        path = os.path.normcase(os.path.abspath(os.path.join(root, name)))
        if not name or '\0' in name or not _within(path, root):
            raise JournalGap("目录通知路径越界")
        events.append(dict(path=path, root=root, rescan=False, action=actions[action]))
        if not step:
            break
        if step % 4 or step < 12+size or offset+step >= len(data):
            raise JournalGap("目录通知偏移无效")
        offset += step
    return events


class NativeDirectoryWatcher:
    """One cancellable overlapped reader per merged root, never per leaf folder."""
    def __init__(self, paths, callback):
        self.paths = _merge_roots(paths)
        self.callback = callback
        self.ready = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads, self._handles, self._ready_roots = [], set(), set()
        self.errors = []

    def _emit(self, event):
        try:
            self.callback(event)
        except Exception as error:
            with self._lock:
                self.errors.append(str(error))
                del self.errors[:-8]

    def _mark_ready(self, root):
        with self._lock:
            self._ready_roots.add(root)
            if len(self._ready_roots) == len(self.paths):
                self.ready.set()

    def start(self):
        if self._threads or self._stop.is_set():
            return
        if not self.paths:
            self.ready.set()
        for root in self.paths:
            thread = threading.Thread(target=self._watch, args=(root,), name='PhotoNest-folder-watch', daemon=True)
            self._threads.append(thread)
            thread.start()

    def close(self):
        self._stop.set()
        with self._lock:
            for handle in self._handles:
                _kernel().CancelIoEx(handle, None)
        deadline = time.monotonic()+5
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(max(0, deadline-time.monotonic()))

    def _watch(self, root):
        kernel = handle = signal = None
        pending = False
        overlapped = _Overlapped()
        try:
            kernel = _kernel()
            identity = os.stat(root)
            identity = identity.st_dev, identity.st_ino
            handle = kernel.CreateFileW(root, 1, SHARE_ALL, None, 3,
                                        BACKUP_SEMANTICS | OVERLAPPED_FLAG, None)
            if handle in (None, INVALID_HANDLE):
                raise _native_error('监听目录')
            signal = kernel.CreateEventW(None, True, False, None)
            if not signal:
                raise _native_error('创建目录监听事件')
            overlapped.hEvent = signal
            buffer = ctypes.create_string_buffer(64*1024)
            with self._lock:
                self._handles.add(handle)
            checked = time.monotonic()
            while not self._stop.is_set():
                if time.monotonic()-checked >= 1:
                    now = os.stat(root)
                    if (now.st_dev, now.st_ino) != identity:
                        raise JournalUnavailable('监听根目录已被替换')
                    checked = time.monotonic()
                kernel.ResetEvent(signal)
                with self._lock:
                    if self._stop.is_set():
                        break
                    queued = kernel.ReadDirectoryChangesW(handle, buffer, len(buffer), True,
                        1 | 2 | 4 | 8 | 16, None, ctypes.byref(overlapped), None)
                    if not queued and ctypes.get_last_error() != 997:
                        raise _native_error('读取目录变化')
                    pending = True
                self._mark_ready(root)
                while not self._stop.is_set():
                    state = kernel.WaitForSingleObject(signal, 200)
                    if state == 0:
                        break
                    if state != 258:
                        raise _native_error('等待目录变化')
                    if time.monotonic()-checked >= 1:
                        now = os.stat(root)
                        if (now.st_dev, now.st_ino) != identity:
                            raise JournalUnavailable('监听根目录已被替换')
                        checked = time.monotonic()
                if self._stop.is_set():
                    break
                returned = ctypes.c_uint32()
                success = kernel.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(returned), False)
                pending = False
                if not success:
                    if ctypes.get_last_error() == 1022:
                        self._emit(dict(path=root, root=root, rescan=True, action='overflow'))
                        continue
                    raise _native_error('获取目录变化')
                try:
                    events = parse_notify_buffer(buffer.raw[:returned.value], root)
                except JournalGap:
                    events = [dict(path=root, root=root, rescan=True, action='overflow')]
                for event in events:
                    self._emit(event)
        except Exception as error:
            if not self._stop.is_set():
                with self._lock:
                    self.errors.append(f'{root}: {error}')
                    del self.errors[:-8]
                self._emit(dict(path=root, root=root, rescan=True, action='unavailable', error=str(error)))
        finally:
            self._mark_ready(root)
            if kernel and handle not in (None, INVALID_HANDLE):
                with self._lock:
                    self._handles.discard(handle)
                if pending:
                    kernel.CancelIoEx(handle, ctypes.byref(overlapped))
                    returned = ctypes.c_uint32()
                    kernel.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(returned), True)
                kernel.CloseHandle(handle)
            if kernel and signal:
                kernel.CloseHandle(signal)
