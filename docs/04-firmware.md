# 04 设备端固件开发（基于本仓库已就绪的 SDK）

> **重大前提**：你的工作区 `E:\Code\Harmony` 里**已经有一套完整可用的环境**，不需要从零搭建：
> - `hi3861_application/` —— HiSpark hi3861_hdu_iot_application SDK（含 `vendor/hqyj/fs_hi3861` 华清远见 FS-Hi3861 板级适配）
> - `harmony_sdk/` —— 同源的另一份 SDK 副本
> - `Tool/DevTools_Hi3861V100_v1.0/` —— RISC-V 交叉编译工具链
> - `Tool/HiBurn.exe` —— 烧录工具
> - `Tool/CH340/` —— USB 转串口驱动
> - `Tool/mosquitto-1.6.9-install-windows-x64.exe` —— MQTT Broker
> - `Tool/paho.mqtt.embedded-c-master.zip` —— 嵌入式 MQTT C 客户端库
> - `Tool/MDK5/`、`Tool/ST-Link/`、`Tool/en.stm32cubemx_v5-6-0.zip` —— 工作区附带的 STM32 工具链（**本项目不使用**，只是把工作区内容列全）
> - `OpenHarmony开发板实验指导书Windows版v2.1.15(1).pdf` —— 官方实验指导书
>
> **所以本项目的"环境搭建"风险从"高"降到"低"。** 下面直接讲怎么写代码。

---

## 0. 你不需要从零写任何驱动

`hi3861_application/src/vendor/hqyj/fs_hi3861/demo/` 下已有 **40 个可用示例**，本项目所需的全部硬件能力都已覆盖：

| 能力 | 现成 demo（直接抄） | 说明 |
|---|---|---|
| **温湿度采集** | `base_06_i2c_sht20/` | SHT20 温湿度传感器驱动，`SHT20_ReadData(&t, &h)` |
| **光照/接近/红外** | `base_07_i2c_ap3216c/` | AP3216C 三合一传感器 |
| **OLED 显示** | `base_08_i2c_ssd1306/` | 0.96" 128×64 |
| **风扇（模拟水泵）** | `base_02_i2c_pcf8574_fan/` | PCF8574 IO 扩展 + `set_fan(true/false)` |
| **蜂鸣器（报警）** | `base_03_i2c_pcf8574_buzzer/` | 同一 PCF8574 |
| **RGB 灯（状态指示）** | `base_05_i2c_aw2013/`、`Ext_Agriculture_Test/task/hal_bsp_pwm_rgb.c` | — |
| **按键（现场触发故障）** | `base_01_key/`、`Ext_Agriculture_Test/task/key_user_task.c` | 演示时手按一下就产生故障日志 |
| **WiFi 联网** | `network_02_wifi_sta/` | — |
| **MQTT 上云** | `network_05_wifi_mqtt/`、`smartFarm/mqtt_send_task.c` | 已对接华为云 IoTDA 格式 |
| **NFC 配网** | `network_06_wifi_nfc_sta/`、`base_09_i2c_nfc/` | — |
| **多任务（CMSIS-RTOS2）** | `kernel_01_task/` ~ `kernel_08_message_queue/` | 任务/定时器/事件/互斥锁/信号量/消息队列 |
| **★ UART 任务骨架** | `Ext_Agriculture_Test/task/uart_recv_task.c`、`smartCar_uart_udp/task/uart_recv_task.c` | 官方 demo 的 UART 任务创建方式可借；**它的"帧结束"判据有缺陷，不要照抄**（本项目不接第二路串口，用不到） |
| **★ 智慧农业完整项目** | `smartFarm/` | 温湿度采集 + 阈值自动控制风扇 + MQTT 收发 + OLED 显示 |
| **★ 农业传感器扩展板** | `Ext_Agriculture_Test/` | 多任务架构模板（oled/key/uart 任务分离） |

