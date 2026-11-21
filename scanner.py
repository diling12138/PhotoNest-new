"""PhotoNest - library scanner.

Walks the configured roots, extracts metadata, hashes files, generates
thumbnails and keeps SQLite in sync. Runs on a single background thread so
it never blocks the desktop interface.
"""
from __future__ import annotations

import hashlib
import ctypes
import os
import threading
import itertools
import stat as stat_module
import errno
import json
from types import SimpleNamespace
from datetime import datetime

from PIL import Image

import thumbs
import sources

ISO = "%Y-%m-%dT%H:%M:%S"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".wmv"}

EXCLUDE_DIRS = {
    "node_modules", ".git", ".svn", "$recycle.bin", "system volume information",
    ".photonest", "windows", "program files", "program files (x86)",
    "appdata", "programdata", "recovery", "perflogs", "windowsapps", "msocache",
    ".cache", ".codex", ".venv", "venv", "__pycache__", "_internal", "_build",
    ".vscode", ".idea", ".npm", ".nuget", ".gradle", ".cargo", ".rustup",
}

HASH_LIMIT = 256 * 1024 * 1024  # hash the whole file below this size
SAMPLE_SIZE = 4 * 1024 * 1024


def fixed_drives() -> list[str]:
    """Discover local fixed disks only; never probe network or removable drives."""
    if os.name != "nt":
        return [os.path.expanduser("~")]
    kernel = ctypes.windll.kernel32
    kernel.GetLogicalDrives.restype = ctypes.c_uint32
    kernel.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    kernel.GetDriveTypeW.restype = ctypes.c_uint32
    mask = kernel.GetLogicalDrives()
    return [f"{chr(65 + i)}:\\" for i in range(26)
            if mask & (1 << i) and kernel.GetDriveTypeW(f"{chr(65 + i)}:\\") == 3]


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def _within(path, parent):
    try:
        return os.path.commonpath((_key(path), _key(parent))) == _key(parent)
    except ValueError:
        return False


def priority_folders():
    home = os.path.expanduser("~")
    folders = [os.path.join(home, name) for name in ("Pictures", "Desktop", "Downloads", "Documents")]
    if os.name == "nt":
        # Include redirected Windows known folders (for example Pictures on D:).
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                for name in ("My Pictures", "Desktop", "Personal", "{374DE290-123F-4565-9164-39C4925E467B}"):
                    try:
                        folders.insert(0, os.path.expandvars(winreg.QueryValueEx(key, name)[0]))
                    except OSError:
                        pass
        except OSError:
            pass
    return list({_key(path): os.path.abspath(path) for path in folders}.values())


