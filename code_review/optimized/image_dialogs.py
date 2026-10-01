# -*- coding: utf-8 -*-
"""四个对话框：修改尺寸 / 转换格式 / 裁剪 / 调整。

它们全部建立在 ``ui_theme`` 的自绘控件上（圆角按钮、圆角输入框、自绘滑块、
自绘勾选、分段控件），没有一个是 tkinter 默认的灰色方块控件 —— 那正是
"粗糙感"的唯一来源。

一条必须遵守的结构约束
==================================================================
``build_resize_body(parent, ...)`` 与 ``build_convert_body(parent, ...)``
是**可以脱离对话框单独渲染**的：``layout_test.py`` 会拿一个裸 ``tk.Toplevel``
直接调它们，量出"在 150% 显示缩放下这个对话框会不会比屏幕还高"。
比屏幕还高意味着底部那排「取消 / 确定」被顶到屏幕外 —— 那是用户真会遇到的
故障，而肉眼在 100% 缩放下永远看不到。

所以这两个函数：
  * 只接受一个 parent 控件，**不向调用方要 top**（用 ``parent.winfo_toplevel()``
    自己往上找窗口）；
  * 不假设自己长在某个 Dialog 类里；
  * 返回值是一个 form 对象，调用方从它读值或触发应用。

每个表单都有 ``_ready`` 这道闸门
==================================================================
控件是**一个一个建起来**的，而建控件的过程本身会触发回调（``RoundEntry``
一 ``set()`` 就发 ``on_change``、``SliderRow`` 一构造就发一次 ``command``）。
回调里再去碰"还没被创建出来的那个标签"，就是 ``AttributeError``。
所以：``_ready`` 在 ``__init__`` 最后一行才置真，之前所有回调一律直接返回。
这不是防御性编程，是把"控件创建顺序"这件事显式化。

四个对话框各自要解决一个"看起来简单其实有坑"的问题
==================================================================
  * **修改尺寸** —— 锁定纵横比时改一边算另一边，而"算出来又写回去"会触发
    第二次 trace，不挡住就是死循环（或数值越滚越大）。用 ``_sync`` 闸门。
  * **转换格式** —— 体积预估要真在内存里编码一遍（不准的预估不如不显示），
    但那是几十毫秒的活，必须防抖，否则拖滑块时窗口一顿一顿的。
  * **裁剪** —— 两套坐标系（图片坐标 / 画布坐标），混了就表现为"框跑偏"；
    有长宽比时还得"以某个角为不动点，把框收进图片范围内"。
  * **调整** —— 滑块每次动都要重算整张预览。做法是先把底图缩到 ~1.4MP
    再算预览（快），点「应用」时才在**全分辨率**上重算一遍（准）。
"""
from __future__ import annotations

import logging
import tkinter as tk

import ai_ops as ai_ops
import image_ops as ops
import image_view
from PIL import Image, ImageDraw
from ui_theme import (ACCENT, BG_ELEV, BG_SUNKEN, BG_VIEW, CheckRow, FG_BAD,
                      FG_DIM, FG_MUTED,
                      FG_OK, FG_TEXT, FG_WARN, RadioRow, RoundButton,
                      RoundEntry, Segmented, SliderRow, STROKE_SOFT,
                      dark_titlebar, font, round_rect)

try:
    from PIL import ImageTk
except ImportError:                                     # noqa: BLE001
    ImageTk = None

log = logging.getLogger("haavik.dialogs")

#: 单边最大像素数。再大内存就不可控了（也要拦住"我把宽改成 1000000000"）。
MAX_SIDE = 100_000

#: 超过这个像素数就提醒一句"会慢"。
BIG_RESULT_MP = 50.0

#: 调整对话框的预览底图上限。预览要跟手，就不能拿 5000 万像素去算。
PREVIEW_PIXELS = 1_400_000

#: 滑块防抖（毫秒）。
SLIDER_DEBOUNCE_MS = 90

#: 体积预估防抖（毫秒）。编码一张全尺寸图是几十毫秒的活，不能每帧都做。
ESTIMATE_DEBOUNCE_MS = 180


# ==========================================================================
# 小工具
# ==========================================================================
def _parse_int(text, default: int) -> int:
    try:
        return int(float(str(text).strip()))
    except (TypeError, ValueError):
        return int(default)


def _parse_float(text, default: float) -> float:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return float(default)


def _clamp_int(text, default: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, _parse_int(text, default)))


def _sep(parent, *, pady=(2, 12), color=STROKE_SOFT):
    tk.Frame(parent, bg=color, height=1).pack(fill="x", pady=pady)


def _heading(parent, title, subtitle=None):
    box = tk.Frame(parent, bg=BG_ELEV)
    box.pack(fill="x")
    tk.Label(box, text=title, bg=BG_ELEV, fg=FG_TEXT, font=font(12, "bold"),
             anchor="w").pack(anchor="w")
    if subtitle:
        tk.Label(box, text=subtitle, bg=BG_ELEV, fg=FG_DIM, font=font(9),
                 anchor="w", justify="left").pack(anchor="w", pady=(3, 0))
    return box


def _footer(parent, *, ok_text, on_ok, on_cancel, left=None):
    """底部按钮条。``left`` 是个回调，用来往左边塞次级动作（重置之类）。"""
    _sep(parent, pady=(4, 12))
    row = tk.Frame(parent, bg=BG_ELEV)
    row.pack(fill="x")
    ok = RoundButton(row, ok_text, on_ok, kind="accent", height=32, pad_x=18)
    ok.pack(side="right")
    RoundButton(row, "取消", on_cancel, kind="solid", height=32,
                pad_x=16).pack(side="right", padx=(0, 8))
    if left is not None:
        left(row)
    return ok


def _wrap(parent):
    """对话框内容的统一外壳（内边距一致，四个框看起来才像一家人）。"""
    box = tk.Frame(parent, bg=BG_ELEV)
    box.pack(fill="both", expand=True, padx=20, pady=(16, 14))
    return box


# ==========================================================================
# 对话框外壳
# ==========================================================================
class BaseDialog(tk.Toplevel):
    """深色对话框：深色标题栏 + 居中 + 模态 + Esc 取消。

    内容由 ``build_*_body()`` 填进 ``self.body`` —— 那几个函数同时也被
    layout_test 单独渲染，所以它们**不依赖这个类**存在。
    """

    def __init__(self, parent, title, *, dx=96, dy=72, resizable=False):
        self.parent_window = parent.winfo_toplevel()
        # dx/dy：相对主窗口的偏移（同级对话框错开一点，不至于完全重叠）。
        # 必须存成属性 —— show() 用它们算窗口位置。**这里漏了整整一个版本**：
        # 从 v1.2.0 的大重写起 dx/dy 只进参数、没进 self，于是每一次 show()
        # 都在第 188 行抛 AttributeError。窗口版程序没有控制台，异常被 tk 静默
        # 吞掉，表现出来就是"点了没反应"——裁剪/改尺寸/转格式/调整/口令
        # 五个对话框全都点不开，而且一句提示都没有。
        # 之所以能潜伏这么久：show() 里有 wait_window（阻塞），测试没法直接调，
        # 于是所有测试都只构建不显示，正好绕开了唯一会用到 dx/dy 的那一行。
        self.dx = dx
        self.dy = dy
        super().__init__(self.parent_window)
        self.title(title)
        self.configure(bg=BG_ELEV)
        self.transient(self.parent_window)
        self.resizable(bool(resizable), bool(resizable))
        self.withdraw()      # 先藏：等布局定稿再显示，免得先闪个小方块再跳大
        self.bind("<Escape>", lambda _e: self.cancel())
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.body = tk.Frame(self, bg=BG_ELEV)
        self.body.pack(fill="both", expand=True)
        self.form = None

    # ----- 关闭 -----
    def cancel(self):
        self._safe_destroy()

    def apply_close(self):
        """表单确认之后调它。表单自己做完了事，这里只负责收窗口。"""
        self._safe_destroy()

    def _safe_destroy(self):
        try:
            if self.grab_current() is self:
                self.grab_release()
        except tk.TclError:
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass

    # ----- 显示 -----
    def show(self):
        """显示并阻塞，直到窗口关闭。"""
        self.present()
        self.wait_window(self)

    def present(self, *, modal: bool = True):
        """显示但**不阻塞** —— 给"要一边跑一边更新"的窗口用（进度条）。

        位置/夹取那段逻辑只有一份：``show()`` 就是 ``present()`` 之后再等它关闭。
        分成两个方法而不是复制一遍，是因为这段定位代码曾经因为漏存 dx/dy
        让五个对话框全打不开 —— 那种代码不能有两份。

        ``modal=False`` 时不抢 grab：进度条属于"后台在干活，界面还能用"的那种窗口，
        而且不抢 grab 也免得跟后面弹出来的 messagebox 互相盖。
        """
        self.update_idletasks()
        req_w = max(320, self.winfo_reqwidth())
        req_h = max(160, self.winfo_reqheight())
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(req_w, max(320, sw - 80)), min(req_h, max(160, sh - 140))
        x = min(max(0, self.parent_window.winfo_rootx() + self.dx), sw - w)
        y = min(max(24, self.parent_window.winfo_rooty() + self.dy), sh - h - 48)
        self.minsize(min(req_w, w), min(req_h, h))
        self.geometry("%dx%d+%d+%d" % (w, h, max(0, x), max(0, y)))
        self.deiconify()
        dark_titlebar(self)
        self.lift()
        if modal:
            try:
                self.grab_set()
            except tk.TclError:
                pass
        self.focus_force()


