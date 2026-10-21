#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志智能聚合分析 —— 可视化看板后端（零依赖，仅用标准库）

================================================================================
为什么用标准库 http.server 而不是 FastAPI/Flask
  · 项目跑在 Python 3.8 虚拟环境里，装框架要额外几 MB 依赖
  · 答辩现场最怕"装包失败/版本冲突"——零依赖 = 零风险
  · 本看板是本地单机只读展示，不需要异步/高并发
  · 顺带说明：这符合课题"轻量级"的定位
================================================================================

启动：
  .venv\\Scripts\\python.exe tools\\dashboard_server.py
  # 然后浏览器打开 http://127.0.0.1:8080

数据源（自动挑选，可用 --log 指定）：
  优先 eval/dataset/d2_v2.log（30 分钟、148 次故障注入、带标注）
  回退 logs/multi.log（真机采集）

API：
  GET /api/meta            概览：数据源、解析统计、时间范围
  GET /api/summary         各级别/故障码/模块/设备分布
  GET /api/timeline        时间线：按秒聚合的级别计数（供折线图）
  GET /api/zones           6 个大棚的环境曲线
  GET /api/windows         窗口列表与异常分数
  GET /api/logs            日志明细（支持 level/tag/zone/q/limit/offset）
  GET /api/reports         已生成报告列表
  GET /api/report?name=xx  单份报告 Markdown 原文
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import threading
import time
from collections import Counter, defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import sys
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agri_logger.parser.parser import LogParser, LEVEL_ORDER      # noqa: E402
from agri_logger.parser import fields as F                        # noqa: E402
from agri_logger.pipeline.windows import aggregate_and_score      # noqa: E402
from agri_logger.analyzer.rca import (window_signature, cluster_families,     # noqa: E402
                                      AnomalyAnalyzer, EvidenceValidator, Report)
from agri_logger.analyzer.llm_client import LLMClient                        # noqa: E402

# ---------------------------------------------------------------------------
# 自动分析：实时模式下【检测到新的异常窗口】就调 LLM 生成本窗口的报告
#
# 为什么要有它：需求是"检测到异常 → 给出该异常的根因报告"，而不是"等人点按钮"。
# 但必须防止失控烧钱/刷屏，所以有三道闸：
#   ① 只分析【已闭合】的窗口（最后一个是还在增长的，先不动）
#   ② 同一窗口只出一份（window_id 去重）
#   ③ 同一聚合指纹在冷却期内不重复出报告（防止同一故障每 60 秒刷一份）
# ---------------------------------------------------------------------------
AUTO_ANALYZE = True          # 总开关；命令行 --no-auto-analyze 可关
AUTO_SCAN_BATCH = 5          # 每轮最多排入几个新窗口（防积压时一次烧掉大量预算）
AUTO_FP_COOLDOWN_S = 180.0   # 同一指纹的冷却时间（秒）；设 0 = 不冷却

WEB_DIR = ROOT / "web"
REPORT_DIRS = [ROOT / "reports_live", ROOT / "reports_llm", ROOT / "reports"]
LIVE_REPORT_DIR = ROOT / "reports_live"

# 报告来源标注：**必须一眼看出这份结论是"真机数据"还是"模拟数据集"算出来的**。
# 数据是真的、结论却是旧的，比数据本身是假的更危险——现场演示时最容易翻车。
REPORT_SOURCE = {
    "reports_live": ("真机实时", "live"),
    "reports_llm": ("模拟数据集", "sim"),
    "reports": ("模拟数据集", "sim"),
}

# ⚠️ 本项目跑在 Python 3.8 上，**不能用 3.9+/3.10+ 的语法**：
#    · 不能用 `list[str]` / `dict[str, int]` 作为运行期类型（annotations 已 postponed，注解位置可以）
#    · 绝对不能用 `X | None`（3.10+ 才有）—— 这里统一用 Optional
# 这个坑在 docs/09 里有记录。

# ---------------------------------------------------------------------------
# 数据仓库：启动时加载一次，之后全部走内存（只读展示）
# ---------------------------------------------------------------------------

