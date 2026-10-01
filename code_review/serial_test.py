# -*- coding: utf-8 -*-
"""序列号 + 月度闸门 + 图片工具的三合一回归。

为什么这三件事放在一支测试里
==================================================================
它们串在同一条链路上，单独测任何一段都测不出真正会坏的地方：

    月度闸门（state_store）  ── 决定要不要出题
        答题 / 序列号（serial_check）  ── 决定能不能进
            图片工具（image_tool）  ── 进去之后能不能真的干活

典型的坏法都不是"某一段算错了"，而是**接缝**错了：
过闸了但没写状态（下次还问）、写了状态但月份用的是 UTC（月初/月末多问一次）、
序列号校验过了但界面没换页、另存为默认名指向原文件（静默覆盖用户的图）。
所以这里从头到尾走一遍真实流程，只把弹框换成"自动确认"。

判定口径
==================================================================
不联网、不弹窗、不写用户目录：状态写进临时目录，图片写进临时目录，跑完自清理。
任何一项不成立 → 退出码非 0。
"""
from __future__ import annotations

import builtins
import contextlib
import hashlib
import io
import logging
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "optimized"))

import serial_check      # noqa: E402
import state_store       # noqa: E402


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def add(self, text: str = "") -> None:
        self.lines.append(text)

    def head(self, text: str) -> None:
        self.add("")
        self.add("-" * 72)
        self.add(text)

    def check(self, label: str, cond: bool, extra: str = "") -> bool:
        # >>> check() 参数顺序守卫（别删）
        # 本文件是 check("说明", 条件)。把条件放进第一位同样不会报错 ——
        # 非空字符串恒为真，断言会静默变成"永远通过"。
        if not isinstance(label, str):
            raise TypeError(
                u'check() 参数写反了：本文件应为 check("说明", 条件)，'
                u'收到 check(%r, %r)' % (label, cond))

        if cond:
            self.lines.append("  [通过] %s %s" % (label, extra))
        else:
            self.failures.append(label)
            self.lines.append("  [失败] %s %s" % (label, extra))
        return bool(cond)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# 1. 序列号：正向生成 ↔ 逆向校验
# --------------------------------------------------------------------------
def test_serial(rep: Report) -> None:
    rep.head("[1] 序列号：正向生成 vs 逆向校验（自证）")
    ok, logs = serial_check.selftest(200)
    for line in logs:
        rep.lines.append("    " + line)
    rep.check("serial_check.selftest() 全部通过", ok)

    sample = serial_check.generate("2026-09")
    rep.add("    样例：%s（2026-09）" % sample)
    rep.check("样例形状正确（25 位 / 5 组）",
              len(sample) == 29 and sample.count("-") == 4
              and serial_check.is_wellformed(sample))
    rep.check("同一月份生成两次结果不同（nonce 是随机的）",
              serial_check.generate("2026-09") != serial_check.generate("2026-09"))
    rep.check("verify 返回人类可读的原因而不是只有 bool",
              isinstance(serial_check.verify("abc")[1], str)
              and len(serial_check.verify("abc")[1]) > 4)