> **结论：本项目的固件工作是"改造现有 demo"而不是"从零开发"。** 工作量从 3 周压缩到 3~5 天，这是这个工作区给你们的巨大红利。

### 0.1 板载传感器的驱动就在 BSP 里（这才是我们真正要用的）

**一块 FS-Hi3861 = 一个传感器节点**，三项读数全部来自板载芯片，驱动在 SDK 的板级支持包里已经写好：

| 需要的读数 | BSP 头文件（`hi3861_application/src/vendor/hqyj/fs_hi3861/common/bsp/include/`） | 读法 |
|---|---|---|
| 温度 `t` + 湿度 `h` | `hal_bsp_sht20.h` | `SHT20_ReadData(&temperature, &humidity)`，浮点读数，写日志前 ×10 转整型 |
| 光照 `l`（ALS） | `hal_bsp_ap3216c.h` | `AP3216C_ReadData(&ir, &als, &ps)`，取 `als` 作为光照值 |

**这就是"数据真实性"的全部来源**：一个节点、两个芯片、三项读数（同一次采样）。固件不需要额外模块、不需要外接 ADC、也不需要第二路串口——`sensor_hub.c` 的职责就是把这两个驱动读到的数放进 `ZoneState`。

> **为什么把这一点单列一节**：这三项读数会被下游反复引用（原始值、6 个 zone 的派生值、告警阈值都挂在这三个数上）。先确认来源只有一处，后面所有"数据是不是真的"的问题才有唯一答案。
> **一个坑先说在前面**：`BUILD.gn` 的 `sources` 必须把两个驱动源文件都列上（`hal_bsp_sht20.c`、`hal_bsp_ap3216c.c`）。只抄 SHT20 那份配置就编译，链接期会报 `undefined reference to AP3216C_Init`——这个坑我们踩过，完整定位过程见 `docs/09-troubleshooting-log.md`。

---

## 1. 三条实现路线（按优先级）

| 路线 | 做法 | 上手 | 适用 |
|---|---|---|---|
| **路线 1（首选）** | 复制 `smartFarm/` 或 `Ext_Agriculture_Test/`，在其多任务骨架上**加入我们的日志宏与 6 个 zone** | 1~2 天 | A 同学主攻 |
| **路线 2** | 在 `src/applications/sample/wifi-iot/app/` 下新建自己的应用（改 `app/BUILD.gn` 一行即可） | 半天 | A 同学；不动 vendor 目录 |
| **路线 3（立即开工）** | Python 日志模拟器（`tools/openharmony_log_sim.py`），**B、C 同学今天就能开始 Python 侧开发** | 5 分钟 | 全体，工具链无关 |

> **项目管理硬建议**：**B、C 同学从 W1 起就走路线 3**（用模拟器推进 Python 侧），A 同学并行攻路线 1。**不要三个人集体等真机固件。**

---

## 1.5 ★ 怎么指定烧录哪个 demo，以及怎么烧（先看这节）

### 1.5.1 指定 demo：只改一个文件

**唯一需要改的是 `demo/BUILD.gn`**，规则是"**取消注释哪一行，就编译那个 demo**"：

```gn
// 文件：hi3861_application/src/vendor/hqyj/fs_hi3861/demo/BUILD.gn
lite_component("demo") { 
  features = [ 
    "base_06_i2c_sht20:base_sht20_example",     // ← 只有这行没注释 = 只编译它
    # "base_01_key:base_key_example",           // ← 带 # 的被忽略
    # "base_07_i2c_ap3216c:base_ap3216c_example", 
  ] 
} 
```

> ⚠️ **一次只启用一个**。同时打开多个会导致多个 `SYS_RUN` 一起跑，日志互相干扰，你分不清哪行是哪个 demo 打的。

**另外两处不要动**，它们会自动串起来：

| 文件 | 内容 | 作用 |
|---|---|---|
| `src/applications/sample/wifi-iot/app/BUILD.gn` | `"//vendor/hqyj/fs_hi3861/demo:demo"` | 告诉系统"编译 hqyj 的 demo 组件" |
| `src/vendor/hqyj/fs_hi3861/demo/BUILD.gn` | `features = [...]` | 告诉系统"组件里具体编译哪个例子" |

