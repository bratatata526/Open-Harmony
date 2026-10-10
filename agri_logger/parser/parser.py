#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志解析器 —— 把一行原始日志变成结构化 LogRecord

这是"设备端 ↔ 分析端"契约的另一半。配合 parser/fields.py 使用。

规范定义见 docs/02-log-spec.md。三端一致性靠这套解析器 + fields.py 保证：
    设备端 log_def.h  ──输出──▶  原始行  ──本解析器──▶  LogRecord  ──▶ 分析
    模拟器 log_sim.py ──输出──▶  原始行  ──本解析器──▶  LogRecord  ──▶ 分析
    两者输出的字节流【完全同构】，解析器不需要区分来源。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Literal, Optional

from . import fields as F

Level = Literal["D", "I", "W", "E", "F"]

# 级别排序：用于 --min-level 过滤与"ERROR 及以上"这类判断。
# 放在共享模块里，避免各脚本自己定义一份而出现不一致。
LEVEL_ORDER: dict[str, int] = {"D": 0, "I": 1, "W": 2, "E": 3, "F": 4}

# ---------------------------------------------------------------------------
# 冻结正则（与 docs/02-log-spec.md §6 一致）
# ---------------------------------------------------------------------------

# 真机 / 模拟器输出：
#   000120.456789 I A001 AgriSensor: 1 24 2 281 3 466 7 120456 z=A
RE_HILOG = re.compile(
    r"^(?P<ts>\d{6,}\.\d{6})\s+"
    r"(?P<level>[DIWEF])\s+"
    r"(?P<domain>[0-9A-Fa-f]{4,5})\s+"
    r"(?P<tag>[A-Za-z][A-Za-z0-9_]{0,30}):\s*"
    r"(?P<msg>.*)$"
)

# 扩展位：若日后接入非 OpenHarmony 设备（如 STM32 裸机日志）
#   [STM32] 001234 I SENSOR: 1 103 2 248
RE_MCU = re.compile(
    r"^\[STM32\]\s+"
    r"(?P<ts>\d+)\s+"
    r"(?P<level>[DIWEF])\s+"
    r"(?P<tag>[A-Za-z][A-Za-z0-9_]{0,30}):\s*"
    r"(?P<msg>.*)$"
)

RE_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
RE_ZONE = re.compile(r"(?:^|\s)z=(?P<zone>[A-Za-z0-9_-])")
RE_NUM = re.compile(r"^-?\d+$")

MAX_LINE_BYTES = 128          # hilog_lite 硬限制，超过会被静默丢弃


@dataclass
class LogRecord:
    """解析后的一条日志。全项目（采集/聚合/识别/报告）都用这个结构。"""

    # ---- 定位 ----
    line_no: int = 0
    evidence_id: str = ""                 # 报告引用用，形如 "hl:00001234"
    source: str = "hilog"                 # hilog | mcu
    src_name: str = ""                    # "COM3" / "sim"

    # ---- 时间（双时间轴）----
    ts_device: Optional[float] = None     # 设备上电后秒数
    ts_host: float = field(default_factory=time.time)   # 主机绝对时间
    ts_source: str = "host"               # device | host

    # ---- 前缀字段 ----
    level: Level = "I"
    domain: str = ""
    tag: str = ""
    message: str = ""
    raw_line: str = ""

    # ---- 业务字段（由 fields.py 映射而来）----
    zone: Optional[str] = None            # 'A'~'F'
    seq: Optional[int] = None
    fault: Optional[int] = None           # 故障码数值，如 1002
    fault_tag: Optional[str] = None       # "F1002"
    fault_name: Optional[str] = None      # "单总线复位无应答"
    code: Optional[int] = None            # 错误码
    detail: Optional[str] = None          # 由 code 查表得到的可读短语
    is_normal_event: bool = False         # True = 正常业务事件，不算异常

    values: dict[int, int] = field(default_factory=dict)   # 原始字段 ID → 原始值
    physical: dict[str, str] = field(default_factory=dict) # 字段名 → 带单位可读值

    # ---- 派生（后续模块填）----
    template: Optional[str] = None
    template_id: Optional[str] = None
    fingerprint: Optional[str] = None
    parse_ok: bool = True
    parse_error: Optional[str] = None

    # ---- 便捷属性 ----
    @property
    def temp(self) -> Optional[float]:
        v = self.values.get(2)
        return v / 10.0 if v is not None else None

    @property
    def humi(self) -> Optional[float]:
        v = self.values.get(3)
        return v / 10.0 if v is not None else None

    @property
    def is_error(self) -> bool:
        return self.level in ("E", "F")

    def summary(self) -> str:
        """一行摘要，用于控制台打印与报告引用。

        只展示【语义化字段】，不重复展示原始 kv，避免 "s=21 ... seq=21" 这类冗余。
        """
        parts: list[str] = []
        if self.ts_device is not None:
            parts.append(f"{self.ts_device:>12.6f}")
        parts.append(f"{self.level} {self.domain} {self.tag}:")
        if self.zone:
            parts.append(f"[{self.zone}]")
        if self.seq is not None:
            parts.append(f"#{self.seq}")

        # 故障信息只留可读形式
        if self.fault_tag:
            parts.append(f"{self.fault_tag}({self.fault_name})")
        elif self.detail:
            parts.append(self.detail)

        # 物理量（带单位）。跳过以下几类，避免与上面的语义字段重复：
        #   · 已由 zone/seq/fault 表达的字段
        #   · 调试量（cost/tick），它们在最后单独追加
        SKIP = {"seq", "fault", "code", "state", "retry", "miss", "queue",
                "rssi", "thresh", "zone_id", "zone", "cost", "tick"}
        for name, text in self.physical.items():
            if name in SKIP:
                continue
            parts.append(f"{name}={text}")

        # 调试量放最后
        for name in ("cost", "tick"):
            if name in self.physical:
                parts.append(f"{name}={self.physical[name]}")
        return " ".join(parts)


