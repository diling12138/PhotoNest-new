"""Windows Shell integration. Only recorded Recycle Bin items may be purged."""
from contextlib import contextmanager
import os
import ctypes
import sys
import threading
import shutil
import uuid
import json
import stat
import struct
import tempfile
import time
import subprocess
import hashlib

import pythoncom
import win32api
import win32security
import win32event
from win32com.shell import shell, shellcon
from win32com.server.policy import DesignatedWrapPolicy

_shell_lock = threading.RLock()


class SessionInstance:
    """One process per library; another launch requests that its window be shown."""
    def __init__(self, data_dir):
        name = 'Local\\PhotoNest-'+hashlib.sha256(os.path.normcase(os.path.abspath(data_dir)).encode()).hexdigest()[:24]
        self._mutex = win32event.CreateMutex(None, False, name)
        self.already_running = win32api.GetLastError() == 183
        self._event = win32event.CreateEvent(None, False, False, name+'-open')

    def request_open(self):
        win32event.SetEvent(self._event)

    def consume_open(self):
        return self._event is not None and win32event.WaitForSingleObject(self._event, 0) == 0

    def close(self):
        for name in ('_event', '_mutex'):
            handle = getattr(self, name, None)
            if handle is not None:
                handle.Close()
                setattr(self, name, None)


@contextmanager
def _com():
    # PyInstaller's DLL directory is inherited by external viewers and Explorer.
    # Restore Windows' search order during Shell calls, then restore our runtime.
    with _shell_lock:
        bundle = getattr(sys, "_MEIPASS", None)
        old_path = os.environ.get("PATH", "")
        dll_path = ctypes.create_unicode_buffer(32768)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetDllDirectoryW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p]
        kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
        kernel.GetDllDirectoryW(len(dll_path), dll_path)
        handle = None
        if bundle:
            handle = os.add_dll_directory(bundle)
            kernel.SetDllDirectoryW(None)
            prefix = os.path.normcase(os.path.abspath(bundle))
            os.environ["PATH"] = os.pathsep.join(p for p in old_path.split(os.pathsep)
                if os.path.normcase(os.path.abspath(p or os.curdir)) != prefix
                and not os.path.normcase(os.path.abspath(p or os.curdir)).startswith(prefix+os.sep))
        pythoncom.CoInitialize()
        try:
            yield
        finally:
            pythoncom.CoUninitialize()
            if bundle:
                kernel.SetDllDirectoryW(dll_path.value or None)
                os.environ["PATH"] = old_path
                if handle:
                    handle.close()


def _existing(path):
    path = os.path.abspath(path)
    # stat also reports permissions errors, rather than misreporting a missing file.
    os.stat(path)
    return path


def is_administrator():
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def launch_elevated(executable, arguments, directory):
    with _com():
        shell.ShellExecuteEx(lpVerb="runas", lpFile=executable,
            lpParameters=subprocess.list2cmdline(arguments), lpDirectory=directory,
            # pywin32 exposes SEE_MASK_NOASYNC under its legacy DDEWAIT name.
            nShow=1, fMask=shellcon.SEE_MASK_FLAG_NO_UI | shellcon.SEE_MASK_FLAG_DDEWAIT)


def reveal(path, owner=0):
    with _com():
        pidl, _ = shell.SHParseDisplayName(_existing(path), 0)
        shell.SHOpenFolderAndSelectItems(pidl, (), 0)


def open_file(path, owner=0):
    with _com():
        path = _existing(path)
        shell.ShellExecuteEx(hwnd=owner, lpVerb="open", lpFile=path,
                             lpDirectory=os.path.dirname(path), nShow=1,
                             fMask=shellcon.SEE_MASK_FLAG_NO_UI)


def _item(path):
    return shell.SHCreateItemFromParsingName(path, None, shell.IID_IShellItem)


