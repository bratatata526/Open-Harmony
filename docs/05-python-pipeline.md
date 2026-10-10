# 05 Python 采集与分析流水线

> 这是本项目的**主体工作量所在**（题目第 2、3 项），也是评分权重最高的部分。本文给出可直接落地的目录结构、关键算法与代码骨架。

---

## 1. 目录结构（全组统一，便于互相复用）

```
agri_logger/
├── __init__.py
├── __main__.py                # CLI 入口：python -m agri_logger --help
├── config.py                  # 配置加载（pydantic 校验，拒绝魔法数字）
├── models.py                  # LogRecord / Window / Report / Alert 数据类
├── collector/
│   ├── base.py                # Collector 抽象基类
│   ├── serial_collector.py    # 串口采集（含自动重连、端口自动发现）
│   ├── mqtt_collector.py      # MQTT 采集
│   ├── file_collector.py      # 文件/tail 采集（离线分析 + 模拟器对接）
│   └── manager.py             # 多源管理 + 通道故障切换
├── parser/
│   ├── regexes.py             # 与 docs/02 §6 一致的冻结正则
│   ├── parser.py              # 解析 → LogRecord
│   └── timebase.py            # 设备时间轴 → 主机时间轴换算 + 重启检测
├── pipeline/
│   ├── filters.py             # 级别/tag/zone/正则/时间窗 过滤
│   ├── templates.py           # 模板挖掘（Drain 简化实现）
│   ├── fingerprint.py         # 告警指纹
│   ├── windowing.py           # 滑动窗口聚合
│   └── store.py               # JSONL + SQLite 落盘
├── detector/
│   ├── keywords.yaml          # 关键词库（≥80 条，见 §5）
│   ├── keyword.py             # 关键词匹配 + Aho-Corasick 加速
│   └── anomaly.py             # 异常打分（多信号融合）
├── clustering/
│   ├── embed.py               # 文本向量化（LLM embedding 或 TF-IDF 降级）
│   └── cluster.py             # 层次/DBSCAN 聚类 → 异常族
├── analyzer/
│   ├── llm_client.py          # LLM 调用封装（重试/超时/成本统计/缓存）
│   ├── prompts.py             # 提示词模板（见 docs/06）
│   ├── rca.py                 # 根因分析
│   └── correlate.py           # 跨模块因果关联（Memory→Report→Sensor 链）
├── notifier/
│   ├── console.py, email.py, webhook.py
│   └── policy.py              # 去重/抑制/升级策略
├── report/
│   ├── render_md.py, render_html.py
│   └── templates/*.j2
└── web/                       # 看板（加分项，优先级最低）
```

**目录即架构**：评委看代码时，第一眼看的就是目录。这个结构能直接传达"流水线式设计"。

---

## 2. 配置管理（拒绝魔法数字）

`config/config.yaml`：

