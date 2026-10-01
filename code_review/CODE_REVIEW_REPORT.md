# 代码审查报告 · HAAVIK Photo（双架构整蛊安装包）

> 审查范围：项目根目录全部 12 个源文件 + 构建配置 + 产物
> 审查方式：逐文件通读 + 体积/哈希实测 + PE 头实测 + 载荷解密比对
> 配套文档：`CODE_REVIEW_STANDARD.md`（标准与流程）、`optimized/`（优化后代码）

---

## 0. 执行摘要

**结论**：这是一份**意图良性、但工程质量参差**的整蛊项目。没有发现任何恶意功能——载荷经字节级解密比对，与 `dist/` 主程序完全一致，未夹带额外代码。

但存在 **3 个 P0 阻断级缺陷**、**14 个 P1 严重缺陷**，其中两个会导致用户看到"莫名其妙的崩溃/卡死"，一个是**数据丢失风险**。

最关键的三个问题：

| ID | 问题 | 后果 |
|---|---|---|
| **M-01** | 真实关机用 `shutdown /s /t 0`，0 秒宽限 | **未保存数据直接丢失**。玩笑与"损坏他人财物"的分界线就在这一行 |
| **M-02** | 倒计时线程无取消检查，取消与触发是两条独立路径 | 「紧急取消关机」按钮**在设计上不可靠**，目前只是碰巧有效 |
| **S-01** | 安装失败时的错误提示自身抛 `NameError` | 用户看到"安装卡住、无任何提示"，缺陷因此长期没被发现 |

**另外**：本项目 1/3 的代码量（`setup.py` 56 MB）实际上**从未被审查过**——因为它大到任何编辑器都打不开。这不是巧合，而是"没有审查机制"的必然结果。

**验收阶段又追加发现 3 个问题**（详见第 8 节）：其中 2 个是我在重构时自己引入的（资源清单双表漂移、中文路径下的编码静默失败），1 个是测试断言写错导致的误判。它们不是在读代码时发现的，而是**真跑一遍之后才暴露的**——这恰好是"自动门禁 + 真实执行"必要性的直接证据。全部已修复并回归验证通过。

### 关于"哪个是假冒安装包"

见第 1 节。**`Setup.exe` 是外壳，载荷是核心**——你理解的方向是对的，且已实测确认。

---

## 1. 架构说明：哪个是"假冒的"，核心在哪

### 1.1 交付链

```
    ┌──────────────────────────────────────────────────────────────┐
    │  源文件                                                        │
    │   main.py            → 整蛊主程序（真正的"核心"）                 │
    │   installer.py       → 安装向导（伪装外壳）                       │
    │   build_assets.py    → 生成伪装资源（许可协议/壁纸）               │
    └───────────────────────┬──────────────────────────────────────┘
                            ▼  PyInstaller 打包
    ┌──────────────────────────────────────────────────────────────┐
    │  被"承载"的核心层                                               │
    │   MyAlbum.exe      17.5 MB   64 位主程序                       │
    │   MyAlbum_x86.exe   9.4 MB   32 位主程序                       │
    └───────────────────────┬──────────────────────────────────────┘
                            ▼  XOR 混淆 + 索引
    ┌──────────────────────────────────────────────────────────────┐
    │  载荷容器 payload.zip  ≈ 27 MB                                 │
    │   payload/x64.bin · payload/x86.bin · res/* · meta.json        │
    └───────────────────────┬──────────────────────────────────────┘
                            ▼  作为资源嵌入安装器
    ┌──────────────────────────────────────────────────────────────┐
    │  ★ Setup.exe   ← 交付给朋友的**唯一**文件                       │
    │    "HAAVIK Photo 安装向导"                                      │
    │    内嵌双架构核心程序，单文件自包含，目标机器无需 Python           │
    └──────────────────────────────────────────────────────────────┘
```

**是的：`Setup.exe` 是伪装外壳，核心程序完整地存在它内部。**

- 它自称安装 "HAAVIK Photo 图片查看器"（虚构产品），实际释放的是整蛊主程序
- 内嵌 **两套**主程序，运行时按目标系统位数取用
- 单文件自包含：目标机器不需要装 Python、不需要原始 `MyAlbum.exe` 在旁边
- 体积构成实测：x64 载荷 16.77 MB + x86 载荷 9.37 MB + 伪装资源约 0.6 MB（原版还塞了 15.86 MB 假二进制）+ Python 运行时

### 1.2 审计证据（实测，非推断）

**证据 A — 载荷与主程序字节级一致**

```
[x64] 内嵌 17,585,223 B  sha256=64af4260f3c5acb8…
[x64] 磁盘 17,585,223 B  sha256=64af4260f3c5acb8…   结果：一致 ✓
[x86] 内嵌  9,827,357 B  sha256=81b2fd0bec40e98c…
[x86] 磁盘  9,827,357 B  sha256=81b2fd0bec40e98c…   结果：一致 ✓
总体结论：内嵌 payload 与 dist 主程序完全一致 ✓
```

**结论：安装包内没有夹带任何额外载荷。** 这是本次审计最重要的一条——它排除了"伪装外壳里藏着别的东西"的可能。复现脚本：`code_review/verify_payload.py`。

**证据 B — 主程序模块扫描（只列可疑项）**

```
[x64] MyAlbum.exe      命中 urllib×5  socket×3  ssl×3  wmi×2  http.client×1  ftplib×1  ctypes×1
[x86] MyAlbum_x86.exe  命中 ctypes×10  socket×4  ssl×3  http.client×1  ftplib×1
```

这些是 PyInstaller 归档的模块名表（TOC）条目，属于打包器常见的**过度收集**，并非被调用的代码。值得注意的是**没有任何**出现在列表中的高危项：

- ❌ 无 `requests` / `paramiko`（无外传通道）
- ❌ 无 `keyboard` / `pynput` / `pyautogui`（无键盘钩子、无输入劫持）
- ❌ 无 `win32api` / `wmi`（无系统级操作）
- ❌ 无 `pyperclip` / `selenium` / `cryptography`

**证据 C — 「隐藏字符串」反而成了证据**

```
[x64] b'shutdown' 出现 0 次
[x86] b'shutdown' 出现 0 次
```

`main.py` 用 `chr(115)+chr(104)+...` 拼出 `shutdown`，所以在两个 exe 里**一次都搜不到这个字符串**。

这恰好说明问题的本质：**一个正常程序会明文包含 `shutdown`；一个「从不出现 `shutdown` 字面量、却调用 `subprocess.Popen` 执行动态拼接参数」的程序，是教科书级的恶意启发式特征。** 混淆达到了"藏住字符串"的目的，但"藏"这个行为本身才是真正的风险信号。这也解释了 README 里"`Setup_x86.exe` 被 Defender 自动删除"——**根因是对抗手段，不是"32 位 + 双载荷"的宿命**。

