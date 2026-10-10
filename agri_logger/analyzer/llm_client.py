#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 调用封装 —— 重试 / 缓存 / 成本统计 / 预算上限 / 降级

设计原则（详见 docs/06-llm-analysis.md）：
  1. 永不抛异常 —— 失败返回 None，由上层决定降级策略
  2. 必须缓存   —— 同一份输入用 sha256 做 key，重复分析零成本
  3. 必须计费   —— 统计 token 与费用，可查、可限
  4. 必须限预算 —— 防止死循环把余额烧光（这不是杞人忧天，是真实事故）
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 价格表（元 / 百万 token）—— 按 DeepSeek 官方定价
# 空闲时段价格是高峰时段的一半；高峰 = 周一至周五 9:00-12:00, 14:00-18:00
# ---------------------------------------------------------------------------
PRICE = {
    "deepseek-flash": {
        "in_hit":  {"peak": 0.04, "off": 0.02},
        "in_miss": {"peak": 2.00, "off": 1.00},
        "out":     {"peak": 8.00, "off": 4.00},
    },
    "deepseek-v4-pro": {
        "in_hit":  {"peak": 0.30, "off": 0.15},
        "in_miss": {"peak": 9.00, "off": 4.50},
        "out":     {"peak": 27.00, "off": 13.50},
    },
}


def is_peak_now(ts: Optional[float] = None) -> bool:
    t = time.localtime(ts or time.time())
    if t.tm_wday >= 5:                      # 周末全天空闲
        return False
    hm = t.tm_hour * 60 + t.tm_min
    return (9 * 60 <= hm < 12 * 60) or (14 * 60 <= hm < 18 * 60)


@dataclass
class Usage:
    prompt_hit: int = 0
    prompt_miss: int = 0
    completion: int = 0
    cost_cny: float = 0.0
    cached: bool = False
    calls: int = 0

    def add(self, other: "Usage") -> None:
        self.prompt_hit += other.prompt_hit
        self.prompt_miss += other.prompt_miss
        self.completion += other.completion
        self.cost_cny += other.cost_cny
        self.calls += other.calls

    def __str__(self) -> str:
        return (f"调用 {self.calls} 次 | 输入 hit={self.prompt_hit} miss={self.prompt_miss} "
                f"输出={self.completion} | {self.cost_cny:.4f} 元"
                + ("  [缓存命中]" if self.cached else ""))


