# -*- coding: utf-8 -*-
"""一键发版工具（发布这一步交给项目作者自己跑）。

为什么单独一个脚本
==================================================================
发版要改的"同一件事"散在五处：`version_check.APP_VERSION`（唯一权威）、
`installer.APP_VERSION`（有意副本，它不 import version_check）、
两份 `version*.txt` 里各四处版本号、以及公开仓库里的 `manifest.json`
与 `version.txt`。少改一处，症状是"老版本用户看不到更新"或
"文件属性页自相矛盾"——两种都很难从表面看出来。所以全部由这里代劳。

用法
==================================================================
一条命令走完（推荐）：

    python code_review/release.py --set-version 1.3.3 --build --audit --publish --publish-github

依次做四件事：
  1. 改四处版本号（`version_check.APP_VERSION`、`installer.APP_VERSION`、
     两份 `version*.txt` 里各两处）；
  2. 重建（`build.py` 自己会断言版本号一致）；
  3. 审计（`verify_payload.py`，退出码必须 0）；
  4. 发 **Gitee 分发仓**：建发行版 + 传 `Setup.exe` + 更新 `manifest.json`
     与 `version.txt`，最后**匿名**复核三个端点是否真能被未登录的人读到；
  5. 发 **GitHub 分发仓**：把同一份 `manifest.json` / `version.txt` 也 PUT
     到 ``wang1357441/haavik-photo-dist`` 的 main 分支，jsdelivr 缓存的就是它
     —— 这是 2026-09-23 加的"清单第二源"，让国内那批直连不到 GitHub 的人
     也能拿到清单（清单本体仍走 Gitee 优先、jsdelivr 备用两路冗余）。

发完再收尾（提交源码 + 打 tag + 推送）：

    python code_review/release.py --commit -m "release 1.3.3"

发版后**必跑**这一步复核（也可以加 `--verify` 让上面那步自动带上）：

    python code_review/_verify_release.py

**发布目标只有 Gitee**（国内直连、朋友匿名可下）。GitHub 那条路保留但不默认走：
本机直连 github.com 不通、账号也仍被限流，所以要发 GitHub 得显式加
`--publish-github`。

**桌面一个文件都不放**：不部署、也不往桌面塞安装包。发到 Gitee 之后，
使用者自己在程序里点「检查更新」，或自己去仓库下载 —— 装与不装是他的事。

**生成器不归这里管**：`serial_check.py` 在另一个仓库（`verification-code`）。
发版流程一个字都不碰它；万一被传进分发仓，两个仓库的职责就混了。

令牌从哪来
==================================================================
**不写死在文件里，也不落盘**：
  * 发 Gitee → 环境变量 ``GITEE_TOKEN``（Gitee 私人令牌；勾 projects 即可）；
  * 发 GitHub / 推送源码仓 → 环境变量 ``GITHUB_TOKEN``，
    或本机 git 凭据管理器（`git credential fill`，凭据由 wincred 加密保管）。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OPT = os.path.join(HERE, "optimized")

GIT = os.environ.get(
    "HAAVIK_GIT",
    r"C:\Users\王浩然\.workbuddy\binaries\PortableGit\versions\1.2.0\cmd\git.exe")
GIT_EXEC_PATH = os.path.dirname(GIT).replace("\\cmd", r"\mingw64\bin")

#: **真正在用的分发目标**：Gitee 公开仓（国内直连、匿名可读）。
#: 仓库职责写死在这里，避免以后又混：只放更新通道要用的三个文件 ——
#: Setup.exe / manifest.json / version.txt。
GITEE_OWNER = "whr3443kklld"
GITEE_DIST = "haavk-photos---haavk-photo"
GITEE_CODE_REPO = "verification-code"   # 生成器住这儿，发版流程一个字都不碰
GITEE_API = "https://gitee.com/api/v5"

#: GitHub 侧的目标。**注意不是存放源码那个账号**：
#: 源码在 `hhhs81`（那个账号被 GitHub 隐藏了，匿名全 404，见 MEMORY），
#: 而分发用 `wang1357441` —— 它是 2026-01-04 注册的老账号，**匿名可访问已验证 ✓**
#: （2026-09-22：个人主页与匿名 API 都是 200）。分发仓里**只放二进制，不放源码**。
DIST_REPO = "wang1357441/haavik-photo-dist"
#: ⚠ 这个仓同时也是**清单的第二个地址**（见 :data:`version_check.MANIFEST_URLS`）：
#: jsdelivr 把它的 main 分支当 CDN 缓存，国内匿名可读、1.7 秒拿到响应
#: （2026-09-23 实测）—— 这意味着"**每次发版都同时跑 `--publish --publish-github`**"
#: 才会让 jsdelivr 这面镜子保持新鲜；只跑 `--publish` 时，jsdelivr 缓存的还是上次
#: 发 GitHub 时的版本，并不会自己同步。
PRIVATE_REPO = "hhhs81/haavik-photo"
USER_AGENT = "haavik-release"

#: **备用镜像**（多源分发）。加一行，就等于多一个分发平台。
#:
#: 每项是 ``(平台名, 下载地址模板)``；模板里的 ``{tag}`` / ``{name}`` 会被替换。
#: 生成清单时，这些地址会写进每个组件的 ``urls``，新版程序按顺序试
#: （见 ``version_check.component_urls``）—— **某一个平台挂了、满了、被墙了，
#: 已经装了程序的人照样能更新**。
#:
#: 为什么这件事必须做：Gitee 单仓库附件上限 1 GB，我们 4 个版本已经用掉 414 MB。
#: 万一哪天不够用要换平台，**只要清单里的 ``url`` 改成新平台的地址**，
#: 老用户（只认 ``url`` 的那批）也能跟着走 —— 这就是"换平台不丢老用户"的前提。
#:
#: ⚠ **加镜像之前必须先匿名验证它真的能下**（别只看"上传成功"）：
#:   2026-09-22 就栽过一次 —— `hhhs81` 那个账号在 GitHub 眼里是"被隐藏"状态，
#:   带令牌一切正常（`state=uploaded`、能下到字节数正确的文件），**匿名却全 404**。
#:   所以 `MIRRORS` 里的每一项，都必须先通过"不带令牌真下下来、比 sha256"这一关。
MIRRORS = (
    # GitHub Releases（公开分发仓；发行版总量无上限、单文件 2 GB）
    ("github", "https://github.com/wang1357441/haavik-photo-dist"
               "/releases/download/{tag}/{name}"),
    # Gitee 的第二个仓库（每仓 1 GB 附件，等于又一块盘；要先建仓再用）：
    # ("gitee2", "https://gitee.com/whr3443kklld/haavk-photos-2/releases/download/{tag}/{name}"),
    # ModelScope 魔搭（国内 CDN，适合将来几百 MB 的 AI 模型包）：
    # ("modelscope", "https://www.modelscope.cn/models/<你的账号>/<仓名>/resolve/master/{name}"),
)

#: **最后兜底**的镜像：永远排在 ``url``（主源）**后面**。
#:
#: 这里放"能用但慢"的源。为什么不放进 ``MIRRORS``：``MIRRORS`` 在
#: :data:`MIRROR_FIRST` 下会排在主源**前面**，而实测 `ghfast.top`（GitHub 加速站）
#: 只有 **≈57 KB/s**，主源 Gitee 是 **≈1 MB/s** —— 排前面会让每次下载慢十几倍。
#: 放兜底位，它的意义就变成"**主源和 GitHub 都不行时，还有一条路**"。
#:
#: ⚠ 第三方加速站的信任边界：**内容有 sha256 兜底**（它换不了文件），
#: 但它能看到"谁在什么时候下了什么"；而且这类站关停很常见（下面那批已经全关了）。
#: 所以它只能待在兜底位 —— 平时根本不会被用到。
#:
#: 2026-09-23 复测 15 个候选（含镜像站系 kkgithub / bgithub）：
#:   直连 github.com 读不到；ghproxy 系（mirror.ghproxy.com / gh-proxy.com /
#:   gh.llkk.cc / ghp.ci / hub.gitmirror.com / ghproxy.cc / ghproxy.link /
#:   github.moeyy.xyz / ghproxy.top / ur1.fun / gitclone / hub.nuaa.cf /
#:   hub.yzuu.cf / gh.api.99988866.xyz）**全不通**；
#:   `gh-proxy.net` 只回 31KB 错误页、`ghps.cc` 只回 4259 字节 —— 都是**假通过**，
#:   被"限时读满 256KB"的探针抓出来了。
#:
#: ⚠⚠ **2026-09-24 大修正 —— 上面那批结论有一多半是错的**：
#:   旧验法"取头/中/尾三处 offset 比 sha256"隐含要求服务器支持 ``Range``，
#:   但这些加速站**大多不支持 Range**（连 Gitee 都不支持！回 200、忽略 Range），
#:   于是三处都取回文件开头、指纹必然对不上 → 被误判成"内容不对"。
#:   **改用唯一可靠的办法后**（整段下载比全文 sha256 + 长度相等 + 开头是 MZ），
#:   实测**8 个是活的**，而且**不输 Gitee**：
#:     `gh-proxy.com` 1053~1570 KB/s ✓ 全文 sha256 与 Gitee 逐字节一致
#:     `gh.xxooo.cf`  1180~1433 KB/s ✓ 一致
#:     `ghfast.top`    307~940  KB/s ✓
#:     `gh.xmly.dev`   292~501  KB/s ✓
#:     `gh.ddlc.top`   397~550  KB/s ✓
#:     `hk.gh-proxy.com` / `cdn.gh-proxy.com` 约 1000 KB/s ✓
#:     `ghproxy.net` 28 KB/s ✗ 太慢（只够兜底，先不加）
#:   对照：**Gitee 714~2285 KB/s（波动大）；GitHub 直连 50 KB/s（读 2MB 还超时）**
#:   → 所以快的那批**提到 ``MIRRORS`` 排前面**，慢的留这里兜底。
#:
#: ⚠ 都得走**前缀形式**（`https://<站>/https://github.com/...`）；
#:   写成 `<站>/<owner>/<repo>/...` 是 404（2026-09-23 实测过一次）。
FALLBACK_MIRRORS = (
    ("ghfast", "https://ghfast.top/https://github.com/wang1357441"
               "/haavik-photo-dist/releases/download/{tag}/{name}"),
    ("xmly", "https://gh.xmly.dev/https://github.com/wang1357441"
             "/haavik-photo-dist/releases/download/{tag}/{name}"),
    ("ddlc", "https://gh.ddlc.top/https://github.com/wang1357441"
             "/haavik-photo-dist/releases/download/{tag}/{name}"),
)

#: **优先镜像**（2026-09-24 新增）：实测**比 Gitee 还快或相当**的那批。
#: 因为它们进 ``MIRRORS``，会排在 ``url``（Gitee）**前面** ——
#: 这正是使用者要的"**谁快谁前、Gitee 放最后**"。
FAST_MIRRORS = (
    ("ghproxy", "https://gh-proxy.com/https://github.com/wang1357441"
                "/haavik-photo-dist/releases/download/{tag}/{name}"),
    ("xxooo", "https://gh.xxooo.cf/https://github.com/wang1357441"
              "/haavik-photo-dist/releases/download/{tag}/{name}"),
)


#: 新客户端的**偏好顺序**：先试镜像，还是先试 ``url``（Gitee）。
#:
#: 2026-09-22 使用者定的战略：**以后要以别的平台为主**（更新包只会越来越大，
#: Gitee 单仓附件只有 1 GB），所以要"从现在就开始练它"，将来脱离时才顺。
#: 但实测 GitHub 从国内**拉不动**（首字节 47~50 秒、约 20 KB/s，
#: 97MB 的包读 298 秒直接超时；换 UA 也没用，是线路问题）。
#: 所以客户端那边加了一道**健康检查**（``version_check.source_ready``）：
#: 每个源真下载前要**限时读满 256KB**，做不到就跳过。
#: 效果：把镜像排第一的代价压到约 8 秒，然后自动落到 Gitee；
#: 哪天线路变好了，健康检查一通过，它就自己顶上来了 —— 不用改代码。
#:
#: ⚠ **``url`` 永远会被补在最后当兜底**（见 ``version_check.component_urls``），
#: 所以老客户端（只认 ``url``）照旧走 Gitee，一点不受影响。
MIRROR_FIRST = True
#: 若某天确认镜像真的拉得动了，可以把下面这行也打开，让 ``url`` 直接指向镜像
#: （那一步影响老客户端，要慎重）。
# UNSAFE_MOVE_URL_TO_MIRROR = True


def mirror_urls(name: str, tag: str, primary: str) -> list:
    """某个文件的一组下载地址，列表顺序＝**实际的尝试顺序**。

    顺序是：``FAST_MIRRORS`` + ``MIRRORS``（偏好源；:data:`MIRROR_FIRST` 为真时
    排最前）→ ``primary``（也就是清单里的 ``url``）→ ``FALLBACK_MIRRORS``（最后兜底）。

    2026-09-24 起 ``FAST_MIRRORS``（实测比 Gitee 还快的镜像）排在最前面 ——
    这就是使用者要的"**谁快谁前、Gitee 放最后**"。客户端那侧还会把 ``url``
    再补在最后（见 ``version_check.component_urls``），所以这里的顺序只决定
    **偏好**，不会让任何客户端"少一个选择"。
    """
    def _expand(pairs):
        out = []
        if not tag:
            return out                 # 拿不到 tag 就别拼镜像地址（拼出来一定是死链）
        for _label, tpl in pairs:
            try:
                url = tpl.format(tag=tag, name=name)
            except Exception:                                  # noqa: BLE001
                continue
            if url and url not in out:
                out.append(url)
        return out

    fast = _expand(FAST_MIRRORS)
    head, tail = _expand(MIRRORS), _expand(FALLBACK_MIRRORS)
    ordered = (fast + head + [primary] + tail) if MIRROR_FIRST \
        else ([primary] + fast + head + tail)
    out: list = []
    for url in ordered:
        if url and url not in out:
            out.append(url)
    return out

FILES_WITH_VERSION = (
    os.path.join(OPT, "version_check.py"),
    os.path.join(OPT, "installer.py"),
    os.path.join(OPT, "version.txt"),
    os.path.join(OPT, "version_setup.txt"),
)


#: 本进程内存里登记过的令牌值 —— 只在内存，永不落盘。见 :func:`_scrub`。
_SECRETS = []


def _remember_secret(value: str) -> None:
    """把一把令牌登记进"输出脱敏表"。

    为什么需要这一层：Gitee v5 的 GET 是**把令牌拼在 URL query 里**的
    （``?access_token=…``，见 :func:`gitee`）。只要有哪条异常消息把 URL
    带出来（``URLError``/``HTTPError``/自己拼的 f-string 都可能），
    令牌就跟着进日志了。与其逐处 review，不如在**唯一出口**擦一遍。
    """
    v = (value or "").strip()
    if v and v not in _SECRETS:
        _SECRETS.append(v)


def _scrub(text) -> str:
    """把输出里的令牌擦干净（值、``access_token=``、``Authorization`` 头）。"""
    s = str(text)
    for v in _SECRETS:
        if v:
            s = s.replace(v, "***")
    s = re.sub(r"(access_token=)[^&\s\"'#]+", r"\1***", s)
    s = re.sub(r"(Authorization:\s*token\s+)\S+", r"\1***", s, flags=re.I)
    s = re.sub(r"(://)[^/@\s]+:[^/@\s]+@", r"\1***:***@", s)   # URL 里的 user:pass@
    return s


def fingerprint(secret: str) -> str:
    """令牌指纹：够用来确认"两次用的是同一把"，但反推不出令牌本身。"""
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()[:8]


def say(msg=""):
    """**所有**对外输出都走这里 —— 顺手脱敏（见 :func:`_scrub`）。"""
    print(_scrub(msg), flush=True)


# --------------------------------------------------------------------------
# 令牌
# --------------------------------------------------------------------------
def _token_from_user_env(name: str) -> str:
    """从**用户级持久环境变量**（``HKCU\\Environment``）里读一个值。

    为什么不能只信 ``os.environ``：**刚设好的用户环境变量，不会出现在"已经开着"的
    进程里** —— Windows 只把它发给之后新启动的进程。而我这边的工具进程就是这么一个
    "已经开着的老进程"：注册表里明明写了，``os.environ`` 却看不到。
    所以查令牌**一律直接读注册表**（这也是记忆里那条"查令牌别信子进程 env"的由来）。
    """
    if os.name != "nt":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            return str(winreg.QueryValueEx(key, name)[0] or "").strip()
    except OSError:
        return ""


def get_token() -> str:
    # 顺序：环境变量 → 用户级持久环境变量（注册表）→ git 凭据管理器。
    # 注册表要排在凭据管理器**前面**：凭据管理器里可能躺着**上一个账号**的令牌，
    # 而我们刚设的用户变量才是"现在要用哪个账号"的答案。
    for source, tok in (("环境变量 GITHUB_TOKEN", os.environ.get("GITHUB_TOKEN")),
                        ("用户环境变量 GITHUB_TOKEN（HKCU\\Environment）",
                         _token_from_user_env("GITHUB_TOKEN"))):
        tok = str(tok or "").strip()
        if tok:
            _remember_secret(tok)
            say("用%s（指纹 %s）" % (source, fingerprint(tok)))
            return tok
    env = dict(os.environ)
    env["GIT_EXEC_PATH"] = GIT_EXEC_PATH
    p = subprocess.run(
        [GIT, "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
        text=True, capture_output=True, env=env, timeout=60)
    m = re.search(r"^password=(.+)$", p.stdout, re.M)
    if not m:
        raise SystemExit("拿不到 GitHub 凭据：设 GITHUB_TOKEN，或先让 git 登录一次")
    tok = m.group(1).strip()
    _remember_secret(tok)
    say("用 git 凭据管理器里的令牌（指纹 %s，⚠ 可能是旧账号的）" % fingerprint(tok))
    return tok


def api(token, url, method="GET", body=None, timeout=120):
    """调 GitHub REST API 并解析 JSON。

    ⚠ **响应体可能是空的**（2026-09-24 踩到）：删除类接口成功时回
    ``204 No Content``，body 是空的。早期版本无条件 ``json.loads()``，
    于是"删掉同名旧附件"这一步会抛
    ``JSONDecodeError: Expecting value: line 1 column 1``——
    第一次发布没有旧附件可删所以看不出来，**第二次发布会炸**。
    空 body 就返回 ``{}``，调用方本来也不看它的返回值。
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "token " + token, "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(1 << 22)
    if not raw.strip():
        return {}                       # 204 No Content（删除成功就是这种）
    return json.loads(raw.decode("utf-8"))


