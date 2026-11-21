"""PhotoNest appearance preferences; uses the Windows app theme without extra packages."""
import ctypes

CHOICES = (("system", "跟随系统"), ("light", "浅色"), ("dark", "深色"))


def system_is_dark():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except (ImportError, OSError):
        return False


def title_bar(window, dark):
    try:
        handle = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(bool(dark))
        ctypes.windll.dwmapi.DwmSetWindowAttribute(handle, 20, ctypes.byref(value), 4)
    except (AttributeError, OSError):
        pass
