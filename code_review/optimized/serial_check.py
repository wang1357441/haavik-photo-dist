# -*- coding: utf-8 -*-
"""序列号：正向生成（generate）与逆向校验（verify）—— 同一个文件里的两个算法。

这一个文件同时是"生成器"和"校验器"
==================================================================
* 主程序 import 它，用 ``verify()`` 校验朋友输入的那 25 位字符；
* 它自己的 ``__main__`` 就是**生成器**：朋友把这一个文件下载下来，
  运行 ``python serial_check.py``，就会打印一枚当月有效的序列号。

之所以两半放在同一个文件里，是因为"两份各自维护的实现最终一定会漂移"。
发布到公开仓库的生成器与打进 exe 的校验器**必须是同一份字节**：
同一个 ALPHABET、同一个 MAGIC、同一个 SECRET、同一个算法。
于是"正向生成出来的序列号，逆向一定校验得过"就是一个本地跑一遍
就能证明的事实（``python serial_check.py --selftest``），
而不是一句需要别人信任的话。

这不是安全机制（请如实转告朋友）
==================================================================
``SERIAL_SECRET`` 是公开的（就写在这个文件里，随生成器一起发出去），
任何会读代码的人都能自己写一个生成器。
所以它的真实定位是**一道题，不是一把锁**：

    想让朋友自己去仓库里翻答案   -> 有效
    想防止别人破解               -> 无效，别指望

把它说成"加密保护"是自欺欺人，而自欺欺人的安全声明迟早会变成事故。
主程序里的 ``state_store.py`` 也只是把"这个月已经解锁过"写成一个
用户可编辑的 JSON —— 同理，它不是防线。

序列号结构（25 位 = 5 组 × 5 位）
==================================================================
单字符取值空间是 32（Crockford Base32），刻意去掉了 I / L / O / U：
这串东西是要让人**盯着屏幕手抄**再敲回去的，歧义字符必须拿掉。

    Byte 0        MAGIC 格式标记（防止把随便一串东西当序列号）
    Byte 1..6     随机 nonce（6 字节 = 48 位）
    Byte 7..14    标签 = HMAC-SHA256(SECRET, MAGIC ‖ nonce ‖ 月份序号)[:8]
    ----------------------------------------------------------------
    合计 15 字节 = 120 位 = 恰好 24 个 Base32 字符（无填充）
    第 25 位 = ALPHABET[前 24 位取值之和 mod 32]  —— 手抄校验字符

第 25 位是专门给"抄错一个字符"准备的：让输入错误在本地就被挡下来，
而不是让朋友对着一个莫名其妙的"序列号无效"反复重试。

月份绑定
==================================================================
标签里混进了"月份序号"（``year * 12 + month - 1``），所以一枚序列号
只对生成的当月有效。校验时额外容忍 ±1 个月，原因是两台机器的时钟
可能跨在月初/月末的两侧，而为了这个去为难朋友毫无意义。
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import secrets
import sys
from datetime import date

#: Crockford Base32：去掉 I / L / O / U（易混字符）
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ALPHABET_INDEX = {ch: i for i, ch in enumerate(ALPHABET)}
ALPHABET_SET = frozenset(ALPHABET)

#: 格式标记。序列号第 1 个字节必须等于它，用来尽早排除"把别的东西粘进来了"。
MAGIC = 0x48  # 'H'

#: 签名密钥。**公开的**，见模块说明——这不是秘密，它属于题目的一部分。
SERIAL_SECRET = "HAAVIK-PHOTO / 天空属于哈夫克 / 2026"

GROUPS = 5
GROUP_LEN = 5
TOTAL_CHARS = GROUPS * GROUP_LEN          # 25
PAYLOAD_CHARS = TOTAL_CHARS - 1           # 24
PAYLOAD_BYTES = PAYLOAD_CHARS * 5 // 8    # 15
NONCE_LEN = 6
TAG_LEN = PAYLOAD_BYTES - 1 - NONCE_LEN   # 8

#: 校验时允许的月份偏移。见模块说明"月份绑定"。
MONTH_TOLERANCE = (-1, 0, 1)

#: 手抄时最常见的混淆字符，直接归一化，别难为朋友。
_CONFUSABLE = {"I": "1", "L": "1", "O": "0"}
_SEPARATORS = frozenset(" \t\r\n-_·.")


# --------------------------------------------------------------------------
# 月份
# --------------------------------------------------------------------------
def month_epoch(month: str | None = None) -> int:
    """``"2026-09"`` -> ``24320``（自 0 年 1 月起算的月份序号）。"""
    if month is None:
        today = date.today()
        return today.year * 12 + today.month - 1
    try:
        year_s, month_s = str(month).strip().split("-")[:2]
        year, mon = int(year_s), int(month_s)
    except ValueError as exc:
        raise ValueError("月份格式应为 YYYY-MM，收到 %r" % (month,)) from exc
    if not 1 <= mon <= 12:
        raise ValueError("月份超出范围：%r" % (month,))
    return year * 12 + mon - 1


def month_label(epoch: int) -> str:
    """``24320`` -> ``"2026-09"``（报告里用，便于人看）。"""
    year, mon = divmod(epoch, 12)
    return "%04d-%02d" % (year, mon + 1)


def current_month() -> str:
    """本月的 ``"YYYY-MM"``。月度闸门（这个月问过了没有）也用它。"""
    return month_label(month_epoch(None))


# --------------------------------------------------------------------------
# Base32（Crockford，固定 24 字符，无填充）
# --------------------------------------------------------------------------
def encode_payload(data: bytes) -> str:
    """15 字节 -> 24 个 Base32 字符。"""
    if len(data) != PAYLOAD_BYTES:
        raise ValueError("载荷必须是 %d 字节，收到 %d" % (PAYLOAD_BYTES, len(data)))
    value = int.from_bytes(data, "big")
    out = []
    for shift in range(PAYLOAD_CHARS - 1, -1, -1):
        out.append(ALPHABET[(value >> (5 * shift)) & 0x1F])
    return "".join(out)


def decode_payload(chars: str) -> bytes:
    """24 个 Base32 字符 -> 15 字节。"""
    if len(chars) != PAYLOAD_CHARS:
        raise ValueError("载荷必须是 %d 个字符" % PAYLOAD_CHARS)
    value = 0
    for ch in chars:
        try:
            value = (value << 5) | ALPHABET_INDEX[ch]
        except KeyError as exc:
            raise ValueError("非法字符 %r" % ch) from exc
    return value.to_bytes(PAYLOAD_BYTES, "big")


def checksum_char(payload: str) -> str:
    """第 25 位：前 24 位取值之和 mod 32。挡手抄错位。"""
    total = 0
    for ch in payload:
        try:
            total += ALPHABET_INDEX[ch]
        except KeyError as exc:
            raise ValueError("非法字符 %r" % ch) from exc
    return ALPHABET[total % len(ALPHABET)]


# --------------------------------------------------------------------------
# 归一化 / 排版
# --------------------------------------------------------------------------
def normalize(text: str) -> str:
    """去掉分隔符、统一大写、把 I/L/O 还原成 1/1/0。

    只做"人抄写时会犯的错"的归一化，绝不做猜测性纠正 ——
    猜错的代价是"看起来输入对了但其实不对"，比直接报错难受得多。
    """
    out = []
    for ch in str(text).upper():
        if ch in _SEPARATORS:
            continue
        out.append(_CONFUSABLE.get(ch, ch))
    return "".join(out)


def format_serial(normalized: str) -> str:
    """把 25 个裸字符排版成 ``XXXXX-XXXXX-XXXXX-XXXXX-XXXXX``。"""
    if len(normalized) != TOTAL_CHARS:
        raise ValueError("序列号必须是 %d 个字符" % TOTAL_CHARS)
    return "-".join(
        normalized[i:i + GROUP_LEN] for i in range(0, TOTAL_CHARS, GROUP_LEN)
    )


def is_wellformed(text: str) -> bool:
    """长度与字符集是否合法（未做任何密码学校验）。"""
    norm = normalize(text)
    return len(norm) == TOTAL_CHARS and all(ch in ALPHABET_SET for ch in norm)


# --------------------------------------------------------------------------
# 正向算法：生成
# --------------------------------------------------------------------------
def _tag(nonce: bytes, epoch: int) -> bytes:
    """标签 = HMAC-SHA256(SECRET, MAGIC ‖ nonce ‖ 月份序号)[:8]。"""
    message = bytes([MAGIC]) + nonce + epoch.to_bytes(3, "big")
    return hmac.new(SERIAL_SECRET.encode("utf-8"), message, hashlib.sha256).digest()[:TAG_LEN]


def generate(month: str | None = None) -> str:
    """**正向算法**：造一枚 ``month``（默认本月）有效的序列号。

    nonce 用 ``secrets``（操作系统熵源），所以每次调用结果都不同——
    这一点是刻意的：固定种子的生成器会让"同一台机器每次算出同一串"，
    朋友只要见过一次就会以为所有月份都一样。
    """
    epoch = month_epoch(month)
    nonce = secrets.token_bytes(NONCE_LEN)
    data = bytes([MAGIC]) + nonce + _tag(nonce, epoch)
    payload = encode_payload(data)
    return format_serial(payload + checksum_char(payload))


# --------------------------------------------------------------------------
# 逆向算法：校验
# --------------------------------------------------------------------------
def verify(text: str, month: str | None = None) -> tuple[bool, str]:
    """**逆向算法**：校验一枚序列号。返回 ``(是否通过, 人类可读的原因)``。

    从里往外拆回生成时的三个字段，再重算标签比对。因为标签是把
    ``nonce`` 与月份一起喂进 HMAC 得到的，所以"改一个字符就换一枚序列号"
    这件事在数学上成立 —— 这正是正向/逆向互洽的原因，也是它**不是**
    加密保护的原因（密钥公开）。

    返回原因字符串而不是只返回 bool：界面上要告诉朋友到底哪一步不对
    （长度？字符集？校验位？签名？），一直说"序列号无效"是最差的做法。
    """
    norm = normalize(text)
    if not norm:
        return False, "还没有输入序列号"
    if len(norm) != TOTAL_CHARS:
        return False, "长度不对：应该是 %d 位（5 组 × 5 位），当前 %d 位" % (TOTAL_CHARS, len(norm))
    bad = sorted(set(norm) - ALPHABET_SET)
    if bad:
        return False, "含非法字符：%s（可用字符 %s）" % ("".join(bad), ALPHABET)

    payload, given_check = norm[:PAYLOAD_CHARS], norm[PAYLOAD_CHARS]
    if given_check != checksum_char(payload):
        return False, "校验位不匹配（多半是抄错了一个字符）"

    data = decode_payload(payload)
    if data[0] != MAGIC:
        return False, "格式标记不对，这不是本程序的序列号"
    nonce, tag = data[1:1 + NONCE_LEN], data[1 + NONCE_LEN:]

    base = month_epoch(month)
    for delta in MONTH_TOLERANCE:
        if hmac.compare_digest(tag, _tag(nonce, base + delta)):
            note = "本月有效" if delta == 0 else "有效（月份偏移 %+d）" % delta
            return True, note
    return False, "签名校验不通过：这枚序列号不是为 %s 生成的" % _month_text(month)


def _month_text(month: str | None) -> str:
    return month_label(month_epoch(month))


def verify_any(texts, month: str | None = None) -> tuple[bool, str]:
    """依次校验多个候选（界面把 5 个输入框拼起来会用到）。"""
    for text in texts:
        ok, why = verify(text, month)
        if ok:
            return True, why
    return False, "序列号无效"


# --------------------------------------------------------------------------
# 自证：正向与逆向必须互洽
# --------------------------------------------------------------------------
def selftest(rounds: int = 200) -> tuple[bool, list[str]]:
    """证明"正向造出来的，逆向一定认；改一个字符，一定不认"。

    Returns:
        (是否全部通过, 逐条日志)
    """
    logs: list[str] = []
    ok = True

    def check(label: str, cond: bool) -> None:
        nonlocal ok
        logs.append("  [%s] %s" % ("通过" if cond else "失败", label))
        if not cond:
            ok = False

    # 1) 正逆向互洽（覆盖全年 12 个月 + 跨年）
    bad = 0
    for i in range(rounds):
        month = "%04d-%02d" % (2026 + i // 12 % 3, i % 12 + 1)
        serial = generate(month)
        passed, why = verify(serial, month)
        if not passed:
            bad += 1
            logs.append("       ↑ %s 校验失败：%s" % (serial, why))
    check("%d 轮「生成 → 校验」全部通过" % rounds, bad == 0)

    # 2) 排版/大小写/混淆字符都不该影响结果
    sample = generate("2026-09")
    variants = (
        sample,
        sample.lower(),
        sample.replace("-", ""),
        " " + sample + " ",
        sample.replace("0", "O").replace("1", "I"),
    )
    check("大小写 / 分隔符 / O↔0 I↔1 均归一化",
          all(verify(v, "2026-09")[0] for v in variants))

    # 3) 改一个字符必须失败（包括只改校验位）
    tamper_fail = 0
    base = normalize(sample)
    for pos in range(TOTAL_CHARS):
        for delta in (1, 7):
            cur = base[pos]
            other = ALPHABET[(ALPHABET_INDEX[cur] + delta) % len(ALPHABET)]
            mutated = base[:pos] + other + base[pos + 1:]
            if not verify(mutated, "2026-09")[0]:
                tamper_fail += 1
    check("篡改 %d 处（每一位 × 2 种改法）全部被拒" % (TOTAL_CHARS * 2),
          tamper_fail == TOTAL_CHARS * 2)

    # 4) 换月份必须失败（月份绑定真的生效）
    check("换一个月就失效（月份偏移 > 1）",
          not verify(sample, "2026-11")[0] and not verify(sample, "2026-07")[0])
    check("相邻月份容忍 ±1",
          verify(sample, "2026-08")[0] and verify(sample, "2026-10")[0])

    # 5) 乱输入不能通过，且不能抛异常
    junk_ok = True
    for junk in ("", "HELLO", "0" * 25, "ABCDEFGHIJKLMNOPQRSTUVWXY",
                 "12345-67890-12345-67890-12345", "!!!!!-!!!!!-!!!!!-!!!!!-!!!!!"):
        try:
            if verify(junk, "2026-09")[0]:
                junk_ok = False
                logs.append("       ↑ 垃圾输入竟然通过了：%r" % junk)
        except Exception as exc:  # noqa: BLE001 - 校验器绝不允许抛
            junk_ok = False
            logs.append("       ↑ 垃圾输入把校验器打崩了：%r -> %s" % (junk, exc))
    check("垃圾输入一律拒绝且不抛异常", junk_ok)

    return ok, logs


# --------------------------------------------------------------------------
# 命令行：这就是"序列号生成器"
# --------------------------------------------------------------------------
BANNER = """\
================================================================
 HAAVIK Photo 序列号生成器
