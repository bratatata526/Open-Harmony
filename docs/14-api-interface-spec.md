# 前端接口规范（看板 UI 对接契约）

> **这份文档是「分析后端 → 看板前端」的唯一契约。**
> 前端只认这里的字段名与类型；后端改字段必须同步改这里，并在 §5 变更记录留一行。
>
> **读者**：负责前端与 UI 优化的同学。你**不需要读 Python 代码**，按本文档对接即可。
>
> **相关文档**：日志字段字典的冻结源头是 `docs/02-log-spec.md`；
> 告警策略与邮件配置见 `docs/13-alerting.md`；解析器用法见 `docs/00-interface-handoff.md`。
>
> **配套阅读**（强烈建议按这个顺序）：
> 1. **`docs/15-data-formats.md`** —— 板子上传格式 / 日志格式 / 报告格式（"数据长什么样"）
> 2. **`docs/16-frontend-dev-guide.md`** —— 前端开发规范（"该怎么写"）
> 3. 本文 —— 接口字段清单（"接口返回什么"）
>
> **配套代码**（不用自己写取数逻辑）：
> `web/api-contract.js`（枚举/配色/轮询的唯一来源）、`web/api-client.js`（取数封装）、
> `web/samples/`（真实响应样例，可离线开发）。用法见 `docs/16` §2、§3。
>
> **代码位置**：`tools/dashboard_server.py`（HTTP 层）、`web/index.html`（现有前端）、
> `agri_logger/parser/`（数据结构）、`agri_logger/pipeline/windows.py`（窗口聚合）、
> `agri_logger/notifier/`（告警）。

---

## 0. 一句话说清这套数据是什么

一块 Hi3861 开发板采集**一个传感器节点**的读数（温度 / 湿度 / 光照），固件把这些读数
**派生**成 6 个逻辑设备（Zone-A ~ Zone-F，代表园区里 6 个大棚共享同一片天空），每 5 秒一轮
输出规范日志；后端把日志解析成结构化记录，聚合成「窗口」并给每个窗口打异常分，
最后把异常窗口交给 LLM（或规则引擎）产出根因报告，并通过告警通道推送。

```
传感器读数（1 个物理节点）
   │ 固件派生出 6 个逻辑 zone
   ▼
串口日志行 ──解析──▶ LogRecord ──60s 窗口聚合──▶ WindowStats ──打分──▶ 异常窗口
                                                                  │
                                                  ┌───────────────┴───────────────┐
                                                  ▼                               ▼
                                            LLM 根因报告 .md                  告警推送（邮件/Webhook）
                                                  │                               │
                                                  └──────────► 看板 ◄──────────────┘
```

### 0.1 两种运行形态（前端必须都支持）

| 形态 | 含义 | `meta.mode` | 数据量特征 |
|---|---|---|---|
| **回放 replay** | 读磁盘静态日志文件，用于复现算法效果 | `"replay"` | 一次性全量，`/api/logs` 可翻页 |
| **实时 live** | 直接读开发板串口（或模拟器），持续增长 | `"live"` | 用 `/api/live/logs?since=` 增量拉 |

**判据只有 `meta.mode` 一个**，不要用别的字段猜。
回放模式下"日志一直在刷"是正常的（循环回放），**不代表板子连着** —— 这是现场演示最容易被误解的点。

### 0.2 单传感器口径（直接影响 UI 文案）

* 物理上**只有一个传感器节点**。温度/湿度/光照是**同一个节点**的三项读数。
* `zone` 是**逻辑设备编号**（`"A"`~`"F"`），**不是"第几个传感器"**。
* 固件单设备模式（`ZONE_COUNT=1`）下日志里只有 `zone="A"`，此时 `/api/devices`
  只返回一个元素、`/api/zones` 只有一个 key。
* **文案不要写「6 个传感器」**，请写「6 个逻辑设备」或「6 个大棚」。
* `z="-"` 表示**整机级**日志（健康检查、启动、模式上报），不属于任何单个 zone。

---

## 1. 通用约定

| 项目 | 约定 |
|---|---|
| Base URL | `http://127.0.0.1:8080`（端口由 `--port` 决定） |
| 协议 | HTTP/1.1，**全部 GET**，无 POST，无鉴权（本机只读） |
| 编码 | `application/json; charset=utf-8` |
| 缓存 | 所有响应带 `Cache-Control: no-store`，不要自己做 HTTP 缓存 |
| 空值 | 可选字段可能为 `null`（尤其图表数据点），前端**必须容忍** |
| 时间单位 | `t` / `ts` / `start` / `end` / `span` 都是**设备上电后的相对秒数**（`float`），**不是 Unix 时间戳** |
| 主机时间 | 只有 `meta.loaded_at`、告警的 `created_at`、报告的 `mtime` 是**本地时间字符串** |
| 温度/湿度 | 已是**物理值**（℃ / %），不是原始 ×10 整数 |
| 光照 | 相对值 `0~1000`，**无单位**，可能为 `null` |

### 1.1 错误响应

```json
{ "error": "TypeError: ..." }
```

HTTP `500`。**前端必须 try/catch**，失败时保留旧数据 + 顶部提示条，不要让整块白屏。
`404` 只出现在 `/api/report?name=`（报告不存在）与静态资源缺失。

