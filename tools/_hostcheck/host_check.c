/*
 * 宿主编译对照实验（临时文件，用完即删）
 *
 * 目的：把【真机上跑的那份 log_def.h】原封不动搬到 PC 上编译运行，
 *       看它到底输出什么。这样可以一刀切开两种可能：
 *         · 主机输出正常  → 板子跑的不是这份代码（固件陈旧）或线上丢字节
 *         · 主机输出同样错 → 源码本身有 bug
 *
 * 编译：gcc -std=c99 -I<stub目录> -I<firmware目录> host_check.c -o host_check.exe
 */
#include <stdio.h>
#include <string.h>
#include "log_def.h"          /* ← 真机上用的那一份，一字未改 */

/* ---- 从 agri_logger_multi.c 原样抄来的两个函数 ---- */

static char *agri_fmt_pair(char *buf, int id, int val)
{
    char *p = buf;
    p = agri_i2a(id, p);
    *p++ = ':';
    p = agri_i2a(val, p);
    return buf;
}

#define F(buf, id, val)   agri_fmt_pair((buf), (id), (int)(val))

/* 与固件同尺寸的调用方缓冲：真机上是 char b1[16]... */
static char b1[16], b2[16], b3[16], b4[16];

static void show(const char *what)
{
    printf("  %-34s -> ", what);
    fflush(stdout);
}

int main(void)
{
    printf("======= 0) 时基核对（本轮新增，最容易错的地方）=======\n");
    printf("  模拟真机：tick 频率 = %u Hz（1 tick = %u ms）\n",
           osKernelGetTickFreq(), 1000 / osKernelGetTickFreq());
    printf("  设 tick 计数 = 2750，按 100Hz 换算应为 27.500000 秒\n");
    _stub_tick = 2750;
    printf("  实际输出: ");
    fflush(stdout);
    AGRI_I(DOM_HEALTH, "AgriHealth", '-', F(b1, AGRI_F_SEQ, 1), NULL, NULL, NULL);
    printf("  → 秒数应为 000027；若显示 000002 就是又按 1ms/tick 算错了\n");

    printf("\n  再验一次：tick = 31314（应等于 313.140000 秒）\n");
    _stub_tick = 31314;
    printf("  实际输出: ");
    fflush(stdout);
    AGRI_I(DOM_HEALTH, "AgriHealth", '-', F(b1, AGRI_F_SEQ, 2), NULL, NULL, NULL);

    printf("\n  边界：tick = 0（应等于 000000.000000）\n");
    _stub_tick = 0;
    printf("  实际输出: ");
    fflush(stdout);
    AGRI_I(DOM_HEALTH, "AgriHealth", '-', F(b1, AGRI_F_SEQ, 3), NULL, NULL, NULL);

    printf("\n  若把 tick 频率改成 1000Hz（模拟另一种 SDK 配置），代码应自动正确：\n");
    _stub_freq = 1000;
    _stub_tick = 27507;      /* 27507ms = 27.507s */
    printf("  实际输出: ");
    fflush(stdout);
    AGRI_I(DOM_HEALTH, "AgriHealth", '-', F(b1, AGRI_F_SEQ, 4), NULL, NULL, NULL);
    _stub_freq = 100;        /* 还原 */

    printf("\n======= 1) 启动自检行 AgriSelfCheck =======\n");
    printf("期望: 1 0 5 1001 8 20480 15 -355 z=-\n");
    printf("实际: ");
    fflush(stdout);
    agri_log_emit('I', DOM_HEALTH, "AgriSelfCheck", '-',
                  F(b1, AGRI_F_SEQ,   0),
                  F(b2, AGRI_F_FAULT, 1001),
                  F(b3, AGRI_F_HEAP,  20480),
                  F(b4, AGRI_F_MISS,  -355),
                  30, 1000);

    printf("\n======= 2) 健康行：heap 正常分支 =======\n");
    printf("源码: SEQ, HEAP, COST, MISS\n");
    printf("期望: 1 5 8 1 9 6 15 0 z=-\n");
    printf("实际: ");
    fflush(stdout);
    agri_log_emit('I', DOM_HEALTH, "AgriHealth", '-',
                  F(b1, AGRI_F_SEQ,   5),
                  F(b2, AGRI_F_HEAP,  1),
                  F(b3, AGRI_F_COST,  6),
                  F(b4, AGRI_F_MISS,  0),
                  27, 507000);

    printf("\n======= 3) 健康行：heap 过低分支（4 位故障码）=======\n");
    printf("期望: 1 5 5 3006 6 1 8 1234 z=-\n");
    printf("实际: ");
    fflush(stdout);
    agri_log_emit('W', DOM_HEALTH, "AgriHealth", '-',
                  F(b1, AGRI_F_SEQ,   5),
                  F(b2, AGRI_F_FAULT, 3006),
                  F(b3, AGRI_F_CODE,  1),
                  F(b4, AGRI_F_HEAP,  1234),
                  29, 59000);

    printf("\n======= 4) 阈值越界告警行（4 位故障码 + 3 位温度）=======\n");
    printf("期望: 1 7 5 1004 6 3 2 355 z=A\n");
    printf("实际: ");
    fflush(stdout);
    agri_log_emit('W', DOM_SENSOR, "AgriSensor", 'A',
                  F(b1, AGRI_F_SEQ,   7),
                  F(b2, AGRI_F_FAULT, 1004),
                  F(b3, AGRI_F_CODE,  3),
                  F(b4, AGRI_F_TEMP,  355),
                  27, 502000);

    printf("\n======= 5) 边界：负数 / 零 / 最大 6 位 =======\n");
    show("miss=-1270");
    agri_log_emit('W', DOM_HEALTH, "AgriHealth", '-',
                  F(b1, AGRI_F_MISS, -1270), NULL, NULL, NULL, 1, 1);
    show("seq=0");
    agri_log_emit('I', DOM_HEALTH, "AgriHealth", '-',
                  F(b1, AGRI_F_SEQ, 0), NULL, NULL, NULL, 1, 1);
    show("seq=999999");
    agri_log_emit('I', DOM_HEALTH, "AgriHealth", '-',
                  F(b1, AGRI_F_SEQ, 999999), NULL, NULL, NULL, 1, 1);

    printf("\n======= 6) 最长可能行（验证不超 128 字节）=======\n");
    {
        char longtag[32];
        int i;
        for (i = 0; i < 31; i++) longtag[i] = 'T';
        longtag[31] = '\0';
        agri_log_emit('E', DOM_FAULT, longtag, 'F',
                      F(b1, AGRI_F_FAULT, 5002),
                      F(b2, AGRI_F_CODE,  99),
                      F(b3, AGRI_F_HEAP,  429496),
                      F(b4, AGRI_F_MISS,  -1270),
                      359999, 999999);
    }

    return 0;
}
