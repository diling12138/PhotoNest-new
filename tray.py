"""Native Windows notification icon. Callbacks must enqueue work for the UI."""
from __future__ import annotations

import os
import threading
import ctypes

import win32api
import win32con
import win32gui


def _allow_shell_messages(hwnd, messages):
    # PhotoNest may be elevated while Explorer is not. Whitelist only the
    # numeric tray/restart notifications for this hidden window, not the process.
    change = ctypes.WinDLL('user32', use_last_error=True).ChangeWindowMessageFilterEx
    change.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)
    change.restype = ctypes.c_int
    for message in messages:
        if not change(hwnd, message, 1, None):
            raise ctypes.WinError(ctypes.get_last_error())


class TrayIcon:
    _MESSAGE = win32con.WM_APP + 21
    _REFRESH = win32con.WM_APP + 22
    _ID = 1

    def __init__(self, on_open, on_pause, on_exit, icon_path=None):
        self.on_open, self.on_pause, self.on_exit = on_open, on_pause, on_exit
        self.icon_path = os.fspath(icon_path) if icon_path else ""
        self.available = False
        self.error = ""
        self._paused = False
        self._thread = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._hwnd = 0
        self._icon = 0
        self._owned_icon = False
        self._added = False
        self._taskbar_created = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return self.available
        self._ready.clear()
        self._stop.clear()
        self.error = ""
        self._thread = threading.Thread(target=self._run, name="photonest-tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3)
        if not self.available:
            self.close()
        return self.available

    def close(self):
        self._stop.set()
        self.available = False
        if self._hwnd:
            try:
                win32gui.PostMessage(self._hwnd, win32con.WM_CLOSE, 0, 0)
            except win32gui.error:
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def set_paused(self, paused):
        self._paused = bool(paused)
        if self._hwnd:
            try:
                win32gui.PostMessage(self._hwnd, self._REFRESH, 0, 0)
            except win32gui.error:
                pass

    def _call(self, callback):
        try:
            callback()
        except Exception as error:
            self.error = str(error)

    def _load_icon(self):
        if self.icon_path:
            try:
                if self.icon_path.lower().endswith(".exe"):
                    large, small = win32gui.ExtractIconEx(self.icon_path, 0, 1)
                    icons = small + large
                    if icons:
                        self._icon, self._owned_icon = icons[0], True
                        for extra in icons[1:]:
                            win32gui.DestroyIcon(extra)
                        return
                else:
                    self._icon = win32gui.LoadImage(0, self.icon_path, win32con.IMAGE_ICON,
                        0, 0, win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE)
                    if self._icon:
                        self._owned_icon = True
                        return
            except win32gui.error:
                pass
        self._icon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        self._owned_icon = False
        if not self._icon:
            raise OSError("无法创建 PhotoNest 托盘图标")

    def _data(self):
        tip = "PhotoNest · 已暂停扫描与识别" if self._paused else "PhotoNest · 后台扫描与识别"
        return (self._hwnd, self._ID,
                win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP,
                self._MESSAGE, self._icon, tip)

    def _add(self):
        result = win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, self._data())
        # pywin32 versions return either None (success) or a bool.
        if result is not None and not result:
            raise OSError("Windows 未能显示 PhotoNest 托盘图标")
        self._added = True
        self.available = True

    def _remove(self, hwnd):
        if self._added:
            self._added = False
            try:
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (hwnd, self._ID))
            except win32gui.error:
                pass

    def _menu(self, hwnd):
        menu = win32gui.CreatePopupMenu()
        try:
            win32gui.AppendMenu(menu, win32con.MF_STRING, 1, "打开 PhotoNest")
            win32gui.AppendMenu(menu, win32con.MF_STRING, 2,
                "继续扫描与识别" if self._paused else "暂停扫描与识别")
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
            win32gui.AppendMenu(menu, win32con.MF_STRING, 3, "退出程序")
            x, y = win32gui.GetCursorPos()
            win32gui.SetForegroundWindow(hwnd)
            win32gui.TrackPopupMenu(menu, win32con.TPM_RIGHTBUTTON, x, y, 0, hwnd, None)
            win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
        finally:
            win32gui.DestroyMenu(menu)

    def _wndproc(self, hwnd, message, wparam, lparam):
        if message == self._MESSAGE:
            if lparam in (win32con.WM_LBUTTONUP, win32con.WM_LBUTTONDBLCLK):
                self._call(self.on_open)
            elif lparam in (win32con.WM_RBUTTONUP, win32con.WM_CONTEXTMENU):
                try:
                    self._menu(hwnd)
                except win32gui.error as error:
                    self.error = str(error)
                    self._call(self.on_open)
            return 0
        if message == win32con.WM_COMMAND:
            callback = {1: self.on_open, 2: self.on_pause, 3: self.on_exit}.get(wparam & 0xffff)
            if callback:
                self._call(callback)
            return 0
        if message == self._taskbar_created or message == self._REFRESH:
            try:
                if message == self._taskbar_created:
                    self._added = False
                    self._add()
                elif self._added:
                    win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, self._data())
            except Exception as error:
                self.error = str(error)
                self.available = False
                self._call(self.on_open)  # Never leave a hidden app without a working tray.
            return 0
        if message == win32con.WM_QUERYENDSESSION:
            return 1
        if message == win32con.WM_ENDSESSION and wparam:
            self._call(self.on_exit)
            return 0
        if message == win32con.WM_CLOSE:
            self._remove(hwnd)
            win32gui.DestroyWindow(hwnd)
            return 0
        if message == win32con.WM_DESTROY:
            self.available = False
            self._remove(hwnd)
            self._hwnd = 0
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, message, wparam, lparam)

    def _run(self):
        instance = None
        class_name = f"PhotoNestTray_{id(self):x}"
        registered = False
        hwnd = 0
        was_ready = False
        try:
            instance = win32api.GetModuleHandle(None)
            window_class = win32gui.WNDCLASS()
            window_class.hInstance = instance
            window_class.lpszClassName = class_name
            window_class.lpfnWndProc = self._wndproc
            win32gui.RegisterClass(window_class)
            registered = True
            self._taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")
            # A hidden top-level window receives Explorer's restart broadcast;
            # a message-only window does not. No window is shown to the user.
            hwnd = self._hwnd = win32gui.CreateWindow(class_name, "PhotoNest tray", 0,
                0, 0, 0, 0, 0, 0, instance, None)
            _allow_shell_messages(hwnd, (self._taskbar_created, self._MESSAGE))
            self._load_icon()
            if self._stop.is_set():
                return
            self._add()
            was_ready = True
            self._ready.set()
            win32gui.PumpMessages()
        except Exception as error:
            self.error = str(error)
            if was_ready and not self._stop.is_set():
                self._call(self.on_open)
        finally:
            self.available = False
            self._remove(hwnd)
            try:
                if hwnd and win32gui.IsWindow(hwnd):
                    win32gui.DestroyWindow(hwnd)
            except win32gui.error:
                pass
            self._hwnd = 0
            try:
                if self._owned_icon and self._icon:
                    win32gui.DestroyIcon(self._icon)
            except win32gui.error:
                pass
            self._icon = 0
            self._owned_icon = False
            try:
                if registered:
                    win32gui.UnregisterClass(class_name, instance)
            except win32gui.error:
                pass
            self._ready.set()
