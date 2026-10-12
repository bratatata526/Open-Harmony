#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
把 DevEco 的串口号自动改成【当前实际检测到的】那个。

为什么需要这个工具
------------------
Windows 是**按物理 USB 插口**分配 COM 号的。同一个板子换个 USB 口，
号就变了（本项目实测一轮里出现过 COM3 → COM4 → COM5 → COM3）。
而 DevEco 的 `upload_port` 是**写死在 .deveco/deveco.ini 里的**。

后果非常容易误判：
    板子其实在 COM5，配置还写着 COM3
    → 点 Upload 报 `error 4: upload port is occupied`
    → 看起来像"串口被占用"，于是去关 Monitor、杀进程、查句柄……
    → 实际上根本没人占用，只是**那个端口号不存在**

实测踩过两次，每次都浪费十几分钟。所以做成一条命令。

用法
----
    python tools/sync_com_port.py            # 检测并写入
    python tools/sync_com_port.py --check    # 只报告，不改
    python tools/sync_com_port.py --port COM7   # 强制指定
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INI = ROOT / "hi3861_application" / ".deveco" / "deveco.ini"
ARC = ROOT / "firmware" / "sdk-integration" / "deveco.ini"

# 常见 USB 转串口芯片关键字，用来从多个口里挑出开发板
HINTS = ("CH340", "CH341", "CP210", "FT232", "USB-SERIAL", "SILICON")


def detect_port():
    """返回 (端口名, 说明)。找不到返回 (None, 原因)。"""
    try:
        import serial.tools.list_ports as lp
    except ImportError:
        return None, "没装 pyserial"
    ports = list(lp.comports())
    if not ports:
        return None, "系统里一个串口都没有（板子没插 / 驱动没装 / 线只能供电）"
    if len(ports) == 1:
        return ports[0].device, ports[0].description or ""
    for p in ports:
        d = ("%s %s" % (p.description or "", p.manufacturer or "")).upper()
        if any(h in d for h in HINTS):
            return p.device, p.description or ""
    return ports[0].device, "%s（多个口中选了第一个）" % (ports[0].description or "")


def main(argv=None):
    ap = argparse.ArgumentParser(description="把 DevEco 串口号改成实际检测到的那个")
    ap.add_argument("--check", action="store_true", help="只报告，不写入")
    ap.add_argument("--port", default=None, help="强制指定端口，跳过自动检测")
    args = ap.parse_args(argv)

    if not INI.exists():
        print("✘ 找不到 %s" % INI)
        print("  提示：SDK 目录不在仓库里，先按 firmware/README.md 获取一份。")
        return 2

    if args.port:
        port, desc = args.port, "（命令行指定）"
    else:
        port, desc = detect_port()
    if not port:
        print("✘ 检测不到串口：%s" % desc)
        return 1

    text = INI.read_text(encoding="utf-8", errors="replace")
    cur_up = (re.search(r"upload_port\s*=\s*(\S+)", text) or [None, "?"])[1]
    cur_mo = (re.search(r"monitor_port\s*=\s*(\S+)", text) or [None, "?"])[1]

    print("检测到的串口 : %s   %s" % (port, desc))
    print("当前配置     : upload_port=%s  monitor_port=%s" % (cur_up, cur_mo))
    print()

    if cur_up == port and cur_mo == port:
        print("✔ 配置已经是对的，无需改动")
        return 0

    if args.check:
        print("✘ 配置与实际不符 —— 直接点 Upload 会报 'upload port is occupied'")
        print("  （那个报错有误导性：其实不是被占用，是【这个端口号不存在】）")
        print("  跑一次不带 --check 的本脚本即可修正。")
        return 1

    new = re.sub(r"upload_port\s*=\s*\S+", "upload_port = %s" % port, text)
    new = re.sub(r"monitor_port\s*=\s*\S+", "monitor_port = %s" % port, new)
    INI.write_text(new, encoding="utf-8")

    # 同步一份到 firmware/ 存档，队友拿到的就是能用的配置
    if ARC.parent.exists():
        ARC.write_text(new, encoding="utf-8")
        print("✔ 已更新 %s" % INI.relative_to(ROOT))
        print("✔ 已同步 %s" % ARC.relative_to(ROOT))
    else:
        print("✔ 已更新 %s" % INI.relative_to(ROOT))

    print()
    print("现在可以去 DevEco 点 Upload 了。")
    print("⚠️ 如果板子又换了 USB 口，号还会变 —— 再跑一次本脚本即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
