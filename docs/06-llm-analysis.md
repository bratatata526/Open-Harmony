# 06 LLM 智能分析与报告生成

> 这是题目第 (3)(4) 项的核心实现，也是评分权重最高的部分。本文的重点不是"怎么调 API"，而是**怎么把 LLM 用在刀刃上**。

---

## 1. 核心设计原则：什么时候**不该**调用 LLM

这是整个项目最重要的设计判断，也是答辩时最能区分"会用 AI"和"调过 API"的地方。

| 任务 | 用什么做 | 为什么不用 LLM |
|---|---|---|
| 判断一条日志是不是 ERROR | 看 `level` 字段 | 结构化字段是免费的、100% 准确的，用 LLM 是浪费 |
| 判断有没有"内存分配失败" | 关键词库 + 故障码 `F3001` | 确定的字符串匹配，准确率 100%，成本 0，延迟 0 |
| 判断任务是否卡死 | **日志间隔统计**（正常 5s，突然 30s 无日志） | 这是时序统计问题，LLM 看文本看不出来 |
| 判断"堆不足 → 分配失败 → 上报超时"是同一条因果链 | LLM ✅ | 需要跨模块语义推理，规则难以穷举 |
| 生成人类可读的根因描述 | LLM ✅ | 自然语言生成是 LLM 的本职 |
| 把 20 条不同表述的异常归成 3 个"异常族" | LLM ✅ | 语义聚类正是 LLM 擅长的 |
| 给出处置建议 | LLM ✅ + 知识库检索增强 | 需要运维领域知识 |

**结论：LLM 只做三件事——语义聚类、根因推理、自然语言报告。其余全部用规则/统计完成。**

```
日志流 3000 行/小时
   │
   ├─ 规则过滤（级别、tag、zone）        → 砍掉 80%
   ├─ 模板归一化（同一故障只留 1 个代表）  → 再砍 70%
   ├─ 关键词/故障码打标                  → 已知异常直接定性（0 次 LLM 调用）
   └─ 只剩"未知模板 + 异常窗口"才进 LLM   → 约 1~10 次调用/小时
```

**答辩金句**："我们没有把日志丢给大模型，而是**先用工程手段把问题规模缩小 100 倍，再让大模型解决真正需要智能的那 1%**。这既控制了成本，也保证了准确率——因为规则负责的部分是 100% 准确的。"

---

## 2. 三级 LLM 调用架构

```
┌── L1：窗口异常分析（每个异常窗口 1 次调用）─────────────────────┐
│  输入：窗口统计摘要 + 异常模板 Top-N + 命中的关键词              │
│  输出：{是否真异常, 异常族归属, 严重度, 一句话描述}              │
│  频率：异常窗口才触发，正常窗口 0 调用                          │
└──────────────────────────────────────────────────────────────┘
┌── L2：根因分析（每个"异常族" 1 次调用）────────────────────────┐
│  输入：该族的代表性日志（真实行）+ 时间线 + 上下文窗口           │
│  输出：5 段式报告 JSON（现象/证据/根因/影响/建议）              │
│  频率：每族每 10 分钟最多 1 次（避免同一故障反复分析）           │
└──────────────────────────────────────────────────────────────┘
┌── L3：日报/综述（每天 1 次）──────────────────────────────────┐
│  输入：当天所有族的统计 + 关键事件时间线                        │
│  输出：运维日报 Markdown                                       │
└──────────────────────────────────────────────────────────────┘
```

**L1/L2 的分工理由**：L1 是"快速筛"，要求快、便宜、可高频；L2 是"深度想"，要求准、可解释。分开之后，**L2 的输入是 L1 已经结构化的结果**（而不是原始日志），token 消耗和幻觉风险都大幅下降。

---

## 3. LLM 客户端封装