# ==========================================================================
# 一、修改尺寸
# ==========================================================================
class ResizeForm:
    """修改尺寸表单。

    两种输入模式（像素 / 百分比）共用一条出口：``apply()`` 一定先把结果算成
    一个确定的像素尺寸，再交给 ``on_px``；走百分比时额外调一次 ``on_pct``，
    好让状态栏能说"已缩放到 50%"，而不是干巴巴一句"已改尺寸"。
    """

    PRESETS = (25, 50, 75, 100, 150, 200)

    def __init__(self, parent, *, size, on_px, on_pct=None, close=None):
        self.w0, self.h0 = max(1, int(size[0])), max(1, int(size[1]))
        self.on_px = on_px
        self.on_pct = on_pct
        self.close = close or (lambda: parent.winfo_toplevel().destroy())
        self._sync = False
        self._ready = False

        root = _wrap(parent)
        _heading(root, "修改尺寸",
                 "原图 %d × %d 像素（%.1f MP）"
                 % (self.w0, self.h0, ops.megapixels(self.w0, self.h0)))
        _sep(root, pady=(12, 12))

        # ---- 模式 ----
        self.mode = Segmented(root, (("px", "按像素"), ("pct", "按百分比")),
                              value="px", command=lambda _k: self._on_mode(),
                              height=30, bg=BG_ELEV)
        self.mode.pack(anchor="w")

        # ---- 按像素 ----
        px_row = tk.Frame(root, bg=BG_ELEV)
        px_row.pack(anchor="w", pady=(12, 0))
        tk.Label(px_row, text="宽", bg=BG_ELEV, fg=FG_DIM,
                 font=font(10)).pack(side="left", padx=(0, 6))
        self.w_entry = RoundEntry(px_row, width=96,
                                  on_change=lambda: self._on_px_edit(from_w=True),
                                  on_enter=self.apply)
        self.w_entry.pack(side="left")
        tk.Label(px_row, text="×", bg=BG_ELEV, fg=FG_DIM,
                 font=font(10)).pack(side="left", padx=8)
        self.h_entry = RoundEntry(px_row, width=96,
                                  on_change=lambda: self._on_px_edit(from_w=False),
                                  on_enter=self.apply)
        self.h_entry.pack(side="left")
        tk.Label(px_row, text="像素", bg=BG_ELEV, fg=FG_DIM,
                 font=font(10)).pack(side="left", padx=(8, 0))

        self.lock = CheckRow(root, "锁定纵横比（改一边，另一边跟着算）",
                             value=True, bg=BG_ELEV,
                             command=lambda _v: self._on_lock())
        self.lock.pack(anchor="w", pady=(8, 0))

        # ---- 按百分比 ----
        pct_row = tk.Frame(root, bg=BG_ELEV)
        pct_row.pack(anchor="w", pady=(10, 0))
        tk.Label(pct_row, text="比例", bg=BG_ELEV, fg=FG_DIM,
                 font=font(10)).pack(side="left", padx=(0, 6))
        self.pct_entry = RoundEntry(pct_row, width=74, on_change=self._update,
                                    on_enter=self.apply)
        self.pct_entry.pack(side="left")
        tk.Label(pct_row, text="%（100 = 原大小）", bg=BG_ELEV, fg=FG_DIM,
                 font=font(9)).pack(side="left", padx=(8, 0))

        preset_row = tk.Frame(root, bg=BG_ELEV)
        preset_row.pack(anchor="w", pady=(8, 0))
        self.presets = Segmented(preset_row,
                                 [(str(p), "%d%%" % p) for p in self.PRESETS],
                                 value="100", command=self._on_preset,
                                 height=28, bg=BG_ELEV)
        self.presets.pack(side="left", padx=(46, 0))

        _sep(root, pady=(14, 10))

        # ---- 结果预览 ----
        self.result = tk.Label(root, text="", bg=BG_ELEV, fg=FG_OK,
                               font=font(10, "bold"), anchor="w", justify="left")
        self.result.pack(anchor="w")
        self.note = tk.Label(root, text="", bg=BG_ELEV, fg=FG_DIM, font=font(9),
                             anchor="w", justify="left", wraplength=430)
        self.note.pack(anchor="w", pady=(3, 0))

        self.buttons = _footer(root, ok_text="修改尺寸", on_ok=self.apply,
                               on_cancel=self.close,
                               left=lambda row: RoundButton(
                                   row, "恢复原尺寸", self._reset_all,
                                   kind="ghost", height=32,
                                   pad_x=10).pack(side="left"))

        self._ready = True
        self.w_entry.set(self.w0)
        self.h_entry.set(self.h0)
        self.pct_entry.set("100")
        self._on_mode()

    # ----- 交互 -----
    def _on_mode(self):
        if not self._ready:
            return
        px = self.mode.get() == "px"
        for entry in (self.w_entry, self.h_entry):
            entry.set_state("normal" if px else "disabled")
        self.pct_entry.set_state("disabled" if px else "normal")
        self.lock.set_enabled(px)
        self.presets.set_enabled(not px)
        self._update()

    def _on_lock(self):
        if self.lock.get() and self.mode.get() == "px":
            self._sync_px(from_w=True)
        else:
            self._update()

    def _on_px_edit(self, *, from_w: bool):
        if not self._ready:
            return
        if self.mode.get() != "px" or not self.lock.get():
            self._update()
            return
        # 从**被改的那一边**算另一边。总是从宽算高的话，用户手动改高
        # 会立刻被"从宽算出来的高"顶回去 —— 高框像被焊住了一样。
        self._sync_px(from_w=from_w)

    def _sync_px(self, *, from_w: bool):
        """按锁定的那一边算另一边。

        ``_sync`` 闸门是必须的：往 entry 写回 → 触发 trace → 又进本函数。
        没这道门就会"改一次宽、高被算两遍"，极端情况下数值越滚越大。
        """
        if self._sync:
            return
        self._sync = True
        try:
            if from_w:
                w = _clamp_int(self.w_entry.get(), self.w0, 1, MAX_SIDE)
                self.h_entry.set(max(1, int(round(w * self.h0 / float(self.w0)))))
            else:
                h = _clamp_int(self.h_entry.get(), self.h0, 1, MAX_SIDE)
                self.w_entry.set(max(1, int(round(h * self.w0 / float(self.h0)))))
        finally:
            self._sync = False
        self._update()

    def _on_preset(self, key):
        try:
            pct = int(str(key))
        except (TypeError, ValueError):
            return
        self.pct_entry.set(str(pct))
        self._update()

    def _reset_all(self):
        self.w_entry.set(self.w0)
        self.h_entry.set(self.h0)
        self.pct_entry.set("100")
        self.presets.set("100")
        self._update()

    # ----- 取值 -----
    def target(self):
        """算出"会得到多大"。返回 ``(mode, w, h, pct)``；像素模式下 pct 为 None。"""
        if self.mode.get() == "px":
            w = _clamp_int(self.w_entry.get(), self.w0, 1, MAX_SIDE)
            h = _clamp_int(self.h_entry.get(), self.h0, 1, MAX_SIDE)
            return "px", w, h, None
        pct = max(1.0, min(2000.0, _parse_float(self.pct_entry.get(), 100.0)))
        w = max(1, int(round(self.w0 * pct / 100.0)))
        h = max(1, int(round(self.h0 * pct / 100.0)))
        return "pct", w, h, pct

    def _update(self):
        if not self._ready:
            return
        mode, w, h, pct = self.target()
        txt = "将得到 %d × %d 像素" % (w, h)
        if pct is not None:
            txt += "（%.2f%%）" % pct
        self.result.configure(text=txt)
        notes = []
        if ops.megapixels(w, h) > BIG_RESULT_MP:
            notes.append("这张图会非常大（%.0f MP），处理时要等一会儿。" % ops.megapixels(w, h))
        if w > 10000 or h > 10000:
            notes.append("超过 10000 像素的图，很多老软件打不开。")
        if w > self.w0 and h > self.h0:
            notes.append("这是放大，画质不会因此变好。")
        self.note.configure(text=" ".join(notes), fg=FG_WARN if notes else FG_DIM)

    # ----- 应用 -----
    def apply(self):
        if not self._ready:
            return
        mode, w, h, pct = self.target()
        if mode == "pct" and callable(self.on_pct):
            self.on_pct(pct)
        else:
            self.on_px(w, h)
        self.close()


def build_resize_body(parent, *, size, on_px, on_pct=None, close=None):
    """供 ``layout_test`` 单独渲染，也被 ``ResizeDialog`` 用。"""
    return ResizeForm(parent, size=size, on_px=on_px, on_pct=on_pct, close=close)


class ResizeDialog(BaseDialog):
    def __init__(self, parent, *, size, on_px, on_pct=None):
        super().__init__(parent, "修改尺寸")
        self.form = build_resize_body(self.body, size=size, on_px=on_px,
                                     on_pct=on_pct, close=self.apply_close)


# ==========================================================================
# 二、转换格式
# ==========================================================================
class ConvertForm:
    """转换格式表单：格式 + 画质 + 保留 EXIF + 体积预估。"""

    #: 四列，七项 → 两行。短名 + 一行说明，比七句长文案排下来紧凑得多，
    #: 150% 缩放下也不会把对话框撑到屏幕外面去。
    COLS = 4

    def __init__(self, parent, *, image, fmt="JPEG", quality=None,
                 exif_bytes=None, source_bytes=0, dpi=None, on_ok=None,
                 close=None):
        self.image = image
        self.exif_bytes = exif_bytes
        self.has_exif = bool(exif_bytes)
        self.source_bytes = int(source_bytes or 0)
        self.dpi = dpi
        self.on_ok = on_ok
        self.close = close or (lambda: parent.winfo_toplevel().destroy())
        self._after = None
        self._ready = False

        root = _wrap(parent)
        self._timer = root
        _heading(root, "转换格式",
                 "选好之后会接着弹出保存对话框，由你决定存到哪里。")
        _sep(root, pady=(12, 12))

        fmt = fmt if fmt in ops.FORMATS else "JPEG"
        grid = tk.Frame(root, bg=BG_ELEV)
        grid.pack(anchor="w", fill="x")
        self.radios = {}
        for i, name in enumerate(ops.FORMATS):
            row = RadioRow(grid, ops.FMT_SHORT[name], value=(name == fmt),
                           bg=BG_ELEV,
                           command=lambda n=name: self.set_format(n))
            row.grid(row=i // self.COLS, column=i % self.COLS, sticky="w",
                     padx=(0, 18), pady=2)
            self.radios[name] = row

        self.desc = tk.Label(root, text="", bg=BG_ELEV, fg=FG_DIM, font=font(9),
                             anchor="w", justify="left", wraplength=470)
        self.desc.pack(anchor="w", pady=(8, 0))

        _sep(root, pady=(12, 8))

        self.quality = SliderRow(
            root, "画质", 30, 100,
            value=int(quality or ops.DEFAULT_QUALITY.get(fmt, 90)),
            command=lambda _v: self._schedule_estimate(), fmt="%d", suffix="",
            bg=BG_ELEV, label_px=44)
        self.quality.pack(fill="x")
        self.quality_note = tk.Label(root, text="", bg=BG_ELEV, fg=FG_MUTED,
                                     font=font(9), anchor="w", justify="left",
                                     wraplength=470)
        self.quality_note.pack(anchor="w", pady=(2, 0))

        self.keep_exif = CheckRow(root, "保留 EXIF 信息（相机型号、拍摄时间等）",
                                  value=self.has_exif, bg=BG_ELEV,
                                  command=lambda _v: self._schedule_estimate())
        self.keep_exif.pack(anchor="w", pady=(8, 0))

        self.estimate = tk.Label(root, text="", bg=BG_ELEV, fg=FG_DIM,
                                 font=font(9), anchor="w", justify="left")
        self.estimate.pack(anchor="w", pady=(10, 0))
        self.warn = tk.Label(root, text="", bg=BG_ELEV, fg=FG_WARN, font=font(9),
                             anchor="w", justify="left", wraplength=470)
        self.warn.pack(anchor="w", pady=(3, 0))

        self.buttons = _footer(root, ok_text="下一步：保存", on_ok=self.apply,
                               on_cancel=self.close)

        self._ready = True
        self.set_format(fmt, initial=True)

    # ----- 格式 -----
    def get_format(self) -> str:
        for name, row in self.radios.items():
            if row.get():
                return name
        return "JPEG"

    def set_format(self, name, *, initial=False):
        if not self._ready or name not in self.radios:
            return
        for key, row in self.radios.items():
            row.set(key == name)
        lossy = name in ops.LOSSY
        self.quality.set_enabled(lossy)
        self.quality_note.configure(
            text="" if lossy else "%s 是无损格式，没有画质可调。"
                 % ops.FMT_SHORT[name])
        if not initial:
            self.quality.set(ops.DEFAULT_QUALITY.get(name, 90))
        self.keep_exif.set_enabled(name in ops.EXIF_OK and self.has_exif)
        self._update_notes()
        self._schedule_estimate()

    def _update_notes(self):
        fmt = self.get_format()
        self.desc.configure(text=ops.FMT_LABEL[fmt])
        notes = []
        if ops.image_has_alpha(self.image) and fmt in ops.NEEDS_FLATTEN:
            notes.append("%s 没有透明通道，透明区域会被合成到白底上。"
                         % ops.FMT_SHORT[fmt])
        if fmt == "ICO":
            notes.append(ops.ico_warning(self.image.size)
                         if self.image is not None else "")
        if fmt == "GIF" and ops.image_has_alpha(self.image):
            notes.append("GIF 的半透明只有「透明 / 不透明」两档，"
                         "边缘的半透明像素会变硬。")
        if fmt not in ops.EXIF_OK and self.has_exif:
            notes.append("%s 存不下 EXIF，相机信息会丢。" % ops.FMT_SHORT[fmt])
        self.warn.configure(text=" ".join(n for n in notes if n))

    # ----- 体积预估 -----
    def _schedule_estimate(self):
        if not self._ready:
            return
        if self._after is not None:
            try:
                self._timer.after_cancel(self._after)
            except (tk.TclError, ValueError):
                pass
        self._after = self._timer.after(ESTIMATE_DEBOUNCE_MS, self._estimate)

    def _estimate(self):
        """在内存里编码一遍量体积。

        必须防抖：每次拖滑块都编码一张全尺寸图，窗口会明显一顿一顿的。
        """
        self._after = None
        if self.image is None:
            return 0
        try:
            if not self.estimate.winfo_exists():
                return 0
        except tk.TclError:
            return 0
        fmt = self.get_format()
        n = ops.estimate_saved_size(self.image, fmt,
                                    quality=int(self.quality.get()),
                                    dpi=self.dpi,
                                    exif=self._exif_to_write(fmt))
        try:
            if not self.estimate.winfo_exists():
                return n
        except tk.TclError:
            return n
        if n <= 0:
            self.estimate.configure(text="体积估算失败（不影响保存）")
            return 0
        text = "预计约 %s" % ops.human_size(n)
        if self.source_bytes:
            delta = n - self.source_bytes
            text += "（原文件 %s，%s %.0f%%）" % (
                ops.human_size(self.source_bytes), "↑" if delta > 0 else "↓",
                abs(delta) / float(self.source_bytes) * 100.0)
        self.estimate.configure(text=text)
        return n

    def _exif_to_write(self, fmt):
        if fmt in ops.EXIF_OK and self.keep_exif.get() and self.exif_bytes:
            return self.exif_bytes
        return None

    # ----- 应用 -----
    def apply(self):
        if not self._ready:
            return
        fmt = self.get_format()
        keep = bool(self.keep_exif.get() and fmt in ops.EXIF_OK)
        if callable(self.on_ok):
            self.on_ok(fmt, int(self.quality.get()), keep)
        self.close()


