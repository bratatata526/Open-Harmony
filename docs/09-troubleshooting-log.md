# 09 真机调试踩坑记录（实战验证过，别重踩）

> 本文档记录的都是**实际花了时间才定位到**的问题，不是理论推测。
> 每条都给出：症状 → 根因 → 解决 → 验证方法。
> **新同学上手前先读这一页，能省半天。**

---

## 坑 1 ★★★ 烧录后必须物理拔插一次 USB

### 症状
- 刚烧录成功，但串口**一个字节都收不到**
- 连 **ROM 引导日志都没有**（正常情况下复位后必然打印 `ready to OS start`）
- 现象与"板子彻底坏了"完全一样，极易误判为硬件故障

### 根因
HiBurn 烧录会把芯片置于 **bootloader 模式**。如果烧完没能正常退出，芯片就停在那里等待烧录命令，**不加载应用、也不打印任何东西**。
连续多次烧录而不断电，出现该状态的概率明显上升。

### 解决
```
拔掉 USB 线 → 等 5 秒 → 重新插上
```
注意：**不是按复位键**，必须物理断电。复位键只复位 CPU，不能退出 bootloader 状态。

### 验证
拔插后立即采集，应看到：
```
ready to OS start
sdk ver:Hi3861V100R001C00SPC025 2020-09-03 18:10:00
FileSystem mount ok.
wifi init success!
[agri_logger] stage-1 start: zone=A period=5000ms stack=4096
```

> **判据口诀**：**连 ROM 日志都没有 → 一定是板子状态问题，不是你的代码问题。**
> 因为 `ready to OS start` 在我们的应用加载之前就打印了。

---

## 坑 2 ★★★ DTR/RTS 电平：必须同时拉高

### 症状
串口能打开、驱动正常、板子刚烧录成功，但 `read()` 永远返回空。

### 根因
FS-Hi3861 把 CH340 的 **DTR/RTS 接到了芯片的复位/使能脚**，构成**自动复位电路**。
只有**同时拉高 DTR 和 RTS** 才释放复位，板子才开始运行并输出日志。

### 实测四种组合

| DTR | RTS | 结果 |
|---|---|---|
| False | False | ❌ 0 字节 |
| True | False | ❌ 0 字节 |
| False | True | ❌ 0 字节 |
| **True** | **True** | ✅ **正常输出** ← 本项目采用 |

### 解决
`tools/serial_capture.py` 的 `open_port()` 默认即为 `dtr=True, rts=True`。

```python
ser.dtr = True
ser.rts = True
```

### 验证
```powershell
python tools/uart_diagnose.py     # 自动遍历四种组合并报告哪个有数据
```

### 额外推论
**复位是靠串口 DTR/RTS 电平触发的，不是靠按键。** 所以打开串口时板子会自动复位一次并打印完整启动日志——**这是正常的，不是 bug**。

> **为什么这个坑特别坑**：网上教程里 `dtr=False` 和 `dtr=True` 两种写法都能找到依据（前者用于"防止开串口时板子重启"，后者用于自动复位电路）。**只有实测才知道哪种适用于你的板子。**

---

## 坑 3 ★★ 任务栈要把日志缓冲区算进去

### 症状
板子能启动、能打日志，但**每跑完一轮就自动复位**，永远进不到第 2 轮。

### 根因
初版照抄官方 `base_06` demo 的 `TASK_STACK_SIZE = 1024`。但我们的任务重得多：

| 项目 | 栈开销 |
|---|---|
| `log_def.h` 的 `msg[120]` | 120 字节 |
| `b1[40]`+`b2[40]`+`b3[40]`+`b4[40]` | **160 字节** |
| `agri_logger_demo.c` 的 `fa/fb/fc/fd[16]×4` | 64 字节 |
| `SHT20_ReadData` 内部 + `snprintf` | 可观但未知 |

**光日志缓冲区就 344 字节**，1KB 栈必然溢出。
而 **LiteOS-M 上栈溢出【不报错】，直接静默复位**——所以现象就是"跑一轮就重启"，极难排查。

### 解决
```c
#define TASK_STACK_SIZE     (1024 * 4)    /* 4KB，宁可给大不要给小 */
```

### 教训
**日志缓冲区大小必须算进任务栈预算。** 写 `TASK_STACK_SIZE` 时不要照抄 demo，要按自己的任务实际开销估。

---

## 坑 4 ★★ 延时用 `usleep()`，不要用 `osDelay()`

### 症状
配合坑 3 一起出现：每轮跑完就复位。

### 根因
`osDelay()` 的参数单位是 **RTOS tick，不是毫秒**。tick 频率随 SDK 配置而变，传 `5000` 到底延时多久取决于 `osKernelGetTickFreq()`，行为不确定。

而官方 demo 用的是 `sleep()` / `usleep()`（POSIX，单位明确），一直很稳。

### 解决
```c
#define MS_TO_US(ms)   ((ms) * 1000)
...
usleep(MS_TO_US(REPORT_PERIOD_MS));    /* 语义无歧义 */
```

### 原则
**能用单位明确的 API 就别用单位含糊的。** 差一个换算系数就是几小时的排查时间。

---

## 坑 5 ★★ 只点 Stack Analysis 不会编译

### 症状
点了按钮、看到 `[SUCCESS]`，但改了代码现象完全没变。

