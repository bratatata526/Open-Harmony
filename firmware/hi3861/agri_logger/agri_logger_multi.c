/*
 * Copyright (c) 2026 OpenHarmony 日志智能聚合分析工具项目组
 *
 * 【规范日志 · 阶段 2】6 个逻辑大棚 + 真实传感器 + 故障注入器（正式版）
 *
 * ============================================================================
 * 设计要点（答辩会被问，先写在这里）
 * ============================================================================
 *
 * 【1】为什么用"单任务循环 6 个 zone"而不是"6 个任务各管一个 zone"？
 *      两个理由：
 *        · RAM：每个任务至少 1KB 栈，6 个任务 + 传感器 + 健康检查 ≈ 8KB+，
 *          而 Hi3861 可用 RAM 有限，不划算。
 *        · 数据一致性：6 个大棚共享同一个园区基准（同一片天空）。
 *          单任务里先读一次传感器、再依次更新 6 个棚，能保证同一轮内
 *          **基准完全相同**，物理上更严谨。多任务里每轮基准会不同，反而不真实。
 *      → 这与 simulator（farm_model.py）的模型完全一致，保证数据同构。
 *
 * 【2】6 个大棚的数据从哪来？（只有一个物理传感器）
 *      园区基准  = 板载 SHT20 的真实读数（物理锚点）
 *      个体差异  = 固定的物理偏移（朝向/保温/土壤/遮挡）
 *      缓变惯性  = 一阶滞后跟随基准（大棚越大变化越慢）
 *      传感器噪声 = ±0.1℃ / ±0.2%（测量不确定度）
 *      完整说明见 agri_logger/parser/farm_model.py 的模块注释。
 *
 * 【3】内存健康用的是【真实】SDK 接口，不是编的数
 *      · free_lmp           —— 小内存池剩余空间（真实可用堆）
 *      · malloc_fail_count  —— 累计内存申请失败次数
 *      · osThreadGetStackSpace() —— 任务栈剩余水位
 *      见 hi_mem.h 的 hi_mdm_mem_info 与 cmsis_os2.h。
 *
 * 【4】故障注入的判据必须"像真的"
 *      每类故障都带真实的数值证据，让下游能用上【数值判据】而不只是关键词：
 *        传感器超时 → cost=5001ms（阈值 1000ms）、retry=3
 *        任务卡死   → cost=6000ms（阈值 5000ms）
 *        看门狗     → miss=2
 *      唯一的例外见 inject_fault() 里对 heap 值的注释说明。
 *
 * 【5】题目要求的"每 20~30 次上报触发一次故障"演示时太慢
 *      6 个 zone × 5 秒 = 每 30 秒一轮，每 20~30 轮才注入一次
 *      = 10~15 分钟才见一次故障，现场演示等不起。
 *      → 用 FAULT_INJECT_INTERVAL 可调：演示设 2~3，正式跑设 20~30，负样本设 0。
 * ============================================================================
 */

#include <stdio.h>
#include <unistd.h>
#include <string.h>

#include "ohos_init.h"
#include "cmsis_os2.h"
#include "hi_io.h"
#include "hi_gpio.h"
#include "hi_time.h"
#include "hi_mem.h"
#include "hal_bsp_sht20.h"
#include "hal_bsp_ap3216c.h"
#include "hal_bsp_ssd1306.h"
#include "hal_bsp_pcf8574.h"
#include "hal_bsp_key.h"        /* 板载用户按键：KEY_Init / KEY_Get_Input_Value */
#include "iot_errno.h"          /* IOT_SUCCESS（=0），AP3216C_ReadData 的返回值 */

#include "log_def.h"

/* ==========================================================================
 * 配置
 * ======================================================================== */

/* ★★★ 运行模式 ★★★
 *
 * 1 = 【单设备模式】—— 只上报板子真实传感器的读数
 *     ZONE_COUNT = 1，profile 标记 is_real：不加偏置、不加惯性、不加人工噪声。
 *     日志里的数值就是 SHT20 / AP3216C 的原始读数，一个字节的加工都没有。
 *     适用：要向别人证明"数据是真的"、或者只想看一个真实设备的时候。
 *
 * 0 = 【六棚模式】—— 一个真实基准 → 6 个逻辑设备
 *     6 个大棚共享同一片天空，各自的朝向/保温/土壤差异体现为固定偏置 + 热惯性。
 *     适用：题目要求"设备数量 6 个"的场景。物理依据见 docs/03 §1.4.2。
 *
 * ⚠️ 切换模式要改这里并【重新编译烧录】。改完记得把开机横幅的 FW 号 +1。 */
#define SINGLE_DEVICE_MODE  1

#if SINGLE_DEVICE_MODE
#define ZONE_COUNT          1
#else
#define ZONE_COUNT          6
#endif

/* 上报周期：题目要求"每 5 秒上报一次"。
 * 整个循环 5 秒走完，6 个 zone 在同一轮内依次输出。 */
#define CYCLE_PERIOD_MS     5000

/* 任务栈 —— 实测踩坑记录：
 * 初版照抄官方 demo 的 1024 字节，结果【跑一轮就静默复位】。
 * 光日志缓冲区（msg[120] + b1..b4[40]*4 = 280 字节）就超出 1KB 栈，
 * 而 LiteOS-M 上栈溢出不报错、直接复位，极难排查。
 * 本任务要管 6 个 zone 的状态，给 8KB 留足余量。 */
#define TASK_STACK_SIZE     (1024 * 8)

/* 阈值 —— 与 simulator / farm_model.py 保持一致 */
#define TEMP_HIGH_X10       350      /* 35.0℃ → 通风          */
#define TEMP_INVALID_X10    850      /* 85.0℃ 传感器失效典型值 */
#define HUMI_LOW_X10        400      /* 40.0% → 开灌溉        */
#define HUMI_TARGET_X10     550      /* 55.0% → 停灌溉        */
#define HEAP_LOW_THR        4096     /* 堆内存低水位（字节）   */

