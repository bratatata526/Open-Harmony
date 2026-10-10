/* 宿主对照实验用的最小桩：只为让 log_def.h 能在 PC 上编译
 *
 * tick 频率可设置，用来复现真机环境（本 SDK 是 100Hz，即 1 tick = 10ms）。
 * 默认就按真机的 100Hz，避免"主机跑对了、真机跑错了"这种假通过。
 */
#ifndef CMSIS_OS2_STUB_H
#define CMSIS_OS2_STUB_H

#include <stdint.h>

static uint32_t _stub_tick = 0;
static uint32_t _stub_freq = 100;      /* 真机实测 100Hz = 10ms/tick */

static uint32_t osKernelGetTickCount(void) { return _stub_tick; }
static uint32_t osKernelGetTickFreq(void) { return _stub_freq; }

#endif
