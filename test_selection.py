"""Native Tk multi-selection with isolated photos and mocked OS file operations."""
from pathlib import Path
from types import SimpleNamespace
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from test_ui import make_library
from app import PhotoNestApp
from scanner import Scanner
from app import APP_VERSION
import ui


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="PhotoNest-selection-")
        self.root = Path(self.temp.name)
        self.db = make_library(self.root, 125)
        self.app = PhotoNestApp(self.db, Scanner(self.db, str(self.root/'cache')),
            str(self.root/'cache'), str(self.root), autostart=False)
        self.errors = []
        self.app.report_callback_exception = lambda *args: self.errors.append(args)
        self.app.update()

    def tearDown(self):
        self.app._on_close()
        self.temp.cleanup()

    def wait_files(self):
        deadline = time.monotonic()+10
        while self.app._file_busy and time.monotonic() < deadline:
            self.app.update()
            time.sleep(.01)
        self.assertFalse(self.app._file_busy)
        self.assertFalse(self.errors, self.errors)

    def click(self, asset_id, state=0):
        self.app._tiles[asset_id].event_generate('<Button-1>', x=30, y=30, state=state)

    def test_multiselect_is_explicit_and_controls_hide_after_exit(self):
        app = self.app
        first, second = [a['id'] for a in app.items[:2]]
        self.assertFalse(app.selection_delete_btn.winfo_manager())
        self.assertFalse(app.selection_bar.winfo_manager())
        with patch.object(app, '_open_lightbox') as opened:
            self.click(first)
        opened.assert_called_once_with(first)
        self.assertFalse(app.selected_ids)
        with patch('app.ui.ContextMenu') as constructor:
            app._photo_menu(SimpleNamespace(x_root=100,y_root=100), first)
            commands = [call.kwargs for call in constructor.return_value.add_command.call_args_list]
            self.assertFalse(app.selected_ids, 'Right click must not start selection mode')
            next(c for c in commands if c['label']=='多选')['command']()
        app.update()
        self.assertTrue(app.multi_select_mode)
        self.assertEqual(app.selected_ids, {first})
        self.assertEqual(app.selection_delete_btn.master, app.clear_selection_btn.master)
        self.assertEqual(app.selection_delete_btn.winfo_rooty(), app.clear_selection_btn.winfo_rooty())
        gap = app.clear_selection_btn.winfo_rootx() - (
            app.selection_delete_btn.winfo_rootx()+app.selection_delete_btn.winfo_width())
        self.assertTrue(0 <= gap <= 16, 'Delete and finish selection should be adjacent')
        self.assertTrue(app.selection_delete_btn.winfo_manager())
        self.assertTrue(app._tiles[second].find_withtag('selection-toggle'))
        self.click(second)
        self.assertEqual(app.selected_ids, {first, second})
        self.click(first)
        self.assertEqual(app.selected_ids, {second})
        app._clear_selection()
        self.assertFalse(app.multi_select_mode)
        self.assertFalse(app.selected_ids)
        self.assertFalse(app.selection_bar.winfo_manager())
        self.assertFalse(app.selection_delete_btn.winfo_manager())
        self.assertFalse(app._tiles[first].find_withtag('selection-toggle'))
        app._enter_multi_select()
        app._selection_escape(SimpleNamespace(widget=app._tiles[first]))
        self.assertFalse(app.multi_select_mode, 'Escape also exits multi-select with zero selected photos')

    def test_ctrl_shift_all_pagination_and_filtered_scope(self):
        app = self.app
        order = [a['id'] for a in app.items]
        app._enter_multi_select(order[0])
        self.click(order[2], 4)
        self.assertEqual(app.selected_ids, {order[0], order[2]})
        self.click(order[2], 4)
        self.assertEqual(app.selected_ids, {order[0]})
        self.click(order[6], 1)
        self.assertEqual(app.selected_ids, set(order[2:7]))
        app._select_all()
        self.assertEqual(len(app.selected_ids), 125)
        self.assertEqual(len(app.items), 120, 'Select all must not render every unloaded photo')
        app._load_more()
        self.assertEqual(len(app.selected_ids), 125)
        app._render_items()
        self.assertTrue(app._tiles[order[0]].find_withtag('selected'))
        app.show_view({'type':'search', 'q':'IMG_0000'})
        self.assertFalse(app.selected_ids)
        app._select_all()
        self.assertEqual(len(app.selected_ids), 1)
        before = set(app.selected_ids)
        self.assertIsNone(app._select_all(SimpleNamespace(widget=app.search_entry)))
        self.assertEqual(app.selected_ids, before)
        app._clear_selection()
        self.assertFalse(app.selected_ids)
        self.assertFalse(self.errors, self.errors)

    def test_context_menu_keeps_multiselection_and_bulk_delete_reports_partial_failure(self):
        app = self.app
        order = [a['id'] for a in app.items]
        first, second, outside = order[:3]
        app._enter_multi_select(first)
        self.click(second, 4)
        with patch('app.ui.ContextMenu') as constructor, patch.object(app, '_delete_photos') as delete:
            app._photo_menu(SimpleNamespace(x_root=100,y_root=100), first)
            commands = [call.kwargs for call in constructor.return_value.add_command.call_args_list]
            next(c for c in commands if c['label']=='将所选移到系统回收站')['command']()
            delete.assert_called_once_with(sorted([first, second]))
        with patch('app.ui.ContextMenu'):
            app._photo_menu(SimpleNamespace(x_root=100,y_root=100), outside)
        self.assertEqual(app.selected_ids, {outside})
        recycled = self.root/'fake-bin.jpg'
        first_path = Path(self.db.get_asset(first)['path'])
        second_path = Path(self.db.get_asset(second)['path'])
        def recycle(path, **kwargs):
            if path == str(second_path):
                raise PermissionError('locked fixture')
            os.replace(path, recycled)
            return str(recycled)
        with patch('app.desktop.recycle', side_effect=recycle), \
                patch('app.messagebox.askyesno', return_value=True) as confirm, \
                patch('app.messagebox.showerror') as error:
            app._delete_photos([first, second])
            self.wait_files()
        confirm.assert_called_once()
        error.assert_called_once()
        self.assertFalse(first_path.exists())
        self.assertTrue(second_path.exists())
        self.assertTrue(self.db.get_asset(first)['deleted'])
        self.assertFalse(self.db.get_asset(second)['deleted'])
        with patch('app.desktop.restore', side_effect=lambda token,path: os.replace(token,path)):
            app._restore_photos([first])
            self.wait_files()
        self.assertTrue(first_path.exists())
        self.assertFalse(self.db.get_asset(first)['deleted'])

    def test_popup_shares_button_typography_theme_and_releases_grab(self):
        app = self.app
        self.assertIn(APP_VERSION, app.title())
        app._set_theme('dark')
        app.focus_force()
        app.update()
        first = app.items[0]['id']
        opened = []
        with patch.object(app, '_open_lightbox', side_effect=lambda _: opened.append(app._context_menu.winfo_exists())):
            app._photo_menu(SimpleNamespace(x_root=app.winfo_screenwidth()-10,
                y_root=app.winfo_screenheight()-10), first)
            app.update()
            menu = app._context_menu
            self.assertEqual(menu.body.cget('bg'), ui.CARD)
            self.assertEqual(menu.entries[0].font.actual(), app.scan_btn.font.actual())
            self.assertTrue(any(e.variant == 'menu-danger' for e in menu.entries))
            self.assertLessEqual(menu.winfo_rooty()+menu.winfo_height(), app.winfo_screenheight())
            self.assertIs(app.grab_current(), menu)
            menu.entries[0]._invoke()
        self.assertEqual(opened, [0], 'Popup must close before its command opens a viewer/dialog')
        self.assertIsNone(app.grab_current())
        app._set_theme('light')
        app.focus_force()
        app._photo_menu(SimpleNamespace(x_root=100,y_root=100), first)
        app.update()
        self.assertEqual(app._context_menu.body.cget('bg'), ui.CARD)
        app.show_view({'type':'favorites'})
        self.assertIsNone(app._context_menu)
        self.assertFalse(self.errors, self.errors)


if __name__ == '__main__':
    unittest.main(verbosity=2)