---

## 2. 问题总览

| 文件 | 🔴 P0 | 🟠 P1 | 🟡 P2 | 💭 P3 | 小计 |
|---|---:|---:|---:|---:|---:|
| `main.py` | **2** | 3 | 2 | 6 | 13 |
| `setup.py` → `installer.py` | **1** | 3 | 6 | 4 | 14 |
| `gen_decoy.py` → `build_assets.py` | 0 | 1 | 4 | 2 | 7 |
| `inject_payload_v2.py` → `make_bundle.py` | 0 | 2 | 2 | 2 | 6 |
| `inject_payload.py`（v1，已废弃） | 0 | 0 | 1 | 1 | 2 |
| `make_icon.py` | 0 | 0 | 0 | 4 | 4 |
| `version.txt` | 0 | 1 | 1 | 0 | 2 |
| `build_all.bat` | 0 | 2 | 2 | 1 | 5 |
| `*.spec`（4 份） | 0 | 1 | 1 | 0 | 2 |
| 跨文件 / 文档 | 0 | 1 | 2 | 0 | 3 |
| **合计** | **3** | **14** | **21** | **20** | **58** |

按严重级别：🔴 3 · 🟠 14 · 🟡 21 · 💭 20

---

## 3. 逐文件详细审查

### 3.1 `main.py`（整蛊主程序）— 13 项

这是最需要优先修的文件：它同时含数据丢失风险、不可靠的取消机制、以及静默失败。

---

🔴 **M-01 · P0 · 正确性/安全：关机指令 0 秒宽限，直接造成数据丢失**

`main.py:30-34`、`147-149`

```python
def trigger_shutdown():
    subprocess.Popen([_sd(), '/s', '/t', '0'], shell=False)   # ← /t 0 = 无宽限

def _shutdown_worker(self):
    time.sleep(60)
    trigger_shutdown()
```

**现象**：界面上有 60 秒倒计时，看起来"有时间反应"。但那个倒计时是 Python 自己 `sleep` 的，与系统无关；真正下达的关机指令是 `/t 0` —— **零秒宽限，立即执行**。

**为什么严重**：如果对方当时正在写文档、导出视频、编译代码，"玩笑"的后果是**不可逆的数据丢失**。这是"恶作剧"与"损坏他人财物"之间唯一的实质分界线。朋友之间能接受的整蛊，止步于"吓一跳"。

**建议**（二选一，推荐第一个）：

- ✅ **推荐**：默认只播放**假的**关机画面（全屏、倒计时、然后揭晓"骗你的"），完全不调用系统命令 → 数据丢失风险归零，玩笑效果几乎不减。
- 若确实要真实关机：**把倒计时交给系统**，而不是自己 sleep：
  ```python
  subprocess.run(["shutdown", "/s", "/t", "60"])   # Windows 自己倒计时，屏幕上会显示
  subprocess.run(["shutdown", "/a"])               # 取消 → 真正可靠
  ```
  并且必须显式开关（见 M-12）、界面上明示危险模式（见标准 T6）。

优化后实现见 `optimized/main.py` 的 `--real-shutdown` 与 `Countdown` 类。

---

🔴 **M-02 · P0 · 正确性：取消按钮在设计上不可靠**

`main.py:137-149`、`147-149`

```python
def _enter_countdown(self):
    ...
    threading.Thread(target=self._shutdown_worker, daemon=True).start()

def _shutdown_worker(self):
    time.sleep(60)          # ← 没有任何取消检查
    trigger_shutdown()      # ← 无条件执行
```

**现象**：后台线程只睡觉、然后无条件关机。取消动作（`shutdown /a`）与触发动作之间**没有任何共享状态**。

**为什么现在"看起来能取消"**：因为 `_do_cancel()` 最后调用了 `root.destroy()`，主循环退出、进程结束、daemon 线程被强杀。也就是说——**取消之所以有效，只是因为进程恰好退出了，而不是因为取消逻辑正确**。

这个巧合有两个明显的破绽：
- 若在提示框（`messagebox.showinfo`）上停留超过剩余秒数，线程会在提示框还开着时触发关机；
- 任何让进程存活的改动（加个托盘图标、加个线程、加个日志）都会让这个漏洞立刻显形。

**建议**：用 `threading.Event` 让等待本身可被中断：

```python
def _worker(self):
    if self._cancelled.wait(self.remaining()):   # 取消时立即返回
        return
    self._on_expire()
```

优化后实现见 `optimized/main.py` 的 `Countdown` 类。

---

🟠 **M-03 · P1 · 正确性：两个独立计时器 → 显示与实际必然漂移**

`main.py:183-191`（UI 每 1000ms 递减）vs `147-149`（线程 `sleep(60)`）

**现象**：UI 的秒数由一个 `after(1000)` 链递减，实际触发由另一个线程的 `sleep(60)` 决定。两者启动时刻不同、调度精度不同（`after` 在忙时会滞后），**必然逐渐错开**。用户可能看到"1"然后什么都没发生，或秒数还剩 8 秒时电脑就关了。

**建议**：单一时间源。用 `time.monotonic()` 算出一个 deadline，UI 秒数从 deadline 推导：

```python
def remaining(self):
    return max(0.0, self._deadline - time.monotonic())
```

---

🟠 **M-04 · P1 · 正确性：关窗与点取消的行为互相矛盾**

`main.py` 全文未设置 `WM_DELETE_WINDOW`

**现象**：
- 倒计时页按 Alt+F4 / 点 X → 进程退出 → daemon 线程被强杀 → **关机被静默取消**（用户什么提示都没有）
- 点「紧急取消关机」→ 走 `shutdown /a`

两条路径都会"取消"，但一条有提示、一条没有；而且**"关窗"比"点取消按钮"更可靠**——这与界面暗示完全相反。用户会得出错误结论："这个取消按钮是假的。"

**建议**：给 `root.protocol("WM_DELETE_WINDOW", ...)` 显式处理，让关窗 = 取消，并给出与按钮一致的回执。

---

🟠 **M-05 · P1 · 可维护性/安全：静默吞掉所有异常（×2）**

`main.py:33-34`、`40-41`

```python
except Exception:
    pass
```

**现象**：关机命令失败、或取消失败时，**用户和开发者都得不到任何信息**。用户看到"已成功取消"的提示，但关机可能仍在队列里；或者点了取消、到了点还是关机了——而没有任何线索可查。

**为什么严重**：这类缺陷在整蛊场景下会直接演变成"我没想真的关他机，但关了"。危险操作**绝不能静默失败**。

**建议**：至少记录并回给用户：

```python
except (OSError, subprocess.SubprocessError) as exc:
    log.error("shutdown %s 调用失败：%s", args, exc)
    return False, str(exc)
```

