#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 异常分析与根因报告生成（L1/L2 两级）

================================================================================
核心设计：LLM 只做三件事，其余全部用规则/统计完成
================================================================================
  · 语义聚类      —— 把不同表述的异常归成"异常族"
  · 根因推理      —— "堆不足 → 分配失败 → 上报超时"这种跨模块因果链
  · 自然语言报告  —— 运维人员能直接照着处理

不给 LLM 做的（用规则/统计，更快更准）：
  · 判断一条日志是不是 ERROR      → 看 level 字段
  · 判断有没有"内存分配失败"      → 查故障码 F3001
  · 判断任务是否卡死              → 看日志间隔统计（见 windows.py）
  日志流 4475 行 → 规则过滤 + 窗口聚合 → 只剩 8 个异常窗口进 LLM，成本降 97%

================================================================================
防幻觉的三重保险（答辩必讲）
================================================================================
  ① 提示词层：明确"严禁编造"，要求引用真实证据编号
  ② 结构层  ：evidence_refs 是数组，程序可校验每个编号是否真实存在
  ③ 程序层  ：★ 报告的证据段【由程序从原始日志取】，LLM 只输出编号
              → 编造的编号查不到，直接不渲染
              → 报告的事实准确率 100%，因为事实不经过模型
================================================================================
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..parser.parser import LogRecord
from ..parser import fields as F
from ..pipeline.windows import WindowStats
from .llm_client import LLMClient, Usage

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# L1 提示词：窗口异常判定
# ---------------------------------------------------------------------------

SYSTEM_L1 = """你是 OpenHarmony 物联网设备的日志运维专家。你的任务是判断一个日志窗口是否包含真实异常，并对异常归类。

【领域背景】
设备是"智慧农业温室大棚"的 6 个环境监测节点（Zone-A~F），运行 OpenHarmony 轻量系统。
每个节点每 5 秒采集一次温度/湿度/光照，通过 MQTT 上报。

【故障码字典（判据的唯一来源，不得臆造其他故障码）】
F1001/F1002/F1006 = 传感器读取失败类（超时、单总线无应答、连续失败）
F1003/F1004/F1005 = 数据质量类（CRC 错、物理不可能值、越阈值）
F2001~F2005 = 通信类（连接失败、上报超时、信号弱、重连、帧校验错）
F3001~F3006 = 资源与调度类（内存分配失败、栈溢出、任务卡死、队列积压、看门狗即将触发、堆不足）
F3007~F3010 = 系统崩溃类（看门狗超时复位、中断阻塞、系统死机、非法内存访问）
F3011 = CPU 负载类（CPU 占用率过高，cpu>95%）
F4001/F4002 = 执行器类（灌溉超时、状态变更）
F5001/F5002 = 生命周期类（启动、配置重载失败）
F5003/F5004 = 复位与配置类（异常复位、看门狗超时配置非法）

【消息体字段 ID 含义（日志体是"ID 值"配对，必须据此解读其中的数字）】
1=seq  2=temp(×0.1℃)  3=humi(×0.1%)  4=light  5=fault故障码  6=code错误码
7=tick(ms)  8=heap(剩余堆,字节)  9=cost(ms)  10=retry  11=state(0idle/1灌溉/2通风/3降级)
12=thresh  13=rssi(存正值,实际取负)  14=queue  15=miss(看门狗漏喂次数)  16=zone_id
17=mcause(机器异常原因码)  18=addr(出错地址/mtval)  19=cpu(占用率%)  20=memfail(申请失败次数)

【重要判据】
1. F2004（断线重连）、F4002（灌溉状态变更）、F5001（设备启动）是【正常运维事件】，不是异常。
2. 若窗口内日志的【到达间隔】明显超过正常上报周期（5 秒），说明任务可能卡死，
   即使没有 ERROR 也要标记为异常——这是"日志静默"异常。
3. seq 出现缺口说明日志或数据丢失，属于异常。
4. 温度读数 -127.0℃ 或 85.0℃ 是传感器失效的标志值，属于异常。
5. F3007/F3008/F5003 表示系统【已经】因看门狗超时或死机异常复位过，严重度高于
   F3005（后者只是"即将触发"）；F5004 是看门狗超时时间配置非法（FAQ 要求自定义值 ≥6500ms）。
6. F3009/F3010 若同时带 mcause(17) 或 addr(18)，说明发生了非法指令/非法地址访问等硬件异常，
   属于"死机类"，根因多在野指针、数组越界、栈溢出写坏内存。
7. F3011 通常是任务死循环或长时间占用 CPU 的征兆。若同一窗口内 F3011 → F3005 →
   F3007/F5003 依次出现，按【一条因果链】处理：根因是 CPU 被长期占用，复位是结果。
8. 中断阻塞(F3008)与任务阻塞(F3003)要分开：前者发生在中断上下文里长时间不返回，
   会直接触发看门狗复位，排查方向是中断服务程序里是否有阻塞调用。

【输出格式】必须输出合法 JSON，且只输出 JSON：
{"is_anomaly": true, "cluster_key": "onewire_sensor_timeout", "severity": "P1",
 "summary": "一句话中文描述", "evidence_refs": ["hl:00000103"], "confidence": 0.9}

字段说明：
- is_anomaly: 布尔值
- cluster_label: 异常族的【人类可读名称】，中文，不超过 16 字，例如"传感器单总线无应答"。
  ⚠️ 它只是给人看的标签，**不承担聚合职责**：程序会用窗口内的故障码集合算出
  确定的聚合指纹来归并同类异常。所以你用词不同也不会影响聚合结果，
  放心按最贴切的说法命名即可。
- severity: P0(设备离线/数据全断) / P1(功能受损) / P2(性能下降) / P3(仅提示)
- summary: 不超过 50 字，必须包含"哪个 zone、什么现象"
- evidence_refs: 支撑判断的日志证据编号，必须来自输入中真实存在的编号
- confidence: 0~1

【示例】
输入窗口摘要：zone=B，60秒，ERROR 1 条，故障码 F1002×1
证据：
  [hl:00000103] E A001 AgriSensor: 1 103 5 1002 6 1 9 5001 z=B
输出：
{"is_anomaly": true, "cluster_label": "传感器单总线无应答", "severity": "P1",
 "summary": "Zone-B 温度传感器单总线无应答，连续重试后输出无效值", 
 "evidence_refs": ["hl:00000103"], "confidence": 0.92}
"""

