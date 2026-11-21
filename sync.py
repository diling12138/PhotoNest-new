"""Coordinate durable startup catch-up and live directory notifications."""
from __future__ import annotations

import json
import os
import queue
import threading
import time

from changefeed import Journal, JournalCancelled, JournalGap, NativeDirectoryWatcher
from scanner import IMAGE_EXTS, VIDEO_EXTS, fixed_drives, priority_folders


def _key(path):
    return os.path.normcase(os.path.abspath(path))


class LibrarySync:
    def __init__(self, db, scanner, *, journal_factory=Journal, watcher_factory=NativeDirectoryWatcher):
        self.db, self.scanner = db, scanner
        self.journal_factory, self.watcher_factory = journal_factory, watcher_factory
        self._stop = threading.Event()
        self._thread = None
        self._events = queue.Queue(maxsize=20000)
        self._overflow = threading.Event()
        self._watcher = None
        self._watch_paths = ()
        self._availability = {}
        self._journals = {}
        self._pending = set(json.loads(db.get_setting('sync_pending', '[]')))
        self._inflight = {}
        self._versions = {path: 0 for path in self._pending}
        self._baseline = False
        self._baseline_paths = None
        self._bootstrap = {}
        self._last_journal = 0
        self._last_discovery = 0
        self._last_event = 0
        self._retry_at = 0
        self._watch_retry_at = 0
        self._finished_retries = None
        self._paused = db.get_setting('scan_paused', '0') == '1'
        self.status = {'phase': 'idle', 'message': '等待同步', 'journal_volumes': 0}

    def snapshot(self):
        return dict(self.status, paused=self._paused, pending=len(self._pending))

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name='photonest-sync', daemon=True)
        self._thread.start()

    def set_paused(self, paused):
        self._paused = bool(paused)
        self.db.set_setting('scan_paused', '1' if paused else '0')
        if paused:
            self.scanner.stop()

    def notify(self, event):
        try:
            self._events.put_nowait(event)
        except queue.Full:
            self._overflow.set()

    def close(self):
        self._stop.set()
        if self._watcher:
            self._watcher.close()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=15)

    def _save_pending(self):
        self.db.set_setting('sync_pending', json.dumps(sorted(self._pending), ensure_ascii=False))

    def _queue_retries(self, state, *, inflight=False):
        paths = {_key(path) for path in state.get('retry_paths', ())}
        if not paths:
            return
        for path in paths:
            self._pending.add(path)
            self._versions[path] = self._versions.get(path, 0)+1
            if inflight:
                # The current scan already attempted this work. Keep it durable
                # without repeatedly submitting it during metadata/hash work.
                self._inflight[path] = self._versions[path]
        self._save_pending()
        self._retry_at = time.monotonic()+30

    def _scopes(self):
        return self.db.root_paths()

    def _prepare(self):
        import sources
        sources.discover_media_roots()
        version = str(getattr(sources, 'RULE_VERSION', 1))
        if self.db.get_setting('source_rules_version') != version:
            self.db.reclassify_sources()
            self.db.set_setting('source_rules_version', version)
        scopes = self._scopes()
        if not scopes:
            scopes = fixed_drives()
        volumes = sorted({os.path.splitdrive(os.path.abspath(p))[0]+os.sep for p in scopes
                          if os.path.splitdrive(os.path.abspath(p))[0]})
        baseline = self.db.get_setting('sync_baseline') != '1' or self.db.get_setting('initial_scan_complete') != '1'
        global_baseline = baseline
        affected = set()
        for volume in volumes:
            try:
                journal = self.journal_factory(volume)
                snapshot = journal.snapshot()
                self._journals[volume] = journal
                saved = self.db.get_setting('sync_usn:'+_key(volume))
                if global_baseline or not saved:
                    self._bootstrap[volume] = snapshot
                    baseline = True
                    affected.update(p for p in scopes if os.path.splitdrive(p)[0].casefold() == os.path.splitdrive(volume)[0].casefold())
            except Exception:
                # A missing journal never means that the indexed files disappeared.
                baseline = True
                affected.update(p for p in scopes if os.path.splitdrive(p)[0].casefold() == os.path.splitdrive(volume)[0].casefold())
        self._baseline = baseline
        self._baseline_paths = None if global_baseline else affected
        if global_baseline:
            # Every available volume needs a pre-scan checkpoint, including upgrades.
            for volume, journal in self._journals.items():
                if volume not in self._bootstrap:
                    self._bootstrap[volume] = journal.snapshot()
        self._refresh_watches(scopes)
        self.status.update(phase='baseline' if baseline else 'catchup', journal_volumes=len(self._journals))

    def _refresh_watches(self, scopes=None, *, force=False):
        scopes = scopes if scopes is not None else self._scopes()
        candidates = []
        for scope in scopes:
            volume = os.path.splitdrive(os.path.abspath(scope))[0]+os.sep
            if _key(scope) == _key(volume) and volume in self._journals:
                # Known media trees provide immediate updates. Journal catch-up
                # discovers new media elsewhere without watching every system cache.
                candidates.extend(row['path'] for row in self.db.media_roots()
                                  if os.path.splitdrive(row['path'])[0].casefold() == os.path.splitdrive(scope)[0].casefold())
                candidates.extend(p for p in priority_folders()
                                  if os.path.splitdrive(p)[0].casefold() == os.path.splitdrive(scope)[0].casefold())
            else:
                candidates.append(scope)
        available = set()
        returned = set()
        for path in {*scopes, *candidates}:
            path = _key(path)
            exists = os.path.isdir(path)
            if exists:
                available.add(path)
                if self._availability.get(path) is False:
                    returned.add(path)
            self._availability[path] = exists
        if returned:
            # Registering a new watch cannot report files already present when
            # a formerly offline disk/media folder comes back.
            self._pending.update(returned)
            for path in returned:
                self._versions[path] = self._versions.get(path, 0)+1
            self._save_pending()
            self._last_event = time.monotonic()
        paths = tuple(sorted({_key(p) for p in candidates if _key(p) in available}))
        if len(paths) > 64:
            groups = {}
            for path in paths:
                groups.setdefault(os.path.splitdrive(path)[0], []).append(path)
            paths = tuple(os.path.commonpath(group) for group in groups.values())
        if paths == self._watch_paths and not force:
            return
        if self._watcher:
            self._watcher.close()
        self._watch_paths = paths
        if paths:
            self._watcher = self.watcher_factory(paths, self.notify)
            self._watcher.start()
            ready = getattr(self._watcher, 'ready', None)
            if ready is not None:
                ready.wait(timeout=2)

    def _journal_paths(self, volume, journal, events, snapshot):
        result = set()
        coalesced = {}
        for event in events:
            previous = coalesced.get(event['file_id'])
            item = dict(event)
            if previous:
                item['reason'] = item.get('reason', 0xffffffff) | previous.get('reason', 0xffffffff)
            coalesced[item['file_id']] = item
        for event in coalesced.values():
            if self._stop.is_set():
                raise InterruptedError('Sync stopped before persisting catch-up')
            is_dir = event.get('is_dir', False)
            reason = event.get('reason', 0xffffffff)
            # Directory timestamps change when children change. Those children
            # already have records; only structural directory changes need a subtree scan.
            if is_dir and not reason & (0x100 | 0x200 | 0x800 | 0x1000 | 0x2000):
                continue
            if not is_dir and not reason & (0x7 | 0x100 | 0x200 | 0x800 | 0x1000 | 0x2000 | 0x8000):
                continue
            media = os.path.splitext(event.get('name', ''))[1].lower() in IMAGE_EXTS | VIDEO_EXTS
            if not is_dir and not media and not reason & (0x200 | 0x1000 | 0x2000):
                continue
            identity = f"{snapshot['device']}:{event['file_id']}"
            previous = (self.db.directory_by_identity(identity) if is_dir else
                        self.db.find_by_identity(identity))
            if previous:
                result.add(previous['path'])
            if not is_dir and not media:
                continue
            path = event.get('path') or journal.resolve(event)
            if path and self.scanner.accepts_path(path):
                result.add(os.path.abspath(path))
            elif not path:
                if reason & 0x200:
                    continue  # The final deletion needs only its known old path.
                # Even known media can move while their new names are temporarily
                # inaccessible. Reconciling only the absent old name would lose
                # the new name after committing this journal cursor.
                parent = self.db.directory_by_identity(f"{snapshot['device']}:{event.get('parent_id')}")
                if parent:
                    result.add(parent['path'])
                else:
                    result.update(p for p in self._scopes() if os.path.splitdrive(p)[0].lower() == os.path.splitdrive(volume)[0].lower())
        return result

    def catch_up(self):
        """Persist work and the cursor in one transaction before handing off to scanning."""
        self.status.update(phase='catchup', message='正在读取文件变更日志…')
        for volume, journal in list(self._journals.items()):
            if self._stop.is_set():
                return
            saved = self.db.get_setting('sync_usn:'+_key(volume))
            if not saved:
                continue
            try:
                events, snapshot = journal.changes(json.loads(saved), stop=self._stop)
                paths = self._journal_paths(volume, journal, events, snapshot)
                # Path validation can encounter transient access errors too;
                # persist those jobs atomically with the journal checkpoint.
                paths.update(self.scanner.snapshot().get('retry_paths', ()))
            except (InterruptedError, JournalCancelled):
                return
            except Exception as error:
                # Gap/reset/access failure: reconcile before establishing a fresh baseline.
                self._baseline = True
                scopes = {p for p in self._scopes() if os.path.splitdrive(p)[0].casefold() == os.path.splitdrive(volume)[0].casefold()}
                if self._baseline_paths is None:
                    self._baseline_paths = scopes
                else:
                    self._baseline_paths.update(scopes)
                if isinstance(error, JournalGap):
                    try:
                        self._bootstrap[volume] = journal.snapshot()
                    except Exception:
                        self._journals.pop(volume, None)
                else:
                    # An unreadable log must not trigger an endless cycle of
                    # successful snapshot -> full scan -> failed log read.
                    # Reconcile once and use recursive notifications this session.
                    self._journals.pop(volume, None)
                    self._bootstrap.pop(volume, None)
                self.status['journal_volumes'] = len(self._journals)
                continue
            with self.db.batch():
                self._pending.update(paths)
                for path in paths:
                    self._versions[path] = self._versions.get(path, 0)+1
                self._save_pending()
                self.db.set_setting('sync_usn:'+_key(volume), json.dumps(snapshot))
        self._last_journal = time.monotonic()

    def _drain_events(self):
        changed = False
        for _ in range(20000):
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            path = event.get('path')
            if event.get('action') == 'unavailable':
                self._watch_retry_at = time.monotonic()+10
            if not path or not self.scanner.accepts_path(path):
                continue
            if event.get('action') == 'modified' and os.path.isdir(path) and not event.get('rescan'):
                continue
            if (event.get('rescan') or os.path.isdir(path)
                    or os.path.splitext(path)[1].lower() in IMAGE_EXTS | VIDEO_EXTS
                    or self.db.q1("SELECT 1 FROM scan_directory WHERE path=? COLLATE NOCASE", (path,))):
                self._pending.add(os.path.abspath(path))
                path = os.path.abspath(path)
                self._versions[path] = self._versions.get(path, 0)+1
                changed = True
        if self._overflow.is_set():
            self._overflow.clear()
            for scope in self._scopes():
                self._pending.add(scope)
                self._versions[scope] = self._versions.get(scope, 0)+1
            changed = True
        if changed:
            self._last_event = time.monotonic()
            self._save_pending()

    def _step(self):
        self._drain_events()
        if self._watch_retry_at and time.monotonic() >= self._watch_retry_at:
            self._watch_retry_at = 0
            self._refresh_watches(force=True)
        if self._paused:
            self.status.update(phase='paused', message='后台同步已暂停')
            return
        state = self.scanner.snapshot()
        if state['running'] and self._pending and time.monotonic()-self._last_event > .7:
            fresh = {p: self._versions.get(p, 0) for p in self._pending
                     if self._inflight.get(p) != self._versions.get(p, 0)}
            if fresh and self.scanner.start(paths=sorted(fresh)):
                self._inflight.update(fresh)
        if self._baseline:
            if state['running'] and not state.get('baseline_ready'):
                return
            if self.status.get('baseline_started'):
                if state['phase'] != 'done' and not state.get('baseline_ready'):
                    self.status.pop('baseline_started', None)
                    # Scanner.start clears its internal queue after cancellation.
                    # Retain durable work, but do not confirm those abandoned jobs
                    # when the replacement baseline eventually completes.
                    self._inflight.clear()
                    return
                with self.db.batch():
                    self._queue_retries(state, inflight=True)
                    for volume, snapshot in self._bootstrap.items():
                        self.db.set_setting('sync_usn:'+_key(volume), json.dumps(snapshot))
                    self.db.set_setting('sync_baseline', '1')
                self._baseline = False
                self._bootstrap.clear()
                self._baseline_paths = None
                self.status.pop('baseline_started', None)
                self.catch_up()
                self._refresh_watches()
            else:
                initial = self.db.get_setting('initial_scan_complete') != '1'
                options = dict(paths=sorted(self._baseline_paths)) if self._baseline_paths else dict(discover=not self._scopes(), initial=initial)
                if self.scanner.start(**options):
                    self.status.update(baseline_started=True, phase='baseline', message='正在建立增量同步基准…')
                return
        if self._inflight and not state['running']:
            if state['phase'] == 'done' and ('retry_paths' in state or not state.get('errors')):
                self._pending.difference_update(p for p, version in self._inflight.items()
                                                if self._versions.get(p, 0) == version)
                self._save_pending()
            else:
                self._retry_at = time.monotonic()+30
            self._inflight.clear()
        retry_token = (state.get('run_id'), tuple(sorted(state.get('retry_paths', ()))))
        if not state['running'] and retry_token != self._finished_retries:
            # Also harvest enrichment failures from a whole baseline, for which
            # there may never have been any targeted jobs in flight.
            with self.db.batch():
                self._queue_retries(state)
            self._finished_retries = retry_token
        if time.monotonic()-self._last_journal > 60 and not state['running']:
            self.catch_up()
        if self._pending and not state['running'] and time.monotonic() >= self._retry_at and time.monotonic()-self._last_event > .7:
            self._inflight = {p: self._versions.get(p, 0) for p in self._pending}
            if not self.scanner.start(paths=sorted(self._inflight)):
                self._inflight.clear()
        if time.monotonic()-self._last_discovery > 60:
            self._refresh_watches()
            self._last_discovery = time.monotonic()
        self.status.update(phase='watching', message='正在后台监听文件变化')

    def _run(self):
        try:
            self._prepare()
            if not self._baseline:
                self.catch_up()
                if not self._pending and not self._baseline and not self._paused:
                    self.scanner.start(paths=[])
            while not self._stop.is_set():
                try:
                    self._step()
                except Exception as error:
                    self.status.update(phase='error', message=f'后台同步暂不可用：{error}')
                    self._stop.wait(5)
                self._stop.wait(.3)
        except Exception as error:
            self.status.update(phase='error', message=f'后台同步未启动：{error}')
        finally:
            if self._watcher:
                self._watcher.close()
