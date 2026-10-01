# -*- coding: utf-8 -*-
"""AI 修图那一页的界面逻辑 —— 从 ``image_tool.py`` 拆出来的一块。

为什么单独一个文件
==================================================================
``image_tool.py`` 一度涨到 1600 行，越过"单文件 ≤1500 行"这条线。
拆的边界就按**功能分组**切：工具栏、查看器、胶片条、编辑、保存、状态栏留在
``image_tool``；「AI」按钮、下拉菜单、能力跑批、模型下载/校验、抠图弹窗这一坨
整体搬到本文件。两边只通过 ``self`` 上的既有属性/方法打交道，没有来回调。

为什么是 Mixin 而不是一个独立对象
==================================================================
这些方法**本来就是 ImageTool 的方法**：它们用 ``self.image``、``self._show_status``、
``self._apply_edit``、``self._ai_button``，而这些是界面的状态。做成
``AiToolMixin`` 让 ``ImageTool`` 直接继承，方法体一个字都不用改、
所有调用点（``main.py`` / 各测试 / layout_test 的 ``tool._build_matte_brush_body``）
也一个都不用改 —— 搬迁本身不引入任何行为差异。

硬约束（与 image_tool 一致）
==================================================================
  * 不碰网络：联网 / 下载 / 解包全在 ``ai_ops``（纯本地）与 ``main.py``（联网），
    本文件只通过 ``self.on_download_ai_models`` / ``on_check_ai_update`` /
    ``on_install_ai_pack_local`` 这三个回调请求它们干活；
  * **任何工作线程都不许碰 Tcl**：AI 能力跑在后台线程，结果只能经 ``queue``
    回到主线程（见 :meth:`AiToolMixin._run_ai_async`）。
"""
from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from tkinter import messagebox

import ai_ops
import image_command
import image_dialogs as dialogs
import image_ops
from ui_theme import font

log = logging.getLogger("haavik.ai_tool")

#: AI 结果写进撤销栈时用的标签前缀。
#: 单独提成常量，是因为**有三处**必须用同一个值：写入（AI 结果落地）、
#: 识别（"上一次是不是 AI 做的"，决定菜单里要不要出现「退回这一次 AI」）、
#: 以及测试。三处各写各的字面量，改一处就会静默地漏掉另一处。
AI_EDIT_PREFIX = "AI·"


