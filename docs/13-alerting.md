# 告警通道与邮件通知（策略 · 配置 · 可行性论证）

> **这份文档回答两个问题**：
> ① 检测到异常后，告警是怎么发出去的？（实现）
> ② 在现有基础上做「邮箱通知」可行吗？难点在哪？（可行性论证）
>
> 代码：`agri_logger/notifier/`（四个文件）+ `tools/dashboard_server.py` 的
> `_dispatch_alert()` 与 `/api/alerts*` 接口。
> 接口字段见 `docs/14-api-interface-spec.md` §3.15~§3.17。

---

## 1. 需求原文与我们的落地方式

> 题目要求：「智能告警与报告生成：当检测到异常模式时，通过 LLM 生成一段可读性强的摘要描述
> （如"检测到 TaskA 在 0x1000 地址处发生栈溢出，疑似递归调用过深"），支持告警推送或者邮件通知。」

拆成三件必须做的事：

| 要求 | 我们怎么做 | 落在哪个文件 |
|---|---|---|
| 检测到异常模式 | 60 秒窗口聚合 + 5 信号加权打分，`score ≥ 0.25` 判异常 | `agri_logger/pipeline/windows.py` |
| LLM 生成可读摘要 | L2 提示词新增 `alert_summary` 字段（**同一次调用，不额外花钱**） | `agri_logger/analyzer/rca.py` |
| 告警推送 / 邮件通知 | 四通道：控制台、文件（JSONL 审计）、Webhook、SMTP 邮件 | `agri_logger/notifier/` |

### 1.1 摘要长什么样

需求给的例子是「检测到 TaskA 在 0x1000 地址处发生栈溢出，疑似递归调用过深」。
这句话能这么说，前提是**日志里真有任务名和出错地址**。我们实际能拿到的字段是：

| 例子里的成分 | 我们的数据来源 | 说明 |
|---|---|---|
| `TaskA` | 没有任务名字段 → 改用 **逻辑设备（zone）+ 模块 tag**（如 `AgriHealth`） | 不编造 |
| `0x1000` | **字段 18 `addr`（mtval）** | 有就写，没有就整段省略 |
| `栈溢出` | **故障码 F3002 的字典名** | 确定性，不靠模型猜 |
| `疑似递归调用过深` | **故障码 → 成因映射表**（规则）或 **LLM 的 `suspected_root_cause`** | 两级保障 |

后端生成的真实样例（`python -m agri_logger.notifier --dry-run` 的输出，未改一个字）：

```
检测到设备 A 在地址 0x1000 处（非法指令）发生系统死机（F3009），
疑似野指针、栈溢出写坏内存，或执行了非法指令。
```

> 注意 `（非法指令）` 来自 `mcause=2`。**地址与原因码都是日志里真实存在的字节**，
> 不是模型编的 —— 这正是防幻觉架构在告警层的延伸。

### 1.2 为什么摘要要有「规则兜底」

LLM 可能不可用（没配 Key、断网、额度耗尽、答辩现场 WiFi 挂了）。
如果摘要只由 LLM 生成，那么**最需要告警的时候恰好没有告警**。

所以摘要走两级：

```
rca["alert_summary"]（LLM 输出）
      ↓ 为空时
analysis.summary（L1 输出）
      ↓ 仍为空时
analyzer/summary.py: one_line_alert()（纯规则，确定性）
```

三处共用 `analyzer/summary.py` 的措辞逻辑，**保证邮件、报告、看板说的是同一句话**。
这一点是刻意设计的：如果各写一份，迟早出现「邮件说 A、报告说 B」。

---

## 2. 告警链路

```
异常窗口 (WindowStats, score ≥ 0.25)
        │
        │ ① 报告生成（LLM 或规则降级）
        ▼
   build_alert()  ──▶  AlertEvent（alert_id / severity / title / summary / evidence …）
        │
        │ ② 策略层 AlertPolicy
        │     · 总开关
        │     · 级别门槛（默认 P2）
        │     · alert_id 去重
        │     · 聚合指纹冷却（默认 180s）
        │     · 每小时限流（默认 12 条）
        ▼
   通道分发
   ┌────────┬────────┬──────────┬─────────┐
   ▼        ▼        ▼          ▼
控制台    文件      Webhook    邮件(SMTP)
（零配置）（JSONL） （钉钉/飞书）（可选）
```