# --------------------------------------------------------------------------
# 1.5 生成器命令行：打印完不许闪退，但也不能让脚本挂死
# --------------------------------------------------------------------------
def test_cli_pause(rep: Report) -> None:
    rep.head("[1.5] 生成器命令行：不闪退，也不许让脚本挂死")

    # 使用者实测报的问题：双击 serial_check.exe，小黑窗只活 1.6 秒，
    # 序列号根本来不及看，更别说抄下来（而答错被锁之后生成器是唯一出口）。
    # 修法是"打印完停下来等回车"，但停顿**只能在"像人双击开的"时候发生**：
    #
    # ⚠ code_review/_serial_e2e.py 是 subprocess.run(..., capture_output=True)
    #   跑它的 —— 那时 stdout 是管道，**stdin 却还是继承来的控制台**。
    #   如果判据只看 sys.stdin.isatty()，那支自检就会挂死在这里等回车
    #   （要等 90 秒超时才炸）。所以判据必须是"stdin 与 stdout 都是真控制台"。
    #   这一条在本地很难"自然发生"，所以这里直接把各种组合喂进去。

    class _IO:
        def __init__(self, tty):
            self._tty = tty

        def isatty(self):
            return self._tty

    class _Sys:
        def __init__(self, stdin, stdout):
            self.stdin, self.stdout = stdin, stdout

    real_sys = serial_check.sys
    real_input = builtins.input
    hits = []

    def run_pause(stdin_tty=True, stdout_tty=True, stdin_none=False,
                  raise_eof=False):
        hits.clear()
        serial_check.sys = _Sys(None if stdin_none else _IO(stdin_tty),
                                _IO(stdout_tty))

        def _inp(*a, **k):
            hits.append(1)
            if raise_eof:
                raise EOFError()
            return ""

        builtins.input = _inp
        try:
            serial_check._pause_at_end()
        finally:
            builtins.input = real_input
        return len(hits)

    try:
        rep.check("双击那种情形（stdin+stdout 都是控制台）→ **真的停下来等回车**",
                  run_pause(True, True) == 1)
        rep.check("stdout 是管道（capture_output / 重定向）→ 不停，立刻退出",
                  run_pause(True, False) == 0)
        rep.check("stdin 是管道 → 不停，立刻退出",
                  run_pause(False, True) == 0)
        rep.check("没有 stdin（windowed 打包）→ 不停，也不炸",
                  run_pause(True, True, stdin_none=True) == 0)
        rep.check("输入被关掉（EOFError）→ 静默收场，不往上抛",
                  run_pause(True, True, raise_eof=True) == 1)
    finally:
        serial_check.sys = real_sys

    # 命令行本身：默认要停，--quiet / --no-pause 一定不停（脚本靠这两条）
    real_pause = serial_check._pause_at_end
    seen = []
    serial_check._pause_at_end = lambda: seen.append(1)
    try:
        for argv, expect in (["--no-pause"], 0), ([], 1), (["--quiet"], 0):
            seen.clear()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = serial_check.main(list(argv))
            out = buf.getvalue()
            rep.check("main(%r)：退出码 0，停顿 %d 次" % (list(argv), expect),
                      rc == 0 and len(seen) == expect,
                      "实际停顿 %d 次" % len(seen))
            serials = [ln for ln in out.splitlines()
                       if len(ln) == 29 and ln.count("-") == 4]
            if list(argv) == ["--quiet"]:
                rep.check("--quiet 只吐序列号本身（自动化靠它解析）",
                          len(out.strip()) == 29 and bool(serials),
                          out.strip()[:12] + "…")
            else:
                rep.check("打印出来的序列号本地 verify 一定认",
                          bool(serials) and serial_check.verify(serials[0])[0],
                          (serials or [""])[0])
    finally:
        serial_check._pause_at_end = real_pause