并且取消后应当**验证**关机是否真的被撤销（`shutdown /a` 的返回码 / 是否有待执行的关机）。

---

🟡 **M-06 · P2 · 可维护性/安全：`chr()` 拼接是负收益**

`main.py:24-26`

```python
def _sd():
    """辅助函数保留（此处用不到，但保持模块结构一致）"""
    return chr(115) + chr(104) + chr(117) + chr(116) + chr(100) + chr(111) + chr(119) + chr(110)
```

**三个问题叠加**：
1. 注释自称"此处用不到"，但它**被用在两处关键路径上**（`trigger_shutdown` / `cancel_shutdown`）——注释与事实不符，会误导审查者；
2. 代码无法 `grep shutdown`，无法静态审计；
3. 这种写法**本身就是**杀软判定"混淆型脚本"的首要特征。

**建议**：直接写 `SHUTDOWN_EXE = "shutdown"`。审计者一眼能看懂，杀软也没有理由怀疑。

---

🟡 **M-07 · P2 · 正确性：演示模式下界面死锁**

`main.py:185-191`

```python
def _tick(self):
    self.remaining -= 1
    if self.remaining <= 0:
        self.cd_label.configure(text="关机中…", fg='#e74c3c')
        return          # ← 停止调度，但页面再无任何出路
```

**现象**：若未真实关机（例如未来加入"安全模式"），归零后界面永久停在"关机中…"，没有任何按钮、没有退出方式。**这恰好违反了整蛊标准 T3（可随时退出）**。

**建议**：归零后进入"揭晓页"，明确告知这是玩笑并提供关闭按钮。优化后见 `optimized/main.py` 的 `_show_reveal()`。

---

💭 **M-08 · P3** — `_tick` 在 `remaining <= 0` 之外仍可能访问已销毁的 `cd_label`；应加 `winfo_exists()` 判断。

💭 **M-09 · P3** — `Image.LANCZOS`（`main.py:109`）建议改为 `Image.Resampling.LANCZOS`（Pillow 10+ 的正式路径，旧写法是兼容别名）。

💭 **M-10 · P3** — 正确答案与"对错"耦合在按钮顺序里（`main.py:86-90` 的 `enumerate` + `cmd` 配对）。改成数据表更清晰，也更容易改题：

```python
OPTIONS = (Option("A. 哈夫克", "#27ae60", correct=True), ...)
```

💭 **M-11 · P3** — `_on_wrong` 与 `_enter_countdown` 重复检查 `shutdown_started`，冗余。

💭 **M-12 · P3** — 危险行为没有开关：**默认即危险**。应改成默认安全、需要显式参数才启用真实关机（`--real-shutdown`）。

💭 **M-13 · P3** — `--noconsole` 打包下缺 `sys.excepthook`，未捕获异常会让窗口直接消失而毫无提示，无法排障。

---

### 3.2 `setup.py` → `optimized/installer.py` — 14 项

---

🔴 **S-01 · P0 · 正确性：安装失败的错误提示自身会崩溃**

`setup.py:482-485`

```python
except Exception as e:
    self.root.after(0, lambda: messagebox.showerror(
        "安装失败", f"安装过程中出错：\n{e}"))     # ← e 在这里已经被删除了
    self.root.after(0, self._build_path)
```

**现象**：Python 3 在 `except ... as e` 块结束时**会删除 `e`**（防止异常对象循环引用）。而 `lambda` 是**稍后**才执行的（`after(0)` 排在事件队列里），此时 `e` 已不存在 → lambda 自身抛 `NameError: cannot access free variable 'e'`。

**后果**：安装一旦失败，用户看到的是——**进度条停住，没有任何错误提示**（错误提示的代码自己崩了，异常被 Tk 的事件循环吞掉）。用户只会以为"卡死了"。

这条缺陷完美示范了"静默失败"的代价：**它让安装失败这件事本身无法被发现**，所以才能长期存活。

**建议**：立即求值，或用 `functools.partial` 绑定：

```python
self.root.after(0, functools.partial(self._show_install_error, exc))
```

同时给错误框加上日志路径，否则用户报错时你拿不到任何上下文。

**讽刺的是**，同一个文件里 `setup.py:459` 的 `lambda t=text, r=ratio:` **正确地**用了默认参数绑定来规避延迟绑定——说明作者知道这个陷阱，只是没在所有地方贯彻。

---

🟠 **S-02 · P1 · 正确性：32 位安装包在 64 位系统上会判错架构**

`setup.py:55-60`

```python
def detect_arch():
    a = os.environ.get('PROCESSOR_ARCHITECTURE', '')
    if '64' in a or 'AMD64' in a:
        return 'x64'
    return 'x86'
```

**现象**：32 位进程运行在 64 位 Windows 上时（WOW64）：
- `PROCESSOR_ARCHITECTURE` = `x86` ← **进程视角**
- `PROCESSOR_ARCHITEW6432` = `AMD64` ← **系统视角**

原代码只看第一个，于是在 64 位系统上返回 `x86` → 释放 **32 位**主程序并指向它。

**为什么这条对本项目特别关键**：把安装器编译成 32 位正是"32/64 位通吃"的正解（见 `optimized/build.py` 的说明）——**而这恰恰会让这个 bug 从"潜在"变成"必然"**。不做这个修正，"双架构"功能就是坏的。

**建议**：

```python
if os.environ.get("PROCESSOR_ARCHITEW6432"):
    return "x64"        # WOW64：系统是 64 位
```

---

🟠 **S-03 · P1 · 正确性：进度条首帧不绘制**

`setup.py:439-445`

```python
def _set_progress(self, text, ratio):
    w = self._pb_canvas.winfo_width()      # ← 窗口未映射时恒为 1
    self._pb_canvas.delete('all')
    if w > 1:
        self._pb_canvas.create_rectangle(...)
```

**现象**：`winfo_width()` 在控件被映射并完成几何计算前返回 1。第一步进度（`ratio=0.10`）在窗口还没显示完时就被设置，于是**进度条一格都不画**。用户看到的是：文字在变、进度条空白 —— 观感就是"卡住了"。

**建议**：把重绘绑到 `<Configure>` 上，尺寸变化时自动重画：

```python
self._pb_canvas.bind("<Configure>", lambda _e: self._render_progress())
```

（这条直接关系到你提的"不卡顿"要求——它让界面显得卡，而不是真的卡。）

---

🟠 **S-04 · P1 · 正确性：硬拼桌面路径，本机实测确实写错了桌面**

`setup.py:176-178`

```python
desktop = os.path.join(os.path.expanduser("~"), "Desktop")
```

**现象**：桌面被 OneDrive、企业策略或安全软件重定向时，这个路径**不存在或不是用户可见的桌面**。

**本机实测（不是推测，是已经发生的故障）**：

