/* ============================================================
   OpenChart Pro - 主应用逻辑
   ============================================================ */

/* ---------- 全局状态 ---------- */
window.currentMarket = 'crypto';
window.currentSymbol = 'BTC-USDT';
window.currentInterval = '1H';

// 市场配置（key 与后端 Market 枚举一致：crypto/us/hk/cn）
// 参考 PRD §1.5 + §1.7 加密/股票功能边界矩阵
const MARKETS = {
  crypto: { label: '加密货币', defaultSymbol: 'BTC-USDT', currency: 'USDT', intervals: ['1m','5m','15m','30m','1H','4H','1D','1W','1M'] },
  us:     { label: '美股',     defaultSymbol: 'AAPL',     currency: 'USD',  intervals: ['5m','15m','30m','1H','4H','1D','1W','1M'] },  // PRD: 美股隐藏 1m
  hk:     { label: '港股',     defaultSymbol: '0700.HK',  currency: 'HKD',  intervals: ['5m','15m','30m','1H','1D','1W','1M'] },       // PRD: 港股隐藏 1m
  cn:     { label: 'A股',      defaultSymbol: '600519',   currency: 'CNY',  intervals: ['5m','15m','30m','1H','1D','1W','1M'] },       // PRD: A股隐藏 1m 和 4H
};

let symbolsCache = {};   // market → [{ symbol, name, market }]
let isFullscreen = false;

/* ============================================================
   初始化
   ============================================================ */
