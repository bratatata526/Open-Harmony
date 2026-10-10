#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
固件一致性校验 —— 烧录之后跑一下，确认板上跑的确实是当前这份代码。

为什么需要这个工具
------------------
"改了源码 → 重新烧录 → 现象没变" 是嵌入式最常见的坑，而它有两种完全不同的成因：

  ① 烧录没生效（板子上还是旧固件）      → 重新 Rebuild + Upload
  ② 平台真的有 bug（如 printf 多位数截断）→ 改代码绕开

两种成因的**表面现象一模一样**（数值被截断），但处理方式相反。
肉眼看日志分不出来，必须用"只有新代码才会产生的特征"来判定。本工具就干这个。

三道判据（缺一不可）
--------------------
  1. 开机横幅里的版本号 FW=vX.Y
  2. 开机自检行 AgriSelfCheck 的四个边界值一个不少
     （零 / 4 位 / 5 位 / 负数 —— 这四种正是平台 printf 最容易打错的）
  3. 故障码完整性：字段 5 的值必须落在合法故障码表内
     （旧固件会把 3006 打成 "3"，一眼就能看出来）

用法
----
    # 直接读板子（会占用串口，先关掉 DevEco Monitor 和看板）
    python tools/check_firmware.py --port COM3 --duration 15

    # 或者校验一份已经抓好的日志
    python tools/check_firmware.py --log logs/multi.log

    # 端口自动发现
    python tools/check_firmware.py --port auto

注意：判据 1 和 2 只在**开机时**打印一次。如果本轮没抓到，工具会提示你按复位键重跑。