### 2.1 触发点（什么时候发）

| 触发场景 | 代码位置 | 说明 |
|---|---|---|
| **实时模式自动分析** | `dashboard_server._auto_report_one()` | 检测到新的已闭合异常窗口 → 出报告 → 推告警。**这是主路径** |
| **手动生成根因报告** | `dashboard_server._analyze_worker()` | 点按钮触发的分析同样推送；同一窗口会被 `alert_id` 去重，**不会重复发信** |
| **无异常时的健康报告** | `dashboard_server._write_health_report()` | 级别 `P3`，默认**只写审计文件不打扰人** |
| **通道自测** | `GET /api/alerts/test` | 合成告警，跳过策略，只验通道 |

> 自动分析不依赖浏览器是否开着：`_refresh_loop` 每 2 秒独立驱动，
> 前端关掉照样会检测并推送。

### 2.2 告警事件结构（`AlertEvent`）

| 字段 | 说明 |
|---|---|
| `alert_id` | **确定性**：`A-<窗口号>-<指纹sha1前8位>`。同一窗口重新分析得到同一个 id → 天然幂等 |
| `severity` | `P0`/`P1`/`P2`/`P3` |
| `title` | 报告标题（≤30 字，含设备与现象） |
| `summary` | ★ **一句话可读摘要**（LLM 优先，规则兜底） |
| `device` / `device_name` | 逻辑设备与中文名 |
| `span` | 时间范围（设备相对秒） |
| `score` | 异常分 |
| `mode` | `llm` / `rule`（**降级必须可见**） |
| `source` | 数据来源（真机串口 / 文件路径） |
| `report_file` | 关联报告文件名（邮件里作为附件） |
| `reasons` | 触发信号列表（中文） |
| `evidence` | 证据日志原文（含 `eid`，可回查） |

### 2.3 策略参数（全部可用环境变量覆盖）

| 参数 | 默认 | 环境变量 | 为什么这么定 |
|---|---|---|---|
| 推送门槛 | `P2` | `ALERT_MIN_SEVERITY` | P3（健康报告）不该打扰人 |
| 冷却 | `180s` | `ALERT_COOLDOWN_S` | 一次故障持续 10 分钟 = 10 个窗口，不去重就是 10 封邮件 |
| 每小时上限 | `12` | `ALERT_MAX_PER_HOUR` | 防雪崩把邮箱配额 / 额度打光 |
| 演练模式 | 关 | `ALERT_DRY_RUN=1` | 答辩前排练时只看会发什么，不真发 |

> **告警疲劳是真实风险**：一个持续 30 分钟的故障会覆盖 30 个窗口。
> 如果每个窗口都发一封，人的第一反应是"把它设成垃圾邮件"——那这套告警就白做了。
> 所以去重 / 冷却 / 限流不是"锦上添花"，是让告警**持续可用**的前提。

---

## 3. 四个通道

| 通道 | 依赖 | 配置量 | 适合场景 |
|---|---|---|---|
| **控制台** | 无 | 零 | 本地演示、调试 |
| **文件** `logs/alerts.jsonl` | 无 | 零 | **审计留痕**，也是看板历史的持久化来源 |
| **Webhook** | `urllib`（标准库） | 一个 URL | 钉钉 / 飞书 / 企业微信 / 自建 HTTP |
| **邮件 SMTP** | `smtplib`（标准库） | SMTP 主机 + 授权码 + 收件人 | 正式通知、答辩演示"邮件真的到了" |

**全部零第三方依赖**。这是刻意的：项目 `requirements.txt` 只有 `pyserial` 和 `openai`，
告警恰恰是最需要「克隆下来就能跑」的部分，多一个依赖就多一类装不上的事故。

### 3.1 通道纪律

`Channel.send()` **永不抛异常**，统一返回 `ChannelResult(ok, skipped, detail)`。
邮箱密码写错、SMTP 连不上、Webhook 404 —— 都只是**这一条告警标红**，
分析主链路照常运行。告警是锦上添花，采集与分析才是主链路。

