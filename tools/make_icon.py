#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成启动器图标：像素风扳手 → assets/wrench.ico + assets/wrench.png

纯标准库实现（zlib + struct），**不依赖 Pillow** —— 与本项目"零额外依赖"的
定位一致。图标是"资产"，生成脚本入库、产物也入库，随时可重新生成。

为什么不是手搓点阵
    16×16 手绘竖版扳手辨识度不够（试过两版：像叉子 / 像音叉）。
    改 用【参数化 + 旋转 45°】栅格化：
        · 左下 = 梅花环（带孔）
        · 中间 = 手柄（斜向 45°）
        · 右上 = 开口钳（C 形，留缝）
    45° 斜置是扳手图标的通用形制，小尺寸下最不容易误读。
    16×16 采样 → 之后只做整数倍最近邻放大，保持像素感（不插值、不模糊）。

ICO 规格
    16 / 32 / 48 / 64  → 32bpp BGRA DIB + AND 掩码（最兼容）
    128 / 256          → PNG 压缩帧（Vista+ 标准做法，体积小）

用法
    .venv\\Scripts\\python.exe tools/make_icon.py
    .venv\\Scripts\\python.exe tools/make_icon.py --preview   # 8× 放大预览
"""

from __future__ import annotations

import argparse
import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

BASE = 16          # 设计尺寸（像素点阵）
ART_SIZE = BASE    # 之后所有尺寸都由它整数倍放大得到

# 调色板：0=透明 2=主体 3=高光 4=暗面（取自 web 主题）
CLEAR = (0, 0, 0, 0)
BODY = (0x3C, 0xE6, 0x8B, 255)     # --phosphor
LIGHT = (0xB9, 0xFF, 0xD9, 255)    # 高光
DARK = (0x1E, 0x9C, 0x5C, 255)     # 暗面


# ---------------------------------------------------------------------------
# 扳手形状（参数空间：x 沿手柄方向，钳口在 +x，梅花环在 -x）
# ---------------------------------------------------------------------------

def in_wrench(x: float, y: float) -> bool:
    # 手柄（短而粗——16px 下"矮胖"比"细长"好认）
    if abs(y) <= 0.28 and -0.54 <= x <= 0.44:
        return True
    # 梅花环（带孔）
    if 0.20 <= math.hypot(x + 0.62, y) <= 0.46:
        return True
    # 开口钳：C 形，朝 +x 开缝（缺口 ±0.22）
    if 0.20 <= math.hypot(x - 0.66, y) <= 0.50:
        if not (x > 0.66 and abs(y) < 0.22):
            return True
    return False


def rasterize(n: int = BASE, ss: int = 6, scale: float = 1.8,
              angle_deg: float = -45.0) -> list[list[bool]]:
    """把扳手栅格化成 n×n 布尔掩码。

    · ss 倍超采样后按覆盖率 > 50% 取点 → 边缘是"硬"的像素台阶，不做抗锯齿
    · angle_deg 旋转：-45° → 钳口指向右上，梅花环落在左下
    · scale = 画布覆盖的模型范围半边长：越大 → 扳手越小（留白越多）
    """
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)
    mask = [[False] * n for _ in range(n)]
    thr = (ss * ss) / 2.0
    for py in range(n):
        for px in range(n):
            hit = 0
            for sy in range(ss):
                for sx in range(ss):
                    X = ((px + (sx + 0.5) / ss) / n) * 2.0 - 1.0
                    Y = ((py + (sy + 0.5) / ss) / n) * 2.0 - 1.0
                    # 屏幕坐标 → 扳手坐标（逆旋转；scale 放大可视范围）
                    x = (X * ca + Y * sa) * scale
                    y = (-X * sa + Y * ca) * scale
                    if in_wrench(x, y):
                        hit += 1
            mask[py][px] = hit >= thr
    return mask


def fits(mask: list[list[bool]], margin: int = 1) -> bool:
    """形状是否完整落在画布内（四周至少留 margin 像素）。"""
    n = len(mask)
    hit = False
    for y in range(n):
        for x in range(n):
            if not mask[y][x]:
                continue
            hit = True
            if x < margin or y < margin or x >= n - margin or y >= n - margin:
                return False
    return hit


def fit_scale(n: int = BASE, ss: int = 6, angle_deg: float = -45.0,
              margin: int = 1) -> float:
    """二分搜索"刚好塞进画布"的缩放系数 —— 让扳手尽可能大又不被裁切。

    注意语义：scale 在 rasterize 里是【乘数】，越大 → 可视范围越大 → 扳手越小。
    """
    lo, hi = 0.2, 6.0                     # lo=太大  hi=一定放得下
    for _ in range(24):
        mid = (lo + hi) / 2.0
        if fits(rasterize(n, ss, mid, angle_deg), margin):
            hi = mid
        else:
            lo = mid
    return hi


def mask_to_pixels(mask: list[list[bool]]) -> list[list[tuple]]:
    """掩码 → RGBA，并按行给最左/最右像素加重高光与暗面（做出金属感）。"""
    n = len(mask)
    img = [[CLEAR] * n for _ in range(n)]
    for y in range(n):
        cols = [x for x in range(n) if mask[y][x]]
        if not cols:
            continue
        lo, hi = cols[0], cols[-1]
        for x in cols:
            img[y][x] = BODY
        img[y][lo] = LIGHT
        if hi != lo:
            img[y][hi] = DARK
    return img


def scale_nearest(img: list[list[tuple]], k: int) -> list[list[tuple]]:
    """整数倍最近邻放大 —— 像素画必须这么放，插值就糊了。"""
    out = []
    for row in img:
        big = [px for px in row for _ in range(k)]
        for _ in range(k):
            out.append(list(big))
    return out


# ---------------------------------------------------------------------------
# PNG / ICO 编码（标准库手写）
# ---------------------------------------------------------------------------

def png_bytes(img: list[list[tuple]], w: int, h: int) -> bytes:
    raw = bytearray()
    for row in img:
        raw.append(0)                       # 过滤器类型 0 = None
        for px in row:
            raw += bytes(px)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)     # 8bit RGBA
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def dib_bytes(img: list[list[tuple]], size: int) -> bytes:
    """32bpp BGRA DIB（自下而上）+ 1bpp AND 掩码 —— ICO 里最兼容的帧格式。"""
    hdr = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32,
                      0, size * size * 4, 0, 0, 0, 0)
    px = bytearray()
    for y in range(size - 1, -1, -1):
        for x in range(size):
            r, g, b, a = img[y][x]
            px += bytes((b, g, r, a))
    mask_stride = ((size + 31) // 32) * 4
    mask = bytearray()
    for y in range(size - 1, -1, -1):
        bits = bytearray(mask_stride)
        for x in range(size):
            if img[y][x][3] == 0:           # 透明 → 掩码置 1
                bits[x // 8] |= 0x80 >> (x % 8)
        mask += bits
    return hdr + bytes(px) + bytes(mask)


def ico_bytes(frames: dict[int, list[list[tuple]]]) -> bytes:
    entries, payloads = [], []
    offset = 6 + 16 * len(frames)
    for size in sorted(frames):
        img = frames[size]
        data = png_bytes(img, size, size) if size >= 128 else dib_bytes(img, size)
        dim = 0 if size >= 256 else size    # 256 在目录里记作 0
        entries.append(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32,
                                   len(data), offset))
        payloads.append(data)
        offset += len(data)
    return (struct.pack("<HHH", 0, 1, len(frames))
            + b"".join(entries) + b"".join(payloads))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成像素扳手图标")
    ap.add_argument("--preview", action="store_true",
                    help="额外输出 assets/wrench_preview.png（放大预览，便于检查）")
    ap.add_argument("--scale", type=float, default=None,
                    help="手动指定缩放（越大扳手越小；默认自动贴合画布）")
    args = ap.parse_args(argv)

    scale = args.scale if args.scale else fit_scale(BASE)
    mask = rasterize(BASE, scale=scale)
    base = mask_to_pixels(mask)
    frames = {s: scale_nearest(base, s // BASE)
              for s in (16, 32, 48, 64, 128, 256)}

    ASSETS.mkdir(parents=True, exist_ok=True)
    ico_path = ASSETS / "wrench.ico"
    png_path = ASSETS / "wrench.png"
    ico_path.write_bytes(ico_bytes(frames))
    png_path.write_bytes(png_bytes(frames[256], 256, 256))

    print("点阵（# = 实体）：")
    for row in mask:
        print("   " + "".join("#" if v else "." for v in row))
    print(f"[OK] {ico_path.relative_to(ROOT)}  {ico_path.stat().st_size} 字节  "
          f"尺寸 {sorted(frames)}")
    print(f"[OK] {png_path.relative_to(ROOT)}  {png_path.stat().st_size} 字节  256×256")

    if args.preview:
        pp = ASSETS / "wrench_preview.png"
        pp.write_bytes(png_bytes(scale_nearest(base, 48), 768, 768))
        print(f"[OK] {pp.relative_to(ROOT)}  768×768（放大预览，仅供检查）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