**本项目当前设置**：已改为只启用 `base_06_i2c_sht20`（SHT20 温湿度，每秒打印一次），其余全部注释。

### 1.5.2 烧录步骤（DevEco Device Tool，`.deveco/deveco.ini` 已配好）

VSCode 里**工作区要打开 `hi3861_application` 目录**（不是仓库根目录 `E:\Code\Harmony`），然后：

1. 左侧 **DevEco Device Tool** 图标 → **PROJECT TASKS**
2. 点 **`Build`**（首次建议 **`Rebuild`**）→ 等输出窗口出现 `BUILD SUCCESS`
3. 点 **`Upload`** → 弹出烧录面板 → 点 **Upload**
   - `upload_port` 已配为 **COM3**，`upload_speed` 已配为 **921600**
   - **若卡在 `Connecting, please reset device...`** → 按一下板子上的复位键

4. **★★ 烧录完成后，物理拔插一次 USB 线（拔掉 → 等 5 秒 → 重新插上）★★**
   - **这一步不能省。** HiBurn 会把芯片置于 bootloader 模式，若未正常退出，
     板子会停在那里**连 ROM 引导日志都不打印**，现象与"板子坏了"完全一样。
   - 详细症状与判据见 `docs/09-troubleshooting-log.md` 坑 1。

> ⚠️ **不要只看 `[SUCCESS]` 就以为编译生效了**。DevEco 面板里的 `Stack Analysis` 也会输出
> `[SUCCESS] Took 2.28 seconds`，但它**只分析旧固件，不编译不烧录**（真编译要 ~97 秒）。
> **判断依据是固件时间戳或反查固件内字符串**，方法见 `docs/09-troubleshooting-log.md` 坑 5。

**验证**（先关掉 DevEco 的 Monitor，它占着 COM3）：

```powershell
.venv\Scripts\python.exe tools/serial_capture.py --monitor --port COM3 --duration 30
```

期待每秒一行 `temperature = 25.50      humidity = 60.20`；**对着板载 SHT20 哈气**，湿度应上升。

> **常见坑**：DevEco 的 Monitor 和我们自己的 Python 采集器**不能同时占用 COM3**（Windows 独占访问）。用哪个就关另一个。

> **如何确认烧进去的是新固件**：改一下 demo 里的打印字符串（比如加个 `[v2]` 前缀）再烧一次，串口输出变了就说明烧录生效。**这是排查"改了代码但现象没变"最快的方法。**

---

## 2. 路线 1：改造 `smartFarm` 多任务骨架

### 2.1 现状分析（读源码得到的结论）

`smartFarm/smartFarm_demo.c` 的入口结构：

```c
static void smartFarm_Project_example(void)
{
    SC_peripheral_init();                              // 外设初始化
    nfc_config();                                      // NFC 配网
    mqttClient_connectServer / init / subscribe;       // MQTT 三步
    // 创建 3 个线程
    osThreadNew((osThreadFunc_t)mqtt_send_task,     NULL, &options);  // 上报
    osThreadNew((osThreadFunc_t)mqtt_recv_task,     NULL, &options);  // 收命令
    osThreadNew((osThreadFunc_t)sensor_collect_task,NULL, &options);  // 采集
}
SYS_RUN(smartFarm_Project_example);     // ← 应用入口宏
```

`smartFarm/sensor_collect_task.c` 的采集逻辑（第 116~156 行）：