```yaml
collector:
  sources:
    - name: hilog-serial
      type: serial
      port: auto            # auto：自动发现含 "USB-SERIAL"/"CP210"/"CH340" 的端口
      baudrate: 115200
      source_kind: hilog
      enabled: true
    - name: mcu-serial
      type: serial
      port: COM3            # STM32 的虚拟串口（先到设备管理器确认；若最小系统板无板载
                            # 串口芯片，则需经 USB-TTL 接入，见 docs/03 §3.1.2）
      baudrate: 115200
      source_kind: mcu
      enabled: true
    - name: mqtt-fallback
      type: mqtt
      broker: 192.168.1.100
      port: 1883
      topic: "agri/+/log"
      source_kind: hilog
      enabled: false        # 默认关闭，演示时才开

  reconnect:
    initial_backoff_s: 1.0
    max_backoff_s: 30.0
    jitter: 0.3             # 抖动，避免多源同时重连打爆串口

filter:
  min_level: I              # 默认丢弃 DEBUG
  include_tags: []          # 空 = 全部
  exclude_tags: ["AgriFault"]   # 例：排除注入器自身的日志
  include_zones: ["A","B","C","D","E","F"]

windowing:
  window_s: 60              # 聚合窗口长度
  slide_s: 30               # 滑动步长（重叠 50%，避免故障被切在窗口边界）
  max_lines_per_window: 500 # 保护 LLM 上下文与成本

detector:
  keyword_file: agri_logger/detector/keywords.yaml
  anomaly_threshold: 0.55   # 0~1，超过则触发 LLM 分析

llm:
  base_url: https://api.deepseek.com
  model: deepseek-flash
  thinking: true            # 离线批处理，用思考模式换准确率
  reasoning_effort: high
  timeout_s: 60
  max_retries: 3
  temperature: 0.2          # 低温度，减少报告措辞随机性
  cache_dir: .cache/llm
  budget_cny_per_day: 5.0   # 超预算自动降级，防止跑飞

notifier:
  console: {enabled: true}
  email:
    enabled: false
    smtp_host: smtp.qq.com
    smtp_port: 465
    use_ssl: true
    user: your@qq.com
    password_env: AGRI_SMTP_PASS      # 从环境变量读，绝不写进配置文件
    to: ["team@example.com"]
    min_severity: P1
  webhook:
    enabled: true
    kind: dingtalk                     # dingtalk | feishu | wecom
    url_env: AGRI_WEBHOOK_URL
    min_severity: P2

store:
  jsonl_dir: data/logs
  sqlite_path: data/agri.db
  rotate_mb: 64
```

```python
# config.py
from pydantic import BaseModel, Field
import yaml, os

class LLMConfig(BaseModel):
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    thinking: bool = True
    reasoning_effort: str = "high"
    timeout_s: int = 60
    max_retries: int = 3
    temperature: float = 0.2
    cache_dir: str = ".cache/llm"
    budget_cny_per_day: float = 5.0

    @property
    def api_key(self) -> str:
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError("未设置 DEEPSEEK_API_KEY 环境变量")
        return key

# ... 其余 Config 类同理，用 pydantic 做类型与范围校验
def load_config(path: str = "config/config.yaml") -> "Config":
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)     # 配置写错立刻报错，而不是运行到一半才崩
```

> **为什么用 pydantic 校验配置**：项目中期会有大量参数调整（窗口长度、阈值、关键词），如果配置写错只在运行时才暴露，调试成本极高。**"配置错误在启动时立即失败"是工程素养的体现**，也是报告里可以写的一条设计决策。

---

## 3. 采集层（稳定性评分的正面战场）

### 3.1 串口采集器

