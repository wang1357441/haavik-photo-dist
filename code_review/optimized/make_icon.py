# -*- coding: utf-8 -*-
"""生成应用图标 app.ico（纯标准库，32x32，深色地球 + 弯月）。

相对原版的改动：
* ``abs(x - sx) <= 0`` 这种写法等价于 ``x == sx``，直接写清楚；
* 用 math.hypot 代替 d2 ** 0.5，意图更明确；
* 把散落的魔数提成具名常量，改尺寸/配色不用再逐个找数字；
* 增加 --size 参数与一个简单的自校验（文件头是否合法）。
"""
from __future__ import annotations

import argparse
import math
import struct
import sys

# ---- 画布 ----
DEFAULT_SIZE = 32

# ---- 地球 ----
EARTH_CX_RATIO, EARTH_CY_RATIO, EARTH_R_RATIO = 0.50, 0.5625, 0.34375
LAND_BASE = (30, 80, 120)
LAND_TINT = (60, 100, 110)
LAND_COLOR_A = (60, 130, 70)
LAND_COLOR_B = (70, 130, 80)

# ---- 月亮 ----
MOON_CX_RATIO, MOON_CY_RATIO, MOON_R_RATIO = 0.75, 0.21875, 0.15625
MOON_CUT_CX_RATIO, MOON_CUT_CY_RATIO, MOON_CUT_R_RATIO = 0.84375, 0.15625, 0.15625
MOON_COLOR = (220, 220, 230)

BACKGROUND = (16, 18, 30, 255)
STARS = ((5, 5), (10, 3), (28, 18))


def make_ico(path: str, size: int = DEFAULT_SIZE) -> int:
    if size <= 0 or size > 256:
        raise ValueError("size 必须在 1..256 之间")

    s = size
    earth = (int(s * EARTH_CX_RATIO), int(s * EARTH_CY_RATIO), s * EARTH_R_RATIO)
    moon = (int(s * MOON_CX_RATIO), int(s * MOON_CY_RATIO), s * MOON_R_RATIO)
    moon_cut = (int(s * MOON_CUT_CX_RATIO), int(s * MOON_CUT_CY_RATIO), s * MOON_CUT_R_RATIO)

    pixels = bytearray()
    for y in range(s):
        for x in range(s):
            r, g, b, a = BACKGROUND

            # 地球本体：从圆心向外线性变亮
            d_earth = math.hypot(x - earth[0], y - earth[1])
            if d_earth <= earth[2]:
                shade = (earth[2] - d_earth) / earth[2]
                r = int(LAND_BASE[0] + LAND_TINT[0] * shade)
                g = int(LAND_BASE[1] + LAND_TINT[1] * shade)
                b = int(LAND_BASE[2] + LAND_TINT[2] * shade)
                # 两点"陆地"高光，避免看起来是个纯球
                if 5 < (x - earth[0]) + (y - earth[1]) < 10 and d_earth > s * 0.16:
                    r, g, b = LAND_COLOR_A
                if -8 < (x - earth[0]) - (y - earth[1]) < -3 and d_earth > s * 0.24:
                    r, g, b = LAND_COLOR_B

            # 弯月：月亮圆盘减去一个偏移圆盘，形成月牙
            if math.hypot(x - moon[0], y - moon[1]) <= moon[2]:
                if math.hypot(x - moon_cut[0], y - moon_cut[1]) > moon_cut[2]:
                    r, g, b, a = (*MOON_COLOR, 255)

            # 星点
            if (x, y) in STARS:
                r, g, b, a = 255, 255, 255, 255

            pixels.extend((b, g, r, a))  # ICO 的 DIB 是 BGRA

    and_mask = bytearray((s // 8) * s)  # 每个像素 1 bit；32 位图这个掩码实际被忽略
    bmp_header = struct.pack(
        "<IiiHHIIiiII",
        40,          # biSize
        s,           # biWidth
        s * 2,       # biHeight = XOR 图 + AND 掩码
        1,           # biPlanes
        32,          # biBitCount
        0,           # biCompression = BI_RGB
        len(pixels) + len(and_mask),  # biSizeImage
        0, 0, 0, 0,
    )
    image_data = bmp_header + bytes(pixels) + bytes(and_mask)
    icondir = struct.pack("<HHH", 0, 1, 1)  # reserved, type=icon, count=1
    entry = struct.pack(
        "<BBBBHHII",
        s if s < 256 else 0,
        s if s < 256 else 0,
        0, 0, 1, 32,
        len(image_data),
        6 + 16,  # 偏移 = ICONDIR(6) + ICONDIRENTRY(16)
    )

    with open(path, "wb") as fh:
        fh.write(icondir + entry + image_data)
    return 6 + 16 + len(image_data)


def self_check(path: str) -> None:
    with open(path, "rb") as fh:
        head = fh.read(6)
    if struct.unpack("<HHH", head) != (0, 1, 1):
        raise AssertionError("生成的 ICO 文件头不合法")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 app.ico")
    parser.add_argument("output", nargs="?", default="app.ico")
    parser.add_argument("--size", type=int, default=DEFAULT_SIZE)
    args = parser.parse_args()

    written = make_ico(args.output, args.size)
    self_check(args.output)
    print(f"ICO written: {args.output} ({written} bytes, {args.size}x{args.size})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
