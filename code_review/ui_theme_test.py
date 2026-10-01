# -*- coding: utf-8 -*-
"""``ui_theme.py`` 的冒烟测试：自绘控件层能不能真的画出来。

为什么需要它
==================================================================
``ui_theme`` 是一个"纯画图"模块 —— 它的 bug 不会报错，只会**画错**：
圆角少一段、图标少一根线、按钮宽度算错导致文字被裁。这类问题肉眼很难在
一整个界面里发现，但它决定了"这个工具看起来像不像正经软件"。

所以这里做三件事：

1. **图标名清单从源码 AST 里提取**，不在这里抄第二份。
   抄一份的后果是：源码里加了图标、测试里没加，于是新图标从来没被画过；
   或者源码里改名、测试还测旧名。清单只有一个来源。
2. 逐个画每个图标，断言**画出来了东西**（items 非空）。
   ``draw_icon`` 对未知名字会画红色问号 —— 所以还要额外断言"没有出现问号"。
3. 在 100% / 125% / 150% 三种 UI 缩放下各建一套控件，
   断言按钮实际宽度 >= 按字体量出来的自然宽度（即：文字没被裁掉）。

输出写 ``code_review/_ui_theme.txt``（PowerShell 收不到 stdout），
退出码非 0 = 有断言不成立。
"""
from __future__ import annotations

import ast
import gc
import io
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
OPTIMIZED = os.path.join(HERE, "optimized")
OUT = os.path.join(HERE, "_ui_theme.txt")

if OPTIMIZED not in sys.path:
    sys.path.insert(0, OPTIMIZED)

#: 期望的图标数下限。源码里加了图标不用改这里；掉了图标才会撞上。
MIN_ICONS = 28

#: 三种显示缩放（Windows 的 100% / 125% / 150%）。
SCALES = (1.0, 1.25, 1.5)


class Report:
    def __init__(self):
        self.lines = []
        self.failures = []

    def add(self, text=""):
        self.lines.append(text)

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
        head = ["=" * 68,
                "ui_theme.py 冒烟测试",
                "=" * 68,
                ""]
        if self.failures:
            tail = ["", "=" * 68,
                    "FAILED: %d 项不成立" % len(self.failures)]
            tail += ["  * " + f for f in self.failures]
            tail.append("=" * 68)
        else:
            tail = ["", "=" * 68, "RESULT: OK", "=" * 68]
        return "\n".join(head + self.lines + tail) + "\n"


def icon_names_from_source():
    """从 ``ui_theme.py`` 的 ``draw_icon`` 里抠出所有图标名。"""
    path = os.path.join(OPTIMIZED, "ui_theme.py")
    with io.open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)
    names = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Name)
                and node.left.id == "name"):
            for comp in node.comparators:
                if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                    names.add(comp.value)
    return sorted(names)


