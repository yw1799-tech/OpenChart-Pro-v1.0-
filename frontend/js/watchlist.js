/* ============================================================
   OpenChart Pro - 自选列表管理（按市场独立）
   ============================================================ */

const Watchlist = (() => {
  const STORAGE_KEY = 'openchart_watchlist';
  let allItems = {};   // market -> [{ symbol, name }]
  let priceCache = {}; // symbol -> { price, changePct }

  // 各市场默认品种
  const DEFAULTS = {
    crypto: [
      { symbol: 'BTC-USDT', name: 'Bitcoin' },
      { symbol: 'ETH-USDT', name: 'Ethereum' },
      { symbol: 'SOL-USDT', name: 'Solana' },
    ],
    us: [
      { symbol: 'AAPL', name: 'Apple Inc.' },
      { symbol: 'NVDA', name: 'NVIDIA Corp.' },
      { symbol: 'TSLA', name: 'Tesla Inc.' },
    ],
    hk: [
      { symbol: '0700.HK', name: '腾讯控股' },
      { symbol: '9988.HK', name: '阿里巴巴' },
    ],
    // v11.5: 旧 'a' key 改成 'cn' 与全局 currentMarket 一致（之前 A 股 watchlist 默认空）
    cn: [
      { symbol: '600519', name: '贵州茅台' },
      { symbol: '000858', name: '五粮液' },
      { symbol: '300750', name: '宁德时代' },
    ],
  };

  let _inited = false;
  function init() {
    if (_inited) { console.warn('[Watchlist] 已初始化，跳过重复 init'); return; }
    _inited = true;
    load();
    render();

    // 添加按钮
    document.querySelector('.watchlist-header .add-btn')?.addEventListener('click', () => {
      if (typeof Search !== 'undefined') Search.open();
    });

    // 定时刷新价格（90s 一次，进一步减压）
    let _wlTimer = setInterval(refreshPrices, 90000);
    setTimeout(refreshPrices, 5000);
    if (window.__visibilityHandlers) {
      window.__visibilityHandlers.push(({ hidden }) => {
        if (hidden && _wlTimer) { clearInterval(_wlTimer); _wlTimer = null; }
        else if (!hidden && !_wlTimer) { refreshPrices(); _wlTimer = setInterval(refreshPrices, 90000); }
      });
    }
  }

  function load() {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      allItems = saved ? JSON.parse(saved) : {};
    } catch {
      allItems = {};
    }
    // 确保每个市场都有默认品种
    for (const [mkt, defaults] of Object.entries(DEFAULTS)) {
      if (!allItems[mkt] || allItems[mkt].length === 0) {
        allItems[mkt] = [...defaults];
      }
    }
    save();
  }

  function save() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(allItems));
    } catch (e) {
      console.warn('[Watchlist] 保存失败:', e);
    }
  }

  function getCurrentItems() {
    const market = window.currentMarket || 'crypto';
    return allItems[market] || [];
  }

  function add(symbol, name) {
    const market = window.currentMarket || 'crypto';
    if (!allItems[market]) allItems[market] = [];
    if (allItems[market].some(i => i.symbol === symbol)) {
      showToast(`${symbol} 已在自选列表中`, 'info', 2000);
      return;
    }
    allItems[market].push({ symbol, name: name || symbol });
    save();
    render();
    showToast(`已添加 ${symbol} 到自选`, 'success', 2000);
  }

  function remove(symbol) {
    const market = window.currentMarket || 'crypto';
    if (allItems[market]) {
      allItems[market] = allItems[market].filter(i => i.symbol !== symbol);
      save();
      render();
    }
  }

  function render() {
    const container = document.querySelector('.watchlist-items');
    if (!container) return;
    container.innerHTML = '';

    const items = getCurrentItems();

    if (items.length === 0) {
      container.innerHTML = '<div style="color:var(--text-tertiary);padding:20px;text-align:center;font-size:12px;">暂无自选品种<br>点击 + 添加</div>';
      return;
    }

    for (const item of items) {
      const el = document.createElement('div');
      el.className = 'watchlist-item' + (item.symbol === window.currentSymbol ? ' active' : '');
      el.dataset.symbol = item.symbol;

      const pc = priceCache[item.symbol] || {};
      const changeClass = (pc.changePct > 0) ? 'up' : (pc.changePct < 0) ? 'down' : '';
      const priceStr = pc.price != null ? fmtPrice(pc.price) : '--';
      const changeAmtStr = pc.changeAmt != null ? ((pc.changeAmt >= 0 ? '+' : '') + fmtPrice(pc.changeAmt)) : '';
      const changePctStr = pc.changePct != null ? ((pc.changePct >= 0 ? '+' : '') + pc.changePct.toFixed(2) + '%') : '';

      const market = window.currentMarket || 'crypto';
      const showName = market !== 'crypto' && item.name;
      el.innerHTML = `
        <div style="flex:1;min-width:0;">
          <div class="symbol-name">${item.symbol}${showName ? '<span style="color:var(--text-tertiary);font-size:11px;margin-left:6px;font-weight:400;">' + item.name + '</span>' : ''}</div>
          <div style="display:flex;align-items:baseline;gap:6px;margin-top:2px;">
            <span class="symbol-price ${changeClass}">${priceStr}</span>
            <span class="symbol-change ${changeClass}">${changeAmtStr} ${changePctStr}</span>
          </div>
        </div>
        <button class="remove-btn" title="移除">✕</button>
      `;

      el.addEventListener('click', (e) => {
        if (e.target.classList.contains('remove-btn')) return;
        switchSymbol(item.symbol, window.currentMarket);
      });
      el.querySelector('.remove-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        remove(item.symbol);
      });

      container.appendChild(el);
    }
  }

  async function refreshPrices() {
    const items = getCurrentItems();
    if (items.length === 0) return;

    const market = window.currentMarket || 'crypto';
    const apiMarket = market === 'a' ? 'cn' : market;

    // 并行请求（Promise.allSettled），不串行，减少总耗时
    const promises = items.map(item =>
      fetch(`/api/klines?symbol=${encodeURIComponent(item.symbol)}&interval=1D&limit=2&market=${apiMarket}`)
        .then(r => r.ok ? r.json() : null)
        .then(data => {
          if (!data) return;
          const candles = data.candles || [];
          if (candles.length > 0) {
            const last = candles[candles.length - 1];
            const prev = candles.length > 1 ? candles[candles.length - 2] : last;
            priceCache[item.symbol] = {
              price: last.close,
              changeAmt: last.close - prev.close,
              changePct: prev.close !== 0 ? ((last.close - prev.close) / prev.close * 100) : 0,
            };
          }
        })
        .catch(() => {})
    );
    await Promise.allSettled(promises);
    render();
  }

  function fmtPrice(p) {
    if (p == null) return '--';
    if (p >= 10000) return p.toLocaleString('en-US', {maximumFractionDigits: 0});
    if (p >= 1000) return p.toLocaleString('en-US', {maximumFractionDigits: 2});
    if (p >= 1) return p.toFixed(2);
    return p.toFixed(4);
  }

  function getItems() { return getCurrentItems(); }

  return { init, add, remove, render, getItems, refreshPrices };
})();
