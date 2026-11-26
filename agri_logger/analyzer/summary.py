#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可读摘要生成器 —— 把窗口统计拼成"人话"，**不依赖 LLM**

============================================================================
为什么需要这个模块（三层复用，只有一份措辞逻辑）
============================================================================
  · 告警标题/摘要     notifier 发邮件、发 Webhook 时的一句话
  · 报告摘要行        rca.Report._summary_line() 标题正下方那行
  · 规则降级根因       LLM 不可用时 _rule_rca() 的 symptom / action

这三处如果各写一份拼装逻辑，迟早会出现"邮件里说 A、报告里说 B"的
自相矛盾。所以统一到这里，**任何路径都得到同一句话**。

============================================================================
设计红线：只写日志里【真实存在】的字段
============================================================================
需求给的例子是「检测到 TaskA 在 0x1000 地址处发生栈溢出，疑似递归调用过深」。
这句话能这么说，前提是真有 任务名 和 出错地址。本项目的日志里：
  · 没有任务名字段 → 用 zone（逻辑设备）+ tag（模块名，如 AgriHealth）代替
  · 有出错地址（字段 18 addr）和机器异常原因码（字段 17 mcause）→ 原样引用
没有的字段一律不编。宁可少说，也不能让运维拿着一个假地址去查代码。
"""

from __future__ import annotations

from typing import Optional

from ..parser import fields as F
from ..pipeline.windows import NORMAL_PERIOD_S, WindowStats

# ---------------------------------------------------------------------------
# RISC-V 机器异常原因码 —— 与 fields.py 字段 17 的说明、docs/02 §5.6 一致
# ---------------------------------------------------------------------------
MCAUSE_NAMES = {
    0: "指令地址非对齐",
    1: "取指访问错",
    2: "非法指令",
    4: "Load 地址非对齐",
    5: "Load 访问错",
    6: "Store 地址非对齐",
    7: "Store/AMO 访问错",
    15: "Store 页错",
}

# 单传感器口径下，日志里只有一个真实设备。zone 只有 A（单设备模式）或 A~F（六棚模式）。
GLOBAL_DEVICE = "整机"


def mcause_name(v: Optional[int]) -> str:
    if v is None:
        return ""
    return MCAUSE_NAMES.get(int(v), "未知 mcause=%d" % int(v))


# ---------------------------------------------------------------------------
# 从窗口里取证：地址、原因码、主导模块、故障名
# ---------------------------------------------------------------------------

def window_facts(w: WindowStats) -> dict:
    """抽取"能写进人话"的客观事实。没取到的一律给 None，绝不补假值。"""
    # 主导故障码：次数最多、且不是"正常运维事件"的那个
    abnormal = [(tag, cnt) for tag, cnt in w.fault_count.most_common()
                if tag.startswith("F") and tag[1:].isdigit()
                and not F.is_normal_event(int(tag[1:]))]
    top_tag, top_cnt = (abnormal[0] if abnormal else (None, 0))

    # 出错地址 / mcause：从后往前找（崩溃点通常在窗口末尾）。
    # ★ 只有【两者都找到】才提前结束：早期版本写成"找到一个就 break"，
    #   结果最后一条只带 mcause、地址在更早一条上时，地址就丢了 ——
    #   摘要里少了 0x1000，运维就得自己去翻日志。
    addr = None
    mcause = None
    for r in reversed(w.records):
        if addr is None and 18 in r.values:
            addr = r.values[18]
        if mcause is None and 17 in r.values:
            mcause = r.values[17]
        if addr is not None and mcause is not None:
            break

    # 主导模块：出现次数最多的 domain（"A008 系统异常" 这种）
    top_domain = None
    for dom, _cnt in w.domain_count.most_common():
        if dom in F.DOMAINS:
            top_domain = dom
            break

    return {
        "device": _device_label(w),
        "fault_tag": top_tag,
        "fault_name": F.fault_name(int(top_tag[1:])) if top_tag else "",
        "fault_count": top_cnt,
        "abnormal_faults": abnormal,
        "addr": addr,
        "mcause": mcause,
        "mcause_name": mcause_name(mcause),
        "domain": top_domain,
        "domain_name": F.DOMAINS.get(top_domain, "") if top_domain else "",
        "zones": [z for z in w.zones if z and z != "-"],
    }


def _device_label(w: WindowStats) -> str:
    """设备称呼。单设备模式下只有一个 zone，就直说"设备"而不是"1 号棚"。"""
    zones = [z for z in w.zones if z and z != "-"]
    if not zones:
        return GLOBAL_DEVICE
    if len(zones) == 1:
        return "设备 %s" % zones[0]
    if len(zones) >= 4:                     # 六棚模式且几乎都涉及 → 系统性问题
        return "全部 %d 个设备" % len(zones)
    return "设备 " + "、".join(zones)


def addr_clause(facts: dict) -> str:
    """「在 0x1000 地址处」这一小段。没有地址就给空串，不要硬凑。"""
    addr = facts.get("addr")
    if addr is None:
        return ""
    try:
        text = "0x%X" % (int(addr) & 0xFFFFFFFF)
    except (TypeError, ValueError):
        text = str(addr)
    mc = facts.get("mcause_name")
    return "在地址 %s 处%s" % (text, ("（%s）" % mc) if mc else "")


# ---------------------------------------------------------------------------
# 因果链：与 rca._rule_rca 共用，避免两处判定不一致
# ---------------------------------------------------------------------------

def cause_chain(codes) -> list:
    """按故障码集合推出一个"人话"因果链（顺序即因果顺序）。

    这是**规则**，不是推理：故障码 →（该故障可能引发的下游模块）的固定映射。
    LLM 可用时会给更细的推理；不可用时这条链就是兜底结论。
    """
    codes = set(int(c) for c in codes)
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
    return chain


# 故障码 → "疑似原因"（写给运维看的判断方向，全部来自 FAQ 与常见踩坑）
#
# ★ 措辞纪律（两条，都来自实测中被读着别扭的句子）：
#   1. 每句都必须能直接接在"，疑似"后面读通 → 只写【成因名词短语】，
#      不写"请检查…""建议…"这类动作句（动作句属于报告第五节处置建议）。
#   2. 不能复述故障名本身。故障名已经出现在"发生 X"里，
#      再写"发生单总线复位无应答，疑似单总线无应答"就是原地打转。
#      这里只补【为什么】。
CAUSE_HINTS = {
    1001: "上拉电阻缺失、高优先级任务打断时序或供电不足",
    1002: "接线松动或上拉电阻缺失",
    1003: "线缆过长或时序被高优先级任务打断",
    1004: "传感器已失效（读出了物理不可能的值）",
    1005: "环境确实超标，或传感器标定漂移",
    1006: "供电中断或 I2C 总线异常",
    2002: "网络不畅，或内存不足导致的次生超时",
    2003: "距离过远或被遮挡",
    2005: "链路质量差（线缆接触不良或电磁干扰）",
    3001: "未释放的上报缓冲，或递归过深导致内存耗尽",
    3002: "递归过深或任务栈设置过小",
    3003: "低优先级任务被饿死或任务内死循环",
    3004: "消费端处理不过来，生产速度超过消费速度",
    3005: "阻塞导致进不了空闲任务喂狗",
    3006: "存在内存泄漏，空闲堆被持续占用",
    3007: "任务死循环或长时间占用 CPU，进不了空闲任务喂狗",
    3008: "中断服务程序里有阻塞调用，长时间不返回",
    3009: "野指针、栈溢出写坏内存，或执行了非法指令",
    3010: "野指针、数组越界，或栈溢出写坏相邻任务栈",
    3011: "循环体里长时间处理且未主动让出 CPU",
    4001: "驱动电路或继电器异常",
    5002: "配置分区损坏或校验不通过",
    5003: "设备曾因看门狗超时或死机重启过",
    5004: "自定义超时低于 6500ms 的下限",
}


def cause_hint(facts: dict) -> str:
    """给一句"疑似什么原因"。优先用故障码字典，其次用因果链，最后诚实地说不知道。"""
    tag = facts.get("fault_tag")
    if tag:
        hint = CAUSE_HINTS.get(int(tag[1:]))
        if hint:
            return hint
    chain = cause_chain(int(x[0][1:]) for x in (facts.get("abnormal_faults") or []))
    if len(chain) > 1:
        return "这是跨模块因果链 " + " → ".join(chain) + "，问题出在链首模块"
    if chain:
        return "这是" + chain[0] + "引发的"
    if facts.get("mcause_name"):
        return "内存被写坏导致的硬件异常（%s）" % facts["mcause_name"]
    return ""


# ---------------------------------------------------------------------------
# 对外：两种粒度的"人话"
# ---------------------------------------------------------------------------

def _what(f: dict, label: str, w: Optional[WindowStats] = None) -> str:
    """「发生了什么」用哪个名字。

    优先级：主导故障码的字典名 > LLM/规则给的标签 > 兜底。
    ★ 为什么故障码字典优先：它是【确定性】的，且一定与日志里的 Fxxxx 对得上。
      而标签可能是 LLM 随手起的名字（有时带 " / " 这类拼接符），
      放进"发生 X"这个句式里会读着别扭。
    """
    if f.get("fault_name"):
        return f["fault_name"]
    lab = (label or "").strip()
    # ★ 过滤掉"指纹"式的标签：Analysis.display_name 在没有 label 时会退回
    #   cluster_key（形如 "FW-1002-3005" / "FEAT-SILENCE"）。那是给程序用的键，
    #   直接塞进"检测到 X 发生 Y"里会变成"发生 FW-1002-3005"，读着像天书。
    if lab and not (lab.startswith("FW-") or lab.startswith("FEAT-")):
        return lab
    if w is not None and _is_silent(w):
        return "日志静默"
    return "异常"


def _sp(text: str) -> str:
    """中文与西文之间补一个空格，中文与中文之间不补。

    为什么要这个：设备名可能是「设备 A」（以西文结尾）也可能是「全部 6 个设备」
    （以中文结尾）。前一种后面直接接「在」会挤成「设备 A在」，后一种加空格又会
    变成「全部 6 个设备 在」。用尾字符判断一次，两种都读着顺。
    """
    return text + (" " if text and text[-1].isascii() and text[-1].isalnum() else "")


def one_line_alert(w: WindowStats, label: str = "") -> str:
    """★ 告警用的一句话摘要（≤ 约 70 字）。

    这是最需要打磨的一句：它会出现在邮件主题、Webhook 消息、看板告警条里。
    句式固定为「检测到 <设备> 在 <地址> 处 <发生了什么>，疑似 <原因>」，
    与需求给的例子同构，便于运维一眼抓住"哪台设备、出了什么事、大概为什么"。
    """
    f = window_facts(w)
    what = _what(f, label, w)

    head = "检测到" + _sp(f["device"])
    ac = addr_clause(f)
    if ac:
        head += ac
    head += "发生%s" % what
    if f["fault_tag"]:
        head += "（%s）" % f["fault_tag"]

    hint = cause_hint(f)
    return head + ("，疑似%s。" % hint if hint else "。")


def describe_window(w: WindowStats, label: str = "") -> str:
    """稍长的一版（≤ 约 140 字），用于报告摘要行与告警正文首段。

    比 one_line_alert 多给：时间范围、出现次数、异常分、触发信号。
    句式："检测到 <设备> 在 <a>~<b>s 范围内出现 <故障>（<F码>），<补充>。<疑似>（元信息）"
    """
    f = window_facts(w)
    what = _what(f, label, w)

    # 主干：必须连成一口气读完，中间不能塞逗号（否则读起来像两句话）
    head = ("检测到%s在 %d~%ds 范围内出现%s"
            % (_sp(f["device"]), int(w.start_ts), int(w.end_ts), what))
    if f["fault_tag"]:
        head += "（%s）" % f["fault_tag"]

    # 补充信息：每项都是独立短句，用逗号分隔
    extras = []
    if f["fault_tag"] and f["fault_count"] > 1:
        extras.append("共 %d 次" % f["fault_count"])
    ac = addr_clause(f)
    if ac:
        extras.append(ac)
    if _is_silent(w):
        extras.append("并出现日志静默（最大间隔 %.1fs，正常约 %.0fs）"
                      % (w.gap_max, NORMAL_PERIOD_S))
    if w.seq_gap_total:
        extras.append("seq 缺口 %d 条" % w.seq_gap_total)

    text = head + ("，" + "，".join(extras) if extras else "") + "。"
    hint = cause_hint(f)
    if hint:
        text += "疑似%s。" % hint
    text += "（窗口 %s，异常分 %.2f，%d 行日志）" % (
        w.window_id, w.anomaly_score, w.line_count)
    return text


def health_line(windows_count: int, records_count: int, duration_s: float) -> str:
    """没有异常时也要有一句话 —— 阴性样本同样是有价值的结论。"""
    return ("在 %d 个窗口、%d 行日志（跨度 %.0f 秒）内未检出异常："
            "级别分布正常、无异常故障码、无日志静默、无序号缺口、无阈值越界。"
            % (windows_count, records_count, duration_s))


def _is_silent(w: WindowStats) -> bool:
    return w.gap_max > NORMAL_PERIOD_S * 2
