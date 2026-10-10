#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""滑动窗口聚合 —— 把原始日志流压成"可送 LLM 的摘要"

================================================================================
为什么必须做窗口聚合（而不是把日志直接丢给 LLM）

  30 分钟日志 = 4475 行 ≈ 90,000 token
  → 每分析一次 ~0.09 元，而且模型会被大量正常日志淹没，抓不住重点

  压成窗口后：
  4475 行 → 60 个窗口 → 只有【异常窗口】送 LLM（约 8 个）
  → 每个窗口摘要 ~400 token，总计 3,200 token，成本降 97%

  关键：这不是"省钱的妥协"，而是【提高准确率】的手段——
  模型看统计摘要比看几千行流水更容易发现"任务卡死"这类模式。
================================================================================

窗口统计里最有价值的信号是【日志到达间隔】：
  正常每 5 秒一轮 → 均值 ~0.6 秒（6 个 zone 交错）
  任务卡死时 → 最大间隔突增到 10 秒以上，且 ERROR 反而不多
  纯关键词方案会 100% 漏掉这类异常。
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from ..parser.parser import LEVEL_ORDER, LogRecord
from ..parser import fields as F


@dataclass
class WindowStats:
    """一个窗口的统计摘要 —— 这就是送 LLM 的东西"""

    window_id: str
    start_ts: float = 0.0            # 设备时间（秒）
    end_ts: float = 0.0
    records: list[LogRecord] = field(default_factory=list)

    # ---- 基础计数 ----
    line_count: int = 0
    level_count: Counter = field(default_factory=Counter)
    domain_count: Counter = field(default_factory=Counter)
    zone_count: Counter = field(default_factory=Counter)
    fault_count: Counter = field(default_factory=Counter)      # fault_tag -> 次数
    code_count: Counter = field(default_factory=Counter)

    # ---- 时序特征（发现"日志静默"的关键）----
    gaps: list[float] = field(default_factory=list)

    # ---- seq 缺口（发现丢日志）----
    seq_gaps: dict[str, int] = field(default_factory=dict)

    # ---- 派生 ----
    anomaly_score: float = 0.0
    anomaly_reasons: list[str] = field(default_factory=list)
    sent_to_llm: bool = False

    # ---------------- 属性 ----------------
    @property
    def err_count(self) -> int:
        return self.level_count.get("E", 0) + self.level_count.get("F", 0)

    @property
    def warn_count(self) -> int:
        return self.level_count.get("W", 0)

    @property
    def info_count(self) -> int:
        return self.level_count.get("I", 0)

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_ts - self.start_ts)

    @property
    def zones(self) -> list[str]:
        return sorted(self.zone_count.keys())

    @property
    def gap_mean(self) -> float:
        return statistics.mean(self.gaps) if self.gaps else 0.0

    @property
    def gap_max(self) -> float:
        return max(self.gaps) if self.gaps else 0.0

    @property
    def seq_gap_total(self) -> int:
        return sum(self.seq_gaps.values())

    # ---------------- 输出 ----------------
    def fault_lines(self) -> list[LogRecord]:
        """所有带故障码的记录"""
        return [r for r in self.records if r.fault_tag]

    def error_lines(self) -> list[LogRecord]:
        return [r for r in self.records if r.level in ("E", "F")]

    def evidence(self, max_lines: int = 25, min_level: str = "W") -> list[LogRecord]:
        """挑出代表性日志作为证据，供 LLM 引用（带 evidence_id）

        优先级：故障行 > ERROR > WARN > 阈值附近的 INFO
        """
        picked: list[LogRecord] = []
        seen: set[str] = set()

        for r in self.records:
            if r.fault_tag and r.evidence_id not in seen:
                picked.append(r); seen.add(r.evidence_id)

        for r in self.records:
            if r.level in ("E", "F") and r.evidence_id not in seen:
                picked.append(r); seen.add(r.evidence_id)

        if LEVEL_ORDER.get(min_level, 2) <= 2:
            for r in self.records:
                if r.level == "W" and r.evidence_id not in seen:
                    picked.append(r); seen.add(r.evidence_id)

        return picked[:max_lines]

    def to_prompt_text(self) -> str:
        """转成给 LLM 看的文本（**只放统计摘要，不放原始流水**）"""
        lines = [
            f"窗口: {self.window_id}",
            f"时间范围(设备相对时间): {self.start_ts:.1f}s ~ {self.end_ts:.1f}s（时长 {self.duration_s:.0f}s）",
            f"总行数: {self.line_count}  ERROR: {self.err_count}  WARN: {self.warn_count}  INFO: {self.info_count}",
            f"日志到达间隔: 均值 {self.gap_mean:.2f}s  最大 {self.gap_max:.2f}s"
            + ("   ← 异常：明显大于正常上报周期" if self.gap_max > 8 else ""),
            f"seq 缺口: {self.seq_gap_total} 条" + (f"  明细 {dict(self.seq_gaps)}" if self.seq_gaps else ""),
            f"涉及设备: {', '.join(self.zones) if self.zones else '无'}",
        ]
        if self.fault_count:
            faults = ", ".join(f"{tag}×{n}({F.fault_name(int(tag[1:]))})"
                               for tag, n in self.fault_count.most_common())
            lines.append(f"故障码统计: {faults}")
        if self.code_count:
            codes = ", ".join(f"{F.error_phrase(int(c))}×{n}"
                              for c, n in self.code_count.most_common())
            lines.append(f"错误码统计: {codes}")
        if self.domain_count:
            lines.append("模块分布: " + ", ".join(
                f"{d}({F.DOMAINS.get(d, '?')})×{n}" for d, n in self.domain_count.most_common()))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 聚合器
