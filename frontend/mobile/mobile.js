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

// v12.27.9: 立刻设置版本徽章 (不等 IIFE/renderNow), 防 5s 超时误报 'JS 未运行'
// v12.27.13: 修 — mobile.js 在 version-badge div 之前加载, 此时 getElementById
//   返回 null. 加 DOMContentLoaded 兜底, 确保 div 解析后再设
(function() {
  function _setLoadedBadge() {
    const vs = document.getElementById('version-status');
    if (vs) {
      vs.textContent = '✓ 已加载 ' + new Date().toLocaleTimeString().slice(0,5);
      vs.style.color = '#3fb950';
    }
  }
  // 立即试 (脚本顺序对的情况)
  _setLoadedBadge();
  // 兜底: DOM 完成后再试 (脚本在 div 之前的情况)
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _setLoadedBadge, { once: true });
  } else {
    setTimeout(_setLoadedBadge, 0);
  }
})();

// v12.27.5 全局 runtime error 拦截 (放 IIFE 外, 优先生效)
window.addEventListener('error', function(e) {
  const vs = document.getElementById('version-status');
  if (vs) {
    vs.textContent = '⚠️ ' + ((e.message || 'JS Err').slice(0, 30));
    vs.style.color = '#f85149';
    vs.parentElement.title = (e.filename || '') + ':' + (e.lineno || '') + '\n' + (e.message || '');
    // 把错误堆栈也显示在屏幕中央 (持续 30s)
    const errBox = document.createElement('div');
    errBox.style.cssText = 'position:fixed;top:80px;left:14px;right:14px;background:#2a1010;border:2px solid #f85149;border-radius:8px;padding:12px;color:#fff;font-size:11px;z-index:9999;word-break:break-all;';
    errBox.innerHTML = '<div style="color:#f85149;font-weight:700;margin-bottom:6px;">⚠️ JS RUNTIME ERROR</div>' +
      '<div><b>消息:</b> ' + (e.message || '?') + '</div>' +
      '<div><b>文件:</b> ' + (e.filename || '?') + ':' + (e.lineno || '?') + ':' + (e.colno || '?') + '</div>' +
      '<div style="margin-top:6px;color:#9ba8b8;"><b>堆栈:</b> ' + ((e.error && e.error.stack) ? String(e.error.stack).slice(0, 400) : '无') + '</div>' +
      '<button onclick="this.parentElement.remove()" style="background:#f85149;color:#fff;border:none;padding:4px 10px;border-radius:4px;margin-top:6px;font-size:11px;cursor:pointer;">关闭</button>';
    document.body.appendChild(errBox);
    setTimeout(() => errBox.remove(), 30000);
  }
});
window.addEventListener('unhandledrejection', function(e) {
  const vs = document.getElementById('version-status');
  if (vs) {
    vs.textContent = '⚠️ Promise: ' + ((e.reason && e.reason.message) || 'rej').slice(0, 25);
    vs.style.color = '#f85149';
  }
});