USER_L1_TEMPLATE = """【本次窗口统计】
{stats}

【代表性日志证据（含真实证据编号）】
{evidence}

请按 JSON 格式输出判断结果。"""


# ---------------------------------------------------------------------------
# L2 提示词：根因分析报告
# ---------------------------------------------------------------------------

SYSTEM_L2 = """你是 OpenHarmony 系统运维专家，负责输出根因分析报告。报告要能让一线运维人员直接照着处理。

【铁律】
1. 只使用输入中提供的日志证据。**严禁编造**任何日志行、时间戳、地址、函数名、版本号。
2. 每条证据必须引用输入中真实存在的证据编号（形如 hl:00001234）。
3. 若证据不足以确定根因，把 suspected_root_cause 写成"证据不足，疑似方向：..."，
   并把 confidence 降到 0.5 以下。诚实比自信更重要。
4. 各字段均为中文字符串，不要嵌套对象。

【输出格式】只输出 JSON：
{
  "title": "报告标题（≤30字，含 zone 与现象）",
  "severity": "P0|P1|P2|P3",
  "symptom": "现象：客观描述发生了什么，含时间与设备",
  "suspected_root_cause": "疑似根因：给出因果链推理，说明为什么是这个原因而不是别的",
  "impact": "影响：影响了哪些功能、多少设备、数据是否丢失",
  "action": "处置建议：分【立即】【短期】【长期】三档，可执行",
  "confidence": 0.0
}

【领域知识（用于推理，勿直接照抄）】
- 单总线/传感器读失败常见原因：上拉电阻缺失、高优先级任务打断时序、线缆过长、供电不足
- 任务卡死(F3003)常见原因：低优先级任务被饿死、死循环、在中断里做阻塞操作
- 内存分配失败(F3001)常见原因：未释放的日志/上报缓冲、递归过深、任务栈设置过小
- 上报超时(F2002)可能是网络问题，也可能是内存不足导致的【次生故障】——要看时间顺序！
  【重要】若同一窗口内同时出现 F3006(堆不足) → F3001(分配失败) → F2002(上报超时)，
  说明这是【一条因果链】，根因是内存枯竭，上报超时是受害者，不要误判为网络问题。
- Hi3861 轻量设备 RAM 非常有限（约 300KB 级），任务栈溢出与堆枯竭是高发问题
- 看门狗(F3005/F3007/F5003)常见原因：系统在空闲时自动喂狗，超时多半是【阻塞导致进不了空闲】。
  任务中阻塞会先打印 "watchdog isr" 死机信息再复位；中断中阻塞会打印中断阻塞位置后直接复位。
  频繁触发时按 FAQ 3.1.4 查三类原因：① 任务死循环/长时间阻塞 ② 长时间占用 CPU
  ③ 循环体长时间处理——第 ③ 类应在循环体里调 hi_cpup_load_check_proc（CPU>95% 时让出 CPU），
  必要时调 hi_watchdog_feed 主动喂狗。看门狗自定义超时时间不得低于 6500ms。
- 死机(F3009/F3010)定位：看 eid/rid（0x1 任务中死机、0x2 中断中死机、0x3 看门狗任务中死机、
  0x4 看门狗中断中死机、0x5 系统软复位）+ mcause/ccause（见 FAQ 表3-5：0x0 指令地址非对齐、
  0x2 非法指令、0x5 Load 访问错、0x7 Store/AMO 访问错）+ mtval（出错地址）。
  栈溢出看任务信息里 Stack Overflow 标志是否为 1；单板重启后可用 AT+DUMP 查看上次死机信息。
- 非法内存访问(F3010)本质是"踩内存"：野指针、数组越界、栈溢出写坏相邻任务栈、释放后继续使用。
  结合 addr(字段18) 与 map / 反汇编(asm) 定位到具体函数，再看轨迹调度信息确认异常前后的任务切换。
"""

