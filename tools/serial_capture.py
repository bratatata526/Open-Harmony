#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""串口日志采集器（OpenHarmony 系统日志异常智能聚合分析工具 · 第 2 步）

把 FS-Hi3861（或 STM32）通过 USB 输出的日志实时收下来，边收边解析。

这是 docs/05-python-pipeline.md 里 collector + parser 的最小可运行版本，
用来在写正式流水线【之前】先把"日志能不能稳定抓下来"这件事验证掉。

用法：
  # 0) 看看有哪些串口
  python tools/serial_capture.py --list

  # 1) 抓 60 秒，同时打印和存文件（最常用）
  python tools/serial_capture.py --port auto --duration 60 --out logs/raw.log

  # 2) 只看 ERROR/WARN（其他丢弃，但原始行仍完整落盘）
  python tools/serial_capture.py --port COM5 --min-level W

  # 3) 不解析，纯抓字节（排查乱码/波特率问题时用）
  python tools/serial_capture.py --port COM5 --raw

  # 4) 拔线重连演练：运行中直接把 USB 拔掉再插上，观察自动恢复
  python tools/serial_capture.py --port auto --duration 120

退出时（Ctrl+C 或到时）会打印统计：行数 / 解析率 / 行长分布 / 重连次数 / seq 连续性。
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# 注意：pyserial 只在【真正打开串口】时才需要。
# --replay / 解析相关功能不依赖它，所以放在函数里延迟导入，
# 这样没装 pyserial 的同学也能先跑通解析与回放。
try:
    import serial
    import serial.tools.list_ports
    HAS_PYSERIAL = True
except ImportError:
    serial = None                      # type: ignore[assignment]
    HAS_PYSERIAL = False

PYSERIAL_HINT = (
    "缺少 pyserial，无法打开串口。请执行：\n"
    "    pip install pyserial\n"
    "（若只想离线分析已有日志，本脚本的 --replay 不需要 pyserial）"
)

# ---------------------------------------------------------------------------
# 解析：统一委托给 agri_logger.parser（不在本文件里重复实现）
#
# ★ 踩过坑的设计决策 ★
#   早期版本在本文件里自带一套正则。设备端换成"字段ID 值"新格式后，
#   采集器还在找旧的 zone=/seq=，打印出 "z=A z=A s=None" 这种错乱结果。
#   根因：解析逻辑有两份实现，改一处忘一处。
#   现在改为委托共享解析器，从结构上消除"两处不同步"的可能。
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from agri_logger.parser.parser import LogParser, LogRecord, LEVEL_ORDER
    HAS_PARSER = True
except ImportError as _e:                                    # pragma: no cover
    print(f"⚠️ 无法导入 agri_logger.parser（{_e}），将退化为不解析模式", file=sys.stderr)
    HAS_PARSER = False
    LEVEL_ORDER = {"D": 0, "I": 1, "W": 2, "E": 3, "F": 4}

    class LogRecord:                                         # type: ignore[no-redef]
        """占位类型，仅在解析器不可用时使用"""
        level = "I"
        raw_line = ""

MAX_LINE_BYTES = 128          # hilog_lite 硬限制：超过会被静默丢弃


# ---------------------------------------------------------------------------
# 解析：统一委托给 agri_logger.parser（**不在本文件里重复实现**）
#
# ★ 这是一个踩过坑的设计决策 ★
#   早期版本在本文件里自带一套正则。设备端换成"字段ID 值"新格式后，
#   采集器还在找旧的 zone=/seq=，于是打印出 "z=A z=A s=None" 这种错乱结果。
#   根因是解析逻辑有两份实现，改一处忘一处。
#   现在改为委托共享解析器，从结构上消除"两处不同步"的可能。
# ---------------------------------------------------------------------------

if HAS_PARSER:
    _SHARED_PARSER = LogParser()
else:
    _SHARED_PARSER = None


