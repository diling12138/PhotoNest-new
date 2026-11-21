"""Application media ownership and bounded discovery of user media roots."""
from __future__ import annotations
import ntpath
import os
import re
import ctypes
from functools import lru_cache

RULE_VERSION = 2
_APP_KEYS = (
    ("微信", {"wechat", "wechat files", "weixin", "weixin files", "xwechat", "xwechat_files", "微信"}),
    ("QQ", {"tencent files", "qq files", "nt_qq", "ntqq", "qq", "qqfile_recv", "qq图片"}),
    ("Steam", {"steam", "steamlibrary", "steam library"}),
    ("Telegram", {"telegram", "telegram desktop"}),
    ("钉钉", {"dingtalk", "钉钉"}),
    ("飞书", {"feishu", "lark", "飞书"}),
    ("企业微信", {"wxwork", "wecom", "企业微信"}),
    ("截图", {"screenshots", "screenshot", "snipaste", "截图", "屏幕截图", "截屏"}),
    ("浏览器下载", {"downloads", "download", "chrome downloads", "edge downloads", "下载"}),
    ("相机照片", {"dcim", "camera", "camera roll", "相机胶卷"}),
)
_discovered_roots = {}

_RESOURCE_BRANCHES = {
    'cache', 'caches', 'appcache', 'htmlcache', 'webcache', 'browsercache',
    'code cache', 'gpucache', 'gpu cache', 'shadercache', 'grshadercache',
    'dawncache', 'dawnwebgpucache', 'dawngraphitecache', 'qtwebengine',
    'resources', 'resource', 'res', 'icons', 'icon', 'avatarcache',
    'avatars', 'headimage', 'headpic', 'package', 'packages', 'update',
    'updates', 'updater', 'installer', 'installers', 'setup', 'squirreltemp',
}
_BROWSERS = {'chrome', 'chromium', 'edge', 'msedge', 'brave-browser', 'vivaldi', 'qqbrowser'}


