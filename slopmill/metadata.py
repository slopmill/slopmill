# SPDX-License-Identifier: AGPL-3.0-or-later
"""Take the hidden data out of a picture the author uploads before it is kept.

A phone photo carries EXIF: where it was taken (GPS), when, and on what; other files carry
XMP, IPTC (Photoshop's APP13), comments or PNG text. An issue's pictures are published, so
that would publish it. Rather than hunt for each kind, every upload is decoded in full and
saved again from its pixels alone: turned the way EXIF says first (phones store "rotate
this" there rather than rotating the pixels), with only the colour profile carried over so
colours do not shift. Decoding in full also refuses a truncated or disguised file.

Pillow comes with matplotlib (the charts). Without it, strip() says so rather than keeping
a picture it could not clean.
"""
import os

MAX_PIXELS = 40_000_000          # 40 megapixels: bigger than any phone photo, far from a bomb
DEMO_MAX_PIXELS = 12_000_000     # the public demo runs in 768MB


class MetadataError(Exception):
    pass


def strip(path, max_pixels=MAX_PIXELS):
    """Rewrite the JPEG, PNG or WebP at path from its pixels alone. Raises MetadataError if
    it cannot be read, is animated, or is too big to decode safely."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise MetadataError("cleaning pictures needs Pillow: run  uv sync  (or pip install pillow)")
    try:
        with Image.open(path) as im:
            fmt = im.format
            if fmt not in ("JPEG", "PNG", "WEBP"):
                raise MetadataError("that file is not a JPEG, PNG or WebP picture")
            if getattr(im, "is_animated", False):
                raise MetadataError("animated pictures are not taken: upload a still picture")
            w, h = im.size
            if w * h > max_pixels:
                raise MetadataError(f"that picture is {w}×{h}: at most {max_pixels // 1_000_000} "
                                    f"megapixels, so make it smaller first")
            icc = im.info.get("icc_profile")
            im.load()                          # every byte decoded: a broken file stops here
            upright = ImageOps.exif_transpose(im)
            clean = upright.copy()
            clean.info = {}                    # nothing carried over but the pixels
            if upright.mode == "P" and "transparency" in im.info:
                clean.info["transparency"] = im.info["transparency"]
    except MetadataError:
        raise
    except Exception as e:           # Pillow's own errors: a broken, truncated or disguised file
        raise MetadataError(f"that picture could not be read: {type(e).__name__}")
    tmp = path + ".clean"
    kw = {"icc_profile": icc} if icc else {}
    try:
        if fmt == "JPEG":
            if clean.mode not in ("RGB", "L", "CMYK"):
                clean = clean.convert("RGB")
            clean.save(tmp, "JPEG", quality=92, optimize=True, **kw)
        elif fmt == "PNG":
            clean.save(tmp, "PNG", optimize=True, **kw)        # lossless
        else:
            clean.save(tmp, "WEBP", quality=90, **kw)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return True
