# -*- coding: utf-8 -*-
"""界面主题 + 自绘控件 + 矢量图标。

为什么要有这个文件
==================================================================
tkinter 的默认控件（灰底、浮雕边框、方角）就是"粗糙"的来源。
但本项目**不引入任何第三方 UI 库** —— 每多一个依赖，`Setup.exe` 的
依赖图里就多一批需要解释的东西，"这个 exe 只做我们说过的那些事"这条
可证明性就弱一分。所以这里用 Canvas **自己画**：

  * ``round_rect_points()``  真·圆角矩形的顶点（用多边形逼近四分之一圆）
  * ``RoundButton``          圆角按钮，带 hover / 按下 / 禁用三态
  * ``IconButton``           只有图标没有文字的圆角按钮
  * ``Slider``               圆角滑块（tk.Scale 太丑，且不吃主题）
  * ``RoundFrame``           圆角面板（Canvas 当背景 + 内嵌 Frame）
  * ``draw_icon()``          一套 Canvas 矢量图标（约 24 个）

Tk 的 Canvas **没有抗锯齿**，这是硬伤。缓解办法是：
  * 圆角用"多边形 + 每角 8 段"逼近，比 4 段弧线顺；
  * 曲线优先用 ``create_line(..., smooth=True)``；
  * 尽量用填充面而不是描边线来表达形状。
剩下那点锯齿，在现代高 DPI 屏上基本看不出来。

配色只有一处
==================================================================
锁屏页 / 问答页 / 工具页必须像同一个软件。所以颜色、圆角半径、间距、
字体族全在这里定义一次，别的文件一律引用，不抄字面量。
"""
from __future__ import annotations

import ctypes
import logging
import math
import tkinter as tk
from tkinter import font as tkfont

log = logging.getLogger("haavik.ui")

# --------------------------------------------------------------------------
# 配色
# --------------------------------------------------------------------------
BG_APP = "#101114"          # 应用底色
BG_TOOLBAR = "#181a20"      # 顶部工具栏
BG_VIEW = "#0a0b0e"         # 看图区（最暗，让图片自己跳出来）
BG_PANEL = "#181a20"        # 侧栏 / 底栏
BG_ELEV = "#1e2128"         # 浮起面板、对话框
BG_HOVER = "#24272f"
BG_ACTIVE = "#2d313a"
BG_SUNKEN = "#0d0e11"       # 输入框等"凹陷"面
STROKE = "#2a2e37"
STROKE_SOFT = "#21242b"

FG_TEXT = "#eceef2"
FG_DIM = "#8b93a1"
FG_MUTED = "#5d6572"
FG_ON_ACCENT = "#ffffff"

ACCENT = "#4c8dff"
ACCENT_HOVER = "#5f9bff"
ACCENT_PRESS = "#3d7ae8"
FG_OK = "#3ddc84"
FG_WARN = "#ffc94d"
FG_BAD = "#ff5f56"

# 棋盘格（PNG/WebP 的透明区）
CHECKER_A = "#1a1c21"
CHECKER_B = "#22252c"

RADIUS_SM = 6
RADIUS_MD = 10
RADIUS_LG = 14
PAD = 10

FONT_UI = "Microsoft YaHei UI"
FONT_MONO = "Consolas"


def font(size: int, weight: str = "normal") -> tuple:
    return (FONT_UI, size, weight)


def mono(size: int, weight: str = "normal") -> tuple:
    return (FONT_MONO, size, weight)


