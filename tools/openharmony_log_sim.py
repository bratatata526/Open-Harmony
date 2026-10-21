#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hi3861 多任务日志模拟器（OpenHarmony 系统日志异常智能聚合分析工具）

输出与真机【字节级同构】的日志流，使 2~6 组不依赖任何硬件即可开始开发。

设计要点（详见 docs/04-firmware.md §5）：
  1. 格式同构   —— 每行都通过 docs/02-log-spec.md §6 的正则自检，不通过直接抛异常
  2. 行长约束   —— 每行 <= 128 字节。hilog_lite 对超长日志是【静默丢弃】的，
                   真机上这类日志会凭空消失，是极难排查的故障，所以这里硬性拦截
  3. 虚拟时钟   —— --speed N 加速 N 倍，可快速产出"一整天"的数据
  4. 故障注入   --scenario 选择注入哪些场景，与固件 fault_inject.c 语义一致
  5. 固定 seed  —— 6 个组拿到完全相同的日志流，算法效果可横向比较（评测前提）
  6. 信道模拟   —— 丢行 / 乱序 / 截断 / 乱码，检验解析器的健壮性
  7. 重启模拟   —— 周期性产生 boot+1 且 seq 归零，检验时间轴换算
  8. 标注输出   —— 另存 ground-truth，供 eval/metrics_detect.py 计算召回率

用法示例：
  # 打印到屏幕观察（10 倍速）
  python tools/openharmony_log_sim.py --speed 10

  # 生成 30 分钟带标注的评测集（固定 seed，推荐）
  python tools/openharmony_log_sim.py --duration 1800 --seed 20260101 \\
      --out eval/dataset/d2_1800s.log --label-out eval/dataset/d2_1800s.gt.jsonl

  # 带噪声信道，检验采集器健壮性
  python tools/openharmony_log_sim.py --duration 600 --drop-rate 0.002 \\
      --truncate-rate 0.001 --garbage-rate 0.001

  # 自检（校验正则与行长约束，CI 里跑）
  python tools/openharmony_log_sim.py --selftest
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Optional

# ---------------------------------------------------------------------------
# ★ 唯一真源：字段表从 agri_logger.parser.fields 导入，不在这里重复定义
#
# 这样做的意义：设备端（log_def.h）、模拟器（本文件）、解析器（parser.py）
# 三方共用同一张字段表。改字段只需改一处，不存在"三端不一致"的可能。
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from agri_logger.parser import fields as F
    from agri_logger.parser.farm_model import FarmModel
except ImportError as e:                                    # pragma: no cover
    print(f"无法导入 agri_logger.parser（{e}）\n"
          f"请在仓库根目录下运行，或检查 agri_logger/ 目录是否完整。", file=sys.stderr)
    raise SystemExit(2)

# 字段 ID 常量（与 log_def.h 的 agri_field_t 一致）
F_SEQ, F_TEMP, F_HUMI, F_LIGHT = 1, 2, 3, 4
F_FAULT, F_CODE, F_TICK, F_HEAP = 5, 6, 7, 8
F_COST, F_RETRY, F_STATE, F_THRESH = 9, 10, 11, 12
F_RSSI, F_QUEUE, F_MISS = 13, 14, 15
# 系统异常类专用字段（Hi3861 FAQ 第 3 章）
F_MCAUSE, F_ADDR, F_CPU, F_MEMFAIL = 17, 18, 19, 20

# 故障码数值（与 log_def.h 的 AGRI_FAULT_xxx 一致）
FAULT = {f.name: f.code for f in F.FAULTS.values()}
ERROR_CODE = {name: code for code, name in F.ERROR_CODES.items()}

# 冻结正则，与 docs/02 §6 一致
RE_SELF_HILOG = re.compile(
    r"^\d{6,}\.\d{6} [DIWEF] [0-9A-Fa-f]{4,5} [A-Za-z][A-Za-z0-9_]{0,30}: .+$"
)

MAX_LINE_BYTES = 128          # hilog_lite 硬限制：超过会被静默丢弃
ZONES = list("ABCDEF")        # 6 个逻辑大棚

