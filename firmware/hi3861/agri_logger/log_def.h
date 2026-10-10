/*
 * Copyright (c) 2026 OpenHarmony 日志智能聚合分析工具项目组
 *
 * 日志规范实现 —— 全项目【唯一】的日志出口
 *
 * ============================================================================
 * 这个文件是"设备端 ↔ 分析端"的接口契约。改它之前请先读 docs/02-log-spec.md。
 * ============================================================================
 *
 * 设计约束（来自真实平台，不是风格偏好）：
 *   1. hilog_lite 不支持 %s           → 消息体里【禁止出现字符串值】
 *   2. 单条日志 ≤ 128 字节            → 超长会被【静默丢弃】，不报错不截断
 *   3. 最多 6 个数字变参              → 字段数量受限
 *   4. Hi3861 无 RTC                  → 时间戳用"上电后秒.微秒"
 *
 * 因此消息体格式为：
 *
 *     <zone> <字段ID>:<值> <字段ID>:<值> ...
 *
 * 例：
 *     000120.456789 I A001 AgriSensor: A 1:24 2:281 3:466 7:120456
 *     │             │ │    │          │ │    │     │     └ tick
 *     │             │ │    │          │ │    │     └────── 湿度 46.6%
 *     │             │ │    │          │ │    └──────────── 温度 28.1℃
 *     │             │ │    │          │ └───────────────── seq = 24
 *     │             │ │    │          └─────────────────── zone = A
 *     │             │ │    └────────────────────────────── tag
 *     │             │ └─────────────────────────────────── domain
 *     │             └───────────────────────────────────── level
 *     └─────────────────────────────────────────────────── 上电后 120.456789 秒
 *
 * 为什么值全是数字但加了 "ID:"：
 *   · 纯位置参数（只有值）→ 字段一多就没人记得住第 3 个是什么，可读性归零
 *   · ID 是 1~2 字符的数字，成本极低，却让日志【人眼可读】且【顺序无关】
 *   · 解析器用 ID 反查字段名与单位，实现"设备端紧凑、分析端可读"
 *
 * 为什么温度存 ×10 的整数（281 表示 28.1℃）：
 *   · 绕开轻量平台浮点 printf 支持不确定的坑
 *   · 模板挖掘时 281/282 都被归一化为 <*>，不会因浮点精度产生模板爆炸
 */

#ifndef AGRI_LOG_DEF_H
#define AGRI_LOG_DEF_H

#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include "cmsis_os2.h"

/* ==========================================================================
 * 1. 模块 ID（domain）—— 对应 docs/02 §3.2
 * ======================================================================== */
#define DOM_SENSOR   "A001"   /* 环境传感：温湿度、光照                      */
#define DOM_REPORT   "A002"   /* 数据上报：MQTT / 串口上行                    */
#define DOM_HEALTH   "A003"   /* 设备自检：堆内存、栈水位、心跳                */
#define DOM_CTRL     "A004"   /* 执行器：灌溉、风扇、蜂鸣器                    */
#define DOM_TASK     "A005"   /* 任务调度：超时、队列积压                     */
#define DOM_COMM     "A006"   /* 通信：WiFi、MQTT 重连                       */
#define DOM_FAULT    "A007"   /* 故障注入器（仅测试构建）                     */

/* ==========================================================================
 * 2. 字段 ID —— 对应 docs/02 §3.3
 *    ★ 新增字段必须：① 在这里加 ② 更新 docs/02 ③ 更新 Python 的 FIELD_TABLE
 * ======================================================================== */
