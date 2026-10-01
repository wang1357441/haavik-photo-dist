# -*- coding: utf-8 -*-
"""产物审计：载荷容器 ↔ dist/ 一致性、exe 归档真实索引、PE 版本信息与签名主体。

这个脚本回答一个问题：**"这个安装包只做我们说过的那些事吗？"**
它同时是 CODE_REVIEW_REPORT.md 里"无恶意功能"结论的可重复证据。

用法
==================================================================
请用 **64 位工具链**的 python 跑（那个环境里有 PyInstaller 和 tkinter）：

    ~/.workbuddy/cache/hvphoto/venv2/Scripts/python.exe code_review/verify_payload.py

省略路径参数时以脚本所在目录的上级为项目根。
加 ``--out <文件>`` 可改报告输出位置（默认 code_review/VERIFY_REPORT.md）。

四个章节（其中 C 有三小节）
==================================================================
A. 容器一致性
   复用 ``installer.Bundle``（容器结构的唯一权威读取实现）读出 meta.json，
   把两个架构的载荷解出来与 dist/ 逐字节比对，资源项逐个核 CRC32，
   并核对容器里的产品标识与 ``APP_VERSION`` 一致。
   **这一节是"容器内字节 == dist/ 字节"这条不变量的守门人。**

B. exe 归档内容（读**真实索引**，不做字节扫描）
   PyInstaller 的归档索引（TOC）以 类型码 标注每个条目：
   ``s`` 顶层脚本 / ``m`` 引导模块 / ``b`` 二进制 / ``x`` 数据 / ``z`` PYZ。
   于是可以下两条精确断言：
   1. **顶层脚本里除了 PyInstaller 自带的 ``pyi_rth_*`` / ``pyiboot*``，
      只允许出现该程序的入口脚本（main 或 installer）。**
      出现第三个脚本 = 有人往构建里夹带了东西。
   2. **本项目自己的模块，源码可达的集合与归档里真实存在的集合必须相等。**
      少了 = 漏打（运行期才 ModuleNotFoundError）；多了 = 夹带。

   旧版做法是拿 "socket"、"winreg" 这些名字去整个 exe 里数字节出现次数，
   于是 PIL / tkinter 依赖图带进来的模块全被报成"命中"——
   既误报（socket 真的在 PYZ 里，但没有任何本项目代码 import 它），
   又漏报（换个变量名拼出的模块名它就看不见）。已废弃。

C. 源码审计（AST，精确）
   C1 导入审计：解析入口脚本及其同目录兄弟模块，收集顶层 import，
      与 B 节的 PYZ 模块表做三态比对：

        源码   本项目代码直接 import
        依赖   只在 PYZ 里，源码没 import —— 由依赖图（PIL/tkinter/stdlib）引入
        无     归档里就没有

      "无恶意功能"这句结论靠的就是这一小节：可疑模块若全部落在"依赖"或"无"，
      说明它们不是本项目代码请求来的。

      额外一条强断言（``SINGLE_FILE_ONLY``）：
      **``urllib`` 只允许出现在 ``version_check.py``（更新）与
      ``feedback_send.py``（反馈提交）里**；**``smtplib`` 只允许出现在
      ``feedback_send.py`` 里**。整个程序**只有这几个网络出口**，
      而且每一个都发生在用户亲手点了之后。``feedback_send.py`` 另外
      被断言"源码里没有任何 URL 字面量"：地址必须由 ``main.py`` 传进去。

   C2 自成一体的模块：``serial_check.py`` 要被单独发到公开仓库当序列号生成器，
      所以它**不允许 import 任何同目录兄弟模块**（否则朋友拿到的是一份跑不起来
      的东西，而这件事在本地永远测不出来）。同时打印它的 sha256，供发布后核对。

   C3 写盘 / 执行外部程序：用 AST 找出"确定会碰磁盘或起进程"的调用
      （open 写模式、os.remove/replace/makedirs、os.startfile、
      subprocess/shutil/tempfile、``*.save|write|writestr|dump``），
      断言命中的源文件集合与 ``WRITERS_EXPECTED`` 完全一致。
      这一小节回答的是最实际的那个问题：**这个 exe 会动我机器上的什么？**

D. PE 版本信息 + 签名主体
   纯 Python 解析 exe 的 VS_VERSIONINFO（不依赖 PowerShell，避开中文编码坑），
   核对公司/产品/版权署名；签名状态复用 ``sign_build.signature_status()``。

⚠️ 自签名 ≠ 受信任：签名主体能读出来只证明"文件里盖了章"，
不代表系统或杀软认可。详见 sign_build.py 的模块说明。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import os
import sys
import tempfile
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.dirname(HERE)
OPTIMIZED = os.path.join(HERE, "optimized")
if OPTIMIZED not in sys.path:
    sys.path.insert(0, OPTIMIZED)

# 复用现役实现，而不是在审计脚本里再造一份（项目硬性约定 4：单一来源）
import payload_codec as codec          # noqa: E402  编解码 + CRC32
import sign_build                      # noqa: E402  签名状态查询
import build as build_mod              # noqa: E402  PE 头解析

# 载荷容器里装的就是这两个架构的主程序
CONTAINER_ARCHES = (("x64", "MyAlbum.exe"), ("x86", "MyAlbum_x86.exe"))

# exe -> (入口脚本名, 必须存在的内嵌数据)
EXE_SPECS = {
    "MyAlbum.exe": ("main", ("haavik.png", "selfcheck_modules.txt")),
    "MyAlbum_x86.exe": ("main", ("haavik.png", "selfcheck_modules.txt")),
    "Setup.exe": ("installer", ("payload.zip",)),
}

# PyInstaller 自带的引导 / 运行时钩子（三个 exe 上实测一致）。
# 它们的名字由 PyInstaller 固定，任何版本都不该出现第四个。
PYI_ROLES = (
    "pyimod01_archive", "pyimod02_importers", "pyimod03_ctypes",
    "pyimod04_pywin32", "struct",
    "pyiboot01_bootstrap",
)
PYI_HOOK_PREFIX = "pyi_rth_"

# 可疑模块：名字 -> 为什么值得看。只列顶层包名。
SUSPICIOUS = {
    "socket": "网络套接字",
    "ssl": "TLS",
    "urllib": "HTTP 客户端",
    "http": "HTTP",
    "requests": "HTTP 客户端（三方）",
    "ftplib": "FTP",
    "smtplib": "发邮件",
    "telnetlib": "Telnet",
    "winreg": "读写注册表（持久化常用）",
    "ctypes": "调用任意 DLL",
    "subprocess": "启动子进程",
    "paramiko": "SSH",
    "psutil": "进程 / 系统信息",
    "pyautogui": "模拟鼠标键盘",
    "keyboard": "全局键盘钩子",
    "pynput": "全局输入监听",
    "cryptography": "加密库",
    "Crypto": "加密库（pycryptodome）",
    "selenium": "浏览器自动化",
    "pyperclip": "剪贴板",
    "win32api": "Win32 API",
    "wmi": "WMI 查询",
    "schedule": "定时任务",
}

# 红线模块：这些能力只要被**本项目源码直接 import**，就是必须停下来人工确认的事。
# 换句话说，"无持久化 / 无输入钩子 / 无任意进程与屏幕访问"这句结论就是靠这个集合守住的。
# 注意 urllib 已移出红线：它被 version_check.py 合法使用，见下面的白名单与
# SINGLE_FILE_ONLY —— 移出红线的代价由"只允许出现在那一个文件里"这条断言来偿还。
REDLINE = frozenset(SUSPICIOUS) - {"ctypes", "subprocess", "urllib", "smtplib"}

# 双用途模块：允许本项目源码使用，但**每一条都必须写明用途**，
# 报告里会连同来源行号一起打出来供人工复核。
# 想往这里加东西之前，先想清楚：它会不会让"这个 exe 只做这一件事"不再成立？
ALLOWED_IN_SOURCE = {
    "ctypes": ("读 FOLDERID_Desktop 拿桌面真实路径（这台机器被 360 迁移过，"
               "~\\Desktop 是错的）/ 深色标题栏（dwmapi，失败即静默退回）/ "
               "剪贴板 CF_DIB **写与读**（读是为了「叠加图片」里的"
               "「用剪贴板」）/ 设壁纸 SystemParametersInfoW / 回收站 "
               "SHFileOperationW"),
    "subprocess": ("启动 shutdown 做倒计时 / 取桌面路径的 PowerShell 兜底 / "
                   "explorer /select 定位文件 / shell_ops.spawn_detached 启动"
                   "另一个程序（重启自己或打开用户确认过的更新包，会先清掉 "
                   "PyInstaller 的内部环境变量）"),
    "smtplib": ("反馈：用户亲手点「直接发送」后，程序直连**发信邮箱**把用户写的那"
                "几句话发出去（只连一个服务器、只发一封信、内容只有用户打的字）。"
                "为什么不用第三方：朋友的电脑大多没有邮件程序、也没有邮箱账号，"
                "而「匿名发信」在今天的互联网上并不存在。⚠️ 这条路要求 exe 里带一个"
                "发信授权码（可随时在邮箱后台重置）—— 这是**明知代价的选择**，"
                "换来的好处是「不经过任何第三方、朋友什么都不需要」。"
                "只允许出现在 feedback_send.py 里，见 SINGLE_FILE_ONLY。"),
    "urllib": "版本检查：一次 HTTPS GET 取回一个版本号字符串（不下载、不执行、不落盘）",
}

#: 单文件独占：模块名 -> 唯一允许 import 它的源文件。
#: 这条断言存在的意义：把"联网能力"钉死在一个 40 行、可逐行读完的文件里。
#: 只要 urllib 在别处冒头，审计就红，构建就停。
SINGLE_FILE_ONLY = {
    # 联网点一：更新（取版本号 / 清单 / 下载更新包，都不执行）
    # 联网点二：反馈提交（用户亲手点一次 POST；地址由 main.py 传入）
    # 加第三个之前先想清楚：这个 exe 还能不能一句话说清？
    "urllib": frozenset({"version_check.py", "feedback_send.py"}),
    # 发信：反馈那条"程序直连邮箱"的路。只准出现在这一个文件里 ——
    # 也就是说"这个 exe 会发邮件"这件事只有一个出口，改一行就能看出来。
    "smtplib": frozenset({"feedback_send.py"}),
}

#: 允许出现在**源码**里、但**不允许出现在这些文件里**的字符串。
#: 目前只有一条：反馈提交那个文件的地址必须由调用方传入 ——
#: 它自己带任何 URL 字面量，就等于"联网点可以指向别处而没人看得见"。
NO_URL_LITERAL = {
    "feedback_send.py": "反馈地址必须由 main.py 传入（见该文件头部的说明）",
}

#: 必须**自成一体的**模块：它们要被单独复制到公开仓库去（朋友下载后直接跑），
#: 所以不允许 import 任何同目录兄弟模块 —— 否则朋友拿到的是一份跑不起来的东西，
#: 而这件事在本地永远测不出来（本地什么都在）。
STANDALONE_MODULES = {
    "serial_check.py": "序列号生成器，原样发布到公开仓库；朋友下载单个文件就能用",
}

#: "会碰磁盘 / 会启动外部程序"的调用形态。只认**明确**的写盘形态，
#: 不认 ``str.replace`` 这种同名的字符串方法 —— 误报会把这份报告变成噪音，
#: 而没人看的报告等于没有报告。
WRITE_ATTRS = frozenset({
    "save", "write", "writestr", "dump", "unlink", "rmtree",
    "copyfile", "copyfileobj", "copytree",
})
OS_WRITE_FUNCS = frozenset({
    "remove", "unlink", "replace", "rename", "renames", "makedirs", "mkdir",
    "rmdir", "removedirs", "startfile", "system", "popen", "execv", "execvp",
    "fdopen", "chmod", "truncate",
})
MODULE_WRITE_PREFIX = ("subprocess", "shutil", "tempfile")

#: 每个入口脚本**允许**出现写盘/执行动作的源文件集合。
#: 这张表就是"这个 exe 会碰你机器上的什么"的完整清单，多一个都算审计失败。
WRITERS_EXPECTED = {
    "main": {
        "state_store.py",    # 写 state.json（月度过闸状态）
        "image_ops.py",      # save_image：写用户点「另存为」选定的那一张图
        "shell_ops.py",      # os.startfile 打印 / explorer /select（不写内容）
        "version_check.py",  # 写用户点「下载」拿到的更新包
        "ai_ops.py",         # 写用户点「下载 AI 模型」拿到的模型包（解包到 models/）
        "main.py",           # os.startfile 运行用户确认过的更新包
    },
    "installer": {
        "installer.py",      # 写主程序、写安装日志、创建快捷方式
    },
}

# CPython 扩展模块 / 运行库：不同 Python 版本带的文件不同，按名字规则放行。
KNOWN_BINARY_HINT = (
    "_bz2", "_ctypes", "_decimal", "_elementtree", "_hashlib", "_lzma",
    "_queue", "_socket", "_ssl", "_tkinter", "pyexpat", "select", "unicodedata",
    "vcruntime", "python3", "libcrypto", "libssl", "libffi",
    "tk86t", "tcl86t", "tk85t", "tcl85t",
    # numpy 在某些 wheel 里会自带 OpenBLAS / MKL 替代品的 DLL，
    # 文件名就长 "libopenblas_v0.3.21-gcc_8_3_0.dll" 这样，
    # 不是 KNOWN_BINARY_HINT 里那种"模块名匹配"的格式 —— 改用子串包含。
    "libopenblas",
)
# Tcl/Tk 的数据文件：PyInstaller 5 用 tk\ 目录名、6 用 _tk_data\，两种都认。
TCL_TK_HINT = ("_tcl_data", "_tk_data", "tcl8", "tk8", "tk\\", "tcl\\", "tk/")

# numpy / charset_normalizer 的 .pyd、.dll、dist-info、.libs —— 这些都是
# 上游 wheel 自带的合法运行时文件，没有代码夹带的可能。审计只关心"会不会
# 有人塞自己的代码进来"，所以它们和 KNOWN_BINARY_HINT 里的 stdlib 扩展
# 是一类：标准包的标准文件，单独归一类方便看清是不是只有"这个版本的 numpy
# 应该有的东西"。
# ⚠ 这些都是**前缀**匹配，所以 "numpy-2.5.3.dist-info" / "numpy.libs" /
# "numpy\_core" 都会被收；不属于 numpy 本体但**同名撞前缀**的极端情况
# 目前项目里没有，碰到了再说（先 raise 而不是悄悄放行）。
NUMPY_CHARSET_HINT = (
    "numpy",                    # numpy 主体（C 扩展/数据/.libs 子目录）
    "charset_normalizer",       # requests 的依赖（HTTP 库常带它）
    "_wmi",                     # Windows 系统访问（pywin32 子模块）
)


# ==========================================================================
# 报告收集
# ==========================================================================
def display_width(text: str) -> int:
    """终端显示宽度：CJK 全角字符占 2 列，ASCII 占 1 列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in str(text))


