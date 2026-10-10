# 00 接口交接文档（B、C 同学从这里开始）

> **用途**：这份文档是"设备端 ↔ 分析端"的接口契约落地说明。
> 读完你应该能：不依赖真机、不依赖 A 同学的进度，**立刻开始写解析/识别/报告代码**。
>
> **前提**：接口已冻结。你可以放心把它当契约用，不会中途变。

---

## 1. 你的环境（3 步）

```powershell
cd E:\Code\Harmony

# 1) 虚拟环境已建好，pyserial 已装。先自检：
.venv\Scripts\python.exe tools/serial_capture.py --check-env

# 2) 跑接口契约自测（应该全部 ✅）
.venv\Scripts\python.exe -m agri_logger.parser.parser

# 3) 生成你自己的数据集（固定 seed，可复现）
.venv\Scripts\python.exe tools/openharmony_log_sim.py --duration 1800 --seed 20260101 `
    --out eval/dataset/d2_1800s.log --label-out eval/dataset/d2_1800s.gt.jsonl
```

> VSCode 里解释器选 `.venv\Scripts\python.exe`（`.vscode/settings.json` 已配好，新开终端会自动激活）。

---

## 2. 日志长什么样（唯一需要记住的格式）

### 真机/模拟器输出的原始行

```
000120.456789 I A001 AgriSensor: 1 24 2 281 3 466 7 120456 z=A
└──── 前缀 ────┘ └───────── 消息体（字段ID 值 成对）─────────┘
```

| 部分 | 含义 |
|---|---|
| `000120.456789` | 设备**上电后**秒.微秒（Hi3861 无 RTC，不是绝对时间） |
| `I` | 级别：`D`/`I`/`W`/`E`/`F` |
| `A001` | domain（模块）：A001传感 A002上报 A003健康 A004执行器 A005任务 A006通信 |
| `AgriSensor` | tag（日志来源模块） |
| `1 24 2 281 ...` | **消息体**：字段ID 与值**成对出现**，空格分隔 |
| `z=A` | zone（逻辑设备 A~F；`-` 表示全局日志） |

### 字段 ID 速查（完整表见 `agri_logger/parser/fields.py`）

| ID | 名称 | 换算 | 例 |
|---|---|---|---|
| 1 | seq | 原值 | `1 24` → 序号 24 |
| 2 | temp | **÷10 ℃** | `2 281` → 28.1℃ |
| 3 | humi | **÷10 %** | `3 466` → 46.6% |
| 4 | light | 原值 | `4 830` |
| 5 | fault | 查故障码表 | `5 1002` → `F1002 单总线复位无应答` |
| 6 | code | 查错误码表 | `6 1` → `reset_timeout` |
| 7 | tick | ms | `7 120456` |
| 8 | heap | 字节 | `8 3072` |
| 9 | cost | ms | `9 5010` |
| 10 | retry | 原值 | |
| 11 | state | 0=idle 1=irrigating 2=venting 3=degraded | |
| 13 | rssi | 正值表示 -dBm | `13 83` → -83dBm |
| 14 | queue | 原值 | |
| 15 | miss | 原值 | |

> **为什么值全是数字**：`hilog_lite` 不支持 `%s`，且单行 ≤128 字节。字段 ID 让日志**顺序无关**、**可读**、**可扩展**。

---

## 3. 解析器：把原始行变成结构体（**直接用，别重写**）

```python
from agri_logger.parser.parser import LogParser

p = LogParser()
rec = p.parse("000120.456789 I A001 AgriSensor: 1 24 2 281 3 466 z=A", src_name="COM3")

rec.level        # 'I'
rec.zone         # 'A'
rec.seq          # 24
rec.temp         # 28.1   ← 已 ÷10，直接用
rec.humi         # 46.6
rec.fault        # None
rec.physical     # {'seq': '24', 'temp': '28.1℃', 'humi': '46.6%'}
rec.evidence_id  # 'hl:00000001'  ← 报告引用这个，可回查
rec.summary()    # 一行摘要，可直接打印
```

故障行会被**自动还原成可读形式**：

```python
rec = p.parse("000510.334455 E A001 AgriSensor: 1 103 5 1002 6 1 10 3 z=B")
rec.fault        # 1002
rec.fault_tag    # 'F1002'
rec.fault_name   # '单总线复位无应答'
rec.detail       # 'reset_timeout'   ← 由 code=1 查表得来
rec.is_normal_event  # False
```

> ⚠️ **`is_normal_event` 是误报率的关键**：`F2004 断线重连`、`F4002 灌溉状态变更`、`F5001 设备启动`
> 都是**正常运维事件**，必须排除在异常之外。不做这一步，误报率会明显偏高。

**解析器纪律**（已实现，勿破坏）：
1. **永不抛异常**——脏数据返回 `None` 并计数
2. **永不丢原始行**——`raw_line` 原样保留，便于用新规则重放
3. **行长监控**——`>128` 字节会在真机上被**静默丢弃**，必须统计

---

## 4. 模拟器：不依赖真机拿到数据

```powershell
# 边看边开发（10 倍速）
.venv\Scripts\python.exe tools/openharmony_log_sim.py --speed 10

