# -*- coding: utf-8 -*-
"""ai_ops.py 的回归测试。

覆盖什么
==================================================================
这一支验证"纯本地 AI 修图"的**骨架**逻辑（不依赖真实模型权重）：

* 系统门控 ``ai_supported``：Win10/11 64 位才放行，Win7/XP、非 Windows、
  32 位进程一律 False；
* 模型目录自检 ``check_models`` / ``models_present``：没下权重时老实说"没装"；
* 模型注册表：每个能力都能映射到模型；
* 解包 ``apply_ai_pack``：正常 zip 解开并登记版本；含 Zip Slip 的恶意包被拒；
* 打包 ``build_ai_pack``：把 models/ 打成 .pack 并给出 size/sha256（发版用）；
* 能力函数：在"没权重"时统一返回 ``(False, 人话原因)``，绝不抛栈；
* 预处理/后处理（需要 numpy + Pillow）：形状与值域正确。

判定口径
==================================================================
numpy / Pillow 缺失时，**相关用例跳过**（报告里写 [跳过]），不算失败——
那是运行环境的事，不是代码的事。其它用例不依赖第三方库。

用法
==================================================================
    ~/.workbuddy/cache/hvphoto/venv2/Scripts/python.exe code_review/ai_ops_test.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "optimized"))
sys.path.insert(0, ROOT)                             # 好让 _redact 也能被 import

import _redact                                       # noqa: E402
import ai_ops as A                                   # noqa: E402
import shell_ops as S                                # noqa: E402
import state_store                                   # noqa: E402


class Report:
    def __init__(self):
        self.lines = []
        self.failures = []
        self.warnings = []

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

    def skip(self, label, why):
        self.lines.append("[跳过] %s（%s）" % (label, why))
        self.warnings.append(label)

    def text(self):
        head = ["=" * 68, "ai_ops.py 回归测试", "=" * 68]
        tail = ["", "=" * 68]
        if self.failures:
            tail += ["FAILED: %d 项不成立" % len(self.failures)]
            tail += ["  * " + f for f in self.failures]
        else:
            tail += ["RESULT: OK"]
        tail.append("=" * 68)
        return "\n".join(head + self.lines + tail) + "\n"


@contextlib.contextmanager
def _patch_ai_support(*, windows, version, is64):
    """临时改写 ai_supported 的判断依据，复原后不影响别的测试。"""
    saved_w = S.windows_version
    saved_iw = S.IS_WINDOWS
    saved_64 = A._is_64bit
    S.IS_WINDOWS = windows
    S.windows_version = lambda: version
    A._is_64bit = lambda: is64
    try:
        yield
    finally:
        S.IS_WINDOWS = saved_iw
        S.windows_version = saved_w
        A._is_64bit = saved_64


def _fake_models_dir(rep, tmp):
    """把 ai_ops.models_dir 指到一个临时目录，并返回它。"""
    real = A.models_dir
    A.models_dir = lambda: tmp
    return real


def main():
    rep = Report()
    tmp_state = tempfile.mkdtemp(prefix="haavik_ai_state_")
    old_env = os.environ.get("HAAVIK_STATE_DIR")
    os.environ["HAAVIK_STATE_DIR"] = tmp_state
    state_store.set_state_dir_for_tests(tmp_state)

    rep.add("python %s | os.name=%s" % (sys.version.split()[0], os.name))

    try:
        # ============================================================
        rep.head("A. 模块纯度与系统门控")
        # ============================================================
        rep.check(callable(A.ai_supported), "ai_supported 是可调用函数")
        rep.check(isinstance(A.ai_available, type(A.ai_supported)),
                  "ai_available 是 ai_supported 的别名")
        rep.check(set(A.CAP_LABELS) ==
                  {A.CAP_SUPER_RES, A.CAP_DENOISE, A.CAP_FACE, A.CAP_MATTE,
                   A.CAP_SELECT_MATTE, A.CAP_BRUSH_MATTE, A.CAP_ENHANCE,
                   A.CAP_INPAINT, A.CAP_COLORIZE},
                  "九个能力都在 CAP_LABELS 里登记")

        with _patch_ai_support(windows=True, version=(10, 0), is64=True):
            rep.check(A.ai_supported() is True, "Win10/64 → 支持")
        with _patch_ai_support(windows=True, version=(6, 1), is64=True):
            rep.check(A.ai_supported() is False, "Win7/64 → 不支持（老系统不装 AI）")
        with _patch_ai_support(windows=True, version=(10, 0), is64=False):
            rep.check(A.ai_supported() is False, "Win10/32 → 不支持（需 64 位进程）")
        with _patch_ai_support(windows=False, version=(0, 0), is64=True):
            rep.check(A.ai_supported() is False, "非 Windows → 不支持")

        # ============================================================
        rep.head("B. 模型目录自检（没下权重时）")
        # ============================================================
        # 指到一个空的临时目录，确保不是读到了本机真装过的 models/
        with tempfile.TemporaryDirectory() as md:
            real = _fake_models_dir(rep, md)
            ok, missing = A.check_models()
            rep.check(ok is False and missing == ["ai_manifest.json"],
                      "缺 ai_manifest.json → 判定未装", str(missing))
            rep.check(A.models_present() is False, "models_present() 返回 False")
            A.models_dir = real

        # ============================================================
        rep.head("C. 模型注册表映射")
        # ============================================================
        # 仍需要权重的能力必须能映射到模型；人脸修复/智能增强已改为纯算法、
        # 不映射模型（见下面断言）。
        for cap in (A.CAP_SUPER_RES, A.CAP_DENOISE, A.CAP_MATTE,
                    A.CAP_SELECT_MATTE, A.CAP_INPAINT):
            m = A._model_for_cap(cap)
            rep.check(m is not None and m in A.MODELS,
                      "能力 %s → 模型 %s" % (cap, m))
        rep.check(A._model_for_cap(A.CAP_FACE) is None
                  and A._model_for_cap(A.CAP_ENHANCE) is None,
                  "人脸修复/智能增强已改为不依赖权重（纯算法，无模型映射）")

        # ============================================================
        rep.head("C2. 别名解析 / available_caps（修复改名弄挂老用户）")
        # ============================================================
        with tempfile.TemporaryDirectory() as work:
            md = os.path.join(work, "models")
            os.makedirs(md)
            real = _fake_models_dir(rep, md)
            # 模拟老组件：只有旧名 birefnet_matting.onnx，没有主名 birefnet_int8.onnx
            with open(os.path.join(md, "birefnet_matting.onnx"), "wb") as fh:
                fh.write(b"x")
            A.clear_sessions()
            p = A._resolve_model_file("birefnet")
            rep.check(p is not None
                      and os.path.basename(p) == "birefnet_matting.onnx",
                      "别名：只有旧名 birefnet_matting.onnx 时也能解析到",
                      os.path.basename(p) if p else "None")
            caps = A.available_caps()
            rep.check(A.CAP_MATTE in caps and A.CAP_SELECT_MATTE in caps
                      and A.CAP_BRUSH_MATTE in caps,
                      "available_caps 含抠图相关（靠别名解析）", str(caps))
            rep.check(A.CAP_SUPER_RES not in caps,
                      "无超分模型时 available_caps 不含超分")
            # 加上主名：应优先用主名
            with open(os.path.join(md, "birefnet_int8.onnx"), "wb") as fh:
                fh.write(b"y")
            p2 = A._resolve_model_file("birefnet")
            rep.check(p2 is not None
                      and os.path.basename(p2) == "birefnet_int8.onnx",
                      "主名存在时优先用主名", os.path.basename(p2) if p2 else "None")
            A.models_dir = real

        # ============================================================
        rep.head("C3. 菜单能力 = 能用权重的 + 不需要权重的画笔抠图")
        # ============================================================
        # 为什么单独测这一条：画笔抠图（CAP_BRUSH_MATTE）是纯手动画笔，
        # 不跑模型、不要权重。以前它和别的能力一起被 models_present() 挡在
        # 门外 —— 于是"没下模型"的机器点开「AI」只看到一串"去下载"，
        # 一个能点的功能都没有，用户体感就是"AI 打不开"。（2026-09-21 修）
        with tempfile.TemporaryDirectory() as work:
            md = os.path.join(work, "models")
            os.makedirs(md)                       # 空目录 = 一个模型都没装
            real = _fake_models_dir(rep, md)
            A.clear_sessions()
            rep.check(A.available_caps() == [],
                      "没有任何权重时 available_caps() 为空（模型驱动的口径不变）",
                      str(A.available_caps()))
            mc = A.menu_caps()
            # 2026-09-27：人脸修复/智能增强改成纯算法后，没下权重也该出现在菜单
            rep.check(mc == [A.CAP_FACE, A.CAP_BRUSH_MATTE, A.CAP_ENHANCE],
                      "没有任何权重时菜单列出：人脸修复 + 画笔抠图 + 智能增强",
                      str(mc))
            rep.check(A.cap_needs_model(A.CAP_BRUSH_MATTE) is False,
                      "画笔抠图被认定为**不需要**模型")
            rep.check(A.cap_needs_model(A.CAP_FACE) is False,
                      "人脸修复被认定为**不需要**模型（纯算法）")
            rep.check(A.cap_needs_model(A.CAP_ENHANCE) is False,
                      "智能增强被认定为**不需要**模型（纯算法）")
            for c in (A.CAP_MATTE, A.CAP_SELECT_MATTE, A.CAP_SUPER_RES,
                      A.CAP_DENOISE, A.CAP_INPAINT, A.CAP_COLORIZE):
                if not A.cap_needs_model(c):
                    rep.check(False, "%s 应该需要模型" % c)
            rep.check(A.cap_needs_model(A.CAP_MATTE) is True,
                      "一键抠图被认定为需要模型")
            rep.check(A.cap_needs_model(A.CAP_SELECT_MATTE) is True,
                      "选择性抠图被认定为需要模型（要先自动抠一遍）")
            # 装了权重之后：菜单 = 菜单口径的超集，顺序稳定
            with open(os.path.join(md, "superres_x4.onnx"), "wb") as fh:
                fh.write(b"z")
            mc2 = A.menu_caps()
            rep.check(A.CAP_SUPER_RES in mc2 and A.CAP_BRUSH_MATTE in mc2
                      and A.CAP_FACE in mc2 and A.CAP_ENHANCE in mc2,
                      "装了超分权重后菜单同时含超分/画笔抠图/人脸修复/智能增强",
                      str(mc2))
            rep.check(mc2.index(A.CAP_SUPER_RES) < mc2.index(A.CAP_BRUSH_MATTE),
                      "菜单顺序稳定（超分在前、画笔抠图在后）", str(mc2))
            A.models_dir = real
            A.clear_sessions()

        # ============================================================
        rep.head("D. 打包 / 解包 / 版本登记")
        # ============================================================
        with tempfile.TemporaryDirectory() as work:
            md = os.path.join(work, "models")
            os.makedirs(md)
            with open(os.path.join(md, "fake_realesrgan.onnx"), "wb") as fh:
                fh.write(b"FAKE_MODEL_BYTES" * 1000)
            real = _fake_models_dir(rep, md)
            pack = os.path.join(work, "haavik_ai.pack")
            ok, path, info = A.build_ai_pack(pack)
            rep.check(ok and os.path.isfile(pack), "build_ai_pack 产出 .pack")
            rep.check(info is not None and isinstance(info["sha256"], str)
                      and len(info["sha256"]) == 64, "打包信息含 64 位 sha256",
                      str(info.get("sha256")[:12]) if info else "")
            rep.check(info is not None and info["size"] > 0, "打包信息含体积 > 0",
                      str(info.get("size")) if info else "")
            A.models_dir = real

            # 解包到另一个目录，验证 check_models 通过 + 版本登记
            md2 = os.path.join(work, "installed", "models")
            real2 = _fake_models_dir(rep, md2)
            ok2, msg2 = A.apply_ai_pack(pack, info["version"])
            rep.check(ok2 is True, "apply_ai_pack 解包成功", msg2)
            rep.check(A.models_present() is True, "解包后 models_present() 为 True")
            st = state_store.load()
            rep.check(state_store.ai_component_version(st) == info["version"],
                      "已登记 AI 组件版本", state_store.ai_component_version(st))
            A.models_dir = real2

        # ============================================================
        rep.head("E. Zip Slip 防护")
        # ============================================================
        with tempfile.TemporaryDirectory() as work:
            md = os.path.join(work, "models")
            os.makedirs(md)
            real = _fake_models_dir(rep, md)
            evil = os.path.join(work, "evil.pack")
            with zipfile.ZipFile(evil, "w") as zf:
                zf.writestr("../escape.txt", "pwned")
                zf.writestr("ai_manifest.json", json.dumps(
                    {"version": "1.0.0", "files": [{"path": "../escape.txt"}]}))
            ok3, msg3 = A.apply_ai_pack(evil, "1.0.0")
            rep.check(ok3 is False, "含 Zip Slip 的包被拒", msg3)
            rep.check(not os.path.exists(os.path.join(work, "escape.txt")),
                      "恶意文件没有被解到包外")
            A.models_dir = real

        # ============================================================
        rep.head("F. 能力函数在「没权重」时优雅降级")
        # ============================================================
        with tempfile.TemporaryDirectory() as md:
            real = _fake_models_dir(rep, md)
            with _patch_ai_support(windows=True, version=(10, 0), is64=True):
                for cap, fn in (
                    (A.CAP_SUPER_RES, A.super_resolve),
                    (A.CAP_DENOISE, A.denoise),
                    (A.CAP_FACE, A.face_restore),
                    (A.CAP_MATTE, A.matting),
                    (A.CAP_ENHANCE, A.enhance),
                ):
                    # 没有真实图也能验证"先判权重缺失"这条路径不会崩
                    try:
                        res = fn(None)
                    except Exception as exc:               # noqa: BLE001
                        res = ("BOOM", str(exc))
                    rep.check(isinstance(res, tuple) and res[0] is False,
                              "能力 %s 在缺权重时返回 (False, 原因)" % cap,
                              str(res))
            A.models_dir = real

        # ============================================================
        rep.head("F2. 人脸修复 / 智能增强（纯算法，无需权重）")
        # ============================================================
        try:
            from PIL import Image as _PILImage
            import numpy as _np
            with _patch_ai_support(windows=True, version=(10, 0), is64=True):
                simg = _PILImage.new("RGB", (64, 48), (100, 120, 140))
                ok_f, out_f = A.face_restore(simg)
                rep.check(ok_f and isinstance(out_f, _PILImage.Image)
                          and out_f.size == (64, 48) and out_f.mode == "RGB",
                          "人脸修复返回同尺寸 RGB 图",
                          str((ok_f, out_f.size if out_f else None)))
                ok_e, out_e = A.enhance(simg)
                rep.check(ok_e and isinstance(out_e, _PILImage.Image)
                          and out_e.mode == "RGB",
                          "智能增强返回 RGB 图", str((ok_e,)))
                # 暗图自动提亮：明显偏暗的图经过增强后平均亮度应上升
                dark = _PILImage.new("RGB", (32, 32), (20, 20, 20))
                _, out_d = A.enhance(dark)
                after = float(_np.asarray(out_d, dtype=float).mean())
                rep.check(after > 20,
                          "智能增强把偏暗图提亮了（均值 %.1f > 20）" % after,
                          "%.1f" % after)
                # 没图也要优雅降级，不崩
                rep.check(A.face_restore(None)[0] is False
                          and A.enhance(None)[0] is False,
                          "人脸修复/智能增强对 None 输入返回 (False, 原因)")
        except ImportError:
            rep.check(True, "（无 Pillow/numpy，跳过 F2 实测）")

        # ============================================================
        rep.head("G. 预处理 / 后处理（需 numpy + Pillow）")
        # ============================================================
        try:
            import numpy as np
            from PIL import Image
            with _patch_ai_support(windows=True, version=(10, 0), is64=True):
                img = Image.new("RGB", (32, 24), (10, 20, 30))
                arr, src = A.preprocess_image(img)
                rep.check(arr.ndim == 3 and arr.shape[0] == 3 and arr.dtype == np.float32,
                          "preprocess_image → CHW float32", str(arr.shape))
                out = A.postprocess_array(arr, src)
                rep.check(isinstance(out, Image.Image) and out.size == src,
                          "postprocess_array → 同尺寸 RGB 图", str(out.size))
        except ImportError as exc:
            rep.skip("预处理/后处理", "缺 numpy/Pillow：%s" % exc)

        # ============================================================
        rep.head("I. refine_matte（选择性抠图 · 画笔精修，纯 Pillow）")
        # ============================================================
        if A.Image is None:
            rep.skip("refine_matte", "缺 Pillow")
        else:
            from PIL import Image as PILImage
            base = PILImage.new("RGBA", (100, 100), (255, 0, 0, 255))
            orig = PILImage.new("RGB", (100, 100), (10, 20, 30))
            out0 = A.refine_matte(base, orig, [])
            rep.check(out0.size == (100, 100), "空 strokes 原样返回")
            out_e = A.refine_matte(base, orig, [(50, 50, 10, "erase")])
            ae = out_e.split()[3]
            rep.check(ae.getpixel((50, 50)) == 0, "erase 中心 alpha=0")
            rep.check(ae.getpixel((5, 5)) == 255, "erase 不影响远处 alpha=255")
            out_r = A.refine_matte(base, orig, [(50, 50, 10, "restore")])
            ar = out_r.split()[3]
            rep.check(ar.getpixel((50, 50)) == 255, "restore 中心 alpha=255")
            rep.check(out_r.getpixel((50, 50))[:3] == (10, 20, 30),
                      "restore 取回原图颜色")
            out3 = A.refine_matte(base, orig,
                                 [(50, 50, 10, "erase"), (50, 50, 10, "restore")])
            rep.check(out3.split()[3].getpixel((50, 50)) == 255,
                      "erase 后 restore 同点 → 不透明")

        # ============================================================
        rep.head("J. 校验 AI 完整性（verify_ai_integrity，纯本地）")
        # ============================================================
        with tempfile.TemporaryDirectory() as md:
            real = _fake_models_dir(rep, md)
            # 没装：清单都没有
            ok_v, msg_v = A.verify_ai_integrity()
            rep.check(ok_v is False and "尚未" in msg_v,
                      "未安装时校验返回失败", msg_v)
            # 写一个完整清单 + 一个文件 + onnxruntime 目录
            f = os.path.join(md, "m.onnx")
            open(f, "wb").write(b"x" * 100)
            json.dump({"version": "1.0.0",
                       "files": [{"path": "m.onnx", "size": 100}]},
                      open(os.path.join(md, "ai_manifest.json"), "w"))
            os.makedirs(os.path.join(md, "onnxruntime"))
            ok_v, msg_v = A.verify_ai_integrity()
            rep.check(ok_v is True and "完整" in msg_v,
                      "文件齐全 + onnxruntime 存在 → 完整", msg_v)
            # 改坏大小
            open(f, "wb").write(b"x" * 50)
            ok_v, msg_v = A.verify_ai_integrity()
            rep.check(ok_v is False and "大小不符" in msg_v,
                      "文件大小不符 → 不完整", msg_v)
            # 恢复大小，删掉 onnxruntime 目录
            open(f, "wb").write(b"x" * 100)
            import shutil as _sh
            _sh.rmtree(os.path.join(md, "onnxruntime"), ignore_errors=True)
            ok_v, msg_v = A.verify_ai_integrity()
            rep.check(ok_v is False and "onnxruntime" in msg_v,
                      "缺 onnxruntime 目录 → 不完整", msg_v)
            A.models_dir = real

        # ============================================================
        rep.head("K. 删除 AI 模型（delete_models / 体积统计）")
        # ============================================================
        rep.check(A.human_size(0) == "0 B", "human_size(0)", A.human_size(0))
        rep.check(A.human_size(1024) == "1 KB", "human_size(1024)", A.human_size(1024))
        rep.check(A.human_size(92076310) == "87.8 MB",
                  "human_size 把 88MB 说成 87.8 MB", A.human_size(92076310))
        rep.check(A.human_size(None) == "0 MB",
                  "human_size 收到 None 不炸（菜单构建时不能因为文案崩掉）",
                  A.human_size(None))

        with tempfile.TemporaryDirectory() as md:
            # 没装模型：删除必须**拒绝**并说清"没有可删的"，而不是报成功
            real = _fake_models_dir(rep, md + "_不存在")
            ok_d, msg_d = A.delete_models()
            rep.check(ok_d is False and "没有安装" in msg_d,
                      "★ 没装模型时删除返回失败并说明原因（不谎报成功）", msg_d)
            A.models_dir = real

        with tempfile.TemporaryDirectory() as box:
            # ⚠ 目录名必须是 "models" —— 防呆要求的就是它。
            # 这里如果直接用 TemporaryDirectory() 的名字（tmpXXXX），
            # 会被防呆正确地拦下来（第一版测试就踩了这个，是**测试**写错了）。
            md = os.path.join(box, "models")
            real = _fake_models_dir(rep, md)
            os.makedirs(os.path.join(md, "onnxruntime"))
            open(os.path.join(md, "a.onnx"), "wb").write(b"x" * 3000)
            open(os.path.join(md, "b.onnx"), "wb").write(b"y" * 2000)
            open(os.path.join(md, "onnxruntime", "c.py"), "w").write("z" * 1000)
            rep.check(A.models_size_bytes() == 6000,
                      "★ models_size_bytes 把子目录里的文件也算进去",
                      str(A.models_size_bytes()))

            # ---- 防环：models/ 里若有指回上层的目录联接，不能无限递归 ----
            # （junction 在 is_dir(follow_symlinks=False) 眼里也是目录；
            #   真递归下去就是死循环，而它是在"拼菜单"时跑的 —— 表现会是
            #   "点 AI 没反应"，正是项目最怕的静默失败。）
            loop = os.path.join(md, "loop")
            made_loop = False
            try:
                import subprocess as _sp
                r = _sp.run(["cmd", "/c", "mklink", "/J", loop, box],
                            capture_output=True, timeout=20)
                made_loop = (r.returncode == 0)
            except Exception:                                 # noqa: BLE001
                made_loop = False
            if made_loop:
                import time as _time
                t0 = _time.time()
                n_loop = A.models_size_bytes()
                dt_loop = _time.time() - t0
                rep.check(dt_loop < 5.0,
                          "★★ 有目录联接（会成环）时不卡死（防环生效）",
                          "耗时 %.0f ms" % (dt_loop * 1000))
                rep.check(n_loop == 6000,
                          "★ 有环时体积也没被重复累加", str(n_loop))
                try:
                    os.rmdir(loop)
                except OSError:
                    pass
            else:
                rep.add("  (跳过：本机建不出 junction，不做防环断言)")

            # ---- 失败路径：删不掉时**绝不能**清版本号 ----
            import shell_ops as _S
            real_del = _S.delete_tree_to_recycle_bin
            real_set = A.set_ai_component_version
            wiped = {"n": 0}
            A.set_ai_component_version = lambda v, *a, **k: wiped.__setitem__("n", wiped["n"] + 1)
            _S.delete_tree_to_recycle_bin = lambda p, **k: (False, "（测试）假装删失败")
            ok_d, msg_d = A.delete_models()
            rep.check(ok_d is False and "没有" in msg_d,
                      "★ 删失败时如实报错，并写明模型没有被删除", msg_d)
            rep.check("*" not in msg_d,
                      "失败说明里不许出现 Markdown 星号（tk 会把星号原样显示）",
                      msg_d)
            rep.check(wiped["n"] == 0,
                      "★★ 删失败时**没有**去清版本号（否则界面会变成"
                      "「点一下就能重下」，而磁盘上还躺着一份）", str(wiped["n"]))
            rep.check(os.path.isdir(md),
                      "删失败时目录原样还在")
            # 拿掉猴补丁，恢复真实现
            _S.delete_tree_to_recycle_bin = real_del
            A.set_ai_component_version = real_set

            # ---- 真删（走回收站）----
            ok_d, msg_d = A.delete_models()
            rep.check(ok_d is True, "★ 真的把模型目录删掉（进回收站）", msg_d)
            rep.check(not os.path.exists(md),
                      "★ 删完目录确实不在了")
            rep.check(os.path.isdir(box),
                      "★ 只删了 models 这一层，外面的父目录没被动")
            rep.check(A.ai_component_version() == "",
                      "★ 删成功后版本记录被清空（界面会回到「下载 AI 模型组件…」）",
                      repr(A.ai_component_version()))
            A.models_dir = real

        # ============================================================
        rep.head("L. 菜单上的 AI 版本显示（ai_menu_status / 远端版本缓存）")
        # ============================================================
        # 起因是浩然问"可以更新 AI 吗？" —— 因为界面上根本没写他装的是哪一版、
        # 有没有更新，想知道只能点一下（还得等联网）。
        # 菜单是**同步**拼的、绝不能联网，所以只能用"已装版本 + 上次查到的远端版本"。
        # 正因为远端那个是**缓存**，措辞必须诚实 —— 下面几条就是钉这个。
        _cases = (
            # (已装, 远端缓存, 文案里必须出现, 是否算"有更新")
            ("", "1.4.0", "下载 AI 模型组件", False),
            ("1.2.0", "1.4.0", "有更新", True),
            ("1.4.0", "1.4.0", "已是最新", False),
            ("1.4.0", "", "点此检查更新", False),
            ("1.2.0", "", "点此检查更新", False),
        )
        for inst, rem, must, expect_new in _cases:
            text, has_new = A.ai_menu_status(inst, rem)
            rep.check(must in text and has_new is expect_new,
                      "文案对得上：装了 %r / 远端缓存 %r" % (inst or "无", rem or "无"),
                      text)

        # ★ 最关键的一条：**不知道远端时绝不许说"已是最新"**。
        #   那是"猜"，而本项目最怕的就是把没验证过的事说成事实。
        for rem in ("", None):
            text, _ = A.ai_menu_status("1.4.0", rem)
            rep.check("已是最新" not in text,
                      "★ 远端版本缓存为空时**不许**说「已是最新」（不能猜）",
                      text)
            rep.check("点此检查更新" in text,
                      "★ 这种情况要老实说「点此检查更新」", text)

        # 没装的时候不许报版本号（那时候没有版本可言）
        text_notinst, _ = A.ai_menu_status("", "1.4.0")
        rep.check("v" not in text_notinst.replace("约 90 MB", ""),
                  "★ 没装模型时不显示任何版本号", text_notinst)

        # 远端缓存能存能取（菜单下次拼的时候要读它）
        A.set_ai_remote_version("9.9.9")
        rep.check(A.ai_remote_version() == "9.9.9",
                  "★ 远端版本缓存能写能读", repr(A.ai_remote_version()))
        A.set_ai_remote_version("")
        rep.check(A.ai_remote_version() == "",
                  "★ 缓存能被清空", repr(A.ai_remote_version()))

        # ============================================================
        rep.head("H. 打包排除 / onnxruntime 运行时加载")
        # ============================================================
        rep.check(A._pack_excluded("onnxruntime/onnxruntime/foo.pyc") is True,
                  "_pack_excluded 跳过 .pyc")
        rep.check(A._pack_excluded("onnxruntime/onnxruntime-1.18.0.dist-info/METADATA") is True,
                  "_pack_excluded 跳过 .dist-info")
        rep.check(A._pack_excluded("onnxruntime/onnxruntime/capi/_pybind_state.pyd") is False,
                  "_pack_excluded 保留真正的 .pyd / 运行文件")
        rep.check(A._pack_excluded("realesrgan_x4.onnx") is False,
                  "_pack_excluded 保留模型权重")

        # onnxruntime 目录不存在时不报错、且不在 sys.path 留下垃圾
        with tempfile.TemporaryDirectory() as md:
            real = _fake_models_dir(rep, md)
            before = list(sys.path)
            A._ensure_onnxruntime_importable()
            rep.check(before == list(sys.path),
                      "models/onnxruntime 不存在时 sys.path 不变")
            A.models_dir = real
        # 存在时会被加进 sys.path（先造一个假目录）
        with tempfile.TemporaryDirectory() as md:
            real = _fake_models_dir(rep, md)
            fake_ort = os.path.join(md, "onnxruntime")
            os.makedirs(fake_ort)
            A._ensure_onnxruntime_importable()
            rep.check(fake_ort in sys.path, "models/onnxruntime 存在时被加进 sys.path")
            A.models_dir = real

    finally:
        if old_env is None:
            os.environ.pop("HAAVIK_STATE_DIR", None)
        else:
            os.environ["HAAVIK_STATE_DIR"] = old_env
        state_store.set_state_dir_for_tests(tmp_state)

    out_path = os.path.join(ROOT, "_ai_ops.txt")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(rep.text())
    _redact.scrub_file(out_path)          # 生成物落盘前脱敏（本机路径 + 令牌）
    sys.stderr.write(_redact.scrub(rep.text()))
    return 1 if rep.failures else 0


if __name__ == "__main__":
    sys.exit(main())
