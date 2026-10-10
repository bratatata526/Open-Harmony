# -*- coding: utf-8 -*-
"""
提交前安全扫描：逐个读取【已暂存文件】的真实内容，查找密钥。

为什么不用 `git diff --cached | Select-String`：
  本机沙箱禁止程序创建命名管道，git 调用 sh.exe 做文本转换时会失败：
      fatal: unable to read files to diff
      sh.exe: *** fatal error - couldn't create signal pipe, Win32 error 5
  此时 git diff 输出为空 —— 搜不到密钥不是因为"没有"，而是因为"没搜"。
  这种假阴性正是密钥泄露的典型方式，所以改成直接读文件内容。

用法：
    python tools/secret_scan.py             # 扫描已暂存文件（提交前跑）
    python tools/secret_scan.py --tracked   # 扫描所有已跟踪文件（push 前跑）
    python tools/secret_scan.py --all       # 扫描整个工作区（含未跟踪，排查用）
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 密钥特征：DeepSeek 的 sk- 开头；以及通用高危字段
PATTERNS = [
    ("DeepSeek/OpenAI 风格密钥", re.compile(r"sk-[A-Za-z0-9_\-]{20,}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("AWS Access Key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("私钥文件头", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("通用赋值式密钥", re.compile(
        r"(?i)(api[_-]?key|apikey|secret|passwd|password|access[_-]?token)"
        r"\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{20,}")),
]

# 这些是模板/示例文件，命中的是占位符，不算泄露
ALLOWLIST_FILES = {".env.example", "config.example.yaml"}

# 占位符特征
PLACEHOLDER = re.compile(r"(在这里|REPLACE|YOUR_|xxxx|XXXX|示例|placeholder|<.*>)")


def git_paths(extra):
    """取 git 路径清单。

    ★ 必须用 -z（NUL 分隔、不做引号转义）。
      默认输出会把含非 ASCII 的路径转义成
          "reports/W0006_\346\211\247\350\241\214\345\231\250..."
      带着双引号和八进制转义，拿去 open() 会直接 OSError(WinError 123)。
      更糟的是这类文件会被【静默跳过】，扫描结果就成了假的"全部通过"。
    """
    out = subprocess.run(["git"] + extra + ["-z"], cwd=str(ROOT), capture_output=True)
    if out.returncode != 0:
        print("✘ git 取清单失败：%s" % out.stderr.decode("utf-8", "replace").strip())
        sys.exit(2)
    raw = out.stdout.decode("utf-8", "replace")
    return [p for p in raw.split("\0") if p.strip()]


def staged_files():
    return git_paths(["diff", "--cached", "--name-only"])


def tracked_files():
    return git_paths(["ls-files"])


def scan_file(path, text):
    hits = []
    for name, pat in PATTERNS:
        for m in pat.finditer(text):
            frag = m.group(0)
            if PLACEHOLDER.search(frag):
                continue
            hits.append((name, frag[:60]))
    return hits


def main():
    args = sys.argv[1:]
    if "--all" in args:
        mode = "整个工作区"
        files = []
        for p in ROOT.rglob("*"):
            if not p.is_file():
                continue
            if any(part in (".git", ".venv", "Tool", "harmony_sdk",
                            "hi3861_application", "gitsh", "__pycache__")
                   for part in p.parts):
                continue
            files.append(str(p.relative_to(ROOT)).replace("\\", "/"))
    elif "--tracked" in args:
        mode = "已跟踪文件（即将被 push 的内容）"
        files = tracked_files()
    else:
        mode = "已暂存文件（即将被 commit 的内容）"
        files = staged_files()
        if not files:
            print("暂存区是空的 —— 如果已经提交完、想在 push 前复查，请用：")
            print("    python tools/secret_scan.py --tracked\n")

    print("扫描对象：%s（%d 个文件）\n" % (mode, len(files)))

    bad = 0
    for rel in files:
        p = ROOT / rel
        if not p.exists() or p.is_dir():
            continue
        if rel.endswith((".png", ".jpg", ".jpeg", ".gif", ".pdf", ".bin",
                         ".exe", ".zip", ".log")) and p.stat().st_size > 2_000_000:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hits = scan_file(p, text)
        if not hits:
            continue
        if p.name in ALLOWLIST_FILES:
            print("  [模板] %s" % rel)
            for name, frag in hits:
                print("         命中 %s（占位符，允许）：%s" % (name, frag))
            continue
        bad += 1
        print("  ✘✘ %s" % rel)
        for name, frag in hits:
            print("         %s: %s" % (name, frag))

    print()
    if bad:
        print("✘ 发现 %d 个文件含疑似真实密钥 —— 请处理后再提交！" % bad)
        return 1
    print("✔ 未发现任何真实密钥")
    return 0


if __name__ == "__main__":
    sys.exit(main())
