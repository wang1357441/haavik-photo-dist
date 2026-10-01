# -*- coding: utf-8 -*-
"""Windows 系统集成：剪贴板 / 桌面背景 / 回收站 / 打印 / 资源管理器定位。

这一层要单独拿出来说清楚
==================================================================
前面几个模块（``image_ops`` / ``ui_theme``）只动内存和窗口。这个模块不一样 ——
它**真的会碰操作系统**。所以它是全项目里最需要"给能力 = 给预算"的一层：
每加一个能力，``verify_payload.py`` 的审计里就必须多一条对应的限制断言，
否则这个 exe 就不是"只做我们说过的那些事"了。

本模块全部的对外动作（没有第 7 条）：

  1. ``copy_image_to_clipboard``  把图放进剪贴板（CF_DIB）
  2. ``copy_text_to_clipboard``   把文本放进剪贴板（用来复制文件路径）
     ``read_image_from_clipboard`` 反过来：把剪贴板里的图读回来（叠加用）。
                                   只读，不动剪贴板里的东西。
  3. ``write_wallpaper_bmp``      把当前图写成一张 BMP —— 见下面"为什么必须落一个文件"
  4. ``set_wallpaper``            调 ``SystemParametersInfoW`` 把桌面背景换成它
  5. ``delete_to_recycle_bin``    删到**回收站**（``SHFileOperationW`` + ``FOF_ALLOWUNDO``）
  6. ``reveal_in_explorer``       在资源管理器里定位文件
  7. ``print_file`` / ``open_with_default``  交给系统默认程序（打印动词 / 打开）
  8. ``spawn_detached``           启动另一个程序（重启自己 / 打开下载好的更新包）。
                                  **只做一件事：把 PyInstaller 的内部环境变量
                                  从子进程环境里去掉**，然后交给系统启动 ——
                                  它不下载、不安装、不写任何文件。详见第 9 节。
  9. ``open_url``                 用默认浏览器打开一个 http(s) 网址
                                  （锁定页的「打开仓库」按钮，点了才发生）
 10. ``open_mailto``              用默认**邮件程序**写好一封反馈信（收件人/标题/
                                  正文填好），由用户自己点发送 —— 程序本身
                                  不发信、没有账号、没有密码。详见函数注释。

三条不会动摇的红线
==================================================================
**一、永远不永久删除。** ``delete_to_recycle_bin`` 只走 ``SHFileOperationW``
并**总是**带 ``FOF_ALLOWUNDO``。它没有"永久删除"这个分支，也不做降级重试 ——
回收站调不动就如实报错。多写一行 ``os.remove`` 当兜底，整个项目的性质就变了。

**二、只删"图片文件"和"程序自己的模型目录"。** 两条删除路径分得很清楚：

* ``delete_to_recycle_bin`` —— 只删**图片文件**。删除前逐条检查：存在、
  是文件而不是目录、扩展名在图片格式表里。
* ``delete_tree_to_recycle_bin`` —— 只删**程序自己下载的模型目录**
  （``<程序目录>\\models``）。它不是宽松版，而是单独的一条路：
  **必须**显式声明 ``require_basename="models"`` 且目录名逐字相等、
  不能是驱动器根、不能是链接/联接。

所以"这个 exe 能删掉的东西"仍然可以被一句话说清：
**你用本程序打开过的图片文件，加上它自己下载的那一个模型目录。**
桌面、文档、以及任何别的目录都不会被误伤。

**三、设为桌面背景必须落一个文件。** 这不是偷懒：Windows 换墙纸的公开接口
（``SPI_SETDESKWALLPAPER``）**只接受一个 BMP 文件路径**，不接受内存里的位图。
替代方案 ``IActiveDesktop`` 是 COM，本机安全策略直接禁掉，而且那是"能改系统设置"
这个量级的东西，不该为了省一个文件把它引进来。所以：写到系统临时目录里一个
固定名字 ``haavik_wallpaper.bmp``，**同名覆盖、不累积**，只在用户点"设为桌面背景"
的那一刻发生。
"""
from __future__ import annotations

import logging
import os
import struct
import subprocess
import sys

IS_WINDOWS = os.name == "nt"


