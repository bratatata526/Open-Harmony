# 前端开发规范（看板 UI）

> **读者**：负责看板前端与 UI 优化的同学。
> **这份文档管"怎么写"**（文件组织、接入方式、规范、自检）；
> **"数据长什么样"** 在 `docs/15-data-formats.md`；
> **"接口字段是什么"** 在 `docs/14-api-interface-spec.md`。
>
> 三份配合看，不要跳。看完你应该能：**不启动 Python 后端也能把界面做出来**，
> 并且接上后端时**不用改一行业务代码**。

---

## 0. 三条必须记住的结论（先看这个）

1. **前端不解析日志、不判断异常。** 后端已经算好了窗口、异常分、异常族、报告、告警。
   前端只负责"取 + 展示"。任何"我自己算一下异常数"的做法都会和后端对不上。
2. **所有枚举与配色从 `web/api-contract.js` 取，不要硬编码。** 换主题、换口径只改那一个文件。
3. **时间一律是"设备上电后的相对秒数"**（`float`），不是 Unix 时间戳。
   只有 `meta.loaded_at`、告警 `created_at`、报告 `mtime` 是本地时间字符串。

---

## 1. 现在的前端长什么样（先摸清家底）

```
web/
├─ index.html          ← 目前页面 + 样式 + 逻辑全在这一个文件里（约 81 KB）
├─ api-contract.js     ← ★ 契约单一来源（枚举/配色/轮询/接口清单，约 20 KB）
├─ api-client.js       ← ★ 取数层（真实/离线自动切换、轮询、实时日志流，约 13 KB）
└─ samples/            ← ★ 真实后端响应样例（15 个数据文件 + manifest，约 100 KB）
   ├─ manifest.json
   ├─ meta.json / summary.json / timeline.json / zones.json / ...
   └─ live-status.json / live-logs.json
```

**现状的取舍**：`index.html` 是单文件（内联 CSS + JS）。这在校赛演示阶段的好处是
"双击一个文件就能跑、没有构建步骤、没有 npm"；坏处是维护性差、多人同时改容易冲突。

**本规范不要求你立刻重构它**，但要求**新增的公共能力放到独立文件里**：

| 类型 | 放哪 | 说明 |
|---|---|---|
| 契约常量（枚举/配色/轮询） | `web/api-contract.js` | 已存在，只改它 |
| 取数逻辑 | `web/api-client.js` | 已存在，只改它 |
| 新面板的渲染逻辑 | 建议 `web/panels/<name>.js` | 每个面板一个文件，挂到 `window.Panels.<name> = { mount(el) }` |
| 样式 | 建议 `web/styles/<topic>.css` | 按主题拆，不要都堆在 `<style>` 里 |
| 图标/图片 | `web/assets/` | 现成图标在仓库根的 `assets/`（扳手图标），页面用的放这里 |

> ⚠️ **不要**为了"看起来工程化"而引入打包器（webpack/vite/npm 依赖）。
> 这个项目的硬约束是「双击就能跑、断网也能演示」。**原生 ES module / 普通 `<script>` 足够。**

---

## 2. 接入三步（照抄即可）

### 第 1 步：引入两个文件

在 `index.html` 的 `</body>` 前，**顺序不能反**（client 依赖 contract）：

```html
<script src="/api-contract.js"></script>
<script src="/api-client.js"></script>
<script src="/panels/logs.js"></script>
```

> 路径是 `/api-contract.js`（根路径），因为后端把 `web/` 目录作为静态根。
> 直接用 `file://` 打开会读不到 `/api/...`，请看 §2.3。

### 第 2 步：取数

```js
// 单个接口
const meta = await AGRILOG_CLIENT.get('meta');

// 带查询参数
const logs = await AGRILOG_CLIENT.get('logs', { level: 'W', limit: 100 });

// 轮询（返回 stop 函数；内置"上一次没回来就跳过本轮"）
const stop = AGRILOG_CLIENT.poll('summary', { zone: 'A' }, 3000, renderSummary, showError);
```

### 第 3 步：实时日志流（**这段千万别自己写**）

实时日志有个坑：后端重启后记录从 0 重新计数，前端手上的游标会失效，
不处理就会在界面上看到**重复日志**。`api-client` 已经处理好了：