def parse_line(raw: str, line_no: int) -> Optional["LogRecord"]:
    """解析一行。永不抛异常：不匹配返回 None（计入解析失败率）。"""
    if _SHARED_PARSER is None:
        return None
    return _SHARED_PARSER.parse(raw, src_name="capture", line_no=line_no)


def classify_bad(raw: str) -> str:
    """给解析失败的行分类。报告里比一个"成功率"数字更有信息量。"""
    s = raw.strip()
    if not s:
        return "空行"
    if not re.match(r"^[\[0-9]", s):
        return "非日志格式（噪声/乱码）"
    if re.match(r"^\d{6}\.\d{6}\s", s):
        return "有 hilog 时间戳但前缀不完整"
    return "其他"


def _color(level: str) -> str:
    """给终端输出加色：ERROR 红、WARN 黄、DEBUG 灰"""
    return {"D": "\033[90m", "I": "\033[0m", "W": "\033[33m",
            "E": "\033[31m", "F": "\033[41m"}.get(level, "\033[0m")


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

@dataclass
class Stats:
    lines_total: int = 0
    parse_ok: int = 0
    parse_bad: int = 0
    bytes_total: int = 0
    reconnects: int = 0
    line_len_max: int = 0
    len_over_limit: int = 0
    level_count: Counter = field(default_factory=Counter)
    bad_reasons: Counter = field(default_factory=Counter)
    seq_expect: dict = field(default_factory=dict)      # (source, zone) -> 期望 seq
    seq_lost: Counter = field(default_factory=Counter)
    seq_seen: Counter = field(default_factory=Counter)
    fault_count: Counter = field(default_factory=Counter)   # 故障码统计
    first_ts_host: Optional[float] = None
    last_ts_host: Optional[float] = None
    gaps: list = field(default_factory=list)            # 相邻行到达间隔（秒）
    _last_arrival: Optional[float] = None

    def observe(self, rec: "LogRecord", ts_host: float) -> None:
        self.parse_ok += 1
        self.level_count[rec.level] += 1
        if self._last_arrival is not None:
            self.gaps.append(ts_host - self._last_arrival)
        self._last_arrival = ts_host
        if getattr(rec, "fault_tag", None):
            self.fault_count[rec.fault_tag] += 1
        if rec.seq is not None and rec.zone:
            key = (rec.source, rec.zone)
            exp = self.seq_expect.get(key)
            if exp is not None and rec.seq > exp:
                self.seq_lost[key] += rec.seq - exp
            self.seq_expect[key] = rec.seq + 1
            self.seq_seen[key] += 1

    def continuity(self) -> float:
        lost = sum(self.seq_lost.values())
        seen = sum(self.seq_seen.values())
        return 1.0 - lost / max(1, lost + seen)

    def report(self) -> str:
        dur = ((self.last_ts_host or 0) - (self.first_ts_host or 0))
        rate = self.parse_ok / max(1e-9, dur)
        lines = [
            "════════════ 采集统计 ════════════",
            f"总行数          {self.lines_total}",
            f"解析成功        {self.parse_ok}"
            f"  ({self.parse_ok / max(1, self.lines_total) * 100:.2f}%)",
            f"解析失败        {self.parse_bad}"
            f"  ({self.parse_bad / max(1, self.lines_total) * 100:.2f}%)",
            f"字节数          {self.bytes_total / 1024:.1f} KB",
            f"采集时长        {dur:.1f} 秒",
            f"平均速率        {rate:.2f} 行/秒",
            f"重连次数        {self.reconnects}",
            "──────── 级别分布 ────────",
        ]
        for lv in ("F", "E", "W", "I", "D"):
            if self.level_count.get(lv):
                lines.append(f"  {lv}  {self.level_count[lv]}")
        lines.append("──────── 行长检查（hilog_lite 上限 128 字节）────────")
        lines.append(f"最长行          {self.line_len_max} 字节"
                     + ("  ✅" if self.line_len_max <= MAX_LINE_BYTES else "  ❌ 超限！真机会静默丢弃"))
        lines.append(f"超限行数        {self.len_over_limit}")
        lines.append("──────── 序号连续性（丢日志检出）────────")
        lines.append(f"连续性          {self.continuity() * 100:.2f}%")
        for (src, zone), lost in sorted(self.seq_lost.items()):
            lines.append(f"  {src} zone={zone}: 丢失 {lost} 条")
        if self.gaps:
            lines.append("──────── 到达间隔（判断任务是否卡死）────────")
            lines.append(f"均值 {statistics.mean(self.gaps):.2f}s   "
                         f"中位 {statistics.median(self.gaps):.2f}s   "
                         f"最大 {max(self.gaps):.2f}s")
        if self.bad_reasons:
            lines.append("──────── 解析失败原因 ────────")
            for reason, n in self.bad_reasons.most_common():
                lines.append(f"  {n:6d}  {reason}")
        if self.fault_count:
            lines.append("──────── 故障码分布 ────────")
            for tag, n in self.fault_count.most_common(12):
                lines.append(f"  {tag:8s} {n}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------

PORT_HINTS = ("CP210", "CH340", "USB-SERIAL", "USB TO UART", "Silicon Labs", "wch")


def discover_port() -> Optional[str]:
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        return None
    if len(ports) == 1:
        return ports[0].device
    for p in ports:
        desc = f"{p.description} {p.manufacturer or ''}".upper()
        if any(h.upper() in desc for h in PORT_HINTS):
            return p.device
    return ports[0].device


def list_ports() -> int:
    if not HAS_PYSERIAL:
        print(PYSERIAL_HINT, file=sys.stderr)
        return 2
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("未发现任何串口。检查：① 数据线是否支持传输 ② 驱动是否安装（Tool/CH340）")
        return 1
    print(f"发现 {len(ports)} 个串口：")
    for p in ports:
        print(f"  {p.device:8s}  {p.description}  [{p.manufacturer or '-'}]")
    print("\n提示：拔掉板子再运行一次，少掉的那个就是它。")
    return 0


def open_port(port: str, baud: int, dtr: bool = True, rts: bool = True) -> "serial.Serial":
    """打开串口。

    ★★★ FS-Hi3861 的 DTR/RTS 有一个"自动复位电路"，这是踩了四次坑才搞清的 ★★★

    该板子把 CH340 的 DTR/RTS 接到了芯片的复位/使能脚，构成自动复位电路。
    只有【同时拉高 DTR 和 RTS】才释放复位、板子才开始运行并输出日志。

    实测记录（同一块板子，不同配置的结果）：
        DTR=0 RTS=0  →  0 字节
        DTR=1 RTS=0  →  0 字节
        DTR=0 RTS=1  →  0 字节
        DTR=1 RTS=1  →  ✅ 正常输出          ← 本项目采用

    重要推论：**复位不是靠按键触发的，而是靠串口的 DTR/RTS 电平。**
    所以：
      · 打开串口后板子会自动复位一次并打印完整启动日志（这是正常的，不是 bug）
      · 如果用户手动按了复位键，同时串口电平不对，板子会一直停在复位态 → 0 字节

    验证方法：python tools/uart_diagnose.py   （自动遍历四种组合并报告哪个有数据）

    ⚠️ 另一个独立且必须知道的坑（我们踩过）：
        **烧录后必须物理拔插一次 USB**。
        HiBurn 烧录会把芯片置于 bootloader 模式，若未能正常退出，
        板子会停在那里【连 ROM 引导日志都不打印】，现象与"板子坏了"完全一样。
        连续多次烧录而不断电，出现该状态的概率明显上升。
        解决办法：拔掉 USB → 等 5 秒 → 重新插上。
    """
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.bytesize = serial.EIGHTBITS
    ser.parity = serial.PARITY_NONE
    ser.stopbits = serial.STOPBITS_ONE
    ser.timeout = 0.5
    ser.dtr = dtr
    ser.rts = rts
    ser.open()
    return ser


def run(args: argparse.Namespace) -> Stats:
    if not HAS_PYSERIAL:
        print(PYSERIAL_HINT, file=sys.stderr)
        raise SystemExit(2)
    stats = Stats()
    out = open(args.out, "w", encoding="utf-8", newline="") if args.out else None
    backoff = 1.0
    line_no = 0
    start = time.time()
    buf = b""

    try:
        while True:
            if args.duration and time.time() - start >= args.duration:
                break
            port = args.port
            if port == "auto":
                port = discover_port()
                if not port:
                    print(f"[auto] 未发现串口，{backoff:.0f}s 后重试…", flush=True,
                          file=sys.stderr)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
            try:
                ser = open_port(port, args.baud)
            except (serial.SerialException, OSError) as e:
                print(f"[{port}] 打开失败（{e}），{backoff:.0f}s 后重试…", flush=True,
                      file=sys.stderr)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                stats.reconnects += 1
                continue

            # ★ 人看的提示写 stderr，绝不写 stdout：
            #   stdout 是给管道/看板用的数据通道，混进提示行会让下游把它当日志解析。
            print(f"[{port}] 已连接 @ {args.baud} 8N1"
                  + (f"，输出到 {args.out}" if args.out else "")
                  + "\n（Ctrl+C 结束）", flush=True, file=sys.stderr)
            backoff = 1.0
            try:
                while True:
                    if args.duration and time.time() - start >= args.duration:
                        raise KeyboardInterrupt
                    chunk = ser.read(4096)
                    if not chunk:
                        continue
                    stats.bytes_total += len(chunk)
                    buf += chunk
                    # ★ 必须缓冲残行：一次 read() 很可能只拿到半行
                    while b"\n" in buf:
                        raw_bytes, buf = buf.split(b"\n", 1)
                        line_no += 1
                        stats.lines_total += 1
                        now = time.time()
                        if stats.first_ts_host is None:
                            stats.first_ts_host = now
                        stats.last_ts_host = now

                        raw = raw_bytes.decode("utf-8", errors="replace").rstrip("\r")
                        nbytes = len(raw_bytes) + 1
                        stats.line_len_max = max(stats.line_len_max, nbytes)
                        if nbytes > MAX_LINE_BYTES:
                            stats.len_over_limit += 1

                        if out:
                            out.write(raw + "\n")
                            out.flush()

                        if args.raw:
                            print(raw, flush=True)
                            stats.parse_ok += 1
                            continue

                        rec = parse_line(raw, line_no)
                        if rec is None:
                            stats.parse_bad += 1
                            stats.bad_reasons[classify_bad(raw)] += 1
                            if args.show_bad:
                                print(f"\033[90m[未解析] {raw}\033[0m", flush=True)
                            continue
                        stats.observe(rec, now)
                        if LEVEL_ORDER[rec.level] >= LEVEL_ORDER[args.min_level]:
                            # 直接复用共享解析器的 summary()，保证与下游模块展示一致
                            print(f"{_color(rec.level)}{rec.summary()}\033[0m", flush=True)
                    if len(buf) > 8192:        # 保护：异常超长残行
                        stats.parse_bad += 1
                        stats.bad_reasons["超长残行（无换行符）"] += 1
                        buf = b""
            except (serial.SerialException, OSError) as e:
                stats.reconnects += 1
                print(f"\n[!] 连接断开（{e}），{backoff:.0f}s 后重连…", flush=True,
                      file=sys.stderr)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                try:
                    ser.close()
                except Exception:
                    pass
    except KeyboardInterrupt:
        print("\n[停止] 用户中断", flush=True, file=sys.stderr)
    finally:
        if out:
            out.close()

    # ★ 统计报表一律走 stderr：stdout 是"数据通道"，只能有日志行。
    #   否则下游（看板 --live）会把 "已连接 @ 115200" 这类提示当成待解析的日志，
    #   白白拉低解析率 —— 实测踩过：解析率 97% 而不是 100%，原因就是这两行提示。
    print(file=sys.stderr)
    print(stats.report(), file=sys.stderr)
    if args.out:
        print(f"\n原始日志已保存：{args.out}", file=sys.stderr)
        print(f"回看命令：python tools/serial_capture.py --replay {args.out} --min-level E",
              file=sys.stderr)
    return stats


def replay(path: str, min_level: str) -> int:
    """离线回放已抓取的日志（不需要插板子）"""
    total = ok = bad = 0
    for i, line in enumerate(Path(path).read_text(encoding="utf-8",
                                                  errors="replace").splitlines(), 1):
        total += 1
        rec = parse_line(line, i)
        if rec is None:
            bad += 1
            continue
        ok += 1
        if LEVEL_ORDER[rec.level] >= LEVEL_ORDER[min_level]:
            print(rec.summary())
    print(f"\n回放完成：{total} 行，解析成功 {ok}（{ok / max(1, total) * 100:.1f}%），失败 {bad}")
    return 0


def monitor(args: argparse.Namespace) -> int:
    """串口监视器：把收到的每个字节块都带时间戳打出来。

    和采集器的区别：这里【不做任何解析、不做行缓冲】，纯看物理链路上有什么。
    专门用来回答"到底通不通"：
      · 按下板子复位键 → 应该看到一大段启动日志刷出来
      · 拔掉 USB 再插上 → 应该看到端口消失又出现
      · 一个字都没有 → 波特率不对 / 选错端口 / 固件不打印
    """
    if not HAS_PYSERIAL:
        print(PYSERIAL_HINT, file=sys.stderr)
        return 2

    port = args.port if args.port != "auto" else discover_port()
    if not port:
        print("未发现串口。先运行 --list 看看。", file=sys.stderr)
        return 1

    print(f"监视 {port} @ {args.baud} 8N1（Ctrl+C 结束）")
    print("现在请按板子上的【复位键】——正常情况下会立刻刷出一大段启动日志。\n")

    t0 = time.time()
    total = 0
    try:
        with open_port(port, args.baud) as ser:
            while True:
                chunk = ser.read(256)
                if chunk:
                    total += len(chunk)
                    text = chunk.decode("utf-8", errors="replace")
                    # 保留 \r\n 的形状，让乱码/半行一眼可见
                    shown = text.replace("\r", "\\r").replace("\n", "\\n\n")
                    print(f"[{time.time() - t0:7.3f}s +{len(chunk):4d}B] {shown}",
                          end="" if not shown.endswith("\n") else "")
                    sys.stdout.flush()
                elif args.duration and time.time() - t0 >= args.duration:
                    break
    except KeyboardInterrupt:
        pass
    except (serial.SerialException, OSError) as e:
        print(f"\n[!] 串口异常：{e}", file=sys.stderr)
        return 1

    print(f"\n─── 监视结束：共收到 {total} 字节 ───")
    if total == 0:
        print("一个字节都没收到。按顺序排查：")
        print("  1) 端口选对了吗？拔掉板子再跑 --list，少掉的那个才是它")
        print("  2) 波特率对吗？试 921600 和 9600（FS-Hi3861 日志口通常 115200）")
        print("  3) 刚才按复位键了吗？不按的话有些固件不打印任何东西")
        print("  4) 烧的固件本身就不打印？换 base_06_i2c_sht20 试（它每 2 秒打一次）")
        print("  5) 数据线是充电线吗？换一根能传数据的")
    else:
        print("收到数据了 ✅ 链路是通的。接下来用采集器正式抓：")
        print(f"  python tools/serial_capture.py --port {port} --duration 60 --out logs/raw.log")
    return 0


def scan_baud(args: argparse.Namespace) -> int:
    """波特率扫描：逐个波特率试读，报告哪个有数据。

    用于排查"串口能打开但收不到任何字节"这类问题——最常见的原因是波特率不匹配。
    每个波特率监听 --dwell 秒（期间请反复按板子复位键）。
    """
    if not HAS_PYSERIAL:
        print(PYSERIAL_HINT, file=sys.stderr)
        return 2

    port = args.port if args.port != "auto" else discover_port()
    if not port:
        print("未发现串口。先运行 --list。", file=sys.stderr)
        return 1

    rates = [int(r) for r in args.baud_list.split(",")]
    print(f"扫描 {port}，依次尝试 {len(rates)} 个波特率")
    print("★ 每换一个波特率，就按一下板子上的【复位键】——一直按，不要停\n")

    results: list[tuple[int, int, str]] = []
    for rate in rates:
        try:
            with open_port(port, rate) as ser:
                ser.reset_input_buffer()
                buf = b""
                deadline = time.time() + args.dwell
                while time.time() < deadline:
                    chunk = ser.read(512)
                    if chunk:
                        buf += chunk
                        if len(buf) > 60000:
                            break
        except (serial.SerialException, OSError) as e:
            print(f"  {rate:>7}  ✗ 打开失败：{e}")
            results.append((rate, 0, ""))
            continue

        sample = buf.decode("utf-8", errors="replace")
        mark = "✓ 有数据！" if buf else "✗ 无数据"
        print(f"  {rate:>7}  {mark}  收到 {len(buf)} 字节")
        results.append((rate, len(buf), sample))
        if buf:
            preview = sample.replace("\r", "\\r").replace("\n", "\\n\n")
            for line in preview.splitlines()[:6]:
                print(f"          {line}")
            print()

    print("\n════════ 扫描结果 ════════")
    hit = [(r, n, s) for r, n, s in results if n > 0]
    if not hit:
        print("所有波特率都收不到数据。这说明问题不在波特率，按顺序查：")
        print("  1) 【按复位键了吗】——最容易忽略。不按复位，板子可能停在 bootloader，不跑应用")
        print("  2) 拔掉 USB 重新插上（彻底断电重启）")
        print("  3) 端口选对了吗？拔掉板子再跑 --list，少掉的那个才是它")
        print("  4) 数据线是充电线吗？换一根能传数据的")
        print("  5) 板子有没有另一个 USB 口/排针是专门输出日志的？")
        print("     FS-Hi3861 有 Type-C（下载+日志）与 USB-A 两个座子，确认烧录用的是哪个")
        return 1

    rate, n, sample = max(hit, key=lambda x: x[1])
    print(f"★ 推荐波特率：{rate}（收到 {n} 字节）")
    print(f"\n接下来用这个波特率正式采集：")
    print(f"  .venv\\Scripts\\python.exe tools/serial_capture.py "
          f"--port {port} --baud {rate} --duration 60 --out logs/raw.log")
    return 0


def check_env() -> int:
    """一键环境自检：在 VSCode 终端里先跑这个，缺什么一目了然。"""
    import platform
    print("════════ 环境自检 ════════")
    print(f"Python 版本    {sys.version.split()[0]}  ({platform.architecture()[0]})")
    print(f"Python 路径    {sys.executable}")
    print("               ↑ 这就是终端里敲 python 实际用的解释器。")
    print("                 VSCode 里选的解释器【不影响】终端，以这一行为准。")
    if sys.version_info < (3, 9):
        print("  说明：本项目全部脚本已在 3.8.10 上实测通过（无 3.9+ 专属语法）。")
        print("        仅提示：Python 3.8 已于 2024-10 停止安全更新，方便时升级到 3.10+ 即可，")
        print("        但不是现在必须做的事——不影响本项目任何功能。")
    print(f"当前目录       {Path.cwd()}")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print("标准输出编码   utf-8  ✅")
    except Exception:
        print(f"标准输出编码   {sys.stdout.encoding}  ⚠️ 中文可能乱码")
    print(f"pyserial       {'已安装 ' + serial.__version__ if HAS_PYSERIAL else '❌ 未安装'}")
    if HAS_PYSERIAL:
        print(f"               位置 {serial.__file__}")
        print("               ↑ 与上面的 Python 路径同属一个 Python 即为正确。")
    if not HAS_PYSERIAL:
        # ★ 关键：用 sys.executable 拼命令，保证装进【当前这个】解释器。
        #   很多人 pip install 装到了另一个 Python 上，然后奇怪"明明装了却 import 不到"。
        print("   → 本机请执行下面这一行（复制粘贴即可，它会装进当前解释器）：")
        print(f'     & "{sys.executable}" -m pip install pyserial')
        print("   PowerShell 提示：命令以引号开头时会被当成字符串，必须加调用运算符 &。")
        print("   若 PATH 上的 python 就是它，直接 `python -m pip install pyserial` 最省事。")
        print("   装完请重新运行 --check-env 确认。")
    if HAS_PYSERIAL:
        ports = list(serial.tools.list_ports.comports())
        print(f"串口数量       {len(ports)}")
        for p in ports:
            print(f"   {p.device:8s} {p.description}  [{p.manufacturer or '-'}]")
        if not ports:
            print("   → 一个都没发现。检查：① 数据线是否支持传输 ② 驱动（Tool/CH340）③ 板子是否上电")
    baud_hint = ("若日志是乱码，依次试：115200 → 921600 → 9600。"
                 "FS-Hi3861 的日志口通常是 115200。")
    print(f"提示           {baud_hint}")
    print("\n下一步：")
    print("  1) 关掉 DevEco 的 Monitor 串口工具（它和本脚本不能同时占用同一个 COM 口）")
    print("  2) python tools/serial_capture.py --port auto --duration 60 --out logs/raw.log")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="串口日志采集器（FS-Hi3861 / STM32 → PC）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--check-env", action="store_true",
                   help="环境自检（Python 版本/pyserial/串口/编码）后退出")
    p.add_argument("--monitor", action="store_true",
                   help="串口监视器：原样打印收到的字节（不理解析），用来排查'通不通'")
    p.add_argument("--scan-baud", action="store_true",
                   help="波特率扫描：逐个波特率试读，报告哪个有数据（排查收不到字节）")
    p.add_argument("--baud-list", default="115200,921600,57600,9600,38400,230400,460800",
                   help="扫描的波特率列表，逗号分隔")
    p.add_argument("--dwell", type=float, default=5.0,
                   help="每个波特率监听多少秒（扫描期间请反复按复位键）")
    p.add_argument("--list", action="store_true", help="列出所有串口后退出")
    p.add_argument("--port", default="auto", help="串口号，或 auto 自动发现")
    p.add_argument("--baud", type=int, default=115200, help="波特率")
    p.add_argument("--duration", type=float, default=0, help="采集时长（秒，0=一直跑）")
    p.add_argument("--out", default=None, help="原始日志落盘路径（建议 logs/raw.log）")
    p.add_argument("--min-level", default="I", choices=list("DIWEF"),
                   help="屏幕只显示该级别及以上（不影响落盘）")
    p.add_argument("--raw", action="store_true", help="不解析，原样打印（排查波特率用）")
    p.add_argument("--show-bad", action="store_true", help="打印未能解析的行")
    p.add_argument("--replay", metavar="FILE", default=None,
                   help="离线回放已抓取的日志文件")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)
    if args.check_env:
        return check_env()
    if args.scan_baud:
        return scan_baud(args)
    if args.monitor:
        return monitor(args)
    if args.list:
        return list_ports()
    if args.replay:
        return replay(args.replay, args.min_level)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
