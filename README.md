# OpenHarmony 日志智能聚合分析工具

> 一块 Hi3861 开发板 + 一条 USB 线，把板载温湿度/光照日志变成可查询、可聚合、可出根因报告、可邮件/Webhook 告警的 Web 看板。
>
> **本文是环境配置与使用说明。** 环境搭建步骤参考《OpenHarmony 开发板实验指导书 Windows 版 V2.1.15》第 1 章「开发环境搭建」，并结合本仓库的实际目录、脚本与固件实现做了项目化整理；日志规范、算法、接口与前端细节见 [`docs/`](docs/)。
>
> 仓库：<https://github.com/bratatata526/Open-Harmony>

---

## 1. 项目概览

### 1.1 数据链路

```text
板载 SHT20（温度/湿度） + AP3216C（光照）
        │
        ▼
firmware/hi3861/agri_logger/  ── 按《日志规范》输出单行日志（ASCII，≤128 字节）
        │  USB Type-C / CH340 / 115200
        ▼
tools/serial_capture.py 或 dashboard 实时读串口
        │
        ▼
agri_logger/parser → fields → windows（60s 窗口、5 信号加权打分）
        │
        ├─→ 异常族聚类 / 证据校验
        ├─→ 规则引擎（无 API Key 时）或 DeepSeek LLM（有 Key 时）
        ├─→ Markdown 根因报告（reports_live / reports_llm）
        └─→ 告警：控制台 / logs/alerts.jsonl / Webhook / 邮件
        │
        ▼
web/index.html（原生 JS，零构建、离线可用） → http://127.0.0.1:8080
```

### 1.2 已经实现的功能

- **设备端**：单设备模式 / 六棚逻辑设备模式；板载 KEY 运行时开关故障注入；OLED 显示当前模式；开机横幅 + 数值转换自检。
- **看板**：KPI、故障码分布、异常族聚合、日志密度时间线、实时日志流、大棚环境、根因报告、告警中心。
- **分析**：60 秒窗口 + 级别/故障码/静默/seq 缺口/阈值 5 信号打分；异常族聚类；证据逐条回查；LLM 与规则引擎双路。
- **报告**：Markdown 根因报告，自动标注数据来源与引擎；真机报告与模拟器报告分目录保存。
- **告警**：控制台、JSONL 审计、钉钉/飞书/企业微信 Webhook、SMTP 邮件；去重/冷却/限流/分级；支持演练模式。
- **交互**：设备聚焦、日志级别/模块/正则过滤、时间范围缩放、环境曲线点击筛选、报告阅读器、声音提示、布局复位。

### 1.3 三种运行形态

| 形态 | 数据来源 | 启动方式 | 能否生成设备报告 |
|---|---|---|---|
| **回放 replay** | `eval/dataset/d2_v2.log`（30 分钟、148 次故障注入、带标注）或 `--log` 指定 | `run.ps1 tools/dashboard_server.py --port 8080` | 不能（界面会提示） |
| **实时真机 live** | 开发板串口（`--live`，默认自动发现 COM） | 启动器 / `start_dashboard.cmd` / `--live` | **能** |
| **实时模拟器 live-sim** | `tools/openharmony_log_sim.py` 模拟日志 | `--live-sim` | 不能（模拟数据） |

> 回放和模拟器用于算法演示与前端开发；**要生成"设备报告"必须接真机并处于 live 模式**，这样报告里的数据来源才能标注为真机串口。

### 1.4 关于"一块板 = 6 个设备"

物理上只有 **1 个传感器节点**（板载 SHT20 + AP3216C）。`agri_logger_multi.c` 里的六棚模式是把同一份真实读数派生成 6 个逻辑大棚（固定偏置 + 热惯性），**基准值仍是真实传感器读数**；单设备模式（默认）则直接上报真实读数。两种模式的切换见 §7.3。

---

## 2. 环境要求

| 类别 | 要求 |
|---|---|
| **开发板** | 华清远见 FS-Hi3861（Hi3861 核心板）；板载 SHT20 温湿度、AP3216C 光照、0.96" OLED、用户 KEY；USB Type-C **数据线**（不是只供电线） |
| **PC** | Windows 10/11 x64；安装驱动/工具需要管理员权限；建议 ≥10 GB 空闲磁盘 |
| **固件工具链** | VSCode **System x64**（课程资料中的 1.78.2 即可）、DevEco Device Tool **3.0.x**、Hi3861 SDK（课程资料，约 0.5 GB）、DevTools_Hi3861V100 工具链（约 3 GB）、CH340 串口驱动 |
| **Python** | **3.8+ 64 位**（本项目实测 3.9.1）；安装在**全英文路径**，安装时勾选 **Add Python to PATH** |
| **网络** | pip 可用（国内建议镜像）；DeepSeek API 可选；不联网也能跑回放和规则引擎 |
| **可选账号** | DeepSeek API Key（LLM 根因）；SMTP 授权码（邮件告警）；钉钉/飞书/企业微信机器人 URL（Webhook） |

