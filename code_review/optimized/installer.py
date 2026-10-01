# -*- coding: utf-8 -*-
"""HAAVIK Photo 安装引导器（优化版 · 单文件双架构）。

为什么这个 exe 能在 32 位和 64 位 Windows 上都能跑
==================================================================
Windows 的 WoW64 子系统只向下兼容、不向上兼容：

    * 32 位 exe  ->  32 位 Windows   能跑（原生）
                  ->  64 位 Windows   能跑（WoW64）
    * 64 位 exe  ->  32 位 Windows   **加载失败**（连入口都进不去）

因此"一个安装包通吃两种架构"的唯一正解是**把安装器本身编译成 32 位**，
再由它在运行时判断目标系统的架构，释放对应的主程序：

    目标系统 64 位  ->  释放 x64 的 MyAlbum.exe
    目标系统 32 位  ->  释放 x86 的 MyAlbum_x86.exe

判断逻辑见 detect_arch()：必须读 PROCESSOR_ARCHITEW6432（系统视角），
而不是只看 PROCESSOR_ARCHITECTURE（进程视角），否则 32 位安装包在
64 位系统上会把自己误判成 32 位系统，释放错版本。

为了"不卡顿"做的事
==================================================================
1. 载荷放在 ``payload.zip`` 里按需读取，而不是塞成几十 MB 的 Python 源码。
   原方案要求 Python 在启动时解析/反序列化一个 36 MB 的字符串常量，
   在 32 位进程里既慢又吃内存；改成 zip 后是「打开文件 + 读指定条目」。
2. 所有磁盘 IO、休眠、子进程调用都在后台线程，UI 更新一律经 root.after
   回到主线程 —— 界面永远不会卡住。
3. XOR 用整块 int 异或，而不是逐字节生成器（17 MB 从秒级降到百毫秒级）。
4. 打包时用 --exclude-module 裁掉用不到的 stdlib，缩短解包时间。

安装完成后磁盘上多了什么
==================================================================
**一个文件**：``<你选的文件夹>\\MyAlbum.exe``（默认就是桌面）。
不建快捷方式、不生成任何附带目录。

安装器自己还会写一处诊断日志：``%LOCALAPPDATA%\\HAAVIK Photo\\install.log``，
仅在安装失败时用来告诉用户"详细原因在哪"。除此之外不碰注册表、不装服务、
不设自启动 —— 卸载就是把那个 exe 删掉。

本文件原名 setup.py。在项目根目录放一个叫 setup.py 的文件是个陷阱：
pip / setuptools 会自动发现并尝试执行它，因此改名为 installer.py。
"""
from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import zipfile
import zlib
from tkinter import filedialog, messagebox

# 允许以包内相对位置导入（源码运行时）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import payload_codec as codec  # noqa: E402

# 错误报告发信：复用 feedback_send.py 这一个「唯一的发信出口」
# （审计要求 smtplib 只能出现在 feedback_send.py 里）。这样安装/更新失败时，
# 用户点一下就能把报告发到作者邮箱，不用手动拷文字。
# ⚠ 代价：Setup.exe 会间接带上 feedback_send 的依赖（version_check / urllib 等），
# 但安装器运行时只用 feedback_send 的 smtp 发信那段，且仅在用户点击「发送」时联网——
# 这是为「装失败能一键上报」引入的唯一联网出口，已在审计白名单内。
import feedback_send  # noqa: E402

APP_NAME = "HAAVIK Photo"
# 安装器自身不 import version_check（版本号只能手写同步）：
# build.py 的 check_toolchains() 有一条断言，本值必须等于 version_check.APP_VERSION，
# 否则构建失败。报告里要写版本号时直接用这份字面量，不依赖 version_check。
APP_VERSION = "1.8.11"

#: 主程序源码仓库（用户在免责声明里能点过去自己审计）。
#: 安装器刻意**不** import version_check（那会带进 urllib，setup.exe 的依赖图
#: 会多一条联网模块），所以这个常量是手写的；同步责任在 build.py 里。
REPO_HINT_URL_INSTALLER = "https://gitee.com/whr3443kklld/haavk-photos---haavk-photo"

BUNDLE_NAME = "payload.zip"
BUNDLE_META = "meta.json"

#: 释放出来的主程序文件名。**唯一来源** ——
#: 安装器写它、完成页显示它、卸载脚本删它，都要引用这里，
#: 免得哪天改名只改了其中一处（"装完了但卸载删不掉"就是这么来的）。
PROGRAM_FILENAME = "MyAlbum.exe"

# 布局下限。实际窗口尺寸由 _fit_window() 按页面内容实测后决定，
# 不再硬编码 —— 硬编码尺寸在 125%/150% 显示缩放下必然裁切底部控件。
MIN_WINDOW_W = 700
MIN_WINDOW_H = 580

#: 安装结果 = **一个 exe**。
#:
#: 1.1.0 及以前会额外往安装目录写一批"伪装资源"（许可协议、示例壁纸、
#: 伪造的 .dll/.icc/.ttf），让目录看起来像个正经的图片软件。现在不这么做了，
#: 理由有三条，任何一条都足够：
#:   * 主程序本身已经是个**真能用**的图片工具，不需要布景来证明自己；
#:   * 用户明确要求"释放出来就是一个 EXE"，一个文件夹不方便转发；
#:   * 那些文件没有任何代码读取（伪造 DLL 还会引入 DLL 搜索顺序的风险面），
#:     留着只是给审计报告添一批要解释的东西。
#: 容器格式仍然支持 ``resources`` 字段，只是当前构建不再往里放东西。
INSTALL_SINGLE_EXE = True

DEBUG = bool(os.environ.get("HAAVIK_DEBUG"))
log = logging.getLogger("haavik.installer")


def _probe_writable(folder: str) -> bool:
    """真写一个临时文件再删掉 —— 比 ``os.access(W_OK)`` 可靠。

    Windows 上 ``os.access`` 只看只读属性位，对"目录 ACL 拒绝写入"
    这类情况会给出错误答案（说能写，实际写不下去）。
    """
    try:
        fd, probe = tempfile.mkstemp(prefix=".haavik_probe_", dir=folder)
    except OSError as exc:
        log.warning("目录不可写：%s（%s）", folder, exc)
        return False
    try:
        os.close(fd)
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass
    return True


def default_install_dir() -> str:
    """默认安装位置：**桌面**。

    2026-09-13 起改成直接放桌面，依据是明确的用户要求：
    「安装程序用完之后，释放的文件是直接放在桌面上的 EXE，
      不需要再创快捷方式了」。

    这一改同时去掉了两个中间角色：
      * 原来的 ``D:\\HAAVIK Photo`` 目录（装完还得再建个快捷方式指过去，多一跳）；
      * 快捷方式本身 —— exe 已经在桌面上了，再放个 .lnk 指回桌面纯属重复，
        而且会多出一个"删了 exe 还留着 .lnk"的状态。

    结果：桌面上多出 ``MyAlbum.exe`` 这**一个文件**，双击即用、右键即删。

    万一桌面不可写（组策略只读、安全软件锁定），退到用户目录下的
    ``Programs\\HAAVIK Photo``，保证任何机器上都装得下去，不静默失败。
    """
    desktop = get_desktop_dir()
    if os.path.isdir(desktop) and _probe_writable(desktop):
        return desktop

    fallback = os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Programs", APP_NAME
    )
    log.warning("桌面不可用（%s），回退到 %s", desktop, fallback)
    return fallback