# --------------------------------------------------------------------------
# 2. 月度状态
# --------------------------------------------------------------------------
def test_state(rep: Report, tmp: str) -> None:
    rep.head("[2] 月度状态：过闸记录写在哪、下个月还认不认")
    state_store.set_state_dir_for_tests(tmp)

    path = state_store.state_path()
    rep.add("    状态文件：%s" % path)
    rep.check("状态目录被 HAAVIK_STATE_DIR 接管（不碰用户目录）",
              bool(path) and os.path.dirname(path) == tmp)

    fresh = state_store.load()
    day1 = date(2026, 9, 1)
    rep.check("全新环境：尚未过闸",
              not state_store.is_unlocked(fresh, today=day1, version="1.3.0"))
    rep.check("状态文件此时还不存在", not os.path.isfile(path))

    state_store.mark_unlocked(fresh, state_store.VIA_QUIZ, today=day1,
                              version="1.3.0")
    rep.check("过闸后文件被写出", os.path.isfile(path))

    reread = state_store.load()
    rep.check("同一天：已过闸（不用再问）",
              state_store.is_unlocked(reread, today=day1, version="1.3.0"))
    rep.check("第 6 天：还有效",
              state_store.is_unlocked(reread, today=date(2026, 9, 7),
                                      version="1.3.0"))
    rep.check("第 7 天：到期，重新问",
              not state_store.is_unlocked(reread, today=date(2026, 9, 8),
                                          version="1.3.0"))
    rep.check("第 30 天：重新问",
              not state_store.is_unlocked(reread, today=date(2026, 10, 1),
                                          version="1.3.0"))
    rep.check("版本变了要重新问（同一时刻、换了版本号）",
              not state_store.is_unlocked(reread, today=day1, version="1.3.1"))
    rep.check("不传版本号时只看时间（兼容只关心时间的调用方）",
              state_store.is_unlocked(reread, today=day1))
    rep.check("累计次数被记下", int(reread[state_store.KEY_UNLOCK_COUNT]) == 1)
    rep.check("过闸方式被记下", reread[state_store.KEY_LAST_VIA] == state_store.VIA_QUIZ)
    rep.check("过闸日期被记下", reread[state_store.KEY_UNLOCKED_AT] == "2026-09-01")
    rep.check("过闸版本被记下", reread[state_store.KEY_UNLOCKED_VERSION] == "1.3.0")

    # reset_gate 是唯一入口：命令行 --reset-gate 与左下角白点共用它
    state_store.reset_gate(reread)
    rep.check("reset_gate 之后必须重新出题",
              not state_store.is_unlocked(reread, today=day1, version="1.3.0"))

    # 损坏的 JSON 不能让程序打不开，但必须退化成"全新状态"而不是崩
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ 这不是 json")
    broken = state_store.load()
    rep.check("状态文件损坏时退化成全新状态（不抛异常）",
              isinstance(broken, dict) and not state_store.is_unlocked(broken, today=day1))

    # 不是 dict 的 JSON 同样
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("[1, 2, 3]")
    rep.check("状态文件是个数组时也退化成全新状态",
              isinstance(state_store.load(), dict))
    os.remove(path)

    # ---- 反馈的隐私同意：记的是**版本号**，不是"同意过"这个布尔值 ----
    fresh = state_store.empty_state()
    rep.check("全新状态：第一次用反馈要先弹同意书",
              state_store.needs_feedback_consent(fresh, 1))
    state_store.save(fresh)
    state_store.mark_feedback_consent(fresh, 1)
    rep.check("同意过 v1 之后不再重复打扰",
              not state_store.needs_feedback_consent(state_store.load(), 1))
    rep.check("同意的是第几版被记下来了",
              state_store.feedback_consent_version(state_store.load()) == 1,
              repr(state_store.feedback_consent_version(state_store.load())))
    rep.check("同意的日期也记了（备查）",
              bool(state_store.load().get(state_store.KEY_FEEDBACK_CONSENT_AT)))
    # ★ 关键：说明内容改版（版本号 +1）必须重新问一次
    rep.check("★ 隐私说明改版（v2）后必须重新征求同意 —— "
              "「以前同意过」不能当永久许可",
              state_store.needs_feedback_consent(state_store.load(), 2))
    # 没同意过时不能被当成"已同意"
    rep.check("从没同意过 → 版本号 0，且仍需征求同意",
              state_store.feedback_consent_version(state_store.empty_state()) == 0
              and state_store.needs_feedback_consent(state_store.empty_state(), 1))
    # 状态文件里的值被人改坏（比如写成字符串）也不能崩
    weird = state_store.empty_state()
    weird[state_store.KEY_FEEDBACK_CONSENT] = "不是数字"
    rep.check("同意版本号损坏时当成没同意过（不抛异常）",
              state_store.feedback_consent_version(weird) == 0
              and state_store.needs_feedback_consent(weird, 1))
    os.remove(path)


# --------------------------------------------------------------------------
# 2.5 v1.2.0 状态迁移：exe 旁边的 state.json 搬进 %LOCALAPPDATA%，桌面不再多文件
# --------------------------------------------------------------------------
def test_migration(rep: Report, tmp: str) -> None:
    rep.head("[2.5] v1.2.0 迁移：旧状态（exe 旁边）→ %LOCALAPPDATA%，旧文件删掉")

    # 全程重定向到临时目录：LOCALAPPDATA、app_dir（monkeypatch）、缓存都要动，
    # 绝不允许测试把用户真实的 %LOCALAPPDATA%\HAAVIK Photo\ 当成实验田。
    legacy_dir = os.path.join(tmp, "exe_dir")
    os.makedirs(legacy_dir)
    fake_lad = os.path.join(tmp, "lad")
    real_lad = os.environ.get("LOCALAPPDATA")
    real_app_dir = state_store.app_dir
    os.environ["LOCALAPPDATA"] = fake_lad
    state_store.app_dir = lambda: legacy_dir      # monkeypatch：假装 exe 在 legacy_dir
    state_store._cached_path = None
    os.environ.pop(state_store.ENV_OVERRIDE, None)   # 迁移只在无覆盖时发生
    try:
        new_dir = os.path.join(fake_lad, state_store.APP_DIRNAME)
        new_path = os.path.join(new_dir, state_store.STATE_FILENAME)
        old_path = os.path.join(legacy_dir, state_store.STATE_FILENAME)

        with open(old_path, "w", encoding="utf-8") as fh:
            json.dump({"schema": 1, "unlocked_month": "2026-09",
                       state_store.KEY_QUIZ_ASKED: 1}, fh)

        got = state_store.load()
        rep.check("旧状态被搬进 %LOCALAPPDATA%\\HAAVIK Photo\\",
                  os.path.isfile(new_path))
        rep.check("迁移后的月份还在（朋友不会被重新问一遍）",
                  got.get(state_store.KEY_UNLOCKED_MONTH) == "2026-09",
                  repr(got.get(state_store.KEY_UNLOCKED_MONTH)))
        rep.check("★ 桌面（exe 旁边）的旧 state.json 已删除",
                  not os.path.exists(old_path))

        rep2 = state_store.load()
        rep.check("第二次 load 不会重复迁移（旧文件已经不在了）",
                  rep2.get(state_store.KEY_UNLOCKED_MONTH) == "2026-09")

        # 新位置已有状态时：旧的只是个没人要的垃圾，留着不动（新状态是权威）
        with open(old_path, "w", encoding="utf-8") as fh:
            json.dump({"unlocked_month": "1999-01"}, fh)
        state_store.load()
        rep.check("新位置已有状态时不碰旧文件（新状态是权威）",
                  os.path.exists(old_path))
    finally:
        state_store.app_dir = real_app_dir
        if real_lad is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = real_lad
        state_store._cached_path = None


