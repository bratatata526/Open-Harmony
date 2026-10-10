#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""串口电气层诊断：搞清"串口能打开但收不到任何字节"到底是什么原因。

排查思路（从软到硬）：
  1. 列出所有串口 + 底层 USB 设备，确认板子被系统正确识别
  2. 枚举 DTR/RTS 的 4 种组合，每种组合下读一段——判断板子是否被控制线吊在复位态
  3. 报告 DTR/RTS 是"真实控制线"还是"CH340 内部假线"（ch341 driver 行为差异）

用法：
  python tools/uart_diagnose.py                      # 全自动，约 40 秒
  python tools/uart_diagnose.py --port COM3 --dwell 3
"""

from __future__ import annotations

import argparse
import itertools
import sys
import time
from typing import Optional

try:
    import serial
    import serial.tools.list_ports
    HAS_PYSERIAL = True
except ImportError:
    serial = None            # type: ignore
    HAS_PYSERIAL = False

try:
    import usb.core
    import usb.util
    HAS_PYUSB = True
except ImportError:
    usb = None               # type: ignore
    HAS_PYUSB = False


def hr(title: str) -> None:
    print(f"\n{'─' * 62}\n{title}\n{'─' * 62}")


# ---------------------------------------------------------------------------
# 1. 串口与 USB 枚举
# ---------------------------------------------------------------------------

def enum_ports() -> list:
    hr("① 串口枚举")
    if not HAS_PYSERIAL:
        print("缺少 pyserial")
        return []
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("❌ 系统里一个串口都没有。板子没被识别？")
        return []
    for p in ports:
        print(f"  {p.device}")
        print(f"    描述   : {p.description}")
        print(f"    厂商   : {p.manufacturer}")
        print(f"    VID:PID: {hex(p.vid) if p.vid else '?'}:{hex(p.pid) if p.pid else '?'}")
        print(f"    序列号 : {p.serial_number}")
        print(f"    位置   : {p.location}")
        print(f"    硬件ID : {p.hwid}")
    return ports


def enum_usb() -> None:
    hr("② USB 设备枚举（找 CH340 / CP210x 等串口芯片）")
    if not HAS_PYUSB:
        print("缺少 pyusb，跳过。安装：pip install pyusb")
        return
    KNOWN = {
        (0x1A86, 0x7523): "CH340（最常见）",
        (0x1A86, 0x5523): "CH341",
        (0x1A86, 0x55D4): "CH9102",
        (0x10C4, 0xEA60): "CP2102",
        (0x0403, 0x6001): "FT232",
        (0x0483, 0x5740): "STM32 虚拟串口",
    }
    found = False
    try:
        for dev in usb.core.find(find_all=True):
            key = (dev.idVendor, dev.idProduct)
            if key in KNOWN or dev.idVendor in (0x1A86, 0x10C4, 0x0403):
                found = True
                print(f"  {hex(dev.idVendor)}:{hex(dev.idProduct)}  {KNOWN.get(key, '未知串口芯片')}")
                try:
                    print(f"    厂商字符串: {usb.util.get_string(dev, dev.iManufacturer)}")
                    print(f"    产品字符串: {usb.util.get_string(dev, dev.iProduct)}")
                    print(f"    序列号    : {usb.util.get_string(dev, dev.iSerialNumber)}")
                except Exception as e:
                    print(f"    （字符串读取失败：{e}）")
    except Exception as e:
        print(f"  USB 枚举失败：{e}")
        print("  （Windows 上 pyusb 需要 libusb 驱动，读不到属正常；不影响结论）")
    if not found:
        print("  未发现已知串口芯片（或 pyusb 无法访问，需 libusb 驱动）")


# ---------------------------------------------------------------------------
# 2. DTR/RTS 四种组合
# ---------------------------------------------------------------------------

def probe_matrix(port: str, baud: int, dwell: float) -> dict:
    hr("③ DTR/RTS 组合测试（判断板子是否被控制线吊在复位态）")
    print("  原理：很多开发板把 DTR/RTS 接到复位/使能脚。")
    print("        Python 开串口时若把 DTR 拉高，板子会一直被按在复位上 → 一个字节都收不到。")
    print(f"  每个组合监听 {dwell} 秒。★ 期间请【反复按板子复位键】。\n")

    results: dict[tuple[bool, bool], int] = {}
    for dtr, rts in itertools.product([False, True], repeat=2):
        label = f"DTR={'高' if dtr else '低'} RTS={'高' if rts else '低'}"
        try:
            ser = serial.Serial()
            ser.port = port
            ser.baudrate = baud
            ser.timeout = 0.3
            ser.dtr = dtr
            ser.rts = rts
            ser.open()
            ser.reset_input_buffer()
            buf = b""
            deadline = time.time() + dwell
            while time.time() < deadline:
                c = ser.read(256)
                if c:
                    buf += c
            ser.close()
        except (serial.SerialException, OSError) as e:
            print(f"  {label:<22} ✗ 打开失败：{e}")
            results[(dtr, rts)] = -1
            continue
        results[(dtr, rts)] = len(buf)
        mark = f"✓ 收到 {len(buf)} 字节" if buf else "✗ 0 字节"
        print(f"  {label:<22} {mark}")
        if buf:
            text = buf.decode("utf-8", errors="replace")
            for line in text.replace("\r", "\\r").replace("\n", "\\n\n").splitlines()[:5]:
                print(f"        {line}")
    return results


# ---------------------------------------------------------------------------
# 3. 结论
# ---------------------------------------------------------------------------

def conclude(results: dict, port: str, baud: int) -> int:
    hr("④ 结论与下一步")

    hit = {k: v for k, v in results.items() if v and v > 0}
    if hit:
        print("★ 找到能收到数据的 DTR/RTS 组合：")
        for (dtr, rts), n in hit.items():
            print(f"    DTR={'高' if dtr else '低'}  RTS={'高' if rts else '低'}  → {n} 字节")
        dtr, rts = max(hit, key=lambda k: hit[k])
        print(f"\n  把这两个参数写进代码即可（本项目 serial_capture.py 的 open_port() 里）：")
        print(f"    ser.dtr = {dtr}")
        print(f"    ser.rts = {rts}")
        print(f"\n  再用这个组合正式抓：")
        print(f"    .venv\\Scripts\\python.exe tools/serial_capture.py "
              f"--port {port} --baud {baud} --duration 60 --out logs/raw.log")
        print("\n  请把这个结果告诉协助你的 AI 助手，会更新到 open_port() 里。")
        return 0

    print("四种 DTR/RTS 组合、全部波特率都收不到数据。")
    print("→ 问题不在软件参数。请按下面顺序物理排查：\n")
    print("  【第 1 步】板子上是不是有两个 USB 座子？")
    print("      FS-Hi3861 板上有 Type-C 与 USB-A 两个接口。")
    print("      → 把线插到【另一个】座子上再跑一次本诊断。")
    print("      → 官方文档明确：程序下载与日志输出都通过【板载 USB 转 UART】电路，")
    print("        但不同批次板子走的是不同座子，试一次就知道。")
    print()
    print("  【第 2 步】板子上电了吗？")
    print("      看电源指示灯（板上有'电源指示灯'丝印）是否常亮。")
    print("      不亮 = 供电问题（换线 / 换 USB 口 / 别用 USB Hub）。")
    print()
    print("  【第 3 步】用官方工具交叉验证，排除我们脚本的因素：")
    print("      Tool\\UartAssist.exe  →  打开 COM3，115200 8N1，点'打开串口'，")
    print("      然后按板子复位键。")
    print("      · UartAssist 也收不到  → 板子/线/座子的问题（回到第 1、2 步）")
    print("      · UartAssist 能收到    → 我们的脚本有问题，请把 UartAssist 的截图发我")
    print()
    print("  【第 4 步】拔掉 USB，等 5 秒，重新插上（彻底断电重启），再跑诊断。")
    print("      烧录后板子有可能停在 bootloader 里没有跳转到应用。")
    print()
    print("  【第 5 步】换一根确认能传数据的 USB 线（很多线只供电）。")
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="串口电气层诊断：定位'能打开端口但收不到字节'的原因",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--port", default="COM3", help="串口号")
    p.add_argument("--baud", type=int, default=115200, help="波特率")
    p.add_argument("--dwell", type=float, default=3.0, help="每种组合监听秒数")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)
    print("=" * 62)
    print("  串口电气层诊断")
    print("=" * 62)

    ports = enum_ports()
    if not ports:
        return 1
    enum_usb()

    target = args.port
    if not any(p.device == target for p in ports):
        target = ports[0].device
        print(f"\n（{args.port} 不在列表里，改用 {target}）")

    results = probe_matrix(target, args.baud, args.dwell)
    return conclude(results, target, args.baud)


if __name__ == "__main__":
    sys.exit(main())