class AiToolMixin:
    """「AI 修图」的界面逻辑。由 ``image_tool.ImageTool`` 继承，不单独实例化。"""

    # ==================================================================
    # AI 修图（纯本地；联网 / 下载 / 解包都通过 on_download_ai_models 回调给 main.py）
    # ==================================================================
    # ------------------------------------------------------------------
    # AI 修图入口：**用系统原生菜单**（2026-09-21 按用户要求回退到老版本做法）
    #
    # 为什么不再自己画一个弹层
    # ==================================================================
    # 1.7.6 把老版的 ``tk.Menu`` 换成了自绘的 ``Toplevel(overrideredirect=True)``，
    # 结果在用户的机器上"点 AI 完全没反应"。自绘无边框弹层要自己算屏幕坐标、
    # 自己管 ``-topmost``、自己处理"鼠标移开就收起"——每一环都依赖打包进去那套
    # Tcl/Tk 的具体版本，换个环境就可能不显示。
    #
    # 更坑的是：**窗口对象确实建出来了**（``winfo_exists()`` 为真、照片里也能看到），
    # 所以"查对象在不在"的测试全都是绿的，而用户屏幕上什么都没有。
    # 教训：断言要盯"看得见"，不能盯"对象存在"。
    #
    # 原生 ``tk.Menu``：位置、层级、键盘上下选、点菜单外面收起、Esc 收起，
    # 全由 Tk 自己管。我们只负责"往里面放哪几项"，不碰任何窗口机制。
    # 代价是不跟暗色主题 —— 跟"能不能用"比，不值一提。
    # ------------------------------------------------------------------
    def _ai_toggle_menu(self):
        """点「AI」按钮：弹出能力菜单。

        名字沿用 ``toggle``（``image_tool`` 注册的就是它），但"开 / 关"交给 Tk：
        菜单已弹出时再点按钮，Tk 自己会先把旧的收掉。这里只做"建菜单 + 在哪弹"。
        """
        if not ai_ops.ai_available():
            messagebox.showinfo("AI 修图", ai_ops.support_reason())
            return
        btn = getattr(self, "_ai_button", None)
        if btn is None:
            return
        try:
            menu = self._ai_build_menu()
            x = btn.winfo_rootx()
            y = btn.winfo_rooty() + btn.winfo_height() + 2
            try:
                menu.tk_popup(x, y)
            finally:
                menu.grab_release()
        except Exception as exc:                       # noqa: BLE001
            # 失败要响（项目硬约定）：打成窗口程序后 stderr 没人看，
            # 悄悄吞掉就正好是用户说的"点了完全没反应"。
            log.exception("AI 菜单弹出失败")
            messagebox.showerror(
                "AI 修图",
                "菜单没能弹出来。\n\n请把下面这句话告诉作者：\n%s: %s"
                % (type(exc).__name__, exc))

    def _ai_build_menu(self) -> "tk.Menu":
        """把当前该给的能力 / 入口拼成一个 ``tk.Menu``。

        单独一个方法是为了**能测**：不用真的弹窗，也能断言"菜单里到底有哪几项"
        （见 ``image_tool_test`` 的 AI 菜单段）。
        """
        menu = tk.Menu(self, tearoff=0, font=font(10))
        # ---- 「一句话改图」放最前面 ----
        # 这是"对话式修图"的第一版（粗糙版）：**不接模型**，就是一张
        # "说法 → 动作"的对照表。所以它**不依赖 AI 模型在不在**，永远可点。
        # 详见 image_command 的模块说明（粗糙版是精细版的骨架）。
        menu.add_command(label="用一句话改图…（说「调亮一点」「把背景去掉」）",
                         command=self._show_command)
        menu.add_separator()
        if self.on_install_ai_pack_local is not None:
            menu.add_command(label="从本地文件安装 AI 模型包…（选 haavik_ai.pack）",
                             command=self._prompt_ai_install_local)
            menu.add_separator()
        # 能力行：能用权重跑起来的 + 不需要权重的画笔抠图。
        # **没下模型时也要有一项能点**——否则用户点开「AI」只看到"去下载"，
        # 体感就是"AI 打不开"。（2026-09-21 修）
        for cap in ai_ops.menu_caps():
            menu.add_command(label=ai_ops.CAP_LABELS[cap],
                             command=lambda c=cap: self._run_ai_cap(c))
        # 「背景虚化 / 换背景」：复用抠图模型出的透明通道，纯 PIL 合成，
        # **不新增任何模型体积**（见 image_ops.composite_over_blur / flatten）。
        # 它们依赖抠图权重，所以没下模型时也要像其它模型能力一样拦住。
        menu.add_separator()
        menu.add_command(label="背景虚化（主体清晰、背景模糊）",
                         command=self._bg_blur)
        menu.add_command(label="换成白底",
                         command=lambda: self._bg_replace((255, 255, 255)))
        menu.add_command(label="换成蓝底（证件照）",
                         command=lambda: self._bg_replace((0, 71, 171)))
        menu.add_command(label="换成红底（证件照）",
                         command=lambda: self._bg_replace((220, 30, 40)))
        menu.add_separator()
        # ★ 把"你现在是哪一版、要不要更新"直接写在菜单上。
        #
        # 为什么值得为这一行改三个文件：用户（浩然自己就是）会问
        # "可以更新 AI 吗？" —— 因为**界面上根本没有这个信息**，
        # 想知道只能点一下（还得等它联网查完再弹窗）。
        # 菜单是**同步**拼的，绝不能在这里联网，所以只用本地已知的两样：
        # 已装版本（准确）+ 上次查到的远端版本（缓存，可能过时）。
        # 措辞由 ai_ops.ai_menu_status 统一负责 —— 缓存为空时它会说
        # "点此检查更新"，**不会**拿旧值冒充"已是最新"。
        # 没装时它给出的是"下载 AI 模型组件…"，所以两个分支共用同一份文案。
        # ⚠ 版本号**只在模型真在的时候才算数**：状态文件里记着 1.4.0，但
        #   models/ 被手工删了、被杀软清了、或上次装到一半 —— 这时候说
        #   "AI 模型 v1.4.0" 就是**撒谎**。以文件为准，状态只当参考。
        _present = ai_ops.models_present()
        _label, _has_new = ai_ops.ai_menu_status(
            ai_ops.ai_component_version() if _present else "",
            ai_ops.ai_remote_version())
        menu.add_command(label=_label, command=self._prompt_ai_download)
        if not ai_ops.models_present():
            menu.add_command(label="查看支持的能力", command=self._show_ai_caps_info)
        else:
            if not ai_ops.available_caps():
                # 组件装了但没有可用权重：如实说，并给"重新下载"这条出口。
                menu.add_command(
                    label="组件已装，但没找到可用模型（点此重新下载）",
                    command=self._prompt_ai_download)
            # 删除模型：只在"真的装了"的时候出现，而且**如实标出能拿回多少空间**
            # （不写体积的话，用户不知道点它值不值）。
            _sz = ai_ops.human_size(ai_ops.models_size_bytes())
            menu.add_command(label="删除 AI 模型…（释放 %s）" % _sz,
                             command=self._prompt_ai_delete)

        # ---- 「AI 做的有问题，想退回去」 ----
        # 机制其实**早就有**：AI 的结果走的是同一条 _apply_edit，
        # 所以它本来就在撤销栈里（Ctrl+Z 能退，状态栏还会写「已撤销：AI·超分放大」）。
        # 实测确认过（probe_ai_undo.py）。**缺的不是能力，是"让人知道"** ——
        # 没人会想到"AI 那一步也能撤"。所以把它摆成菜单里的一项，
        # 位置就在他刚点过 AI 的地方，并且**写清楚会退掉哪一步**。
        # ⚠ 只在"上一次操作确实是 AI"时才出现：平时多一项会让人以为点了会发生什么。
        back = self.last_ai_edit_label()
        if back:
            menu.add_separator()
            menu.add_command(label="不满意？退回这一次 AI 的结果（%s）" % back,
                             command=self.undo)
        return menu

    def last_ai_edit_label(self):
        """撤销栈顶如果是 AI 留下的，返回它的能力名（如 ``超分放大``），否则 None。

        只读、不改任何状态 —— 菜单要靠它决定"要不要显示『退回去』那一项"。
        """
        stack = getattr(self, "_undo_stack", None) or []
        if not stack:
            return None
        try:
            label = str(stack[-1][0] or "")
        except (IndexError, TypeError):
            return None
        if not label.startswith(AI_EDIT_PREFIX):
            return None
        return label[len(AI_EDIT_PREFIX):] or "AI"

    # ------------------------------------------------------------------
    # 一句话改图（对话式修图的第一版）
    # ------------------------------------------------------------------
    def _show_command(self):
        """收一句话 → 解析 → 落到图上。

        ⚠ 这个窗口是**模态**的（``BaseDialog.show()`` 里有 ``wait_window``），
        所以这里会同步等到它关掉为止。**写测试时必须先排一个看门狗**去关窗，
        否则调用方就挂在那儿了（这条在 serial_test 里踩过，注释也留在那边）。
        """
        dlg = image_command.CommandDialog(self)
        dlg.show()
        cmd = dlg.take()
        if cmd is None:
            return
        ok, msg = image_command.apply_command(self, cmd)
        # 失败也要吭声：窗口程序没有控制台，静默就等于"点了没反应"
        self._show_status(("完成：" if ok else "没做成：") + str(msg))

    def _prompt_ai_download(self):
        """点「重新下载 / 更新 AI 模型」：先联网查有没有新版本，再决定怎么问。

        已装过 → 先走 ``on_check_ai_update`` 拿远端版本对比：
          * 有更新：问"要下载更新吗？"；
          * 已是最新版：先说"已是最新版"，再问"确定要重新安装吗？"
            （重新安装用来修复可能损坏的文件）；
          * 查更新失败：把**具体原因**（网络不通／服务器出错／程序内部出错）
            说清楚，再退回到"仍要重新安装吗？"。
        没装过 → 直接问"要下载吗？"（谈不上"是否最新"）。
        """
        if self.on_download_ai_models is None:
            messagebox.showinfo(
                "AI 模型下载",
                "当前版本未接入 AI 模型下载通道，请更新到支持 AI 的版本。")
            return
        if not ai_ops.models_present():
            if not messagebox.askyesno(
                    "下载 AI 模型",
                    "AI 模型组件约 90 MB（纯本地推理，不会把你的图片上传到任何"
                    "服务器）。\n\n下载到本机后需解包并校验完整性，是否现在下载？"):
                return
            self.on_download_ai_models(on_done=self._on_ai_download_done)
            return
        # 已经装过：先查有没有新版本，再决定怎么问（这一步不下载）。
        if self.on_check_ai_update is None:
            # 没接查更新通道就退回原来的直接确认
            if not messagebox.askyesno(
                    "更新 AI 模型",
                    "AI 模型组件已就绪。要重新下载以更新到最新版本吗？"):
                return
            self.on_download_ai_models(on_done=self._on_ai_download_done)
            return
        self._show_status("正在检查 AI 模型是否有更新…")
        self.update_idletasks()
        self.on_check_ai_update(on_done=self._on_ai_check_done)

    def _prompt_ai_delete(self):
        """点「删除 AI 模型」：**两步确认**，确认完才真删。

        两步是有意的，不是啰嗦：

          1. ``messagebox`` 提醒"会删什么、能不能还原" —— 只要点一下确定；
          2. 再弹一个窗口，**必须亲手勾选**才能点「确定删除」。

        菜单里「重新下载 / 更新」和「删除」紧挨着，只靠一个"确定"按钮挡不住
        手抖。勾选是**一个动作**（手要移过去、眼睛要看一下那句话），
        比"再点一次确定"扎实得多。

        删除本身走 :func:`ai_ops.delete_models` —— 它进**回收站**（可还原）、
        只删 ``models/`` 目录、并且会先放掉模型文件句柄（否则 Windows 上
        "文件被占用"根本删不掉）。
        """
        size_text = ai_ops.human_size(ai_ops.models_size_bytes())

        # ---- 第一步：弹框提醒 ----
        if not messagebox.askokcancel(
                "删除 AI 模型",
                "接下来会删掉程序自己下载的 AI 模型（%s）。\n\n"
                "删掉后：超分放大、降噪、抠图、图片修复都用不了，"
                "想用时要重新下载。\n"
                "你的照片和别的文件不会被动。\n\n"
                "点「确定」后还需要你再勾选一次才算真的删。" % size_text,
                icon="warning"):
            return

        # ---- 第二步：勾选确认 ----
        # 用一个可变字典接结果：对话框是模态的（show() 里有 wait_window），
        # 这里会一直等到它关掉，之后才读得到结论。
        decision = {"ok": False}
        dlg = dialogs.DeleteModelsDialog(
            self, size_text=size_text,
            on_ok=lambda: decision.__setitem__("ok", True))
        dlg.show()
        if not decision["ok"]:
            return

        # ---- 真删 ----
        ok, msg = ai_ops.delete_models()
        if ok:
            messagebox.showinfo("AI 模型", msg)
        else:
            # 失败必须说出来（含"没有被删除"），否则用户会以为已经删了。
            messagebox.showerror("删除失败", msg)

    def _on_ai_check_done(self, needs_update, remote_ver, installed_ver, error):
        """``on_check_ai_update`` 的回调：拿到"有没有新版本"后再弹确认框。"""
        if error is not None:
            # 查更新失败：**原因由 main.py 翻译好再传进来**（可能是网络不通，
            # 也可能是程序内部出错）—— 这里不预设是网络问题。
            if messagebox.askyesno(
                    "检查更新失败",
                    "没能确认 AI 模型组件是否有新版本：\n\n%s\n\n"
                    "仍要重新下载并安装 AI 模型吗？" % error):
                self.on_download_ai_models(on_done=self._on_ai_download_done)
            return
        if needs_update:
            if messagebox.askyesno(
                    "AI 模型有更新",
                    "AI 模型组件有新版本 v%s（你当前是 v%s）。\n\n"
                    "要下载并更新吗？" % (remote_ver, installed_ver)):
                self.on_download_ai_models(on_done=self._on_ai_download_done)
            return
        # 已是最新版：先告诉用户，再问"确定要重新安装吗？"
        messagebox.showinfo(
            "AI 模型已是最新版",
            "AI 模型组件已是最新版本（v%s）。" % (installed_ver or "?"))
        if messagebox.askyesno(
                "确定要重新安装吗？",
                "当前已是最新版本。\n\n确定要重新下载并覆盖安装一遍吗？"
                "（用于修复可能损坏或被杀软误删的文件）"):
            self.on_download_ai_models(on_done=self._on_ai_download_done)

    def _on_verify_ai_click(self):
        """点「校验 AI 完整性」：本地比对已装模型文件，不联网。"""
        if not ai_ops.models_present():
            messagebox.showinfo(
                "校验 AI 完整性",
                "AI 模型组件还没下载/安装，先点「AI 修图」下载或安装模型包。")
            return
        self._show_status("正在校验 AI 完整性…")
        self.update_idletasks()
        ok, msg = ai_ops.verify_ai_integrity()
        if ok:
            messagebox.showinfo("校验 AI 完整性", msg)
        else:
            messagebox.showwarning("校验 AI 完整性", msg)

    def _prompt_ai_install_local(self):
        """让用户选一个 ``haavik_ai.pack``，交给 main.py 本地解包安装。

        不联网、不经过发布通道：适合"发布通道还没上 AI 组件"或想离线装的场景。
        """
        if self.on_install_ai_pack_local is None:
            messagebox.showinfo(
                "AI 模型安装",
                "当前版本未接入本地安装通道，请更新到支持 AI 的版本。")
            return
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="选择 AI 模型包（haavik_ai.pack）",
            filetypes=[("AI 模型包", "*.pack"), ("所有文件", "*.*")])
        if not path:
            return
        self._show_status("正在安装 AI 模型…")
        self.update_idletasks()
        self.on_install_ai_pack_local(path, on_done=self._on_ai_download_done)

    def _show_ai_caps_info(self):
        caps = "\n".join("· " + v for v in ai_ops.CAP_LABELS.values())
        messagebox.showinfo(
            "AI 修图支持的能力",
            "下载并解包 AI 模型后，以下能力都在你的电脑上本地运行：\n\n" + caps)

    def _run_ai_async(self, work, on_done, *, interval=80):
        """后台线程跑 ``work()``，结果回**主线程**交给 ``on_done(res)``。

        ⚠ 线程边界必须这么划：**绝不能在工作线程里调 ``self.after``**。
        那是在跨线程碰 Tcl 解释器，本机实测会抛::

            RuntimeError: main thread is not in main loop

        线程一崩，``on_done`` 就永远等不到 —— 界面停在「AI 处理中…」，
        用户看到的是"**AI 修图点了没反应**"。

        做法与 ``main.py`` 的 ``_run_in_background`` 同一套：工作线程只往
        ``queue`` 里放结果，``after`` 轮询由**主线程**发起，两层不越界。

        ``work`` 抛异常时按 (False, 人话原因) 交给 ``on_done`` —— 与各 AI
        能力函数统一的返回值形状一致，调用方不需要再分两种情况。
        """
        box = queue.Queue()

        def runner():
            try:
                box.put(("done", work()))
            except Exception as exc:                       # noqa: BLE001
                log.warning("后台 AI 任务失败：%s", exc)
                box.put(("error", exc))

        def poll():
            try:
                kind, payload = box.get_nowait()
            except queue.Empty:
                try:
                    if self.winfo_exists():
                        self.after(interval, poll)
                except tk.TclError:
                    pass
                return
            if kind == "error":
                on_done((False, "AI 处理出错：%s" % payload))
            else:
                on_done(payload)

        threading.Thread(target=runner, name="ai-task", daemon=True).start()
        self.after(interval, poll)        # ← 由主线程发起，不越线程边界

    def _run_ai_cap(self, cap):
        """在后台线程跑某个 AI 能力，跑完回到主线程替换工作图。"""
        if self.image is None:
            self._show_status("先打开一张图，再用 AI 修图")
            return
        # 没下模型时只挡住**真的需要权重**的能力；画笔抠图纯手动画笔，
        # 不该被 97MB 的下载卡住（否则"AI 菜单里一项都不能用"）。
        if not ai_ops.models_present() and ai_ops.cap_needs_model(cap):
            self._show_status("AI 模型尚未下载，先点「AI 修图」下载")
            return
        if cap == ai_ops.CAP_SELECT_MATTE:
            self._run_selective_matte()
            return
        if cap == ai_ops.CAP_BRUSH_MATTE:
            self._run_brush_matte()
            return
        label = ai_ops.CAP_LABELS.get(cap, cap)
        self._show_status("AI 处理中：%s…" % label)
        self.update_idletasks()
        fn = {
            ai_ops.CAP_SUPER_RES: ai_ops.super_resolve,
            ai_ops.CAP_DENOISE: ai_ops.denoise,
            ai_ops.CAP_FACE: ai_ops.face_restore,
            ai_ops.CAP_MATTE: ai_ops.matting,
            ai_ops.CAP_ENHANCE: ai_ops.enhance,
            ai_ops.CAP_INPAINT: ai_ops.inpaint,
            ai_ops.CAP_COLORIZE: ai_ops.colorize,
        }.get(cap)
        if fn is None:
            self._show_status("未知 AI 能力：%s" % cap)
            return

        # 走统一的异步助手：它保证结果只从主线程回来（见 _run_ai_async 的说明）
        self._run_ai_async(lambda: fn(self.image),
                           lambda res: self._on_ai_done(cap, res))

    def _on_ai_done(self, cap, res):
        if not isinstance(res, tuple) or len(res) != 2:
            self._show_status("AI 处理返回了异常结果")
            return
        ok, payload = res
        if not ok:
            self._show_status(str(payload))
            return
        label = ai_ops.CAP_LABELS.get(cap, cap)
        self._apply_edit(payload, AI_EDIT_PREFIX + label)
        # 顺口告诉人家"这一步是能退的" —— 不说的话，没人会想到去翻撤销栈。
        self._show_status("AI 完成：%s · 想退回去就再点一下「AI」" % label)

    def _bg_blur(self, radius=None):
        """背景虚化：先抠图拿到透明主体，再合成到「原图高斯模糊版」上。"""
        if self.image is None:
            self._show_status("先打开一张图，再用 AI 修图")
            return
        if not ai_ops.models_present():
            self._show_status("AI 模型尚未下载，先点「AI 修图」下载")
            return
        self._show_status("AI 处理中：背景虚化（自动抠图中）…")
        self.update_idletasks()
        orig = self.image
        if radius is None:
            # 模糊半径随图大小走：太小看不出、太大主体也糊。2% 边长是个平衡点。
            radius = max(8, int(min(orig.size) * 0.02))
        self._run_ai_async(
            lambda: ai_ops.matting(orig),
            lambda res: self._on_bg_done(
                res, "背景虚化",
                lambda rgba: image_ops.composite_over_blur(rgba, orig, radius)))

    def _bg_replace(self, color):
        """换背景色：先抠图，再把主体合成到纯色底（证件照换底等）。"""
        if self.image is None:
            self._show_status("先打开一张图，再用 AI 修图")
            return
        if not ai_ops.models_present():
            self._show_status("AI 模型尚未下载，先点「AI 修图」下载")
            return
        name = ("换成白底" if color == (255, 255, 255) else
                "换成蓝底" if color == (0, 71, 171) else
                "换成红底" if color == (220, 30, 40) else "换背景色")
        self._show_status("AI 处理中：%s（自动抠图中）…" % name)
        self.update_idletasks()
        orig = self.image
        self._run_ai_async(
            lambda: ai_ops.matting(orig),
            lambda res: self._on_bg_done(
                res, name, lambda rgba: image_ops.flatten(rgba, color)))

    def _on_bg_done(self, res, label, compose):
        """抠图完成后的统一回调：把透明主体按 ``compose`` 合成、落地、入撤销栈。"""
        if not isinstance(res, tuple) or len(res) != 2:
            self._show_status("AI 处理返回了异常结果")
            return
        ok, payload = res
        if not ok:
            self._show_status(str(payload))
            return
        try:
            out = compose(payload)
        except Exception as exc:                                  # noqa: BLE001
            # 窗口程序没有控制台，合成出错必须弹出来，不能静默。
            self._show_status("%s 失败：%s" % (label, exc))
            return
        self._apply_edit(out, AI_EDIT_PREFIX + label)
        self._show_status("AI 完成：%s · 想退回去就再点一下「AI」" % label)

    def _on_ai_download_done(self, success, msg):
        if success:
            messagebox.showinfo(
                "AI 模型",
                "%s\n\n现在可以打开「AI 修图」直接使用各项能力了。" % msg)
        else:
            messagebox.showerror("AI 模型下载失败", str(msg))

    def _run_selective_matte(self):
        """选择性抠图：先后台自动抠好整图，再弹画笔弹窗让用户精修。"""
        if self.image is None:
            self._show_status("先打开一张图，再用 AI 修图")
            return
        if not ai_ops.models_present():
            self._show_status("AI 模型尚未下载，先点「AI 修图」下载")
            return
        self._show_status("AI 处理中：选择性抠图（自动抠图中）…")
        self.update_idletasks()
        orig = self.image

        # 同样走统一的异步助手（自动抠图较慢，且绝不能让线程崩在 after 上）
        self._run_ai_async(lambda: ai_ops.matting(orig), self._on_matte_ready)

    def _on_matte_ready(self, res):
        if not isinstance(res, tuple) or len(res) != 2:
            self._show_status("AI 处理返回了异常结果")
            return
        ok, payload = res
        if not ok:
            self._show_status(str(payload))
            return
        base_rgba = payload
        orig_rgb = self.image.convert("RGB")

        def on_ok(rgba):
            self._apply_edit(rgba, "AI·选择性抠图")
            self._show_status("AI 完成：选择性抠图")

        dialogs.MatteBrushDialog(
            self, base_rgba=base_rgba, orig_rgb=orig_rgb,
            on_ok=on_ok).show()

    def _run_brush_matte(self):
        """画笔抠图：不跑模型，直接从"原图全不透明"起步，用户手动画笔擦掉背景。

        和「选择性抠图」（先自动抠、再精修）的区别：这里没有任何自动结果，
        完全靠用户手动把主体留下来 / 把背景擦掉，适合自动抠图翻车、或想完全
        自己控制的场景。
        """
        if self.image is None:
            self._show_status("先打开一张图，再用 AI 修图")
            return
        orig_rgb = self.image.convert("RGB")
        base_rgba = orig_rgb.copy().convert("RGBA")   # 起点：整图都在（alpha=255）
        self._show_status("画笔抠图：在弹窗里刷掉不要的背景")

        def on_ok(rgba):
            self._apply_edit(rgba, "AI·画笔抠图")
            self._show_status("AI 完成：画笔抠图")

        dialogs.MatteBrushDialog(
            self, base_rgba=base_rgba, orig_rgb=orig_rgb, on_ok=on_ok,
            title="画笔抠图 · 手动精细化",
            hint="从整图开始：用「擦除」画笔刷掉不要的背景，误删的主体用「恢复」"
                 "画笔补回；点「应用到图片」完成。").show()

    def _build_matte_brush_body(self, top):
        """layout_test 用：把选择性抠图弹窗内容渲染进裸 Toplevel 测高度。"""
        from PIL import Image
        if self.image is not None:
            base = self.image.convert("RGBA")
            orig = self.image.convert("RGB")
        else:
            base = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
            orig = Image.new("RGB", (800, 600), (30, 30, 30))
        dialogs.MatteBrushForm(top, base_rgba=base, orig_rgb=orig,
                               on_ok=lambda *_: None, on_cancel=lambda: None)