def build_convert_body(parent, *, image, fmt="JPEG", quality=None,
                       exif_bytes=None, source_bytes=0, dpi=None,
                       on_ok=None, close=None):
    """供 ``layout_test`` 单独渲染，也被 ``ConvertDialog`` 用。"""
    return ConvertForm(parent, image=image, fmt=fmt, quality=quality,
                       exif_bytes=exif_bytes, source_bytes=source_bytes,
                       dpi=dpi, on_ok=on_ok, close=close)


class ConvertDialog(BaseDialog):
    def __init__(self, parent, *, image, fmt, quality, exif_bytes=None,
                 source_bytes=0, dpi=None, on_ok=None):
        super().__init__(parent, "转换格式")
        self.form = build_convert_body(self.body, image=image, fmt=fmt,
                                      quality=quality, exif_bytes=exif_bytes,
                                      source_bytes=source_bytes, dpi=dpi,
                                      on_ok=on_ok, close=self.apply_close)


# ==========================================================================
# 三、裁剪
# ==========================================================================
#: 裁剪框的长宽比预设。``orig`` 表示"跟原图一样"，实际会取整张图。
ASPECTS = (("free", "自由"), ("orig", "原图"), ("1:1", "1:1"), ("4:3", "4:3"),
           ("3:2", "3:2"), ("16:9", "16:9"), ("9:16", "9:16"))


def _ratio_of(kind, iw: int, ih: int):
    """``"4:3"`` → 1.333…；``free`` / 解析不出来 → ``None``。"""
    if not kind or kind == "free":
        return None
    if kind == "orig":
        return (iw / float(ih)) if ih else None
    a, _colon, b = str(kind).partition(":")
    try:
        return float(a) / float(b)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


class CropCanvas(tk.Canvas):
    """裁剪框画布：图上拖一个框，八个手柄调整尺寸。

    **两套坐标系，混了就会"框跑偏"**：
      * ``_rect`` 是**图片坐标**（最终交给 ``ops.crop`` 的就是它）；
      * 画布坐标只是画给人看的。
    两者靠 ``_scale``（缩放）与 ``_ox/_oy``（居中偏移）互转。
    """

    HANDLE = 9          # 手柄边长（画布像素）
    MIN_SIDE = 8        # 裁剪框最小边（图片像素）
    PAD = 12

    def __init__(self, parent, *, width=580, height=320, bg=BG_VIEW,
                 on_change=None):
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0)
        self._on_change = on_change
        self._img = None
        self._photo = None
        self._render_key = None
        self._rect = [0.0, 0.0, 1.0, 1.0]
        self._aspect = "free"
        self._mode = None
        self._start = (0.0, 0.0)
        self._grab = None
        self._rect0 = None
        self._scale = 1.0
        self._ox = self._oy = 0.0
        self.bind("<Configure>", lambda _e: self._recompute())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)

    # ----- 内容 -----
    def set_image(self, img):
        self._img = img
        self._photo = None
        self._render_key = None
        self._recompute(full=True)
        self._notify()

    def _iw(self):
        return int(self._img.width) if self._img is not None else 1

    def _ih(self):
        return int(self._img.height) if self._img is not None else 1

    @property
    def aspect(self) -> str:
        return self._aspect

    def set_aspect(self, kind):
        self._aspect = kind
        self._fit_aspect_into_image()
        self._draw()
        self._notify()

    def reset(self):
        self._aspect = "free"
        self._rect = [0.0, 0.0, float(self._iw()), float(self._ih())]
        self._draw()
        self._notify()

    def crop_box(self):
        """给 ``ops.crop`` 用的整数框（已夹进图片范围）。"""
        if self._img is None:
            return None
        return ops.clamp_box(self._rect, self._iw(), self._ih())

    # ----- 坐标换算 -----
    def _to_canvas(self, x, y):
        return (self._ox + x * self._scale, self._oy + y * self._scale)

    def _to_image(self, cx, cy):
        s = self._scale or 1.0
        return ((cx - self._ox) / s, (cy - self._oy) / s)

    def _rect_canvas(self):
        x0, y0 = self._to_canvas(self._rect[0], self._rect[1])
        x1, y1 = self._to_canvas(self._rect[2], self._rect[3])
        return (x0, y0, x1, y1)

    def _recompute(self, full=False):
        cw = max(1, self.winfo_width())
        ch = max(1, self.winfo_height())
        if self._img is None:
            self._scale, self._photo, self._render_key = 1.0, None, None
            self._draw()
            return
        iw, ih = self._iw(), self._ih()
        bw = max(8, cw - self.PAD * 2)
        bh = max(8, ch - self.PAD * 2)
        # 不夹在 1.0 以下：小图要放大才好下手拖手柄
        self._scale = max(1e-4, min(bw / float(iw), bh / float(ih)))
        self._ox = (cw - iw * self._scale) / 2.0
        self._oy = (ch - ih * self._scale) / 2.0
        self._render_key = None
        if full:
            self._rect = [0.0, 0.0, float(iw), float(ih)]
            self._fit_aspect_into_image()
        self._draw()

    # ----- 几何 -----
    def _clamp(self, l, t, r, b):
        iw, ih = float(self._iw()), float(self._ih())
        if l > r:
            l, r = r, l
        if t > b:
            t, b = b, t
        ms = float(self.MIN_SIDE)
        l = max(0.0, min(l, iw - ms))
        t = max(0.0, min(t, ih - ms))
        r = max(l + ms, min(r, iw))
        b = max(t + ms, min(b, ih))
        return [l, t, r, b]

    def _fit_aspect(self, ax, ay, ix, iy, rat):
        """从不动点 ``(ax, ay)`` 朝 ``(ix, iy)`` 张出一个符合比例的框。

        放不下就整体缩小 —— 而不是让它顶出图片外面再被裁掉（那样手感是
        "框黏在边上不动"，很怪）。
        """
        sx = 1.0 if ix >= ax else -1.0
        sy = 1.0 if iy >= ay else -1.0
        w = abs(ix - ax)
        h = abs(iy - ay)
        if rat:
            if w >= h * rat:
                h = w / rat
            else:
                w = h * rat
        avail_w = (self._iw() - ax) if sx > 0 else ax
        avail_h = (self._ih() - ay) if sy > 0 else ay
        if rat:
            k = 1.0
            if w > 0:
                k = min(k, max(0.0, avail_w) / w)
            if h > 0:
                k = min(k, max(0.0, avail_h) / h)
            w *= k
            h *= k
        else:
            w = min(w, max(0.0, avail_w))
            h = min(h, max(0.0, avail_h))
        w = max(w, float(self.MIN_SIDE))
        h = max(h, float(self.MIN_SIDE))
        l, r = (ax, ax + w) if sx > 0 else (ax - w, ax)
        t, b = (ay, ay + h) if sy > 0 else (ay - h, ay)
        return self._clamp(l, t, r, b)

    def _fit_aspect_into_image(self):
        """收敛到"与图片内切、保持比例、以原框中心为中心"。"""
        rat = _ratio_of(self._aspect, self._iw(), self._ih())
        if not rat:
            self._rect = self._clamp(*self._rect)
            return
        iw, ih = float(self._iw()), float(self._ih())
        l, t, r, b = self._rect
        cx, cy = (l + r) / 2.0, (t + b) / 2.0
        w = min(iw, ih * rat)
        h = w / rat
        if h > ih:
            h = ih
            w = h * rat
        l = min(max(0.0, cx - w / 2.0), iw - w)
        t = min(max(0.0, cy - h / 2.0), ih - h)
        self._rect = [l, t, l + w, t + h]

    def _resize_from(self, handle, rect0, ix, iy):
        l, t, r, b = rect0
        if "w" in handle:
            l = ix
        if "e" in handle:
            r = ix
        if "n" in handle:
            t = iy
        if "s" in handle:
            b = iy
        rat = _ratio_of(self._aspect, self._iw(), self._ih())
        if not rat:
            return self._clamp(l, t, r, b)
        if handle in ("n", "s"):
            # 上下边：左右两根竖线保持对称（中心不动）
            cx = (l + r) / 2.0
            h = max(1.0, abs(b - t))
            w = h * rat
            return self._clamp(cx - w / 2.0, min(t, b), cx + w / 2.0, max(t, b))
        if handle in ("e", "w"):
            cy = (t + b) / 2.0
            w = max(1.0, abs(r - l))
            h = w / rat
            return self._clamp(min(l, r), cy - h / 2.0, max(l, r), cy + h / 2.0)
        # 角：**对角**那个角是不动点
        fixed = {"nw": (r, b), "se": (l, t), "ne": (l, b), "sw": (r, t)}[handle]
        return self._fit_aspect(fixed[0], fixed[1], ix, iy, rat)

    # ----- 交互 -----
    def _hit(self, x, y):
        x0, y0, x1, y1 = self._rect_canvas()
        if x1 - x0 < 26 or y1 - y0 < 26:
            return None                 # 框太小，八个手柄会叠在一起
        tol = self.HANDLE / 2.0 + 2.0
        mx, my = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        spots = (("nw", x0, y0), ("n", mx, y0), ("ne", x1, y0),
                 ("w", x0, my), ("e", x1, my),
                 ("sw", x0, y1), ("s", mx, y1), ("se", x1, y1))
        for name, px, py in spots:
            if abs(x - px) <= tol and abs(y - py) <= tol:
                return name
        return None

    def _press(self, event):
        if self._img is None:
            return
        self.focus_set()
        ix, iy = self._to_image(event.x, event.y)
        self._start = (ix, iy)
        self._rect0 = list(self._rect)
        hit = self._hit(event.x, event.y)
        l, t, r, b = self._rect
        if hit:
            self._mode = hit
        elif l <= ix <= r and t <= iy <= b:
            self._mode = "move"
            self._grab = (event.x, event.y, list(self._rect))
        else:
            self._mode = "new"
            self._rect = [ix, iy, ix, iy]
        self.configure(cursor="crosshair" if self._mode == "new" else "fleur")
        self._draw()

    def _drag(self, event):
        if self._mode is None or self._img is None:
            return
        ix, iy = self._to_image(event.x, event.y)
        if self._mode == "move":
            x0, y0, r0 = self._grab
            dx = (event.x - x0) / (self._scale or 1.0)
            dy = (event.y - y0) / (self._scale or 1.0)
            l, t, r, b = r0
            w, h = r - l, b - t
            # 整框平移：撞到边就贴住，尺寸不变（真裁剪工具都是这个手感）
            l = min(max(0.0, l + dx), max(0.0, self._iw() - w))
            t = min(max(0.0, t + dy), max(0.0, self._ih() - h))
            self._rect = [l, t, l + w, t + h]
        elif self._mode == "new":
            self._rect = self._fit_aspect(
                self._start[0], self._start[1], ix, iy,
                _ratio_of(self._aspect, self._iw(), self._ih()))
        else:
            self._rect = self._resize_from(self._mode, self._rect0, ix, iy)
        self._draw()
        self._notify()

    def _release(self, _event=None):
        if self._mode == "new" and self._img is not None:
            l, t, r, b = self._rect
            if (r - l) < self.MIN_SIDE or (b - t) < self.MIN_SIDE:
                # 只是点了一下没拖 —— 当作取消这次框选，回到原来的框
                self._rect = list(self._rect0 or
                                  [0.0, 0.0, float(self._iw()), float(self._ih())])
                self._draw()
                self._notify()
        self._mode = None
        self._grab = None
        self.configure(cursor="")

    # ----- 绘制 -----
    def _ensure_photo(self):
        if self._img is None or ImageTk is None:
            return
        w = max(1, int(round(self._iw() * self._scale)))
        h = max(1, int(round(self._ih() * self._scale)))
        key = (id(self._img), w, h)
        if key == self._render_key and self._photo is not None:
            return
        try:
            self._photo = ImageTk.PhotoImage(
                self._img if (w, h) == self._img.size
                else self._img.resize((w, h), ops.resample()))
            self._render_key = key
        except (MemoryError, ValueError, tk.TclError) as exc:
            log.warning("裁剪预览渲染失败：%s", exc)
            self._photo = None

    def _draw(self):
        self.delete("all")
        cw, ch = max(1, self.winfo_width()), max(1, self.winfo_height())
        if self._img is None:
            self.create_text(cw / 2, ch / 2, text="没有图片", fill=FG_DIM,
                             font=font(10))
            return
        self._ensure_photo()
        if self._photo is not None:
            self.create_image(self._ox, self._oy, anchor="nw", image=self._photo)

        x0, y0, x1, y1 = self._rect_canvas()
        # 框外压暗。Tk 图元没有 alpha，只能用 stipple 网点近似半透明；
        # 万一这个平台不支持就干脆不画 —— 宁可没有压暗，也不能把图糊死。
        for (a, b, c, d) in ((0, 0, cw, y0), (0, y1, cw, ch),
                             (0, y0, x0, y1), (x1, y0, cw, y1)):
            if c - a < 1 or d - b < 1:
                continue
            try:
                self.create_rectangle(a, b, c, d, fill="#000000", outline="",
                                      stipple="gray75")
            except tk.TclError:
                pass

        # 三分线（构图辅助）
        for i in (1, 2):
            gx = x0 + (x1 - x0) * i / 3.0
            gy = y0 + (y1 - y0) * i / 3.0
            self.create_line(gx, y0, gx, y1, fill="#ffffff", dash=(2, 4), width=1)
            self.create_line(x0, gy, x1, gy, fill="#ffffff", dash=(2, 4), width=1)

        self.create_rectangle(x0, y0, x1, y1, outline="#ffffff", width=2)

        if x1 - x0 >= 26 and y1 - y0 >= 26:
            hs = self.HANDLE
            mx, my = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            for px, py in ((x0, y0), (mx, y0), (x1, y0), (x0, my), (x1, my),
                           (x0, y1), (mx, y1), (x1, y1)):
                round_rect(self, px - hs / 2.0, py - hs / 2.0,
                           px + hs / 2.0, py + hs / 2.0, 2,
                           fill="#ffffff", outline="#1b1d22", width=1)

    def _notify(self):
        if callable(self._on_change):
            box = self.crop_box()
            if box:
                self._on_change(box)