/* 故障注入间隔（单位：轮 = 一次上报）。
 *
 * 题目原文："正常情况下输出 INFO 级数据，【每 20~30 次上报后】，随机触发一次
 *            '传感器超时'的 ERROR 日志" —— 所以正式跑用 20~30。
 *
 * 取值建议：
 *   20~30  正式数据集 / 验收。符合题目要求，日志以 INFO 为主、故障是稀疏事件，
 *          这样"异常识别"才有意义（满地都是 ERROR 就不叫异常了）。
 *   2~3    现场演示 / 调试分析链路。故障密集，几分钟就能攒够异常窗口，
 *          但【不要用它生成评测数据集】—— 违反题目要求，异常比例也失真。
 *   0      纯正常样本（负样本集），用来测误报率。
 *
 * ⚠️ 改这个值不影响场景轮换 —— 轮换用独立计数器 g_scenario_turn，
 *    不会像 g_cycle % 6 那样被间隔整除而只轮到少数场景。 */
#define FAULT_INJECT_INTERVAL   20

/* 健康检查间隔（单位：轮） */
#define HEALTH_CHECK_EVERY      2

/* ==========================================================================
 * 大棚物理属性（与 farm_model.py 的 ZONE_PROFILES 严格对应）
 *
 * ★★★ 改动这里必须同步改 agri_logger/parser/farm_model.py ★★★
 * ======================================================================== */
typedef struct {
    char zid;
    int  temp_bias_x10;      /* 温度基准偏移 ×10℃（朝向/保温）  */
    int  humi_bias_x10;      /* 湿度基准偏移 ×10%（土壤/通风）  */
    int  light_bias;         /* 光照基准偏移（棚体遮挡）        */
    int  inertia_pct;        /* 热惯性 0~100：越大跟随基准越慢   */
    int  is_real;            /* 1 = 真实设备：原始读数直通，不做任何加工 */
} zone_profile_t;

#if SINGLE_DEVICE_MODE
static const zone_profile_t ZONE_PROFILES[ZONE_COUNT] = {
    /* 单设备模式：这一台就是开发板本身。
     * 偏置全 0、惯性 0、is_real=1 → 日志里的温湿度就是 SHT20 的读数，
     * 光照就是 AP3216C 的读数，不加任何人工成分。 */
    { 'A',  0,   0,     0,    0,   1 },
};
#else
static const zone_profile_t ZONE_PROFILES[ZONE_COUNT] = {
    /* zid  temp  humi  light  inertia  is_real */
    { 'A',  +12,  -15,   +40,   35,  0 },   /* 1 号棚·向阳主棚：采光好、升温快、土壤偏干 */
    { 'B',  -15,  +25,  -110,   45,  0 },   /* 2 号棚·背阴棚：日照少、温度低、湿度高     */
    { 'C',   +6,  +10,   -60,   55,  0 },   /* 3 号棚·旧棚膜：透光差、保温差             */
    { 'D',   -6,   -5,   +25,   30,  0 },   /* 4 号棚·新棚膜：透光好、升温快、散热也快   */
    { 'E',   -4,  +30,     0,   50,  0 },   /* 5 号棚·近水渠：蒸发大、湿度显著偏高       */
    { 'F',   +4,  -20,   +10,   60,  0 },   /* 6 号棚·大棚：热惯性最大、变化最平缓       */
};
#endif

/* 故障场景 —— 每个 zone 主攻一类，保证日志多样性 */
typedef enum {
    SCEN_SENSOR = 0, SCEN_TASK, SCEN_MEMORY, SCEN_WATCHDOG, SCEN_ACTUATOR, SCEN_NETWORK
} scenario_t;

/* ==========================================================================
 * 运行时状态
 * ======================================================================== */
typedef struct {
    const zone_profile_t *prof;
    unsigned seq;              /* 单调递增序号，丢包检测依据 */
    int      temp_x10;
    int      humi_x10;
    int      light;
    int      initialised;
    int      irrigating;
    int      venting;
    int      fail_streak;
    scenario_t scenario;
} zone_state_t;

static zone_state_t  g_zone[ZONE_COUNT];
static unsigned      g_cycle = 0;
static int           g_inject_countdown = FAULT_INJECT_INTERVAL;
static int           g_injecting_zone = -1;
static osThreadId_t  g_task_id = NULL;

/* 内存健康（用 SDK 真实接口获取） */
static unsigned      g_heap_free_last = 0;
static int           g_light_last = 0;      /* 最近一次真实光照读数（AP3216C ALS） */
static unsigned      g_scenario_turn = 0;   /* 单设备模式下的故障场景轮换计数（每次注入 +1） */
static unsigned      g_malloc_fail_last = 0;
static unsigned      g_stack_space_last = 0;

/* ==========================================================================
 * 演示开关：板载 KEY 按键切换"故障注入"的开启/关闭
 *
 * 为什么要做这个：
 *   演示脚本是"先展示一切正常，再展示遇到故障会怎么样"。
 *   如果设备端没有开关，就只能靠重新烧录来切换 —— 现场根本来不及。
 *
 * ★ 这个开关的语义是【设备端真的不发】注入日志，不是让前端过滤掉。
 *   区别很大：
 *     · 设备端关闭 → 日志流里【根本不存在】注入的故障，"纯真实数据"是物理事实
 *     · 前端过滤   → 设备照发，只是界面不显示；被问"设备到底报没报"就露馅
 *   所以我们做的是前者。
 *
 * 按下后 OLED 第 1 行会立刻变成 FAULT:ON / FAULT:OFF，
 * 并发一条 AgriMode 日志，看板也能读到当前模式。
 *
 * 按键用 BSP 官方的 hal_bsp_key（GPIO_14，上拉输入，低电平=按下）。
 * ⚠️ 用了它就必须把 hal_bsp_key.c 加进 BUILD.gn 的 sources，
 *    否则链接期报 undefined reference（AP3216C / SSD1306 都栽过这个坑）。
 * ======================================================================== */
#define KEY_POLL_MS         100                  /* 主循环里每 100ms 查一次按键 */

static volatile int  g_inject_on = 0;       /* 0=纯真实数据（默认） 1=开启注入 */
static int           g_key_stable = 1;      /* 去抖后的按键状态：1=松开 0=按下 */
static int           g_key_same = 0;        /* 连续读到同一状态的次数 */
static volatile int  g_mode_dirty = 1;      /* 模式变了要刷 OLED / 发日志 */

