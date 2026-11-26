#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""告警事件与告警策略 —— 决定"哪条异常该推送、推几次、推给谁"

============================================================================
为什么告警要单独一层，而不是"生成报告时顺手发一封邮件"
============================================================================
  一个 60 秒窗口就异常一次，一次故障持续 10 分钟 = 10 个窗口 = 10 封邮件。
  邮箱会被打爆，人会开始忽略告警 —— 这比没有告警更糟（"告警疲劳"）。

  所以中间必须有策略层，做四件事：
    ① 去重    同一窗口只推一次（alert_id 稳定）
    ② 冷却    同一聚合指纹在 N 秒内不重复推（同一故障的连续窗口）
    ③ 限流    每小时最多 N 条（防止雪崩把配额打光）
    ④ 分级    P3 只记文件不打扰人；P0 全通道推送

============================================================================
alert_id 为什么是确定性的
============================================================================
  alert_id = "A-<窗口号>-<指纹哈希前 8 位>"。同一窗口无论被自动分析
  还是被手动"生成根因报告"触发，算出来的 id 一样 → 天然幂等。
  用 uuid 就会变成"点一次按钮多发一封邮件"。
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..pipeline.windows import WindowStats
from ..analyzer import summary as S
from .config import AlertConfig

SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def _rank(sev: str) -> int:
    return SEVERITY_ORDER.get((sev or "P3").upper(), 3)


def _clip(text: str, n: int) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    return t if len(t) <= n else t[: n - 1] + "…"


# ---------------------------------------------------------------------------
# 告警事件
# ---------------------------------------------------------------------------

