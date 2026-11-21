"""Run with `python test_ui.py`; all files and changes stay in a temporary library."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from contextlib import nullcontext

import tkinter as tk
from PIL import Image, ImageDraw, ImageFont

from app import PhotoNestApp, ask_album_name
from db import DB
from scanner import Scanner
from ui import Button


def make_library(folder: Path, count: int = 137):
    folder.mkdir(parents=True, exist_ok=True)
    originals = folder / "旅行照片"
    originals.mkdir(exist_ok=True)
    db = DB(str(folder / "library.db"))
    # Ordinary UI fixtures represent an already initialized library. The first-run
    # integration test below explicitly mocks all drive discovery for an empty DB.
    db.set_setting("initial_scan_complete", "1")
    db.add_root(str(originals))
    for index in range(count):
        path = originals / f"IMG_{index:04}.jpg"
        im = Image.new("RGB", (480, 320), (90 + index % 120, 160, 210))
        draw = ImageDraw.Draw(im)
        draw.polygon([(0, 270), (140, 65), (240, 210), (350, 100), (480, 280)], fill=(50, 85 + index % 60, 95))
        draw.rectangle((0, 270, 480, 320), fill=(65, 140, 180))
        im.save(path)
        db.insert_asset({"path": str(path), "root": str(originals), "dir": str(originals),
                         "rel_path": path.name, "name": path.name, "ext": ".jpg",
                         "size": path.stat().st_size, "mtime": path.stat().st_mtime,
                         "width": 480, "height": 320, "media_type": "image",
                         "hash": "duplicate-pair" if index < 2 else str(index),
                         "taken_at": f"2026-{10 if index < 80 else 9:02}-18T08:42:00",
                         "deleted": 0, "favorite": index % 5 == 0, "is_screenshot": 0})
    album = db.create_album("山野之间")
    db.add_to_album(album["id"], [1, 2, 3])
    return db


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class InterfaceSmokeTest(unittest.TestCase):
    @unittest.skipUnless((Path(__file__).parent / "models/manifest.json").exists(), "Local model pack required")
    def test_ai_workflow(self):
        with tempfile.TemporaryDirectory(prefix="photonest-ai-ui-") as folder:
            root = Path(folder)
            db = make_library(root, count=1)
            asset = db.get_asset(1)
            image = Image.new("RGB", (1000, 450), "white")
            draw = ImageDraw.Draw(image)
            font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 54)
            draw.text((65, 45), "会议通知", font=font, fill="black")
            draw.text((65, 140), "项目报销发票", font=font, fill="black")
            image.save(asset["path"])
            info = Path(asset["path"]).stat()
            db.update_asset(1, size=info.st_size, mtime=info.st_mtime, width=1000, height=450)
            app = PhotoNestApp(db, Scanner(db, str(root / "cache")), str(root / "cache"), str(root))
            errors = []
            app.report_callback_exception = lambda *args: errors.append(args)

            def wait_until(condition, timeout=20):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    app.update()
                    if condition():
                        return
                    time.sleep(.02)
                self.fail("Timed out waiting for AI UI: " + str(app.ai.snapshot()))

            try:
                app.show_view({"type": "ai"})
                app._start_ai()
                wait_until(lambda: not app.ai.snapshot()["running"])
                self.assertEqual(app.ai.store.stats(app.ai.version)["done"], 1)
                app.search_mode.set("文字匹配")
                app.search_var.set("会议通知")
                app._apply_search()
                wait_until(lambda: "ids" in app.view)
                self.assertEqual([r["id"] for r in app.items], [1])
                app._open_lightbox(1)
                wait_until(lambda: app._lb_preview is not None)
                app._open_ocr()
                wait_until(lambda: not app._ocr_windows)
                self.assertFalse(any(isinstance(w, tk.Toplevel) for w in app.lb_window.winfo_children()))
                area = app.lb_ocr_text
                self.assertIn("会议通知", area.get("1.0", "end"))
                area.mark_set("insert", "1.0")
                app._highlight_ocr_line()
                self.assertEqual(len(app.lb_canvas.find_withtag("ocr-highlight")), 1)
                area.delete("1.0", "end")
                area.insert("1.0", "纠正后的项目会议")
                app._set_theme("dark")
                self.assertEqual(area.get("1.0", "end-1c"), "纠正后的项目会议")
                self.assertEqual(area.cget("bg"), "#191e27")
                self.assertEqual(app.lb_canvas.cget("bg"), "#101319")
                app._set_theme("light")
                self.assertEqual(area.get("1.0", "end-1c"), "纠正后的项目会议")
                app.lb_ocr_save._invoke()
                self.assertEqual(app.ai.store.text_matches("项目会议"), [1])
                destination = root / "识别结果.txt"
                with patch("app.filedialog.asksaveasfilename", return_value=str(destination)):
                    app.lb_ocr_export._invoke()
                self.assertEqual(destination.read_text(encoding="utf-8"), "纠正后的项目会议")
                app._show_viewer_tab("info")
                app._edit_tags()
                dialog = next(w for w in app.lb_window.winfo_children() if isinstance(w, tk.Toplevel))
                entry = next(w for w in descendants(dialog) if isinstance(w, tk.Entry))
                entry.delete(0, "end")
                entry.insert(0, "工作, 会议")
                next(w for w in descendants(dialog) if isinstance(w, Button) and w.text == "保存")._invoke()
                self.assertEqual(set(db.tags_for(1)), {"工作", "会议"})
                app._close_lightbox()
                app.show_view({"type": "tag", "name": "工作"})
                self.assertEqual(len(app.items), 1)
                self.assertFalse(errors, errors)
            finally:
                app._on_close()

    def test_library_workflow(self):
        with tempfile.TemporaryDirectory(prefix="photonest-ui-") as folder:
            root = Path(folder)
            recycled = {}
            def recycle(path, **kwargs):
                target = root / ("recycled-" + Path(path).name)
                os.replace(path, target)
                recycled[str(target)] = path
                return str(target)
            def restore(token, path):
                os.replace(token, path)
                recycled.pop(token)
            db = make_library(root)
            app = PhotoNestApp(db, Scanner(db, str(root / "cache")), str(root / "cache"), str(root))
            errors = []
            app.report_callback_exception = lambda *args: errors.append(args)

            def settle():
                start = time.monotonic()
                while time.monotonic() - start < 8:
                    app.update()
                    if (time.monotonic()-start > .4 and not app._resize_after and not app._thumb_pending and not app._file_busy
                            and (not app.lb or app._lb_preview is not None)):
                        break
                    time.sleep(0.01)
                app.update_idletasks()

            try:
                settle()
                self.assertEqual(len(app.items), 120)
                app._load_more()
                self.assertEqual(len(app.items), 137)
                self.assertFalse(app.has_more)
                for width in (1080, 1440, 1800, 1080):
                    app.geometry(f"{width}x780")
                    settle()
                    tiles = [w for w in app.grid_frame.winfo_children() if isinstance(w, tk.Canvas)]
                    self.assertTrue(tiles)
                    self.assertLessEqual(max(w.winfo_x() + w.winfo_width() for w in tiles), app.grid_frame.winfo_width() + 2)
                    self.assertEqual(len({w.winfo_width() for w in tiles}), 1,
                                     (width, app.winfo_geometry(), app.canvas.winfo_geometry(), app.grid_frame.winfo_geometry(),
                                      app._grid_columns, app._thumb_size, [(w.winfo_width(), w.cget("width")) for w in tiles[:6]]))
                    rendered = [app.scan_btn.itemcget(item, "text") for item in app.scan_btn.find_all()
                                if app.scan_btn.type(item) == "text"]
                    self.assertIn("扫描并识别", rendered)
                    self.assertEqual(round(app.canvas.canvasx(0)), 0)
                app.search_var.set("IMG_0001")
                app._apply_search()
                self.assertEqual(len(app.items), 1)
                app.show_view({"type": "all"})
                first = app.items[0]["id"]
                app._open_lightbox(first)
                settle()
                self.assertIsNotNone(app._lb_preview)
                self.assertIn(" / 120", app.lb_position.get())
                app._set_zoom(2.0)
                self.assertAlmostEqual(app._lb_display_rect[2], app._lb_preview.width*2)
                self.assertLessEqual(app.lb_canvas.photo.width(), app.lb_canvas.winfo_width())
                app._zoom_photo(1.25)
                self.assertAlmostEqual(app._image_scale(), 2.5)
                app._fit_lightbox()
                self.assertIsNone(app._zoom_scale)
                before = db.get_asset(first)["favorite"]
                app._lb_toggle_favorite()
                self.assertEqual(db.get_asset(first)["favorite"], not before)
                app._lb_nav(1)
                self.assertEqual(app.lb["index"], 1)
                app.lb_window.geometry("980x680")
                settle()
                self.assertGreater(app.lb_canvas.winfo_width(), 500)
                app._close_lightbox()
                self.assertIsNone(app.lb_window)
                app._open_settings()
                settle()
                self.assertEqual(app.view["type"], "settings")
                self.assertTrue(any(w.winfo_class() == "Treeview" for w in descendants(app.grid_frame)))
                app._open_duplicates()
                settle()
                self.assertEqual(len(db.duplicate_groups()), 1)
                self.assertIn("path", db.duplicate_groups()[0]["items"][0])
                with patch("app.messagebox.askyesno", return_value=True), patch("app.desktop.recycle", side_effect=recycle):
                    delete = next(w for w in descendants(app.grid_frame) if isinstance(w, Button) and w.text.startswith("保留第一张"))
                    delete._invoke()
                    settle()
                self.assertEqual(len(db.duplicate_groups()), 0)
                self.assertEqual(db.counts()["trash"], 1)
                self.assertEqual(len(list((root / "旅行照片").glob("*.jpg"))), 136)
                app.show_view({"type": "trash"})
                app._open_lightbox(app.items[0]["id"])
                settle()
                self.assertTrue(app.lb_restore_btn.winfo_ismapped())
                with patch("app.desktop.restore", side_effect=restore):
                    app._lb_restore()
                    settle()
                self.assertEqual(db.counts()["trash"], 0)
                self.assertFalse(recycled)

                def fill_album():
                    dialogs = [w for w in app.winfo_children() if isinstance(w, tk.Toplevel)]
                    dialog = dialogs[-1]
                    entry = next(w for w in descendants(dialog) if isinstance(w, tk.Entry))
                    entry.delete(0, "end")
                    entry.insert(0, "秋日山野")
                    next(w for w in descendants(dialog) if isinstance(w, Button) and w.text == "创建相册")._invoke()

                app.after(80, fill_album)
                self.assertEqual(ask_album_name(app), "秋日山野")
                app.after(80, lambda: next(w for w in descendants(app) if isinstance(w, Button) and w.text == "加入相册")._invoke())
                app._add_to_album([first])
                album = db.albums()[0]
                self.assertTrue((root / "albums" / f"{album['id']}.json").exists())
                payload = json.loads((root / "albums" / f"{album['id']}.json").read_text(encoding="utf-8"))
                self.assertTrue(payload["photos"])
                app.show_view({"type": "search", "q": "no-match"})
                self.assertEqual(app.items, [])
                app._start_scan()
                deadline = time.monotonic() + 15
                while app.scanner.snapshot()["running"] and time.monotonic() < deadline:
                    settle()
                self.assertFalse(app.scanner.snapshot()["running"])
                app._poll_scan()
                self.assertFalse(errors, errors)
            finally:
                app.scanner.stop()
                app._on_close()

    def test_first_use(self):
        with tempfile.TemporaryDirectory(prefix="photonest-empty-") as folder:
            db = DB(os.path.join(folder, "library.db"))
            app = PhotoNestApp(db, Scanner(db, folder), folder, folder, autostart=False)
            try:
                app.update()
                texts = [w.cget("text") for w in descendants(app.grid_frame) if isinstance(w, tk.Label)]
                self.assertIn("正在准备照片扫描", texts)
            finally:
                app._on_close()

    def test_first_open_scans_without_folder_dialog_and_remembers_completion(self):
        with tempfile.TemporaryDirectory(prefix="photonest-discovery-") as folder:
            root = Path(folder)
            photos = root / "drive" / "Pictures"
            photos.mkdir(parents=True)
            Image.new("RGB", (320, 240), "skyblue").save(photos / "自动发现.jpg")
            data = root / "data"
            db = DB(str(data / "library.db"))
            scanner = Scanner(db, str(data / "cache"))
            with patch("scanner.fixed_drives", return_value=[str(root / "drive")]) as discover, \
                    patch("scanner.priority_folders", return_value=[str(photos)]), \
                    patch("app.filedialog.askdirectory", side_effect=AssertionError("No folder picker on startup")):
                app = PhotoNestApp(db, scanner, str(data / "cache"), str(data))
                try:
                    deadline = time.monotonic() + 10
                    while db.get_setting("initial_scan_complete") != "1" and time.monotonic() < deadline:
                        app.update()
                        time.sleep(.02)
                    app._poll_scan()
                    self.assertEqual(db.counts()["active"], 1)
                    self.assertEqual(len(app.items), 1)
                    self.assertEqual(db.get_setting("initial_scan_complete"), "1")
                    self.assertEqual(discover.call_count, 1)
                    app._startup_scan()
                    self.assertEqual(discover.call_count, 1)
                    scanner._thread.join(5)
                    db.set_setting("initial_scan_complete", "0")
                    db.set_setting("scan_paused", "1")
                    app._startup_scan()
                    self.assertFalse(scanner.snapshot()["running"])
                    db.set_setting("scan_paused", "0")
                    app._startup_scan()
                    scanner._thread.join(5)
                    self.assertEqual(db.get_setting("initial_scan_complete"), "1")
                    self.assertEqual(db.counts()["active"], 1)
                    self.assertEqual(discover.call_count, 1)
                finally:
                    app._on_close()

    def test_focus_wheel_and_system_theme(self):
        with tempfile.TemporaryDirectory(prefix="photonest-input-") as folder:
            root = Path(folder)
            db = make_library(root, 20)
            with patch("themes.system_is_dark", return_value=False):
                app = PhotoNestApp(db, Scanner(db, folder), folder, folder)
            def settle():
                deadline = time.monotonic() + .25
                while time.monotonic() < deadline:
                    app.update()
                    time.sleep(.01)
            try:
                app.geometry("1080x680")
                settle()
                self.assertEqual(app.theme_choice, "system")
                app.search_entry.focus_force()
                settle()
                app.main.event_generate("<Button-1>", x=20, y=20)
                settle()
                self.assertIsNot(app.focus_get(), app.search_entry)
                app.search_entry.focus_force()
                app.search_entry.event_generate("<Escape>")
                settle()
                self.assertIsNot(app.focus_get(), app.search_entry)
                app.search_entry.focus_force()
                app.canvas.yview_moveto(0)
                # Background test launches can leave a native window minimized at -32000.
                # Resolve the pointer target deterministically while retaining real Tk bindtags.
                x, y = app.canvas.winfo_rootx()+100, app.canvas.winfo_rooty()+100
                target = app.winfo_containing(x, y)
                resolver = nullcontext() if target else patch.object(app, "winfo_containing", return_value=app.grid_frame)
                with resolver:
                    app.search_entry.event_generate("<MouseWheel>", delta=-120, rootx=x, rooty=y)
                settle()
                self.assertGreater(app.canvas.yview()[0], 0)
                app.show_view({"type": "settings"})
                app.theme_radios["dark"].invoke()
                self.assertTrue(app.is_dark)
                self.assertEqual(db.get_setting("theme"), "dark")
                with patch("themes.system_is_dark", return_value=False):
                    app._poll_theme()
                    self.assertTrue(app.is_dark)
                    app._set_theme("system")
                    self.assertFalse(app.is_dark)
                with patch("themes.system_is_dark", return_value=True):
                    app._poll_theme()
                    self.assertTrue(app.is_dark)
            finally:
                app._on_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
