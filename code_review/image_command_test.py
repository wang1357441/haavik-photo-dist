# -*- coding: utf-8 -*-
"""``image_command.py``（一句话改图·粗糙版）的验收测试。

这一支盯的就一件事：**它到底听不听得懂人话，以及听不懂时会不会乱猜。**

为什么把"听懂人话"单独测：它是个纯函数（``parse_command``），
将来换成真模型时**要换的就是它** —— 所以这张"认识哪些说法"的表必须被钉住，
换内核的时候才能一眼看出"哪些原来认识的，现在不认识了"。

⚠ 两条硬要求（写在断言里）：
  * **不认识就返回 None**，绝不猜 —— 猜错的代价是"它偷偷改了你的图"；
  * **顺序不能乱**（"变清晰"是超分、"清楚一点"是锐化、"去背景"是抠图）——
    中文里这些说法互相咬得很紧，顺序错一个就会静默做错事。
"""
from __future__ import annotations

import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
OPTIMIZED = os.path.join(HERE, "optimized")
OUT = os.path.join(HERE, "_image_command.txt")
if OPTIMIZED not in sys.path:
    sys.path.insert(0, OPTIMIZED)
os.environ.setdefault("LOCALAPPDATA", os.path.join(os.path.expanduser("~"),
                                                  "AppData", "Local"))


class Report:
    def __init__(self):
        self.lines = []
        self.failures = []

    def add(self, text=""):
        self.lines.append(text)

    def head(self, title):
        self.lines += ["", "-" * 68, title, "-" * 68]

    def check(self, label, cond, extra=""):
        if not isinstance(label, str):
            raise TypeError("check() 参数写反了：应为 check(说明, 条件)")
        mark = "  OK  " if cond else " FAIL "
        self.lines.append("[%s] %s%s" % (mark, label, ("  <- " + extra) if extra else ""))
        if not cond:
            self.failures.append(label + (" (" + extra + ")" if extra else ""))
        return bool(cond)

    def text(self):
        head = ["=" * 68, "一句话改图 · 解析与落盘 回归", "=" * 68]
        tail = ["", "=" * 68]
        if self.failures:
            tail += ["FAILED: %d 项不成立" % len(self.failures)]
            tail += ["  * " + f for f in self.failures]
        else:
            tail += ["RESULT: OK"]
        tail.append("=" * 68)
        return "\n".join(head + self.lines + tail) + "\n"


