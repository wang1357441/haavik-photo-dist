# -*- coding: utf-8 -*-
"""一键构建：图标 -> 双架构主程序 -> 载荷容器 -> 32 位安装器。

用法
==================================================================
    python build.py                # 完整构建
    python build.py --skip-main    # 跳过主程序，只用现成的 dist/*.exe（改安装器时快）
    python build.py --only-setup   # 只重打安装器
    python build.py --verify       # 只校验产物

关键设计：为什么安装器用 32 位 Python 编译
==================================================================
Windows 的 WoW64 只向下兼容：

    32 位 exe  ->  32 位 Windows   能跑（原生）
                  ->  64 位 Windows   能跑（WoW64）
    64 位 exe  ->  32 位 Windows   加载失败（连入口都进不去）

所以"一个安装包在 32/64 位都能用"的唯一解，是把**安装器**编译成 32 位，
由它在运行时判断目标系统架构，再释放对应的主程序。
主程序本身仍是双架构（x64 与 x86 各一份，都放进载荷容器）。

工具链（本机实测可用）
==================================================================
    32 位: ~/.workbuddy/cache/py38/  Python 3.8.10 + PyInstaller 5.13.2 + tkinter + Pillow
    64 位: ~/.workbuddy/cache/hvphoto/venv2/  Python 3.12.14 + PyInstaller 6.22.2 + tkinter + Pillow

刻意**不生成 .spec 文件**：PyInstaller 5 与 6 的 spec 语法不兼容
（6 移除了 ``cipher`` 与 ``a.zipfiles``）。原仓库里 x64 的 spec 是 6 的格式、
x86 的 spec 是 5 的格式，两套架构其实由不同大版本的工具链构建 ——
这正是构建不可复现的根源。全部改用命令行参数，跨版本一致。

资源打包（最易漏，已加守卫）
==================================================================
主程序运行时用 ``resource_path("haavik.png")`` 到 ``sys._MEIPASS`` 下找图片，
所以这张图必须用 ``--add-data`` 打进 exe。原 ``.spec`` 里写作
``datas=[('haavik.png', '.')]`` —— 改成命令行参数后**漏过一次**：
exe 里没有这张图，程序不报错，只是默默显示「图片加载失败」，
而当时其它所有校验（PE 位数、签名、载荷哈希、安装链路）**全部通过**。

现在 ``build_main()`` 与 ``build_setup()`` 逐项调用 ``assert_embedded()``
断言资源确实在 exe 里，缺了就构建失败。

签名（默认开启，--no-sign 关闭）
==================================================================
构建过程中用 sign_build.py 给三个 exe 加自签名（署名"哈夫克"）。
**顺序是有约束的**，不能随便挪：

    主程序 → [签名主程序] → 打包载荷 → 构建安装器 → [签名安装器]

* 主程序必须在 ``build_payload()`` **之前**签好：载荷容器里的 crc32 是
  打包那一刻算出来的，先签后打包，才保得住"容器内字节 == dist/ 里字节"
  这条不变量（verify_payload.py 依赖它）。
* 安装器必须在 PyInstaller 编译**之后**签：签名会改写文件尾部，
  先签再编译等于白签。

另：这里的"签名"是自签名，**不提供任何信任背书**，也不改善杀软判断。
详见 sign_build.py 的模块说明，别把它当成"能过杀软"的手段。
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))  # code_review/optimized -> 项目根

if HERE not in sys.path:
    sys.path.insert(0, HERE)

import sign_build  # noqa: E402  （同目录，故需先补 sys.path）
import version_check  # noqa: E402  版本号唯一来源

MAIN_X86_EXE = "MyAlbum_x86.exe"
MAIN_X64_EXE = "MyAlbum.exe"
SETUP_EXE = "Setup.exe"

PYTHON_X86 = os.path.join(os.path.expanduser("~"), ".workbuddy", "cache", "py38", "python.exe")
PYTHON_X64 = os.path.join(
    os.path.expanduser("~"), ".workbuddy", "cache", "hvphoto", "venv2", "Scripts", "python.exe"
)

APP_ICON = os.path.join(HERE, "app.ico")
APP_IMAGE = os.path.join(HERE, "haavik.png")   # 主程序显示的那张图（必须进 exe）
VERSION_FILE = os.path.join(HERE, "version.txt")
# 安装器单独一份版本信息：共用一份会让 Setup.exe 的属性里写着
# OriginalFilename = MyAlbum.exe，看着就不像安装程序。
SETUP_VERSION_FILE = os.path.join(HERE, "version_setup.txt")
MAIN_PY = os.path.join(HERE, "main.py")
INSTALLER_PY = os.path.join(HERE, "installer.py")

DIST = os.path.join(ROOT, "dist")
BUILD_TMP = os.path.join(ROOT, "build_work")
BUNDLE = os.path.join(ROOT, "payload.zip")

# 裁掉用不到的 stdlib：缩短解包时间、减小体积。既影响启动速度（"不卡顿"），
# 也影响 exe 大小。tkinter / 子进程 / zipfile / json 都是必需的，不能裁。
#
# ⚠️⚠️ 这个清单是个陷阱：**它是按"现在没人用"写的，而以后新加的代码可能开始用**。
#   2026-09-19 就是这么炸的：smtplib 一直躺在这个清单里，直到反馈发信那条路
#   开始 import 它 —— 打出来的 exe 一双击就 ModuleNotFoundError，
#   而本地跑源码永远测不出来（本地什么模块都有）。
#   同一天又踩了 numpy：ai_ops.preprocess_image 在跑 AI 修图时要用 numpy 做
#   CHW float32 转换，被 --exclude-module numpy 一挡，构建期不报错、运行期
#   ModuleNotFoundError。所以 numpy 已经从下面这个清单里拿掉了 —— 它现在属于
#   "主程序真要用的第三方库"，由 PyInstaller 自动收集，不要再加回来。
#   两层防护已经补上：
#     ① verify_payload.py 会断言"源码 import 的模块 ∩ EXCLUDES = 空"（发版前必跑）；
#     ② main.py 有 --selfcheck，release.py 会在发布前**真的把打好的 exe 启动一次**。
#   往这里加东西之前，先想清楚"将来会不会有人用它"；加完必须跑一次审计。
EXCLUDES = (
    "unittest", "pydoc", "doctest", "sqlite3", "xmlrpc", "ftplib", "imaplib",
    "poplib", "telnetlib", "nntplib", "socketserver", "asyncio",
    "concurrent.futures", "multiprocessing", "lib2to3", "distutils", "setuptools",
    "pip", "pdb", "difflib", "curses", "pytz", "scipy", "pandas",
    "matplotlib", "PyQt5", "PySide2", "wx",
    # onnxruntime 刻意排除：它体积大（带原生 DLL，~150MB），**不进主程序**，
    # 而是随 AI 组件包分发（解包后落在 models/onnxruntime/，由
    # ai_ops._ensure_onnxruntime_importable 在 import 前把该目录加进 sys.path /
    # Windows DLL 搜索目录）。这样主程序保持精简、AI 大依赖只在用户真下载 AI
    # 组件时才落地。verify_payload 已为它开白名单（INTENTIONALLY_EXCLUDED）。
    "onnxruntime",
)

# 与 EXCLUDES 反着来的清单：源码里是**懒导入**（import 写在函数里、不是模块
# 顶层），PyInstaller 有时漏捡，导致构建期不报错、运行期才 ModuleNotFoundError。
# 这里显式点名，强制打进 exe。目前只有 numpy —— ai_ops.preprocess_image 用它
# 做 CHW float32 转换；它必须是主程序自带的，不能靠 AI 组件包装。
HIDDENIMPORTS = (
    "numpy",
)


def log(msg: str) -> None:
    print(msg, flush=True)


def run(cmd, desc: str, cwd: str = None) -> None:
    log("    $ %s" % " ".join('"%s"' % c if " " in c else c for c in cmd))
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, errors="replace")
    if proc.returncode != 0:
        log("    [FAIL] %s (rc=%d)" % (desc, proc.returncode))
        tail = (proc.stdout or "")[-3000:]
        err = (proc.stderr or "")[-3000:]
        log(tail)
        log(err)
        raise SystemExit(1)


def python_bits(exe: str) -> int:
    out = subprocess.run(
        [exe, "-c", "import struct;print(struct.calcsize('P')*8)"],
        capture_output=True, text=True, check=True,
    )
    return int(out.stdout.strip())


def pe_architecture(path: str) -> str:
    """读 PE 头的 machine 字段，确认产物位数（不靠猜）。"""
    machine = {
        0x014C: "x86 (32位)",
        0x8664: "x64 (64位)",
        0xAA64: "ARM64",
    }
    with open(path, "rb") as fh:
        data = fh.read(0x400)
    if data[:2] != b"MZ":
        return "非 PE 文件"
    lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if lfanew + 6 > len(data):
        return "PE 头越界"
    sig = data[lfanew:lfanew + 4]
    if sig != b"PE\x00\x00":
        return "PE 签名缺失"
    m = struct.unpack_from("<H", data, lfanew + 4)[0]
    return machine.get(m, "未知 (0x%04X)" % m)


def parse_version_file(path: str) -> tuple[tuple[int, int, int], str]:
    """从 PyInstaller 的 version 文件里读出两处版本号，返回 ``((1,2,0), "1.2.0.0")``。

    这个文件是 ``VSVersionInfo(...)`` 的 Python 字面量（不是 ini），所以用正则取：

      * ``filevers=(1, 2, 0, 0)``                      —— FixedFileInfo 数值版本
      * ``StringStruct(u'FileVersion', u'1.2.0.0')``   —— 详情页「文件版本」字符串

    文件里有**四处**版本号：``filevers`` / ``prodvers`` / ``FileVersion`` /
    ``ProductVersion``。它们必须彼此相等 —— 只改其中一两个就会出现
    "数值版本 1.1、显示版本 1.0"这种属性页自相矛盾。这里直接断言，
    因为这正是本项目踩过的坑（改版本时只落了 ProductVersion 一处）。
    主程序与安装器两 file 之间的一致性由 ``check_toolchains`` 对 APP_VERSION 校验。
    """
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()

    def grab(pattern: str, what: str) -> tuple:
        m = re.search(pattern, text)
        if not m:
            raise SystemExit("[ERROR] %s 里找不到 %s" % (path, what))
        return tuple(m.groups())

    fv = grab(r"filevers\s*=\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)",
              "filevers=(a, b, c, d)")
    pv = grab(r"prodvers\s*=\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)",
              "prodvers=(a, b, c, d)")
    shown = grab(r"StringStruct\(\s*u?['\"]FileVersion['\"]\s*,\s*u?['\"]([^'\"]+)['\"]\s*\)",
                 "StringStruct(u'FileVersion', u'...')")[0]
    shown_prod = grab(r"StringStruct\(\s*u?['\"]ProductVersion['\"]\s*,\s*u?['\"]([^'\"]+)['\"]\s*\)",
                      "StringStruct(u'ProductVersion', u'...')")[0]

    if fv != pv:
        raise SystemExit("[ERROR] %s 的 filevers=%s 与 prodvers=%s 不一致"
                         % (path, fv, pv))
    if shown != shown_prod:
        raise SystemExit("[ERROR] %s 的 FileVersion=%r 与 ProductVersion=%r 不一致"
                         % (path, shown, shown_prod))
    numeric = tuple(int(x) for x in fv[:3])
    if numeric != tuple(int(x) for x in shown.split(".")[:3]):
        raise SystemExit("[ERROR] %s 的数值版本 %s 与显示版本 %r 不一致"
                         % (path, ".".join(map(str, numeric)), shown))
    return numeric, shown


def ast_module_string(path: str, name: str) -> str:
    """取某个源文件里**模块级**字符串常量的值（不 import，避开 tkinter 依赖）。"""
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    for node in tree.body:          # 只看模块级赋值，不碰函数内的局部变量
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                value = node.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    return value.value
    raise SystemExit("[ERROR] %s 里找不到模块级的 %s 字符串常量" % (path, name))


def assert_shared_names() -> None:
    """断言"同一个名字"在四处写法一致。

    ``"HAAVIK Photo"`` 这一串同时出现在：
      * ``main.APP_TITLE``        —— 主程序窗口标题
      * ``installer.APP_NAME``    —— 安装向导、安装日志目录、
        以及卸载时识别 1.1.0 旧安装目录
      * ``state_store.APP_DIRNAME`` —— 状态目录名（桌面不可用时的兜底位置）
      * ``image_tool.APP_TITLE``  —— 图片工具页的标题

    它们**必须**一样：不一样的话会出现"标题写 A、日志写到 B、
    状态写到 C 目录"，而每一处单独看都很正常 —— 这种漂移肉眼几乎发现不了，
    只有把断言写下来才拦得住。这和 installer.APP_VERSION 那条是同一个套路的延伸。
    """
    here = HERE
    pairs = (
        ("main.APP_TITLE", os.path.join(here, "main.py"), "APP_TITLE"),
        ("installer.APP_NAME", os.path.join(here, "installer.py"), "APP_NAME"),
        ("state_store.APP_DIRNAME", os.path.join(here, "state_store.py"), "APP_DIRNAME"),
        ("image_tool.APP_TITLE", os.path.join(here, "image_tool.py"), "APP_TITLE"),
    )
    values = {}
    for label, path, const in pairs:
        if not os.path.isfile(path):
            raise SystemExit("[ERROR] 找不到 %s（%s）" % (path, label))
        values[label] = ast_module_string(path, const)

    distinct = sorted(set(values.values()))
    if len(distinct) != 1:
        detail = "\n".join("        %-26s = %r" % (k, v) for k, v in values.items())
        raise SystemExit(
            "[ERROR] 产品名在不同模块里不一致：\n%s\n"
            "        请把上面几个常量统一成同一个字符串。" % detail
        )
    log("    产品名一致 OK: %r（%d 处）" % (distinct[0], len(values)))


def assert_copies_of_version() -> None:
    """断言源码里那些"版本号的副本"都等于 ``version_check.APP_VERSION``。

    版本号是典型的"会被抄到很多地方"的东西，而这个项目已经因为抄漏栽过一次：
    容器 meta 里的产品版本曾经写死 1.0.0，改版本时没人想起来它，
    于是 ``payload.zip`` 里写着 "HAAVIK Photo 1.0.0"，
    而 exe 已经是 1.1.0 —— 同一份东西两个说法。

    现在的分工：
      * ``make_bundle.py``（只在构建期运行，不进任何 exe）**直接 import** 唯一来源，不抄；
      * ``installer.py`` 里的 ``APP_VERSION`` 是**有意的**副本（不 import 是为了让
        Setup.exe 的依赖图里不出现 urllib），由下面这条断言钉住。
    """
    found = ast_module_string(os.path.join(HERE, "installer.py"), "APP_VERSION")

    if version_check.parse_version(found) != version_check.parse_version(version_check.APP_VERSION):
        raise SystemExit(
            "[ERROR] installer.py 的 APP_VERSION=%r 与 version_check.APP_VERSION=%r 不一致。\n"
            "        installer.py 不 import version_check（免得 Setup.exe 带上 urllib），\n"
            "        所以这份副本必须手工同步。"
            % (found, version_check.APP_VERSION)
        )
    log("    installer.APP_VERSION OK: %s（与 APP_VERSION 一致）" % found)


def check_toolchains() -> None:
    for label, exe in (("32位", PYTHON_X86), ("64位", PYTHON_X64)):
        if not os.path.isfile(exe):
            raise SystemExit("[ERROR] 找不到%s工具链：%s" % (label, exe))
        bits = python_bits(exe)
        expected = 32 if label == "32位" else 64
        if bits != expected:
            raise SystemExit("[ERROR] %s 期望 %d 位，实际 %d 位" % (exe, expected, bits))
        log("    %s 工具链 OK: %s" % (label, exe))
    if not os.path.isfile(APP_IMAGE):
        raise SystemExit(
            "[ERROR] 缺少显示图 %s（主程序将显示「图片加载失败」）" % APP_IMAGE
        )
    log("    显示图 OK: %s (%.0f KB)" % (APP_IMAGE, os.path.getsize(APP_IMAGE) / 1024))

    # 版本号单源校验：version_check.APP_VERSION 是唯一权威，
    # 两份 PE 版本信息（各含数值与字符串两处）都必须与它一致。
    # 否则会出现"程序内提示 1.1.0、文件属性里写着 1.0.0"这种自相矛盾 ——
    # 用户看的正是属性那一栏。
    app_ver = version_check.APP_VERSION
    want = version_check.parse_version(app_ver)
    for label, path in (("主程序版本信息", VERSION_FILE), ("安装器版本信息", SETUP_VERSION_FILE)):
        if not os.path.isfile(path):
            raise SystemExit("[ERROR] 缺少%s %s" % (label, path))
        numeric, shown = parse_version_file(path)
        try:
            shown_t = tuple(int(x) for x in shown.split(".")[:3])
        except ValueError:
            raise SystemExit("[ERROR] %s 的 FileVersion=%r 不是 x.y.z 形式" % (label, shown))
        if numeric != want or shown_t != want:
            raise SystemExit(
                "[ERROR] %s 与 version_check.APP_VERSION=%s 不一致：\n"
                "        filevers=%s    FileVersion=%s\n"
                "        请把 %s 里的 filevers / prodvers / FileVersion / ProductVersion "
                "四处一起改成 %s"
                % (label, app_ver, ".".join(map(str, numeric)), shown,
                   os.path.basename(path), app_ver)
            )
        log("    %s OK: filevers=%s FileVersion=%s（与 APP_VERSION 一致）"
            % (label, ".".join(map(str, numeric)), shown))

    assert_copies_of_version()
    assert_shared_names()


def assert_embedded(path: str, needle: bytes, desc: str) -> None:
    """确认某个资源真的被打进了 exe，没打进去就**构建失败**。

    这条守卫是拿一次真实事故换来的：把构建从 .spec 改成命令行参数时，
    漏掉了 ``--add-data haavik.png``。exe 里根本没有那张图，程序启动后
    显示"[图片加载失败]"——而当时其它所有校验（PE 位数、签名、
    载荷哈希、安装链路）**全部通过**，没有任何一项能发现它。
    教训：资源的"存在性"必须单独断言，不能指望别的检查顺带覆盖。
    """
    with open(path, "rb") as fh:
        blob = fh.read()
    if needle not in blob:
        raise SystemExit(
            "[ERROR] %s 里找不到 %s。\n"
            "        PyInstaller 的 --add-data 漏了，或资源路径写错。" % (path, desc)
        )
    log("      [资源] %-22s 已嵌入 %s" % (os.path.basename(path), desc.decode("utf-8", "replace")))


def _ast_import_roots(path: str) -> list:
    """扫一个 .py，返回它 import 的顶层模块名。"""
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), os.path.basename(path))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append(node.module.split(".")[0])
    return out


# 故意不进主程序、随 AI 组件包分发的模块（与 verify_payload.INTENTIONALLY_EXCLUDED
# 对齐）。它们只会在"用户真下载 AI 组件"之后、由 ai_ops 运行时按需 import，
# 所以**不该进自检清单**——否则装完自检会因"找不到 onnxruntime"误判失败，
# 触发 installer.py 的回滚，把刚装好的程序又卸掉（更糟的是用户以为装上了）。
SELFCHECK_OPTIONAL = {"onnxruntime"}


def write_selfcheck_list(script: str) -> str:
    """给 ``main.py --selfcheck`` 生成「该 import 什么」的清单。

    为什么需要它：打包后的 exe 里没有 .py 源码，--selfcheck 没法自己扫源码，
    所以构建时扫一次、把结果当数据文件塞进 exe。

    ⚠️ 这份清单是**从源码推出来的**，不是手写的。手写第二份迟早会和源码脱节，
    而脱节的清单只会给出「一切正常」的假绿灯 —— 比没有更糟（v1.6.0 的教训：
    当时审计就是只打印、不判失败，于是没人当回事）。

    遍历方式是**跟着入口脚本的 import 图走**，不是扫整个目录：目录里的
    installer.py 不会被打进主程序，把它算进来会让自检凭空失败。

    注意 ``_ast_import_roots`` 用 ``ast.walk`` 连函数体内的懒导入也会扫出来
    （例如 ai_ops 在函数里 ``import onnxruntime``、``import numpy``）。numpy 被
    ``HIDDENIMPORTS`` 强制打进 exe，所以没关系；但 onnxruntime 是**故意排除**的，
    必须在这里剔掉（见 ``SELFCHECK_OPTIONAL``），否则装完自检会误报失败并回滚。
    """
    local = {os.path.splitext(f)[0] for f in os.listdir(HERE) if f.endswith(".py")}
    entry = os.path.splitext(os.path.basename(script))[0]
    seen_local, external, queue = set(), set(), [entry]
    while queue:
        name = queue.pop()
        if name in seen_local:
            continue
        seen_local.add(name)
        for root in _ast_import_roots(os.path.join(HERE, name + ".py")):
            if root in ("__future__", entry):
                continue
            if root in SELFCHECK_OPTIONAL:
                # 随 AI 组件包分发、运行期才需要的库，不该在装完自检时强求。
                continue
            if root in local:
                queue.append(root)
            else:
                external.add(root)
    names = sorted(n for n in seen_local if n != entry) + sorted(external)

    os.makedirs(BUILD_TMP, exist_ok=True)
    out = os.path.join(BUILD_TMP, "selfcheck_modules.txt")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(names) + "\n")
    log("      [自检清单] %d 个模块（本地 %d + 外部 %d）"
        % (len(names), len([n for n in names if n in local]), len(external)))
    return out


def build_icon() -> None:
    log("[1/6] 生成图标 ...")
    run([sys.executable, os.path.join(HERE, "make_icon.py"), APP_ICON], "生成图标")


def pyinstaller_cmd(python: str, script: str, name: str, work: str,
                    extra_add_data=(), version_file: str = VERSION_FILE,
                    extra_hiddenimports=()) -> list:
    cmd = [
        python, "-m", "PyInstaller",
        "--onefile", "--noconsole", "--clean", "--noconfirm", "--noupx",
        "--name", name,
        "--distpath", DIST,
        "--workpath", os.path.join(BUILD_TMP, work),
        "--specpath", BUILD_TMP,
        "--icon", APP_ICON,
        "--version-file", version_file,
    ]
    for e in EXCLUDES:
        cmd += ["--exclude-module", e]
    for h in HIDDENIMPORTS:
        cmd += ["--hidden-import", h]
    for h in extra_hiddenimports:
        cmd += ["--hidden-import", h]
    for src, dst in extra_add_data:
        cmd += ["--add-data", "%s%s%s" % (src, os.pathsep, dst)]
    cmd.append(script)
    return cmd


def build_main() -> None:
    # 主程序运行时用 resource_path("haavik.png") 找这张图（sys._MEIPASS 下），
    # 所以必须 --add-data 打进 exe。原 .spec 里有 datas=[('haavik.png','.')]，
    # 改成命令行参数时漏过一次，见 assert_embedded 的说明。
    # selfcheck_modules.txt：main.py --selfcheck 靠它知道该 import 什么。
    # 少了它，自检会当场判失败（宁可拦住发布，也不能悄悄放行）。
    data = ((APP_IMAGE, "."), (write_selfcheck_list(MAIN_PY), "."))

    log("[2/6] 构建 32 位主程序 (MyAlbum_x86.exe) ...")
    run(pyinstaller_cmd(PYTHON_X86, MAIN_PY, "MyAlbum_x86", "main_x86", extra_add_data=data),
        "构建 x86 主程序")
    x86 = os.path.join(DIST, "MyAlbum_x86.exe")
    log("      产物位数: %s" % pe_architecture(x86))
    assert_embedded(x86, b"haavik.png", b"haavik.png")
    assert_embedded(x86, b"selfcheck_modules.txt", b"selfcheck_modules.txt")

    log("[3/6] 构建 64 位主程序 (MyAlbum.exe) ...")
    run(pyinstaller_cmd(PYTHON_X64, MAIN_PY, "MyAlbum", "main_x64", extra_add_data=data),
        "构建 x64 主程序")
    x64 = os.path.join(DIST, "MyAlbum.exe")
    log("      产物位数: %s" % pe_architecture(x64))
    assert_embedded(x64, b"haavik.png", b"haavik.png")
    assert_embedded(x64, b"selfcheck_modules.txt", b"selfcheck_modules.txt")


def build_payload() -> None:
    log("[4/6] 生成载荷容器 payload.zip ...")
    run([PYTHON_X86, os.path.join(HERE, "make_bundle.py"), "--root", ROOT], "生成载荷容器")


def build_setup() -> None:
    log("[5/6] 构建 32 位安装器 Setup.exe ...")
    # 安装失败一键上报需要发信凭据模块 _smtp_secret。
    # 它不在仓库里（make_smtp_secret.py 从 feedback_smtp.txt 生成），
    # 没生成就构建 = 打出一个「点了发送却发不出去」的安装包，必须拦下。
    smtp_secret = os.path.join(HERE, "_smtp_secret.py")
    if not os.path.isfile(smtp_secret):
        raise SystemExit(
            "[ERROR] 找不到 %s：安装器要打包发信凭据才能上报错误报告。\n"
            "        先运行：python make_smtp_secret.py\n"
            "        （需要 code_review/feedback_smtp.txt，格式见 make_smtp_secret.py 头部）"
            % smtp_secret)
    # 安装器必须用 32 位 Python 编译 —— 这是"32/64 位都能跑"的关键
    run(
        pyinstaller_cmd(
            PYTHON_X86, INSTALLER_PY, "Setup", "setup_x86",
            extra_add_data=((BUNDLE, "."),),
            version_file=SETUP_VERSION_FILE,
            extra_hiddenimports=("_smtp_secret",),
        ),
        "构建安装器",
    )
    arch = pe_architecture(os.path.join(DIST, SETUP_EXE))
    log("      Setup.exe 位数: %s" % arch)
    if "x86" not in arch:
        raise SystemExit(
            "[ERROR] Setup.exe 不是 32 位，将无法在 32 位 Windows 上运行。请检查工具链。"
        )
    assert_embedded(os.path.join(DIST, SETUP_EXE), b"payload.zip", b"payload.zip")


def sign_main_exes() -> None:
    """签名两个主程序。

    **必须在 build_payload() 之前调用。** 载荷容器里的 crc32 是打包那一刻
    算出来的；只有先签后打包，"容器内字节 == dist/ 里字节"才成立。
    """
    log("[3.5/6] 签名主程序（署名 %s）..." % sign_build.CERT_CN)
    info = sign_build.ensure_cert()
    log("      证书 %s" % info["thumbprint"])
    log("      主体 %s   有效期至 %s" % (info["subject"], info["not_after"]))
    paths = [os.path.join(DIST, MAIN_X86_EXE), os.path.join(DIST, MAIN_X64_EXE)]
    for r in sign_build.sign_files(paths):
        log("      [签名] %-22s %s" % (os.path.basename(r["path"]), r["status"]))


def sign_setup() -> None:
    """签名安装器。**必须在 PyInstaller 编译完成之后调用。**"""
    log("[5.5/6] 签名安装器 ...")
    for r in sign_build.sign_files([os.path.join(DIST, SETUP_EXE)]):
        log("      [签名] %-22s %s" % (os.path.basename(r["path"]), r["status"]))


def verify() -> None:
    log("[6/6] 校验产物 ...")
    targets = [
        (os.path.join(DIST, SETUP_EXE), "Setup.exe (32位安装器，32/64 位系统通用)"),
        (os.path.join(DIST, MAIN_X64_EXE), "MyAlbum.exe (x64 主程序)"),
        (os.path.join(DIST, MAIN_X86_EXE), "MyAlbum_x86.exe (x86 主程序)"),
    ]
    ok = True
    for path, desc in targets:
        if not os.path.isfile(path):
            log("    [MISSING] %s" % path)
            ok = False
            continue
        log(
            "    %-52s %8.2f MB  %s"
            % (desc, os.path.getsize(path) / 1024 / 1024, pe_architecture(path))
        )
    if os.path.isfile(BUNDLE):
        log("    %-52s %8.2f MB" % ("payload.zip（载荷容器）", os.path.getsize(BUNDLE) / 1024 / 1024))

    # 签名状态：只有 SignerCertificate 存在才算签上了。
    # 自签名证书必然显示 UnknownError（根不受信任），这不是失败。
    log("    签名状态（自签名，未导入证书的机器上必然不是 Valid）：")
    for r in sign_build.signature_status([p for p, _ in targets if os.path.isfile(p)]):
        flag = "OK " if r["signer"] else "无 "
        log("      %s %-24s %s" % (flag, os.path.basename(r["path"]), r["status"]))
        if r["signer"]:
            log("          %s" % r["signer"])

    if not ok:
        raise SystemExit("[ERROR] 产物不完整")


def main() -> int:
    parser = argparse.ArgumentParser(description="HAAVIK Photo 一键构建")
    parser.add_argument("--skip-main", action="store_true", help="跳过主程序构建")
    parser.add_argument("--only-setup", action="store_true", help="只重打安装器")
    parser.add_argument("--verify", action="store_true", help="只校验")
    parser.add_argument("--no-sign", action="store_true",
                        help="跳过 Authenticode 签名（默认会给三个 exe 签名）")
    args = parser.parse_args()

    started = time.time()
    os.makedirs(DIST, exist_ok=True)
    os.makedirs(BUILD_TMP, exist_ok=True)

    log("项目根目录: %s" % ROOT)
    log("检查工具链 ...")
    check_toolchains()

    if args.verify:
        verify()
        return 0

    build_icon()
    if not (args.skip_main or args.only_setup):
        build_main()
    else:
        log("[2-3/6] 跳过主程序构建（使用现有 dist/*.exe）")
        for f in (MAIN_X64_EXE, MAIN_X86_EXE):
            p = os.path.join(DIST, f)
            if not os.path.isfile(p):
                raise SystemExit("[ERROR] 缺少 %s，无法跳过主程序构建" % p)

    # 签名主程序必须在打包载荷之前 —— 容器里的 crc32 依赖 dist/ 的最终字节
    if not args.no_sign:
        sign_main_exes()

    build_payload()
    build_setup()

    # 安装器只能在编译完成后签
    if not args.no_sign:
        sign_setup()

    verify()

    log("\n全部完成，用时 %.1f 秒" % (time.time() - started))
    log("产物目录: %s" % DIST)
    return 0


if __name__ == "__main__":
    sys.exit(main())
