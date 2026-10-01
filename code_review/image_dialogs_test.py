# -*- coding: utf-8 -*-
"""``image_dialogs.py`` 的回归测试（需要 Tk，但不需要人操作）。

盯的是五条不变量：
  1. **锁纵横比从"被改的那一边"算**：只从宽算高的话，用户改高会被顶回去 ——
     高框像被焊住一样。这条是修过的真 bug，必须钉死；
  2. **_sync 闸门**：写回 → trace → 再进同步，没有闸门数值会越滚越大；
  3. **数值全部被夹紧**：宽高夹在 [1, MAX_SIDE]、比例夹在 [1%, 2000%]；
  4. **预览与最终共用同一条管线**：调整对话框的预览用 ~1.4MP 底图（跟手），
     「应用」时在全分辨率上重算 —— 两边算式必须一字不差；
  5. **alpha 通道活下来**：调整/转格式的任何一步都不许把透明弄丢或弄黑。
"""
from __future__ import annotations

import gc
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
OPTIMIZED = os.path.join(HERE, "optimized")
OUT = os.path.join(HERE, "_image_dialogs.txt")

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
        head = ["=" * 68, "image_dialogs.py 回归测试", "=" * 68]
        if self.failures:
            tail = ["", "=" * 68, "FAILED: %d 项不成立" % len(self.failures)]
            tail += ["  * " + f for f in self.failures]
            tail.append("=" * 68)
        else:
            tail = ["", "=" * 68, "RESULT: OK", "=" * 68]
        return "\n".join(head + self.lines + tail) + "\n"