```python
# collector/serial_collector.py
import serial, serial.tools.list_ports, time, random, logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

PORT_HINTS = ("USB-SERIAL", "CP210", "CH340", "Silicon Labs", "wch")

def discover_port() -> str | None:
    """自动发现板子的串口，避免换电脑就要改配置"""
    ports = list(serial.tools.list_ports.comports())
    if len(ports) == 1:
        return ports[0].device
    for p in ports:
        desc = f"{p.description} {p.manufacturer or ''}".upper()
        if any(h.upper() in desc for h in PORT_HINTS):
            return p.device
    return None

@dataclass
class CollectorStats:
    lines_total: int = 0
    lines_bad: int = 0
    reconnects: int = 0
    bytes_total: int = 0

class SerialCollector:
    def __init__(self, name: str, port: str, baudrate: int, source_kind: str,
                 on_line, reconnect_cfg: dict):
        self.name, self.port, self.baudrate = name, port, baudrate
        self.source_kind, self.on_line = source_kind, on_line
        self.rc = reconnect_cfg
        self.stats = CollectorStats()
        self._stop = False

    def _open(self) -> serial.Serial:
        p = self.port if self.port != "auto" else discover_port()
        if not p:
            raise serial.SerialException("未发现可用串口")
        return serial.Serial(p, self.baudrate, timeout=0.5, exclusive=True)

    def run(self) -> None:
        """无限循环：打开 → 读 → 断开 → 指数退避重连。绝不因异常退出。"""
        backoff = self.rc["initial_backoff_s"]
        while not self._stop:
            try:
                with self._open() as ser:
                    log.info("[%s] 已连接 %s @ %d", self.name, ser.port, self.baudrate)
                    backoff = self.rc["initial_backoff_s"]      # 连上后重置退避
                    buf = b""
                    while not self._stop:
                        chunk = ser.read(4096)
                        if not chunk:
                            continue
                        self.stats.bytes_total += len(chunk)
                        buf += chunk
                        # 按 \n 切分；保留残行等待下一次读取（跨 read 的行必须缓冲！）
                        while b"\n" in buf:
                            raw, buf = buf.split(b"\n", 1)
                            self._handle(raw)
                        if len(buf) > 8192:      # 保护：异常超长行，防止内存爆
                            log.warning("[%s] 超长残行被丢弃 (%d bytes)", self.name, len(buf))
                            buf = b""
            except (serial.SerialException, OSError) as e:
                self.stats.reconnects += 1
                sleep = min(backoff, self.rc["max_backoff_s"])
                sleep *= 1 + random.uniform(-self.rc["jitter"], self.rc["jitter"])
                log.warning("[%s] 连接断开(%s)，%.1fs 后重连（第 %d 次）",
                            self.name, e, sleep, self.stats.reconnects)
                time.sleep(max(0.2, sleep))
                backoff *= 2

    def _handle(self, raw: bytes) -> None:
        line = raw.decode("utf-8", errors="replace").rstrip("\r")
        if not line.strip():
            return
        self.stats.lines_total += 1
        self.on_line(self.name, self.source_kind, line)    # 交给流水线，异常在流水线内兜住
```

**四个必须做对的细节**（每一个都是"稳定性"评分的得分点）：
1. **跨 read 的行缓冲**：串口一次 `read()` 很可能只拿到半行。不做缓冲 → 大量解析失败。
2. **指数退避 + 抖动**：拔线后疯狂重连会刷爆日志；抖动避免多源同时重连。
3. **`decode(errors="replace")`**：串口噪声一定会产生非法 UTF-8 字节，**不能让一个坏字节打死采集线程**。
4. **`on_line` 里不抛异常**：回调必须 try/except 全包，否则一个脏行会退出整个采集循环。

### 3.2 通道故障切换（manager）

```python
class CollectorManager:
    """多源并行采集；MQTT 为主时串口作为热备，反之亦然。"""
    def __init__(self, cfg, on_record):
        self.collectors = [build_collector(s, self._dispatch, cfg) for s in cfg.sources]
        self.primary = cfg.primary_source           # 可选：指定主通道
        self.health = {c.name: "unknown" for c in self.collectors}

    def _dispatch(self, name, kind, line):
        self.health[name] = "alive"
        try:
            rec = self.parser.parse(line, src_name=name, source_kind=kind)
        except Exception:
            log.exception("解析器内部异常（不应发生），已丢弃该行")
            return
        if rec:
            self.on_record(rec)
```

---

## 4. 解析层

见 `docs/02-log-spec.md` §6 的冻结正则。补充三个工程要点：

### 4.1 解析器性能与降级

```python
class LogParser:
    def __init__(self):
        self.line_no = 0
        self.stats = {"ok": 0, "bad": 0, "by_reason": Counter()}

    def parse(self, raw: str, src_name: str, source_kind: str) -> LogRecord | None:
        self.line_no += 1
        m = RE_HILOG.match(raw) if source_kind == "hilog" else RE_MCU.match(raw)
        if not m:
            # 容错 1：有些板子在 hilog 前会带 ANSI 颜色码或前缀噪声，做一次清洗后重试
            cleaned = strip_ansi(raw)
            m = RE_HILOG.match(cleaned) or RE_MCU.match(cleaned)
        if not m:
            self.stats["bad"] += 1
            self.stats["by_reason"][classify_bad(raw)] += 1   # 空行/截断/乱码/未知格式
            return None

        g = m.groupdict()
        kv = dict(RE_KV.findall(g["msg"]))
        err_code = _to_int(kv.get("c"))
        return LogRecord(
            line_no=self.line_no,
            evidence_id=f"{'hl' if source_kind=='hilog' else 'mcu'}:{self.line_no:08d}",
            source=source_kind, src_name=src_name,
            ts_device=_to_float(g["ts"]), ts_host=time.time(), ts_source="host",
            level=g["level"], domain=g.get("domain", "MCU00"), tag=g["tag"],
            message=g["msg"],
            zone=kv.get("z"), seq=_to_int(kv.get("s")),
            fault=kv.get("fault"),
            err_code=err_code,
            detail=ERR_CODE_TABLE.get(err_code, "unknown"),   # ★ 查表还原可读短语
            kv=kv,
        )
```

