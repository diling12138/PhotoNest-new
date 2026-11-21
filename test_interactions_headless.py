"""Exercise the actual input routing methods without opening windows or loading models.

This complements, rather than replaces, test_ui.py's native Tk integration checks.
"""
import ast
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace, MethodType
import unittest
from unittest.mock import patch

import themes


class Widget:
    def __init__(self, master=None):
        self.master = master
        self.steps = []
        self.focused = False
    def focus_set(self):
        self.focused = True
    def yview_scroll(self, step, unit):
        self.steps.append((step, unit))


class Text(Widget):
    pass


class Combo(Widget):
    pass


class Entry(Widget):
    pass


class RoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = ast.parse(Path(__file__).with_name("app.py").read_text(encoding="utf-8"))
        app_class = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == "PhotoNestApp")
        names = ("_route_mousewheel", "_release_search_focus", "_end_search_input", "_viewer_key_nav",
                 "_sync_viewer_ocr", "_stash_viewer_ocr", "_open_ocr", "_save_ocr")
        methods = [n for n in app_class.body if isinstance(n, ast.FunctionDef) and n.name in names]
        scope = dict(tk=SimpleNamespace(Text=Text, Listbox=Text, Entry=Entry, TclError=RuntimeError),
                     ttk=SimpleNamespace(Combobox=Combo, Entry=Entry))
        exec(compile(ast.Module(body=methods, type_ignores=[]), "app.py", "exec"), scope)
        cls.methods = scope

    def setUp(self):
        self.entry = Entry()
        self.page, self.sidebar, self.editor = Widget(), Widget(), Text()
        self.target = Widget()
        self.app = SimpleNamespace(search_entry=self.entry, focus_get=lambda: self.entry,
            winfo_containing=lambda x, y: self.target, focus_set=lambda: setattr(self, "ended", True))
        self.event = SimpleNamespace(widget=self.entry, x_root=100, y_root=100, delta=-120)

    def test_click_outside_releases_entry_focus(self):
        self.event.widget = self.target
        self.methods["_release_search_focus"](self.app, self.event)
        self.assertTrue(self.target.focused)

    def test_click_inside_preserves_entry_focus(self):
        self.methods["_release_search_focus"](self.app, self.event)
        self.assertFalse(self.entry.focused)

    def test_escape_ends_search_input(self):
        self.assertEqual(self.methods["_end_search_input"](self.app), "break")
        self.assertTrue(self.ended)

    def test_wheel_uses_pointer_page_instead_of_focused_entry(self):
        self.target.master = Widget()
        self.target.master._wheel_target = self.page
        self.assertEqual(self.methods["_route_mousewheel"](self.app, self.event), "break")
        self.assertEqual(self.page.steps, [(1, "units")])
        self.assertEqual(self.entry.steps, [])

    def test_sidebar_and_text_have_independent_scroll_targets(self):
        self.target._wheel_target = self.sidebar
        self.methods["_route_mousewheel"](self.app, self.event)
        self.assertEqual(self.sidebar.steps, [(1, "units")])
        self.target = self.editor
        self.methods["_route_mousewheel"](self.app, self.event)
        self.assertEqual(self.editor.steps, [(1, "units")])
        self.assertEqual(self.page.steps, [])

    def test_wheel_outside_app_and_combobox_are_not_captured(self):
        self.target = None
        self.assertIsNone(self.methods["_route_mousewheel"](self.app, self.event))
        self.target = Combo()
        self.assertIsNone(self.methods["_route_mousewheel"](self.app, self.event))

    def test_small_wheel_delta_scrolls_once(self):
        self.target._wheel_target = self.page
        self.event.delta = -15
        self.methods["_route_mousewheel"](self.app, self.event)
        self.assertEqual(self.page.steps, [(1, "units")])

    def test_tcl_popdown_without_python_wrapper_uses_native_scroll(self):
        self.app.winfo_containing = lambda *args: (_ for _ in ()).throw(KeyError("popdown"))
        self.assertIsNone(self.methods["_route_mousewheel"](self.app, self.event))

    def test_editor_arrow_keys_do_not_change_photo(self):
        moves = []
        self.app._lb_nav = moves.append
        self.event.widget = self.editor
        self.methods["_viewer_key_nav"](self.app, self.event, -1)
        self.assertEqual(moves, [])
        self.event.widget = self.target
        self.methods["_viewer_key_nav"](self.app, self.event, -1)
        self.assertEqual(moves, [-1])


class OCRDraftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        RoutingTests.setUpClass.__func__(cls)

    def setUp(self):
        class Field:
            def __init__(self):
                self.value = ""
            def get(self, *args):
                return self.value
            def delete(self, *args):
                self.value = ""
            def insert(self, index, value):
                self.value = value
            def configure(self, **kwargs):
                pass
            def edit_reset(self):
                pass
            def edit_modified(self, *args):
                pass
            def set(self, value):
                self.value = value
        self.assets = {key: dict(id=key, size=100, mtime=1, media_type="image") for key in (1, 2)}
        self.results = {key: dict(text=f"photo {key}", ocr_text=f"photo {key}", blocks=[], ocr_done=1) for key in (1, 2)}
        self.app = SimpleNamespace(_ocr_asset_id=None, _ocr_file_stamp=None, _ocr_drafts={},
            _ocr_windows={}, lb_window=SimpleNamespace(winfo_exists=lambda: True),
            lb_ocr_text=Field(), lb_ocr_status=Field(), lb_ocr_title=Field(), lb_ocr_retry=Field(),
            lb_ocr_copy=Field(), lb_ocr_export=Field(), lb_ocr_save=Field(), _viewer_tab="info",
            ai=SimpleNamespace(store=SimpleNamespace(result=self.results.get)),
            db=SimpleNamespace(get_asset=self.assets.get), lb={"dirty": False}, lb_asset=self.assets[1])
        self.app._stash_viewer_ocr = MethodType(self.methods["_stash_viewer_ocr"], self.app)
        self.app._sync_viewer_ocr = MethodType(self.methods["_sync_viewer_ocr"], self.app)
        self.app._sync_viewer_ocr(self.assets[1])

    def test_unsaved_text_survives_photo_navigation(self):
        self.app.lb_ocr_text.value = "unsaved correction"
        self.app._sync_viewer_ocr(self.assets[2])
        self.assertEqual(self.app.lb_ocr_text.value, "photo 2")
        self.app._sync_viewer_ocr(self.assets[1])
        self.assertEqual(self.app.lb_ocr_text.value, "unsaved correction")

    def test_changed_file_does_not_restore_stale_draft(self):
        self.app.lb_ocr_text.value = "old correction"
        self.app._stash_viewer_ocr()
        self.assets[1]["size"] += 1
        self.results.pop(1)
        self.app._sync_viewer_ocr(self.assets[1])
        self.assertEqual(self.app.lb_ocr_text.value, "")
        self.assertFalse(self.app._ocr_loaded)

    def test_open_cached_ocr_keeps_unsaved_text(self):
        self.app.lb["dirty"] = True
        self.app.lb_ocr_text.value = "unsaved correction"
        self.app._show_viewer_tab = lambda *args, **kwargs: None
        self.app._request_viewer_ocr = lambda *args: self.fail("Cached OCR should not replace a draft")
        self.methods["_open_ocr"](self.app, 1)
        self.assertEqual(self.app.lb_ocr_text.value, "unsaved correction")

    def test_crop_cannot_overwrite_whole_photo_correction(self):
        self.app._ocr_crop = (.1, .1, .9, .9)
        self.app.ai.store.correct_text = lambda *args: self.fail("Crop must not replace full OCR")
        self.methods["_save_ocr"](self.app)


class SystemThemeTests(unittest.TestCase):
    def test_windows_app_theme_light_dark_and_missing_value(self):
        fake = SimpleNamespace(HKEY_CURRENT_USER=1, OpenKey=lambda *args: nullcontext(1),
                               QueryValueEx=lambda *args: (0, 4))
        with patch.dict("sys.modules", winreg=fake):
            self.assertTrue(themes.system_is_dark())
            fake.QueryValueEx = lambda *args: (1, 4)
            self.assertFalse(themes.system_is_dark())
            def missing(*args):
                raise OSError("not configured")
            fake.OpenKey = missing
            self.assertFalse(themes.system_is_dark())


if __name__ == "__main__":
    unittest.main(verbosity=2)
