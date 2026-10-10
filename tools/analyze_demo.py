#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端演示：日志文件 → 解析 → 窗口聚合 → 异常识别 → LLM 根因报告

这是周一中期汇报要现场跑的东西。**没有 API Key 也能跑**（自动降级到规则引擎），
这样即使汇报现场网络不通，也照样有产出。

用法：
  # 用模拟数据集（不需要板子）
  .venv\\Scripts\\python.exe tools/analyze_demo.py --input eval/dataset/d2_v2.log

  # 用真机采集的日志
  .venv\\Scripts\\python.exe tools/analyze_demo.py --input logs/multi.log

  # 只看统计不调 LLM（零成本，用于快速验证）
  .venv\\Scripts\\python.exe tools/analyze_demo.py --input eval/dataset/d2_v2.log --no-llm

  # 限制报告数量（省成本）
  .venv\\Scripts\\python.exe tools/analyze_demo.py --input eval/dataset/d2_v2.log --max-reports 3
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agri_logger.analyzer.llm_client import LLMClient           # noqa: E402
from agri_logger.analyzer.rca import (                          # noqa: E402
    AnomalyAnalyzer, EvidenceValidator, Report, cluster_families)
from agri_logger.parser.parser import LogParser                 # noqa: E402
from agri_logger.pipeline.windows import aggregate_and_score    # noqa: E402