================================================================
你说的那句暗号，答案就是这串东西的来源。
它只对**生成它的那个月**有效（前后各容忍 1 个月）。
"""


def _pause_at_end() -> None:
    """停住窗口，别让序列号一闪而过。

    为什么必须有这个
    ==================================================================
    这个文件会被打成 ``serial_check.exe``（onefile、控制台）发给朋友，**双击**
    运行。程序打印完就 ``return``，进程一退，Windows 立刻收走那个小黑窗 ——
    实测只活了 1.6 秒，序列号根本来不及看，更别说抄下来。而答错被锁之后
    生成器是**唯一出口**，这条路必须能看清。

    为什么不能无脑 ``input()``（这条最要命）
    ==================================================================
    ``code_review/_serial_e2e.py`` 是
    ``subprocess.run(..., capture_output=True)`` 跑它的 —— 那时 **stdout 是管道，
    但 stdin 还是继承来的那个控制台**。只看 ``sys.stdin.isatty()`` 会让那支
    自检**挂死**在这里等回车（要等到 90 秒超时才炸），而这是本地测不出来的
    那种坏法。
    所以判据是"**stdin 与 stdout 都接在真控制台上**"，也就是"看起来像人双击开的"：
    双击 → 两个都是控制台 → 停；管道 / 重定向 / ``capture_output`` → 任一个不是 → 不停。
    真跑自动化再显式加 ``--no-pause``，就是双保险。
    """
    try:
        if sys.stdin is None or sys.stdout is None:      # windowed 打包时 stdin 是 None
            return
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return
    except (AttributeError, ValueError, OSError):
        return
    try:
        input("\n按回车键关闭这个窗口…")
    except (EOFError, KeyboardInterrupt, OSError):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="HAAVIK Photo 序列号生成器 / 校验器（正向生成 + 逆向校验）"
    )
    parser.add_argument("--month", default=None,
                        help="指定月份 YYYY-MM（默认本月）")
    parser.add_argument("--count", type=int, default=1, help="生成几枚（默认 1）")
    parser.add_argument("--verify", default=None, help="校验一枚序列号后退出")
    parser.add_argument("--selftest", action="store_true",
                        help="跑自证：正向与逆向必须互洽")
    parser.add_argument("--quiet", action="store_true", help="只输出序列号本身")
    parser.add_argument("--no-pause", action="store_true",
                        help="跑完立刻退出，不停下来等回车（脚本调用用这个）")
    args = parser.parse_args(argv)

    if args.selftest:
        ok, logs = selftest()
        for line in logs:
            print(line)
        print("\n自证结果：%s" % ("全部通过 ✓" if ok else "存在失败 ✗"))
        return 0 if ok else 1

    if args.verify:
        passed, why = verify(args.verify, args.month)
        print("%s —— %s" % ("通过 ✓" if passed else "不通过 ✗", why))
        return 0 if passed else 1

    count = max(1, args.count)
    if not args.quiet:
        print(BANNER)
        print("目标月份：%s" % _month_text(args.month))
        print("")
        print("序列号（%d 枚，每枚都能用）：" % count)
    for _ in range(count):
        print(generate(args.month))
    if not args.quiet:
        print("")
        print("用法：把上面那串（含分隔符）原样敲进程序的「序列号」输入框。")
        print("想复制：在这个窗口里用鼠标把那一串拖黑选中，按回车就复制走了。")
        if not args.no_pause:
            _pause_at_end()
    return 0


if __name__ == "__main__":
    sys.exit(main())