> **两处关键点**：
> 1. `z=`/`s=`/`c=` 三个紧凑字段名在这里被映射回 `zone`/`seq`/`err_code`，**下游代码不需要知道设备端的压缩策略**；
> 2. `detail` 不是解析出来的，而是**查 `ERR_CODE_TABLE` 得到的**（表见 `docs/02` §5.4）。这样关键词库、模板命名、LLM 提示词都能用上 `reset_timeout` 这种可读短语，而设备端日志仍然紧凑。
>
> 建议把表放在 `agri_logger/parser/err_codes.yaml` 里而不是硬编码，方便非程序员组员维护。

> `evidence_id` 是**报告引用真实日志的证据编号**。报告里出现 `[hl:00001234]` 这样的引用，评委可以立刻回查原始行——**这是"不幻觉"的工程保证**，比在提示词里写"请不要编造"可靠得多。

### 4.2 时间轴与重启检测

见 `docs/02` §4.3 的 `TimeBase` 实现。补充重启检测：

```python
class StreamState:
    def __init__(self):
        self.tb = TimeBase(); self.last_ts_device = -1.0; self.boot_id = 1

    def apply(self, rec: LogRecord) -> LogRecord:
        if rec.ts_device is None:
            rec.ts_source = "host"; return rec
        if self.last_ts_device >= 0 and rec.ts_device < self.last_ts_device - 5.0:
            self.boot_id += 1                       # 设备重启
            self.tb = TimeBase()
            log.warning("检测到设备重启 → boot_id=%d", self.boot_id)
        self.last_ts_device = rec.ts_device
        self.tb.observe(rec.ts_device, rec.ts_host)
        rec.ts_host = self.tb.to_host(rec.ts_device)
        rec.ts_source = "device"
        rec.kv["boot_id"] = str(self.boot_id)
        return rec
```

### 4.3 序号连续性检测（丢日志的量化证据）

```python
class SequenceChecker:
    """按 (src_name, zone) 维护 seq 期望值，检出丢包与重复。"""
    def __init__(self):
        self.expect: dict[tuple[str, str], int] = {}
        self.lost = Counter(); self.dup = Counter()

    def check(self, rec: LogRecord) -> None:
        if rec.seq is None or not rec.zone:
            return
        key = (rec.src_name, rec.zone)
        exp = self.expect.get(key)
        if exp is not None:
            if rec.seq == exp:
                pass
            elif rec.seq > exp:
                self.lost[key] += rec.seq - exp          # 丢了 seq-exp 条
                log.warning("丢日志：%s zone=%s 缺失 seq %d~%d（%d 条）",
                            rec.src_name, rec.zone, exp, rec.seq - 1, rec.seq - exp)
            else:
                self.dup[key] += 1                        # 重传/重复
        self.expect[key] = rec.seq + 1

    def continuity_rate(self) -> float:
        total_lost = sum(self.lost.values())
        total_seen = sum(self.expect.values())
        return 1.0 - total_lost / max(1, total_lost + total_seen)
```

**验收要求**：`continuity_rate() ≥ 99.9%`。这个函数就是验收报告里那个数字的来源。

---

## 5. 过滤层（评分明确要求"过滤功能是否可用"）

支持 **5 个维度**，并支持组合与"否定过滤"：