def resource_path(rel: str) -> str:
    """兼容 PyInstaller 解包目录 (sys._MEIPASS) 与源码直接运行。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


# ==========================================================================
# 日志 / 控制台
# ==========================================================================
def setup_logging() -> None:
    handlers = []
    log_dir = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_NAME)
    try:
        os.makedirs(log_dir, exist_ok=True)
        handlers.append(logging.FileHandler(os.path.join(log_dir, "install.log"), encoding="utf-8"))
    except OSError:
        pass  # 日志写不了绝不能影响安装主流程
    if DEBUG and sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.DEBUG if DEBUG else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers or None,
        force=True,
    )


def silence_console_if_frozen() -> None:
    """--noconsole 打包后 stdout/stderr 可能指向无效句柄，写日志会抛异常。

    保留 HAAVIK_DEBUG=1 可在排查问题时恢复输出。
    """
    if getattr(sys, "frozen", False) and not DEBUG:
        try:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
        except OSError:
            pass


# ==========================================================================
# 载荷容器
# ==========================================================================
class BundleError(RuntimeError):
    """载荷容器缺失、结构不合法或内容损坏。"""


class Bundle:
    """``payload.zip`` 的读取封装。

    容器结构::

        meta.json                 索引 + 校验信息
        payload/x64.bin           64 位主程序（XOR 混淆）
        payload/x86.bin           32 位主程序（XOR 混淆）
        res/<name>                伪装资源（原始字节）

    用 zip 而不是超大 Python 源码：启动时只需读 meta.json（几百字节），
    真正需要哪个载荷才读哪个，32 位进程也不会因为一次性载入几十 MB 而卡顿。
    """

    def __init__(self, path: str) -> None:
        self.path = path
        if not os.path.isfile(path):
            raise BundleError("安装包不完整：找不到 %s" % os.path.basename(path))
        try:
            self._zip = zipfile.ZipFile(path, "r")
            raw = self._zip.read(BUNDLE_META)
            self.meta = json.loads(raw.decode("utf-8"))
        except (OSError, zipfile.BadZipFile, KeyError, ValueError) as exc:
            raise BundleError("载荷容器无法读取或已损坏：%s" % exc) from exc

        if not isinstance(self.meta.get("payload"), dict):
            raise BundleError("载荷容器结构不合法：缺少 payload 索引")
        log.info(
            "载荷容器已加载：%s（构建于 %s，%d 个架构）",
            os.path.basename(path),
            self.meta.get("created", "未知"),
            len(self.meta["payload"]),
        )

    def payload(self, arch: str):
        """取出并校验指定架构的主程序。

        Returns:
            (原始字节, 原始长度, crc32)
        """
        info = self.meta["payload"].get(arch)
        if not info:
            raise BundleError("容器中缺少 %s 架构的主程序" % arch)

        member = info["member"]
        try:
            raw = self._zip.read(member)
        except (KeyError, zipfile.BadZipFile) as exc:
            raise BundleError("读取 %s 失败：%s" % (member, exc)) from exc

        if info.get("obfuscated"):
            raw = codec.deobfuscate(raw)

        data = codec.verify(
            raw,
            expect_pe=True,
            expected_size=info.get("size"),
            expected_crc=info.get("crc32"),
        )
        return data, len(data), codec.crc32(data)

    def resources(self):
        """返回 {资源名: 原始字节}，缺失项不含在结果里（由调用方记录）。"""
        out = {}
        for name, info in (self.meta.get("resources") or {}).items():
            try:
                raw = self._zip.read(info["member"])
            except (KeyError, zipfile.BadZipFile) as exc:
                log.warning("资源 %s 读取失败：%s", name, exc)
                continue
            if info.get("crc32") and codec.crc32(raw) != info["crc32"]:
                log.warning("资源 %s 校验和不匹配，已跳过", name)
                continue
            out[name] = raw
        return out


# ==========================================================================
# 系统检测
# ==========================================================================
WIN_VERSIONS = {
    (4, 0): "Windows 95",
    (4, 10): "Windows 98",
    (4, 90): "Windows ME",
    (5, 0): "Windows 2000",
    (5, 1): "Windows XP",
    (5, 2): "Windows Server 2003 / XP 64-bit",
    (6, 0): "Windows Vista",
    (6, 1): "Windows 7",
    (6, 2): "Windows 8",
    (6, 3): "Windows 8.1",
    (10, 0): "Windows 10 / 11",
}
MIN_SUPPORTED = (6, 0)  # Vista
WIN11_BUILD = 22000


def detect_windows_version():
    """返回 (major, minor, 完整名称, 是否受支持)。

    修正：Windows 11 的 major.minor 同样是 10.0，只靠版本号无法区分，
    这里补充 build number 判断，界面上的"检测到的系统"才不会把 11 显示成 10。
    """
    try:
        v = sys.getwindowsversion()
    except Exception as exc:  # noqa: BLE001
        log.warning("无法获取 Windows 版本：%s", exc)
        return 0, 0, "未知 / %s" % exc, False

    major, minor = v.major, v.minor
    name = WIN_VERSIONS.get((major, minor), "Windows %d.%d" % (major, minor))
    if (major, minor) == (10, 0) and getattr(v, "build", 0) >= WIN11_BUILD:
        name = "Windows 11"
    return major, minor, name, (major, minor) >= MIN_SUPPORTED


def detect_arch() -> str:
    """判断**操作系统**架构，而不是当前进程架构。

    32 位安装包跑在 64 位 Windows 上时：
        PROCESSOR_ARCHITECTURE  = x86      （进程视角）
        PROCESSOR_ARCHITEW6432  = AMD64    （系统视角，仅 WOW64 下存在）

    原实现只看第一个变量，会把 64 位系统判成 32 位，
    于是给 64 位机器释放了 32 位主程序。
    这正是"双架构安装包"最容易出错、也最难被发现的地方。
    """
    if os.environ.get("PROCESSOR_ARCHITEW6432"):
        return "x64"
    arch = os.environ.get("PROCESSOR_ARCHITECTURE", "").upper()
    if arch in ("AMD64", "IA64", "ARM64"):
        return "x64"
    if sys.maxsize > 2 ** 32:
        return "x64"
    return "x86"


def describe_self() -> str:
    """当前安装器自身的位数，写进日志便于排查"为什么释放了错版本"。"""
    return "64" if sys.maxsize > 2 ** 32 else "32"


def show_incompatible_and_abort() -> None:
    message = (
        "%s 不支持当前版本的 Windows。\n\n"
        "本程序需要 Windows Vista 或更高版本。\n"
        "(Windows 95 / 98 / ME / 2000 / XP 均不兼容)\n\n"
        "安装程序将退出。" % APP_NAME
    )
    try:
        _show_report_dialog("系统不兼容", message, parent=None)
    except tk.TclError:
        log.warning("tkinter 不可用，改用 PowerShell 提示")
        script = (
            "Add-Type -AssemblyName PresentationFramework; "
            "[System.Windows.MessageBox]::Show(%r, '系统不兼容', 'OK', 'Error')" % message
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("PowerShell 提示同样失败：%s", exc)
    sys.exit(1)


# ==========================================================================
# 文件系统操作
# ==========================================================================
def _run_powershell(script: str, timeout: int = 20):
    """调用 PowerShell，**编码全程可控**。返回 (returncode, stdout, stderr) 或 None。

    原实现踩了两个坑，都会导致"静默用错结果"：

    1. **脚本传入**：把脚本直接塞进命令行参数，在中文用户名/中文路径下会被
       控制台代码页破坏。→ 改用 ``-EncodedCommand``（UTF-16LE + base64），
       命令行上只剩纯 ASCII，任何代码页都不会损坏脚本。
    2. **结果读回**：PowerShell 的输出按控制台代码页编码（中文系统是 GBK），
       而 ``subprocess.run(text=True)`` 按 locale/UTF-8 解码。一旦输出里含中文
       （例如桌面路径里的用户名），解码就抛 UnicodeDecodeError，**读线程直接
       崩掉、stdout 变成空字符串**，调用方却看不到异常 —— 于是静默走到错误的
       分支（实测就是桌面路径变空、回退到旧桌面）。

    这里改为：脚本内先设 ``[Console]::OutputEncoding = UTF8``，再以字节形式
    取回结果并显式按 UTF-8 解码、``errors="replace"`` 兜底，绝不因编码崩掉。
    """
    wrapped = "[Console]::OutputEncoding=[Text.Encoding]::UTF8;\n" + script
    encoded = base64.b64encode(wrapped.encode("utf-16-le")).decode("ascii")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("PowerShell 调用失败：%s", exc)
        return None

    out = (proc.stdout or b"").decode("utf-8", "replace").strip()
    err = (proc.stderr or b"").decode("utf-8", "replace").strip()
    return proc.returncode, out, err


# --------------------------------------------------------------------------
# 桌面路径：直接问 shell32，不走子进程
# --------------------------------------------------------------------------
class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


#: FOLDERID_Desktop —— "用户可见的那个桌面"
FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"
CSIDL_DESKTOPDIRECTORY = 0x0010
MAX_PATH_LEN = 260


def _desktop_via_known_folder():
    """SHGetKnownFolderPath(FOLDERID_Desktop)：Vista+ 的官方方式。"""
    try:
        guid = _GUID()
        if ctypes.windll.ole32.CLSIDFromString(
            ctypes.c_wchar_p(FOLDERID_DESKTOP), ctypes.byref(guid)
        ) != 0:
            return None

        ptr = ctypes.c_void_p()
        shell32 = ctypes.windll.shell32
        shell32.SHGetKnownFolderPath.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        ]
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        hr = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(ptr))
        if hr != 0 or not ptr.value:
            return None

        try:
            return ctypes.wstring_at(ptr)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(ptr)
    except (OSError, AttributeError, ValueError):
        return None


def _desktop_via_csidl():
    """SHGetFolderPathW(CSIDL_DESKTOPDIRECTORY)：老 API，兼容性兜底。"""
    try:
        buf = ctypes.create_unicode_buffer(MAX_PATH_LEN)
        shell32 = ctypes.windll.shell32
        shell32.SHGetFolderPathW.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p
        ]
        shell32.SHGetFolderPathW.restype = ctypes.c_long
        if shell32.SHGetFolderPathW(None, CSIDL_DESKTOPDIRECTORY, None, 0, buf) != 0:
            return None
        return buf.value or None
    except (OSError, AttributeError, ValueError):
        return None


def get_desktop_dir() -> str:
    """取"用户真正看到的那个桌面"目录。

    这不是过度设计 —— 硬拼 ``~/Desktop`` 或者走子进程问 PowerShell 都有实测故障：
      * 硬拼 ``~/Desktop``：本机桌面已被 360 迁移工具重定向到
        ``D:\\360MoveData\\Users\\...\\Desktop``，而 ``C:\\Users\\...\\Desktop``
        是搬迁后遗留的旧目录（靠 junction 才碰巧能读）。写进旧目录，
        用户桌面上什么都看不到 —— 装完没图标，整蛊效果直接归零。
      * 走 PowerShell：中文系统输出为 GBK，Python 按 UTF-8 解码会崩，
        结果变空后静默回退，同样落到错误的目录（实测复现）。

    ctypes 直调 shell32 没有子进程、没有编码问题、没有 PowerShell 依赖，
    返回的就是资源管理器里那个桌面。
    """
    for probe, name in ((_desktop_via_known_folder, "SHGetKnownFolderPath"),
                        (_desktop_via_csidl, "SHGetFolderPathW")):
        path = probe()
        if path and os.path.isdir(path):
            log.debug("桌面目录（%s）：%s", name, path)
            return path
        if path:
            log.warning("%s 返回的路径不存在：%r", name, path)

    # 双 API 都失败才退到子进程与硬拼路径（几乎不会走到）
    result = _run_powershell("[Environment]::GetFolderPath('Desktop')", timeout=15)
    if result and result[1] and os.path.isdir(result[1]):
        log.warning("shell32 取桌面失败，改用 PowerShell：%s", result[1])
        return result[1]

    fallback = os.path.join(os.path.expanduser("~"), "Desktop")
    log.warning("无法获取真实桌面路径，回退到 %s（可能不是用户可见的桌面）", fallback)
    return fallback


def clean_child_env(env=None) -> dict:
    """去掉 PyInstaller 的内部环境变量（``_PYI_*`` / ``_MEIPASS*``）。

    装完之后要启动新程序，而**新程序的引导器会核对"父进程的可执行文件是不是
    和我自己一样"**；只要它从环境里看到 ``_PYI_ARCHIVE_FILE``，就会认定自己是
    "被别人启动的第二阶段子进程"，然后发现父进程是 Setup.exe、不是它自己，
    于是弹框拒绝启动：

        Security validation failure: parent process has different executable!

    用户看到的就是一个和本项目毫无关联的 Error 框。清掉这几个变量，新程序就
    回到"像用户双击那样"的干净起点。

    为什么这里单独写一份、不 import ``shell_ops``：那个模块会把 ctypes、
    ``SHFileOperationW``、剪贴板整套带进 Setup.exe 的依赖图，而"这个安装器
    只会写一个 exe + 一份日志"正是审计要保住的性质。三行的代价更小。
    （应用那边同一份逻辑在 ``shell_ops.clean_child_env``。）
    """
    base = dict(os.environ if env is None else env)
    for key in list(base):
        if key in ("_MEIPASS", "_MEIPASS2") or key.startswith("_PYI_"):
            base.pop(key, None)
    return base


def crc32_file(path: str) -> int:
    """分块算文件的 CRC32（不要整读进内存：主程序有 18MB）。"""
    crc = 0
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            crc = zlib.crc32(blk, crc)
    return crc & 0xFFFFFFFF


def write_payload(target_exe: str, data: bytes) -> int:
    """原子写入主程序，并**回读校验**。

    先写 .part 再 os.replace：中途失败（磁盘满、被杀软拦截、
    用户拔盘）不会在用户机器上留下一个半截的 exe，
    否则用户双击时会得到"不是有效的 Win32 应用程序"这种莫名其妙的报错。

    校验顺序是**先验临时文件、再替换**，不能反：
    反了的话一次坏写入会先把好文件顶掉、然后才报错，用户手上就没程序了。
    替换之后再验一次目标位置 —— 这一步专治"写完被安全软件动过"：
    360 / 火绒 这类主动防御有可能在我们落盘之后修改甚至隔离这个 exe，
    那会造成"装完了但打不开"，而且从界面上完全看不出原因。
    """
    os.makedirs(os.path.dirname(target_exe), exist_ok=True)
    tmp = target_exe + ".part"
    want_crc = zlib.crc32(data) & 0xFFFFFFFF
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())

        got_size, got_crc = os.path.getsize(tmp), crc32_file(tmp)
        if got_size != len(data) or got_crc != want_crc:
            raise RuntimeError(
                "写出来的程序文件和预期不一致（%d 字节 / 校验 %08X，"
                "应为 %d 字节 / 校验 %08X）。\n\n"
                "常见原因是磁盘空间不足或安全软件在写入时插手了 —— "
                "请先确认磁盘有空间，并把安全软件（360、火绒等）对该文件夹的"
                "拦截设为允许，再重试一次。"
                % (got_size, got_crc, len(data), want_crc))

        os.replace(tmp, target_exe)

        if (not os.path.exists(target_exe)
                or os.path.getsize(target_exe) != len(data)
                or crc32_file(target_exe) != want_crc):
            raise RuntimeError(
                "程序文件写进去之后又被改动或删除了 —— 几乎可以确定是安全软件"
                "（360、火绒等）把它拦下或隔离了。\n\n"
                "请到安全软件的「防护记录 / 隔离区」里把这个文件放行，然后重试；"
                "或者暂时退出主动防御再装一次。"
                "安装日志：%s" % os.path.join(
                    os.environ.get("LOCALAPPDATA", ""), APP_NAME, "install.log"))
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    log.info("主程序已写入并校验通过 %s (%d 字节)", target_exe, len(data))
    return len(data)


def backup_path_for(target_exe: str) -> str:
    """旧版本存到哪。

    放 ``%LOCALAPPDATA%\\HAAVIK Photo\\rollback\\`` 而不是目标目录旁边：
    目标目录默认就是桌面，往那儿丢一个 ``MyAlbum.exe.bak`` 会在用户眼前
    留一个说不清是什么的文件（本项目「桌面除了程序本体不放别的」的约定）。
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME, "rollback", os.path.basename(target_exe) + ".bak")