class CropForm:
    """裁剪表单：预览 + 比例预设 + 结果尺寸。"""

    def __init__(self, parent, *, image, on_ok=None, close=None):
        self.image = image
        self.on_ok = on_ok
        self.close = close or (lambda: parent.winfo_toplevel().destroy())

        root = _wrap(parent)
        _heading(root, "裁剪",
                 "拖动白色边框调整要保留的区域；在框外拖可以重新框一块。")
        _sep(root, pady=(12, 10))

        self.canvas = CropCanvas(root, width=580, height=320,
                                 on_change=self._on_box)
        self.canvas.pack(fill="both", expand=True)

        row = tk.Frame(root, bg=BG_ELEV)
        row.pack(fill="x", pady=(10, 0))
        tk.Label(row, text="比例", bg=BG_ELEV, fg=FG_DIM,
                 font=font(9)).pack(side="left", padx=(0, 8))
        self.aspects = Segmented(row, ASPECTS, value="free",
                                 command=self._on_aspect, height=28, bg=BG_ELEV)
        self.aspects.pack(side="left")

        self.info = tk.Label(root, text="", bg=BG_ELEV, fg=FG_OK,
                             font=font(10, "bold"), anchor="w")
        self.info.pack(anchor="w", pady=(10, 0))
        self.note = tk.Label(root, text="", bg=BG_ELEV, fg=FG_DIM, font=font(9),
                             anchor="w", justify="left", wraplength=520)
        self.note.pack(anchor="w", pady=(2, 0))

        self.buttons = _footer(root, ok_text="裁剪", on_ok=self.apply,
                               on_cancel=self.close,
                               left=lambda r: RoundButton(
                                   r, "重置", self._reset, kind="ghost",
                                   height=32, pad_x=10).pack(side="left"))

        if image is not None:
            self.canvas.set_image(image)

    # ----- 事件 -----
    def _on_aspect(self, key):
        self.canvas.set_aspect(key)

    def _on_box(self, box):
        w = max(0, int(box[2] - box[0]))
        h = max(0, int(box[3] - box[1]))
        self.info.configure(text="裁剪后 %d × %d 像素" % (w, h))
        iw, ih = self.image.size if self.image is not None else (1, 1)
        ratio = (w * h) / float(max(1, iw * ih)) * 100.0
        small = w < 32 or h < 32
        self.note.configure(
            text="占原图面积的 %.0f%%" % ratio
                 + ("　再小下去就不是一张能看的图了。" if small else ""),
            fg=FG_WARN if small else FG_DIM)

    def _reset(self):
        self.canvas.reset()
        self.aspects.set("free")

    def apply(self):
        box = self.canvas.crop_box()
        if box and callable(self.on_ok):
            self.on_ok(box)
        self.close()


def build_crop_body(parent, *, image, on_ok=None, close=None):
    return CropForm(parent, image=image, on_ok=on_ok, close=close)


class CropDialog(BaseDialog):
    def __init__(self, parent, *, image, on_ok=None):
        super().__init__(parent, "裁剪", dy=60, resizable=True)
        self.form = build_crop_body(self.body, image=image, on_ok=on_ok,
                                    close=self.apply_close)


# ==========================================================================
# 四、调整
# ==========================================================================
#: 一键色调滤镜。互斥（选一个就顶掉上一个），再点一次取消。
TONES = (("gray", "灰度"), ("sepia", "复古"), ("invert", "反色"))


class AdjustForm:
    """亮度 / 对比度 / 饱和度 / 锐度 + 几个一键滤镜。

    预览和最终的差别只有一个字：**准**。
    预览用缩到 ~1.4MP 的底图（滑块要跟手），点「应用」时拿**全分辨率**
    底图重跑同一套运算。两边算式完全一样，所以预览所见即所得。
    """

    def __init__(self, parent, *, image, on_ok=None, close=None):
        self.image = image
        self.on_ok = on_ok
        self.close = close or (lambda: parent.winfo_toplevel().destroy())
        self._tone = None
        self._auto = False
        self._after = None
        self._ready = False
        self._base = self._preview_base(image)

        root = _wrap(parent)
        self._timer = root
        _heading(root, "调整", "拖动滑块实时预览；满意了再点「应用」。")
        _sep(root, pady=(12, 10))

        holder = tk.Frame(root, bg=BG_VIEW, height=250)
        holder.pack(fill="x")
        holder.pack_propagate(False)
        self.viewer = None
        if ImageTk is not None:
            self.viewer = image_view.ImageViewer(holder, bg=BG_VIEW)
            self.viewer.pack(fill="both", expand=True)

        self.sliders = {}
        for key, label in (("brightness", "亮度"), ("contrast", "对比度"),
                           ("color", "饱和度"), ("sharpness", "锐度")):
            row = SliderRow(root, label, 0, 200, value=100,
                            command=self._on_slider, fmt="%d", suffix="%",
                            reset_value=100, bg=BG_ELEV, label_px=48)
            row.pack(fill="x", pady=(6 if key == "brightness" else 2, 0))
            self.sliders[key] = row

        _sep(root, pady=(12, 8))

        presets = tk.Frame(root, bg=BG_ELEV)
        presets.pack(fill="x")
        tk.Label(presets, text="滤镜", bg=BG_ELEV, fg=FG_DIM,
                 font=font(9)).pack(side="left", padx=(0, 8))
        self.auto_btn = RoundButton(presets, "自动对比度", self._toggle_auto,
                                   kind="solid", height=28, pad_x=10)
        self.auto_btn.pack(side="left", padx=(0, 6))
        self.tone_btns = {}
        for key, label in TONES:
            btn = RoundButton(presets, label, lambda k=key: self._toggle_tone(k),
                              kind="solid", height=28, pad_x=10)
            btn.pack(side="left", padx=(0, 6))
            self.tone_btns[key] = btn

        self.buttons = _footer(root, ok_text="应用", on_ok=self.apply,
                               on_cancel=self.close,
                               left=lambda r: RoundButton(
                                   r, "重置全部", self._reset_all, kind="ghost",
                                   height=32, pad_x=10).pack(side="left"))

        self._ready = True
        self._render_preview()

    # ----- 预览管线 -----
    @staticmethod
    def _preview_base(img):
        if img is None:
            return None
        w, h = img.size
        if w * h <= PREVIEW_PIXELS:
            return img
        k = (PREVIEW_PIXELS / float(w * h)) ** 0.5
        return ops.resize_px(img, max(1, int(w * k)), max(1, int(h * k)))

    def _params(self):
        return {k: row.get() / 100.0 for k, row in self.sliders.items()}

    def process(self, img):
        """把当前这套设置套到任意一张图上。预览与最终共用同一条管线。"""
        if img is None:
            return None
        out = img
        if self._auto:
            out = ops.auto_contrast(out)
        if self._tone == "gray":
            out = ops.grayscale(out)
        elif self._tone == "sepia":
            out = ops.sepia(out)
        elif self._tone == "invert":
            out = ops.invert(out)
        return ops.adjust(out, **self._params())

    def result_image(self):
        """在**全分辨率**原图上重算一遍 —— 这才是要写回去的那张。"""
        return self.process(self.image)

    def _on_slider(self, _v):
        self._schedule()

    def _schedule(self):
        if not self._ready:
            return
        if self._after is not None:
            try:
                self._timer.after_cancel(self._after)
            except (tk.TclError, ValueError):
                pass
        self._after = self._timer.after(SLIDER_DEBOUNCE_MS, self._render_preview)

    def _render_preview(self):
        self._after = None
        if self.viewer is None or self._base is None:
            return
        try:
            if not self.viewer.winfo_exists():
                return
            self.viewer.set_image(self.process(self._base), keep_view=True)
        except tk.TclError:
            return

    # ----- 滤镜 -----
    def _toggle_auto(self):
        self._auto = not self._auto
        self.auto_btn.set_kind("accent" if self._auto else "solid")
        self._schedule()

    def _toggle_tone(self, key):
        self._tone = None if self._tone == key else key
        for k, btn in self.tone_btns.items():
            btn.set_kind("accent" if k == self._tone else "solid")
        self._schedule()

    def _reset_all(self):
        self._auto = False
        self.auto_btn.set_kind("solid")
        self._tone = None
        for btn in self.tone_btns.values():
            btn.set_kind("solid")
        for row in self.sliders.values():
            row.set(100)
        self._schedule()

    def is_identity(self) -> bool:
        """一项都没动过（调用方可以据此跳过写回）。"""
        return (not self._auto and self._tone is None
                and all(abs(v - 1.0) < 1e-6 for v in self._params().values()))

    # ----- 应用 -----
    def apply(self):
        if not self._ready:
            return
        if callable(self.on_ok):
            self.on_ok(self.result_image(), self.is_identity())
        self.close()


