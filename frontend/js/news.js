/* ============================================================
   News 模块 — 新闻快讯面板
   特性：搜索 / 多维过滤 / WS 去重 / AI 解读持久化 / ★4+ 桌面通知 / 一键加候选
   ============================================================ */

const News = (function () {
  let _items = [];
  let _filterMarket = 'all';
  let _filterMinImportance = 1;
  let _filterSentiment = 'all';   // all | bullish | bearish | neutral
  let _filterSymbol = '';         // 精确 symbol 过滤
  let _searchKeyword = '';        // 标题关键词
  let _autoRefreshTimer = null;
  let _renderPending = false;
  let _searchDebounceTimer = null;
  let _aiAbortControllers = {};   // newsId -> AbortController（防僵尸）

  function _scheduleRender() {
    if (_renderPending) return;
    _renderPending = true;
    requestAnimationFrame(() => { _renderPending = false; render(); });
  }

  const SENTIMENT_COLOR = {
    bullish: 'var(--color-up)',
    bearish: 'var(--color-down)',
    neutral: 'var(--text-secondary)',
  };
  const SENTIMENT_ICON = { bullish: '🟢', bearish: '🔴', neutral: '🟡' };

  function _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function _normalize(item) {
    return {
      id: item.id,
      title: item.title || '',
      content: item.content || '',
      source: item.source || '',
      url: item.url || '',
      published_at: item.published_at || 0,
      importance: item.importance || 1,
      sentiment: item.sentiment || 'neutral',
      categories: Array.isArray(item.categories) ? item.categories : [],
      impact_tags: Array.isArray(item.impact_tags) ? item.impact_tags : [],
      impact_on_crypto: item.impact_on_crypto || null,
      is_macro_data: !!item.is_macro_data,
      macro_type: item.macro_type || '',
      macro_actual: item.macro_actual,
      macro_forecast: item.macro_forecast,
      macro_deviation_pct: item.macro_deviation_pct,
      ai_analysis: typeof item.ai_analysis === 'string'
        ? (() => { try { return JSON.parse(item.ai_analysis); } catch { return null; } })()
        : item.ai_analysis,
      markets: Array.isArray(item.markets) ? item.markets : [],
    };
  }

  let _inited = false;
  function init() {
    if (_inited) { console.warn('[News] 已初始化，跳过重复 init'); return; }
    _inited = true;
    _ensureDom();

    if (typeof ws !== 'undefined' && ws) {
      ws.on('flash_news', (msg) => {
        if (!msg || !msg.data) return;
        const incoming = _normalize(msg.data);
        // 去重：已有同 id 不再插入
        if (_items.some(it => it.id === incoming.id)) return;
        _items.unshift(incoming);
        if (_items.length > 200) _items.length = 200;
        _scheduleRender();

        if (msg.data.importance >= 4) {
          const stars = '★'.repeat(msg.data.importance);
          const text = `📰 ${stars} ${msg.data.title.substring(0, 40)}`;
          if (typeof showToast === 'function') showToast(text, 'info', 4000);
          // 桌面通知（已授权时）
          if (typeof Notification !== 'undefined' && Notification.permission === 'granted') {
            try {
              new Notification('OpenChart Pro · 高重要度新闻', {
                body: msg.data.title,
                tag: 'news-' + msg.data.id,
              });
            } catch {}
          }
        }
      });
    }

    // 首次加载请求桌面通知权限（用户允许后 ★4+ 可弹通知）
    if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
      try { Notification.requestPermission().catch(() => {}); } catch {}
    }

    refresh();
    _autoRefreshTimer = setInterval(refresh, 180000);
    if (window.__visibilityHandlers) {
      window.__visibilityHandlers.push(({ hidden }) => {
        if (hidden) {
          if (_autoRefreshTimer) { clearInterval(_autoRefreshTimer); _autoRefreshTimer = null; }
        } else if (!_autoRefreshTimer) {
          refresh();
          _autoRefreshTimer = setInterval(refresh, 180000);
        }
      });
    }
    console.log('[News] 已初始化');
  }

  function _ensureDom() {
    const pane = document.querySelector('.bottom-pane[data-pane="news"]');
    if (!pane) { console.warn('[News] 未找到容器'); return; }
    if (pane.dataset.inited) return;
    pane.dataset.inited = '1';
    pane.innerHTML = `
      <div class="oc-toolbar news-toolbar">
        <span class="oc-text-lg" style="font-weight:600;">📰 新闻快讯</span>
        <select id="news-filter-market" class="select oc-text-sm" style="width:100px;">
          <option value="all">全部市场</option>
          <option value="crypto">加密</option>
          <option value="us">美股</option>
          <option value="hk">港股</option>
          <option value="cn">A股</option>
          <option value="macro">宏观</option>
          <option value="other">其他</option>
        </select>
        <select id="news-filter-importance" class="select oc-text-sm" style="width:90px;">
          <option value="1">全部</option>
          <option value="3">★★★+</option>
          <option value="4">★★★★+</option>
          <option value="5">★★★★★</option>
        </select>
        <select id="news-filter-sentiment" class="select oc-text-sm" style="width:96px;">
          <option value="all">全部情绪</option>
          <option value="bullish">🟢 利好</option>
          <option value="bearish">🔴 利空</option>
          <option value="neutral">🟡 中性</option>
        </select>
        <input id="news-search-input" class="input oc-text-sm" placeholder="🔍 搜索关键词 (回车)" style="width:170px;padding:3px 8px;">
        <input id="news-symbol-input" class="input oc-text-sm" placeholder="按代码筛选 (如 NVDA)" style="width:120px;padding:3px 8px;">
        <span class="oc-toolbar-spacer"></span>
        <button id="news-refresh-btn" class="btn btn-sm" title="刷新">🔄</button>
        <span id="news-status" class="oc-text-sm oc-muted" style="width:100%;text-align:right;margin-top:2px;"></span>
      </div>
      <div class="news-list" style="overflow-y:auto;flex:1;min-height:0;"></div>
    `;
    pane.querySelector('#news-filter-market').addEventListener('change', (e) => {
      _filterMarket = e.target.value;
      refresh();
    });
    pane.querySelector('#news-filter-importance').addEventListener('change', (e) => {
      _filterMinImportance = parseInt(e.target.value, 10) || 1;
      refresh();
    });
    pane.querySelector('#news-filter-sentiment').addEventListener('change', (e) => {
      _filterSentiment = e.target.value;
      render();
    });
    pane.querySelector('#news-search-input').addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      _searchKeyword = e.target.value.trim();
      refresh();
    });
    pane.querySelector('#news-symbol-input').addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      _filterSymbol = e.target.value.trim();
      refresh();
    });
    pane.querySelector('#news-refresh-btn').addEventListener('click', refresh);
  }

  async function refresh() {
    try {
      const params = new URLSearchParams({
        importance_min: String(_filterMinImportance),
        limit: '100',
      });
      if (_filterMarket !== 'all') params.set('market', _filterMarket);
      if (_searchKeyword) params.set('keyword', _searchKeyword);
      if (_filterSymbol) params.set('symbol', _filterSymbol);
      const resp = await fetch(`/api/news/flash?${params}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const d = await resp.json();
      _items = (d.items || []).map(_normalize);
      render();
      const statusEl = document.querySelector('#news-status');
      if (statusEl) statusEl.textContent = `共 ${_items.length} 条 · ${new Date().toLocaleTimeString()}`;
    } catch (e) {
      console.warn('[News] 刷新失败:', e);
      if (typeof showToast === 'function') showToast(`新闻刷新失败: ${e.message}`, 'error', 3000);
    }
  }

  function render() {
    const listEl = document.querySelector('.bottom-pane[data-pane="news"] .news-list');
    if (!listEl) return;

    let visible = _items;
    if (_filterSentiment !== 'all') visible = visible.filter(it => it.sentiment === _filterSentiment);

    if (!visible.length) {
      const isFiltered = _searchKeyword || _filterSymbol || _filterMarket !== 'all' || _filterMinImportance > 1;
      listEl.innerHTML = `
        <div class="oc-empty">
          <div class="oc-empty-icon">📰</div>
          <div class="oc-empty-title">${isFiltered ? '当前筛选无匹配' : '暂无新闻'}</div>
          <div class="oc-empty-hint">${isFiltered ? '可清除筛选条件或点击🔄刷新' : '21 个源每 2 分钟采集一次，刚启动需等几分钟'}</div>
        </div>`;
      return;
    }

    listEl.innerHTML = visible.map(_renderItem).join('');

    // 还原已展开的 AI 解读（持久化）
    visible.forEach(item => {
      if (item.ai_analysis && item._showAi) {
        const resultEl = listEl.querySelector(`.news-ai-result[data-news-id="${item.id}"]`);
        if (resultEl) {
          resultEl.style.display = 'block';
          resultEl.innerHTML = _renderAIAnalysis(item.ai_analysis, true);
        }
      }
    });

    if (!listEl._delegated) {
      listEl._delegated = true;
      listEl.addEventListener('click', (e) => _onListClick(e, listEl));
    }
  }

  async function _onListClick(e, listEl) {
    // AI 解读按钮
    const aiBtn = e.target.closest('.news-ai-btn');
    if (aiBtn) {
      e.stopPropagation();
      await _onAIClick(aiBtn, listEl);
      return;
    }
    // 一键加候选池
    const poolBtn = e.target.closest('.news-pool-btn');
    if (poolBtn) {
      e.stopPropagation();
      await _onAddToPool(poolBtn);
      return;
    }
    // 品种代码 → 切到该品种主图
    const symEl = e.target.closest('[data-symbol]');
    if (symEl) {
      e.stopPropagation();
      const sym = symEl.dataset.symbol;
      const mkt = symEl.dataset.market || _inferMarketBySymbol(sym);
      if (sym) {
        if (mkt && mkt !== window.currentMarket && typeof switchMarket === 'function') {
          switchMarket(mkt);
          setTimeout(() => switchSymbol && switchSymbol(sym, mkt), 200);
        } else if (typeof switchSymbol === 'function') {
          switchSymbol(sym, window.currentMarket);
        }
      }
    }
  }

  function _inferMarketBySymbol(s) {
    if (!s) return '';
    const u = s.toUpperCase();
    if (/-USDT$|-USD$|-USDC$/.test(u)) return 'crypto';
    if (/\.HK$/.test(u)) return 'hk';
    if (/^\d{6}$/.test(u)) return 'cn';
    if (/^[A-Z]{1,5}$/.test(u)) return 'us';
    return '';
  }

  async function _onAIClick(btn, listEl) {
    const newsId = btn.dataset.newsId;
    const item = _items.find(x => x.id === newsId);
    const resultEl = listEl.querySelector(`.news-ai-result[data-news-id="${CSS.escape(newsId)}"]`);
    if (!resultEl || !item) return;

    // 已展开 → 折叠
    if (resultEl.style.display !== 'none') {
      resultEl.style.display = 'none';
      item._showAi = false;
      return;
    }
    resultEl.style.display = 'block';
    item._showAi = true;

    // 已有解读 → 直接展示（无需重新调用 LLM）
    if (item.ai_analysis) {
      resultEl.innerHTML = _renderAIAnalysis(item.ai_analysis, true);
      return;
    }

    // 触发 LLM 解读
    resultEl.innerHTML = '<span style="color:var(--text-tertiary);">🤖 AI 分析中（推理模型可能 30-90s）...</span>';
    const origText = btn.textContent;
    btn.disabled = true;
    btn.textContent = '⏳ 分析中...';
    btn.style.opacity = '0.6';
    if (_aiAbortControllers[newsId]) try { _aiAbortControllers[newsId].abort(); } catch {}
    const ctrl = new AbortController();
    _aiAbortControllers[newsId] = ctrl;
    const timer = setTimeout(() => ctrl.abort(), 180000);
    try {
      const resp = await fetch(`/api/news/flash/${newsId}/analyze`, { method: 'POST', signal: ctrl.signal });
      clearTimeout(timer);
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        resultEl.innerHTML = `<span style="color:var(--color-down);">❌ HTTP ${resp.status}: ${_esc(err.detail || '后端返回错误')}</span>`;
        return;
      }
      const d = await resp.json();
      const ai = typeof d.ai_analysis === 'string' ? JSON.parse(d.ai_analysis) : d.ai_analysis;
      item.ai_analysis = ai;   // 持久化到内存
      resultEl.innerHTML = _renderAIAnalysis(ai, !!d.cached);
      btn.textContent = '🤖 已分析';
    } catch (e) {
      if (e.name === 'AbortError') {
        resultEl.innerHTML = '<span style="color:var(--text-tertiary);">已取消</span>';
      } else {
        resultEl.innerHTML = `<span style="color:var(--color-down);">❌ ${_esc(e.message || e)}</span>`;
      }
    } finally {
      btn.disabled = false;
      btn.style.opacity = '1';
      if (btn.textContent === '⏳ 分析中...') btn.textContent = origText;
      delete _aiAbortControllers[newsId];
    }
  }

  async function _onAddToPool(btn) {
    const symbol = btn.dataset.symbol;
    const market = btn.dataset.market;
    if (!symbol || !market) return;
    if (market === 'crypto') {
      if (typeof showToast === 'function') showToast('加密币种不入候选池（已默认监控 6 币）', 'warning');
      return;
    }
    if (!confirm(`将 ${symbol} (${market.toUpperCase()}) 加入候选池？`)) return;
    btn.disabled = true;
    const orig = btn.textContent;
    btn.textContent = '⏳';
    try {
      const resp = await fetch('/api/pool', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol, market,
          reason: `新闻驱动手动添加: ${(_items.find(it => (it.categories || []).includes(symbol)) || {}).title || symbol}`.substring(0, 200),
          score: 50,
        }),
      });
      const d = await resp.json();
      if (!resp.ok) throw new Error(d.detail || `HTTP ${resp.status}`);
      if (typeof showToast === 'function') showToast(`✅ ${symbol} 已加入候选池（AI 诊断已自动触发）`, 'success', 3500);
      btn.textContent = '✅ 已加';
    } catch (e) {
      btn.disabled = false;
      btn.textContent = orig;
      if (typeof showToast === 'function') showToast(`加入失败: ${e.message}`, 'error', 3000);
    }
  }

  function _renderItem(item) {
    const SENT_CHIP_CLASS = { bullish: 'oc-chip-up', bearish: 'oc-chip-down', neutral: 'oc-chip-neutral' };
    const sentChip = `<span class="oc-chip ${SENT_CHIP_CLASS[item.sentiment] || 'oc-chip-neutral'}">${SENTIMENT_ICON[item.sentiment] || ''}${item.sentiment === 'bullish' ? ' 利好' : item.sentiment === 'bearish' ? ' 利空' : ' 中性'}</span>`;
    const stars = `<span class="oc-star-tag">${'★'.repeat(item.importance)}</span>`;
    const time = new Date(item.published_at).toLocaleString('zh-CN', { hour: '2-digit', minute: '2-digit', month: 'numeric', day: 'numeric' });
    // 品种 chip
    const cats = item.categories.length
      ? item.categories.map(c => {
          const mkt = _inferMarketBySymbol(c);
          const poolBadge = (mkt && mkt !== 'crypto')
            ? `<button class="news-pool-btn" data-symbol="${_esc(c)}" data-market="${_esc(mkt)}" title="一键加入候选池" style="font-size:9px;padding:1px 5px;margin-left:2px;background:transparent;border:1px solid var(--color-up);border-radius:3px;color:var(--color-up);cursor:pointer;">+池</button>`
            : '';
          return `<span class="news-cat oc-chip oc-chip-info" data-symbol="${_esc(c)}" data-market="${_esc(mkt)}" style="cursor:pointer;margin-right:2px;">${_esc(c)}</span>${poolBadge}`;
        }).join(' ')
      : '';
    const linkBtn = item.url
      ? `<a href="${_esc(item.url)}" target="_blank" rel="noopener noreferrer" class="oc-text-xs oc-muted" style="margin-left:6px;">原文 ↗</a>`
      : '';
    const aiBtn = item.importance >= 3
      ? `<button class="news-ai-btn" data-news-id="${_esc(item.id)}" style="font-size:10px;padding:2px 8px;margin-left:6px;background:var(--color-purple);color:white;border:none;border-radius:3px;cursor:pointer;">${item.ai_analysis ? '🤖 已分析' : '🤖 AI 解读'}</button>`
      : '';
    // 宏观
    const macroBadge = item.is_macro_data
      ? (() => {
          const dev = item.macro_deviation_pct;
          const devTxt = dev != null ? `${dev > 0 ? '+' : ''}${(+dev).toFixed(2)}%` : '';
          const isStrong = dev != null && Math.abs(dev) >= 1;
          return `<span class="oc-chip ${isStrong ? 'oc-chip-warn' : 'oc-chip-purple'}" title="宏观数据：实际 ${item.macro_actual ?? '-'} / 预期 ${item.macro_forecast ?? '-'}">📊 ${item.macro_type?.toUpperCase() || 'MACRO'}${devTxt ? ' ' + devTxt : ''}</span>`;
        })()
      : '';
    // 市场归属
    const marketBadges = (item.markets || []).filter(m => m !== 'other').map(m =>
      `<span class="oc-chip oc-chip-neutral" style="font-size:9px;">${m.toUpperCase()}</span>`
    ).join(' ');
    return `
      <div class="news-item" data-news-id="${_esc(item.id)}" style="padding:10px 14px;border-bottom:1px solid var(--border-secondary);transition:background 0.15s;">
        <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
          ${stars}
          ${sentChip}
          ${marketBadges}${macroBadge}
          <span class="oc-text-xs oc-muted">${time} · ${_esc(item.source)}</span>
          ${linkBtn}${aiBtn}
        </div>
        <div style="margin:6px 0;color:var(--text-primary);font-size:13px;line-height:1.5;">${_esc(item.title)}</div>
        ${cats ? `<div style="margin-top:4px;">${cats}</div>` : ''}
        <div class="news-ai-result" data-news-id="${_esc(item.id)}" style="display:none;margin-top:8px;padding:10px;background:var(--bg-tertiary);border-left:3px solid var(--color-purple);border-radius:0 4px 4px 0;font-size:11px;"></div>
      </div>
    `;
  }

  function _renderAIAnalysis(ai, cached) {
    if (!ai) return '<span style="color:var(--text-tertiary);">无解读结果</span>';
    const viewColor = SENTIMENT_COLOR[ai.overall_view] || SENTIMENT_COLOR.neutral;
    const cachedTag = cached ? '<span style="color:var(--text-tertiary);font-size:10px;">(已缓存)</span>' : '';
    const impacts = (ai.impacts || []).map(i => {
      const dirColor = SENTIMENT_COLOR[i.direction] || '';
      return `<div style="margin-left:8px;color:${dirColor};">▸ <span style="cursor:pointer;text-decoration:underline;" data-symbol="${_esc(i.symbol)}">${_esc(i.symbol)}</span> (${_esc(i.direction)}, ${_esc(i.horizon || '')}, 强度 ${(i.strength || 0).toFixed(2)}): ${_esc(i.reason || '')}</div>`;
    }).join('');
    const reasons = (ai.reasons || []).map(r => `<li>${_esc(r)}</li>`).join('');
    const risks = (ai.risks || []).map(r => `<li>${_esc(r)}</li>`).join('');
    const lvl = ai.key_levels || {};
    // 宏观影响（如有）单独显示
    const macroBlock = ai.macro_impact ? (() => {
      const mi = ai.macro_impact;
      const lvlColor = mi.level === 'significant' ? 'var(--color-warning)' : 'var(--text-secondary)';
      return `<div style="margin-top:6px;padding:6px 8px;background:rgba(155,109,213,0.1);border-radius:3px;">
        <strong style="color:${lvlColor};">📊 宏观: ${_esc(mi.event?.toUpperCase())}</strong>
        ${mi.actual != null ? `实际 ${mi.actual} vs 预期 ${mi.forecast}（偏差 ${mi.deviation}%, ${_esc(mi.tone)}/${_esc(mi.level)}）` : '（无具体数值）'}
      </div>`;
    })() : '';
    return `
      <div style="line-height:1.6;">
        <div><strong style="color:${viewColor};">${_esc(ai.overall_view?.toUpperCase() || '')}</strong> ${cachedTag}</div>
        <div style="margin:4px 0;color:var(--text-primary);">${_esc(ai.summary || '')}</div>
        ${impacts ? `<div style="margin-top:4px;"><strong>影响品种：</strong>${impacts}</div>` : ''}
        ${reasons ? `<div style="margin-top:4px;"><strong>支持理由：</strong><ul style="margin:2px 0 0 16px;">${reasons}</ul></div>` : ''}
        ${risks ? `<div style="margin-top:4px;"><strong>潜在风险：</strong><ul style="margin:2px 0 0 16px;">${risks}</ul></div>` : ''}
        ${(lvl.support || lvl.resistance) ? `<div style="margin-top:4px;"><strong>关键价位：</strong>支撑 ${lvl.support || '-'} / 阻力 ${lvl.resistance || '-'}</div>` : ''}
        ${macroBlock}
      </div>
    `;
  }

  // 给外部模块（app.js switchMarket）调用，切换市场时同步刷新
  function setMarketFilter(market) {
    if (_filterMarket === market) return;
    _filterMarket = market;
    const sel = document.querySelector('#news-filter-market');
    if (sel) sel.value = market;
    refresh();
  }

  return { init, refresh, render, setMarketFilter };
})();

window.News = News;