/* ==========================================================================
 * 工具：字段缓冲
 *
 * ★★★ 为什么不用 snprintf ★★★
 * Hi3861 SDK 的 printf/snprintf 在【多位数整数】格式化上有缺陷：
 * 真机上 1001 会被打印成 "1"、5001 成 "5"（同一份代码在主机 gcc 上完全正确）。
 * 所以日志路径上完全避开 stdio，改用自己写的十进制转换。
 * 详见 log_def.h 的 agri_expand_field() 注释 与 docs/09 坑 9。
 *
 * 用法：F(buf, AGRI_F_TEMP, z->temp_x10)
 *       → buf 内容为 "2:275"
 * ======================================================================== */
static char *agri_fmt_pair(char *buf, int id, int val)
{
    char *p = buf;

    /* 字段 ID（最多 2 位） */
    p = agri_i2a(id, p);
    *p++ = ':';
    /* 值 */
    p = agri_i2a(val, p);
    return buf;
}

#define F(buf, id, val)   agri_fmt_pair((buf), (id), (int)(val))

/* 取一个每次调用都不同的伪随机小量（用于传感器噪声） */
static int noise(int mod)
{
    return (int)(osKernelGetTickCount() % (unsigned)mod);
}

/* ==========================================================================
 * 内存健康：用 SDK 真实接口
 * ======================================================================== */
static void mem_health_update(void)
{
    hi_mdm_mem_info info;

    memset(&info, 0, sizeof(info));
    if (hi_mem_get_sys_info(&info) == 0) {
        g_heap_free_last = (unsigned)info.free_lmp;
        g_malloc_fail_last = (unsigned)info.malloc_fail_count;
    } else {
        g_heap_free_last = 0;          /* 0 表示"读不到"，日志里会体现出来 */
    }

    if (g_task_id != NULL) {
        g_stack_space_last = (unsigned)osThreadGetStackSpace(g_task_id);
    }
}

/* ==========================================================================
 * 多大棚环境模型 —— 一阶滞后 + 个体偏移 + 噪声
 * （与 farm_model.py 的 ZoneEnvironment.update() 同构）
 * ======================================================================== */
static void zone_env_update(zone_state_t *z, int base_t, int base_h, int base_l)
{
    const zone_profile_t *p = z->prof;
    int target_t = base_t + p->temp_bias_x10;
    int target_h = base_h + p->humi_bias_x10;
    int target_l = base_l + p->light_bias;
    int alpha;                          /* 跟随比例 0~100 */

    /* ★ 真实设备：传感器读数【直通】，不加偏置、不加惯性、不加人工噪声。
     *   这一条是"数据是真的"的代码级保证 —— 日志里的数字就是芯片读出来的数字。
     *   单设备模式下走这里，任何人工成分都进不来。 */
    if (p->is_real) {
        z->temp_x10  = base_t;
        z->humi_x10  = base_h;
        z->light     = base_l;
        z->initialised = 1;
        /* 灌溉期间湿度自然上升也要保留：那是"浇水"这个物理动作的结果，
         * 不是编造的数据。 */
        if (z->irrigating) {
            z->humi_x10 += 32;
        }
        if (z->humi_x10 > 900) { z->humi_x10 = 900; }
        return;
    }

    if (target_l < 0) {
        target_l = 0;
    }

    if (!z->initialised) {
        /* 首次直接对齐，避免开机时从 25.0 缓慢爬升的假象 */
        z->temp_x10 = target_t;
        z->humi_x10 = target_h;
        z->light = target_l;
        z->initialised = 1;
    }

    alpha = 100 - p->inertia_pct;       /* 惯性越大，跟随越慢 */

    z->temp_x10 += (target_t - z->temp_x10) * alpha / 100;
    z->light += (target_l - z->light) * (alpha * 6 / 10) / 100;

    /* 湿度：灌溉期间上升，之后按惯性回落 */
    if (z->irrigating) {
        z->humi_x10 += 32;              /* 每轮 +3.2% */
    } else {
        z->humi_x10 += (target_h - z->humi_x10) * (alpha * 88 / 100) / 100;
    }

    /* 传感器噪声：±0.1℃ / ±0.2% / ±5（很小，体现测量不确定度） */
    z->temp_x10 += noise(3) - 1;
    z->humi_x10 += noise(5) - 2;
    z->light += noise(11) - 5;

    /* 物理合理性钳制 */
    if (z->humi_x10 < 150)  { z->humi_x10 = 150; }
    if (z->humi_x10 > 900)  { z->humi_x10 = 900; }   /* 上限 90%，不模拟饱和 */
    if (z->temp_x10 < -200) { z->temp_x10 = -200; }
    if (z->temp_x10 > 600)  { z->temp_x10 = 600; }
    if (z->light < 0)       { z->light = 0; }
    if (z->light > 1200)    { z->light = 1200; }
}

/* ==========================================================================
 * OLED 实时显示（板载 0.96" SSD1306，128x64，I2C 0x78）
 *
 * 为什么值得做：板子【脱机】运行时，屏幕是唯一能当场看到真实读数的地方。
 * 演示时不用连电脑、不用开看板，一眼就能看到温湿度光照在跳。
 *
 * ★ 两个坑必须避开（都来自本项目已经踩过的教训）★
 *
 *   1) 绝不能用平台 printf/snprintf 拼字符串 —— 它对多位数整数不可靠（坑 9）。
 *      日志里 1001 会变成 "1"；屏幕上如果打出 "T: 3 C" 而不是 "T: 30.7 C"，
 *      同样是致命的，而且更丢人（评委直接看着屏幕）。
 *      所以这里复用日志路径上那套自写的十进制转换 agri_u2a()。
 *
 *   2) 不要每轮 SSD1306_CLS() 清屏再重画 —— 5 秒一次的清屏会看到明显闪烁。
 *      改成【定宽补空格】：每行固定 16 字符（8x16 字体，每字符 8 像素，
 *      128/8 = 16），内容变短时用空格补齐把旧字符覆盖掉，
 *      这样正常运行期间【永远不需要清屏】。
 * ======================================================================== */

#define OLED_LINE_CHARS 16      /* 8x16 字体一行正好 16 个字符 */
#define OLED_LINES      4       /* 128x64 屏，16 号字体只能放 4 行 */

static char   g_oled_line[OLED_LINE_CHARS + 1];
static int    g_oled_ok = 0;    /* SSD1306 初始化是否成功；失败就不去刷它 */

