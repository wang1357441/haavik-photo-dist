# -*- coding: utf-8 -*-
"""图像处理逻辑层：只依赖 Pillow 与标准库，**一行 tkinter 都不 import**。

为什么要单独一层
==================================================================
把"怎么算"和"怎么显示"分开。这个模块里的每个函数都是**纯函数**：
给一张 PIL.Image，还一张新的 PIL.Image，不碰控件、不弹对话框、不写盘
（除了 ``save_image`` 这一个显式的写盘出口）。

好处有两个，都是实打实的：
  * "打开 → 旋转 → 裁剪 → 改尺寸 → 存成 WebP"这条链路可以在**没有窗口**
    的情况下被完整测试。图像处理是最容易出静默错误的地方（方向反了、
    某一维算错、透明变成黑底），而这类错误在肉眼看界面时几乎发现不了。
  * 界面怎么改都不会弄红逻辑测试。

本文件里几条结论是**实测出来的**，不是照抄常识（两个工具链各跑了一遍，
Pillow 12.3 / 10.4 结果完全一致）：

  * ``JPEG`` 从 RGBA 直接存会抛 ``cannot write mode RGBA as JPEG``，
    所以存 JPEG 前必须把透明区域合成到白底上（见 ``prepare_for_save``）。
  * ``ICO`` 会把超大图**静默**缩到 256 以内（1200×800 存进去、读回是
    256×171），给 ``sizes`` 参数也不改变这一点。所以选 ICO 时必须提示用户。
  * ``dpi`` 只有 JPEG / PNG / BMP / TIFF 真的存得进去；
    WEBP / GIF / ICO 收下参数但读回来是 None（见 ``DPI_OK``）。
  * WebP 动图的单帧时长**读回来是 None**（GIF 正常），
    所以播放时要准备一个回退时长，不能假设一定有值。

关于动图
==================================================================
任何一次编辑都会把动图**塌成单帧**（Windows「照片」对多数操作也是这么做的）。
这是有意的选择：逐帧套用裁剪/旋转在数学上没问题，但"改了一张 GIF 的尺寸"
这个动作本身就很少见，而"用户以为自己只是改尺寸、结果动图不动了"才是真问题。
所以：动图播放只发生在**没有编辑过**的原图上，一旦编辑就退化成静态图，
并且状态栏会明确说出来。
"""
from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass, field

try:
    from PIL import (ExifTags, Image, ImageChops, ImageEnhance, ImageFilter,
                     ImageOps)
except ImportError:                       # 没有 Pillow 时整个工具不可用
    Image = None                          # type: ignore[assignment]
    ImageOps = None                       # type: ignore[assignment]
    ImageEnhance = None                   # type: ignore[assignment]
    ImageChops = None                     # type: ignore[assignment]
    ImageFilter = None                    # type: ignore[assignment]
    ExifTags = None                       # type: ignore[assignment]

log = logging.getLogger("haavik.imageops")

#: 本模块能不能干活。界面要据此给出明确说明，而不是抛 ImportError。
AVAILABLE = Image is not None

# ==========================================================================
# 格式表
# ==========================================================================
FORMATS = ("JPEG", "PNG", "WEBP", "AVIF", "BMP", "TIFF", "GIF",
           "TGA", "PCX", "PPM", "DDS", "ICO")

FMT_EXT = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "AVIF": ".avif",
           "BMP": ".bmp", "TIFF": ".tif", "GIF": ".gif", "TGA": ".tga",
           "PCX": ".pcx", "PPM": ".ppm", "DDS": ".dds", "ICO": ".ico"}

EXT_FMT = {".jpg": "JPEG", ".jpeg": "JPEG", ".jpe": "JPEG", ".png": "PNG",
           ".webp": "WEBP", ".avif": "AVIF", ".bmp": "BMP", ".dib": "BMP",
           ".tif": "TIFF", ".tiff": "TIFF", ".gif": "GIF", ".ico": "ICO",
           ".tga": "TGA", ".pcx": "PCX", ".ppm": "PPM", ".pgm": "PPM",
           ".pbm": "PPM", ".dds": "DDS"}

FMT_LABEL = {
    "JPEG": "JPEG（.jpg，照片首选，体积小，不支持透明）",
    "PNG":  "PNG（.png，无损，支持透明，适合截图 / 图标）",
    "WEBP": "WebP（.webp，同画质体积更小，支持透明与动图）",
    "AVIF": "AVIF（.avif，比 WebP 更小，支持透明，新浏览器都认）",
    "BMP":  "BMP（.bmp，无压缩，体积很大，仅用于兼容老软件）",
    "TIFF": "TIFF（.tif，印刷 / 归档常用，无损，体积偏大）",
    "GIF":  "GIF（.gif，最多 256 色，支持透明与动图）",
    "TGA":  "TGA（.tga，游戏贴图常用，支持透明，无压缩）",
    "PCX":  "PCX（.pcx，很老的格式，体积小，不支持透明）",
    "PPM":  "PPM（.ppm / .pgm / .pbm，最简单无损格式，不适合分享）",
    "DDS":  "DDS（.dds，DirectX 贴图，游戏 / 3D 工具常用）",
    "ICO":  "ICO（.ico，Windows 图标，最大 256×256）",
}
FMT_SHORT = {k: v.split("（")[0] for k, v in FMT_LABEL.items()}

#: 我们认的"图片文件"扩展名 —— **打开对话框与"删除到回收站"的白名单共用这一份**。
#:
#: 为什么要写这么长：Pillow 能读的格式其实有 70 种，而以前打开对话框只列了
#: 11 个扩展名，于是 PSD / AVIF / TGA / DDS 这些**能读的文件连选都选不中**。
#:
#: 为什么又**故意排除** ``.pdf`` / ``.eps``：Pillow 也能处理它们，但本质上
#: 是文档/印前文件，不该被这个程序当成图片打开（更不该被它删掉）。
#: 一句话能说清的白名单，比"凡是 Pillow 认的都算"值钱得多。
IMAGE_EXTS = (
    # 能另存的
    ".jpg", ".jpeg", ".jpe", ".png", ".webp", ".avif", ".bmp", ".dib",
    ".tif", ".tiff", ".gif", ".tga", ".pcx", ".ppm", ".pgm", ".pbm",
    ".dds", ".ico",
    # 只能读、不能写的（打开看看没问题，另存时转成别的格式）
    ".psd", ".jp2", ".j2k", ".mpo", ".sgi", ".im", ".xbm",
)

