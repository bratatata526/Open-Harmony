/* ============================================================================
 * api-client.js —— 取数层（配合 api-contract.js 使用）
 * ============================================================================
 *
 * 它只干三件事，但把前端最容易写错的地方都封掉了：
 *   1) 按契约的 `dataSource.mode` 自动选「真实接口」还是「离线样例」
 *   2) 提供轮询助手（自带"上一次没回来就跳过这一轮"，不会堆请求）
 *   3) 提供实时日志流助手 —— 正确实现 reset / epoch / next 游标
 *      （写错这一段就会在界面上看到【重复日志】，这是本项目踩过的坑）
 *
 * 用法：
 *   <script src="/api-contract.js"></script>
 *   <script src="/api-client.js"></script>
 *   <script>
 *     AGRILOG_CLIENT.get('meta')                 // → Promise<数据>
 *     AGRILOG_CLIENT.get('logs', { limit: 50 })  // 带查询参数
 *     var stop = AGRILOG_CLIENT.poll('meta', {}, 2000, function (d) { ... });
 *     AGRILOG_CLIENT.streamLiveLogs(function (batch) { render(batch.items); });
 *   </script>
 *
 * 依赖：api-contract.js（必须先引入）。无其它依赖。
 * ==========================================================================*/

(function (global) {
  'use strict';

  var C = global.AGRILOG_CONTRACT;
  if (!C) {
    throw new Error('api-client.js 需要先引入 api-contract.js');
  }

  var DS = C.dataSource;

  /* ── 基础工具 ─────────────────────────────────────────────────────── */

  function buildQuery(params) {
    if (!params) { return ''; }
    var parts = [];
    Object.keys(params).forEach(function (k) {
      var v = params[k];
      if (v === undefined || v === null || v === '') { return; }
      parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(v));
    });
    return parts.length ? ('?' + parts.join('&')) : '';
  }

  // 把路径里的查询串也考虑进去（analyzeStatus 的 path 自带 ?action=status）
  function joinUrl(path, params) {
    var base = DS.baseUrl || '';
    var q = buildQuery(params);
    if (!q) { return base + path; }
    return base + path + (path.indexOf('?') >= 0 ? '&' + q.slice(1) : q);
  }

  function mockUrl(sampleFile) {
    return (DS.mockDir || '/samples').replace(/\/$/, '') + '/' + sampleFile;
  }

  function delay(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms || 0); });
  }

  function getJSON(url, timeoutMs) {
    var ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var timer = null;
    var opts = { headers: { 'Accept': 'application/json' }, cache: 'no-store' };
    if (ctrl) { opts.signal = ctrl.signal; }
    if (timeoutMs) {
      timer = setTimeout(function () { if (ctrl) { ctrl.abort(); } }, timeoutMs);
    }
    return fetch(url, opts).then(function (r) {
      if (timer) { clearTimeout(timer); }
      if (!r.ok) {
        // 后端出错时返回 {error: "..."}，这里把它带进异常里，便于界面显示
        return r.text().then(function (t) {
          throw new Error('HTTP ' + r.status + ' ' + url + ' :: ' + t.slice(0, 200));
        });
      }
      return r.json();
    }, function (e) {
      if (timer) { clearTimeout(timer); }
      throw new Error('网络失败 ' + url + ' :: ' + (e && e.message ? e.message : e));
    });
  }

  /* ── 状态（供界面显示"现在用的是真实数据还是样例"）──────────────── */
  var state = {
    lastSource: '',      // 'live' | 'mock' | ''
    lastError: '',
    mockFallbackCount: 0
  };

  function describeSource() {
    if (state.lastSource === 'live') { return '真实后端'; }
    if (state.lastSource === 'mock') { return '离线样例'; }
    return '未知';
  }

  /* ── 核心取数 ─────────────────────────────────────────────────────── */

  /**
   * 取一个接口的数据。
   * @param {string} name   契约 endpoints 里的键，如 'meta' / 'logs' / 'alerts'
   * @param {object} params 查询参数对象
   * @param {object} [opt]  { forceMode: 'live'|'mock', timeoutMs }
   * @returns {Promise<any>}
   */
  function get(name, params, opt) {
    opt = opt || {};
    var ep = C.endpoints[name];
    if (!ep) {
      return Promise.reject(new Error('契约里没有这个接口：' + name +
        '（可用：' + Object.keys(C.endpoints).join(', ') + '）'));
    }
    var mode = opt.forceMode || DS.mode || 'auto';
    var realUrl = joinUrl(ep.path, params);
    var canMock = !!ep.sample;
    var mockPath = canMock ? mockUrl(ep.sample) : '';

    function readMock(reason) {
      if (!canMock) {
        return Promise.reject(new Error('接口 ' + name + ' 没有离线样例；' +
          '请启动后端，或把 dataSource.mode 设为 live。' + (reason ? '（原错误：' + reason + '）' : '')));
      }
      return delay(DS.mockLatencyMs).then(function () {
        return getJSON(mockPath, opt.timeoutMs);
      }).then(function (data) {
        state.lastSource = 'mock';
        state.lastError = reason || '';
        if (reason) { state.mockFallbackCount++; }
        return data;
      });
    }

    if (mode === 'mock') {
      return readMock('');
    }
    if (mode === 'live') {
      return getJSON(realUrl, opt.timeoutMs).then(function (data) {
        state.lastSource = 'live';
        state.lastError = '';
        return data;
      });
    }
    // auto：先真后假。这是默认模式 —— 有后端就用后端，没后端自动降级，
    // 前端同学因此不必先起 Python 才能调样式。
    return getJSON(realUrl, opt.timeoutMs).then(function (data) {
      state.lastSource = 'live';
      state.lastError = '';
      return data;
    }, function (err) {
      return readMock(err && err.message ? err.message : String(err));
    });
  }

  /* ── 轮询助手 ─────────────────────────────────────────────────────── */

  /**
   * 定时轮询一个接口。返回 stop()。
   * ★ 内置"上一次还没回来就跳过这一轮"，避免后端慢时请求堆积成雪崩。
   */
  function poll(name, params, intervalMs, onData, onError) {
    var stopped = false;
    var busy = false;
    function tick() {
      if (stopped || busy) { return; }
      busy = true;
      get(name, params).then(function (d) {
        if (!stopped) { onData && onData(d); }
      }, function (e) {
        if (!stopped) { onError && onError(e); }
      }).then(function () {
        busy = false;
      });
    }
    var id = setInterval(tick, intervalMs);
    tick();                                // 立刻先来一发，别让首屏空着
    return function stop() {
      stopped = true;
      clearInterval(id);
    };
  }

  /* ── 实时日志流助手（正确实现游标 / epoch / reset）─────────────────
   *
   * ★ 这个坑必须封装掉：看板重启后后端记录从 0 重新计数，而前端手上
   *   还攥着重启前的游标（比如 53）。若不处理，等记录涨过 53 就会把
   *   显示过的行再发一遍 → 界面上出现【重复日志】。
   *
   * onBatch 收到的对象：
   *   { items, reset, epoch, online, error, silent_s, source }
   *   reset=true 表示数据源换过代，调用方应【清空列表】再渲染 items。
   */
  function streamLiveLogs(onBatch, opt) {
    opt = opt || {};
    var intervalMs = opt.intervalMs || C.poll.liveLogs;
    var since = 0;
    var epoch = null;
    var stopped = false;
    var busy = false;
    var lastError = '';

    function tick() {
      if (stopped || busy) { return; }
      busy = true;
      get('liveLogs', { since: since }).then(function (d) {
        busy = false;
        if (stopped) { return; }
        var isReset = !!d.reset || (epoch !== null && d.epoch !== epoch);
        if (isReset) {
          since = 0;
          epoch = d.epoch;
          // 重置后这一批就是"从头开始"的数据，交给调用方清屏重建
          onBatch && onBatch({
            items: d.reset ? [] : d.items,
            reset: true,
            epoch: d.epoch,
            online: d.online,
            error: d.error,
            silent_s: d.silent_s,
            source: state.lastSource
          });
          return;                          // 下一轮用 since=0 重新拉全量
        }
        epoch = d.epoch;
        since = d.next;                    // ★ 用后端给的 next，不要自己累加
        if (d.error) { lastError = d.error; }
        onBatch && onBatch({
          items: d.items || [],
          reset: false,
          epoch: d.epoch,
          online: d.online,
          error: d.error || '',
          silent_s: d.silent_s || 0,
          source: state.lastSource
        });
      }, function (e) {
        busy = false;
        if (stopped) { return; }
        lastError = e && e.message ? e.message : String(e);
        onBatch && onBatch({
          items: [], reset: false, online: false, error: lastError,
          silent_s: 0, source: state.lastSource
        });
      });
    }

    var id = setInterval(tick, intervalMs);
    tick();
    return {
      stop: function () { stopped = true; clearInterval(id); },
      reset: function () { since = 0; epoch = null; }
    };
  }

  /* ── 实时状态助手 ─────────────────────────────────────────────────── */

  // 未启用实时模式时后端只返回 {enabled:false}，这里统一成同一个形状，
  // 免得界面各处都要写 `if (d.enabled)` 判空。
  function normalizeLiveStatus(d) {
    if (!d || d.enabled !== true) {
      return { enabled: false, online: false, sim: false, source: '', lines: 0,
               error: '', silent_s: 0, reconnects: 0 };
    }
    return {
      enabled: true,
      online: !!d.online,
      sim: !!d.sim,
      source: d.source || '',
      lines: d.lines || 0,
      error: d.error || '',
      silent_s: d.silent_s || 0,
      reconnects: d.reconnects || 0,
      uptime_s: d.uptime_s || 0,
      cmd: d.cmd || ''
    };
  }

  /* ── 动作型接口 ───────────────────────────────────────────────────── */

  function startAnalyze(params) {
    return get('analyze', params || { max: 2 });
  }
  function analyzeStatus() {
    return get('analyzeStatus');
  }
  function reconnect() {
    return get('liveReconnect', {}, { forceMode: 'live' });
  }
  function reloadAlertConfig() {
    return get('alertsReload', {}, { forceMode: 'live' });
  }

  /**
   * 等分析结束（自动轮询）。分析通常 5~25 秒（含 LLM），期间可拿 onTick 更新进度条。
   * @returns {Promise<分析状态对象>}
   */
  function waitAnalyze(onTick, opt) {
    opt = opt || {};
    var deadline = Date.now() + (opt.timeoutMs || 180000);
    return new Promise(function (resolve, reject) {
      var timer = setInterval(function () {
        analyzeStatus().then(function (st) {
          onTick && onTick(st);
          if (!st.running) {
            clearInterval(timer);
            if (st.error) { reject(new Error(st.error)); }
            else { resolve(st); }
            return;
          }
          if (Date.now() > deadline) {
            clearInterval(timer);
            reject(new Error('分析超时'));
          }
        }, function (e) {
          clearInterval(timer);
          reject(e);
        });
      }, opt.intervalMs || C.poll.analyze);
    });
  }

  /* ── 导出 ─────────────────────────────────────────────────────────── */
  global.AGRILOG_CLIENT = {
    get: get,
    poll: poll,
    streamLiveLogs: streamLiveLogs,
    normalizeLiveStatus: normalizeLiveStatus,
    startAnalyze: startAnalyze,
    analyzeStatus: analyzeStatus,
    waitAnalyze: waitAnalyze,
    reconnect: reconnect,
    reloadAlertConfig: reloadAlertConfig,
    state: state,
    describeSource: describeSource,
    // 调试用：打印当前生效的契约摘要
    debug: function () {
      return {
        mode: DS.mode,
        baseUrl: DS.baseUrl || '(同源)',
        mockDir: DS.mockDir,
        endpoints: Object.keys(C.endpoints).length,
        lastSource: state.lastSource,
        lastError: state.lastError
      };
    }
  };
})(typeof window !== 'undefined' ? window : globalThis);