/**
 * @brief 拼一行并显示。val 按 x10 定点处理（x10=1 表示 val 是 0.1 单位）
 *
 * 定宽补空格的写法是为了【不清屏也不留残影】：
 * 上一轮 "T: 30.7 C" 有 9 个字符，这一轮 "T: 9.8 C" 只有 8 个，
 * 如果不补空格，末尾那个 "C" 的残影会一直留在屏幕上。
 */
static void oled_draw_line(int line, const char *label, int val, int x10,
                           const char *unit)
{
    char       *p = g_oled_line;
    const char *s;

    if (!g_oled_ok) {
        return;
    }
    if (line < 0 || line >= OLED_LINES) {
        return;
    }

    for (s = label; *s != '\0'; s++) {
        *p++ = *s;
    }
    if (val < 0) {
        *p++ = '-';
        val = -val;
    }
    if (x10) {
        p = agri_u2a((unsigned int)(val / 10), p);
        *p++ = '.';
        *p++ = (char)('0' + (val % 10));
    } else {
        p = agri_u2a((unsigned int)val, p);
    }
    for (s = unit; *s != '\0'; s++) {
        *p++ = *s;
    }
    /* 补齐到定宽，覆盖掉上一轮的残留字符 */
    while (p < g_oled_line + OLED_LINE_CHARS) {
        *p++ = ' ';
    }
    *p = '\0';

    SSD1306_ShowStr(0, (unsigned char)line,
                    (unsigned char *)g_oled_line, TEXT_SIZE_16);
}

/** 把当前真实读数刷到屏幕上。temp_x10/humi_x10 是 ×10 定点，light 是原始值。 */
static void oled_update(int temp_x10, int humi_x10, int light, unsigned cycle)
{
    unsigned c = cycle;

    if (!g_oled_ok) {
        return;
    }
    if (c > 9999u) {
        c = 9999u;                      /* 防止轮次号撑破行宽 */
    }
    /* 第 1 行同时显示轮次和【当前是否在注入故障】——
     * 演示时最怕的就是"到底开没开注入"说不清，屏幕上直接写着。 */
    oled_draw_line(0, "#", (int)c, 0, g_inject_on ? " FAULT:ON" : " FAULT:OFF");
    oled_draw_line(1, "T: ", temp_x10, 1, " C");      /* 温度：30.7 C */
    oled_draw_line(2, "H: ", humi_x10, 1, " %");      /* 湿度：47.1 % */
    oled_draw_line(3, "L: ", light,    0, "");        /* 光照：真实 ALS 原始值 */
}

/* ==========================================================================
 * 板载 KEY：切换故障注入开关
 *
 * 用【轮询 + 去抖】而不是中断，原因：
 *   · 主循环本来就是 5 秒一轮，把等待切成 100ms 小段顺手就查了按键，
 *     响应延迟 ≤100ms，演示时按下立刻有反应
 *   · 中断回调在中断上下文里改状态容易出问题（本项目已经栽过够多坑了）
 * ======================================================================== */
static void key_setup(void)
{
    KEY_Init();          /* GPIO_14 上拉输入，官方 BSP 驱动 */
}

/** 每 100ms 调一次。检测到"按下"就把注入开关翻一次。 */
static void key_poll(void)
{
    int now = (KEY_Get_Input_Value() == 0) ? 0 : 1;   /* 低电平 = 按下 */

    if (now == g_key_stable) {
        g_key_same = 0;
        return;
    }
    if (++g_key_same < 2) {                     /* 连续 2 次(200ms)一致才认账 */
        return;
    }
    g_key_same = 0;
    g_key_stable = now;

    if (now == 0) {                             /* 按下瞬间 */
        g_inject_on = g_inject_on ? 0 : 1;
        /* 打开时把倒计时清零，下一次循环立刻注一次故障 ——
         * 否则按下后要等 20 轮(约 100 秒)才看到第一条，演示时以为没生效。 */
        g_inject_countdown = 1;
        g_mode_dirty = 1;                       /* 通知主循环刷屏 + 发日志 */
    }
}

/**
 * @brief OLED 初始化。失败不影响主流程，只是不刷屏。
 * @note 官方 demo 的顺序是 PCF8574_Init() 之后再 SSD1306_Init()，照抄。
 */
static void oled_setup(void)
{
    g_oled_ok = (SSD1306_Init() == 0) ? 1 : 0;
    if (!g_oled_ok) {
        return;
    }
    SSD1306_CLS();                       /* 只在开机清一次屏 */

    /* 标题行是静态的，写一次就够，后面每轮只更新数值行 */
    oled_draw_line(0, "AGRI-OPS boot", 0, 0, "");
    oled_draw_line(1, "T: --.- C", 0, 0, "");
    oled_draw_line(2, "H: --.- %", 0, 0, "");
    oled_draw_line(3, "L: ---", 0, 0, "");
}

/* ==========================================================================
 * 故障注入器 —— 每类都带真实的数值证据
 *
 * ★ 关于 heap 值的诚实说明（答辩可能被追问）★
 *   注入了 F3001「内存分配失败」这类**逻辑故障**时，heap 字段填的是
 *   "该故障发生时应该处于的水位"（1024），而不是当时设备的实际剩余堆。
 *   原因：真实的 OOM 需要先制造真正的内存泄漏，那会让设备不稳定，
 *   无法连续演示 2 小时。
 *   但 F3006「空闲堆不足」走的是【真实路径】——见 do_health_check()，
 *   它读取 hi_mem_get_sys_info().free_lmp，低于阈值才报。
 *   即：**"堆水位"是真数据，"分配失败"是按判据注入的逻辑故障。**
 * ======================================================================== */