document.addEventListener('DOMContentLoaded', async () => {
  // ════════════════════════════════════════════════════════════
  // ?safe=1: 只跑 K 线
  // ?verbose=1: 打开详细 console.log（默认关闭，避免 DevTools 累积消息卡死）
  // ════════════════════════════════════════════════════════════
  const _qs = new URLSearchParams(location.search);
  const SAFE_MODE = _qs.get('safe') === '1';
  const VERBOSE = _qs.get('verbose') === '1';
  window.__SAFE_MODE = SAFE_MODE;
  // console.log 分级：
  // - 默认：保留前缀白名单的 log（[Chart] / [loadMore] / [PW-HOOK] / [App] 等开发标记）
  //   以及所有 console.warn / console.error —— 这样关键 bug 能被看到
  // - ?verbose=1：全部打印
  // - 非白名单的 log 被吞掉（防海量 log 卡 DevTools）
  const _origLog = console.log;
  window.__origLog = _origLog;  // 需要时可调 window.__origLog(...) 强制打印
  if (!VERBOSE) {
    const KEY_PREFIXES = ['[Chart]', '[loadMore]', '[App]', '[PW-HOOK]', '[Portfolio]',
                          '[Signals]', '[WS]', '[News]', '[Pool]', '[Alert]', '[Trade]'];
    console.log = function(...args) {
      if (args.length && typeof args[0] === 'string') {
        for (const p of KEY_PREFIXES) {
          if (args[0].indexOf(p) >= 0) { _origLog.apply(console, args); return; }
        }
        // 形如 '%c[chart.js] xxx' 格式（带 CSS 样式）
        if (args[0].startsWith('%c') && args.length > 1) {
          for (const p of KEY_PREFIXES) {
            if (args[0].indexOf(p) >= 0) { _origLog.apply(console, args); return; }
          }
        }
      }
      /* 其他 log 静默 */
    };
  }
  if (SAFE_MODE) {
    console.warn('🛡️ SAFE MODE 启用：只跑 K 线，关掉 WS、新闻、信号、候选池、持仓等');
  }

  function safeInit(name, fn) {
    try { fn(); } catch(e) { console.error(`[App] ${name} 初始化失败:`, e); }
  }

  // 初始化图表（最关键）
  try {
    if (typeof klinecharts !== 'undefined') {
      initChart();
      console.log('[App] KLineChart 初始化成功');
    } else {
      console.error('[App] KLineChart 库未加载！请检查网络连接');
      showToast('K线图表库加载失败，请刷新页面重试', 'error');
    }
  } catch(e) {
    console.error('[App] 图表初始化失败:', e);
  }

  // 初始化各模块
  // ════════════════════════════════════════════════════════════
  // 防卡死四道防线（永久解决长时间使用/挂机后页面无响应）
  // ════════════════════════════════════════════════════════════
  // 用 Set 保证 handler 不重复注册（模块重复 init 时不会累积）
  window.__visibilityHandlers = window.__visibilityHandlers || new Set();
  // 兼容老代码：提供 push 方法转 add
  if (Array.isArray(window.__visibilityHandlers)) {
    const old = window.__visibilityHandlers;
    window.__visibilityHandlers = new Set(old);
  }
  if (!window.__visibilityHandlers.push) {
    window.__visibilityHandlers.push = function(fn) { this.add(fn); };
  }
  let _hiddenSince = 0;
  const AUTO_RELOAD_THRESHOLD_MS = 30 * 60 * 1000;   // 挂机 30 分钟回来 → reload
  const FORCED_RELOAD_INTERVAL_MS = 4 * 60 * 60 * 1000; // 每 4h 强制 reload 一次
  const WATCHDOG_INTERVAL_MS = 30 * 1000;            // 每 30s 心跳
  const WATCHDOG_DEAD_THRESHOLD_MS = 60 * 1000;      // 60s 没心跳 → 主线程死了（更快 reload）
  const HEAP_WARN_MB = 300;                           // JS 堆超 300MB → reload（降低阈值更敏感）

  // 防线 1：可见性 + 长挂机 reload
  document.addEventListener('visibilitychange', () => {
    const hidden = document.hidden;
    if (hidden) {
      _hiddenSince = Date.now();
    } else {
      const idleMs = _hiddenSince > 0 ? Date.now() - _hiddenSince : 0;
      _hiddenSince = 0;
      if (idleMs >= AUTO_RELOAD_THRESHOLD_MS) {
        console.log(`[App] 挂机 ${Math.round(idleMs/60000)} 分钟，自动 reload`);
        location.reload();
        return;
      }
    }
    // Set 迭代安全（即使 handler 内部修改集合也不会崩）
    for (const fn of Array.from(window.__visibilityHandlers)) {
      try { fn({ hidden }); } catch (e) { console.warn('[Visibility] handler 异常:', e); }
    }
  });

  // 防线 2：定时强制 reload（4 小时一次，无论是否在用 — 在隐藏时立即执行避免打扰）
  setTimeout(function scheduleForceReload() {
    if (document.hidden) {
      location.reload();
    } else {
      console.log('[App] 已运行 4 小时，等下次隐藏时自动 reload');
      const onHide = () => {
        if (document.hidden) {
          document.removeEventListener('visibilitychange', onHide);
          location.reload();
        }
      };
      document.addEventListener('visibilitychange', onHide);
      // 兜底：如果用户 1 小时不离开，强制 reload（即使前台）
      setTimeout(() => location.reload(), 60 * 60 * 1000);
    }
  }, FORCED_RELOAD_INTERVAL_MS);

  // 防线 3：Watchdog 主线程心跳。把心跳写到 sessionStorage，独立 worker 监测。
  // 因为单线程 JS 一旦卡住没法自检，用 Web Worker 旁路监测。
  try {
    const watchdogCode = `
      let lastBeat = Date.now();
      self.onmessage = (e) => {
        if (e.data === 'beat') lastBeat = Date.now();
      };
      setInterval(() => {
        const gap = Date.now() - lastBeat;
        if (gap > ${WATCHDOG_DEAD_THRESHOLD_MS}) {
          self.postMessage({ type: 'dead', gap });
        }
      }, ${WATCHDOG_INTERVAL_MS});
    `;
    const blob = new Blob([watchdogCode], { type: 'application/javascript' });
    const watchdog = new Worker(URL.createObjectURL(blob));
    watchdog.onmessage = (e) => {
      if (e.data && e.data.type === 'dead') {
        console.error(`[Watchdog] 主线程卡死 ${Math.round(e.data.gap/1000)}s，强制 reload`);
        location.reload();
      }
    };
    setInterval(() => watchdog.postMessage('beat'), WATCHDOG_INTERVAL_MS);
    console.log('[Watchdog] 主线程心跳监测已启动');

    // Long Task Observer：捕获所有 >50ms 的同步任务，定位卡顿真凶
    if (typeof PerformanceObserver !== 'undefined') {
      try {
        const longTaskObs = new PerformanceObserver((list) => {
          for (const entry of list.getEntries()) {
            if (entry.duration > 50) {
              const attr = entry.attribution && entry.attribution[0];
              const culprit = attr ? `${attr.name}/${attr.containerName||attr.containerId||attr.containerSrc||'?'}` : 'unknown';
              console.error(`🐢 LONG TASK ${entry.duration.toFixed(0)}ms @ ${entry.startTime.toFixed(0)}ms | culprit=${culprit}`);
              // 同时打 stack trace 到 console 帮助定位
              console.trace(`LongTask trace`);
            }
          }
        });
        longTaskObs.observe({ entryTypes: ['longtask'] });
        console.log('[LongTask] 已启用：>50ms 任务会在 Console 红字打印 + stack trace');
      } catch (e) {
        console.warn('[LongTask] PerformanceObserver 不可用:', e);
      }
    }
    // heartbeat 移到 5 分钟一次（之前 30s 太频繁，console.log 本身有开销）
    setInterval(() => {
      const memMB = performance.memory ? (performance.memory.usedJSHeapSize / 1024 / 1024).toFixed(0) : '?';
      console.log(`[heartbeat] ${new Date().toLocaleTimeString()} heap=${memMB}MB`);
    }, 300000);

    // 慢回调诊断（轻量版，只在 localStorage，避免 sessionStorage 跨标签丢失）
    // 包装 setTimeout/setInterval 回调，> 1 秒的同步执行写日志
    const _origSetTimeout = window.setTimeout.bind(window);
    const _origSetInterval = window.setInterval.bind(window);
    function _wrapCallback(cb, name) {
      if (typeof cb !== 'function') return cb;
      return function wrapped() {
        const t0 = performance.now();
        try {
          return cb.apply(this, arguments);
        } finally {
          const dt = performance.now() - t0;
          if (dt > 1000) {  // 1 秒以上才记录（500ms 太敏感）
            try {
              const logs = JSON.parse(localStorage.getItem('__slowCallbacks') || '[]');
              logs.push({ t: Date.now(), dt: Math.round(dt), name, stack: new Error().stack.split('\n').slice(1,5).join('\n') });
              if (logs.length > 20) logs.shift();
              localStorage.setItem('__slowCallbacks', JSON.stringify(logs));
            } catch {}
          }
        }
      };
    }
    window.setTimeout = function(cb, delay, ...args) {
      return _origSetTimeout(_wrapCallback(cb, 'setTimeout'), delay, ...args);
    };
    window.setInterval = function(cb, delay, ...args) {
      return _origSetInterval(_wrapCallback(cb, 'setInterval'), delay, ...args);
    };
    // 上次会话的慢回调
    try {
      const slow = localStorage.getItem('__slowCallbacks');
      if (slow) {
        const logs = JSON.parse(slow);
        if (logs.length > 0) {
          console.warn(`🔴 历史慢回调（>1s）共 ${logs.length} 条:`);
          logs.slice(-10).forEach(l => {
            console.warn(`  [${new Date(l.t).toLocaleTimeString()}] ${l.name} ${l.dt}ms\n${l.stack}`);
          });
        }
      }
    } catch {}

    // 事件录像机（节流写 storage，每 5 秒最多写一次，避免高频写阻塞）
    window.__eventLog = [];
    let _logFlushPending = false;
    window.__logEvent = (type, detail) => {
      try {
        window.__eventLog.push({ t: Date.now(), type, detail });
        if (window.__eventLog.length > 100) window.__eventLog.shift();
        if (_logFlushPending) return;
        _logFlushPending = true;
        setTimeout(() => {
          _logFlushPending = false;
          try { sessionStorage.setItem('__lastEvents', JSON.stringify(window.__eventLog)); } catch {}
        }, 5000);
      } catch {}
    };
    // 卡死前最后日志：刷新页面后自动打印
    try {
      const last = sessionStorage.getItem('__lastEvents');
      if (last) {
        console.warn('🎬 上次会话最后事件录像（卡死前）:');
        console.table(JSON.parse(last));
      }
    } catch {}

    // 全局错误捕获
    window.addEventListener('error', (e) => {
      console.error(`💥 Global Error: ${e.message} @ ${e.filename}:${e.lineno}:${e.colno}`, e.error);
    });
    window.addEventListener('unhandledrejection', (e) => {
      console.error(`💥 Unhandled Promise Rejection:`, e.reason);
    });
  } catch (e) {
    console.warn('[Watchdog] Worker 不可用:', e);
  }

  // 防线 4：内存监控（Chrome 才有 performance.memory）
  if (performance && performance.memory) {
    setInterval(() => {
      const usedMB = performance.memory.usedJSHeapSize / 1024 / 1024;
      if (usedMB > HEAP_WARN_MB) {
        console.warn(`[Memory] JS 堆 ${usedMB.toFixed(0)} MB > ${HEAP_WARN_MB} MB，自动 reload 释放内存`);
        location.reload();
      }
    }, 60 * 1000);
  }

  // 实测 GPU 不是问题（289MB 正常），删除这个周期 reload（它本身在烧 CPU）
  // 保留作为伪代码注释；如再卡再启用
  /* setInterval(() => {
    try {
      if (typeof chart !== 'undefined' && chart && typeof chart.clearData === 'function') {
        const sym = window.currentSymbol;
        if (sym && typeof loadKlines === 'function') {
          chart.clearData();
          loadKlines(sym, window.currentInterval, window.currentMarket);
        }
      }
    } catch (e) {}
  }, 10 * 60 * 1000); */

  safeInit('Search', () => typeof Search !== 'undefined' && Search.init());
  safeInit('Settings', () => typeof Settings !== 'undefined' && Settings.init());
  safeInit('Indicators', () => typeof Indicators !== 'undefined' && Indicators.init());
  // 关键模块（chart 用户立即看到，必须同步初始化）
  safeInit('Watchlist', () => typeof Watchlist !== 'undefined' && Watchlist.init());

  if (SAFE_MODE) {
    console.warn('🛡️ SAFE MODE: 跳过所有底部面板初始化');
    return;  // 不再加载 News/Signals/Portfolio/Watchpool/Dashboard 等
  }

  // 非关键模块（底部面板、菜单弹窗等）→ 用 requestIdleCallback 错开，避免一次性 7 秒卡死
  const lazyInits = [
    ['Alerts', () => typeof Alerts !== 'undefined' && Alerts.init()],
    ['Backtest', () => typeof Backtest !== 'undefined' && Backtest.init()],
    ['Screener', () => typeof Screener !== 'undefined' && Screener.init()],
    ['Dashboard', () => typeof Dashboard !== 'undefined' && Dashboard.init()],
    ['News',       () => typeof News       !== 'undefined' && News.init()],
    ['Watchpool',  () => typeof Watchpool  !== 'undefined' && Watchpool.init()],
    ['Signals',    () => typeof Signals    !== 'undefined' && Signals.init()],
    ['Crypto',     () => typeof Crypto     !== 'undefined' && Crypto.init()],
    ['Portfolio',  () => typeof Portfolio  !== 'undefined' && Portfolio.init()],
    ['Review',     () => typeof Review     !== 'undefined' && Review.init()],
    ['Formula', () => typeof Formula !== 'undefined' && Formula.init()],
    ['ChanlunVerdict', () => typeof ChanlunVerdict !== 'undefined' && ChanlunVerdict.init()],
  ];
  const idle = window.requestIdleCallback || ((cb) => setTimeout(cb, 50));
  function runLazyInit(idx) {
    if (idx >= lazyInits.length) return;
    idle(() => {
      const [name, fn] = lazyInits[idx];
      safeInit(name, fn);
      runLazyInit(idx + 1);
    });
  }
  runLazyInit(0);

  // 绑定事件
  safeInit('bindToolbar', bindToolbar);
  safeInit('bindIntervalButtons', bindIntervalButtons);
  safeInit('bindBottomPanel', bindBottomPanel);
  safeInit('bindDrawingTools', bindDrawingTools);
  safeInit('bindKeyboardShortcuts', bindKeyboardShortcuts);
  safeInit('bindResizeHandles', bindResizeHandles);

  // 加载所有市场品种数据
  try {
    await Promise.allSettled([
      loadMarketSymbols('crypto'),
      loadMarketSymbols('us'),
      loadMarketSymbols('hk'),
      loadMarketSymbols('cn'),
    ]);
  } catch(e) {
    console.warn('[App] 加载品种列表失败:', e);
  }

  // SAFE MODE 下不连 WS（避免 WS 消息洪流）
  if (SAFE_MODE) {
    console.warn('🛡️ SAFE MODE: WebSocket 不连接');
    return;
  }

  // 连接 WebSocket（K 线 tick 节流：200ms 内多次推送只用最后一条）
  try {
    if (typeof ws !== 'undefined' && ws) {
      let _lastKlineUpdate = 0;
      let _pendingKline = null;
      let _klineTimer = null;
      ws.on('kline', (data) => {
        if (data.symbol !== window.currentSymbol) return;
        _pendingKline = data;
        if (_klineTimer) return;
        const now = Date.now();
        const elapsed = now - _lastKlineUpdate;
        const delay = elapsed >= 200 ? 0 : (200 - elapsed);
        _klineTimer = setTimeout(() => {
          _klineTimer = null;
          _lastKlineUpdate = Date.now();
          const d = _pendingKline;
          _pendingKline = null;
          if (!d) return;
          try { updateCandle(d); } catch (e) { console.warn('[Kline] updateCandle 异常:', e); }
          try { updateInfoPanel(d); } catch (e) { console.warn('[Kline] updateInfoPanel 异常:', e); }
        }, delay);
      });
      ws.connect();
    }
  } catch(e) {
    console.warn('[App] WebSocket连接失败:', e);
  }

  // 加载默认品种K线
  try {
    await loadKlines(window.currentSymbol, window.currentInterval, window.currentMarket);
    if (typeof ws !== 'undefined' && ws) {
      ws.subscribe(window.currentSymbol, window.currentInterval);
    }
  } catch(e) {
    console.warn('[App] 加载K线失败:', e);
  }

  // 更新标题
  document.title = `${window.currentSymbol} - OpenChart Pro`;

  // 控制台日志
  appendConsole('系统启动完成', 'info');
  appendConsole(`当前市场: ${window.currentMarket}, 品种: ${window.currentSymbol}, 周期: ${window.currentInterval}`, 'info');
  appendConsole('提示: 按 / 搜索品种, 数字键1-9切换周期, Alt+A创建警报', 'info');

  console.log('[App] 初始化完成');
});

