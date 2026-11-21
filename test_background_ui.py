"""Background UI tests use a mocked tray and sync, plus synthetic animated media."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from PIL import Image

from app import PhotoNestApp
from scanner import Scanner
from test_ui import make_library


class BackgroundUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='PhotoNest-background-ui-')
        self.root = Path(self.temp.name)
        self.db = make_library(self.root, 1)
        self.scanner = Scanner(self.db, str(self.root/'cache'))
        self.app = None

    def tearDown(self):
        if self.app:
            self.app._on_close()
        else:
            self.db.close()
        self.temp.cleanup()

    def wait(self, condition, timeout=8):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            self.app.update()
            if condition():
                return
            time.sleep(.02)
        self.fail('Background UI did not reach expected state')

    def test_close_hides_to_tray_and_explicit_exit_closes_workers(self):
        with patch('app.LibrarySync') as sync, patch('app.TrayIcon') as tray:
            tray.return_value.start.return_value = True
            sync.return_value.snapshot.return_value = dict(paused=False, message='正在后台监听')
            self.app = PhotoNestApp(self.db, self.scanner, str(self.root/'cache'), str(self.root), background=True)
            self.app.update()
            self.app._hide_to_tray()
            self.assertEqual(self.app.state(), 'withdrawn')
            sync.return_value.close.assert_not_called()
            self.app._restore_from_tray()
            self.app.update()
            self.assertEqual(self.app.state(), 'normal')
            self.app._on_close()
            sync.return_value.close.assert_called_once()
            tray.return_value.close.assert_called_once()

    def test_gif_animates_without_locking_source_and_media_filter_works(self):
        path = self.root/'动画.gif'
        Image.new('RGB', (120,80), 'red').save(path, save_all=True,
            append_images=[Image.new('RGB', (120,80), 'blue')], duration=80, loop=0)
        info = path.stat()
        self.db.update_asset(1, path=str(path), dir=str(self.root), name=path.name, ext='.gif',
            width=120, height=80, size=info.st_size, mtime=info.st_mtime)
        self.app = PhotoNestApp(self.db, self.scanner, str(self.root/'cache'), str(self.root), autostart=False)
        self.app.show_view(dict(type='gifs'))
        self.assertEqual(len(self.app.items), 1)
        self.app._open_lightbox(1)
        self.wait(lambda: self.app._lb_preview is not None)
        colors = set()
        def animated():
            if self.app._lb_preview:
                colors.add(self.app._lb_preview.getpixel((0,0)))
            return len(colors)>1
        self.wait(animated)
        moved = self.root/'renamed.gif'
        path.rename(moved)
        moved.rename(path)
        self.app._pause_gif(reset=True)
        self.wait(lambda: self.app._lb_preview.getpixel((0,0)) == (255,0,0))
        self.assertFalse(self.app._gif_playing)
        self.app._close_lightbox()
        self.app.show_view(dict(type='videos'))
        self.assertEqual(self.app.items, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
