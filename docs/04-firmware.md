# 04 设备端固件开发（基于本仓库已就绪的 SDK）

> **重大前提**：你的工作区 `E:\Code\Harmony` 里**已经有一套完整可用的环境**，不需要从零搭建：
> - `hi3861_application/` —— HiSpark hi3861_hdu_iot_application SDK（含 `vendor/hqyj/fs_hi3861` 华清远见 FS-Hi3861 板级适配）
> - `harmony_sdk/` —— 同源的另一份 SDK 副本
> - `Tool/DevTools_Hi3861V100_v1.0/` —— RISC-V 交叉编译工具链
> - `Tool/HiBurn.exe` —— 烧录工具
> - `Tool/CH340/` —— USB 转串口驱动
> - `Tool/mosquitto-1.6.9-install-windows-x64.exe` —— MQTT Broker
> - `Tool/paho.mqtt.embedded-c-master.zip` —— 嵌入式 MQTT C 客户端库
> - `Tool/MDK5/`、`Tool/ST-Link/`、`Tool/en.stm32cubemx_v5-6-0.zip` —— STM32 开发全套
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
| **★ 与 STM32 串口通信** | `Ext_Agriculture_Test/task/uart_recv_task.c`、`smartCar_uart_udp/task/uart_recv_task.c` | **官方就是按"Hi3861 + STM32 协作"设计的** |
| **★ 智慧农业完整项目** | `smartFarm/` | 温湿度采集 + 阈值自动控制风扇 + MQTT 收发 + OLED 显示 |
| **★ 农业传感器扩展板** | `Ext_Agriculture_Test/` | 多任务架构模板（oled/key/uart 任务分离） |

> **结论：本项目的固件工作是"改造现有 demo"而不是"从零开发"。** 工作量从 3 周压缩到 3~5 天，这是这个工作区给你们的巨大红利。

### 0.1 关键发现：官方已经为"Hi3861 + STM32 协作"做好了接口

`Ext_Agriculture_Test/agriculture_demo.c` 第 67~82 行：

```c
/**
 * @brief  串口初始化
 * @note   与STM32单片机之间的串口通信
 */
void uart_init(void)
{
    hi_io_set_func(HI_IO_NAME_GPIO_5, HI_IO_FUNC_GPIO_5_UART1_RXD);
    hi_io_set_func(HI_IO_NAME_GPIO_6, HI_IO_FUNC_GPIO_6_UART1_TXD);
    hi_uart_attribute uart_param1 = {
        .baud_rate = 57600, .data_bits = 8, .stop_bits = 1, .parity = 0,
    };
    hi_uart_init(HI_UART_IDX_1, &uart_param1, NULL);
}
```

**这三行直接把方案 B（板间串口）的硬件问题解决了**：

| 事实 | 值 |
|---|---|
| 与 STM32 通信的串口 | `HI_UART_IDX_1`（UART1） |
| **Hi3861 侧引脚** | **GPIO_5 = UART1_RXD，GPIO_6 = UART1_TXD** |
| 演示代码用的波特率 | **57600**（本课题统一改为 **115200**，理由见下） |
| 发送 API | `hi_uart_write(HI_UART_IDX_1, buf, len)` |
| 接收 API | `hi_uart_read(HI_UART_IDX_1, buf, len)`（阻塞式） |

> ⚠️ **注意**：demo 里 `uart_send_buff()` 写的是 `HI_UART_IDX_2`，而 `uart_init()` 初始化的是 `HI_UART_IDX_1`——**这是官方 demo 里的一个不一致**（`uart_send_buff` 发到了未初始化的 UART2）。**我们统一用 `HI_UART_IDX_1`**，并在报告里把这个坑写出来（"我们发现了官方示例中的 UART 索引不一致问题"——这种细节会让评委印象深刻）。

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
├── sensor_hub.c / .h    # 板载传感器 + MCU 串口数据汇聚
├── fault_inject.c / .h  # 故障注入器
└── mcu_link.c / .h      # STM32 帧解析
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
        "mcu_link.c",
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
#include "mcu_link.h"

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
    sensor_hub_init();    /* 板载传感器 + UART1（对接 STM32） */

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

## 3. STM32 侧固件

### 3.1 工具链（工作区已就绪）

- `Tool/MDK5/` —— Keil MDK
- `Tool/ST-Link/` —— 下载器驱动
- `Tool/en.stm32cubemx_v5-6-0.zip` —— CubeMX（可选，HAL 库工程生成）
- `Tool/CH340/` —— USB 转串口驱动