/* ---------- 控制台输出 ---------- */
function appendConsole(msg, type) {
  const output = document.querySelector('.console-output');
  if (!output) return;
  const line = document.createElement('div');
  line.className = 'console-line';
  const time = new Date().toLocaleTimeString('zh-CN');
  const colors = { info: 'var(--color-accent)', warn: 'var(--color-warning)', error: 'var(--color-down)', success: 'var(--color-up)' };
  line.innerHTML = `<span style="color:var(--text-tertiary);margin-right:8px;">[${time}]</span><span style="color:${colors[type] || 'var(--text-secondary)'};">${msg}</span>`;
  output.appendChild(line);
  output.scrollTop = output.scrollHeight;
}
window.appendConsole = appendConsole;

/* ============================================================
   市场与品种
   ============================================================ */
// 前后端 market 值统一为 crypto/us/hk/cn（已与 TDD 对齐，此函数保留作向后兼容）
function toApiMarket(m) {
  return m === 'a' ? 'cn' : m;  // 兼容旧代码中可能残留的 'a'
}

async function loadMarketSymbols(market) {
  if (symbolsCache[market]) {
    if (typeof Search !== 'undefined') Search.setSymbols(symbolsCache[market]);
    return;
  }
  try {
    const apiMkt = toApiMarket(market);
    const resp = await fetch(`/api/symbols?market=${apiMkt}`);
    if (resp.ok) {
      const data = await resp.json();
      symbolsCache[market] = data.symbols || data.data || data;
      if (typeof Search !== 'undefined') Search.setSymbols(symbolsCache[market]);
    }
  } catch (e) {
    console.warn('[App] 加载品种列表失败:', e);
  }
}