def windows_version() -> tuple:
    """返回 ``(major, minor)``。

    非 Windows 或取不到时返回 ``(0, 0)``。AI 修图这类只在较新 Windows 上
    才能跑的能力，靠它判断"够不够新"（Win7/XP 一律被挡在门外）。
    installer.py 里有一份更完整的 ``detect_windows_version``，但那个模块
    不该被主程序依赖，所以这里留一份最小实现。
    """
    if not IS_WINDOWS:
        return (0, 0)
    try:
        v = sys.getwindowsversion()
        return (int(v.major), int(v.minor))
    except Exception as exc:                      # noqa: BLE001
        log.warning("取 Windows 版本失败：%s", exc)
        return (0, 0)

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        """``SHFileOperationW`` 的参数结构。

        ``fFlags`` 必须是 ``WORD``（16 位）而不是 DWORD —— 网上很多示例写成
        ``DWORD``，在 64 位下会把后面的字段顶偏，于是 ``fAnyOperationsAborted``
        读到垃圾，表现为"删成功了却报被取消"。
        """
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    #: 函数原型只声明一次并缓存下来。
    #:
    #: 为什么非声明不可：``HGLOBAL`` 在 64 位下是 64 位指针，而 ctypes 默认把
    #: 返回值当 ``c_int``、把没声明的参数当 ``c_int``。不声明的话
    #: ``GlobalAlloc`` 拿到手的句柄会被截断，再传给 ``SetClipboardData``
    #: 直接抛 ``OverflowError: int too long to convert``。
    #: —— 这是本文件第一次跑测试时踩到的真实报错，不是假想。
    _PROTOTYPES: dict = {}

    def _api():
        if _PROTOTYPES:
            return _PROTOTYPES
        k = ctypes.windll.kernel32
        u = ctypes.windll.user32
        s = ctypes.windll.shell32
        o = ctypes.windll.ole32

        k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k.GlobalAlloc.restype = wintypes.HGLOBAL
        k.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k.GlobalLock.restype = ctypes.c_void_p
        k.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        k.GlobalUnlock.restype = wintypes.BOOL
        k.GlobalSize.argtypes = [wintypes.HGLOBAL]
        k.GlobalSize.restype = ctypes.c_size_t
        k.GlobalFree.argtypes = [wintypes.HGLOBAL]
        k.GlobalFree.restype = wintypes.HGLOBAL
        k.Sleep.argtypes = [wintypes.DWORD]

        u.OpenClipboard.argtypes = [wintypes.HWND]
        u.OpenClipboard.restype = wintypes.BOOL
        u.EmptyClipboard.argtypes = []
        u.EmptyClipboard.restype = wintypes.BOOL
        u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HGLOBAL]
        u.SetClipboardData.restype = wintypes.HANDLE
        u.GetClipboardData.argtypes = [wintypes.UINT]
        u.GetClipboardData.restype = wintypes.HANDLE
        u.CloseClipboard.argtypes = []
        u.CloseClipboard.restype = wintypes.BOOL
        u.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        u.IsClipboardFormatAvailable.restype = wintypes.BOOL
        u.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
        u.RegisterClipboardFormatW.restype = wintypes.UINT

        # 换壁纸：第三个参数是 PVOID，要显式声明，否则 ctypes 会把传入的
        # 字符串当成整数而报 OverflowError。
        u.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                            ctypes.c_void_p, wintypes.UINT]
        u.SystemParametersInfoW.restype = wintypes.BOOL

        s.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                           wintypes.HANDLE,
                                           ctypes.POINTER(ctypes.c_wchar_p)]
        s.SHGetKnownFolderPath.restype = ctypes.c_long
        s.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
        s.SHFileOperationW.restype = ctypes.c_int

        o.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        o.CoTaskMemFree.restype = None

        _PROTOTYPES.update(kernel32=k, user32=u, shell32=s, ole32=o)
        return _PROTOTYPES

else:                                                       # 非 Windows：占位
    def _api():
        raise RuntimeError("这个模块的 Windows 接口在非 Windows 平台不可用")

log = logging.getLogger("haavik.shell")

#: 换壁纸时写的那一个文件的名字（放在系统临时目录里）。
WALLPAPER_NAME = "haavik_wallpaper.bmp"

#: 剪贴板里放一张图，太大就没意义了（300+ MB 的 DIB 会把剪贴板拖死）。
CLIPBOARD_MAX_PIXELS = 80_000_000


def _fail(ok_false, why: str, *args):
    """统一的失败出口：记一条日志，然后把原因原样返回给界面。"""
    log.warning(why, *args) if args else log.warning(why)
    return ok_false, (why % args if args else why)


# ==========================================================================
# 系统目录
# ==========================================================================
#: FOLDERID 的 GUID。用 GUID 而不是 CSIDL：CSIDL 在 Vista 之后就被"兼容保留"了，
#: 而这台机器被 360 迁移过 —— `os.path.expanduser("~") + "\\Desktop"` 指向的
#: 桌面是**错的**（真实桌面在 D:\360MoveData\...）。Known Folder API 是唯一可靠的。
#:
#: 注：``installer.py`` 里有一份功能相同的 ``get_desktop_dir()``，是有意重复的。
#: 安装器的职责边界是不去 import 图片工具这一侧的模块（那条链上挂着
#: PIL / 十几个兄弟模块），15 行 ctypes 换"安装器依赖图干净"是划算的。
_FOLDER_IDS = {
    "desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
}


def _guid(text: str):
    """把 ``{XXXXXXXX-....}`` 变成 GUID 结构体。"""
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    raw = text.strip("{}").replace("-", "")
    d1 = int(raw[0:8], 16)
    d2 = int(raw[8:12], 16)
    d3 = int(raw[12:16], 16)
    d4 = (ctypes.c_ubyte * 8)(*[int(raw[16 + i * 2:18 + i * 2], 16) for i in range(8)])
    return GUID(d1, d2, d3, d4)


