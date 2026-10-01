# -*- coding: utf-8 -*-
"""填发信凭据的小窗口 —— 运行它，填三个框，当场就能试发一封。

为什么做这个窗口
======================================================================
凭据本来可以手写进 `feedback_smtp.txt`，但那要求使用者认识路径、会跑命令、
还得小心别把授权码贴到别的地方去。一个窗口省事得多：

  * 三个输入框（发信邮箱 / 授权码 / 收件邮箱），已有的值会自动读出来预填；
  * **「试发一封」—— 真发**。这一步最值钱：「能不能发出去」应该在填的
    当场就知道，而不是等朋友点了「直接发送」才发现不行；
  * **「保存并生成」** —— 写 `feedback_smtp.txt` 并生成
    `optimized/_smtp_secret.py`（两个文件都在 .gitignore 里，不会进仓库）。

它**不进安装包**：只在作者这台机器上跑，而且用的就是程序里那套发送代码
（``feedback_send.send_via_smtp_with``）—— 所以"这里通了"＝"程序里也通"。

界面用的是程序自己那套自绘控件（ui_theme），免得又多一套长得不一样的窗口。
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
OPTIMIZED = os.path.join(HERE, "optimized")
if OPTIMIZED not in sys.path:
    sys.path.insert(0, OPTIMIZED)

import feedback_send as fb                                  # noqa: E402
import make_smtp_secret as gen                              # noqa: E402
import ui_theme as T                                        # noqa: E402

WIN_W, WIN_H = 640, 520
# ⚠️ 高度别往下调：430 时底部的按钮会被挤到窗口外面（截图里看到的，
# 这就是"构建了但点不到按钮"那一类问题）。改完记得截一张图确认按钮在。 


class SetupWindow:
    """一个窗口三件事：读旧值 → 试发 → 保存。"""

    def __init__(self, root: tk.Tk, *, front: bool = True):
        """``front=False`` 时不抢前台/不置顶 —— 给自动化测试用，
        否则跑一次测试就在使用者屏幕上弹一个窗口出来。"""
        self.root = root
        self.box: queue.Queue = queue.Queue()
        self.busy = False
        #: 保存成功之后就不再抢置顶了（那时用户可能要看邮箱）
        self.saved = False

        root.title("HAAVIK 反馈发信设置")
        root.configure(bg=T.BG_APP)
        root.geometry("%dx%d" % (WIN_W, WIN_H))
        root.resizable(False, False)
        try:
            T.dark_titlebar(root)
        except tk.TclError:
            pass

        body = tk.Frame(root, bg=T.BG_APP)
        body.pack(fill="both", expand=True, padx=20, pady=(16, 14))

        tk.Label(body, text="反馈发信设置", bg=T.BG_APP, fg=T.FG_TEXT,
                 font=T.font(13, "bold"), anchor="w").pack(fill="x")
        tk.Label(body, text="把 QQ 邮箱生成的 SMTP 授权码填进来（不是登录密码）。\n"
                            "这些值只写在这台机器上，不会上传到任何地方。",
                 bg=T.BG_APP, fg=T.FG_DIM, font=T.font(9), anchor="w",
                 justify="left").pack(fill="x", pady=(4, 12))

        old = self._read_existing()
        self.user = tk.StringVar(value=old.get("SMTP_USER")
                                 or "2495938027@qq.com")
        self.password = tk.StringVar(value="")
        self.to = tk.StringVar(value=old.get("SMTP_TO")
                               or old.get("SMTP_USER")
                               or "2495938027@qq.com")

        self.fields = {}
        self._row(body, "发信邮箱", self.user, "用哪个邮箱把反馈发出去")
        self._row(body, "授 权 码", self.password,
                  "QQ 邮箱「设置 → 账户 → POP3/IMAP/SMTP 服务」里生成的那串")
        self._row(body, "收件邮箱", self.to,
                  "填自己 = 自己发给自己，反馈就落在你这个邮箱里")

        self.status = tk.Label(body, text="填好后点「试发一封」——真的会发一封信，"
                                          "收到就说明通了。",
                               bg=T.BG_APP, fg=T.FG_DIM, font=T.font(9),
                               anchor="w", justify="left", wraplength=WIN_W - 60)
        self.status.pack(fill="x", pady=(14, 0))

        row = tk.Frame(body, bg=T.BG_APP)
        row.pack(fill="x", side="bottom")
        self.test_btn = T.RoundButton(row, "试发一封", self.on_test,
                                      kind="accent", height=34, pad_x=18)
        self.test_btn.pack(side="left")
        self.save_btn = T.RoundButton(row, "保存并生成", self.on_save,
                                      kind="solid", height=34, pad_x=18)
        self.save_btn.pack(side="left", padx=(10, 0))
        T.RoundButton(row, "关闭", root.destroy, kind="solid", height=34,
                      pad_x=16).pack(side="right")

        root.after(200, lambda: self.fields["授 权 码"].entry.focus_set())

        # 启动时做三件事，都是"让使用者看得见这个窗口"：
        #   * 摆到屏幕中间偏上 —— 不写坐标的话 Windows 会随手挑个位置；
        #   * **一直置顶** —— 实测踩过：不置顶时它会被编辑器/浏览器整片盖住，
        #     使用者说"我没看到窗口"，而现象和"程序没起来"一模一样。
        #     这是个一次性设置窗口（填完就关），宁可它一直在最前，
        #     也不要让人找不到它。保存成功后会自动取消置顶（见 on_save）。
        #   * 写一个记录文件，把"真的起来了、窗口在哪、是否可见"落下来 ——
        #     这样"没看到窗口"这件事有据可查，不用猜。
        root.update_idletasks()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry("%dx%d+%d+%d" % (WIN_W, WIN_H,
                                       max(0, (sw - WIN_W) // 2),
                                       max(0, (sh - WIN_H) // 3)))
        if front:
            root.lift()
            root.attributes("-topmost", True)
            # 实测踩过：光设一次 -topmost 不够 —— 这台机器上它会被别的程序
            # （编辑器 / 浏览器 / 本对话窗口）抢回去，于是使用者说"我没看到窗口"。
            # 两招一起上：
            #   ① 启动时"先最小化再恢复"—— 这一步在 Windows 上会强制窗口回到前台；
            #   ② 之后每隔几秒重申一次置顶（只在前 60 秒，之后交给用户自己安排）。
            root.iconify()
            root.after(400, self._force_front)
            root.after(700, self._keep_front, 0)
            try:
                root.focus_force()
            except tk.TclError:
                pass
        root.after(900, self._write_alive)

    def _force_front(self):
        """把窗口顶到最前（最小化→恢复是 Windows 上最可靠的一招）。"""
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.focus_force()
        except tk.TclError:
            pass

    def _keep_front(self, tick):
        """**一直**重申置顶，直到窗口被保存过或关掉。

        ⚠️ 曾经写成"只在前 60 秒重申"—— 结果使用者在 60 秒后才去看，
        置顶早被别的程序抢回去了，他说"我完全看不到弹窗"。
        一次性设置窗口宁可一直挡在最前面，也不要让人找不到。
        """
        if self.saved:
            return
        try:
            self.root.attributes("-topmost", True)
        except tk.TclError:
            return
        self.root.after(3000, self._keep_front, tick + 1)

    def _un_topmost(self):
        try:
            self.root.attributes("-topmost", False)
        except tk.TclError:
            pass

    def _write_alive(self):
        """记下"窗口真的起来了、在哪"。

        为什么留这个：窗口被别的窗口挡住时，使用者会说"没打开" ——
        而这句话和"程序崩了"在现象上一模一样。有这个文件就能区分。
        """
        try:
            geo = (self.root.winfo_x(), self.root.winfo_y(),
                   self.root.winfo_width(), self.root.winfo_height())
            with open(os.path.join(HERE, "_gui_alive.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write("pid=%d\n窗口位置(x,y,宽,高)=%s\n可见=%s\n"
                         % (os.getpid(), geo, bool(self.root.winfo_viewable())))
        except (OSError, tk.TclError):
            pass

    # ---------- 界面零件 ----------
    def _read_existing(self) -> dict:
        if not os.path.isfile(gen.SRC):
            return {}
        try:
            with open(gen.SRC, "r", encoding="utf-8") as fh:
                return gen.parse(fh.read())
        except OSError:
            return {}

    def _row(self, parent, label, var, hint):
        row = tk.Frame(parent, bg=T.BG_APP)
        row.pack(fill="x", pady=(0, 10))
        tk.Label(row, text=label, bg=T.BG_APP, fg=T.FG_TEXT,
                 font=T.font(9), width=8, anchor="w").pack(side="left")
        entry = T.RoundEntry(row, textvariable=var, width=330, height=32,
                             justify="left", font_size=10)
        entry.pack(side="left", padx=(6, 10))
        self.fields[label] = entry
        tk.Label(row, text=hint, bg=T.BG_APP, fg=T.FG_MUTED,
                 font=T.font(8), anchor="w").pack(side="left")
        return entry

    def say(self, text, ok=None):
        color = T.FG_DIM if ok is None else (T.FG_OK if ok else T.FG_BAD)
        self.status.configure(text=text, fg=color)

    # ---------- 后台跑，别把窗口卡住 ----------
    def _run(self, work, label):
        """SMTP 要连好几秒 —— 必须在后台线程跑，否则窗口会「卡死」。

        （用户刚因为另一个服务"一直卡死"吃过亏，这个窗口绝不能犯同样的毛病。）
        """
        if self.busy:
            # 点了没反应最让人恼火（尤其是这种"等十几秒"的动作）——
            # 必须说一句正在忙，而不是静默丢掉这一次点击。
            self.say("上一个动作还在跑，等它出结果再点。", ok=False)
            return
        self.busy = True

        def runner():
            try:
                self.box.put(("done", work()))
            except Exception as exc:                        # noqa: BLE001
                self.box.put(("fail", exc))

        self.say("%s…（最多等十几秒）" % label)
        threading.Thread(target=runner, daemon=True).start()
        self.root.after(120, self._poll)

    def _poll(self):
        try:
            kind, payload = self.box.get_nowait()
        except queue.Empty:
            self.root.after(120, self._poll)
            return
        self.busy = False
        if kind == "fail":
            self.say("出错了：%s" % payload, ok=False)
            return
        ok, detail = payload
        self.say(detail or ("成功" if ok else "失败"), ok=bool(ok))
        if self.saved:
            # 保存过了 → 不再抢置顶（用户可能要切去看邮箱）
            self._un_topmost()

    # ---------- 两个动作 ----------
    def on_test(self):
        user = self.user.get().strip()
        password = self.password.get().strip()
        to = self.to.get().strip()
        if "@" not in user:
            self.say("发信邮箱看起来不对。", ok=False)
            return
        if not password:
            self.say("先把授权码粘进来（16 个字符那串，不是登录密码）。", ok=False)
            return

        def work():
            return fb.send_via_smtp_with(
                user, password, to,
                text="这是 HAAVIK Photo 反馈通道的一次自检。\n"
                     "如果你看到这封信，说明「软件里点一下就发到作者邮箱」"
                     "这条路是通的。\n",
                subject="【HAAVIK】反馈通道自检")

        self._run(work, "正在发测试邮件")

    def on_save(self):
        user = self.user.get().strip()
        password = self.password.get().strip()
        to = self.to.get().strip()
        if "@" not in user or not password:
            self.say("发信邮箱和授权码都要填。", ok=False)
            return
        if "@" not in to:
            self.say("收件邮箱看起来不对。", ok=False)
            return

        def work():
            lines = [
                "# 反馈发信凭据 —— 这个文件在 .gitignore 里，不会进仓库。",
                "# 由 smtp_setup_gui.py 写入；也可以手改（改完要重新生成一次）。",
                "",
                "SMTP_USER=%s" % user,
                "SMTP_PASS=%s" % password,
                "SMTP_TO=%s" % to,
                "SMTP_HOST=",
                "",
            ]
            with open(gen.SRC, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines))
            # 生成"能打进 exe"的那个模块；它只回显长度，不打印授权码
            import io
            import contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = gen.main()
            if code != 0:
                return False, "生成失败：%s" % " / ".join(buf.getvalue().split())
            # ⚠️ 这里在**工作线程**里：只能改一个布尔值，不能碰 tk
            # （连 root.after 也不行 —— Tk 不是线程安全的）。
            # 真正取消置顶由主线程的 _poll 做。
            self.saved = True
            return True, ("已保存并生成好了：\n"
                          "%s\n%s\n\n现在关掉这个窗口，跟助手说一声"
                          "（它会构建 + 发版）。" % (gen.SRC, gen.DST))

        self._run(work, "正在保存")


def main() -> int:
    root = tk.Tk()
    SetupWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
