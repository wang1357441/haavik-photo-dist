# 代码审查交付物索引

本目录是 HAAVIK Photo 项目的代码审查成果。三份东西，各管一件事。

---

## 1. 标准与流程 —— 先看这份

**`CODE_REVIEW_STANDARD.md`**

建立机制用的。包含：

- **原则**（可审查性优先、优先序高于完备性、可读性 > 隐蔽性 ……）
- **严重级别定义** 🔴P0 / 🟠P1 / 🟡P2 / 💭P3，含"阻断合并"的判定三问
- **审查维度清单**：正确性 / 安全 / **整蛊类软件专项红线 T1–T9** / 可维护性 / 性能 / 构建发布 / 文档
- **七步流程**与合并门禁
- **单人项目的简化执行方案**（做不到"他人审查"时的替代做法）
- **审查意见模板** + 反面示例
- **度量指标**（5 个，不多）
- **附录 A**：本项目已证实的 10 条反模式，附 grep 搜索方式
- **附录 B**：可直接执行的门禁命令

> 其中 **T9（不做杀软对抗）** 是这份标准里最关键的一条。
> 它把"避免被拦截"的解法从"加强对抗"改为"消除触发原因"，
> 理由是工程计算，不是道德说教 —— 详见报告 F-01 / I-03 / X-02。

---

## 2. 审查报告 —— 逐文件结论

**`CODE_REVIEW_REPORT.md`**

包含：

- **执行摘要**与最关键的三个缺陷
- **架构说明**：哪个是"假冒安装包"、核心程序存在哪、审计证据
  （载荷与 `dist/` 主程序的 **SHA-256 字节级比对**、模块扫描、体积实测）
- **问题总览表**：58 项，按 文件 × 严重级别 分布
- **逐文件详细审查**：10 个文件组，每项含 文件:行号 + 现象 + 为什么 + 建议
- **红线清单**：我不会协助的部分及理由
- **优化前后对照表**
- **验收过程中发现并修复的缺陷**（含我自己引入的两个）
- **验收结论表**

---

## 3. 优化后代码

**`optimized/`** —— 可直接替换原文件使用。

| 文件 | 替代 | 说明 |
|---|---|---|
| `main.py` | `main.py` | 整蛊主程序。**默认不关机**，`--real-shutdown` 才真关机 |
| `installer.py` | `setup.py`（56 MB → 约 900 行） | 安装向导，修掉 4 个真实缺陷 |
| `payload_codec.py` | 新增 | 编解码 + 完整性校验，含 `self_test()` |
| `build_assets.py` | `gen_decoy.py` | 可复现；Pillow 生成壁纸；不伪造二进制 |
| `make_bundle.py` | `inject_payload.py` + `inject_payload_v2.py` | 非破坏性，生成 `payload.zip` |
| `make_icon.py` | `make_icon.py` | 具名常量 + 自校验 |
| `version.txt` | `version.txt` | 移除微软冒充 |
| `build.py` | `build_all.bat` | 一键构建；**安装器用 32 位 Python 编译** |

原 4 份 `*.spec` 不再需要（PyInstaller 5 与 6 的 spec 语法不兼容，
改用命令行参数，架构由"用哪个解释器调用"决定）。

---

## 验收脚本（可重复执行）

```powershell
$py38 = "$env:USERPROFILE\.workbuddy\cache\py38\python.exe"
$py64 = "$env:USERPROFILE\.workbuddy\cache\hvphoto\venv2\Scripts\python.exe"

# 载荷与主程序字节级比对 + 模块扫描（审计证据来源）
& $py64 verify_payload.py .

# 布局溢出检测：100%/125%/150% 缩放下是否有控件被裁切
& $py64 layout_test.py

# 端到端安装机制：解密 -> 校验 -> 原子写入（输出到临时目录并自清理）
& $py38 e2e_install_test.py

# 真实部署到桌面（释放单个 exe，不建快捷方式）
& $py38 deploy_local.py
& $py38 deploy_local.py --uninstall            # 只删具名文件，绝不递归删目录
& $py38 deploy_local.py --uninstall --purge-legacy   # 额外清 1.1.0 以前的 D:\HAAVIK Photo

# 一键构建（含工具链检查、产物位数实测）
& $py64 optimized\build.py
& $py64 optimized\build.py --skip-main   # 只重打容器与安装器
& $py64 optimized\build.py --verify      # 只校验产物
```

---

## 交付物落点

| 位置 | 内容 |
|---|---|
| `D:\360MoveData\Users\%USERNAME%\Desktop\Setup.exe` | **交付给朋友的唯一文件**（32 位安装器，32/64 位 Windows 通用） |
| `D:\360MoveData\Users\%USERNAME%\Desktop\Setup_原版备份.exe` | 改造前的原版，留作对比 |
| 桌面 `MyAlbum.exe` | 安装结果 —— **就这一个文件**，不建快捷方式、不生成 `Resources\` |
| 桌面 `state.json` | 首次运行主程序后出现，记"这个月的问题问过了"；删掉 = 重新出题 |

---

## 一句话总结

代码意图是良性的，但存在 3 个 P0 缺陷（其中一个是数据丢失风险）、
以及一个流程性根因：**1/3 的代码量因为文件太大而从未被审查过**。
优化后交付的 `Setup.exe` 已验证可在 32/64 位 Windows 上运行、
界面无裁切、默认不造成任何数据风险。
