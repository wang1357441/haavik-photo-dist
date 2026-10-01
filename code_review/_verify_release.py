# -*- coding: utf-8 -*-
"""发版后的收尾验证：假装客户端还是上一版，把整条更新链路真的走一遍。

用法：
    python code_review/_verify_release.py            # 上一版 → 当前 APP_VERSION
    python code_review/_verify_release.py --from 1.2.4

做的事（全部走 version_check 里的**真代码**，不 mock、不猜）：
  1. 匿名读版本锚点；
  2. 比对版本号，确认"该提示更新"；
  3. 取更新清单并校验结构；
  4. **真的把安装包下载下来**，核对字节数与 sha256；
  5. 收尾删掉下载到的文件，不留痕。

退出码：0 = 全通；1 = 有问题。发版流程里这一步不能省 ——
"远端到底能不能被匿名读到""下载下来的字节对不对"只有真跑一遍才知道。
"""
import hashlib
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OPT = os.path.join(HERE, "optimized")
OUT = os.path.join(HERE, "_verify_release.txt")
sys.path.insert(0, OPT)

for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
            "ALL_PROXY", "all_proxy"):
    os.environ.pop(key, None)
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))

import version_check as vc                              # noqa: E402

L = []


def parse_version(text):
    return tuple(int(p) for p in str(text).strip().split("."))


# 默认"假装"的起点 = **严格小于**当前版本的那个版本。
# ⚠️ 不能简单地把 patch 减一：版本号是 X.Y.0 时减一还是它自己，
# 于是就变成"假装最新版在看更新"，会得出"没有新版"的假失败。
# （2026-09-17 发 1.4.0 时真踩过：明明一切正常，判定却是"链路有问题"。）
ver = vc.APP_VERSION
maj, mid, patch = parse_version(ver)
if patch > 0:
    pretend = "%d.%d.%d" % (maj, mid, patch - 1)
elif mid > 0:
    pretend = "%d.%d.99" % (maj, mid - 1)
else:
    pretend = "0.0.1"
if "--from" in sys.argv:
    pretend = sys.argv[sys.argv.index("--from") + 1]

L.append("当前发布版本（程序里）= %s" % ver)
L.append("假装客户端装的是   = %s" % pretend)
L.append("")

ok_all = True
t0 = time.time()
try:
    raw = vc.fetch_latest()
    L.append("① 匿名读版本锚点：%.1fs → %r" % (time.time() - t0, raw))
except Exception as exc:                                 # noqa: BLE001
    L.append("① 匿名读版本锚点：**失败** %r" % exc)
    raw = None
    ok_all = False

if raw is not None:
    hit = vc.check_for_update(pretend)
    L.append("② 版本比对：%s → %s  该提示更新 = %s" % (
        pretend, hit, bool(hit)))
    ok_all = ok_all and bool(hit) and hit == ver
    L.append("   （本机自己 %s 看：%s，应为 None）" % (
        ver, vc.check_for_update(ver)))

    t1 = time.time()
    try:
        man = vc.fetch_manifest()
        L.append("③ 更新清单：%.1fs  版本=%s  %d 字节  sha256=%s…" % (
            time.time() - t1, man["version"], man["size"], man["sha256"][:16]))
    except Exception as exc:                             # noqa: BLE001
        L.append("③ 更新清单：**失败** %r" % exc)
        man = None
        ok_all = False

    if man:
        t2 = time.time()
        ok, detail = vc.download_update(man)
        dt = max(time.time() - t2, 0.01)
        L.append("④ 下载安装包：%s（%s）" % ("成功" if ok else "**失败**", detail))
        ok_all = ok_all and ok
        if ok:
            L.append("   用时 %.1fs（%.1f MB/s）" % (
                dt, man["size"] / 1048576.0 / dt))
            h = hashlib.sha256()
            with open(detail, "rb") as fh:
                for blk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(blk)
            got = h.hexdigest()
            size = os.path.getsize(detail)
            same_hash = got == man["sha256"]
            same_size = size == man["size"]
            L.append("   字节数 %d == 清单 %d：%s" % (size, man["size"], same_size))
            L.append("   sha256 与清单一致：%s" % same_hash)
            ok_all = ok_all and same_hash and same_size
            try:
                os.remove(detail)
                L.append("   （已删掉下载到的安装包）")
            except OSError as exc:
                L.append("   ⚠ 删除失败：%r" % exc)

L.append("")
L.append("最终判定：%s" % ("✓ 使用者能更新到 " + ver if ok_all else "✗ 链路有问题"))

with open(OUT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(L) + "\n")
print("\n".join(L))
sys.exit(0 if ok_all else 1)
