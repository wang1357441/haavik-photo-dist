# -*- coding: utf-8 -*-
"""``_redact.py`` 的回归测试 —— 生成物里的**本机路径**与**令牌**都必须被擦掉。

为什么单独一支
==================================================================
``_redact`` 是"所有生成物落盘前的最后一道闸"。它本身坏了不会有人发现
（报告照样生成、测试照样绿），但会把用户名、磁盘布局、乃至发版令牌
一起带进公开仓库。2026-09-21 就是这么发现的：``release.py`` 以前会打印
令牌的前 6 + 后 4 位，而发版日志被重定向进 ``build_work/release_run*.log``
—— 实测那 3 份日志里**确实躺着 10 位明文**。

覆盖什么
==================================================================
* A 路径脱敏（原有行为不能被改坏）；
* B 机密脱敏：完整值、以及 ``前6位…后4位`` 这种"半脱敏"写法；
  外加两条**反向**断言（短值 / 名字不像机密的，不许乱替换）；
* C ``find_leaks`` 的返回值里**不许含机密原文**（否则巡检自己就成了泄密点）；
* D ``scrub_file`` 与命令行 ``--check`` 端到端（先 rc=1，擦完 rc=0）；
* E ``release.py`` 这一侧的兜底：URL query 里的 ``access_token=``、
  ``Authorization`` 头、``user:pass@``，以及"指纹不含原文"。

本测试用的是**自己造的假令牌**，绝不打印本机那一把的真值。
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "optimized"))

import _redact                                              # noqa: E402

#: 一眼假的 32 位"令牌"。够长（>=20）所以会同时触发完整值 + "前6…后4"两条规则。
FAKE_NAME = "HAAVIK_TEST_TOKEN"
FAKE = "deadbeef" + "0123456789abcdef" + "cafebabe"
FAKE2 = "f" * 20 + "0123456789ab"


class Report:
    def __init__(self):
        self.lines = []
        self.failures = []

    def add(self, text=""):
        self.lines.append(text)

    def head(self, title):
        self.lines += ["", "-" * 68, title, "-" * 68]

    def check(self, ok, label, detail=""):
        if isinstance(ok, str) and not isinstance(label, str):
            raise TypeError("check() 参数写反了：应为 check(条件, 说明)")
        mark = "  OK  " if ok else " FAIL "
        self.lines.append("[%s] %s%s" % (mark, label, ("  <- " + detail) if detail else ""))
        if not ok:
            self.failures.append(label + (" (" + detail + ")" if detail else ""))
        return ok

    def text(self):
        head = ["=" * 68, "_redact.py 脱敏 回归测试", "=" * 68]
        tail = ["", "=" * 68]
        if self.failures:
            tail += ["FAILED: %d 项不成立" % len(self.failures)]
            tail += ["  * " + f for f in self.failures]
        else:
            tail += ["RESULT: OK"]
        tail.append("=" * 68)
        return "\n".join(head + self.lines + tail) + "\n"


def main() -> int:
    rep = Report()
    tmpdir = tempfile.mkdtemp(prefix="haavik_redact_")
    saved = {}
    for name in (FAKE_NAME, "HAAVIK_TEST_PASSWORD", "HAAVIK_TEST_NOTE"):
        saved[name] = os.environ.get(name)

    try:
        # ---------------- A. 路径 ----------------
        rep.head("A. 本机路径照旧被脱敏（原有行为不能坏）")
        temp = tempfile.gettempdir()
        home = os.path.expanduser("~")
        s = _redact.scrub("图在 " + temp + "\\a.png")
        rep.check(temp not in s, "临时目录被换成 %TEMP%", s)
        rep.check("%TEMP%" in s, "占位符确实写进去了")
        s = _redact.scrub("家目录 " + home + "\\x.txt")
        rep.check(home not in s, "用户目录被换成 %USERPROFILE%", s)
        rep.check(_redact.scrub("") == "", "空串进来、空串出去")
        rep.check(_redact.scrub(None) is None, "None 进来、None 出去（不炸）")
        s = _redact.scrub(temp.replace("\\", "/") + "/b.png")
        rep.check("%TEMP%" in s, "正斜杠写法也覆盖到")

        # ---------------- B. 机密 ----------------
        rep.head("B. 环境里的令牌值必须被擦掉（本次新增）")
        os.environ[FAKE_NAME] = FAKE
        rep.check(len(FAKE) >= 20, "假令牌够长（>=20，会同时触发两条规则）")

        s = _redact.scrub("token=" + FAKE)
        rep.check(FAKE not in s, "完整令牌被替换掉了", s)
        rep.check(("%SECRET:" + FAKE_NAME + "%") in s, "替换成可读占位符（知道是谁被擦了）", s)

        frag = FAKE[:6] + "\u2026" + FAKE[-4:]
        s = _redact.scrub("日志里写着 用了 " + frag)
        rep.check(frag not in s, "『前6位…后4位』这种半脱敏写法也一并擦掉", s)
        rep.check(s.startswith("日志里写着 用了 %SECRET:"), "它也被换成同一个占位符", s)

        # 反向断言：不许乱替换
        os.environ["HAAVIK_TEST_PASSWORD"] = "短"
        rep.check("短" in _redact.scrub("这句话里有短"), "太短的值不当机密（<=11 位不碰）")
        os.environ["HAAVIK_TEST_NOTE"] = "n" * 40
        rep.check("n" * 40 in _redact.scrub("n" * 40),
                  "名字不像机密的（没有 TOKEN/SECRET/PASSWORD…）长值也不碰")

        real = os.environ.get("GITEE_TOKEN", "").strip()
        if real:
            rep.check(real not in _redact.scrub("t=" + real),
                      "本机真令牌（GITEE_TOKEN）同样被擦（只断言『擦掉了』，不打印值）")
        else:
            rep.add("（本机没设 GITEE_TOKEN，跳过真令牌那一条）")

        # ---------------- C. find_leaks ----------------
        rep.head("C. 巡检 find_leaks：能报出来，但结果里不许含机密原文")
        hits = _redact.find_leaks("token=" + FAKE)
        rep.check(any(h.startswith("%SECRET:") for h in hits), "能发现机密泄漏", "、".join(hits))
        rep.check(all(FAKE not in h for h in hits),
                  "返回的是 %SECRET:名字% 而不是值 —— 否则巡检自己就泄密了")
        hits2 = _redact.find_leaks("图 " + temp + "\\a.png")
        rep.check(any(temp in h for h in hits2), "路径泄漏照旧能发现", "、".join(hits2))
        rep.check(_redact.find_leaks("干干净净的一行字") == [], "干净文本返回空表")

        # ---------------- D. 端到端 ----------------
        rep.head("D. scrub_file 与命令行 --check 端到端")
        leak = os.path.join(tmpdir, "_leak.txt")
        with io.open(leak, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("令牌 " + FAKE + "\n图片 " + temp + "\\a.png\n家 " + home + "\\b\n")
        rc_before = subprocess.call(
            [sys.executable, os.path.join(ROOT, "_redact.py"), "--check", tmpdir],
            stdout=subprocess.DEVNULL)
        rep.check(rc_before == 1, "--check 在擦之前能抓出泄漏（rc=1）", "rc=%d" % rc_before)

        changed = _redact.scrub_file(leak, quiet=False)
        rep.check(changed, "scrub_file 报告自己改动了内容")
        back = io.open(leak, encoding="utf-8").read()
        rep.check(FAKE not in back and temp not in back and home not in back,
                  "文件里：令牌、临时目录、家目录 全没了")
        rep.check(_redact.find_leaks(back) == [], "复扫该文件：干净")
        rc_after = subprocess.call(
            [sys.executable, os.path.join(ROOT, "_redact.py"), "--check", tmpdir],
            stdout=subprocess.DEVNULL)
        rep.check(rc_after == 0, "擦完之后 --check 通过（rc=0）", "rc=%d" % rc_after)
        rep.check(_redact.scrub_file(leak) is False, "已经干净的文件不会被再改一遍")

        # ---------------- E. release.py 的兜底 ----------------
        rep.head("E. release.py 输出侧兜底：URL / 头 / 指纹")
        import release as rel                                # noqa: PLC0415
        rel._remember_secret(FAKE2)
        out = rel._scrub("GET https://gitee.com/api/v5/repos/x?access_token=" + FAKE2 + " 失败")
        rep.check(FAKE2 not in out, "Gitee GET 拼在 URL query 里的 access_token 被擦", out)
        rep.check("access_token=***" in out, "只擦值、保留参数名（方便看懂是哪个参数）")
        out = rel._scrub("Authorization: token " + FAKE2)
        rep.check(FAKE2 not in out, "Authorization 头被擦", out)
        out = rel._scrub("https://user:" + FAKE2 + "@gitee.com/x")
        rep.check(FAKE2 not in out, "URL 里的 user:pass@ 被擦", out)
        rep.check(rel._scrub("普通一行").count("***") == 0, "没有机密时不动普通文本")

        fp = rel.fingerprint(FAKE2)
        rep.check(len(fp) == 8, "指纹固定 8 位", fp)
        rep.check(FAKE2[:6] not in fp, "指纹里不含令牌前 6 位（推不出原文）")
        rep.check(rel.fingerprint(FAKE2) != rel.fingerprint(FAKE2 + "x"),
                  "不同令牌指纹不同（能用来确认『两次是同一把』）")
        # say() 是唯一出口：即使调用方把令牌拼进了消息，也不会打出去
        rep.check(FAKE2 not in rel._scrub(rel._scrub("x=" + FAKE2)), "擦两遍也不会漏（幂等）")

    except Exception as exc:                               # noqa: BLE001
        rep.check(False, "测试过程中抛异常")
        rep.add(repr(exc))
    finally:
        for name, old in saved.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
        shutil.rmtree(tmpdir, ignore_errors=True)

    out_path = os.path.join(ROOT, "_redact.txt")
    with io.open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(rep.text())
    _redact.scrub_file(out_path)                           # 报告自己也过一遍
    sys.stderr.write(rep.text())
    return 1 if rep.failures else 0


if __name__ == "__main__":
    sys.exit(main())