> **不需要**：MQTT Broker、STM32、微信小程序。指导书第 1 章的 1.2 / 1.3 / 1.4 分别是 MQTT、STM32、小程序实验环境，本项目不涉及；代码里出现的 `F2001 MQTT 连接失败` 只是**日志故障码**，不会真的连 MQTT。

---

## 3. 目录结构

```text
Open-Harmony/
├─ agri_logger/              # Python：解析、字段、窗口、分析、报告、告警
│  ├─ parser/                #   日志解析、字段表、农场模型
│  ├─ pipeline/              #   60s 窗口聚合与异常打分
│  ├─ analyzer/              #   异常族聚类、证据校验、LLM/规则根因
│  ├─ notifier/              #   控制台/文件/Webhook/邮件 四通道告警
│  └─ ...
├─ web/                      # 看板前端（index.html + 契约/sample，无构建步骤）
├─ tools/                    # 启动器、采集、模拟器、固件同步、自检、诊断
├─ firmware/                 # 固件源码 + SDK 集成文件（不随 SDK 提交）
├─ docs/                     # 01~16 项目文档
├─ eval/                     # 回放日志与标注数据集
├─ reports/                  # 报告样例
├─ reports_live/             # 真机实时报告（新生成的 *.md 不入库，保留历史样例）
├─ reports_llm/              # 模拟器报告（样例入库）
├─ logs/                     # 采集/启动器/告警审计日志
├─ assets/                   # 启动器图标
├─ hi3861_application/       # ★ 自备 Hi3861 SDK（约 0.5 GB，git 忽略）
├─ .venv/                    # ★ 本地 Python 虚拟环境（git 忽略）
└─ .env                      # ★ 本地密钥/告警配置（git 忽略）
```

> `hi3861_application/`、`.venv/`、`.env` 都**不在仓库里**。前者是因为太大，后两者是因为跟本机/密钥绑定。新电脑按 §4、§5 重建即可。

---

## 4. Python 看板环境（没有开发板也能先跑）

### 4.1 安装 Python

- 从 <https://www.python.org/downloads/windows/> 下载 **64 位** 安装包（建议 3.9 / 3.10；本项目实测 3.9.1）。
- 安装路径保持**全英文、无空格**（例如 `C:\Python39`），勾选 **Add Python to PATH**。
- 验证：

```powershell
python --version
py -3 --version
```

> 如果 `python` 指向 Microsoft Store 的占位程序，请在「设置 → 应用执行别名」里关掉它，或改用 `py -3`。

### 4.2 创建虚拟环境并安装依赖