class DataStore:
    def __init__(self, log_path: Path, window_s: float = 60.0, threshold: float = 0.25):
        self.log_path = log_path
        self.window_s = window_s
        self.threshold = threshold
        self.lock = threading.Lock()
        self.loaded_at = time.time()

        self.parser = LogParser()
        self.records: list = []
        self.windows: list = []
        self.anomalies: list = []
        self.meta: dict = {}
        self.summary: dict = {}
        self.timeline: list = []
        self.zone_curves: dict = {}
        self.live: dict = {"enabled": False}
        self._view_cache: dict = {}     # 单设备视图缓存（zone -> 派生视图）
        self._live_views_n = 0          # 上次重算派生视图时的记录数
        self._live_views_t = 0.0        # 上次重算时刻（节流用）
        self._live_proc = None          # 实时数据源子进程（退出时要收尸）
        self._live_stop = False         # 通知看护循环退出
        self._live_gen = 0              # generation：强制重连时让旧线程自动退出
        # 数据流世代号：records 被清空/重启就 +1。
        # 前端靠它判断"我手里的游标还算不算数"，避免重启后出现重复日志。
        self._live_epoch = 0
        self._live_thread = None
        self._refresh_thread = None     # 独立刷新节拍（不依赖浏览器是否开着）
        self._live_port = ""
        self._live_baud = 115200
        self._live_sim = False
        self._bad_samples: list = []    # 实时模式下解析失败的原始行（最多留 5 条）
        self.analysis: dict = {"running": False, "done": False, "started": 0.0}
        # ---- 自动分析状态（检测到异常 → 出报告）----
        self._auto_q: deque = deque()          # 待分析窗口队列
        self._auto_done: set = set()           # 已排入/已出过报告的窗口号（去重）
        self._auto_fp_at: dict = {}            # 聚合指纹 -> 上次出报告时刻（冷却）
        self._auto_busy = False                # 工作线程是否在跑
        self.auto_state = {"queued": 0, "done": 0, "skipped": 0, "error": 0}
        self._load()

    # ---------------- 加载 ----------------
    def _load(self) -> None:
        t0 = time.time()
        self.records = []
        for line in self.log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            rec = self.parser.parse(line, src_name=self.log_path.name)
            if rec:
                self.records.append(rec)
            else:
                self._bad_samples.append(line)
                del self._bad_samples[:-5]
        parse_dt = time.time() - t0

        self.windows, self.anomalies = aggregate_and_score(
            self.records, window_s=self.window_s, threshold=self.threshold)

        ts = [r.ts_device for r in self.records if r.ts_device is not None]
        self.meta = {
            "log_path": str(self.log_path.relative_to(ROOT)),
            "log_size_kb": round(self.log_path.stat().st_size / 1024, 1),
            "records": len(self.records),
            "parse_ok": self.parser.stats["ok"],
            "parse_bad": self.parser.stats["bad"],
            "parse_rate": round(self.parser.stats["ok"] / max(1, len(self.records)
                                                              + self.parser.stats["bad"]) * 100, 2),
            "parse_bad_reasons": dict(self.parser.bad_reasons),
            "parse_bad_samples": list(self._bad_samples),
            "longest_line": self.parser.longest_line,
            "over_limit": self.parser.over_limit,
            "duration_s": round(max(ts) - min(ts), 1) if ts else 0.0,
            "windows": len(self.windows),
            "anomaly_windows": len(self.anomalies),
            "load_ms": round(parse_dt * 1000),
            "loaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._build_summary()
        self._build_timeline()
        self._build_zone_curves()

    def device_mode(self) -> dict:
        """读设备当前的运行模式 —— 来自固件发的 AgriMode 日志行。

        为什么要在看板上显示：演示时最怕"到底开没开故障注入"说不清。
        设备端把它写进日志、OLED 上也显示，看板再显示一次，三处一致。
        按键切换后设备会立刻补发一条 AgriMode，所以看板 3 秒内就能跟上。
        """
        for r in reversed(self.records):
            if r.tag == "AgriMode":
                v = r.values.get(11)
                if v is None:
                    continue
                return {
                    "known": True,
                    "injecting": (v == 1),
                    "label": "故障注入中" if v == 1 else "纯真实数据",
                    "ts": r.ts_device,
                }
        return {"known": False, "injecting": None, "label": "未知（固件未上报）"}

    def families(self, zone: str = "") -> list:
        """把异常窗口按【聚合指纹】聚成异常族 —— 这就是"智能聚合"的可见结果。

        相邻窗口常常是同一个故障模式的重复出现（注入器按场景轮转），
        不聚合的话报告会一份一份重复地写，看不出"这是个反复发生的问题"。
        聚类由 cluster_families() 确定性完成，所以结果可复现、
        与 LLM 的措辞无关，LLM 挂掉也一样准。
        """
        return cluster_families(self.anomalies)

    # ---------------- 汇总 ----------------
    def _build_summary(self, records=None) -> dict:
        records = self.records if records is None else records
        lv = Counter(r.level for r in records)
        dom = Counter(r.domain for r in records)
        zone = Counter(r.zone for r in records if r.zone)
        faults = Counter(r.fault_tag for r in records if r.fault_tag)
        tags = Counter(r.tag for r in records)

        return {
            "levels": {k: lv.get(k, 0) for k in ("D", "I", "W", "E", "F")},
            "domains": [{"domain": d, "name": F.DOMAINS.get(d, "?"), "count": c}
                        for d, c in dom.most_common()],
            "zones": [{"zone": z, "count": c} for z, c in sorted(zone.items())],
            "tags": [{"tag": t, "count": c} for t, c in tags.most_common()],
            "faults": [{
                "tag": t, "code": int(t[1:]),
                "name": F.fault_name(int(t[1:])),
                "count": c,
                "normal": F.is_normal_event(int(t[1:])),
            } for t, c in faults.most_common()],
        }

    # ---------------- 时间线（按秒聚合）----------------
    def _build_timeline(self, records=None) -> list:
        records = self.records if records is None else records
        buckets: Dict[int, Counter] = defaultdict(Counter)
        for r in records:
            if r.ts_device is None:
                continue
            buckets[int(r.ts_device)][r.level] += 1
        if not buckets:
            return []
        lo, hi = min(buckets), max(buckets)
        # 采样：控制在 ~240 个点以内，保证前端流畅
        step = max(1, (hi - lo + 1) // 240)
        out = []
        for s in range(lo, hi + 1, step):
            agg = Counter()
            for k in range(s, min(s + step, hi + 1)):
                agg.update(buckets.get(k, {}))
            out.append({"t": s, "I": agg.get("I", 0), "W": agg.get("W", 0),
                        "E": agg.get("E", 0) + agg.get("F", 0), "step": step})
        return out

    # ---------------- 6 个大棚环境曲线 ----------------
    def _build_zone_curves(self, records=None) -> dict:
        records = self.records if records is None else records
        series: Dict[str, Dict[str, list]] = {
            z: {"t": [], "temp": [], "humi": [], "light": []} for z in "ABCDEF"
        }
        for r in records:
            if not r.zone or r.zone not in series or r.ts_device is None:
                continue
            # ★★★ 只收【环境读数行】，绝不收【故障报告行】★★★
            # 踩过的坑：故障注入器为了标记"数据已失效"，会故意发一条
            # temp=-1270（-127.0℃，物理不可能值）的行。如果按"有温度字段就收"
            # 的写法，这个标记值会被当成真实读数画进曲线，
            # 界面上就出现"A 棚 -127.0℃"这种荒谬结果 —— 现场演示直接翻车。
            # 判据：带故障码字段(5)的行是【故障报告】，不是环境读数。
            if 5 in r.values:
                continue
            t, h = r.temp, r.humi
            if t is None and h is None:
                continue
            # 第二道防线：物理合理范围。传感器量程外的值一律不画。
            # 注意 parser 已经把 ×10 的原始值换算成物理量了：
            #   temp 单位 ℃（317 -> 31.7），humi 单位 %（463 -> 46.3）
            if t is not None and not (-40.0 <= t <= 85.0):      # 常见环境温区
                continue
            if h is not None and not (0.0 <= h <= 100.0):       # 相对湿度
                continue
            s = series[r.zone]
            # 抽样：每 10 秒一个点，避免上万点拖慢前端
            if s["t"] and r.ts_device - s["t"][-1] < 10:
                continue
            s["t"].append(round(r.ts_device, 1))
            s["temp"].append(t if t is not None else None)
            s["humi"].append(h if h is not None else None)
            s["light"].append(r.values.get(4))

        out = {}
        for z, s in series.items():
            if s["t"]:
                out[z] = {
                    "name": self._zone_label(z, records),
                    "t": s["t"], "temp": s["temp"], "humi": s["humi"], "light": s["light"],
                }
        return out

    @staticmethod
    def _zone_name(z: str) -> str:
        return {"A": "1号棚·向阳主棚", "B": "2号棚·背阴棚", "C": "3号棚·旧棚膜",
                "D": "4号棚·新棚膜", "E": "5号棚·近水渠", "F": "6号棚·大棚"}.get(z, f"{z} 棚")

    # 单设备模式（固件 ZONE_COUNT=1）下，这个"设备"就是开发板本身，
    # 再叫它"1号棚·向阳主棚"是错的 —— 会让人以为还有别的棚。
    SINGLE_DEVICE_NAME = "真实设备（板载 SHT20 + AP3216C）"

    def _zone_label(self, z: str, records: list) -> str:
        """按实际出现的设备数量决定怎么称呼。只有一个设备时用真实设备名。"""
        present = set(r.zone for r in records if r.zone and r.zone != "-")
        if len(present) == 1 and z in present:
            return self.SINGLE_DEVICE_NAME
        return self._zone_name(z)

    # ---------------- 单设备（zone）视图 ----------------
    def view(self, zone: str = "") -> dict:
        """把整块看板聚焦到【一个设备（棚）】上，或看全部。

        为什么需要：真实运维是按设备看的。"6 个棚的整体统计"看不出
        "3 号棚今天特别不对"，必须能切到单设备视角。
        单设备视角下窗口/时间线/异常族全部只由这个棚的日志算出，
        所以异常判定不会被别的棚的日志摊薄。

        带 2 秒缓存：前端三秒一刷，没必要每次重算。
        """
        zone = (zone or "").strip().upper()
        if zone in ("", "ALL", "-"):
            zone = ""
        key = zone or "ALL"
        with self.lock:
            n = len(self.records)
        c = self._view_cache.get(key)
        if c is not None and c["n"] == n and (time.time() - c["t"]) < 2.0:
            return c

        if not zone:
            recs = list(self.records)
            windows, anomalies = self.windows, self.anomalies
        else:
            # ★ 全局性质的日志（z=-，如健康检查、启动）不算任何单个设备的，
            #   否则"这个棚的日志"里会混进整机事件，异常判定被污染。
            recs = [r for r in self.records if r.zone == zone]
            windows, anomalies = aggregate_and_score(
                recs, window_s=self.window_s, threshold=self.threshold)

        ts = [r.ts_device for r in recs if r.ts_device is not None]
        meta = {
            "zone": zone or "ALL",
            "zone_name": (self._zone_label(zone, recs) if zone
                          else ("真实设备（单设备模式）" if len(
                              set(r.zone for r in recs if r.zone and r.zone != "-")) == 1
                                else "全部设备（6 个棚）")),
            "records": len(recs),
            "duration_s": round(max(ts) - min(ts), 1) if ts else 0.0,
            "windows": len(windows),
            "anomaly_windows": len(anomalies),
            "longest_line": self.meta.get("longest_line", 0),
        }

        out = {
            "n": n, "t": time.time(), "zone": zone or "ALL",
            "records": recs,
            "meta": meta,
            "summary": self._build_summary(recs),
            "timeline": self._build_timeline(recs),
            "zone_curves": self._build_zone_curves(recs),
            "windows": windows,
            "anomalies": anomalies,
            "families": cluster_families(anomalies),
        }
        self._view_cache[key] = out
        return out

    def windows_payload(self, windows=None) -> list:
        out = []
        for w in (self.windows if windows is None else windows):
            out.append({
                "id": w.window_id,
                "start": round(w.start_ts, 1),
                "end": round(w.end_ts, 1),
                "lines": w.line_count,
                "err": w.err_count,
                "warn": w.warn_count,
                "info": w.info_count,
                "score": w.anomaly_score,
                "gap_max": round(w.gap_max, 2),
                "seq_gap": w.seq_gap_total,
                "zones": w.zones,
                "faults": [{"tag": t, "name": F.fault_name(int(t[1:])), "count": c,
                            "normal": F.is_normal_event(int(t[1:]))}
                           for t, c in w.fault_count.most_common()],
                "reasons": w.anomaly_reasons,
                "anomaly": w.anomaly_score >= self.threshold,
            })
        return out

    # ---------------- 日志明细 ----------------
    def query_logs(self, level: str = "", tag: str = "", zone: str = "",
                   q: str = "", limit: int = 300, offset: int = 0) -> dict:
        minlv = LEVEL_ORDER.get(level.upper(), -1) if level else -1
        pat = re.compile(q, re.IGNORECASE) if q else None

        hits = []
        for r in self.records:
            if minlv >= 0 and LEVEL_ORDER.get(r.level, 0) < minlv:
                continue
            if tag and r.tag != tag:
                continue
            if zone and r.zone != zone:
                continue
            if pat and not pat.search(r.raw_line):
                continue
            hits.append(r)

        total = len(hits)
        page = hits[offset:offset + limit]
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "items": [{
                "eid": r.evidence_id,
                "ts": r.ts_device,
                "level": r.level,
                "domain": r.domain,
                "tag": r.tag,
                "zone": r.zone,
                "seq": r.seq,
                "fault": r.fault_tag,
                "fault_name": r.fault_name,
                "detail": r.detail,
                "raw": r.raw_line,
                "physical": r.physical,
            } for r in page],
        }

    # ---------------- 实时模式（真机串口 / 模拟器）----------------
    def start_live(self, port: str, baud: int, sim: bool) -> None:
        """启动实时数据源。

        为什么要有这个模式：
          默认模式读【静态文件】并【循环回放】——这是为了演示算法效果（可复现、带标注）。
          但它会让人误以为"连着真机"，因为关掉板子日志流照样在动。
          实时模式直接读串口（或模拟器输出），板子一停，数据源就断——
          **界面上会明确显示 ONLINE / OFFLINE**，不会再产生误解。

        port 传空字符串 = 自动发现，并且【每次重连都重新发现】——
        板子换 USB 口导致 COM 号变化时，看板能自己跟过去。
        """
        # ★ 清空启动时读入的静态记录：实时模式就只显示实时数据，
        #   否则会把"基底回放"和"实时新增"混在一起，反而更让人困惑。
        #   ★ records 一清空，世代号必须 +1 —— 前端靠它知道"你的游标作废了"，
        #     否则游标倒退会导致日志重复显示（见 live_logs 的说明）。
        with self.lock:
            self.records = []
            self._live_epoch += 1
        self.parser = LogParser()           # 重置解析统计，只统计实时数据

        self.live = {
            "enabled": True, "source": port or "自动发现",
            "online": False, "lines": 0, "started": time.time(),
            "error": "", "last_recv": 0.0, "sim": sim, "attempt": 0,
            "reconnects": 0,
        }
        self._live_views_n = 0
        self._live_views_t = 0.0
        self._live_port, self._live_baud, self._live_sim = port, baud, sim
        self._spawn_live()

    def _spawn_live(self) -> None:
        """起一个采集线程。用 generation 计数保证旧线程一定会退出。

        为什么需要 generation：强制重连时先让旧线程停、再起新线程。
        只用一个 _live_stop 布尔量会有竞态——旧线程可能刚好走到
        while 判断之前，看到已经被重置成 False 就又转一圈，
        于是两个线程同时读同一个串口。用递增的代号就干净了：
        代号一变，旧线程下一轮必然退出。
        """
        self._live_gen += 1
        gen = self._live_gen
        self._live_stop = False
        self._live_thread = threading.Thread(
            target=self._live_loop,
            args=(gen, self._live_port, self._live_baud, self._live_sim),
            daemon=True)
        self._live_thread.start()
        self._ensure_refresh_thread()      # 顺带把"刷新+自动分析"的节拍拉起来

    def _kill_live_proc(self, wait_s: float = 3.0) -> None:
        p = self._live_proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except Exception:                                    # noqa: BLE001
                pass
            t0 = time.time()
            while p.poll() is None and time.time() - t0 < wait_s:
                time.sleep(0.05)
            if p.poll() is None:
                try:
                    p.kill()
                except Exception:                                # noqa: BLE001
                    pass
        self._live_proc = None

    def restart_live(self) -> dict:
        """强制重连：立刻放弃当前数据源、重新发现端口并重开。

        为什么需要它：
          子进程（serial_capture.py）自带断线重连，但它是**钉死在一个端口号**上重试的。
          板子拔了再插，Windows 很可能把它枚举成别的 COM 号（实测 COM3→COM4→COM5→COM3），
          子进程却还在死磕那个已经不存在的口，于是"插回去也接不上"。
          这个函数把子进程连根拔掉、重新发现端口，是现场最可靠的兜底手段。

        ★ 保留已经收到的记录，不清空 —— 重连不应该丢掉前面的数据。
        """
        if not self.live.get("enabled"):
            return {"ok": False, "reason": "当前不是实时模式"}

        self._live_stop = True                 # 让旧线程下一轮退出
        self._kill_live_proc()                 # 并把子进程杀掉，别让它继续霸占串口
        th = getattr(self, "_live_thread", None)
        if th is not None:
            th.join(timeout=3.0)

        self.live["online"] = False
        self.live["error"] = "正在强制重连…"
        self.live["attempt"] = 0
        self.live["reconnects"] = self.live.get("reconnects", 0) + 1
        self.live["last_recv"] = 0.0
        self.live["started"] = time.time()
        if not self._live_sim:
            self.live["source"] = self._live_port or detect_serial_port() or "自动发现"
        self._spawn_live()

        return {"ok": True, "source": self.live.get("source"),
                "reconnects": self.live["reconnects"]}

    def _live_loop(self, gen: int, port: str, baud: int, sim: bool) -> None:
        """实时数据源的看护循环（一个 generation 一个实例）。

        数据源退出后【不立刻收工】，而是隔 5 秒重试一次——
        这样"拔线 → 插回"或"板子重启"之后看板能自己恢复，
        不用把看板也重启一遍（否则现场演示很容易翻车）。
        重试期间 live_status() 照实报 offline，不会假装在线。

        ★ 端口在【每一轮重连时重新发现】。Windows 按物理 USB 插口分配 COM 号，
          板子从 COM3 换到 COM4（或换回来）是常事；只在启动时探测一次的话，
          换口之后看板会拿着旧端口号一直重试，永远连不上。

        ★ 子进程一律传 `--port auto`（除非用户用 --com 明确指定）。
          这一点是踩坑改出来的：子进程自己也有重连循环，它的重试退避最长到 30 秒，
          而且如果给它一个写死的端口号，它就只会死磕那个号。
          交给它 auto，它每轮重试都会重新枚举端口，换号也能跟上。
          看板外层的 5 秒循环作为第二道保险。
        """
        import subprocess

        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        attempt = 0
        while not self._live_stop and gen == self._live_gen:
            attempt += 1
            self.live["attempt"] = attempt

            if sim:
                cmd = [sys.executable, "-u", str(ROOT / "tools" / "openharmony_log_sim.py"),
                       "--speed", "1", "--tick-ms", "1000", "--period", "5"]
            else:
                # 空字符串 = 让采集器自己发现；给了具体号才钉死
                use_port = port or "auto"
                self.live["source"] = use_port if use_port != "auto" else (
                    detect_serial_port() or "自动发现")
                # 直接读串口：用项目自己的采集器，保证 DTR/RTS 配置一致
                cmd = [sys.executable, "-u", str(ROOT / "tools" / "serial_capture.py"),
                       "--port", use_port, "--baud", str(baud), "--min-level", "D", "--raw"]
            cwd = str(ROOT)
            self.live["cmd"] = " ".join(cmd)

            # ★ 两个必须注意的点（都是实测踩出来的）：
            #   1) 子进程要加 `-u`。Python 的 stdout 在【非终端】下是块缓冲
            #      （8KB 才刷一次），而交互式运行时是行缓冲。少了 `-u`，
            #      日志会憋在子进程缓冲区里几十秒不吐出来，看板看起来"离线"。
            #   2) stderr 必须丢弃（DEVNULL），不能设成 PIPE。
            #      子进程往 stderr 写启动横幅，PIPE 没人读，缓冲区写满后
            #      子进程会【阻塞在 write 上】→ 再也产不出数据。
            try:
                proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, bufsize=1,
                                        universal_newlines=True, encoding="utf-8",
                                        errors="replace", env=env)
                self._live_proc = proc
                self.live["pid"] = proc.pid
                # ★ 这里【不】把 online 置 True。
                #   online 的语义是"正在收到数据"，不是"进程起来了"。
                #   之前在这里乐观置 True，会出现"界面显示在线、其实一个字节都没来"
                #   的假象（最长持续 6 秒），排查时很误导人。
                #   现在只有真正解析出一条记录才会置 True（见下面 rec 分支）。
                self.live["error"] = ""
                self.live["last_recv"] = 0.0

                assert proc.stdout is not None
                for line in proc.stdout:
                    line = line.rstrip("\r\n")
                    if not line:
                        continue
                    rec = self.parser.parse(line, src_name="live")
                    if rec:
                        with self.lock:
                            self.records.append(rec)
                            self.live["lines"] += 1
                            self.live["last_recv"] = time.time()
                            self.live["online"] = True
                    else:
                        # 解析失败的行【留证据】。真机上解析率不是 100% 是正常的
                        # （上电瞬间的 ROM 启动日志就不是我们的格式），
                        # 但要能当场拿出原始行来说明"失败的到底是什么"，
                        # 而不是含糊地说"有几条没解析出来"。
                        with self.lock:
                            self._bad_samples.append(line)
                            del self._bad_samples[:-5]
                proc.wait()
                if self._live_stop or gen != self._live_gen:
                    break
                self.live["online"] = False
                self.live["error"] = f"数据源已退出（returncode={proc.returncode}），5 秒后重连…"
            except FileNotFoundError as e:
                self.live["online"] = False
                self.live["error"] = f"无法启动采集进程：{e}"
            except Exception as e:                               # noqa: BLE001
                self.live["online"] = False
                self.live["error"] = f"{type(e).__name__}: {e}"
            finally:
                # 只有"自己这一代"才有权清掉进程引用，否则会把新线程刚起的进程抹掉
                if gen == self._live_gen:
                    self._live_proc = None

            for _ in range(50):                                  # 5 秒，可被 stop 打断
                if self._live_stop or gen != self._live_gen:
                    break
                time.sleep(0.1)

    def live_status(self) -> dict:
        if not self.live.get("enabled"):
            return {"enabled": False}
        st = dict(self.live)
        # 超过 6 秒没收到数据视为离线（板子停了 / 线拔了）
        lr = st.get("last_recv", 0.0) or 0.0
        silent = (time.time() - lr) if lr > 0 else (time.time() - st.get("started", time.time()))
        st["online"] = bool(st.get("online")) and lr > 0 and (time.time() - lr) < 6
        st["uptime_s"] = round(time.time() - st.get("started", time.time()), 1)
        # ★ last_recv=0（还没收到过任何数据）时不能拿它算沉默时长 ——
        #   那会算出"已沉默 17 亿秒"这种荒谬数字。改用启动时刻起算。
        st["silent_s"] = round(max(0.0, silent), 1)
        # 数据源进程还活着但一直没数据（典型场景：串口被拔掉/被 Monitor 占用，
        # 采集器正在内部重试）——此时 error 是空的，得给个能看懂的原因，
        # 否则界面上只剩一个光秃秃的红色 OFFLINE，排查时抓瞎。
        if not st["online"] and not st.get("error"):
            if st.get("lines", 0) == 0:
                st["error"] = ("数据源未产生任何数据：串口 %s 可能不存在 / 被 DevEco Monitor 占用 / "
                               "板子未复位（尝试物理拔插一次 USB）"
                               % (st.get("source") or "?"))
            else:
                st["error"] = ("已 %.0f 秒未收到数据：数据源还在，但设备不再输出（板子断电 / 串口被拔）"
                               % st["silent_s"])
        return st

    def stop_live(self) -> None:
        """关看板时顺手收掉数据源子进程，别留孤儿进程占着串口。"""
        self._live_stop = True
        self._kill_live_proc()
        th = getattr(self, "_live_thread", None)
        if th is not None:
            th.join(timeout=3.0)
        self._live_thread = None

    def _ensure_refresh_thread(self) -> None:
        """拉起"派生视图刷新 + 自动分析"的独立节拍（每 2 秒）。

        ★ 为什么必须有它：
          live_refresh() 原本只在【浏览器发 GET】时被调用。于是"检测到异常就出报告"
          会依赖有没有人开着看板页面 —— 前端一关，异常不再被扫描，报告就永远不会产生。
          自动分析属于后台能力，不该依赖前端。这里给它一个自己的节拍。
        """
        th = getattr(self, "_refresh_thread", None)
        if th is not None and th.is_alive():
            return
        self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._refresh_thread.start()

    def _refresh_loop(self) -> None:
        while self.live.get("enabled") and not self._live_stop:
            time.sleep(2.0)
            if not self.live.get("enabled") or self._live_stop:
                break
            try:
                self.live_refresh()        # 内部有 2 秒节流 + 记录数变化判断
            except Exception as e:                               # noqa: BLE001
                print("[refresh] %s: %s" % (type(e).__name__, e))

    def live_refresh(self) -> None:
        """实时模式下重算派生视图（汇总 / 时间线 / 分区曲线 / 窗口）。

        静态模式的视图在 _load() 里算一次就够；实时模式记录一直在涨，
        必须定期重算，否则顶部 KPI 和图表还停在旧数字上。
        重算是 O(n)，用【记录数变化 + 2 秒节流】避免每 800ms 全量跑一遍。
        """
        if not self.live.get("enabled"):
            return
        with self.lock:
            n = len(self.records)
        if n == self._live_views_n:
            return
        if (time.time() - self._live_views_t) < 2.0:
            return

        with self.lock:
            self.windows, self.anomalies = aggregate_and_score(
                self.records, window_s=self.window_s, threshold=self.threshold)
            self._build_summary()
            self._build_timeline()
            self._build_zone_curves()
            self._live_views_n = n
            self._live_views_t = time.time()
            ok, bad = self.parser.stats["ok"], self.parser.stats["bad"]
            self.meta.update({
                "records": n,
                "parse_ok": ok,
                "parse_bad": bad,
                "parse_rate": round(ok / max(1, ok + bad) * 100, 2),
                "parse_bad_reasons": dict(self.parser.bad_reasons),
                "parse_bad_samples": list(self._bad_samples),
                "windows": len(self.windows),
                "anomaly_windows": len(self.anomalies),
                "log_size_kb": 0.0,
                "duration_s": 0.0,
            })
            ts = [r.ts_device for r in self.records if r.ts_device is not None]
            if ts:
                self.meta["duration_s"] = round(max(ts) - min(ts), 1)

        # 派生视图刚刷新完 → 顺便看看有没有【新的异常窗口】要自动出报告
        self._scan_new_anomalies()

    # ---------------- 自动分析：检测到异常就出报告 ----------------
    def _scan_new_anomalies(self) -> None:
        """把【刚刚闭合】的新异常窗口排进自动分析队列。

        为什么只挑"闭合"的窗口：最后一个窗口还在增长，现在分析等于分析半截数据，
        等下一轮它闭合了再看，报告才不会自相矛盾。
        """
        if not AUTO_ANALYZE or not self.live.get("enabled"):
            return
        if self.analysis.get("running"):
            return          # 手动分析在跑，别和它抢 LLM；窗口不会丢，下一轮再排

        with self.lock:
            windows = list(self.windows)
            records = list(self.records)
        if len(windows) < 2:
            return          # 还没有闭合窗口
        anomaly_ids = {w.window_id for w in self.anomalies}

        now = time.time()
        fresh = []
        for w in windows[:-1]:                      # 排除仍在增长的最后一个窗口
            if w.window_id not in anomaly_ids:
                continue
            if w.window_id in self._auto_done:
                continue
            if not any(getattr(r, "src_name", "") == "live" for r in w.records):
                self._auto_done.add(w.window_id)    # 静态基底数据不自动出报告
                continue
            fp = window_signature(w)
            if AUTO_FP_COOLDOWN_S and (now - self._auto_fp_at.get(fp, 0.0)) < AUTO_FP_COOLDOWN_S:
                self._auto_done.add(w.window_id)    # 同一故障刚报过，别刷屏
                self.auto_state["skipped"] += 1
                continue
            self._auto_done.add(w.window_id)        # 先占位，避免下一轮重复排
            self._auto_fp_at[fp] = now
            fresh.append(w)
            if len(fresh) >= AUTO_SCAN_BATCH:
                break

        if not fresh:
            return
        self._auto_q.extend(fresh)
        self.auto_state["queued"] += len(fresh)
        self._auto_kick()

    def _auto_kick(self) -> None:
        """确保只有一个自动分析线程在跑。"""
        with self.lock:
            if self._auto_busy or not self._auto_q:
                return
            self._auto_busy = True
        threading.Thread(target=self._auto_worker, daemon=True).start()

    def _auto_worker(self) -> None:
        """逐条消费队列：一条异常 → 一次 L1+L2 → 一份 .md 报告。"""
        try:
            llm = LLMClient()               # 内部带缓存与日预算，超预算会自动降级
            analyzer = AnomalyAnalyzer(llm)
            while self._auto_q:
                w = self._auto_q.popleft()
                try:
                    fn = self._auto_report_one(w, analyzer, llm)
                    if fn:
                        self.auto_state["done"] += 1
                except Exception as e:                       # noqa: BLE001
                    self.auto_state["error"] += 1
                    print("[auto] %s 自动分析失败：%s: %s"
                          % (w.window_id, type(e).__name__, e))
        finally:
            with self.lock:
                self._auto_busy = False
            if self._auto_q:
                self._auto_kick()

    def _auto_report_one(self, w, analyzer, llm) -> Optional[str]:
        """给单个异常窗口出一份报告（结构与手动分析完全一致）。"""
        with self.lock:
            records = list(self.records)

        a = analyzer.analyze_window(w, records)
        rca, usage = analyzer.analyze_root_cause(w, a, records)
        validator = EvidenceValidator(records)
        ev = validator.validate(a.evidence_refs) or w.evidence(max_lines=15)

        rep = Report(
            window=w, analysis=a, evidence=ev, rca=rca, usage=usage,
            mode=("llm" if a.mode == "llm" else "rule"), family_count=1,
            generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            data_source="真机实时串口 %s（检测到异常自动生成）"
                        % self.live.get("source", "?"),
            llm_model=getattr(llm, "model", "") if a.mode == "llm" else "")
        name = a.display_name or window_signature(w)
        name = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", name)[:48]
        fn = "%s_%s.md" % (w.window_id, name)
        LIVE_REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (LIVE_REPORT_DIR / fn).write_text(
            rep.to_markdown(validator), encoding="utf-8")
        print("[auto] 已生成 %s（引擎 %s，分数 %.3f，证据 %d 条）"
              % (fn, a.mode, w.anomaly_score, len(ev)))
        return fn

    def live_logs(self, since: int) -> dict:
        """只返回 since 之后新增的记录（增量拉取，避免整表重传）。

        ★ 必须带回 epoch（数据流世代）并处理"游标倒退"。
          踩过的坑：看板重启后 records 数组清空、从 0 重新计数，
          而前端手里还攥着重启前的游标（比如 53）。
          这时 records[53:] 是空的，但返回的 next 只有 10 —— 比 53 小。
          前端老实地把游标改成 10，等 records 再涨过 53，
          就会把【已经显示过的行再发一遍】，界面上出现重复日志。
          所以这里主动告诉客户端：世代变了 / 你的游标超前了，请重置。
        """
        with self.lock:
            n = len(self.records)
            if since > n:
                # 客户端游标比现有数据还大 → 它经历了一次重启，必须重置
                return {"next": 0, "online": self.live_status().get("online", False),
                        "error": self.live_status().get("error", ""),
                        "silent_s": self.live_status().get("silent_s", 0.0),
                        "epoch": self._live_epoch, "reset": True, "items": []}
            new = self.records[since:]
            start = n
        st = self.live_status()
        return {
            "next": start,
            "online": st.get("online", False),
            "error": st.get("error", ""),
            "silent_s": st.get("silent_s", 0.0),
            "epoch": self._live_epoch,      # 世代号：变了说明数据源重启过
            "reset": False,
            "items": [{
                "eid": r.evidence_id, "ts": r.ts_device, "level": r.level,
                "domain": r.domain, "tag": r.tag, "zone": r.zone, "seq": r.seq,
                "fault": r.fault_tag, "fault_name": r.fault_name, "detail": r.detail,
                "raw": r.raw_line, "physical": r.physical,
            } for r in new],
        }

    # ---------------- 报告 ----------------
    def list_reports(self) -> list:
        out = []
        for d in REPORT_DIRS:
            if not d.exists():
                continue
            label, kind = REPORT_SOURCE.get(d.name, (d.name, "other"))
            for p in sorted(d.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
                age = max(0.0, time.time() - p.stat().st_mtime)
                out.append({
                    "name": p.name,
                    "dir": d.name,
                    "source": label,            # 中文来源标注，界面上直接显示
                    "source_kind": kind,        # live / sim / other，用于配色
                    "kb": round(p.stat().st_size / 1024, 1),
                    "mtime": time.strftime("%m-%d %H:%M", time.localtime(p.stat().st_mtime)),
                    "age_s": round(age, 1),     # 生成距今多少秒，前端显示"x 分钟前"
                })
        return out

    def read_report(self, name: str) -> Optional[str]:
        safe = Path(name).name                      # 防路径穿越
        for d in REPORT_DIRS:
            p = d / safe
            if p.exists() and p.suffix == ".md":
                return p.read_text(encoding="utf-8", errors="replace")
        return None

    # ---------------- 实时数据的根因分析 ----------------
    def start_analyze(self, zone: str = "", max_reports: int = 2,
                      use_llm: bool = True) -> dict:
        """对【当前累积的实时数据】跑异常分析与 LLM 根因报告。

        zone 非空时只分析【这一个设备（棚）】的日志 —— 这是真实运维的视角：
        "6 个棚的整体统计"看不出"3 号棚今天特别不对"。
        单设备分析还有个好处：异常分数不会被其它棚的正常日志摊薄。

        为什么要做这件事：
          数据接进来了、异常也标出来了，但如果报告还是从模拟数据集生成的那几份，
          现场看到的"结论"就是假的——**数据是真的、结论是假的，比数据假更危险**。
          所以必须能把"此刻设备上真实发生的事"变成一份报告。

        LLM 调用要十几秒，不能卡住 HTTP 线程，因此放到后台线程跑，
        前端轮询 analyze_status() 看进度。
        """
        if self.analysis.get("running"):
            return {"started": False, "reason": "已有分析在跑"}

        zv = self.view(zone)
        recs = zv["records"]
        if not recs:
            return {"started": False, "reason": "该设备还没有日志数据"}

        self.analysis = {
            "running": True, "done": False, "error": "",
            "started": time.time(), "finished": 0.0,
            "zone": zv["zone"], "zone_name": zv["meta"]["zone_name"],
            "records": len(recs), "windows": 0, "anomalies": 0,
            "reports": [], "log": [], "use_llm": use_llm,
        }
        t = threading.Thread(target=self._analyze_worker,
                             args=(zone, max_reports, use_llm), daemon=True)
        t.start()
        return {"started": True, "zone": zv["zone"]}

    def _analyze_worker(self, zone: str, max_reports: int, use_llm: bool) -> None:
        st = self.analysis

        def log(msg):
            st["log"].append("%s  %s" % (time.strftime("%H:%M:%S"), msg))
            del st["log"][:-40]

        try:
            from agri_logger.analyzer.llm_client import LLMClient
            from agri_logger.analyzer.rca import (
                AnomalyAnalyzer, EvidenceValidator, Report)

            zv = self.view(zone)
            records = zv["records"]
            windows, anomalies = zv["windows"], zv["anomalies"]
            scope = zv["meta"]["zone_name"]
            st["windows"] = len(windows)
            st["anomalies"] = len(anomalies)
            log("分析范围：%s（%d 行）" % (scope, len(records)))
            log("窗口 %d 个，异常窗口 %d 个" % (len(windows), len(anomalies)))

            if not anomalies:
                # ★ 没有异常也要出报告！
                #   踩过的坑：原代码在这里直接 return，用户点了"生成根因报告"
                #   结果一份文件都没有，看起来像功能坏了。
                #   而且"这段时间一切正常"本身就是有价值的结论：
                #   它是一次阴性样本记录，说明监测系统在跑、且没有误报。
                log("未发现异常窗口 —— 生成【系统健康报告】（而不是什么都不出）")
                fn = self._write_health_report(zv, records, windows, log)
                if fn:
                    st["reports"].append(fn)
                    log("已生成 %s" % fn)
                st["done"] = True
                return

            llm = LLMClient(enabled=use_llm)
            if not use_llm:
                log("按请求使用规则引擎（不调 LLM）")
            elif llm.available:
                log("LLM 可用：%s" % llm.model)
            else:
                log("LLM 不可用，降级到规则引擎（%s）"
                    % (llm.init_error or "").split("\n")[0][:70])

            analyzer = AnomalyAnalyzer(llm)
            validator = EvidenceValidator(records)
            LIVE_REPORT_DIR.mkdir(parents=True, exist_ok=True)

            # 先做确定性聚类，报告里写明"本族出现 N 次"
            families = cluster_families(anomalies)
            fam_count = {f["key"]: f["count"] for f in families}
            win_fam = {}
            for f in families:
                for wid in f["windows"]:
                    win_fam[wid] = f["key"]

            targets = sorted(anomalies, key=lambda x: -x.anomaly_score)[:max_reports]
            used = set()
            for w in targets:
                fam_key = win_fam.get(w.window_id, window_signature(w))
                log("分析 %s（分数 %.3f，%d 行，族 %s，本族共 %d 个窗口）..."
                    % (w.window_id, w.anomaly_score, w.line_count,
                       fam_key, fam_count.get(fam_key, 1)))
                a = analyzer.analyze_window(w, records)
                rca, usage = analyzer.analyze_root_cause(w, a, records)
                analyzer.usage.add(usage)
                ev = validator.validate(a.evidence_refs) or w.evidence(max_lines=15)
                a.cluster_key = fam_key
                rep = Report(window=w, analysis=a, evidence=ev, rca=rca,
                             usage=usage, mode=("llm" if a.mode == "llm" else "rule"),
                             family_count=fam_count.get(fam_key, 1),
                             generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                             data_source=("真机实时串口 %s（%s）"
                                          % (self.live.get("source", "?"), scope)
                                          if self.live.get("enabled")
                                          else self.meta.get("log_path", "")),
                             llm_model=getattr(llm, "model", "") if a.mode == "llm" else "")
                # 文件名带设备号，一眼看出这份报告是哪个棚的
                tag = ("Z%s_" % zv["zone"]) if zv["zone"] != "ALL" else ""
                base = "%s%s_%s" % (tag, w.window_id, a.display_name or fam_key)
                base = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", base)[:64]
                fn = base + ".md"
                if fn in used:
                    fn = "%s_%s.md" % (base, fam_key)
                used.add(fn)
                (LIVE_REPORT_DIR / fn).write_text(
                    rep.to_markdown(validator), encoding="utf-8")
                st["reports"].append(fn)
                log("已生成 %s" % fn)

            log("证据校验：%s" % validator.report().replace("\n", " "))
            if llm.usage_today.calls or llm.usage_today.cached:
                log("LLM 用量：调用 %d 次 / 命中缓存 %d 次"
                    % (llm.usage_today.calls, llm.usage_today.cached))
            st["done"] = True
        except Exception as e:                                   # noqa: BLE001
            import traceback
            st["error"] = "%s: %s" % (type(e).__name__, e)
            log("失败：%s" % st["error"])
            log(traceback.format_exc().splitlines()[-1])
        finally:
            st["running"] = False
            st["finished"] = time.time()

    def _write_health_report(self, zv, records, windows, log) -> Optional[str]:
        """没有任何异常窗口时，生成一份【系统健康报告】。

        为什么必须有这份东西：
          · 用户点了"生成根因报告"，一份文件都不出，看起来像功能坏了
          · "这段时间一切正常"本身就是有价值的结论 —— 它是一条【阴性样本】，
            证明监测系统确实在跑、而且没有误报
          · 答辩时也更有说服力："我们的系统在正常时段输出的是健康报告，
            不是空白"

        这份报告【不调用 LLM】—— 没有异常就没有根因可推，
        为了"有产出"去调模型纯属浪费，还容易诱导模型编造问题。
        """
        from agri_logger.parser import fields as F

        LIVE_REPORT_DIR.mkdir(parents=True, exist_ok=True)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        meta = zv["meta"]
        lv = Counter(r.level for r in records)
        faults = Counter(r.fault_tag for r in records if r.fault_tag)
        zones = Counter(r.zone for r in records if r.zone and r.zone != "-")

        # 数据来源说明：真机串口 还是 文件
        if self.live.get("enabled"):
            src = "真机实时串口 %s（%s）" % (self.live.get("source", "?"), meta["zone_name"])
        else:
            src = self.meta.get("log_path", "")

        # 时间跨度
        ts = [r.ts_device for r in records if r.ts_device is not None]
        span = "%.1f 秒" % (max(ts) - min(ts)) if ts else "（未知）"

        # 分等级
        lv_line = " ｜ ".join("%s=%d" % (k, lv.get(k, 0)) for k in ("D", "I", "W", "E", "F"))

        # 故障码：有的话列出来，没有就写"无"
        if faults:
            fault_rows = "\n".join(
                "| `%s` | %s | %d |%s |" % (
                    t, F.fault_name(int(t[1:])), c,
                    " 是（正常事件，不计入异常）" if F.is_normal_event(int(t[1:])) else " 否")
                for t, c in faults.most_common())
            fault_block = ("| 故障码 | 名称 | 次数 | 正常事件 |\n|---|---|---|---|\n"
                           + fault_rows)
        else:
            fault_block = "**本次范围内没有出现任何故障码。**"

        dev = self.device_mode()
        mode_line = dev["label"] if dev.get("known") else "未知（固件未上报 AgriMode）"

        md = f"""# 系统健康报告 —— 未发现异常

| 项目 | 值 |
|---|---|
| **生成时间** | **{now}**（本地时间） |
| 数据来源 | {src} |
| 分析范围 | {meta['zone_name']} |
| 设备当前模式 | **{mode_line}** |
| 记录数 | {len(records)} 行 |
| 时间跨度 | {span} |
| 窗口数 | {len(windows)} 个（窗口长度 {self.window_s:.0f} 秒） |
| **异常窗口** | **0 个**（判定阈值 {self.threshold}） |
| 级别分布 | {lv_line} |
| 涉及设备 | {'、'.join(sorted(zones)) if zones else '（无设备维度）'} |

## 一、结论

**在本次分析范围内（{len(windows)} 个窗口、{len(records)} 行日志），
未检出任何异常窗口。**

这意味着：级别分布正常、无异常故障码、无日志静默、无序号缺口、无阈值越界。

## 二、故障码统计

{fault_block}

## 三、这个结论意味着什么

⚠️ **请注意区分两种"没有异常"**：

1. **设备确实运行正常** —— 这是好消息，本报告就是记录这个状态
2. **设备被人为关闭了故障注入** —— 见上表"设备当前模式"

> 如果是第 2 种（设备处于"纯真实数据"模式），那么"没有异常"是**预期的**，
> 不能据此说"系统没有误报"。要验证异常识别能力，请：
> 按下板子上的 **KEY 按键**开启故障注入，等 1~2 分钟再生成一次报告。

## 四、如何验证监测系统确实在工作

「没有异常」本身不能证明监测链路是活的。建议做一次对照实验：

| 步骤 | 预期 |
|---|---|
| 1. 记下当前的健康报告 | 异常窗口 = 0 |
| 2. 按板子上的 KEY 开启故障注入 | 板载 OLED 显示 `FAULT:ON`，看板徽标变琥珀色 |
| 3. 等 1~2 分钟，再点一次「生成根因报告」 | 出现异常窗口，生成带证据的根因报告 |
| 4. 再按一次 KEY 关闭注入 | 恢复纯真实数据 |

**这一来一回，才同时证明了"不误报"和"不漏报"。**

---

<sub>本报告由「OpenHarmony 日志智能聚合分析工具」（本项目自研的分析程序）自动生成
 ｜ 生成时间 {now}
 ｜ 本报告不调用 LLM：没有异常就没有根因可推，为凑产出去调模型只会诱导它编造问题
 ｜ 判定方法：60 秒窗口聚合 + 5 信号加权打分（级别 0.30 / 故障码 0.25 / 静默 0.25 / seq缺口 0.10 / 阈值 0.10）</sub>
"""
        fn = "健康报告_%s.md" % time.strftime("%m%d_%H%M%S")
        (LIVE_REPORT_DIR / fn).write_text(md, encoding="utf-8")
        return fn

    def analyze_status(self) -> dict:
        st = dict(self.analysis)
        if st.get("started"):
            st["elapsed_s"] = round((st.get("finished") or time.time())
                                    - st["started"], 1)
        return st


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------

STORE: Optional[DataStore] = None


class Handler(BaseHTTPRequestHandler):
    server_version = "AgriLogDashboard/1.0"

    # ---- 工具 ----
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def log_message(self, fmt, *args):              # 静音访问日志，避免刷屏
        pass

    def _shutdown(self):
        """优雅停止：先收掉采集子进程（释放串口），再关 HTTP 服务。

        ★ 为什么必须走这个接口，而不是让启动器直接 taskkill：
          采集器是看板的【子进程】。粗暴杀父进程会留下孤儿进程继续占着串口，
          现象就是"看板关了，DevEco 还是打不开串口 / 报 upload port is occupied"。
          这里按"先停采集、再停服务"的顺序收，串口一定被释放。
        """
        try:
            STORE.stop_live()
            r = {"ok": True, "msg": "采集已停止，串口已释放"}
        except Exception as e:                               # noqa: BLE001
            r = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        self._json(r)
        # 在独立线程里关服务：不能在请求线程里阻塞 serve_forever 的收尾
        import threading as _th
        _th.Thread(target=self.server.shutdown, daemon=True).start()

    # ---- GET ----
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        qs = parse_qs(u.query)
        one = lambda k, d="": (qs.get(k, [d])[0])

        try:
            if path in ("/", "/index.html"):
                return self._serve_file(WEB_DIR / "index.html")

            # 实时模式下，派生视图要跟着新数据走（内部有 2 秒节流，开销可忽略）
            if STORE.live.get("enabled"):
                STORE.live_refresh()

            if path == "/api/meta":
                m = dict(STORE.meta)
                m["live"] = STORE.live_status()
                m["mode"] = "live" if STORE.live.get("enabled") else "replay"
                if m["mode"] == "live":
                    # 实时模式下 log_path 已无意义（读的是串口，不是文件）。
                    # 留着旧路径会让人以为"结论还是从那个文件算的"。
                    m["log_path"] = "（实时串口 %s，非文件）" % m["live"].get("source", "?")
                    m["data_source"] = "真机实时"
                else:
                    m["data_source"] = "静态文件回放"
                # 若前端选了某个设备，把该设备视角的统计一并带上
                zv = STORE.view(one("zone"))
                m["scope"] = zv["zone"]
                m["scope_name"] = zv["meta"]["zone_name"]
                m["records"] = zv["meta"]["records"]
                m["windows"] = zv["meta"]["windows"]
                m["anomaly_windows"] = zv["meta"]["anomaly_windows"]
                m["duration_s"] = zv["meta"]["duration_s"]
                m["device_mode"] = STORE.device_mode()
                return self._json(m)
            if path == "/api/live/status":
                return self._json(STORE.live_status())
            if path == "/api/live/reconnect":
                return self._json(STORE.restart_live())
            if path == "/api/shutdown":
                # 启动器的「停止」按钮 / 关闭窗口时调用，见 _shutdown()
                return self._shutdown()
            if path == "/api/analyze":
                if one("action") == "status":
                    return self._json(STORE.analyze_status())
                r = STORE.start_analyze(
                    zone=one("zone"),
                    max_reports=min(int(one("max", "2") or 2), 6),
                    use_llm=(one("no_llm") != "1"))
                return self._json(r)
            if path == "/api/live/logs":
                return self._json(STORE.live_logs(int(one("since", "0") or 0)))
            if path == "/api/summary":
                return self._json(STORE.view(one("zone"))["summary"])
            if path == "/api/families":
                return self._json(STORE.view(one("zone"))["families"])
            if path == "/api/timeline":
                return self._json(STORE.view(one("zone"))["timeline"])
            if path == "/api/zones":
                return self._json(STORE.view(one("zone"))["zone_curves"])
            if path == "/api/windows":
                return self._json(STORE.windows_payload(STORE.view(one("zone"))["windows"]))
            if path == "/api/devices":
                # 设备清单 + 每个设备的条数，给前端选择器用
                recs = STORE.view("")["records"]
                return self._json([
                    {"zone": z["zone"], "count": z["count"],
                     "name": STORE._zone_label(z["zone"], recs)}
                    for z in STORE.view("")["summary"]["zones"] if z["zone"] != "-"
                ])
            if path == "/api/logs":
                return self._json(STORE.query_logs(
                    level=one("level"), tag=one("tag"), zone=one("zone"), q=one("q"),
                    limit=min(int(one("limit", "300") or 300), 2000),
                    offset=int(one("offset", "0") or 0)))
            if path == "/api/reports":
                return self._json(STORE.list_reports())
            if path == "/api/report":
                md = STORE.read_report(one("name"))
                if md is None:
                    return self._json({"error": "报告不存在"}, 404)
                return self._json({"name": one("name"), "markdown": md})

            # 静态资源
            rel = path.lstrip("/")
            if rel and ".." not in rel:
                return self._serve_file(WEB_DIR / rel)

            return self._json({"error": "not found", "path": path}, 404)
        except Exception as e:                       # noqa: BLE001
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _serve_file(self, p: Path):
        if not p.exists() or not p.is_file():
            return self._json({"error": f"文件不存在: {p.name}"}, 404)
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        if ctype.startswith("text/") and "charset" not in ctype:
            ctype += "; charset=utf-8"
        self._send(200, p.read_bytes(), ctype)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def detect_serial_port() -> str:
    """自动发现开发板串口。

    为什么不能写死 COM3：Windows 是按【物理 USB 插口】分配 COM 号的，
    板子换个 USB 口就从 COM3 变成 COM4（本项目实测踩过），
    写死端口号会在演示当天直接失败。这里枚举一次，选出最像开发板的那个。
    """
    try:
        import serial.tools.list_ports
    except ImportError:
        return "auto"                      # 交给 serial_capture.py 自己发现
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        return "auto"
    if len(ports) == 1:
        return ports[0].device
    for p in ports:                        # 多个口时按常见 USB 转串口芯片名筛选
        desc = ("%s %s" % (p.description or "", p.manufacturer or "")).upper()
        if any(h in desc for h in ("CH340", "CH341", "CP210", "FT232", "USB-SERIAL", "SILICON")):
            return p.device
    return ports[0].device


def pick_log(explicit: Optional[str]) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = ROOT / p
        if not p.exists():
            raise SystemExit(f"❌ 日志文件不存在：{p}")
        return p
    for cand in ("eval/dataset/d2_v2.log", "logs/multi.log", "eval/dataset/d2_demo.log"):
        p = ROOT / cand
        if p.exists() and p.stat().st_size > 1000:
            return p
    raise SystemExit("❌ 找不到可用的日志文件，请用 --log 指定")


def main(argv=None) -> int:
    # ★ 必须把标准输出/错误切成 UTF-8 且 errors="replace"。
    #   踩过的坑：双击 .cmd 启动时控制台代码页是 GBK(936)，而脚本里打印了 "▶"
    #   —— 这个字符 GBK 编不出来，Python 直接抛 UnicodeEncodeError 崩掉，
    #   表现是"窗口一闪而过 / 看板起不来"，且从终端用 PYTHONIOENCODING=utf-8
    #   手动跑时【完全复现不了】。
    #   errors="replace" 是第二道保险：就算终端编码再怪，也只会显示成 ? 而不会崩。
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="日志分析看板（零依赖后端）")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--log", default=None, help="日志文件路径（默认自动挑选）")
    ap.add_argument("--window", type=float, default=60.0, help="窗口长度（秒）")
    ap.add_argument("--threshold", type=float, default=0.25, help="异常阈值")
    ap.add_argument("--live", action="store_true",
                    help="实时模式：直接读板子串口，而不是回放静态文件")
    ap.add_argument("--live-sim", action="store_true",
                    help="实时模式（模拟器作数据源），用于没有板子时演示'实时'效果")
    ap.add_argument("--com", default=None,
                    help="实时模式的串口号（默认自动发现；板子换 USB 口后 COM 号会变）")
    ap.add_argument("--baud", type=int, default=115200, help="实时模式波特率")
    ap.add_argument("--no-auto-analyze", action="store_true",
                    help="关闭「检测到异常就自动生成报告」（默认开启，仅实时模式生效）")
    args = ap.parse_args(argv)

    if args.no_auto_analyze:
        global AUTO_ANALYZE
        AUTO_ANALYZE = False
        print("▶ 已关闭自动分析：异常报告只在点「生成根因报告」时产出")

    global STORE
    logp = pick_log(args.log)
    print(f"▶ 加载日志 {logp.relative_to(ROOT)} ...")
    STORE = DataStore(logp, window_s=args.window, threshold=args.threshold)
    m = STORE.meta
    print(f"  ✅ {m['records']} 条 ｜ 解析率 {m['parse_rate']}% ｜ "
          f"窗口 {m['windows']} 个（异常 {m['anomaly_windows']}）｜ 耗时 {m['load_ms']}ms")

    if args.live or args.live_sim:
        if args.live_sim:
            print("▶ 实时模式：数据源 = 模拟器")
            STORE.start_live("", args.baud, sim=True)
        else:
            # ★ 不显式指定 --com 时传空串，让采集器用 `--port auto`。
            #   传具体端口号会把子进程钉死在那一个号上：
            #   板子拔了再插很可能换成别的 COM 号（实测 COM3→COM4→COM5→COM3），
            #   子进程会一直重试那个已经不存在的口 —— 现象就是"插回去也接不上"。
            com = args.com or ""
            shown = args.com or (detect_serial_port() or "自动发现")
            print(f"▶ 实时模式：数据源 = 串口 {shown} @ {args.baud}"
                  + ("" if args.com else "（自动发现，换号会自动跟上）"))
            # ★ 必须提前说清楚：Windows 串口是独占的，看板占着的时候
            #   DevEco 的 Upload/Monitor 都会报 "upload port is occupied"。
            #   实测踩过：烧录失败，排查半天发现是自己的看板占着。
            print(f"  ⚠️  看板运行期间【独占串口】——此期间 DevEco 无法 Upload / 烧录 / 开 Monitor")
            print(f"      要烧录请先 Ctrl+C 停掉看板（或关掉本终端）")
            STORE.start_live(com, args.baud, sim=False)
    else:
        print("▶ 回放模式：循环播放静态文件（改实时请加 --live，或 --live-sim）")

    if not (WEB_DIR / "index.html").exists():
        print(f"  ⚠️ 找不到前端页面：{WEB_DIR / 'index.html'}")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"\n{'═' * 62}")
    print(f"  看板已启动：  {url}")
    print(f"  日志基底：    {m['log_path']}")
    print(f"  运行模式：    {'实时（读设备）' if (args.live or args.live_sim) else '回放（读文件）'}")
    print(f"  Ctrl+C 停止")
    print(f"{'═' * 62}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[停止] 看板已关闭")
    finally:
        STORE.stop_live()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
