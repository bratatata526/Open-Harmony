# 12 · 图形启动器（选端口 → 开始 → 看板）

> 目标：不碰命令行也能把工具跑起来。日常使用和现场演示都建议走这里。
> 实现：`tools/launcher.py`（tkinter，**零额外依赖**，只用 Python 标准库）。

---

## 一、首次使用（三步）

```powershell
# 1) 创建桌面快捷方式（图标 = 像素扳手）
powershell -ExecutionPolicy Bypass -File tools\make_shortcut.ps1

# 2) 双击桌面上的「OpenHarmony 日志分析工具」
#    （或在仓库根目录双击 launcher.cmd —— 带控制台，排查用）

# 3) 窗口里：选端口 → 「开始」
```

「开始」之后会自动：起看板服务 → 等它就绪 → **打开浏览器** `http://127.0.0.1:8080`。

> ⚠️ 快捷方式里存的是本机绝对路径（`pythonw.exe` 与仓库位置），所以 **`.lnk` 不入库**，
> 每台电脑各自跑一次 `make_shortcut.ps1`。图标（`assets/wrench.ico`）是入库的。

---

## 二、界面只有四样东西

| 控件 | 说明 |
|---|---|
| **端口** | 默认「自动发现（推荐）」。下拉里是当前枚举到的串口（显示 `COM6 · USB-SERIAL CH340`）。板子换 USB 口后点【刷新】 |
| **开发板** | **只读**，当前只有 `Hi3861 / HiSpark Pegasus`。加板子只改 `tools/launcher.py` 里的 `BOARDS` 表 |
| **开始** | 起服务 + 打开看板。若检测到 8080 已有本项目在跑，会问你要不要直接打开，不重复启动 |
| **停止** | 停服务并**释放串口**（之后再变灰，可再次开始） |

状态条会实时显示：`运行中 ｜ 数据源 COM6 ｜ 已接收 1234 行`；没有数据时直接给出原因
（串口不存在 / 被 DevEco Monitor 占用 / 板子未复位），不用去翻命令行。

---

## 三、行为约定（重要）

1. **关窗口 = 停止监听服务 + 释放串口**。不留后台进程，不占串口。
2. 运行期间**独占串口**：此时 DevEco 无法 Upload / 烧录 / 开 Monitor。要烧录就先点「停止」。
3. 「停止」走的是服务端的 `/api/shutdown`：**先杀采集子进程释放串口，再关服务**。
   这不是多此一举——直接 taskkill 看板进程会留下孤儿子进程继续占着串口，现象是
   "看板关了，DevEco 还是打不开串口 / 报 upload port is occupied"。
4. 没有暂停功能：需要"先停一下再继续"就是「停止」→「开始」（重新开始会自动重连串口）。

---

## 四、常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 双击快捷方式没反应 | 先看 `logs/launcher.log`；再用 `launcher.cmd`（带控制台）运行一次，报错会打在屏幕上 |
| 快捷方式指向的路径不存在 | 仓库被移动/改名过 → 重跑 `tools\make_shortcut.ps1` |
| 提示「服务未能启动（8080 可能已被占用）」 | 上一份看板还在跑，或别的程序占了 8080。窗口里会附服务端最后几行日志。关掉占用者后点「开始」 |
| 状态条一直「等待数据」 | 三类原因：① 串口号选错 ② 串口被 DevEco Monitor 占着 ③ 板子没复位（刚烧录完需**物理拔插一次 USB**） |
| 下拉里没有串口 | 驱动没装 / USB 线只供电不通信 → 设备管理器看有没有「USB-SERIAL CH340」。插好后点【刷新】 |
| 图标显示成白纸 | `assets/wrench.ico` 缺失 → 跑 `.venv\Scripts\python.exe tools\make_icon.py` 重新生成 |
| 提示 `No module named tkinter` | 装的 Python 没勾 tcl/tk（罕见）。用官方安装包装一次即可 |

---

## 五、相关文件

| 文件 | 作用 |
|---|---|
| `tools/launcher.py` | 启动器本体（界面 + 服务进程管理） |
| `assets/wrench.ico` / `assets/wrench.png` | 像素扳手图标（多尺寸：16/32/48/64/128/256） |
| `tools/make_icon.py` | 图标生成器（**纯标准库**：zlib+struct 手写 PNG/ICO，不依赖 Pillow） |
| `tools/make_shortcut.ps1` | 桌面快捷方式生成器 |
| `launcher.cmd` | 带控制台的启动入口（排查用） |
| `logs/launcher.log` | 启动器与服务端输出（出问题先看这里） |

图标想换个样子：改 `tools/make_icon.py` 里的 `in_wrench()` 几何参数（钳口/环半径、手柄粗细），
然后重跑生成脚本即可，`--preview` 会额外输出一张放大预览图便于肉眼检查。
