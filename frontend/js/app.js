/* ============================================================
   OpenChart Pro - 主应用逻辑
   ============================================================ */

/* ---------- 全局状态 ---------- */
window.currentMarket = 'crypto';
window.currentSymbol = 'BTC-USDT';
window.currentInterval = '1H';

const MARKETS = {
  crypto: { label: '加密货币', defaultSymbol: 'BTC-USDT', currency: 'USDT', intervals: ['1m','5m','15m','30m','1H','4H','1D','1W','1M'] },
  us:     { label: '美股',     defaultSymbol: 'AAPL',     currency: 'USD',  intervals: ['1m','5m','15m','30m','1H','4H','1D','1W','1M'] },
  hk:     { label: '港股',     defaultSymbol: '0700.HK',  currency: 'HKD',  intervals: ['1m','5m','15m','30m','1H','1D','1W','1M'] },
  a:      { label: 'A股',      defaultSymbol: '600519',   currency: 'CNY',  intervals: ['5m','15m','30m','1H','1D','1W','1M'] },
  forex:  { label: '外汇',     defaultSymbol: 'EUR-USD',  currency: 'USD',  intervals: ['1m','5m','15m','30m','1H','4H','1D','1W','1M'] },
};

let symbolsCache = {};   // market → [{ symbol, name, market }]
let isFullscreen = false;

/* ============================================================
   初始化
   ============================================================ */
document.addEventListener('DOMContentLoaded', async () => {
  // 安全调用：即使某个模块失败也不影响其他模块
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
  safeInit('Search', () => typeof Search !== 'undefined' && Search.init());
  safeInit('Settings', () => typeof Settings !== 'undefined' && Settings.init());
  safeInit('Indicators', () => typeof Indicators !== 'undefined' && Indicators.init());
  safeInit('Alerts', () => typeof Alerts !== 'undefined' && Alerts.init());
  safeInit('Backtest', () => typeof Backtest !== 'undefined' && Backtest.init());
  safeInit('Screener', () => typeof Screener !== 'undefined' && Screener.init());
  safeInit('Dashboard', () => typeof Dashboard !== 'undefined' && Dashboard.init());
  safeInit('Watchlist', () => typeof Watchlist !== 'undefined' && Watchlist.init());
  safeInit('Formula', () => typeof Formula !== 'undefined' && Formula.init());

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
      loadMarketSymbols('a'),
    ]);
  } catch(e) {
    console.warn('[App] 加载品种列表失败:', e);
  }

  // 连接 WebSocket
  try {
    if (typeof ws !== 'undefined' && ws) {
      ws.on('kline', (data) => {
        if (data.symbol === window.currentSymbol) {
          updateCandle(data);
          updateInfoPanel(data);
        }
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
// 前端market值到后端market值的映射
function toApiMarket(m) {
  return m === 'a' ? 'cn' : m;
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
    });
  });

  // 收起/展开
  document.getElementById('bottom-toggle')?.addEventListener('click', toggleBottomPanel);
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

    // Ctrl+S - 保存公式
    if (e.key === 's' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      Formula.save();
      return;
    }

    // Ctrl+Enter - 运行公式
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      Formula.run();
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

  // 底部分隔线
  const bottomHandle = document.getElementById('resize-bottom');
  if (bottomHandle) {
    bindHorizontalResize(bottomHandle, 'bottom-panel', 100, 600);
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

function bindHorizontalResize(handle, panelId, min, max) {
  let startY, startHeight;
  const panel = document.getElementById(panelId);
  if (!panel) return;

  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    startY = e.clientY;
    startHeight = panel.offsetHeight;
    handle.classList.add('active');

    const onMove = (e) => {
      const diff = startY - e.clientY;
      const newHeight = Math.min(max, Math.max(min, startHeight + diff));
      panel.style.height = newHeight + 'px';
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
