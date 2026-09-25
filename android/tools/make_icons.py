#!/usr/bin/env python3
"""生成启动图标 PNG（纯标准库，不需要 Pillow）。

为什么要自己画
--------------

图标原本可以用矢量图（`res/drawable/*.xml`），但 Android 8 以下的部分启动器
对矢量启动图支持不佳，可能出现「图标空白」。既然只需要一张简单的图形，
用它换掉一整个图像库依赖更划算：这里用 zlib + struct 手写 PNG，
形状用 SDF（有向距离场）判断，再按 2×2 超采样抹平锯齿。

用法：python3 tools/make_icons.py  （在 android/ 目录下执行）
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 与网页端主色一致
ACCENT = (0x4F, 0x6B, 0xED)
WHITE = (0xFF, 0xFF, 0xFF)
TRANSPARENT = (0, 0, 0, 0)

#: 各密度对应的边长（px）
DENSITIES = {
    "mdpi": 48,
    "hdpi": 72,
    "xhdpi": 96,
    "xxhdpi": 144,
    "xxxhdpi": 192,
}


def write_png(path: Path, size: int, pixels: list) -> None:
    """把 RGBA 像素写成 PNG。"""
    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter type 0
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def rounded_rect(px: float, py: float, cx: float, cy: float,
                 hw: float, hh: float, r: float) -> float:
    """圆角矩形的 SDF：<0 在内部，=0 在边界。"""
    dx = abs(px - cx) - (hw - r)
    dy = abs(py - cy) - (hh - r)
    ax, ay = max(dx, 0.0), max(dy, 0.0)
    return math.hypot(ax, ay) + min(max(dx, dy), 0.0) - r


def in_triangle(px: float, py: float, a, b, c) -> bool:
    """重心坐标法判断点是否在三角形内。"""

    def cross(p, q, r) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    d1, d2, d3 = cross(a, b, (px, py)), cross(b, c, (px, py)), cross(c, a, (px, py))
    neg = d1 < 0 or d2 < 0 or d3 < 0
    pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (neg and pos)


#: 气泡尾巴的三个顶点（归一化坐标）
TAIL = ((0.34, 0.58), (0.29, 0.79), (0.52, 0.62))
#: 气泡里的三个圆点
DOTS = ((0.38, 0.45), (0.50, 0.45), (0.62, 0.45))


def sample(x: float, y: float) -> tuple:
    """返回归一化坐标 (x, y) 处的颜色（含透明度）。"""
    # 背板：带圆角的方块
    if rounded_rect(x, y, 0.5, 0.5, 0.5, 0.5, 0.22) > 0:
        return TRANSPARENT
    # 对话气泡 + 尾巴
    if rounded_rect(x, y, 0.5, 0.45, 0.30, 0.19, 0.10) <= 0 or in_triangle(x, y, *TAIL):
        for dx, dy in DOTS:
            if math.hypot(x - dx, y - dy) <= 0.045:
                return ACCENT + (255,)
        return WHITE + (255,)
    return ACCENT + (255,)


def render(size: int) -> list:
    """按 2×2 超采样渲染一张图。"""
    scale = 2
    rows = []
    for py in range(size):
        row = []
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(scale):
                for sx in range(scale):
                    x = (px + (sx + 0.5) / scale) / size
                    y = (py + (sy + 0.5) / scale) / size
                    r, g, b, a = sample(x, y)
                    # 先按透明度预乘再平均，避免边缘出现暗边
                    acc[0] += r * a
                    acc[1] += g * a
                    acc[2] += b * a
                    acc[3] += a
            n = scale * scale
            alpha = acc[3] / n
            if alpha <= 0:
                row.append(TRANSPARENT)
            else:
                # 反预乘：acc 里存的是 r*a 之和，除以 a 之和才是原色
                row.append((
                    round(min(255, acc[0] / acc[3])),
                    round(min(255, acc[1] / acc[3])),
                    round(min(255, acc[2] / acc[3])),
                    round(alpha),
                ))
        rows.append(row)
    return rows


def main() -> int:
    for name, size in DENSITIES.items():
        target = ROOT / "res" / f"mipmap-{name}"
        target.mkdir(parents=True, exist_ok=True)
        path = target / "ic_launcher.png"
        write_png(path, size, render(size))
        print(f"已生成 {path.relative_to(ROOT)}（{size}×{size}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())