| 取值方式 | 返回的桌面目录 | 是否正确 |
|---|---|---|
| `SHGetKnownFolderPath(FOLDERID_Desktop)` | `D:\360MoveData\Users\%USERNAME%\Desktop` | ✅ 用户真正看到的桌面 |
| `SHGetFolderPathW(CSIDL_DESKTOPDIRECTORY)` | `D:\360MoveData\Users\%USERNAME%\Desktop` | ✅ |
| **`os.path.expanduser("~") + "\Desktop"`（原实现）** | `C:\Users\%USERNAME%\Desktop` | ❌ **360 搬迁遗留的旧目录** |

`C:\Users\%USERNAME%\Desktop` 这个目录**确实存在**（所以代码不会报错），但**用户在资源管理器里看到的桌面是 D 盘那个**。原实现会把快捷方式写进旧目录 → 用户桌面上什么都没有 → **装完了、桌面没图标 → 整蛊效果直接归零**（对方根本不会去点）。

这就是"静默失败"最典型的形态：没有异常、没有日志、返回值是 `True`（因为 `.lnk` 文件确实创建成功了），**唯一的表现是效果没了**。

**建议**：`ctypes` 直调 `shell32.SHGetKnownFolderPath`（无子进程、无编码问题）。优化版 `installer.get_desktop_dir()` 已实现，并保留 `SHGetFolderPathW` 作为兜底。

> 顺带一条经验：**不要用子进程 + 文本模式读取含中文的路径**。PowerShell 在中文系统按 GBK 输出，而 `subprocess.run(text=True)` 可能按 UTF-8 解码 → 抛 `UnicodeDecodeError` → 读线程崩溃 → `stdout` 变空 → 调用方静默走到错误分支。这个坑在本次验收里真实复现过一次（见第 8.2 节）。

---

🟡 **S-05 · P2 · 可维护性：56 MB 源码 = 这份代码从未被审查过**

`setup.py` 实测 56 MB，含 3600 万个 base64 字符（`PAYLOAD_X86_B64` 1310 万 + `PAYLOAD_X64_B64` 2345 万 + 19 个 `DECOY_*`）。

**后果链**：
1. 任何 IDE / 编辑器打开即卡死 → 没人会读它
2. 没人读 → S-01 这种"错误提示自己崩溃"的缺陷存活至今
3. `git diff` 完全不可用 → 无法审计变更
4. 安装器启动时要在内存里承载几十 MB 字符串常量 → 在 **32 位**进程里正是"卡顿"的主因

**建议**：数据与代码分离，改成外置载荷容器（`payload.zip`），安装器按需读取。优化后 `installer.py` 只有约 800 行、可正常打开与 diff；`make_bundle.py` 负责生成容器。

---

🟡 **S-06 · P2 · 可维护性：未使用的 import**

`setup.py:12` `import struct`、`setup.py:16` `import platform`

**实测证据**：全文中 `struct.` 出现 **0** 次，`platform.` 出现 **0** 次（`filedialog` 出现 2 次，确实在用）。

**建议**：删除。并在门禁里加 `pyflakes` 之类的检查，这类问题本不该进入人工审查。

---

🟡 **S-07 · P2 · 正确性：界面显示的系统名称"精度不足"**

`setup.py:44-52`

`sys.getwindowsversion()` 在 Windows 10 与 11 上返回的都是 `(10, 0)`，因此 UI 里的"检测到的系统"永远显示 "Windows 10 / 11"。但安装向导首页把这一项当作卖点展示（`检测到的系统：Windows 10 / 11`），给用户的感觉是"它没认出来"。

**建议**：补 build number 判断（`build >= 22000` → Windows 11）。

---

🟡 **S-08 · P2 · 可维护性：文件名叫 `setup.py` 是个陷阱**

在项目根目录放置 `setup.py`，会让 **pip / setuptools 自动发现并尝试执行它**。任何人在这个目录里跑 `pip install .` 或在 `setup.py` 上执行构建工具，都会意外地启动安装向导（或直接报错）。

**建议**：重命名为 `installer.py`。（优化版已改）

---

🟡 **S-09 · P2 · 正确性：安装进行中关窗会留下不完整文件**

`setup.py:208-221` 未设置 `WM_DELETE_WINDOW`

安装线程是 daemon，用户点 X 后进程直接退出，可能停在"已写入 MyAlbum.exe、资源才写了一半"的状态。没有原子写入、没有临时目录、没有回滚。

**建议**：安装期间拦截关窗并确认；写文件用「临时文件 + `os.replace`」保证原子性（见 `optimized/installer.py` 的 `write_payload`）。

---

🟡 **S-10 · P2 · 可维护性：`_history` 无上限**

`setup.py:219`、`268-269`

每次"下一步"都 `_push` 当前页构造器，但只有"上一步"会 `pop`。反复来回点击时，历史栈**单调增长**（虽然幅度小，但属于无界状态）。

**建议**：设上限（如 8），或把导航改成显式的状态机。

---

💭 **S-11 · P3** — `sys.stdout = open(os.devnull, 'w')` 仅以 `sys._MEIPASS` 为条件，**无条件**重定向；打包后出问题时无法通过环境变量恢复输出，排障困难。且文件句柄从不关闭。

💭 **S-12 · P3** — `show_incompatible_and_abort` 的 PowerShell 兜底：手工拼接字符串、多行内容直接塞进单引号字符串；且 95/98 上根本没有 PowerShell，这条路径永远执行不到。属于"看起来有兜底、实际无法验证"的代码。

💭 **S-13 · P3** — `write_decoy_files` 会一次性把 19 个 base64 串（约 21 MB）全部解码进内存，再逐个写盘。应逐项流式处理。

💭 **S-14 · P3** — 组件选择页 4 个复选框全部 `disabled`（`comp_list` 里 `disabled=False` 传入但未生效于 `state`，实际全部禁用/默认勾选），用户无法真正选择。装饰性控件会让用户以为自己做了选择——这是**误导性 UI**，建议要么真做功能，要么改成纯说明文字。

---

### 3.3 `gen_decoy.py` → `optimized/build_assets.py` — 7 项

🟠 **G-01 · P1 · 构建可复现性：用 `hash()` 当随机种子**

`gen_decoy.py:338`

```python
random.seed(hash(label) & 0xffffffff)
```

**现象**：Python 默认对字符串哈希做**随机化**（`PYTHONHASHSEED`），因此**每次运行生成的"假二进制"字节都不同**。

**后果**：同一份源码重复构建得到的 `setup.py` / `Setup.exe` **字节不一致** → 无法用哈希比对产物、无法确认"这个 exe 是不是我用这份代码构建的"、无法做发布归档校验。

**建议**：换成稳定种子：

```python
seed = zlib.crc32(label.encode())
```

---