DOMAIN = {
    "sensor": "A001", "report": "A002", "health": "A003", "ctrl": "A004",
    "task": "A005", "comm": "A006", "fault": "A007", "crash": "A008",
}
TAG = {
    "sensor": "AgriSensor", "report": "AgriReport", "health": "AgriHealth",
    "ctrl": "AgriCtrl", "task": "AgriTask", "comm": "AgriComm", "fault": "AgriFault",
    "crash": "AgriCrash",
}


def fmt_msg(zone: str, *pairs: tuple[int, int]) -> str:
    """组装规范消息体：'<ID> <值> <ID> <值> ... z=<zone>'

    这是与 log_def.h 的 AGRI_LOG 宏【完全对应】的 Python 实现。
    例：fmt_msg('A', (1, 24), (2, 281), (3, 466)) → '1 24 2 281 3 466 z=A'
    """
    parts = []
    for fid, val in pairs:
        parts.append(str(fid))
        parts.append(str(int(val)))
    parts.append(f"z={zone}")
    return " ".join(parts)

# ---------------------------------------------------------------------------
# 故障场景表：与固件 fault_inject.c 一一对应
# 每条 = (级别, 域, [(字段ID, 值), ...], 故障码, 根因类, 是否次生事件)
# 消息体的具体拼装交给 fmt_msg()，保证与 log_def.h 同构。
# ---------------------------------------------------------------------------