def pad(text: str, width: int) -> str:
    """按显示宽度左对齐补齐，避免中英混排时列错位。"""
    text = str(text)
    return text + " " * max(0, width - display_width(text))


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.failures: list[str] = []
        self.notes: list[str] = []

    def add(self, text: str = "") -> None:
        self.lines.append(text)

    def head(self, title: str) -> None:
        self.lines.append("")
        self.lines.append("=" * 72)
        self.lines.append(title)
        self.lines.append("=" * 72)

    def fail(self, text: str) -> None:
        self.failures.append(text)
        self.lines.append("  [失败] " + text)

    def note(self, text: str) -> None:
        self.notes.append(text)
        self.lines.append("  [注意] " + text)


# ==========================================================================
# A. 容器一致性
# ==========================================================================
def section_container(rep: Report, root: str) -> None:
    rep.head("A. 载荷容器 payload.zip  vs  dist/ 主程序")
    bundle_path = os.path.join(root, "payload.zip")
    if not os.path.isfile(bundle_path):
        rep.fail("找不到 %s" % bundle_path)
        return

    try:
        import installer  # 容器结构的唯一权威读取实现
    except ImportError as exc:
        rep.fail(
            "无法导入 installer（%s）。\n"
            "         本脚本需要 tkinter，请用 64 位工具链的 python 运行：\n"
            "         ~/.workbuddy/cache/hvphoto/venv2/Scripts/python.exe %s"
            % (exc, os.path.relpath(__file__, root))
        )
        return

    try:
        bundle = installer.Bundle(bundle_path)
    except installer.BundleError as exc:
        rep.fail("容器不可读：%s" % exc)
        return

    rep.add("  容器: %s  (%.2f MB, 构建于 %s)"
            % (os.path.basename(bundle_path), os.path.getsize(bundle_path) / 1024 / 1024,
               bundle.meta.get("created", "未知")))
    product = bundle.meta.get("product", "?")
    rep.add("  产品: %s" % product)
    # 这条断言是拿一次真实漂移换来的：容器 meta 里的版本号一度写死在另一个
    # 模块里，改版本时没人想起来它，于是容器写着 "HAAVIK Photo 1.0.0"、
    # 三个 exe 已经是 1.1.0，而当时其它所有校验**全部通过**。
    # 期望值直接取自生产它的那个模块，不在这里再抄一遍。
    import make_bundle  # noqa: PLC0415 - 与构建期同一份定义
    want_product = "%s %s" % (make_bundle.PRODUCT_NAME, make_bundle.PRODUCT_VERSION)
    if product == want_product:
        rep.add("  -> 容器内的产品标识与 APP_VERSION 一致 ✓")
    else:
        rep.fail("容器里的产品标识是 %r，但当前版本应为 %r（APP_VERSION=%s）"
                 % (product, want_product, make_bundle.PRODUCT_VERSION))
    rep.add("")

    for arch, filename in CONTAINER_ARCHES:
        exe = os.path.join(root, "dist", filename)
        if not os.path.isfile(exe):
            rep.fail("找不到 dist\\%s" % filename)
            continue
        try:
            data, size, crc = bundle.payload(arch)
        except (installer.BundleError, codec.DecodeError) as exc:
            rep.fail("%s 载荷解不开：%s" % (arch, exc))
            continue
        with open(exe, "rb") as fh:
            disk = fh.read()

        rep.add("  [%s] %s" % (arch, filename))
        rep.add("       容器内 %10s B  crc=%s  sha256=%s" %
                ("{:,}".format(size), crc, hashlib.sha256(data).hexdigest()[:16]))
        rep.add("       磁盘上 %10s B            sha256=%s" %
                ("{:,}".format(len(disk)), hashlib.sha256(disk).hexdigest()[:16]))
        if data == disk:
            rep.add("       -> 字节级一致 ✓")
        else:
            rep.fail("%s：容器内载荷与 dist/%s 不一致 ✗" % (arch, filename))

    # 资源项。当前版本按"单 EXE 安装"发布，容器里**本来就没有**资源，
    # 所以"0 项声明 / 0 项读出"是正确结果，不是失败。
    # 旧写法把空集也判成失败（`and declared`），会在每次正常构建时误报。
    declared = bundle.meta.get("resources") or {}
    got = bundle.resources()
    if len(got) == len(declared):
        if declared:
            rep.add("")
            rep.add("  资源 %d/%d 项 CRC32 全部通过 ✓（%s）"
                    % (len(got), len(declared), "、".join(sorted(got))))
        else:
            rep.add("")
            rep.add("  容器内附带资源 0 项 —— 单 EXE 安装，安装目录里只会有一个 exe ✓")
    else:
        missing = sorted(set(declared) - set(got))
        rep.fail("资源只读出 %d/%d 项，缺失：%s" % (len(got), len(declared), missing))

    # 容器里不该有 meta.json 之外的未登记条目（防"夹带"）
    try:
        import zipfile
        with zipfile.ZipFile(bundle_path) as zf:
            members = set(zf.namelist())
    except Exception as exc:  # noqa: BLE001
        rep.fail("容器无法作为 zip 打开：%s" % exc)
        return

    expected = {"meta.json"}
    for info in (bundle.meta.get("payload") or {}).values():
        expected.add(info.get("member"))
    for info in declared.values():
        expected.add(info.get("member"))
    extra = sorted(m for m in members - expected if m)
    absent = sorted(m for m in expected - members if m)
    if absent:
        rep.fail("容器索引登记了但实际不存在的条目：%s" % absent)
    if extra:
        rep.note("容器里有 %d 个未登记条目（请人工确认）：%s" % (len(extra), extra))
    elif not absent:
        rep.add("  容器条目与 meta.json 索引完全对应，无夹带 ✓")