```powershell
cd /d E:\harmony_run\Harmony        # 改成你的仓库路径
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

国内下载慢时：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

依赖清单（`requirements.txt`）：`pyserial`、`openai<1.60`、`PyYAML`、`Jinja2`、`rich`。

> `.venv` **不能跨电脑拷贝**：里面的 `pyvenv.cfg` 记录的是原电脑 Python 的绝对路径，换机必然报 `No Python at '...'`。到新电脑重新执行本节即可；`start_dashboard.cmd` 检测到 `.venv` 失效时会提示重建。

### 4.3 配置 `.env`

```powershell
copy .env.example .env
notepad .env
```

最少只需要填 `DEEPSEEK_API_KEY`（不填也能跑，报告会自动降级到规则引擎）：

```ini
DEEPSEEK_API_KEY=sk-你的Key
# 可选
# DEEPSEEK_MODEL=deepseek-flash
# DEEPSEEK_BASE_URL=https://api.deepseek.com
# LLM_DAILY_BUDGET_CNY=5.0
```

告警配置（邮件/Webhook）见 §7.2。`.env` 已被 `.gitignore` 忽略，**不要提交、不要截图外发**。

### 4.4 环境自检

```powershell
.\run.ps1 -Check
```

会打印 Python 路径/版本、依赖状态、`DEEPSEEK_API_KEY` 是否加载。若提示脚本被禁止运行，执行一次：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

也可以直接用 `check_all.cmd`（它还会检查串口、DevEco 烧录口、固件一致性，但后两步需要板子）。

### 4.5 先跑回放模式（不需要板子）

```powershell
.\run.ps1 tools/dashboard_server.py --port 8080
```

浏览器打开 <http://127.0.0.1:8080>。默认自动挑选：

1. `eval/dataset/d2_v2.log`（优先，30 分钟、148 次故障注入、带标注）
2. `logs/multi.log`（真机采集）
3. `eval/dataset/d2_demo.log`

指定其他文件：

```powershell
.\run.ps1 tools/dashboard_server.py --port 8080 --log logs/multi.log
```

> 回放模式只读文件，**不会**生成"设备报告"，也不会触发真实告警；这是刻意设计，避免把模拟数据当成真机结论。

### 4.6 创建桌面启动器（可选）

```powershell
powershell -ExecutionPolicy Bypass -File tools\make_shortcut.ps1
```

桌面会生成「OpenHarmony 日志分析工具」（像素扳手图标）。快捷方式里存的是本机绝对路径，所以 `.lnk` 不入库，每台电脑各自生成一次。

---

## 5. 开发板固件环境（按指导书第 1 章）

> 本节对应指导书 **1.1 OpenHarmony 环境搭建**（1.1.1～1.1.9）。如果你只跑 Python 看板，可以跳过本节，直接看 §6。

### 5.1 安装 VSCode

- 使用 **System x64** 版本（管理员登录的电脑必须用 System 版），课程资料中的 `VSCodeSetup-x64-1.78.2.exe` 即可。
- 必装插件：**C/C++**、**Chinese (Simplified)**、**Include Autocomplete**、**GN**；可选：vscode-icons、C-family Documentation Comments、EasyZoom。
- DevEco Device Tool 安装后还会用到 C/C++ 与 **CodeLLDB**，按提示装齐即可。

### 5.2 安装 DevEco Device Tool

- DevEco Device Tool 是 OpenHarmony 的一站式开发插件，支持代码编辑、编译、烧录、调试，以 VSCode 插件形式工作。
- 安装时选择 **全英文路径的 Python**；勾选 **Add Python to environment variables / Add Python to PATH**。
- 安装完成后在 VSCode 扩展栏确认 DevEco Device Tool 与 C/C++、CodeLLDB 都在。
- 本项目 `deveco.ini` 对应 **DevEco Device Tool 3.0.2 / Core 3.1.400**；更高版本一般兼容，界面可能略有差异。

### 5.3 获取 Hi3861 SDK

**不要直接使用 Gitee 上最新代码**（在线工程经常变化，可能与课程实验不兼容）。使用课程资料中的：

```text
\程序源码\hi3861_hdu_iot_application.rar
```

解压要求：

- 解压到**全英文、无空格、路径尽量短**的目录（例如 `D:\Hi3861\`）；
- 整个路径不超过 **260 个字符**（Windows 限制），否则编译会报路径过长；
- **推荐**把 SDK 放到本仓库根目录并命名为 `hi3861_application`，这样 `tools/sync_firmware.py`、`tools/sync_com_port.py` 可以直接工作：

```text
<仓库根目录>\
├─ firmware\
├─ tools\
├─ web\
└─ hi3861_application\      ← SDK 解压到这里
   ├─ src\
   ├─ .deveco\
   └─ ...
```

> SDK 约 0.5 GB、2.6 万个文件；`Tool/` 工具链约 3 GB。它们**都不入 Git**，新电脑需要重新获取/解压。

### 5.4 导入 SDK 到 DevEco

1. VSCode 打开后，进入 **DevEco Device Tool 主页** → **导入工程**。
2. 选择 `hi3861_application` 目录（zip 解压后可能叫 `hi3861_hdu_iot_application-master`）。
3. 后续弹窗：**SOC = Hi3861**，**开发板 = hi3861**，**框架 = hb**，点击导入。
4. 导入成功后，后续可从 DevEco 主页直接打开该工程。

### 5.5 配置编译工具链

先把课程资料「工具软件」里的 **DevTools_Hi3861V100** 工具包解压到**全英文目录**（例如 `D:\Hi3861\Tool\`），再按下面配置：

1. 左侧「工程配置」→ 选择自定义工具包（部分版本没有这一屏，直接下一步）。
2. 在 `hi3861` 下找到 **`compiler_bin_path`**，指向你解压的 **`DevTools_Hi3861V100_v1.0`** 目录（通常在课程资料的 `Tool\` 下）。
3. 同理检查 `.deveco/deveco.ini` 中的 `compiler_bin_path`：

```ini
compiler_bin_path = D:\Hi3861\Tool\DevTools_Hi3861V100_v1.0
```

> 这是**每台电脑都不同**的绝对路径。仓库里的 `deveco.ini` 只是模板，照抄别人的路径一定编译失败。

### 5.6 把本项目的固件集成进 SDK

本仓库的 `firmware/` 只是**版本存档**，编译器不会直接读它；需要把源码和编译配置放进 SDK。推荐流程：

```powershell
# 1) 覆盖三份 SDK 集成配置（SDK 在仓库根目录时）
Copy-Item firmware\sdk-integration\app_BUILD.gn  "hi3861_application\src\applications\sample\wifi-iot\app\BUILD.gn" -Force
Copy-Item firmware\sdk-integration\demo_BUILD.gn "hi3861_application\src\vendor\hqyj\fs_hi3861\demo\BUILD.gn" -Force
Copy-Item firmware\sdk-integration\deveco.ini    "hi3861_application\.deveco\deveco.ini" -Force

