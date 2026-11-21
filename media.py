"""Small, on-demand video metadata and poster extraction using bundled OpenCV."""
from __future__ import annotations

from contextlib import contextmanager
import math
import os

from PIL import Image


VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".wmv"})
MAX_VIDEO_PIXELS = 40_000_000
VIDEO_TIMEOUT_MS = 1500


def is_video(path):
    return os.path.splitext(os.fspath(path))[1].lower() in VIDEO_EXTS


@contextmanager
def _capture(path):
    import cv2
    capture = cv2.VideoCapture()
    try:
        # Only open local files. The timeout options belong to the bundled FFmpeg
        # backend; do not silently retry with an unbounded network/camera backend.
        source = os.fspath(path)
        if os.path.isfile(source):
            capture.open(source, cv2.CAP_FFMPEG,
                [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, VIDEO_TIMEOUT_MS,
                 cv2.CAP_PROP_READ_TIMEOUT_MSEC, VIDEO_TIMEOUT_MS])
        yield capture
    finally:
        capture.release()


def _number(value):
    return float(value) if math.isfinite(value) and value > 0 else 0.0


def _info(capture):
    import cv2
    width = round(_number(capture.get(cv2.CAP_PROP_FRAME_WIDTH)))
    height = round(_number(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)))
    fps = _number(capture.get(cv2.CAP_PROP_FPS))
    frames = _number(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    return {"width": width, "height": height, "duration": frames / fps if fps else 0.0}


def video_probe(path):
    """Best-effort dimensions/duration in seconds; never decode the whole video."""
    try:
        with _capture(path) as capture:
            if capture.isOpened():
                return _info(capture)
    except Exception:
        pass
    return {"width": 0, "height": 0, "duration": 0.0}


def video_thumbnail(path, target_size=480):
    """Decode at most one frame. Bad/huge/unsupported videos return None."""
    try:
        import cv2
        if isinstance(target_size, int):
            target_size = (target_size, target_size)
        target_size = tuple(max(1, min(1600, int(value))) for value in target_size)
        if len(target_size) != 2:
            return None
        with _capture(path) as capture:
            if not capture.isOpened():
                return None
            info = _info(capture)
            if not 0 < info["width"] * info["height"] <= MAX_VIDEO_PIXELS:
                return None
            ok, frame = capture.read()
            if not ok or frame is None or not frame.size:
                return None
            height, width = frame.shape[:2]
            if width * height > MAX_VIDEO_PIXELS:
                return None
            scale = min(target_size[0] / width, target_size[1] / height, 1.0)
            if scale < 1:
                frame = cv2.resize(frame, (max(1, round(width * scale)), max(1, round(height * scale))),
                                   interpolation=cv2.INTER_AREA)
            image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            image.info["source_size"] = (width, height)
            return image
    except Exception:
        return None
