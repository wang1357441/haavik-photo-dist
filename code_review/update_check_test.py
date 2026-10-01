# -*- coding: utf-8 -*-
"""版本提示 + 更新下载 的回归测试。

为什么要有这一支
==================================================================
``version_check.py`` 是**本项目唯一的联网点**，而它的输出会改变界面，
并且（配置了清单地址之后）会往磁盘落一个安装包。这三件事都属于
"改了别的代码可能悄悄弄坏、但肉眼很难发现"的类型：

* 界面通路坏了 —— 平时看到的只是"没有新版"，不会有人发现提示其实永远不显示；
* 比对逻辑错了 —— 要么对新版本视而不见（漏报），要么把同版本报成新版本（误报）；
* **下载校验坏了 —— 这条最危险**：sha256 没认真比对就等于"谁来都能塞个包给你"，
  而表面上"更新成功"看起来一切正常。

判定口径
==================================================================
**外网是否可达不算测试失败。** 锚点 `gist.githubusercontent.com` 在部分网络下不可达，
而那不是这个项目能控制的事；把它算作失败，只会让发布流程随机变红，
久而久之被当成"噪音"忽略掉 —— 那比没有测试更糟。所以：

    外网不通          -> 打 [警告]，并**跳过**依赖外网的断言（报告里明确写"未判定"）
    界面/逻辑/校验错  -> 打 [失败]，退出码非 0

第四阶段（清单与下载）**不依赖外网**：它在本机起一个 http 服务，
把"取清单 → 下载 → 核对 sha256 → 拒绝被篡改的包"整条链路真的跑一遍。
127.0.0.1 是 version_check 里唯一放行的明文 http 地址，原因就是这一节。

用法
==================================================================
    ~/.workbuddy/cache/hvphoto/venv2/Scripts/python.exe code_review/update_check_test.py

报告写在 `code_review/_update.txt`（已被 .gitignore 排除）。
"""
from __future__ import annotations

import functools
import gc
import hashlib
import http.server
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "code_review", "optimized"))

import main as prank          # noqa: E402
import state_store            # noqa: E402
import version_check as vc    # noqa: E402

FAKE_NEWER = "9.9.9"
NET_RETRIES = 3


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def add(self, text: str = "") -> None:
        self.lines.append(text)

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

    def warn(self, text: str) -> None:
        self.warnings.append(text)
        self.lines.append("  [警告] %s" % text)

    def skip(self, text: str) -> None:
        self.lines.append("  [跳过] %s（未判定）" % text)


def pump(root: tk.Tk, seconds: float) -> None:
    """推进 Tk 事件循环若干秒（代替 mainloop，便于测试里断言）。"""
    end = time.time() + seconds
    while time.time() < end:
        try:
            root.update()
        except tk.TclError:
            return
        time.sleep(0.05)


def close_root(root: tk.Tk) -> None:
    """销毁窗口，并立刻**在主线程里**把可能成环的 Tk 对象回收干净。

    这不是洁癖，是一处实测到的崩溃换来的：tkinter 的控件与回调之间很容易
    构成引用环，环上的对象只能靠 gc 回收，而 gc 会在**任意线程**触发
    （哪个线程在分配内存就在哪个线程跑）。这一支测试后面会起一个 http 服务线程，
    只要 Tcl 解释器恰好被那个线程回收掉，Tcl 就会直接 abort 整个进程：

        Tcl_AsyncDelete: async handler deleted by the wrong thread

    表现极具误导性 —— 测试什么结果都没输出、退出码是个负数，
    看起来像"环境坏了"，其实只是回收线程不对。
    """
    try:
        root.destroy()
    except tk.TclError:
        pass
    gc.collect()


def make_app(root: tk.Tk, *, check_updates: bool) -> "prank.App":
    return prank.App(root, real_shutdown=False, seconds=3600, show_image=False,
                     check_updates=check_updates, state=state_store.empty_state())


# --------------------------------------------------------------------------
# 1. 界面通路
# --------------------------------------------------------------------------
def test_ui_wiring(rep: Report) -> None:
    """注入一个更高版本，验证 UI 真的会变（不联网）。"""
    rep.add("")
    rep.add("-" * 72)
    rep.add("[1] 界面通路（注入 %s，不联网）" % FAKE_NEWER)

    root = tk.Tk()
    # check_updates=False：本阶段只验 UI，不要真的发请求，保证确定性
    app = make_app(root, check_updates=False)

    title0 = root.title()
    label0 = app.update_label.cget("text") if app.update_label else "<无>"
    rep.add("    注入前 title=%r  label=%r" % (title0, label0))
    rep.check("注入前不显示任何版本提示",
              title0 == prank.APP_TITLE and label0 == "")

    app._on_update_result(FAKE_NEWER)
    pump(root, 1.0)

    title1 = root.title()
    label1 = app.update_label.cget("text") if app.update_label else "<无>"
    rep.add("    注入后 title=%r" % title1)
    rep.add("           label=%r" % label1)
    rep.check("标题栏出现新版本", FAKE_NEWER in title1)
    rep.check("页面上出现新版本", FAKE_NEWER in label1)
    rep.check("提示里标明当前版本", vc.APP_VERSION in label1)

    # 没有配置清单地址时，按钮应该是"怎么拿到新版？"而不是"下载更新"
    btn_text = app.update_button.cget("text") if app.update_button else "<无>"
    rep.add("    按钮文字=%r（MANIFEST_URL=%r）" % (btn_text, vc.MANIFEST_URL))
    if vc.MANIFEST_URL:
        rep.check("配置了清单地址时按钮是「下载更新」", btn_text == "下载更新")
    else:
        rep.check("未配置清单地址时按钮是「怎么拿到新版？」", btn_text == "怎么拿到新版？")
        rep.check("未配置清单地址时点击按钮不会去下载（只弹说明）",
                  app._downloading is False)

    close_root(root)


