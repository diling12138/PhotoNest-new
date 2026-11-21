"""Image ownership checks using temporary synthetic files only."""
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from ai_engine import read_image


class ImageReadTests(unittest.TestCase):
    def test_rgb_and_transparency_are_detached_from_closed_source(self):
        with tempfile.TemporaryDirectory(prefix="PhotoNest-engine-image-") as folder:
            for mode, color, expected in (("RGB", (10, 20, 30), (10, 20, 30)),
                                          ("RGBA", (0, 0, 255, 128), (127, 127, 255))):
                with self.subTest(mode=mode):
                    path = Path(folder) / (mode + ".png")
                    with Image.new(mode, (8, 8), color) as source:
                        source.save(path)
                    result = read_image(path, crop=(0.25, 0.25, 0.75, 0.75))
                    try:
                        path.unlink()
                        result.load()
                        self.assertEqual(result.mode, "RGB")
                        self.assertEqual(result.size, (4, 4))
                        self.assertEqual(result.getpixel((2, 2)), expected)
                    finally:
                        result.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