def upload_asset(token, url, path, timeout=900, tries=3):
    """上传附件：**body 必须是文件字节**（发 JSON 会 422）。

    ⚠ 2026-09-24 加**重试**：GitHub 的附件上传在国内线路上**很不稳** ——
    实测发 67MB 的 AI 包时抛 ``URLError: The write operation timed out``
    （连接建上了、也开了传，传到一半写不动了）。跟 Gitee 那边一样，重试能救。

    重试前先**清掉可能残掉的上传**：GitHub 对"同名附件"是直接 422 拒绝的
    （不像 Gitee 会跳过），所以失败重来时得先把那个可能已经建了一半、
    或者上一次真的传成功了的同名附件删掉，否则第二次一定 422。
    """
    import time as _time
    data = open(path, "rb").read()
    name = os.path.basename(path)
    last: Exception | None = None
    for attempt in range(1, max(1, tries) + 1):
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": "token " + token, "User-Agent": USER_AGENT,
            "Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read(1 << 22).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read(400).decode("utf-8", "replace")
            except Exception:                                # noqa: BLE001
                pass
            last = exc
            if exc.code != 422:
                raise
            say("  上传 %s 返回 422（多半是同名附件还在）：%s"
                % (name, body.strip()[:200]))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
            say("  上传 %s 第 %d 次失败：%s" % (name, attempt, exc))
        if attempt < tries:
            _time.sleep(4 * attempt)
            say("  重试第 %d 次之前，先删掉可能残留的同名附件" % (attempt + 1))
            _delete_release_asset(token, name)
    if last is not None:
        raise last
    raise RuntimeError("上传 %s 失败" % name)