async function switchMarket(market) {
  console.log('[App] switchMarket:', market);
  if (!MARKETS[market]) return;
  window.currentMarket = market;
  const cfg = MARKETS[market];

  // 更新周期按钮可见性（先做，避免切换品种时用了被隐藏的周期）
  document.querySelectorAll('.interval-btn').forEach(btn => {
    btn.style.display = cfg.intervals.includes(btn.dataset.interval) ? '' : 'none';
  });

  // 如果当前周期不在新市场支持的列表中，切到1H
  if (!cfg.intervals.includes(window.currentInterval)) {
    window.currentInterval = '1H';
    document.querySelectorAll('.interval-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.interval === '1H');
    });
  }

  // 切换默认品种并加载K线
  try {
    await switchSymbol(cfg.defaultSymbol, market);
  } catch(e) {
    console.error('[App] switchMarket failed:', e);
  }

  // 刷新品种列表
  try { loadMarketSymbols(market); } catch(e) {}

  // 刷新自选列表
  try { if (typeof Watchlist !== 'undefined') Watchlist.render(); } catch(e) {}

  // 加密市场不使用候选池：隐藏 tab + pane；自动切到默认 tab（新闻）若当前在候选池
  try {
    const wpTab = document.querySelector('.bottom-tab[data-tab="watchpool"]');
    const wpPane = document.querySelector('.bottom-pane[data-pane="watchpool"]');
    if (market === 'crypto') {
      if (wpTab) wpTab.style.display = 'none';
      if (wpPane) wpPane.style.display = 'none';
      if (wpTab && wpTab.classList.contains('active')) {
        const newsTab = document.querySelector('.bottom-tab[data-tab="news"]');
        if (newsTab) newsTab.click();
      }
    } else {
      if (wpTab) wpTab.style.display = '';
      if (wpPane) wpPane.style.display = '';
    }
  } catch (e) { console.warn('[App] watchpool tab toggle:', e); }

  // 新闻面板按市场过滤同步刷新
  try {
    if (typeof News !== 'undefined' && typeof News.setMarketFilter === 'function') {
      News.setMarketFilter(market);
    }
  } catch (e) { console.warn('[App] news market sync:', e); }

  showToast(`已切换到 ${cfg.label} 市场`, 'info', 2000);
}

