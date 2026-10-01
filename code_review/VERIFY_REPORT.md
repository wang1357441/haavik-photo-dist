HAAVIK Photo 产物审计报告
项目根目录: hvphoto（本机绝对路径已省略；加 --show-paths 显示）
解释器    : 3.12.14 (main, Sep  1 2026, 14:17:39) [MSC v.1944 64 bit (AMD64)]

========================================================================
A. 载荷容器 payload.zip  vs  dist/ 主程序
========================================================================
  容器: payload.zip  (49.54 MB, 构建于 2026-10-01 15:01:38)
  产品: HAAVIK Photo 1.8.11
  -> 容器内的产品标识与 APP_VERSION 一致 ✓

  [x64] MyAlbum.exe
       容器内 30,682,896 B  crc=d4ecc96f  sha256=50cc749ce37bdb71
       磁盘上 30,682,896 B            sha256=50cc749ce37bdb71
       -> 字节级一致 ✓
  [x86] MyAlbum_x86.exe
       容器内 21,265,760 B  crc=eff77a7c  sha256=5cccf12ed9754966
       磁盘上 21,265,760 B            sha256=5cccf12ed9754966
       -> 字节级一致 ✓

  容器内附带资源 0 项 —— 单 EXE 安装，安装目录里只会有一个 exe ✓
  容器条目与 meta.json 索引完全对应，无夹带 ✓

========================================================================
B. exe 归档内容（读 PyInstaller 真实索引，不做字节扫描）
========================================================================
  PyInstaller 6.22.2

------------------------------------------------------------------------
  MyAlbum.exe  (29.26 MB, x64 (64位))
  条目 1005 个，类型码分布：{'b': 995, 'm': 5, 's': 4, 'z': 1}
  入口脚本：'main' ✓（无额外脚本）
  引导模块 5 个：pyimod01_archive、pyimod02_importers、pyimod03_ctypes、pyimod04_pywin32、struct ✓
  必需数据 haavik.png       已嵌入 ✓
  必需数据 selfcheck_modules.txt 已嵌入 ✓
  条目分类：PIL 扩展×7，PyInstaller 引导×8，Tcl/Tk 数据×924，stdlib 归档×1，主程序显示图×1，入口脚本×1，模块表归档(PYZ)×1，第三方 C 扩展/运行时×42，自检模块清单×1，运行库×19
  全部条目均可归类，无来历不明的文件 ✓
  PYZ 模块表：396 个模块
  本项目模块：源码可达 16 个，归档里 16 个（PYZ 15 + 入口脚本 1）
      源码可达：_smtp_secret、ai_ops、ai_tool、feedback_send、image_command、image_dialogs、image_ops、image_tool、image_view、main、serial_check、shell_ops、state_store、ui_common、ui_theme、version_check
      归档内含：_smtp_secret、ai_ops、ai_tool、feedback_send、image_command、image_dialogs、image_ops、image_tool、image_view、main、serial_check、shell_ops、state_store、ui_common、ui_theme、version_check
      -> 完全一致 ✓（没有漏打、也没有夹带）

------------------------------------------------------------------------
  MyAlbum_x86.exe  (20.28 MB, x86 (32位))
  条目 972 个，类型码分布：{'b': 40, 'm': 5, 's': 6, 'x': 920, 'z': 1}
  入口脚本：'main' ✓（无额外脚本）
  引导模块 5 个：pyimod01_archive、pyimod02_importers、pyimod03_ctypes、pyimod04_pywin32、struct ✓
  必需数据 haavik.png       已嵌入 ✓
  必需数据 selfcheck_modules.txt 已嵌入 ✓
  条目分类：PIL 扩展×6，PyInstaller 引导×10，Tcl/Tk 数据×919，stdlib 归档×1，主程序显示图×1，入口脚本×1，模块表归档(PYZ)×1，第三方 C 扩展/运行时×14，自检模块清单×1，运行库×18
  全部条目均可归类，无来历不明的文件 ✓
  PYZ 模块表：383 个模块
  本项目模块：源码可达 16 个，归档里 16 个（PYZ 15 + 入口脚本 1）
      源码可达：_smtp_secret、ai_ops、ai_tool、feedback_send、image_command、image_dialogs、image_ops、image_tool、image_view、main、serial_check、shell_ops、state_store、ui_common、ui_theme、version_check
      归档内含：_smtp_secret、ai_ops、ai_tool、feedback_send、image_command、image_dialogs、image_ops、image_tool、image_view、main、serial_check、shell_ops、state_store、ui_common、ui_theme、version_check
      -> 完全一致 ✓（没有漏打、也没有夹带）