def _delete_release_asset(token: str, name: str) -> None:
    """删掉当前 Release 上所有叫 ``name`` 的附件（没有就什么都不做）。

    需要它是因为 GitHub 不允许两条同名附件；而"上传失败重来"时，
    上一次可能已经建好了一条。找不到 Release 也**不抛** —— 这是清理动作，
    不该成为主流程的绊脚石。
    """
    try:
        rel = api(token, "https://api.github.com/repos/%s/releases/tags/%s"
                  % (DIST_REPO, "v" + current_version()))
        for a in rel.get("assets", []):
            if a.get("name") == name:
                api(token, "https://api.github.com/repos/%s/releases/assets/%s"
                    % (DIST_REPO, a["id"]), method="DELETE")
                say("    已删掉残留的同名附件 %s" % name)
    except Exception as exc:                                # noqa: BLE001
        say("    （清理同名附件时出错，继续：%s）" % exc)


def put_content(token, repo, path_in_repo, text, message):
    body = {"message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii")}
    try:
        cur = api(token, "https://api.github.com/repos/%s/contents/%s"
                  % (repo, path_in_repo))
        body["sha"] = cur["sha"]
    except Exception:                                    # noqa: BLE001
        pass
    api(token, "https://api.github.com/repos/%s/contents/%s" % (repo, path_in_repo),
        method="PUT", body=body)


# --------------------------------------------------------------------------
# 1) 改版本号
# --------------------------------------------------------------------------
def current_version() -> str:
    sys.path.insert(0, OPT)
    import version_check
    return version_check.APP_VERSION