# --------------------------------------------------------------------------
# 2. 真实外网 + 比对逻辑
# --------------------------------------------------------------------------
def test_logic(rep: Report) -> tuple[bool, str | None]:
    """打真实锚点，验证比对逻辑。返回 (外网是否可用, 远端版本号)。"""
    rep.add("")
    rep.add("-" * 72)
    rep.add("[2] 真实外网 + 比对逻辑（TIMEOUT=%.1fs，最多试 %d 次）"
            % (vc.TIMEOUT, NET_RETRIES))

    raw = None
    for attempt in range(1, NET_RETRIES + 1):
        t0 = time.time()
        try:
            raw = vc.fetch_latest()
            rep.add("    第 %d 次：%.2fs  取回 %r" % (attempt, time.time() - t0, raw))
            break
        except Exception as exc:  # noqa: BLE001
            rep.add("    第 %d 次：%.2fs  失败 %s: %s"
                    % (attempt, time.time() - t0, type(exc).__name__, exc))

    if raw is None:
        rep.warn("锚点不可达（重试 %d 次）—— 外网相关断言全部跳过，不计入结论"
                 % NET_RETRIES)
        rep.skip("远端锚点可读")
        rep.skip("旧版本能看到新版本")
        rep.skip("同版本不误报")
        return False, None

    rep.check("远端锚点可读", True)
    rep.check("旧版本(1.0.0) 能看到新版本", vc.check_for_update("1.0.0") == raw)
    rep.check("同版本不误报", vc.check_for_update(raw) is None)
    return True, raw