# ---------------------------------------------------------------------------

class WindowAggregator:
    """固定窗口聚合。窗口按【逻辑时间】切分（设备时间），便于与数据集对齐。"""

    def __init__(self, window_s: float = 60.0):
        self.window_s = window_s
        self._cur: Optional[WindowStats] = None
        self._done: list[WindowStats] = []
        self._last_ts: Optional[float] = None
        self._expect_seq: dict[str, int] = {}
        self._widx = 0

    def feed(self, rec: LogRecord) -> None:
        ts = rec.ts_device if rec.ts_device is not None else rec.ts_host
        if self._cur is None:
            self._new_window(ts)

        # 窗口边界：跨窗则收尾并开新窗
        while ts - self._cur.start_ts >= self.window_s:
            self._close_current()
            self._new_window(self._cur.end_ts if self._cur else ts)

        w = self._cur
        w.records.append(rec)
        w.line_count += 1
        w.end_ts = ts
        w.level_count[rec.level] += 1
        w.domain_count[rec.domain] += 1
        if rec.zone:
            w.zone_count[rec.zone] += 1
            # seq 连续性
            exp = self._expect_seq.get(rec.zone)
            if rec.seq is not None:
                if exp is not None and rec.seq > exp:
                    w.seq_gaps[rec.zone] = w.seq_gaps.get(rec.zone, 0) + (rec.seq - exp)
                self._expect_seq[rec.zone] = rec.seq + 1
        if rec.fault_tag:
            w.fault_count[rec.fault_tag] += 1
        if rec.code is not None:
            w.code_count[str(rec.code)] += 1

        # 到达间隔（用设备时间，避免主机抖动干扰）
        if self._last_ts is not None:
            gap = ts - self._last_ts
            if 0 <= gap < 3600:
                w.gaps.append(gap)
        self._last_ts = ts

    def _new_window(self, start_ts: float) -> None:
        self._widx += 1
        self._cur = WindowStats(window_id=f"W{self._widx:04d}", start_ts=start_ts, end_ts=start_ts)

    def _close_current(self) -> None:
        if self._cur is not None and self._cur.line_count > 0:
            self._done.append(self._cur)
        self._cur = None

    def finish(self) -> list[WindowStats]:
        self._close_current()
        return self._done


