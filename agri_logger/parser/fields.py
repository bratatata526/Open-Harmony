#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志字段表 —— 设备端与解析端的【唯一】共享定义

============================================================================
这是 docs/02-log-spec.md 的可执行版本。
设备端（firmware/hi3861/agri_logger/log_def.h）、模拟器、解析器三方都以此为准。
改这张表必须同时改：① log_def.h 的 agri_field_t ② docs/02 §3.3 ③ 这里。
============================================================================

消息体格式（设备端输出）：
    <字段ID> <值> <字段ID> <值> ... z=<zone>

例：
    000120.456789 I A001 AgriSensor: 1 24 2 281 3 466 7 120456 z=A

为什么是"数字ID + 空格 + 数值"：
  · hilog_lite 不支持 %s → 值必须是数字
  · 只有空格和数字 → 解析只需 split(' ')，规则最简单
  · ID 让字段【顺序无关】，加字段不影响老解析器
  · 1~2 位数字 ID 的成本极低，却换来人眼可读
"""

from __future__ import annotations

from typing import NamedTuple

# ---------------------------------------------------------------------------
# 字段定义
# ---------------------------------------------------------------------------

class FieldDef(NamedTuple):
    fid: int          # 协议里的数字 ID
    name: str         # 解析后的字段名（写进 LogRecord）
    scale: float      # 还原倍数：物理值 = 原始值 / scale
    unit: str         # 单位（用于报告展示）
    desc: str         # 中文说明


FIELDS: dict[int, FieldDef] = {f.fid: f for f in [
    FieldDef(1,  "seq",     1,   "",    "该 zone 内单调递增序号，丢包检测依据"),
    FieldDef(2,  "temp",    10,  "℃",  "温度，原始值为实际值×10"),
    FieldDef(3,  "humi",    10,  "%",   "湿度，原始值为实际值×10"),
    FieldDef(4,  "light",   1,   "",    "光照相对值 0~1000"),
    FieldDef(5,  "fault",   1,   "",    "故障码数值，见 FAULTS"),
    FieldDef(6,  "code",    1,   "",    "错误码，见 ERROR_CODES"),
    FieldDef(7,  "tick",    1,   "ms",  "系统 tick"),
    FieldDef(8,  "heap",    1,   "B",   "剩余堆内存"),
    FieldDef(9,  "cost",    1,   "ms",  "耗时"),
    FieldDef(10, "retry",   1,   "",    "重试次数 / 连续失败次数"),
    FieldDef(11, "state",   1,   "",    "状态机：0=idle 1=irrigating 2=venting 3=degraded"),
    FieldDef(12, "thresh",  10,  "",    "阈值，原始值为实际值×10"),
    FieldDef(13, "rssi",    1,   "dBm", "WiFi 信号强度（存正值，实际为负）"),
    FieldDef(14, "queue",   1,   "",    "消息队列长度"),
    FieldDef(15, "miss",    1,   "",    "看门狗漏喂次数"),
    FieldDef(16, "zone_id", 1,   "",    "逻辑设备编号 0=A 1=B ... 5=F"),
]}

# 状态机编码 → 可读短语（解析时还原）
STATE_NAMES = {0: "idle", 1: "irrigating", 2: "venting", 3: "degraded"}

# ---------------------------------------------------------------------------
# 故障码（对应 docs/02 §5.2 的 Fxxxx）
# ---------------------------------------------------------------------------

class FaultDef(NamedTuple):
    code: int         # 协议里的数值（如 1002）
    tag: str          # 展示用（如 "F1002"）
    name: str         # 名称
    level: str        # 期望级别
    domain: str       # 期望 domain
    is_normal: bool   # True = 正常业务事件，不算异常（用于控制误报）


FAULTS: dict[int, FaultDef] = {f.code: f for f in [
    FaultDef(1001, "F1001", "传感器读取超时",   "E", "A001", False),
    FaultDef(1002, "F1002", "单总线复位无应答", "E", "A001", False),
    FaultDef(1003, "F1003", "数据校验失败",     "E", "A001", False),
    FaultDef(1004, "F1004", "读数超出合理范围", "W", "A001", False),
    FaultDef(1005, "F1005", "数值越阈值",       "W", "A001", False),
    FaultDef(1006, "F1006", "传感器离线",       "E", "A001", False),
    FaultDef(2001, "F2001", "MQTT 连接失败",    "E", "A006", False),
    FaultDef(2002, "F2002", "上报超时",         "E", "A002", False),
    FaultDef(2003, "F2003", "WiFi 信号弱",      "W", "A006", False),
    FaultDef(2004, "F2004", "断线重连",         "W", "A006", True),   # 正常事件
    FaultDef(2005, "F2005", "数据帧校验错",     "E", "A006", False),
    FaultDef(3001, "F3001", "内存分配失败",     "E", "A003", False),
    FaultDef(3002, "F3002", "任务栈溢出",       "E", "A003", False),
    FaultDef(3003, "F3003", "任务调度超时",     "E", "A005", False),
    FaultDef(3004, "F3004", "队列积压",         "W", "A005", False),
    FaultDef(3005, "F3005", "看门狗即将触发",   "E", "A003", False),
    FaultDef(3006, "F3006", "空闲堆不足",       "W", "A003", False),
    FaultDef(4001, "F4001", "执行器超时",       "W", "A004", False),
    FaultDef(4002, "F4002", "灌溉状态变更",     "I", "A004", True),   # 正常事件
    FaultDef(5001, "F5001", "设备启动",         "I", "A003", True),   # 正常事件
    FaultDef(5002, "F5002", "配置重载失败",     "W", "A003", False),
]}

# ---------------------------------------------------------------------------
# 错误码（对应 docs/02 §5.4）—— 把 c=1 还原成人能读的短语
# ---------------------------------------------------------------------------

ERROR_CODES: dict[int, str] = {
    0:  "none",
    1:  "reset_timeout",
    2:  "crc_mismatch",
    3:  "out_of_range",
    4:  "no_response_x5",
    5:  "threshold_exceed",
    6:  "state_change",
    7:  "publish_timeout",
    8:  "rssi_low",
    9:  "reconnect",
    10: "stack_overflow",
    11: "task_hung",
    12: "alloc_failed",
    13: "queue_full",
    14: "wdt_feed_missed",
    15: "heap_low",
    16: "config_load_failed",
    99: "unknown",
}

# domain → 模块用途（用于报告与因果关联）
DOMAINS: dict[str, str] = {
    "A001": "环境传感",
    "A002": "数据上报",
    "A003": "设备自检",
    "A004": "执行器控制",
    "A005": "任务调度",
    "A006": "配网与通信",
    "A007": "故障注入器",
}


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------

def field_name(fid: int) -> str:
    fd = FIELDS.get(fid)
    return fd.name if fd else f"f{fid}"


def field_scale(fid: int) -> float:
    fd = FIELDS.get(fid)
    return fd.scale if fd else 1.0


def fault_tag(code: int) -> str:
    fd = FAULTS.get(code)
    return fd.tag if fd else f"F{code}"


def fault_name(code: int) -> str:
    fd = FAULTS.get(code)
    return fd.name if fd else "未知故障"


def is_normal_event(code: int) -> bool:
    """该故障码是否属于"正常业务事件"（用于抑制误报）。

    ★ 这是误报率的关键：F2004 断线重连、F4002 灌溉状态变更、F5001 设备启动
      都是【正常运维事件】，必须排除在异常之外。不做这一步，误报率会显著偏高。
    """
    fd = FAULTS.get(code)
    return fd.is_normal if fd else False


def error_phrase(code: int) -> str:
    return ERROR_CODES.get(code, "unknown")


def format_physical(fid: int, raw: int) -> str:
    """把原始值格式化成带单位的可读形式，如 (2, 281) → '28.1℃'"""
    fd = FIELDS.get(fid)
    if fd is None:
        return str(raw)
    if fd.scale == 1:
        val = str(raw)
    else:
        val = f"{raw / fd.scale:.1f}"
    return f"{val}{fd.unit}" if fd.unit else val
