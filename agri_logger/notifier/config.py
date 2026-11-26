#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""告警配置 —— 全部来自环境变量（`.env` 由 run.ps1 / 启动器注入）

============================================================================
为什么用环境变量而不是 config.yaml
============================================================================
  · 邮箱授权码是密钥，绝不能进仓库。`.env` 已在 .gitignore 里，天然合规。
  · run.ps1 会把 .env 里每一行 KEY=VALUE 放进环境变量再传子进程，
    所以只要在 .env 里加几行，看板/启动器/命令行三种入口【同时】生效，
    不需要改三个地方。
  · 没有配置就是"通道关闭"，不会报错。零配置也能跑（控制台 + 文件）。

============================================================================
一条硬纪律：配置错误永远不能让主流程失败
============================================================================
  告警是"锦上添花"，采集与分析才是主链路。邮箱写错了、SMTP 连不上，
  都只能记一条 skipped/enabled=False，绝不能抛异常把分析循环带崩。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

TRUTHY = {"1", "true", "yes", "on", "y", "t"}


def _env_str(env: Dict[str, str], key: str, default: str = "") -> str:
    return (env.get(key) or default).strip()


def _env_bool(env: Dict[str, str], key: str, default: bool = False) -> bool:
    v = (env.get(key) or "").strip().lower()
    if not v:
        return default
    return v in TRUTHY