def test_full_path(rep: Report, net_ok: bool) -> None:
    """check_updates=True 跑完整链路：不误报、不崩、不留常驻定时器。

    锚点**刻意指向本机 http 服务**（内容就是当前版本号）：既毫秒级返回，
    也不受外网影响。之前这里是等 ``vc.TIMEOUT + 4`` 秒然后断言"轮询已结束"，
    一旦配上多个远端地址（403 + 超时），轮询还没跑完就断言 —— 随机变红。
    依赖网络快慢的测试等于噪音，所以改成不依赖外网。
    """
    rep.add("")
    rep.add("-" * 72)
    rep.add("[3] 完整链路（check_updates=True，锚点指向本机服务）")

    serve_dir = tempfile.mkdtemp(prefix="haavik_anchor_")
    with open(os.path.join(serve_dir, "version.txt"), "w", encoding="utf-8") as fh:
        fh.write(vc.APP_VERSION + "\n")
    httpd, base = _serve(serve_dir)
    old_env = os.environ.get(vc.ENV_VERSION_URL)
    old_channel = vc._CHANNEL
    os.environ[vc.ENV_VERSION_URL] = base + "/version.txt"
    vc._CHANNEL = None                    # 别把上一阶段选中的通道带进来
    try:
        root = tk.Tk()
        app = make_app(root, check_updates=True)
        pump(root, 4)

        title = root.title()
        label = app.update_label.cget("text") if app.update_label else "<无>"
        rep.add("    title=%r  label=%r" % (title, label))

        # 本地版本 == 远端版本时必须是"没有提示"，不能误报
        rep.check("本地==远端时不误报（%s）" % vc.APP_VERSION,
                  title == prank.APP_TITLE and label == "")
        rep.check("轮询已结束，没有留下常驻定时器",
                  not root.tk.call("after", "info"))
        close_root(root)
    finally:
        if old_env is None:
            os.environ.pop(vc.ENV_VERSION_URL, None)
        else:
            os.environ[vc.ENV_VERSION_URL] = old_env
        vc._CHANNEL = old_channel
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(serve_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# 4. 清单 + 下载（本机 http 服务，不依赖外网）
# --------------------------------------------------------------------------
class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:      # 测试输出保持干净
        pass


def _serve(directory: str):
    handler = functools.partial(_Quiet, directory=directory)
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, "http://127.0.0.1:%d" % httpd.server_address[1]


def _fake_installer(path: str) -> tuple[str, int]:
    """造一个"安装包"：内容是确定性的，方便算 sha256。"""
    blob = b"MZ" + bytes(range(256)) * 40
    with open(path, "wb") as fh:
        fh.write(blob)
    return hashlib.sha256(blob).hexdigest(), len(blob)


def test_manifest_and_download(rep: Report, tmp: str) -> None:
    rep.add("")
    rep.add("-" * 72)
    rep.add("[5] 清单 + 下载 + sha256 校验（本机 http 服务，127.0.0.1）")

    serve_dir = os.path.join(tmp, "www")
    os.makedirs(serve_dir, exist_ok=True)
    pkg = os.path.join(serve_dir, "Setup.exe")
    good_sha, good_size = _fake_installer(pkg)
    httpd, base = _serve(serve_dir)
    rep.add("    本地服务：%s（目录 %s）" % (base, serve_dir))

    try:
        # ---- 地址白名单 ----
        rep.check("https 一律放行", vc._allowed("https://example.com/a"))
        rep.check("回环 http 放行（测试用的口子）", vc._allowed(base + "/x"))
        rep.check("普通 http 拒绝（明文更新包等于送给别人改）",
                  not vc._allowed("http://example.com/a"))
        rep.check("其它协议拒绝", not vc._allowed("ftp://example.com/a"))

        def write_manifest(name: str, data: dict) -> str:
            path = os.path.join(serve_dir, name)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            return base + "/" + name

        full = {"version": "9.9.9", "url": base + "/Setup.exe",
                "sha256": good_sha, "size": good_size, "notes": "测试"}

        # ---- 清单解析 ----
        url = write_manifest("manifest.json", full)
        man = vc.fetch_manifest(url)
        rep.check("清单能取回并解析", man["version"] == "9.9.9")
        rep.check("清单里的 sha256 被规范成小写",
                  man["sha256"] == good_sha.lower())
        rep.check("manifest_is_newer(9.9.9) = True", vc.manifest_is_newer(man))
        rep.check("manifest_is_newer(0.0.1) = False",
                  not vc.manifest_is_newer({"version": "0.0.1"}))

        # ---- 各种坏清单必须被拒 ----
        # 注意每个用例都给**完整**的字典：用 dict(default, **over) 那种写法，
        # "缺字段"这个用例会被默认值悄悄补全，于是一条永远不会失败的空断言。
        bad_cases = (
            ("缺 size 字段", {k: v for k, v in full.items() if k != "size"}),
            ("缺 sha256 字段", {k: v for k, v in full.items() if k != "sha256"}),
            ("size 不是正整数", dict(full, size=0)),
            ("size 是字符串", dict(full, size="10242")),
            ("sha256 长度不对", dict(full, sha256="abc")),
            ("sha256 不是十六进制", dict(full, sha256="z" * 64)),
            ("下载地址是普通 http", dict(full, url="http://example.com/Setup.exe")),
            ("下载地址是 ftp", dict(full, url="ftp://example.com/Setup.exe")),
            ("版本号解析不了", dict(full, version="not-a-version")),
            ("顶层是数组", [1, 2, 3]),
        )
        for index, (label, data) in enumerate(bad_cases):
            bad_url = write_manifest("bad_%d.json" % index, data)
            try:
                vc.fetch_manifest(bad_url)
                rep.check("拒绝坏清单：%s" % label, False, "(居然通过了)")
            except (ValueError, KeyError, TypeError) as exc:
                rep.check("拒绝坏清单：%s" % label, True, "(%s)" % type(exc).__name__)

        # ---- 正常下载 ----
        dest = os.path.join(tmp, "dl_ok")
        ok, detail = vc.download_update(man, dest_dir=dest)
        rep.check("正常下载成功", ok, detail if not ok else "")
        if ok:
            rep.check("落地文件名带版本号（不会互相覆盖）",
                      os.path.basename(detail) == "Setup-9.9.9.exe",
                      "(实际 %r)" % os.path.basename(detail))
            with open(detail, "rb") as got, open(pkg, "rb") as want:
                rep.check("下载内容与源文件字节一致", got.read() == want.read())
            rep.check("落地的 sha256 与清单一致",
                      vc.sha256_file(detail) == good_sha)
            rep.check("没有留下 .part 临时文件",
                      not [n for n in os.listdir(dest) if n.endswith(".part")])

        # ---- 下载进度：界面靠它画进度条，所以必须有序、单调、收尾进校验 ----
        seen = []

        def on_prog(done, total, phase):
            seen.append((done, total, phase))

        okp, detailp = vc.download_update(man, dest_dir=os.path.join(tmp, "dl_prog"),
                                          on_progress=on_prog)
        rep.check("带进度回调时照样下载成功", okp, detailp if not okp else "")
        rep.check("进度回调被调用过", bool(seen), "(%d 次)" % len(seen))
        if seen:
            downs = [s for s in seen if s[2] == "download"]
            phases = sorted(set(s[2] for s in seen))
            rep.check("进度阶段只有 download / verify", phases == ["download", "verify"],
                      repr(phases))
            rep.check("已下字节单调不减",
                      all(b[0] <= a[0] for b, a in zip(downs, downs[1:])),
                      repr([d[0] for d in downs][:8]))
            rep.check("最后一次下载进度正好等于总字节",
                      bool(downs) and downs[-1][0] == downs[-1][1] == good_size,
                      repr(downs[-1] if downs else None))
            rep.check("收尾一定会报一次 verify（进度条满了要切到校验阶段）",
                      seen[-1][2] == "verify", repr(seen[-1]))

        def bad_prog(done, total, phase):
            raise RuntimeError("假装界面挂了")

        okb, detailb = vc.download_update(
            man, dest_dir=os.path.join(tmp, "dl_badprog"), on_progress=bad_prog)
        rep.check("进度回调抛异常时下载仍然成功（画进度条不能毁掉下载）", okb,
                  detailb if not okb else "")

        # ---- 清理积压的更新包：只删"用过/半截"的，别的绝不动 ----
        pr = os.path.join(tmp, "updates")
        os.makedirs(pr)
        for n in ("Setup-1.0.0.exe", "Setup-1.4.0.exe", "Setup-1.4.1.exe",
                  "Setup-1.5.0.exe", "Setup-1.2.0.exe.part",
                  "Setup-1.3.0.exe.part", "别动我.txt"):
            with open(os.path.join(pr, n), "wb") as fh:
                fh.write(b"x")
        # 1.2.0 那个半截包装成"好多天前的"（过期的该清）；
        # 1.3.0 那个保持"刚写的"（新鲜的必须留着给断点续传）
        _stale = os.path.join(pr, "Setup-1.2.0.exe.part")
        _old_t = time.time() - vc.PART_KEEP_SECONDS - 3600
        os.utime(_stale, (_old_t, _old_t))
        os.makedirs(os.path.join(pr, "Setup-9.9.9.exe"))   # 同名目录：不许当文件删
        saved = vc.update_dir
        vc.update_dir = lambda: pr                         # 只改这一处，很明确
        try:
            removed = vc.prune_updates("1.4.1")
        finally:
            vc.update_dir = saved
        rep.check("清掉了不高于当前版本的旧包",
                  set(removed) >= {"Setup-1.0.0.exe", "Setup-1.4.0.exe",
                                   "Setup-1.4.1.exe"},
                  repr(sorted(removed)))
        rep.check("比当前版本新的包留着（用户可能刚下好还没装）",
                  os.path.isfile(os.path.join(pr, "Setup-1.5.0.exe")))
        rep.check("**过期的**半截包清掉（放了好多天、不会再续传了）",
                  not os.path.exists(os.path.join(pr, "Setup-1.2.0.exe.part")))
        # ⚠ 规则在 2026-09-22 改过：以前是"任何 .part 一律清"，
        # 但那样会把**断点续传的命根子**清掉 —— 下到一半关掉程序、
        # 下次打开想接着下，靠的就是这个半截文件。
        # 现在只清过期的（vc.PART_KEEP_SECONDS），新鲜的一律留着。
        rep.check("**新鲜的**半截包必须留着（清了它断点续传就废了）",
                  os.path.isfile(os.path.join(pr, "Setup-1.3.0.exe.part")))
        rep.check("新鲜半截包不在删除名单里",
                  "Setup-1.3.0.exe.part" not in removed)
        rep.check("不是我们下的文件绝不动",
                  os.path.isfile(os.path.join(pr, "别动我.txt")))
        rep.check("同名目录不会被当成文件删掉",
                  os.path.isdir(os.path.join(pr, "Setup-9.9.9.exe")))

        # ---- 被篡改的包必须被拒，而且不留垃圾 ----
        tampered = os.path.join(tmp, "dl_bad_sha")
        ok2, why2 = vc.download_update(dict(man, sha256="0" * 64), dest_dir=tampered)
        rep.check("sha256 不匹配时下载被拒", not ok2, "(%s)" % why2)
        rep.check("被拒时不留下任何文件（含 .part）",
                  not os.path.exists(tampered)
                  or not os.listdir(tampered),
                  "(目录内容 %r)" % (os.listdir(tampered) if os.path.isdir(tampered) else None,))

        tampered2 = os.path.join(tmp, "dl_bad_size")
        ok3, why3 = vc.download_update(dict(man, size=good_size + 1), dest_dir=tampered2)
        rep.check("体积不符时下载被拒", not ok3, "(%s)" % why3)

        # ---- 超大体积的清单根本不启动下载 ----
        ok4, why4 = vc.download_update(
            dict(man, size=vc.MAX_DOWNLOAD_BYTES + 1), dest_dir=os.path.join(tmp, "dl_huge"))
        rep.check("清单声明的体积超上限时直接拒绝",
                  not ok4 and "上限" in why4, "(%s)" % why4)

        # ---- 清单地址没配置时，必须明确报错而不是静默 ----
        # 注意：`fetch_manifest("")` 里的空串会被 `target = url or MANIFEST_URL`
        # 当成 falsy 而回退到常量 —— 常量非空时它就会真的去请求网络。
        # 所以这里必须把常量本身置空，再无参调用，测的才是"未配置"这条路径。
        saved_url = vc.MANIFEST_URL
        vc.MANIFEST_URL = ""
        try:
            vc.fetch_manifest()
            rep.check("MANIFEST_URL 为空时 fetch_manifest 明确报错", False, "(居然通过了)")
        except ValueError:
            rep.check("MANIFEST_URL 为空时 fetch_manifest 明确报错", True)
        finally:
            vc.MANIFEST_URL = saved_url
        # ---- 网络通道：自己找路（直连 → 系统代理 → 本机常见端口） ----
        # 注意 Report.check 的签名是 (label, cond, extra) —— 标签在前。
        cands = vc.channel_candidates()
        rep.check("通道候选的第一个是直连", cands[0][1] is None, str(cands[0]))
        names = [c[1] for c in cands]
        rep.check("候选里没有重复的通道",
                  len(names) == len(set(names)), "%d 个候选" % len(names))
        rep.check("候选里包含本机常见代理端口",
                  any(c[1] and "127.0.0.1" in c[1] for c in cands))
        rep.check("探测通道的超时比正式请求短（试探要快速失败）",
                  vc.PROBE_TIMEOUT < vc.TIMEOUT)

        # 刚才取本地服务已经成功过 —— 通道应该已经选定，而且是直连
        rep.check("取到本地服务后选中的是直连通道",
                  vc._CHANNEL is not None and vc._CHANNEL[1] is None,
                  str(vc._CHANNEL))

        # 服务器回 404 说明"通道是通的"，不能继续去试代理
        vc._CHANNEL = None
        try:
            vc._get(base + "/definitely-missing.json", 5.0, 100)
            rep.check("不存在的路径应当 404", False)
        except urllib.error.HTTPError as exc:
            rep.check("不存在的路径返回 404", exc.code == 404, str(exc.code))
        except Exception as exc:                         # noqa: BLE001
            rep.check("不存在的路径应当 404", False, repr(exc))
        rep.check("404 也算通道可用（不再白试一遍代理）",
                  vc._CHANNEL is not None, str(vc._CHANNEL))

        # 通道记不住时不影响功能：清掉缓存再取一次，仍应成功。
        # 这里显式用本地服务的地址 —— 测的是"重新选路"这件事本身，
        # 不该依赖外网是否可达（远端 404 会把异常带上来，那是另一码事）。
        vc._CHANNEL = None
        ok_after = vc.fetch_manifest(base + "/manifest.json")
        rep.check("清掉通道缓存后仍能取到清单（会重新选路）",
                  ok_after.get("version") is not None)

        # ---- 更新源可覆盖（验证 / 应急用的口子） ----
        os.environ[vc.ENV_VERSION_URL] = "http://127.0.0.1:9/version.txt"
        rep.check("HAAVIK_VERSION_URL 能覆盖版本锚点首选",
                  vc.version_urls()[0] == "http://127.0.0.1:9/version.txt",
                  vc.version_urls()[0])
        rep.check("覆盖之后默认地址仍在候选里（兜底不丢）",
                  vc.VERSION_URL in vc.version_urls())
        os.environ[vc.ENV_MANIFEST_URL] = "http://127.0.0.1:9/manifest.json"
        rep.check("HAAVIK_MANIFEST_URL 能覆盖清单首选",
                  vc._manifest_targets(None)[0] == "http://127.0.0.1:9/manifest.json")
        del os.environ[vc.ENV_VERSION_URL]
        del os.environ[vc.ENV_MANIFEST_URL]
        rep.check("删掉环境变量后回到默认锚点",
                  vc.version_urls()[0] == vc.VERSION_URL)
        rep.check("删掉环境变量后回到默认清单地址",
                  vc._manifest_targets(None)[0] == vc.MANIFEST_URL)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_manual_check(rep: Report) -> None:
    """[4] 用户亲手点「检查更新」：三种结果各说各的话（2026-09-20 补的洞）。

    之前只测了"启动时自动查"（见 [3]），没测这个按钮。而手动那条路把后台任务
    的 ``work`` 写成了**不收参数**的 lambda，而 ``_run_in_background`` 内部是
    ``work(report)`` —— 于是每一轮都抛::

        TypeError: <lambda>() takes 0 positional arguments but 1 was given

    那句异常又被当成"网络不通"报给用户。症状极具误导性：
    **"明明已经是最新版，却提示网络不通"**。所以这一支必须真点一次按钮，
    并且断言"最新版"和"网络不通"是两句话。

    锚点一律指向本机 http 服务（内容由本支自己写），
    既毫秒级返回、不受外网影响，也能覆盖"远端更高"这条分支。
    """
    rep.add("")
    rep.add("-" * 72)
    rep.add("[4] 手动点「检查更新」（本机服务，不依赖外网）")
    serve_dir = tempfile.mkdtemp(prefix="haavik_manual_")
    anchor = os.path.join(serve_dir, "version.txt")
    with open(anchor, "w", encoding="utf-8") as fh:
        fh.write(vc.APP_VERSION + "\n")      # ①一开始：本地就是最新版
    httpd, base = _serve(serve_dir)

    saved_urls = vc.version_urls            # 只改这一处，很明确
    saved_channel = vc._CHANNEL
    vc.version_urls = lambda: [base + "/version.txt"]

    root = None
    try:
        root = tk.Tk()
        # check_updates=False：关掉启动自动查，这一支考的就是手动这条路
        app = make_app(root, check_updates=False)
        app._show_tool(first_time=False)
        pump(root, 0.8)

        def click_and_wait(seconds: float) -> str:
            vc._CHANNEL = None              # 每轮重新选路，别把上一轮的状态带进来
            app.tool._st_msg.configure(text="")
            app._update_from_tool()         # ← 就是那个按钮点下去做的事
            pump(root, seconds)
            return app.tool._st_msg.cget("text")

        # ---- ① 本地 == 远端：必须说"已是最新版本"，绝不能说网络不通 ----
        got = click_and_wait(3.0)
        rep.add("    ①本地==远端   -> %r" % got)
        rep.check("本地就是最新版时说「已是最新版本」",
                  "已是最新版本" in got, repr(got))
        rep.check("本地就是最新版时**不**说网络不通",
                  "网络不通" not in got, repr(got))
        rep.check("最新版提示里带上了自己的版本号",
                  vc.APP_VERSION in got, repr(got))

        # ---- ② 远端更高：必须报"发现新版本" ----
        with open(anchor, "w", encoding="utf-8") as fh:
            fh.write(FAKE_NEWER + "\n")
        got = click_and_wait(3.0)
        rep.add("    ②远端更高     -> %r" % got)
        rep.check("远端更高时报「发现新版本」并给出版本号",
                  "发现新版本" in got and FAKE_NEWER in got, repr(got))
        rep.check("发现有新版时不说网络不通", "网络不通" not in got, repr(got))
        btn = app.tool._update_btn.cget("text")
        rep.check("发现有新版时入口变成「下载更新 x.y.z」",
                  "下载更新" in btn and FAKE_NEWER in btn, repr(btn))

        # ---- ③ 真的连不上：这时才说"网络不通" ----
        app._pending_update = None          # ②刚把它设上了，③要考的是"查"而不是"下"
        vc.version_urls = lambda: ["http://127.0.0.1:9/version.txt"]  # discard 端口，必然被拒
        # 通道候选只留直连：否则这一轮要把 PROXY_PORTS 里 10 个端口挨个探一遍
        # （每个最多 PROBE_TIMEOUT 秒），测试会慢得没道理。
        # 被测的仍然是**真的网络层失败**（URLError），只是把"换通道"这件事去掉。
        saved_cands = vc.channel_candidates
        vc.channel_candidates = lambda: [("直连", None)]
        try:
            got = click_and_wait(6.0)
        finally:
            vc.channel_candidates = saved_cands
        rep.add("    ③真的连不上   -> %r" % got)
        rep.check("真的连不上时明说「网络不通」", "网络不通" in got, repr(got))

        # ---- ④ AI 组件查更新：失败也必须回调（不能静默卡住） ----
        # 曾经的写法把 ``except ... as exc`` 里的 exc 直接包进 after 回调，
        # 而那个名字在 except 块结束时**已被 Python 删掉** → 回调里
        # `NameError: cannot access free variable 'exc'` → on_done 永远不被调用。
        # 界面表现极具误导性：点了「更新 AI 模型」，卡在「正在检查…」，
        # 然后**什么都没发生**。（2026-09-20 修）
        import ai_ops
        seen_calls: list = []
        saved_fetch_manifest = vc.fetch_manifest

        def boom(*_a, **_k):
            raise OSError("模拟：取不到更新清单")

        vc.fetch_manifest = boom
        try:
            app._check_ai_update(lambda *a: seen_calls.append(a))
            pump(root, 2.0)
        finally:
            vc.fetch_manifest = saved_fetch_manifest
        rep.add("    ④AI 查更新失败 -> on_done 被调用 %d 次" % len(seen_calls))
        rep.check("AI 组件查更新失败时 on_done 真的被调用（不会静默卡住）",
                  len(seen_calls) == 1, repr(seen_calls))
        if len(seen_calls) == 1:
            _needs, _rv, _iv, err = seen_calls[0]
            if ai_ops.ai_supported():
                rep.check("失败原因带了出来，且指明「网络不通」",
                          err is not None and "网络不通" in str(err), repr(err))
            else:
                rep.skip("失败原因文案（本机 AI 不受支持，走的是另一条分支）")

        # ---- ⑤ 归因判定本身：程序自己的错不许甩锅给网络 ----
        rep.check("连接类异常算网络问题",
                  vc.is_network_error(urllib.error.URLError("boom")))
        rep.check("超时算网络问题",
                  vc.is_network_error(TimeoutError("timed out")))
        rep.check("TypeError **不**算网络问题（就是它曾经被误报）",
                  not vc.is_network_error(TypeError("takes 0 positional arguments")))
        rep.check("服务器 404 不算网络问题（路是通的，别乱试代理）",
                  not vc.is_network_error(
                      urllib.error.HTTPError("u", 404, "Not Found", {}, None)))
        rep.check("内部错误翻成「程序内部出错」",
                  "程序内部出错" in vc.describe_check_failure(TypeError("x")))
        rep.check("连接错误翻成「网络不通」",
                  vc.describe_check_failure(urllib.error.URLError("x")) == "网络不通")
        rep.check("空内容（没异常也没版本号）单独有一句话",
                  vc.describe_check_failure(None) == "更新源返回了空内容")
    finally:
        vc.version_urls = saved_urls
        vc._CHANNEL = saved_channel
        if root is not None:
            close_root(root)
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(serve_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# 6. 竞速取锚点（2026-09-25 新增）
# --------------------------------------------------------------------------
#: 让"某条地址"按需变坏/变慢的测试服务。
#
# ⚠⚠ 这里有个必须记住的坑（我第一版就踩了）：
#   **mode 绝不能挂在 handler 类的类属性上** —— 多个 server 共用同一个
#   handler 类，后设的 mode 会覆盖先设的，于是几个用例全返回同一个结果，
#   测试"通过"了但什么都没验到。
#   → mode 挂在 **server 实例**上（`self.server.mode`），互不干扰。
class _MoodyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:                       # noqa: N802
        mode = getattr(self.server, "mode", "good")
        if mode == "good":
            self.send_response(200)
            body = FAKE_NEWER.encode()
        elif mode == "bad_json":
            # 秒回的一段错误页：**回得快但内容不合格**。
            # 这正是竞速最危险的情况（"越快越糟"）——
            # 如果只看"谁先返回"，它就会赢。
            self.send_response(200)
            body = b"<html><body>Service Unavailable</body></html>"
        elif mode == "stale":
            # 缓存里的旧版本号：格式合法但比 APP_VERSION 旧。
            # ⚠ 竞速**只看格式**、不看新旧，所以这条是允许"赢"的 ——
            #   它格式合格。真实里由"jsdelivr 排末位"来防（见下面的断言）。
            self.send_response(200)
            body = b"0.0.1"
        elif mode == "http429":
            self.send_response(429)
            body = b""
        elif mode == "slow":
            time.sleep(getattr(self.server, "delay", 2.0))
            self.send_response(200)
            body = FAKE_NEWER.encode()
        else:
            self.send_response(500)
            body = b""
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def _serve_mode(mode: str, delay: float = 2.0):
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _MoodyHandler)
    httpd.daemon_threads = True
    httpd.mode = mode                 # ← 挂实例，不挂类
    httpd.delay = delay
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:%d/v" % httpd.server_address[1]


def test_race(rep: Report) -> None:
    rep.add("")
    rep.add("-" * 72)
    rep.add("[6] 竞速取锚点：谁先答**对**用谁（本机 http 服务）")

    servers = []
    saved_urls = vc.DEFAULT_VERSION_URLS
    saved_env = os.environ.pop(vc.ENV_VERSION_URL, None)

    def mk(mode, delay=2.0):
        s, u = _serve_mode(mode, delay)
        servers.append(s)
        return u

    good = mk("good")
    junk = mk("bad_json")
    slow = mk("slow", delay=2.0)
    err = mk("http429")

    def run(urls, timeout=6.0):
        vc.DEFAULT_VERSION_URLS = tuple(urls)
        t0 = time.monotonic()
        try:
            return vc.fetch_latest(timeout), time.monotonic() - t0, None
        except Exception as exc:                    # noqa: BLE001
            return None, time.monotonic() - t0, exc

    try:
        # ---- ① 正常：一遍就拿到 ----
        val, dt, exc = run([good, good])
        rep.check("两个好地址 -> 拿到正确版本号",
                  val == FAKE_NEWER and exc is None, repr(val))

        # ---- ② ★核心：错误页秒回，但绝不能赢 ----
        # junk 是**秒回**的（不睡），good 也是秒回。
        # 两者速度相近；即使 junk 侥幸先到，也必须被 _accept 拦下。
        for _ in range(3):                          # 多跑几次，避开偶发时序
            val, dt, exc = run([junk, good])
            if val != FAKE_NEWER:
                break
        rep.check("★错误页秒回时不许赢，仍由好地址正确应答",
                  val == FAKE_NEWER,
                  "拿到 %r（若为错误页正文说明 _accept 没拦住）" % val)

        # ---- ③ ★竞速的意义：慢/坏的地址不许拖累整体 ----
        # slow 要睡 2 秒；如果没有竞速，串行下用户就得等它。
        val, dt, exc = run([slow, good])
        rep.check("★有慢地址在场时，耗时 ≈ 快的那条（不被慢的拖住）",
                  val == FAKE_NEWER and dt < 1.5,
                  "耗时 %.2fs（慢地址需 2s）" % dt)

        val, dt, exc = run([err, good])
        rep.check("429 在第一位也不影响结果",
                  val == FAKE_NEWER and exc is None,
                  "%.2fs" % dt)

        # ---- ④ 全坏：必须抛错，绝不静默返回空 ----
        val, dt, exc = run([err, err], timeout=3.0)
        rep.check("★全坏时抛异常，绝不返回空（返回空=更新功能静默失效）",
                  val is None and exc is not None, repr(exc))
        rep.check("全坏时异常里带着**真实**原因（不是光秃秃一句超时）",
                  exc is not None and ("429" in str(exc) or "Too Many" in str(exc)),
                  repr(str(exc)[:60]) if exc else "")

        val, dt, exc = run([junk, junk], timeout=3.0)
        rep.check("全是不合格内容时也抛异常",
                  val is None and exc is not None, repr(exc))

        # ---- ⑤ 单地址时退回老行为（验证用的环境变量只指一个地址）----
        os.environ[vc.ENV_VERSION_URL] = good
        val, dt, exc = run([good], timeout=6.0)
        rep.check("只配一个地址时照样能用（环境变量覆盖路径）",
                  val == FAKE_NEWER and exc is None, repr(val))
        os.environ.pop(vc.ENV_VERSION_URL, None)

        val, dt, exc = run([junk], timeout=6.0)
        rep.check("单地址且内容不合格时抛错（不静默）",
                  val is None and exc is not None, repr(exc))

        # ---- ⑥ 配置层面的防呆（上一条的另一半）----
        # 竞速只判"格式合法"，格式合法的旧版本号是能赢的。
        # 所以"别读到旧版本号"只能靠**排位**来防：jsdelivr 必须排最后。
        # ⚠ 这一段必须在所有 run(...) 之前读：run() 会改 DEFAULT_VERSION_URLS，
        #   放在后面就会读到上一个用例留下的假地址（我第一版就这么错了）。
        urls = list(saved_urls)
        jsd = [i for i, u in enumerate(urls) if "jsdelivr" in u]
        rep.check("★jsdelivr（缓存、会滞后）排在候选表末位",
                  bool(jsd) and jsd[0] == len(urls) - 1,
                  "位置 %r / 共 %d 条：%r" % (jsd, len(urls), urls))
        rep.check("候选表里至少有 2 条非 jsdelivr 的源（不能只剩缓存可用）",
                  len([u for u in urls if "jsdelivr" not in u]) >= 2,
                  "%d 条" % len([u for u in urls if "jsdelivr" not in u]))
        rep.check("RACE_WIDTH 至少 2（否则竞速退化成串行）",
                  getattr(vc, "RACE_WIDTH", 0) >= 2, str(getattr(vc, "RACE_WIDTH", None)))
        rep.check("竞速线程都是 daemon（不能拖住程序退出）",
                  _race_threads_are_daemon(), "")

        # ---- ⑦ 两个环境变量都设时，覆盖优先（发布/应急通道）----
        rep.check("ENV_VERSION_URL 覆盖后 version_urls() 把它排第一",
                  _env_override_first(good))
    finally:
        vc.DEFAULT_VERSION_URLS = saved_urls
        if saved_env is not None:
            os.environ[vc.ENV_VERSION_URL] = saved_env
        else:
            os.environ.pop(vc.ENV_VERSION_URL, None)
        for s in servers:
            s.shutdown()
            s.server_close()


def _env_override_first(url: str) -> bool:
    saved = os.environ.get(vc.ENV_VERSION_URL)
    os.environ[vc.ENV_VERSION_URL] = url
    try:
        return vc.version_urls()[0] == url
    finally:
        if saved is None:
            os.environ.pop(vc.ENV_VERSION_URL, None)
        else:
            os.environ[vc.ENV_VERSION_URL] = saved


def _race_threads_are_daemon() -> bool:
    """竞速必须用 daemon 线程 —— 否则程序退出时会被挂住的请求卡住。

    做法：数一下调用前后**活跃线程**的变化，并确认新增的都是 daemon。
    用一个"永远不回应"的地址触发竞速（它会挂到超时）。
    """
    before = {t.ident for t in threading.enumerate()}
    # 一个"连上但不说话"的地址：用本机一个没人监听的端口，
    # 连不上会快速失败 —— 所以改用 sleep 很久的地址更能留住线程。
    srv = socketserver.TCPServer(("127.0.0.1", 0), _MoodyHandler)
    srv.daemon_threads = True
    srv.mode = "slow"
    srv.delay = 3.0
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/v" % srv.server_address[1]

    saved = vc.DEFAULT_VERSION_URLS
    vc.DEFAULT_VERSION_URLS = (url, url)
    try:
        vc.fetch_latest(0.4)            # 故意只等 0.4s，让 worker 还挂着
    except Exception:                   # noqa: BLE001 - 这里就是要它失败
        pass
    new = [t for t in threading.enumerate() if t.ident not in before]
    ok = all(t.daemon for t in new)
    vc.DEFAULT_VERSION_URLS = saved
    srv.shutdown()
    srv.server_close()
    return ok


def main() -> int:
    rep = Report()
    rep.add("=" * 72)
    rep.add("版本提示 / 更新下载 回归测试")
    rep.add("APP_VERSION    = %s" % vc.APP_VERSION)
    rep.add("VERSION_URL    = %s" % vc.VERSION_URL)
    rep.add("MANIFEST_URL   = %r%s" % (vc.MANIFEST_URL,
                                      "" if vc.MANIFEST_URL else "（未配置：只提示不下载）"))
    rep.add("=" * 72)

    tmp = tempfile.mkdtemp(prefix="haavik_update_")
    state_store.set_state_dir_for_tests(os.path.join(tmp, "state"))
    # App 启动时会按 LOCALAPPDATA 清理"用过的旧更新包"（version_check.update_dir
    # 读的就是它）。测试必须把它指到临时目录，否则会去删使用者真实目录里的东西。
    os.environ["LOCALAPPDATA"] = tmp
    try:
        test_ui_wiring(rep)
        net_ok, _ = test_logic(rep)
        test_full_path(rep, net_ok)
        test_manual_check(rep)
        test_manifest_and_download(rep, tmp)
        test_race(rep)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    rep.add("")
    rep.add("=" * 72)
    if rep.failures:
        rep.add("结论：%d 项失败 ✗" % len(rep.failures))
        for item in rep.failures:
            rep.add("    - %s" % item)
    else:
        rep.add("结论：界面通路、比对逻辑、手动检查、更新下载与校验全部通过 ✓")
    if rep.warnings:
        rep.add("（有 %d 条警告，不计入结论）" % len(rep.warnings))
    rep.add("=" * 72)

    text = "\n".join(rep.lines) + "\n"
    out = os.path.join(ROOT, "code_review", "_update.txt")
    try:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        print("[WARN] 报告写不出去：%s" % exc)
    # 生成物要进仓库：本机绝对路径一律换成占位符（硬性约定 12）。
    # 这一支会起一个本地 HTTP 服务，报告里带的是 127.0.0.1 与临时目录真路径。
    try:
        import _redact
        _redact.scrub_tree(os.path.join(ROOT, "code_review"))
    except ImportError:
        pass
    try:
        print(text)
    except UnicodeEncodeError:
        pass
    return 1 if rep.failures else 0


if __name__ == "__main__":
    sys.exit(main())
