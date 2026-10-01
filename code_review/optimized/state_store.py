# -*- coding: utf-8 -*-
"""月度解锁状态：把"这个月已经过闸了"记在一个 JSON 里。

这个文件解决的唯一问题
==================================================================
需求是"每个月问朋友一次『天空属于什么』"，那么程序必须**记得**上个月
是不是已经问过、过没过。没有状态就没有"一个月"这个概念。

于是这是本项目**第一个、也是唯一一个**会往用户磁盘写配置的地方
（另一个是图片工具写用户自己点"保存"的那张图；再一个是要安装包时
下载到本地的那一份）。所有写盘点在 verify_payload.py 的
"写盘文件断言"里逐个列了出来 —— 这是有意为之，不是顺带：

    main.exe 会写盘的文件只有 state_store.py / image_tool.py / version_check.py
    三个，分别写 state.json、用户选定的输出图片、用户点过的更新包。

它对"安全"的贡献是零（请如实说明）
==================================================================
state.json 就在安装目录里，用户拿记事本就能改，把 ``unlocked_month``
改成下个月就绕过去了。这是**刻意的**：本项目不是在做正版保护，
而是在做一道题。一个能被人一眼看穿的机制，比一个假装很安全、
实际上只会把正常用户卡死的机制要好得多。

存放位置（2026-09-13 改：**用户桌面不许多出任何文件**）
==================================================================
1. 环境变量 ``HAAVIK_STATE_DIR``（给测试用，也方便朋友自己换位置）
2. ``%LOCALAPPDATA%\\HAAVIK Photo\\`` —— **默认**。用户看不见、也从来
   不会整理到它，"用这个软件 = 桌面只多一个 exe"这条体验由它保证。
   （旧需求"不藏、写在 exe 旁边"已被新需求取代：安装位置本来就是桌面，
   写在旁边等于往桌面扔文件。）
3. **安装目录**（程序 exe 旁边）—— 只有 %LOCALAPPDATA% 不可写时才退到这。

**出题的节奏**（2026-09-16 起）：**每 7 天问一次**，而且**版本一变就重新问**
（更新完也要问一遍）。只有打开程序时才判断，不做后台定时。
详见 :func:`is_unlocked`。

**老状态迁移**：v1.2.0 之前状态写在 exe 旁边。首次运行时若新位置没有
状态、而旧位置有，就把旧的搬过来再删掉旧文件 —— 用户不该因为这次
"搬家"被重新问一遍题。迁移只在无环境变量覆盖时进行。

写入一律"先写临时文件再 os.replace"，中途失败不会在用户机器上留下
一个半截的 JSON（否则下次启动读到一个截断的文件，会莫名其妙地
"这个月又要重新答题"）。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from datetime import date, datetime

import serial_check  # 月份的定义只有一处：serial_check.month_epoch/month_label

log = logging.getLogger("haavik.state")

#: 目录名。installer.py 里那颗 APP_NAME 与 main.py 的 APP_TITLE 是同一串字，
#: build.py 的 assert_shared_names() 会断言三处一致，不允许各自漂移。
APP_DIRNAME = "HAAVIK Photo"

STATE_FILENAME = "state.json"
SCHEMA = 1
ENV_OVERRIDE = "HAAVIK_STATE_DIR"

KEY_SCHEMA = "schema"
KEY_FIRST_SEEN = "first_seen"
KEY_UNLOCKED_MONTH = "unlocked_month"      # 旧字段：只保留给老状态文件，不再参与判断
KEY_UNLOCKED_AT = "unlocked_at"            # ISO 日期，如 "2026-09-16"
KEY_UNLOCKED_VERSION = "unlocked_version"  # 上次过闸时的程序版本
KEY_UNLOCK_COUNT = "unlock_count"
KEY_LAST_VIA = "last_unlock_via"
KEY_QUIZ_ASKED = "quiz_asked_count"
KEY_SERIAL_TRIES = "serial_attempts"
KEY_LOCKED_AT = "locked_at"                # 答错被锁的时间；空 = 没被锁
#: 用户同意过第几版"反馈隐私说明"（0 = 没同意过）。
#: 存版本号而不是布尔值：说明改了内容就把版本加一，所有人重新被问一次 ——
#: "以前点过一次同意"不能当永久许可用。
KEY_FEEDBACK_CONSENT = "feedback_consent_version"
KEY_FEEDBACK_CONSENT_AT = "feedback_consent_at"   # 同意的日期（备查）
#: 已安装的「AI 模型组件」版本（字符串，如 "1.0.0"）。空 = 没装 / 没下过。
#: 与核心程序版本号相互独立：AI 模型包可能上 G，只在它自己迭代时才重下，
#: 所以得有自己的一份"装到哪一版了"记录。
KEY_AI_COMPONENT_VERSION = "ai_component_version"
#: 用户上次**成功发过反馈**时留的回信邮箱（"留过"才记，没留或校验不过 = 空）。
#: 用法：反馈框打开时预填；用户想换就删掉重填。
#: ⚠ 读出来**必须**再过一遍 :func:`feedback_send.is_valid_reply_email` —— state.json
#: 是普通文件，被外部改成攻击串（"a@b.com\\r\\nBcc: x@y.com"）是有可能的；
#: 不二次校验就把这个值塞进 Reply-To，等于把攻击串塞进邮件头。
KEY_LAST_REPLY_EMAIL = "last_reply_email"
#: 上一次**成功检查**时，远端清单里 AI 组件的版本号。
#:
#: 为什么要单独记一个：菜单是**同步**拼出来的，绝不能在拼菜单时联网
#: （点一下「AI」不该等网络）。可"要不要更新"又只有远端知道。
#: 折中办法就是"把上次查到的远端版本记下来"，菜单拿它跟已装版本比一下，
#: 就能直接写出「有更新 v1.4.0」而不用再点一次。
#: ⚠ 它**只是缓存的提示**，真要不要更新仍以联网查到的那一刻为准；
#:   拿不到就老实说"点此检查更新"，不许拿旧值冒充。
KEY_AI_REMOTE_VERSION = "ai_remote_version"

#: 隔多久重新问一次。2026-09-16 从"每月一次"改成**每 7 天一次**。
#: 判定用"距上次过闸的自然日天数"，不是 168 小时 —— 用户感知的是
#: "过了一周没有"，而不是分钟级精度。
QUIZ_PERIOD_DAYS = 7

VIA_QUIZ = "quiz"
VIA_SERIAL = "serial"

_cached_path: str | None = None


# --------------------------------------------------------------------------
# 位置
# --------------------------------------------------------------------------
def app_dir() -> str:
    """程序自己所在的目录。

    * 打包后（onefile）：``sys.executable`` 就是安装目录里那个 exe ——
      注意**不是** ``sys._MEIPASS``（那是每次启动都会换的临时解包目录，
      写进去等于写进垃圾桶）；
    * 源码运行时：本文件所在目录。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _is_writable(directory: str) -> bool:
    """真去写一个探针文件，而不是只看 os.access。

    os.access 在 Windows 上对"目录存在但没有写权限"这种情况并不可靠，
    而且它会把只读属性、ACL 拒绝等混在一起。花一次 open() 换一个确定的答案。
    """
    try:
        os.makedirs(directory, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".write_probe", dir=directory)
        os.close(fd)
        os.remove(probe)
        return True
    except OSError:
        return False


