# -*- coding: utf-8 -*-
"""HAAVIK Photo —— 一个真能用的图片工具，外加一道每月出现一次的闸门。

行为契约
==================================================================
1. 默认（不带任何参数）的启动流程：

       本月还没过闸？──是──► 出「天空属于谁」这一问（配一张图）
       │                        ├─ 答对 ─► 本月过闸，直接进图片工具
       │                        └─ 答错 ─► 「软件已锁定」
       │                                     （一排输入框 + 序列号在哪的提示）
       否 ────────────────────────────► 直接进图片工具，什么都不问

   闸门是"每月一次"，不是每次启动：过闸记录写在
   %LOCALAPPDATA%\\HAAVIK Photo\\state.json（v1.2.0 起不在桌面，桌面上永远只有 exe 本身）。
   **答对不需要任何序列号**；答错也不用干等，序列号生成器就在公开仓库里，
   自己下载、自己生成、自己填 —— 这道题是给人解的，不是用来卡人的。

2. 图片工具是**真的能用**的：打开、改尺寸（按像素或按百分比，可锁纵横比）、
   转格式（JPEG / PNG / WebP / BMP）、旋转、另存为。
   **绝不默认覆盖原图**：默认文件名 ``原名_改.ext``；即便用户执意选原路径，
   还要再确认一次，而那个确认框的默认按钮是「否」。

3. 写盘只有三处，verify_payload.py 里有断言把来源文件钉死：
   * ``state.json``                —— 月度过闸状态（state_store.py）
   * 用户点「另存为」选定的那张图   —— image_tool.py
   * 用户点「下载」拿到的更新包     —— version_check.py
   除此之外不碰任何文件。

4. ``--real-shutdown`` 保留为**大人自己的玩法**：带上它，答错才会走老的
   「60 秒关机倒计时」。默认流程里**永远不会**调用 ``shutdown``，
   界面上也永远不出现倒计时 —— 默认路径上没有可能丢失未保存数据的东西。

5. 不写注册表、不设自启动、不请求提权。

启动参数
------------------------------------------------------------------
    --real-shutdown   答错时走老的"真关机倒计时"（默认关闭，改走锁定页）
    --seconds N       倒计时秒数（默认 60，仅 --real-shutdown 下有效）
    --no-image        跳过图片加载（无 Pillow 环境的降级路径）
    --no-update-check 关闭启动时的版本查询（全离线运行）
    --reset-gate      清掉"本月已过闸"，下次启动重新出题（给自己演示用）
    --debug           输出调试日志

退出码
------------------------------------------------------------------
    0   正常结束（答对通关 / 用户主动关闭）
    1   初始化失败（无图形环境）
"""
from __future__ import annotations

import argparse
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from tkinter import messagebox

try:
    from PIL import Image, ImageTk
except ImportError:  # Pillow 缺失时降级为纯色占位，不影响整蛊流程
    Image = None       # type: ignore[assignment]
    ImageTk = None     # type: ignore[assignment]

import feedback_send  # 第二个联网点：用户亲手点「直接发送」时才提交一次
import image_dialogs as dialogs
import image_tool
import serial_check
import shell_ops
import state_store
import ui_common
import version_check  # 联网点之一：只读版本号 / 清单 / 下载更新包（都不执行）

log = logging.getLogger("haavik")

APP_TITLE = "HAAVIK Photo"
BG_DARK = ui_common.BG_DARK
BG_ALERT = ui_common.BG_ALERT
FG_TEXT = ui_common.FG_TEXT
FG_DIM = ui_common.FG_DIM
FG_OK = ui_common.FG_OK
FG_BAD = ui_common.FG_BAD
FG_WARN = ui_common.FG_WARN
ACCENT = ui_common.ACCENT
PANEL = ui_common.BG_PANEL

CORRECT_HINT = "天空属于 哈夫克"
COUNTDOWN_DEFAULT = 60
MIN_WINDOW_W = ui_common.MIN_WINDOW_W
MIN_WINDOW_H = ui_common.MIN_WINDOW_H

#: 序列号提示里显示的仓库地址 —— **专门放验证码/生成器的仓库**，
#: 刻意和"分发安装包"的仓库分开：
#:   * 分发仓：只放 Setup.exe / manifest.json / version.txt（给更新通道用）
#:   * 验证码仓（就是这个）：只放生成器，是"作者给你留的那道题"
#: 两件事混在一个仓库里，使用者和未来的自己都会搞不清哪个文件是给谁用的。
#: ⚠️ 曾经指向 GitHub：对国内的朋友是**死链**（github.com 直连不通）。
REPO_HINT_URL = "https://gitee.com/whr3443kklld/verification-code"

# --------------------------------------------------------------------------
# 反馈
# --------------------------------------------------------------------------
#: 反馈收件箱。**只出现在这一处** —— 界面层拿不到它，也不需要它：
#: image_tool 只知道"有个 on_feedback 回调"，地址与发送手段都在这一层。
FEEDBACK_MAIL = "nhgn0763@agent.qq.com"

#: 「软件内直接发送」的接收端地址。**空字符串 = 没配置**（那时界面只给
#: "用邮件发送 / 复制文字"两条路，不会出现一个点了没反应的按钮）。
#:
#: 填上之后程序会多一个联网点：用户亲手点「直接发送」→ 一次 HTTPS POST
#: （见 feedback_send.py）→ 不重试、不跳转、不下载。
#:
#: 支持两类地址，都**不是密钥**，只是"公开的收件地址"：
#:   * 表单服务：``https://formspree.io/f/xxxxxxxx``
#:     （表单 ID 本来就写在人家网页源码里，任何人看网页都能拿到；
#:       被刷了就删掉表单重来）
#:   * 自己的云函数：``https://xxxx.apigw.tencentcs.com/release/feedback``
#:     （凭据留在云函数的环境变量里，程序里什么都没有）
#:
#: ⚠️ 无论接上**哪一条**直达通道（smtp 或 endpoint），都必须同步做两件事：
#:   1. 隐私说明里**加一条**写清那封信是怎么出去的
#:      （smtp：「用作者指定的一个邮箱直接发信」；
#:        endpoint：「内容会先经过 <那个服务> 再转成邮件」）；
#:   2. ``PRIVACY_VERSION`` **加一**，让所有已经同意过的人重新看一遍。
#:      （不这么做，"同意"就是对着旧内容点的一次，不作数。）
FEEDBACK_ENDPOINT = ""

#: 隐私说明的版本号。**改了下面那段文字就把它加一**。
#: 存进 state.json 的是版本号而不是"同意过"的布尔值 —— 说明内容变了就该
#: 重新问一次，"以前点过一次同意"不能当永久许可。
#: v1（1.5.1）：只能用系统邮件程序时的六条。
#: v2（1.6.0）：多了第 7 条 —— 这封信是程序直连作者指定的邮箱发出去的
#:              （`privacy_paragraphs()` 会按当前通道自动加上）。
#: v3（1.6.2）：收件地址改了。之前那版信发到了作者自己的 QQ 邮箱，
#:              而界面上写的收件人是另一个 —— **说明和实际对不上就是假说明**，
#:              所以让所有人重新看一遍（这次界面上写的就是真发到的那个）。
#: v4（2026-09-26）：两件事，都得让所有人重看一遍：
#:   ① **可以自愿留一个邮箱**了 —— 留了作者就能回信（不留就什么都不带）。
#:      第 3 条原来写的是"账号一律不发"，多了这个口子就必须明说。
#:   ② 修掉一处**自相矛盾**：第 4 条原来写死"用你电脑上的邮件程序发、
#:      不经过程序自己的网络"，可真实通道是 smtp（程序直连邮箱发信）——
#:      第 4 条和第 7 条说的根本不是一件事。现在把"走哪条路"从第 4 条里
#:      剥出去，改成**每种通道各自追加一条真实的**，不会再打架。
PRIVACY_VERSION = 5

#: 第一次用反馈必须先看的几条。写得短、写得具体 ——
#: 用户真正需要知道的是"什么东西会离开这台电脑"，不是一段法律措辞。
#: ⚠️ 这里每一句都必须与代码行为一致；第 4 条尤其别写成"程序不联网"
#: （检查更新是会联网的），否则这份说明自己就是假的。
PRIVACY_PARAS = (
    "1. 会发出去的东西：只有你在反馈框里打的字。",
    "2. 会附带的信息：程序版本号 + Windows 版本号。这一条**可以取消勾选**，"
    "取消后就只发你打的那几个字。",
    "3. 不会发出去的东西：你的文件名与文件内容、图片、聊天记录、账号密码、"
    "机器名、IP 地址、日志 —— 一律不读、不发。"
    "**唯一例外是你自愿留下的回信邮箱**（见下一条）："
    "你不填，就一个字都不会带。",
    "4. 想收到回信的话，可以在下面**自愿**留一个邮箱（不填也行）。"
    "留了，作者才能回你；不填，这封信里就没有任何能联系到你的东西。",
    "5. **只有你亲手点了「发送」才会发出去** —— 程序不会自己发信，"
    "也不会在后台偷偷上报。这封信具体走哪条路，见最后一条。",
    "6. 发给谁、做什么用：发到作者的邮箱 %s，只用来改进这个程序，"
    "不会转给别人。" % FEEDBACK_MAIL,
    "7. 不同意也没关系：所有功能照常可用，下次点「反馈」还会再问你一次。",
    "8. 安装/更新失败时，安装器（Setup.exe）可以一键把一份**出错报告**发到作者邮箱："
    "内容只有出错信息、系统版本与位数、安装/自检日志——不含照片或个人文件；"
    "也只有你亲手点「发送」才会发。",
)


#: 走「程序直连邮箱」那条路时要追加的一条说明。
#: **只在真的用那条路时才出现** —— 隐私说明必须描述真实行为，
#: 不能提前写一条"可能会这样"的话（那也是假的）。
PRIVACY_SMTP_PARA = (
    "9. 这封信是**由作者指定的一个邮箱**直接发出去的：程序连邮箱发信，"
    "不经过任何第三方网站。那个邮箱只用来收反馈；它的发信凭据写在安装包里，"
    "作者可以随时在邮箱后台重置它。",
)

#: 走「交给系统邮件程序」那条路时要追加的一条。
#: ⚠ 这一条以前**藏在基础条款的第 4 条里**，结果真实通道是 smtp 时就自相矛盾了
#: （第 4 条说"不经过程序自己的网络"，第 7 条又说"程序连邮箱发信"）。
#: 现在每种通道都各有一条，不会再打架（2026-09-26 修，随 v4 一起）。
PRIVACY_MAILTO_PARA = (
    "9. 这封信是由**你电脑上的邮件程序**生成的：内容你能看见、也能改，"
    "**要你自己点发送**才真的出去。程序自己不发这封信、也不碰你的邮箱。",
)


