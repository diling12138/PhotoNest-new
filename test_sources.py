import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sources


class SourceTests(unittest.TestCase):
    def setUp(self):
        sources._discovered_roots.clear()

    def tearDown(self):
        sources._discovered_roots.clear()
        sources._personal_roots.cache_clear()

    def test_application_owner_is_inherited_from_upper_media_root(self):
        for path, label, root in (
            (r'D:\Tencent Files\123\nt_qq\nt_data\Pic\2026\11\a.jpg', 'QQ', r'D:\Tencent Files'),
            (r'D:\Weixin Files\user\FileStorage\Image\2026\a.jpg', '微信', r'D:\Weixin Files'),
            (r'D:\Steam\userdata\123\760\remote\game\screenshots\a.jpg', 'Steam', r'D:\Steam\userdata'),
            (r'D:\Photos\2026\11\holiday\a.gif', '本地照片', r'D:\Photos')):
            with self.subTest(path=path):
                self.assertEqual(sources.source_details(path), (label, root))

    def test_discovery_reads_custom_steam_media_path_and_wechat_appdata(self):
        import winreg
        with tempfile.TemporaryDirectory(prefix='PhotoNest-sources-') as folder:
            root = Path(folder)
            steam = root/'programs/Steam'
            (steam/'userdata/123/config').mkdir(parents=True)
            screenshots = root/'custom recordings'
            screenshots.mkdir()
            encoded = str(screenshots).replace('\\', '\\\\')
            (steam/'userdata/123/config/localconfig.vdf').write_text(
                '"ScreenshotPath" "'+encoded+'"', encoding='utf-8')
            wechat = root/'roaming/Tencent/Weixin'
            wechat.mkdir(parents=True)
            with patch('sources._personal_roots', return_value=[]), \
                    patch.dict(os.environ, {'ProgramFiles':str(root/'programs'),
                        'ProgramFiles(x86)':str(root/'programs'), 'APPDATA':str(root/'roaming'),
                        'LOCALAPPDATA':str(root/'local')}), \
                    patch.object(winreg, 'OpenKey', side_effect=OSError('test registry')):
                found = sources.discover_media_roots()
            mapping = {os.path.normcase(item['path']): item['source_app'] for item in found}
            self.assertEqual(mapping[os.path.normcase(str(screenshots))], 'Steam')
            self.assertEqual(mapping[os.path.normcase(str(wechat))], '微信')
            self.assertEqual(sources.source_for(str(screenshots/'clip.mp4')), 'Steam')

    def test_network_media_root_is_rejected_without_probing_directory(self):
        with patch('sources.os.path.isdir') as probe:
            self.assertFalse(sources._local_path(r'\\server\private\photos'))
            probe.assert_not_called()

    def test_resource_directories_are_identified_without_probing_files(self):
        resources = (
            r'C:\Windows\Web\Wallpaper\Windows\wallpaper.jpg',
            r'C:\ProgramData\Microsoft\User Account Pictures\user.png',
            r'C:\Program Files\Example App\resources\icons\icon.png',
            r'C:\Users\Alice\AppData\Local\Microsoft\Windows\Themes\CachedFiles\theme.jpg',
            r'C:\Users\Alice\AppData\Local\Packages\Microsoft.Windows.ContentDeliveryManager_x\LocalState\Assets\a.jpg',
            r'C:\Users\Alice\AppData\Local\Google\Chrome\User Data\Default\Cache\Cache_Data\web.jpg',
            r'C:\Users\Alice\AppData\Local\Microsoft\Edge\User Data\Profile 1\Code Cache\web.png',
            r'C:\Users\Alice\AppData\Local\Mozilla\Firefox\Profiles\user\cache2\web.jpg',
            r'C:\Users\Alice\AppData\Local\Example\app-1.2.3\resources\icon.png',
            r'D:\Steam\steamapps\common\Game\assets\icon.png',
            r'D:\Steam\appcache\librarycache\cover.jpg',
            r'D:\Steam\package\update\logo.png',
            r'D:\Steam\userdata\123\760\remote\42\screenshots\thumbnails\a.jpg',
            r'D:\QQNT\resources\app\logo.png',
            r'D:\Tencent Files\123\nt_qq\nt_data\Cache\generated.jpg',
            r'D:\WeChat\All Users\resources\icons\logo.png',
        )
        with patch('sources.os.stat', side_effect=AssertionError('Resource rules must be lexical')):
            for path in resources:
                with self.subTest(path=path):
                    self.assertTrue(sources.is_resource_path(path))

    def test_resource_rules_preserve_downloads_chat_media_and_steam_captures(self):
        user_media = (
            r'C:\Users\Alice\Downloads\a87bd0327.jpg',
            r'C:\Users\Alice\Downloads\wallpapers\setup.jpg',
            r'D:\Photos\cache\family.png',
            r'D:\Photos\icons\picture.png',
            r'D:\Photos\resources\tiny.gif',
            r'D:\Photos\Windows\holiday.jpg',
            r'D:\Tencent Files\123\nt_qq\nt_data\Pic\2026\11\a87bd0327.jpg',
            r'D:\Tencent Files\123\FileRecv\resources\photo.jpg',
            r'D:\Weixin Files\wxid\FileStorage\Image\2026\a.jpg',
            r'D:\xwechat_files\wxid\msg\attach\abcd\Img\a.gif',
            r'C:\Program Files (x86)\Steam\userdata\123\760\remote\42\screenshots\a.jpg',
            r'D:\SteamLibrary\steamapps\common\Game\screenshots\capture.jpg',
            r'D:\Steam\userdata\123\gamerecordings\clip.mp4',
            r'D:\Custom Steam Captures\clip.mp4',
        )
        for path in user_media:
            with self.subTest(path=path):
                self.assertFalse(sources.is_resource_path(path))


if __name__ == '__main__':
    unittest.main(verbosity=2)