### 根因
DevEco 面板里有多个任务：

| 任务 | 作用 | 耗时 |
|---|---|---|
| `Build` / `Rebuild` | **编译源码生成固件** | ~97 秒 |
| `Upload` | **烧录到板子** | ~22 秒 |
| `Stack Analysis` | **只分析已有固件**的内存/栈占用，**不编译不烧录** | ~2 秒 |
| `Monitor` | 打开串口监视，**不编译不烧录** | — |

**看到 2 秒的 `[SUCCESS]` 基本就是 Stack Analysis**，不是编译。

### 解决
**判断"编译是否真的生效"看固件时间戳**，不要看 SUCCESS：

```powershell
Get-Item 'E:\Code\Harmony\hi3861_application\src\out\hispark_pegasus\wifiiot_hispark_pegasus\Hi3861_wifiiot_app_allinone.bin' | Select-Object LastWriteTime
```

更硬的办法是**反查固件里有没有你的字符串**：

```powershell
$b = [System.IO.File]::ReadAllBytes($bin)
$a = -join ($b | % { if ($_ -ge 32 -and $_ -lt 127) { [char]$_ } else { ' ' } })
$a.Contains('AgriSensor')     # True = 你的代码进去了
```

### 完整流程
```
Rebuild（等 BUILD SUCCESS，约 1.5 分钟）
   ↓
Upload（卡在 "Connecting, please reset device..." 就按板子复位键）
   ↓
★ 物理拔插 USB（见坑 1）★
   ↓
采集验证
```

---

## 坑 6 ★★ 串口独占：烧录 / Monitor / 脚本 / 看板 四者互斥

### 症状
脚本侧：
```
serial.serialutil.SerialException: could not open port 'COM3':
PermissionError(13, '拒绝访问。', None, 5)
```
烧录侧（实拍）：
```
Uploading with HiBurn
error 4: upload port is occupied.scons: *** [upload] Error 1
[FAILED] Took 0.86 seconds
```

### 根因
Windows 下**同一个 COM 口只能被一个程序打开**。以下四者两两互斥：
**DevEco 的 Upload（HiBurn）· DevEco 的 Monitor · 我们的采集器 · 我们的实时看板**。

> ⚠️ **实测踩过**：起看板做实时演示（`dashboard_server.py --live`），
> 然后去 DevEco 点 Upload，报 `upload port is occupied`，排查半天才发现是**自己的看板**占着。
> 看板启动时现在会主动打印这条警告：
> ```
> ⚠️  看板运行期间【独占 COM3】——此期间 DevEco 无法 Upload / 烧录 / 开 Monitor
> ```

### 解决
**用哪个就关另一个**，烧录前按顺序确认：

```powershell
# 1) 有没有我们自己起的进程占着（只看 .venv 里的，别误杀 DevEco 的 python）
Get-Process python | Where-Object { $_.Path -like 'E:\Code\Harmony\.venv\*' }
#   有 → 在对应终端按 Ctrl+C，或 Stop-Process -Id <PID>

# 2) 端口现在到底空不空（[OK] 才能烧）
.venv\Scripts\python.exe -c "
import serial, serial.tools.list_ports
for p in serial.tools.list_ports.comports():
    try:
        s = serial.Serial(p.device, 115200, timeout=0.2); s.close()
        print('  [OK]   %-6s 空闲，可以烧录' % p.device)
    except Exception as e:
        print('  [BUSY] %-6s 被占用: %s' % (p.device, e))
"
```

**退出 DevEco Monitor 的方法**：
1. 再点一次 `Monitor` 按钮（开关式）
2. 或在 Monitor 的终端标签里按 `Ctrl+C`
3. 或点终端标签上的垃圾桶图标
4. 都不行就 `Ctrl+Shift+P` → `Reload Window`

> **建议**：日常调试直接用我们的采集器，只在"怀疑采集器本身有问题"时才开 Monitor 交叉验证。
> **要烧录时，先把看板和采集器都停掉。**

### 附带坑：COM 口号会变，别写死
Windows 是**按物理 USB 插口**分配 COM 号的。同一个板子换一个 USB 口，
就可能从 COM3 变成 COM4、COM5；插回原来的口又变回 COM3。
**本项目实测一轮里出现过 COM3 → COM4 → COM5 → COM3。**

- **采集侧不用管**：`serial_capture.py --port auto`、看板 `--live`（不带 `--com`）
  都是**每次重连重新发现**端口，换号能自己跟上。
- **烧录侧要管**：DevEco 的 `upload_port` 是写死在 `.deveco/deveco.ini` 里的。
  端口变了就得改，或者在 Upload 面板的下拉框里选当前那个。
  **报 `port is occupied` 之前，先确认这个号是不是当前真实存在的号**——
  端口号不对时报的错也可能很像"被占用"。

---

## 坑 7 ★ 日志行超过 128 字节会被静默丢弃

### 症状
故障明明发生了，但日志里找不到那一行。没有任何报错。

### 根因
`hilog_lite` 的单条日志上限是 **128 字节**（官方文档明确），**超出后静默丢弃**——不报错、不截断、什么都不打印。

### 解决
- 固件侧：字段名用短名（`z`/`s`/`c`），值用整型（温度 ×10 存 `281` 而非 `28.1`）
- 采集侧：统计行长分布，`max > 128` 立即告警