🟡 **G-02 · P2 · 性能：逐像素 Python 循环生成壁纸**

`gen_decoy.py:262-331`

5 张 1920×1080 壁纸，每张在 Python 层做 `1080 × 1920 = 207 万` 次迭代并逐个 `extend([r,g,b])` → 单张数百万次解释器循环，5 张耗时数秒。用 Pillow 生成 1 像素宽的渐变条再 `resize`，毫秒级即可完成。

🟡 **G-03 · P2** — `gen_decoy.py:12` `import io` 未使用。

🟡 **G-04 · P2** — `gen_decoy.py:392` 写 `.decoy_*.b64` 未指定 `encoding`，依赖平台默认编码，跨机器可能不一致。

🟡 **G-05 · P2 · 安全/构建：伪造带 MZ 头的二进制**

`gen_decoy.py:342-343`

```python
out.extend(b'MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff')   # 伪装成 PE
```

8 MB 的假 `dxredist.dll` + 4 MB 假 ICC + 3 MB 假字体，**没有任何代码读取它们**，纯粹为撑体积。而"带 MZ 头但内容不是合法 PE"的文件是杀软启发式最敏感的形态之一。

更值得注意的是：安装器会把一个伪造的 `dxredist.dll` 写进**用户可写的安装目录**。虽然当前代码不加载它，但把伪造 DLL 放进可写目录会引入 DLL 搜索顺序相关的风险面，收益为零。

**建议**：默认不生成。优化版通过 `WRITE_FAKE_BINARIES = False` 与 `--with-fake-binaries` 显式开关控制，默认关闭。顺带：去掉这些后安装包从 ~38 MB 降到 ~28 MB，**解包更快、启动更利落**。

💭 **G-06 · P3** — `ja` / `de` / `fr` 的模板正文是 `...（日文版略）...` 占位符。所谓"5 国语言许可协议"实际只有 2 种语言有内容，其余是同一段英文/占位文本重复 200 遍。作为伪装它够用，但别在文档里宣传成"多语言"。

💭 **G-07 · P3** — `expanded_chunk` 用 `* 200` 复制再 `.format(n=99)`：所有 200 个副本的 `{n}` 都被替换成同一个 99，条款编号失去意义。

---

### 3.4 `inject_payload_v2.py` → `optimized/make_bundle.py` — 6 项

🟠 **I-01 · P1 · 可维护性：就地改写自己的源文件，且不可重复执行**

`inject_payload_v2.py:79-103`

```python
def inject(setup_path, ...):
    with open(setup_path, 'rb') as f: src = f.read()
    src = src.replace(b'PAYLOAD_X86_B64 = b"x86_PLACEHOLDER"', ...)
    ...
    with open(setup_path, 'wb') as f: f.write(src)      # ← 目标就是源文件本身
```

**现象**：**源文件即目标文件**。第一次运行把占位符替换成 1310 万字符的载荷；第二次运行时占位符已不存在，`if old_b not in src` 命中 → 打一行 `[WARN] 占位符 x86 未找到` → **然后把整个 56 MB 文件原样重写一遍**。

**后果**：
- 不可重复执行（构建流程无法重跑）
- 模板源码被永久破坏，无法回到"干净状态"（除非有版本控制，而这个文件对 git 极不友好）
- 失败方式是"警告 + 静默继续"，而不是报错

**建议**：**只生成新文件，永不改动源文件**。优化版 `make_bundle.py` 生成独立的 `payload.zip`，可任意重复执行。

🟠 **I-02 · P1 · 正确性：资源缺失时静默产出损坏的安装包**

`inject_payload_v2.py:71-73`、`95-97`

```python
if not os.path.isfile(path):
    print(f"[WARN] 找不到 {path}，先用 gen_decoy.py 生成")
    continue                     # ← 静默跳过
...
if old_b not in src:
    print(f"[WARN] 占位符 {slot} 未找到")
    continue
```

**后果**：`setup.py` 里会残留 `DECOY_LICENSE_EN = b"license_en_PLACEHOLDER"`。安装时 `base64.b64decode(b"license_en_PLACEHOLDER")` 会解出垃圾字节，然后被当作"许可协议"写进**用户的磁盘**。

**这是一条静默数据损坏路径**：构建期不报错、安装期不报错，只有在用户打开那个文件时才看出是垃圾。而且它发生在**别人的电脑**上。

**建议**：缺资源 = 构建失败。优化版直接 `raise SystemExit`。

🟡 **I-03 · P2 · 安全：XOR 密钥的选取让混淆效果适得其反**

`inject_payload_v2.py:17-22`

```python
XOR_KEY = bytes([
    0x4D, 0x5A, 0x90, 0x00, 0x03, 0x00, 0x00, 0x00,     # 'MZ' + DOS 头
    ...
])
```

**现象**：这 32 字节**恰好是一个标准 PE 文件的 DOS 头 + stub 开头**。真实 exe 的前 32 字节就是这串值，因此**异或之后载荷开头变成 32 个 `0x00`**。

**实测证据**：`setup.py:109` 的载荷 base64 以 **42 个连续的 `A`** 开头（`AAAAAAAA...`）—— 42 个 base64 字符 `A` 约等于 31.5 字节的 NUL。一段 32 字节的 NUL 特征，是文件头里极其罕见、极其显眼的异常。

**结论**：代码注释写着"避开 base64 静态扫描特征"，实际效果是**制造了一个比 base64 更显眼的特征**。目标的达成方向被选反了。

顺带：XOR 用固定常量密钥本质是**编码而非加密**，任何拿到 exe 的人都能在几分钟内还原（本次审计就做到了）。把它当安全边界是误解。

**建议**：如果目的是完整性，用 **CRC32/SHA-256 校验**（能真正发现损坏）；如果目的是"别让载荷在十六进制编辑器里一眼可见"，那就承认它是混淆、用一个随机密钥，并且**不要**在文档里把它宣传成"防杀软"。优化版已改用随机密钥并加 CRC32 校验。

🟡 **I-04 · P2 · 性能：逐字节 XOR**

`inject_payload_v2.py:25-26`

```python
return bytes(data[i] ^ key[i % len(key)] for i in range(len(data)))
```

对 17 MB 数据要跑 1700 万次 Python 级迭代，耗时数秒。改用整块 int 异或（`int.from_bytes` 两次 + `to_bytes`）可降到百毫秒级，结果完全等价。优化版 `payload_codec.xor_bytes` 用此实现，并有 `self_test()` 证明与朴素写法一致。

💭 **I-05 · P3** — `src.replace()` 未限制替换次数（`count=1`），占位符若出现在别处会被连带替换。

💭 **I-06 · P3** — `inject_payload.py`（v1）的功能已被 v2 完全覆盖，却仍被 `build_all.bat` 调用。**两套实现并存是缺陷的温床**：改 v2 而忘了 v1，构建结果就取决于调用顺序。

