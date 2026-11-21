"""Exercise tray lifecycle through a fake Win32 message queue, never the desktop."""
import queue
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import tray


class FakeGui:
    error = RuntimeError
    NIF_ICON, NIF_MESSAGE, NIF_TIP = 1, 2, 4
    NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2

    def __init__(self, *, fail_add=False):
        self.messages = queue.Queue()
        self.calls = []
        self.fail_add = fail_add
        self.live = False
        self.unregistered = False
        self.destroyed_icons = []
        self.menu_command = 2

    def WNDCLASS(self):
        return SimpleNamespace()

    def RegisterClass(self, window_class):
        self.proc = window_class.lpfnWndProc

    def UnregisterClass(self, *args):
        self.unregistered = True

    def RegisterWindowMessage(self, name):
        return 0xC123

    def CreateWindow(self, *args):
        self.live = True
        return 42

    def LoadImage(self, *args):
        return 7

    def LoadIcon(self, *args):
        return 8

    def ExtractIconEx(self, *args):
        return [9], [7]

    def DestroyIcon(self, icon):
        self.destroyed_icons.append(icon)

    def Shell_NotifyIcon(self, mode, data):
        self.calls.append((mode, data))
        if mode == self.NIM_ADD and self.fail_add:
            raise RuntimeError('fake Shell denied icon creation')

    def PostMessage(self, hwnd, message, wparam, lparam):
        self.messages.put((hwnd, message, wparam, lparam))

    def PostQuitMessage(self, code):
        self.messages.put(None)

    def PumpMessages(self):
        while True:
            message = self.messages.get(timeout=5)
            if message is None:
                return
            self.proc(*message)

    def DestroyWindow(self, hwnd):
        self.live = False
        self.proc(hwnd, tray.win32con.WM_DESTROY, 0, 0)

    def IsWindow(self, hwnd):
        return self.live

    def DefWindowProc(self, *args):
        return 0

    def CreatePopupMenu(self):
        return 1

    def AppendMenu(self, *args):
        pass

    def GetCursorPos(self):
        return 10, 10

    def SetForegroundWindow(self, *args):
        pass

    def TrackPopupMenu(self, menu, flags, x, y, reserved, hwnd, rectangle):
        self.PostMessage(hwnd, tray.win32con.WM_COMMAND, self.menu_command, 0)

    def DestroyMenu(self, menu):
        pass


class TrayTests(unittest.TestCase):
    def setUp(self):
        allow = patch('tray._allow_shell_messages')
        self.addCleanup(allow.stop)
        self.allow_messages = allow.start()

    def wait_until(self, condition):
        deadline = time.monotonic() + 2
        while not condition() and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertTrue(condition())

    def test_lifecycle_callbacks_explorer_restart_and_close(self):
        gui = FakeGui()
        received = []
        def callback(action):
            received.append((action, threading.current_thread().name))
        icon = tray.TrayIcon(lambda: callback('open'), lambda: callback('pause'),
                             lambda: callback('exit'), 'PhotoNest.exe')
        with patch('tray.win32gui', gui), patch('tray.win32api.GetModuleHandle', return_value=1):
            try:
                self.assertTrue(icon.start())
                self.assertTrue(icon.available)
                self.allow_messages.assert_called_once_with(42, (icon._taskbar_created, icon._MESSAGE))
                gui.PostMessage(42, icon._MESSAGE, 0, tray.win32con.WM_LBUTTONUP)
                self.wait_until(lambda: len(received) == 1)
                self.assertEqual(received[0], ('open', 'photonest-tray'))
                gui.PostMessage(42, icon._MESSAGE, 0, tray.win32con.WM_RBUTTONUP)
                self.wait_until(lambda: len(received) == 2)
                self.assertEqual(received[-1][0], 'pause')
                icon.set_paused(True)
                self.wait_until(lambda: any(mode == gui.NIM_MODIFY for mode, _ in gui.calls))
                self.assertIn('已暂停', gui.calls[-1][1][-1])
                gui.PostMessage(42, icon._taskbar_created, 0, 0)
                self.wait_until(lambda: sum(mode == gui.NIM_ADD for mode, _ in gui.calls) == 2)
                gui.PostMessage(42, tray.win32con.WM_COMMAND, 3, 0)
                self.wait_until(lambda: len(received) == 3)
                self.assertEqual(received[-1][0], 'exit')
            finally:
                icon.close()
            self.assertFalse(icon.available)
            self.assertFalse(icon._thread.is_alive())
            self.assertTrue(gui.unregistered)
            self.assertTrue(any(mode == gui.NIM_DELETE for mode, _ in gui.calls))
            self.assertEqual(set(gui.destroyed_icons), {7, 9})
            icon.close()

    def test_creation_failure_prevents_hiding_and_recovery_failure_reopens(self):
        gui = FakeGui(fail_add=True)
        icon = tray.TrayIcon(lambda: None, lambda: None, lambda: None)
        with patch('tray.win32gui', gui), patch('tray.win32api.GetModuleHandle', return_value=1):
            self.assertFalse(icon.start())
            self.assertFalse(icon._thread.is_alive())
            self.assertTrue(icon.error)
            self.assertTrue(gui.unregistered)
        gui = FakeGui()
        opened = threading.Event()
        icon = tray.TrayIcon(opened.set, lambda: None, lambda: None)
        with patch('tray.win32gui', gui), patch('tray.win32api.GetModuleHandle', return_value=1):
            try:
                self.assertTrue(icon.start())
                gui.fail_add = True
                gui.PostMessage(42, icon._taskbar_created, 0, 0)
                self.assertTrue(opened.wait(timeout=2))
                self.assertFalse(icon.available)
            finally:
                icon.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