# 2) 把固件源码部署到 SDK（默认方向：firmware → SDK）
.venv\Scripts\python.exe tools\sync_firmware.py

# 3) 核对两边是否一致
.venv\Scripts\python.exe tools\sync_firmware.py --check
```

三个配置文件的职责：

| 文件 | SDK 目标路径 | 作用 |
|---|---|---|
| `app_BUILD.gn` | `src\applications\sample\wifi-iot\app\BUILD.gn` | 决定**编译哪个应用**；本项目只启用 `agri_logger:agri_logger` |
| `demo_BUILD.gn` | `src\vendor\hqyj\fs_hi3861\demo\BUILD.gn` | 官方 demo 集合；只保留 SHT20，供切回官方 demo 时使用 |
| `deveco.ini` | `.deveco\deveco.ini` | DevEco 的编译/烧录/监视配置（COM 口、工具链路径、烧录速率） |

`sync_firmware.py` 只同步三个源码文件：

```text
firmware/hi3861/agri_logger/agri_logger_multi.c  →  SDK/.../app/agri_logger/agri_logger_multi.c
firmware/hi3861/agri_logger/log_def.h             →  SDK/.../app/agri_logger/log_def.h
firmware/hi3861/agri_logger/BUILD.gn              →  SDK/.../app/agri_logger/BUILD.gn
```

> 如果 SDK 不在仓库根目录，`sync_firmware.py` 的默认路径会找不到。此时请手工把上面三个文件复制到 `<SDK>\src\applications\sample\wifi-iot\app\agri_logger\`，或把 SDK 放到仓库根目录再跑脚本。
>
> 指导书 1.1.6 里把产品配置写成 `.../wifi-iot/BUILD.gn`；本 SDK 实际文件是 `.../wifi-iot/app/BUILD.gn`，以实际存在的文件为准。

### 5.7 修改 COM 口与烧录参数

打开 `hi3861_application\.deveco\deveco.ini`，至少确认：

```ini
upload_port = COM5          # 改成设备管理器里 CH340 的 COM 号
monitor_port = COM5         # 同上
upload_speed = 921600
compiler_bin_path = D:\Hi3861\Tool\DevTools_Hi3861V100_v1.0
```

也可以让脚本自动把 COM 口同步成当前实际检测到的端口：

```powershell
.venv\Scripts\python.exe tools\sync_com_port.py          # 检测并写入
.venv\Scripts\python.exe tools\sync_com_port.py --check  # 只检查，不修改
```

> Windows 按**物理 USB 插口**分配 COM 号，换个 USB 口就可能从 COM3 变成 COM4/COM5。烧录前先确认端口，能避免大量"upload port is occupied"的误判。

### 5.8 编译

1. VSCode **打开 `hi3861_application` 目录**（不是仓库根目录）。
2. 左侧 **DevEco Device Tool** → **PROJECT TASKS**。
3. 点 **`Rebuild`**（首次建议 Rebuild，之后可 Build）。
4. 等待输出窗口出现 **`BUILD SUCCESS`**。首次编译会解压工具，约 1～2 分钟；之后通常更快。

> 注意：DevEco 的 **Stack Analysis** 也会打印 `[SUCCESS] Took 2.28 seconds`，但它**只分析旧固件，不编译不烧录**。判断是否真的编译成功，看 `BUILD SUCCESS` 和产物时间戳。

### 5.9 烧录

1. 安装 **CH340 驱动**：课程资料 `工具软件\CH340 驱动\CH341SER.EXE`，双击安装。
2. USB Type-C 连接开发板与电脑，打开板子左下角**电源开关 S1**。
3. 设备管理器确认出现 `USB-SERIAL CH340 (COMx)`。
4. DevEco → **`Upload`** → 确认 `upload_port` → 点 Upload。
5. 出现 **`Connecting, please reset device...`** 时，按一下板子的**复位键**。
6. 等待约 30 秒，提示烧录成功。
7. **★ 烧录完成后物理拔插一次 USB 线（拔掉 → 等 5 秒 → 重新插上）★**
   - HiBurn 会把芯片留在 bootloader；如果不拔插，板子可能连 ROM 日志都不打印，现象与"板子坏了"完全一样。
   - **按复位键不够**，必须重新枚举 USB。

> 烧录/监视期间，DevEco Monitor、`serial_capture.py`、看板**不能同时占用同一个 COM 口**（Windows 独占）。用哪个就关掉另一个；看板请先点「停止」再烧录。

### 5.10 验证固件是否真的生效

先关闭 DevEco Monitor 和看板，然后：

```powershell
.venv\Scripts\python.exe tools\serial_capture.py --list
.venv\Scripts\python.exe tools\serial_capture.py --monitor --port COM5 --baud 115200 --duration 30
```

采集期间**按一下板子的复位键**，应看到开机横幅：

```text
[agri_logger] FW=v2.8 mode=single-device(真实传感器直通), period=5000ms, tickfreq=100Hz, heap=...
I A003 AgriSelfCheck: 1 0 5 1001 8 20480 15 -355 z=-
```

这两行是"固件真的烧进去了"的判据：

- `FW=v2.8`：当前固件版本；改了日志格式请同步 +1。
- `tickfreq=100Hz`：1 tick = 10ms，时间戳单位正确。
- `AgriSelfCheck`：零 / 4 位 / 5 位 / 负数四个边界值都打全，说明数值转换正常。

更严格的自动校验：

```powershell
.venv\Scripts\python.exe tools\check_firmware.py --port auto --duration 20
```

它会检查版本号、自检行和故障码完整性。采集期间同样按一下复位键。

> 也可以直接用 DevEco 的 **Monitor**（`monitor_port` 选同一个 COM，115200）或串口调试助手（UartAssist）查看；注意串口助手的中文编码选项。

---

## 6. 启动与使用

### 6.1 图形启动器（推荐）

```powershell
# 首次生成桌面快捷方式（已生成可跳过）
powershell -ExecutionPolicy Bypass -File tools\make_shortcut.ps1