# ==========================================================================
# B. exe 归档真实索引
# ==========================================================================
def load_reader(exe: str):
    """返回 (CArchiveReader, toc dict) 或抛 ImportError / RuntimeError。"""
    from PyInstaller.archive import readers
    try:
        reader = readers.CArchiveReader(exe)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("不是有效的 PyInstaller 归档：%s" % exc) from exc
    return reader, dict(reader.toc)


def pyz_module_list(reader, toc):
    """读出 PYZ 里的真实模块表。名字在不同 PyInstaller 版本下不一样自适配。"""
    from PyInstaller.archive import readers

    names = [n for n in toc if str(n).lower().endswith(".pyz")]
    if not names:
        raise RuntimeError("归档里找不到 PYZ 成员")
    blob = reader.extract(names[0])
    fd, tmp = tempfile.mkstemp(suffix=".pyz")
    os.close(fd)
    try:
        with open(tmp, "wb") as fh:
            fh.write(blob)
        archive = readers.ZlibArchiveReader(tmp)
        return sorted(str(m) for m in archive.toc)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def classify_entry(name: str, spec_script: str, required_data) -> str:
    if name == spec_script:
        return "入口脚本"
    if name == "payload.zip":
        return "载荷容器"
    if name == "haavik.png":
        return "主程序显示图"
    if name == "selfcheck_modules.txt":
        # --selfcheck 靠它知道该 import 什么。少了它，自检会失去意义，
        # 所以既归类、也列进 EXE_SPECS 的必需数据（漏了就 fail）。
        return "自检模块清单"
    if name in PYI_ROLES or name.startswith(PYI_HOOK_PREFIX):
        return "PyInstaller 引导"
    if str(name).lower().endswith(".pyz"):
        return "模块表归档(PYZ)"
    if name == "base_library.zip":
        return "stdlib 归档"
    if name.startswith(TCL_TK_HINT):
        return "Tcl/Tk 数据"
    if name.startswith("PIL") or name.startswith("PIL\\"):
        return "PIL 扩展"
    # numpy / charset_normalizer 等第三方包自带的 C 扩展与运行时文件
    # （含 dist-info / .libs / *.pyd / *.dll）。识别规则：
    #   · 顶层目录叫 numpy / charset_normalizer 的 → 是它的运行时
    #   · 顶层叫 numpy-2.5.3.dist-info / numpy.libs 的 → 也算 numpy 的一部分
    #     （"numpy.libs" 这种带点的目录名是 numpy 自定义格式，不属于别的）
    #   · 顶层直接一个 _wmi.pyd 这样的 C 扩展 → pywin32 的 Windows 子模块
    # **严格只看首段目录名**，防止误放行恶意文件。
    norm = name.replace("\\", "/")
    if "/" in norm:
        top = norm.split("/", 1)[0].lower()
    else:
        # 顶层文件（无路径分隔符）：剥掉 .pyd / .dll 扩展再比
        # —— _wmi.pyd 在顶层算 _wmi 模块，libopenblas*.dll 在顶层没人兜得住，
        # 留给下面 KNOWN_BINARY_HINT 那一条。
        top = norm.rsplit(".", 1)[0].lower()
    if top in NUMPY_CHARSET_HINT or any(
            top.startswith(p + "-") or top == p + ".libs"
            for p in NUMPY_CHARSET_HINT):
        return "第三方 C 扩展/运行时"
    low = name.lower()
    if low.endswith((".pyd", ".dll")):
        if any(h in low for h in KNOWN_BINARY_HINT):
            return "运行库"
        return "?"
    return "?"