------------------------------------------------------------------------
  Setup.exe  (66.00 MB, x86 (32位))
  条目 960 个，类型码分布：{'b': 31, 'm': 5, 's': 4, 'x': 919, 'z': 1}
  入口脚本：'installer' ✓（无额外脚本）
  引导模块 5 个：pyimod01_archive、pyimod02_importers、pyimod03_ctypes、pyimod04_pywin32、struct ✓
  必需数据 payload.zip      已嵌入 ✓
  条目分类：PyInstaller 引导×8，Tcl/Tk 数据×919，stdlib 归档×1，入口脚本×1，模块表归档(PYZ)×1，第三方 C 扩展/运行时×14，载荷容器×1，运行库×15
  全部条目均可归类，无来历不明的文件 ✓
  PYZ 模块表：224 个模块
  本项目模块：源码可达 4 个，归档里 4 个（PYZ 3 + 入口脚本 1）
      源码可达：_smtp_secret、feedback_send、installer、payload_codec
      归档内含：_smtp_secret、feedback_send、installer、payload_codec
      -> 完全一致 ✓（没有漏打、也没有夹带）

========================================================================
C. 源码导入审计（AST 精确解析，与 PYZ 模块表三态比对）
========================================================================

------------------------------------------------------------------------
  入口 installer.py：直接导入 22 个顶层模块（含同目录兄弟模块递归展开）
    __future__、_smtp_secret、base64、binascii、ctypes、email、feedback_send、json、logging、os、payload_codec、shutil、smtplib、subprocess、sys、tempfile、threading、time、tkinter、urllib、zipfile、zlib