# ---------------------------------------------------------------------------
# 异常打分 —— 多信号融合（不只看 ERROR 数量）
# ---------------------------------------------------------------------------

WEIGHTS = {
    "level":     0.30,   # ERROR/FATAL 占比
    "fault":     0.25,   # 命中故障码（且排除"正常事件"）
    "silence":   0.25,   # ★ 日志静默：最大间隔远超正常周期
    "seqgap":    0.10,   # seq 缺口 = 丢数据
    "threshold": 0.10,   # 数值越限
}

# 正常上报周期（秒）：6 个 zone 交错，所以正常最大间隔约等于周期
NORMAL_PERIOD_S = 5.0


def score_window(w: WindowStats) -> WindowStats:
    """给窗口打异常分，并给出可解释的原因列表。

    这是本项目与"关键词告警"的核心区别：
      · 关键词只看文本 → 漏掉"日志静默"这类无 ERROR 的异常
      · 多信号融合能看到"少了日志"这件事
    """
    parts: dict[str, float] = {}
    reasons: list[str] = []

    n = max(1, w.line_count)

    # 1) 级别
    p = min(1.0, (w.err_count / n) * 8.0)
    if w.err_count:
        reasons.append(f"含 {w.err_count} 条 ERROR/FATAL")
    parts["level"] = p * WEIGHTS["level"]

    # 2) 故障码（排除正常运维事件，这是误报率的关键）
    abnormal = 0
    for tag, cnt in w.fault_count.items():
        code = int(tag[1:])
        if not F.is_normal_event(code):
            abnormal += cnt
    p = min(1.0, abnormal / 2.0)
    if abnormal:
        names = [f"{t}({F.fault_name(int(t[1:]))})"
                 for t in w.fault_count if not F.is_normal_event(int(t[1:]))]
        reasons.append("异常故障码: " + ", ".join(names))
    parts["fault"] = p * WEIGHTS["fault"]

    # 3) ★ 日志静默 —— 最容易被遗漏的信号
    p = 0.0
    if w.gap_max > NORMAL_PERIOD_S * 2:
        p = min(1.0, (w.gap_max - NORMAL_PERIOD_S * 2) / (NORMAL_PERIOD_S * 4))
        reasons.append(f"日志静默: 最大间隔 {w.gap_max:.1f}s（正常约 {NORMAL_PERIOD_S:.0f}s）")
    parts["silence"] = p * WEIGHTS["silence"]

    # 4) seq 缺口
    p = min(1.0, w.seq_gap_total / 5.0)
    if w.seq_gap_total:
        reasons.append(f"seq 缺口 {w.seq_gap_total} 条（疑似丢日志）")
    parts["seqgap"] = p * WEIGHTS["seqgap"]

    # 5) 数值越限（读数不合理）
    p = 0.0
    for r in w.records:
        t = r.temp
        h = r.humi
        if t is not None and (t < -20 or t > 80):
            p = 1.0; reasons.append(f"温度读数不合理: {t}℃"); break
        if h is not None and (h < 10 or h > 98):
            p = 1.0; reasons.append(f"湿度读数不合理: {h}%"); break
    parts["threshold"] = p * WEIGHTS["threshold"]

    w.anomaly_score = round(sum(parts.values()), 4)
    w.anomaly_reasons = reasons
    return w


def aggregate_and_score(records: list[LogRecord], window_s: float = 60.0,
                        threshold: float = 0.25) -> tuple[list[WindowStats], list[WindowStats]]:
    """入口：聚合 + 打分。返回 (全部窗口, 异常窗口)"""
    agg = WindowAggregator(window_s=window_s)
    for r in records:
        agg.feed(r)
    windows = [score_window(w) for w in agg.finish()]
    anomalies = [w for w in windows if w.anomaly_score >= threshold]
    return windows, anomalies