### 1.2 轮询节奏（照抄即可，别更密）

| 数据 | 间隔 | 说明 |
|---|---|---|
| `/api/meta` | 2 s | 顶部徽标 / KPI |
| `/api/summary` `/api/timeline` `/api/zones` `/api/families` | 3 s | 图表 |
| `/api/live/logs` | 1 s | 实时日志流（增量） |
| `/api/live/status` | 2 s | ONLINE / OFFLINE 徽标 |
| `/api/analyze?action=status` | 1 s（**仅分析进行中**） | 进度条；结束后停 |
| `/api/alerts` | 5 s | 告警列表 |
| `/api/reports` | 10 s | 报告列表 |

后端对 `/api/meta` `/api/summary` `/api/timeline` `/api/zones` `/api/families` `/api/windows`
带 **2 秒缓存**（缓存键含 `zone`）。比这更密不会更快，只浪费 CPU。

---

## 2. 数据模型

### 2.1 `LogRecord` —— 一条日志

前端在 `/api/logs`、`/api/live/logs` 里看到的就是它：

| 字段 | 类型 | 含义 |
|---|---|---|
| `eid` | `string` | **证据编号**，形如 `hl:00000123`。报告里引用的就是它，可回查原始行 |
| `ts` | `float \| null` | 设备相对秒数 |
| `level` | `"D" \| "I" \| "W" \| "E" \| "F"` | 级别，`F` 最严重 |
| `domain` | `string` | 模块号 `A001`~`A008`，见 §2.4 |
| `tag` | `string` | 模块名，如 `AgriSensor` |
| `zone` | `string \| null` | 逻辑设备 `A~F`；`"-"` = 整机级 |
| `seq` | `int \| null` | 该 zone 内单调递增序号，**丢包检测依据** |
| `fault` | `string \| null` | 故障码标签，如 `"F3002"`；普通日志为 `null` |
| `fault_name` | `string \| null` | 故障中文名，如 `"任务栈溢出"` |
| `detail` | `string \| null` | 错误码短语，如 `"stack_overflow"` |
| `raw` | `string` | **原始日志行**（保留它，用于"查看原文"） |
| `physical` | `object<string,string>` | 已格式化的物理量，键见 §2.3，值**已带单位** |

> ⚠️ `physical` 的值是**字符串**（`"28.3℃"`），只给人看。
> 要画图请从 `ts` 与 `/api/zones` 取数值，**不要解析 `physical` 字符串**。

### 2.2 日志行格式（理解数据来源用，前端不必解析）

```
000120.456789 I A001 AgriSensor: 1 24 2 281 3 466 4 101 z=A
└─ 设备ts ──┘ │  │    │          └─ "字段ID 值" 成对 ─┘ └zone┘
            级别 domain tag
```

* 消息体是 `<ID> <值>` 成对出现，**顺序无关**（所以加字段不破坏老解析器）。
* 行长硬上限 **128 字节**（`hilog_lite` 超长会**静默丢弃**）；`meta.longest_line`
  与 `meta.over_limit` 就是监控这一点的。

### 2.3 字段字典（`physical` 的键 / 日志里的数字 ID）

| ID | 名称 | 换算 | 单位 | 说明 |
|---|---|---|---|---|
| 1 | `seq` | 原值 | | zone 内单调递增，丢包检测 |
| 2 | `temp` | ÷10 | ℃ | 温度 |
| 3 | `humi` | ÷10 | % | 湿度 |
| 4 | `light` | 原值 | | 光照相对值 0~1000（AP3216C ALS） |
| 5 | `fault` | 查表 | | 故障码，见 §2.5 |
| 6 | `code` | 查表 | | 错误码，见 §2.6 |
| 7 | `tick` | 原值 | ms | 系统 tick |
| 8 | `heap` | 原值 | B | 剩余堆 |
| 9 | `cost` | 原值 | ms | 耗时 |
| 10 | `retry` | 原值 | | 重试 / 连续失败次数 |
| 11 | `state` | 查表 | | 0=idle 1=irrigating 2=venting 3=degraded |
| 12 | `thresh` | ÷10 | | 阈值 |
| 13 | `rssi` | 原值 | dBm | 存正值，实际取负 |
| 14 | `queue` | 原值 | | 消息队列长度 |
| 15 | `miss` | 原值 | | 看门狗漏喂次数 |
| 16 | `zone_id` | 原值 | | 0=A 1=B … 5=F |
| 17 | `mcause` | 查表 | | 机器异常原因码（0 指令地址非对齐 / 1 取指访问错 / 2 非法指令 / 4 Load 地址非对齐 / 5 Load 访问错 / 6 Store 地址非对齐 / 7 Store/AMO 访问错 / 15 Store 页错） |
| 18 | `addr` | 原值 | B | **出错地址 / mtval**（非法访问、踩内存时由硬件记录） |
| 19 | `cpu` | 原值 | % | CPU 占用率 0~100 |
| 20 | `memfail` | 原值 | | 内存池累计申请失败次数 |

