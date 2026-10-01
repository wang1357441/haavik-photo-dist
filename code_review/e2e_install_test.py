# -*- coding: utf-8 -*-
"""端到端安装机制测试（不依赖 GUI 点击）。

做的事：把 Setup.exe 内部会执行的那套流程完整跑一遍，但输出到临时目录，
不碰桌面、不碰 LOCALAPPDATA，跑完自清理。

验证点：
  1. 容器能按架构取出正确的载荷
  2. 解出的字节与 dist/ 中对应主程序**完全相同**（SHA-256）
  3. 原子写入正常，无 .part 残留
  4. **单 EXE 安装**：容器里没有附带资源，安装目录里除了那一个 exe 什么都不多
  5. 主程序能被系统识别为有效 PE，且位数与目标架构匹配
"""
from __future__ import annotations

import hashlib
import os
import shutil
import struct
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "optimized"))

ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "code_review", "optimized"))

import installer  # noqa: E402


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pe_bits(path):
    with open(path, "rb") as fh:
        data = fh.read(0x400)
    if data[:2] != b"MZ":
        return None
    off = struct.unpack_from("<I", data, 0x3C)[0]
    if data[off:off + 4] != b"PE\x00\x00":
        return None
    m = struct.unpack_from("<H", data, off + 4)[0]
    return {0x014C: 32, 0x8664: 64}.get(m)