### 3.2 邮件正文与附件

* 正文纯文本（任何客户端都不乱码），顺序按「运维在手机上先看什么」排：
  **摘要 → 关键字段 → 触发信号 → 证据日志 → 报告附件说明**。
* 完整报告（Markdown）作为**附件**：正文保持简短可读，要细节的人点开附件，
  不用在手机小屏上翻长报告。
* 主题格式：`[P1] 报告标题`，中文主题显式按 UTF-8 编码（`email.header.Header`）。
* `X-AgriLog-Alert-Id` 头带上 `alert_id`，便于在邮箱里按线程/去重。

---

## 4. ★ 邮箱通知可行性论证

**结论先说：技术上完全可行，且已实现并可自测；真正的风险不在代码，在「SMTP 配置」和「现场网络」。**

### 4.1 可行性拆解

| 维度 | 评估 | 说明 |
|---|---|---|
| **技术可行性** | ✅ **已实现** | `smtplib` + `email` 均为标准库，**零新增依赖** |
| **成本** | ✅ **0 元** | 用 QQ / 163 / 企业邮箱自带的 SMTP，免费额度对演示足够 |
| **实现复杂度** | ✅ 低 | 约 120 行（`channels.EmailChannel`），含 SSL/STARTTLS 双路回退 |
| **可验证性** | ✅ 好 | `/api/alerts/test?channel=email` 一键验证，不用等真故障 |
| **离线可用性** | ⚠️ **取决于网络** | 邮件必然出网。断网时自动降级到控制台 + 文件，不阻塞主链路 |
| **到达时效** | ⚠️ **秒级，但不保证 3 秒** | 见 §4.5 |
| **稳定性** | ⚠️ 中 | 受邮箱服务商频率限制、垃圾邮件策略、校园网/企业网出口策略影响 |

### 4.2 需要的配置（QQ 邮箱为例）

`.env` 里加这几行（`run.ps1` 会自动注入环境变量）：

```ini
ALERT_EMAIL_ENABLED=1
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_SECURITY=ssl
SMTP_USER=你的QQ号@qq.com
SMTP_PASSWORD=授权码           # ★ 不是 QQ 登录密码
ALERT_EMAIL_FROM=你的QQ号@qq.com
ALERT_EMAIL_TO=收件人1@example.com,收件人2@example.com
```

**拿授权码的步骤**（QQ 邮箱）：设置 → 账号 → 「POP3/IMAP/SMTP 服务」→ 开启
「IMAP/SMTP 服务」→ 按提示发短信 → 得到 16 位**授权码**。

> 常见服务商速查：
>
> | 邮箱 | SMTP 主机 | 端口 / 加密 |
> |---|---|---|
> | QQ 邮箱 | `smtp.qq.com` | `465` / SSL，或 `587` / STARTTLS |
> | 163 邮箱 | `smtp.163.com` | `465` / SSL |
> | 阿里企业邮 | `smtp.mxhichina.com` | `465` / SSL |
> | Gmail | `smtp.gmail.com` | `465` / SSL（需应用专用密码，且国内网络通常不通） |

### 4.3 已知的坑（**这是可行性论证最需要的部分**）

这些是 SMTP 通知里 90% 的失败原因，我们**在代码里已经做了针对性处理**：