我们的实现在 `serial_capture.py` 的统计里直接输出：
```
最长行          65 字节  ✅
超限行数        0
```

---

## 坑 8 ★ 同一份解析逻辑不要写两遍

### 症状
采集器打印出 `z=A z=A s=None` 这种错乱输出。

### 根因
`serial_capture.py` 早期自带一套正则（找旧的 `zone=`/`seq=` 格式），设备端换成"字段ID 值"新格式后没同步改。**解析逻辑有两份实现，改一处忘一处。**

### 解决
**采集器改为委托共享解析器**：
```python
from agri_logger.parser.parser import LogParser
_SHARED_PARSER = LogParser()
def parse_line(raw, line_no):
    return _SHARED_PARSER.parse(raw, src_name="capture", line_no=line_no)
```

### 推广
**任何"三端共享的定义"都只能有一份实现**。字段表、故障码表、级别排序都属于这类。
我们现在的做法：`agri_logger/parser/fields.py` 是唯一真源，固件、模拟器、解析器三方都从它派生，并有自动校验兜底：

```
三方一致性校验：模拟器输出 84 行，解析器成功 84 行，失败 0 行
✅ 模拟器 / 固件字段表 / 解析器 三方一致
```

---

## 坑 9 ★★★ 平台 printf 对多位数整数不可靠（最隐蔽的一个）

### 症状
日志里**多位数被腰斩成只剩首位**：

```
期望:  000015.047000 E A001 AgriSensor: 1 3 5 1001 6 1 9 5001 z=A
实际:  000015.047000 E A001 AgriSensor: 1 3 5 1    6 1 9 5    z=A
                                                    ↑1001→1  ↑5001→5

期望:  1 4 5 1004 6 3 2 -1270 z=A
实际:  1 4 5 1    6 3 2 -      z=A       ← -1270 变成 "-"
```

**只有多位数出错**：`50`（cost）正常，`286`（温度）正常，但 `1001` / `5001` / `20480` 全错。
所以极易被忽略——正常流量看起来完全正常，**只有故障注入时数值大才暴露**。

### 定位过程（这个方法论值得学）
1. **先怀疑自己的代码**：把 `agri_expand_field()` 的算法在主机上用 gcc 复现
   → 主机输出 `5 1001` **完全正确**
2. **再复现完整调用链**：从 `F()` 宏到最终消息体逐级打印
   → 主机输出 `1 3 5 1001 6 1 9 5001 z=A` **仍然正确**
3. **得出结论**：**同一份代码，主机对、板子错** → 问题在平台库，不在我们的逻辑

> **关键判据**：如果同一份代码在主机上正确、在目标板上错误，
> 那问题一定在**平台库或编译器**，继续读自己的代码是浪费时间。

### 根因
Hi3861 SDK 的 `printf` / `snprintf` 在**多位数整数**格式化上有缺陷
（嵌入式 newlib 的常见问题：整数格式化被配成非 32 位宽度）。

### 解决：日志路径上完全不用 stdio 格式化
自己写十进制转换，一次一个字符输出：

```c
static inline char *agri_u2a(unsigned int v, char *p)   /* 无符号转十进制 */
{
    char tmp[12]; int n = 0;
    if (v == 0) { *p++ = '0'; *p = '\0'; return p; }
    while (v > 0 && n < (int)sizeof(tmp)) { tmp[n++] = (char)('0' + (v % 10u)); v /= 10u; }
    while (n > 0) { *p++ = tmp[--n]; }
    *p = '\0';
    return p;
}

static inline char *agri_i2a(int v, char *p)            /* 有符号 */
{
    if (v < 0) { *p++ = '-'; return agri_u2a((unsigned int)(-(long long)v), p); }
    return agri_u2a((unsigned int)v, p);
}

static inline char *agri_pad6(unsigned int v, char *p)  /* 时间戳补零 */
```

**改动范围**（全部在 `log_def.h` / `agri_logger_multi.c`）：
- `agri_fmt_pair()` 替代 `snprintf(buf, "%d:%d", id, val)`
- `agri_expand_field()` 改为逐字符解析，不用 `strtol`
- `agri_log_emit()` 用 `strcpy` 风格手写拼接 + `fputs` 输出，不用 `printf("%06u.%06u ...")`
- 入口横幅去掉所有 `%d/%u`

**验证**（主机 gcc 编译真实头文件）：

```
000015.047000 E A001 AgriSensor: 1 3 5 1001 6 1 9 5001 z=A     ✅
000015.047000 W A001 AgriSensor: 1 4 5 1004 6 3 2 -1270 z=A    ✅
000240.000000 I A003 AgriHealth: 1 48 8 20480 15 0 9 5120 z=-  ✅
999999.999999 I A002 AgriReport: 1 24 9 52 z=A                ✅
```

### 附带踩坑：嵌入式 stdio 是按需裁剪的

绕开格式化后，输出整行时**不能用 `fputs` / `puts`**：

```
riscv32-unknown-elf-ld: undefined reference to `fputs'
```

**原因**：这个 SDK 的 stdio 是**按需裁剪**的，`fputs` 没有链进 libc。
**实测可用的只有 `printf` 和 `putchar`。**

**最终方案**：手工拼好整行，再用 `putchar` 逐字符输出：

```c
for (const char *s = line; *s != '\0'; s++) {
    putchar((int)(unsigned char)*s);
}
```

> **教训**：嵌入式平台的 C 库不是桌面 glibc，**能用的函数是子集**。
> 遇到 `undefined reference` 时不要加 `#include`，而是换一个已知可用的等价函数。

