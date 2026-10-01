# -*- coding: utf-8 -*-
"""把序列号生成器打成一个**独立 exe**，发到 verification-code 仓库。

为什么必须做这个
==================================================================
生成器现在是 `serial_check.py` —— 一个 Python 脚本。**没装 Python 的朋友
双击它只会弹"用什么程序打开？"**。而答错被锁之后，生成器是唯一的出口
（锁定不会过期），所以它必须"双击就能跑"。

做的事：
  1. 用 PyInstaller 把 serial_check.py 打成 onefile + console 的 exe；
  2. **先验证这个 exe 真的能用**：运行它、抓出它打印的序列号、
     再喂给本地 serial_check.verify() —— 必须通过（改一个字符必须被拒）；
  3. 在 verification-code 建发行版并把 exe 作为附件传上去
     （Gitee 的 raw 对大文件要求登录，附件才是匿名可下的那条路）；
  4. 匿名复核附件地址；
  5. 更新那个仓库的 README，把两种用法都写清楚。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OPT = os.path.join(HERE, "optimized")
OUT = os.path.join(HERE, "_genexe.txt")
L = []

sys.path.insert(0, OPT)
import serial_check                                        # noqa: E402

tok = os.environ.get("GITEE_TOKEN2", "").strip()
OWNER, REPO = "whr3443kklld", "verification-code"
TAG = "generator-1.0"

for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
            "ALL_PROXY", "all_proxy"):
    os.environ.pop(key, None)
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))

# ---------------- 1. 打包 ----------------
work = tempfile.mkdtemp(prefix="hvgen_")
L.append("临时目录：%s" % work)
L.append("")
L.append("== 1. PyInstaller 打包 ==")
cmd = [sys.executable, "-m", "PyInstaller",
       "--onefile", "--console", "--clean", "--noconfirm",
       "--name", "serial_check",
       "--distpath", os.path.join(work, "dist"),
       "--workpath", os.path.join(work, "build"),
       "--specpath", work,
       os.path.join(OPT, "serial_check.py")]
p = subprocess.run(cmd, capture_output=True, text=True, cwd=work, timeout=900)
L.append("  rc=%d" % p.returncode)
for line in (p.stdout or "").strip().splitlines()[-4:]:
    L.append("  | " + line)
if p.returncode != 0:
    for line in (p.stderr or "").strip().splitlines()[-8:]:
        L.append("  ! " + line)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    raise SystemExit("打包失败")

exe = os.path.join(work, "dist", "serial_check.exe")
if not os.path.isfile(exe):
    raise SystemExit("没找到产物 %s" % exe)
blob = open(exe, "rb").read()
L.append("  产物：%d 字节（%.1f MB）  sha256=%s…"
         % (len(blob), len(blob) / 1048576.0,
            hashlib.sha256(blob).hexdigest()[:16]))

# ---------------- 2. 验收这个 exe ----------------
L.append("")
L.append("== 2. 验收：真跑一遍这个 exe ==")
env = {k: v for k, v in os.environ.items()
       if not k.startswith("_PYI_") and k not in ("_MEIPASS", "_MEIPASS2")}
r = subprocess.run([exe], capture_output=True, timeout=180, env=env)
text = (r.stdout.decode("utf-8", "replace")
        + r.stderr.decode("utf-8", "replace"))
L.append("  运行 rc=%d，输出 %d 字符" % (r.returncode, len(text)))
codes = re.findall(r"[0-9A-Z]{5}(?:-[0-9A-Z]{5})+", text)
L.append("  抓到序列号：%r" % codes[:1])
ok = False
if codes:
    ok, why = serial_check.verify(codes[0])
    L.append("  程序侧 verify(%s) -> %s（%s）" % (codes[0], ok, why))
    bad = codes[0][:-1] + ("A" if codes[0][-1] != "A" else "B")
    ok2, why2 = serial_check.verify(bad)
    L.append("  改一个字符 -> %s（%s）" % (ok2, why2))
    ok = ok and not ok2
L.append("  结论：%s" % ("✓ exe 生成的码，程序认" if ok else "✗ 有问题，先别上传"))
if not ok:
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    raise SystemExit("exe 验收没过")

# ---------------- 3. 传发行版附件 ----------------
L.append("")
L.append("== 3. 传到 verification-code 的发行版 ==")


def api(path, method="GET", fields=None, files=None, timeout=900):
    url = "https://gitee.com/api/v5" + path
    data = None
    headers = {"User-Agent": "haavik-release"}
    if files is not None:
        boundary = "----haavik" + os.urandom(8).hex()
        parts = []
        for k, v in (fields or {}).items():
            parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\""
                          "\r\n\r\n%s\r\n" % (boundary, k, v)).encode("utf-8"))
        name, content = files
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\";"
                      " filename=\"%s\"\r\nContent-Type: application/"
                      "octet-stream\r\n\r\n" % (boundary, name)).encode("utf-8"))
        parts.append(content)
        parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
        data = b"".join(parts)
        headers["Content-Type"] = ("multipart/form-data; boundary=%s"
                                   % boundary)
    elif method == "GET":
        q = dict(fields or {})
        q["access_token"] = tok
        url += "?" + urllib.parse.urlencode(q)
    else:
        body = dict(fields or {})
        body["access_token"] = tok
        data = urllib.parse.urlencode(body).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(1 << 22)
            return resp.status, (json.loads(raw.decode("utf-8")) if raw else {})
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(300).decode("utf-8", "replace")


rid = None
s, res = api("/repos/%s/%s/releases" % (OWNER, REPO), method="POST", fields={
    "tag_name": TAG, "name": "序列号生成器（免 Python）",
    "body": "不会用 Python 就直接下这个 exe，双击即可。\n\n"
            "它和 serial_check.py 是同一份算法，生成的码程序一定认。",
    "target_commitish": "master"})
if isinstance(res, dict) and res.get("id"):
    rid = res["id"]
    L.append("  发行版 %s 已创建 id=%s" % (TAG, rid))
else:
    L.append("  建发行版返回 %s，找找已有的" % s)
    s2, rels = api("/repos/%s/%s/releases" % (OWNER, REPO))
    if isinstance(rels, list):
        rid = next((r["id"] for r in rels if r.get("tag_name") == TAG), None)
    L.append("  复用 id=%s" % rid)

if rid:
    s3, detail = api("/repos/%s/%s/releases/tags/%s" % (OWNER, REPO, TAG))
    have = [a.get("name") for a in (detail.get("assets") or [])] \
        if isinstance(detail, dict) else []
    L.append("  现有附件：%r" % have)
    if "serial_check.exe" in have:
        L.append("  已经有同名附件，跳过上传")
    else:
        s4, up = api("/repos/%s/%s/releases/%s/attach_files"
                     % (OWNER, REPO, rid), method="POST",
                     fields={"access_token": tok},
                     files=("serial_check.exe", blob))
        L.append("  上传返回 %s：%s" % (s4, json.dumps(up, ensure_ascii=False)[:160]
                                   if isinstance(up, dict) else up))

# ---------------- 4. 匿名复核 ----------------
L.append("")
L.append("== 4. 匿名复核（不带令牌）==")
asset = ("https://gitee.com/%s/%s/releases/download/%s/serial_check.exe"
         % (OWNER, REPO, TAG))
try:
    req = urllib.request.Request(asset, headers={"User-Agent": "HAAVIK-Photo"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        head = resp.read(2)
    L.append("  %s -> 通 %s，开头 %r" % (asset, resp.status, head))
except urllib.error.HTTPError as exc:
    L.append("  匿名取不到：HTTP %s" % exc.code)
except Exception as exc:                                   # noqa: BLE001
    L.append("  匿名取不到：%r" % exc)

# ---------------- 5. 更新那个仓库的 README ----------------
README = """HAAVIK Photo —— 验证码仓库
================================

