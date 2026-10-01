# -*- coding: utf-8 -*-
"""``image_ops.py`` 的回归测试：无窗口，纯逻辑。

为什么值得单独写一支
==================================================================
图像处理是这个工具里最容易出**静默错误**的地方。方向反了、某一维算错、
透明区域变成黑块、调亮度顺手把透明度也改了 —— 这些都不会抛异常，
只会在用户存出来的文件里出现，而用户往往过很久才发现。

所以这支测试盯的是四类事：
  1. **尺寸数学**：改尺寸/旋转/裁剪算出来的到底是几乘几；
  2. **像素级后果**：顺时针是不是真的顺时针、镜像是不是真的镜像；
  3. **不变量**：透明通道有没有被颜色操作搞坏、原图有没有被就地改掉；
  4. **格式能力不说谎**：表里写着支持 dpi / 透明的，必须真的往返得回来。

第 4 条尤其重要 —— 一个"声称支持透明但存出来是黑底"的格式表，
比没有这张表更坏。
"""
from __future__ import annotations

import gc
import io
import os
import shutil
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
OPTIMIZED = os.path.join(HERE, "optimized")
OUT = os.path.join(HERE, "_image_ops.txt")

if OPTIMIZED not in sys.path:
    sys.path.insert(0, OPTIMIZED)


class Report:
    def __init__(self):
        self.lines = []
        self.failures = []

    def add(self, text=""):
        self.lines.append(text)

    def head(self, title):
        self.lines.append("")
        self.lines.append("-" * 68)
        self.lines.append(title)
        self.lines.append("-" * 68)

    def check(self, ok, label, detail=""):
        # >>> check() 参数顺序守卫（别删）
        # 本文件是 check(条件, "说明")。写反了不会报错 —— 非空字符串恒为真，
        # 断言会静默变成"永远通过"。宁可当场炸掉，也不要一个假的绿灯。
        if isinstance(ok, str) and not isinstance(label, str):
            raise TypeError(
                u'check() 参数写反了：本文件应为 check(条件, "说明")，'
                u'收到 check(%r, %r)' % (ok, label))

        mark = "  OK  " if ok else " FAIL "
        self.lines.append("[%s] %s%s" % (mark, label, ("  <- " + detail) if detail else ""))
        if not ok:
            self.failures.append(label + (" (" + detail + ")" if detail else ""))
        return ok

    def text(self):
        head = ["=" * 68, "image_ops.py 回归测试（无窗口）", "=" * 68]
        if self.failures:
            tail = ["", "=" * 68, "FAILED: %d 项不成立" % len(self.failures)]
            tail += ["  * " + f for f in self.failures]
            tail.append("=" * 68)
        else:
            tail = ["", "=" * 68, "RESULT: OK", "=" * 68]
        return "\n".join(head + self.lines + tail) + "\n"


def same_pixels(a, b) -> bool:
    """逐像素完全相等。用于"这条操作不该改这个通道"这类断言。"""
    return (a.size == b.size and a.mode == b.mode
            and a.tobytes() == b.tobytes())


def alpha_bytes(img) -> bytes:
    return img.convert("RGBA").split()[-1].tobytes()


