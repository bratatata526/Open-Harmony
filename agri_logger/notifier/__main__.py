#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""告警链路自测：不依赖开发板、不依赖 LLM API Key

用法：
    python -m agri_logger.notifier                 # 用当前配置真发一次测试告警
    python -m agri_logger.notifier --dry-run       # 演练：只显示会发什么
    python -m agri_logger.notifier --email-only    # 只走邮件通道
    python -m agri_logger.notifier --show-config   # 只看配置，不发任何东西

它做三件事：
  1. 打印配置体检结果（哪个通道就绪、哪个缺哪一项）
  2. 用【真实解析器 + 真实窗口聚合】造一个 P1 告警（含真实的 addr/mcause 字段）
  3. 走一遍策略 + 通道，把每个通道的结果逐条打印出来

为什么要用真实解析器造样本，而不是手写一个 AlertEvent：
  只有走同一条链路，才能同时验证"日志 → 解析 → 窗口 → 摘要 → 通道"全通。
  手写对象会把解析和摘要两个最容易出错的地方跳过去。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

# 让 `python -m agri_logger.notifier` 与直接跑脚本都能 import 到包
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agri_logger.notifier import (AlertConfig, AlertManager, build_alert,   # noqa: E402
                                 build_channels)
from agri_logger.parser.parser import LogParser                              # noqa: E402
from agri_logger.pipeline.windows import score_window, WindowAggregator       # noqa: E402

# 一条"看起来像真机"的样本流：正常上报 + 一次系统死机（带 addr/mcause）
SAMPLE_LINES = [
    "000100.000000 I A001 AgriSensor: 1 20 2 283 3 462 4 720 z=A",
    "000105.000000 I A001 AgriSensor: 1 21 2 284 3 463 4 718 z=A",
    "000110.000000 I A002 AgriReport: 1 21 9 33 z=A",
    # ↓ 系统死机：F3009 + 错误码 20(crash_task) + mcause=2(非法指令) + addr=4096(0x1000)
    "000110.500000 E A008 AgriCrash: 1 22 5 3009 6 20 17 2 18 4096 z=A",
    "000111.000000 E A003 AgriHealth: 1 23 5 3002 6 10 8 1024 z=A",
    "000116.000000 I A001 AgriSensor: 1 24 2 285 3 464 4 715 z=A",
]


class _StubAnalysis:
    """最小化的 Analysis 替身 —— 只为验证告警拼装，不承担分析职责。

    mode="rule" 是刻意的：这样测的是【规则兜底】路径，
    也就是没有 API Key 时、答辩现场断网时真正会走的那条路。
    """

    def __init__(self, key: str, label: str):
        self.cluster_key = key
        self.cluster_label = label
        self.display_name = label
        self.summary = ""
        self.severity = "P1"
        self.mode = "rule"


def build_sample_alert():
    p = LogParser()
    recs = [r for r in (p.parse(line, src_name="selftest") for line in SAMPLE_LINES) if r]
    if not recs:
        raise SystemExit("❌ 样本日志全部解析失败，解析器或字段表有问题")

    agg = WindowAggregator(window_s=60.0)
    for r in recs:
        agg.feed(r)
    windows = [score_window(w) for w in agg.finish()]
    if not windows:
        raise SystemExit("❌ 窗口聚合为空")

    w = max(windows, key=lambda x: x.anomaly_score)
    w.anomaly_score = max(w.anomaly_score, 0.5)      # 样本太小，补足到"必报"的分数
    a = _StubAnalysis("FW-3002-3009", "任务栈溢出 / 系统死机")
    ev = build_alert(w, a, rca={}, report_file="(自测，无报告文件)",
                     source="自测样本（非真机）",
                     device_name="真实设备（板载传感器）",
                     evidence=w.evidence(max_lines=4))
    return w, ev


def show_config(cfg: AlertConfig) -> None:
    print("════════ 告警配置体检 ════════")
    print(cfg.describe())
    print()
    print("  总开关        %s" % ("开" if cfg.enabled else "关"))
    print("  推送门槛      %s（P0 最严重）" % cfg.min_severity)
    print("  冷却 / 限流   %.0fs / 每小时 %d 条" % (cfg.cooldown_s, cfg.max_per_hour))
    if cfg.file_enabled:
        print("  文件通道      %s" % cfg.file_path)
    if cfg.webhook_ready():
        print("  Webhook       ✅ %s（%s）"
              % (cfg.webhook_url, cfg.webhook_kind or "auto"))
    else:
        print("  Webhook       ❌ 未配置（.env 里设 ALERT_WEBHOOK_URL）")
    if cfg.email_ready():
        print("  邮件通道      ✅ 就绪  %s:%d(%s)  %s → %s"
              % (cfg.smtp_host, cfg.smtp_port, cfg.smtp_security,
                 cfg.smtp_user, ", ".join(cfg.email_to)))
    else:
        print("  邮件通道      ❌ %s" % cfg.email_problem())
    print()


