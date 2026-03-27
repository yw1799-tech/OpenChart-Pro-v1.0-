/* ============================================================
   OpenChart Pro - KLineChart 初始化与管理
   ============================================================ */

let chart = null;
let mainPaneId = null;
const subPanes = [];        // 副图 pane 列表 { id, name }
const MAX_SUB_PANES = 4;

/* ---------- 暗色主题配置 (参考TradingView Pro) ---------- */
const darkTheme = {
  grid: {
    show: true,
    horizontal: { show: true, size: 1, color: 'rgba(42,46,57,0.5)', style: 'dash', dashedValue: [3, 3] },
    vertical:   { show: false },
  },
  candle: {
    type: 'candle_solid',
    bar: {
      upColor: 'rgba(14,203,129,0.9)',
      downColor: 'rgba(246,70,93,0.9)',
      noChangeColor: '#838D9E',
      upBorderColor: '#0ecb81',
      downBorderColor: '#f6465d',
      noChangeBorderColor: '#838D9E',
      upWickColor: '#0ecb81',
      downWickColor: '#f6465d',
      noChangeWickColor: '#838D9E',
    },
    priceMark: {
      show: true,
      high: { show: true, color: '#787b86', textSize: 10 },
      low:  { show: true, color: '#787b86', textSize: 10 },
      last: {
        show: true,
        upColor: '#0ecb81',
        downColor: '#f6465d',
        noChangeColor: '#838D9E',
        line: { show: true, style: 'dash', dashedValue: [6, 4], size: 1 },
        text: { show: true, size: 11, paddingLeft: 8, paddingTop: 4, paddingRight: 8, paddingBottom: 4, borderRadius: 2, fontFamily: 'JetBrains Mono, Consolas, monospace' },
      },
    },
    tooltip: {
      showRule: 'always',
      showType: 'standard',
      text: { size: 11, color: '#787b86', marginLeft: 8, marginTop: 6, marginRight: 8, marginBottom: 0 },
    },
  },
  indicator: {
    lastValueMark: { show: false },
    tooltip: { showRule: 'always', showType: 'standard', text: { size: 11 } },
    lines: [
      { color: '#2196F3', size: 1 },   // 蓝
      { color: '#FF9800', size: 1 },   // 橙
      { color: '#AB47BC', size: 1 },   // 紫
      { color: '#26A69A', size: 1 },   // 青绿
      { color: '#EF5350', size: 1 },   // 红
    ],
  },
  xAxis: {
    show: true,
    size: 'auto',
    axisLine: { show: false },
    tickLine: { show: false },
    tickText: { show: true, color: '#787b86', size: 11, fontFamily: 'JetBrains Mono, Consolas, monospace' },
  },
  yAxis: {
    show: true,
    size: 'auto',
    position: 'right',
    type: 'normal',
    inside: false,
    axisLine: { show: false },
    tickLine: { show: false },
    tickText: { show: true, color: '#787b86', size: 11, fontFamily: 'JetBrains Mono, Consolas, monospace' },
  },
  crosshair: {
    show: true,
    horizontal: {
      show: true,
      line: { show: true, style: 'dash', dashedValue: [4, 4], size: 1, color: 'rgba(120,123,134,0.4)' },
      text: { show: true, size: 11, color: '#D1D4DC', borderRadius: 2, paddingLeft: 8, paddingRight: 8, paddingTop: 4, paddingBottom: 4, backgroundColor: '#363A45', borderColor: '#505050', borderSize: 1, fontFamily: 'JetBrains Mono, Consolas, monospace' },
    },
    vertical: {
      show: true,
      line: { show: true, style: 'dash', dashedValue: [4, 4], size: 1, color: 'rgba(120,123,134,0.4)' },
      text: { show: true, size: 11, color: '#D1D4DC', borderRadius: 2, paddingLeft: 8, paddingRight: 8, paddingTop: 4, paddingBottom: 4, backgroundColor: '#363A45', borderColor: '#505050', borderSize: 1, fontFamily: 'JetBrains Mono, Consolas, monospace' },
    },
  },
  separator: { size: 1, color: 'rgba(42,46,57,0.8)', activeBackgroundColor: 'rgba(33,150,243,0.2)' },
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

  // 注册BOLL指标 — 高级配色
  try {
    klinecharts.registerIndicator({
      name: 'BOLL',
      shortName: 'BOLL',
      calcParams: [20, 2],
      precision: 2,
      figures: [
        { key: 'up',   title: 'UP: ',   type: 'line' },
        { key: 'mid',  title: 'MID: ',  type: 'line' },
        { key: 'dn',   title: 'DN: ',   type: 'line' },
      ],
      styles: {
        lines: [
          { color: 'rgba(33,150,243,0.45)', size: 1 },   // 上轨 - 淡蓝
          { color: 'rgba(255,152,0,0.6)', size: 1 },      // 中轨 - 暖橙
          { color: 'rgba(33,150,243,0.45)', size: 1 },   // 下轨 - 淡蓝
        ],
      },
      calc: (dataList, { calcParams }) => {
        const period = calcParams[0];
        const stdDevMultiplier = calcParams[1];
        return dataList.map((kLineData, i) => {
          if (i < period - 1) return {};
          let sum = 0;
          for (let j = i - period + 1; j <= i; j++) {
            sum += dataList[j].close;
          }
          const mid = sum / period;
          let devSum = 0;
          for (let j = i - period + 1; j <= i; j++) {
            const diff = dataList[j].close - mid;
            devSum += diff * diff;
          }
          const stdDev = Math.sqrt(devSum / period);
          return {
            up:  mid + stdDevMultiplier * stdDev,
            mid: mid,
            dn:  mid - stdDevMultiplier * stdDev,
          };
        });
      },
    });
    console.log('[Chart] 已注册自定义BOLL指标样式');
  } catch(e) {
    console.warn('[Chart] 注册BOLL样式失败:', e);
  }


  } catch(e) {
    console.warn('[Chart] 注册缠论指标失败:', e);
  }

  // 不再自定义RSI，使用KLineChart内置RSI但修改参数为只有1条线
  // 内置RSI默认参数[6,12,24]改为[14]
  // 通过覆盖注册实现

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
    const resp = await fetch(`/api/klines?symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}&limit=1000&market=${encodeURIComponent(apiMarket)}`);
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

    // 如果缠论分析已启用，自动刷新
    if (isChanlunActive()) {
      loadChanlun(symbol, interval, market);
    }
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

  // 如果当前在缠论研判Tab，自动刷新
  const activeTab = document.querySelector('.bottom-tab.active');
  if (activeTab && activeTab.dataset.tab === 'chanlun-verdict' && typeof ChanlunVerdict !== 'undefined') {
    ChanlunVerdict.analyze(symbol, window.currentMarket === 'a' ? 'cn' : window.currentMarket);
  }
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
  // overlay类指标叠加到主图candle_pane，与K线共享Y轴
  chart.createIndicator(name, false, { id: 'candle_pane' });
  console.log(`[Chart] 已叠加主图指标: ${name} -> candle_pane`);
}