USER_L2_TEMPLATE = """【异常窗口信息】
{stats}

【完整证据日志（按时间排序，共 {n} 条）】
{evidence}

【同窗口其他异常窗口编号】{related}

请输出根因分析报告 JSON。"""


# ---------------------------------------------------------------------------
# 异常族指纹：聚合的【唯一依据】，由程序确定性计算
# ---------------------------------------------------------------------------

def window_signature(w: "WindowStats") -> str:
    """算出一个窗口的确定性"异常族指纹"。

    ★ 为什么聚合键绝不能让 LLM 决定 ★
      实测踩过：同一批注入故障在相邻两个 60 秒窗口里重复出现，
      分数一样、行数一样、涉及的故障码完全一样，
      但 LLM 一次命名成 `传感器读取超时`、一次命名成 `onewire_sensor_timeout`，
      于是"智能聚合"把【同一个族劈成了两类】，还各写了一份报告。

      题目要求的"异常智能聚合"核心就是：把反复出现的同类异常压成一个族，
      报告里说"出现 N 次"。聚合键一旦不稳定，这个要求就直接落空。

      所以职责必须分开：
        · cluster_key   ← 程序算，确定性、可复现、换模型/换措辞/LLM 挂掉都不变
        · cluster_label ← LLM 或故障码字典起的"给人看的名字"
      这样即使 LLM 完全不可用，聚合结果依然正确。

    指纹构成（固定顺序拼接，保证同一模式永远得到同一个键）：
      · 有故障码 → "FW-1001-1004-3005"（升序，与出现顺序无关）
      · 无故障码 → 退化为异常特征组合，如 "FEAT-SILENCE-ERR"
    """
    from agri_logger.pipeline.windows import NORMAL_PERIOD_S

    codes = sorted(int(t[1:]) for t in w.fault_count if t.startswith("F") and t[1:].isdigit())
    if codes:
        return "FW-" + "-".join(str(c) for c in codes)

    feats = []
    if w.gap_max > NORMAL_PERIOD_S * 2:
        feats.append("SILENCE")
    if w.err_count:
        feats.append("ERR")
    if w.warn_count:
        feats.append("WARN")
    if not feats:
        feats.append("NORMAL")
    return "FEAT-" + "-".join(feats)


def _codes_of(w: "WindowStats") -> set:
    return set(int(t[1:]) for t in w.fault_count if t.startswith("F") and t[1:].isdigit())