# 或直接双击
.\launcher.cmd
```

使用步骤：

1. 双击桌面「OpenHarmony 日志分析工具」（像素扳手图标）或 `launcher.cmd`。
2. 端口选「**自动发现（推荐）**」；也可以选具体 COM 口。
3. 点「**开始**」：启动看板服务 → 等就绪 → 自动打开 <http://127.0.0.1:8080>。
4. 点「**停止**」或关闭启动器：停止服务并**释放串口**。

启动器默认是**实时真机模式**（`--live`）。没有板子时请用 §6.3 的回放命令，不要用启动器。

### 6.2 一键脚本

- `start_dashboard.cmd`：带控制台的一键启动，检测 `.venv`（失效时提示重建并安装依赖），然后以 `--live` 启动 8080 看板。适合"不想用启动器、想看日志"的场景。
- `launcher.cmd`：启动器调试模式，报错会打在控制台。
- `check_all.cmd`：环境自检 + 串口列表 + 同步 DevEco 烧录口 + 固件一致性校验（最后一步约 15 秒，需要板子）。
- `run.ps1`：统一入口，加载 `.env` 后跑任意 Python 脚本；`run.ps1 -Check` 只做环境自检。

### 6.3 命令行三种模式

```powershell
# ① 回放：不需要板子，默认读 eval/dataset/d2_v2.log
.\run.ps1 tools/dashboard_server.py --port 8080

# ② 实时真机：自动发现串口；也可加 --com COM5
.\run.ps1 tools/dashboard_server.py --port 8080 --live --baud 115200