```js
const stream = AGRILOG_CLIENT.streamLiveLogs(function (batch) {
  if (batch.reset) {
    logList.innerHTML = '';        // ← 世代变了，必须先清空
  }
  batch.items.forEach(appendLogRow);
  setOnlineBadge(batch.online, batch.error, batch.silent_s);
});

// 离开页面时
window.addEventListener('beforeunload', stream.stop);
```

**不要**自己维护 `since` 游标、**不要**用 `items.length` 累加 —— 用响应里的 `next`，
而这一步 `streamLiveLogs` 已经替你做了。

---

## 3. 「一键更换」：三种场景怎么改

### 3.1 换数据源（真实后端 ↔ 离线样例）

改 `web/api-contract.js` 里**一个字段**：

```js
dataSource: {
  mode: 'mock',   // 'live' = 只连后端 | 'mock' = 只读样例 | 'auto' = 先真后假（默认）
  baseUrl: '',    // 后端不在同源时填 http://127.0.0.1:8080
  mockDir: '/samples'
}
```

| 场景 | 用什么 mode | 说明 |
|---|---|---|
| 纯做样式、没起后端 | `mock` | 全程离线，100% 可复现 |
| 联调 | `live` | 出错就报错，不静默降级（免得"以为连上了其实读的是样例"） |
| 答辩/日常 | `auto` | 有后端用后端，没后端自动回落样例 |

**界面上必须显示当前数据源**（用 `AGRILOG_CLIENT.describeSource()` 返回"真实后端/离线样例"）。
否则你会遇到这个尴尬：评委问"这数据哪来的"，你答不上来。

### 3.2 换主题/配色

改 `web/api-contract.js` 的 `theme` 段：

```js
theme: {
  colors:  { bg: '#0d1117', panel: '#161b22', /* ... */ },
  level:   { D:'#8b97a6', I:'#58a6ff', W:'#d29922', E:'#f85149', F:'#ff7b72' },
  severity:{ P0:'#f85149', P1:'#fb8500', P2:'#d29922', P3:'#8b97a6' },
  // ...
}
```

**也可以完全不改这个文件**，在自己的页面里先覆盖：

```html
<script>
  // 只改配色和轮询，不动其它 —— 深合并
  window.AGRILOG_OVERRIDE = {
    theme: { colors: { bg: '#0b0f14' } },
    poll:  { liveLogs: 500 }
  };
</script>
<script src="/api-contract.js"></script>
```

> 规则：**页面里的局部覆盖优先，`api-contract.js` 是默认值**。
> 这样多个人同时调样式也不会互相覆盖冲突。

### 3.3 换文案/字段名

所有面向用户的文案集中在 `api-contract.js` 的 `text` 段和 `helpers` 里。

**特别注意"单传感器口径"**：物理上只有一个传感器节点，6 个 zone 是**逻辑设备**。

```js
// ❌ 错
`共 ${zones.length} 个传感器`
// ✅ 对（用契约里的文案）
`共 ${zones.length} 个${AGRILOG_CONTRACT.text.devicePlural}`
```

设备名一律走 `AGRILOG_CONTRACT.helpers.deviceLabel(zone, name)` ——
单设备模式下它会返回"设备 A"而不是"1号棚·向阳主棚"。

---

## 4. 六条硬规范（违反会在验收时暴露）

### 4.1 不要自己判断异常
用 `/api/windows` 里的 `anomaly` 布尔值和 `/api/families`，**不要**自己拿 `score` 去比阈值
（阈值可被 `--threshold` 改，前端写死 0.25 一定会对不上）。

### 4.2 统计异常数必须排除"正常事件"
F2004 断线重连、F4002 灌溉状态变更、F5001 设备启动是**正常运维事件**。

```js
const abnormal = faults.filter(f => !f.normal);
```

### 4.3 曲线里的 `null` 是断点，不是 0
`/api/zones` 的 `temp/humi/light` 可能为 `null`（该点没有有效读数，或已剔除故障标记值）。
补 0 会在图上画出"0℃/0%"这种荒谬数值。**断线就断线画。**

### 4.4 时间线按 `step` 画
`/api/timeline` 的点已经被抽样过（`step` 可能是 7 秒）。x 轴按 `t` 定位、宽度按 `step`，
**不要假设"一个点 = 一秒"**。

### 4.5 颜色只从契约取
```js
el.style.color = AGRILOG_CONTRACT.helpers.levelColor(rec.level);
el.style.color = AGRILOG_CONTRACT.helpers.severityColor(alert.severity);
```
不要在看板里另立一套"我的红色和你的红色不一样"。

