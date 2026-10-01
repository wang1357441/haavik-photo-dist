# -*- coding: utf-8 -*-
"""纯本地 AI 修图层 —— 离线、无遥测、可独立更新。

这一层解决什么
==================================================================
v1.8 起加入的「AI 修图」要跑**本地模型**（超分 / 降噪 / 人脸修复 / 一键抠图
/ 智能增强 / 图片修复），整套权重可能上 G。它有几个硬约束，决定了这一层
必须这么写：

  * **纯本地、绝不联网推理**：模型只在用户机器上跑，不把图片发出去。
    所以本文件**一个字节都不联网** —— 下载是 ``version_check`` 的事（它还
    负责 sha256 校验），这里只负责"把下好的包解开、把模型跑起来"。
  * **与核心程序版本号解耦**：1GB+ 的模型包不该每次小修小补都重下，
    所以它是一个独立的 ``ai`` 组件（见 ``version_check.parse_components``），
    只在它自己迭代时才重发；装到哪一版记在 ``state.json`` 的
    ``ai_component_version`` 里。
  * **老系统不装**：ONNX Runtime 与这些模型只在 Windows 10/11 的 64 位进程
    上有意义，Win7/XP（以及 32 位安装包）上 ``ai_supported()`` 直接返回
    False，界面也不显示 AI 入口。
  * **缺权重优雅降级**：模型文件可能还没下、或下坏了。任何能力在跑之前都
    先确认"本机支持 + 库在 + 权重在"，缺一样就返回 ``(False, 人话原因)``，
    而不是抛个看不懂的栈。

为什么这一层不碰 tkinter
==================================================================
它只吃 PIL 图、吐 PIL 图，是纯逻辑（和 ``image_ops`` 一个性质），界面那侧
（``image_tool``）负责把结果画出来。分层守住后，这一层可以在没有窗口的环境
里直接单元测试，也不用把 ONNX Runtime 这种庞然大物拖进界面依赖图。

关于"占位"
==================================================================
目前仓库里**还没有真实权重文件**：能力函数里的预处理/后处理是写好了的真代码
（用 numpy，可单测），但模型推理那段在"权重没下"时根本走不到 —— 因为
``models_present()`` 会先把关。等模型到位、``onnxruntime`` 装进打包环境，
这里不用改结构就能直接跑起来。
"""
from __future__ import annotations

import gc
import hashlib
import json
import logging
import os
import struct
import sys
import zipfile

log = logging.getLogger("haavik.ai")

# ---- 让本模块在不装 PIL / onnxruntime 的环境里也能被 import（方便单测门控逻辑） ----
try:                                        # 轻量依赖，缺了只影响"真正处理图片"，不影响模块加载
    from PIL import Image
except ImportError:                          # pragma: no cover - 取决于运行环境
    Image = None                             # type: ignore[assignment]


# --------------------------------------------------------------------------
# 能力清单（界面与模型注册表都引用这些名字，别随便改）
# --------------------------------------------------------------------------
CAP_SUPER_RES = "super_resolution"           # 超分放大
CAP_DENOISE = "denoise"                      # 降噪
CAP_FACE = "face_restore"                    # 人脸修复
CAP_MATTE = "matting"                        # 一键抠图（输出带透明通道）
CAP_SELECT_MATTE = "selective_matting"       # 选择性抠图（先自动抠，再画笔精修）
CAP_BRUSH_MATTE = "brush_matting"            # 画笔抠图（纯手动画笔，不跑模型）
CAP_ENHANCE = "enhance"                      # 智能增强
CAP_INPAINT = "inpaint"                      # 图片修复（需遮罩）
CAP_COLORIZE = "colorize"                    # 老照片上色（黑白/褪色→彩色）

CAP_LABELS = {
    CAP_SUPER_RES: "超分放大",
    CAP_DENOISE: "降噪",
    CAP_FACE: "人脸修复",
    CAP_MATTE: "一键抠图",
    CAP_SELECT_MATTE: "选择性抠图",
    CAP_BRUSH_MATTE: "画笔抠图",
    CAP_ENHANCE: "智能增强",
    CAP_INPAINT: "图片修复",
    CAP_COLORIZE: "老照片上色",
}

#: 不需要任何权重文件、也不跑模型的能力：
#:  · 「画笔抠图」完全是用户手动画笔（起点"整图都不透明"，擦掉背景即可），
#:    ai_ops.refine_matte 只用 numpy/PIL；
#:  · 「人脸修复」「智能增强」是**纯算法**实现（磨皮/提亮/对比/饱和/锐化），
#:    不依赖任何 onnx 权重，所以**没下载 92MB AI 组件也能用**。
#: 它们的意义：新装/没下模型时 AI 菜单里至少有几项真能用 —— 否则点开「AI」
#: 看到的全是"去下载"，体感就是"AI 打不开"。（2026-09-21 修，2026-09-27 扩两项）
MODEL_FREE_CAPS = (CAP_BRUSH_MATTE, CAP_FACE, CAP_ENHANCE)