这里只有一样东西：**序列号生成器**。它和放安装包的那个仓库
（`haavk-photos---haavk-photo`）是分开的 —— 一个是给程序自动更新用的，
一个是给"答错题被锁住、要自己算一枚码"用的。

两种用法，挑一个
----------------
**1. 不会用 Python？下这个 exe（推荐）**

到本仓库的「发行版」里下载 `serial_check.exe`，**双击它**就会弹出一个小黑窗，
里面那串就是当月有效的序列号。复制下来填进程序即可。
不用装任何东西，不用命令行。

**2. 有 Python？直接用源码**

下载 `serial_check.py`，双击运行（或命令行 `python serial_check.py`）。

关于这串码
----------
它不是加密锁，是作者留的一道题：算法就在 `serial_check.py` 里，
能读代码就能自己算。生成器和程序里的校验逻辑是**同一份算法**，
所以生成出来的码程序一定认；它只对**生成它的那个月**有效（前后各容忍 1 个月）。
"""
cur = api("/repos/%s/%s/contents/README.md" % (OWNER, REPO))
fields = {"content": __import__("base64").b64encode(
              README.encode("utf-8")).decode("ascii"),
          "message": "readme: 补上免 Python 的 exe 用法",
          "branch": "master"}
if isinstance(cur, dict) and cur.get("sha"):
    fields["sha"] = cur["sha"]
s5, r5 = api("/repos/%s/%s/contents/README.md" % (OWNER, REPO),
             method="PUT" if fields.get("sha") else "POST", fields=fields)
L.append("")
L.append("== 5. README 更新 -> %s ==" % s5)

shutil.rmtree(work, ignore_errors=True)
L.append("（临时打包目录已删）")

with open(OUT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(L) + "\n")
print("\n".join(L[-14:]))