| 坑 | 现象 | 我们的处理 |
|---|---|---|
| **把登录密码当授权码** | `535 Login Fail` / `Authorization failed` | 报错原文写进 `detail`，并在配置体检里明确提示「QV/163 填授权码，不是登录密码」 |
| **端口与加密方式不配** | `ConnectionRefused` / `SSL: WRONG_VERSION_NUMBER` / 卡住超时 | **自动回退**：`465/SSL` ↔ `587/STARTTLS` 各试一次 |
| **发件人与认证用户不一致** | `553 Mail from must equal authorized user` | 发送时用「认证用户」作为 `MAIL FROM`；`email_from` 留空则自动取 `SMTP_USER` |
| **25 端口被云厂商封锁** | 连接超时 | 默认就用 465，不碰 25 |
| **中文主题乱码** | 主题变 `=?utf-8?` 或乱码 | 显式 `Header(subject, "utf-8")` |
| **中文附件名变成 `attach.bin`** | 附件名丢失 | RFC 2231 编码：`filename=("utf-8","",name)` |
| **校园网 / 企业网禁出 465** | 全部超时 | 降级到本地通道；或改用 Webhook（走 443，几乎不被封） |
| **被判定为垃圾邮件** | 收不到，但服务端返回成功 | 演示前先发给**自己**确认；把发件人加入白名单 |
| **频率限制** | 短时间大量发送被临时拒绝 | 策略层限流（每小时 12 条）+ 冷却（180s）已经挡住 |
| **配错后每封都卡 30 秒** | 告警很慢、报告生成也被拖慢 | **失败退避**：邮件失败后 120 秒内跳过邮件（只走其它通道），修好配置后自动恢复；`skip_detail` 里带着上次的具体错误 |
| **`SMTP_PASSWORD` 泄露进 Git** | 安全事故 | 配置只写 `.env`，`.env` 在 `.gitignore` 里；代码里**没有任何硬编码凭据** |

### 4.4 零配置也能跑（降级设计）

`AlertConfig` 的默认行为：

* **不配任何东西** → 控制台 + 文件两通道可用，`/api/meta` 里
  `email_ready: false`、`email_problem: "未启用（…）"`，**不是错误状态**。
* **只配了 `SMTP_HOST` 和 `ALERT_EMAIL_TO`（忘开开关）** → 自动视为启用，避免"配了不生效"。
* **配错** → 每次告警的 `channels[].detail` 里带**具体原因**（端口、认证、超时各不同），
  不抛异常。

这让「邮箱通知」成为**可选增强而不是硬依赖** —— 答辩现场即使校园网禁 SMTP，
降级路径也能完整演示告警（控制台红字 + `alerts.jsonl` + 看板告警面板）。

### 4.5 关于「告警 3 秒内到达」这条验收指标

`README.md` §5 写的是「告警 3 秒内到达邮件/Webhook」。必须诚实区分两段耗时：

```
检测（窗口闭合） → 分析（LLM 调用）→ 推送（SMTP/HTTP）→ 到达
   ≈ 实时        ←─  3~20 秒  ─→       ←─ 0.5~3 秒 ─→
```

* **`分析` 是主要耗时**：一次 L1+L2 调用实测 3~20 秒（受模型、思考模式、网络影响）。
* **推送本身很快**：SMTP/HTTP 握手通常 0.5~3 秒。

因此正确的表述是：**「检测到异常后，推送在秒级完成；端到端（含 LLM 分析）约 5~25 秒」**。
把端到端说成 3 秒，答辩时被追问会很被动。

**如何把端到端压到 3 秒以内**（如果一定要）：把告警从 L2 里解耦——
异常分数超过阈值时**先用规则摘要立即推送**（P0/P1），LLM 报告生成后再推一条"报告已就绪"。
这条路径**当前未实现**，但 `summary.one_line_alert()` 已经是现成的规则摘要，
改造成本约几十行（在 `_scan_new_anomalies()` 里先推规则告警）。

### 4.6 更稳的替代/补充：Webhook

如果现场网络对 SMTP 不友好，**Webhook 是更可靠的选择**：

* 走 `443/HTTPS`，绝大多数网络都放行；
* 钉钉 / 飞书 / 企业微信群机器人都是「复制一个 URL」即可；
* 结构化 JSON 便于二次处理。

把机器人 URL 原样填到 `ALERT_WEBHOOK_URL` 即可，`ALERT_WEBHOOK_KIND` 默认 `auto`，
会按域名自动识别钉钉 / 飞书 / 企业微信的负载格式（三家的字段名并不一样：
钉钉/企业微信是 `msgtype + text.content`，飞书是 `msg_type + content.text`）。
自建接口或反向代理把域名改掉了，就显式设 `ALERT_WEBHOOK_KIND=dingtalk|feishu|wecom|generic`。

> 注意：这三家机器人在「HTTP 200」里也可能返回 `errcode/code != 0`（关键词不匹配、
> IP 白名单没加、机器人被停用）。本项目会继续检查业务码，不会把假成功显示成已发送。