### 教训
**轻量平台上的 `printf` 不可靠。** 日志这种"必须准确"的路径要自己控制格式，
不能把正确性寄托在平台库上。

> **附带好处**：绕开 stdio 后，日志路径的开销也大幅下降——
> 在 5 秒周期的实时任务里，这本身就是合理选择。

---

## 坑 10 ★★★ "我关了开发板，为什么还有日志在刷？"

这是**看板使用方式**的坑，不是 bug，但现场演示时最容易翻车（老师/评委一定会问）。

### 症状
开发板断电了、USB 拔了，`http://127.0.0.1:8080` 的日志流照样一行一行往外冒。

### 根因
看板默认是**回放模式**：它读的是磁盘上的静态日志文件
（默认 `eval/dataset/d2_v2.log`），然后**循环播放**——
和开发板是否通电、USB 是否插着**没有任何关系**。
它的用途是让算法效果可复现（同一份输入 → 同一份结论），
**不是**在连真机。

### 解决：三种模式分清

| 启动命令 | 数据来源 | 板子断电后 | 用途 |
|---|---|---|---|
| `dashboard_server.py` | 静态文件循环回放 | **照常刷**（这是设计如此） | 演示算法、复现结果 |
| `dashboard_server.py --live` | 真机串口（COM3） | **6 秒内变红 OFFLINE，流停止** | 现场证明"真的在采设备数据" |
| `dashboard_server.py --live-sim` | 模拟器子进程 | **6 秒内变红 OFFLINE，流停止** | 没有板子时演示实时链路 |

界面上的标识（不会看错）：

- 右上角徽标：`回放 · d2_v2.log`（琥珀色）／`实时 · COM3`（绿色跳动）／`离线 · COM3已停`（红色）
- 日志流面板标题旁的模式条：回放时直接写明
  **"回放中 · 循环播放 xxx.log —— 与开发板是否通电/连接【无关】"**

### 附带的两个真实技术坑（都在 `_live_loop` 里）

**坑 10a：子进程必须加 `-u`，否则实时流"假死"。**
Python 的 `stdout` 在**非终端**（管道）下是**块缓冲**——攒满 8KB 才刷一次；
而人在终端里手动跑同样的命令是**行缓冲**，看起来一切正常。
于是出现最诡异的现象：**手动跑有输出，看板里一行都没有**。
解决：`Popen([sys.executable, "-u", ...])`，并设 `PYTHONUNBUFFERED=1`。

**坑 10b：子进程的 `stderr` 必须是 `DEVNULL`，不能是 `PIPE`。**
采集器/模拟器的启动横幅写在 stderr。若设成 `PIPE` 又没人读，
缓冲区写满（约 4~64KB）后子进程会**阻塞在 `write()` 上**，
再也产不出数据 —— 表现为"进程活着，但零日志"。
解决：`stderr=subprocess.DEVNULL`。

**坑 10c：数据源断了要能自己回来 —— 但"自动重连"有个陷阱。**
`_live_loop` 是一个**看护循环**：子进程退出后隔 5 秒重连
（`attempt` 计数可以在 `/api/live/status` 里看到），
所以"拔线 → 插回"、"板子重启"都不需要重启看板。
重连等待期间 `online` 照实报 `false`，**不会假装在线**。

> ⚠️ **陷阱（实测踩过，详见坑 13）**：`serial_capture.py` 自己也有断线重连循环，
> 它**永远不会退出**。如果给它一个写死的端口号，它就会**死磕那个号**。
> 板子拔了再插，Windows 很可能把它枚举成别的 COM 号
> （本项目实测一轮里出现过 COM3 → COM4 → COM5 → COM3），
> 于是子进程一直重试一个已经不存在的口 ——
> **现象就是"拔线后显示断开，插回去却再也接不上"**。
> 修法：给子进程传 `--port auto`，并加一个「重新连接」按钮做兜底。

### 验证
```powershell
# 1) 起实时看板
.\run.ps1 tools/dashboard_server.py --port 8080 --live-sim

# 2) 看状态（另开一个终端）
curl.exe -s http://127.0.0.1:8080/api/live/status
# {"enabled":true,"source":"simulator","online":true,"lines":49,"pid":33292,"attempt":1,...}

# 3) 杀掉数据源，模拟"关板子"（把 pid 换成上面返回的）
Stop-Process -Id 33292 -Force

# 4) 6 秒后再看 → online 变 false、lines 冻结、error 说明原因
curl.exe -s http://127.0.0.1:8080/api/live/status
# {"online":false,"lines":49,"silent_s":9.9,"error":"数据源已退出（returncode=...），5 秒后重连…"}
```
**实测结果**：`online=True → 杀进程 → 8 秒后 online=False、lines 冻结在 49 → 5 秒退避后 attempt=2 自动恢复、lines 继续增长`。

---

## 坑 11 ★★★ 改了源码却忘了重新烧录（表现为"数值被截断"）

**这是坑 9 的复发形态，也是最容易误判成"平台有 bug"的一次。**

### 症状（真机实测）
看板里出现**本该是 5 位、却只有 1 位**的故障码，界面显示成"未知故障"：

