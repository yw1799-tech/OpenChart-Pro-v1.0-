/* ============================================================
   OpenChart Pro - KLineChart 初始化与管理
   ============================================================ */

let chart = null;
let mainPaneId = null;
const subPanes = [];        // 副图 pane 列表 { id, name }
const MAX_SUB_PANES = 4;

/* ---------- 暗色主题配置 ---------- */
const darkTheme = {
  grid: {
    show: true,
    horizontal: { show: true, size: 1, color: '#1C2333', style: 'dash', dashedValue: [2, 4] },
    vertical:   { show: true, size: 1, color: '#1C2333', style: 'dash', dashedValue: [2, 4] },
  },
  candle: {
    type: 'candle_solid',
    bar: {
      upColor: '#00C853',
      downColor: '#FF1744',
      noChangeColor: '#7D8590',
      upBorderColor: '#00C853',
      downBorderColor: '#FF1744',
      noChangeBorderColor: '#7D8590',
      upWickColor: '#00C853',
      downWickColor: '#FF1744',
      noChangeWickColor: '#7D8590',
    },
    priceMark: {
      show: true,
      high: { show: true, color: '#7D8590', textSize: 10 },
      low:  { show: true, color: '#7D8590', textSize: 10 },
      last: {
        show: true,
        upColor: '#00C853',
        downColor: '#FF1744',
        noChangeColor: '#7D8590',
        line: { show: true, style: 'dash', dashedValue: [4, 4], size: 1 },
        text: { show: true, size: 11, paddingLeft: 4, paddingTop: 3, paddingRight: 4, paddingBottom: 3, borderRadius: 2 },
      },
    },
    tooltip: {
      showRule: 'always',
      showType: 'standard',
      text: { size: 11, color: '#7D8590', marginLeft: 8, marginTop: 6, marginRight: 8, marginBottom: 0 },
    },
  },
  indicator: {
    lastValueMark: { show: true, text: { size: 10 } },
    tooltip: { showRule: 'always', text: { size: 11 } },
  },
  xAxis: {
    show: true,
    size: 'auto',
    axisLine: { show: true, color: '#30363D', size: 1 },
    tickLine: { show: true, size: 1, length: 3, color: '#30363D' },
    tickText: { show: true, color: '#7D8590', size: 11 },
  },
  yAxis: {
    show: true,
    size: 'auto',
    position: 'right',
    type: 'normal',
    inside: false,
    axisLine: { show: true, color: '#30363D', size: 1 },
    tickLine: { show: true, size: 1, length: 3, color: '#30363D' },
    tickText: { show: true, color: '#7D8590', size: 11 },
  },
  crosshair: {
    show: true,
    horizontal: {
      show: true,
      line: { show: true, style: 'dash', dashedValue: [4, 2], size: 1, color: '#4B5563' },
      text: { show: true, size: 11, color: '#E6EDF3', borderRadius: 2, paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2, backgroundColor: '#30363D' },
    },
    vertical: {
      show: true,
      line: { show: true, style: 'dash', dashedValue: [4, 2], size: 1, color: '#4B5563' },
      text: { show: true, size: 11, color: '#E6EDF3', borderRadius: 2, paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2, backgroundColor: '#30363D' },
    },
  },
  separator: { size: 2, color: '#30363D', activeBackgroundColor: 'rgba(33,150,243,0.15)' },
};

/* ---------- 图表初始化 ---------- */
function initChart() {
  if (typeof klinecharts === 'undefined') {
    console.error('[Chart] klinecharts 库未加载');
    return;
  }

  const container = document.getElementById('chart-container');
  if (!container) {
    console.error('[Chart] 未找到 #chart-container');
    return;
  }

  // 确保容器有尺寸
  if (container.clientHeight < 50) {
    container.style.height = '100%';
    container.style.minHeight = '400px';
  }

  chart = klinecharts.init(container, {
    styles: darkTheme,
    locale: 'zh-CN',
    customApi: {
      formatDate: (dateTimeFormat, timestamp, format, type) => {
        const d = new Date(timestamp);
        const pad = (n) => String(n).padStart(2, '0');
        if (type === 'xAxis') {
          return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
        }
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
      },
    },
  });

  // 默认添加成交量副图
  try {
    chart.createIndicator('VOL', false, { id: 'vol_pane', height: 80 });
    subPanes.push({ id: 'vol_pane', name: 'VOL' });
  } catch(e) {
    console.warn('[Chart] 添加成交量副图失败:', e);
  }

  console.log('[Chart] 初始化完成');
}

