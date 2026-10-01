# -*- coding: utf-8 -*-
"""一句话改图 —— 把中文口令变成具体的编辑动作。

这是"对话式修图"的**第一版，故意做得很粗糙**：不接模型，就是一张
"说法 → 动作"的对照表。为什么先这样做：

  * 零体积、零下载、立刻能用 —— 不会为了等模型而一直拖着不做；
  * 更重要的是：它把"**听懂人话**"和"**干活**"彻底分开了。
    干活的是已有的那些函数（旋转 / 调亮度 / 抠图…）；听懂人话的就是
    :func:`parse_command` 这一个**纯函数**。
    **将来换成真模型（小嵌入模型 / 小 LLM）时，只换这一个函数** ——
    弹窗、动作执行、撤销栈全都不用动。

也就是说：粗糙版不是权宜之计，它是精细版的骨架。
（使用者原话："从小模型循序渐进，从很粗糙变得越来越精细，但要敢干。"）

硬约束
==================================================================
  * 不联网、不引第三方库；
  * ``parse_command`` 是**纯函数**（进字符串、出 :class:`Command` 或 None），
    所以它能被穷举测试 —— 而"听懂人话"这件事恰恰最需要测试；
  * **不认识就说"不认识"，绝不猜**。猜错的代价是"它偷偷改了你的图"，
    比"它说没听懂"难受得多。
"""
from __future__ import annotations

import re
import tkinter as tk
from dataclasses import dataclass, field

import ai_ops
import image_dialogs as dialogs
import image_ops as ops
from ui_theme import BG_ELEV, BG_SUNKEN, FG_BAD, FG_DIM, FG_TEXT, font

#: 中文数字：口令里"放大两倍 / 缩小一半"很常见。
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "半": 0.5}


@dataclass
class Command:
    """一句话解析出来的结果。

    ``kind`` 是动作类别（见 :func:`apply_command` 的分派表），
    ``label`` 是**人话**，会直接进撤销栈 —— 这样 Ctrl+Z 的时候
    状态栏会说「已撤销：调亮 20%」，而不是笼统的「撤销」。
    """
    kind: str
    label: str
    params: dict = field(default_factory=dict)


#: 程度模糊词 —— **它们里面的"一"不是数量**，必须先摘掉再找数字。
#: ⚠ 这条是测试逼出来的真 bug：`调暗一点` 里的"一点"被当成数字 1，
#: 于是亮度算成 `1 - 1 = 0`，**整张图变成全黑**。
#: 一个"随手点一下"的说法能把用户的图搞黑，正是最该被测试挡住的坏法。
#: 长词要排在短词前面（"一点点"先于"一点"），否则摘不干净。
_SOFTENERS = ("一点点", "一点", "一些", "稍微", "略微", "有点", "稍稍", "些")


def _number(text: str):
    """从口令里抠出第一个数（支持 ``2`` / ``2.5`` / ``两`` / ``一半``）。

    找不到返回 None —— 调用方自己决定"没写数字时默认多少"。
    """
    t = text
    for soft in _SOFTENERS:
        t = t.replace(soft, "")
    if "一半" in t:
        return 0.5
    m = re.search(r"(\d+(?:\.\d+)?)", t)
    if m:
        return float(m.group(1))
    for ch in t:
        if ch in _CN_NUM:
            return float(_CN_NUM[ch])
    return None


def _amount(text: str, default: float) -> float:
    """把口令里的数变成"幅度"（0~3 的倍数差）。

    约定：写 ``30`` 当 **30%**（``0.3``），写 ``0.3`` 也当 ``0.3``；
    什么都没写就用 ``default``。
    """
    val = _number(text)
    if val is None:
        return default
    if val > 1:
        val = val / 100.0
    return max(0.01, min(val, 3.0))


#: 角度说法 → 度数。中文口语里"转个身""翻个面"也可能是在说 180°。
_ANGLE_WORDS = {
    "一百八十度": 180, "一百八十": 180, "180度": 180, "180": 180,
    "九十度": 90, "90度": 90, "90": 90,
    "二百七十度": 270, "二百七十": 270, "270度": 270, "270": 270,
    "四十五度": 45, "四十五": 45, "45度": 45,
    "十五度": 15, "十五": 15, "15度": 15,
    "三十度": 30, "三十": 30, "30度": 30,
    "六十度": 60, "六十": 60, "60度": 60,
}


