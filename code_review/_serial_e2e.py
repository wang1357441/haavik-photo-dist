# -*- coding: utf-8 -*-
"""端到端验证"激活码"链路：朋友拿到的生成器，能不能生成出程序认的码。

这是"反向校验"的实证，不是看代码相信它：
  1. 匿名从 Gitee 下载仓库里那一份 serial_check.py（朋友看到的原件）；
  2. 用它生成一枚序列号（跑它自己的 CLI）；
  3. 把序列号喂给**本地** serial_check.verify()（= 程序里跑的那段）；
  4. 必须通过；再故意改一个字符，必须拒绝。
另外顺便核对"仓库那份"与"本地那份"是否同一份（哈希）。
"""
import hashlib
import os
import re
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OPT = os.path.join(HERE, "optimized")
OUT = os.path.join(HERE, "_serial_e2e.txt")
L = []

for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
            "ALL_PROXY", "all_proxy"):
    os.environ.pop(key, None)
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))

RAW = ("https://gitee.com/whr3443kklld/haavk-photos---haavk-photo"
       "/raw/master/serial_check.py")
local_path = os.path.join(OPT, "serial_check.py")
base, ext = os.path.splitext(local_path)
PUB = base + "_published" + ext

with open(local_path, "rb") as fh:
    local = fh.read()
with urllib.request.urlopen(
        urllib.request.Request(RAW, headers={"User-Agent": "haavik-release"}),
        timeout=30) as resp:
    pub = resp.read()

L.append("本地 serial_check.py ：%d 字节  sha256=%s" % (
    len(local), hashlib.sha256(local).hexdigest()[:16]))
L.append("仓库 serial_check.py ：%d 字节  sha256=%s" % (
    len(pub), hashlib.sha256(pub).hexdigest()[:16]))
L.append("两份是否同一份：%s" % (local == pub))
L.append("")

with open(PUB, "wb") as fh:
    fh.write(pub)

# --- 1) 用"仓库那份"生成序列号 ---
env = dict(os.environ)
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    env.pop(k, None)
proc = subprocess.run([sys.executable, PUB], capture_output=True, timeout=90,
                      env=env, cwd=os.path.dirname(PUB))
text = (proc.stdout.decode("utf-8", "replace") +
        proc.stderr.decode("utf-8", "replace"))
L.append("== 用仓库那份生成器跑一次（rc=%d）==" % proc.returncode)
for line in text.strip().splitlines()[:14]:
    L.append("  | " + line)

# 从输出里捞形如 XXXXX-XXXXX-… 的序列号
cands = re.findall(r"[0-9A-Za-z]{5}(?:-[0-9A-Za-z]{5})+", text)
L.append("")
L.append("抓到的候选序列号：%r" % cands[:3])
if not cands:
    L.append("✗ 没能从生成器输出里抓到序列号，后面的验证无从谈起")
else:
    code = cands[0]
    sys.path.insert(0, OPT)
    import serial_check as vc                              # noqa: E402
    ok, why = vc.verify(code)
    L.append("程序侧 verify(%r) -> %s  (%s)" % (code, ok, why))

    bad = code[:-1] + ("A" if code[-1] != "A" else "B")
    ok2, why2 = vc.verify(bad)
    L.append("改一个字符 verify(%r) -> %s  (%s)" % (bad, ok2, why2))
    L.append("")
    L.append("结论：%s" % (
        "✓ 生成器生成的码，程序认；改过的码，程序拒"
        if ok and not ok2 else "✗ 链路有问题"))

os.remove(PUB)
with open(OUT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(L) + "\n")
print("done")