```bash
# 只看 ERROR 及以上
python -m agri_logger tail --min-level E

# 只看 zone B，且只看传感器模块
python -m agri_logger tail --zone B --tag AgriSensor

# 正则：所有含 fault 的行
python -m agri_logger tail --regex 'fault=F\d+'

# 时间窗：从 14:30:00 到 14:35:00
python -m agri_logger tail --since 14:30:00 --until 14:35:00

# 排除噪声：去掉心跳和注入器自身
python -m agri_logger tail --exclude-tag AgriFault --exclude-regex 'heartbeat'

# 组合：实时只看"故障码 F3xxx（系统类）"
python -m agri_logger tail --regex 'fault=F3' --min-level W
```

```python
# pipeline/filters.py
@dataclass
class FilterSpec:
    min_level: str = "D"
    include_tags: list[str] = field(default_factory=list)
    exclude_tags: list[str] = field(default_factory=list)
    include_zones: list[str] = field(default_factory=list)
    regex: str | None = None
    exclude_regex: str | None = None
    since: float | None = None
    until: float | None = None

class Filter:
    LEVEL_ORDER = {"D": 0, "I": 1, "W": 2, "E": 3, "F": 4}

    def __init__(self, spec: FilterSpec):
        self.s = spec
        self._re = re.compile(spec.regex) if spec.regex else None
        self._xre = re.compile(spec.exclude_regex) if spec.exclude_regex else None

    def accept(self, r: LogRecord) -> bool:
        if self.LEVEL_ORDER[r.level] < self.LEVEL_ORDER[self.s.min_level]: return False
        if self.s.include_tags and r.tag not in self.s.include_tags: return False
        if r.tag in self.s.exclude_tags: return False
        if self.s.include_zones and r.zone not in self.s.include_zones: return False
        if self._re and not self._re.search(r.message): return False
        if self._xre and self._xre.search(r.message): return False
        if self.s.since and r.ts_host < self.s.since: return False
        if self.s.until and r.ts_host > self.s.until: return False
        return True
```

> **过滤器的价值不只是"筛日志"**：它直接决定送进 LLM 的数据量（=成本）和信噪比（=准确率）。报告里应该有一节"**过滤对 LLM 成本的影响**"：不加过滤 vs 加过滤（排除心跳/注入器/DEBUG）后，token 消耗下降多少百分比。

---

## 6. 模板挖掘（B 同学负责，**C 的依赖，优先交付**）

### 6.1 为什么必须做模板挖掘

一条日志 `z=B s=103 fault=F1002 c=1 r=3 cost=5001` 里，真正有信息量的只有 `fault=F1002`；`z`/`s`/`c`/`cost`/`r` 都是噪声。如果不做归一化：

- **关键词库命中会虚高**（同一类故障因数值不同被算作不同事件）；
- **LLM 每次看到的文本都不同 → 缓存失效 → 成本翻倍**；
- **告警无法去重**（`z=A...cost=5001` 和 `z=B...cost=5010` 被判为两条不同告警）。

### 6.2 简化 Drain 算法（约 100 行，无需第三方库）

