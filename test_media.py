"""Decode only synthetic local fixtures; no user's video library is touched."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image

import media
import thumbs


class MediaTests(unittest.TestCase):
    def test_video_probe_and_on_demand_thumbnail(self):
        with tempfile.TemporaryDirectory(prefix="PhotoNest-video-") as folder:
            root = Path(folder)
            path = root / "视频, 测试.avi"
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (320, 240))
            self.assertTrue(writer.isOpened())
            try:
                for index in range(10):
                    frame = np.zeros((240, 320, 3), dtype=np.uint8)
                    frame[:, :, 1] = 160 + index
                    writer.write(frame)
            finally:
                writer.release()
            info = media.video_probe(path)
            self.assertEqual((info["width"], info["height"]), (320, 240))
            self.assertAlmostEqual(info["duration"], 1, places=1)
            image = media.video_thumbnail(path, 100)
            self.assertIsNotNone(image)
            try:
                self.assertEqual(image.mode, "RGB")
                self.assertEqual(image.size, (100, 75))
                self.assertEqual(image.info["source_size"], (320, 240))
                self.assertGreater(image.getpixel((50, 35))[1], 100)
            finally:
                image.close()
            cache = str(root / "cache")
            self.assertFalse(Path(thumbs.thumb_path(cache, 1)).exists())
            target = thumbs.ensure(str(path), 1, cache)
            self.assertIsNotNone(target)
            self.assertFalse(Path(thumbs.preview_path(cache, 1)).exists())
            with patch("media.video_thumbnail", side_effect=AssertionError("Cached poster must be reused")):
                self.assertEqual(thumbs.ensure(str(path), 1, cache), target)

    def test_corrupt_video_returns_no_cover_and_gif_keeps_first_frame(self):
        with tempfile.TemporaryDirectory(prefix="PhotoNest-video-bad-") as folder:
            root = Path(folder)
            broken = root / "broken.mp4"
            broken.write_bytes(b"invalid-video-fixture")
            self.assertEqual(media.video_probe(broken), {"width": 0, "height": 0, "duration": 0.0})
            self.assertIsNone(media.video_thumbnail(broken))
            self.assertIsNone(thumbs.ensure(str(broken), 1, str(root/'cache')))
            gif = root/'animation.gif'
            frames = [Image.new("RGB", (30, 20), color) for color in ("red", "blue")]
            try:
                frames[0].save(gif, save_all=True, append_images=frames[1:], duration=50, loop=0)
            finally:
                for frame in frames:
                    frame.close()
            target = thumbs.ensure(str(gif), 2, str(root/'cache'))
            with Image.open(target) as image:
                red, _, blue = image.convert('RGB').getpixel((15, 10))
                self.assertGreater(red, blue)

    def test_huge_frame_is_rejected_before_decode_and_capture_is_released(self):
        class HugeCapture:
            released = False
            def open(self, *args):
                return True
            def isOpened(self):
                return True
            def get(self, field):
                return 100_000 if field in (cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT) else 1
            def read(self):
                raise AssertionError("Do not allocate/decode an oversized video frame")
            def release(self):
                self.released = True
        capture = HugeCapture()
        with patch('media.os.path.isfile', return_value=True), patch('cv2.VideoCapture', return_value=capture):
            self.assertIsNone(media.video_thumbnail('huge.avi'))
        self.assertTrue(capture.released)


if __name__ == '__main__':
    unittest.main(verbosity=2)
