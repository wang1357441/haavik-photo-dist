# -*- coding: utf-8 -*-
"""静态扫描：找出所有 rep.check(...) 参数写反的调用。

8 个测试文件有两套签名，写反会静默变成"永远通过"，所以这里用 AST 扫一遍：
  * 条件在前（6 个）：可疑 = 第 1 个实参是字符串字面量，而第 2 个不是"文本"。
  * 说明在前（2 个）：可疑 = 第 1 个实参不是"文本"，而第 2 个是字符串字面量。
"文本"包含 `"a" % b` 与 f-string 这类由字符串拼出来的形态。
"""
import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "_scan.txt")

COND_FIRST = ["image_dialogs_test.py", "image_tool_test.py", "image_ops_test.py",
              "image_view_test.py", "ui_theme_test.py", "shell_ops_test.py",
              "feedback_test.py"]
LABEL_FIRST = ["serial_test.py", "update_check_test.py"]


def is_text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.JoinedStr):                  # f-string
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return is_text(node.left)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr in ("format", "join")
    return False


def scan(name, cond_first):
    path = os.path.join(HERE, name)
    with io.open(path, encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src, filename=name)
    bad = []
    total = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "check" and len(node.args) >= 2):
            continue
        total += 1
        a0, a1 = node.args[0], node.args[1]
        if cond_first:
            if is_text(a0) and not is_text(a1):
                bad.append((node.lineno, "条件位放了说明"))
        else:
            if not is_text(a0) and is_text(a1):
                bad.append((node.lineno, "说明位放了条件"))
    return total, bad


L = []
problems = 0
for group, cond_first in ((COND_FIRST, True), (LABEL_FIRST, False)):
    L.append("== %s ==" % ("条件在前" if cond_first else "说明在前"))
    for n in group:
        total, bad = scan(n, cond_first)
        if bad:
            problems += len(bad)
            L.append("  %-24s 共 %2d 处 check()，**可疑 %d 处**" % (
                n, total, len(bad)))
            for line, why in bad:
                L.append("       第 %d 行：%s" % (line, why))
        else:
            L.append("  %-24s 共 %2d 处 check()，全部合规 ✓" % (n, total))
L.append("")
L.append("可疑合计：%d 处" % problems)

with io.open(OUT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(L) + "\n")
sys.exit(1 if problems else 0)
