# -*- coding: utf-8 -*-
"""决定性复现：把 PyInstaller 的 onefile 内部环境变量喂给一个新进程，看它报不报那个框。

上一版只喂了 _PYI_PARENT_PROCESS_LEVEL，没复现 —— 因为引导器判断"我是第二阶段
子进程"看的是 **_PYI_ARCHIVE_FILE**（PyInstaller CHANGES 里写的：为了防伪造环境，
GHSA-9fxf-4qw3-3ghmr）。所以这次把几个变量分开喂，看哪一个能触发。

判定方式：启动后 6 秒内出现**标题恰为 "Error" 的可见窗口** ——
那是引导器 fatal error 的窗口（我们自己的弹窗没有一个叫 Error）。
"""
import ctypes
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "_pyi2.txt")
L = []

user32 = ctypes.windll.user32
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                     ctypes.POINTER(ctypes.c_int))


def window_titles():
    out = []

    def cb(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n > 0:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            out.append(buf.value)
        return True

    user32.EnumWindows(EnumWindowsProc(cb), None)
    return out


EXE = os.path.join(ROOT, "dist", "MyAlbum.exe")
ARGS = [EXE, "--no-image", "--no-update-check"]


def run_once(label, extra):
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("_PYI_") or k in ("_MEIPASS", "_MEIPASS2"):
            env.pop(k, None)
    env.update(extra)
    proc = subprocess.Popen(ARGS, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    time.sleep(6)
    titles = window_titles()
    err = [t for t in titles if t.strip() == "Error"]
    alive = proc.poll() is None
    L.append("")
    L.append("  [%s]" % label)
    L.append("      注入：%r" % extra)
    L.append("      进程活着=%s  有 Error 框=%s" % (alive, "**是**" if err else "否"))
    if not err:
        L.append("      可见窗口（前 6 个）：%r" % titles[:6])
    try:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, timeout=30)
    except Exception as exc:                               # noqa: BLE001
        L.append("      taskkill 失败：%r" % exc)
    time.sleep(1.5)
    return bool(err)


L.append("目标程序：%s" % EXE)
L.append("它的引导器里编着那段检查：%s"
         % (b"Security validation failure" in open(EXE, "rb").read()))

results = {}
results["干净环境"] = run_once("对照组：干净环境", {})
results["仅 ARCHIVE_FILE"] = run_once(
    "只喂 _PYI_ARCHIVE_FILE", {"_PYI_ARCHIVE_FILE": EXE})
results["ARCHIVE+LEVEL"] = run_once(
    "喂 ARCHIVE_FILE + PARENT_PROCESS_LEVEL",
    {"_PYI_ARCHIVE_FILE": EXE, "_PYI_PARENT_PROCESS_LEVEL": "1"})
results["ARCHIVE+HOME+LEVEL"] = run_once(
    "喂三件套（引导器真正会设的那几个）",
    {"_PYI_ARCHIVE_FILE": EXE, "_PYI_PARENT_PROCESS_LEVEL": "1",
     "_PYI_APPLICATION_HOME_DIR": os.path.dirname(EXE)})

L.append("")
L.append("== 结论 ==")
for k, v in results.items():
    L.append("  %-22s 出现 Error 框 = %s" % (k, v))
if results["干净环境"] is False and any(
        v for k, v in results.items() if k != "干净环境"):
    L.append("  ✓ 复现成立：**继承下来的 _PYI_* 环境变量就是元凶**。")
    L.append("  修法：拉起任何子进程（重启自己 / 启动安装包 / 安装完启动程序）")
    L.append("        之前，把 _PYI_* 与 _MEIPASS* 从环境里清掉。")
else:
    L.append("  仍未复现，需要换思路。")

with open(OUT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(L) + "\n")
print("\n".join(L))