```python
# analyzer/llm_client.py
import json, os, time, hashlib, logging
from dataclasses import dataclass
from pathlib import Path
from openai import OpenAI, APIError, APITimeoutError, RateLimitError

log = logging.getLogger(__name__)

# 价格表（元 / 百万 token），按 DeepSeek 官方定价，用于成本统计
PRICE = {
    "deepseek-flash": {
        "in_hit":       {"peak": 0.04, "off": 0.02},
        "in_miss":      {"peak": 2.0,  "off": 1.0},
        "out":          {"peak": 8.0,  "off": 4.0},
    }
}

def is_peak_now(ts: float | None = None) -> bool:
    """高峰时段：北京时间周一至周五 9:00-12:00, 14:00-18:00（不含法定节假日）"""
    t = time.localtime(ts or time.time())
    if t.tm_wday >= 5:
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

class LLMClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = OpenAI(api_key=cfg.api_key, base_url=cfg.base_url,
                             timeout=cfg.timeout_s, max_retries=0)  # 自己实现重试
        self.cache_dir = Path(cfg.cache_dir); self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.usage_today = Usage(); self._day = time.strftime("%Y-%m-%d")
        self.model = cfg.model

    # ---------- 缓存：同一份输入永不重复付费 ----------
    def _cache_key(self, messages: list[dict], schema_name: str) -> str:
        blob = schema_name + json.dumps(messages, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    def _cache_get(self, key: str):
        p = self.cache_dir / f"{key}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return None

    def _cache_put(self, key: str, value) -> None:
        (self.cache_dir / f"{key}.json").write_text(
            json.dumps(value, ensure_ascii=False), encoding="utf-8")

    # ---------- 主调用：重试 + 缓存 + 成本 + 预算 ----------
    def call_json(self, messages: list[dict], schema_name: str = "default",
                  max_tokens: int = 2048) -> tuple[dict | None, Usage]:
        """返回 (解析后的 dict 或 None, usage)。永不抛异常——失败返回 None 由上层降级。"""
        self._rollover_day()
        if self.usage_today.cost_cny > self.cfg.budget_cny_per_day:
            log.error("LLM 日预算已用完（%.4f 元），进入降级模式", self.usage_today.cost_cny)
            return None, Usage(cached=False)

        key = self._cache_key(messages, schema_name)
        if (hit := self._cache_get(key)) is not None:
            return hit, Usage(cached=True)          # 缓存命中：0 成本，0 延迟

        delay = 1.0
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    response_format={"type": "json_object"},   # 强制 JSON 输出
                    max_tokens=max_tokens,                     # 防 JSON 被截断
                    temperature=self.cfg.temperature,
                    extra_body=({"thinking": {"type": "enabled"},
                                 "reasoning_effort": self.cfg.reasoning_effort}
                                if self.cfg.thinking else None),
                )
                content = resp.choices[0].message.content
                if not content:                       # 官方文档提示：JSON 模式偶发空 content
                    log.warning("LLM 返回空 content，第 %d 次重试", attempt)
                    time.sleep(delay); delay *= 2; continue
                data = json.loads(content)
                u = self._account(resp.usage)
                self._cache_put(key, data)
                return data, u
            except RateLimitError:
                log.warning("触发限速（429），%.1fs 后重试", delay)
                time.sleep(delay); delay *= 2
            except (APITimeoutError, APIError) as e:
                log.warning("LLM 调用失败（%s），%.1fs 后重试", e, delay)
                time.sleep(delay); delay *= 2
            except json.JSONDecodeError:
                log.warning("JSON 解析失败（第 %d 次），将附加修正提示重试", attempt)
                messages = messages + [{"role": "user",
                    "content": "上次输出不是合法 JSON。请只输出 JSON，不要任何解释文字与 markdown 代码块。"}]
        log.error("LLM 调用最终失败，进入降级模式")
        return None, Usage()

    def _account(self, usage) -> Usage:
        hit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
        miss = getattr(usage, "prompt_cache_miss_tokens", 0) or (
            (usage.prompt_tokens or 0) - hit)
        out = usage.completion_tokens or 0
        p = PRICE[self.model]
        seg = "peak" if is_peak_now() else "off"
        cost = (hit * p["in_hit"][seg] + miss * p["in_miss"][seg]
                + out * p["out"][seg]) / 1_000_000
        self.usage_today.prompt_hit += hit
        self.usage_today.prompt_miss += miss
        self.usage_today.completion += out
        self.usage_today.cost_cny += cost
        log.info("LLM 用量 hit=%d miss=%d out=%d 本次 %.4f 元 累计 %.4f 元",
                 hit, miss, out, cost, self.usage_today.cost_cny)
        return Usage(hit, miss, out, cost)

    def _rollover_day(self):
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day, self.usage_today = today, Usage()
```

**六个工程细节，每一个都是"能跑通"和"能稳定跑"的区别**：