(function() {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);

  // v12.27.5: 立即标记 IIFE 开始执行
  const _vs0 = document.getElementById('version-status');
  if (_vs0) { _vs0.textContent = '🟡 IIFE 启动'; _vs0.style.color = '#d29922'; }

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
    'trade/ondemand':    '🔍 按需分析',
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

  // v12.27.0: v3 4 tab 任务驱动路由
  //   旧 5 tab × N sub → 新 4 tab × N sub + 设置移右上 ⚙
  //   now (无 sub) / holdings (chip 切换) / opp (含 8 sub) / insights (含 5 sub)
  const TAB_DEFAULT_SUB = {
    now: 'now',           // 主页一屏
    holdings: 'all',      // chip 切换
    opp: 'signals',       // 默认机会
    insights: 'reviews',  // 默认复盘
    // 兼容旧路径 (重定向)
    home: 'now',
    market: 'opp',
    trade: 'holdings',
    learn: 'insights',
    settings: 'notify',
  };
  // 旧 sub → 新 sub 映射 (Hash 路由兼容)
  const LEGACY_TAB_REDIRECT = {
    'home/home': ['now', 'now'],
    'market/news': ['opp', 'news'],
    'market/pool': ['opp', 'pool'],
    'market/signals': ['opp', 'signals'],
    'market/library': ['opp', 'library'],
    'market/combos': ['opp', 'combos'],
    'trade/spot': ['holdings', 'all'],
    'trade/swap': ['holdings', 'swap'],
    'trade/ondemand': ['opp', 'ondemand'],
    'trade/orders': ['opp', 'orders'],
    'trade/rejected': ['opp', 'rejected'],
    'trade/control': ['settings', 'auto'],   // 设置 sheet
    'learn/reviews': ['insights', 'reviews'],
    'learn/lessons': ['insights', 'lessons'],
    'learn/rules': ['insights', 'rules'],
    'learn/weekly': ['insights', 'weekly'],
  };

  let _state = {
    activeTab: 'now',
    activeSub: 'now',          // v12.27.7: 默认 'now' (匹配新 4 tab); 旧 'home' 导致路由 'now/home' 找不到 renderer
    signalFilter: 'all',
    newsFilter: 'all',
    poolFilter: 'all',
    rejectedFilter: 'all',
    orderFilter: 'all',        // v12.21.0: 订单流水 filter (pending/filled/cancelled)
    orderMarketFilter: 'all',  // v12.27.9: 订单流水按市场过滤 (all/us/hk/cn/crypto)
    holdingsFilter: 'all',     // v12.27.0: 持仓页 chip (all/us/hk/cn/crypto/swap/risk)
    // v12.25.0: 跨页联动 — 跳转后可设的临时过滤
    symbolFilter: null,        // 跨 tab 跳转时锁定 symbol (e.g. AAPL)
    cache: {
      status: null, positions: null, signals: null, history: null,
      news: null, pool: null, reviews: null, lessons: null,
      advices: null, llmCost: null, riskRules: null, strategies: null,
      rejectedTrades: null,
      swapAcct: null, swapPos: null, swapOrders: null,  // v12.21.0: swap 缓存
    },
    lastUpdate: 0,
  };

  // ═══════════════════════════════════════════════════════════
  // v12.25.0: 全局跨页联动 — navigate(tab, sub?, opts?)
  // opts: {
  //   filter: { key: 'signal'|'pool'|'rejected'|'order'|'news', value: 'all'|... },
  //   symbolFilter: 'AAPL',  // 跨 tab 锁定 symbol (持仓→历史信号场景)
  // }
  // 用途: 总览 stats 可点击, 持仓抽屉跳新闻/复盘/信号, 复盘跳教训等
  // ═══════════════════════════════════════════════════════════
  const FILTER_CHIP_ATTR = {
    signal: 'data-vfilter',
    pool: 'data-pfilter',
    rejected: 'data-rjfilter',
    order: 'data-ofilter',
    news: 'data-nfilter',
  };
  const FILTER_STATE_KEY = {
    signal: 'signalFilter', pool: 'poolFilter',
    rejected: 'rejectedFilter', order: 'orderFilter', news: 'newsFilter',
  };
  // v12.25.3 Phase D: Hash 路由
  let _hashSyncing = false;  // 防止 hash → state → hash 循环
  function buildHash() {
    const parts = [_state.activeTab, _state.activeSub].filter(Boolean);
    let hash = '#/' + parts.join('/');
    const qs = [];
    if (_state.symbolFilter) qs.push('s=' + encodeURIComponent(_state.symbolFilter));
    // 把对应 filter (非 'all') 也写进 URL
    const tabFilter = {
      'market/signals': ['signal', 'signalFilter'],
      'market/news': ['news', 'newsFilter'],
      'market/pool': ['pool', 'poolFilter'],
      'trade/rejected': ['rejected', 'rejectedFilter'],
      'trade/orders': ['order', 'orderFilter'],
    };
    const f = tabFilter[`${_state.activeTab}/${_state.activeSub}`];
    if (f && _state[f[1]] && _state[f[1]] !== 'all') qs.push('f=' + encodeURIComponent(_state[f[1]]));
    if (qs.length) hash += '?' + qs.join('&');
    return hash;
  }
  function applyHash(hash) {
    // 解析 #/tab/sub?s=AAPL&f=confirm
    if (!hash || hash === '#' || hash === '#/') return false;
    let h = hash.replace(/^#\/?/, '');
    let qs = '';
    const qIdx = h.indexOf('?');
    if (qIdx >= 0) { qs = h.slice(qIdx+1); h = h.slice(0, qIdx); }
    const parts = h.split('/');
    const tab = parts[0]; const sub = parts[1] || TAB_DEFAULT_SUB[tab];
    if (!tab || !TAB_DEFAULT_SUB[tab]) return false;
    // 解析 query
    const params = new URLSearchParams(qs);
    const sym = params.get('s');
    const fv = params.get('f');
    _state.symbolFilter = sym || null;
    // 找 tab/sub 对应的 filter state key
    const tabFilter = {
      'market/signals': 'signalFilter',
      'market/news': 'newsFilter',
      'market/pool': 'poolFilter',
      'trade/rejected': 'rejectedFilter',
      'trade/orders': 'orderFilter',
    };
    const fkey = tabFilter[`${tab}/${sub}`];
    if (fkey) _state[fkey] = fv || 'all';
    _state.activeSub = sub;
    return { tab, sub };
  }
  function syncHash() {
    if (_hashSyncing) return;
    const newHash = buildHash();
    if (location.hash !== newHash) {
      _hashSyncing = true;
      try { history.replaceState(null, '', newHash); } catch { location.hash = newHash; }
      setTimeout(() => { _hashSyncing = false; }, 30);
    }
  }
  window.addEventListener('hashchange', () => {
    if (_hashSyncing) return;
    const r = applyHash(location.hash);
    if (r) {
      _hashSyncing = true;
      switchTab(r.tab);
      setTimeout(() => { _hashSyncing = false; }, 30);
    }
  });

  function navigate(tab, sub = null, opts = {}) {
    // 关闭当前抽屉
    if (typeof closeSheet === 'function') closeSheet();
    // v12.27.8 Bug 2 修: 旧 tab/sub 路径自动转换为 v3 4-tab 路径
    //   旧: navigate('market', 'news') → 新: navigate('opp', 'news')
    //   旧: navigate('learn', 'reviews') → 新: navigate('insights', 'reviews')
    //   旧: navigate('trade', 'spot') → 新: navigate('holdings', 'all')
    if (tab && sub) {
      const legacyKey = `${tab}/${sub}`;
      if (LEGACY_TAB_REDIRECT[legacyKey]) {
        [tab, sub] = LEGACY_TAB_REDIRECT[legacyKey];
      }
    } else if (tab && !sub) {
      // 仅 tab 没 sub: 旧 tab → 新 tab
      const legacyTabMap = { home: 'now', market: 'opp', trade: 'holdings', learn: 'insights' };
      if (legacyTabMap[tab]) tab = legacyTabMap[tab];
    }
    // 应用 symbolFilter (跨页锁定 symbol)
    _state.symbolFilter = opts.symbolFilter || null;
    // 应用 chip filter
    if (opts.filter && opts.filter.key && opts.filter.value) {
      const stateKey = FILTER_STATE_KEY[opts.filter.key];
      if (stateKey) _state[stateKey] = opts.filter.value;
    }
    // 设 sub (在 switchTab 之前, switchTab 会用 _state.activeSub)
    if (sub) _state.activeSub = sub;
    switchTab(tab);
    // chip UI active 状态同步 (要等 DOM 切完)
    if (opts.filter && opts.filter.key && opts.filter.value) {
      const attr = FILTER_CHIP_ATTR[opts.filter.key];
      setTimeout(() => {
        document.querySelectorAll(`.chip[${attr}]`).forEach(c => {
          c.classList.toggle('active', c.getAttribute(attr) === opts.filter.value);
        });
      }, 80);
    }
    // v12.25.3 Phase D: 同步 hash
    setTimeout(syncHash, 100);
  }
  // 暴露给开发者控制台调试
  window._mobileNav = navigate;

  // ─── Main tab 切换 (5 个底部 tab) ───
  $$('.tab').forEach(tab => {
    tab.addEventListener('click', () => switchTab(tab.dataset.tab));
  });

  function switchTab(name) {
    _state.activeTab = name;
    // v12.25.3: 如果 _state.activeSub 已被 navigate/applyHash 设过, 保留; 否则用 default
    if (!_state.activeSub || !$(`.subpage[data-subpage="${_state.activeSub}"]`)) {
      _state.activeSub = TAB_DEFAULT_SUB[name] || 'home';
    }
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
    if (typeof syncHash === 'function') setTimeout(syncHash, 50);
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
    if (typeof syncHash === 'function') setTimeout(syncHash, 50);
  }

  function updatePageTitle() {
    // v12.27.0: 旧 hdr 已隐藏, 此函数在 v3 框架下 noop
    const el = $('#page-title');
    if (!el) return;
    const key = `${_state.activeTab}/${_state.activeSub}`;
    el.textContent = SUB_TITLES[key] || PAGE_TITLES[_state.activeTab] || _state.activeTab;
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
  // v12.27.9: 订单流水 按市场 filter
  $$('.chip[data-omfilter]').forEach(chip => {
    chip.addEventListener('click', () => {
      _state.orderMarketFilter = chip.dataset.omfilter;
      $$('.chip[data-omfilter]').forEach(c => c.classList.toggle('active', c === chip));
      renderOrderFlow();
    });
  });

  // ─── 设置页交互 (v12.27.6: 全部加 null guard, 旧 ID 在 v3 框架可能不存在) ───
  // v12.27.0 后这些 control 移到右上 ⚙ 设置 sheet
  // 但旧 render 函数内部可能动态注入这些元素, 所以保留 listener 但安全引用
  function _bindSafe(selector, event, handler) {
    const el = $(selector);
    if (el) el.addEventListener(event, handler);
  }
  _bindSafe('#auto-trade-toggle', 'change', async (e) => {
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
  _bindSafe('#tg-test-btn', 'click', async () => {
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
  _bindSafe('#summary-now-btn', 'click', async () => {
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

  // ─── 刷新按钮 (v12.27.0 移到 .statusbar 的 #refresh-btn) ───
  _bindSafe('#refresh-btn', 'click', () => {
    Object.keys(_state.cache).forEach(k => _state.cache[k] = null);
    const btn = $('#refresh-btn');
    if (btn) btn.classList.add('spinning');
    setTimeout(() => { if (btn) btn.classList.remove('spinning'); }, 600);
    refresh();
  });

  // ─── v12.27.0 浮动 ⚡ 按钮 (任何 tab 都能调出按需分析) ───
  const fabBtn = $('#fab');
  if (fabBtn) fabBtn.addEventListener('click', () => {
    // Phase 6 改为 sheet 弹按需分析表单; 现在跳转到 opp/ondemand
    if (typeof navigate === 'function') navigate('opp', 'ondemand');
  });

  // ─── v12.27.0 设置图标 ⚙ (右上) ───
  const settingsIcon = $('#settings-icon');
  if (settingsIcon) settingsIcon.addEventListener('click', () => {
    // Phase 6 改为完整 sheet; 现在简单弹一个选项 sheet
    if (typeof openSheet !== 'function') return;
    openSheet('设置', `
      <div class="kv-list">
        <div class="kv-row" data-go-set="auto"><span class="k">⚙️ 自动交易控制</span><span class="v">›</span></div>
        <div class="kv-row" data-go-set="notify"><span class="k">🔔 通知 (Telegram)</span><span class="v">›</span></div>
        <div class="kv-row" data-go-set="llm"><span class="k">💰 LLM 配额</span><span class="v">›</span></div>
        <div class="kv-row" data-go-set="system"><span class="k">📊 系统状态</span><span class="v">›</span></div>
      </div>
    `);
    // 设置子项点击 → 跳到旧 settings/* 路由 (Phase 6 改为完整 sheet 内容)
    setTimeout(() => {
      $$('#sheet-content [data-go-set]').forEach(el => {
        el.style.cursor = 'pointer';
        el.style.padding = '10px';
        el.style.borderBottom = '1px solid var(--bd)';
        el.addEventListener('click', () => {
          const target = el.dataset.goSet;
          if (target === 'auto' && typeof navigate === 'function') {
            closeSheet();
            // 旧 trade/control 路径仍然可用
            _state.activeTab = 'trade';
            _state.activeSub = 'control';
            // 强制走旧路由
            if (TAB_RENDERERS['trade/control']) TAB_RENDERERS['trade/control']();
          } else if (target && typeof navigate === 'function') {
            closeSheet();
            _state.activeTab = 'settings';
            _state.activeSub = target;
            if (TAB_RENDERERS['settings/' + target]) TAB_RENDERERS['settings/' + target]();
          }
        });
      });
    }, 100);
  });

  // ─── v12.27.0 状态条 数据填充 ───
  function updateStatusBar() {
    // status (running/error)
    const dot = $('#sb-status-dot');
    const text = $('#sb-status-text');
    // auto trade state
    const auto = $('#sb-auto-trade');
    if (auto && _state.cache.status) {
      const enabled = _state.cache.status.enabled;
      auto.textContent = enabled ? '⚡ 自动开' : '🔴 自动关';
      auto.className = 'sb-item' + (enabled ? '' : ' warn');
    }
    // llm budget
    const budget = $('#sb-llm-budget');
    if (budget && _state.cache.llmCost) {
      // v12.27.2: API 字段是 today_cost_usd, 不是 today_total_usd (历史 bug 修复)
      const used = _state.cache.llmCost.today_cost_usd || _state.cache.llmCost.today_total_usd || 0;
      const total = _state.cache.llmCost.daily_budget || 25;
      const pct = (used / total * 100);
      budget.textContent = `💰 $${used.toFixed(2)} / $${total}`;
      budget.className = 'sb-item' + (pct > 80 ? ' warn' : '');
    }
  }
  // 每次 refresh 后更新
  setInterval(updateStatusBar, 3000);

  // ─── v12.27.0 持仓 chip 切换 ───
  $$('.chip[data-h-filter]').forEach(chip => {
    chip.addEventListener('click', () => {
      _state.holdingsFilter = chip.dataset.hFilter;
      $$('.chip[data-h-filter]').forEach(c => c.classList.toggle('active', c === chip));
      if (typeof renderHoldings === 'function') renderHoldings();
    });
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
    // v12.21.2 hotfix: limit 200 → 2000 (与桌面端 v12.20.14 修复对齐)
    // 之前 200 截断导致手机看到的总数刚好 200 (美 31+A 108+港 61),漏 550 只低分股
    // API 上限已在 v12.20.14 改 le=2000 (main.py:2336)
    _state.cache.pool = await fetchJSON('/api/pool?limit=2000');
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
      const todayCost = (llmCost && (llmCost.today_cost_usd || llmCost.today_total_usd)) || 0;
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
      let items = data.items || [];
      // v12.25.0: 跨页 symbol 锁定 — 在标题/symbols 字段里搜
      if (_state.symbolFilter) {
        const sf = _state.symbolFilter.toUpperCase();
        items = items.filter(n => {
          const t = (n.title || '').toUpperCase();
          const s = (n.symbols || []).map(x => String(x).toUpperCase());
          return t.includes(sf) || s.includes(sf);
        });
      }
      const hintHTML = _state.symbolFilter
        ? `<div class="filter-hint">🔗 仅显示 <b>${escape(_state.symbolFilter)}</b> 的新闻 <span class="clear-hint" style="cursor:pointer;color:#5b9eff;">× 清除</span></div>`
        : '';
      if (!items.length) {
        $('#news-list').innerHTML = hintHTML + '<div class="empty"><div class="empty-icon">📰</div><div>暂无新闻</div></div>';
      } else {
        $('#news-list').innerHTML = hintHTML + items.map(renderNewsCard).join('');
      }
      $$('#news-list .news-card').forEach(card => {
        card.addEventListener('click', () => openNewsDetail(card.dataset.id));
      });
      const clearH = $('#news-list .clear-hint');
      if (clearH) clearH.addEventListener('click', () => { _state.symbolFilter = null; renderNews(); });
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
      // v12.25.0: 跨页 symbol 锁定
      if (_state.symbolFilter) {
        items = items.filter(p => p.symbol === _state.symbolFilter);
      }
      $('#pool-count').textContent = items.length;
      const hintHTML = _state.symbolFilter
        ? `<div class="filter-hint">🔗 仅显示 <b>${escape(_state.symbolFilter)}</b> <span class="clear-hint" style="cursor:pointer;color:#5b9eff;">× 清除</span></div>`
        : '';
      if (!items.length) {
        $('#pool-list').innerHTML = hintHTML + '<div class="empty"><div class="empty-icon">🎯</div><div>无候选池条目</div></div>';
        return;
      }
      $('#pool-list').innerHTML = hintHTML + items.map(renderPoolRow).join('');
      $$('#pool-list .pool-row').forEach(r => {
        r.addEventListener('click', () => openPoolDetail(r.dataset.id));
      });
      const clearH = $('#pool-list .clear-hint');
      if (clearH) clearH.addEventListener('click', () => { _state.symbolFilter = null; renderPool(); });
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
        <div class="sheet-actions" style="display:flex;flex-wrap:wrap;gap:6px;">
          <button class="btn btn-link" data-go-pool-news>📰 该股新闻</button>
          <button class="btn btn-link" data-go-pool-signals>🎯 历史信号</button>
          <button class="btn btn-link" data-go-pool-reviews>📊 历史复盘</button>
        </div>
      `;
      $('#sheet-content').innerHTML = html;
      // v12.25.0: 候选池详情跳转
      const sym = item.symbol;
      const goPN = $('#sheet-content [data-go-pool-news]');
      if (goPN) goPN.addEventListener('click', () => navigate('market', 'news', { symbolFilter: sym }));
      const goPS = $('#sheet-content [data-go-pool-signals]');
      if (goPS) goPS.addEventListener('click', () => navigate('market', 'signals', { symbolFilter: sym }));
      const goPR = $('#sheet-content [data-go-pool-reviews]');
      if (goPR) goPR.addEventListener('click', () => navigate('learn', 'reviews', { symbolFilter: sym }));
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
        else if (f === 'verifying') items = items.filter(s => !s.ai_verdict || (s.ai_verdict === 'confirm' && (!s.revalidated_at || s.revalidated_at == 0)));
        else items = items.filter(s => s.ai_verdict === f);
      }
      // v12.25.0: 跨页 symbol 锁定
      if (_state.symbolFilter) {
        items = items.filter(s => s.symbol === _state.symbolFilter);
      }
      // 顶部 symbolFilter 提示条
      const hintHTML = _state.symbolFilter
        ? `<div class="filter-hint">🔗 仅显示 <b>${escape(_state.symbolFilter)}</b> 的信号 <span class="clear-hint" style="cursor:pointer;color:#5b9eff;">× 清除</span></div>`
        : '';
      if (!items.length) {
        $('#signals-list').innerHTML = hintHTML + '<div class="empty"><div class="empty-icon">📡</div><div>无信号</div></div>';
      } else {
        $('#signals-list').innerHTML = hintHTML + items.slice(0, 100).map(renderSignalRow).join('');
      }
      $$('#signals-list .row').forEach(r => {
        r.addEventListener('click', () => openSignalDetail(r.dataset.id));
      });
      const clearH = $('#signals-list .clear-hint');
      if (clearH) clearH.addEventListener('click', () => { _state.symbolFilter = null; renderSignals(); });
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
        <div class="sheet-actions" style="display:flex;flex-wrap:wrap;gap:6px;">
          ${pool ? `<button class="btn btn-link" data-go-pool>🎯 候选池档案</button>` : ''}
          <button class="btn btn-link" data-go-news-sig>📰 该股新闻</button>
          <button class="btn btn-link" data-go-reviews-sig>📊 历史复盘</button>
        </div>
      `;
      $('#sheet-content').innerHTML = html;
      // v12.25.0: 跳转按钮
      const goPool = $('#sheet-content [data-go-pool]');
      if (goPool && pool) goPool.addEventListener('click', () => {
        navigate('market', 'pool', { symbolFilter: s.symbol });
        // 自动打开该 symbol 的池详情 (如有 id)
        if (pool && pool.id) setTimeout(() => openPoolDetail(pool.id), 200);
      });
      const goNewsSig = $('#sheet-content [data-go-news-sig]');
      if (goNewsSig) goNewsSig.addEventListener('click', () => navigate('market', 'news', { symbolFilter: s.symbol }));
      const goRevSig = $('#sheet-content [data-go-reviews-sig]');
      if (goRevSig) goRevSig.addEventListener('click', () => navigate('learn', 'reviews', { symbolFilter: s.symbol }));
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
  // v12.21.4 PR1 回归修: 接入 _state.cache (之前每次都拉新, 30s refresh 浪费 3 个 API)
  async function loadSwapAccount() {
    if (_state.cache.swapAcct) return _state.cache.swapAcct;
    try {
      const r = await fetchJSON('/api/swap/account');
      _state.cache.swapAcct = r;
      return r;
    } catch (e) { return null; }
  }
  async function loadSwapPositions() {
    if (_state.cache.swapPos) return _state.cache.swapPos;
    try {
      const r = await fetchJSON('/api/swap/positions?status=open');
      _state.cache.swapPos = r.items || [];
      return _state.cache.swapPos;
    } catch (e) { return []; }
  }
  async function loadSwapOrders(status) {
    // 注: status 参数变化时 cache key 应变 — 当前调用方都不传 status (默认 all),共用 cache OK
    if (_state.cache.swapOrders && !status) return _state.cache.swapOrders;
    try {
      const r = await fetchJSON('/api/swap/orders' + (status ? '?status=' + status : ''));
      const items = r.items || [];
      if (!status) _state.cache.swapOrders = items;
      return items;
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
        ${((p.qty||0) * (p.contract_size||0.01)).toFixed(6)} 个 @ ${(p.avg_open_price||0).toFixed(4)} · 保证金 ${fmtMoney(p.margin_usd||0)}
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
      let items = filterMkt === 'all'
        ? allRejected
        : allRejected.filter(t => t.market === filterMkt);
      // v12.25.0: 跨页 symbol 锁定
      if (_state.symbolFilter) {
        items = items.filter(t => t.symbol === _state.symbolFilter);
      }
      const hintHTML = _state.symbolFilter
        ? `<div class="filter-hint">🔗 仅显示 <b>${escape(_state.symbolFilter)}</b> 的拒单 <span class="clear-hint" style="cursor:pointer;color:#5b9eff;">× 清除</span></div>`
        : '';

      if (!items.length) {
        $('#rejected-list').innerHTML = hintHTML + `<div class="empty">
          <div class="empty-icon">⊘</div>
          <div>无拒单记录</div>
          <div class="small" style="margin-top:6px;">${filterMkt === 'all' ? '系统未拒过单' : '该市场无拒单'}</div>
        </div>`;
        return;
      }
      // v12.25.0 Phase B: 拒单卡可点击 → 详情抽屉
      $('#rejected-list').innerHTML = hintHTML + items.slice(0, 80).map((t, idx) => {
        const action = ACTION_LABEL[t.action] || t.action;
        const reason = t.rejected_reason || t.reason || '';
        return `<div class="row warn rejected-row" data-idx="${idx}" style="cursor:pointer;">
          <div class="row-title">
            <div class="row-symbol">${escape(t.symbol)} <span class="small muted">${MARKET_LABEL[t.market]||t.market}</span></div>
            <div class="row-time">${fmtRelTime(t.traded_at*1000)}</div>
          </div>
          <div class="row-meta small">尝试 ${action}</div>
          <div class="row-reason">⊘ ${escape(reason)}</div>
        </div>`;
      }).join('');
      // 缓存 items 给 openRejectedDetail 用 (避免重新过滤)
      _state._rejectedSnapshot = items.slice(0, 80);
      $$('#rejected-list .rejected-row').forEach(r => {
        r.addEventListener('click', () => openRejectedDetail(parseInt(r.dataset.idx)));
      });
      const clearH = $('#rejected-list .clear-hint');
      if (clearH) clearH.addEventListener('click', () => { _state.symbolFilter = null; renderRejected(); });
    } catch (e) {
      console.error(e);
      $('#rejected-list').innerHTML = '<div class="empty">加载失败</div>';
    }
  }

  // v12.25.0 Phase B: 拒单详情抽屉
  // 显示: 完整拒单原因 + 关联信号 (如 trigger_detail.signal_id) + 同 symbol 24h 同类拒单趋势
  async function openRejectedDetail(idx) {
    openSheet('拒单详情', '<div class="empty">加载中…</div>');
    try {
      const items = _state._rejectedSnapshot || [];
      const t = items[idx];
      if (!t) { $('#sheet-content').innerHTML = '<div class="empty">未找到</div>'; return; }
      // 解析 trigger_detail (可能是 JSON string 或 object)
      let trig = t.trigger_detail;
      if (typeof trig === 'string') { try { trig = JSON.parse(trig); } catch { trig = {}; } }
      trig = trig || {};
      const sigId = trig.signal_id || trig.sig_id;
      // 同 symbol 24h 拒单数
      const log = await loadHistory();
      const cutoff = (Date.now()/1000) - 86400;
      const sameSymRejects = (log.items||[]).filter(x =>
        x.status === 'rejected' && x.symbol === t.symbol && x.traded_at >= cutoff
      );
      const action = ACTION_LABEL[t.action] || t.action;
      const reason = t.rejected_reason || t.reason || '';
      const html = `
        <h4>拒单基本信息</h4>
        <div class="kv-row"><span class="k">代码 / 市场</span><span class="v">${escape(t.symbol)} · ${MARKET_LABEL[t.market]||t.market}</span></div>
        <div class="kv-row"><span class="k">尝试动作</span><span class="v">${action}</span></div>
        <div class="kv-row"><span class="k">时间</span><span class="v">${fmtTime(t.traded_at)}</span></div>
        <div class="kv-row"><span class="k">触发类型</span><span class="v small">${escape(t.trigger_type || '')}</span></div>
        <h4>⊘ 拒单原因</h4>
        <div style="background:rgba(239,68,68,0.08);border-left:3px solid var(--down);padding:8px 10px;border-radius:4px;">${escape(reason)}</div>
        ${trig && Object.keys(trig).length ? `<h4>触发上下文</h4>
        <pre style="white-space:pre-wrap;font-size:11px;background:var(--bg-card-2);padding:8px;border-radius:6px;">${escape(JSON.stringify(trig, null, 2))}</pre>` : ''}
        <h4>📊 该股 24h 拒单趋势</h4>
        <div class="kv-row"><span class="k">同 symbol 拒单数</span><span class="v ${sameSymRejects.length>=3?'down':''}">${sameSymRejects.length}</span></div>
        ${sameSymRejects.length >= 3 ? `<div class="muted small">⚠️ 该股 24h 内被拒 ${sameSymRejects.length} 次, 信号质量可能存在系统性问题</div>` : ''}
        <div class="sheet-actions" style="display:flex;flex-wrap:wrap;gap:6px;">
          ${sigId ? `<button class="btn btn-link" data-go-rj-signal>🎯 查看信号详情</button>` : ''}
          <button class="btn btn-link" data-go-rj-news>📰 该股新闻</button>
          <button class="btn btn-link" data-go-rj-pool>🎯 候选池</button>
        </div>
      `;
      $('#sheet-content').innerHTML = html;
      const goSig = $('#sheet-content [data-go-rj-signal]');
      if (goSig && sigId) goSig.addEventListener('click', () => {
        closeSheet();
        setTimeout(() => openSignalDetail(sigId), 100);
      });
      const goN = $('#sheet-content [data-go-rj-news]');
      if (goN) goN.addEventListener('click', () => navigate('market', 'news', { symbolFilter: t.symbol }));
      const goP = $('#sheet-content [data-go-rj-pool]');
      if (goP) goP.addEventListener('click', () => navigate('market', 'pool', { symbolFilter: t.symbol }));
    } catch (e) {
      $('#sheet-content').innerHTML = `<div class="empty">加载失败：${e.message}</div>`;
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
      // v12.25.2 Phase C: 拉持仓全生命周期 + AI 建议历史
      const [lifecycle, allAdvices] = await Promise.all([
        fetchJSON('/api/auto-trade/log?position_id=' + encodeURIComponent(id) + '&limit=200').catch(()=>({items:[]})),
        fetchJSON('/api/positions/' + encodeURIComponent(id) + '/advices').catch(()=>({items:[]})),
      ]);
      const lifeEvents = lifecycle.items || [];
      const adviceHistory = allAdvices.items || allAdvices || [];
      const ccy = p.cost_currency || 'USD';
      // v12.26.3 P2-E: 时间线分组显示 — 真发生 vs 仅观察
      // 已执行 (executed) — 绿色边, 真减仓/平仓
      // 闭市拒单 (rejected + 含"连续竞价") — 灰色边, 仅观察 (闭市生成被拦)
      // 其他拒单 (rejected + 其他原因) — 黄色边, 真拒单
      const execEvents = lifeEvents.filter(ev => ev.status === 'executed');
      const closedMktEvents = lifeEvents.filter(ev =>
        ev.status === 'rejected' && /连续竞价|pending|减仓延后/.test(ev.rejected_reason || '')
      );
      const otherRejectEvents = lifeEvents.filter(ev =>
        ev.status === 'rejected' && !/连续竞价|pending|减仓延后/.test(ev.rejected_reason || '')
      );
      function _renderEv(ev, kind) {
        const action = ACTION_LABEL[ev.action] || ev.action;
        const isOpen = ev.action === 'open';
        const isClose = ev.action === 'close';
        const isAdd = ev.action === 'add';
        const isReduce = ev.action === 'reduce';
        const icon = isOpen ? '🟢' : isAdd ? '➕' : isReduce ? '➖' : isClose ? '🔴' : '•';
        const reason = ev.reason || ev.rejected_reason || '';
        const colors = { exec: '#10b981', observe: '#6b7280', reject: '#fbbf24' };
        const tag = { exec: '✅ 已执行', observe: '👁️ 仅观察 (闭市)', reject: '⊘ 拒单' };
        return `<div class="lifecycle-event" style="border-left:3px solid ${colors[kind]};padding:6px 10px;margin:6px 0;background:var(--bg-card-2);border-radius:4px;${kind==='observe'?'opacity:0.7;':''}">
          <div style="display:flex;justify-content:space-between;font-size:12px;">
            <span><b>${icon} ${action}</b> ${ev.quantity||0} @ ${(ev.price||0).toFixed(4)} <span class="small muted">${tag[kind]}</span></span>
            <span class="small muted">${fmtTime(ev.traded_at)}</span>
          </div>
          ${reason ? `<div class="small muted" style="margin-top:3px;">${escape(reason).slice(0,120)}</div>` : ''}
        </div>`;
      }
      const timelineHTML = lifeEvents.length ? `
        ${execEvents.length ? `<div class="small muted" style="margin:8px 0 4px;">✅ 真发生的操作 (${execEvents.length})</div>${execEvents.map(ev => _renderEv(ev, 'exec')).join('')}` : ''}
        ${closedMktEvents.length ? `<div class="small muted" style="margin:8px 0 4px;">👁️ 闭市观察记录 (${closedMktEvents.length}, 系统尝试但未执行)</div>${closedMktEvents.map(ev => _renderEv(ev, 'observe')).join('')}` : ''}
        ${otherRejectEvents.length ? `<div class="small muted" style="margin:8px 0 4px;">⊘ 其他拒单 (${otherRejectEvents.length})</div>${otherRejectEvents.map(ev => _renderEv(ev, 'reject')).join('')}` : ''}
      ` : '<div class="muted small">无历史记录</div>';
      // AI 建议历史 HTML
      const adviceHistHTML = adviceHistory.length ? adviceHistory.slice(0, 8).map(a => {
        const cn = ADVICE_LABEL_CN[a.advice] || a.advice || '?';
        const cls = (a.advice === 'close' || a.advice === 'reduce') ? 'down' : (a.advice === 'add' ? 'up' : '');
        return `<div style="border-left:2px solid var(--border);padding:4px 8px;margin:4px 0;background:var(--bg-card-2);border-radius:4px;">
          <div style="display:flex;justify-content:space-between;font-size:12px;">
            <span class="${cls}">🤖 ${escape(cn)}</span>
            <span class="small muted">${fmtRelTime(a.advised_at*1000)}</span>
          </div>
          ${a.reason ? `<div class="small muted" style="margin-top:3px;">${escape(a.reason).slice(0,100)}</div>` : ''}
        </div>`;
      }).join('') : '<div class="muted small">无历史建议</div>';
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
        <h4>📊 完整时间线 (${lifeEvents.length} 事件)</h4>
        ${timelineHTML}
        <h4>🤖 AI 建议历史 (${adviceHistory.length} 条)</h4>
        ${adviceHistHTML}
        <h4>🤖 最新 AI 建议</h4>
        ${advice && advice.advice
          ? `<div class="kv-row"><span class="k">建议</span><span class="v">${escape(ADVICE_LABEL_CN[advice.advice] || advice.advice)}</span></div>
             ${advice.reason ? `<div style="margin:4px 0;">${escape(advice.reason)}</div>` : ''}
             <div class="kv-row"><span class="k">时间</span><span class="v small">${fmtTime(advice.advised_at)}</span></div>`
          : '<div class="muted">暂无建议</div>'}
        <div class="sheet-actions" style="display:flex;flex-wrap:wrap;gap:6px;">
          <button class="btn" id="advise-now-btn">🤖 重新分析</button>
          <button class="btn btn-link" data-go-news>📰 该股新闻</button>
          <button class="btn btn-link" data-go-reviews>📊 历史复盘</button>
          <button class="btn btn-link" data-go-signals>🎯 历史信号</button>
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
      // v12.25.0: 跨页跳转按钮
      const sym = p.symbol;
      const goNews = $('#sheet-content [data-go-news]');
      if (goNews) goNews.addEventListener('click', () => navigate('market', 'news', { symbolFilter: sym }));
      const goReviews = $('#sheet-content [data-go-reviews]');
      if (goReviews) goReviews.addEventListener('click', () => navigate('learn', 'reviews', { symbolFilter: sym }));
      const goSignals = $('#sheet-content [data-go-signals]');
      if (goSignals) goSignals.addEventListener('click', () => navigate('market', 'signals', { symbolFilter: sym }));
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
      let items = data.items || [];
      // v12.25.0: 跨页 symbol 锁定
      if (_state.symbolFilter) {
        items = items.filter(r => r.symbol === _state.symbolFilter);
      }
      const hintHTML = _state.symbolFilter
        ? `<div class="filter-hint">🔗 仅显示 <b>${escape(_state.symbolFilter)}</b> 的复盘 <span class="clear-hint" style="cursor:pointer;color:#5b9eff;">× 清除</span></div>`
        : '';
      if (!items.length) {
        $('#review-list').innerHTML = hintHTML + '<div class="empty"><div class="empty-icon">🔍</div><div>暂无复盘</div><div class="small" style="margin-top:6px;">闭环交易后台自动生成</div></div>';
      } else {
        $('#review-list').innerHTML = hintHTML + items.map(renderReviewRow).join('');
      }
      $$('#review-list .row').forEach(r => {
        r.addEventListener('click', () => openReviewDetail(r.dataset.pid));
      });
      const clearH = $('#review-list .clear-hint');
      if (clearH) clearH.addEventListener('click', () => { _state.symbolFilter = null; renderReviewList(); });
    } catch (e) {
      console.error(e);
      $('#review-list').innerHTML = '<div class="empty">加载失败</div>';
    }
  }

  // v12.27.9: 把 LLM 教训对象/字符串还原成可读文本
  //   后端 reviewer.py 提示 lesson 是 {type, content} 对象
  //   也兼容旧格式纯字符串 + LLM 偶尔输出的其他字段名 (text/summary/lesson)
  function _fmtLessonItem(x) {
    if (typeof x === 'string') return escape(x);
    if (x && typeof x === 'object') {
      const content = x.content || x.text || x.summary || x.lesson || x.pattern || '';
      const type = x.type ? `<span class="muted small">[${escape(x.type)}]</span> ` : '';
      if (content) return type + escape(String(content));
      // 没有可识别字段时, 显示所有字段值拼接
      const vals = Object.values(x).filter(v => typeof v === 'string' || typeof v === 'number');
      if (vals.length) return escape(vals.join(' · '));
      return '<span class="muted small">' + escape(JSON.stringify(x).slice(0,80)) + '</span>';
    }
    return escape(String(x));
  }
  // 通用文本项 (pros/cons 偶尔也是对象)
  function _fmtTextItem(x) {
    if (typeof x === 'string') return escape(x);
    if (x && typeof x === 'object') {
      const txt = x.content || x.text || x.summary || x.note || '';
      if (txt) return escape(String(txt));
      return escape(JSON.stringify(x).slice(0, 80));
    }
    return escape(String(x));
  }

  function renderReviewRow(r) {
    const grade = (r.grade || '').toUpperCase();
    // v12.27.8 Bug 3 修: trade_review 表只有 realized_pnl_local + realized_pnl_pct
    //   旧代码用 realized_pnl_usd (字段不存在) → 永远 0 → 永远 'up' (绿) → 颜色错乱
    //   现按 pnl_pct 决定颜色 (准确), 显示用 realized_pnl_local
    const pnlPct = r.realized_pnl_pct || 0;
    const pnlLocal = r.realized_pnl_local != null ? r.realized_pnl_local : (r.realized_pnl_usd || 0);
    const ccy = (r.market === 'cn') ? 'CNY' : (r.market === 'hk') ? 'HKD' : 'USD';
    const cls = pnlPct > 0.01 ? 'up' : (pnlPct < -0.01 ? 'down' : 'muted');
    const lessonsArr = Array.isArray(r.lessons) ? r.lessons : [];
    // v12.20.9: swap 复盘加 ⚡ 徽章 + 杠杆/funding/强平标记
    const isSwap = r.is_swap == 1 || r.is_swap === true;
    let swapBadges = '';
    if (isSwap) {
      const sideTxt = r.swap_pos_side === 'long' ? '🟢多' : (r.swap_pos_side === 'short' ? '🔴空' : '');
      const lev = r.swap_leverage ? `${r.swap_leverage}x` : '';
      const liqBadge = r.swap_liquidated ? '<span style="background:rgba(248,81,73,0.18);color:var(--down);padding:1px 5px;border-radius:6px;font-size:10px;">💀强平</span>' : '';
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
        <div class="row-pnl ${cls}">${fmtPnl(pnlLocal, ccy)} (${fmtPct(pnlPct)})</div>
      </div>
      <div class="row-meta">
        持仓 ${Math.round((r.hold_hours || (r.holding_seconds||0)/3600))} h · ${fmtTime(r.close_at)}
        ${isSwap && r.swap_funding_total ? ' · funding ' + fmtPnl(r.swap_funding_total) : ''}
      </div>
      ${lessonsArr.length ? `<div class="row-reason">📌 ${lessonsArr.slice(0,2).map(x => {
        if (typeof x === 'string') return escape(x);
        if (x && typeof x === 'object') return escape(x.content || x.text || x.summary || JSON.stringify(x).slice(0,40));
        return escape(String(x));
      }).join(' / ')}</div>` : ''}
    </div>`;
  }

  async function openReviewDetail(pid) {
    openSheet('复盘详情', '<div class="empty">加载中…</div>');
    try {
      const r = await fetchJSON('/api/trade-review/' + encodeURIComponent(pid));
      const grade = (r.grade || '').toUpperCase();
      // v12.27.8 Bug 3: trade_review 没有 realized_pnl_usd 列, 用 realized_pnl_local + 货币
      const pnl = r.realized_pnl_local != null ? r.realized_pnl_local : (r.realized_pnl_usd || 0);
      const ccy = (r.market === 'cn') ? 'CNY' : (r.market === 'hk') ? 'HKD' : 'USD';
      const pnlPct = r.realized_pnl_pct || 0;
      const pros = Array.isArray(r.pros) ? r.pros : [];
      const cons = Array.isArray(r.cons) ? r.cons : [];
      const lessons = Array.isArray(r.lessons) ? r.lessons : [];
      const turning = Array.isArray(r.turning_points) ? r.turning_points : [];
      // v12.21.3 PR2: 合约复盘加专属信息块 (杠杆/funding/手续费/强平)
      const isSwap = r.is_swap == 1 || r.is_swap === true;
      const swapBlock = isSwap ? `
        <div style="background:linear-gradient(135deg,rgba(167,139,250,0.15),rgba(167,139,250,0.03));border-left:4px solid #a78bfa;padding:12px 14px;border-radius:0 8px 8px 0;margin-bottom:14px;">
          <div style="color:#c4b5fd;font-weight:700;font-size:13px;margin-bottom:6px;">⚡ 加密永续合约 ${r.swap_pos_side === 'long' ? '🟢做多' : '🔴做空'} ${r.swap_leverage||'?'}x ${r.swap_liquidated ? '<span style="background:var(--down);color:#fff;padding:1px 5px;border-radius:3px;font-size:10px;margin-left:4px;">💀 强平</span>' : ''}</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:11px;">
            <div><span style="color:var(--text-3);">杠杆:</span> <strong>${r.swap_leverage||0}x</strong></div>
            <div><span style="color:var(--text-3);">${r.swap_liquidated ? '强平亏损' : '正常平仓'}</span></div>
            <div><span style="color:var(--text-3);">资金费累积:</span> <strong class="${(r.swap_funding_total||0)>=0?'up':'down'}">${fmtPnl(r.swap_funding_total||0)}</strong></div>
            <div><span style="color:var(--text-3);">手续费累积:</span> <strong class="down">-${fmtMoney(r.swap_total_fee||0)}</strong></div>
          </div>
        </div>` : '';
      const html = `
        ${swapBlock}
        <h4>结果</h4>
        <div class="kv-row"><span class="k">代码</span><span class="v">${escape(r.symbol)}${isSwap ? ' <span style="background:#3d2f5a;color:#c4b5fd;padding:1px 4px;border-radius:3px;font-size:9px;margin-left:4px;">⚡合约</span>' : ''}</span></div>
        <div class="kv-row"><span class="k">评级</span><span class="v"><span class="grade-pill ${grade}">${grade||'?'}</span> ${GRADE_LABEL[grade]||''}</span></div>
        <div class="kv-row"><span class="k">实现盈亏</span><span class="v ${pnlPct>0.01?'up':(pnlPct<-0.01?'down':'muted')}">${fmtPnl(pnl, ccy)} (${fmtPct(pnlPct)})</span></div>
        <div class="kv-row"><span class="k">持仓时长</span><span class="v">${Math.round(r.hold_hours || (r.holding_seconds||0)/3600)} h</span></div>
        <div class="kv-row"><span class="k">闭环时间</span><span class="v">${fmtTime(r.close_at)}</span></div>
        ${r.summary ? `<h4>总结</h4><div>${escape(r.summary)}</div>` : ''}
        ${r.primary_lesson ? `<h4>🎯 核心教训</h4><div>${escape(r.primary_lesson)}</div>` : ''}
        ${r.what_if_better ? `<div class="muted small" style="margin-top:6px;">💭 若改进:${escape(r.what_if_better)}</div>` : ''}
        ${r.entry_analysis ? `<h4>📥 入场分析</h4><div>${escape(r.entry_analysis)}</div>` : ''}
        ${r.mid_analysis ? `<h4>⚙ 持仓中分析</h4><div>${escape(r.mid_analysis)}</div>` : ''}
        ${r.exit_analysis ? `<h4>📤 出场分析</h4><div>${escape(r.exit_analysis)}</div>` : ''}
        ${pros.length ? `<h4>👍 做对了什么</h4><ul>${pros.map(x=>`<li>${_fmtTextItem(x)}</li>`).join('')}</ul>` : ''}
        ${cons.length ? `<h4>👎 做错了什么</h4><ul>${cons.map(x=>`<li>${_fmtTextItem(x)}</li>`).join('')}</ul>` : ''}
        ${turning.length ? `<h4>关键转折点</h4><ul>${turning.map(x=>{
            const t = typeof x === 'object' ? `${escape(x.time||'')} ${escape(x.event||x.note||'')}` : escape(x);
            return `<li>${t}</li>`;
          }).join('')}</ul>` : ''}
        ${lessons.length ? `<h4>📌 教训</h4><ul style="cursor:pointer;" data-go-lessons>${lessons.map(x=>`<li>${_fmtLessonItem(x)}</li>`).join('')}</ul><div class="small muted" style="margin-top:-6px;">💡 点击教训列表可跳到教训库查看完整收录</div>` : ''}
        ${r.improvements ? `<h4>💡 改进建议</h4><div>${escape(r.improvements)}</div>` : ''}
        ${renderStrategyParamAnalysis(r.strategy_param_analysis)}
        <div class="sheet-actions" style="display:flex;flex-wrap:wrap;gap:6px;">
          <button class="btn btn-link" data-go-rev-news>📰 该股新闻</button>
          <button class="btn btn-link" data-go-rev-signals>🎯 历史信号</button>
          ${lessons.length ? `<button class="btn btn-link" data-go-rev-lessons>📌 教训库</button>` : ''}
        </div>
      `;
      $('#sheet-content').innerHTML = html;
      // v12.25.0: 跳转按钮
      const goRN = $('#sheet-content [data-go-rev-news]');
      if (goRN) goRN.addEventListener('click', () => navigate('market', 'news', { symbolFilter: r.symbol }));
      const goRS = $('#sheet-content [data-go-rev-signals]');
      if (goRS) goRS.addEventListener('click', () => navigate('market', 'signals', { symbolFilter: r.symbol }));
      const goRL = $('#sheet-content [data-go-rev-lessons]');
      if (goRL) goRL.addEventListener('click', () => navigate('learn', 'lessons'));
      const goLessonsList = $('#sheet-content [data-go-lessons]');
      if (goLessonsList) goLessonsList.addEventListener('click', () => navigate('learn', 'lessons'));
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
      // v12.25.0 Phase B: 教训卡可点击 → 详情
      _state._lessonsSnapshot = items;
      $$('#lessons-list .lesson-card').forEach((card, i) => {
        card.style.cursor = 'pointer';
        card.addEventListener('click', (e) => {
          if (e.target.classList.contains('lesson-adopt-btn')) return;  // 已绑 stopPropagation
          openLessonDetail(items[i] && items[i].id);
        });
      });
    } catch (e) {
      console.error(e);
      $('#lessons-list').innerHTML = '<div class="empty">加载失败</div>';
    }
  }

  // v12.25.0 Phase B: 教训详情抽屉
  async function openLessonDetail(lessonId) {
    openSheet('教训详情', '<div class="empty">加载中…</div>');
    try {
      const items = _state._lessonsSnapshot || [];
      const l = items.find(x => String(x.id) === String(lessonId));
      if (!l) { $('#sheet-content').innerHTML = '<div class="empty">未找到</div>'; return; }
      // 找该教训关联的复盘 (从最近 reviews 里搜 lessons 数组含本 pattern)
      const reviewsData = await loadReviews().catch(() => ({items: []}));
      const matchingReviews = (reviewsData.items || []).filter(r => {
        const ls = Array.isArray(r.lessons) ? r.lessons : [];
        return ls.some(x => String(x).includes(String(l.pattern || l.summary || '').slice(0,30)));
      }).slice(0, 10);
      const status = l.status || 'active';
      const score = parseFloat(l.adoption_score || 0);
      const hasParams = l.has_specific_params == 1 || l.has_specific_params === true;
      const showAdoptBtn = (status === 'active' && hasParams);
      const html = `
        <h4>📌 教训内容</h4>
        <div style="background:var(--bg-card-2);padding:10px;border-radius:6px;margin-bottom:10px;">${escape(l.pattern || l.summary || '—')}</div>
        <h4>统计</h4>
        <div class="kv-row"><span class="k">状态</span><span class="v">${POOL_STATUS_LABEL[status]||status}</span></div>
        <div class="kv-row"><span class="k">出现次数</span><span class="v">${l.occurrences||0}</span></div>
        <div class="kv-row"><span class="k">采纳分</span><span class="v">${score.toFixed(1)}</span></div>
        ${l.pool_id ? `<div class="kv-row"><span class="k">作用池</span><span class="v">${escape(l.pool_id)}</span></div>` : ''}
        ${l.worst_pnl_pct != null ? `<div class="kv-row"><span class="k">最差 PnL</span><span class="v down">${fmtPct(l.worst_pnl_pct)}</span></div>` : ''}
        ${l.first_seen_at ? `<div class="kv-row"><span class="k">首次出现</span><span class="v">${fmtTime(l.first_seen_at)}</span></div>` : ''}
        ${l.last_seen_at ? `<div class="kv-row"><span class="k">最近出现</span><span class="v">${fmtTime(l.last_seen_at)}</span></div>` : ''}
        <h4>能否转化为风控规则</h4>
        <div class="kv-row"><span class="k">含具体参数</span><span class="v">${hasParams?'✅ 是':'❌ 否 (仅 prompt 软规则)'}</span></div>
        ${matchingReviews.length ? `<h4>📊 触发的复盘 (${matchingReviews.length})</h4>
          <div id="lesson-rev-list">${matchingReviews.map((r, i) =>
            `<div class="row warn lesson-rev-item" data-pid="${r.position_id||r.id}" style="cursor:pointer;">
              <div class="row-symbol">${escape(r.symbol)} <span class="grade-pill ${(r.grade||'').toUpperCase()}">${(r.grade||'?').toUpperCase()}</span></div>
              <div class="row-meta small">${fmtPnl(r.realized_pnl_usd||0)} · ${fmtTime(r.close_at)}</div>
            </div>`).join('')}</div>` : '<div class="muted small">尚未在复盘中查到</div>'}
        <div class="sheet-actions" style="display:flex;flex-wrap:wrap;gap:6px;">
          ${showAdoptBtn ? `<button class="btn" id="lesson-detail-adopt">📥 采纳为风控规则</button>` : ''}
          ${status === 'adopted' ? `<button class="btn btn-link" data-go-rules>🛡️ 查看已生成规则</button>` : ''}
        </div>
      `;
      $('#sheet-content').innerHTML = html;
      const adoptBtn = $('#lesson-detail-adopt');
      if (adoptBtn) adoptBtn.addEventListener('click', () => openLessonAdopt(l.id));
      const goRules = $('#sheet-content [data-go-rules]');
      if (goRules) goRules.addEventListener('click', () => navigate('learn', 'rules'));
      $$('#sheet-content .lesson-rev-item').forEach(el => {
        el.addEventListener('click', () => {
          closeSheet();
          setTimeout(() => openReviewDetail(el.dataset.pid), 100);
        });
      });
    } catch (e) {
      $('#sheet-content').innerHTML = `<div class="empty">加载失败：${e.message}</div>`;
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
          <div class="muted small" style="margin-bottom:10px;">命中即 conf=100，仓位 ×1.5 · 点击卡片查看详情</div>
        </div>
        ${COMBOS.map((c, i) => `<div class="combo-card combo-clickable" data-idx="${i}" style="cursor:pointer;">
          <div class="combo-name">${escape(c.name)}</div>
          <div class="combo-meta">${c.market}</div>
          <div class="combo-strats">${c.cn_strategies.map(s => `<span class="combo-chip">${escape(s)}</span>`).join('')}</div>
        </div>`).join('')}
      `;
      // v12.25.0 Phase B: 共振组合卡可点击 → 详情
      _state._combosSnapshot = COMBOS;
      $$('#combos-list .combo-clickable').forEach(c => {
        c.addEventListener('click', () => openComboDetail(parseInt(c.dataset.idx)));
      });
    } catch (e) {
      console.error(e);
      $('#combos-list').innerHTML = '<div class="empty">加载失败</div>';
    }
  }

  // v12.25.0 Phase B: 共振组合详情 — 显示策略列表 + 该组合命中过的最近信号 + 历史胜率
  async function openComboDetail(idx) {
    openSheet('共振组合详情', '<div class="empty">加载中…</div>');
    try {
      const c = (_state._combosSnapshot || [])[idx];
      if (!c) { $('#sheet-content').innerHTML = '<div class="empty">未找到</div>'; return; }
      // 从信号列表里找该组合命中过的 (ai_reason 含组合名)
      const sigData = await loadSignals().catch(() => ({items:[]}));
      const matching = (sigData.items || []).filter(s => {
        const reason = (s.ai_reason || s.reason || '').toString();
        return reason.includes(c.name);
      }).slice(0, 20);
      // 找该组合相关 closed reviews 计算胜率
      const reviewsData = await loadReviews().catch(() => ({items:[]}));
      const relReviews = (reviewsData.items || []).filter(r => {
        const reason = (r.summary || r.entry_analysis || '').toString();
        return reason.includes(c.name);
      });
      const winRate = relReviews.length
        ? (relReviews.filter(r => (r.realized_pnl_usd||0) > 0).length / relReviews.length * 100).toFixed(0)
        : null;
      const html = `
        <h4>${escape(c.name)}</h4>
        <div class="kv-row"><span class="k">市场</span><span class="v">${escape(c.market)}</span></div>
        <div class="kv-row"><span class="k">策略数</span><span class="v">${c.cn_strategies.length}</span></div>
        <div class="kv-row"><span class="k">命中 conf</span><span class="v">100</span></div>
        <div class="kv-row"><span class="k">仓位倍数</span><span class="v">×1.5</span></div>
        <h4>📡 包含策略</h4>
        <div style="display:flex;flex-wrap:wrap;gap:6px;">
          ${c.cn_strategies.map(s => `<span class="combo-chip" style="background:var(--bg-card-2);padding:4px 10px;border-radius:12px;font-size:12px;">${escape(s)}</span>`).join('')}
        </div>
        <h4>📊 历史表现</h4>
        ${winRate !== null ? `<div class="kv-row"><span class="k">胜率(${relReviews.length} 笔闭环)</span><span class="v ${parseInt(winRate)>=60?'up':parseInt(winRate)<40?'down':''}">${winRate}%</span></div>` : '<div class="muted small">尚无闭环数据</div>'}
        ${matching.length ? `<h4>🎯 最近命中信号 (${matching.length})</h4>
          <div>${matching.slice(0,10).map(s =>
            `<div class="row combo-sig-item" data-id="${s.id}" style="cursor:pointer;">
              <div class="row-symbol">${escape(s.symbol)} <span class="small muted">${MARKET_LABEL[s.market]||s.market}</span></div>
              <div class="row-meta small">${VERDICT_LABEL[s.ai_verdict]||'⏳'} · conf ${s.ai_confidence||0} · ${fmtRelTime(s.generated_at)}</div>
            </div>`).join('')}</div>` : '<div class="muted small">尚未命中过</div>'}
      `;
      $('#sheet-content').innerHTML = html;
      $$('#sheet-content .combo-sig-item').forEach(el => {
        el.addEventListener('click', () => {
          closeSheet();
          setTimeout(() => openSignalDetail(el.dataset.id), 100);
        });
      });
    } catch (e) {
      $('#sheet-content').innerHTML = `<div class="empty">加载失败：${e.message}</div>`;
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
    console.log(`[refresh] 路由: ${key}`);
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
    // v12.27.0: v3 4 tab 路由表
    'now/now':             renderNow,                    // 主页 v3 (Phase 2 实现)
    // 持仓页 (内部 chip 切换, 共一个 render)
    'holdings/all':        renderHoldings,
    'holdings/us':         renderHoldings,
    'holdings/hk':         renderHoldings,
    'holdings/cn':         renderHoldings,
    'holdings/crypto':     renderHoldings,
    'holdings/swap':       renderHoldings,
    'holdings/risk':       renderHoldings,
    // 机会页 (复用旧 render)
    'opp/signals':         renderSignals,
    'opp/pool':            renderPoolWithMarketSummary,
    'opp/news':            renderNews,
    'opp/library':         renderStrategyLibrary,
    'opp/combos':          renderCombos,
    'opp/rejected':        renderRejected,
    'opp/ondemand':        renderOnDemandPage,
    'opp/orders':          renderOrderFlow,
    // 复盘页 (新增 strategies sub)
    'insights/reviews':    renderInsightsWithCharts,    // 顶部健康度 + 30天权益曲线 + reviews
    'insights/lessons':    renderInsightsWithCharts,
    'insights/strategies': renderInsightsWithCharts,
    'insights/rules':      renderInsightsWithCharts,
    'insights/weekly':     renderInsightsWithCharts,
    // 旧路径 兼容 (会被 hash redirect 到新路径)
    'home/home':           renderOverview,
    'market/news':         renderNews,
    'market/pool':         renderPoolWithMarketSummary,
    'market/signals':      renderSignals,
    'market/library':      renderStrategyLibrary,
    'market/combos':       renderCombos,
    'trade/spot':          renderPositions,
    'trade/swap':          renderSwapDashboard,
    'trade/ondemand':      renderOnDemandPage,
    'trade/orders':        renderOrderFlow,
    'trade/rejected':      renderRejected,
    'trade/control':       renderTradeControl,
    'learn/reviews':       renderReviewList,
    'learn/lessons':       renderLessons,
    'learn/rules':         renderRulesOnly,
    'learn/weekly':        () => renderWeekly(),
    'settings/notify':     renderSettingsNotify,
    'settings/sources':    () => renderSources(),
    'settings/llm':        renderSettingsLLM,
    'settings/system':     renderSettingsSystem,
  };

  // v12.27.0 Phase 2: Now 主页完整实现
  async function renderNow() {
    console.log('[renderNow] 开始渲染主页...');
    // v12.27.4: 标记 mobile.js 已加载执行
    const vs = document.getElementById('version-status');
    if (vs) { vs.textContent = '✓ 已加载 ' + new Date().toLocaleTimeString().slice(0,5); vs.style.color = '#3fb950'; }
    try {
      const [status, log, signals, positions, llmCost, swapAcct, swapPos, advices] = await Promise.all([
        loadStatus(), loadHistory(), loadSignals(), loadPositions(),
        loadLLMCost(), loadSwapAccount(), loadSwapPositions(), loadAdvices(),
      ]);
      // ── Hero: 总权益 ──
      const pools = status.pools || [];
      let totalEquityUSD = 0, totalPnlUSD = 0, totalInitialUSD = 0;
      for (const p of pools) {
        totalEquityUSD += p.equity_usd || 0;
        totalPnlUSD += p.pnl_usd || 0;
        const fx = p.fx_to_usd || 1;
        totalInitialUSD += (p.initial_capital || 0) * fx;
      }
      const swapMode = swapAcct && swapAcct.mode === 'swap_mock';
      let swapEquity = 0, swapInitial = 0, swapPnl = 0, swapUpnl = 0;
      if (swapAcct) {
        swapEquity = (swapAcct.balance_usd || 0) + (swapAcct.total_margin_usd || 0);
        swapInitial = swapAcct.initial_balance_usd || 0;
        swapPnl = swapAcct.total_pnl_usd || 0;
        swapUpnl = (swapPos || []).reduce((s, p) => s + (p.unrealized_pnl_usd || 0), 0);
      }
      const grandEquity = totalEquityUSD + (swapMode ? (swapEquity + swapUpnl) : 0);
      const grandPnl = totalPnlUSD + (swapMode ? (swapPnl + swapUpnl) : 0);
      const grandInitial = totalInitialUSD + (swapMode ? swapInitial : 0);
      const grandPct = grandInitial > 0 ? (grandPnl / grandInitial * 100) : 0;
      const heq = $('#hero-equity');
      if (heq) heq.textContent = fmtMoney(grandEquity);
      const hpr = $('#hero-pnl-row');
      if (hpr) {
        const cls = grandPnl >= 0 ? 'up' : 'down';
        hpr.innerHTML = `<span class="pnl-pill ${cls}">${grandPnl>=0?'+':''}${fmtMoney(Math.abs(grandPnl))} ${grandPnl>=0?'+':''}${grandPct.toFixed(2)}%</span>
          <span class="hero-vs">${grandPnl>=0?'↑':'↓'} 总盈亏</span>`;
      }
      // ── Hero sparkline (基于 status 的池数据 — 简化:用当前 pnl 模拟 30 点) ──
      // 真实 30 天权益曲线需要专门 API, 暂用 pool pnl 走势模拟
      const sparkEl = $('#hero-spark');
      if (sparkEl) {
        // 用最近 30 笔 trade 的累计 PnL 作为 sparkline 数据
        const trades = (log.items || []).filter(t => t.status === 'executed').slice(0, 30).reverse();
        let cum = grandInitial;
        const points = trades.map(t => { cum += (t.amount_usd || 0) * 0.001; return cum; });
        if (points.length < 2) points.push(grandInitial, grandEquity);
        sparkEl.innerHTML = renderSparkline(points, 400, 36, grandPnl >= 0);
      }

      // ── 三层 Inbox 计算 ──
      const allHoldings = [...(positions || []), ...((swapMode ? swapPos : []) || [])];
      const urgent = [], attention = [], info = [];
      // 紧急: 距 SL <2% / 距强平 <5%
      for (const p of (positions || [])) {
        const sl = p.stop_loss; const cur = p.current_price;
        if (sl && cur && sl > 0) {
          const distPct = Math.abs((cur - sl) / cur) * 100;
          if (distPct < 2) {
            urgent.push({
              symbol: p.symbol, market: p.market,
              title: `${p.symbol} 距 SL ${distPct.toFixed(1)}% 🚨`,
              desc: `浮${(p.pnl_pct||0)>=0?'盈':'亏'} ${fmtPct(p.pnl_pct)} · ${p.quantity} 股 · SL ${fmtMoney(sl)}`,
              progress: Math.max(0, Math.min(99, 100 - distPct * 50)),
              progClass: 'danger',
              click: () => openPositionDetail(p.id),
            });
          } else if (distPct < 5) {
            attention.push({
              symbol: p.symbol, market: p.market,
              title: `${p.symbol} 距 SL ${distPct.toFixed(1)}%`,
              desc: `浮${(p.pnl_pct||0)>=0?'盈':'亏'} ${fmtPct(p.pnl_pct)}`,
              click: () => openPositionDetail(p.id),
            });
          }
        }
      }
      // 合约: 距强平
      for (const p of ((swapMode ? swapPos : []) || [])) {
        if (p.liq_price && p.avg_open_price) {
          const distPct = p.pos_side === 'long'
            ? (1 - p.liq_price / p.avg_open_price) * 100
            : (p.liq_price / p.avg_open_price - 1) * 100;
          if (distPct < 5) {
            urgent.push({
              symbol: p.symbol, market: 'crypto',
              title: `⚡ ${(p.symbol||'').replace('-SWAP','')} 距强平 ${distPct.toFixed(1)}%`,
              desc: `${p.leverage}x 杠杆 · 浮亏 ${fmtPct(((p.unrealized_pnl_usd||0)/(p.margin_usd||1))*100)}`,
              progress: Math.max(0, Math.min(99, 100 - distPct * 20)),
              progClass: 'danger',
            });
          }
        }
      }
      // 关注: AI 共识 reduce/close (30min 内 2 条)
      const advList = advices || [];
      const advByPos = {};
      for (const a of advList) {
        if (!advByPos[a.position_id]) advByPos[a.position_id] = [];
        advByPos[a.position_id].push(a);
      }
      for (const pid in advByPos) {
        const recent = advByPos[pid].filter(a => (Date.now()/1000 - a.advised_at) < 1800);
        const reduceN = recent.filter(a => a.advice === 'reduce' || a.advice === 'close').length;
        if (reduceN >= 1) {
          const p = (positions || []).find(x => String(x.id) === String(pid));
          if (!p) continue;
          // 已在紧急里则不重复
          if (urgent.some(u => u.symbol === p.symbol)) continue;
          attention.push({
            symbol: p.symbol, market: p.market,
            title: `${p.symbol} AI 建议: ${reduceN >= 2 ? '减仓共识' : '关注'}`,
            desc: `30min 内 ${reduceN} 条 reduce 建议 · 浮${(p.pnl_pct||0)>=0?'盈':'亏'} ${fmtPct(p.pnl_pct)}`,
            click: () => openPositionDetail(p.id),
          });
        }
      }
      // 关注: 待重验 confirm 信号
      const sigItems = signals.items || [];
      const pendingReval = sigItems.filter(s =>
        s.ai_verdict === 'confirm' && !s.revalidated_at &&
        (Date.now() - (s.generated_at || 0)) < 6 * 3600 * 1000
      );
      if (pendingReval.length > 0) {
        attention.push({
          title: `${pendingReval.length} 条待重验确认信号`,
          desc: `待开市/巡检验证 · 点击进入信号页`,
          click: () => navigate('opp', 'signals', { filter: { key: 'signal', value: 'confirm' } }),
        });
      }
      // 提醒: 教训新增 / 复盘新增
      info.push({
        title: '近 24h LLM 学习产出',
        desc: '点击查看复盘 / 教训',
        click: () => navigate('insights', 'reviews'),
      });

      // 渲染 inbox
      function renderInbox(blockId, listId, cntId, items) {
        const block = $('#' + blockId);
        const list = $('#' + listId);
        const cnt = $('#' + cntId);
        if (!block || !list) return;
        if (!items.length) { block.hidden = true; return; }
        block.hidden = false;
        if (cnt) cnt.textContent = items.length;
        list.innerHTML = items.map((it, i) => `
          <div class="inbox-row" data-idx="${i}">
            <div class="left">
              <div class="inbox-symbol">${escape(it.title)}</div>
              <div class="inbox-desc">${escape(it.desc || '')}</div>
              ${it.progress != null ? `<div class="progress"><div class="progress-fill ${it.progClass||'safe'}" style="width:${it.progress}%;"></div></div>` : ''}
            </div>
            <div class="inbox-arrow">›</div>
          </div>
        `).join('');
        list.querySelectorAll('.inbox-row').forEach((row, i) => {
          if (items[i].click) row.addEventListener('click', items[i].click);
        });
      }
      renderInbox('inbox-urgent', 'inbox-urgent-list', 'inbox-urgent-cnt', urgent);
      renderInbox('inbox-attention', 'inbox-attention-list', 'inbox-attention-cnt', attention);
      renderInbox('inbox-info', 'inbox-info-list', 'inbox-info-cnt', info);
      const empty = $('#inbox-empty');
      if (empty) empty.hidden = (urgent.length + attention.length + info.length > 0);

      // ── 4 池 Grid ──
      const pgrid = $('#pools-grid');
      if (pgrid) {
        const stockCards = pools.sort((a,b) => {
          const ord = {us_hk: 0, cn: 1, crypto: 2};
          return (ord[a.pool_id] || 9) - (ord[b.pool_id] || 9);
        }).map(p => {
          const cls = (p.pnl || 0) >= 0 ? 'up' : 'down';
          const ccy = p.currency || 'USD';
          const nameMap = {'us_hk': '🇺🇸🇭🇰 美港股', 'cn': '🇨🇳 A 股', 'crypto': '🪙 加密现货'};
          const name = nameMap[p.pool_id] || p.name;
          // 用 pool 持仓数(简化, 不查具体)
          return `<div class="pool" data-pool-id="${escape(p.pool_id)}">
            <div class="pool-hdr-v3">
              <span class="pool-name-v3">${name}</span>
            </div>
            <div class="pool-equity-v3">${fmtMoney(p.equity, ccy)}</div>
            <div class="pool-pnl-v3 ${cls}">${(p.pnl||0)>=0?'+':''}${fmtMoney(Math.abs(p.pnl), ccy)} (${fmtPct(p.pnl_pct||0)})</div>
          </div>`;
        }).join('');
        let swapCard = '';
        if (swapMode) {
          const totalSwap = swapPnl + swapUpnl;
          const swapCls = totalSwap >= 0 ? 'up' : 'down';
          swapCard = `<div class="pool swap" data-pool-id="swap">
            <div class="pool-hdr-v3">
              <span class="pool-name-v3 swap">⚡ 加密合约</span>
            </div>
            <div class="pool-equity-v3">${fmtMoney(swapEquity + swapUpnl)}</div>
            <div class="pool-pnl-v3 ${swapCls}">${totalSwap>=0?'+':''}${fmtMoney(Math.abs(totalSwap))} · ${(swapPos||[]).length} 仓</div>
          </div>`;
        }
        pgrid.innerHTML = stockCards + swapCard || '<div class="muted small" style="grid-column:1/-1;text-align:center;padding:14px;">暂无池数据</div>';
        // 池卡 click → 持仓页对应市场
        pgrid.querySelectorAll('.pool[data-pool-id]').forEach(c => {
          c.addEventListener('click', () => {
            const pid = c.dataset.poolId;
            const filterMap = {'us_hk': 'us', 'cn': 'cn', 'crypto': 'crypto', 'swap': 'swap'};
            _state.holdingsFilter = filterMap[pid] || 'all';
            navigate('holdings', 'all');
            setTimeout(() => {
              $$('.chip[data-h-filter]').forEach(c => c.classList.toggle('active', c.dataset.hFilter === _state.holdingsFilter));
            }, 80);
          });
        });
      }

      // ── 24h 关键事件 ──
      const evList = $('#events-list');
      if (evList) {
        const events = [];
        // 最近成交
        const recent = (log.items || [])
          .filter(t => t.traded_at && (Date.now()/1000 - t.traded_at) < 86400)
          .slice(0, 10);
        for (const t of recent) {
          const cls = t.status === 'executed' ? 'up' : 'down';
          const icon = t.action === 'open' ? '🟢' : t.action === 'close' ? '🔴' : t.action === 'add' ? '➕' : t.action === 'reduce' ? '➖' : '•';
          let text = '';
          if (t.status === 'executed') {
            text = `<strong>${escape(t.symbol)}</strong> ${ACTION_LABEL[t.action]||t.action} ${(t.quantity||0).toFixed(2)} @ ${(t.price||0).toFixed(4)}`;
          } else {
            text = `<strong>${escape(t.symbol)}</strong> ${ACTION_LABEL[t.action]||t.action} 拒单 — ${escape((t.rejected_reason||'').slice(0,30))}`;
          }
          events.push({ ts: t.traded_at, icon, text, cls });
        }
        events.sort((a, b) => b.ts - a.ts);
        evList.innerHTML = events.slice(0, 8).map(e => `
          <div class="event-row">
            <span class="event-time">${fmtRelTime(e.ts*1000)}</span>
            <span class="event-icon">${e.icon}</span>
            <span class="event-text">${e.text}</span>
          </div>
        `).join('') || '<div class="muted small" style="padding:14px;">暂无事件</div>';
      }
    } catch (e) {
      console.error('[renderNow]', e);
      const heq = $('#hero-equity');
      if (heq) {
        heq.textContent = '加载失败';
        heq.style.fontSize = '14px';
        heq.style.color = '#f85149';
      }
      // v12.27.2: 错误显示在屏幕上 (不依赖 console)
      const evList = $('#events-list');
      if (evList) {
        evList.innerHTML = `<div style="padding:14px;background:#2a1010;border:1px solid #f85149;border-radius:8px;margin:14px;color:#f85149;font-size:11px;word-break:break-all;">
          <div style="font-weight:700;margin-bottom:6px;">⚠️ renderNow 报错 (调试信息)</div>
          <div style="color:#fff;">${(e && e.message) || String(e)}</div>
          <div style="margin-top:8px;color:#9ba8b8;">${(e && e.stack) ? String(e.stack).slice(0,400) : ''}</div>
        </div>`;
      }
    }
  }

  // 辅助: SVG sparkline 生成器
  function renderSparkline(values, width = 100, height = 24, isPositive = true) {
    if (!values || values.length < 2) return '';
    const max = Math.max(...values), min = Math.min(...values);
    const range = max - min || 1;
    const stride = width / (values.length - 1);
    const points = values.map((v, i) =>
      `${i * stride},${height - ((v - min) / range) * height}`
    ).join(' ');
    const color = isPositive ? '#c4ff4d' : '#f85149';
    const fillColor = isPositive ? 'rgba(196,255,77,0.2)' : 'rgba(248,81,73,0.2)';
    const fillPoints = `0,${height} ${points} ${width},${height}`;
    return `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" style="width:100%;height:100%;">
      <polygon points="${fillPoints}" fill="${fillColor}"/>
      <polyline points="${points}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round"/>
    </svg>`;
  }

  // v12.27.8 Phase 3: 真实 renderHoldings (用 v3 框架的 #holdings-list)
  //   - chip 切换 (all/us/hk/cn/crypto/swap/risk)
  //   - 顶部环形饼图 (按市场分布持仓市值)
  //   - holding-v3 卡 (sparkline + SL/TP 进度条 + AI inline)
  async function renderHoldings() {
    const listEl = $('#holdings-list');
    const pieCard = $('#holdings-pie-card');
    const pieSvg = $('#holdings-pie');
    const pieLeg = $('#holdings-pie-legend');
    const titleEl = $('#holdings-title');
    const subEl = $('#holdings-sub');
    if (!listEl) return;
    listEl.innerHTML = '<div class="muted small" style="padding:14px;">⏳ 加载持仓中...</div>';
    try {
      const [items, advices, swapAcct, swapPos] = await Promise.all([
        loadPositions(), loadAdvices(), loadSwapAccount(), loadSwapPositions(),
      ]);
      const swapMode = swapAcct && swapAcct.mode === 'swap_mock';
      const swapPositions = swapMode ? (swapPos || []) : [];
      const adviceMap = {};
      (advices || []).forEach(a => { if (a && a.position_id) adviceMap[a.position_id] = a; });

      // ── 顶部饼图 (按市场聚合 USD 市值) ──
      const allUsd = {us: 0, hk: 0, cn: 0, crypto: 0, swap: 0};
      for (const p of items) {
        const m = p.market || 'crypto';
        if (allUsd[m] != null) allUsd[m] += (p.market_value_usd || 0);
        else allUsd.crypto += (p.market_value_usd || 0);
      }
      for (const p of swapPositions) {
        allUsd.swap += (p.margin_usd || 0) + (p.unrealized_pnl_usd || 0);
      }
      const total = Object.values(allUsd).reduce((s, v) => s + v, 0);
      if (total > 0 && pieSvg && pieLeg) {
        if (pieCard) pieCard.hidden = false;
        const colors = {us: '#5b9eff', hk: '#bc8cff', cn: '#f4a13b', crypto: '#c4ff4d', swap: '#a78bfa'};
        const labels = {us: '🇺🇸 美股', hk: '🇭🇰 港股', cn: '🇨🇳 A股', crypto: '🪙 加密', swap: '⚡ 合约'};
        // SVG 环形饼 (viewBox 0 0 36 36, r=15.9)
        let segs = '';
        let acc = 0;
        for (const k of ['us','hk','cn','crypto','swap']) {
          const v = allUsd[k];
          if (v <= 0) continue;
          const pct = v / total * 100;
          segs += `<circle cx="18" cy="18" r="15.9155" fill="none" stroke="${colors[k]}" stroke-width="3.5"
            stroke-dasharray="${pct.toFixed(2)} ${(100-pct).toFixed(2)}"
            stroke-dashoffset="${(-acc).toFixed(2)}"
            transform="rotate(-90 18 18)"/>`;
          acc += pct;
        }
        pieSvg.innerHTML = segs +
          `<text x="18" y="17" text-anchor="middle" font-size="3.5" fill="#fff" font-weight="700">${fmtMoney(total)}</text>
           <text x="18" y="22" text-anchor="middle" font-size="2.5" fill="#9ba8b8">总市值</text>`;
        pieLeg.innerHTML = ['us','hk','cn','crypto','swap']
          .filter(k => allUsd[k] > 0)
          .map(k => `<div class="pie-legend-row">
            <span class="pie-l-name"><span class="pie-dot" style="background:${colors[k]};"></span>${labels[k]}</span>
            <span class="pie-l-val">${fmtMoney(allUsd[k])} <span class="muted small">(${(allUsd[k]/total*100).toFixed(1)}%)</span></span>
          </div>`).join('');
      } else if (pieCard) {
        pieCard.hidden = true;
      }

      // ── 按 chip 过滤 ──
      const f = _state.holdingsFilter || 'all';
      let filtered = [];
      let filteredSwap = [];
      if (f === 'all') {
        filtered = items.slice();
        filteredSwap = swapPositions.slice();
      } else if (f === 'swap') {
        filteredSwap = swapPositions.slice();
      } else if (f === 'risk') {
        // v12.27.9 风险榜: 三档分级 (放宽标准, 之前过严导致空)
        //   🚨 严重: 距 SL <5% / 浮亏 >5% / 距强平 <5%
        //   ⚠️ 关注: 距 SL <10% / 浮亏 1~5% / 距强平 5~15%
        //   按风险等级排序 (严重在上)
        filtered = items.filter(p => {
          const sl = p.stop_loss; const cur = p.current_price;
          const pnl = p.pnl_pct || 0;
          if (sl && cur && Math.abs((cur - sl) / cur) * 100 < 10) return true;
          if (pnl < -1) return true;
          return false;
        }).sort((a, b) => {
          // 风险得分高的在前 (越亏 / 越接近 SL = 风险越高)
          const scoreA = -(a.pnl_pct || 0) + (a.stop_loss && a.current_price ? Math.max(0, 10 - Math.abs((a.current_price - a.stop_loss)/a.current_price)*100) : 0);
          const scoreB = -(b.pnl_pct || 0) + (b.stop_loss && b.current_price ? Math.max(0, 10 - Math.abs((b.current_price - b.stop_loss)/b.current_price)*100) : 0);
          return scoreB - scoreA;
        });
        filteredSwap = swapPositions.filter(p => {
          if (p.liq_price && p.avg_open_price) {
            const distPct = p.pos_side === 'long'
              ? (1 - p.liq_price / p.avg_open_price) * 100
              : (p.liq_price / p.avg_open_price - 1) * 100;
            return distPct < 15;
          }
          return false;
        });
      } else {
        filtered = items.filter(p => p.market === f);
      }

      // ── 标题/小字 更新 ──
      const filterLabels = {all: '所有', us: '🇺🇸 美股', hk: '🇭🇰 港股', cn: '🇨🇳 A股',
                            crypto: '🪙 加密现货', swap: '⚡ 加密合约', risk: '🚨 风险榜'};
      const totalCount = filtered.length + filteredSwap.length;
      let totalPnl = 0;
      for (const p of filtered) totalPnl += (p.pnl_usd || 0);
      for (const p of filteredSwap) totalPnl += (p.unrealized_pnl_usd || 0);
      if (titleEl) titleEl.textContent = `💼 持仓 — ${filterLabels[f] || f}`;
      if (subEl) {
        const cls = totalPnl >= 0 ? 'up' : 'down';
        subEl.innerHTML = `${totalCount} 笔 · 浮动 <span class="${cls}">${fmtPnl(totalPnl)}</span>`;
      }

      // v12.27.9: 风险榜顶部说明依据 (用户问"数据怎么来的")
      const riskHintHtml = f === 'risk' ? `
        <div style="margin: 0 14px 12px; padding: 10px 12px; background: rgba(248,81,73,0.06); border-left: 3px solid var(--down); border-radius: 0 8px 8px 0; font-size: 11px; line-height: 1.6;">
          <div style="color: var(--down); font-weight: 700; margin-bottom: 4px;">🚨 风险榜判定依据</div>
          <div style="color: var(--text-2);">现货命中 <b>任一</b> 即列入:</div>
          <div style="color: var(--text-3); margin-left: 8px;">• 距止损 SL &lt;10%</div>
          <div style="color: var(--text-3); margin-left: 8px;">• 当前浮亏 &gt;1%</div>
          <div style="color: var(--text-2); margin-top: 4px;">合约: 距强平 &lt;15%</div>
          <div style="color: var(--text-3); margin-top: 4px; font-size: 10px;">⚠️ 排序 = 风险得分 (越亏 / 越接近 SL → 越靠前)</div>
        </div>` : '';

      // ── 渲染卡片 ──
      if (!totalCount) {
        listEl.innerHTML = riskHintHtml + `<div class="empty" style="padding:30px 14px;">
          <div class="empty-icon">${f === 'risk' ? '✅' : '📭'}</div>
          <div>${f === 'risk' ? '当前无风险持仓' : '该分类下无持仓'}</div>
          ${f === 'risk' ? `<div class="muted small" style="margin-top:6px;">${items.length + swapPositions.length} 笔持仓 全部健康 (无亏损 &gt;1%, 无逼近 SL)</div>` : ''}
        </div>`;
        return;
      }
      // 现货卡 + 合约卡
      const spotHtml = filtered.map(p => renderHoldingV3(p, adviceMap[p.id])).join('');
      const swapHtml = filteredSwap.map(p => renderHoldingV3Swap(p)).join('');
      listEl.innerHTML = riskHintHtml + spotHtml + swapHtml;
      // 绑点击 → 持仓详情
      listEl.querySelectorAll('.holding-v3[data-id]').forEach(c => {
        c.addEventListener('click', () => openPositionDetail(c.dataset.id));
      });
      listEl.querySelectorAll('.holding-v3[data-swap-id]').forEach(c => {
        c.addEventListener('click', () => openSwapPositionDetail(c.dataset.swapId));
      });
    } catch (e) {
      console.error('[renderHoldings]', e);
      listEl.innerHTML = `<div class="empty" style="padding:14px;color:#f85149;">
        加载失败: ${escape((e && e.message) || String(e))}
      </div>`;
    }
  }

  // v3 现货持仓卡
  function renderHoldingV3(p, advice) {
    const pnlPct = p.pnl_pct || 0;
    const cls = pnlPct >= 0 ? '' : (pnlPct < -5 ? 'danger' : 'warn-b');
    const ccy = p.cost_currency || 'USD';
    const sl = p.stop_loss, tp = p.take_profit;
    const cur = p.current_price || p.avg_cost || 0;
    const avg = p.avg_cost || 0;
    // SL 进度条: 当前价距 SL 多远 (越近越红)
    let slBarHtml = '';
    if (sl && cur) {
      const distPct = Math.abs((cur - sl) / cur) * 100;
      const fill = Math.max(5, Math.min(100, 100 - distPct * 5));
      const cls2 = distPct < 2 ? 'danger' : distPct < 5 ? 'warn-b' : 'safe';
      slBarHtml = `<div class="bar-row-v3 ${cls2}">
        <span class="lbl">SL</span>
        <div class="progress"><div class="progress-fill ${cls2 === 'safe' ? 'safe' : (cls2 === 'danger' ? 'danger' : 'warn-bg')}" style="width:${fill}%;"></div></div>
        <span class="pct">${distPct.toFixed(1)}%</span>
      </div>`;
    }
    let tpBarHtml = '';
    if (tp && cur) {
      const distPct = Math.abs((tp - cur) / cur) * 100;
      const fill = Math.max(5, Math.min(100, 100 - distPct * 2));
      tpBarHtml = `<div class="bar-row-v3 safe">
        <span class="lbl">TP</span>
        <div class="progress"><div class="progress-fill acc" style="width:${fill}%;"></div></div>
        <span class="pct">${distPct.toFixed(1)}%</span>
      </div>`;
    }
    // AI 建议 inline
    let aiHtml = '';
    if (advice && advice.advice) {
      const cn = ADVICE_LABEL_CN[advice.advice] || advice.advice;
      const adCls = (advice.advice === 'close' || advice.advice === 'reduce') ? 'close'
                   : (advice.advice === 'add') ? '' : 'warn-b';
      aiHtml = `<div class="ai-tip ${adCls}">
        <span class="ai-tip-role">🤖 AI</span>${escape(cn)}${advice.confidence ? `<span class="ai-tip-conf">置信 ${advice.confidence}</span>` : ''}
        ${advice.reason ? `<div style="margin-top:3px;font-size:11px;color:var(--text-2);">${escape(String(advice.reason).slice(0, 80))}</div>` : ''}
      </div>`;
    }
    return `<div class="holding-v3 ${cls}" data-id="${escape(p.id)}">
      <div class="h3-hdr">
        <div class="h3-l">
          <span class="h3-symbol">${escape(p.symbol)}</span>
          <span class="h3-side">${MARKET_LABEL[p.market] || p.market} · ${p.side === 'long' ? '多' : '空'}</span>
        </div>
        <div class="h3-pnl-pct ${pnlPct >= 0 ? 'up' : 'down'}">${fmtPct(pnlPct)}</div>
      </div>
      <div class="h3-row">
        <span>${(p.quantity || 0).toLocaleString()} @ ${fmtMoney(avg, ccy)}</span>
        <span class="v">${fmtMoney(cur, ccy)}</span>
      </div>
      <div class="h3-row">
        <span>浮动盈亏</span>
        <span class="v ${pnlPct >= 0 ? 'up' : 'down'}">${fmtPnl(p.pnl_local || 0, ccy)}</span>
      </div>
      ${slBarHtml}
      ${tpBarHtml}
      ${aiHtml}
    </div>`;
  }

  // v3 合约持仓卡
  function renderHoldingV3Swap(p) {
    const upnl = p.unrealized_pnl_usd || 0;
    const margin = p.margin_usd || 1;
    const upnlPct = (upnl / margin) * 100;
    const cls = upnlPct >= 0 ? '' : (upnlPct < -20 ? 'danger' : 'warn-b');
    const sideTxt = p.pos_side === 'long' ? '🟢多' : '🔴空';
    const dispSym = (p.symbol || '').replace('-SWAP', '');
    let liqBarHtml = '';
    if (p.liq_price && p.avg_open_price) {
      const distPct = p.pos_side === 'long'
        ? (1 - p.liq_price / p.avg_open_price) * 100
        : (p.liq_price / p.avg_open_price - 1) * 100;
      const fill = Math.max(5, Math.min(100, 100 - distPct * 3));
      const c2 = distPct < 5 ? 'danger' : distPct < 15 ? 'warn-b' : 'safe';
      liqBarHtml = `<div class="bar-row-v3 ${c2}">
        <span class="lbl">强平</span>
        <div class="progress"><div class="progress-fill ${c2 === 'safe' ? 'safe' : (c2 === 'danger' ? 'danger' : 'warn-bg')}" style="width:${fill}%;"></div></div>
        <span class="pct">${distPct.toFixed(1)}%</span>
      </div>`;
    }
    return `<div class="holding-v3 swap ${cls}" data-swap-id="${escape(p.id)}">
      <div class="h3-hdr">
        <div class="h3-l">
          <span class="h3-symbol">${escape(dispSym)}</span>
          <span class="h3-side swap">${sideTxt} · ${p.leverage || 1}x</span>
        </div>
        <div class="h3-pnl-pct ${upnlPct >= 0 ? 'up' : 'down'}">${fmtPct(upnlPct)}</div>
      </div>
      <div class="h3-row">
        <span>${((p.qty||0) * (p.contract_size||0.01)).toFixed(6)} 个 @ ${(p.avg_open_price||0).toFixed(4)}</span>
        <span class="v">${fmtMoney(p.mark_price || p.avg_open_price || 0)}</span>
      </div>
      <div class="h3-row">
        <span>浮动 PnL</span>
        <span class="v ${upnlPct >= 0 ? 'up' : 'down'}">${fmtPnl(upnl)}</span>
      </div>
      ${liqBarHtml}
      <div class="h3-row">
        <span class="muted small">资金费 ${fmtPnl(p.funding_fee_total_usd || 0)} · 保证金 ${fmtMoney(margin)}</span>
      </div>
    </div>`;
  }

  // v12.27.8: swap 持仓详情 (复用旧抽屉, 简版)
  async function openSwapPositionDetail(swapId) {
    openSheet('合约持仓详情', '<div class="empty">加载中…</div>');
    try {
      const items = await loadSwapPositions();
      const p = (items || []).find(x => String(x.id) === String(swapId));
      if (!p) { $('#sheet-content').innerHTML = '<div class="empty">未找到</div>'; return; }
      const sym = (p.symbol || '').replace('-SWAP', '');
      const html = `
        <h4>基本</h4>
        <div class="kv-row"><span class="k">代码</span><span class="v">${escape(sym)} (永续)</span></div>
        <div class="kv-row"><span class="k">方向</span><span class="v">${p.pos_side === 'long' ? '🟢 多' : '🔴 空'} · ${p.leverage || 1}x</span></div>
        <div class="kv-row"><span class="k">数量</span><span class="v">${((p.qty||0) * (p.contract_size||0.01)).toFixed(6)}</span></div>
        <div class="kv-row"><span class="k">开仓价</span><span class="v">${(p.avg_open_price||0).toFixed(4)}</span></div>
        <div class="kv-row"><span class="k">现价</span><span class="v">${(p.mark_price||p.avg_open_price||0).toFixed(4)}</span></div>
        <div class="kv-row"><span class="k">浮动盈亏</span><span class="v ${(p.unrealized_pnl_usd||0)>=0?'up':'down'}">${fmtPnl(p.unrealized_pnl_usd||0)}</span></div>
        <h4>风控</h4>
        <div class="kv-row"><span class="k">保证金</span><span class="v">${fmtMoney(p.margin_usd||0)}</span></div>
        <div class="kv-row"><span class="k">强平价</span><span class="v">${(p.liq_price||0).toFixed(4)}</span></div>
        <div class="kv-row"><span class="k">累计 funding</span><span class="v">${fmtPnl(p.funding_fee_total_usd||0)}</span></div>
        <div class="kv-row"><span class="k">累计手续费</span><span class="v down">-${fmtMoney(p.total_fee_usd||0)}</span></div>
        <div class="sheet-actions" style="display:flex;flex-wrap:wrap;gap:6px;">
          <button class="btn btn-link" data-go-news>📰 该币新闻</button>
          <button class="btn btn-link" data-go-signals>🎯 历史信号</button>
        </div>
      `;
      $('#sheet-content').innerHTML = html;
      const goN = $('#sheet-content [data-go-news]');
      if (goN) goN.addEventListener('click', () => navigate('opp', 'news', { symbolFilter: sym }));
      const goS = $('#sheet-content [data-go-signals]');
      if (goS) goS.addEventListener('click', () => navigate('opp', 'signals', { symbolFilter: sym }));
    } catch (e) {
      $('#sheet-content').innerHTML = `<div class="empty">加载失败: ${escape(e.message)}</div>`;
    }
  }
  async function renderInsightsWithCharts() {
    // Phase 5 完整实现; 现根据 sub 调对应 render
    const sub = _state.activeSub;
    try {
      // 顶部 健康度卡 + 30 天权益曲线 (常驻所有 sub)
      await renderInsightsHeader();
      if (sub === 'reviews') await renderReviewList();
      else if (sub === 'lessons') await renderLessons();
      else if (sub === 'rules') await renderRulesOnly();
      else if (sub === 'weekly') await renderWeekly();
      else if (sub === 'strategies') await renderStrategyPerf();
    } catch (e) { console.error('[renderInsightsWithCharts]', e); }
  }

  // v12.27.8 Phase 5: 健康度卡 + 30 天权益曲线
  async function renderInsightsHeader() {
    try {
      const reviewsData = await loadReviews().catch(() => ({items: []}));
      const reviews = reviewsData.items || [];
      // 30 天内
      const cutoff = Date.now() / 1000 - 30 * 86400;
      const recent = reviews.filter(r => (r.close_at || 0) >= cutoff);
      // 累计 pnl_pct
      let cumPct = 0;
      let wins = 0, losses = 0, totalPctSum = 0;
      for (const r of recent) {
        const p = r.realized_pnl_pct || 0;
        if (p > 0) wins++; else if (p < 0) losses++;
        totalPctSum += p;
        cumPct += p;
      }
      const winRate = recent.length ? (wins / recent.length * 100) : 0;
      // 健康度卡
      const healthCard = $('#health-card');
      const healthPnl = $('#health-pnl');
      const healthWarn = $('#health-warn');
      const healthDetail = $('#health-detail');
      const healthStats = $('#health-stats');
      if (healthCard && healthPnl) {
        healthCard.hidden = false;
        const healthy = totalPctSum >= 0;
        healthCard.classList.toggle('healthy', healthy);
        healthPnl.textContent = `${totalPctSum >= 0 ? '+' : ''}${totalPctSum.toFixed(2)}%`;
        healthPnl.className = 'health-num ' + (totalPctSum >= 0 ? 'up' : 'down');
        if (healthWarn) {
          healthWarn.textContent = healthy ? '✓ 系统健康' : '⚠️ 累计亏损';
          healthWarn.className = 'health-warn-text' + (healthy ? ' up' : '');
        }
        if (healthDetail) {
          healthDetail.textContent = `30 天内 ${recent.length} 笔闭环 · 胜率 ${winRate.toFixed(1)}% · 平均 ${recent.length ? (totalPctSum/recent.length).toFixed(2) : 0}%/笔`;
        }
        if (healthStats) {
          healthStats.innerHTML = `
            <div class="h-stat"><div class="h-stat-num up">${wins}</div><div class="h-stat-label">盈利笔数</div></div>
            <div class="h-stat"><div class="h-stat-num down">${losses}</div><div class="h-stat-label">亏损笔数</div></div>
            <div class="h-stat"><div class="h-stat-num">${winRate.toFixed(0)}%</div><div class="h-stat-label">胜率</div></div>
          `;
        }
      }
      // 30 天权益曲线 — v12.27.9 改为时间轴 (X = 真实日期, 不是 review 序号)
      //   旧版按 review index 等距, 一笔交易 = 一个点 → 时间稀疏的话曲线压缩, 视觉误导
      //   新版: 31 个点 (day -30 ~ day 0), 每点 = 该日及之前累计 pnl%
      const eqEl = $('#equity-30d');
      if (eqEl) {
        const sorted = recent.slice().sort((a, b) => (a.close_at || 0) - (b.close_at || 0));
        const today = new Date(); today.setHours(23, 59, 59, 999);
        const todayTs = today.getTime() / 1000;
        // 生成 31 个 day-ts 点 (day -30 → day 0)
        const dayPoints = [];
        for (let d = 30; d >= 0; d--) {
          const ts = todayTs - d * 86400;
          let cum = 0;
          for (const r of sorted) {
            if ((r.close_at || 0) <= ts) cum += (r.realized_pnl_pct || 0);
          }
          dayPoints.push(cum);
        }
        const w = 320, h = 140;
        const max = Math.max(...dayPoints, 0);
        const min = Math.min(...dayPoints, 0);
        const range = (max - min) || 1;
        const stride = w / (dayPoints.length - 1);
        const ptStr = dayPoints.map((v, i) => `${i * stride},${(h - ((v - min) / range) * h).toFixed(2)}`).join(' ');
        const last = dayPoints[dayPoints.length - 1];
        const isPos = last >= 0;
        const color = isPos ? '#c4ff4d' : '#f85149';
        const fill = isPos ? 'rgba(196,255,77,0.15)' : 'rgba(248,81,73,0.15)';
        const zeroY = (h - ((0 - min) / range) * h).toFixed(2);
        eqEl.innerHTML = `
          <line x1="0" y1="${zeroY}" x2="${w}" y2="${zeroY}" stroke="#30363d" stroke-width="0.5" stroke-dasharray="3,3"/>
          <text x="${w-2}" y="${parseFloat(zeroY)-2}" text-anchor="end" fill="#6b7280" font-size="8">0%</text>
          <polygon points="0,${h} ${ptStr} ${w},${h}" fill="${fill}"/>
          <polyline points="${ptStr}" fill="none" stroke="${color}" stroke-width="1.8"/>
          <circle cx="${w}" cy="${(h - ((last - min) / range) * h).toFixed(2)}" r="3" fill="${color}"/>
          <text x="6" y="14" fill="#9ba8b8" font-size="10">累计 ${last >= 0 ? '+' : ''}${last.toFixed(2)}%</text>
          <text x="${w-6}" y="14" text-anchor="end" fill="#6b7280" font-size="9">最高 ${max >= 0 ? '+' : ''}${max.toFixed(1)}% · 最低 ${min.toFixed(1)}%</text>
        `;
      }
    } catch (e) {
      console.error('[insightsHeader]', e);
    }
  }

  // v12.27.8 Phase 5: 策略表现页 — 按 pool_id (市场) + symbol top-N + 日 PnL 热图
  async function renderStrategyPerf() {
    const barsEl = $('#strategy-bars');
    const heatEl = $('#pnl-heatmap');
    if (!barsEl) return;
    barsEl.innerHTML = '<div class="muted small" style="padding:8px;">⏳ 加载中…</div>';
    try {
      const reviewsData = await loadReviews().catch(() => ({items: []}));
      const reviews = reviewsData.items || [];
      const cutoff = Date.now() / 1000 - 30 * 86400;
      const recent = reviews.filter(r => (r.close_at || 0) >= cutoff);
      if (!recent.length) {
        barsEl.innerHTML = '<div class="muted small" style="padding:14px;text-align:center;">30 天内无闭环交易</div>';
        if (heatEl) heatEl.innerHTML = '<div class="muted small" style="grid-column:1/-1;text-align:center;padding:14px;">无数据</div>';
        return;
      }
      // ── 按 symbol 聚合 (top-10 by 累计 pnl_pct, 含正负) ──
      const bySymbol = {};
      for (const r of recent) {
        const k = r.symbol || '?';
        if (!bySymbol[k]) bySymbol[k] = {symbol: k, market: r.market, count: 0, totalPct: 0};
        bySymbol[k].count++;
        bySymbol[k].totalPct += (r.realized_pnl_pct || 0);
      }
      const sorted = Object.values(bySymbol).sort((a, b) => Math.abs(b.totalPct) - Math.abs(a.totalPct)).slice(0, 10);
      const maxAbs = Math.max(...sorted.map(s => Math.abs(s.totalPct)), 1);
      barsEl.innerHTML = sorted.map(s => {
        const pct = s.totalPct;
        const cls = pct >= 0 ? 'up' : 'down';
        const w = (Math.abs(pct) / maxAbs * 100).toFixed(1);
        return `<div class="bar-h-row">
          <div class="bar-h-name">${escape(s.symbol)} <span class="muted small">${MARKET_LABEL[s.market]||s.market} ${s.count}笔</span></div>
          <div class="bar-h-track"><div class="bar-h-fill ${cls}" style="width:${w}%;"></div></div>
          <div class="bar-h-val ${cls}">${fmtPct(pct)}</div>
        </div>`;
      }).join('');

      // ── 30 天日 PnL 热图 ──
      if (heatEl) {
        // 按日期分组 (本地时区)
        const byDay = {};
        for (const r of recent) {
          if (!r.close_at) continue;
          const d = new Date(r.close_at * 1000);
          const key = d.toISOString().slice(0, 10);  // YYYY-MM-DD
          if (!byDay[key]) byDay[key] = {pct: 0, count: 0};
          byDay[key].pct += (r.realized_pnl_pct || 0);
          byDay[key].count++;
        }
        // 生成最近 30 天 cell
        const cells = [];
        const today = new Date(); today.setHours(0,0,0,0);
        for (let i = 29; i >= 0; i--) {
          const d = new Date(today); d.setDate(d.getDate() - i);
          const key = d.toISOString().slice(0, 10);
          const v = byDay[key];
          let cls = '';
          if (v) {
            const p = v.pct;
            if (p > 5) cls = 'u4';
            else if (p > 2) cls = 'u3';
            else if (p > 0.5) cls = 'u2';
            else if (p > 0) cls = 'u1';
            else if (p > -0.5) cls = 'd1';
            else if (p > -2) cls = 'd2';
            else if (p > -5) cls = 'd3';
            else cls = 'd4';
          }
          const title = v ? `${key}: ${v.count} 笔 ${fmtPct(v.pct)}` : `${key}: 无交易`;
          cells.push(`<div class="heat-cell ${cls}" title="${title}"></div>`);
        }
        heatEl.innerHTML = cells.join('');
      }
    } catch (e) {
      console.error('[strategyPerf]', e);
      barsEl.innerHTML = `<div class="muted small" style="padding:14px;color:#f85149;">加载失败: ${escape((e && e.message) || String(e))}</div>`;
    }
  }

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
      const todayCost = (llmCost && (llmCost.today_cost_usd || llmCost.today_total_usd)) || 0;
      // v12.25.0: quick-stats 可点击跳转
      $('#home-stats').innerHTML = `
        <div class="stat-card clickable" data-go="trade/spot" style="cursor:pointer;">
          <div class="stat-label">📊 总持仓 ›</div><div class="stat-value">${posCount}</div></div>
        <div class="stat-card clickable" data-go="trade/orders" data-go-filter="order:filled" style="cursor:pointer;">
          <div class="stat-label">📥 今日成交 ›</div><div class="stat-value">${todayTrades}</div></div>
        <div class="stat-card clickable" data-go="market/signals" data-go-filter="signal:verifying" style="cursor:pointer;">
          <div class="stat-label">⏳ 待重验 ›</div><div class="stat-value ${pendingReval>0?'warn':''}">${pendingReval}</div></div>
        <div class="stat-card clickable" data-go="settings/llm" style="cursor:pointer;">
          <div class="stat-label">💰 今日 AI ›</div><div class="stat-value">$${todayCost.toFixed(3)}</div></div>
      `;
      $$('#home-stats .stat-card.clickable').forEach(c => {
        c.addEventListener('click', () => {
          const [tab, sub] = (c.dataset.go || '').split('/');
          let opts = {};
          if (c.dataset.goFilter) {
            const [k, v] = c.dataset.goFilter.split(':');
            opts.filter = { key: k, value: v };
          }
          navigate(tab, sub, opts);
        });
      });

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
        <div class="swap-info-row"><span class="k">数量</span><span class="v">${((p.qty||0) * (p.contract_size||0.01)).toFixed(6)} 个</span></div>
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
  // v12.21.4: 接入 _state.cache.swapOrders (与 loadSwapOrders 共用 cache)
  async function loadSwapOrdersAll() {
    if (_state.cache.swapOrders) return _state.cache.swapOrders;
    try {
      const r = await fetchJSON('/api/swap/orders?limit=100');
      _state.cache.swapOrders = r.items || [];
      return _state.cache.swapOrders;
    } catch { return []; }
  }
  async function renderOrderFlow() {
    try {
      // v12.27.12: 订单页专用大窗口拉取 (independent of cache)
      //   loadHistory() 默认 limit=50, 但用户 30 天有 50+ 闭环 → 每笔 ≥2 log
      //   = 100+ entries, 50 窗口被新数据填满 → 老 close 拿不到 → 美股只剩 9 个 open
      // v12.27.13: swap orders 后端 max=200 (不是 500), 之前 500 返回 422
      const [swapOrdersResp, logResp] = await Promise.all([
        fetchJSON('/api/swap/orders?limit=200').catch(() => ({items: []})),
        fetchJSON('/api/auto-trade/log?limit=500').catch(() => ({items: []})),
      ]);
      const swapOrders = swapOrdersResp.items || [];
      const log = logResp;
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
          // v12.23.5: contract_size 让 renderOrderRow 把张×ctVal 显示成真币
          contract_size: o.contract_size || 0.01,
          reason: o.reject_reason || '',
          // v12.23.2: 单笔已实现 PnL (仅 close/reduce 订单填, 其他为 null)
          realized_pnl_usd: o.realized_pnl_usd,
        });
      }
      for (const t of (log.items || [])) {
        // v12.27.13: spot close/reduce 的 realized_pnl_usd 在 trigger_detail 里, 旧版没提取 → 平仓盈亏看不到
        let spotPnl = null;
        if (t.trigger_detail && (t.action === 'close' || t.action === 'reduce')) {
          const td = typeof t.trigger_detail === 'string'
            ? (() => { try { return JSON.parse(t.trigger_detail); } catch { return {}; } })()
            : t.trigger_detail;
          if (td && td.realized_pnl_usd != null) spotPnl = td.realized_pnl_usd;
        }
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
          realized_pnl_usd: spotPnl,  // v12.27.13: 现货 close PnL 也走顶层字段, 让 renderOrderRow 显示
        });
      }
      // 按时间倒序
      items.sort((a, b) => (b.ts || 0) - (a.ts || 0));

      // v12.27.9: 顶部 各市场 30 天成交汇总卡 (用户问"缺少各市场历史成交")
      // v12.27.10: 区分 现货加密 vs 合约 (swap), swap → 合约 (⚡), spot → 加密 (🪙)
      const sumEl = $('#order-market-summary');
      if (sumEl) {
        const cutoff = Date.now() / 1000 - 30 * 86400;
        const isFilled = (s) => s === 'filled' || s === 'executed';
        const exec30d = items.filter(i => isFilled(i.status) && (i.ts || 0) >= cutoff);
        const mktAgg = {us: {n:0, pnl:0}, hk: {n:0, pnl:0}, cn: {n:0, pnl:0},
                        swap: {n:0, pnl:0}, crypto: {n:0, pnl:0}};
        for (const it of exec30d) {
          // swap 走 'swap' 桶, 现货 crypto 走 'crypto' 桶, 股市走对应市场
          const m = it.isSwap ? 'swap' : (it.market || 'crypto');
          if (!mktAgg[m]) continue;
          mktAgg[m].n++;
          if (it.realized_pnl_usd != null) mktAgg[m].pnl += Number(it.realized_pnl_usd) || 0;
        }
        // 4 列布局: 现货 crypto 笔数为 0 时不占列, 把合约塞进第 4 列
        const hasSpotCrypto = mktAgg.crypto.n > 0;
        const mktConf = [
          {k: 'us', emoji: '🇺🇸', name: '美股'},
          {k: 'hk', emoji: '🇭🇰', name: '港股'},
          {k: 'cn', emoji: '🇨🇳', name: 'A股'},
          // 没现货加密单时, 第 4 列直接显示合约
          hasSpotCrypto ? {k: 'crypto', emoji: '🪙', name: '加密现货'} : {k: 'swap', emoji: '⚡', name: '合约'},
        ];
        // 如果同时有现货+合约, 加第 5 列
        if (hasSpotCrypto && mktAgg.swap.n > 0) {
          mktConf.push({k: 'swap', emoji: '⚡', name: '合约'});
        }
        const totalN = exec30d.length;
        const cols = mktConf.length;
        sumEl.innerHTML = `
          <div style="margin: 0 14px 10px; padding: 10px 12px; background: var(--bg-1); border-radius: 10px; border: 1px solid var(--bd);">
            <div style="font-size: 11px; color: var(--text-2); margin-bottom: 8px; display:flex; justify-content:space-between;">
              <span>📊 30 天各市场成交</span>
              <span class="muted">共 ${totalN} 笔</span>
            </div>
            <div style="display: grid; grid-template-columns: repeat(${cols}, 1fr); gap: 6px;">
              ${mktConf.map(({k, emoji, name}) => {
                const a = mktAgg[k];
                const cls = a.pnl > 0 ? 'up' : a.pnl < 0 ? 'down' : '';
                return `<div style="text-align:center; padding: 6px 4px; background: var(--bg-2); border-radius: 6px; cursor: pointer;" data-mkt-quick="${k}">
                  <div style="font-size:14px;">${emoji}</div>
                  <div style="font-size: 11px; color: var(--text-2); margin: 2px 0;">${name}</div>
                  <div style="font-size: 14px; font-weight: 700;">${a.n}</div>
                  <div class="${cls}" style="font-size: 10px; font-weight: 600;">${a.pnl !== 0 ? (a.pnl >= 0 ? '+' : '') + '$' + a.pnl.toFixed(0) : '—'}</div>
                </div>`;
              }).join('')}
            </div>
          </div>`;
        // 点击市场卡 → 切换 chip + 过滤
        // chip 用 'crypto' 桶名涵盖现货+合约, 'swap'/'crypto' 都映射到 chip 'crypto'
        sumEl.querySelectorAll('[data-mkt-quick]').forEach(el => {
          el.addEventListener('click', () => {
            const k = el.dataset.mktQuick;
            const chipKey = (k === 'swap') ? 'crypto' : k;
            _state.orderMarketFilter = chipKey;
            $$('.chip[data-omfilter]').forEach(c => c.classList.toggle('active', c.dataset.omfilter === chipKey));
            renderOrderFlow();
          });
        });
      }

      // 应用 filter
      // v12.27.10: '已成交' chip 同时匹配 'filled' (合约) 和 'executed' (现货)
      //   旧版 chip 只对一种状态生效, 美股/港股/A股 (status=executed) 全被遮
      const f = _state.orderFilter || 'all';
      const mf = _state.orderMarketFilter || 'all';
      let filtered;
      if (f === 'all') filtered = items;
      else if (f === 'filled') filtered = items.filter(i => i.status === 'filled' || i.status === 'executed');
      else filtered = items.filter(i => i.status === f);
      if (mf !== 'all') {
        filtered = filtered.filter(i => {
          const m = i.isSwap ? 'crypto' : (i.market || 'crypto');
          return m === mf;
        });
      }
      // v12.25.0: 跨页 symbol 锁定
      if (_state.symbolFilter) {
        filtered = filtered.filter(i => i.symbol === _state.symbolFilter);
      }
      const hintHTML = _state.symbolFilter
        ? `<div class="filter-hint">🔗 仅显示 <b>${escape(_state.symbolFilter)}</b> 的订单 <span class="clear-hint" style="cursor:pointer;color:#5b9eff;">× 清除</span></div>`
        : '';

      if (!filtered.length) {
        $('#order-flow-list').innerHTML = hintHTML + `<div class="empty">
          <div class="empty-icon">📋</div>
          <div>无订单记录</div>
        </div>`;
        return;
      }
      const toShow = filtered.slice(0, 80);
      $('#order-flow-list').innerHTML = hintHTML + toShow.map((o, idx) => {
        const html = renderOrderRow(o);
        return html.replace('<div class="order-row', `<div class="order-row order-clickable" data-idx="${idx}" style="cursor:pointer;"`).replace(/style="cursor:pointer;"\s*style="cursor:pointer;"/, 'style="cursor:pointer;"');
      }).join('');
      _state._orderSnapshot = toShow;
      $$('#order-flow-list .order-clickable').forEach(r => {
        r.addEventListener('click', () => openOrderDetail(parseInt(r.dataset.idx)));
      });
      const clearH = $('#order-flow-list .clear-hint');
      if (clearH) clearH.addEventListener('click', () => { _state.symbolFilter = null; renderOrderFlow(); });
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
    // v12.23.2: 单笔 PnL 显示 (close/reduce 才有, open/add 为 null)
    const pnl = o.realized_pnl_usd;
    const pnlHtml = (pnl !== null && pnl !== undefined && pnl !== '')
      ? ` · <span class="${pnl >= 0 ? 'up' : 'down'}" style="font-weight:600;">${pnl >= 0 ? '+' : ''}$${Number(pnl).toFixed(2)}</span>`
      : '';
    const marketLabel = o.isSwap ? '⚡' : (o.market === 'us' ? '🇺🇸' : o.market === 'hk' ? '🇭🇰' : o.market === 'cn' ? '🇨🇳' : o.market === 'crypto' ? '🪙' : '');
    return `<div class="order-row status-${o.status}">
      <div class="order-hdr">
        <div class="order-symbol">${marketLabel ? marketLabel + ' ' : ''}${escape(o.symbol)} ${swapTag} <span class="muted small">${intentTxt} ${sideTxt}</span></div>
        <span class="order-status">${statusLabel}</span>
      </div>
      <div class="order-meta">
        ${o.isSwap ? `${((o.qty||0) * (o.contract_size||0.01)).toFixed(6)} 个` : `${(o.qty||0).toFixed(4)}`} @ ${(o.price||0).toFixed(4)}
        ${o.fee ? ` · 手续费 ${fmtMoney(o.fee)}` : ''}${pnlHtml}
        · ${fmtRelTime(o.ts * 1000)}
      </div>
      ${o.reason && o.status !== 'filled' && o.status !== 'executed' ? `<div class="order-meta small" style="color:var(--down);margin-top:4px;">${escape(o.reason).slice(0, 80)}</div>` : ''}
    </div>`;
  }

  // v12.25.0 Phase B: 订单详情抽屉
  async function openOrderDetail(idx) {
    openSheet('订单详情', '<div class="empty">加载中…</div>');
    try {
      const items = _state._orderSnapshot || [];
      const o = items[idx];
      if (!o) { $('#sheet-content').innerHTML = '<div class="empty">未找到</div>'; return; }
      const statusLabel = {
        pending: '⏳ 挂单中', filled: '✅ 已成交', cancelled: '⊘ 已撤单',
        rejected: '❌ 已拒单', executed: '✅ 已成交',
      }[o.status] || o.status;
      const intentTxt = ACTION_LABEL[o.intent] || o.intent || '?';
      const pnl = o.realized_pnl_usd;
      const isSwap = !!o.isSwap;
      const realQty = isSwap ? (o.qty||0) * (o.contract_size||0.01) : (o.qty||0);
      const html = `
        <h4>订单信息</h4>
        <div class="kv-row"><span class="k">代码</span><span class="v">${escape(o.symbol)}${isSwap ? ' ⚡合约 ' + (o.leverage||'?') + 'x' : ''}</span></div>
        <div class="kv-row"><span class="k">动作 / 方向</span><span class="v">${intentTxt} ${o.pos_side ? (o.pos_side==='long'?'多':'空') : (o.side||'')}</span></div>
        <div class="kv-row"><span class="k">状态</span><span class="v">${statusLabel}</span></div>
        <div class="kv-row"><span class="k">价格</span><span class="v">${(o.price||0).toFixed(4)}</span></div>
        <div class="kv-row"><span class="k">${isSwap?'数量(张/真币)':'数量'}</span>
          <span class="v">${isSwap ? `${(o.qty||0).toFixed(4)} 张 / ${realQty.toFixed(6)} 个` : realQty.toFixed(4)}</span></div>
        ${o.fee ? `<div class="kv-row"><span class="k">手续费</span><span class="v">${fmtMoney(o.fee)}</span></div>` : ''}
        ${pnl !== null && pnl !== undefined ? `<div class="kv-row"><span class="k">单笔已实现 PnL</span><span class="v ${pnl>=0?'up':'down'}">${pnl>=0?'+':''}$${Number(pnl).toFixed(2)}</span></div>` : ''}
        <div class="kv-row"><span class="k">时间</span><span class="v">${fmtTime(o.ts)}</span></div>
        ${o.reason ? `<h4>${o.status==='rejected'?'⊘ 拒单原因':'备注'}</h4>
          <div style="background:var(--bg-card-2);padding:8px;border-radius:6px;font-size:12px;">${escape(o.reason)}</div>` : ''}
        <div class="sheet-actions" style="display:flex;flex-wrap:wrap;gap:6px;">
          <button class="btn btn-link" data-go-od-news>📰 该股新闻</button>
          <button class="btn btn-link" data-go-od-reviews>📊 历史复盘</button>
          <button class="btn btn-link" data-go-od-signals>🎯 历史信号</button>
        </div>
      `;
      $('#sheet-content').innerHTML = html;
      const goN = $('#sheet-content [data-go-od-news]');
      if (goN) goN.addEventListener('click', () => navigate('market', 'news', { symbolFilter: o.symbol }));
      const goR = $('#sheet-content [data-go-od-reviews]');
      if (goR) goR.addEventListener('click', () => navigate('learn', 'reviews', { symbolFilter: o.symbol }));
      const goS = $('#sheet-content [data-go-od-signals]');
      if (goS) goS.addEventListener('click', () => navigate('market', 'signals', { symbolFilter: o.symbol }));
    } catch (e) {
      $('#sheet-content').innerHTML = `<div class="empty">加载失败：${e.message}</div>`;
    }
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
      // v12.21.2: count 和 cap 同一行显示, 避免小字号下 "200" 被误读成 "20"
      const html = `<div class="pool-market-chips">${
        ['us', 'cn', 'hk'].map(m => {
          const n = byMarket[m] || 0;
          const cap = CAPS[m];
          const ratio = n / cap;
          const cls = ratio >= 0.9 ? 'usage-high' : ratio >= 0.8 ? 'usage-mid' : 'usage-low';
          return `<div class="pool-market-chip ${cls}">
            <div class="pmc-name">${NAMES[m]}</div>
            <div class="pmc-count">${n} <span class="pmc-cap-inline">/ ${cap}</span></div>
            <div class="pmc-cap">${(ratio*100).toFixed(0)}% 已用</div>
          </div>`;
        }).join('')
      }</div>`;
      const sumEl = $('#pool-market-summary');
      if (sumEl) sumEl.innerHTML = html;
    } catch (e) { console.debug('pool summary', e); }
  }

  // ─── 设置: 通知 (PR3 升级) ───
  async function renderSettingsNotify() {
    try {
      const status = await loadStatus();
      const cfg = status.config || {};
      const tgEnabled = cfg.telegram_bot_token ? '✅ 已配置' : '❌ 未配置';
      const tgEnabledCls = cfg.telegram_bot_token ? 'up' : 'down';
      $('#settings-channels').innerHTML = `
        <div class="kv-row"><span class="k">Telegram Bot</span><span class="v ${tgEnabledCls}">${tgEnabled}</span></div>
        <div class="kv-row"><span class="k">Chat ID</span><span class="v">${cfg.telegram_chat_id ? '已设置' : '未设置'}</span></div>
        <div class="kv-row"><span class="k">推送频率</span><span class="v">实时 + 4h 简报</span></div>
        <div class="kv-row"><span class="k">日报推送</span><span class="v">每天 BJ 09:00</span></div>
      `;
      // 推送类型(展示当前推送的事件类型,这些都是后端硬编码暂时不可关)
      const typeListHtml = `<div class="card section">
        <div class="section-title">📲 当前推送事件类型</div>
        <div class="kv-list">
          <div class="kv-row"><span class="k">📡 高分信号 (AI conf ≥ 75)</span><span class="v up">✅ 启用</span></div>
          <div class="kv-row"><span class="k">📥 自动开仓/加仓</span><span class="v up">✅ 启用</span></div>
          <div class="kv-row"><span class="k">🏁 平仓 / 减仓</span><span class="v up">✅ 启用</span></div>
          <div class="kv-row"><span class="k">⚡ 合约强平</span><span class="v up">✅ 启用</span></div>
          <div class="kv-row"><span class="k">🔍 复盘完成</span><span class="v up">✅ 启用</span></div>
          <div class="kv-row"><span class="k">📊 4h 持仓简报</span><span class="v up">✅ 启用</span></div>
          <div class="kv-row muted small"><span class="k">分项关闭功能将在 v12.22 上线</span><span class="v"></span></div>
        </div>
      </div>`;
      // append 到 notify subpage
      const np = document.querySelector('.subpage[data-subpage="notify"]');
      if (np && !np.querySelector('.section-title')) {
        np.insertAdjacentHTML('beforeend', typeListHtml);
      }
    } catch (e) {
      console.error('[notify]', e);
    }
  }

  // ─── 设置: LLM 配额 ───
  async function renderSettingsLLM() {
    try {
      const cost = await loadLLMCost();
      if (!cost) {
        $('#llm-detail-card').innerHTML = '<div class="empty">无法加载 LLM 数据</div>';
        return;
      }
      const today = cost.today_cost_usd || cost.today_total_usd || 0;
      const limit = cost.daily_budget || cost.daily_limit_usd || 25.0;
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

  // ─── 设置: 系统状态 (PR3 升级) ───
  async function renderSettingsSystem() {
    try {
      const [status, health] = await Promise.all([
        loadStatus(),
        fetchJSON('/api/health').catch(() => ({})),
      ]);
      const cfg = status.config || {};
      const rows = [];
      // 服务状态
      rows.push(['服务状态', '🟢 在线']);
      rows.push(['自动交易', status.enabled ? '🟢 开' : '🔴 关']);
      rows.push(['加密模式', cfg.crypto_trading_mode || 'spot_mock']);
      // 池子统计
      const pools = status.pools || [];
      rows.push(['活跃池子', pools.length]);
      const totalPos = pools.reduce((s, p) => s + (p.position_count || 0), 0);
      rows.push(['现货持仓总数', totalPos]);
      // 配额
      rows.push(['每股每日上限', `${cfg.max_daily_ops_per_symbol || 5} 次`]);
      rows.push(['同股冷却', `${Math.round((cfg.cooldown_sec || 900) / 60)} 分钟`]);
      // 后台 loop
      rows.push(['新闻拉取', '🟢 28 个源']);
      rows.push(['信号监控', '🟢 候选池自动绑定中']);
      rows.push(['SL/TP 巡检', '🟢 60s 间隔']);
      rows.push(['复盘引擎', '🟢 每 4h 单笔 + 每天周报']);
      rows.push(['教训聚合', '🟢 每 6h 自动']);
      // 健康
      if (health && health.status) rows.push(['健康检查', `🟢 ${health.status}`]);
      $('#settings-status').innerHTML = rows.map(([k, v]) =>
        `<div class="kv-row"><span class="k">${escape(k)}</span><span class="v">${escape(v)}</span></div>`
      ).join('');
    } catch (e) {
      console.error('[system]', e);
      $('#settings-status').innerHTML = '<div class="empty">加载失败</div>';
    }
  }

  // ─── PR2: 周报页 ───
  // v12.27.8 Bug 4: top_wins / top_losses 是 position_id 数组(后端 reviewer.py:806),
  //   先拉 reviews 列表建 pid → review 映射, 渲染时把 pid 还原成 symbol + pnl%
  // v12.27.11: 周报里 pid 来自 N 周前 (可能 12+ 天前), 默认 loadReviews limit=50
  //   不够覆盖 → reviewMap 找不到 → 退化截断 UUID. 改用 limit=500 (API 上限)
  //   还找不到的, 单条降级 fetch /api/trade-review/{pid}
  async function renderWeekly() {
    const container = document.querySelector('.subpage[data-subpage="weekly"]');
    if (!container) return;
    container.innerHTML = '<div class="empty">⏳ 加载周报...</div>';
    try {
      const [data, bigReviews] = await Promise.all([
        fetchJSON('/api/trade-review/weekly/list?limit=4'),
        fetchJSON('/api/trade-review?limit=500').catch(() => ({items: []})),
      ]);
      const reports = data.items || [];
      if (!reports.length) {
        container.innerHTML = `<div class="empty">
          <div class="empty-icon">📊</div>
          <div>暂无周报</div>
          <div class="small muted" style="margin-top:6px;">每周日凌晨自动生成上周综合复盘</div>
        </div>`;
        return;
      }
      // 建 pid → review 映射 (用 500 条窗口, 覆盖最近 4-8 周的周报)
      const reviewMap = {};
      for (const r of (bigReviews.items || [])) {
        reviewMap[r.position_id] = r;
      }
      // 单条降级: 找出所有周报里出现但 reviewMap 没有的 pid, 并发拉
      const allPids = new Set();
      for (const rpt of reports) {
        for (const arr of [rpt.top_wins || [], rpt.top_losses || []]) {
          for (const x of arr) {
            if (typeof x === 'string') allPids.add(x);
            else if (x && typeof x === 'object' && x.position_id) allPids.add(x.position_id);
          }
        }
      }
      const missing = [...allPids].filter(pid => !reviewMap[pid]);
      if (missing.length) {
        const fetched = await Promise.all(missing.map(pid =>
          fetchJSON('/api/trade-review/' + encodeURIComponent(pid)).catch(() => null)
        ));
        for (const r of fetched) {
          if (r && r.position_id) reviewMap[r.position_id] = r;
        }
      }
      container.innerHTML = reports.map(r => renderWeeklyReportCard(r, reviewMap)).join('');
    } catch (e) {
      console.error('[weekly]', e);
      container.innerHTML = '<div class="empty">加载周报失败</div>';
    }
  }

  // v12.27.8 Bug 4: 把任意类型的 top_wins/top_losses 元素还原成可读文本
  function _formatWeeklyTopItem(item, reviewMap, isWin) {
    // 字符串 → 假定是 position_id, 查映射
    if (typeof item === 'string') {
      const r = reviewMap && reviewMap[item];
      if (r) {
        const pct = r.realized_pnl_pct || 0;
        return `${escape(r.symbol)} <span class="${isWin?'up':'down'}">${fmtPct(pct)}</span>${r.grade ? ` <span class="grade-pill ${r.grade.toUpperCase()}" style="font-size:9px;">${r.grade.toUpperCase()}</span>` : ''}`;
      }
      // 找不到对应复盘 — 直接显示截断的 pid
      return `<span class="muted small">${escape(item.slice(0, 24))}</span>`;
    }
    // 对象 → 提取关键字段
    if (item && typeof item === 'object') {
      const sym = item.symbol || item.position_id || '?';
      const pct = item.realized_pnl_pct != null ? item.realized_pnl_pct : (item.pnl_pct != null ? item.pnl_pct : null);
      const note = item.note || item.text || item.reason || '';
      const pctStr = pct != null ? ` <span class="${isWin?'up':'down'}">${fmtPct(pct)}</span>` : '';
      return `${escape(String(sym))}${pctStr}${note ? ' — ' + escape(String(note)) : ''}`;
    }
    return escape(String(item));
  }

  // v12.27.8 Bug 4: 把 recurring_mistakes / actionable_changes 元素还原成文本
  function _formatWeeklyMistake(item) {
    if (typeof item === 'string') return escape(item);
    if (item && typeof item === 'object') {
      const txt = item.text || item.summary || item.pattern || item.description || '';
      const cnt = item.count != null ? ` (${item.count} 次)` : '';
      if (txt) return escape(String(txt)) + cnt;
      return escape(JSON.stringify(item));
    }
    return escape(String(item));
  }

  function renderWeeklyReportCard(r, reviewMap) {
    const weekStart = new Date(r.week_start * 1000);
    const weekEnd = new Date(r.week_end * 1000);
    const winRate = ((r.win_rate || 0) * 100).toFixed(1);
    const pnl = r.total_pnl_usd || 0;
    const pnlCls = pnl >= 0 ? 'up' : 'down';
    const grade = r.avg_grade || '?';
    const wins = Array.isArray(r.top_wins) ? r.top_wins : [];
    const losses = Array.isArray(r.top_losses) ? r.top_losses : [];
    const mistakes = Array.isArray(r.recurring_mistakes) ? r.recurring_mistakes : [];
    const changes = Array.isArray(r.actionable_changes) ? r.actionable_changes : [];
    return `<div class="card section">
      <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:10px;">
        <div style="font-weight:700;font-size:14px;">📊 ${weekStart.getMonth()+1}-${weekStart.getDate()} ~ ${weekEnd.getMonth()+1}-${weekEnd.getDate()}</div>
        <span class="grade-pill ${grade}" style="font-size:12px;">${grade}</span>
      </div>
      <div class="quick-stats" style="margin-bottom:14px;">
        <div class="stat-card"><div class="stat-label">总笔数</div><div class="stat-value">${r.trades_count || 0}</div></div>
        <div class="stat-card"><div class="stat-label">胜率</div><div class="stat-value">${winRate}%</div></div>
        <div class="stat-card"><div class="stat-label">胜/负</div><div class="stat-value">${r.wins||0}/${r.losses||0}</div></div>
        <div class="stat-card"><div class="stat-label">总 PnL</div><div class="stat-value ${pnlCls}">${fmtPnl(pnl)}</div></div>
      </div>
      ${r.summary ? `<div style="font-size:12px;line-height:1.6;background:rgba(255,255,255,0.03);padding:10px;border-radius:6px;margin-bottom:12px;">${escape(r.summary)}</div>` : ''}
      ${wins.length ? `
        <div style="font-size:12px;font-weight:600;color:var(--up);margin-bottom:6px;">🏆 Top 盈利</div>
        <ul style="margin:0 0 12px 0;padding-left:18px;font-size:11px;line-height:1.6;">
          ${wins.slice(0, 3).map(w => `<li>${_formatWeeklyTopItem(w, reviewMap, true)}</li>`).join('')}
        </ul>` : ''}
      ${losses.length ? `
        <div style="font-size:12px;font-weight:600;color:var(--down);margin-bottom:6px;">📉 Top 亏损</div>
        <ul style="margin:0 0 12px 0;padding-left:18px;font-size:11px;line-height:1.6;">
          ${losses.slice(0, 3).map(l => `<li>${_formatWeeklyTopItem(l, reviewMap, false)}</li>`).join('')}
        </ul>` : ''}
      ${mistakes.length ? `
        <div style="font-size:12px;font-weight:600;color:var(--warn);margin-bottom:6px;">🔁 反复出现的错误</div>
        <ul style="margin:0 0 12px 0;padding-left:18px;font-size:11px;line-height:1.6;">
          ${mistakes.slice(0, 5).map(m => `<li>${_formatWeeklyMistake(m)}</li>`).join('')}
        </ul>` : ''}
      ${changes.length ? `
        <div style="font-size:12px;font-weight:600;color:var(--accent);margin-bottom:6px;">💡 可操作的改进</div>
        <ul style="margin:0;padding-left:18px;font-size:11px;line-height:1.6;">
          ${changes.slice(0, 5).map(c => `<li>${_formatWeeklyMistake(c)}</li>`).join('')}
        </ul>` : ''}
    </div>`;
  }

  // ─── PR3: 新闻源管理 ───
  async function renderSources() {
    const container = document.querySelector('.subpage[data-subpage="sources"]');
    if (!container) return;
    container.innerHTML = '<div class="empty">⏳ 加载新闻源...</div>';
    try {
      const sources = await fetchJSON('/api/news/sources');
      if (!sources || !sources.length) {
        container.innerHTML = '<div class="empty">无新闻源数据</div>';
        return;
      }
      // 排序: 启用 + 健康 在前; 失败/禁用 在后
      sources.sort((a, b) => {
        const aHealth = (a.disabled || (a.fail_count || 0) > 5) ? 1 : 0;
        const bHealth = (b.disabled || (b.fail_count || 0) > 5) ? 1 : 0;
        if (aHealth !== bHealth) return aHealth - bHealth;
        return (b.last_success_at || 0) - (a.last_success_at || 0);
      });
      // 分类汇总
      const total = sources.length;
      const healthy = sources.filter(s => !s.disabled && (s.fail_count || 0) < 3).length;
      const warning = sources.filter(s => !s.disabled && (s.fail_count || 0) >= 3 && (s.fail_count || 0) < 10).length;
      const failed = sources.filter(s => s.disabled || (s.fail_count || 0) >= 10).length;
      let html = `<div class="quick-stats" style="margin-bottom:12px;">
        <div class="stat-card"><div class="stat-label">总数</div><div class="stat-value">${total}</div></div>
        <div class="stat-card"><div class="stat-label">健康</div><div class="stat-value up">${healthy}</div></div>
        <div class="stat-card"><div class="stat-label">告警</div><div class="stat-value warn">${warning}</div></div>
        <div class="stat-card"><div class="stat-label">失败</div><div class="stat-value down">${failed}</div></div>
      </div>`;
      html += '<div class="list">' + sources.map(renderSourceRow).join('') + '</div>';
      container.innerHTML = html;
    } catch (e) {
      console.error('[sources]', e);
      container.innerHTML = '<div class="empty">加载失败</div>';
    }
  }

  function renderSourceRow(s) {
    const failCount = s.fail_count || 0;
    const disabled = s.disabled || s.permanently_disabled;
    let cls = 'up';
    let statusIcon = '🟢';
    let statusText = '正常';
    if (disabled) { cls = 'down'; statusIcon = '🚫'; statusText = '已禁用'; }
    else if (failCount >= 10) { cls = 'down'; statusIcon = '🔴'; statusText = `失败 ${failCount} 次`; }
    else if (failCount >= 3) { cls = 'warn'; statusIcon = '🟡'; statusText = `失败 ${failCount} 次`; }
    const lastOk = s.last_success_at ? fmtRelTime(s.last_success_at) : '从未成功';
    return `<div class="row ${cls}">
      <div class="row-title">
        <div class="row-symbol">${statusIcon} ${escape(s.name || '?')}</div>
        <div class="small muted">${statusText}</div>
      </div>
      <div class="row-meta small">
        最后成功:${lastOk}
        ${s.interval ? ` · 周期 ${Math.round(s.interval/60)}m` : ''}
        ${s.last_count != null ? ` · 上次入库 ${s.last_count} 条` : ''}
      </div>
      ${s.last_error ? `<div class="row-reason">⚠️ ${escape(s.last_error).slice(0, 100)}</div>` : ''}
    </div>`;
  }


  // ═══════════════════════════════════════════════════════════
  // v12.22.0: 按需分析模块 (mobile)
  // ═══════════════════════════════════════════════════════════
  const _onDemandState = {
    lastAdvice: null,
    hasPos: false,
  };
  const OD_ACTION_LABEL = {
    hold: '继续持有', add: '加仓', reduce: '减仓 50%', close: '平仓',
    open_long: '开多', open_short: '开空', wait: '暂不建议',
  };
  const OD_ACTION_ICON = {
    hold: '✅', add: '🟢', reduce: '🟡', close: '🔴',
    open_long: '🟢', open_short: '🔴', wait: '⏸',
  };
  const OD_ACTION_COLOR_CLS = {
    hold: '', add: 'up', reduce: 'warning',
    close: 'down', open_long: 'up', open_short: 'down', wait: 'muted',
  };

  async function renderOnDemandPage() {
    const formEl = $('#ondemand-form');
    if (!formEl) return;
    if (!formEl.dataset.inited) {
      formEl.dataset.inited = '1';
      formEl.innerHTML = `
        <div class="section-title">🔍 按需分析</div>
        <div class="muted small" style="margin-bottom:8px;">输入代码 + 持仓状态 → AI 分析师建议</div>
        <div style="display:flex;gap:6px;margin-bottom:8px;">
          <input id="od-symbol" type="text" placeholder="如: ETH / AAPL / 0981"
            style="flex:1;padding:8px 10px;background:var(--bg-tertiary,#1a1a1f);border:1px solid #333;border-radius:6px;color:#fff;font-size:14px;">
          <select id="od-market" style="padding:8px 6px;background:var(--bg-tertiary,#1a1a1f);border:1px solid #333;border-radius:6px;color:#fff;font-size:13px;">
            <option value="crypto">加密</option>
            <option value="us">美股</option>
            <option value="hk">港股</option>
            <option value="cn">A股</option>
          </select>
        </div>
        <div style="display:flex;gap:0;margin-bottom:8px;">
          <button id="od-no-pos" class="seg active" data-pos="no" style="flex:1;">📈 无持仓</button>
          <button id="od-has-pos" class="seg" data-pos="yes" style="flex:1;">💼 已有持仓</button>
        </div>
        <div id="od-pos-panel" hidden style="margin-bottom:8px;padding:8px;background:var(--bg-tertiary,#1a1a1f);border-radius:6px;">
          <div style="display:flex;gap:6px;margin-bottom:6px;">
            <select id="od-pos-side" style="padding:6px 8px;background:#0d1117;border:1px solid #333;border-radius:4px;color:#fff;font-size:13px;">
              <option value="long">多</option>
              <option value="short">空</option>
            </select>
            <input id="od-pos-avg" type="number" step="any" placeholder="持仓成本价"
              style="flex:1;padding:6px 8px;background:#0d1117;border:1px solid #333;border-radius:4px;color:#fff;font-size:13px;">
            <input id="od-pos-qty" type="number" step="any" placeholder="股数/个数"
              style="flex:1;padding:6px 8px;background:#0d1117;border:1px solid #333;border-radius:4px;color:#fff;font-size:13px;">
          </div>
          <div style="display:flex;gap:6px;">
            <input id="od-pos-sl" type="number" step="any" placeholder="止损 (留空 AI 给)"
              style="flex:1;padding:6px 8px;background:#0d1117;border:1px solid #333;border-radius:4px;color:#fff;font-size:12px;">
            <input id="od-pos-tp" type="number" step="any" placeholder="止盈 (留空 AI 给)"
              style="flex:1;padding:6px 8px;background:#0d1117;border:1px solid #333;border-radius:4px;color:#fff;font-size:12px;">
            <button id="od-pos-fetch" class="btn" style="padding:6px 10px;font-size:11px;">📥 账户</button>
          </div>
        </div>
        <button id="od-analyze-btn" class="btn" style="width:100%;padding:10px;font-size:14px;font-weight:600;background:var(--up,#26a69a);color:#fff;">🚀 开始分析</button>
      `;
      // 绑定
      $$('#ondemand-form .seg[data-pos]').forEach(b => {
        b.addEventListener('click', () => {
          const isYes = b.dataset.pos === 'yes';
          $$('#ondemand-form .seg[data-pos]').forEach(x => x.classList.toggle('active', x === b));
          $('#od-pos-panel').hidden = !isYes;
          _onDemandState.hasPos = isYes;
        });
      });
      $('#od-pos-fetch')?.addEventListener('click', _odFetchPos);
      $('#od-analyze-btn')?.addEventListener('click', _odAnalyze);
    }
  }

  async function _odFetchPos() {
    const sym = ($('#od-symbol').value || '').trim();
    const mkt = $('#od-market').value;
    if (!sym) { toast('先填代码', 'down'); return; }
    try {
      const resp = await fetch('/api/positions');
      const items = await resp.json();
      const arr = Array.isArray(items) ? items : (items.items || []);
      const sUp = sym.toUpperCase();
      const found = arr.find(p => {
        const ps = (p.symbol || '').toUpperCase();
        return (p.market || '').toLowerCase() === mkt &&
          (ps === sUp || ps.startsWith(sUp + '-') || ps.startsWith(sUp + '.'));
      });
      if (!found) { toast(`未找到 ${sym}(${mkt}) 持仓`, 'muted'); return; }
      $('#od-pos-side').value = found.side || 'long';
      $('#od-pos-avg').value = found.avg_cost || '';
      $('#od-pos-qty').value = found.quantity || '';
      $('#od-pos-sl').value = found.ai_stop_loss || '';
      $('#od-pos-tp').value = found.ai_take_profit || '';
      toast('已读取持仓', 'up');
    } catch (e) {
      toast(`读取失败: ${e.message}`, 'down');
    }
  }

  async function _odAnalyze() {
    const sym = ($('#od-symbol').value || '').trim();
    const mkt = $('#od-market').value;
    if (!sym) { toast('请填代码', 'down'); return; }
    let position = null;
    if (_onDemandState.hasPos) {
      const qty = parseFloat($('#od-pos-qty').value || '0');
      const avg = parseFloat($('#od-pos-avg').value || '0');
      if (!(qty > 0 && avg > 0)) { toast('请填均价和数量', 'down'); return; }
      const slV = parseFloat($('#od-pos-sl').value || '');
      const tpV = parseFloat($('#od-pos-tp').value || '');
      position = {
        side: $('#od-pos-side').value || 'long',
        avg_cost: avg, quantity: qty,
        stop_loss: isNaN(slV) ? null : slV,
        take_profit: isNaN(tpV) ? null : tpV,
      };
    }
    const btn = $('#od-analyze-btn');
    btn.disabled = true; btn.textContent = '⏳ 分析中...';
    $('#ondemand-result').innerHTML = `
      <div class="card section">
        <div style="text-align:center;padding:16px;">🔄 正在分析 ${escape(sym)}...<br>
        <span class="muted small">收集数据 + AI 分析师 (~20-30s)</span></div>
      </div>`;
    try {
      const resp = await fetch('/api/on-demand/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: sym, market: mkt, has_position: _onDemandState.hasPos, position }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      _onDemandState.lastAdvice = data;
      _odRenderResult(data);
    } catch (e) {
      $('#ondemand-result').innerHTML = `<div class="card section" style="border-left:3px solid var(--down,#ef5350);">
        <div style="color:var(--down,#ef5350);">❌ ${escape(e.message)}</div></div>`;
    } finally {
      btn.disabled = false; btn.textContent = '🚀 开始分析';
    }
  }

  function _odRenderResult(data) {
    const advice = data.advice || {};
    const t0 = data.t0_snapshot || {};
    const action = advice.action || 'wait';
    const conf = advice.confidence || 0;
    const cls = OD_ACTION_COLOR_CLS[action] || '';
    const sigs = advice.supporting_signals || [];
    const counters = advice.counter_signals || [];
    const risks = advice.key_risks || [];
    const watch = advice.watch_signals || [];
    const aborts = advice.abort_conditions || [];
    const exit = advice.exit_strategy || {};
    const entry = advice.entry_strategy || {};
    const sizing = advice.position_sizing || {};
    const pos = data.position;

    const sym = ($('#od-symbol').value || '').trim();
    const mkt = $('#od-market').value;
    let html = `
      <div class="card section">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;">
          <div>
            <div style="font-size:15px;font-weight:600;">${escape(sym)} <span class="small muted">${escape(mkt)}</span></div>
            <div class="small muted">T0: ${t0.price ? t0.price : '—'} · ${t0.ts ? new Date(t0.ts).toLocaleTimeString() : ''}</div>
          </div>
          <div style="text-align:right;">
            <div class="${cls}" style="font-size:16px;font-weight:600;">${OD_ACTION_ICON[action]} ${escape(OD_ACTION_LABEL[action] || action)}</div>
            <div class="small">把握度 ${conf}/100</div>
            ${advice.time_horizon ? `<div class="small muted">${escape(advice.time_horizon)}</div>` : ''}
          </div>
        </div>
      </div>`;

    if (pos) {
      const cur = t0.price || 0;
      const cost = pos.avg_cost || 0;
      const isLong = (pos.side || 'long') === 'long';
      const pnlPct = (cost > 0 && cur > 0) ? ((isLong ? cur - cost : cost - cur) / cost * 100) : 0;
      const pnlCls = pnlPct >= 0 ? 'up' : 'down';
      html += `<div class="card section" style="font-size:12px;">
        📌 你的${pos.type === 'swap' ? '合约' : '现货'}持仓: ${isLong ? '多' : '空'} · ${pos.quantity} @ ${pos.avg_cost} ·
        <span class="${pnlCls}">${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%</span>
      </div>`;
    }

    html += `<div class="card section">
      <div class="muted small">💭 核心逻辑</div>
      <div style="margin-top:4px;">${escape(advice.main_thesis || '')}</div>
    </div>`;

    if (entry && entry.ideal_price) {
      html += `<div class="card section">
        <div class="muted small">💡 入场策略</div>
        <div style="margin-top:4px;">理想入场: <strong>${entry.ideal_price}</strong>${entry.acceptable_range ? ` · 区间 ${entry.acceptable_range[0]}~${entry.acceptable_range[1]}` : ''}</div>
        <div class="small">方式: ${escape(entry.approach || '市价')} · 仓位 <strong>${sizing.suggested_pct || 0}%</strong></div>
        ${sizing.reasoning ? `<div class="muted small" style="margin-top:4px;">${escape(sizing.reasoning)}</div>` : ''}
      </div>`;
    }

    if (exit && (exit.stop_loss || exit.take_profit_1)) {
      html += `<div class="card section">
        <div class="muted small">🎯 风控</div>
        <div style="margin-top:4px;">
          ${exit.stop_loss ? `止损: <span class="down">${exit.stop_loss}</span>` : ''}
          ${exit.take_profit_1 ? ` · 目标 1: <span class="up">${exit.take_profit_1}</span>` : ''}
          ${exit.take_profit_2 ? ` · 目标 2: <span class="up">${exit.take_profit_2}</span>` : ''}
        </div>
        ${exit.trail_logic ? `<div class="muted small">${escape(exit.trail_logic)}</div>` : ''}
      </div>`;
    }

    html += _odRenderCollapse('📈 支撑信号', sigs.map(s => `<div>• ${escape(s.signal || '')}${s.weight ? ` <span class="muted small">[${escape(s.weight)}]</span>` : ''}${s.data ? `<br><span class="muted small">${escape(s.data)}</span>` : ''}</div>`).join('<hr style="border:none;border-top:1px dashed #333;margin:4px 0;">'));
    html += _odRenderCollapse('⚠️ 反向信号', counters.map(s => `<div>• ${escape(s.signal || '')}${s.data ? `<br><span class="muted small">${escape(s.data)}</span>` : ''}</div>`).join('<hr style="border:none;border-top:1px dashed #333;margin:4px 0;">'));
    html += _odRenderCollapse('🚨 关键风险', risks.map(r => `<div>• <span class="down">${escape(r.risk || '')}</span>${r.trigger ? `<br><span class="muted small">触发: ${escape(r.trigger)}</span>` : ''}</div>`).join('<hr style="border:none;border-top:1px dashed #333;margin:4px 0;">'));
    if (watch.length) html += _odRenderCollapse('👀 需关注信号', watch.map(t => `<div>• ${escape(t)}</div>`).join(''));
    if (aborts.length) html += _odRenderCollapse('⛔ 执行前再核对', aborts.map(t => `<div>• ${escape(t)}</div>`).join(''));
    if (advice.professional_summary) html += _odRenderCollapse('📄 完整报告', `<div style="white-space:pre-wrap;">${escape(advice.professional_summary)}</div>`);
    if (data.missing_data && data.missing_data.length) {
      html += `<div class="card section" style="border-left:3px solid var(--warning,#ffa726);font-size:12px;">⚠️ 数据缺失: ${data.missing_data.join(', ')}</div>`;
    }

    const execLabel = action === 'hold' ? '更新止损/止盈' : action === 'wait' ? '暂无可执行操作' : `按建议${OD_ACTION_LABEL[action] || action}`;
    const execDisabled = action === 'wait';
    html += `<button id="od-exec-btn" class="btn" ${execDisabled ? 'disabled' : ''} style="width:100%;padding:12px;font-size:15px;font-weight:600;margin-top:8px;background:${execDisabled ? '#444' : 'var(--up,#26a69a)'};color:#fff;">${OD_ACTION_ICON[action]} ${escape(execLabel)}</button>`;

    $('#ondemand-result').innerHTML = html;
    $('#od-exec-btn')?.addEventListener('click', _odExecute);
  }

  function _odRenderCollapse(title, inner) {
    if (!inner || !inner.trim()) return '';
    return `<details class="card section" style="padding:0;">
      <summary style="padding:10px 12px;cursor:pointer;">${title}</summary>
      <div style="padding:0 12px 10px;font-size:12px;line-height:1.6;">${inner}</div>
    </details>`;
  }

  async function _odExecute() {
    const data = _onDemandState.lastAdvice;
    if (!data || !data.advice) { toast('请先分析', 'down'); return; }
    const advice = data.advice;
    const aid = advice.advice_id;
    if (!aid) { toast('advice_id 缺失', 'down'); return; }
    const action = advice.action;
    const ok = confirm(`确认执行?\n${OD_ACTION_LABEL[action] || action}\n把握度 ${advice.confidence}/100\n仓位 ${(advice.position_sizing||{}).suggested_pct||0}%\n\n执行前会重读实时价检查漂移`);
    if (!ok) return;
    const btn = $('#od-exec-btn');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ 执行中...'; }
    try {
      const resp = await fetch('/api/on-demand/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ advice_id: aid, confirm: true }),
      });
      const r = await resp.json();
      if (!resp.ok) throw new Error(r.detail || `HTTP ${resp.status}`);
      toast(`✅ 执行成功 @ ${r.executed_price}`, 'up');
      if (btn) { btn.style.background = 'var(--up,#26a69a)'; btn.textContent = '✅ 已执行'; }
    } catch (e) {
      toast(`❌ ${e.message}`, 'down');
      if (btn) { btn.disabled = false; btn.textContent = '🔄 重试'; }
    }
  }


  // 自动 30s 轮询当前 tab
  setInterval(() => {
    Object.keys(_state.cache).forEach(k => _state.cache[k] = null);
    refresh();
  }, 30000);

  // v12.25.3 Phase D: 启动时尝试从 hash 恢复路由 (深链支持)
  // URL 例: #/trade/spot, #/market/signals?s=AAPL&f=confirm
  if (location.hash && location.hash.length > 2) {
    const r = applyHash(location.hash);
    if (r) {
      _hashSyncing = true;
      switchTab(r.tab);
      setTimeout(() => { _hashSyncing = false; }, 100);
    } else {
      refresh();
    }
  } else {
    refresh();
  }
})();