> `addr` 与 `mcause` 会出现在**告警摘要**里（形如「在地址 0x1000 处（非法指令）」）。
> 前端若要展示摘要，请直接用 `/api/alerts` 已拼好的 `summary`，**不要自己重算** ——
> 否则会和报告里的说法不一致。

### 2.4 模块号（`domain`）

| domain | 模块 | 典型故障 |
|---|---|---|
| `A001` | 环境传感 | F1001~F1006 |
| `A002` | 数据上报 | F2002 |
| `A003` | 设备自检 | F3001 F3002 F3005 F3006 F5001~F5004 |
| `A004` | 执行器控制 | F4001 F4002 |
| `A005` | 任务调度 | F3003 F3004 F3011 |
| `A006` | 配网与通信 | F2001 F2003 F2004 F2005 |
| `A007` | 故障注入器 | （仅测试构建） |
| `A008` | 系统异常（看门狗/死机） | F3007~F3010 |

### 2.5 故障码（`fault` / `faults[].tag`）

| 标签 | 数值 | 期望级别 | 名称 | 正常事件？ |
|---|---|---|---|---|
| F1001 | 1001 | E | 传感器读取超时 | 否 |
| F1002 | 1002 | E | 单总线复位无应答 | 否 |
| F1003 | 1003 | E | 数据校验失败 | 否 |
| F1004 | 1004 | W | 读数超出合理范围 | 否 |
| F1005 | 1005 | W | 数值越阈值 | 否 |
| F1006 | 1006 | E | 传感器离线 | 否 |
| F2001 | 2001 | E | MQTT 连接失败 | 否 |
| F2002 | 2002 | E | 上报超时 | 否 |
| F2003 | 2003 | W | WiFi 信号弱 | 否 |
| F2004 | 2004 | W | 断线重连 | **是** |
| F2005 | 2005 | E | 数据帧校验错 | 否 |
| F3001 | 3001 | E | 内存分配失败 | 否 |
| F3002 | 3002 | E | 任务栈溢出 | 否 |
| F3003 | 3003 | E | 任务调度超时 | 否 |
| F3004 | 3004 | W | 队列积压 | 否 |
| F3005 | 3005 | E | 看门狗即将触发 | 否 |
| F3006 | 3006 | W | 空闲堆不足 | 否 |
| F3007 | 3007 | E | 看门狗超时复位 | 否 |
| F3008 | 3008 | E | 中断阻塞 | 否 |
| F3009 | 3009 | E | 系统死机 | 否 |
| F3010 | 3010 | E | 非法内存访问 | 否 |
| F3011 | 3011 | W | CPU 占用率过高 | 否 |
| F4001 | 4001 | W | 执行器超时 | 否 |
| F4002 | 4002 | I | 灌溉状态变更 | **是** |
| F5001 | 5001 | I | 设备启动 | **是** |
| F5002 | 5002 | W | 配置重载失败 | 否 |
| F5003 | 5003 | E | 异常复位 | 否 |
| F5004 | 5004 | W | 看门狗超时配置非法 | 否 |

> **「正常事件」以 `faults[].normal` 字段为准。** F2004 / F4002 / F5001 是正常运维事件，
> 前端若自己算异常 KPI **必须排除**（后端打分已排除，不排除就会和后端数字对不上）。

### 2.6 错误码（`detail`）

`0 none`、`1 reset_timeout`、`2 crc_mismatch`、`3 out_of_range`、`4 no_response_x5`、
`5 threshold_exceed`、`6 state_change`、`7 publish_timeout`、`8 rssi_low`、`9 reconnect`、
`10 stack_overflow`、`11 task_hung`、`12 alloc_failed`、`13 queue_full`、`14 wdt_feed_missed`、
`15 heap_low`、`16 config_load_failed`、`17 wdt_reset`、`18 isr_block`、`19 cpu_overload`、
`20 crash_task`、`21 crash_isr`、`22 bad_addr`、`23 mem_corrupt`、`24 abnormal_reset`、
`25 wdt_config_invalid`、`99 unknown`

---

## 3. 全部接口

### 3.0 一览

| # | 路径 | 用途 | 主要消费者 |
|---|---|---|---|
| 1 | `GET /` | 看板页面（`web/index.html`） | 浏览器 |
| 2 | `GET /api/meta` | 概览 + 运行形态 + 告警状态 | 顶栏徽标 / KPI |
| 3 | `GET /api/summary` | 级别 / 模块 / 设备 / 故障码分布 | 统计卡、饼图 |
| 4 | `GET /api/timeline` | 时间线 | 折线 / 柱状 |
| 5 | `GET /api/zones` | 环境曲线（温度/湿度/光照） | 曲线面板 |
| 6 | `GET /api/families` | **异常族**（智能聚合结果） | 异常族面板 |
| 7 | `GET /api/windows` | 窗口列表与异常分 | 异常时间线 |
| 8 | `GET /api/logs` | 日志明细（翻页 / 过滤） | 日志流（回放） |
| 9 | `GET /api/devices` | 设备选择器 | 顶栏下拉 |
| 10 | `GET /api/live/status` | 实时数据源状态 | ONLINE 徽标 |
| 11 | `GET /api/live/logs` | 实时增量日志 | 日志流（实时） |
| 12 | `GET /api/live/reconnect` | 强制重连串口 | 「重新连接」按钮 |
| 13 | `GET /api/analyze` | 启动分析 / 查进度 | 「生成根因报告」 |
| 14 | `GET /api/reports` | 报告列表 | 报告列表 |
| 15 | `GET /api/report` | 单份报告 Markdown | 报告详情 |
| 16 | `GET /api/alerts` | 告警状态 + 历史 | **告警面板（新）** |
| 17 | `GET /api/alerts/test` | 告警通道自测 | 设置 / 诊断（新） |
| 18 | `GET /api/alerts/reload` | 重读告警配置 | 设置 / 诊断（新） |
| 19 | `GET /api/shutdown` | 优雅停止 | 启动器（非页面） |