def _angle(text: str):
    """从口令里读出旋转角度（度）。**读不出就返回 None**，不猜。

    ⚠ 只在句子**确实带角度线索**时才返回数 —— 不能拿 :func:`_number` 硬抠，
    否则"缩小到10"里的 10、"放大3倍"里的 3 都会被当成旋转角度。
    ⚠ 也**不要**把"顺时针/逆时针"这种方向词当成角度：方向由调用方另判
    （见 parse_command 里 ccw 那一段），这里只负责"多少度"。
    """
    t = str(text or "")
    # 带"度"字的，随便什么数字都可信（"旋转37度"、"斜一点15度"）
    m = re.search(r"(\d+(?:\.\d+)?)\s*度", t)
    if m:
        return float(m.group(1))
    # 不带"度"字的，只认下面这张表里的固定说法（避免误伤普通数字）
    for word, deg in sorted(_ANGLE_WORDS.items(), key=lambda kv: -len(kv[0])):
        if word in t:
            return float(deg)
    # 中文数字式："转半圈" = 180°
    if "半圈" in t:
        return 180.0
    return None


#: 明确不聊的词 —— 撞到就当场拒绝，不装傻。
#:
#: 使用者问："用户要是说出来什么调戏 AI 的词呢？" 回答分两层 ——
#:
#: **第一层（真正的保障）：它本来就是沙箱。**
#: 能发生的只有 :func:`apply_command` 分派表里那十来件事（旋转 / 调亮度 / 抠图…），
#: 没有别的能力：不联网、不写文件、不能执行任何东西、也碰不到图片之外的世界。
#: 「说不认识就不动」这一条，加上 AST 检查（见 image_command_test 的 D 段），
#: 让"它只能干这些"变成**可证明的事**，而不是"你信我"。
#:
#: **第二层（这一张表）：明显的越界词直接拒绝。**
#: 为什么不只是"装没听懂"：装傻会让人以为"多试几次也许能绕过去"，
#: 反而鼓励继续试。直接说"我只管改图"，边就画清楚了。
#:
#: ⚠ 词表**只用来当场拒绝** —— 不记录、不上传（本模块不联网，也没地方可传）。
#: 有意思的是，真正兜住安全的是第一层；这一层是"把话说清楚"，不是防线。
_BLOCKED = ("傻", "笨", "蠢", "滚", "垃圾", "废物", "弱智", "智障", "骂",
            "色情", "黄色", "裸", "涩", "搞黄", "去死", "妈的", "操你")


def is_blocked(text: str) -> bool:
    """这句话是不是"明显不是在说改图"。"""
    return _one_of(str(text or ""), _BLOCKED)


def _one_of(text: str, keys) -> bool:
    return any(k in text for k in keys)