function addSubIndicator(name) {
  if (!chart) return;
  const paneId = addSubPane(name, name);

  // RSI: 添加30/50/70水平参考线overlay
  if (name === 'RSI' && paneId) {
    setTimeout(() => {
      try {
        [
          { value: 70, color: 'rgba(239,83,80,0.5)' },
          { value: 50, color: 'rgba(120,123,134,0.35)' },
          { value: 30, color: 'rgba(38,166,154,0.5)' },
        ].forEach(lv => {
          chart.createOverlay({
            name: 'horizontalStraightLine',
            points: [{ value: lv.value }],
            styles: {
              line: { color: lv.color, size: 1, style: 'dashed', dashedValue: [4, 3] },
              text: { show: false },
            },
            lock: true,
          }, paneId);
        });
      } catch(e) { console.warn('[Chart] RSI参考线失败:', e); }
    }, 300);
  }
  return paneId;
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

  // BOLL
  const ma20b = sma(closes, 20);
  let bollUp = null, bollDn = null;
  if (ma20b && closes.length >= 20) {
    let devSum = 0;
    for (let i = n - 20; i < n; i++) devSum += (closes[i] - ma20b) ** 2;
    const std = Math.sqrt(devSum / 20);
    bollUp = ma20b + 2 * std;
    bollDn = ma20b - 2 * std;
  }

  // ATR
  let atr14 = null;
  if (klines.length >= 15) {
    let trSum = 0;
    for (let i = n - 14; i < n; i++) {
      const h = klines[i].high, l = klines[i].low, pc = klines[i-1].close;
      trSum += Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc));
    }
    atr14 = trSum / 14;
  }

  const indicators = [
    { name: 'MA5',    value: ma5,     color: '#2196F3' },
    { name: 'MA10',   value: ma10,    color: '#FF9800' },
    { name: 'MA20',   value: ma20,    color: '#AB47BC' },
    { name: 'RSI',    value: rsiVal,  color: '#AB47BC' },
    { name: 'MACD',   value: macdDif, color: '#2196F3' },
  ];

  let html = '<div class="ind-grid">';
  for (const ind of indicators) {
    html += `<div class="ind-cell">
      <span class="ind-dot" style="background:${ind.color}"></span>
      <span class="ind-label">${ind.name}</span>
      <span class="ind-val">${fmt(ind.value)}</span>
    </div>`;
  }
  html += '</div>';

  container.innerHTML = html;
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

/* ---------- 缠论分析数据加载 ---------- */






/* ---------- 窗口大小响应 ---------- */
window.addEventListener('resize', () => {
  if (chart) chart.resize();
});
