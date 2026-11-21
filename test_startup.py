"""Startup permission checks mock UAC; never open an authorization dialog."""
from contextlib import nullcontext
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import app
import desktop


class StartupTests(unittest.TestCase):
    def test_frozen_launch_requests_once_before_opening_library(self):
        data = str(Path.cwd()/'existing library 中文')
        with patch.object(sys, 'argv', ['PhotoNest.exe']), \
                patch.object(sys, 'frozen', True, create=True), \
                patch('app.choose_data_dir', return_value=(data, 'app', [data])), \
                patch('desktop.is_administrator', return_value=False), \
                patch('desktop._com', return_value=nullcontext()), \
                patch('desktop.shell.ShellExecuteEx') as launch, \
                patch('app.DB') as database, patch('app.PhotoNestApp') as window, \
                patch('app.message_box') as message:
            result = app.main()
            self.assertEqual(result, 0, message.call_args)
        launch.assert_called_once()
        options = launch.call_args.kwargs
        self.assertEqual(options['lpVerb'], 'runas')
        self.assertEqual(options['lpFile'], sys.executable)
        self.assertEqual(options['lpParameters'], '--data "'+data+'"')
        database.assert_not_called()
        window.assert_not_called()

    def test_source_launch_keeps_script_and_explicit_library(self):
        data = str(Path.cwd()/'explicit library 中文')
        with patch.object(sys, 'argv', ['app.py', '--data', 'relative library']), \
                patch.object(sys, 'frozen', False, create=True), \
                patch('app.choose_data_dir', return_value=(data, 'argument', [data])), \
                patch('desktop.is_administrator', return_value=False), \
                patch('desktop.launch_elevated') as launch:
            self.assertEqual(app.main(), 0)
        launch.assert_called_once_with(sys.executable,
            [str(Path(app.__file__).resolve()), '--data', data], app.app_dir())

    def test_declining_authorization_exits_without_retry_or_opening_ui(self):
        cancelled = OSError('UAC cancelled')
        cancelled.winerror = 1223
        with patch.object(sys, 'argv', ['PhotoNest.exe']), \
                patch('app.choose_data_dir', return_value=('C:\\library', 'app', [])), \
                patch('desktop.is_administrator', return_value=False), \
                patch('desktop.launch_elevated', side_effect=cancelled) as launch, \
                patch('app.DB') as database, patch('app.PhotoNestApp') as window, \
                patch('app.message_box') as message:
            self.assertEqual(app.main(), 0)
        launch.assert_called_once()
        database.assert_not_called()
        window.assert_not_called()
        message.assert_not_called()

    def test_elevated_instance_opens_once_without_requesting_again(self):
        with tempfile.TemporaryDirectory(prefix='PhotoNest-startup-') as folder:
            with patch.object(sys, 'argv', ['PhotoNest.exe']), \
                    patch.object(sys, 'frozen', True, create=True), \
                    patch('app.choose_data_dir', return_value=(folder, 'argument', [folder])), \
                    patch('app.setup_logging'), \
                    patch('desktop.is_administrator', return_value=True), \
                    patch('desktop.launch_elevated') as launch, \
                    patch('app.DB'), patch('app.Scanner'), patch('app.PhotoNestApp') as window:
                self.assertEqual(app.main(), 0)
            launch.assert_not_called()
            window.assert_called_once()
            window.return_value.mainloop.assert_called_once()


if __name__ == '__main__':
    unittest.main(verbosity=2)