class _Progress(DesignatedWrapPolicy):
    _com_interfaces_ = [shell.IID_IFileOperationProgressSink]
    _public_methods_ = ["StartOperations", "FinishOperations", "PreRenameItem", "PostRenameItem",
        "PreMoveItem", "PostMoveItem", "PreCopyItem", "PostCopyItem", "PreDeleteItem",
        "PostDeleteItem", "PreNewItem", "PostNewItem", "UpdateProgress", "ResetTimer",
        "PauseTimer", "ResumeTimer"]

    def __init__(self, recycle):
        self._wrap_(self)
        self.recycle = recycle
        self.token = None
        self.completed = False
        self.result = 0

    def PreDeleteItem(self, flags, item):
        # Modern Windows may omit TSF_DELETE_RECYCLE_IF_POSSIBLE even when
        # FOFX_RECYCLEONDELETE was requested. recycle() protects the original.
        return 0

    def PostDeleteItem(self, flags, item, result, created):
        self.result = result
        self.completed = result >= 0 and result != 0x00270008
        if created is not None:
            self.token = created.GetDisplayName(shellcon.SIGDN_DESKTOPABSOLUTEPARSING)
        return 0

    def PostMoveItem(self, flags, item, destination, name, result, created):
        self.result = result
        self.completed = result >= 0 and result != 0x00270008
        return 0


def _noop(self, *args):
    return 0


for _method in _Progress._public_methods_:
    if not hasattr(_Progress, _method):
        setattr(_Progress, _method, _noop)


class _OperationIncomplete(OSError):
    def __init__(self, result, aborted):
        self.hresult = result
        self.aborted = bool(aborted)
        super().__init__(f"Windows 未完成文件操作（0x{result & 0xffffffff:08X}）；文件记录已保留")


def _operation(item, *, recycle=False, destination=None, name=None):
    operation = pythoncom.CoCreateInstance(shell.CLSID_FileOperation, None,
        pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IFileOperation)
    flags = (shellcon.FOF_NOCONFIRMATION | shellcon.FOF_NOERRORUI | shellcon.FOF_SILENT
             | shellcon.FOFX_EARLYFAILURE | shellcon.FOFX_SHOWELEVATIONPROMPT)
    if recycle:
        flags |= 0x20000000 | 0x00080000 | shellcon.FOF_ALLOWUNDO | shellcon.FOF_WANTNUKEWARNING
    operation.SetOperationFlags(flags)
    progress = _Progress(recycle)
    sink = pythoncom.WrapObject(progress, shell.IID_IFileOperationProgressSink)
    if destination is not None:
        operation.MoveItem(item, _item(destination), name, sink)
    else:
        operation.DeleteItem(item, sink)
    operation.PerformOperations()
    # Some shells report S_DONT_PROCESS_CHILDREN (0x00270008) after moving the
    # item to the Recycle Bin and leave the created item empty; the caller
    # resolves the record from the bin instead of treating it as a failure.
    aborted = operation.GetAnyOperationsAborted()
    if aborted or (
            not progress.completed and not (recycle and progress.result == 0x00270008)):
        raise _OperationIncomplete(progress.result, aborted)
    return progress.token


def _discard_recovery(backup, record):
    for entry in (backup, record):
        try:
            if os.path.isfile(entry):
                os.chmod(entry, stat.S_IREAD | stat.S_IWRITE)
                os.unlink(entry)
        except OSError:
            pass  # A completed recycle must not become a failure due to cache cleanup.


def _prepare_recovery(path, recovery_dir):
    os.makedirs(recovery_dir, exist_ok=True)
    backup = os.path.join(recovery_dir, uuid.uuid4().hex+".recovery")
    record = backup+".json"
    before = os.stat(path)
    try:
        # The source folder may allow deleting an existing photo but forbid new files.
        # Always make an independent copy in the application's writable data directory.
        shutil.copy2(path, backup)
        after = os.stat(path)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        if identity(before) != identity(after) or os.path.getsize(backup) != before.st_size:
            raise OSError("照片在准备回收时发生变化，请稍后重试；原文件未删除")
        os.chmod(backup, stat.S_IREAD | stat.S_IWRITE)
        with open(backup, "r+b") as target:
            os.fsync(target.fileno())
        with open(record, "x", encoding="utf-8") as target:
            json.dump({"original_path": path, "size": before.st_size,
                       "mtime_ns": before.st_mtime_ns, "mode": before.st_mode}, target, ensure_ascii=False)
            target.flush()
            os.fsync(target.fileno())
        return backup, record, before
    except Exception:
        _discard_recovery(backup, record)
        raise