def known_folder(name: str):
    """取系统已知文件夹的真实路径。取不到返回 None（不猜、不退回 ~/Desktop）。"""
    if not IS_WINDOWS:
        return None
    guid_text = _FOLDER_IDS.get(name)
    if not guid_text:
        return None
    try:
        api = _api()
        guid = _guid(guid_text)
        out = ctypes.c_wchar_p()
        res = api["shell32"].SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(out))
        if res != 0:
            log.warning("SHGetKnownFolderPath(%s) 返回 0x%08X", name, res & 0xFFFFFFFF)
            return None
        try:
            path = out.value or ""
        finally:
            api["ole32"].CoTaskMemFree(out)
        return path if path and os.path.isdir(path) else None
    except Exception:                                       # noqa: BLE001
        log.warning("取系统目录 %s 失败", name, exc_info=True)
        return None


def pictures_dir() -> str:
    """图片目录；取不到就退到用户主目录（这个兜底不会指向错的地方）。"""
    return known_folder("pictures") or os.path.expanduser("~")


def temp_dir() -> str:
    import tempfile
    try:
        return tempfile.gettempdir()
    except Exception:                                       # noqa: BLE001
        return os.environ.get("TEMP") or os.path.expanduser("~")


def wallpaper_bmp_path() -> str:
    """换壁纸时会写的那一个文件路径（固定名字，同名覆盖）。"""
    return os.path.join(temp_dir(), WALLPAPER_NAME)


# ==========================================================================
# 1 / 2. 剪贴板
# ==========================================================================
#: CF_DIB：把一张"没有文件头、只有 BITMAPINFOHEADER + 像素"的位图放进剪贴板。
CF_DIB = 8
#: CF_UNICODETEXT
CF_UNICODETEXT = 13

_GMEM_MOVEABLE = 0x0002


def dib_from_image(img) -> bytes:
    """把 PIL 图打包成 CF_DIB 需要的字节。

    三个地方极容易写错，所以都明确写出来：
      * **行必须按 4 字节对齐**（``stride``），否则图片会斜成一片；
      * **通道顺序是 BGR**，不是 RGB；
      * **行是自下而上**存的（``biHeight`` 为正数＝bottom-up）。
    """
    rgb = img.convert("RGB")
    w, h = rgb.size
    src_stride = w * 3
    stride = ((src_stride + 3) // 4) * 4
    pad = stride - src_stride
    raw = rgb.tobytes()

    rows = []
    for y in range(h - 1, -1, -1):                          # 自下而上
        row = raw[y * src_stride:(y + 1) * src_stride]
        bgr = bytearray(len(row))
        bgr[0::3] = row[2::3]                               # R 去第 3 位
        bgr[1::3] = row[1::3]
        bgr[2::3] = row[0::3]                               # B 来到第 1 位
        rows.append(bytes(bgr) + b"\x00" * pad)

    pixels = b"".join(rows)
    # BITMAPINFOHEADER 共 40 字节
    header = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0,
                         len(pixels), 2835, 2835, 0, 0)
    return header + pixels


def _copy_global_bytes(data: bytes, fmt: int, hwnd=0):
    """把一段字节以指定剪贴板格式放进去。返回 (成功?, 说明)。"""
    if not IS_WINDOWS:
        return False, "这个功能只在 Windows 上可用。"
    api = _api()
    user32, kernel32 = api["user32"], api["kernel32"]

    # 剪贴板是全局独占资源，别的程序正拿着它的时候要等一下，不能立刻放弃
    opened = False
    for _ in range(10):
        if user32.OpenClipboard(wintypes.HWND(hwnd) if hwnd else None):
            opened = True
            break
        kernel32.Sleep(30)
    if not opened:
        return False, "剪贴板正被别的程序占用，请稍后再试。"

    handle = None
    try:
        if not user32.EmptyClipboard():
            return False, "清空剪贴板失败。"
        n = len(data)
        handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, n)
        if not handle:
            return False, "分配剪贴板内存失败（图片可能太大）。"
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return False, "锁定剪贴板内存失败。"
        try:
            ctypes.memmove(ptr, data, n)
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(fmt, handle):
            return False, "写入剪贴板失败。"
        handle = None            # 交给系统了，不能再自己释放
        return True, ""
    finally:
        user32.CloseClipboard()
        if handle:
            kernel32.GlobalFree(handle)