def main():
    bundle_path = os.path.join(ROOT, "payload.zip")
    out = []
    failures = []

    out.append("容器: %s" % bundle_path)
    bundle = installer.Bundle(bundle_path)
    out.append("meta: format=%s  product=%s  created=%s"
               % (bundle.meta.get("format"), bundle.meta.get("product"), bundle.meta.get("created")))
    out.append("")

    tmp = tempfile.mkdtemp(prefix="haavik_e2e_")
    out.append("临时安装目录: %s" % tmp)
    try:
        expect = {"x64": ("MyAlbum.exe", 64), "x86": ("MyAlbum_x86.exe", 32)}

        out.append("")
        out.append("--- 1/3 载荷解出与写入 ---")
        for arch, (filename, want_bits) in expect.items():
            data, size, crc = bundle.payload(arch)
            target = os.path.join(tmp, filename)
            written = installer.write_payload(target, data)

            disk_hash = sha256_file(target)
            ref_hash = sha256_file(os.path.join(ROOT, "dist", filename))
            bits = pe_bits(target)
            leftovers = [n for n in os.listdir(tmp) if n.endswith(".part")]

            ok_hash = disk_hash == ref_hash
            ok_size = written == size
            ok_bits = bits == want_bits
            ok_atomic = not leftovers
            if not (ok_hash and ok_size and ok_bits and ok_atomic):
                failures.append("%s 校验失败" % arch)

            out.append("  %-4s %-16s %10s B  crc=%s" % (arch, filename, "{:,}".format(written), crc))
            out.append("       与 dist 哈希一致 : %s" % ("OK" if ok_hash else "FAIL"))
            out.append("       写入长度一致     : %s" % ("OK" if ok_size else "FAIL"))
            out.append("       PE 位数=%s (期望 %s): %s" % (bits, want_bits, "OK" if ok_bits else "FAIL"))
            out.append("       无 .part 残留    : %s" % ("OK" if ok_atomic else "FAIL"))

        # ---- 写入回读校验 ----
        # 守的是"装完了却打不开、界面上还看不出原因"那种情况：360/火绒这类
        # 主动防御有可能在我们落盘之后改动甚至隔离那个 exe。所以写完之后
        # 必须把字节读回来对一遍 CRC32；而且**校验没过时绝不能把好文件顶掉**。
        out.append("")
        out.append("--- 1.5 写入回读校验 ---")
        # 单独开一个临时目录：安装目录本身有"只许出现预期的那几个 exe"的断言，
        # 探针文件放进去会把它弄红。
        probe_dir = tempfile.mkdtemp(prefix="haavik_crc_")
        try:
            probe = os.path.join(probe_dir, "probe.exe")
            blob = b"MZ" + b"\x00" * 4094
            installer.write_payload(probe, blob)
            ok_crc = installer.crc32_file(probe) == (zlib.crc32(blob) & 0xFFFFFFFF)
            out.append("  写入后 crc32 与源一致     : %s"
                       % ("OK" if ok_crc else "FAIL"))
            if not ok_crc:
                failures.append("write_payload 写入后 crc32 不一致")

            real_crc = installer.crc32_file
            try:
                installer.crc32_file = lambda _path: 0xDEADBEEF  # 假装读回来是坏的
                raised = False
                try:
                    installer.write_payload(probe, blob + b"x")
                except RuntimeError as exc:
                    raised = ("不一致" in str(exc)) or ("预期" in str(exc))
                with open(probe, "rb") as fh:
                    kept = fh.read() == blob
                no_part = not [n for n in os.listdir(probe_dir)
                               if n.endswith(".part")]
                out.append("  能发现坏写入并明确报错   : %s"
                           % ("OK" if raised else "FAIL"))
                out.append("  校验失败时旧文件没被顶掉 : %s"
                           % ("OK" if kept else "FAIL"))
                out.append("  校验失败后无 .part 残留  : %s"
                           % ("OK" if no_part else "FAIL"))
                if not raised:
                    failures.append("write_payload 没能发现写入损坏")
                if not kept:
                    failures.append("write_payload 校验失败时把旧文件顶掉了")
                if not no_part:
                    failures.append("write_payload 校验失败后留下了 .part")
            finally:
                installer.crc32_file = real_crc
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)

        # 目标被占用（正在运行）时，安装器必须能识别出来，好给出可读的提示，
        # 而不是甩给用户一句裸的 PermissionError。
        # 拿解释器自己当样本：Windows 上正在运行的 exe 就是无法以写入方式打开。
        running_locked = installer._is_locked(sys.executable)
        missing_free = installer._is_locked(os.path.join(tmp, "不存在.exe"))
        out.append("")
        out.append("  _is_locked(正在运行的程序) = %s（期望 True）" % running_locked)
        out.append("  _is_locked(不存在的文件)   = %s（期望 False）" % missing_free)
        if not running_locked:
            failures.append("_is_locked 没能识别出正在运行的可执行文件")
        if missing_free:
            failures.append("_is_locked 对不存在的文件误判为被占用")

        # ---- 装完就地自检，自检不过就回滚 ----
        # 守的是 v1.6.0 那次事故：包能下、能校验、能装，界面一路打勾，
        # **只有新程序一双击就 ModuleNotFoundError**（构建时排除了它要用的模块），
        # 而这边审计/签名/哈希/安装链路全是绿的 —— 结果是朋友桌上的程序坏了。
        # 这一段**真的把刚装的 exe 跑一遍**，不是只检查函数存在：
        # "构建了但没人调用"的代码，测试里只构建就等于没测（2026-09-16 的教训）。
        out.append("")
        out.append("--- 1.8 装完自检与回滚 ---")
        rb = tempfile.mkdtemp(prefix="haavik_rb_")
        try:
            # 备份必须落在 %LOCALAPPDATA% 下，不能搁在目标目录旁边 ——
            # 目标目录默认就是桌面，往那儿丢个 .bak 会在用户眼前留不明文件。
            where = installer.backup_path_for(os.path.join(tmp, "MyAlbum.exe"))
            localapp = os.environ.get("LOCALAPPDATA", "")
            ok_where = bool(localapp) and os.path.abspath(where).startswith(
                os.path.abspath(localapp))
            out.append("  备份落在 %%LOCALAPPDATA%% 下 : %s" % ("OK" if ok_where else "FAIL"))
            out.append("        %s" % where)
            if not ok_where:
                failures.append("备份位置不在 LOCALAPPDATA 下（会污染目标目录）")

            good = os.path.join(rb, "MyAlbum.exe")
            with open(good, "wb") as fh:
                fh.write(b"MZ" + b"\x00" * 1024)
            with open(good, "rb") as fh:
                gold = fh.read()

            # 第一次安装（没有旧版本）：备份返回空串，且不报错
            empty = installer.backup_exe(os.path.join(rb, "不存在.exe"))
            out.append("  没有旧版本时备份返回空串 : %s" % ("OK" if empty == "" else "FAIL"))
            if empty != "":
                failures.append("没有旧版本时 backup_exe 应返回空串")

            # 覆盖装：先备份 -> 写坏 -> 回滚，字节必须和原来一模一样
            real_backup_path = installer.backup_path_for
            installer.backup_path_for = lambda _t: os.path.join(rb, "backup.bak")
            try:
                saved = installer.backup_exe(good)
                with open(good, "wb") as fh:
                    fh.write(b"BROKEN-INSTALL")
                msg = installer.restore_exe(good, saved)
                with open(good, "rb") as fh:
                    restored = fh.read() == gold
                out.append("  自检不过后回滚到上一版   : %s" % ("OK" if restored else "FAIL"))
                out.append("       %s" % msg.strip().splitlines()[0])
                if not restored:
                    failures.append("restore_exe 没能还原上一版")
                if not saved or not os.path.isfile(saved):
                    failures.append("backup_exe 没有真的存下备份")
            finally:
                installer.backup_path_for = real_backup_path

            # 第一次安装、没有备份可退：装坏的那个必须删掉，
            # 不能在目标位置留一个双击打不开、还不知道是什么的图标
            orphan = os.path.join(rb, "orphan.exe")
            with open(orphan, "wb") as fh:
                fh.write(b"BROKEN-INSTALL")
            installer.restore_exe(orphan, "")
            gone = not os.path.exists(orphan)
            out.append("  无备份时删掉装坏的文件   : %s" % ("OK" if gone else "FAIL"))
            if not gone:
                failures.append("没有备份时 restore_exe 留下了打不开的文件")

            # 真的把打好的 exe 跑一遍 --selfcheck
            real_exe = os.path.join(ROOT, "dist", "MyAlbum.exe")
            ok_run, why = installer.selfcheck_installed(real_exe)
            out.append("  真跑 dist/MyAlbum.exe    : %s %s"
                       % ("OK" if ok_run else "FAIL", (why or "")[:60]))
            if not ok_run:
                failures.append("打好的 exe 自检没通过：%s" % why)

            # 坏掉的 exe 必须判不通过，而且要说出原因（不能只是返回 False）
            broken = os.path.join(rb, "broken.exe")
            with open(broken, "wb") as fh:
                fh.write(b"not an executable at all")
            ok_bad, why_bad = installer.selfcheck_installed(broken)
            out.append("  坏 exe 被判不通过并说明  : %s"
                       % ("OK" if (not ok_bad and why_bad) else "FAIL"))
            if ok_bad or not why_bad:
                failures.append("selfcheck_installed 没能识别出坏掉的 exe")
        finally:
            shutil.rmtree(rb, ignore_errors=True)

        out.append("")
        out.append("--- 2/3 单 EXE 安装 ---")
        resources = bundle.resources()
        declared = bundle.meta.get("resources") or {}
        out.append("  容器 meta 声明的资源项 : %d" % len(declared))
        out.append("  容器里实际可读出的资源 : %d" % len(resources))
        skipped = installer.report_skipped_resources(resources)
        out.append("  按单文件安装跳过的资源 : %d" % skipped)
        if len(resources) != len(declared):
            failures.append("容器资源索引与内容不一致：%d/%d" % (len(resources), len(declared)))

        # 这是"单 EXE"这条需求的直接断言：安装目录里除了那一个 exe，不该多出任何东西
        produced = sorted(os.listdir(tmp))
        expected_only = sorted(expect[a][0] for a in expect)
        out.append("  安装目录内容           : %s" % ", ".join(produced))
        if produced != expected_only:
            failures.append("安装目录里出现了预期外的文件：%s"
                            % sorted(set(produced) - set(expected_only)))
        if os.path.isdir(os.path.join(tmp, "Resources")):
            failures.append("仍然生成了 Resources/ 目录（单 EXE 安装不应有）")
        out.append("  只有 %d 个 exe、无 Resources/ 目录 : %s"
                   % (len(produced), "OK" if produced == expected_only else "FAIL"))

        out.append("")
        out.append("--- 3/3 架构决策 ---")
        os_arch = installer.detect_arch()
        proc_bits = 64 if sys.maxsize > 2 ** 32 else 32
        out.append("  安装器自身位数          = %s" % installer.describe_self())
        out.append("  本测试脚本所在进程位数  = %s" % proc_bits)
        out.append("  detect_arch() 判定系统  = %s" % os_arch)
        out.append("  （脚本跑在 32 位 Python 上时，进程自报 32 位，但 detect_arch 仍应")
        out.append("    正确识别 64 位系统 —— 这正是 S-02 缺陷被修复的直接体现）")

        out.append("")
        out.append("总磁盘占用: %.2f MB" % (
            sum(os.path.getsize(os.path.join(dp, f))
                for dp, _, fs in os.walk(tmp) for f in fs) / 1024 / 1024))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        out.append("临时目录已清理: %s" % (not os.path.exists(tmp)))

    out.append("")
    out.append("结论：" + ("端到端安装机制全部通过 ✓" if not failures else "失败项: %s" % failures))
    report = "\n".join(out)
    # 输出写到 code_review/ 而不是仓库根目录 —— 根目录只留 README/CHANGELOG/.gitignore
    with open(os.path.join(HERE, "_e2e.txt"), "w", encoding="utf-8") as fh:
        fh.write(report)
    # 生成物要进仓库：本机绝对路径一律换成占位符（硬性约定 12）
    try:
        import _redact
        _redact.scrub_tree(HERE)
    except ImportError:
        pass
    print(report)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