static void inject_fault(zone_state_t *z, char *b1, char *b2, char *b3, char *b4)
{
    unsigned s = ++z->seq;

    switch (z->scenario) {
    /* ---- 传感器读取失败：超时 → 无效值 → 降级运行 ---- */
    case SCEN_SENSOR:
        AGRI_E(DOM_SENSOR, "AgriSensor", z->prof->zid,
               F(b1, AGRI_F_SEQ, s),
               F(b2, AGRI_F_FAULT, AGRI_FAULT_SENSOR_TIMEOUT),
               F(b3, AGRI_F_CODE, AGRI_CODE_RESET_TIMEOUT),
               F(b4, AGRI_F_COST, 5001));            /* 判据：cost > 1000ms */
        s = ++z->seq;
        AGRI_W(DOM_SENSOR, "AgriSensor", z->prof->zid,
               F(b1, AGRI_F_SEQ, s),
               F(b2, AGRI_F_FAULT, AGRI_FAULT_OUT_OF_RANGE),
               F(b3, AGRI_F_CODE, AGRI_CODE_OUT_OF_RANGE),
               F(b4, AGRI_F_TEMP, -1270));           /* -127.0℃ 明显不合理 */
        s = ++z->seq;
        AGRI_W(DOM_SENSOR, "AgriSensor", z->prof->zid,
               F(b1, AGRI_F_SEQ, s),
               F(b2, AGRI_F_STATE, 3),               /* 3 = degraded */
               F(b3, AGRI_F_TEMP, z->temp_x10),
               F(b4, AGRI_F_HUMI, z->humi_x10));
        break;

    /* ---- 任务调度：卡死 + 队列积压 ---- */
    case SCEN_TASK:
        AGRI_E(DOM_TASK, "AgriTask", z->prof->zid,
               F(b1, AGRI_F_SEQ, s),
               F(b2, AGRI_F_FAULT, AGRI_FAULT_TASK_HUNG),
               F(b3, AGRI_F_CODE, AGRI_CODE_TASK_HUNG),
               F(b4, AGRI_F_COST, 6000));            /* 判据：cost > 5000ms */
        s = ++z->seq;
        AGRI_W(DOM_TASK, "AgriTask", z->prof->zid,
               F(b1, AGRI_F_SEQ, s),
               F(b2, AGRI_F_FAULT, AGRI_FAULT_QUEUE_FULL),
               F(b3, AGRI_F_CODE, AGRI_CODE_QUEUE_FULL),
               F(b4, AGRI_F_QUEUE, 8));
        break;

    /* ---- 内存：堆不足 → 分配失败 → 上报超时（构成因果链）---- */
    case SCEN_MEMORY:
        AGRI_W(DOM_HEALTH, "AgriHealth", z->prof->zid,
               F(b1, AGRI_F_SEQ, s),
               F(b2, AGRI_F_FAULT, AGRI_FAULT_HEAP_LOW),
               F(b3, AGRI_F_CODE, AGRI_CODE_HEAP_LOW),
               F(b4, AGRI_F_HEAP, 3072));            /* 判据：heap < 4096 */
        s = ++z->seq;
        AGRI_E(DOM_HEALTH, "AgriHealth", z->prof->zid,
               F(b1, AGRI_F_SEQ, s),
               F(b2, AGRI_F_FAULT, AGRI_FAULT_ALLOC_FAILED),
               F(b3, AGRI_F_CODE, AGRI_CODE_ALLOC_FAILED),
               F(b4, AGRI_F_HEAP, 1024));            /* err = -12 (ENOMEM) */
        s = ++z->seq;
        AGRI_E(DOM_REPORT, "AgriReport", z->prof->zid,
               F(b1, AGRI_F_SEQ, s),
               F(b2, AGRI_F_FAULT, AGRI_FAULT_PUBLISH_TIMEOUT),
               F(b3, AGRI_F_CODE, AGRI_CODE_PUBLISH_FAIL),
               F(b4, AGRI_F_COST, 5010));            /* 次生故障 */
        break;

    /* ---- 看门狗：漏喂 ---- */
    case SCEN_WATCHDOG:
        AGRI_E(DOM_HEALTH, "AgriHealth", z->prof->zid,
               F(b1, AGRI_F_SEQ, s),
               F(b2, AGRI_F_FAULT, AGRI_FAULT_WDT_FEED_MISSED),
               F(b3, AGRI_F_CODE, AGRI_CODE_WDT_MISS),
               F(b4, AGRI_F_MISS, 2));               /* 判据：miss >= 2 */
        break;

    /* ---- 执行器：灌溉超时（反复触发，用于验证告警抑制）---- */
    case SCEN_ACTUATOR:
        AGRI_W(DOM_CTRL, "AgriCtrl", z->prof->zid,
               F(b1, AGRI_F_SEQ, s),
               F(b2, AGRI_F_FAULT, AGRI_FAULT_ACTUATOR_TIMEOUT),
               F(b3, AGRI_F_CODE, AGRI_CODE_TASK_HUNG),
               F(b4, AGRI_F_COST, 5000));
        break;

    /* ---- 通信：连接失败 → 信号弱 → 重连 ---- */
    case SCEN_NETWORK:
        AGRI_E(DOM_COMM, "AgriComm", z->prof->zid,
               F(b1, AGRI_F_SEQ, s),
               F(b2, AGRI_F_FAULT, AGRI_FAULT_MQTT_CONN_FAIL),
               F(b3, AGRI_F_CODE, AGRI_CODE_PUBLISH_FAIL),
               F(b4, AGRI_F_RSSI, 111));             /* 111 → 实际 -111dBm */
        s = ++z->seq;
        AGRI_W(DOM_COMM, "AgriComm", z->prof->zid,
               F(b1, AGRI_F_SEQ, s),
               F(b2, AGRI_F_FAULT, AGRI_FAULT_RSSI_LOW),
               F(b3, AGRI_F_CODE, AGRI_CODE_RSSI_LOW),
               F(b4, AGRI_F_RSSI, 83));
        s = ++z->seq;
        AGRI_W(DOM_COMM, "AgriComm", z->prof->zid,
               F(b1, AGRI_F_SEQ, s),
               F(b2, AGRI_F_FAULT, AGRI_FAULT_RECONNECT),
               F(b3, AGRI_F_CODE, AGRI_CODE_RECONNECT),
               F(b4, AGRI_F_RETRY, 2));              /* 正常事件，用于检验误报率 */
        break;
    }
}

/* ==========================================================================
 * 健康检查（A003）—— 堆水位用 SDK 真实数据；低于阈值是真的告警
 * ======================================================================== */
