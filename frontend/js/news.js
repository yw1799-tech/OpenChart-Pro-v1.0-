/* ============================================================
   News 模块 — 新闻快讯面板 (Phase 3A)

   功能：
   - 拉取 /api/news/flash 渲染列表
   - WebSocket 实时推送高价值新闻（importance >= 3）
   - 按市场/星级筛选
   - 点击品种代码切换主图
   ============================================================ */

const News = (function () {
  let _items = [];
  let _filterMarket = 'all';
  let _filterMinImportance = 1;
  let _autoRefreshTimer = null;

  const SENTIMENT_COLOR = {
    bullish: 'var(--color-up)',
    bearish: 'var(--color-down)',
    neutral: 'var(--text-secondary)',
  };
  const SENTIMENT_ICON = { bullish: '🟢', bearish: '🔴', neutral: '🟡' };

  /* ---------- 初始化 ---------- */
  function init() {
    // 创建 News 标签页 DOM
    _ensureDom();
    // 绑定 WebSocket 推送
    if (typeof ws !== 'undefined' && ws) {
      ws.on('flash_news', (msg) => {
        if (!msg || !msg.data) return;
        // 直接 prepend 到列表头
        _items.unshift(_normalize(msg.data));
        if (_items.length > 200) _items.length = 200;
        render();
        // toast 提示高价值新闻
        if (msg.data.importance >= 4) {
          if (typeof showToast === 'function') {
            const cats = (msg.data.categories || []).join(', ');
            showToast(`📰 ${'★'.repeat(msg.data.importance)} ${msg.data.title.substring(0, 40)}`, 'info', 4000);
          }
        }
      });
    }
    // 首次加载
    refresh();
    // 每 60s 拉取一次（兜底）
    _autoRefreshTimer = setInterval(refresh, 60000);
    console.log('[News] 已初始化');
  }

  function _ensureDom() {
    // 由 index.html 提供 div.bottom-pane[data-pane="news"]
    const pane = document.querySelector('.bottom-pane[data-pane="news"]');
    if (!pane) {
      console.warn('[News] 未找到 [data-pane="news"] 容器');
      return;
    }
    if (pane.dataset.inited) return;
    pane.dataset.inited = '1';
    pane.innerHTML = `
      <div class="news-toolbar" style="display:flex;gap:8px;padding:8px;border-bottom:1px solid var(--border-secondary);align-items:center;">
        <span style="font-size:13px;font-weight:600;">📰 新闻快讯</span>
        <select id="news-filter-market" class="select" style="width:110px;font-size:11px;">
          <option value="all">全部市场</option>
          <option value="crypto">加密货币</option>
          <option value="us">美股</option>
          <option value="hk">港股</option>
          <option value="cn">A股</option>
          <option value="macro">宏观</option>
        </select>
        <select id="news-filter-importance" class="select" style="width:90px;font-size:11px;">
          <option value="1">全部</option>
          <option value="3">★★★+</option>
          <option value="4">★★★★+</option>
          <option value="5">★★★★★</option>
        </select>
        <button id="news-refresh-btn" class="btn btn-sm">🔄 刷新</button>
        <span id="news-status" style="font-size:11px;color:var(--text-tertiary);margin-left:auto;"></span>
      </div>
      <div class="news-list" style="overflow-y:auto;height:calc(100% - 40px);"></div>
    `;
    pane.querySelector('#news-filter-market').addEventListener('change', (e) => {
      _filterMarket = e.target.value;
      render();
    });
    pane.querySelector('#news-filter-importance').addEventListener('change', (e) => {
      _filterMinImportance = parseInt(e.target.value, 10) || 1;
      refresh();
    });
    pane.querySelector('#news-refresh-btn').addEventListener('click', refresh);
  }

  /* ---------- 数据 ---------- */
  function _normalize(item) {
    return {
      id: item.id,
      title: item.title || '',
      source: item.source || '',
      url: item.url || '',
      published_at: item.published_at || 0,
      importance: item.importance || 1,
      sentiment: item.sentiment || 'neutral',
      categories: Array.isArray(item.categories) ? item.categories : [],
      impact_tags: Array.isArray(item.impact_tags) ? item.impact_tags : [],
      impact_on_crypto: item.impact_on_crypto || null,
      is_macro_data: !!item.is_macro_data,
    };
  }

  async function refresh() {
    try {
      const url = `/api/news/flash?importance_min=${_filterMinImportance}&limit=100`;
      const resp = await fetch(url);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const d = await resp.json();
      _items = (d.items || []).map(_normalize);
      render();
      const statusEl = document.querySelector('#news-status');
      if (statusEl) statusEl.textContent = `共 ${_items.length} 条 · ${new Date().toLocaleTimeString()}`;
    } catch (e) {
      console.warn('[News] 刷新失败:', e);
    }
  }

  /* ---------- 渲染 ---------- */
  function render() {
    const listEl = document.querySelector('.bottom-pane[data-pane="news"] .news-list');
    if (!listEl) return;

    // 应用市场筛选（粗粒度，按 source name 包含关键词）
    let visible = _items;
    if (_filterMarket !== 'all') {
      const marketKeywords = {
        crypto: ['CoinDesk', 'OKX', 'CryptoPanic', 'Cointelegraph'],
        us: ['Yahoo', 'Finnhub', 'SEC', 'PR'],
        cn: ['东方财富', '巨潮', '金十', '新浪财经'],
        hk: ['HKEX', 'AAStocks'],
        macro: ['ForexFactory', '金十'],
      };
      const keys = marketKeywords[_filterMarket] || [];
      visible = _items.filter((it) => keys.some((k) => it.source.includes(k)));
    }

    if (!visible.length) {
      listEl.innerHTML = `<div style="padding:40px;text-align:center;color:var(--text-tertiary);">暂无新闻 (规则引擎评分 ≥${_filterMinImportance}星)</div>`;
      return;
    }

    listEl.innerHTML = visible.map(_renderItem).join('');
    // 绑定品种点击
    listEl.querySelectorAll('[data-symbol]').forEach((el) => {
      el.addEventListener('click', (e) => {
        e.stopPropagation();
        const sym = el.dataset.symbol;
        if (sym && typeof switchSymbol === 'function') {
          switchSymbol(sym, window.currentMarket);
        }
      });
    });
  }

  function _renderItem(item) {
    const stars = '★'.repeat(item.importance);
    const sentColor = SENTIMENT_COLOR[item.sentiment] || SENTIMENT_COLOR.neutral;
    const sentIcon = SENTIMENT_ICON[item.sentiment] || SENTIMENT_ICON.neutral;
    const time = new Date(item.published_at).toLocaleString('zh-CN', { hour: '2-digit', minute: '2-digit', month: 'numeric', day: 'numeric' });
    const cats = item.categories.length
      ? item.categories.map((c) => `<span class="news-cat" data-symbol="${c}" style="cursor:pointer;color:var(--color-accent);background:var(--bg-tertiary);padding:1px 6px;border-radius:3px;margin-right:4px;font-size:10px;">${c}</span>`).join('')
      : '';
    const linkBtn = item.url
      ? `<a href="${item.url}" target="_blank" style="font-size:10px;color:var(--text-tertiary);margin-left:8px;">原文 ↗</a>`
      : '';
    return `
      <div class="news-item" style="padding:8px 12px;border-bottom:1px solid var(--border-secondary);">
        <div style="display:flex;align-items:center;gap:6px;font-size:12px;">
          <span style="color:var(--color-warning);">${stars}</span>
          <span style="color:${sentColor};">${sentIcon}</span>
          <span style="color:var(--text-tertiary);font-size:10px;">${time}</span>
          <span style="color:var(--text-secondary);font-size:10px;">${item.source}</span>
          ${linkBtn}
        </div>
        <div style="margin:4px 0;color:var(--text-primary);font-size:13px;line-height:1.4;">${item.title}</div>
        ${cats ? `<div style="margin-top:2px;">${cats}</div>` : ''}
      </div>
    `;
  }

  return { init, refresh, render };
})();

// 全局暴露（供 app.js 初始化）
window.News = News;