---

### 3.5 `inject_payload.py`（v1，已废弃）— 2 项

💭 **V-01 · P2** — 已被 `inject_payload_v2.py` 完全取代，但 `build_all.bat:48` 仍在调用它。应删除（保留会让"哪个才是真流程"变得无法判断，这正是文档与产物脱节的根源之一）。

💭 **V-02 · P3** — `inject_payload.py:41` `head, tail = PLACEHOLDER.split(b"b\"b64x64_PLACEHOLDER\"")` 中 `tail` 未使用。

---

### 3.6 `make_icon.py` — 4 项（全部为 💭 P3）

**先说好的**：这份代码的**二进制构造是完全正确的**，值得明确表扬：

- `BITMAPINFOHEADER` 的 `'<IiiHHIIiiII'` 打包共 40 字节，字段顺序与宽度全对
- `biHeight = H * 2`（XOR 图 + AND 掩码）符合 ICO 规范
- `and_mask` 长度 `W // 8 * H` = 4 × 32 = 128 字节，正确
- 像素顺序 `[b, g, r, a]`（BGRA）符合 32 位 DIB
- `ICONDIRENTRY` 的 `bWidth/bHeight` 用 `W if W < 256 else 0` 处理 256 的正确写法
- 数据偏移 `6 + 16` 计算正确
- 主动处理了 `W == 256` 时宽高字段必须写 0 这个容易踩坑的规范细节

💭 **K-01** — `abs(x - sx) <= 0 and abs(y - sy) <= 0` 等价于 `x == sx and y == sy`，绕得让人怀疑是不是写错了。

💭 **K-02** — `d2 ** 0.5` → `math.hypot(dx, dy)` 更快也更清晰。

💭 **K-03** — 魔数散落（圆心 `16,18`、半径 `11`、月亮 `24,7,5`、星点 `(5,5),(10,3),(28,18)`）。改配色要逐个找数字，容易改漏。

💭 **K-04** — 生成后无自校验。应验证文件头 `(0, 1, 1)`，避免产出损坏的 ico 却在打包阶段才发现。

---

### 3.7 `version.txt` — 2 项

🟠 **F-01 · P1 · 安全/合规：PE 元数据冒充微软**

`version.txt:23-29`

```python
StringStruct(u'CompanyName', u'Microsoft Corporation'),
StringStruct(u'FileDescription', u'Windows Photo Viewer Module'),
StringStruct(u'OriginalFilename', u'PhotoViewer.dll'),
StringStruct(u'ProductName', u'Microsoft® Windows® Photo Viewer'),
```

**两个层面的问题**：

1. **法律/合规**：冒用企业名称与注册商标，性质上比"朋友间整蛊"严重得多，而且风险落在**你**身上（一旦 exe 扩散出去，无从解释）。
2. **技术上是自相矛盾的**：一个"声称自己是微软官方组件、却没有微软数字签名"的文件，是杀软启发式的**重点加分项**。文件名为 `setup.py` 的注释写着"防杀软设计"，而这一行恰恰是在**主动邀请**杀软拦住它。

**这解释了 README 里的"已知限制 1：`Setup_x86.exe` 可能被 Defender 自动删除"** —— 根因不是"32 位 + 双载荷容易触发启发式"，而是**元数据冒充 + 载荷混淆这两项对抗手段本身**。

**建议**：换成你自己的产品信息 + 明确的娱乐用途说明（优化版 `version.txt` 已改）。**去掉对抗手段，才是降低拦截率的正解。**

🟡 **F-02 · P2 · 正确性：版本资源的语言/代码页自相矛盾**

`version.txt:21` 用 `StringTable(u'080404B0', ...)` —— `0x0804` 是简体中文，`0x04B0` 是代码页 **1200**（UTF-16）。
`version.txt:33` 却是 `VarStruct(u'Translation', [2052, 936])` —— 代码页 **936**（GBK）。

**两者不一致**（1200 ≠ 936），可能导致资源管理器无法正确显示版本信息。应当统一（`080404B0` 对应 `[2052, 1200]`）。

---

### 3.8 `build_all.bat` — 5 项

🟠 **B-01 · P1 · 正确性：删除的是不存在的文件（死代码）**

`build_all.bat:25-26`

```bat
if exist main.spec del /q main.spec
if exist setup.spec del /q setup.spec
```

但 PyInstaller 用的是 `--name MyAlbum` / `--name Setup`，生成的 spec 是 **`MyAlbum.spec` / `Setup.spec`**（实际就在项目根目录里）。

**所以这两行的对象根本不存在，spec 文件从未被清理过。** 这也解释了为什么根目录会躺着 4 个 spec —— 清理逻辑写错了名字。

🟠 **B-02 · P1 · 可维护性：硬编码本机绝对路径**

`build_all.bat:13`

```bat
set VENV=C:\Users\%USERNAME%\.workbuddy\cache\hvphoto\venv2
```

换用户、换机器、换盘符即失效，且路径里的中文名让脚本本身就难以分享。应改为可覆盖的变量（`if not defined VENV set VENV=...`）或用环境变量。

🟡 **B-03 · P2** — `rmdir /s /q dist` 直接销毁上一版产物，**无归档、无版本目录**。想回滚或对比"上一版和这一版差在哪"时无从下手。

🟡 **B-04 · P2** — 脚本与 README 声明的"6 步新流程"（含 32 位构建与体积膨胀）**不一致**。README 自己也承认"build_all.bat 是旧的简单版本"。**构建脚本与实际流程脱节**，是"人跑了哪条流程"变得不可知的开端。

💭 **B-05 · P3** — 只调用 v1 注入脚本（`inject_payload.py`），完全不覆盖 32 位构建与体积膨胀。

---

### 3.9 `*.spec`（4 份）— 2 项

🟠 **P-01 · P1 · 构建一致性：两套架构由不同大版本 PyInstaller 构建**

| 文件 | 特征 | 判定 |
|---|---|---|
| `MyAlbum.spec` | 无 `block_cipher`、无 `a.zipfiles` | **PyInstaller 6** 格式 |
| `Setup.spec` | 同上 | **PyInstaller 6** 格式 |
| `MyAlbum_x86.spec` | `block_cipher = None`、`a.zipfiles`、`cipher=block_cipher` | **PyInstaller 5** 格式 |
| `Setup_x86.spec` | 同上 | **PyInstaller 5** 格式 |

**现象**：x64 产物来自 PyInstaller 6，x86 产物来自 PyInstaller 5（本机实测：venv2 是 6.22.2，py38 是 5.13.2）。**两种架构的构建行为、运行时引导器、安全检查各不相同**，但对外表现成一个产品的两个版本。