def section_archive(rep: Report, root: str) -> dict:
    rep.head("B. exe 归档内容（读 PyInstaller 真实索引，不做字节扫描）")

    try:
        import PyInstaller  # noqa: F401
    except ImportError as exc:
        rep.fail("本解释器没有 PyInstaller（%s），无法读归档索引。\n"
                 "         请用 64 位工具链的 python 运行本脚本。" % exc)
        return {}
    from PyInstaller import __version__ as pyi_version
    rep.add("  PyInstaller %s" % pyi_version)

    pyz_by_exe: dict[str, list] = {}

    for filename, (script, required_data) in EXE_SPECS.items():
        exe = os.path.join(root, "dist", filename)
        rep.add("")
        rep.add("-" * 72)
        if not os.path.isfile(exe):
            rep.fail("找不到 dist\\%s" % filename)
            continue

        rep.add("  %s  (%.2f MB, %s)"
                % (filename, os.path.getsize(exe) / 1024 / 1024,
                   build_mod.pe_architecture(exe)))

        try:
            reader, toc = load_reader(exe)
        except (ImportError, RuntimeError) as exc:
            rep.fail("%s 归档读取失败：%s" % (filename, exc))
            continue

        by_type: dict[str, list] = {}
        for name, entry in toc.items():
            by_type.setdefault(entry[4], []).append(name)
        rep.add("  条目 %d 个，类型码分布：%s"
                % (len(toc), {k: len(v) for k, v in sorted(by_type.items())}))

        # ---- 断言 1：顶层脚本只允许「入口脚本 + PyInstaller 自带的钩子」 ----
        # 分类器把 PyInstaller 的 pyi_rth_* / pyiboot* 都归进"PyInstaller 引导"，
        # 所以凡是落进"入口脚本"分类的，必须**有且仅有**该 exe 的入口脚本一个。
        apps = [n for n in sorted(toc) if classify_entry(n, script, required_data) == "入口脚本"]
        if apps == [script]:
            rep.add("  入口脚本：%r ✓（无额外脚本）" % script)
        else:
            rep.fail("%s 的归档里出现了非预期的脚本：%r（期望只有 %r）"
                     % (filename, apps, script))

        # ---- 断言 2：'m' 类条目必须恰好是 PyInstaller 自带的那几个 ----
        mine_m = sorted(n for n, e in toc.items() if e[4] == "m")
        unexpected_m = [n for n in mine_m if n not in PYI_ROLES]
        if unexpected_m:
            rep.fail("%s 的引导模块里出现了非 PyInstaller 条目：%s" % (filename, unexpected_m))
        rep.add("  引导模块 %d 个：%s ✓" % (len(mine_m), "、".join(mine_m)))

        # ---- 断言 3：必需的内嵌数据在不在 ----
        for need in required_data:
            if need in toc:
                rep.add("  必需数据 %s 已嵌入 ✓" % pad(need, 16))
            else:
                rep.fail("%s 里找不到 %s —— PyInstaller 的 --add-data 漏了" % (filename, need))

        # ---- 分类清点，未分类的列出来给人看 ----
        buckets: dict[str, int] = {}
        unknown = []
        for name in toc:
            kind = classify_entry(name, script, required_data)
            buckets[kind] = buckets.get(kind, 0) + 1
            if kind == "?":
                unknown.append(name)
        rep.add("  条目分类：" + "，".join("%s×%d" % (k, v) for k, v in sorted(buckets.items())))
        if unknown:
            rep.note("%s 有 %d 个未分类条目（对照上一版是否一致）：%s"
                     % (filename, len(unknown), unknown))
        else:
            rep.add("  全部条目均可归类，无来历不明的文件 ✓")

        # ---- PYZ 真实模块表 ----
        try:
            mods = pyz_module_list(reader, toc)
            pyz_by_exe[filename] = mods
            rep.add("  PYZ 模块表：%d 个模块" % len(mods))
        except Exception as exc:  # noqa: BLE001
            rep.note("%s 的 PYZ 模块表读不出来（%s）" % (filename, exc))
            continue

        # ---- 断言 4：本项目自己的模块，一个不多一个不少 ----
        # 把"源码里 import 了什么"与"归档里真的有谁"对上：
        #   * 少了 = 有人加了模块但打包没带上（要到运行期才 ModuleNotFoundError）；
        #   * 多了 = 有本项目模块被塞进构建，但入口根本 import 不到它。
        # 两侧都由代码算出来，没有手写清单可以漂移。
        #
        # 注意入口脚本的存放位置：它是以类型码 's'（script）直接躺在 TOC 里的，
        # **不进 PYZ 模块表**。所以归档侧必须显式把入口脚本并回来，
        # 否则这条断言每次都会误报"缺了 main / installer"——自己找不到自己。
        # （这也正是它第一次运行就挂在 main 上的原因。）
        project_modules = {
            os.path.splitext(f)[0] for f in os.listdir(OPTIMIZED) if f.endswith(".py")
        }
        if script not in project_modules:
            rep.fail("%s 的入口脚本 %r 不是 code_review/optimized/ 下的源文件，"
                     "下面的模块比对没有意义" % (filename, script))
            continue
        pyz_project = sorted(m for m in mods if m in project_modules)
        in_archive = sorted(set(pyz_project) | {script})
        reachable = sorted(reachable_modules(OPTIMIZED, script) & project_modules)
        rep.add("  本项目模块：源码可达 %d 个，归档里 %d 个（PYZ %d + 入口脚本 1）"
                % (len(reachable), len(in_archive), len(pyz_project)))
        rep.add("      源码可达：%s" % "、".join(reachable))
        rep.add("      归档内含：%s" % "、".join(in_archive))
        if in_archive == reachable:
            rep.add("      -> 完全一致 ✓（没有漏打、也没有夹带）")
        else:
            rep.fail("%s 的项目模块集合不一致：缺失 %s，多出 %s"
                     % (filename, sorted(set(reachable) - set(in_archive)),
                        sorted(set(in_archive) - set(reachable))))

    return pyz_by_exe