typedef enum {
    AGRI_F_SEQ     = 1,   /* 该 zone 内单调递增序号（丢包检测的唯一依据） */
    AGRI_F_TEMP    = 2,   /* 温度 ×10    例：281 = 28.1℃                 */
    AGRI_F_HUMI    = 3,   /* 湿度 ×10    例：466 = 46.6%                 */
    AGRI_F_LIGHT   = 4,   /* 光照相对值  0~1000                          */
    AGRI_F_FAULT   = 5,   /* 故障码数值（含义见 docs/02 §5.2）            */
    AGRI_F_CODE    = 6,   /* 错误码（含义见 docs/02 §5.4）                */
    AGRI_F_TICK    = 7,   /* 系统 tick，单位 ms                          */
    AGRI_F_HEAP    = 8,   /* 剩余堆内存，单位字节                         */
    AGRI_F_COST    = 9,   /* 耗时，单位 ms                               */
    AGRI_F_RETRY   = 10,  /* 重试次数                                    */
    AGRI_F_STATE   = 11,  /* 状态机：0=idle 1=irrigating 2=venting 3=degraded */
    AGRI_F_THRESH  = 12,  /* 阈值 ×10（仅在需要强调时才带）               */
    AGRI_F_RSSI    = 13,  /* WiFi 信号强度绝对值（67 表示 -67dBm）        */
    AGRI_F_QUEUE   = 14,  /* 消息队列长度                                 */
    AGRI_F_MISS    = 15,  /* 看门狗漏喂次数                               */
    AGRI_F_ZONE    = 16,  /* 逻辑设备编号 0=A 1=B ... 5=F（用于非首字段场景）*/
} agri_field_t;

/* 故障码数值 —— 与 docs/02 §5.2 的 Fxxxx 一一对应 */
#define AGRI_FAULT_NONE              0
#define AGRI_FAULT_SENSOR_TIMEOUT    1001   /* F1001 */
#define AGRI_FAULT_ONEWIRE_NO_ACK    1002   /* F1002 */
#define AGRI_FAULT_CRC_MISMATCH      1003   /* F1003 */
#define AGRI_FAULT_OUT_OF_RANGE      1004   /* F1004 */
#define AGRI_FAULT_THRESHOLD_EXCEED  1005   /* F1005 */
#define AGRI_FAULT_SENSOR_OFFLINE    1006   /* F1006 */
#define AGRI_FAULT_MQTT_CONN_FAIL    2001   /* F2001 */
#define AGRI_FAULT_PUBLISH_TIMEOUT   2002   /* F2002 */
#define AGRI_FAULT_RSSI_LOW          2003   /* F2003 */
#define AGRI_FAULT_RECONNECT         2004   /* F2004 */
#define AGRI_FAULT_FRAME_CRC_ERROR   2005   /* F2005 */
#define AGRI_FAULT_ALLOC_FAILED      3001   /* F3001 */
#define AGRI_FAULT_STACK_OVERFLOW    3002   /* F3002 */
#define AGRI_FAULT_TASK_HUNG         3003   /* F3003 */
#define AGRI_FAULT_QUEUE_FULL        3004   /* F3004 */
#define AGRI_FAULT_WDT_FEED_MISSED   3005   /* F3005 */
#define AGRI_FAULT_HEAP_LOW          3006   /* F3006 */
#define AGRI_FAULT_ACTUATOR_TIMEOUT  4001   /* F4001 */
#define AGRI_FAULT_IRRIGATION_STATE  4002   /* F4002 */
#define AGRI_FAULT_BOOT              5001   /* F5001 */
#define AGRI_FAULT_CONFIG_LOAD_FAIL  5002   /* F5002 */

/* 错误码 —— 与 docs/02 §5.4 一一对应 */
#define AGRI_CODE_NONE         0
#define AGRI_CODE_RESET_TIMEOUT 1
#define AGRI_CODE_CRC_MISMATCH  2
#define AGRI_CODE_OUT_OF_RANGE  3
#define AGRI_CODE_NO_RESPONSE   4
#define AGRI_CODE_THRESHOLD     5
#define AGRI_CODE_STATE_CHANGE  6
#define AGRI_CODE_PUBLISH_FAIL  7
#define AGRI_CODE_RSSI_LOW      8
#define AGRI_CODE_RECONNECT     9
#define AGRI_CODE_STACK_OVERFLOW 10
#define AGRI_CODE_TASK_HUNG     11
#define AGRI_CODE_ALLOC_FAILED  12
#define AGRI_CODE_QUEUE_FULL    13
#define AGRI_CODE_WDT_MISS      14
#define AGRI_CODE_HEAP_LOW      15
#define AGRI_CODE_CONFIG_FAIL   16
#define AGRI_CODE_UNKNOWN       99

/* ==========================================================================
 * 3. 核心宏
 * ======================================================================== */

