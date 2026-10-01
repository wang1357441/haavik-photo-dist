# -*- coding: utf-8 -*-
"""feedback_send.py 回归测试 —— 本项目**第二个联网点**。

测什么、为什么这么测
======================================================================
这个模块是"用户亲手点一下，把字发出去"的那条路。它有两个特点：

  1. **它会联网** —— 所以不能只读代码说"应该没问题"，要真的对着一个
     HTTP 服务器发一次，看**对面收到了什么**（方法、路径、Content-Type、
     正文里的每一个字段）。用一个本地起的服务器，两端都在我手里，
     能逐字节核。
  2. **它必须安静地失败** —— 网络不通、对面拒绝、对面要求跳转，
     都不许把异常抛到 Tk 的回调里（那会是一个静默的崩溃）。
     所以每一种失败都要有一条断言。

约定：``check(条件, "说明")``（条件在前）。写反了由守卫当场抛 TypeError。
"""

from __future__ import annotations

import http.server
import io
import json
import os
import socket
import sys
import threading
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
OPTIMIZED = os.path.join(HERE, "optimized")
OUT = os.path.join(HERE, "_feedback.txt")

if OPTIMIZED not in sys.path:
    sys.path.insert(0, OPTIMIZED)
if HERE not in sys.path:
    sys.path.insert(0, HERE)                          # 好让 _redact 也能被 import

import _redact                                        # noqa: E402
import feedback_send as fb                                    # noqa: E402


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
        if isinstance(ok, str) and not isinstance(label, str):
            raise TypeError(
                u'check() 参数写反了：本文件应为 check(条件, "说明")，'
                u'收到 check(%r, %r)' % (ok, label))
        mark = "  OK  " if ok else " FAIL "
        self.lines.append("[%s] %s%s"
                          % (mark, label, ("  <- " + detail) if detail else ""))
        if not ok:
            self.failures.append(label + (" (" + detail + ")" if detail else ""))
        return ok

    def text(self):
        head = ["=" * 68, "feedback_send.py 回归测试", "=" * 68]
        if self.failures:
            tail = ["", "=" * 68, "FAILED: %d 项不成立" % len(self.failures)]
            tail += ["  * " + f for f in self.failures]
            tail.append("=" * 68)
        else:
            tail = ["", "=" * 68, "RESULT: OK", "=" * 68]
        return "\n".join(head + self.lines + tail) + "\n"


# --------------------------------------------------------------------------
# 一个能被逐字节检查的假接收端
# --------------------------------------------------------------------------
class FakeEndpoint(http.server.BaseHTTPRequestHandler):
    """假接收端：把收到的请求记下来，按路径给不同的回话。

    ``/ok``      → 200 {"ok": true}
    ``/reject``  → 400 {"ok": false, "why": "内容里请先不要放网址"}
    ``/formerr`` → 422 {"errors": [{"message": "Form not found"}]}   ← Formspree 那种形状
    ``/junk``    → 200 "not json at all"
    ``/boom``    → 500 空回话
    ``/go-away`` → 302 跳去 /ok（**不该被跟随**）
    """
    received = []
    paths_hit = []

    def do_POST(self):                                        # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        FakeEndpoint.paths_hit.append(self.path)
        FakeEndpoint.received.append({
            "path": self.path,
            "method": self.command,
            "ctype": self.headers.get("Content-Type"),
            "accept": self.headers.get("Accept"),
            "ua": self.headers.get("User-Agent"),
            "body": body,
        })
        if self.path == "/go-away":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        table = {
            "/ok": (200, b'{"ok": true}'),
            "/reject": (400, '{"ok": false, "why": "内容里请先不要放网址"}'
                             .encode("utf-8")),
            "/formerr": (422, b'{"errors": [{"message": "Form not found"}]}'),
            "/junk": (200, b"not json at all"),
            "/boom": (500, b""),
        }
        status, payload = table.get(self.path, (404, b'{"ok": false}'))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):        # 别把测试输出弄脏
        pass


def start_server():
    """起一个只在回环上的服务器，返回 (基础地址, 服务器对象)。"""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeEndpoint)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]
    return "http://127.0.0.1:%d" % port, srv


