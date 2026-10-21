#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenHarmony 日志分析工具 —— 图形启动器

做四件事，别的一概不管（分析能力全在现有 web 看板里，不重复实现）：
    ① 选串口   ② 选开发板（只读，目前只有 Hi3861）
    ③ 开始     —— 后台起看板服务 → 就绪后自动打开浏览器
    ④ 停止     —— 请求服务端优雅停止（先释放串口）→ 关窗口

设计要点（都是踩过的坑换来的，别改）
  1. 服务端是【子进程】，采集器是服务端的【孙进程】。
     停止必须让服务端自己收尾（调 /api/shutdown → 它内部 stop_live() 杀掉采集器）。
     直接 taskkill 父进程会留孤儿进程继续占串口，现象是：
     看板关了，DevEco 还是打不开串口 / 报 upload port is occupied。
  2. 关窗口 = 停止监听并释放串口（项目约定），不留后台服务。
  3. 用 pythonw.exe 启动本程序 → 无黑窗；服务端子进程再加 CREATE_NO_WINDOW。
  4. UI 线程绝不做阻塞 IO：HTTP 轮询放工作线程，结果用 after() 回主线程。
  5. 单实例：8080 已有本项目在跑 → 不重复启动，直接给"打开看板"。

用法（正常双击桌面快捷方式即可，不用手敲）
    .venv\\Scripts\\pythonw.exe tools\\launcher.py            # 无控制台
    .venv\\Scripts\\python.exe  tools\\launcher.py --debug    # 带控制台日志，排查用
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
LOGS = ROOT / "logs"
LOG_FILE = LOGS / "launcher.log"
SERVER = ROOT / "tools" / "dashboard_server.py"

HOST, PORT = "127.0.0.1", 8080
BASE_URL = "http://%s:%d" % (HOST, PORT)
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 开发板（只读下拉：目前只有 Hi3861；将来加板子只改这张表）
BOARDS = [
    {"id": "hi3861", "name": "Hi3861 / HiSpark Pegasus", "baud": 115200},
]
AUTO_PORT = "自动发现（推荐）"

# 配色取自 web 看板主题，保证两端视觉一致
C_BG, C_PANEL, C_LINE = "#0a0e10", "#111a1d", "#22322e"
C_INK, C_DIM, C_FAINT = "#cfe8dd", "#7d9a92", "#4a5f59"
C_GREEN, C_AMBER, C_RED = "#3ce68b", "#ffb02e", "#ff4d4d"
C_BTN = "#16281f"


# ===========================================================================
# 核心逻辑（不依赖 tkinter，便于无界面测试）
# ===========================================================================