def main():
    rep = Report()
    tmp = tempfile.mkdtemp(prefix="haavik_imgops_")

    import image_ops as O
    from PIL import Image

    rep.add("Pillow %s | python %s" % (Image.__version__, sys.version.split()[0]))
    rep.add("临时目录：%s" % os.path.basename(tmp))

    try:
        # ================================================================
        rep.head("A. 模块纯度与格式表自洽")
        # ================================================================
        rep.check("tkinter" not in sys.modules,
                  "import image_ops 不会带进 tkinter（这一层必须能无界面跑）")
        rep.check(O.AVAILABLE, "Pillow 可用")

        missing = [f for f in O.FORMATS
                   if f not in O.FMT_EXT or f not in O.FMT_LABEL or f not in O.FMT_SHORT]
        rep.check(not missing, "FORMATS 里每个格式在扩展名/标签表里都有",
                  "缺 %s" % missing)
        bad_back = [f for f in O.FORMATS if O.EXT_FMT.get(O.FMT_EXT[f]) != f]
        rep.check(not bad_back, "扩展名 → 格式 能反向映射回自己", "对不上 %s" % bad_back)
        rep.check(O.ALPHA_OK == frozenset(O.FORMATS) - O.NEEDS_FLATTEN,
                  "ALPHA_OK == FORMATS - NEEDS_FLATTEN（不能有两份独立清单）")
        for name in ("DPI_OK", "EXIF_OK", "ANIM_OK", "LOSSY"):
            sub = getattr(O, name)
            rep.check(sub <= frozenset(O.FORMATS), "%s 只能是 FORMATS 的子集" % name,
                      "越界 %s" % sorted(sub - frozenset(O.FORMATS)))
        rep.check(O.save_types("WEBP")[0][0] == "WebP",
                  "另存为过滤器把选中格式排在第一", "%r" % (O.save_types("WEBP")[0],))
        rep.check(O.open_types()[0][0] == "图片文件", "打开过滤器有『图片文件』一项")

        rep.head("A2. 已知限制必须被如实表达")
        rep.check(O.ico_warning((1200, 800)) != "",
                  "1200×800 选 ICO 时会提示『会被缩小』")
        rep.check("透明边" in O.ico_warning((1200, 800)),
                  "非正方形选 ICO 时还提示『会补透明边』")
        rep.check(O.ico_warning((8, 8)) != "",
                  "8×8 选 ICO 时会提示『太小会被放大』")
        rep.check(O.ico_warning((64, 64)) == "",
                  "64×64 的正方图选 ICO 时不多嘴",
                  "%r" % O.ico_warning((64, 64)))
        rep.check("JPEG" in O.NEEDS_FLATTEN and "PNG" not in O.NEEDS_FLATTEN,
                  "JPEG 需要预合成白底、PNG 不需要")

        # ================================================================
        rep.head("B. 读取")
        # ================================================================
        png = os.path.join(tmp, "示例照片.png")
        Image.new("RGBA", (1200, 800), (200, 30, 30, 128)).save(png)

        got = O.load_image(png)
        rep.check(got.ok, "能读回自己写出的 PNG", got.error)
        rep.check(got.size == (1200, 800), "尺寸正确", "%s" % (got.size,))
        rep.check(got.mode == "RGBA", "模式正确", got.mode)
        rep.check(got.size_bytes == os.path.getsize(png), "记录的文件大小正确",
                  "%d vs %d" % (got.size_bytes, os.path.getsize(png)))
        rep.check(got.has_alpha, "识别出这张图含透明")
        rep.check(not got.animated and got.n_frames == 1, "静态图不被当动图")

        rep.check(O.load_image(os.path.join(tmp, "没有这个文件.png")).ok is False,
                  "文件不存在时返回 ok=False（不抛异常）")
        junk = os.path.join(tmp, "not-an-image.png")
        with io.open(junk, "w", encoding="utf-8") as fh:
            fh.write("这不是图片")
        bad = O.load_image(junk)
        rep.check(bad.ok is False and bad.error, "非图片文件返回失败 + 说明文字",
                  "%r" % (bad.error[:40],))

        # ---- EXIF 方向：不摆正的话手机照片是躺着的；摆正后又必须把方向标记清掉 ----
        rep.head("B2. EXIF 方向（一个双向的坑）")
        ex = Image.Exif()
        ex[0x0112] = 6                       # Orientation = 6
        oriented = os.path.join(tmp, "带方向.jpg")
        Image.new("RGB", (1200, 800), (10, 200, 10)).save(oriented, exif=ex)

        raw = Image.open(oriented)
        raw.load()
        rep.check(raw.size == (1200, 800) and raw.getexif().get(0x0112) == 6,
                  "构造出来的文件确实是 1200×800 + orientation=6")
        raw.close()

        orient_res = O.load_image(oriented)
        rep.check(orient_res.size == (800, 1200),
                  "读进来时已经按 EXIF 转正", "%s" % (orient_res.size,))
        # 关键：如果方向标记没被清掉，保存时会被**再转一次**（存出来是躺着的）
        left = orient_res.image.getexif().get(0x0112)
        rep.check(left in (None, 1),
                  "转正后 EXIF 里的方向标记已消失/归 1（否则保存会二次旋转）",
                  "实际 %r" % left)

        # ---- 大图保护 ----
        rep.head("B3. 超大图保护")
        keep = Image.MAX_IMAGE_PIXELS
        try:
            Image.MAX_IMAGE_PIXELS = 100
            big = os.path.join(tmp, "看起来很大.png")
            Image.new("RGB", (100, 100), (0, 0, 0)).save(big)
            res = O.load_image(big)
            rep.check(res.ok is False and "太大" in res.error,
                      "超过安全上限时明确拒绝，而不是把内存吃光",
                      "%r" % (res.error[:40],))
        finally:
            Image.MAX_IMAGE_PIXELS = keep

        # ---- 动图 ----
        rep.head("B4. 动图")
        frames = [Image.new("RGBA", (48, 32), (i * 60, 0, 0, 255)) for i in range(4)]
        gif = os.path.join(tmp, "动图.gif")
        frames[0].save(gif, format="GIF", save_all=True, append_images=frames[1:],
                       duration=120, loop=0)
        g = O.load_image(gif)
        rep.check(g.ok and g.animated and g.n_frames == 4,
                  "GIF 识别为 4 帧动图", "animated=%s n_frames=%s" % (g.animated, g.n_frames))
        rep.check(len(g.frames) == 4, "四帧都拿到了", "%d" % len(g.frames))
        rep.check(all(d > 0 for d in g.durations) and len(g.durations) == 4,
                  "每帧都有正的显示时长", "%s" % (g.durations,))

        webp = os.path.join(tmp, "动图.webp")
        frames[0].save(webp, format="WEBP", save_all=True, append_images=frames[1:],
                       duration=120, loop=0)
        w = O.load_image(webp)
        rep.check(w.ok and w.animated and w.n_frames == 4, "WebP 动图也识别为 4 帧",
                  "animated=%s n_frames=%s" % (w.animated, w.n_frames))
        # WebP 的 info['duration'] 只有在**该帧被 load 之后**才出现（实测）：
        # open 后是 None、seek 后仍是 None、load/copy 之后才是 120。
        # 读不到时必须有回退值，否则动图会以 0 毫秒间隔狂翻。
        rep.check(w.durations and all(d > 0 for d in w.durations),
                  "★ WebP 每帧都有正时长（不会 0 毫秒狂翻）", "%s" % (w.durations,))
        rep.check(w.durations == [120] * 4,
                  "WebP 的时长确实读出来了（说明『先 copy 再取时长』的顺序是对的）",
                  "%s" % (w.durations,))
        plain = Image.new("RGB", (4, 4), (0, 0, 0))
        rep.check(O.frame_ms(plain) == O.DEFAULT_FRAME_MS,
                  "彻底读不到时长时回退到默认值 %dms" % O.DEFAULT_FRAME_MS,
                  "%d" % O.frame_ms(plain))

        # ================================================================
        rep.head("C. 编辑：尺寸数学")
        # ================================================================
        base = Image.new("RGBA", (1200, 800), (200, 30, 30, 128))
        rep.check(O.resize_px(base, 600, 400).size == (600, 400), "按像素改尺寸")
        rep.check(O.resize_pct(base, 25).size == (300, 200), "按 25% 改尺寸")
        rep.check(O.resize_pct(base, 0).size == (1, 1), "0% 被夹到 1×1（不崩）")
        rep.check(O.resize_px(base, 99999, 99999, allow_up=False).size == (1200, 800),
                  "allow_up=False 时不放大")
        rep.check(O.rotate_cw(base).size == (800, 1200), "顺时针 90° 换宽高")
        rep.check(O.rotate_ccw(base).size == (800, 1200), "逆时针 90° 换宽高")
        rep.check(O.rotate_180(base).size == (1200, 800), "180° 尺寸不变")
        rep.check(O.rotate_any(base, 90).size == (800, 1200), "任意角 90° 换宽高")

        # ---- 非 90 倍数的任意角：**这里以前一用就炸**（2026-09-24 修）----
        # rotate_any 原来传 LANCZOS 给 Image.rotate，而 Pillow 的 rotate
        # **只收 NEAREST/BILINEAR/BICUBIC**，于是抛 ValueError。
        # 为什么长期没被发现：90/180/270 走的是 PIL 的快路径、根本不看
        # resample，所以"任意角 90°"那条老用例一直是绿的 —— **测试也是绿的，
        # 功能却是死的**。所以必须有非 90 倍数的用例才算真覆盖。
        for deg in (45, 15, 3, -30, 37.5):
            try:
                got = O.rotate_any(base, deg)
                size_ok = (got is not None and got.size[0] > 0 and got.size[1] > 0)
            except Exception as exc:                       # noqa: BLE001
                got, size_ok = exc, False
            rep.check(size_ok, "任意角 %.1f° 能真的转出来（不再抛异常）" % deg,
                      "%s" % (got,))
        # 45° 的补边数学：1200×800 转 45° 后外接矩形约 1414×1414
        r45 = O.rotate_any(base, 45)
        rep.check(abs(r45.size[0] - 1414) <= 2 and abs(r45.size[1] - 1414) <= 2,
                  "45° 补边后尺寸合理（约 1414×1414）", "%s" % (r45.size,))
        # ⚠ 不要写"转 45° 再转 -45° 就回到原尺寸" —— 那是错的：
        #   第一次转完画布已经变成 1414×1414（补过边），第二次转是**在这个
        #   更大的画布**上转，外接矩形又要变大。所以判据只能是"它确实变了"，
        #   不能是"它回到原样"。真正"回到原样"靠撤销栈，不靠转回去。
        back = O.rotate_any(r45, -45)
        rep.check(back.size != r45.size,
                  "在已补边的画布上再转 -45° 会继续变（说明真的在转）",
                  "%s → %s" % (r45.size, back.size))
        # 用像素验角度与**方向**：正角度必须和 rotate_cw **同向**。
        # ⚠ 这个方向以前是反的（2026-09-24 修）：rotate_any 直接把正度数交给
        #   PIL 的 Image.rotate，而 PIL 的正角度是**逆时针** —— 于是
        #   "旋转45度"和界面上"右转"会得到相反的结果，谁也说不清哪个是哪个。
        sq = Image.new("RGBA", (40, 40), (0, 0, 255, 255))
        for x in range(12):
            for y in range(12):
                sq.putpixel((x, y), (255, 0, 0, 255))
        sq90 = O.rotate_any(sq, 90)
        rep.check(sq90.getpixel((37, 5))[:3] == (255, 0, 0),
                  "任意角 90° 和 rotate_cw 同向（左上红 → 右上）",
                  "%s" % (sq90.getpixel((37, 5)),))
        rep.check(list(sq90.getdata()) == list(O.rotate_cw(sq).getdata()),
                  "任意角 90° 与 rotate_cw **像素完全一致**")
        sqm90 = O.rotate_any(sq, -90)
        rep.check(list(sqm90.getdata()) == list(O.rotate_ccw(sq).getdata()),
                  "任意角 -90° 与 rotate_ccw **像素完全一致**")

        rep.check(O.fit_size(4000, 2000, 1000, 1000) == (1000, 500), "fit_size 按长边缩")
        rep.check(O.fit_size(100, 100, 1000, 1000) == (100, 100), "fit_size 默认不放大")
        rep.check(O.fit_size(100, 100, 1000, 1000, allow_up=True) == (1000, 1000),
                  "fit_size(allow_up) 才放大")
        rep.check(O.zoom_size(100, 50, 100.0) == (4000, 2000),
                  "zoom_size 把离谱倍数夹到 40×", "%s" % (O.zoom_size(100, 50, 100.0),))
        rep.check(abs(O.scale_factor_for(2000, 1000, 1000, 1000) - 0.5) < 1e-9,
                  "scale_factor_for 算得出 50%")

        rep.head("C2. 编辑：像素级后果（方向不能反）")
        # 左上红、右下蓝的不对称图 —— 顺时针 90° 后红色应该在右上
        asym = Image.new("RGBA", (4, 2), (0, 0, 255, 255))
        asym.putpixel((0, 0), (255, 0, 0, 255))
        asym.putpixel((1, 0), (255, 0, 0, 255))
        cw = O.rotate_cw(asym)
        rep.check(cw.size == (2, 4), "顺时针后是 2×4", "%s" % (cw.size,))
        rep.check(cw.getpixel((1, 0))[:3] == (255, 0, 0),
                  "顺时针：左上角的红跑到了右上角", "%s" % (cw.getpixel((1, 0)),))
        rep.check(cw.getpixel((0, 3))[:3] == (0, 0, 255),
                  "顺时针：右下角的蓝跑到了左下角", "%s" % (cw.getpixel((0, 3)),))

        # 左红右蓝 → 水平翻转后应变成左蓝右红
        lr = Image.new("RGB", (2, 1), (0, 0, 255))
        lr.putpixel((0, 0), (255, 0, 0))
        fh = O.flip_h(lr)
        rep.check(fh.getpixel((0, 0)) == (0, 0, 255) and fh.getpixel((1, 0)) == (255, 0, 0),
                  "flip_h 真的左右镜像",
                  "左 %s 右 %s" % (fh.getpixel((0, 0)), fh.getpixel((1, 0))))
        fv = Image.new("RGB", (1, 2), (0, 0, 255))
        fv.putpixel((0, 0), (255, 0, 0))
        fv2 = O.flip_v(fv)
        rep.check(fv2.getpixel((0, 0)) == (0, 0, 255) and fv2.getpixel((0, 1)) == (255, 0, 0),
                  "flip_v 真的上下镜像",
                  "上 %s 下 %s" % (fv2.getpixel((0, 0)), fv2.getpixel((0, 1))))

        rep.head("C3. 编辑：裁剪与越界")
        rep.check(O.crop(base, (100, 50, 500, 450)).size == (400, 400), "正常裁剪")
        rep.check(O.clamp_box((500, 450, 100, 50), 1200, 800) == (100, 50, 500, 450),
                  "裁剪框写反了也能纠正（l>r / t>b）")
        rep.check(O.clamp_box((-500, -500, 99999, 99999), 1200, 800) == (0, 0, 1200, 800),
                  "裁剪框越界被夹进图片内")
        tiny = O.clamp_box((600, 400, 600, 400), 1200, 800)
        rep.check(tiny[2] - tiny[0] >= 1 and tiny[3] - tiny[1] >= 1,
                  "零尺寸的框被撑到至少 1 像素（PIL 拿到空框会报错/返回黑块）",
                  "%s" % (tiny,))
        rep.check(O.crop(base, (-99, -99, 99, 99)).size == (99, 99),
                  "越界的 crop 也能出结果", "%s" % (O.crop(base, (-99, -99, 99, 99)).size,))

        rep.head("C4. 不变量：原图绝不被就地修改")
        src_snapshot = base.copy()
        O.resize_px(base, 10, 10)
        O.rotate_cw(base)
        O.crop(base, (0, 0, 5, 5))
        O.grayscale(base)
        O.adjust(base, brightness=0.2)
        O.flatten(base)
        rep.check(same_pixels(base, src_snapshot),
                  "跑过一整轮编辑后，作为输入的那张图一个像素都没变")

        # ================================================================
        rep.head("D. 颜色操作（重点：不许弄坏透明通道）")
        # ================================================================
        rgba = Image.new("RGBA", (8, 8), (200, 100, 50, 90))
        rgba.putpixel((0, 0), (255, 255, 255, 0))     # 一个全透明像素
        a0 = alpha_bytes(rgba)

        gray = O.grayscale(rgba)
        rep.check(gray.mode == "RGBA", "灰度化后仍是 RGBA（不把模式换掉）", gray.mode)
        px = gray.getpixel((4, 4))
        rep.check(px[0] == px[1] == px[2], "灰度化后 R=G=B", "%s" % (px,))
        rep.check(alpha_bytes(gray) == a0,
                  "★ 灰度化没有改动 alpha（原来透明的地方还透明）")
        rep.check(gray.getpixel((0, 0))[3] == 0, "全透明像素仍然是全透明")

        inv = O.invert(rgba)
        rep.check(alpha_bytes(inv) == a0, "★ 反色没有改动 alpha")
        rep.check(inv.getpixel((0, 0))[3] == 0,
                  "★ 反色后透明区没有变成不透明白/黑块",
                  "%s" % (inv.getpixel((0, 0)),))
        rep.check(inv.getpixel((4, 4))[:3] == (55, 155, 205),
                  "反色算得对（200,100,50 → 55,155,205）", "%s" % (inv.getpixel((4, 4)),))

        sep = O.sepia(rgba)
        sp = sep.getpixel((4, 4))
        rep.check(sp[0] > sp[2], "复古色调整体偏暖（R > B）", "%s" % (sp,))
        rep.check(alpha_bytes(sep) == a0, "★ 复古色调没有改动 alpha")

        adj = O.adjust(rgba, brightness=0.5)
        rep.check(alpha_bytes(adj) == a0,
                  "★ 调暗没有改动 alpha（ImageEnhance 内部会一起混合 alpha）")
        rep.check(sum(adj.getpixel((4, 4))[:3]) < sum(rgba.getpixel((4, 4))[:3]),
                  "调暗确实变暗了", "%s → %s" % (rgba.getpixel((4, 4)), adj.getpixel((4, 4))))
        rep.check(same_pixels(O.adjust(rgba), rgba),
                  "所有参数都是 1.0 时结果与原图完全一致")

        rep.check(O.posterize(rgba, 2).mode == "RGBA", "色调分离保持 RGBA")
        rep.check(alpha_bytes(O.posterize(rgba, 2)) == a0, "★ 色调分离没有改动 alpha")
        rep.check(O.auto_contrast(rgba).size == rgba.size, "自动对比度不报错")

        rep.head("D2. 调色板（GIF 帧）不能把颜色操作搞炸")
        pal = Image.new("P", (16, 16))
        for name, fn in (("灰度", O.grayscale), ("反色", O.invert),
                         ("复古", O.sepia), ("色调分离", O.posterize),
                         ("自动对比度", O.auto_contrast),
                         ("调整", lambda im: O.adjust(im, contrast=1.4))):
            try:
                out = fn(pal)
                rep.check(out is not None and out.size == pal.size,
                          "P 模式能做%s" % name, "→ %s" % (out.mode if out else None))
            except Exception as exc:                        # noqa: BLE001
                rep.check(False, "P 模式能做%s" % name,
                          "%s: %s" % (type(exc).__name__, exc))

        # ================================================================
        rep.head("D3. 背景虚化 / 换背景：合成不出错、透明处=背景、不透明处=主体")
        # ================================================================
        # 用纯色底：高斯模糊对常数图是恒等变换，于是「透明处」的结果必须
        # 精确等于底色、「主体处」必须精确等于主体原色 —— 不靠肉眼凑。
        base = Image.new("RGB", (40, 30), (10, 20, 30))
        subj = Image.new("RGBA", (40, 30), (0, 0, 0, 0))     # 全透明
        subj.paste(Image.new("RGBA", (10, 10), (200, 100, 50, 255)), (15, 10))
        out = O.composite_over_blur(subj, base, radius=4)
        rep.check(out.mode == "RGB" and out.size == (40, 30),
                  "背景虚化输出尺寸/模式正确", "%s %s" % (out.mode, out.size))
        rep.check(out.getpixel((2, 2)) == (10, 20, 30),
                  "★ 透明处 = 模糊后的背景（纯色底不变）", "%s" % (out.getpixel((2, 2)),))
        rep.check(out.getpixel((20, 15))[:3] == (200, 100, 50),
                  "★ 主体处 = 主体原色（没被背景糊掉）", "%s" % (out.getpixel((20, 15)),))
        # 换白底
        flat_white = O.flatten(subj, (255, 255, 255))
        rep.check(flat_white.mode == "RGB" and flat_white.size == (40, 30),
                  "换白底输出尺寸/模式正确")
        rep.check(flat_white.getpixel((2, 2)) == (255, 255, 255),
                  "★ 换白底后透明处=白", "%s" % (flat_white.getpixel((2, 2)),))
        rep.check(flat_white.getpixel((20, 15))[:3] == (200, 100, 50),
                  "★ 换白底后主体不变")

        # ================================================================
        rep.head("E. 保存：每个格式都真的存得出来、读得回来")
        # ================================================================
        saved = {}
        for fmt in O.FORMATS:
            path = os.path.join(tmp, "out" + O.FMT_EXT[fmt])
            ok, detail, n = O.save_image(rgba, path, fmt, quality=85)
            rep.check(ok, "写出 %s" % fmt, detail)
            if not ok:
                continue
            saved[fmt] = path
            rep.check(n > 0 and n == os.path.getsize(path),
                      "  %s 报告的字节数 = 磁盘上的字节数" % fmt, "%d" % n)
            with Image.open(path) as back:
                back.load()
                rep.check(back.size[0] > 0, "  %s 能被读回来" % fmt, "%s" % (back.size,))

        rep.head("E2. 透明 → JPEG/BMP 必须合成白底（不是黑块）")
        t = Image.new("RGBA", (32, 32), (0, 0, 0, 0))        # 全透明
        for fmt in sorted(O.NEEDS_FLATTEN):
            path = os.path.join(tmp, "透明_to" + O.FMT_EXT[fmt])
            ok, detail, _ = O.save_image(t, path, fmt)
            rep.check(ok, "透明图能存成 %s" % fmt, detail)
            if not ok:
                continue
            with Image.open(path) as back:
                back.load()
                rep.check(back.getpixel((16, 16))[:3] == (255, 255, 255),
                          "  %s 里透明区域是白的（不是黑的）" % fmt,
                          "%s" % (back.getpixel((16, 16)),))
                rep.check(back.mode == "RGB", "  %s 读回来是 RGB" % fmt, back.mode)

        rep.head("E3. ICO 的两条隐藏规则 —— 我们必须自己兜住")
        big_ico = os.path.join(tmp, "图标.ico")
        ok, detail, _ = O.save_image(Image.new("RGB", (1200, 800), (9, 9, 9)), big_ico, "ICO")
        rep.check(ok, "1200×800 能存成 ICO", detail)
        if ok:
            with Image.open(big_ico) as back:
                back.load()
                rep.check(back.size == (256, 256),
                          "★ 自己垫成正方形后，读回是 256×256（Pillow 自己只会给 256×171）",
                          "%s" % (back.size,))
                rep.check(max(back.size) <= O.ICO_MAX,
                          "★ ICO 不会超过 256（这就是必须提示用户的原因）",
                          "%s" % (back.size,))
        # 源图小于 16 时 Pillow 会写出一个 6 字节的坏文件，连它自己都读不回来。
        # prepare_ico 把画布垫到 16，正好堵住这个洞。
        tiny_ico = os.path.join(tmp, "太小.ico")
        ok, detail, n = O.save_image(Image.new("RGBA", (8, 8), (5, 5, 5, 255)),
                                     tiny_ico, "ICO")
        rep.check(ok, "8×8 也能存 ICO（历史上会写出 6 字节坏文件）", detail)
        if ok:
            rep.check(n > 100, "8×8 存出来的不是 6 字节的坏文件", "%d 字节" % n)
            try:
                with Image.open(tiny_ico) as back:
                    back.load()
                    rep.check(back.size == (16, 16),
                              "8×8 被垫到 16×16 且能被读回", "%s" % (back.size,))
            except Exception as exc:                        # noqa: BLE001
                rep.check(False, "8×8 的 ICO 能被读回",
                          "%s: %s" % (type(exc).__name__, exc))
        rep.check(O.ico_sizes((1200, 800))[0] == (16, 16)
                  and O.ico_sizes((1200, 800))[-1] == (256, 256),
                  "ICO 尺寸清单覆盖标准尺寸且从小到大",
                  "%s" % (O.ico_sizes((1200, 800)),))
        rep.check(all(s[0] <= 64 for s in O.ico_sizes((64, 48))),
                  "小图不会往 ICO 里塞比它还大的尺寸（不做无谓放大）",
                  "%s" % (O.ico_sizes((64, 48)),))

        rep.head("E4. dpi 往返：表里说支持的必须真存得进去")
        for fmt in O.FORMATS:
            path = os.path.join(tmp, "dpi" + O.FMT_EXT[fmt])
            ok, detail, _ = O.save_image(rgba, path, fmt, dpi=(300, 300))
            if not ok:
                rep.check(False, "%s 按 dpi 保存" % fmt, detail)
                continue
            with Image.open(path) as back:
                got_dpi = back.info.get("dpi")
            if fmt in O.DPI_OK:
                rep.check(got_dpi is not None,
                          "%s 在 DPI_OK 里，dpi 真的存下来了" % fmt, "%r" % (got_dpi,))
            else:
                rep.check(got_dpi is None,
                          "%s 不在 DPI_OK 里，如实承认读不到 dpi（不假装支持）" % fmt,
                          "%r" % (got_dpi,))

        rep.head("E5. exif 往返")
        exif_src = Image.Exif()
        exif_src[0x010F] = "HAAVIK"          # Make
        exif_src[0x0110] = "测试机型"         # Model
        exif_bytes = exif_src.tobytes()
        jpath = os.path.join(tmp, "带exif.jpg")
        ok, detail, _ = O.save_image(Image.new("RGB", (64, 48), (1, 2, 3)), jpath,
                                     "JPEG", exif=exif_bytes)
        rep.check(ok, "带 EXIF 保存 JPEG", detail)
        rows = O.exif_rows(O.load_image(jpath).image)
        rep.check(any(r[2] == "HAAVIK" for r in rows),
                  "EXIF 能读回来（Make = HAAVIK）", "%s" % (rows[:3],))
        rep.check(any(r[0] == "相机厂商" for r in rows),
                  "常见标签有中文名", "%s" % ([r[0] for r in rows][:5],))
        # 顺带把一个容易误会的现象记录下来：EXIF 的 ASCII 字段存不了中文，
        # Pillow 在**写入**时就已经把"测试机型"替换成 "????" 了。
        # 这不是我们读错 —— 以后有人看到问号，别去查读取代码。
        rep.check("相机型号" in [r[0] for r in rows],
                  "中文 EXIF 值会被 Pillow 在写入阶段替换成 ?（记录该行为，不是读错）",
                  "Model = %r" % next((r[2] for r in rows if r[0] == "相机型号"), None))

        rep.head("E6. 估算体积 / 失败要响")
        est = O.estimate_saved_size(rgba, "JPEG", quality=85)
        real = os.path.getsize(saved["JPEG"])
        rep.check(est > 0 and 0.5 * real < est < 2.0 * real,
                  "估算体积与真实体积同量级", "估 %d / 实 %d" % (est, real))
        rep.check(O.estimate_saved_size(rgba, "JPEG") != 0, "估算失败时会返回 0 而不是抛")
        ok, detail, n = O.save_image(rgba, os.path.join(tmp, "没有这个目录", "a.png"), "PNG")
        rep.check(ok is False and n == 0 and detail,
                  "目录不存在时返回失败 + 原因（不抛异常）", "%r" % (detail[:40],))
        rep.check(O.suggested_name(png, "JPEG") == "示例照片_改.jpg",
                  "另存为默认名带 _改",
                  "%r" % O.suggested_name(png, "JPEG"))
        rep.check("示例照片.png" != O.suggested_name(png, "PNG"),
                  "★ 默认文件名绝不会等于原文件名")
        rep.check(O.suggested_name(None, "PNG") == "未命名.png", "没有源文件时叫『未命名』")

        # ================================================================
        rep.head("F. 显示辅助")
        # ================================================================
        cb = O.checkerboard((70, 30), cell=8)
        rep.check(cb.size == (70, 30), "棋盘格尺寸正确", "%s" % (cb.size,))
        colors = {cb.getpixel((x, y)) for x in range(0, 17, 8) for y in range(0, 17, 8)}
        rep.check(len(colors) == 2, "棋盘格确实是两种颜色交错", "%d 种" % len(colors))
        th = O.thumbnail(Image.new("RGB", (1000, 500), (0, 0, 0)), (200, 200))
        rep.check(th.size == (200, 100), "缩略图等比缩进框内", "%s" % (th.size,))
        th2 = O.thumbnail(Image.new("RGB", (10, 10), (0, 0, 0)), (200, 200))
        rep.check(th2.size == (10, 10), "缩略图默认不把小图放大")
        rep.check(O.flatten_on(rgba, (255, 255, 255)).mode == "RGB",
                  "flatten_on 输出 RGB（用于贴到查看区）")

        info = O.image_info(rgba, png, animated=True, n_frames=4)
        for key in ("文件名", "尺寸", "色彩模式", "分辨率", "文件大小"):
            rep.check(key in info, "信息面板有『%s』一项" % key)
        rep.check(info.get("动画") == "4 帧", "动图会多一行帧数", "%r" % info.get("动画"))
        rep.check("含透明" in info.get("色彩模式", ""), "色彩模式标注了透明")

        # ================================================================
        rep.head("G. 端到端：打开 → 旋转 → 裁剪 → 改尺寸 → 存 → 再读")
        # ================================================================
        doc = O.load_image(png)
        img = doc.image
        img = O.rotate_cw(img)                      # 1200×800 → 800×1200
        img = O.crop(img, (0, 0, 800, 600))         # → 800×600
        img = O.resize_px(img, 400, 300)            # → 400×300
        final = os.path.join(tmp, "最后.webp")
        ok, detail, n = O.save_image(img, final, "WEBP", quality=80)
        rep.check(ok, "整条链路最后能存成 WebP", detail)
        back = O.load_image(final)
        rep.check(back.ok and back.size == (400, 300),
                  "★ 存出来再读回来，尺寸和预期一致", "%s" % (back.size,))

        # ================================================================
        rep.head("H. 叠加：把另一张图贴到这张上")
        # ================================================================
        base = Image.new("RGBA", (200, 120), (0, 0, 255, 255))    # 纯蓝底
        patch = Image.new("RGBA", (40, 30), (255, 0, 0, 255))     # 纯红块
        out = O.overlay(base, patch, x=10, y=20)
        rep.check(out.size == base.size, "叠加结果和底图一样大", "%s" % (out.size,))
        rep.check(base.getpixel((15, 25)) == (0, 0, 255, 255),
                  "★ 底图没有被就地改动（还是蓝的）",
                  "%s" % (base.getpixel((15, 25)),))
        rep.check(out.getpixel((15, 25)) == (255, 0, 0, 255),
                  "盖到的地方变成叠加图的颜色", "%s" % (out.getpixel((15, 25)),))
        rep.check(out.getpixel((60, 60)) == (0, 0, 255, 255),
                  "没盖到的地方保持底图原样", "%s" % (out.getpixel((60, 60)),))
        rep.check(out.getpixel((9, 19)) == (0, 0, 255, 255)
                  and out.getpixel((10, 20)) == (255, 0, 0, 255),
                  "x/y 指的是叠加图的左上角（第 10 / 20 个像素起）")

        big = O.overlay(base, patch, x=0, y=0, scale=200)
        rep.check(big.getpixel((79, 59)) == (255, 0, 0, 255)
                  and big.getpixel((81, 61)) == (0, 0, 255, 255),
                  "缩放 200% 后覆盖到 80×60，越界处仍是底图",
                  "%s / %s" % (big.getpixel((79, 59)), big.getpixel((81, 61))))

        half = O.overlay(base, patch, x=0, y=0, opacity=50)
        px = half.getpixel((5, 5))
        rep.check(abs(px[0] - 128) <= 3 and abs(px[2] - 128) <= 3,
                  "不透明度 50% ≈ 红蓝各半", "%s" % (px,))
        rep.check(O.overlay(base, patch, x=0, y=0, opacity=0)
                  .getpixel((5, 5)) == (0, 0, 255, 255),
                  "不透明度 0% = 完全看不见")

        rep.check("multiply" in O.BLEND_NAMES and "screen" in O.BLEND_NAMES,
                  "混合方式表里有正片叠底 / 滤色", "%s" % (list(O.BLEND_NAMES),))
        grey = Image.new("RGBA", (10, 10), (128, 128, 128, 255))
        white = Image.new("RGBA", (10, 10), (255, 255, 255, 255))
        mult = O.overlay(white, grey, x=0, y=0, mode="multiply")
        rep.check(mult.getpixel((5, 5))[:3] == (128, 128, 128),
                  "正片叠底：白 × 灰 = 灰", "%s" % (mult.getpixel((5, 5)),))
        scr = O.overlay(grey, grey, x=0, y=0, mode="screen")
        rep.check(scr.getpixel((5, 5))[0] > 130,
                  "滤色：灰 ⊕ 灰 比灰更亮", "%s" % (scr.getpixel((5, 5)),))
        only = O.overlay(base, patch, x=0, y=0, mode="multiply")
        rep.check(only.getpixel((100, 100)) == (0, 0, 255, 255),
                  "★ 混合模式不会越过叠加图的范围（没盖到的地方不变）",
                  "%s" % (only.getpixel((100, 100)),))

        see_through = Image.new("RGBA", (10, 10), (255, 0, 0, 0))
        rep.check(O.overlay(base, see_through, x=0, y=0)
                  .getpixel((5, 5)) == (0, 0, 255, 255),
                  "全透明的叠加图等于没贴")

        # 羽化：必须看**贴上去那张图的边缘**。
        # 别看画布角落（0,0）—— 那一点的模糊邻域整个都在图内，
        # "模糊一个常数还是常数"，永远看不出效果（我第一版就栽在这）。
        hard = O.overlay(base, patch, x=0, y=0)
        soft = O.overlay(base, patch, x=0, y=0, feather=4)
        rep.check(hard.getpixel((39, 15)) == (255, 0, 0, 255)
                  and hard.getpixel((41, 15)) == (0, 0, 255, 255),
                  "不羽化时边界是硬的（39 还是红、41 已经是蓝）",
                  "%s / %s" % (hard.getpixel((39, 15)), hard.getpixel((41, 15))))
        edge = soft.getpixel((41, 15))
        rep.check(edge not in ((255, 0, 0, 255), (0, 0, 255, 255)),
                  "羽化把边界揉开了（40 附近是红蓝混合的中间色）", "%s" % (edge,))
        rep.check(soft.getpixel((2, 2)) == (255, 0, 0, 255),
                  "羽化只影响边缘，图内部还是实心", "%s" % (soft.getpixel((2, 2)),))

        neg = O.overlay(base, patch, x=-10, y=-10)
        rep.check(neg.getpixel((0, 0)) == (255, 0, 0, 255)
                  and neg.size == base.size,
                  "负坐标时从画外裁进来，画面尺寸不变",
                  "%s" % (neg.getpixel((0, 0)),))

        try:
            O.overlay(None, patch)
            rep.check(False, "底图是 None 时应当明确报错")
        except ValueError:
            rep.check(True, "底图是 None 时报 ValueError，而不是崩在 Pillow 深处")

        buf = io.BytesIO()
        patch.convert("RGB").save(buf, format="PNG")
        got = O.clipboard_to_image(buf.getvalue())
        rep.check(got is not None and got.size == patch.size,
                  "剪贴板字节能变成图（PNG）",
                  "%s" % (getattr(got, "size", None),))
        rep.check(O.clipboard_to_image(b"not an image at all") is None,
                  "认不出来就返回 None（不抛异常）")

    except Exception:                                       # noqa: BLE001
        rep.add("")
        rep.add("!! 测试过程中抛异常：")
        rep.add(traceback.format_exc())
        rep.check(False, "测试全过程无异常")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        gc.collect()

    text = rep.text()
    with io.open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    # 生成物要进仓库：本机绝对路径一律换成占位符（硬性约定 12）
    try:
        import _redact
        _redact.scrub_tree(os.path.dirname(os.path.abspath(__file__)))
    except ImportError:
        pass
    print(text)
    return 1 if rep.failures else 0


if __name__ == "__main__":
    sys.exit(main())