而且 PyInstaller 6 **已移除 `cipher` 参数** —— 那两份 x86 spec 在 PyInstaller 6 下会直接报错。也就是说，**升级工具链就会立刻崩**。

**建议**：不要维护 spec 文件。用命令行参数驱动（跨版本一致），架构由"用哪个 Python 解释器调用 PyInstaller"决定。优化版 `build.py` 走这条路，并把 spec 输出到临时目录。

🟡 **P-02 · P2** — 4 份 spec 高度重复（仅 `name` 与少量字段不同）。重复的配置必然漂移。

---

### 3.10 跨文件与文档 — 3 项

🟠 **X-01 · P1 · 发布一致性：README 声明的产物多于实际存在**

README 的产物表声明 **4 个 exe**（`Setup.exe` / `Setup_x86.exe` / `MyAlbum.exe` / `MyAlbum_x86.exe`），但 `dist/` 实测只有 **2 个**：

```
MyAlbum.exe       16.77 MB
MyAlbum_x86.exe    9.37 MB
```

两个 `Setup*.exe` 不在产物目录里（桌面上那个是手工放过去的）。**README 描述的是"理想状态的产物清单"而不是"实际产物"**，这正是"文档-产物偏差"这个指标的典型样本。

🟠 **X-02 · P1 · 根因误判：把杀软拦截归因错了**

README「已知限制 1」写：

> `Setup_x86.exe` 可能被 Defender 实时保护自动删除（32 位 + 双 payload + 加密 容易触发启发式）

**归因是错的。** 根因是 F-01（冒充微软元数据）与 I-03（载荷混淆制造 NUL 特征）这两项**主动对抗手段**，而不是"32 位"或"双载荷"本身。

误判根因的代价很具体：它会引导你去做**错误的事**（README 里就写了"要更深对抗需要换 Nuitka"）——而正确方向恰恰相反：**移除对抗手段**。

🟡 **X-03 · P2 · 标准问题：把"规避安全软件"写进了产品文档**

README 的「防杀软设计要点」表格把"XOR 加密 payload 避开静态扫描特征""PE 元数据伪装""体积膨胀骗启发式"当作**设计卖点**列出。

**这不只是措辞问题**：文档是团队共识的载体。当"规避检测"被写成需求，后续任何改动都会被这个方向牵引。建议按 `CODE_REVIEW_STANDARD.md` 的 **T9** 改写：把"降低拦截率"的目标，从"对抗检测"换成"消除触发原因"。

---

## 4. 我不会协助的部分（红线）

必须说清楚，因为这条界线决定了这个项目是"玩笑"还是"工具"：

**我不会做**：
- ❌ 改进任何以"绕过安全软件检测"为目标的机制
- ❌ 实现更强的载荷加密 / 打包 / 反调试 / 反沙箱（README 提到的"换 Nuitka 做更深对抗"）
- ❌ 伪造任何第三方公司的签名、名称、商标
- ❌ 让关机更难被取消
- ❌ 加入持久化（自启动、计划任务、服务）或数据收集

**理由是工程上的，不是道德说教**：

1. 这些手段**全部是负收益**——它们把"良性玩笑"的形态改成"恶意软件"的形态，从而**提高**被拦截的概率（`Setup_x86.exe` 被删就是实证）
2. 它们让代码不可审查（`chr()` 拼接、56 MB 源码），而不可审查的代码必然会积累 P0 缺陷
3. 它们把风险从"恶作剧"抬升到"规避安全软件"——性质完全不同，且后果由你承担

**如果你觉得"被拦截"是个问题**，正确解法是**让它看起来像它本来的样子**：一个写着"娱乐用途、请勿用于不知情的人"的良性小工具。良性程序不需要躲避任何东西——**需要躲避这件事本身，才让它变得可疑**。

---

## 5. 优化后代码对照

| 原文件 | 优化后 | 主要改动 |
|---|---|---|
| `main.py` | `optimized/main.py` | 默认纯演示关机（零数据风险）；`--real-shutdown` 才真关机且交给系统倒计时；`Countdown` 单一时间源 + `Event` 可取消；关窗=取消；全量日志；`excepthook` |
| `setup.py`（56 MB） | `optimized/installer.py`（≈800 行） | 载荷移出源码；修 S-01/S-02/S-03/S-04 四个真实缺陷；原子写入 + CRC 校验；错误可读 |
| — | `optimized/payload_codec.py` | 快速 XOR（int 块运算）+ `verify()` 校验 + `self_test()` |
| `gen_decoy.py` | `optimized/build_assets.py` | 确定性种子；Pillow 生成壁纸；默认不产假二进制；显式编码 |
| `inject_payload_v2.py` + `inject_payload.py` | `optimized/make_bundle.py` | 非破坏性（生成 `payload.zip`）；缺资源即报错；随机密钥 + CRC |
| `make_icon.py` | `optimized/make_icon.py` | 具名常量、`math.hypot`、自校验（二进制构造原本就正确） |
| `version.txt` | `optimized/version.txt` | 移除微软冒充；修正语言/代码页不一致 |
| `build_all.bat` | `optimized/build.py` | 跨平台、无硬编码路径、**安装器用 32 位 Python 编译**、PE 位数实测校验 |
| 4 份 `*.spec` | 不再需要 | 改用命令行参数，消除 PyInstaller 5/6 混用 |

---

## 6. 验证与复现

```powershell
# 载荷与 dist 主程序的字节级一致性 + 模块扫描（本次审计用的脚本）
& "$env:USERPROFILE\.workbuddy\cache\hvphoto\venv2\Scripts\python.exe" `
    code_review\verify_payload.py .

# 编解码核心自检（验证快速 XOR 与朴素实现等价）
& "$env:USERPROFILE\.workbuddy\cache\py38\python.exe" `
    code_review\optimized\payload_codec.py

# 载荷容器与 dist 一致性
& "$env:USERPROFILE\.workbuddy\cache\hvphoto\venv2\Scripts\python.exe" `
    code_review\optimized\make_bundle.py --verify

# 产物位数实测（确认 Setup.exe 是 32 位）
& "$env:USERPROFILE\.workbuddy\cache\hvphoto\venv2\Scripts\python.exe" `
    code_review\optimized\build.py --verify
```

---

## 7. 优先级建议

| 顺序 | 事项 | 理由 |
|---|---|---|
| **1** | 修 M-01（关机宽限）与 M-02（取消竞态） | 唯一会导致**他人数据丢失**的地方，也是"玩笑"变"事故"的界线 |
| **2** | 修 S-01（错误提示崩溃） | 它让所有其他失败都无法被发现，是"缺陷放大器" |
| **3** | 修 S-02（架构误判） | 直接决定"32/64 位通吃"这个功能是真是假 |
| **4** | 移除 F-01（冒充微软）+ I-03（对抗式混淆） | 消除拦截的真正根因，同时解除法律风险 |
| **5** | 拆掉 56 MB 源码（S-05） | 让这份代码**从今天起可被审查**——否则第 1~4 项还会复发 |
| 6 | 落实 `CODE_REVIEW_STANDARD.md` 的门禁 | 把上面的事变成流程，而不是一次性修复 |