**待确认**：STM32 的型号与温度传感器型号（见 `docs/03` §5 确认表）。下面的代码结构与具体型号无关。

### 3.2 日志宏

```c
/* mcu_log.h —— 与 docs/02 §2.5 一致：[STM32] <ms> <LEVEL> <TAG>: <msg> */
#ifndef MCU_LOG_H
#define MCU_LOG_H

#include <stdio.h>

#define MCU_LOG(level_ch, tag, fmt, ...)                              \
    printf("[STM32] %06u %c %s: " fmt "\r\n",                         \
           (unsigned)(millis() % 1000000u), (level_ch), (tag), ##__VA_ARGS__)

#define LOG_I(tag, fmt, ...) MCU_LOG('I', tag, fmt, ##__VA_ARGS__)
#define LOG_W(tag, fmt, ...) MCU_LOG('W', tag, fmt, ##__VA_ARGS__)
#define LOG_E(tag, fmt, ...) MCU_LOG('E', tag, fmt, ##__VA_ARGS__)

#endif
```

**输出样例（与规范一致，值全为数字）**：

```
[STM32] 000512 E ONEWIRE: s=103 fault=F1002 c=1 r=3
[STM32] 000513 I SENSOR: s=103 t=248 h=612 a=830
```

### 3.3 采集主循环 + 温度/光照

```c
/* main.c */
int main(void)
{
    uart1_init(115200);       /* 日志输出 → PC（若无板载串口芯片则经 USB-TTL） */
    uart2_init(115200);       /* 与 Hi3861 通信（方案 B，可选） */
    adc_init();               /* PA0 ← LM393 光敏模块 AO */
    temp_sensor_init();       /* DS18B20 / DHT11 / NTC，按实际型号 */
    led_init(); key_init();

    LOG_I("SYS", "s=0 fault=F5001 c=6 boot=1");

    u32 last_sample = 0, last_beat = 0, seq = 0;
    while (1) {
        u32 now = millis();
        if (now - last_sample >= 5000u) {          /* 与 Hi3861 同步：5 秒 */
            last_sample = now;
            sample_and_log(&seq);
        }
        if (now - last_beat >= 10000u) {
            last_beat = now;
            LOG_I("SYS", "s=%u up=%u", ++seq, now / 1000u);
        }
        if (key_pressed()) {                        /* 现场手动触发故障 */
            LOG_E("ONEWIRE", "s=%u fault=F1002 c=1 r=3", ++seq);
        }
    }
}

void sample_and_log(u32 *seq)
{
    u16 adc = read_light_raw();                     /* 8 次滑动平均 */
    int t_x10 = temp_read_x10();                    /* 温度 ×10，整型！ */
    int h_x10 = 612;                                /* 无湿度传感器时用板载值或固定值 */

    LOG_I("SENSOR", "s=%u t=%d h=%d a=%u", ++(*seq), t_x10, h_x10, adc);
    uart2_send_frame("A", *seq, t_x10, h_x10, adc, 0);   /* 方案 B：发给 Hi3861 */

    /* 真实异常：遮住光敏电阻 → WARN */
    if (adc < LIGHT_DARK_THR) {
        LOG_W("LIGHT", "s=%u fault=F1004 c=3 f=3 tx10=%d", *seq, (int)(adc * 10 / 41));
    }
}
```

> **`t_x10` 用整型而不是浮点**：① 与日志规范一致（`t=248` 表示 24.8℃）；② STM32 上避免浮点格式化（`printf("%.1f")` 在部分工程配置下需要额外的库支持，容易踩坑）。**读传感器时内部用浮点没关系，输出日志前乘 10 转整型即可。**

### 3.4 三种温度传感器的驱动要点

| 型号 | 关键点 | 经典坑 |
|---|---|---|
| **DS18B20** | 复位 → `0xCC` 跳过 ROM → `0x44` 启动转换 → 延时 750ms → `0xBE` 读 9 字节 → CRC 校验 | ① 缺 **4.7kΩ 上拉** → 恒读 `85.0`（上电默认值）；② 读时序被中断打断 → 必须 `__disable_irq()` 保护；③ 转换延时不足 → 读到上一次的值 |
| **DHT11** | 主机拉低 ≥18ms → 释放 → 等 80µs 低 + 80µs 高 → 读 40 bit | ① 采样间隔 < 1s 返回旧值；② 上电需 1s 稳定；③ **读失败率高是正常现象**——正好作为 `F1001` 的真实触发源 |
| **NTC** | ADC 采样 → 分压算阻值 → Steinhart-Hart 或查表 | 需标定；建议查表法（快且可预测） |