# ==========================================================================
# C. 源码导入审计（AST）
# ==========================================================================
def collect_imports(src_dir: str, entry: str) -> dict:
    """从入口脚本出发，跟随同目录兄弟模块，收集顶层 import。

    Returns:
        {顶层模块名: ["文件:行号", ...]}
    """
    found: dict[str, list] = {}
    for mod in sorted(reachable_modules(src_dir, entry)):
        path = os.path.join(src_dir, mod + ".py")
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    found.setdefault(top, []).append("%s:%d" % (mod + ".py", node.lineno))
            elif isinstance(node, ast.ImportFrom):
                if node.level:      # 相对导入：包内
                    continue
                if node.module:
                    top = node.module.split(".")[0]
                    found.setdefault(top, []).append("%s:%d" % (mod + ".py", node.lineno))
            elif isinstance(node, ast.Call):
                # importlib.import_module("socket") 这种动态导入如果字面量写死也要抓
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                if name in ("import_module", "__import__") and node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        top = arg.value.split(".")[0]
                        found.setdefault(top, []).append(
                            "%s:%d (动态导入字面量)" % (mod + ".py", node.lineno))
    return found


def all_src_origins(per_exe: dict, name: str) -> list:
    """把某个模块在各入口的导入出处汇总成 ["[main] main.py:37", ...]。"""
    out = []
    for script in sorted(per_exe):
        for origin in per_exe[script].get(name, []):
            out.append("[%s] %s" % (script, origin))
    return out


def _is_write_mode(node) -> bool:
    """判断 ``open(...)`` 的第二个参数（或 mode= 关键字）是不是写模式。"""
    arg = None
    if len(node.args) >= 2:
        arg = node.args[1]
    for kw in node.keywords:
        if kw.arg == "mode":
            arg = kw.value
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return any(ch in arg.value for ch in "wax+")
    return False


def _call_signature(node: ast.Call):
    """把一次调用归一成 ``(形态, 名字)``；认不出来就返回 None。

    只报告"确定会写盘或确定会起进程"的形态。刻意不去猜变量类型 ——
    猜错的代价是报告里混进一堆 ``str.replace`` 之类的假命中，
    然后所有人都学会无视这份报告。
    """
    fn = node.func
    if isinstance(fn, ast.Name):
        if fn.id == "open":
            return ("open", "open") if _is_write_mode(node) else None
        return None

    if not isinstance(fn, ast.Attribute):
        return None
    name = fn.attr
    base = fn.value

    # 形如 os.remove(...) / os.path... 这种两级以上的属性
    chain = []
    cursor = base
    while isinstance(cursor, ast.Attribute):
        chain.append(cursor.attr)
        cursor = cursor.value
    if isinstance(cursor, ast.Name):
        chain.append(cursor.id)
    chain.reverse()               # ["os"] / ["os", "path"]
    head = chain[0] if chain else ""

    if len(chain) >= 1:
        for prefix in MODULE_WRITE_PREFIX:
            if chain[0] == prefix:
                return (prefix, ".".join(chain + [name]))
        if chain[0] == "os" and len(chain) == 1 and name in OS_WRITE_FUNCS:
            return ("os", "os." + name)
    if name in WRITE_ATTRS:
        return ("method", name)
    return None


def collect_side_effects(src_dir: str, entry: str) -> dict:
    """收集某个入口脚本（含其递归可达的兄弟模块）里的写盘 / 起进程动作。

    Returns:
        {文件名: ["行号 形态 调用", ...]}
    """
    found: dict[str, list] = {}
    siblings = {
        os.path.splitext(f)[0]
        for f in os.listdir(src_dir)
        if f.endswith(".py")
    }
    for mod in sorted(reachable_modules(src_dir, entry)):
        path = os.path.join(src_dir, mod + ".py")
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            sig = _call_signature(node)
            if sig is None:
                continue
            kind, name = sig
            found.setdefault(mod + ".py", []).append(
                ":%-4d %-10s %s" % (node.lineno, kind, name)
            )
    return found


def reachable_modules(src_dir: str, entry: str) -> set:
    """从入口脚本出发、只跟随**同目录兄弟模块**的 import 闭包。"""
    seen: set = set()
    queue = [entry]
    siblings = {
        os.path.splitext(f)[0]
        for f in os.listdir(src_dir)
        if f.endswith(".py")
    }
    while queue:
        mod = queue.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = os.path.join(src_dir, mod + ".py")
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in siblings:
                        queue.append(top)
            elif isinstance(node, ast.ImportFrom):
                # ``from X import a, b``：要跟的是 **X**（node.module），
                # 不是 a/b —— 只看 names 的话，"从兄弟模块借名字"这种
                # 最常见的 import 形态会被整条漏掉（ui_theme 就这么漏的）。
                tops = []
                if node.module and node.level == 0:
                    tops.append(node.module.split(".")[0])
                for alias in node.names:      # 兼容 ``from . import 兄弟``
                    tops.append(alias.name.split(".")[0])
                for top in tops:
                    if top in siblings:
                        queue.append(top)
    return seen


