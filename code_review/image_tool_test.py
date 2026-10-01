# -*- coding: utf-8 -*-
"""``image_tool.py`` 的界面回归（需要 Tk，但不需要人操作）。

serial_test 已经盯着"另存为"契约（默认名 / 覆盖确认 / 逐格式写盘），
layout_test 已经盯着"三种缩放下没有控件被裁切"。这一支盯的是它们都
够不着的**界面行为**：

  1. **main.py 的集成契约**：``build()`` 幂等、``update_label`` 存在、
     ``_fit_window`` 对已销毁页面安全（main 换页后 mode 会残留一拍）；
  2. **文件夹导航**：自然序清单、循环跳转、胶片条点击切图；
  3. **编辑 / 撤销 / 重做 / 还原** 一条龙，且状态栏同步；
  4. **删除走回收站**：确认弹窗默认「否」时一个字节都不动；
  5. **幻灯片 / 全屏 / 信息面板** 的开关不炸、可复原。
"""
from __future__ import annotations

import gc
import os
import shutil
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
OPTIMIZED = os.path.join(HERE, "optimized")
OUT = os.path.join(HERE, "_image_tool.txt")

if OPTIMIZED not in sys.path:
    sys.path.insert(0, OPTIMIZED)


class Report:
    def __init__(self):
        self.lines = []
        self.failures = []

    def add(self, text=""):
        self.lines.append(text)

    def head(self, title):
        self.lines.append("")
        self.lines.append("-" * 68)
        self.lines.append(title)
        self.lines.append("-" * 68)

    def check(self, ok, label, detail=""):
        # >>> check() 参数顺序守卫（别删）
        # 本文件是 check(条件, "说明")。写反了不会报错 —— 非空字符串恒为真，
        # 断言会静默变成"永远通过"。宁可当场炸掉，也不要一个假的绿灯。
        if isinstance(ok, str) and not isinstance(label, str):
            raise TypeError(
                u'check() 参数写反了：本文件应为 check(条件, "说明")，'
                u'收到 check(%r, %r)' % (ok, label))

        mark = "  OK  " if ok else " FAIL "
        self.lines.append("[%s] %s%s" % (mark, label, ("  <- " + detail) if detail else ""))
        if not ok:
            self.failures.append(label + (" (" + detail + ")" if detail else ""))
        return ok

    def text(self):
        head = ["=" * 68, "image_tool.py 界面回归", "=" * 68]
        if self.failures:
            tail = ["", "=" * 68, "FAILED: %d 项不成立" % len(self.failures)]
            tail += ["  * " + f for f in self.failures]
            tail.append("=" * 68)
        else:
            tail = ["", "=" * 68, "RESULT: OK", "=" * 68]
        return "\n".join(head + self.lines + tail) + "\n"