```c
void sensor_collect_task(void)
{
    float temperature = 0, humidity = 0;
    while (1) {
        SHT20_ReadData(&temperature, &humidity);       // 读温湿度
        show_humi_page(humidity);                      // OLED 显示
        sys_msg_data.temperature = (int)temperature;
        sys_msg_data.humidity    = (int)humidity;
        // 阈值自动控制：湿度 >= 上限 → 开风扇；<= 下限 → 关风扇
        if (sys_msg_data.nvFlash.smartControl_flag != 0) {
            if (sys_msg_data.humidity >= sys_msg_data.nvFlash.humi_upper) { set_fan(true); ... }
            else if (sys_msg_data.humidity <= sys_msg_data.nvFlash.humi_lower) { set_fan(false); ... }
        }
        usleep(100 * 1000);                            // 100ms 采集一次
    }
}
```

**这就是题目要求的"温室大棚 + 阈值自动灌溉"的现成实现！** 我们要做的是：
1. 把 `usleep(100ms)` 改成 **5 秒上报周期**（题目要求）；
2. 把 `set_fan()` 语义从"降湿风扇"改为"灌溉水泵"（日志里用 `st=1` 表示 irrigating）；
3. **加入日志**（原代码几乎不打印过程日志，这是最大的缺口）；
4. 加入 **6 个 zone 的状态管理**；
5. 加入 **故障注入器**。

### 2.2 目录与构建

```bash
# 在 SDK 里新建我们的应用（不改动 vendor，避免升级被覆盖）
hi3861_application/src/applications/sample/wifi-iot/app/agri_logger/
├── BUILD.gn
├── agri_logger.c        # 入口 + 任务创建
├── log_def.h            # ★ 日志宏（唯一出口，见 docs/02 §2.3）
├── zone.c / zone.h      # 6 个逻辑设备状态
├── sensor_hub.c / .h    # 板载 SHT20 / AP3216C 采集（温度/湿度/光照三项读数）
└── fault_inject.c / .h  # 故障注入器
```

**`app/BUILD.gn` 改为**：

```gn
import("//build/lite/config/component/lite_component.gni")
lite_component("app") {
    features = [
        "agri_logger:agri_logger",       # ← 只编译我们的应用
        # "//vendor/hqyj/fs_hi3861/demo:demo",   # 调试时可切换回官方 demo
    ]
}
```

**`agri_logger/BUILD.gn`**：

```gn
static_library("agri_logger") {
    sources = [
        "agri_logger.c",
        "zone.c",
        "sensor_hub.c",
        "fault_inject.c",
    ]
    include_dirs = [
        "//vendor/hqyj/fs_hi3861/common/bsp/include",   # hal_bsp_sht20.h 等
        "//vendor/hqyj/fs_hi3861/common/bsp/src",
        "//utils/native/lite/include",
        "//kernel/liteos_m/components/cmsis/2.0",
        "//base/iot_hardware/peripheral/interfaces/kits",
    ]
    cflags = [ "-Wno-unused-variable" ]
}
```

> **`include_dirs` 要按实际路径核对**：用 `dir /s hal_bsp_sht20.h` 在 `hi3861_application/src/vendor/hqyj` 下找一下真实位置再填。**构建报"找不到头文件"时，99% 是这个路径写错了。**

### 2.3 入口代码