class Core:
    def __init__(self, log=None):
        self.proc: subprocess.Popen | None = None
        self.tail: deque[str] = deque(maxlen=60)     # 服务端输出尾部，出错时给人看
        self._log = log or (lambda s: None)

    # ---- 串口枚举 ----
    @staticmethod
    def list_ports() -> list[tuple[str, str]]:
        """返回 [(COM3, 'USB-SERIAL CH340 (COM3)'), ...]；pyserial 缺失时返回空列表。"""
        try:
            from serial.tools import list_ports
        except Exception:
            return []
        out = []
        for p in list_ports.comports():
            dev = p.device
            desc = (p.description or "").strip()
            if desc and desc != dev:
                out.append((dev, "%s · %s" % (dev, desc)))
            else:
                out.append((dev, dev))
        return out

    # ---- HTTP ----
    @staticmethod
    def _get(path: str, timeout: float = 1.5) -> dict:
        with urllib.request.urlopen(BASE_URL + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def alive(self) -> bool:
        """看板服务是否在跑（且有响应）。"""
        try:
            self._get("/api/meta", 1.0)
            return True
        except Exception:
            return False

    def status(self) -> dict:
        try:
            return self._get("/api/live/status", 1.5)
        except Exception:
            return {}

    # ---- 启停 ----
    def start(self, com: str = "", baud: int = 115200) -> None:
        cmd = [sys.executable, "-u", str(SERVER),
               "--host", HOST, "--port", str(PORT), "--live", "--baud", str(baud)]
        if com:
            cmd += ["--com", com]
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        self._log("start: %s" % " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=env,
            creationflags=CREATE_NO_WINDOW)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        p = self.proc
        if p is None or p.stdout is None:
            return
        for line in p.stdout:
            s = line.rstrip()
            if not s:
                continue
            self.tail.append(s)
            self._log("[server] " + s)

    def wait_ready(self, timeout: float = 25.0) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.proc is not None and self.proc.poll() is not None:
                return False                      # 进程已退出：8080 被占 / 启动失败
            if self.alive():
                return True
            time.sleep(0.25)
        return False

    def stop(self, timeout: float = 8.0) -> bool:
        """优雅停止：/api/shutdown（服务端先释放串口）→ 等退出 → 兜底强杀整棵进程树。"""
        p = self.proc
        if p is None:
            return True
        if p.poll() is None:
            try:
                self._get("/api/shutdown", 3.0)
            except Exception:
                pass                                   # 服务没响应就往下走兜底
        t0 = time.time()
        while time.time() - t0 < timeout:
            if p.poll() is not None:
                self._log("stop: 服务已退出，串口已释放")
                self.proc = None
                return True
            time.sleep(0.2)

        self._log("stop: 优雅停止超时，改用 taskkill /T /F")
        try:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                           capture_output=True, creationflags=CREATE_NO_WINDOW)
        except Exception as e:                                  # noqa: BLE001
            self._log("stop: taskkill 失败 %s" % e)
        try:
            p.wait(timeout=3)
        except Exception:
            pass
        self.proc = None
        return True


# ===========================================================================
# 界面
# ===========================================================================

