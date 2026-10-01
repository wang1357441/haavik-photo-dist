# -*- coding: utf-8 -*-
"""看图区：滚轮缩放 / 拖拽平移 / 棋盘格 / 动图播放。

这一层只负责"把一张图摆给用户看"，不改图。所有像素运算在 ``image_ops`` 里。

三个决定观感的关键点
==================================================================
**一、平移不复用图片。** 拖拽的时候只在画布上 ``coords()`` 挪那个
``create_image`` 项 —— 走的是 Tk 自己的绘制，不重新做一次
``resize`` + ``PhotoImage``。反过来做的话，拖一张 4000×3000 的图会
每帧重采样一遍，手感立刻是"卡"。所以：**缩放决定图片的尺寸，
平移只决定它的位置**，两件事分开。

**二、棋盘格画在画布坐标系里，不是图片坐标系里。** 看上去是细节，
其实差一个数量级：垫在图片坐标系下要给 4000×3000 的图铺 47000 个小方块，
垫在画布坐标系下只要铺一次 1200×800（还要缓存起来复用）。
而且 Windows「照片」的棋盘格本来就是**屏幕尺寸固定**的 ——
放大图片时格子不会跟着变大。两边的做法在这里是一致的。

**三、放大和缩小用不同的重采样算法。** 缩小时输出变小，用 LANCZOS
（质量好、也不慢）；放大时输出可能上千万像素，用 BILINEAR
（快得多，肉眼看不出和 LANCZOS 的区别 —— 因为放大本来就没有信息可恢复）。
滚轮连续滚动时再加一层 30ms 防抖，免得每来一个 wheel 事件就重采一次。
"""
from __future__ import annotations

import logging
import tkinter as tk

import image_ops as ops
from ui_theme import BG_VIEW

try:
    from PIL import ImageTk
except ImportError:                                      # noqa: BLE001
    ImageTk = None

log = logging.getLogger("haavik.view")

#: 缩放倍数范围。下限再小也没有意义（整张图变成一个点），
#: 上限设 32 是因为再往上只是锯齿，没有信息。
MIN_ZOOM = 0.02
MAX_ZOOM = 32.0

#: 一个滚轮格子的缩放比例。1.15 比 1.25 顺，接近触控板的阻尼感。
ZOOM_STEP = 1.15

#: 滚轮防抖（毫秒）。连续滚动时只在停下来之后真正重采样一次。
WHEEL_DEBOUNCE_MS = 30

#: 渲染尺寸超过这个像素数就退回 NEAREST —— 再好的算法也换不来不卡。
BIG_RENDER_PIXELS = 12_000_000