SCENARIOS: dict[str, list[tuple]] = {
    # 传感器读取失败（真实物理故障的软件复刻）
    "sensor_timeout": [
        ("E", "sensor", [(F_FAULT, 1002), (F_CODE, 1), (F_RETRY, 3), (F_COST, 5001)],
         "F1002", "onewire_bus_failure", False),
        ("W", "sensor", [(F_FAULT, 1004), (F_CODE, 3), (F_TEMP, -1270)],
         "F1004", "onewire_bus_failure", True),
        ("W", "sensor", [(F_STATE, 3), (F_TEMP, 248), (F_HUMI, 610)],
         "F1004", "onewire_bus_failure", True),
    ],
    # 调度与并发
    "task_starvation": [
        ("E", "task", [(F_FAULT, 3003), (F_CODE, 11), (F_COST, 6000)],
         "F3003", "task_starvation", False),
        ("W", "task", [(F_FAULT, 3004), (F_CODE, 13), (F_QUEUE, 8)],
         "F3004", "task_starvation", True),
    ],
    # 内存（渐进式，构成因果链）
    "heap_exhaustion": [
        ("W", "health", [(F_FAULT, 3006), (F_CODE, 15), (F_HEAP, 3072)],
         "F3006", "heap_exhaustion", False),
        ("E", "health", [(F_FAULT, 3001), (F_CODE, 12), (F_HEAP, 1024)],
         "F3001", "heap_exhaustion", True),
        ("E", "report", [(F_FAULT, 2002), (F_CODE, 7), (F_COST, 5010)],
         "F2002", "heap_exhaustion", True),
    ],
    # 看门狗
    "watchdog": [
        ("E", "health", [(F_FAULT, 3005), (F_CODE, 14), (F_MISS, 2)],
         "F3005", "watchdog_timeout", False),
    ],
    # 执行器超时（反复触发，用于验证告警抑制）
    "actuator_timeout": [
        ("W", "ctrl", [(F_FAULT, 4001), (F_CODE, 11), (F_COST, 5000), (F_RETRY, 1)],
         "F4001", "actuator_timeout", False),
    ],
    # 通信
    "network": [
        ("E", "comm", [(F_FAULT, 2001), (F_CODE, 7), (F_RSSI, 111)],
         "F2001", "network_failure", False),
        ("W", "comm", [(F_FAULT, 2003), (F_CODE, 8), (F_RSSI, 83)],
         "F2003", "network_failure", True),
        ("W", "comm", [(F_FAULT, 2004), (F_CODE, 9), (F_RETRY, 2)],
         "F2004", "network_failure", True),
    ],
    # 正常业务事件（不算异常，用于检验误报率）
    "normal_irrigation": [
        ("I", "ctrl", [(F_FAULT, 4002), (F_CODE, 6), (F_STATE, 1)],
         "F4002", "normal_operation", False),
    ],
    # ---------------- 系统异常类（Hi3861V100/LV100 FAQ 第 3 章）----------------
    # FAQ 3.1 / 3.1.4：CPU 被长期占用 → 看门狗漏喂 → 超时复位 → 异常复位（完整因果链）
    "watchdog_reset": [
        ("W", "task", [(F_FAULT, 3011), (F_CODE, 19), (F_CPU, 98)],
         "F3011", "cpu_overload", False),
        ("E", "health", [(F_FAULT, 3005), (F_CODE, 14), (F_MISS, 2)],
         "F3005", "watchdog_timeout", True),
        ("E", "crash", [(F_FAULT, 3007), (F_CODE, 17), (F_MISS, 3)],
         "F3007", "watchdog_reset", True),
        ("E", "health", [(F_FAULT, 5003), (F_CODE, 24)],
         "F5003", "abnormal_reset", True),
    ],
    # FAQ 3.2：任务/中断中死机 + 非法地址访问（mcause 见 FAQ 表3-5，addr 为 mtval）
    "system_crash": [
        ("E", "crash", [(F_FAULT, 3009), (F_CODE, 20), (F_MCAUSE, 5)],
         "F3009", "crash_task", False),
        ("E", "crash", [(F_FAULT, 3010), (F_CODE, 22), (F_ADDR, 3584)],
         "F3010", "bad_addr", True),
        ("E", "crash", [(F_FAULT, 3008), (F_CODE, 18), (F_MCAUSE, 2)],
         "F3008", "isr_block", True),
        ("E", "health", [(F_FAULT, 5003), (F_CODE, 24)],
         "F5003", "abnormal_reset", True),
    ],
    # ---- 传感器读取失败全链路：超时 → 校验失败 → 离线（覆盖 F1001/F1003/F1006）----
    "sensor_read_fail": [
        ("E", "sensor", [(F_FAULT, 1001), (F_CODE, 1), (F_COST, 1200)],
         "F1001", "sensor_timeout", False),
        ("E", "sensor", [(F_FAULT, 1003), (F_CODE, 2), (F_RETRY, 2)],
         "F1003", "crc_error", True),
        ("E", "sensor", [(F_FAULT, 1006), (F_CODE, 4), (F_MISS, 5)],
         "F1006", "sensor_offline", True),
    ],
    # ---- 数据帧校验错（覆盖 F2005），信号弱作为次生 ----
    "frame_corrupt": [
        ("E", "comm", [(F_FAULT, 2005), (F_CODE, 2), (F_RETRY, 3)],
         "F2005", "frame_crc_error", False),
        ("W", "comm", [(F_FAULT, 2003), (F_CODE, 8), (F_RSSI, 72)],
         "F2003", "frame_crc_error", True),
    ],
    # ---- 任务栈溢出（覆盖 F3002）----
    # 字段表不动：用 state=3(degraded) + code=10(stack_overflow) 表达"栈水位超限"，
    # 再接一条分配失败与任务卡死，构成"栈溢出 → 内存被打坏 → 任务跑不动"的因果链。
    "stack_overflow": [
        ("E", "health", [(F_FAULT, 3002), (F_CODE, 10), (F_STATE, 3)],
         "F3002", "stack_overflow", False),
        ("E", "health", [(F_FAULT, 3001), (F_CODE, 12), (F_HEAP, 1024)],
         "F3001", "stack_overflow", True),
        ("E", "task", [(F_FAULT, 3003), (F_CODE, 11), (F_COST, 6200)],
         "F3003", "stack_overflow", True),
    ],
    # ---- 启动期配置问题（覆盖 F5002/F5004）----
    "boot_config_fail": [
        ("W", "health", [(F_FAULT, 5002), (F_CODE, 16), (F_RETRY, 1)],
         "F5002", "config_load_fail", False),
        ("W", "health", [(F_FAULT, 5004), (F_CODE, 25)],
         "F5004", "wdt_cfg_invalid", False),
    ],
}

# 每个 zone 的主攻场景（与 docs/02 §5.3 的 6 组分工一致）
ZONE_SCENARIO = {
    "A": "sensor_timeout",
    "B": "task_starvation",
    "C": "heap_exhaustion",
    "D": "watchdog",
    "E": "actuator_timeout",
    "F": "network",
}


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