def read_excludes(src_dir: str) -> list:
    """从 build.py 里抠出 ``EXCLUDES`` 那个元组。

    用 AST 而不是 import：import 会执行 build.py 的模块级代码，
    审计工具不该因为"读一下常量"就启动别人的脚本。
    """
    path = os.path.join(src_dir, "build.py")
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "EXCLUDES" not in names:
            continue
        if isinstance(node.value, (ast.Tuple, ast.List)):
            return [e.value for e in node.value.elts
                    if isinstance(e, ast.Constant)
                    and isinstance(e.value, str)]
    return []


def section_sources(rep: Report, root: str, pyz_by_exe: dict) -> None:
    rep.head("C. 源码导入审计（AST 精确解析，与 PYZ 模块表三态比对）")

    src_dir = OPTIMIZED
    per_exe: dict[str, dict] = {}
    for filename, (script, _) in EXE_SPECS.items():
        if filename not in pyz_by_exe:
            continue
        if script in per_exe:
            continue
        per_exe[script] = collect_imports(src_dir, script)

    for script, imports in sorted(per_exe.items()):
        rep.add("")
        rep.add("-" * 72)
        rep.add("  入口 %s.py：直接导入 %d 个顶层模块（含同目录兄弟模块递归展开）"
                % (script, len(imports)))
        rep.add("    " + "、".join(sorted(imports)))

    rep.add("")
    rep.add("  可疑模块三态比对：")
    rep.add("    源码 = 本项目代码直接 import（★ 表示不在双用途白名单里，必须人工确认）")
    rep.add("    依赖 = 只在 PYZ 里，源码没 import —— 由 PIL / tkinter / stdlib 依赖图引入")
    rep.add("")
    rep.add("    %s %s %s" % (pad("模块", 14), pad("出现在哪些 exe 的 PYZ 里", 40), "判定"))
    rep.add("    " + "-" * 74)

    all_src = set()
    for imports in per_exe.values():
        all_src |= set(imports)

    redline_hits: list[str] = []
    for name in sorted(SUSPICIOUS):
        where = [fn for fn, mods in pyz_by_exe.items()
                 if any(m == name or m.startswith(name + ".") for m in mods)]
        if not where:
            verdict = "无（归档里就没有）"
        elif name not in all_src:
            verdict = "依赖（仅由依赖图引入）"
        elif name in ALLOWED_IN_SOURCE:
            verdict = "源码（双用途，用途见下）"
        else:
            verdict = "源码 ★需人工确认"
            redline_hits.append(name)
        rep.add("    %s %s %s"
                % (pad(name, 14), pad("、".join(where) or "-", 40), verdict))

    # ★★★ 这一条是"能启动"和"一启动就崩"的分界线（v1.6.0 真踩过）：
    #   **源码 import 的模块，不许出现在 build.py 的 EXCLUDES 里。**
    #   排除清单是为了减体积写的，而"后来新加的代码又开始 import 它"这件事
    #   没有任何东西提醒 —— 结果打出来的 exe 一运行就 ModuleNotFoundError，
    #   而本地跑源码永远测不出来（本地什么模块都有）。
    #
    #   2026-09-19 的真实事故：EXCLUDES 里有 smtplib，新写的反馈发信代码
    #   import 了它 → 发出去的安装包装完就打不开（用户截图给我看才知道）。
    #   审计当时其实**打印**过 "smtplib 无（归档里就没有）"，但只是打印、
    #   没有判失败 —— 所以这里必须 fail。
    #
    #   为什么不直接拿"PYZ 模块表"比对：os / io / math / struct 这些是内置或
    #   冻结模块，压根不在 PYZ 里，那样比会满屏误报（第一版就是）。
    #   真正要防的是"我们自己排除了它"，所以直接对着排除清单比。
    excluded = read_excludes(src_dir)
    rep.add("")
    if not excluded:
        rep.fail("读不出 build.py 的 EXCLUDES（这条断言就形同虚设）—— "
                 "改过 build.py 的写法就要同步改这里的解析")
    else:
        # 故意排除、但源码确实会 import 的模块：它们不进主程序，而是**随 AI 组件包
        # 分发**（解包后落在 models/<模块名>/，运行时由 ai_ops 加进 sys.path 再 import）。
        # 因此"主程序里被排除"是设计意图，不算 bug——从冲突比对里剔掉，但显式记一笔。
        INTENTIONALLY_EXCLUDED = frozenset({"onnxruntime"})
        clash = sorted(n for n in excluded
                       if n.split(".")[0] in all_src
                       and n.split(".")[0] not in INTENTIONALLY_EXCLUDED)
        intentional = sorted(n for n in excluded
                             if n.split(".")[0] in all_src
                             and n.split(".")[0] in INTENTIONALLY_EXCLUDED)
        if intentional:
            rep.add("    故意排除、随 AI 组件包分发的（不计入冲突）：%s"
                    % "、".join(intentional))
        if clash:
            rep.fail("build.py 的 EXCLUDES 排除了 %s，但源码 import 了它 —— "
                     "打出来的 exe 一启动就会 ModuleNotFoundError。"
                     "（把它从 EXCLUDES 里去掉）" % clash)
        else:
            rep.add("    源码 import 的模块与 build.py 的 EXCLUDES 无交集 ✓"
                    "（比对了 %d 个排除项 / %d 个 import）"
                    % (len(excluded), len(all_src)))
            rep.add("        —— 这一条是 v1.6.0 那次「装完打不开」事故补上的")

    # 双用途模块：把用途和来源行号摊开给人看
    dual_used = [n for n in sorted(ALLOWED_IN_SOURCE) if n in all_src]
    if dual_used:
        rep.add("")
        rep.add("  双用途模块（允许源码直接使用，逐条列出用途与来源，供人工复核）：")
        for name in dual_used:
            rep.add("    %s %s" % (pad(name, 14), ALLOWED_IN_SOURCE[name]))
            for origin in all_src_origins(per_exe, name):
                rep.add("        %s" % origin)

    # 红线模块：进了归档但源码没 import 的，逐个点名，证明是依赖图带进来的
    rep.add("")
    in_archive = [n for n in sorted(REDLINE)
                  if any(any(m == n or m.startswith(n + ".") for m in mods)
                         for mods in pyz_by_exe.values())]
    dirty = sorted(set(in_archive) & all_src)
    if dirty:
        rep.fail("红线模块被本项目源码直接 import：%s" % dirty)
    else:
        rep.add("  红线模块（网络 / 持久化 / 输入钩子 / 进程与屏幕信息，共 %d 个）中，"
                "进入归档的有 %d 个：" % (len(REDLINE), len(in_archive)))
        rep.add("    %s" % ("、".join(in_archive) if in_archive else "（一个都没有）"))
        rep.add("  这 %d 个**全部**是依赖图引入了符号但本项目代码从未调用，"
                "源码侧一个都没 import ✓" % len(in_archive))
        rep.add("  —— 这是「无注册表持久化、无全局输入钩子、无后台上传」的可重复证据。")

    # 单文件独占断言：联网能力必须被钉死在一个文件里
    rep.add("")
    rep.add("  单文件独占断言（联网能力不得散落）：")
    for mod, only in sorted(SINGLE_FILE_ONLY.items()):
        allowed = only if isinstance(only, (set, frozenset)) else {only}
        origins = all_src_origins(per_exe, mod)
        if not origins:
            rep.add("    %s 未被任何源码 import ✓" % pad(mod, 14))
            continue
        offenders = [o for o in origins
                     if not any((f + ":") in o for f in allowed)]
        if offenders:
            rep.fail("%s 只允许出现在 %s 里，实际还出现在：%s"
                     % (mod, "、".join(sorted(allowed)), offenders))
        else:
            rep.add("    %s 只出现在 %s（共 %d 处）✓ —— 全项目就这几个联网点"
                    % (pad(mod, 14), "、".join(sorted(allowed)), len(origins)))
            for origin in origins:
                rep.add("        %s" % origin)

    section_standalone(rep, src_dir, per_exe)
    section_no_url_literal(rep, src_dir)
    section_writers(rep, src_dir, per_exe)


