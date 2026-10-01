# -*- coding: utf-8 -*-
"""主程序界面里被多个页面共用的一点点东西：配色、窗口下限、窗口自适应。

为什么单独一个文件
==================================================================
问答页、锁定页、图片工具页分居 main.py 与 image_tool.py，但它们必须
**长得像同一个程序**（同底色、同字号风格、同一种"窗口按内容实测放大"的行为）。
把这些常量抄两份，第一次改配色就会只改一半 —— 那是本项目已经踩过的
同一种坑（版本号抄五处、资源清单抄两份）。所以只留一处。

``fit_window`` 为什么必须存在（这不是洁癖，是拿事故换来的）
==================================================================
tkinter 的字体尺寸随系统显示缩放变化。任何"把窗口尺寸写死 + 禁止缩放"
的做法，在 125% / 150% 显示缩放下都会把底部控件挤出可视区 ——
用户看到的是一个被切掉一半的按钮，而且没法把窗口拉大去看全。
所以这里不猜尺寸：让 Tk 先算完布局，读出真实需求尺寸，再据此设窗口大小
（只增不减，避免页面切换时窗口抖一下）。
"""
from __future__ import annotations

import logging
import tkinter as tk

log = logging.getLogger("haavik.ui")

#: 主程序统一底色。深色是为了让整蛊页的"危险感"成立，
#: 图片工具页也沿用同一套，免得切页面时像两个不同的软件。
BG_DARK = "#14161c"
BG_PANEL = "#1e212a"
BG_VIEW = "#0b0c10"
BG_ALERT = "#1a1a1a"
FG_TEXT = "#e8e8e8"
FG_DIM = "#7f8c8d"
FG_WARN = "#f1c40f"
FG_BAD = "#e74c3c"
FG_OK = "#2ecc71"
ACCENT = "#3498db"

#: 窗口布局下限。实际尺寸由 fit_window() 按页面内容实测决定。
MIN_WINDOW_W = 900
MIN_WINDOW_H = 700

#: 图片预览区上限（像素）。用固定像素的容器而不是 Label 的 width/height ——
#: 后者的单位是"字符数/文本行数"，会随显示缩放一起放大，150% 下能把窗口撑到屏幕外。
FIT_W, FIT_H = 720, 460


def fit_window(root: tk.Tk, min_w: int, min_h: int) -> tuple[int, int]:
    """按当前页面的**实测**需求调整窗口。返回调整后的 (min_w, min_h)。

    调用方负责保存返回值（下一页从这里继续只增不减，窗口才不会越切越小）。
    """
    try:
        root.update_idletasks()
        need_w = max(min_w, root.winfo_reqwidth())
        need_h = max(min_h, root.winfo_reqheight())
        if need_w != min_w or need_h != min_h:
            min_w, min_h = need_w, need_h
            root.minsize(need_w, need_h)
            log.debug("窗口需求尺寸调整为 %dx%d", need_w, need_h)

        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(min_w, sw), min(min_h, max(400, sh - 80))
        root.geometry("%dx%d+%d+%d"
                      % (w, h, max(0, (sw - w) // 2), max(0, (sh - h) // 3)))
    except tk.TclError:  # 窗口已销毁
        pass
    return min_w, min_h