```
真机: 000047.193000 I A003 AgriHealth: 1 9 8 1 15  9 6 z=-
真机: 000029.059000 E A003 AgriHealth: 1 5 5 3 6 1 15  z=D
真机: 000027.502000 W A001 AgriSensor: 1 7 5 1 6 3 2 - z=A
```

- `5 3` 本该是 `5 3006`（4 位故障码只剩首位）
- `2 -` 本该是 `2 355`（温度只剩一个负号）
- `8 1`（堆=1 字节）、`9 6`（栈=6 字节）——数值荒谬

### 定位过程（这套方法值得照搬）

**第一步：先排除"线上丢字节"。** 把原始字节按 `repr()` 打出来，而不是肉眼看字符串：

```python
for l in [x for x in open("logs/probe.log", encoding="utf-8") if "AgriHealth" in x]:
    print(repr(l))
```
结果三行**完全一致、行长稳定 52 字符、换行完整** → 不是丢字节，是固件就这么打的。

**第二步：统计"字段结构指纹"。** 把每行归一化成 `(tag, 字段ID序列)` 再聚合，
比逐行看快得多，一眼就能发现"哪个模块的结构不对"：

| 模块 | 真机实际 | 当前源码应产生 |
|---|---|---|
| AgriSensor | `1,2,3,4` ✓ | 1,2,3,4 |
| AgriReport | `1,9` ✓ | 1,9 |
| AgriHealth | **`1,8,15,9`** ✗ | 1,8,**9**,15 |
| AgriHealth(故障) | **`1,5,6,15`** ✗ | 1,5,6,**8** |

**第三步（决定性）：把手上的源码搬到 PC 上编译运行。**
同一份 `log_def.h`，用宿主 gcc 编译（见 `tools/_hostcheck/`），输出**完全正确**：

```
主机: 1 5 8 1 9 6 15 0 z=-      ← 正确
真机: 1 5 8 1 15  9 6 z=-       ← 错误
```
> **"同一份代码，主机对、板子错"** 有两种可能：① 平台 bug（坑 9 就是）；
> ② **板子上跑的压根不是这份代码**。必须用第四步区分。

**第四步：找"当前源码不可能产生"的特征。** 这一步是决定性的，不能省：

- 当前源码 `do_health_check()` 的**所有分支**传的 zone 都是 `'-'`，
  但真机打出了 **`z=D`** —— 当前源码**产生不出来**。
- 当前源码故障健康分支用的是 `AGRI_W`（WARN），
  真机打出的是 **`E A003 AgriHealth`** —— 当前源码里健康日志**没有 `E` 级别**。

两条都对不上 → **板上是旧固件**，不是平台 bug。

### 根因
源码在最后一次烧录**之后**又改过（健康日志的字段顺序、zone、级别都调整过），
但板子没重烧。旧版本用的是 `snprintf` 拼字段 —— 于是同时呈现**旧结构 + 坑 9 的截断**，
两种现象叠在一起，极容易被误判成"平台又出问题了"。

> ⚠️ **连带影响**：`logs/multi.log` 是这份旧固件采的，里面 `heap`/`miss` 字段
> **不可信**（被截断过）。基于它得出的内存类结论要重新采一遍再下。

### 解决（两条，缺一不可）

**1) 给固件加版本号，开机横幅里带上**（`agri_logger_multi.c`）：

```c
printf("[agri_logger] FW=v2.1 stage-2 ready: 6 zones, period=5000ms, stack=8192, heap=");
```
**改了日志格式/字段顺序，必须把这个号 +1。**
以后"改了代码但现象没变"，看一眼开机横幅就知道板上是哪一版。

**2) 加一行"数值转换自检"日志**，开机就打，专测最容易被平台坑的四种值：

```
I A003 AgriSelfCheck: 1 0 5 1001 8 20480 15 -355 z=-
                        ↑零 ↑4位   ↑5位     ↑负数
```
- 打全 → 数值转换在真机上正常
- 被截断（`1001`→`1`、`-355`→`-`）→ 立刻暴露，不用等业务日志出怪现象

> 这一行还是**答辩时的现场判据**：可以直接指着它说明
> "我们知道自己平台的 printf 有缺陷，所以自己写了十进制转换，并且开机就验证它"。

### 验证
重新编译（DevEco: PROJECT TASKS → `Rebuild`，约 97 秒）→ `Upload` → **物理拔插 USB**，
然后跑固件一致性校验工具：

```powershell
.venv\Scripts\python.exe tools/check_firmware.py --port auto --duration 150
# 采集开始后【按一下板子复位键】：开机横幅和自检行只在开机瞬间打印
```

**实测结果（2026-10-10，FS-Hi3861 真机）**：

```
【判据 1】固件版本号
  ✔ 开机横幅版本号：FW=v2.1

【判据 2】开机自检行（四种最容易出错的数值）
  原始行：000000.013000 I A003 AgriSelfCheck: 1 0 5 1001 8 20480 15 -355 z=-
    ✔ 字段 1  = 0        (零)
    ✔ 字段 5  = 1001     (4 位)
    ✔ 字段 8  = 20480    (5 位)
    ✔ 字段 15 = -355     (负数)

【判据 3】故障码完整性
  合法故障码：F1001×5  F1004×4  F3005×5  F5001×1

【附】字段结构指纹 → ✔ 9 种结构全部符合固件源码

  结论：✔ 全部通过 —— 板上跑的确实是当前这份代码
```