**首版验收**：室温读数偏差 ≤ 2℃；**用手捂住 30 秒读数上升 ≥ 1℃**（这是"传感器真在工作"的最强证据，答辩现场就做这个动作）。

### 3.5 光敏模块（LM393）接线与标定

```
模块 VCC → STM32 3.3V      ⚠️ 绝不能接 5V！AO 满量程等于 VCC，5V 会打坏 ADC 引脚
模块 GND → STM32 GND
模块 AO  → STM32 PA0 (ADC1_IN0)
模块 DO  → 可不接（或接 PB0 做数字量越限演示）
```

**标定步骤**（5 分钟，结果填进 `docs/03` §5）：
1. 用万用表量 AO 对 GND 电压：暗环境 ≈ 0.3~1.0V，手机手电筒照射 ≈ 2.0~3.2V；
2. 记录 ADC 原始值（12 位，0~4095）：`LIGHT_DARK_THR` 取"正常室内光照值"的 1/3 左右；
3. 旋转蓝色电位器，观察模块上 DO 指示灯翻转，记录对应位置。

> **本课题最好的现场演示素材**：评委看着你用手遮住光敏电阻，3 秒后告警出现在屏幕上。**不需要任何软件注入，完全真实的物理故障。**

---

## 4. 与 Hi3861 的串口协议（方案 B）

### 4.1 帧格式

```
$AGRI,<zone>,<seq>,<t_x10>,<h_x10>,<adc>,<flags>*<CS>\r\n

示例：$AGRI,A,103,248,612,830,0*3F\r\n
      │     │   │    │    │    │   └─ flags：bit0=传感器故障 bit1=灌溉中
      │     │   │    │    │    └───── 光照 ADC 原始值
      │     │   │    │    └────────── 湿度 ×10（612 = 61.2%）
      │     │   │    └─────────────── 温度 ×10（248 = 24.8℃）
      │     │   └──────────────────── 序号
      │     └──────────────────────── zone（A~F）
      └────────────────────────────── 固定帧头
CS = 从 '$' 后到 '*' 前所有字节的异或值（2 位大写十六进制）
```

**为什么用逗号分隔 + 异或校验，而不是 JSON**：
- STM32 上无浮点格式化负担、帧长固定（约 30 字节）、解析开销极小；
- **校验失败可以产生 `F2005 帧校验错` 日志**——这本身就是一个可注入、可观测的真实异常。

### 4.2 Hi3861 侧的解析与转发

**先说清楚：官方 demo 的收包写法有缺陷，不要照抄。**

`smartCar_uart_udp/task/uart_recv_task.c` 的收包逻辑（第 83~103 行）用 `len < last_len` 判断"一帧收完"：

```c
hi_u32 len = hi_uart_read(HI_UART_IDX_2, uart_buff, sizeof(uart_buff));
if (len > 0) {
    memcpy_s(pbuff, len, uart_buff, len); pbuff += len;
    if (len < last_len) {              /* ← 靠"本次读到的字节比上次少"猜测帧结束 */
        pbuff = recvBuff; parse_json_data(recvBuff); ...
    }
    last_len = len;
}
```

**这个启发式方法的问题**：帧长固定时永远不满足 `len < last_len`，会一直收不到；帧长波动时可能提前/滞后判定。**正确做法是按分隔符 `\n` 切分**：