def _restore_recovery(backup, path, original_stat):
    # Exclusive creation avoids overwriting a file created meanwhile. Copying also
    # works when the photo and application data live on different volumes.
    created = False
    try:
        with open(backup, "rb") as source, open(path, "xb") as target:
            created = True
            shutil.copyfileobj(source, target, length=1024*1024)
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        if created:
            try:
                os.unlink(path)
            except OSError:
                pass
        raise
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    os.chmod(path, original_stat.st_mode)


def _current_user_sid():
    handle = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    try:
        return win32security.ConvertSidToStringSid(
            win32security.GetTokenInformation(handle, win32security.TokenUser)[0])
    finally:
        handle.Close()


def _recycle_bin_folders(path):
    """Only inspect this user's bin, without enumerating other users' folders."""
    drive, _ = os.path.splitdrive(os.path.abspath(path))
    if not drive:
        return []
    return [os.path.join(drive + os.sep, "$Recycle.Bin", _current_user_sid())]


def _recycled_token(path, size, since):
    """Return the $R item recorded for *path* when the shell reported nothing."""
    target = os.path.normcase(os.path.abspath(path))
    best = None
    for folder in _recycle_bin_folders(path):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not name.startswith("$I"):
                continue
            record = os.path.join(folder, name)
            try:
                if os.path.getmtime(record) < since:
                    continue
                with open(record, "rb") as handle:
                    header = handle.read(28)
                    if len(header) != 28:
                        continue
                    version, stored_size, deleted_at, length = struct.unpack("<QQQI", header)
                    if (version != 2 or stored_size != size or not 0 < length <= 32768
                            or deleted_at / 10000000 - 11644473600 < since):
                        continue
                    raw = handle.read(length * 2)
                payload = os.path.join(folder, "$R" + name[2:])
                if not os.path.isfile(payload) or os.path.getsize(payload) != size:
                    continue
            except OSError:
                continue
            if len(raw) != length * 2:
                continue
            original = raw.decode("utf-16-le", "surrogatepass").split("\x00", 1)[0]
            if os.path.normcase(original) != target:
                continue
            candidate = (deleted_at, payload)
            if best is None or candidate[0] >= best[0]:
                best = candidate
    return best[1] if best else None


def recycle(path, *, recovery_dir=None):
    path = _existing(path)
    recovery_dir = os.path.abspath(recovery_dir or os.path.join(tempfile.gettempdir(), "PhotoNest", "recovery"))
    backup, record, original_stat = _prepare_recovery(path, recovery_dir)
    started = time.time()
    try:
        with _com():
            token = _operation(_item(path), recycle=True)
            if os.path.exists(path):
                raise OSError("Windows 未移动原文件")
            if not token:
                # The shell finished without reporting the created item; the
                # bin record is still authoritative, so look it up by path.
                token = _recycled_token(path, original_stat.st_size, started)
                if not token:
                    raise OSError("照片已被删除，但系统回收站中没有找到对应记录")
    except Exception as exc:
        if not os.path.exists(path):
            try:
                _restore_recovery(backup, path, original_stat)
            except Exception as recovery_error:
                raise OSError(f"Windows 回收失败，安全副本保留于：{backup}。{recovery_error}") from exc
        else:
            current = os.stat(path)
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
                    original_stat.st_dev, original_stat.st_ino, original_stat.st_size, original_stat.st_mtime_ns):
                raise OSError(f"回收未完成，原位置的文件已变化；安全副本保留于：{backup}") from exc
        _discard_recovery(backup, record)
        code = getattr(exc, "hresult", None)
        detail = f"（Windows 0x{code & 0xffffffff:08X}）" if code is not None else f"（{exc}）"
        raise OSError("无法移入系统回收站，原文件已保留或恢复。"+detail) from exc
    _discard_recovery(backup, record)
    return token