@dataclass
class AlertEvent:
    alert_id: str
    created_at: str
    severity: str
    title: str
    summary: str                 # ★ 一句话可读摘要（LLM 优先，规则兜底）
    window_id: str = ""
    cluster_key: str = ""
    device: str = ""
    device_name: str = ""
    span: str = ""
    score: float = 0.0
    mode: str = "rule"           # llm | rule
    source: str = ""             # 数据来源（真机串口 / 文件）
    report_file: str = ""
    reasons: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    extra: Dict[str, object] = field(default_factory=dict)

    # ---------------- 序列化 ----------------
    def to_dict(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "created_at": self.created_at,
            "severity": self.severity,
            "title": self.title,
            "summary": self.summary,
            "window_id": self.window_id,
            "cluster_key": self.cluster_key,
            "device": self.device,
            "device_name": self.device_name,
            "span": self.span,
            "score": round(float(self.score), 3),
            "mode": self.mode,
            "source": self.source,
            "report_file": self.report_file,
            "reasons": list(self.reasons),
            "evidence": list(self.evidence),
            "extra": dict(self.extra),
        }

    def one_line(self) -> str:
        return "[%s] %s ｜ %s" % (self.severity, self.title, self.summary)

    def to_email_text(self) -> str:
        """邮件正文。纯文本，任何客户端都不会乱码。

        顺序刻意按"运维在手机上先看什么"排：摘要 → 关键字段 → 证据 → 处置。
        """
        lines = [
            "【%s】%s" % (self.severity, self.title),
            "",
            "摘要：%s" % self.summary,
            "",
            "── 详情 ──────────────────────────",
            "设备      %s" % (self.device or "—"),
            "时间范围  %s" % (self.span or "—"),
            "窗口      %s" % (self.window_id or "—"),
            "异常族    %s" % (self.cluster_key or "—"),
            "异常分    %.2f" % float(self.score),
            "分析引擎  %s" % ("LLM 语义分析" if self.mode == "llm" else "本地规则引擎（降级）"),
            "数据来源  %s" % (self.source or "—"),
            "生成时间  %s" % self.created_at,
        ]
        if self.reasons:
            lines += ["", "── 触发信号 ──────────────────────"]
            for r in self.reasons[:8]:
                lines.append("· %s" % r)
        if self.evidence:
            lines += ["", "── 证据日志（可在原始日志中按行号回查）──"]
            for e in self.evidence[:6]:
                lines.append(e)
        if self.report_file:
            lines += ["", "完整根因报告见附件 / 看板「根因报告」页：%s" % self.report_file]
        lines += [
            "",
            "── 说明 ──────────────────────────",
            "本告警由「OpenHarmony 日志智能聚合分析工具」自动生成。",
            "报告中的证据段由程序直接从原始日志取出，不经过模型，可逐条回查。",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 由窗口 + 分析结果构造告警
# ---------------------------------------------------------------------------

def build_alert(window: WindowStats,
                analysis,
                rca: Optional[dict] = None,
                report_file: str = "",
                source: str = "",
                device_name: str = "",
                evidence: Optional[list] = None,
                max_evidence: int = 6) -> AlertEvent:
    """把一次分析的结果包成告警。

    ★ 摘要的两级来源：
      1. rca["alert_summary"] —— L2 提示词专门要求 LLM 输出的"告警级一句话"
      2. 规则兜底 summary.one_line_alert() —— 与提示词同构，不含任何编造字段
    这样"LLM 挂了邮件里就空白"的情况不会发生。
    """
    rca = rca or {}
    facts = S.window_facts(window)

    sev = str(rca.get("severity") or getattr(analysis, "severity", "P2") or "P2").upper()
    if sev not in SEVERITY_ORDER:
        sev = "P2"

    label = (getattr(analysis, "display_name", "") or "").strip()
    # 过滤指纹式标签：没有 LLM 命名时 display_name 会退回 cluster_key
    # （"FW-1002-3005" / "FEAT-SILENCE"），塞进邮件标题会像乱码。
    if label.startswith("FW-") or label.startswith("FEAT-"):
        label = ""
    title = _clip(str(rca.get("title") or label
                      or ("%s 异常" % window.window_id)), 60)

    # 摘要：LLM 的告警级一句话 > L1 的 summary > 确定性拼装
    summary = ""
    for cand in (rca.get("alert_summary"), getattr(analysis, "summary", "")):
        if isinstance(cand, str) and cand.strip():
            summary = _clip(cand, 120)
            break
    if not summary:
        summary = S.one_line_alert(window, label)

    key = getattr(analysis, "cluster_key", "") or ""
    fp = key or window.window_id
    digest = hashlib.sha1(("%s|%s" % (window.window_id, fp)).encode("utf-8")).hexdigest()[:8]

    ev_lines: List[str] = []
    for r in (evidence or []):
        raw = getattr(r, "raw_line", "") or ""
        eid = getattr(r, "evidence_id", "") or ""
        if raw:
            ev_lines.append("[%s] %s" % (eid, raw) if eid else raw)
        if len(ev_lines) >= max_evidence:
            break

    return AlertEvent(
        alert_id="A-%s-%s" % (window.window_id, digest),
        created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        severity=sev,
        title=title,
        summary=summary,
        window_id=window.window_id,
        cluster_key=key,
        device=facts["device"],
        device_name=device_name,
        span="%ds ~ %ds" % (int(window.start_ts), int(window.end_ts)),
        score=window.anomaly_score,
        mode="llm" if getattr(analysis, "mode", "rule") == "llm" else "rule",
        source=source,
        report_file=report_file,
        reasons=list(window.anomaly_reasons),
        evidence=ev_lines,
        extra={"lines": window.line_count, "err": window.err_count,
               "faults": dict(window.fault_count)},
    )


def build_health_alert(windows_count: int, records_count: int, duration_s: float,
                       source: str = "", report_file: str = "") -> AlertEvent:
    """健康检查（无异常）—— 级别 P3，默认只进文件，不打扰人。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    return AlertEvent(
        alert_id="A-HEALTH-%s" % time.strftime("%Y%m%d%H%M%S"),
        created_at=now, severity="P3",
        title="系统健康报告：未发现异常",
        summary=S.health_line(windows_count, records_count, duration_s),
        device=S.GLOBAL_DEVICE, span="%.0fs" % duration_s,
        mode="rule", source=source, report_file=report_file,
        extra={"windows": windows_count, "records": records_count})


# ---------------------------------------------------------------------------
# 策略
# ---------------------------------------------------------------------------

class AlertPolicy:
    """去重 / 冷却 / 限流 / 分级。全部是确定性的，可单元测试。"""

    def __init__(self, cfg: AlertConfig):
        self.cfg = cfg
        self._sent_ids: Dict[str, float] = {}     # alert_id -> 时刻
        self._fp_last: Dict[str, float] = {}      # 聚合指纹 -> 上次推送时刻
        self._times: List[float] = []             # 最近推送时刻（限流用）
        self.skipped: Dict[str, int] = {}

    # ---- 判定 ----
    def check(self, ev: AlertEvent, now: Optional[float] = None) -> Tuple[bool, str]:
        now = time.time() if now is None else now
        if not self.cfg.enabled:
            return False, "告警总开关关闭"
        if _rank(ev.severity) > _rank(self.cfg.min_severity):
            return False, "级别 %s 低于推送门槛 %s" % (ev.severity, self.cfg.min_severity)
        if ev.alert_id in self._sent_ids:
            return False, "该窗口已推送过（alert_id 去重）"

        fp = ev.cluster_key or ev.window_id
        last = self._fp_last.get(fp)
        if last is not None and self.cfg.cooldown_s > 0:
            el = now - last
            if el < self.cfg.cooldown_s:
                return False, ("同一异常族 %s 距上次推送仅 %.0fs（冷却 %.0fs）"
                               % (fp, el, self.cfg.cooldown_s))
        self._gc(now)
        if self.cfg.max_per_hour > 0 and len(self._times) >= self.cfg.max_per_hour:
            return False, "已达每小时上限 %d 条" % self.cfg.max_per_hour
        return True, ""

    # ---- 记账 ----
    def note(self, ev: AlertEvent, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        self._sent_ids[ev.alert_id] = now
        self._fp_last[ev.cluster_key or ev.window_id] = now
        self._times.append(now)

    def note_skip(self, reason: str) -> None:
        k = reason.split("（")[0][:24]
        self.skipped[k] = self.skipped.get(k, 0) + 1

    def _gc(self, now: float) -> None:
        cut = now - 3600.0
        self._times = [t for t in self._times if t >= cut]
        # id 表长期不用会涨，窗口数是有限的，仍给个上限
        if len(self._sent_ids) > 2000:
            keep = sorted(self._sent_ids.items(), key=lambda kv: -kv[1])[:1000]
            self._sent_ids = dict(keep)


# ---------------------------------------------------------------------------
# 管理器
# ---------------------------------------------------------------------------

class AlertManager:
    """策略 + 通道的组合体。看板持有一个实例。"""

    HISTORY_MAX = 200

    def __init__(self, cfg: Optional[AlertConfig] = None):
        self.cfg = cfg or AlertConfig()
        self.policy = AlertPolicy(self.cfg)
        self.history: List[dict] = []
        self.sent = 0
        self.failed = 0
        self.last_result: List[dict] = []
        self.last_error = ""
        self._ensure_channels()

    def _ensure_channels(self) -> None:
        # 延迟导入：channels 依赖本模块的 AlertEvent，模块级导入会成环
        from .channels import build_channels
        self.channels = build_channels(self.cfg)

    # ---- 重新读取配置（改完 .env 不用重启看板）----
    def reload(self, env_file: Optional[Path] = None) -> None:
        self.cfg = AlertConfig.from_env(env_file)
        self.policy.cfg = self.cfg
        self._ensure_channels()
        # 配置刚被改过，上一轮失败留下的退避必须清掉，否则"改了 .env
        # 点测试还是被跳过"，现场会误判成配置没生效。
        for ch in self.channels:
            ch.reset_failures()

    # ---- 发送 ----
    def send(self, ev: AlertEvent, report_md: str = "") -> dict:
        """推一条告警。返回结构化结果，**永不抛异常**。

        三条分支，对应三种完全不同的意图：
          ① 总开关关闭       → 什么都不做，安静返回（不算失败）
          ② 级别低于门槛     → 只写审计文件，不打扰人（P3 健康报告走这条）
          ③ 正常             → 过冷却/限流，走全部通道
        """
        out = {"ok": False, "skipped": False, "reason": "",
               "alert": ev.to_dict(), "channels": []}

        # ① 总开关
        if not self.cfg.enabled:
            out.update(ok=True, skipped=True, quiet=True, reason="告警总开关关闭")
            self._remember(ev, out, delivered=False)
            return out

        # ② 低于推送门槛 → 只留审计记录
        #   "不打扰"不等于"不记录"：文件里留着才能事后回溯"那天系统是健康的"。
        if _rank(ev.severity) > _rank(self.cfg.min_severity):
            audit = [ch for ch in self.channels if ch.kind == "file"]
            results = [ch.send(ev, report_md) for ch in audit]
            out.update(ok=True, skipped=True, quiet=True,
                       reason="级别 %s 低于推送门槛 %s，仅记入审计文件"
                              % (ev.severity, self.cfg.min_severity),
                       channels=[r.to_dict() for r in results])
            self._remember(ev, out, delivered=False)
            return out

        # ③ 去重 / 冷却 / 限流
        ok, reason = self.policy.check(ev)
        if not ok:
            self.policy.note_skip(reason)
            out.update(skipped=True, reason=reason)
            self._remember(ev, out, delivered=False)
            return out

        results = [ch.send(ev, report_md) for ch in self.channels]
        out["channels"] = [r.to_dict() for r in results]
        self.last_result = out["channels"]

        delivered = [r for r in results if r.ok and not r.skipped]
        self.policy.note(ev, time.time())
        if delivered:
            self.sent += 1
            out["ok"] = True
        else:
            self.failed += 1
            out["ok"] = False
            out["reason"] = (("所有通道均未成功：" + "; ".join(str(r) for r in results))
                             if results else "没有可用通道")
            self.last_error = out["reason"]

        self._remember(ev, out, delivered=bool(delivered))
        return out

    def send_direct(self, ev: AlertEvent, report_md: str = "",
                    kinds: Optional[List[str]] = None) -> dict:
        """绕过策略层直接推送 —— **只给通道自测用**。

        为什么必须绕过：自测是用户显式动作，点第二下时若被"同一指纹冷却"
        挡掉，用户会误判"刚才通了、现在坏了"。但绕过不等于不记账：
        这条自测仍会写进 history，看板上能看见，否则"我明明点了测试"会变成悬案。
        """
        chans = list(self.channels)
        if kinds:
            chans = [c for c in chans if c.kind in kinds]
        out = {"ok": False, "skipped": False, "reason": "",
               "alert": ev.to_dict(), "channels": []}
        if not chans:
            out["reason"] = "没有匹配的可用通道"
            return out

        # 显式自测必须真的再试一次：邮件通道失败后会退避 120 秒，
        # 不清掉的话第二次点"测试邮箱通道"会显示"冷却中"，用户
        # 会以为配置又坏了（实际上根本没发）。
        for ch in chans:
            ch.reset_failures()

        results = [ch.send(ev, report_md) for ch in chans]
        out["channels"] = [r.to_dict() for r in results]
        self.last_result = out["channels"]
        delivered = [r for r in results if r.ok and not r.skipped]
        out["ok"] = bool(delivered)
        if delivered:
            self.sent += 1
        else:
            self.failed += 1
            out["reason"] = "所有被测试的通道均失败，见 channels 明细"
            self.last_error = out["reason"]
        self._remember(ev, out, delivered=bool(delivered))
        return out

    def _remember(self, ev: AlertEvent, out: dict, delivered: bool) -> None:
        rec = ev.to_dict()
        rec["delivered"] = delivered
        rec["channels"] = out.get("channels", [])
        rec["skip_reason"] = "" if delivered else out.get("reason", "")
        self.history.append(rec)
        del self.history[:-self.HISTORY_MAX]

    # ---- 状态（给 /api/alerts 用）----
    def recent(self, limit: int = 50) -> List[dict]:
        return list(reversed(self.history[-max(1, limit):]))

    def status(self) -> dict:
        return {
            "enabled": self.cfg.enabled,
            "dry_run": self.cfg.dry_run,
            "min_severity": self.cfg.min_severity,
            "cooldown_s": self.cfg.cooldown_s,
            "max_per_hour": self.cfg.max_per_hour,
            "channels": [ch.name for ch in self.channels],
            "email_ready": self.cfg.email_ready(),
            "email_problem": self.cfg.email_problem(),
            "webhook_ready": self.cfg.webhook_ready(),
            "webhook_kind": self.cfg.webhook_kind,
            "smtp_host": self.cfg.smtp_host,
            "smtp_port": self.cfg.smtp_port,
            "smtp_security": self.cfg.smtp_security,
            "smtp_user": self.cfg.smtp_user,
            "email_to": list(self.cfg.email_to),
            "sent": self.sent,
            "failed": self.failed,
            "skipped": dict(self.policy.skipped),
            "last_error": self.last_error,
            "last_result": self.last_result,
            "describe": self.cfg.describe(),
        }