static void do_health_check(char *b1, char *b2, char *b3, char *b4)
{
    mem_health_update();

    if (g_heap_free_last == 0) {
        /* ⚠️ 当前 SDK 的 hi_mem_get_sys_info() 未返回有效值。
         * 此时【不编造堆数字】，退化为只报真实可得的栈水位。
         * 日志里 heap 字段缺失（而非填假值）—— 诚实优先于完整。 */
        AGRI_I(DOM_HEALTH, "AgriHealth", '-',
               F(b1, AGRI_F_SEQ, g_cycle),
               F(b2, AGRI_F_COST, (int)g_stack_space_last),   /* 任务栈剩余字节 */
               F(b3, AGRI_F_MISS, (int)g_malloc_fail_last),   /* 累计分配失败次数 */
               NULL);
        return;
    }

    if (g_heap_free_last < HEAP_LOW_THR) {
        /* 真实的内存告警：free_lmp 确实低于水位线 */
        AGRI_W(DOM_HEALTH, "AgriHealth", '-',
               F(b1, AGRI_F_SEQ, g_cycle),
               F(b2, AGRI_F_FAULT, AGRI_FAULT_HEAP_LOW),
               F(b3, AGRI_F_CODE, AGRI_CODE_HEAP_LOW),
               F(b4, AGRI_F_HEAP, (int)g_heap_free_last));
    } else {
        AGRI_I(DOM_HEALTH, "AgriHealth", '-',
               F(b1, AGRI_F_SEQ, g_cycle),
               F(b2, AGRI_F_HEAP, (int)g_heap_free_last),
               F(b3, AGRI_F_COST, (int)g_stack_space_last),
               F(b4, AGRI_F_MISS, (int)g_malloc_fail_last));
    }
}

/* ==========================================================================
 * 主循环
 * ======================================================================== */