#: 模型注册表：文件名 → 它服务哪些能力、以及推理时的基本参数。
#: 文件名是相对于 :func:`models_dir` 的路径；权重到位后这些文件必须出现在
#: ``models/`` 里，且被 ``ai_manifest.json`` 登记。新增模型只改这里 + 对应能力函数。
MODELS = {
    "superres": {
        # 超分/清晰化（2026-09-24 换成量化版 swin2sr_q）。
        # 实测对比（同一张测试图、CPU、onnxruntime）：
        #   swin2sr_q.onnx    20.6MB  1.98s  拉普拉斯方差 4995（锐 4.2 倍）
        #   superres_x4.onnx  50.3MB  2.30s  拉普拉斯方差 1189
        # 即"体积小 2.4 倍、更快、细节明显更多"，所以选量化版。
        # ⚠ 它是**窗口注意力**模型：H/W 必须是 patch 的倍数（实测 8），
        #   不是就 reshape 报错 —— 靠下面的 pad_multiple 补边兜住
        #   （已实测：120x90 这种"非 8 倍数"的尺寸补边后正常跑通，输出 4 倍）。
        "file": "swin2sr_q.onnx",
        "aliases": ["superres_x4.onnx", "realesrgan_x4.onnx"],
        "caps": [CAP_SUPER_RES, CAP_DENOISE],
        "scale": 4,
        "input_size": 0,                     # 0 = 任意尺寸（补边后即可）
        # 窗口注意力模型要求 H/W 是 patch 的倍数（实测 Swin2SR 需 8 的倍数），
        # 不满足会 reshape 报错 —— 推理前 pad 到倍数、跑完再裁回。1 = 不要求。
        "pad_multiple": 8,
    },
    "birefnet": {
        # 一键抠图（2026-09-27 换成 BiRefNet INT8 量化版，anakhiu/birefnet-onnx-int8）。
        # 替换原因：旧 rmbg_matting（RMBG-1.4）底层 BiRefNet 权重是 **非商用**许可，
        # 不能跟着咱们的安装包再分发（规则 #19：AI 模型必须 MIT/Apache 可商用）。
        # BiRefNet INT8 是 MIT，可商用可再分发，且更难假阳性、边缘更干净。
        # 体积 301MB（量化后）；输入 [1,3,1024,1024] f32 + **ImageNet 归一化**，
        # 输出是 raw logits（需 sigmoid 一次）才得到 [0,1] 的 alpha。
        # ⚠ 别名只登记"同契约的 BiRefNet 文件名"，**不**登记旧 rmbg_matting.onnx ——
        # 因为旧权重的输入/输出约定与本代码路径（ImageNet+sigmoid）不符，
        # 若放任别名加载会产出垃圾透明通道。旧版 AI 组件用户必须重下 1.5.0 包，
        # 这正是预期行为（远端清单版本号变了，会提示更新）。
        "file": "birefnet_int8.onnx",
        "aliases": ["birefnet_matting.onnx"],
        "caps": [CAP_MATTE, CAP_SELECT_MATTE, CAP_BRUSH_MATTE],
        "scale": 1,
        "input_size": 1024,
        "norm": "imagenet",            # 输入做 ImageNet 归一化（与 swin2sr 的 [0,1] 不同）
        "sigmoid": True,               # 输出是 raw logits，需 sigmoid 成 [0,1] alpha
    },
    "colorize": {
        # 老照片上色（2026-09-27 新增，DeOldify 量化版，MartinDelophy/deoldify-onnx-web）。
        # MIT 许可，可商用可再分发。体积 61MB。
        # 推理契约（取自作者官方 demo index.html，已实测确认）：
        #   输入名 "input"：灰度图复制成 3 通道、float32 值域 [0,255]、**不做 /255 归一化**，
        #   缩到 256x256；输出名 "out"：[1,3,H,W] 是**直接 RGB 彩图**（非 LAB a/b），
        #   后处理只需 CHW→HWC、clip 到 uint8、缩回原尺寸。**无需 LAB 合并、无需猜通道顺序**。
        "file": "deoldify.quant.onnx",
        "aliases": ["deoldify.onnx"],
        "caps": [CAP_COLORIZE],
        "scale": 1,
        "input_size": 256,
    },
    "inpainting": {
        # 去物/修复（2026-09-25 换成 MIGAN）。
        # 实测（1024x768、CPU、onnxruntime）：0.44s，任意尺寸都能跑
        # （8x8 ~ 1024x768 全试过），抹掉红块后误差仅 1.3（目标背景绿 120,160,90）。
        # 体积 28.2MB / MIT 许可（可商用、可再分发）。
        #
        # ⚠ 它的输入约定**和常见修复模型不一样**：
        #   不是"rgb 与 mask 拼成 4 通道"，而是**两个独立输入**：
        #     image: uint8 [N,3,H,W]      mask: uint8 [N,1,H,W]     -> result uint8 [N,3,H,W]
        #   而且遮罩含义**是反直觉的**：**要修的地方涂黑(0)，要保留的涂白(255)**。
        #   （踩过一次：按"白=要修"传进去 → 洞里纹丝不动、洞外反被改。）
        #   见 inpaint() 里的 two_input 分支。
        #
        # input_size=0：它吃任意尺寸，不必也不该缩到 512（缩了反而糊）。
        "file": "inpainting.onnx",
        "aliases": ["migan.onnx"],
        "caps": [CAP_INPAINT],
        "scale": 1,
        "input_size": 0,
        #: 两个独立输入的模型（True）还是 4 通道拼接的老式模型（False）。
        #: 推理时据此选喂法，见 inpaint()。
        "two_input": True,
        #: 该模型的遮罩语义：True = "要修的地方涂黑、要保留的涂白"（MIGAN 这样）。
        #: False = 常规约定（白色=要修）。inpaint() 按此决定要不要反转。
        "mask_inverted": True,
    },
}

#: 远端清单里 ``ai`` 组件的钥匙名（与 version_check.parse_components 对齐）。
COMPONENT_NAME = "ai"
#: 占位：当前代码期望的 AI 组件版本。真正要不要用、要不要更，以远端清单为准；
#: 这个值只在"本机完全没有网络/清单"时作为兜底提示用。
#:
#: 1.3.0（2026-09-24）：超分/降噪换成量化版 swin2sr_q.onnx。**必须**提版本号 ——
#: 组件版本号是客户端判断"要不要重下这个包"的唯一依据，模型换了而不动版本号，
#: 已装 1.2.0 的人永远拿不到新模型（清单里 ai.version 没变 = 不需要更新）。
#:
#: 1.4.0（2026-09-25）：补上第三个能力 —— **去物/修复（MIGAN）**。
#: 此前 inpainting 一栏一直是缺口（MODELS 里注册着、但包里没模型、
#: 来源是 USER_PICK 占位），所以"图片修复"这项对使用者一直是灰色的。
#: 现在 6 项能力里落地 3 项：超分/降噪、抠图、修复。
#: 同样**必须提版本号**，否则已装 1.3.0 的人不会重下。
#:
#: 1.5.0（2026-09-27）：两处模型变动 + 一项新能力 ——
#:   · 抠图换成 BiRefNet INT8（MIT，替掉非商用的 RMBG-1.4），输入需 ImageNet 归一化、
#:     输出需 sigmoid（见 MODELS[birefnet]）。
#:   · 新增**老照片上色（DeOldify 量化版，MIT）**，黑白/褪色照→彩色。
#: 总体积约 450MB（301+61+超分/修复），仍在 500MB 预算内。
#: **必须提版本号**：已装 1.4.0 的人清单里 ai.version 没变就不会重下，拿不到新模型。
REQUIRED_AI_VERSION = "1.5.0"

_MODEL_SESSIONS: dict = {}                   # 懒加载 + 缓存 onnx 会话


# --------------------------------------------------------------------------
# 系统门控
# --------------------------------------------------------------------------
def _is_64bit() -> bool:
    """当前进程是不是 64 位（AI 推理只在 x64 上有意义）。"""
    return struct.calcsize("P") * 8 == 64


def ai_supported() -> bool:
    """本机能不能跑 AI 修图。两个硬门槛：

    1. Windows 10/11（Win7/XP 及更早直接不支持 —— 模型与运行时都不值当）；
    2. 64 位进程（32 位安装包上 AI 一律不可用）。

    非 Windows 一律 False。判定只看系统，不碰网络、不读盘。
    """
    import shell_ops
    if not shell_ops.IS_WINDOWS:
        return False
    if not _is_64bit():
        return False
    major, minor = shell_ops.windows_version()
    return (major, minor) >= (10, 0)


#: ``ai_available`` 是界面侧更顺口的别名（按钮显示判断用这个名字）。
ai_available = ai_supported


def support_reason() -> str:
    """返回"为什么支持/不支持"的人话，给 UI 提示用。"""
    import shell_ops
    if not shell_ops.IS_WINDOWS:
        return "AI 修图仅支持 Windows"
    if not _is_64bit():
        return "AI 修图需要 64 位版本（当前是 32 位安装包）"
    major, minor = shell_ops.windows_version()
    if (major, minor) < (10, 0):
        return "AI 修图需要 Windows 10 / 11（当前系统较旧）"
    return "本机支持 AI 修图"


# --------------------------------------------------------------------------
# 模型目录与"装好没有"
# --------------------------------------------------------------------------
def models_dir() -> str:
    """模型权重落地的目录（程序目录下的 ``models/``）。

    放在程序目录而不是 %LOCALAPPDATA%，是因为这套权重是**跟着安装包走**的、
    且体积大；用户重装/换机器时它天然跟着走。这里复用 ``state_store.app_dir``
    （打包后是 exe 所在目录，源码运行是模块目录）。
    """
    import state_store
    return os.path.join(state_store.app_dir(), "models")