# ==========================================================================
# 五、下载 / 校验进度（更新时用）
# ==========================================================================
class ProgressForm:
    """一条进度 + 一行结果。

    为什么单独做（而不是再弹一个 messagebox）
    ------------------------------------------------------------------
    * 34.5MB 要下二十秒左右，只给一句"正在下载"用户不知道是不是卡住了；
    * **"下完了"和"下完了并且 sha256 对得上"是两件事** —— 校验结果必须看得见。
      360 那种更新进度条之所以让人放心，就是因为它把两件事都摆出来了；
      我们这里同样：进度条 + 已下/总量 + 最后一行"✓ 校验通过"。
    * 它**不阻塞**（见 ``ProgressDialog``）：调用方一边跑后台线程一边喂
      :meth:`set_progress`，所以不用把界面卡在 wait_window 里。
    """

    def __init__(self, parent, *, title="正在更新", subtitle="", on_close=None):
        self.on_close = on_close or (
            lambda: parent.winfo_toplevel().destroy())
        self._ratio = 0.0
        self._close_btn = None

        root = _wrap(parent)
        _heading(root, title, subtitle)

        self.bar = tk.Canvas(root, height=16, bg=BG_SUNKEN,
                             highlightthickness=0)
        self.bar.pack(fill="x", pady=(14, 0))
        # 首帧宽度还是 1，不重画的话进度条一开始看不出来（安装页踩过同样的坑）
        self.bar.bind("<Configure>", lambda _e: self._redraw())

        self.line = tk.Label(root, text="准备下载…", bg=BG_ELEV, fg=FG_DIM,
                             font=font(9), anchor="w")
        self.line.pack(fill="x", pady=(8, 0))
        self.result = tk.Label(root, text=" ", bg=BG_ELEV, fg=FG_DIM,
                               font=font(10, "bold"), anchor="w",
                               justify="left")
        self.result.pack(fill="x", pady=(6, 0))

        self.footer = tk.Frame(root, bg=BG_ELEV)
        self.footer.pack(fill="x", pady=(10, 0))

    # ----- 进度 -----
    def _redraw(self) -> None:
        width = self.bar.winfo_width()
        self.bar.delete("all")
        if width > 1:
            self.bar.create_rectangle(0, 0, int(width * self._ratio), 16,
                                      fill=ACCENT, outline="")

    def set_progress(self, done: int, total: int, phase: str = "download") -> None:
        total = max(int(total or 0), 1)
        done = max(0, min(int(done or 0), total))
        if phase == "verify":
            self._ratio = 1.0
            self._redraw()
            self.line.configure(
                text="下载完成（%s），正在校验 sha256…" % ops.human_size(total))
            return
        self._ratio = float(done) / total
        self._redraw()
        self.line.configure(text="已下载 %s / %s（%d%%）" % (
            ops.human_size(done), ops.human_size(total), int(self._ratio * 100)))

    def set_note(self, text: str) -> None:
        """还没开始跑（比如"正在取更新清单…"）时先写一句，让用户知道在动。"""
        self.line.configure(text=text)

    def set_result(self, ok: bool, text: str) -> None:
        """把**校验结果**摆出来，并放出「关闭」按钮。"""
        self.result.configure(text=("✓ " if ok else "✗ ") + text,
                              fg=FG_OK if ok else FG_BAD)
        if self._close_btn is None:
            self._close_btn = RoundButton(self.footer, "关闭", self.on_close,
                                          kind="solid", height=32, pad_x=16)
            self._close_btn.pack(side="right")


def build_progress_body(parent, *, title="正在更新", subtitle="", on_close=None):
    return ProgressForm(parent, title=title, subtitle=subtitle,
                        on_close=on_close)


class ProgressDialog(BaseDialog):
    """进度窗口：**显示但不阻塞**（``present()`` 而不是 ``show()``）。

    它只负责"显示"；下载跑在后台线程里，由 ``main.py`` 把进度喂进来。
    用户中途按 Esc / 关掉它也不影响下载 —— ``main.py`` 那边会先看窗口还在不在。
    """

    def __init__(self, parent, *, title="正在更新", subtitle="", on_close=None):
        super().__init__(parent, title, dy=40)
        # on_close 一定要显式给：ProgressForm 的默认行为是销毁**顶层窗口**，
        # 而这里 parent 的顶层就是主窗口 —— 不指定的话点「关闭」会把主窗口关掉。
        self.form = build_progress_body(self.body, title=title,
                                        subtitle=subtitle,
                                        on_close=on_close or self.cancel)
        self.protocol("WM_DELETE_WINDOW", self.cancel)


def build_adjust_body(parent, *, image, on_ok=None, close=None):
    return AdjustForm(parent, image=image, on_ok=on_ok, close=close)


class AdjustDialog(BaseDialog):
    def __init__(self, parent, *, image, on_ok=None):
        super().__init__(parent, "调整", dy=50, resizable=True)
        self.form = build_adjust_body(self.body, image=image, on_ok=on_ok,
                                      close=self.apply_close)


# ==========================================================================
# 六、叠加图片（把另一张图贴到当前图上）
# ==========================================================================
#: 预览画布的最大尺寸。底图会按它缩小着看，但**坐标只存底图坐标**，
#: 所以"所见即所得"是靠换算保证的，不是靠运气。
PREVIEW_MAX = (700, 400)


class OverlayCanvas(tk.Canvas):
    """叠加预览：拖动移动、滚轮缩放。

    位置存在表单里（而且是**底图坐标**），这个画布只负责"画"和"把鼠标动作
    翻译成底图坐标上的位移"。
    """

    def __init__(self, parent, form, **kw):
        super().__init__(parent, highlightthickness=0, bd=0, **kw)
        self.form = form
        self._photo = None          # 必须留着引用，否则会被 GC 掉变成空白
        self._img_id = None
        self._drag = None
        self.bind("<Configure>", lambda _e: self.render())
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<MouseWheel>", self._wheel)

    def _press(self, event):
        self._drag = (event.x, event.y)
        self.configure(cursor="fleur")

    def _release(self, _event):
        self._drag = None

    def _motion(self, event):
        if self._drag is None or self.form.patch is None:
            return
        k = self.form.view_scale() or 1.0
        dx = (event.x - self._drag[0]) / k
        dy = (event.y - self._drag[1]) / k
        self._drag = (event.x, event.y)
        self.form.move_by(dx, dy)          # 表单改坐标并回调刷新

    def _wheel(self, event):
        if self.form.patch is None:
            return
        self.form.bump_scale(5 if event.delta > 0 else -5)

    def render(self):
        img = self.form.preview_image()
        if img is None or ImageTk is None:
            self.delete("all")
            self._photo = None
            self._img_id = None
            return
        self._photo = ImageTk.PhotoImage(img)
        cx, cy = self.winfo_width() // 2, self.winfo_height() // 2
        if self._img_id is None:
            self._img_id = self.create_image(cx, cy, anchor="center",
                                             image=self._photo)
        else:
            self.itemconfigure(self._img_id, image=self._photo)
            self.coords(self._img_id, cx, cy)


class OverlayForm:
    """把另一张图叠到当前图上。

    两个"素材从哪来"的动作（选文件 / 取剪贴板）**由调用方提供回调** ——
    这个模块只做界面，不碰文件对话框和系统剪贴板，和别的表单保持一致。
    """

    def __init__(self, parent, *, base, patch=None, name="", on_pick=None,
                 on_clipboard=None, on_ok=None, close=None):
        self.base = base
        self.patch = patch
        self.patch_name = name
        self.on_pick = on_pick
        self.on_clipboard = on_clipboard
        self.on_ok = on_ok
        self.close = close or (lambda: parent.winfo_toplevel().destroy())

        self.pos = [0.0, 0.0]
        self.scale_pct = 100.0
        self.opacity = 100.0
        self.feather = 0.0
        self.mode = "normal"

        root = _wrap(parent)
        _heading(root, "叠加图片",
                 "把另一张图贴到当前图上：在预览里拖动定位，滚轮缩放。")

        row = tk.Frame(root, bg=BG_ELEV)
        row.pack(fill="x", pady=(10, 6))
        RoundButton(row, "选择图片…", self._pick, kind="solid", height=30,
                    pad_x=14).pack(side="left")
        if on_clipboard is not None:
            RoundButton(row, "用剪贴板", self._clip, kind="solid", height=30,
                        pad_x=14).pack(side="left", padx=(8, 0))
        self.info = tk.Label(row, text="", bg=BG_ELEV, fg=FG_DIM, font=font(9),
                             anchor="w", justify="left")
        self.info.pack(side="left", padx=(12, 0))

        self.canvas = OverlayCanvas(
            root, self, width=PREVIEW_MAX[0], height=PREVIEW_MAX[1],
            bg="#15171c", cursor="arrow")
        self.canvas.pack(pady=(4, 10))

        self.rows = {}
        self.rows["scale"] = SliderRow(root, "缩放", 5, 400, value=100,
                                       command=self._on_scale, suffix="%",
                                       reset_value=100, width=220)
        self.rows["scale"].pack(fill="x", pady=2)
        self.rows["opacity"] = SliderRow(root, "不透明度", 0, 100, value=100,
                                         command=self._on_opacity, suffix="%",
                                         reset_value=100, width=220)
        self.rows["opacity"].pack(fill="x", pady=2)
        self.rows["feather"] = SliderRow(root, "边缘羽化", 0, 60, value=0,
                                         command=self._on_feather, suffix=" px",
                                         reset_value=0, width=220)
        self.rows["feather"].pack(fill="x", pady=2)

        mode_row = tk.Frame(root, bg=BG_ELEV)
        mode_row.pack(fill="x", pady=(8, 0))
        tk.Label(mode_row, text="叠加方式", bg=BG_ELEV, fg=FG_TEXT,
                 font=font(9)).pack(side="left", padx=(0, 8))
        self.mode_seg = Segmented(mode_row, ops.BLEND_MODES, value="normal",
                                  command=self._on_mode)
        self.mode_seg.pack(side="left")

        _sep(root, pady=(12, 10))
        footer = tk.Frame(root, bg=BG_ELEV)
        footer.pack(fill="x")
        self.ok_btn = RoundButton(footer, "应用", self.apply, kind="accent",
                                  height=32, pad_x=18)
        self.ok_btn.pack(side="right")
        RoundButton(footer, "取消", self.close, kind="solid", height=32,
                    pad_x=16).pack(side="right", padx=(0, 8))

        if self.patch is not None:
            self._recenter()
        self._refresh_info()
        self.after_idle()

    # ---------- 素材 ----------
    def _pick(self):
        if not callable(self.on_pick):
            return
        got = self.on_pick()
        if got:
            self.set_patch(*got)

    def _clip(self):
        if not callable(self.on_clipboard):
            return
        got = self.on_clipboard()
        if got:
            self.set_patch(*got)
        else:
            self.info.configure(text="剪贴板里没有图片。", fg=FG_BAD)

    def set_patch(self, img, name=""):
        """换一张要叠加的图，并把它摆到正中间。"""
        if img is None:
            return
        self.patch = img
        self.patch_name = name or "（未命名）"
        self.scale_pct = 100.0
        self.opacity = 100.0
        self.feather = 0.0
        self.mode = "normal"
        if "scale" in self.rows:
            self.rows["scale"].set(100)
            self.rows["opacity"].set(100)
            self.rows["feather"].set(0)
            self.mode_seg.set("normal")
        self._recenter()
        self._refresh_info()

    def _recenter(self):
        """默认摆在正中间 —— 比摆在左上角好得多（用户第一眼就看得见）。"""
        if self.patch is None or self.base is None:
            return
        k = self.scale_pct / 100.0
        self.pos = [(self.base.width - self.patch.width * k) / 2.0,
                    (self.base.height - self.patch.height * k) / 2.0]
        self._render()

    def _refresh_info(self):
        if self.patch is None:
            self.info.configure(text="（还没有选要叠加的图）", fg=FG_DIM)
            return
        self.info.configure(
            text="%s（%d×%d）" % (self.patch_name, self.patch.width,
                                 self.patch.height), fg=FG_DIM)

    # ---------- 交互 ----------
    def view_scale(self) -> float:
        """预览相对底图缩小了多少（1.0 = 一比一）。"""
        if self.base is None:
            return 1.0
        cw = self.canvas.winfo_width() or PREVIEW_MAX[0]
        ch = self.canvas.winfo_height() or PREVIEW_MAX[1]
        return min(1.0, max(0.02, cw / max(1, self.base.width)),
                   max(0.02, ch / max(1, self.base.height)))

    def move_by(self, dx: float, dy: float) -> None:
        self.pos[0] += dx
        self.pos[1] += dy
        self._render()

    def bump_scale(self, delta: float) -> None:
        self.scale_pct = max(5.0, min(400.0, self.scale_pct + delta))
        self.rows["scale"].set(self.scale_pct)
        self._render()

    def _on_scale(self, value):
        self.scale_pct = float(value)
        self._render()

    def _on_opacity(self, value):
        self.opacity = float(value)
        self._render()

    def _on_feather(self, value):
        self.feather = float(value)
        self._render()

    def _on_mode(self, key):
        self.mode = str(key)
        self._render()

    def after_idle(self):
        self.canvas.after(60, self._render)     # 等画布拿到真实尺寸再画第一帧

    def _render(self):
        try:
            self.canvas.render()
        except tk.TclError:
            pass                                # 窗口已经关了

    # ---------- 绘制 / 结果 ----------
    def preview_image(self):
        if self.base is None:
            return None
        k = self.view_scale()
        w = max(1, int(round(self.base.width * k)))
        h = max(1, int(round(self.base.height * k)))
        shown = self.base.convert("RGBA").resize((w, h), ops.resample())
        if self.patch is None:
            return ops.flatten_on(shown, (21, 23, 28))
        out = ops.overlay(shown, self.patch,
                          x=int(round(self.pos[0] * k)),
                          y=int(round(self.pos[1] * k)),
                          scale=self.scale_pct * k, opacity=self.opacity,
                          mode=self.mode, feather=self.feather * k)
        return ops.flatten_on(out, (21, 23, 28))

    def result_image(self):
        """按**底图原始分辨率**算最终结果（预览只是预览）。"""
        return ops.overlay(self.base, self.patch,
                           x=int(round(self.pos[0])), y=int(round(self.pos[1])),
                           scale=self.scale_pct, opacity=self.opacity,
                           mode=self.mode, feather=self.feather)

    def apply(self):
        if self.patch is None:
            self.info.configure(text="先选一张要叠加的图。", fg=FG_BAD)
            return
        if callable(self.on_ok):
            self.on_ok(self.result_image())
        self.close()