def parse_command(text) -> Command | None:
    """一句话 → :class:`Command`；**没听懂就返回 None**（不猜）。

    顺序有讲究，不是随手排的：**先窄后宽**。
    比如"变清晰"必须排在"清晰"之前，否则会被当成"锐化"；
    "去背景"必须排在"背景亮"这类之前。改这张表时请保持这个习惯。
    """
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    t = raw.replace(" ", "").replace("　", "")
    for ch in "，,。.！!？?、":
        t = t.replace(ch, "")

    # ---- 明显不是在说改图：当场拒绝 ----
    # 放在最前面：这句话跟"改图"无关，后面的关键词一个都不该有机会被它命中。
    if is_blocked(t):
        return Command("refused", "不聊这个")

    # ---- 撤销 / 重做 ----
    # 2026-09-24 扩说法：口语里"搞错了""不想要了""刚才那个不算"都是在要求撤销。
    if _one_of(t, ("撤销", "退回去", "退回上", "上一步", "取消刚才", "取消上一步",
                   "复原", "撤回", "回退", "返回上", "变回原来", "恢复原样",
                   "恢复原来", "恢复到原来", "恢复成原来", "搞错了", "弄错了",
                   "算错了", "不要这个", "重来", "刚才不算", "删了刚才")):
        return Command("undo", "撤销")
    if _one_of(t, ("重做", "再改回来", "恢复刚才", "重新来一次", "再做一次",
                   "再做一遍", "重复刚才")):
        return Command("redo", "重做")

    # ---- 要 AI 能力的两项（**必须排在"清晰/放大"前面**）----
    if _one_of(t, ("抠图", "去背景", "去掉背景", "背景去掉", "去背", "扣背景",
                   "抠出主体", "只留主体", "把背景拿掉", "拿走背景", "除背景",
                   "背景清理", "抠出来", "单独把主体", "主体留下", "去掉后面的")):
        return Command("ai_cap", "一键抠图", {"cap": ai_ops.CAP_MATTE})
    if _one_of(t, ("超分", "清晰化", "变清晰", "放大清晰", "变清楚", "让它清楚",
                   "更清晰", "清晰一些", "清晰点", "清楚一些", "清楚点",
                   "提高清晰度", "画质提升", "提升画质", "变高清", "高清一些",
                   "糊得看不清", "太糊了", "有点糊", "变精细",
                   # ⚠ "不清楚"单独一个词**不能**收：它是有歧义的
                   #   （"我不清楚"=不知道）。只收"明显在说图"的写法。
                   "图不清楚", "照片不清楚", "图片不清楚", "画面不清楚",
                   "看着糊", "有点模糊")):
        return Command("ai_cap", "超分放大", {"cap": ai_ops.CAP_SUPER_RES})

    # ---- 背景虚化 / 换背景（复用抠图模型，纯合成，无新体积）----
    # ⚠ 必须排在「抠图」之后、「黑白」之前：这些说法里带"背景"，但和
    #   "去背景/抠图"（那是把背景删成透明）不是一回事，别让它们被更早的分支吃掉。
    # ⚠ 顺序：先蓝/红这种**带颜色**的，再白底/换背景这种**默认**的 ——
    #   否则"蓝底证件照"会被"证件照"先圈成白底。
    if _one_of(t, ("背景虚化", "背景模糊", "虚化背景", "把背景弄模糊", "背景弄模糊",
                   "模糊背景", "背景打糊", "把背景虚化")):
        return Command("bg_blur", "背景虚化")
    if _one_of(t, ("蓝底", "蓝背景", "换成蓝底", "证件照蓝底", "蓝底证件照",
                   "背景换成蓝")):
        return Command("bg_blue", "换成蓝底")
    if _one_of(t, ("红底", "红背景", "换成红底", "证件照红底", "红底证件照",
                   "背景换成红")):
        return Command("bg_red", "换成红底")
    if _one_of(t, ("换白底", "白底", "白背景", "换成白底", "变白底", "白底背景",
                   "换底色", "换背景", "换背景色", "换个背景", "背景换成白",
                   "证件照", "证件照白底")):
        return Command("bg_white", "换成白底")

    # ---- 人脸修复 / 智能增强（这两项现在权重缺、是灰的；口令先认，模型就位即生效）----
    if _one_of(t, ("人脸修复", "修脸", "脸修复", "把脸修清晰", "修复人脸",
                   "脸清晰", "美颜", "人像修复", "人脸清晰")):
        return Command("ai_cap", "人脸修复", {"cap": ai_ops.CAP_FACE})
    if _one_of(t, ("智能增强", "整体增强", "增强画质", "一键增强", "智能优化",
                   "整体提升", "画面增强", "画质增强")):
        return Command("ai_cap", "智能增强", {"cap": ai_ops.CAP_ENHANCE})

    # ---- 老照片上色（把黑白/褪色照变彩色，新能力）----
    # ⚠ 必须排在「黑白」**之前**：因为上色口令里常带"黑白"（如"把黑白照变彩色"），
    #   若先过「黑白」分支会被"黑白"关键词吃掉、错变成去色。这里用**明确的加色词**
    #   （上色 / 变彩色 / 彩色化），且**绝不用光秃秃的"彩色""颜色"**——
    #   否则会和「黑白」分支里的"去掉彩色""去掉颜色"撞车（"去掉彩色"也含"彩色"）。
    #   注意：单说"老照片"仍走「黑白」分支（那是"做成老照片的怀旧灰调"），
    #   只有带"上色/变彩色"才走这里（"把老照片变彩色"）。
    if _one_of(t, ("上色", "给照片上色", "黑白照上色", "把黑白照上彩色",
                   "黑白照片变彩色", "把黑白照片变彩色", "老照片上色",
                   "给老照片上色", "把老照片变彩色", "彩色化", "变彩色",
                   "黑白变彩", "给黑白照片上色")):
        return Command("ai_cap", "老照片上色", {"cap": ai_ops.CAP_COLORIZE})

    # ---- 黑白 ----
    if _one_of(t, ("黑白", "灰度", "去色", "灰白", "没有颜色", "去掉颜色",
                   "去掉彩色", "单色", "灰调", "老照片那种色", "像老照片",
                   "老照片", "复古色", "怀旧色", "做旧", "褪成老照片")):
        return Command("grayscale", "变黑白")

    # ---- 自动调色（本地，不跑模型；一键把图颜色扶正）----
    if _one_of(t, ("自动调色", "自动优化", "一键优化", "自动美化", "自动修正",
                   "让图更舒服", "整体调一下", "自动颜色", "自动校色", "自动提色")):
        return Command("autofix", "自动调色")

    # ---- 翻转（**必须排在"旋转"前面**："翻转"里也有个"转"字）----
    if _one_of(t, ("翻", "镜像", "倒过来", "掉个方向")):
        if _one_of(t, ("垂直", "上下", "倒过来", "翻个身")):
            return Command("flip_v", "垂直翻转")
        return Command("flip_h", "水平翻转")

    # ---- 旋转 ----
    # ⚠ 关键字用宽的"转"，因为说法太多（转一下 / 左转 / 顺时针转…）。
    # 代价是"转"太常见，所以要显式排掉「转格式」那条 —— 那是另一个对话框的活，
    # 认成旋转就会把图转一下、还让使用者莫名其妙。
    # "斜"也算：斜一点 / 斜过来 都是在说旋转。
    if ("转" in t and "格式" not in t) or "斜" in t:
        # ⚠⚠ 两处顺序都不能动（2026-09-24 修的 bug）：
        #   ① 方向词必须**先**读出来。原来无论说 90 还是 180 都返回"右转 90°"，
        #      用户说"旋转180度"结果只转了 90°；说"左转90度"结果**往右**转了
        #      —— 都是"悄悄改错"，比说"没听懂"坏得多。
        #   ② 角度其次。读得出角度就按角度转（rotate_any），读不出才默认 90°。
        ccw = _one_of(t, ("左", "逆", "反", "逆时针", "反时针", "往回"))
        deg = _angle(t)
        if deg is not None:
            if deg % 360 == 0:
                return None                          # "转0度"没意义，别动图
            if deg % 360 == 180:
                return Command("rotate_180", "转 180°")
            if deg % 360 == 90:
                return Command("rotate_ccw" if ccw else "rotate_cw",
                               "左转 90°" if ccw else "右转 90°")
            if deg % 360 == 270:
                # 说"270度"本身就把方向说全了：正着算 270 = 逆时针 90
                return Command("rotate_ccw", "转 270°")
            # 非 90 倍数的角度（15°/45°/3°…）：只能靠 rotate_any 补边填充。
            # ⚠ rotate_any 的约定是**正数 = 顺时针**（和 rotate_cw 一致），
            #   所以"左斜/逆时针斜"要传**负**角度。
            if ccw:
                deg = -deg
            return Command("rotate_any", "旋转 %.0f°" % deg, {"deg": deg})
        # 没写角度：按方向词定，都没有就默认右转 90°
        if ccw:
            return Command("rotate_ccw", "左转 90°")
        return Command("rotate_cw", "右转 90°")

    # ---- 裁剪（要人用鼠标框，所以只是把那个窗口打开）----
    if _one_of(t, ("裁剪", "裁一下", "裁掉", "剪裁", "切一下", "裁切", "裁边",
                   "去掉边上", "去掉多余的边", "只保留中间", "去掉白边",
                   "去掉黑边", "去掉边框", "切掉边", "割掉", "截一下",
                   "截取中间")):
        return Command("crop", "裁剪")

    # ---- 锐化 ----
    # ⚠ 排在"清晰"（AI 超分）之后：因为"变清晰/清晰化"要的是**真放大**，
    #   只有"锐一点/清晰一点/更锐利"这种"在原尺寸上抬眼"才是锐化。
    if _one_of(t, ("锐化", "变锐", "锐一点", "锐一些", "更锐", "更锐利",
                   "锋利", "变锋利", "变锐利", "太软了", "发虚", "肉",
                   "细节更多", "把细节提出来", "清晰一点", "清晰一些",
                   "清晰些", "清楚一点", "清楚一些", "清楚些", "锐利些")):
        return Command("adjust", "锐化 %.0f%%" % (_amount(t, 0.3) * 100),
                       {"sharpness": 1.0 + _amount(t, 0.3)})

    # ---- 亮度 / 对比度 / 饱和度 ----
    # ⚠⚠ 顺序陷阱（2026-09-24 差点写错）：
    #   "太暗了"这种句子**含"暗"字但意图是"调亮"**。如果先判"暗"，
    #   它会被返回成"调暗"——正好把图搞得更黑，是最坏的那种错。
    #   所以把"抱怨太暗/不够亮"的说法**放在"暗"前面单独判**。
    if _one_of(t, ("太暗", "有点暗", "太黑了", "太黑", "暗了", "偏暗", "好暗",
                   "不够亮", "照不亮", "看不清", "好黑", "有点黑", "发黑",
                   "曝光不足", "欠曝", "加点光", "亮不起来", "太阴", "太闷",
                   "黑乎乎", "黑漆漆", "乌漆嘛黑", "看不清楚")):
        a = _amount(t, 0.2)
        return Command("adjust", "调亮 %.0f%%" % (a * 100), {"brightness": 1.0 + a})
    # ⚠ 对称地："过曝/太亮"是**调暗**。放在"亮"之前判，否则"太亮"会被
    #   下面的"亮"组抓走、返回"调亮"——正好把图搞得更白（和"太暗"同款陷阱）。
    if _one_of(t, ("过曝", "曝光过度", "太亮", "有点亮", "偏亮", "白得",
                   "白花", "过亮", "亮过头", "有点白", "曝光太高", "曝光高了",
                   "曝光过了", "太白了", "泛白", "发白", "高光溢出",
                   "亮得晃眼", "刺眼", "耀眼", "亮瞎")):
        a = _amount(t, 0.2)
        return Command("adjust", "调暗 %.0f%%" % (a * 100),
                       {"brightness": max(0.0, 1.0 - a)})
    if _one_of(t, ("暗", "发暗", "沉一点", "偏黑", "压暗")):
        a = _amount(t, 0.2)
        return Command("adjust", "调暗 %.0f%%" % (a * 100),
                       {"brightness": max(0.0, 1.0 - a)})
    if _one_of(t, ("亮", "提亮", "亮一点", "亮一些", "亮点", "亮堂", "打光")):
        a = _amount(t, 0.2)
        return Command("adjust", "调亮 %.0f%%" % (a * 100), {"brightness": 1.0 + a})
    if _one_of(t, ("对比", "对比度")):
        a = _amount(t, 0.2)
        if _one_of(t, ("低", "弱", "减少", "降", "小", "淡", "平")):
            return Command("adjust", "减弱对比 %.0f%%" % (a * 100),
                           {"contrast": max(0.0, 1.0 - a)})
        return Command("adjust", "加强对比 %.0f%%" % (a * 100), {"contrast": 1.0 + a})
    # ⚠ 饱和度这一组顺序也很讲究："淡/灰/素"既可能说颜色淡、也可能说别的。
    #   先判"加饱和"（因为"太浓了"也是要求减饱和，得先拦），再判"减饱和"。
    if _one_of(t, ("鲜艳", "饱和", "颜色浓", "加色", "颜色重", "更艳", "浓一点",
                   "颜色深一点", "色彩浓", "颜色饱和", "画面更艳", "艳一点",
                   "加饱和度", "颜色艳丽", "艳些", "浓一些", "色彩丰富",
                   "颜色再重", "提饱和")):
        a = _amount(t, 0.3)
        return Command("adjust", "加饱和 %.0f%%" % (a * 100), {"color": 1.0 + a})
    if _one_of(t, ("褪色", "发灰", "褪成", "减饱和", "去饱和", "变得灰", "灰了",
                   "颜色淡", "颜色浅", "不鲜艳", "太艳了", "颜色过重", "太浓了",
                   "减点色", "去掉一点颜色", "素一点", "太素了", "素色", "淡一点",
                   "淡一些", "颜色太淡", "颜色太重", "太浓", "太艳", "单薄",
                   "色彩少", "颜色收一点", "降饱和", "灰扑扑", "颜色太满")):
        a = _amount(t, 0.3)
        return Command("adjust", "褪色 %.0f%%" % (a * 100), {"color": max(0.0, 1.0 - a)})

    # ---- 放大 / 缩小（放最后，因为"放大"这个词最容易和别的撞）----
    if _one_of(t, ("放大", "变大", "大一点", "放大一点", "变大一点",
                   "拉大", "尺寸大", "分辨率高", "调大")):
        n = _number(t)
        pct = n * 100 if n else 200.0
        if n and n <= 1:                     # 写 0.5 这种 → 当倍数
            pct = n * 100
        if n and n > 1 and n < 20:           # 写"两倍 / 2" → 倍数
            pct = n * 100
        if n and n >= 20:                    # 写"150" → 当百分比
            pct = n
        return Command("resize_pct", "放大到 %.0f%%" % pct, {"pct": pct})
    if _one_of(t, ("缩小", "变小", "小一点", "缩小一点", "变小一点",
                   "尺寸小", "调小", "弄小", "改小", "压缩一下", "压小",
                   "尺寸降", "分辨率降")):
        n = _number(t)
        if n is None:
            pct = 50.0
        elif n <= 1:                         # "一半" → 0.5
            pct = n * 100
        elif n < 20:                         # "0.5倍"? 越过；"2倍"不可能是缩小
            pct = 100.0 / max(n, 0.01)
        else:
            pct = n
        return Command("resize_pct", "缩小到 %.0f%%" % pct, {"pct": pct})

    return None