def _run_gui(debug: bool) -> int:
    import tkinter as tk
    from tkinter import ttk, messagebox

    # 高 DPI 下文字不糊
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.core = Core(log=self.log)
            self.running = False          # 服务是否由本程序启动且仍在跑
            self.busy = False             # 正在启动/停止，屏蔽重复点击
            self.auto_open_done = False
            # ★ tkinter 不是线程安全的：工作线程【不能】直接调 self.after()，
            #   否则会抛 "RuntimeError: main thread is not in main loop"（实测踩到）。
            #   统一走队列 + 主线程轮询转交（见 _post / _pump_ui）。
            self._ui_q: queue.Queue = queue.Queue()

            self.title("OpenHarmony 日志分析工具")
            self.configure(bg=C_BG)
            self.resizable(False, False)
            self._center(560, 452)
            try:
                self.iconbitmap(default=str(ASSETS / "wrench.ico"))
            except Exception:
                pass

            self.protocol("WM_DELETE_WINDOW", self.on_close)
            self._build()
            self.refresh_ports()
            self.set_state("idle")
            self._closing = False
            self.after(50, self._pump_ui)

        # ---------- 线程 → 主线程 的唯一通道 ----------
        def _post(self, fn) -> None:
            """工作线程用这个提交回调；由主线程 _pump_ui 执行。"""
            self._ui_q.put(fn)

        def _pump_ui(self) -> None:
            while True:
                try:
                    fn = self._ui_q.get_nowait()
                except queue.Empty:
                    break
                try:
                    fn()
                except Exception:                                # noqa: BLE001
                    pass
            if not self._closing:
                self.after(50, self._pump_ui)

        # ---------- 布局 ----------
        def _center(self, w: int, h: int) -> None:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            self.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2, (sh - h) // 3))

        def _label(self, parent, text, **kw):
            kw.setdefault("bg", C_BG)
            kw.setdefault("fg", C_INK)
            kw.setdefault("font", ("Microsoft YaHei UI", 10))
            return tk.Label(parent, text=text, **kw)

        def _build(self) -> None:
            pad = 22
            # ---- 标题 ----
            head = tk.Frame(self, bg=C_BG)
            head.pack(fill="x", padx=pad, pady=(18, 10))
            self._label(head, "日志分析工具", bg=C_BG, fg=C_INK,
                        font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
            self._label(head, "OpenHarmony · Hi3861 串口日志采集与分析",
                        bg=C_BG, fg=C_FAINT,
                        font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(2, 0))

            # ---- 表单卡片 ----
            card = tk.Frame(self, bg=C_PANEL, highlightbackground=C_LINE,
                            highlightthickness=1)
            card.pack(fill="x", padx=pad)
            inner = tk.Frame(card, bg=C_PANEL)
            inner.pack(fill="x", padx=16, pady=14)
            inner.columnconfigure(1, weight=1)

            style = ttk.Style(self)
            try:
                style.theme_use("clam")
            except Exception:
                pass
            style.configure("TCombobox", fieldbackground=C_PANEL, background=C_PANEL,
                            foreground=C_INK, arrowcolor=C_GREEN,
                            bordercolor=C_LINE, lightcolor=C_LINE, darkcolor=C_LINE,
                            selectbackground=C_PANEL, selectforeground=C_INK)
            style.map("TCombobox", fieldbackground=[("readonly", C_PANEL)],
                      foreground=[("readonly", C_INK)])

            # 串口
            self._label(inner, "端口", bg=C_PANEL, fg=C_DIM).grid(
                row=0, column=0, sticky="w", pady=6, padx=(0, 12))
            self.port_var = tk.StringVar()
            self.port_box = ttk.Combobox(inner, textvariable=self.port_var,
                                         state="readonly", width=26)
            self.port_box.grid(row=0, column=1, sticky="ew", pady=6)
            self.btn_refresh = tk.Button(
                inner, text="刷新", command=self.refresh_ports,
                bg=C_PANEL, fg=C_GREEN, activebackground=C_BTN, activeforeground=C_GREEN,
                relief="flat", bd=0, font=("Microsoft YaHei UI", 9), cursor="hand2",
                padx=10, pady=2)
            self.btn_refresh.grid(row=0, column=2, padx=(8, 0))

            # 开发板（只读）
            self._label(inner, "开发板", bg=C_PANEL, fg=C_DIM).grid(
                row=1, column=0, sticky="w", pady=6, padx=(0, 12))
            self.board_var = tk.StringVar(value=BOARDS[0]["name"])
            board_box = ttk.Combobox(inner, textvariable=self.board_var,
                                     state="readonly", width=26,
                                     values=[b["name"] for b in BOARDS])
            board_box.grid(row=1, column=1, sticky="ew", pady=6)
            self._label(inner, "当前仅支持", bg=C_PANEL, fg=C_FAINT,
                        font=("Microsoft YaHei UI", 9)).grid(row=1, column=2, padx=(8, 0))

            # ---- 状态条 ----
            stat = tk.Frame(self, bg=C_BG)
            stat.pack(fill="x", padx=pad, pady=(16, 0))
            self.dot = tk.Label(stat, text="●", bg=C_BG, fg=C_FAINT,
                                font=("Microsoft YaHei UI", 11))
            self.dot.pack(side="left")
            self.state_lbl = tk.Label(stat, text="未启动", bg=C_BG, fg=C_INK,
                                      font=("Microsoft YaHei UI", 10, "bold"))
            self.state_lbl.pack(side="left", padx=(6, 0))
            self.detail_lbl = tk.Label(stat, text="", bg=C_BG, fg=C_DIM,
                                       font=("Microsoft YaHei UI", 9), anchor="w",
                                       justify="left", wraplength=470)
            self.detail_lbl.pack(side="left", padx=(12, 0))

            # ---- 按钮 ----
            btns = tk.Frame(self, bg=C_BG)
            btns.pack(fill="x", padx=pad, pady=(16, 0))
            self.btn_start = tk.Button(
                btns, text="开 始", command=self.on_start,
                bg=C_GREEN, fg="#062f1c", activebackground="#31c877",
                activeforeground="#062f1c", relief="flat", bd=0,
                font=("Microsoft YaHei UI", 11, "bold"), cursor="hand2",
                width=14, pady=7)
            self.btn_start.pack(side="left")
            self.btn_stop = tk.Button(
                btns, text="停 止", command=self.on_stop,
                bg=C_BTN, fg=C_DIM, activebackground="#1d3329",
                activeforeground=C_INK, relief="flat", bd=0,
                font=("Microsoft YaHei UI", 11, "bold"), cursor="hand2",
                width=14, pady=7, state="disabled")
            self.btn_stop.pack(side="left", padx=(10, 0))
            self.btn_open = tk.Button(
                btns, text="打开看板", command=self.open_dashboard,
                bg=C_BG, fg=C_GREEN, activebackground=C_BTN, activeforeground=C_GREEN,
                relief="solid", bd=1, highlightthickness=0,
                font=("Microsoft YaHei UI", 9), cursor="hand2", padx=12, pady=6,
                state="disabled")
            self.btn_open.pack(side="right")

            # ---- 底注 ----
            self._label(self, "运行期间独占串口：此时 DevEco 无法 Upload / 烧录 / 开 Monitor。"
                             "关闭本窗口将停止服务并释放串口。",
                        bg=C_BG, fg=C_FAINT,
                        font=("Microsoft YaHei UI", 8), wraplength=500,
                        justify="left").pack(anchor="w", padx=pad, pady=(14, 0))

        # ---------- 小工具 ----------
        def log(self, msg: str) -> None:
            line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
            try:
                LOGS.mkdir(parents=True, exist_ok=True)
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
            if debug:
                print(line, flush=True)

        def set_state(self, state: str, detail: str = "") -> None:
            colors = {"idle": C_FAINT, "starting": C_AMBER, "running": C_GREEN,
                      "stopping": C_AMBER, "error": C_RED}
            texts = {"idle": "未启动", "starting": "正在启动…", "running": "运行中",
                     "stopping": "正在停止…", "error": "启动失败"}
            self.dot.configure(fg=colors.get(state, C_FAINT))
            self.state_lbl.configure(text=texts.get(state, state),
                                     fg=C_RED if state == "error" else C_INK)
            self.detail_lbl.configure(text=detail,
                                      fg=C_RED if state == "error" else C_DIM)

        def _buttons(self, start: bool, stop: bool, open_: bool) -> None:
            self.btn_start.configure(state="normal" if start else "disabled")
            self.btn_stop.configure(state="normal" if stop else "disabled",
                                    bg=C_BTN if stop else C_PANEL,
                                    fg=C_INK if stop else C_FAINT,
                                    cursor="hand2" if stop else "arrow")
            self.btn_open.configure(state="normal" if open_ else "disabled",
                                    fg=C_GREEN if open_ else C_FAINT,
                                    cursor="hand2" if open_ else "arrow")

        # ---------- 动作 ----------
        def refresh_ports(self) -> None:
            ports = Core.list_ports()
            vals = [AUTO_PORT] + ["%s · %s" % (d, desc.split(" · ", 1)[1])
                                  if " · " in desc else d for d, desc in ports]
            self._port_map = {AUTO_PORT: ""}
            for (dev, desc), label in zip(ports, vals[1:]):
                self._port_map[label] = dev
            self.port_box.configure(values=vals)
            keep = self.port_var.get()
            self.port_var.set(keep if keep in vals else AUTO_PORT)

        def on_start(self) -> None:
            if self.busy or self.running:
                return
            if self.core.alive():
                if messagebox.askyesno("已有实例", "检测到看板已在运行。\n\n是否直接打开？"):
                    self.open_dashboard()
                return
            self.busy = True
            self._buttons(False, False, False)
            self.set_state("starting", "正在启动看板服务…")
            com = self._port_map.get(self.port_var.get(), "")
            baud = BOARDS[0]["baud"]
            threading.Thread(target=self._do_start, args=(com, baud), daemon=True).start()

        def _do_start(self, com: str, baud: int) -> None:
            try:
                self.core.start(com, baud)
                ok = self.core.wait_ready()
            except Exception as e:                                   # noqa: BLE001
                self._post(lambda: self._fail("启动异常：%s" % e))
                return
            if not ok:
                tail = "\n".join(list(self.core.tail)[-6:])
                self._post(lambda: self._fail(
                    "服务未能启动（8080 可能已被占用）。\n" + tail))
                return
            self._post(self._started)

        def _started(self) -> None:
            self.running = True
            self.busy = False
            self._buttons(False, True, True)
            self.set_state("running", "服务已就绪，正在打开看板…")
            self.open_dashboard()
            self.after(400, self._tick)

        def _fail(self, msg: str) -> None:
            self.busy = False
            self.running = False
            self._buttons(True, False, False)
            self.set_state("error", msg)
            self.log("ERROR " + msg.replace("\n", " | "))
            self.core.stop()

        def on_stop(self) -> None:
            if self.busy or not self.running:
                return
            self.busy = True
            self._buttons(False, False, False)
            self.set_state("stopping", "正在停止并释放串口…")
            threading.Thread(target=self._do_stop, daemon=True).start()

        def _do_stop(self) -> None:
            self.core.stop()
            self._post(self._stopped)

        def _stopped(self) -> None:
            self.running = False
            self.busy = False
            self._buttons(True, False, False)
            self.set_state("idle", "已停止，串口已释放。")

        def open_dashboard(self) -> None:
            try:
                webbrowser.open(BASE_URL)
            except Exception as e:                                   # noqa: BLE001
                self.set_state("error", "打开浏览器失败：%s" % e)

        # ---------- 轮询 ----------
        def _tick(self) -> None:
            if not self.running:
                return
            threading.Thread(target=self._poll_once, daemon=True).start()
            self.after(1000, self._tick)

        def _poll_once(self) -> None:
            st = self.core.status()
            self._post(lambda: self._render(st))

        def _render(self, st: dict) -> None:
            if not self.running:
                return
            if not st:
                self.set_state("error", "看板无响应（服务可能已退出）")
                self.running = False
                self._buttons(True, False, False)
                return
            src = st.get("source") or "—"
            lines = st.get("lines", 0)
            if st.get("online"):
                self.set_state("running", "数据源 %s ｜ 已接收 %s 行" % (src, lines))
            else:
                err = st.get("error") or "等待数据…"
                self.set_state("starting", "数据源 %s ｜ 已接收 %s 行 ｜ %s"
                               % (src, lines, err))

        # ---------- 关闭 ----------
        def on_close(self) -> None:
            if self.busy:
                return
            if self.running:
                self.busy = True
                self._buttons(False, False, False)
                self.set_state("stopping", "正在停止服务并释放串口…")
                threading.Thread(target=self._do_stop_and_quit, daemon=True).start()
            else:
                self._closing = True
                self.destroy()

        def _do_stop_and_quit(self) -> None:
            self.core.stop()
            self._post(self._quit)

        def _quit(self) -> None:
            self._closing = True
            self.destroy()

    App().mainloop()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OpenHarmony 日志分析工具 图形启动器")
    ap.add_argument("--debug", action="store_true", help="在控制台打印日志（排查用）")
    args = ap.parse_args(argv)

    if not SERVER.exists():
        print("找不到 %s，请在仓库根目录下运行。" % SERVER, file=sys.stderr)
        return 2
    try:
        return _run_gui(args.debug)
    except Exception:                                            # noqa: BLE001
        # pythonw 下没有控制台，异常写文件 + 弹窗，否则用户只会看到"什么都没发生"
        import traceback
        tb = traceback.format_exc()
        try:
            LOGS.mkdir(parents=True, exist_ok=True)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write("\n[启动器崩溃]\n" + tb + "\n")
        except OSError:
            pass
        try:
            import tkinter.messagebox as mb
            mb.showerror("启动器错误", tb[-1500:])
        except Exception:
            print(tb, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