# --------------------------------------------------------------------------
# 3. 界面闸门（真跑一遍 Tk，只把弹框自动化）
# --------------------------------------------------------------------------
def test_gate(rep: Report, tmp: str) -> None:
    rep.head("[3] 界面闸门：首启问答 → 答错锁定 → 序列号解除 → 图片工具")
    state_store.set_state_dir_for_tests(tmp)
    # ⚠️ version_check.update_dir() 读的是 **LOCALAPPDATA**，而 App 启动时会按它
    # 清理"用过的旧更新包"。不把它指到临时目录的话，跑一次测试就会去删使用者
    # 真实 %LOCALAPPDATA%\HAAVIK Photo\updates 里的东西 —— 第一次跑就这么干了。
    # （同文件里的迁移测试也是这么隔离的；各测试是独立进程，所以不用还原。）
    os.environ["LOCALAPPDATA"] = tmp

    import tkinter as tk
    import main as prank

    # 弹框全部自动化：测试不该卡在模态框上
    asked = []
    prank.messagebox.askyesno = lambda *a, **k: (asked.append(k.get("default")), True)[1]
    prank.messagebox.askokcancel = lambda *a, **k: True
    prank.messagebox.showinfo = lambda *a, **k: None
    prank.messagebox.showwarning = lambda *a, **k: None
    prank.messagebox.showerror = lambda *a, **k: None

    root = tk.Tk()
    app = prank.App(root, real_shutdown=False, seconds=3600,
                    show_image=False, check_updates=False)
    rep.check("首启：出题（mode=quiz）", app.mode == "quiz", "(实际 %r)" % app.mode)

    # ---- 答错 → 锁定 ----
    app._on_answer(prank.OPTIONS[1])
    rep.check("答错：进入锁定页（mode=lock）", app.mode == "lock", "(实际 %r)" % app.mode)
    rep.check("锁定页给出了一个可输入的序列号输入框",
              app.code_entry.entry is not None
              and app.code_entry.entry.winfo_class() == "Entry"
              and str(app.code_entry.entry.cget("state")) != "disabled")
    rep.check("没有把输入框做成只读/禁用（要能打字）",
              str(app.code_entry.entry.cget("state")) == "normal",
              repr(str(app.code_entry.entry.cget("state"))))
    rep.check("锁定页提示里有仓库地址", prank.REPO_HINT_URL.startswith("https://"))
    rep.check("答错**不会**设置过闸", not state_store.is_unlocked(state_store.load()))

    # ---- ★ 锁定必须落盘：关掉程序重开，还在锁定页（不回问题页）----
    # 使用者实测反馈："我关闭程序，再打开，又回到问题了。" —— 那等于没锁。
    # 用一个全新的 App 实例模拟"重新打开程序"（新进程读同一份 state.json）。
    rep.check("答错后锁定状态已落盘", state_store.is_locked(state_store.load()))
    root_l = tk.Tk()
    app_l = prank.App(root_l, real_shutdown=False, seconds=3600,
                      show_image=False, check_updates=False)
    rep.check("重开程序：直接进锁定页（不再回到问题页）", app_l.mode == "lock",
              "(实际 %r)" % app_l.mode)
    rep.check("重开的锁定页同样给得出输入框",
              app_l.code_entry.entry is not None)
    root_l.destroy()

    # ---- 锁定页的粘贴：Ctrl+V 要能整串灌进去 ----
    # 顺带守住"粘不进来时必须吭声" —— 静默失败会让人得出"不能粘贴"的结论。
    # 重试是必要的：本机跑着 360 / 火绒，它们的剪贴板挂钩会短暂占住剪贴板，
    # 那一刻 Tk 读不到内容 —— 那是环境问题，不是产品问题，不该判失败。
    good_for_paste = serial_check.generate()
    want = serial_check.normalize(good_for_paste)
    got_paste = ""
    for _attempt in range(8):
        root.clipboard_clear()
        root.clipboard_append(good_for_paste)
        root.update()
        try:
            if root.clipboard_get() != good_for_paste:
                raise tk.TclError("读回来的不是刚放进去的东西")
        except tk.TclError:
            time.sleep(0.2)
            continue
        app.code_entry.clear()
        app.code_entry.focus_first()
        root.update()
        app.code_entry.entry.event_generate("<Control-v>", when="now")
        root.update()
        got_paste = app.code_entry.value()
        if got_paste == want:
            break
        time.sleep(0.2)
    # 对照：直接调一次（绕过按键事件）。两步结果不一样就说明是"事件没进来"，
    # 一样就说明是处理函数本身的问题 —— 这条信息比"粘不进去"有用得多。
    app.code_entry.clear()
    app.code_entry.paste_anywhere()
    root.update()
    direct = app.code_entry.value()
    rep.check("Ctrl+V 能整串粘贴进输入框", got_paste == want,
              "%r（直接调 paste_anywhere 得到 %r / 页面提示 %r）"
              % (got_paste, direct, app.lock_msg.cget("text")[:60]))

    # 打字：一个字一个字敲进去，同样要能进（这正是用户要的"同时能粘贴和输入"）
    app.code_entry.clear()
    app.code_entry.focus_first()
    root.update()
    for ch in serial_check.normalize(good_for_paste):
        app.code_entry.entry.insert("end", ch)
        app.code_entry._on_key()
    root.update()
    rep.check("一个字一个字敲也能完整进去（打字路径）",
              app.code_entry.value() == serial_check.normalize(good_for_paste),
              repr(app.code_entry.value()))
    # 敲进去的如果是小写 / 带分隔符，也要被归一化（不改变字母本身）
    app.code_entry.clear()
    app.code_entry.var.set(" ".join(good_for_paste.lower()))
    app.code_entry._on_key()
    rep.check("小写与空格被归一化（不改变字母本身）",
              app.code_entry.value() == serial_check.normalize(good_for_paste),
              repr(app.code_entry.value()))

    root.clipboard_clear()
    root.clipboard_append("这串里没有一个合法字符！")
    root.update()
    app.code_entry.clear()
    app.code_entry.paste_anywhere()
    root.update()
    rep.check("剪贴板里没有合法字符时给出提示（不是静默无视）",
              "有效字符" in app.lock_msg.cget("text"), repr(app.lock_msg.cget("text")[:30]))
    app.code_entry.clear()      # 别把刚才粘进去的东西留给后面的用例
    root.clipboard_clear()
    root.update()

    # ---- 乱序列号 → 仍锁定 ----
    def fill(text: str) -> None:
        """把一串序列号灌进输入框（模拟用户打字/粘贴的结果）。"""
        app.code_entry.clear()
        app.code_entry.var.set(serial_check.normalize(text))
        app.code_entry._on_key()

    fill("1234567890123456789012345")
    app._verify_serial()
    rep.check("乱序列号：仍锁定", app.mode == "lock", "(实际 %r)" % app.mode)
    rep.check("乱序列号：给出了具体原因",
              "校验位" in app.lock_msg.cget("text") or "签名" in app.lock_msg.cget("text"),
              repr(app.lock_msg.cget("text")[:30]))

    # ---- 正确序列号 → 进工具 ----
    good = serial_check.generate()
    fill(good)
    rep.add("    填入生成器产出的序列号：%s -> 输入框拼出 %s" % (good, app.code_entry.value()))
    rep.check("输入框拼出的字符串与生成的序列号一致",
              app.code_entry.value() == serial_check.normalize(good))
    app._verify_serial()
    rep.check("正确序列号：进入图片工具（mode=tool）", app.mode == "tool",
              "(实际 %r)" % app.mode)
    rep.check("正确序列号：本月已过闸", state_store.is_unlocked(state_store.load()))
    rep.check("正确序列号：过闸方式记为 serial",
              state_store.load()[state_store.KEY_LAST_VIA] == state_store.VIA_SERIAL)
    rep.check("输对序列号后：锁定被解除（不会卡在锁定页）",
              not state_store.is_locked(state_store.load()))
    root.destroy()

    # ---- ★ 用序列号解锁后，图片工具里必须**已经有图**（不能是空白页）----
    # 2026-09-16 补的回归：示例图曾经只在 first_time=True 时加载，而"输序列号"
    # 这条路传给 _show_tool 的是 False → 使用者看到空白工具页，报"图片加载不出来"。
    # 本文件其它地方一律 show_image=False，正好把这一条照不到 —— 所以这里
    # 特意开着 show_image 跑，并且**走真序列号路径**，不直接调 _show_tool。
    sample = prank.resource_path("haavik.png")
    rep.check("示例图 haavik.png 在源码目录里找得到", os.path.isfile(sample), sample)

    state_store.reset_gate(state_store.load())        # 逼它重新出题
    root_s = tk.Tk()
    app_s = prank.App(root_s, real_shutdown=False, seconds=3600,
                      show_image=True, check_updates=False)
    rep.check("（前置）重置过闸后重新出题", app_s.mode == "quiz",
              "(实际 %r)" % app_s.mode)
    app_s._on_answer(prank.OPTIONS[1])                # 答错 → 锁定页
    rep.check("（前置）答错后进入锁定页", app_s.mode == "lock",
              "(实际 %r)" % app_s.mode)

    def fill_s(text: str) -> None:
        app_s.code_entry.clear()
        app_s.code_entry.var.set(serial_check.normalize(text))
        app_s.code_entry._on_key()

    fill_s(serial_check.generate())
    app_s._verify_serial()
    rep.check("输序列号解锁：进入图片工具", app_s.mode == "tool",
              "(实际 %r)" % app_s.mode)
    rep.check("★ 解锁后工具页不是空白的（示例图已自动打开）",
              app_s.tool.has_image())
    root_s.destroy()

    # ---- 本月第二次启动：不再问 ----
    root2 = tk.Tk()
    app2 = prank.App(root2, real_shutdown=False, seconds=3600,
                     show_image=False, check_updates=False)
    rep.check("本月第二次启动：直接进工具，不再出题", app2.mode == "tool",
              "(实际 %r)" % app2.mode)
    root2.destroy()

    # ---- 答对：同样过闸，且不需要序列号 ----
    st = state_store.load()
    st[state_store.KEY_UNLOCKED_AT] = "2020-01-01"      # 假装很久以前过的闸
    state_store.save(st)
    root3 = tk.Tk()
    app3 = prank.App(root3, real_shutdown=False, seconds=3600,
                     show_image=False, check_updates=False)
    rep.check("距上次过闸太久（>7 天）：重新出题", app3.mode == "quiz",
              "(实际 %r)" % app3.mode)
    app3._on_answer(prank.OPTIONS[0])                    # 正确选项
    rep.check("答对：直接进图片工具（无需序列号）", app3.mode == "tool",
              "(实际 %r)" % app3.mode)
    rep.check("答对：过闸方式记为 quiz",
              state_store.load()[state_store.KEY_LAST_VIA] == state_store.VIA_QUIZ)
    root3.destroy()

    # ---- 版本一变也要重新问（用户要求：更新完也要问一遍） ----
    st2 = state_store.load()
    st2[state_store.KEY_UNLOCKED_VERSION] = "0.0.1"     # 假装是旧版本过的闸
    state_store.save(st2)
    root_v = tk.Tk()
    app_v = prank.App(root_v, real_shutdown=False, seconds=3600,
                      show_image=False, check_updates=False)
    rep.check("版本变过：即使时间没过期也重新出题", app_v.mode == "quiz",
              "(实际 %r)" % app_v.mode)
    root_v.destroy()

    # ---- reset_gate：把闸门恢复成"还没问" ----
    state = state_store.load()
    state_store.reset_gate(state)
    root4 = tk.Tk()
    app4 = prank.App(root4, real_shutdown=False, seconds=3600,
                     show_image=False, check_updates=False)
    rep.check("清掉过闸记录后：重新出题（--reset-gate 的效果）",
              app4.mode == "quiz", "(实际 %r)" % app4.mode)
    root4.destroy()