# ③ 实时模拟器：没有板子但想演示"实时链路"
.\run.ps1 tools/dashboard_server.py --port 8080 --live-sim
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--port 8080` | 看板端口（默认 8080） |
| `--log <文件>` | 回放模式指定日志文件 |
| `--live` | 实时读串口 |
| `--live-sim` | 实时模拟器 |
| `--com COM5` | 固定串口（默认自动发现；换 USB 口后可能变） |
| `--baud 115200` | 串口波特率（固件默认 115200） |
| `--window 60` | 窗口长度（秒） |
| `--threshold 0.25` | 异常阈值 |
| `--no-auto-analyze` | 关闭"检测到异常自动出报告" |

### 6.4 看板界面

打开 <http://127.0.0.1:8080> 后：

- **顶栏**：运行模式（回放/实时/离线）、设备模式（`AgriMode`）、告警状态、设备选择器、KPI、声音开关、复位布局。
- **左列**：故障码分布、异常族聚合。
- **中列**：日志密度时间线（点击看详情/缩放）、实时日志流（级别/设备/模块/正则过滤、跟随最新、清空、重新连接）。
- **右列**：大棚环境（点击棚卡片看详情；详情里点「区间末温度/湿度/光照」卡片只看对应曲线，再点一次恢复）、根因报告列表。
- **告警中心**：点顶栏「告警 已就绪 / 部分就绪」打开；显示通道、已发送/失败/策略跳过、最近告警，可点「测试文件通道 / 测试邮箱通道 / 测试Webhook」。
- **报告**：勾选 `AI分析` 后点「生成根因报告」；报告阅读器支持 Markdown 渲染和「另存为」。

### 6.5 报告生成的前提

界面只在 **实时真机（live && !sim）** 下允许生成设备报告；回放/模拟器会提示"无法生成设备报告"。这是为了避免把回放数据算出的报告误标成真机结论。

报告输出位置：

| 来源 | 目录 | 是否入库 |
|---|---|---|
| 真机实时 | `reports_live/` | 新生成的 `*.md` 被忽略；仓库里保留少量历史样例和 `.gitkeep` |
| 模拟器 | `reports_llm/` | 样例入库（未忽略，按需清理） |
| 样例 | `reports/` | 入库，供参考格式 |

### 6.6 告警

- 告警由实时分析触发（生成报告时），默认走「控制台 + `logs/alerts.jsonl` 审计」；邮件/Webhook 需在 `.env` 配置（§7.2）。
- 告警中心可以一键测试通道，不需要等真实故障。
- 演练模式 `ALERT_DRY_RUN=1`：只显示"会发什么"，不真的发，适合答辩前彩排。
- 审计历史在 `logs/alerts.jsonl`（已被 `.gitignore` 忽略）。

---

## 7. 配置说明

### 7.1 `.env`：LLM

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 空 | DeepSeek API Key；不填则自动降级规则引擎 |
| `DEEPSEEK_MODEL` | `deepseek-flash` | 模型名 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | API 地址 |
| `LLM_DAILY_BUDGET_CNY` | `5.0` | 每日成本上限（元） |

> `run.ps1`、启动器、看板服务启动时都会自动加载 `.env`。启动横幅会打印 `🤖 LLM 可用（deepseek-flash）` 或 `不可用 → 规则引擎降级`；没有 Key 不影响采集/解析/看板。

### 7.2 `.env`：告警（邮件默认清空，需要时自己填）

> ⚠️ **邮件通道默认关闭，配置项留空。** 请填成你自己的配置后再把 `ALERT_EMAIL_ENABLED` 改成 `1`；
> 真实邮箱地址/授权码只留在本机 `.env`，**不要写进 README、代码，也不要提交到 Git**。

```ini
# 总开关与策略
ALERT_ENABLED=1
ALERT_MIN_SEVERITY=P2      # P0 最严重；低于此级别只写审计文件
ALERT_DRY_RUN=0            # 1 = 演练，不真发
ALERT_COOLDOWN_S=180
ALERT_MAX_PER_HOUR=12

# 邮件（默认清空 = 不启用；请在这里填写你自己的 SMTP 配置）
ALERT_EMAIL_ENABLED=0
SMTP_HOST=
SMTP_PORT=465
SMTP_SECURITY=ssl
SMTP_USER=
SMTP_PASSWORD=
ALERT_EMAIL_FROM=
ALERT_EMAIL_TO=

# Webhook（钉钉/飞书/企业微信/自建）
ALERT_WEBHOOK_URL=
# ALERT_WEBHOOK_KIND=auto    # auto | dingtalk | feishu | wecom | generic
```

**填写提醒（邮件）**：

| 配置项 | 填什么 |
|---|---|
| `SMTP_HOST` | QQ 邮箱填 `smtp.qq.com`，163 填 `smtp.163.com` |
| `SMTP_PORT` / `SMTP_SECURITY` | QQ/163 一般 `465` + `ssl` |
| `SMTP_USER` | 你的完整邮箱地址 |
| `SMTP_PASSWORD` | 邮箱**授权码**（不是登录密码；QQ 邮箱在设置→账号里开启 IMAP/SMTP 后生成 16 位授权码） |
| `ALERT_EMAIL_FROM` | 一般与 `SMTP_USER` 相同 |
| `ALERT_EMAIL_TO` | 收件人；多个用逗号分隔 |
| `ALERT_EMAIL_ENABLED` | 填完改为 `1` |

- 填好后保存，在告警中心点「测试邮箱通道」验证；不需要等真实故障。
- 邮件失败会自动退避 120 秒；手动点「测试邮箱通道」会强制重试，不被退避挡住。
- Webhook 按 URL 自动识别钉钉/飞书/企业微信的负载格式，并检查 HTTP 200 里的 `errcode/code`；**暂不支持钉钉/飞书的"加签"**，请用自定义关键词或 IP 白名单。
- 配置完可在看板告警中心点「测试邮箱通道 / 测试Webhook」验证，不必等故障。
- 命令行体检：`.venv\Scripts\python.exe -m agri_logger.notifier --show-config`；只测邮件：`--email-only`；演练：`--dry-run`。

### 7.3 固件配置

文件：`firmware/hi3861/agri_logger/agri_logger_multi.c`

| 常量 | 默认 | 说明 |
|---|---|---|
| `SINGLE_DEVICE_MODE` | `1` | `1` = 单设备，直接上报板载真实读数；`0` = 六棚逻辑设备（同一真实基准派生 6 个 zone） |
| `FAULT_INJECT_INTERVAL` | `20` | 每隔多少轮注入一次故障；演示可调 2~3，正式跑 20~30，负样本设 0 |

- 板载 **KEY（GPIO_14）** 运行时切换故障注入：开机默认 `FAULT:OFF`（纯真实数据），按一下 `FAULT:ON`，再按一下关闭；OLED 第 1 行同步显示。
- 注入故障带设备号 `z=A`；真实硬件故障是 `z=-`，日志里可区分。
- 改日志格式/字段顺序/时基后，务必把开机横幅的 `FW=v2.8` 版本号 +1，并同步 `tools/check_firmware.py` 的期望值。
- 详细固件说明见 [`firmware/README.md`](firmware/README.md) 与 [`docs/04-firmware.md`](docs/04-firmware.md)。

### 7.4 DevEco 配置

`.deveco/deveco.ini` 关键项：

| 键 | 说明 |
|---|---|
| `upload_port` / `monitor_port` | 板子实际 COM 号（换 USB 口会变） |
| `upload_speed` | 921600（课程板默认） |
| `compiler_bin_path` | `DevTools_Hi3861V100_v1.0` 的绝对路径，每台电脑不同 |
| `board_frameworks.hb.build.product` | `wifiiot_hispark_pegasus@hisilicon` |

---

## 8. 常用命令

```powershell
# 环境
.\run.ps1 -Check
.\check_all.cmd