------------------------------------------------------------------------
  入口 main.py：直接导入 47 个顶层模块（含同目录兄弟模块递归展开）
    PIL、__future__、_smtp_secret、ai_ops、ai_tool、argparse、base64、ctypes、dataclasses、datetime、email、feedback_send、gc、hashlib、hmac、image_command、image_dialogs、image_ops、image_tool、image_view、io、json、logging、math、numpy、onnxruntime、os、platform、queue、re、secrets、serial_check、shell_ops、smtplib、state_store、struct、subprocess、sys、tempfile、threading、time、tkinter、ui_common、ui_theme、urllib、version_check、zipfile

  可疑模块三态比对：
    源码 = 本项目代码直接 import（★ 表示不在双用途白名单里，必须人工确认）
    依赖 = 只在 PYZ 里，源码没 import —— 由 PIL / tkinter / stdlib 依赖图引入

    模块           出现在哪些 exe 的 PYZ 里                 判定
    --------------------------------------------------------------------------
    Crypto         -                                        无（归档里就没有）
    cryptography   -                                        无（归档里就没有）
    ctypes         MyAlbum.exe、MyAlbum_x86.exe、Setup.exe  源码（双用途，用途见下）
    ftplib         -                                        无（归档里就没有）
    http           MyAlbum.exe、MyAlbum_x86.exe、Setup.exe  依赖（仅由依赖图引入）
    keyboard       -                                        无（归档里就没有）
    paramiko       -                                        无（归档里就没有）
    psutil         -                                        无（归档里就没有）
    pyautogui      -                                        无（归档里就没有）
    pynput         -                                        无（归档里就没有）
    pyperclip      -                                        无（归档里就没有）
    requests       -                                        无（归档里就没有）
    schedule       -                                        无（归档里就没有）
    selenium       -                                        无（归档里就没有）
    smtplib        MyAlbum.exe、MyAlbum_x86.exe、Setup.exe  源码（双用途，用途见下）
    socket         MyAlbum.exe、MyAlbum_x86.exe、Setup.exe  依赖（仅由依赖图引入）
    ssl            MyAlbum.exe、MyAlbum_x86.exe、Setup.exe  依赖（仅由依赖图引入）
    subprocess     MyAlbum.exe、MyAlbum_x86.exe、Setup.exe  源码（双用途，用途见下）
    telnetlib      -                                        无（归档里就没有）
    urllib         MyAlbum.exe                              源码（双用途，用途见下）
    win32api       -                                        无（归档里就没有）
    winreg         -                                        无（归档里就没有）
    wmi            -                                        无（归档里就没有）

    故意排除、随 AI 组件包分发的（不计入冲突）：onnxruntime
    源码 import 的模块与 build.py 的 EXCLUDES 无交集 ✓（比对了 29 个排除项 / 51 个 import）
        —— 这一条是 v1.6.0 那次「装完打不开」事故补上的

  双用途模块（允许源码直接使用，逐条列出用途与来源，供人工复核）：
    ctypes         读 FOLDERID_Desktop 拿桌面真实路径（这台机器被 360 迁移过，~\Desktop 是错的）/ 深色标题栏（dwmapi，失败即静默退回）/ 剪贴板 CF_DIB **写与读**（读是为了「叠加图片」里的「用剪贴板」）/ 设壁纸 SystemParametersInfoW / 回收站 SHFileOperationW
        [installer] installer.py:47
        [main] shell_ops.py:87
        [main] shell_ops.py:88
        [main] ui_theme.py:31
    smtplib        反馈：用户亲手点「直接发送」后，程序直连**发信邮箱**把用户写的那几句话发出去（只连一个服务器、只发一封信、内容只有用户打的字）。为什么不用第三方：朋友的电脑大多没有邮件程序、也没有邮箱账号，而「匿名发信」在今天的互联网上并不存在。⚠️ 这条路要求 exe 里带一个发信授权码（可随时在邮箱后台重置）—— 这是**明知代价的选择**，换来的好处是「不经过任何第三方、朋友什么都不需要」。只允许出现在 feedback_send.py 里，见 SINGLE_FILE_ONLY。
        [installer] feedback_send.py:35
        [main] feedback_send.py:35
    subprocess     启动 shutdown 做倒计时 / 取桌面路径的 PowerShell 兜底 / explorer /select 定位文件 / shell_ops.spawn_detached 启动另一个程序（重启自己或打开用户确认过的更新包，会先清掉 PyInstaller 的内部环境变量）
        [installer] installer.py:52
        [main] main.py:56
        [main] shell_ops.py:63
    urllib         版本检查：一次 HTTPS GET 取回一个版本号字符串（不下载、不执行、不落盘）
        [installer] feedback_send.py:36
        [installer] feedback_send.py:37
        [main] feedback_send.py:36
        [main] feedback_send.py:37
        [main] version_check.py:58
        [main] version_check.py:59
        [main] version_check.py:60

  红线模块（网络 / 持久化 / 输入钩子 / 进程与屏幕信息，共 19 个）中，进入归档的有 3 个：
    http、socket、ssl
  这 3 个**全部**是依赖图引入了符号但本项目代码从未调用，源码侧一个都没 import ✓
  —— 这是「无注册表持久化、无全局输入钩子、无后台上传」的可重复证据。

  单文件独占断言（联网能力不得散落）：
    smtplib        只出现在 feedback_send.py（共 2 处）✓ —— 全项目就这几个联网点
        [installer] feedback_send.py:35
        [main] feedback_send.py:35
    urllib         只出现在 feedback_send.py、version_check.py（共 7 处）✓ —— 全项目就这几个联网点
        [installer] feedback_send.py:36
        [installer] feedback_send.py:37
        [main] feedback_send.py:36
        [main] feedback_send.py:37
        [main] version_check.py:58
        [main] version_check.py:59
        [main] version_check.py:60

  自成一体的模块断言（要被单独复制到公开仓库的东西）：
    理由：朋友下载的是**单个文件**，本地永远测不出'少了一个兄弟模块'。
    serial_check.py
        用途     : 序列号生成器，原样发布到公开仓库；朋友下载单个文件就能用
        依赖     : __future__、argparse、datetime、hashlib、hmac、secrets、sys（全部为 Python 标准库）
        行数     : 430 行 / 18524 字节
        sha256   : e37847620c88a834434cd4cd840dba0620fae40adfa05444cabdc6c60140be03
        ——发布到公开仓库后，可用上面这行哈希核对是不是同一份
        未 import 任何同目录兄弟模块 ✓

  联网点不许自带地址（地址由 main.py 传入；回环地址除外）：
    feedback_send.py     里没有任何地址字面量 ✓（反馈地址必须由 main.py 传入（见该文件头部的说明））

  写盘 / 执行外部程序 审计（AST，只看确定的形态）：
    这一节回答：**这个 exe 会动我机器上的什么？**
    只认 open(写模式) / os.<写函数> / subprocess / shutil / tempfile /
    *.save|write|writestr|dump —— 不认 str.replace 之类的同名方法，
    免得把报告变成噪音，而没人看的报告等于没有报告。

    [installer] 实际会碰磁盘/起进程的源文件：installer.py
      允许     installer.py
          :557  os         os.makedirs
          :121  tempfile   tempfile.mkstemp
          :176  os         os.makedirs
          :401  subprocess subprocess.run
          :576  os         os.replace
          :619  os         os.makedirs
          :620  shutil     shutil.copy2
          :642  os         os.remove
          :701  subprocess subprocess.run
          :129  os         os.remove
          :197  open       open
          :198  open       open
          :561  open       open
          :562  method     write
          :634  shutil     shutil.copy2
          :733  open       open
          :368  subprocess subprocess.run
          :591  os         os.remove
          :1658 open       open
          :1659 method     write
          :1417 subprocess subprocess.Popen

    [main] 实际会碰磁盘/起进程的源文件：ai_ops.py、image_ops.py、main.py、shell_ops.py、state_store.py、version_check.py
      允许     ai_ops.py
          :1193 open       open
          :1194 method     dump
          :1149 os         os.makedirs
          :1198 method     write
          :1200 method     write
      允许     image_ops.py
          :789  method     save
          :816  method     save
          :838  method     save
      允许     main.py
          :322  subprocess subprocess.run
          :1946 open       open
          :1947 method     write
          :1858 subprocess subprocess.Popen
      允许     shell_ops.py
          :946  subprocess subprocess.Popen
          :256  tempfile   tempfile.gettempdir
          :697  subprocess subprocess.Popen
          :714  os         os.startfile
          :751  os         os.startfile
          :810  os         os.startfile
          :832  os         os.startfile
      允许     state_store.py
          :135  os         os.makedirs
          :136  tempfile   tempfile.mkstemp
          :138  os         os.remove
          :186  os         os.makedirs
          :291  os         os.makedirs
          :292  tempfile   tempfile.mkstemp
          :189  os         os.remove
          :299  os         os.replace
          :295  os         os.fdopen
          :296  method     dump
          :303  os         os.remove
      允许     version_check.py
          :1379 os         os.remove
          :1480 os         os.remove
          :1525 os         os.makedirs
          :1629 os         os.replace
          :1584 method     write

    上述集合与 WRITERS_EXPECTED 完全一致 ✓
    换句话说，main.exe 只会为三件事写盘：state.json（月度状态）、
    你在「另存为」里亲自选的那张图、你点过「下载」的更新包。
    installer.exe 只会写主程序本体（默认就落在桌面）与它自己的诊断日志。