def candidate_dirs() -> list[str]:
    override = os.environ.get(ENV_OVERRIDE)
    if override:
        # 显式指定就只用它，不要"偷偷回退到别处"—— 否则测试会以为测到了 A，
        # 实际数据写在 B，而两者都"看起来正常"。
        return [override]
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    # 默认 %LOCALAPPDATA%（桌面上不许多出文件）；exe 旁边只是兜底。
    return [os.path.join(local, APP_DIRNAME), app_dir()]


def legacy_state_path() -> str | None:
    """v1.2.0 之前的状态文件位置（exe 旁边）。只用于一次性迁移。"""
    d = app_dir()
    return os.path.join(d, STATE_FILENAME) if d else None


def migrate_legacy_state() -> str | None:
    """把旧位置的状态搬到新位置，然后删掉旧文件。

    返回迁移说明（没迁移就返回 None）。三条纪律：
      * 只在有环境变量覆盖之外才做（测试自己管自己的目录）；
      * 只碰 ``state.json`` 这一个具名文件，绝不递归删目录；
      * 新位置已经有状态时**什么都不动**（新状态是权威，旧的当成垃圾）。
    """
    if os.environ.get(ENV_OVERRIDE):
        return None
    new_path = state_path()
    old_path = legacy_state_path()
    if not new_path or not old_path or old_path == new_path:
        return None
    if not os.path.isfile(old_path):
        return None
    if os.path.isfile(new_path):
        log.info("新旧位置都有状态文件，以新位置为准（%s）", new_path)
        return None
    try:
        with open(old_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            log.warning("旧状态文件不是对象，不迁移：%s", old_path)
            return None
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        save(raw)
        if os.path.isfile(new_path):
            os.remove(old_path)     # 只删这一个具名文件
            log.info("状态已从 %s 迁移到 %s（旧文件已删除）", old_path, new_path)
            return old_path
    except (OSError, ValueError) as exc:
        # 迁移失败不致命：旧的还在原地，程序照常按"全新状态"出题
        log.warning("旧状态迁移失败（%s）：%s", old_path, exc)
    return None


def state_dir() -> str:
    """返回实际存放目录（首次调用时探测一次并缓存）。"""
    global _cached_path
    if _cached_path is None:
        for d in candidate_dirs():
            if _is_writable(d):
                _cached_path = d
                break
        else:
            # 三个位置都不给写：退回内存态。绝不因为存不了状态就让程序打不开 ——
            # 那会把"这个月的题"变成"永久打不开的软件"。
            log.error("没有任何可写目录（%s），本次运行不保存状态", candidate_dirs())
            _cached_path = ""
    return _cached_path


def state_path() -> str | None:
    d = state_dir()
    return os.path.join(d, STATE_FILENAME) if d else None


def set_state_dir_for_tests(directory: str) -> None:
    """测试专用：强制指定目录并清掉缓存。"""
    global _cached_path
    os.environ[ENV_OVERRIDE] = directory
    _cached_path = None


# --------------------------------------------------------------------------
# 读写
# --------------------------------------------------------------------------
def empty_state() -> dict:
    return {
        KEY_SCHEMA: SCHEMA,
        KEY_FIRST_SEEN: datetime.now().isoformat(timespec="seconds"),
        KEY_UNLOCKED_MONTH: "",
        KEY_UNLOCKED_AT: "",
        KEY_UNLOCKED_VERSION: "",
        KEY_UNLOCK_COUNT: 0,
        KEY_LAST_VIA: "",
        KEY_QUIZ_ASKED: 0,
        KEY_SERIAL_TRIES: 0,
        KEY_LOCKED_AT: "",
        # ⚠️ 加了新字段**必须同时加进这里**：load() 只保留"这个字典里出现过
        # 的键"（防止状态文件里塞进任意东西）。只在上面定义 KEY_ 常量而忘了
        # 加进来，写进去的值读回来永远是默认值 —— 而且**不报错**，
        # 只会表现成"设置好像没生效"。（v1.5.1 加反馈同意时踩过一次。）
        KEY_FEEDBACK_CONSENT: 0,
        KEY_FEEDBACK_CONSENT_AT: "",
        # AI 模型组件版本：没装就是空串。加新字段必须同时加进这里（见上方说明）。
        KEY_AI_COMPONENT_VERSION: "",
        # 上次成功发反馈时留的回信邮箱（空 = 第一次或被用户清掉过）。
        KEY_LAST_REPLY_EMAIL: "",
        # 上次查到的远端 AI 版本（菜单提示用；空 = 没查过）。
        KEY_AI_REMOTE_VERSION: "",
    }


def load() -> dict:
    """读状态。任何异常都退化成"全新状态"，但一定记日志，绝不静默。"""
    migrate_legacy_state()      # v1.2.0 起状态搬去 %LOCALAPPDATA%，只搬一次
    path = state_path()
    if not path or not os.path.isfile(path):
        return empty_state()

    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("状态文件读不了（%s），按全新状态处理：%s", path, exc)
        return empty_state()

    if not isinstance(raw, dict):
        log.warning("状态文件结构不对（%s 不是对象），按全新状态处理", path)
        return empty_state()

    state = empty_state()
    state.update({k: v for k, v in raw.items() if k in state})
    try:
        if int(raw.get(KEY_SCHEMA, 0)) != SCHEMA:
            log.warning("状态文件 schema=%r 与当前 %d 不符，只取能认得的字段",
                        raw.get(KEY_SCHEMA), SCHEMA)
    except (TypeError, ValueError):
        log.warning("状态文件 schema 不是数字：%r", raw.get(KEY_SCHEMA))
    return state


def save(state: dict) -> bool:
    """原子写回。返回是否成功（失败只记日志，调用方不该因此中断使用）。"""
    path = state_path()
    if not path:
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".tmp",
                                   dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        log.debug("状态已写入 %s", path)
        return True
    except OSError as exc:
        log.warning("状态写不进去（%s）：%s", path, exc)
        return False


# --------------------------------------------------------------------------
# 闸门
# --------------------------------------------------------------------------
def is_unlocked(state: dict, today: date | None = None,
                version: str | None = None) -> bool:
    """过闸是否仍然有效。

    两个条件**同时**成立才算"不用再问"：

    1. 距上次过闸的自然日不到 :data:`QUIZ_PERIOD_DAYS` 天；
    2. **版本没变过** —— 用户的要求："新下载完的软件，就算是更新也要询问"。
       更新意味着换了新版，那就重新问一次（`version` 传 None 表示不检查这条，
       给只关心时间的调用方和测试用）。

    两个条件任何一条不满足都会重新出题；老状态文件里没有 `unlocked_at`
    字段（v1.2.x 及以前存的是月份），自然也会被判为"需要重新问"。
    """
    stamp = str(state.get(KEY_UNLOCKED_AT) or "")
    if not stamp:
        log.info("尚未过闸（没有记录）")
        return False
    if version is not None and str(state.get(KEY_UNLOCKED_VERSION) or "") != str(version):
        log.info("版本已变（记录 %r / 当前 %r）→ 重新出题",
                 state.get(KEY_UNLOCKED_VERSION), version)
        return False
    try:
        last = date.fromisoformat(stamp)
    except ValueError:
        log.warning("过闸日期看不懂：%r → 当作未过闸", stamp)
        return False
    days = ((today or date.today()) - last).days
    if days < 0:                                   # 系统时间被往回调过
        return True
    if days >= QUIZ_PERIOD_DAYS:
        log.info("距上次过闸 %d 天（≥%d）→ 重新出题", days, QUIZ_PERIOD_DAYS)
        return False
    log.info("过闸仍有效：第 %d 天（上限 %d 天）", days, QUIZ_PERIOD_DAYS)
    return True


#: 锁定状态：答错了就得输序列号才能继续。
#: 这里刻意**不设自动过期**，因为使用者的原话是"我关闭程序再打开，又回到问题了"
#: —— 那正是他要修的行为：锁了就得是锁着，关掉程序、重启电脑、装个新版都逃不掉。
#: 唯一解开的方式是**输入正确的序列号**（mark_unlocked 会顺手清掉它）。
#: 对朋友也不是死路：锁定页上就写着生成器在哪，那是"作者留的那道题"。
def is_locked(state: dict) -> bool:
    """是不是处在"答错了、要输序列号"的锁定状态。"""
    return bool(str(state.get(KEY_LOCKED_AT) or ""))


def mark_locked(state: dict) -> dict:
    """记下"被锁了"。

    已经锁着就不重复写盘 —— 反复刷新时间戳会让"锁了多久"这类判断失去意义，
    也会平白多写几次文件。
    """
    if is_locked(state):
        log.info("已经被锁着（%s），保持原样", state.get(KEY_LOCKED_AT))
        return state
    state[KEY_LOCKED_AT] = datetime.now().isoformat(timespec="seconds")
    save(state)
    log.info("已锁定：答错了，需要序列号才能继续（%s）", state[KEY_LOCKED_AT])
    return state


def mark_unlocked(state: dict, via: str, today: date | None = None,
                  version: str | None = None) -> dict:
    """记下"刚过闸"。返回同一个 dict（就地更新，便于链式调用）。

    ``version`` 由调用方传进来（state_store **不 import version_check** ——
    那会把联网模块拖进依赖图，installer 也跟着变大）。
    """
    state[KEY_UNLOCKED_AT] = (today or date.today()).isoformat()
    if version is not None:
        state[KEY_UNLOCKED_VERSION] = str(version)
    state[KEY_LAST_VIA] = via
    state[KEY_UNLOCK_COUNT] = int(state.get(KEY_UNLOCK_COUNT, 0)) + 1
    # 过了闸就等于解开锁 —— 在这一处清，所有解锁路径都不会漏
    # （答对题目、输对序列号都走这里）。
    state[KEY_LOCKED_AT] = ""
    save(state)
    log.info("已过闸：%s 方式=%s 版本=%s 累计 %s 次（顺手解除了锁定）",
             state[KEY_UNLOCKED_AT], via, state.get(KEY_UNLOCKED_VERSION, "-"),
             state[KEY_UNLOCK_COUNT])
    return state


def reset_gate(state: dict) -> dict:
    """清掉"已过闸" = 下次启动重新出题。

    两个调用点共用这一份实现：命令行 ``--reset-gate``，以及图片工具页
    左下角白点的隐藏入口（口令通过后重启）。只清过闸信息 ——
    累计次数、首次见到时间这些统计留着，它们不影响出题。
    锁定状态也一起清掉：白点入口的含义就是"从头再来一遍"。
    """
    state[KEY_UNLOCKED_AT] = ""
    state[KEY_UNLOCKED_VERSION] = ""
    state[KEY_UNLOCKED_MONTH] = ""
    state[KEY_LOCKED_AT] = ""
    save(state)
    log.info("已清掉过闸记录，下次启动会重新出题")
    return state


def note_quiz_asked(state: dict) -> dict:
    state[KEY_QUIZ_ASKED] = int(state.get(KEY_QUIZ_ASKED, 0)) + 1
    save(state)
    return state


def note_serial_attempt(state: dict) -> dict:
    state[KEY_SERIAL_TRIES] = int(state.get(KEY_SERIAL_TRIES, 0)) + 1
    save(state)
    return state


def set_last_reply_email(state: dict, value: str) -> dict:
    """反馈成功后把"用户留的回信邮箱"记下来；下次打开预填。

    ⚠ 这里**不校验**：调用方拿到的 ``value`` 是用户刚刚亲手点过发送的那个串，
    程序自己走的路径（``composed()`` → ``send_via_smtp``）已经过
    :func:`feedback_send.is_valid_reply_email`，这里再校验一次是浪费。
    校验的**真正关口**在读出来塞进 UI / Reply-To 时（见 FeedbackDialog 预填处）。

    空串也照存 —— "用户主动删空"和"从没填过"对下次预填来说是一回事：
    都是"不预填"（空串本身在 RoundEntry 里就是"没东西"）。
    """
    state[KEY_LAST_REPLY_EMAIL] = str(value or "").strip()
    save(state)
    return state


def get_last_reply_email(state: dict) -> str:
    """读出上次记的回信邮箱。**过一遍校验**才返回，state.json 不被信任。"""
    raw = str(state.get(KEY_LAST_REPLY_EMAIL) or "")
    if not raw:
        return ""
    # 把校验塞在这里而不是调用方 —— 任何读到这个字段的地方都自动受益。
    # 校验失败的返回值是空串（= 不预填），不报错：state.json 字段被外部改成
    # 攻击串时，UI 上看上去就是"没填过"，不破坏程序也不吓到用户。
    try:
        from feedback_send import is_valid_reply_email              # noqa: PLC0415
        return raw if is_valid_reply_email(raw) else ""
    except Exception:                                              # noqa: BLE001
        return ""


def feedback_consent_version(state: dict) -> int:
    """用户同意过第几版反馈隐私说明（0 = 从没同意过）。"""
    try:
        return int(state.get(KEY_FEEDBACK_CONSENT, 0) or 0)
    except (TypeError, ValueError):
        return 0


def needs_feedback_consent(state: dict, current: int) -> bool:
    """要不要先弹同意书。

    存的是**版本号**而不是一个 True：将来隐私说明改了内容，把版本加一，
    所有人都会被重新问一次 —— 而不是拿着"以前同意过"当永久许可。
    """
    return feedback_consent_version(state) < int(current)


def mark_feedback_consent(state: dict, version: int) -> dict:
    state[KEY_FEEDBACK_CONSENT] = int(version)
    state[KEY_FEEDBACK_CONSENT_AT] = date.today().isoformat()
    save(state)
    return state


def ai_component_version(state: dict) -> str:
    """已安装的 AI 模型组件版本（空串 = 没装）。"""
    return str(state.get(KEY_AI_COMPONENT_VERSION) or "")


def ai_remote_version(state: dict) -> str:
    """上次成功检查时，远端 AI 组件的版本号（空串 = 还没查过/查不到）。"""
    return str(state.get(KEY_AI_REMOTE_VERSION) or "")


def set_ai_remote_version(state: dict, version: str) -> dict:
    """记下"上次查到的远端 AI 版本"。**只是缓存的提示**，见键定义的注释。

    联网查到的那一刻由 ``ai_ops`` 调用；写盘失败不影响主流程
    （菜单顶多少显示一句提示，不该让更新流程崩掉）。
    """
    state[KEY_AI_REMOTE_VERSION] = str(version or "")
    save(state)
    return state


def set_ai_component_version(state: dict, version: str) -> dict:
    """记下"AI 模型组件装到了哪一版"，并写盘。

    ``ai_ops`` 在解包完模型、校验通过后调用；版本号来自远端清单里那个
    ``ai`` 组件的 ``version`` 字段，与核心程序版本号完全独立。
    """
    state[KEY_AI_COMPONENT_VERSION] = str(version)
    save(state)
    log.info("已记录 AI 模型组件版本：%s", version)
    return state


def describe(state: dict) -> str:
    """一行人类可读的摘要，给日志和调试用。"""
    return (
        "首次见到 %s；上次过闸=%s（版本 %s，累计 %s 次，最近方式=%s；"
        "有效期 %s 天）；被问过 %s 次；序列号尝试 %s 次；锁定=%s；"
        "AI 组件=%s"
        % (
            state.get(KEY_FIRST_SEEN, "?"),
            state.get(KEY_UNLOCKED_AT) or "否",
            state.get(KEY_UNLOCKED_VERSION) or "-",
            state.get(KEY_UNLOCK_COUNT, 0),
            state.get(KEY_LAST_VIA) or "-",
            QUIZ_PERIOD_DAYS,
            state.get(KEY_QUIZ_ASKED, 0),
            state.get(KEY_SERIAL_TRIES, 0),
            state.get(KEY_LOCKED_AT) or "否",
            ai_component_version(state) or "未装",
        )
    )