class LLMClient:
    """DeepSeek 客户端封装。API Key 从环境变量 DEEPSEEK_API_KEY 读取。"""

    def __init__(self,
                 model: str = "deepseek-flash",
                 base_url: str = "https://api.deepseek.com",
                 cache_dir: str = ".cache/llm",
                 timeout_s: int = 60,
                 max_retries: int = 3,
                 temperature: float = 0.2,
                 thinking: bool = True,
                 reasoning_effort: str = "high",
                 budget_cny_per_day: float = 5.0,
                 enabled: bool = True):
        self.model = model
        self.base_url = base_url
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.temperature = temperature
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        self.budget = budget_cny_per_day
        self.enabled = enabled

        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.usage_today = Usage()
        self._day = time.strftime("%Y-%m-%d")
        self._client: Any = None
        self._init_error: Optional[str] = None

    # ---------------- 初始化（延迟到第一次调用） ----------------
    def _ensure_client(self) -> bool:
        if self._client is not None:
            return True
        if not self.enabled:
            self._init_error = "LLM 已禁用（未配置 API Key 或显式关闭）"
            return False
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not key:
            self._init_error = ("未设置环境变量 DEEPSEEK_API_KEY。\n"
                                "  PowerShell: $env:DEEPSEEK_API_KEY='sk-xxxx'")
            return False
        try:
            from openai import OpenAI
        except ImportError:
            self._init_error = "未安装 openai 库：pip install openai"
            return False
        try:
            self._client = OpenAI(api_key=key, base_url=self.base_url,
                                  timeout=self.timeout_s, max_retries=0)
            return True
        except Exception as e:                                  # noqa: BLE001
            self._init_error = f"创建客户端失败：{e}"
            return False

    @property
    def available(self) -> bool:
        return self._ensure_client()

    @property
    def init_error(self) -> Optional[str]:
        self._ensure_client()
        return self._init_error

    # ---------------- 缓存 ----------------
    def _cache_key(self, messages: list[dict], tag: str) -> str:
        blob = tag + json.dumps(messages, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    def _cache_get(self, key: str) -> Optional[dict]:
        p = self.cache_dir / f"{key}.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return None
        return None

    def _cache_put(self, key: str, value: dict) -> None:
        try:
            (self.cache_dir / f"{key}.json").write_text(
                json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as e:
            log.warning("写缓存失败：%s", e)

    # ---------------- 主调用 ----------------
    def call_json(self, messages: list[dict], tag: str = "default",
                  max_tokens: int = 3000) -> tuple[Optional[dict], Usage]:
        """调用 LLM 并返回解析后的 JSON。失败返回 (None, usage)，永不抛异常。"""
        self._rollover_day()

        if self.usage_today.cost_cny > self.budget:
            log.error("LLM 日预算已用完（%.4f 元 / %.2f 元），进入降级模式",
                      self.usage_today.cost_cny, self.budget)
            return None, Usage()

        key = self._cache_key(messages, tag)
        hit = self._cache_get(key)
        if hit is not None:
            log.info("[%s] 缓存命中，0 成本", tag)
            return hit, Usage(cached=True)

        if not self._ensure_client():
            log.warning("[%s] LLM 不可用：%s", tag, self._init_error)
            return None, Usage()

        delay = 1.0
        for attempt in range(1, self.max_retries + 1):
            try:
                kwargs: dict[str, Any] = dict(
                    model=self.model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    max_tokens=max_tokens,
                    temperature=self.temperature,
                )
                if self.thinking:
                    kwargs["extra_body"] = {
                        "thinking": {"type": "enabled"},
                        "reasoning_effort": self.reasoning_effort,
                    }

                resp = self._client.chat.completions.create(**kwargs)
                content = resp.choices[0].message.content

                if not content:
                    # 官方文档明确：JSON Output 模式有概率返回空 content
                    log.warning("[%s] 返回空 content，第 %d 次重试", tag, attempt)
                    time.sleep(delay); delay *= 2
                    continue

                data = json.loads(content)
                u = self._account(getattr(resp, "usage", None), tag)
                self._cache_put(key, data)
                return data, u

            except json.JSONDecodeError as e:
                log.warning("[%s] JSON 解析失败（%s），附加修正提示重试", tag, e)
                messages = messages + [{
                    "role": "user",
                    "content": "上次输出不是合法 JSON。请只输出 JSON，不要任何解释文字、不要 markdown 代码块。"
                }]
                time.sleep(delay); delay *= 2
            except Exception as e:                              # noqa: BLE001
                name = type(e).__name__
                log.warning("[%s] 调用异常 %s：%s，%.1fs 后重试", tag, name, e, delay)
                time.sleep(delay); delay *= 2

        log.error("[%s] LLM 调用最终失败，进入降级模式", tag)
        return None, Usage()

    # ---------------- 计费 ----------------
    def _account(self, usage: Any, tag: str) -> Usage:
        hit = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        miss = int(getattr(usage, "prompt_cache_miss_tokens", 0) or 0) or (prompt - hit)
        out = int(getattr(usage, "completion_tokens", 0) or 0)

        p = PRICE.get(self.model, PRICE["deepseek-flash"])
        seg = "peak" if is_peak_now() else "off"
        cost = (hit * p["in_hit"][seg] + miss * p["in_miss"][seg]
                + out * p["out"][seg]) / 1_000_000.0

        u = Usage(prompt_hit=hit, prompt_miss=miss, completion=out,
                  cost_cny=cost, calls=1)
        self.usage_today.add(u)
        log.info("[%s] hit=%d miss=%d out=%d 本次 %.4f 元 累计 %.4f 元",
                 tag, hit, miss, out, cost, self.usage_today.cost_cny)
        return u

    def _rollover_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day, self.usage_today = today, Usage()

    # ---------------- 报表 ----------------
    def cost_report(self) -> str:
        u = self.usage_today
        total_in = u.prompt_hit + u.prompt_miss
        lines = [
            "════════ LLM 成本报表 ════════",
            f"模型            {self.model}（{'高峰' if is_peak_now() else '空闲'}时段计价）",
            f"调用次数        {u.calls}",
            f"输入 token      {total_in:,}（缓存命中 {u.prompt_hit:,} / 未命中 {u.prompt_miss:,}）",
            f"输出 token      {u.completion:,}",
            f"费用            {u.cost_cny:.4f} 元",
            f"日预算          {self.budget:.2f} 元（剩余 {max(0, self.budget - u.cost_cny):.2f} 元）",
        ]
        cache_dir = self.cache_dir
        if cache_dir.exists():
            lines.append(f"本地缓存文件    {len(list(cache_dir.glob('*.json')))} 个")
        return "\n".join(lines)