**通用查询参数 `zone`**（#3~#8 支持）：传 `A`~`F` 表示**只按该逻辑设备重算**全部派生视图
（窗口 / 异常 / 时间线 / 曲线）；传空或不传 = 全部设备。
**注意**：`zone` 会改变 `windows` / `families` / `anomaly_windows` 的内容，
不只是"过滤显示"——单设备视角下异常分不会被其它设备摊薄。

---

### 3.1 `GET /api/meta`

```json
{
  "log_path": "eval/dataset/d2_v2.log",
  "log_size_kb": 260.3,
  "records": 4475,
  "parse_ok": 4475,
  "parse_bad": 0,
  "parse_rate": 100.0,
  "parse_bad_reasons": {},
  "parse_bad_samples": [],
  "longest_line": 92,
  "over_limit": 0,
  "duration_s": 1800.0,
  "windows": 30,
  "anomaly_windows": 24,
  "load_ms": 41,
  "loaded_at": "2026-11-17 19:48:41",

  "live": { "enabled": false },
  "mode": "replay",
  "data_source": "静态文件回放",

  "scope": "ALL",
  "scope_name": "全部设备（6 个棚）",
  "device_mode": { "known": true, "injecting": true, "label": "故障注入中", "ts": 1795.0 },

  "alerts": {
    "enabled": true, "dry_run": false, "min_severity": "P2",
    "cooldown_s": 180.0, "max_per_hour": 12,
    "channels": ["console", "file"],
    "email_ready": false,
    "email_problem": "未启用（.env 里设 ALERT_EMAIL_ENABLED=1）",
    "webhook_ready": false,
    "smtp_host": "", "smtp_port": 465, "smtp_security": "ssl",
    "smtp_user": "", "email_to": [],
    "sent": 0, "failed": 0, "skipped": {},
    "last_error": "", "last_result": [],
    "describe": "告警 开启 ｜ 通道 控制台 / 文件 / Webhook(未配) / 邮件(未启用)",
    "auto_state": { "queued": 0, "done": 0, "skipped": 0, "error": 0 }
  }
}
```

| 字段 | 类型 | 前端用途 |
|---|---|---|
| `parse_rate` | `float` | 解析成功率，**≥99% 才达标**；低于 99% 建议黄色 |
| `over_limit` | `int` | 超 128 字节的行数，**>0 必须红色警告**（真机会静默丢弃） |
| `longest_line` | `int` | 最长行字节数，`≤128` 正常 |
| `parse_bad_samples` | `string[]` | 最多 5 条解析失败的原始行，是"为什么失败"的现场证据 |
| `loaded_at` | `string` | **主机**本地时间（不是设备时间） |
| `mode` | `"replay" \| "live"` | **运行形态的唯一判据** |
| `live` | `object` | 仅 `mode=="live"` 时有内容，见 §3.10 |
| `data_source` | `string` | 直接显示，如 `"真机实时"` / `"静态文件回放"` |
| `scope` / `scope_name` | `string` | 当前视角（`"ALL"` 或 `"A"`）与中文名 |
| `device_mode` | `object` | 固件是否在注入故障 |
| `alerts` | `object` | 告警通道就绪情况，见 §3.16 |

> `device_mode.known == false` 表示固件没上报 `AgriMode`（老固件 / 刚上电），
> 此时 `injecting` 为 `null`，UI 应显示「未知」而**不是**「未注入」。

---

### 3.2 `GET /api/summary?zone=`

```json
{
  "levels": { "D": 0, "I": 4200, "W": 200, "E": 60, "F": 15 },
  "domains": [ { "domain": "A001", "name": "环境传感", "count": 1800 } ],
  "zones":   [ { "zone": "A", "count": 745 } ],
  "tags":    [ { "tag": "AgriSensor", "count": 1500 } ],
  "faults":  [ { "tag": "F3005", "code": 3005, "name": "看门狗即将触发",
                 "count": 3, "normal": false } ]
}
```

* `levels` **恒含** `D/I/W/E/F` 五个键（缺失补 `0`）。
* `zones` 里会出现 `"-"`（整机级），**做设备选择器时要过滤掉**。
* `faults` **按次数降序**，且**包含**正常事件，靠 `normal` 区分。

---

### 3.3 `GET /api/timeline?zone=`

```json
[ { "t": 0, "I": 21, "W": 0, "E": 0, "step": 1 } ]
```