static void agri_main_task(void)
{
    char b1[16], b2[16], b3[16], b4[16];
    int  i;
    float temperature = 0.0f, humidity = 0.0f;
    int  last_fail = 0;

    g_task_id = osThreadGetId();

    /* ---- 第一次内存健康探测（用于验证 SDK 内存接口是否可用）----
     * 若日志里 heap=0，说明 hi_mem_get_sys_info() 在当前 SDK 上不返回有效值，
     * 此时健康检查会退化为"只报栈水位"（见 do_health_check 的注释）。 */
    mem_health_update();

    /* ---- 启动日志（生命周期事件 F5001）---- */
    AGRI_I(DOM_HEALTH, "AgriHealth", '-',
           F(b1, AGRI_F_SEQ, 0),
           F(b2, AGRI_F_FAULT, AGRI_FAULT_BOOT),
           F(b3, AGRI_F_CODE, AGRI_CODE_STATE_CHANGE),
           F(b4, AGRI_F_HEAP, (int)g_heap_free_last));   /* 真实剩余堆，0=读不到 */

    /* 注意：这里不用 printf 的 %d/%u —— 平台 printf 对多位数整数不可靠
     * （见 log_def.h 的踩坑说明）。启动横幅只打常量文本 + 自写转换，保证一定正确。
     *
     * ★★ 横幅里必须带【版本号】★★
     *   踩过的坑：改了源码却没重新烧录，串口现象一点没变，排查半天。
     *   有了版本号，只要看一眼开机横幅就知道板上跑的是哪一版——
     *   这是"改了代码但现象没变"最快的判据（docs/09 坑 5 的配套措施）。
     *   改了日志格式/字段顺序/时基，请务必把这个号 +1。
     *
     * ★★ 横幅里还要带【tick 频率】★★
     *   时基错位过一次（把 10ms/tick 当成 1ms，时间戳慢了 10 倍，见 log_def.h）。
     *   把频率打出来，一眼就能核对时间戳的单位对不对：
     *     tickfreq=100Hz 表示 1 tick = 10ms，日志里每秒应有 100 个 tick
     *   这比"盯着时间戳猜它是不是秒"可靠得多。 */
    printf("[agri_logger] FW=v2.7 mode=");
#if SINGLE_DEVICE_MODE
    printf("single-device(真实传感器直通), period=5000ms, tickfreq=");
#else
    printf("6-zones(逻辑设备), period=5000ms, tickfreq=");
#endif
    {
        char tb[16];
        agri_i2a((int)osKernelGetTickFreq(), tb);
        printf("%sHz, heap=", tb);
    }
    {
        char hb[16];
        agri_i2a((int)g_heap_free_last, hb);
        printf("%s\r\n", hb);
    }

    /* ---- 数值转换自检（关键）----
     * 平台 printf 会把 1001 打成 "1"（坑 9）。自检行直接用 F() 走完整链路，
     * 把 4 位数、6 位数、零、负数四种情况各打一遍：
     *   · 全部打全  -> 自写的十进制转换在真机上工作正常
     *   · 出现截断  -> 立刻能看出，不用等业务日志出怪现象才发现
     * 这一行是"数值转换是否可靠"的现场判据，答辩时可直接指着它讲。
     *
     * 期望输出（已在 PC 上用 gcc 编译同一份 log_def.h 验证过）：
     *     I A003 AgriSelfCheck: 1 0 5 1001 8 20480 15 -355 z=-
     *                           ↑零 ↑4位   ↑5位     ↑负数（故意超域，专测符号分支） */
    AGRI_I(DOM_HEALTH, "AgriSelfCheck", '-',
           F(b1, AGRI_F_SEQ,   0),          /* 零：最容易漏掉的边界      */
           F(b2, AGRI_F_FAULT, 1001),       /* 4 位：printf 会打成 "1"   */
           F(b3, AGRI_F_HEAP,  20480),      /* 5 位                      */
           F(b4, AGRI_F_MISS,  -355));      /* 负数：printf 会打成 "-"   */

    while (1) {
        int base_t, base_h, base_l;

        g_cycle++;

        /* ================= 1) 读一次真实传感器 = 园区基准 =================
         * 一次读数驱动全部 6 个棚 —— 这是"只有一个传感器却有 6 个设备"
         * 的物理依据：6 个大棚共享同一片天空。 */
        if (SHT20_ReadData(&temperature, &humidity) != 0) {
            /* 传感器本身读失败：这是【真实的】硬件故障，不是注入的 */
            last_fail++;
            AGRI_E(DOM_SENSOR, "AgriSensor", '-',
                   F(b1, AGRI_F_SEQ, g_cycle),
                   F(b2, AGRI_F_FAULT, AGRI_FAULT_SENSOR_TIMEOUT),
                   F(b3, AGRI_F_CODE, AGRI_CODE_RESET_TIMEOUT),
                   F(b4, AGRI_F_RETRY, last_fail));
            base_t = 250;
            base_h = 610;
        } else {
            last_fail = 0;
            base_t = (int)(temperature * 10.0f);
            base_h = (int)(humidity * 10.0f);
        }

        /* ---- 光照：读【真实】的 AP3216C，不是常量 ----
         * 踩过的坑：初版这里写的是 `base_l = 800;`（一个常量），
         * 于是日志里那列"光照"其实是 800 加各棚偏置，看着像在变、实则纯合成。
         * 这种"看起来像真数据"的假数据最危险 —— 被问一句"光照哪来的"就穿帮。
         * 板载 AP3216C 本来就有 ALS 通道，直接读它。
         * 读失败时【保留上次的值】而不是编一个：留旧值至少还是真实测量过的数。 */
        {
            uint16_t ir_d = 0, als_d = 0, ps_d = 0;
            if (AP3216C_ReadData(&ir_d, &als_d, &ps_d) == IOT_SUCCESS) {
                g_light_last = (int)als_d;
            }
            base_l = g_light_last;
        }

        /* ---- 把本轮真实读数刷到板载 OLED 上 ----
         * 放在读传感器之后、发日志之前：屏幕和日志用的是【同一份】读数，
         * 所以"屏幕上看到的"和"日志里打出来的"必然一致，不会出现对不上的情况。 */
        oled_update(base_t, base_h, base_l, g_cycle);

        /* ---- 模式切换了？立刻刷屏 + 发一条日志 ----
         * 放在这里而不是按键回调里：按键是 100ms 级的，而日志要用到
         * b1..b4 缓冲和 g_cycle，在主循环里发最省事也最安全。 */
        if (g_mode_dirty) {
            g_mode_dirty = 0;
            oled_update(base_t, base_h, base_l, g_cycle);
            AGRI_I(DOM_HEALTH, "AgriMode", '-',
                   F(b1, AGRI_F_SEQ, g_cycle),
                   F(b2, AGRI_F_STATE, g_inject_on ? 1 : 0),  /* 1=注入开 0=纯真实 */
                   F(b3, AGRI_F_HEAP, (int)g_heap_free_last),
                   NULL);
        }

        /* ================= 2) 决定本轮是否注入故障 =================
         * ★ g_inject_on 关掉时，【倒计时都不减】，也就是一条注入日志都不会产生。
         *   日志流里根本不存在那些故障，"纯真实数据"是物理事实而不是界面过滤。 */
        g_injecting_zone = -1;
#if FAULT_INJECT_INTERVAL > 0
        if (g_inject_on && --g_inject_countdown <= 0) {
            g_inject_countdown = FAULT_INJECT_INTERVAL;
            g_injecting_zone = (int)(g_cycle % ZONE_COUNT);   /* 轮流挑一个 zone */
#if SINGLE_DEVICE_MODE
            /* 单设备模式下只有一个 zone。如果场景固定，日志里就永远只有一类故障，
             * 分析链路（跨模块因果关联、聚类）根本测不全，所以让场景逐轮轮换。
             *
             * ★ 必须用【独立的轮换计数器】，不能用 g_cycle % 6 ★
             *   踩过的坑：写成 (scenario_t)(g_cycle % 6)，但注入本身是每
             *   FAULT_INJECT_INTERVAL 轮才发生一次，取模只在"间隔的倍数"这个
             *   子集上取值。实测间隔=3 时 g_cycle 永远是 3 的倍数，
             *   g_cycle % 6 只能得到 0 和 3 —— 六个场景里【只有两个跑得到】，
             *   看板上永远只有 F1001/F1004/F3005 三种故障码，而且数量几乎相等，
             *   极难察觉是 bug。
             *   用独立计数器每次注入 +1，才能真正轮完 6 个场景。 */
            g_zone[g_injecting_zone].scenario = (scenario_t)(g_scenario_turn % 6u);
            g_scenario_turn++;
#endif
        }
#endif

        /* ================= 3) 依次处理各个设备 =================
         * 单设备模式下 ZONE_COUNT=1，这一圈只跑一次，输出就是真实读数。 */
        for (i = 0; i < ZONE_COUNT; i++) {
            zone_state_t *z = &g_zone[i];
            unsigned s;

            /* 3.1 用园区基准驱动本棚环境 */
            zone_env_update(z, base_t, base_h, base_l);

            /* 3.2 本棚是注入对象 → 走故障分支 */
            if (i == g_injecting_zone) {
                inject_fault(z, b1, b2, b3, b4);
                continue;
            }

            /* 3.3 正常周期上报（INFO）—— 题目要求的"每 5 秒一次" */
            s = ++z->seq;
            AGRI_I(DOM_SENSOR, "AgriSensor", z->prof->zid,
                   F(b1, AGRI_F_SEQ, s),
                   F(b2, AGRI_F_TEMP, z->temp_x10),
                   F(b3, AGRI_F_HUMI, z->humi_x10),
                   F(b4, AGRI_F_LIGHT, z->light));

            /* 3.4 上报动作自身（A002，用于统计上报耗时） */
            AGRI_I(DOM_REPORT, "AgriReport", z->prof->zid,
                   F(b1, AGRI_F_SEQ, s),
                   F(b2, AGRI_F_COST, 12 + (int)(s % 40)),
                   NULL, NULL);

            /* 3.5 业务规则一：湿度偏低 → WARN + 开灌溉
             *     题目原文："模拟湿度低于阈值时输出 WARN 并触发'灌溉'状态日志" */
            if (z->humi_x10 < HUMI_LOW_X10 && !z->irrigating) {
                z->irrigating = 1;
                AGRI_W(DOM_SENSOR, "AgriSensor", z->prof->zid,
                       F(b1, AGRI_F_SEQ, s),
                       F(b2, AGRI_F_FAULT, AGRI_FAULT_THRESHOLD_EXCEED),
                       F(b3, AGRI_F_CODE, AGRI_CODE_THRESHOLD),
                       F(b4, AGRI_F_HUMI, z->humi_x10));

                AGRI_I(DOM_CTRL, "AgriCtrl", z->prof->zid,
                       F(b1, AGRI_F_SEQ, s),
                       F(b2, AGRI_F_FAULT, AGRI_FAULT_IRRIGATION_STATE),
                       F(b3, AGRI_F_STATE, 1),            /* 1 = irrigating */
                       F(b4, AGRI_F_THRESH, HUMI_LOW_X10));
            } else if (z->humi_x10 > HUMI_TARGET_X10 && z->irrigating) {
                z->irrigating = 0;
                AGRI_I(DOM_CTRL, "AgriCtrl", z->prof->zid,
                       F(b1, AGRI_F_SEQ, s),
                       F(b2, AGRI_F_FAULT, AGRI_FAULT_IRRIGATION_STATE),
                       F(b3, AGRI_F_STATE, 0),            /* 0 = idle */
                       F(b4, AGRI_F_HUMI, z->humi_x10));
            }

            /* 3.6 业务规则二：温度过高 → WARN + 开通风 */
            if (z->temp_x10 > TEMP_HIGH_X10 && !z->venting) {
                z->venting = 1;
                AGRI_W(DOM_SENSOR, "AgriSensor", z->prof->zid,
                       F(b1, AGRI_F_SEQ, s),
                       F(b2, AGRI_F_FAULT, AGRI_FAULT_THRESHOLD_EXCEED),
                       F(b3, AGRI_F_CODE, AGRI_CODE_THRESHOLD),
                       F(b4, AGRI_F_TEMP, z->temp_x10));

                AGRI_I(DOM_CTRL, "AgriCtrl", z->prof->zid,
                       F(b1, AGRI_F_SEQ, s),
                       F(b2, AGRI_F_FAULT, AGRI_FAULT_IRRIGATION_STATE),
                       F(b3, AGRI_F_STATE, 2),            /* 2 = venting */
                       F(b4, AGRI_F_THRESH, TEMP_HIGH_X10));
            } else if (z->temp_x10 < TEMP_HIGH_X10 - 20 && z->venting) {
                z->venting = 0;
                AGRI_I(DOM_CTRL, "AgriCtrl", z->prof->zid,
                       F(b1, AGRI_F_SEQ, s),
                       F(b2, AGRI_F_FAULT, AGRI_FAULT_IRRIGATION_STATE),
                       F(b3, AGRI_F_STATE, 0),
                       F(b4, AGRI_F_TEMP, z->temp_x10));
            }

            /* 3.7 温度越界检查（物理不可能值） */
            if (z->temp_x10 > TEMP_INVALID_X10) {
                AGRI_W(DOM_SENSOR, "AgriSensor", z->prof->zid,
                       F(b1, AGRI_F_SEQ, s),
                       F(b2, AGRI_F_FAULT, AGRI_FAULT_OUT_OF_RANGE),
                       F(b3, AGRI_F_CODE, AGRI_CODE_OUT_OF_RANGE),
                       F(b4, AGRI_F_TEMP, z->temp_x10));
            }
        }

        /* ================= 4) 健康检查（A003）================= */
        if (g_cycle % HEALTH_CHECK_EVERY == 0) {
            do_health_check(b1, b2, b3, b4);
        }

        /* ================= 5) 等待下一轮 =================
         * ★ 把 5 秒的等待切成 50 个 100ms 小段，每段查一次按键。
         *   否则按一下 KEY 要等最多 5 秒才有反应，演示时会以为没生效。
         *   用轮询而不是中断：主循环本来就要等，顺手查一下最简单，
         *   也避免在中断上下文里改状态（这个项目已经栽过够多坑了）。
         *
         * 用 usleep()（微秒，单位明确）而不是 osDelay()
         * （单位是 RTOS tick，换算关系不确定）。详见 docs/09 坑 4。 */
        for (i = 0; i < CYCLE_PERIOD_MS / KEY_POLL_MS; i++) {
            key_poll();
            usleep(KEY_POLL_MS * 1000);
        }
    }
}

