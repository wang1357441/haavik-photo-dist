# -*- coding: utf-8 -*-
"""资源编解码与完整性校验 —— 构建期（make_bundle）与运行期（installer）共用。

⚠️ 关于"加密"的澄清（请勿误解）
------------------------------------------------------------------
本模块里的 XOR 是**混淆编码**，不是加密：

* 密钥硬编码在源码里，任何人反编译都能在几分钟内还原；
* 它不提供任何机密性，也挡不住任何认真的安全分析；
* 把它当作安全边界，只会换来杀软误报、代码不可读、构建不可复现，
  而挡不住任何真正想看的人（详见 CODE_REVIEW_REPORT.md 的 S-01）。

保留它的唯一理由是"让载荷在十六进制编辑器里不可读"，仅此而已。

真正有价值的是 CRC32 校验：它能在安装器**写出文件之前**发现
"载荷被截断 / 换错文件 / 拷贝损坏"，把一类本来要到用户机器上才暴露的
静默故障，提前到构建期或安装期明确报错。

密钥变更规则
------------------------------------------------------------------
修改 OBFUSCATION_KEY 后必须重新执行 make_bundle.py 并重新打包，
否则旧安装包会解出乱码。deobfuscate() 配合 MZ 头校验会明确报错，
而**不是**静默写坏文件——这正是本模块存在的意义。

Python 3.8 兼容（32 位打包环境用的是 3.8.10）。
"""
from __future__ import annotations

import base64
import binascii

# 32 字节固定混淆键。
#
# 刻意**不再**沿用"看起来像 PE 头"的那串字节（原版是 MZ 90 00 03 ... 这样的
# DOS 头）。原因很具体：真实 PE 文件的前 32 字节恰好就是那串值，异或之后
# 载荷开头会变成 32 个 0x00，形成一段极其显眼的 NUL 特征——
# 比原始 base64 更容易被静态规则命中，与"避开特征"的初衷完全相反。
OBFUSCATION_KEY: bytes = bytes.fromhex(
    "6a3f91c2d84b05e7a1c6f2039dbe7c48"
    "52a0d3f16b8e94c7a5d2e08b31f6a9c4"
)

PE_MAGIC = b"MZ"


class DecodeError(ValueError):
    """资源解码或完整性校验失败。"""


def xor_bytes(data: bytes, key: bytes = OBFUSCATION_KEY) -> bytes:
    """按 key 循环异或（对称，加密解密同一个函数）。

    用整块 int 异或代替逐字节生成器：17 MB 载荷从「秒级」降到「百毫秒级」。
    与朴素逐字节实现完全等价，由 self_test() 覆盖。
    """
    if not key:
        raise ValueError("key 不能为空")
    if not data:
        return b""

    klen = len(key)
    repeated = (key * (len(data) // klen + 1))[: len(data)]
    size = len(data)
    return (int.from_bytes(data, "big") ^ int.from_bytes(repeated, "big")).to_bytes(size, "big")


# 保留旧名，避免外部脚本引用失效
_xor = xor_bytes


def obfuscate(data: bytes) -> bytes:
    """原始字节 -> 混淆字节（构建期写入容器用）。"""
    return xor_bytes(data)


def deobfuscate(data: bytes) -> bytes:
    """混淆字节 -> 原始字节（运行期读取容器用）。"""
    return xor_bytes(data)


def encode(data: bytes) -> str:
    """原始字节 -> XOR -> base64(ascii)。文本型注入流程用（可选）。"""
    return base64.b64encode(xor_bytes(data)).decode("ascii")


def decode(
    blob,
    *,
    expect_pe: bool = False,
    expected_size=None,
):
    """base64 -> XOR -> 原始字节，并做完整性校验。

    容器化之后安装器主要走 ``deobfuscate``，此函数保留给文本注入流程与测试。
    """
    if isinstance(blob, str):
        blob = blob.encode("ascii")

    try:
        raw = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DecodeError("base64 解码失败：%s" % exc) from exc

    data = xor_bytes(raw)
    return verify(data, expect_pe=expect_pe, expected_size=expected_size)


def verify(data: bytes, *, expect_pe: bool = False, expected_size=None, expected_crc=None) -> bytes:
    """对已解出的原始字节做长度 / PE 头 / CRC 校验，返回自身便于链式调用。"""
    if expected_size is not None and len(data) != expected_size:
        raise DecodeError("长度不符：期望 %d 字节，实际 %d 字节" % (expected_size, len(data)))

    if expect_pe and not data.startswith(PE_MAGIC):
        raise DecodeError(
            "载荷不是有效的 PE 文件（缺少 MZ 头）：疑似注入错位、密钥不匹配，或安装包已损坏"
        )

    if expected_crc:
        actual = crc32(data)
        if actual != expected_crc:
            raise DecodeError("CRC 不匹配：期望 %s，实际 %s" % (expected_crc, actual))

    return data


def crc32(data: bytes) -> str:
    """返回 8 位十六进制 CRC32 字符串。"""
    return format(binascii.crc32(data) & 0xFFFFFFFF, "08x")


def self_test() -> None:
    """快速自检：验证快速 XOR 与朴素实现等价、往返一致、校验生效。"""
    def naive(d, k=OBFUSCATION_KEY):
        return bytes(d[i] ^ k[i % len(k)] for i in range(len(d)))

    samples = [
        b"",
        b"MZ" + bytes(range(30)),
        bytes(range(256)) * 7,
        b"the quick brown fox jumps over the lazy dog",
    ]
    for s in samples:
        assert xor_bytes(s) == naive(s), "快速 XOR 与朴素实现不一致，长度=%d" % len(s)
        assert deobfuscate(obfuscate(s)) == s, "混淆往返失败"
        assert decode(encode(s)) == s, "base64 往返失败"

    assert verify(b"MZ\x90\x00", expect_pe=True) == b"MZ\x90\x00"
    assert verify(b"MZ\x90\x00", expected_crc=crc32(b"MZ\x90\x00")) == b"MZ\x90\x00"

    for bad in (b"not-a-pe!!", b"PK\x03\x04"):
        try:
            verify(bad, expect_pe=True)
        except DecodeError:
            pass
        else:
            raise AssertionError("expect_pe 校验未生效")

    try:
        verify(b"MZ", expected_size=999)
    except DecodeError:
        pass
    else:
        raise AssertionError("长度校验未生效")


if __name__ == "__main__":
    self_test()
    print("payload_codec: self-test passed")
