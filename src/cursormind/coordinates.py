from __future__ import annotations

import base64
from io import BytesIO

from PIL import Image, ImageChops, ImageStat


def verify_visual_point(previous: str | None, current: str, width: float, x: float, y: float):
    """Reject a screenshot-based click if the target patch changed while the model was thinking."""
    if previous is None:
        raise ValueError("Chưa có screenshot cho bước này; cần quan sát lại.")
    old = Image.open(BytesIO(base64.b64decode(previous))).convert("RGB")
    new = Image.open(BytesIO(base64.b64decode(current))).convert("RGB")
    if old.size != new.size or width <= 0:
        raise ValueError("Kích thước ảnh đã đổi; cần quan sát lại.")
    scale = old.width / width
    px, py, radius = round(x * scale), round(y * scale), 24
    box = (max(0, px - radius), max(0, py - radius),
           min(old.width, px + radius), min(old.height, py + radius))
    if box[2] <= box[0] or box[3] <= box[1]:
        raise ValueError("Điểm ngoài screenshot hiện tại.")
    difference = ImageChops.difference(old.crop(box), new.crop(box))
    mean = sum(ImageStat.Stat(difference).mean) / 3
    pixels = list(difference.getdata())
    changed = sum(max(pixel) > 30 for pixel in pixels) / len(pixels)
    if mean > 10 or changed > .20:
        raise ValueError("Vùng ảnh ở điểm đích đã thay đổi; cần quan sát lại trước khi click.")