def feedback_service_name() -> str:
    """从接收端地址里取出服务名（用来在隐私说明里如实写出"经过谁"）。

    故意不 import urllib.parse（那是第二个联网点专用的模块）——
    这里只是一次字符串切分，不值得为它多开一个口子。
    """
    text = (FEEDBACK_ENDPOINT or "").strip()
    if "//" not in text:
        return ""
    return text.split("//", 1)[1].split("/", 1)[0]


def privacy_paragraphs() -> tuple:
    """**当前配置下**要展示的隐私条款。

    为什么要做成函数而不是一个常量：条款必须跟着真实行为走。
    接上了直达通道却不说明"这封信是怎么出去的"，那份同意书就是摆设。
    这条不只是注释 —— ``feedback_test`` 里有断言盯着它。
    """
    paras = list(PRIVACY_PARAS)
    mode = feedback_transport()
    # ⚠ **每一种通道都要追加一条**，包括 mailto。
    #   以前 mailto 那条是写在基础条款里的（第 4 条），结果真实通道换成 smtp 后
    #   基础条款和第 7 条互相矛盾 —— 说明就成了假的。
    #   现在基础条款只管"什么会/不会出去、只有你点了才发"，
    #   "这封信怎么走的"一律由这里按实际配置生成。三选一，没有第四条路。
    if mode == "smtp":
        paras.append(PRIVACY_SMTP_PARA[0])
    elif mode == "endpoint":
        name = feedback_service_name() or "一个第三方表单服务"
        paras.append("9. 这封信会**先经过 %s** 再转成邮件发给作者："
                     "那个服务的服务器能看到你写的内容。" % name)
    else:                                     # mailto
        paras.append(PRIVACY_MAILTO_PARA[0])
    return tuple(paras)


def windows_build_text() -> str:
    """给反馈附带的系统信息：只到"版本号"这一级，不带机器名/用户名。

    故意不用 ``platform`` 模块：``sys.getwindowsversion()`` 是标准库直接给的，
    不需要额外 import（少一个 import 就少一条要解释的东西）。
    """
    try:
        v = sys.getwindowsversion()
        return "Windows %d.%d.%d" % (v.major, v.minor, v.build)
    except (AttributeError, TypeError):
        return "Windows（版本号取不到）"


def feedback_version_info() -> str:
    """反馈框里显示的"会附上什么" —— 用户在勾选框旁边就能看到原文。"""
    return "%s %s · %s" % (APP_TITLE, version_check.APP_VERSION,
                           windows_build_text())


def feedback_recipient() -> str:
    """界面上"收件人"那一行显示什么。

    ⚠️ 它必须**等于实际发送目标**：隐私说明里写谁，就得真发给谁。
    所以这里不是常量，而是问当前那条通道算出来的（smtp 那条可能发给
    自己在凭据里配的另一个地址）。
    """
    if feedback_transport() == "smtp":
        return feedback_send.effective_recipient() or FEEDBACK_MAIL
    return FEEDBACK_MAIL


def feedback_transport() -> str:
    """反馈用哪条通道发。三条按"朋友那边需要什么"从少到多排：

    ``smtp``     —— 程序直接连邮箱发信。朋友**什么都不需要有**（不用邮箱、
                    不用邮件程序、不用登录），是最接近"点一下就到我邮箱"的一条。
                    开关 = 构建时有没有塞发信凭据（见 ``feedback_send.credentials``）。
    ``endpoint`` —— POST 到公开接收端（表单服务 / 自己的云函数）。
                    开关 = ``FEEDBACK_ENDPOINT`` 非空。
    ``mailto``   —— 交给系统邮件程序。朋友得配过邮件程序才行，
                    所以现在只作为最后的兜底（并且界面会说明失败时怎么办）。
    """
    if feedback_send.smtp_ready():
        return "smtp"
    if FEEDBACK_ENDPOINT:
        return "endpoint"
    return "mailto"


# 不再用 chr(115)+chr(104)+... 拼接：
# 这种"反静态特征"写法是杀软判定脚本小子的头号特征，而且让代码不可读、
# 无法 grep、无法审计。可读性的收益远大于那一点点虚假的隐蔽性。
SHUTDOWN_EXE = "shutdown"

