/* ============================================================
   OpenChart Pro - 搜索品种组件
   ============================================================ */

const Search = (() => {
  let overlay = null;
  let inputEl = null;
  let resultsEl = null;
  let highlightIndex = -1;
  let flatResults = [];       // 扁平化的搜索结果列表（用于键盘导航）
  let allSymbols = [];        // 缓存的品种列表 { symbol, name, market }

  const HOT_SYMBOLS = [
    { symbol: 'BTC-USDT',  name: 'Bitcoin',    market: 'Crypto' },
    { symbol: 'ETH-USDT',  name: 'Ethereum',   market: 'Crypto' },
    { symbol: 'AAPL',      name: 'Apple Inc',  market: 'US' },
    { symbol: 'TSLA',      name: 'Tesla Inc',  market: 'US' },
    { symbol: '0700.HK',   name: '腾讯控股',    market: 'HK' },
    { symbol: '600519',    name: '贵州茅台',    market: 'A股' },
  ];

  const MAX_PER_GROUP = 5;

  function init() {
    overlay = document.getElementById('search-modal');
    if (!overlay) return;

    inputEl = overlay.querySelector('.search-input');
    resultsEl = overlay.querySelector('.search-results');

    // 搜索框输入事件
    if (inputEl) {
      inputEl.addEventListener('input', () => {
        doSearch(inputEl.value.trim());
      });
    }

    // 热门品种点击
    overlay.querySelectorAll('.search-hot-item').forEach(el => {
      el.addEventListener('click', () => {
        selectSymbol(el.dataset.symbol);
      });
    });

    // 关闭按钮
    const closeBtn = overlay.querySelector('.modal-close');
    if (closeBtn) closeBtn.addEventListener('click', close);

    // 点击遮罩关闭
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) close();
    });

    // 键盘导航
    overlay.addEventListener('keydown', handleKeydown);
  }

  function open() {
    if (!overlay) return;
    overlay.classList.add('show');
    highlightIndex = -1;
    flatResults = [];
    if (inputEl) {
      inputEl.value = '';
      inputEl.focus();
    }
    renderHot();
  }

  function close() {
    if (!overlay) return;
    overlay.classList.remove('show');
    highlightIndex = -1;
  }

  function isOpen() {
    return overlay && overlay.classList.contains('show');
  }

  /* ---------- 设置品种数据 ---------- */
  function setSymbols(symbols) {
    allSymbols = symbols || [];
  }

  /* ---------- 搜索逻辑（搜所有市场 + 后端API兜底）---------- */
  let searchTimer = null;

  function doSearch(query) {
    if (!query) {
      renderHot();
      return;
    }

    const q = query.toLowerCase();

    // 1. 先从所有市场的本地缓存中搜索
    let matches = [];
    // 搜索当前市场缓存
    if (allSymbols && allSymbols.length) {
      matches.push(...allSymbols.filter(s =>
        s.symbol.toLowerCase().includes(q) || (s.name && s.name.toLowerCase().includes(q))
      ));
    }
    // 搜索其他市场缓存（symbolsCache在app.js中定义）
    if (typeof symbolsCache !== 'undefined') {
      for (const [mkt, syms] of Object.entries(symbolsCache)) {
        if (!Array.isArray(syms)) continue;
        const otherMatches = syms.filter(s =>
          (s.symbol.toLowerCase().includes(q) || (s.name && s.name.toLowerCase().includes(q))) &&
          !matches.some(m => m.symbol === s.symbol)
        );
        matches.push(...otherMatches);
      }
    }

    // 2. 如果本地结果少于3个，延迟请求后端API搜索
    if (matches.length < 3) {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => remoteSearch(query), 300);
    }

    // 按市场分组
    const groups = {};
    for (const item of matches) {
      const market = item.market || '其他';
      if (!groups[market]) groups[market] = [];
      if (groups[market].length < MAX_PER_GROUP) {
        groups[market].push(item);
      }
    }

    renderGroups(groups);
  }

  /* ---------- 渲染 ---------- */
  function renderHot() {
    if (!resultsEl) return;
    resultsEl.innerHTML = '';
    flatResults = [];
    // 热门品种已在HTML的.search-hot区域，此处不重复渲染
  }

  function renderGroups(groups) {
    if (!resultsEl) return;
    resultsEl.innerHTML = '';
    flatResults = [];
    highlightIndex = -1;

    const marketNames = Object.keys(groups);
    if (marketNames.length === 0) {
      resultsEl.innerHTML = '<div style="padding:20px 16px;color:var(--text-tertiary);text-align:center;">未找到匹配品种</div>';
      return;
    }

    for (const market of marketNames) {
      const title = document.createElement('div');
      title.className = 'search-group-title';
      title.textContent = market;
      resultsEl.appendChild(title);

      for (const item of groups[market]) {
        const el = document.createElement('div');
        el.className = 'search-result-item';
        el.dataset.symbol = item.symbol;
        el.innerHTML = `
          <div>
            <span class="sr-symbol">${item.symbol}</span>
            <span class="sr-name">${item.name || ''}</span>
          </div>
          <span class="sr-market">${market}</span>
        `;
        el.addEventListener('click', () => selectSymbol(item.symbol, item.market));
        el.addEventListener('mouseenter', () => {
          highlightIndex = flatResults.indexOf(el);
          updateHighlight();
        });
        resultsEl.appendChild(el);
        flatResults.push(el);
      }
    }
  }

  function updateHighlight() {
    flatResults.forEach((el, i) => {
      el.classList.toggle('highlight', i === highlightIndex);
    });
    // 滚动到可见
    if (highlightIndex >= 0 && flatResults[highlightIndex]) {
      flatResults[highlightIndex].scrollIntoView({ block: 'nearest' });
    }
  }

  /* ---------- 键盘导航 ---------- */
  function handleKeydown(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      close();
      return;
    }

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (flatResults.length > 0) {
        highlightIndex = (highlightIndex + 1) % flatResults.length;
        updateHighlight();
      }
      return;
    }

    if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (flatResults.length > 0) {
        highlightIndex = (highlightIndex - 1 + flatResults.length) % flatResults.length;
        updateHighlight();
      }
      return;
    }

    if (e.key === 'Enter') {
      e.preventDefault();
      if (highlightIndex >= 0 && flatResults[highlightIndex]) {
        selectSymbol(flatResults[highlightIndex].dataset.symbol);
      }
      return;
    }
  }

  /* ---------- 远程搜索（后端API）---------- */
  async function remoteSearch(query) {
    if (!query || query.length < 1) return;
    const markets = ['crypto', 'us', 'hk', 'cn'];
    const allResults = [];

    await Promise.allSettled(markets.map(async (mkt) => {
      try {
        const resp = await fetch(`/api/symbols?market=${mkt}&q=${encodeURIComponent(query)}`);
        if (!resp.ok) return;
        const data = await resp.json();
        const syms = data.symbols || [];
        allResults.push(...syms);
      } catch {}
    }));

    if (allResults.length === 0) return;

    // 合并去重
    const existing = new Set((allSymbols || []).map(s => s.symbol));
    const newSymbols = allResults.filter(s => !existing.has(s.symbol));
    if (newSymbols.length === 0) return;

    // 重新渲染
    const q = query.toLowerCase();
    const combined = [...(allSymbols || []), ...newSymbols];
    const matches = combined.filter(s =>
      s.symbol.toLowerCase().includes(q) || (s.name && s.name.toLowerCase().includes(q))
    );

    const groups = {};
    const MARKET_LABELS = { crypto: '加密货币', us: '美股', hk: '港股', cn: 'A股' };
    for (const item of matches) {
      const label = MARKET_LABELS[item.market] || item.market || '其他';
      if (!groups[label]) groups[label] = [];
      if (groups[label].length < MAX_PER_GROUP) {
        groups[label].push(item);
      }
    }
    renderGroups(groups);
  }

  /* ---------- 选中品种 ---------- */
  function selectSymbol(symbol, market) {
    if (!symbol) return;
    close();
    // 如果传了market且和当前不同，先切换市场
    if (market && market !== window.currentMarket) {
      const frontMarket = market === 'cn' ? 'a' : market;
      if (typeof switchMarket === 'function' && typeof MARKETS !== 'undefined' && MARKETS[frontMarket]) {
        window.currentMarket = frontMarket;
      }
    }
    // 切换K线
    if (typeof switchSymbol === 'function') {
      switchSymbol(symbol, window.currentMarket);
    }
    // 同时添加到自选列表（如果不存在的话）
    if (typeof Watchlist !== 'undefined') {
      const items = Watchlist.getItems();
      if (!items.some(i => i.symbol === symbol)) {
        Watchlist.add(symbol, symbol);
      }
    }
    showToast(`已切换至 ${symbol}`, 'info', 2000);
  }

  return { init, open, close, isOpen, setSymbols };
})();
