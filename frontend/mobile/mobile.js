// OpenChart Pro Mobile — 7 tab SPA
// 复用现有 API：
//   /api/auto-trade/status       总览 + 池数据 + 系统配置
//   /api/news/flash              新闻快讯列表
//   /api/news/flash/{id}/analyze 触发 AI 深度解读
//   /api/news/cost               LLM 今日成本
//   /api/pool                    候选池列表
//   /api/pool/{id}/diagnosis     候选池 AI 诊断
//   /api/signals                 策略信号列表
//   /api/signals/{id}            信号详情（含 related_news + pool_context）
//   /api/positions               持仓
//   /api/positions/advices/latest 每个持仓的最新 AI 建议
//   /api/auto-trade/log          自动交易历史
//   /api/trade-review                 复盘列表
//   /api/trade-review/{position_id}   单笔复盘详情
//   /api/trade-review/lessons/top     教训库
//   /api/auto-trade/toggle       开关
//   /api/auto-trade/summary-now  立即推持仓简报
//   /api/settings/telegram-test  Telegram 测试

(function() {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);

  // v12.21.0 PR1: 重新设计 5 tab 结构 (按业务流: 总览/行情/交易/学习/设置)
  const PAGE_TITLES = {
    home: '总览', market: '行情', trade: '交易',
    learn: '学习', settings: '设置',
  };
  const SUB_TITLES = {
    'home/home':         '总览',
    'market/news':       '新闻快讯',
    'market/pool':       '候选池',
    'market/signals':    '策略信号',
    'market/library':    '策略库',
    'market/combos':     '共振组合',
    'trade/spot':        '现货持仓',
    'trade/swap':        '⚡ 合约持仓',
    'trade/orders':      '订单流水',
    'trade/rejected':    '拒单记录',
    'trade/control':     '自动交易控制',
    'learn/reviews':     '复盘记录',
    'learn/lessons':     '教训库',
    'learn/rules':       '风控规则',
    'learn/weekly':      '周报',
    'settings/notify':   '通知',
    'settings/sources':  '新闻源',
    'settings/llm':      'LLM 配额',
    'settings/system':   '系统状态',
  };
  const MARKET_LABEL = { crypto: '加密', us: '美股', hk: '港股', cn: 'A股', macro: '宏观' };
  const ACTION_ICON = { open: '📥', add: '➕', reduce: '➖', close: '🏁' };
  const ACTION_LABEL = { open: '开仓', add: '加仓', reduce: '减仓', close: '平仓' };
  const ADVICE_LABEL_CN = { hold: '继续持有', reduce: '减仓', add: '加仓', close: '平仓' };
  const VERDICT_LABEL = {
    confirm: '✅ 已确认', warn: '⚠️ 警示', reject: '❌ 已拒绝',
    skipped: '⊘ 已跳过', llm_error: '⛔ LLM错', stale: '⌛ 已过期',
  };
  const VERDICT_CLASS = {
    confirm: 'up', warn: 'warn', reject: 'down',
    skipped: 'muted', llm_error: 'down', stale: 'muted',
  };
  const POOL_STATUS_LABEL = {
    active: '活跃', cooling: '冷却', archived: '已归档', candidate: '候选',
    monitoring: '正式关注', adopted: '已采纳', disabled: '已禁用', expired: '已过期',
  };
  const GRADE_LABEL = { A: '优', B: '良', C: '一般', D: '差' };

  // v12.21.0 PR1: 5 main tab × N sub-tab 默认路由
  const TAB_DEFAULT_SUB = {
    home: 'home',
    market: 'news',
    trade: 'spot',
    learn: 'reviews',
    settings: 'notify',
  };

  let _state = {
    activeTab: 'home',
    activeSub: 'home',         // 'home/home' / 'market/news' 等的右半
    signalFilter: 'all',
    newsFilter: 'all',
    poolFilter: 'all',
    rejectedFilter: 'all',
    orderFilter: 'all',        // v12.21.0: 订单流水 filter (pending/filled/cancelled)
    cache: {
      status: null, positions: null, signals: null, history: null,
      news: null, pool: null, reviews: null, lessons: null,
      advices: null, llmCost: null, riskRules: null, strategies: null,
      rejectedTrades: null,
      swapAcct: null, swapPos: null, swapOrders: null,  // v12.21.0: swap 缓存
    },
    lastUpdate: 0,
  };

  // ─── Main tab 切换 (5 个底部 tab) ───
  $$('.tab').forEach(tab => {
    tab.addEventListener('click', () => switchTab(tab.dataset.tab));
  });

  function switchTab(name) {
    _state.activeTab = name;
    _state.activeSub = TAB_DEFAULT_SUB[name] || 'home';
    $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
    $$('.page').forEach(p => p.classList.toggle('active', p.dataset.page === name));
    // 切到 main tab 后，重置该 tab 下的 sub-tab UI 到 default
    const activePage = $(`.page[data-page="${name}"]`);
    if (activePage) {
      activePage.querySelectorAll('.seg').forEach(s => {
        s.classList.toggle('active', s.dataset.sub === _state.activeSub);
      });
      activePage.querySelectorAll('.subpage').forEach(sp => {
        sp.hidden = sp.dataset.subpage !== _state.activeSub;
      });
    }
    updatePageTitle();
    refresh();
  }

  // ─── Sub-tab 切换 (segment 内部) ───
  $$('.seg').forEach(seg => {
    seg.addEventListener('click', () => switchSubTab(seg.dataset.sub, seg));
  });

  function switchSubTab(subName, clickedSeg) {
    _state.activeSub = subName;
    const activePage = $(`.page[data-page="${_state.activeTab}"]`);
    if (!activePage) return;
    activePage.querySelectorAll('.seg').forEach(s => {
      s.classList.toggle('active', s === clickedSeg || s.dataset.sub === subName);
    });
    activePage.querySelectorAll('.subpage').forEach(sp => {
      sp.hidden = sp.dataset.subpage !== subName;
    });
    updatePageTitle();
    refresh();
  }

  function updatePageTitle() {
    const key = `${_state.activeTab}/${_state.activeSub}`;
    $('#page-title').textContent = SUB_TITLES[key] || PAGE_TITLES[_state.activeTab] || _state.activeTab;
  }

  // ─── Filter chips ───
  $$('.chip[data-vfilter]').forEach(chip => {
    chip.addEventListener('click', () => {
      _state.signalFilter = chip.dataset.vfilter;
      $$('.chip[data-vfilter]').forEach(c => c.classList.toggle('active', c === chip));
      renderSignals();
    });
  });
  $$('.chip[data-nfilter]').forEach(chip => {
    chip.addEventListener('click', () => {
      _state.newsFilter = chip.dataset.nfilter;
      $$('.chip[data-nfilter]').forEach(c => c.classList.toggle('active', c === chip));
      _state.cache.news = null;
      renderNews();
    });
  });
  $$('.chip[data-pfilter]').forEach(chip => {
    chip.addEventListener('click', () => {
      _state.poolFilter = chip.dataset.pfilter;
      $$('.chip[data-pfilter]').forEach(c => c.classList.toggle('active', c === chip));
      renderPool();
    });
  });
  // v12.19.0: 拒单 filter
  $$('.chip[data-rjfilter]').forEach(chip => {
    chip.addEventListener('click', () => {
      _state.rejectedFilter = chip.dataset.rjfilter;
      $$('.chip[data-rjfilter]').forEach(c => c.classList.toggle('active', c === chip));
      renderRejected();
    });
  });
  // v12.21.0: 订单流水 filter
  $$('.chip[data-ofilter]').forEach(chip => {
    chip.addEventListener('click', () => {
      _state.orderFilter = chip.dataset.ofilter;
      $$('.chip[data-ofilter]').forEach(c => c.classList.toggle('active', c === chip));
      renderOrderFlow();
    });
  });

  // ─── 设置页交互 ───
  $('#auto-trade-toggle').addEventListener('change', async (e) => {
    try {
      const r = await fetch('/api/auto-trade/toggle', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({enabled: e.target.checked}),
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      toast(e.target.checked ? '✅ 自动交易已开启' : '🔴 自动交易已关闭', 'up');
      _state.cache.status = null; refresh();
    } catch (err) {
      toast('❌ 切换失败: ' + err.message, 'down');
      e.target.checked = !e.target.checked;
    }
  });

  $('#tg-test-btn').addEventListener('click', async () => {
    toast('⏳ 发送中…');
    try {
      const r = await fetch('/api/settings/telegram-test', {method: 'POST'});
      const d = await r.json();
      if (d.ok) toast('✅ 已发送到 Telegram', 'up');
      else toast('❌ ' + (d.error || '失败'), 'down');
    } catch (err) {
      toast('❌ ' + err.message, 'down');
    }
  });

  $('#summary-now-btn').addEventListener('click', async () => {
    toast('⏳ 推送中…');
    try {
      const r = await fetch('/api/auto-trade/summary-now', {method: 'POST'});
      if (r.status === 404) { toast('该功能等下一个版本', 'warn'); return; }
      const d = await r.json();
      if (d.ok) toast('✅ 已推送', 'up');
      else toast('❌ ' + (d.error || '失败'), 'down');
    } catch (err) {
      toast('❌ ' + err.message, 'down');
    }
  });

  // ─── 刷新按钮 ───
  $('#refresh-btn').addEventListener('click', () => {
    Object.keys(_state.cache).forEach(k => _state.cache[k] = null);
    const btn = $('#refresh-btn');
    btn.classList.add('spinning');
    setTimeout(() => btn.classList.remove('spinning'), 600);
    refresh();
  });

  // ─── Sheet 抽屉 ───
  function openSheet(title, html) {
    $('#sheet-title').textContent = title;
    $('#sheet-content').innerHTML = html;
    $('#sheet').hidden = false;
  }
  function closeSheet() { $('#sheet').hidden = true; }
  $('#sheet-close').addEventListener('click', closeSheet);
  $('.sheet-mask').addEventListener('click', closeSheet);

  // ─── 数据获取 ───
  async function fetchJSON(url, opts) {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }
  async function loadStatus() {
    if (_state.cache.status) return _state.cache.status;
    _state.cache.status = await fetchJSON('/api/auto-trade/status');
    return _state.cache.status;
  }
  async function loadPositions() {
    if (_state.cache.positions) return _state.cache.positions;
    _state.cache.positions = await fetchJSON('/api/positions');
    return _state.cache.positions;
  }
  async function loadAdvices() {
    if (_state.cache.advices) return _state.cache.advices;
    try {
      _state.cache.advices = await fetchJSON('/api/positions/advices/latest');
    } catch { _state.cache.advices = []; }
    return _state.cache.advices;
  }
  async function loadSignals() {
    if (_state.cache.signals) return _state.cache.signals;
    _state.cache.signals = await fetchJSON('/api/signals?limit=100');
    return _state.cache.signals;
  }
  async function loadHistory() {
    if (_state.cache.history) return _state.cache.history;
    _state.cache.history = await fetchJSON('/api/auto-trade/log?limit=50');
    return _state.cache.history;
  }
  async function loadNews() {
    if (_state.cache.news) return _state.cache.news;
    const f = _state.newsFilter;
    let url = '/api/news/flash?limit=80';
    if (f === 'important') url += '&importance_min=3';
    else if (['crypto','us','hk','cn','macro'].includes(f)) url += '&market=' + f;
    _state.cache.news = await fetchJSON(url);
    return _state.cache.news;
  }
  async function loadPool() {
    if (_state.cache.pool) return _state.cache.pool;
    _state.cache.pool = await fetchJSON('/api/pool?limit=200');
    return _state.cache.pool;
  }
  async function loadReviews() {
    if (_state.cache.reviews) return _state.cache.reviews;
    _state.cache.reviews = await fetchJSON('/api/trade-review?limit=50');
    return _state.cache.reviews;
  }
  async function loadLessons() {
    if (_state.cache.lessons) return _state.cache.lessons;
    _state.cache.lessons = await fetchJSON('/api/trade-review/lessons/top?limit=80');
    return _state.cache.lessons;
  }
  async function loadLLMCost() {
    if (_state.cache.llmCost) return _state.cache.llmCost;
    try { _state.cache.llmCost = await fetchJSON('/api/news/cost'); }
    catch { _state.cache.llmCost = null; }
    return _state.cache.llmCost;
  }

  // ─── 工具 ───
  function fmtMoney(v, ccy = 'USD') {
    if (v == null || isNaN(v)) return '—';
    const sym = ccy === 'CNY' ? '¥' : ccy === 'HKD' ? 'HK$' : '$';
    const sign = v < 0 ? '-' : '';
    const abs = Math.abs(v);
    return `${sign}${sym}${abs.toLocaleString('en-US', {maximumFractionDigits: 2})}`;
  }
  function fmtPct(v) {
    if (v == null || isNaN(v)) return '—';
    return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;
  }
  function fmtPnl(v, ccy = 'USD') {
    if (v == null || isNaN(v)) return '—';
    const sym = ccy === 'CNY' ? '¥' : ccy === 'HKD' ? 'HK$' : '$';
    return `${v >= 0 ? '+' : '-'}${sym}${Math.abs(v).toFixed(2)}`;
  }
  function fmtTime(ts) {
    if (!ts) return '—';
    const d = ts > 1e12 ? new Date(ts) : new Date(ts * 1000);
    return d.toLocaleString('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'});
  }
  function fmtRelTime(ts) {
    if (!ts) return '—';
    const ms = ts > 1e12 ? ts : ts * 1000;
    const diff = (Date.now() - ms) / 1000;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return Math.floor(diff/60) + ' 分钟前';
    if (diff < 86400) return Math.floor(diff/3600) + ' 小时前';
    if (diff < 7*86400) return Math.floor(diff/86400) + ' 天前';
    return fmtTime(ts);
  }
  function escape(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }
  function stars(n) {
    n = Math.max(0, Math.min(5, parseInt(n) || 0));
    return '★'.repeat(n) + '☆'.repeat(5 - n);
  }
  function sentimentEmoji(s) {
    if (s == null) return '';
    if (s > 0.3) return '🟢';
    if (s < -0.3) return '🔴';
    return '⚪';
  }
  function toast(text, kind = '') {
    const t = $('#toast');
    t.textContent = text;
    t.className = 'toast show' + (kind ? ' ' + kind : '');
    setTimeout(() => t.classList.remove('show'), 2500);
  }

  // ═══════════════════════════════════════════════════════════
  // 1. 总览
  // ═══════════════════════════════════════════════════════════
  // v12.19.0: 主页 = 总览 + quick-stats 4 卡 + 各市场盈亏 + 最近成交/信号
  async function renderHome() {
    try {
      const [status, log, signals, positions, llmCost] = await Promise.all([
        loadStatus(), loadHistory(), loadSignals(), loadPositions(), loadLLMCost(),
      ]);

      const enabled = status.enabled;
      const badge = $('#enabled-badge');
      badge.textContent = enabled ? '自动交易 开' : '自动交易 关';
      badge.className = 'badge ' + (enabled ? 'on' : 'off');

      const pools = status.pools || [];
      let totalEquityUSD = 0, totalPnlUSD = 0, totalInitialUSD = 0;
      for (const p of pools) {
        totalEquityUSD += p.equity_usd || 0;
        totalPnlUSD += p.pnl_usd || 0;
        const fx = p.fx_to_usd || 1;
        totalInitialUSD += (p.initial_capital || 0) * fx;
      }
      $('#ov-equity').textContent = fmtMoney(totalEquityUSD);
      const pctTotal = totalInitialUSD > 0 ? (totalPnlUSD / totalInitialUSD * 100) : 0;
      $('#ov-pnl').innerHTML = `<span class="${totalPnlUSD>=0?'up':'down'}">${fmtPnl(totalPnlUSD)} (${fmtPct(pctTotal)})</span>`;

      // ─── Quick stats: 持仓数 / 今日交易 / 待重验 / AI 成本 ───
      const posCount = (positions || []).length;
      const now = Date.now();
      const todayStart = (() => {
        const d = new Date(); d.setHours(0,0,0,0); return d.getTime();
      })();
      const todayTrades = (log.items || []).filter(t =>
        t.status === 'executed' && (t.traded_at * 1000) >= todayStart
      ).length;
      // 待重验：confirm + 24h 内 + status='active' (排除已 acted/expired) + 未重验过
      // v12.19.1 (P2-A): 加 status='active' 过滤，避免数字偏大
      const sigItems = signals.items || [];
      const pendingReval = sigItems.filter(s =>
        s.ai_verdict === 'confirm'
        && (s.revalidated_at == 0 || !s.revalidated_at)
        && (s.status === 'active' || !s.status)
        && (now - (s.generated_at || 0)) < 24 * 3600 * 1000
      ).length;
      const todayCost = (llmCost && llmCost.today_total_usd) || 0;
      $('#home-stats').innerHTML = `
        <div class="stat-card">
          <div class="stat-label">📊 当前持仓</div>
          <div class="stat-value">${posCount}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">📥 今日成交</div>
          <div class="stat-value">${todayTrades}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">⏳ 待重验</div>
          <div class="stat-value ${pendingReval>0?'warn':''}">${pendingReval}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">💰 今日 AI</div>
          <div class="stat-value">$${todayCost.toFixed(3)}</div>
        </div>
      `;

      // 各市场盈亏卡
      const poolHtml = pools.sort((a,b)=> {
        const ord = {us_hk:0, cn:1, crypto:2};
        return (ord[a.pool_id]||9) - (ord[b.pool_id]||9);
      }).map(p => {
        const cls = (p.pnl||0) >= 0 ? 'up' : 'down';
        const ccy = p.currency || 'USD';
        return `<div class="pool-card ${cls}">
          <div>
            <div class="pool-name">${escape(p.name)} (${ccy})</div>
            <div class="pool-equity">${fmtMoney(p.equity, ccy)}</div>
            <div class="pool-pnl ${cls}">${fmtPnl(p.pnl, ccy)} (${fmtPct(p.pnl_pct)})</div>
            <div class="pool-meta">现金 ${fmtMoney(p.cash, ccy)} · 持仓 ${fmtMoney(p.positions_value, ccy)}</div>
          </div>
          <div class="pool-arrow">›</div>
        </div>`;
      }).join('');
      $('#ov-pools').innerHTML = poolHtml || '<div class="empty">暂无池数据</div>';

      const trades = (log.items || []).filter(t => t.status === 'executed').slice(0, 5);
      $('#ov-recent-trades-list').innerHTML = trades.length
        ? trades.map(renderTradeRow).join('')
        : '<div class="empty small">暂无成交</div>';

      const sigs = sigItems.filter(s => ['confirm','warn'].includes(s.ai_verdict)).slice(0, 5);
      $('#ov-recent-signals-list').innerHTML = sigs.length
        ? sigs.map(renderSignalRow).join('')
        : '<div class="empty small">暂无重点信号</div>';

      // 给最近信号 row 绑定点击事件
      $$('#ov-recent-signals-list .row').forEach(r => {
        r.addEventListener('click', () => openSignalDetail(r.dataset.id));
      });

    } catch (e) {
      console.error(e);
      $('#ov-equity').textContent = '加载失败';
    }
  }

  // ═══════════════════════════════════════════════════════════
  // 2. 新闻快讯
  // ═══════════════════════════════════════════════════════════
  async function renderNews() {
    try {
      const data = await loadNews();
      const items = data.items || [];
      if (!items.length) {
        $('#news-list').innerHTML = '<div class="empty"><div class="empty-icon">📰</div><div>暂无新闻</div></div>';
        return;
      }
      $('#news-list').innerHTML = items.map(renderNewsCard).join('');
      $$('#news-list .news-card').forEach(card => {
        card.addEventListener('click', () => openNewsDetail(card.dataset.id));
      });
    } catch (e) {
      console.error(e);
      $('#news-list').innerHTML = '<div class="empty">加载失败</div>';
    }
  }

  function renderNewsCard(n) {
    const imp = parseInt(n.importance) || 1;
    const sent = n.sentiment;
    let cls = `imp-${imp}`;
    if (sent > 0.3) cls = 'sent-up';
    else if (sent < -0.3) cls = 'sent-down';
    const aiBadge = n.ai_analysis ? '<span class="ai-badge">AI 已解读</span>' : '';
    const market = MARKET_LABEL[n.market] || n.market || '';
    return `<div class="news-card ${cls}" data-id="${escape(n.id)}">
      <div class="news-title">${escape(n.title || '').slice(0, 120)}</div>
      <div class="news-meta">
        <span>${escape(n.source || '')}</span>
        ${market ? `<span>${market}</span>` : ''}
        <span class="stars">${stars(imp)}</span>
        ${sent != null ? `<span>${sentimentEmoji(sent)}</span>` : ''}
        ${aiBadge}
        <span style="margin-left:auto;">${fmtRelTime(n.published_at || n.collected_at)}</span>
      </div>
    </div>`;
  }

  async function openNewsDetail(id) {
    openSheet('新闻详情', '<div class="empty">加载中…</div>');
    try {
      const n = await fetchJSON('/api/news/flash/' + encodeURIComponent(id));
      const aiHtml = n.ai_analysis
        ? renderAIAnalysis(n.ai_analysis)
        : `<div class="muted">尚无 AI 解读</div>
           <button class="btn" id="ai-trigger-btn" style="margin-top:8px;">🤖 触发 LLM 深度解读</button>`;
      const body = `
        <h4>标题</h4>
        <div>${escape(n.title || '')}</div>
        <h4>正文</h4>
        <div style="white-space:pre-wrap;">${escape((n.content || '').slice(0, 2000))}</div>
        <h4>来源 / 时间</h4>
        <div class="kv-row"><span class="k">来源</span><span class="v">${escape(n.source || '')}</span></div>
        <div class="kv-row"><span class="k">发布</span><span class="v">${fmtTime(n.published_at)}</span></div>
        <div class="kv-row"><span class="k">重要度</span><span class="v">${stars(n.importance || 1)}</span></div>
        <div class="kv-row"><span class="k">情绪</span><span class="v">${sentimentEmoji(n.sentiment)} ${(n.sentiment||0).toFixed(2)}</span></div>
        ${n.url ? `<div class="kv-row"><span class="k">原文</span><a href="${escape(n.url)}" target="_blank">打开</a></div>` : ''}
        <h4>🤖 AI 深度解读</h4>
        ${aiHtml}
      `;
      $('#sheet-content').innerHTML = body;
      const btn = $('#ai-trigger-btn');
      if (btn) btn.addEventListener('click', async () => {
        btn.disabled = true; btn.textContent = '⏳ LLM 调用中…';
        try {
          const r = await fetch('/api/news/flash/' + encodeURIComponent(id) + '/analyze', {method:'POST'});
          const d = await r.json();
          if (r.ok) {
            toast('✅ 解读完成', 'up');
            _state.cache.news = null;
            openNewsDetail(id);
          } else {
            toast('❌ ' + (d.detail || '失败'), 'down');
            btn.disabled = false; btn.textContent = '🤖 重试';
          }
        } catch (e) {
          toast('❌ ' + e.message, 'down');
          btn.disabled = false; btn.textContent = '🤖 重试';
        }
      });
    } catch (e) {
      $('#sheet-content').innerHTML = `<div class="empty">加载失败：${e.message}</div>`;
    }
  }

  function renderAIAnalysis(a) {
    if (typeof a === 'string') {
      try { a = JSON.parse(a); } catch { return `<div>${escape(a)}</div>`; }
    }
    const fields = [];
    if (a.summary) fields.push(`<h4>摘要</h4><div>${escape(a.summary)}</div>`);
    if (a.impact) fields.push(`<h4>影响</h4><div>${escape(a.impact)}</div>`);
    if (a.affected_symbols && a.affected_symbols.length) {
      fields.push(`<h4>受影响标的</h4><div>${a.affected_symbols.map(s=>`<code>${escape(s)}</code>`).join(' · ')}</div>`);
    }
    if (a.action_suggestion) fields.push(`<h4>操作建议</h4><div>${escape(a.action_suggestion)}</div>`);
    if (a.confidence != null) fields.push(`<div class="kv-row"><span class="k">置信度</span><span class="v">${a.confidence}</span></div>`);
    return fields.join('') || `<pre style="white-space:pre-wrap;">${escape(JSON.stringify(a, null, 2))}</pre>`;
  }

  // ═══════════════════════════════════════════════════════════
  // 3. 候选池
  // ═══════════════════════════════════════════════════════════
  async function renderPool() {
    try {
      const data = await loadPool();
      let items = (data.items || []);
      const f = _state.poolFilter;
      if (f === 'active') items = items.filter(p => p.status === 'active');
      else if (f === 'cooling') items = items.filter(p => p.status === 'cooling');
      else if (['us','hk','cn'].includes(f)) items = items.filter(p => p.market === f);
      $('#pool-count').textContent = items.length;
      if (!items.length) {
        $('#pool-list').innerHTML = '<div class="empty"><div class="empty-icon">🎯</div><div>无候选池条目</div></div>';
        return;
      }
      $('#pool-list').innerHTML = items.map(renderPoolRow).join('');
      $$('#pool-list .pool-row').forEach(r => {
        r.addEventListener('click', () => openPoolDetail(r.dataset.id));
      });
    } catch (e) {
      console.error(e);
      $('#pool-list').innerHTML = '<div class="empty">加载失败</div>';
    }
  }

  function renderPoolRow(p) {
    const score = parseFloat(p.score) || 0;
    let scoreCls = 'low';
    if (score >= 70) scoreCls = 'high';
    else if (score >= 50) scoreCls = 'mid';
    const sCls = `s-${p.status || 'archived'}`;
    return `<div class="pool-row ${sCls}" data-id="${p.id}">
      <div class="pool-row-hdr">
        <div class="row-symbol">${escape(p.symbol)} <span class="small muted">${MARKET_LABEL[p.market]||p.market}</span></div>
        <div class="score-badge ${scoreCls}">${score.toFixed(1)}</div>
      </div>
      <div class="row-meta">
        ${POOL_STATUS_LABEL[p.status] || p.status} · ${escape((p.reason || '').slice(0, 60))}
      </div>
      ${p.ai_diagnosis ? `<div class="row-reason">🤖 ${escape(extractDiagSummary(p.ai_diagnosis)).slice(0, 100)}</div>` : ''}
    </div>`;
  }
  function extractDiagSummary(diag) {
    if (typeof diag === 'string') {
      try { diag = JSON.parse(diag); } catch { return diag; }
    }
    return diag.summary || diag.rating || diag.verdict || JSON.stringify(diag).slice(0, 80);
  }

  async function openPoolDetail(id) {
    openSheet('候选池详情', '<div class="empty">加载中…</div>');
    try {
      // 用 list 缓存找到本条
      const data = await loadPool();
      const item = (data.items || []).find(x => String(x.id) === String(id));
      if (!item) { $('#sheet-content').innerHTML = '<div class="empty">未找到</div>'; return; }
      let diag = item.ai_diagnosis;
      if (typeof diag === 'string') {
        try { diag = JSON.parse(diag); } catch {}
      }
      const score = parseFloat(item.score) || 0;
      const html = `
        <h4>基本信息</h4>
        <div class="kv-row"><span class="k">代码</span><span class="v">${escape(item.symbol)}</span></div>
        <div class="kv-row"><span class="k">市场</span><span class="v">${MARKET_LABEL[item.market]||item.market}</span></div>
        <div class="kv-row"><span class="k">状态</span><span class="v">${POOL_STATUS_LABEL[item.status]||item.status}</span></div>
        <div class="kv-row"><span class="k">综合分</span><span class="v">${score.toFixed(1)}</span></div>
        <div class="kv-row"><span class="k">入池时间</span><span class="v">${fmtTime(item.added_at)}</span></div>
        <div class="kv-row"><span class="k">入池原因</span><span class="v small">${escape(item.reason || '')}</span></div>
        ${diag ? renderPoolDiagnosis(diag) : '<h4>🤖 AI 诊断</h4><div class="muted">尚无诊断</div>'}
      `;
      $('#sheet-content').innerHTML = html;
    } catch (e) {
      $('#sheet-content').innerHTML = `<div class="empty">加载失败：${e.message}</div>`;
    }
  }
  function renderPoolDiagnosis(d) {
    const out = ['<h4>🤖 AI 诊断</h4>'];
    if (d.rating) out.push(`<div class="kv-row"><span class="k">评级</span><span class="v">${escape(d.rating)}</span></div>`);
    if (d.summary) out.push(`<div style="margin:6px 0;">${escape(d.summary)}</div>`);
    if (d.bull_points && d.bull_points.length) {
      out.push('<h4>多头逻辑</h4><ul>' + d.bull_points.map(x => `<li>${escape(x)}</li>`).join('') + '</ul>');
    }
    if (d.bear_points && d.bear_points.length) {
      out.push('<h4>空头风险</h4><ul>' + d.bear_points.map(x => `<li>${escape(x)}</li>`).join('') + '</ul>');
    }
    if (d.entry_zone) out.push(`<div class="kv-row"><span class="k">建议入场区</span><span class="v">${escape(String(d.entry_zone))}</span></div>`);
    if (d.target) out.push(`<div class="kv-row"><span class="k">目标价</span><span class="v">${escape(String(d.target))}</span></div>`);
    if (d.stop) out.push(`<div class="kv-row"><span class="k">止损位</span><span class="v">${escape(String(d.stop))}</span></div>`);
    return out.join('');
  }

  // ═══════════════════════════════════════════════════════════
  // 4. 策略信号
  // ═══════════════════════════════════════════════════════════
  async function renderSignals() {
    try {
      const data = await loadSignals();
      let items = data.items || [];
      const f = _state.signalFilter;
      if (f && f !== 'all') {
        if (f === '') items = items.filter(s => !s.ai_verdict);
        else items = items.filter(s => s.ai_verdict === f);
      }
      if (!items.length) {
        $('#signals-list').innerHTML = '<div class="empty"><div class="empty-icon">📡</div><div>无信号</div></div>';
        return;
      }
      $('#signals-list').innerHTML = items.slice(0, 100).map(renderSignalRow).join('');
      $$('#signals-list .row').forEach(r => {
        r.addEventListener('click', () => openSignalDetail(r.dataset.id));
      });
    } catch (e) {
      console.error(e);
      $('#signals-list').innerHTML = '<div class="empty">加载失败</div>';
    }
  }

  function renderSignalRow(s) {
    const v = s.ai_verdict || '';
    const icon = VERDICT_LABEL[v] || '⏳';
    const cls = VERDICT_CLASS[v] || 'accent';
    const conf = s.ai_confidence || 0;
    const dirCn = s.action === 'buy' ? '买入' : '卖出';
    const dirCls = s.action === 'buy' ? 'up' : 'down';
    return `<div class="row ${cls}" data-id="${escape(s.id)}" style="cursor:pointer;">
      <div class="row-title">
        <div class="row-symbol">
          ${icon} ${escape(s.symbol)}
          <span class="small ${dirCls}">${dirCn}</span>
          <span class="small muted">${MARKET_LABEL[s.market]||s.market}</span>
        </div>
        <div class="row-time">${fmtTime(s.generated_at)}</div>
      </div>
      <div class="row-meta">
        ${escape(STRATEGY_NAME_CN[s.strategy_name] || s.strategy_name)} · 系统 ${s.confidence||0} / AI ${conf} · ${s.interval||''}
      </div>
      ${s.ai_summary ? `<div class="row-reason">${escape(s.ai_summary).slice(0,140)}</div>`
                     : (s.ai_reason ? `<div class="row-reason">${escape(s.ai_reason).slice(0,140)}</div>` : '')}
    </div>`;
  }

  async function openSignalDetail(id) {
    openSheet('信号详情', '<div class="empty">加载中…</div>');
    try {
      const s = await fetchJSON('/api/signals/' + encodeURIComponent(id));
      const v = s.ai_verdict || '';
      const verdictTxt = VERDICT_LABEL[v] || '⏳ 验证中';
      const trig = s.triggered_by || {};
      const trigKeys = Object.keys(trig);
      const news = s.related_news || [];
      const pool = s.pool_context;
      const html = `
        <h4>基本</h4>
        <div class="kv-row"><span class="k">代码 / 方向</span><span class="v">${escape(s.symbol)} · ${s.action}</span></div>
        <div class="kv-row"><span class="k">市场 / 周期</span><span class="v">${MARKET_LABEL[s.market]||s.market} · ${escape(s.interval||'')}</span></div>
        <div class="kv-row"><span class="k">策略</span><span class="v">${escape(STRATEGY_NAME_CN[s.strategy_name] || s.strategy_name || '')}</span></div>
        <div class="kv-row"><span class="k">系统置信度</span><span class="v">${s.confidence||0}</span></div>
        <div class="kv-row"><span class="k">价位</span><span class="v">${s.price||'—'}</span></div>
        <div class="kv-row"><span class="k">生成时间</span><span class="v">${fmtTime(s.generated_at)}</span></div>
        <h4>🤖 AI 验证</h4>
        <div class="kv-row"><span class="k">判定</span><span class="v">${verdictTxt}</span></div>
        <div class="kv-row"><span class="k">置信度</span><span class="v">${s.ai_confidence||0}</span></div>
        ${s.ai_summary ? `<div style="margin:6px 0;">${escape(s.ai_summary)}</div>` : ''}
        ${s.ai_reason ? `<h4>验证理由</h4><div>${escape(s.ai_reason)}</div>` : ''}
        ${trigKeys.length ? `<h4>触发条件</h4><pre style="white-space:pre-wrap;font-size:12px;">${escape(JSON.stringify(trig, null, 2))}</pre>` : ''}
        ${news.length ? `<h4>关联新闻 (${news.length})</h4>` + news.map(n => `
          <div class="kv-row"><span class="k">${stars(n.importance||1)}</span><span class="v small">${escape((n.title||'').slice(0,80))}</span></div>
        `).join('') : ''}
        ${pool ? renderPoolContextInSignal(pool) : ''}
      `;
      $('#sheet-content').innerHTML = html;
    } catch (e) {
      $('#sheet-content').innerHTML = `<div class="empty">加载失败：${e.message}</div>`;
    }
  }
  function renderPoolContextInSignal(p) {
    let d = p.diagnosis;
    if (typeof d === 'string') { try { d = JSON.parse(d); } catch {} }
    return `<h4>🎯 候选池上下文</h4>
      <div class="kv-row"><span class="k">综合分</span><span class="v">${(p.pool_score||0).toFixed(1)}</span></div>
      ${d && d.rating ? `<div class="kv-row"><span class="k">AI 评级</span><span class="v">${escape(d.rating)}</span></div>` : ''}
      ${d && d.summary ? `<div style="margin:6px 0;font-size:12px;">${escape(d.summary).slice(0,200)}</div>` : ''}`;
  }

  // ═══════════════════════════════════════════════════════════
  // 5. 持仓管理
  // ═══════════════════════════════════════════════════════════
  // v12.20.3: 加载 swap 持仓 (合约模式独立)
  async function loadSwapAccount() {
    try {
      const r = await fetchJSON('/api/swap/account');
      return r;
    } catch (e) { return null; }
  }
  async function loadSwapPositions() {
    try {
      const r = await fetchJSON('/api/swap/positions?status=open');
      return r.items || [];
    } catch (e) { return []; }
  }
  async function loadSwapOrders(status) {
    try {
      const r = await fetchJSON('/api/swap/orders' + (status ? '?status=' + status : ''));
      return r.items || [];
    } catch (e) { return []; }
  }

  // v12.19.0: 持仓页 = 顶部汇总条 + 按市场分组 + 各持仓 row
  // v12.20.3: 若 swap_mock 模式 → 同时显示 swap_positions
  // v12.21.0 PR1: 仅在新 trade/spot 路由展示**现货**, swap 由 trade/swap 独立 dashboard 负责
  //   不再在持仓 sub 重复渲染 swap (避免与 trade/swap 重复)
  async function renderPositions() {
    try {
      const [items, advices] = await Promise.all([
        loadPositions(), loadAdvices(),
      ]);
      const swapMode = false;  // v12.21.0: 持仓 sub 永远不显示 swap (独立到 trade/swap)
      const swapAcct = null;
      const swapPositions = [];
      // ─ 顶部汇总条 ─
      let totalUSD = 0, pnlUSD = 0, winN = 0, loseN = 0;
      for (const p of items) {
        const pnlPct = p.pnl_pct || 0;
        if (pnlPct >= 0) winN++; else loseN++;
        totalUSD += (p.market_value_usd || 0);
        pnlUSD += (p.pnl_usd || 0);
      }
      const summaryCls = pnlUSD >= 0 ? 'up' : 'down';
      // v12.20.3: swap 汇总（如果启用）
      let swapSummaryHtml = '';
      if (swapMode) {
        const sBalance = swapAcct.balance_usd || 0;
        const sMargin = swapAcct.total_margin_usd || 0;
        const sPnl = swapAcct.total_pnl_usd || 0;
        const sCls = sPnl >= 0 ? 'up' : 'down';
        let sUpnl = 0;
        for (const p of swapPositions) sUpnl += (p.unrealized_pnl_usd || 0);
        const upnlCls = sUpnl >= 0 ? 'up' : 'down';
        swapSummaryHtml = `
          <div class="card section" style="background:linear-gradient(135deg,#1c2128,#1a1f26);">
            <div class="section-title">⚡ 加密合约 (swap mock) — ${swapPositions.length} 持仓</div>
            <div class="summary-row"><span class="summary-k">余额</span><span class="summary-v">${fmtMoney(sBalance)}</span></div>
            <div class="summary-row"><span class="summary-k">占用保证金</span><span class="summary-v">${fmtMoney(sMargin)}</span></div>
            <div class="summary-row"><span class="summary-k">浮动 PnL</span><span class="summary-v ${upnlCls}">${fmtPnl(sUpnl)}</span></div>
            <div class="summary-row"><span class="summary-k">累计已实现 PnL</span><span class="summary-v ${sCls}">${fmtPnl(sPnl)}</span></div>
          </div>`;
      }
      $('#positions-summary').innerHTML = (items.length ? `
        <div class="summary-row">
          <span class="summary-k">现货持仓 ${items.length} 笔</span>
          <span class="summary-k">浮盈 ${winN} / 浮亏 ${loseN}</span>
        </div>
        <div class="summary-row">
          <span class="summary-k">市值</span>
          <span class="summary-v">${fmtMoney(totalUSD)}</span>
        </div>
        <div class="summary-row">
          <span class="summary-k">浮动盈亏</span>
          <span class="summary-v ${summaryCls}">${fmtPnl(pnlUSD)}</span>
        </div>
      ` : '') + swapSummaryHtml;

      if (!items.length && !swapPositions.length) {
        $('#positions-list').innerHTML = `<div class="empty">
          <div class="empty-icon">📭</div>
          <div>暂无持仓</div>
          <div class="small" style="margin-top:6px;">等系统自动开仓</div>
        </div>`;
        return;
      }
      const adviceMap = {};
      (advices || []).forEach(a => { if (a && a.position_id) adviceMap[a.position_id] = a; });
      // ─ 按市场分组 ─
      const groups = {us:[], hk:[], cn:[], crypto:[]};
      for (const p of items) {
        const m = p.market || 'crypto';
        if (groups[m]) groups[m].push(p);
        else groups[m] = [p];
      }
      const mktOrder = ['crypto','us','hk','cn'];
      const groupHtml = mktOrder.filter(m => groups[m] && groups[m].length).map(m => {
        const g = groups[m];
        const subPnl = g.reduce((s,p) => s + (p.pnl_pct || 0), 0) / g.length;
        return `<div class="pos-group">
          <div class="pos-group-hdr">
            <span class="pos-group-name">${MARKET_LABEL[m] || m}</span>
            <span class="pos-group-meta">${g.length} 笔 · 均 ${fmtPct(subPnl)}</span>
          </div>
          ${g.map(p => renderPositionRow(p, adviceMap[p.id])).join('')}
        </div>`;
      }).join('');
      // v12.20.3: swap 持仓单独分组显示 (与现货分开)
      let swapGroupHtml = '';
      if (swapMode && swapPositions.length) {
        const sumUpnl = swapPositions.reduce((s,p)=>s+(p.unrealized_pnl_usd||0),0);
        swapGroupHtml = `<div class="pos-group">
          <div class="pos-group-hdr">
            <span class="pos-group-name">⚡ 加密合约</span>
            <span class="pos-group-meta">${swapPositions.length} 笔 · 浮盈 ${fmtPnl(sumUpnl)}</span>
          </div>
          ${swapPositions.map(renderSwapPositionRow).join('')}
        </div>`;
      }
      $('#positions-list').innerHTML = groupHtml + swapGroupHtml;
      // v12.20.5 Bug 2: 仅给现货 row (有 data-id) 绑 click; swap row (data-swap-id) 不可点
      $$('#positions-list .row[data-id]').forEach(r => {
        r.addEventListener('click', () => openPositionDetail(r.dataset.id));
      });
    } catch (e) {
      console.error(e);
      $('#positions-list').innerHTML = '<div class="empty">加载失败</div>';
    }
  }

  // v12.20.3: swap 持仓行 (含杠杆/强平/funding)
  function renderSwapPositionRow(p) {
    const pnl = p.unrealized_pnl_usd || 0;
    const cls = pnl >= 0 ? 'up' : 'down';
    const sideCn = p.pos_side === 'long' ? '多 🟢' : '空 🔴';
    const dispSym = (p.symbol || '').replace('-SWAP', '');
    const liqDist = p.liq_price && p.avg_open_price
      ? ((p.pos_side === 'long' ? (1 - p.liq_price/p.avg_open_price) : (p.liq_price/p.avg_open_price - 1)) * 100).toFixed(1)
      : '?';
    return `<div class="row ${cls}" data-swap-id="${escape(p.id)}" style="cursor:default;">
      <div class="row-title">
        <div class="row-symbol">${escape(dispSym)} <span class="small muted">${sideCn} · ${p.leverage}x</span></div>
        <div class="row-pnl ${cls}">${fmtPnl(pnl)}</div>
      </div>
      <div class="row-meta">
        ${(p.qty||0).toFixed(4)} 张 @ ${(p.avg_open_price||0).toFixed(4)} · 保证金 ${fmtMoney(p.margin_usd||0)}
      </div>
      <div class="row-meta small">
        🛑 强平 ${(p.liq_price||0).toFixed(4)} (距 ${liqDist}%) ${p.pre_liq_armed ? '⚠️ 已减仓' : ''}
      </div>
      <div class="row-meta small">
        💰 funding ${fmtPnl(p.funding_fee_total_usd||0)} · 手续费 -${fmtMoney(p.total_fee_usd||0)}
      </div>
    </div>`;
  }

  // v12.19.0: 拒单子页 — 显示自动交易日志中 status='rejected' 的记录
  async function renderRejected() {
    try {
      const [log] = await Promise.all([loadHistory()]);
      const allRejected = (log.items || []).filter(t => t.status === 'rejected');
      const filterMkt = _state.rejectedFilter;
      const items = filterMkt === 'all'
        ? allRejected
        : allRejected.filter(t => t.market === filterMkt);

      if (!items.length) {
        $('#rejected-list').innerHTML = `<div class="empty">
          <div class="empty-icon">⊘</div>
          <div>无拒单记录</div>
          <div class="small" style="margin-top:6px;">${filterMkt === 'all' ? '系统未拒过单' : '该市场无拒单'}</div>
        </div>`;
        return;
      }
      $('#rejected-list').innerHTML = items.slice(0, 80).map(t => {
        const action = ACTION_LABEL[t.action] || t.action;
        const reason = t.rejected_reason || t.reason || '';
        return `<div class="row warn">
          <div class="row-title">
            <div class="row-symbol">${escape(t.symbol)} <span class="small muted">${MARKET_LABEL[t.market]||t.market}</span></div>
            <div class="row-time">${fmtRelTime(t.traded_at*1000)}</div>
          </div>
          <div class="row-meta small">尝试 ${action}</div>
          <div class="row-reason">⊘ ${escape(reason)}</div>
        </div>`;
      }).join('');
    } catch (e) {
      console.error(e);
      $('#rejected-list').innerHTML = '<div class="empty">加载失败</div>';
    }
  }

  function renderPositionRow(p, advice) {
    const avg = p.avg_cost || 0;
    const cur = p.current_price || avg;
    const qty = p.quantity || 0;
    const side = p.side || 'long';
    const sideCn = side === 'long' ? '多' : '空';
    const ccy = p.cost_currency || 'USD';
    const pnlPct = p.pnl_pct || 0;
    const pnlLocal = p.pnl_local || 0;
    const cls = pnlPct >= 0 ? 'up' : 'down';
    const sl = p.stop_loss, tp = p.take_profit;
    const adviceCn = advice && advice.advice ? (ADVICE_LABEL_CN[advice.advice] || advice.advice) : '';
    const adviceTxt = advice && advice.advice
      ? `<div class="row-reason">🤖 ${escape(adviceCn)}${advice.reason ? ' · ' + escape(String(advice.reason).slice(0,80)) : ''}</div>`
      : '';
    // v12.16.2 (#3): 入场策略显示
    let strategyTxt = '';
    if (p.entry_strategy) {
      if (p.entry_strategy === 'resonance' && p.entry_strategies) {
        const lvl = p.entry_resonance_level || '?';
        const boost = p.entry_sizing_boost || 1.0;
        const namesCn = p.entry_strategies.map(s => STRATEGY_NAME_CN[s] || s).join(' + ');
        strategyTxt = `<div class="row-meta small" style="color:var(--accent);">📡 共振 L${lvl} (×${boost}): ${escape(namesCn)}</div>`;
      } else {
        const cn = STRATEGY_NAME_CN[p.entry_strategy] || p.entry_strategy;
        strategyTxt = `<div class="row-meta small">📡 策略：${escape(cn)}</div>`;
      }
    }
    return `<div class="row ${cls}" data-id="${escape(p.id)}" style="cursor:pointer;">
      <div class="row-title">
        <div class="row-symbol">${escape(p.symbol)} <span class="small muted">${MARKET_LABEL[p.market]||p.market} · ${sideCn}</span></div>
        <div class="row-pnl ${cls}">${fmtPct(pnlPct)}</div>
      </div>
      <div class="row-meta">
        ${qty.toLocaleString()} @ ${fmtMoney(avg, ccy)} → ${fmtMoney(cur, ccy)} · ${fmtPnl(pnlLocal, ccy)}
      </div>
      ${strategyTxt}
      ${(sl || tp) ? `<div class="row-meta small">
        ${sl ? `🛑 SL ${fmtMoney(sl, ccy)}` : ''} ${tp ? `🎯 TP ${fmtMoney(tp, ccy)}` : ''}
      </div>` : ''}
      ${adviceTxt}
    </div>`;
  }

  async function openPositionDetail(id) {
    openSheet('持仓详情', '<div class="empty">加载中…</div>');
    try {
      const [items, advices] = await Promise.all([loadPositions(), loadAdvices()]);
      const p = items.find(x => String(x.id) === String(id));
      if (!p) { $('#sheet-content').innerHTML = '<div class="empty">未找到</div>'; return; }
      const advice = (advices || []).find(a => String(a.position_id) === String(id));
      const ccy = p.cost_currency || 'USD';
      const html = `
        <h4>基本信息</h4>
        <div class="kv-row"><span class="k">代码</span><span class="v">${escape(p.symbol)}</span></div>
        <div class="kv-row"><span class="k">市场 / 方向</span><span class="v">${MARKET_LABEL[p.market]||p.market} · ${p.side==='long'?'多':'空'}</span></div>
        <div class="kv-row"><span class="k">数量</span><span class="v">${(p.quantity||0).toLocaleString()}</span></div>
        <div class="kv-row"><span class="k">均价</span><span class="v">${fmtMoney(p.avg_cost, ccy)}</span></div>
        <div class="kv-row"><span class="k">现价</span><span class="v">${fmtMoney(p.current_price, ccy)}</span></div>
        <div class="kv-row"><span class="k">浮盈</span><span class="v ${(p.pnl_pct||0)>=0?'up':'down'}">${fmtPnl(p.pnl_local, ccy)} (${fmtPct(p.pnl_pct)})</span></div>
        <div class="kv-row"><span class="k">市值 USD</span><span class="v">${fmtMoney(p.market_value_usd)}</span></div>
        <h4>风控</h4>
        <div class="kv-row"><span class="k">止损 SL</span><span class="v">${p.stop_loss ? fmtMoney(p.stop_loss, ccy) : '—'}</span></div>
        <div class="kv-row"><span class="k">止盈 TP</span><span class="v">${p.take_profit ? fmtMoney(p.take_profit, ccy) : '—'}</span></div>
        <div class="kv-row"><span class="k">开仓时间</span><span class="v">${fmtTime(p.opened_at)}</span></div>
        <h4>🤖 最新 AI 建议</h4>
        ${advice && advice.advice
          ? `<div class="kv-row"><span class="k">建议</span><span class="v">${escape(ADVICE_LABEL_CN[advice.advice] || advice.advice)}</span></div>
             ${advice.reason ? `<div style="margin:4px 0;">${escape(advice.reason)}</div>` : ''}
             <div class="kv-row"><span class="k">时间</span><span class="v small">${fmtTime(advice.advised_at)}</span></div>`
          : '<div class="muted">暂无建议</div>'}
        <div class="sheet-actions">
          <button class="btn" id="advise-now-btn">🤖 重新分析</button>
        </div>
      `;
      $('#sheet-content').innerHTML = html;
      const btn = $('#advise-now-btn');
      if (btn) btn.addEventListener('click', async () => {
        btn.disabled = true; btn.textContent = '⏳ 调用中…';
        try {
          const r = await fetch('/api/positions/' + encodeURIComponent(id) + '/advise', {method:'POST'});
          if (r.ok) {
            toast('✅ 已生成新建议', 'up');
            _state.cache.advices = null;
            openPositionDetail(id);
          } else {
            const d = await r.json().catch(()=>({}));
            toast('❌ ' + (d.detail || '失败'), 'down');
            btn.disabled = false; btn.textContent = '🤖 重试';
          }
        } catch (e) {
          toast('❌ ' + e.message, 'down');
          btn.disabled = false; btn.textContent = '🤖 重试';
        }
      });
    } catch (e) {
      $('#sheet-content').innerHTML = `<div class="empty">加载失败：${e.message}</div>`;
    }
  }

  // ═══════════════════════════════════════════════════════════
  // 6. 复盘学习（含历史成交、教训库）
  // ═══════════════════════════════════════════════════════════
  // v12.19.0: renderReview 已废弃 — 拆为 positions/reviews(renderReviewList) + learn/lessons(renderLessons) + learn/rules(renderRulesOnly)

  async function renderReviewList() {
    try {
      const data = await loadReviews();
      const items = data.items || [];
      if (!items.length) {
        $('#review-list').innerHTML = '<div class="empty"><div class="empty-icon">🔍</div><div>暂无复盘</div><div class="small" style="margin-top:6px;">闭环交易后台自动生成</div></div>';
        return;
      }
      $('#review-list').innerHTML = items.map(renderReviewRow).join('');
      $$('#review-list .row').forEach(r => {
        r.addEventListener('click', () => openReviewDetail(r.dataset.pid));
      });
    } catch (e) {
      console.error(e);
      $('#review-list').innerHTML = '<div class="empty">加载失败</div>';
    }
  }

  function renderReviewRow(r) {
    const grade = (r.grade || '').toUpperCase();
    const pnl = r.realized_pnl_usd || 0;
    const pnlPct = r.realized_pnl_pct || 0;
    const cls = pnl >= 0 ? 'up' : 'down';
    const lessonsArr = Array.isArray(r.lessons) ? r.lessons : [];
    // v12.20.9: swap 复盘加 ⚡ 徽章 + 杠杆/funding/强平标记
    const isSwap = r.is_swap == 1 || r.is_swap === true;
    let swapBadges = '';
    if (isSwap) {
      const sideTxt = r.swap_pos_side === 'long' ? '🟢多' : (r.swap_pos_side === 'short' ? '🔴空' : '');
      const lev = r.swap_leverage ? `${r.swap_leverage}x` : '';
      const liqBadge = r.swap_liquidated ? '<span style="background:rgba(248,81,73,0.18);color:var(--color-down);padding:1px 5px;border-radius:6px;font-size:10px;">💀强平</span>' : '';
      swapBadges = `<span style="background:rgba(188,140,255,0.18);color:var(--purple);padding:1px 6px;border-radius:8px;font-size:10px;">⚡合约 ${sideTxt} ${lev}</span> ${liqBadge}`;
    }
    return `<div class="row ${cls}" data-pid="${escape(r.position_id)}" style="cursor:pointer;">
      <div class="row-title">
        <div class="row-symbol">
          <span class="grade-pill ${grade}">${grade || '?'}</span>
          ${escape(r.symbol)}
          <span class="small muted">${MARKET_LABEL[r.market]||r.market}</span>
          ${swapBadges}
        </div>
        <div class="row-pnl ${cls}">${fmtPnl(pnl)} (${fmtPct(pnlPct)})</div>
      </div>
      <div class="row-meta">
        持仓 ${Math.round((r.holding_seconds||0)/3600)} h · ${fmtTime(r.close_at)}
        ${isSwap && r.swap_funding_total ? ' · funding ' + fmtPnl(r.swap_funding_total) : ''}
      </div>
      ${lessonsArr.length ? `<div class="row-reason">📌 ${escape(lessonsArr.slice(0,2).join(' / '))}</div>` : ''}
    </div>`;
  }

  async function openReviewDetail(pid) {
    openSheet('复盘详情', '<div class="empty">加载中…</div>');
    try {
      const r = await fetchJSON('/api/trade-review/' + encodeURIComponent(pid));
      const grade = (r.grade || '').toUpperCase();
      const pnl = r.realized_pnl_usd || 0;
      const pros = Array.isArray(r.pros) ? r.pros : [];
      const cons = Array.isArray(r.cons) ? r.cons : [];
      const lessons = Array.isArray(r.lessons) ? r.lessons : [];
      const turning = Array.isArray(r.turning_points) ? r.turning_points : [];
      const html = `
        <h4>结果</h4>
        <div class="kv-row"><span class="k">代码</span><span class="v">${escape(r.symbol)}</span></div>
        <div class="kv-row"><span class="k">评级</span><span class="v"><span class="grade-pill ${grade}">${grade||'?'}</span> ${GRADE_LABEL[grade]||''}</span></div>
        <div class="kv-row"><span class="k">实现盈亏</span><span class="v ${pnl>=0?'up':'down'}">${fmtPnl(pnl)} (${fmtPct(r.realized_pnl_pct||0)})</span></div>
        <div class="kv-row"><span class="k">持仓时长</span><span class="v">${Math.round((r.holding_seconds||0)/3600)} h</span></div>
        <div class="kv-row"><span class="k">闭环时间</span><span class="v">${fmtTime(r.close_at)}</span></div>
        ${r.summary ? `<h4>总结</h4><div>${escape(r.summary)}</div>` : ''}
        ${pros.length ? `<h4>👍 做对了什么</h4><ul>${pros.map(x=>`<li>${escape(x)}</li>`).join('')}</ul>` : ''}
        ${cons.length ? `<h4>👎 做错了什么</h4><ul>${cons.map(x=>`<li>${escape(x)}</li>`).join('')}</ul>` : ''}
        ${turning.length ? `<h4>关键转折点</h4><ul>${turning.map(x=>{
            const t = typeof x === 'object' ? `${escape(x.time||'')} ${escape(x.event||x.note||'')}` : escape(x);
            return `<li>${t}</li>`;
          }).join('')}</ul>` : ''}
        ${lessons.length ? `<h4>📌 教训</h4><ul>${lessons.map(x=>`<li>${escape(x)}</li>`).join('')}</ul>` : ''}
        ${renderStrategyParamAnalysis(r.strategy_param_analysis)}
      `;
      $('#sheet-content').innerHTML = html;
    } catch (e) {
      $('#sheet-content').innerHTML = `<div class="empty">加载失败：${e.message}</div>`;
    }
  }

  // v12.16.2 (#1): 渲染策略参数分析段
  function renderStrategyParamAnalysis(data) {
    if (!data || typeof data !== 'object') return '';
    let strategies = data.strategies;
    if (typeof data === 'string') {
      try { data = JSON.parse(data); strategies = data.strategies; } catch { return ''; }
    }
    if (!Array.isArray(strategies) || !strategies.length) return '';
    const conclusion = data.overall_conclusion || '';
    let html = '<h4>🎯 策略参数分析</h4>';
    if (conclusion) html += `<div style="margin-bottom:8px;color:var(--text-2);">${escape(conclusion)}</div>`;
    for (const s of strategies) {
      const stars = '★'.repeat(s.evaluation || 0) + '☆'.repeat(5 - (s.evaluation || 0));
      const cur = JSON.stringify(s.current_params || {}, null, 0).slice(0, 80);
      const sug = s.suggested_params ? JSON.stringify(s.suggested_params).slice(0, 80) : null;
      html += `<div style="background:var(--bg-card-2);padding:8px 10px;border-radius:6px;margin-bottom:6px;">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <span style="font-weight:600;">${escape(s.name || '?')}</span>
          <span class="warn">${stars}</span>
        </div>
        <div class="row-meta small" style="margin-top:3px;">当前：${escape(cur)}</div>
        ${s.reason ? `<div class="row-reason" style="margin-top:4px;">${escape(s.reason)}</div>` : ''}
        ${sug ? `<div class="row-meta small" style="margin-top:4px;color:var(--accent);">💡 建议：${escape(sug)}${s.expected_improvement ? ` — ${escape(s.expected_improvement)}` : ''}</div>` : ''}
      </div>`;
    }
    return html;
  }

  // v12.19.1 (P2-B): renderHistory 死代码已删除（5-tab 重构后没人调用）
  // 最近成交在主页 "最近成交" section 显示，使用同样的 renderTradeRow 渲染

  function renderTradeRow(t) {
    const icon = ACTION_ICON[t.action] || '•';
    const status = t.status === 'executed' ? '✅' : '❌';
    const isExec = t.status === 'executed';
    let cls = '';
    let pnlHtml = '';
    if (t.trigger_detail && t.trigger_detail.realized_pnl_usd != null) {
      const pnl = t.trigger_detail.realized_pnl_usd;
      const pct = t.trigger_detail.realized_pnl_pct || 0;
      cls = pnl >= 0 ? 'up' : 'down';
      pnlHtml = `<div class="row-pnl ${cls}">${fmtPnl(pnl)} (${fmtPct(pct)})</div>`;
    } else if (t.action === 'open' || t.action === 'add') cls = 'accent';
    else if (!isExec) cls = 'muted';
    return `<div class="row ${cls}">
      <div class="row-title">
        <div class="row-symbol">
          ${status} ${icon} ${escape(t.symbol)}
          <span class="small muted">${MARKET_LABEL[t.market]||t.market} · ${ACTION_LABEL[t.action]||t.action}</span>
        </div>
        <div class="row-time">${fmtTime(t.traded_at)}</div>
      </div>
      <div class="row-meta">
        ${isExec
          ? `${(t.quantity||0).toLocaleString()} @ ${(t.price||0).toFixed(4)} = $${(t.amount_usd||0).toFixed(2)}`
          : `<span class="muted">${escape(t.rejected_reason||'').slice(0,80)}</span>`}
      </div>
      ${pnlHtml}
    </div>`;
  }

  async function renderLessons() {
    try {
      const data = await loadLessons();
      const items = data.items || [];
      // 显示顶部 actions（仅在教训页）
      $('#review-actions').style.display = 'flex';
      const actCnt = items.filter(l => l.status === 'active').length;
      const adoptCnt = items.filter(l => l.status === 'adopted').length;
      $('#lesson-stats').textContent = `${actCnt} 活跃 · ${adoptCnt} 已采纳`;
      if (!items.length) {
        $('#lessons-list').innerHTML = '<div class="empty"><div class="empty-icon">📌</div><div>暂无教训</div><div class="small" style="margin-top:6px;">闭环交易越多，教训库越丰富</div></div>';
        return;
      }
      $('#lessons-list').innerHTML = items.map(renderLessonCard).join('');
      $$('#lessons-list .lesson-adopt-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          openLessonAdopt(btn.dataset.id);
        });
      });
    } catch (e) {
      console.error(e);
      $('#lessons-list').innerHTML = '<div class="empty">加载失败</div>';
    }
  }

  // v12.19.0: 风控规则单独子页 (从 renderSettings 抽出)
  async function renderRulesOnly() {
    try {
      const rules = await loadRiskRules();
      const list = (rules.items || rules || []);
      if (!Array.isArray(list) || !list.length) {
        $('#rules-list').innerHTML = `<div class="empty">
          <div class="empty-icon">🛡️</div>
          <div>暂无风控规则</div>
          <div class="small" style="margin-top:6px;">教训采纳后会自动生成</div>
        </div>`;
        return;
      }
      $('#rules-list').innerHTML = list.map(renderRuleCard).join('');
      // 绑定 toggle 切换
      $$('#rules-list .rule-toggle input').forEach(inp => {
        inp.addEventListener('change', async (e) => {
          const id = inp.dataset.id;
          const enabled = e.target.checked;
          try {
            const r = await fetch('/api/risk-rules/' + id + '/toggle', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({enabled}),
            });
            if (!r.ok) throw new Error('HTTP ' + r.status);
            toast(enabled ? '✅ 规则已启用' : '🔴 规则已禁用', 'up');
            _state.cache.riskRules = null;
          } catch (e) {
            toast('❌ ' + e.message, 'down');
            inp.checked = !enabled;
          }
        });
      });
    } catch (e) {
      console.error(e);
      $('#rules-list').innerHTML = '<div class="empty">加载失败</div>';
    }
  }

  // v12.19.0: 共振组合子页
  async function renderCombos() {
    try {
      // 21 个 GOLDEN_COMBOS（与后端 monitor.py 同步）
      const COMBOS = [
        { name: '趋势 + 量能起爆', cn_strategies: ['均线金叉死叉','成交量突破','MACD 金叉死叉'], market: '全市场' },
        { name: '反转底部确认', cn_strategies: ['布林带均值回归','成交量突破','EMA 三线排列'], market: '全市场' },
        { name: '突破共振', cn_strategies: ['唐奇安通道突破','布林挤压突破','ADX 趋势跟随'], market: '全市场' },
        { name: '加密 Smart Money', cn_strategies: ['资金费率极值','OI 持仓突破','多空比反转'], market: '加密' },
        { name: '加密极值反转', cn_strategies: ['F&G 极值反转','资金费率极值'], market: '加密' },
        { name: '缠论 + 量能确认', cn_strategies: ['缠论买卖点','成交量突破'], market: '全市场' },
        { name: 'A 股政策资金', cn_strategies: ['北向资金排名','板块联动','涨停后回踩'], market: 'A 股' },
        { name: 'RSI 趋势回踩共振', cn_strategies: ['均线金叉死叉','RSI 趋势回踩','成交量突破'], market: '全市场' },
        { name: 'RSI 真背离反转', cn_strategies: ['RSI 真背离','成交量突破'], market: '全市场' },
        { name: '美股财报后高开', cn_strategies: ['高开延续','均线金叉死叉','成交量突破'], market: '美股' },
        { name: '美股高开 + MACD 动量', cn_strategies: ['高开延续','MACD 金叉死叉'], market: '美股' },
        { name: '港股南向资金共振', cn_strategies: ['港股通南向','均线金叉死叉','成交量突破'], market: '港股' },
        { name: '新闻驱动 + 量能确认', cn_strategies: ['新闻事件驱动','成交量突破','均线金叉死叉'], market: '全市场' },
        { name: '美股盘前突破 + 趋势', cn_strategies: ['盘前/开盘突破','均线金叉死叉','成交量突破'], market: '美股' },
        { name: '美股相对强势 + 三重过滤', cn_strategies: ['相对大盘强势','三重过滤'], market: '美股' },
        { name: 'A 股龙虎榜 + 板块', cn_strategies: ['龙虎榜跟盘','板块联动'], market: 'A 股' },
        { name: 'A 股融资 + 北向 + 板块', cn_strategies: ['融资余额突破','北向资金排名','板块联动'], market: 'A 股' },
        { name: '加密巨鲸 + 量能 + 趋势', cn_strategies: ['链上巨鲸大单','成交量突破','均线金叉死叉'], market: '加密' },
        { name: '加密稳定币 + F&G 共振', cn_strategies: ['稳定币流入','F&G 极值反转'], market: '加密' },
        { name: '量价 + RSI 双背离', cn_strategies: ['量价背离','RSI 真背离'], market: '全市场' },
        { name: '三重过滤 + RSI 回踩', cn_strategies: ['三重过滤','RSI 趋势回踩'], market: '全市场' },
      ];
      $('#combos-list').innerHTML = `
        <div class="card section">
          <div class="section-title">🌟 黄金共振组合（${COMBOS.length} 个）</div>
          <div class="muted small" style="margin-bottom:10px;">命中即 conf=100，仓位 ×1.5</div>
        </div>
        ${COMBOS.map(c => `<div class="combo-card">
          <div class="combo-name">${escape(c.name)}</div>
          <div class="combo-meta">${c.market}</div>
          <div class="combo-strats">${c.cn_strategies.map(s => `<span class="combo-chip">${escape(s)}</span>`).join('')}</div>
        </div>`).join('')}
      `;
    } catch (e) {
      console.error(e);
      $('#combos-list').innerHTML = '<div class="empty">加载失败</div>';
    }
  }

  // 切到非教训 chip 时隐藏 actions
  function hideLessonActions() { $('#review-actions').style.display = 'none'; }

  // LLM 合并按钮
  $('#lesson-merge-btn').addEventListener('click', async () => {
    const btn = $('#lesson-merge-btn');
    if (!confirm('调用 LLM 把语义相同的教训合并？耗时约 30s，预算 ~$0.05')) return;
    btn.disabled = true;
    const oldText = btn.textContent;
    btn.textContent = '⏳ LLM 处理中…';
    try {
      const r = await fetch('/api/trade-review/lessons/merge', {method: 'POST'});
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || '失败');
      const msg = `✅ ${d.before}→${d.after} 条 (合并 ${d.merged_clusters} 类，禁用 ${d.lessons_disabled} 条)`
                + (d.auto_adopted_after_merge > 0 ? `，自动采纳 ${d.auto_adopted_after_merge} 条` : '');
      toast(msg, 'up');
      _state.cache.lessons = null;
      _state.cache.riskRules = null;
      renderLessons();
    } catch (e) {
      toast('❌ ' + e.message, 'down');
    } finally {
      btn.disabled = false;
      btn.textContent = oldText;
    }
  });

  function renderLessonCard(l) {
    const status = l.status || 'active';
    const score = parseFloat(l.adoption_score || 0);
    const hasParams = l.has_specific_params == 1 || l.has_specific_params === true;
    let badge = '';
    if (status === 'adopted') badge = '<span class="adopt-badge adopted">✅ 已采纳</span>';
    else if (score >= 15 && hasParams) badge = '<span class="adopt-badge auto">🔴 立即采纳建议</span>';
    else if (score >= 8 && hasParams) badge = '<span class="adopt-badge sug">🟠 推荐采纳</span>';
    else if (!hasParams) badge = '<span class="adopt-badge prompt">⚪ 仅 prompt 软规则</span>';
    else badge = `<span class="adopt-badge wait">🟡 score ${score.toFixed(1)}</span>`;
    const showAdoptBtn = (status === 'active' && hasParams);
    return `<div class="lesson-card ${status}">
      <div class="lesson-pat">${escape(l.pattern || l.summary || '—')}</div>
      <div class="row-meta">
        ${POOL_STATUS_LABEL[status] || status} · 出现 ${l.occurrences||0} 次 · score ${score.toFixed(1)}
        ${l.pool_id ? ' · ' + escape(l.pool_id) : ''}
        ${l.worst_pnl_pct != null ? ' · 最差 ' + fmtPct(l.worst_pnl_pct) : ''}
      </div>
      <div style="margin-top:6px;display:flex;gap:8px;align-items:center;">
        ${badge}
        ${showAdoptBtn ? `<button class="btn lesson-adopt-btn" data-id="${l.id}" style="margin-left:auto;padding:4px 12px;font-size:12px;">📥 采纳为规则</button>` : ''}
      </div>
    </div>`;
  }

  async function openLessonAdopt(lessonId) {
    openSheet('采纳为风控规则', '<div class="empty">翻译中…</div>');
    try {
      const r = await fetch('/api/trade-review/lessons/' + lessonId + '/translate', {method:'POST'});
      const d = await r.json();
      if (!d.ok) {
        $('#sheet-content').innerHTML = `
          <div class="empty">${escape(d.msg || '无法翻译')}</div>
          <div class="row-reason" style="margin-top:8px;">${escape(d.lesson_text || '')}</div>`;
        return;
      }
      const ruleType = d.rule_type;
      const params = d.params || {};
      const RULE_LABEL = {
        rsi_block: 'RSI 超买拦截',
        drawdown_force_close: '浮亏强平',
        trend_block: '下跌趋势拦截',
        cooldown_override: '冷却时长覆盖',
        prompt_principle: '提示原则',
      };
      let paramsHtml = '';
      Object.entries(params).forEach(([k,v]) => {
        const id = `param-${k}`;
        paramsHtml += `<div class="kv-row" style="display:flex;align-items:center;gap:8px;">
          <span class="k" style="min-width:160px;">${escape(k)}</span>
          <input id="${id}" data-key="${k}" value="${escape(String(v))}"
                 style="flex:1;background:var(--bg-card-2);color:var(--text);padding:6px 10px;border:1px solid var(--border);border-radius:6px;font-size:14px;" />
        </div>`;
      });
      const html = `
        <h4>原始教训</h4>
        <div style="margin:6px 0;">${escape(d.lesson_text || '')}</div>
        <h4>规则类型</h4>
        <div class="kv-row"><span class="k">类型</span><span class="v">${escape(ruleType)} (${RULE_LABEL[ruleType]||''})</span></div>
        <div class="kv-row"><span class="k">作用域</span><span class="v">${escape(d.pool_id || 'all')}</span></div>
        <h4>参数（可调整）</h4>
        ${paramsHtml}
        <div class="sheet-actions">
          <button class="btn btn-secondary" id="adopt-cancel">取消</button>
          <button class="btn" id="adopt-confirm">📥 采纳并启用</button>
        </div>
      `;
      $('#sheet-content').innerHTML = html;
      $('#adopt-cancel').addEventListener('click', closeSheet);
      $('#adopt-confirm').addEventListener('click', async () => {
        const newParams = {};
        $$('#sheet-content input[data-key]').forEach(inp => {
          let v = inp.value.trim();
          if (v !== '' && !isNaN(parseFloat(v))) v = parseFloat(v);
          newParams[inp.dataset.key] = v;
        });
        const btn = $('#adopt-confirm');
        btn.disabled = true; btn.textContent = '⏳ 写入中…';
        try {
          const r = await fetch('/api/trade-review/lessons/' + lessonId + '/adopt', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({rule_type: ruleType, params: newParams}),
          });
          const dd = await r.json();
          if (!r.ok) throw new Error(dd.detail || '失败');
          toast('✅ 已采纳为规则 #' + dd.rule_id, 'up');
          _state.cache.lessons = null;
          closeSheet();
          renderLessons();
        } catch (e) {
          toast('❌ ' + e.message, 'down');
          btn.disabled = false; btn.textContent = '📥 采纳并启用';
        }
      });
    } catch (e) {
      $('#sheet-content').innerHTML = `<div class="empty">加载失败：${e.message}</div>`;
    }
  }

  // 风控规则加载
  async function loadRiskRules() {
    if (_state.cache.riskRules) return _state.cache.riskRules;
    try { _state.cache.riskRules = await fetchJSON('/api/risk-rules'); }
    catch { _state.cache.riskRules = {items: []}; }
    return _state.cache.riskRules;
  }

  async function loadStrategies() {
    if (_state.cache.strategies) return _state.cache.strategies;
    try { _state.cache.strategies = await fetchJSON('/api/strategies'); }
    catch { _state.cache.strategies = []; }
    return _state.cache.strategies;
  }

  // v12.16.2 (#2): 策略库分组定义；v12.17.0 加 9 个新策略分类
  const STRATEGY_GROUPS = [
    { name: '🌐 通用型 (全市场)', strategies: ['ma_cross','donchian_breakout','bollinger_reversion','volume_breakout','flash_event','chanlun','macd_cross','ema_triple','squeeze_breakout','adx_trend_follow','rsi_pullback','rsi_real_divergence','rsi_breakout_50','volume_price_divergence','triple_screen'] },
    { name: '💰 加密专属', strategies: ['funding_extreme','oi_breakout','long_short_ratio','fear_greed_reversal','whale_activity','stablecoin_flow'] },
    { name: '🇨🇳 A 股专属', strategies: ['limit_up_followup','northbound_flow_top','sector_momentum','lhb_follow','margin_breakout'] },
    { name: '🇭🇰 港股专属', strategies: ['southbound_inflow','ah_spread_revert'] },
    { name: '🇺🇸 美股专属', strategies: ['gap_up_continuation','vwap_pullback','premarket_breakout','vix_extreme','relative_strength_top'] },
  ];
  const STRATEGY_NAME_CN = {
    ma_cross: '均线金叉死叉', donchian_breakout: '通道突破', bollinger_reversion: '布林带均值回归',
    volume_breakout: '成交量突破', flash_event: '新闻事件驱动', chanlun: '缠论买卖点',
    macd_cross: 'MACD 金叉死叉', ema_triple: 'EMA 三线排列', squeeze_breakout: '布林挤压突破',
    adx_trend_follow: 'ADX 趋势跟随',
    rsi_pullback: 'RSI 趋势回踩', rsi_real_divergence: 'RSI 真背离', rsi_breakout_50: 'RSI 50 上穿',
    volume_price_divergence: '量价背离', triple_screen: '三重过滤',
    funding_extreme: '资金费率极值', oi_breakout: 'OI 持仓突破', long_short_ratio: '多空比反转',
    fear_greed_reversal: 'F&G 极值反转',
    whale_activity: '链上巨鲸大单', stablecoin_flow: '稳定币流入',
    limit_up_followup: '涨停后回踩', northbound_flow_top: '北向资金排名', sector_momentum: '板块联动',
    lhb_follow: '龙虎榜跟盘', margin_breakout: '融资余额突破',
    southbound_inflow: '港股通南向', ah_spread_revert: 'AH 价差回归',
    gap_up_continuation: '高开延续', vwap_pullback: 'VWAP 回踩',
    premarket_breakout: '盘前/开盘突破', vix_extreme: 'VIX 极值反转',
    relative_strength_top: '相对大盘强势',
  };

  async function renderStrategyLibrary() {
    const cont = $('#library-list');
    try {
      const strategies = await loadStrategies();
      const byName = {};
      strategies.forEach(s => byName[s.name] = s);
      let html = '';
      for (const grp of STRATEGY_GROUPS) {
        html += `<div class="section-title" style="margin-top:12px;">${grp.name}</div>`;
        for (const n of grp.strategies) {
          const s = byName[n];
          if (!s) continue;
          const cn = STRATEGY_NAME_CN[n] || n;
          html += `<div class="card" style="margin-bottom:8px;padding:10px 12px;">
            <div style="display:flex;justify-content:space-between;align-items:baseline;">
              <div style="font-weight:600;font-size:14px;">${escape(cn)}</div>
              <div class="small muted">${escape(n)}</div>
            </div>
            <div class="row-meta" style="margin-top:4px;">${escape(s.description || '')}</div>
          </div>`;
        }
      }
      cont.innerHTML = html || '<div class="empty">暂无策略</div>';
    } catch (e) {
      cont.innerHTML = '<div class="empty">加载失败</div>';
    }
  }

  // ═══════════════════════════════════════════════════════════
  // 7. 设置
  // ═══════════════════════════════════════════════════════════
  async function renderSettings() {
    try {
      const [status, llmCost, riskRules] = await Promise.all([loadStatus(), loadLLMCost(), loadRiskRules()]);
      $('#auto-trade-toggle').checked = !!status.enabled;
      const cfg = status.config || {};
      $('#settings-status').innerHTML = `
        <div class="kv-row"><span class="k">服务地址</span><span class="v small">${location.host}</span></div>
        <div class="kv-row"><span class="k">初始资金</span><span class="v">$${(cfg.initial_capital_usd||0).toLocaleString()}</span></div>
        <div class="kv-row"><span class="k">总仓位上限</span><span class="v">${((cfg.total_position_cap_pct||0)*100).toFixed(0)}%</span></div>
        <div class="kv-row"><span class="k">单股软上限</span><span class="v">${((cfg.max_single_position_pct||0)*100).toFixed(0)}%</span></div>
        <div class="kv-row"><span class="k">单股硬上限</span><span class="v">${((cfg.hard_single_cap_pct||0.30)*100).toFixed(0)}%</span></div>
        <div class="kv-row"><span class="k">同股冷却</span><span class="v">${(cfg.cooldown_sec||0)/60} 分钟</span></div>
        <div class="kv-row"><span class="k">每日操作上限</span><span class="v">${cfg.max_daily_ops_per_symbol||0} 次/股</span></div>
        <div class="kv-row"><span class="k">并发持仓上限</span><span class="v">${cfg.max_concurrent_positions||0}</span></div>
      `;
      if (llmCost) {
        const used = llmCost.today_cost_usd || 0;
        const budget = llmCost.daily_budget || 0;
        const pct = budget > 0 ? (used / budget * 100) : 0;
        const byPath = (llmCost.by_path || []).map(b =>
          `<div class="kv-row"><span class="k">└ ${escape(b.path||'其它')}</span><span class="v">$${(b.cost||0).toFixed(4)} (${b.n} 次)</span></div>`
        ).join('');
        $('#settings-llm-cost').innerHTML = `
          <div class="kv-row"><span class="k">今日累计</span><span class="v ${pct>=80?'down':(pct>=50?'warn':'up')}">$${used.toFixed(4)} / $${budget.toFixed(2)} (${pct.toFixed(0)}%)</span></div>
          ${byPath}
        `;
      } else {
        $('#settings-llm-cost').innerHTML = '<div class="empty small">LLM 成本数据不可用</div>';
      }

      // v12.19.0: 风控规则已移到 learn/rules 子页，此处不再渲染
    } catch (e) {
      $('#settings-status').innerHTML = '<div class="empty small">加载失败</div>';
    }
  }

  function renderRuleCard(r) {
    const RULE_LABEL = {
      rsi_block: '🚫 RSI 超买拦截',
      drawdown_force_close: '🛑 浮亏强平',
      trend_block: '📉 下跌趋势拦截',
      cooldown_override: '⏱️ 冷却覆盖',
      prompt_principle: '💬 提示原则',
    };
    const SOURCE_LABEL = {
      auto_adopted: '🤖 自动',
      user_adopted: '👤 手动',
      manual: '👤 手动',
      migrated: '⚙️ 迁移',
    };
    const enabled = r.enabled == 1 || r.enabled === true;
    const fr = r.false_reject_rate || 0;
    const cls = !enabled ? 'disabled' : (fr > 30 ? 'high-fr' : '');
    const params = r.params || {};
    const paramsStr = Object.entries(params).map(([k,v]) => `${k}=${v}`).join(', ');
    return `<div class="rule-card ${cls}">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
        <div style="flex:1;">
          <div class="rule-name">${RULE_LABEL[r.rule_type] || r.rule_type}</div>
          <div class="rule-desc">${escape(r.description || paramsStr)}</div>
          <div class="rule-meta">
            <span>${escape(r.pool_id || 'all')}</span>
            <span>${SOURCE_LABEL[r.source_kind] || r.source_kind}</span>
            <span>命中 ${r.hits||0} 次</span>
            ${fr > 0 ? `<span class="${fr>30?'down':''}">假阳率 ${fr}%</span>` : ''}
          </div>
        </div>
        <label class="rule-toggle">
          <input type="checkbox" data-id="${r.id}" ${enabled?'checked':''}>
          <span class="slider"></span>
        </label>
      </div>
    </div>`;
  }

  // ─── 主刷新 ───
  async function refresh() {
    const key = `${_state.activeTab}/${_state.activeSub}`;
    const renderFn = TAB_RENDERERS[key];
    if (renderFn) {
      try { await renderFn(); }
      catch (e) { console.error(`[refresh] ${key} 失败:`, e); }
    } else {
      console.warn(`[refresh] 未知路由: ${key}`);
    }
    _state.lastUpdate = Date.now();
    updateLastUpdateLabel();
  }

  function updateLastUpdateLabel() {
    const el = $('#last-update');
    if (!el) return;
    const sec = Math.floor((Date.now() - _state.lastUpdate) / 1000);
    if (_state.lastUpdate === 0) { el.textContent = '—'; return; }
    if (sec < 5) el.textContent = '刚刚更新';
    else if (sec < 60) el.textContent = `${sec}秒前`;
    else if (sec < 3600) el.textContent = `${Math.floor(sec/60)}分钟前`;
    else el.textContent = `${Math.floor(sec/3600)}h前`;
  }
  setInterval(updateLastUpdateLabel, 5000);

  // v12.19.0: 5 main × N sub tab 路由表
  // 注意：TAB_RENDERERS 必须在所有 render 函数定义后再赋值（见文件末尾）
  let TAB_RENDERERS = {};

  // v12.21.0 PR1: 5 main × N sub 路由表 (新结构)
  // 注:renderOverview / renderSwapDashboard / renderOrderFlow / renderTradeControl /
  //    renderSettingsNotify / renderSettingsLLM / renderSettingsSystem 等新函数在文件末尾定义
  TAB_RENDERERS = {
    // 总览 (单页)
    'home/home':           renderOverview,
    // 行情 (5 sub) — 复用旧 render
    'market/news':         renderNews,
    'market/pool':         renderPoolWithMarketSummary,  // v12.21.0: 包装旧 renderPool 加市场汇总条
    'market/signals':      renderSignals,
    'market/library':      renderStrategyLibrary,
    'market/combos':       renderCombos,
    // 交易 (5 sub) — 含全新 ⚡合约 + 订单流水 + 自动控制
    'trade/spot':          renderPositions,             // 复用,但只显示现货
    'trade/swap':          renderSwapDashboard,         // 全新,合约专属页
    'trade/orders':        renderOrderFlow,             // 全新,合约+现货订单合并
    'trade/rejected':      renderRejected,
    'trade/control':       renderTradeControl,          // 全新,自动交易控制
    // 学习 (4 sub) — reviews / weekly 占位
    'learn/reviews':       renderReviewList,
    'learn/lessons':       renderLessons,
    'learn/rules':         renderRulesOnly,
    'learn/weekly':        renderWeeklyPlaceholder,
    // 设置 (4 sub) — 拆出 4 sub
    'settings/notify':     renderSettingsNotify,
    'settings/sources':    renderSourcesPlaceholder,
    'settings/llm':        renderSettingsLLM,
    'settings/system':     renderSettingsSystem,
  };

  // ═══════════════════════════════════════════════════════════
  // v12.21.0 PR1: 新增 render 函数 (总览 + 交易 5 sub + 设置 4 sub)
  // ═══════════════════════════════════════════════════════════

  // ─── 总览页 (升级版 — 加 ⚡合约池 + 实时告警) ───
  async function renderOverview() {
    try {
      const [status, log, signals, positions, llmCost, swapAcct, swapPos] = await Promise.all([
        loadStatus(), loadHistory(), loadSignals(), loadPositions(), loadLLMCost(),
        loadSwapAccount(), loadSwapPositions(),
      ]);

      // ─ 顶部 enabled badge ─
      const enabled = status.enabled;
      const badge = $('#enabled-badge');
      badge.textContent = enabled ? '自动交易 开' : '自动交易 关';
      badge.className = 'badge ' + (enabled ? 'on' : 'off');

      // ─ Hero: 总权益(含合约) ─
      const pools = status.pools || [];
      let totalEquityUSD = 0, totalPnlUSD = 0, totalInitialUSD = 0;
      for (const p of pools) {
        totalEquityUSD += p.equity_usd || 0;
        totalPnlUSD += p.pnl_usd || 0;
        const fx = p.fx_to_usd || 1;
        totalInitialUSD += (p.initial_capital || 0) * fx;
      }
      // v12.21.0: 加合约账户到总权益
      const swapMode = swapAcct && swapAcct.mode === 'swap_mock';
      let swapEquity = 0, swapInitial = 0, swapPnl = 0, swapUpnl = 0;
      if (swapAcct) {
        swapEquity = (swapAcct.balance_usd || 0) + (swapAcct.total_margin_usd || 0);
        swapInitial = swapAcct.initial_balance_usd || 0;
        swapPnl = swapAcct.total_pnl_usd || 0;
        swapUpnl = (swapPos || []).reduce((s, p) => s + (p.unrealized_pnl_usd || 0), 0);
      }
      const grandEquity = totalEquityUSD + swapEquity + swapUpnl;
      const grandPnl = totalPnlUSD + swapPnl + swapUpnl;
      const grandInitial = totalInitialUSD + swapInitial;
      const grandPct = grandInitial > 0 ? (grandPnl / grandInitial * 100) : 0;
      $('#ov-equity').textContent = fmtMoney(grandEquity);
      $('#ov-pnl').innerHTML = `<span class="${grandPnl>=0?'up':'down'}">${fmtPnl(grandPnl)} (${fmtPct(grandPct)})</span>`;

      // ─ Quick stats ─
      const posCount = (positions || []).length + (swapPos || []).length;
      const todayStart = (() => { const d = new Date(); d.setHours(0,0,0,0); return d.getTime(); })();
      const todayTrades = (log.items || []).filter(t =>
        t.status === 'executed' && (t.traded_at * 1000) >= todayStart
      ).length;
      const sigItems = signals.items || [];
      const now = Date.now();
      const pendingReval = sigItems.filter(s =>
        s.ai_verdict === 'confirm'
        && (s.revalidated_at == 0 || !s.revalidated_at)
        && (s.status === 'active' || !s.status)
        && (now - (s.generated_at || 0)) < 24 * 3600 * 1000
      ).length;
      const todayCost = (llmCost && llmCost.today_total_usd) || 0;
      $('#home-stats').innerHTML = `
        <div class="stat-card"><div class="stat-label">📊 总持仓</div><div class="stat-value">${posCount}</div></div>
        <div class="stat-card"><div class="stat-label">📥 今日成交</div><div class="stat-value">${todayTrades}</div></div>
        <div class="stat-card"><div class="stat-label">⏳ 待重验</div><div class="stat-value ${pendingReval>0?'warn':''}">${pendingReval}</div></div>
        <div class="stat-card"><div class="stat-label">💰 今日 AI</div><div class="stat-value">$${todayCost.toFixed(3)}</div></div>
      `;

      // ─ 4 池横滑卡 (含 ⚡合约) ─
      const stockCards = pools.sort((a,b)=> {
        const ord = {us_hk:0, cn:1, crypto:2};
        return (ord[a.pool_id]||9) - (ord[b.pool_id]||9);
      }).map(p => {
        const cls = (p.pnl||0) >= 0 ? 'up' : 'down';
        const ccy = p.currency || 'USD';
        return `<div class="pool-card ${cls}">
          <div>
            <div class="pool-name">${escape(p.name)} (${ccy})</div>
            <div class="pool-equity">${fmtMoney(p.equity, ccy)}</div>
            <div class="pool-pnl ${cls}">${fmtPnl(p.pnl, ccy)} (${fmtPct(p.pnl_pct)})</div>
            <div class="pool-meta">现金 ${fmtMoney(p.cash, ccy)}</div>
          </div>
          <div class="pool-arrow">›</div>
        </div>`;
      }).join('');
      // 合约池卡(独立样式)
      let swapCard = '';
      if (swapMode) {
        const totalSwap = swapPnl + swapUpnl;
        const swapCls = totalSwap >= 0 ? 'up' : 'down';
        swapCard = `<div class="pool-card swap" data-go-tab="trade" data-go-sub="swap" style="cursor:pointer;">
          <div>
            <div class="pool-name">⚡ 加密合约 (USD)</div>
            <div class="pool-equity">${fmtMoney(swapEquity + swapUpnl)}</div>
            <div class="pool-pnl ${swapCls}">${fmtPnl(totalSwap)} · ${swapPos.length} 仓</div>
            <div class="pool-meta">余额 ${fmtMoney(swapAcct.balance_usd||0)} · 浮 ${fmtPnl(swapUpnl)}</div>
          </div>
          <div class="pool-arrow">›</div>
        </div>`;
      }
      $('#ov-pools').innerHTML = stockCards + swapCard || '<div class="empty">暂无池数据</div>';
      // 合约池卡片点击 → 跳到合约页
      $$('#ov-pools .pool-card[data-go-tab]').forEach(c => {
        c.addEventListener('click', () => {
          _state.activeSub = c.dataset.goSub;
          switchTab(c.dataset.goTab);
        });
      });

      // ─ 实时告警 (v12.21.0 新模块) ─
      const alerts = computeAlerts({ positions, swapPos, signals: sigItems });
      const alertCard = $('#ov-alerts-card');
      if (alerts.length) {
        alertCard.hidden = false;
        $('#ov-alerts').innerHTML = alerts.map(a => `
          <div class="alert-row severity-${a.severity}">
            <div class="alert-icon">${a.icon}</div>
            <div class="alert-body">
              <div class="alert-title">${escape(a.title)}</div>
              <div class="alert-desc">${escape(a.desc)}</div>
            </div>
          </div>
        `).join('');
      } else {
        alertCard.hidden = true;
      }

      // ─ 24h 重点信号 ─
      const sigs = sigItems.filter(s => ['confirm','warn'].includes(s.ai_verdict)).slice(0, 5);
      $('#ov-recent-signals-list').innerHTML = sigs.length
        ? sigs.map(renderSignalRow).join('')
        : '<div class="empty small">暂无重点信号</div>';
      $$('#ov-recent-signals-list .row').forEach(r => {
        r.addEventListener('click', () => openSignalDetail(r.dataset.id));
      });

      // ─ 最近成交 ─
      const trades = (log.items || []).filter(t => t.status === 'executed').slice(0, 5);
      $('#ov-recent-trades-list').innerHTML = trades.length
        ? trades.map(renderTradeRow).join('')
        : '<div class="empty small">暂无成交</div>';

    } catch (e) {
      console.error('[overview]', e);
      $('#ov-equity').textContent = '加载失败';
    }
  }

  // ─── 实时告警计算逻辑 ───
  function computeAlerts({ positions, swapPos, signals }) {
    const alerts = [];
    // 1. 合约距强平 < 5%
    for (const p of (swapPos || [])) {
      if (!p.liq_price || !p.avg_open_price) continue;
      const distPct = p.pos_side === 'long'
        ? (1 - p.liq_price / p.avg_open_price) * 100
        : (p.liq_price / p.avg_open_price - 1) * 100;
      if (distPct > 0 && distPct < 5) {
        const sym = (p.symbol || '').replace('-SWAP', '');
        alerts.push({
          severity: distPct < 3 ? 'high' : 'mid',
          icon: '💀',
          title: `${sym} 距强平 ${distPct.toFixed(1)}%`,
          desc: `${p.pos_side === 'long' ? '多' : '空'} ${p.leverage}x · 浮亏 ${fmtPnl(p.unrealized_pnl_usd)}`,
        });
      }
    }
    // 2. 现货浮亏 > 5%
    for (const p of (positions || [])) {
      if ((p.pnl_pct || 0) < -5) {
        alerts.push({
          severity: (p.pnl_pct < -10) ? 'high' : 'mid',
          icon: '📉',
          title: `${p.symbol} 浮亏 ${(p.pnl_pct || 0).toFixed(2)}%`,
          desc: `${p.market} · ${(p.pnl_pct || 0) < -10 ? '严重亏损,关注' : '关注 SL 触发'}`,
        });
      }
    }
    // 3. 24h confirm 信号未重验
    const now = Date.now();
    const pending = (signals || []).filter(s =>
      s.ai_verdict === 'confirm'
      && (!s.revalidated_at || s.revalidated_at == 0)
      && (s.status === 'active' || !s.status)
      && (now - (s.generated_at || 0)) < 6 * 3600 * 1000  // 6h 内才告警
    );
    if (pending.length > 0) {
      alerts.push({
        severity: 'low',
        icon: '⏳',
        title: `${pending.length} 条信号待重验`,
        desc: '6h 内 confirm 信号还没二次确认',
      });
    }
    return alerts.slice(0, 5);  // 最多 5 条
  }

  // ─── ⚡合约 dashboard (PR1 核心新模块) ───
  async function renderSwapDashboard() {
    try {
      const [acct, positions, orders] = await Promise.all([
        loadSwapAccount(), loadSwapPositions(), loadSwapOrdersAll(),
      ]);

      // 模式检查
      if (!acct || acct.mode !== 'swap_mock') {
        $('#swap-account-hero').innerHTML = `<div style="padding:20px;text-align:center;">
          <div style="font-size:36px;margin-bottom:8px;">🔒</div>
          <div style="font-size:14px;font-weight:600;">合约模式未启用</div>
          <div class="muted small" style="margin-top:6px;">当前 spot_mock 模式 · 去 [交易/自动] 切换到 swap_mock</div>
        </div>`;
        $('#swap-positions-summary').innerHTML = '';
        $('#swap-positions-list').innerHTML = '';
        return;
      }

      // ─ Hero: 账户信息 ─
      const balance = acct.balance_usd || 0;
      const margin = acct.total_margin_usd || 0;
      const realized = acct.total_pnl_usd || 0;
      const unrealized = (positions || []).reduce((s, p) => s + (p.unrealized_pnl_usd || 0), 0);
      const equity = balance + margin + unrealized;
      const initial = acct.initial_balance_usd || 10000;
      const totalRet = ((equity - initial) / initial * 100);
      const upnlCls = unrealized >= 0 ? 'up' : 'down';
      const realizedCls = realized >= 0 ? 'up' : 'down';
      const totalCls = totalRet >= 0 ? 'up' : 'down';
      $('#swap-account-hero').innerHTML = `
        <div class="swap-hero-label">⚡ 永续合约账户净值</div>
        <div class="swap-hero-balance">${fmtMoney(equity)}</div>
        <div class="${totalCls}" style="font-size:13px;margin-top:2px;">${fmtPct(totalRet)} (初始 ${fmtMoney(initial)})</div>
        <div class="swap-hero-grid">
          <div class="swap-hero-cell">
            <div class="cell-label">可用余额</div>
            <div class="cell-value">${fmtMoney(balance)}</div>
          </div>
          <div class="swap-hero-cell">
            <div class="cell-label">占用保证金</div>
            <div class="cell-value">${fmtMoney(margin)}</div>
          </div>
          <div class="swap-hero-cell">
            <div class="cell-label">浮动 PnL</div>
            <div class="cell-value ${upnlCls}">${fmtPnl(unrealized)}</div>
          </div>
        </div>
        <div style="margin-top:8px;font-size:11px;color:#b8a8d0;text-align:center;">
          累计已实现 PnL <span class="${realizedCls}">${fmtPnl(realized)}</span>
        </div>
      `;

      // ─ 持仓汇总 ─
      const longN = (positions || []).filter(p => p.pos_side === 'long').length;
      const shortN = (positions || []).filter(p => p.pos_side === 'short').length;
      const pendingOrders = (orders || []).filter(o => o.status === 'pending').length;
      $('#swap-positions-summary').innerHTML = `
        <div class="summary-row">
          <span class="summary-k">持仓 ${positions.length}</span>
          <span class="summary-v">🟢多 ${longN} · 🔴空 ${shortN}</span>
        </div>
        <div class="summary-row">
          <span class="summary-k">挂单中</span>
          <span class="summary-v">${pendingOrders} 笔限价</span>
        </div>
      `;

      // ─ 持仓列表 ─
      if (!positions.length) {
        $('#swap-positions-list').innerHTML = `<div class="empty">
          <div class="empty-icon">📭</div>
          <div>暂无合约持仓</div>
          <div class="small muted" style="margin-top:6px;">系统会按加密信号自动开仓</div>
        </div>`;
        return;
      }
      $('#swap-positions-list').innerHTML = positions.map(renderSwapPositionDetail).join('');
      // 绑定平仓按钮
      $$('#swap-positions-list .btn-close').forEach(btn => {
        btn.addEventListener('click', async (e) => {
          e.stopPropagation();
          const posId = btn.dataset.posid;
          const sym = btn.dataset.sym;
          if (!confirm(`确定平仓 ${sym}?(市价单立即成交)`)) return;
          btn.disabled = true; btn.textContent = '⏳ 平仓中...';
          try {
            const r = await fetch('/api/swap/close/' + encodeURIComponent(posId), {method: 'POST'});
            const d = await r.json();
            if (d.ok) {
              toast('✅ 已平仓 ' + sym, 'up');
              _state.cache.swapPos = null; _state.cache.swapAcct = null;
              setTimeout(refresh, 800);
            } else {
              toast('❌ 平仓失败: ' + (d.reason || d.detail || '未知'), 'down');
              btn.disabled = false; btn.textContent = '🔴 手动平仓';
            }
          } catch (err) {
            toast('❌ ' + err.message, 'down');
            btn.disabled = false; btn.textContent = '🔴 手动平仓';
          }
        });
      });

    } catch (e) {
      console.error('[swap-dash]', e);
      $('#swap-account-hero').innerHTML = '<div class="empty">加载失败</div>';
    }
  }

  // ─── 合约持仓 row(详细版,含手动平仓按钮) ───
  function renderSwapPositionDetail(p) {
    const sym = (p.symbol || '').replace('-SWAP', '');
    const isLong = p.pos_side === 'long';
    const sideTxt = isLong ? '多' : '空';
    const sideCls = isLong ? 'long' : 'short';
    const upnl = p.unrealized_pnl_usd || 0;
    const upnlCls = upnl >= 0 ? 'up' : 'down';
    // 距强平距离
    const liqDist = (p.liq_price && p.avg_open_price)
      ? (isLong ? (1 - p.liq_price/p.avg_open_price) : (p.liq_price/p.avg_open_price - 1)) * 100
      : null;
    const liqDistTxt = liqDist != null ? `${liqDist.toFixed(1)}%` : '?';
    const danger = liqDist != null && liqDist < 5;
    const liqBarPct = liqDist != null ? Math.max(0, Math.min(100, 100 - liqDist * 4)) : 0;
    // 浮盈率
    const pnlPct = (p.qty && p.avg_open_price && p.contract_size && p.margin_usd)
      ? (upnl / p.margin_usd * 100)
      : 0;
    return `<div class="swap-row ${danger ? 'danger' : ''}">
      <div class="swap-row-hdr">
        <div>
          <span class="swap-symbol">${escape(sym)}</span>
          <span class="swap-side-badge ${sideCls}">${sideTxt}</span>
          <span class="lev-badge">${p.leverage}x</span>
        </div>
        <div class="swap-pnl ${upnlCls}">${fmtPnl(upnl)}<br><span style="font-size:10px;font-weight:400;">${fmtPct(pnlPct)}</span></div>
      </div>
      <div class="swap-info-grid">
        <div class="swap-info-row"><span class="k">数量</span><span class="v">${(p.qty||0).toFixed(4)} 张</span></div>
        <div class="swap-info-row"><span class="k">均价</span><span class="v">${(p.avg_open_price||0).toFixed(4)}</span></div>
        <div class="swap-info-row"><span class="k">保证金</span><span class="v">${fmtMoney(p.margin_usd||0)}</span></div>
        <div class="swap-info-row"><span class="k">强平价</span><span class="v ${danger?'down':''}">${(p.liq_price||0).toFixed(4)}</span></div>
        <div class="swap-info-row"><span class="k">SL</span><span class="v">${p.stop_loss ? p.stop_loss.toFixed(4) : '—'}</span></div>
        <div class="swap-info-row"><span class="k">TP</span><span class="v">${p.take_profit ? p.take_profit.toFixed(4) : '—'}</span></div>
        <div class="swap-info-row"><span class="k">资金费</span><span class="v ${(p.funding_fee_total_usd||0)>=0?'up':'down'}">${fmtPnl(p.funding_fee_total_usd||0)}</span></div>
        <div class="swap-info-row"><span class="k">手续费</span><span class="v down">-${fmtMoney(p.total_fee_usd||0)}</span></div>
      </div>
      ${liqDist != null ? `
        <div style="font-size:10px;color:var(--text-3);margin-top:8px;display:flex;justify-content:space-between;">
          <span>距强平 ${liqDistTxt}</span>
          ${p.pre_liq_armed ? '<span style="color:#f59e0b;">⚠️ 已减仓</span>' : ''}
          ${p.breakeven_armed ? '<span style="color:#22c55e;">✓ 保本</span>' : ''}
          ${p.trailing_armed ? '<span style="color:#a78bfa;">📈 trailing</span>' : ''}
        </div>
        <div class="liq-bar"><div class="liq-bar-fill" style="width:${liqBarPct}%;"></div></div>
      ` : ''}
      <div class="swap-actions">
        <button class="btn-close" data-posid="${escape(p.id)}" data-sym="${escape(sym)}">🔴 手动平仓</button>
      </div>
    </div>`;
  }

  // ─── 订单流水 (合约 + 现货合并) ───
  async function loadSwapOrdersAll() {
    try {
      const r = await fetchJSON('/api/swap/orders?limit=100');
      return r.items || [];
    } catch { return []; }
  }
  async function renderOrderFlow() {
    try {
      const [swapOrders, log] = await Promise.all([
        loadSwapOrdersAll(),
        loadHistory(),
      ]);
      // 把 swap orders 和 spot trades 合并到一个时间线
      const items = [];
      for (const o of (swapOrders || [])) {
        items.push({
          ts: o.created_at,
          status: o.status,
          symbol: (o.symbol || '').replace('-SWAP', ''),
          side: o.side,
          pos_side: o.pos_side,
          intent: o.intent,
          price: o.fill_price || o.price || 0,
          qty: o.fill_qty || o.qty || 0,
          fee: o.fee_usd || 0,
          leverage: o.leverage,
          isSwap: true,
          reason: o.reject_reason || '',
        });
      }
      for (const t of (log.items || [])) {
        items.push({
          ts: t.traded_at,
          status: t.status,
          symbol: t.symbol,
          side: t.action,
          intent: t.action,
          price: t.price || 0,
          qty: t.quantity || 0,
          isSwap: t.market === 'crypto' && t.trigger_type && t.trigger_type.indexOf('swap') === 0,
          reason: t.reason || '',
          market: t.market,
        });
      }
      // 按时间倒序
      items.sort((a, b) => (b.ts || 0) - (a.ts || 0));

      // 应用 filter
      const f = _state.orderFilter || 'all';
      const filtered = f === 'all' ? items : items.filter(i => i.status === f);

      if (!filtered.length) {
        $('#order-flow-list').innerHTML = `<div class="empty">
          <div class="empty-icon">📋</div>
          <div>无订单记录</div>
        </div>`;
        return;
      }
      $('#order-flow-list').innerHTML = filtered.slice(0, 80).map(renderOrderRow).join('');
    } catch (e) {
      console.error('[order-flow]', e);
      $('#order-flow-list').innerHTML = '<div class="empty">加载失败</div>';
    }
  }
  function renderOrderRow(o) {
    const intentTxt = ACTION_LABEL[o.intent] || o.intent || '?';
    const sideTxt = o.pos_side ? (o.pos_side === 'long' ? '多' : '空') : '';
    const swapTag = o.isSwap ? `<span class="swap-tag">⚡${o.leverage || ''}${o.leverage ? 'x' : ''}</span>` : '';
    const statusLabel = {
      pending: '⏳挂单', filled: '✅成交', cancelled: '⊘撤单',
      rejected: '❌拒单', executed: '✅成交',
    }[o.status] || o.status;
    return `<div class="order-row status-${o.status}">
      <div class="order-hdr">
        <div class="order-symbol">${escape(o.symbol)} ${swapTag} <span class="muted small">${intentTxt} ${sideTxt}</span></div>
        <span class="order-status">${statusLabel}</span>
      </div>
      <div class="order-meta">
        ${(o.qty||0).toFixed(4)} @ ${(o.price||0).toFixed(4)}
        ${o.fee ? ` · 手续费 ${fmtMoney(o.fee)}` : ''}
        · ${fmtRelTime(o.ts * 1000)}
      </div>
      ${o.reason && o.status !== 'filled' && o.status !== 'executed' ? `<div class="order-meta small" style="color:var(--color-down);margin-top:4px;">${escape(o.reason).slice(0, 80)}</div>` : ''}
    </div>`;
  }

  // ─── 自动交易控制 ───
  async function renderTradeControl() {
    try {
      const [status, swapAcct] = await Promise.all([loadStatus(), loadSwapAccount()]);
      // 同步 toggle 状态
      $('#auto-trade-toggle').checked = !!status.enabled;

      // 加密模式 radio
      const currentMode = swapAcct ? swapAcct.mode : 'spot_mock';
      $('#crypto-mode-radio').innerHTML = `
        <label class="${currentMode === 'spot_mock' ? 'checked' : ''}">
          <input type="radio" name="crypto-mode" value="spot_mock" ${currentMode === 'spot_mock' ? 'checked' : ''}>
          <div>
            <div class="r-title">🪙 现货 mock</div>
            <div class="r-sub">买入持有,无杠杆</div>
          </div>
        </label>
        <label class="${currentMode === 'swap_mock' ? 'checked' : ''}">
          <input type="radio" name="crypto-mode" value="swap_mock" ${currentMode === 'swap_mock' ? 'checked' : ''}>
          <div>
            <div class="r-title">⚡ 永续合约 mock</div>
            <div class="r-sub">双向 + 杠杆 1-20x + 真实 OKX 数据</div>
          </div>
        </label>
      `;
      $$('#crypto-mode-radio input[name=crypto-mode]').forEach(r => {
        r.addEventListener('change', async (e) => {
          const newMode = e.target.value;
          if (!confirm('切换到 ' + (newMode === 'swap_mock' ? '永续合约' : '现货') + ' 模式?')) {
            e.target.checked = !e.target.checked;
            return;
          }
          try {
            const resp = await fetch('/api/swap/mode', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({mode: newMode}),
            });
            const d = await resp.json();
            if (d.ok) {
              toast('✅ 已切换到 ' + newMode, 'up');
              _state.cache.swapAcct = null;
              setTimeout(refresh, 600);
            } else {
              toast('❌ ' + (d.detail || '失败'), 'down');
            }
          } catch (err) {
            toast('❌ ' + err.message, 'down');
          }
        });
      });

      // 当前配置摘要
      const cfg = status.config || {};
      $('#trade-config-info').innerHTML = `
        <div class="kv-row"><span class="k">单股每日操作上限</span><span class="v">${cfg.max_daily_ops_per_symbol || 5} 次</span></div>
        <div class="kv-row"><span class="k">同股冷却时间</span><span class="v">${Math.round((cfg.cooldown_sec || 900) / 60)} 分钟</span></div>
        <div class="kv-row"><span class="k">单笔目标占比</span><span class="v">${((cfg.open_position_pct_buy || 0.05) * 100).toFixed(1)}%</span></div>
        <div class="kv-row"><span class="k">单股软上限</span><span class="v">${((cfg.market_sizing_us_max_single || 0.12) * 100).toFixed(0)}%</span></div>
        <div class="kv-row"><span class="k">单股硬上限</span><span class="v">${((cfg.hard_single_cap_pct || 0.30) * 100).toFixed(0)}%</span></div>
      `;
    } catch (e) {
      console.error('[trade-control]', e);
    }
  }

  // ─── 行情/候选池 增强 (加市场上限汇总) ───
  async function renderPoolWithMarketSummary() {
    // 先渲染原有 pool
    await renderPool();
    // 在顶部加按市场分组汇总
    try {
      const data = _state.cache.pool;
      if (!data || !data.items) return;
      const items = data.items;
      const CAPS = { us: 650, cn: 600, hk: 200 };
      const NAMES = { us: '🇺🇸 美股', cn: '🇨🇳 A 股', hk: '🇭🇰 港股' };
      const byMarket = items.reduce((acc, it) => {
        const m = it.market || 'unknown';
        if (it.status === 'archived') return acc;
        acc[m] = (acc[m] || 0) + 1;
        return acc;
      }, {});
      const html = `<div class="pool-market-chips">${
        ['us', 'cn', 'hk'].map(m => {
          const n = byMarket[m] || 0;
          const cap = CAPS[m];
          const ratio = n / cap;
          const cls = ratio >= 0.9 ? 'usage-high' : ratio >= 0.8 ? 'usage-mid' : 'usage-low';
          return `<div class="pool-market-chip ${cls}">
            <div class="pmc-name">${NAMES[m]}</div>
            <div class="pmc-count">${n}</div>
            <div class="pmc-cap">/ ${cap} (${(ratio*100).toFixed(0)}%)</div>
          </div>`;
        }).join('')
      }</div>`;
      const sumEl = $('#pool-market-summary');
      if (sumEl) sumEl.innerHTML = html;
    } catch (e) { console.debug('pool summary', e); }
  }

  // ─── 设置: 通知 ───
  async function renderSettingsNotify() {
    $('#settings-channels').innerHTML = `
      <div class="kv-row"><span class="k">Telegram</span><span class="v up">✅ 已配置</span></div>
      <div class="kv-row"><span class="k">推送频率</span><span class="v">实时 + 4h 简报</span></div>
      <div class="kv-row muted small"><span class="k" colspan="2">详细推送类型管理 (PR3 实现)</span></div>
    `;
  }

  // ─── 设置: LLM 配额 ───
  async function renderSettingsLLM() {
    try {
      const cost = await loadLLMCost();
      if (!cost) {
        $('#llm-detail-card').innerHTML = '<div class="empty">无法加载 LLM 数据</div>';
        return;
      }
      const today = cost.today_total_usd || 0;
      const limit = cost.daily_limit_usd || 5.0;
      const ratio = Math.min(100, today / limit * 100);
      const byPath = cost.today_by_path || {};
      const pathRows = Object.entries(byPath)
        .sort((a, b) => (b[1].cost_usd || 0) - (a[1].cost_usd || 0))
        .map(([path, d]) => {
          const c = d.cost_usd || 0;
          const pct = today > 0 ? (c / today * 100).toFixed(0) : 0;
          return `<div class="kv-row"><span class="k">${escape(path)}</span><span class="v">$${c.toFixed(4)} (${pct}%)</span></div>`;
        }).join('');
      $('#llm-detail-card').innerHTML = `
        <div class="section-title">💰 今日 LLM 花费</div>
        <div style="text-align:center;padding:8px;">
          <div style="font-size:24px;font-weight:700;">$${today.toFixed(4)}</div>
          <div class="small muted">/ $${limit.toFixed(2)} 上限</div>
        </div>
        <div class="llm-progress"><div class="llm-progress-fill" style="width:${ratio}%;"></div></div>
        <div class="small muted center" style="margin-top:4px;">${ratio.toFixed(1)}% 已用</div>
        <div style="margin-top:14px;">
          <div class="section-title small">按 path 分布</div>
          ${pathRows || '<div class="empty small">无数据</div>'}
        </div>
      `;
    } catch (e) {
      $('#llm-detail-card').innerHTML = '<div class="empty">加载失败</div>';
    }
  }

  // ─── 设置: 系统状态 ───
  async function renderSettingsSystem() {
    try {
      const status = await loadStatus();
      const cfg = status.config || {};
      const rows = [];
      rows.push(['运行状态', status.enabled ? '🟢 自动交易开' : '🔴 自动交易关']);
      rows.push(['加密模式', cfg.crypto_trading_mode || 'spot_mock']);
      rows.push(['池子数', (status.pools || []).length]);
      const totalPos = (status.pools || []).reduce((s, p) => s + (p.position_count || 0), 0);
      rows.push(['总持仓数', totalPos]);
      $('#settings-status').innerHTML = rows.map(([k, v]) =>
        `<div class="kv-row"><span class="k">${escape(k)}</span><span class="v">${escape(v)}</span></div>`
      ).join('');
    } catch (e) {
      $('#settings-status').innerHTML = '<div class="empty">加载失败</div>';
    }
  }

  // ─── 占位 (PR2/PR3 实现) ───
  async function renderWeeklyPlaceholder() { /* 已在 HTML 内放占位 */ }
  async function renderSourcesPlaceholder() { /* 已在 HTML 内放占位 */ }

  // 自动 30s 轮询当前 tab
  setInterval(() => {
    Object.keys(_state.cache).forEach(k => _state.cache[k] = null);
    refresh();
  }, 30000);

  refresh();
})();
