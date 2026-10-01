# -*- coding: utf-8 -*-
"""``shell_ops.py`` 的回归测试。

这支测试**故意不调用**三个会打扰到你的函数：
  ``set_wallpaper``（会真的换掉你的桌面背景）、
  ``reveal_in_explorer``（会弹一个资源管理器窗口）、
  ``print_file``（会拉起打印程序）。
只测它们的参数校验那一半。删到回收站是会真删的 —— 删的是本测试自己
在临时目录里建的文件，删完还会确认它真的进了回收站（原位置不再存在）。

剪贴板会被本测试占用一次（放进去一张 2×2 的小图）。
"""
from __future__ import annotations

import gc
import io
import os
import shutil
import struct
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
OPTIMIZED = os.path.join(HERE, "optimized")
OUT = os.path.join(HERE, "_shell_ops.txt")

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
        head = ["=" * 68, "shell_ops.py 回归测试", "=" * 68]
        if self.failures:
            tail = ["", "=" * 68, "FAILED: %d 项不成立" % len(self.failures)]
            tail += ["  * " + f for f in self.failures]
            tail.append("=" * 68)
        else:
            tail = ["", "=" * 68, "RESULT: OK", "=" * 68]
        return "\n".join(head + self.lines + tail) + "\n"


def bmp_wrap(dib: bytes) -> bytes:
    """给一段 CF_DIB 补上 BMP 文件头，交给 Pillow 去解析。

    这是本测试最有用的一招：通道顺序（BGR）、行方向（自下而上）、
    行对齐（4 字节 stride）这三件事**全都会被 Pillow 的 BMP 解析器检验**，
    比自己写三条断言可靠得多 —— 我自己写的断言可能和我自己的错误一致。
    """
    offset = 14 + 40
    size = offset + len(dib) - 40
    header = b"BM" + struct.pack("<IHHI", size, 0, 0, offset)
    return header + dib