/* ============================================================
   工具栏绑定
   ============================================================ */
function bindToolbar() {
  // 市场下拉
  const marketSelect = document.getElementById('market-select');
  if (marketSelect) {
    marketSelect.addEventListener('change', (e) => switchMarket(e.target.value));
  }

  // 搜索框
  const searchInput = document.getElementById('toolbar-search-input');
  if (searchInput) {
    searchInput.addEventListener('focus', () => Search.open());
    searchInput.addEventListener('click', () => Search.open());
  }

  // 工具栏按钮
  document.getElementById('btn-indicators')?.addEventListener('click', () => Indicators.open());
  document.getElementById('btn-drawing')?.addEventListener('click', toggleDrawingPanel);
  document.getElementById('btn-alerts')?.addEventListener('click', () => Alerts.open());
  document.getElementById('btn-backtest')?.addEventListener('click', () => { switchBottomTab('backtest'); expandBottomPanel(); });
  document.getElementById('btn-screener')?.addEventListener('click', () => { switchBottomTab('screener'); expandBottomPanel(); if (typeof Screener !== 'undefined') Screener.autoRefreshIfNeeded(); });
  document.getElementById('btn-dashboard')?.addEventListener('click', () => { switchBottomTab('dashboard'); expandBottomPanel(); if (typeof Dashboard !== 'undefined') Dashboard.loadAll(); });
  document.getElementById('btn-settings')?.addEventListener('click', () => Settings.open());
}

/* ============================================================
   时间周期
   ============================================================ */
function bindIntervalButtons() {
  document.querySelectorAll('.interval-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      switchInterval(btn.dataset.interval);
    });
  });
}