class ImageViewer(tk.Frame):
    """看图区。把它 pack/grid 到主界面里就行。

    ``on_zoom``    缩放率变化时回调（参数是倍数，1.0 = 100%），用来刷状态栏。
    ``on_state``   图为空 / 有图时回调，用来启用或禁用工具栏按钮。
    """

    def __init__(self, parent, *, on_zoom=None, on_state=None, bg=BG_VIEW,
                 checker_cell=8):
        super().__init__(parent, bg=bg)
        self._on_zoom = on_zoom
        self._on_state = on_state
        self._bg = bg
        self._cell = checker_cell

        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0,
                                takefocus=1)
        self.canvas.pack(fill="both", expand=True)

        # ---- 内容 ----
        self._image = None            # 当前工作图（PIL.Image）
        self._original = None         # 用于"按住对比原图"
        self._frames = []             # 动图帧
        self._durations = []
        self._frame_index = 0

        # ---- 视图状态 ----
        self._zoom = 1.0              # 1.0 = 图片像素 : 屏幕像素 1:1
        self._fit_mode = True         # 当前是不是"适应窗口"
        self._ox = 0.0                # 图片左上角在画布里的位置
        self._oy = 0.0

        # ---- 内部 ----
        self._photo = None            # 必须持引用，否则被 GC 后显示为空白
        self._item = None             # create_image 的 item id
        self._checker_item = None
        self._checker_photo = None
        self._render_key = None       # (源对象 id, 渲染尺寸, 算法) —— 命中就不重做
        self._wheel_after = None
        self._anim_after = None
        self._anim_on = True
        self._comparing = False
        self._drag_from = None

        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<MouseWheel>", self._on_wheel)          # Windows
        self.canvas.bind("<Button-4>", lambda e: self._on_wheel(e, 1))   # X11 上滚
        self.canvas.bind("<Button-5>", lambda e: self._on_wheel(e, -1))  # X11 下滚
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", self._on_double)
        self.canvas.bind("<ButtonPress-2>", self._on_press)       # 中键也能拖
        self.canvas.bind("<B2-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-2>", self._on_release)

        # 构造时就播一次状态。少了这一行，工具栏会停在"打开时是什么样"
        # （初始为全都可用），而不是"还没有图，该全禁用"。
        self._notify_state()

    # ==================================================================
    # 内容
    # ==================================================================
    def set_image(self, img, *, frames=None, durations=None, keep_view=False):
        """换一张图。``frames`` 非空就按动图播。

        ``keep_view=True`` 保留当前的缩放与位置（"旋转 90° 之后视图别跳"）。
        """
        self._stop_animation()
        self._image = img
        self._frames = list(frames or [])
        self._durations = list(durations or [])
        self._frame_index = 0
        self._render_key = None
        if not keep_view:
            self._fit_mode = True
        self._apply_source()
        if self._fit_mode:
            self._recompute_fit()        # 只在需要时算一次，别多画一帧
        else:
            self._clamp_offset()
        self._redraw(force=True)
        self._maybe_start_animation()
        self._notify_state()
        self._notify_zoom()

    def set_original(self, img):
        """设置"按住对比"用的原图。"""
        self._original = img

    def clear(self):
        self._stop_animation()
        self._image = None
        self._original = None
        self._frames = []
        self._durations = []
        self._photo = None
        self._shown = None
        self._render_key = None
        self.canvas.delete("all")
        self._item = None
        self._checker_item = None
        self._checker_photo = None
        self._notify_state()

    @property
    def image(self):
        return self._image

    def has_image(self) -> bool:
        return self._image is not None

    # ==================================================================
    # 缩放 / 平移
    # ==================================================================
    def get_zoom(self) -> float:
        return self._zoom

    def canvas_size(self):
        try:
            return (max(1, self.canvas.winfo_width()),
                    max(1, self.canvas.winfo_height()))
        except tk.TclError:
            return (1, 1)

    def fit(self):
        """适应窗口：整张图都看得见，居中。"""
        if self._image is None:
            return
        self._fit_mode = True
        self._recompute_fit()
        self._redraw(force=True)
        self._notify_zoom()

    def fit_width(self):
        """适应宽度：按宽度铺满（长图滚动查看时用）。"""
        if self._image is None:
            return
        cw, _ch = self.canvas_size()
        self._fit_mode = False
        self._set_zoom(cw / float(self._image.width), anchor="center")
        self.pan_to(top=True)

    def actual(self):
        """1:1，一个图片像素对一个屏幕像素。"""
        if self._image is None:
            return
        self._fit_mode = False
        self._set_zoom(1.0, anchor="center")

    def zoom_in(self, anchor=None):
        self._zoom_by(ZOOM_STEP, anchor)

    def zoom_out(self, anchor=None):
        self._zoom_by(1.0 / ZOOM_STEP, anchor)

    def set_zoom(self, factor, anchor=None):
        self._fit_mode = False
        self._set_zoom(float(factor), anchor=anchor)

    def toggle_fit(self):
        """在"适应窗口"和 100% 之间来回。"""
        if self._image is None:
            return
        if self._fit_mode:
            self.actual()
        else:
            self.fit()

    def pan_to(self, *, center=False, top=False):
        if self._image is None:
            return
        if center:
            self._center()
        elif top:
            cw, _ch = self.canvas_size()
            w = self._image.width * self._zoom
            self._ox = (cw - w) / 2.0
            self._oy = 0.0
            self._place()
        self._redraw()

    def _zoom_by(self, factor, anchor=None):
        if self._image is None:
            return
        self._fit_mode = False
        self._set_zoom(self._zoom * factor, anchor=anchor)

    def _set_zoom(self, target, anchor=None):
        """改缩放率。``anchor`` 是画布坐标 —— 缩放会以这个点为不动点。

        Windows「照片」就是"以鼠标位置为锚点"缩放：光标指着的那个细节
        在缩放前后停在原地。少了这一步，缩放会变成"从左上角开始放大"，
        用起来非常别扭。
        """
        if self._image is None:
            return
        target = max(MIN_ZOOM, min(MAX_ZOOM, float(target)))
        if abs(target - self._zoom) < 1e-9:
            return
        cw, ch = self.canvas_size()
        if anchor is None:
            anchor = "center"
        if anchor == "center":
            px, py = cw / 2.0, ch / 2.0
        else:
            px, py = float(anchor[0]), float(anchor[1])
        ratio = target / self._zoom
        self._ox = px - (px - self._ox) * ratio
        self._oy = py - (py - self._oy) * ratio
        self._zoom = target
        self._clamp_offset()
        self._redraw(force=True)
        self._notify_zoom()

    def _recompute_fit(self):
        cw, ch = self.canvas_size()
        k = ops.fit_size(self._image.width, self._image.height, cw, ch)
        self._zoom = max(MIN_ZOOM, k[0] / float(self._image.width))
        self._center()

    def _center(self):
        cw, ch = self.canvas_size()
        w = self._image.width * self._zoom
        h = self._image.height * self._zoom
        self._ox = (cw - w) / 2.0
        self._oy = (ch - h) / 2.0

    def _clamp_offset(self):
        """别让图片被拖到完全看不见。

        留一点余量（图片比画布小时居中；比画布大时允许拖到边缘对齐），
        否则"拖过头"之后用户会以为图没了。
        """
        if self._image is None:
            return
        cw, ch = self.canvas_size()
        w = self._image.width * self._zoom
        h = self._image.height * self._zoom
        margin = 40.0
        if w <= cw:
            self._ox = (cw - w) / 2.0
        else:
            self._ox = min(margin, max(cw - w - margin, self._ox))
        if h <= ch:
            self._oy = (ch - h) / 2.0
        else:
            self._oy = min(margin, max(ch - h - margin, self._oy))

    # ==================================================================
    # 交互
    # ==================================================================
    def _on_wheel(self, event, direction=None):
        if self._image is None:
            return
        if direction is None:
            direction = 1 if getattr(event, "delta", 0) > 0 else -1
        if direction == 0:
            return
        anchor = (event.x, event.y)
        self._fit_mode = False
        factor = ZOOM_STEP if direction > 0 else 1.0 / ZOOM_STEP
        # 先按"快算法"渲染一帧，让画面立刻跟手；停手后再补一帧高质量的。
        # 这是"顺滑"和"清晰"都要的唯一办法：两者本来是对立的。
        self._set_zoom(self._zoom * factor, anchor=anchor)
        if self._wheel_after is not None:
            try:
                self.after_cancel(self._wheel_after)
            except (tk.TclError, ValueError):
                pass
        self._wheel_after = self.after(WHEEL_DEBOUNCE_MS, self._wheel_settle)

    def _wheel_settle(self):
        self._wheel_after = None
        self._render_key = None       # 丢掉缓存，用最好的算法重画一次
        self._redraw(force=True)

    def _on_press(self, event):
        self.canvas.focus_set()
        if self._image is None:
            return
        self._drag_from = (event.x, event.y, self._ox, self._oy)
        self.canvas.configure(cursor="fleur")

    def _on_drag(self, event):
        if self._drag_from is None or self._image is None:
            return
        x0, y0, ox0, oy0 = self._drag_from
        self._ox = ox0 + (event.x - x0)
        self._oy = oy0 + (event.y - y0)
        self._fit_mode = False
        self._clamp_offset()
        self._place()                  # 只挪位置，不重采样 —— 拖拽才能跟手

    def _on_release(self, _event=None):
        self._drag_from = None
        self.canvas.configure(cursor="")

    def _on_double(self, event):
        if self._image is None:
            return
        # 双击：不是适应窗口就适应窗口，否则回到 100%（并以光标为锚点）
        if self._fit_mode:
            self.set_zoom(1.0, anchor=(event.x, event.y))
        else:
            self.fit()

    def _on_configure(self, _event=None):
        if self._image is None:
            self._checker_item = None
            self._checker_photo = None
            return
        if self._fit_mode:
            self._recompute_fit()
            self._redraw(force=True)
        else:
            self._clamp_offset()
            self._place()
        self._checker_photo = None      # 画布尺寸变了，棋盘格要重铺
        self._redraw()

    # ==================================================================
    # 对比原图 / 动图
    # ==================================================================
    def show_original(self, on: bool):
        """按住某个键（或按钮）时临时显示原图，松开恢复。"""
        if self._image is None or self._original is None:
            return
        on = bool(on)
        if on == self._comparing:
            return
        self._comparing = on
        self._render_key = None
        self._redraw(force=True)

    @property
    def comparing(self) -> bool:
        return self._comparing

    def set_animation_running(self, running: bool):
        self._anim_on = bool(running)
        if self._anim_on:
            self._maybe_start_animation()
        else:
            self._stop_animation()

    @property
    def animation_running(self) -> bool:
        return self._anim_after is not None

    def is_animated(self) -> bool:
        return len(self._frames) > 1

    @property
    def frame_index(self) -> int:
        return self._frame_index

    @property
    def frame_count(self) -> int:
        return len(self._frames)

    def _apply_source(self):
        """把"该显示哪一帧"落到 self._image 上。"""
        if len(self._frames) > 1:
            self._frame_index = min(self._frame_index, len(self._frames) - 1)
            self._image = self._frames[self._frame_index]

    def _maybe_start_animation(self):
        self._stop_animation()
        if not self._anim_on or len(self._frames) <= 1:
            return
        self._schedule_next_frame()

    def _schedule_next_frame(self):
        delay = 100
        if 0 <= self._frame_index < len(self._durations):
            delay = max(20, int(self._durations[self._frame_index]))
        self._anim_after = self.after(delay, self._advance_frame)

    def _advance_frame(self):
        self._anim_after = None
        if not self._anim_on or len(self._frames) <= 1:
            return
        self._frame_index = (self._frame_index + 1) % len(self._frames)
        self._apply_source()
        self._render_key = None
        self._redraw(force=True)
        self._schedule_next_frame()

    def _stop_animation(self):
        if self._anim_after is not None:
            try:
                self.after_cancel(self._anim_after)
            except (tk.TclError, ValueError):
                pass
            self._anim_after = None

    def destroy(self):
        # after 回调留在事件循环里而控件已销毁，会报 "invalid command name"。
        self._stop_animation()
        if self._wheel_after is not None:
            try:
                self.after_cancel(self._wheel_after)
            except (tk.TclError, ValueError):
                pass
            self._wheel_after = None
        self._photo = None
        self._checker_photo = None
        super().destroy()

    # ==================================================================
    # 绘制
    # ==================================================================
    def _source(self):
        if self._comparing and self._original is not None:
            return self._original
        return self._image

    def _redraw(self, force=False):
        src = self._source()
        if src is None or ImageTk is None:
            return
        cw, ch = self.canvas_size()
        if cw <= 1 or ch <= 1:
            return

        w = max(1, int(round(src.width * self._zoom)))
        h = max(1, int(round(src.height * self._zoom)))
        algo = self._pick_algo(w * h, src.size, (w, h))
        key = (id(src), w, h, algo)
        if not force and key == self._render_key:
            self._place()
            return

        try:
            scaled = src if (w, h) == src.size else src.resize((w, h), algo)
            self._photo = ImageTk.PhotoImage(scaled)
        except (MemoryError, ValueError, tk.TclError) as exc:
            log.warning("渲染 %dx%d 失败：%s", w, h, exc)
            return

        self._ensure_checker(cw, ch)
        if self._item is None:
            self._item = self.canvas.create_image(0, 0, anchor="nw",
                                                 image=self._photo)
        else:
            self.canvas.itemconfigure(self._item, image=self._photo)
            self.canvas.tag_raise(self._item)
        if self._checker_item is not None:
            self.canvas.tag_lower(self._checker_item)
        self._render_key = key
        self._place()

    def _pick_algo(self, out_pixels, src_size, out_size):
        """缩小用 LANCZOS，放大用 BILINEAR，超大用 NEAREST。

        Pillow 9.1 之后常量搬到了 ``Image.Resampling`` 枚举里；
        老版本还在 ``Image`` 模块上。``getattr(..., ops.Image)`` 两种都能取到。
        """
        resampling = getattr(ops.Image, "Resampling", ops.Image)
        if out_pixels > BIG_RENDER_PIXELS:
            return getattr(resampling, "NEAREST", 0)
        growing = out_size[0] > src_size[0] or out_size[1] > src_size[1]
        if growing:
            return getattr(resampling, "BILINEAR", ops.resample())
        return getattr(resampling, "LANCZOS", ops.resample())

    def _place(self):
        if self._item is None:
            return
        self.canvas.coords(self._item, int(round(self._ox)), int(round(self._oy)))

    def _ensure_checker(self, cw, ch):
        """透明图下面垫棋盘格。

        只在图片**确实有透明**的时候才铺 —— 否则多画一层没用的东西。
        每个画布尺寸只铺一次并缓存（``_checker_photo`` 被清空时重铺）。
        """
        src = self._source()
        if src is None or not ops.image_has_alpha(src):
            if self._checker_item is not None:
                self.canvas.delete(self._checker_item)
                self._checker_item = None
                self._checker_photo = None
            return
        if self._checker_photo is not None and self._checker_item is not None:
            return
        try:
            tile = ops.checkerboard((cw, ch), cell=self._cell)
            self._checker_photo = ImageTk.PhotoImage(tile)
        except (MemoryError, ValueError, tk.TclError) as exc:
            log.warning("生成棋盘格失败：%s", exc)
            return
        if self._checker_item is None:
            self._checker_item = self.canvas.create_image(0, 0, anchor="nw",
                                                         image=self._checker_photo)
        else:
            self.canvas.itemconfigure(self._checker_item, image=self._checker_photo)
        self.canvas.tag_lower(self._checker_item)

    # ==================================================================
    # 通知
    # ==================================================================
    def _notify_zoom(self):
        if callable(self._on_zoom):
            self._on_zoom(self._zoom)

    def _notify_state(self):
        if callable(self._on_state):
            self._on_state(self.has_image())


__all__ = ["ImageViewer", "MIN_ZOOM", "MAX_ZOOM", "ZOOM_STEP"]