### 4.6 引擎标注与来源标注不能隐藏
- 报告里的 `分析引擎`（`LLM 语义分析` / `本地规则引擎（降级）`）
- 报告列表里的 `source_kind`（`live` 真机 / `sim` 模拟 / `other` 历史样例）
- 实时模式下的 `sim: true`（数据源是模拟器）

这三处**必须显示**。它们决定了结论的可信度，也是答辩时"我们没说假话"的证据。

---

## 5. 状态与轮询规范

| 状态 | 要求 |
|---|---|
| **加载中** | 首次加载显示骨架/占位，不要空白闪烁 |
| **出错** | 顶部提示条 + 保留上一次的数据（**不要清空**，清空会让人以为系统坏了） |
| **降级** | `AGRILOG_CLIENT.state.lastSource === 'mock'` 时，角标显示"离线样例" |
| **离线** | `liveStatus.online === false` 时显示 `error` 的中文原因（后端已写好，别自己编） |

**轮询间隔**一律从 `AGRILOG_CONTRACT.poll` 取。三个纪律：

1. **页面隐藏时暂停**（`document.hidden`）——否则后台标签页一直在打请求。
2. **不要并发同一接口**（`poll` 已内置）。
3. **分析进度只在 `running === true` 时轮询**，结束后立刻停。

```js
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { stops.forEach(s => s()); }
  else { mountPanels(); }
});
```

---

## 6. 新增一个面板的标准流程（模板）

以"告警面板"为例：

```js
// web/panels/alerts.js
(function () {
  'use strict';
  const C = AGRILOG_CONTRACT;
  let stop = null;

  function row(a) {
    const color = C.helpers.severityColor(a.severity);
    return `<div class="alert-row" style="border-left:4px solid ${color}">
      <div class="alert-title">${esc(a.title)}</div>
      <div class="alert-summary">${esc(a.summary)}</div>
      <div class="alert-meta">
        ${esc(a.severity)} · ${esc(a.mode === 'llm' ? 'LLM' : '规则降级')}
        · ${esc(a.span)}
        ${a.delivered ? '' : `<span class="warn">未推送：${esc(a.skip_reason)}</span>`}
      </div>
    </div>`;
  }

  function mount(el) {
    stop = AGRILOG_CLIENT.poll('alerts', { limit: 50 }, C.poll.alerts,
      d => { el.innerHTML = d.items.map(row).join('') || '暂无告警'; },
      e  => { el.innerHTML = `<div class="err">${esc(e.message)}</div>`; });
  }
  function unmount() { if (stop) { stop(); stop = null; } }

  window.Panels = window.Panels || {};
  window.Panels.alerts = { mount, unmount };
})();
```

**新增面板检查清单**：

- [ ] 数据只来自 `AGRILOG_CLIENT.get/poll/streamLiveLogs`
- [ ] 颜色/枚举只来自 `AGRILOG_CONTRACT`
- [ ] 所有插值过了 HTML 转义（`esc()`，见下）
- [ ] 有空状态文案（"暂无告警"），不是一块白
- [ ] 有错误态，且**不清空**已有数据
- [ ] 提供 `unmount()` 并在切换视图时调用
- [ ] 面板高度随窗口伸缩（**不要写死像素高**，这是本项目已经修过的 bug）

### 6.1 转义（必须）

日志原文、报告标题、告警摘要都是**外部数据**，直接塞进 `innerHTML` 就是 XSS。
统一用一个函数：

```js
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]
  ));
}
```

渲染 Markdown 报告时同理：**优先用文本渲染 + 只对固定结构做样式**，
不要 `innerHTML = markdown` 一把梭（报告内容里含 LLM 生成的文字）。

---

## 7. 可用性与演示要求

| 项 | 要求 | 为什么 |
|---|---|---|
| 字号 | 正文 ≥ 14px，关键结论 ≥ 16px | 答辩投屏后小字完全看不清 |
| 对比度 | 正文与背景对比 ≥ 4.5:1 | 投影仪色彩普遍偏淡 |
| 键盘 | 主操作可用键盘完成（`Ctrl+Alt+R` 已用于还原布局） | 现场鼠标失灵时能救场 |
| 空数据 | 必须有空态文案 | 演示时不会出现"一片空白以为崩了" |
| 动效 | 曲线/徽标可动，但不要全屏闪烁 | 录屏和投屏都更稳 |
| 中文字体 | `"Microsoft YaHei"` 兜底 | 演示机都是 Windows |