```python
# pipeline/templates.py
import re
from collections import defaultdict

VAR = "<*>"
# 领域归零：把"数值"统一抹成 <*>，但【保留字段名】。
# 这是本项目模板挖掘的关键——保留 fault=F1002 里的 key 与值，因为故障码本身是语义而非噪声。
DOMAIN_NORMALIZERS = [
    # 1) 纯数字值 → <*>（c=1 / s=103 / cost=5001 / tx10=-1270 ...）
    (re.compile(r"(?<=[a-z0-9_])=-?\d+"), "=<*>"),
    # 2) 十六进制 / 浮点残留 → <*>
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), VAR),
    (re.compile(r"-?\d+\.\d+"), VAR),
    # 3) 脱落的裸数字 → <*>
    (re.compile(r"(?<![\w=<*])-?\d+(?![\w>])"), VAR),
]
# 注意：故障码 fault=F1002 里的 F1002 含字母，不会被规则 1 命中 → 完整保留 ✅
# 这正是我们要的：模板里能看到 fault=F1002，而不是 fault=<*>。

class TemplateMiner:
    """Drain 简化版：按 token 数分桶 → 逐层比较 → 相似度最高的叶子即模板。
    depth 固定为 4（本项目日志结构高度规整，不需要更深的树）。"""
    def __init__(self, depth: int = 4, sim_threshold: float = 0.5):
        self.depth = depth
        self.sim_threshold = sim_threshold
        self.trees: dict[int, dict] = defaultdict(dict)
        self.templates: dict[str, str] = {}      # tid -> template
        self._next_id = 0

    def _tokenize(self, msg: str) -> list[str]:
        m = msg
        for pat, rep in DOMAIN_NORMALIZERS:
            m = pat.sub(rep, m)
        return m.split()                          # 规范里字段已是空格分隔，无需再切逗号

    def add(self, msg: str) -> tuple[str, str]:
        toks = self._tokenize(msg)
        bucket = self.trees[len(toks)]
        node = bucket
        for d in range(min(self.depth - 1, len(toks))):
            key = toks[d] if d == 0 else self._key(toks[d])
            if key not in node:
                node[key] = {} if d < self.depth - 2 else {"__leaves__": []}
            node = node[key]
        leaves = node.setdefault("__leaves__", [])
        for leaf in leaves:
            if self._similar(leaf["toks"], toks) >= self.sim_threshold:
                merged = [a if a == b else VAR for a, b in zip(leaf["toks"], toks)]
                leaf["toks"], leaf["count"] = merged, leaf["count"] + 1
                return leaf["tid"], self.templates[leaf["tid"]]
        tid = f"T{self._next_id:04d}"; self._next_id += 1
        tpl = " ".join(toks)
        self.templates[tid] = tpl
        leaves.append({"tid": tid, "toks": toks, "count": 1, "sample": msg})
        return tid, tpl

    def _key(self, tok: str) -> str:
        return VAR if tok in (VAR, "=", ",") else tok

    def _similar(self, a: list[str], b: list[str]) -> float:
        if len(a) != len(b): return 0.0
        same = sum(1 for x, y in zip(a, b) if x == y or x == VAR or y == VAR)
        return same / len(a)
```

### 6.3 要产出的三个指标（写进评测报告）

| 指标 | 定义 | 目标 |
|---|---|---|
| 模板压缩率 | `1 - 模板数 / 不同原始文本数` | ≥ 60% |
| 模板稳定性 | 相同故障在 6 个 zone 上归入同一 tid 的比例 | ≥ 95% |
| token 节省率 | 用模板替换原始文本后，送 LLM 的 token 下降比例 | ≥ 50% |

> **答辩叙事（讲模板挖掘时用）**："日志里 90% 的文本差异是**噪声数值差异**，不是语义差异。我们先做模板归一化，再送 LLM，**成本降一半，准确率还升了**——因为模型不再被无关数字干扰。"

---

## 7. 异常打分与滑动窗口

### 7.1 多信号融合打分

单一信号都不够可靠，融合才稳：

