"""Native Shell test. Deletes only photos created by this test itself."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from PIL import Image
import desktop


class RecycleTests(unittest.TestCase):
    def test_recycle_restore_and_scoped_purge(self):
        # Verify ordinary-user operations without opening an interactive UAC dialog.
        with tempfile.TemporaryDirectory(prefix="PhotoNest-owned-shell-test-") as folder, \
                patch('desktop.shellcon.FOFX_SHOWELEVATIONPROMPT', 0):
            root = Path(folder)
            first, other = root / "中文,照片.jpg", root / "保留照片.jpg"
            for path in (first, other):
                Image.new("RGB", (20, 15), "blue").save(path)
            token = sentinel = None
            try:
                try:
                    sentinel = desktop.recycle(str(other), recovery_dir=str(root/'recovery'))
                except OSError:
                    self.assertTrue(other.exists(), "A failed recycle must preserve the original")
                    self.skipTest("Windows Recycle Bin unavailable in this execution environment; cancellation preserved original")
                token = desktop.recycle(str(first), recovery_dir=str(root/'recovery'))
                self.assertFalse(first.exists())
                self.assertFalse(other.exists())
                with desktop._com():
                    self.assertIsNotNone(desktop._recycled_item(token))
                desktop.restore(token, str(first)); token = None
                self.assertTrue(first.exists())
                token = desktop.recycle(str(first), recovery_dir=str(root/'recovery'))
                desktop.purge(token)
                with desktop._com():
                    self.assertIsNone(desktop._recycled_item(token))
                    self.assertIsNotNone(desktop._recycled_item(sentinel))
                token = None
                desktop.restore(sentinel, str(other)); sentinel = None
                self.assertTrue(other.exists())
            finally:
                for pending in (token, sentinel):
                    if pending:
                        desktop.purge(pending)


if __name__ == "__main__":
    unittest.main(verbosity=2)
