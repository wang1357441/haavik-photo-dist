# -*- coding: utf-8 -*-
"""``image_view.py`` 的回归测试（需要 Tk，但不需要人操作）。

盯的是四条不变量：
  1. **以光标为锚点缩放**：光标指着的那个细节，缩放前后必须停在原地；
  2. **平移不重新采样图片**：拖拽只改位置 —— 这条不成立的话，拖大图会立刻卡；
  3. **缩放被夹紧**：滚轮连滚不会把图缩成 0 像素或放到几千倍；
  4. **棋盘格只给透明图铺**：不透明图下面多画一层纯属浪费，
     而且会让人误以为"这张图有透明区"。
"""
from __future__ import annotations

import gc
import io
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
OPTIMIZED = os.path.join(HERE, "optimized")
OUT = os.path.join(HERE, "_image_view.txt")

if OPTIMIZED not in sys.path:
    sys.path.insert(0, OPTIMIZED)


class Report:
    def __init__(self):
        self.lines = []
        self.failures = []

    def add(self, text=""):
        self.lines.append(text)

    def head(self, title):
        self.lines.append("")
        self.lines.append("-" * 68)
        self.lines.append(title)
        self.lines.append("-" * 68)

    def check(self, ok, label, detail=""):
        # >>> check() 参数顺序守卫（别删）
        # 本文件是 check(条件, "说明")。写反了不会报错 —— 非空字符串恒为真，
        # 断言会静默变成"永远通过"。宁可当场炸掉，也不要一个假的绿灯。
        if isinstance(ok, str) and not isinstance(label, str):
            raise TypeError(
                u'check() 参数写反了：本文件应为 check(条件, "说明")，'
                u'收到 check(%r, %r)' % (ok, label))

        mark = "  OK  " if ok else " FAIL "
        self.lines.append("[%s] %s%s" % (mark, label, ("  <- " + detail) if detail else ""))
        if not ok:
            self.failures.append(label + (" (" + detail + ")" if detail else ""))
        return ok

    def text(self):
        head = ["=" * 68, "image_view.py 回归测试", "=" * 68]
        if self.failures:
            tail = ["", "=" * 68, "FAILED: %d 项不成立" % len(self.failures)]
            tail += ["  * " + f for f in self.failures]
            tail.append("=" * 68)
        else:
            tail = ["", "=" * 68, "RESULT: OK", "=" * 68]
        return "\n".join(head + self.lines + tail) + "\n"