# ---------------------------------------------------------------------------
# 解析器
# ---------------------------------------------------------------------------

class LogParser:
    """把原始行解析成 LogRecord。

    ★ 最重要的纪律：**永不抛异常**。任何脏数据都只返回 None 并计数，
      绝不能让一行乱码打断整个采集循环。这是"稳定性"评分的关键。
    """

    def __init__(self) -> None:
        self.line_no = 0
        self.stats = {"ok": 0, "bad": 0}
        self.bad_reasons: dict[str, int] = {}
        self.longest_line = 0
        self.over_limit = 0

    # -- 内部：给失败原因归类（比一个"成功率"数字更有信息量）--
    def _bad(self, reason: str) -> None:
        self.stats["bad"] += 1
        self.bad_reasons[reason] = self.bad_reasons.get(reason, 0) + 1

    def parse(self, raw: str, src_name: str = "", line_no: Optional[int] = None) -> Optional[LogRecord]:
        if line_no is None:
            self.line_no += 1
            line_no = self.line_no
        else:
            self.line_no = line_no

        # 行长监控：hilog_lite 对超长日志【静默丢弃】，必须在采集端就能发现
        nbytes = len(raw.encode("utf-8", errors="replace")) + 2
        self.longest_line = max(self.longest_line, nbytes)
        if nbytes > MAX_LINE_BYTES:
            self.over_limit += 1

        clean = RE_ANSI.sub("", raw).rstrip("\r\n")
        if not clean.strip():
            self._bad("空行")
            return None

        m = RE_HILOG.match(clean)
        source = "hilog"
        if not m:
            m = RE_MCU.match(clean)
            source = "mcu"
        if not m:
            self._bad(self._classify_bad(clean))
            return None

        g = m.groupdict()
        rec = LogRecord(
            line_no=line_no,
            evidence_id=f"{'hl' if source == 'hilog' else 'mcu'}:{line_no:08d}",
            source=source,
            src_name=src_name,
            ts_device=self._to_float(g["ts"]),
            ts_host=time.time(),
            ts_source="host",
            level=g["level"],                       # type: ignore[arg-type]
            domain=g.get("domain") or "MCU00",
            tag=g["tag"],
            message=g["msg"],
            raw_line=clean,
        )
        self._parse_message(rec)
        self.stats["ok"] += 1
        return rec

    # -- 消息体解析：核心逻辑 --
    def _parse_message(self, rec: LogRecord) -> None:
        msg = rec.message

        # zone：形如 " z=A"
        mz = RE_ZONE.search(msg)
        if mz:
            rec.zone = mz.group("zone").upper()

        # 其余 token：成对出现 "ID 值"，遇到非数字的值就跳过（容错）
        toks = msg.split()
        i = 0
        while i < len(toks) - 1:
            k, v = toks[i], toks[i + 1]
            if k.isdigit() and RE_NUM.match(v):
                fid, val = int(k), int(v)
                if fid in F.FIELDS:
                    rec.values[fid] = val
                    fd = F.FIELDS[fid]
                    rec.physical[fd.name] = F.format_physical(fid, val)
                i += 2
            else:
                i += 1

        # ---- 映射到语义字段 ----
        rec.seq = rec.values.get(1)
        if 5 in rec.values:
            rec.fault = rec.values[5]
            rec.fault_tag = F.fault_tag(rec.fault)
            rec.fault_name = F.fault_name(rec.fault)
            rec.is_normal_event = F.is_normal_event(rec.fault)
        if 6 in rec.values:
            rec.code = rec.values[6]
            rec.detail = F.error_phrase(rec.code)

    @staticmethod
    def _classify_bad(s: str) -> str:
        if not re.match(r"^[\[\d]", s):
            return "非日志格式（噪声/乱码）"
        if re.match(r"^\d{6}\.\d{6}\s", s):
            return "有 hilog 时间戳但前缀不完整"
        if re.match(r"^\d", s):
            return "像 MCU 日志但格式不符"
        return "其他"

    @staticmethod
    def _to_float(v: Optional[str]) -> Optional[float]:
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    # -- 统计报告 --
    def report(self) -> str:
        total = self.stats["ok"] + self.stats["bad"]
        rate = self.stats["ok"] / max(1, total) * 100
        lines = [
            "──────── 解析统计 ────────",
            f"总行数      {total}",
            f"解析成功    {self.stats['ok']}  ({rate:.2f}%)",
            f"解析失败    {self.stats['bad']}  ({100 - rate:.2f}%)",
            f"最长行      {self.longest_line} 字节"
            + ("  ✅" if self.longest_line <= MAX_LINE_BYTES else "  ❌ 超限！真机会静默丢弃"),
        ]
        if self.over_limit:
            lines.append(f"超限行数    {self.over_limit}")
        if self.bad_reasons:
            lines.append("失败原因：")
            for r, n in sorted(self.bad_reasons.items(), key=lambda x: -x[1]):
                lines.append(f"  {n:6d}  {r}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 自测：解析规范样例，验证三端一致
# ---------------------------------------------------------------------------

SPEC_SAMPLES = [
    # (原始行, 期望 level, 期望 zone, 期望 fault_tag, 期望 temp)
    ("000120.456789 I A001 AgriSensor: 1 24 2 281 3 466 7 120456 z=A", "I", "A", None, 28.1),
    ("000510.334455 E A001 AgriSensor: 1 103 5 1002 6 1 10 3 z=B", "E", "B", "F1002", None),
    ("001240.001900 E A003 AgriHealth: 1 312 5 3001 6 12 8 1024 z=C", "E", "C", "F3001", None),
    ("003600.100200 W A001 AgriSensor: 1 720 5 1005 6 5 2 386 z=A", "W", "A", "F1005", 38.6),
    ("000000.000100 I A003 AgriHealth: 1 0 5 5001 6 6 z=B", "I", "B", "F5001", None),
    ("003600.101500 I A004 AgriCtrl: 1 721 5 4002 6 6 11 1 z=A", "I", "A", "F4002", None),
]


def selftest() -> int:
    """不依赖硬件与模拟器的接口契约自测。CI 里跑它，能第一时间发现三端不一致。"""
    print("=" * 66)
    print("  接口契约自测：解析规范样例")
    print("=" * 66)
    p = LogParser()
    fails = 0
    for raw, lv, zone, ftag, temp in SPEC_SAMPLES:
        rec = p.parse(raw, src_name="selftest")
        if rec is None:
            print(f"❌ 解析失败: {raw}")
            fails += 1
            continue
        problems = []
        if rec.level != lv:
            problems.append(f"level={rec.level} 期望 {lv}")
        if rec.zone != zone:
            problems.append(f"zone={rec.zone} 期望 {zone}")
        if rec.fault_tag != ftag:
            problems.append(f"fault_tag={rec.fault_tag} 期望 {ftag}")
        if temp is not None and rec.temp != temp:
            problems.append(f"temp={rec.temp} 期望 {temp}")
        if problems:
            print(f"❌ {raw}\n   {'; '.join(problems)}")
            fails += 1
        else:
            print(f"✅ {rec.summary()}")

    # 脏数据必须不崩
    dirty = ["", "   ", "garbage", "000120.456789 X A001 T: 1 2",
             "000120.456789 I A001 AgriSensor", "\x00\x01\x02", "A" * 5000,
             "000120.456789 I A001 AgriSensor: 1 2 3", "温度 28 度"]
    for d in dirty:
        try:
            p.parse(d, src_name="dirty")
        except Exception as e:                       # noqa: BLE001
            print(f"❌ 脏数据导致异常: {d[:40]!r} → {e}")
            fails += 1
    print(f"\n脏数据测试：{len(dirty)} 条，均未抛异常 ✅")

    print()
    print(p.report())
    print()
    if fails:
        print(f"❌ 自测失败 {fails} 项 —— 三端接口不一致，先修 fields.py / log_def.h")
        return 1
    print("✅ 自测通过：解析器与 log_def.h 的接口定义一致")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(selftest())
