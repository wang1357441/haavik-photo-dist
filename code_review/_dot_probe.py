# -*- coding: utf-8 -*-
"""真实环境里点 10 下白点，走完「口令框 → 输错 → 输对 → 触发重启回调」。

image_tool_test 用 FakeDlg 替掉了 PasswordDialog —— 这恰好遮住了
"对话框真身能不能构建成功"这件事。这里不替，全程走真代码：
真实 Tk + 真实 ImageTool + 真实 PasswordDialog，并且把 tk 回调里
被吞掉的异常抓出来（--windowed 打包后 stderr 不可见，异常会静默消失，
表现出来就是"点了没反应"）。

注意 show() 会 wait_window 阻塞，所以必须**先排好"谁来关它"**再点第 10 下。
（第一版探针忘了这件事，结果 fix 生效后它真的把口令框打开了、
然后永远等在那里 —— 反倒成了 bug 已修的旁证。）
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
OPTIMIZED = os.path.join(HERE, "optimized")
OUT = os.path.join(HERE, "_dot_probe.txt")
if OPTIMIZED not in sys.path:
    sys.path.insert(0, OPTIMIZED)

L = []
errors = []

import tkinter as tk                                       # noqa: E402
import image_tool                                          # noqa: E402

root = tk.Tk()
root.geometry("1100x720+80+40")

# 把 tk 回调里被吞掉的异常捞出来 —— 这是本次排查的关键
root.report_callback_exception = lambda *a: errors.append(
    "".join(traceback.format_exception(*a)))

fired = []
tool = image_tool.ImageTool(root, on_quit=None,
                            on_restart=lambda: fired.append(1),
                            on_check_update=None)
tool.build()
root.update()

# --- 1) 白点真的在界面上吗 ---
dot = tool._dot
L.append("白点控件：%s" % dot.winfo_class())
L.append("  存在=%s  已布局=%s  宽高=%dx%d  屏幕位置=(%d,%d)" % (
    bool(dot.winfo_exists()), bool(dot.winfo_ismapped()),
    dot.winfo_width(), dot.winfo_height(),
    dot.winfo_rootx(), dot.winfo_rooty()))
L.append("  状态栏可见=%s  窗口尺寸=%dx%d" % (
    bool(dot.master.winfo_ismapped()),
    root.winfo_width(), root.winfo_height()))
L.append("  画布里的图元=%r" % (dot.find_all(),))
L.append("  绑定了 <Button-1> = %s" % bool(dot.bind("<Button-1>")))
L.append("  口令常量=%r  需要连点=%d 下  间隔上限=%dms" % (
    image_tool.RESTART_PASSWORD, image_tool.DOT_CLICKS,
    image_tool.DOT_RESET_MS))


def toplevels():
    return [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]


# --- 2) 连点 9 下：不该有任何事发生 ---
L.append("")
L.append("== 用 <Button-1> 事件连点 9 下 ==")
for _ in range(9):
    dot.event_generate("<Button-1>", when="now")
    root.update()
L.append("  内部计数=%d  顶层窗口数=%d" % (tool._dot_count, len(toplevels())))
rep9 = (tool._dot_count == 9 and not toplevels())

# --- 3) 第 10 下：先排好"谁来填口令"，再点 ---
L.append("")
L.append("== 第 10 下：口令框应该冒出来 ==")
plan = ["0000", image_tool.RESTART_PASSWORD]     # 先输错，再输对
captured = []


def drive():
    """口令框一出现就抓住它：记录状态 → 填口令 → 点确定。"""
    for w in toplevels():
        if w in captured:
            continue
        captured.append(w)
        form = getattr(w, "form", None)
        if form is None or not hasattr(form, "entry"):
            L.append("  ⚠ 对话框没有 form/entry，直接关掉")
            w.destroy()
            return
        L.append("  对话框出现：title=%r  可见=%s  尺寸=%dx%d  位置=(%d,%d)" % (
            w.title(), bool(w.winfo_ismapped()),
            w.winfo_width(), w.winfo_height(),
            w.winfo_rootx(), w.winfo_rooty()))
        L.append("  输入框掩码=%r" % form.entry.entry.cget("show"))
        text = plan.pop(0)
        form.entry.set(text)
        form.apply()
        L.append("  尝试口令 %r 之后：窗口还在=%s  错误提示=%r" % (
            text, bool(toplevels()), form.err.cget("text")))
        if plan:
            root.after(80, drive)
        return
    root.after(60, drive)          # 还没出现，等一会儿再看


root.after(120, drive)
dot.event_generate("<Button-1>", when="now")      # 这一行会阻塞到口令框被关掉
root.update()

rep10 = (len(captured) == 1 and tool._dot_count == 0)

# --- 4) 口令通过后，on_restart 应当在 ~150ms 后被调用 ---
deadline = 3.0
step = 0.02
waited = 0.0
while waited < deadline and not fired:
    root.update()
    import time
    time.sleep(step)
    waited += step

L.append("")
L.append("== 结果 ==")
L.append("  第 10 下弹出对话框：%s" % ("是" if captured else "**否**"))
L.append("  触发后计数归零：%s" % ("是" if tool._dot_count == 0 else "否"))
L.append("  on_restart 被调用：%s（等了 %.2fs）" % (
    "是" if fired == [1] else "**否 %r**" % fired, waited))
L.append("  捕获到的 tk 回调异常：%d 个" % len(errors))
for e in errors:
    L.append(e)

verdict = rep9 and rep10 and fired == [1] and not errors
L.append("")
L.append("最终判定：%s" % ("✓ 白点入口全链路可用"
                          if verdict else "✗ 仍有问题"))

with open(OUT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(L) + "\n")

try:
    root.destroy()
except tk.TclError:
    pass
print("verdict=%s" % ("PASS" if verdict else "FAIL"))
sys.exit(0 if verdict else 1)