```c
/* agri_logger.c */
#include <stdio.h>
#include <unistd.h>
#include "ohos_init.h"
#include "cmsis_os2.h"

#include "log_def.h"
#include "zone.h"
#include "sensor_hub.h"
#include "fault_inject.h"

#define ZONE_COUNT        6
#define TASK_STACK_SIZE   (1024 * 4)     /* 轻量设备栈很宝贵，4KB 起步 */
#define REPORT_PERIOD_MS  5000
#define HEALTH_PERIOD_MS  10000

static osThreadId_t g_report_tid[ZONE_COUNT];

static void zone_report_task(void *arg)
{
    int idx = (int)(intptr_t)arg;
    ZoneState *z = zone_get(idx);

    osDelay(1000 + idx * 137);           /* 错开启动，让日志交错而非成簇 */

    while (1) {
        /* ---- 1) 先看要不要注入故障（测试构建） ---- */
        if (zone_should_inject(idx)) {
            fault_inject(idx);
        }

        /* ---- 2) 正常周期上报（INFO，一行装完，注意 ≤128 字节） ---- */
        unsigned s = zone_next_seq(idx);
        LOG_I(DOM_SENSOR, TAG_SENSOR,
              "z=%c s=%u t=%d h=%d l=%u tick=%u",
              z->id, s, z->temp_x10, z->humi_x10, z->lux, (unsigned)osKernelGetTickCount());

        /* ---- 3) 上报动作自身（A002） ---- */
        LOG_I(DOM_REPORT, TAG_REPORT, "z=%c s=%u cost=%u tx=48",
              z->id, s, 12 + (unsigned)(s % 40));

        /* ---- 4) 阈值判断 → WARN + 执行器动作（A004） ---- */
        if (z->temp_x10 > TEMP_HIGH_THR_X10) {
            LOG_W(DOM_SENSOR, TAG_SENSOR,
                  "z=%c s=%u fault=F1005 c=5 f=1 tx10=%d", z->id, s, z->temp_x10);
            ctrl_irrigation(idx, 1);      /* 内部打印 F4002 st=1 */
        }

        osDelay(REPORT_PERIOD_MS);
    }
}

static void health_task(void *arg)
{
    (void)arg;
    while (1) {
        /* 任务栈水位与堆使用：这是 A003 的日志来源 */
        unsigned heap = (unsigned)mem_get_free();      /* 按 SDK 实际 API 替换 */
        if (heap < 4096u) {
            LOG_W(DOM_HEALTH, TAG_HEALTH, "z=-1 s=0 fault=F3006 c=15 heap=%u", heap);
        } else {
            LOG_I(DOM_HEALTH, TAG_HEALTH, "z=-1 s=0 heap=%u", heap);
        }
        osDelay(HEALTH_PERIOD_MS);
    }
}

static void agri_logger_entry(void)
{
    osThreadAttr_t attr = {0};
    attr.attr_bits  = 0;
    attr.cb_mem     = NULL;
    attr.cb_size    = 0;
    attr.stack_mem  = NULL;
    attr.stack_size = TASK_STACK_SIZE;

    zone_init();          /* 初始化 6 个逻辑设备 */
    sensor_hub_init();    /* 板载 SHT20 + AP3216C 初始化（三项真实读数） */

    char name[24];
    for (int i = 0; i < ZONE_COUNT; i++) {
        snprintf(name, sizeof(name), "zone_%c_report", "ABCDEF"[i]);
        attr.name     = name;
        attr.priority = osPriorityNormal;
        g_report_tid[i] = osThreadNew((osThreadFunc_t)zone_report_task,
                                      (void *)(intptr_t)i, &attr);
    }

    attr.name     = "health";
    attr.priority = osPriorityNormal;
    osThreadNew((osThreadFunc_t)health_task, NULL, &attr);

    LOG_I(DOM_HEALTH, TAG_HEALTH, "z=-1 s=0 fault=F5001 c=6 boot=1");
}

SYS_RUN(agri_logger_entry);
```

> **`osDelay` vs `usleep`**：官方 demo 用 `usleep(100*1000)`（微秒）。在 CMSIS-RTOS2 下建议统一用 **`osDelay()`**（毫秒），语义更清晰且与 RTOS tick 对齐。**但要注意**：如果任务里既有 `usleep` 又有 `osDelay`，两条时间基准不同，日志间隔统计会失真——这也是一个可以写进报告的"踩坑记录"。

### 2.4 关于 6 个 zone 的栈开销

6 个上报任务 × 4KB 栈 = 24KB RAM。Hi3861 的可用 RAM 有限（约 300KB 级，但 LiteOS-M 已占一部分），**24KB 是可以接受的**，但要实测：

```c
/* 在 health_task 里监控每个任务的栈水位 */
osThreadAttr_t attr;
osThreadGetAttr(tid, &attr);
/* 或用 osThreadGetStackSpace(tid) 查剩余栈空间 */
```