def main():
    rep = Report()
    import tkinter as tk

    from PIL import Image

    import image_dialogs as D
    import image_ops as ops
    from ui_theme import BG_ELEV

    root = tk.Tk()
    root.title("image_dialogs probe")
    root.geometry("760x720+40+40")
    root.configure(bg=BG_ELEV)
    root.update()

    holders = []

    def make_holder(height=None):
        h = tk.Toplevel(root)
        h.configure(bg=BG_ELEV)
        h.geometry("700x680+20+20")
        if height:
            h.geometry("700x%d+20+20" % height)
        holders.append(h)
        return h

    try:
        # ================================================================
        rep.head("A. 模块纯度")
        # ================================================================
        with open(os.path.join(OPTIMIZED, "image_dialogs.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        for bad in ("urllib", "socket", "subprocess", "ctypes", "requests"):
            rep.check(bad not in src, "源码不碰 %r" % bad)
        rep.check("from __future__ import annotations" in src,
                  "带 __future__ annotations（32 位 Python 3.8 要编译它）")

        # ================================================================
        rep.head("B. 修改尺寸：锁定纵横比 + 夹紧")
        # ================================================================
        SIZE = (800, 600)

        def new_resize():
            holder = make_holder()
            got = {}
            form = D.build_resize_body(
                holder, size=SIZE, on_px=lambda w, h: got.setdefault("px", (w, h)),
                on_pct=lambda p: got.setdefault("pct", p))
            holder.update()
            return holder, form, got

        holder, form, got = new_resize()
        rep.check(form._ready, "构造完成后 _ready 闸门打开（构建期回调全被挡住）")
        rep.check(form.target() == ("px", 800, 600, None), "初始 target = 原尺寸",
                  str(form.target()))
        rep.check("800 × 600" in form.result.cget("text"),
                  "结果预览说了『将得到 800 × 600』", form.result.cget("text"))

        # 锁定 + 从宽算高
        form.w_entry.set("400")
        rep.check(form.h_entry.get() == "300", "锁比：宽 800→400，高自动 600→300",
                  "h_entry=%r" % form.h_entry.get())

        # ★ 锁定 + 从高算宽（修过的 bug：以前高框被焊住）
        form2_holder, form2, _ = new_resize()
        form2.h_entry.set("150")
        rep.check(form2.w_entry.get() == "200",
                  "锁比：高 600→150，宽自动 800→200（从被改的那一边算）",
                  "w_entry=%r" % form2.w_entry.get())

        # 解锁后改高不动宽
        form2.lock.set(False, notify=True)
        form2.h_entry.set("333")
        rep.check(form2.w_entry.get() == "200", "解锁后改高，宽不再被拖着走",
                  "w_entry=%r" % form2.w_entry.get())

        # 夹紧：超大值
        form.w_entry.set("999999999")
        mode, w, h, _ = form.target()
        rep.check(w == D.MAX_SIDE and h == int(round(D.MAX_SIDE * 600 / 800.0)),
                  "超大宽度被夹到 MAX_SIDE，高按夹紧后的值算",
                  "target=%r" % ((mode, w, h),))

        # 百分比模式
        form3_holder, form3, got3 = new_resize()
        form3.mode.set("pct", notify=True)
        holder3 = form3_holder
        holder3.update()
        rep.check(form3.presets.get() == "100", "切到百分比模式，预设停在 100%")
        form3.presets.set("50", notify=True)
        rep.check(form3.target() == ("pct", 400, 300, 50.0),
                  "点 50% 预设 → target = 半尺寸", str(form3.target()))
        form3.pct_entry.set("99999")
        _m, pw, ph, pct = form3.target()
        rep.check(pct == 2000.0 and pw == 16000 and ph == 12000,
                  "比例被夹在 [1%, 2000%]", "%r %r %r" % (pw, ph, pct))

        # 应用出口
        form.apply()
        rep.check(got.get("px") == (D.MAX_SIDE, 75000),
                  "apply（像素模式）把夹紧后的值交给 on_px", str(got))
        form3.apply()
        rep.check(got3.get("pct") == 2000.0,
                  "apply（百分比模式）把比例交给 on_pct", str(got3))
        for h_ in (holder, form2_holder, form3_holder):
            h_.destroy()

        # ================================================================
        rep.head("C. 转换格式：格式联动 + 体积预估")
        # ================================================================
        img_rgba = Image.new("RGBA", (64, 64), (200, 30, 30, 180))
        holder = make_holder()
        got = {}
        form = D.build_convert_body(
            holder, image=img_rgba, fmt="JPEG", exif_bytes=None,
            source_bytes=12345, on_ok=lambda f, q, k: got.update(
                fmt=f, q=q, keep=k))
        holder.update()
        rep.check(form.get_format() == "JPEG", "默认 JPEG")
        form._estimate()          # 防抖 180ms 还没到点，测试直接调一次拿结果
        rep.check("预计约" in form.estimate.cget("text"),
                  "体积预估真的编码了一遍", form.estimate.cget("text"))

        form.set_format("PNG")
        rep.check(form.quality.slider._disabled is True,
                  "PNG 是无损格式，画质滑块被禁用")
        rep.check("无损" in form.quality_note.cget("text"),
                  "并说明了为什么", form.quality_note.cget("text"))

        form.set_format("JPEG")
        rep.check("白底" in form.warn.cget("text"),
                  "透明图转 JPEG 警告合成到白底", form.warn.cget("text"))

        form.set_format("GIF")
        rep.check("两档" in form.warn.cget("text"),
                  "透明图转 GIF 警告半透明变硬", form.warn.cget("text"))

        # EXIF 开关只在"有 EXIF 且格式存得下"时可用
        rep.check(form.keep_exif._enabled is False, "没有 EXIF 可保留 → 勾选禁用")

        img_big = Image.new("RGBA", (512, 512), (10, 10, 10, 255))
        holder_big = make_holder()
        form_big = D.build_convert_body(holder_big, image=img_big, fmt="ICO")
        holder_big.update()
        rep.check(bool(form_big.warn.cget("text")),
                  "512×512 转 ICO 有警告（会被缩小/补边）",
                  form_big.warn.cget("text"))

        form.set_format("PNG")
        form.apply()
        rep.check(got.get("fmt") == "PNG", "apply 把选中的格式交给 on_ok",
                  str(got))
        rep.check(isinstance(got.get("q"), int), "画质以 int 交给 on_ok",
                  repr(got.get("q")))
        holder.destroy()
        holder_big.destroy()

        # ================================================================
        rep.head("D. 裁剪：两套坐标系 + 比例")
        # ================================================================
        img = Image.new("RGB", (800, 600), (40, 90, 160))
        holder = make_holder()
        got = {}
        form = D.build_crop_body(holder, image=img,
                                 on_ok=lambda box: got.setdefault("box", box))
        holder.update()
        cv = form.canvas
        rep.check(cv.crop_box() == (0, 0, 800, 600), "初始框 = 整张图",
                  str(cv.crop_box()))

        # 坐标换算往返
        cx, cy = cv._to_canvas(123, 45)
        ix, iy = cv._to_image(cx, cy)
        rep.check(abs(ix - 123) < 1e-6 and abs(iy - 45) < 1e-6,
                  "图片坐标 → 画布坐标 → 图片坐标 原地往返",
                  "%.4f,%.4f" % (ix, iy))

        # 比例
        cv.set_aspect("1:1")
        l, t, r, b = cv._rect
        rep.check(abs((r - l) - (b - t)) < 1e-6, "1:1 → 框是正方形",
                  "w=%.1f h=%.1f" % (r - l, b - t))
        cv.set_aspect("16:9")
        l, t, r, b = cv._rect
        rep.check(abs((r - l) / (b - t) - 16 / 9.0) < 1e-6 and
                  r <= 800 + 1e-6 and b <= 600 + 1e-6,
                  "16:9 → 框保持比例且在内切图片范围内",
                  "ratio=%.4f" % ((r - l) / (b - t)))

        # 角手柄：对角是不动点，比例被保持
        cv.set_aspect("4:3")
        rect0 = list(cv._rect)
        fixed = (rect0[0], rect0[1])          # nw 角不动
        new_rect = cv._resize_from("se", rect0, 300, 500)
        w0, h0 = rect0[2] - rect0[0], rect0[3] - rect0[1]
        w1, h1 = new_rect[2] - new_rect[0], new_rect[3] - new_rect[1]
        rep.check(abs(w1 / h1 - 4 / 3.0) < 0.02,
                  "拖 se 角，框保持 4:3", "w=%.1f h=%.1f" % (w1, h1))
        rep.check(abs(new_rect[0] - fixed[0]) < 1e-6 and
                  abs(new_rect[1] - fixed[1]) < 1e-6,
                  "对角（nw）是不动点", "moved=%.3f,%.3f" %
                  (new_rect[0] - fixed[0], new_rect[1] - fixed[1]))
        rep.check(0 <= new_rect[0] and new_rect[2] <= 800 and
                  0 <= new_rect[1] and new_rect[3] <= 600,
                  "新框仍整在图片内")

        # 夹紧 + 重置
        cv.set_aspect("free")
        cv._rect = [-50.0, -50.0, 900.0, 900.0]
        box = cv.crop_box()
        rep.check(box == (0, 0, 800, 600), "越界框被 ops.clamp_box 收回来",
                  str(box))
        cv.reset()
        rep.check(cv.crop_box() == (0, 0, 800, 600) and cv.aspect == "free",
                  "重置回整图 + 自由比例")

        form.apply()
        rep.check(got.get("box") == (0, 0, 800, 600), "apply 把整数框交给 on_ok",
                  str(got))
        holder.destroy()

        # ================================================================
        rep.head("E. 调整：预览/最终同管线 + alpha 保护")
        # ================================================================
        img = Image.new("RGBA", (160, 120), (120, 90, 60, 200))
        holder = make_holder()
        got = {}
        form = D.build_adjust_body(
            holder, image=img,
            on_ok=lambda im, ident: got.update(img=im, ident=ident))
        holder.update()
        rep.check(form.is_identity(), "默认全 100% → is_identity")
        rep.check(form.viewer is not None and form.viewer.winfo_exists(),
                  "预览查看器建起来了")

        # 预览底图降采样
        big = Image.new("RGB", (3000, 2000), (5, 5, 5))
        base = D.AdjustForm._preview_base(big)
        rep.check(base is not None and
                  base.width * base.height <= D.PREVIEW_PIXELS + 4096,
                  "6MP 底图被降到 ~1.4MP 供预览",
                  "%d×%d" % base.size if base is not None else "None")
        rep.check(D.AdjustForm._preview_base(img) is img,
                  "小图直接用原图做底图（不复制）")

        # 滑块 → 参数；管线保持尺寸；alpha 逐字节不变
        form.sliders["brightness"].set(150)
        rep.check(not form.is_identity(), "亮度 150% 后不再是 identity")
        alpha0 = img.getchannel("A").tobytes()
        out = form.process(img)
        rep.check(out.size == img.size, "处理不改变尺寸", str(out.size))
        rep.check(out.getchannel("A").tobytes() == alpha0,
                  "★ 调整前后 alpha 通道逐字节相等")

        # 预览与最终同管线：同一套设置下，同尺寸底图的输出必须完全一致
        form._tone = "gray"
        preview_out = form.process(form._base)
        params = {k: v / 100.0 for k, v in
                  (("brightness", 150.0), ("contrast", 100.0),
                   ("color", 100.0), ("sharpness", 100.0))}
        direct = ops.adjust(ops.grayscale(form._base), **params)
        rep.check(preview_out.tobytes() == direct.tobytes(),
                  "预览像素 == 手工复算的同一管线（所见即所得）")

        form._tone = None
        form._render_preview()
        rep.check(form.viewer.has_image(), "预览渲染出图了")

        # 全分辨率重算
        img_full = Image.new("RGBA", (1600, 1200), (90, 90, 90, 255))
        form.image = img_full
        result = form.result_image()
        rep.check(result.size == (1600, 1200),
                  "result_image 在全分辨率上重算", str(result.size))
        rep.check(result.getchannel("A").tobytes() ==
                  img_full.getchannel("A").tobytes(),
                  "全分辨率重算同样保住 alpha")

        form.apply()
        rep.check(got.get("img") is not None and got.get("ident") is False,
                  "apply 把结果图 + identity 标志交给 on_ok")
        holder.destroy()

        # ================================================================
        rep.head("F. 对话框外壳：**真的 show() 一遍**（不只是构建）")
        # ================================================================
        # 为什么这里必须真调 show()：
        # show() 里有 wait_window（阻塞），早期测试图省事只构建不显示，
        # 而 position 计算（唯一用到 self.dx / self.dy 的两行）正好在 show()
        # 里面 —— 于是"BaseDialog 忘了把 dx/dy 存成属性"这个缺陷，
        # 让**五个对话框全部点不开**，却一路绿灯潜伏了整整一个版本
        # （窗口版没有控制台，异常被 tk 静默吞掉，用户只看到"点了没反应"）。
        # 做法：先排一个 after 去关窗，再进 show()，让它自己退出来。
        def _smoke(dlg, label):
            job = root.after(90, dlg.cancel)
            try:
                dlg.show()
                rep.check(True, "%s 能真正打开并关闭（show 全程无异常）" % label)
            except Exception as exc:                      # noqa: BLE001
                try:
                    root.after_cancel(job)
                except (tk.TclError, ValueError):
                    pass
                rep.check(False,
                          "%s 能真正打开并关闭（show 全程无异常）" % label,
                          "%s: %s" % (type(exc).__name__, exc))

        _smoke(D.ResizeDialog(root, size=SIZE, on_px=lambda w, h: None),
               "ResizeDialog")
        _smoke(D.ConvertDialog(root, image=img, fmt="PNG", quality=90),
               "ConvertDialog")
        _smoke(D.CropDialog(root, image=img, on_ok=lambda box: None),
               "CropDialog")
        _smoke(D.AdjustDialog(root, image=img, on_ok=lambda im, i: None),
               "AdjustDialog")
        _smoke(D.PasswordDialog(root, on_ok=lambda text: text == "0228"),
               "PasswordDialog")

        # ---- 这两个是**补的洞**：以前 F 节只跑五个对话框，画笔抠图弹窗
        # （AI → 选择性抠图 / 画笔抠图）与叠加图片弹窗从来没被 show() 过。
        # 而"show() 里才算窗口位置、唯一用到 self.dx/self.dy 的那行"正是
        # 历史上让五个对话框全点不开的地方 —— 只构建不显示等于没测。
        # （2026-09-21 补）
        _mb_base = Image.new("RGBA", (200, 150), (200, 120, 40, 255))
        _mb_orig = Image.new("RGB", (200, 150), (200, 120, 40))
        _smoke(D.MatteBrushDialog(root, base_rgba=_mb_base, orig_rgb=_mb_orig,
                                  on_ok=lambda rgba: None),
               "MatteBrushDialog（AI 画笔抠图）")
        _smoke(D.OverlayDialog(root, base=_mb_base,
                               patch=Image.new("RGBA", (20, 20), (255, 0, 0, 255)),
                               name="测试叠加.png"),
               "OverlayDialog（叠加图片）")

        # 进度窗口用的是 present()（**不阻塞**），所以不能走 _smoke。
        # 这一节盯三件事：能显示、能被喂进度、能显示校验结果并放出关闭按钮。
        pd = D.ProgressDialog(root, title="正在下载更新 9.9.9",
                              subtitle="测试用")
        pd.present()
        root.update()
        rep.check(bool(pd.form.bar.winfo_ismapped()), "进度窗口显示出来了（present 不阻塞）")
        pd.form.set_note("正在取更新清单…")
        root.update()
        rep.check("更新清单" in pd.form.line.cget("text"),
                  "能在开始前先写一句状态", repr(pd.form.line.cget("text")))
        pd.form.set_progress(500, 2000, "download")
        root.update()
        rep.check(abs(pd.form._ratio - 0.25) < 1e-6,
                  "按比例更新（500/2000 → 25%）", repr(pd.form._ratio))
        rep.check("已下载" in pd.form.line.cget("text")
                  and "25%" in pd.form.line.cget("text"),
                  "文字里带已下/总量和百分比",
                  repr(pd.form.line.cget("text")))
        pd.form.set_progress(2000, 2000, "verify")
        root.update()
        rep.check(abs(pd.form._ratio - 1.0) < 1e-6
                  and "校验" in pd.form.line.cget("text"),
                  "进校验阶段时进度条满格且文字切换",
                  repr(pd.form.line.cget("text")))
        pd.form.set_result(True, "校验通过 · sha256 与清单一致（abcdef123456…）")
        root.update()
        rep.check("✓" in pd.form.result.cget("text")
                  and "sha256" in pd.form.result.cget("text"),
                  "校验结果看得见（带 ✓ 和 sha256）",
                  repr(pd.form.result.cget("text")))
        rep.check(pd.form._close_btn is not None,
                  "出结果之后才有「关闭」按钮")
        pd.cancel()
        root.update()
        rep.check(not pd.winfo_exists(), "进度窗口能关掉且不抛异常")

        # ================================================================
        # 叠加窗口：拖动定位 / 滚轮缩放 / 取结果
        # ================================================================
        ov_base = Image.new("RGBA", (200, 120), (0, 0, 255, 255))
        ov_patch = Image.new("RGBA", (40, 30), (255, 0, 0, 255))
        ov = D.OverlayDialog(root, base=ov_base)
        ov.present(modal=False)
        root.update()
        rep.check(ov.form.preview_image() is not None,
                  "还没选叠加图时也能显示底图预览（不是空白）")
        ov.form.set_patch(ov_patch, "红块.png")
        root.update()
        rep.check(ov.form.patch is ov_patch, "选好之后表单记住了这张图")
        rep.check("红块.png" in ov.form.info.cget("text"),
                  "界面上写了叠加图的名字与尺寸", repr(ov.form.info.cget("text")))
        rep.check(abs(ov.form.pos[0] - 80.0) < 1e-6
                  and abs(ov.form.pos[1] - 45.0) < 1e-6,
                  "默认摆在正中间（不是左上角）", "%s" % (ov.form.pos,))

        before = list(ov.form.pos)
        ov.form.move_by(7, -3)
        rep.check(abs(ov.form.pos[0] - before[0] - 7) < 1e-6
                  and abs(ov.form.pos[1] - before[1] + 3) < 1e-6,
                  "拖动按**底图坐标**位移（预览缩小过也不影响结果）",
                  "%s" % (ov.form.pos,))
        ov.form.bump_scale(20)
        rep.check(ov.form.scale_pct == 120.0, "滚轮能改缩放",
                  "%r" % ov.form.scale_pct)
        ov.form.bump_scale(9999)
        rep.check(ov.form.scale_pct == 400.0, "缩放有上限，不会失控",
                  "%r" % ov.form.scale_pct)

        got = ov.form.result_image()
        rep.check(got is not None and got.size == ov_base.size,
                  "取到的结果和底图一样大", "%s" % (getattr(got, "size", None),))
        rep.check(ov_base.getpixel((5, 5)) == (0, 0, 255, 255),
                  "★ 预览与取结果都不会改动底图本身")
        prev = ov.form.preview_image()
        rep.check(prev.size[0] <= D.PREVIEW_MAX[0] + 2
                  and prev.size[1] <= D.PREVIEW_MAX[1] + 2,
                  "预览尺寸不超过画布上限（否则对话框会被撑爆）",
                  "%s" % (prev.size,))
        ov.cancel()
        root.update()
        rep.check(not ov.winfo_exists(), "叠加窗口能关掉")

        # ================================================================
        # 反馈：隐私说明（第一次必须先过）+ 在程序里打字的输入框
        # ================================================================
        agreed, declined = [], []
        cn = D.ConsentDialog(root, paragraphs=("一、只发你打的字。", "二、不发文件。"),
                             note="说明版本 v1",
                             on_agree=lambda: agreed.append("yes"),
                             on_decline=lambda: declined.append("no"))
        cn.present(modal=False)
        root.update()
        rep.check(cn.winfo_exists(), "隐私说明窗口能显示出来")
        cn.form.decline()
        root.update()
        rep.check(declined == ["no"] and not agreed,
                  "「不同意」走的是不同意那条路", "%r / %r" % (agreed, declined))
        rep.check(not cn.winfo_exists(), "点不同意之后窗口关掉")

        cn2 = D.ConsentDialog(root, paragraphs=("一、只发你打的字。",),
                              on_agree=lambda: agreed.append("yes"))
        cn2.present(modal=False)
        root.update()
        cn2.form.agree()
        root.update()
        rep.check(agreed == ["yes"], "「同意并继续」走的是同意那条路", "%r" % (agreed,))
        rep.check(not cn2.winfo_exists(), "点同意之后窗口也关掉")

        sent, copied = [], []
        fb = D.FeedbackDialog(
            root, version_info="HAAVIK Photo 9.9.9 · Windows 10.0.1",
            on_send=lambda body, reply_to="": (
                sent.append((body, reply_to)), (True, "已交给邮件程序。"))[1],
            on_copy=lambda body: (copied.append(body), (True, "已复制。"))[1],
            on_policy=lambda: None)
        fb.present(modal=False)
        root.update()
        rep.check(fb.form.attach is not None and fb.form.attach.get(),
                  "默认勾选「附上版本与系统信息」")
        rep.check("Windows" in fb.form.version_info,
                  "勾选框旁边就写着会附上什么（用户看得见原文）",
                  repr(fb.form.version_info))

        fb.form.send()
        root.update()
        rep.check(not sent and "先写点什么" in fb.form.msg.cget("text"),
                  "空内容不放行（会明确说一句，而不是弹一封空邮件）",
                  repr(fb.form.msg.cget("text")))

        fb.form.text.insert("1.0", "有个小问题")
        fb.form._on_modified()
        root.update()
        rep.check(fb.form.typed() == "有个小问题"
                  and "5 字" in fb.form.count.cget("text"),
                  "能打字，并且字数实时显示", repr(fb.form.count.cget("text")))
        rep.check("有个小问题" in fb.form.composed()
                  and "Windows" in fb.form.composed(),
                  "勾选时：会发出去的内容 = 打的字 + 附带的版本信息")
        fb.form.attach.set(False)
        rep.check(fb.form.composed() == "有个小问题",
                  "取消勾选后：只发用户打的字", repr(fb.form.composed()))
        fb.form.attach.set(True)

        fb.form.send()
        root.update()
        rep.check(len(sent) == 1 and "有个小问题" in sent[0][0],
                  "发送时把用户打的内容交给调用方", repr(sent)[:80])
        # ---- 回信邮箱（选填）：默认没填 → 交出去的必须是空串 ----
        rep.check(sent[0][1] == "",
                  "★ 没留邮箱时，交给发送层的回信地址是空串"
                  "（不能把占位提示当地址发出去）", repr(sent[0][1]))
        rep.check("回信邮箱" not in sent[0][0],
                  "★ 没留邮箱 → 正文里不该出现「回信邮箱」那一行",
                  repr(sent[0][0])[:80])

        # 填一个 → 正文里出现，而且作为 reply_to 交出去
        sent.clear()
        # 用 RoundEntry.set()（它的正规入口）—— 直接改 var 会被占位逻辑清掉
        fb.form.reply_entry.set("  me@example.com  ")       # 故意带首尾空格
        root.update()
        rep.check(fb.form.reply_to() == "me@example.com",
                  "★ 回信邮箱去掉首尾空格", repr(fb.form.reply_to()))
        fb.form.send()
        root.update()
        rep.check("回信邮箱：me@example.com" in sent[0][0],
                  "★ 留了邮箱 → 正文里写明「回信邮箱：…」"
                  "（用户看得见会发什么）", repr(sent[0][0])[-60:])
        rep.check(sent[0][1] == "me@example.com",
                  "★ 同时把地址交给发送层（那一层会设 Reply-To）",
                  repr(sent[0][1]))
        # 清掉，别影响后面的用例
        fb.form.reply_entry.set("")
        root.update()

        rep.check("邮件程序" in fb.form.msg.cget("text"),
                  "发送之后有结果反馈（不是静默）",
                  repr(fb.form.msg.cget("text")))
        fb.form.copy()
        root.update()
        rep.check(len(copied) == 1 and "有个小问题" in copied[0],
                  "「复制文字」把内容交给调用方")
        rep.check(fb.form.policy_btn is not None and callable(fb.form.on_policy),
                  "同意过之后仍有「隐私说明」入口可以重读")

        # 太长时要提醒（mailto 那条路另有硬上限，这里先劝一次）
        fb.form.text.delete("1.0", "end")
        fb.form.text.insert("1.0", "很长" * (D.FeedbackForm.SOFT_LIMIT // 2 + 10))
        fb.form._on_modified()
        root.update()
        rep.check("太长" in fb.form.count.cget("text"),
                  "超过建议长度会给提示", repr(fb.form.count.cget("text"))[:60])
        fb.cancel()
        root.update()
        rep.check(not fb.winfo_exists(), "反馈窗口能关掉")

        probe = D.ResizeDialog(root, size=SIZE, on_px=lambda w, h: None)
        rep.check(isinstance(getattr(probe, "dx", None), int) and
                  isinstance(getattr(probe, "dy", None), int),
                  "BaseDialog 把 dx/dy 存成了属性（show 定位要靠它们）",
                  "dx=%r dy=%r" % (getattr(probe, "dx", "缺失"),
                                   getattr(probe, "dy", "缺失")))
        probe.destroy()

        gc.collect()
        rep.add("（外壳销毁无异常；Tcl_AsyncDelete 靠最后统一 gc 收尾）")

        # ================================================================
        rep.head("G. 口令框（左下角白点的隐藏入口用）")
        # ================================================================
        holder = tk.Frame(root, bg="#1e212a")
        holders.append(holder)
        accepted = []
        closed = []

        def _check(text):
            accepted.append(text)
            return text == "0228"

        form = D.build_password_body(holder, on_ok=_check,
                                     close=lambda: closed.append(1),
                                     subtitle="测试用")
        # 注意参数顺序：本文件是 check(条件, "说明")。写反了不会报错，
        # 只会因为"非空字符串恒为真"而静默变成永远通过。
        rep.check(form.entry.entry.cget("show") != "",
                  "输入是掩码的（不回显明文）",
                  repr(form.entry.entry.cget("show")))
        rep.check(form.buttons is not None, "有关键的「确定」按钮")

        form.entry.set("1234")
        form.apply()
        rep.check(accepted[-1] == "1234", "on_ok 拿到用户输入", repr(accepted[-1]))
        rep.check(not closed, "口令不对时不关窗")
        rep.check(form.entry.get() == "", "口令不对时输入被清空")
        rep.check(form.err.cget("text").strip() != "", "给出错误提示",
                  form.err.cget("text"))

        form.entry.set("0228")
        form.apply()
        rep.check(closed == [1], "口令正确时关窗", repr(closed))
        holder.destroy()

    except Exception:
        rep.check(False, "测试过程抛了异常")
        rep.add(traceback.format_exc())
    finally:
        try:
            for h in holders:
                try:
                    h.destroy()
                except tk.TclError:
                    pass
            root.destroy()
        except tk.TclError:
            pass
        gc.collect()

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(rep.text())

    try:
        import _redact
        _redact.scrub_tree(HERE)
    except Exception:
        pass

    print(rep.text().splitlines()[-3])
    sys.exit(1 if rep.failures else 0)


if __name__ == "__main__":
    main()
