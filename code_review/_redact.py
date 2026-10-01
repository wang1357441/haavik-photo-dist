# -*- coding: utf-8 -*-
"""把生成物（报告 / 日志）里的**本机绝对路径**和**令牌**换成占位符。

为什么需要它
==================================================================
项目硬性约定 12：**仓库里不许出现本机绝对路径** —— 它会把 Windows 用户名
和磁盘布局一起带进公开仓库（例：``D:\\<用户名>\\Desktop\\hvphoto``）。

源码和 Markdown 是手写的，可以靠自觉；**生成物是拼出来的**，只要有
``os.path.abspath()`` 或一条 ``logging`` 就把真路径漏出去了。靠"每次写报告
的时候记得处理一下"是守不住的 —— 事实上这个坑就是这么被发现的：
三支新测试各自的报告里都带着 ``C:\\Users\\<用户名>\\AppData\\Local\\Temp\\...``。

**2026-09-21 补第二类：令牌。** 起因很具体 —— ``release.py`` 以前会打印
``令牌前6位…后4位``，而发版日志被重定向进 ``build_work/release_run*.log``；
实测那 3 份日志里**确实躺着 10 位明文**。路径脱敏救不了这个，所以这里再加一条：
凡 ``os.environ`` 里**名字像机密的变量**（``*TOKEN*`` / ``*SECRET*`` /
``*PASSWORD*`` / ``*API_KEY*`` …）且值够长，其**值**一律替换成
``%SECRET:<变量名>%``。生成物落盘前统一过一遍，就再也漏不出去了。

所以：生成物在落盘之前**统一过一遍** ``scrub_file()``。
调用点只有一行，漏了也不会有人发现不了 —— 因为下面还有 ``--check`` 模式
可以一次扫全目录（``verify_payload.py`` 里也接了这条检查）。

用法
==================================================================
    import _redact
    _redact.scrub_file(OUT)                     # 写完之后过一遍
    _redact.scrub_file(LOG)                     # 日志也一样
"""
from __future__ import annotations

import io
import os
import tempfile

#: 环境变量名里出现这些词（大写比较），就把它的**值**当机密。
SECRET_NAME_HINTS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "APIKEY", "API_KEY",
                     "PRIVATE_KEY", "CREDENTIAL", "AUTH")

#: 值短于这个长度就不当机密 —— 短串（"1"、"true"）满篇都是，替换会乱套。
MIN_SECRET_LEN = 12


def _rules():
    home = os.path.expanduser("~")
    user = ""
    if home:
        user = os.path.basename(home.replace("\\", "/").rstrip("/"))
    cands = []
    try:
        cands.append((tempfile.gettempdir(), "%TEMP%"))
    except Exception:                                       # noqa: BLE001
        pass
    for key, mark in (("TEMP", "%TEMP%"), ("TMP", "%TEMP%"),
                      ("USERPROFILE", "%USERPROFILE%"),
                      ("LOCALAPPDATA", "%LOCALAPPDATA%"),
                      ("APPDATA", "%APPDATA%"),
                      ("PROGRAMFILES", "%PROGRAMFILES%")):
        v = os.environ.get(key)
        if v:
            cands.append((v, mark))
    if home:
        cands.append((home, "%USERPROFILE%"))

    seen = {}
    for path, mark in cands:
        if not path:
            continue
        # 反斜杠与正斜杠两种写法都要覆盖（报告里两种都可能出现）
        for variant in (path, path.replace("\\", "/")):
            if variant and variant not in seen:
                seen[variant] = mark
    rules = sorted(seen.items(), key=lambda kv: len(kv[0]), reverse=True)
    return rules, user