| 字段 | 说明 |
|---|---|
| `t` | 桶起始的**设备相对秒** |
| `I` / `W` / `E` | 该桶内 INFO / WARN / ERROR+FATAL 条数（`F` 已并入 `E`） |
| `step` | **桶宽（秒）**。总点数控制在 ~240 以内，可能 `step > 1` |

> `step` 是后端采样后的桶宽。画图时 x 轴按 `t` 定位、宽度按 `step`，
> **不要假设"一个点 = 一秒"**。界面上可写「每 N 秒聚合」。

---

### 3.4 `GET /api/zones?zone=`

**key 是 zone 字母**，值是该设备的三条曲线。

```json
{
  "A": {
    "name": "真实设备（板载 SHT20 + AP3216C）",
    "t":     [0.0, 10.0, 20.0],
    "temp":  [28.3, 28.4, null],
    "humi":  [46.2, 46.5, 46.8],
    "light": [101, 100, 99]
  }
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | `string` | 中文设备名。**单设备模式是「真实设备（板载 …）」**，多设备才是「1号棚·向阳主棚」 |
| `t` | `float[]` | 时间点（设备相对秒），**每 10 秒一个点**（后端已抽样） |
| `temp` | `(float\|null)[]` | ℃ |
| `humi` | `(float\|null)[]` | % |
| `light` | `(int\|null)[]` | 0~1000，**可能为 null** |

> ⚠️ 后端已剔除「故障报告行」和超量程值（例如注入器故意写的 `-127.0℃` 标记）。
> 所以**不要**把 `null` 当异常处理或补 `0` —— 断点应按「线段断开」画。

---

### 3.5 `GET /api/families?zone=` —— 智能聚合结果

把相似异常窗口聚成"异常族"。**这是「智能聚合」在界面上最值得展示的部分。**

```json
[
  {
    "key": "FW-1002-1004-3005",
    "codes": ["F1002", "F1004", "F3005"],
    "label": "单总线复位无应答",
    "count": 6,
    "windows": ["W0003", "W0007", "W0011"],
    "zones": ["A", "C"],
    "max_score": 0.61,
    "span": "120~420s",
    "err": 18,
    "warn": 9,
    "lines": 540
  }
]
```

| 字段 | 说明 |
|---|---|
| `key` | **聚合指纹**，程序算的、确定性、可复现。UI 建议标注「本族合并了 N 个窗口」 |
| `codes` | 该族涉及的故障码标签 |
| `label` | 给人看的名字（LLM 或字典给的）。**可能重名，去重请用 `key`** |
| `count` | 该族包含几个窗口 |
| `windows` | 窗口号列表，可点击跳到窗口详情 |
| `max_score` | 族内最高异常分 |
| `span` | 时间跨度（设备相对秒，字符串） |

> **排序**：已按 `count` 降序、`max_score` 降序。前端不要重排——顺序本身有含义。

---

### 3.6 `GET /api/windows?zone=`

```json
[
  {
    "id": "W0014",
    "start": 780.0, "end": 835.0,
    "lines": 150, "err": 5, "warn": 12, "info": 133,
    "score": 0.43,
    "gap_max": 5.2,
    "seq_gap": 0,
    "zones": ["A", "B", "C"],
    "faults": [ { "tag": "F3005", "name": "看门狗即将触发", "count": 1, "normal": false } ],
    "reasons": ["含 5 条 ERROR/FATAL", "异常故障码: F3005(看门狗即将触发)"],
    "anomaly": true
  }
]
```

| 字段 | 说明 |
|---|---|
| `score` | 异常分 `0~1`，**≥ 0.25 判为异常**（阈值可被 `--threshold` 改） |
| `gap_max` | 窗口内**最大日志到达间隔**（秒）。正常约 5s；`>10s` 说明可能任务卡死（**日志静默**） |
| `seq_gap` | 序号缺口总数（丢日志） |
| `anomaly` | 后端已算好的布尔值，**直接用它**，不要前端再比阈值 |
| `reasons` | 中文触发信号，直接展示（这是"可解释性"的落点） |

---

### 3.7 `GET /api/logs?level=&tag=&zone=&q=&limit=&offset=`

| 参数 | 说明 |
|---|---|
| `level` | 级别**下限**：传 `W` 表示 W/E/F（`D < I < W < E < F`） |
| `tag` | 精确匹配模块名 |
| `zone` | 精确匹配逻辑设备 |
| `q` | **正则**，对 `raw` 原始行做不区分大小写搜索 |
| `limit` | 默认 `300`，**上限 2000** |
| `offset` | 默认 `0` |

```json
{
  "total": 4475, "offset": 0, "limit": 300,
  "items": [
    { "eid": "hl:00000001", "ts": 0.0, "level": "I",
      "domain": "A001", "tag": "AgriSensor", "zone": "A",
      "seq": 21, "fault": null, "fault_name": null, "detail": null,
      "raw": "000000.000000 I A001 AgriSensor: 1 21 2 307 3 469 4 101 z=A",
      "physical": { "seq": "21", "temp": "30.7℃", "humi": "46.9%", "light": "101" } }
  ]
}
```

> `q` 是**正则**不是纯文本。前端应做输入转义或明确提示「支持正则」。

---

### 3.8 `GET /api/devices`

```json
[ { "zone": "A", "count": 745, "name": "真实设备（板载 SHT20 + AP3216C）" } ]
```

* 已过滤掉 `zone == "-"`（整机级）。
* 单设备模式下**只有一项** —— 此时建议**隐藏**选择器，避免只有单个选项的下拉框。

---

### 3.9 `GET /api/live/logs?since=<int>`

**实时模式专用的增量拉取**。`since` = 你已收到的记录条数（游标）。

```json
{
  "next": 120,
  "online": true,
  "error": "",
  "silent_s": 0.4,
  "epoch": 0,
  "reset": false,
  "items": [ /* 结构同 §3.7 的 items */ ]
}
```

| 字段 | 说明 |
|---|---|
| `next` | **下次传的 `since`**。直接用返回的这个值，**不要自己 `+items.length`** |
| `epoch` | **数据流世代号**。变了说明后端重启过 / 清空过数据 |
| `reset` | `true` = **你的游标作废**，请清空日志列表并把 `since` 设为 `0` |
| `online` | 是否正在收到数据（后端以「6 秒内收到过」判定） |
| `silent_s` | 已沉默多少秒 |
| `error` | 中文原因，**离线时必定非空**，直接显示给用户 |

> ★ **必须处理 `reset` 与 `epoch`**。项目踩过的坑：看板重启后记录数组从 0 重新计数，
> 而前端还攥着重启前的游标（如 53）。此时若前端"老实"把游标改成更小的 `next`，
> 等记录再涨过 53 就会**把显示过的行再发一遍**，界面上出现**重复日志**。
>
> 正确写法：
> ```js
> if (resp.reset || resp.epoch !== myEpoch) {
>   clearLogList();
>   since = 0;
>   myEpoch = resp.epoch;
> } else {
>   since = resp.next;
> }
> ```

---

### 3.10 `GET /api/live/status`

未启用实时模式：`{ "enabled": false }`（**只有这一个键**，注意判空）。

启用后：

```json
{
  "enabled": true,
  "source": "COM3",
  "online": true,
  "lines": 320,
  "started": 1763384921.2,
  "last_recv": 1763384950.8,
  "error": "",
  "sim": false,
  "attempt": 1,
  "reconnects": 0,
  "cmd": "python serial_capture.py --port auto ...",
  "pid": 12345,
  "uptime_s": 29.6,
  "silent_s": 0.4
}
```

| 字段 | 前端用途 |
|---|---|
| `online` | 绿色跳动徽标 / 红色 OFFLINE |
| `silent_s` | 「已 N 秒未收到数据」 |
| `error` | **中文解释**，离线时必看。后端会区分「从没收到过」与「收到了但停了」 |
| `sim` | `true` 时界面**必须标注**「模拟器数据源」，否则会被误当成真机 |
| `reconnects` | 重连次数 |
| `started` / `last_recv` | **主机 Unix 时间戳**（浮点），不是设备时间 |

> `sim: true` 必须醒目提示。这是现场演示最容易被误解的点之一。

---

### 3.11 `GET /api/live/reconnect`

```json
{ "ok": true, "source": "COM4", "reconnects": 2 }
```

非实时模式：`{ "ok": false, "reason": "当前不是实时模式" }`

> 它会**保留已收到的记录**（不清空）。调用后可再拉一次 `/api/live/logs?since=<当前游标>` 同步。

---

### 3.12 `GET /api/analyze`

**启动分析**（不带 `action`）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `zone` | `""` | 只分析某个设备；空 = 全部 |
| `max` | `2` | 最多出几份报告，**上限 6** |
| `no_llm` | `0` | `1` = 强制走规则引擎（**答辩断网时的保底方案**） |

```json
{ "started": true, "zone": "ALL" }
```

已在跑 / 没数据：

```json
{ "started": false, "reason": "已有分析在跑" }
```

**查进度**（`?action=status`）：

```json
{
  "running": true, "done": false, "error": "",
  "started": 1763384921.2, "finished": 0, "elapsed_s": 12.3,
  "zone": "ALL", "zone_name": "全部设备（6 个棚）",
  "records": 4475, "windows": 30, "anomalies": 24,
  "reports": ["W0014_看门狗即将触发.md"],
  "log": ["19:48:49  分析范围：…", "19:48:49  窗口 30 个…"],
  "use_llm": true
}
```

| 字段 | 说明 |
|---|---|
| `log` | **最近 40 行**中文进度日志，直接滚动显示 —— 这是「分析真的在动」的最好证据 |
| `reports` | 本次生成的报告文件名，点击看 §3.14 |
| `anomalies == 0` | 此时**仍会生成一份「健康报告」**（`reports` 非空），UI 不要以为失败了 |

---

### 3.13 `GET /api/reports`

```json
[
  { "name": "W0014_看门狗即将触发.md",
    "dir": "reports_live",
    "source": "真机实测",
    "source_kind": "live",
    "kb": 6.4,
    "mtime": "11-17 19:48",
    "age_s": 62.3 }
]
```

| 字段 | 说明 |
|---|---|
| `source` | 中文来源，**直接显示** |
| `source_kind` | `live`=真机实时 · `sim`=模拟数据集 · `other`=历史样例。**必须显示来源标注**，不能让人误以为所有结论都来自真机 |
| `age_s` | 距今秒数，用于「x 分钟前」 |

排序已按修改时间**倒序**。

---

### 3.14 `GET /api/report?name=<filename>`

```json
{ "name": "W0014_看门狗即将触发.md", "markdown": "# …" }
```

不存在：`404 { "error": "报告不存在" }`

> `markdown` 是 **Markdown 原文**，由前端渲染。报告结构固定为：
> 「标题 → **一句话摘要（引用行）** → 元信息表 → 一、现象 → 二、证据 → 三、疑似根因
> → 四、影响 → 五、处置建议」。
>
> 其中**证据段由程序从原始日志取出，不经过模型**，每条都带 `eid`，可逐条回查。
> 报告表格里会标明「分析引擎：LLM 语义分析 / 本地规则引擎（降级）」——
> **这个标注不要隐藏**，它关系到结论可信度。

---

### 3.15 `GET /api/alerts?limit=<int>`

告警状态 + 最近告警（默认 `50`，**上限 200**）。

```json
{
  "status": { "enabled": true, "channels": ["console","file"], "sent": 2, "failed": 0,
              "email_ready": false, "email_problem": "…", "describe": "…" },
  "items": [
    {
      "alert_id": "A-W0014-6aa752ee",
      "created_at": "2026-11-17 19:48:49",
      "severity": "P1",
      "title": "W0014 异常（规则引擎判定）",
      "summary": "检测到全部 6 个设备发生看门狗即将触发（F3005），疑似阻塞导致进不了空闲任务喂狗。",
      "window_id": "W0014",
      "cluster_key": "FW-1002-1004-2001-2002-3001-3003-3004-3005-3006-4001",
      "device": "全部 6 个设备",
      "device_name": "全部设备（6 个棚）",
      "span": "780s ~ 835s",
      "score": 0.43,
      "mode": "rule",
      "source": "eval\\dataset\\d2_v2.log",
      "report_file": "W0014_看门狗即将触发.md",
      "reasons": ["含 5 条 ERROR/FATAL"],
      "evidence": ["[hl:00001953] 000780.000000 E A003 AgriHealth: 1 157 5 3005 6 14 15 2 z=D"],
      "extra": { "lines": 150, "err": 5, "faults": { "F3005": 1 } },
      "delivered": true,
      "channels": [ { "channel": "console", "ok": true, "skipped": false, "detail": "" } ],
      "skip_reason": ""
    }
  ]
}
```

**告警是前端最需要"设计"的一块，重点看这几个字段：**

| 字段 | 怎么用 |
|---|---|
| `summary` | ★ **一句话可读摘要**（LLM 生成或规则兜底）。这就是「智能告警」的展示核心，**直接大字号显示，不要自己拼** |
| `severity` | `P0` 红 / `P1` 橙 / `P2` 黄 / `P3` 灰。**配色请按此映射** |
| `delivered` | `true`=真的推出了；`false`=被策略拦下（冷却 / 限流 / 低级别），原因见 `skip_reason` |
| `channels` | 每通道结果。`skipped: true` 表示「演练模式未实际发送」 |
| `skip_reason` | 中文，**离线排查就靠它**。例：「同一异常族距上次推送仅 40s（冷却 180s）」 |
| `report_file` | 非空时可点击跳到 §3.14 |
| `mode` | `llm`=LLM 语义分析，`rule`=本地规则引擎降级。**必须显示**（答辩诚实性） |
| `alert_id` | 幂等键。同一窗口重新分析会产生**相同** `alert_id`，可用来做前端去重 |

> **历史是内存态**：后端重启后 `items` 从空开始。要长期留存请读 `logs/alerts.jsonl`
> （每行一个 JSON，结构与 `items[]` 完全一致，由 `FileChannel` 追加）。

---

### 3.16 `GET /api/alerts/test?channel=&force=`

发一条**明显是自测**的告警，验证通道能否送达。

| 参数 | 说明 |
|---|---|
| `channel` | `email` / `webhook` / `file` / `console`；不传 = 全部通道 |
| `force` | 保留参数。自测**始终绕过**去重 / 冷却 / 限流 |

```json
{
  "ok": true,
  "test": true,
  "alert": { "alert_id": "A-TEST-20261117194845", "severity": "P3",
             "title": "【自测】告警通道连通性测试",
             "summary": "这是一条手动触发的测试告警…" },
  "channels": [ { "channel": "file", "ok": true, "skipped": false, "detail": "alerts.jsonl" } ],
  "reason": ""
}
```

通道不存在：

```json
{ "ok": false, "test": true,
  "reason": "没有该通道：email（当前可用：['console', 'file']）",
  "alert": { }, "channels": [] }