代价：需要群里有一个机器人，且消息是"群消息"而非"私人邮件"。
**建议两个都配**：邮件给"正式留痕"，Webhook 给"实时响铃"。

### 4.7 可行性总评

| 问题 | 回答 |
|---|---|
| 能不能做？ | **能，已经做完了**（`agri_logger/notifier/`） |
| 要花多少钱？ | 0 元（免费 SMTP 额度足够） |
| 要装什么依赖？ | **不用装**，标准库 |
| 最大的风险是什么？ | **现场网络能不能出 SMTP**，以及**授权码配错** |
| 怎么把风险降到最低？ | ① 提前用 `/api/alerts/test?channel=email` 验证；② 同时配 Webhook；③ 保留控制台 + 文件降级 |
| 答辩怎么说？ | 「告警支持四通道，邮件走 SMTP 且已实测到达；断网时自动降级到本地审计，不影响监测链路」 |

---

## 5. 配置清单（全部环境变量）

```ini
# ---- 总开关与策略 ----
ALERT_ENABLED=1                 # 0 = 全局关闭
ALERT_MIN_SEVERITY=P2           # 低于此级别只记审计文件，不推送
ALERT_DRY_RUN=0                 # 1 = 演练：只显示会发什么，不真发
ALERT_COOLDOWN_S=180            # 同一异常族冷却秒数
ALERT_MAX_PER_HOUR=12           # 每小时最多推送条数

# ---- 控制台 / 文件 ----
ALERT_CONSOLE=1
ALERT_FILE_ENABLED=1
ALERT_FILE=logs/alerts.jsonl

# ---- Webhook（钉钉/飞书/企业微信/自建）----
ALERT_WEBHOOK_URL=
ALERT_WEBHOOK_TIMEOUT_S=8
ALERT_WEBHOOK_KIND=auto         # auto | dingtalk | feishu | wecom | generic

# ---- 邮件（SMTP）----
ALERT_EMAIL_ENABLED=0
SMTP_HOST=
SMTP_PORT=465
SMTP_SECURITY=ssl               # ssl | starttls | none
SMTP_USER=
SMTP_PASSWORD=                  # ★ 授权码，不是登录密码
ALERT_EMAIL_FROM=               # 留空 = 用 SMTP_USER
ALERT_EMAIL_TO=                 # 逗号分隔
SMTP_TIMEOUT_S=15
```

> 密钥扫描：`tools/secret_scan.py` 已把 `.env` 列入白名单（不提交），
> `SMTP_PASSWORD` 只存在于本地 `.env`。

---

## 6. 运行与验证（三条命令，不需要开发板）

```powershell
# ① 只看配置体检（不发任何东西）
.venv\Scripts\python.exe -m agri_logger.notifier --show-config

# ② 演练：用真实解析器造一条 P1 告警，走完整链路但只显示不发
.venv\Scripts\python.exe -m agri_logger.notifier --dry-run

# ③ 只验证邮件通道（配好 .env 之后跑这条）
.venv\Scripts\python.exe -m agri_logger.notifier --email-only
```

看板上也可以验：

```
http://127.0.0.1:8080/api/alerts/test?channel=email     # 发一封测试邮件
http://127.0.0.1:8080/api/alerts                        # 看历史与每通道结果
http://127.0.0.1:8080/api/alerts/reload                 # 改完 .env 免重启
```

**验收判据**：
1. `--dry-run` 打印出的 `console:OK` + `file:OK`；
2. `--email-only` 打印 `email:OK`，且**收件箱真的收到一封标题带 `[P3]` 的测试邮件**；
3. 故意把 `SMTP_PASSWORD` 改错 → `detail` 里出现 `535` 之类的**具体原因**（说明错误可诊断）；
4. 连续触发同一异常 → 第二条被 `alert_id 去重` 或 `冷却` 拦下（说明不会刷屏）。

---

## 7. 邮件到底是怎么实现的（可复制的实现指南）

> 这一节是给**要理解/接手/复刻这套邮件通知的人**看的（含做前端的同学）。
> 全部用 Python 标准库，不装任何包。