# --------------------------------------------------------------------------
# 4. 图片工具：能干活，且绝不默认覆盖原图
# --------------------------------------------------------------------------
def test_image_tool(rep: Report, tmp: str) -> None:
    rep.head("[4] 图片工具：改尺寸 / 转格式 / 另存为（含「绝不覆盖原图」这条硬约束）")
    import tkinter as tk

    from PIL import Image

    import image_tool

    src_dir = os.path.join(tmp, "pics")
    os.makedirs(src_dir, exist_ok=True)
    src = os.path.join(src_dir, "示例照片.png")
    Image.new("RGBA", (1200, 800), (200, 30, 30, 128)).save(src)
    src_hash = sha256_file(src)

    root = tk.Tk()
    root.withdraw()
    tool = image_tool.ImageTool(root, on_quit=root.destroy)

    rep.check("打开前没有图，且操作按钮已禁用", tool.image is None)
    rep.check("能打开一张 PNG", tool.load_file(src))
    rep.check("打开后拿到正确尺寸",
              tool.image.size == (1200, 800), "(实际 %s)" % (tool.image.size,))

    # 改尺寸：按像素
    tool.resize_to_px(600, 400)
    rep.check("按像素改尺寸生效",
              tool.image.size == (600, 400), "(实际 %s)" % (tool.image.size,))
    # 改尺寸：按百分比
    tool.resize_to_pct(50)
    rep.check("按百分比改尺寸生效",
              tool.image.size == (300, 200), "(实际 %s)" % (tool.image.size,))
    # 还原
    tool.revert()
    rep.check("还原原图生效", tool.image.size == (1200, 800))
    # 旋转
    tool.rotate()
    rep.check("旋转 90° 生效",
              tool.image.size == (800, 1200), "(实际 %s)" % (tool.image.size,))
    tool.revert()

    # 默认文件名不得等于原文件名
    rep.check("另存为默认名带 _改 后缀，不指回原文件",
              tool.suggested_name("JPEG") == "示例照片_改.jpg",
              "(实际 %r)" % tool.suggested_name("JPEG"))

    # 四种格式都能写出来，且 RGBA→JPEG 会自动合成白底（不是直接报错）
    for fmt in image_tool.FORMATS:
        out = os.path.join(tmp, "out" + image_tool.FMT_EXT[fmt])
        ok, detail = tool.write_image(out, fmt, 85)
        rep.check("写出 %s" % fmt, ok, detail)
        if ok:
            with Image.open(out) as back:
                want_mode = "RGB" if fmt not in image_tool.ALPHA_OK else None
                ok_mode = want_mode is None or back.mode == want_mode
                rep.check("  %s 能被 Pillow 读回且模式正确" % fmt,
                          ok_mode, "(实际 %s)" % back.mode)

    # ---- 硬约束：输出路径 == 源文件时必须**再确认一次**，默认按钮是"否" ----
    prompts = []
    image_tool.filedialog.asksaveasfilename = lambda **k: src
    image_tool.messagebox.askyesno = lambda *a, **k: (prompts.append(k.get("default")), False)[1]
    tool.last_saved = None
    result = tool.save_as(fmt="PNG")
    rep.check("选中原路径时会再问一次，且默认按钮是「否」",
              len(prompts) == 1 and prompts[0] == image_tool.messagebox.NO,
              "(实际 %r)" % (prompts,))
    rep.check("用户选「否」时没有写出任何东西", result is None)
    rep.check("原图字节完全没变（sha256 一致）",
              sha256_file(src) == src_hash)

    # 用户明确要覆盖时才真的覆盖 —— 并且这时哈希应当变化
    image_tool.messagebox.askyesno = lambda *a, **k: True
    tool.resize_to_px(320, 320)
    result = tool.save_as(fmt="PNG")
    rep.check("用户选「是」时才真的覆盖", result is not None and sha256_file(src) != src_hash)

    # 打开一个根本不是图片的文件：必须报错而不是崩
    junk = os.path.join(tmp, "not-an-image.png")
    with open(junk, "w", encoding="utf-8") as fh:
        fh.write("这不是图片")
    image_tool.messagebox.showerror = lambda *a, **k: None
    rep.check("打开非图片文件返回 False 而不是抛异常", tool.load_file(junk) is False)

    root.destroy()


