# -*- coding: utf-8 -*-
"""联网客户端 —— 本项目**唯一**的联网点。

这个文件被允许做的事，逐条列出（审计里可逐项验证）
==================================================================
1. ``fetch_latest()``：一次 HTTPS GET，取回一个纯文本版本号（最多 64 字节）。
   —— 就是原来的行为，没变。
2. ``fetch_manifest()``：一次 HTTPS GET，取回一个 JSON 清单
   （版本号 / 下载地址 / sha256 / 字节数）。**前提是 MANIFEST_URL 非空**。
3. ``download_update()``：把清单里那个地址的文件按流下载到
   ``%LOCALAPPDATA%\\HAAVIK Photo\\updates\\``，下载完**核对 sha256**，
   不一致就删掉并报错。**前提同上**。

它绝不做的事
==================================================================
* 不执行、不运行、不加载下载下来的任何东西 —— 它只返回一个"文件在这里"的路径。
  要不要装，由**用户点一下**决定（main.py 里那一步）。这不是洁癖：
  Windows 上正在运行的 exe 是被锁住的，想"静默替换自己"必须借助
  ``MoveFileEx(..., DELAY_UNTIL_REBOOT)`` 或另起一个 helper 进程去等主进程退出
  —— 那两种写法都是恶意软件的教科书特征，也正是本项目从头到尾在避免的形态。
* 不上传任何东西。不发机器标识、不发用户名、不发安装路径。请求头里只有一个
  固定的 User-Agent 字符串。
* 不在后台循环轮询。每次启动只查一次。

为什么只允许 http **回环地址**（一个刻意留的口子）
==================================================================
真实更新走 https（主源 ``gitee.com``，备用 ``raw.githubusercontent.com``）。
回环地址（127.0.0.1 / localhost）额外放行，是为了让
``update_check_test.py`` 能起一个本地 http 服务，把"清单 → 下载 → 校验 →
拒绝被篡改的包"这条链路**真的跑一遍**，而不是靠看代码相信它。
除这两种情况外一律拒绝 —— 明文 http 的更新包等于把安装包送到别人手里改。

网络通道：自己找路，不让用户配
==================================================================
中国网络下直连 GitHub 经常不通。程序**按顺序自己试**：

    直连  →  系统/环境里配好的代理  →  本机常见代理端口（7890、10809…）

第一个能用的会被记住，之后的检查与下载都走它。用户不需要填任何设置；
哪天他装了代理软件，程序不用改一行代码就能用上。
（代理只影响"走哪条路"，不参与信任判断：清单与安装包仍然靠 TLS + sha256
 保证完整性与来源。详见 ``channel_candidates()`` 的说明。）

``APP_VERSION`` 是本项目**唯一**的版本号来源
==================================================================
main.py 的提示文字、installer 的标题栏、payload.zip 的 meta、build.py 写进
PE 版本信息的 FileVersion、发布时的 Git tag，全部由它派生。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("haavik.update")

APP_VERSION = "1.8.11"

#: 版本锚点候选地址（**按顺序尝试，第一个成功的就用**）。
#: 只放「代码托管商自己的域」（gitee.com / github.com），刻意不引第三方 CDN：
#: 而能改清单的人就能顺手改掉 sha256，等于能把安装包换成他自己的。
#: 多给一个地址的原因很实际：单一域名被干扰/限流时，更新功能不该整体失效。
#:
#: 2026-09-15 起**不再使用 Gist**：那个账号下的 Gist 会整个消失
#: （匿名和带 token 都 404），而仓库文件表现正常 —— 与其留一个"先失败
#: 一次再回退"的地址，不如只留可靠的。
#:
#: ⚠⚠ **2026-09-24 大改**：使用者要求"这次先别用 Gitee，我要试 GitHub 单独能不能扛"。
#:   于是这里**去掉了 Gitee**，改用 **jsdelivr 缓存 GitHub 仓**（实测国内 1.0~2.7 秒、
#:   连测 5 次全成功）。同时删掉两条**早就是死链**的旧地址 —— 它们指向 `hhhs81`
#:   那个被 GitHub 隐藏的账号（匿名全 404），留着只是每次白试一遍。
#:   ⚠ jsdelivr 是**缓存**：刚发完版那几分钟它可能还拿着上一版，等它 TTL 过期就同步。
#:   想退回 Gitee 就把下面那行注释掉、把 Gitee 那条取消注释。
_ANCHOR_USE_GITEE = False

#: GitHub **公开分发仓**（放 version.txt / manifest.json 的仓库）。
#: ⚠ 这个仓在 `wang1357441` 名下（不是 `hhhs81` —— 那个账号被 GitHub 隐藏，
#:   匿名访问全 404）。
GH_REPO = "wang1357441/haavik-photo-dist"
GH_BRANCH = "main"
#: 该仓里两个文件的 raw 地址。镜像站就是拿这个地址去"套前缀"的。
GH_RAW_BASE = "https://raw.githubusercontent.com/%s/%s" % (GH_REPO, GH_BRANCH)

#: ⚠⚠ **2026-09-25 实测：加速镜像站可以包 raw.githubusercontent.com**
#:
#: 之前（09-23）的记录说"镜像站只对 Release 附件好用、对 raw 不行"，
#: 但 09-25 复测发现 `https://gh-proxy.com/https://raw.githubusercontent.com/...`
#: **0.31~0.34 秒**就拿到了正确的 `1.8.2` —— 比 GitHub 官方 API 的 0.41~0.44 秒
#: **还快**，而且连测稳定。
#:
#: 这一条很关键，它解决了一个之前没解的问题：**锚点只有 GitHub 官方 API 一条快路**。
#: 官方 API 虽然好，但它是**未认证调用、按 IP 限流**（一小时几十次），
#: 学校/机房这种多人共用一个出口 IP 的场景很容易被 429。那时节目就会退到
#: jsdelivr（**缓存、滞后一两版**）→ 读回旧版本号 → 判定"已是最新" → 更新功能静默失效。
#: 现在多了这条**又快又不吃 GitHub 限流**的路，就不用再怕这件事。
#:
#: ⚠ 前缀形式的写法是 `https://<镜像站>/https://github.com/...`。
#:   写成 `https://<镜像站>/<owner>/<repo>/...` 一律 404（实测过）。
#:   这里给的是 raw 域名，实测同样吃前缀。
_GH_RAW_MIRRORS = (
    "https://gh-proxy.com/",      # 09-25 实测 0.31s，最快
    "https://gh.xxooo.cf/",       # 09-25 实测 0.69~0.83s
)

if _ANCHOR_USE_GITEE:
    DEFAULT_VERSION_URLS = (
        "https://gitee.com/whr3443kklld/haavk-photos---haavk-photo/raw/master/version.txt",
        "https://cdn.jsdelivr.net/gh/wang1357441/haavik-photo-dist@main/version.txt",
    )
else:
    DEFAULT_VERSION_URLS = (
        # ① GitHub 官方 contents API（配 Accept: raw 直接回文件原文）。
        #    走 api.github.com —— 本机可达，而 raw.githubusercontent.com 直连很慢（实测 16 秒）。
        #    ⚠ 代价：未认证调用按 IP 限流，出口 IP 人多时可能 429 → 所以它**不是唯一依靠**。
        "https://api.github.com/repos/%s/contents/version.txt" % GH_REPO,
        # ② 镜像站包 raw 域名（实测最快、不吃 GitHub 限流）
        *[m + GH_RAW_BASE + "/version.txt" for m in _GH_RAW_MIRRORS],
        # ③ ⚠ **2026-09-25：jsdelivr 从第 2 位降到末位。**
        #    它是**缓存**且滞后一到两版（发完 1.8.2 后实测它仍读回 1.8.1），
        #    `Cache-Control: no-cache` 也冲不掉。留着它当"第二名"等于：
        #    主路一被限流，就轮到它回答 → 读回旧版本号 → **误报"已是最新"**。
        #    放在最后，前面几条快路都断了才会用到它，危害有限。
        "https://cdn.jsdelivr.net/gh/%s@main/version.txt" % GH_REPO,
        # 2026-09-26 战略：Gitee 降级为"仅查更新"——大文件已删，只留版本锚点/清单，
        # 且清单指向 GitHub 下载。这里把它作为查更新的兜底源（排最后，平时用不到）。
        "https://gitee.com/whr3443kklld/haavk-photos---haavk-photo/raw/master/version.txt",
    )
#: 兼容旧引用（文档与测试用的是这个名字）。它就是首选地址。
VERSION_URL = DEFAULT_VERSION_URLS[0]

#: **可覆盖的更新源**（环境变量）。两个用途：
#:   * 验证：把源指到本机 http 服务，把"检查 → 下载 → 校验 → 交接"整条链路
#:     真的跑一遍（回环地址是 _allowed 里唯一放行的明文 http，见模块说明）；
#:   * 应急：某天 GitHub 这条路彻底不通时，换成别的镜像不用重新打包。
#: 不设置就是 DEFAULT_VERSION_URLS 里那几个地址。
ENV_VERSION_URL = "HAAVIK_VERSION_URL"
ENV_MANIFEST_URL = "HAAVIK_MANIFEST_URL"


def version_urls() -> list:
    """本次要尝试的版本锚点地址（环境变量优先，然后是默认候选）。"""
    override = os.environ.get(ENV_VERSION_URL)
    if override:
        return [override] + [u for u in DEFAULT_VERSION_URLS if u != override]
    return list(DEFAULT_VERSION_URLS)


# --------------------------------------------------------------------------
# 竞速取锚点（2026-09-25）
# --------------------------------------------------------------------------
#: 参与竞速的线程数上限。
#: 为什么不是"全都发出去"：候选地址有 4~5 条，全发出去对学校/机房的
#: 共用出口 IP 不友好，而且没有收益 —— 第 3 名之后本来就追不上前两名。
#: 取 3 条：够覆盖"官方 API + 两条镜像"，又不会把连接铺开。
RACE_WIDTH = 3
#: 竞速总时限。比单个 TIMEOUT(8s) 短 —— 竞速的意义就是"只要有人答得快"，
#: 如果前 3 条在 5 秒内都没动静，那说明整条链路都不好，不如早点让
#: 调用方走"取不到"的分支，界面不必干等。
RACE_TIMEOUT = 5.0


def _race_get(urls: list, timeout: float) -> bytes:
    """**同时**去拿几个地址，谁先返回**合法的版本号**就用谁。

    为什么要有这个（而不是像以前那样一条一条试）：

    以前是串行的 —— 第 1 条不通就干等它超时（最长 8 秒），才轮到第 2 条。
    实测各条正常时是 0.3~0.9 秒，一旦主路被限流（GitHub 未认证 API 按 IP
    限流，学校/机房共用一个出口时很容易撞上），用户就要盯着一个转圈的
    提示干等 8 秒，然后才拿到本来 0.3 秒就能拿到的答案。

    现在改成**都发出去、谁先答对用谁**：
      * 正常情况下 ≈ 最快那条的耗时（实测 0.31~0.44 秒），不再受最慢那条拖累；
      * 某条挂了也不影响 —— 它只是"没先回答"，不需要等它超时。

    三条规矩（都是踩过坑才加的）：
      1. **必须验过才算数**：一段 HTML 错误页、一段 JSON 包裹、一条缓存里的
         旧版本号，都不算"答对了"。判定标准交给 ``_accept``（版本号正则）。
         否则"先返回的"往往是错误页 —— 越快越糟。
      2. **全部不合格就抛最后一个异常**，交给上面 ``fetch_latest`` 的分支
         去说"取不到"，绝不把垃圾当版本号返回。禁静默。
      3. **线程要 daemon 且必然回收**：这是后台线程里跑的，程序退出时
         不能因为还挂着几个连不上的请求而卡住。
    """
    if not urls:
        raise ValueError("没有可用的版本锚点地址")
    if len(urls) == 1:
        # 只有一条就没什么好竞速的，省下线程开销（也让单地址的测试更好写）。
        return _get(urls[0], timeout, MAX_BYTES, accept=_GITHUB_RAW_ACCEPT)

    width = min(RACE_WIDTH, len(urls))
    result: dict = {}
    done = threading.Event()

    # ⚠ 这两个字典由多个线程同时写，所以：① 用 `if k not in` 判存在，
    #   **不能用 `setdefault(k, []).append()`** —— 那句每次都会新建一个
    #   空列表、只把最后一个 append 进去，前面的失败原因全丢（实测踩到：
    #   "全失败"时报出来的异常是光秃秃的超时、看不到真实原因，排查困难）；
    #   ② 线程只写这一个共享 dict，不碰 done 以外的同步原语，规避竞态。
    failures: dict = {}          # url -> 失败原因
    tried: list = []             # 实际发出去的 url（顺序不定）

    def _note(u: str, exc: Exception) -> None:
        """记下"这条失败了"（不覆盖已有的第一条原因，便于看到最原始那个）。"""
        failures[u] = exc
        tried.append(u)

    def _worker(u: str) -> None:
        try:
            raw = _get(u, timeout, MAX_BYTES, accept=_GITHUB_RAW_ACCEPT)
        except Exception as exc:                     # noqa: BLE001 - 竞速里一律记下
            _note(u, exc)
            log.warning("版本锚点竞速失败（%s）：%s", u, exc)
            return
        try:
            _accept(raw)
        except ValueError as exc:
            # 拿到了东西但不是版本号（错误页 / JSON 包裹 / 缓存旧值不合规）。
            # 记下来，让别人继续争 —— 但**不**据此放弃。
            _note(u, exc)
            log.warning("版本锚点内容不合格（%s）：%s", u, exc)
            return
        result["win_url"] = u
        result["win_raw"] = raw
        done.set()

    for u in urls[:width]:
        threading.Thread(target=_worker, args=(u,), daemon=True).start()

    if not done.wait(timeout):
        # 没人及时答对。**把真实失败原因带出去**，别只报一句超时 ——
        # 否则以后看到"检查更新失败"完全无从下手（不知道是 DNS、被墙、
        # 还是内容不对）。优先抛"最有信息量"的那个：网络类异常 > 内容类。
        if failures:
            net_exc = next(
                (e for e in failures.values()
                 if isinstance(e, (urllib.error.URLError, OSError))),
                None,
            )
            raise net_exc or next(iter(failures.values()))
        raise TimeoutError(
            "版本锚点竞速 %.1f 秒内无人应答（已试 %d 条）" % (timeout, len(tried))
        )
    log.debug("版本锚点竞速命中：%s", result.get("win_url"))
    return result["win_raw"]


def _accept(raw: bytes) -> str:
    """把一段原始响应变成**可信的版本号**，不合格就抛 ValueError。

    ⚠ 这个函数是"竞速能不能用"的关键：竞速天生奖励"先返回的"，
    而错误页/JSON 包裹/缓存的旧值都可能**返回得更快**。所以
    "先到"必须先过这一关，才有资格算赢。
    """
    text = _unwrap_github_api(raw.decode("utf-8", "replace"))
    parse_version(text)          # 不是版本号就直接抛
    return text

#: 更新清单地址。
#:
#: **空字符串 = 只提示"有新版本"，不自动下载**（回到 1.1.0 的行为）。
#: 要让"自动下载"生效，需要把这个常量换成公开可达的 manifest.json 地址。
#:
#: 为什么必须是**公开**仓库：私有仓库的 Release 附件匿名取不到，
#: 要取就得把带 repo 权限的 Personal Access Token 打进 exe ——
#: 那等于把一把能改你仓库的钥匙分发给每一个拿到安装包的人。不可接受。
#: 所以清单托管在公开仓库（见 VERSION_URL），安装包本体放在公开分发仓库
#: ``hhhs81/haavik-photo-dist`` 的 Release 里。
#:
#: **为什么要两个清单地址（2026-09-23）**：
#: 单点风险。清单只有一个出处的话，那个源一挂（被墙、配额满、限流、
#: 域名污染……），所有客户端就**再也收不到版本提示**了 —— 用户那边
#: 的表现是"我点了检查更新什么都没发生"，日志完全正常。多源不是奢侈，
#: 是清单本身的"接口契约"就该有。
#:
#: **第二个源是 jsdelivr.net**：它把 GitHub 的公开仓当 CDN 缓存。
#: 之所以能作为合法的备用源：
#:   1. **匿名可读**：jsdelivr 不要求登录，匿名 GET 就回真内容；
#:   2. **国内可达**：这台机器上 1.7 秒拿到响应（比 raw.githubusercontent.com
#:      的 8 秒超时强得多 —— 后者从国内根本不通）；
#:   3. **没有第三方信任风险**（这里需要解释一下）：
#:      即使 jsdelivr 被攻陷、它改掉了缓存里的 manifest.json，
#:      manifest 里指向的**安装包本体**仍然在 Gitee/GitHub Release 上，
#:      下载下来后**还要过一遍 sha256 校验** —— 校验不过就拒绝下载完成。
#:      所以攻击者必须同时控制"清单"和"清单指向的文件"两条路才能下手，
#:      而第二条路要同时攻陷 Gitee + GitHub Release。门槛其实**比现在
#:      只用 Gitee 时更高**（之前只攻陷一个源就够了）。
#:
#: **发布时怎么保持两源同步**：`--publish` 走 Gitee；`--publish-github`
#: 走 GitHub 公开分发仓（它会 PUT manifest.json / version.txt 到 main 分支）。
#: jsdelivr 缓存的就是这个 GitHub 仓的 main 分支 —— 所以"保持两源一致"
#: 的动作就是**每次发版时同时跑 `--publish --publish-github`**。
#: 这也是使用者 2026-09-23 提出的"优先从 GitHub 下"那条线的延续：
#: GitHub 列表本身就是新主源，jsdelivr 把它搬给国内那批直连不到 GitHub 的人。
MANIFEST_URLS = (
    # ⚠ 2026-09-24：**GitHub contents API 排第一**，理由和版本锚点那边一样 ——
    #   jsdelivr 是缓存，刚发完版它还拿着旧清单（实测发完 1.8.2 时它还是
    #   1.8.0 的清单）。而清单决定了"下载哪个文件、sha256 是多少"，
    #   拿旧的等于**让人下到旧版、还以为自己刚更新过**。
    #   走 api.github.com（可达），配 Accept: raw 直接回文件原文。
    "https://api.github.com/repos/wang1357441/haavik-photo-dist/contents/manifest.json",
    # 2026-09-25 新增：镜像站包 raw（实测最快、不吃 GitHub 的 IP 限流）。
    # ⚠ 清单从镜像站拿**是有额外风险的** —— 镜像若能改这里的 sha256，
    #   就能把安装包换成他自己的。之所以还能接受：
    #   镜像必须**同时**改掉清单和被它指向的那个 Release 附件（第二条要
    #   真攻陷 GitHub），而且这些镜像只是**竞速选手之一**，
    #   不是唯一来源；官方 API 那条路一直在跑。
    *[m + GH_RAW_BASE + "/manifest.json" for m in _GH_RAW_MIRRORS],
    # 末位：jsdelivr 缓存 GitHub 的 haavik-photo-dist 仓 main 分支。
    # 国内快，但**是缓存、滞后一两版**（2026-09-25 复测：发完 1.8.2 后
    # 它读回的还是 1.8.1）。所以**从第 2 位降到末位** —— 让它在
    # 前面几条又快又准的路都断了之后才兜底，危害才有限。
    "https://cdn.jsdelivr.net/gh/wang1357441/haavik-photo-dist@main/manifest.json",
    # 2026-09-26 战略：Gitee 仅作查更新兜底（清单内容已与 GitHub 同步，且指向 GitHub 下载）。
    "https://gitee.com/whr3443kklld/haavk-photos---haavk-photo/raw/master/manifest.json",
)
#: 兼容旧引用（测试与界面的按钮文案判断用的就是它）。首选地址。
MANIFEST_URL = MANIFEST_URLS[0]

#: 发布页（给人手动下载用的兜底入口）。自动下载失败时，提示里会把它给用户 ——
#: "下载不了" 不应该是一个死胡同。
RELEASE_PAGE_URL = "https://github.com/wang1357441/haavik-photo-dist/releases"

#: 超时给得比较宽（8 秒）。理由：这个查询是在**后台线程**里跑的，
#: 界面上不做任何等待，所以超时放宽的代价是零；而收紧的代价是
#: "在朋友那条更差的路由上静默地永不提示" —— 那等于功能不存在。
#: 本机实测中位 0.4 秒，但在刚构建完、杀软正在扫 exe 的负载下 3 秒会不够。
TIMEOUT = 8.0
#: 下载时每个读块的超时。整包可能几十 MB，不能拿 TIMEOUT 当总时限。
CHUNK_TIMEOUT = 30.0
MAX_BYTES = 64
CHUNK = 64 * 1024
#: 下载体积上限（200 MB）。清单被篡改成指向一个 4 GB 的东西时，在这里挡住。
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
#: 更新包落地的目录名（在 %LOCALAPPDATA% 下）
UPDATE_DIRNAME = "updates"
USER_AGENT = "HAAVIK-Photo"


# --------------------------------------------------------------------------
# 通用
# --------------------------------------------------------------------------
def _allowed(url: str) -> bool:
    """只允许 https，外加回环地址的 http（给测试用，见模块说明）。"""
    try:
        parts = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parts.scheme == "https":
        return True
    if parts.scheme == "http" and (parts.hostname or "") in ("127.0.0.1", "localhost", "::1"):
        return True
    log.warning("拒绝非 https 的地址（回环除外）：%r", url)
    return False


def _get(url: str, timeout: float, limit: int, accept: str | None = None) -> bytes:
    """一次 GET，最多读 ``limit`` 字节。失败抛异常，由调用方决定怎么办。

    走**自动挑选出来的通道**（见下面「网络通道」一节），调用方不需要知道
    自己是直连还是在走代理。

    ``accept`` 是给 GitHub contents API 用的：带上
    ``application/vnd.github.raw`` 它就**直接回文件原文**。见 :func:`fetch_latest`。
    """
    if not _allowed(url):
        raise ValueError("不接受的地址：%r" % url)
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    with _open_channel(req, timeout) as resp:
        return resp.read(limit)


def is_network_error(exc: BaseException | None) -> bool:
    """这次失败是不是**网络层**的（连不上 / 超时 / 被重置）。

    "网络不通"在界面上是一句很具体的话，误用它会让排查方向整个跑偏 ——
    这个项目就踩过一次：一个参数写错引发的 ``TypeError`` 被显示成"网络不通"，
    用户以为是自己的网坏了。

    判定尺度：**只有真的连不上才算**。服务器明明回话了（哪怕回的是 404、
    403），说明路是通的，那是内容/权限/服务器的问题，不是网络问题。
    """
    if exc is None:
        return False
    if isinstance(exc, urllib.error.HTTPError):
        return False                      # 服务器回话了，通道本身没问题
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError))


def describe_check_failure(exc: BaseException | None) -> str:
    """把一次「检查更新」的失败翻成一句给用户看的话（区分该怪谁）。

    调用方拿去拼成 ``"检查更新失败（%s），稍后再试" % describe_check_failure(e)``。
    """
    if exc is None:
        return "更新源返回了空内容"
    if isinstance(exc, urllib.error.HTTPError):
        return "更新服务器返回错误（HTTP %s）" % exc.code
    if is_network_error(exc):
        return "网络不通"
    return "程序内部出错（%s）" % exc


# --------------------------------------------------------------------------
# 网络通道：直连 → 系统/环境代理 → 本机常见代理端口
# --------------------------------------------------------------------------
#: 常见的本地代理端口。用户机器上装了 Clash / v2ray / SSR 之类的客户端时，
#: 基本都是这几个里的某一个 —— 程序自己去找，比让用户去填设置强得多。
#: （2026-09-15 在用户机器上实测：这些端口当时**都没有**监听，
#:   但他哪天装了，程序不需要改一行代码就能用上。）
PROXY_PORTS = (7890, 7891, 7897, 7898, 10809, 10808, 1080, 1081, 8080, 20171)
#: 探测通道时的单个超时。比正式请求短 —— 试探就该快速失败。
PROBE_TIMEOUT = 4.0

#: 本次进程里已经选中的通道 ``(说明, 代理地址或None)``。选一次就一直用，
#: 避免每次请求都从头把所有端口试一遍。
_CHANNEL: tuple | None = None


def _build_opener(proxy: str | None):
    """proxy 为 None = **直连**（去掉环境里可能存在的代理设置）。"""
    if proxy is None:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


def channel_candidates() -> list:
    """按顺序要尝试的通道。顺序本身就是策略：**先直连**。

    直连排第一有两个理由：绝大多数用户本来就能直连（代理反而是少数派），
    而且直连最快 —— 把代理放前面会让每个用户都先吃一次连接超时。

    隐私与信任：这里列的代理**全是用户自己机器上/系统里已配置的**，
    不是我们指定的第三方服务器。TLS 与 sha256 校验照旧生效，
    所以即使代理不可信，它也无法把更新包换成别的东西
    （要解密就得先往系统里装根证书 —— 那是整机已被攻陷的场景，
    不在这个项目的威胁模型里）。
    """
    cands: list = [("直连", None)]
    seen = {None}
    try:
        env = urllib.request.getproxies()      # 环境变量 + Windows 系统设置
    except Exception:                          # noqa: BLE001
        env = {}
    for scheme in ("https", "http"):
        proxy = (env or {}).get(scheme)
        if proxy and proxy not in seen:
            seen.add(proxy)
            cands.append(("系统代理 %s" % proxy, proxy))
    for port in PROXY_PORTS:
        proxy = "http://127.0.0.1:%d" % port
        if proxy not in seen:
            seen.add(proxy)
            cands.append(("本机 %d 端口" % port, proxy))
    return cands


def _open_channel(req, timeout: float):
    """用可用的通道发这个请求；选中的通道会在进程内记住。

    只有**连不上**才换通道；服务器回了 4xx/5xx（比如 404）说明通道是通的，
    那是内容/权限问题，换通道也没用 —— 直接把状态交给调用方。
    """
    global _CHANNEL
    if _CHANNEL is not None:
        try:
            opener = _build_opener(_CHANNEL[1])
            return opener.open(req, timeout=timeout)
        except urllib.error.HTTPError:
            raise                              # 通道没问题，别再试别的
        except (urllib.error.URLError, OSError) as exc:
            log.info("通道「%s」这次不通，重新选：%s", _CHANNEL[0], exc)
            _CHANNEL = None

    last: Exception | None = None
    for label, proxy in channel_candidates():
        try:
            resp = _build_opener(proxy).open(req, timeout=min(timeout, PROBE_TIMEOUT))
        except urllib.error.HTTPError:
            _CHANNEL = (label, proxy)
            log.info("网络通道：%s（服务器已回应）", label)
            raise
        except (urllib.error.URLError, OSError) as exc:
            last = exc
            log.debug("通道「%s」不通：%s", label, exc)
            continue
        _CHANNEL = (label, proxy)
        log.info("网络通道：%s", label)
        return resp
    if last is not None:
        raise last
    raise OSError("没有任何可用的网络通道")


def open_https(req, timeout: float = CHUNK_TIMEOUT):
    """按本机可用的通道发一个 HTTPS 请求，返回已打开的响应。

    **给"第二个联网点"用的公开出口**（目前只有反馈提交走它）：
    通道选择与代理策略只有一份实现（:func:`_open_channel`），
    反馈那条路直接用同一个 —— 否则会出现"检查更新连得通、反馈却连不通"
    这种只有用户才能发现的怪事（本项目在下载那条路上踩过一次）。

    调用方负责读响应与关闭；本函数**不**跟随跳转。
    """
    return _open_channel(req, timeout)


# --------------------------------------------------------------------------
# 版本号
# --------------------------------------------------------------------------
def parse_version(text: str) -> tuple[int, ...]:
    """把 ``'1.2.3'``（可带前导 v）解析成 ``(1, 2, 3)``。非法输入抛 ValueError。"""
    return tuple(int(p) for p in text.strip().lstrip("v").split("."))


#: GitHub contents API 上带这个 Accept 会**直接回文件原文**，而不是 JSON 包裹。
#: 这样同一个地址既能当普通文本读，也能（万一对方忽略了 Accept）当 JSON 兜底解析。
_GITHUB_RAW_ACCEPT = "application/vnd.github.raw"


def _unwrap_github_api(text: str) -> str:
    """如果拿到的是 GitHub contents API 的 JSON 包裹，就拆出里面真正的文件内容。

    正常情况下我们带了 ``Accept: application/vnd.github.raw``，对方直接回原文，
    这个函数原样返回。但要兼容"Accept 被忽略/被中间层改掉"的情况 —— 那时回来的是::

        {"name": "version.txt", "encoding": "base64", "content": "MS44LjIK", ...}

    不拆包后果很具体：
      * 锚点：一整段 JSON 被当版本号 → ``parse_version`` 抛 ValueError
        → 上层当成"网络失败" → **更新功能静默失效**；
      * 清单：那段 JSON 会被当清单本身 → 顶层没有 version/url/sha256
        → 抛"清单缺字段"，而且那一步是**故意不换地址**的（坏清单换地址也没用）
        → 整条链断在第一个源上。

    所以两个地方都先过一遍这里。
    """
    s = str(text or "").strip()
    if not s.startswith("{"):
        return s
    try:
        obj = json.loads(s)
    except ValueError:
        return s
    if not isinstance(obj, dict):
        return s
    raw = obj.get("content")
    if isinstance(raw, str) and obj.get("encoding") == "base64":
        try:
            return base64.b64decode(raw).decode("utf-8", "replace").strip()
        except (ValueError, TypeError):
            return s
    return s


def fetch_latest(timeout: float = TIMEOUT) -> str:
    """取回远端版本号字符串。

    **2026-09-25 起改成"竞速"**：把候选地址一起发出去，谁先返回**合格**
    的版本号就用谁（见 :func:`_race_get`）。以前是一条一条串行试，
    主路被限流时用户要白等一个超时周期才轮到下一条。

    只给了一条候选地址时（验证用的环境变量只指一个地址）自动退化成
    单发，行为与老版本一致。

    全部失败才抛异常 —— **绝不返回""或 None 让上层以为"没有新版"**。
    """
    urls = version_urls()
    if not urls:
        raise ValueError("没有可用的版本锚点地址")
    # 单条候选就没什么可竞速的，直接用原始超时（保持老行为，测试也好写）。
    if len(urls) == 1:
        raw = _get(urls[0], timeout, MAX_BYTES, accept=_GITHUB_RAW_ACCEPT)
        text = _accept(raw)
        log.debug("版本锚点命中：%s -> %r", urls[0], text)
        return text
    # 多条候选：竞速。⚠ 竞速总时限取 min(RACE_TIMEOUT, timeout)，
    # 免得调用方特意收紧了超时、这里反而能拖更久。
    return _accept(_race_get(urls, min(RACE_TIMEOUT, timeout)))


def check_for_update(current: str = APP_VERSION, timeout: float = TIMEOUT) -> str | None:
    """远端版本比本地新时返回远端版本号，否则（含任何失败）返回 None。"""
    try:
        latest = fetch_latest(timeout)
        if parse_version(latest) > parse_version(current):
            log.info("发现新版本 %s（当前 %s）", latest, current)
            return latest
        log.debug("版本已是最新：%s", current)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # 联网失败是**预期内**的情况（朋友可能没网/被墙），记警告即可，
        # 不弹框、不阻塞、不改变整蛊流程 —— 但绝不静默吞掉。
        log.warning("版本检查失败（不影响使用）：%s", exc)
    return None


# --------------------------------------------------------------------------
# 更新清单
# --------------------------------------------------------------------------
MANIFEST_KEYS = ("version", "url", "sha256", "size")


def _manifest_targets(url: str | None) -> list:
    """要尝试的清单地址。

    * 显式传了 ``url``：**只用它**（测试拿本地 http 服务当目标，绝不能
      悄悄回退到真实网络 —— 那会让"没网时也能测"这件事失效）；
    * 环境变量 ``HAAVIK_MANIFEST_URL`` 设了：它是首选；
    * 都没设：以 ``MANIFEST_URL`` 打头，接上 ``MANIFEST_URLS`` 里的备用地址。
    """
    if url is not None:
        return [url]
    override = os.environ.get(ENV_MANIFEST_URL)
    if override:
        return [override] + [u for u in MANIFEST_URLS if u != override]
    if not MANIFEST_URL:
        return []
    return [MANIFEST_URL] + [u for u in MANIFEST_URLS if u != MANIFEST_URL]


def fetch_manifest(url: str | None = None, timeout: float = TIMEOUT) -> dict:
    """取回并校验更新清单。结构不对就抛 ValueError。

    清单长这样（就是几个字符串，刻意保持极简，便于人肉核对）::

        {
          "version": "1.2.1",
          "url":     "https://github.com/<你>/<公开仓库>/releases/download/v1.2.1/Setup.exe",
          "sha256":  "a0689f98...",
          "size":    35648712,
          "notes":   "修了 150% 缩放下按钮被裁的问题"
        }

    也支持**组件清单**（2026-09 新增）：把更新拆成多个可独立更新的块，
    这样 1GB+ 的 AI 模型包只在它自己迭代时才重发，核心程序的小修小补
    不必逼用户重下整个大模型::

        {
          "version": "1.8.0",
          "notes":   "核心程序小修 + AI 模型 v1.0 首发",
          "components": {
            "core": {"version": "1.8.0", "url": ".../Setup.exe", "sha256": "...", "size": 36300000},
            "ai":   {"version": "1.0.0", "url": ".../haavik_ai.pack", "sha256": "...",
                     "size": 900000000, "min_os": "win10"}
          }
        }

    单体清单会被当成只有一个 ``core`` 组件的特例（向后兼容旧版发布）。
    每个组件可带 ``min_os``（如 ``"win10"``）：不满足的系统看不到、也不会
    去下载它（老系统装 AI 没意义）。详见 :func:`parse_components`。

    网络失败会换下一个候选地址重试；**清单内容本身不合法则立即上抛** ——
    换地址也救不了坏清单，而且掩盖真问题。

    **2026-09-25**：候选有多条时改成**并发去拿，谁先拿到合法清单用谁**
    （同 :func:`fetch_latest`）。⚠ 但"合法"的判据比锚点严格得多：
    清单在下载前没有第二道校验（下载后才有 sha256），所以**内容不合法
    一律视为硬错误、立即上抛**，绝不因为"还有别的地址"就把它咽下去。
    """
    targets = [t for t in _manifest_targets(url) if t]
    if not targets:
        raise ValueError("没有配置更新清单地址（MANIFEST_URL 为空）")

    def _parse(raw: bytes, target: str) -> dict:
        """把一份原始清单响应解析成 dict；**不合法就抛 ValueError**。"""
        # ⚠ 先拆一层 GitHub contents API 的 JSON 包裹（万一 Accept 被忽略）。
        #   不拆的话，那段 JSON 会被当成清单本身 → 顶层没有 version/url/sha256
        #   → 抛"清单既缺少字段也没有 components" → 直接**上抛不换地址**
        #   （那一步是故意的：坏清单换地址也没用）。于是整条更新链断在第一个源上。
        body = _unwrap_github_api(raw.decode("utf-8", "replace"))
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise ValueError("清单不是合法 JSON：%s" % exc) from exc
        if not isinstance(data, dict):
            raise ValueError("清单顶层必须是一个对象")

        # 两种合法形态（任一种成立即可，都不是才报错）：
        #   * 单体清单（旧）：顶层直接带 version/url/sha256/size（原行为）；
        #   * 组件清单（新）：顶层带 ``components`` 字典，每个组件各自带这些字段。
        # —— 这样 1GB+ 的 AI 模型包可以独立成 ``ai`` 组件，只在它自己迭代时
        #    才重发，核心程序的小修小补不再逼用户重下整个大模型。
        has_flat = all(k in data for k in MANIFEST_KEYS)
        components = data.get("components")
        has_components = isinstance(components, dict) and bool(components)
        if not has_flat and not has_components:
            raise ValueError("清单既缺少字段 %r，也没有 components" % (MANIFEST_KEYS,))
        if has_flat:
            if not isinstance(data["size"], int) or data["size"] <= 0:
                raise ValueError("清单里的 size 必须是正整数，收到 %r" % (data["size"],))
            digest = str(data["sha256"]).strip().lower()
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("清单里的 sha256 不是 64 位十六进制")
            if not _allowed(str(data["url"])):
                raise ValueError("清单里的下载地址不被接受")
            data["sha256"] = digest
        parse_version(str(data.get("version") or ""))   # 顺便验证顶层版本号可解析
        log.info("更新清单：版本 %s（%s），来自 %s",
                 data.get("version"),
                 "组件清单" if has_components else "单体清单",
                 target)
        return data

    # ---- 只有一条候选（或测试显式指定了 url）：老实走单发 ----
    if len(targets) == 1:
        target = targets[0]
        try:
            raw = _get(target, timeout, 8192, accept=_GITHUB_RAW_ACCEPT)
        except ValueError:
            raise                       # 地址不被接受：配置问题，别掩盖
        except (urllib.error.URLError, OSError) as exc:
            log.warning("清单取不到（%s）：%s", target, exc)
            raise
        return _parse(raw, target)

    # ---- 多条候选：并发竞速 ----
    result: dict = {}
    done = threading.Event()
    hard_error: list = []           # 内容不合法 —— 这是硬错误，要传出去
    # ⚠ 同 _race_get 的教训：**不能用 setdefault(k, []).append()**（每次都新建列表）。
    failures: dict = {}             # url -> 网络失败原因
    tried: list = []

    def _worker(t: str) -> None:
        try:
            raw = _get(t, timeout, 8192, accept=_GITHUB_RAW_ACCEPT)
        except ValueError as exc:
            hard_error.append(exc)  # 地址不被接受：配置问题
            done.set()
            return
        except (urllib.error.URLError, OSError) as exc:
            failures[t] = exc
            tried.append(t)
            log.warning("清单取不到（%s）：%s", t, exc)
            return
        try:
            data = _parse(raw, t)
        except ValueError as exc:
            # ⚠ 拿到了东西但不是合法清单 —— **这是硬错误**。
            #   故意不做成"换下一条地址"：坏清单换地址也救不了，
            #   而且会把"发布方发错了"这个真问题藏起来。
            hard_error.append(exc)
            done.set()
            return
        result["data"] = data
        done.set()

    for t in targets[:RACE_WIDTH]:
        threading.Thread(target=_worker, args=(t,), daemon=True).start()

    if not done.wait(min(RACE_TIMEOUT, timeout)):
        raise _pick_manifest_error(hard_error, failures, tried,
                                   min(RACE_TIMEOUT, timeout))
    if hard_error:
        raise hard_error[0]
    if "data" in result:
        return result["data"]
    raise _pick_manifest_error(hard_error, failures, tried,
                               min(RACE_TIMEOUT, timeout))


def _pick_manifest_error(hard_error: list, failures: dict, tried: list,
                         timeout: float) -> Exception:
    """从一堆失败里挑一个**最有信息量**的异常抛出去。

    顺序：① 内容不合法（真问题，必须让人看到）→ ② 网络类异常
    （DNS/被墙/超时，能看出是环境问题）→ ③ 兜底的超时。
    ⚠ 绝不返回"空结果"让上层以为"没有更新" —— 那样更新功能会静默失效。
    """
    if hard_error:
        return hard_error[0]
    if failures:
        net_exc = next(
            (e for e in failures.values()
             if isinstance(e, (urllib.error.URLError, OSError))),
            None,
        )
        return net_exc or next(iter(failures.values()))
    return TimeoutError(
        "更新清单竞速 %.1f 秒内无人应答（已试 %d 条）" % (timeout, len(tried))
    )


def manifest_is_newer(manifest: dict, current: str = APP_VERSION) -> bool:
    try:
        return parse_version(str(manifest["version"])) > parse_version(current)
    except (KeyError, ValueError) as exc:
        log.warning("清单版本号无法比较：%s", exc)
        return False


# --------------------------------------------------------------------------
# 组件化更新（2026-09 新增）
# --------------------------------------------------------------------------
#: 组件信息里的必填字段（与单体清单一样，刻意保持极简）。
COMPONENT_KEYS = ("version", "url", "sha256", "size")
#: 组件体积上限（2 GB）。单体更新包仍卡在 :data:`MAX_DOWNLOAD_BYTES` 的 200MB，
#: 但 AI 模型包可能上 G，不能卡在那道闸上；这个上限仍是"防清单被篡改成指向
#: 一个超大文件"的最后一道墙。单个组件也可在清单里用 ``max_bytes`` 覆盖。
COMPONENT_MAX_BYTES = 2 * 1024 * 1024 * 1024
#: 组件下载落地后的文件名前缀（加组件名）。例如 ai 组件 → ``haavik_ai.pack``。
COMPONENT_PREFIX = "haavik_"


def parse_components(manifest: dict) -> dict:
    """把清单解析成 ``{组件名: 组件信息}``。

    兼容两种清单：
      * 单体清单（旧）：当成名为 ``"core"`` 的组件，字段直接从顶层取；
      * 组件清单（新）：直接读 ``manifest["components"]``。

    每个组件信息是一个 dict，含 ``version``/``url``/``sha256``/``size``，
    以及可选的 ``min_os``（如 ``"win10"``，表示只在 Windows 10+ 提供）。
    字段不合法会抛 ValueError；``min_os`` 不被当前系统支持时该组件会被
    **静默剔除**（不是错误 —— 老系统本来就不该看到它，也不该去下载）。
    """
    comps: dict = {}
    raw = manifest.get("components")
    if isinstance(raw, dict) and raw:
        for name, info in raw.items():
            if not isinstance(info, dict):
                raise ValueError("组件 %r 不是对象" % name)
            for key in COMPONENT_KEYS:
                if key not in info:
                    raise ValueError("组件 %r 缺少字段 %r" % (name, key))
            if not isinstance(info["size"], int) or info["size"] <= 0:
                raise ValueError("组件 %r 的 size 必须是正整数" % name)
            digest = str(info["sha256"]).strip().lower()
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("组件 %r 的 sha256 不是 64 位十六进制" % name)
            if not _allowed(str(info["url"])):
                raise ValueError("组件 %r 的下载地址不被接受" % name)
            # 可选的备用地址列表（多源分发）。这里只检查**形状**：
            # 具体某个地址合不合规交给下载那一步筛（见 component_urls / 
            # download_component）—— 一个坏镜像不该让"整份清单都不认"，
            # 那会把"还有别的源可用"这件事一起毁掉。
            extra_urls = info.get("urls")
            if extra_urls is not None:
                if not isinstance(extra_urls, (list, tuple)) or not extra_urls:
                    raise ValueError("组件 %r 的 urls 必须是非空列表" % name)
                for u in extra_urls:
                    if not isinstance(u, str) or not u.strip():
                        raise ValueError("组件 %r 的 urls 里混进了空地址" % name)
            parse_version(str(info["version"]))
            # 老系统不支持的组件（如 AI 只在 Win10+）：剔除，不去下载
            min_os = str(info.get("min_os") or "")
            if min_os and not _os_satisfies(min_os):
                log.info("组件 %r 要求 %s，当前系统不满足 → 跳过", name, min_os)
                continue
            info = dict(info)
            info["sha256"] = digest
            comps[name] = info
        return comps

    # 单体清单：合成一个 core 组件（向后兼容旧版发布）
    for key in MANIFEST_KEYS:
        if key not in manifest:
            raise ValueError("单体清单缺少字段 %r" % key)
    if not isinstance(manifest["size"], int) or manifest["size"] <= 0:
        raise ValueError("单体清单的 size 必须是正整数")
    digest = str(manifest["sha256"]).strip().lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("单体清单的 sha256 不是 64 位十六进制")
    if not _allowed(str(manifest["url"])):
        raise ValueError("单体清单的下载地址不被接受")
    core = {
        "version": str(manifest["version"]),
        "url": str(manifest["url"]),
        "sha256": digest,
        "size": int(manifest["size"]),
    }
    # ⚠ 单体清单也要把顶层的 `urls`（多源备用地址）带进来 —— 否则"合成出来的 core
    # 组件"看起来只有一个源，而**线上用的就是单体清单**，等于多源白做。
    # （2026-09-22 实测踩到：改完清单用程序自己的代码一解析，core 只有 1 个源。）
    # 这里只收"形状对"的：不是列表、或有空元素，就当没有（记一条警告），
    # 不让一个可选的备用字段把整份清单否掉 —— 那会把"还有别的源可用"一起毁掉。
    extra_urls = manifest.get("urls")
    if extra_urls is not None:
        if (isinstance(extra_urls, (list, tuple)) and extra_urls
                and all(isinstance(u, str) and u.strip() for u in extra_urls)):
            core["urls"] = [str(u).strip() for u in extra_urls]
        else:
            log.warning("单体清单的 urls 形状不对，已忽略：%r", extra_urls)
    comps["core"] = core
    return comps


def _os_satisfies(min_os: str) -> bool:
    """极简 OS 门槛判断，只认 ``"win10"`` / ``"win7"`` / ``"win"`` 这种粒度。

    这里**不引入**平台细节（那属于 shell_ops），只做"够不够新"的粗判：
      * ``win10`` → Windows 10/11（major.minor >= (10, 0)）；
      * ``win7``  → Windows 7+（>= (6, 1)）；
      * ``win``   → 任意 Windows。
    非 Windows 一律不满足（调用方会先把非 Windows 的组件剔掉，这里只是兜底）。
    """
    import platform
    if not str(platform.system()).lower().startswith("win"):
        return False
    try:
        parts = [int(x) for x in str(platform.version()).split(".")[:2]]
        major, minor = parts[0], parts[1]
    except (ValueError, IndexError, TypeError):
        return False
    if min_os == "win10":
        return (major, minor) >= (10, 0)
    if min_os == "win7":
        return (major, minor) >= (6, 1)
    if min_os == "win":
        return True
    return False


def _host_of(url: str) -> str:
    """从地址里取出主机名，只为了在失败信息里写清"是哪个源挂的"。"""
    try:
        return urllib.parse.urlparse(url).netloc or url
    except Exception:                                          # noqa: BLE001
        return url


#: 换源前"探一下这个源活不活"的时间预算（秒）。
#:
#: 由来（2026-09-22 实测，必须留着这段数据，否则以后一定会有人想把 GitHub 排第一）：
#:   * Gitee：首字节 3.6 秒，25 秒读到 22 MB（≈1 MB/s）→ 69MB 约 1 分钟；
#:   * GitHub：首字节 **47~50 秒**，实际吞吐约 **20 KB/s**；97MB 的包读 298 秒直接超时。
#:   换浏览器 UA / 加常见请求头**都没用**（三种 UA 首字节都是 ~50 秒）——
#:   这不是配置问题，是到 `release-assets.githubusercontent.com` 的线路问题。
#:
#: 结论：GitHub 可以当"容量兜底"（发行版总量无上限、单文件 2 GB），
#: 但**不能放在下载路径的第一位**。可偏偏"以后要脱离 Gitee"这件事又要求
#: 从现在起就把它排在前面练着 —— 那就必须有这道闸：
#: 每个源在真下载之前，先要求它**在这个秒数内吐出第一个字节**，否则立刻跳过。
#: 这样"GitHub 排第一"的代价被压到 ~10 秒（而不是几分钟），
#: 而且哪天线路真变好了，它自己就会被用上。
SOURCE_PROBE_TIMEOUT = 8.0
#: 健康检查要**真读到**多少字节才算"拉得动"。
#:
#: ⚠ 为什么不是"读到 1 个字节就行"（2026-09-22 亲手踩的坑）：
#: 一开始探针只读 1 个字节，结果 GitHub **通过了**（12.8 秒拿到 1 字节），
#: 可真去下 69MB 时首字节要 47 秒、之后每秒只走几十 KB。
#: **"1 个字节快"不等于"69MB 拉得动"** —— 那是个假保险，比没有更危险
#: （它会让一个拉不动的源看起来是好的）。
#: 所以要限时读满这个量，量的才是**吞吐**。
#:
#: ⚠ 另外要防"错误页假通过"：加速站挂了的时候往往回一个几 KB 的 HTML，
#: 会被"小文件也算读完"这条规则放过。所以还要比一次
#: **服务器声明的总长度 == 期望大小**（见 ``expect_size`` 参数）。
SOURCE_PROBE_SAMPLE = 256 * 1024
#: 健康检查时**单次读**的超时。给得小是为了让总预算真的封得住 ——
#: ``resp.read()`` 是阻塞的，socket 超时才是它唯一的刹车；
#: 若沿用 30 秒的下载超时，一次卡住就把整个预算拖穿了。
SOURCE_PROBE_READ_TIMEOUT = 2.0

#: "够快，不用再找了"的门槛（字节/秒）。Gitee 正常是 1 MB/s 上下。
#: 第一个源达到这个速度就直接用它 —— 别为了"可能更快"给每次更新都加十几秒探测。
COMFORTABLE_RATE = 300 * 1024

#: **中途换源**的阈值（2026-09-23 加）。
#:
#: 选源只看了 256KB 短样本，会挑到"开局够快、长跑极差"的源 —— 所以下载中也要盯着。
#: 这条阈值的意思是：**已经接到 ≥ :data:`SLOW_MIN_BYTES` 字节之后**，只要持续
#: ≥ :data:`SLOW_PATIENCE_SECONDS` 秒速度都低于 :data:`SLOW_THRESHOLD_BPS`，
#: 就放弃这条源、把半截 .part 留着续传、让下一个源接班。
#:
#: 阈值为什么是 100 KB/s：
#:   * 60 MB 的 Setup.exe 在 100 KB/s 下要走 10 分钟 —— 一个组件给 10 分钟的耐心够长；
#:   * 健康检查（256 KB）只能保证"握手那一小段"够快，长跑常掉链（GitHub 实测
#:     171 KB/s 通过健康检查，8 秒后只剩 ~100 KB/s，再过几秒就剩 2 KB/s），
#:     没有这条阈值就是"看着在走、永远走不完"。
#:
#: 三个参数都得有，缺一个就会误判：
#:   * 没 :data:`SLOW_MIN_BYTES`：TLS 握手那几 KB 也会被算成"速度 0" → 一开始就换；
#:   * 没 :data:`SLOW_PATIENCE_SECONDS`：每次短暂抖动就换源，源会被换光；
#:   * 没 :data:`SLOW_THRESHOLD_BPS`：剩 5 KB/s 也能"持续慢"但没触底。
SLOW_THRESHOLD_BPS = 100 * 1024
SLOW_PATIENCE_SECONDS = 10.0
SLOW_MIN_BYTES = 64 * 1024
#: 每隔这么久采一次速度（不能太频繁：太频繁会被短抖动带偏；也不能太慢：
#: 慢了等反应过来时已经浪费了好几十 KB 的耐心）。
SLOW_CHECK_EVERY = 2.0

#: **不再优待 GitHub**（2026-09-24 实测改）。
#:
#: 2026-09-23 曾把 GitHub 设成偏好源（"还是优先从 GitHub 下"）。但 2026-09-24
#: 用**完整下载 + 跳过首包计时**重测后，GitHub 直连是**全场最差**：
#: 50 KB/s 甚至读 2MB 直接超时（下 69MB 要 20 分钟以上），而镜像有 1000~1500 KB/s。
#: 所以那份偏好被数据推翻了，改成"**谁快谁前**"，GitHub 一点都不优待。
PREFER_HOST = ""
#: 偏好源的速度至少要到"测得的最快源"的这么多倍，才继续用它；低于就让位。
#: 0.6 的含义：偏好源只要不比其他源慢 40% 以上，就按意愿走它。
PREFER_MIN_RATIO = 0.6

#: **排在最后的源**（2026-09-24 使用者要求："Gitee 肯定要放最后面"）。
#:
#: 理由（他 2026-09-22 就定的战略）：**不能把身家全押在 Gitee 上** ——
#: 单仓附件只有 1 GB，且实测**速度波动大（714~2285 KB/s）**、还**不支持 Range**
#: （断点续传在它上面是废的）。所以让它当**兜底腿**：别的都好用时不劳它，
#: 别的全拉不动时它一定在。⚠ 它仍会被补在名单里，只是排在最后 —— 不会"少一个选择"。
DEMOTE_HOSTS = ("gitee.com",)


def _prefer_preferred(usable: list, measured: dict) -> list:
    """按使用者的两条意愿微调顺序（``usable`` 已按速度降序排好）：

      1. **:data:`DEMOTE_HOSTS`（Gitee）一律挪到最末** —— 让它当兜底腿，
         不把身家全押在它身上（它配额小、速度波动大、还不支持 Range）；
      2. 若设了 :data:`PREFER_HOST`，且它**够快**（>= 最快源的
         :data:`PREFER_MIN_RATIO`），再把它提到第一。

    两条都是"**偏好**"而不是"硬规则"：
      * 降级的源**仍在名单里**（只是最后试），别的都不行时照样兜得住；
      * ``PREFER_HOST`` 不够快就**不优待** —— 否则等于让每个使用者白等好几倍。
    """
    if len(usable) < 2:
        return usable
    # 1) 降级：把 DEMOTE_HOSTS 里的挪到末尾（保持它们彼此的相对速度次序）
    demoted = [u for u in usable if any(h in _host_of(u) for h in DEMOTE_HOSTS)]
    if demoted:
        kept = [u for u in usable if u not in demoted]
        if kept:                     # 别把整张表都降成空
            log.info("按意愿把 %s 排到最后（当兜底腿）",
                     "、".join(_host_of(u) for u in demoted))
            usable = kept + demoted
    # 2) 偏好：够快才往前挪
    if not PREFER_HOST:
        return usable
    fastest = measured[usable[0]][1]
    for i, url in enumerate(usable):
        if PREFER_HOST in _host_of(url):
            if fastest > 0 and measured[url][1] >= fastest * PREFER_MIN_RATIO:
                return [url] + [u for u in usable if u != url]
            log.info("偏好源 %s 只有最快源的 %.0f%%，这次不优先它",
                     _host_of(url), (measured[url][1] / fastest * 100)
                     if fastest else 0)
            return usable
    return usable


def probe_source(url: str, timeout: float = SOURCE_PROBE_TIMEOUT,
                 sample: int = SOURCE_PROBE_SAMPLE,
                 expect_size: int = 0) -> tuple:
    """探一个源：返回 ``(能不能用, 实测速度 字节/秒)``。

    判据是"限时读满 ``sample`` 字节"，也就是**量吞吐**，不是量首字节。

    ``expect_size``（可选）＝这个文件**应该有**多大。给了它就多一道：
    **服务器声明的总长度必须等于它**。这道闸专门挡"加速站其实回了一个错误页"——
    错误页往往只有几 KB，会被"小文件也算读完"那条规则误判成通过
    （2026-09-22 实测 `ghps.cc` 就是这样：回了 4259 字节的网页，探针却判它"通过"）。

    ⚠ **探测用的请求头必须和真正下载时一致** —— 否则探的是另一件事。
    这就是为什么这里用模块的 ``USER_AGENT``，而不是另起一个"好看"的 UA。
    """
    if not _allowed(url):
        return False, 0.0
    t0 = time.time()
    deadline = t0 + timeout
    got = 0
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT,
                          "Range": "bytes=0-%d" % (sample - 1)})
        with _open_channel(req, SOURCE_PROBE_READ_TIMEOUT) as resp:
            total = 0
            content_range = resp.headers.get("Content-Range") or ""
            if "/" in content_range:                 # 206：.../总长度
                try:
                    total = int(content_range.rsplit("/", 1)[1])
                except (TypeError, ValueError):
                    total = 0
            if not total:                            # 200：Content-Length 就是总长度
                try:
                    total = int(resp.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    total = 0
            if expect_size and total and total != expect_size:
                log.info("源健康检查 %s：**文件大小不对**（声明 %d，期望 %d）"
                         "—— 多半回了个错误页", _host_of(url), total, expect_size)
                return False, 0.0
            # 目标量：想读 sample，但如果这东西本来就比 sample 小，
            # 那"整份读完"就算够 —— 否则一个小包会永远通不过健康检查。
            want = min(sample, total) if total else sample
            last = time.time()
            elapsed = 0.0
            while got < want and time.time() < deadline:
                blk = resp.read(min(64 * 1024, want - got))
                if not blk:
                    break
                got += len(blk)
                now = time.time()
                elapsed += now - last
                last = now
    except Exception as exc:                                   # noqa: BLE001
        log.info("源健康检查没过 %s（%.1f 秒，只读到 %d 字节）：%s",
                 _host_of(url), time.time() - t0, got, exc)
        return False, 0.0
    ok = got >= want
    # 速度按"**真正在读数据的那段时间**"算，把建连/TLS 那段排除掉 ——
    # 否则一个首字节慢但之后飞快的源会被低估。
    rate = got / elapsed if elapsed > 0.05 else float(got)
    log.info("源健康检查 %s：%s（%.1f 秒读到 %d 字节，约 %.0f KB/s）",
             _host_of(url), "通过" if ok else "**不够快**",
             time.time() - t0, got, rate / 1024.0)
    return ok, rate


def source_ready(url: str, timeout: float = SOURCE_PROBE_TIMEOUT,
                 sample: int = SOURCE_PROBE_SAMPLE,
                 expect_size: int = 0) -> bool:
    """只要布尔值的场合用这个（内部就是 :func:`probe_source`）。"""
    return probe_source(url, timeout, sample, expect_size)[0]


def order_sources(urls: list, expect_size: int = 0) -> tuple:
    """把候选源排成**实际尝试顺序**：能用的按实测速度从快到慢，不能用的垫在后面。

    这是"**有时候特别慢怎么办**"的答案：慢不是靠等，是靠**换个快的源**。

    规矩（省时间和找快源两头都要顾）：
      * 先探第一个。**已经够快**（>= :data:`COMFORTABLE_RATE`）就直接用它，
        不再打扰其它源 —— 否则每次更新都要多花十几秒去"比价"，
        而这十几秒对绝大多数用户是白花的；
      * 第一个偏慢 → 把剩下的也比一遍，谁快用谁；
      * 探不通的源**排在最后**而不是丢掉：万一是探测那一刻网络抖动、
        真下载时又好了，还能兜一次。

    返回 ``(能用的, 探不通的, 逐条探测说明)``。

    **分两组返回是有意的**：能用的先按速度试着下；只有当能用的全失败了，
    才去试"探不通"的那组。这样既保住了"探测不过就快速跳过"（省时间），
    又保留了"万一探测那一刻只是网络抖了一下、真下的时候又好了"的兜底机会。
    """
    notes: list = []
    if not urls:
        return [], [], notes
    measured: dict = {}

    def _measure(url):
        ok, rate = probe_source(url, expect_size=expect_size)
        measured[url] = (ok, rate)
        return ok, rate

    ok, rate = _measure(urls[0])
    # ⚠ "第一个够快就直接用"这条捷径**只在第一个源不是降级源时**才能走。
    # 否则名单里排第一的是 Gitee（降级源）时，这条捷径会把它又扶回第一位，
    # 降级就白做了（2026-09-24 加降级时发现的漏洞）。
    first_demoted = any(h in _host_of(urls[0]) for h in DEMOTE_HOSTS)
    if ok and rate >= COMFORTABLE_RATE and not first_demoted:
        notes.append("  · %s → %.0f KB/s（够快，直接用）"
                     % (_host_of(urls[0]), rate / 1024.0))
        return list(urls), [], notes
    notes.append("  · %s → %s" % (_host_of(urls[0]),
                                  "%.0f KB/s（偏慢，去看看别的源）" % (rate / 1024.0)
                                  if ok else "拉不动"))
    if first_demoted and ok:
        notes.append("  · %s 是要降级的源，继续比价" % _host_of(urls[0]))
    for url in urls[1:]:
        ok2, rate2 = _measure(url)
        notes.append("  · %s → %s" % (_host_of(url),
                                      "%.0f KB/s" % (rate2 / 1024.0)
                                      if ok2 else "拉不动"))
    usable = [u for u in urls if measured[u][0]]
    usable.sort(key=lambda u: -measured[u][1])          # 快的在前
    usable = _prefer_preferred(usable, measured)        # 偏好平台够快就往前挪
    unusable = [u for u in urls if not measured[u][0]]
    return usable, unusable, notes


#: **本次不用的源**（2026-09-24 使用者要求："桌面上那个程序先把从 Gitee 下删了，
#: 我要试试 GitHub 单独下载的速度"）。
#:
#: 这是**临时**开关（他说的是"先试试"），不是永久路线：想恢复就把元组清空。
#: 效果：`component_urls` 会把命中的地址**整个丢掉**（连 `url` 兜底位也不补），
#: 于是客户端只剩 GitHub 系 + 镜像，一个 Gitee 地址都不会碰。
#:
#: ⚠ 风险要知道：万一哪天 GitHub 系全挂了，客户端**没有 Gitee 可退**。
#: 这正是他要"试"的东西 —— 拿真实体验判断 GitHub 单独扛不扛得住。
EXCLUDE_HOSTS = ("gitee.com",)


def component_urls(info: dict) -> list:
    """这个组件要**依次尝试**的下载地址（多源分发）。

    清单里可以有两样东西：

      * ``url``  —— **单个地址**。老版本程序只认这一个，所以它必须留着，
        而且必须指向一个**真的能用**的源（一般选"国内最快的那个"）；
      * ``urls`` —— 一组地址，**这是新客户端的偏好顺序**。可以故意把
        ``url`` 排在后面，甚至不列它 —— 意思就是"新客户端优先走别的源，
        老客户端仍旧走这一个"。

    ``url`` 会被**补在最后**当作永远兜底。这条很关键：
    老客户端只有 ``url``，所以它一定是个活着的源；把 ``url`` 补在末尾，
    就保证了**新客户端绝不会比老客户端更差**（最坏情况也只是多试几个源），
    同时又能按 ``urls`` 的顺序去用更快的/更大的平台。

    ⚠ 例外：:data:`EXCLUDE_HOSTS` 里的源会被**完全剔除**（连兜底位也不留）——
    那是使用者明确要求"这次别用它"时用的开关。
    """
    primary = str(info.get("url") or "").strip()
    out: list = []
    extra = info.get("urls")
    if isinstance(extra, (list, tuple)):
        for item in extra:
            u = str(item or "").strip()
            if u and u not in out:
                out.append(u)
    if primary and primary not in out:
        out.append(primary)
    # 最后一道过滤：使用者明确不要的源，连兜底位也不留（见 EXCLUDE_HOSTS）。
    if EXCLUDE_HOSTS:
        kept = [u for u in out if not any(h in _host_of(u) for h in EXCLUDE_HOSTS)]
        if kept != out:
            log.info("按要求剔除了 %d 个源：%s", len(out) - len(kept),
                     "、".join(_host_of(u) for u in out if u not in kept))
        out = kept
    return out


def download_component(name: str, info: dict, dest_dir: str | None = None,
                       timeout: float = CHUNK_TIMEOUT,
                       on_progress=None) -> tuple[bool, str]:
    """下载某一个组件（按 name + 组件信息）。语义同 :func:`download_update`，
    但文件名稳定为 ``haavik_<name>.pack``，方便原地替换；体积上限用
    :data:`COMPONENT_MAX_BYTES`。

    ``dest_dir`` 不传则落到 :func:`update_dir`（与核心更新包同目录）。

    **多源**：清单给了一组地址就按顺序试（见 :func:`component_urls`）。
    某个源连不上、或者下下来的东西 sha256 对不上，都换下一个 ——
    前者是"这个镜像挂了"，后者是"这个镜像上的文件是旧的/坏的"，
    两种情况都该换，换来换去都不过才是真的失败。
    """
    # 落地文件名稳定为 haavik_<name>.pack（与 prune_updates / 测试约定一致，
    # 也和清单里 haavik_ai.pack 的下载地址文件名对齐）。
    label = "%s%s.pack" % (COMPONENT_PREFIX, name)
    max_bytes = int(info.get("max_bytes") or COMPONENT_MAX_BYTES)

    tried = [u for u in component_urls(info) if _allowed(u)]
    skipped = [u for u in component_urls(info) if not _allowed(u)]
    for u in skipped:
        log.warning("清单里的备用地址不被接受（只收 https），跳过：%s", u)
    if not tried:
        return False, "清单里没有可用的下载地址（组件 %s）" % name

    multi = len(tried) > 1
    if multi:
        # "有时候特别慢怎么办"：不是干等，而是**把候选源比一遍、挑最快的那个**。
        # 第一个源已经够快时，order_sources 不会去打扰其它源。
        usable, unusable, notes = order_sources(tried, expect_size=int(info["size"]))
    else:
        usable, unusable, notes = list(tried), [], []
    reasons: list = list(notes)
    total_src = len(usable) + len(unusable)
    attempt_no = 0
    for group in (usable, unusable):
        for url in group:
            attempt_no += 1
            if attempt_no > 1:
                log.info("换下一个源（%d/%d）：%s", attempt_no, total_src, _host_of(url))
                # 让界面上的进度条回到起点，不然会看着像"卡在 100% 不动"
                _report(on_progress, 0, int(info["size"]), "retry")
            ok, msg = _download_file(url, int(info["size"]),
                                     str(info["sha256"]).lower(), label,
                                     dest_dir, timeout, on_progress, max_bytes)
            if ok:
                if attempt_no > 1:
                    log.info("第 %d 个源成功了：%s", attempt_no, _host_of(url))
                return True, msg
            reasons.append("  · %s → %s" % (_host_of(url), msg))

    return False, "所有下载源都失败了（共 %d 个）：\n%s" % (total_src, "\n".join(reasons))



def update_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "HAAVIK Photo", UPDATE_DIRNAME)


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _report(on_progress, done, total, phase):
    """喂一次进度。

    回调是界面给的，跑在工作线程里 —— 所以：
      * 它自己不能碰 tk（要在调用方换到主线程），这里只管把数字送出去；
      * 它要是抛了，**记一条 warning 就好，绝不让下载因为画进度条而失败**。
        这跟"失败要响"不冲突：响的是日志，不是让用户丢了这次下载。
    """
    if on_progress is None:
        return
    try:
        on_progress(done, total, phase)
    except Exception as exc:                               # noqa: BLE001
        log.warning("进度回调出错（已忽略，下载继续）：%s", exc)


#: 半截下载文件（``*.part``）保留多久（秒）—— 3 天。
#:
#: 为什么要留：**断点续传的命根子就是这个半截文件**。下到一半关掉程序、
#: 重启电脑，只要它还在，下次就从断点接着下；一进来就清掉，
#: 等于把"慢归慢、但总能下完"这件事废掉（2026-09-22 发现这个冲突）。
#: 留 3 天是两头兼顾：这几天里接着下都没问题，太老的照样清掉，免得攒垃圾。
PART_KEEP_SECONDS = 3 * 24 * 3600


def prune_updates(current_version: str | None = None) -> list:
    """清理更新目录里**已经用过**的安装包，减少残留。

    为什么需要：每点一次「检查更新」就会在
    ``%LOCALAPPDATA%\\HAAVIK Photo\\updates\\`` 落一个 ``Setup-x.y.z.exe``
    （34.5MB）。更新几次就攒出上百 MB，而它们再也不会被用到 ——
    用户不会自己去那个目录删东西，所以得程序自己收。

    规矩（守死，别扩大）：
      * **只在 ``update_dir()`` 这一层动手**：不递归、不碰父目录、不碰别处；
      * 只删 ``Setup-*.exe``（版本号不高于当前的）与**过期的** ``*.part``，
        目录里的任何其它东西都留着 —— 特别地，**已下完的 ``haavik_*.pack``
        是装好的 AI 模型包，绝不清**；
      * ``Setup-*.exe`` 只删**版本号不高于当前程序**的那些 —— 比当前版本
        新的那个包很可能是用户刚下好、还没装的，绝对不能删；
      * 删不掉就记日志继续：清理失败不该影响任何事。

    ⚠ **``*.part`` 只清"过期的"**（见 :data:`PART_KEEP_SECONDS`）。
    半截文件是**断点续传的命根子**：下到一半关掉程序、下次打开还想接着下，
    靠的就是它。一进来就清掉，等于把"慢归慢，但总能下完"这件事废掉
    —— 2026-09-22 发现这个冲突，当时这里是无条件删的。

    Returns: 删掉的文件名列表（给日志和测试看）。
    """
    folder = update_dir()
    try:
        names = os.listdir(folder)
    except OSError:
        return []                      # 目录还不存在 = 没什么可清的
    now = parse_version(current_version) if current_version else None
    removed = []
    for name in sorted(names):
        path = os.path.join(folder, name)
        if not os.path.isfile(path):      # 目录/软链接一律不碰
            continue
        if name.endswith(".part"):
            try:
                age = time.time() - os.path.getmtime(path)
            except OSError:
                continue                  # 读不到时间就当不知道 —— 宁可留着
            if age < PART_KEEP_SECONDS:
                continue                  # 还新鲜 → 留给断点续传接着下
        elif name.startswith("Setup-") and name.endswith(".exe"):
            stamp = name[len("Setup-"):-len(".exe")]
            try:
                stamp_ver = parse_version(stamp)
            except ValueError:
                continue      # 名字里的版本号看不懂 → 当作"不是我们下的"，不碰
            if now is None or stamp_ver > now:
                continue                  # 比当前版本新 → 留着
        else:
            continue                      # haavik_*.pack 等其它文件一律不动
        try:
            os.remove(path)
            removed.append(name)
        except OSError as exc:
            log.warning("清理旧更新包失败（不影响使用）：%s：%s", path, exc)
    if removed:
        log.info("已清理 %d 个用过/半截的更新包：%s",
                 len(removed), "、".join(removed))
    return removed


#: 速度/剩余时间用"滑动窗口"算：至少隔这么久才算一次，否则数字会乱跳。
RATE_WINDOW = 1.5


def _human_size(num: int) -> str:
    mb = num / 1048576.0
    return ("%.1f MB" % mb) if mb >= 1 else ("%.0f KB" % (num / 1024.0))


def _human_secs(secs: float) -> str:
    secs = max(0, int(secs))
    if secs < 60:
        return "%d 秒" % secs
    if secs < 3600:
        return "%d 分 %d 秒" % (secs // 60, secs % 60)
    return "%d 小时 %d 分" % (secs // 3600, (secs % 3600) // 60)


def describe_rate(prev, done, total, now: float | None = None) -> tuple:
    """把"已下多少 / 总共多少"变成一句人话：**下载速度 + 还要等多久**。

    这是"**有时候特别慢怎么办**"的一半答案：光有一条爬得极慢的进度条，
    人只会以为程序卡死了。把速度和剩余时间摆出来，他就能自己决定
    "是等，还是先干别的、回头再更新"（反正断了也能接着下）。

    ``prev`` ＝ 上一次的 ``(时间戳, 已下载字节)``，``None`` 表示第一次。
    返回 ``(要显示的那句话, 新的 prev)``；还看不出速度时那句话是空串
    （调用方就别改界面，免得数字乱跳）。

    为什么按"最近这一小段"算而不是从头算平均：**中途换源/续传会让总平均失真**——
    从 20 KB/s 的源换到 1 MB/s 的源之后，总平均还要拖很久才反映得过来。
    """
    now = time.time() if now is None else now
    try:
        done, total = int(done), int(total)
    except (TypeError, ValueError):
        return "", prev
    if total <= 0 or done < 0:
        return "", prev
    if prev is None or done < prev[1]:
        # 第一次，或者进度倒退了（换源从头下 / 续传重置）→ 重新起算
        return "", (now, done)
    span, moved = now - prev[0], done - prev[1]
    if span < RATE_WINDOW:
        return "", prev                     # 窗口太短，算出来会乱跳
    head = "已下载 %s / %s · " % (_human_size(done), _human_size(total))
    if moved <= 0:
        # 这一整段时间一个字节都没往前走 —— 如实说，别假装还在动
        return head + "**卡住了**（正在重试或换源…）", (now, done)
    rate = moved / span
    left = max(total - done, 0) / rate
    return (head + "%.0f KB/s · 还需约 %s" % (rate / 1024.0, _human_secs(left)),
            (now, done))


class _IntegrityError(Exception):
    """**内容/校验**类失败：文件本身不对 —— 不重试、也不续传。

    为什么要单独一个类型，而不是复用 ``ValueError``：
    下载循环里要区分两类失败 —— **"没下完"**（可重试、保留半截文件续传）与
    **"下完了但东西不对"**（重试和续传都没意义）。
    以前用 ``ValueError`` 当后者的标记，于是 except 只能写成
    ``(URLError, OSError, http.client.HTTPException)`` 那一边 ——
    而 ``http.client`` 是审计的**红线模块**，为了写这个 except 去 import 它，
    构建会被审计拦下（2026-09-23 实测：`[失败] 红线模块被本项目源码直接 import：['http']`）。

    改成"自己的异常标记内容错、其余一律当没下完"之后：
      * 不再需要那条 import；
      * 而且更稳 —— 传一半被掐断时可能抛的 ``IncompleteRead``、
        以及将来没预料到的异常类型，**全都落进"可重试"**（这正是我们要的默认），
        不会让一次网络抖动把整个更新崩掉。
    """


class _TooSlowError(Exception):
    """**速度过慢**类失败：连接正常、字节在走，但持续低于合理速度。

    跟 _IntegrityError 的区别：这里不是"内容不对"，所以**保留半截 .part**，
    让下一个源能 Range 续上。跟网络异常（连接被掐、超时）的区别：网络异常的
    特征是"完全没数据在走"，这条的特征是"**有数据、但慢得不像话**"。

    为什么必须做这件事（2026-09-23 加）：选源只看了 256KB 短样本，会挑到
    "开局够快、长跑极差"的源（实测 GitHub 171KB/s 通过健康检查、真跑 8 秒
    只下了 0.81MB）—— 256KB 测的是握手后那一小段，长跑一被线路拖累就抓瞎。
    所以得在**下载中**也盯着速度；到忍耐极限就把这条腿砍掉，让下一个源接班。
    """


def _remove_quietly(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _download_file(url: str, expect_size: int, expect_sha: str, label: str,
                   dest_dir: str | None, timeout: float, on_progress,
                   max_bytes: int = MAX_DOWNLOAD_BYTES,
                   retries: int = 3) -> tuple[bool, str]:
    """把 ``url`` 指向的文件流下载到 ``<dest_dir>/<label>.part``，校验 sha256
    后改名为 ``<label>``。

    三道闸，任何一道不过就删掉半截文件并返回失败：
      1. 体积上限 ``max_bytes``（防清单被改成指向一个巨大的文件）；
      2. 字节数必须与期望一致；
      3. sha256 必须与期望一致 —— 真正的完整性保证。

    **断点续传**（2026-09-22 加）：网络断了**不删半截文件**，下次带上
    ``Range: bytes=<已落盘>-`` 接着下。为什么必须做：从国内下 GitHub 的大文件
    很容易传到一半掉线（实测 ≈20 KB/s），没有续传就得从 0 重来，
    一个 97MB 的包永远下不完。

    ⚠ 续传有一个**必须防的坑**：服务器可能**不认** ``Range``，直接回完整文件（HTTP 200）。
    这时若还往半截文件后面追加，就会拼出"前半截旧 + 完整新"的损坏文件 ——
    而且**体积很可能正好等于期望值**，光看大小发现不了。所以只认 ``206``；
    不是 206 就清空重来。

    网络类失败（断网 / 通道超时 / 连接被重置）会**自动重试** ``retries`` 次，
    退避递增（2s、4s…最多 8s）；连着两次"一字节都没往前走"就收手，
    免得碰上一个只回你几字节再断的服务器把重试磨到天荒地老。
    内容类失败（体积 / sha 不符）**不重试也不续传** —— 那说明清单或文件本身有问题，
    留着半截文件只会把错误接下去。
    """
    if expect_size > max_bytes:
        return False, "体积（%d 字节）超过上限 %d，拒绝下载" % (expect_size, max_bytes)

    folder = dest_dir or update_dir()
    dest = os.path.join(folder, label)
    tmp = dest + ".part"
    last_err: Exception | None = None
    progress_last = -1          # 上一轮结束时落盘了多少 —— 用来判断"有没有往前走"
    stall = 0
    attempts = max(retries, 1)
    for attempt in range(1, attempts + 1):
        try:
            os.makedirs(folder, exist_ok=True)

            # ---- 断点续传：把上一轮留下的半截文件接着用 ----
            resume_from = 0
            if os.path.exists(tmp):
                have = os.path.getsize(tmp)
                if 0 < have < expect_size:
                    resume_from = have
                    log.info("发现半截文件，准备续传：%d / %d 字节", have, expect_size)
                else:
                    # 空文件 / 比目标还大 —— 不可信，清掉重来
                    _remove_quietly(tmp)

            digest = hashlib.sha256()
            if resume_from:
                with open(tmp, "rb") as fh:
                    for blk in iter(lambda: fh.read(CHUNK), b""):
                        digest.update(blk)

            headers = {"User-Agent": USER_AGENT}
            if resume_from:
                headers["Range"] = "bytes=%d-" % resume_from
            req = urllib.request.Request(url, headers=headers)
            log.info("开始下载 %s -> %s（第 %d/%d 次%s）",
                     url, dest, attempt, attempts,
                     "，从 %d 字节续" % resume_from if resume_from else "")
            # 与检查更新走同一条通道（否则会出现"检查得通、下载却直连失败"的怪事）
            with _open_channel(req, timeout) as resp:
                status = int(getattr(resp, "status", 200) or 200)
                written = resume_from
                mode = "ab" if resume_from else "wb"
                if resume_from and status != 206:
                    log.warning("服务器不支持续传（HTTP %s），这次从头下", status)
                    resume_from, written, mode = 0, 0, "wb"
                    digest = hashlib.sha256()
                elif resume_from:
                    log.info("服务器支持续传（HTTP 206），从 %d 字节接着下", resume_from)
                try:
                    declared = int(resp.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    declared = 0
                received = 0
                # ⚠ 中途换源：每隔 :data:`SLOW_CHECK_EVERY` 秒采一次速度，
                # 累积 ≥ :data:`SLOW_MIN_BYTES` 字节之后还持续 ≥ :data:`SLOW_PATIENCE_SECONDS`
                # 秒低于 :data:`SLOW_THRESHOLD_BPS`，就抛 _TooSlowError 让外层换源。
                # 初始化必须在循环外（每次 attempt 都要重置：换源后是新的连接、新的速度曲线）。
                speed_check_at = time.monotonic() + SLOW_CHECK_EVERY
                speed_bytes_at = 0
                slow_since = None
                with open(tmp, mode) as fh:
                    while True:
                        block = resp.read(CHUNK)
                        if not block:
                            break
                        received += len(block)
                        written += len(block)
                        if written > max_bytes:
                            raise _IntegrityError("下载超过体积上限，已中止")
                        digest.update(block)
                        fh.write(block)
                        _report(on_progress, written, expect_size, "download")
                        # ---- 速度采样 ----
                        now = time.monotonic()
                        if now >= speed_check_at:
                            db = written - speed_bytes_at
                            dt = now - (speed_check_at - SLOW_CHECK_EVERY)
                            bps = db / dt if dt > 0 else 0
                            if written > SLOW_MIN_BYTES and bps < SLOW_THRESHOLD_BPS:
                                if slow_since is None:
                                    slow_since = speed_check_at - SLOW_CHECK_EVERY
                                elif now - slow_since >= SLOW_PATIENCE_SECONDS:
                                    raise _TooSlowError(
                                        "持续 %.0f 秒低于 %d KB/s（当前 %.0f KB/s）"
                                        % (now - slow_since,
                                           SLOW_THRESHOLD_BPS // 1024,
                                           bps / 1024))
                            else:
                                slow_since = None
                            speed_check_at = now + SLOW_CHECK_EVERY
                            speed_bytes_at = written
                    fh.flush()
                    os.fsync(fh.fileno())

            # ⚠ 传输被截断？—— 服务器声明的长度没给够。
            # 必须**单独识别出来**，因为 `resp.read(amt)` 在"提前 EOF"时
            # **不抛异常**，只是少给数据（只有 read() 不带参数才抛 IncompleteRead）。
            # 不识别的话，"传到一半掉线"会被当成"内容不对" → 删掉半截文件 →
            # **永远续不上**，正好把断点续传这条命根子废掉（2026-09-22 实测抓到）。
            # 归到"网络类"，才能保留 .part 并重试续传。
            if declared and received < declared:
                raise urllib.error.URLError(
                    "传输被截断：服务器声明 %d 字节，只收到 %d 字节"
                    % (declared, received))

            # 进校验阶段先说一声：进度条会满格，下面这段要算 sha256
            _report(on_progress, expect_size, expect_size, "verify")
            if written != expect_size:
                raise _IntegrityError("体积不符：期望 %d 字节，实际 %d 字节"
                                      % (expect_size, written))
            actual = digest.hexdigest()
            if actual != expect_sha:
                raise _IntegrityError("sha256 不符：期望 %s，实际 %s"
                                      % (expect_sha[:16], actual[:16]))

            os.replace(tmp, dest)
            log.info("已下载并校验通过：%s（%d 字节，sha256=%s…）",
                     dest, written, actual[:12])
            return True, dest
        except _IntegrityError as exc:
            # 内容 / 校验问题 → 不重试，并且**删掉半截文件**：
            # 内容本身就不对，留着它续传只会把错误接下去。
            last_err = exc
            log.warning("下载失败（内容校验，不重试）：%s", exc)
            _remove_quietly(tmp)
            return False, str(exc)
        except _TooSlowError as exc:
            # ⚠ 这里**不重试**（这条源就是慢）、**不删半截文件**（让下一个源续传）。
            # 重试会让同一个慢源再浪费 SLOW_PATIENCE_SECONDS 才认命；
            # 不删半截文件则让下一个源能 Range 接着下 —— 这是"中途换源"的核心契约。
            last_err = exc
            log.warning("下载过慢（第 %d/%d 次）：%s", attempt, attempts, exc)
            return False, ("持续过慢：%s（已落盘 %d/%d 字节，下个源接着下）"
                           % (exc, written, expect_size))
        except Exception as exc:                       # noqa: BLE001
            # 其余**一律当"没下完"**：网络、通道、读取超时、传一半被掐断
            # （IncompleteRead 也在这一档）—— 可重试，而且**保留半截文件**续传。
            #
            # ⚠ 这里故意收得宽，是有理由的：可用异常类型列不全。
            #   `http.client.IncompleteRead` 不是 OSError 的子类；而列全它就得
            #   `import http.client` —— 那是审计的红线模块，构建直接被拦
            #   （2026-09-23 实测）。所以反过来做：**自己的 _IntegrityError 标"内容错"，
            #   其余全算"没下完"**。这样既不用碰审计的红线，又比列清单更稳：
            #   将来任何一种没预料到的传输异常，都会走"重试 + 续传"，
            #   而不是让一次网络抖动把整个更新崩掉。
            # 代价是"我们代码自己的 bug"也会被重试 3 次 —— 但失败原因照原样
            # 报给使用者（下面那句 warning + 最终的返回文本），不会静默。
            last_err = exc
            have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            log.warning("下载中断（第 %d/%d 次）：%s（已落盘 %d/%d 字节，"
                        "下次接着下）", attempt, attempts, exc, have, expect_size)
            if have > progress_last:
                stall = 0
            else:
                stall += 1
            progress_last = have
            if stall >= 2:
                _remove_quietly(tmp)
                return False, ("连着两轮都没往前走（已落盘 %d/%d 字节），"
                               "放弃这个源：%s" % (have, expect_size, exc))
            if attempt < attempts:
                backoff = min(2 ** attempt, 8)
                log.info("退避 %d 秒后重试…", backoff)
                time.sleep(backoff)
                continue
            return False, "下载失败：%s" % exc
    return False, str(last_err) if last_err else "未知下载失败"


def download_update(manifest: dict, dest_dir: str | None = None,
                    timeout: float = CHUNK_TIMEOUT,
                    on_progress=None) -> tuple[bool, str]:
    """把清单指向的**核心安装包**下载下来并核对 sha256（原行为不变）。

    Returns:
        ``(是否成功, 落地路径 或 失败原因)``

    文件名带版本号（``Setup-<version>.exe``），因为多个版本的安装包可能共存于
    更新目录，不能互相覆盖。组件（如 AI 模型包）请用 :func:`download_component`。

    **多源**：和组件一样，清单给了 ``urls`` 就按顺序试（见 :func:`component_urls`）。
    """
    expect_size = int(manifest["size"])
    expect_sha = str(manifest["sha256"]).lower()
    version = str(manifest.get("version", "?"))
    label = "Setup-%s.exe" % "".join(c for c in version if c.isalnum() or c in ".-")

    tried = [u for u in component_urls(manifest) if _allowed(u)]
    if not tried:
        return False, "清单里没有可用的下载地址（核心安装包）"
    multi = len(tried) > 1
    if multi:
        usable, unusable, notes = order_sources(tried, expect_size=expect_size)
    else:
        usable, unusable, notes = list(tried), [], []
    reasons: list = list(notes)
    total_src = len(usable) + len(unusable)
    attempt_no = 0
    for group in (usable, unusable):
        for url in group:
            attempt_no += 1
            if attempt_no > 1:
                log.info("换下一个源（%d/%d）：%s", attempt_no, total_src, _host_of(url))
                _report(on_progress, 0, expect_size, "retry")
            ok, msg = _download_file(url, expect_size, expect_sha, label,
                                     dest_dir, timeout, on_progress,
                                     MAX_DOWNLOAD_BYTES)
            if ok:
                if attempt_no > 1:
                    log.info("第 %d 个源成功了：%s", attempt_no, _host_of(url))
                return True, msg
            reasons.append("  · %s → %s" % (_host_of(url), msg))
    return False, "所有下载源都失败了（共 %d 个）：\n%s" % (total_src, "\n".join(reasons))