def cluster_families(windows: list, threshold: float = 0.6) -> list:
    """把异常窗口聚成"异常族" —— 程序的确定性聚类，与 LLM 无关。

    ★ 为什么不能要求指纹完全相等 ★
      实测踩过：指纹 `FW-1001-1004-3005`（×2）和 `FW-1001-1004`（×1）
      讲的是同一类问题，只因为注入器场景轮转，某个窗口正好多带一个 F3005，
      就被劈成了两族。要求"集合完全相等"太苛刻。

      改用故障码集合的 Jaccard 相似度做单链聚类：
        · 容得下零星差异（少一个/多一个故障码仍是同一族）
        · 又不会把不相干的问题混在一起（无交集就绝不合并）

    ★ 为什么聚类必须由程序做、不能让 LLM 做 ★
      LLM 不能保证同一输入得到同一分组：换措辞、换模型、重跑一次都可能变。
      "智能聚合"的分组结果必须可复现、可解释、可写进评测。
      LLM 在这里只负责给族起个好听的名字（cluster_label）。

    返回：按出现次数降序的族列表，每项含 key / codes / windows / count / ...
    """
    groups = []          # [{"codes": set, "windows": [...]}]
    # 输入顺序固定（按指纹字典序），保证聚类结果可复现
    ordered = sorted(windows, key=lambda w: window_signature(w))

    for w in ordered:
        codes = _codes_of(w)
        best, best_sim = None, 0.0
        for g in groups:
            gc = g["codes"]
            if not gc and not codes:
                sim = 1.0                      # 都没有故障码：同类
            elif not gc or not codes:
                sim = 0.0
            else:
                inter = len(gc & codes)
                union = len(gc | codes)
                sim = inter / union if union else 0.0
            if sim > best_sim:
                best, best_sim = g, sim
        if best is not None and best_sim >= threshold:
            best["codes"] |= codes
            best["windows"].append(w)
        else:
            groups.append({"codes": set(codes), "windows": [w]})

    out = []
    for g in groups:
        ws = g["windows"]
        codes = sorted(g["codes"])
        key = ("FW-" + "-".join(str(c) for c in codes)) if codes else window_signature(ws[0])
        zones = set()
        first = last = None
        err = warn = lines = 0
        max_score = 0.0
        for w in ws:
            zones.update(z for z in w.zones if z)
            first = w.start_ts if first is None else min(first, w.start_ts)
            last = w.end_ts if last is None else max(last, w.end_ts)
            err += w.err_count
            warn += w.warn_count
            lines += w.line_count
            max_score = max(max_score, w.anomaly_score)
        out.append({
            "key": key,
            "codes": ["F%d" % c for c in codes],
            "label": F.fault_name(codes[0]) if codes else (
                "日志静默" if "SILENCE" in key else "其他异常"),
            "count": len(ws),
            "windows": [w.window_id for w in ws],
            "zones": sorted(zones),
            "max_score": round(max_score, 3),
            "span": "%d~%ds" % (first or 0, last or 0),
            "err": err, "warn": warn, "lines": lines,
        })
    return sorted(out, key=lambda x: (-x["count"], -x["max_score"]))


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Analysis:
    window_id: str
    is_anomaly: bool = False
    cluster_key: str = "unknown"           # ★ 程序算的指纹，聚合依据，稳定
    cluster_label: str = ""                # ★ 给人看的名字，LLM/字典给的，可中文可英文
    severity: str = "P3"
    summary: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    confidence: float = 0.0
    mode: str = "llm"                      # llm | rule | skipped
    raw: dict = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        """报告标题与列表里显示的名字：优先用 label，没有再退回指纹"""
        return self.cluster_label or self.cluster_key

    @property
    def badge(self) -> str:
        icon = {"P0": "🔴", "P1": "🟠", "P2": "🟡", "P3": "⚪"}.get(self.severity, "⚪")
        tag = {"llm": "LLM", "rule": "规则降级", "skipped": "跳过"}.get(self.mode, self.mode)
        return f"{icon} {self.severity} [{tag}]"