/**
 * @brief 输出一条规范日志
 *
 * @param lv    级别字符：'D' 'I' 'W' 'E' 'F'
 * @param dom   模块 ID，用 DOM_xxx
 * @param tag   模块标签字符串字面量，≤31 字符，如 "AgriSensor"
 * @param zone  逻辑设备：'A'~'F'，或 '-' 表示与具体设备无关（如全局健康日志）
 * @param f1..f4 字段对，格式为 "ID:值"。不需要的填 NULL
 *
 * @note 使用方式（字段数可变，最多 4 个）：
 *         AGRI_LOG('I', DOM_SENSOR, "AgriSensor", 'A', "1:24", "2:281", "3:466", "7:120456");
 *         AGRI_LOG('E', DOM_SENSOR, "AgriSensor", 'B', "1:103", "5:1002", "6:1", NULL);
 *         AGRI_LOG('I', DOM_HEALTH, "AgriHealth", '-', "1:0", "8:20480", NULL, NULL);
 *
 * ★★★ 时基：必须用 osKernelGetTickFreq()，不能把 tick 当成 1ms ★★★
 *
 * 踩过的坑（真机实测）：
 *   最初按"tick = 1ms"写死 `_t_ / 1000` 当秒数。跑起来**定时完全正常**
 *   （实测每轮 5.02 秒，与 usleep(5000000) 一致），**但时间戳慢了约 10 倍**：
 *     真实经过 40.1 秒 → 设备时间戳只前进 4.1 秒。
 *
 * 根因：本 SDK 的 `LOSCFG_BASE_CORE_TICK_PER_SECOND = 100`
 *       （见 device/hisilicon/.../targets/hi3861v100/include/target_config.h），
 *       即 **1 tick = 10ms**，不是 1ms。
 *
 * 危害（比"时间戳不准"严重得多）：
 *   · 分析端按"设备秒"切 60 秒窗口 → 实际切成了约 600 秒，窗口语义全错
 *   · 日志静默检测的判据是"间隔 > 2×正常周期(5s)"；
 *     时基慢 10 倍时间隔显示成 0.5s，**静默检测永远不触发**
 *   · 模拟器发的是真实秒，真机发的是 1/10 秒 → 两边时间尺度不一致，
 *     评测数据集不能代表真机行为
 *
 * 因此改用 CMSIS-RTOS2 的 `osKernelGetTickFreq()` 动态换算：
 *   · 与 SDK 配置解耦，tick 改成 1000Hz 也自动正确
 *   · 不写死任何魔数
 */
#define AGRI_LOG(lv, dom, tag, zone, f1, f2, f3, f4)                            \
    do {                                                                        \
        unsigned long long _t_ = (unsigned long long)osKernelGetTickCount();    \
        unsigned _f_ = (unsigned)osKernelGetTickFreq();                         \
        if (_f_ == 0) {                                                         \
            _f_ = 100;      /* 兜底：本平台实测 100Hz(10ms/tick)，见上方说明 */  \
        }                                                                       \
        agri_log_emit((lv), (dom), (tag), (zone),                               \
                      (f1), (f2), (f3), (f4),                                   \
                      (unsigned)(_t_ / _f_),                                    \
                      (unsigned)((_t_ % _f_) * (1000000U / _f_)));              \
    } while (0)

/* 便捷宏：只关心级别 */
#define AGRI_I(dom, tag, zone, f1, f2, f3, f4) AGRI_LOG('I', dom, tag, zone, f1, f2, f3, f4)
#define AGRI_W(dom, tag, zone, f1, f2, f3, f4) AGRI_LOG('W', dom, tag, zone, f1, f2, f3, f4)
#define AGRI_E(dom, tag, zone, f1, f2, f3, f4) AGRI_LOG('E', dom, tag, zone, f1, f2, f3, f4)

/* 校验单条消息体长度上限 —— 超过这个值 hilog_lite 会静默丢弃 */
#define AGRI_MSG_MAX   120

/* ==========================================================================
 * 4. 实现（static inline 放在头文件里，够小，省一个编译单元）
 * ======================================================================== */

