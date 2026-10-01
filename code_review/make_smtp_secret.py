# -*- coding: utf-8 -*-
"""把 code_review/feedback_smtp.txt 变成程序能用的凭据模块。

为什么要有这一步（而不是把授权码直接写进源码）
======================================================================
* 授权码是**真凭据**：拿到它就能以那个邮箱发信。它不该出现在仓库、
  不该出现在聊天记录、也不该出现在任何会被 git 追踪的文件里。
* 所以流程是：你把凭据写进 `code_review/feedback_smtp.txt`（已在 .gitignore 里）
  → 运行本脚本 → 生成 `code_review/optimized/_smtp_secret.py`（同样在 .gitignore 里）
  → 构建时它会跟着别的模块一起打进 exe。
* 换句话说：**凭据只存在于"你这台机器 + 打出来的安装包"里**。
  ⚠️ 也正因如此，安装包本身带着一个能发信的凭据 —— 这是这条路线的代价，
  写在 README 与程序的隐私说明里，不藏着。

feedback_smtp.txt 的格式（三行，行首行尾空白会被去掉）：
    SMTP_USER=你的邮箱@qq.com
    SMTP_PASS=那个授权码（**不是**登录密码）
    SMTP_TO=（可省略）收件地址，填了就以它为准
    SMTP_HOST=（可省略）发信服务器，常见邮箱会自动认出来
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "feedback_smtp.txt")
DST = os.path.join(HERE, "optimized", "_smtp_secret.py")

KEYS = ("SMTP_USER", "SMTP_PASS", "SMTP_TO", "SMTP_HOST")

TEMPLATE = """# -*- coding: utf-8 -*-
\"\"\"发信凭据 —— **由 make_smtp_secret.py 生成，不要手改，不要提交**。

生成时间：{stamp}
来源：code_review/feedback_smtp.txt（已被 .gitignore 忽略）
\"\"\"

SMTP_USER = {user!r}
SMTP_PASS = {password!r}
SMTP_TO = {to!r}
SMTP_HOST = {host!r}
"""


def parse(text: str) -> dict:
    """解析 KEY=VALUE 三/四行。返回 dict（值可能是空串）。"""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().upper()
        if key in KEYS:
            out[key] = value.strip()
    return out


def main() -> int:
    if not os.path.isfile(SRC):
        print("找不到 %s" % SRC)
        print("照着这个格式建一个（授权码不是登录密码）：")
        print("  SMTP_USER=你的邮箱@qq.com")
        print("  SMTP_PASS=授权码")
        print("  SMTP_TO=（可省略）收件地址")
        return 2

    with open(SRC, "r", encoding="utf-8") as fh:
        data = parse(fh.read())

    user = data.get("SMTP_USER", "")
    password = data.get("SMTP_PASS", "")
    to = data.get("SMTP_TO", "")
    host = data.get("SMTP_HOST", "")
    if not user or "@" not in user:
        print("SMTP_USER 看起来不像邮箱地址：%r" % user)
        return 3
    if not password:
        print("SMTP_PASS（授权码）是空的 —— 没它发不出去。")
        return 3

    import datetime
    with open(DST, "w", encoding="utf-8") as fh:
        fh.write(TEMPLATE.format(
            stamp=datetime.datetime.now().isoformat(timespec="seconds"),
            user=user, password=password, to=to, host=host))

    # 只回显"够核对"的部分：授权码本身**绝不打印**
    print("已生成 %s" % DST)
    print("  发信账号：%s" % user)
    print("  收件地址：%s" % (to or "（与发信账号相同）"))
    print("  授权码  ：%d 位（不显示内容）" % len(password))
    print("下一步：构建 + 发版（我会在构建前删掉 dist，避免用到旧包）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