```python
# detector/anomaly.py
from dataclasses import dataclass

WEIGHTS = {
    "level":      0.35,   # ERROR/FATAL 直接加分
    "keyword":    0.30,   # 命中关键词库（加权，高危关键词权重更高）
    "novelty":    0.15,   # 该模板首次出现（未知异常最有价值）
    "burst":      0.10,   # 短时间内同模板激增
    "threshold":  0.10,   # 数值越限（val vs thr）
}

@dataclass
class AnomalyScore:
    total: float
    parts: dict[str, float]

    def explain(self) -> str:
        return " + ".join(f"{k}={v:.2f}" for k, v in self.parts.items() if v > 0)

def score(win: "Window", kw_hits: dict[str, float], known_templates: set[str],
          baseline: dict[str, float]) -> AnomalyScore:
    n = max(1, win.line_count)
    err_ratio = win.err_count / n
    p_level = min(1.0, err_ratio * 5)                       # 1 条 ERROR / 20 行即满分

    p_kw = min(1.0, sum(kw_hits.values()) / 3.0)

    novel = [t for t in win.template_ids if t not in known_templates]
    p_novel = min(1.0, len(novel) / 3.0)

    p_burst = 0.0
    for tid, c in win.template_counts.items():
        base = baseline.get(tid, 0.0)
        if base > 0 and c / base > 3.0:
            p_burst = min(1.0, (c / base) / 10.0)

    p_thr = 1.0 if win.has_threshold_violation else 0.0

    parts = {"level": p_level * WEIGHTS["level"],
             "keyword": p_kw * WEIGHTS["keyword"],
             "novelty": p_novel * WEIGHTS["novelty"],
             "burst": p_burst * WEIGHTS["burst"],
             "threshold": p_thr * WEIGHTS["threshold"]}
    return AnomalyScore(total=round(sum(parts.values()), 3), parts=parts)
```

> **为什么用多信号融合而不是单纯看 ERROR 数量**：`F1005 越阈值` 只是 WARN，但业务上很关键；而某些 ERROR（如 `F2004 断线重连`）其实是**正常事件**。单看级别必然误报。**这个 `explain()` 输出要写进报告**——它让"为什么判为异常"变得可解释，直接对应评分点"LLM 对异常判断的准确率/可解释性"。

### 7.2 滑动窗口

```python
# pipeline/windowing.py
class SlidingWindowAggregator:
    """重叠窗口（window_s=60, slide_s=30）：避免故障恰好落在窗口边界被切成两半。"""
    def __init__(self, window_s: int, slide_s: int, max_lines: int):
        ...
    def feed(self, rec: LogRecord): ...
    def pop_ready(self) -> list["Window"]:
        """返回已经"结束"的窗口（end + slide < now），保证不会在半途分析"""
        ...
```

**窗口内要预计算**（供 detector 与 LLM 使用）：行数、各级别计数、模板分布、关键词命中、时序间隔的均值/标准差（**间隔异常本身就是信号**：正常 5s 一次，突然 30s 没有日志 = 任务卡死）、`seq` 缺口数。

> **"日志间隔突变"是一个被大多数人忽略的强信号**。任务卡死（`F3003`）往往不是"多了一条 ERROR"，而是"**少了一堆 INFO**"。只做关键词匹配的方案会完全漏掉这类异常——**这是我们相对关键词方案的核心优势**，报告里必须单列一节讲。

---

## 8. 存储层

```python
# pipeline/store.py
class Store:
    def __init__(self, jsonl_dir: str, sqlite_path: str, rotate_mb: int = 64):
        ...
    def write(self, rec: LogRecord) -> None:
        self.buf.append(rec)
        if len(self.buf) >= 200:          # 批量写，别一行一次 IO
            self.flush()
    def flush(self) -> None:
        # 1) 追加 JSONL（可 grep、可重放、可作回归集）
        # 2) INSERT INTO logs（可 SQL 聚合查询）
        ...
```

**为什么 JSONL + SQLite 双写，而不是直接上 Elasticsearch**（答辩可能被问）：
- 本课题数据量级：6 设备 × 1 条/5s ≈ 日均 10 万行 —— **SQLite 完全够用**（百万级轻松）；
- 引入 ES 需要额外服务与运维，**与"轻量级 AI + 系统运维"的课题定位不符**；
- JSONL 保证**可重放**（用新规则重新解析历史数据），这是算法迭代的关键能力；
- 如果真要扩展：`Store` 是接口，**换实现即可**（`ElasticStore`），架构上已经预留。

---

## 9. 命令行入口（演示时的门面）

```bash
python -m agri_logger collect            # 启动完整流水线（采集→解析→聚合→分析→告警）
python -m agri_logger tail --min-level E # 只看错误流
python -m agri_logger replay data/logs/2026-xx.jsonl --detector        # 离线重放（评测用）
python -m agri_logger report --last 1    # 生成最近 1 小时报告
python -m agri_logger eval --gt eval/dataset/gt.jsonl --pred data/pred.jsonl
python -m agri_logger stats              # 采集统计：行数/解析率/连续性/重连次数
```