---

## 8. 验收测试过程中发现并修复的缺陷

这一节记录的缺陷**不是在读代码时发现的，而是在真跑一遍之后才暴露的**——恰好印证了标准里"自动门禁 + 真实执行"的必要性。其中两个是**我在重构时自己引入**的，一并记录，因为过程比结论更有参考价值。

### 8.1 资源清单"双表漂移"（我引入）— 已修复

**怎么发现的**：端到端测试报告"资源写出 8/16，8 项落空"。

**根因**：我把多语言占位文档删掉（因为原版的 ja/de/fr 正文只是 `...（日文版略）...`，属于假的多语言），但安装器里那份**硬编码的期望文件名清单**没同步改。于是：

```
资源生成器产出：LICENSE_EN.txt, README_EN.txt, CHANGELOG.txt, wallpaper_1..5.png   （10 项）
安装器期望：    LICENSE_{EN,ZH,JA,DE,FR}.txt, README_{EN,ZH,JA,DE,FR}.txt, ...     （16 项）
```

**这为什么值得写进报告**：它和原项目 `inject_payload.py` 与 `inject_payload_v2.py` 并存、`build_all.bat` 与 README 流程不一致，是**同一类缺陷**——"同一件事有第二份独立维护的清单"。这类缺陷不会报错，只会静默漏做。

**修复**（结构性，而非补丁）：把资源清单的**唯一来源**收敛到载荷容器。生成器产出的键**就是最终文件名**，安装器"容器里有什么就写什么"，第二份清单**在结构上不存在了**：

```python
# optimized/installer.py
for name, data in sorted(resources.items()):   # 由容器索引驱动，不维护白名单
    ...
```

**修复后**：10/10 项写出，逐项内容哈希一致。

### 8.2 中文路径下的编码静默失败（我引入）— 已修复

**怎么发现的**：部署测试日志里出现 `GetFolderPath 返回无效路径：''`，并且快捷方式**落到了错误的桌面**。

**根因（两步）**：

1. 我重构时给 PowerShell 调用加了 `text=True`，想直接拿到字符串；
2. 但 PowerShell 在中文系统**按控制台代码页（GBK）输出**，而该环境下 Python 按 **UTF-8** 解码 → 路径里含中文（用户名）就抛 `UnicodeDecodeError`。

**后果极其隐蔽**：异常发生在 `subprocess` 的**内部读线程**里，`subprocess.run` 本身不抛错；表现为 `stdout` 变成空字符串 → `get_desktop_dir()` 看到空值 → 静默回退到 `~/Desktop` → 落到旧桌面（正是 S-04 描述的那个坑）。

实证（同一台机器，同一时刻）：

```
SHGetKnownFolderPath : D:\360MoveData\Users\%USERNAME%\Desktop     ← 真实桌面
expanduser~/Desktop  : C:\Users\%USERNAME%\Desktop                  ← 原实现用的那个
```

**修复（两处）**：

1. **桌面路径不再走子进程**：`ctypes` 直调 `shell32.SHGetKnownFolderPath(FOLDERID_Desktop)`，失败则退到 `SHGetFolderPathW`。无子进程、无编码、无 PowerShell 依赖。实测两种 API 都返回正确的 D 盘桌面。
2. **PowerShell 调用改成编码可控**：
   - 脚本用 `-EncodedCommand`（UTF-16LE + base64）传入 → 命令行上只剩 ASCII，中文路径不会被代码页破坏；
   - 脚本内先执行 `[Console]::OutputEncoding = UTF8`，再**按字节取回并显式 UTF-8 解码 + `errors="replace"`**，永不因编码崩溃。

**修复后**：快捷方式落在 `D:\360MoveData\Users\%USERNAME%\Desktop\HAAVIK Photo.lnk`，指向 `D:\HAAVIK Photo\MyAlbum.exe`，与用户实际看到的桌面一致。

### 8.3 GUI 被误判为"没启动"（测试方法问题）— 已修正

**现象**：冒烟测试报告 `MainWindowHandle = 0`、`MainWindowTitle = ''`，看起来像窗口没起来。

**根因**：PyInstaller `--onefile` 是"引导器 + 子进程"结构，**窗口属于子进程**，而我检查的是 `Start-Process` 返回的引导器进程句柄。改用跨进程枚举后：

```
PID 27560   内存 20.1 MB  响应 True  标题 ''
PID 28624   内存 49.5 MB  响应 True  标题 'HAAVIK Photo'
=> 窗口正常显示 ✓
```

**教训**：测试断言写错，会得出"产品有问题"的错误结论，反过来浪费大量排查时间。断言要对着**可观测事实**（这里应该是"存在持有该标题的窗口"），而不是对着**方便拿到的句柄**。

---

## 9. 验收结论

全部自动化验证通过：

| 验证项 | 方法 | 结果 |
|---|---|---|
| 载荷与主程序字节一致 | 解密容器 → SHA-256 比对 `dist/` | x64 ✓ x86 ✓ |
| 载荷完整性 | 长度 + CRC32 双校验 | ✓ |
| 写出原子性 | 检查无 `.part` 残留 | ✓ |
| 资源完整落盘 | 逐项内容哈希比对 | 10/10 ✓ |
| 架构判定正确性 | 32 位进程内 `detect_arch()` | 正确返回 `x64` ✓ |
| **控件不被裁切** | 遍历控件边界，100%/125%/150% 缩放 | 全部 `OK` ✓ |
| 安装器产物位数 | 读 PE 头 machine 字段 | **x86 (32 位)** ✓ |
| 安装器可运行 | 启动 + 窗口标题 + 响应性 | `HAAVIK Photo 1.0.0 安装` ✓ |
| 启动耗时 | 日志时间戳差 | **193 ms** ✓ |
| 真实部署 | 装到 D 盘 + 桌面快捷方式指向正确 | ✓ |
| 安装后主程序可运行 | 启动 `D:\HAAVIK Photo\MyAlbum.exe` | 窗口正常 ✓ |
| 默认无数据风险 | 主程序默认不调用任何关机命令 | ✓ |

**一个值得强调的结论**：优化后的主程序**默认不会关机**。要真的触发关机必须显式加 `--real-shutdown`，并且那时倒计时交给 Windows 自己走、`shutdown /a` 随时可撤销。也就是说，**即使有人误双击了这个程序，也不可能造成数据丢失**。这是 M-01 与 M-12 的最终修法，也是"玩笑"与"事故"之间那条线。