# --------------------------------------------------------------------------
# 4. 倒计时归零 → 揭晓页（演示模式）
# --------------------------------------------------------------------------
def test_countdown(rep: Report, tmp: str) -> None:
    """倒计时到点必须翻到揭晓页，而且**是由主线程翻的**。

    2026-09-20 补的洞：以前 ``_tick`` 在归零时只把秒数写成 "0" 就 return，
    真正翻页靠倒计时线程里的 ``root.after(0, self._show_reveal)`` ——
    那是在跨线程碰 Tcl 解释器，一旦抛::

        RuntimeError: main thread is not in main loop

    线程就崩了，页面永远停在 0：看起来就是"程序卡死"。
    现在翻页由每 200ms 一次的主线程循环负责，这里真跑一遍确认。

    ⚠ 用**演示模式**（``real_shutdown=False``）跑：这个分支绝不会真的
    下达关机命令，也不会碰 ``shutdown /a``。
    """
    rep.head("[4] 倒计时归零 → 揭晓页（演示模式，不会真的关机）")
    state_store.set_state_dir_for_tests(tmp)
    os.environ["LOCALAPPDATA"] = tmp

    import tkinter as tk
    import main as prank

    root = tk.Tk()
    app = prank.App(root, real_shutdown=False, seconds=1, show_image=False,
                    check_updates=False, state=state_store.empty_state())
    rep.check("构造出来是演示模式（不会真下达关机命令）",
              app.real_shutdown is False)
    app._enter_countdown()
    rep.check("进入倒计时页", app.mode == "countdown", "(实际 %r)" % app.mode)

    deadline = time.time() + 12.0
    while time.time() < deadline and not app._expired:
        root.update()
        time.sleep(0.03)
    rep.check("倒计时到点后翻到了揭晓页（不会停在 0 秒）", app._expired)

    def texts(widget) -> list:
        found = []
        try:
            if "text" in widget.keys():
                found.append(str(widget.cget("text")))
        except tk.TclError:
            pass
        for child in widget.winfo_children():
            found.extend(texts(child))
        return found

    page = texts(root)
    rep.check("揭晓页真的渲染出来了（页面上有「骗你的」）",
              any("骗你的" in t for t in page), repr(page[:2]))
    rep.check("揭晓页说清了电脑没事、没调用关机",
              any("没有任何事" in t for t in page), repr(page[:2]))
    rep.check("揭晓页把正确答案也写出来了（学习目的）",
              any("正确答案" in t for t in page), repr(page[:2]))

    app._cancel_pending()
    root.destroy()