/* ---------- K线数据加载 ---------- */
async function loadKlines(symbol, interval, market) {
  if (!chart) return;

  // 自动推断market
  if (!market) market = window.currentMarket || 'crypto';
  // 前端用'a'表示A股，后端用'cn'
  const apiMarket = market === 'a' ? 'cn' : market;

  const loading = document.getElementById('chart-loading');
  if (loading) loading.classList.add('show');

  try {
    const resp = await fetch(`/api/klines?symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}&limit=500&market=${encodeURIComponent(apiMarket)}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    // 服务端返回 { candles: [...] } 或 { data: [...] } 或直接数组
    const raw = data.candles || data.data || data;
    if (!Array.isArray(raw) || raw.length === 0) {
      console.warn('[Chart] 无K线数据');
      return;
    }
    const klines = raw.map(k => ({
      timestamp: k.timestamp || k.time || k.t,
      open:      parseFloat(k.open  || k.o),
      high:      parseFloat(k.high  || k.h),
      low:       parseFloat(k.low   || k.l),
      close:     parseFloat(k.close || k.c),
      volume:    parseFloat(k.volume || k.v || 0),
      turnover:  parseFloat(k.turnover || k.amount || 0),
    }));

    chart.applyNewData(klines);

    // 更新水印
    const wm = document.getElementById('chart-watermark');
    if (wm) wm.textContent = symbol;

    // 用最后一根K线更新右侧信息面板
    if (klines.length > 0) {
      const last = klines[klines.length - 1];
      const prev = klines.length > 1 ? klines[klines.length - 2] : last;
      updateInfoPanelFromKline(last, prev);
      // 计算并显示指标值
      updateIndicatorValues(klines);
    }

    console.log(`[Chart] 已加载 ${klines.length} 根K线: ${symbol} ${interval}`);
  } catch (err) {
    console.error('[Chart] 加载K线失败:', err);
    showToast(`加载K线数据失败: ${err.message}`, 'error');
  } finally {
    if (loading) loading.classList.remove('show');
  }
}

/* ---------- 实时更新 ---------- */
function updateCandle(candleData) {
  if (!chart) return;
  chart.updateData({
    timestamp: candleData.timestamp || candleData.t,
    open:      parseFloat(candleData.open  || candleData.o),
    high:      parseFloat(candleData.high  || candleData.h),
    low:       parseFloat(candleData.low   || candleData.l),
    close:     parseFloat(candleData.close || candleData.c),
    volume:    parseFloat(candleData.volume || candleData.v || 0),
    turnover:  parseFloat(candleData.turnover || candleData.amount || 0),
  });
}

/* ---------- 切换 ---------- */
async function switchInterval(interval) {
  if (!window.currentSymbol) return;
  const oldInterval = window.currentInterval;
  window.currentInterval = interval;

  // 更新按钮状态
  document.querySelectorAll('.interval-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.interval === interval);
  });

  // 重新加载K线
  await loadKlines(window.currentSymbol, interval, window.currentMarket);

  // 切换 WebSocket 订阅
  if (ws && ws.ws) {
    ws.switch(window.currentSymbol, oldInterval, window.currentSymbol, interval);
  }
}

async function switchSymbol(symbol, market) {
  console.log('[Chart] switchSymbol:', symbol, 'market:', market);
  const oldSymbol = window.currentSymbol;
  const interval = window.currentInterval;
  window.currentSymbol = symbol;
  if (market) window.currentMarket = market;

  // 更新标题
  document.title = `${symbol} - OpenChart Pro`;

  // 重新加载K线
  try {
    await loadKlines(symbol, interval, window.currentMarket);
  } catch(e) {
    console.error('[Chart] switchSymbol loadKlines failed:', e);
  }

  // 切换 WebSocket 订阅
  try {
    if (typeof ws !== 'undefined' && ws && ws.ws && ws.ws.readyState === WebSocket.OPEN) {
      ws.switch(oldSymbol, interval, symbol, interval);
    }
  } catch(e) {
    console.warn('[Chart] WS switch failed:', e);
  }

  // 更新自选列表高亮
  document.querySelectorAll('.watchlist-item').forEach(item => {
    item.classList.toggle('active', item.dataset.symbol === symbol);
  });
}

/* ---------- 副图 Pane 管理 ---------- */
function addSubPane(name, indicatorName) {
  if (subPanes.length >= MAX_SUB_PANES) {
    showToast(`最多添加 ${MAX_SUB_PANES} 个副图`, 'warning');
    return null;
  }
  if (!chart) return null;

  const paneId = chart.createIndicator(indicatorName || name, false, { id: name.toLowerCase() + '_pane' });
  subPanes.push({ id: paneId, name });
  return paneId;
}

function removeSubPane(paneId) {
  if (!chart) return;
  chart.removeIndicator(paneId);
  const idx = subPanes.findIndex(p => p.id === paneId);
  if (idx !== -1) subPanes.splice(idx, 1);
}

/* ---------- 自定义指标注册框架 ---------- */
function registerCustomIndicator(config) {
  if (!klinecharts || !klinecharts.registerIndicator) return;
  try {
    klinecharts.registerIndicator(config);
    console.log(`[Chart] 已注册自定义指标: ${config.name}`);
  } catch (e) {
    console.error(`[Chart] 注册指标失败: ${config.name}`, e);
  }
}

/* ---------- 添加主图/副图指标 ---------- */
function addMainIndicator(name) {
  if (!chart) return;
  chart.createIndicator(name, true);
}

function addSubIndicator(name) {
  if (!chart) return;
  return addSubPane(name, name);
}

function removeIndicator(name, paneId) {
  if (!chart) return;
  chart.removeIndicator(paneId, name);
}

/* ---------- 从K线数据更新右侧信息面板 ---------- */
function updateInfoPanelFromKline(lastCandle, prevCandle) {
  console.log('[Chart] updateInfoPanelFromKline called', lastCandle);
  if (!lastCandle) return;

  function setVal(id, text, color) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    if (color) el.style.color = color;
  }

  function fmtPrice(p) {
    if (p == null || isNaN(p)) return '--';
    p = parseFloat(p);
    if (p >= 1000) return p.toLocaleString('en-US', {maximumFractionDigits: 2});
    if (p >= 1) return p.toFixed(4);
    return p.toFixed(6);
  }

  function fmtVol(v) {
    if (!v || isNaN(v)) return '--';
    v = parseFloat(v);
    if (v >= 1e9) return (v/1e9).toFixed(2) + 'B';
    if (v >= 1e6) return (v/1e6).toFixed(2) + 'M';
    if (v >= 1e3) return (v/1e3).toFixed(2) + 'K';
    return v.toFixed(2);
  }

  const o = lastCandle.open, h = lastCandle.high, l = lastCandle.low, c = lastCandle.close;
  const vol = lastCandle.volume;
  const prevClose = prevCandle ? prevCandle.close : o;
  const change = c - prevClose;
  const changePct = prevClose !== 0 ? (change / prevClose * 100) : 0;
  const upColor = 'var(--color-up)', downColor = 'var(--color-down)';
  const clr = change >= 0 ? upColor : downColor;

  setVal('info-open', fmtPrice(o));
  setVal('info-high', fmtPrice(h));
  setVal('info-low', fmtPrice(l));
  setVal('info-close', fmtPrice(c), clr);
  setVal('info-volume', fmtVol(vol));
  setVal('info-change', (change >= 0 ? '+' : '') + change.toFixed(2) + ' (' + (changePct >= 0 ? '+' : '') + changePct.toFixed(2) + '%)', clr);
}

/* ---------- 计算并更新右侧指标值 ---------- */
function updateIndicatorValues(klines) {
  if (!klines || klines.length < 20) return;

  const closes = klines.map(k => k.close);
  const n = closes.length;

  // 简单MA计算
  function sma(arr, period) {
    if (arr.length < period) return null;
    let sum = 0;
    for (let i = arr.length - period; i < arr.length; i++) sum += arr[i];
    return sum / period;
  }

  // RSI计算
  function rsi(arr, period) {
    if (arr.length < period + 1) return null;
    let gains = 0, losses = 0;
    for (let i = arr.length - period; i < arr.length; i++) {
      const diff = arr[i] - arr[i-1];
      if (diff > 0) gains += diff; else losses -= diff;
    }
    const avgGain = gains / period;
    const avgLoss = losses / period;
    if (avgLoss === 0) return 100;
    const rs = avgGain / avgLoss;
    return 100 - (100 / (1 + rs));
  }

  // EMA计算
  function ema(arr, period) {
    if (arr.length < period) return null;
    const k = 2 / (period + 1);
    let val = sma(arr.slice(0, period), period);
    for (let i = period; i < arr.length; i++) {
      val = arr[i] * k + val * (1 - k);
    }
    return val;
  }

  const container = document.getElementById('info-indicators');
  if (!container) return;

  const ma5 = sma(closes, 5);
  const ma10 = sma(closes, 10);
  const ma20 = sma(closes, 20);
  const rsiVal = rsi(closes, 14);
  const ema12 = ema(closes, 12);
  const ema26 = ema(closes, 26);
  const macdDif = (ema12 && ema26) ? (ema12 - ema26) : null;

  function fmt(v) {
    if (v == null || isNaN(v)) return '--';
    if (Math.abs(v) >= 1000) return v.toLocaleString('en-US', {maximumFractionDigits: 1});
    if (Math.abs(v) >= 1) return v.toFixed(2);
    return v.toFixed(4);
  }

  const indicators = [
    { name: 'MA(5)', value: ma5, color: '#FF6B6B' },
    { name: 'MA(10)', value: ma10, color: '#4ECDC4' },
    { name: 'MA(20)', value: ma20, color: '#45B7D1' },
    { name: 'RSI(14)', value: rsiVal, color: '#FFEAA7' },
    { name: 'MACD', value: macdDif, color: '#A855F7' },
  ];

  container.innerHTML = indicators.map(ind => `
    <div class="info-indicator-row">
      <span class="ind-name" style="color:${ind.color}">${ind.name}</span>
      <span class="ind-value">${fmt(ind.value)}</span>
    </div>
  `).join('');
}

/* ---------- 加载活跃警报到右侧面板 ---------- */
async function loadActiveAlerts() {
  const container = document.querySelector('.info-alerts');
  if (!container) return;

  try {
    const resp = await fetch('/api/alerts');
    if (!resp.ok) return;
    const data = await resp.json();
    const alerts = data.alerts || data || [];

    if (!Array.isArray(alerts) || alerts.length === 0) {
      container.innerHTML = '<div style="color:var(--text-tertiary);font-size:11px;padding:4px 0;">暂无活跃警报</div>';
      return;
    }

    // 只显示当前品种相关的或前5条
    const relevant = alerts.filter(a => a.enabled !== false).slice(0, 5);
    container.innerHTML = relevant.map(a => `
      <div class="alert-item" style="font-size:11px;padding:3px 0;border-bottom:1px solid var(--border-secondary);display:flex;justify-content:space-between;">
        <span>${a.symbol} ${a.condition_type === 'price' ? (a.condition?.operator === 'above' ? '>' : '<') + ' ' + (a.condition?.value || '') : a.condition_type}</span>
        <span style="color:var(--color-warning);">⏳</span>
      </div>
    `).join('');
  } catch {
    container.innerHTML = '<div style="color:var(--text-tertiary);font-size:11px;">加载失败</div>';
  }
}

// 页面加载后自动加载警报
document.addEventListener('DOMContentLoaded', () => {
  setTimeout(loadActiveAlerts, 3000);
});

/* ---------- 窗口大小响应 ---------- */
window.addEventListener('resize', () => {
  if (chart) chart.resize();
});
