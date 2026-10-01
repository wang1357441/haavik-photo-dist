# -*- coding: utf-8 -*-
"""给构建产物加 Authenticode 签名。

用途
==================================================================
用一张**自签名**代码签名证书给 dist/ 下的 exe 盖章，署名"哈夫克"。

必须清楚的边界（别误解）
==================================================================
1. **这不是"真签名"**。自签名证书没有 CA 背书，在任何**没有**导入了
   这张证书的机器上，签名链都终止于一个不受信任的根 —— Windows 属性页
   会显示"未知发布者 / 签名无效"。它只在你自己导入过证书的机器上显示为有效。
2. **它对杀软与 SmartScreen 信誉没有任何帮助，甚至可能是负分。**
   杀软的启发式对"有签名但验证不过"的文件，比"完全没签名"更警觉。
   真正的信任来自 CA 签发的证书（需实名主体 + 年费 + 送检），
   本项目刻意不走那条路（见 MEMORY.md 硬性约定 2）。
3. 所以它的作用只有两个：文件属性里有一个可读的署名；同一作者的多个
   产物可以用同一张证书互相印证。**仅此而已。**

为什么用 PowerShell 而不是 signtool
==================================================================
本机没有 Windows SDK，`signtool.exe` 不存在。PowerShell 自带的
`New-SelfSignedCertificate` / `Set-AuthenticodeSignature` 零依赖可用。

为什么签名不会破坏 PyInstaller onefile
==================================================================
签名只是往 PE 尾部追加证书表，不改任何节区内容。PyInstaller 定位内嵌归档
的方式是**从文件末尾向前扫描** cookie magic（`_find_magic_pattern`，
从 SEEK_END 往回扫），因此追加在末尾的证书表不影响归档发现。
即便如此，签完仍必须实测启动 —— 见 build.py 的 smoke_check()。

PowerShell 输出编码（踩过的坑）
==================================================================
中文系统上 PowerShell 默认按 GBK 写 stdout，Python 侧若用
`text=True` 会按 UTF-8 解码并抛 UnicodeDecodeError；异常发生在读线程里，
表现为 stdout 静默变空、随后走错分支。所以这里：
  * 脚本用 `-EncodedCommand`（UTF-16LE + base64）传入，绕开命令行编码；
  * 脚本内部先把 `[Console]::OutputEncoding` 设成 UTF8；
  * 读回时按**字节**取，再显式 UTF-8 解码。
"""
from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys

#: 署名主体。改这里等于换签名身份。
CERT_CN = "哈夫克"
CERT_O = "哈夫克"
CERT_SUBJECT = "CN=%s, O=%s, C=CN" % (CERT_CN, CERT_O)
#: 用 FriendlyName 定位证书，而不是 Subject —— Windows 会按自己的规则
#: 重排/规范化 Subject 字符串，拿 Subject 做等值比较不可靠。
CERT_FRIENDLY = "HAAVIK-CodeSigning"
CERT_YEARS = 10

_PS_PROLOGUE = (
    "$ErrorActionPreference='Stop';"
    "$ProgressPreference='SilentlyContinue';"
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
)


def find_powershell() -> str:
    """定位 powershell.exe。优先用 System32 下的绝对路径。"""
    for cand in (
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                     "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
        "powershell.exe",
    ):
        if os.path.isfile(cand) or cand == "powershell.exe":
            return cand
    return "powershell.exe"


def _ps_literal(text: str) -> str:
    """把字符串转成 PowerShell 单引号字面量（单引号自身用两个表示）。"""
    return "'" + text.replace("'", "''") + "'"