def main():
    rep = Report()
    names = icon_names_from_source()
    rep.add("从源码提取到的图标名（%d 个）：" % len(names))
    rep.add("  " + " ".join(names))
    rep.add("")
    rep.check(len(names) >= MIN_ICONS, "图标数量 >= %d" % MIN_ICONS,
              "实际 %d" % len(names))
    rep.add("")

    import tkinter as tk
    import ui_theme as T

    root = tk.Tk()
    root.title("ui_theme probe")
    root.geometry("960x560")
    root.configure(bg=T.BG_APP)
    root.update_idletasks()

    try:
        # ---- 1. 几何原语 ----
        rep.add("-" * 68)
        rep.add("几何原语")
        rep.add("-" * 68)
        pts = T.round_rect_points(0, 0, 100, 60, 10)
        rep.check(len(pts) == 4 * 9 * 2, "圆角矩形顶点数 = 4*(steps+1)*2",
                  "实际 %d" % len(pts))
        flat = T.round_rect_points(0, 0, 100, 60, 0)
        rep.check(flat == [0, 0, 100, 0, 100, 60, 0, 60],
                  "r=0 退化成直角矩形", "实际 %r" % (flat,))
        clamped = T.round_rect_points(0, 0, 100, 40, 9999)
        xs = clamped[0::2]
        ys = clamped[1::2]
        rep.check(min(xs) >= -0.01 and max(xs) <= 100.01
                  and min(ys) >= -0.01 and max(ys) <= 40.01,
                  "半径超过半高时被夹紧（不画出格）",
                  "x[%.1f,%.1f] y[%.1f,%.1f]" % (min(xs), max(xs), min(ys), max(ys)))
        rep.add("")

        # ---- 2. 图标 ----
        rep.add("-" * 68)
        rep.add("矢量图标（逐个真的画一遍）")
        rep.add("-" * 68)
        bad = []
        for name in names:
            cv = tk.Canvas(root, width=64, height=64, bg=T.BG_APP,
                           highlightthickness=0, bd=0)
            cv.pack()
            try:
                items = T.draw_icon(cv, name, 32, 32, 13, T.FG_TEXT)
            except Exception as exc:                        # noqa: BLE001
                bad.append("%s 抛异常 %s: %s" % (name, type(exc).__name__, exc))
                cv.destroy()
                continue
            if not items:
                bad.append("%s 什么都没画" % name)
            else:
                # 靠 tag 判定有没有走到"未知图标"分支。
                # 早期版本用"图元个数 == 4"来猜，结果 wallpaper 被误报 ——
                # 数图元个数本来就不是可靠信号（image 也是 3 个图元）。
                tags = set()
                for it in items:
                    tags.update(cv.gettags(it))
                if "icon-unknown" in tags:
                    bad.append("%s 走了未知图标分支（红色问号）" % name)
            cv.destroy()
        rep.check(not bad, "全部 %d 个图标都画出了内容、且没有走占位分支" % len(names),
                  "; ".join(bad))

        cv = tk.Canvas(root, width=64, height=64, bg=T.BG_APP,
                       highlightthickness=0, bd=0)
        cv.pack()
        unknown = T.draw_icon(cv, "definitely_not_an_icon", 32, 32, 13, T.FG_TEXT)
        utags = set()
        for it in unknown:
            utags.update(cv.gettags(it))
        rep.check(bool(unknown) and "icon-unknown" in utags,
                  "未知图标名画红色问号 + 打上 icon-unknown 标记",
                  "items=%d tags=%s" % (len(unknown), sorted(utags)))
        cv.destroy()
        rep.add("")

        # ---- 3. 三种缩放下的控件 ----
        # 每轮**销毁上一轮的容器**：控件在 150% 缩放下会宽出 20%，
        # 攒在一起会把根窗口挤爆，后面的控件只剩 1px —— 那不是缺陷，
        # 是测试自己把地方占满了。
        root.geometry("1280x800")
        for scale in SCALES:
            rep.add("-" * 68)
            rep.add("控件 @ %d%% 缩放" % int(scale * 100))
            rep.add("-" * 68)
            root.tk.call("tk", "scaling", scale)

            holder = tk.Frame(root, bg=T.BG_APP)
            holder.pack(fill="x")
            row_btn = tk.Frame(holder, bg=T.BG_APP)
            row_btn.pack(fill="x")
            row_sld = tk.Frame(holder, bg=T.BG_APP)
            row_sld.pack(fill="x")
            row_pan = tk.Frame(holder, bg=T.BG_APP)
            row_pan.pack(fill="x")

            built = []
            for kind in ("ghost", "accent", "solid", "danger"):
                b = T.RoundButton(row_btn, "转换格式", None, icon="convert",
                                  kind=kind, height=34)
                b.pack(side="left", padx=4, pady=4)
                built.append((kind, b))
            icon_btn = T.IconButton(row_btn, "zoom_in", None, size=34, tooltip="放大")
            icon_btn.pack(side="left", padx=4, pady=4)

            panel = T.RoundFrame(row_pan, radius=T.RADIUS_LG)
            panel.pack(side="left", padx=4, pady=4)
            tk.Label(panel.body, text="面板", bg=T.BG_ELEV, fg=T.FG_TEXT).pack()

            root.update()
            rep.check(icon_btn.winfo_width() >= icon_btn.winfo_reqheight() - 1,
                      "图标按钮是方的",
                      "%dx%d" % (icon_btn.winfo_width(), icon_btn.winfo_height()))

            for kind, b in built:
                natural = b._natural_width()
                rep.check(b.winfo_width() >= natural,
                          "%s 按钮宽度 >= 自然宽度" % kind,
                          "实际 %d, 需要 %d" % (b.winfo_width(), natural))
                rep.check(len(b.find_all()) > 0, "%s 按钮真的画了东西" % kind,
                          "items=%d" % len(b.find_all()))

            # 三态重绘不抛异常，且都画了东西
            for mode, fn in (("hover", built[0][1]._on_enter),
                             ("press", built[0][1]._on_press),
                             ("idle", built[0][1]._on_leave)):
                fn()
                root.update_idletasks()
                rep.check(len(built[0][1].find_all()) > 0,
                          "ghost 按钮 %s 态重绘" % mode)

            # 禁用态：不许触发命令
            fired = []
            b0 = built[0][1]
            b0.set_command(lambda: fired.append(1))
            b0.set_state("disabled")
            b0._invoke()
            root.update_idletasks()
            rep.check(not fired, "禁用态点击不触发命令")
            rep.check(len(b0.find_all()) > 0, "禁用态仍然画得出来")
            b0.set_state("normal")
            b0._invoke()
            rep.check(fired == [1], "恢复后点击能触发命令")

            # 滑块
            seen = []
            sld = T.Slider(row_sld, 0, 100, value=20, width=220,
                           command=seen.append)
            sld.pack(side="left", padx=4, pady=4)
            root.update()
            rep.check(sld.winfo_width() > 1, "滑块拿到了实际宽度",
                      "w=%d" % sld.winfo_width())
            sld.set(70)
            root.update_idletasks()
            rep.check(seen and abs(seen[-1] - 70) < 1e-6,
                      "滑块 set(70) 回调收到 70", "实际 %r" % (seen[-1:],))
            rep.check(abs(sld.get() - 70) < 1e-6, "滑块 get() 返回 70")
            sld.set(500)
            rep.check(sld.get() == 100, "滑块超范围被夹到上限",
                      "实际 %r" % sld.get())
            rep.check(len(sld.find_all()) > 0, "滑块真的画了东西",
                      "items=%d" % len(sld.find_all()))
            rep.add("")

            holder.destroy()
            root.update()

        # ---- 4. 面板 & 标题栏 ----
        rep.add("-" * 68)
        rep.add("圆角面板 / 深色标题栏")
        rep.add("-" * 68)
        pf = T.RoundFrame(root, radius=T.RADIUS_LG, pad=14)
        pf.pack(fill="both", expand=True, padx=10, pady=10)
        tk.Label(pf.body, text="hello", bg=T.BG_ELEV, fg=T.FG_TEXT).pack()
        root.update()
        rep.check(len(pf.find_all()) > 0, "圆角面板画了底")
        rep.check(pf.body.winfo_width() > 1, "面板内容区被撑开",
                  "body %dx%d" % (pf.body.winfo_width(), pf.body.winfo_height()))

        ok = T.dark_titlebar(root)
        root.update_idletasks()
        rep.check(ok, "深色标题栏设置成功（Win10 1809+）",
                  "返回值 %r" % ok)
        rep.check(True, "dark_titlebar 不抛异常")
        rep.add("")

        # ================================================================
        rep.add("-" * 60)
        rep.add("RoundEntry 占位提示 —— 曾经坏过，而且会**静默吞掉用户输入**")
        rep.add("-" * 60)
        # 这个参数全项目没人用过，所以坏了很久没人发现（2026-09-26 拆掉）。
        # 两个缺陷叠在一起才致命：
        #   ① 提示文字是**当真值写进变量**的 → 打字会拼在提示后面；
        #   ② `get()` 在占位状态下**一律返回空串**，而打字并**不会**关掉那个状态
        #      → "用户明明填了，程序却当没填"，**一声不响**。
        box = tk.Toplevel(root)
        box.configure(bg=T.BG_ELEV)
        ph = T.RoundEntry(box, placeholder="you@example.com", width=180)
        ph.pack()
        root.update()
        rep.check(ph.var.get() == "you@example.com",
                  "没填时变量里是提示文字（这是它的实现方式）")
        rep.check(ph.get() == "",
                  "★ 但 get() 返回空串 —— 提示不算值", repr(ph.get()))

        # 聚焦 = 用户要打字了 → 提示必须先清掉
        ph.focus_get = lambda: ph.entry          # 模拟"焦点在框里"
        ph._on_focus()
        root.update()
        rep.check(ph.var.get() == "" and ph.get() == "",
                  "★ 一聚焦就把提示清掉（否则第一个字符会拼在提示后面）",
                  repr(ph.var.get()))

        # 打字 → 必须取得到
        ph.entry.insert("end", "me@qq.com")
        root.update()
        rep.check(ph.get() == "me@qq.com",
                  "★★ 打字之后 get() 必须拿得到（原来这里会被静默吞掉）",
                  repr(ph.get()))

        # 失焦且为空 → 提示放回来
        ph.set("")
        ph.focus_get = lambda: None              # 模拟"焦点跑掉了"
        ph._on_focus()
        root.update()
        rep.check(ph.var.get() == "you@example.com",
                  "★ 失焦且为空 → 提示放回来", repr(ph.var.get()))
        rep.check(ph.get() == "",
                  "★ 提示回来了，get() 仍然是空串（提示永远不算值）")

        # 自愈：绕过 set() 直接改变量（写测试/以后加"程序填值"时很容易这么写）
        ph.var.set("abc@x.com")
        root.update()
        rep.check(ph.get() == "abc@x.com",
                  "★★ 绕过 set() 直接改变量 → get() 也不能吞掉它",
                  repr(ph.get()))
        rep.check(ph._show_ph is False,
                  "★ 自愈时把占位状态也纠正过来（不只是「这次返回对」）")

        # 正规入口
        ph.set("x@y.com")
        rep.check(ph.get() == "x@y.com", "set()/get() 往返正常")
        ph.set(None)
        rep.check(ph.get() == "", "set(None) 等于清空")

        # 没配 placeholder 的框（绝大多数调用点都是这种）行为必须干净
        plain = T.RoundEntry(box, width=120)
        plain.pack()
        root.update()
        rep.check(plain.var.get() == "" and plain.get() == "",
                  "没配提示的框：一开始就是空的")
        plain.entry.insert("end", "abc")
        root.update()
        rep.check(plain.get() == "abc",
                  "没配提示的框打字正常", repr(plain.get()))
        plain.focus_get = lambda: None
        plain._on_focus()
        root.update()
        rep.check(plain.get() == "abc",
                  "★ 没配提示的框失焦不会乱动内容", repr(plain.get()))
        box.destroy()
        root.update()
        rep.add("")

    except Exception:                                        # noqa: BLE001
        rep.add("!! 测试过程中抛异常：")
        rep.add(traceback.format_exc())
        rep.check(False, "测试全过程无异常")
    finally:
        # Tcl_AsyncDelete 是真实存在的坑：tkinter 对象若在引用环里被**别的线程**
        # 的 gc 回收，会在错误的线程释放 Tcl，进程直接 abort（rc 负数、零输出）。
        # 收尾一律回到主线程 destroy + collect。
        try:
            root.destroy()
        except Exception:                                    # noqa: BLE001
            pass
        gc.collect()

    text = rep.text()
    with io.open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    # 生成物要进仓库：本机绝对路径一律换成占位符（硬性约定 12）
    try:
        import _redact
        _redact.scrub_tree(os.path.dirname(os.path.abspath(__file__)))
    except ImportError:
        pass
    print(text)
    return 1 if rep.failures else 0


if __name__ == "__main__":
    sys.exit(main())