/* ============================================================
   底部面板
   ============================================================ */
function bindBottomPanel() {
  // 标签页切换
  document.querySelectorAll('.bottom-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      const tabName = tab.dataset.tab;
      switchBottomTab(tabName);
      // 切换到仪表盘时自动加载数据
      if (tabName === 'dashboard' && typeof Dashboard !== 'undefined') {
        Dashboard.loadAll();
      }
      // 切换到选股时自动刷新
      if (tabName === 'screener' && typeof Screener !== 'undefined') {
        Screener.autoRefreshIfNeeded();
      }
      // 切换到缠论研判时自动分析
      if (tabName === 'chanlun-verdict' && typeof ChanlunVerdict !== 'undefined') {
        ChanlunVerdict.analyze(window.currentSymbol, window.currentMarket === 'a' ? 'cn' : window.currentMarket);
      }
    });
  });

  // 收起/展开
  document.getElementById('bottom-toggle')?.addEventListener('click', toggleBottomPanel);
  // 最大化 / 半屏 快捷按钮
  const panel = document.getElementById('bottom-panel');
  document.getElementById('bottom-maximize')?.addEventListener('click', () => {
    if (!panel) return;
    panel.classList.remove('collapsed');
    const target = Math.floor(window.innerHeight * 0.8);
    panel.style.height = target + 'px';
    try { localStorage.setItem('bottom_panel_height', String(target)); } catch {}
    if (chart) chart.resize();
  });
  document.getElementById('bottom-half')?.addEventListener('click', () => {
    if (!panel) return;
    panel.classList.remove('collapsed');
    const target = Math.floor(window.innerHeight * 0.5);
    panel.style.height = target + 'px';
    try { localStorage.setItem('bottom_panel_height', String(target)); } catch {}
    if (chart) chart.resize();
  });
  // 启动时恢复用户上次设定的高度
  try {
    const saved = parseInt(localStorage.getItem('bottom_panel_height') || '', 10);
    if (saved && saved > 80 && panel) {
      const clamped = Math.min(saved, Math.floor(window.innerHeight * 0.85));
      panel.style.height = clamped + 'px';
    }
  } catch {}
  // 窗口 resize 时夹紧：避免保存的高度 > 新屏幕
  let _resizeTimer = null;
  window.addEventListener('resize', () => {
    if (_resizeTimer) return;
    _resizeTimer = setTimeout(() => {
      _resizeTimer = null;
      if (!panel || panel.classList.contains('collapsed')) return;
      const cap = Math.floor(window.innerHeight * 0.85);
      if (panel.offsetHeight > cap) {
        panel.style.height = cap + 'px';
        try { localStorage.setItem('bottom_panel_height', String(cap)); } catch {}
      }
      if (chart) chart.resize();
    }, 200);
  });
}

function switchBottomTab(tabName) {
  document.querySelectorAll('.bottom-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === tabName);
  });
  document.querySelectorAll('.bottom-pane').forEach(p => {
    p.classList.toggle('active', p.dataset.pane === tabName);
  });
}

function toggleBottomPanel() {
  const panel = document.getElementById('bottom-panel');
  if (!panel) return;
  panel.classList.toggle('collapsed');
  const btn = document.getElementById('bottom-toggle');
  if (btn) btn.textContent = panel.classList.contains('collapsed') ? '▲' : '▼';
  // 触发图表 resize
  setTimeout(() => { if (chart) chart.resize(); }, 300);
}

function expandBottomPanel() {
  const panel = document.getElementById('bottom-panel');
  if (panel && panel.classList.contains('collapsed')) {
    panel.classList.remove('collapsed');
    const btn = document.getElementById('bottom-toggle');
    if (btn) btn.textContent = '▼';
    setTimeout(() => { if (chart) chart.resize(); }, 300);
  }
}

// F 键三态切换：split (50/50) → chart-only (面板折叠) → panel-only (面板占大头)
let _focusMode = 'split';  // split | chart | panel
function cycleFocusMode() {
  const panel = document.getElementById('bottom-panel');
  if (!panel) return;
  if (_focusMode === 'split') {
    // → chart only
    _focusMode = 'chart';
    panel.classList.add('collapsed');
    panel.style.removeProperty('height');
    if (typeof showToast === 'function') showToast('🖥 专注 K 线图（再按 F 切到数据面板）', 'info', 1500);
  } else if (_focusMode === 'chart') {
    // → panel only（面板 80vh，K 线只剩 20vh）
    _focusMode = 'panel';
    panel.classList.remove('collapsed');
    panel.style.height = '80vh';
    if (typeof showToast === 'function') showToast('📊 专注数据面板（再按 F 回到分屏）', 'info', 1500);
  } else {
    // → split
    _focusMode = 'split';
    panel.classList.remove('collapsed');
    panel.style.removeProperty('height');
    if (typeof showToast === 'function') showToast('⊟ 分屏模式', 'info', 1500);
  }
  const btn = document.getElementById('bottom-toggle');
  if (btn) btn.textContent = panel.classList.contains('collapsed') ? '▲' : '▼';
  setTimeout(() => { if (chart) chart.resize(); }, 300);
}

