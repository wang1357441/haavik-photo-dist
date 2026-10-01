# -*- coding: utf-8 -*-
"""图片工具主界面 —— "像 Windows 照片那样"的看图 + 编辑器。

定位（这条从旧版继承下来，一字未改其含义）
==================================================================
这个程序原来只是个"假的看图器"——图片是布景，整蛊才是正片。
现在它同时是**一个真能用的图片工具**：改了尺寸、转了格式，存出来的
就是你想要的那张图。一个"什么都不干"的 exe 是最容易被杀软和
SmartScreen 侧目的形态，而一个"打开就能用、有正经用途"的程序
才是它该有的样子。

必须守住的硬约束（serial_test 在盯着，一条都不能松）
==================================================================
  1. ``suggested_name`` **绝不返回原文件名** —— "改完存回原位"是这类
     软件最常见的真实事故，所以默认名一律带 ``_改`` 后缀；
  2. ``save_as`` 选中**原路径**时必须再确认一次，且默认按钮是「否」；
     用户拒绝时一个字节都不许写（测试拿 sha256 验过）。

分层（每层各管一件事，任何一层都不超过 ~1500 行）::
  * ``ui_theme``      主题 + 自绘圆角控件（无第三方 UI 库）
  * ``image_ops``     图像纯逻辑（不 import tkinter，可无窗口测试）
  * ``shell_ops``     系统集成（剪贴板 / 壁纸 / 回收站 / 打印 / 资源管理器）
  * ``image_view``    Canvas 查看器（缩放 / 平移 / 棋盘格 / 动图）
  * ``image_dialogs`` 四个圆角对话框（改尺寸 / 转格式 / 裁剪 / 调整）
  * ``image_tool``    本文件：把上面的东西串成一个界面

界面结构::

    ┌──────────────────────────────────────────────┐
    │ 工具栏（打开 ‹› 保存 │ 缩放组 │ 编辑组 │ 工具组）│
    ├───────────────────────────────────────┬──────┤
    │                                       │ 信息 │
    │              查看器                    │ 面板 │
    │                                       │(可折)│
    ├───────────────────────────────────────┴──────┤
    │ 胶片条（同文件夹缩略图，分帧渲染）              │
    ├──────────────────────────────────────────────┤
    │ 状态栏：文件名* · 序号              缩放% · 消息│
    └──────────────────────────────────────────────┘
"""
from __future__ import annotations

import logging
import os
import tkinter as tk
from tkinter import filedialog, messagebox

import ai_tool
import image_dialogs as dialogs
import image_ops as ops
import image_view
import shell_ops
import ai_ops
from ui_common import (BG_DARK, BG_PANEL, BG_VIEW, FG_DIM, FG_TEXT,
                       MIN_WINDOW_H, MIN_WINDOW_W, fit_window)
from ui_theme import BG_ELEV, FG_MUTED, FG_WARN, IconButton, font, round_rect

try:
    from PIL import Image, ImageTk
except ImportError:  # 没有 Pillow 时整个工具不可用，界面要明确说出来而不是崩掉
    Image = None          # type: ignore[assignment]
    ImageTk = None        # type: ignore[assignment]

log = logging.getLogger("haavik.image")

APP_TITLE = "HAAVIK Photo"

# ---- 这些名字是 serial_test 的契约，名字与语义都不能动 ----
FORMATS = ops.FORMATS
FMT_EXT = ops.FMT_EXT
ALPHA_OK = ops.ALPHA_OK

#: 撤销 / 重做栈上限。每格都是一整张图，再多内存就不可控了。
HISTORY_MAX = 25

#: 幻灯片换片间隔（毫秒）。
SLIDESHOW_MS = 3000

#: 胶片条缩略图高度（画布像素）。
THUMB_H = 56
THUMB_PAD = 5

#: 信息面板宽度。
INFO_W = 280

#: 胶片条一次最多渲染多少张缩略图（再多滚动就卡了）。
THUMB_LIMIT = 200

# ---- 左下角那个小白点：重新开始的隐藏入口 --------------------------------
#: 连点多少下才弹口令框。
DOT_CLICKS = 10
#: 两次点击间隔超过这个毫秒数就重新计数（否则"随便多戳几下"也会撞上）。
DOT_RESET_MS = 1200
#: 口令。**这不是安全边界**：它只是个"我确认我知道自己在做什么"的闸门，
#: 和序列号一样是给人用的，不是用来防人的（知道口令的人可以把程序重启到
#: 出题那一步，仅此而已 —— 不写任何文件、不改任何东西）。
RESTART_PASSWORD = "0228"


# ==========================================================================
# 小零件
# ==========================================================================
class _Sep(tk.Canvas):
    """工具栏里的一根竖分隔线（把功能分组隔开）。"""

    def __init__(self, parent, color="#333742", height=22):
        super().__init__(parent, width=1, height=height, bg=color,
                         highlightthickness=0, bd=0)


