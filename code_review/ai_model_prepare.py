# -*- coding: utf-8 -*-
"""AI 模型组件准备脚本（发版用，**不进任何 exe**）。

把"真实模型权重 + onnxruntime"组装成 ``haavik_ai.pack``，交给 release.py 发到远端。
本文件是**骨架 + 已落地能力**：超分（swin2sr_q 量化版）、抠图（BiRefNet INT8，
MIT，替掉非商用的 RMBG-1.4）、修复（MIGAN）、上色（DeOldify 量化版，MIT）——
这些真实 ONNX 已实测可从 hf-mirror.com / GitHub Release 匿名拉到；人脸修复与
智能增强已改为纯算法实现（不依赖权重），不再从这里下载。

设计要点
========
* onnxruntime 不进主程序：这里用 ``pip install --target models/onnxruntime`` 把它
  塞进 models/，随 AI 组件包一起分发；运行时由 ai_ops 加进 sys.path 再 import。
  这样主程序保持精简（详见 build.py 的 EXCLUDES 注释 + verify_payload 的白名单）。
* 模型按能力分文件落到 models/，文件名必须和 ai_ops.MODELS[].file 完全一致。
* 最终 build_ai_pack 会扫 models/ 写出 ai_manifest.json 并打成 .pack，
  返回的 size / sha256 就是发布清单里 ai 组件要填的值。

用法
====
    # 只看支持什么、不下载
    python code_review/ai_model_prepare.py --list

    # 只把 onnxruntime 塞进 models/（不碰模型，调试打包用）
    python code_review/ai_model_prepare.py --only-onnxruntime

    # 准备全部已落地能力（会拉模型 + 装 onnxruntime + 打包）
    python code_review/ai_model_prepare.py --all --models-dir optimized/models

    # 只准备其中几种（体积更小，可分阶段发）
    python code_review/ai_model_prepare.py --caps super_resolution,denoise,matting
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OPT = os.path.join(ROOT, "code_review", "optimized")
if OPT not in sys.path:
    sys.path.insert(0, OPT)

import ai_ops as A


# --------------------------------------------------------------------------
# 能力 → 候选模型来源
# --------------------------------------------------------------------------
# 每个条目：
#   repo          : HuggingFace 仓库 ID（走 hf-mirror.com 国内镜像拉取，匿名可读）。
#   src           : 仓库里该权重的相对路径（必须真的存在，发布前已实测可达）。
#   filename      : 落到 models/ 的文件名，**必须**和 ai_ops.MODELS[].file 一致。
#   already_onnx  : True = 仓库里就是 .onnx，直接拉；False = 需要 PyTorch→ONNX 转换。
#   export        : already_onnx=False 时导出参数（占位，按模型补 opset / dummy 形状）。
#
# 已实测可达的镜像源（2026-09-20 起，2026-09-27 更新）：
#   超分  : Xenova/swin2SR-realworld-sr-x4-64-bsrgan-psnr 里的 onnx/model_quantized.onnx
#           （Swin2SR x4 量化版，动态输入、CPU 实测可加载可跑）。
#   抠图  : anakhiu/birefnet-onnx-int8 里的 birefnet_int8.onnx（INT8 量化，301MB，MIT）。
#           替换原因：旧 briaai/RMBG-1.4 底层是非商用许可，不能随安装包再分发。
#   修复  : andraniksargsyan/migan 里的 migan.onnx（28.2MB，MIT）。
#   上色  : MartinDelophy/deoldify-onnx-web 的 GitHub Release 资产 deoldify.quant.onnx
#           （61MB，MIT，走 release 直链，不在 HF）。
MODEL_REGISTRY = {
    A.CAP_SUPER_RES: {
        # 2026-09-24 改：换成**量化版** swin2sr_q.onnx（20.6MB）。
        # 实测同一张测试图：量化版 20.6MB/1.98s/锐度 4995，
        # 非量化 50.3MB/2.30s/锐度 1189 → 量化版全面更好。
        "repo": "Xenova/swin2SR-realworld-sr-x4-64-bsrgan-psnr",
        "src": "onnx/model_quantized.onnx",
        "filename": "swin2sr_q.onnx",
        "already_onnx": True, "export": {},
    },
    A.CAP_DENOISE: {
        # 降噪暂且复用超分这份 x4 权重（输出也是 4x，并非严格 1x 降噪——已知局限，
        # 等以后有专门的 1x 降噪 ONNX 再单独加一个模型）。
        "repo": "Xenova/swin2SR-realworld-sr-x4-64-bsrgan-psnr",
        "src": "onnx/model_quantized.onnx",
        "filename": "swin2sr_q.onnx",
        "already_onnx": True, "export": {},
    },
    # CAP_FACE（人脸修复）2026-09-27 起改为纯算法实现（ai_ops.face_restore），
    # 不依赖权重，故不再从这里下载 GFPGAN 权重（其输入约定 [-1,1]、底层 StyleGAN2 非商用）。
    A.CAP_MATTE: {
        # 2026-09-27 换成 BiRefNet INT8 量化版（anakhiu/birefnet-onnx-int8，MIT）。
        # 替换原因：旧 briaai/RMBG-1.4 底层 BiRefNet 权重是**非商用**许可，
        # 不能跟着安装包再分发（规则 #19：AI 模型必须 MIT/Apache 可商用）。
        # BiRefNet INT8 是 MIT，可商用可再分发；301MB。
        # 契约（取自作者 README，已实测确认）：
        #   input "input_image" [1,3,1024,1024] f32（**固定 1024²，别尺寸直接 shape error**），
        #   预处理 = x/255 再 ImageNet 归一化(mean [0.485,0.456,0.406] std [0.229,0.224,0.225])，
        #   output "output_image" [1,1,1024,1024] f32 是 **raw logits**，sigmoid 一次得 alpha。
        "repo": "anakhiu/birefnet-onnx-int8", "src": "birefnet_int8.onnx",
        "filename": "birefnet_int8.onnx",
        "already_onnx": True, "export": {},
    },
    # 注：CAP_FACE（人脸修复）/ CAP_ENHANCE（智能增强）2026-09-27 起改为纯算法实现，
    # 不依赖任何 onnx 权重，故不再从这里下载。（真要神经网络复原需另评估 GFPGAN，
    # 其输入约定 [-1,1]、且底层 StyleGAN2 是非商用许可。）
    A.CAP_INPAINT: {
        # 2026-09-25 选定：MIGAN（28.2MB / MIT）。实测 1024x768 只要 0.44s、
        # 任意尺寸都能跑、抹除误差 1.3~2.0，且许可干净（可商用可再分发）。
        # ⚠ 输入是**两个独立张量**（image uint8 + mask uint8），不是 4 通道拼接；
        #   且遮罩语义是反的（要修的地方涂黑）。这些由 ai_ops.inpaint() 处理。
        "repo": "andraniksargsyan/migan", "src": "migan.onnx",
        "filename": "inpainting.onnx",
        "already_onnx": True, "export": {},
    },
    A.CAP_COLORIZE: {
        # 2026-09-27 新增：老照片上色（DeOldify 量化版，MartinDelophy/deoldify-onnx-web，MIT）。
        # 61MB。注意它**不在 HuggingFace**，而在 GitHub Release（走 release 直链，不走 hf-mirror）。
        # 契约（取自作者 index.html，已实测确认）：
        #   input "input" [1,3,256,256] f32，**灰度复制成 3 通道、值域 [0,255]、不做 /255 归一化**
        #   （demo 里 ImageNet 归一化那几行是注释掉的，不要加），
        #   output "out" [1,3,256,256] f32 是**直接 RGB 彩图**（不是 LAB a/b），
        #   后处理只需 CHW→HWC、round、clip 到 uint8、缩回原尺寸。
        #   ⚠ 直接给 GitHub release 资产直链（已在 url 字段写明），pull_model 会优先用 url。
        "url": "https://github.com/MartinDelophy/deoldify-onnx-web/releases/download/v1.0.0/deoldify.quant.onnx",
        "filename": "deoldify.quant.onnx",
        "already_onnx": True, "export": {},
    },
}


# --------------------------------------------------------------------------
# 下载 / 转换
# --------------------------------------------------------------------------
# HuggingFace 国内镜像：沙箱与国内用户机器都应可达，resolve 会 302 跳到真实文件。
HF_MIRROR = "https://hf-mirror.com"


def _resolve_url(repo: str, src: str) -> str:
    """把 HF 仓库 + 文件路径转成可匿名下载的镜像直链（自动 302 跳转）。"""
    return "%s/%s/resolve/main/%s" % (HF_MIRROR, repo, src)


def download(url: str, dest: str, timeout: float = 600.0) -> None:
    import ssl
    import urllib.request
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        url, headers={"User-Agent": "haavik-ai-prep/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r, \
                open(dest, "wb") as fh:
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
    except Exception as exc:                       # noqa: BLE001
        raise RuntimeError("下载失败 %s：%s" % (url, exc))


def convert_to_onnx(src: str, dst: str, export: dict) -> None:
    """占位：PyTorch → ONNX。

    每个模型的输入/输出名字、opset、dummy 输入形状都不同，**不能通用**，
    必须按模型逐个补完。真实实现大致是：

        import torch
        model = load_your_model(src)               # 按模型写
        dummy = torch.randn(*export["dummy"])       # 形状按模型定
        torch.onnx.export(model, dummy, dst,
                         opset_version=export["opset"],
                         input_names=["input"], output_names=["output"])

    发布前必须把这一段按具体模型写对，否则导出的 .onnx 推理会出垃圾图。
    """
    try:
        import torch                                         # noqa: F401
    except ImportError:
        raise SystemExit("[ERROR] 转换需要 torch（pip install torch），"
                         "本脚本只负责流程，不替你训练/转模型")
    raise NotImplementedError(
        "convert_to_onnx 是占位：请按 %s 这个模型的导出约定补完（输入/输出名、"
        "opset、dummy 形状见 MODEL_REGISTRY 里的 export 字段）" % dst)


def pull_model(cap: str, models_dir: str) -> str | None:
    spec = MODEL_REGISTRY[cap]
    # 还没定具体模型的（FACE / ENHANCE）跳过
    if spec.get("ms_id") == "USER_PICK" or ("repo" not in spec and "url" not in spec):
        print("  [跳过] %-16s：尚未选定具体模型（USER_PICK），先跳过" % cap)
        return None
    dst = os.path.join(models_dir, spec["filename"])
    # 优先用显式直链（如 DeOldify 走 GitHub Release，不在 HF）；否则走 hf-mirror。
    if spec.get("url"):
        url = spec["url"]
    else:
        url = _resolve_url(spec["repo"], spec["src"])
    print("  拉取 %-16s ← %s" % (spec["filename"], url))
    if spec["already_onnx"]:
        download(url, dst)
    else:
        with tempfile.TemporaryDirectory() as td:
            raw = os.path.join(td, "raw.bin")
            download(url, raw)
            convert_to_onnx(raw, dst, spec["export"])
    return dst


def vendor_onnxruntime(models_dir: str, version: str) -> None:
    """把 onnxruntime（CPU 版）装进 models/onnxruntime，随 AI 组件包分发。"""
    dst = os.path.join(models_dir, "onnxruntime")
    os.makedirs(dst, exist_ok=True)
    print("  装 onnxruntime==%s → %s" % (version, dst))
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps",
         "--target", dst, "onnxruntime==%s" % version],
        check=True,
    )


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------
def prepare(caps, models_dir: str, pack_out: str,
            only_ort: bool = False, onnx_version: str = "1.30.0") -> dict:
    os.makedirs(models_dir, exist_ok=True)
    if only_ort:
        vendor_onnxruntime(models_dir, onnx_version)
    else:
        for cap in caps:
            pull_model(cap, models_dir)
        vendor_onnxruntime(models_dir, onnx_version)

    # 让 ai_ops 从我们刚填好的目录打包
    A.models_dir = lambda: models_dir
    ok, path, info = A.build_ai_pack(pack_out)
    if not ok:
        raise SystemExit("[ERROR] 打包失败：%s" % path)
    print("\n打包成功：%s" % path)
    print("  把下面这段填进发布清单的 ai 组件：")
    print(json.dumps(
        {"version": info["version"], "url": "<填下载地址>",
         "sha256": info["sha256"], "size": info["size"]},
        ensure_ascii=False, indent=2))
    return info


def main() -> int:
    ap = argparse.ArgumentParser(description="AI 模型组件准备（发版用，不进 exe）")
    ap.add_argument("--caps", help="逗号分隔的能力名，如 super_resolution,denoise,matting")
    ap.add_argument("--all", action="store_true", help="准备全部 6 种能力")
    ap.add_argument("--only-onnxruntime", action="store_true",
                    help="只装 onnxruntime（不碰模型，调试打包用）")
    ap.add_argument("--models-dir", default=os.path.join(OPT, "models"))
    # 默认就打到 release.py 读 AI 包的位置（<项目根>/build_work/ai_staging/），
    # 这样 `ai_model_prepare.py --all` 产出的包能被 `--publish-github` 直接读到，
    # 不会因为我们把包放错目录而静默发了旧版本。
    ap.add_argument("--output",
                    default=os.path.join(ROOT, "build_work", "ai_staging", "haavik_ai.pack"))
    ap.add_argument("--onnx-version", default="1.30.0",
                    help="随 AI 组件分发的 onnxruntime 版本（须 >=1.30 以兼容主程序的 numpy 2.x）")
    ap.add_argument("--list", action="store_true",
                    help="只列出支持的能力与候选来源")
    args = ap.parse_args()

    if args.list:
        print("支持的能力与候选模型来源（已实测可达）：")
        for cap, spec in MODEL_REGISTRY.items():
            if spec.get("ms_id") == "USER_PICK":
                src = "USER_PICK（待定）"
            elif spec.get("url"):
                src = spec["url"]
            else:
                src = "%s/%s" % (spec["repo"], spec["src"])
            print("  %-16s %-48s %s -> %s" % (
                cap, src,
                "已是 ONNX" if spec["already_onnx"] else "需转 ONNX",
                spec["filename"]))
        return 0

    if args.only_onnxruntime:
        A.models_dir = lambda: args.models_dir
        prepare([], args.models_dir, args.output,
                only_ort=True, onnx_version=args.onnx_version)
        return 0

    if args.all:
        caps = list(MODEL_REGISTRY.keys())
    elif args.caps:
        caps = [c.strip() for c in args.caps.split(",") if c.strip()]
        for c in caps:
            if c not in MODEL_REGISTRY:
                raise SystemExit("[ERROR] 未知能力：%s" % c)
    else:
        raise SystemExit("[ERROR] 至少要给 --caps / --all / --only-onnxruntime 之一")

    A.models_dir = lambda: args.models_dir
    prepare(caps, args.models_dir, args.output, onnx_version=args.onnx_version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
