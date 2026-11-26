#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""告警与通知子包

对外只有四个东西需要知道：

    AlertConfig   从环境变量/.env 读配置
    AlertManager  持有策略与通道，看板用它发告警
    AlertEvent    一条告警的结构（可 JSON 序列化）
    build_alert   由「窗口 + 分析结果」造出一条告警

验证方式（不需要开发板、不需要 API Key）：

    .venv\\Scripts\\python.exe -m agri_logger.notifier
    .venv\\Scripts\\python.exe -m agri_logger.notifier --dry-run      # 演练，不真发
    .venv\\Scripts\\python.exe -m agri_logger.notifier --email-only   # 只测邮件
"""

from __future__ import annotations

from .config import AlertConfig, read_env_file
from .alert import (AlertEvent, AlertManager, AlertPolicy,
                    build_alert, build_health_alert, SEVERITY_ORDER)
from .channels import (Channel, ChannelResult, ConsoleChannel, FileChannel,
                       WebhookChannel, EmailChannel, build_channels)

__all__ = [
    "AlertConfig", "read_env_file",
    "AlertEvent", "AlertManager", "AlertPolicy",
    "build_alert", "build_health_alert", "SEVERITY_ORDER",
    "Channel", "ChannelResult", "ConsoleChannel", "FileChannel",
    "WebhookChannel", "EmailChannel", "build_channels",
]
