#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
固件源码同步：SDK 部署目录  →  firmware/ 存档目录

为什么要这个脚本
----------------
本项目里固件源码有两份：
  · 编译源头：hi3861_application/src/applications/sample/wifi-iot/app/agri_logger/
  · 版本存档：firmware/hi3861/agri_logger/

**只有 SDK 那份会被编译。** 两份必须保持一致，否则会出现
"改了源码、现象没变"的假象（本项目已经踩过：坑 11）。

踩过的真实事故：手工同步时把方向写反了（存档 → SDK），
把刚加的一行 BUILD.gn 覆盖没了，白烧一次板子。
所以把方向【固定在这个脚本里】，不要再手工 copy。

用法
----
    python tools/sync_firmware.py            # 同步并核对
    python tools/sync_firmware.py --check    # 只核对，不改动
"""

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SDK_DIR = ROOT / "hi3861_application" / "src" / "applications" / "sample" / "wifi-iot" / "app" / "agri_logger"
ARC_DIR = ROOT / "firmware" / "hi3861" / "agri_logger"

# 只同步这几个文件。存档目录里的其他文件（如 *.c.reference）不在同步范围。
FILES = ["agri_logger_multi.c", "log_def.h", "BUILD.gn"]


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description="SDK 固件源码 → firmware/ 存档（方向固定）")
    ap.add_argument("--check", action="store_true", help="只核对，不写入")
    args = ap.parse_args(argv)

    if not SDK_DIR.is_dir():
        print("✘ 找不到 SDK 部署目录：%s" % SDK_DIR)
        return 2

    ARC_DIR.mkdir(parents=True, exist_ok=True)
    print("同步方向：SDK（编译源头） → firmware/（存档）\n")

    bad = 0
    for name in FILES:
        src, dst = SDK_DIR / name, ARC_DIR / name
        if not src.exists():
            print("  ✘ SDK 里没有 %s" % name)
            bad += 1
            continue
        same = dst.exists() and sha(src) == sha(dst)
        if same:
            print("  = %-24s 已一致  %s" % (name, sha(src)[:12]))
            continue
        if args.check:
            print("  ! %-24s 不一致（--check 模式，未写入）" % name)
            bad += 1
            continue
        shutil.copy2(src, dst)
        ok = sha(src) == sha(dst)
        print("  %s %-24s %s → 已写入  %s"
              % ("✔" if ok else "✘", name,
                 "新增" if not dst.exists() else "更新", sha(src)[:12]))
        if not ok:
            bad += 1

    # 存档目录里多出来的 .c 文件？提醒一下（可能是改名或删除后忘了同步）
    for p in sorted(ARC_DIR.glob("*.c")):
        if p.name not in FILES and not p.name.endswith(".reference"):
            print("  ? 存档里有 SDK 中不存在的文件：%s（确认是否需要删除）" % p.name)

    print()
    if bad:
        print("✘ %d 个文件需要处理" % bad)
        return 1
    print("✔ 全部一致 —— 可以放心编译")
    return 0


if __name__ == "__main__":
    sys.exit(main())