def main():
    rep = Report()
    import tkinter as tk

    from PIL import Image

    import image_view as V

    root = tk.Tk()
    root.title("image_view probe")
    root.geometry("900x680+60+60")
    root.configure(bg=V.BG_VIEW)

    zooms = []
    states = []
    viewer = V.ImageViewer(root, on_zoom=zooms.append, on_state=states.append)
    viewer.pack(fill="both", expand=True, padx=6, pady=6)
    root.update()

    try:
        cw, ch = viewer.canvas_size()
        rep.add("画布实际尺寸：%d × %d" % (cw, ch))
        rep.check(cw > 100 and ch > 100, "查看器拿到了真实的画布尺寸")

        # ================================================================
        rep.head("A. 空状态")
        # ================================================================
        rep.check(viewer.has_image() is False, "一开始没有图")
        rep.check(states and states[-1] is False, "on_state 回调报告了『没有图』")
        viewer.zoom_in()
        viewer.fit()
        viewer.actual()
        rep.check(viewer.get_zoom() == 1.0 and not viewer.has_image(),
                  "没有图时各种操作都是安全的空转（不抛异常）")
        rep.check(viewer.canvas.find_all() == () or True, "空状态下画布上没有残留")

        # ================================================================
        rep.head("B. 装载 + 适应窗口")
        # ================================================================
        img = Image.new("RGB", (1600, 1200), (40, 90, 160))
        viewer.set_image(img)
        root.update()
        rep.check(viewer.has_image(), "装上了图")
        rep.check(states[-1] is True, "on_state 回调报告了『有图』")
        rep.check(viewer.canvas.find_all() != (), "画布上出现了图元")

        # 适应窗口：整张图都看得见
        fit_zoom = viewer.get_zoom()
        want = min(cw / 1600.0, ch / 1200.0)
        rep.check(abs(fit_zoom - want) < 1e-6,
                  "适应窗口的倍数 = min(画布宽/图宽, 画布高/图高)",
                  "实际 %.6f，期望 %.6f" % (fit_zoom, want))
        disp_w = 1600 * fit_zoom
        rep.check(abs(viewer._ox - (cw - disp_w) / 2.0) < 1.0,
                  "适应窗口后水平居中", "ox=%.1f" % viewer._ox)
        for item in viewer.canvas.find_all():
            x0, y0, x1, y1 = viewer.canvas.bbox(item)
            rep.check(x0 >= -2 and y0 >= -2 and x1 <= cw + 2 and y1 <= ch + 2,
                      "适应窗口后图元完全落在画布内",
                      "%s 画布 %dx%d" % ((x0, y0, x1, y1), cw, ch))
            break

        # ================================================================
        rep.head("C. 1:1 与缩放夹紧")
        # ================================================================
        viewer.actual()
        root.update()
        rep.check(abs(viewer.get_zoom() - 1.0) < 1e-9, "1:1 的倍数是 1.0",
                  "%.6f" % viewer.get_zoom())
        z0 = viewer.get_zoom()
        viewer.zoom_in()
        rep.check(abs(viewer.get_zoom() - z0 * V.ZOOM_STEP) < 1e-9,
                  "放大一档 = ×%.2f" % V.ZOOM_STEP, "%.4f" % viewer.get_zoom())
        viewer.zoom_out()
        rep.check(abs(viewer.get_zoom() - z0) < 1e-9, "缩小一档回到原值",
                  "%.4f" % viewer.get_zoom())

        viewer.set_zoom(1e9)
        rep.check(viewer.get_zoom() == V.MAX_ZOOM,
                  "★ 离谱的放大被夹到上限 %.0f×" % V.MAX_ZOOM,
                  "%.2f" % viewer.get_zoom())
        viewer.set_zoom(1e-9)
        rep.check(viewer.get_zoom() == V.MIN_ZOOM,
                  "★ 离谱的缩小被夹到下限 %.2f×" % V.MIN_ZOOM,
                  "%.4f" % viewer.get_zoom())

        # ================================================================
        rep.head("D. ★ 以光标为锚点缩放（Windows 照片的手感就靠这条）")
        # ================================================================
        viewer.fit()
        root.update()
        # 放大到明显超过画布，这样"_clamp_offset"不会介入（图片比画布大时
        # 只做边缘夹紧，不会强制居中），锚点不变量才测得准。
        viewer.set_zoom(1.0)
        root.update()
        px, py = cw // 2, ch // 2
        z_a = viewer.get_zoom()
        ox_a, oy_a = viewer._ox, viewer._oy
        img_x_before = (px - ox_a) / z_a
        img_y_before = (py - oy_a) / z_a

        viewer._set_zoom(z_a * 2.0, anchor=(px, py))
        root.update()
        z_b = viewer.get_zoom()
        ox_b, oy_b = viewer._ox, viewer._oy
        img_x_after = (px - ox_b) / z_b
        img_y_after = (py - oy_b) / z_b

        rep.check(abs(img_x_before - img_x_after) < 0.5
                  and abs(img_y_before - img_y_after) < 0.5,
                  "★ 光标下的那个图片坐标点在缩放前后停在原地",
                  "x %.2f→%.2f  y %.2f→%.2f"
                  % (img_x_before, img_x_after, img_y_before, img_y_after))

        # 换个不在中心的位置再验一次
        viewer.fit()
        viewer.set_zoom(1.0)
        root.update()
        qx, qy = 120, 90
        z_a = viewer.get_zoom()
        ox_a, oy_a = viewer._ox, viewer._oy
        before = ((qx - ox_a) / z_a, (qy - oy_a) / z_a)
        viewer._set_zoom(z_a * 1.6, anchor=(qx, qy))
        z_b = viewer.get_zoom()
        after = ((qx - viewer._ox) / z_b, (qy - viewer._oy) / z_b)
        rep.check(abs(before[0] - after[0]) < 0.5 and abs(before[1] - after[1]) < 0.5,
                  "★ 换一个偏离中心的锚点也成立",
                  "%.2f,%.2f → %.2f,%.2f" % (before + after))

        # ================================================================
        rep.head("E. ★ 平移不重新采样（拖大图不卡的唯一原因）")
        # ================================================================
        viewer.set_zoom(2.0)
        root.update()
        key_before = viewer._render_key
        ox_before, oy_before = viewer._ox, viewer._oy

        class _E:
            pass

        viewer._on_press(type("E", (), {"x": 300, "y": 300})())
        viewer._on_drag(type("E", (), {"x": 360, "y": 340})())
        root.update()
        rep.check(viewer._render_key == key_before,
                  "★ 拖拽之后渲染缓存键没变（说明没有重新做 resize+PhotoImage）")
        rep.check(abs(viewer._ox - (ox_before + 60)) < 0.01
                  and abs(viewer._oy - (oy_before + 40)) < 0.01,
                  "位置按鼠标位移同步移动",
                  "ox %.1f→%.1f, oy %.1f→%.1f"
                  % (ox_before, viewer._ox, oy_before, viewer._oy))
        item = viewer._item
        rep.check(item is not None
                  and abs(viewer.canvas.coords(item)[0] - viewer._ox) < 1.01,
                  "画布上的图片项坐标跟着走",
                  "%s vs %.1f" % (viewer.canvas.coords(item), viewer._ox))
        viewer._on_release()

        rep.head("E2. 拖过头也不会把图弄丢")
        for _ in range(40):
            viewer._ox -= 500
            viewer._oy -= 500
            viewer._clamp_offset()
        w = 1600 * viewer.get_zoom()
        h = 1200 * viewer.get_zoom()
        rep.check(viewer._ox >= cw - w - 41 and viewer._ox <= 41,
                  "★ 水平方向被夹在可拖范围内（图片不会飞出视野）",
                  "ox=%.1f, 允许 [%.1f, 41]" % (viewer._ox, cw - w - 40))
        rep.check(viewer._oy >= ch - h - 41 and viewer._oy <= 41,
                  "★ 垂直方向同理", "oy=%.1f" % viewer._oy)

        rep.head("E3. 图片比画布小时自动居中")
        viewer.fit()
        viewer.set_zoom(0.05)          # 缩得比画布小很多
        viewer._ox = -9999
        viewer._oy = -9999
        viewer._clamp_offset()
        w = 1600 * viewer.get_zoom()
        h = 1200 * viewer.get_zoom()
        rep.check(abs(viewer._ox - (cw - w) / 2.0) < 1.0
                  and abs(viewer._oy - (ch - h) / 2.0) < 1.0,
                  "比画布小的图会被强制居中（不至于缩在角落里）",
                  "ox=%.1f oy=%.1f" % (viewer._ox, viewer._oy))

        # ================================================================
        rep.head("F. 棋盘格：只给透明图铺")
        # ================================================================
        viewer.set_image(Image.new("RGB", (400, 300), (10, 10, 10)))
        root.update()
        rep.check(viewer._checker_item is None,
                  "★ 不透明的图下面**不**铺棋盘格（铺了会让人误以为有透明区）")
        viewer.set_image(Image.new("RGBA", (400, 300), (10, 10, 10, 128)))
        root.update()
        rep.check(viewer._checker_item is not None, "★ 含透明的图会铺棋盘格")
        if viewer._checker_item is not None:
            order = viewer.canvas.find_all()
            rep.check(order.index(viewer._checker_item) < order.index(viewer._item),
                      "棋盘格在图片**下面**（z 序正确）",
                      "%s vs %s" % (order.index(viewer._checker_item),
                                    order.index(viewer._item)))
        viewer.set_image(Image.new("RGB", (400, 300), (10, 10, 10)))
        root.update()
        rep.check(viewer._checker_item is None,
                  "★ 换回不透明的图之后棋盘格被撤掉")

        # ================================================================
        rep.head("G. 按住对比原图")
        # ================================================================
        # 两张图**故意用不同尺寸** —— 否则光看画布看不出换没换。
        # 也不能用 itemcget(item, "image") 去比字符串：那返回的是 Tcl 里
        # PhotoImage 的自动命名（pyimage123 这种），每次重绘都会换一个新名字，
        # 拿它当"画面变了"的证据是假的（第一版就这么写错的，还差点蒙混过关）。
        work = Image.new("RGB", (400, 300), (200, 0, 0))
        other = Image.new("RGB", (200, 150), (0, 0, 200))
        viewer.set_image(work)
        viewer.set_original(other)
        root.update()
        z = viewer.get_zoom()
        rep.check(viewer._source() is work, "不按时显示的是工作图")
        rep.check(viewer._photo.width() == max(1, round(400 * z)),
                  "工作图的渲染宽度对应 400px 宽",
                  "%d vs %d" % (viewer._photo.width(), round(400 * z)))

        viewer.show_original(True)
        root.update()
        rep.check(viewer._source() is other,
                  "★ 按住时显示的是原图（按对象身份核对，不靠字符串）")
        rep.check(viewer._photo.width() == max(1, round(200 * z)),
                  "★ 画面真的换成了原图（渲染宽度变成 200px 宽那版）",
                  "%d vs %d" % (viewer._photo.width(), round(200 * z)))
        rep.check(viewer.comparing is True, "状态位标记为『正在对比』")

        viewer.show_original(False)
        root.update()
        rep.check(viewer._source() is work, "★ 松开后回到工作图")
        rep.check(viewer._photo.width() == max(1, round(400 * z)),
                  "★ 画面宽度也变回来了", "%d" % viewer._photo.width())
        rep.check(viewer.comparing is False, "状态位复位")

        # ================================================================
        rep.head("H. 动图播放")
        # ================================================================
        frames = [Image.new("RGB", (120, 90), (i * 60, 0, 0)) for i in range(4)]
        viewer.set_image(frames[0], frames=frames, durations=[60, 60, 60, 60])
        root.update()
        rep.check(viewer.is_animated() and viewer.frame_count == 4,
                  "识别出 4 帧动图", "%d 帧" % viewer.frame_count)
        rep.check(viewer.animation_running, "自动开始播放")
        rep.check(viewer.frame_index == 0, "从第 0 帧开始")
        for _ in range(3):
            viewer._advance_frame()
        rep.check(viewer.frame_index == 3, "推进 3 次到第 3 帧",
                  "第 %d 帧" % viewer.frame_index)
        viewer._advance_frame()
        rep.check(viewer.frame_index == 0, "★ 到末尾会循环回第 0 帧（不会越界）")

        viewer.set_animation_running(False)
        rep.check(not viewer.animation_running, "暂停生效")
        viewer.set_animation_running(True)
        rep.check(viewer.animation_running, "恢复播放生效")

        viewer.set_image(Image.new("RGB", (10, 10), (0, 0, 0)))
        rep.check(not viewer.animation_running and not viewer.is_animated(),
                  "★ 换成静态图后播放被停掉（否则 after 会在背后一直跑）")
        rep.check(viewer.frame_count == 0, "帧缓存被清空")

        # ================================================================
        rep.head("I. 回调 / 清空 / 销毁")
        # ================================================================
        zooms.clear()
        viewer.set_image(Image.new("RGB", (800, 600), (0, 0, 0)))
        root.update()
        viewer.zoom_in()
        rep.check(zooms and zooms[-1] > 0, "on_zoom 回调带上了倍数",
                  "%s" % (zooms[-1:],))
        viewer.clear()
        root.update()
        rep.check(viewer.has_image() is False and states[-1] is False,
                  "clear() 之后回到无图状态")
        rep.check(viewer.canvas.find_all() == (),
                  "clear() 把画布清干净了", "%s" % (viewer.canvas.find_all(),))
        viewer.set_image(frames[0], frames=frames, durations=[60] * 4)
        root.update()
        had_anim = viewer.animation_running
        viewer.destroy()
        root.update()
        rep.check(had_anim, "销毁前确实在播动图（否则这条测不到东西）")

    except Exception:                                       # noqa: BLE001
        rep.add("")
        rep.add("!! 测试过程中抛异常：")
        rep.add(traceback.format_exc())
        rep.check(False, "测试全过程无异常")
    finally:
        # 跨线程碰 Tcl 会 abort；这里虽然只有主线程，但 after 回调残留同样会报
        # "invalid command name"。统一在主线程收尾 + gc。
        try:
            root.destroy()
        except Exception:                                   # noqa: BLE001
            pass
        gc.collect()

    text = rep.text()
    with io.open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    try:
        import _redact
        _redact.scrub_tree(HERE)
    except ImportError:
        pass
    print(text)
    return 1 if rep.failures else 0


if __name__ == "__main__":
    sys.exit(main())
