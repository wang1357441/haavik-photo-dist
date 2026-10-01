# -*- coding: utf-8 -*-
"""本地部署（真实执行一遍安装器做的事，供验收与重装使用）。

与 GUI 安装走**完全相同**的函数，只是跳过了点击流程：
    default_install_dir() -> Bundle.payload() -> write_payload()

部署结果（2026-09-13 起按用户要求改）：
    桌面\\MyAlbum.exe          **就这一个文件**，不建快捷方式、不生成 Resources/
    （首次运行主程序后，状态写在 %%LOCALAPPDATA%%\\HAAVIK Photo\\state.json ——
      v1.2.0 起**桌面上不会再多出任何文件**。删掉那个 state.json 就等于重新出题。）

卸载：删除桌面的 MyAlbum.exe 与 %LOCALAPPDATA%\\HAAVIK Photo\\ 里的 state.json，
不留任何系统痕迹。

⚠️ 卸载**只删这两个具名文件**，绝不递归删目录。
   理由很直白：安装目录现在默认就是**桌面**，一句 shutil.rmtree(install_dir)
   会把用户整个桌面端掉。任何"按目录递归删"的写法在这里都是事故。

用法：
    python deploy_local.py                 # 部署并验收
    python deploy_local.py --uninstall      # 卸载（只删具名文件）
    python deploy_local.py --purge-legacy   # 额外清掉 1.1.0 以前的 D:\\HAAVIK Photo 目录
"""
from __future__ import annotations

import argparse
import hashlib
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "optimized"))

import installer  # noqa: E402
import state_store  # noqa: E402

LINES = []


def say(msg=""):
    LINES.append(msg)
    print(msg, flush=True)


def sha256(path):
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
    m = struct.unpack_from("<H", data, off + 4)[0]
    return {0x014C: 32, 0x8664: 64}.get(m)


def owned_files(folder):
    """我们在 ``folder`` 里拥有的**具名文件**（卸载时的唯一依据）。

    名字全部来自各自的唯一来源常量，不在这里手抄：
    ``installer.PROGRAM_FILENAME``、``state_store.STATE_FILENAME``。
    """
    return [
        os.path.join(folder, installer.PROGRAM_FILENAME),
        os.path.join(folder, state_store.STATE_FILENAME),
    ]


def legacy_shortcut(folder=None):
    """1.1.0 及以前留下的桌面快捷方式路径（现在只用来清理，不再创建）。"""
    return os.path.join(folder or installer.get_desktop_dir(), installer.APP_NAME + ".lnk")


def legacy_dirs():
    """1.1.0 及以前的主程序安装目录（``D:\\HAAVIK Photo`` 那一套）。"""
    out = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        path = "%s:\\%s" % (letter, installer.APP_NAME)
        if os.path.isdir(path):
            out.append(path)
    return out


def _remove_file(path):
    if not os.path.isfile(path):
        return "不存在，跳过"
    try:
        os.remove(path)
    except OSError as exc:
        return "删除失败：%s" % exc
    return "已删除" if not os.path.exists(path) else "删了但还在"


def uninstall(purge_legacy=False):
    target = installer.default_install_dir()
    say("安装位置（默认即桌面）：%s" % target)
    say("")
    say("只删下面这些**具名文件**，不做任何目录递归删除：")
    for path in owned_files(target):
        say("  %s  -> %s" % (path, _remove_file(path)))

    # v1.2.0 起状态文件搬进了 %LOCALAPPDATA%\HAAVIK Photo\ —— 卸载时一并清掉
    #（同样只删具名文件；目录只在恰好为空时用 os.rmdir 移除，绝不递归）
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    state_dir = os.path.join(local, state_store.APP_DIRNAME)
    state_file = os.path.join(state_dir, state_store.STATE_FILENAME)
    say("  %s  -> %s" % (state_file, _remove_file(state_file)))
    try:
        os.rmdir(state_dir)     # 非空会抛 OSError —— 那就留着，这是有意的
        say("  %s（空目录）已移除" % state_dir)
    except OSError:
        say("  %s 保留（不存在或里面还有东西）" % state_dir)

    link = legacy_shortcut(target)
    say("")
    say("旧版本快捷方式：%s" % link)
    say("  -> %s" % _remove_file(link))

    if purge_legacy:
        say("")
        say("--purge-legacy：清理 1.1.0 以前的安装目录")
        found = legacy_dirs()
        if not found:
            say("  没有找到")
        for folder in found:
            say("  %s" % folder)
            entries = os.listdir(folder)
            # 只认我们自己的文件；出现任何陌生条目就停手，交给人看。
            known = {os.path.basename(p) for p in owned_files(folder)}
            known.add("Resources")          # 1.1.0 的伪装资源目录
            strangers = [e for e in entries if e not in known]
            if strangers:
                say("    目录里有别的东西，**跳过**（需人工确认）：%s" % strangers)
                continue
            for path in owned_files(folder):
                say("    %s -> %s" % (path, _remove_file(path)))
            res_dir = os.path.join(folder, "Resources")
            if os.path.isdir(res_dir):
                for name in os.listdir(res_dir):
                    say("    %s -> %s" % (os.path.join(res_dir, name),
                                          _remove_file(os.path.join(res_dir, name))))
                try:
                    os.rmdir(res_dir)
                    say("    Resources/ 空目录已移除")
                except OSError as exc:
                    say("    Resources/ 移除失败：%s" % exc)
            try:
                os.rmdir(folder)
                say("    空目录已移除")
            except OSError as exc:
                say("    目录非空或移除失败：%s" % exc)
    else:
        found = legacy_dirs()
        if found:
            say("")
            say("注意：检测到 1.1.0 以前的安装目录，未删除（需要时加 --purge-legacy）：")
            for folder in found:
                say("  %s" % folder)
    return 0


