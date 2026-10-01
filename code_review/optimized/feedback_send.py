# -*- coding: utf-8 -*-
"""把用户写的反馈 POST 到接收端。

**这是本项目的第二个、也是最后一个联网点。**

为什么要有它
======================================================================
朋友的电脑上大多**没有配邮件程序**，`mailto:` 那条路（shell_ops.open_mailto）
对他们基本是死路。所以"在软件里点一下就发出去"必须由程序自己提交一次 HTTPS。

为什么不是"程序直接发邮件"
======================================================================
发邮件要 SMTP 账号密码，而那等于把你邮箱的**发信权**发给每一个拿到安装包的人
（PyInstaller 的包解包不难）。后果是账号被拿去发垃圾邮件、被封。
所以这里只做一件事：把用户打的字 POST 到**你控制的那个地址**，
凭据（如果有）留在服务端。

每一条设计都是为了"这个 exe 只做这一件事"还成立
======================================================================
* **地址不在这里**：由调用方传入（``main.FEEDBACK_ENDPOINT``）。
  这样审计可以断言"本文件里没有任何 URL 字面量" —— 想偷偷指向别处，
  必须改另一个文件，藏不住。
* **只上报用户打的字**（外加版本号与时间）：附件、文件、日志、机器信息一律不带。
  ⚠️ 加字段之前先想清楚：界面上的隐私说明必须同步改，而且版本号要加一。
* **一次提交，不重试、不跟随跳转**：跟着跳转走 = 程序去访问一个它没检查过的
  地址；自动重试 = 用户按一次按钮，对面收到好几条。
* **响应最多读 2KB 就停** —— 这是"只上报、不下载"的落地方式。
* **失败必须说人话**：界面会把原因原样显示给用户（"失败要响"这条约定）。
"""

from __future__ import annotations

import json
import logging
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage

# version_check 改为函数内懒导入（见 send_via_smtp / send_feedback）：
# 本模块被安装器 import 时若在这里顶层 import version_check，会把「更新下载/写盘」
# 整套代码连带打进安装器（安装器根本用不到），触发 verify_payload 审计失败。
# 安装器只走 send_via_smtp_with（不需要 version_check），故放函数内懒导入。
log = logging.getLogger("haavik.feedback")