**如果 RAM 吃紧的降级方案**（写进报告的技术权衡）：
- 方案 A：改为 **2 个上报任务**，每个交替处理 3 个 zone；
- 方案 B：改为 **1 个任务循环 6 个 zone**（日志不再交错，真实感下降，但 RAM 最省）；
- **推荐先试 A**，并在日志里输出 `heap=` 与栈水位，用**实测数据**决定，而不是拍脑袋。

---

## 3. 日志模拟器（路线 3，**B、C 同学立即开工**）

模拟器逐字节复刻真机日志格式，让整个项目**不依赖硬件进度**。

### 3.1 关键设计

| 设计点 | 做法 | 为什么 |
|---|---|---|
| 格式同构 | 输出必须通过 `docs/02` §6 的正则**自检**，不通过直接抛异常 | 保证真机与模拟器可被同一解析器处理 |
| 行长约束 | 每行 `len(line) <= 128`，超长直接报错 | 复刻 `hilog_lite` 的硬限制（**静默丢弃**最难排查） |
| 虚拟时钟 | 内部维护 tick，`--speed N` 可加速 N 倍 | 演示时可 10 倍速跑完"一整天的数据" |
| 故障注入 | 与固件共用同一张场景表（`config/scenarios.yaml`） | 真机与模拟器故障时序、故障码、错误码完全一致 |
| 固定 seed | 默认 `20260101` | **6 个组拿到完全相同的日志流**，算法效果可横向比较 |
| 丢包/乱序 | `--drop-rate 0.001 --reorder-rate 0.002` | 真实串口会丢字节。**只喂完美数据的采集器，一上真机就崩** |
| 截断行 | 按概率产生半行/乱码行 | 检验解析器"永不抛异常"的纪律 |
| 重启 | 每 N 分钟产生 `boot=+1` 且 `s=0` | 检验时间轴换算与重启检测 |

### 3.2 用法

```bash
# 1) 打印到屏幕观察
python tools/openharmony_log_sim.py --zones A,B,C,D,E,F --period 5 --speed 10

# 2) 落文件，作为离线数据集
python tools/openharmony_log_sim.py --duration 1800 --out eval/dataset/d2_1800s.log \
       --label-out eval/dataset/d2_1800s.gt.jsonl --seed 20260101

# 3) 喂给采集器（文件跟随模式）
python tools/openharmony_log_sim.py --out logs/live.log &
python -m agri_logger collect --source file --path logs/live.log --follow
```

### 3.3 实现骨架（注意自检与行长检查）