# 图片显示区最大尺寸（答题页用；图片工具的预览区尺寸在 ui_common 里）
MAX_IMG_W, MAX_IMG_H = 720, 460


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def resource_path(rel: str) -> str:
    """兼容 PyInstaller 解包目录 (sys._MEIPASS) 与源码直接运行。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def load_image(path: str, max_w: int = MAX_IMG_W, max_h: int = MAX_IMG_H):
    """读取并按比例缩放到显示区内。失败返回 None，由调用方降级。"""
    if Image is None:
        return None
    try:
        with Image.open(path) as img:
            ratio = min(max_w / img.width, max_h / img.height, 1.0)
            size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
            resample = getattr(Image, "Resampling", Image).LANCZOS
            return img.convert("RGB").resize(size, resample)
    except (OSError, ValueError) as exc:
        log.warning("图片加载失败 %s：%s", path, exc)
        return None


def run_shutdown(args: list[str]) -> tuple[bool, str]:
    """调用系统 shutdown 命令。

    返回 ``(是否成功, 说明文字)``。**不抛异常**——整蛊程序不该因为
    关机命令失败而崩掉，否则用户会看到一个莫名其妙的错误框。
    """
    try:
        proc = subprocess.run(
            [SHUTDOWN_EXE, *args],
            shell=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("shutdown %s 调用失败：%s", args, exc)
        return False, str(exc)

    detail = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        log.error("shutdown %s 返回 %s：%s", args, proc.returncode, detail)
        return False, detail
    log.info("shutdown %s 成功：%s", args, detail)
    return True, detail


# --------------------------------------------------------------------------
# 倒计时：单一时间源（只在 --real-shutdown 下才会被用到）
# --------------------------------------------------------------------------
class Countdown:
    """把"显示秒数"和"到点触发"绑到同一个 deadline 上。

    旧实现用了两套计时器（UI 的 after 循环 + 线程里的 time.sleep(60)），
    两者独立递增，会漂移；而且线程里没有任何取消检查——用户在最后一秒
    点取消，线程照样会在 60 秒整执行关机。这里用 Event 修掉这个竞态。
    """

    def __init__(self, seconds: int, on_expire) -> None:
        self._deadline = time.monotonic() + seconds
        self._on_expire = on_expire
        self._cancelled = threading.Event()
        self._thread = threading.Thread(target=self._worker, name="countdown", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def remaining(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    def cancel(self) -> bool:
        """取消。返回 True 表示成功拦截了尚未触发的动作。"""
        already = self._cancelled.is_set()
        self._cancelled.set()
        return not already

    def _worker(self) -> None:
        # Event.wait 可以在取消时立刻返回，不需要等满 sleep
        if self._cancelled.wait(self.remaining()):
            log.info("倒计时已取消，未执行任何动作")
            return
        self._on_expire()


# --------------------------------------------------------------------------
# 序列号输入框：一排 5 格 × 5 位
# --------------------------------------------------------------------------
class CodeEntry:
    """序列号输入框：**一个普通输入框**，既能一个字一个字敲，也能整串粘。

    为什么不再用"5 个小格子"
    ------------------------------------------------------------------
    原来做成 Windows 激活码那样的 5 个小格（每格 5 位、填满自动跳格）。
    好看，但对"输入"不友好 —— 使用者反馈"没有输入框，没法输入"：
      * 粘进来只往当前那一格塞，超出 5 位就被截掉；
      * 敲键时跳格逻辑插手，想改中间某一位很别扭；
      * 一整串 25 位分在 5 个小格上，看不出"我到底输够没有"。
    现在换成一个**普通输入框**，打字和粘贴是同一条路：
      * 想敲就敲（自动转大写、忽略分隔符与非法字符，但**不猜、不纠正**）；
      * 想粘就粘（Ctrl+V / Shift+Insert / 右键菜单 / 旁边的「粘贴」按钮）；
      * 超长不做截断，留着让校验说"多了一位" —— 偷偷砍掉比报错难查得多；
      * 粘不进来（剪贴板空、或里面没有一个合法字符）**一定吭声**，
        通过 ``on_problem`` 写到页面上。静默失败是这段代码最大的错。
    归一化仍然只交给 ``serial_check.normalize`` —— 界面层不做任何猜测。
    """

    def __init__(self, parent, *, on_submit=None, on_problem=None,
                 width=30, **entry_kw) -> None:
        self.on_submit = on_submit
        self.on_problem = on_problem
        self._width = width
        self._entry_kw = entry_kw
        self.var = tk.StringVar(value="")
        self.frame = tk.Frame(parent, bg=entry_kw.get("bg", BG_DARK))
        self.entry = None

    def build(self) -> tk.Frame:
        self.entry = tk.Entry(
            self.frame, textvariable=self.var, width=self._width,
            justify="center", font=("Consolas", 16, "bold"),
            **self._entry_kw,
        )
        self.entry.pack(ipady=6)
        # 打字时实时归一化（大写、去分隔符、丢掉非法字符）
        self.entry.bind("<KeyRelease>", lambda _e: self._on_key())
        self.entry.bind("<Return>", lambda _e: self.submit())
        # 粘贴：所有常见键位都显式绑在自己身上，不依赖 Entry 的类绑定；
        # 返回 "break" 是为了不让类绑定再粘一遍（那会粘成两份）。
        for seq in ("<<Paste>>", "<Control-v>", "<Control-V>", "<Shift-Insert>"):
            self.entry.bind(seq, lambda _e: self._on_paste())
        self.entry.bind("<Button-3>", self._popup)     # 右键菜单
        return self.frame

    # ---------- 交互 ----------
    @staticmethod
    def _filter(text: str) -> str:
        norm = serial_check.normalize(text)
        return "".join(ch for ch in norm if ch in serial_check.ALPHABET_SET)

    def _say(self, text: str) -> None:
        """把"发生了什么"写回页面 —— 粘贴失败最忌讳一声不吭。"""
        if self.on_problem is not None:
            self.on_problem(text)

    def _on_key(self) -> None:
        cleaned = self._filter(self.var.get())
        if cleaned != self.var.get():
            self.var.set(cleaned)

    def paste_anywhere(self) -> None:
        """给页面上的「粘贴」按钮用。"""
        self._on_paste()

    def _on_paste(self) -> str:
        # 剪贴板偶尔会读不到：Windows 上任何程序都可能在一瞬间占着它，
        # 本机的 360 / 火绒 这类挂钩剪贴板的安全软件尤其明显 —— 表现就是
        # "点了粘贴没反应"。所以给它几次机会（合计约 120ms），而不是一次就放弃。
        raw = None
        last_exc = None
        for attempt in range(3):
            try:
                raw = self.frame.clipboard_get()
                break
            except tk.TclError as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.06)
        if raw is None:
            log.info("读取剪贴板失败：%r", last_exc)
            self._say("剪贴板里读不到文字。先把那串序列号复制下来，再来粘贴。")
            return "break"
        if not str(raw).strip():
            self._say("剪贴板里是空的。先把那串序列号复制下来，再来粘贴。")
            return "break"
        text = self._filter(raw)
        if not text:
            self._say("剪贴板里没有一个有效字符 —— 序列号只用 0-9 和 A-Z"
                      "（不含 I、L、O）。请重新复制一次。")
            return "break"
        self.var.set(text)
        self.focus_first()
        need = serial_check.TOTAL_CHARS
        if len(text) < need:
            self._say("粘进来 %d 位，但这串要 %d 位 —— 可能复制时漏了一截。"
                      % (len(text), need))
        elif len(text) > need:
            self._say("粘进来 %d 位，比 %d 位多 —— 多半是复制时多带了东西。"
                      % (len(text), need))
        else:
            self._say("已粘贴 %d 位。" % need)
        return "break"

    def _popup(self, event) -> None:
        """右键菜单 —— 只用鼠标的人也得有路走。"""
        menu = tk.Menu(self.frame, tearoff=0)
        menu.add_command(label="粘贴", command=self.paste_anywhere)
        menu.add_command(label="清空", command=self.clear)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ---------- 取值 ----------
    def value(self) -> str:
        return self.var.get()

    def clear(self) -> None:
        self.var.set("")
        self.focus_first()

    def focus_first(self) -> None:
        try:
            self.entry.focus_set()
        except (tk.TclError, AttributeError):
            pass                # 还没 build() 或者窗口已经销毁

    def select_all(self) -> None:
        """全选内容 —— 输错之后让用户能直接"改一位重试"，不必先清空。"""
        try:
            self.entry.selection_range(0, "end")
            self.entry.icursor("end")
        except (tk.TclError, AttributeError):
            pass

    def set_ok(self, ok: bool) -> None:
        color = "#2ecc71" if ok else "#e74c3c"
        try:
            self.entry.configure(highlightthickness=2, highlightbackground=color,
                                 highlightcolor=color)
        except (tk.TclError, AttributeError):
            pass

    def submit(self) -> None:
        if self.on_submit is not None:
            self.on_submit(self.value())


@dataclass(frozen=True)
class Option:
    """一个答题选项。correct 标记正确答案，避免把"对错"散落在回调里。"""

    label: str
    color: str
    correct: bool = False


OPTIONS: tuple[Option, ...] = (
    Option("A. 哈夫克", "#27ae60", correct=True),
    Option("B. 阿萨拉", "#e67e22"),
    Option("C. GTI", "#3498db"),
)


# --------------------------------------------------------------------------
# 主界面
# --------------------------------------------------------------------------
class App:
    """把 root 变成"HAAVIK Photo"。

    它只做决定：这个月要不要出题、出的是题还是锁、过闸之后交给谁。
    图片工具本身在 image_tool.ImageTool 里，这里不掺和它的事。
    """

    def __init__(
        self,
        root: tk.Tk,
        *,
        real_shutdown: bool,
        seconds: int,
        show_image: bool,
        check_updates: bool = True,
        state: dict | None = None,
    ) -> None:
        self.root = root
        self.real_shutdown = real_shutdown
        self.seconds = seconds
        self.show_image = show_image
        self.check_updates = check_updates

        self.state = state if state is not None else state_store.load()
        log.info("状态：%s", state_store.describe(self.state))

        self.countdown: Countdown | None = None
        self._armed = False          # 是否已经向系统下达过关机指令
        self._tick_job: str | None = None
        self._expired = False        # 归零后是否已经翻过揭晓页（防止翻两次）
        self.mode = "quiz"           # quiz / lock / countdown / tool

        self.tool: image_tool.ImageTool | None = None
        self.update_label: tk.Label | None = None
        self.update_button: tk.Button | None = None
        self._pending_update: str | None = None
        self._manifest: dict | None = None
        self._downloading = False
        #: 下载/校验进度窗口（None = 没在显示）。用户中途关掉它是允许的，
        #: 所以每次写它之前都要先问一句"还在吗"。
        self._progress_dlg = None
        #: AI 模型下载完成时回调给图片工具（由 _download_ai_models 设置）。
        self._ai_download_cb = None

        root.title(APP_TITLE)
        root.configure(bg=BG_DARK)
        # 允许缩放：显示缩放比很低的机器上用户还能自己拉大，避免控件被裁掉
        root.resizable(True, True)
        root.minsize(MIN_WINDOW_W, MIN_WINDOW_H)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._min_w = MIN_WINDOW_W
        self._min_h = MIN_WINDOW_H
        root._fit_owner = self  # 供布局测试定位"该由谁来做窗口自适应"

        # 闸门判断：**先看有没有被锁**。
        # 答错之后进的是"锁定"状态，而不是"再问一次"——锁定必须跨进程活着：
        # 关掉程序、重启电脑、装个新版，再打开都还在锁定页。
        # （这是使用者实测反馈出来的：原来关掉重开又回到了问题页，等于没锁。）
        if state_store.is_locked(self.state):
            log.info("上次是答错被锁的状态 → 直接进锁定页")
            self._build_lock_page()
        elif state_store.is_unlocked(self.state, version=version_check.APP_VERSION):
            self._show_tool(first_time=False)
        else:
            self._build_quiz_page()

        # 顺手把更新目录里**已经用过**的安装包清掉（每点是 34.5MB，
        # 不清理会一直攒）。只删版本号不高于当前程序的那些 —— 刚下好还没装的
        # 那个必须留着。清理失败不影响任何事，所以这里兜住异常只记日志。
        try:
            version_check.prune_updates(version_check.APP_VERSION)
        except Exception as exc:                           # noqa: BLE001
            log.warning("清理旧更新包时出错（已忽略）：%s", exc)

        if self.check_updates:
            self._start_update_check()

    # ---------- 窗口自适应 ----------
    def _fit_window(self) -> None:
        """按当前页面的**实测**需求调整窗口，确保任何显示缩放下控件都完整可见。

        真正的实现在 ui_common.fit_window（问答页、锁定页、图片工具页共用一份），
        这里只负责把"当前页面是谁"这件事传出去。
        """
        if self.mode == "tool" and self.tool is not None:
            self.tool._min_w = max(self.tool._min_w, self._min_w)
            self.tool._min_h = max(self.tool._min_h, self._min_h)
            self.tool._fit_window()
            self._min_w, self._min_h = self.tool._min_w, self.tool._min_h
            return
        self._min_w, self._min_h = ui_common.fit_window(self.root, self._min_w, self._min_h)

    def _reset_min_size(self) -> None:
        """换页时把窗口下限收回默认值，免得被上一页撑大之后再也缩不回去。"""
        self._min_w, self._min_h = MIN_WINDOW_W, MIN_WINDOW_H

    # ---------- 后台任务：线程 + 队列 + 主线程轮询 ----------
    def _window_alive(self) -> bool:
        try:
            return bool(self.root.winfo_exists())
        except tk.TclError:
            return False

    def _run_in_background(self, work, on_done, *, interval: int = 200,
                           max_attempts: int = 3000, on_progress=None) -> None:
        """把 ``work(report)`` 丢到后台线程，结果与进度都经队列回主线程。

        刻意**不**在工作线程里直接调 ``root.after``：那是跨线程碰 Tcl 解释器，
        在部分 Tcl 构建上会偶发崩溃。用「队列 + 主线程轮询」把线程边界划干净。

        ``work(report)``：``report(value)`` 可以从工作线程上报进度，
        交给主线程的 ``on_progress(value)`` 去画。
        ``on_done(result, error)``：正常时 error 为 None；``work`` 抛异常时
        result 为 None、error 是那个异常对象。
        """
        box: queue.Queue = queue.Queue()

        def report(value) -> None:
            try:
                box.put(("progress", value))
            except Exception as exc:                       # noqa: BLE001
                log.warning("上报进度失败（已忽略）：%s", exc)

        def runner() -> None:
            try:
                box.put(("done", work(report)))
            except Exception as exc:  # noqa: BLE001 - 后台线程的异常必须带回主线
                box.put(("error", exc))

        threading.Thread(target=runner, name="bg-task", daemon=True).start()
        self._poll_background(box, on_done, 1, interval, max_attempts,
                              on_progress)

    def _poll_background(self, box, on_done, attempt: int,
                         interval: int, max_attempts: int,
                         on_progress=None) -> None:
        # 一次把所有排队的消息抽干，进度只取**最新**的那条：
        # 34.5MB 会产生上百条进度，一条一条慢慢显示的话进度条永远落后于实际。
        latest = None
        while True:
            try:
                kind, payload = box.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                latest = payload
                continue
            if on_progress is not None and latest is not None:
                on_progress(latest)        # 收尾前把最后一条进度补上
            if kind == "done":
                on_done(payload, None)
            else:
                on_done(None, payload)
            return
        if latest is not None and on_progress is not None:
            on_progress(latest)
        if not self._window_alive():
            return
        if attempt < max_attempts:
            self.root.after(
                interval,
                lambda: self._poll_background(box, on_done, attempt + 1,
                                              interval, max_attempts,
                                              on_progress),
            )
        else:
            log.warning("后台任务迟迟不回，放弃等待（不影响使用）")

    # ---------- 版本提示 / 更新 ----------
    def _start_update_check(self) -> None:
        self._run_in_background(
            lambda _report: version_check.check_for_update() or "",
            lambda latest, _err: self._on_update_result(latest or ""),
            interval=200, max_attempts=300,   # 60 秒兜底，不留常驻定时器
        )

    def _on_update_result(self, latest: str) -> None:
        if not latest:
            log.debug("版本检查完成：没有新版本")
            return
        self._pending_update = latest
        try:
            if not self._window_alive():
                return
            log.info("提示用户有新版本 %s", latest)
            self.root.title("%s · 有新版本 %s" % (APP_TITLE, latest))
        except tk.TclError:
            log.debug("窗口已关闭，跳过版本提示")
            return
        self._apply_update_text(latest)

    def _make_update_notice(self, parent) -> None:
        """在页面上留出"有新版本"的位置。

        **先建好留空**，查到新版本再填文字 —— 不在异步回调里动态 pack 新控件，
        是为了避免窗口尺寸在用户眼皮底下跳一下。
        """
        holder = tk.Frame(parent, bg=BG_DARK)
        holder.pack(pady=(0, 2))
        self.update_label = tk.Label(
            holder, text="", bg=BG_DARK, fg=FG_WARN, font=("Microsoft YaHei", 9)
        )
        self.update_label.pack(side="left")
        self.update_button = tk.Button(
            holder, text="", command=self._on_update_clicked,
            font=("Microsoft YaHei", 9, "underline"), bg=BG_DARK, fg=FG_WARN,
            activebackground=BG_DARK, activeforeground="#ffffff",
            relief="flat", bd=0, cursor="hand2",
        )
        if self._pending_update:
            self._apply_update_text(self._pending_update)

    def _apply_update_text(self, latest: str) -> None:
        try:
            if self.update_label is not None and self.update_label.winfo_exists():
                self.update_label.configure(
                    text="◇ 发现新版本 %s（当前 %s）" % (latest, version_check.APP_VERSION)
                )
            if self.update_button is not None and self.update_button.winfo_exists():
                self.update_button.configure(
                    text="下载更新" if version_check.MANIFEST_URL
                    else "怎么拿到新版？",
                )
                self.update_button.pack(side="left", padx=6)
            self._fit_window()
        except tk.TclError:
            pass

    def _on_update_clicked(self) -> None:
        if self._downloading:
            messagebox.showinfo("正在下载", "更新正在下载中，请稍候。")
            return
        if self._pending_update is None:
            return
        if not version_check.MANIFEST_URL:
            messagebox.showinfo(
                "怎样拿到新版本",
                "当前版本 %s，最新版本 %s。\n\n"
                "这个安装包没有配置自动下载地址，所以需要向作者索取新的安装包。\n"
                "（作者配置了公开仓库之后，这里就会直接变成下载按钮。）"
                % (version_check.APP_VERSION, self._pending_update),
            )
            return
        if not messagebox.askyesno(
            "下载更新",
            "发现新版本 %s，是否现在下载？\n\n"
            "· 下载到本机后程序只会校验完整性，\n"
            "· **不会自己安装** —— 要装还要你亲手点一下。"
            % self._pending_update,
        ):
            return

        self._downloading = True
        self.root.title("%s · 正在下载更新 %s…" % (APP_TITLE, self._pending_update))
        # 先把进度窗口摆出来，再开始下载 —— 34.5MB 要下二十来秒，
        # 只改标题栏用户根本不知道是不是卡住了（360 那种进度条的价值就在这）。
        self._open_progress(title="正在下载更新 %s" % self._pending_update,
                            subtitle="下载完成后会核对 sha256；装不装由你决定。")

        def work(report):
            report(("", "正在取更新清单…"))
            manifest = version_check.fetch_manifest()
            if not version_check.manifest_is_newer(manifest):
                raise ValueError("清单里的版本（%s）并不比当前版本新"
                                 % manifest.get("version"))
            ok, detail = version_check.download_update(
                manifest, on_progress=lambda d, t, ph: report((d, t, ph)))
            if not ok:
                # 再试一次：这类失败大多是瞬时网络（对朋友那条路由尤其如此），
                # 而重试一次的代价只是几秒钟 —— 比让用户自己去猜为什么失败强得多。
                log.info("下载失败，重试一次：%s", detail)
                ok, detail = version_check.download_update(
                    manifest, on_progress=lambda d, t, ph: report((d, t, ph)))
            if not ok:
                raise ValueError(detail)
            return detail, manifest      # 落地路径 + 清单（要拿它显示 sha256）

        self._run_in_background(work, self._on_download_done,
                                interval=300, max_attempts=4000,
                                on_progress=self._on_download_progress)

    # ---------- 进度窗口（下载/校验时显示） ----------
    def _open_progress(self, *, title: str, subtitle: str = "") -> None:
        self._close_progress()
        try:
            self._progress_dlg = dialogs.ProgressDialog(
                self.root, title=title, subtitle=subtitle)
            self._progress_dlg.present(modal=False)   # 不阻塞、不抢焦点：一边跑一边刷新
        except tk.TclError as exc:
            # 进度窗口不是必需品 —— 开不出来就算了，下载照旧
            log.warning("进度窗口创建失败（已忽略）：%s", exc)
            self._progress_dlg = None

    def _close_progress(self) -> None:
        dlg = self._progress_dlg
        self._progress_dlg = None
        if dlg is not None:
            try:
                dlg.cancel()
            except tk.TclError:
                pass

    def _progress_alive(self):
        """进度窗口还在吗？用户中途关掉它是允许的，之后就别再往上写了。"""
        dlg = self._progress_dlg
        if dlg is None:
            return None
        try:
            if not dlg.winfo_exists():
                self._progress_dlg = None
                return None
        except tk.TclError:
            self._progress_dlg = None
            return None
        return dlg

    def _on_download_progress(self, info) -> None:
        """后台线程报上来的进度（已经回到主线程），画到进度窗口上。

        除了进度条，还顺手写上**下载速度和剩余时间**（算法在
        :func:`version_check.describe_rate`）—— "特别慢"的时候，
        "还要等多久"比"已经下了多少"有用得多：人看到那个数，
        就能自己决定是等，还是先干别的（反正断了也能接着下）。
        窗口关掉就什么都不用做了（用户中途关掉进度窗是允许的）。
        """
        dlg = self._progress_alive()
        if dlg is None:
            return
        try:
            if len(info) == 2:                # ("", "正在取更新清单…")
                dlg.form.set_note(info[1])
                self._progress_mark = None
                return
            done, total, phase = info[0], info[1], info[2]
            dlg.form.set_progress(done, total, phase)
            if phase == "download":
                note, self._progress_mark = version_check.describe_rate(
                    getattr(self, "_progress_mark", None), done, total)
                if note:
                    dlg.form.set_note(note)   # 这句话里也含"已下载 X / Y"，不会丢信息
        except (tk.TclError, IndexError, TypeError) as exc:
            log.warning("更新进度刷新失败（已忽略）：%s", exc)

    # ---------- AI 模型「查更新」（只读清单、比版本，不下载） ----------
    def _check_ai_update(self, on_done) -> None:
        """图片工具点「重新下载 / 更新 AI 模型」时先走这里：只读清单、比版本。

        不下载任何东西，只在后台取一次更新清单，用 ``ai_ops.ai_needs_update``
        比出"远端版本 vs 已装版本"，结果通过 ``on_done`` 回主线程。
        真正的下载仍在 :meth:`_download_ai_models`（用户确认之后才触发）。

        ⚠ 必须走 :meth:`_run_in_background`（队列 + 主线程轮询），
        **不能**在自建线程里直接 ``self.root.after(...)``：那是跨线程碰 Tcl
        解释器，本机实测会抛::

            RuntimeError: main thread is not in main loop

        线程一崩，``on_done`` 就永远不会被调用 —— 界面卡在「正在检查…」，
        用户看到的是"**点了更新 AI 模型什么都没发生**"。
        （2026-09-20 修：这里原来就是自建线程 + root.after。）
        """
        import ai_ops, version_check
        if not ai_ops.ai_supported():
            self.root.after(
                0, lambda: on_done(False, "", ai_ops.ai_component_version(),
                                   ai_ops.support_reason()))
            return
        self._ai_check_cb = on_done

        def work(_report):
            manifest = version_check.fetch_manifest()
            needs, remote_ver, _info = ai_ops.ai_needs_update(manifest)
            # 顺手记下"远端现在是多少" —— 菜单下次拼的时候就能直接写出
            # 「已是最新」/「有更新 vX」，不用再让人点一次才知道。
            # ⚠ 写盘失败不影响这次检查（顶多下次菜单少一句提示）。
            if remote_ver:
                try:
                    ai_ops.set_ai_remote_version(remote_ver)
                except Exception as exc:                  # noqa: BLE001
                    log.warning("记录远端 AI 版本失败（不影响检查）：%r", exc)
            # 报给界面的"已装版本"同样**以文件为准**：models/ 可能被手工删掉、
            # 被杀软清掉、或上次装到一半 —— 那种时候说"你当前是 v1.4.0"会让人
            # 看不懂（明明没有模型，却提示"已是最新"或"你当前是 v1.4.0"）。
            installed_now = (ai_ops.ai_component_version()
                             if ai_ops.models_present() else "")
            return (needs, remote_ver, installed_now)

        self._run_in_background(work, self._ai_check_finished,
                                interval=200, max_attempts=300)   # 60 秒兜底

    def _ai_check_finished(self, result, error) -> None:
        import ai_ops
        cb = getattr(self, "_ai_check_cb", None)
        self._ai_check_cb = None
        if cb is None:
            return
        if error is not None:
            log.warning("AI 组件查更新失败：%r", error)
            cb(False, "", ai_ops.ai_component_version(),
               version_check.describe_check_failure(error))
            return
        needs, remote_ver, installed = result
        cb(needs, remote_ver, installed, None)

    # ---------- AI 模型组件下载（独立组件，体积大，只在它迭代时才重下） ----------
    def _download_ai_models(self, on_done) -> None:
        """图片工具点「下载 AI 模型」时走到这里。

        整条链路都在这层（联网 / 进度 / 解包 / 写状态），图片工具本身不碰网络。
        组件化更新见 ``version_check.parse_components``：AI 模型是一个独立的
        ``ai`` 组件，核心程序小修小补时不会逼用户重下整个大模型。
        """
        import ai_ops
        if not ai_ops.ai_supported():
            on_done(False, ai_ops.support_reason())
            return
        self._ai_download_cb = on_done

        def work(report):
            report(("", "正在取更新清单…"))
            manifest = version_check.fetch_manifest()
            info = manifest.get("ai") \
                or version_check.parse_components(manifest).get("ai")
            if info is None:
                raise ValueError("远端暂未提供 AI 模型组件下载")
            ok, path = version_check.download_component(
                "ai", info, on_progress=lambda d, t, ph: report((d, t, ph)))
            if not ok:                       # 瞬时网络失败重试一次
                ok, path = version_check.download_component(
                    "ai", info, on_progress=lambda d, t, ph: report((d, t, ph)))
                if not ok:
                    raise ValueError(path)
            ok, msg = ai_ops.apply_ai_pack(path, info["version"])
            if not ok:
                raise ValueError(msg)
            # 刚装上的就是远端当时提供的那个版本 —— 顺手记下来，
            # 菜单下次就能直接写「已是最新」，不用再点一次。
            try:
                ai_ops.set_ai_remote_version(info["version"])
            except Exception as exc:                      # noqa: BLE001
                log.warning("记录远端 AI 版本失败（不影响安装）：%r", exc)
            return True, "AI 模型组件 %s 已安装" % info["version"]

        self._open_progress(
            title="正在下载 AI 模型…",
            subtitle="约 90 MB，纯本地推理；下载完会校验完整性。")
        self._run_in_background(
            work, self._on_ai_download_finished,
            interval=300, max_attempts=6000,
            on_progress=self._on_download_progress)

    def _on_ai_download_finished(self, result, error) -> None:
        self._close_progress()
        cb = self._ai_download_cb
        self._ai_download_cb = None
        if cb is None:
            return
        if error is not None:
            cb(False, str(error))
        else:
            ok = bool(result and result[0])
            msg = result[1] if result else "完成"
            cb(ok, msg)

    def _install_ai_pack_local(self, path, on_done) -> None:
        """图片工具点「从本地文件安装 AI 模型包」时走到这里。

        与 _download_ai_models 对称：解包 / 校验 / 写状态都在 ai_ops，本层只负责
        进度窗口与线程；区别是不联网、直接吃用户选好的本地 .pack 文件。
        版本号从包内 ai_manifest.json 读，落到状态里，与远端组件保持一致口径。
        """
        import ai_ops, zipfile, json
        if not ai_ops.ai_supported():
            on_done(False, ai_ops.support_reason())
            return
        if not os.path.isfile(path):
            on_done(False, "AI 模型包不存在：%s" % path)
            return
        # 读包内版本号（与 build_ai_pack 写进 ai_manifest.json 的一致）
        try:
            with zipfile.ZipFile(path) as zf:
                data = json.loads(zf.read("ai_manifest.json"))
                version = data.get("version", ai_ops.REQUIRED_AI_VERSION)
        except Exception as exc:
            on_done(False, "AI 模型包读不出版本号：%s" % exc)
            return
        self._ai_download_cb = on_done

        def work(report):
            report(("", "正在解包 AI 模型…"))
            ok, msg = ai_ops.apply_ai_pack(path, version)
            if not ok:
                raise ValueError(msg)
            return True, "AI 模型组件 %s 已安装（本地）" % version

        self._open_progress(
            title="正在安装 AI 模型…",
            subtitle="从本地文件解包并校验完整性。")
        self._run_in_background(
            work, self._on_ai_download_finished,
            interval=300, max_attempts=6000,
            on_progress=self._on_download_progress)

    def _on_download_done(self, result, error) -> None:
        self._downloading = False
        try:
            self.root.title("%s · 有新版本 %s" % (APP_TITLE, self._pending_update)
                            if error is None else APP_TITLE)
        except tk.TclError:
            pass
        if error is not None:
            log.warning("更新下载失败：%s", error)
            dlg = self._progress_alive()
            if dlg is not None:
                try:
                    dlg.form.set_result(False, "下载失败：%s" % error)
                except tk.TclError:
                    pass
            messagebox.showwarning(
                "更新没有下载成功",
                "原因：\n%s\n\n可以这样继续：\n"
                "· 过一会儿再点一次 —— 网络偶尔会抽风（程序每次启动也会自动重试）\n"
                "· 如果你开着代理 / VPN，先关掉再试一次\n"
                "· 也可以从发布页手动下载安装包：\n  %s\n\n"
                "（更新是可选的：不更新也能照常用这个软件。）"
                % (error, version_check.RELEASE_PAGE_URL),
            )
            return
        path, manifest = result
        # 把**校验结果**摆到进度窗口上（"下完了"和"下完了并且 sha256 对得上"
        # 是两件事，两件都要让用户看见）。
        dlg = self._progress_alive()
        if dlg is not None:
            try:
                dlg.form.set_progress(int(manifest.get("size", 0)),
                                      int(manifest.get("size", 0)), "verify")
                dlg.form.set_result(
                    True, "校验通过 · sha256 与清单一致（%s…）"
                    % str(manifest.get("sha256", ""))[:12])
            except tk.TclError:
                pass
        if not messagebox.askyesno(
            "下载完成",
            "新版本 %s 已经下载并校验通过：\n\n%s\n\n现在运行安装程序吗？\n"
            "（选「否」也可以稍后自己去双击这个文件。）"
            % (manifest.get("version", "?"), path),
        ):
            self._status_line("更新已下载到 %s（未安装）" % os.path.basename(path))
            return
        # 先启动安装向导（它还要用户点"下一步"才会真正写文件），
        # **紧接着退出自己** —— 顺序很重要：Windows 会锁住正在运行的 exe，
        # 而安装器要覆盖的正是这个文件；主程序不让路，安装就会"拒绝访问"。
        # 反过来说，先启动安装器的好处是：万一它起不来，这里还能正常弹提示。
        try:
            # 等价于用户在资源管理器里双击它 —— 但**必须清掉 PyInstaller 的
            # 内部环境变量**再交出去。否则这串变量会一路传下去：
            #   本程序 → Setup.exe → 装完后 Setup.exe 启动的新 MyAlbum.exe
            # 新程序一启动就误判自己是"被别人启动的子进程"，核对父进程可执行
            # 文件发现不是自己 → 弹 "Security validation failure: parent process
            # has different executable!" 并拒绝启动（用户看到的就是一个
            # 完全不认识的 Error 框）。2026-09-17 实测复现。
            shell_ops.spawn_detached(path)
        except OSError as exc:
            log.warning("无法启动更新包：%s", exc)
            messagebox.showerror("打不开", "无法运行更新包：\n\n%s" % exc)
            return
        log.info("安装向导已启动，本程序退出让路（避免目标 exe 被占用）")
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _status_line(self, text: str) -> None:
        """把一句话写到当前页面的状态行（图片工具有一个，别的页面没有）。"""
        if self.tool is not None and self.mode == "tool":
            self.tool._status(text)
        else:
            log.info("%s", text)

    # ---------- 反馈（第一次必须先过隐私说明） ----------
    def _show_feedback(self) -> None:
        """点「反馈」的入口。第一次要先看同意书，同意过就直接进反馈框。

        同意与否**不缓存成"同意了"这个事实本身**，而是记一个**说明版本号**：
        将来这段说明改了内容，把 ``PRIVACY_VERSION`` 加一，所有人会被重新问
        一次 —— 拿"以前同意过"当永久许可是不对的。
        """
        if state_store.needs_feedback_consent(self.state, PRIVACY_VERSION):
            log.info("首次使用反馈：先展示隐私说明 v%d", PRIVACY_VERSION)
            self._show_privacy(first_time=True)
            return
        self._open_feedback_box()

    def _show_privacy(self, *, first_time: bool = False) -> None:
        """展示隐私说明。``first_time`` 时同意后接着打开反馈框。"""

        def on_agree():
            state_store.mark_feedback_consent(self.state, PRIVACY_VERSION)
            log.info("用户同意反馈隐私说明 v%d", PRIVACY_VERSION)
            if first_time:
                self._open_feedback_box()

        def on_decline():
            # 不同意**不记任何东西**：下次点还会再问一次。
            # 记一个"拒绝"很容易变成"这个功能对你永久消失了"，而用户
            # 只是这次不想看而已。
            log.info("用户这次没同意反馈隐私说明（下次还会再问）")

        dialogs.ConsentDialog(
            self.root,
            paragraphs=privacy_paragraphs(),
            note="说明版本 v%d · 收件人 %s" % (PRIVACY_VERSION,
                                              feedback_recipient()),
            on_agree=on_agree,
            on_decline=on_decline,
        ).show()

    def _open_feedback_box(self) -> None:
        """真正的反馈框：在程序里打字，交给系统邮件程序发出去。

        注意这一层**自己不发信**：没有 SMTP、没有账号密码 ——
        只把内容交给用户自己的邮件客户端，由他点发送。
        """

        def on_send(body, reply_to=""):
            # ⚠ 这条路上 ``reply_to`` **保持原样、不额外处理**：
            #   走系统邮件程序时，信是**用户自己**发出去的，对方本来就能直接
            #   回信 —— 不需要（mailto 也没法可靠地）设 Reply-To。
            #   那个地址已经作为"回信邮箱：xxx"写进正文了，作者照样看得到。
            ok, detail = shell_ops.open_mailto(
                feedback_recipient(),
                "HAAVIK Photo 反馈（%s）" % version_check.APP_VERSION,
                body)
            if ok:
                # 用户点了一次"发送" → 把他**这次**留的邮箱记下来（不管有没填）：
                #   · 留了 → 下次打开预填
                #   · 没留 → 下次打开不预填（保持原状）
                state_store.set_last_reply_email(self.state, reply_to)
                # ⚠️ 这里**不能说"发出去了"**：open_mailto 只能保证"信被交给了
                # 系统"，而很多电脑根本没配邮件程序（会弹一个"打开方式"框，
                # 甚至什么都不弹）—— 那时用户以为发了，其实一个字都没出去。
                # 所以这句话要同时给出"没成功长什么样"和"下一步怎么办"。
                return True, ("已把写好的信交给系统邮件程序。\n"
                              "如果没有弹出邮件窗口（多半是这台电脑没配邮件程序），"
                              "请点「复制文字」，粘到微信 / QQ 里发给作者。")
            return False, ("没能打开邮件程序（%s）。改点「复制文字」，"
                           "粘到微信 / QQ 里发给作者也一样。" % detail)

        def on_copy(body):
            ok, detail = shell_ops.copy_text_to_clipboard(body)
            if ok:
                return True, "已复制。粘到邮件或聊天窗口里发给我就行。"
            return False, "复制失败：%s" % detail

        def on_direct(body, reply_to=""):
            """软件内直接发送。具体走哪条由 feedback_transport() 决定。"""
            if feedback_transport() == "smtp":
                # to 留空 = 由凭据决定（SMTP_TO，没配就发信账号自己）——
                # 那段逻辑只写在 feedback_send.effective_recipient() 一处
                # reply_to：用户自愿留的邮箱，那一层会校验后设成 Reply-To
                ok, detail = feedback_send.send_via_smtp(
                    body, version=version_check.APP_VERSION,
                    reply_to=reply_to)
            else:
                # endpoint 那条路：地址已经写在正文里了（composed() 加的），
                # 这一层不用再单独传 —— 表单字段只有那一个，不另开口子。
                ok, detail = feedback_send.send_feedback(
                    FEEDBACK_ENDPOINT, body, version=version_check.APP_VERSION,
                    client_time=datetime.now().isoformat(timespec="seconds"),
                    open_fn=version_check.open_https)
            # 真正发出去了（HTTP 2xx / SMTP 收条）才记 —— 失败的不记，
            # 否则下次预填的可能是"上次网络抽风时用户输到一半的串"
            if ok:
                state_store.set_last_reply_email(self.state, reply_to)
            return ok, detail

        mode = feedback_transport()
        dialogs.FeedbackDialog(
            self.root,
            version_info=feedback_version_info(),
            on_send=on_send if mode == "mailto" else None,
            on_direct=on_direct if mode != "mailto" else None,
            on_copy=on_copy,
            on_policy=self._show_privacy,
            initial_reply=state_store.get_last_reply_email(self.state),
        ).show()

    # ---- 表单出口（layout_test 直接渲染这两块） ----
    def _build_feedback_body(self, top):
        # 这里**故意**给上 on_direct：配了直达通道时界面会多一个按钮，
        # layout_test 要量的是"最宽的那一种"，不是当前配置的那一种。
        return dialogs.build_feedback_body(
            top, version_info=feedback_version_info(),
            on_send=None, on_direct=lambda body, reply_to="": (True, "（测试用）"),
            on_copy=None, on_policy=lambda: None,
            close=top.destroy)

    def _build_consent_body(self, top):
        return dialogs.build_consent_body(
            top, paragraphs=privacy_paragraphs(),
            note="说明版本 v%d · 收件人 %s" % (PRIVACY_VERSION,
                                              feedback_recipient()),
            on_agree=None, on_decline=None, close=top.destroy)

    # ---------- 图片工具页右下角的「检查更新」 ----------
    def _update_from_tool(self) -> None:
        """工具页那个入口被点了。

        已经知道有新版本 → 直接进下载流程（按钮这时写的是「下载更新 x.y.z」）；
        否则去查一次，查完把结果同时写到状态栏和按钮上。
        """
        if self._pending_update:
            self._on_update_clicked()
            return
        self._status_line("正在检查更新…")
        # ⚠ 这里必须是 ``lambda _report: ...``（收一个参数）：``_run_in_background``
        # 内部是 ``work(report)``。写成 ``lambda: ...`` 的话每一轮都会抛
        # ``TypeError: <lambda>() takes 0 positional arguments but 1 was given``，
        # 而那句异常又被当成"网络不通"报给用户 —— 看起来像网坏了，其实是代码错。
        # （2026-09-20 修：老用户"明明是最新版却提示网络不通"就是这个原因。）
        self._run_in_background(
            lambda _report: version_check.fetch_latest(),
            self._on_manual_check_done,
            interval=200, max_attempts=300,      # 60 秒兜底，不留常驻定时器
        )

    def _on_manual_check_done(self, latest, error) -> None:
        """手动检查的回调。

        三种结果要分清楚 —— 网络不通和"已是最新"对用户是完全不同的两件事，
        都含糊地报"已是最新"就是在骗人；反过来把程序自己的毛病说成"网络不通"
        也一样坏（那会让人去查网线，其实该查代码）。
        """
        if error is not None or not latest:
            log.warning("手动检查更新失败：%r", error)
            self._status_line("检查更新失败（%s），稍后再试"
                              % version_check.describe_check_failure(error))
            return
        try:
            newer = (version_check.parse_version(latest) >
                     version_check.parse_version(version_check.APP_VERSION))
        except ValueError:
            log.warning("远端版本号看不懂：%r", latest)
            self._status_line("检查更新失败（远端版本号异常）")
            return

        if not newer:
            if self.tool is not None:
                self.tool.set_update_state(None)
            self._status_line("已是最新版本 v%s" % version_check.APP_VERSION)
            return

        self._pending_update = latest
        self._apply_update_text(latest)
        try:
            self.root.title("%s · 有新版本 %s" % (APP_TITLE, latest))
        except tk.TclError:
            pass
        if self.tool is not None:
            self.tool.set_update_state(latest)
        self._status_line("发现新版本 %s —— 点右下角「下载更新」" % latest)

    # ---------- 页面 1：问答 ----------
    def _build_quiz_page(self) -> None:
        self.mode = "quiz"
        self._clear()
        self._reset_min_size()
        self.root.title(APP_TITLE)
        self.root.configure(bg=BG_DARK)
        state_store.note_quiz_asked(self.state)

        bar = tk.Frame(self.root, bg=BG_DARK, height=38)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        tk.Label(
            bar,
            text="  ◇ HAAVIK Photo  ·  我的相册",
            bg=BG_DARK,
            fg=FG_TEXT,
            font=("Microsoft YaHei", 10),
        ).pack(side="left", pady=9)
        tk.Label(
            bar,
            text="v%s  " % version_check.APP_VERSION,
            bg=BG_DARK,
            fg=FG_DIM,
            font=("Microsoft YaHei", 9),
        ).pack(side="right", pady=11)

        img_frame = tk.Frame(self.root, bg=BG_DARK)
        img_frame.pack(pady=(10, 6))
        self.image_label = tk.Label(img_frame, bg=BG_DARK, bd=0)
        self.image_label.pack()
        self._render_image()

        qf = tk.Frame(self.root, bg=BG_DARK)
        qf.pack(fill="x", padx=30)
        tk.Label(
            qf,
            text="看完图片，请问：天空属于谁？",
            bg=BG_DARK,
            fg="#ffffff",
            font=("Microsoft YaHei", 14, "bold"),
        ).pack(pady=(10, 14))

        bf = tk.Frame(qf, bg=BG_DARK)
        bf.pack()
        for opt in OPTIONS:
            btn = tk.Button(
                bf,
                text=opt.label,
                bg=opt.color,
                fg="white",
                activebackground=opt.color,
                activeforeground="white",
                font=("Microsoft YaHei", 12, "bold"),
                width=14,
                height=2,
                relief="flat",
                cursor="hand2",
                bd=0,
            )
            # 默认参数绑定：避免闭包捕获循环变量（经典延迟绑定 bug）
            btn.configure(command=lambda o=opt: self._on_answer(o))
            btn.pack(side="left", padx=12)

        tk.Label(
            self.root,
            text="提示：单击你的答案",
            bg=BG_DARK,
            fg=FG_DIM,
            font=("Microsoft YaHei", 9),
        ).pack(pady=6)

        self._make_update_notice(self.root)

        if self.real_shutdown:
            # 操作者可见的警示：真实模式必须在界面上留痕
            tk.Label(
                self.root,
                text="⚠ 真实关机模式已启用（--real-shutdown）",
                bg=BG_DARK,
                fg="#e67e22",
                font=("Microsoft YaHei", 8),
            ).pack()

        self._fit_window()

    def _render_image(self) -> None:
        path = resource_path("haavik.png")
        photo = load_image(path) if self.show_image else None
        if photo is None:
            # 占位区用**固定像素**尺寸的容器，而不是 Label 的 width/height
            # （后者的单位是"字符数/文本行数"，会随显示缩放一起放大，
            #   在 150% 缩放下能把窗口撑到屏幕之外）。
            holder = tk.Frame(self.image_label, width=MAX_IMG_W, height=MAX_IMG_H, bg="#222")
            holder.pack_propagate(False)
            holder.pack()
            tk.Label(
                holder,
                text="[ 图片加载失败 ]" if self.show_image else "[ 已跳过图片 ]",
                bg="#222",
                fg="white",
                font=("Microsoft YaHei", 14),
            ).place(relx=0.5, rely=0.5, anchor="center")
            return
        # 必须持有引用，否则被 GC 回收后显示为空
        self._photo = ImageTk.PhotoImage(photo)
        self.image_label.configure(image=self._photo)

    # ---------- 回答 ----------
    def _on_answer(self, opt: Option) -> None:
        if self._armed:
            return

        if opt.correct:
            if messagebox.askyesno("再确认一下", "小子，你确定知道哈夫克？"):
                messagebox.showinfo("回答正确", "答对了，牛逼！\n\n正确答案：%s。" % CORRECT_HINT)
                state_store.mark_unlocked(self.state, state_store.VIA_QUIZ,
                                          version=version_check.APP_VERSION)
                self._show_tool(first_time=True)
                return
            # "答对但不敢确认" → 同样进入惩罚分支（游戏设计如此）
        else:
            messagebox.showwarning("喂！", "什么玩意，不识时务的小子。")

        if self.real_shutdown:
            # 大人自己的玩法：老的 60 秒倒计时（默认不启用）
            self._enter_countdown()
        else:
            # 先把"被锁"落盘，再换页。顺序不能反：锁定必须活得比这个进程久，
            # 关掉重开还得在锁定页（save 自己会记日志，写不进去也查得到）。
            state_store.mark_locked(self.state)
            self._build_lock_page()

    # ---------- 页面 2：软件已锁定 ----------
    def _build_lock_page(self) -> None:
        self.mode = "lock"
        self._clear()
        self._reset_min_size()
        self.root.title("%s · 软件已锁定" % APP_TITLE)
        self.root.configure(bg=ui_common.BG_VIEW)

        wrap = tk.Frame(self.root, bg=BG_ALERT)
        wrap.pack(expand=True, fill="both", padx=60, pady=40)

        tk.Label(
            wrap, text="🔒", bg=BG_ALERT, fg=FG_BAD, font=("Segoe UI Emoji", 34)
        ).pack(pady=(24, 0))
        tk.Label(
            wrap, text="软件已锁定", bg=BG_ALERT, fg=FG_BAD,
            font=("Microsoft YaHei", 26, "bold"),
        ).pack(pady=(4, 6))
        tk.Label(
            wrap,
            text="本软件未通过授权验证，请输入序列号以继续使用。",
            bg=BG_ALERT, fg=FG_TEXT, font=("Microsoft YaHei", 11),
        ).pack(pady=(0, 4))
        tk.Label(
            wrap,
            text="序列号是一串 25 位字符，分 5 组。答对开场那道题不需要序列号。",
            bg=BG_ALERT, fg=FG_DIM, font=("Microsoft YaHei", 9),
        ).pack(pady=(0, 14))

        self.code_entry = CodeEntry(
            wrap, on_submit=self._verify_serial,
            # lock_msg 在下面几行才建出来，所以这里用 lambda 延迟取 ——
            # 这个回调只在用户真的按下粘贴时才跑，那时它一定已经在了。
            on_problem=lambda text: self.lock_msg.configure(text=text, fg=FG_DIM),
            bg=BG_ALERT, fg="#1b1b1b", insertbackground="#1b1b1b",
            relief="flat", bd=0, disabledbackground="#5a5f6a",
        )
        self.code_entry.build().pack(pady=(0, 6))

        # 「粘贴」按钮 + 一句提示。25 位手敲太痛苦，粘贴才是这一页的主要用法；
        # 但"Ctrl+V 能用"这件事不能靠用户猜 —— 所以既给按钮，也写出来。
        paste_row = tk.Frame(wrap, bg=BG_ALERT)
        paste_row.pack(pady=(0, 4))
        tk.Button(
            paste_row, text="粘贴", command=self.code_entry.paste_anywhere,
            font=("Microsoft YaHei", 9), bg=PANEL, fg=FG_TEXT,
            activebackground=ACCENT, activeforeground="white",
            relief="flat", bd=0, cursor="hand2", width=6,
        ).pack(side="left", padx=(0, 6))
        tk.Label(
            paste_row,
            text="直接打字也行；也可以 Ctrl+V（或右键）把整串粘进来",
            bg=BG_ALERT, fg=FG_DIM, font=("Microsoft YaHei", 9),
        ).pack(side="left")

        self.lock_msg = tk.Label(
            wrap, text="", bg=BG_ALERT, fg=FG_DIM, font=("Microsoft YaHei", 9)
        )
        self.lock_msg.pack(pady=(0, 14))

        btns = tk.Frame(wrap, bg=BG_ALERT)
        btns.pack()
        tk.Button(
            btns, text="解除锁定", command=self._verify_serial,
            font=("Microsoft YaHei", 11, "bold"), bg="#27ae60", fg="white",
            activebackground="#2ecc71", activeforeground="white",
            relief="flat", bd=0, cursor="hand2", width=14, height=2,
        ).pack(side="left", padx=8)
        tk.Button(
            btns, text="退出程序", command=self._on_close,
            font=("Microsoft YaHei", 11), bg=PANEL, fg=FG_TEXT,
            activebackground="#2c3038", activeforeground="white",
            relief="flat", bd=0, cursor="hand2", width=12, height=2,
        ).pack(side="left", padx=8)

        hint = tk.Frame(wrap, bg=BG_ALERT)
        hint.pack(pady=(22, 4))
        tk.Label(
            hint,
            text="序列号在哪？—— 作者给你留了一道题，生成器在这个仓库里：",
            bg=BG_ALERT, fg=FG_WARN, font=("Microsoft YaHei", 10, "bold"),
        ).pack(side="left")

        url_row = tk.Frame(wrap, bg=BG_ALERT)
        url_row.pack(pady=(2, 2))
        url_var = tk.StringVar(value=REPO_HINT_URL)
        url_entry = tk.Entry(
            url_row, textvariable=url_var, font=("Consolas", 10), width=42,
            state="readonly", justify="center", bg=PANEL, fg=FG_WARN,
            readonlybackground=PANEL, relief="flat", bd=0,
        )
        url_entry.pack(side="left", ipady=4)
        tk.Button(
            url_row, text="复制", command=lambda: self._copy_url(url_var.get()),
            font=("Microsoft YaHei", 9), bg=PANEL, fg=FG_TEXT,
            activebackground=ACCENT, activeforeground="white",
            relief="flat", bd=0, cursor="hand2", width=6,
        ).pack(side="left", padx=6)
        # 一键打开 —— 省掉"复制 → 切到浏览器 → 粘贴"这三步。
        # 用默认浏览器，不需要用户知道什么叫"地址栏"。
        tk.Button(
            url_row, text="打开", command=lambda: self._open_url(url_var.get()),
            font=("Microsoft YaHei", 9), bg=PANEL, fg=FG_TEXT,
            activebackground=ACCENT, activeforeground="white",
            relief="flat", bd=0, cursor="hand2", width=6,
        ).pack(side="left")

        tk.Label(
            wrap,
            text="仓库里有一个生成器（serial_check.py）。下载后运行它，\n"
                 "它会打印一枚当月有效的序列号，把它填进上面的输入框即可。\n"
                 "（这不是加密保护，是作者留给你的一道题 —— 会读代码就能自己算。）",
            bg=BG_ALERT, fg=FG_DIM, font=("Microsoft YaHei", 9), justify="center",
        ).pack(pady=(8, 20))

        self._fit_window()
        self.code_entry.focus_first()

    def _copy_url(self, text: str) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except tk.TclError:
            log.debug("剪贴板不可用")
            return
        self.lock_msg.configure(
            text="地址已复制。（也可以直接点右边的「打开」，用浏览器打开它。）",
            fg=FG_OK)

    def _open_url(self, text: str) -> None:
        """用默认浏览器打开仓库地址 —— 少三步操作，也少一次"粘错地方"。"""
        ok, detail = shell_ops.open_url(text)
        if ok:
            self.lock_msg.configure(
                text="已用浏览器打开仓库。找不到刚才那个窗口的话，看下任务栏。",
                fg=FG_OK)
        else:
            log.info("打不开仓库地址：%s", detail)
            self.lock_msg.configure(
                text="没能打开浏览器（%s）。可以点「复制」，自己粘到浏览器地址栏。" % detail,
                fg=FG_BAD)

    def _verify_serial(self) -> None:
        raw = self.code_entry.value()
        state_store.note_serial_attempt(self.state)
        ok, why = serial_check.verify(raw)
        self.code_entry.set_ok(ok)
        if not ok:
            log.info("序列号未通过：%s", why)
            self.lock_msg.configure(
                text="%s（已尝试 %d 次）" % (why, self.state.get(
                    state_store.KEY_SERIAL_TRIES, 0)),
                fg=FG_BAD,
            )
            # 把光标放回输入框并全选：改一位就能重试，不用先清空再重新输。
            # 注意顺序 —— focus 在前、select 在后，否则选择会被抢掉。
            self.code_entry.focus_first()
            self.code_entry.select_all()
            return

        log.info("序列号通过：%s", why)
        self.lock_msg.configure(text="验证通过，%s" % why, fg=FG_OK)
        messagebox.showinfo("验证通过", "序列号正确，可以正常使用了。\n\n（%s）" % why)
        state_store.mark_unlocked(self.state, state_store.VIA_SERIAL,
                                  version=version_check.APP_VERSION)
        self._show_tool(first_time=False)

    # ---------- 页面 3：图片工具 ----------
    def _show_tool(self, *, first_time: bool) -> None:
        self.mode = "tool"
        self._clear()
        self._reset_min_size()
        log.info("进入图片工具（本月首次=%s）", first_time)
        self.tool = image_tool.ImageTool(self.root, on_quit=self._on_close,
                                         on_check_update=self._update_from_tool,
                                         on_restart=self._restart_app,
                                         on_feedback=self._show_feedback,
                                         on_download_ai_models=self._download_ai_models,
                                         on_install_ai_pack_local=self._install_ai_pack_local,
                                         on_check_ai_update=self._check_ai_update)
        self.tool.build()
        self.update_label = self.tool.update_label
        self.update_button = None
        if self._pending_update:
            self._apply_update_text(self._pending_update)
            self.tool.set_update_state(self._pending_update)
        # 顺手把示例图打开，让"这是个能用的图片工具"一眼可见。
        # ⚠️ 这里**不能**拿 first_time 当条件。_show_tool 每次都新建一个
        # ImageTool，此刻它必然是空的；而 first_time 只在"答对题目"那条路上
        # 是 True，"输序列号"那条路传的是 False —— 于是用序列号解锁的使用者
        # 看到的是一个空白的图片工具（就是"图片加载不出来"）。
        # 正确的条件是"工具里没图"：谁走到这一页，都该看到一张能动手玩的图。
        if self.show_image and not self.tool.has_image():
            sample = resource_path("haavik.png")
            if os.path.isfile(sample):
                self.tool.load_file(sample)
        self._fit_window()

    # ---------- 页面 4：倒计时（仅 --real-shutdown） ----------
    def _enter_countdown(self) -> None:
        if self._armed:
            return
        self._armed = True
        self.mode = "countdown"

        self._clear()
        self._reset_min_size()
        self.root.configure(bg=BG_ALERT)

        if self.real_shutdown:
            # 把倒计时交给系统：屏幕上的秒数和系统倒计时同源，且 shutdown /a 随时可撤
            ok, detail = run_shutdown(["/s", "/t", str(self.seconds)])
            if not ok:
                log.warning("真实关机未能下达（%s），降级为演示模式", detail)
                self.real_shutdown = False

        self.countdown = Countdown(self.seconds, self._on_expire)
        self.countdown.start()
        self._expired = False        # 新一轮倒计时，允许再翻一次揭晓页

        self._build_warning_page()
        self._fit_window()

    def _on_expire(self) -> None:
        """倒计时归零 —— 注意：这是在**后台线程**里被调用的。

        ⚠ 这里绝对不能碰任何 Tk 控件，也**不能**调 ``root.after``：
        那是在跨线程碰 Tcl 解释器，本机实测会抛
        ``RuntimeError: main thread is not in main loop``。线程一崩，
        揭晓页就永远不出现 —— 秒数停在 0、页面卡死。

        真正"翻页"由主线程的 :meth:`_tick` 负责（它本来就每 200ms 看一次
        剩余时间）。这里只做**与界面无关**的事。
        """
        if self.real_shutdown:
            # 系统已经在走它自己的倒计时了，这里什么都不做
            log.info("倒计时结束，交由系统执行关机")
            return
        log.info("倒计时归零（揭晓页交由主线程的 _tick 翻）")

    def _build_warning_page(self) -> None:
        wrap = tk.Frame(self.root, bg=BG_ALERT)
        wrap.pack(expand=True)

        tk.Label(
            wrap, text="! 危险 !", bg=BG_ALERT, fg=FG_BAD, font=("Microsoft YaHei", 40, "bold")
        ).pack(pady=(30, 10))
        tk.Label(
            wrap,
            text="回答错误，系统将在以下秒数后自动关机：",
            bg=BG_ALERT,
            fg="white",
            font=("Microsoft YaHei", 12),
        ).pack(pady=10)

        self.cd_label = tk.Label(
            wrap, text=str(self.seconds), bg=BG_ALERT, fg="#f39c12", font=("Arial", 72, "bold")
        )
        self.cd_label.pack(pady=10)

        tk.Label(
            wrap,
            text="正确答案：天空属于 哈夫克",
            bg=BG_ALERT,
            fg="#bdc3c7",
            font=("Microsoft YaHei", 11),
        ).pack(pady=10)

        tk.Button(
            wrap,
            text="[ 紧急取消关机 ]",
            command=self._do_cancel,
            font=("Microsoft YaHei", 13, "bold"),
            bg="#27ae60",
            fg="white",
            relief="flat",
            cursor="hand2",
            width=22,
            height=2,
            bd=0,
            activebackground="#2ecc71",
            activeforeground="white",
        ).pack(pady=18)

        self._tick()

    def _tick(self) -> None:
        """UI 秒数完全由 deadline 推导，与触发逻辑同源，不会漂移。"""
        if self.countdown is None or not self.cd_label.winfo_exists():
            return
        left = self.countdown.remaining()
        if left <= 0:
            self.cd_label.configure(text="0")
            # ⚠ 归零后的揭晓页**必须在这里（主线程）翻**。
            # 曾经靠倒计时线程里 ``root.after(0, self._show_reveal)`` 来翻 ——
            # 那是在跨线程碰 Tcl 解释器，本机实测会抛
            # ``RuntimeError: main thread is not in main loop``，
            # 于是秒数停在 0、页面再也不动：看起来就是"程序卡住了"。
            # （2026-09-20 修：改由这个每 200ms 跑一次的主线程循环来翻。）
            self._tick_job = None
            if not self._expired:
                self._expired = True
                self._show_reveal()
            return
        self.cd_label.configure(text=str(int(left + 0.999)))
        self._tick_job = self.root.after(200, self._tick)

    def _show_reveal(self) -> None:
        """演示模式归零后的揭晓页。修掉旧版"只显示关机中…然后永远卡住"的问题。"""
        if not self._window_alive():
            return
        self._clear()
        self.root.configure(bg="#0f2016")

        wrap = tk.Frame(self.root, bg="#0f2016")
        wrap.pack(expand=True)
        tk.Label(
            wrap, text="骗你的 😄", bg="#0f2016", fg=FG_OK, font=("Microsoft YaHei", 40, "bold")
        ).pack(pady=(40, 16))
        tk.Label(
            wrap,
            text="这只是个整蛊程序，你的电脑没有任何事。\n\n"
            "（本次未调用任何系统关机命令）\n\n"
            "不过正确答案还是要记住：%s" % CORRECT_HINT,
            bg="#0f2016",
            fg="#ecf0f1",
            font=("Microsoft YaHei", 12),
            justify="center",
        ).pack(pady=12)
        tk.Button(
            wrap,
            text="关闭",
            command=self.root.destroy,
            font=("Microsoft YaHei", 12, "bold"),
            bg="#27ae60",
            fg="white",
            relief="flat",
            cursor="hand2",
            width=16,
            height=2,
            bd=0,
        ).pack(pady=20)
        self._fit_window()

    # ---------- 通用 ----------
    def _clear(self) -> None:
        for child in self.root.winfo_children():
            child.destroy()
        self.tool = None
        self.update_label = None
        self.update_button = None
        self.image_label = None

    def _do_cancel(self) -> None:
        self._cancel_pending()
        messagebox.showinfo("已取消", "✓ 关机已成功取消。\n\n记住正确答案：%s！" % CORRECT_HINT)
        self.root.destroy()

    def _cancel_pending(self) -> None:
        """统一的取消入口：UI 取消、关窗、异常退出都走这里。"""
        if self.countdown is not None:
            self.countdown.cancel()
        if self._tick_job is not None:
            try:
                self.root.after_cancel(self._tick_job)
            except tk.TclError:
                pass
            self._tick_job = None
        if self._armed and self.real_shutdown:
            run_shutdown(["/a"])

    def _on_close(self) -> None:
        """关窗 = 取消。旧版直接退出，daemon 线程被强杀，
        于是"关窗能逃掉、点取消反而不一定"——行为不一致，这里统一。"""
        if self._armed:
            self._cancel_pending()
            if not messagebox.askokcancel("确认退出", "已为你取消关机，确定要关闭程序吗？"):
                return
        self.root.destroy()

    # ---------- 左下角白点的隐藏入口：重新开始 ----------
    def _restart_app(self) -> None:
        """口令通过后：清掉过闸记录 → 拉起一个新进程 → 关掉当前这个。

        为什么是"新进程"而不是"就地重置界面"：要的就是**从头开始**
        —— 问答页 → 答错 → 锁定页 → …整条链路。这条链路的状态（页面栈、
        倒计时闸门、示例图只开一次等）散在 App 里，就地重置等于把这些
        再实现一遍。拉起新进程 = 100% 复现"朋友第一次双击"的那一刻。

        安全边界（和本项目其它地方一致）：
          * 只有**用户点了那 10 下并输对口令**才会走到这里；
          * 不做静默自我替换：新进程是"再启动一个自己"，当前进程**正常退出**，
            没有 MoveFileEx / helper 进程那套改写正在运行的文件的手法。
        """
        log.info("口令验证通过：清过闸记录并重新启动")
        try:
            state_store.reset_gate(self.state)      # 从头开始（重新出题）
        except Exception as exc:                    # noqa: BLE001
            log.warning("清过闸记录失败（继续重启）：%s", exc)
        try:
            if getattr(sys, "frozen", False):
                # 打包后：sys.executable 就是桌面上的那个 MyAlbum.exe。
                # ⚠️ 必须走 spawn_detached 而不是 os.startfile：**要把 PyInstaller
                # 的内部环境变量（_PYI_ARCHIVE_FILE 等）从子进程环境里清掉**，
                # 否则新进程会误判自己是"被别人启动的子进程"，去核对父进程
                # 可执行文件、发现对不上，然后弹
                # "Security validation failure: parent process has different
                #  executable!" 并拒绝启动。2026-09-17 实测复现过。
                shell_ops.spawn_detached(sys.executable)
            else:
                # 源码运行：得把脚本路径带上，否则只是又开一个解释器。
                # 同样清环境 —— 从打包版里被拉起的情况不该再踩一次。
                subprocess.Popen([sys.executable, os.path.abspath(sys.argv[0])],
                                 env=shell_ops.clean_child_env())
        except (OSError, ValueError) as exc:
            log.error("重启失败：%s", exc)
            messagebox.showerror("重启失败",
                                 "没能拉起新进程：\n\n%s\n\n请手动重新打开程序。" % exc)
            return
        try:
            self.root.destroy()
        except tk.TclError:
            pass                # 这 150ms 里窗口可能已经被用户关掉了


# --------------------------------------------------------------------------
# 自检（--selfcheck）：不开窗，只回答"这个 exe 能不能跑起来"
# --------------------------------------------------------------------------
#: 构建时生成的模块清单文件名。内容由 build.py 扫源码 AST 得出后塞进包里，
#: **不是手写的** —— 手写第二份迟早会和源码脱节，而脱节的清单只会给出假绿灯。
SELFCHECK_MODULES = "selfcheck_modules.txt"


def selfcheck_report_path() -> str:
    """自检报告写哪。

    打包出来的是 windowed exe，没有 stdout，print 出去的东西谁也看不见。
    所以结论必须落盘，否则"失败原因"永远查不到。
    """
    base = os.environ.get("TEMP") or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "haavik_selfcheck.log")


def selfcheck() -> int:
    """验证这个 exe 是否具备启动条件，全程不开任何窗口。

    返回 0 = 通过；1 = 有问题（细节写进 selfcheck_report_path()）。

    这一条是 v1.6.0 那次事故补的：build.py 的 EXCLUDES 里一直躺着 smtplib，
    直到反馈发信那条路开始 import 它 —— 打出来的 exe 一双击就
    ModuleNotFoundError。本地跑源码永远测不出来（本地什么模块都有），
    只有**真的把打包后的 exe 启动一次**才测得出来。
    所以：发版前必须跑它（release.py），装完也必须跑它（installer.py）。
    """
    problems: list[str] = []
    notes: list[str] = []

    # ① 内置资源还在不在。历史上漏过 --add-data haavik.png，
    #    当时的表现是"程序能开，只是图片不显示"，没有任何一项检查能发现。
    try:
        if not os.path.isfile(resource_path("haavik.png")):
            problems.append("内置图片 haavik.png 不在包里（--add-data 漏了？）")
    except Exception as exc:  # noqa: BLE001 - 自检里任何异常都要变成一句人话
        problems.append("resource_path() 自己就出错了：%s: %s"
                        % (type(exc).__name__, exc))

    # ② 源码 import 过的模块，在这个 exe 里每一个都能 import 到吗。
    #    少任何一个，程序启动瞬间就是 ModuleNotFoundError。
    list_path = resource_path(SELFCHECK_MODULES)
    if os.path.isfile(list_path):
        with open(list_path, "r", encoding="utf-8") as fh:
            names = [ln.strip() for ln in fh if ln.strip()]
        for name in names:
            try:
                __import__(name)
            except Exception as exc:  # noqa: BLE001 - 抓的就是 ModuleNotFoundError
                problems.append("import %s 失败：%s: %s"
                                % (name, type(exc).__name__, exc))
    elif getattr(sys, "_MEIPASS", ""):
        # 打包后清单必须存在 —— 没有它，"少了个模块"这类问题就查不出来了
        problems.append("自检清单 %s 不在包里（构建时没塞进来？）" % SELFCHECK_MODULES)
    else:
        # 源码直接跑：清单本来就是构建时才生成的，这一项不适用，
        # 但要写明白，别让人误以为"没检查"等于"检查过了"。
        notes.append("源码模式下跳过模块检查（清单是构建时生成的）")

    # ③ 版本号读得出来吗。读不出来「检查更新」会一直报错。
    try:
        if not getattr(version_check, "APP_VERSION", ""):
            problems.append("version_check.APP_VERSION 是空的")
    except Exception as exc:  # noqa: BLE001
        problems.append("读版本号失败：%s: %s" % (type(exc).__name__, exc))

    write_err = ""
    text = "\n".join(
        ["HAAVIK Photo 自检 —— %s" % ("通过" if not problems else "有问题")]
        + ["    " + p for p in problems]
        + ["    （%s）" % n for n in notes]
    )
    try:
        with open(selfcheck_report_path(), "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError as exc:
        # 报告写不进去也算有问题：那就等于"失败了还说不出原因"
        write_err = "自检报告也写不进去（%s）：%s" % (selfcheck_report_path(), exc)
        text += "\n    " + write_err

    print(text)  # 源码直接跑时看得见；打包后这行是空转，靠上面的文件
    return 0 if not problems and not write_err else 1


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HAAVIK Photo（图片工具 + 每周问答闸门）")
    parser.add_argument(
        "--real-shutdown",
        action="store_true",
        help="答错时走「真关机倒计时」（默认关闭；默认改走「软件已锁定」页）",
    )
    parser.add_argument(
        "--seconds", type=int, default=COUNTDOWN_DEFAULT,
        help="倒计时秒数（默认 60，仅配合 --real-shutdown 才有意义）",
    )
    parser.add_argument("--no-image", action="store_true", help="跳过图片加载")
    parser.add_argument(
        "--no-update-check",
        action="store_true",
        help="关闭启动时的版本查询（完全离线运行）",
    )
    parser.add_argument(
        "--reset-gate",
        action="store_true",
        help="清掉「本月已过闸」，下次启动重新出题（给自己反复演示用）",
    )
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    parser.add_argument(
        "--selfcheck",
        action="store_true",
        help="不开窗，只检查这个 exe 能不能跑起来（0=能跑，1=有问题）；"
             "报告写在 %%TEMP%%\\haavik_selfcheck.log",
    )
    return parser.parse_args(argv)


def setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def install_excepthook() -> None:
    """窗口化程序没有控制台，未捕获异常会静默消失。至少要弹个框。"""

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return sys.__excepthook__(exc_type, exc, tb)
        log.critical("未捕获异常", exc_info=(exc_type, exc, tb))
        try:
            messagebox.showerror("程序错误", "发生未预期的错误：\n\n%s: %s" % (exc_type.__name__, exc))
        except Exception:  # noqa: BLE001 - 弹框失败也不能再抛
            pass

    sys.excepthook = hook


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.debug)
    install_excepthook()

    if args.selfcheck:
        return selfcheck()

    if args.seconds <= 0:
        args.seconds = COUNTDOWN_DEFAULT

    state = state_store.load()
    if args.reset_gate:
        state_store.reset_gate(state)
        log.info("--reset-gate：已清掉过闸记录，本次启动会重新出题")

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        log.critical("无法初始化图形界面：%s", exc)
        return 1

    App(
        root,
        real_shutdown=args.real_shutdown,
        seconds=args.seconds,
        show_image=not args.no_image,
        check_updates=not args.no_update_check,
        state=state,
    )
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