### 7.1 先纠正一个常见误解：前端不能直接发邮件

**浏览器无法直连 SMTP**（没有 TCP 能力，只有 HTTP），而且如果把邮箱授权码写进
前端 JS，任何人打开 F12 就能拿到你的邮箱控制权。所以：

```
❌ 错误架构：浏览器 ──SMTP──▶ 邮件服务器        （技术上做不到 + 泄露凭据）

✅ 正确架构：浏览器 ──HTTP──▶ 本机后端 ──SMTP──▶ 邮件服务器
                          （/api/alerts/test）
```

**所以"发邮件"永远是后端的事**；前端要做的是三件：
1. 显示通道**是否就绪**（`meta.alerts.email_ready` / `email_problem`）；
2. 提供「测试」按钮打 `/api/alerts/test?channel=email`；
3. 在告警列表里显示**每条告警的投递结果**（`delivered` / `channels[]` / `skip_reason`）。

### 7.2 SMTP 会话流程（看懂这 6 步就会写了）

```
① 连接      465 → SMTP_SSL（直接 TLS）
            587 → SMTP + STARTTLS（先明文、再升级加密）
② EHLO      打招呼，服务器回能力清单
③ AUTH      用户名 + 授权码（QQ/163 是"授权码"，不是登录密码）
④ MAIL FROM 发件人 —— ★ 必须等于认证用户，否则 553
⑤ RCPT TO   收件人（可以多个）
⑥ DATA      头部 + 空行 + 正文，以 <CRLF>.<CRLF> 结束
```