**关键点**：自检行在**开机后 13 毫秒**就打出来了（`000000.013000`），
四个边界值一个不少 —— 说明自写的十进制转换在真机上完全可靠。

**修复前后的对比（同一块板子）**：

| 判据 | 修复前（旧固件） | 修复后（FW=v2.1） |
|---|---|---|
| `AgriHealth` 字段序列 | `1,8,15,9` ✗ | **`1,8,9,15`** ✔ |
| 4 位故障码 | `3006` → `3`、`1004` → `1` ✗ | **`F3005` / `F1001` 完整** ✔ |
| 负数 | `-1270` → `-` ✗ | **`-355` 完整含负号** ✔ |
| 字段 15（miss） | 空值 `15 ` ✗ | **`15 -355` / `15 2`** ✔ |
| 宽度自检 | 无 | **开机 13ms 即验证** ✔ |

其中**负数 `-1270` 能完整打印**是最硬的一条：它正是坑 9 那个平台缺陷的靶心，
旧固件必然打成 `-`。这一条同时证明了"新固件在跑"和"绕开 printf 的方案有效"。

### 教训
**"主机对、板子错"不等于"平台有 bug"。** 先问一句：**板子上跑的，真的是我手上这份代码吗？**
判据不是文件时间戳（构建产物比源码新是正常的，本项目就被这一点误导过），
而是**只有新代码才会产生 / 才能产生的特征**——比如字段顺序、负数打印、开机自检值。

---

## 坑 12 ★★★ 时基错位：定时是对的，时间戳慢了 10 倍

**这个坑最阴的地方是"看起来一切正常"。** 定时、循环、日志节奏全对，
只有时间戳悄悄慢了一个数量级，而所有下游分析都建立在时间戳上。

### 症状（真机实测）
日志看起来完全正常，节奏也对；但把看板接上真机后，数字开始对不上：

```
记录数 525 条，seq 从 62 涨到 103（41 轮循环），
而设备时间戳只从 31.624 走到 52.866 —— 21.2 秒
```

41 轮循环只花了 21 秒 → 每轮 0.52 秒。但固件里明明是 `usleep(5000000)`（5 秒）。

### 定位：拿秒表量，别猜
```powershell
# 记录 T0 的设备时间戳和 seq，等 40 秒，再记一次
```
**实测结果**：

| 指标 | 数值 |
|---|---|
| 真实经过时间 | **40.1 秒** |
| 设备时间戳前进 | **4.1 秒** |
| 比例 | **0.103**（≈1/10） |
| 每轮循环真实耗时 | **5.02 秒** ✓ 与 `usleep(5000000)` 完全一致 |

结论一句话：**定时是对的，时间戳的单位是错的。**

### 根因
`LOSCFG_BASE_CORE_TICK_PER_SECOND = 100`
（见 `device/hisilicon/hispark_pegasus/sdk_liteos/platform/os/Huawei_LiteOS/targets/hi3861v100/include/target_config.h`）

即 **1 tick = 10 ms，不是 1 ms**。而 `log_def.h` 里把 tick 当毫秒写死：

```c
(unsigned)(_t_ / 1000ULL),                    /* ← 按 1ms/tick 算，错 10 倍 */
(unsigned)((_t_ % 1000ULL) * 1000ULL)
```

### 危害（比"时间戳不准"严重得多）

1. **窗口语义全错**：分析端按"设备秒"切 60 秒窗口，实际切成了约 **600 秒**。
   窗口越界、异常占比、压缩比……所有指标都失去意义。
2. **静默检测永远不触发**：判据是"间隔 > 2×正常周期(5s)"。
   时基慢 10 倍后，5 秒的真实间隔显示成 0.5 秒，
   `gap_max` 永远够不到 10 秒的阈值 → **占召回贡献最大的那条判据直接失效**。
3. **真机数据与评测数据集不同尺度**：模拟器发的是真实秒，
   真机发的是 1/10 秒，两边混在一起算指标等于白算。

> 这三个后果都不是"显示不好看"，而是**会让算法指标虚高或虚低**。
> 如果没做这一步核对，中期报告里所有基于真机数据的时间类指标都是错的。

### 解决：用 `osKernelGetTickFreq()` 动态换算，不写死魔数
```c
#define AGRI_LOG(lv, dom, tag, zone, f1, f2, f3, f4)                            \
    do {                                                                        \
        unsigned long long _t_ = (unsigned long long)osKernelGetTickCount();    \
        unsigned _f_ = (unsigned)osKernelGetTickFreq();                         \
        if (_f_ == 0) { _f_ = 100; }   /* 兜底：本平台实测 100Hz */              \
        agri_log_emit((lv), (dom), (tag), (zone), (f1), (f2), (f3), (f4),       \
                      (unsigned)(_t_ / _f_),                                    \
                      (unsigned)((_t_ % _f_) * (1000000U / _f_)));              \
    } while (0)
```
好处：**与 SDK 配置解耦**。tick 频率改成 1000Hz 也自动正确，不用改代码。

### 验证（宿主编译，不用反复烧板子）
`tools/_hostcheck/` 里的桩把 tick 频率做成可设置，直接复现真机环境：

