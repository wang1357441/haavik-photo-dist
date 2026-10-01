# -*- coding: utf-8 -*-
"""反馈接收端 —— **部署在你自己的云函数上**，不进程序、不打包进 exe。

为什么会有这个文件
======================================================================
"让朋友在软件里直接点发送"必须有一个**程序能提交、你能收到**的出口。
出口只有三條路，这个文件是第二条的实现：

1. **第三方表单服务**（Formspree / Web3Forms 这类）
   3 分钟注册，拿到一个**公开的表单地址**（它就写在人家的网页源码里，
   所以不是密钥）。代价：内容会先经过那家公司的服务器。
   实测结论（2026-09-19，从你这台机器上试的）：
     * formsubmit.co      ❌ 按设计只服务网页里的表单，程序提交一律拒收
     * formspree.io/f/xxx ✅ 收下我们的 POST（只报"表单不存在"，因为用的是假 ID）
     * api.web3forms.com  ✅ 收下（要一个 UUID 形式的 access_key）
2. **你自己的云函数**（本文件）
   程序里只有一个公开 URL，**没有任何凭据**；邮箱授权码放在云函数的
   环境变量里 —— 也就是放在服务端。要花十分钟部署一次，但最干净、最可控。
3. **复制文字 → 微信 / QQ 发给作者**
   零成本、不需要任何服务、到达率最高（朋友本来就有微信）。
   程序的「复制文字」按钮就是干这个的。

⛔ 永远不会做的事：把邮箱的 SMTP 密码打进 exe。那等于把你邮箱的
   发信权发给每一个拿到安装包的人 —— 后果是账号被拿去发垃圾邮件，
   严重到会被封号。这与"Gitee 令牌"完全不是一个量级。

程序侧的约定（改这里之前先看一遍，两边必须一致）
======================================================================
* 方法：``POST``，``Content-Type: application/json``
* 正文：``{"v":1, "app":"HAAVIK Photo", "version":"1.5.x",
          "message":"用户打的字", "client_time":"2026-09-19T09:30:00"}``
* 成功：HTTP 200 + ``{"ok": true}``
* 失败：非 200，或 ``{"ok": false, "why": "..."}``
  —— ``why`` 会被程序**原样显示给用户**，所以写人话，别写堆栈。
* **程序只提交一次**：不重试、不跟随跳转、不下载任何东西。
  所以这里必须"收到就算成功"，别让程序等。
* 体积：只有用户打的字（程序那边建议上限 600 字），不要假设有大文件。

部署（腾讯云函数 SCF 为例，免费额度足够几十个人的反馈量）
======================================================================
1. 新建函数 → 运行环境选 Python 3.x → 把本文件内容贴进去；
2. 配置环境变量：``SMTP_HOST`` / ``SMTP_PORT`` / ``SMTP_USER`` / ``SMTP_PASS``
   / ``MAIL_TO``（QQ 邮箱用 ``smtp.qq.com`` + 465 + **授权码**，
   注意授权码不是登录密码，要单独去邮箱设置里生成）；
3. 加触发器：选「API 网关」，拿到一个 ``https://....`` 的地址；
4. 把那个地址原样填进程序里的 ``FEEDBACK_ENDPOINT``（我改一行、发一版就行）。

会被刷吗
======================================================================
地址是公开的，理论上谁拿到都能往你邮箱塞东西。所以这里做了三件小事：
内容长度上限、拒绝内容里带链接的（垃圾邮件几乎都带链接）、
以及"一个人一分钟最多几条"的内存限速。
真被刷了也不难：在云函数里把地址换掉（重新建一个触发器），旧地址立刻作废。
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import time
from email.message import EmailMessage

#: 收到的一条反馈最多多长（比程序那边的建议值宽一些，但不无限）
MAX_CHARS = 2000
#: 同一个人（同一个云函数实例）一分钟最多几条 —— 只是抬高门槛，不是安全机制
MAX_PER_MINUTE = 5
_seen: list[float] = []


def _reply(ok: bool, why: str = "") -> dict:
    """统一出口。程序的界面会直接显示 why，所以这里必须说人话。"""
    body = {"ok": bool(ok)}
    if why:
        body["why"] = why
    return {
        "statusCode": 200 if ok else 400,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def _too_fast() -> bool:
    now = time.time()
    _seen[:] = [t for t in _seen if now - t < 60]
    if len(_seen) >= MAX_PER_MINUTE:
        return True
    _seen.append(now)
    return False


def _send(version: str, message: str) -> None:
    host = os.environ.get("SMTP_HOST", "")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASS", "")
    to = os.environ.get("MAIL_TO", "") or user
    if not (host and user and password and to):
        raise RuntimeError("云函数的环境变量没配全（SMTP_HOST/SMTP_USER/"
                           "SMTP_PASS/MAIL_TO）")

    mail = EmailMessage()
    mail["Subject"] = "【HAAVIK Photo 反馈】%s" % version
    mail["From"] = user
    mail["To"] = to
    mail.set_content("版本：%s\n\n%s\n" % (version, message))
    # 465 用 SMTPS；587 那种要 STARTTLS —— 这里按端口自动选，省得配错
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20,
                              context=ssl.create_default_context()) as smtp:
            smtp.login(user, password)
            smtp.send_message(mail)
    else:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(user, password)
            smtp.send_message(mail)


def main_handler(event, context):           # noqa: ARG001 —— SCF 的入口签名
    """云函数入口：收到反馈 → 发一封邮件给你。"""
    if _too_fast():
        return _reply(False, "提交太频繁了，请过一会儿再试。")
    try:
        raw = (event or {}).get("body") or ""
        if (event or {}).get("isBase64Encoded"):
            import base64
            raw = base64.b64decode(raw).decode("utf-8", "replace")
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return _reply(False, "请求格式不对（不是合法 JSON）。")

    message = str(data.get("message") or "").strip()
    if not message:
        return _reply(False, "反馈内容是空的。")
    if len(message) > MAX_CHARS:
        return _reply(False, "内容太长了（上限 %d 字）。" % MAX_CHARS)
    if "http://" in message or "https://" in message:
        # 不是不让人贴链接，而是垃圾提交几乎都带链接；
        # 真有链接要说，让朋友在微信里补一句就行。
        return _reply(False, "内容里请先不要放网址（防垃圾提交）。")

    version = str(data.get("version") or "?")
    try:
        _send(version, message)
    except Exception as exc:                # noqa: BLE001 —— 必须告诉用户 WHY
        return _reply(False, "发送失败：%s" % exc)
    return _reply(True, "")