def is_resource_path(path):
    """Recognize resource directories; never infer ownership from image contents/names.

    This predicate does no filesystem I/O and accepts both file and directory paths.
    Cache-like names outside a known system/application tree remain user media.
    """
    _, tail = ntpath.splitdrive(ntpath.normpath(os.fspath(path).replace('/', '\\')))
    parts = [part.casefold() for part in re.split(r'[\\/]+', tail) if part]
    if not parts:
        return False
    if parts[0] in {'windows', 'programdata', '$recycle.bin', 'system volume information',
                    'recovery', 'perflogs', 'msocache'}:
        return True

    # User-created Steam captures and chat attachments can live inside an app
    # installation. Admit those media trees before the installation-root rule.
    for index, part in enumerate(parts):
        after = parts[index+1:]
        if part in {'steam', 'steamlibrary', 'steam library'}:
            captures = [i for i, value in enumerate(after)
                        if value in {'screenshots', 'recordings', 'gamerecordings'}]
            if captures:
                return 'thumbnails' in after[min(captures)+1:]
            if after and after[0] == 'userdata':
                if set(after[1:]) & (_RESOURCE_BRANCHES | {'config', 'thumbnails'}):
                    return True
                return False
            if after and after[0] in (_RESOURCE_BRANCHES | {'steamapps', 'bin', 'clientui', 'steamui', 'steam'}):
                return True
        is_chat = part in {
            'tencent files', 'qq files', 'nt_qq', 'ntqq', 'qqnt', 'qq', 'qqfile_recv',
            'wechat', 'wechat files', 'weixin', 'weixin files', 'xwechat', 'xwechat_files',
            '微信', 'wxwork', 'wecom', 'dingtalk', 'feishu', 'lark', 'telegram', 'telegram desktop',
        } or part.startswith(('tencent.wechat_', 'tencent.wechatforwindows_'))
        if is_chat:
            pairs = set(zip(after, after[1:]))
            if (part == 'qqfile_recv' or 'qqfile_recv' in after or 'filerecv' in after
                    or 'file_recv' in after
                    or pairs & {('nt_data', 'pic'), ('nt_data', 'video'), ('nt_data', 'file'),
                                ('filestorage', 'image'), ('filestorage', 'video'), ('filestorage', 'file'),
                                ('msg', 'attach'), ('msg', 'video'), ('msg', 'file')}):
                return False
            if set(after) & _RESOURCE_BRANCHES:
                return True

    for index, part in enumerate(parts):
        after = parts[index+1:]
        if part in _BROWSERS and 'user data' in after:
            profile = after[after.index('user data')+1:]
            if (set(profile) & _RESOURCE_BRANCHES
                    or ('service worker' in profile and 'cachestorage' in profile)):
                return True
        if part == 'mozilla' and after[:1] == ['firefox']:
            if set(after[1:]) & {'cache2', 'startupcache', 'thumbnails', 'jumplistcache'}:
                return True

    if parts[0] in {'program files', 'program files (x86)', 'windowsapps'}:
        return True
    if 'appdata' in parts:
        after = parts[parts.index('appdata')+1:]
        if after[:2] == ['local', 'programs']:
            return True
        if after[:3] in (['local', 'microsoft', 'windows'], ['roaming', 'microsoft', 'windows']):
            if len(after) > 3 and after[3] in {'themes', 'explorer', 'caches'}:
                return True
        if after[:3] == ['local', 'microsoft', 'bingwallpaper']:
            return True
        if after[:2] == ['local', 'packages'] and len(after) > 2:
            package = after[2]
            if package.startswith(('microsoft.windows.contentdeliverymanager_', 'microsoft.windows.shellassets_')):
                return True
        # Versioned application/update installs under Local, not arbitrary
        # folders called "resources" in a user's Downloads or photo albums.
        if after[:1] == ['local'] and any(re.fullmatch(r'app-\d[\w.\-]*', p) for p in after[1:]):
            if set(after[1:]) & _RESOURCE_BRANCHES:
                return True
    return False


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def _within(path, root):
    try:
        return os.path.commonpath((_key(path), _key(root))) == _key(root)
    except ValueError:
        return False


def _local_path(path):
    drive = ntpath.splitdrive(os.path.abspath(path))[0]
    if drive.startswith('\\\\'):
        return False
    if os.name == 'nt':
        function = ctypes.windll.kernel32.GetDriveTypeW
        function.argtypes = [ctypes.c_wchar_p]
        return bool(drive) and function(drive+'\\') == 3
    return True


@lru_cache(maxsize=1)
def _personal_roots():
    home = os.path.expanduser("~")
    values = [os.path.join(home, name) for name in ("Pictures", "Desktop", "Downloads", "Documents", "Videos")]
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                for name in ("My Pictures", "Desktop", "Personal", "My Video", "{374DE290-123F-4565-9164-39C4925E467B}"):
                    try:
                        values.append(os.path.expandvars(winreg.QueryValueEx(key, name)[0]))
                    except OSError:
                        pass
        except OSError:
            pass
    return list(dict.fromkeys(os.path.abspath(p) for p in values))


def source_details(path):
    path = os.fspath(path)
    matching = [(root, label) for root, label in list(_discovered_roots.items()) if _within(path, root)]
    if matching:
        root, label = max(matching, key=lambda item: len(item[0]))
        return label, root
    module = ntpath if ntpath.splitdrive(path)[0] else os.path
    drive, tail = module.splitdrive(path)
    separator = "\\" if module is ntpath else os.sep
    parts = [p for p in re.split(r"[\\/]+", tail) if p]
    folders = parts[:-1]
    lowered = [part.casefold() for part in folders]
    for label, keys in _APP_KEYS:
        for index, part in enumerate(lowered):
            if part in keys or (label == "微信" and part.startswith(("tencent.wechat_", "tencent.wechatforwindows_"))):
                end = index+1
                if label == "Steam" and "userdata" in lowered[index+1:]:
                    end = lowered.index("userdata", index+1)+1
                root = module.join(drive+separator if drive or path.startswith(separator) else "", *folders[:end])
                return label, root
    for root in sorted(_personal_roots(), key=len, reverse=True):
        if _within(path, root):
            return "本地照片", root
    if folders:
        # Keep a useful library root rather than one watcher per month/day.
        end = 3 if len(folders) >= 3 and lowered[0] == "users" else 1
        return "本地照片", module.join(drive+separator if drive or path.startswith(separator) else "", *folders[:end])
    return "本地照片", module.dirname(path)