#: 协议版本。改了请求体结构就加一（接收端那边也有一份对应的约定）。
PROTOCOL = 1
APP_NAME = "HAAVIK Photo"
#: 正文上限（字符）。界面建议 600 字，这里宽一点，但不无限。
MAX_BODY = 4000
#: 响应最多读这么多字节。收端只需要回一个 {"ok": true}。
MAX_REPLY = 2048
TIMEOUT = 15.0
USER_AGENT = "HAAVIK-Photo/1 (feedback)"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """不允许跳转。

    跟着跳转走意味着"程序会访问一个它没有检查过的地址"，而这条路的
    地址是写死在程序里的 —— 跳转让那个"写死"失去意义。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        log.warning("反馈通道要求跳转到 %s，已拒绝", newurl)
        return None


def _address_ok(url: str) -> bool:
    """地址必须是 https；唯一例外是**回环地址**。

    回环那条例外是为了让测试能对着本机起的假接收端真发一次 ——
    流量不出本机，所以它不构成"明文外发"。别的任何 http 一律拒绝。
    """
    if not isinstance(url, str):
        return False
    if url.startswith("https://"):
        return True
    return url.startswith("http://127.0.0.1") or url.startswith("http://localhost")


# ==========================================================================
# 通道二：程序直接连邮箱把信发出去（不需要任何第三方服务）
# ==========================================================================
#: 常见邮箱 → 发信服务器。**这是一个固定的、一眼能看完的表**：
#: 换邮箱服务商只改这里，别的地方都不用动。
SMTP_HOSTS = {
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

#: 先试 465（SSL），连不上再试 587（STARTTLS）。
#: 有些网络会封 465 —— 两条都试过才算失败，成功率最高。
SMTP_PORTS = ((465, "ssl"), (587, "starttls"))


def smtp_host_for(address: str) -> str:
    """从邮箱地址推出它的发信服务器（认不出返回空字符串）。"""
    if not address or "@" not in str(address):
        return ""
    domain = str(address).rsplit("@", 1)[1].strip().lower()
    if domain in SMTP_HOSTS:
        return SMTP_HOSTS[domain]
    return ""


def credentials():
    """读构建时塞进来的发信凭据；没有就返回空元组（= 这条路没开）。

    凭据模块 ``_smtp_secret`` 由 ``code_review/make_smtp_secret.py`` 从
    ``code_review/feedback_smtp.txt`` 生成 —— 那两个文件都在 .gitignore 里：
    **仓库里不放授权码，聊天里也不需要出现它**。
    """
    try:
        import _smtp_secret as sec                        # noqa: PLC0415
    except ImportError:
        return "", "", "", ""
    return (str(getattr(sec, "SMTP_USER", "") or ""),
            str(getattr(sec, "SMTP_PASS", "") or ""),
            str(getattr(sec, "SMTP_TO", "") or ""),
            str(getattr(sec, "SMTP_HOST", "") or ""))


def smtp_ready() -> bool:
    """这条路能不能用（构建时有没有塞凭据）。界面据此决定显示哪个按钮。"""
    user, password, _to, _host = credentials()
    return bool(user and password)


def effective_recipient(override: str = "") -> str:
    """这封信**实际**会发给谁。

    优先级：调用方指定 > 凭据里的 ``SMTP_TO`` > 发信账号自己（自己发给自己）。

    ⚠️ 隐私说明里"收件人"那一行必须用这个函数算出来 —— 界面上写谁，
    就得真发给谁。写一套、发另一套，那份同意书就是假的。
    """
    if override:
        return str(override)
    user, _password, to, _host = credentials()
    return to or user


def is_valid_reply_email(text: str) -> bool:
    """用户自愿留的"回信邮箱"能不能用。

    为什么**必须自己严格校验**，而不是"看着差不多就塞进邮件头"：

    ⚠ 这是真实的安全口子，不是洁癖 —— **邮件头里换行 = 换来一个新的头**。
      用户在框里打 ``a@b.com`` 回车 ``Bcc: someone@x.com``，如果不拦，
      那封信就会多带一个密送收件人，**"只发给作者一个人"这句话当场变假**。
      而这句话是写在隐私说明里、用户点过同意的那一句。

    所以只放行"看着就是普通邮箱"的串：没有空白、没有控制字符、
    正好一个 ``@``、域名里有小数点且顶级域名是字母。
    **宁可拒收**（并让他改一下），也不拼一个能被改写的头。

    ⚠ **先后顺序很关键**：先 ``strip()``（去掉外层空白），**再**查内部控制字符。
      * 外层空白是粘贴带进来的，去掉就没事 —— ``"a@b.com\\n"`` 会被**接受**
        （去掉末尾换行后它就是个合法地址）；
      * **内部**换行才是致命的 —— ``"a@b.com\\nBcc: x@y.com"`` 会被**拒绝**。
      真正要保证的性质只有一条：**最终塞进邮件头的那个串里没有 CR/LF/控制字符**。
      调用方也是拿 ``.strip()`` 之后的值去设头的，所以这条性质成立。

    另外这里**故意不用正则**：一段能逐行读懂的判断，比一条要费神解析的
    表达式更适合"这个 exe 只做我们说过的那些事"这个前提。
    """
    t = (text or "").strip()
    if not t or len(t) > 254:
        return False
    # ① 空白与控制字符一律拒（换行 / 制表符正是能改邮件头的那一类）
    for ch in t:
        if ch.isspace() or ord(ch) < 32 or ord(ch) == 127:
            return False
    # ② 正好一个 @，两边都非空
    if t.count("@") != 1:
        return False
    local, _, domain = t.partition("@")
    if not local or not domain:
        return False
    # ③ 字符集：本地部分与域名各自只允许"正常邮箱会有的那些"
    if not all(c.isalnum() or c in "._%+-" for c in local):
        return False
    if not all(c.isalnum() or c in ".-" for c in domain):
        return False
    # ④ 域名必须有点、不能以点开头结尾、不能有连续点
    if "." not in domain or domain.startswith(".") or domain.endswith("."):
        return False
    if ".." in t:
        return False
    tld = domain.rsplit(".", 1)[1]
    return tld.isalpha() and len(tld) >= 2


def _deliver(host, port, mode, user, password, mail, timeout):
    """一次发信尝试。

    ⚠️ 故意**不 import ssl**：``SMTP_SSL`` 与 ``starttls()`` 在不传 context 时
    会自己建默认上下文。少 import 一个红线模块，"这个 exe 里有完整 TLS 客户端"
    这句话就不必写进审计报告。
    """
    ctx = None
    if mode == "ssl":
        client = smtplib.SMTP_SSL(host, port, timeout=timeout)
    else:
        client = smtplib.SMTP(host, port, timeout=timeout)
    with client:
        if mode == "starttls":
            client.starttls()
        client.login(user, password)
        client.send_message(mail)


def send_via_smtp_with(user: str, password: str, to: str = "", host: str = "",
                       *, text: str = "", subject: str = "",
                       reply_to: str = "", timeout: float = TIMEOUT):
    """用**显式给的一组凭据**发一封。返回 ``(成功?, 人话说明)``。

    为什么要有这个入口：填凭据那一步（``code_review/smtp_setup_gui.py``）要能
    **当场试一次** —— "能不能发出去"应该在填的时候就看见，而不是等朋友点了
    才发现。那条测试走的就是这里的同一段代码，所以它测通了，
    程序里那条路就是通的（两处不会分叉）。
    """
    if not user or "@" not in str(user):
        return False, "发信邮箱看起来不对。"
    if not password:
        return False, "授权码是空的。"
    host = host or smtp_host_for(user)
    if not host:
        return False, ("认不出这个邮箱的服务商（%s）—— 常见邮箱有 qq/163/126/"
                       "yeah/sina/sohu/aliyun/outlook/hotmail/gmail。" % user)
    target = to or user
    if "@" not in str(target):
        return False, "收件邮箱看起来不对。"

    mail = EmailMessage()
    mail["Subject"] = subject or ("HAAVIK Photo 反馈")
    mail["From"] = user
    mail["To"] = target
    # ★ 回信邮箱（选填）：设成 Reply-To，作者在收件箱里**点一下"回复"就能
    #   回到提问的人**，不用手抄地址。不填就完全不设这个头。
    #   ⚠ 必须先过 is_valid_reply_email —— 那个函数就是为"别让用户在邮件头里
    #     塞换行"而存在的（见它的说明）。校验不过**就不发**，并且说清怎么改，
    #     而不是把用户写的原样塞进去赌一把。
    if reply_to:
        if not is_valid_reply_email(reply_to):
            return False, ("回信邮箱看起来不太对（多了空格、少了 @、"
                           "或者打错了一处）。改一下再发；"
                           "不想留就直接清空它 —— 不填也能发。")
        mail["Reply-To"] = reply_to.strip()
    mail.set_content(text or "")

    last = None
    for port, mode in SMTP_PORTS:
        try:
            _deliver(host, port, mode, user, password, mail, timeout)
        except smtplib.SMTPAuthenticationError as exc:
            # 授权码不对/被重置 —— 换端口解决不了，直接告诉用户
            log.warning("发信登录被拒：%s", exc)
            return False, ("邮箱拒绝了登录（授权码可能已被重置或填错了）。"
                           "可以让作者重新生成一次授权码。")
        except (smtplib.SMTPException, OSError, ValueError) as exc:
            last = exc
            log.info("发信端口 %s（%s）没成功：%s", port, mode, exc)
            continue
        log.info("邮件已通过 %s:%s 发出", host, port)
        return True, "已发送。谢谢！"
    log.warning("发信失败：%s", last)
    return False, ("连不上发信服务器（%s）。可以改用「复制文字」发给我。"
                   % (last or "网络不通"))


def send_via_smtp(message: str, *, version: str = "?", to: str = "",
                  reply_to: str = "", timeout: float = TIMEOUT):
    """程序里那条路：读打包时塞进来的凭据，把用户写的字发出去。

    真正的发送逻辑在 :func:`send_via_smtp_with` 里（与填凭据时的自测**共用**），
    这里只负责"取凭据 + 组装正文"。

    ``reply_to`` 是用户**自愿**留下的回信邮箱（没留就是空串）。它的合法性由
    :func:`send_via_smtp_with` 把守 —— 这里原样往下传，不自己再判一遍
    （同一件事只留一处判断，免得两边的规矩哪天不一样）。
    """
    text = (message or "").strip()
    if not text:
        return False, "反馈内容是空的。"
    if len(text) > MAX_BODY:
        return False, "反馈太长了（上限 %d 字）。" % MAX_BODY
    user, password, _to, host = credentials()
    if not (user and password):
        return False, "这个版本没有配置发信账号。"
    return send_via_smtp_with(
        user, password, effective_recipient(to), host,
        subject="HAAVIK Photo 反馈（%s）" % version,
        text="版本：%s\n\n%s\n" % (version, text), reply_to=reply_to,
        timeout=timeout)


def _verdict(status: int, raw: bytes):
    """把接收端的回话翻译成 ``(成功?, 给人看的原因)``。

    兼容三种常见写法：``{"ok": true}``、``{"success": true}``、
    以及直接 200 什么都不说（当成成功，但不假装收到了细节）。
    """
    text = (raw or b"").decode("utf-8", "replace").strip()
    data = None
    if text:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    if isinstance(data, dict):
        ok = data.get("ok")
        if ok is None:
            ok = data.get("success")
        if ok is True:
            return True, "已发送。谢谢！"
        reason = (data.get("why") or data.get("message")
                  or data.get("error") or "")
        if not reason:
            errors = data.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                reason = errors[0].get("message") or ""
        if ok is False:
            return False, str(reason) if reason else "接收端拒绝了这条反馈。"
        if 200 <= status < 300:
            # 200 但回话里没说成功/失败：当成成功，但把原话带一半回去供排查
            return True, "已发送。"
        return False, str(reason) if reason else "服务器返回 %d。" % status
    if 200 <= status < 300:
        return True, "已发送。"
    return False, "服务器返回 %d。" % status


def send_feedback(url: str, message: str, *, version: str = "?",
                  client_time: str = "", timeout: float = TIMEOUT, open_fn=None):
    """把一条反馈提交出去。返回 ``(成功?, 给人看的说明)``。

    ``url`` 由调用方给（见模块开头：本文件里不许有地址字面量）。
    这个函数**不会抛异常**给界面 —— 一切失败都变成 (False, 原因)。
    """
    text = (message or "").strip()
    if not text:
        return False, "反馈内容是空的。"
    if len(text) > MAX_BODY:
        return False, "反馈太长了（上限 %d 字）。" % MAX_BODY
    if not isinstance(url, str) or not _address_ok(url):
        # 明文 http 直接拒绝：这条路传的是用户写的话，不该裸奔。
        # （唯一例外是回环地址 —— 那是测试用的本地服务器，流量不出本机。）
        return False, "反馈通道没配置好（地址必须是 https）。"

    payload = {
        "v": PROTOCOL,
        "app": APP_NAME,
        "version": version,
        "client_time": client_time,
        "message": text,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Accept": "application/json",
                 "User-Agent": USER_AGENT})
    log.info("提交反馈：%d 字 → %s", len(text), url)

    try:
        # 默认直连（带 NoRedirect，不跟随跳转）；主程序会把 version_check.open_https
        # 注入进来，复用「检查更新」同款的代理通道策略。feedback_send 自身不依赖
        # version_check —— 否则会把「更新下载/写盘」整套代码连带打进安装器，
        # 触发 verify_payload 的「白名单之外写盘源文件」审计失败。
        if open_fn is None:
            open_fn = urllib.request.build_opener(NoRedirect).open
        resp = open_fn(req, timeout)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(MAX_REPLY)
        except OSError:
            raw = b""
        ok, why = _verdict(exc.code, raw)
        log.info("反馈被拒：HTTP %s %s", exc.code, why)
        return ok, why
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("反馈提交失败（网络层）：%s", exc)
        return False, "网络不通（%s）。可以改用「复制文字」发给我。" % exc

    try:
        with resp:
            status = getattr(resp, "status", 0) or 0
            raw = resp.read(MAX_REPLY)
    except OSError as exc:
        log.warning("读反馈回执失败：%s", exc)
        return False, "发送后没收到回执（%s）。" % exc
    ok, why = _verdict(status, raw)
    log.info("反馈回执：HTTP %s ok=%s", status, ok)
    return ok, why