### 7.3 最小可运行实现（40 行，存成 `send_mail.py` 即可跑）

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小 SMTP 发信示例（与 agri_logger/notifier/channels.py 的核心逻辑一致）"""
import smtplib, ssl
from email.header import Header
from email.mime.text import MIMEText

HOST, PORT = "smtp.qq.com", 465      # 163: smtp.163.com:465
USER, PASS = "you@qq.com", "授权码"   # ★ 不是登录密码
TO = ["someone@example.com"]

# 1) 组信：中文主题必须显式按 utf-8 编码，否则乱码或直接报错
msg = MIMEText("这是一封测试邮件：告警通道连通性验证。", "plain", "utf-8")
msg["Subject"] = Header("[P3] 自测：告警通道连通性测试", "utf-8")
msg["From"] = USER
msg["To"] = ", ".join(TO)

# 2) 发信：465 用 SMTP_SSL
ctx = ssl.create_default_context()
with smtplib.SMTP_SSL(HOST, PORT, timeout=15, context=ctx) as s:
    s.login(USER, PASS)
    s.sendmail(USER, TO, msg.as_string())

print("已发送")
```

**跑不通时按这个顺序查**（对应真实错误码）：

| 报错 | 含义 | 怎么改 |
|---|---|---|
| `535 Login Fail` / `Authorization failed` | 账号密码错 | 用的是**授权码**吗？QQ 要先在设置里开启 IMAP/SMTP |
| `553 Mail from must equal authorized user` | 发件人 ≠ 认证用户 | `From` 必须写认证的那个邮箱 |
| `ConnectionRefusedError` | 端口不对/被封 | 试 587 + `starttls()`；公司/校园网可能封 465 |
| `ssl.SSLError: WRONG_VERSION_NUMBER` | 明文连了 SSL 端口 | 465 必须 `SMTP_SSL`，587 必须 `starttls()` |
| `gaierror` | 域名解析失败 | 主机名拼错，或没有外网 DNS |
| 超时无响应 | 出口被墙 | 换 587；或改用 Webhook（走 443，几乎不被封） |
| `421` / `450` 频率限制 | 发太勤 | 本项目已在策略层限流（180s 冷却 + 12 条/小时） |
| 发送成功但收不到 | 被判垃圾邮件 | 先发给自己，把发件人加白名单；检查"已删除/垃圾箱" |

### 7.4 本项目里的工程化版本比上面多了什么

最小示例能发信，但直接用在告警系统里会出事（配置错就把主流程拖死）。真正的实现
`agri_logger/notifier/channels.py` 在最小示例之上加了 6 件事：

| 增强 | 解决的问题 |
|---|---|
| 双路回退（465 ↔ 587 互换重试） | 用户忘了改端口/加密方式（最高频配置错误） |
| 两次尝试的**错误都保留** | 只报最后一次会让人查错方向 |
| 失败退避 120 秒 | 免得每条告警都等 2×15 秒超时，把分析队列拖慢 |
| `send()` **永不抛异常** | 邮件失败绝不能带崩采集/分析主链路 |
| 中文附件名 RFC 2231 编码 | 否则部分客户端显示成 `attach.bin` |
| 正文纯文本 + 报告作附件 | 手机上正文可读，要细节再点附件 |

### 7.5 告警邮件长什么样（真实模板）

正文由 `AlertEvent.to_email_text()` 生成，顺序按「运维在手机上先看什么」排：

```text
【P1】W0014 设备C 堆枯竭引发分配失败与上报超时

摘要：检测到设备 C 空闲堆不足后内存分配失败并上报超时（F3006/F3001/F2002），
      疑似上报缓冲未释放致堆枯竭。

── 详情 ──────────────────────────
设备      设备 C
时间范围  780s ~ 835s
窗口      W0014
异常族    FW-1002-1004-2001-...-3006
异常分    0.43
分析引擎  LLM 语义分析
数据来源  真机实时串口 COM3
生成时间  2026-11-17 20:10:28

── 触发信号 ──────────────────────
· 含 5 条 ERROR/FATAL
· 异常故障码: F3005(看门狗即将触发), ...

── 证据日志（可在原始日志中按行号回查）──
[hl:00001953] 000780.000000 E A003 AgriHealth: 1 157 5 3005 6 14 15 2 z=D
...

完整根因报告见附件 / 看板「根因报告」页：W0014_设备C_堆枯竭引发分配失败与上报超时.md

── 说明 ──────────────────────────
本告警由「OpenHarmony 日志智能聚合分析工具」自动生成。
报告中的证据段由程序直接从原始日志取出，不经过模型，可逐条回查。
```

**前端对应要展示的字段**（`GET /api/alerts`）：
`severity`（配色）、`title`、`summary`（**大字号显示，这就是"AI 摘要"**）、
`span`、`mode`（LLM/规则，**必须显示**）、`delivered` + `channels[]`（投递结果）、
`skip_reason`（没推出去的原因）、`report_file`（可点击跳报告）。

### 7.6 如果只想验证、不想配邮箱

三条路都能证明"告警链路是通的"，都不需要邮箱：

1. **控制台 + 文件**（零配置，默认就有）：跑一条真异常，看终端红字与 `logs/alerts.jsonl`；
2. **Webhook**（只要一个 URL，走 443）：钉钉/飞书/企业微信群机器人；
3. **演练模式**：`ALERT_DRY_RUN=1`，只显示"会发什么"，不产生副作用。

---

## 8. 与现有代码的衔接点

| 位置 | 作用 |
|---|---|
| `agri_logger/analyzer/summary.py` | 摘要措辞的唯一来源（告警 / 报告 / 规则降级三处共用） |
| `agri_logger/analyzer/rca.py` `SYSTEM_L2` | 新增 `alert_summary` 输出要求（**不增加调用次数**） |
| `agri_logger/analyzer/rca.py` `_rule_rca` | 规则降级时也产出 `alert_summary` |
| `agri_logger/notifier/alert.py` | `AlertEvent` / `AlertPolicy`（去重冷却限流）/ `AlertManager` |
| `agri_logger/notifier/channels.py` | 四个通道，`send()` 永不抛异常 |
| `agri_logger/notifier/config.py` | 从环境变量 + `.env` 读配置，含体检与脱敏视图 |
| `agri_logger/notifier/__main__.py` | 自测 CLI（本文 §6 三条命令） |
| `tools/dashboard_server.py` `_dispatch_alert()` | 出报告后推告警（自动 + 手动两条路径） |
| `tools/dashboard_server.py` `/api/alerts*` | 看板读取与自测入口 |

**接口约定**：任何时候新增通道，只需实现 `Channel` 子类并在 `build_channels()` 里注册，
**不需要改分析逻辑**。