/**
 * @brief 把 "ID:值" 形式的字段对展开成 "ID 值"，成功返回 1，失败返回 0
 *
 * ★★★ 重大踩坑记录：为什么不用 snprintf 做整数转换 ★★★
 *
 * 症状：真机上 1001 被打印成 "1"、5001 被打印成 "5"、-1270 被打印成 "-"。
 *       同一份代码在主机 gcc 上完全正确（输出 "5 1001"）。
 *       即：**同样的代码，主机对、板子错**。
 *
 * 根因：Hi3861 SDK 的 printf/snprintf 在【多位数整数】格式化上有缺陷
 *       （嵌入式 newlib 的常见问题：整数格式化被配成非 32 位宽度，
 *        导致 1001 只输出首位 "1"）。
 *       单字符输出正常（如 cost=50 中的 "5"、"0"），所以不易察觉。
 *
 * 对策：**日志路径上完全不用 snprintf 格式化整数**，改用自己写的
 *       十进制转换。这样既绕开平台 bug，也避免了 stdio 的重开销
 *       （在 5 秒周期的实时任务里更合适）。
 *
 * 教训：轻量平台上的 printf 不可靠，日志这种"必须准确"的路径要自己控制。
 *       详见 docs/09-troubleshooting-log.md 坑 9。
 */
static inline char *agri_u2a(unsigned int v, char *p)
{
    char tmp[12];
    int  n = 0;

    if (v == 0) {
        *p++ = '0';
        *p = '\0';
        return p;
    }
    while (v > 0 && n < (int)sizeof(tmp)) {
        tmp[n++] = (char)('0' + (v % 10u));
        v /= 10u;
    }
    while (n > 0) {
        *p++ = tmp[--n];
    }
    *p = '\0';
    return p;
}

static inline char *agri_i2a(int v, char *p)
{
    if (v < 0) {
        *p++ = '-';
        /* 注意：不能写 -v，INT_MIN 取负会溢出 */
        return agri_u2a((unsigned int)(-(long long)v), p);
    }
    return agri_u2a((unsigned int)v, p);
}

/**
 * @brief 输出 6 位十进制（不足补前导零），用于时间戳的 秒/微秒
 *
 * 为什么不用 printf("%06u")：平台 printf 的多位数整数格式化有缺陷（见上）。
 */
static inline char *agri_pad6(unsigned int v, char *p)
{
    char tmp[12];
    int  n = 0;

    if (v > 999999u) {
        v = 999999u;                    /* 钳制，避免撑爆时间戳字段 */
    }
    while (n < 6) {
        tmp[n++] = (char)('0' + (v % 10u));
        v /= 10u;
    }
    while (n > 0) {
        *p++ = tmp[--n];
    }
    *p = '\0';
    return p;
}

static inline int agri_expand_field(const char *field, char *out, int out_size)
{
    const char *colon;
    const char *val;
    char       *p = out;
    int         id_len, val_len;
    int         neg = 0;

    if (field == NULL || out == NULL || out_size < 12) {
        return 0;
    }
    colon = strchr(field, ':');
    if (colon == NULL) {
        /* 忘了写冒号：原样拷过去，解析器会按"未知格式"记一笔，便于发现代码写错 */
        int n = (int)strlen(field);
        if (n > out_size - 1) {
            n = out_size - 1;
        }
        memcpy(out, field, (size_t)n);
        out[n] = '\0';
        return (n > 0) ? 1 : 0;
    }

    id_len = (int)(colon - field);
    val = colon + 1;
    val_len = (int)strlen(val);
    if (id_len <= 0 || val_len <= 0) {
        return 0;
    }

    /* ---- 字段 ID：逐字符拷贝 ---- */
    if (p + id_len + 1 >= out + out_size) {
        return 0;
    }
    memcpy(p, field, (size_t)id_len);
    p += id_len;
    *p++ = ' ';

    /* ---- 值：自己解析十进制，完全绕开 snprintf ---- */
    if (*val == '-') {
        neg = 1;
        val++;
    }
    while (*val >= '0' && *val <= '9') {
        if (p >= out + out_size - 2) {
            return 0;                       /* 缓冲不足，安全退出 */
        }
        /* 这里不用 atoi/strtol，同样是为了避开平台库的不确定行为 */
        *p++ = *val++;
    }
    if (neg) {
        /* 负号要插到数字前面：整体右移一位 */
        char *q = p;
        while (q > out + id_len + 1) {
            *q = *(q - 1);
            q--;
        }
        *(out + id_len + 1) = '-';
        p++;
    }
    *p = '\0';
    return 1;
}