# ==========================================================================
# C2. 自成一体的模块（要被单独发布出去的那些）
# ==========================================================================
def url_literals(source: str) -> list:
    """源码里出现的"真的指向某个地方"的地址字面量。

    规则刻意写得精确，因为误报会把报告变成噪音，而没人看的报告等于没有报告：

      * ``"https://"`` 这种**纯协议前缀不算** —— 那是在比较协议名，
        不是在指某个地方（``feedback_send._address_ok`` 就要比较它）；
      * **回环地址不算**（127.0.0.1 / localhost / [::1]）：它只可能指向本机，
        而测试用的假接收端就是靠它 —— 流量不出本机，谈不上"往外发"；
      * 其余 ``http(s)://`` 后面还有东西的字符串**一律算**：
        那样"往哪儿发"就必须在 ``main.py`` 里看得见。
    """
    loopback = ("http://127.0.0.1", "http://localhost", "http://[::1]",
                "https://127.0.0.1", "https://localhost", "https://[::1]")
    found = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        text = node.value.strip()
        if not text.startswith(("http://", "https://")):
            continue
        if len(text) <= len("https://"):
            continue
        if text.startswith(loopback):
            continue
        found.append((node.lineno, text[:60]))
    return found


def section_no_url_literal(rep: Report, src_dir: str) -> None:
    """联网点的地址不许自己带 —— 必须由调用方传进去。

    为什么这条重要：``SINGLE_FILE_ONLY`` 已经保证"只有两个文件能联网"，
    但如果那两个文件里自己写着一个地址，那么"它到底往哪儿发"就没人看得见了
    （改一行字符串的事，报告也照过）。把地址钉在 ``main.py`` 里，
    整个项目"往哪儿发东西"就只剩少数几个常量，grep 一下就能看全。
    """
    rep.add("")
    rep.add("  联网点不许自带地址（地址由 main.py 传入；回环地址除外）：")
    for filename, why in sorted(NO_URL_LITERAL.items()):
        path = os.path.join(src_dir, filename)
        if not os.path.isfile(path):
            rep.fail("找不到 %s（%s）" % (filename, why))
            continue
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
        hits = url_literals(source)
        if hits:
            rep.fail("%s 里出现了地址字面量（%s）：%s" % (filename, why, hits))
        else:
            rep.add("    %s 里没有任何地址字面量 ✓（%s）"
                    % (pad(filename, 20), why))


def section_standalone(rep: Report, src_dir: str, per_exe: dict) -> None:
    rep.add("")
    rep.add("  自成一体的模块断言（要被单独复制到公开仓库的东西）：")
    rep.add("    理由：朋友下载的是**单个文件**，本地永远测不出'少了一个兄弟模块'。")
    siblings = {os.path.splitext(f)[0] for f in os.listdir(src_dir) if f.endswith(".py")}

    for filename, why in sorted(STANDALONE_MODULES.items()):
        path = os.path.join(src_dir, filename)
        if not os.path.isfile(path):
            rep.fail("找不到 %s（%s）" % (filename, why))
            continue
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source, filename=path)

        imported_siblings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in siblings and top + ".py" != filename:
                        imported_siblings.add(top)
        third_party = sorted(
            m for m in {a.name.split(".")[0] for n in ast.walk(tree)
                        if isinstance(n, ast.Import) for a in n.names}
            | {n.module.split(".")[0] for n in ast.walk(tree)
               if isinstance(n, ast.ImportFrom) and n.module}
        )
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        rep.add("    %s" % filename)
        rep.add("        用途     : %s" % why)
        rep.add("        依赖     : %s（全部为 Python 标准库）"
                % ("、".join(third_party) or "无"))
        rep.add("        行数     : %d 行 / %d 字节"
                % (source.count("\n") + 1, len(source.encode("utf-8"))))
        rep.add("        sha256   : %s" % digest)
        rep.add("        ——发布到公开仓库后，可用上面这行哈希核对是不是同一份")
        if imported_siblings:
            rep.fail("%s import 了同目录兄弟模块 %s —— 单独发出去会跑不起来"
                     % (filename, sorted(imported_siblings)))
        else:
            rep.add("        未 import 任何同目录兄弟模块 ✓")


# ==========================================================================
# C3. 会碰磁盘 / 会起进程的源文件
# ==========================================================================
def section_writers(rep: Report, src_dir: str, per_exe: dict) -> None:
    rep.add("")
    rep.add("  写盘 / 执行外部程序 审计（AST，只看确定的形态）：")
    rep.add("    这一节回答：**这个 exe 会动我机器上的什么？**")
    rep.add("    只认 open(写模式) / os.<写函数> / subprocess / shutil / tempfile /")
    rep.add("    *.save|write|writestr|dump —— 不认 str.replace 之类的同名方法，")
    rep.add("    免得把报告变成噪音，而没人看的报告等于没有报告。")

    ok = True
    for script in sorted(per_exe):
        hits = collect_side_effects(src_dir, script)
        allowed = WRITERS_EXPECTED.get(script, set())
        actual = set(hits)
        rep.add("")
        rep.add("    [%s] 实际会碰磁盘/起进程的源文件：%s"
                % (script, "、".join(sorted(actual)) or "（一个都没有）"))
        for mod in sorted(hits):
            tag = "允许" if mod in allowed else "★越界"
            rep.add("      %s %s" % (pad(tag, 8), mod))
            for line in hits[mod]:
                rep.add("          %s" % line)
        extra = sorted(actual - allowed)
        missing = sorted(allowed - actual)
        if extra:
            ok = False
            rep.fail("%s：出现了白名单之外的写盘/起进程源文件 %s" % (script, extra))
        if missing:
            rep.note("%s：白名单里的 %s 这次没命中（改了实现？请同步这张表）"
                     % (script, missing))
    if ok:
        rep.add("")
        rep.add("    上述集合与 WRITERS_EXPECTED 完全一致 ✓")
        rep.add("    换句话说，main.exe 只会为三件事写盘：state.json（月度状态）、")
        rep.add("    你在「另存为」里亲自选的那张图、你点过「下载」的更新包。")
        rep.add("    installer.exe 只会写主程序本体（默认就落在桌面）与它自己的诊断日志。")


# ==========================================================================
# D. PE 版本信息 + 签名主体
# ==========================================================================
VERSION_KEYS = (
    "CompanyName", "FileDescription", "LegalCopyright",
    "ProductName", "OriginalFilename", "FileVersion", "ProductVersion",
)