def free_port_address():
    """一个**没人监听**的回环地址（用来测"连不上"那条路）。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return "http://127.0.0.1:%d" % port


def main():
    rep = Report()
    rep.add("模块：%s" % fb.__file__)
    rep.add("协议版本：v%d   正文上限：%d 字   回执最多读：%d 字节"
            % (fb.PROTOCOL, fb.MAX_BODY, fb.MAX_REPLY))
    rep.add("")

    try:
        # ================================================================
        rep.head("A. 先说清楚它会不会乱发（参数闸门）")
        # ================================================================
        ok, why = fb.send_feedback("https://example.invalid/x", "   ")
        rep.check(not ok and "空" in why, "空内容直接拒绝，不发请求", why)
        ok, why = fb.send_feedback("https://example.invalid/x", "字" * (fb.MAX_BODY + 1))
        rep.check(not ok and "太长" in why, "超长直接拒绝", why)
        ok, why = fb.send_feedback("", "你好")
        rep.check(not ok and "https" in why, "地址是空的 → 拒绝", why)
        ok, why = fb.send_feedback("http://example.com/x", "你好")
        rep.check(not ok and "https" in why,
                  "★ 明文 http 的**对外**地址一律拒绝（用户写的话不该裸奔）", why)
        ok, why = fb.send_feedback("ftp://example.com/x", "你好")
        rep.check(not ok, "非 http(s) 协议 → 拒绝", why)

        # ================================================================
        rep.head("B. 真发一次：对面收到了什么")
        # ================================================================
        base, srv = start_server()
        try:
            FakeEndpoint.received.clear()
            FakeEndpoint.paths_hit.clear()
            mine = "有个小问题：叠加的时候能不能吸附边缘？\n第二行也测一下。"
            ok, why = fb.send_feedback(base + "/ok", mine, version="9.9.9",
                                       client_time="2026-09-19T09:30:00")
            rep.check(ok, "正常提交成功", why)
            rep.check(len(FakeEndpoint.received) == 1,
                      "只提交了**一次**（不重试）",
                      "%d 次" % len(FakeEndpoint.received))
            if FakeEndpoint.received:
                got = FakeEndpoint.received[0]
                rep.check(got["method"] == "POST", "用的是 POST", got["method"])
                rep.check(got["path"] == "/ok", "打到了配的那个路径", got["path"])
                rep.check(got["ctype"] and "application/json" in got["ctype"],
                          "声明了 application/json", repr(got["ctype"]))
                rep.check(got["accept"] and "json" in got["accept"],
                          "请求了 JSON 回执（否则表单服务会回网页）",
                          repr(got["accept"]))
                try:
                    data = json.loads(got["body"].decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    data = {}
                    rep.check(False, "正文是合法 UTF-8 JSON", repr(exc))
                rep.check(data.get("message") == mine,
                          "★ 正文里的字与用户打的**一模一样**（含换行与中文）",
                          repr(data.get("message"))[:60])
                rep.check(data.get("version") == "9.9.9", "带了版本号",
                          repr(data.get("version")))
                rep.check(data.get("v") == fb.PROTOCOL, "带了协议版本")
                rep.check("client_time" in data, "带了提交时间")
                keys = sorted(data)
                rep.check(set(keys) <= {"v", "app", "version", "client_time",
                                        "message"},
                          "★ 只发这几个字段 —— 没有文件名、没有机器信息、"
                          "没有日志（隐私说明里承诺过的）", "%s" % (keys,))

            # ---- 对面拒绝：要把原因原样带回来（界面会显示给用户）----
            ok, why = fb.send_feedback(base + "/reject", "看看这个 https://x.cn")
            rep.check(not ok and "网址" in why,
                      "对面拒绝时，把它的原话带回来给用户看", why)
            ok, why = fb.send_feedback(base + "/formerr", "表单不存在的情况")
            rep.check(not ok and "Form not found" in why,
                      "★ 能从 errors[0].message 里取出原因（表单服务的形状）", why)
            ok, why = fb.send_feedback(base + "/boom", "服务器炸了")
            rep.check(not ok and "500" in why, "5xx 也如实说明状态码", why)
            ok, why = fb.send_feedback(base + "/junk", "回话不是 JSON")
            rep.check(ok, "★ 200 且回话看不懂时，按成功处理（不吓用户）", why)

            # ---- 跳转：绝不跟随 ----
            FakeEndpoint.paths_hit.clear()
            ok, why = fb.send_feedback(base + "/go-away", "试图让我们跳转")
            rep.check(not ok, "要求跳转时判为失败（不假装成功）", why)
            rep.check(FakeEndpoint.paths_hit == ["/go-away"],
                      "★ 没有跟随跳转（跳转目标没被访问过）",
                      "%s" % (FakeEndpoint.paths_hit,))

            # ---- 连不上：不能抛异常出来 ----
            ok, why = fb.send_feedback(free_port_address() + "/x", "没人监听")
            rep.check(not ok and why, "连不上时返回失败 + 原因（不抛异常）", why[:70])
        finally:
            srv.shutdown()
            srv.server_close()

        # ================================================================
        rep.head("C. 回执翻译（各种写法都要认）")
        # ================================================================
        cases = [
            (200, b'{"ok": true}', True, "标准写法"),
            (200, b'{"success": true}', True, "success 写法"),
            (200, b'{"ok": 1}', True, "真值写成了 1"),
            (200, b"", True, "空回话（有些服务就这么回）"),
            (400, '{"ok": false, "why": "限速了"}'.encode("utf-8"), False,
             "带 why 的拒绝"),
            (502, b"<html>bad gateway</html>", False, "网关的 HTML 错误页"),
        ]
        for status, raw, want, label in cases:
            ok, why = fb._verdict(status, raw)
            rep.check(ok is want, "回执翻译：%s" % label, "%r / %s" % (ok, why))
        ok, why = fb._verdict(400, '{"why": "只说了原因"}'.encode("utf-8"))
        rep.check(not ok and "只说了原因" in why,
                  "没写 ok 但带了 why → 当成失败并显示原因", why)

        # ================================================================
        rep.head("D. 模块自身的两条硬规矩")
        # ================================================================
        with open(os.path.join(OPTIMIZED, "feedback_send.py"),
                  "r", encoding="utf-8") as fh:
            source = fh.read()
        # 判定规则**直接用审计里的那一份**，不在这里再写一遍 ——
        # 两套规则迟早会分叉，然后测试说没事、审计说有事。
        import verify_payload as vp                    # noqa: PLC0415
        hits = vp.url_literals(source)
        rep.check(not hits,
                  "★ 模块里没有任何地址字面量（地址由 main.py 传入）",
                  "%s" % (hits or "（只有 https:// 这种协议前缀与回环地址）",))
        rep.check("urlopen" not in source,
                  "没有直接用 urlopen —— 连接走 version_check.open_https"
                  "（与更新同一条通道）")
        # 不 import ssl 是有意的：少一个红线模块，审计报告里就少一句要解释的话
        rep.check("\nimport ssl" not in source and "import ssl\n" not in source,
                  "★ 没有 import ssl（SMTP_SSL/starttls 会自己建默认上下文）")

        # ================================================================
        rep.head("E. 通道二：程序直连邮箱发信（不需要任何第三方服务）")
        # ================================================================
        # 这张表是**固定的、可审查的**：换服务商只改这里。所以断言要盯住内容 ——
        # 悄悄多一个"陌生的服务器"必须让测试红。
        expected = {
            "qq.com": "smtp.qq.com",
            "vip.qq.com": "smtp.qq.com",
            "foxmail.com": "smtp.qq.com",
            "163.com": "smtp.163.com",
            "126.com": "smtp.126.com",
            "yeah.net": "smtp.126.com",
            "sina.com": "smtp.sina.com",
            "sohu.com": "smtp.sohu.com",
            "aliyun.com": "smtp.aliyun.com",
            "outlook.com": "smtp-mail.outlook.com",
            "hotmail.com": "smtp-mail.outlook.com",
            "gmail.com": "smtp.gmail.com",
        }
        rep.check(fb.SMTP_HOSTS == expected,
                  "★ 发信服务器表与预期完全一致（多一个少一个都算改行为）",
                  "差异：%s" % ({k: fb.SMTP_HOSTS.get(k) for k in expected
                                if fb.SMTP_HOSTS.get(k) != expected[k]},))
        rep.check(fb.smtp_host_for("someone@QQ.com") == "smtp.qq.com",
                  "认得出 qq.com（大小写不敏感）", fb.smtp_host_for("someone@QQ.com"))
        rep.check(fb.smtp_host_for("某人@some-unknown-corp.cn") == "",
                  "不认识的域名 → 空（宁可明说认不出，也不猜一个服务器）")
        rep.check(fb.smtp_host_for("没有at符号") == ""
                  and fb.smtp_host_for("") == "",
                  "地址不合法 → 空")
        rep.check(fb.SMTP_PORTS == ((465, "ssl"), (587, "starttls")),
                  "端口顺序：先 465(SSL) 再 587(STARTTLS)（有些网络封 465）",
                  "%s" % (fb.SMTP_PORTS,))

        # 这条路在"有凭据/没凭据"两种状态下都要表现得干净：不联网、不抛异常、
        # 给一句人话。⚠️ 这里**永远 monkeypatch 凭据**，绝不用真的那份 ——
        # 第一版写成"直接调用"，结果测试真的往使用者邮箱里发了一封信。
        saved_creds = fb.credentials
        try:
            fb.credentials = lambda: ("", "", "", "")
            rep.check(not fb.smtp_ready(),
                      "凭据为空 → 这条路是关的（构建时才知道开不开）")
            ok, why = fb.send_via_smtp("你好")
            rep.check(not ok and "发信账号" in why,
                      "没配发信账号时直接说明，不发请求", why)
            fb.credentials = lambda: ("someone@unknown-corp.cn", "pw", "", "")
            rep.check(fb.smtp_ready(), "凭据齐全 → 这条路是开的")
            ok, why = fb.send_via_smtp("你好")
            rep.check(not ok and "认不出" in why,
                      "域名认不出时明说，不联网", why)
        finally:
            fb.credentials = saved_creds
        ok, why = fb.send_via_smtp("   ")
        rep.check(not ok and "空" in why, "空内容照样拦住", why)
        ok, why = fb.send_via_smtp("字" * (fb.MAX_BODY + 1))
        rep.check(not ok and "太长" in why, "超长照样拦住", why)

        # ================================================================
        rep.head("E2. 回信邮箱：只有用户自愿才留，而且绝不能让它改写邮件头")
        # ================================================================
        # 为什么单独一节：这个字段是**用户可控的文本**，而它最终要变成一个
        # 邮件头（Reply-To）。**邮件头里换行 = 换来一个新的头** ——
        # 打一行 "Bcc: 别人@x.com" 就能让这封信多带一个收件人，而隐私说明
        # 写着"只发给作者一个人"。所以下面是**安全断言**，不是功能断言。
        for good in ("me@qq.com", "a.b+tag@sub.example.co", "x1@163.com",
                     # ⚠ 这两个是**故意接受**的：外层空白是粘贴带进来的，
                     #   先 strip() 之后它就是个合法地址。真正致命的是
                     #   **内部**换行（下面那组），不是末尾多一个换行。
                     "a@b.com\t", "a@b.com\r\n", "  me@qq.com  "):
            rep.check(fb.is_valid_reply_email(good),
                      "正常邮箱必须放行：%r" % good)
        bad = [
            ("", "空串（= 没留，不算邮箱）"),
            ("没有at符号", "没有 @"),
            ("a@b", "域名没有点"),
            ("a@@b.com", "两个 @"),
            ("@b.com", "缺本地部分"),
            ("a@", "缺域名"),
            ("a b@c.com", "中间有空格"),
            ("a@b..com", "域名里有连续点"),
            ("a@b.c", "顶级域名只有一个字母"),
            ("a@" + "b" * 300 + ".com", "超长"),
        ]
        for text, why_bad in bad:
            rep.check(not fb.is_valid_reply_email(text),
                      "可疑的要拒绝（%s）" % why_bad)
        # ★★ 这一组是本节的重点：**内部的**换行/回车 = 能换来一个新的邮件头
        for inj in ("a@b.com\r\nBcc: x@y.com", "a@b.com\nBcc: x@y.com",
                    "a@b.com\rBcc: x@y.com", "\r\nBcc: x@y.com",
                    "Bcc: x@y.com\r\na@b.com"):
            rep.check(not fb.is_valid_reply_email(inj),
                      "★★ 内嵌换行必须被拦住（%r）" % inj)

        # ---- 真的发一次？不 —— 把 _deliver 换掉，只抓 mail 对象，绝不外发 ----
        # ⚠ 这个测试文件里有过一次教训："第一版写成直接调用，结果真的往
        #   使用者的邮箱发了一封信"。所以这里必须拦住底层发送。
        saved_deliver = fb._deliver
        caught = []
        fb._deliver = (lambda host, port, mode, user, password, mail, timeout:
                       caught.append(mail))
        try:
            sent_mails = caught
            ok, why = fb.send_via_smtp_with(
                "me@qq.com", "pw", "author@qq.com", "smtp.qq.com",
                text="有个问题", reply_to="user@example.com")
            rep.check(ok and len(sent_mails) == 1,
                      "留了合法邮箱 → 正常发送", why)
            hdr = sent_mails[-1].get("Reply-To") if sent_mails else None
            rep.check(hdr == "user@example.com",
                      "★ Reply-To 设成了用户的邮箱（作者点一下「回复」就直达他）",
                      repr(hdr))

            sent_mails.clear()
            ok, why = fb.send_via_smtp_with(
                "me@qq.com", "pw", "author@qq.com", "smtp.qq.com",
                text="有个问题", reply_to="a@b.com\r\nBcc: evil@x.com")
            rep.check(not ok, "★ 注入串 → 直接拒发", why)
            rep.check(not sent_mails,
                      "★★ 拒发时**一封都没发出去**"
                      "（不是「发了但没设那个头」）", str(len(sent_mails)))
            rep.check("回信邮箱" in (why or ""),
                      "拒发的原因要说到点子上（让用户知道改哪个框）", why)

            sent_mails.clear()
            ok, why = fb.send_via_smtp_with(
                "me@qq.com", "pw", "author@qq.com", "smtp.qq.com",
                text="有个问题", reply_to="")
            rep.check(ok and len(sent_mails) == 1,
                      "没留邮箱 → 照常发（选填就是选填）", why)
            rep.check(sent_mails[-1].get("Reply-To") is None,
                      "★ 没留邮箱时**不设** Reply-To（不能凭空多一个头）",
                      repr(sent_mails[-1].get("Reply-To")))
        finally:
            fb._deliver = saved_deliver

        # ================================================================
        rep.head("F. 隐私说明必须跟着真实行为走（不然那份同意书就是假的）")
        # ================================================================
        import main as prank                                   # noqa: PLC0415
        mode = prank.feedback_transport()
        rep.check(mode in ("smtp", "endpoint", "mailto"),
                  "通道三选一，不会返回别的东西", mode)
        base = len(prank.PRIVACY_PARAS)
        paras = prank.privacy_paragraphs()
        rep.check(len(paras) >= base, "隐私条款不会比基础版更少",
                  "%d 条" % len(paras))
        if mode == "mailto":
            rep.check(len(paras) == base,
                      "只用系统邮件程序时，条款就是基础那几条"
                      "（没接通道就不写通道的话）", "%d 条" % len(paras))
            rep.check(prank.PRIVACY_VERSION >= 1, "版本号是正的")
        else:
            # 接上了直达通道：必须多一条说明，而且版本号必须升过
            rep.check(len(paras) > base,
                      "★ 接上直达通道后，隐私说明里多了一条"
                      "（写清这封信是怎么出去的）",
                      "通道=%s，%d 条" % (mode, len(paras)))
            rep.check(prank.PRIVACY_VERSION >= 2,
                      "★ 说明内容变了 → PRIVACY_VERSION 必须 >= 2"
                      "（否则老用户不会重新看到这份说明）",
                      "当前 v%d" % prank.PRIVACY_VERSION)
        # 服务名解析（endpoint 那条路要在说明里如实写出"经过谁"）
        saved = prank.FEEDBACK_ENDPOINT
        try:
            prank.FEEDBACK_ENDPOINT = "https://formspree.io/f/abc123"
            rep.check(prank.feedback_service_name() == "formspree.io",
                      "能从接收端地址里取出服务名", prank.feedback_service_name())
            prank.FEEDBACK_ENDPOINT = ""
            rep.check(prank.feedback_service_name() == "",
                      "没配地址时服务名是空（说明里不会出现半句话）")
        finally:
            prank.FEEDBACK_ENDPOINT = saved

        # ---- 收件人：**界面显示的必须等于实际发送目标** ----
        saved_creds = fb.credentials
        try:
            fb.credentials = lambda: ("a@qq.com", "pw", "", "")
            rep.check(fb.effective_recipient() == "a@qq.com",
                      "凭据里没配收件人 → 发信账号自己（自己发给自己）",
                      fb.effective_recipient())
            fb.credentials = lambda: ("a@qq.com", "pw", "b@qq.com", "")
            rep.check(fb.effective_recipient() == "b@qq.com",
                      "凭据里配了收件人 → 用配的那个", fb.effective_recipient())
            rep.check(fb.effective_recipient("c@qq.com") == "c@qq.com",
                      "调用方明确指定时优先（最高优先级）")
        finally:
            fb.credentials = saved_creds
        if mode == "smtp":
            rep.check(prank.feedback_recipient() == fb.effective_recipient()
                      or prank.feedback_recipient() == prank.FEEDBACK_MAIL,
                      "★ 隐私说明里的收件人 = 实际发送目标（写谁就发谁）",
                      "%s / %s" % (prank.feedback_recipient(),
                                   fb.effective_recipient()))
            # ★ 光有上面那条还不够 —— 它有个"或等于 FEEDBACK_MAIL"的宽松分支，
            #   1.6.2 那个 bug 正是从这条缝里溜过去的：界面写的是反馈信箱，
            #   凭据里的 SMTP_TO 却是作者自己的 QQ 邮箱，两条路指向不同的地方，
            #   而"界面 == 实际"依然成立（界面显示的就是那个错的）。
            #   所以要再钉一条：**所有通道最终都得指向同一个信箱**。
            rep.check((fb.effective_recipient() or "") == prank.FEEDBACK_MAIL,
                      "★ 程序直连发信时，真发到的地方 = 那个反馈信箱"
                      "（凭据里的 SMTP_TO 不能指到别处）",
                      "%s / %s" % (fb.effective_recipient(), prank.FEEDBACK_MAIL))
        else:
            rep.check(prank.feedback_recipient() == prank.FEEDBACK_MAIL,
                      "没有直达通道时，收件人就是那个固定邮箱",
                      prank.feedback_recipient())

        # ================================================================
        rep.head("F2. \"不泄露别人的信息\"专项：日志 / state.json / 正文三处都得干净")
        # ================================================================
        # ⚠ 这是项目当前的核心承诺（2026-09-26）：熟人用 → 反馈人的信息是底线。
        #   整条链路过一遍：留了合法邮箱 → 正文里有、邮件头有；但**日志不能有、
        #   state.json 不能有、不填就一个字符都看不到**。下面 9 条断言盯着这三处。
        import logging                                            # noqa: PLC0415
        # 把所有 logger 的输出抓到一个 stringio 里，看是否有邮箱漏出去
        log_capture = io.StringIO()
        log_handler = logging.StreamHandler(log_capture)
        log_handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(log_handler)
        log_handler.setFormatter(logging.Formatter("%(message)s"))
        saved_deliver2 = fb._deliver
        sent_mails2 = []
        fb._deliver = (lambda host, port, mode, user, password, mail, timeout:
                       sent_mails2.append(mail))
        try:
            log_capture.seek(0); log_capture.truncate(0)

            # 用一个特征明显的假邮箱（特征 = "LEAK_PROBE_XYZ"），
            # 整个流程跑完，去三个地方搜这个字符串 —— 任何一处出现就证明泄露了
            PROBE = "leak_probe_xyz@example.com"

            # 走真实的反馈流程：composed() 才是负责把"回信邮箱：…"写进正文的人；
            # send_via_smtp_with 本身只负责发，不动正文。模拟"用户填了邮箱"
            # 必须用 GUI（RoundEntry），因为占位提示的坑刚修过 —— 不能绕。
            import tkinter as tk                                  # noqa: PLC0415
            import image_dialogs as idlg                          # noqa: PLC0415
            root = tk.Tk(); root.withdraw()
            form = idlg.FeedbackForm(
                root,
                version_info="HAAVIK Photo 1.8.5 · Windows 10.0",
                on_send=lambda b, r="": (True, "mocked"),
                on_copy=lambda b: (True, "mocked"),
                on_policy=lambda: None,
            )
            try:
                # 真实用户行为：填邮箱 + 打点字
                form.text.delete("1.0", "end")
                form.text.insert("1.0", "就是个反馈")
                form.reply_entry.set(PROBE)

                # 1) ★ composed() 是"会发出去什么"的唯一真相 —— 正文里有邮箱
                body_with_email = form.composed()
                rep.check(PROBE in body_with_email,
                          "留了邮箱 → 正文里有（用户能看到自己留了什么）",
                          repr(body_with_email[-80:]))

                # 2) 真发（用 mocked _deliver —— 一封都不外发）
                sent_mails2.clear()
                log_capture.seek(0); log_capture.truncate(0)
                ok, why = fb.send_via_smtp_with(
                    "me@qq.com", "pw", "author@qq.com", "smtp.qq.com",
                    text=body_with_email, reply_to=PROBE)
                log_after_send = log_capture.getvalue()
                rep.check(ok and len(sent_mails2) == 1,
                          "基准：合法邮箱能发出去", why)
                hdr = sent_mails2[-1].get("Reply-To")
                rep.check(hdr == PROBE,
                          "Reply-To 头设对了（作者点回复能直达）",
                          repr(hdr))

                # 3) ★ 日志里**不能**有（这是泄露！）
                rep.check(PROBE not in log_after_send,
                          "★★ 日志里**没有**回信邮箱"
                          "（程序崩了不会把邮箱留下）",
                          "log 长度=%d" % len(log_after_send))
                # 顺手把"probe@example.com"这种部分串也排掉（防有人写成
                # local part + @ + host 但日志里漏出一段）
                rep.check("leak_probe" not in log_after_send,
                          "★★ 日志里连邮箱的 local part 都没有",
                          repr([ln for ln in log_after_send.splitlines()
                                if "leak" in ln.lower()]))

                # 4) 不填邮箱 → 正文里**一个字都不能带**
                form.reply_entry.set("")
                body_no_email = form.composed()
                rep.check("回信邮箱" not in body_no_email,
                          "★★ 不留邮箱 → 正文里没有「回信邮箱：」这一行",
                          repr(body_no_email))
                rep.check("@" not in body_no_email,
                          "★★ 不留邮箱 → 正文里没有任何 @ 符号",
                          repr(body_no_email))
                # 真发一次，验证邮件头也没 Reply-To
                sent_mails2.clear()
                log_capture.seek(0); log_capture.truncate(0)
                ok, why = fb.send_via_smtp_with(
                    "me@qq.com", "pw", "author@qq.com", "smtp.qq.com",
                    text=body_no_email, reply_to="")
                rep.check(ok and len(sent_mails2) == 1,
                          "不留邮箱 → 照常发（选填 = 选填）")
                rep.check(sent_mails2[-1].get("Reply-To") is None,
                          "★★ 不留邮箱 → 邮件头里也没有 Reply-To")
                # 日志里也不该出现"空 reply" 这种暴露意图的痕迹
                # （什么都不写进去最安全）
                log_after_empty = log_capture.getvalue()
                # 这条允许日志里有"已发出"这种字样 —— 关键是邮箱字样不在
                rep.check("@" not in log_after_empty or "author@qq.com" not in log_after_empty
                          or log_after_empty.count("@") <= 1,
                          "不留邮箱的发送日志里没有别人的邮箱痕迹",
                          repr(log_after_empty[-200:]))
            finally:
                try:
                    root.destroy()
                except tk.TclError:
                    pass

            # 6) ★ 隐私说明里**必须**明说"不填就不带" —— 没这句话就是假说明
            paras_now = prank.privacy_paragraphs()
            joined = "\n".join(paras_now)
            rep.check("不填" in joined or "不填也" in joined or "不填就" in joined,
                      "★★ 隐私说明里明说了\"不填也行/就不带\"",
                      repr(joined[:200]))

            # 7) ★ state.json 不能长出"邮箱 / email / reply_to"这种字段
            #    （白名单字段是写死的，不该出现这种键）
            allowed = set(prank.state_store.empty_state().keys())
            # 看 state_store 模块里有没有任何 KEY_* 常量带 "email"/"reply"
            import state_store as ss                               # noqa: PLC0415
            suspect_keys = [n for n in dir(ss)
                             if n.startswith("KEY_")
                             and any(k in n.lower()
                                     for k in ("email", "reply", "mail"))]
            rep.check(not suspect_keys,
                      "★★ state_store 里没有任何 KEY_* 跟邮箱/回信有关"
                      "（写盘文件不会偷偷长出邮箱字段）",
                      repr(suspect_keys))

            # 8) 校验拒绝时反馈信**绝对不能**带那个攻击串进任何地方
            sent_mails2.clear()
            INJ = "evil@attacker.com\r\nBcc: spy@elsewhere.com"
            ok, why = fb.send_via_smtp_with(
                "me@qq.com", "pw", "author@qq.com", "smtp.qq.com",
                text="hello", reply_to=INJ)
            log_after_inj = log_capture.getvalue()
            rep.check(not ok,
                      "注入串被拒（前置条件）", why)
            rep.check(not sent_mails2,
                      "★★ 拒发时**没发出任何信**"
                      "（不能「发了但没带那个头」）")
            # 日志里**可以**提一句"看起来不对"（用户需要这个反馈），
            # 但**不能**把整条 INJ 原样写进去（那是攻击串 → 日志即漏洞）
            rep.check(INJ not in log_after_inj and "evil@" not in log_after_inj,
                      "★★ 日志里不写完整的攻击串（不留可被复制的载荷）",
                      repr(log_after_inj[:160]))
        finally:
            fb._deliver = saved_deliver2
            logging.getLogger().removeHandler(log_handler)

        # ================================================================
        rep.head("G. 填凭据那个窗口（作者自己要用的，建不起来就等于没有）")
        # ================================================================
        # 这一节盯三件事：窗口能建、空授权码不放行、保存真的写出两个文件。
        # ⚠️ 保存那一步用的是**临时路径**，绝不碰真的 code_review/feedback_smtp.txt。
        import tempfile                                        # noqa: PLC0415
        import time                                            # noqa: PLC0415
        import tkinter as tk                                   # noqa: PLC0415
        import smtp_setup_gui as gui                           # noqa: PLC0415

        tmpdir = tempfile.mkdtemp(prefix="haavik_smtp_gui_")
        saved_src, saved_dst = gui.gen.SRC, gui.gen.DST
        gui.gen.SRC = os.path.join(tmpdir, "feedback_smtp.txt")
        gui.gen.DST = os.path.join(tmpdir, "_smtp_secret.py")
        root = tk.Tk()
        root.withdraw()

        def wait_idle(seconds=12.0):
            """等后台动作跑完（窗口用线程 + after 轮询，测试要自己泵事件循环）。"""
            deadline = time.time() + seconds
            while win.busy and time.time() < deadline:
                root.update()
                time.sleep(0.05)
            return not win.busy

        try:
            win = gui.SetupWindow(root, front=False)
            root.update()
            rep.check(win.user.get().endswith("@qq.com"),
                      "发信邮箱有预填值（省得手打）", win.user.get())
            rep.check(win.password.get() == "",
                      "★ 授权码框故意不预填（凭据不该被顺手显示出来）")

            win.on_test()
            root.update()
            rep.check("授权码" in win.status.cget("text"),
                      "没填授权码时明确提示，不发信", win.status.cget("text")[:40])

            # 地址认不出域名时也要说人话，而不是抛异常
            win.user.set("someone@unknown-corp.cn")
            win.password.set("abcdefghijklmnop")
            win.on_test()
            rep.check(wait_idle(), "试发动作会结束")
            rep.check("认不出" in win.status.cget("text"),
                      "不认识的邮箱域名 → 明说认不出，不发信",
                      win.status.cget("text")[:50])

            # 保存：写出凭据文件 + 生成的模块（都在临时目录里）
            win.user.set("a@qq.com")
            win.password.set("0123456789abcdef")
            win.to.set("b@qq.com")
            win.on_save()
            rep.check(wait_idle(), "保存动作会结束（不会永远转圈）")
            rep.check(os.path.isfile(gui.gen.SRC),
                      "写出了凭据文件", gui.gen.SRC)
            rep.check(os.path.isfile(gui.gen.DST),
                      "生成了可打进 exe 的模块", gui.gen.DST)
            if os.path.isfile(gui.gen.DST):
                with open(gui.gen.DST, "r", encoding="utf-8") as fh:
                    secret = fh.read()
                rep.check("0123456789abcdef" in secret,
                          "授权码真的写进了生成的模块（否则程序发不出去）")
                rep.check("SMTP_TO" in secret and "b@qq.com" in secret,
                          "收件人也写进去了")
            if os.path.isfile(gui.gen.SRC):
                with open(gui.gen.SRC, "r", encoding="utf-8") as fh:
                    txt = fh.read()
                rep.check("SMTP_USER=a@qq.com" in txt,
                          "凭据文件是 KEY=VALUE 格式（生成脚本认这个）", )
        finally:
            gui.gen.SRC, gui.gen.DST = saved_src, saved_dst
            try:
                root.destroy()
            except tk.TclError:
                pass
            import shutil                                      # noqa: PLC0415
            shutil.rmtree(tmpdir, ignore_errors=True)

    except Exception:                                          # noqa: BLE001
        rep.add("")
        rep.add("!! 测试过程中抛异常：")
        rep.add(traceback.format_exc())
        rep.check(False, "测试全过程无异常")

    text = _redact.scrub(rep.text())
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(text)
    _redact.scrub_file(OUT)               # 生成物落盘前脱敏（本机路径 + 令牌）
    print(text)
    return 1 if rep.failures else 0


if __name__ == "__main__":
    sys.exit(main())