| 输入 tick | 期望 | 实际 |
|---|---|---|
| 2750 @ 100Hz | `000027.500000` | **`000027.500000`** ✔（旧代码输出 `000002.750000`） |
| 31314 @ 100Hz | `000313.140000` | **`000313.140000`** ✔ |
| 0 @ 100Hz | `000000.000000` | **`000000.000000`** ✔ |
| 27507 @ **1000Hz** | `000027.507000` | **`000027.507000`** ✔ 自动适配 |

**真机复核**：开机横幅里现在带 `tickfreq=100Hz`，一眼就能核对时间戳单位。
再用"秒表法"量一次，比例应从 0.103 变成 **1.00**：

```powershell
# 设备时间戳前进量 ÷ 真实经过时间 应该 ≈ 1.0
```

### 教训
**时间戳是所有时序分析的基石，必须用秒表核对一次，不能假设单位。**
具体做法：记下设备时间戳 → 用手机/系统时钟等 30~60 秒 → 再记一次 → 算比例。
比例不是 1.0 就说明时基有问题。

> **凡是"平台给了个计数器"，先确认它的单位再拿来做时间轴。**
> CMSIS-RTOS2 有 `osKernelGetTickFreq()` 就是干这个用的，不要凭直觉假设它是毫秒。

---

## 坑 13 ★★ 前端"插回去也接不上"：子进程被钉死在旧端口号上

**这是使用者第一个报上来的问题**，而且极有迷惑性：
看板明明做了自动重连，为什么接不回来？

### 症状
拔掉 USB → 界面 6 秒内变红 `OFFLINE`（这部分是正常的）；
**插回去 → 仍然是 OFFLINE，日志流不恢复。**

### 定位
把数据源实际执行的命令行打印出来，一眼就看到了：

```powershell
curl.exe -s http://127.0.0.1:8080/api/live/status
# 看 cmd 字段：
#   "... serial_capture.py --port COM3 --baud 115200 --min-level D --raw"
#                                 ^^^^^^^^^^^ 钉死的端口号
```

板子插回后枚举成了别的号（比如 COM5），而子进程还在重试 COM3。

**根因**：父进程（看板）和子进程（采集器）**各自都有重连逻辑**。
子进程的重连循环**永远不会退出**，所以父进程的重连逻辑**根本没机会执行**。
父进程每轮重新发现端口的代码写了也白写。

### 解决
1. **子进程改用 `--port auto`** —— `serial_capture.py` 每轮重试都会 `discover_port()`，
   换号能自己跟上。看板不指定 `--com` 时就传 `auto`。
2. **加强制重连兜底**：`/api/live/reconnect` 接口 + 前端「重新连接」按钮。
   它把子进程连根拔掉、重新发现端口再开，是现场最可靠的手段。
   前端还会在掉线 **3 秒后自动触发一次**，多数情况用户不用手动点。
3. **重连不清空已收到的记录**（`restart_live()` 不碰 `self.records`）——
   重连不该丢数据。

> 实现细节：强制重连用 **generation 计数**而不是布尔标志。
> 只用一个 `_live_stop` 会有竞态——旧线程可能刚好走到 `while` 判断之前，
> 看到它已被重置成 `False` 就又转一圈，于是两个线程同时读同一个串口。
> 代号一变，旧线程下一轮必然退出。

### 验证（实测）
```
T0  : online=True  source=COM3  lines=62
      杀掉数据源 pid=39004（模拟拔线）
      强制重连: ok=True  reconnects=1
T+16: online=True  source=COM3  lines=88
      新 cmd = serial_capture.py --port auto --baud 115200 --min-level D --raw
      记录保留 = 88 条（重连不清空）
```

### 教训
**"自动重连"要分清是谁在重连。**
父进程和子进程各自都有重连逻辑时，如果子进程被钉死在一个易变的资源标识
（端口号、IP、文件路径）上，父进程的重连逻辑就形同虚设。

> 排查这类问题的第一步永远是：**把实际执行的命令行打印出来看**。
> 这次如果不去看 `/api/live/status` 的 `cmd` 字段，
> 光看代码是发现不了"传给子进程的是 COM3 而不是 auto"的。

---

## 坑 14 ★★ 头文件能找到、编译也过了，链接期却说"未定义"

**这个坑的迷惑性在于：报错位置（链接）和真实原因（构建脚本）隔了十万八千里。**

### 症状
给固件加了一行 `AP3216C_Init()` / `AP3216C_ReadData()`（真实光照来源），
在文件顶部 `#include "hal_bsp_ap3216c.h"`。

构建输出：前面 327 个目标**全部成功**，卡在最后一步链接：

