# -*- coding: utf-8 -*-
"""查清 Pillow 在这个打包环境里"实际能读/能写"哪些格式。

不查文档、不猜：直接问 Pillow 自己注册了哪些扩展名，再逐个试写。
"各种格式都能打开转换"能做到什么程度，取决于这份清单。
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "_fmt.txt")
sys.path.insert(0, os.path.join(HERE, "optimized"))

from PIL import Image, features                          # noqa: E402

L = []
L.append("Pillow %s（Python %s）" % (Image.__version__, sys.version.split()[0]))
L.append("")

L.append("== 可选编解码器（features）==")
for name in ("jpg", "zlib", "libtiff", "webp", "webp_anim", "avif", "jpeg2000",
             "raqm", "freetype2", "libjpeg_turbo", "xcb"):
    try:
        L.append("  %-14s %s" % (name, features.check(name)))
    except Exception as exc:                               # noqa: BLE001
        L.append("  %-14s 查不到（%s）" % (name, exc))
L.append("")

exts = Image.registered_extensions()
L.append("== 注册的扩展名共 %d 个 ==" % len(exts))
core = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif",
        ".ico", ".dib", ".tga", ".pcx", ".ppm", ".pgm", ".pbm", ".psd",
        ".avif", ".jxl", ".heic", ".heif", ".dds", ".jp2", ".j2k", ".eps",
        ".pdf", ".mpo", ".sgi", ".im", ".spider", ".xbm", ".fli", ".flc")
for ext in core:
    fmt = exts.get(ext)
    L.append("  %-8s -> %s" % (ext, fmt or "（没有）"))
L.append("")

L.append("== 真写一遍：能不能存 ==")
img = Image.new("RGBA", (32, 24), (10, 120, 200, 255))
for ext in core:
    fmt = exts.get(ext)
    if not fmt:
        continue
    buf = io.BytesIO()
    try:
        img.convert("RGB").save(buf, format=fmt)
        size = len(buf.getvalue())
        # 再读回来验一遍
        buf.seek(0)
        back = Image.open(buf)
        back.load()
        L.append("  %-8s %-6s 写 %6d 字节  读回 %s ✓" % (ext, fmt, size, back.size))
    except Exception as exc:                               # noqa: BLE001
        L.append("  %-8s %-6s ✗ %s: %s" % (ext, fmt, type(exc).__name__,
                                           str(exc)[:60]))

L.append("")
L.append("== 我们当前声明的可保存格式 ==")
import image_ops as ops                                    # noqa: E402
L.append("  FORMULATS: %s" % (ops.FORMATS,))
L.append("  打开对话框的扩展名通配: %s" % ops.all_ext_glob())
L.append("  能打开（Pillow 注册数）: %d 种" % len(exts))

with io.open(OUT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(L) + "\n")
print("\n".join(L))