// 全局快捷键监听（避免和输入框冲突）
document.addEventListener('keydown', (e) => {
  // 在 input/textarea/select 里按键不响应
  const tag = (e.target.tagName || '').toLowerCase();
  if (['input', 'textarea', 'select'].includes(tag) || e.target.isContentEditable) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === 'f' || e.key === 'F') {
    e.preventDefault();
    cycleFocusMode();
  }
});

/* ============================================================
   画线工具面板
   ============================================================ */
function bindDrawingTools() {
  // 画线下拉菜单项点击
  document.querySelectorAll('.drawing-dropdown .dd-item').forEach(item => {
    item.addEventListener('click', () => {
      const tool = item.dataset.tool;
      if (tool === 'clear-all') {
        if (chart) chart.removeOverlay();
        showToast('已清除所有画线', 'info', 2000);
      } else if (tool && chart) {
        chart.createOverlay(tool);
        showToast(`已选择画线工具: ${item.textContent.trim()}`, 'info', 2000);
      }
      closeDrawingPanel();
    });
  });

  // 点击其他区域关闭
  document.addEventListener('click', (e) => {
    const dd = document.getElementById('drawing-dropdown');
    const btn = document.getElementById('btn-drawing');
    if (dd && dd.classList.contains('show') && !dd.contains(e.target) && e.target !== btn) {
      dd.classList.remove('show');
    }
  });
}

function toggleDrawingPanel() {
  const dd = document.getElementById('drawing-dropdown');
  if (dd) dd.classList.toggle('show');
}

function closeDrawingPanel() {
  const dd = document.getElementById('drawing-dropdown');
  if (dd) dd.classList.remove('show');
}

/* ============================================================
   键盘快捷键
   ============================================================ */
function bindKeyboardShortcuts() {
  document.addEventListener('keydown', (e) => {
    // 如果焦点在输入框/textarea内，忽略大部分快捷键
    const tag = document.activeElement?.tagName;
    const isInput = tag === 'INPUT' || tag === 'TEXTAREA' || document.activeElement?.isContentEditable;

    // / - 搜索
    if (e.key === '/' && !isInput) {
      e.preventDefault();
      Search.open();
      return;
    }

    // Esc - 关闭弹窗
    if (e.key === 'Escape') {
      if (Search.isOpen()) { Search.close(); return; }
      closeDrawingPanel();
      // 关闭所有模态框
      document.querySelectorAll('.modal-overlay.show').forEach(m => m.classList.remove('show'));
      return;
    }

    if (isInput) return;

    // 数字键 1-9 切换周期
    const intervals = ['1m','5m','15m','30m','1H','4H','1D','1W','1M'];
    const num = parseInt(e.key);
    if (num >= 1 && num <= 9 && !e.ctrlKey && !e.altKey && !e.metaKey) {
      e.preventDefault();
      if (intervals[num - 1]) switchInterval(intervals[num - 1]);
      return;
    }

    // Ctrl+S - 保存公式（v11.5: Formula 模块未实现，guard）
    if (e.key === 's' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      if (typeof Formula !== 'undefined') Formula.save();
      return;
    }

    // Ctrl+Enter - 运行公式（v11.5: 同上 guard）
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      if (typeof Formula !== 'undefined') Formula.run();
      return;
    }

    // Alt+A - 警报
    if (e.key === 'a' && e.altKey) {
      e.preventDefault();
      Alerts.open();
      return;
    }

    // Alt+I - 指标
    if (e.key === 'i' && e.altKey) {
      e.preventDefault();
      Indicators.open();
      return;
    }

    // Alt+D - 仪表盘
    if (e.key === 'd' && e.altKey) {
      e.preventDefault();
      switchBottomTab('dashboard');
      expandBottomPanel();
      return;
    }

    // Tab / Shift+Tab - 切换底部面板标签
    if (e.key === 'Tab' && !e.ctrlKey && !e.altKey) {
      e.preventDefault();
      cycleBottomTab(e.shiftKey ? -1 : 1);
      return;
    }

    // F11 - 全屏
    if (e.key === 'F11') {
      e.preventDefault();
      toggleFullscreen();
      return;
    }
  });
}

function cycleBottomTab(direction) {
  const tabs = Array.from(document.querySelectorAll('.bottom-tab'));
  const currentIdx = tabs.findIndex(t => t.classList.contains('active'));
  if (currentIdx < 0) return;
  const nextIdx = (currentIdx + direction + tabs.length) % tabs.length;
  switchBottomTab(tabs[nextIdx].dataset.tab);
}

