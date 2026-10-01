# -*- coding: utf-8 -*-
"""version_check.py 组件化更新的回归测试。

覆盖什么
==================================================================
把"一整包"拆成多个可独立更新的组件后，新增的几条逻辑：

* ``parse_components``：单体清单（旧）向后兼容；组件清单正确解析；
  ``min_os`` 不被满足时静默剔除；缺字段报错；
* ``download_component``：走真实 http（本机 127.0.0.1 回环），核对 sha256/size，
  被篡改（size 不符 / sha 不符）一律拒绝；超过体积上限（2GB）先拦下；
* ``prune_updates``：**过期的** ``.part`` 才清、新鲜的要留着（断点续传靠它），
  已下完的 ``haavik_*.pack``
  不动，旧版 ``Setup-*.exe`` 才清。

不依赖外网：清单与下载都来自本机起的 http 服务。

用法
==================================================================
    ~/.workbuddy/cache/hvphoto/venv2/Scripts/python.exe code_review/component_update_test.py
"""
from __future__ import annotations

import hashlib
import http.server
import os
import socketserver
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "optimized"))
sys.path.insert(0, ROOT)                             # 好让 _redact 也能被 import

import _redact                                       # noqa: E402
import version_check as vc                                 # noqa: E402


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
        head = ["=" * 68, "组件化更新 回归测试", "=" * 68]
        tail = ["", "=" * 68]
        if self.failures:
            tail += ["FAILED: %d 项不成立" % len(self.failures)]
            tail += ["  * " + f for f in self.failures]
        else:
            tail += ["RESULT: OK"]
        tail.append("=" * 68)
        return "\n".join(head + self.lines + tail) + "\n"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class _Handler(http.server.BaseHTTPRequestHandler):
    routes = {}                       # path -> bytes（在测试里填）
    #: 这些路径**故意不支持 Range**：要验"服务器不认续传时必须从头重来，
    #: 绝不能把新数据追加到半截文件后面拼出一个坏文件"。
    no_range = set()
    #: 这些路径**前 flaky_drops 次只发一半就掐断**：拿来测"掉线 → 自动续传"。
    flaky = {}
    flaky_drops = 2
    #: 这些路径**故意很慢**（每 8KB 睡 0.1 秒 ≈ 80 KB/s）：
    #: 拿来测"第一个源偏慢时，会去把别的源比一遍、挑更快的那个"。
    slow = set()
    #: 这些路径**前 N 字节快**（一次性写出去），之后每 4KB 睡 0.1 秒 ≈ 40 KB/s：
    #: 拿来测"健康检查通过 + 开头几秒也够快 + 然后掉链 → 中途换源"。
    slow_after = {}                  # path -> bytes（到这个字节之后才开始慢）

    def _range_start(self, total: int) -> int:
        if self.path in self.no_range:
            return 0
        raw = self.headers.get("Range") or ""
        if not raw.startswith("bytes="):
            return 0
        try:
            start = int(raw.split("=", 1)[1].split("-")[0])
        except (ValueError, IndexError):
            return 0
        return max(0, min(start, total))

    def do_GET(self):
        body = self.routes.get(self.path)
        if body is None:
            self.send_error(404)
            return
        start = self._range_start(len(body))
        piece = body[start:]

        drop = False
        if self.path in self.flaky:
            seen = self.flaky.get(self.path, 0)
            if seen < self.flaky_drops:
                self.flaky[self.path] = seen + 1
                drop = True
                piece = piece[:max(1, len(piece) // 2)]      # 只发一半

        if start:
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d"
                             % (start, start + len(piece) - 1, len(body)))
        else:
            self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        # ⚠ 长度声明的是**整份剩余**：这样"只发一半就断"在客户端看来才是
        # IncompleteRead（真实掉线就是这副样子），而不是"正常读完"。
        self.send_header("Content-Length", str(len(body) - start))
        self.end_headers()
        slow_threshold = self.slow_after.get(self.path)
        should_slow = (self.path in self.slow
                       or (slow_threshold is not None and start >= slow_threshold))
        if should_slow and not drop:
            for i in range(0, len(piece), 4096):       # 慢慢吐：≈40 KB/s
                self.wfile.write(piece[i:i + 4096])
                try:
                    self.wfile.flush()
                except OSError:
                    break
                time.sleep(0.1)
        else:
            self.wfile.write(piece)
        if drop:
            try:
                self.wfile.flush()
            except OSError:
                pass
            self.close_connection = True

    def log_message(self, *a):         # 静默
        pass


def _serve(rep, routes: dict):
    _Handler.routes = routes
    _Handler.flaky = {}
    _Handler.no_range = set()
    _Handler.slow = set()
    _Handler.slow_after = {}
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port


def main():
    rep = Report()
    rep.add("python %s | os.name=%s" % (sys.version.split()[0], os.name))

    try:
        # ============================================================
        rep.head("A. parse_components")
        # ============================================================
        flat = {"version": "1.2.1", "url": "https://example.com/S.exe",
                "sha256": "a" * 64, "size": 35}
        comps = vc.parse_components(flat)
        rep.check(list(comps) == ["core"], "单体清单 → 仅 core 组件", str(list(comps)))

        cpm = {"version": "1.8.0", "components": {
            "core": {"version": "1.8.0", "url": "https://example.com/S.exe",
                     "sha256": "a" * 64, "size": 36},
            "ai": {"version": "1.0.0", "url": "https://example.com/a.pack",
                   "sha256": "b" * 64, "size": 900000000, "min_os": "win10"}}}
        comps2 = vc.parse_components(cpm)
        rep.check(set(comps2) == {"core", "ai"}, "组件清单 → core + ai",
                  str(set(comps2)))
        rep.check(comps2["ai"]["size"] == 900000000, "ai 组件体积保留")

        # min_os 不被满足应被剔除：用 win77（_os_satisfies 不认）→ 整个 ai 被丢
        cpm_bad = {"version": "1.8.0", "components": {
            "ai": {"version": "1.0.0", "url": "https://example.com/a.pack",
                   "sha256": "b" * 64, "size": 9, "min_os": "win77"}}}
        rep.check(vc.parse_components(cpm_bad) == {}, "min_os 不满足 → 组件被剔除")

        # 缺字段应报错
        bad = {"version": "1.8.0", "components": {
            "ai": {"version": "1.0.0", "url": "https://example.com/a.pack",
                   "sha256": "b" * 64}}}        # 缺 size
        try:
            vc.parse_components(bad)
            rep.check(False, "组件缺字段 → 应抛 ValueError")
        except ValueError:
            rep.check(True, "组件缺字段 → 抛 ValueError")

        # 带备用地址（urls）的组件清单：解析后必须原样留住
        multi = {"version": "1.8.0", "components": {
            "ai": {"version": "1.0.0", "url": "https://example.com/a.pack",
                   "urls": ["https://example.com/a.pack",
                            "https://mirror.example.com/a.pack"],
                   "sha256": "b" * 64, "size": 100}}}
        mc = vc.parse_components(multi)
        rep.check(vc.component_urls(mc["ai"]) == ["https://example.com/a.pack",
                                                  "https://mirror.example.com/a.pack"],
                  "组件清单里的 urls 被保留，顺序不变",
                  str(vc.component_urls(mc["ai"])))
        rep.check(vc.parse_components(cpm)["ai"].get("urls") is None,
                  "老清单（没有 urls）照样能解析 —— 向后兼容")

        # ⚠ 线上用的其实是**单体清单**：它也必须把顶层 urls 带进 core，
        # 否则"清单里两个源"在程序眼里只有一个，多源等于白做。
        # （2026-09-22 真事：改完线上清单，用程序自己的代码一解析，core 只有 1 个源。）
        flat_multi = dict(flat, urls=[flat["url"], "https://mirror.example.com/S.exe"])
        fm = vc.parse_components(flat_multi)
        rep.check(vc.component_urls(fm["core"]) == [flat["url"],
                                                   "https://mirror.example.com/S.exe"],
                  "**单体清单的 urls 也带进 core**（线上就是单体清单）",
                  str(vc.component_urls(fm["core"])))
        rep.check("urls" not in vc.parse_components(flat)["core"],
                  "没有 urls 的单体清单照旧能解析")
        fm_bad = vc.parse_components(dict(flat, urls="not-a-list"))
        rep.check(vc.component_urls(fm_bad["core"]) == [flat["url"]],
                  "单体清单的 urls 形状不对 → 忽略它，但不否掉整份清单")

        # ============================================================
        rep.head("B. download_component（回环 http + 校验）")
        # ============================================================
        content = b"HAAVIK_AI_MODEL_PACK" * 4096
        digest = _sha(content)
        good_info = {"version": "1.0.0", "url": "", "sha256": digest,
                     "size": len(content)}
        bad_sha_info = dict(good_info, sha256="c" * 64)
        bad_size_info = dict(good_info, size=len(content) + 1)

        httpd, port = _serve(rep, {"/ai.pack": content})
        base = "http://127.0.0.1:%d/ai.pack" % port
        try:
            with tempfile.TemporaryDirectory() as d:
                good = dict(good_info, url=base)
                ok, path = vc.download_component("ai", good, dest_dir=d)
                rep.check(ok and os.path.isfile(path), "正常下载 + 校验通过",
                          str(path))
                with open(path, "rb") as fh:
                    rep.check(_sha(fh.read()) == digest, "落地文件 sha256 一致")

                # sha 不符：拒绝
                ok2, why2 = vc.download_component("ai", dict(bad_sha_info, url=base),
                                                  dest_dir=d)
                rep.check(ok2 is False, "sha256 不符 → 拒绝", why2)

                # size 不符：拒绝
                ok3, why3 = vc.download_component("ai", dict(bad_size_info, url=base),
                                                  dest_dir=d)
                rep.check(ok3 is False, "字节数不符 → 拒绝", why3)

                # 超过体积上限：声明 size 巨大 → 根本不下载
                huge = dict(good_info, url=base, size=vc.COMPONENT_MAX_BYTES + 1)
                ok4, why4 = vc.download_component("ai", huge, dest_dir=d)
                rep.check(ok4 is False and "上限" in why4, "超过上限 → 先拦下", why4)
        finally:
            httpd.shutdown()

        # ============================================================
        rep.head("B2. 多源下载：一个源挂了自动换下一个")
        # ============================================================
        # 使用者的诉求：以后要换平台，但**已经装了的老用户必须还能更新**。
        # 做法是清单里给一组地址：url（老客户端只认它，必须留着且必须真能用）
        # + urls（新客户端按顺序试）。这一节盯的就是"换源"真的会发生。
        rep.check(vc.component_urls({"url": "A"}) == ["A"],
                  "只给 url：就一个源")
        rep.check(vc.component_urls({"url": "A", "urls": ["A", "B"]}) == ["A", "B"],
                  "urls 里重复的那个去掉（同一个源不重复下）")
        rep.check(vc.component_urls({"url": "B", "urls": ["A"]}) == ["A", "B"],
                  "**urls 决定新客户端的偏好顺序**（可以把 url 排到后面）",
                  str(vc.component_urls({"url": "B", "urls": ["A"]})))
        rep.check(vc.component_urls({"url": "B", "urls": ["A", "C"]}) == ["A", "C", "B"],
                  "**url 被补在最后当兜底**（新客户端绝不会比老客户端更差）",
                  str(vc.component_urls({"url": "B", "urls": ["A", "C"]})))
        # 为什么这条规则要长这样：战略上要从 Gitee 逐步转到别的平台，
        # 新客户端就得能"优先试新平台"；但老客户端只认 url，所以 url 必须
        # 一直是个活着的源。把 url 补在最后，两件事同时成立。

        try:
            vc.parse_components({"version": "1.0.0", "components": {
                "ai": {"version": "1.0.0", "url": "https://example.com/a.pack",
                       "urls": "not-a-list", "sha256": "b" * 64, "size": 10}}})
            rep.check(False, "urls 不是列表 → 应抛 ValueError")
        except ValueError:
            rep.check(True, "urls 形状不对 → 抛 ValueError")

        # ⚠ "/bad.pack" 要**长度正确、内容错误**：这样它才通得过健康检查
        #   （探针只比"声明的总长度对不对"），从而真正走到下面的 sha 校验那条路。
        #   写成 b"WRONG-BYTES" 的话，长度不对会被探针直接挡掉，
        #   这一条就测不到"内容坏了要换源"了。
        httpd2, port2 = _serve(rep, {"/ai.pack": content,
                                     "/bad.pack": b"X" * len(content),
                                     "/norange.pack": content,
                                     "/flaky.pack": content,
                                     "/slow.pack": content})
        dead = "http://127.0.0.1:1/ai.pack"        # 没人听 1 端口，必然连不上
        good = "http://127.0.0.1:%d/ai.pack" % port2
        rotten = "http://127.0.0.1:%d/bad.pack" % port2
        try:
            with tempfile.TemporaryDirectory() as d2:
                # 注意这些用例的写法：**要测的那个源必须排在 urls 的第一位**，
                # 而 url 留成好的那个 —— 这正是新规则下的真实形态
                # （新客户端优先试新平台，老客户端的 url 兜底）。

                # 0) 源健康检查本身
                #
                # 为什么需要它：GitHub 从国内**连得上**（TCP/TLS 握手成功、
                # 返回 200 和正确的 Content-Length），但首字节要 47~50 秒、
                # 之后每秒只走几十 KB —— 97MB 的包读 298 秒直接超时。
                # 这种源排在前面会让使用者干等几分钟，所以真下载前先探一下。
                #
                # ⚠ 这里对死源的那个秒数只做"上界"断言：**第一次**探测会连同
                # "网络通道"（直连 → 系统代理）一起走一遍，所以会偏慢；
                # 真实运行时版本检查会先把通道定下来，之后每次探测只有几秒。
                rep.check(vc.source_ready(good) is True, "健康检查：活的源 → 通过")
                t_dead = time.time()
                rep.check(vc.source_ready(dead) is False,
                          "健康检查：连不上的源 → 不通过")
                dead_secs = time.time() - t_dead
                rep.check(dead_secs < 60,
                          "健康检查放弃死源是有上界的（不会无限等）",
                          "%.1f 秒（含首次通道选择）" % dead_secs)

                # 0b) "**有时候特别慢怎么办**" —— 不是干等，是把候选源比一遍挑最快的
                #
                # ⚠ 门槛由测试自己控制（临时改 COMFORTABLE_RATE），**不依赖本机的绝对速度**：
                #    实测这台机器上"本地 http 服务器"也只有 ≈88 KB/s，
                #    把门槛钉死在 300 KB/s 会让"快/慢"两个源根本分不开。
                _Handler.slow = {"/slow.pack"}
                _saved_rate = vc.COMFORTABLE_RATE
                try:
                    slow_url = "http://127.0.0.1:%d/slow.pack" % port2
                    # (1) 门槛抬到天上 → 第一个源永远不会"够快" → 必须去比一遍
                    vc.COMFORTABLE_RATE = 10 ** 9
                    usable, unusable, notes = vc.order_sources(
                        [slow_url, good], expect_size=len(content))
                    rep.check(bool(usable) and usable[0] == good,
                              "**第一个源偏慢 → 自动去挑更快的那个**",
                              "顺序=%s" % [u.rsplit("/", 1)[-1] for u in usable])
                    rep.check(len(usable) == 2 and not unusable,
                              "慢的那个只是被排到后面，**没被丢掉**")
                    # (2) 门槛降到地上 → 第一个源一定"够快" → 不该去打扰其它源
                    vc.COMFORTABLE_RATE = 1
                    u2, un2, n2 = vc.order_sources([good, slow_url],
                                                   expect_size=len(content))
                    rep.check(u2[:2] == [good, slow_url],
                              "第一个源够快 → 直接用，不去打扰其它源（省时间）",
                              "顺序=%s" % [u.rsplit("/", 1)[-1] for u in u2])
                    rep.check(len(n2) == 1, "够快时探测说明只有一条（没白探）",
                              str(n2))
                finally:
                    _Handler.slow = set()
                    vc.COMFORTABLE_RATE = _saved_rate

                # 0c) 使用者的两条要求（2026-09-24 改为实测数据支持的版本）：
                #     ① **谁快谁前**（纯按速度）；② **Gitee 一律放最后当兜底腿**。
                #     （原来的"优先 GitHub"已被 2026-09-24 实测推翻：GitHub 直连
                #      是全场最慢的 50 KB/s，镜像有 1000~1500 KB/s。）
                def _pick(rate_gitee, rate_mirror, rate_github=50 * 1024):
                    """直接喂速度给排序规则（不碰网络，纯逻辑）。"""
                    urls = ["https://gitee.com/x/S.exe",
                            "https://gh.xxooo.cf/x/S.exe",
                            "https://github.com/x/S.exe"]
                    measured = {urls[0]: (True, rate_gitee),
                                urls[1]: (True, rate_mirror),
                                urls[2]: (True, rate_github)}
                    usable = sorted(urls, key=lambda u: -measured[u][1])
                    return vc._prefer_preferred(usable, measured)

                # Gitee 就算**最快**，也要被挪到最后（当兜底腿）
                got = _pick(2000 * 1024, 1300 * 1024)
                rep.check(got[-1].split("/")[2] == "gitee.com",
                          "**Gitee 就算最快也排最后**（当兜底腿，别全押在它身上）",
                          "顺序=%s" % [u.split("/")[2] for u in got])
                # 最快的那个（镜像）必须排第一 —— 谁快谁前
                rep.check(got[0].split("/")[2] == "gh.xxooo.cf",
                          "**谁快谁前**：最快的镜像排第一",
                          got[0].split("/")[2])
                # GitHub 不再受任何优待：最慢就该排最后（Gitee 之外的倒数第一）
                rep.check(got[-2].split("/")[2] == "github.com",
                          "GitHub 不再被优待（实测最慢 → 排在 Gitee 之前垫底）",
                          "顺序=%s" % [u.split("/")[2] for u in got])
                # Gitee 慢的时候，它当然还是在最后
                got = _pick(100 * 1024, 1300 * 1024)
                rep.check(got[-1].split("/")[2] == "gitee.com",
                          "Gitee 慢时也在最后（降级不受速度影响）",
                          "顺序=%s" % [u.split("/")[2] for u in got])
                # 只有 Gitee 一个源时，降级规则不能把它弄没了
                got = vc._prefer_preferred(["https://gitee.com/x/S.exe"],
                                           {"https://gitee.com/x/S.exe": (True, 1)})
                rep.check(len(got) == 1 and got[0].split("/")[2] == "gitee.com",
                          "只有一个源(Gitee)时，降级规则不捣乱（仍能下）")

                # 1) 排第一的源连不上 → 必须自动换第二个
                info = {"version": "1.0.0", "url": good, "urls": [dead, good],
                        "sha256": digest, "size": len(content)}
                ok, path = vc.download_component("ai", info, dest_dir=d2)
                rep.check(ok and os.path.isfile(path),
                          "**第一个源连不上 → 自动用第二个源下成**",
                          os.path.basename(path))
                with open(path, "rb") as fh:
                    rep.check(_sha(fh.read()) == digest, "换源下到的文件 sha256 一致")

                # 2) 排第一的源能连、但给的是坏内容（sha 不符）→ 也要换下一个
                info2 = {"version": "1.0.0", "url": good, "urls": [rotten, good],
                         "sha256": digest, "size": len(content)}
                ok2, path2 = vc.download_component("ai", info2, dest_dir=d2)
                rep.check(ok2 and os.path.isfile(path2),
                          "**第一个源内容对不上（sha 不符）→ 也换下一个**",
                          os.path.basename(path2 or ""))

                # 3) 所有源都挂 → 失败信息里得点名"是哪些源挂的"
                info3 = {"version": "1.0.0", "url": dead,
                         "urls": ["http://127.0.0.1:2/b.pack", dead],
                         "sha256": digest, "size": len(content)}
                ok3, why3 = vc.download_component("ai", info3, dest_dir=d2)
                rep.check(ok3 is False and "所有下载源" in why3,
                          "全部源都挂 → 明确说\"所有下载源都失败了\"",
                          why3.replace("\n", " ")[:70])
                rep.check(why3.count("127.0.0.1") >= 2,
                          "失败信息逐条列出每个源不能用的原因",
                          why3.replace("\n", " ")[:90])

                # 4) 全都连不上时，不该留下半截文件（连都没连上，哪来的半截）
                #    ⚠ 注意：如果是"连上了、下了一半才失败"，现在是**故意保留**
                #    .part 给续传用的（见测试 5/7），别把这条当成"失败就该清干净"。
                rep.check(not os.path.exists(os.path.join(d2, "haavik_ai.pack.part")),
                          "全都连不上 → 不会留下半截 .part")

                # ---- 以下是"从 GitHub 下到一半断掉"这件事的正解：断点续传 ----

                # 5) 已经落了半截 → 必须接着下完，而不是从 0 重来
                with tempfile.TemporaryDirectory() as d3:
                    with open(os.path.join(d3, "haavik_ai.pack.part"), "wb") as fh:
                        fh.write(content[: len(content) // 2])
                    ok5, why5 = vc.download_component(
                        "ai", {"version": "1.0.0", "url": good,
                               "sha256": digest, "size": len(content)}, dest_dir=d3)
                    rep.check(ok5, "**有半截 .part → 接着下完（不是从 0 重来）**",
                              str(why5)[-40:])
                    with open(os.path.join(d3, "haavik_ai.pack"), "rb") as fh:
                        blob = fh.read()
                    rep.check(_sha(blob) == digest and len(blob) == len(content),
                              "续传拼出来的文件完全正确（sha256 一致）")

                # 6) 服务器**不认** Range（回 200 全量）→ 必须清空重来。
                #    这一条守的是"拼出坏文件"：往里追加会得到"前半截旧 + 完整新"，
                #    而且体积很可能正好等于期望值，光看大小发现不了。
                _Handler.no_range = {"/norange.pack"}
                try:
                    with tempfile.TemporaryDirectory() as d4:
                        with open(os.path.join(d4, "haavik_ai.pack.part"), "wb") as fh:
                            fh.write(b"X" * (len(content) // 2))
                        ok6, why6 = vc.download_component(
                            "ai", {"version": "1.0.0",
                                   "url": "http://127.0.0.1:%d/norange.pack" % port2,
                                   "sha256": digest, "size": len(content)},
                            dest_dir=d4)
                        rep.check(ok6, "服务器不认 Range → 自动从头下成", str(why6)[-40:])
                        with open(os.path.join(d4, "haavik_ai.pack"), "rb") as fh:
                            rep.check(_sha(fh.read()) == digest,
                                      "**从头下出来的文件是对的（没把新数据拼到半截后面）**")
                finally:
                    _Handler.no_range = set()

                # 7) 传到一半掉线（服务器前两次只发一半就掐断）→ 自动续传下完。
                #    真实世界里 GitHub 就是这样：连接的住，但传着传着就断。
                _Handler.flaky = {"/flaky.pack": 0}
                _Handler.flaky_drops = 2
                try:
                    with tempfile.TemporaryDirectory() as d5:
                        ok7, why7 = vc.download_component(
                            "ai", {"version": "1.0.0",
                                   "url": "http://127.0.0.1:%d/flaky.pack" % port2,
                                   "sha256": digest, "size": len(content)},
                            dest_dir=d5)
                        rep.check(ok7, "**中途掉线 → 自动重试 + 续传，最后下完了**",
                                  str(why7)[-60:])
                        with open(os.path.join(d5, "haavik_ai.pack"), "rb") as fh:
                            rep.check(_sha(fh.read()) == digest,
                                      "掉线续传出来的文件 sha256 正确")
                finally:
                    _Handler.flaky = {}
        finally:
            httpd2.shutdown()

        # ============================================================
        rep.head("B3. 慢的时候，至少让使用者知道还要等多久")
        # ============================================================
        # 使用者的追问："**但要是有的时候特别慢怎么办呢？**"
        # 一半答案是"换个快的源"（见 B2 的 order_sources），
        # 另一半是"让他心里有数" —— 光有一条爬得极慢的进度条，
        # 人只会以为程序卡死了。所以这里把速度/剩余时间也算出来。
        note, prev = vc.describe_rate(None, 0, 1000, now=1000.0)
        rep.check(note == "" and prev == (1000.0, 0),
                  "第一次先起算、不给数字（免得乱跳）", repr(prev))
        note, prev2 = vc.describe_rate(prev, 100, 1000, now=1000.5)
        rep.check(note == "" and prev2 == prev,
                  "间隔太短不算（窗口 %.1f 秒）" % vc.RATE_WINDOW)
        # 2 秒下了 2 MB、总量 10 MB → 1 MB/s，还剩 8 MB → 约 8 秒
        note, _ = vc.describe_rate((1000.0, 0), 2 * 1024 * 1024, 10 * 1024 * 1024,
                                   now=1002.0)
        rep.check("1024 KB/s" in note and "还需约 8 秒" in note,
                  "速度和剩余时间都算出来了", note)
        note, _ = vc.describe_rate((1000.0, 5 * 1024 * 1024), 5 * 1024 * 1024,
                                   10 * 1024 * 1024, now=1003.0)
        rep.check("卡住了" in note,
                  "**一整段一个字节没动 → 如实说卡住了**（别假装还在动）", note)
        note, prev3 = vc.describe_rate((1000.0, 500), 100, 2000, now=1005.0)
        rep.check(note == "" and prev3 == (1005.0, 100),
                  "进度倒退（换源从头下 / 续传重置）→ 重新起算，不报错")
        note, _ = vc.describe_rate((1000.0, 0), 10, 0, now=1010.0)
        rep.check(note == "", "总量为 0（清单没给大小）→ 不报也不崩")
        rep.check(vc._human_secs(45) == "45 秒"
                  and vc._human_secs(605) == "10 分 5 秒"
                  and vc._human_secs(3661) == "1 小时 1 分",
                  "时间换算对", "%s / %s / %s"
                  % (vc._human_secs(45), vc._human_secs(605),
                     vc._human_secs(3661)))

        # ============================================================
        rep.head("B4. 中途换源：开头够快、之后持续慢 → 换下一个源续传")
        # ============================================================
        # 为什么需要这个（2026-09-23）：选源只看了 256KB 短样本，会挑到
        # "开局够快、长跑极差"的源 —— 健康检查通过 + 开头几秒也够快 +
        # 然后掉到 30 KB/s 持续 10 秒以上，必须放弃、半截 .part 留着、
        # 让下一个源 Range 续传。
        #
        # ⚠ 测试里把阈值收紧（3 秒 / 32 KB / 1 秒一采）：慢源真下完 4MB 慢模式
        # 部分要 100 秒，太慢；线上保持默认（10 秒 / 64 KB / 2 秒一采）。
        big_content = b"Y" * (4 * 1024 * 1024)  # 4 MB
        big_digest = _sha(big_content)

        httpd3, port3 = _serve(rep, {
            "/fast.pack": big_content,
            "/slow_after.pack": big_content,   # 同一个内容，前 64KB 快、之后 40 KB/s
        })
        slow_then_url = "http://127.0.0.1:%d/slow_after.pack" % port3
        fast_url = "http://127.0.0.1:%d/fast.pack" % port3
        # 让慢源"前 64 KB 快"——能过健康检查 + SLOW_MIN_BYTES，之后开始 40 KB/s
        _Handler.slow_after = {"/slow_after.pack": 64 * 1024}

        _saved_thr = vc.SLOW_THRESHOLD_BPS
        _saved_pat = vc.SLOW_PATIENCE_SECONDS
        _saved_min = vc.SLOW_MIN_BYTES
        _saved_check = vc.SLOW_CHECK_EVERY
        with tempfile.TemporaryDirectory() as d4:
            try:
                vc.SLOW_PATIENCE_SECONDS = 3.0
                vc.SLOW_MIN_BYTES = 32 * 1024
                vc.SLOW_CHECK_EVERY = 1.0
                # SLOW_THRESHOLD_BPS 保持 100 KB/s 不变 —— 慢源 40 KB/s < 100 KB/s
                # 一定会触发；只有正常速度（≥ 100 KB/s）才不会误触。

                info = {"version": "1.0.0", "url": fast_url,
                        "urls": [slow_then_url, fast_url],   # 慢源优先 → 触发换源
                        "sha256": big_digest, "size": len(big_content)}
                ok, path = vc.download_component("ai", info, dest_dir=d4)
                rep.check(ok and os.path.isfile(path),
                          "**中途掉链的源 → 自动换下一个源续传**",
                          str(path) if ok else "FAILED")
                if ok and path:
                    with open(path, "rb") as fh:
                        blob = fh.read()
                    rep.check(_sha(blob) == big_digest and len(blob) == len(big_content),
                              "**续传拼出来的文件完全正确**（没把坏字节拼进去）",
                              "size=%d expected=%d" % (len(blob), len(big_content)))
            finally:
                _Handler.slow_after = {}
                vc.SLOW_THRESHOLD_BPS = _saved_thr
                vc.SLOW_PATIENCE_SECONDS = _saved_pat
                vc.SLOW_MIN_BYTES = _saved_min
                vc.SLOW_CHECK_EVERY = _saved_check
        httpd3.shutdown()

        # ============================================================
        rep.head("C. prune_updates 的组件意识")
        # ============================================================
        old_local = os.environ.get("LOCALAPPDATA")
        with tempfile.TemporaryDirectory() as upd:
            os.environ["LOCALAPPDATA"] = upd
            ud = vc.update_dir()                     # 指向临时目录
            os.makedirs(ud, exist_ok=True)
            # 旧版安装包（该清）、新版安装包（留着）、已装 AI 包（留着）、
            # **过期的**半截包（清）、**新鲜的**半截包（留着续传）
            with open(os.path.join(ud, "Setup-1.0.0.exe"), "wb") as fh:
                fh.write(b"old")
            with open(os.path.join(ud, "Setup-9.9.10.exe"), "wb") as fh:
                fh.write(b"new")
            with open(os.path.join(ud, "haavik_ai.pack"), "wb") as fh:
                fh.write(b"ai-installed")
            stale = os.path.join(ud, "haavik_ai.pack.part")
            with open(stale, "wb") as fh:
                fh.write(b"half")
            old_time = time.time() - vc.PART_KEEP_SECONDS - 3600
            os.utime(stale, (old_time, old_time))          # 装成好多天前的
            fresh = os.path.join(ud, "Setup-9.9.9.exe.part")
            with open(fresh, "wb") as fh:
                fh.write(b"half-fresh")
            removed = vc.prune_updates(current_version="9.9.9")
            rep.check("Setup-1.0.0.exe" in removed, "旧版 Setup 被清", str(removed))
            rep.check("Setup-9.9.10.exe" not in removed, "比当前新的 Setup 保留")
            rep.check("haavik_ai.pack.part" in removed, "**过期**的半截包被清")
            rep.check(os.path.isfile(os.path.join(ud, "haavik_ai.pack")),
                      "已装组件包 haavik_ai.pack 不动")
            rep.check(not os.path.exists(stale), "过期半截包已从磁盘消失")
            rep.check(os.path.isfile(fresh),
                      "**新鲜的半截包留着续传**（清了它，断点续传就废了）")
            rep.check("Setup-9.9.9.exe.part" not in removed,
                      "新鲜半截包不在删除名单里")
        if old_local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = old_local

    except Exception as exc:                               # noqa: BLE001
        rep.check(False, "测试过程中抛异常：%s" % exc)

    out_path = os.path.join(ROOT, "_component_update.txt")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(rep.text())
    _redact.scrub_file(out_path)          # 生成物落盘前脱敏（本机路径 + 令牌）
    sys.stderr.write(_redact.scrub(rep.text()))
    return 1 if rep.failures else 0


if __name__ == "__main__":
    sys.exit(main())