```python
#!/usr/bin/env python3
"""tools/openharmony_log_sim.py —— Hi3861 多任务日志模拟器
输出与真机字节级同构的日志流。正则与 docs/02-log-spec.md §6 保持一致。"""
import argparse, json, random, re, sys, time
from dataclasses import dataclass, field

# ---- 自检：必须与规范一致 ----
RE_SELF = re.compile(
    r"^\d{6,}\.\d{6} [DIWEF] [0-9A-Fa-f]{4,5} [A-Za-z][A-Za-z0-9_]{0,30}: .+$")
MAX_LINE = 128                      # hilog_lite 硬限制：超过会被静默丢弃

DOMAIN = {"sensor": "A001", "report": "A002", "health": "A003",
          "ctrl": "A004", "task": "A005", "comm": "A006", "fault": "A007"}
TAG = {"sensor": "AgriSensor", "report": "AgriReport", "health": "AgriHealth",
       "ctrl": "AgriCtrl", "task": "AgriTask", "comm": "AgriComm", "fault": "AgriFault"}
ZONES = list("ABCDEF")

@dataclass
class ZoneSim:
    zid: str
    seq: int = 0
    boot: int = 1
    t_x10: int = 250
    h_x10: int = 610
    lux: int = 800
    next_fault_at: int = 0
    def __post_init__(self):
        # 与固件 zone_init() 同逻辑：注入节奏错开
        self.next_fault_at = 20 + ZONES.index(self.zid) * 3 + random.randint(0, 4)

class Emitter:
    def __init__(self, out, gt=None):
        self.out, self.gt = out, gt
    def emit(self, vtick: float, level: str, dom: str, msg: str, meta: dict | None = None):
        line = (f"{int(vtick):06d}.{int((vtick % 1) * 1e6):06d} {level} "
                f"{DOMAIN[dom]} {TAG[dom]}: {msg}")
        # ---- 双重自检：格式 + 行长 ----
        if not RE_SELF.match(line):
            raise AssertionError(f"输出违反日志规范: {line!r}")
        if len(line.encode("utf-8")) + 2 > MAX_LINE:
            raise AssertionError(f"行长超限({len(line)+2}>{MAX_LINE})，真机会静默丢弃: {line!r}")
        self.out.write(line + "\r\n")
        if self.gt is not None and meta:
            self.gt.write(json.dumps({"ts": round(vtick, 3), **meta}, ensure_ascii=False) + "\n")

# 故障场景表：与固件 fault_inject.c 一一对应（建议抽到 config/scenarios.yaml 共用）
SCENARIOS = {
    0: [("E", "sensor", "z={z} s={s} fault=F1002 c=1 r=3 cost=5001", "F1002", "onewire_bus_failure")],
    1: [("E", "task",   "z={z} s={s} fault=F3003 c=11 cost=6000", "F3003", "task_starvation")],
    2: [("W", "health", "z={z} s={s} fault=F3006 c=15 heap=3072", "F3006", "heap_exhaustion"),
        ("E", "health", "z={z} s={s} fault=F3001 c=12 heap=1024 req=8192", "F3001", "heap_exhaustion"),
        ("E", "report", "z={z} s={s} fault=F2002 c=7 cost=5010", "F2002", "heap_exhaustion")],
    3: [("E", "health", "z={z} s={s} fault=F3005 c=14 miss=2", "F3005", "watchdog")],
    4: [("W", "ctrl",   "z={z} s={s} fault=F4001 c=11 cost=5000 r=1", "F4001", "actuator_timeout")],
    5: [("E", "comm",   "z={z} s={s} fault=F2001 c=7 err=111", "F2001", "network")],
}
```

> **完整实现请按 §3.1 逐项补齐**。**由 A 同学负责实现并维护**——因为这是全项目的"数据源"，且 A 负责设备端，最清楚真机日志长什么样。基础版本 `tools/openharmony_log_sim.py` **已经写好并跑通**（含格式自检、行长检查、故障注入、信道噪声、ground-truth 标注），A 只需在此基础上增强。

---

## 4. 统一验收（W4 里程碑）

| 检查项 | 方法 | 通过标准 |
|---|---|---|
| **格式一致性** | 真机日志与模拟器日志混在一起，跑同一份解析器 | 解析成功率均 ≥ 99% |
| **行长合规** | 统计所有日志行的字节长度 | `max <= 128`，无一行超限 |
| **序号连续性** | 按 zone 分组检查 `s` 是否连续 | 无注入丢包时 100% 连续 |
| **故障可复现** | 固定 seed 跑模拟器两次 | 两次故障序列完全一致 |
| **真机稳定性** | 连续运行 2 小时 | 无死机、无重启、日志不断流 |
| **时间轴正确** | 检查 `ts_device` → `ts_host` 换算误差 | 误差 < 100ms，重启后能正确检出 |
| **数据真实性** | 遮住板载 AP3216C / 对着 SHT20 哈气 | 日志数值随之变化（**截图留证，写进报告**） |
| **栈/堆余量** | health 任务输出的 `heap=` 与栈水位 | 连续 2 小时无下降趋势（证明无内存泄漏） |

> 最后一项特别值得做：**"连续运行 2 小时堆内存无下降趋势"是证明固件没有内存泄漏的硬证据**。把 heap 值导出成折线图放进技术报告，比写十句"系统稳定"都有用。