def set_version(new: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", new):
        raise SystemExit("版本号格式应为 X.Y.Z")
    maj, mid, patch = new.split(".")
    four = "(%s, %s, %s, 0)" % (maj, mid, patch)

    for path in FILES_WITH_VERSION:
        txt = open(path, encoding="utf-8").read()
        before = txt
        txt = re.sub(r'APP_VERSION = "[^"]+"', 'APP_VERSION = "%s"' % new, txt)
        txt = re.sub(r"filevers=\([^)]*\)", "filevers=" + four, txt)
        txt = re.sub(r"prodvers=\([^)]*\)", "prodvers=" + four, txt)
        txt = re.sub(r"u'\d+\.\d+\.\d+\.\d+'", "u'%s.0'" % new, txt)
        if txt != before:
            open(path, "w", encoding="utf-8", newline="\n").write(txt)
            say("  改了 %s" % os.path.relpath(path, ROOT))
    # 自检：四处是否真的一致
    sys.path.insert(0, OPT)
    import version_check
    import installer
    if version_check.APP_VERSION != new or installer.APP_VERSION != new:
        raise SystemExit("改完仍不一致，中止（别急着往下走）")
    for path in FILES_WITH_VERSION[2:]:
        if new not in open(path, encoding="utf-8").read():
            raise SystemExit("%s 里没看到新版本号" % path)
    say("版本号已统一为 %s" % new)


# --------------------------------------------------------------------------
# 2a) 发布到 Gitee 公开分发仓（**真正在用的那条路**）
# --------------------------------------------------------------------------
def sha256_of(path: str) -> tuple:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest(), os.path.getsize(path)


def get_gitee_token() -> str:
    tok = os.environ.get("GITEE_TOKEN", "").strip()
    if not tok:
        raise SystemExit(
            "发 Gitee 需要环境变量 GITEE_TOKEN（Gitee 私人令牌，勾 projects 即可）。\n"
            "提醒：令牌只用于上传，读更新的人不需要它 —— 所以它永远不会进安装包。")
    _remember_secret(tok)
    # ⚠ 这里**只报指纹和长度**。以前打的是 `tok[:6]…tok[-4:]`，10 位明文
    #   跟着发版日志进了 build_work/release_run*.log（2026-09-21 实测确实在）。
    #   指纹同样能确认"两次用的是同一把令牌"，但推不出令牌。
    say("用环境变量 GITEE_TOKEN（长度 %d，指纹 %s）" % (len(tok), fingerprint(tok)))
    return tok


def gitee(token, path, method="GET", fields=None, timeout=120):
    """Gitee v5 调用：GET 走 query，其它方法走 form body。"""
    url = GITEE_API + path
    data = None
    if method == "GET":
        p = dict(fields or {})
        p["access_token"] = token
        url += "?" + urllib.parse.urlencode(p)
    else:
        b = dict(fields or {})
        b["access_token"] = token
        data = urllib.parse.urlencode(b).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"User-Agent": USER_AGENT,
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read(1 << 22)
        return json.loads(body.decode("utf-8")) if body else {}


def gitee_put_text(token, repo, path_in_repo, text, message):
    """写/改仓库里的一个文本文件。

    ⚠️ Gitee 这里有个必须记住的区别：**新建用 POST，覆盖用 PUT**。
    一律用 POST 的话，第二次发版（文件已存在）会得到一句干巴巴的
    ``HTTP 400 Bad Request`` —— 2026-09-17 真踩过。
    """
    url = "/repos/%s/%s/contents/%s" % (GITEE_OWNER, repo, path_in_repo)
    fields = {"content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
              "message": message, "branch": "master"}
    method = "POST"                                  # 默认按"新建"
    try:
        cur = gitee(token, url)
        if isinstance(cur, dict) and cur.get("sha"):
            fields["sha"] = cur["sha"]
            method = "PUT"                           # 已经存在 → 必须 PUT
    except Exception:                                # noqa: BLE001
        pass                                         # 取不到就按新建试
    gitee(token, url, method=method, fields=fields)


def gitee_attach_exe(token, repo, release_id, exe, filename="Setup.exe",
                    max_retries: int = 3, per_attempt_timeout: int = 300):
    """把安装包作为**附件**传上去（``filename`` 决定下载地址里的文件名）。

    两个坑都踩过：必须 multipart；access_token 要作为**表单字段**传，
    放在 query 上会被拒。

    第三个坑（2026-09-23 实测）：Gitee 的大文件上传（69 MB+）时不时挂 —— 一次
    是 ``Errno 10054``（远程主机强迫关闭），一次是 ``write operation timed out``
    （900 秒还没传完）。所以这里加了**重试 + 单次超时收紧**：
      * 单次超时 300 秒（5 分钟），比一次上传 69MB 的合理时间略宽；
        单次挂了就别等了，立刻重试，免得一次上传卡死整次发版。
      * 失败退避 4s / 8s / 16s；
      * 3 次都失败就 raise —— 这种情况一定是 Gitee 那边的真问题（限流、节点抖、
        网络真断），重复只会浪费时间和配额。
      * ⚠ 重试**可能留下孤儿附件**：第一次连接在第 50MB 处断了、Gitee 那侧
        其实落了一个空文件，下次 publish 时 :func:`publish_gitee` 的 ``have``
        列表会重读、能识别并跳过，**不会**双传同名的包。
    """
    boundary = "----haavik" + uuid.uuid4().hex
    head = ("--%s\r\nContent-Disposition: form-data; name=\"access_token\""
            "\r\n\r\n%s\r\n"
            "--%s\r\nContent-Disposition: form-data; name=\"file\"; "
            "filename=\"%s\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n"
            % (boundary, token, boundary, filename)).encode("utf-8")
    tail = ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    with open(exe, "rb") as fh:
        blob = fh.read()
    body = head + blob + tail

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(
                "%s/repos/%s/%s/releases/%s/attach_files"
                % (GITEE_API, GITEE_OWNER, repo, release_id),
                data=body, method="POST",
                headers={"User-Agent": USER_AGENT,
                         "Content-Type": "multipart/form-data; boundary=" + boundary})
            with urllib.request.urlopen(req, timeout=per_attempt_timeout) as resp:
                return json.loads(resp.read(1 << 22).decode("utf-8"))
        except (urllib.error.URLError, OSError) as exc:
            last_err = exc
            say("  上传 %s 第 %d/%d 次失败：%s" % (filename, attempt, max_retries, exc))
            if attempt >= max_retries:
                raise
            time.sleep(min(4 ** attempt, 16))
    raise last_err     # 不可达（for 循环一定 raise 或 return）


def anon_get(url, timeout=60):
    """不带任何令牌取一次 —— 这才是使用者（未登录）的真实处境。"""
    req = urllib.request.Request(url, headers={"User-Agent": "HAAVIK-Photo"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read(48)


def _ai_pack_version(path: str) -> str | None:
    """从 .pack（zip）里读 ai_manifest.json 的 version，作为发布清单里 ai 组件的版本。

    这样发布版本永远和包内真实版本一致，不会和 ai_ops.REQUIRED_AI_VERSION 漂移。
    """
    try:
        import json
        import zipfile
        with zipfile.ZipFile(path) as zf:
            data = zf.read("ai_manifest.json")
        return json.loads(data).get("version")
    except Exception:                                # noqa: BLE001
        return None


def _remote_manifest(token: str) -> dict:
    """读分发仓当前的 ``manifest.json``（拿不到就返回空 dict，绝不抛）。"""
    try:
        old = gitee(token, "/repos/%s/%s/contents/manifest.json"
                    % (GITEE_OWNER, GITEE_DIST))
        if isinstance(old, dict) and old.get("content"):
            return json.loads(base64.b64decode(old["content"]).decode("utf-8"))
    except Exception as exc:                             # noqa: BLE001
        say("（读远端清单失败，按『没有旧清单』处理：%s）" % exc)
    return {}


def _github_manifest_anon() -> dict:
    """**不带令牌**读 GitHub dist 仓的 manifest.json（拿不到返回空 dict，绝不抛）。

    存在的唯一理由：``publish_gitee`` 手上只有 Gitee 令牌，但它需要知道
    **GitHub 那边**的 AI 包头挂在哪个 tag 上（见下面沿用 ai 段那段注释 ——
    两个站的 tag 常常不一样，猜错就是 6 条镜像腿全 404）。

    走的正是客户端自己那条路（contents API + ``Accept: raw``），所以"这一步测得通"
    和"使用者拿得到"是同一件事。未认证调用受 IP 限流，但每次发版只调一次。
    """
    url = ("https://api.github.com/repos/%s/contents/manifest.json" % DIST_REPO)
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "HAAVIK-Photo",
            "Accept": "application/vnd.github.raw",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
        data = json.loads(body.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:                                # noqa: BLE001
        say("（读 GitHub 清单失败，AI 镜像腿只能按旧办法拼：%s）" % exc)
        return {}


def _ai_mirror_urls_for_gitee(prev_ai: dict) -> list:
    """Gitee 清单里 AI 段的**镜像腿**该长什么样。

    ⚠ 这是个真踩过的坑（2026-09-25 实测）：AI 包在**两个站挂在不同的 tag** ——
    Gitee 不能替换同名附件，所以给 AI 包单独开了 ``ai-x.y.z``；而 GitHub 那边
    它和主程序共用 ``vX.Y.Z``。于是拿 Gitee 的 tag 去拼 GitHub 镜像地址，
    **6 条腿全 404**，只剩 Gitee 自己那条能下。

    顺序（能拿到就早返回）：
      1. GitHub 清单里那份 ai 的 ``urls`` —— 那是权威的 GitHub 地址；
      2. 上一版清单里原有的 ``urls``（它上一次是对的，别用一个猜的 tag 覆盖掉）；
      3. 拿 Gitee 的 tag 自己拼（旧行为，通常拼出来是死链，只当最后的保底）。
    """
    gh_ai = _github_manifest_anon().get("ai")
    if isinstance(gh_ai, dict) and gh_ai.get("url"):
        urls = [u for u in (gh_ai.get("urls") or []) if u]
        if not urls:
            urls = mirror_urls("haavik_ai.pack",
                               _release_tag_of(gh_ai["url"]), gh_ai["url"])
        if urls:
            say("  AI 镜像腿按 GitHub 的 tag 拼（GitHub=%s，Gitee=%s）"
                % (_release_tag_of(gh_ai["url"]),
                   _release_tag_of(prev_ai.get("url"))))
            return urls
    keep = [u for u in (prev_ai.get("urls") or []) if u]
    if keep:
        say("  ⚠ 读不到 GitHub 清单，沿用它原有的镜像腿（不按 Gitee 的 tag 重拼）")
        return keep
    return mirror_urls("haavik_ai.pack",
                       _release_tag_of(prev_ai.get("url")), prev_ai["url"])


def _release_tag_of(url) -> str | None:
    """从附件下载地址里抠出它属于哪个发行版 tag。"""
    m = re.search(r"/releases/download/([^/]+)/", str(url or ""))
    return m.group(1) if m else None


def _manifest_release_tags(token: str) -> set:
    """**清单正指向的**发行版 tag 集合 —— 这些绝不能删。

    两个来源都要看：核心程序的 ``url`` 和 AI 组件的 ``ai.url``。两者可能指向
    不同的发行版（AI 包只在它自己迭代时才重发，所以 ai.url 往往指着更早那一版）。
    删掉其中任何一个，使用者那边就是"检查更新下载 404 / AI 模型装不上"，
    而日志里只有一句 404。
    """
    man = _remote_manifest(token)
    tags = set()
    for item in (man, man.get("ai") if isinstance(man.get("ai"), dict) else None):
        if isinstance(item, dict):
            tag = _release_tag_of(item.get("url"))
            if tag:
                tags.add(tag)
    return tags


def prune_releases(token: str, keep: int = 2) -> list:
    """删掉旧的发行版，只保留最新的 ``keep`` 条 —— 给 Gitee 的附件配额腾地方。

    **为什么需要这个**：Gitee 的仓库附件限制是**总量 1 GB**（不是单文件大小），
    每发一版都要挂一个 60~70 MB 的 Setup.exe（带 AI 组件时再加一个几十 MB 的
    大包），十几版之后配额就悄悄满了，上传直接回::

        400 {"message":"验证失败：文件大小已超出仓库附件配额：1 GB"}

    这句话完全不提"你得先删旧版本"，第一次撞上非常费解
    （2026-09-20 实测：v1.7.4 只传上去了 Setup.exe，AI 包被拒，于是清单没更新、
    使用者那边"检查更新"什么都看不到 —— 而日志里只有一句 400）。

    ⚠ 删之前必须确认**清单里指向的那条别被删掉**：使用者正拿着旧清单来下载。
    所以这里从不删 ``keep`` 之内的（默认留最新的 2 条）。
    返回被删掉的 tag 列表。
    """
    sys.path.insert(0, OPT)
    import version_check

    rels = gitee(token, "/repos/%s/%s/releases" % (GITEE_OWNER, GITEE_DIST))
    if not isinstance(rels, list):
        return []

    def ver_key(rel):
        try:
            return version_check.parse_version(str(rel.get("tag_name") or "").lstrip("v"))
        except Exception:                                # noqa: BLE001
            return (0, 0, 0)

    ordered = sorted([r for r in rels if r.get("tag_name")],
                     key=ver_key, reverse=True)
    protected = _manifest_release_tags(token)
    if protected:
        say("清单指向的发行版（一律不删）：%s" % "、".join(sorted(protected)))
    removed = []
    for rel in ordered[max(0, keep):]:
        tag, rid = rel.get("tag_name"), rel.get("id")
        if tag in protected:
            say("  保留 %s：当前清单正指向它，删了使用者就下载不到" % tag)
            continue
        try:
            gitee(token, "/repos/%s/%s/releases/%s" % (GITEE_OWNER, GITEE_DIST, rid),
                  method="DELETE")
        except urllib.error.HTTPError as exc:
            say("  删不掉 %s（HTTP %s），跳过" % (tag, exc.code))
            continue
        removed.append(tag)
        say("  已删除旧发行版 %s（把它占的附件配额还回去）" % tag)
    left = [r.get("tag_name") for r in ordered
            if r.get("tag_name") not in removed]
    say("保留的发行版：%s" % (", ".join(left) if left else "（没有）"))
    return removed


def publish_gitee(token: str, notes: str = "") -> None:
    ver = current_version()
    exe = os.path.join(ROOT, "dist", "Setup.exe")
    if not os.path.isfile(exe):
        raise SystemExit("找不到 %s —— 先跑 build.py（或加 --build）" % exe)
    sha, size = sha256_of(exe)
    say("Setup.exe %d 字节 sha256=%s…" % (size, sha[:16]))

    # AI 模型组件（独立组件，体积大，只在它迭代时才重发）。
    # 文件存在才发，并据此决定清单写成「组件形式」还是「单体形式」。
    ai_path = os.path.join(ROOT, "build_work", "ai_staging", "haavik_ai.pack")
    ai_sha = ai_size = None
    if os.path.isfile(ai_path):
        ai_sha, ai_size = sha256_of(ai_path)
        say("haavik_ai.pack %d 字节 sha256=%s…" % (ai_size, ai_sha[:16]))
    else:
        say("（没找到 %s，本次只发核心程序）" % ai_path)

    tag = "v" + ver
    rel = None
    try:
        rel = gitee(token, "/repos/%s/%s/releases" % (GITEE_OWNER, GITEE_DIST),
                    method="POST",
                    fields={"tag_name": tag, "name": "HAAVIK Photo " + ver,
                            "body": notes or ("HAAVIK Photo " + ver),
                            "target_commitish": "master"})
        say("发行版 %s 已创建 id=%s" % (tag, rel.get("id")))
    except urllib.error.HTTPError as exc:
        say("建发行版返回 %s，改为复用已存在的" % exc.code)
    if not (isinstance(rel, dict) and rel.get("id")):
        rs = gitee(token, "/repos/%s/%s/releases" % (GITEE_OWNER, GITEE_DIST))
        rel = next((r for r in rs if r.get("tag_name") == tag), None) \
            if isinstance(rs, list) else None
        if not rel:
            raise SystemExit("既建不出也找不到 %s 这个发行版，中止" % tag)
        say("复用已有发行版 id=%s" % rel["id"])

    have = [a.get("name") for a in (rel.get("assets") or [])]

    def upload_attachment(path, name):
        if name in have:
            say("这条发行版已经有 %s 了，跳过上传"
                "（要换包得先在网页上把旧附件删掉）" % name)
            return
        try:
            got = gitee_attach_exe(token, GITEE_DIST, rel["id"], path, filename=name)
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read(400).decode("utf-8", "replace")
            except Exception:                            # noqa: BLE001
                pass
            if "配额" in body:
                raise SystemExit(
                    "上传 %s 被拒：**Gitee 那个仓库的附件总配额（1 GB）满了**。\n"
                    "  历史发行版的附件会一直占着，必须先删旧的才能再发 ——\n"
                    "  这不是单文件太大，是累积占用。\n"
                    "  处理：python code_review/release.py --prune-releases 2\n"
                    "  服务器原话：%s"
                    % (name, body.strip() or "(无正文)"))
            say("上传 %s 失败：HTTP %s %s" % (name, exc.code, body.strip()[:200]))
            raise
        say("附件已上传：%s %s 字节" % (got.get("name"), got.get("size")))

    upload_attachment(exe, "Setup.exe")
    ai_url = None
    if ai_sha is not None:
        upload_attachment(ai_path, "haavik_ai.pack")
        ai_url = ("https://gitee.com/%s/%s/releases/download/%s/haavik_ai.pack"
                  % (GITEE_OWNER, GITEE_DIST, tag))

    setup_url = ("https://gitee.com/%s/%s/releases/download/%s/Setup.exe"
                 % (GITEE_OWNER, GITEE_DIST, tag))
    # --notes 为空时**沿用清单里原有的说明**，别把它清掉：
    # 补跑一次 --publish（比如上次卡在更新清单那步）时，若把 notes 覆盖成空字符串，
    # 就等于"把已经写好的发布说明悄悄抹了"。
    # 读一次远端现有清单：下面"沿用发布说明"和"沿用 ai 组件"都要用它。
    prev = _remote_manifest(token)

    note_line = (notes or "").splitlines()[0] if notes else ""
    if not note_line and str(prev.get("version")) == ver:
        note_line = str(prev.get("notes") or "")
        if note_line:
            say("沿用清单里原有的说明（没给 --notes）")

    # 单体（扁平）清单：核心程序字段顶层直接给，保证**不认识组件清单的**旧客户端
    # （如 1.6.2）也能读到并自动更新到本版本；AI 模型组件作为顶层 ``ai`` 键附带，
    # 只有新客户端（1.7.1+）读取，旧客户端忽略它。两者互不干扰。
    manifest = {"version": ver, "url": setup_url,
                "urls": mirror_urls("Setup.exe", tag, setup_url),
                "sha256": sha, "size": size,
                "notes": note_line}
    if ai_sha is not None:
        ai_ver = _ai_pack_version(ai_path) or "1.1.0"
        manifest["ai"] = {"version": ai_ver, "url": ai_url,
                          "urls": mirror_urls("haavik_ai.pack", tag, ai_url),
                          "sha256": ai_sha, "size": ai_size, "min_os": "win10"}
    else:
        # ⚠ 本次没准备新的 AI 包时，**必须把远端清单里那条 ai 原样搬过来**。
        # 不搬的后果（2026-09-21 发现）：清单里 `ai` 键直接消失 → 所有客户端
        # `ai_needs_update()` 一律返回"不需要更新" → 已经装了旧 AI 组件的人
        # **永远升不上新模型**，界面上连"有更新"都不提示，而且日志一片正常。
        # 这正是"AI 大模型只在它自己迭代时才重发"该有的样子：核心发版不动它。
        prev_ai = prev.get("ai")
        if isinstance(prev_ai, dict) and prev_ai.get("url"):
            prev_ai = dict(prev_ai)
            # 顺手把备用地址补上：这条 ai 是上一次写的，那时可能还没有 urls。
            # ⚠ **不能**按 `prev_ai["url"]` 的 tag 去拼 —— 那是 Gitee 的 tag
            #   （AI 包常单独挂 ai-x.y.z），拿它拼 GitHub 镜像地址会全 404。
            #   详见 _ai_mirror_urls_for_gitee 的注释。
            prev_ai["urls"] = _ai_mirror_urls_for_gitee(prev_ai)
            manifest["ai"] = prev_ai
            say("沿用清单里原有的 ai 组件 v%s（本次没准备新的 AI 包，"
                "它仍挂在 %s 上）"
                % (prev_ai.get("version"), _release_tag_of(prev_ai.get("url"))))
        else:
            say("⚠ 本地没有 AI 包、远端清单里也没有 ai 组件 —— 本次清单不含 AI。"
                "（想让使用者能下 AI 模型，得先把包放进 build_work/ai_staging/）")
    gitee_put_text(token, GITEE_DIST, "manifest.json",
                   json.dumps(manifest, ensure_ascii=False, indent=2,
                              sort_keys=True) + "\n",
                   "manifest for " + ver)
    gitee_put_text(token, GITEE_DIST, "version.txt", ver + "\n",
                   "version anchor for " + ver)
    say("清单与版本锚点已更新为 %s%s" % (ver, "（含 ai 组件）" if ai_sha else ""))

    # 匿名复核：不做这一步就等于"我以为发出去了"。
    say("匿名复核（不带任何令牌，模拟使用者的真实处境）：")
    raw = "https://gitee.com/%s/%s/raw/master/" % (GITEE_OWNER, GITEE_DIST)
    bad = []
    checks = [("version.txt", raw + "version.txt"),
              ("manifest.json", raw + "manifest.json"),
              ("Setup.exe 附件", setup_url)]
    if ai_url:
        checks.append(("haavik_ai.pack 附件", ai_url))
    for label, u in checks:
        try:
            st, head = anon_get(u)
            say("  %-16s 通 %s  %r" % (label, st, head[:16]))
        except Exception as exc:                         # noqa: BLE001
            say("  %-16s **取不到** %r" % (label, exc))
            bad.append(label)
    if bad:
        raise SystemExit(
            "匿名复核没过：%s —— 这时候朋友们是下载不到的，先别通知任何人，"
            "查清原因再说。" % "、".join(bad))


# --------------------------------------------------------------------------
# 2b) 发布到 GitHub（备用；本机直连不通，得显式 --publish-github）
# --------------------------------------------------------------------------
def _gitee_manifest_anon() -> dict:
    """匿名读 Gitee 上的 manifest.json（公开仓库、不需令牌）。

    ⚠ 2026-09-24 起 ``publish_github`` **不再用它来决定 ai 段**（改用
    :func:`_github_manifest` 读 GitHub 自己那份，理由见那边）。
    保留它是因为"两边的清单是不是一致"在排查时很有用 —— 想人工比对时
    直接调它就行。当前正式流程里没有调用点，**这是有意的，不是漏接线**。

    拿不到就返回空 dict，绝不抛。
    """
    try:
        url = "https://gitee.com/%s/%s/raw/master/manifest.json" % (
            GITEE_OWNER, GITEE_DIST)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read(1 << 22)
        return json.loads(data.decode("utf-8"))
    except Exception as exc:                                # noqa: BLE001
        say("（匿名读 Gitee manifest 失败：%s）" % exc)
        return {}


def _github_manifest(token: str) -> dict:
    """读 GitHub dist 仓 **main 分支**上的 manifest.json（当前线上状态）。

    为什么要单独一个：`publish_gitee` 用 ``_remote_manifest`` 读 Gitee 那份，
    而"只发 GitHub"的场合（这次就是）Gitee 那份可能落后好几版 ——
    拿它当"上一次的清单"去沿用 ai 段，就会把旧 AI 段写进新清单。
    GitHub 这份才是那种场合下的权威。

    读不到就返回空 dict，绝不抛 —— 网络抖动不该让发版在半路炸，
    但调用方看到空 dict 会走"清单不含 ai"的分支并**打印警告**，
    不会静静地把 ai 段弄丢。
    """
    try:
        out = api(token, "https://api.github.com/repos/%s/contents/manifest.json"
                  % DIST_REPO)
        data = base64.b64decode(out["content"])
        return json.loads(data.decode("utf-8"))
    except Exception as exc:                                # noqa: BLE001
        say("（读 GitHub 清单失败，沿用 ai 段这一步拿不到东西：%s）" % exc)
        return {}


def publish_github(token: str, notes: str = "") -> None:
    ver = current_version()
    exe = os.path.join(ROOT, "dist", "Setup.exe")
    if not os.path.isfile(exe):
        raise SystemExit("找不到 %s —— 先跑 build.py" % exe)

    h = hashlib.sha256()
    with open(exe, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    sha, size = h.hexdigest(), os.path.getsize(exe)
    say("Setup.exe %d 字节 sha256=%s…" % (size, sha[:16]))

    tag = "v" + ver
    try:
        rel = api(token, "https://api.github.com/repos/%s/releases" % DIST_REPO,
                  method="POST", body={
                      "tag_name": tag, "name": "HAAVIK Photo " + ver,
                      "body": (notes or "HAAVIK Photo " + ver) + "\n\nSHA256: " + sha})
        say("Release %s 已创建 id=%s" % (tag, rel["id"]))
    except urllib.error.HTTPError as exc:
        if exc.code != 422:
            raise
        rel = api(token, "https://api.github.com/repos/%s/releases/tags/%s"
                  % (DIST_REPO, tag))
        say("Release %s 已存在 id=%s，改为替换附件" % (tag, rel["id"]))

    # 删掉同名旧附件（GitHub 不允许重名）。用统一的清理函数，
    # 上传失败重试时它也会被再调一次。
    for a in list(rel.get("assets", [])):
        if a["name"] == "Setup.exe":
            api(token, "https://api.github.com/repos/%s/releases/assets/%s"
                % (DIST_REPO, a["id"]), method="DELETE")
            say("  删掉同名旧附件 Setup.exe")
    up = upload_asset(token,
                      "https://uploads.github.com/repos/%s/releases/%s/assets?name=Setup.exe"
                      % (DIST_REPO, rel["id"]), exe, timeout=3600)
    say("  附件已上传 %d 字节 state=%s" % (up["size"], up["state"]))

    # ⚠ 这里必须和 publish_gitee 一样写上 urls（2026-09-23 修）：
    # jsdelivr 缓存的就是 GitHub dist 仓的 main 分支，客户端从 jsdelivr
    # 拉到清单后**只能看到 GitHub 这一个源**就是缺这条的副作用 —— 多源、
    # 测速换源、健康检查全失灵。所以这里也用 :func:`mirror_urls` 拼一份。
    setup_url = "https://github.com/%s/releases/download/%s/Setup.exe" % (DIST_REPO, tag)

    # ---- AI 组件：本地有包就**发本地的**，没有才去搬 Gitee 那份 ----
    #
    # 为什么不是"一律搬 Gitee"（2026-09-24 修）：
    #   只发 GitHub（`--publish-github` 而不跑 `--publish`）时，Gitee 那份清单
    #   还是**上一版**的（比如 ai 仍是 v1.2.0）—— 照搬它就把**旧 AI 段写进了
    #   新清单**：使用者下到的新版程序一直提示"AI 已是最新"，新模型永远发不出去。
    #   这正是本项目最怕的那种失败：**不报错、只是悄悄发错东西。**
    #   所以本地 `build_work/ai_staging/haavik_ai.pack` 存在时就以它为准，
    #   真把它传到 GitHub Release 上，再用它的 sha256/size/version 写清单。
    ai_section = None
    ai_path = os.path.join(ROOT, "build_work", "ai_staging", "haavik_ai.pack")
    if os.path.isfile(ai_path):
        ai_sha, ai_size = sha256_of(ai_path)
        ai_ver = _ai_pack_version(ai_path) or "1.1.0"
        say("本地有 AI 包 v%s %d 字节 sha256=%s…，本次发到 GitHub"
            % (ai_ver, ai_size, ai_sha[:16]))
    # 删掉同名旧 AI 包（GitHub 不允许重名）
        for a in list(rel.get("assets", [])):
            if a["name"] == "haavik_ai.pack":
                api(token, "https://api.github.com/repos/%s/releases/assets/%s"
                    % (DIST_REPO, a["id"]), method="DELETE")
                say("  删掉同名旧 AI 包")
        up_ai = upload_asset(
            token,
            "https://uploads.github.com/repos/%s/releases/%s/assets"
            "?name=haavik_ai.pack" % (DIST_REPO, rel["id"]), ai_path, timeout=3600)
        say("  AI 包已上传 %d 字节 state=%s" % (up_ai["size"], up_ai["state"]))
        ai_url = ("https://github.com/%s/releases/download/%s/haavik_ai.pack"
                  % (DIST_REPO, tag))
        ai_section = {
            "version": ai_ver,
            "url": ai_url,
            "urls": mirror_urls("haavik_ai.pack", tag, ai_url),
            "sha256": ai_sha, "size": ai_size, "min_os": "win10",
        }
    else:
        # 本地没包：这次不动 AI 组件。那就必须去**别处**找一份现成的 ai 段
        # 原样搬过来（否则清单里 `ai` 键直接消失 → 已装 AI 的人永远升不上）。
        # ⚠ 注意这里读的是 **GitHub 自己那份**清单（上一次发的），而不是 Gitee：
        #   只发 GitHub 的场合，GitHub 这份才是"当前线上状态"的权威。
        #   Gitee 那份可能落后好几版（本次就是：只发 GitHub，Gitee 还是 1.8.1）。
        cur_man = _github_manifest(token)
        prev_ai = cur_man.get("ai") if isinstance(cur_man, dict) else None
        if isinstance(prev_ai, dict) and prev_ai.get("url"):
            ai_section = dict(prev_ai)
            ai_section["urls"] = mirror_urls("haavik_ai.pack",
                                            _release_tag_of(prev_ai.get("url")),
                                            prev_ai["url"])
            say("本地没有 AI 包 → 沿用 GitHub 清单里的 ai 组件 v%s（挂在 %s）"
                % (prev_ai.get("version"),
                   _release_tag_of(prev_ai.get("url"))))
        else:
            say("⚠ 本地没有 AI 包、GitHub 清单里也没有 ai 组件 —— "
                "本次清单不含 AI（已装 AI 的人不会被告知更新）。")

    manifest = {
        "version": ver,
        "url": setup_url,
        "urls": mirror_urls("Setup.exe", tag, setup_url),
        "sha256": sha, "size": size,
        "notes": notes.splitlines()[0] if notes else "",
    }
    if ai_section:
        manifest["ai"] = ai_section
    mtext = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    put_content(token, DIST_REPO, "manifest.json", mtext, "manifest for " + ver)
    put_content(token, DIST_REPO, "version.txt", ver + "\n", "version anchor for " + ver)
    say("清单与版本锚点已更新为 %s" % ver)


# --------------------------------------------------------------------------
# 3) 私有仓提交
# --------------------------------------------------------------------------
def git_commit(token: str, message: str, push: bool = True) -> None:
    ver = current_version()
    env = dict(os.environ)
    env["GIT_EXEC_PATH"] = GIT_EXEC_PATH

    def run(args, **kw):
        p = subprocess.run([GIT] + args, cwd=ROOT, text=True,
                           capture_output=True, env=env, **kw)
        if p.returncode != 0:
            say("  git %s -> rc=%d\n%s" % (" ".join(args[:2]), p.returncode,
                                           (p.stderr or "").strip()[:300]))
        return p

    run(["add", "-A"])
    msg_file = os.path.join(HERE, "_commit_msg.txt")
    open(msg_file, "w", encoding="utf-8").write(message + "\n")
    run(["commit", "-F", msg_file])
    os.remove(msg_file)
    run(["tag", ver if ver.startswith("v") else "v" + ver])
    if not push:
        say("已提交并打 tag（未推送）")
        return
    # 推送：临时把 token 放进 remote URL，推完立刻还原
    clean = "https://github.com/%s.git" % PRIVATE_REPO
    run(["remote", "set-url", "origin",
         "https://%s@github.com/%s.git" % (token, PRIVATE_REPO)])
    rc = run(["push", "origin", "main"]).returncode
    rc |= run(["push", "origin", "--tags"]).returncode
    run(["remote", "set-url", "origin", clean])
    cfg = open(os.path.join(ROOT, ".git", "config"), encoding="utf-8").read()
    say("remote 已还原，配置里没有令牌：%s" % (not re.search(r"ghp_|github_pat_", cfg)))
    if rc != 0:
        # 推送失败**不能只说"完成"** —— 本地提交和 tag 都在，但远端没更新，
        # 这种"看起来成功"最耽误事（本机网络对 github 的推送时不时会中断）。
        raise SystemExit(
            "推送没成功（多半是网络中断）。提交与 tag 都已在本地，"
            "网络恢复后重跑：\n"
            "  git push origin main\n"
            "  git push origin --tags\n"
            "（公开分发仓库那边的 Release 与清单已经在 --publish 那一步完成，"
            "不受这里影响。）")
    say("推送完成")


def run_step(name: str, label: str) -> None:
    """跑一条流水线步骤（重建 / 审计 / 复核），失败就中止 ——
    发版最怕的不是失败，是"失败了还继续往下发"。"""
    path = os.path.join(OPT if name == "build.py" else HERE, name)
    say("[%s] %s" % (label, os.path.relpath(path, ROOT)))
    p = subprocess.run([sys.executable, path], cwd=os.path.dirname(path),
                       text=True, capture_output=True)
    for line in (p.stdout or "").strip().splitlines()[-3:]:
        say("    " + line)
    if p.returncode != 0:
        for line in (p.stderr or "").strip().splitlines()[-6:]:
            say("    " + line)
        raise SystemExit("%s 失败（退出码 %d）—— 停在这里，不往下走"
                         % (label, p.returncode))


def run_smoke() -> None:
    """发布前**真的把打好的 exe 启动一次**（--selfcheck，不开窗）。

    这一条是 v1.6.0 事故的直接后果：build.py 的 EXCLUDES 排除了 smtplib，
    而新代码 import 了它 —— 审计、签名、载荷哈希、安装链路**全部通过**，
    只有真正启动 exe 才会暴露。本地跑源码永远测不出来（本地什么模块都有）。

    ⚠️ 为什么不开 GUI 而是跑 --selfcheck：windowed exe 崩了会弹一个框、然后
    卡在那里等用户点确定 —— 进程还活着，脚本看不出它坏了。--selfcheck
    全程不开窗，只用退出码说话，才有资格当闸门。
    """
    say("[启动自检] 真的把 exe 跑一遍（不是静态检查）")
    report = os.path.join(os.environ.get("TEMP", "."), "haavik_selfcheck.log")
    for name in ("MyAlbum.exe", "MyAlbum_x86.exe"):
        exe = os.path.join(ROOT, "dist", name)
        if not os.path.isfile(exe):
            raise SystemExit("找不到 %s —— 先构建。停在这里，不往下走" % exe)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("_PYI") and not k.startswith("_MEIPASS")}
        try:
            p = subprocess.run([exe, "--selfcheck"], cwd=os.path.dirname(exe),
                               env=env, timeout=180,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except subprocess.TimeoutExpired:
            raise SystemExit("%s 自检超时（180 秒）—— 不正常，停在这里" % name)
        if p.returncode != 0:
            say("    %s 退出码 %d" % (name, p.returncode))
            if os.path.isfile(report):
                for line in open(report, encoding="utf-8").read().strip().splitlines()[:8]:
                    say("    " + line)
            raise SystemExit("%s 启动自检失败 —— 这个包发出去就是「装完打不开」，"
                             "停在这里，不往下走" % name)
        say("    %s 通过 ✓" % name)


def main() -> int:
    ap = argparse.ArgumentParser(description="HAAVIK Photo 发版工具（默认发 Gitee）")
    ap.add_argument("--set-version", metavar="X.Y.Z", help="改四处版本号")
    ap.add_argument("--build", action="store_true", help="重建（build.py）")
    ap.add_argument("--audit", action="store_true", help="审计产物（verify_payload.py，必须 0）")
    ap.add_argument("--publish", action="store_true",
                    help="发到 Gitee 分发仓：发行版 + 附件 + 清单 + 匿名复核")
    ap.add_argument("--publish-github", action="store_true",
                    help="发到 GitHub 公开仓（备用；本机直连不通）")
    ap.add_argument("--verify", action="store_true",
                    help="发版后复核：模拟老客户端真去下载一遍并核对 sha256")
    ap.add_argument("--commit", action="store_true", help="源码仓提交并推送（含 tag）")
    ap.add_argument("--no-push", action="store_true", help="只提交，不推送")
    ap.add_argument("--message", "-m", default="", help="提交信息")
    ap.add_argument("--notes", default="", help="发行版说明（第一行写进清单）")
    ap.add_argument("--show", action="store_true", help="只看当前版本号")
    ap.add_argument("--prune-releases", metavar="KEEP", type=int, default=None,
                    help="删掉旧发行版、只留最新的 KEEP 条（给 Gitee 附件配额腾地方）")
    args = ap.parse_args()

    actions = (args.set_version or args.build or args.audit or args.publish
               or args.publish_github or args.verify or args.commit
               or args.prune_releases is not None)
    if args.show or not actions:
        say("当前版本：%s" % current_version())
        if not actions:
            say("（没有指定动作。发版的完整顺序：")
            say("   --set-version X.Y.Z --build --audit --publish --verify --commit ）")
            say("（上传附件报「超出仓库附件配额」时：")
            say("   --prune-releases 2  只留最新两版，然后再 --publish ）")
        return 0

    done = []

    # 配额清理放在最前面：撞上配额时先腾地方，后面的 --publish 才发得出去。
    if args.prune_releases is not None:
        say("[清理] 旧发行版（保留最新 %d 条）" % args.prune_releases)
        prune_releases(get_gitee_token(), max(1, args.prune_releases))
        done.append("清理旧发行版")

    if args.set_version:
        set_version(args.set_version)
        done.append("版本号")

    if args.build:
        run_step("build.py", "重建")
        done.append("重建")

    if args.audit:
        run_step("verify_payload.py", "审计")
        done.append("审计")

    # 静态审计过不了"一启动就崩"这一类问题 —— 必须真跑一次。
    # 紧跟着 --build 发的包才需要这道闸门；预构建好的包直接用 --publish 发，
    # 那时 build.py 已经过了自检，不需要（也没有）再跑一遍。
    if args.build:
        run_smoke()
        done.append("启动自检")

    if args.publish:
        publish_gitee(get_gitee_token(), args.notes)
        done.append("发布(Gitee)")

    if args.publish_github:
        publish_github(get_token(), args.notes)
        done.append("发布(GitHub)")

    if args.verify:
        run_step("_verify_release.py", "发版后复核")
        done.append("复核")

    if args.commit:
        # ⚠ 只用 `--no-push` 时**不该逼人要令牌**：本地提交与打 tag 根本不需要凭据。
        # 2026-09-21 实测：本机没设 GITHUB_TOKEN，于是 `--commit --no-push` 连本地
        # 提交都做不成（`get_token()` 先抛 SystemExit）—— 很反直觉：
        # "只提交、不推送"偏偏卡在推送凭据上。
        git_commit(get_token() if not args.no_push else "",
                   args.message or ("release %s" % current_version()),
                   push=not args.no_push)
        done.append("提交源码")

    say("完成：%s" % "、".join(done))
    if args.publish and not args.verify:
        say("⚠ 还没跑发版后复核。建议补一句："
            "python code_review/release.py --verify")
    say("下一步由使用者自己做：打开程序点右下角「检查更新」，或去 Gitee 下载。")
    say("（桌面不放任何文件 —— 装哪一版、什么时候装，是他自己的事。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