# --------------------------------------------------------------------------
# 圆角
# --------------------------------------------------------------------------
def round_rect_points(x1, y1, x2, y2, r, steps=8):
    """返回一个圆角矩形的多边形顶点，可直接给 create_polygon。

    用"每角 8 段"的折线逼近四分之一圆。相较"两个矩形 + 四个 pieslice 弧"
    的经典画法，这样只有一个图元、能带描边、还能整体改色（itemconfig）。
    """
    r = max(0.0, min(r, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    if r <= 0.5:
        return [x1, y1, x2, y1, x2, y2, x1, y2]
    corners = (
        (x2 - r, y2 - r, 0.0),      # 右下 0° → 90°
        (x1 + r, y2 - r, 90.0),     # 左下 90° → 180°
        (x1 + r, y1 + r, 180.0),    # 左上 180° → 270°
        (x2 - r, y1 + r, 270.0),    # 右上 270° → 360°
    )
    pts = []
    for cx, cy, a0 in corners:
        for i in range(steps + 1):
            a = math.radians(a0 + 90.0 * i / steps)
            pts.append(cx + r * math.cos(a))
            pts.append(cy + r * math.sin(a))
    return pts


def round_rect(canvas, x1, y1, x2, y2, r, **kw):
    """在 canvas 上画一个圆角矩形，返回 item id。"""
    return canvas.create_polygon(round_rect_points(x1, y1, x2, y2, r), **kw)


def circle(canvas, cx, cy, r, **kw):
    return canvas.create_oval(cx - r, cy - r, cx + r, cy + r, **kw)


# --------------------------------------------------------------------------
# 矢量图标
# --------------------------------------------------------------------------
#: 每个图标都在单位坐标（-1..1，中心为原点）里描述，再由 draw_icon 缩放。
#: 之所以不用 Unicode 字符：中文系统上那些符号会被渲染成彩色 Emoji，
#: 大小与基线都不可控，在工具栏里非常难看。
def _tags(tag, extra):
    """把 ``extra`` 并进 ``tag``（tag 可能是 None / str / tuple）。"""
    if tag is None:
        return (extra,)
    if isinstance(tag, (list, tuple)):
        return tuple(tag) + (extra,)
    return (tag, extra)


def _ln(canvas, pts, cx, cy, s, color, w, smooth=False, tag=None):
    coords = []
    for i in range(0, len(pts), 2):
        coords.append(cx + pts[i] * s)
        coords.append(cy + pts[i + 1] * s)
    return canvas.create_line(*coords, fill=color, width=w, capstyle="round",
                              joinstyle="round", smooth=smooth, tags=tag)


def _poly(canvas, pts, cx, cy, s, color, w, tag=None):
    coords = []
    for i in range(0, len(pts), 2):
        coords.append(cx + pts[i] * s)
        coords.append(cy + pts[i + 1] * s)
    return canvas.create_polygon(*coords, fill=color, outline=color,
                                 width=w, joinstyle="round", tags=tag)


def _oval(canvas, x, y, rx, ry, cx, cy, s, **kw):
    return canvas.create_oval(cx + (x - rx) * s, cy + (y - ry) * s,
                              cx + (x + rx) * s, cy + (y + ry) * s, **kw)


def _arc(canvas, x, y, r, a0, a1, cx, cy, s, **kw):
    return canvas.create_arc(cx + (x - r) * s, cy + (y - r) * s,
                             cx + (x + r) * s, cy + (y + r) * s,
                             start=a0, extent=a1 - a0, style="arc", **kw)


def draw_icon(canvas, name, cx, cy, size, color, w=1.6, tag=None):
    """在 (cx, cy) 处画一个 size 半宽的矢量图标。返回 item id 列表。

    找不到名字时画一个问号方块（不静默什么都不画 —— 那样只会让人以为
    "按钮坏了"，而不是"图标名写错了"），同时打一条 warning，并给这些
    图元挂上 tag ``icon-unknown``。挂 tag 是为了让测试能**程序化地**
    发现拼错的图标名 —— 否则只能靠一个个图标肉眼比。
    """
    s = float(size)
    kw = dict(fill=color, width=w, tags=tag, capstyle="round", joinstyle="round")
    items = []

    def L(pts, smooth=False, width=w):
        items.append(_ln(canvas, pts, cx, cy, s, color, width, smooth, tag))

    def P(pts):
        items.append(_poly(canvas, pts, cx, cy, s, color, w, tag))

    def O(x, y, rx, ry, width=w, fill=""):
        items.append(_oval(canvas, x, y, rx, ry, cx, cy, s,
                           outline=color, width=width, fill=fill, tags=tag))

    if name == "open":                      # 文件夹
        L([-0.95, -0.5, -0.45, -0.5, -0.2, -0.85, 0.9, -0.85], smooth=True)
        L([-0.95, -0.5, 0.9, -0.5, 0.9, 0.75, -0.95, 0.75, -0.95, -0.5], smooth=True)
    elif name == "image":                   # 图片（山 + 太阳）
        items.append(round_rect(canvas, cx - 0.92 * s, cy - 0.72 * s,
                                cx + 0.92 * s, cy + 0.72 * s, 0.18 * s,
                                outline=color, fill="", width=w, tags=tag))
        O(0.42, -0.34, 0.18, 0.18, width=w)
        L([-0.85, 0.55, -0.15, -0.25, 0.35, 0.28, 0.62, 0.02, 0.88, 0.55], smooth=True)
    elif name == "prev":
        L([0.3, -0.6, -0.3, 0.0, 0.3, 0.6], smooth=True, width=w + 0.4)
    elif name == "next":
        L([-0.3, -0.6, 0.3, 0.0, -0.3, 0.6], smooth=True, width=w + 0.4)
    elif name == "zoom_in" or name == "zoom_out":
        O(-0.15, -0.15, 0.62, 0.62)
        L([0.34, 0.34, 0.88, 0.88])
        L([-0.45, -0.15, 0.15, -0.15])
        if name == "zoom_in":
            L([-0.15, -0.45, -0.15, 0.15])
    elif name == "fit":                     # 四角括号
        for sx in (-1, 1):
            for sy in (-1, 1):
                L([sx * 0.92, sy * 0.34, sx * 0.92, sy * 0.92, sx * 0.34, sy * 0.92])
    elif name == "actual":                  # 1:1
        L([-0.42, -0.62, -0.42, 0.62])
        L([-0.42, -0.62, -0.78, -0.30])
        L([0.10, -0.34, 0.30, -0.62, 0.30, 0.62])
        L([0.68, -0.34, 0.88, -0.62, 0.88, 0.62])
    elif name == "rotate_l":
        _arc(canvas, 0, 0, 0.72, 40, 330, cx, cy, s, outline=color, width=w, tags=tag)
        P([-0.42, -0.95, -0.98, -0.62, -0.42, -0.30])
    elif name == "rotate_r":
        _arc(canvas, 0, 0, 0.72, 210, 500, cx, cy, s, outline=color, width=w, tags=tag)
        P([0.42, -0.95, 0.98, -0.62, 0.42, -0.30])
    elif name == "flip_h":
        L([0.0, -0.95, 0.0, 0.95])
        L([-0.30, -0.55, -0.92, 0.0, -0.30, 0.55, -0.30, -0.55], smooth=True)
        P([0.32, -0.52, 0.90, 0.0, 0.32, 0.52])
    elif name == "flip_v":                  # flip_h 上下镜像：横轴 + 上下箭头
        L([-0.95, 0.0, 0.95, 0.0])
        L([-0.55, -0.30, 0.0, -0.92, 0.55, -0.30, -0.55, -0.30], smooth=True)
        P([-0.52, 0.32, 0.0, 0.90, 0.52, 0.32])
    elif name == "crop":
        L([-0.62, -0.95, -0.62, 0.62, 0.95, 0.62])
        L([-0.95, -0.62, 0.62, -0.62, 0.62, 0.95])
    elif name == "adjust":                  # 三条滑杆
        for dy, x in ((-0.55, 0.30), (0.0, -0.30), (0.55, 0.10)):
            L([-0.9, dy, 0.9, dy])
            items.append(circle(canvas, cx + x * s, cy + dy * s, 0.18 * s,
                                fill=color, outline=color, tags=tag))
    elif name == "resize":
        L([-0.9, -0.55, -0.9, -0.9, -0.55, -0.9])
        L([0.9, 0.55, 0.9, 0.9, 0.55, 0.9])
        L([-0.75, -0.75, 0.75, 0.75])
        L([0.30, 0.75, 0.75, 0.75, 0.75, 0.30])
    elif name == "convert":
        L([-0.9, -0.40, 0.45, -0.40], smooth=False)
        P([0.85, -0.40, 0.35, -0.68, 0.35, -0.12])
        L([0.9, 0.45, -0.45, 0.45])
        P([-0.85, 0.45, -0.35, 0.73, -0.35, 0.17])
    elif name == "info":
        O(0, 0, 0.9, 0.9)
        L([0.0, -0.42, 0.0, 0.45])
        items.append(circle(canvas, cx, cy - 0.64 * s, 0.11 * s,
                            fill=color, outline=color, tags=tag))
    elif name == "play":
        P([-0.4, -0.72, 0.72, 0.0, -0.4, 0.72])
    elif name == "pause":
        P([-0.5, -0.7, -0.16, -0.7, -0.16, 0.7, -0.5, 0.7])
        P([0.16, -0.7, 0.5, -0.7, 0.5, 0.7, 0.16, 0.7])
    elif name == "fullscreen":
        for sx in (-1, 1):
            for sy in (-1, 1):
                L([sx * 0.92, sy * 0.36, sx * 0.92, sy * 0.92, sx * 0.36, sy * 0.92])
    elif name == "trash":
        L([-0.85, -0.55, 0.85, -0.55])
        L([-0.28, -0.55, -0.38, -0.88, 0.38, -0.88, 0.28, -0.55])
        L([-0.62, -0.55, -0.48, 0.9, 0.48, 0.9, 0.62, -0.55], smooth=True)
        L([-0.18, -0.30, -0.14, 0.55])
        L([0.18, -0.30, 0.14, 0.55])
    elif name == "save":
        L([-0.9, -0.9, 0.9, -0.9, 0.9, 0.9, -0.9, 0.9, -0.9, -0.9])
        items.append(round_rect(canvas, cx - 0.5 * s, cy - 0.9 * s,
                                cx + 0.5 * s, cy - 0.18 * s, 0.1 * s,
                                outline=color, fill="", width=w, tags=tag))
        items.append(round_rect(canvas, cx - 0.58 * s, cy + 0.2 * s,
                                cx + 0.58 * s, cy + 0.9 * s, 0.1 * s,
                                outline=color, fill="", width=w, tags=tag))
    elif name == "copy":
        items.append(round_rect(canvas, cx - 0.85 * s, cy - 0.85 * s,
                                cx + 0.2 * s, cy + 0.2 * s, 0.16 * s,
                                outline=color, fill="", width=w, tags=tag))
        items.append(round_rect(canvas, cx - 0.2 * s, cy - 0.2 * s,
                                cx + 0.85 * s, cy + 0.85 * s, 0.16 * s,
                                outline=color, fill="", width=w, tags=tag))
    elif name == "wallpaper":
        items.append(round_rect(canvas, cx - 0.95 * s, cy - 0.72 * s,
                                cx + 0.95 * s, cy + 0.5 * s, 0.14 * s,
                                outline=color, fill="", width=w, tags=tag))
        L([0.0, 0.5, 0.0, 0.82])
        L([-0.42, 0.82, 0.42, 0.82])
        L([-0.7, 0.16, -0.2, -0.34, 0.05, -0.06, 0.3, -0.3, 0.72, 0.16], smooth=True)
    elif name == "print":
        L([-0.42, -0.3, -0.42, -0.92, 0.42, -0.92, 0.42, -0.3])
        items.append(round_rect(canvas, cx - 0.95 * s, cy - 0.34 * s,
                                cx + 0.95 * s, cy + 0.42 * s, 0.14 * s,
                                outline=color, fill="", width=w, tags=tag))
        L([-0.42, 0.42, -0.42, 0.92, 0.42, 0.92, 0.42, 0.42])
    elif name == "folder":
        L([-0.95, -0.45, -0.42, -0.45, -0.18, -0.8, 0.9, -0.8], smooth=True)
        L([-0.95, -0.45, 0.9, -0.45, 0.9, 0.8, -0.95, 0.8, -0.95, -0.45], smooth=True)
    elif name == "more":
        for x in (-0.62, 0.0, 0.62):
            items.append(circle(canvas, cx + x * s, cy, 0.13 * s,
                                fill=color, outline=color, tags=tag))
    elif name == "close":
        L([-0.7, -0.7, 0.7, 0.7])
        L([0.7, -0.7, -0.7, 0.7])
    elif name == "undo":
        _arc(canvas, 0, 0.12, 0.7, 30, 300, cx, cy, s, outline=color, width=w, tags=tag)
        P([-0.72, -0.72, -0.95, -0.10, -0.32, -0.20])
    elif name == "redo":
        _arc(canvas, 0, 0.12, 0.7, -120, 150, cx, cy, s, outline=color, width=w, tags=tag)
        P([0.72, -0.72, 0.95, -0.10, 0.32, -0.20])
    elif name == "check":
        L([-0.75, 0.05, -0.2, 0.62, 0.8, -0.6], smooth=True, width=w + 0.6)
    elif name == "layers":
        # 叠加：后面一个框 + 前面一个框（"两张图摞在一起"）
        items.append(canvas.create_rectangle(
            cx - 0.85 * s, cy - 0.85 * s, cx + 0.3 * s, cy + 0.3 * s,
            outline=color, width=w, tags=tag))
        items.append(canvas.create_rectangle(
            cx - 0.3 * s, cy - 0.3 * s, cx + 0.85 * s, cy + 0.85 * s,
            outline=color, width=w + 0.6, tags=tag))
    elif name == "mail":
        # 反馈：一个信封（矩形 + 从两个上角折向中间的那道 V）
        items.append(canvas.create_rectangle(
            cx - 0.9 * s, cy - 0.6 * s, cx + 0.9 * s, cy + 0.6 * s,
            outline=color, width=w, tags=tag))
        L([-0.9, -0.6, 0, 0.18, 0.9, -0.6])
    elif name == "ai":
        # AI 修图：一颗四角闪光星（"本地智能"的意象），中心一个实心点
        P([0.0, -0.95, 0.24, -0.24, 0.95, 0.0, 0.24, 0.24,
           0.0, 0.95, -0.24, 0.24, -0.95, 0.0, -0.24, -0.24])
        items.append(circle(canvas, cx, cy, 0.17 * s,
                            fill=color, outline=color, tags=tag))
    else:
        log.warning("draw_icon: 未知图标名 %r —— 画问号占位（图标名写错了会走到这里）",
                    name)
        utag = _tags(tag, "icon-unknown")
        items.append(round_rect(canvas, cx - 0.8 * s, cy - 0.8 * s,
                                cx + 0.8 * s, cy + 0.8 * s, 0.2 * s,
                                outline=FG_BAD, fill="", width=w, tags=utag))
        items.append(_ln(canvas, [-0.35, -0.3, 0.0, 0.15], cx, cy, s,
                         FG_BAD, w, False, utag))
        items.append(circle(canvas, cx, cy + 0.42 * s, 0.1 * s,
                            fill=FG_BAD, outline=FG_BAD, tags=utag))
    return items


# --------------------------------------------------------------------------
# 自绘按钮
# --------------------------------------------------------------------------
class RoundButton(tk.Canvas):
    """圆角按钮：hover / 按下 / 禁用三态，可带图标。

    ``kind``：
      ``ghost``   透明底 + 悬停时浮起（工具栏默认）
      ``accent``  实心强调色（主操作）
      ``solid``   实心面板色（对话框里的「取消」这类）
      ``danger``  实心红（删除）
    """

    KINDS = {
        "ghost": (None, BG_HOVER, BG_ACTIVE, FG_TEXT),
        "accent": (ACCENT, ACCENT_HOVER, ACCENT_PRESS, FG_ON_ACCENT),
        "solid": (BG_HOVER, BG_ACTIVE, "#343943", FG_TEXT),
        "danger": ("#a3352f", "#bf4038", "#8c2b26", "#ffffff"),
    }

    def __init__(self, parent, text="", command=None, *, icon=None,
                 kind="ghost", height=34, radius=RADIUS_SM, pad_x=12,
                 font_size=10, tooltip=None, width=None, bg=None):
        self._bg = bg or _parent_bg(parent)
        super().__init__(parent, height=height, highlightthickness=0, bd=0,
                         bg=self._bg, takefocus=0)
        self._text = text
        self._icon = icon
        self._command = command
        self._kind = kind if kind in self.KINDS else "ghost"
        self._radius = radius
        self._pad_x = pad_x
        self._font = font(font_size)
        self._state = "normal"
        self._mode = "idle"
        self._fixed_w = width
        self._tip = tooltip
        self._fontobj = None

        self._width = width or self._natural_width()
        self.configure(width=self._width)
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Return>", lambda _e: self._invoke())
        if tooltip:
            Tooltip(self, tooltip)
        self.after_idle(self._redraw)

    # ----- 尺寸 -----
    def _natural_width(self) -> int:
        fs = font(self._font[1])
        tmp = tkfont.Font(font=fs)
        text_w = tmp.measure(self._text) if self._text else 0
        icon_w = 16 + 6 if self._icon else 0
        return int(text_w + icon_w + self._pad_x * 2)

    def set_text(self, text: str) -> None:
        self._text = text
        if self._fixed_w is None:
            self._width = self._natural_width()
            self.configure(width=self._width)
        self._redraw()

    def set_icon(self, icon: str | None) -> None:
        self._icon = icon
        self._redraw()

    def set_kind(self, kind: str) -> None:
        if kind in self.KINDS:
            self._kind = kind
            self._redraw()

    def set_state(self, state: str) -> None:
        self._state = state
        self.configure(cursor="" if state == "disabled" else "hand2")
        self._redraw()

    def set_command(self, command) -> None:
        self._command = command

    # ----- 状态 -----
    def _on_enter(self, _e=None):
        if self._state == "disabled":
            return
        self._mode = "hover"
        self._redraw()

    def _on_leave(self, _e=None):
        self._mode = "idle"
        self._redraw()

    def _on_press(self, _e=None):
        if self._state == "disabled":
            return
        self._mode = "press"
        self._redraw()

    def _on_release(self, _e=None):
        if self._state == "disabled":
            return
        inside = (0 <= self.winfo_pointerx() - self.winfo_rootx() <= self.winfo_width()
                  and 0 <= self.winfo_pointery() - self.winfo_rooty() <= self.winfo_height())
        self._mode = "hover" if inside else "idle"
        self._redraw()
        if inside:
            self._invoke()

    def _invoke(self):
        if self._state != "disabled" and callable(self._command):
            self._command()

    # ----- 绘制 -----
    def _redraw(self):
        try:
            w, h = self.winfo_width(), self.winfo_height()
        except tk.TclError:
            return
        if w <= 1 or h <= 1:
            self.after(16, self._redraw)
            return
        self.delete("all")
        base, hover, press, fg = self.KINDS[self._kind]
        if self._state == "disabled":
            fill = BG_HOVER if self._kind == "ghost" else "#2a2d34"
            fg = FG_MUTED
        elif self._mode == "press":
            fill = press
        elif self._mode == "hover":
            fill = hover
        else:
            fill = base

        if fill is not None:
            round_rect(self, 1, 1, w - 1, h - 1, self._radius, fill=fill, outline="")
        elif self._mode != "idle":
            round_rect(self, 1, 1, w - 1, h - 1, self._radius, fill=hover, outline="")

        cy = h / 2
        fs = self._font[1]
        icon_size = min(9, max(7, fs - 1))
        if self._icon and self._text:
            tmp = tkfont.Font(font=self._font)
            tw = tmp.measure(self._text)
            total = icon_size * 2 + 6 + tw
            x = (w - total) / 2
            draw_icon(self, self._icon, x + icon_size, cy, icon_size, fg, 1.7)
            self.create_text(x + icon_size * 2 + 6, cy, text=self._text, anchor="w",
                             fill=fg, font=self._font)
        elif self._icon:
            draw_icon(self, self._icon, w / 2, cy, icon_size, fg, 1.7)
        elif self._text:
            self.create_text(w / 2, cy, text=self._text, fill=fg, font=self._font)


class IconButton(RoundButton):
    """只有图标的方圆形按钮。"""

    def __init__(self, parent, icon, command=None, *, size=34, tooltip=None,
                 bg=None, kind="ghost"):
        super().__init__(parent, "", command, icon=icon, kind=kind,
                         height=size, width=size, radius=RADIUS_SM,
                         pad_x=0, tooltip=tooltip, bg=bg)


class Tooltip:
    """鼠标悬停 450ms 后出现的圆角提示条。"""

    def __init__(self, widget, text, delay=450):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after = None
        self._win = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def _schedule(self, _e=None):
        self.hide()
        self._after = self.widget.after(self.delay, self.show)

    def show(self):
        if self._win is not None:
            return
        try:
            x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        except tk.TclError:
            return
        win = tk.Toplevel(self.widget)
        win.wm_overrideredirect(True)
        win.attributes("-topmost", True)
        cv = tk.Canvas(win, highlightthickness=0, bd=0, bg=BG_APP)
        tmp = tkfont.Font(font=font(9))
        w = tmp.measure(self.text) + 20
        h = 26
        cv.configure(width=w, height=h)
        round_rect(cv, 1, 1, w - 1, h - 1, RADIUS_SM, fill="#000000", outline=STROKE)
        cv.create_text(w / 2, h / 2, text=self.text, fill=FG_TEXT, font=font(9))
        cv.pack()
        win.wm_geometry("+%d+%d" % (x - w // 2, y))
        self._win = win

    def hide(self, _e=None):
        if self._after:
            try:
                self.widget.after_cancel(self._after)
            except (tk.TclError, ValueError):
                pass
            self._after = None
        if self._win is not None:
            try:
                self._win.destroy()
            except tk.TclError:
                pass
            self._win = None


# --------------------------------------------------------------------------
# 圆角滑块
# --------------------------------------------------------------------------
class Slider(tk.Canvas):
    """圆角滑块。替代 tk.Scale —— 后者是 90 年代的外观，且不吃主题配色。

    用法与 Scale 接近：``var = Slider(parent, 0, 100, value=50, command=cb)``，
    ``var.get()`` / ``var.set(v)``。
    """

    def __init__(self, parent, lo, hi, *, value=None, command=None, height=28,
                 width=200, bg=None, fmt="%d", step=1):
        self._bg = bg or _parent_bg(parent)
        super().__init__(parent, height=height, width=width, highlightthickness=0,
                         bd=0, bg=self._bg)
        self.lo, self.hi = float(lo), float(hi)
        self._v = float(lo if value is None else value)
        self._cmd = command
        self._fmt = fmt
        self._step = step
        self._drag = False
        self._disabled = False
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.after_idle(self._redraw)

    # ----- 数值 -----
    def get(self) -> float:
        return self._v

    def set(self, v, notify=True) -> None:
        v = max(self.lo, min(self.hi, float(v)))
        if self._step:
            v = round(v / self._step) * self._step
        if v == self._v:
            return
        self._v = v
        self._redraw()
        if notify and callable(self._cmd):
            self._cmd(v)

    def set_state(self, state: str) -> None:
        """``disabled`` 的滑块不接受拖拽，整体变暗（调整对话框里"不适用"要用）。"""
        self._disabled = state == "disabled"
        self.configure(cursor="" if self._disabled else "hand2")
        if self._disabled:
            self._drag = False
        self._redraw()

    # ----- 交互 -----
    def _track(self):
        w = self.winfo_width()
        return 11.0, w - 11.0

    def _val_from_x(self, x):
        x0, x1 = self._track()
        r = (x - x0) / max(1.0, x1 - x0)
        return self.lo + r * (self.hi - self.lo)

    def _press(self, e):
        if self._disabled:
            return
        self._drag = True
        self.set(self._val_from_x(e.x))

    def _motion(self, e):
        if self._drag and not self._disabled:
            self.set(self._val_from_x(e.x))

    def _release(self, _e):
        self._drag = False

    # ----- 绘制 -----
    def _redraw(self):
        try:
            w, h = self.winfo_width(), self.winfo_height()
        except tk.TclError:
            return
        if w <= 1:
            self.after(16, self._redraw)
            return
        self.delete("all")
        cy = h / 2
        x0, x1 = self._track()
        r = (self._v - self.lo) / max(1e-9, self.hi - self.lo)
        px = x0 + r * (x1 - x0)

        on = not self._disabled
        track = BG_SUNKEN if on else "#15171c"
        done = ACCENT if on else "#334054"
        knob = FG_TEXT if on else FG_MUTED
        ha = 6.0
        round_rect(self, x0 - ha, cy - 2.5, x1 + ha, cy + 2.5, 2.5,
                   fill=track, outline="")
        if px > x0:
            round_rect(self, x0 - ha, cy - 2.5, px, cy + 2.5, 2.5,
                       fill=done, outline="")
        circle(self, px, cy, 8, fill=knob, outline=BG_APP, width=2)
        if self._fmt:
            self.create_text(x1 - 4, cy, text=self._fmt % self._v, anchor="e",
                             fill=FG_DIM, font=mono(9))


# --------------------------------------------------------------------------
# 圆角面板
# --------------------------------------------------------------------------
class RoundFrame(tk.Canvas):
    """圆角面板：Canvas 画底，真正的控件放进 ``.body``。

    tkinter 的子控件没有"透明"概念，所以圆角面板只能这样拼：
    底下是画了圆角的 Canvas，内层 Frame 用 create_window 贴上去。
    """

    def __init__(self, parent, *, radius=RADIUS_LG, fill=BG_ELEV, outline=STROKE,
                 pad=12, bg=None):
        self._outer_bg = bg or _parent_bg(parent)
        super().__init__(parent, highlightthickness=0, bd=0, bg=self._outer_bg)
        self._radius = radius
        self._fill = fill
        self._outline = outline
        self.body = tk.Frame(self, bg=fill)
        self._pad = pad
        self._win = self.create_window(pad, pad, window=self.body, anchor="nw")
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, _e=None):
        w, h = self.winfo_width(), self.winfo_height()
        self.delete("bg")
        round_rect(self, 1, 1, w - 1, h - 1, self._radius,
                   fill=self._fill, outline=self._outline, tags="bg")
        self.tag_lower("bg")
        self.itemconfigure(self._win, width=max(1, w - self._pad * 2),
                           height=max(1, h - self._pad * 2))


def _parent_bg(widget) -> str:
    """尽量取父控件的底色，避免圆角外露出白边。"""
    try:
        return widget.cget("bg")
    except tk.TclError:
        return BG_APP


# --------------------------------------------------------------------------
# 圆角输入框
# --------------------------------------------------------------------------
class RoundEntry(tk.Frame):
    """圆角输入框。

    ``tk.Entry`` 本身是方的、还带一圈 90 年代的凹陷立体边框，直接放进深色
    圆角界面里非常突兀。这里把 Entry **放进一个 Canvas**：Canvas 画圆角底，
    Entry 比它内缩 6px —— 正好等于圆角半径，于是它那对方角藏在圆角里面
    看不见（内缩量小于半径时方角会从圆弧外面露出来，这是个容易忽略的细节）。

    聚焦时描一圈强调色，这是"光标现在在这个框里"的唯一提示。
    """

    PAD = 6

    def __init__(self, parent, *, textvariable=None, width=92, height=30,
                 radius=RADIUS_SM, justify="center", font_size=11, bg=None,
                 placeholder="", on_change=None, on_enter=None, show=""):
        self._bg = bg or _parent_bg(parent)
        super().__init__(parent, width=width, height=height, bg=self._bg)
        self.pack_propagate(False)
        self._radius = radius
        self._state = "normal"
        self._focused = False
        self._placeholder = placeholder
        self._show_ph = False

        self.var = textvariable if textvariable is not None else tk.StringVar()
        self._cv = tk.Canvas(self, bg=self._bg, highlightthickness=0, bd=0)
        self._cv.pack(fill="both", expand=True)

        self.entry = tk.Entry(
            self._cv, textvariable=self.var, justify=justify,
            font=mono(font_size), bg=BG_SUNKEN, fg=FG_TEXT,
            insertbackground=FG_TEXT, relief="flat", bd=0, highlightthickness=0,
            disabledbackground=BG_SUNKEN, disabledforeground=FG_MUTED,
            selectbackground=ACCENT, selectforeground=FG_ON_ACCENT,
            show=show,
        )
        self._win = self._cv.create_window(0, 0, window=self.entry, anchor="nw")
        self._cv.bind("<Configure>", self._on_configure)
        self.entry.bind("<FocusIn>", self._on_focus, add="+")
        self.entry.bind("<FocusOut>", self._on_focus, add="+")
        self._cv.bind("<Button-1>", lambda _e: self.focus_set())
        if on_change is not None:
            self.var.trace_add("write", lambda *_: on_change())
        if on_enter is not None:
            self.entry.bind("<Return>", lambda _e: on_enter())
        if placeholder:
            self._placeholder_on()
        self.after_idle(self._redraw)

    # ----- 值 -----
    def get(self) -> str:
        """当前值。**占位提示不算值**（没填就是空串）。

        ★ 这里带一道**自愈**：占位状态是靠 `_show_ph` 这个标志记的，
          而只要有人绕过 `set()` 直接改 `var`（写测试、或以后加"程序填值"
          的功能时很容易这么写），标志就和变量对不上了。原来那种写法会
          **一律返回空串** —— 表现成"用户明明填了，程序却当没填"，**不报错**。
          现在只要发现"提示还在、但变量已经不是提示文字了"，就以变量为准、
          顺手把标志纠正过来（**绝不清空变量**，那是另一回事）。
        """
        if self._show_ph:
            cur = self.var.get()
            if cur != self._placeholder:
                self._show_ph = False
                try:
                    self.entry.configure(fg=FG_TEXT)
                except tk.TclError:
                    pass
                return cur
            return ""
        return self.var.get()

    def set(self, value) -> None:
        self._placeholder_off()
        self.var.set("" if value is None else str(value))

    def focus_set(self):
        try:
            self.entry.focus_set()
            self.entry.select_range(0, "end")
        except tk.TclError:
            pass

    def bind_entry(self, sequence, func):
        self.entry.bind(sequence, func, add="+")

    def set_state(self, state: str) -> None:
        self._state = "disabled" if state == "disabled" else "normal"
        try:
            self.entry.configure(state=self._state)
        except tk.TclError:
            pass
        self._redraw()

    # ----- 占位文字 -----
    #
    # ⚠⚠ 这个功能原来**是坏的**，而且坏得很安静（2026-09-26 拆掉）：
    #    提示文字是**当真值写进同一个变量**的（不是画上去的灰字），
    #    但**没有任何地方在聚焦/打字时把它清掉** —— 于是：
    #      ① 用户一点进去打字，就变成 "you@example.comabc"（拼在提示后面）；
    #      ② `get()` 又规定"`_show_ph` 为真时一律返回空串"，而打字并**不会**
    #         把它关掉 → **用户明明填了，程序却当没填**，而且一声不响。
    #    当时全项目没人用过这个参数，所以没人发现（这次是给反馈框加
    #    "回信邮箱"才撞上的）。
    #
    # 修法就是"把标准的占位语义补齐"：**一聚焦就清掉提示**（这样打字从空白
    # 开始），**失焦且仍为空就把提示放回去**。两件事都挂在焦点事件上，
    # 而不是靠调用方记得先调 `_placeholder_off()`。
    def _placeholder_on(self):
        if not self._placeholder:        # 没配提示就什么都不做
            return
        if self.var.get():
            return
        self._show_ph = True
        self.var.set(self._placeholder)
        self.entry.configure(fg=FG_MUTED)

    def _placeholder_off(self):
        if not self._show_ph:
            return
        self._show_ph = False
        self.var.set("")
        self.entry.configure(fg=FG_TEXT)

    # ----- 绘制 / 焦点 -----
    def _on_focus(self, _e=None):
        self._focused = self.entry is self.focus_get()
        # ★ 焦点一进来就把提示清掉：否则用户的第一个字符会拼在提示文字后面，
        #   而他看到的好像"还能编辑"—— 那是最容易让人困惑的一种表现。
        if self._focused:
            self._placeholder_off()
        elif not self.var.get():
            # 失焦且确实是空的 → 把提示放回去（纯界面行为，不影响取值）
            self._placeholder_on()
        self._redraw()

    def _on_configure(self, _e=None):
        w, h = self._cv.winfo_width(), self._cv.winfo_height()
        p = self.PAD
        self._cv.coords(self._win, p, p)
        self._cv.itemconfigure(self._win, width=max(1, w - 2 * p),
                               height=max(1, h - 2 * p))
        self._redraw()

    def _redraw(self):
        w, h = self._cv.winfo_width(), self._cv.winfo_height()
        if w <= 1 or h <= 1:
            self._cv.after(16, self._redraw)
            return
        self._cv.delete("bg")
        if self._state == "disabled":
            outline, fill = STROKE_SOFT, BG_SUNKEN
        elif self._focused:
            outline, fill = ACCENT, BG_SUNKEN
        else:
            outline, fill = STROKE, BG_SUNKEN
        round_rect(self._cv, 1, 1, w - 1, h - 1, self._radius,
                   fill=fill, outline=outline, width=1.4, tags="bg")
        self._cv.tag_lower("bg")


# --------------------------------------------------------------------------
# 分段控件
# --------------------------------------------------------------------------
class Segmented(tk.Canvas):
    """一排等宽圆角小按钮，只有一个选中（"按像素 / 按百分比"这类切换）。

    为什么不用 Radiobutton：一组 Radiobutton 在深色界面上永远带着浅色
    ``selectcolor`` 方块，那是 tkinter 最难消掉的一处"廉价感"。
    """

    PAD_X = 12

    def __init__(self, parent, options, *, value=None, command=None, height=30,
                 bg=None, font_size=9, radius=RADIUS_SM):
        self._bg = bg or _parent_bg(parent)
        self._segs = [(str(k), str(v)) for k, v in options]
        self._cmd = command
        self._font = font(font_size)
        self._radius = radius
        self._hover = None
        # 等宽：取最宽的那个标签
        tmp = tkfont.Font(font=self._font)
        seg = max(tmp.measure(lab) for _k, lab in self._segs) + self.PAD_X * 2
        self._seg = float(max(28, seg))
        w = int(self._seg * len(self._segs)) + 4
        super().__init__(parent, width=w, height=height, highlightthickness=0,
                         bd=0, bg=self._bg, takefocus=0)
        self._value = self._segs[0][0] if value is None else str(value)
        self._enabled = True
        self.bind("<Button-1>", self._on_click)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", lambda _e: self._set_hover(None))
        self.after_idle(self._redraw)

    def get(self) -> str:
        return self._value

    def set_enabled(self, enabled: bool) -> None:
        """禁用之后不响应点击，整体变暗（"当前模式用不到这组选项"）。"""
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if self._enabled else "")
        self._redraw()

    def set(self, key, notify=False) -> None:
        key = str(key)
        if key not in [k for k, _ in self._segs]:
            return
        if key == self._value:
            return
        self._value = key
        self._redraw()
        if notify and callable(self._cmd):
            self._cmd(key)

    def set_options(self, options, value=None) -> None:
        self._segs = [(str(k), str(v)) for k, v in options]
        self._value = str(value) if value is not None else self._segs[0][0]
        tmp = tkfont.Font(font=self._font)
        self._seg = float(max(28, max(tmp.measure(lab) for _k, lab in self._segs)
                              + self.PAD_X * 2))
        self.configure(width=int(self._seg * len(self._segs)) + 4)
        self._redraw()

    # ----- 交互 -----
    def _index_at(self, x):
        i = int((x - 2) // self._seg)
        return max(0, min(len(self._segs) - 1, i))

    def _on_click(self, event):
        if not self._enabled:
            return
        self.set(self._segs[self._index_at(event.x)][0], notify=True)

    def _on_motion(self, event):
        if not self._enabled:
            return
        self._set_hover(self._index_at(event.x))

    def _set_hover(self, idx):
        if idx == self._hover:
            return
        self._hover = idx
        self._redraw()

    # ----- 绘制 -----
    def _redraw(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            self.after(16, self._redraw)
            return
        self.delete("all")
        on = self._enabled
        round_rect(self, 1, 1, w - 1, h - 1, self._radius,
                   fill=BG_SUNKEN if on else "#15171c",
                   outline=STROKE if on else STROKE_SOFT)
        for i, (key, label) in enumerate(self._segs):
            x0 = 2 + i * self._seg
            x1 = x0 + self._seg
            sel = key == self._value
            if not on:
                fg = FG_MUTED
                if sel:
                    round_rect(self, x0 + 1, 3, x1 - 1, h - 3,
                               max(2, self._radius - 2), fill="#262a33", outline="")
            elif sel:
                round_rect(self, x0 + 1, 3, x1 - 1, h - 3, max(2, self._radius - 2),
                           fill=ACCENT, outline="")
                fg = FG_ON_ACCENT
            elif i == self._hover:
                round_rect(self, x0 + 1, 3, x1 - 1, h - 3, max(2, self._radius - 2),
                           fill=BG_HOVER, outline="")
                fg = FG_TEXT
            else:
                fg = FG_DIM
            self.create_text((x0 + x1) / 2, h / 2, text=label, fill=fg,
                             font=self._font)


# --------------------------------------------------------------------------
# 自绘勾选 / 单选行
# --------------------------------------------------------------------------
class _MarkRow(tk.Frame):
    """勾选行的公共部分：一个 20×20 的自绘标记 + 一行文字。整行可点。

    ``value`` 是"初始是否选中"的便捷写法（内部仍然是 BooleanVar）——
    自己建一堆 ``BooleanVar`` 再传进来太啰嗦，而单选组在这里是"每行一个
    布尔量、一次只有一个为真"，用一个变量管一行反而更好写。
    """

    MARK = 20

    def __init__(self, parent, text, *, variable=None, value=None, command=None,
                 bg=None, font_size=9, kind="check"):
        self._bg = bg or _parent_bg(parent)
        super().__init__(parent, bg=self._bg)
        self._kind = kind
        self._cmd = command
        self._font = font(font_size)
        self._enabled = True
        self._hover = False
        if variable is None:
            variable = tk.BooleanVar(value=bool(value) if value is not None else False)
        self.var = variable
        self._trace = self.var.trace_add("write", lambda *_: self._redraw())

        self._cv = tk.Canvas(self, width=self.MARK, height=self.MARK,
                             bg=self._bg, highlightthickness=0, bd=0)
        self._cv.pack(side="left", pady=1)
        self.label = tk.Label(self, text=text, bg=self._bg, fg=FG_TEXT,
                              font=self._font, anchor="w", cursor="hand2")
        self.label.pack(side="left", padx=(7, 0), fill="x", expand=True)
        for w in (self._cv, self.label):
            w.bind("<Button-1>", self._on_click)
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
        self.after_idle(self._redraw)

    def get(self) -> bool:
        return bool(self.var.get())

    def set(self, value, notify=False) -> None:
        self.var.set(bool(value))
        if notify and callable(self._cmd):
            self._cmd(self.get())

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        fg = FG_TEXT if self._enabled else FG_MUTED
        self.label.configure(fg=fg,
                             cursor="hand2" if self._enabled else "")
        self._redraw()

    def set_text(self, text: str) -> None:
        self.label.configure(text=text)

    def _on_click(self, _e=None):
        if not self._enabled:
            return
        self.var.set(not self.var.get())
        if callable(self._cmd):
            self._cmd(self.get())

    def _on_enter(self, _e=None):
        self._hover = self._enabled
        self._redraw()

    def _on_leave(self, _e=None):
        self._hover = False
        self._redraw()

    def _redraw(self):
        s = self.MARK
        self._cv.delete("all")
        on = self.get()
        if not self._enabled:
            base, stroke = BG_SUNKEN, STROKE_SOFT
        elif on:
            base, stroke = ACCENT, ACCENT
        elif self._hover:
            base, stroke = BG_HOVER, FG_DIM
        else:
            base, stroke = BG_SUNKEN, STROKE
        if self._kind == "radio":
            self._cv.create_oval(1, 1, s - 1, s - 1, fill=base, outline=stroke,
                                 width=1.5)
            if on:
                r = s * 0.26
                self._cv.create_oval(s / 2 - r, s / 2 - r, s / 2 + r, s / 2 + r,
                                     fill=FG_ON_ACCENT, outline="")
        else:
            round_rect(self._cv, 1, 1, s - 1, s - 1, 5, fill=base, outline=stroke,
                       width=1.5)
            if on:
                draw_icon(self._cv, "check", s / 2, s / 2, s * 0.30,
                          FG_ON_ACCENT, 2.2)


class CheckRow(_MarkRow):
    """自绘复选框。tk.Checkbutton 的指示器是个经典灰色方块，和圆角界面不搭。"""

    def __init__(self, parent, text, **kw):
        kw.pop("kind", None)
        super().__init__(parent, text, kind="check", **kw)


class RadioRow(_MarkRow):
    """自绘单选框。"""

    def __init__(self, parent, text, **kw):
        kw.pop("kind", None)
        super().__init__(parent, text, kind="radio", **kw)


# --------------------------------------------------------------------------
# 一行滑块（标签 + 滑块 + 数值）
# --------------------------------------------------------------------------
class SliderRow(tk.Frame):
    """``亮度   ──●────   100%`` 这样的一行。双击标签复位。

    内部用 ``grid`` 而不是 ``pack``：标签列用 ``minsize``（**像素**）对齐，
    这样几行的滑块起止位置严丝合缝。用 ``Label(width=N)`` 去对齐是不行的 ——
    那个 N 的单位是"字符数"，中文标签会被截断（"对比度"变成"对比…"）。
    """

    def __init__(self, parent, label, lo, hi, *, value, command=None, fmt="%d",
                 suffix="", reset_value=None, width=200, bg=None, font_size=9,
                 label_px=60):
        self._bg = bg or _parent_bg(parent)
        super().__init__(parent, bg=self._bg)
        self._reset = value if reset_value is None else reset_value
        self._cmd = command
        self._suffix = suffix
        self._fmt = fmt
        self.label = tk.Label(self, text=label, bg=self._bg, fg=FG_TEXT,
                              font=font(font_size), anchor="w", cursor="hand2")
        self.label.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.value_label = tk.Label(self, text="", bg=self._bg, fg=FG_DIM,
                                    font=mono(font_size), anchor="e", width=5)
        self.value_label.grid(row=0, column=2, sticky="e", padx=(8, 0))
        self.slider = Slider(self, lo, hi, value=value, command=self._on_slide,
                             width=width, bg=self._bg, fmt=None)
        self.slider.grid(row=0, column=1, sticky="ew")
        self.columnconfigure(0, minsize=label_px)
        self.columnconfigure(1, weight=1)
        self.label.bind("<Double-Button-1>", lambda _e: self.reset())
        self._on_slide(self.slider.get())

    def _on_slide(self, v):
        self.value_label.configure(text=(self._fmt % v) + self._suffix)
        if callable(self._cmd):
            self._cmd(v)

    def set_command(self, command):
        self._cmd = command

    def get(self):
        return self.slider.get()

    def set(self, v, notify=False):
        self.slider.set(v, notify=False)
        self._on_slide(self.slider.get())
        if notify and callable(self._cmd):
            self._cmd(self.slider.get())

    def reset(self):
        self.set(self._reset, notify=True)

    def set_enabled(self, enabled: bool):
        enabled = bool(enabled)
        self.label.configure(fg=FG_TEXT if enabled else FG_MUTED)
        self.value_label.configure(fg=FG_DIM if enabled else FG_MUTED)
        self.slider.set_state("normal" if enabled else "disabled")


# --------------------------------------------------------------------------
# 窗口级：深色标题栏
# --------------------------------------------------------------------------
def dark_titlebar(window) -> bool:
    """把窗口标题栏也刷成深色（Win10 1809+ 的沉浸式暗色标题栏）。

    没有这一步，深色界面顶着一条雪白/浅灰的标题栏，观感就"廉价"了 ——
    这是全程序最便宜、效果最明显的一处质感提升。

    用 ctypes 调 dwmapi（不是 winreg，不写注册表）。失败就算了，
    标题栏颜色不该影响任何功能。
    """
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        if not hwnd:
            hwnd = window.winfo_id()
        val = ctypes.c_int(1)
        for attr in (20, 19):        # DWMWA_USE_IMMERSIVE_DARK_MODE 新旧编号
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                return True
        # Win10 早期版本没有这个属性，退而求其次：把非客户区也刷深
        for attr, v in ((35, 0x00141414), (36, 0x00eceef2)):   # 边框 / 标题文字
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(ctypes.c_uint(v)), 4)
        return False
    except Exception:                # noqa: BLE001 - 纯装饰，绝不因它崩
        log.debug("设置深色标题栏失败（不影响使用）", exc_info=True)
        return False
