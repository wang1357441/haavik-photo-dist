# -*- coding: utf-8 -*-
"""把双架构主程序打包成 ``payload.zip``（安装器运行时读取的载荷容器）。

为什么是 zip 而不是"把 base64 写进 setup.py"
==================================================================
原方案把 3600 万个 base64 字符塞成 setup.py 里的字符串常量：

* setup.py 变成 56 MB，任何编辑器/IDE 打开即卡死，代码审查无从谈起；
* 安装器启动时要在内存里 load 一个几十 MB 的字符串常量，
  在 **32 位**进程里既慢又吃地址空间 —— 而这正是"卡顿"的主因；
* 注入脚本就地改写这个 56 MB 文件，跑第二次时占位符已不存在，
  只会打一行 WARN 然后把文件原样重写：既不可重复执行，又破坏了模板。

改成 zip 之后：meta.json 只有几百字节，用到哪个载荷才读哪个，
单次读取上限 40 MB，32 位进程也毫无压力。

容器里现在只装两个架构的主程序
==================================================================
1.1.0 及以前还会装一批"伪装资源"（许可协议、示例壁纸、伪造二进制），
安装时写进安装目录的 ``Resources/`` 里充门面。现在不装了：

* 主程序本身已经是**真能用**的图片工具，不需要布景；
* 用户要求"安装结果就是一个 EXE"，多一个资源目录就多一层不便；
* 那些字节没有任何代码读取，留着只是给审计报告添条目。

``resources`` 字段仍然保留在 meta.json 里（当前恒为空对象），
所以容器格式没变，verify_payload.py 的"容器索引与 meta 完全对应"那条
断言也照旧成立 —— 空集合也是个集合。

相对 inject_payload_v2.py / inject_payload.py 的其他改动
==================================================================
1. **非破坏性**：只生成新文件，源文件永不改动，可任意重复执行。
2. **失败即报错**：载荷缺失时直接退出。原实现在资源缺失时 ``continue``，
   结果是安装器把 ``b"license_en_PLACEHOLDER"`` 这种占位串当成真实资源
   写到用户磁盘上（静默损坏，且要到用户机器上才暴露）。
3. **带校验**：原始长度 + CRC32 一起写进 meta.json，安装器写出前逐项核对。
4. **合并了 v1/v2 两个功能重叠的脚本**（v1 早已被 v2 完全取代，
   却仍留在仓库里并被 build_all.bat 调用 —— 典型的双实现漂移）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import payload_codec as codec  # noqa: E402
import version_check           # noqa: E402  版本号唯一来源

#: 产品标识，写进 payload.zip 的 meta.json，便于事后确认安装包来源。
#: 版本号直接取唯一来源，不在这里再抄一份 ——
#: 曾经这里写死 "1.0.0"，改版本时没跟着改，于是容器 meta 里写着
#: "HAAVIK Photo 1.0.0" 而 exe 已经是 1.1.0，同一份东西两个说法。
PRODUCT_NAME = "HAAVIK Photo"
PRODUCT_VERSION = version_check.APP_VERSION

TARGET_ARCHES = (("x64", "MyAlbum.exe"), ("x86", "MyAlbum_x86.exe"))
BUNDLE_FORMAT = 1


def collect_payloads(root: str):
    """读取 dist/ 下两个架构的主程序，混淆后返回索引与数据。"""
    index = {}
    blobs = {}
    dist = os.path.join(root, "dist")

    for arch, filename in TARGET_ARCHES:
        path = os.path.join(dist, filename)
        if not os.path.isfile(path):
            raise SystemExit(
                "[ERROR] 找不到 %s\n        请先用 PyInstaller 构建对应架构的主程序。" % path
            )
        with open(path, "rb") as fh:
            data = fh.read()

        if not data.startswith(codec.PE_MAGIC):
            raise SystemExit("[ERROR] %s 不是有效的 PE 文件，构建产物可能已损坏。" % filename)

        blobs["payload/%s.bin" % arch] = codec.obfuscate(data)
        index[arch] = {
            "member": "payload/%s.bin" % arch,
            "size": len(data),
            "crc32": codec.crc32(data),
            "obfuscated": True,
            "source": filename,
        }
        print(
            "      %-4s %-16s %10s B  crc=%s"
            % (arch, filename, "{:,}".format(len(data)), index[arch]["crc32"])
        )
    return index, blobs


def collect_resources():
    """当前版本不往容器里放任何附带资源。

    返回一个空索引 —— 保留这个函数（而不是把它删掉）是为了让
    ``meta.json`` 的 ``resources`` 字段在所有版本里都存在，
    容器结构不随版本变来变去。verify_payload.py 的对照断言依赖它。
    """
    return {}, {}


def build_bundle(root: str, out_path: str) -> str:
    print("[1/3] 收集主程序 ...")
    payload_index, blobs = collect_payloads(root)

    print("[2/3] 附带资源（单 EXE 安装，本版本为空）...")
    res_index, res_blobs = collect_resources()
    blobs.update(res_blobs)

    print("[3/3] 写出载荷容器 ...")
    meta = {
        "format": BUNDLE_FORMAT,
        "product": "%s %s" % (PRODUCT_NAME, PRODUCT_VERSION),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "payload": payload_index,
        "resources": res_index,
        "note": "载荷经 XOR 混淆（非加密）。修改 OBFUSCATION_KEY 后必须重新生成本容器。",
    }

    tmp = out_path + ".tmp"
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # meta.json 放第一个：安装器只需读它就能拿到全部索引
        zf.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
        for member in sorted(blobs):
            # ZIP_STORED：载荷本身已是不可压缩的 deflate 流，再压一次纯属浪费 CPU
            zf.writestr(member, blobs[member], compress_type=zipfile.ZIP_STORED)
    os.replace(tmp, out_path)

    print("\n[OK] 已生成 %s" % out_path)
    print("     大小 %.2f MB" % (os.path.getsize(out_path) / 1024 / 1024))
    print("     载荷 %d 个架构，资源 %d 项" % (len(payload_index), len(res_index)))
    return out_path


def verify(root: str, bundle_path: str) -> int:
    """校验容器内容与 dist/ 是否一致（CI 用），以及索引是否自洽。"""
    sys.path.insert(0, root)
    try:
        import installer
    except ImportError as exc:
        print("[FAIL] 无法导入 installer：%s" % exc)
        return 1

    try:
        bundle = installer.Bundle(bundle_path)
    except installer.BundleError as exc:
        print("[FAIL] 容器不可读：%s" % exc)
        return 1

    failures = 0
    for arch, filename in TARGET_ARCHES:
        path = os.path.join(root, "dist", filename)
        with open(path, "rb") as fh:
            disk = fh.read()
        try:
            data, size, crc = bundle.payload(arch)
        except codec.DecodeError as exc:
            print("[FAIL] %s: %s" % (arch, exc))
            failures += 1
            continue
        same = data == disk
        if not same:
            failures += 1
        print(
            "[%s] %-4s 容器 %s B / dist %s B  一致=%s  crc=%s"
            % ("OK" if same else "FAIL", arch, "{:,}".format(size), "{:,}".format(len(disk)), same, crc)
        )

    resources = bundle.resources()
    expected = len(bundle.meta.get("resources") or {})
    print("[%s] 资源 %d/%d 项可读" % ("OK" if len(resources) == expected else "FAIL", len(resources), expected))
    if len(resources) != expected:
        failures += 1

    print("容器校验通过 ✓" if not failures else "容器校验失败：%d 项 ✗" % failures)
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 payload.zip 载荷容器")
    parser.add_argument(
        "--root",
        default=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    )
    parser.add_argument("--out", default=None, help="输出路径，默认 <root>/payload.zip")
    parser.add_argument("--verify", action="store_true", help="只校验已有容器")
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    out = args.out or os.path.join(root, "payload.zip")

    codec.self_test()  # 先证明快速 XOR 与朴素实现等价，再拿它处理真数据

    if args.verify:
        return verify(root, out)

    build_bundle(root, out)
    return verify(root, out)


if __name__ == "__main__":
    sys.exit(main())