#: 需要"先把透明区域合成到不透明底色"的格式。
#: 为什么显式列出来而不是靠 Pillow 自己转：Pillow 对 BMP 的隐式转换是把 alpha
#: 丢掉，底色不一定是白的（可能是黑），而"透明变黑块"是用户最常抱怨的一种。
#: 自己合成，我们至少能保证底色是白的，并且能在界面上说清楚。
#: PCX / PPM 也放进来 —— 它们同样没有 alpha 通道。
NEEDS_FLATTEN = frozenset({"JPEG", "BMP", "PCX", "PPM"})

#: "不破坏透明信息"的格式。等价于 ``FORMATS - NEEDS_FLATTEN``，
#: 保留这个别名是因为它读起来是"我们要不要为透明做点什么"。
ALPHA_OK = frozenset(FORMATS) - NEEDS_FLATTEN

#: 真的会把 dpi 写进文件、并能读回来的格式（**逐个实测**出来的，见 image_ops_test
#: 的 E4 节 —— 它会把"表里说支持却没存下来"和"表里说不支持却存下来了"都报出来。
#: PCX 是这么加进来的：我原以为它不行，实测 (300,300) 能原样读回）。
DPI_OK = frozenset({"JPEG", "PNG", "BMP", "TIFF", "PCX"})

#: 会把 exif 写进去的格式。
EXIF_OK = frozenset({"JPEG", "PNG", "WEBP", "TIFF"})

#: 有"画质"这个概念（有损，质量参数有意义）的格式。
LOSSY = frozenset({"JPEG", "WEBP", "AVIF"})

#: 支持多帧的格式。
ANIM_OK = frozenset({"GIF", "WEBP"})

#: 默认画质。
DEFAULT_QUALITY = {"JPEG": 92, "WEBP": 90, "AVIF": 80}

#: WebP 动图读不到单帧时长时的回退值（毫秒）。见模块 docstring。
DEFAULT_FRAME_MS = 100

#: ICO 的硬上限。超过就必须缩，而且是 Pillow **静默**缩的。
ICO_MAX = 256

#: Windows 图标的标准尺寸。只取 ≤ 画布边长的那些，绝不放大。
ICO_STD_SIZES = (16, 24, 32, 48, 64, 128, 256)

APP_TITLE = "HAAVIK Photo"


def resample():
    """默认重采样算法。Pillow 9.1 之前没有 ``Image.Resampling``，两个工具链都要跑。"""
    return getattr(Image, "Resampling", Image).LANCZOS  # type: ignore[union-attr]


def rotate_resample():
    """``Image.rotate`` 能接受的重采样算法。

    ⚠⚠ 为什么不能直接用 :func:`resample`（2026-09-24 修的真 bug）：
    Pillow 的 ``Image.rotate`` **只接受 NEAREST / BILINEAR / BICUBIC 三种**，
    传 LANCZOS 会直接抛

        ValueError: Image.Resampling.LANCZOS (1) cannot be used.

    这跟 ``Image.resize``（那个收 LANCZOS）不是一回事 —— 名字一样、限制不同。
    因为 :func:`resample` 给的是 LANCZOS，"任意角度旋转"这条路以前**一用就炸**；
    只是没人从界面上走过（界面上的旋转按钮都是 90 的整数倍，那条路不看
    resample），所以一直没被发现。BICUBIC 是三种里质量最高的，用它。
    """
    return getattr(Image, "Resampling", Image).BICUBIC  # type: ignore[union-attr]