function toggleFullscreen() {
  isFullscreen = !isFullscreen;
  document.getElementById('app').classList.toggle('fullscreen', isFullscreen);
  setTimeout(() => { if (chart) chart.resize(); }, 300);
  showToast(isFullscreen ? '已进入全屏模式 (F11 退出)' : '已退出全屏模式', 'info', 2000);
}

/* ============================================================
   信息面板更新
   ============================================================ */
function updateInfoPanel(data) {
  if (!data) return;
  const setTextById = (id, text) => {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  };

  setTextById('info-open', formatPrice(data.open || data.o));
  setTextById('info-high', formatPrice(data.high || data.h));
  setTextById('info-low', formatPrice(data.low || data.l));
  setTextById('info-close', formatPrice(data.close || data.c));
  setTextById('info-volume', formatVolume(data.volume || data.v));
  setTextById('info-change', formatChange(data.close || data.c, data.open || data.o));

  // 颜色
  const closeEl = document.getElementById('info-close');
  const changeEl = document.getElementById('info-change');
  if (closeEl && data.close && data.open) {
    const color = data.close >= data.open ? 'var(--color-up)' : 'var(--color-down)';
    closeEl.style.color = color;
    if (changeEl) changeEl.style.color = color;
  }
}

function formatPrice(p) {
  if (p == null) return '--';
  p = parseFloat(p);
  if (p >= 1000) return p.toFixed(2);
  if (p >= 1) return p.toFixed(4);
  return p.toFixed(6);
}

function formatVolume(v) {
  if (v == null) return '--';
  v = parseFloat(v);
  if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
  if (v >= 1e6) return (v / 1e6).toFixed(2) + 'M';
  if (v >= 1e3) return (v / 1e3).toFixed(2) + 'K';
  return v.toFixed(2);
}

function formatChange(close, open) {
  if (close == null || open == null || open === 0) return '--';
  const pct = ((close - open) / open * 100);
  return (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
}

/* ============================================================
   可拖拽分隔线
   ============================================================ */
function bindResizeHandles() {
  // 左侧分隔线（自选列表宽度）
  const leftHandle = document.getElementById('resize-left');
  if (leftHandle) {
    bindVerticalResize(leftHandle, 'watchlist-panel', 'left', 100, 300);
  }

  // 右侧分隔线（信息面板宽度）
  const rightHandle = document.getElementById('resize-right');
  if (rightHandle) {
    bindVerticalResize(rightHandle, 'info-panel', 'right', 160, 400);
  }

  // 底部分隔线：上限 = 屏幕 85%，下限 100px
  const bottomHandle = document.getElementById('resize-bottom');
  if (bottomHandle) {
    const dynMax = () => Math.floor(window.innerHeight * 0.85);
    bindHorizontalResize(bottomHandle, 'bottom-panel', 100, dynMax, 'bottom_panel_height');
  }
}

function bindVerticalResize(handle, panelId, side, min, max) {
  let startX, startWidth;
  const panel = document.getElementById(panelId);
  if (!panel) return;

  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    startX = e.clientX;
    startWidth = panel.offsetWidth;
    handle.classList.add('active');

    const onMove = (e) => {
      const diff = side === 'left' ? (e.clientX - startX) : (startX - e.clientX);
      const newWidth = Math.min(max, Math.max(min, startWidth + diff));
      panel.style.width = newWidth + 'px';
      panel.style.minWidth = newWidth + 'px';
    };
    const onUp = () => {
      handle.classList.remove('active');
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      if (chart) chart.resize();
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
}

function bindHorizontalResize(handle, panelId, min, max, storageKey) {
  let startY, startHeight;
  const panel = document.getElementById(panelId);
  if (!panel) return;

  const resolveMax = () => typeof max === 'function' ? max() : max;

  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    startY = e.clientY;
    startHeight = panel.offsetHeight;
    handle.classList.add('active');
    // 拖拽时如果处于 collapsed 状态自动展开
    panel.classList.remove('collapsed');

    const onMove = (e) => {
      const diff = startY - e.clientY;
      const newHeight = Math.min(resolveMax(), Math.max(min, startHeight + diff));
      panel.style.height = newHeight + 'px';
    };
    const onUp = () => {
      handle.classList.remove('active');
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      if (chart) chart.resize();
      // 记住用户设定的高度
      if (storageKey) {
        try { localStorage.setItem(storageKey, String(panel.offsetHeight)); } catch {}
      }
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
}

/* ============================================================
   右键菜单
   ============================================================ */
document.addEventListener('contextmenu', (e) => {
  const chartContainer = document.getElementById('chart-container');
  if (!chartContainer || !chartContainer.contains(e.target)) return;

  e.preventDefault();
  const menu = document.getElementById('context-menu');
  if (!menu) return;

  menu.style.left = e.clientX + 'px';
  menu.style.top = e.clientY + 'px';
  menu.classList.add('show');

  const closeMenu = () => {
    menu.classList.remove('show');
    document.removeEventListener('click', closeMenu);
  };
  setTimeout(() => document.addEventListener('click', closeMenu), 0);
});
