"""Ancestor selection and safe recovery use only an owned temporary library."""
from pathlib import Path
import os
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import Mock, patch

from app import PhotoNestApp, folder_ancestors
from scanner import Scanner
from test_ui import make_library
from ui import Button


class IgnoreScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='PhotoNest-ignore-scope-')
        self.root = Path(self.temp.name)
        self.db = make_library(self.root, 1)
        self.scanner = Scanner(self.db, str(self.root/'cache'))
        self.app = PhotoNestApp(self.db, self.scanner, str(self.root/'cache'), str(self.root), autostart=False)

    def tearDown(self):
        self.app._on_close()
        self.temp.cleanup()

    @staticmethod
    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from IgnoreScopeTests.descendants(child)

    def test_picker_selects_nonadjacent_parent_and_cancel_keeps_rules(self):
        folder = self.root/'QQ'/'account'/'months'/'2026-11'/'Ori'
        selected = self.root/'QQ'
        failures = []
        def choose(confirm):
            dialog = next(w for w in self.app.winfo_children() if isinstance(w, tk.Toplevel))
            try:
                widgets = list(self.descendants(dialog))
                chooser = next(w for w in widgets if isinstance(w, ttk.Combobox))
                self.assertIn(str(selected), chooser['values'])
                self.assertGreater(folder_ancestors(str(folder)).index(str(selected)), 1)
                chooser.set(str(selected))
                chooser.event_generate('<<ComboboxSelected>>')
                self.app.update_idletasks()
                button = next(w for w in widgets if isinstance(w, Button)
                              and w.text == ('忽略所选范围' if confirm else '取消'))
                self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),
                                     dialog.winfo_rooty()+dialog.winfo_height())
                button.command()
            except Exception as error:
                failures.append(error)
                dialog.destroy()
        self.app.after(40, lambda: choose(True))
        self.assertEqual(self.app._choose_ignore_folder(str(folder)), str(selected))
        self.app.after(40, lambda: choose(False))
        self.assertIsNone(self.app._choose_ignore_folder(str(folder)))
        self.assertFalse(failures, failures)
        self.assertEqual(self.db.scan_exclude_paths(), [])
        ancestors = folder_ancestors(str(folder))
        self.assertEqual(len(set(ancestors)), len(ancestors))
        self.assertEqual(os.path.dirname(ancestors[-1]), ancestors[-1])

    def test_ignore_selected_ancestor_hides_all_descendants_and_preserves_files(self):
        asset = self.db.get_asset(1)
        nested = self.root/'QQ'/'account'/'months'/'Ori'
        nested.mkdir(parents=True)
        path = nested/'photo.jpg'
        path.write_bytes(Path(asset['path']).read_bytes())
        self.db.update_asset(1, path=str(path), dir=str(nested))
        with patch.object(self.app, '_choose_ignore_folder', return_value=str(self.root/'QQ')):
            self.app._ignore_photo_folder(self.db.get_asset(1))
        self.assertEqual(self.db.scan_exclude_paths(), [str(self.root/'QQ')])
        self.assertTrue(self.db.get_asset(1)['excluded'])
        self.assertTrue(path.exists())
        self.assertFalse(self.scanner.accepts_path(str(path)))

    def test_restore_parent_outside_scope_queues_only_configured_scope(self):
        self.db.remove_root(self.db.root_paths()[0])
        scope = self.root/'Photos'/'Family'
        self.db.add_root(str(scope))
        self.app.sync = Mock()
        self.app._rescan_included_paths([str(self.root/'Photos')])
        self.app.sync.notify.assert_called_once_with(dict(path=str(scope), rescan=True, action='included'))
        self.app.sync.notify.reset_mock()
        self.app._rescan_included_paths([str(self.root/'Other')])
        self.app.sync.notify.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