def ai_manifest_path() -> str:
    return os.path.join(models_dir(), "ai_manifest.json")


def check_models() -> tuple[bool, list]:
    """核对模型目录是否齐全。

    Returns:
        ``(是否齐全, 缺失文件列表)``。

    规则：``ai_manifest.json`` 是"装好"的标记，里面 ``files`` 列出每个权重
    文件的相对路径（与可选的体积）。缺清单 → 直接判未装；清单在但某个文件
    缺失或体积对不上 → 列进缺失项。
    """
    manifest = ai_manifest_path()
    if not os.path.isfile(manifest):
        return False, ["ai_manifest.json"]
    try:
        with open(manifest, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("AI 清单读不了：%s", exc)
        return False, ["ai_manifest.json"]
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return False, ["ai_manifest.json"]
    missing = []
    for entry in data["files"]:
        rel = str(entry.get("path") or "")
        if not rel:
            continue
        full = os.path.join(models_dir(), rel)
        if not os.path.isfile(full):
            missing.append(rel)
            continue
        expect = entry.get("size")
        if isinstance(expect, int) and expect > 0:
            try:
                if os.path.getsize(full) != expect:
                    missing.append(rel)
            except OSError:
                missing.append(rel)
    return (len(missing) == 0, missing)


def models_present() -> bool:
    """模型目录是否齐全（快捷键，给 UI 用）。"""
    ok, _ = check_models()
    return ok


# --------------------------------------------------------------------------
# 版本记录（装到哪一版了，与核心程序版本号独立）
# --------------------------------------------------------------------------
def ai_component_version(state: dict | None = None) -> str:
    import state_store
    if state is None:
        state = state_store.load()
    return state_store.ai_component_version(state)


def set_ai_component_version(version: str, state: dict | None = None) -> dict:
    import state_store
    if state is None:
        state = state_store.load()
    return state_store.set_ai_component_version(state, version)


def ai_remote_version(state: dict | None = None) -> str:
    """上次成功检查时看到的远端 AI 版本（空串 = 没查过）。"""
    import state_store
    if state is None:
        state = state_store.load()
    return state_store.ai_remote_version(state)


def set_ai_remote_version(version: str, state: dict | None = None) -> dict:
    """联网查到远端 AI 版本后记一笔（只作菜单提示用，见 state_store 的键注释）。"""
    import state_store
    if state is None:
        state = state_store.load()
    return state_store.set_ai_remote_version(state, version)


def ai_menu_status(installed: str = "", remote: str = "") -> tuple[str, bool]:
    """菜单上那行字该写什么。

    Returns:
        ``(文案, 是否有更新)``。

    为什么要有这个函数：**菜单是同步拼的，绝不能在拼菜单时联网** ——
    点一下「AI」就等几秒网络，比"看不到版本号"难受得多。
    所以这里只用**本地已知**的两样东西：
      * 已装版本（本地状态，永远准确）
      * **上次查到的远端版本**（缓存，可能过时）

    正因为远端那个是缓存，措辞必须诚实：
      * 知道两边版本 → 直接说「有更新 vX」或「已是最新」；
      * 没查过 → 说「点此检查更新」，**不猜**。

    ⚠ 它**不判对错、只写文案** —— 真要不要更新以联网那一刻为准。
    """
    installed = str(installed or "")
    remote = str(remote or "")
    if not installed:
        # 措辞要**能点** —— 这一行本身就是一个菜单项，光写"还没下载"
        # 用户不知道点它会下载。
        return "下载 AI 模型组件（纯本地推理，约 90 MB）", False
    if not remote:
        # 没查过就别说"已是最新"—— 那是**猜**，正是本项目最忌讳的。
        return "AI 模型 v%s（点此检查更新）" % installed, False
    if remote != installed:
        return "AI 模型有更新！v%s（你现在是 v%s）" % (remote, installed), True
    return "AI 模型 v%s（已是最新）" % installed, False


def models_size_bytes() -> int:
    """模型目录当前占多少字节（给界面显示"删除能释放多少"用）。

    纯只读：目录不存在或读不动就当 0，**绝不抛** —— 它只用来填一句提示文案，
    不该因为它把菜单构建搞崩。用 ``os.scandir`` 递归而不是 ``os.walk``：
    800+ 个文件的目录遍历快很多（实测 843 个文件约 11 ms），
    而且 entry.stat() 通常走缓存。

    ⚠ **必须防环**：Windows 的**目录联接（junction）**在
    ``is_dir(follow_symlinks=False)`` 眼里**也是目录**（它本来就是目录，
    只是带了个重解析点）。要是 models/ 里有个联接指回上层，这里就会
    无限递归 —— 而它是在"拼菜单"时跑的，卡住的表现就是**点「AI」没反应**，
    正好又回到项目最怕的那种静默失败。
    所以按真实路径去重，同一个真实目录只进一次。
    """
    total = 0
    seen = set()
    stack = [models_dir()]
    while stack:
        cur = stack.pop()
        try:
            real = os.path.normcase(os.path.realpath(cur))
        except OSError:
            continue
        if real in seen:
            continue
        seen.add(real)
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def human_size(n: int) -> str:
    """字节数说成人话（``95.4 MB``）。给 UI 文案用。"""
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "0 MB"
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024.0 or unit == "GB":
            return ("%.0f %s" % (v, unit)) if unit in ("B", "KB") else \
                   ("%.1f %s" % (v, unit))
        v /= 1024.0
    return "%.1f GB" % v


def delete_models() -> tuple[bool, str]:
    """删掉 AI 模型目录（**进回收站，可还原**），并把已装版本记录清空。

    这是"我不想用 AI 了，把这几十上百 MB 还给我"的出口。要点：

    * **先 ``clear_sessions()`` 再 ``gc.collect()``** —— 这两步缺一不可。
      onnxruntime 的 ``InferenceSession`` 会一直握着 ``.onnx`` 的文件句柄，
      Windows 上"文件被占用"就删不掉整棵目录（报共享冲突）。
      只清缓存字典还不够：真正的句柄要等对象被回收才释放，所以必须再催一次 GC。
    * 走 :func:`shell_ops.delete_tree_to_recycle_bin`，且声明
      ``require_basename="models"`` —— 传错路径时那一层会拦住。
    * **删成功才清版本号**：删失败了还去清，程序会以为"没装过"，
      界面上就变成"点一下就能重新下载"，而磁盘上其实还躺着一份半残的目录。
    * 只删 ``models/``：用户下载时缓存的 ``.pack`` 不在这个目录里，**不动它**
      —— 留着的话重建起来是"秒装"，也算给自己留条后路。

    Returns:
        ``(成功?, 说明)``。成功时说明是给界面看的一句话。
    """
    import shell_ops
    d = models_dir()
    if not os.path.isdir(d):
        return False, "本机没有安装 AI 模型（%s 不存在），不需要删除。" % d

    # 1) 放掉模型文件句柄（见 docstring：清缓存 + 催 GC，两步都要）
    clear_sessions()
    gc.collect()

    # 2) 进回收站
    ok_del, msg = shell_ops.delete_tree_to_recycle_bin(d, require_basename="models")
    if not ok_del:
        return False, "删除失败：%s（模型没有被删除，AI 功能照常可用）" % msg

    # 3) 成功后才清版本记录 → 界面会回到「下载 AI 模型组件…」
    try:
        set_ai_component_version("")
    except Exception as exc:                              # noqa: BLE001
        log.warning("模型已删，但清版本记录失败：%s", exc)
        return True, ("模型已移到回收站，但没能清掉「已装版本」记录：%s。"
                      "下次打开程序可能仍显示已安装。" % exc)
    log.info("已删除 AI 模型目录：%s", d)
    return True, "AI 模型已移到回收站（可以还原）。需要用 AI 时再下载一次即可。"


def ai_needs_update(manifest: dict) -> tuple[bool, str, dict | None]:
    """对比远端清单里的 ai 组件与本地已装版本。

    Returns:
        ``(是否需要更新, 远端 ai 版本号, 远端 ai 组件信息或 None)``。
    没有 ai 组件 / 本机不支持 / 已是最新 → 第一个元素为 False。

    AI 组件信息优先从清单顶层 ``ai`` 键取（单体清单兼容旧客户端，只有新客户端
    读它）；拿不到再退回组件清单 ``components.ai``（给未来用）。
    """
    import version_check
    info = manifest.get(COMPONENT_NAME) \
        or version_check.parse_components(manifest).get(COMPONENT_NAME)
    if not isinstance(info, dict):
        return False, "", None
    for key in ("version", "url", "sha256", "size"):
        if key not in info:
            return False, "", None
    if not ai_supported():
        return False, info.get("version", ""), info
    installed = ai_component_version()
    if installed and installed == str(info.get("version")) and models_present():
        return False, installed, info
    return True, str(info.get("version")), info


def verify_ai_integrity() -> tuple[bool, str]:
    """本地校验已安装 AI 模型组件的完整性（不联网、不下载）。

    拿安装时写下的 ``ai_manifest.json`` 当基准，逐一核对每个文件"在不在、
    体积对不对"，并额外确认 ``onnxruntime/`` 运行库目录存在（AI 真正跑起来
    必须有的）。返回 ``(是否完整, 人类可读报告)`` —— 给「校验 AI 完整性」按钮用。

    它查的是"装好之后有没有被改坏 / 删掉 / 杀软误删"，不是去和远端比版本
    （那是 :func:`ai_needs_update` 的事）。

    ⚠️ **不**用 ``models_present()`` 把关：那个函数只要"有文件大小对不上"就判
    False，会把一个**损坏的安装**误报成"没装"，反而掩盖问题。这里永远先读清单，
    清单在就逐项报"缺哪个 / 哪个大小不对"，让用户知道到底坏在哪。
    """
    manifest = ai_manifest_path()
    if not os.path.isfile(manifest):
        return False, ("AI 模型组件尚未下载/安装（找不到 ai_manifest.json），"
                       "无法校验。先点「AI 修图」下载或安装模型包。")
    try:
        with open(manifest, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return False, "AI 组件清单读不出来：%s" % exc
    version = str(data.get("version", ""))
    entries = data.get("files") if isinstance(data.get("files"), list) else []
    total = len(entries)
    problems = []
    for entry in entries:
        rel = str(entry.get("path") or "")
        if not rel:
            continue
        full = os.path.join(models_dir(), rel)
        if not os.path.isfile(full):
            problems.append("缺失：%s" % rel)
            continue
        expect = entry.get("size")
        if isinstance(expect, int) and expect > 0:
            try:
                if os.path.getsize(full) != expect:
                    problems.append("大小不符：%s" % rel)
            except OSError:
                problems.append("读不到：%s" % rel)
    ort_dir = os.path.join(models_dir(), "onnxruntime")
    if not os.path.isdir(ort_dir):
        problems.append("缺失：onnxruntime/ 运行库目录（AI 跑不起来）")
    if problems:
        shown = "\n".join("· " + p for p in problems[:25])
        more = "" if len(problems) <= 25 else "\n· …（另有 %d 项）" % (len(problems) - 25)
        return False, "AI 模型组件不完整（v%s，共 %d 个文件）：\n%s%s" % (
            version, total, shown, more)
    return True, "AI 模型组件完整（v%s，共 %d 个文件全部通过大小校验，onnxruntime 就绪）。" % (
        version, total)


# --------------------------------------------------------------------------
# 推理运行时（懒加载）
# --------------------------------------------------------------------------
def _require_pil():
    if Image is None:
        raise RuntimeError("缺少 Pillow，AI 修图无法处理图片（请确认安装包完整）")
    return Image


def _ensure_onnxruntime_importable() -> None:
    """让 onnxruntime 既能"随 AI 组件包分发"、也能"装在打包环境里"被 import 到。

    onnxruntime 体积大（带原生 DLL），我们**不把它打进主程序**，而是随 AI 组件包
    一起分发：包解压后它落在 ``models/onnxruntime/``。这里在 import 之前把那个目录
    塞进 ``sys.path``，并在 Windows 上加进 DLL 搜索目录，于是：

      * 生产环境：优先用包里自带的 onnxruntime（主程序里根本没有它，省 ~150MB）；
      * 开发/源码环境：包里没有，则回退到 venv 里 pip 装的那个，方便本地单测。

    往 ``sys.path`` 塞一个不存在或空的目录**不会**破坏 import —— Python 只是那里
    找不到就继续往后找。所以这段代码对两种环境都安全。
    """
    ort_dir = os.path.join(models_dir(), "onnxruntime")
    if not os.path.isdir(ort_dir):
        return
    if ort_dir not in sys.path:
        sys.path.insert(0, ort_dir)
    if sys.platform == "win32":
        try:
            os.add_dll_directory(ort_dir)             # Windows 上 .pyd 要找同目录的 DLL
        except (OSError, AttributeError, ValueError):
            pass


def _model_candidate_files(model_name: str) -> list:
    """某模型可接受的权重文件名（主名 + 兼容别名），按优先级排序。"""
    info = MODELS.get(model_name) or {}
    files = []
    if info.get("file"):
        files.append(info["file"])
    for alias in (info.get("aliases") or []):
        if alias not in files:
            files.append(alias)
    return files


def _resolve_model_file(model_name: str) -> str | None:
    """返回实际存在的权重文件绝对路径（主名找不到就依次找别名），都没有则 None。

    这是"改名不弄挂老用户"的关键：注册表主名可以演进（如 birefnet→rmbg），
    只要把旧名登记为别名，老安装目录里那份权重仍能被找到并直接使用。
    """
    base = models_dir()
    for name in _model_candidate_files(model_name):
        path = os.path.join(base, name)
        if os.path.isfile(path):
            return path
    return None


#: 菜单展示顺序（稳定，与历史一致）；过滤后即为"当前真正能跑的能力"。
_CAP_ORDER = (CAP_SUPER_RES, CAP_DENOISE, CAP_FACE, CAP_MATTE,
              CAP_SELECT_MATTE, CAP_BRUSH_MATTE, CAP_ENHANCE, CAP_INPAINT,
              CAP_COLORIZE)


def available_caps() -> list:
    """当前已装权重**真正跑得起来**的能力（界面菜单只列这些）。

    目的：不再"列出一个能力、点了却报模型缺失"。判据只是对应模型文件在不在
    （含兼容别名）；系统门控另有按钮层把关。返回顺序固定，便于界面稳定展示。
    """
    caps = set()
    for name in MODELS:
        if _resolve_model_file(name) is not None:
            caps.update(MODELS[name]["caps"])
    return [c for c in _CAP_ORDER if c in caps]


def cap_needs_model(cap: str) -> bool:
    """这个能力是否**必须**有权重才能跑（画笔抠图不需要 —— 见 MODEL_FREE_CAPS）。"""
    return cap not in MODEL_FREE_CAPS


def menu_caps() -> list:
    """「AI」下拉菜单该列出的能力 = 能用权重跑起来的 + 不需要权重的。

    与 :func:`available_caps` 的区别只有一处：**没下模型时也要列出画笔抠图**。
    它不碰权重，点了就能用，所以把"整机一个能力都没有"变成"至少有一项能干活"。
    """
    caps = set(available_caps())
    caps.update(MODEL_FREE_CAPS)
    return [c for c in _CAP_ORDER if c in caps]


def get_session(model_name: str):
    """懒加载并缓存某个模型的 onnx 会话。

    缺 ``onnxruntime`` 或权重文件 → 抛带人话的 RuntimeError，由上层捕获转成
    ``(False, 原因)``。会话按模型名缓存，避免重复加载那几百 MB。
    """
    cached = _MODEL_SESSIONS.get(model_name)
    if cached is not None:
        return cached
    try:
        _ensure_onnxruntime_importable()
        import onnxruntime as ort
    except ImportError as exc:                        # pragma: no cover
        raise RuntimeError(
            "缺少 onnxruntime 运行库，AI 修图跑不起来（需随 AI 模型组件一起分发，"
            "或安装到打包环境）"
        ) from exc
    info = MODELS.get(model_name)
    if info is None:
        raise RuntimeError("未知模型 %r" % model_name)
    path = _resolve_model_file(model_name)
    if path is None:
        want = " 或 ".join(_model_candidate_files(model_name)) or info["file"]
        raise RuntimeError("模型文件缺失：%s（请先下载 AI 模型组件）" % want)
    session = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
    _MODEL_SESSIONS[model_name] = session
    log.info("已加载 AI 模型会话：%s", model_name)
    return session


def clear_sessions() -> None:
    """清空缓存的 onnx 会话（测试 / 切换模型目录用）。"""
    _MODEL_SESSIONS.clear()


# --------------------------------------------------------------------------
# 预处理 / 后处理（真代码，可单测；不依赖具体模型权重）
# --------------------------------------------------------------------------
def preprocess_image(img, size: int = 0):
    """PIL 图 → CHW float32 numpy（值域 [0,1]），按需缩到 ``size``（正方形）。

    返回 ``(array, 原始尺寸)``；``size==0`` 表示不强制缩放（保持原图尺寸，
    但模型若有固定输入尺寸，调用方应传入对应 size）。
    """
    Image = _require_pil()
    import numpy as np
    if img.mode != "RGB":
        img = img.convert("RGB")
    src_w, src_h = img.size
    if size and size > 0:
        img = img.resize((size, size), Image.BILINEAR)
    arr = np.asarray(img, dtype=np.float32) / 255.0
    arr = arr.transpose(2, 0, 1)                   # HWC → CHW
    return arr, (src_w, src_h)


def postprocess_array(arr, src_size=None):
    """CHW float32 numpy → PIL RGB。``arr`` 值域 [0,1]，会先 clip 再 *255。"""
    Image = _require_pil()
    import numpy as np
    if arr.ndim == 4:                              # 批维度，取第一张
        arr = arr[0]
    arr = np.clip(arr, 0.0, 1.0)
    arr = (arr * 255.0).round().astype(np.uint8)
    arr = arr.transpose(1, 2, 0)                   # CHW → HWC
    out = Image.fromarray(arr, mode="RGB")
    if src_size is not None and out.size != src_size:
        out = out.resize(src_size, Image.BILINEAR)
    return out


def postprocess_matte(arr, src_size=None):
    """抠图专用：取模型输出的第一个通道当 alpha，合成 RGBA。

    ``arr`` 形状应为 (1, 1, H, W) 或 (1, H, W) 的 alpha 预测；返回带透明通道的
    PIL 图（RGB 复用原图，alpha 来自预测）。
    """
    Image = _require_pil()
    import numpy as np
    a = np.asarray(arr, dtype=np.float32)
    while a.ndim > 2:
        a = a[0] if a.shape[0] <= 4 else a
    a = np.clip(a, 0.0, 1.0)
    alpha = (a * 255.0).round().astype(np.uint8)
    if alpha.ndim == 3:                            # 多通道取首个
        alpha = alpha[:, :, 0]
    return alpha  # 调用方负责与原图合成 RGBA（需要原图尺寸）


def _run_sr_padded(session, model, img, multiple=None):
    """跑超分/降噪类模型：必要时 pad 到 ``multiple`` 的整数倍，输出再裁回原尺寸。

    背景：Swin2SR 这类**窗口注意力**模型要求 H/W 是 patch 的倍数（实测 8），
    真实照片尺寸任意、不满足就直接 reshape 报错。这里用**边缘复制**补边
    （比补黑边少引入伪影），推理后把放大出来的 padding 区裁掉，
    得到 ``(4h, 4w)`` 的结果。

    返回 ``(out_chw_float, (w, h))``；``out`` 已裁到 ``(scale*h, scale*w)``。
    """
    import numpy as np
    info = MODELS.get(model, {})
    if multiple is None:
        multiple = int(info.get("pad_multiple", 1) or 1)
    scale = int(info.get("scale", 4) or 4)
    arr, (w, h) = preprocess_image(img)          # CHW float32，保持原尺寸
    if multiple > 1:
        ph = (h + multiple - 1) // multiple * multiple
        pw = (w + multiple - 1) // multiple * multiple
        if (ph, pw) != (h, w):
            arr = np.pad(arr, ((0, 0), (0, ph - h), (0, pw - w)), mode="edge")
    inp = session.get_inputs()[0].name
    out = np.asarray(session.run(None, {inp: arr[None, ...]})[0])
    if out.ndim == 4:
        out = out[0]
    out = out[:, :h * scale, :w * scale]         # 裁掉 padding 放大出来的一块
    return out, (w, h)


# --------------------------------------------------------------------------
# 能力函数：统一返回值 ``(ok, 结果或人话原因)``
# --------------------------------------------------------------------------
def _gate(cap: str) -> tuple[bool, str]:
    """跑任何能力前的三道闸：支持 / 库在 / 权重在。"""
    if not ai_supported():
        return False, support_reason()
    try:
        _ensure_onnxruntime_importable()
        import onnxruntime                              # noqa: F401
    except ImportError:
        return False, "缺少 onnxruntime 运行库，AI 修图无法运行"
    ok, missing = check_models()
    if not ok:
        return False, "AI 模型尚未下载（缺：%s）" % ("、".join(missing) or "未知")
    return True, cap


def _model_for_cap(cap: str) -> str | None:
    for name, info in MODELS.items():
        if cap in info["caps"]:
            return name
    return None


def super_resolve(img, scale: int | None = None):
    """超分放大。

    注意一个坑：Real-ESRGAN 这类 x4 模型，**输出本身就是放大后的尺寸**
    （输入 w×h → 输出 4w×4h），并不是"输出原图、等你再放大"。所以这里
    直接把模型输出当最终结果，**绝不二次 resize** —— 否则会变成 16 倍、
    反而更糊。``scale`` 字段只作信息记录，不参与几何变换。
    """
    ok, why = _gate(CAP_SUPER_RES)
    if not ok:
        return False, why
    model = _model_for_cap(CAP_SUPER_RES)
    try:
        session = get_session(model)
        out, _ = _run_sr_padded(session, model, img)
        # 模型输出即最终放大图；不二次缩放（见函数说明）。
        return True, postprocess_array(out)
    except Exception as exc:                        # noqa: BLE001
        log.warning("超分失败：%s", exc)
        return False, "超分失败：%s" % exc


def denoise(img):
    """降噪（复用超分模型族里的降噪权重路径）。"""
    ok, why = _gate(CAP_DENOISE)
    if not ok:
        return False, why
    model = _model_for_cap(CAP_DENOISE)
    try:
        session = get_session(model)
        out, src = _run_sr_padded(session, model, img)
        return True, postprocess_array(out, src)     # 缩回原尺寸 = 降噪效果
    except Exception as exc:                        # noqa: BLE001
        log.warning("降噪失败：%s", exc)
        return False, "降噪失败：%s" % exc


def face_restore(img):
    """人脸修复 / 优化（磨皮 + 细节找回 + 气色；纯算法、无需权重）。

    ⚠ 这是**算法版**人脸优化，不是 GFPGAN 那种神经网络复原：用轻度高斯模糊
    与原图混合做「磨皮」柔化皮肤/细纹/噪点，再用非锐化掩膜把被磨掉的边缘
    细节补回来，最后轻微提亮 + 提饱和让气色更好。它对所有图都生效、不检测
    脸在哪，所以叫「人脸优化」更准。要更强的神经网络复原需另接 GFPGAN 权重
    （其输入约定是 [-1,1]、且底层 StyleGAN2 是非商用许可，需单独评估）。
    """
    try:
        from PIL import ImageEnhance, ImageFilter
    except ImportError as exc:                        # pragma: no cover
        return False, "缺少 Pillow，人脸修复无法处理图片（请确认安装包完整）"
    if img is None:
        return False, "先打开一张图，再用 AI 修图"
    try:
        Image = _require_pil()
        base = img.convert("RGB")
        # 1) 磨皮：原图与高斯模糊版按 35% 混合（柔化皮肤/细纹/噪点）
        blurred = base.filter(ImageFilter.GaussianBlur(radius=2.4))
        soft = Image.blend(base, blurred, 0.35)
        # 2) 细节找回：轻微非锐化掩膜，把磨掉的边缘补回来
        soft = soft.filter(ImageFilter.UnsharpMask(radius=1.5, percent=130,
                                                   threshold=3))
        # 3) 气色：轻微提亮 + 提饱和
        soft = ImageEnhance.Brightness(soft).enhance(1.04)
        soft = ImageEnhance.Color(soft).enhance(1.06)
        return True, soft.convert("RGB")
    except Exception as exc:                          # noqa: BLE001
        log.warning("人脸修复失败：%s", exc)
        return False, "人脸修复失败：%s" % exc


def matting(img):
    """一键抠图：返回带透明通道的 RGBA 图。"""
    ok, why = _gate(CAP_MATTE)
    if not ok:
        return False, why
    model = _model_for_cap(CAP_MATTE)
    try:
        Image = _require_pil()
        import numpy as np
        session = get_session(model)
        spec = MODELS[model]
        arr, _ = preprocess_image(img, size=spec["input_size"])
        # BiRefNet 这类需要 ImageNet 归一化（R/G/B 通道分别减均值除标准差），
        # 而老 rmbg_matting 是 [0,1]。靠模型注册表的 norm 字段区分，互不影响。
        if spec.get("norm") == "imagenet":
            mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)[:, None, None]
            std = np.array([0.229, 0.224, 0.225], dtype=np.float32)[:, None, None]
            arr = (arr - mean) / std
        inp = session.get_inputs()[0].name
        out = session.run(None, {inp: arr[None, ...]})[0]
        # 输出是 raw logits 的模型要先 sigmoid 一次，得到 [0,1] 的 alpha。
        if spec.get("sigmoid"):
            out = 1.0 / (1.0 + np.exp(-np.asarray(out, dtype=np.float32)))
        alpha = postprocess_matte(out)
        # 把 alpha 缩回原图尺寸并合成 RGBA
        alpha = Image.fromarray(alpha, mode="L").resize(img.size, Image.BILINEAR)
        rgb = img.convert("RGB")
        return True, Image.merge("RGBA", (rgb.split()[0], rgb.split()[1],
                                          rgb.split()[2], alpha))
    except Exception as exc:                        # noqa: BLE001
        log.warning("抠图失败：%s", exc)
        return False, "抠图失败：%s" % exc


def colorize(img):
    """老照片上色：把黑白/褪色照片还原成彩色（DeOldify 量化模型）。

    输入约定（取自作者官方 demo，已实测确认）：灰度图复制成 3 通道、
    float32 值域 **[0,255]、不做 /255 归一化**、缩到 256x256；
    输出名 "out" 是 [1,3,H,W] 的**直接 RGB 彩图**（不是 LAB 的 a/b），
    后处理只需 CHW→HWC、clip 到 uint8、缩回原尺寸。
    所以这里**不合并原图亮度、不猜通道顺序** —— 和之前"需合并 L、通道顺序待测"
    的猜测不同，实测结论是开箱即用的 RGB。

    注意：即使原图是彩色，也先转灰度再复制成 3 通道（这正是 DeOldify 的做法——
    它给亮度上色），输出即为彩色结果。
    """
    ok, why = _gate(CAP_COLORIZE)
    if not ok:
        return False, why
    model = _model_for_cap(CAP_COLORIZE)
    try:
        Image = _require_pil()
        import numpy as np
        session = get_session(model)
        base = img.convert("RGB")
        src_w, src_h = base.size
        # DeOldify 约定：灰度复制成 3 通道、[0,255]、不归一化、缩 256 平方。
        gray = base.convert("L").convert("RGB")          # 灰度复制到 3 通道
        gray = gray.resize((256, 256), Image.BILINEAR)
        arr = np.asarray(gray, dtype=np.float32)         # [0,255]，不 /255
        arr = arr.transpose(2, 0, 1)[None, ...]           # HWC→CHW，加批维度
        inp_name = session.get_inputs()[0].name          # 实测 "input"
        out = session.run(None, {inp_name: arr})[0]      # [1,3,H,W] 直接 RGB
        out = out[0].transpose(1, 2, 0)                  # CHW→HWC
        out = np.clip(out, 0, 255).round().astype(np.uint8)
        colored = Image.fromarray(out, mode="RGB").resize(
            (src_w, src_h), Image.BILINEAR)
        return True, colored
    except Exception as exc:                            # noqa: BLE001
        log.warning("上色失败：%s", exc)
        return False, "上色失败：%s" % exc


def refine_matte(base_rgba, orig_rgb, strokes):
    """选择性抠图：在自动抠好的 RGBA 上，用画笔笔画精修透明通道。

    ``base_rgba`` 是 :func:`matting` 的结果（原图尺寸、带透明通道）；
    ``orig_rgb`` 是原图的 RGB（用于在「恢复」模式下把被误删的主体补回成不透明）；
    ``strokes`` 是笔画列表，每笔为 ``(cx, cy, radius, mode)``，坐标是**原图坐标**，
    ``mode`` 为 ``"erase"``（擦除：alpha 置 0）或 ``"restore"``（恢复：alpha 置 255
    且 RGB 用原图）。返回精修后的 RGBA（新图，不改动入参）。

    纯 Pillow 运算，不跑模型——所以「选择性抠图」复用现有 44MB 的 matting 权重，
    无需任何新增模型文件，下载体积不变。
    """
    Image = _require_pil()
    from PIL import ImageDraw
    cur = base_rgba.convert("RGBA").copy()
    if not strokes:
        return cur
    base_rgb = orig_rgb.convert("RGB")
    for (cx, cy, r, mode) in strokes:
        if r <= 0:
            continue
        m = Image.new("L", cur.size, 0)
        d = ImageDraw.Draw(m)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
        if mode == "erase":
            alpha = cur.split()[3].copy()
            alpha.paste(0, mask=m)
            cur.putalpha(alpha)
        else:  # restore：补回原图、设不透明
            cur.paste(base_rgb, mask=m)
            alpha = cur.split()[3].copy()
            alpha.paste(255, mask=m)
            cur.putalpha(alpha)
    return cur


def enhance(img):
    """智能增强（亮度 / 对比度 / 色彩 / 锐度的自适应综合提升；纯算法、无需权重）。

    思路：先用「暗图自动提亮」把偏暗的图拉到舒服亮度（够亮就不动），再轻微提
    对比与饱和让画面更精神，最后用非锐化掩膜找回一点细节。全是 PIL/numpy 自适应
    运算，不依赖任何 onnx 权重，所以**没下载 AI 模型组件也能用**。
    """
    try:
        from PIL import ImageEnhance, ImageFilter
    except ImportError as exc:                        # pragma: no cover
        return False, "缺少 Pillow，智能增强无法处理图片（请确认安装包完整）"
    if img is None:
        return False, "先打开一张图，再用 AI 修图"
    try:
        Image = _require_pil()
        import numpy as np
        base = img.convert("RGB")
        arr = np.asarray(base, dtype=np.float32)
        # 1) 暗图自动提亮：偏暗才提，已够亮不动（避免过曝）
        lum = float(arr[..., :3].mean())
        if lum < 110:
            factor = min(1.35, 110.0 / max(lum, 1.0))
            arr = np.clip(arr * factor, 0, 255)
            base = Image.fromarray(arr.astype(np.uint8), "RGB")
        # 2) 轻微提对比 + 饱和，画面更精神（系数都保守）
        base = ImageEnhance.Contrast(base).enhance(1.10)
        base = ImageEnhance.Color(base).enhance(1.18)
        # 3) 非锐化掩膜找回细节（threshold 高，只锐该锐的边缘）
        base = base.filter(ImageFilter.UnsharpMask(radius=2, percent=120,
                                                    threshold=4))
        return True, base.convert("RGB")
    except Exception as exc:                          # noqa: BLE001
        log.warning("智能增强失败：%s", exc)
        return False, "智能增强失败：%s" % exc


def inpaint(img, mask):
    """图片修复：把 ``mask`` 圈出来的区域按模型补全（去物、抹水印等）。

    ``mask`` 是 L 模式，**白色=要修**（这是本函数对外的约定，和界面一致）。
    模型内部用什么约定由 ``MODELS[model]["mask_inverted"]`` 决定，这里负责翻译。

    两套喂法（见 ``MODELS[model]["two_input"]``）：

    * **two_input=True**（MIGAN 这类）：两个独立输入，直接给 uint8 原值 ——
      ``{"image": [N,3,H,W] uint8, "mask": [N,1,H,W] uint8}``。
      ⚠ 它要的遮罩是**反的**（要修的地方涂黑），所以这里必须反转。
    * **two_input=False**（老式约定）：rgb 与 mask 归一化后拼成 4 通道。
    """
    ok, why = _gate(CAP_INPAINT)
    if not ok:
        return False, why
    model = _model_for_cap(CAP_INPAINT)
    spec = MODELS[model]
    try:
        Image = _require_pil()
        import numpy as np
        session = get_session(model)
        size = spec.get("input_size") or 0

        rgb, _ = preprocess_image(img, size=size)
        # 遮罩跟着图一起缩放（size=0 时保持原尺寸）。
        # 用 NEAREST 而不是 BILINEAR：遮罩是"要 / 不要"的二值语义，
        # 双线性会在边缘造出一圈灰，模型会把它当成"半修"，边缘会脏。
        m = mask.convert("L")
        if size and size > 0:
            m = m.resize((size, size), Image.NEAREST)
        if spec.get("mask_inverted"):
            m = Image.eval(m, lambda v: 255 - v)     # 白(要修) -> 黑(要修)
        mask_u8 = np.asarray(m, dtype=np.uint8)[None, None, ...]

        if spec.get("two_input"):
            # MIGAN 这类：uint8 进、uint8 出，要按输入名分别喂。
            img_u8 = (rgb * 255.0 + 0.5).astype(np.uint8)[None, ...]
            names = [i.name for i in session.get_inputs()]
            feed = {names[0]: img_u8, names[1]: mask_u8}
            out = session.run(None, feed)[0]
            # 它的输出就是 uint8 图，不需要再 *255
            res = np.asarray(out)[0].transpose(1, 2, 0)
            return True, Image.fromarray(res.astype(np.uint8), mode="RGB").resize(
                img.size, Image.BILINEAR)

        mask_arr = mask_u8.astype(np.float32) / 255.0
        inp_rgb = session.get_inputs()[0].name
        combined = np.concatenate([rgb[None, ...], mask_arr], axis=1)
        out = session.run(None, {inp_rgb: combined})[0]
        return True, postprocess_array(out, img.size)
    except Exception as exc:                        # noqa: BLE001
        log.warning("图片修复失败：%s", exc)
        return False, "图片修复失败：%s" % exc


# --------------------------------------------------------------------------
# 解包 / 打包（下载到的 .pack 是 zip；解开即装好）
# --------------------------------------------------------------------------
# 打包时整包剔掉的非运行时目录（onnxruntime 随包分发，会夹带一堆用不上的）。
# 注意：只剔"明显非运行时"的，``tools`` 等可能被 ``onnxruntime/__init__`` 间接
# 引入的目录保留，避免 import 时缺模块（改完会实测 import 是否仍正常）。
_EXCLUDE_DIR_SEGMENTS = ("__pycache__", "test", "tests", "examples", "docs", "third_party")


def _pack_excluded(rel: str) -> bool:
    """打包时跳过非运行时文件（onnxruntime 是随包分发的，会夹带一堆用不上的）。

    排除这些：体积更小、且少触发杀软对 ``.exe`` / 测试目录的误报
    （之前整包解压时杀软锁 ``.exe`` 会让 extractall 整体失败，被报成"损坏"）。
    运行真正需要的 ``.py`` / ``.pyd`` / ``.dll`` / ``.json`` 等都保留。
    """
    for seg in rel.replace("\\", "/").split("/"):
        if seg in _EXCLUDE_DIR_SEGMENTS:
            return True
        if (seg.endswith(".pyc") or seg.endswith(".exe")
                or seg.endswith(".dist-info") or seg.endswith(".egg-info")):
            return True
    return False


def _safe_zip_names(names: list) -> bool:
    """防 Zip Slip：任何成员路径不得以 / 或盘符开头、不得含 ..。"""
    for n in names:
        if not n or n.startswith("/") or n.startswith("\\") \
           or n.startswith("..") or "/../" in n or "\\..\\" in n \
           or (len(n) >= 2 and n[1] == ":"):
            return False
    return True


def _extract_all_safe(zf: "zipfile.ZipFile", dest: str, tries: int = 6) -> list:
    """逐文件解压，避免 ``extractall`` 一旦有一个文件出错就整体失败。

    两个真实坑都踩过 / 预判到：
      * **Windows 路径超长（>260 字符）**：``models/onnxruntime/...`` 嵌套很深，
        程序装在桌面深层目录时很容易超长，``extractall`` 会整包失败、被统一报成
        "损坏"。这里给目标目录加 ``\\\\?\\`` 长路径前缀，把上限拉到 ~32k 字符。
      * **杀软在解压瞬间锁文件**：onnxruntime 自带 ``.exe``，Windows Defender 会
        边解压边扫，偶尔锁住某个文件导致 ``OSError``。这里对每个成员失败重试几次，
        给杀软一点时间放手，而不是整包判死。
    """
    import time
    bad = []
    for info in zf.infolist():
        name = info.filename
        target = os.path.join(dest, name)
        # 防 Zip Slip 的二次保险（_safe_zip_names 已整体校验过）
        if not (target == dest or target.startswith(dest + os.sep)
                or target.startswith(dest + "/")):
            bad.append(name + "(越界)")
            continue
        last = None
        for attempt in range(tries):
            try:
                zf.extract(info, dest)
                break
            except OSError as exc:
                last = exc
                time.sleep(0.3 * (attempt + 1))
        else:
            bad.append(name)
            log.warning("解包成员失败（重试 %d 次）：%s -> %s", tries, name, last)
    return bad


def apply_ai_pack(pack_path: str, version: str) -> tuple[bool, str]:
    """把下好的 ``haavik_ai.pack``（zip）解到 :func:`models_dir`，校验后登记版本。

    三道保险：zip 完整性由下载时的 sha256 保证；这里逐文件解压（长路径 +
    杀软锁重试）并防 Zip Slip，解完用 :func:`check_models` 复验清单里登记的
    文件都在。都过了才写 ``ai_component_version``，否则一律回滚到"没装好"状态。
    """
    if not os.path.isfile(pack_path):
        return False, "AI 模型包不存在：%s" % pack_path
    dest = models_dir()
    # Windows 长路径前缀：models/onnxruntime 嵌套深，装桌面深层目录时易超 260 字符
    dest_abs = os.path.abspath(dest)
    if os.name == "nt" and not dest_abs.startswith("\\\\?\\"):
        dest_abs = "\\\\?\\" + dest_abs
    try:
        with zipfile.ZipFile(pack_path, "r") as zf:
            names = zf.namelist()
            if not _safe_zip_names(names):
                return False, "AI 模型包内含非法路径，已拒绝解包（防 Zip Slip）"
            os.makedirs(dest_abs, exist_ok=True)
            bad = _extract_all_safe(zf, dest_abs)
            if bad:
                return False, "AI 模型包解包失败（文件被占用或杀软拦截）：%s" \
                             % ("、".join(bad[:3]) or "未知")
    except (zipfile.BadZipFile, OSError) as exc:
        log.warning("解包 AI 模型失败：%s", exc)
        return False, "AI 模型包损坏，解包失败：%s" % exc

    ok, missing = check_models()
    if not ok:
        return False, "AI 模型包解开了但缺文件：%s" % ("、".join(missing) or "未知")
    set_ai_component_version(version)
    log.info("AI 模型组件 %s 已安装到 %s", version, dest)
    return True, "AI 模型组件 %s 已就绪" % version


def build_ai_pack(output_path: str) -> tuple[bool, str, dict | None]:
    """把当前 ``models/`` 目录打成 ``.pack``（zip），并返回组件信息。

    给发版用（release.py 调用）：扫一遍 ``models/``，写出
    ``ai_manifest.json``（含每个文件的体积），再整体打包。**返回的尺寸与
    sha256 是 ``.pack`` 文件本身的**（也就是 ``version_check.download_component``
    下载后要核对的那个值），不是解压后的体积。

    返回 ``(成功, 路径, {version,size,sha256})``。**没有权重时**会返回
    ``(False, 原因, None)`` —— 这是正常的"还没训练/收集模型"状态，不是错误。
    """
    src = models_dir()
    if not os.path.isdir(src):
        return False, "models/ 目录不存在，无法打包 AI 组件", None
    files = []
    for root, _dirs, fs in os.walk(src):
        for f in fs:
            if f == "ai_manifest.json" and root == src:
                continue                    # 旧的清单不进包，下面重写一份
            full = os.path.join(root, f)
            rel = os.path.relpath(full, src).replace(os.sep, "/")
            if _pack_excluded(rel):
                continue
            files.append({"path": rel, "size": os.path.getsize(full)})
    if not files:
        return False, "models/ 里没有任何模型文件，无法打包", None
    manifest = {"version": REQUIRED_AI_VERSION, "files": files}
    with open(os.path.join(src, "ai_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)

    try:
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(os.path.join(src, "ai_manifest.json"), "ai_manifest.json")
            for entry in files:
                zf.write(os.path.join(src, entry["path"]), entry["path"])
    except OSError as exc:
        return False, "打包 AI 组件失败：%s" % exc, None

    # 哈希与尺寸都针对最终 .pack 文件（下载端要核对的正是它）
    digest = hashlib.sha256()
    size = 0
    with open(output_path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    info = {"version": REQUIRED_AI_VERSION, "size": size, "sha256": digest.hexdigest()}
    return True, output_path, info


__all__ = [
    "CAP_SUPER_RES", "CAP_DENOISE", "CAP_FACE", "CAP_MATTE", "CAP_SELECT_MATTE",
    "CAP_BRUSH_MATTE",
    "CAP_ENHANCE", "CAP_INPAINT", "CAP_COLORIZE", "CAP_LABELS", "MODELS",
    "COMPONENT_NAME",
    "REQUIRED_AI_VERSION", "MODEL_FREE_CAPS",
    "ai_supported", "ai_available", "support_reason", "models_dir", "check_models",
    "models_present", "ai_component_version", "set_ai_component_version",
    "ai_needs_update", "verify_ai_integrity", "get_session", "clear_sessions",
    "models_size_bytes", "human_size", "delete_models",
    "ai_remote_version", "set_ai_remote_version", "ai_menu_status",
    "available_caps", "menu_caps", "cap_needs_model", "preprocess_image",
    "postprocess_array", "postprocess_matte", "super_resolve", "denoise",
    "face_restore", "matting", "colorize", "refine_matte", "enhance", "inpaint",
    "apply_ai_pack", "build_ai_pack",
]