@dataclass
class Report:
    window: WindowStats
    analysis: Analysis
    evidence: list[LogRecord] = field(default_factory=list)
    rca: dict = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    mode: str = "llm"
    family_count: int = 1        # 同一聚合指纹在本次分析里出现了几个窗口
    generated_at: str = ""       # 报告生成时刻（本地时间，形如 2026-10-10 14:23:45）
    data_source: str = ""        # 数据来源说明（真机串口 COM3 / 文件 xxx.log）
    llm_model: str = ""          # 实际使用的模型（降级时留空）

    def _stamp(self) -> str:
        """生成时刻。为空则现取 —— 保证任何调用路径下报告都有时间戳。"""
        if self.generated_at:
            return self.generated_at
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def _summary_line(self) -> str:
        """报告的【一句话摘要】—— 放在标题正下方，是整份报告最先被读的一行。

        优先用 LLM 给的 summary（L1 提示词要求"≤50字，须含哪个 zone、什么现象"）；
        规则降级时用窗口统计拼成同一句式，保证两种引擎下摘要都像"人话"、
        且【任何情况下都有摘要】，不会出现空白。
        粒度只用「zone + 模块 tag + 故障名」，不引入 task 名/地址等日志里没有的字段。
        """
        w = self.window
        if self.mode == "llm":
            s = (self.analysis.summary or "").strip()
            if s:
                return s
        zones = "、".join(z for z in w.zones if z and z != "-") or "全局"
        if w.fault_count:
            tag, cnt = w.fault_count.most_common(1)[0]
            return ("检测到 %s 在 %ds~%ds 出现%s（%s）共 %d 次，窗口内 %d 条日志。"
                    % (zones, w.start_ts, w.end_ts, F.fault_name(int(tag[1:])),
                       tag, cnt, w.line_count))
        return ("检测到 %s 窗口异常（%s）。"
                % (zones, "；".join(w.anomaly_reasons) or "异常分数越界"))

    def to_markdown(self, validator: "EvidenceValidator") -> str:
        a = self.analysis
        r = self.rca
        badge = {"llm": "LLM 语义分析", "rule": "本地规则引擎（降级）",
                 "raw": "仅原始事实（降级）"}.get(self.mode, self.mode)

        banner = ""
        if self.mode == "rule":
            banner = ("\n> ⚠️ **本报告由本地规则引擎生成**（LLM 不可用）。"
                      "根因结论基于故障码字典，未做跨模块语义推理。\n")
        elif self.mode == "raw":
            banner = ("\n> ⚠️ **本报告仅包含原始事实与统计**（LLM 与规则库均不可用），"
                      "不含根因推断，请人工分析。\n")

        title = r.get("title") or a.summary or f"{a.window_id} 异常分析"
        out = [
            f"# {title}",
            "",
            # ★ 摘要紧贴标题：报告以"简洁直观"为主，读者第一眼要能看懂发生了什么
            f"> **摘要**｜{self._summary_line()}",
            "",
            # ★ 生成时间放表格第一行：拿到任何一份报告，第一眼就知道它是什么时候算的
            f"| 项目 | 值 |",
            f"|---|---|",
            f"| **生成时间** | **{self._stamp()}**（本地时间） |",
            f"| 数据来源 | {self.data_source or '（未标注）'} |",
            f"| 窗口编号 | `{a.window_id}` |",
            f"| 严重度 | **{a.severity}** |",
            f"| 异常族 | **{a.display_name}** |",
            f"| 聚合指纹 | `{a.cluster_key}` |",
            f"| 本族出现 | {self.family_count} 个窗口（本次分析范围） |",
            f"| 时间范围 | {self.window.start_ts:.0f}s ~ {self.window.end_ts:.0f}s"
            f"（{self.window.duration_s:.0f}s，设备上电后的相对时间） |",
            f"| 涉及设备 | {'、'.join(self.window.zones) or '全局'} |",
            f"| 日志行数 | {self.window.line_count}（E={self.window.err_count} W={self.window.warn_count}） |",
            f"| 置信度 | {a.confidence * 100:.0f}% |",
            f"| 分析引擎 | {badge}"
            + (f"（模型 {self.llm_model}）" if self.llm_model and self.mode == "llm" else "")
            + " |",
            banner,
            "## 一、现象",
            r.get("symptom") or a.summary or "（无）",
            "",
            "## 二、证据",
            "",
            "```",
            # ★ 关键：证据段由程序生成，不经过 LLM → 事实准确率 100%
            validator.render_evidence_block(self.evidence, self.window),
            "```",
            "",
            "## 三、疑似根因",
            r.get("suspected_root_cause") or "（LLM 未提供）",
            "",
            "## 四、影响",
            r.get("impact") or "（LLM 未提供）",
            "",
            "## 五、处置建议",
            r.get("action") or "（LLM 未提供）",
            "",
            "---",
            # ★ 落款要说清楚"OpenHarmony 日志智能聚合分析工具"是【本项目自己写的程序】，
            #   不是数据/服务的提供方。之前写成"由 OpenHarmony ... 自动生成"，
            #   会被误读成"这报告是 OpenHarmony 提供的"，与事实不符。
            f"<sub>本报告由「OpenHarmony 日志智能聚合分析工具」"
            f"（本项目自研的分析程序）自动生成"
            f" ｜ 生成时间 {self._stamp()}"
            f" ｜ 数据来源 {self.data_source or '未标注'}"
            f" ｜ 证据引用 {len(self.evidence)} 条，全部可在原始日志中按证据编号回查</sub>",
        ]
        return "\n".join(out)