本脚本跑在 Python 3.8 上（不用 3.9+ 语法）。
"""

import argparse
import os
import re
import subprocess
import sys
import time
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agri_logger.parser.parser import LogParser          # noqa: E402
from agri_logger.parser import fields as F               # noqa: E402

# 开机横幅里的版本号。改了日志格式请同步改 firmware 里的 FW=vX.Y
RE_FW = re.compile(r"FW=(v\d+\.\d+)")
# 开机横幅里的 tick 频率（FW=v2.2 起）。用来核对时间戳单位。
RE_TICKFREQ = re.compile(r"tickfreq=(\d+)\s*Hz")
# 开机横幅里声明的上报周期，用于时基核对（比写死 5 秒更可靠）
RE_PERIOD = re.compile(r"period=(\d+)\s*ms")

# 开机自检行的期望值：字段ID -> 期望值
SELFCHECK_EXPECT = OrderedDict([
    (1, 0),        # seq = 0       零
    (5, 1001),     # fault = 1001  4 位
    (8, 20480),    # heap = 20480  5 位
    (15, -355),    # miss = -355   负数（故意超域，专测符号分支）
])

# 各模块**合法**的字段 ID 序列（按线上出现顺序）。
#
# ★ 默认不从这张表判断，而是【直接从固件源码推导】（见 load_expected_from_firmware）。
#   原因：手抄一份预期表 = 又造了一个"真相的第二份拷贝"，固件一改它就过期，
#   最后一定会出现"工具报错、其实固件是对的"这种假警报（就是踩过的坑 8）。
#   这张表只在找不到固件源码时作为兜底。
FALLBACK_SHAPES = {
    "AgriSensor": [(1, 2, 3, 4), (1, 5, 6), (1, 5, 6, 2)],
    "AgriReport": [(1, 9)],
    "AgriHealth": [(1, 8, 9, 15), (1, 9, 15), (1, 5, 6, 8)],
    "AgriCtrl": [(1, 5, 11, 12), (1, 5, 11, 3)],
    "AgriSelfCheck": [(1, 5, 8, 15)],
}

FIRMWARE_SRC = ROOT / "firmware" / "hi3861" / "agri_logger" / "agri_logger_multi.c"
FIRMWARE_HDR = ROOT / "firmware" / "hi3861" / "agri_logger" / "log_def.h"

RE_AGRI_CALL = re.compile(r'AGRI_([IWEF])\(\s*[^,]+,\s*"([^"]+)",\s*([^,]+),(.*?)\);',
                          re.S)
RE_F_FIELD = re.compile(r'F\(\s*\w+\s*,\s*(AGRI_F_\w+)')
RE_ENUM_ID = re.compile(r'AGRI_F_(\w+)\s*=\s*(\d+)')


def load_expected_from_firmware(src_path=None, hdr_path=None):
    """从固件源码里把所有 AGRI_x(...) 调用解析出来，得到"固件会打哪些结构"。

    返回 (shapes_dict, 说明字符串)。解析不到就返回 (None, 原因)。
    """
    src_path = src_path or FIRMWARE_SRC
    hdr_path = hdr_path or FIRMWARE_HDR
    try:
        hdr = Path(hdr_path).read_text(encoding="utf-8", errors="replace")
        src = Path(src_path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return None, "读不到固件源码（%s）" % e

    ids = {}
    for m in RE_ENUM_ID.finditer(hdr):
        ids["AGRI_F_" + m.group(1)] = int(m.group(2))
    if not ids:
        return None, "log_def.h 里没解析出字段 ID 枚举"

    shapes = {}
    total = 0
    for m in RE_AGRI_CALL.finditer(src):
        tag = m.group(2)
        total += 1
        flds = []
        for name in RE_F_FIELD.findall(m.group(4)):
            if name not in ids:
                flds = None
                break
            flds.append(ids[name])
        if not flds:
            continue                       # 这条调用没有字段，或字段名不认识
        shapes.setdefault(tag, set()).add(tuple(flds))

    if not shapes:
        return None, "固件源码里没解析出任何日志调用"
    return ({k: sorted(v) for k, v in shapes.items()},
            "已从固件源码推导（%d 处 AGRI_ 调用、%d 个模块）" % (total, len(shapes)))


class Checker(object):
    def __init__(self):
        self.parser = LogParser()
        self.raw_lines: List[str] = []
        self.records = []
        self.bad_lines: List[str] = []
        self.capture_wall_s = 0.0        # 采集的真实墙钟时长（用于核对时基）
        self.declared_period_s = 5.0     # 固件声明的上报周期；能从横幅解析就覆盖
        self.expected, self.expected_note = load_expected_from_firmware()
        if self.expected is None:
            self.expected = FALLBACK_SHAPES
            self.expected_note = "固件源码不可用，改用内置兜底表（%s）" % self.expected_note

    # ---------- 采集 ----------
    def capture(self, port: str, baud: int, duration: float) -> int:
        """调用项目自己的采集器抓一段（复用它的 DTR/RTS 与重连逻辑，不重写）"""
        cmd = [sys.executable, "-u", str(ROOT / "tools" / "serial_capture.py"),
               "--port", port, "--baud", str(baud), "--min-level", "D",
               "--raw", "--duration", str(int(duration))]
        print("▶ 采集 %.0f 秒（端口 %s @ %d）..." % (duration, port, baud))
        print("  [提示] 想抓开机横幅和自检行，请在采集开始后按一下板子的【复位键】\n")
        t_wall = time.time()
        try:
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, universal_newlines=True,
                                    encoding="utf-8", errors="replace")
            out, err = proc.communicate(timeout=duration + 25)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
        except FileNotFoundError as e:
            print("✘ 无法启动采集器：%s" % e)
            return 2
        self.raw_lines = [l.rstrip("\r\n") for l in (out or "").splitlines() if l.strip()]
        self.capture_wall_s = time.time() - t_wall
        if not self.raw_lines and err:
            print("✘ 没抓到任何数据。采集器输出：")
            for l in (err or "").splitlines()[-6:]:
                print("    " + l)
            return 2
        return 0

    def load(self, path: Path) -> int:
        if not path.exists():
            print("✘ 日志文件不存在：%s" % path)
            return 2
        self.raw_lines = [l for l in path.read_text(
            encoding="utf-8", errors="replace").splitlines() if l.strip()]
        return 0

    # ---------- 解析 ----------
    def parse(self) -> None:
        for line in self.raw_lines:
            # 顺手从开机横幅里取固件声明的上报周期，时基核对时用它当基准，
            # 而不是写死 5 秒 —— 周期改了工具也不用改。
            m = RE_PERIOD.search(line)
            if m and int(m.group(1)) > 0:
                self.declared_period_s = int(m.group(1)) / 1000.0
            rec = self.parser.parse(line, src_name="check")
            if rec:
                self.records.append(rec)
            else:
                self.bad_lines.append(line)

    # ---------- 未解析行的证据 ----------
    def bad_report(self) -> List[str]:
        """把解析失败的行原样列出来。

        为什么必须列：解析率不是 100% 时，光看一个百分比说明不了任何问题——
        "失败的到底是什么"才是关键。开机瞬间的 ROM 引导日志本来就不是我们的格式，
        属于正常；但如果混进了本该解析成功的业务日志，那就是真问题。
        不把原始行摆出来，就只能靠猜。
        """
        if not self.bad_lines:
            return ["  （无解析失败的行）"]
        out = ["  共 %d 行未能解析，按内容分类：" % len(self.bad_lines)]
        for reason, n in sorted(self.parser.bad_reasons.items(), key=lambda x: -x[1]):
            out.append("      %-24s ×%d" % (reason, n))
        out.append("  原始行样本（最多 6 条）：")
        for l in self.bad_lines[:6]:
            out.append("      | %s" % l.strip()[:100])
        out.append("  → 判读：上电瞬间的 ROM 引导日志 / 开机横幅本来就【不是】本项目的日志格式，")
        out.append("     这类行解析失败是正常的；只要业务日志（AgriSensor 等）没有落进来就没问题。")
        return out

    # ---------- 判据 1：版本号 ----------
    def check_version(self) -> Tuple[bool, str]:
        fw = None
        tickfreq = None
        for line in self.raw_lines:
            if fw is None:
                m = RE_FW.search(line)
                if m:
                    fw = m.group(1)
            if tickfreq is None:
                m2 = RE_TICKFREQ.search(line)
                if m2:
                    tickfreq = int(m2.group(1))
        if not fw:
            return False, ("本轮未捕获开机横幅（它只在开机时打印一次）"
                           "—— 请按一下板子复位键重跑，以确认烧录生效")
        msg = "开机横幅版本号：FW=%s" % fw
        if tickfreq:
            msg += " ｜ tickfreq=%dHz（1 tick = %.0f ms）" % (tickfreq, 1000.0 / tickfreq)
        else:
            msg += " ｜ 横幅里没有 tickfreq（FW 早于 v2.2，时基可能仍是错的，见 docs/09 坑 12）"
        return True, msg

    # ---------- 判据 4：时基（秒表法，自动版）----------
    def check_timebase(self) -> Tuple[bool, List[str]]:
        """核对"设备时间戳"与"真实时间"的一致性和单位。

        为什么要自动做这件事：时基错位时【定时、日志节奏全都正常】，
        只有时间戳悄悄慢一个数量级。人工发现它得拿手机掐表，
        很容易一直没人查；而所有窗口/间隔/静默检测都建立在时间戳上。

        用两个互补的判据：
          A) 批间隔 —— 板子是"每 N 秒吐一批"。批【内】的时间戳只差几毫秒，
             批【间】的差才是周期。所以取间隔的【最大值】当批间隔，
             再和固件声明的周期比。
             ★ 这条判据做成【单边】的：真实发生日志静默时，最大间隔只会
               比周期更大，不会更小。所以"偏大"不能算错（否则真故障会被
               误判成时基问题），只有"明显偏小"才说明时基被压缩了。
          B) 时间戳跨度 ÷ 墙钟时长 —— 粗判据。板子是突发发送，首末记录都不在
             采集窗口的绝对两端，天然会低一点，所以只作为佐证，不单独判负。
        """
        out = []
        if self.capture_wall_s <= 0:
            return True, ["  （校验的是已有日志文件，没有墙钟时长，跳过时基核对）"]
        ts = sorted(set(r.ts_device for r in self.records if r.ts_device is not None))
        if len(ts) < 5:
            return True, ["  （记录太少，跳过时基核对）"]

        # ---- 判据 A：批间隔（取最大间隔）vs 固件声明的周期 ----
        gaps = [b - a for a, b in zip(ts, ts[1:]) if b - a > 1e-6]
        if not gaps:
            return True, ["  （所有记录时间戳相同，跳过时基核对）"]
        burst = max(gaps)
        want = self.declared_period_s
        ratio = burst / want if want else 0.0
        small = sorted(g for g in gaps if g < want * 0.5)
        out.append("  批内间隔（毫秒级，共 %d 个，中位 %.4f 秒）"
                   % (len(small), small[len(small) // 2] if small else 0.0))
        out.append("  批间隔（取最大）   %.2f 秒   （固件声明周期 %.1f 秒）" % (burst, want))
        out.append("  周期比例           %.3f" % ratio)

        # ---- 判据 B：跨度 / 墙钟（佐证）----
        span = ts[-1] - ts[0]
        wall = self.capture_wall_s
        out.append("  采集墙钟 %.1f 秒 ｜ 时间戳跨度 %.1f 秒 ｜ 跨度比例 %.3f"
                   % (wall, span, span / wall if wall else 0))
        out.append("  （跨度比例天生略小于 1：突发发送，首末记录距窗口两端各差最多一个周期）")

        if ratio >= 0.70:
            out.append("  ✔ 时基正确（批间隔 ≥ 声明周期，说明时间戳单位就是秒）")
            if ratio > 1.5:
                out.append("     批间隔明显大于周期（%.2f 秒）：期间很可能发生了真实日志静默。"
                           % burst)
                out.append("     这是【业务现象】不是时基问题 —— 反而说明静默检测在工作。")
            return True, out

        factor = (1 / ratio) if ratio else 0
        out.append("  ✘ 时基不对！批间隔只有声明周期的 %.3f 倍（约 1/%.0f）" % (ratio, factor))
        out.append("     典型原因：把 RTOS tick 当成 1ms，而本 SDK 是 100Hz(10ms/tick)。")
        if ratio:
            out.append("     后果：60 秒窗口实际切成约 %d 秒；静默检测（间隔>2×5s）永远不触发。"
                       % round(60 / ratio))
        out.append("     修法：用 osKernelGetTickFreq() 动态换算，详见 docs/09 坑 12。")
        return False, out

    # ---------- 判据 2：开机自检行 ----------
    def check_selfcheck(self) -> Tuple[bool, List[str]]:
        recs = [r for r in self.records if r.tag == "AgriSelfCheck"]
        if not recs:
            return False, ["本轮未出现 AgriSelfCheck 行（同样只在开机时打印一次）"
                           "—— 按复位键重跑即可"]
        out = []
        ok = True
        for r in recs:
            out.append("  原始行：%s" % r.raw_line.strip())
            for fid, want in SELFCHECK_EXPECT.items():
                got = r.values.get(fid)
                name = F.FIELDS.get(fid, ("?", ""))[0] if hasattr(F, "FIELDS") else str(fid)
                if got == want:
                    out.append("    ✔ 字段 %-2d %-10s = %-8s (期望 %s)"
                               % (fid, name, got, want))
                else:
                    ok = False
                    out.append("    ✘ 字段 %-2d %-10s = %-8s (期望 %s)  ← 数值被截断/丢失！"
                               % (fid, name, got, want))
        return ok, out

    # ---------- 判据 3：故障码完整性 ----------
    def check_fault_codes(self) -> Tuple[bool, List[str]]:
        bad = Counter()
        good = Counter()
        for r in self.records:
            v = r.values.get(5)
            if v is None:
                continue
            if v in F.FAULTS:
                good[v] += 1
            else:
                bad[v] += 1
        out = []
        if good:
            out.append("  合法故障码：%s"
                       % "  ".join("F%d×%d" % (k, n) for k, n in sorted(good.items())))
        if bad:
            out.append("  ✘ 非法/被截断的故障码值：")
            for v, n in sorted(bad.items()):
                out.append("      %-8s ×%d   ← 合法码是 1001~5002 的 4 位数；"
                           "打成 1 位数说明数值转换被截断" % (v, n))
            return False, out
        if not good:
            return True, ["  本轮未出现故障码（正常，说明设备没报故障）"]
        return True, out

    # ---------- 字段结构指纹（诊断用） ----------
    def shape_report(self) -> Tuple[bool, List[str]]:
        shapes: Dict[str, Counter] = {}
        samples: Dict[Tuple[str, tuple], str] = {}
        for r in self.records:
            key = tuple(r.values.keys())          # dict 保序 = 线上出现顺序
            shapes.setdefault(r.tag, Counter())[key] += 1
            samples.setdefault((r.tag, key), r.raw_line.strip())

        out = []
        all_expected = True
        for tag in sorted(shapes):
            allowed = self.expected.get(tag)
            for key, n in shapes[tag].most_common():
                if allowed is None:
                    mark = "?"
                    all_expected = False
                elif list(key) in [list(a) for a in allowed]:
                    mark = "✔"
                else:
                    mark = "✘"
                    all_expected = False
                out.append("  %s %-14s 字段序列 %-14s ×%d"
                           % (mark, tag, ",".join(str(k) for k in key), n))
                if mark != "✔":
                    out.append("        样本：%s" % samples[(tag, key)])
        if not shapes:
            return False, ["  没有解析出任何记录"]
        return all_expected, out

    # ---------- 主流程 ----------
    def run(self) -> int:
        print("=" * 72)
        print("  固件一致性校验")
        print("=" * 72)
        self.parse()
        total = len(self.raw_lines)
        okn = self.parser.stats["ok"]
        print("输入行 %d ｜ 解析成功 %d（%.2f%%）｜ 失败 %d"
              % (total, okn, okn / max(1, total) * 100, self.parser.stats["bad"]))
        for l in self.bad_report():
            print(l)
        print("")

        results = []

        print("【判据 1】固件版本号")
        ok1, msg1 = self.check_version()
        print("  %s %s\n" % ("✔" if ok1 else "⚠", msg1))
        results.append(("版本号", ok1))

        print("【判据 2】开机自检行（四种最容易出错的数值）")
        ok2, lines2 = self.check_selfcheck()
        for l in lines2:
            print(l)
        print("  → %s\n" % ("✔ 通过" if ok2 else "⚠ 未通过/未捕获"))
        results.append(("自检行", ok2))

        print("【判据 3】故障码完整性")
        ok3, lines3 = self.check_fault_codes()
        for l in lines3:
            print(l)
        print("  → %s\n" % ("✔ 通过" if ok3 else "✘ 失败"))
        results.append(("故障码", ok3))

        print("【判据 4】时基核对（设备时间戳 ÷ 真实时间 应 ≈ 1.0）")
        ok5, lines5 = self.check_timebase()
        for l in lines5:
            print(l)
        print("  → %s\n" % ("✔ 通过" if ok5 else "✘ 失败"))
        results.append(("时基", ok5))

        print("【附】字段结构指纹（对照固件源码应产生的结构）")
        print("  预期表来源：%s" % self.expected_note)
        print("  固件应产生的结构：")
        for tag in sorted(self.expected):
            print("      %-14s %s" % (tag, "  ".join(
                "(" + ",".join(str(k) for k in s) + ")" for s in self.expected[tag])))
        print("  实测：")
        ok4, lines4 = self.shape_report()
        for l in lines4:
            print(l)
        print("  → %s\n" % ("✔ 全部符合预期" if ok4 else "⚠ 有未预期的结构，请核对上面的样本行"))
        results.append(("字段结构", ok4))

        print("=" * 72)
        hard_fail = [n for n, ok in results if not ok and n in ("故障码", "时基")]
        soft = [n for n, ok in results if not ok and n not in ("故障码", "时基")]
        if hard_fail:
            print("  结论：✘ 不通过 —— %s 有问题，说明板上跑的多半不是当前这份代码"
                  % "、".join(hard_fail))
        elif soft:
            print("  结论：⚠ 基本通过，但 %s 未能确认" % "、".join(soft))
            print("         （多半只是本轮没抓到开机那几行，按一下复位键重跑即可）")
        else:
            print("  结论：✔ 全部通过 —— 板上跑的确实是当前这份代码")
        print("=" * 72)
        return 0 if not hard_fail else 1


def detect_port() -> str:
    try:
        import serial.tools.list_ports
    except ImportError:
        return "auto"
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        return "auto"
    if len(ports) == 1:
        return ports[0].device
    for p in ports:
        d = ("%s %s" % (p.description or "", p.manufacturer or "")).upper()
        if any(h in d for h in ("CH340", "CH341", "CP210", "FT232", "USB-SERIAL")):
            return p.device
    return ports[0].device


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="固件一致性校验：确认板上跑的是当前这份代码")
    ap.add_argument("--port", default=None, help="串口（默认自动发现）")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--duration", type=float, default=15.0, help="采集秒数")
    ap.add_argument("--log", default=None, help="改为校验一份已有日志，不连板子")
    args = ap.parse_args(argv)

    ck = Checker()
    if args.log:
        p = Path(args.log)
        if not p.is_absolute():
            p = ROOT / p
        print("▶ 读取日志 %s\n" % p.relative_to(ROOT))
        rc = ck.load(p)
    else:
        port = args.port or detect_port()
        rc = ck.capture(port, args.baud, args.duration)
    if rc != 0:
        return rc
    print("")
    return ck.run()


if __name__ == "__main__":
    sys.exit(main())