def backup_exe(target_exe: str) -> str:
    """覆盖之前先把旧版本存一份，返回备份路径（没有旧版本就返回空串）。

    存不下来**必须说出来**：静默失败的话，下面一旦要回滚就成了"以为有备份、
    其实没有"，那比不备份更糟。
    """
    if not os.path.isfile(target_exe):
        return ""
    dst = backup_path_for(target_exe)
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(target_exe, dst)
    except OSError as exc:
        raise RuntimeError(
            "想把当前的 %s 先备份一份再覆盖，但备份失败了：%s\n\n"
            "为了不弄坏你现在的程序，安装已经停在这里，什么都没改。"
            % (os.path.basename(target_exe), exc))
    log.info("已备份旧版本 -> %s", dst)
    return dst


def restore_exe(target_exe: str, backup: str) -> str:
    """把备份放回去。返回一句能直接显示给用户的话。"""
    if backup and os.path.isfile(backup):
        try:
            shutil.copy2(backup, target_exe)
            return "已经把你原来那一版放了回去，程序还是能打开的。"
        except OSError as exc:
            return ("想把你原来那一版放回去，但没成功：%s\n"
                    "备份文件还在：%s" % (exc, backup))
    # 第一次安装，没有上一版可退：把刚装坏的那个删掉，
    # 总比在桌面上留一个双击打不开、还不知道是什么的图标强。
    try:
        os.remove(target_exe)
        return "这是第一次安装，没有上一版可以退回，已经把装坏的文件删掉了。"
    except OSError as exc:
        return ("这是第一次安装，没有上一版可以退回；装坏的文件也没删掉（%s）。\n"
                "请手动删除：%s" % (exc, target_exe))