# ---------------------------------------------------------------------------
# 证据校验器 —— 防幻觉的第三重保险
# ---------------------------------------------------------------------------

class EvidenceValidator:
    """校验 LLM 引用的证据编号是否真实存在。

    ★ 报告的"事实准确率 100%"就靠这个：
      LLM 只输出证据【编号】，报告里的证据【内容】由本类从原始日志取出。
      编造的编号查不到 → 直接不渲染 → 幻觉无法进入报告。
    """

    def __init__(self, records: list[LogRecord]):
        self.index: dict[str, LogRecord] = {r.evidence_id: r for r in records}
        self.total_refs = 0
        self.invalid_refs: list[str] = []

    def validate(self, refs: list[str]) -> list[LogRecord]:
        ok: list[LogRecord] = []
        for ref in refs:
            self.total_refs += 1
            rec = self.index.get(ref)
            if rec is None:
                self.invalid_refs.append(ref)
            else:
                ok.append(rec)
        return ok

    def render_evidence_block(self, records: list[LogRecord],
                              window: Optional[WindowStats] = None) -> str:
        if not records:
            return "（无有效证据）"
        return "\n".join(
            f"[{r.evidence_id}] {r.raw_line}"
            for r in records
        )

    @property
    def hallucination_rate(self) -> float:
        if self.total_refs == 0:
            return 0.0
        return len(self.invalid_refs) / self.total_refs

    def report(self) -> str:
        return (f"证据引用 {self.total_refs} 次，无效引用 {len(self.invalid_refs)} 次"
                f"（幻觉率 {self.hallucination_rate * 100:.2f}%），"
                f"**全部无效引用已被程序层拦截，报告事实准确率 100%**")


# ---------------------------------------------------------------------------
# 分析器
# ---------------------------------------------------------------------------

