/* ============================================================
   OpenChart Pro - AI驱动智能选股系统
   用户无需手动设置筛选条件，AI自动推荐 + 技术面自动扫描
   ============================================================ */

const Screener = (() => {
  let lastRefresh = 0;
  let currentMarket = 'crypto';
  const REFRESH_INTERVAL = 30 * 60 * 1000; // 30分钟缓存，避免频繁调AI

  function init() {
    // 绑定市场切换按钮
    document.querySelectorAll('.screener-market-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.screener-market-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentMarket = btn.dataset.market;
        refresh();
      });
    });

    // 绑定刷新按钮
    document.getElementById('screener-refresh')?.addEventListener('click', () => {
      lastRefresh = 0; // 强制刷新
      refresh();
    });
  }

  /**
   * 当面板可见时自动刷新（超过5分钟未更新则自动触发）
   */
  function autoRefreshIfNeeded() {
    const now = Date.now();
    if (now - lastRefresh > REFRESH_INTERVAL) {
      refresh();
    }
  }

  /**
   * 主刷新函数 — 并行请求AI推荐 + 技术信号
   */
  async function refresh() {
    const aiSection = document.getElementById('screener-ai-section');
    const techSection = document.getElementById('screener-tech-section');
    const refreshBtn = document.getElementById('screener-refresh');
    const updatedEl = document.getElementById('screener-updated');

    if (!aiSection || !techSection) return;

    // 显示 loading
    if (refreshBtn) { refreshBtn.disabled = true; refreshBtn.textContent = '⏳ 加载中...'; }
    aiSection.innerHTML = _renderLoading('正在获取 AI 推荐...');
    techSection.innerHTML = _renderLoading('正在扫描技术信号...');

    // 先获取AI推荐
    let aiData = null;
    try {
      aiData = await fetchAIRecommendations(currentMarket);
      renderAICards(aiData);
    } catch (e) {
      aiSection.innerHTML = _renderError('AI 推荐', e.message);
    }

    // 用AI推荐的品种做技术面扫描（不是另外一套品种）
    techSection.innerHTML = _renderLoading('正在对推荐品种进行技术面扫描...');
    try {
      const symbols = aiData?.recommendations?.map(r => r.symbol) || [];
      const techData = await fetchTechSignals(currentMarket, symbols);
      renderTechTable(techData, aiData?.recommendations);
    } catch (e) {
      techSection.innerHTML = _renderError('技术信号', e.message);
    }

    lastRefresh = Date.now();
    if (updatedEl) {
      const t = new Date();
      updatedEl.textContent = `${t.getHours().toString().padStart(2,'0')}:${t.getMinutes().toString().padStart(2,'0')} 更新`;
    }
    if (refreshBtn) { refreshBtn.disabled = false; refreshBtn.textContent = '🔄 刷新'; }
  }

  // ------------------------------------------------------------------
  // 数据获取
  // ------------------------------------------------------------------

  async function fetchAIRecommendations(market) {
    const resp = await fetch('/api/screener/ai-recommend', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ market }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return await resp.json();
  }

  async function fetchTechSignals(market, symbols) {
    const resp = await fetch('/api/screener/tech-signals', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ market, symbols: symbols || [] }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return await resp.json();
  }

  // ------------------------------------------------------------------
  // 渲染：AI 热门推荐卡片
  // ------------------------------------------------------------------

  function renderAICards(data) {
    const el = document.getElementById('screener-ai-section');
    if (!el) return;

    const recs = data.recommendations || [];
    if (!recs.length) {
      el.innerHTML = `<div class="screener-section-title">🔥 AI 热门推荐</div>
        <div class="screener-empty">暂无推荐数据，请稍后刷新</div>`;
      return;
    }

    const source = data.source || '';
    let h = `<div class="screener-section-title">🔥 AI 热门推荐
      <span class="screener-source">${source}</span></div>
      <div class="screener-cards-grid">`;

    for (const r of recs) {
      const score = Math.round(r.score || 0);
      const changePct = r.change_pct || 0;
      const changeClass = changePct > 0 ? 'up' : changePct < 0 ? 'down' : '';
      const changeStr = (changePct >= 0 ? '+' : '') + changePct.toFixed(2) + '%';
      const priceStr = r.price != null ? _fmtPrice(r.price) : '--';

      // 评分颜色
      let scoreColor = 'var(--text-tertiary)';
      if (score >= 80) scoreColor = '#00E676';
      else if (score >= 60) scoreColor = 'var(--color-up)';
      else if (score >= 40) scoreColor = 'var(--color-warning)';
      else scoreColor = 'var(--color-down)';

      // 星星
      const fullStars = Math.min(5, Math.round(score / 20));
      const stars = '★'.repeat(fullStars) + '☆'.repeat(5 - fullStars);

      // 技术信号标签
      const signalTags = (r.signals || []).map(s => `<span class="screener-signal-tag">${s}</span>`).join('');

      // action 中文映射
      const actionMap = { buy: '买入', strong_buy: '强烈买入', sell: '卖出', hold: '持有', watch: '关注', avoid: '回避' };
      const actionCN = actionMap[(r.action || '').toLowerCase()] || r.action || '';

      // action颜色
      const actionColors = { buy: '#0ecb81', strong_buy: '#0ecb81', sell: '#f6465d', hold: '#e4a853', watch: '#848e9c', avoid: '#f6465d' };
      const actionColor = actionColors[(r.action || '').toLowerCase()] || '#848e9c';

      h += `<div class="screener-card">
        <div class="screener-card-top">
          <div>
            <div class="screener-card-symbol">${r.symbol}</div>
            <div class="screener-card-name">${r.name || ''}</div>
          </div>
          <span class="screener-card-action" style="color:${actionColor};border-color:${actionColor};cursor:pointer;" title="点击加入自选" onclick="event.stopPropagation();Screener.addToWatchlist('${r.symbol}','${(r.name||'').replace(/'/g,'')}','${r.market||currentMarket}')">+ ${actionCN}</span>
        </div>
        <div class="screener-card-price">${priceStr}</div>
        <div class="screener-card-change ${changeClass}">${changeStr}</div>
        <div class="screener-card-stars" style="color:${scoreColor};">${stars} <span style="font-size:11px;">AI评分 ${score}</span></div>
        <div class="screener-card-topic">🔥 ${r.hot_topic || ''}</div>
        ${r.reason ? `<div class="screener-card-reason">${r.reason}</div>` : ''}
        ${r.risk ? `<div class="screener-card-risk">⚠ ${r.risk}</div>` : ''}
        ${signalTags ? `<div class="screener-card-signals">${signalTags}</div>` : ''}
        <div class="screener-card-actions">
          <button class="btn btn-sm btn-primary screener-btn-kline" onclick="Screener.viewKline('${r.symbol}','${r.market || currentMarket}')">📊 查看K线</button>
          <button class="btn btn-sm screener-btn-judge" onclick="Screener.viewAIJudge('${r.symbol}')">🤖 AI研判</button>
        </div>
      </div>`;
    }

    h += '</div>';
    el.innerHTML = h;
  }

  // ------------------------------------------------------------------
  // 渲染：技术面信号表格
  // ------------------------------------------------------------------

  function renderTechTable(data, aiRecs) {
    const el = document.getElementById('screener-tech-section');
    if (!el) return;

    // 构建名称映射
    const nameMap = {};
    if (aiRecs) aiRecs.forEach(r => { nameMap[r.symbol] = r.name; });

    const signals = data.signals || [];
    if (!signals.length) {
      el.innerHTML = `<div class="screener-section-title">📊 技术面信号（自动筛选）</div>
        <div class="screener-empty">暂未发现明显技术信号</div>`;
      return;
    }

    let h = `<div class="screener-section-title">📊 技术面信号（自动筛选）
      <span class="screener-source">扫描 ${signals.length} 个信号</span></div>
      <table class="screener-tech-table">
        <thead>
          <tr>
            <th>品种</th>
            <th>价格</th>
            <th>涨跌</th>
            <th>信号</th>
            <th>RSI</th>
            <th>MACD</th>
            <th>量比</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>`;

    for (const s of signals) {
      const changePct = s.change_pct || 0;
      const changeClass = changePct > 0 ? 'up' : changePct < 0 ? 'down' : '';
      const changeStr = (changePct >= 0 ? '+' : '') + changePct.toFixed(2) + '%';
      const priceStr = s.price != null ? _fmtPrice(s.price) : '--';
      const rsiStr = s.rsi != null ? s.rsi.toFixed(1) : '--';
      const volStr = s.volume_ratio != null ? s.volume_ratio.toFixed(1) + 'x' : '--';

      // RSI颜色
      let rsiClass = '';
      if (s.rsi != null) {
        if (s.rsi <= 30) rsiClass = 'rsi-oversold';
        else if (s.rsi >= 70) rsiClass = 'rsi-overbought';
      }

      // MACD趋势颜色
      const macdClass = (s.macd_trend || '').includes('多') ? 'up' : (s.macd_trend || '').includes('空') ? 'down' : '';

      const sName = nameMap[s.symbol] || '';
      h += `<tr>
        <td><strong>${s.symbol}</strong>${sName ? '<br><span style="font-size:11px;color:var(--text-tertiary);">' + sName + '</span>' : ''}</td>
        <td>${priceStr}</td>
        <td class="${changeClass}">${changeStr}</td>
        <td><span class="screener-signal-badge">${s.signal_type || '--'}</span></td>
        <td class="${rsiClass}">${rsiStr}</td>
        <td class="${macdClass}">${s.macd_trend || '--'}</td>
        <td>${volStr}</td>
        <td><button class="btn btn-xs screener-btn-kline" onclick="Screener.viewKline('${s.symbol}','${s.market || currentMarket}')">K线</button></td>
      </tr>`;
    }

    h += '</tbody></table>';
    el.innerHTML = h;
  }

  // ------------------------------------------------------------------
  // 操作函数
  // ------------------------------------------------------------------

  function viewKline(symbol, market) {
    if (typeof switchMarket === 'function' && market) {
      switchMarket(market);
    }
    if (typeof switchSymbol === 'function') {
      switchSymbol(symbol, market || window.currentMarket);
    }
  }

  function viewAIJudge(symbol) {
    if (typeof switchBottomTab === 'function') {
      switchBottomTab('aijudge');
    }
    if (typeof AIJudge !== 'undefined' && AIJudge.analyze) {
      AIJudge.analyze(symbol);
    }
  }

  // ------------------------------------------------------------------
  // 辅助函数
  // ------------------------------------------------------------------

  function _fmtPrice(p) {
    if (p >= 1000) return p.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (p >= 1) return p.toFixed(2);
    if (p >= 0.01) return p.toFixed(4);
    return p.toFixed(6);
  }

  function _renderLoading(msg) {
    return `<div class="screener-loading">
      <div class="screener-spinner"></div>
      <span>${msg}</span>
    </div>`;
  }

  function _renderError(section, msg) {
    return `<div class="screener-section-title">${section}</div>
      <div class="screener-error">加载失败: ${msg || '未知错误'}</div>`;
  }

  function addToWatchlist(symbol, name, market) {
    // 港股需要加.HK后缀
    let wlSymbol = symbol;
    if (market === 'hk' && !symbol.endsWith('.HK')) {
      const code = symbol.replace(/^0+/, '') || '0';
      wlSymbol = code.padStart(4, '0') + '.HK';
    }
    // 调用Watchlist的add方法
    if (typeof Watchlist !== 'undefined' && Watchlist.add) {
      Watchlist.add(wlSymbol, name || '');

    } else {
      showToast('自选列表模块未加载', 'error');
    }
  }

  return { init, refresh, autoRefreshIfNeeded, viewKline, viewAIJudge, addToWatchlist };
})();