def main() -> int:
    # 日志写进文件而不是 stderr：这一支测试**故意**制造"状态文件损坏"这类情况，
    # 那几条 warning 是预期内的，混在 stdout 里只会把真正的失败淹掉。
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        filename=os.path.join(HERE, "_serial_log.txt"),
        filemode="w",
        encoding="utf-8",
    )

    rep = Report()
    rep.add("=" * 72)
    rep.add("序列号 / 月度闸门 / 图片工具 回归")
    rep.add("APP_VERSION = %s" % __import__("version_check").APP_VERSION)
    rep.add("REPO_HINT_URL = %s" % __import__("main").REPO_HINT_URL)
    rep.add("=" * 72)

    tmp = tempfile.mkdtemp(prefix="haavik_serial_")
    try:
        test_serial(rep)
        test_cli_pause(rep)
        test_state(rep, os.path.join(tmp, "state"))
        test_migration(rep, os.path.join(tmp, "mig"))
        test_gate(rep, os.path.join(tmp, "gate"))
        test_countdown(rep, os.path.join(tmp, "cd"))
        test_image_tool(rep, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    rep.add("")
    rep.add("=" * 72)
    if rep.failures:
        rep.add("结论：%d 项失败 ✗" % len(rep.failures))
        for item in rep.failures:
            rep.add("    - %s" % item)
    else:
        rep.add("结论：序列号、月度闸门、倒计时揭晓、图片工具全部通过 ✓")
    if rep.warnings:
        rep.add("（有 %d 条警告，不计入结论）" % len(rep.warnings))
    rep.add("=" * 72)

    text = "\n".join(rep.lines) + "\n"
    out = os.path.join(HERE, "_serial.txt")
    try:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        print("[WARN] 报告写不出去：%s" % exc)
    # 生成物要进仓库：本机绝对路径一律换成占位符（硬性约定 12）。
    # 这一支尤其需要 —— 它故意制造"状态文件损坏"，日志里会打满临时目录真路径。
    try:
        import _redact
        _redact.scrub_tree(HERE)
    except ImportError:
        pass
    try:
        print(text)
    except UnicodeEncodeError:
        pass
    return 1 if rep.failures else 0


if __name__ == "__main__":
    sys.exit(main())