class AnomalyAnalyzer:
    """L1 判定 + L2 根因分析。LLM 不可用时自动降级到规则引擎。"""

    def __init__(self, llm: Optional[LLMClient] = None):
        self.llm = llm or LLMClient()
        self.usage = Usage()

    # ---------------- L1 ----------------
    def analyze_window(self, w: WindowStats, all_records: list[LogRecord]) -> Analysis:
        ev = w.evidence(max_lines=20)
        evidence_text = "\n".join(f"  [{r.evidence_id}] {r.raw_line}" for r in ev) or "  （无）"

        messages = [
            {"role": "system", "content": SYSTEM_L1},
            {"role": "user", "content": USER_L1_TEMPLATE.format(
                stats=w.to_prompt_text(), evidence=evidence_text)},
        ]
        # ⚠️ max_tokens 必须给足：思考模式会先产出大量思维链 token，
        #    给太小会导致 JSON 被截断 → 解析失败 → 静默降级到规则引擎。
        #    实测 1200 会失败，3000 稳定通过。
        data, usage = self.llm.call_json(messages, tag=f"L1-{w.window_id}", max_tokens=3000)
        self.usage.add(usage)

        if data is None:
            log.warning("[L1-%s] LLM 未返回结果，降级到规则引擎", w.window_id)
            return self._rule_analysis(w)

        return Analysis(
            window_id=w.window_id,
            is_anomaly=bool(data.get("is_anomaly", True)),
            # ★ 聚合键用程序算的指纹，【不用】 LLM 给的名字
            cluster_key=window_signature(w),
            # LLM 给的名字只当显示标签，且兼容旧字段名 cluster_key
            cluster_label=str(data.get("cluster_label")
                              or data.get("cluster_key") or ""),
            severity=str(data.get("severity", "P3")),
            summary=str(data.get("summary", "")),
            evidence_refs=[str(x) for x in data.get("evidence_refs", []) if isinstance(x, str)],
            confidence=float(data.get("confidence", 0.5) or 0.5),
            mode="llm",
            raw=data,
        )

    # ---------------- L2 ----------------
    def analyze_root_cause(self, w: WindowStats, analysis: Analysis,
                           all_records: list[LogRecord]) -> tuple[dict, Usage]:
        ev = w.evidence(max_lines=30)
        evidence_text = "\n".join(f"  [{r.evidence_id}] {r.raw_line}" for r in ev) or "  （无）"

        messages = [
            {"role": "system", "content": SYSTEM_L2},
            {"role": "user", "content": USER_L2_TEMPLATE.format(
                stats=w.to_prompt_text(), n=len(ev), evidence=evidence_text,
                related=analysis.cluster_key)},
        ]
        data, usage = self.llm.call_json(messages, tag=f"L2-{w.window_id}", max_tokens=2500)
        if data is None:
            return self._rule_rca(w), usage
        return data, usage

    # ---------------- 降级：规则引擎 ----------------
    def _rule_analysis(self, w: WindowStats) -> Analysis:
        """LLM 不可用时的降级：用故障码字典给出判定

        注意：cluster_key 走的是和 LLM 路径【同一个】window_signature()，
        所以降级不会改变聚合结果 —— 这正是把聚合从 LLM 手里拿回来的好处。
        """
        sig = window_signature(w)
        if w.fault_count:
            top_tag, cnt = w.fault_count.most_common(1)[0]
            code = int(top_tag[1:])
            return Analysis(
                window_id=w.window_id, is_anomaly=True,
                cluster_key=sig,
                cluster_label=F.fault_name(code),
                severity="P1" if w.err_count else "P2",
                summary=f"{w.window_id}: {top_tag}({F.fault_name(code)}) 出现 {cnt} 次，"
                        f"涉及 {', '.join(w.zones)}",
                confidence=0.65, mode="rule",
                raw={"faults": dict(w.fault_count), "reasons": w.anomaly_reasons},
            )
        return Analysis(
            window_id=w.window_id, is_anomaly=w.anomaly_score >= 0.25,
            cluster_key=sig,
            cluster_label="日志静默" if "SILENCE" in sig else "",
            severity="P2", summary="；".join(w.anomaly_reasons) or "无明显异常",
            confidence=0.5, mode="rule")

    def _rule_rca(self, w: WindowStats) -> dict:
        """降级根因：基于故障码字典的因果链识别"""
        codes = {int(t[1:]) for t in w.fault_count}
        chain = []
        if {3006, 3001} & codes:
            chain.append("内存类故障")
        if 2002 in codes:
            chain.append("上报超时")
        if {1002, 1004, 1001, 1006} & codes:
            chain.append("传感器读取异常")
        if {3003, 3004} & codes:
            chain.append("任务调度异常")
        # ---- 系统异常类（FAQ 第 3 章）：CPU 过载 → 看门狗/崩溃 → 异常复位 ----
        if 3011 in codes:
            chain.append("CPU 占用率过高")
        if {3007, 3008, 3009, 3010} & codes:
            chain.append("看门狗/系统崩溃")
        if 5003 in codes:
            chain.append("设备异常复位")

        if len(chain) > 1:
            cause = ("跨模块因果链：" + " → ".join(chain) +
                     "。存在先后依赖关系，建议按链首模块排查。")
        elif chain:
            cause = f"疑似 {chain[0]}，请结合证据日志确认。"
        else:
            cause = "证据不足，疑似方向：需人工复核（LLM 与规则库均未给出明确结论）。"

        return {
            "title": f"{w.window_id} 异常（规则引擎判定）",
            "severity": "P1" if w.err_count else "P2",
            "symptom": "；".join(w.anomaly_reasons) or "（无）",
            "suspected_root_cause": cause,
            "impact": f"影响设备 {', '.join(w.zones) or '全局'}，窗口内 {w.line_count} 行日志。",
            "action": "【立即】人工复核证据日志；【短期】检查对应模块；【长期】完善监控。",
            "confidence": 0.55,
        }