**`stats` 的输出就是"稳定性"评分的证据**，建议做成固定格式：

```
════════ 采集统计（运行 02:14:33） ════════
源             行数     解析成功   解析失败   成功率   重连   连续性
hilog-serial   8,412    8,409      3         99.96%   1     99.94%
mcu-serial     4,203    4,201      2         99.95%   0     99.98%
──────────────────────────────────────────
合计          12,615   12,610      5         99.96%   1     99.95%
字节数 1.8 MB ｜ 平均速率 1.56 行/秒 ｜ 峰值 12.3 行/秒
```

---

## 10. 测试要求（W5 起持续维护）

```python
# tests/test_parser.py
import pytest
from agri_logger.parser.parser import LogParser

# 样例直接取自 docs/02-log-spec.md §7，保证"规范—代码—测试"三者一致
GOOD_HILOG = "000120.456789 I A001 AgriSensor: z=A s=24 t=265 h=612 l=830 tick=120456"
GOOD_ERR   = "000510.334455 E A001 AgriSensor: z=B s=103 fault=F1002 c=1 r=3 cost=5001"
GOOD_MCU   = "[STM32] 000513 I SENSOR: s=103 t=248 h=612 a=830"
BAD_LINES  = ["", "   ", "garbage", "000120.456789 X A001 T: x",
              "000120.456789 I A001 AgriSensor", "\x00\x01\x02", "A" * 10000]

def test_parse_hilog_ok():
    r = LogParser().parse(GOOD_HILOG, "COM5", "hilog")
    assert r and r.level == "I" and r.zone == "A" and r.seq == 24
    assert r.kv["t"] == "265" and r.tag == "AgriSensor"
    assert r.domain == "A001"

def test_parse_error_codes():
    """c= 必须被查表还原成可读短语——这是下游关键词匹配的前提"""
    r = LogParser().parse(GOOD_ERR, "COM5", "hilog")
    assert r.fault == "F1002" and r.err_code == 1 and r.detail == "reset_timeout"

def test_parse_mcu_ok():
    r = LogParser().parse(GOOD_MCU, "COM3", "mcu")
    assert r and r.source == "mcu" and r.seq == 103 and r.kv["t"] == "248"

@pytest.mark.parametrize("bad", BAD_LINES)
def test_never_raises(bad):
    """最重要的测试：解析器对任何输入都不得抛异常"""
    assert LogParser().parse(bad, "COM5", "hilog") is None

def test_line_length_guard():
    """128 字节硬限制：超过则 hilog_lite 会静默丢弃，必须能被检出"""
    long_line = "000120.456789 I A001 AgriSensor: z=A s=1 " + "x=1 " * 40
    assert len(long_line.encode()) > 128
    # 解析器仍应正常处理（行长超限是"设备端"的问题，不是解析器的责任）
    # 但监控模块必须统计并告警，见 LineLengthMonitor
```

**必须有的测试清单**：

| 测试文件 | 覆盖 |
|---|---|
| `test_parser.py` | 正常/脏数据/超长/编码错误，**永不抛异常**；错误码查表正确 |
| `test_timebase.py` | 时间换算精度、设备重启检测（`s` 归零 + `ts_device` 回退）、时钟回退 |
| `test_sequence.py` | 丢包检测、重复检测、连续性计算 |
| `test_templates.py` | **相同故障不同数值 → 同一模板**；`fault=F1002` 必须保留在模板里不被抹成 `<*>` |
| `test_filters.py` | 5 个维度 + 组合 + 否定 |
| `test_anomaly.py` | 已知场景打分排序正确（故障窗口分数 > 正常窗口） |
| `test_line_length.py` | 行长监控：超 128 字节必须产生告警 |

运行：`pytest -q --cov=agri_logger --cov-report=term-missing`，**目标覆盖率 ≥ 60%**。
