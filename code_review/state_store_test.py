# -*- coding: utf-8 -*-
"""状态存储的结构性检查。

**为什么单独有它**：``state_store.load()`` 只保留**在 ``empty_state()`` 里出现过
的键**（防止状态文件被塞进任意东西）。这条设计本身是对的，但它有个**静默**的
副作用：

    新加了一个 ``KEY_XXX`` 常量、也写了存取函数，**却忘了加进 empty_state()**
    → ``save()`` 写得进去，``load()`` 读回来永远是默认值，
    **不报错**，只表现成"设置好像没生效"。

已经咬过**两次**了（v1.5.1 加反馈同意字段、2026-09-25 加远端 AI 版本），
两次都是靠手工发现。所以这里把它变成一条**结构性断言**：
只要新增了 ``KEY_*`` 常量而没登记进 ``empty_state()``，测试立刻红。

跑法：``python code_review/state_store_test.py``
"""
from __future__ import annotations

import io
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.join(HERE, "optimized") not in sys.path:
    sys.path.insert(0, os.path.join(HERE, "optimized"))

import state_store as S

OUT = os.path.join(HERE, "_state_store.txt")
SRC = os.path.join(HERE, "optimized", "state_store.py")


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
        head = "状态存储：结构性检查（state_store）"
        tail = ("=" * 68 + "\n"
                + ("RESULT: OK\n" if not self.failures
                   else "FAILED: %d 项不成立\n" % len(self.failures)
                        + "".join("  * %s\n" % f for f in self.failures))
                + "=" * 68 + "\n")
        return "\n".join([head] + self.lines) + "\n" + tail


def _key_constants() -> dict:
    """从源码里抠出所有 ``KEY_XXX = "..."`` 常量（名字 → 值）。"""
    with io.open(SRC, "r", encoding="utf-8") as fh:
        src = fh.read()
    out = {}
    for m in re.finditer(r'^(KEY_[A-Z0-9_]+)\s*=\s*"([^"]*)"', src, re.M):
        out[m.group(1)] = m.group(2)
    return out


def main() -> int:
    rep = Report()
    old_env = os.environ.get("HAAVIK_STATE_DIR")
    tmp = tempfile.mkdtemp(prefix="haavik_state_")
    os.environ["HAAVIK_STATE_DIR"] = tmp
    try:
        S.set_state_dir_for_tests(tmp)

        # ---------- 1. ★ 结构性不变量：每个 KEY_ 都要在 empty_state 里 ----------
        rep.head("1. ★ 新增 KEY_* 常量必须同时登记进 empty_state()")
        consts = _key_constants()
        rep.check(len(consts) >= 10,
                  "确实从源码里读到了 KEY_* 常量", "%d 个" % len(consts))
        blank = S.empty_state()
        missing = sorted(name for name, val in consts.items() if val not in blank)
        rep.check(
            not missing,
            "★★ 每个 KEY_* 都在 empty_state() 里（漏了就会"
            "「写得进、读不回」而且不报错）",
            "漏掉：%s" % ", ".join(missing) if missing else
            "共 %d 个键，全部登记" % len(consts))

        # 反向：empty_state 里不该有"源码里没有的键"（防手滑写错字面量）
        values = set(consts.values())
        stray = sorted(k for k in blank if k not in values and k != S.KEY_SCHEMA)
        rep.check(not stray,
                  "empty_state() 里没有来路不明的键（防字面量写错）",
                  "多出来：%s" % ", ".join(stray) if stray else "干净")

        # ---------- 2. 写进去能读回来（本轮新加的键重点测） ----------
        rep.head("2. 存 / 取往返（含本轮新加的「远端 AI 版本」）")
        for field, getter, setter, val in (
            ("AI 组件版本", S.ai_component_version, S.set_ai_component_version,
             "1.4.0"),
            ("远端 AI 版本", S.ai_remote_version, S.set_ai_remote_version,
             "9.9.9"),
        ):
            st = S.load()
            setter(st, val)
            back = getter(S.load())
            rep.check(back == val,
                      "★ %s 写进去能读回来" % field,
                      "期望 %r，读回 %r" % (val, back))

        # ---------- 3. 不认识的键必须被丢掉（安全设计，别改坏） ----------
        rep.head("3. 状态文件里塞进不认识的键 → 读的时候丢掉（安全设计）")
        st = S.load()
        st["__evil_key__"] = "x"
        S.save(st)
        back = S.load()
        rep.check("__evil_key__" not in back,
                  "★ 不认识的键不会被带回来（防止状态文件被塞任意东西）")
        rep.check(S.ai_remote_version(back) == "9.9.9",
                  "★ 但认得出来的字段照常保留",
                  repr(S.ai_remote_version(back)))

        # ---------- 4. 空状态是干净的 ----------
        rep.head("4. 全新状态的默认值")
        blank2 = S.empty_state()
        rep.check(blank2.get(S.KEY_AI_COMPONENT_VERSION) == "",
                  "没装 AI 时组件版本是空串")
        rep.check(blank2.get(S.KEY_AI_REMOTE_VERSION) == "",
                  "没查过远端时缓存是空串")
        rep.check(int(blank2.get(S.KEY_SCHEMA, 0)) == S.SCHEMA,
                  "schema 是当前版本", str(blank2.get(S.KEY_SCHEMA)))

    except Exception:
        import traceback
        rep.check(False, "测试过程抛了异常")
        rep.add(traceback.format_exc())
    finally:
        if old_env is None:
            os.environ.pop("HAAVIK_STATE_DIR", None)
        else:
            os.environ["HAAVIK_STATE_DIR"] = old_env
        S.set_state_dir_for_tests(tmp)

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
