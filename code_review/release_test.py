# -*- coding: utf-8 -*-
"""发版侧：Gitee 清单里 AI 组件「镜像腿」该指哪儿。

**为什么单独有这个测试**（2026-09-25 实测踩到的真 bug）：

AI 模型包在**两个站挂在不同的 tag** 上 —— Gitee 不能替换同名附件，所以给 AI 包
单独开了 ``ai-x.y.z``；而 GitHub 那边它和主程序共用 ``vX.Y.Z``。
``publish_gitee`` 在"本次没准备新 AI 包、沿用上一版 ai 段"时会重新拼一遍
镜像腿，原来的写法是拿 ``prev_ai["url"]`` 的 tag 去拼 —— 那是 **Gitee 的 tag**，
拼出来的 6 条 GitHub 镜像地址**全部 404**，只剩 Gitee 自己那条能下。

它不会报错、不会让 AI 装不上（主腿还活着），只是**悄悄少了 6 条备用路** ——
正是本项目最怕的那种"不报错、只是坏了一半"。所以必须钉住。

跑法：``python code_review/release_test.py``
"""
from __future__ import annotations

import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
if os.path.join(HERE, "optimized") not in sys.path:
    sys.path.insert(0, os.path.join(HERE, "optimized"))

import release as R

OUT = os.path.join(HERE, "_release.txt")

GH_URL = ("https://github.com/wang1357441/haavik-photo-dist"
          "/releases/download/v1.8.4/haavik_ai.pack")
GITEE_URL = ("https://gitee.com/whr3443kklld/haavk-photos---haavk-photo"
             "/releases/download/ai-1.4.0/haavik_ai.pack")


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
        if isinstance(ok, str) and not isinstance(label, str):
            raise TypeError(
                u'check() 参数写反了：本文件应为 check(条件, "说明")，'
                u'收到 check(%r, %r)' % (ok, label))
        mark = "  OK  " if ok else " FAIL "
        self.lines.append("[%s] %s%s" % (mark, label,
                                        ("  <- " + detail) if detail else ""))
        if not ok:
            self.failures.append(label + (" (" + detail + ")" if detail else ""))

    def text(self):
        head = "发版侧：AI 镜像腿（release.py / _ai_mirror_urls_for_gitee）"
        tail = ("=" * 68 + "\n"
                + ("RESULT: OK\n" if not self.failures
                   else "FAILED: %d 项不成立\n" % len(self.failures)
                        + "".join("  * %s\n" % f for f in self.failures))
                + "=" * 68 + "\n")
        return "\n".join([head] + self.lines) + "\n" + tail


def _tag(u):
    return R._release_tag_of(u)