```c
/* mcu_link.c —— 按 \n 切分，稳定可靠 */
#define MCU_BUF_SIZE 256
static char  s_buf[MCU_BUF_SIZE];
static int   s_len = 0;
static unsigned s_mcu_seq_expect = 0;

void mcu_link_task(void)
{
    uint8_t chunk[64];
    while (1) {
        /* 阻塞读；超时时间由 uart 属性决定 */
        hi_u32 n = hi_uart_read(HI_UART_IDX_1, chunk, sizeof(chunk));
        for (hi_u32 i = 0; i < n; i++) {
            char c = (char)chunk[i];
            if (c == '\n') {
                s_buf[s_len] = '\0';
                if (s_len > 0) { on_mcu_frame(s_buf); }
                s_len = 0;                       /* 复位，准备下一帧 */
            } else if (c != '\r') {
                if (s_len < MCU_BUF_SIZE - 1) {
                    s_buf[s_len++] = c;
                } else {
                    /* 超长：丢弃并记日志（这就是 F2005 的真实来源之一） */
                    LOG_E(DOM_SENSOR, TAG_SENSOR, "z=? s=0 fault=F2005 c=2 len=%d", s_len);
                    s_len = 0;
                }
            }
        }
    }
}

static void on_mcu_frame(const char *line)
{
    /* 1) 校验帧头与校验和 */
    uint8_t zone; unsigned seq; int t_x10, h_x10; unsigned adc, flags;
    if (!parse_agri_frame(line, &zone, &seq, &t_x10, &h_x10, &adc, &flags)) {
        LOG_E(DOM_SENSOR, TAG_SENSOR, "z=? s=0 fault=F2005 c=2");   /* 帧校验错 */
        return;
    }

    /* 2) 序号连续性检查（MCU 侧丢帧检测） */
    if (s_mcu_seq_expect && seq != s_mcu_seq_expect) {
        LOG_W(DOM_SENSOR, TAG_SENSOR, "z=%c s=%u gap=%u",
              zone, seq, seq - s_mcu_seq_expect);
    }
    s_mcu_seq_expect = seq + 1;

    /* 3) 更新该 zone 的状态 */
    ZoneState *z = zone_by_id(zone);
    if (z) { z->temp_x10 = t_x10; z->humi_x10 = h_x10; z->lux = adc; }

    /* 4) ★ 用我们的日志宏转发 —— 这一步是整个方案的技术亮点 */
    LOG_I(DOM_SENSOR, TAG_SENSOR,
          "z=%c s=%u t=%d h=%d l=%u src=1 mseq=%u", zone, seq, t_x10, h_x10, adc, seq);
}
```

> **这一行的意义**（答辩必讲）：它把"STM32 采集的真实物理数据"和"OpenHarmony 规范的 hilog 格式日志"**合二为一**。可以说："我们实现了从传感器到 OpenHarmony 日志系统的完整数据通路，而不是两块板子各跑各的。"

### 4.3 串口参数统一

| 参数 | 官方 demo | 本课题 | 理由 |
|---|---|---|---|
| 波特率 | 57600 | **115200** | 与日志输出口一致，减少配置项；115200 对 5 秒一帧的数据量绰绰有余 |
| 数据位/停止位/校验 | 8/1/无 | 8/1/无 | 沿用 |
| Hi3861 引脚 | GPIO_5(RXD) / GPIO_6(TXD) | 同 | 沿用官方定义 |
| 帧结束 | 靠 `len < last_len` 猜 | **`\n` 分隔** | 见 §4.2 的分析 |

> ⚠️ **接线前必须确认电平**：Hi3861 是 3.3V 器件。STM32 若在 5V 供电下 TX 输出 5V，**会打坏 Hi3861 的 RX**。
> **安全做法**：STM32 用 3.3V 供电；首版**只接单向线**（STM32_TX → Hi3861_RX）+ **共地**，不接反向线。

---

## 5. 路线 3：日志模拟器（**B、C 同学立即开工**）

模拟器逐字节复刻真机日志格式，让整个项目**不依赖硬件进度**。

### 5.1 关键设计

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

### 5.2 用法

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

### 5.3 实现骨架（注意自检与行长检查）

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

> **完整实现请按 §5.1 逐项补齐**。**由 A 同学负责实现并维护**——因为这是全项目的"数据源"，且 A 负责设备端，最清楚真机日志长什么样。基础版本 `tools/openharmony_log_sim.py` **已经写好并跑通**（含格式自检、行长检查、故障注入、信道噪声、ground-truth 标注），A 只需在此基础上增强。

---

## 6. 统一验收（W4 里程碑）

| 检查项 | 方法 | 通过标准 |
|---|---|---|
| **格式一致性** | 真机日志与模拟器日志混在一起，跑同一份解析器 | 解析成功率均 ≥ 99% |
| **行长合规** | 统计所有日志行的字节长度 | `max <= 128`，无一行超限 |
| **序号连续性** | 按 zone 分组检查 `s` 是否连续 | 无注入丢包时 100% 连续 |
| **故障可复现** | 固定 seed 跑模拟器两次 | 两次故障序列完全一致 |
| **真机稳定性** | 连续运行 2 小时 | 无死机、无重启、日志不断流 |
| **时间轴正确** | 检查 `ts_device` → `ts_host` 换算误差 | 误差 < 100ms，重启后能正确检出 |
| **数据真实性** | 手捂传感器 / 遮光敏电阻 | 日志数值随之变化（**截图留证，写进报告**） |
| **栈/堆余量** | health 任务输出的 `heap=` 与栈水位 | 连续 2 小时无下降趋势（证明无内存泄漏） |

> 最后一项特别值得做：**"连续运行 2 小时堆内存无下降趋势"是证明固件没有内存泄漏的硬证据**。把 heap 值导出成折线图放进技术报告，比写十句"系统稳定"都有用。