/**
 * @brief 组装并打印一行规范日志
 *
 * 输出格式（与 docs/02-log-spec.md §2.2 一致）：
 *     <秒>.<微秒> <级别> <domain> <tag>: <zone> <ID:值>...
 *
 * 注意这里用 printf 而不是 HiLogPrint，原因见 docs/02 §2.4：
 *   · hilog_lite 不支持 %s（我们的 tag/domain 是字符串）
 *   · 原生接口需要改 SDK 公共头文件注册模块
 *   · printf 输出的字节流与 hilog 规范【完全同构】，解析器无法区分也不需要区分
 */
static inline void agri_log_emit(char lv, const char *dom, const char *tag, char zone,
                                 const char *f1, const char *f2,
                                 const char *f3, const char *f4,
                                 unsigned sec, unsigned usec)
{
    char msg[AGRI_MSG_MAX];
    char b1[40], b2[40], b3[40], b4[40];
    const char *fields[4];
    int pos = 0;
    int rc;

    /* ---- 第一步：把每个 "ID:值" 展开成 "ID 值"，各自独立缓冲 ---- */
    if (agri_expand_field(f1, b1, (int)sizeof(b1)) > 0) { fields[0] = b1; } else { fields[0] = NULL; }
    if (agri_expand_field(f2, b2, (int)sizeof(b2)) > 0) { fields[1] = b2; } else { fields[1] = NULL; }
    if (agri_expand_field(f3, b3, (int)sizeof(b3)) > 0) { fields[2] = b3; } else { fields[2] = NULL; }
    if (agri_expand_field(f4, b4, (int)sizeof(b4)) > 0) { fields[3] = b4; } else { fields[3] = NULL; }

    /* ---- 第二步：顺序拼接，缺字段自动跳过（允许 NULL 占位）----
     * 用 strcpy 而不是 snprintf：内容全是 ASCII、长度已由上游保证，
     * 且必须避开平台 printf 的整数格式化缺陷（见下方"第四步"）。 */
    msg[0] = '\0';
    for (int i = 0; i < 4; i++) {
        if (fields[i] == NULL) {
            continue;
        }
        if (pos > 0) {
            if (pos >= (int)sizeof(msg) - 1) {
                break;
            }
            msg[pos++] = ' ';
        }
        {
            const char *s = fields[i];
            while (*s != '\0' && pos < (int)sizeof(msg) - 1) {
                msg[pos++] = *s++;
            }
        }
        msg[pos] = '\0';
    }

    /* ---- 第三步：zone 放最后——日志刷屏时最右一列是设备号最好定位 ---- */
    if (pos > 0 && pos < (int)sizeof(msg) - 5) {
        msg[pos++] = ' ';
    }
    if (pos < (int)sizeof(msg) - 4) {
        msg[pos++] = 'z';
        msg[pos++] = '=';
        msg[pos++] = zone;
        msg[pos] = '\0';
    }

    /* ---- 第四步：输出整行 ----
     * ⚠️ 时间戳用 "%06u.%06u" 是多位数整数格式化 —— 正是平台 printf 有缺陷的场景。
     *    因此这里不用 printf 的格式化能力，改为手工拼好整行再输出。
     *
     * ⚠️ 输出用 putchar 逐字符，不能用 fputs/puts：
     *    这个 SDK 的 stdio 是【按需裁剪】的，fputs 没有链进 libc，
     *    编译会报 `undefined reference to 'fputs'`。
     *    printf 与 putchar 可用（实测），所以逐字符输出是最稳的方式。 */
    {
        char line[AGRI_MSG_MAX + 48];
        char *p = line;
        char  tsbuf[24];
        char *tp = tsbuf;

        tp = agri_pad6(sec, tp);
        *tp++ = '.';
        tp = agri_pad6(usec, tp);
        *tp = '\0';

        for (const char *s = tsbuf; *s != '\0'; ) { *p++ = *s++; }
        *p++ = ' ';
        *p++ = lv;
        *p++ = ' ';
        for (const char *s = dom; *s != '\0'; ) { *p++ = *s++; }
        *p++ = ' ';
        for (const char *s = tag; *s != '\0'; ) { *p++ = *s++; }
        *p++ = ':';
        *p++ = ' ';
        for (const char *s = msg; *s != '\0'; ) { *p++ = *s++; }
        *p++ = '\r';
        *p++ = '\n';
        *p = '\0';

        for (const char *s = line; *s != '\0'; s++) {
            putchar((int)(unsigned char)*s);
        }
    }
}

#endif /* AGRI_LOG_DEF_H */