def main() -> int:
    rep = Report()
    real_gh = R._github_manifest_anon
    real_mirror = R.mirror_urls

    try:
        # ---------- 1. 正常情形：GitHub 清单读得到 ----------
        rep.head("1. GitHub 清单读得到 → 镜像腿必须指 GitHub 的 tag")
        rep.check(_tag(GH_URL) == "v1.8.4", "先确认样例 URL 的 tag 抠得对",
                  str(_tag(GH_URL)))
        rep.check(_tag(GITEE_URL) == "ai-1.4.0",
                  "Gitee 那条的 tag 是 ai-1.4.0（两站 tag 不同，这就是坑的根源）",
                  str(_tag(GITEE_URL)))

        R._github_manifest_anon = lambda: {
            "ai": {"version": "1.4.0", "url": GH_URL,
                   "urls": [GH_URL, "https://gh-proxy.com/" + GH_URL]}
        }
        got = R._ai_mirror_urls_for_gitee({"url": GITEE_URL, "urls": []})
        rep.check(bool(got), "拿到了镜像腿", str(len(got)))
        rep.check(all("github.com" in u for u in got),
                  "★ 镜像腿全部指向 github.com", str(got[:1]))
        rep.check(all(_tag(u) == "v1.8.4" for u in got),
                  "★★ 镜像腿用的是 **GitHub 的 tag**（v1.8.4），"
                  "不是 Gitee 的 ai-1.4.0", str([_tag(u) for u in got]))
        rep.check(not any("ai-1.4.0" in u for u in got),
                  "★★ 镜像腿里**不能**出现 Gitee 的 tag "
                  "（出现了就是 6 条腿全 404）")

        # ---------- 2. GitHub 清单有 ai 但没有 urls ----------
        rep.head("2. GitHub 有 ai 段但缺 urls → 按它的 url 现拼")
        seen = {}

        def _fake_mirror(name, tag, primary):
            seen["tag"] = tag
            return ["https://mirror.example/%s/%s" % (tag, name)]

        R.mirror_urls = _fake_mirror
        R._github_manifest_anon = lambda: {"ai": {"url": GH_URL, "urls": []}}
        got = R._ai_mirror_urls_for_gitee({"url": GITEE_URL, "urls": []})
        rep.check(seen.get("tag") == "v1.8.4",
                  "★ 现拼时用的也是 GitHub 的 tag", str(seen.get("tag")))
        rep.check(bool(got), "拼出了东西", str(got))
        R.mirror_urls = real_mirror

        # ---------- 3. GitHub 读不到 → 保留原有 urls，不拿 Gitee tag 乱拼 ----------
        rep.head("3. GitHub 清单读不到 → 保留它原有的镜像腿（不许重拼）")
        R._github_manifest_anon = lambda: {}
        prev_urls = ["https://gh-proxy.com/" + GH_URL, GH_URL]
        got = R._ai_mirror_urls_for_gitee({"url": GITEE_URL,
                                           "urls": list(prev_urls)})
        rep.check(got == prev_urls,
                  "★ 原样保留上一版的镜像腿（不能用猜的 tag 覆盖掉对的）",
                  str(got[:1]))

        # ---------- 4. 什么都拿不到 → 退回旧行为，且不炸 ----------
        rep.head("4. 什么都没有 → 退回按 Gitee tag 拼（保底，且不抛异常）")
        try:
            got = R._ai_mirror_urls_for_gitee({"url": GITEE_URL, "urls": []})
            rep.check(isinstance(got, list),
                      "退回旧行为，返回的是列表（不炸）", str(len(got)) if got else "[]")
        except Exception as exc:                                # noqa: BLE001
            rep.check(False, "退回旧行为时不该抛异常", repr(exc))

        # ---------- 5. 拿不到 tag 时的底层保护 ----------
        # 注意：它**不是**返回空列表，而是"只留主地址、一条镜像都不拼" ——
        # 这比返回空更好：使用者至少还有主地址可用，而不是什么都拿不到。
        # （第一版测试把它断言成 [] 是**测试**写错了，不是代码错了。）
        rep.head("5. 底层保护：tag 为空 → 不拼任何镜像腿，但主地址要留着")
        prim = "https://gitee.com/o/r/releases/download/ai-1.4.0/haavik_ai.pack"
        got_none = R.mirror_urls("haavik_ai.pack", None, prim)
        got_empty = R.mirror_urls("haavik_ai.pack", "", prim)
        rep.check(not any("github.com" in u for u in got_none),
                  "★ tag 为 None → **不拼任何 GitHub 镜像**（拼出来一定是死链）",
                  str(got_none[:1]))
        rep.check(prim in got_none,
                  "★ 但主地址必须留着（宁缺勿错，也不能让人什么都拿不到）",
                  str(len(got_none)) + " 条")
        rep.check(got_empty == got_none,
                  "★ tag 为空串时行为一样（None / \"\" 不许跑出两种结果）",
                  str(got_empty == got_none))

    except Exception:
        import traceback
        rep.check(False, "测试过程抛了异常")
        rep.add(traceback.format_exc())
    finally:
        R._github_manifest_anon = real_gh
        R.mirror_urls = real_mirror

    text = rep.text()
    with io.open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    try:
        import _redact
        _redact.scrub_file(OUT)
    except Exception:                                            # noqa: BLE001
        pass
    print(text)
    return 1 if rep.failures else 0


if __name__ == "__main__":
    sys.exit(main())