def file_hash(path: str, size: int, stop=None) -> str:
    digest = hashlib.blake2b(digest_size=16)
    if stop is not None and stop.is_set():
        return ''
    try:
        if size <= HASH_LIMIT:
            with open(path, "rb") as fh:
                while True:
                    if stop is not None and stop.is_set():
                        return ""
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        else:
            with open(path, "rb") as fh:
                for offset in (0, max(0, size // 2-SAMPLE_SIZE//2), max(0, size-SAMPLE_SIZE)):
                    fh.seek(offset)
                    remaining = SAMPLE_SIZE
                    while remaining:
                        if stop is not None and stop.is_set():
                            return ''
                        chunk = fh.read(min(1024*1024, remaining))
                        if not chunk:
                            break
                        digest.update(chunk)
                        remaining -= len(chunk)
            digest.update(str(size).encode())
    except OSError:
        return ""
    return digest.hexdigest()


def _gps_to_deg(values, ref) -> float | None:
    try:
        deg, minute, sec = (float(v) for v in values)
        result = deg + minute / 60.0 + sec / 3600.0
        if str(ref).upper() in ("S", "W"):
            result = -result
        return round(result, 6)
    except Exception:
        return None


def _parse_exif_datetime(value) -> str | None:
    if not value:
        return None
    text = str(value).strip().replace("\x00", "")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M"):
        try:
            return datetime.strptime(text[:19], fmt).strftime(ISO)
        except ValueError:
            continue
    return None


def is_screenshot(path: str) -> bool:
    haystack = path.lower()
    keys = ("screenshot", "screen shot", "screencap", "截图", "截屏", "snipaste", "屏幕截图")
    return any(key in haystack for key in keys)


def probe(path: str, ext: str) -> dict:
    """Best-effort metadata extraction. Never raises."""
    info = {
        "width": 0, "height": 0, "taken_at": None, "camera": None,
        "gps_lat": None, "gps_lon": None,
        "media_type": "video" if ext in VIDEO_EXTS else "image",
    }
    if ext in VIDEO_EXTS:
        return info

    try:
        with Image.open(path) as im:
            info["width"], info["height"] = im.size
            exif = im.getexif()
            if not exif:
                return info

            taken = exif.get(36867) or exif.get(36868) or exif.get(306)
            info["taken_at"] = _parse_exif_datetime(taken)

            make = (exif.get(271) or "").strip()
            model = (exif.get(272) or "").strip()
            camera = " ".join(x for x in (make, model) if x)
            info["camera"] = camera or None

            try:
                gps = exif.get_ifd(34853)
                if gps:
                    lat = _gps_to_deg(gps.get(2), gps.get(1))
                    lon = _gps_to_deg(gps.get(4), gps.get(3))
                    info["gps_lat"], info["gps_lon"] = lat, lon
            except Exception:
                pass
    except OSError as error:
        if error.errno is not None or getattr(error, 'winerror', None) is not None:
            info['io_error'] = str(error)
    except Exception:
        pass
    return info


class Scanner:
    def __init__(self, db, cache_dir: str, *, exclude_paths=()):
        self.db = db
        self.cache_dir = cache_dir
        self.exclude_paths = tuple(_key(p) for p in (*exclude_paths,
            os.path.join(cache_dir, "thumb"), os.path.join(cache_dir, "preview")))
        self._custom_exclude_paths = ()
        self._filter_resources = True
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.revision = 0
        self._run_id = 0
        self._retry_paths = {}
        self._pending_paths = set()
        self._pending_event = threading.Event()
        self._stat_cache = {}
        self._directory_buffer = []
        self._root_paths = ()
        self._media_paths = ()
        self._remembered_media = set()
        self._lightweight = False
        self._servicing_pending = False
        self._seen_paths = set()
        self._not_scanned = []
        self._refresh_scopes()
        self.status = {
            "running": False,
            "phase": "idle",
            "total": 0,
            "done": 0,
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "errors": 0,
            "message": "",
            "current": "",
            "started_at": None,
            "finished_at": None,
            "error_log": [],
            "changed": 0,
            "baseline_ready": False,
        }

    # ------------------------------------------------------------------ api
    def start(self, *, discover=False, initial=False, paths=None) -> bool:
        self._refresh_scopes()
        requested = None if paths is None else {os.path.abspath(p) for p in paths}
        with self._lock:
            if self._thread and self._thread.is_alive() and self.status["running"]:
                if requested is None:
                    return False
                self._pending_paths.update(requested)
                if requested:
                    self._pending_event.set()
                return True
            self._stop.clear()
            self._run_id += 1
            self._retry_paths.clear()
            self._pending_paths.clear()
            self._pending_event.clear()
            self._stat_cache.clear()
            self.status.update(running=True, phase="walking", total=0, done=0, added=0, updated=0,
                skipped=0, errors=0, changed=0, baseline_ready=False,
                message="正在检查文件变化…" if paths is not None else "正在查找照片…", current="", error_log=[])
            self._thread = threading.Thread(target=self._run, args=(discover, initial, requested),
                                            name="photonest-scanner", daemon=True)
            self._thread.start()
        return True

    def stop(self, *, wait=False) -> None:
        self._stop.set()
        if wait and self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=10)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.status, error_log=list(self.status["error_log"]), run_id=self._run_id,
                        retry_paths=sorted(self._retry_paths.values()))

    # -------------------------------------------------------------- internals
    def _set(self, **fields) -> None:
        with self._lock:
            self.status.update(fields)

    def _error(self, message: str) -> None:
        with self._lock:
            self.status["errors"] += 1
            log = self.status["error_log"]
            log.append(message[:300])
            del log[:-8]

    def _retry(self, path):
        """Keep concrete failed work; disconnected volumes are handled by sync discovery."""
        path = os.path.abspath(path)
        if self.is_excluded(path):
            return
        if not any(_within(path, root) for root in self._root_paths):
            return
        drive = os.path.splitdrive(path)[0]
        if drive:
            try:
                os.stat(drive+os.sep)
            except FileNotFoundError:
                return
            except OSError as error:
                if (error.errno in (errno.ENODEV, errno.ENXIO)
                        or getattr(error, 'winerror', None) in (15, 21, 53, 55, 1167)):
                    return
        with self._lock:
            self._retry_paths.setdefault(_key(path), path)

    @staticmethod
    def _identity(info):
        return f"{info.st_dev}:{info.st_ino}" if getattr(info, 'st_ino', 0) else None

    def _refresh_scopes(self):
        self.refresh_exclusions()
        self._root_paths = tuple(self.db.root_paths())
        media = self.db.media_roots()
        self._media_paths = tuple(row['path'] for row in media
                                  if any(_within(row['path'], root) for root in self._root_paths))
        self._remembered_media = {_key(p) for p in self._media_paths}

    def refresh_exclusions(self):
        """Refresh user rules without restarting a running scan or touching media."""
        try:
            configured = json.loads(self.db.get_setting('scan_exclude_paths', '[]'))
        except (TypeError, ValueError):
            configured = []
        paths = tuple(sorted({_key(p) for p in configured
                              if isinstance(p, str) and p.strip() and os.path.isabs(p)})) if isinstance(configured, list) else ()
        enabled = self.db.get_setting('scan_filter_resources', '1') == '1'
        changed = (paths, enabled) != (self._custom_exclude_paths, self._filter_resources)
        self._custom_exclude_paths, self._filter_resources = paths, enabled
        with self._lock:
            self._retry_paths = {key: path for key, path in self._retry_paths.items() if not self.is_excluded(path)}
        if changed and self._thread and self._thread.is_alive():
            self._pending_event.set()  # Also interrupt an in-progress hash at its next read block.

    def is_excluded(self, path):
        """Pure path policy, safe for event filters and queued enrichment work."""
        return (any(_within(path, p) for p in (*self.exclude_paths, *self._custom_exclude_paths))
                or (self._filter_resources and sources.is_resource_path(path)))

    def accepts_path(self, path):
        original_path = os.path.abspath(path)
        path = _key(path)
        if self.is_excluded(path):
            return False
        if not any(_within(path, root) for root in self._root_paths):
            return False  # Source grouping must never widen the user's scan scopes.
        bases = [p for p in (*self._root_paths, *self._media_paths) if _within(path, p)]
        if not bases:
            return False
        base = max(bases, key=len)
        relative = os.path.relpath(path, base)
        if any(part.casefold() in EXCLUDE_DIRS for part in relative.split(os.sep) if part != '.'):
            return False
        # Check every ancestor, including ancestors of an explicit media root:
        # direct notification paths must not bypass the traversal's link/cloud rules.
        drive, tail = os.path.splitdrive(path)
        current = drive+os.sep
        for part in tail.strip('\\/').split(os.sep):
            if not part:
                continue
            current = os.path.join(current, part)
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                break  # A removed subtree is allowed through for missing-file reconciliation.
            except OSError:
                self._retry(original_path)
                return False
            attrs, tag = getattr(info, 'st_file_attributes', 0), getattr(info, 'st_reparse_tag', 0)
            if (attrs & (0x1000 | 0x40000 | 0x400000) or tag in (0xA000000C, 0xA0000003)
                    or stat_module.S_ISLNK(info.st_mode)):
                return False
        return True

    def _changed(self):
        with self._lock:
            self.revision += 1
            self.status['changed'] += 1

    def _flush_directories(self):
        if not self._directory_buffer:
            return
        pending, self._directory_buffer = self._directory_buffer, []
        with self.db.batch():
            for identity, path, root in pending:
                self.db.remember_directory(identity, path, root)

    def _directory_seen(self, path, root):
        root = max((r for r in self._root_paths if _within(path, r)), key=len, default=root)
        try:
            identity = self._identity(os.stat(path))
        except OSError as error:
            self._not_scanned.append(_key(path))
            self._retry(path)
            self._error(str(error))
            return
        if identity:
            self._directory_buffer.append((identity, path, root))
            if len(self._directory_buffer) >= 128:
                self._flush_directories()
                if self._pending_event.is_set() and not self._servicing_pending:
                    self._drain_pending()

    def _skip_directory(self, path):
        return (os.path.basename(path).lower() in EXCLUDE_DIRS
                or self.is_excluded(path)
                or os.path.islink(path)
                or getattr(os.path, "isjunction", lambda p: False)(path))

    def _iter_files(self, roots):
        # Scan overlapping scopes once, but keep the most specific owner for counts/removal.
        owner_names = {_key(r["path"]): r["path"] for r in roots}
        owners = sorted(owner_names, key=len, reverse=True)
        bases = [root for root in owners if not any(root != other and _within(root, other) for other in owners)]
        priority = {_key(p): p for p in priority_folders()
                    if any(_within(p, root) for root in bases) and os.path.isdir(p)}
        priority.update({_key(p): p for p in self._media_paths
                         if any(_within(p, root) for root in bases) and os.path.isdir(p)})
        completed = []
        for base in list(dict.fromkeys(list(priority) + bases)):
            if self._stop.is_set():
                return
            if any(_within(base, p) for p in completed):
                continue
            if self._skip_directory(base) or not self.accepts_path(base):
                self._not_scanned.append(base)
                continue
            if not os.path.isdir(base):
                self._error(f"目录不可用：{base}")
                self._retry(owner_names.get(base, base))
                self._not_scanned.append(base)
                continue
            source = owner_names.get(base) or priority[base]
            def failed(error):
                path = getattr(error, 'filename', None) or source
                self._not_scanned.append(_key(path))
                self._retry(path)
                self._error(str(error))
            for dirpath, dirnames, filenames in os.walk(source, onerror=failed):
                if self._stop.is_set():
                    return
                if self.is_excluded(dirpath):
                    self._not_scanned.append(_key(dirpath))
                    dirnames[:] = []
                    continue
                self._set(current=dirpath)
                owner = next(root for root in owners if _within(dirpath, root))
                self._directory_seen(dirpath, owner_names[owner])
                self._not_scanned.extend(_key(os.path.join(dirpath, d)) for d in dirnames
                    if self._skip_directory(os.path.join(dirpath, d)))
                dirnames[:] = [d for d in dirnames if not self._skip_directory(os.path.join(dirpath, d))
                    and not any(_within(os.path.join(dirpath, d), p) for p in completed)]
                for filename in filenames:
                    if self._stop.is_set():
                        return
                    ext = os.path.splitext(filename)[1].lower()
                    if ext in IMAGE_EXTS or ext in VIDEO_EXTS:
                        path = os.path.join(dirpath, filename)
                        if self.is_excluded(path):
                            self._not_scanned.append(_key(path))
                            continue
                        try:
                            stat = os.stat(path, follow_symlinks=False)
                            # Avoid links and cloud-only placeholders that would trigger downloads.
                            attributes = getattr(stat, "st_file_attributes", 0)
                            tag = getattr(stat, "st_reparse_tag", 0)
                            if (attributes & (0x1000 | 0x40000 | 0x400000)
                                    or tag in (0xA000000C, 0xA0000003)
                                    or stat_module.S_ISLNK(getattr(stat, 'st_mode', 0))):
                                self._not_scanned.append(_key(path))
                                continue
                        except OSError as exc:
                            self._error(str(exc))
                            self._retry(path)
                            self._not_scanned.append(_key(path))
                            continue
                        owner = next(root for root in owners if _within(path, root))
                        path = os.path.join(owner_names[owner], os.path.relpath(path, owner))
                        self._stat_cache[_key(path)] = stat
                        yield owner_names[owner], path
            completed.append(base)

    def _scan_files(self, stream):
        iterator, batch_size = iter(stream), 1
        previous_lightweight = self._lightweight
        self._lightweight = True
        try:
            while not self._stop.is_set():
                batch = list(itertools.islice(iterator, batch_size))
                if not batch:
                    break
                with self.db.batch():
                    self._flush_directories()
                    for root, path in batch:
                        if self._stop.is_set():
                            break
                        self._seen_paths.add(_key(path))
                        try:
                            outcome = self._process(root, path)
                        except Exception as error:
                            self._not_scanned.append(_key(path))
                            self._error(f"{os.path.basename(path)}: {error}")
                            if isinstance(error, OSError):
                                self._retry(path)
                            outcome = 'error'
                        with self._lock:
                            self.status['total'] += 1
                            self.status['done'] += 1
                            self.status['current'] = path
                            if outcome in ('added', 'updated', 'skipped'):
                                self.status[outcome] += 1
                batch_size = 128
                if self._pending_event.is_set() and not self._servicing_pending:
                    self._drain_pending()
        finally:
            self._lightweight = previous_lightweight
            self._flush_directories()

    def _rows_in_scope(self, scope):
        prefix = os.path.abspath(scope).rstrip('\\/')+os.sep
        escaped = prefix.replace('!', '!!').replace('%', '!%').replace('_', '!_')+'%'
        return self.db.q("SELECT id,path,missing FROM asset WHERE deleted=0 AND excluded=0 AND "
                         "(path=? COLLATE NOCASE OR path LIKE ? ESCAPE '!')", (scope, escaped))

    def _reconcile(self, scopes):
        if self._stop.is_set():
            return
        for scope in scopes:
            rows = self._rows_in_scope(scope)
            for start in range(0, len(rows), 128):
                missing = []
                for row in rows[start:start+128]:
                    if self._stop.is_set():
                        return
                    path = _key(row['path'])
                    if (path not in self._seen_paths
                            and not any(_within(path, p) for p in self._not_scanned)
                            and self.accepts_path(path)):
                        missing.append(row['id'])
                with self.db.batch():
                    for asset_id in missing:
                        if self._stop.is_set():
                            return
                        if self.db.mark_missing(asset_id):
                            self._changed()

    def _target_files(self, paths):
        directories = []
        for path in paths:
            if self._stop.is_set() or not self.accepts_path(path):
                continue
            try:
                info = os.stat(path, follow_symlinks=False)
            except FileNotFoundError:
                # Distinguish an externally deleted path from an offline volume.
                parent = os.path.dirname(path)
                while parent:
                    try:
                        os.stat(parent)
                        break
                    except FileNotFoundError:
                        ancestor = os.path.dirname(parent)
                        if ancestor == parent:
                            self._not_scanned.append(path)
                            self._error(f'磁盘暂不可用：{path}')
                            break
                        parent = ancestor
                    except OSError as error:
                        self._not_scanned.append(path)
                        self._retry(path)
                        self._error(str(error))
                        break
                continue
            except OSError as error:
                self._not_scanned.append(path)
                self._retry(path)
                self._error(str(error))
                continue
            if stat_module.S_ISDIR(info.st_mode):
                directories.append({'path': path})
            elif os.path.splitext(path)[1].lower() in IMAGE_EXTS | VIDEO_EXTS:
                attrs = getattr(info, 'st_file_attributes', 0)
                if attrs & (0x1000 | 0x40000 | 0x400000):
                    self._not_scanned.append(path)
                    continue
                owner = max((r for r in self._root_paths if _within(path, r)), key=len)
                self._stat_cache[_key(path)] = info
                yield owner, path
        if directories:
            # Traversal roots may be subtrees, but asset ownership remains its configured root.
            for _, path in self._iter_files(directories):
                owner = max((r for r in self._root_paths if _within(path, r)), key=len)
                yield owner, path

    def _scan_targets(self, paths):
        distinct = {_key(p): os.path.abspath(p) for p in paths if self.accepts_path(p)}
        paths = sorted(distinct.values(), key=len)
        scopes = [p for p in paths if not any(p != other and _within(p, other) for other in paths)]
        if not scopes:
            return
        previous_seen, previous_skips = self._seen_paths, self._not_scanned
        self._seen_paths, self._not_scanned = set(), []
        try:
            self._scan_files(self._target_files(scopes))
            self._reconcile(scopes)
        finally:
            # A pending target may run after the outer baseline has already
            # visited its directory. Preserve its observations/protected scopes
            # so the outer reconciliation cannot hide a newly imported photo.
            previous_seen.update(self._seen_paths)
            previous_skips.extend(self._not_scanned)
            self._seen_paths, self._not_scanned = previous_seen, previous_skips

    def _drain_pending(self):
        with self._lock:
            paths, self._pending_paths = self._pending_paths, set()
            self._pending_event.clear()
        if paths and not self._stop.is_set():
            self._servicing_pending = True
            try:
                self._scan_targets(paths)
            finally:
                self._servicing_pending = False

    def _fill_metadata(self):
        attempted = set()
        while not self._stop.is_set():
            rows = self.db.q("SELECT id,path,size,mtime,ext FROM asset WHERE deleted=0 AND missing=0 AND excluded=0 "
                             "AND media_type='image' AND (width=0 OR height=0)")
            rows = [r for r in rows if (r['id'], r['size'], r['mtime']) not in attempted
                    and not self.is_excluded(r['path'])]
            if not rows:
                return
            pending = []
            for row in rows:
                if self._stop.is_set():
                    break
                if self._pending_event.is_set():
                    self._drain_pending()
                if self.is_excluded(row['path']):
                    continue
                attempted.add((row['id'], row['size'], row['mtime']))
                try:
                    before = os.stat(row['path'])
                    if before.st_size != row['size'] or abs(before.st_mtime-row['mtime']) >= 0.000001:
                        continue
                    meta = probe(row['path'], row['ext'])
                    if meta.get('io_error'):
                        self._retry(row['path'])
                        self._error(meta['io_error'])
                        continue
                    after = os.stat(row['path'])
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        continue
                    if meta['width'] or meta['height']:
                        pending.append((meta, row))
                except OSError as error:
                    self._retry(row['path'])
                    self._error(str(error))
                if len(pending) >= 128:
                    self._save_metadata(pending)
                    pending.clear()
            self._save_metadata(pending)

    def _save_metadata(self, pending):
        if not pending:
            return
        with self.db.batch():
            for meta, row in pending:
                if self.is_excluded(row['path']):
                    continue
                changed = self.db.execute("UPDATE asset SET width=?,height=?,taken_at=COALESCE(?,taken_at),"
                    "camera=?,gps_lat=?,gps_lon=? WHERE id=? AND size=? AND mtime=? AND deleted=0 AND missing=0 AND excluded=0",
                    (meta['width'], meta['height'], meta['taken_at'], meta['camera'], meta['gps_lat'],
                     meta['gps_lon'], row['id'], row['size'], row['mtime'])).rowcount
                if changed:
                    self._changed()

    def _hash_background(self):
        attempted = set()
        interrupted = SimpleNamespace(is_set=lambda: self._stop.is_set() or self._pending_event.is_set())
        while not self._stop.is_set():
            if self._pending_event.is_set():
                self._drain_pending()
                self._fill_metadata()
            rows = self.db.q("SELECT id,path,size,mtime FROM asset WHERE deleted=0 AND missing=0 AND excluded=0 "
                             "AND (hash IS NULL OR hash='')")
            rows = [r for r in rows if (r['id'], r['size'], r['mtime']) not in attempted
                    and not self.is_excluded(r['path'])]
            if not rows:
                return
            pending = []
            for row in rows:
                if interrupted.is_set():
                    break
                if self.is_excluded(row['path']):
                    continue
                try:
                    before = os.stat(row['path'])
                    if before.st_size != row['size'] or abs(before.st_mtime-row['mtime']) >= .000001:
                        attempted.add((row['id'], row['size'], row['mtime']))
                        continue
                    digest = file_hash(row['path'], row['size'], interrupted)
                    if interrupted.is_set():
                        break
                    if not digest:
                        self._retry(row['path'])
                        self._error(f"暂时无法读取文件：{row['path']}")
                    after = os.stat(row['path'])
                    if digest and (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns):
                        pending.append((digest, row['id'], row['size'], row['mtime'], row['path']))
                except OSError as error:
                    self._retry(row['path'])
                    self._error(str(error))
                attempted.add((row['id'], row['size'], row['mtime']))
                if len(pending) >= 128:
                    self._save_hashes(pending)
                    pending.clear()
            self._save_hashes(pending)

    def _save_hashes(self, pending):
        if pending:
            with self.db.batch():
                for values in pending:
                    if self.is_excluded(values[4]):
                        continue
                    self.db.execute("UPDATE asset SET hash=? WHERE id=? AND size=? AND mtime=? "
                                    "AND deleted=0 AND missing=0 AND excluded=0", values[:4])

    def _run(self, discover=False, initial=False, paths=None) -> None:
        self._set(
            running=True, phase="walking", total=0, done=0, added=0, updated=0,
            skipped=0, errors=0, message="正在查找文件…", current="",
            started_at=datetime.now().strftime(ISO), finished_at=None, error_log=[],
        )
        try:
            if discover:
                self.db.set_setting("drive_discovery_complete", "0")
                drives = fixed_drives()
                if not drives:
                    raise OSError("没有发现可扫描的本机磁盘")
                known = {_key(r["path"]) for r in self.db.list_roots()}
                for root in drives:
                    if self._stop.is_set():
                        break
                    if _key(root) not in known:
                        self.db.add_root(root)
                        known.add(_key(root))
                if not self._stop.is_set():
                    self.db.set_setting("drive_discovery_complete", "1")
            self._refresh_scopes()
            if paths is None:
                discover_media = getattr(sources, 'discover_media_roots', lambda: [])
                # Application discovery is relevant only to whole-volume scopes.
                # Explicit/test photo folders must not inspect the host's app stores.
                whole_volume = any(os.path.splitdrive(p)[1] in ('\\', '/') for p in self._root_paths)
                discovered = discover_media() if whole_volume else []
                with self.db.batch():
                    for item in discovered:
                        path = item['path']
                        if any(_within(path, root) for root in self._root_paths):
                            self.db.remember_media_root(path, item['source_app'])
                self._refresh_scopes()
            self._seen_paths, self._not_scanned = set(), []
            if paths is None:
                self._scan_files(self._iter_files([{'path': p} for p in self._root_paths]))
                self._reconcile(self._root_paths)
            else:
                self._scan_targets(paths)
            self._drain_pending()
            if not self._stop.is_set():
                # File paths/identities are durable now; enrichment can resume
                # from zero-width/empty-hash rows without repeating discovery.
                self._set(baseline_ready=True)
                if initial:
                    self.db.set_setting('initial_scan_complete', '1')
            self._set(phase='metadata', message='正在补齐媒体信息…')
            self._fill_metadata()
            if not self._stop.is_set():
                self._set(phase='hashing', message='媒体已就绪，正在后台校验重复文件…')
            while True:
                self._hash_background()
                with self._lock:
                    stopped = self._stop.is_set()
                    if self._pending_paths and not stopped:
                        continue
                    self.status.update(running=False, phase='paused' if stopped else 'done', current='',
                        message='已暂停，可继续扫描' if stopped else
                            f"完成：新增 {self.status['added']}，更新 {self.status['updated']}，失败 {self.status['errors']}",
                        finished_at=datetime.now().strftime(ISO))
                    break
        except Exception as exc:
            self._set(running=False, phase="error", message=f"扫描失败：{exc}")
            self._error(str(exc))

    def _process(self, root: str, path: str) -> str:
        if self.is_excluded(path):
            self._stat_cache.pop(_key(path), None)
            return 'skipped'
        stat = self._stat_cache.pop(_key(path), None)
        if stat is None:
            stat = os.stat(path)
        identity = self._identity(stat)
        with self.db.lock:
            if self._stop.is_set():
                return "cancelled"
            existing = self.db.find_by_path(path)
            if existing is None and identity:
                candidate = self.db.find_by_identity(identity)
                if candidate:
                    try:
                        os.stat(candidate['path'])
                    except FileNotFoundError:
                        existing = candidate
                    except OSError:
                        pass
        if existing and existing.get('deleted'):
            return 'skipped'
        source, source_root = sources.source_details(path)
        if source_root and not any(_within(source_root, scope) for scope in self._root_paths):
            source_root = root
        if source_root and _key(source_root) not in self._remembered_media:
            self.db.remember_media_root(source_root, source)
            self._remembered_media.add(_key(source_root))
        location = dict(path=path, root=root, dir=os.path.dirname(path), name=os.path.basename(path),
                        rel_path=os.path.relpath(path, root).replace('\\', '/'), file_id=identity,
                        source_app=source, source_root=source_root, missing=0, excluded=0,
                        ext=os.path.splitext(path)[1].lower())
        unchanged = (existing and int(existing['size']) == stat.st_size
                     and abs(float(existing['mtime'])-stat.st_mtime) < .000001)
        if unchanged:
            with self.db.lock:
                if self._stop.is_set():
                    return 'cancelled'
                if self.is_excluded(path):
                    return 'skipped'
                changes = {k: v for k, v in location.items() if existing.get(k) != v}
                if changes:
                    self.db.update_asset(existing['id'], **changes)
                    self._changed()
                    return 'updated'
            return 'skipped'

        ext = os.path.splitext(path)[1].lower()
        meta = (dict(width=0, height=0, taken_at=None, camera=None, gps_lat=None, gps_lon=None,
                     media_type='video' if ext in VIDEO_EXTS else 'image')
                if self._lightweight else probe(path, ext))
        if meta.get('io_error'):
            self._retry(path)
            self._error(meta['io_error'])
        if self._stop.is_set():
            return "cancelled"

        taken_at = meta["taken_at"] or datetime.fromtimestamp(stat.st_mtime).strftime(ISO)
        payload = {
            "root": root,
            "dir": os.path.dirname(path),
            "rel_path": os.path.relpath(path, root).replace("\\", "/"),
            "name": os.path.basename(path),
            "ext": ext,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "hash": None,
            "source_app": source,
            "source_root": source_root,
            "file_id": identity,
            "missing": 0,
            "excluded": 0,
            "width": meta["width"],
            "height": meta["height"],
            "taken_at": taken_at,
            "camera": meta["camera"],
            "gps_lat": meta["gps_lat"],
            "gps_lon": meta["gps_lon"],
            "media_type": meta["media_type"],
            "is_screenshot": 1 if is_screenshot(path) else 0,
            "favorite": 0,
            "deleted": 0,
        }

        with self.db.lock:
            if self._stop.is_set() or not self.db.q1("SELECT 1 FROM root WHERE path=?", (root,)):
                return "cancelled"
            if self.is_excluded(path):
                return 'skipped'
            if existing:
                asset_id = int(existing["id"])
                # Never let a re-scan reset user state (favorite / recycle bin).
                update = {k: v for k, v in payload.items() if k not in ("favorite", "deleted")}
                update['path'] = path
                self.db.update_asset(asset_id, **update)
                thumbs.remove(self.cache_dir, asset_id)
                outcome = "updated"
            else:
                payload["path"] = path
                asset_id = self.db.insert_asset(payload)
                outcome = "added"
            self._changed()
        return outcome