def copy_image_to_clipboard(img, hwnd=0):
    """把一张图放进剪贴板（CF_DIB）。"""
    if img is None:
        return False, "没有可复制的图片。"
    if img.width * img.height > CLIPBOARD_MAX_PIXELS:
        return _fail(False, "这张图有 %d 万像素，太大，不适合放进剪贴板。",
                     img.width * img.height // 10000)
    try:
        data = dib_from_image(img)
    except (OSError, ValueError, struct.error) as exc:
        return _fail(False, "准备剪贴板数据失败：%s", exc)
    ok, detail = _copy_global_bytes(data, CF_DIB, hwnd)
    if ok:
        log.info("已把 %dx%d 的图放进剪贴板（%d 字节 DIB）",
                 img.width, img.height, len(data))
    return ok, detail


def copy_text_to_clipboard(text: str, hwnd=0):
    """把一段文本放进剪贴板（用来"复制文件路径"）。"""
    if text is None:
        return False, "没有可复制的文本。"
    try:
        data = str(text).encode("utf-16-le") + b"\x00\x00"
    except (UnicodeEncodeError, ValueError) as exc:
        return _fail(False, "准备剪贴板文本失败：%s", exc)
    return _copy_global_bytes(data, CF_UNICODETEXT, hwnd)


def _clipboard_global_bytes(user32, kernel32, fmt):
    """把剪贴板里某一种格式的数据整块复制出来（读不到返回 None）。

    ⚠️ ``GetClipboardData`` 的返回类型必须声明成 HANDLE：默认的 ``c_int``
    在 64 位下会把句柄截断，然后 ``GlobalLock`` 拿到一个非法地址 ——
    表现出来就是"偶尔崩一下"，而不会给你一条干净的报错。
    这个声明已经在 ``_api()`` 里做掉了。
    """
    handle = user32.GetClipboardData(fmt)
    if not handle:
        return None
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        return None
    try:
        size = kernel32.GlobalSize(handle)
        if not size:
            return None
        return ctypes.string_at(ptr, size)
    finally:
        kernel32.GlobalUnlock(handle)


def _dib_to_image(blob, ops):
    """把 CF_DIB 的字节解析成 PIL 图（解析不了返回 None）。

    只处理**无压缩的 24 / 32 位**：这两种覆盖了画图、Office、截图工具、
    以及本程序自己复制出来的图。调色板（8 位）或压缩过的 DIB 一律如实
    放弃 —— 宁可说"读不到"，也不要猜出一张花屏的图给用户。
    """
    if not blob or len(blob) < 40:
        return None
    header = struct.unpack_from("<IiiHHIIiiII", blob, 0)
    _size, width, height, _planes, bits, compression = header[:6]
    if compression != 0 or bits not in (24, 32) or width <= 0 or height == 0:
        log.info("剪贴板里的 DIB 是 %d 位 / 压缩方式 %d，暂不解析", bits, compression)
        return None
    top_down = height < 0
    height = abs(height)
    stride = ((width * bits // 8 + 3) // 4) * 4
    need = 40 + stride * height
    if len(blob) < need:
        log.info("剪贴板 DIB 比头部声明的短（%d < %d），放弃", len(blob), need)
        return None
    raw = blob[40:need]
    # ⚠️ 用 ``frombytes`` 而不是 ``frombuffer``：BGR/BGRA 是 **raw 模式**，
    # 不是 Pillow 的"图像模式"，``frombuffer`` 会直接抛
    # ``ValueError: unrecognized image mode``（实测踩过）。
    mode = "RGB" if bits == 24 else "RGBA"
    raw_mode = "BGR" if bits == 24 else "BGRA"
    try:
        img = ops.Image.frombytes(mode, (width, height), raw, "raw", raw_mode,
                                  stride, 1 if top_down else -1)
    except (ValueError, TypeError) as exc:
        log.warning("解析剪贴板 DIB 失败：%s", exc)
        return None
    return img


def read_image_from_clipboard():
    """把剪贴板里的图片读成一张 PIL 图；没有图片就返回 None。

    两条路，按可靠性排：

    1. **注册格式 ``PNG``** —— 浏览器、现代截图工具常放这个，直接把字节
       交给 Pillow，我们一个结构都不用自己解析；
    2. 退回 **``CF_DIB``** —— 画图 / Office / 以及本程序自己复制的图都放它，
       得手工解析（见 :func:`_dib_to_image` 支持到哪一步）。

    为什么值得写这段 ctypes：Windows 上 Tk **完全读不到位图剪贴板**
    （``clipboard_get`` 只能拿到文本），而"从别处复制一张图贴进来"是
    叠加功能最自然的用法。
    """
    if not IS_WINDOWS:
        return None
    try:
        import image_ops as ops
    except ImportError:                                     # pragma: no cover
        return None
    api = _api()
    user32, kernel32 = api["user32"], api["kernel32"]

    # 剪贴板可能被别的程序短暂占着，等一小会儿比直接放弃好
    opened = False
    for _ in range(10):
        if user32.OpenClipboard(None):
            opened = True
            break
        kernel32.Sleep(30)
    if not opened:
        log.info("剪贴板被别的程序占着，这次读不到图片")
        return None

    try:
        png_fmt = int(user32.RegisterClipboardFormatW("PNG") or 0)
        if png_fmt and user32.IsClipboardFormatAvailable(png_fmt):
            blob = _clipboard_global_bytes(user32, kernel32, png_fmt)
            img = ops.clipboard_to_image(blob) if blob else None
            if img is not None:
                log.info("从剪贴板读到图片（PNG 格式，%dx%d）", *img.size)
                return img
        if user32.IsClipboardFormatAvailable(CF_DIB):
            blob = _clipboard_global_bytes(user32, kernel32, CF_DIB)
            img = _dib_to_image(blob, ops) if blob else None
            if img is not None:
                log.info("从剪贴板读到图片（CF_DIB，%dx%d）", *img.size)
                return img
        log.info("剪贴板里没有能识别的图片")
        return None
    except Exception as exc:                                # noqa: BLE001
        log.warning("读剪贴板图片失败：%s", exc)
        return None
    finally:
        user32.CloseClipboard()


# ==========================================================================
# 3 / 4. 桌面背景
# ==========================================================================
def write_wallpaper_bmp(img, path=None):
    """把图写成换壁纸用的 BMP。返回 ``(成功?, 说明, 路径)``。

    走 ``image_ops`` 的保存通道（BMP 会把透明区域合成到白底），
    这样"写文件"这件事仍然只有一个实现。
    """
    if img is None:
        return False, "没有可设为背景的图片。", ""
    import image_ops
    target = path or wallpaper_bmp_path()
    ok, detail, _ = image_ops.save_image(img, target, "BMP")
    return ok, detail, target


def set_wallpaper(img):
    """把图设为桌面背景。返回 ``(成功?, 说明)``。

    会先在系统临时目录写一个 ``haavik_wallpaper.bmp``（同名覆盖）。
    见模块 docstring 里"为什么必须落一个文件"。
    """
    if not IS_WINDOWS:
        return False, "这个功能只在 Windows 上可用。"
    ok, detail, path = write_wallpaper_bmp(img)
    if not ok:
        return False, "准备壁纸文件失败：%s" % detail
    SPI_SETDESKWALLPAPER = 20
    SPIF_UPDATEINIFILE = 0x01
    SPIF_SENDCHANGE = 0x02
    try:
        res = _api()["user32"].SystemParametersInfoW(
            SPI_SETDESKWALLPAPER, 0, ctypes.c_wchar_p(path),
            SPIF_UPDATEINIFILE | SPIF_SENDCHANGE)
    except Exception as exc:                                # noqa: BLE001
        return _fail(False, "调用系统接口设置壁纸失败：%s", exc)
    if not res:
        return _fail(False, "系统拒绝了这次壁纸设置（可能被组策略限制）。")
    log.info("桌面背景已切换为 %s", path)
    return True, ""


# ==========================================================================
# 5. 删除到回收站（本项目唯一的删除通道）
# ==========================================================================
def _is_image_file(path: str) -> bool:
    """是不是"我们这一类"文件：真实存在、是文件、扩展名在图片格式表里。

    扩展名表**只有一份**，在 :data:`image_ops.IMAGE_EXTS`（打开对话框用的也是
    那一份）—— 以前这里另写了一份短的，结果漏了 ``.ico``，还跟打开对话框不一致。
    """
    if not path or not os.path.isfile(path):
        return False
    if os.path.isdir(path):
        return False
    try:
        import image_ops
        exts = image_ops.IMAGE_EXTS
    except Exception:                                       # noqa: BLE001
        # 只有一个场景会走到这里：Pillow 缺失导致 image_ops 导不进来。
        # 那时候整个程序都没法看图，这里退化成"最小可解释集合"即可 ——
        # 但**绝不**退化成"什么都行"。
        exts = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif",
                ".tiff", ".ico")
    return os.path.splitext(path)[1].lower() in exts


def _shfileop_delete(path):
    """真正调用 ``SHFileOperationW`` 把 ``path`` 放进回收站。返回 ``(成功?, 说明)``。

    **本模块唯一一处会删除东西的 Win32 调用。** 抽出来是为了单一来源 ——
    文件版（:func:`delete_to_recycle_bin`）和目录版
    （:func:`delete_tree_to_recycle_bin`）都走这里，免得那份
    "必须带 FOF_ALLOWUNDO" 的旗标代码出现第二份（出现两份就迟早会有一份漏掉）。

    只负责"删"，**不做任何类型校验** —— 校验是调用方的事，而且两个调用方
    的校验规则完全不同。
    """
    FO_DELETE = 3
    FOF_SILENT = 0x0004
    FOF_NOCONFIRMATION = 0x0010
    FOF_ALLOWUNDO = 0x0040            # ← 这一位就是"进回收站"而不是"永久删"
    FOF_NOERRORUI = 0x0400

    api = _api()

    # pFrom 必须是**双重 null 结尾**的列表。ctypes 会把 str 补一个结尾 null，
    # 所以我们自己再加一个，凑成 \0\0。
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = os.path.abspath(path) + "\x00"
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    op.fAnyOperationsAborted = False
    op.hNameMappings = None
    op.lpszProgressTitle = None

    try:
        res = api["shell32"].SHFileOperationW(ctypes.byref(op))
    except Exception as exc:                                # noqa: BLE001
        return _fail(False, "调用回收站接口失败：%s", exc)
    if res != 0:
        return _fail(False, "系统没能把它放进回收站（错误码 0x%08X）。"
                            "**没有被删除**。", res & 0xFFFFFFFF)
    if op.fAnyOperationsAborted:
        return False, "操作被取消，没有被删除。"
    if os.path.exists(path):
        return False, "系统报告已删除，但它还在原处。请手动检查一下。"
    log.info("已把 %s 放进回收站（可恢复）", path)
    return True, "已移到回收站（可以还原）"


def delete_to_recycle_bin(path):
    """把**一个图片文件**放进回收站。返回 ``(成功?, 说明)``。

    这里没有"永久删除"的分支，也没有"回收站失败就 rm"的兜底 ——
    那行兜底一旦写下去，整个项目"不会破坏你的文件"这个前提就没了。
    """
    if not IS_WINDOWS:
        return False, "删除到回收站只在 Windows 上可用。"
    if not path:
        return False, "没有指定要删除的文件。"
    if os.path.isdir(path):
        return _fail(False, "拒绝删除目录：%s（本程序只删单个图片文件）", path)
    if not os.path.isfile(path):
        return _fail(False, "文件不存在：%s", path)
    if not _is_image_file(path):
        return _fail(False, "拒绝删除非图片文件：%s（本程序只删图片）", path)

    return _shfileop_delete(path)


def delete_tree_to_recycle_bin(path, *, require_basename: str = ""):
    """把**程序自己下载的数据目录**整棵放进回收站。返回 ``(成功?, 说明)``。

    这是 :func:`delete_to_recycle_bin` 的**受控例外**，不是它的宽松版 ——
    两个函数刻意分开，各自的校验规则完全不同，绝不合并：

    * 那个只删"用户打开过的**图片文件**"（保护他的照片）；
    * 这个只删"**程序自己管理的数据目录**"，目前唯一的用途就是 AI 模型目录
      ``<程序目录>\\models``（800+ 个 ``.onnx`` / ``.py``，既不是图片、
      也不是单文件，走不了那个函数）。

    为什么明知拓宽了红线还要开这个口子：模型是**从网上下载来的、可以再下一遍**，
    用户想收回那几十上百 MB 空间是完全正当的需求。而且它**仍然进回收站**
    （可还原）、**仍然没有永久删除的分支**、仍然不做降级重试。

    ``require_basename`` 是**必填**的防呆开关：调用方要自己说清"我删的是
    ``models``"。传错路径（比如手滑传了桌面）时，这里名字对不上就会拦下来。

    动手之前判完这四条，任何一条不过都**不碰磁盘**：
      1. ``require_basename`` 非空，且与实际目录名**逐字相等**；
      2. 存在、且**是目录**（传文件进来直接拒）；
      3. **不是驱动器根**（根目录的 dirname 等于它自己）；
      4. **不是软链接 / 目录联接**（junction）—— 否则删掉一个链接，
         可能把它指向的真实目录整棵带走。
    """
    if not IS_WINDOWS:
        return False, "删除到回收站只在 Windows 上可用。"
    if not path:
        return False, "没有指定要删除的目录。"
    if not require_basename:
        return _fail(False, "内部错误：删除目录必须声明 require_basename（拒绝执行）")
    if not os.path.isdir(path):
        return _fail(False, "目录不存在：%s", path)

    abspath = os.path.abspath(path)
    # 根目录先判：它的 basename 是空串，若先走名字检查会得到一句绕口的
    # "目录名不是 'D:\'（实际是 ''）"，不如直接说清"这是驱动器根"。
    if os.path.dirname(os.path.normpath(abspath)) in ("", os.path.normpath(abspath)):
        return _fail(False, "拒绝删除驱动器根目录：%s", abspath)
    if os.path.basename(os.path.normpath(abspath)).lower() != \
            str(require_basename).lower():
        return _fail(False, "拒绝删除：目录名不是 %r（实际是 %r）",
                     require_basename, os.path.basename(os.path.normpath(abspath)))

    # 链接判定必须在 isdir 之后：对不存在的路径 realpath 也会返回字符串，
    # 拿它比较只会得到误导性的结果。
    try:
        if os.path.normcase(abspath) != os.path.normcase(os.path.realpath(abspath)):
            return _fail(False, "拒绝删除链接 / 目录联接：%s"
                                "（怕顺着链接删到别的地方）", abspath)
    except OSError as exc:                                  # noqa: BLE001
        return _fail(False, "无法确认 %s 是不是链接：%s", abspath, exc)

    return _shfileop_delete(abspath)


# ==========================================================================
# 6. 资源管理器定位
# ==========================================================================
def reveal_in_explorer(path):
    """在资源管理器里打开所在文件夹并选中这个文件。"""
    if not path or not os.path.exists(path):
        return _fail(False, "文件不存在：%s", path)
    target = os.path.normpath(os.path.abspath(path))
    try:
        # explorer 的 /select 参数后面要跟逗号，而且整段不能拆成两个参数 ——
        # `explorer /select,C:\x.jpg` 才对，写成 ['/select', path] 会被当成
        # 两个路径，结果是打开两个窗口。
        subprocess.Popen(["explorer", "/select," + target],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, ValueError) as exc:
        return _fail(False, "启动资源管理器失败：%s", exc)
    return True, ""


# ==========================================================================
# 7. 交给系统默认程序
# ==========================================================================
def print_file(path):
    """用系统默认的打印方式打印这个文件（走 ``print`` 动词）。"""
    if not IS_WINDOWS:
        return False, "这个功能只在 Windows 上可用。"
    if not path or not os.path.isfile(path):
        return _fail(False, "文件不存在：%s", path)
    try:
        os.startfile(os.path.abspath(path), "print")
    except (OSError, ValueError) as exc:
        return _fail(False, "调用打印失败：%s", exc)
    return True, "已交给系统的打印程序"


def print_image(img):
    """打印一张 PIL 图（所见即所打）。

    先把当前内存里的图导出到系统临时目录的 ``haavik_print.png``
    （同名覆盖、不累积），再走系统 ``print`` 动词。这样无论图片有没有被
    旋转 / 裁剪 / 调整过，打出来的都是用户当下在看的这张，而不是磁盘上
    那份可能已过时的原文件。
    """
    if not IS_WINDOWS:
        return False, "这个功能只在 Windows 上可用。"
    if img is None:
        return False, "没有可以打印的图片。"
    import image_ops
    target = os.path.join(temp_dir(), "haavik_print.png")
    ok, detail, _ = image_ops.save_image(img, target, "PNG")
    if not ok:
        return False, "准备打印文件失败：%s" % detail
    return print_file(target)


def open_url(url: str):
    """用系统默认浏览器打开一个网址。

    只放行 http/https —— 这不是安全检查，是"别让一个参数错误去启动什么奇怪的东西"。
    （浏览器不是 PyInstaller 打的包，所以这里不需要 clean_child_env。）
    """
    if not str(url).startswith(("http://", "https://")):
        return _fail(False, "只允许打开 http/https 网址：%r", url)
    if not IS_WINDOWS:
        return False, "这个功能只在 Windows 上可用。"
    try:
        os.startfile(url)       # 交给 shell，等价于在浏览器地址栏回车
    except (OSError, ValueError) as exc:
        return _fail(False, "打不开这个网址：%s", exc)
    return True, ""


#: mailto: 链接的长度上限（保守值）。Windows 用 ShellExecute 打开它，
#: 太长会被截断或直接失败 —— 而"截断的反馈"比"没发出去"更糟：用户以为发出去了。
MAILTO_MAX = 1800

#: 百分号编码里"不用编码"的字符（RFC 3986 unreserved）
_URL_SAFE = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~")


def _percent(text: str) -> str:
    """把文本按 UTF-8 **逐字节**做百分号编码。

    两个都不许省的理由：
      * **逐字节**，不是逐字符 —— ``urllib`` 不在这个模块的依赖图里（审计把
        ``urllib`` 钉死在 ``version_check.py``，靠它保证"只有那一个文件碰网络"），
        所以这里自己写；而按字符编码中文会乱码，必须走 UTF-8 字节。
      * **空格编码成 ``%20``**，不是 ``+``：``+`` 只在表单编码里表示空格，
        在 ``mailto:`` 里它就是一个加号（收件人真会看到 "+"）。
    """
    out = []
    for byte in text.encode("utf-8"):
        ch = chr(byte)
        out.append(ch if ch in _URL_SAFE else "%%%02X" % byte)
    return "".join(out)


def open_mailto(address: str, subject: str = "", body: str = ""):
    """用**系统默认邮件程序**写好一封新邮件（收件人 / 标题 / 正文都填好），
    然后由用户自己点「发送」。

    **这个程序自己不发任何邮件**：没有 SMTP、没有账号、没有密码 ——
    也就没有"把凭据打包进 exe"这种东西。用户看得见内容、可以改、也可以不点发送。
    这是桌面程序"发反馈"唯一诚实的做法。

    返回 ``(成功?, 说明)``。失败时界面上应当引导用户用「复制文字」。
    """
    if not address or "@" not in str(address):
        return _fail(False, "收件地址看起来不对：%r", address)
    if not IS_WINDOWS:
        return False, "这个功能只在 Windows 上可用。"
    parts = []
    if subject:
        parts.append("subject=" + _percent(subject))
    if body:
        parts.append("body=" + _percent(body))
    url = "mailto:" + str(address) + ("?" + "&".join(parts) if parts else "")
    if len(url) > MAILTO_MAX:
        return False, ("内容太长了（编码后 %d 个字符，邮件程序一次最多收 %d）——"
                       "请用「复制文字」，粘到邮件或聊天窗口里发。"
                       % (len(url), MAILTO_MAX))
    try:
        # 与 open_url 同理：目标程序是邮件客户端，不是 PyInstaller 打的包，
        # 不需要 clean_child_env。
        os.startfile(url)
    except (OSError, ValueError) as exc:
        return _fail(False, "打不开邮件程序：%s", exc)
    log.info("已把反馈交给系统邮件程序（收件人 %s，%d 字符正文）",
             address, len(body or ""))
    return True, ""


def open_with_default(path):
    """用系统默认程序打开这个文件。

    这里**不需要** clean_child_env：打开的是图片，目标一般是系统的照片查看器。
    真被"污染"的只有一种情形 —— 目标程序自己是 PyInstaller 打包的 onefile，
    而那种情况下父进程校验要求"父可执行文件 == 自己"，同为 MyAlbum.exe 是通的。
    （会出事的是"启动**另一个** PyInstaller exe"，即重启自己与打开更新包，
    那两处走 spawn_detached，见第 9 节。）
    """
    if not IS_WINDOWS:
        return False, "这个功能只在 Windows 上可用。"
    if not path or not os.path.isfile(path):
        return _fail(False, "文件不存在：%s", path)
    try:
        os.startfile(os.path.abspath(path))
    except (OSError, ValueError) as exc:
        return _fail(False, "打开失败：%s", exc)
    return True, ""


# ==========================================================================
# 8. 枚举文件夹里的图片（幻灯片 / 上一张下一张要用）
# ==========================================================================
def list_images(folder: str, recursive: bool = False, limit: int = 5000):
    """列出文件夹里的图片文件，按文件名自然序。

    用 ``os.scandir`` 而不是 ``os.listdir`` + ``stat``：同一目录下少跑一半
    系统调用。幻灯片切图时这是肉眼能感觉到的差别。
    """
    try:
        import image_ops
        exts = image_ops.IMAGE_EXTS      # 与"打开"和"删除"同一份表
    except Exception:                                       # noqa: BLE001
        exts = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif",
                ".tiff", ".ico")
    found = []
    if not folder or not os.path.isdir(folder):
        return found
    try:
        if recursive:
            for dirpath, dirnames, filenames in os.walk(folder):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    if os.path.splitext(name)[1].lower() in exts:
                        found.append(os.path.join(dirpath, name))
                        if len(found) >= limit:
                            break
        else:
            with os.scandir(folder) as it:
                for entry in it:
                    try:
                        if not entry.is_file():
                            continue
                    except OSError:
                        continue
                    if os.path.splitext(entry.name)[1].lower() in exts:
                        found.append(entry.path)
                        if len(found) >= limit:
                            break
    except OSError as exc:
        log.warning("枚举 %s 失败：%s", folder, exc)
        return found
    found.sort(key=_natural_key)
    return found


def _natural_key(path: str):
    """自然序：``img2`` 排在 ``img10`` 前面（纯字典序会把 10 排到 2 前）。

    幻灯片按字典序跳的话，第 10 张照片会插在第 2 张旁边 —— 一眼就看得出来。
    """
    name = os.path.basename(path).lower()
    parts = []
    digits = ""
    for ch in name:
        if ch.isdigit():
            digits += ch
        else:
            if digits:
                parts.append((1, int(digits), ""))
                digits = ""
            parts.append((0, 0, ch))
    if digits:
        parts.append((1, int(digits), ""))
    return parts


def is_windows() -> bool:
    return IS_WINDOWS


# ==========================================================================
# 9. 启动另一个程序（必须先清掉 PyInstaller 的内部环境变量）
# ==========================================================================
#: PyInstaller 塞进环境里的内部变量。它判断"我是不是被别人当子进程启动的"
#: 靠的就是 **_PYI_ARCHIVE_FILE**；一旦认定自己是第二阶段的子进程，它就会去
#: 核对**父进程的可执行文件必须和自己相同**，不相同就弹框拒绝启动：
#:
#:     Security validation failure: parent process has different executable!
#:
#: 我们的程序自己会拉起别的 exe（重启自己 / 打开更新包），子进程**默认继承**
#: 当前进程的环境，于是把这串变量带了下去 —— 新进程一启动就误判自己是子进程，
#: 然后弹上面那个框，而且错误信息跟我们一点关系都看不出来。
#: 2026-09-17 实测复现：干净环境正常；只要带上 _PYI_ARCHIVE_FILE +
#: _PYI_PARENT_PROCESS_LEVEL，标题为 "Error" 的框必然出现。
PYI_ENV_EXACT = ("_MEIPASS", "_MEIPASS2")
PYI_ENV_PREFIX = "_PYI_"


def clean_child_env(env=None) -> dict:
    """返回一份可以交给子进程的环境：去掉 PyInstaller 的内部变量。

    这不是在"防谁"，只是要让子进程回到"像用户双击那样"的干净起点。
    """
    base = dict(os.environ if env is None else env)
    for key in list(base):
        if key in PYI_ENV_EXACT or key.startswith(PYI_ENV_PREFIX):
            base.pop(key, None)
    return base


def spawn_detached(path: str, args=None, cwd=None):
    """启动一个程序（等价于用户在资源管理器里双击它），但**不把 PyInstaller
    的内部环境变量传下去**。

    返回 ``subprocess.Popen``；启动失败抛 OSError，由调用方负责提示用户。
    """
    cmd = [path] + [str(a) for a in (args or [])]
    return subprocess.Popen(cmd, cwd=cwd, env=clean_child_env(), shell=False)


__all__ = [
    "IS_WINDOWS", "windows_version", "WALLPAPER_NAME", "CLIPBOARD_MAX_PIXELS",
    "CF_DIB",
    "CF_UNICODETEXT", "is_windows", "known_folder", "pictures_dir", "temp_dir",
    "wallpaper_bmp_path", "dib_from_image", "copy_image_to_clipboard",
    "copy_text_to_clipboard", "read_image_from_clipboard", "write_wallpaper_bmp",
    "set_wallpaper",
    "delete_to_recycle_bin", "delete_tree_to_recycle_bin",
    "reveal_in_explorer", "print_file", "print_image",
    "open_with_default", "list_images",
    "open_url", "open_mailto", "clean_child_env", "spawn_detached",
]