def _utf16z(text: str) -> bytes:
    return text.encode("utf-16-le") + b"\x00\x00"


def pe_version_strings(path: str) -> dict:
    """纯 Python 读 VS_VERSIONINFO 的 StringFileInfo 字段。

    VERSIONINFO 里每个 String 结构是：
        WORD len / WORD value_len / WORD type / WCHAR key[] / padding /
        WCHAR value[] / padding
    所以定位到 key 的 UTF-16LE 字节后，按 4 字节对齐取到值即可。
    这样就不用调 PowerShell，绕开中文系统上的 GBK 编码坑。
    """
    with open(path, "rb") as fh:
        blob = fh.read()

    out = {}
    for key in VERSION_KEYS:
        needle = _utf16z(key)
        idx = blob.find(needle)
        if idx < 0:
            out[key] = None
            continue
        pos = (idx + len(needle) + 3) & ~3          # 对齐到 4 字节边界
        end = pos
        while end + 1 < len(blob) and blob[end:end + 2] != b"\x00\x00":
            end += 2
        out[key] = blob[pos:end].decode("utf-16-le", "replace")
    return out


def section_version_and_sig(rep: Report, root: str) -> None:
    rep.head("D. PE 版本信息  +  Authenticode 签名主体")

    paths = [os.path.join(root, "dist", f) for f in EXE_SPECS]
    paths = [p for p in paths if os.path.isfile(p)]

    rep.add("  PE 版本信息（VS_VERSIONINFO 资源，纯文本元数据，不是数字签名）")
    for path in paths:
        rep.add("")
        rep.add("  %s" % os.path.basename(path))
        fields = pe_version_strings(path)
        for key in VERSION_KEYS:
            value = fields.get(key)
            flag = "  " if value else "! "
            rep.add("    %s%s %s" % (flag, pad(key, 20),
                                    value if value is not None else "(未找到)"))

        # 署名核对：公司 / 产品 / 版权都应指向 哈夫克
        company = fields.get("CompanyName") or ""
        product = fields.get("ProductName") or ""
        if sign_build.CERT_CN in company and sign_build.CERT_CN in product:
            rep.add("    -> 署名核对 ✓ 公司/产品均含 %r" % sign_build.CERT_CN)
        else:
            rep.fail("%s 的署名不含 %r（公司=%r 产品=%r）"
                     % (os.path.basename(path), sign_build.CERT_CN, company, product))

    if not paths:
        rep.fail("dist/ 下找不到任何待审计的 exe")
        return

    rep.add("")
    rep.add("  Authenticode 签名状态（自签名，未导入证书的机器上必然不是 Valid）")
    try:
        rows = sign_build.signature_status(paths)
    except Exception as exc:  # noqa: BLE001
        rep.note("签名状态查询失败：%s" % exc)
        return

    for row in rows:
        name = os.path.basename(row["path"])
        if not row.get("signer"):
            rep.fail("%s 没有签名（Get-AuthenticodeSignature 读不到 SignerCertificate）" % name)
            rep.add("    %s  status=%s" % (name, row.get("status")))
            continue
        subject = row["signer"]
        rep.add("    %s" % name)
        rep.add("      status : %s" % row["status"])
        rep.add("      signer : %s" % subject)
        if sign_build.CERT_CN in subject:
            rep.add("      -> 主体含 %r ✓" % sign_build.CERT_CN)
        else:
            rep.fail("%s 的签名主体里没有 %r" % (name, sign_build.CERT_CN))


# ==========================================================================
# E. 关机构造串（弱信号，仅参考）
# ==========================================================================
def section_shutdown(rep: Report, root: str) -> None:
    rep.head("E. 关机构造串（弱信号，仅作参考，不参与判定）")
    rep.add("  下面 b'shutdown' 计数为 0 是**正常的**，不是异常：")
    rep.add("  main.py 里 SHUTDOWN_EXE = \"shutdown\" 确实是明文常量（已弃用 chr() 拼接")
    rep.add("  那种反静态写法），但源文件被压进了 PYZ-00.pyz，而 PYZ 是 zlib 压缩的，")
    rep.add("  裸字节扫描根本看不到。")
    rep.add("  —— 这恰好说明为什么本节不参与判定：换个拼法就能骗过它，")
    rep.add("     正确的做法是去读归档的真实索引（见 B 节）。")
    rep.add("")
    rep.add("  补充（1.2.0 起）：默认流程**永远不会**走到 shutdown —— 答错改走")
    rep.add("  「软件已锁定」页；只有带 --real-shutdown 启动才会走老的关机倒计时。")
    rep.add("  这条能力仍然留在 manifest 的 '命令行参数' 一节里，没有偷偷去掉。")
    for filename in EXE_SPECS:
        path = os.path.join(root, "dist", filename)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            raw = fh.read()
        rep.add("    %s b'shutdown'×%-3d  b'/s'×%-5d  b'/a'×%-4d"
                % (pad(filename, 18), raw.count(b"shutdown"),
                   raw.count(b"/s"), raw.count(b"/a")))


# ==========================================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="HAAVIK Photo 产物审计")
    parser.add_argument("root", nargs="?", default=DEFAULT_ROOT,
                        help="项目根目录（默认：本脚本所在目录的上级）")
    parser.add_argument("--out", default=os.path.join(HERE, "VERIFY_REPORT.md"),
                        help="报告输出路径")
    parser.add_argument(
        "--show-paths",
        action="store_true",
        help="在报告抬头打印项目根目录的完整绝对路径（默认省略，因为报告要进仓库）",
    )
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    rep = Report()
    rep.add("HAAVIK Photo 产物审计报告")
    # 这份报告是**要进仓库、可能被公开**的，所以默认不写本机绝对路径 ——
    # 它会把 Windows 用户名和磁盘布局一起带出去（例：D:\<用户名>\Desktop\hvphoto）。
    # 目录名本身对审计没有信息量，本地排查时加 --show-paths 就能看全。
    if args.show_paths:
        rep.add("项目根目录: %s" % root)
    else:
        rep.add("项目根目录: %s（本机绝对路径已省略；加 --show-paths 显示）"
                % os.path.basename(root))
    rep.add("解释器    : %s" % sys.version.replace("\n", " "))

    section_container(rep, root)
    pyz_by_exe = section_archive(rep, root)
    section_sources(rep, root, pyz_by_exe)
    section_version_and_sig(rep, root)
    section_shutdown(rep, root)

    rep.head("总结")
    if rep.failures:
        rep.add("  [不通过] %d 项：" % len(rep.failures))
        for item in rep.failures:
            rep.add("      - %s" % item.replace("\n", " "))
    else:
        rep.add("  [通过] 全部断言成立。")
    for item in rep.notes:
        rep.add("  [需人工确认] %s" % item.replace("\n", " "))

    report = "\n".join(rep.lines) + "\n"
    try:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(report)
        rep.lines.append("")
        rep.lines.append("报告已写入: %s" % args.out)
        report = "\n".join(rep.lines) + "\n"
    except OSError as exc:
        print("[WARN] 报告写不出去：%s" % exc)

    print(report)
    return 1 if rep.failures else 0


if __name__ == "__main__":
    sys.exit(main())
