"""PhotoNest - thumbnail generation and cache layout."""
from __future__ import annotations

import os
import threading

from PIL import Image, ImageOps
import media

THUMB_SIZE = 480
PREVIEW_SIZE = 1600

THUMB_QUALITY = 82
PREVIEW_QUALITY = 86


def paths(cache_dir: str, asset_id: int) -> tuple[str, str]:
    shard = f"{asset_id % 256:02x}"
    thumb = os.path.join(cache_dir, "thumb", shard, f"{asset_id}.jpg")
    preview = os.path.join(cache_dir, "preview", shard, f"{asset_id}.jpg")
    return thumb, preview


def thumb_path(cache_dir: str, asset_id: int) -> str:
    return paths(cache_dir, asset_id)[0]


def preview_path(cache_dir: str, asset_id: int) -> str:
    return paths(cache_dir, asset_id)[1]


def _flatten(im: Image.Image) -> Image.Image:
    """Convert any mode to RGB, compositing transparency onto white."""
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return im.convert("RGB")


def generate(src: str, asset_id: int, cache_dir: str, kind=None) -> tuple[int, int] | None:
    """Render only the requested cache size. JPEG uses a reduced decoder."""
    thumb_file, preview_file = paths(cache_dir, asset_id)
    os.makedirs(os.path.dirname(thumb_file), exist_ok=True)
    os.makedirs(os.path.dirname(preview_file), exist_ok=True)
    temporary = None
    try:
        before = os.stat(src)
        target_size = THUMB_SIZE if kind == "thumb" else PREVIEW_SIZE
        if media.is_video(src):
            im = media.video_thumbnail(src, target_size)
            if im is None:
                return None
            width, height = im.info.get("source_size", im.size)
        else:
            with Image.open(src) as original:
                width, height = original.size
                if original.getexif().get(274) in (5, 6, 7, 8):
                    width, height = height, width
                original.draft("RGB", (target_size, target_size))
                try:
                    original.seek(0)  # animated GIF / WEBP: first frame
                except Exception:
                    pass
                im = _flatten(ImageOps.exif_transpose(original))
        try:
            for label, target, size, quality in (("thumb", thumb_file, THUMB_SIZE, THUMB_QUALITY),
                    ("preview", preview_file, PREVIEW_SIZE, PREVIEW_QUALITY)):
                if kind is not None and kind != label:
                    continue
                output = im.copy()
                output.thumbnail((size, size), Image.Resampling.LANCZOS)
                temporary = target + f".{threading.get_ident()}.tmp"
                output.save(temporary, "JPEG", quality=quality)
                output.close()
                after = os.stat(src)
                if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                    return None
                os.replace(temporary, target)

            return width, height
        finally:
            im.close()
    except Exception:
        # Corrupt file, unsupported codec (HEIC without plugin), etc.
        return None
    finally:
        if temporary:
            try:
                os.remove(temporary)
            except OSError:
                pass


def remove(cache_dir: str, asset_id: int) -> None:
    for path in paths(cache_dir, asset_id):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def ensure(src: str, asset_id: int, cache_dir: str, kind: str = "thumb") -> str | None:
    """Return a usable cache path, generating it on demand when missing."""
    thumb_file, preview_file = paths(cache_dir, asset_id)
    target = thumb_file if kind == "thumb" else preview_file
    if os.path.exists(target):
        return target
    if generate(src, asset_id, cache_dir, kind) is None:
        return None
    return target if os.path.exists(target) else None