#: 菜单/弹窗里"它能听懂什么"用的例子（也是测试的输入来源）。
def examples() -> list:
    return [
        # 亮度
        "调亮一点", "太暗了", "调暗 30", "有点过曝", "曝光太高", "白得刺眼",
        # 色彩
        "变黑白", "鲜艳一点", "太艳了", "像老照片", "颜色太淡", "素一点",
        # 几何
        "转一下", "左转", "旋转 180 度", "水平翻转", "裁剪一下", "裁掉多余的边",
        # 尺寸
        "放大两倍", "缩小一半", "弄小一点", "改小",
        # AI
        "把背景去掉", "变清晰", "变高清", "太糊了",
        # 背景 / 人脸 / 增强
        "背景虚化", "换成白底", "蓝底证件照", "人脸修复", "智能增强", "自动调色",
        # 其它
        "锐化一下", "清晰一点", "搞错了", "撤销", "变回原来",
    ]


def apply_command(tool, cmd: Command) -> tuple:
    """把 :class:`Command` 落到界面上。返回 ``(做没做成, 给人看的一句话)``。

    这里只做**分派**，不带任何判断逻辑 —— 判断全在 :func:`parse_command` 里，
    这样两边都能被单独测。
    """
    if cmd is None:
        return False, "没听懂"
    if cmd.kind == "refused":
        # 不装傻、不解释、不纠缠：把边画清楚，然后把话头递回改图上。
        return False, "我只管改图 —— 说「调亮一点」「把背景去掉」这种，我立刻就做"
    if tool.image is None and cmd.kind not in ("undo", "redo"):
        return False, "先打开一张图片"
    try:
        if cmd.kind == "undo":
            tool.undo()
            return True, "已撤销"
        if cmd.kind == "redo":
            tool.redo()
            return True, "已重做"
        if cmd.kind == "rotate_cw":
            return bool(tool._edit(ops.rotate_cw, cmd.label)), cmd.label
        if cmd.kind == "rotate_ccw":
            return bool(tool._edit_ccw()), cmd.label
        if cmd.kind == "rotate_180":
            return bool(tool._edit(ops.rotate_180, cmd.label)), cmd.label
        if cmd.kind == "rotate_any":
            deg = float(cmd.params["deg"])

            def _do_rot(im, deg=deg):
                return ops.rotate_any(im, deg)
            return bool(tool._edit(_do_rot, cmd.label)), cmd.label
        if cmd.kind == "flip_h":
            return bool(tool._edit(ops.flip_h, cmd.label)), cmd.label
        if cmd.kind == "flip_v":
            return bool(tool._edit(ops.flip_v, cmd.label)), cmd.label
        if cmd.kind == "grayscale":
            return bool(tool._edit(ops.grayscale, cmd.label)), cmd.label
        if cmd.kind == "adjust":
            kw = dict(cmd.params)

            def _do(im, kw=kw):
                return ops.adjust(im, **kw)
            return bool(tool._edit(_do, cmd.label)), cmd.label
        if cmd.kind == "resize_pct":
            tool.resize_to_pct(float(cmd.params["pct"]))
            return True, cmd.label
        if cmd.kind == "crop":
            tool._show_crop()
            return True, "打开裁剪（用鼠标框）"
        if cmd.kind == "autofix":
            return bool(tool._edit(ops.autocontrast, cmd.label)), cmd.label
        if cmd.kind == "bg_blur":
            tool._bg_blur()
            return True, cmd.label
        if cmd.kind == "bg_white":
            tool._bg_replace((255, 255, 255))
            return True, cmd.label
        if cmd.kind == "bg_blue":
            tool._bg_replace((0, 71, 171))
            return True, cmd.label
        if cmd.kind == "bg_red":
            tool._bg_replace((220, 30, 40))
            return True, cmd.label
        if cmd.kind == "ai_cap":
            tool._run_ai_cap(str(cmd.params["cap"]))
            return True, cmd.label
    except Exception as exc:                                   # noqa: BLE001
        # 失败了要吭声（窗口程序没有控制台，静默等于"点了没反应"）
        return False, "%s：%s" % (type(exc).__name__, exc)
    return False, "没听懂"