def main():
    parser = argparse.ArgumentParser(description="本地部署 / 卸载")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument(
        "--purge-legacy",
        action="store_true",
        help="卸载时额外清理 1.1.0 以前的 D:\\HAAVIK Photo 目录",
    )
    args = parser.parse_args()

    if args.uninstall:
        return uninstall(purge_legacy=args.purge_legacy)

    failures = []
    desktop = installer.get_desktop_dir()
    say("=" * 74)
    say("HAAVIK Photo 本地部署")
    say("=" * 74)

    say("")
    say("[1/4] 读取载荷容器")
    bundle = installer.Bundle(os.path.join(ROOT, "payload.zip"))
    arch = installer.detect_arch()
    say("      安装器自身 %s 位；目标系统判定 %s；源容器构建于 %s"
        % (installer.describe_self(), arch, bundle.meta.get("created")))

    install_dir = installer.default_install_dir()
    target_exe = os.path.join(install_dir, installer.PROGRAM_FILENAME)
    say("      安装位置：%s" % install_dir)
    say("      桌面目录：%s" % desktop)
    say("      将释放：%s 版主程序" % ("64 位" if arch == "x64" else "32 位"))
    if os.path.normcase(install_dir) != os.path.normcase(desktop):
        # 正常情况下默认位置就是桌面；不一致说明桌面不可写、走了回退分支。
        say("      注意：默认位置不是桌面（桌面可能不可写），这是回退分支")

    say("")
    say("[2/4] 写出主程序")
    # 先给目标文件夹拍个快照，装完再比 —— 见 [3/4] 的说明
    before = set(os.listdir(install_dir))
    data, size, crc = bundle.payload(arch)
    installer.write_payload(target_exe, data)
    ref = os.path.join(ROOT, "dist", installer.PROGRAM_FILENAME if arch == "x64" else "MyAlbum_x86.exe")
    same = sha256(target_exe) == sha256(ref)
    bits = pe_bits(target_exe)
    say("      %s  %s B  crc=%s" % (target_exe, "{:,}".format(size), crc))
    say("      与构建产物哈希一致 : %s" % ("OK" if same else "FAIL"))
    say("      PE 位数 = %s        : %s" % (bits, "OK" if bits == (64 if arch == "x64" else 32) else "FAIL"))
    if not same:
        failures.append("主程序哈希不一致")
    if bits != (64 if arch == "x64" else 32):
        failures.append("主程序位数不符")

    say("")
    say("[3/4] 单文件安装（不往那个文件夹里多放任何东西）")
    resources = bundle.resources()
    skipped = installer.report_skipped_resources(resources)
    say("      容器内附带资源 %d 项，按单文件安装跳过 %d 项" % (len(resources), skipped))
    say("      程序文件存在       : %s" % os.path.isfile(target_exe))
    # 断言用"写盘前后的差集"，**不要**去按名字查"有没有 Resources/"：
    # 安装位置默认就是桌面，而桌面上本来就有用户自己的 resources/ 文件夹
    # （实测就撞上了一条假的失败）。差集才是我们要的语义 ——
    # "这次安装到底往这个文件夹里多放了什么"。
    added = sorted(set(os.listdir(install_dir)) - before)
    extra = [n for n in added if n != installer.PROGRAM_FILENAME]
    say("      安装新增的条目     : %s" % (", ".join(added) or "（无：文件已存在，属重复部署）"))
    say("      除程序本体外的新增 : %s" % ("（无）" if not extra else extra))
    if not os.path.isfile(target_exe):
        failures.append("主程序没有写出来")
    if extra:
        failures.append("安装往目标文件夹里多放了东西：%s" % extra)

    say("")
    say("[4/4] 确认没有创建快捷方式")
    link = legacy_shortcut(install_dir)
    if os.path.isfile(link):
        # 只可能是 1.1.0 遗留的；现在这套流程不再创建 .lnk，顺手清掉。
        say("      发现旧版遗留的快捷方式，删除：%s -> %s" % (link, _remove_file(link)))
    still = os.path.isfile(link)
    say("      快捷方式 %s" % link)
    say("      当前不存在         : %s" % (not still))
    if still:
        failures.append("桌面上的快捷方式没有被清掉")
    if not hasattr(installer, "create_desktop_shortcut"):
        say("      installer 里已无创建快捷方式的函数 ✓")
    else:
        failures.append("installer 仍然保留 create_desktop_shortcut")

    say("")
    say("=" * 74)
    if failures:
        say("部署失败：%s" % failures)
    else:
        say("部署完成 ✓  桌面上只有 %s，没有快捷方式" % installer.PROGRAM_FILENAME)
        say("卸载命令：python code_review/deploy_local.py --uninstall")
    say("=" * 74)

    # 输出写到 code_review/ 而不是仓库根目录 —— 根目录只留 README/CHANGELOG/.gitignore
    with open(os.path.join(HERE, "_deploy.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(LINES))
    # 生成物脱敏：本机绝对路径换成占位符（硬性约定 12）。
    # 这份日志本来就是给你自己看的，占位符不影响可读性，却能随手贴出去。
    try:
        import _redact
        _redact.scrub_file(os.path.join(HERE, "_deploy.txt"))
    except ImportError:
        pass
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