```
[328/329] ACTION ... run_wifiiot_scons
riscv32-unknown-elf-ld: libagri_logger.a(agri_logger_multi.o):
    in function `agri_logger_entry':
    undefined reference to `AP3216C_Init'
riscv32-unknown-elf-ld: ... undefined reference to `AP3216C_ReadData'
scons: *** [output\bin\Hi3861_wifiiot_app.out] Error 1
BUILD FAILED!!!!
```

### 根因
**GN 不会自动把 BSP 的 `.c` 编进来**，必须在 BUILD.gn 的 `sources` 里点名。
我们照抄的是 `base_06_i2c_sht20` 的配置，它只有 SHT20 + PCF8574：

```gn
sources = [
    "agri_logger_multi.c",
    "//vendor/hqyj/fs_hi3861/common/bsp/src/hal_bsp_pcf8574.c",
    "//vendor/hqyj/fs_hi3861/common/bsp/src/hal_bsp_sht20.c"
]
```

头文件在 include 路径里，所以**编译阶段找得到声明、一路通过**；
但实现所在的 `.c` 从没被编译成 `.o`，链接时自然找不到符号。

### 解决
对照同芯片的官方 demo（`base_07_i2c_ap3216c/BUILD.gn`），补上驱动源文件：

```gn
sources = [
    "agri_logger_multi.c",
    "//vendor/hqyj/fs_hi3861/common/bsp/src/hal_bsp_pcf8574.c",
    "//vendor/hqyj/fs_hi3861/common/bsp/src/hal_bsp_sht20.c",
    "//vendor/hqyj/fs_hi3861/common/bsp/src/hal_bsp_ap3216c.c"   /* ← 补这一行 */
]
```

### 定位方法（可复用）
1. **看报错发生在哪个阶段**。`undefined reference` 一律是**链接期**问题，
   不是语法问题——**不要去改代码**。
2. **头文件 ≠ 实现**。`#include` 只提供声明；实现必须被编进某个目标。
3. **去同芯片的官方 demo 里找对照**。`vendor/hqyj/fs_hi3861/demo/base_*/BUILD.gn`
   每个 demo 都列了它需要的 BSP 源文件，照抄最保险。
4. 换用新的外设（I2C/GPIO/PWM）时，**先查 demo 的 BUILD.gn，再看头文件**。

> **判断依据**：如果报错出现在 `[328/329]` 这种最后阶段、
> 而前面所有编译目标都成功，那几乎一定是**构建脚本缺源文件**，
> 与 C 代码本身无关。

---

## 快速排查决策树

遇到"收不到数据"，按这个顺序走，**不要跳步**：

```
① 连 ROM 日志都没有（按复位键后）
   → 板子状态问题 → 物理拔插 USB（坑 1）

② 有 ROM 日志，但没有我们的日志
   → 固件没烧进去 → 查固件时间戳/字符串（坑 5）

③ 一个字都没有，但板子有反应
   → DTR/RTS 电平 → 跑 uart_diagnose.py（坑 2）

④ 有数据但解析率 0%
   → 烧的是官方 demo（裸 printf）还是我们的 agri_logger？（查固件字符串）

⑤ 跑一轮就重启
   → 任务栈（坑 3）→ 延时 API（坑 4）

⑥ 打不开串口 / PermissionError
   → Monitor 占着 → 关掉（坑 6）

⑦ 故障日志"消失"
   → 行长超 128 被静默丢弃（坑 7）

⑧ 数值被腰斩（1001 → 1，-1270 → -）
   → 平台 printf 多位数缺陷 → 日志路径改用自写转换（坑 9）

⑨ 板子关了日志还在刷
   → 看板是【回放模式】在读静态文件，跟板子无关 → 用 --live（坑 10）

⑩ 数值被截断（1001→1、-355→-），但源码明明是对的
   → 先排除线上丢字节（看 repr）→ 再查字段结构指纹
   → 若出现"当前源码不可能产生"的特征（如健康行的 z=D）
     → 板上是【旧固件】，重新 Rebuild + Upload（坑 11）
   → 开机横幅的 FW=vX.Y 和 AgriSelfCheck 行是判据

⑪ 日志看起来正常，但时间/窗口/间隔类指标全不对
   → 【秒表法】核对时基：设备时间戳前进量 ÷ 真实经过时间 应 ≈ 1.0
   → 不是 1.0 → tick 单位假设错了，用 osKernelGetTickFreq() 动态换算（坑 12）

⑫ 看板"拔线显示断开，插回去接不上"
   → 先看 /api/live/status 的 cmd 字段，端口号是不是被钉死了
   → 是 → 子进程改用 --port auto；再用「重新连接」按钮兜底（坑 13）

⑬ 编译全过，链接期报 undefined reference to 某个外设函数
   → 头文件只管声明，实现要在 BUILD.gn 的 sources 里点名（坑 14）
   → 去 vendor/hqyj/fs_hi3861/demo/base_*/BUILD.gn 找同芯片的对照
```

---

## 附：一键排查命令

```powershell
cd E:\Code\Harmony

# 环境
.venv\Scripts\python.exe tools/serial_capture.py --check-env

# 端口在不在
.venv\Scripts\python.exe tools/serial_capture.py --list

# 波特率对不对
.venv\Scripts\python.exe tools/serial_capture.py --scan-baud

# DTR/RTS 哪个组合有数据
.venv\Scripts\python.exe tools/uart_diagnose.py

# 看物理链路有没有字节（不解析）
.venv\Scripts\python.exe tools/serial_capture.py --monitor --port COM3 --duration 20

# 正式采集
.venv\Scripts\python.exe tools/serial_capture.py --port COM3 --duration 60 --out logs/raw.log

# 看板三种模式
.venv\Scripts\python.exe tools/dashboard_server.py --port 8080              # 回放（静态文件）
.venv\Scripts\python.exe tools/dashboard_server.py --port 8080 --live-sim   # 实时（模拟器）
.venv\Scripts\python.exe tools/dashboard_server.py --port 8080 --live       # 实时（真机 COM3）
```