class CommandDialog(dialogs.BaseDialog):
    """「一句话改图」的小窗口：打一句话，回车就执行。

    只负责**收集**那句话并解析；真正落到图上由调用方拿 :attr:`result` 去做。
    这样这个窗口本身不碰图片状态，测起来也简单（构建 → 打字 → 回车）。
    """

    def __init__(self, parent, *, dx=110, dy=88):
        super().__init__(parent, "一句话改图", dx=dx, dy=dy)
        self.result = None
        self._build()

    def _build(self) -> None:
        body = self.body
        tk.Label(body, text="说一句话，它照着做 —— 例如：",
                 bg=BG_ELEV, fg=FG_TEXT, font=font(10),
                 anchor="w").pack(fill="x", padx=16, pady=(14, 4))
        tk.Label(body, text="　".join(examples()[:6]),
                 bg=BG_ELEV, fg=FG_DIM, font=font(9), anchor="w",
                 justify="left", wraplength=380).pack(fill="x", padx=16)
        tk.Label(body, text="　".join(examples()[6:]),
                 bg=BG_ELEV, fg=FG_DIM, font=font(9), anchor="w",
                 justify="left", wraplength=380).pack(fill="x", padx=16, pady=(0, 10))

        self.entry = tk.Entry(body, bg=BG_SUNKEN, fg=FG_TEXT, font=font(11),
                              relief="flat", insertbackground=FG_TEXT)
        self.entry.pack(fill="x", padx=16, ipady=6)
        self.entry.bind("<Return>", lambda _e: self._on_run())
        self.entry.focus_set()

        self.msg = tk.Label(body, text=" ", bg=BG_ELEV, fg=FG_BAD,
                            font=font(9), anchor="w", wraplength=380,
                            justify="left")
        self.msg.pack(fill="x", padx=16, pady=(6, 0))

        row = tk.Frame(body, bg=BG_ELEV)
        row.pack(fill="x", padx=16, pady=(8, 14))
        tk.Button(row, text="执行", command=self._on_run).pack(side="right")
        tk.Button(row, text="取消", command=self.cancel).pack(side="right", padx=(0, 8))

    def _on_run(self) -> None:
        """解析 —— **没听懂就停在这儿说清楚，绝不猜着做**。"""
        text = self.entry.get()
        cmd = parse_command(text)
        if cmd is None:
            self.msg.configure(
                text="没听懂这句话。上面那些说法都认；"
                     "要加别的说法，告诉作者一句就行。")
            return
        if cmd.kind == "refused":
            # 窗口不关、话说清楚：它只干改图这一件事，别的聊不动。
            self.msg.configure(
                text="这个我只管改图。说「调亮一点」「把背景去掉」这种，我立刻就做。")
            return
        self.result = cmd
        self._safe_destroy()

    def take(self):
        """取走解析结果（没执行过就是 None）。"""
        return self.result
