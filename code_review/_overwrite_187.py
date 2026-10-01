# -*- coding: utf-8 -*-
"""把新打好的 Setup.exe 覆盖上传到 GitHub 分发仓的 v1.8.7 发行版。

为什么需要它
==================================================================
用户要求「覆盖原来发布的那个 1.8.7」。经查：Gitee 分发仓没有 v1.8.7 发行版
（它的清单只写 version=1.8.7、url 指向 GitHub）；**真正的 v1.8.7 发行版在
GitHub 分发仓 wang1357441/haavik-photo-dist**，挂着 1.8.7 那版（没有绿色按钮的）
Setup.exe。

所以「覆盖 1.8.7」= 把 GitHub 上 v1.8.7 那个发行版的 Setup.exe 换成新打好的
（带错误报告一键发信的）安装器。这样：
  * 点「检查更新」的人 → 读清单（已发成 1.8.11）→ 下到 v1.8.11（有绿色按钮）；
  * 直接点 v1.8.7 发行版链接下载的人 → 拿到被覆盖成的新安装器（也有绿色按钮）。
两条入口都拿到报告功能。

安全设计
==================================================================
* 默认只做只读检查（打印 v1.8.7 发行版及其附件），不删不改。
* 加 ``--do`` 才真正：先删 v1.8.7 上已有的 Setup.exe 附件，再传新的。
* GitHub 不允许同名附件，所以必须先删旧的再传新的（release.upload_asset 内的
  ``_delete_release_asset`` 重试时也会再删一次）。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import release  # noqa: E402

DIST = release.DIST_REPO
TAG = "v1.8.7"


def main():
    do = "--do" in sys.argv
    token = release.get_token()
    rel = release.api(token, "https://api.github.com/repos/%s/releases/tags/%s"
                      % (DIST, TAG))
    if not isinstance(rel, dict) or not rel.get("id"):
        raise SystemExit("GitHub 上找不到发行版 %s —— 没法覆盖。" % TAG)
    rid = rel["id"]
    assets = rel.get("assets") or []
    print("[只读] 发行版 %s id=%s" % (TAG, rid))
    for a in assets:
        print("    附件: %s  %s 字节" % (a.get("name"), a.get("size")))
    if not do:
        print("\n（未加 --do，未做任何改动。确认无误后加 --do 再跑。）")
        return

    exe = os.path.join(release.ROOT, "dist", "Setup.exe")
    if not os.path.isfile(exe):
        raise SystemExit("找不到 %s，先构建。" % exe)

    # 删掉 v1.8.7 上已有的 Setup.exe（GitHub 同名附件直接 422 拒绝）
    for a in assets:
        if a.get("name") == "Setup.exe":
            try:
                release.api(token,
                            "https://api.github.com/repos/%s/releases/assets/%s"
                            % (DIST, a["id"]), method="DELETE")
                print("  已删除 v1.8.7 上的旧 Setup.exe 附件 id=%s" % a["id"])
            except Exception as exc:  # noqa: BLE001
                print("  删除旧附件出错（继续尝试上传）：%s" % exc)

    release.upload_asset(
        token,
        "https://uploads.github.com/repos/%s/releases/%s/assets?name=Setup.exe"
        % (DIST, rid), exe, timeout=3600)
    print("  已覆盖上传新 Setup.exe 到 %s 发行版" % TAG)


if __name__ == "__main__":
    main()