def build_overlay_body(parent, *, base, patch=None, name="", on_pick=None,
                       on_clipboard=None, on_ok=None, close=None):
    return OverlayForm(parent, base=base, patch=patch, name=name,
                       on_pick=on_pick, on_clipboard=on_clipboard,
                       on_ok=on_ok, close=close)


class OverlayDialog(BaseDialog):
    def __init__(self, parent, *, base, patch=None, name="", on_pick=None,
                 on_clipboard=None, on_ok=None):
        super().__init__(parent, "叠加图片", dy=30, resizable=True)
        self.form = build_overlay_body(self.body, base=base, patch=patch,
                                       name=name, on_pick=on_pick,
                                       on_clipboard=on_clipboard, on_ok=on_ok,
                                       close=self.apply_close)

# ==========================================================================
# 七、口令（左下角白点的隐藏入口用）
# ==========================================================================
class PasswordForm:
    """一个口令输入框 + 确定 / 取消。

    **它不判断口令对不对**：判断由调用方给的 ``on_ok(text) -> bool`` 负责
    （口令常量留在用它的那一层）。这样这个对话框本身不藏着任何"秘密"，
    将来换个用途也不用改这里 —— 和 ``serial_check`` 那套"题不是锁"的思路一致。
    """

    def __init__(self, parent, *, on_ok=None, close=None,
                 subtitle=None, ok_text="确定"):
        self.on_ok = on_ok
        self.close = close or (lambda: parent.winfo_toplevel().destroy())

        root = _wrap(parent)
        _heading(root, "验证", subtitle)
        _sep(root, pady=(12, 10))

        row = tk.Frame(root, bg=BG_ELEV)
        row.pack(fill="x")
        tk.Label(row, text="口令", bg=BG_ELEV, fg=FG_DIM,
                 font=font(9)).pack(side="left", padx=(0, 10))
        self.entry = RoundEntry(row, width=150, height=34, show="\u25cf",
                                justify="left", on_enter=self.apply)
        self.entry.pack(side="left")
        self.err = tk.Label(root, text=" ", bg=BG_ELEV, fg=FG_BAD,
                            font=font(9), anchor="w")
        self.err.pack(fill="x", pady=(8, 0))
        self.buttons = _footer(root, ok_text=ok_text, on_ok=self.apply,
                               on_cancel=self.close)

    def apply(self):
        text = self.entry.get().strip()
        if self.on_ok is None or self.on_ok(text):
            self.close()
        else:
            self.err.configure(text="口令不对，再试一次。")
            self.entry.set("")
            self.entry.focus_set()


def build_password_body(parent, *, on_ok=None, close=None, subtitle=None,
                        ok_text="确定"):
    return PasswordForm(parent, on_ok=on_ok, close=close, subtitle=subtitle,
                        ok_text=ok_text)


class PasswordDialog(BaseDialog):
    def __init__(self, parent, *, on_ok=None, subtitle=None, ok_text="确定"):
        super().__init__(parent, "验证", dy=40)
        self.form = build_password_body(self.body, on_ok=on_ok,
                                        close=self.apply_close,
                                        subtitle=subtitle, ok_text=ok_text)
        self.after(120, self.form.entry.focus_set)