# ==========================================================================
# 小工具
# ==========================================================================
def human_size(n) -> str:
    """把字节数变成人看的大小。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "-"


def format_bytes_text(n) -> str:
    return human_size(n)


def ext_of(path: str) -> str:
    return os.path.splitext(path or "")[1].lower()


def fmt_of_path(path: str, default: str = "JPEG") -> str:
    """按扩展名猜输出格式；猜不出来就用 ``default``。"""
    return EXT_FMT.get(ext_of(path), default)


def all_ext_glob() -> str:
    """给文件对话框用的一串通配符。

    用 :data:`IMAGE_EXTS`（而不是"能另存的那些"）—— 因为 PSD / AVIF / TGA / DDS
    这些**读得了但写不了**的格式，用户也得能在对话框里选中它们打开。
    """
    return " ".join("*" + e for e in sorted(set(IMAGE_EXTS)))


def open_types():
    """「打开」对话框的过滤器。"""
    return (
        ("图片文件", all_ext_glob()),
        ("所有文件", "*.*"),
    )


def save_types(fmt: str):
    """「另存为」对话框的过滤器：选中的格式排第一。"""
    fmt = fmt if fmt in FORMATS else "JPEG"
    rest = [f for f in FORMATS if f != fmt]
    types = [(FMT_SHORT[fmt], "*" + FMT_EXT[fmt])]
    types += [(FMT_SHORT[o], "*" + FMT_EXT[o]) for o in rest]
    types.append(("所有文件", "*.*"))
    return types


def ico_side(size) -> int:
    """给这张图算一个合适的正方画布边长。"""
    side = max(int(size[0]), int(size[1]))
    return max(ICO_STD_SIZES[0], min(ICO_MAX, side))


def ico_sizes(size=None):
    """要写进 .ico 的尺寸清单：标准尺寸里 ≤ 画布边长的那些。"""
    side = ico_side(size) if size else ICO_MAX
    picked = [s for s in ICO_STD_SIZES if s <= side]
    return [(s, s) for s in picked] or [(side, side)]


def ico_warning(size) -> str:
    """选 ICO 时该提示什么。

    ICO 有两条**没有报错、只有后果**的规则，不说清楚就是坑：
      * 超过 256 会被静默缩小（实测 1200×800 存出来读回是 256×171）；
      * 小于 16×16 时 Pillow 会写出一个**6 字节的坏文件**（连它自己都
        读不回来）。所以我们要把画布垫到 16 —— 代价是图会被放大。
    另外 ICO 是正方形的，非正方形的图会被补透明边（内容不拉伸）。
    """
    w, h = int(size[0]), int(size[1])
    notes = []
    if max(w, h) > ICO_MAX:
        notes.append("ICO 最大 %d×%d，这张 %d×%d 会被缩小。"
                     % (ICO_MAX, ICO_MAX, w, h))
    if max(w, h) < ICO_STD_SIZES[0]:
        notes.append("ICO 最小 %d×%d，这张 %d×%d 太小，会被放大后补边。"
                     % (ICO_STD_SIZES[0], ICO_STD_SIZES[0], w, h))
    if w != h:
        notes.append("ICO 是正方形的，会补上透明边（画面本身不拉伸）。")
    return " ".join(notes)


def suggested_name(source_path, fmt: str, suffix: str = "_改") -> str:
    """默认输出文件名：``原名_改.ext``。

    **绝不返回原文件名。** 这是本工具唯一一条硬约束的落点：
    改了尺寸/格式再存回原位，是这类软件最常见的真实事故。
    """
    ext = FMT_EXT.get(fmt, ".jpg")
    if not source_path:
        return "未命名" + ext
    stem = os.path.splitext(os.path.basename(source_path))[0]
    return "%s%s%s" % (stem, suffix, ext)


# ==========================================================================
# 读取
# ==========================================================================
@dataclass
class Loaded:
    """一次读取的全部结果。``ok`` 为假时看 ``error``。"""
    ok: bool = False
    error: str = ""
    image: "object" = None                      # 第一帧（已按 EXIF 摆正）
    path: str = ""
    fmt: str = ""
    size: "tuple" = (0, 0)
    mode: str = ""
    n_frames: int = 1
    animated: bool = False
    frames: list = field(default_factory=list)  # 动图才有；元素是 PIL.Image
    durations: list = field(default_factory=list)
    exif_bytes: "bytes | None" = None
    has_alpha: bool = False
    size_bytes: int = 0


def _orientation_of(img):
    try:
        return img.getexif().get(0x0112)
    except Exception:                                       # noqa: BLE001
        return None


def load_image(path: str) -> Loaded:
    """读一张图。**不抛异常**，失败时把原因放在 ``Loaded.error`` 里。

    做了三件容易被忽略的事：
      * ``exif_transpose`` 按 EXIF 方向转正 —— 不转的话手机照片开出来是躺着的，
        而用户一旦保存就把错的朝向**固化**进去了；
      * 用 ``with`` 打开再 ``copy()`` 出来，脱离文件句柄；
      * 顺手记下帧数与单帧时长，动图才能播。
    """
    if not AVAILABLE:
        return Loaded(error="没有找到 Pillow，无法处理图片。")

    try:
        with Image.open(path) as opened:
            opened.load()
            fmt = (opened.format or "").upper()
            raw_exif = opened.info.get("exif")
            n_frames = int(getattr(opened, "n_frames", 1) or 1)
            animated = bool(getattr(opened, "is_animated", False))

            first = ImageOps.exif_transpose(opened) if ImageOps else opened
            first = first.copy()

            frames, durations = [], []
            if animated and n_frames > 1:
                for i in range(n_frames):
                    try:
                        opened.seek(i)
                        # 顺序不能换：copy() 会强制 load，而 WebP 的
                        # info['duration'] 只有在 load 之后才出现（见 frame_ms）。
                        frames.append(opened.copy())
                        durations.append(frame_ms(opened))
                    except (EOFError, OSError, ValueError) as exc:
                        log.warning("动图 %s 第 %d 帧读不出来：%s", path, i, exc)
                        break
                if frames:
                    # 第一帧同样要摆正，否则动图播起来是躺着的
                    if ImageOps and _orientation_of(frames[0]) not in (None, 1):
                        frames[0] = ImageOps.exif_transpose(frames[0])
                    n_frames = len(frames)
                    first = frames[0].copy()
                else:
                    animated, n_frames = False, 1
    except FileNotFoundError:
        return Loaded(error="文件不存在：\n%s" % path)
    except Image.DecompressionBombError:
        return Loaded(error="这张图太大了，超过了 Pillow 的安全上限（约 %s 像素）。"
                            % format(int(Image.MAX_IMAGE_PIXELS or 0), ","))
    except (OSError, ValueError, SyntaxError) as exc:
        log.warning("打开失败 %s：%s", path, exc)
        return Loaded(error="这个文件不是能识别的图片格式：\n\n%s\n\n%s"
                            % (os.path.basename(path), exc))

    try:
        size_bytes = os.path.getsize(path)
    except OSError:
        size_bytes = 0

    mode = first.mode
    return Loaded(
        ok=True, image=first, path=os.path.abspath(path), fmt=fmt,
        size=first.size, mode=mode,
        n_frames=n_frames, animated=animated,
        frames=frames, durations=durations,
        exif_bytes=raw_exif if isinstance(raw_exif, (bytes, bytearray)) else None,
        has_alpha=image_has_alpha(first),
        size_bytes=size_bytes,
    )


def frame_ms(img) -> int:
    """取当前帧的显示时长（毫秒）。

    ★ 调用时机有硬要求：**必须在这一帧被 ``load()`` 之后**再调用。

    实测（Pillow 10.4 与 12.3 一致）：
        open 后未 load          → ``info['duration']`` 是 None
        seek(1) 后仍未 load     → 还是 None
        seek(1) 后 load / copy  → 120（正常读出来了）

    这很反直觉 —— ``seek`` 明明已经跳到那一帧了。所以 ``load_image()`` 里
    是先 ``opened.copy()``（copy 会强制 load）再取时长，顺序不能调换。
    漏掉这一步的后果不是报错，而是所有 WebP 动图以 0 毫秒间隔狂翻。

    万一真的读不到（比如别的实现），回退到 ``DEFAULT_FRAME_MS``：
    播得慢一点总比疯狂翻滚强。
    """
    for key in ("duration", "timestamp"):
        v = (img.info or {}).get(key)
        try:
            v = int(v)
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return DEFAULT_FRAME_MS


def _mode_has_alpha(mode: str) -> bool:
    """这个**色彩模式**本身带 alpha 通道。

    注意 ``P``（调色板，GIF 就是 P）不算 —— 它可能用某个索引当透明色，
    也可能完全不带透明，光看模式名判断不了。要判断一张 P 图到底有没有透明，
    用 ``image_has_alpha()``。
    """
    return bool(mode) and ("A" in mode)


def image_has_alpha(img) -> bool:
    """这张**具体的图**有没有透明像素。"""
    if img is None:
        return False
    if _mode_has_alpha(img.mode):
        return True
    if img.mode == "P":
        return (img.info or {}).get("transparency") is not None
    return False


# ==========================================================================
# 尺寸数学（纯算术，不碰图片 —— 查看器缩放要用）
# ==========================================================================
def fit_size(w: int, h: int, box_w: int, box_h: int, allow_up: bool = False):
    """把 w×h 等比缩进 box_w×box_h。默认不放大（小图就按原样居中）。"""
    if w <= 0 or h <= 0 or box_w <= 0 or box_h <= 0:
        return (max(1, int(w)), max(1, int(h)))
    k = min(box_w / float(w), box_h / float(h))
    if not allow_up:
        k = min(k, 1.0)
    return (max(1, int(round(w * k))), max(1, int(round(h * k))))


def zoom_size(w: int, h: int, factor: float, lo: float = 0.02, hi: float = 40.0):
    """按倍数缩放，倍数本身被夹在 [lo, hi] 里（防止滚轮滚到 0 或天上去）。"""
    factor = max(lo, min(hi, float(factor)))
    return (max(1, int(round(w * factor))), max(1, int(round(h * factor))))


def scale_factor_for(w: int, h: int, box_w: int, box_h: int) -> float:
    """要"适应窗口"时的倍数。查看器用它把缩放率显示成 100% / 42% 之类。"""
    if w <= 0 or h <= 0 or box_w <= 0 or box_h <= 0:
        return 1.0
    return min(box_w / float(w), box_h / float(h))


def megapixels(w: int, h: int) -> float:
    return (w * h) / 1e6


# ==========================================================================
# 编辑操作（每一个都是"给一张图、还一张新图"，绝不就地改）
# ==========================================================================
def _clone(img):
    return img.copy()


def resize_px(img, w: int, h: int, *, allow_up: bool = True):
    """按像素改尺寸。``allow_up=False`` 时只缩不放。"""
    if img is None:
        return None
    w = max(1, int(round(w)))
    h = max(1, int(round(h)))
    if not allow_up:
        w, h = min(w, img.width), min(h, img.height)
    if (w, h) == img.size:
        return _clone(img)
    return img.resize((w, h), resample())


def resize_pct(img, pct: float, *, allow_up: bool = True):
    """按百分比改尺寸。"""
    if img is None:
        return None
    k = max(0.0, float(pct)) / 100.0
    return resize_px(img, img.width * k, img.height * k, allow_up=allow_up)


def rotate_cw(img):
    """顺时针 90°。注意 ``Image.rotate`` 是逆时针，所以这里用 -90。"""
    if img is None:
        return None
    return img.rotate(-90, expand=True)


def rotate_ccw(img):
    if img is None:
        return None
    return img.rotate(90, expand=True)


def rotate_180(img):
    if img is None:
        return None
    return img.rotate(180, expand=True)


def rotate_any(img, degrees: float, *, expand: bool = True, matte=(0, 0, 0, 0)):
    """任意角度旋转。**正角度 = 顺时针**（和 :func:`rotate_cw` 同向）。

    和 :func:`rotate_cw` / :func:`rotate_ccw` 保持同一套符号约定很重要：
    如果这里正数是逆时针、那里正数是顺时针，使用者在界面上按"右转"
    和说"转45度"就会得到**相反**的结果，没人搞得清哪个是哪个。

    实现上要取负：PIL 的 ``Image.rotate`` **正角度是逆时针**，所以顺时针
    得传 ``-degrees``（:func:`rotate_cw` 里也是这么做的）。

    透明格式用透明填充；不透明格式用左上角像素当底色 —— 一张白底图转 3°
    之后四个角补黑边会非常难看，取左上角像素是"看起来最不突兀"的做法。

    ⚠ 重采样必须用 :func:`rotate_resample`（BICUBIC），**不能用** LANCZOS ——
    详见那个函数说明，这是一个"原来非 90 倍数必炸"的坑。
    """
    if img is None:
        return None
    fill = matte
    if not _mode_has_alpha(img.mode):
        fill = img.getpixel((0, 0)) if img.width and img.height else (255, 255, 255)
    return img.rotate(-float(degrees), resample=rotate_resample(), expand=expand,
                      fillcolor=fill)


def flip_h(img):
    """水平翻转（左右镜像）。"""
    if img is None:
        return None
    return ImageOps.mirror(img) if ImageOps else img.transpose(Image.FLIP_LEFT_RIGHT)


def flip_v(img):
    """垂直翻转（上下镜像）。"""
    if img is None:
        return None
    return ImageOps.flip(img) if ImageOps else img.transpose(Image.FLIP_TOP_BOTTOM)


def clamp_box(box, w: int, h: int, min_side: int = 1):
    """把一个任意的裁剪框夹进图片里，并保证非空。

    用户可以在裁剪框上任意拖，滑出图片外是常态 —— 夹紧是这一层的责任，
    不能让 PIL 拿到越界的框（它会直接抛错或返回一块全黑）。
    """
    l, t, r, b = [int(round(v)) for v in box]
    if l > r:
        l, r = r, l
    if t > b:
        t, b = b, t
    l = max(0, min(l, w - min_side))
    t = max(0, min(t, h - min_side))
    r = max(l + min_side, min(r, w))
    b = max(t + min_side, min(b, h))
    return (l, t, r, b)


def crop(img, box):
    """裁剪。框会先被夹进图片范围内。"""
    if img is None:
        return None
    return img.crop(clamp_box(box, img.width, img.height))


# ---- 颜色 / 明暗 ----
def _with_alpha(img, fn):
    """在 RGB 上做变换，再把**原来的** alpha 原样贴回去。

    为什么必须这样：把一个半透明的 PNG 调亮，不该顺手把它的透明度也改掉。
    而几种常见写法都会悄悄改掉透明度 —— 直接 ``ImageOps.grayscale(RGBA)``
    会把 alpha 顶成 255（透明区变成不透明），``ImageOps.invert(RGBA)`` 会连
    alpha 一起取反（透明区变全黑），``ImageEnhance`` 的若干实现内部用
    ``Image.blend`` 混合，alpha 会被一起缩放。

    单元测试里有一条断言专门盯着这件事：调亮度前后 alpha 通道必须逐像素相等。
    """
    if img.mode in ("RGBA", "LA"):
        rgba = img.convert("RGBA")
        alpha = rgba.split()[-1]
        out = fn(rgba.convert("RGB")).convert("RGBA")
        out.putalpha(alpha)
        return out
    return fn(_rgb_safe(img))


def auto_contrast(img, cutoff: float = 0.5):
    if img is None:
        return None
    return _with_alpha(img, lambda im: ImageOps.autocontrast(im, cutoff=cutoff))


def grayscale(img):
    if img is None:
        return None
    return _with_alpha(img, ImageOps.grayscale)


def invert(img):
    if img is None:
        return None
    return _with_alpha(img, ImageOps.invert)


def sepia(img, strength: float = 1.0):
    """复古色调。用矩阵近似，不引入额外依赖。"""

    def tone(im):
        gray = ImageOps.grayscale(im)
        sep = ImageOps.colorize(gray, black=(38, 22, 12), white=(255, 240, 214))
        if strength >= 1.0:
            return sep
        return Image.blend(im, sep, max(0.0, min(1.0, strength)))

    if img is None:
        return None
    return _with_alpha(img, tone)


def posterize(img, bits: int = 4):
    if img is None:
        return None
    bits = max(1, min(8, int(bits)))
    return _with_alpha(img, lambda im: ImageOps.posterize(im, bits))


def adjust(img, *, brightness: float = 1.0, contrast: float = 1.0,
           color: float = 1.0, sharpness: float = 1.0):
    """亮度 / 对比度 / 饱和度 / 锐度。1.0 = 不变。

    用 ``ImageEnhance`` 而不是手写查找表：它已经处理好了 16 位与调色板模式
    的各种边界，自己写反而更容易在这些模式上出错。
    """
    if img is None:
        return None
    if ImageEnhance is None:
        return _clone(img)
    steps = tuple(
        (cls, float(v)) for cls, v in
        ((ImageEnhance.Brightness, brightness), (ImageEnhance.Contrast, contrast),
         (ImageEnhance.Color, color), (ImageEnhance.Sharpness, sharpness))
        if abs(float(v) - 1.0) > 1e-6
    )
    if not steps:
        return _clone(img)

    def run(im):
        out = im
        for cls, value in steps:
            out = cls(out).enhance(value)
        return out

    return _with_alpha(img, run)


def _rgb_safe(img):
    """转成 RGB 再交给只吃 RGB/L 的 ImageOps 函数（P 模式会让它们报错）。"""
    if img.mode in ("RGB", "L"):
        return img
    if img.mode in ("RGBA", "LA"):
        return img.convert("RGB")
    return img.convert("RGB")


# ==========================================================================
# 透明 / 底色
# ==========================================================================
def flatten(img, matte=(255, 255, 255)):
    """把透明区域合成到不透明底色上，返回 RGB 图。"""
    if img is None:
        return None
    if img.mode not in ("RGBA", "LA", "P"):
        return img.convert("RGB") if img.mode != "RGB" else img
    rgba = img.convert("RGBA")
    bg = Image.new("RGB", rgba.size, tuple(matte)[:3])
    bg.paste(rgba, mask=rgba.split()[-1])
    return bg


def composite_over_blur(rgba, base_rgb, radius=18):
    """把 RGBA 主体合成到「原图高斯模糊版」上 = **背景虚化**。

    主体保持清晰、背景被虚化（保留原构图、糊掉杂乱）。纯 PIL 运算，
    不跑模型 —— 所以它复用「一键抠图」出的透明通道就能做，零新增体积。

    返回 RGB 图（背景不透明）。``rgba`` 与 ``base_rgb`` 必须同尺寸。
    """
    if rgba is None or base_rgb is None:
        return None
    from PIL import ImageFilter
    subject = rgba.convert("RGBA")
    blurred = base_rgb.convert("RGB").filter(
        ImageFilter.GaussianBlur(radius)).convert("RGBA")
    # ⚠ PIL 的 ``alpha_composite(im1, im2)`` 是 **im2 在上层**（和直觉相反）。
    # 主体要在上层，所以模糊底作 im1、主体作 im2。
    return Image.alpha_composite(blurred, subject).convert("RGB")


def prepare_ico(img):
    """把图摆进一块正方形透明画布，边长夹在 [16, 256] 内。

    这两步都不是装饰，是实测出来的必需品：
      * **必须正方形**：ICO 是方形格式，Pillow 对非正方形源图只会按比例缩
        （1200×800 存出来是 256×171），Windows 拿到这种图标显示会怪。自己垫
        透明边，画面内容不被拉伸，只是外面多一圈透明。
      * **边长必须 ≥ 16**：源图小于 16 时 Pillow 会写出一个 6 字节的文件，
        连 Pillow 自己都 ``UnidentifiedImageError``。垫到 16 是唯一的兜底。
    """
    if img is None:
        return None
    side = ico_side(img.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    src = img.convert("RGBA")
    k = min(side / float(src.width), side / float(src.height))
    nw = max(1, min(side, int(round(src.width * k))))
    nh = max(1, min(side, int(round(src.height * k))))
    if (nw, nh) != src.size:
        src = src.resize((nw, nh), resample())
    canvas.paste(src, ((side - nw) // 2, (side - nh) // 2), src)
    return canvas


def prepare_for_save(img, fmt: str):
    """把图调成目标格式能接受的模式。

    RGBA 存 JPEG 会让 Pillow 直接抛 ``cannot write mode RGBA as JPEG`` ——
    用户看到的只有一句英文报错。这里提前处理掉，见 ``NEEDS_FLATTEN``。
    """
    if img is None:
        return None
    if fmt == "ICO":
        return prepare_ico(img)
    if fmt in NEEDS_FLATTEN:
        return flatten(img)
    if fmt == "GIF":
        # GIF 只有 1 位透明，Pillow 自己会转调色板；这里只需保证有 alpha 可用
        return img.convert("RGBA") if img.mode in ("LA", "P") else img
    if fmt in ("PNG", "WEBP", "TIFF"):
        if img.mode in ("RGB", "RGBA", "L", "LA", "P"):
            return img
        return img.convert("RGBA")
    return img.convert("RGB") if img.mode not in ("RGB", "CMYK") else img


# ==========================================================================
# 编码参数
# ==========================================================================
def encoder_params(fmt: str, quality: int = 92, dpi=None, exif=None,
                   size=None) -> dict:
    """按格式拼出 save 的参数。表里没有的格式就是空参数（默认行为最稳）。

    ``size`` 是**已经过 prepare_for_save 之后**的尺寸 —— 只有 ICO 用得上，
    它需要知道画布边长来决定写几个尺寸进去。
    """
    q = max(1, min(100, int(quality)))
    params: dict = {}
    if fmt == "JPEG":
        params.update(quality=q, optimize=True)
    elif fmt == "WEBP":
        params.update(quality=q, method=5)
    elif fmt == "AVIF":
        # 只传 quality：别的（speed / subsampling）各版本支持情况不一样，
        # 传了万一不认识会抛 TypeError，而"存不出来"比"慢一点"糟得多。
        params.update(quality=q)
    elif fmt == "PNG":
        params.update(optimize=True)
    elif fmt == "TIFF":
        params.update(compression="tiff_lzw")
    elif fmt == "ICO":
        params.update(sizes=ico_sizes(size))
    if dpi and fmt in DPI_OK:
        params["dpi"] = tuple(dpi)
    if exif and fmt in EXIF_OK:
        params["exif"] = bytes(exif)
    return params


def save_image(img, path: str, fmt: str, *, quality: int = 92, dpi=None,
               exif=None) -> "tuple[bool, str, int]":
    """写一张（单帧）图。返回 ``(成功?, 失败原因, 写入字节数)``。

    这是本模块**唯一**的写盘出口。调用方（界面）负责在写之前问清楚
    "要不要覆盖原图"。
    """
    if img is None:
        return False, "没有可保存的图片。", 0
    if not AVAILABLE:
        return False, "没有找到 Pillow，无法保存图片。", 0
    fmt = fmt if fmt in FORMATS else fmt_of_path(path)
    try:
        out = prepare_for_save(img, fmt)
        out.save(path, format=fmt,
                 **encoder_params(fmt, quality, dpi, exif, out.size))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("写图失败 %s (%s)：%s", path, fmt, exc)
        return False, "写入 %s 时出错：\n\n%s" % (os.path.basename(path), exc), 0
    n = os.path.getsize(path) if os.path.exists(path) else 0
    if n <= 0:
        # 一个 0 字节的产物比失败更坏 —— 用户会以为存成功了
        return False, "写入 %s 后文件是空的，已放弃。" % os.path.basename(path), 0
    log.info("已写出 %s（%s，%d 字节）", path, fmt, n)
    return True, "", n


def save_animation(frames, path: str, fmt: str = "GIF", *, durations=None,
                   loop: int = 0, quality: int = 90) -> "tuple[bool, str, int]":
    """写一张动图。只有 GIF / WEBP 支持。"""
    if not frames:
        return False, "没有可保存的帧。", 0
    fmt = fmt if fmt in ANIM_OK else "GIF"
    durations = list(durations or [])
    if len(durations) != len(frames):
        durations = [DEFAULT_FRAME_MS] * len(frames)
    params = {"save_all": True, "append_images": list(frames[1:]),
              "duration": durations, "loop": max(0, int(loop))}
    if fmt == "WEBP":
        params.update(quality=max(1, min(100, int(quality))), method=5)
    try:
        prepare_for_save(frames[0], fmt).save(path, format=fmt, **params)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("写动图失败 %s (%s)：%s", path, fmt, exc)
        return False, "写入 %s 时出错：\n\n%s" % (os.path.basename(path), exc), 0
    n = os.path.getsize(path) if os.path.exists(path) else 0
    return True, "", n


def estimate_saved_size(img, fmt: str, *, quality: int = 92, dpi=None,
                        exif=None) -> int:
    """在内存里先存一遍，量出大概多少字节。

    给"转换格式"对话框做体积预估用。注意这是个**估计**：真存到磁盘上
    得到的是同一套参数，字节数通常一致，但对 JPEG 这类格式不同 Pillow
    版本结果会有微小差异（实测 PNG 在 10.4 与 12.3 上差了几个百分点），
    所以界面上不写"精确"。
    """
    if img is None or not AVAILABLE:
        return 0
    buf = io.BytesIO()
    try:
        out = prepare_for_save(img, fmt)
        out.save(buf, format=fmt,
                 **encoder_params(fmt, quality, dpi, exif, out.size))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.debug("预估体积失败 %s：%s", fmt, exc)
        return 0
    return buf.tell()


# ==========================================================================
# 显示辅助（仍然不碰 tkinter：棋盘格返回的是一张 PIL 图）
# ==========================================================================
def checkerboard(size, cell: int = 8, light=(255, 255, 255), dark=(205, 205, 205)):
    """棋盘格底图。用来垫在透明图片下面 —— 不垫的话透明区看起来就是黑的，
    用户会以为图坏了。"""
    w, h = max(1, int(size[0])), max(1, int(size[1]))
    cell = max(2, int(cell))
    tile = Image.new("RGB", (cell * 2, cell * 2), light)
    dark = tuple(dark)[:3]
    for y in (0, cell):
        for x in (0, cell):
            if (x // cell + y // cell) % 2:
                tile.paste(dark, (x, y, x + cell, y + cell))
    out = Image.new("RGB", (w, h))
    for y in range(0, h, cell * 2):
        for x in range(0, w, cell * 2):
            out.paste(tile, (x, y))
    return out


def thumbnail(img, box, *, allow_up: bool = False):
    """缩略图。``img.copy()`` 之后再缩，避免 Pillow 的惰性求值拖住原图。"""
    if img is None:
        return None
    w, h = fit_size(img.width, img.height, box[0], box[1], allow_up=allow_up)
    return img.resize((w, h), resample())


def flatten_on(img, bg_rgb, size=None):
    """把图贴到一块纯色/棋盘底上，用于显示（不改变原图）。"""
    if img is None:
        return None
    target = tuple(size or img.size)
    out = Image.new("RGB", target, tuple(bg_rgb)[:3])
    if image_has_alpha(img):
        rgba = img.convert("RGBA")
        out.paste(rgba, ((target[0] - rgba.width) // 2,
                         (target[1] - rgba.height) // 2), rgba)
    else:
        out.paste(img.convert("RGB"), ((target[0] - img.width) // 2,
                                       (target[1] - img.height) // 2))
    return out


# ==========================================================================
# 合成：把一张图叠到另一张图上（"在图片上叠加图片"）
# ==========================================================================
#: 叠加方式：键 -> 中文名。第一项是默认值。
#: 除了"正常"以外的都走像素混合（正片叠底压暗、滤色提亮…），
#: 但仍然**只在叠加图盖到的地方**生效 —— 靠它自己的 alpha 当蒙版。
BLEND_MODES = (
    ("normal", "正常"),
    ("multiply", "正片叠底"),
    ("screen", "滤色"),
    ("overlay", "叠加"),
    ("lighten", "变亮"),
    ("darken", "变暗"),
    ("difference", "差值"),
)
BLEND_NAMES = dict(BLEND_MODES)

_BLEND_FUNCS = {
    "multiply": "multiply", "screen": "screen", "overlay": "overlay",
    "lighten": "lighter", "darken": "darker", "difference": "difference",
}


def blend_rgb(a, b, mode: str):
    """按混合方式算两张 RGB 图的像素结果（不含 alpha）。"""
    name = _BLEND_FUNCS.get(mode)
    if not name or ImageChops is None:
        return a
    fn = getattr(ImageChops, name, None)
    if fn is None:                # 该版本 Pillow 没这个函数：如实退回"正常"
        log.warning("这个 Pillow 没有 ImageChops.%s，叠加退回「正常」", name)
        return a
    return fn(a, b)


def overlay(base, patch, *, x: int = 0, y: int = 0, scale: float = 100.0,
            opacity: float = 100.0, mode: str = "normal",
            feather: float = 0.0):
    """把 ``patch`` 叠到 ``base`` 上，返回一张新的 RGBA 图（尺寸与 base 相同）。

    参数都是"界面上能直接给"的形式：

    * ``x`` / ``y``  —— 叠加图左上角在底图上的位置（可以为负，超出部分自然裁掉）；
    * ``scale``      —— 缩放百分比，100 = 原尺寸；
    * ``opacity``    —— 不透明度百分比，0 = 完全看不见；
    * ``mode``       —— :data:`BLEND_MODES` 里的键；
    * ``feather``    —— 边缘羽化像素数（把叠加图的 alpha 高斯模糊一下）。
      贴水印/贴纸时用它把硬边压软，不然一眼就能看出是"贴上去的方块"。

    为什么返回新图而不是就地改：这个程序里所有编辑都走"原图不动 + 新图进历史"，
    撤销才有东西可回退。
    """
    if base is None or patch is None:
        raise ValueError("叠加需要两张图")
    if not AVAILABLE:
        raise ValueError("没有找到 Pillow，无法叠加")
    base_rgba = base.convert("RGBA")
    layer = patch.convert("RGBA")

    factor = max(0.0, float(scale) / 100.0)
    if abs(factor - 1.0) > 1e-6 and factor > 0:
        w = max(1, int(round(layer.width * factor)))
        h = max(1, int(round(layer.height * factor)))
        layer = layer.resize((w, h), resample())

    alpha = layer.getchannel("A")
    layer.putalpha(alpha)

    # 放到和底图一样大的透明画布上：这样"位置"就只是一次 paste，
    # 负坐标/超出边界都不用自己裁剪。
    canvas = Image.new("RGBA", base_rgba.size, (0, 0, 0, 0))
    canvas.paste(layer, (int(x), int(y)))

    # ⚠️ 羽化必须在**贴到画布之后**做。
    # 叠加上来的那张图自己整块都是不透明的 —— 对它自己模糊等于"模糊一个
    # 常数"，出来仍然是硬边（不加这条注释，下次很可能又写成先模糊后 paste）。
    # 只有放到画布上、边界外侧是透明之后，模糊才会把边揉开。
    if feather and float(feather) > 0:
        canvas.putalpha(canvas.getchannel("A").filter(
            ImageFilter.GaussianBlur(float(feather))))
    op = max(0.0, min(1.0, float(opacity) / 100.0))
    if op < 1.0:
        canvas.putalpha(canvas.getchannel("A").point(
            lambda v, k=op: int(round(v * k))))

    if mode == "normal" or mode not in BLEND_NAMES:
        return Image.alpha_composite(base_rgba, canvas)

    mask = canvas.getchannel("A")
    base_rgb = base_rgba.convert("RGB")
    blended = blend_rgb(base_rgb, canvas.convert("RGB"), mode)
    out = Image.composite(blended, base_rgb, mask).convert("RGBA")
    out.putalpha(base_rgba.getchannel("A"))     # 底图的透明信息保持不变
    return out


def clipboard_to_image(data):
    """把剪贴板里的字节/对象变成一张 PIL 图（尽量宽容）。

    接受 ``bytes``（PNG/DIB/JPEG 都行，靠 Pillow 自己嗅探）或已经是 Image。
    失败返回 None —— 由界面去说"剪贴板里没有图片"。
    """
    if data is None or not AVAILABLE:
        return None
    if isinstance(data, Image.Image):
        return data
    try:
        buf = io.BytesIO(bytes(data))
        img = Image.open(buf)
        img.load()                  # 必须立刻求值：buf 出了这个函数就没人持有了
        return img
    except Exception as exc:                                # noqa: BLE001
        log.info("剪贴板里的内容不是能识别的图片：%s", exc)
        return None


# ==========================================================================
# 信息 / EXIF
# ==========================================================================
#: 常见 EXIF 标签的中文名。查不到的标签仍然会列出来，显示英文名或编号 ——
#: 宁可多一行看不懂的，也不要"这张照片的相机信息凭空少了"。
EXIF_CN = {
    "Make": "相机厂商", "Model": "相机型号", "LensModel": "镜头",
    "Software": "处理软件", "DateTime": "修改时间",
    "DateTimeOriginal": "拍摄时间", "DateTimeDigitized": "数字化时间",
    "ExposureTime": "曝光时间", "FNumber": "光圈", "ISOSpeedRatings": "ISO",
    "PhotographicSensitivity": "ISO", "ShutterSpeedValue": "快门速度",
    "ApertureValue": "光圈值", "ExposureBiasValue": "曝光补偿",
    "FocalLength": "焦距", "FocalLengthIn35mmFilm": "等效焦距",
    "Flash": "闪光灯", "WhiteBalance": "白平衡", "MeteringMode": "测光模式",
    "ExposureProgram": "曝光程序", "ExposureMode": "曝光模式",
    "SceneCaptureType": "场景类型", "Orientation": "方向",
    "XResolution": "水平分辨率", "YResolution": "垂直分辨率",
    "ResolutionUnit": "分辨率单位", "ColorSpace": "色彩空间",
    "ExifImageWidth": "原始宽度", "ExifImageHeight": "原始高度",
    "ImageWidth": "宽度", "ImageLength": "高度", "BitsPerSample": "位深",
    "Compression": "压缩方式", "Artist": "作者", "Copyright": "版权",
    "ImageDescription": "图片说明", "GPSLatitude": "纬度",
    "GPSLongitude": "经度", "GPSAltitude": "海拔", "GPSTimeStamp": "GPS 时间",
    "GPSLatitudeRef": "纬度方向", "GPSLongitudeRef": "经度方向",
    "DigitalZoomRatio": "数码变焦", "SubjectDistance": "被摄距离",
    "LightSource": "光源", "Contrast": "对比度", "Saturation": "饱和度",
    "Sharpness": "锐度", "BodySerialNumber": "机身序列号",
    "LensMake": "镜头厂商", "LensSerialNumber": "镜头序列号",
    "SceneType": "场景类型", "CustomRendered": "特殊处理",
}

#: 子 IFD 的原始 tag id（不用 ExifTags.IFD 枚举，那个在旧版 Pillow 上没有）。
_IFD_NAMES = ((0x8769, "拍摄参数"), (0x8825, "GPS 定位"), (0xA005, "互操作性"))

#: 值太长就截断，但要说清楚截断成了多少
_VALUE_LIMIT = 120


def _fmt_exif_value(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        for enc in ("utf-8", "utf-16-le", "latin-1"):
            try:
                text = raw.decode(enc).strip("\x00").strip()
            except (UnicodeDecodeError, LookupError):
                continue
            if text and all(ch.isprintable() or ch in "\r\n\t" for ch in text):
                return text
        return "%d 字节数据" % len(raw)
    if isinstance(value, (tuple, list)):
        parts = [_fmt_exif_value(v) for v in value]
        text = ", ".join(parts)
        return text if len(text) <= _VALUE_LIMIT else text[:_VALUE_LIMIT] + "…"
    # IFDRational：曝光时间写成 "1/125" 比 0.008 好懂得多
    num = getattr(value, "numerator", None)
    den = getattr(value, "denominator", None)
    if num is not None and den is not None:
        try:
            if int(den) == 1:
                return str(int(num))
            if num and abs(float(num) / float(den)) > 1e-9:
                return "%d/%d" % (int(num), int(den))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        return str(value)
    if isinstance(value, float):
        return ("%.4f" % value).rstrip("0").rstrip(".")
    text = str(value)
    return text if len(text) <= _VALUE_LIMIT else text[:_VALUE_LIMIT] + "…"


def exif_rows(img, limit: int = 80):
    """把 EXIF 摊成 ``[(中文名, 英文名, 值字符串), ...]``。

    返回值的第三项永远是字符串 —— 界面直接往上贴就行，不用再判类型。
    """
    if img is None or not AVAILABLE or ExifTags is None:
        return []
    rows = []
    try:
        exif = img.getexif()
    except Exception as exc:                                # noqa: BLE001
        log.debug("读 EXIF 失败：%s", exc)
        return []

    def walk(items):
        for tag_id, value in items:
            if tag_id in (0x8769, 0x8825, 0xA005):          # 子 IFD 单独走
                continue
            name = ExifTags.TAGS.get(tag_id, "Tag %d" % tag_id)
            rows.append((EXIF_CN.get(name, name), name, _fmt_exif_value(value)))

    try:
        walk(exif.items())
        for ifd_id, group in _IFD_NAMES:
            try:
                sub = exif.get_ifd(ifd_id)
            except Exception:                               # noqa: BLE001
                continue
            if sub:
                rows.append(("— %s —" % group, group, ""))
                walk(sub.items())
    except Exception as exc:                                # noqa: BLE001
        log.debug("展开 EXIF 失败：%s", exc)

    rows = [r for r in rows if r[2] != ""]
    if len(rows) > limit:
        rows = rows[:limit] + [("", "", "…… 还有 %d 项" % (len(rows) - limit))]
    return rows


def image_info(img, path=None, *, exif_bytes=None, animated: bool = False,
               n_frames: int = 1) -> dict:
    """汇总成一份给界面看的字典。键名就是界面上的标签，省一层翻译。"""
    if img is None:
        return {}
    dpi = (img.info or {}).get("dpi")
    dpi_text = "-"
    if dpi and len(dpi) == 2:
        try:
            dpi_text = "%.0f × %.0f" % (float(dpi[0]), float(dpi[1]))
        except (TypeError, ValueError):
            dpi_text = "-"
    nbytes = 0
    if path:
        try:
            nbytes = os.path.getsize(path)
        except OSError:
            nbytes = 0
    info = {
        "文件名": os.path.basename(path) if path else "（未保存）",
        "尺寸": "%d × %d" % (img.width, img.height),
        "像素数": "%.1f 万像素（%.1f MP）"
                  % (img.width * img.height / 1e4, megapixels(img.width, img.height)),
        "色彩模式": img.mode + ("（含透明）" if image_has_alpha(img) else ""),
        "分辨率": (dpi_text + " dpi") if dpi_text != "-" else "未标注",
        "文件大小": human_size(nbytes) if nbytes else "-",
        "文件格式": (img.format or "未知") if hasattr(img, "format") else "未知",
    }
    if animated and n_frames > 1:
        info["动画"] = "%d 帧" % n_frames
    if exif_bytes:
        info["EXIF"] = "%s 字节" % format(len(exif_bytes), ",")
    return info


__all__ = [
    "AVAILABLE", "FORMATS", "FMT_EXT", "EXT_FMT", "FMT_LABEL", "FMT_SHORT",
    "NEEDS_FLATTEN", "ALPHA_OK", "DPI_OK", "EXIF_OK", "LOSSY", "ANIM_OK",
    "DEFAULT_QUALITY", "DEFAULT_FRAME_MS", "ICO_MAX", "APP_TITLE",
    "Loaded", "load_image", "resample", "human_size", "ext_of", "fmt_of_path",
    "open_types", "save_types", "ico_warning", "suggested_name",
    "image_has_alpha", "frame_ms", "ico_side", "ico_sizes", "prepare_ico",
    "ICO_STD_SIZES",
    "fit_size", "zoom_size", "scale_factor_for", "megapixels",
    "resize_px", "resize_pct", "rotate_cw", "rotate_ccw", "rotate_180",
    "rotate_any", "flip_h", "flip_v", "clamp_box", "crop",
    "auto_contrast", "grayscale", "invert", "sepia", "posterize", "adjust",
    "flatten", "prepare_for_save", "encoder_params",
    "save_image", "save_animation", "estimate_saved_size",
    "checkerboard", "thumbnail", "flatten_on",
    "exif_rows", "image_info", "EXIF_CN",
]
