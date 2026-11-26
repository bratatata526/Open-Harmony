#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""告警通道 —— 控制台 / 文件 / Webhook / 邮件

============================================================================
四条通道，一条共同纪律：**send() 永不抛异常**
============================================================================
  返回值是 ChannelResult(ok, skipped, detail)，由上层统一记录。
  这样"邮箱密码错了"只是这一条告警标红，分析主链路照常跑。

============================================================================
为什么用标准库而不是 requests / 第三方 SMTP 库
============================================================================
  本项目 Python 侧刻意保持"零网关依赖"（requirements.txt 只有 pyserial
  和 openai）。告警通道用 urllib + smtplib 就够，少一个依赖就少一类
  "新同学克隆下来装不上"的事故 —— 而告警恰恰是最需要"拿来就能跑"的部分。
"""

from __future__ import annotations

import json
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.header import Header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path
from typing import List

from .alert import AlertEvent
from .config import AlertConfig


@dataclass
class ChannelResult:
    channel: str
    ok: bool = False
    skipped: bool = False
    detail: str = ""

    def to_dict(self) -> dict:
        return {"channel": self.channel, "ok": self.ok,
                "skipped": self.skipped, "detail": self.detail}

    def __str__(self) -> str:
        state = "OK" if self.ok else ("跳过" if self.skipped else "失败")
        return "%s:%s%s" % (self.channel, state,
                            ("(" + self.detail + ")") if self.detail else "")


class Channel:
    """通道基类。子类只实现 _send()，异常统一在这里兜住。"""

    kind = "base"

    def __init__(self, cfg: AlertConfig):
        self.cfg = cfg

    @property
    def name(self) -> str:
        return self.kind

    def send(self, ev: AlertEvent, report_md: str = "") -> ChannelResult:
        if self.cfg.dry_run and self.kind != "console":
            # 演练模式：报告"本来会发什么"，不产生任何副作用
            return ChannelResult(self.name, ok=True, skipped=True,
                                 detail="演练模式：未实际发送")
        try:
            return self._send(ev, report_md)
        except Exception as e:                                   # noqa: BLE001
            return ChannelResult(self.name, ok=False,
                                 detail="%s: %s" % (type(e).__name__, e))

    def _send(self, ev: AlertEvent, report_md: str) -> ChannelResult:
        raise NotImplementedError

    def reset_failures(self) -> None:
        """清掉"上次失败后的退避"。

        只有邮件通道需要（SMTP 失败会冷却 120 秒避免拖慢主链路），
        其余通道是空实现。手动自测和重新加载配置时必须调用 ——
        否则用户改完 .env 点"测试"会被上一次的冷却直接跳过，
        看起来像"配置改了也没用"。
        """


# ---------------------------------------------------------------------------
# 1) 控制台
# ---------------------------------------------------------------------------

class ConsoleChannel(Channel):
    kind = "console"

    def _send(self, ev: AlertEvent, report_md: str) -> ChannelResult:
        icon = {"P0": "🔴", "P1": "🟠", "P2": "🟡", "P3": "⚪"}.get(ev.severity, "⚪")
        print("\n" + "─" * 72)
        print("%s [告警 %s] %s" % (icon, ev.severity, ev.title))
        print("   %s" % ev.summary)
        print("   设备: %s ｜ 窗口: %s ｜ 指纹: %s ｜ 异常分: %.2f"
              % (ev.device, ev.window_id, ev.cluster_key, ev.score))
        if ev.report_file:
            print("   报告: %s" % ev.report_file)
        if ev.evidence:
            print("   证据: %s" % ev.evidence[0])
        print("─" * 72 + "\n")
        return ChannelResult(self.name, ok=True)


# ---------------------------------------------------------------------------
# 2) 文件（JSONL 追加）—— 看板 /api/alerts 的历史来源
# ---------------------------------------------------------------------------

class FileChannel(Channel):
    kind = "file"
    _lock = threading.Lock()

    def _resolve(self) -> Path:
        p = Path(self.cfg.file_path or "logs/alerts.jsonl")
        if not p.is_absolute():
            # 相对路径按"仓库根目录"解析，而不是当前工作目录 ——
            # 从 tools/ 或任意目录启动时都要写到同一个文件里
            root = Path(__file__).resolve().parents[2]
            p = root / p
        return p

    def _send(self, ev: AlertEvent, report_md: str) -> ChannelResult:
        p = self._resolve()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(ev.to_dict(), ensure_ascii=False)
            with self._lock, open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            return ChannelResult(self.name, ok=True, detail=str(p.name))
        except OSError as e:
            return ChannelResult(self.name, ok=False, detail="写文件失败: %s" % e)


# ---------------------------------------------------------------------------
# 3) Webhook
# ---------------------------------------------------------------------------

class WebhookChannel(Channel):
    kind = "webhook"

    # 四家机器人的字段名不一样，抄错一个键就是 HTTP 200 + errcode 非 0
    # 这种"看起来发了、其实没发"的假成功。所以按平台分别拼负载。
    _PLATFORMS = {
        "dingtalk": "钉钉",
        "feishu": "飞书",
        "wecom": "企业微信",
        "generic": "通用 JSON",
    }

    @staticmethod
    def _guess_kind(url: str) -> str:
        u = (url or "").lower()
        if "oapi.dingtalk.com" in u:
            return "dingtalk"
        if "open.feishu.cn" in u or "open.larksuite.com" in u:
            return "feishu"
        if "qyapi.weixin.qq.com" in u:
            return "wecom"
        return "generic"

    def _kind(self) -> str:
        forced = (self.cfg.webhook_kind or "auto").strip().lower()
        if forced in self._PLATFORMS and forced != "auto":
            return forced
        return self._guess_kind(self.cfg.webhook_url)

    def _text(self, ev: AlertEvent) -> str:
        lines = [
            "【%s】%s" % (ev.severity, ev.title),
            ev.summary,
            "设备：%s ｜ 窗口：%s ｜ 异常分：%.2f"
            % (ev.device or "—", ev.window_id or "—", float(ev.score)),
        ]
        if ev.span:
            lines.append("时间范围：%s" % ev.span)
        if ev.report_file:
            lines.append("报告：%s" % ev.report_file)
        if ev.source:
            lines.append("数据来源：%s" % ev.source)
        return "\n".join(lines)[:1800]

    def _payload(self, ev: AlertEvent) -> tuple:
        kind = self._kind()
        text = self._text(ev)
        if kind in ("dingtalk", "wecom"):
            # 钉钉/企业微信：text 必须是对象，不能是字符串
            return kind, {"msgtype": "text", "text": {"content": text}}
        if kind == "feishu":
            # 飞书：msg_type + content.text，与钉钉不是同一套键名
            return kind, {"msg_type": "text", "content": {"text": text}}
        payload = ev.to_dict()
        payload["text"] = text
        payload["msgtype"] = "text"
        return kind, payload

    @staticmethod
    def _business_ok(kind: str, code: int, raw: str) -> tuple:
        """HTTP 2xx 只代表"到达机器人网关"，不代表机器人接收了消息。

        钉钉/企业微信/飞书都会在 200 响应里塞 errcode/code：
        授权码过期、关键词不匹配、IP 白名单没加，全是 200 + 非 0。
        必须再看一层，否则界面显示"发送成功"、手机永远不响。
        """
        if not (200 <= code < 300):
            return False, "HTTP %s %s" % (code, raw[:120])
        if not raw.strip():
            return True, "HTTP %s" % code
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return True, "HTTP %s" % code
        if not isinstance(data, dict):
            return True, "HTTP %s" % code

        if kind in ("dingtalk", "wecom"):
            ec = data.get("errcode", 0)
            try:
                ec = int(ec)
            except (TypeError, ValueError):
                ec = 0
            if ec != 0:
                return False, "HTTP %s errcode=%s %s" % (
                    code, ec, str(data.get("errmsg", ""))[:100])
        elif kind == "feishu":
            ec = data.get("code", data.get("StatusCode", 0))
            try:
                ec = int(ec or 0)
            except (TypeError, ValueError):
                ec = 0
            if ec != 0:
                return False, "HTTP %s code=%s %s" % (
                    code, ec,
                    str(data.get("msg") or data.get("StatusMessage", ""))[:100])
        return True, "HTTP %s" % code

    def _send(self, ev: AlertEvent, report_md: str) -> ChannelResult:
        kind, payload = self._payload(ev)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            self.cfg.webhook_url, data=body, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8",
                     "User-Agent": "AgriLogAlert/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.webhook_timeout_s) as r:
                code = getattr(r, "status", 200)
                raw = ""
                try:
                    raw = r.read(1200).decode("utf-8", "replace").strip()
                except Exception:                                # noqa: BLE001
                    pass
                ok, detail = self._business_ok(kind, code, raw)
                return ChannelResult(
                    self.name, ok=ok,
                    detail=("%s %s" % (self._PLATFORMS.get(kind, kind), detail))[:200])
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read(400).decode("utf-8", "replace").strip()
            except Exception:                                    # noqa: BLE001
                pass
            return ChannelResult(self.name, ok=False,
                                 detail=("HTTP %s %s" % (e.code, detail))[:200])
        except urllib.error.URLError as e:
            return ChannelResult(self.name, ok=False, detail="网络不可达: %s" % e.reason)
        except Exception as e:                                   # noqa: BLE001
            return ChannelResult(self.name, ok=False,
                                 detail="%s: %s" % (type(e).__name__, e))


# ---------------------------------------------------------------------------
# 4) 邮件（SMTP）
# ---------------------------------------------------------------------------

class EmailChannel(Channel):
    kind = "email"

    # ★ 失败退避：SMTP 配错（授权码过期、端口被封）时，每次告警都要等
    #   两次连接超时（默认 15s × 2 = 30s）。告警是串行发的，这会把
    #   "生成下一份报告"一起拖慢。所以失败后冷却一段时间再试，
    #   期间跳过邮件、只走其它通道 —— 修好配置后最多等这么久就自动恢复。
    FAIL_BACKOFF_S = 120.0
    _fail_until = 0.0
    _last_error = ""

    @classmethod
    def reset_failures(cls) -> None:
        """清掉失败退避：手动自测 / 重载配置后必须真的再连一次。

        退避本身是为自动告警设计的（避免每次异常都等 30 秒超时），
        但用户点"测试邮箱通道"是显式动作，被退避跳过会让人以为配置没生效。
        """
        cls._fail_until = 0.0
        cls._last_error = ""

    def _send(self, ev: AlertEvent, report_md: str) -> ChannelResult:
        problem = self.cfg.email_problem()
        if problem:
            return ChannelResult(self.name, ok=False, skipped=True, detail=problem)

        now = time.time()
        if now < self._fail_until:
            return ChannelResult(
                self.name, ok=False, skipped=True,
                detail="上次失败后冷却中（%.0f 秒后重试）｜上次原因：%s"
                       % (self._fail_until - now, self._last_error[:120]))

        msg = self._build_message(ev, report_md)

        # 465(SSL) 与 587(STARTTLS) 两条路都试一遍：
        # 用户只改了 SMTP_HOST 却忘了改端口/加密方式，是最高频的配置错误，
        # 与其让他对着 "ConnectionRefused" 猜，不如自动兜一次。
        plan = self._connection_plan()
        errors: List[str] = []
        for host, port, security in plan:
            try:
                self._deliver(host, port, security, msg)
                EmailChannel._fail_until = 0.0
                EmailChannel._last_error = ""
                return ChannelResult(
                    self.name, ok=True,
                    detail="%s:%d(%s) → %s"
                           % (host, port, security, ",".join(self.cfg.email_to)))
            except Exception as e:                               # noqa: BLE001
                # ★ 两次尝试的错误【都要留下】：只报最后一次会让用户
                #   拿着 "587/starttls 失败" 去怀疑自己配的 465，反而更糊涂。
                errors.append("%s:%d(%s) %s: %s"
                              % (host, port, security, type(e).__name__, e))
                continue
        detail = " ｜ ".join(errors) or "未尝试任何连接"
        EmailChannel._fail_until = time.time() + self.FAIL_BACKOFF_S
        EmailChannel._last_error = detail
        return ChannelResult(self.name, ok=False, detail=detail)

    # ---------------- 内部 ----------------
    def _connection_plan(self) -> List[tuple]:
        sec = (self.cfg.smtp_security or "ssl").lower()
        host, port = self.cfg.smtp_host, int(self.cfg.smtp_port or 465)
        plan = [(host, port, sec)]
        if sec == "ssl" and port == 465:
            plan.append((host, 587, "starttls"))
        elif sec == "starttls" and port == 587:
            plan.append((host, 465, "ssl"))
        return plan

    def _build_message(self, ev: AlertEvent, report_md: str):
        subject = "[%s] %s" % (ev.severity, ev.title)
        # 邮件主题必须显式按 utf-8 编码，否则中文主题会变成乱码或直接报错
        msg = MIMEMultipart()
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = self.cfg.email_from or self.cfg.smtp_user
        msg["To"] = ", ".join(self.cfg.email_to)
        msg["X-AgriLog-Alert-Id"] = ev.alert_id

        msg.attach(MIMEText(ev.to_email_text(), "plain", "utf-8"))

        # 报告正文（Markdown）作为附件：邮件正文保持简短可读，
        # 需要细节的人点开附件即可，不用在手机小屏上翻长报告。
        if report_md.strip():
            part = MIMEBase("text", "markdown")
            part.set_payload(report_md.encode("utf-8"))
            encoders.encode_base64(part)
            fname = ev.report_file or ("%s.md" % ev.window_id)
            # RFC 2231 编码，中文文件名才不会在某些客户端变成 attach.bin
            part.add_header("Content-Disposition", "attachment",
                            filename=("utf-8", "", fname))
            msg.attach(part)
        return msg

    def _deliver(self, host: str, port: int, security: str, msg) -> None:
        timeout = self.cfg.email_timeout_s
        if security == "ssl":
            ctx = ssl.create_default_context()
            server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
        try:
            server.ehlo()
            if security == "starttls":
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            if self.cfg.smtp_user:
                server.login(self.cfg.smtp_user, self.cfg.smtp_password)
            server.sendmail(self.cfg.email_from or self.cfg.smtp_user,
                            self.cfg.email_to, msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:                                    # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------

def build_channels(cfg: AlertConfig) -> List[Channel]:
    """按配置组装实际启用的通道。未配置的通道【直接不出现】，而不是出现一个失败项。

    这一点很重要：看板要显示"告警已就绪"，不能因为"没配 Webhook"就把状态
    标成异常 —— 零配置下控制台+文件本来就该是健康状态。
    """
    out: List[Channel] = []
    if cfg.console:
        out.append(ConsoleChannel(cfg))
    if cfg.file_enabled:
        out.append(FileChannel(cfg))
    if cfg.webhook_ready():
        out.append(WebhookChannel(cfg))
    if cfg.email_ready():
        out.append(EmailChannel(cfg))
    return out