def run_ps(script: str) -> str:
    """执行一段 PowerShell 脚本，返回 stdout（已按 UTF-8 解码）。"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    proc = subprocess.run(
        [find_powershell(), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,  # 关键：按字节取，不用 text=True
    )
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    err = proc.stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(
            "PowerShell 执行失败 (rc=%d)\n--- stdout ---\n%s\n--- stderr ---\n%s"
            % (proc.returncode, out, err)
        )
    return out


def _cert_lookup_ps(require_private_key: bool) -> str:
    cond = " -and $_.HasPrivateKey" if require_private_key else ""
    return (
        "$cert = Get-ChildItem Cert:\\CurrentUser\\My -CodeSigningCert -ErrorAction SilentlyContinue"
        " | Where-Object { $_.FriendlyName -eq %s%s }"
        " | Sort-Object NotAfter -Descending | Select-Object -First 1;"
        % (_ps_literal(CERT_FRIENDLY), cond)
    )


def ensure_cert() -> dict:
    """确保签名证书存在，不存在则创建。返回 {thumbprint, subject, not_after}。"""
    script = (
        _PS_PROLOGUE
        + _cert_lookup_ps(require_private_key=True)
        + "if (-not $cert) {"
        + "  $cert = New-SelfSignedCertificate"
        + " -Type CodeSigningCert"
        + " -Subject %s" % _ps_literal(CERT_SUBJECT)
        + " -FriendlyName %s" % _ps_literal(CERT_FRIENDLY)
        + " -CertStoreLocation Cert:\\CurrentUser\\My"
        + " -KeyAlgorithm RSA -KeyLength 2048 -HashAlgorithm SHA256"
        + " -KeyUsage DigitalSignature"
        + " -NotAfter (Get-Date).AddYears(%d);" % CERT_YEARS
        + "  Write-Output 'CREATED=1';"
        + "} else { Write-Output 'CREATED=0'; };"
        + "Write-Output ('THUMBPRINT=' + $cert.Thumbprint);"
        + "Write-Output ('SUBJECT=' + $cert.Subject);"
        + "Write-Output ('NOTAFTER=' + $cert.NotAfter.ToString('yyyy-MM-dd'));"
    )
    info = {}
    for line in run_ps(script).splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            info[k.strip()] = v.strip()
    if not info.get("THUMBPRINT"):
        raise RuntimeError("证书创建后仍拿不到指纹，PowerShell 返回：%r" % info)
    return {
        "created": info.get("CREATED") == "1",
        "thumbprint": info["THUMBPRINT"],
        "subject": info.get("SUBJECT", ""),
        "not_after": info.get("NOTAFTER", ""),
    }


def signature_status(paths) -> list:
    """读取每个文件的签名状态，不修改文件。"""
    if not paths:
        return []
    arr = ",".join(_ps_literal(p) for p in paths)
    script = (
        _PS_PROLOGUE
        + "foreach ($p in @(%s)) {" % arr
        + "  if (-not (Test-Path -LiteralPath $p)) { Write-Output ('MISSING|' + $p); continue };"
        + "  $s = Get-AuthenticodeSignature -LiteralPath $p;"
        + "  $subj = ''; if ($s.SignerCertificate) { $subj = $s.SignerCertificate.Subject };"
        + "  Write-Output ('ROW|' + $p + '|' + $s.Status + '|' + $subj);"
        + "}"
    )
    rows = []
    for line in run_ps(script).splitlines():
        parts = line.split("|")
        if parts[0] == "MISSING":
            rows.append({"path": parts[1], "exists": False, "status": "MISSING", "signer": ""})
        elif parts[0] == "ROW":
            rows.append({
                "path": parts[1], "exists": True,
                "status": parts[2], "signer": parts[3] if len(parts) > 3 else "",
            })
    return rows


def sign_files(paths, thumbprint: str | None = None) -> list:
    """给文件盖章（就地修改）。返回签名后的状态行。"""
    paths = [os.path.abspath(p) for p in paths]
    for p in paths:
        if not os.path.isfile(p):
            raise SystemExit("[ERROR] 找不到要签名的文件：%s" % p)
    arr = ",".join(_ps_literal(p) for p in paths)
    script = (
        _PS_PROLOGUE
        + _cert_lookup_ps(require_private_key=True)
        + "if (-not $cert) { throw '找不到可用的代码签名证书（须有私钥）。先跑 --ensure-cert。' };"
        + "foreach ($p in @(%s)) {" % arr
        + "  $null = Set-AuthenticodeSignature -FilePath $p -Certificate $cert -HashAlgorithm SHA256;"
        + "  $s = Get-AuthenticodeSignature -LiteralPath $p;"
        + "  $subj = ''; if ($s.SignerCertificate) { $subj = $s.SignerCertificate.Subject };"
        + "  Write-Output ('ROW|' + $p + '|' + $s.Status + '|' + $subj);"
        + "}"
    )
    rows = []
    for line in run_ps(script).splitlines():
        parts = line.split("|")
        if parts[0] == "ROW":
            rows.append({
                "path": parts[1], "status": parts[2],
                "signer": parts[3] if len(parts) > 3 else "",
            })
    for r in rows:
        if not r["signer"]:
            raise RuntimeError("签名未生效（SignerCertificate 为空）：%s" % r["path"])
    return rows


def trust_cert() -> str:
    """把证书导入 当前用户\\受信任的根 与 受信任的发布者。

    ⚠️ 这会让本机把这**一整张证书私钥**所签的任何东西都视为可信。
    只在明确知道后果时使用；不要放进自动构建流程。
    """
    script = (
        _PS_PROLOGUE
        + _cert_lookup_ps(require_private_key=False)
        + "if (-not $cert) { throw '找不到证书，先跑 --ensure-cert' };"
        + "foreach ($name in @('Root','TrustedPublisher')) {"
        + "  $st = New-Object System.Security.Cryptography.X509Certificates.X509Store($name,'CurrentUser');"
        + "  $st.Open('ReadWrite');"
        + "  $st.Add($cert);"
        + "  $st.Close();"
        + "  Write-Output ('IMPORTED=' + $name);"
        + "}"
    )
    return run_ps(script)


def main() -> int:
    parser = argparse.ArgumentParser(description="给构建产物加自签名（署名 %s）" % CERT_CN)
    parser.add_argument("--ensure-cert", action="store_true", help="确保签名证书存在（不存在则创建）")
    parser.add_argument("--status", action="store_true", help="只读：打印文件签名状态")
    parser.add_argument("--trust", action="store_true",
                        help="把证书导入当前用户的受信任根（危险，见函数说明）")
    parser.add_argument("files", nargs="*", help="要签名/查询的 exe 路径")
    args = parser.parse_args()

    if args.ensure_cert:
        info = ensure_cert()
        print("[%s] 证书 %s" % ("新建" if info["created"] else "已存在", info["thumbprint"]))
        print("       主体   : %s" % info["subject"])
        print("       有效期至: %s" % info["not_after"])

    if args.trust:
        print(trust_cert())

    if args.status:
        for r in signature_status([os.path.abspath(f) for f in args.files]):
            print("  %-60s %s" % (os.path.basename(r["path"]), r["status"]))
            if r["signer"]:
                print("      signer: %s" % r["signer"])
        return 0

    if not args.files:
        if not (args.ensure_cert or args.trust):
            parser.print_help()
        return 0

    ensure_cert()
    for r in sign_files(args.files):
        print("  [已签名] %-46s %s" % (os.path.basename(r["path"]), r["status"]))
        print("           signer: %s" % r["signer"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