@dataclass
class Stats:
    lines_emitted: int = 0
    lines_dropped: int = 0
    lines_reordered: int = 0
    lines_truncated: int = 0
    lines_garbage: int = 0
    faults_injected: int = 0
    boot_events: int = 0
    silence_events: int = 0
    max_line_bytes: int = 0

    def report(self) -> str:
        return (
            f"输出行数      {self.lines_emitted}\n"
            f"  故意丢弃    {self.lines_dropped}\n"
            f"  故意乱序    {self.lines_reordered}\n"
            f"  故意截断    {self.lines_truncated}\n"
            f"  故意乱码    {self.lines_garbage}\n"
            f"故障注入次数  {self.faults_injected}\n"
            f"重启事件      {self.boot_events}\n"
            f"日志静默      {self.silence_events} 次\n"
            f"最长行字节数  {self.max_line_bytes} / {MAX_LINE_BYTES}"
        )


# ---------------------------------------------------------------------------
# 单个逻辑设备（大棚）
# ---------------------------------------------------------------------------

@dataclass
class ZoneSim:
    zid: str
    seq: int = 0
    boot: int = 1
    t_x10: int = 250          # 温度 ×10（25.0℃）
    h_x10: int = 610          # 湿度 ×10（61.0%）
    lux: int = 800            # 相对光照度
    fail_streak: int = 0
    next_fault_at: int = 0
    scenario: str = "sensor_timeout"

    def __post_init__(self) -> None:
        self.scenario = ZONE_SCENARIO.get(self.zid, "sensor_timeout")
        # 各 zone 注入节奏错开，避免 6 个 zone 同时报错（更接近真实）
        idx = ZONES.index(self.zid)
        self.next_fault_at = 20 + idx * 3 + random.randint(0, 4)

    def tick(self, env) -> None:
        """从多大棚环境模型读取本棚当前值。

        ★ 不再用 random.randint 拍随机数 ★
        数据由 farm_model.FarmModel 派生：园区基准（一个真实传感器读数）
        + 本棚固定物理偏移（朝向/保温/土壤）+ 一阶滞后惯性 + 昼夜循环
        + 灌溉扰动 + 很小的传感器噪声。
        详见 agri_logger/parser/farm_model.py 的模块注释（含答辩话术）。
        """
        self.t_x10 = env.temp_x10
        self.h_x10 = env.humi_x10
        self.lux = env.light

    def should_inject(self) -> bool:
        """每 20~30 次上报触发一次故障（题目明确要求的行为）"""
        if self.seq >= self.next_fault_at:
            self.next_fault_at = self.seq + 20 + random.randint(0, 10)
            return True
        return False


# ---------------------------------------------------------------------------
# 输出器：自检 + 信道损伤 + ground-truth
# ---------------------------------------------------------------------------