========================================================================
D. PE 版本信息  +  Authenticode 签名主体
========================================================================
  PE 版本信息（VS_VERSIONINFO 资源，纯文本元数据，不是数字签名）

  MyAlbum.exe
      CompanyName          哈夫克
      FileDescription      哈夫克图片
      LegalCopyright       © 哈夫克
      ProductName          哈夫克图片
      OriginalFilename     MyAlbum.exe
      FileVersion          1.8.11.0
      ProductVersion       1.8.11.0
    -> 署名核对 ✓ 公司/产品均含 '哈夫克'

  MyAlbum_x86.exe
      CompanyName          哈夫克
      FileDescription      哈夫克图片
      LegalCopyright       © 哈夫克
      ProductName          哈夫克图片
      OriginalFilename     MyAlbum.exe
      FileVersion          1.8.11.0
      ProductVersion       1.8.11.0
    -> 署名核对 ✓ 公司/产品均含 '哈夫克'

  Setup.exe
      CompanyName          哈夫克
      FileDescription      哈夫克图片 安装程序
      LegalCopyright       © 哈夫克
      ProductName          哈夫克图片
      OriginalFilename     Setup.exe
      FileVersion          1.8.11.0
      ProductVersion       1.8.11.0
    -> 署名核对 ✓ 公司/产品均含 '哈夫克'

  Authenticode 签名状态（自签名，未导入证书的机器上必然不是 Valid）
    MyAlbum.exe
      status : UnknownError
      signer : CN=哈夫克, O=哈夫克, C=CN
      -> 主体含 '哈夫克' ✓
    MyAlbum_x86.exe
      status : UnknownError
      signer : CN=哈夫克, O=哈夫克, C=CN
      -> 主体含 '哈夫克' ✓
    Setup.exe
      status : UnknownError
      signer : CN=哈夫克, O=哈夫克, C=CN
      -> 主体含 '哈夫克' ✓

========================================================================
E. 关机构造串（弱信号，仅作参考，不参与判定）
========================================================================
  下面 b'shutdown' 计数为 0 是**正常的**，不是异常：
  main.py 里 SHUTDOWN_EXE = "shutdown" 确实是明文常量（已弃用 chr() 拼接
  那种反静态写法），但源文件被压进了 PYZ-00.pyz，而 PYZ 是 zlib 压缩的，
  裸字节扫描根本看不到。
  —— 这恰好说明为什么本节不参与判定：换个拼法就能骗过它，
     正确的做法是去读归档的真实索引（见 B 节）。

  补充（1.2.0 起）：默认流程**永远不会**走到 shutdown —— 答错改走
  「软件已锁定」页；只有带 --real-shutdown 启动才会走老的关机倒计时。
  这条能力仍然留在 manifest 的 '命令行参数' 一节里，没有偷偷去掉。
    MyAlbum.exe        b'shutdown'×0    b'/s'×456    b'/a'×486 
    MyAlbum_x86.exe    b'shutdown'×0    b'/s'×333    b'/a'×336 
    Setup.exe          b'shutdown'×0    b'/s'×1055   b'/a'×1061

========================================================================
总结
========================================================================
  [通过] 全部断言成立。