```

> 这是**唯一会产生副作用（发邮件 / 发 HTTP）的 GET 接口**。
> UI 上必须放在「设置 / 诊断」里，**不能做成首屏按钮**。

---

### 3.17 `GET /api/alerts/reload`

改完 `.env` 后热加载告警配置（**不用重启看板**）。

```json
{ "ok": true, "status": { /* 同 §3.1 meta.alerts */ } }
```

---

### 3.18 `GET /api/shutdown`

优雅停止：**先停采集子进程释放串口，再关 HTTP 服务**。

```json
{ "ok": true, "msg": "采集已停止，串口已释放" }
```

> 供启动器（`tools/launcher.py`）的「停止」按钮调用。**不要用 taskkill 杀看板**：
> 采集器是看板的子进程，粗暴杀父进程会留下孤儿进程继续占着串口，
> 现象是「看板关了，DevEco 还是打不开串口 / 报 upload port is occupied」。

---

## 4. 前端实现检查清单

逐项打勾，能避开本项目已经踩过的全部坑：

- [ ] **`meta.mode`** 驱动三套文案（回放 / 实时 / 离线），不靠猜
- [ ] 实时模式显示 **`sim`** 标记（模拟器数据源必须标注）
- [ ] `/api/live/logs` 处理 **`reset` 与 `epoch`**，否则日志会重复显示
- [ ] `/api/live/logs` 的游标用响应里的 **`next`**，不要自己累加
- [ ] 曲线里的 **`null`** 当断点，不补 `0`、不断言异常
- [ ] `/api/timeline` 的 **`step`** 用于 x 轴宽度，不假设 1 秒 1 点
- [ ] 异常计数**排除 `normal: true`** 的故障码
- [ ] `device_mode.known == false` 时显示「未知」
- [ ] 告警配色按 `P0~P3`，并显示 `mode`（LLM / 规则降级）
- [ ] 告警 `delivered: false` 时把 `skip_reason` 显示出来（这是排查入口）
- [ ] 所有 `fetch` 包 try/catch，失败时保留旧数据 + 顶部提示条
- [ ] 单设备模式下**隐藏**设备选择器
- [ ] 文案不写「6 个传感器」，写「6 个逻辑设备 / 大棚」
- [ ] 报告列表按 `source_kind` 配色，不让人误以为结论都来自真机

---

## 5. 配套前端资源（别自己造轮子）

后端已经把"前端要用的东西"做成了三个文件，**引入即可**：

| 文件 | 作用 | 大小 |
|---|---|---|
| `web/api-contract.js` | ★ **契约单一来源**：19 个接口清单、28 个故障码、20 个字段、8 个 domain、级别/严重度配色、轮询间隔、报告章节结构、文案 | ~20 KB |
| `web/api-client.js` | 取数封装：`get()` / `poll()` / `streamLiveLogs()` / `waitAnalyze()`，内置真实↔离线自动切换 | ~13 KB |
| `web/samples/*.json` | **真实后端响应样例**（15 个数据文件 + manifest，含 LLM 生成的报告与告警），没有 Python 后端也能开发 | ~100 KB |

```html
<script src="/api-contract.js"></script>
<script src="/api-client.js"></script>
<script>
  const meta = await AGRILOG_CLIENT.get('meta');   // 自动选真实接口或离线样例
</script>
```

**契约与后端代码的一致性由程序校验**：`web/api-contract.js` 里的 28 个故障码、
20 个字段、8 个 domain、27 个错误码、4 个状态机取值，与
`agri_logger/parser/fields.py` **逐项严格比对通过**（含 `normal` 正常事件标记与 `scale` 缩放）。

> 「一键更换」的三种用法（换数据源 / 换主题 / 换文案）见 `docs/16` §3。
> 一句话：**改 `dataSource.mode` 就换数据源，改 `theme` 就换配色，都不需要碰业务代码。**

---

## 6. 变更流程

1. 后端要加 / 改字段 → **先改本文档**，再改 `tools/dashboard_server.py`。
2. 新增字段**只增不改**：不得改变已有字段的名字、类型、单位。
3. 确实要改语义 → 新增字段（如 `score_v2`），旧字段保留一个版本周期。
4. 每次变更在下面记一行。

### 变更记录

| 日期 | 变更 | 影响 |
|---|---|---|
| 2026-11-17 | 首版：把散落在代码里的响应结构整理成契约 | 无（纯文档） |
| 2026-11-17 | 新增 `meta.alerts`、`/api/alerts`、`/api/alerts/test`、`/api/alerts/reload` | 纯新增，向后兼容 |
| 2026-11-17 | 硬件口径改为「一块板 = 一个传感器节点」，6 个 zone 是逻辑设备 | 仅文案，字段不变 |
| 2026-11-17 | `physical["state"]` 由裸数字（`"3"`）改为状态机文字（`"degraded"`），与本文 §2.3 一致 | **值的形态变了**：前端若用 `physical.state === "3"` 需改成比对文字（或改用 `values`，但接口不返回 `values`） |
| 2026-11-17 | 报告"涉及设备"过滤掉 `-`（整机级标记） | 报告文本更准确：`A、B、…` 而非 `-、A、B、…` |
| 2026-11-17 | 新增 `web/api-contract.js` / `web/api-client.js` / `web/samples/`，并补充 `docs/15`、`docs/16` | 纯新增 |

---

*本规范与代码同源：字段名以本文档为准，取值以 `tools/dashboard_server.py` 为准。*