def _selfcheck_launch_hint(exc: OSError) -> str:
    """exe 根本起不来时，把"操作系统层面的报错"翻译成人话。

    32 位机器上最常见的一种：系统没装 Microsoft Visual C++ 2015–2022
    运行库（x86 版），于是 python38 / numpy / PIL 依赖的 VCRUNTIME140.dll、
    api-ms-win-crt-*.dll 找不到，程序在"还没进 Python"就被系统拦下。
    这种情况安装器无能为力（它不能偷偷给你装系统组件），但至少把
    "该装哪个、去哪装"说清楚，别只丢一句看不懂的报错。

    ⚠ 注意：如果连安装器自己（Setup.exe）都起不来，用户看到的是 Windows
    自己的系统报错框（"由于找不到 VCRUNTIME140.dll，无法继续执行代码"），
    那种情况这条提示显示不出来——但只要装好下面这个运行库，安装器本身
    也能打开了。所以无论哪种表现，正解都是先装 x86 版 VC++ 运行库。
    """
    msg = str(exc)
    lowered = msg.lower()
    # WinError 126 / "找不到指定的模块" / "specified module"：典型的缺 DLL
    if ("126" in msg or "找不到指定的模块" in msg
            or "specified module" in lowered or "missing" in lowered):
        return (
            "刚装好的程序在你的电脑上起不来，最可能是**缺一个系统运行库**"
            "（Microsoft Visual C++ 2015–2022 可再发行组件包，32 位版）。\n\n"
            "请做这一步（约 30 秒）：\n"
            "  1) 用浏览器打开这个地址，下载一个小安装包：\n"
            "       https://aka.ms/vs/17/release/vc_redist.x86.exe\n"
            "  2) 双击它、一路点“安装”；装完**重新运行本安装程序**即可。\n\n"
            "（这个运行库很多 Windows 程序共用，装一次就好，不是只为我们一家。）"
        )
    # WinError 193 / "不是有效的 Win32 应用程序"：装错了架构
    if "193" in msg and ("win32" in lowered or "valid" in lowered):
        return (
            "这个 32 位程序在你的电脑上打不开（系统说它不是有效的 Win32 程序）。\n"
            "这通常是安装包装错了版本——请确认你下载的是同一个安装包，"
            "重新获取后重试。"
        )
    return "根本起不来：%s" % exc