class Emitter:
    def __init__(self, out: IO[str], gt: Optional[IO[str]], stats: Stats,
                 args: argparse.Namespace):
        self.out, self.gt, self.stats, self.args = out, gt, stats, args

    def _format(self, vtick: float, level: str, dom: str, msg: str) -> str:
        return (f"{int(vtick):06d}.{int((vtick % 1) * 1e6):06d} "
                f"{level} {DOMAIN[dom]} {TAG[dom]}: {msg}")

    def _selfcheck(self, line: str) -> None:
        if not RE_SELF_HILOG.match(line):
            raise AssertionError(f"输出违反日志规范（docs/02 §6）: {line!r}")
        nbytes = len(line.encode("utf-8")) + 2      # +2 是 \r\n
        if nbytes > MAX_LINE_BYTES:
            raise AssertionError(
                f"行长超限 {nbytes} > {MAX_LINE_BYTES}，真机 hilog_lite 会静默丢弃: {line!r}")
        self.stats.max_line_bytes = max(self.stats.max_line_bytes, nbytes)

    def emit(self, vtick: float, level: str, dom: str, msg: str,
             gt_meta: Optional[dict] = None) -> None:
        line = self._format(vtick, level, dom, msg)
        self._selfcheck(line)

        # ---- 信道损伤模拟（真机串口一定会遇到） ----
        if self.args.drop_rate and random.random() < self.args.drop_rate:
            self.stats.lines_dropped += 1
            return

        payload = line
        if self.args.truncate_rate and random.random() < self.args.truncate_rate:
            cut = random.randint(len(line) // 3, max(1, len(line) - 5))
            payload = line[:cut]
            self.stats.lines_truncated += 1
        elif self.args.garbage_rate and random.random() < self.args.garbage_rate:
            pos = random.randint(0, max(0, len(line) - 1))
            payload = line[:pos] + random.choice("\x00\x01\xff\u00ae") + line[pos + 1:]
            self.stats.lines_garbage += 1

        self.out.write(payload + "\r\n")
        self.stats.lines_emitted += 1

        if self.args.reorder_rate and random.random() < self.args.reorder_rate:
            # 乱序：把上一行再吐一遍（模拟串口缓冲区串扰），并计数
            self.out.write(payload + "\r\n")
            self.stats.lines_reordered += 1

        if self.gt is not None and gt_meta:
            self.gt.write(json.dumps({"ts": round(vtick, 3), **gt_meta},
                                     ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> Stats:
    random.seed(args.seed)
    stats = Stats()

    zones = [ZoneSim(z) for z in (args.zones.split(",") if args.zones else ZONES)]

    # 可选：强制场景（演示 / 复现用），默认仍按 zone 分工（保证评测数据集可复现）
    forced = [s.strip() for s in (getattr(args, "scenario", None) or "").split(",") if s.strip()]
    if forced:
        unknown = [s for s in forced if s not in SCENARIOS]
        if unknown:
            print("[sim] 未知场景: %s\n      可选: %s"
                  % (", ".join(unknown), ", ".join(SCENARIOS)), file=sys.stderr)
            raise SystemExit(2)
        for i, z in enumerate(zones):
            z.scenario = forced[i % len(forced)]
    out = open(args.out, "w", encoding="utf-8", newline="") if args.out else sys.stdout
    gt = open(args.label_out, "w", encoding="utf-8") if args.label_out else None
    em = Emitter(out, gt, stats, args)

    period_s = args.period                      # 上报周期（秒）
    virtual_t = 0.0                             # 设备启动后的虚拟时间（秒）
    event_id = 0
    next_sample_at = 0.0
    next_health_at = 10.0
    next_reboot_at = args.reboot_after_s
    # 日志静默：silence_next = 下一次开始静默的时刻，silent_until = 本次静默结束的时刻
    silence_next = args.silence_every
    silent_until = 0.0
    start_wall = time.time()

    # ★ 多大棚环境模型 ★
    # 现实含义：HW-01 节点上的一个真实 SHT20 读数 = 整个园区的环境基准，
    # 6 个大棚由此派生（固定物理偏移 + 惯性 + 昼夜 + 灌溉扰动）。
    # 这就是"只有一个传感器却有 6 个设备"的正当实现方式。
    farm = FarmModel(zone_ids=[z.zid for z in zones])
    BASE_T_X10, BASE_H_X10, BASE_LIGHT = 250, 610, 800

    try:
        while True:
            if args.duration and virtual_t >= args.duration:
                break

            # ---- 更新园区基准 + 驱动 6 个大棚 ----
            # 基准值的微小波动模拟"真实传感器读数本身在变"
            base_t = BASE_T_X10 + int(6 * math.sin(virtual_t / 600.0))
            base_h = BASE_H_X10 + int(10 * math.cos(virtual_t / 900.0))
            farm.update_base(base_t, base_h, BASE_LIGHT, elapsed_s=virtual_t)

            # ---- 日志静默：模拟上报任务卡死，周期日志与健康心跳一起停摆 ----
            if args.silence_every > 0 and virtual_t >= silence_next:
                silence_next = virtual_t + args.silence_every
                silent_until = virtual_t + max(args.silence_for, period_s * 3)
                stats.silence_events += 1

            # ---- 每个 zone 的周期上报 ----
            if virtual_t >= next_sample_at:
                next_sample_at += period_s
                # 静默期一条周期日志都不产生、seq 也不推进 —— 真机上"任务卡死"就是这样
                active_zones = [] if virtual_t < silent_until else zones
                for z in active_zones:
                    z.tick(farm.get(z.zid))

                    # 1) 故障注入优先
                    if z.should_inject():
                        results = SCENARIOS.get(z.scenario, [])
                        # 让注入有"持续性"：约 60% 概率连发整组，模拟一次故障事件
                        scenario_lines = results if random.random() < 0.6 else results[:1]
                        for level, dom, pairs, fault, rca, is_cascade in scenario_lines:
                            z.seq += 1
                            msg = fmt_msg(z.zid, (F_SEQ, z.seq), *pairs)
                            event_id += 1
                            em.emit(virtual_t, level, dom, msg, {
                                "event_id": f"E{event_id:05d}",
                                "zone": z.zid, "fault": fault, "level": level,
                                "scenario": z.scenario, "evidence_seq": z.seq,
                                "root_cause_class": rca, "is_cascade": is_cascade,
                                "expected_cluster": z.scenario,
                                "group_owner": ZONES.index(z.zid) + 1,
                                "severity_true": {"E": "P1", "W": "P2", "I": "P3"}[level],
                            })
                            stats.faults_injected += 1
                        z.fail_streak += 1
                        continue        # 该 tick 注入故障后不再发正常上报（更真实）

                    # 2) 正常周期上报（INFO，一行装完）
                    z.seq += 1
                    em.emit(virtual_t, "I", "sensor", fmt_msg(
                        z.zid,
                        (F_SEQ, z.seq), (F_TEMP, z.t_x10), (F_HUMI, z.h_x10),
                        (F_LIGHT, z.lux), (F_TICK, int(virtual_t * 1000))))

                    # 3) 上报动作自身（A002）
                    em.emit(virtual_t, "I", "report", fmt_msg(
                        z.zid, (F_SEQ, z.seq), (F_COST, random.randint(12, 52))))

                    # 4) 越阈值 → WARN + 灌溉（真实业务事件，用于检验误报率）
                    if z.t_x10 > args.temp_high_x10:
                        em.emit(virtual_t, "W", "sensor", fmt_msg(
                            z.zid, (F_SEQ, z.seq), (F_FAULT, 1005),
                            (F_CODE, 5), (F_TEMP, z.t_x10)))
                        em.emit(virtual_t, "I", "ctrl", fmt_msg(
                            z.zid, (F_SEQ, z.seq), (F_FAULT, 4002),
                            (F_CODE, 6), (F_STATE, 1)))

            # ---- 健康心跳（A003，全局日志用 z=-） ----
            if virtual_t >= next_health_at:
                next_health_at += 10.0
                if virtual_t >= silent_until:      # 静默期连心跳也停（整条日志通路卡死）
                    heap = 20480 - int(200 * (virtual_t / 60.0)) % 4096
                    if heap < 4096:
                        em.emit(virtual_t, "W", "health", fmt_msg(
                            "-", (F_SEQ, 0), (F_FAULT, 3006), (F_CODE, 15), (F_HEAP, heap)))
                    else:
                        em.emit(virtual_t, "I", "health", fmt_msg(
                            "-", (F_SEQ, 0), (F_HEAP, heap)))

            # ---- 设备重启（检验时间轴换算与重启检测） ----
            if args.reboot_after_s and virtual_t >= next_reboot_at:
                next_reboot_at += args.reboot_after_s
                z = random.choice(zones)
                z.boot += 1
                z.seq = 0
                stats.boot_events += 1
                em.emit(virtual_t, "I", "health", fmt_msg(
                    z.zid, (F_SEQ, 0), (F_FAULT, 5001), (F_CODE, 6)))

            # ---- 虚拟时钟推进 ----
            step = args.tick_ms / 1000.0
            virtual_t += step
            if args.speed > 0:
                time.sleep(step / args.speed)
    finally:
        if args.out:
            out.close()
        if gt:
            gt.close()

    return stats


# ---------------------------------------------------------------------------
# 自检：校验正则与行长约束（CI 用）
# ---------------------------------------------------------------------------

def selftest() -> int:
    import io
    print("=== 自检：格式规范与行长约束 ===")
    sink = io.StringIO()
    gt_sink = io.StringIO()
    ns = argparse.Namespace(drop_rate=0, reorder_rate=0, truncate_rate=0,
                            garbage_rate=0)
    em = Emitter(sink, gt_sink, Stats(), ns)
    # 跑一遍全部场景的全部行，确保没有任何一行违反规范
    n = 0
    for name, lines in SCENARIOS.items():
        for level, dom, pairs, fault, rca, cascade in lines:
            for z in ZONES:
                em.emit(123.456789, level, dom, fmt_msg(z, *pairs),
                        {"scenario": name, "zone": z})
                n += 1
    print(f"已校验 {n} 行场景日志：格式 ✅  行长 ✅")
    print(f"最长行 {em.stats.max_line_bytes} / {MAX_LINE_BYTES} 字节")
    print("样例输出：")
    for line in sink.getvalue().splitlines()[:6]:
        print("  " + line + f"   ({len(line.encode()) + 2} bytes)")

    # 额外校验：模拟器产出的行必须能被【真正的解析器】解析
    # 这是"三端一致"最有力的证明——不靠人工比对，靠代码验证
    try:
        from agri_logger.parser.parser import LogParser
        parser = LogParser()
        ok = bad = 0
        for line in sink.getvalue().splitlines():
            if parser.parse(line, src_name="selftest") is None:
                bad += 1
                print(f"  ❌ 解析器无法解析模拟器输出: {line!r}")
            else:
                ok += 1
        print(f"\n三方一致性校验：模拟器输出 {ok + bad} 行，"
              f"解析器成功 {ok} 行，失败 {bad} 行")
        if bad:
            print("❌ 模拟器与解析器不一致！检查 fields.py 与 fmt_msg()")
            return 1
        print("✅ 模拟器 / 固件字段表 / 解析器 三方一致")
    except ImportError as e:
        print(f"\n⚠️ 跳过一致性校验（无法导入解析器：{e}）")

    print("\n自检通过 ✅")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Hi3861 多任务日志模拟器（输出与真机字节级同构）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--zones", default="A,B,C,D,E,F", help="要模拟的 zone 列表")
    p.add_argument("--scenario", default=None,
                   help="强制所有 zone 使用该故障场景（逗号分隔可轮转），如 "
                        "system_crash / watchdog_reset；默认按 zone 分工")
    p.add_argument("--period", type=float, default=5.0, help="上报周期（秒，虚拟时间）")
    p.add_argument("--duration", type=float, default=0, help="运行时长（秒，0=无限）")
    p.add_argument("--speed", type=float, default=1.0, help="虚拟时间加速倍数")
    p.add_argument("--tick-ms", type=int, default=100, help="时钟推进步长（毫秒）")
    p.add_argument("--seed", type=int, default=20260101,
                   help="随机种子（固定后 6 个组拿到相同日志流）")
    p.add_argument("--temp-high-x10", type=int, default=350,
                   help="高温阈值 ×10（超过则触发 F1005 灌溉），默认 35.0℃")
    p.add_argument("--reboot-after-s", type=float, default=1800,
                   help="每隔多少虚拟秒模拟一次设备重启（0=不重启）")
    # 日志静默（非故障型异常）：模拟"上报任务卡死 → 日志彻底停摆"
    p.add_argument("--silence-every", type=float, default=0.0,
                   help="每隔多少虚拟秒制造一次【日志静默】（0=不制造）")
    p.add_argument("--silence-for", type=float, default=15.0,
                   help="每次静默持续多少秒（需 > 2×上报周期 才会被判为静默异常）")
    p.add_argument("--out", default=None, help="输出文件（默认 stdout）")
    p.add_argument("--label-out", default=None,
                   help="ground-truth 标注文件（JSONL，评测用）")
    # 信道损伤
    p.add_argument("--drop-rate", type=float, default=0.0, help="丢弃行的概率")
    p.add_argument("--reorder-rate", type=float, default=0.0, help="重复/乱序的概率")
    p.add_argument("--truncate-rate", type=float, default=0.0, help="截断行的概率")
    p.add_argument("--garbage-rate", type=float, default=0.0, help="注入乱码的概率")
    p.add_argument("--selftest", action="store_true", help="仅运行自检后退出")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    # Windows 控制台默认 GBK，中文与符号会报 UnicodeEncodeError；统一改 UTF-8
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)

    if args.selftest:
        return selftest()

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    if args.label_out:
        Path(args.label_out).parent.mkdir(parents=True, exist_ok=True)

    print(f"[sim] zones={args.zones} period={args.period}s "
          f"duration={args.duration or '∞'}s speed={args.speed}x seed={args.seed}",
          file=sys.stderr)
    stats = run(args)
    print("\n=== 模拟结束统计 ===", file=sys.stderr)
    print(stats.report(), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
