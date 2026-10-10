#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
固件源码同步：firmware/（版本存档，git 跟踪）  →  SDK 部署目录（编译源头，不入 git）

━━━ 为什么默认是这个方向 ━━━
本项目里固件源码有两份：

  · firmware/hi3861/agri_logger/           ← git 跟踪的【版本存档】，改代码改这里
  · hi3861_application/.../agri_logger/    ← SDK 里的【编译源头】，编译器只认这份

因为 SDK 不入 git（4 GB），**唯一可靠的版本记录就是 firmware/**。
所以工作流固定为：

    改 firmware/  →  跑本脚本部署到 SDK  →  在 DevEco 里 Rebuild

━━━ 踩过的两次事故 ━━━
  1. 手工 copy 时方向写反，把刚加的一行 BUILD.gn 覆盖没了，白烧一次板子。
  2. 本脚本最初把默认方向写成了 SDK → firmware，结果跑一次就把
     firmware/ 里刚改好的代码覆盖回旧版 —— 正是脚本本该防止的错误。
  现在方向固定为 firmware → SDK，要反向必须显式加 --from-sdk。

用法
----
    python tools/sync_firmware.py               # 部署：firmware → SDK
    python tools/sync_firmware.py --check       # 只核对两边是否一致，不改动
    python tools/sync_firmware.py --from-sdk    # 反向：SDK → firmware（谨慎）
"""

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# firmware/ 是版本源头；SDK 是编译源头
ARC_DIR = ROOT / "firmware" / "hi3861" / "agri_logger"
SDK_DIR = (ROOT / "hi3861_application" / "src" / "applications"
           / "sample" / "wifi-iot" / "app" / "agri_logger")

# 同步范围。agri_logger_demo.c.reference 是阶段 1 参考实现，不参与编译，两边都不放。
FILES = ["agri_logger_multi.c", "log_def.h", "BUILD.gn"]


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="固件源码同步（默认方向：firmware → SDK）")
    ap.add_argument("--check", action="store_true", help="只核对，不写入")
    ap.add_argument("--from-sdk", action="store_true",
                    help="反向同步 SDK → firmware（仅当你确实在 SDK 里改的代码）")
    args = ap.parse_args(argv)

    if args.from_sdk:
        src_dir, dst_dir = SDK_DIR, ARC_DIR
        direction = "SDK（编译源头） → firmware/（存档）   ⚠️ 反向模式"
    else:
        src_dir, dst_dir = ARC_DIR, SDK_DIR
        direction = "firmware/（版本源头，git 跟踪） → SDK（编译源头）"

    if not src_dir.is_dir():
        print("✘ 找不到源目录：%s" % src_dir)
        if not args.from_sdk:
            print("  提示：SDK 目录不在仓库里，需要先按 firmware/README.md 获取一份 SDK。")
        return 2

    dst_dir.mkdir(parents=True, exist_ok=True)
    print("同步方向：%s\n" % direction)

    changed = 0
    for name in FILES:
        src, dst = src_dir / name, dst_dir / name
        if not src.exists():
            print("  ✘ 源目录里没有 %s" % name)
            changed += 1
            continue
        if dst.exists() and sha(src) == sha(dst):
            print("  = %-24s 已一致  %s" % (name, sha(src)[:12]))
            continue
        if args.check:
            print("  ! %-24s 两边不一致（--check 模式，未写入）" % name)
            changed += 1
            continue
        shutil.copy2(src, dst)
        ok = sha(src) == sha(dst)
        print("  %s %-24s %s  %s"
              % ("✔" if ok else "✘", name,
                 "新增" if not dst.exists() else "已更新", sha(src)[:12]))
        changed += 1

    if args.check:
        print()
        if changed:
            print("✘ 有 %d 个文件两边不一致 —— 跑一次不带 --check 的同步" % changed)
            return 1
        print("✔ 全部一致")
        return 0

    print()
    if changed:
        print("✔ 已部署 %d 个文件到 SDK —— 现在可以去 DevEco 里 Rebuild 了" % changed)
    else:
        print("✔ 无需改动，两边本来就一致")
    print("  下次烧录后记得跑 tools/check_firmware.py 确认固件版本号对得上")
    return 0


if __name__ == "__main__":
    sys.exit(main())