| 细节 | 原因 |
|---|---|
| `max_retries=0` + 自己实现重试 | SDK 自带重试对 429 的退避策略不透明，且无法插入"降级"逻辑 |
| 检查空 `content` | [官方文档明确提示](https://api-docs.deepseek.com/zh-cn/guides/json_mode)：JSON Output 模式**有概率返回空 content**，不处理会随机崩 |
| `max_tokens` 必须设 | 否则 JSON 可能被截断，`json.loads` 直接失败 |
| 缓存 key 用 `sha256(messages)` | 同一窗口重复分析（如进程重启后重放）**永久免费** |
| 统计 `prompt_cache_hit_tokens` | [DeepSeek 硬盘缓存默认开启](https://api-docs.deepseek.com/zh-cn/guides/kv_cache)，命中价格是未命中的 1/50，**必须统计出来写进报告** |
| 日预算硬上限 | 防止"死循环调用把余额烧光"——这不是杞人忧天，是真实事故 |

---

## 4. 提示词工程（三段式）

### 4.1 设计原则

**缓存友好的提示词布局**（这是省钱的关键）：

```
┌──────────── 稳定前缀（永不变）────────────┐  ← 磁盘缓存命中的部分
│ system: 角色 + 任务 + 输出格式 + 少样本   │
│ user:   知识库（故障码字典、判据）        │
├──────────── 可变后缀（每次都变）──────────┤  ← 未命中部分
│ user:   本次窗口的实际日志与统计          │
└──────────────────────────────────────────┘
```

**为什么这样排**：DeepSeek 的上下文硬盘缓存按**前缀**匹配。把不变的部分放前面、变化的部分放最后，能让前 1000+ token 稳定命中缓存，**价格相差 50 倍**。

### 4.2 L1 提示词：窗口异常判定

```python
# analyzer/prompts.py
SYSTEM_L1 = """你是 OpenHarmony 物联网设备的日志运维专家。你的任务是判断一个日志窗口是否包含真实异常，并对异常归类。

【领域背景】
设备是"智慧农业温室大棚"的 6 个环境监测节点（Zone-A~F），运行 OpenHarmony 轻量系统。
每个节点每 5 秒采集一次温度/湿度/光照，通过 MQTT 上报。

【故障码字典（判据的唯一来源，不得臆造其他故障码）】
F1001/F1002/F1006 = 传感器读取失败类（超时、单总线无应答、连续失败）
F1003/F1004/F1005 = 数据质量类（CRC 错、物理不可能值、越阈值）
F2001~F2005 = 通信类（连接失败、上报超时、信号弱、重连、帧校验错）
F3001~F3006 = 系统类（内存分配失败、栈溢出、任务卡死、队列积压、看门狗、堆不足）
F4001/F4002 = 执行器类（灌溉超时、状态变更）
F5001/F5002 = 生命周期类（启动、配置重载失败）

【重要判据】
1. level=W 的 F2004（断线重连）和 F4002（灌溉状态变更）是【正常运维事件】，不是异常。
2. 若窗口内 INFO 日志的【时间间隔】明显超过 5 秒（说明任务可能卡死），即使没有 ERROR 也要标记异常。
3. 数值越限（F1005）属于业务异常，虽然级别是 W，但需要关注。
4. seq 出现缺口说明日志或数据丢失，属于异常。

【输出格式】必须输出合法 JSON，且只输出 JSON：
{"is_anomaly": true, "cluster_key": "sensor_read_failure", "severity": "P1",
 "summary": "一句话中文描述", "evidence_refs": ["hl:00001234"], "confidence": 0.9}

字段说明：
- is_anomaly: 布尔值
- cluster_key: 异常族标识，用简短英文下划线命名；同一类根因必须用同一个 key
- severity: P0(设备离线/数据全断) / P1(功能受损) / P2(性能下降) / P3(仅提示)
- summary: 不超过 50 字，必须包含"哪个 zone、什么现象"
- evidence_refs: 支撑判断的日志证据编号（必须来自输入中真实存在的编号）
- confidence: 0~1

【示例】
输入窗口（zone=B，60秒，14 行）：
  [hl:00000103] E A001 AgriSensor: z=B s=103 fault=F1002 c=1 r=3 cost=5001
  [hl:00000104] W A001 AgriSensor: z=B s=104 fault=F1004 c=3 tx10=-1270 f=1
输出：
{"is_anomaly": true, "cluster_key": "onewire_sensor_timeout", "severity": "P1",
 "summary": "Zone-B 温度传感器单总线无应答，连续 3 次重试失败后输出无效值 -127.0", 
 "evidence_refs": ["hl:00000103", "hl:00000104"], "confidence": 0.92}
"""

USER_L1_TEMPLATE = """【知识库】当前已知异常族（若本次异常属于其中之一，请复用其 cluster_key）：
{known_clusters}

【本次窗口统计】
- Zone: {zone}    时间范围: {ts_start} ~ {ts_end}    时长: {duration_s} 秒
- 总行数: {line_count}    ERROR: {err_count}    WARN: {warn_count}    INFO: {info_count}
- 日志间隔: 均值 {gap_mean:.2f}s / 最大 {gap_max:.2f}s（正常应为 5s）
- seq 缺口: {seq_gap} 条
- 命中的关键词: {keywords}
- 命中的故障码: {faults}

【日志模板分布（Top 12，<*> 表示变量）】
{template_summary}

【代表性原始日志（含真实证据编号）】
{evidence_lines}

请按上述 JSON 格式输出判断结果。"""
```

**输入构造代码**：

```python
def build_l1_messages(win: Window, stats: WindowStats, known_clusters: list[str]) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_L1},                       # 稳定 → 缓存
        {"role": "user", "content": USER_L1_TEMPLATE.format(            # 可变 → 不缓存
            known_clusters=", ".join(known_clusters) or "（暂无）",
            zone=stats.zone, ts_start=stats.ts_start_str, ts_end=stats.ts_end_str,
            duration_s=stats.duration_s, line_count=stats.line_count,
            err_count=stats.err_count, warn_count=stats.warn_count, info_count=stats.info_count,
            gap_mean=stats.gap_mean, gap_max=stats.gap_max,
            seq_gap=stats.seq_gap, keywords=", ".join(stats.keywords) or "无",
            faults=", ".join(stats.faults) or "无",
            template_summary=stats.template_summary(),
            evidence_lines=stats.evidence_lines(max_lines=30),
        )},
    ]
```

### 4.3 L2 提示词：根因分析报告

```python
SYSTEM_L2 = """你是 OpenHarmony 系统运维专家，负责输出根因分析报告。报告要能让一线运维人员直接照着处理。

【铁律】
1. 只使用输入中提供的日志证据。**严禁编造**任何日志行、时间戳、地址、函数名、版本号。
2. 每条证据必须引用输入中真实存在的证据编号（形如 hl:00001234 或 mcu:00000456）。
3. 若证据不足以确定根因，把 suspected_root_cause 写成"证据不足，疑似方向：..."，并把 confidence 降到 0.5 以下。诚实比自信更重要。
4. 5 个段落均为中文字符串，不要用嵌套对象。

【输出格式】只输出 JSON：
{
  "title": "报告标题（≤30字，含 zone 与现象）",
  "severity": "P0|P1|P2|P3",
  "symptom":  "现象：客观描述发生了什么，含时间与设备",
  "evidence": "证据：逐条列出，每条以 [证据编号] 开头，引用真实日志",
  "suspected_root_cause": "疑似根因：给出因果链推理，说明为什么是这个原因而不是别的",
  "impact":   "影响：影响了哪些功能、多少设备、数据是否丢失",
  "action":   "处置建议：分【立即】【短期】【长期】三档，可执行",
  "confidence": 0.0
}

【领域知识（用于推理，勿直接照抄）】
- 单总线(DS18B20)读失败常见原因：4.7kΩ 上拉缺失、高优先级任务打断时序、线缆过长、供电不足
- 任务卡死(F3003)常见原因：低优先级任务被饿死、死循环、在中断里做阻塞操作
- 内存分配失败(F3001)常见原因：未释放的日志/上报缓冲、递归过深、任务栈设置过小
- 上报超时(F2002)可能由网络问题引起，也可能是内存不足导致的次生故障（要看时间顺序！）
- Hi3861 轻量设备 RAM 非常有限（约 300KB 级），任务栈溢出与堆枯竭是高发问题
"""

USER_L2_TEMPLATE = """【异常族】{cluster_key}
【关联窗口数】{window_count}    【首次出现】{first_seen}    【最近出现】{last_seen}
【涉及 Zone】{zones}

【完整证据日志（按时间排序，共 {n} 条）】
{evidence_lines}

【时间线】
{timeline}

【跨源信息（若有）】
{cross_source}

请输出根因分析报告 JSON。"""
```

### 4.4 防止幻觉的三重保险

| 保险 | 实现 |
|---|---|
| **提示词层** | 明确"严禁编造" + 要求引用真实证据编号 |
| **结构层** | `evidence_refs` 是数组，程序可校验每个元素是否存在于本次输入 |
| **程序层（最关键）** | 渲染报告时，**用程序把证据编号替换成真实日志行**，LLM 输出的文本里根本不包含日志内容，它想编也没地方编 |

```python
# analyzer/rca.py 中的证据校验与渲染
class EvidenceValidator:
    def __init__(self, records: list[LogRecord]):
        self.index = {r.evidence_id: r for r in records}

    def validate(self, refs: list[str]) -> tuple[list[LogRecord], list[str]]:
        """返回 (有效的证据记录, 无效的引用)。无效引用是幻觉的直接证据，要记入评测。"""
        ok, bad = [], []
        for ref in refs:
            (ok if ref in self.index else bad).append(self.index.get(ref, ref))
        return ok, bad

    def render_evidence_block(self, refs: list[str]) -> str:
        """报告中的证据段由程序生成，保证 100% 真实"""
        out = []
        for ref in refs:
            r = self.index.get(ref)
            if r is None:
                continue          # 幻觉引用直接不渲染
            out.append(f"[{r.evidence_id}] {r.ts_str} {r.level} {r.tag}: {r.raw_line}")
        return "\n".join(out)
```

> **答辩时这一段要重点讲**：我们的报告里每一条证据都是**程序从原始日志里取出来的**，大模型只负责"组织语言"。所以报告的**事实准确率是 100%**——因为事实不经过模型。评委追问"LLM 幻觉怎么办"，这就是标准答案。

---

## 5. 语义聚类（C 同学负责）

### 5.1 两条路线

| 路线 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **A：LLM 直接聚类** | 把 N 条异常日志（或其模板）一次性给 LLM，要求分组 | 零额外依赖，语义理解强，可解释 | 受上下文长度限制（~50 条/次） |
| **B：向量 + 聚类** | 文本向量化 → 余弦距离 → 层次/DBSCAN 聚类 | 可处理大量数据，增量更新快 | 需要 embedding 能力；纯向量对"数值差异"不敏感 |

**推荐：B 为主 + A 为辅**
- 用**模板 ID**（已归一化）作为聚类单元，数据量本来就小；
- 向量化可用 LLM 的 embedding 接口；**若无 embedding 能力，降级为 TF-IDF + 余弦相似度**（在模板空间上效果足够好，因为模板已经把噪声数值去掉了）；
- 对每个聚类簇，用 **A（LLM）为簇生成一个可读的族名与摘要**。

### 5.2 实现

```python
# clustering/cluster.py
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import AgglomerativeClustering
from sklearn.metrics.pairwise import cosine_distances

class AnomalyClusterer:
    """在"模板空间"上聚类。关键：聚类的单元是模板，不是原始日志。"""
    def __init__(self, distance_threshold: float = 0.35):
        self.threshold = distance_threshold

    def fit(self, templates: list[tuple[str, str]]) -> dict[int, list[str]]:
        """templates: [(template_id, template_text), ...] → {cluster_id: [template_id, ...]}"""
        if len(templates) < 2:
            return {0: [t[0] for t in templates]} if templates else {}
        tids = [t[0] for t in templates]
        texts = [t[1] for t in templates]

        # 字符级 n-gram：对 "z=<*> s=<*> fault=<*> c=<*>" 这类短模板，比词级更稳
        vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), min_df=1)
        X = vec.fit_transform(texts)

        # 层次聚类：不需要预设簇数，且能用距离阈值控制粒度
        model = AgglomerativeClustering(n_clusters=None,
                                        distance_threshold=self.threshold,
                                        metric="precomputed", linkage="average")
        labels = model.fit_predict(cosine_distances(X))
        out: dict[int, list[str]] = {}
        for tid, lab in zip(tids, labels):
            out.setdefault(int(lab), []).append(tid)
        return out

    def quality(self, labels: dict[int, list[str]], gt: dict[str, str]) -> dict:
        """用 ground-truth 算聚类质量（V-measure 是本模块的核心评测指标）"""
        from sklearn.metrics import homogeneity_completeness_v_measure
        tids = [t for grp in labels.values() for t in grp]
        pred = [cid for cid, grp in labels.items() for _ in grp]
        true = [gt.get(t, "unknown") for t in tids]
        h, c, v = homogeneity_completeness_v_measure(true, pred)
        return {"homogeneity": round(h, 3), "completeness": round(c, 3), "v_measure": round(v, 3)}
```

**指标解释（答辩要能说清）**：
- **同质性 homogeneity**：一个簇里的模板是否属于同一真实故障类（聚类是否"纯"）；
- **完整性 completeness**：同一真实故障类的模板是否都被分到同一个簇（聚类是否"全"）；
- **V-measure**：两者的调和平均，是聚类质量的综合指标。

---

## 6. 成本控制（必须能报出准确数字）

### 6.1 实测口径的成本测算

设定：6 个 zone，每 5 秒上报 1 次，每小时约 4320 条正常日志 + 约 60 条故障日志。

| 调用类型 | 每次输入 token | 每次输出 token | 频次 | 每天调用次数 |
|---|---|---|---|---|
| L1 窗口分析 | ~1600（system 800 缓存命中 + 数据 800） | ~150 | 异常窗口约 20 次/小时 | 480 |
| L2 根因分析 | ~2500（system 1200 缓存命中 + 证据 1300） | ~700 | 每族每 10 分钟 1 次，约 6 次/小时 | 144 |
| L3 日报 | ~8000 | ~2000 | 1 次/天 | 1 |

**按 DeepSeek 官方价格计算的日成本**（空闲时段价格约为高峰时段的一半）：

| 项目 | 输入（缓存命中） | 输入（未命中） | 输出 | 日费用（空闲时段） |
|---|---|---|---|---|
| L1 ×480 | 480×800 = 384K | 480×800 = 384K | 480×150 = 72K | 384K×0.02 + 384K×1.0 + 72K×4.0 = 0.0077 + 0.384 + 0.288 ≈ **0.68 元** |
| L2 ×144 | 144×1200 = 173K | 144×1300 = 187K | 144×700 = 101K | 0.0035 + 0.187 + 0.403 ≈ **0.59 元** |
| L3 ×1 | 0 | 8K | 2K | 0.008 + 0.008 ≈ **0.02 元** |
| **合计** | | | | **≈ 1.3 元/天** |

**加上两层优化后实际更低**：
- **模板归一化**：送进去的是模板摘要而非原始日志 → 输入 token 再降 50%+；
- **缓存**：同一窗口重复分析 0 成本；进程重启后重放历史数据几乎免费；
- **异常窗口稀疏**：正常运行时异常窗口远少于 20 次/小时。

> **报告里要写这一节**："本方案在 6 设备满负荷运行下，LLM 日成本约 1.3 元（理论最坏值）。通过模板归一化与缓存，实测成本降至 ______ 元/天。对比之下，如果直接把全部日志送进模型，日成本约 ______ 元（高 2~3 个数量级）。"
> **这就是"技术深度"的体现**——不是会不会调 API，而是**知不知道该花多少钱、以及怎么把成本降下来**。

### 6.2 成本统计报表

```bash
python -m agri_logger cost --today
```
```
════════ LLM 成本报表（2026-xx-xx） ════════
调用次数        142   （缓存命中 61 次，命中率 43.0%）
输入 token      218,340  （缓存命中 96,200 / 未命中 122,140）
输出 token       31,880
费用            0.4731 元   （空闲时段计价）
日预算          5.0000 元   （剩余 90.5%）
────────────────────────────────
L1 窗口分析  98 次   0.2110 元   平均 2.2 ms token/次
L2 根因分析  43 次   0.2570 元   平均 3.4 ms token/次
L3 日报       1 次   0.0051 元
```

---

## 7. 三级降级策略（**这是加分项，不是妥协**）

```python
# analyzer/rca.py
class RootCauseAnalyzer:
    def analyze(self, cluster: Cluster, records: list[LogRecord]) -> Report:
        msgs = build_l2_messages(cluster, records)
        data, usage = self.llm.call_json(msgs, schema_name="rca_l2", max_tokens=2048)

        if data is not None:
            return self._render(data, records, mode="llm", usage=usage)

        # 降级 1：本地规则库（故障码 → 预置的根因模板）
        if (rule := self.rule_engine.match(cluster.faults)):
            return self._render(rule, records, mode="rule", usage=usage)

        # 降级 2：纯模板拼接（只给现象 + 证据，不给根因结论）
        return self._render(self._minimal(cluster, records), records,
                            mode="raw", usage=usage)
```

```python
# 降级时报告要诚实标注
DEGRADE_BANNER = {
    "rule": "⚠️ 本报告由**本地规则引擎**生成（LLM 不可用）。根因结论基于故障码字典，未做跨模块语义推理。",
    "raw":  "⚠️ 本报告仅包含**原始事实与统计**（LLM 与规则库均不可用），不含根因推断，请人工分析。",
}
```

**本地规则库示例**（`analyzer/rules.yaml`）：

```yaml
- fault: F3001
  title: "内存分配失败，疑似堆内存枯竭"
  suspected_root_cause: "请求的分配大小超过当前可用堆。结合同窗口的 F3006(heap_low) 判断，疑似存在未释放的缓冲区导致的渐进式堆枯竭。"
  impact: "上报任务可能因内存不足而失败（次生 F2002），导致数据丢失。"
  action:
    immediate: ["重启设备释放内存", "确认是否配置了过大的日志缓冲区"]
    short_term: ["检查上报缓冲的释放路径", "把日志环形缓冲从 8KB 降到 4KB"]
    long_term: ["引入堆水位监控并在 heap<8KB 时主动降频采集"]
  confidence: 0.75
- fault: F3003
  title: "任务调度超时，疑似低优先级任务被饿死"
  suspected_root_cause: "高优先级任务长时间占用 CPU 或存在阻塞操作，导致低优先级采集/上报任务得不到调度。"
  impact: "数据上报中断，云端看到设备'静默'而非'故障'，监控盲区。"
  action:
    immediate: ["确认卡死任务", "重启设备"]
    short_term: ["降低上报任务优先级或缩短其临界区", "在中断服务程序中移除阻塞调用"]
    long_term: ["为关键任务配置看门狗与心跳超时告警", "用时间片轮转替代纯优先级调度"]
  confidence: 0.8
- fault: F1002
  title: "温度传感器单总线无应答"
  suspected_root_cause: "DS18B20 复位脉冲后未收到存在脉冲，常见于 4.7kΩ 上拉电阻缺失/虚焊、线缆过长或读时序被高优先级任务打断。"
  impact: "温度数据无效（读出 -127.0 或 85.0），依赖温度的灌溉决策失效。"
  action:
    immediate: ["检查传感器接线与上拉电阻", "确认数据线长度 < 1m"]
    short_term: ["读取关键段关中断保护时序", "失败时沿用上次有效值并标记数据为降级"]
    long_term: ["改用带 CRC 校验的读取流程并统计失败率"]
  confidence: 0.85
```

> **降级能力的答辩价值**：主动演示"断网后系统仍能出报告（标注降级模式）"，比只演示"联网时很好用"更能证明工程成熟度。**一个依赖外部 API 才能工作的系统，在生产环境是不可接受的。**

---

## 8. 跨模块因果关联（"智能聚合"的核心体现）

> **本节的变化说明**：原方案设计的是"STM32 日志 + Hi3861 日志"的**跨设备**关联，依赖双板方案。**现改为设备内跨模块关联**——分析价值不降反升，因为不依赖任何外部时间同步。规则表只需把 `src` 从 `mcu/hilog` 改成按 `domain` 区分即可。

### 8.1 要解决的问题

**一个根因会同时污染多个模块的日志**，运维的难点正是把碎片化告警还原成一条因果链：

```
001240.001900 E A003 AgriHealth: z=C s=312 fault=F3001 c=12 heap=1024 req=8192   ← 根因：堆内存枯竭
001240.002400 E A002 AgriReport: z=C s=313 fault=F2002 c=7 cost=5010             ← 次生：上报超时
001240.503100 W A001 AgriSensor: z=C s=314 fault=F1004 c=3 tx10=-1270 f=1        ← 三次生：数据无效
```

**不做关联** → 报 3 条独立告警（运维要自己猜它们是一回事，还得自己排先后）；
**做关联** → 报 1 条："Zone-C 堆内存枯竭 → 上报超时 → 传感器读数无效（降级运行）"，**并指出根因是 A003 模块的内存问题，A002/A001 只是受害者**。

> **为什么这让"智能聚合"名副其实**：三条告警的模块不同、故障码不同、级别不同，关键词匹配只能得到三条平级告警。**只有理解模块间的依赖关系与时间顺序，才能还原成一条链。** 这正是本项目区别于"关键词告警脚本"的地方。

### 8.2 关联算法

```python
# analyzer/correlate.py
from dataclasses import dataclass

@dataclass
class CausalLink:
    cause: LogRecord
    effect: LogRecord
    delta_ms: float
    rule: str

class CrossModuleCorrelator:
    """在时间窗内寻找跨模块因果对。规则显式化，可解释、可评测。"""

    # 因果规则表：(原因特征, 结果特征, 最大时间差ms, 规则名)
    # 特征匹配的是【解析后的 LogRecord】，其中 c= 已被查表还原成 detail 短语
    # domain: A001=传感 A002=上报 A003=健康 A004=执行器 A005=任务 A006=通信
    RULES = [
        # 内存类链：堆不足 → 分配失败 → 上报超时
        ({"domain": "A003", "fault": "F3006"},
         {"domain": "A003", "fault": "F3001"},             3000, "heap_low→alloc_failed"),
        ({"domain": "A003", "fault": "F3001"},
         {"domain": "A002", "fault": "F2002"},             3000, "alloc_failed→publish_timeout"),
        # 调度类链：任务卡死 → 上报超时 + 日志静默
        ({"domain": "A005", "fault": "F3003"},
         {"domain": "A002", "fault": "F2002"},             8000, "task_hung→publish_timeout"),
        ({"domain": "A005", "fault": "F3003"},
         {"gap_after_s": (">", 10)},                      15000, "task_hung→log_silence"),
        # 传感类链：传感器超时 → 数据无效 → 降级
        ({"domain": "A001", "fault": "F1002"},
         {"domain": "A001", "fault": "F1004"},             3000, "sensor_timeout→invalid_value"),
        ({"domain": "A001", "fault": "F1006"},
         {"domain": "A001", "fault": "F1004"},             3000, "sensor_offline→invalid_value"),
        # 执行器类链：越阈值 → 灌溉 → 执行器超时
        ({"domain": "A001", "fault": "F1005"},
         {"domain": "A004", "fault": "F4002"},             2000, "threshold→irrigation_start"),
        ({"domain": "A004", "fault": "F4002"},
         {"domain": "A004", "fault": "F4001"},             8000, "irrigation→actuator_timeout"),
        # 通信类链：连接失败 → 重连 → 恢复
        ({"domain": "A006", "fault": "F2001"},
         {"domain": "A006", "fault": "F2004"},             5000, "connect_refused→reconnect"),
    ]

    def link(self, records: list[LogRecord], max_delta_ms: float = 15000) -> list[CausalLink]:
        links, sorted_recs = [], sorted(records, key=lambda r: r.ts_host)
        for i, cause in enumerate(sorted_recs):
            for effect in sorted_recs[i + 1:]:
                delta = (effect.ts_host - cause.ts_host) * 1000
                if delta > max_delta_ms:
                    break                        # 已排序，后面的只会更远
                for c_pat, e_pat, lim, name in self.RULES:
                    if delta <= lim and _match(cause, c_pat) and _match(effect, e_pat):
                        links.append(CausalLink(cause, effect, delta, name))
        return self._dedup_chains(links)

    def _dedup_chains(self, links: list[CausalLink]) -> list[CausalLink]:
        """把 A→B→C 的链合并成一条链，避免报告里出现碎片化的因果对"""
        ...
```

**因果链可视化**（写进报告；有看板时可以画成 Mermaid 图）：

```
Zone-C 内存故障因果链（2026-xx-xx 00:12:33 ~ 00:13:03）
  [hl:00000311]  A003 AgriHealth  F3006 heap=3072            ← 前兆
        │ +1.2s
  [hl:00000312]  A003 AgriHealth  F3001 alloc_failed heap=1024 req=8192   ← 根因
        │ +0.5s
  [hl:00000313]  A002 AgriReport  F2002 publish_timeout cost=5010        ← 次生
        │ +0.5s
  [hl:00000314]  A001 AgriSensor  F1004 tx10=-1270                       ← 三次生（降级运行）
        │ +30s
  [hl:00000341]  A001 AgriSensor  z=C s=341 t=248 h=610                  ← 恢复
```

> **这是整个项目技术含量最高的一块**，也是"智能聚合"区别于"关键词告警"的分水岭。答辩时如果只能讲一个技术亮点，讲这个。
> **注意**：这条链横跨 3 个模块、3 种故障码、2 个级别，**关键词匹配只能得到 4 条平级告警**。只有理解"健康模块监控的是全局资源，它的异常会向下游所有模块扩散"这一领域知识，才能还原成链。

---

## 9. 告警策略引擎

### 9.1 为什么需要（否则演示会很难看）

同一个故障每 5 秒触发一次 → 一个小时 720 条告警 → **邮箱被刷爆，运维直接关掉告警**。这就是"告警疲劳"，是真实运维中的核心问题。

### 9.2 四项策略

```python
# notifier/policy.py
class AlertPolicy:
    def __init__(self, cfg):
        self.dedup_window_s = cfg.dedup_window_s      # 默认 600：同指纹 10 分钟内只报 1 次
        self.flap_threshold = cfg.flap_threshold      # 默认 5：10 分钟内同指纹出现 5 次 → 判定为抖动
        self.silence_until: dict[str, float] = {}     # 指纹 → 静默截止时间
        self.seen: dict[str, list[float]] = defaultdict(list)

    def decide(self, alert: Alert) -> tuple[bool, str]:
        """返回 (是否发送, 原因)"""
        fp, now = alert.fingerprint, time.time()
        times = [t for t in self.seen[fp] if now - t < self.dedup_window_s]
        self.seen[fp] = times + [now]

        # 1) 静默期（人工确认后静默）
        if fp in self.silence_until and now < self.silence_until[fp]:
            return False, "silenced"

        # 2) 抖动抑制：短时间内反复出现 → 升级为一条"持续故障"告警，而不是发 N 条
        if len(times) >= self.flap_threshold:
            if now - times[0] < self.dedup_window_s:
                return self._emit_flapping(alert, len(times) + 1)

        # 3) 指纹去重：窗口内已发过
        if times:
            return False, "deduped"

        # 4) 升级：P0 立即发全部渠道；P3 只写库不发信
        return True, "sent"

    def _emit_flapping(self, alert: Alert, count: int) -> tuple[bool, str]:
        """升级为 P0 持续故障告警，并进入 30 分钟静默"""
        alert.severity = "P0"
        alert.title = f"[持续故障] {alert.title}（10 分钟内第 {count} 次）"
        self.silence_until[alert.fingerprint] = time.time() + 1800
        return True, "escalated_flapping"
```

**指纹定义**（`pipeline/fingerprint.py`）——决定"什么算同一个故障"：

```python
def fingerprint(rec: LogRecord) -> str:
    """指纹 = 稳定的语义要素组合，剔除 zone/seq/时间/数值等噪声。
    注意：zone 【不】进指纹 → 6 个 zone 的同类故障合并为一条告警（跨设备聚合）。"""
    return "|".join(filter(None, [
        rec.domain,                       # A001
        rec.tag,                          # AgriSensor
        rec.fault or rec.template_id or rec.message[:40],
    ]))
# 例："A001|AgriSensor|F1002"
```

> **"zone 不进指纹"是一个刻意的设计决策**，答辩可能被追问，答案："**运维关心的是'传感器超时'这个故障模式，而不是'哪个大棚'。** 6 个 zone 同时报同一故障，说明是系统性问题（比如固件 bug 或统一的上拉电阻批次问题），合并成一条告警并在内容里列出涉及的 zone，比发 6 条更有信息量。"

### 9.3 推送渠道

```python
# notifier/webhook.py —— 钉钉/飞书/企业微信 Markdown 卡片
class WebhookNotifier:
    TEMPLATES = {
        "dingtalk": {"msgtype": "markdown",
                     "markdown": {"title": "{title}", "text": "{md}"}},
        "feishu":   {"msg_type": "interactive", "card": {...}},
        "wecom":    {"msgtype": "markdown", "markdown": {"content": "{md}"}},
    }
```

**告警内容模板**（要精简，手机上一眼能看完）：

```markdown
## 🔴 [P1] Zone-B 温度传感器单总线无应答

**时间**：2026-xx-xx 00:08:32　**设备**：Zone-B（受影响：B）
**现象**：连续 3 次读取超时，输出无效值 -127.0
**疑似根因**：DS18B20 复位无应答，疑似上拉电阻虚焊或读取时序被高优先级任务打断
**建议**：【立即】检查传感器接线与 4.7kΩ 上拉

[查看完整报告](http://127.0.0.1:8080/report/R20260101_0008)
```

**邮件**：用 `smtplib` + SSL（QQ 邮箱需用**授权码**而非登录密码，`smtp.qq.com:465`）。**密码必须走环境变量**，绝不进 Git（这是安全基本素养，报告里可写一句）。

---

## 10. 报告渲染

```jinja
{# report/templates/report.md.j2 #}
# {{ r.title }}

| 项目 | 值 |
|---|---|
| 报告编号 | `{{ r.report_id }}` |
| 严重度 | **{{ r.severity }}** |
| 异常族 | `{{ r.cluster_key }}` |
| 时间范围 | {{ r.ts_start }} ~ {{ r.ts_end }} |
| 涉及设备 | {{ r.zones \| join('、') }} |
| 置信度 | {{ '%.0f' % (r.confidence * 100) }}% |
| 生成方式 | {{ {'llm':'LLM 语义分析','rule':'本地规则引擎（降级）','raw':'仅原始事实（降级）'}[r.mode] }} |

{% if r.banner %}> {{ r.banner }}{% endif %}

## 一、现象
{{ r.symptom }}

## 二、证据
{# 关键：证据段由程序生成，不经过 LLM #}
```
{{ r.evidence_block }}
```

## 三、疑似根因
{{ r.suspected_root_cause }}

{{ r.causal_chain_mermaid if r.causal_chain_mermaid }}

## 四、影响
{{ r.impact }}

## 五、处置建议
{{ r.action }}

---
<sub>本报告由 OpenHarmony 日志智能聚合分析工具自动生成 ｜ 证据引用共 {{ r.evidence_count }} 条，
全部可在 `data/logs/{{ r.date }}.jsonl` 中按证据编号回查</sub>
```

**"证据可回查"这句话要真的做到**：提供一个命令

```bash
python -m agri_logger show hl:00000103        # 打印该证据的原始行、来源、时间、上下文 5 行
```

答辩现场评委随机报一个证据编号，你当场敲命令回查——**这个动作的说服力超过任何 PPT**。