def _env_int(env: Dict[str, str], key: str, default: int) -> int:
    try:
        return int((env.get(key) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def _env_float(env: Dict[str, str], key: str, default: float) -> float:
    try:
        return float((env.get(key) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def _env_list(env: Dict[str, str], key: str) -> List[str]:
    raw = (env.get(key) or "").strip()
    if not raw:
        return []
    # 逗号/分号/空白都能分隔，运维手写时不至于因为多一个空格就发不出去
    for sep in (";", "，", " "):
        raw = raw.replace(sep, ",")
    return [x.strip() for x in raw.split(",") if x.strip()]


def read_env_file(path: Optional[Path]) -> Dict[str, str]:
    """读 .env（KEY=VALUE）。仅用于补齐环境变量里【缺失】的键。

    为什么要留这个后门：命令行直接跑 `python -m agri_logger.notifier` 时
    没有 run.ps1 帮忙注入，否则用户改完 .env 还要手动 $env:XXX=... 一遍，
    最容易出错的地方就在这里。
    """
    out: Dict[str, str] = {}
    if path is None or not Path(path).exists():
        return out
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        t = line.strip()
        if not t or t.startswith("#") or t.startswith("//"):
            continue
        i = t.find("=")
        if i < 1:
            continue
        k = t[:i].strip()
        v = t[i + 1:].strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


@dataclass
class AlertConfig:
    """告警通道配置。每一条都能用环境变量覆盖。"""

    enabled: bool = True
    # 低于这个级别不推送；P0 最严重
    min_severity: str = "P2"
    dry_run: bool = False          # True = 只打印将要发什么，不真的发

    # ---- 通道 1：控制台（永远可用，零配置）----
    console: bool = True

    # ---- 通道 2：文件（JSONL 追加，零配置；看板 /api/alerts 从这里读历史）----
    file_enabled: bool = True
    file_path: str = "logs/alerts.jsonl"

    # ---- 通道 3：Webhook（钉钉/飞书/企业微信/自建 HTTP）----
    webhook_url: str = ""
    webhook_timeout_s: float = 8.0
    # auto = 按 URL 自动识别；也可强制 dingtalk / feishu / wecom / generic
    webhook_kind: str = "auto"

    # ---- 通道 4：邮件（SMTP）----
    email_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_security: str = "ssl"     # ssl | starttls | none
    smtp_user: str = ""
    smtp_password: str = ""        # ★ QQ/163 是"授权码"，不是登录密码
    email_from: str = ""
    email_to: List[str] = field(default_factory=list)
    email_timeout_s: float = 15.0

    # ---- 节流（防止故障期间刷屏把邮箱打爆）----
    cooldown_s: float = 180.0      # 同一聚合指纹的冷却时间
    max_per_hour: int = 12         # 每小时最多推送几条

    # ---------------- 构造 ----------------
    @classmethod
    def from_env(cls, env_file: Optional[Path] = None,
                 environ: Optional[Dict[str, str]] = None) -> "AlertConfig":
        env = dict(os.environ if environ is None else environ)
        # 只补缺失键：run.ps1 已经注入的优先，.env 是兜底
        for k, v in read_env_file(env_file).items():
            env.setdefault(k, v)

        cfg = cls(
            enabled=_env_bool(env, "ALERT_ENABLED", True),
            min_severity=(_env_str(env, "ALERT_MIN_SEVERITY", "P2") or "P2").upper(),
            dry_run=_env_bool(env, "ALERT_DRY_RUN", False),
            console=_env_bool(env, "ALERT_CONSOLE", True),
            file_enabled=_env_bool(env, "ALERT_FILE_ENABLED", True),
            file_path=_env_str(env, "ALERT_FILE", "logs/alerts.jsonl"),
            webhook_url=_env_str(env, "ALERT_WEBHOOK_URL", ""),
            webhook_timeout_s=_env_float(env, "ALERT_WEBHOOK_TIMEOUT_S", 8.0),
            webhook_kind=(_env_str(env, "ALERT_WEBHOOK_KIND", "auto") or "auto").lower(),
            email_enabled=_env_bool(env, "ALERT_EMAIL_ENABLED", False),
            smtp_host=_env_str(env, "SMTP_HOST", ""),
            smtp_port=_env_int(env, "SMTP_PORT", 465),
            smtp_security=(_env_str(env, "SMTP_SECURITY", "ssl") or "ssl").lower(),
            smtp_user=_env_str(env, "SMTP_USER", ""),
            smtp_password=_env_str(env, "SMTP_PASSWORD", ""),
            email_from=_env_str(env, "ALERT_EMAIL_FROM", ""),
            email_to=_env_list(env, "ALERT_EMAIL_TO"),
            email_timeout_s=_env_float(env, "SMTP_TIMEOUT_S", 15.0),
            cooldown_s=_env_float(env, "ALERT_COOLDOWN_S", 180.0),
            max_per_hour=_env_int(env, "ALERT_MAX_PER_HOUR", 12),
        )
        # 发件人没填就用 SMTP 用户名（绝大多数邮箱两者相同）
        if not cfg.email_from and cfg.smtp_user:
            cfg.email_from = cfg.smtp_user
        # 配了 host 和收件人但忘了开开关 → 视为想用，自动打开，避免"配了不生效"
        if not cfg.email_enabled and cfg.smtp_host and cfg.email_to:
            cfg.email_enabled = True
        return cfg

    # ---------------- 自检 ----------------
    def email_ready(self) -> bool:
        return bool(self.email_enabled and self.smtp_host and self.email_to)

    def email_problem(self) -> str:
        """邮件通道不可用的【具体原因】。空串表示可用。

        ★ 必须给出具体原因而不是"邮件未配置"这种笼统答案：
          运维排查时，"缺 SMTP_HOST" 和 "缺收件人" 是完全不同的修法。
        """
        if not self.email_enabled:
            return "未启用（.env 里设 ALERT_EMAIL_ENABLED=1）"
        missing = []
        if not self.smtp_host:
            missing.append("SMTP_HOST")
        if not self.email_to:
            missing.append("ALERT_EMAIL_TO")
        if not self.smtp_user:
            missing.append("SMTP_USER")
        if not self.smtp_password:
            missing.append("SMTP_PASSWORD（QQ/163 填授权码，不是登录密码）")
        if missing:
            return "缺少配置：" + "、".join(missing)
        if self.smtp_security not in ("ssl", "starttls", "none"):
            return "SMTP_SECURITY 只能是 ssl / starttls / none，当前是 %r" % self.smtp_security
        return ""

    def webhook_ready(self) -> bool:
        return bool(self.webhook_url)

    def describe(self) -> str:
        """一行摘要，启动时打印，让人一眼确认"告警到底有没有配好"。"""
        chans = []
        chans.append("控制台" if self.console else "控制台(关)")
        chans.append("文件" if self.file_enabled else "文件(关)")
        if self.webhook_ready():
            kind = (self.webhook_kind or "auto").lower()
            chans.append("Webhook" if kind in ("", "auto") else "Webhook(%s)" % kind)
        else:
            chans.append("Webhook(未配)")
        if self.email_ready():
            chans.append("邮件→%s" % ",".join(self.email_to))
        else:
            chans.append("邮件(%s)" % self.email_problem().split("（")[0])
        flags = []
        if not self.enabled:
            flags.append("总开关关闭")
        if self.dry_run:
            flags.append("演练模式")
        return ("告警 %s ｜ 通道 %s"
                % ("关闭" if not self.enabled else "开启",
                   " / ".join(chans))
                + (" ｜ " + "、".join(flags) if flags else ""))