# ==========================================================================
# 八、反馈（在程序里打字，交给系统的邮件程序发出去）
# ==========================================================================
class FeedbackForm:
    """多行输入框 + 「用邮件发送」/「复制文字」。

    **这个表单自己不发送任何东西**：``on_send(body)`` 与 ``on_copy(body)``
    都由调用方提供（只有那一层知道收件地址、也只有那一层碰系统）。
    所以这个文件里既没有邮箱地址，也没有网络调用 —— 少一处"秘密"少一处担心。

    ``on_send`` / ``on_copy`` 都要返回 ``(成功?, 说明)``，理由见约定"失败要吭声"：
    反馈这种东西，用户按下按钮却什么都没发生，他只会以为程序坏了。
    """

    #: 界面上的输入上限。不是技术上限（mailto 那边还有更硬的限制），
    #: 而是"再长就不是反馈了"的提醒 —— 超过就明说会发不出去。
    SOFT_LIMIT = 600

    def __init__(self, parent, *, version_info="", on_send=None, on_direct=None,
                 on_copy=None, on_policy=None, close=None, initial_reply=""):
        self.on_send = on_send
        self.on_direct = on_direct
        self.on_copy = on_copy
        self.on_policy = on_policy
        self.version_info = version_info
        self.close = close or (lambda: parent.winfo_toplevel().destroy())

        # 界面上**同一时刻只出现一条"发送"的路**：
        # 配了直达通道就用它（朋友不用有邮箱、也不用配邮件程序），
        # 没配才退回"交给系统邮件程序"。两条都留着只会让人不知道该点哪个。
        direct = callable(on_direct)
        mail = callable(on_send)
        root = _wrap(parent)
        if direct:
            _heading(root, "反馈",
                     "写几句就行，点「直接发送」就发到作者那里。")
            hint = ("发送失败（网络不通、通道没配好）时会显示原因 —— "
                    "那时点「复制文字」，粘到微信 / QQ 里发给作者也一样。")
        else:
            _heading(root, "反馈",
                     "写几句就行。发送时会用你电脑上的邮件程序写好一封信，"
                     "你看过之后再点发送。")
            hint = ("如果没弹出邮件窗口（多半是这台电脑没配邮件程序）："
                    "点「复制文字」，粘到微信 / QQ 里发给作者也一样。")
        _heading_note = tk.Label(root, text=hint, bg=BG_ELEV, fg=FG_DIM,
                                 font=font(9), anchor="w", justify="left",
                                 wraplength=560)
        self.hint = _heading_note
        self.hint.pack(fill="x", pady=(8, 0))

        tk.Label(root, text="可以直接打字，也可以粘贴。", bg=BG_ELEV,
                 fg=FG_DIM, font=font(9), anchor="w").pack(
                     fill="x", pady=(10, 4))

        box = tk.Frame(root, bg=BG_SUNKEN)
        box.pack(fill="both", expand=True)
        self.text = tk.Text(box, height=9, wrap="word", bg=BG_SUNKEN,
                            fg=FG_TEXT, insertbackground=FG_TEXT,
                            relief="flat", bd=0, highlightthickness=0,
                            font=font(10), padx=8, pady=6)
        self.text.pack(fill="both", expand=True)
        # Text 的"内容变了"要用 <<Modified>> 才抓得到（打字、粘贴、程序填值都算）
        self.text.bind("<<Modified>>", self._on_modified)

        self.count = tk.Label(root, text="", bg=BG_ELEV, fg=FG_DIM,
                              font=font(9), anchor="w")
        self.count.pack(fill="x", pady=(4, 0))

        # ---- 回信邮箱（**选填**）----
        # 为什么要有这个框：以前发出去的反馈里**没有任何能联系到用户的地址**
        # —— 那封信是从程序内置的发信账号寄出的，所以作者就算想回答也回不了
        # （浩然就遇到过"朋友问了问题、只能干看着"）。
        # ⚠ 做成**选填**而不是必填：只想吐槽一句的人不用留。
        #   隐私说明第 7 条写着"不同意也没关系"，一个必填框会把那句话毁掉。
        mail_row = tk.Frame(root, bg=BG_ELEV)
        mail_row.pack(fill="x", pady=(8, 0))
        # ⚠ 这里**故意不给 RoundEntry 传 placeholder**：那个功能全项目没人用过，
        #   实测有缺陷 —— 提示文字是当真值写进输入框的（不是灰字），
        #   用户一打字就变成 "you@example.comabc"；更糟的是 `get()` 在
        #   `_show_ph` 为真时**一律返回空串**，而打字并不会把它关掉 ——
        #   也就是"用户明明填了，程序却当没填"，而且**一声不响**。
        #   例子直接写在标签里，一样清楚，还不踩那个坑。
        tk.Label(mail_row, text="想收到回信？留个邮箱（不填也行，例如 me@qq.com）",
                 bg=BG_ELEV, fg=FG_TEXT, font=font(9), anchor="w").pack(side="left")
        self.reply_entry = RoundEntry(mail_row, width=200, height=30,
                                      justify="left",
                                      on_change=self._refresh_count)
        self.reply_entry.pack(side="left", padx=(8, 0))
        # 预填上次的邮箱（来自 state_store.get_last_reply_email —— 已经过
        # is_valid_reply_email 校验，所以塞进来不需要再判），
        # 通过 set() 而不是直接改 .var，才能正确触发"占位提示关闭"那一路逻辑。
        if initial_reply:
            self.reply_entry.set(initial_reply)

        if version_info:
            row = tk.Frame(root, bg=BG_ELEV)
            row.pack(fill="x", pady=(6, 0))
            self.attach = CheckRow(row, "附上版本与系统信息", value=True,
                                   command=lambda _v: self._refresh_count())
            self.attach.pack(side="left")
            tk.Label(row, text=version_info, bg=BG_ELEV, fg=FG_DIM,
                     font=font(9), anchor="w").pack(side="left", padx=(10, 0))
        else:
            self.attach = None

        self.msg = tk.Label(root, text=" ", bg=BG_ELEV, fg=FG_DIM, font=font(9),
                            anchor="w", justify="left", wraplength=560)
        self.msg.pack(fill="x", pady=(8, 0))

        _sep(root, pady=(10, 10))
        footer = tk.Frame(root, bg=BG_ELEV)
        footer.pack(fill="x")
        RoundButton(footer, "复制文字", self.copy, kind="solid", height=32,
                    pad_x=16).pack(side="right", padx=(0, 8))
        # 同一时刻只放一条"发送"的路（见 __init__ 里的说明）
        if direct:
            self.send_btn = RoundButton(footer, "直接发送", self.direct,
                                        kind="accent", height=32, pad_x=18)
        else:
            self.send_btn = RoundButton(footer, "用邮件发送", self.send,
                                        kind="accent", height=32, pad_x=18)
        self.send_btn.pack(side="right")
        RoundButton(footer, "取消", self.close, kind="solid", height=32,
                    pad_x=16).pack(side="right", padx=(0, 8))
        if callable(on_policy):
            # 同意过不代表不能再看看 —— 左边留一个随时能重读的入口
            self.policy_btn = RoundButton(footer, "隐私说明", on_policy,
                                          kind="solid", height=32, pad_x=14)
            self.policy_btn.pack(side="left")
        else:
            self.policy_btn = None

        self._refresh_count()

    # ---------- 内容 ----------
    def typed(self) -> str:
        """用户打的字（去掉首尾空白）。"""
        return self.text.get("1.0", "end").strip()

    def attach_info(self) -> bool:
        return bool(self.attach is not None and self.attach.get())

    def reply_to(self) -> str:
        """用户自愿留的回信邮箱（没留 / 还停在占位提示上 = 空串）。

        用 ``RoundEntry.get()`` 而不是 ``.var.get()``：占位提示是把提示文字
        **写进同一个变量**实现的，直接读变量会把 "you@example.com" 当成
        用户填的地址发出去 —— 那就成了一个根本没用的假地址。
        """
        try:
            return (self.reply_entry.get() or "").strip()
        except tk.TclError:
            return ""

    def composed(self) -> str:
        """真正会发出去的内容 = 用户打的字（+ 回信邮箱 + 用户勾选的信息）。

        把"会发什么"集中在这里只有一个好处：**用户看到的就是会发出去的**，
        不存在"界面上没写、但它偷偷加了一句"。
        """
        body = self.typed()
        tail = []
        email = self.reply_to()
        if email:
            # 写进正文**不只是**为了让作者看得见 —— smtp 那条路还会同时设
            # Reply-To（作者点一下"回复"就能直达），这里这句是"看得见的凭据"，
            # 万一某个收信端不认 Reply-To，作者也照着手抄得到。
            tail.append("回信邮箱：%s" % email)
        if self.attach_info() and self.version_info:
            tail.append("（程序自动附上：%s）" % self.version_info)
        if tail:
            body = "%s\n\n----\n%s" % (body, "\n".join(tail))
        return body

    # ---------- 交互 ----------
    def _on_modified(self, _event=None) -> None:
        try:
            self.text.edit_modified(False)      # 不复位的话只会触发一次
        except tk.TclError:
            return
        self._refresh_count()

    def _refresh_count(self) -> None:
        n = len(self.typed())
        if n > self.SOFT_LIMIT:
            self.count.configure(
                text="已输入 %d 字 —— 太长了，邮件程序可能发不出去，"
                     "建议精简到 %d 字以内" % (n, self.SOFT_LIMIT), fg=FG_WARN)
        else:
            self.count.configure(text="已输入 %d 字" % n, fg=FG_DIM)

    def _guard(self) -> str:
        """空内容不放行 —— 弹一封空邮件只会让双方都莫名其妙。"""
        if not self.typed():
            self.msg.configure(text="先写点什么吧（哪怕一句话也行）。", fg=FG_BAD)
            try:
                self.text.focus_set()
            except tk.TclError:
                pass
            return ""
        return self.composed()

    def send(self) -> None:
        body = self._guard()
        if not body:
            return
        if not callable(self.on_send):
            self.msg.configure(text="这个版本没有配置反馈地址。", fg=FG_BAD)
            return
        # 回信邮箱**一起交出去**：走系统邮件程序时它已经在正文里了，
        # 走直连（smtp）时那一层会拿它去设 Reply-To。
        ok, detail = self.on_send(body, self.reply_to())
        self.msg.configure(text=detail or ("已交给邮件程序。" if ok else "发送失败。"),
                           fg=FG_OK if ok else FG_BAD)

    def direct(self) -> None:
        """软件内直接发送（只有配了通道时界面上才会有这个按钮）。"""
        body = self._guard()
        if not body:
            return
        if not callable(self.on_direct):
            self.msg.configure(text="这个版本没有配置直达通道。", fg=FG_BAD)
            return
        try:
            ok, detail = self.on_direct(body, self.reply_to())
        except Exception as exc:                            # noqa: BLE001
            # 提交这条路绝不能把异常抛到 Tk 的回调里（那会是一个静默的崩溃）
            log.warning("反馈提交抛出异常：%s", exc)
            ok, detail = False, "发送时出错：%s。可以改用「复制文字」。" % exc
        self.msg.configure(text=detail or ("已发送。" if ok else "发送失败。"),
                           fg=FG_OK if ok else FG_BAD)

    def copy(self) -> None:
        body = self._guard()
        if not body:
            return
        if not callable(self.on_copy):
            self.msg.configure(text="复制失败：没有可用的剪贴板通道。", fg=FG_BAD)
            return
        ok, detail = self.on_copy(body)
        self.msg.configure(text=detail or ("已复制。" if ok else "复制失败。"),
                           fg=FG_OK if ok else FG_BAD)


def build_feedback_body(parent, *, version_info="", on_send=None, on_direct=None,
                        on_copy=None, on_policy=None, close=None,
                        initial_reply=""):
    return FeedbackForm(parent, version_info=version_info, on_send=on_send,
                        on_direct=on_direct, on_copy=on_copy,
                        on_policy=on_policy, close=close,
                        initial_reply=initial_reply)


class FeedbackDialog(BaseDialog):
    def __init__(self, parent, *, version_info="", on_send=None, on_direct=None,
                 on_copy=None, on_policy=None, initial_reply=""):
        super().__init__(parent, "反馈", dy=40)
        self.form = build_feedback_body(self.body, version_info=version_info,
                                        on_send=on_send, on_direct=on_direct,
                                        on_copy=on_copy, on_policy=on_policy,
                                        close=self.apply_close,
                                        initial_reply=initial_reply)
        self.after(120, self.form.text.focus_set)


# ==========================================================================
# 九、反馈前的隐私说明（第一次用反馈必须先过这一关）
# ==========================================================================
class ConsentForm:
    """把"什么东西会离开这台电脑"一条条摆出来，用户点了同意才继续。

    为什么必须有这个窗口：反馈的本质是把**用户自己打的字**发到别人的邮箱里。
    这件事必须在他按发送**之前**说清楚，而不是藏在一份没人看的文档里。所以：

      * 一条条列，不写成一整段大话（大话没人读）；
      * 明确写出**不带什么**（机器名、文件内容、浏览记录、账号一律不带）；
      * 明确写出**谁看得到**、**怎么发出去的**（用他自己的邮件程序，他能取消）；
      * 明确写出**不同意也没关系**（所有功能照常，下次还会再问）。

    文案由调用方给（那一层才是知道收件地址、也才知道政策版本的地方），
    这个表单只负责"摆出来 + 收集同意/不同意"。
    """

    def __init__(self, parent, *, paragraphs=(), note="", on_agree=None,
                 on_decline=None, close=None):
        self.on_agree = on_agree
        self.on_decline = on_decline
        self.close = close or (lambda: parent.winfo_toplevel().destroy())

        root = _wrap(parent)
        _heading(root, "反馈前，先说清楚",
                 "在按「同意」之前，请花十秒看完这几条。")

        for para in paragraphs:
            tk.Label(root, text=para, bg=BG_ELEV, fg=FG_TEXT, font=font(9),
                     anchor="w", justify="left", wraplength=560).pack(
                         fill="x", pady=(6, 0))

        if note:
            tk.Label(root, text=note, bg=BG_ELEV, fg=FG_DIM, font=font(8),
                     anchor="w", justify="left", wraplength=560).pack(
                         fill="x", pady=(10, 0))

        _sep(root, pady=(14, 10))
        footer = tk.Frame(root, bg=BG_ELEV)
        footer.pack(fill="x")
        self.agree_btn = RoundButton(footer, "同意并继续", self.agree,
                                     kind="accent", height=32, pad_x=18)
        self.agree_btn.pack(side="right")
        self.decline_btn = RoundButton(footer, "不同意", self.decline,
                                       kind="solid", height=32, pad_x=16)
        self.decline_btn.pack(side="right", padx=(0, 8))

    def agree(self) -> None:
        if callable(self.on_agree):
            self.on_agree()
        self.close()

    def decline(self) -> None:
        if callable(self.on_decline):
            self.on_decline()
        self.close()


def build_consent_body(parent, *, paragraphs=(), note="", on_agree=None,
                       on_decline=None, close=None):
    return ConsentForm(parent, paragraphs=paragraphs, note=note,
                       on_agree=on_agree, on_decline=on_decline, close=close)


class ConsentDialog(BaseDialog):
    def __init__(self, parent, *, paragraphs=(), note="", on_agree=None,
                 on_decline=None):
        super().__init__(parent, "反馈前，先说清楚", dy=30)
        self.form = build_consent_body(self.body, paragraphs=paragraphs,
                                       note=note, on_agree=on_agree,
                                       on_decline=on_decline,
                                       close=self.apply_close)


# ==========================================================================
# 删除 AI 模型：第二步确认（必须亲手勾选）
# ==========================================================================
def delete_models_points(size_text: str = "") -> tuple:
    """删除 AI 模型要说清的那几句话。**只此一份**（界面和测试都读它）。

    写成一条条而不是一整段，理由和同意书那边一样：一整段没人读。

    内容按重要性排，而且**先说"不会动你什么"**——用户看到"删除"两个字时，
    第一个念头是"会不会删掉我的照片"。那句话必须排在最前面。

    Returns:
        ``((文字, 是否强调), ...)``。**强调不是靠 Markdown 星号** ——
        ``tk.Label`` 不认 Markdown，写 ``**这样**`` 只会把星号原样显示出来。
        所以强调在这里是个布尔量，由渲染那层决定用哪个颜色。
    """
    takes = "（现在占 %s）" % size_text if size_text else ""
    return (
        ("会删掉程序自己下载的「AI 模型」文件夹%s。" % takes, False),
        ("不会动你的照片、桌面和任何别的文件 —— 只删它自己下回来的模型。", True),
        ("删掉的东西进回收站，后悔了可以去回收站还原。", False),
        ("删掉之后：超分放大、降噪、抠图、图片修复都用不了，"
         "什么时候想用再下载一次就行。", False),
        ("「用一句话改图」和画笔抠图不受影响（它们不靠模型）。", False),
    )


#: 勾选框上那句话。单独拎出来是为了测试能直接断言"要勾的是这句"。
DELETE_AGREE_TEXT = "我已了解：删除后 AI 修图功能将不可用，直到重新下载"