# 串口
.venv\Scripts\python.exe tools\serial_capture.py --check-env
.venv\Scripts\python.exe tools\serial_capture.py --list
.venv\Scripts\python.exe tools\serial_capture.py --monitor --port auto --duration 30
.venv\Scripts\python.exe tools\serial_capture.py --port auto --out logs/raw.log

# 固件
.venv\Scripts\python.exe tools\check_firmware.py --port auto --duration 20
.venv\Scripts\python.exe tools\sync_firmware.py
.venv\Scripts\python.exe tools\sync_firmware.py --check
.venv\Scripts\python.exe tools\sync_com_port.py

# 模拟器 / 离线分析
.venv\Scripts\python.exe tools\openharmony_log_sim.py --help
.venv\Scripts\python.exe tools\analyze_demo.py --input eval/dataset/d2_v2.log

# 告警
.venv\Scripts\python.exe -m agri_logger.notifier --show-config
.venv\Scripts\python.exe -m agri_logger.notifier --email-only
.venv\Scripts\python.exe -m agri_logger.notifier --dry-run

# 密钥扫描（提交前建议跑）
.venv\Scripts\python.exe tools\secret_scan.py --tracked
```

---

## 9. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 双击快捷方式没反应 | 看 `logs/launcher.log`；改用 `launcher.cmd`（带控制台）运行一次，报错会打印出来；快捷方式路径失效就重跑 `tools\make_shortcut.ps1` |
| `No Python at 'C:\Users\...\python.exe'` | `.venv` 是从别的电脑拷来的，已失效。删掉 `.venv`，运行 `start_dashboard.cmd` 自动重建，或按 §4.2 手工重建 |
| `ModuleNotFoundError: No module named 'serial'` | 依赖没装齐：`.venv\Scripts\python.exe -m pip install -r requirements.txt` |
| 设备管理器没有 CH340 / 串口列表为空 | 装 `CH341SER.EXE`；换一条**数据线**；换 USB 口后点【刷新】；用 `serial_capture.py --list` 确认 |
| 烧录报 `upload port is occupied` | ① 先关 DevEco Monitor / 看板 / `serial_capture.py`；② 端口号可能已变（`sync_com_port.py`）；③ 上一份看板留下孤儿子进程，重启电脑或结束 `python.exe` |
| 烧录成功但串口没日志 | **物理拔插一次 USB**（HiBurn 停在 bootloader）；按复位键；确认波特率 115200；跑 `check_firmware.py --port auto` |
| 看板一直「等待数据」 | 串口号选错 / 被 Monitor 占用 / 板子没复位。启动器状态条会直接给出原因；也可先 `serial_capture --monitor` 验证物理链路 |
| 编译报 `undefined reference to AP3216C_Init` 等 | `BUILD.gn` 的 `sources` 没列出对应 BSP 驱动；用回 `firmware/hi3861/agri_logger/BUILD.gn`，不要手删 |
| `compiler_bin_path` 无效 / 编译工具找不到 | 改 `deveco.ini` 或 DevEco 工程配置，指向自己的 `DevTools_Hi3861V100_v1.0`，不要照抄别人的 `E:\Code\...` |
| 路径过长 / 中文路径导致编译失败 | SDK 解压到短英文根目录（如 `D:\Hi3861\`）；Python 也装全英文路径；整个路径 <260 字符 |
| 8080 被占用 | 用 `netstat -ano` 找到监听 8080 的 PID 并结束进程，或改用 `--port 8081` |
| LLM 一直不可用 | `.env` 没填 `DEEPSEEK_API_KEY` 或网络不通；看启动横幅；没有 Key 会自动用规则引擎，不影响主流程 |
| 报告按钮提示"无法生成设备报告" | 当前是回放或模拟器模式；接真机后用 `--live` 启动（启动器默认就是 live） |
| 邮件收不到 | ① `SMTP_PASSWORD` 填的是**授权码**不是登录密码；② 查垃圾邮件/广告邮件；③ 校园网可能封 465，改用 Webhook；④ 告警中心点「测试邮箱通道」看具体错误 |
| 看板生成报告后没有告警 | 检查 `ALERT_MIN_SEVERITY`（P3 只写审计文件）、`ALERT_ENABLED`、`ALERT_DRY_RUN`；看 `logs/alerts.jsonl` 和告警中心「策略跳过」 |
| 中文显示乱码 | 固件日志本身是 ASCII；终端乱码时把控制台/串口助手编码设为 UTF-8 或 GBK 对应项，波特率 115200 |

更多真机踩坑记录见 [`docs/09-troubleshooting-log.md`](docs/09-troubleshooting-log.md)。

---

## 10. 文档地图

| 文档 | 内容 |
|---|---|
| [README.md](README.md) | 本文：环境配置与使用说明 |
| [docs/01-requirements.md](docs/01-requirements.md) | 选题定位、需求拆解、技术指标、评分对照 |
| [docs/02-log-spec.md](docs/02-log-spec.md) | **日志规范白皮书**（字段字典、故障码/错误码、冻结正则）——改固件前必读 |
| [docs/04-firmware.md](docs/04-firmware.md) | 固件开发、demo 选择、烧录与验证 |
| [docs/06-llm-analysis.md](docs/06-llm-analysis.md) | LLM 分层调用、提示词、缓存与成本、三级降级 |
| [docs/07-evaluation.md](docs/07-evaluation.md) | 评测方案、标注数据集、指标与消融实验 |
| [docs/08-deliverables-and-defense.md](docs/08-deliverables-and-defense.md) | 交付物、报告大纲、答辩问题库 |
| [docs/09-troubleshooting-log.md](docs/09-troubleshooting-log.md) | 真机调试踩坑记录与排查决策树 |
| [docs/12-launcher.md](docs/12-launcher.md) | 图形启动器：端口选择、停止释放串口、快捷方式 |
| [docs/13-alerting.md](docs/13-alerting.md) | 告警策略、四个通道、邮件可行性、常见故障 |
| [docs/14-api-interface-spec.md](docs/14-api-interface-spec.md) | 19 个 HTTP 接口的字段/类型/轮询节奏 |
| [docs/15-data-formats.md](docs/15-data-formats.md) | 从串口字节流到报告 Markdown 的 6 层数据格式 |
| [docs/16-frontend-dev-guide.md](docs/16-frontend-dev-guide.md) | 前端文件组织、接入方式、六条硬规范、自检清单 |

历史/参考文档（新版已退役，但仓库中保留）：`docs/03-hardware-and-wiring.md`（硬件接线）、`docs/05-python-pipeline.md`（Python 流水线）、`docs/10-data-authenticity.md`（数据真实性）、`docs/11-ppt-prompt-pack.md`（PPT 提示词）。

---

## 11. 数据口径与注意事项

- **真实读数**：温度/湿度来自板载 SHT20，光照来自板载 AP3216C；环境值不做人工伪造。
- **六棚**：6 个 zone 是同一真实基准派生出的逻辑设备，派生的是棚间差异，不是 6 份独立传感器数据。
- **故障注入**：仅在按 KEY 后开启，日志带 `z=A`；关闭时设备端不产生任何注入日志。演示时建议"先展示干净状态，再按 KEY 展示故障识别"。
- **报告来源**：真机报告在 `reports_live/`，模拟器报告在 `reports_llm/`；界面和报告正文都会标注数据来源与引擎，避免把模拟数据当真机结论。
- **密钥**：`.env` 已被 `.gitignore` 忽略；提交前可跑 `tools/secret_scan.py --tracked`。SMTP 授权码/DeepSeek Key 不要写进代码或 README。
- **SDK**：`hi3861_application/` 不入库；换电脑需要重新获取 SDK 并按 §5 集成。`firmware/` 是固件的版本源头，改固件请改 `firmware/` 再 `sync_firmware.py` 部署到 SDK。

---

## 12. 参考

- 《OpenHarmony 开发板实验指导书 Windows 版 V2.1.15》第 1 章「开发环境搭建」（VSCode、DevEco Device Tool、SDK、编译、烧录、串口打印），第 5 章「常见问题」。
- 项目内：[`docs/15-data-formats.md`](docs/15-data-formats.md)（日志/报告格式）、[`docs/14-api-interface-spec.md`](docs/14-api-interface-spec.md)（接口契约）、[`docs/16-frontend-dev-guide.md`](docs/16-frontend-dev-guide.md)（前端接入）。
