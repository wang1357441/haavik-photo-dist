# -*- coding: utf-8 -*-
"""把安装器真实 GUI 流程跑一遍（装到临时目录，不碰桌面），抓出所有弹窗。

目的：使用者说"安装完成后会弹出一个不认识的警告"。我们的代码里，成功路径
明明没有任何弹窗 —— 那就得用**真的把流程走完**来定论：
如果这段跑完，弹窗记录是空的，那这个框一定来自安装器之外
（Windows SmartScreen / UAC / 杀软的联网或改文件提示）。

做法：直接驱动 installer.Installer（真类、真 _do_install、真 _build_finish），
只把 messagebox 换成一个记录器；输出目录指向临时目录。
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OPT = os.path.join(HERE, "optimized")
OUT = os.path.join(HERE, "_installer_dlg.txt")
sys.path.insert(0, OPT)

import tkinter as tk                                       # noqa: E402
import installer                                           # noqa: E402

L = []
pops = []


class MB:
    """替掉 messagebox：只记录，不弹（否则会卡住等人点）。"""

    shown = pops

    @staticmethod
    def _rec(kind, *args, **kw):
        shown.append((kind, args[0] if args else "", args[1] if len(args) > 1 else ""))

    showinfo = staticmethod(lambda *a, **k: MB._rec("info", *a, **k))
    showwarning = staticmethod(lambda *a, **k: MB._rec("warning", *a, **k))
    showerror = staticmethod(lambda *a, **k: MB._rec("error", *a, **k))
    askyesno = staticmethod(lambda *a, **k: (MB._rec("askyesno", *a, **k), True)[1])
    askokcancel = staticmethod(lambda *a, **k: (MB._rec("askokcancel", *a, **k), True)[1])


installer.messagebox = MB()

bundle_path = os.path.join(ROOT, "payload.zip")
L.append("载荷容器：%s（%s 字节）" % (bundle_path, os.path.getsize(bundle_path)))
bundle = installer.Bundle(bundle_path)
L.append("meta: %r" % (bundle.meta,))
L.append("容器里的资源：%r" % (bundle.resources(),))
L.append("")

tmp = tempfile.mkdtemp(prefix="hvinstall_")
target = os.path.join(tmp, "out")
os.makedirs(target)
L.append("安装到临时目录：%s" % target)

root = tk.Tk()
root.withdraw()
inst = installer.Installer(root, bundle)
inst.install_dir = target
inst.target_exe = os.path.join(target, installer.PROGRAM_FILENAME)
inst.arch = "x64"
inst.launch_after.set(False)          # 不真去启动，避免拉出主程序窗口
L.append("目标 exe：%s" % inst.target_exe)
L.append("")

# ---- 真的走一遍安装（同步调，after 回调靠 update() 抽干）----
L.append("== 调用 _do_install() ==")
try:
    inst._do_install()
    for _ in range(60):
        root.update()
    L.append("  _do_install 正常结束")
except Exception as exc:                                   # noqa: BLE001
    L.append("  _do_install 抛了：%r" % exc)

got = os.path.join(target, installer.PROGRAM_FILENAME)
L.append("  产物存在：%s（%s 字节）" % (
    os.path.isfile(got), os.path.getsize(got) if os.path.isfile(got) else 0))
L.append("  目录内容：%r" % os.listdir(target))
L.append("  有 .part 残留：%s" % any(n.endswith(".part") for n in os.listdir(target)))

# ---- 完成页 + 点"完成" ----
L.append("")
L.append("== 走完成页 _build_finish() + 点「完成」_finish() ==")
try:
    inst._build_finish()
    for _ in range(10):
        root.update()
    L.append("  完成页构建 OK")
except Exception as exc:                                   # noqa: BLE001
    L.append("  完成页构建抛了：%r" % exc)

try:
    inst._finish()
    for _ in range(10):
        root.update()
    L.append("  _finish 正常结束")
except Exception as exc:                                   # noqa: BLE001
    L.append("  _finish 抛了：%r" % exc)

# ---- 结论 ----
L.append("")
L.append("== 全程捕获到的弹窗 ==")
if not pops:
    L.append("  **一个都没有** → 这个框不是我们的安装器弹的")
else:
    for kind, title, body in pops:
        L.append("  [%s] %s" % (kind, title))
        L.append("      %s" % str(body).replace("\n", " / ")[:200])

try:
    root.destroy()
except tk.TclError:
    pass
shutil.rmtree(tmp, ignore_errors=True)
L.append("")
L.append("（临时目录已删除，桌面与 LOCALAPPDATA 都没碰）")

with open(OUT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(L) + "\n")
print("done")