def selfcheck_installed(target_exe: str, timeout: int = 120) -> tuple:
    """让刚装上的 exe 自己跑一次 ``--selfcheck``，返回 ``(通过, 原因)``。

    为什么装完还要当场验一遍 —— v1.6.0 那次就是这样：
    安装包能下、能校验、能装，界面一路打勾，**只有新程序一双击就
    ModuleNotFoundError**（构建时把它需要的模块排除了）。
    而我这边所有检查（审计、签名、载荷哈希、安装链路）全是绿的，
    结果是"朋友桌上的程序坏了，我这边什么都看不出来"。

    所以现在：装完就地让它自己跑一次，跑不过就把旧版本放回去。
    用户手上永远有一个能打开的程序。
    """
    env = clean_child_env()
    try:
        p = subprocess.run(
            [target_exe, "--selfcheck"],
            cwd=os.path.dirname(target_exe) or None,
            env=env, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except subprocess.TimeoutExpired:
        return False, "自检超过 %d 秒还没结束（不正常）" % timeout
    except OSError as exc:
        return False, _selfcheck_launch_hint(exc)
    if p.returncode == 0:
        return True, ""
    report = os.path.join(os.environ.get("TEMP") or ".", "haavik_selfcheck.log")
    detail = ""
    if os.path.isfile(report):
        try:
            with open(report, "r", encoding="utf-8", errors="replace") as fh:
                detail = fh.read().strip()
        except OSError:
            detail = ""
    return False, detail or ("退出码 %d" % p.returncode)


def _is_locked(path: str) -> bool:
    """目标文件是不是"被占用"（通常是它正在运行）。

    Windows 不允许以写入方式打开一个正在运行的 exe —— 拿这点当探针最准，
    比查进程名可靠（用户可能改了名、可能有多个副本）。
    """
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r+b"):
            return False
    except PermissionError:
        return True
    except OSError as exc:                               # 其它错误也当作不可写
        log.warning("检测 %s 是否可写时出错：%s", path, exc)
        return True


def report_skipped_resources(resources) -> int:
    """容器里若还有资源项（旧容器），只报数、不落盘。

    保留这个函数是为了让"旧容器 + 新安装器"组合也有明确行为：
    **什么都不写，但一定要吭声**。静默丢弃比报错更难查。
    """
    if not resources:
        return 0
    names = sorted(resources)
    log.warning(
        "容器里带了 %d 个资源（%s），但当前版本按单 EXE 安装，一律不落盘。",
        len(names), "、".join(names[:6]) + ("…" if len(names) > 6 else ""),
    )
    return len(names)


# ==========================================================================
# 安装向导
# ==========================================================================
class Installer:
    HISTORY_LIMIT = 8

    INSTALL_STEPS = (
        ("正在准备安装目录…", 0.10, 0.30),
        ("正在校验安装包完整性…", 0.38, 0.30),
        ("正在写入程序文件…", 0.68, 0.45),
        ("正在完成最终配置…", 0.99, 0.25),
    )

    def __init__(self, root: tk.Tk, bundle: Bundle) -> None:
        self.root = root
        self.bundle = bundle

        self.arch = detect_arch()
        self.install_dir = default_install_dir()
        self.target_exe = os.path.join(self.install_dir, PROGRAM_FILENAME)
        self.launch_after = tk.BooleanVar(value=True)   # 默认勾选"完成后立即启动"
        self._accept_var = tk.BooleanVar(value=True)
        self._history = []
        self._installing = False
        self._progress_ratio = 0.0
        self._min_w = MIN_WINDOW_W
        self._min_h = MIN_WINDOW_H

        root.title("%s %s 安装" % (APP_NAME, APP_VERSION))
        root.configure(bg="#ffffff")
        # 允许缩放：显示缩放比例很低的机器上，用户还可以自己拉大窗口。
        root.resizable(True, True)
        root.minsize(MIN_WINDOW_W, MIN_WINDOW_H)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        log.info(
            "安装器自身 %s 位；目标系统 %s 位 → 将释放 %s；默认安装目录 %s",
            describe_self(),
            "64" if self.arch == "x64" else "32",
            self.arch,
            self.install_dir,
        )
        self._build_welcome()
        self._fit_window()

    # ---------- 通用控件 ----------
    def _header(self, title: str, subtitle: str = "") -> None:
        wrap = tk.Frame(self.root, bg="#2c3e50", height=72)
        wrap.pack(fill="x")
        wrap.pack_propagate(False)
        tk.Label(
            wrap, text=title, bg="#2c3e50", fg="white", font=("Microsoft YaHei", 14, "bold")
        ).pack(side="left", padx=24, pady=12)
        if subtitle:
            tk.Label(
                wrap, text=subtitle, bg="#2c3e50", fg="#bdc3c7", font=("Microsoft YaHei", 9)
            ).pack(side="right", padx=24, pady=14)
        tk.Frame(self.root, bg="#3498db", height=3).pack(fill="x")

    def _nav_bar(self):
        bar = tk.Frame(self.root, bg="#f0f0f0", height=58)
        bar.pack(side="bottom", fill="x")
        bar.pack_propagate(False)
        return bar

    def _back_btn(self, bar) -> None:
        tk.Button(
            bar,
            text="< 上一步",
            command=self._back,
            font=("Microsoft YaHei", 10),
            relief="flat",
            bg="#f0f0f0",
            cursor="hand2",
            bd=0,
            activebackground="#f0f0f0",
        ).pack(side="left", padx=24, pady=14)

    def _next_btn(self, bar, on_next, label: str = "下一步 >", color: str = "#3498db") -> None:
        hover = {"#3498db": "#2980b9", "#27ae60": "#229954"}.get(color, color)
        tk.Button(
            bar,
            text=label,
            command=on_next,
            font=("Microsoft YaHei", 10, "bold"),
            bg=color,
            fg="white",
            relief="flat",
            cursor="hand2",
            bd=0,
            width=14,
            height=1,
            activebackground=hover,
            activeforeground="white",
        ).pack(side="right", padx=24, pady=14)

    def _body(self):
        body = tk.Frame(self.root, bg="#ffffff")
        body.pack(expand=True, fill="both", padx=32, pady=16)
        return body

    def _clear(self) -> None:
        for child in self.root.winfo_children():
            child.destroy()

    def _fit_window(self) -> None:
        """按当前页面的**实测**需求调整窗口，杜绝望任何控件被裁切。

        为什么必须这么做（这是"按钮只有一半"的根因）：
        tkinter 的字体尺寸随系统显示缩放变化。原实现把窗口写死成 680x500
        且 `resizable(False, False)`，于是在 125% / 150% 缩放下，页脚控件
        （例如"完成后立即启动"复选框、导航按钮）会被挤到可视区之外，
        用户看到的就是一个被切掉一半的按钮 —— 而且无法拉伸窗口去看全。

        这里不再猜尺寸：让 Tk 先算完布局（update_idletasks），
        读出真实需求尺寸，再据此设置窗口大小，并只增不减以避免页面切换时抖动。
        """
        try:
            self.root.update_idletasks()
            need_w = max(self._min_w, self.root.winfo_reqwidth())
            need_h = max(self._min_h, self.root.winfo_reqheight())
            if need_w != self._min_w or need_h != self._min_h:
                self._min_w, self._min_h = need_w, need_h
                self.root.minsize(need_w, need_h)
                log.debug("窗口需求尺寸调整为 %dx%d", need_w, need_h)

            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            # 不超过屏幕可用区域，避免窗口比屏幕还大
            w, h = min(self._min_w, sw), min(self._min_h, max(400, sh - 80))
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 3)
            self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        except tk.TclError:
            pass

    def _goto(self, builder) -> None:
        self._clear()
        builder()
        self._fit_window()

    def _show(self, builder) -> None:
        """供非 _goto 路径使用（例如从后台线程经 root.after 切换页面）。"""
        self._clear()
        builder()
        self._fit_window()

    def _push(self, builder) -> None:
        self._history.append(builder)
        if len(self._history) > self.HISTORY_LIMIT:  # 有界历史，反复来回点不会无限增长
            del self._history[0]

    def _back(self) -> None:
        if self._history:
            self._goto(self._history.pop())

    def _on_close(self) -> None:
        if self._installing:
            if not messagebox.askokcancel(
                "正在安装", "安装正在进行中，现在关闭可能留下不完整的文件。\n\n确定要退出吗？"
            ):
                return
            log.warning("用户在安装过程中关闭了安装程序")
        self.root.destroy()

    # ---------- 步骤 1：欢迎 ----------
    def _build_welcome(self) -> None:
        self._clear()
        self._header("欢迎使用 %s" % APP_NAME, "v%s" % APP_VERSION)
        body = self._body()

        tk.Label(body, text="✓", bg="#ffffff", fg="#27ae60", font=("Arial", 36, "bold")).pack(
            pady=(2, 2)
        )
        tk.Label(
            body,
            text="欢迎使用 %s 安装程序" % APP_NAME,
            bg="#ffffff",
            fg="#2c3e50",
            font=("Microsoft YaHei", 12, "bold"),
        ).pack(pady=2)

        # ---------- 完整免责声明（滚动） ----------
        # ⚠ 每一段都对应一段实际行为（2026-09-26 安全审计逐条核过）——
        #   不写含糊的"我们可能会收集"那种话，每一句都能在代码里指出出处。
        #   文字长是有意的：宁可让用户多看几屏，也别漏掉哪一条。
        #   给一个滚动区而不是一页装下 —— 防止屏幕小的机器上被裁切。
        DISCLAIMER = (
            "【这个程序是什么】\n"
            "HAAVIK Photo 是一个能正常使用的图片工具：改尺寸、转格式、裁剪、"
            "调亮度对比度等。它同时带一个**小玩笑**：第一次启动时会问你一句"
            "「天空属于谁」，答对就直接进工具；答错会被锁住，需要输入一枚"
            "本月有效的序列号才能解锁（序列号是公开算法生成的，不是加密保护，"
            "会读代码的人能自己算）。\n"
            "\n"
            "【请只在知情并同意的朋友的机器上安装】\n"
            "因为这个小玩笑**会让用户暂时没法用电脑**（锁屏 + 60 秒倒计时）"
            "—— 装到不知情的人的电脑上，等于让他白着急一阵。\n"
            "\n"
            "【程序会联网做什么】\n"
            "  1) 启动时、检查更新按钮点下去时：会向作者维护的清单仓库（gitee.com / "
            "github.com / jsdelivr.net）发起 HTTPS 请求，仅取版本号/清单。"
            "   不会向清单里写你电脑的任何信息。\n"
            "  2) 用户点「下载 AI 模型」时：从同一批清单仓库下载 AI 模型包（zip，"
            "几百 MB），下完用 SHA-256 强校验；校验不过的包**自动丢弃、绝不安装**。\n"
            "  3) 用户点反馈的「发送」时：通过以下三种方式之一把信寄给作者 ——\n"
            "     · SMTP：程序连作者指定的邮箱服务器直发（发信账号在安装包里，"
            "作者可以随时在邮箱后台重置）。\n"
            "     · Endpoint：先经过一个第三方表单服务（如 formspree.io）再转成邮件。\n"
            "     · mailto：交给您电脑上的邮件程序，**要您亲手点发送才真发出去**。\n"
            "     三种方式**只在用户点了「发送」之后才触发**，程序自己不发信。"
            "默认只发您在反馈框里打的字 + 程序版本号 + Windows 版本号；"
            "**回信邮箱是选填的**，不填就一个字都没有（也不会出现在日志里）。\n"
            "\n"
            "【程序会写盘到哪些位置】\n"
            "  · 安装目录（默认桌面）：主程序 + 卸载时回收的 .ico / .png。\n"
            "  · %%LOCALAPPDATA%%\\HAAVIK Photo\\：状态文件、卸载时的旧版备份、"
            "AI 模型组件目录（用户主动下载后才会有）。\n"
            "  · %%LOCALAPPDATA%%\\HAAVIK Photo\\updates\\：下载下来的更新包（您不点"
            "「安装」就一直留着，绝不自动运行）。\n"
            "  · 用户主动打开/另存的图片：写到您选的位置，跟普通保存文件一样。\n"
            "\n"
            "【程序绝对不会做的事】\n"
            "  · 不写注册表、不动启动文件夹、不建计划任务、不注册服务 —— "
            "**不会让程序开机自启**。\n"
            "  · 不读您的图片内容（除非您主动打开）、不读聊天记录、不读其他应用数据。"
            "  · 不读您的用户名、机器名、IP 地址（mac 地址就更不会）。\n"
            "  · 不读剪贴板（除非您主动点「粘贴到反馈框」）。\n"
            "  · 不上报任何遥测/统计/崩溃日志。\n"
            "  · **不请求管理员权限**（UAC 不会弹）；全程以普通用户权限运行。\n"
            "\n"
            "【删除行为】\n"
            "  · 删除图片/AI 模型包：**只走回收站**，可还原；**没有永久删除的选项**。"
            "  · 程序不删任何您没主动选择的文件。\n"
            "\n"
            "【AI 模型（可选下载）】\n"
            "  · 默认**没装**。您在工具里点「下载 AI 模型」才会拉取。\n"
            "  · 模型来自开源权重（详见 AI 模型清单里的许可声明）。\n"
            "  · 推理完全在您电脑本地完成，**AI 跑的时候不联网**。\n"
            "\n"
            "【更新机制】\n"
            "  · 检查更新：手动点「开始检查」才会发起请求，**不会在后台偷偷查**。\n"
            "  · 下载更新：下到本地后用 SHA-256 校验，校验不过**自动丢弃**。"
            "  · 安装更新：必须用户亲手点「立即安装」，**绝不自动运行下载下来的文件**。\n"
            "\n"
            "【作者免责】\n"
            "  · 本程序按\"现状\"提供，作者不对任何无故障、不中断、不出错的承诺负责。"
            "  · 不对因使用本程序造成的任何直接或间接损失负责（包括但不限于：数据丢失、"
            "被不知情的朋友误解、被安全软件误报等）。\n"
            "  · 用户自负风险。\n"
            "\n"
            "【开源 & 可审计】\n"
            "  · 完整源码公开在仓库里，可以审计每一条上面写到的行为是不是真的那样。"
            "  · 仓库地址：%s"
            % REPO_HINT_URL_INSTALLER + "\n"
        )

        # 滚动文本框：用户能从头看到尾，确认无误后再勾选
        text_wrap = tk.Frame(body, bg="#ecf0f1", bd=1, relief="solid")
        text_wrap.pack(pady=8, fill="both", expand=True)
        scroll = tk.Scrollbar(text_wrap)
        scroll.pack(side="right", fill="y")
        text = tk.Text(
            text_wrap,
            wrap="word",
            yscrollcommand=scroll.set,
            font=("Microsoft YaHei", 9),
            bg="#ecf0f1",
            fg="#2c3e50",
            relief="flat",
            bd=8,
            padx=10,
            pady=10,
            height=14,
        )
        text.pack(side="left", fill="both", expand=True)
        scroll.config(command=text.yview)
        text.insert("1.0", DISCLAIMER)
        text.configure(state="disabled")

        # ⚠ 把「接受条款」的勾选放在**读完滚动条之后**（视觉上是有顺序的）
        tk.Checkbutton(
            body,
            text="我已阅读并同意上述全部条款（包括整蛊玩法、联网与写盘行为）",
            variable=self._accept_var,
            bg="#ffffff",
            font=("Microsoft YaHei", 10, "bold"),
            activebackground="#ffffff",
            selectcolor="#ffffff",
            fg="#2c3e50",
        ).pack(pady=(8, 4))

        def on_next():
            if not self._accept_var.get():
                messagebox.showwarning("提示", "请先勾选「我已阅读并同意」")
                return
            self._push(self._build_welcome)
            self._goto(self._build_path)

        self._next_btn(self._nav_bar(), on_next)

    # ---------- 步骤 2：路径 ----------
    def _build_path(self) -> None:
        self._clear()
        self._header("选择安装位置", "步骤 2 / 4")
        body = self._body()

        tk.Label(
            body,
            text="程序会以**单个文件**的形式放进下面的文件夹（默认就是桌面）。",
            bg="#ffffff",
            fg="#34495e",
            font=("Microsoft YaHei", 10),
        ).pack(anchor="w", pady=(8, 8))

        row = tk.Frame(body, bg="#ffffff")
        row.pack(fill="x", pady=4)
        path_var = tk.StringVar(value=self.install_dir)
        tk.Entry(row, textvariable=path_var, font=("Consolas", 10), relief="solid", bd=1).pack(
            side="left", fill="x", expand=True, ipady=4
        )

        def browse():
            chosen = filedialog.askdirectory(title="选择安装位置", initialdir=self.install_dir)
            if chosen:
                # 注意：不再拼接 APP_NAME。目标是个**文件夹**（程序就放在里面），
                # 拼接会变成"桌面\HAAVIK Photo\MyAlbum.exe"，又多出一层目录。
                path_var.set(chosen)

        tk.Button(
            row,
            text="浏览…",
            command=browse,
            font=("Microsoft YaHei", 9),
            relief="flat",
            bg="#ecf0f1",
            cursor="hand2",
            bd=0,
            width=8,
            height=1,
        ).pack(side="left", padx=6)

        tk.Label(
            body,
            text=(
                "程序文件将放到：\n"
                "  %s\n"
                "安装结果只有这一个文件，**不建快捷方式、不生成 Resources 之类的目录**：\n"
                "  %s\n\n"
                "目标系统：Windows %s 位  ·  将安装对应版本的主程序\n"
                "预计占用磁盘空间：约 40 MB\n"
                "卸载方式：把这个 exe 删掉即可 —— 不留目录、不留注册表项、不留系统痕迹。"
                % (self.install_dir, self.target_exe, "64" if self.arch == "x64" else "32")
            ),
            bg="#ffffff",
            fg="#7f8c8d",
            font=("Microsoft YaHei", 9),
            justify="left",
        ).pack(anchor="w", pady=(14, 0))

        def on_next():
            path = path_var.get().strip()
            if not path:
                messagebox.showwarning("提示", "请指定安装路径")
                return
            if not os.path.isabs(path):
                messagebox.showwarning("提示", "请使用绝对路径")
                return
            if not os.path.isdir(path):
                messagebox.showwarning("提示", "这个文件夹不存在：\n%s" % path)
                return
            self.install_dir = path
            self.target_exe = os.path.join(path, PROGRAM_FILENAME)
            self._push(self._build_path)
            self._goto(self._build_components)

        bar = self._nav_bar()
        self._back_btn(bar)
        self._next_btn(bar, on_next)

    # ---------- 步骤 3：组件 ----------
    def _build_components(self) -> None:
        self._clear()
        self._header("选择组件", "步骤 3 / 4")
        body = self._body()

        tk.Label(
            body,
            text="将安装以下组件：",
            bg="#ffffff",
            fg="#34495e",
            font=("Microsoft YaHei", 10),
        ).pack(anchor="w", pady=(4, 10))

        for label, size in (
            ("核心程序（单文件，含全部图片处理功能）", "约 40 MB"),
        ):
            row = tk.Frame(body, bg="#ffffff")
            row.pack(fill="x", pady=2, anchor="w")
            tk.Label(row, text="✔ " + label, bg="#ffffff", font=("Microsoft YaHei", 10)).pack(
                side="left"
            )
            tk.Label(row, text=size, bg="#ffffff", fg="#95a5a6", font=("Microsoft YaHei", 9)).pack(
                side="left", padx=8
            )

        tk.Label(
            body,
            text="\n安装结果是**一个 exe 文件**，直接躺在你选的文件夹里（默认桌面）：\n"
            "  %s\n\n"
            "没有快捷方式 —— 程序自己就在桌面上，再放个指向它的 .lnk 是纯重复。\n"
            "也没有 Resources 之类的附带目录 —— **用这个软件，桌面上只会多这一个文件**。\n\n"
            "首次运行时程序会在 %%LOCALAPPDATA%%\\HAAVIK Photo\\ 写一个 state.json，\n"
            "用来记住「这个月的问题已经问过了」—— 那在系统用户目录里，\n"
            "不会出现在你的桌面上。除此之外它不碰任何文件 ——\n"
            "你打开、改尺寸、另存为的那张图，只有你自己点过「保存」的才会被写出。\n\n"
            "本程序不写注册表、不装服务、不设自启动，因此不会出现在\n"
            "「控制面板 → 程序和功能」里；卸载就是把那个 exe 删掉\n"
            "（想删干净的话，再把 %%LOCALAPPDATA%%\\HAAVIK Photo\\ 文件夹删掉即可）。"
            % self.target_exe,
            bg="#ffffff",
            fg="#7f8c8d",
            font=("Microsoft YaHei", 9),
            justify="left",
        ).pack(anchor="w", pady=(10, 0))

        def on_next():
            self._push(self._build_components)
            self._goto(self._build_install)

        bar = self._nav_bar()
        self._back_btn(bar)
        self._next_btn(bar, on_next)

    # ---------- 步骤 4：安装 ----------
    def _build_install(self) -> None:
        self._build_progress_only()
        self._installing = True
        # 全部重活丢到后台线程，主线程只跑事件循环 —— UI 不卡顿的关键
        threading.Thread(target=self._do_install, name="install", daemon=True).start()

    def _build_progress_only(self) -> None:
        """"安装中"页面的构造成分，不含启动线程。

        拆出来是为了让布局测试能单独渲染这一页（否则测试会真的开始写盘）。
        """
        self._clear()
        self._header("正在安装", "步骤 4 / 4")
        body = self._body()

        tk.Label(
            body,
            text="正在安装 %s …" % APP_NAME,
            bg="#ffffff",
            fg="#2c3e50",
            font=("Microsoft YaHei", 12, "bold"),
        ).pack(pady=(8, 6))

        self._progress_text = tk.StringVar(value="准备安装…")
        tk.Label(
            body,
            textvariable=self._progress_text,
            bg="#ffffff",
            fg="#34495e",
            font=("Microsoft YaHei", 10),
        ).pack(pady=2)

        self._pb_canvas = tk.Canvas(body, height=14, bg="#ecf0f1", highlightthickness=0)
        self._pb_canvas.pack(fill="x", pady=12, ipady=2)
        # 尺寸变化时重绘。原实现只在 set_progress 里读 winfo_width()，
        # 而首帧宽度还是 1，进度条第一次根本不显示（看起来像"卡住了"）。
        self._pb_canvas.bind("<Configure>", lambda _e: self._render_progress())

        self._status_label = tk.Label(
            body, text="", bg="#ffffff", fg="#7f8c8d", font=("Microsoft YaHei", 9)
        )
        self._status_label.pack(pady=4)

        self._nav_bar()

    def _render_progress(self) -> None:
        width = self._pb_canvas.winfo_width()
        self._pb_canvas.delete("all")
        if width > 1:
            self._pb_canvas.create_rectangle(
                0, 0, int(width * self._progress_ratio), 14, fill="#3498db", outline=""
            )

    def _set_progress(self, text: str, ratio: float) -> None:
        self._progress_ratio = ratio
        self._progress_text.set(text)
        self._render_progress()

    def _status(self, text: str) -> None:
        self._status_label.configure(text=text)

    def _do_install(self) -> None:
        """后台线程：只做 IO 与等待；所有 UI 更新经 root.after 回到主线程。"""
        try:
            for text, ratio, delay in self.INSTALL_STEPS:
                self.root.after(0, self._set_progress, text, ratio)
                time.sleep(delay)

            arch = self.arch
            label = "64" if arch == "x64" else "32"
            self.root.after(0, self._status, "正在校验并写入程序文件 (%s 位)..." % label)

            data, size, crc = self.bundle.payload(arch)
            # 目标 exe 正在运行 → 覆盖必然失败。与其让用户看到一句
            # "PermissionError: 拒绝访问"，不如把原因和下一步直接说出来。
            # （正常从程序里更新的路径上，主程序会先自己退出，走不到这里；
            #   这一条是给"手动双击安装包、但程序还开着"兜底的。）
            if _is_locked(self.target_exe):
                raise RuntimeError(
                    "「%s」正在运行，安装程序没法覆盖它。\n\n"
                    "请先把它关掉（看任务栏 / 右下角），然后回到上一步重新点「下一步」。\n\n"
                    "目标位置：%s" % (PROGRAM_FILENAME, self.target_exe))
            # 先把旧版本存一份：下面自检不过时要靠它把程序恢复回去，
            # 否则"更新"就会变成"把朋友桌上那个能用的程序顶掉"。
            backup = backup_exe(self.target_exe)
            write_payload(self.target_exe, data)
            self.root.after(
                0, self._status, "已写入 MyAlbum.exe (%s 位, %d KB)" % (label, size // 1024)
            )

            # 装完就地验一次：这个 exe 真的打得开吗？
            # v1.6.0 那次就是「装得上去、双击打不开」，而我这边所有检查全绿，
            # 结果是朋友桌上的程序坏了、我毫不知情。验不过就回滚。
            self.root.after(0, self._status, "正在确认新程序能打开 ...")
            ok, why = selfcheck_installed(self.target_exe)
            if not ok:
                fixed = restore_exe(self.target_exe, backup)
                raise RuntimeError(
                    "新装的程序没能通过自检，安装已经撤销。\n\n%s\n\n"
                    "具体原因：%s\n\n"
                    "这是这一版安装包自己的问题，不是你电脑的问题 —— "
                    "重试几次也不会变好，请联系作者换一版。"
                    % (fixed, why))

            # 单 EXE 安装：容器里即便还带着资源（旧容器），也一律不落盘，
            # 但必须吭声 —— 静默丢弃比报错更难查。
            skipped = report_skipped_resources(self.bundle.resources())
            if skipped:
                self.root.after(0, self._status, "已跳过 %d 个附带资源（按单文件安装）" % skipped)

            # 这里过去会 create_desktop_shortcut()。2026-09-13 去掉了：
            # 程序本身就放在桌面上，再建一个指向它的 .lnk 是纯重复，
            # 而且会留下"删了 exe 还剩个坏快捷方式"的状态。
            self.root.after(0, self._status, "程序文件已就位 ✓")

            time.sleep(0.3)
            self.root.after(0, self._set_progress, "安装完成", 1.0)
            self.root.after(300, lambda: self._show(self._build_finish))
        except Exception as exc:  # noqa: BLE001 - 顶层兜底，必须让用户看到原因
            log.exception("安装失败")
            # 关键修复：异常变量 exc 在 except 块结束后会被 Python 删除。
            # 原实现把 f"...{e}" 写进 lambda，而 lambda 是之后才执行的，
            # 于是"错误提示"本身抛 NameError，用户只看到安装卡住、没有任何信息。
            self.root.after(0, self._show_install_error, exc)
            self.root.after(400, lambda: self._goto(self._build_path))
        finally:
            self._installing = False

    def _show_install_error(self, exc: BaseException) -> None:
        log_path = os.path.join(os.environ.get("LOCALAPPDATA", ""), APP_NAME, "install.log")
        _show_report_dialog(
            "安装失败",
            "安装过程中出错：\n\n%s: %s\n\n详细信息已记录到：\n%s"
            % (type(exc).__name__, exc, log_path),
            parent=self.root,
        )

    # ---------- 完成 ----------
    def _build_finish(self) -> None:
        self._clear()
        self._header("安装完成", "完成")
        body = self._body()

        tk.Label(body, text="✓", bg="#ffffff", fg="#27ae60", font=("Arial", 36, "bold")).pack(
            pady=(2, 2)
        )
        tk.Label(
            body,
            text="%s 已成功安装！" % APP_NAME,
            bg="#ffffff",
            fg="#2c3e50",
            font=("Microsoft YaHei", 13, "bold"),
        ).pack(pady=(0, 2))

        tk.Label(
            body,
            text=(
                "程序文件：%s\n"
                "它就在桌面上，双击即可运行 —— 没有快捷方式。"
                % self.target_exe
            ),
            bg="#ffffff",
            fg="#34495e",
            font=("Microsoft YaHei", 9),
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(2, 6))

        # 360 / 火绒 这类"主动防御"会在安装时或首次运行时弹一个框，问要不要
        # 放行一个没买证书、又在桌面创建 exe 的小程序。那不是我们的提示，用户
        # 看到会莫名其妙（反馈过"弹出一个我不认识的警告"）。这里如实说明它是什么、
        # 该点哪个 —— **不做任何绕过**（本项目从不对抗杀软），只是把话说清楚。
        tk.Label(
            body,
            text=(
                "如果杀毒软件 / 安全卫士（360、火绒等）弹出提示，\n"
                "那是在问你要不要放行这个程序：它没有购买代码签名证书，\n"
                "又会在桌面创建一个 exe。选「允许」或「仍要运行」即可 ——\n"
                "它只写这一个文件，不装服务、不加开机自启、不改注册表。"
            ),
            bg="#ffffff",
            fg="#7f8c8d",
            font=("Microsoft YaHei", 9),
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(0, 4))

        # 启动选项：两种选择都写得明明白白，让用户知道不勾也能自己启动
        box = tk.Frame(body, bg="#f7f9fb", highlightbackground="#d6e0ea", highlightthickness=1)
        box.pack(fill="x", pady=(4, 4))
        tk.Checkbutton(
            box,
            text="完成后立即启动 %s" % APP_NAME,
            variable=self.launch_after,
            bg="#f7f9fb",
            font=("Microsoft YaHei", 11, "bold"),
            activebackground="#f7f9fb",
            selectcolor="#f7f9fb",
            anchor="w",
        ).pack(fill="x", padx=10, pady=(8, 0))
        tk.Label(
            box,
            text=(
                "  ✓ 勾选：点「完成」后立刻启动程序\n"
                "  ☐ 不勾选：什么都不会发生，稍后双击桌面上的「%s」自行启动"
                % os.path.basename(self.target_exe)
            ),
            bg="#f7f9fb",
            fg="#7f8c8d",
            font=("Microsoft YaHei", 9),
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=10, pady=(0, 8))

        self._next_btn(self._nav_bar(), self._finish, "完成", color="#27ae60")

    def _finish(self) -> None:
        if self.launch_after.get():
            if not os.path.isfile(self.target_exe):
                _show_report_dialog("错误", "找不到 %s" % self.target_exe,
                                    parent=self.root)
            else:
                try:
                    # 清掉 PyInstaller 的内部环境变量再启动，否则新程序会因为
                    # "父进程可执行文件和我不一样"而拒绝启动（见 clean_child_env）。
                    subprocess.Popen([self.target_exe], shell=False,
                                     env=clean_child_env())
                except OSError as exc:
                    log.exception("启动失败")
                    _show_report_dialog("错误", "无法启动程序：\n%s" % exc,
                                        parent=self.root)
        self.root.destroy()


# ==========================================================================
# 安装失败 → 一键把错误报告发到作者邮箱
# ==========================================================================
def _vc_redist_present() -> str:
    """探针：x86 版 VC++ 运行库在不在（安装器是 32 位，依赖它）。

    32 位进程视角下，x86 运行库落在 SysWOW64（64 位系统）或 System32（32 位系统）。
    """
    sysroot = os.environ.get("SystemRoot", "C:\\Windows")
    candidates = [
        os.path.join(sysroot, "SysWOW64", "vcruntime140.dll"),
        os.path.join(sysroot, "System32", "vcruntime140.dll"),
        os.path.join(sysroot, "System32", "vcruntime140_1.dll"),
    ]
    found = [c for c in candidates if os.path.isfile(c)]
    if found:
        return "已安装（%s）" % "; ".join(found)
    return "未找到 vcruntime140.dll（可能缺 VC++ 2015–2022 运行库 x86 版）"


def _collect_diagnostics() -> str:
    """安装失败排查要的系统信息（不含任何照片/个人文件）。"""
    lines = []
    try:
        major, minor, name, supported = detect_windows_version()
        lines.append("系统       : %s (%d.%d) 受支持=%s"
                     % (name, major, minor, supported))
    except Exception as exc:  # noqa: BLE001
        lines.append("系统       : 获取失败（%s）" % exc)
    lines.append("系统架构   : %s" % detect_arch())
    lines.append("安装器位数 : %s 位" % describe_self())
    lines.append("Python     : %s" % sys.version.split()[0])
    lines.append("安装器版本 : %s" % APP_VERSION)
    lines.append("VC++ x86    : %s" % _vc_redist_present())
    return "\n".join(lines)


def _read_log(path: str, limit: int = 20000) -> str:
    """读一份日志，太长就只留末尾。读不到返回空串（不报错）。"""
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read()
    except OSError:
        return ""
    if len(data) > limit:
        data = "...（日志过长，仅显示末尾）...\n" + data[-limit:]
    return data.strip()


def _build_error_report(title: str, message: str) -> str:
    """拼出要发给作者的错误报告正文（纯文本，无附件）。"""
    install_log = os.path.join(os.environ.get("LOCALAPPDATA", ""), APP_NAME, "install.log")
    selfcheck_log = os.path.join(os.environ.get("TEMP") or ".", "haavik_selfcheck.log")
    parts = [
        "=== HAAVIK Photo 安装/更新 失败报告 ===",
        "时间       : %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "出错环节   : %s" % title,
        "",
        "--- 出错信息 ---",
        str(message),
        "",
        "--- 系统信息（不含照片/个人文件）---",
        _collect_diagnostics(),
    ]
    log_text = _read_log(install_log)
    if log_text:
        parts += ["", "--- 安装日志 (%s) ---" % install_log, log_text]
    sc_text = _read_log(selfcheck_log)
    if sc_text:
        parts += ["", "--- 自检日志 (%s) ---" % selfcheck_log, sc_text]
    return "\n".join(parts)


def _send_error_report(title: str, message: str):
    """把错误报告发到作者邮箱。返回 ``(成功?, 人话说明)``。不会抛异常。"""
    user, password, _to, host = feedback_send.credentials()
    if not (user and password):
        return False, "这个安装包没有配置发信账号，请改用「复制文字」发给我。"
    recipient = feedback_send.effective_recipient() or "作者邮箱"
    text = _build_error_report(title, message)
    subject = "HAAVIK Photo 安装失败报告 (%s)" % APP_VERSION
    try:
        return feedback_send.send_via_smtp_with(
            user, password, recipient, host,
            text=text, subject=subject, timeout=25.0,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("发送错误报告失败")
        return False, "发送时出错：%s" % exc


def _show_report_dialog(title: str, message: str, parent=None) -> None:
    """安装失败时的对话框：能看报告、能一键发到作者邮箱、也能复制文字。

    设计取舍（对照项目隐私约定）：
    * 不发照片、不发个人文件——只发「出错信息 + 系统版本/位数 + 安装日志 + 自检日志」。
    * 必须用户**亲手点「发送」**才联网（程序不偷偷上报）；点之前把报告全文摊开，
      点了即视为同意发这份报告到作者邮箱。
    * 收件箱与程序本体反馈是同一个（见 feedback_send.effective_recipient），
      发信凭据由构建时塞入安装包。
    """
    own_root = parent is None
    if own_root:
        parent = tk.Tk()
        parent.withdraw()

    top = tk.Toplevel(parent)
    top.title(title)
    top.grab_set()
    try:
        sw = parent.winfo_screenwidth()
        sh = parent.winfo_screenheight()
    except tk.TclError:
        sw, sh = 800, 600
    top.geometry("%dx%d" % (min(640, sw - 80), min(460, sh - 120)))

    tk.Label(top, text=title, font=("Microsoft YaHei", 13, "bold"),
             fg="#c0392b", anchor="w").pack(fill="x", padx=14, pady=(12, 4))

    report = _build_error_report(title, message)
    frame = tk.Frame(top)
    frame.pack(fill="both", expand=True, padx=14, pady=(0, 6))
    txt = tk.Text(frame, wrap="word", height=18, font=("Consolas", 9))
    txt.insert("1.0", report)
    txt.configure(state="disabled")
    sb = tk.Scrollbar(frame, command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    txt.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")

    tk.Label(top, text=(
        "这份报告会发到作者邮箱（%s），用来排查安装问题；内容只有：出错信息、"
        "系统版本与位数、安装/自检日志——不含你的照片或个人文件。点「发送」即表示同意。"
        % (feedback_send.effective_recipient() or "作者邮箱")),
        font=("Microsoft YaHei", 9), fg="#7f8c8d", wraplength=600,
        justify="left", anchor="w").pack(fill="x", padx=14, pady=(0, 6))

    status = tk.Label(top, text="", font=("Microsoft YaHei", 9),
                      fg="#2c3e50", anchor="w")
    status.pack(fill="x", padx=14, pady=(0, 4))

    btn_bar = tk.Frame(top)
    btn_bar.pack(fill="x", padx=14, pady=(0, 12))

    sending = {"busy": False}

    def _apply_send_result(ok, detail):
        sending["busy"] = False
        status.configure(text=detail)
        if ok:
            send_btn.configure(state="disabled", text="已发送 ✓")
        else:
            send_btn.configure(state="normal")

    def _send_in_thread():
        try:
            ok, detail = _send_error_report(title, message)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, "发送时出错：%s" % exc
        top.after(0, lambda: _apply_send_result(ok, detail))

    def do_send():
        if sending["busy"]:
            return
        sending["busy"] = True
        send_btn.configure(state="disabled")
        status.configure(text="正在发送 ...")
        threading.Thread(target=_send_in_thread, daemon=True).start()

    def do_copy():
        try:
            top.clipboard_clear()
            top.clipboard_append(report)
            status.configure(text="已复制到剪贴板，把这段文字发给我就行。")
        except tk.TclError:
            status.configure(text="复制失败（剪贴板不可用），请手动选择文字复制。")

    send_btn = tk.Button(btn_bar, text="把报告发到作者邮箱",
                         command=do_send, bg="#27ae60", fg="white",
                         font=("Microsoft YaHei", 10, "bold"))
    send_btn.pack(side="right", padx=(6, 0))
    tk.Button(btn_bar, text="复制文字", command=do_copy,
              font=("Microsoft YaHei", 10)).pack(side="right", padx=(6, 0))
    tk.Button(btn_bar, text="关闭", command=top.destroy,
              font=("Microsoft YaHei", 10)).pack(side="right")

    top.wait_window(top)
    if own_root:
        try:
            parent.destroy()
        except tk.TclError:
            pass


# ==========================================================================
# 入口
# ==========================================================================
def fatal(title: str, message: str) -> int:
    """用 GUI 报错（窗口化程序没有控制台，print 看不到）。

    能弹窗时就顺手提供「把错误报告发到作者邮箱」——但安装器自身起不来
    （比如缺 VC++ 运行库，连 Python 都没进）的极端情况走不到这里，
    那种情况只能先装运行库，本函数显示不出。
    """
    log.critical("%s: %s", title, message)
    try:
        _show_report_dialog(title, message, parent=None)
    except tk.TclError:
        pass
    return 2


def main() -> int:
    setup_logging()
    silence_console_if_frozen()

    # —— 发版前自检：真发一封测试报告，证明「打包后的安装器也能发信」 ——
    # 不走 GUI、不开窗，只把结果写进 %TEMP%/haavik_report_selftest.log 并返回
    # 退出码（0=发通了，1=没发通）。release.py 在发布前会跑它当闸门。
    if "--selftest-report" in sys.argv[1:]:
        try:
            ok, detail = _send_error_report(
                "安装器发信自检",
                "这是发版前的发信自检，并非真实安装错误。若你收到这封邮件，"
                "说明打包后的安装器确实能把错误报告发到作者邮箱。",
            )
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, "自检时出错：%s" % exc
        out = os.path.join(os.environ.get("TEMP") or ".", "haavik_report_selftest.log")
        try:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write("ok=%s\n%s\n" % (ok, detail))
        except OSError:
            pass
        return 0 if ok else 1

    major, minor, name, supported = detect_windows_version()
    log.info(
        "系统检测：%s (%s.%s) 支持=%s；安装器 %s 位 → 目标架构 %s",
        name,
        major,
        minor,
        supported,
        describe_self(),
        detect_arch(),
    )
    if not supported:
        show_incompatible_and_abort()

    try:
        bundle = Bundle(resource_path(BUNDLE_NAME))
    except BundleError as exc:
        return fatal(
            "安装包不完整",
            "%s\n\n请重新获取完整的安装包，或联系提供者。" % exc,
        )

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        log.critical("无法初始化图形界面：%s", exc)
        return 1

    Installer(root, bundle)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