class DeleteModelsForm:
    """删除 AI 模型的**第二步**确认：必须亲手勾一下，才能点「确定删除」。

    为什么要有这一步，而不是一个"确定/取消"就完事：

    * **删除是不可逆操作里最容易被误点的一种** —— 菜单里「下载 AI 模型」和
      「删除 AI 模型」就挨着，手一抖就点错，而这两件事的后果天差地别。
    * 所以把"确认"做成**一个动作**（勾选）而不是**一个按钮**：
      按钮可以盲点，勾选必须真的看一眼那句话。
      确定按钮在没勾之前是**禁用**的（``RoundButton.set_state("disabled")``），
      不是"点了再弹一句你还没勾"——后者等于让人白点一次。

    真正的第一步（弹框提醒"会删什么、点确定"）在 ``ai_tool`` 那层用
    ``messagebox`` 完成；这里只负责"知道了，而且是主动勾的"。
    两层分开还有一个好处：测试可以只构建这个表单，不弹任何窗口。
    """

    def __init__(self, parent, *, size_text="", on_ok=None, close=None):
        self.on_ok = on_ok
        self.close = close or (lambda: parent.winfo_toplevel().destroy())

        root = _wrap(parent)
        _heading(root, "删除 AI 模型",
                 "这是第二步确认。请看完下面五条再决定。")

        for line, important in delete_models_points(size_text):
            tk.Label(root, text=line, bg=BG_ELEV,
                     fg=ACCENT if important else FG_TEXT, font=font(9),
                     anchor="w", justify="left", wraplength=560).pack(
                         fill="x", pady=(6, 0))

        _sep(root, pady=(14, 10))
        self.check = CheckRow(root, DELETE_AGREE_TEXT, value=False, bg=BG_ELEV)
        self.check.pack(fill="x")

        _sep(root, pady=(12, 10))
        footer = tk.Frame(root, bg=BG_ELEV)
        footer.pack(fill="x")
        self.ok_btn = RoundButton(footer, "确定删除", self.agree,
                                  kind="danger", height=32, pad_x=18)
        self.ok_btn.pack(side="right")
        RoundButton(footer, "取消", self.decline, kind="solid", height=32,
                    pad_x=16).pack(side="right", padx=(0, 8))

        # ★ 按钮状态**跟着勾选框走**，而不是"只在点击时更新一次"。
        #
        # 用 `command=` 的写法有个藏起来的坑（实测踩到）：`CheckRow.set()`
        # 只改值、**不调 command**，所以任何"非鼠标点击"的改动（键盘、
        # 程序性 set、以后加的可访问性支持）都会让按钮卡在旧状态 ——
        # 表现出来就是"勾了，但确定还是灰的"，而且没有任何报错。
        # 挂到变量自己的 trace 上就没有这个问题：不管谁改的，都会同步。
        self.check.var.trace_add("write", lambda *_: self._sync_ok())
        self._sync_ok()

    def _sync_ok(self) -> None:
        """把确定按钮的可用状态对齐到勾选框当前的值。"""
        checked = self.check.get()
        self.ok_btn.set_state("normal" if checked else "disabled")
        return checked

    def agree(self) -> bool:
        """确定按钮的回调：**自己再挡一道**。

        按钮虽然已经禁用了，但禁用是"界面层"的；这里的判断是"逻辑层"的。
        少写这一道的话，将来谁把按钮状态改坏了（或者用键盘触发），
        就又变成"没勾也能删"。
        """
        if not self.check.get():
            return False
        if callable(self.on_ok):
            self.on_ok()
        self.close()
        return True

    def decline(self) -> None:
        self.close()


def build_delete_models_body(parent, *, size_text="", on_ok=None, close=None):
    return DeleteModelsForm(parent, size_text=size_text, on_ok=on_ok,
                            close=close)


class DeleteModelsDialog(BaseDialog):
    def __init__(self, parent, *, size_text="", on_ok=None):
        super().__init__(parent, "删除 AI 模型", dy=40)
        self.form = build_delete_models_body(self.body, size_text=size_text,
                                             on_ok=on_ok,
                                             close=self.apply_close)


class MatteBrushForm:
    """选择性抠图画笔弹窗的内容体。

    输入 ``base_rgba``（自动抠好的 RGBA，原图尺寸）与 ``orig_rgb``（原图 RGB）。
    用户在预览上拖动画笔：``erase`` 把 alpha 压到 0，``restore`` 把 alpha 提到 255
    并从原图取回颜色。笔划以**原图坐标**记录在 ``strokes`` 里，便于撤销重放。

    纯 Pillow 运算，不跑模型——复用现有 44MB 的 matting 权重即可，无需新模型。
    """
    BRUSH_MIN = 4
    BRUSH_MAX = 160

    def __init__(self, parent, *, base_rgba, orig_rgb, on_ok, on_cancel=None,
                 title="选择性抠图 · 画笔精修",
                 hint="已自动抠好整图。用画笔擦掉不要的部分，或把误删的主体补回来；"
                      "点「应用到图片」完成。"):
        self.base = base_rgba.convert("RGBA")
        self.orig_rgb = orig_rgb.convert("RGB")
        self.on_ok = on_ok
        self.on_cancel = on_cancel or (lambda: parent.winfo_toplevel().destroy())
        self.strokes = []
        self.cur = self.base.copy()
        self._ready = False
        self._drawing = False
        self._last = None
        self._mode = "erase"
        self._brush_px = 40

        # 预览缩放：显示缩到 PREVIEW_MAX 以内，坐标只存原图坐标。
        self.scale = min(PREVIEW_MAX[0] / max(1, self.base.width),
                         PREVIEW_MAX[1] / max(1, self.base.height), 1.0)
        self.pw = max(1, int(round(self.base.width * self.scale)))
        self.ph = max(1, int(round(self.base.height * self.scale)))
        self._photo = None
        self._img_id = None

        root = _wrap(parent)
        _heading(root, title, hint)
        self._build_toolbar(root)
        self.canvas = tk.Canvas(root, width=self.pw, height=self.ph,
                                bg=BG_VIEW, cursor="cross")
        self.canvas.pack(pady=(6, 8))
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        _footer(root, ok_text="应用到图片", on_ok=self._confirm,
                on_cancel=self._cancel, left=self._left_buttons)
        self.after_idle()
        self._ready = True

    # ---------- 工具栏 ----------
    def _build_toolbar(self, root):
        bar = tk.Frame(root, bg=BG_ELEV)
        bar.pack(fill="x", pady=(8, 0))
        tk.Label(bar, text="画笔", bg=BG_ELEV, fg=FG_TEXT, font=font(9)
                 ).pack(side="left", padx=(0, 6))
        self.seg = Segmented(bar, [("擦除", "erase"), ("恢复", "restore")],
                             value="erase", command=self._on_mode)
        self.seg.pack(side="left")
        self.brush = SliderRow(root, "画笔大小", self.BRUSH_MIN, self.BRUSH_MAX,
                               value=40, command=self._on_brush, suffix=" px",
                               reset_value=40, width=200)
        self.brush.pack(fill="x", pady=(6, 0))

    def _left_buttons(self, row):
        RoundButton(row, "撤销", self._undo, kind="solid", height=30,
                    pad_x=12).pack(side="left")
        RoundButton(row, "重置", self._reset, kind="solid", height=30,
                    pad_x=12).pack(side="left", padx=(8, 0))

    # ---------- 模式 / 画笔 ----------
    def _on_mode(self, key):
        self._mode = str(key)

    def _on_brush(self, value):
        self._brush_px = max(self.BRUSH_MIN, min(self.BRUSH_MAX, int(value)))

    # ---------- 鼠标 ----------
    def _to_img(self, ex, ey):
        return ex / self.scale, ey / self.scale

    def _brush_r_img(self):
        return self._brush_px / self.scale

    def _press(self, event):
        if not self._ready:
            return
        self._drawing = True
        self._last = (event.x, event.y)
        self._stamp(event.x, event.y)

    def _motion(self, event):
        if not self._ready or not self._drawing:
            return
        x0, y0 = self._last
        self._stroke_line(x0, y0, event.x, event.y)
        self._last = (event.x, event.y)

    def _release(self, _event):
        self._drawing = False

    def _stamp(self, ex, ey):
        cx, cy = self._to_img(ex, ey)
        self.strokes.append((cx, cy, self._brush_r_img(), self._mode))
        self.cur = ai_ops.refine_matte(self.cur, self.orig_rgb, [self.strokes[-1]])
        self._render()

    def _stroke_line(self, x0, y0, x1, y1):
        dist = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        step = max(1.0, self._brush_px / 2.0)
        n = max(1, int(dist / step))
        for i in range(1, n + 1):
            t = i / n
            self._stamp(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)

    # ---------- 撤销 / 重置 ----------
    def _undo(self):
        if not self.strokes:
            return
        self.strokes.pop()
        self.cur = ai_ops.refine_matte(self.base, self.orig_rgb, self.strokes)
        self._render()

    def _reset(self):
        self.strokes = []
        self.cur = self.base.copy()
        self._render()

    # ---------- 提交 ----------
    def _confirm(self):
        if callable(self.on_ok):
            self.on_ok(self.cur.copy())
        self._cancel()

    def _cancel(self):
        if callable(self.on_cancel):
            self.on_cancel()

    # ---------- 预览 ----------
    def _checker(self):
        tile = 8
        img = Image.new("RGBA", (self.pw, self.ph), (96, 96, 96, 255))
        d = ImageDraw.Draw(img)
        for y in range(0, self.ph, tile * 2):
            for x in range(0, self.pw, tile * 2):
                d.rectangle([x, y, x + tile - 1, y + tile - 1],
                            fill=(72, 72, 72, 255))
                d.rectangle([x + tile, y + tile, x + 2 * tile - 1, y + 2 * tile - 1],
                            fill=(72, 72, 72, 255))
        return img

    def _render(self):
        try:
            preview = self.cur.resize((self.pw, self.ph), Image.BILINEAR)
            chk = self._checker()
            chk.paste(preview, mask=preview.split()[3])
            self._photo = ImageTk.PhotoImage(chk)
            if self._img_id is None:
                self._img_id = self.canvas.create_image(
                    self.pw // 2, self.ph // 2, anchor="center", image=self._photo)
            else:
                self.canvas.itemconfigure(self._img_id, image=self._photo)
                self.canvas.coords(self._img_id, self.pw // 2, self.ph // 2)
        except tk.TclError:
            pass

    def after_idle(self):
        self.canvas.after(60, self._render)


class MatteBrushDialog(BaseDialog):
    """选择性抠图对话框：自动抠好的 RGBA + 原图，交给 MatteBrushForm 画笔精修。"""
    def __init__(self, parent, *, base_rgba, orig_rgb, on_ok, on_cancel=None,
                 title="选择性抠图 · 画笔精修",
                 hint="已自动抠好整图。用画笔擦掉不要的部分，或把误删的主体补回来；"
                      "点「应用到图片」完成。"):
        super().__init__(parent, "选择性抠图", dx=120, dy=80, resizable=True)
        self.form = MatteBrushForm(self.body, base_rgba=base_rgba,
                                   orig_rgb=orig_rgb, on_ok=on_ok,
                                   on_cancel=self.apply_close,
                                   title=title, hint=hint)


__all__ = [
    "MAX_SIDE", "BIG_RESULT_MP", "PREVIEW_PIXELS", "ASPECTS", "TONES",
    "BaseDialog", "ResizeForm", "ConvertForm", "CropForm", "AdjustForm",
    "PasswordForm", "build_password_body", "PasswordDialog",
    "CropCanvas", "build_resize_body", "build_convert_body", "build_crop_body",
    "build_adjust_body", "ResizeDialog", "ConvertDialog", "CropDialog",
    "AdjustDialog",
    "ProgressForm", "build_progress_body", "ProgressDialog",
    "PREVIEW_MAX", "OverlayCanvas", "OverlayForm", "build_overlay_body",
    "OverlayDialog",
    "MatteBrushForm", "MatteBrushDialog",
    "FeedbackForm", "build_feedback_body", "FeedbackDialog",
    "ConsentForm", "build_consent_body", "ConsentDialog",
]