class ImageTool(ai_tool.AiToolMixin, tk.Frame):
    """图片工具页。``main.py`` 把它当一页塞进主窗口。

    AI 修图那一段（按钮/下拉菜单/能力跑批/模型下载/抠图弹窗）在
    :class:`ai_tool.AiToolMixin` 里 —— 它们是本类的方法（用 ``self`` 上的
    界面状态），拆出去只是为了把这个文件压在 1500 行以内。
    """

    def __init__(self, parent, on_quit=None, on_check_update=None,
                 on_restart=None, on_feedback=None, on_download_ai_models=None,
                 on_install_ai_pack_local=None, on_check_ai_update=None):
        super().__init__(parent, bg=BG_DARK)
        self.on_quit = on_quit
        #: 右下角「检查更新」被点时走这里。联网与下载都在 main.py，
        #: 本模块不 import version_check —— "界面不联网"这条分界保持不动。
        self.on_check_update = on_check_update
        #: 左下角白点 + 口令通过后走这里（main.py 负责"清过闸记录 + 重启进程"）。
        #: 同样只回调：本模块不碰进程、不写盘。
        self.on_restart = on_restart
        #: 「反馈」按钮走这里。收件地址、邮件程序、剪贴板都在 main.py 那一层 ——
        #: 本模块既不知道地址，也不会自己发东西。
        self.on_feedback = on_feedback
        #: 「AI 修图」要下载模型时走这里。真实的联网 / 下载 / 解包 / 写状态都在
        #: main.py（它已经有现成的后台线程与进度窗口），本模块只管"要"和"用"，
        #: 不碰网络、不写盘 —— "界面不联网"这条分界保持不动。
        self.on_download_ai_models = on_download_ai_models
        #: 「从本地文件安装 AI 模型包」走这里。与 on_download_ai_models 对称：
        #   下载走网络、本地安装吃用户自己选好的 .pack 文件，两者都只是把包交给
        #   main.py 去解包/校验/写状态，本模块不碰网络、不写盘。
        #   没接（老版本）时按钮不显示，点了也不会崩。
        self.on_install_ai_pack_local = on_install_ai_pack_local
        #: 「重新下载 / 更新 AI 模型」被点时，先走这里联网查一下有没有新版本
        #: （不下载）。main.py 负责联网与版本比较，本模块只拿到结果再决定怎么问。
        self.on_check_ai_update = on_check_ai_update
        self._ai_button = None
        # 注意：这里**不再有** ``_ai_popup`` / ``_ai_outer_bound``。
        # AI 菜单已经回退成系统原生 ``tk.Menu``（见 ai_tool._ai_toggle_menu），
        # "开 / 关 / 点外面收起 / Esc 收起"全由 Tk 自己管 —— 我们这边不留任何
        # 弹层状态，也就不存在"状态残留导致点了没反应"这条路。
        self._update_latest = None
        self._dot_count = 0
        self._dot_job = None

        # ---- 状态 ----
        self.image = None          # 当前工作图（serial_test 直接读写它）
        self._original = None      # 本次打开的原图（revert 的落点）
        self._path = None          # 当前文件来源路径
        self.last_saved = None     # 最近一次成功写盘的路径
        self._exif_bytes = None
        self._undo_stack = []      # [(label, image), ...]
        self._redo_stack = []
        self._files = []           # 当前文件夹的图片清单（自然序）
        self._pos = -1
        self._slideshow = False
        self._slide_job = None
        self._fullscreen = False
        self._info_on = False
        self._quality = ops.DEFAULT_QUALITY.get("JPEG", 92)
        self._keep_exif = True
        self._thumbs = {}          # path -> PhotoImage（保引用防 gc）
        self._thumb_pos = {}       # path -> (x0, x1)
        self._thumb_pending = []   # 还没渲染完的缩略图队列
        self._thumb_job = None
        self._flash_job = None
        self._min_w, self._min_h = MIN_WINDOW_W, MIN_WINDOW_H
        self._edit_buttons = {}

        # ---- 布局 ----
        # 「有新版本」提示行：先建好留空，查到新版本再由 main.py 灌文字 ——
        # 不在异步回调里动态 pack 新控件，避免窗口尺寸在用户眼皮底下跳一下。
        notice = tk.Frame(self, bg=BG_DARK)
        notice.pack(fill="x")
        self._notice = notice
        self.update_label = tk.Label(notice, text="", bg=BG_DARK, fg=FG_WARN,
                                     font=font(9), anchor="w")
        self.update_label.pack(side="left", padx=(10, 0))
        self._build_toolbar()
        mid = tk.Frame(self, bg=BG_DARK)
        mid.pack(fill="both", expand=True)
        self._mid = mid
        self._build_info_panel(mid)
        self._build_viewer(mid)
        self._build_filmstrip()
        self._build_statusbar()

        self._bind_keys()
        self._update_enabled(False)
        self._show_status("打开一张图开始（Ctrl+O）")

    # ==================================================================
    # 界面搭建
    # ==================================================================
    def build(self):
        """``main.py`` 在构造之后调用：把整页 pack 进主窗口并收尾。

        布局在 ``__init__`` 就已搭好（serial_test 不经过这里也能直接用），
        所以这里只做"上墙 + 一次性收尾"，重复调用是安全的空转。
        """
        if getattr(self, "_built", False):
            return
        self._built = True
        self.pack(fill="both", expand=True)
        self._show_status("打开一张图开始（Ctrl+O）")

    def _fit_window(self):
        """按实测需求调整窗口（main.py 换页时统一入口）。

        调用进来的时候这个页面可能已经被销毁了（main 换页后 ``app.mode``
        会残留一拍），所以先确认自己还活着 —— 死控件的 winfo 一律 TclError。
        """
        try:
            if not self.winfo_exists():
                return self._min_w, self._min_h
        except tk.TclError:
            return self._min_w, self._min_h
        self._min_w, self._min_h = fit_window(
            self.winfo_toplevel(), self._min_w, self._min_h)
        return self._min_w, self._min_h

    def _status(self, text):
        """main.py 的更新通道往状态栏写一句话的入口。"""
        self._show_status(text)

    def _build_toolbar(self):
        bar = tk.Frame(self, bg=BG_PANEL)
        bar.pack(fill="x")
        self._bar = bar

        def btn(icon, tip, cmd, group=None):
            b = IconButton(bar, icon, cmd, size=30, tooltip=tip, bg=BG_PANEL)
            b.pack(side="left", padx=1, pady=3)
            if group:
                self._edit_buttons.setdefault(group, []).append(b)
            return b

        # 左：文件与导航
        btn("open", "打开图片（Ctrl+O）", self.open_file)
        btn("prev", "上一张（←）", lambda: self.goto(-1), "nav")
        btn("next", "下一张（→）", lambda: self.goto(1), "nav")
        btn("save", "另存为（Ctrl+S）", self._show_save)
        btn("print", "打印（Ctrl+P）", self.print_current, "print")
        self._sep()
        # 查看
        btn("zoom_out", "缩小（-）", lambda: self._viewer.zoom_out(), "view")
        btn("fit", "适应窗口（F）", lambda: self._viewer.fit(), "view")
        btn("actual", "原始大小 1:1", lambda: self._viewer.actual(), "view")
        btn("zoom_in", "放大（+）", lambda: self._viewer.zoom_in(), "view")
        self._sep()
        # 编辑
        btn("rotate_l", "逆时针旋转", lambda: self._edit_ccw(), "rot")
        btn("rotate_r", "顺时针旋转", lambda: self._edit(ops.rotate_cw, "旋转"), "rot")
        btn("flip_h", "水平翻转", lambda: self._edit(ops.flip_h, "翻转"), "rot")
        btn("flip_v", "垂直翻转", lambda: self._edit(ops.flip_v, "翻转"), "rot")
        btn("undo", "撤销（Ctrl+Z）", self.undo, "hist")
        btn("redo", "重做（Ctrl+Y）", self.redo, "hist")
        self._sep()
        # 四个工具对话框
        btn("crop", "裁剪", self._show_crop)
        btn("adjust", "调整（亮度 / 对比度 / 饱和度 / 锐度）", self._show_adjust)
        btn("layers", "叠加图片（把另一张图贴到这张上）", self._show_overlay)
        btn("resize", "修改尺寸", self._show_resize)
        btn("convert", "转换格式", self._show_convert)
        self._sep()
        # AI 修图分组：仅在本机能跑（Windows 10/11 64 位）时显示。
        # 老系统（Win7/XP、32 位）不显示 —— 那里既没模型也不该装。
        if ai_ops.ai_available():
            # ⚠ 这里**故意不传 group**。传了它就会进 self._edit_buttons，
            # 而 _update_enabled 把"非 nav / 非 hist"的组一律按"有没有打开
            # 图片"来启用/禁用 —— 于是**没打开图片时「AI」是 disabled 的，
            # 点它完全不执行命令**（RoundButton 在 disabled 下 _on_release
            # 直接 return，连日志都没有）。用户看到的就是"点 AI 压根不弹菜单"。
            # 「AI」是个菜单入口，不是"对当前图片的编辑动作"，不该跟着图片走。
            # （2026-09-21 修：用户报的正是这个。）
            #
            # ⚠ 也**故意不挂 hover**：中间试过"悬停自动展开/移开自动收起"，
            # 那套要按时延回看指针位置才能做对，机制多、在别人机器上容易变成
            # "点了没反应"。2026-09-21 按用户要求回退成纯点击（见 ai_tool.py）。
            self._ai_button = btn("ai", "AI 修图（点击打开菜单）", self._ai_toggle_menu)
        self._sep()
        btn("info", "文件信息（Ctrl+I）", self._toggle_info)
        btn("mail", "反馈（写给我，用你的邮件程序发送）", self._show_feedback)
        btn("play", "幻灯片（F5）", self._toggle_slideshow, "nav")
        btn("fullscreen", "全屏（F11 / Esc）", self._toggle_fullscreen)

        # 右端：删除（先放一根弹性空条把 trash 顶到最右）
        tk.Frame(bar, bg=BG_PANEL).pack(side="left", fill="x", expand=True)
        self._btn_trash = btn("trash", "删除到回收站（Del）", self.delete_current, "file")
        self._btn_trash.pack_forget()
        self._btn_trash.pack(side="right", padx=(1, 8), pady=3)

    def _sep(self):
        _Sep(self._bar).pack(side="left", padx=5, pady=5)

    def _build_viewer(self, mid):
        holder = tk.Frame(mid, bg=BG_VIEW)
        holder.pack(side="left", fill="both", expand=True)
        self._viewer = image_view.ImageViewer(
            holder, bg=BG_VIEW,
            on_zoom=self._on_zoom_changed,
            on_state=self._on_has_image)
        self._viewer.pack(fill="both", expand=True)
        self._viewer.bind("<Button-3>", lambda _e: self._popup_menu())

    def _build_info_panel(self, mid):
        self._info = tk.Frame(mid, bg=BG_PANEL, width=INFO_W)
        # 初始收起（_toggle_info 负责展开）；before=viewer 保证它贴在右侧
        self._info_body = tk.Frame(self._info, bg=BG_PANEL)
        self._info_body.pack(fill="both", expand=True)

    def _build_filmstrip(self):
        self._strip = tk.Canvas(self, height=THUMB_H + THUMB_PAD * 2 + 6,
                                bg=BG_PANEL, highlightthickness=0, bd=0)
        self._strip.pack(fill="x")
        self._strip.bind("<Button-1>", self._on_strip_click)
        self._strip.bind("<MouseWheel>", self._on_strip_wheel)
        self._strip.bind("<Button-4>", lambda _e: self._strip.xview_scroll(-2, "units"))
        self._strip.bind("<Button-5>", lambda _e: self._strip.xview_scroll(2, "units"))

    def _build_statusbar(self):
        bar = tk.Frame(self, bg=BG_PANEL, height=26)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self._statusbar = bar

        # 左下角的小白点：重新开始的隐藏入口（连点 DOT_CLICKS 下弹口令框）。
        # 做成 6px 的浅灰点、没有提示、没有光标变化 —— 它属于"知道的人才
        # 找得到"的东西，但也不藏：就在状态栏最左边，看起来像个装饰点。
        self._dot = tk.Canvas(bar, width=16, height=16, bg=BG_PANEL,
                              highlightthickness=0, bd=0)
        self._dot.pack(side="left", padx=(8, 0))
        self._dot.create_oval(5, 5, 11, 11, fill="#d0d4da", outline="",
                              tags=("dot",))
        self._dot.bind("<Button-1>", self._on_dot_click)

        self._st_name = tk.Label(bar, text="", bg=BG_PANEL, fg=FG_TEXT,
                                 font=font(9), anchor="w")
        self._st_name.pack(side="left", padx=(10, 4))
        self._st_pos = tk.Label(bar, text="", bg=BG_PANEL, fg=FG_DIM,
                                font=font(9), anchor="w")
        self._st_pos.pack(side="left")
        self._st_zoom = tk.Label(bar, text="", bg=BG_PANEL, fg=FG_DIM,
                                 font=font(9), anchor="e")
        self._st_zoom.pack(side="right", padx=(0, 4))

        # 「校验 AI 完整性」：本地比对已装模型文件，不联网。放在「检查更新」
        # 左边，两个都是右下角的小字入口（纯文字、无底色，和检查更新同款）。
        self._verify_ai_btn = tk.Label(bar, text="校验 AI 完整性", bg=BG_PANEL,
                                       fg=FG_DIM, font=font(9), cursor="hand2")
        self._verify_ai_btn.pack(side="right", padx=(0, 10))
        self._verify_ai_btn.bind("<Button-1>",
                                 lambda _e: self._on_verify_ai_click())
        self._verify_ai_btn.bind(
            "<Enter>", lambda _e: self._verify_ai_btn.configure(fg=FG_TEXT))
        self._verify_ai_btn.bind(
            "<Leave>", lambda _e: self._verify_ai_btn.configure(fg=FG_DIM))

        # 右下角「检查更新」：**纯文字、无边框、无底色**。
        # 刻意不做成圆角按钮：状态栏只有 26px 高，塞一个带底色的控件
        # 会把整条状态栏压得很重；hover 提亮一档是它唯一的"可点"提示，
        # 和界面里其它 hover 行为一致。
        # 查到新版本时它会变成「下载更新 x.y.z」并转为警示色 ——
        # 只有一种按钮、两种含义，字面永远是"下一步该做什么"。
        self._update_btn = tk.Label(bar, text="检查更新", bg=BG_PANEL,
                                    fg=FG_DIM, font=font(9), cursor="hand2")
        self._update_btn.pack(side="right", padx=(0, 10))
        self._update_btn.bind("<Button-1>", lambda _e: self._on_update_click())
        self._update_btn.bind("<Enter>", lambda _e: self._hover_update(True))
        self._update_btn.bind("<Leave>", lambda _e: self._hover_update(False))

        self._st_msg = tk.Label(bar, text="", bg=BG_PANEL, fg=FG_DIM,
                                font=font(9), anchor="e")
        self._st_msg.pack(side="right", padx=(4, 4))

    # ==================================================================
    # 快捷键
    # ==================================================================
    def _bind_keys(self):
        top = self.winfo_toplevel()

        def k(seq, fn):
            # 绑在主窗口上，所以换页之后旧的绑定还在。页面销毁后这些
            # 快捷键必须安静地失效，而不是对着死控件抛 TclError。
            def run(_e=None):
                try:
                    if not self.winfo_exists():
                        return
                except tk.TclError:
                    return
                fn()
            top.bind(seq, run, add="+")

        k("<Control-o>", self.open_file)
        k("<Left>", lambda: self.goto(-1))
        k("<Right>", lambda: self.goto(1))
        k("<plus>", lambda: self._viewer.zoom_in())
        k("<equal>", lambda: self._viewer.zoom_in())
        k("<KP_Add>", lambda: self._viewer.zoom_in())
        k("<minus>", lambda: self._viewer.zoom_out())
        k("<KP_Subtract>", lambda: self._viewer.zoom_out())
        k("<Key-f>", lambda: self._viewer.fit())
        k("<Key-F>", lambda: self._viewer.fit())
        k("<Key-1>", lambda: self._viewer.actual())
        k("<Control-z>", self.undo)
        k("<Control-y>", self.redo)
        k("<Control-Z>", self.redo)
        k("<Control-s>", self._show_save)
        k("<Control-r>", self.revert)
        k("<Control-p>", self.print_current)
        k("<Control-P>", self.print_current)
        k("<Control-i>", self._toggle_info)
        k("<Delete>", self.delete_current)
        k("<F5>", self._toggle_slideshow)
        k("<F11>", self._toggle_fullscreen)
        k("<Escape>", self._on_escape)

    def _on_escape(self):
        if self._slideshow:
            self._toggle_slideshow()
        elif self._fullscreen:
            self._toggle_fullscreen()

    # ==================================================================
    # 打开 / 文件夹导航
    # ==================================================================
    def open_file(self):
        path = filedialog.askopenfilename(
            title="打开图片", parent=self, filetypes=ops.open_types())
        if path:
            self.load_file(path)

    def open_folder(self):
        folder = filedialog.askdirectory(title="打开文件夹", parent=self)
        if not folder:
            return
        self._set_folder(folder, prefer=None)
        if self._files:
            self._load_path(self._files[0])
        else:
            self._show_status("这个文件夹里没有图片")

    def _set_folder(self, folder, prefer=None):
        self._files = [os.path.abspath(p)
                       for p in shell_ops.list_images(folder or ".")]
        self._pos = -1
        if prefer:
            try:
                self._pos = self._files.index(os.path.abspath(prefer))
            except ValueError:
                self._pos = -1
        self._render_thumbs()

    def has_image(self):
        """当前有没有在工作图上。给 ``main.py`` 判断"要不要放示例图"用。

        别让外面直接读 ``.image``：那是"当前工作图"，将来加图层/多文档时
        语义会变；这里给的是一个稳定的"空 / 非空"问答。
        """
        return self.image is not None

    def load_file(self, path):
        """打开一个图片文件。成功 True；失败弹错并返回 False（不抛异常）。"""
        loaded = ops.load_image(path)
        if not loaded.ok or loaded.image is None:
            messagebox.showerror(APP_TITLE,
                                 loaded.error or "打不开这个文件。", parent=self)
            return False
        folder = os.path.dirname(os.path.abspath(path))
        if folder != self._strip_folder():
            self._set_folder(folder, prefer=path)
        elif os.path.abspath(path) in self._files:
            self._pos = self._files.index(os.path.abspath(path))
        self._load(loaded)
        self._highlight_thumb()
        return True

    def _strip_folder(self):
        return os.path.dirname(self._files[0]) if self._files else None

    def _load(self, loaded):
        """``load_file`` 的实体。``loaded`` 是 ``ops.Loaded``。"""
        self.image = loaded.image
        self._original = loaded.image.copy()
        self._path = loaded.path or self._path
        self._exif_bytes = loaded.exif_bytes
        self.last_saved = None
        self._undo_stack.clear()
        self._redo_stack.clear()

        self._viewer.set_image(
            self.image,
            frames=loaded.frames if loaded.animated else None,
            durations=loaded.durations if loaded.animated else None)
        self._viewer.fit()
        self._refresh_info()
        self._update_status_name()
        self._show_status("%d × %d · %s · %s" % (
            self.image.width, self.image.height, loaded.fmt or "?",
            ops.human_size(loaded.size_bytes or 0)))

    def goto(self, delta):
        """在当前文件夹的图片之间跳（胶片条 / 方向键 / 幻灯片共用）。"""
        if len(self._files) < 2:
            return
        pos = (self._pos + delta) % len(self._files)
        self._pos = pos
        self._load_path(self._files[pos])

    def _load_path(self, path):
        loaded = ops.load_image(path)
        if not loaded.ok or loaded.image is None:
            # 坏文件不挡路：跳过去找下一张，但要让用户知道
            log.warning("切图失败 %s：%s", path, loaded.error)
            self._show_status("打不开 %s，已跳过" % os.path.basename(path))
            return
        self._pos = self._files.index(path) if path in self._files else self._pos
        self._load(loaded)
        self._highlight_thumb()

    # ==================================================================
    # 编辑（撤销 / 重做 / 变换）
    # ==================================================================
    def _edit(self, fn, label):
        if self.image is None:
            return None
        new = fn(self.image)
        if new is None:
            return None
        return self._apply_edit(new, label)

    def _edit_ccw(self):
        if self.image is None:
            return None
        new = self.image.rotate(90, expand=True)
        return self._apply_edit(new, "旋转")

    def _apply_edit(self, new, label):
        self._undo_stack.append((label, self.image))
        if len(self._undo_stack) > HISTORY_MAX:
            self._undo_stack.pop(0)
        self._redo_stack.clear()
        self.image = new
        self._refresh_after_edit()
        return new

    def undo(self):
        if not self._undo_stack:
            return
        label, old = self._undo_stack.pop()
        self._redo_stack.append((label, self.image))
        self.image = old
        self._refresh_after_edit()
        self._undo_btn_state()
        self._show_status("已撤销：%s" % label)

    def redo(self):
        if not self._redo_stack:
            return
        label, nxt = self._redo_stack.pop()
        self._undo_stack.append((label, self.image))
        self.image = nxt
        self._refresh_after_edit()
        self._undo_btn_state()
        self._show_status("已重做：%s" % label)

    def revert(self):
        """回到本次打开时的原图（丢掉全部编辑）。"""
        if self._original is None:
            return
        self.image = self._original.copy()
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._refresh_after_edit()
        self._undo_btn_state()
        self._show_status("已还原原图")

    def resize_to_px(self, w, h):
        new = ops.resize_px(self.image, w, h) if self.image else None
        return self._apply_edit(new, "改尺寸 %d×%d" % (w, h)) if new else None

    def resize_to_pct(self, pct):
        new = ops.resize_pct(self.image, pct) if self.image else None
        return self._apply_edit(new, "缩放 %.0f%%" % pct) if new else None

    def rotate(self):
        """顺时针 90°（serial_test 靠它验证 1200×800 → 800×1200）。"""
        return self._edit(ops.rotate_cw, "旋转")

    def _refresh_after_edit(self):
        self._viewer.set_image(self.image, keep_view=True)
        self._refresh_info()
        self._update_status_name()
        self._undo_btn_state()


    # ==================================================================
    # 保存
    # ==================================================================
    def suggested_name(self, fmt):
        """默认另存名 = ``原名_改.ext``。**绝不等于原文件名。**"""
        return ops.suggested_name(self._path, fmt)

    def write_image(self, path, fmt, quality=92, dpi=None, exif=None):
        """写盘的唯一入口（serial_test 逐格式调用它）。"""
        ok, detail, _n = ops.save_image(self.image, path, fmt,
                                        quality=quality, dpi=dpi, exif=exif)
        return ok, detail

    def save_as(self, fmt=None):
        """另存为。返回写出的路径；用户取消 / 失败返回 None。"""
        if self.image is None:
            return None
        fmt = fmt if fmt in FMT_EXT else "JPEG"
        path = filedialog.asksaveasfilename(
            title="保存为 %s" % fmt, parent=self,
            defaultextension=FMT_EXT[fmt],
            filetypes=ops.save_types(fmt),
            initialfile=self.suggested_name(fmt))
        if not path:
            return None
        # 硬约束：目标 == 原路径 → 再确认一次，默认按钮必须是「否」
        if self._path and os.path.abspath(path) == os.path.abspath(self._path):
            ok = messagebox.askyesno(
                APP_TITLE,
                "你选的就是原图所在的位置。\n确定要覆盖原图吗？",
                default=messagebox.NO, parent=self)
            if not ok:
                return None
        exif = self._exif_bytes if (self._keep_exif and fmt in ops.EXIF_OK
                                    and self._exif_bytes) else None
        ok, detail = self.write_image(path, fmt, self._quality, exif=exif)
        if not ok:
            messagebox.showerror(APP_TITLE, detail, parent=self)
            return None
        self.last_saved = path
        self._show_status("已保存：%s" % os.path.basename(path))
        return path

    def _show_save(self):
        self.save_as()

    # ==================================================================
    # 四个对话框
    # ==================================================================
    def _show_resize(self):
        if self.image is None:
            return
        dialogs.ResizeDialog(self, size=self.image.size,
                             on_px=self._do_resize_px,
                             on_pct=self._do_resize_pct).show()

    def _show_convert(self):
        if self.image is None:
            return
        fmt = ops.fmt_of_path(self._path) if self._path else "JPEG"
        source = 0
        if self._path and os.path.exists(self._path):
            try:
                source = os.path.getsize(self._path)
            except OSError:
                source = 0
        dialogs.ConvertDialog(
            self, image=self.image, fmt=fmt, quality=self._quality,
            exif_bytes=self._exif_bytes, source_bytes=source,
            on_ok=self._do_convert).show()

    def _show_crop(self):
        if self.image is None:
            return

        def on_ok(box):
            cropped = ops.crop(self.image, box)
            if cropped is not None:
                self._apply_edit(cropped, "裁剪")

        dialogs.CropDialog(self, image=self.image, on_ok=on_ok).show()

    def _show_adjust(self):
        if self.image is None:
            return

        def on_ok(img, identity):
            if not identity and img is not None:
                self._apply_edit(img, "调整")

        dialogs.AdjustDialog(self, image=self.image, on_ok=on_ok).show()

    # ---------- 叠加图片 ----------
    def _show_overlay(self):
        """把另一张图贴到当前图上。"""
        if self.image is None:
            self._show_status("先打开一张图片")
            return

        def on_ok(img):
            if img is not None:
                self._apply_edit(img, "叠加图片")

        dialogs.OverlayDialog(
            self, base=self.image,
            on_pick=self._pick_overlay_source,
            on_clipboard=self._overlay_from_clipboard,
            on_ok=on_ok,
        ).show()

    def _pick_overlay_source(self):
        """选一张要叠加上来的图。返回 ``(图, 文件名)`` 或 None。"""
        path = filedialog.askopenfilename(
            title="选择要叠加的图片", parent=self,
            filetypes=ops.open_types())
        if not path:
            return None
        loaded = ops.load_image(path)
        if loaded.image is None:
            messagebox.showwarning(
                "这张图打不开",
                "读不了这个文件：\n\n%s\n\n%s"
                % (os.path.basename(path), loaded.detail or "格式可能不受支持。"),
                parent=self)
            return None
        return loaded.image, os.path.basename(path)

    def _overlay_from_clipboard(self):
        """把剪贴板里的图当作叠加素材。返回 ``(图, 说明)`` 或 None。"""
        img = shell_ops.read_image_from_clipboard()
        if img is None:
            return None
        return img, "剪贴板图片"

    def _build_overlay_body(self, top):
        return dialogs.build_overlay_body(
            top, base=self.image,
            on_pick=None, on_clipboard=None, close=top.destroy)

    # ---------- 反馈 ----------
    def _show_feedback(self):
        """反馈按钮：**本模块只回调，不碰邮件、不碰地址**。

        收件地址、"第一次要先过隐私说明"、交给系统邮件程序 —— 全在 main.py
        那一层。界面层连那个邮箱字符串都拿不到，也就没什么可泄漏的。
        """
        if not callable(self.on_feedback):
            self._show_status("这个版本没有配置反馈方式")
            return
        self.on_feedback()

    # ---- 表单出口（layout_test 直接渲染这两块，不动对话框外壳） ----
    def _build_resize_body(self, top):
        return dialogs.build_resize_body(
            top, size=self.image.size if self.image else (100, 100),
            on_px=self._do_resize_px, on_pct=self._do_resize_pct,
            close=top.destroy)

    def _build_convert_body(self, top):
        return dialogs.build_convert_body(
            top, image=self.image,
            fmt=ops.fmt_of_path(self._path) if self._path else "JPEG",
            quality=self._quality, exif_bytes=self._exif_bytes,
            on_ok=self._do_convert, close=top.destroy)

    def _do_resize_px(self, w, h):
        self.resize_to_px(w, h)

    def _do_resize_pct(self, pct):
        self.resize_to_pct(pct)

    def _do_convert(self, fmt, quality, keep):
        self._quality = int(quality)
        self._keep_exif = bool(keep)
        self.save_as(fmt)

    # ==================================================================
    # 系统动作（右键菜单）
    # ==================================================================
    def _popup_menu(self):
        menu = tk.Menu(self, tearoff=0, bg=BG_ELEV, fg=FG_TEXT,
                       activebackground="#31353f", activeforeground=FG_TEXT,
                       bd=1, font=font(9))
        has = self.image is not None
        menu.add_command(label="打开…", command=self.open_file)
        menu.add_command(label="打开文件夹…", command=self.open_folder)
        menu.add_separator()
        menu.add_command(label="复制图片", command=self.copy_image,
                         state="normal" if has else "disabled")
        menu.add_command(label="设为桌面背景", command=self.set_wallpaper,
                         state="normal" if has else "disabled")
        menu.add_command(label="打印…", command=self.print_current,
                         state="normal" if has else "disabled")
        menu.add_command(label="在资源管理器中显示",
                         command=self.reveal_current,
                         state="normal" if (has and self._path) else "disabled")
        menu.add_separator()
        menu.add_command(label="还原原图", command=self.revert,
                         state="normal" if (has and self._undo_stack) else "disabled")
        menu.add_command(label="删除（移到回收站）", command=self.delete_current,
                         state="normal" if (has and self._path) else "disabled")
        try:
            menu.tk_popup(*self.winfo_pointerxy())
        finally:
            menu.grab_release()

    def copy_image(self):
        ok, detail = shell_ops.copy_image_to_clipboard(self.image)
        self._show_status("已复制到剪贴板" if ok else detail)

    def set_wallpaper(self):
        ok, detail = shell_ops.set_wallpaper(self.image)
        self._show_status("已设为桌面背景" if ok else detail)

    def print_current(self):
        if self.image is None:
            return
        # 打"当前看到的图"：旋转 / 裁剪 / 调整过的编辑结果都会反映出来，
        # 而不是磁盘上那份可能已过时的原文件。
        ok, detail = shell_ops.print_image(self.image)
        self._show_status("已交给系统打印" if ok else detail)

    def reveal_current(self):
        if not self._path:
            return
        ok, detail = shell_ops.reveal_in_explorer(self._path)
        if ok:
            self._show_status("")
        else:
            self._show_status(detail)

    def delete_current(self):
        """删除当前文件（回收站）。删完自动跳到同一位置的下一张。"""
        if self.image is None or not self._path:
            return
        name = os.path.basename(self._path)
        if not messagebox.askyesno(APP_TITLE, "把「%s」移到回收站？" % name,
                                   default=messagebox.NO, parent=self):
            return
        path = self._path
        ok, detail = shell_ops.delete_to_recycle_bin(path)
        if not ok:
            messagebox.showerror(APP_TITLE, detail, parent=self)
            return
        idx = self._pos if 0 <= self._pos < len(self._files) else -1
        try:
            self._files.remove(os.path.abspath(path))
        except ValueError:
            pass
        self._render_thumbs()
        if self._files:
            self._pos = min(max(0, idx), len(self._files) - 1)
            self._load_path(self._files[self._pos])
        else:
            self._clear_image()
        self._show_status("已移到回收站：%s" % name)

    def _clear_image(self):
        self.image = None
        self._original = None
        self._path = None
        self._exif_bytes = None
        self._pos = -1
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._viewer.clear()
        self._refresh_info()
        self._update_status_name()
        self._undo_btn_state()
        self._show_status("没有图片了")

    # ==================================================================
    # 幻灯片 / 全屏 / 信息面板
    # ==================================================================
    def _toggle_slideshow(self):
        if len(self._files) < 2:
            self._show_status("当前文件夹里只有一张（或零张）图")
            return
        self._slideshow = not self._slideshow
        if self._slideshow:
            self._show_status("幻灯片播放中，按 Esc 停止")
            self._slide_job = self.after(SLIDESHOW_MS, self._slide_step)
        else:
            self._stop_slide_job()
            self._show_status("幻灯片已停止")

    def _stop_slide_job(self):
        if self._slide_job is not None:
            try:
                self.after_cancel(self._slide_job)
            except (tk.TclError, ValueError):
                pass
            self._slide_job = None

    def _slide_step(self):
        if not self._slideshow:
            return
        self.goto(1)
        self._slide_job = self.after(SLIDESHOW_MS, self._slide_step)

    def _toggle_fullscreen(self):
        top = self.winfo_toplevel()
        self._fullscreen = not self._fullscreen
        try:
            top.attributes("-fullscreen", self._fullscreen)
        except tk.TclError:
            return
        if self._fullscreen:
            # 全屏只留查看器；信息面板与提示行也一并收起
            if self._info_on:
                self._info.pack_forget()
            for w in (self._notice, self._bar, self._strip, self._statusbar):
                w.pack_forget()
            self._show_status("全屏，按 Esc 退出")
        else:
            if self._info_on:
                self._info.pack(in_=self._mid, side="right", fill="y",
                                before=self._viewer.master)
            self._repack_columns()
        self._viewer.fit()

    def _repack_columns(self):
        """全屏切换后把 pack 顺序恢复成 提示行/工具栏/中部/胶片条/状态栏。

        tk 的 pack 是"按 pack 的先后顺序占位"，全屏时 forget 掉几根再
        pack 回来，顺序就乱了 —— 与其记"谁在谁前面"，不如整列重排一遍。
        """
        for w in (self._notice, self._bar, self._mid, self._strip,
                  self._statusbar):
            try:
                w.pack_forget()
            except tk.TclError:
                pass
        self._notice.pack(fill="x")
        self._bar.pack(fill="x")
        self._mid.pack(fill="both", expand=True)
        self._strip.pack(fill="x")
        self._statusbar.pack(fill="x")

    def _toggle_info(self):
        self._info_on = not self._info_on
        if self._info_on:
            self._info.pack(in_=self._mid, side="right", fill="y",
                            before=self._viewer.master)
            self._refresh_info()
        else:
            self._info.pack_forget()
            self._viewer.fit()

    def _refresh_info(self):
        for child in self._info_body.winfo_children():
            child.destroy()
        if not self._info_on:
            return
        head = tk.Frame(self._info_body, bg=BG_PANEL)
        head.pack(fill="x", padx=12, pady=(12, 4))
        name = os.path.basename(self._path) if self._path else "（未保存的图）"
        tk.Label(head, text=name, bg=BG_PANEL, fg=FG_TEXT, font=font(10, "bold"),
                 anchor="w", wraplength=INFO_W - 24,
                 justify="left").pack(anchor="w")
        if self.image is None:
            tk.Label(self._info_body, text="没有图片", bg=BG_PANEL, fg=FG_DIM,
                     font=font(9)).pack(anchor="w", padx=12, pady=8)
            return
        rows = [
            ("尺寸", "%d × %d 像素" % (self.image.width, self.image.height)),
            ("清晰度", "%.1f MP" % ops.megapixels(self.image.width, self.image.height)),
            ("模式", str(self.image.mode)),
            ("内存", "%.1f MB" % (self.image.width * self.image.height *
                                  len(self.image.getbands()) / 1048576.0)),
        ]
        if self._path and os.path.exists(self._path):
            try:
                rows.append(("文件大小", ops.human_size(os.path.getsize(self._path))))
                rows.append(("所在文件夹", os.path.dirname(self._path)))
            except OSError:
                pass
        for key, val in rows:
            row = tk.Frame(self._info_body, bg=BG_PANEL)
            row.pack(fill="x", padx=12, pady=1)
            tk.Label(row, text=key, bg=BG_PANEL, fg=FG_DIM, font=font(9),
                     anchor="w").pack(side="left")
            tk.Label(row, text=val, bg=BG_PANEL, fg=FG_TEXT, font=font(9),
                     anchor="e", wraplength=INFO_W - 100,
                     justify="left").pack(side="right")
        _Sep(self._info_body).pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(self._info_body, text="EXIF", bg=BG_PANEL, fg=FG_DIM,
                 font=font(9, "bold"), anchor="w").pack(anchor="w", padx=12)
        exif = ops.exif_rows(self.image, limit=24)
        if not exif:
            tk.Label(self._info_body, text="这张图没有 EXIF 信息",
                     bg=BG_PANEL, fg=FG_MUTED, font=font(8),
                     anchor="w").pack(anchor="w", padx=12, pady=2)
            return
        body = tk.Frame(self._info_body, bg=BG_PANEL)
        body.pack(fill="both", expand=True, padx=12)
        for cn, _en, val in exif:
            row = tk.Frame(body, bg=BG_PANEL)
            row.pack(fill="x")
            tk.Label(row, text=cn, bg=BG_PANEL, fg=FG_DIM, font=font(8),
                     anchor="w").pack(side="left")
            tk.Label(row, text=str(val)[:40], bg=BG_PANEL, fg=FG_TEXT,
                     font=font(8), anchor="e").pack(side="right")

    # ==================================================================
    # 胶片条
    # ==================================================================
    def _render_thumbs(self):
        """把当前清单画成胶片条。缩略图分帧渲染，一次 8 张，不卡界面。"""
        self._strip.delete("all")
        self._strip.configure(scrollregion=(0, 0, 0, 0))
        self._thumb_pos.clear()
        self._thumbs.clear()
        self._thumb_pending = list(self._files[:THUMB_LIMIT])
        self._strip.xview_moveto(0.0)
        self._pump_thumbs()

    def _pump_thumbs(self):
        self._thumb_job = None
        if not self._thumb_pending:
            return
        batch, self._thumb_pending = self._thumb_pending[:8], self._thumb_pending[8:]
        y0 = THUMB_PAD
        y1 = THUMB_PAD + THUMB_H
        x = 6 + len(self._thumb_pos) * (THUMB_H + THUMB_PAD * 2)
        if ImageTk is not None:
            for path in batch:
                img = self._thumb_source(path)
                if img is None:
                    continue
                thumb = ops.thumbnail(img, (THUMB_H, THUMB_H), allow_up=True)
                if thumb is None:
                    continue
                try:
                    photo = ImageTk.PhotoImage(thumb)
                except (tk.TclError, ValueError, MemoryError) as exc:
                    log.warning("缩略图渲染失败 %s：%s", path, exc)
                    continue
                self._thumbs[path] = photo
                tw = photo.width()
                self._strip.create_image(x + (THUMB_H - tw) // 2, y0,
                                         anchor="nw", image=photo,
                                         tags=("thumb", path))
                self._thumb_pos[path] = (x, x + THUMB_H)
                x += THUMB_H + THUMB_PAD * 2
        else:
            for path in batch:
                self._strip.create_text(x + THUMB_H // 2, y0 + THUMB_H // 2,
                                        text=os.path.basename(path)[:8],
                                        fill=FG_DIM, font=font(8))
                self._thumb_pos[path] = (x, x + THUMB_H)
                x += THUMB_H + THUMB_PAD * 2
        total = max(1, x + THUMB_PAD * 4)
        self._strip.configure(scrollregion=(0, 0, total, y1 + THUMB_PAD + 4))
        self._highlight_thumb()
        if self._thumb_pending:
            self._thumb_job = self.after(10, self._pump_thumbs)

    @staticmethod
    def _thumb_source(path):
        if Image is None:
            return None
        try:
            with Image.open(path) as opened:
                return opened.copy()
        except Exception as exc:                            # noqa: BLE001
            log.warning("缩略图读不出来 %s：%s", path, exc)
            return None

    def _highlight_thumb(self):
        self._strip.delete("hl")
        path = (self._files[self._pos] if 0 <= self._pos < len(self._files)
                else self._path)
        span = self._thumb_pos.get(path)
        if not span:
            return
        round_rect(self._strip, span[0] - 2, THUMB_PAD - 2,
                   span[1] + 2, THUMB_PAD + THUMB_H + 2, 4,
                   outline="#4aa3e0", width=2, tags=("hl",))

    def _on_strip_click(self, event):
        # 滚动过的部分要用 canvasx 换算回画布坐标
        cx = self._strip.canvasx(event.x)
        for path, (x0, x1) in self._thumb_pos.items():
            if x0 <= cx <= x1:
                self._pos = self._files.index(path)
                self._load_path(path)
                return

    def _on_strip_wheel(self, event):
        self._strip.xview_scroll(-1 if event.delta > 0 else 1, "units")

    # ==================================================================
    # 状态栏 / 启用态
    # ==================================================================
    def _on_zoom_changed(self, zoom):
        self._st_zoom.configure(text="%d%%" % round(zoom * 100))

    def _on_has_image(self, has):
        self._update_enabled(has)

    def _update_enabled(self, has):
        for group, buttons in self._edit_buttons.items():
            if group == "nav":
                state = "normal" if len(self._files) >= 2 else "disabled"
                for b in buttons:
                    b.set_state(state)
            elif group == "hist":
                continue          # undo/redo 由栈状态单独控制
            else:
                for b in buttons:
                    b.set_state("normal" if has else "disabled")
        self._undo_btn_state()

    def _undo_btn_state(self):
        hist = self._edit_buttons.get("hist", [])
        if len(hist) >= 2:
            hist[0].set_state("normal" if self._undo_stack else "disabled")
            hist[1].set_state("normal" if self._redo_stack else "disabled")

    def _update_status_name(self):
        if self.image is None:
            self._st_name.configure(text="")
            self._st_pos.configure(text="")
            return
        name = os.path.basename(self._path) if self._path else "（未保存）"
        dirty = " *" if self._undo_stack else ""
        self._st_name.configure(text=name + dirty)
        if len(self._files) > 1 and self._pos >= 0:
            self._st_pos.configure(
                text="%d / %d" % (self._pos + 1, len(self._files)))
        else:
            self._st_pos.configure(text="")

    def _show_status(self, text):
        # 页面可能已被销毁（换页、关窗），而 after 回调是异步的 ——
        # 死控件的 configure 会抛 TclError。状态栏写不进去不是大事，
        # 静默跳过即可，但绝不能因此崩掉。
        try:
            if not self.winfo_exists() or not self._st_msg.winfo_exists():
                return
            self._st_msg.configure(text=text)
        except tk.TclError:
            return
        if self._flash_job is not None:
            try:
                self.after_cancel(self._flash_job)
            except (tk.TclError, ValueError):
                pass
        try:
            self._flash_job = self.after(
                6000, lambda: self._st_msg.configure(text=""))
        except tk.TclError:
            self._flash_job = None

    # ==================================================================
    # 左下角小白点：连点 → 口令 → 重新开始
    # ==================================================================
    def _on_dot_click(self, _event=None):
        """记一次点击。连续 DOT_CLICKS 下（间隔都得小于 DOT_RESET_MS）才触发。"""
        if self._dot_job is not None:
            try:
                self.after_cancel(self._dot_job)
            except (tk.TclError, ValueError):
                pass
            self._dot_job = None
        self._dot_count += 1
        if self._dot_count >= DOT_CLICKS:
            self._dot_count = 0
            self._open_restart_gate()
            return
        self._dot_job = self.after(DOT_RESET_MS, self._reset_dot_count)

    def _reset_dot_count(self):
        self._dot_job = None
        self._dot_count = 0

    def _open_restart_gate(self):
        """弹口令框。口令对了就走 on_restart（main.py 负责清记录 + 重启）。"""
        def check(text):
            if text == RESTART_PASSWORD:
                self._show_status("口令正确，正在重新开始…")
                if self.on_restart is not None:
                    # 稍等一下再重启：让用户看见那句提示，也让对话框先关干净。
                    # 用 try 包住：这 150ms 里窗口可能已经被关掉了。
                    try:
                        self.after(150, self.on_restart)
                    except tk.TclError:
                        pass
                return True
            log.info("重启口令不正确（输入 %d 位）", len(text))
            return False

        dialogs.PasswordDialog(
            self, on_ok=check,
            subtitle="口令正确后，程序会重新启动，并从头开始（重新出题）。").show()

    # ==================================================================
    # 右下角「检查更新」
    # ==================================================================
    def _hover_update(self, on):
        if self._update_latest:
            self._update_btn.configure(fg="#ffd85c" if on else FG_WARN)
        else:
            self._update_btn.configure(fg=FG_TEXT if on else FG_DIM)

    def _on_update_click(self):
        if self.on_check_update is not None:
            self.on_check_update()

    def set_update_state(self, latest=None):
        """由 ``main.py`` 在查到结果后调用：没有新版显示「检查更新」，
        有新版本就变成「下载更新 x.y.z」并转为警示色。

        这样这个入口只有一种按钮、两种含义 —— 用户看到的字永远是他
        下一步要做的事，不需要再解释一次。
        """
        self._update_latest = latest or None
        if self._update_latest:
            self._update_btn.configure(text="下载更新 %s" % self._update_latest,
                                       fg=FG_WARN)
        else:
            self._update_btn.configure(text="检查更新", fg=FG_DIM)

    # ==================================================================
    # 收尾
    # ==================================================================
    def destroy(self):
        self._stop_slide_job()
        if self._thumb_job is not None:
            try:
                self.after_cancel(self._thumb_job)
            except (tk.TclError, ValueError):
                pass
            self._thumb_job = None
        super().destroy()


__all__ = [
    "APP_TITLE", "FORMATS", "FMT_EXT", "ALPHA_OK",
    "MIN_WINDOW_W", "MIN_WINDOW_H", "HISTORY_MAX", "SLIDESHOW_MS",
    "ImageTool",
]
