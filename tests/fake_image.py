#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""A stand-in image model: `fake_image.py PROMPT OUTPUT` writes a small real JPEG after
FAKE_IMAGE_DELAY seconds, so the UI can be driven without spending image calls."""
import os
import sys
import time
import zlib

time.sleep(float(os.environ.get("FAKE_IMAGE_DELAY", "2")))
# Like `openclaw infer image generate`: the extension becomes the format it wrote, and the
# real path is reported in JSON on stdout.
out = os.path.splitext(sys.argv[2])[0] + ".jpg"
try:
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (768, 512), (14, 22, 40))
    d = ImageDraw.Draw(im)
    for i in range(0, 768, 6):
        d.line([(i, 512), (i + 200, 300 - (zlib.crc32(sys.argv[1].encode()) + i) % 180)], fill=(40, 60 + i % 80, 90), width=2)
    d.ellipse([560, 60, 620, 120], fill=(240, 230, 190))
    im.save(out, "JPEG", quality=80)
except ImportError:
    # A 1x1 JPEG when Pillow is not installed.
    open(out, "wb").write(bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b080001000101011100ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbfcffd9"))

import json
print(json.dumps({"ok": True, "outputs": [{"path": os.path.abspath(out), "mimeType": "image/jpeg"}]}))