/* ==========================================================================
 * 入口
 * ======================================================================== */
static void agri_logger_entry(void)
{
    osThreadAttr_t attr = {0};
    int i;

    printf("[agri_logger] stage-2 init: 6 zones\r\n");

    /* 初始化 6 个大棚的静态属性 */
    for (i = 0; i < ZONE_COUNT; i++) {
        memset(&g_zone[i], 0, sizeof(g_zone[i]));
        g_zone[i].prof = &ZONE_PROFILES[i];
        g_zone[i].scenario = (scenario_t)i;   /* 每个 zone 主攻一类故障 */
    }

    SHT20_Init();
    /* AP3216C：真实光照来源。单设备模式下光照字段就是它的 ALS 读数。
     * 六棚模式下它是各棚光照的公共基准（不再是写死的常量）。 */
    AP3216C_Init();
    PCF8574_Init();
    /* OLED：脱机也能看到实时读数。注意顺序 —— 官方 demo 是先 PCF8574 后 SSD1306。
     * 初始化失败不影响主流程，只是不刷屏（g_oled_ok=0）。 */
    oled_setup();

    /* 板载 KEY：运行时切换"故障注入"的开关。
     * 默认【关闭】—— 开机就是纯真实数据，演示时先给评委看干净状态，
     * 按一下 KEY 才开始注入故障。见 key_poll() 的说明。 */
    key_setup();

    attr.name       = "agri_main";
    attr.attr_bits  = 0;
    attr.cb_mem     = NULL;
    attr.cb_size    = 0;
    attr.stack_mem  = NULL;
    attr.stack_size = TASK_STACK_SIZE;
    attr.priority   = osPriorityNormal;

    if (osThreadNew((osThreadFunc_t)agri_main_task, NULL, &attr) == NULL) {
        printf("[agri_logger] FATAL: create task failed\r\n");
    } else {
        printf("[agri_logger] task created, stack=8192 bytes\r\n");
    }
}

SYS_RUN(agri_logger_entry);

/* ==========================================================================
 * 附：agri_logger/BUILD.gn 内容
 *
 * static_library("agri_logger") {
 *     sources = [
 *         "agri_logger_multi.c",
 *         "//vendor/hqyj/fs_hi3861/common/bsp/src/hal_bsp_pcf8574.c",
 *         "//vendor/hqyj/fs_hi3861/common/bsp/src/hal_bsp_sht20.c"
 *     ]
 *     include_dirs = [
 *         "//utils/native/lite/include",
 *         "//kernel/liteos_m/kal/cmsis",
 *         "//base/iot_hardware/peripheral/interfaces/kits",
 *         "//vendor/hqyj/fs_hi3861/common/bsp/include"
 *     ]
 * }
 * ======================================================================== */