def hr(title: str) -> None:
    print(f"\n{'═' * 74}\n  {title}\n{'═' * 74}")


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="端到端日志智能分析演示")
    ap.add_argument("--input", required=True, help="日志文件路径")
    ap.add_argument("--out-dir", default="reports", help="报告输出目录")
    ap.add_argument("--window", type=float, default=60.0, help="窗口长度（秒）")
    ap.add_argument("--threshold", type=float, default=0.25, help="异常判定阈值")
    ap.add_argument("--max-reports", type=int, default=3, help="最多生成几份报告（省成本）")
    ap.add_argument("--no-llm", action="store_true", help="不调 LLM，只用规则引擎")
    ap.add_argument("--budget", type=float, default=5.0, help="LLM 日预算（元）")
    args = ap.parse_args(argv)

    # ---------------- 1. 解析 ----------------
    hr("① 解析日志")
    src = Path(args.input)
    if not src.exists():
        print(f"❌ 文件不存在：{src}")
        return 1

    parser = LogParser()
    records = []
    t0 = time.time()
    for line in src.read_text(encoding="utf-8", errors="replace").splitlines():
        rec = parser.parse(line, src_name=src.name)
        if rec:
            records.append(rec)
    dt = time.time() - t0

    print(f"文件          {src}")
    print(f"解析成功      {len(records)} 条（{dt:.2f}s，{len(records)/max(dt,1e-9):.0f} 条/秒）")
    print(parser.report())

    if not records:
        print("\n❌ 没有解析出任何记录，请检查日志格式")
        return 1

    # ---------------- 2. 窗口聚合 + 异常打分 ----------------
    hr("② 窗口聚合与异常打分")
    windows, anomalies = aggregate_and_score(records, window_s=args.window,
                                             threshold=args.threshold)
    print(f"窗口长度      {args.window:.0f} 秒")
    print(f"窗口总数      {len(windows)}")
    print(f"异常窗口      {len(anomalies)}（阈值 {args.threshold}）")
    print(f"压缩比        {len(records)} 行 → {len(anomalies)} 个窗口"
          f"（送 LLM 的数据量降 {100 * (1 - len(anomalies) / max(1, len(records))):.2f}%）")

    print("\n异常窗口排行：")
    print(f"  {'窗口':<8}{'分数':>7}{'行数':>7}{'E':>4}{'W':>4}  {'最大间隔':>9}  设备")
    for w in sorted(anomalies, key=lambda x: -x.anomaly_score)[:10]:
        print(f"  {w.window_id:<8}{w.anomaly_score:>7.3f}{w.line_count:>7}"
              f"{w.err_count:>4}{w.warn_count:>4}  {w.gap_max:>8.1f}s  {','.join(w.zones)}")
        for r in w.anomaly_reasons[:2]:
            print(f"           · {r}")

    if not anomalies:
        print("\n✅ 未发现异常窗口")
        return 0

    # ---------------- 3. LLM 客户端 ----------------
    hr("③ LLM 可用性检查")
    llm = LLMClient(budget_cny_per_day=args.budget,
                    enabled=not args.no_llm)
    if args.no_llm:
        print("已指定 --no-llm，全部使用规则引擎（零成本）")
    elif llm.available:
        print("✅ LLM 可用，将进行语义分析与根因推理")
        print(f"   模型 {llm.model} ｜ 预算 {args.budget} 元/天")
    else:
        print("⚠️ LLM 不可用，自动降级到规则引擎：")
        for line in (llm.init_error or "").split("\n"):
            print(f"   {line}")
        print("\n   （这是正常的——降级模式本身就是要演示的能力之一）")

    # ---------------- 4. 分析 + 生成报告 ----------------
    hr("④ 异常分析与报告生成")
    analyzer = AnomalyAnalyzer(llm)
    validator = EvidenceValidator(records)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    reports: list[Report] = []
    targets = sorted(anomalies, key=lambda x: -x.anomaly_score)[:args.max_reports]

    # 先做【确定性聚类】：按故障码集合的相似度把异常窗口聚成族。
    # 与 LLM 措辞无关，所以结果可复现；LLM 只负责给族起名字。
    families = cluster_families(anomalies)
    fam_count = {f["key"]: f["count"] for f in families}
    # 窗口 -> 所属族的键（报告里要写"本族出现几次"）
    win_fam = {}
    for f in families:
        for wid in f["windows"]:
            win_fam[wid] = f["key"]

    print("\n异常族聚合（按故障码相似度聚类，与 LLM 措辞无关）：")
    for f in families:
        print(f"  {f['key']:<26} ×{f['count']} 窗口   {f['span']}   "
              f"涉及 {len(f['zones'])} 个 zone   E{f['err']}/W{f['warn']}")
        print(f"      窗口 {', '.join(f['windows'])}")
    print()

    for w in targets:
        print(f"\n── {w.window_id}  分数={w.anomaly_score:.3f} ──")
        t1 = time.time()

        a = analyzer.analyze_window(w, records)
        fam_key = win_fam.get(w.window_id, a.cluster_key)
        print(f"  L1 判定  {a.badge}  族={a.display_name}  指纹={fam_key}"
              f"  本族共 {fam_count.get(fam_key, 1)} 个窗口")
        print(f"           {a.summary}")

        rca, usage = analyzer.analyze_root_cause(w, a, records)
        analyzer.usage.add(usage)

        # ★ 证据校验：LLM 给的编号必须真实存在
        ev = validator.validate(a.evidence_refs)
        if not ev:
            ev = w.evidence(max_lines=15)          # LLM 没给或给错，用程序挑的
        print(f"  L2 根因  {(rca.get('suspected_root_cause') or '')[:60]}...")
        print(f"  证据     LLM 引用 {len(a.evidence_refs)} 条 → 校验通过 {len(ev)} 条")
        print(f"  耗时     {time.time() - t1:.1f}s")

        mode = a.mode if a.mode == "llm" else "rule"
        a.cluster_key = fam_key                     # 报告里写族的键，不写单窗口指纹
        rep = Report(window=w, analysis=a, evidence=ev, rca=rca,
                     usage=usage, mode=mode,
                     family_count=fam_count.get(fam_key, 1),
                     generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                     data_source="日志文件 %s" % src,
                     llm_model=getattr(llm, "model", "") if a.mode == "llm" else "")
        md = rep.to_markdown(validator)
        safe = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_",
                      f"{w.window_id}_{a.display_name or fam_key}")[:60]
        fp = out_dir / f"{safe}.md"
        fp.write_text(md, encoding="utf-8")
        reports.append(rep)
        print(f"  报告     {fp}")

    # ---------------- 5. 汇总 ----------------
    hr("⑤ 汇总")
    print(f"生成报告      {len(reports)} 份 → {out_dir}/")
    print(f"证据校验      {validator.report()}")

    if llm.usage_today.calls or llm.usage_today.cached:
        print()
        print(llm.cost_report())
    else:
        print("\nLLM 调用      0 次（规则引擎模式，零成本）")

    if reports:
        print(f"\n{'─' * 74}")
        print("  报告摘要预览（第一份）")
        print(f"{'─' * 74}")
        print(reports[0].to_markdown(validator)[:1600])
        print(f"{'─' * 74}")
        print(f"\n完整报告见：{out_dir}/")

    return 0


if __name__ == "__main__":
    sys.exit(main())