def main(argv=None) -> int:
    # Windows 默认可能仍是 cp936；告警诊断输出含中文和符号，先统一为 UTF-8，
    # 避免 --show-config / --dry-run 在中文控制台中因 UnicodeEncodeError 中断。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="告警链路自测")
    ap.add_argument("--dry-run", action="store_true",
                    help="演练模式：只显示会发什么，不真的发")
    ap.add_argument("--email-only", action="store_true",
                    help="只走邮件通道（其余通道关闭），用于单独验证 SMTP")
    ap.add_argument("--show-config", action="store_true",
                    help="只打印配置体检，不发任何告警")
    ap.add_argument("--env", default=str(_ROOT / ".env"),
                    help="配置文件路径（默认仓库根目录 .env）")
    args = ap.parse_args(argv)

    env_file = Path(args.env)
    cfg = AlertConfig.from_env(env_file)
    if args.dry_run:
        cfg.dry_run = True
    if args.email_only:
        cfg.console = False
        cfg.file_enabled = False
        cfg.webhook_url = ""
        cfg.email_enabled = True

    show_config(cfg)
    if args.show_config:
        return 0

    if not cfg.enabled:
        print("⚠️  告警总开关是关闭的（ALERT_ENABLED=0），不会发出任何告警。")
        print("   要测试请先把它改成 1，或用 --show-config 只看配置。")
        return 0

    if not build_channels(cfg):
        print("❌ 没有任何可用通道。至少应保留控制台或文件通道。")
        return 1

    print("── 1) 造一条样本告警（真实解析器 + 真实窗口聚合）──")
    w, ev = build_sample_alert()
    print("   窗口 %s ｜ 异常分 %.2f ｜ %d 行 ｜ 触发信号 %d 条"
          % (w.window_id, w.anomaly_score, w.line_count, len(w.anomaly_reasons)))
    print("   设备 %s ｜ 故障码 %s" % (ev.device, dict(w.fault_count)))
    print()

    print("── 2) 生成的告警内容（这就是运维会看到的）──")
    print("   标题：%s" % ev.title)
    print("   摘要：%s" % ev.summary)
    print("   级别：%s ｜ 引擎：%s" % (ev.severity, ev.mode))
    print()

    print("── 3) 过策略 + 逐通道发送 ──")
    mgr = AlertManager(cfg)
    t0 = time.time()
    res = mgr.send(ev)
    if res.get("skipped"):
        print("   被策略拦下：%s" % res.get("reason"))
    for c in res.get("channels", []):
        mark = "✅" if c["ok"] and not c["skipped"] else ("⏭️" if c["skipped"] else "❌")
        print("   %s %-9s %s" % (mark, c["channel"], c["detail"]))
    print("   耗时 %.0f ms" % ((time.time() - t0) * 1000))
    print()

    print("── 4) 再发一次同样的告警（验证去重）──")
    res2 = mgr.send(ev)
    print("   %s：%s" % ("已去重 ✅" if res2.get("skipped") else "重复发出了 ❌",
                        res2.get("reason") or "无原因"))
    print()

    ok = bool(res.get("ok"))
    if cfg.dry_run:
        # 演练模式所有通道都返回 skipped=True，不能按"没发出去=失败"算；
        # 这里只验证"负载能造出来、通道能走到"，避免用户看到假的 ❌。
        channels = res.get("channels", [])
        ok = bool(channels) and all(bool(c.get("ok")) for c in channels)
    print("════════ 结论 ════════")
    print("告警链路：%s"
          % ("✅ 演练通过（未实际发送）" if cfg.dry_run and ok
             else "✅ 通" if ok else "❌ 不通，见上面每个通道的失败原因"))
    if not cfg.email_ready():
        print("提示：邮件通道未就绪 —— %s" % cfg.email_problem())
        print("      在 .env 里补齐后重跑 `python -m agri_logger.notifier --email-only` 即可验证。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
