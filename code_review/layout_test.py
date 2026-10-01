# -*- coding: utf-8 -*-
"""布局溢出检测：量化验证"每个控件都完整可见、没有被裁切"。

为什么需要这个测试
==================================================================
"按钮只有一半"这类问题靠肉眼看是不可靠的（换台机器、换个缩放比例就复现不了），
所以这里把它变成可量化的断言：

    对每个页面、在 100% / 125% / 150% 三种显示缩放下，
    遍历所有已显示的控件，检查它的右下边界是否落在窗口可视区内。

tkinter 的 `tk scaling` 表示"每点多少像素"，对应关系：
    100% (96 dpi)   -> 1.333
    125% (120 dpi)  -> 1.667
    150% (144 dpi)  -> 2.000
字体以 point 为单位，因此缩放越大字体越大、需要的垂直空间越多，
也越容易把底部控件挤出窗口 —— 这正是原实现 680x500 写死尺寸下的故障现场。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "code_review", "optimized"))

import tkinter as tk  # noqa: E402

from PIL import Image as PILImage  # noqa: E402

import image_tool  # noqa: E402
import installer  # noqa: E402
import main as prank  # noqa: E402
import state_store  # noqa: E402

SCALINGS = (("100%", 1.333), ("125%", 1.667), ("150%", 2.0))
TOLERANCE = 2  # 容许 2px 的取整误差


def walk(widget):
    """深度优先遍历所有子控件。"""
    for child in widget.winfo_children():
        yield child
        for sub in walk(child):
            yield sub


def find_overflow(root):
    """返回越界控件列表 [(控件, 名称, 右/下越界量)]。"""
    root.update_idletasks()
    root.update()

    rw, rh = root.winfo_width(), root.winfo_height()
    ox, oy = root.winfo_rootx(), root.winfo_rooty()
    problems = []

    for w in walk(root):
        if not w.winfo_ismapped():
            continue
        ww, wh = w.winfo_width(), w.winfo_height()
        if ww <= 1 or wh <= 1:      # 未完成布局的占位控件
            continue
        x = w.winfo_rootx() - ox
        y = w.winfo_rooty() - oy
        over_right = (x + ww) - rw
        over_bottom = (y + wh) - rh

        if over_right > TOLERANCE or over_bottom > TOLERANCE:
            name = "%s" % w.winfo_class()
            try:
                txt = w.cget("text")
                if txt:
                    name += "(%s)" % str(txt).split("\n")[0][:26]
            except tk.TclError:
                pass
            problems.append((name, over_right, over_bottom, x, y, ww, wh))
    return rw, rh, problems


def measure(root, builder, label, lines):
    """构建页面 -> 自适应 -> 检查溢出。"""
    for child in root.winfo_children():
        child.destroy()
    builder()
    if hasattr(root, "_fit_owner"):
        root._fit_owner._fit_window()
    root.update_idletasks()
    root.update()

    rw, rh, problems = find_overflow(root)
    status = "OK" if not problems else "裁切 %d 个" % len(problems)
    lines.append("    %-22s 窗口 %4dx%-4d  %s" % (label, rw, rh, status))
    for name, orr, ob, x, y, ww, wh in problems:
        lines.append(
            "        ! %-40s 位于(%d,%d) 尺寸%dx%d  右越界%d  下越界%d"
            % (name, x, y, ww, wh, max(0, orr), max(0, ob))
        )
    return problems


def measure_dialog(screen_root, top, label, lines, line_sink):
    """对话框：它不参与"窗口自适应"，所以改判"会不会比屏幕还高"。

    对对话框用 find_overflow 是没意义的（几何尺寸就是按需求尺寸设的，
    必然不越界，等于什么都没验）。真正会出问题的是 150% 缩放下弹框比屏幕还高，
    底部的"确定/取消"就被顶到屏幕外面去了 —— 那才是用户真的会遇到的。
    """
    top.update_idletasks()
    top.update()
    w, h = top.winfo_reqwidth(), top.winfo_reqheight()
    sw, sh = screen_root.winfo_screenwidth(), screen_root.winfo_screenheight()
    ok = w <= sw and h <= sh - 120
    line_sink.append("    %-22s 需求 %4dx%-4d  屏 %dx%d  %s"
                     % (label, w, h, sw, sh, "OK" if ok else "比屏幕还高!"))
    return [] if ok else [("对话框 %s 过高/过宽" % label, w, h, 0, 0, w, h)]


def main():
    lines = []
    all_problems = []

    lines.append("=" * 78)
    lines.append("布局溢出检测（控件是否完整可见）")
    lines.append("=" * 78)

    # ---------------- 安装器 ----------------
    lines.append("")
    lines.append("[1] Setup 安装向导")

    bundle = installer.Bundle(os.path.join(ROOT, "payload.zip"))
    iroot = tk.Tk()
    iroot.title("layout-probe-installer")
    inst = installer.Installer(iroot, bundle)
    iroot._fit_owner = inst

    pages = (
        ("步骤1 欢迎/条款", inst._build_welcome),
        ("步骤2 安装位置", inst._build_path),
        ("步骤3 组件", inst._build_components),
        ("步骤4 安装中", inst._build_progress_only),
        ("完成页（含启动选项）", inst._build_finish),
    )

    for scale_name, scale in SCALINGS:
        lines.append("  -- 显示缩放 %s (tk scaling=%.3f) --" % (scale_name, scale))
        iroot.tk.call("tk", "scaling", scale)
        for label, builder in pages:
            inst._min_w, inst._min_h = installer.MIN_WINDOW_W, installer.MIN_WINDOW_H
            probs = measure(iroot, builder, label, lines)
            if probs:
                all_problems.append(("安装器/%s/%s" % (scale_name, label), probs))

    iroot.destroy()

    # ---------------- 整蛊主程序 ----------------
    lines.append("")
    lines.append("[2] MyAlbum 主程序：问答页 / 锁定页 / 图片工具 / 关机页 / 揭晓页")
    proot = tk.Tk()
    proot.title("layout-probe-main")

    # 状态写进临时目录：布局测试不该碰用户真正的 state.json
    #（它也绝不该把"本月已过闸"这件事写脏 —— 那会让下次启动不再出题）
    tmp_state = tempfile.mkdtemp(prefix="haavik_layout_")
    state_store.set_state_dir_for_tests(tmp_state)
    probe_state = state_store.empty_state()

    # check_updates=False：布局测试不该打网络，也不该在测量期间往窗口里填文字
    app = prank.App(
        proot, real_shutdown=False, seconds=3600, show_image=False,
        check_updates=False, state=probe_state,
    )
    proot._fit_owner = app
    app.countdown = None  # 防止 _tick 启动倒计时逻辑

    def build_tool_page():
        app.mode = "tool"
        app.tool = image_tool.ImageTool(proot, on_quit=proot.destroy)
        app.tool.build()

    pages = (
        ("问答页（3 个选项）", app._build_quiz_page),
        ("锁定页（序列号输入框）", app._build_lock_page),
        ("图片工具页", build_tool_page),
        ("倒计时页（取消按钮）", app._build_warning_page),
        ("揭晓页", app._show_reveal),
    )

    for scale_name, scale in SCALINGS:
        lines.append("  -- 显示缩放 %s (tk scaling=%.3f) --" % (scale_name, scale))
        proot.tk.call("tk", "scaling", scale)

        for label, builder in pages:
            app._min_w, app._min_h = prank.MIN_WINDOW_W, prank.MIN_WINDOW_H
            if app.tool is not None:
                app.tool._min_w, app.tool._min_h = image_tool.MIN_WINDOW_W, image_tool.MIN_WINDOW_H
            probs = measure(proot, builder, label, lines)
            if probs:
                all_problems.append(("主程序/%s/%s" % (scale_name, label), probs))

        # 这几个对话框比屏幕还高/宽的话，底部的确定/取消按钮会被顶到屏幕外。
        # 叠加窗口是最宽的那个（预览画布 700px + 三条滑块），必须在这里过一遍。
        for label, builder in (
            ("对话框：修改尺寸", lambda top: app.tool._build_resize_body(top)),
            ("对话框：转换格式", lambda top: app.tool._build_convert_body(top)),
            ("对话框：叠加图片", lambda top: app.tool._build_overlay_body(top)),
            ("对话框：选择性抠图", lambda top: app.tool._build_matte_brush_body(top)),
            # 这两个是**文字最多**的窗口（隐私说明六条 + 反馈输入框），
            # 150% 缩放下最容易长到屏幕外 —— 必须在这里过一遍。
            ("对话框：隐私说明", lambda top: app._build_consent_body(top)),
            ("对话框：反馈", lambda top: app._build_feedback_body(top)),
        ):
            app.mode = "tool"
            if app.tool is None:
                app.tool = image_tool.ImageTool(proot, on_quit=proot.destroy)
            app.tool.image = PILImage.new("RGB", (1600, 1200), (30, 30, 30))
            top = tk.Toplevel(proot)
            top.title("layout-probe-dialog")
            builder(top)
            probs = measure_dialog(proot, top, label, lines, lines)
            top.destroy()
            if probs:
                all_problems.append(("主程序/%s/%s" % (scale_name, label), probs))

    proot.destroy()
    shutil.rmtree(tmp_state, ignore_errors=True)

    lines.append("")
    lines.append("=" * 78)
    if all_problems:
        lines.append("结论：发现 %d 处控件被裁切 ✗" % sum(len(p) for _, p in all_problems))
        for where, probs in all_problems:
            lines.append("  %s：%d 个" % (where, len(probs)))
    else:
        lines.append("结论：全部页面在 100%/125%/150% 缩放下均无控件被裁切 ✓")
    lines.append("=" * 78)

    report = "\n".join(lines)
    # 输出写到 code_review/ 而不是仓库根目录 —— 根目录只留 README/CHANGELOG/.gitignore
    with open(os.path.join(HERE, "_layout.txt"), "w", encoding="utf-8") as fh:
        fh.write(report)
    # 生成物要进仓库：本机绝对路径一律换成占位符（硬性约定 12）
    try:
        import _redact
        _redact.scrub_tree(HERE)
    except ImportError:
        pass
    print(report)
    return 1 if all_problems else 0


if __name__ == "__main__":
    sys.exit(main())