---

## 8. 性能与体量

1. **日志列表必须做虚拟滚动或硬上限**（建议最多渲染 500 行 DOM）。否则跑 2 小时后页面会卡死。
2. **图表数据点上限 240**（后端已经抽样到 ~240 个点，别再放大）。
3. **不要每个 tick 重建整棵 DOM**：只更新变化的节点（或至少只更新文本）。
4. **样式动画用 `transform/opacity`**，不要动 `width/height/top/left`（会触发重排）。

---

## 9. 提交前自检清单（10 条，逐条打勾）

- [ ] 不启动后端，`mode:'mock'` 时**所有面板都有数据**（没有死白块）
- [ ] 接上后端，`mode:'live'` 时数据与 `mock` 结构一致、无 undefined
- [ ] 界面上能看到**当前数据源**（真实后端 / 离线样例）
- [ ] 报告页显示了**分析引擎**（LLM / 规则降级）
- [ ] 实时模式下 `sim:true` 有醒目提示
- [ ] 曲线 `null` 处是断线，不是 0
- [ ] 告警配色符合 `P0~P3` 契约
- [ ] 拔掉后端 → 顶部出现错误提示，**旧数据仍在**
- [ ] 页面切到后台 → 请求暂停
- [ ] 文案里没有"6 个传感器"

---

## 10. 和后端协作的流程

你有两种需求，走不同路径：

| 你的需求 | 怎么做 |
|---|---|
| 现有接口不够用（缺字段/缺聚合） | 提给后端，**先改 `docs/14` 再改代码**（只增不改，见 `docs/14` §5） |
| 只是想换个显示方式 | 不用找后端 —— 改 `web/api-contract.js` 或你的面板即可 |

**契约变更的红线**：
1. 已有字段的名字、类型、单位**不许改**；
2. 需要新语义就**新增字段**（如 `score_v2`），旧字段保留一个版本；
3. 改完在 `docs/14` 的「变更记录」表加一行。

> 一句话：**前端可以自由换皮（主题/布局/文案），但不能自由改字段语义。**
> 前者是 UI 工作，后者会让另一个人的代码在半夜崩掉。

---

## 11. 本地怎么跑起来

```powershell
# ① 有后端（推荐，能看到真实数据）
.venv\Scripts\python.exe tools\dashboard_server.py --port 8080
#   浏览器打开 http://127.0.0.1:8080

# ② 没有后端 —— 纯前端离线开发
#   把 web/api-contract.js 的 dataSource.mode 改成 'mock'，
#   然后用任意静态服务器起 web/（因为 fetch 不能用 file://）：
python -m http.server 8000 --directory web
#   浏览器打开 http://127.0.0.1:8000
```

> 为什么必须起静态服务器：`fetch('/samples/meta.json')` 在 `file://` 下会被浏览器
> 以 CORS 为由拒绝。`python -m http.server` 一行命令就够，不需要装任何东西。

---

## 12. 附：常用代码片段

```js
// 拿当前视角（全部 / 单设备）
const scope = new URLSearchParams(location.search).get('zone') || '';

// 设备选择器（单设备模式只有一项 → 隐藏选择器）
const devices = await AGRILOG_CLIENT.get('devices');
if (devices.length > 1) { renderDeviceSelect(devices); }

// 生成报告并等它结束（带进度）
await AGRILOG_CLIENT.startAnalyze({ max: 2, no_llm: 0 });
const st = await AGRILOG_CLIENT.waitAnalyze(s => (progress.textContent = s.log.at(-1) || ''));
if (!st.reports.length) { toast('未检出异常，已生成健康报告'); }

// 看报告
const rep = await AGRILOG_CLIENT.get('report', { name: st.reports[0] });
renderMarkdownSections(rep.markdown);   // 按契约 reportSections 切段

// 告警通道是否就绪（设置页显示）
const meta = await AGRILOG_CLIENT.get('meta');
meta.alerts.email_ready
  ? badge('邮件已就绪', 'ok')
  : badge('邮件未配置：' + meta.alerts.email_problem, 'warn');
```

---

*规范版本 v1.0 ｜ 与 `web/api-contract.js` v1.1.0 配套 ｜ 后端基线 `856e2e9`*