def main():
    rep = Report()
    tmp = tempfile.mkdtemp(prefix="haavik_shell_")

    import shell_ops as S
    import image_ops as ops
    from PIL import Image

    rep.add("python %s | os.name=%s" % (sys.version.split()[0], os.name))

    try:
        # ================================================================
        rep.head("A. 模块纯度与常量")
        # ================================================================
        rep.check("tkinter" not in sys.modules,
                  "import shell_ops 不会带进 tkinter（这一层不碰界面）")
        rep.check(S.IS_WINDOWS is True, "识别出当前是 Windows")
        rep.check(S.WALLPAPER_NAME.endswith(".bmp")
                  and os.sep not in S.WALLPAPER_NAME,
                  "壁纸文件名是固定的 .bmp 裸文件名（不含路径）",
                  S.WALLPAPER_NAME)
        rep.check(S.wallpaper_bmp_path() ==
                  os.path.join(S.temp_dir(), S.WALLPAPER_NAME),
                  "壁纸落在系统临时目录里（同名覆盖、不累积）")

        rep.head("A2. 已知文件夹（这台机器被 360 迁移过，~\\Desktop 是错的）")
        for name in ("desktop", "pictures"):
            p = S.known_folder(name)
            rep.check(bool(p) and os.path.isdir(p),
                      "known_folder(%r) 返回一个真实存在的目录" % name,
                      "…\\%s" % os.path.basename(p or ""))
        rep.check(S.known_folder("不存在的名字") is None,
                  "名字不认识时返回 None（不瞎猜一个路径出来）")
        rep.check(os.path.isdir(S.pictures_dir()), "pictures_dir() 可用的兜底")

        # ================================================================
        rep.head("B. 剪贴板 DIB 打包（结构最容易错的一步）")
        # ================================================================
        img = Image.new("RGB", (2, 2))
        img.putpixel((0, 0), (255, 0, 0))        # 左上 红
        img.putpixel((1, 0), (0, 255, 0))        # 右上 绿
        img.putpixel((0, 1), (0, 0, 255))        # 左下 蓝
        img.putpixel((1, 1), (255, 255, 255))    # 右下 白
        dib = S.dib_from_image(img)

        hdr = struct.unpack("<IiiHHIIiiII", dib[:40])
        rep.check(hdr[0] == 40, "BITMAPINFOHEADER 是 40 字节", "%d" % hdr[0])
        rep.check(hdr[1] == 2 and hdr[2] == 2, "头里的宽高正确", "%dx%d" % (hdr[1], hdr[2]))
        rep.check(hdr[4] == 24, "位深 24", "%d" % hdr[4])
        rep.check(hdr[5] == 0, "不压缩（BI_RGB）")
        stride = ((2 * 3 + 3) // 4) * 4
        rep.check(hdr[6] == stride * 2, "biSizeImage = stride × 高",
                  "%d vs %d" % (hdr[6], stride * 2))
        rep.check(len(dib) == 40 + stride * 2, "总长度 = 头 + 像素",
                  "%d vs %d" % (len(dib), 40 + stride * 2))

        rows = dib[40:]
        rep.check(rows[0:3] == bytes((255, 0, 0)),
                  "★ 第一行第一个像素是 BGR 的『蓝(255)』—— 即原图左下角的纯蓝",
                  "%s" % (list(rows[0:3]),))
        rep.check(rows[3:6] == bytes((255, 255, 255)),
                  "★ 第一行第二个像素是白色（原图右下角）",
                  "%s" % (list(rows[3:6]),))
        rep.check(rows[6:8] == b"\x00\x00",
                  "每行按 4 字节对齐补的 0 确实存在（不补会斜成一片）")

        rep.head("B2. 用 Pillow 的 BMP 解析器复核一遍（不信自己写的断言）")
        try:
            with Image.open(io.BytesIO(bmp_wrap(dib))) as back:
                back.load()
                rep.check(back.size == (2, 2), "Pillow 读出的尺寸正确", "%s" % (back.size,))
                px = [back.getpixel((x, y)) for y in range(2) for x in range(2)]
                want = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]
                rep.check(px == want,
                          "★ 逐像素与原始图完全一致（顺序/通道/方向全对）",
                          "%s" % (px,))
        except Exception as exc:                            # noqa: BLE001
            rep.check(False, "Pillow 能解析我们打包的 DIB",
                      "%s: %s" % (type(exc).__name__, exc))

        rep.head("B3. 剪贴板真的写进去了（会占用一次剪贴板）")
        okc, detailc = S.copy_image_to_clipboard(img)
        rep.check(okc, "把 2×2 的图放进剪贴板", detailc)
        if okc:
            import ctypes
            api = S._api()
            user32, kernel32 = api["user32"], api["kernel32"]
            got_ok = False
            got_dims = None
            if user32.OpenClipboard(None):
                try:
                    rep.check(bool(user32.IsClipboardFormatAvailable(S.CF_DIB)),
                              "剪贴板里出现 CF_DIB 格式")
                    h = user32.GetClipboardData(S.CF_DIB)
                    if h:
                        ptr = kernel32.GlobalLock(h)
                        if ptr:
                            try:
                                size = kernel32.GlobalSize(h)
                                raw = ctypes.string_at(ptr, size)
                                dh = struct.unpack("<Iii", raw[:12])
                                got_ok = dh[0] == 40
                                got_dims = (dh[1], dh[2])
                            finally:
                                kernel32.GlobalUnlock(h)
                finally:
                    user32.CloseClipboard()
            rep.check(got_ok, "从剪贴板读回的也是 40 字节 DIB 头")
            rep.check(got_dims == (2, 2),
                      "★ 从剪贴板读回的尺寸就是 2×2", "%s" % (got_dims,))

        rep.check(S.copy_text_to_clipboard(r"C:\示例\路径.jpg")[0],
                  "文本也能放进剪贴板（用于『复制文件路径』）")

        # ---- ★ 读回来：我们自己写进去的 DIB，自己得能解析出来 ----
        # 这一段是"叠加图片"用的那条通路：从别处复制一张图，贴到当前图上。
        # 用自己写进去的内容做往返，两端都在我们手里，能真正对上像素。
        rep.head("B4. 从剪贴板读图片（叠加功能用）")
        color = Image.new("RGB", (5, 3), (12, 200, 90))
        color.putpixel((0, 0), (255, 0, 0))
        color.putpixel((4, 2), (0, 0, 255))
        okw, detailw = S.copy_image_to_clipboard(color)
        rep.check(okw, "把一张 5×3 的彩色图放进剪贴板", detailw)
        back = S.read_image_from_clipboard()
        rep.check(back is not None, "从剪贴板把它读回来（不是 None）")
        if back is not None:
            rep.check(back.size == (5, 3),
                      "读回来的尺寸一致", "%s" % (back.size,))
            px = back.convert("RGB")
            rep.check(px.getpixel((0, 0)) == (255, 0, 0)
                      and px.getpixel((4, 2)) == (0, 0, 255)
                      and px.getpixel((2, 1)) == (12, 200, 90),
                      "★ 读回来的像素与写进去的一致（含四个角）",
                      "%s / %s / %s" % (px.getpixel((0, 0)), px.getpixel((2, 1)),
                                        px.getpixel((4, 2))))

        # 认不出来的东西必须返回 None，绝不能抛异常 ——
        # 它会被界面直接调用，抛出去就是一个崩溃。
        rep.check(S.read_image_from_clipboard.__doc__ is not None,
                  "读剪贴板这个函数有文档（说明它读哪两种格式）")
        bogus = S._dib_to_image(b"\x00" * 8, ops)
        rep.check(bogus is None, "太短的 DIB 字节 → None（不抛异常）")
        # 8 位调色板 DIB：我们明确**不支持**，要如实放弃而不是猜一张花屏图
        palette_dib = struct.pack("<IiiHHIIiiII", 40, 4, 4, 1, 8, 0, 0,
                                  2835, 2835, 0, 0) + b"\x00" * 64
        rep.check(S._dib_to_image(palette_dib, ops) is None,
                  "8 位调色板 DIB → 如实返回 None（不猜）")
        crushed = struct.pack("<IiiHHIIiiII", 40, 400, 400, 1, 24, 0, 0,
                              2835, 2835, 0, 0) + b"\x00" * 16
        rep.check(S._dib_to_image(crushed, ops) is None,
                  "头部声明的像素比实际数据多 → None（不当成花屏图）")
        rep.check(S.copy_image_to_clipboard(None)[0] is False,
                  "没有图时返回失败而不是抛异常")

        # ================================================================
        rep.head("C. 桌面背景：只验文件那一步，绝不动你的桌面")
        # ================================================================
        okw, detailw, wpath = S.write_wallpaper_bmp(
            Image.new("RGBA", (320, 200), (10, 120, 200, 128)))
        rep.check(okw, "能写出壁纸用的 BMP", detailw)
        if okw:
            rep.check(os.path.basename(wpath) == S.WALLPAPER_NAME,
                      "文件名固定，不会越写越多", os.path.basename(wpath))
            with open(wpath, "rb") as fh:
                rep.check(fh.read(2) == b"BM", "写出来的是合法 BMP 文件头")
            with Image.open(wpath) as back:
                back.load()
                rep.check(back.size == (320, 200), "BMP 尺寸正确", "%s" % (back.size,))
                rep.check(back.mode == "RGB",
                          "透明图被合成过（BMP 没有透明通道）", back.mode)
        # 明确不调用 set_wallpaper()：它会真的换掉你现在的桌面背景。
        rep.check(callable(S.set_wallpaper),
                  "set_wallpaper 存在（**故意没有调用它**，免得改掉你的桌面）")
        rep.check(callable(S.wallpaper_bmp_path), "wallpaper_bmp_path 存在")

        # ================================================================
        rep.head("D. 删除到回收站：先把红线守死，再测真删")
        # ================================================================
        sub = os.path.join(tmp, "一个目录")
        os.makedirs(sub, exist_ok=True)
        txt = os.path.join(tmp, "说明.txt")
        with io.open(txt, "w", encoding="utf-8") as fh:
            fh.write("不是图片")

        okd, dd = S.delete_to_recycle_bin(sub)
        rep.check(okd is False and os.path.isdir(sub),
                  "★ 拒绝删除目录，而且目录还在", dd)
        okd, dd = S.delete_to_recycle_bin(os.path.join(tmp, "根本没有.txt"))
        rep.check(okd is False, "拒绝删除不存在的文件", dd)
        okd, dd = S.delete_to_recycle_bin(txt)
        rep.check(okd is False and os.path.isfile(txt),
                  "★ 拒绝删除非图片文件（.txt），而且文件还在", dd)

        victim = os.path.join(tmp, "待删照片.png")
        Image.new("RGB", (40, 30), (200, 40, 40)).save(victim)
        rep.check(os.path.isfile(victim), "先建一张真图片当删除对象")
        okd, dd = S.delete_to_recycle_bin(victim)
        rep.check(okd, "真图片能放进回收站", dd)
        rep.check(not os.path.exists(victim),
                  "★ 删完原位置确实空了（这一步证明它真的动了系统）")
        rep.check(os.path.isfile(txt) and os.path.isdir(sub),
                  "★ 这一轮里其它文件/目录一个都没被碰（只删了你指的那一个）")

        # ================================================================
        rep.head("D2. 删整个目录（AI 模型用）：防呆必须先全过，再测真删")
        # ================================================================
        # 这个函数能把一整棵目录送进回收站，所以**先把每条防呆都踩一遍** ——
        # 每一条都必须"拒绝 + 目标毫发无损"。防呆全过之后才做一次真删。
        rep.check(callable(S.delete_tree_to_recycle_bin),
                  "delete_tree_to_recycle_bin 存在（AI 模型删除要靠它）")

        keep = os.path.join(tmp, "别动我")
        os.makedirs(keep, exist_ok=True)
        with io.open(os.path.join(keep, "重要.txt"), "w", encoding="utf-8") as fh:
            fh.write("不能被删")

        okt, dt = S.delete_tree_to_recycle_bin(keep)
        rep.check(okt is False and os.path.isdir(keep),
                  "★ 没声明 require_basename → 拒绝，而且目录还在", dt)
        okt, dt = S.delete_tree_to_recycle_bin(keep, require_basename="models")
        rep.check(okt is False and os.path.isdir(keep),
                  "★ 目录名对不上 → 拒绝（传错路径就靠这条拦住）", dt)
        okt, dt = S.delete_tree_to_recycle_bin("", require_basename="models")
        rep.check(okt is False, "拒绝空路径", dt)
        okt, dt = S.delete_tree_to_recycle_bin(os.path.join(tmp, "没这个目录"),
                                               require_basename="没这个目录")
        rep.check(okt is False, "拒绝不存在的目录", dt)
        okt, dt = S.delete_tree_to_recycle_bin(txt, require_basename="说明.txt")
        rep.check(okt is False and os.path.isfile(txt),
                  "★ 传进来一个文件 → 拒绝（这是「删目录」的函数），文件还在", dt)

        # 驱动器根：dirname 等于自己，必须拦住
        drive = os.path.splitdrive(os.path.abspath(os.sep))[0] + os.sep
        okt, dt = S.delete_tree_to_recycle_bin(drive, require_basename=drive)
        rep.check(okt is False, "★ 拒绝驱动器根目录（%s）" % drive, dt)

        rep.check(os.path.isfile(os.path.join(keep, "重要.txt")),
                  "★ 上面这一串拒绝之后，被拒的目录里文件一个都没少")

        # 目录联接（junction）：删链接 = 可能删掉它指向的真目录，必须拦。
        # 用 mklink /J（不需要管理员），建不出来就跳过（不算失败）。
        link_base = os.path.join(tmp, "链接宿主")
        os.makedirs(link_base, exist_ok=True)
        junction = os.path.join(tmp, "models")          # 名字故意叫 models 骗过名字检查
        made = False
        try:
            import subprocess
            r = subprocess.run(
                ["cmd", "/c", "mklink", "/J", junction, link_base],
                capture_output=True, timeout=20)
            made = (r.returncode == 0)
        except Exception:                                     # noqa: BLE001
            made = False
        if made:
            okt, dt = S.delete_tree_to_recycle_bin(junction, require_basename="models")
            rep.check(okt is False and os.path.isdir(link_base),
                      "★ 拒绝删除目录联接（junction），而且它指向的真目录还在", dt)
            try:
                os.rmdir(junction)                            # 只删链接本身
            except OSError:
                pass
        else:
            rep.add("  (跳过：本机建不出 junction，不做这条断言)")

        # 现在做一次**真删**：临时建一个叫 models 的目录，塞几个文件
        real_models = os.path.join(tmp, "models_真删")
        os.makedirs(os.path.join(real_models, "onnxruntime"), exist_ok=True)
        for dn in ("birefnet_int8.onnx", "swin2sr_q.onnx", "ai_manifest.json"):
            with io.open(os.path.join(real_models, dn), "wb") as fh:
                fh.write(b"x" * 1024)
        with io.open(os.path.join(real_models, "onnxruntime", "x.py"),
                     "w", encoding="utf-8") as fh:
            fh.write("y")
        # 名字要真的是 "models" 才过得了防呆，所以外面包一层
        box = os.path.join(tmp, "模型盒子")
        os.makedirs(box, exist_ok=True)
        victim_dir = os.path.join(box, "models")
        shutil.move(real_models, victim_dir)
        rep.check(os.path.isdir(victim_dir), "先建一棵真目录当删除对象（含子目录）")

        okt, dt = S.delete_tree_to_recycle_bin(victim_dir, require_basename="models")
        rep.check(okt, "★ 名字对得上的目录能整棵放进回收站", dt)
        rep.check(not os.path.exists(victim_dir),
                  "★ 删完原位置确实空了（这一步证明它真的动了系统）")
        rep.check(os.path.isdir(keep) and os.path.isfile(os.path.join(keep, "重要.txt")),
                  "★ 这一轮里别的目录一个都没被碰")


        # ================================================================
        rep.head("E. 枚举文件夹里的图片（幻灯片要用）")
        # ================================================================
        pics = os.path.join(tmp, "相册")
        os.makedirs(pics, exist_ok=True)
        for name in ("img10.png", "img2.png", "img1.png"):
            Image.new("RGB", (8, 8), (0, 0, 0)).save(os.path.join(pics, name))
        with io.open(os.path.join(pics, "readme.txt"), "w", encoding="utf-8") as fh:
            fh.write("x")
        os.makedirs(os.path.join(pics, "子目录"), exist_ok=True)

        names = [os.path.basename(p) for p in S.list_images(pics)]
        rep.check("readme.txt" not in names, "只列图片，不列 .txt")
        rep.check("子目录" not in names, "不把目录当图片列出来")
        rep.check(names == ["img1.png", "img2.png", "img10.png"],
                  "★ 自然序：img2 排在 img10 之前（字典序会反）",
                  "%s" % (names,))
        rep.check(S.list_images(os.path.join(tmp, "没有这个目录")) == [],
                  "目录不存在时返回空列表而不是抛异常")

        # ================================================================
        rep.head("F. 两个会弹窗的函数：只验参数校验，不真的弹")
        # ================================================================
        missing = os.path.join(tmp, "没有这个文件.png")
        rep.check(S.reveal_in_explorer(missing)[0] is False,
                  "定位一个不存在的文件 → 失败（不会弹出资源管理器）")
        rep.check(S.print_file(missing)[0] is False,
                  "打印一个不存在的文件 → 失败（不会拉起打印程序）")
        rep.check(S.print_image(None)[0] is False,
                  "print_image(None) → 失败（不会拉起打印程序）")
        # print_image 是"所见即所打"：把内存里的 PIL 图导出成 PNG 再交给
        # print_file。这里 stub 掉 print_file / temp_dir，既能验"真的导出了一张
        # 图"，又保证测试（包括在 Windows 上跑）不会真的拉起打印程序。
        try:
            from PIL import Image
            dummy = Image.new("RGB", (4, 4), (255, 0, 0))
            captured = {}
            real_print_file = S.print_file
            real_temp_dir = S.temp_dir

            def fake_print_file(p):
                # ⚠ 不要写成 ``captured.setdefault("path", p) or (True, "stub")``：
                # setdefault 返回的是**插入进去的那个值**（非空路径字符串，
                # 恒为真），``or`` 会短路，于是这个 stub 返回的是路径字符串、
                # 不是元组 —— ``ok, detail = ...`` 当场
                # ``ValueError: too many values to unpack``。
                # 结果是这一整支测试**从来没能跑通过**，等于一直在骗人。
                captured["path"] = p
                return True, "stub"

            S.print_file = fake_print_file
            S.temp_dir = lambda: tempfile.mkdtemp()
            try:
                ok, detail = S.print_image(dummy)
            finally:
                S.print_file = real_print_file
                S.temp_dir = real_temp_dir
            written = captured.get("path", "")
            rep.check(ok is True and os.path.isfile(written),
                      "print_image(有效图) 真的导出了一张 PNG 并交给 print_file",
                      "%s" % written)
            if os.path.isfile(written):
                os.remove(written)
        except ImportError:
            rep.check(True, "（无 Pillow，跳过 print_image 有效图分支）")
        rep.check(S.open_with_default(missing)[0] is False,
                  "用默认程序打开不存在的文件 → 失败")

        # ================================================================
        rep.head("G. 启动子进程时必须清掉 PyInstaller 的内部环境变量")
        # ================================================================
        # 背景（2026-09-17 实测复现）：onefile 程序被"别人"拉起时，引导器靠
        # _PYI_ARCHIVE_FILE 判断"我是第二阶段的子进程"，然后核对父进程的可执行
        # 文件必须和自己相同 —— 不同就弹
        #   "Security validation failure: parent process has different executable!"
        # 而我们的程序会自己拉起别的 exe（重启自己 / 打开更新包），子进程默认
        # 继承环境，于是把这串变量带下去，新进程一启动就报错。
        fake = {"_PYI_ARCHIVE_FILE": "x", "_PYI_PARENT_PROCESS_LEVEL": "1",
                "_PYI_APPLICATION_HOME_DIR": "y", "_MEIPASS": "z",
                "_MEIPASS2": "w", "PATH": "keep-me", "TEMP": "keep-me-too"}
        cleaned = S.clean_child_env(fake)
        rep.check(not [k for k in cleaned if k.startswith("_PYI_")],
                  "clean_child_env 去掉全部 _PYI_*", repr(sorted(cleaned)))
        rep.check("_MEIPASS" not in cleaned and "_MEIPASS2" not in cleaned,
                  "clean_child_env 去掉 _MEIPASS / _MEIPASS2")
        rep.check(cleaned.get("PATH") == "keep-me"
                  and cleaned.get("TEMP") == "keep-me-too",
                  "其它环境变量原样保留（不能顺手清掉整个环境）")
        rep.check(not [k for k in os.environ if k.startswith("_PYI_")],
                  "（前置）测试进程本身没有 _PYI_ 变量，否则下面的断言没意义")

        captured = {}

        class _FakePopen:
            def __init__(self, cmd, **kw):
                captured["cmd"] = cmd
                captured["env"] = kw.get("env")
                captured["shell"] = kw.get("shell")

        real_popen = S.subprocess.Popen
        S.subprocess.Popen = _FakePopen          # 只换这一处，别真启动程序
        try:
            os.environ["_PYI_ARCHIVE_FILE"] = "poison"
            os.environ["_PYI_PARENT_PROCESS_LEVEL"] = "1"
            S.spawn_detached(r"C:\some\app.exe", args=["--flag", 3])
        finally:
            S.subprocess.Popen = real_popen
            os.environ.pop("_PYI_ARCHIVE_FILE", None)
            os.environ.pop("_PYI_PARENT_PROCESS_LEVEL", None)

        rep.check(captured.get("cmd") == [r"C:\some\app.exe", "--flag", "3"],
                  "spawn_detached 把参数原样带上", repr(captured.get("cmd")))
        env_used = captured.get("env") or {}
        rep.check(not [k for k in env_used if k.startswith("_PYI_")],
                  "★ spawn_detached 交给子进程的环境里没有 _PYI_*",
                  repr(sorted(k for k in env_used if k.startswith("_PYI_"))))
        rep.check(captured.get("shell") is False,
                  "spawn_detached 不走 shell（路径带空格也不会被拆开）")

        # 静态守卫：这两个"启动别的 exe"的地方，源码里必须走清理后的路径。
        # 动态测试照不到它们（真启动会拉起窗口），所以用源码断言钉住。
        opt_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "optimized")
        with io.open(os.path.join(opt_dir, "main.py"), encoding="utf-8") as fh:
            main_src = fh.read()
        rep.check("os.startfile(" not in main_src,
                  "main.py 不再直接用 os.startfile（它没法清环境）",
                  "仍出现 %d 次" % main_src.count("os.startfile("))
        rep.check(main_src.count("shell_ops.spawn_detached(") >= 1
                  and "shell_ops.clean_child_env(" in main_src,
                  "main.py 启动子进程走 shell_ops 的清理版")
        with io.open(os.path.join(opt_dir, "installer.py"),
                     encoding="utf-8") as fh:
            inst_src = fh.read()
        rep.check("clean_child_env()" in inst_src
                  and "env=clean_child_env()" in inst_src,
                  "installer.py 启动新程序时传了清理后的环境")

        # ================================================================
        rep.head("H. 反馈邮件：mailto 链接的编码")
        # ================================================================
        # 这段必须逐字节验：编码写错了邮件里就是乱码或串行，而那时
        # 用户看到的只是"发出去了"，很难发现。
        rep.check(S._percent("a b") == "a%20b",
                  "空格编成 %20（不是 +：mailto 里 + 就是加号本人）",
                  S._percent("a b"))
        rep.check(S._percent("x&y=1?#") == "x%26y%3D1%3F%23",
                  "& = ? # 这些分隔符一律编码（否则参数会被拆开）",
                  S._percent("x&y=1?#"))
        rep.check(S._percent("中") == "%E4%B8%AD",
                  "中文按 UTF-8 **逐字节**编码（按字符编会乱码）",
                  S._percent("中"))
        rep.check(S._percent("a\nb") == "a%0Ab",
                  "换行编成 %0A（正文里的换行要保得住）", S._percent("a\nb"))
        rep.check(S._percent("~-._") == "~-._",
                  "RFC 3986 的 unreserved 字符不编码", S._percent("~-._"))

        captured = {}

        def fake_startfile(url):
            captured["url"] = url

        real_startfile = S.os.startfile
        S.os.startfile = fake_startfile          # 别真去拉起邮件程序
        try:
            ok_m, why_m = S.open_mailto("a@b.com", "标题", "正文\n第二行")
            bad_m = S.open_mailto("这不是地址", "s", "b")
            saved_max = S.MAILTO_MAX
            S.MAILTO_MAX = 30                    # 造一个"太长"的场景
            long_m = S.open_mailto("a@b.com", "s", "很长的正文" * 20)
            S.MAILTO_MAX = saved_max
        finally:
            S.os.startfile = real_startfile

        rep.check(ok_m, "正常内容能交给系统邮件程序", why_m)
        url = captured.get("url", "")
        rep.check(url.startswith("mailto:a@b.com?"),
                  "链接是 mailto: + 收件人", url[:40])
        rep.check(url.count("&") == 1,
                  "两个参数之间只有**一个** & 分隔符（正文里的 & 已编码）",
                  "%d 个" % url.count("&"))
        rep.check("%0A" in url, "正文里的换行被编码进链接")
        rep.check(" " not in url, "链接里不能有裸空格（ShellExecute 会截断）")
        rep.check(not bad_m[0] and "地址" in bad_m[1],
                  "地址不合法 → 明确拒绝", repr(bad_m)[:80])
        rep.check(not long_m[0] and "太长" in long_m[1],
                  "太长 → 明确拒绝并建议用「复制文字」（不能截断后照发）",
                  repr(long_m)[:100])

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
