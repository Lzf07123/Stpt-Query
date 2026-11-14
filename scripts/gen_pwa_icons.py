#!/usr/bin/env python3
"""生成 PWA 位图图标（纯标准库，无第三方依赖）。

按 frontend/static/image/icon.svg 的既有资产（蓝底学士帽）逐形状栅格化：
4x 超采样抗锯齿 + 盒式降采样，输出：
- icon-192.png / icon-512.png            （any：圆角 + 透明四角）
- icon-maskable-192.png / icon-maskable-512.png（maskable：全出血，图形收 80% 安全区）
- apple-touch-icon.png                   （180，全出血不透明，iOS 自行圆角）

复现：python scripts/gen_pwa_icons.py
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "frontend" / "static" / "image"
SUPERSCALE = 4

# icon.svg 的形状事实（viewBox 0 0 100 100）
BG = (0x25, 0x63, 0xEB)
CAPBOARD = (0xFF, 0xFF, 0xFF)
CAPBASE = (0xE2, 0xE8, 0xF0)
TASSEL = (0xFB, 0xBF, 0x24)
DIAMOND = ((18, 48), (50, 33), (82, 48), (50, 63))
# 流苏折线（原 SVG 为贝塞尔描边，栅格化取折线近似，圆头半径 2）
TASSEL_PATH = ((82, 48), (82, 66), (76.5, 71), (70, 66), (70, 55))
TASSEL_RADIUS = 2.0
TASSEL_KNOB = ((70, 55), 3.0)


def _rounded_rect_inside(x: float, y: float, x0: float, y0: float,
                         x1: float, y1: float, r: float) -> bool:
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    dx = max(x0 + r - x, x - (x1 - r), 0.0)
    dy = max(y0 + r - y, y - (y1 - r), 0.0)
    return dx * dx + dy * dy <= r * r


def _polygon_inside(x: float, y: float, points) -> bool:
    """射线法（viewBox 内形状均为简单多边形）。"""
    inside = False
    n = len(points)
    for i in range(n):
        ax, ay = points[i]
        bx, by = points[(i + 1) % n]
        if (ay > y) != (by > y):
            cross = (bx - ax) * (y - ay) / (by - ay) + ax
            if x < cross:
                inside = not inside
    return inside


def _capbase_inside(x: float, y: float) -> bool:
    """帽身：矩形段 + 底部半椭圆（贝塞尔曲线的解析近似）。"""
    if 34 <= x <= 66 and 57 <= y <= 70:
        return True
    if y > 70:
        return ((x - 50) / 16.0) ** 2 + ((y - 70) / 10.0) ** 2 <= 1.0
    return False


def _segment_distance(x: float, y: float, ax: float, ay: float,
                      bx: float, by: float) -> float:
    vx, vy = bx - ax, by - ay
    length_sq = vx * vx + vy * vy
    if length_sq == 0:
        return math.hypot(x - ax, y - ay)
    t = max(0.0, min(1.0, ((x - ax) * vx + (y - ay) * vy) / length_sq))
    return math.hypot(x - (ax + t * vx), y - (ay + t * vy))


def _tassel_inside(x: float, y: float) -> bool:
    pts = TASSEL_PATH
    for i in range(len(pts) - 1):
        if _segment_distance(x, y, *pts[i], *pts[i + 1]) <= TASSEL_RADIUS:
            return True
    kx, ky = TASSEL_KNOB[0]
    return math.hypot(x - kx, y - ky) <= TASSEL_KNOB[1]


def _glyph_color(x: float, y: float) -> tuple[int, int, int] | None:
    """按 icon.svg 的图层顺序返回 (x, y) 处的前景色（无则 None）。"""
    if _polygon_inside(x, y, DIAMOND):
        return CAPBOARD
    if _capbase_inside(x, y):
        return CAPBASE
    if _tassel_inside(x, y):
        return TASSEL
    return None


def render(size: int, *, full_bleed: bool, content_scale: float = 1.0) -> bytes:
    """渲染 RGBA 像素。full_bleed=True 时背景为全出血方块（maskable/apple-touch）。"""
    big = size * SUPERSCALE
    scale = big / 100.0 * content_scale
    offset = (big - 100.0 * scale) / 2.0 if content_scale != 1.0 else 0.0
    radius = 20.0 * scale
    pixels = bytearray()
    for py in range(big):
        for px in range(big):
            x = (px + 0.5 - offset) / scale
            y = (py + 0.5 - offset) / scale
            inside_bg = True
            if not full_bleed:
                inside_bg = _rounded_rect_inside(x, y, 0, 0, 100, 100, 20)
            if not inside_bg:
                pixels += b"\x00\x00\x00\x00"
                continue
            glyph = _glyph_color(x, y)
            color = glyph or BG
            pixels += bytes((color[0], color[1], color[2], 0xFF))
    return _downsample(pixels, big, size)


def _downsample(raw: bytearray, big: int, size: int) -> bytes:
    row_bytes = big * 4
    out = bytearray()
    for ry in range(size):
        for rx in range(size):
            total = [0, 0, 0, 0]
            for sy in range(SUPERSCALE):
                base = (ry * SUPERSCALE + sy) * row_bytes + rx * SUPERSCALE * 4
                for sx in range(SUPERSCALE):
                    i = base + sx * 4
                    total[0] += raw[i]
                    total[1] += raw[i + 1]
                    total[2] += raw[i + 2]
                    total[3] += raw[i + 3]
            n = SUPERSCALE * SUPERSCALE
            out += bytes((round(total[0] / n), round(total[1] / n),
                          round(total[2] / n), round(total[3] / n)))
    return _encode_png(size, size, bytes(out))


def _encode_png(width: int, height: int, rgba: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    scanlines = b"".join(
        b"\x00" + rgba[row * width * 4:(row + 1) * width * 4]
        for row in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(scanlines, 9))
            + chunk(b"IEND", b""))


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    targets = [
        ("icon-192.png", 192, dict(full_bleed=False)),
        ("icon-512.png", 512, dict(full_bleed=False)),
        ("icon-maskable-192.png", 192, dict(full_bleed=True, content_scale=0.8)),
        ("icon-maskable-512.png", 512, dict(full_bleed=True, content_scale=0.8)),
        ("apple-touch-icon.png", 180, dict(full_bleed=True)),
    ]
    for name, size, kwargs in targets:
        data = render(size, **kwargs)
        (OUTPUT_DIR / name).write_bytes(data)
        print("wrote %s (%d bytes)" % (name, len(data)))


if __name__ == "__main__":
    main()