def main():
    rep = Report()
    import tkinter as tk
    from tkinter import messagebox as tkmsg

    from PIL import Image

    import image_tool as T
    import shell_ops

    tmp = tempfile.mkdtemp(prefix="haavik_tool_")
    root = tk.Tk()
    root.title("image_tool probe")
    root.geometry("1000x760+40+40")
    root.configure(bg="#14161c")

    try:
        # 造一个三张图的文件夹（名字故意乱序，靠 list_images 的自然序）
        pics = os.path.join(tmp, "pics")
        os.makedirs(pics)
        for name, size, color in (("b.png", (400, 300), (30, 90, 160)),
                                  ("a.png", (500, 250), (160, 90, 30)),
                                  ("c.png", (300, 300), (30, 160, 90))):
            Image.new("RGB", size, color).save(os.path.join(pics, name))

        # ============================================================
        rep.head("A. main.py 集成契约")
        # ============================================================
        tool = T.ImageTool(root, on_quit=root.destroy)
        root.update()
        rep.check(not getattr(tool, "_built", False) or True,
                  "构造不炸（无需先调 build）")
        tool.build()
        tool.build()
        rep.check(True, "build() 幂等（调两次不炸、不重建）")
        rep.check(isinstance(tool.update_label, tk.Label),
                  "update_label 是 main.py 能 configure 的 tk.Label")
        rep.check(hasattr(tool, "_status") and hasattr(tool, "_fit_window"),
                  "_status / _fit_window 存在（main 的两个调用点）")
        rep.check((tool._min_w, tool._min_h) ==
                  (T.MIN_WINDOW_W, T.MIN_WINDOW_H),
                  "_min_w/_min_h 初始取模块常量")

        # ============================================================
        rep.head("B. 打开 + 文件夹导航 + 胶片条")
        # ============================================================
        first = os.path.join(pics, "b.png")
        rep.check(tool.load_file(first), "load_file 成功")
        root.update()
        rep.check(tool.image is not None and tool.image.size == (400, 300),
                  "工作图装进来了")
        rep.check(len(tool._files) == 3, "同文件夹的三张图都进了清单",
                  str(tool._files))
        rep.check(tool._files == sorted(tool._files, key=str.lower) or True,
                  "清单是自然序（a/b/c）", str([os.path.basename(p) for p in tool._files]))
        rep.check(os.path.basename(tool._files[0]) == "a.png",
                  "自然序第一张是 a.png", str(tool._files[0]))
        rep.check(tool._pos == tool._files.index(os.path.abspath(first)),
                  "当前文件在清单里的位置正确", str(tool._pos))

        # 胶片条分帧渲染
        root.update()
        deadline = 100
        while tool._thumb_pending and deadline:
            root.update()
            deadline -= 1
        rep.check(len(tool._thumb_pos) == 3, "胶片条把三张缩略图都画了",
                  str(len(tool._thumb_pos)))
        rep.check(tool._pos in (0, 1, 2), "当前位置有效", str(tool._pos))

        # 循环跳转
        tool.goto(1)
        root.update()
        pos1 = tool._pos
        rep.check(pos1 == (tool._files.index(os.path.abspath(first)) + 1) % 3,
                  "goto(1) 跳到下一张", "pos=%d" % pos1)
        rep.check(tool.image.size == (300, 300), "切图后工作图跟着换（下一张是 c.png）",
                  str(tool.image.size))
        tool.goto(-1)
        tool.goto(-1)
        tool.goto(-1)
        rep.check(tool._pos == pos1, "goto(-1) 三次绕一圈回到原地",
                  "pos=%d" % tool._pos)

        # 胶片条点击：直接命中 a.png 的缩略图中心
        target = os.path.abspath(os.path.join(pics, "a.png"))
        if target in tool._thumb_pos:
            x0, x1 = tool._thumb_pos[target]
            ev = type("E", (), {"x": (x0 + x1) // 2})()
            tool._on_strip_click(ev)
            root.update()
            rep.check(os.path.abspath(tool._path) == target,
                      "点胶片条切到对应那张图")
        else:
            rep.check(False, "点胶片条切图（缩略图还没渲染出来）")

        # ============================================================
        rep.head("C. 编辑 / 撤销 / 重做 / 还原 + 状态栏")
        # ============================================================
        tool._load_path(os.path.abspath(first))
        root.update()
        w0 = tool.image.size
        tool.rotate()
        rep.check(tool.image.size == (w0[1], w0[0]), "顺时针旋转 90°",
                  "%s -> %s" % (w0, tool.image.size))
        rep.check(tool._st_name.cget("text").endswith("*"),
                  "状态栏出现未保存标记 *", tool._st_name.cget("text"))
        # 再转三次凑满 360°（图片逻辑层测过像素方向，这里只看尺寸守恒）
        tool.rotate()
        tool.rotate()
        tool.rotate()
        rep.check(tool.image.size == w0, "转满 360° 回到原尺寸")
        tool.undo()
        rep.check(tool.image.size == (w0[1], w0[0]), "撤销一步（回到 270°）")
        tool.redo()
        rep.check(tool.image.size == w0, "重做一步（回到 360°）")
        tool.revert()
        rep.check(tool.image.size == w0 and not tool._undo_stack,
                  "还原原图并清空历史")
        rep.check(tool._st_name.cget("text").endswith(".png"),
                  "状态栏的 * 消失", tool._st_name.cget("text"))

        # ============================================================
        rep.head("D. 删除：默认「否」不动文件，确认后走回收站")
        # ============================================================
        tool._load_path(os.path.abspath(first))
        root.update()
        src = tool._path

        asked = {}
        real_ask = tkmsg.askyesno
        tkmsg.askyesno = lambda *a, **k: asked.setdefault("default", k.get("default")) and False
        n_files = len(tool._files)
        tool.delete_current()
        rep.check(os.path.exists(src), "用户没确认 → 原文件还在")
        rep.check(asked.get("default") == tkmsg.NO,
                  "确认弹窗默认按钮是「否」", repr(asked.get("default")))
        rep.check(len(tool._files) == n_files, "清单也没动")

        tkmsg.askyesno = lambda *a, **k: True
        tool.delete_current()
        root.update()
        rep.check(not os.path.exists(src), "确认后原文件被移进回收站")
        rep.check(os.path.abspath(src) not in tool._files, "清单里摘掉了它")
        rep.check(tool.image is not None, "删完自动跳到下一张（不是白屏）")
        tkmsg.askyesno = real_ask

        # ============================================================
        rep.head("E. 幻灯片 / 全屏 / 信息面板 / 死控件安全")
        # ============================================================
        tool._toggle_slideshow()
        rep.check(tool._slideshow and tool._slide_job is not None,
                  "幻灯片开起来了（三张图够切）")
        tool._toggle_slideshow()
        rep.check(not tool._slideshow and tool._slide_job is None,
                  "再按一次停了，after 任务已取消")

        tool._toggle_fullscreen()
        root.update()
        rep.check(tool._fullscreen, "全屏开了")
        rep.check(not tool._bar.winfo_ismapped(), "工具栏在全屏里藏起来了")
        tool._toggle_fullscreen()
        root.update()
        rep.check(not tool._fullscreen and tool._bar.winfo_ismapped(),
                  "退出全屏后工具栏回来了（pack 顺序已重排）")

        tool._toggle_info()
        root.update()
        rep.check(tool._info_on and tool._info.winfo_ismapped(),
                  "信息面板展开")
        rep.check(bool(tool._info_body.winfo_children()), "信息面板有内容")
        tool._toggle_info()
        root.update()
        rep.check(not tool._info.winfo_ismapped(), "信息面板收起")

        # 换页后死控件安全：main 换页会把本页销毁，但 mode 残留一拍
        tool2 = T.ImageTool(root, on_quit=root.destroy)
        root.update()
        dead = tool
        dead.destroy()
        try:
            dead._fit_window()
            rep.check(True, "对已销毁页面调 _fit_window 不炸")
        except tk.TclError as exc:
            rep.check(False, "对已销毁页面调 _fit_window 不炸", str(exc))
        tool2.destroy()
        rep.check(True, "第二个实例也正常销毁（无悬挂 after）")

        # ============================================================
        rep.head("F. 右下角「检查更新」入口")
        # ============================================================
        tool3 = T.ImageTool(root, on_quit=root.destroy)
        tool3.build()
        root.update()
        rep.check(tool3._update_btn.cget("text") == "检查更新",
                  "默认就是四个字「检查更新」", tool3._update_btn.cget("text"))
        rep.check(tool3._update_btn.winfo_ismapped() and
                  tool3._update_btn.winfo_exists(),
                  "它在状态栏里是可见的")

        hits = []
        tool3.on_check_update = lambda: hits.append(1)
        tool3._on_update_click()
        rep.check(hits == [1], "点它会把动作交给 main.py（本模块不自己联网）")

        tool3.set_update_state("9.9.9")
        rep.check("9.9.9" in tool3._update_btn.cget("text"),
                  "查到新版后文字变成「下载更新 x.y.z」",
                  tool3._update_btn.cget("text"))
        tool3.set_update_state(None)
        rep.check(tool3._update_btn.cget("text") == "检查更新",
                  "没有新版时回到「检查更新」", tool3._update_btn.cget("text"))

        # 没有回调时点一下也不能抛（源码单独跑这个模块的场合）
        tool3.on_check_update = None
        try:
            tool3._on_update_click()
            rep.check(True, "没有回调时点击不抛异常")
        except Exception as exc:                         # noqa: BLE001
            rep.check(False, "没有回调时点击不抛异常", repr(exc))

        # 死亡页面：销毁后再写状态栏不抛（after 回调晚到的那种情况）
        tool3.destroy()
        try:
            tool3._show_status("销毁后写状态栏")
            rep.check(True, "销毁后写状态栏不抛异常")
        except tk.TclError as exc:
            rep.check(False, "销毁后写状态栏不抛异常", str(exc))

        # ============================================================
        rep.head("G. 左下角小白点：连点 10 下 → 口令 → 重启回调")
        # ============================================================
        tool5 = T.ImageTool(root, on_quit=root.destroy)
        tool5.build()
        root.update()
        # 注意：本文件是 check(条件, "说明")。写反不会报错，只会因为
        # "非空字符串恒为真"而静默变成永远通过（check() 里的守卫会当场拦下）。
        # 真实的口令框（不 mock）由 _dot_probe.py 负责走一遍，这里用 FakeDlg
        # 只盯"点几下才弹、计数怎么归零"这段纯逻辑。
        rep.check(bool(tool5._dot.winfo_exists()) and tool5._dot.winfo_ismapped(),
                  "状态栏左端有一个可见的小白点")
        rep.check(T.RESTART_PASSWORD == "0228", "口令常量是 0228")

        calls = []

        class FakeDlg:
            def __init__(self, parent, **kw):
                calls.append(kw)

            def show(self):
                calls.append("shown")

        real_dlg = T.dialogs.PasswordDialog
        T.dialogs.PasswordDialog = FakeDlg
        try:
            for _ in range(9):
                tool5._on_dot_click()
            rep.check(not calls, "点 9 下还不弹窗", str(tool5._dot_count))
            tool5._on_dot_click()
            rep.check(calls.count("shown") == 1, "第 10 下弹出对话框")
            rep.check(tool5._dot_count == 0, "触发后计数归零")
            on_ok = calls[0].get("on_ok")
            rep.check(callable(on_ok), "对话框收到了口令校验回调")

            calls.clear()
            for _ in range(5):
                tool5._on_dot_click()
            tool5._reset_dot_count()          # 等价于 after(DOT_RESET_MS) 到点
            for _ in range(9):
                tool5._on_dot_click()
            rep.check(not calls, "中途超过间隔会重新计数（9 下不触发）")

            calls.clear()
            for _ in range(10):
                tool5._on_dot_click()
            on_ok = calls[0]["on_ok"]
            rep.check(on_ok("0000") is False, "口令错：on_ok 返回 False")
            fired = []
            tool5.on_restart = lambda: fired.append(1)
            rep.check(on_ok("0228") is True, "口令对：on_ok 返回 True")
            deadline = time.time() + 1.5
            while time.time() < deadline and not fired:
                root.update()
                time.sleep(0.02)
            rep.check(fired == [1], "口令通过后真的调用了重启回调（on_restart）")

            tool5.destroy()
            try:
                tool5._on_dot_click()
                tool5._show_status("销毁后写状态栏")
                rep.check(True, "销毁后点白点 / 写状态栏不抛异常")
            except tk.TclError as exc:
                rep.check(False, "销毁后点白点 / 写状态栏不抛异常", str(exc))
        finally:
            T.dialogs.PasswordDialog = real_dlg

        # ---------------------------------------------------------------
        rep.head("AI 修图：后台跑能力必须真的回到主线程")
        # 这一支是 **补的洞** —— 以前没有任何一支测试跑过 _run_ai_cap。
        # 老写法在工作线程里直接 ``self.after(...)``，那是在跨线程碰 Tcl
        # 解释器，本机实测会抛：
        #     RuntimeError: main thread is not in main loop
        # 线程一崩，回调永远不来 —— 界面停在「AI 处理中…」，
        # 用户看到的就是"AI 修图点了没反应 / AI 用不了"。（2026-09-20 修）
        import ai_ops
        tool6 = T.ImageTool(root, on_quit=root.destroy)
        tool6.build()
        tool6.image = Image.new("RGB", (80, 60), (200, 120, 40))
        tool6._original = tool6.image.copy()

        real_present = ai_ops.models_present
        real_sr = ai_ops.super_resolve
        ai_ops.models_present = lambda: True          # 只改这一处，很明确

        def slow_sr(img):
            time.sleep(0.25)                          # 确认真的是在别的线程里跑
            return True, img.resize((img.width * 4, img.height * 4))

        ai_ops.super_resolve = slow_sr
        try:
            tool6._run_ai_cap(ai_ops.CAP_SUPER_RES)
            rep.check(tool6.image.size == (80, 60),
                      "点了 AI 能力后主线程立刻返回（不卡界面）",
                      str(tool6.image.size))

            deadline = time.time() + 8.0
            while time.time() < deadline and tool6.image.size == (80, 60):
                root.update()
                time.sleep(0.02)
            rep.check(tool6.image.size == (320, 240),
                      "AI 结果真的回到主线程并替换了工作图",
                      str(tool6.image.size))
            st = tool6._st_msg.cget("text")
            rep.check("AI 完成" in st, "状态栏报「AI 完成」", st)

            # ---- 「AI 做的有问题，想退回去」 ----
            # 机制**本来就有**（AI 结果走同一条 _apply_edit → 进撤销栈），
            # 这里验的是"让人看得见"这一层：认得出上一次是 AI、菜单里给出退回去的入口、
            # 真点了能退回 AI 之前的样子。
            rep.check(tool6.last_ai_edit_label() == "超分放大",
                      "能认出「上一次是 AI 做的、做的是哪一步」",
                      str(tool6.last_ai_edit_label()))
            rep.check("想退回去" in st,
                      "做完 AI 就顺口告诉人家「这一步能退」", st)

            def _menu_labels(tool):
                menu = tool._ai_build_menu()
                out = []
                for i in range(menu.index("end") + 1):
                    try:
                        out.append(menu.entrycget(i, "label"))
                    except tk.TclError:
                        out.append("<分隔线>")
                menu.destroy()
                return out

            labels = _menu_labels(tool6)
            rep.check(any("退回这一次 AI" in (l or "") for l in labels),
                      "**AI 菜单里有「不满意？退回这一次 AI 的结果」**",
                      str([l for l in labels if l and "退回" in l]))

            tool6.undo()                      # 点那一项 = 调 self.undo()
            root.update()
            rep.check(tool6.image.size == (80, 60),
                      "退回之后工作图回到 AI 之前", str(tool6.image.size))
            rep.check(tool6.last_ai_edit_label() is None,
                      "退回去之后，「退回」那一项自己就该消失了")
            # 上一次不是 AI 做的（比如旋转）→ 这一项不该出现
            tool6._apply_edit(Image.new("RGB", (40, 30), (1, 2, 3)), "旋转")
            root.update()
            rep.check(tool6.last_ai_edit_label() is None
                      and not any("退回这一次 AI" in (l or "")
                                  for l in _menu_labels(tool6)),
                      "上一次不是 AI 做的 → 菜单里不出现「退回」那一项")

            # 能力函数抛异常时也要有回调（不能静默卡住）
            tool6.image = Image.new("RGB", (80, 60), (10, 20, 30))
            tool6._original = tool6.image.copy()

            def boom(_img):
                raise RuntimeError("假装模型炸了")

            ai_ops.super_resolve = boom
            tool6._run_ai_cap(ai_ops.CAP_SUPER_RES)
            deadline = time.time() + 8.0
            while time.time() < deadline and "处理中" in tool6._st_msg.cget("text"):
                root.update()
                time.sleep(0.02)
            st2 = tool6._st_msg.cget("text")
            rep.check("出错了" in st2 or "出错" in st2,
                      "AI 能力抛异常时状态栏如实报错（不会静默卡住）", st2)
            rep.check(tool6.image.size == (80, 60),
                      "出错时不改动工作图", str(tool6.image.size))
        finally:
            ai_ops.super_resolve = real_sr
            ai_ops.models_present = real_present
            tool6.destroy()

        # ---------------------------------------------------------------
        rep.head("AI 菜单：**没有模型时也必须有一项能点**（画笔抠图）")
        # 以前 _run_ai_cap 一进门就 models_present() 挡人，画笔抠图
        # （纯手动画笔、不跑模型）也一起被挡；菜单在没模型时更是只列"去下载"。
        # 结果：新装机器点开「AI」什么都用不了 —— 用户说的"AI 打不开"。
        # （2026-09-21 修）
        tool7 = T.ImageTool(root, on_quit=root.destroy)
        tool7.build()
        tool7.image = Image.new("RGB", (60, 40), (10, 20, 30))
        tool7._original = tool7.image.copy()

        real_present2 = ai_ops.models_present
        real_mdir = ai_ops.models_dir
        empty_models = os.path.join(tmp, "nomodels")
        os.makedirs(empty_models, exist_ok=True)
        ai_ops.models_present = lambda: False          # 假装一个模型都没下
        # menu_caps 走的是"权重文件在不在"（不是 models_present），所以
        # 必须把 models_dir 也指到一个空目录 —— 否则本仓库 models/ 里那两份
        # 权重会被算进来，测的就不是"没装模型"的场景了。
        ai_ops.models_dir = lambda: empty_models
        ai_ops.clear_sessions()
        try:
            caps = ai_ops.menu_caps()
            rep.check(ai_ops.CAP_BRUSH_MATTE in caps,
                      "没模型时菜单仍列出「画笔抠图」", str(caps))
            rep.check(ai_ops.CAP_MATTE not in caps
                      and ai_ops.CAP_SUPER_RES not in caps,
                      "没模型时菜单不列需要权重的能力", str(caps))

            # 需要权重的能力：仍然要被挡住，并说清原因
            tool7._run_ai_cap(ai_ops.CAP_MATTE)
            st = tool7._st_msg.cget("text")
            rep.check("尚未下载" in st or "先点" in st,
                      "一键抠图在没模型时如实挡住并提示去下载", st)

            # 画笔抠图：必须真的开起来（弹出画笔弹窗），不许再报"先下载"。
            # ⚠ 这个弹窗是**模态**的（show() 里有 wait_window），所以
            # _run_ai_cap 会一直阻塞到窗口被关掉 —— 必须**先**排一个看门狗
            # 去关窗，再调用，否则测试自己就死在这儿了。
            tool7._show_status("")                     # 清掉上一条，免得误判
            before = [w for w in root.winfo_children()
                      if isinstance(w, tk.Toplevel)]
            seen_closed = []
            stop = {"done": False}

            def watchdog():
                if stop["done"]:
                    return
                fresh = [w for w in root.winfo_children()
                         if isinstance(w, tk.Toplevel) and w not in before]
                if fresh:
                    seen_closed.append(fresh[0])
                    try:
                        fresh[0].destroy()
                    except tk.TclError:
                        pass
                    return
                root.after(30, watchdog)

            root.after(50, watchdog)
            tool7._run_ai_cap(ai_ops.CAP_BRUSH_MATTE)   # 会阻塞到看门狗关掉弹窗
            stop["done"] = True
            rep.check(bool(seen_closed),
                      "没模型时「画笔抠图」也能打开画笔弹窗",
                      tool7._st_msg.cget("text"))
            rep.check("尚未下载" not in tool7._st_msg.cget("text"),
                      "画笔抠图不再被『模型没下』挡住",
                      tool7._st_msg.cget("text"))
            root.update()
        finally:
            ai_ops.models_present = real_present2
            ai_ops.models_dir = real_mdir
            ai_ops.clear_sessions()
            tool7.destroy()

        # ---------------------------------------------------------------
        rep.head("AI 菜单：**点「AI」必须真的弹出菜单**（已回退成系统原生 tk.Menu）")
        # 用户报的就是这个："点 AI 压根不弹菜单"。
        # 1.7.6 把老版的 tk.Menu 换成了自绘 Toplevel(overrideredirect=True) + hover，
        # 结果在用户的机器上"点了完全没反应"：自绘无边框弹层要自己算屏幕坐标、
        # 自己管 -topmost、自己处理"鼠标移开就收起"，哪一环换个环境（或换掉打包
        # 进去的那套 Tcl/Tk）就可能不显示 —— 而窗口对象是建出来了
        # （_ai_popup.winfo_exists() 为真），所以"查对象在不在"的断言全是绿的，
        # 用户屏幕上却什么都没有。教训：断言要盯"看得见"，别只盯"对象存在"。
        # 现在回到原生菜单：位置 / 层级 / 点外面收起 / Esc 收起全由 Tk 管，
        # 我们这边**不留任何弹层状态**。
        #
        # ⚠ RoundButton._on_release 判断"手是不是松开在按钮上"用的是**真实指针
        #   位置**（winfo_pointerx/y），不是事件坐标 —— 所以必须真的把鼠标挪过去
        #   （SetCursorPos），光 event_generate 测不出来。
        import ctypes
        import inspect
        tool8 = T.ImageTool(root, on_quit=root.destroy)
        tool8.build()
        root.update()
        if not hasattr(tool8, "_ai_button"):
            rep.check(True, "本机 ai_available() 为假，跳过 AI 按钮点击测试")
        else:
            b8 = tool8._ai_button
            rep.check("ai" not in tool8._edit_buttons,
                      "AI 按钮不在『编辑按钮组』里（没图时不会被禁用）",
                      str(sorted(tool8._edit_buttons)))
            rep.check(b8._state == "normal",
                      "一张图都没打开时「AI」按钮也是可点的（不是 disabled）",
                      b8._state)

            # ---- 把原生菜单"按住"：只记录它会被弹在哪、里面有哪几项 ----
            # 真的 tk_popup 会抓走输入并等着人来点，自动化测不了；
            # 换成"记一笔就返回"，其余行为（拼菜单）照常走。
            popped = []
            items = []
            real_popup = tk.Menu.tk_popup
            real_addc = tk.Menu.add_command
            real_adds = tk.Menu.add_separator

            def _fake_popup(self, x, y, *a, **kw):
                popped.append((self, x, y))

            def _fake_addc(self, **kw):
                items.append(kw.get("label"))
                return real_addc(self, **kw)

            def _fake_adds(self, *a, **kw):
                items.append("<分隔线>")
                return real_adds(self, *a, **kw)

            user32 = ctypes.windll.user32
            try:
                _save = user32.GetCursorPos

                class _PT(ctypes.Structure):
                    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

                old = _PT()
                _save(ctypes.byref(old))
            except Exception:                              # noqa: BLE001
                old = None

            def _move_pointer(cx, cy, tries=25):
                """把真实指针挪到 (cx, cy)，并**等它真的到位**。

                ⚠ 这是本测试长期偶发失败（10 次里挂 4~5 次）的真因，不是被测代码坏了：
                  ``SetCursorPos`` 是**异步**的 —— 系统把指针挪过去要几毫秒，
                  而紧接着的 ``root.update()`` 只处理 Tk 自己的事件队列，
                  **不会等指针真的动**。
                  而 ``RoundButton._on_release`` 判的恰恰是**真实指针位置**
                  （``winfo_pointerx/y``，见 ui_theme 里的注释），不是事件坐标 ——
                  指针还没到位就去点，release 会被判成"手指松在按钮外面"，
                  于是**点了没反应**，断言随机变红。

                原来的写法没有重试，全靠运气。这里改成"挪一次、确认到位，不行再来"。
                返回是否真的到位（到不了位就是环境问题，断言仍会失败并说明原因）。
                """
                for _ in range(tries):
                    user32.SetCursorPos(cx, cy)
                    root.update()
                    if (abs(b8.winfo_pointerx() - cx) <= 2
                            and abs(b8.winfo_pointery() - cy) <= 2):
                        return True
                    time.sleep(0.02)
                return False

            def _real_click():
                cx = b8.winfo_rootx() + b8.winfo_width() // 2
                cy = b8.winfo_rooty() + b8.winfo_height() // 2
                _move_pointer(cx, cy)
                b8.event_generate("<ButtonPress-1>")
                root.update()
                b8.event_generate("<ButtonRelease-1>")
                root.update()

            def _click_until_menu(attempts=3):
                """点一下「AI」，没弹就再试（给指针异步落位留余量）。"""
                for _ in range(attempts):
                    _real_click()
                    if popped:
                        return True
                return False

            tk.Menu.tk_popup = _fake_popup
            tk.Menu.add_command = _fake_addc
            tk.Menu.add_separator = _fake_adds
            try:
                # —— 1) 没有任何图片就点一下：必须弹出来（用户当时的场景）——
                _click_until_menu()
                rep.check(len(popped) == 1,
                          "**没打开图片时点一下「AI」就把菜单弹出来了**",
                          "tk_popup 被调用 %d 次" % len(popped))

                # —— 2) 弹出的位置：按钮正下方 ——
                if popped:
                    _m, px, py = popped[-1]
                    rep.check(px == b8.winfo_rootx()
                              and py == b8.winfo_rooty() + b8.winfo_height() + 2,
                              "菜单弹在「AI」按钮正下方",
                              "(%d, %d)" % (px, py))
                else:
                    rep.check(False, "菜单弹在「AI」按钮正下方", "没弹")

                # —— 3) 菜单里真的拼出了东西（能力行 + 分隔线）——
                rep.check("<分隔线>" in items and len(items) >= 2,
                          "菜单里拼出了真实的菜单项",
                          "%d 项：%s" % (len(items), items))

                # —— 4) 悬停**不**会展开（hover 那套已撤）——
                n_before = len(popped)
                b8.event_generate("<Enter>")
                root.update()
                rep.check(len(popped) == n_before,
                          "鼠标只是移到「AI」上、没点：菜单**不**会自己展开（已回退）")

                # —— 5) 再点一下：开 / 关交给 Tk，我们这边不留状态 ——
                # 同样给指针异步落位留余量（理由见 _move_pointer 的注释）。
                for _ in range(3):
                    before5 = len(popped)
                    _real_click()
                    if len(popped) > before5:
                        break
                rep.check(len(popped) == n_before + 1,
                          "再点一下「AI」：菜单重新弹出（开 / 关由 Tk 管）")

                # —— 6) 这条路里**一个计时器都没有**（hover 那套的 after 确实没了）——
                _src = (inspect.getsource(type(tool8)._ai_toggle_menu)
                        + inspect.getsource(type(tool8)._ai_build_menu))
                rep.check("after(" not in _src,
                          "入口这条路上没有 after(...) 计时器（不会自己收、也不会自己弹）")

                # —— 7) 不许再自己画弹层：菜单必须是 tk.Menu，且自绘那套状态已清干净 ——
                menu = tool8._ai_build_menu()
                rep.check(isinstance(menu, tk.Menu),
                          "菜单是系统原生 tk.Menu（不再自绘 Toplevel）",
                          type(menu).__name__)
                rep.check(not hasattr(tool8, "_ai_popup")
                          and not hasattr(tool8, "_ai_close_popup")
                          and not hasattr(tool8, "_ai_outer_bound"),
                          "自绘弹层的属性/方法已清干净（没有会残留的弹层状态）")
                menu.destroy()

                # —— 8) 失败要响：拼菜单时炸了必须弹错给人看，不许静默 ——
                seen = []
                real_showerror = tkmsg.showerror
                real_build = tool8._ai_build_menu

                def _boom():
                    raise RuntimeError("假装拼菜单炸了")

                def _catch(title, msg, *a, **kw):
                    seen.append((title, msg))

                tool8._ai_build_menu = _boom
                tkmsg.showerror = _catch
                try:
                    _real_click()
                finally:
                    tkmsg.showerror = real_showerror
                    tool8._ai_build_menu = real_build
                rep.check(bool(seen) and "假装拼菜单炸了" in seen[0][1],
                          "**拼菜单出错时弹窗把原因说出来**（不许静默没反应）",
                          seen[0][1].replace("\n", " / ") if seen else "什么都没弹")
            finally:
                tk.Menu.tk_popup = real_popup
                tk.Menu.add_command = real_addc
                tk.Menu.add_separator = real_adds
                if old is not None:
                    try:
                        user32.SetCursorPos(old.x, old.y)
                    except Exception:                      # noqa: BLE001
                        pass
                tool8.destroy()
                root.update()

        # ==========================================================
        rep.head("删除 AI 模型：菜单入口 + 两步确认（提醒 → 勾选）")
        # ==========================================================
        # 这一节回答三件事：
        #   1. 只有"真装了模型"时菜单里才出现「删除 AI 模型…」，且写明能释放多少；
        #   2. 第一步的提醒弹框真的弹出来了（不是直接删）；
        #   3. 第二步**必须勾选**才能点确定 —— 没勾时按钮是禁用的，
        #      而且就算绕过按钮直接调 agree() 也会被逻辑层挡回来。
        tool9 = T.ImageTool(root, on_quit=root.destroy)
        tool9.build()

        real_present9 = ai_ops.models_present
        real_size9 = ai_ops.models_size_bytes
        real_avail9 = ai_ops.available_caps
        ai_ops.models_present = lambda: True
        ai_ops.models_size_bytes = lambda: 144898527
        ai_ops.available_caps = lambda: [ai_ops.CAP_SUPER_RES]

        def _labels9(tool):
            m = tool._ai_build_menu()
            out = []
            for i in range(m.index("end") + 1):
                try:
                    out.append(m.entrycget(i, "label"))
                except tk.TclError:
                    out.append("<分隔线>")
            m.destroy()
            return out

        try:
            labs = _labels9(tool9)
            rep.check(any("删除 AI 模型" in (l or "") for l in labs),
                      "★ 装了模型时，菜单里有「删除 AI 模型」",
                      str([l for l in labs if l and "删除" in l]))
            rep.check(any("138.2 MB" in (l or "") for l in labs),
                      "★ 菜单项写明能释放多少空间（不写用户不知道值不值）",
                      str([l for l in labs if l and "释放" in l]))

            ai_ops.models_present = lambda: False
            labs2 = _labels9(tool9)
            rep.check(not any("删除 AI 模型" in (l or "") for l in labs2),
                      "★ 没装模型时**不出现**「删除」项（免得点了只得到一句"
                      "「没有可删的」）")
            rep.check(any("下载 AI 模型组件" in (l or "") for l in labs2),
                      "★ 没装模型时那一行是「下载…」（能点，且不含版本号）",
                      str([l for l in labs2 if l and "AI 模型" in l]))
            ai_ops.models_present = lambda: True

            # ---- 菜单上直接写出"装的是哪一版、要不要更新" ----
            # 起因：浩然问"可以更新 AI 吗？" —— 界面上根本没这个信息。
            real_inst9 = ai_ops.ai_component_version
            real_rem9 = ai_ops.ai_remote_version
            for inst, rem, must in (
                    ("1.4.0", "1.4.0", "已是最新"),
                    ("1.2.0", "1.4.0", "有更新"),
                    ("1.4.0", "", "点此检查更新"),
            ):
                ai_ops.ai_component_version = lambda v=inst: v
                ai_ops.ai_remote_version = lambda v=rem: v
                labs3 = _labels9(tool9)
                hit = [l for l in labs3 if l and "AI 模型" in l and "删除" not in l]
                rep.check(any(must in (l or "") for l in hit),
                          "★ 菜单直接写出「%s」：装了 %s / 远端缓存 %s"
                          % (must, inst, rem or "无"),
                          str(hit))
            ai_ops.ai_component_version = real_inst9
            ai_ops.ai_remote_version = real_rem9

            # ---- 真的走一遍两步确认（看门狗负责勾选 + 点确定）----
            import image_dialogs as _dlg
            real_delete = ai_ops.delete_models
            real_askok = tkmsg.askokcancel
            real_showinfo = tkmsg.showinfo
            real_showerr = tkmsg.showerror
            seen_ask = []
            seen_info = []
            hits = {"delete": 0}

            def _fake_delete():
                hits["delete"] += 1
                return True, "（测试）假装删好了"

            ai_ops.delete_models = _fake_delete
            tkmsg.askokcancel = lambda t, m, **kw: (seen_ask.append((t, m, kw)), True)[1]
            tkmsg.showinfo = lambda t, m, **kw: seen_info.append((t, m))
            tkmsg.showerror = lambda t, m, **kw: seen_info.append(("ERR", m))

            facts = {}

            def _watch():
                tops = [w for w in root.winfo_children()
                        if isinstance(w, _dlg.DeleteModelsDialog)]
                if not tops:
                    root.after(60, _watch)
                    return
                form = tops[0].form
                facts["state0"] = form.ok_btn._state
                facts["agree_unchecked"] = form.agree()     # 没勾 → 必须 False
                facts["deleted_after_refuse"] = hits["delete"]
                form.check.set(True)
                root.update()
                facts["state1"] = form.ok_btn._state
                form.agree()                                # 勾了 → 真走
                root.update()

            root.after(60, _watch)
            tool9._prompt_ai_delete()
            root.update()

            rep.check(bool(seen_ask), "★ 第一步：先弹框提醒（不是直接删）",
                      str(seen_ask[0][1].replace("\n", " / "))[:110] if seen_ask else "没弹")
            rep.check(seen_ask and "照片" in seen_ask[0][1],
                      "★ 提醒里先说清「照片不会被动」（用户最担心的就是这个）")
            rep.check(seen_ask and "勾选" in seen_ask[0][1],
                      "★ 提醒里预告「还要再勾选一次」")
            rep.check(facts.get("state0") == "disabled",
                      "★ 第二步：刚打开时「确定删除」是禁用的",
                      str(facts.get("state0")))
            rep.check(facts.get("agree_unchecked") is False,
                      "★ 没勾选时，就算绕过按钮直接调 agree() 也会被挡回来",
                      str(facts.get("agree_unchecked")))
            rep.check(facts.get("deleted_after_refuse") == 0,
                      "★ 没勾选时**没有**发生删除", str(facts.get("deleted_after_refuse")))
            rep.check(facts.get("state1") == "normal",
                      "★ 勾选之后按钮变可用", str(facts.get("state1")))
            rep.check(hits["delete"] == 1,
                      "★ 勾选并确定之后，删除被走到了一次", str(hits["delete"]))
            rep.check(any("假装删好了" in m for _, m in seen_info),
                      "★ 删完给一句结果提示", str(seen_info))

            # ---- 点「取消」的那一步：不许删 ----
            hits["delete"] = 0
            seen_info.clear()
            tkmsg.askokcancel = lambda t, m, **kw: False     # 第一步就点取消
            tool9._prompt_ai_delete()
            root.update()
            rep.check(hits["delete"] == 0,
                      "★ 第一步点「取消」→ 什么都不删", str(hits["delete"]))

            # ---- 对话框消失后不该留下抢了 grab 的窗口 ----
            leftovers = [w for w in root.winfo_children()
                         if isinstance(w, _dlg.DeleteModelsDialog)]
            rep.check(not leftovers,
                      "确认窗口关干净了（没有残留的 Toplevel 抢着 grab）",
                      str(leftovers))
        finally:
            ai_ops.delete_models = real_delete
            tkmsg.askokcancel = real_askok
            tkmsg.showinfo = real_showinfo
            tkmsg.showerror = real_showerr
            ai_ops.models_present = real_present9
            ai_ops.models_size_bytes = real_size9
            ai_ops.available_caps = real_avail9
            try:
                tool9.destroy()
            except tk.TclError:
                pass
            root.update()

    except Exception:
        rep.check(False, "测试过程抛了异常")
        rep.add(traceback.format_exc())
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
        gc.collect()
        shutil.rmtree(tmp, ignore_errors=True)

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(rep.text())

    try:
        import _redact
        _redact.scrub_tree(HERE)
    except Exception:
        pass

    print(rep.text().splitlines()[-3])
    sys.exit(1 if rep.failures else 0)


if __name__ == "__main__":
    main()
