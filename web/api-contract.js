/* ============================================================================
 * api-contract.js —— 前端唯一契约文件（改这一个文件就能换主题/换数据源/换字段）
 * ============================================================================
 *
 * 这份文件是「后端能力」在「前端世界里」的完整投影：
 *   · 有哪些接口、参数是什么、对应哪个样例文件
 *   · 级别 / 严重度 / 模块 / 故障码 / 状态机的**全部枚举与中文名**
 *   · 字段字典（20 个字段的缩放与单位）
 *   · 报告 Markdown 的固定章节结构
 *   · 轮询节奏、配色、文案
 *
 * ---------------------------------------------------------------------------
 * ★「一键更换」怎么用（三件事都能一处改）
 * ---------------------------------------------------------------------------
 * 1) 换数据源（真实后端 ↔ 离线样例）：改下面 `dataSource.mode`
 *      'live' —— 只打真实接口，失败就报错（联调/答辩用）
 *      'mock' —— 只读 /samples/*.json，完全不连后端（纯前端离线开发用）
 *      'auto' —— 先试真实接口，失败自动回落样例（默认，最省心）
 *
 * 2) 换配色/主题：改 `theme`（或在自己的 <script> 里覆盖）
 *
 * 3) 换任何字段/文案/轮询间隔：不需要改这个文件，在你的页面里【先】写
 *      <script>window.AGRILOG_OVERRIDE = { poll: { liveLogs: 500 } };</script>
 *    再引入本文件，就会做深合并覆盖（见文件末尾的实现）。
 *
 * ---------------------------------------------------------------------------
 * 依赖：无。纯数据 + 一个小合并函数，不需要 npm / 打包器。
 * 引用：<script src="/api-contract.js"></script>
 *       <script src="/api-client.js"></script>   ← 配套的取数封装
 * ---------------------------------------------------------------------------
 * 详细说明见 docs/14-api-interface-spec.md（接口）与 docs/15-data-formats.md（数据格式）。
 * ==========================================================================*/