def _recycled_item(token):
    if not token:
        raise ValueError("缺少系统回收站记录")
    # An inaccessible bin must not be confused with an externally emptied bin.
    shell.SHQueryRecycleBin(None)
    pidl = shell.SHGetSpecialFolderLocation(0, shellcon.CSIDL_BITBUCKET)
    folder = shell.SHGetDesktopFolder().BindToObject(pidl, None, shell.IID_IShellFolder)
    def key(value):
        return os.path.normcase(os.path.normpath(value.removeprefix('\\\\?\\')))
    target = key(token)
    for child in folder.EnumObjects(0, shellcon.SHCONTF_NONFOLDERS | shellcon.SHCONTF_INCLUDEHIDDEN):
        item = shell.SHCreateItemFromIDList(pidl + child, shell.IID_IShellItem)
        for flag in (shellcon.SIGDN_DESKTOPABSOLUTEPARSING, shellcon.SIGDN_FILESYSPATH):
            try:
                if key(item.GetDisplayName(flag)) == target:
                    return item
            except pythoncom.com_error:
                continue  # Virtual Shell items may not expose a filesystem name.
    return None


def _recycled_file_path(token):
    """Accept only a single $R file in this user's local Windows bin."""
    if not os.path.isabs(token):
        return None
    path = os.path.normpath(token.removeprefix('\\\\?\\'))
    drive, tail = os.path.splitdrive(path)
    parts = tail.strip('\\/').split(os.sep)
    if (not drive or drive.startswith('\\\\') or len(parts) != 3
            or parts[0].casefold() != '$recycle.bin' or parts[1].casefold() != _current_user_sid().casefold()
            or not parts[2].casefold().startswith('$r')):
        return None
    return path


def _missing_recycled_file(token):
    """Confirm absence only for an exact file in the current user's accessible bin."""
    path = _recycled_file_path(token)
    if path is None:
        return False
    # A missing/offline volume or inaccessible folder is not proof of deletion.
    os.stat(os.path.dirname(path))
    try:
        os.stat(path)
    except FileNotFoundError:
        return True
    return False


def restore(token, original):
    with _com():
        original = os.path.abspath(original)
        if os.path.lexists(original):
            raise FileExistsError("原位置已有同名文件，请先移动或重命名该文件")
        item = _recycled_item(token)
        if item is None:
            raise FileNotFoundError("照片已在系统回收站中被清除或移走，无法恢复")
        os.makedirs(os.path.dirname(original), exist_ok=True)
        _operation(item, destination=os.path.dirname(original), name=os.path.basename(original))
        if not os.path.isfile(original):
            raise OSError("Windows 未能将照片恢复到原位置")


def purge(token):
    with _com():
        item = _recycled_item(token)
        if item is None:
            if _missing_recycled_file(token):
                return  # Already emptied externally; drop only the stale library entry.
            raise FileNotFoundError("系统回收站中未找到对应照片，可能已被清除或无访问权限；记录已保留供核对")
        physical = _recycled_file_path(token)
        if physical is None:
            try:
                physical = _recycled_file_path(item.GetDisplayName(shellcon.SIGDN_FILESYSPATH))
            except pythoncom.com_error:
                pass
        incomplete = None
        try:
            _operation(item)
        except _OperationIncomplete as error:
            # S_DONT_PROCESS_CHILDREN is a transfer status, not proof that the
            # photo remains. Aborted or failed operations still retain the record.
            if error.aborted or error.hresult != 0x00270008:
                raise
            incomplete = error
        if physical is not None:
            # Verify the actual $R payload, not a potentially stale Shell listing.
            # stat propagates access/offline errors instead of calling them missing.
            if _missing_recycled_file(physical):
                return
        elif incomplete is None and _recycled_item(token) is None:
            return  # Virtual receipt: a completed operation and a fresh bin lookup.
        if incomplete is not None:
            raise incomplete
        raise OSError("系统回收站尚未清除该照片，记录已保留")