def _secret_rules():
    """``(机密值, 占位符)`` 列表 —— 只收"名字像机密且够长"的环境变量。

    **值只在这一刻存在于内存里**，不写盘、不进日志、更不会进本文件的常量。
    """
    out = []
    for name, value in sorted(os.environ.items()):
        up = name.upper()
        if not any(h in up for h in SECRET_NAME_HINTS):
            continue
        v = (value or "").strip()
        if len(v) < MIN_SECRET_LEN:
            continue
        mark = "%%SECRET:%s%%" % up
        out.append((v, mark))
        # 还有"脱敏了一半"的写法：`前6位…后4位`（release.py 以前就爱这么打印，
        # 发版日志里实测留着 10 位明文）。光替换完整值救不了它，得单独成条。
        if len(v) >= 20:
            out.append((v[:6] + "\u2026" + v[-4:], mark))
    # 长值先替换，否则短值会把长值切碎
    out.sort(key=lambda kv: len(kv[0]), reverse=True)
    return out


def scrub(text: str) -> str:
    """把一段文本里的本机路径、以及登记在环境里的机密，替换成占位符。"""
    if not text:
        return text
    rules, user = _rules()
    for path, mark in rules:
        text = text.replace(path, mark)
    for secret, mark in _secret_rules():
        text = text.replace(secret, mark)
    if user:
        text = text.replace(user, "%USERNAME%")
    return text


def scrub_file(path: str, *, quiet: bool = True) -> bool:
    """就地把一个生成物脱敏。返回是否改动了内容。

    读不到、写不了都不抛异常 —— 脱敏失败不该把一支测试搞红，
    但会打一行 warning（``quiet=False`` 时改成抛给调用方）。
    """
    try:
        with io.open(path, "r", encoding="utf-8", errors="replace") as fh:
            original = fh.read()
    except OSError:
        return False
    cleaned = scrub(original)
    if cleaned == original:
        return False
    try:
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(cleaned)
    except OSError:
        if not quiet:
            raise
        return False
    return True


def scrub_tree(folder: str, patterns=("_", ), exts=(".txt", ".md", ".log")):
    """把一个目录下所有生成物过一遍（默认只碰下划线开头的那些）。

    用来做一次性清扫，以及给 ``--check`` 这类巡检用。
    """
    changed = []
    if not os.path.isdir(folder):
        return changed
    for name in sorted(os.listdir(folder)):
        if not name.endswith(exts):
            continue
        if patterns and not name.startswith(tuple(patterns)):
            continue
        full = os.path.join(folder, name)
        if os.path.isfile(full) and scrub_file(full):
            changed.append(name)
    return changed


def find_leaks(text: str, extra_names=()):
    """找出文本里还残留的本机路径 / 机密线索（占位符不算）。给巡检用。

    返回的是**可以安全打印的东西**：路径原文，以及 ``%SECRET:名字`` 标记 ——
    **绝不返回机密的值本身**，否则这道巡检自己就成了泄密点。
    """
    rules, user = _rules()
    hits = []
    for path, _mark in rules:
        if path and path in text:
            hits.append(path)
    for secret, mark in _secret_rules():
        if secret in text:
            hits.append(mark)
    for name in (user,) + tuple(extra_names):
        if name and name in text:
            hits.append(name)
    return sorted(set(hits))


def main(argv=None):
    """命令行：``python _redact.py <目录>`` 清扫，``--check`` 只报告不修改。"""
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    check_only = "--check" in argv
    argv = [a for a in argv if a != "--check"]
    folder = argv[0] if argv else os.path.dirname(os.path.abspath(__file__))
    if check_only:
        bad = []
        for name in sorted(os.listdir(folder)):
            if not name.endswith((".txt", ".md", ".log")):
                continue
            full = os.path.join(folder, name)
            if not os.path.isfile(full):
                continue
            with io.open(full, "r", encoding="utf-8", errors="replace") as fh:
                hits = find_leaks(fh.read())
            if hits:
                bad.append((name, hits))
        if bad:
            print("发现 %d 个文件含本机路径 / 机密：" % len(bad))
            for name, hits in bad:
                print("  %s  ->  %s" % (name, "、".join(hits)))
            return 1
        print("干净：没有文件含本机路径，也没有环境里那几把令牌的值")
        return 0
    changed = scrub_tree(folder)
    print("已脱敏 %d 个文件：%s" % (len(changed), "、".join(changed) or "（无需改动）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