# 生成带标注的评测集（固定 seed，可复现）
.venv\Scripts\python.exe tools/openharmony_log_sim.py --duration 1800 --seed 20260101 `
    --out eval/dataset/d2_1800s.log --label-out eval/dataset/d2_1800s.gt.jsonl

# 带信道噪声（检验解析器健壮性）
.venv\Scripts\python.exe tools/openharmony_log_sim.py --duration 600 `
    --drop-rate 0.002 --truncate-rate 0.001 --garbage-rate 0.001 --out eval/dataset/noisy.log
```

**模拟器与固件从同一张字段表生成日志**（导入 `agri_logger.parser.fields`），所以：
- **字节级同构**，同一解析器能解析两者
- 不会出现"模拟数据和真机数据格式不一致"的问题

**已生成的数据集**：

| 文件 | 内容 |
|---|---|
| `eval/dataset/d2_demo.log` | 1800 秒，4475 行，148 次故障注入 |
| `eval/dataset/d2_demo.gt.jsonl` | 对应的 ground-truth 标注（标准答案） |
| `eval/dataset/noisy_demo.log` | 带 2% 丢行 + 1% 截断 + 1% 乱码 |

**标注格式**（算召回率/误报率就靠它）：

```json
{"ts": 100.1, "event_id": "E00001", "zone": "A", "fault": "F1002", "level": "E",
 "scenario": "sensor_timeout", "evidence_seq": 21, "root_cause_class": "onewire_bus_failure",
 "is_cascade": false, "expected_cluster": "sensor_timeout", "severity_true": "P1"}
```

> **`is_cascade` 字段很重要**：`true` 表示这是次生事件，评测时不单独计入召回分母——否则"报了根因但没报症状"的优秀系统会被冤枉成漏报。

---

## 5. 分工与你的第一步任务

| | B 同学 | C 同学 |
|---|---|---|
| **主责** | 采集、解析、过滤、模板挖掘、存储 | 识别、LLM 分析、告警、报告 |
| **第一步** | `collector/serial_collector.py`（断线重连、跨 read 行缓冲） | `detector/keywords.yaml`（≥80 条，8 类） |
| **第二步** | `pipeline/filters.py`（5 维度过滤） | `detector/anomaly.py`（多信号打分） |
| **第三步** | `pipeline/templates.py`（Drain 简化版） | **日志静默检测**（间隔突变） |

**代码骨架位置**：`docs/05-python-pipeline.md`（B）、`docs/06-llm-analysis.md`（C），都含可运行实现。

> **`detector/keywords.yaml`（C 的第一步）是 B 和 C 的共同依赖**，优先交付最小版本。

---

## 6. 接口变更流程（重要）

字段表是**三端共享**的。要改必须同时改四处：

1. `firmware/hi3861/agri_logger/log_def.h` 的 `agri_field_t` 枚举
2. `agri_logger/parser/fields.py` 的 `FIELDS` 表
3. `docs/02-log-spec.md` §3.3
4. 跑 `python -m agri_logger.parser.parser` 确认自测仍通过

**改完必须通知另外两人**（这是三人团队唯一的同步点）。

---

## 7. 自检清单（开工前跑一遍）

```powershell
# ① 环境
.venv\Scripts\python.exe tools/serial_capture.py --check-env

# ② 接口契约
.venv\Scripts\python.exe -m agri_logger.parser.parser     # 应输出"✅ 自测通过"

# ③ 模拟器
.venv\Scripts\python.exe tools/openharmony_log_sim.py --selftest

# ④ 离线回放（不需要真机）
.venv\Scripts\python.exe tools/serial_capture.py --replay eval/dataset/d2_demo.log --min-level E
```

四条全过 = 你可以独立开工，**不需要等 A 同学的真机稳定**。