def source_for(path):
    return source_details(path)[0]


def _vdf_paths(path, keys):
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as source:
            text = source.read(2*1024*1024)
        pairs = re.findall(r'"([^"\r\n]+)"\s*"([^"\r\n]+)"', text)
        for name, value in pairs:
            if name.casefold() in keys:
                value = value.replace("\\\\", "\\")
                if os.path.isabs(value):
                    yield os.path.normpath(value)
    except OSError:
        return


def discover_media_roots():
    """Read known app storage locations; never crawl installation trees."""
    _personal_roots.cache_clear()
    candidates = []
    home = os.path.expanduser("~")
    for root in _personal_roots():
        for label, names in (("QQ", ("Tencent Files", "QQ Files")),
                ("微信", ("WeChat Files", "Weixin Files", "xwechat_files")),
                ("Telegram", ("Telegram Desktop",))):
            candidates.extend((os.path.join(root, name), label) for name in names)
    for data in {os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local")),
                 os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))}:
        for label, names in (("QQ", ("QQ", "NTQQ", "nt_qq")),
                ("微信", ("WeChat", "Weixin", "xwechat", "xwechat_files")),
                ("企业微信", ("WXWork",))):
            candidates.extend((os.path.join(data, "Tencent", name), label) for name in names)
    installations = {os.path.join(os.environ.get(variable, "C:\\Program Files (x86)"), "Steam")
                     for variable in ("ProgramFiles(x86)", "ProgramFiles")}
    if os.name == "nt":
        try:
            import winreg
            for hive, key_name, field in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                    (winreg.HKEY_LOCAL_MACHINE, r"Software\Valve\Steam", "InstallPath"),
                    (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Valve\Steam", "InstallPath")):
                try:
                    with winreg.OpenKey(hive, key_name) as key:
                        installations.add(winreg.QueryValueEx(key, field)[0])
                except OSError:
                    pass
        except ImportError:
            pass
    for installation in list(installations):
        if not _local_path(installation):
            continue
        installations.update(_vdf_paths(os.path.join(installation, "steamapps", "libraryfolders.vdf"), {"path"}))
    for installation in installations:
        if not _local_path(installation):
            continue
        userdata = os.path.join(installation, "userdata")
        candidates.append((userdata, "Steam"))
        configs = [os.path.join(installation, "config", "config.vdf")]
        try:
            with os.scandir(userdata) as entries:
                for index, entry in enumerate(entries):
                    if index >= 1024:
                        break
                    if entry.is_dir(follow_symlinks=False):
                        configs.append(os.path.join(entry.path, "config", "localconfig.vdf"))
        except OSError:
            pass
        for config in configs:
            candidates.extend((path, "Steam") for path in _vdf_paths(config,
                {"screenshotpath", "screenshotfolder", "recordingpath", "recordingfolder", "recordingspath", "recordingsfolder"}))
    found = {}
    for path, label in candidates:
        path = os.path.abspath(path)
        if _local_path(path) and os.path.isdir(path) and not os.path.islink(path) and not getattr(os.path, "isjunction", lambda p: False)(path):
            found[_key(path)] = label
    _discovered_roots.clear()
    _discovered_roots.update(found)
    return [dict(path=path, source_app=label) for path, label in sorted(found.items())]