def main():
    rep = Report()
    import tkinter as tk
    from PIL import Image
    import ai_ops as A
    import image_command as C
    import image_tool as T

    try:
        # ============================================================
        rep.head("A. 认识的说法 → 该做的动作")
        # ============================================================
        CASES = [
            ("转一下", "rotate_cw"), ("顺时针转", "rotate_cw"),
            ("左转", "rotate_ccw"), ("逆时针旋转", "rotate_ccw"),
            ("水平翻转", "flip_h"), ("镜像一下", "flip_h"),
            ("垂直翻转", "flip_v"), ("上下翻一下", "flip_v"),
            ("变黑白", "grayscale"), ("弄成灰度", "grayscale"),
            ("调亮一点", "adjust"), ("提亮", "adjust"),
            ("调暗 30", "adjust"), ("加对比", "adjust"),
            ("鲜艳一点", "adjust"), ("褪色", "adjust"),
            ("锐化", "adjust"), ("裁剪一下", "crop"),
            ("把背景去掉", "ai_cap"), ("抠图", "ai_cap"),
            ("变清晰", "ai_cap"), ("超分", "ai_cap"),
            ("老照片上色", "ai_cap"), ("把黑白照片变彩色", "ai_cap"),
            ("放大两倍", "resize_pct"), ("缩小一半", "resize_pct"),
            ("撤销", "undo"), ("退回去", "undo"), ("重做", "redo"),
        ]
        bad = []
        for text, want in CASES:
            got = C.parse_command(text)
            if got is None or got.kind != want:
                bad.append("%r→%s(想要 %s)" % (text, got.kind if got else None, want))
        rep.check("**%d 句常用说法全部解析正确**" % len(CASES), not bad, "; ".join(bad))

        # 带参数的要单独看数值
        c = C.parse_command("调亮 30")
        rep.check("「调亮 30」= 亮度 ×1.3", c is not None
                  and abs(c.params["brightness"] - 1.3) < 1e-6,
                  str(c.params if c else None))
        c = C.parse_command("调亮一点")
        rep.check("没说数字时用默认幅度（×1.2）", c is not None
                  and abs(c.params["brightness"] - 1.2) < 1e-6,
                  str(c.params if c else None))
        c = C.parse_command("放大两倍")
        rep.check("「放大两倍」= 200%", c is not None
                  and abs(c.params["pct"] - 200) < 1e-6,
                  str(c.params if c else None))
        c = C.parse_command("缩小一半")
        rep.check("「缩小一半」= 50%", c is not None
                  and abs(c.params["pct"] - 50) < 1e-6,
                  str(c.params if c else None))
        c = C.parse_command("调暗一点")
        rep.check("「调暗」是往小里改", c is not None
                  and c.params["brightness"] < 1.0,
                  str(c.params if c else None))

        # ============================================================
        rep.head("A2. **旋转角度必须是真角度**（2026-09-24 修的真 bug）")
        # ============================================================
        # 症状：说"旋转180度"过去一律返回"右转 90°" —— 用户要转半圈，
        # 程序只转了四分之一圈。比"没听懂"更坏：它**悄悄改错了图**。
        ANG = [
            ("旋转180度", "rotate_180", None), ("转180度", "rotate_180", None),
            ("转半圈", "rotate_180", None),
            ("旋转90度", "rotate_cw", None), ("顺时针转90", "rotate_cw", None),
            # 方向词**必须**在给了角度时也生效：说"左转90度"绝不能往右转
            ("左转90度", "rotate_ccw", None), ("逆时针转90", "rotate_ccw", None),
            ("旋转270度", "rotate_ccw", None),
            # 任意角走 rotate_any，且负号表示逆时针
            ("旋转45度", "rotate_any", 45.0), ("斜一点15度", "rotate_any", 15.0),
            ("往左斜30度", "rotate_any", -30.0),
            # 没给角度时仍按方向词（老行为不能坏）
            ("转一下", "rotate_cw", None), ("左转", "rotate_ccw", None),
        ]
        ang_bad = []
        for text, want_kind, want_deg in ANG:
            got = C.parse_command(text)
            if got is None or got.kind != want_kind:
                ang_bad.append("%r→%s(想要 %s)" % (
                    text, got.kind if got else None, want_kind))
            elif want_deg is not None and abs(
                    float(got.params.get("deg", 0)) - want_deg) > 1e-6:
                ang_bad.append("%r→%s度(想要 %s度)" % (
                    text, got.params.get("deg"), want_deg))
        rep.check("**%d 种旋转说法都转对了角度**" % len(ANG), not ang_bad,
                  "; ".join(ang_bad))
        rep.check("「旋转0度」没意义 → 不动图（返回 None）",
                  C.parse_command("旋转0度") is None,
                  str(C.parse_command("旋转0度")))
        rep.check("**「转格式」在加了角度支持后仍是 None**（没被'转'字抢走）",
                  C.parse_command("转格式") is None
                  and C.parse_command("转格式30度") is None,
                  str(C.parse_command("转格式30度")))

        # ============================================================
        rep.head("A3. 扩过的说法（亮度 / 色彩 方向不能反）")
        # ============================================================
        # ⚠ 这一组的重点只有一个：**方向**。说"太暗"必须**调亮**，
        #   说"过曝"必须**调暗**。方向反了就是把图越修越糟。
        #   brightness/color 的值都是**乘数**：>1 是往上加，<1 是往下减。
        DIRECTION = [
            # 嫌暗 → 调亮（乘数 >1）
            ("黑漆漆", "brightness", "up"), ("暗得看不清", "brightness", "up"),
            ("太黑了", "brightness", "up"), ("曝光不足", "brightness", "up"),
            # 嫌亮/过曝/太白 → 调暗（乘数 <1）
            ("曝光太高", "brightness", "down"), ("太白了", "brightness", "down"),
            ("亮得晃眼", "brightness", "down"), ("刺眼", "brightness", "down"),
            ("泛白", "brightness", "down"), ("过曝", "brightness", "down"),
            # 颜色：淡/灰/素 → 减饱和（<1）；浓/艳 → 加饱和（>1）
            ("灰了", "color", "down"), ("太素了", "color", "down"),
            ("颜色太淡", "color", "down"), ("颜色太重", "color", "down"),
            ("浓一点", "color", "up"), ("鲜艳一点", "color", "up"),
        ]
        dir_bad = []
        for text, key, want in DIRECTION:
            got = C.parse_command(text)
            if got is None or got.kind != "adjust" or key not in got.params:
                dir_bad.append("%r→%s" % (text, got.kind if got else None))
                continue
            val = got.params[key]
            if want == "up" and not val > 1.0:
                dir_bad.append("%r→%s=%.2f(该 >1)" % (text, key, val))
            if want == "down" and not val < 1.0:
                dir_bad.append("%r→%s=%.2f(该 <1)" % (text, key, val))
        rep.check("**%d 句扩过的说法方向都对**" % len(DIRECTION), not dir_bad,
                  "; ".join(dir_bad))
        rep.check("「老照片」→ 黑白（老照片那种色）",
                  C.parse_command("老照片").kind == "grayscale",
                  C.parse_command("老照片").kind)
        rep.check("「清晰一点」仍是锐化（扩表后没被超分抢走）",
                  C.parse_command("清晰一点").kind == "adjust",
                  C.parse_command("清晰一点").kind)
        rep.check("「改小」→ 缩小",
                  C.parse_command("改小").kind == "resize_pct",
                  C.parse_command("改小").kind)
        rep.check("「去掉白边」→ 裁剪",
                  C.parse_command("去掉白边").kind == "crop",
                  C.parse_command("去掉白边").kind)

        # ============================================================
        rep.head("B. 顺序不能乱（中文这些说法互相咬得很紧）")
        # ============================================================
        rep.check("「变清晰」→ 超分（**不是**锐化）",
                  C.parse_command("变清晰").kind == "ai_cap",
                  str(C.parse_command("变清晰").params))
        rep.check("「清楚一点」→ 锐化（**不是**超分）",
                  C.parse_command("清楚一点").kind == "adjust",
                  C.parse_command("清楚一点").kind)
        rep.check("「把背景去掉」→ 抠图（**不是**调亮度）",
                  C.parse_command("把背景去掉").kind == "ai_cap",
                  str(C.parse_command("把背景去掉").params))
        rep.check("「变黑白」→ 灰度（**不是**被'亮'字抢走）",
                  C.parse_command("变黑白").kind == "grayscale")
        rep.check("「老照片上色」→ 上色能力（cap=colorize，不是灰度）",
                  (C.parse_command("老照片上色") or C.Command("", "")).params.get("cap")
                  == A.CAP_COLORIZE,
                  str(C.parse_command("老照片上色").params if C.parse_command("老照片上色") else None))
        rep.check("「翻转」没被'转'字抢成旋转",
                  C.parse_command("翻转").kind == "flip_h",
                  C.parse_command("翻转").kind)
        rep.check("**「转格式」不许被认成旋转**（那是另一个对话框的活）",
                  C.parse_command("转格式") is None,
                  str(C.parse_command("转格式")))

        # ============================================================
        rep.head("A4. 新加的说法：背景虚化 / 换背景 / 人脸修复 / 智能增强 / 自动调色")
        # ============================================================
        BG = [
            ("背景虚化", "bg_blur"), ("把背景弄模糊", "bg_blur"),
            ("换成白底", "bg_white"), ("白底", "bg_white"),
            ("证件照", "bg_white"), ("换成蓝底", "bg_blue"),
            ("蓝底证件照", "bg_blue"), ("换成红底", "bg_red"),
            ("红底证件照", "bg_red"),
            ("人脸修复", "ai_cap"), ("修脸", "ai_cap"),
            ("智能增强", "ai_cap"), ("整体增强", "ai_cap"),
            ("自动调色", "autofix"), ("一键优化", "autofix"),
        ]
        bg_bad = []
        for text, want in BG:
            got = C.parse_command(text)
            if got is None or got.kind != want:
                bg_bad.append("%r→%s(想要 %s)" % (
                    text, got.kind if got else None, want))
            elif want == "ai_cap":
                # 人脸修复 / 智能增强现在就是纯算法（不依赖权重），口令要认对。
                cap = got.params.get("cap")
                if cap not in ("face_restore", "enhance"):
                    bg_bad.append("%r→cap=%s" % (text, cap))
        rep.check("**%d 种新说法都解析对了**" % len(BG), not bg_bad,
                  "; ".join(bg_bad))

        # ============================================================
        rep.head("C. **听不懂就说不懂 —— 绝不猜**")
        # ============================================================
        UNKNOWN = ["帮我修得高级一点", "瘦脸", "把天空变成紫色", "来点氛围感",
                   "你觉得怎么样", "???", "", "   "]
        guessed = [t for t in UNKNOWN if C.parse_command(t) is not None]
        rep.check("**%d 句不认识的话，一句都没瞎猜**" % len(UNKNOWN), not guessed,
                  "; ".join("%r→%s" % (t, C.parse_command(t).kind
                                       if C.parse_command(t) else None)
                            for t in guessed))

        # ============================================================
        rep.head("D. 调戏 / 越界的话：当场拒绝，而不是装傻")
        # ============================================================
        # 使用者问："用户要是说出来什么调戏 AI 的词呢？"
        # 真正的保障是 E 段那两条（它根本没有别的能力）；这一段管的是
        # "把话说清楚" —— 装傻会让人以为"多试几次能绕过去"。
        BADS = ["你真傻", "滚", "讲个黄色笑话", "骂你两句", "弱智", "去死吧"]
        missed = [t for t in BADS
                  if (C.parse_command(t) or C.Command("", "")).kind != "refused"]
        rep.check("**%d 句越界的话都被当场拒绝**" % len(BADS), not missed,
                  str(missed))
        rep.check("拒绝时给的是「我只管改图」，不是含糊的「没听懂」",
                  "只管改图" in C.apply_command(None, C.parse_command("去死吧"))[1])
        # 越界词**优先**于改图词：就算前面还夹着一句正经要求，也先拒绝。
        # （宁可让他重打一遍，也不要"边拒绝边把图改了"。）
        rep.check("夹在正经要求里的越界词，也是先拒绝",
                  C.parse_command("把背景去掉，你真傻").kind == "refused")

        # ============================================================
        rep.head("E. **沙箱是可证明的**（AST 检查，不靠嘴说）")
        # ============================================================
        import ast
        src_path = os.path.join(OPTIMIZED, "image_command.py")
        tree = ast.parse(open(src_path, encoding="utf-8").read())

        BANNED_FUNC = {"eval", "exec", "compile", "__import__", "open",
                       "system", "popen", "Popen", "spawn", "startfile",
                       "remove", "unlink", "rmtree", "write_text", "dump"}
        BANNED_MOD = {"subprocess", "urllib", "requests", "socket", "http",
                      "ctypes", "shutil", "pickle", "os", "importlib",
                      "webbrowser", "winreg", "pathlib"}
        bad_call, bad_import = [], []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for al in node.names:
                    if al.name.split(".")[0] in BANNED_MOD:
                        bad_import.append("import " + al.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split(".")[0] in BANNED_MOD:
                    bad_import.append("from " + node.module)
            elif isinstance(node, ast.Call):
                fn = node.func
                nm = getattr(fn, "attr", None) or getattr(fn, "id", None)
                if nm in BANNED_FUNC:
                    bad_call.append("%s(第 %d 行)" % (nm, node.lineno))
        rep.check("**不能执行任何东西**（没有 eval/exec/编译/开文件/起进程）",
                  not bad_call, "; ".join(bad_call))
        rep.check("**碰不到图片之外的任何东西**（没有网络/系统/进程模块）",
                  not bad_import, "; ".join(bad_import))

        # 它能做的动作，必须**全在一张白名单里** —— 多一个都算越界。
        KINDS = {"undo", "redo", "rotate_cw", "rotate_ccw", "rotate_180",
                 "rotate_any", "flip_h", "flip_v",
                 "grayscale", "adjust", "resize_pct", "crop", "ai_cap",
                 "refused", "bg_blur", "bg_white", "bg_blue", "bg_red",
                 "autofix"}
        corpus = [t for t, _w in CASES] + UNKNOWN + BADS + [
            "把图删了", "打开 C 盘", "运行 calc", "上传到网上", "读取我的文件",
            "关机", "把这张图发出去", "访问百度"]
        corpus += [t for t, _k, _d in ANG] + [t for t, _k, _dir in DIRECTION]
        stray = sorted({c.kind for c in (C.parse_command(t) for t in corpus)
                        if c is not None} - KINDS)
        rep.check("**解析出的动作种类全在白名单里**（没有越界动作）",
                  not stray, str(stray))
        rep.check("白名单里没有「删文件 / 上传 / 关机」这类动作",
                  not ({"delete", "upload", "shutdown", "run", "open_file"}
                       & KINDS))

        # ============================================================
        rep.head("F. 落到界面上：真的改了图、撤销时说得清做了什么")
        # ============================================================
        root = tk.Tk()
        root.geometry("900x680+60+60")
        tool = T.ImageTool(root, on_quit=root.destroy)
        tool.pack(fill="both", expand=True)
        tool.build()
        root.update()
        try:
            tool.image = Image.new("RGB", (80, 60), (120, 120, 120))
            tool._original = tool.image.copy()
            root.update()

            ok, msg = C.apply_command(tool, C.parse_command("转一下"))
            rep.check("「转一下」真的把图转了",
                      ok and tool.image.size == (60, 80),
                      "%s %s" % (msg, tool.image.size))
            rep.check("**撤销栈里留的是人话**（不是笼统的'编辑'）",
                      bool(tool._undo_stack)
                      and tool._undo_stack[-1][0] == "右转 90°",
                      str(tool._undo_stack[-1][0] if tool._undo_stack else None))

            tool.undo()
            root.update()
            rep.check("撤销回到说那句话之前", tool.image.size == (80, 60),
                      str(tool.image.size))

            # ---- 180° 真的转 180°（不是 90°）----
            # 用一张**四角颜色不同**的图来验：180° 转完，左上角应该变成
            # 原来的右下角颜色。只比尺寸是看不出来的（180° 不改变尺寸）。
            quad = Image.new("RGB", (80, 60), (0, 0, 0))
            for x in range(80):
                for y in range(60):
                    quad.putpixel((x, y),
                                  (255 if x >= 40 else 0,
                                   255 if y >= 30 else 0, 128))
            tool.image = quad.copy()
            tool._original = quad.copy()
            root.update()
            before_tl = tool.image.getpixel((2, 2))
            ok180, msg180 = C.apply_command(
                tool, C.parse_command("旋转180度"))
            after_tl = tool.image.getpixel((2, 2))
            after_br = tool.image.getpixel((77, 57))
            rep.check("「旋转180度」尺寸不变（80x60 → 80x60）",
                      ok180 and tool.image.size == (80, 60),
                      "%s %s" % (msg180, tool.image.size))
            rep.check("**「旋转180度」真的翻了半圈**（左上变成原来的右下）",
                      after_tl == (255, 255, 128) and after_br == (0, 0, 128),
                      "左上 %s（该 (255,255,128)）右下 %s（该 (0,0,128)）"
                      % (after_tl, after_br))
            rep.check("撤销栈里写的是「转 180°」",
                      bool(tool._undo_stack)
                      and tool._undo_stack[-1][0] == "转 180°",
                      str(tool._undo_stack[-1][0] if tool._undo_stack else None))
            tool.undo()
            root.update()
            rep.check("180° 也能撤销回原样",
                      tool.image.getpixel((2, 2)) == before_tl,
                      str(tool.image.getpixel((2, 2))))

            # ---- 任意角（45°）：走 rotate_any，尺寸会变大（补边）----
            ok45, msg45 = C.apply_command(tool, C.parse_command("旋转45度"))
            rep.check("「旋转45度」能执行且尺寸变大（补了边）",
                      ok45 and tool.image.size[0] > 80,
                      "%s %s" % (msg45, tool.image.size))
            tool.undo()
            root.update()

            # 抠图这类要模型的：没模型时**如实挡住**，不许默默什么都不做
            tool._show_status("")
            ok2, msg2 = C.apply_command(tool, C.parse_command("抠图"))
            st = tool._st_msg.cget("text")
            rep.check("要模型的说法 → 走 AI 那条路并如实提示",
                      ok2 and ("尚未下载" in st or "先点" in st or "AI" in st),
                      st[:60])

            # 没打开图片时：说清楚，不炸
            tool.image = None
            ok3, msg3 = C.apply_command(tool, C.parse_command("调亮一点"))
            rep.check("没打开图片 → 明确说「先打开一张图片」",
                      ok3 is False and "先打开" in msg3, msg3)

            # ---- 弹窗：打字 + 回车 ----
            dlg = C.CommandDialog(root)
            dlg.entry.insert(0, "调亮一点")
            dlg._on_run()
            rep.check("弹窗里打一句话 + 回车 → 解析出来了",
                      isinstance(dlg.result, C.Command)
                      and dlg.result.kind == "adjust",
                      str(dlg.result))
            dlg2 = C.CommandDialog(root)
            dlg2.entry.insert(0, "给我整点赛博朋克")
            dlg2._on_run()
            rep.check("**听不懂时窗口不关、并把话说清楚**",
                      dlg2.result is None and "没听懂" in dlg2.msg.cget("text"),
                      dlg2.msg.cget("text")[:40])
            dlg2._safe_destroy()
        finally:
            tool.destroy()
            root.destroy()

    except Exception:
        rep.check("测试过程抛了异常", False)
        rep.add(traceback.format_exc())

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(rep.text())
    try:
        import _redact
        _redact.scrub_file(OUT)
    except Exception:                                          # noqa: BLE001
        pass
    print(rep.text())
    try:
        print("报告：%s" % OUT)
    except Exception:                                          # noqa: BLE001
        pass
    sys.exit(1 if rep.failures else 0)


if __name__ == "__main__":
    main()