(function (global) {
  'use strict';

  var CONTRACT = {

    /* ── 元信息 ─────────────────────────────────────────────────────────── */
    meta: {
      name: 'OpenHarmony 日志智能聚合分析工具 · 前端契约',
      version: '1.2.0',
      // 本契约对应的后端代码基线（上游仓库 bratatata526/Open-Harmony）
      baseCommit: '856e2e9',
      generatedAt: '2026-11-17',
      docs: {
        api: 'docs/14-api-interface-spec.md',
        formats: 'docs/15-data-formats.md',
        frontend: 'docs/16-frontend-dev-guide.md',
        alerting: 'docs/13-alerting.md'
      }
    },

    /* ── 数据源与轮询 ───────────────────────────────────────────────────── */
    dataSource: {
      mode: 'auto',          // 'live' | 'mock' | 'auto'
      baseUrl: '',           // '' = 同源；跨端口调试可填 http://127.0.0.1:8080
      mockDir: '/samples',   // 样例目录（由 web/samples/ 提供）
      mockLatencyMs: 120     // 模拟网络延迟，让 loading 态能看见（0 = 不延迟）
    },

    // 轮询间隔（毫秒）。比这更密不会更快：后端对派生视图带 2 秒缓存。
    poll: {
      meta: 2000,
      summary: 3000,
      timeline: 3000,
      zones: 3000,
      families: 3000,
      windows: 3000,
      logs: 5000,       // 回放模式的日志明细（全量翻页）
      devices: 10000,
      liveStatus: 2000,
      liveLogs: 1000,   // 实时模式增量拉取
      analyze: 1000,    // 仅分析进行中轮询
      alerts: 5000,
      reports: 10000
    },

    /* ── 接口清单 ───────────────────────────────────────────────────────
     * sample 字段 = 对应的离线样例文件名（在 web/samples/ 下）。
     * 有 sample 的接口，api-client 在 mock 模式下会读它。            */
    endpoints: {
      meta:        { path: '/api/meta',        method: 'GET', params: ['zone'],  sample: 'meta.json',           liveSample: 'meta-live.json' },
      summary:     { path: '/api/summary',     method: 'GET', params: ['zone'],  sample: 'summary.json' },
      timeline:    { path: '/api/timeline',    method: 'GET', params: ['zone'],  sample: 'timeline.json' },
      zones:       { path: '/api/zones',       method: 'GET', params: ['zone'],  sample: 'zones.json' },
      families:    { path: '/api/families',    method: 'GET', params: ['zone'],  sample: 'families.json' },
      windows:     { path: '/api/windows',     method: 'GET', params: ['zone'],  sample: 'windows.json' },
      logs:        { path: '/api/logs',        method: 'GET', params: ['level', 'tag', 'zone', 'q', 'limit', 'offset'], sample: 'logs.json' },
      devices:     { path: '/api/devices',     method: 'GET', params: [],        sample: 'devices.json' },
      liveStatus:  { path: '/api/live/status', method: 'GET', params: [],        sample: 'live-status.json' },
      liveLogs:    { path: '/api/live/logs',   method: 'GET', params: ['since'], sample: 'live-logs.json' },
      liveReconnect: { path: '/api/live/reconnect', method: 'GET', params: [],   sample: null },
      analyze:     { path: '/api/analyze',     method: 'GET', params: ['zone', 'max', 'no_llm'], sample: null },
      analyzeStatus: { path: '/api/analyze?action=status', method: 'GET', params: [], sample: 'analyze-status.json' },
      reports:     { path: '/api/reports',     method: 'GET', params: [],        sample: 'reports.json' },
      report:      { path: '/api/report',      method: 'GET', params: ['name'],  sample: 'report.json' },
      alerts:      { path: '/api/alerts',      method: 'GET', params: ['limit'], sample: 'alerts.json' },
      alertsTest:  { path: '/api/alerts/test', method: 'GET', params: ['channel'], sample: null },
      alertsReload:{ path: '/api/alerts/reload', method: 'GET', params: [],      sample: null },
      shutdown:    { path: '/api/shutdown',    method: 'GET', params: [],        sample: null }
    },

    /* ── 日志级别 ───────────────────────────────────────────────────────
     * 排序 D < I < W < E < F；`E` 统计柱状图时要把 `F` 并进去（后端已并入）。 */
    levels: ['D', 'I', 'W', 'E', 'F'],
    levelInfo: {
      D: { label: '调试', rank: 0 },
      I: { label: '信息', rank: 1 },
      W: { label: '警告', rank: 2 },
      E: { label: '错误', rank: 3 },
      F: { label: '致命', rank: 4 }
    },

    /* ── 告警严重度（配色按此映射，不要自己另立一套）────────────────── */
    severities: ['P0', 'P1', 'P2', 'P3'],
    severityInfo: {
      P0: { label: '数据全断/设备离线', rank: 0, icon: '🔴' },
      P1: { label: '功能受损',         rank: 1, icon: '🟠' },
      P2: { label: '性能下降',         rank: 2, icon: '🟡' },
      P3: { label: '仅提示',           rank: 3, icon: '⚪' }
    },

    /* ── 模块（domain）───────────────────────────────────────────────── */
    domains: {
      A001: '环境传感',
      A002: '数据上报',
      A003: '设备自检',
      A004: '执行器控制',
      A005: '任务调度',
      A006: '配网与通信',
      A007: '故障注入器',
      A008: '系统异常（看门狗/死机）'
    },

    /* ── 状态机（字段 11 state）──────────────────────────────────────── */
    states: { 0: 'idle', 1: 'irrigating', 2: 'venting', 3: 'degraded' },
    stateInfo: {
      idle:       { label: '空闲' },
      irrigating: { label: '灌溉中' },
      venting:    { label: '通风中' },
      degraded:   { label: '降级运行' }
    },

    /* ── 字段字典（消息体 `<ID> <值>`，物理值 = 原始值 / scale）────────
     * 只有绘图/自行格式化时才需要它；接口已给出换算后的物理值。       */
    fields: {
      1:  { name: 'seq',    scale: 1,  unit: '',    desc: 'zone 内单调递增序号（丢包检测）' },
      2:  { name: 'temp',   scale: 10, unit: '℃',   desc: '温度，原始值 ×10' },
      3:  { name: 'humi',   scale: 10, unit: '%',   desc: '湿度，原始值 ×10' },
      4:  { name: 'light',  scale: 1,  unit: '',    desc: '光照相对值 0~1000（AP3216C ALS）' },
      5:  { name: 'fault',  scale: 1,  unit: '',    desc: '故障码数值，见 faults' },
      6:  { name: 'code',   scale: 1,  unit: '',    desc: '错误码，见 errorCodes' },
      7:  { name: 'tick',   scale: 1,  unit: 'ms',  desc: '系统 tick' },
      8:  { name: 'heap',   scale: 1,  unit: 'B',   desc: '剩余堆内存' },
      9:  { name: 'cost',   scale: 1,  unit: 'ms',  desc: '耗时' },
      10: { name: 'retry',  scale: 1,  unit: '',    desc: '重试 / 连续失败次数' },
      11: { name: 'state',  scale: 1,  unit: '',    desc: '状态机编码，见 states' },
      12: { name: 'thresh', scale: 10, unit: '',    desc: '阈值，原始值 ×10' },
      13: { name: 'rssi',   scale: 1,  unit: 'dBm', desc: 'WiFi 信号强度（存正值，实际取负）' },
      14: { name: 'queue',  scale: 1,  unit: '',    desc: '消息队列长度' },
      15: { name: 'miss',   scale: 1,  unit: '',    desc: '看门狗漏喂次数' },
      16: { name: 'zone_id',scale: 1,  unit: '',    desc: '逻辑设备编号 0=A … 5=F' },
      17: { name: 'mcause', scale: 1,  unit: '',    desc: '机器异常原因码，见 mcause' },
      18: { name: 'addr',   scale: 1,  unit: 'B',   desc: '出错地址 / mtval' },
      19: { name: 'cpu',    scale: 1,  unit: '%',   desc: 'CPU 占用率 0~100' },
      20: { name: 'memfail',scale: 1,  unit: '',    desc: '内存池累计申请失败次数' }
    },

    /* ── mcause 取值（字段 17）───────────────────────────────────────── */
    mcause: {
      0: '指令地址非对齐', 1: '取指访问错', 2: '非法指令',
      4: 'Load 地址非对齐', 5: 'Load 访问错',
      6: 'Store 地址非对齐', 7: 'Store/AMO 访问错', 15: 'Store 页错'
    },

    /* ── 故障码（28 个）──────────────────────────────────────────────────
     * normal:true = 正常运维事件，统计异常数时必须排除！            */
    faults: {
      F1001: { code: 1001, name: '传感器读取超时',       level: 'E', domain: 'A001', normal: false },
      F1002: { code: 1002, name: '单总线复位无应答',     level: 'E', domain: 'A001', normal: false },
      F1003: { code: 1003, name: '数据校验失败',         level: 'E', domain: 'A001', normal: false },
      F1004: { code: 1004, name: '读数超出合理范围',     level: 'W', domain: 'A001', normal: false },
      F1005: { code: 1005, name: '数值越阈值',           level: 'W', domain: 'A001', normal: false },
      F1006: { code: 1006, name: '传感器离线',           level: 'E', domain: 'A001', normal: false },
      F2001: { code: 2001, name: 'MQTT 连接失败',        level: 'E', domain: 'A006', normal: false },
      F2002: { code: 2002, name: '上报超时',             level: 'E', domain: 'A002', normal: false },
      F2003: { code: 2003, name: 'WiFi 信号弱',          level: 'W', domain: 'A006', normal: false },
      F2004: { code: 2004, name: '断线重连',             level: 'W', domain: 'A006', normal: true  },
      F2005: { code: 2005, name: '数据帧校验错',         level: 'E', domain: 'A006', normal: false },
      F3001: { code: 3001, name: '内存分配失败',         level: 'E', domain: 'A003', normal: false },
      F3002: { code: 3002, name: '任务栈溢出',           level: 'E', domain: 'A003', normal: false },
      F3003: { code: 3003, name: '任务调度超时',         level: 'E', domain: 'A005', normal: false },
      F3004: { code: 3004, name: '队列积压',             level: 'W', domain: 'A005', normal: false },
      F3005: { code: 3005, name: '看门狗即将触发',       level: 'E', domain: 'A003', normal: false },
      F3006: { code: 3006, name: '空闲堆不足',           level: 'W', domain: 'A003', normal: false },
      F3007: { code: 3007, name: '看门狗超时复位',       level: 'E', domain: 'A008', normal: false },
      F3008: { code: 3008, name: '中断阻塞',             level: 'E', domain: 'A008', normal: false },
      F3009: { code: 3009, name: '系统死机',             level: 'E', domain: 'A008', normal: false },
      F3010: { code: 3010, name: '非法内存访问',         level: 'E', domain: 'A008', normal: false },
      F3011: { code: 3011, name: 'CPU 占用率过高',       level: 'W', domain: 'A005', normal: false },
      F4001: { code: 4001, name: '执行器超时',           level: 'W', domain: 'A004', normal: false },
      F4002: { code: 4002, name: '灌溉状态变更',         level: 'I', domain: 'A004', normal: true  },
      F5001: { code: 5001, name: '设备启动',             level: 'I', domain: 'A003', normal: true  },
      F5002: { code: 5002, name: '配置重载失败',         level: 'W', domain: 'A003', normal: false },
      F5003: { code: 5003, name: '异常复位',             level: 'E', domain: 'A003', normal: false },
      F5004: { code: 5004, name: '看门狗超时配置非法',   level: 'W', domain: 'A003', normal: false }
    },

    /* ── 错误码（字段 6 code → 短语）────────────────────────────────── */
    errorCodes: {
      0: 'none', 1: 'reset_timeout', 2: 'crc_mismatch', 3: 'out_of_range',
      4: 'no_response_x5', 5: 'threshold_exceed', 6: 'state_change',
      7: 'publish_timeout', 8: 'rssi_low', 9: 'reconnect', 10: 'stack_overflow',
      11: 'task_hung', 12: 'alloc_failed', 13: 'queue_full', 14: 'wdt_feed_missed',
      15: 'heap_low', 16: 'config_load_failed', 17: 'wdt_reset', 18: 'isr_block',
      19: 'cpu_overload', 20: 'crash_task', 21: 'crash_isr', 22: 'bad_addr',
      23: 'mem_corrupt', 24: 'abnormal_reset', 25: 'wdt_config_invalid', 99: 'unknown'
    },

    /* ── 报告 Markdown 的固定章节（前端渲染/定位锚点用）──────────────── */
    reportSections: [
      { key: 'title',    pattern: '^# ',                 label: '标题' },
      { key: 'summary',  pattern: '^> \\*\\*摘要\\*\\*', label: '摘要（紧贴标题，必须显示）' },
      { key: 'meta',     pattern: '^\\|',                label: '元信息表' },
      { key: 'symptom',  pattern: '^## 一、现象',         label: '现象' },
      { key: 'evidence', pattern: '^## 二、证据',         label: '证据（程序取自原始日志，可回查 eid）' },
      { key: 'cause',    pattern: '^## 三、疑似根因',     label: '疑似根因' },
      { key: 'impact',   pattern: '^## 四、影响',         label: '影响' },
      { key: 'action',   pattern: '^## 五、处置建议',     label: '处置建议' }
    ],

    // 分析引擎标注：必须显示，不能隐藏（关系到结论可信度与答辩诚实性）
    engineBadges: {
      llm:  { label: 'LLM 语义分析',        cls: 'engine-llm' },
      rule: { label: '本地规则引擎（降级）', cls: 'engine-rule' },
      raw:  { label: '仅原始事实（降级）',   cls: 'engine-raw' }
    },

    // 报告来源标注（列表里必须显示，避免让人误以为结论都来自真机）
    reportSources: {
      live:  { label: '真机实测', cls: 'src-live' },
      sim:   { label: '模拟数据集', cls: 'src-sim' },
      other: { label: '历史样例', cls: 'src-other' }
    },

    /* ── 主题令牌（★ 换配色只改这里；或在自己的页面覆盖）──────────── */
    theme: {
      colors: {
        bg:      '#0d1117',
        panel:   '#161b22',
        border:  '#26303a',
        text:    '#d7dde5',
        muted:   '#8b97a6',
        accent:  '#3fb950',
        danger:  '#f85149',
        warn:    '#d29922',
        info:    '#58a6ff'
      },
      // 日志级别 → 颜色
      level: { D: '#8b97a6', I: '#58a6ff', W: '#d29922', E: '#f85149', F: '#ff7b72' },
      // 告警严重度 → 颜色（P0 最严重）
      severity: { P0: '#f85149', P1: '#fb8500', P2: '#d29922', P3: '#8b97a6' },
      // 实时状态徽标
      status: { online: '#3fb950', offline: '#f85149', replay: '#d29922' },
      font: {
        mono: 'ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace',
        sans: 'system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif'
      },
      radius: { sm: '4px', md: '8px', lg: '12px' }
    },

    /* ── 文案（集中管理，便于统一改口径）────────────────────────────── */
    text: {
      deviceSingular: '设备',           // 单传感器口径：不要说"6 个传感器"
      devicePlural: '逻辑设备',
      modeReplay: '回放 · 静态文件',
      modeLive: '实时 · 串口',
      modeOffline: '离线',
      simWarning: '数据源为模拟器，非真机',
      noAnomaly: '未检出异常（已生成健康报告）'
    }
  };

  /* ── 深合并：把 window.AGRILOG_OVERRIDE 叠到默认契约上 ─────────────── */
  function isPlainObject(v) {
    return v !== null && typeof v === 'object' && !Array.isArray(v);
  }
  function deepMerge(base, patch) {
    if (!isPlainObject(patch)) { return base; }
    Object.keys(patch).forEach(function (k) {
      var pv = patch[k];
      if (isPlainObject(pv) && isPlainObject(base[k])) {
        deepMerge(base[k], pv);          // 就地合并，保持引用稳定
      } else {
        base[k] = pv;
      }
    });
    return base;
  }
  if (global.AGRILOG_OVERRIDE) {
    deepMerge(CONTRACT, global.AGRILOG_OVERRIDE);
  }

  /* ── 便捷辅助（前端常用，避免各处重复实现）───────────────────────── */
  CONTRACT.helpers = {
    // 原始值 → 物理值（绘图时若自己解析消息体才需要）
    // 注意 state(11) 是**文字**不是数字：后端已把 `11 3` 还原成 "degraded"，
    // 这里保持一致，避免前端两条路径拿到不同形态的值。
    physical: function (fieldId, raw) {
      var f = CONTRACT.fields[fieldId];
      if (!f) { return raw; }
      if (Number(fieldId) === 11) {
        return CONTRACT.states[raw] !== undefined ? CONTRACT.states[raw] : String(raw);
      }
      var v = f.scale === 1 ? raw : (raw / f.scale);
      return f.unit ? (v + f.unit) : v;
    },
    // 故障码 → 中文名（未知码也要有兜底，不要显示 undefined）
    faultName: function (tag) {
      var f = CONTRACT.faults[tag];
      return f ? f.name : '未知故障(' + tag + ')';
    },
    faultIsNormal: function (tag) {
      var f = CONTRACT.faults[tag];
      return !!(f && f.normal);
    },
    // 设备称呼：单设备模式下不要显示成"1号棚"
    deviceLabel: function (zone, name) {
      if (name) { return name; }
      return zone ? (CONTRACT.text.deviceSingular + ' ' + zone) : '整机';
    },
    severityColor: function (sev) {
      return CONTRACT.theme.severity[sev] || CONTRACT.theme.colors.muted;
    },
    levelColor: function (lv) {
      return CONTRACT.theme.level[lv] || CONTRACT.theme.colors.muted;
    }
  };

  global.AGRILOG_CONTRACT = CONTRACT;
  if (typeof module === 'object' && module.exports) {
    module.exports = CONTRACT;            // 允许 Node 里 require（写单测/校验用）
  }
})(typeof window !== 'undefined' ? window : globalThis);
