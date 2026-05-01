/* ============================================================
   Portfolio 模块 — 持仓管理面板 (Phase 5)

   功能：
   - 持仓列表（含浮动盈亏）
   - 手动添加/编辑/删除持仓
   - 接收 WebSocket position_advice 推送 + Toast
   - 查看建议历史
   ============================================================ */

const Portfolio = (function () {
  let _items = [];
  let _latestAdvices = {};  // position_id → {advice, reason, advised_at, urgency}
  let _autoRefreshTimer = null;

  const ADVICE_LABEL = { hold: '继续持有', reduce: '减仓', add: '加仓', close: '平仓' };
  const ADVICE_COLOR = {
    hold: 'var(--text-secondary)',
    reduce: 'var(--color-warning)',
    add: 'var(--color-up)',
    close: 'var(--color-down)',
  };

  let _inited = false;
  function init() {
    if (_inited) { console.warn('[Portfolio] 已初始化，跳过重复 init'); return; }
    _inited = true;
    _ensureDom();
    if (typeof ws !== 'undefined' && ws) {
      ws.on('position_advice', (msg) => {
        if (!msg || !msg.data) return;
        const d = msg.data;
        if (typeof showToast === 'function') {
          const adviceLabel = { hold: '继续持有', reduce: '建议减仓', add: '可考虑加仓', close: '建议平仓' }[d.advice] || d.advice;
          const urgencyClass = d.urgency === 'high' ? 'error' : d.urgency === 'medium' ? 'warning' : 'info';
          showToast(`💼 ${d.symbol}: ${adviceLabel} - ${d.reason.substring(0, 50)}`, urgencyClass, 8000);
        }
        refresh();
      });
      // 自动交易事件推送（专用通道）
      ws.on('auto_trade', (msg) => {
        if (!msg || !msg.data) return;
        const d = msg.data;
        if (typeof showToast === 'function') {
          showToast(`🤖 ${d.reason || (d.action + ' ' + d.symbol)} · $${d.amount_usd}`, 'info', 5000);
        }
        _updateAutoTradeStatus();
        if (_viewMode === 'autolog') _renderAutoTradeLog();
        else refresh();
      });
    }
    refresh();
    _autoRefreshTimer = setInterval(refresh, 30000);  // 30 秒一次（原 2min 太慢，盘中实时价应接近同步）
    if (window.__visibilityHandlers) {
      window.__visibilityHandlers.push(({ hidden }) => {
        if (hidden && _autoRefreshTimer) { clearInterval(_autoRefreshTimer); _autoRefreshTimer = null; }
        // v11.5 修复: visibility 恢复后保持 30s 节奏（之前误写成 120000 = 2min）
        else if (!hidden && !_autoRefreshTimer) { refresh(); _autoRefreshTimer = setInterval(refresh, 30000); }
      });
    }
    console.log('[Portfolio] 已初始化');
  }

  function _ensureDom() {
    const pane = document.querySelector('.bottom-pane[data-pane="portfolio"]');
    if (!pane || pane.dataset.inited) return;
    pane.dataset.inited = '1';
    pane.innerHTML = `
      <div class="portfolio-toolbar" style="display:flex;gap:8px;padding:6px 10px;border-bottom:1px solid var(--border-secondary);align-items:center;flex-wrap:wrap;">
        <div class="port-view-tabs" style="display:flex;gap:0;">
          <button class="port-view-tab active" data-view="positions" style="padding:4px 12px;font-size:12px;border:1px solid var(--border-secondary);border-radius:4px 0 0 4px;background:var(--bg-tertiary);color:var(--text-primary);cursor:pointer;">💼 当前持仓</button>
          <button class="port-view-tab" data-view="history" style="padding:4px 12px;font-size:12px;border:1px solid var(--border-secondary);border-left:none;background:transparent;color:var(--text-secondary);cursor:pointer;" title="按单分组：每个 position 一张卡，包含从开到现在的所有 开/加/减/平 操作 + 盈亏">📊 按单历史</button>
          <button class="port-view-tab" data-view="fills" style="padding:4px 12px;font-size:12px;border:1px solid var(--border-secondary);border-left:none;background:transparent;color:var(--text-secondary);cursor:pointer;" title="成交流水：时间倒序显示所有已成交的 开/加/减/平 记录">📜 成交流水</button>
          <button class="port-view-tab" data-view="rejects" style="padding:4px 12px;font-size:12px;border:1px solid var(--border-secondary);border-left:none;border-radius:0 4px 4px 0;background:transparent;color:var(--text-secondary);cursor:pointer;" title="被拒绝的下单尝试（如加仓阈值、冷却期、诊断缺失等）">⏸ 拒单</button>
        </div>
        <label style="display:flex;align-items:center;gap:6px;font-size:12px;margin-left:10px;padding:3px 10px;background:var(--bg-tertiary);border-radius:14px;cursor:pointer;" title="开启后 AI confirm 信号 + rating=buy 会自动模拟下单">
          <input type="checkbox" id="auto-trade-toggle" style="margin:0;">
          <span id="auto-trade-label">🤖 自动交易: <strong style="color:var(--color-down);">关闭</strong></span>
        </label>
        <button id="telegram-config-btn" class="btn btn-sm" style="font-size:11px;" title="配置 Telegram 通知（免费、无量限）">✈️ Telegram</button>
        <button id="port-backfill-targets-btn" class="btn btn-sm" style="font-size:11px;margin-left:auto;" title="为所有缺 AI 止盈/止损的持仓主动调用 AI 补全（手动添加的持仓尤其需要）">🎯 补 SL/TP</button>
        <button id="port-add-btn" class="btn btn-primary btn-sm">+ 添加持仓</button>
        <button id="port-refresh-btn" class="btn btn-sm" title="刷新持仓 + AI建议">🔄</button>
        <span id="port-status" style="font-size:11px;color:var(--text-tertiary);width:100%;text-align:right;margin-top:2px;"></span>
      </div>

      <!-- ════ Hero 账户面板（仅在 positions 视图显示） ════ -->
      <div id="port-hero" class="port-hero" style="display:block;padding:10px 12px 8px;background:linear-gradient(180deg,var(--bg-secondary) 0%,var(--bg-primary) 100%);border-bottom:1px solid var(--border-secondary);"></div>

      <!-- ════ 市场过滤 Tab（仅在 positions 视图显示） ════ -->
      <div id="port-market-filter" style="display:flex;gap:6px;padding:6px 12px;border-bottom:1px solid var(--border-secondary);align-items:center;font-size:11px;"></div>

      <div class="portfolio-add-form" style="display:none;padding:8px;background:var(--bg-tertiary);border-bottom:1px solid var(--border-secondary);">
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
          <input id="port-add-symbol" class="input" placeholder="品种 (如 BTC-USDT / NVDA / 600519)" style="width:180px;">
          <select id="port-add-market" class="select" style="width:90px;">
            <option value="crypto">加密</option>
            <option value="us">美股</option>
            <option value="hk">港股</option>
            <option value="cn">A股</option>
          </select>
          <input id="port-add-qty" type="number" step="any" class="input" placeholder="数量" style="width:100px;">
          <input id="port-add-cost" type="number" step="any" class="input" placeholder="成本价" style="width:100px;">
          <input id="port-add-notes" class="input" placeholder="备注 (可选)" style="flex:1;">
          <button id="port-add-submit" class="btn btn-primary btn-sm">确认</button>
          <button id="port-add-cancel" class="btn btn-sm">取消</button>
        </div>
      </div>
      <div class="portfolio-list" style="overflow-y:auto;flex:1;min-height:0;"></div>
    `;
    // 不在这里设 display；让 .bottom-pane.active CSS 规则控制可见性，否则会覆盖 tab 切换
    pane.style.flexDirection = 'column';
    const $ = (s) => pane.querySelector(s);
    $('#port-refresh-btn').addEventListener('click', refresh);
    $('#port-add-btn').addEventListener('click', () => {
      const f = $('.portfolio-add-form');
      f.style.display = f.style.display === 'none' ? 'block' : 'none';
    });
    $('#port-add-cancel').addEventListener('click', () => {
      $('.portfolio-add-form').style.display = 'none';
    });
    $('#port-add-submit').addEventListener('click', _onAddSubmit);
    // 视图切换 持仓 / 自动单日志
    pane.querySelectorAll('.port-view-tab').forEach(t => {
      t.addEventListener('click', () => {
        const mode = t.dataset.view;
        pane.querySelectorAll('.port-view-tab').forEach(x => {
          const on = x.dataset.view === mode;
          x.classList.toggle('active', on);
          x.style.background = on ? 'var(--bg-tertiary)' : 'transparent';
          x.style.color = on ? 'var(--text-primary)' : 'var(--text-secondary)';
        });
        _viewMode = mode;
        // Hero 和市场过滤栏只在 positions 视图显示
        const hero = pane.querySelector('#port-hero');
        const mfilter = pane.querySelector('#port-market-filter');
        if (hero) hero.style.display = mode === 'positions' ? 'block' : 'none';
        if (mfilter) mfilter.style.display = mode === 'positions' ? 'flex' : 'none';
        if (mode === 'history') _renderAutoTradeLog();
        else if (mode === 'fills') _renderFills();
        else if (mode === 'rejects') _renderRejects();
        else render();
      });
    });
    // 自动交易开关
    $('#auto-trade-toggle').addEventListener('change', async (e) => {
      try {
        await fetch('/api/auto-trade/toggle', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: e.target.checked }),
        });
        _updateAutoTradeStatus();
      } catch (err) {
        if (typeof showToast === 'function') showToast(`切换失败: ${err.message}`, 'error');
        e.target.checked = !e.target.checked;
      }
    });
    // Telegram 配置入口
    $('#telegram-config-btn').addEventListener('click', _showTelegramModal);
    // 补 SL/TP 按钮
    $('#port-backfill-targets-btn').addEventListener('click', () => _backfillTargets(false));
    _updateAutoTradeStatus();
    // 30 秒刷新账户概览；hidden 时暂停避免空跑
    let _acctTimer = setInterval(_updateAutoTradeStatus, 30000);
    if (window.__visibilityHandlers) {
      window.__visibilityHandlers.push(({ hidden }) => {
        if (hidden) { clearInterval(_acctTimer); _acctTimer = null; }
        else if (!_acctTimer) { _updateAutoTradeStatus(); _acctTimer = setInterval(_updateAutoTradeStatus, 30000); }
      });
    }
  }

  let _viewMode = 'positions';
  let _marketFilter = 'all';   // all | crypto | us | hk | cn
  let _lastAccount = null;     // 缓存账户数据（market 过滤切换时不重拉）

  function _fmtUsd(v, decimals = 2) {
    if (v == null || !isFinite(v)) return '—';
    const sign = v < 0 ? '-' : '';
    const abs = Math.abs(v);
    // 用千分位分隔显示完整数值（不再用 K/M 缩写）
    const fixed = abs.toFixed(decimals);
    const [int, dec] = fixed.split('.');
    const withSep = int.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return dec != null ? `${sign}$${withSep}.${dec}` : `${sign}$${withSep}`;
  }

  function _renderHero(account, pools) {
    const hero = document.getElementById('port-hero');
    if (!hero) return;
    if (!account) {
      hero.innerHTML = '';
      return;
    }
    _lastAccount = account;
    const a = account;
    pools = pools || [];
    // v12.0 修正：总览数字应该是 3 池 USD 等值的累加，而不是旧 legacy account 字段
    let equity = 0, pnlUsd = 0, unrealized = 0, realized = 0, initial = 0, cash = 0, posValue = 0;
    if (pools.length) {
      for (const p of pools) {
        const fx = p.fx_to_usd || 1;
        equity += p.equity_usd || 0;
        pnlUsd += p.pnl_usd || 0;
        unrealized += (p.unrealized_pnl || 0) * fx;
        realized += (p.realized_pnl || 0) * fx;
        initial += (p.initial_capital || 0) * fx;
        cash += (p.cash || 0) * fx;
        posValue += (p.positions_value || 0) * fx;
      }
    } else {
      // fallback：无 pools 数据时用旧 account 字段
      equity = (a.cash_usd || 0) + (a.positions_value_usd || 0);
      pnlUsd = a.pnl_usd || 0;
      unrealized = a.unrealized_pnl_usd || 0;
      realized = a.realized_pnl_usd || 0;
      initial = a.initial_capital_usd || 1;
      cash = a.cash_usd || 0;
      posValue = a.positions_value_usd || 0;
    }
    const pnlPct = initial > 0 ? (pnlUsd / initial * 100) : 0;
    const utilRatio = equity > 0 ? (posValue / equity) * 100 : 0;

    const pnlColor = pnlUsd >= 0 ? 'var(--color-up)' : 'var(--color-down)';
    const unColor = unrealized >= 0 ? 'var(--color-up)' : 'var(--color-down)';
    const realColor = realized >= 0 ? 'var(--color-up)' : 'var(--color-down)';
    const pnlBg = pnlUsd >= 0 ? 'rgba(76,175,80,0.10)' : 'rgba(215,90,90,0.10)';

    // 健康指示灯
    const lights = [];
    if ((a.manual_position_cost_usd || 0) > 0) {
      lights.push(`<span class="port-light port-light-warn" title="手动持仓总成本 $${a.manual_position_cost_usd.toFixed(2)}（没扣账户现金，盈亏单独算）">⚠ 含手动持仓</span>`);
    }
    if ((a.orphan_amount_usd || 0) > 0) {
      lights.push(`<span class="port-light port-light-danger" title="有 open 记录但 position 已被删除且无 close 记录，金额 $${a.orphan_amount_usd.toFixed(2)}，通常是手动清仓造成的">⚠ 孤儿交易 ${_fmtUsd(a.orphan_amount_usd)}</span>`);
    }
    if (utilRatio > 80) {
      lights.push(`<span class="port-light port-light-warn" title="持仓占总权益 ${utilRatio.toFixed(0)}%，超 80%">⚠ 仓位偏重 ${utilRatio.toFixed(0)}%</span>`);
    } else if (utilRatio > 0 && utilRatio < 20) {
      lights.push(`<span class="port-light port-light-info" title="持仓占总权益仅 ${utilRatio.toFixed(0)}%，可考虑加大仓位">空仓多 ${utilRatio.toFixed(0)}%</span>`);
    }
    const lightsHtml = lights.length ? `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:6px;">${lights.join('')}</div>` : '';

    // v12.0: 3 资金池子卡片（渲染在总览之下）
    const POOL_EMOJI = { us_hk: '🌎', cn: '🇨🇳', crypto: '🪙' };
    const poolCards = pools.map(p => {
      const pPnlColor = p.pnl >= 0 ? 'var(--color-up)' : 'var(--color-down)';
      const pUnColor = p.unrealized_pnl >= 0 ? 'var(--color-up)' : 'var(--color-down)';
      const pRealColor = p.realized_pnl >= 0 ? 'var(--color-up)' : 'var(--color-down)';
      const pBg = p.pnl >= 0 ? 'rgba(76,175,80,0.06)' : 'rgba(215,90,90,0.06)';
      const pUtil = p.equity > 0 ? (p.positions_value / p.equity * 100) : 0;
      const ccy = p.currency || 'USD';
      const fmtLocal = v => {
        const sign = v < 0 ? '-' : '';
        const abs = Math.abs(v);
        const symbol = ccy === 'USD' ? '$' : (ccy === 'CNY' ? '¥' : '');
        const fixed = abs.toFixed(2);
        const [intP, decP] = fixed.split('.');
        const withSep = intP.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
        return `${sign}${symbol}${withSep}.${decP}`;
      };
      return `
        <div class="port-hero-card" style="background:${pBg};border-left:3px solid ${pPnlColor};">
          <div class="port-hero-label">${POOL_EMOJI[p.pool_id]||''} ${p.name} (${ccy})</div>
          <div style="display:flex;justify-content:space-between;align-items:baseline;margin-top:2px;">
            <div>
              <div class="port-hero-value-md">${fmtLocal(p.equity)}</div>
              <div class="oc-text-xs oc-muted">权益 · ≈ ${_fmtUsd(p.equity_usd || p.equity)}</div>
            </div>
            <div style="text-align:right;">
              <div style="color:${pPnlColor};font-weight:700;font-size:14px;">${p.pnl>=0?'+':''}${fmtLocal(p.pnl)}</div>
              <div class="oc-text-xs" style="color:${pPnlColor};">${p.pnl_pct>=0?'+':''}${p.pnl_pct.toFixed(2)}%</div>
            </div>
          </div>
          <div class="oc-text-xs oc-muted" style="margin-top:6px;display:flex;justify-content:space-between;">
            <span>现金 <strong style="color:var(--text-primary);">${fmtLocal(p.cash)}</strong></span>
            <span>持仓 <strong style="color:var(--text-primary);">${fmtLocal(p.positions_value)}</strong></span>
          </div>
          <div class="port-hero-bar" style="margin-top:4px;">
            <div class="port-hero-bar-fill" style="width:${Math.min(100, pUtil).toFixed(1)}%;background:${pUtil>80?'var(--color-warning)':'var(--color-accent)'};"></div>
          </div>
          <div class="oc-text-xs oc-muted" style="margin-top:2px;display:flex;justify-content:space-between;">
            <span>仓位率 ${pUtil.toFixed(0)}%</span>
            <span>浮盈 <span style="color:${pUnColor};">${p.unrealized_pnl>=0?'+':''}${fmtLocal(p.unrealized_pnl)}</span> · 已实现 <span style="color:${pRealColor};">${p.realized_pnl>=0?'+':''}${fmtLocal(p.realized_pnl)}</span></span>
          </div>
        </div>`;
    }).join('');

    hero.innerHTML = `
      <div class="port-hero-grid" style="grid-template-columns:repeat(2, minmax(180px, 1fr));margin-bottom:8px;">
        <!-- 总权益（USD 等值） -->
        <div class="port-hero-card port-hero-equity">
          <div class="port-hero-label">总权益 (USD 等值)</div>
          <div class="port-hero-value-lg">${_fmtUsd(equity)}</div>
          <div style="font-size:10px;color:var(--text-tertiary);margin-top:2px;">
            初始 ${_fmtUsd(initial)} · 收益率
            <span style="color:${pnlColor};font-weight:600;">${pnlPct>=0?'+':''}${pnlPct.toFixed(2)}%</span>
          </div>
        </div>
        <!-- 累计盈亏（USD 等值） -->
        <div class="port-hero-card" style="background:${pnlBg};border-left:3px solid ${pnlColor};">
          <div class="port-hero-label">总累计盈亏 (USD 等值)</div>
          <div class="port-hero-value-lg" style="color:${pnlColor};">${pnlUsd>=0?'+':''}${_fmtUsd(pnlUsd)}</div>
          <div style="font-size:10px;color:var(--text-tertiary);margin-top:2px;">
            浮盈 <span style="color:${unColor};font-weight:600;">${unrealized>=0?'+':''}${_fmtUsd(unrealized)}</span>
            · 已实现 <span style="color:${realColor};font-weight:600;">${realized>=0?'+':''}${_fmtUsd(realized)}</span>
          </div>
        </div>
      </div>
      ${pools.length ? `
      <div class="port-hero-grid" style="grid-template-columns:repeat(3, minmax(220px, 1fr));">
        ${poolCards}
      </div>` : ''}
      ${lightsHtml}
    `;
  }

  // v11.3 僵尸资金提示（持仓 ≥ 14 天 + 浮盈 |%| < 5）
  function _renderZombieHint(a) {
    const z = a.zombie_capital_usd || 0;
    const cnt = a.zombie_count || 0;
    if (z <= 0 || cnt <= 0) return '';
    const equity = (a.cash_usd || 0) + (a.positions_value_usd || 0);
    const pct = equity > 0 ? (z / equity * 100) : 0;
    const color = pct > 30 ? 'var(--color-down)' : pct > 15 ? 'var(--color-warning)' : 'var(--text-tertiary)';
    const tip = `${cnt} 笔持仓 ≥ 14 天且浮盈 ±5% 内（资金停滞）；系统每 1h 自动减半释放预算`;
    return `<span style="color:${color};margin-left:6px;" title="${tip}">· 💤 僵尸 ${_fmtUsd(z)} (${pct.toFixed(0)}%)</span>`;
  }

  async function _updateAutoTradeStatus() {
    try {
      const r = await fetch('/api/auto-trade/status');
      if (!r.ok) return;
      const d = await r.json();
      const toggle = document.getElementById('auto-trade-toggle');
      const label = document.getElementById('auto-trade-label');
      if (toggle) toggle.checked = !!d.enabled;
      if (label) {
        label.innerHTML = `🤖 自动交易: <strong style="color:${d.enabled?'var(--color-up)':'var(--color-down)'};">${d.enabled?'开启':'关闭'}</strong>`;
      }
      if (d.account) _renderHero(d.account, d.pools || []);
    } catch {}
  }

  function _renderMarketFilter() {
    const el = document.getElementById('port-market-filter');
    if (!el) return;
    // 按市场分组统计
    const counts = { all: _items.length, crypto: 0, us: 0, hk: 0, cn: 0 };
    let totalPnl = 0;
    for (const p of _items) {
      counts[p.market] = (counts[p.market] || 0) + 1;
      if (p.pnl_usd != null && isFinite(p.pnl_usd)) totalPnl += Number(p.pnl_usd);
    }
    const PILLS = [
      { key: 'all',    label: '全部',  emoji: '📋' },
      { key: 'crypto', label: '加密',  emoji: '🪙' },
      { key: 'us',     label: '美股',  emoji: '🇺🇸' },
      { key: 'hk',     label: '港股',  emoji: '🇭🇰' },
      { key: 'cn',     label: 'A股',   emoji: '🇨🇳' },
    ];
    el.innerHTML = PILLS.map(p => {
      const active = _marketFilter === p.key;
      const cnt = counts[p.key] || 0;
      const dim = cnt === 0 && p.key !== 'all';
      return `<button class="port-mfilter-pill ${active?'active':''}" data-market="${p.key}"
        style="display:inline-flex;align-items:center;gap:5px;padding:4px 10px;font-size:11px;border-radius:14px;cursor:pointer;border:1px solid ${active?'var(--color-accent)':'var(--border-secondary)'};background:${active?'rgba(74,158,255,0.12)':'transparent'};color:${dim?'var(--text-tertiary)':active?'var(--color-accent)':'var(--text-primary)'};font-weight:${active?'600':'400'};">
        <span>${p.emoji}</span>
        <span>${p.label}</span>
        <span style="background:${active?'var(--color-accent)':'var(--bg-tertiary)'};color:${active?'#fff':'var(--text-secondary)'};padding:0 6px;border-radius:8px;font-size:10px;min-width:16px;text-align:center;">${cnt}</span>
      </button>`;
    }).join('') + `
      <span style="margin-left:auto;font-size:10px;color:var(--text-tertiary);">
        本页合计: <span style="color:${totalPnl>=0?'var(--color-up)':'var(--color-down)'};font-weight:600;">${totalPnl>=0?'+':''}${_fmtUsd(totalPnl)}</span>
      </span>
    `;
    el.querySelectorAll('.port-mfilter-pill').forEach(btn => {
      btn.addEventListener('click', () => {
        _marketFilter = btn.dataset.market;
        _renderMarketFilter();
        render();
      });
    });
  }

  // action + side 组合出具体显示
  function _autoActionDisplay(action, side) {
    const isLong = (side || 'long') === 'long';
    const map = {
      open: { long: { t: '📥 开多', c: 'var(--color-up)' },    short: { t: '🔽 开空', c: 'var(--color-down)' } },
      add:  { long: { t: '➕ 加多', c: 'var(--color-up)' },    short: { t: '➕ 加空', c: 'var(--color-down)' } },
      reduce:{long: { t: '➖ 减多', c: 'var(--color-warning)'},short: { t: '➖ 减空', c: 'var(--color-warning)' } },
      close:{ long: { t: '🏁 平多', c: 'var(--color-down)' },  short: { t: '🏁 平空', c: 'var(--color-up)' } },
    };
    const x = map[action];
    if (!x) return { t: action, c: 'var(--text-secondary)' };
    return isLong ? x.long : x.short;
  }

  // —— OKX 风格：成交流水 (Fills)。时间倒序显示所有 status=executed 的 open/add/reduce/close
  async function _renderFills() {
    const list = document.querySelector('.bottom-pane[data-pane="portfolio"] .portfolio-list');
    if (!list) return;
    list.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-tertiary);">⏳ 加载...</div>';
    try {
      const r = await fetch('/api/auto-trade/log?limit=500&status=executed');
      const d = await r.json();
      const items = (d.items || []).slice().sort((a,b) => (b.traded_at||0) - (a.traded_at||0));
      if (!items.length) {
        list.innerHTML = '<div style="padding:40px;text-align:center;color:var(--text-tertiary);">暂无成交记录</div>';
        return;
      }
      const MARKET_LABEL = { crypto: '加密', us: '美股', hk: '港股', cn: 'A股' };
      const _esc = (s) => (s == null ? '' : String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])));
      const _time = (sec) => !sec ? '-' : new Date(sec*1000).toLocaleString('zh-CN', { month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false });

      const rows = items.map(it => {
        const side = (it.trigger_detail && it.trigger_detail.side) || 'long';
        const a = _autoActionDisplay(it.action, side);
        const pnlCell = it.trigger_detail && it.trigger_detail.realized_pnl_usd != null
          ? `<span style="color:${it.trigger_detail.realized_pnl_usd >= 0 ? 'var(--color-up)' : 'var(--color-down)'};font-weight:600;">${it.trigger_detail.realized_pnl_usd >= 0 ? '+' : ''}$${it.trigger_detail.realized_pnl_usd} (${it.trigger_detail.realized_pnl_pct >= 0 ? '+' : ''}${it.trigger_detail.realized_pnl_pct}%)</span>`
          : '<span style="color:var(--text-tertiary);">—</span>';
        // v12.3 复盘按钮：仅 close 行显示
        const reviewBtn = it.action === 'close' && it.position_id
          ? `<button class="btn btn-sm port-review-btn" data-pid="${_esc(it.position_id)}" data-sym="${_esc(it.symbol)}" style="font-size:10px;padding:2px 6px;color:var(--color-purple);" title="LLM 深度复盘">📝</button>`
          : '';
        return `<tr style="border-bottom:1px solid var(--border-secondary);">
          <td style="padding:6px 10px;color:var(--text-tertiary);font-size:11px;white-space:nowrap;">${_time(it.traded_at)}</td>
          <td style="padding:6px 10px;font-weight:600;">${_esc(it.symbol)}</td>
          <td style="padding:6px 10px;color:var(--text-secondary);">${MARKET_LABEL[it.market]||it.market}</td>
          <td style="padding:6px 10px;color:${a.c};font-weight:600;white-space:nowrap;">${a.t}</td>
          <td style="padding:6px 10px;text-align:right;">${(it.quantity||0).toFixed(6)}</td>
          <td style="padding:6px 10px;text-align:right;">${(it.price||0).toFixed(4)}</td>
          <td style="padding:6px 10px;text-align:right;font-weight:600;">$${(it.amount_usd||0).toFixed(2)}</td>
          <td style="padding:6px 10px;text-align:right;">${pnlCell}</td>
          <td style="padding:6px 10px;color:var(--text-secondary);font-size:11px;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${_esc(it.reason||'')}">${_esc(it.reason||'-')}</td>
          <td style="padding:6px 10px;text-align:center;">${reviewBtn}</td>
        </tr>`;
      }).join('');

      list.innerHTML = `
        <div style="padding:6px 10px;color:var(--text-tertiary);font-size:11px;background:var(--bg-tertiary);border-bottom:1px solid var(--border-secondary);">
          📜 按时间倒序显示所有已成交的操作（open/add/reduce/close）共 ${items.length} 笔。平仓行点 📝 看 AI 深度复盘。
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:12px;">
          <thead style="position:sticky;top:0;background:var(--bg-secondary);z-index:1;">
            <tr style="border-bottom:1px solid var(--border-primary);color:var(--text-tertiary);">
              <th style="padding:8px 10px;text-align:left;">时间</th>
              <th style="padding:8px 10px;text-align:left;">品种</th>
              <th style="padding:8px 10px;text-align:left;">市场</th>
              <th style="padding:8px 10px;text-align:left;">操作</th>
              <th style="padding:8px 10px;text-align:right;">数量</th>
              <th style="padding:8px 10px;text-align:right;">价格</th>
              <th style="padding:8px 10px;text-align:right;">金额(USD)</th>
              <th style="padding:8px 10px;text-align:right;">本单盈亏</th>
              <th style="padding:8px 10px;text-align:left;">理由</th>
              <th style="padding:8px 10px;text-align:center;">复盘</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>`;
      // 委托复盘按钮点击
      if (!list._reviewDelegated) {
        list._reviewDelegated = true;
        list.addEventListener('click', async (e) => {
          const btn = e.target.closest('.port-review-btn');
          if (!btn) return;
          const pid = btn.dataset.pid;
          const sym = btn.dataset.sym;
          await _showReviewModal(pid, sym, btn);
        });
      }
    } catch (e) {
      list.innerHTML = `<div style="padding:40px;text-align:center;color:var(--color-down);">加载失败: ${e.message}</div>`;
    }
  }

  // ─── v12.3 复盘 modal ───
  async function _showReviewModal(positionId, symbol, btn) {
    let overlay = document.getElementById('trade-review-modal');
    if (overlay) overlay.remove();
    overlay = document.createElement('div');
    overlay.id = 'trade-review-modal';
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center;';
    overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
    overlay.innerHTML = `
      <div style="background:var(--bg-secondary);border-radius:8px;width:min(820px,94vw);max-height:90vh;display:flex;flex-direction:column;box-shadow:0 8px 32px rgba(0,0,0,0.5);">
        <div style="padding:14px 18px;border-bottom:1px solid var(--border-secondary);display:flex;justify-content:space-between;align-items:center;">
          <span style="font-weight:600;">📝 ${symbol} AI 深度复盘</span>
          <div style="display:flex;gap:8px;">
            <button id="rv-regen" class="btn btn-sm" title="强制重新调 LLM 复盘（耗时 30-60s）">🔄 重做</button>
            <button id="rv-close" class="btn btn-sm" style="font-size:14px;padding:2px 10px;">×</button>
          </div>
        </div>
        <div id="rv-body" style="overflow-y:auto;flex:1;padding:14px 20px;font-size:12px;line-height:1.7;">
          <div style="text-align:center;padding:60px;color:var(--text-tertiary);">⏳ 加载复盘...</div>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    overlay.querySelector('#rv-close').addEventListener('click', () => overlay.remove());
    const body = overlay.querySelector('#rv-body');

    async function load(force = false) {
      body.innerHTML = '<div style="text-align:center;padding:60px;color:var(--text-tertiary);">⏳ AI 深度复盘中（约 30-60s）...</div>';
      try {
        let data;
        if (force) {
          const r = await fetch(`/api/trade-review/trigger/${encodeURIComponent(positionId)}?force=true`, {method: 'POST'});
          data = await r.json();
          if (!r.ok || !data.ok) throw new Error(data.detail || data.msg || '触发失败');
          // 拉完整数据
          const r2 = await fetch(`/api/trade-review/${encodeURIComponent(positionId)}`);
          data = await r2.json();
        } else {
          const r = await fetch(`/api/trade-review/${encodeURIComponent(positionId)}`);
          if (r.status === 404) {
            // 还没复盘 → 主动触发
            const tr = await fetch(`/api/trade-review/trigger/${encodeURIComponent(positionId)}`, {method: 'POST'});
            const td = await tr.json();
            if (!tr.ok) throw new Error(td.detail || '复盘失败');
            const r2 = await fetch(`/api/trade-review/${encodeURIComponent(positionId)}`);
            data = await r2.json();
          } else {
            data = await r.json();
          }
        }
        body.innerHTML = _renderReviewBody(data);
      } catch (e) {
        body.innerHTML = `<div style="color:var(--color-down);padding:30px;text-align:center;">加载失败: ${e.message}</div>`;
      }
    }
    overlay.querySelector('#rv-regen').addEventListener('click', () => load(true));
    load(false);
  }

  function _renderReviewBody(d) {
    if (!d || !d.symbol) return '<div style="color:var(--color-down);padding:30px;">数据为空</div>';
    const GRADE_COLOR = { A: 'var(--color-up)', B: 'var(--color-accent)', C: 'var(--color-warning)', D: 'var(--color-down)' };
    const gColor = GRADE_COLOR[d.grade] || 'var(--text-secondary)';
    const pnlColor = (d.realized_pnl_pct || 0) >= 0 ? 'var(--color-up)' : 'var(--color-down)';
    const turning = (d.turning_points || []).map(t => `
      <li style="margin-bottom:6px;">
        <strong>${_escHtml(t.time||'?')} @ ${t.price||'?'}</strong> — ${_escHtml(t.event||'')}
        <div style="color:var(--text-secondary);font-size:11px;margin-left:10px;">💡 ${_escHtml(t.ai_note||'')}</div>
      </li>
    `).join('') || '<li style="color:var(--text-tertiary);">(LLM 未给出)</li>';
    const lessons = (d.lessons || []).map(l => `
      <li><span class="oc-chip oc-chip-purple" style="margin-right:6px;">${_escHtml(l.type||'general')}</span>${_escHtml(l.content||'')}</li>
    `).join('') || '<li style="color:var(--text-tertiary);">(LLM 未给出)</li>';
    const fmt = v => v == null ? '—' : (typeof v === 'number' ? v.toFixed(4) : v);
    return `
      <!-- 头部数据 -->
      <div style="display:grid;grid-template-columns:repeat(4, 1fr);gap:10px;margin-bottom:14px;">
        <div class="oc-card oc-card-compact"><div class="oc-text-xs oc-muted">评分</div>
          <div style="font-size:24px;font-weight:700;color:${gColor};">${d.score||0} <span style="font-size:14px;">${d.grade||'-'}</span></div>
        </div>
        <div class="oc-card oc-card-compact"><div class="oc-text-xs oc-muted">收益率</div>
          <div style="font-size:18px;font-weight:700;color:${pnlColor};">${(d.realized_pnl_pct||0)>=0?'+':''}${(d.realized_pnl_pct||0).toFixed(2)}%</div>
          <div class="oc-text-xs oc-muted">${(d.realized_pnl_local||0)>=0?'+':''}${(d.realized_pnl_local||0).toFixed(2)}</div>
        </div>
        <div class="oc-card oc-card-compact"><div class="oc-text-xs oc-muted">持仓时长</div>
          <div style="font-size:18px;font-weight:600;">${(d.hold_hours||0).toFixed(1)}h</div>
        </div>
        <div class="oc-card oc-card-compact"><div class="oc-text-xs oc-muted">错过的额外利润</div>
          <div style="font-size:18px;font-weight:600;color:${(d.missed_profit_pct||0)>5?'var(--color-warning)':'var(--text-primary)'};">+${(d.missed_profit_pct||0).toFixed(2)}%</div>
        </div>
      </div>
      <!-- 关键价位 -->
      <div style="background:var(--bg-tertiary);padding:10px;border-radius:4px;margin-bottom:14px;font-size:11px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;">
        <span>开仓 <strong>${fmt(d.open_price)}</strong></span>
        <span>平仓 <strong>${fmt(d.close_price)}</strong></span>
        <span>期间高 <strong style="color:var(--color-up);">${fmt(d.period_high)}</strong></span>
        <span>期间低 <strong style="color:var(--color-down);">${fmt(d.period_low)}</strong></span>
        <span>最佳出场 <strong style="color:var(--color-purple);">${fmt(d.best_exit_price)}</strong></span>
      </div>
      <!-- 三段分析 -->
      <h4 style="color:var(--color-accent);margin:16px 0 6px;font-size:13px;">📥 入场质量分析</h4>
      <div style="color:var(--text-primary);">${_escHtml(d.entry_analysis || '(暂无)')}</div>
      <h4 style="color:var(--color-accent);margin:16px 0 6px;font-size:13px;">📊 持仓管理分析</h4>
      <div style="color:var(--text-primary);">${_escHtml(d.mid_analysis || '(暂无)')}</div>
      <h4 style="color:var(--color-accent);margin:16px 0 6px;font-size:13px;">📤 出场质量分析</h4>
      <div style="color:var(--text-primary);">${_escHtml(d.exit_analysis || '(暂无)')}</div>
      <!-- 关键转折点 -->
      <h4 style="color:var(--color-accent);margin:16px 0 6px;font-size:13px;">🎯 关键转折点</h4>
      <ul style="margin:0;padding-left:20px;">${turning}</ul>
      <!-- 改进建议 -->
      <h4 style="color:var(--color-purple);margin:16px 0 6px;font-size:13px;">💡 改进建议</h4>
      <div style="color:var(--text-primary);background:rgba(124,77,255,0.08);padding:10px;border-left:3px solid var(--color-purple);border-radius:0 4px 4px 0;">${_escHtml(d.improvements || '(暂无)')}</div>
      <!-- 教训 -->
      <h4 style="color:var(--color-warning);margin:16px 0 6px;font-size:13px;">📚 可复用教训</h4>
      <ul style="margin:0;padding-left:20px;">${lessons}</ul>
      <div style="margin-top:14px;padding-top:10px;border-top:1px solid var(--border-secondary);font-size:10px;color:var(--text-tertiary);">
        复盘于 ${new Date((d.reviewed_at||0)*1000).toLocaleString('zh-CN')} · LLM ${d.llm_model||'-'} · position_id=${d.position_id}
      </div>
    `;
  }
  function _escHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  // —— 拒单视图
  async function _renderRejects() {
    const list = document.querySelector('.bottom-pane[data-pane="portfolio"] .portfolio-list');
    if (!list) return;
    list.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-tertiary);">⏳ 加载...</div>';
    try {
      const r = await fetch('/api/auto-trade/log?limit=500&status=rejected');
      const d = await r.json();
      const items = (d.items || []).slice().sort((a,b) => (b.traded_at||0) - (a.traded_at||0));
      if (!items.length) {
        list.innerHTML = '<div style="padding:40px;text-align:center;color:var(--text-tertiary);">暂无拒单记录</div>';
        return;
      }
      const MARKET_LABEL = { crypto: '加密', us: '美股', hk: '港股', cn: 'A股' };
      const _esc = (s) => (s == null ? '' : String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])));
      const _time = (sec) => !sec ? '-' : new Date(sec*1000).toLocaleString('zh-CN', { month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false });
      const ACTION_ZH = { open: '📥 开仓', add: '➕ 加仓', reduce: '➖ 减仓', close: '🏁 平仓' };

      const rows = items.map(it => `<tr style="border-bottom:1px solid var(--border-secondary);">
          <td style="padding:6px 10px;color:var(--text-tertiary);font-size:11px;white-space:nowrap;">${_time(it.traded_at)}</td>
          <td style="padding:6px 10px;font-weight:600;">${_esc(it.symbol)}</td>
          <td style="padding:6px 10px;color:var(--text-secondary);">${MARKET_LABEL[it.market]||it.market}</td>
          <td style="padding:6px 10px;color:var(--text-secondary);white-space:nowrap;">${ACTION_ZH[it.action]||it.action}</td>
          <td style="padding:6px 10px;color:var(--color-warning);font-size:11px;">${_esc(it.rejected_reason||'-')}</td>
          <td style="padding:6px 10px;color:var(--text-tertiary);font-size:11px;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${_esc(it.reason||'')}">${_esc(it.reason||'-')}</td>
        </tr>`).join('');

      list.innerHTML = `
        <div style="padding:6px 10px;color:var(--text-tertiary);font-size:11px;background:var(--bg-tertiary);border-bottom:1px solid var(--border-secondary);">
          ⏸ 共 ${items.length} 条拒单。常见原因：加仓阈值 (浮盈&lt;5%)、同股冷却期、日亏熔断、诊断缺失、未到连续竞价时段。
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:12px;">
          <thead style="position:sticky;top:0;background:var(--bg-secondary);z-index:1;">
            <tr style="border-bottom:1px solid var(--border-primary);color:var(--text-tertiary);">
              <th style="padding:8px 10px;text-align:left;">时间</th>
              <th style="padding:8px 10px;text-align:left;">品种</th>
              <th style="padding:8px 10px;text-align:left;">市场</th>
              <th style="padding:8px 10px;text-align:left;">操作</th>
              <th style="padding:8px 10px;text-align:left;">拒因</th>
              <th style="padding:8px 10px;text-align:left;">信号理由</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>`;
    } catch (e) {
      list.innerHTML = `<div style="padding:40px;text-align:center;color:var(--color-down);">加载失败: ${e.message}</div>`;
    }
  }

  async function _renderAutoTradeLog() {
    const list = document.querySelector('.bottom-pane[data-pane="portfolio"] .portfolio-list');
    if (!list) return;
    list.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-tertiary);">⏳ 加载...</div>';
    try {
      const r = await fetch('/api/auto-trade/trades-by-position');
      const d = await r.json();
      const groups = d.groups || [];
      if (!groups.length) {
        list.innerHTML = '<div style="padding:40px;text-align:center;color:var(--text-tertiary);">暂无自动交易记录<br><span style="font-size:11px;">打开右上角"🤖 自动交易"开关后将开始根据 AI 验证通过的信号自动下单（模拟）</span></div>';
        return;
      }
      _renderTradesGrouped(list, groups);
      return;
    } catch (e) {
      console.warn(e);
      list.innerHTML = `<div style="padding:40px;text-align:center;color:var(--color-down);">加载失败: ${e.message}</div>`;
      return;
    }
  }

  // v12.6: 按单历史的市场过滤（持久化在闭包变量）
  let _historyMarketFilter = 'all';

  function _renderTradesGrouped(list, groups) {
    const MARKET_LABEL = { crypto: '加密', us: '美股', hk: '港股', cn: 'A股' };
    const ACTION_ZH = { open: '📥 开仓', add: '➕ 加仓', reduce: '➖ 减仓', close: '🏁 平仓' };
    const ACTION_COLOR = {
      open: 'var(--color-up)', add: 'var(--color-up)',
      reduce: 'var(--color-warning)', close: 'var(--color-down)',
    };
    const _esc = (s) => (s == null ? '' : String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])));
    const _time = (sec) => !sec ? '-' : new Date(sec*1000).toLocaleString('zh-CN', { month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false });

    // v12.6 过滤：① 隐藏仅有拒绝记录的（无成交）② 按市场过滤
    let visible = (groups || []).filter(g => !g.is_reject_only);
    if (_historyMarketFilter !== 'all') {
      visible = visible.filter(g => g.market === _historyMarketFilter);
    }
    // 按市场分组统计计数
    const counts = { all: (groups || []).filter(g => !g.is_reject_only).length, crypto: 0, us: 0, hk: 0, cn: 0 };
    for (const g of (groups || [])) {
      if (g.is_reject_only) continue;
      counts[g.market] = (counts[g.market] || 0) + 1;
    }
    const PILLS = [
      { key: 'all', label: '全部', emoji: '📋' },
      { key: 'crypto', label: '加密', emoji: '🪙' },
      { key: 'us', label: '美股', emoji: '🇺🇸' },
      { key: 'hk', label: '港股', emoji: '🇭🇰' },
      { key: 'cn', label: 'A股', emoji: '🇨🇳' },
    ];
    const filterBar = `
      <div style="padding:6px 10px;border-bottom:1px solid var(--border-secondary);display:flex;gap:6px;align-items:center;flex-wrap:wrap;background:var(--bg-secondary);">
        <span class="oc-text-sm oc-muted">市场筛选：</span>
        ${PILLS.map(p => {
          const active = _historyMarketFilter === p.key;
          const cnt = counts[p.key] || 0;
          return `<button class="port-history-filter ${active?'active':''}" data-market="${p.key}"
            style="display:inline-flex;align-items:center;gap:4px;padding:3px 9px;font-size:11px;border-radius:12px;cursor:pointer;
            border:1px solid ${active?'var(--color-accent)':'var(--border-secondary)'};
            background:${active?'rgba(74,158,255,0.12)':'transparent'};
            color:${active?'var(--color-accent)':'var(--text-primary)'};font-weight:${active?'600':'400'};">
            <span>${p.emoji}</span><span>${p.label}</span>
            <span style="background:${active?'var(--color-accent)':'var(--bg-tertiary)'};color:${active?'#fff':'var(--text-secondary)'};padding:0 6px;border-radius:8px;font-size:10px;min-width:16px;text-align:center;">${cnt}</span>
          </button>`;
        }).join('')}
        <span style="margin-left:auto;color:var(--text-tertiary);font-size:11px;">显示 ${visible.length} 单</span>
      </div>
    `;

    const cards = visible.map((g, idx) => {
      const statusColor = g.is_closed ? 'var(--color-down)' : (g.position_still_open ? 'var(--color-up)' : 'var(--text-tertiary)');
      const sideChip = g.side === 'short'
        ? '<span style="background:rgba(215,90,90,0.2);color:var(--color-down);padding:1px 6px;border-radius:3px;font-size:10px;font-weight:600;">🔽 空</span>'
        : '<span style="background:rgba(90,175,90,0.2);color:var(--color-up);padding:1px 6px;border-radius:3px;font-size:10px;font-weight:600;">📈 多</span>';

      // 盈亏块
      let pnlHtml = '';
      if (g.is_closed) {
        const c = g.realized_pnl_usd >= 0 ? 'var(--color-up)' : 'var(--color-down)';
        pnlHtml = `<span style="color:${c};font-weight:700;font-size:14px;" title="(卖出总额 − 买入总额)">已实现盈亏 ${g.realized_pnl_usd >= 0 ? '+' : ''}$${g.realized_pnl_usd} (${g.realized_pnl_pct >= 0 ? '+' : ''}${g.realized_pnl_pct}%)</span>`;
      } else if (g.position_still_open && g.projected_pnl_usd != null) {
        const uc = (g.unrealized_pnl_usd||0) >= 0 ? 'var(--color-up)' : 'var(--color-down)';
        const pc = (g.projected_pnl_usd||0) >= 0 ? 'var(--color-up)' : 'var(--color-down)';
        pnlHtml = `
          <span style="color:${uc};font-weight:600;" title="当前市值 − 成本">浮盈 ${(g.unrealized_pnl_usd||0) >= 0 ? '+' : ''}$${g.unrealized_pnl_usd||0}</span>
          <span style="color:var(--text-tertiary);font-size:11px;">市值 $${g.current_market_value_usd||0}</span>
          <span style="color:${pc};font-size:11px;" title="已减仓收入 + 当前市值 − 总买入">若此刻全平 ${(g.projected_pnl_usd||0) >= 0 ? '+' : ''}$${g.projected_pnl_usd||0} (${(g.projected_pnl_pct||0) >= 0 ? '+' : ''}${g.projected_pnl_pct||0}%)</span>`;
      } else if (g.is_reject_only) {
        pnlHtml = '<span style="color:var(--text-tertiary);font-size:11px;">（仅有拒绝记录，未开仓）</span>';
      }

      // 分步明细表
      const legRows = (g.legs || []).map(it => {
        const actColor = ACTION_COLOR[it.action] || 'var(--text-secondary)';
        const actLabel = ACTION_ZH[it.action] || it.action;
        const statusChip = it.status === 'executed'
          ? '<span style="color:var(--color-up);font-size:10px;">✅ 已执行</span>'
          : `<span style="color:var(--text-tertiary);font-size:10px;" title="${_esc(it.rejected_reason||'')}">⏸ 拒绝</span>`;
        const rejectHint = it.status !== 'executed' && it.rejected_reason
          ? `<div style="color:var(--text-tertiary);font-size:10px;margin-top:2px;">${_esc(it.rejected_reason)}</div>` : '';
        const pnlHint = it.trigger_detail && it.trigger_detail.realized_pnl_usd != null
          ? `<div style="font-size:11px;color:${it.trigger_detail.realized_pnl_usd >= 0 ? 'var(--color-up)' : 'var(--color-down)'};margin-top:2px;">整单累计盈亏 ${it.trigger_detail.realized_pnl_usd >= 0 ? '+' : ''}$${it.trigger_detail.realized_pnl_usd} (${it.trigger_detail.realized_pnl_pct >= 0 ? '+' : ''}${it.trigger_detail.realized_pnl_pct}%)</div>`
          : '';
        return `<tr style="border-bottom:1px solid var(--border-secondary);${it.status!=='executed'?'opacity:0.6;':''}">
          <td style="padding:5px 8px;color:var(--text-tertiary);font-size:11px;white-space:nowrap;">${_time(it.traded_at)}</td>
          <td style="padding:5px 8px;color:${actColor};font-weight:600;white-space:nowrap;">${actLabel}${statusChip ? '<br>'+statusChip : ''}</td>
          <td style="padding:5px 8px;text-align:right;">${(it.quantity||0).toFixed(6)}</td>
          <td style="padding:5px 8px;text-align:right;">${(it.price||0).toFixed(4)}</td>
          <td style="padding:5px 8px;text-align:right;font-weight:600;">$${(it.amount_usd||0).toFixed(2)}</td>
          <td style="padding:5px 8px;color:var(--text-secondary);font-size:11px;">${_esc(it.reason||'-')}${rejectHint}${pnlHint}</td>
        </tr>`;
      }).join('');

      return `
        <div class="trade-group" data-idx="${idx}" style="border:1px solid var(--border-secondary);border-radius:6px;margin:8px;overflow:hidden;">
          <div class="trade-group-header" style="padding:10px 14px;background:var(--bg-tertiary);display:flex;gap:14px;align-items:center;flex-wrap:wrap;cursor:pointer;" data-idx="${idx}">
            <strong style="font-size:13px;">${_esc(g.symbol)}</strong>
            <span style="color:var(--text-secondary);font-size:11px;">${MARKET_LABEL[g.market] || g.market}</span>
            ${sideChip}
            <span style="color:${statusColor};font-size:11px;font-weight:600;">${g.status_text}</span>
            <span style="color:var(--text-tertiary);font-size:11px;">共 ${g.leg_count} 笔成交</span>
            <span style="color:var(--text-tertiary);font-size:11px;">买入 $${g.total_in_usd}</span>
            <span style="color:var(--text-tertiary);font-size:11px;">卖出 $${g.total_out_usd}</span>
            ${pnlHtml}
            <span style="margin-left:auto;color:var(--text-tertiary);font-size:10px;">${g.open_at ? '首次 '+_time(g.open_at) : ''} · 最近 ${_time(g.last_at)}</span>
            <span class="trade-toggle" style="color:var(--text-tertiary);font-size:12px;" title="点击展开明细">▶</span>
          </div>
          <div class="trade-group-body" style="display:none;">
            <table style="width:100%;font-size:12px;border-collapse:collapse;">
              <thead>
                <tr style="border-bottom:1px solid var(--border-primary);color:var(--text-tertiary);">
                  <th style="padding:6px 8px;text-align:left;">时间</th>
                  <th style="padding:6px 8px;text-align:left;">操作</th>
                  <th style="padding:6px 8px;text-align:right;">数量</th>
                  <th style="padding:6px 8px;text-align:right;">价格</th>
                  <th style="padding:6px 8px;text-align:right;">金额(USD)</th>
                  <th style="padding:6px 8px;text-align:left;">理由 / 拒因 / 累计盈亏</th>
                </tr>
              </thead>
              <tbody>${legRows}</tbody>
            </table>
          </div>
        </div>`;
    }).join('');

    list.innerHTML = `
      <div style="padding:6px 10px;color:var(--text-tertiary);font-size:11px;background:var(--bg-tertiary);border-bottom:1px solid var(--border-secondary);">
        📋 按单分组 · 每一单为一行 · 点 ▶ 展开看 开仓/加仓/减仓/平仓 明细 + 累计盈亏
      </div>
      ${filterBar}
      ${visible.length ? cards : '<div class="oc-empty"><div class="oc-empty-icon">📭</div><div class="oc-empty-title">该市场暂无成交记录</div></div>'}`;

    // 折叠/展开
    list.querySelectorAll('.trade-group-header').forEach(h => {
      h.addEventListener('click', () => {
        const body = h.nextElementSibling;
        const toggle = h.querySelector('.trade-toggle');
        if (!body) return;
        const open = body.style.display !== 'none';
        body.style.display = open ? 'none' : 'block';
        if (toggle) toggle.textContent = open ? '▶' : '▼';
      });
    });

    // v12.6 市场过滤切换
    list.querySelectorAll('.port-history-filter').forEach(btn => {
      btn.addEventListener('click', () => {
        _historyMarketFilter = btn.dataset.market;
        _renderTradesGrouped(list, groups);
      });
    });
  }

  // 旧 renderAutoTradeLog 保留 stub（以防有地方调用，但已不用了）
  async function _renderAutoTradeLogLegacy() {
    const list = document.querySelector('.bottom-pane[data-pane="portfolio"] .portfolio-list');
    if (!list) return;
    list.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-tertiary);">⏳ 加载...</div>';
    try {
      const r = await fetch('/api/auto-trade/log?limit=100');
      const d = await r.json();
      const items = d.items || [];
      if (!items.length) {
        list.innerHTML = '<div style="padding:40px;text-align:center;color:var(--text-tertiary);">暂无自动交易记录<br><span style="font-size:11px;">打开右上角"🤖 自动交易"开关后将开始根据 AI 验证通过的信号自动下单（模拟）</span></div>';
        return;
      }
      // 触发类型 → 中文徽章
      const TRIGGER_LABEL = {
        'signal_confirm':         { t: '📡 信号确认',   c: 'var(--color-accent)' },
        'rating_change':          { t: '🔄 诊断变化',   c: 'var(--color-warning)' },
        'diagnosis_strong_buy':   { t: '🎯 诊断试单',   c: 'var(--color-up)' },
      };
      const rows = items.map(it => {
        const side = (it.trigger_detail && it.trigger_detail.side) || 'long';
        const a = _autoActionDisplay(it.action, side);
        const trig = TRIGGER_LABEL[it.trigger_type] || { t: it.trigger_type || '-', c: 'var(--text-tertiary)' };
        const t = new Date(it.traded_at * 1000).toLocaleString('zh-CN', { month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit' });
        const statusColor = it.status === 'executed' ? 'var(--color-up)' : 'var(--text-tertiary)';
        const statusLabel = it.status === 'executed' ? '✅ 已执行' : '⏸ 拒绝';
        return `<tr style="border-bottom:1px solid var(--border-secondary);">
          <td style="padding:5px 8px;color:var(--text-tertiary);font-size:11px;">${t}</td>
          <td style="padding:5px 8px;font-weight:600;">${_esc(it.symbol)}</td>
          <td style="padding:5px 8px;color:var(--text-secondary);">${({us:'美股',hk:'港股',cn:'A股',crypto:'加密'})[it.market] || it.market}</td>
          <td style="padding:5px 8px;">
            <div style="color:${a.c};font-weight:600;">${a.t}</div>
            <div style="color:${trig.c};font-size:10px;margin-top:2px;">${trig.t}</div>
          </td>
          <td style="padding:5px 8px;text-align:right;">${(it.quantity||0).toFixed(4)}</td>
          <td style="padding:5px 8px;text-align:right;">${(it.price||0).toFixed(4)}</td>
          <td style="padding:5px 8px;text-align:right;">$${(it.amount_usd||0).toFixed(2)}</td>
          <td style="padding:5px 8px;color:var(--text-tertiary);max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${_esc(it.reason||'')}">${_esc(it.reason||'')}</td>
          <td style="padding:5px 8px;color:${statusColor};font-size:11px;">${statusLabel}${it.rejected_reason?`<br><span style="font-size:10px;color:var(--text-tertiary);">${_esc(it.rejected_reason)}</span>`:''}</td>
        </tr>`;
      }).join('');
      list.innerHTML = `
        <table style="width:100%;font-size:12px;border-collapse:collapse;">
          <thead>
            <tr style="border-bottom:1px solid var(--border-secondary);color:var(--text-tertiary);">
              <th style="padding:6px 8px;text-align:left;">时间</th>
              <th style="padding:6px 8px;text-align:left;">品种</th>
              <th style="padding:6px 8px;text-align:left;">市场</th>
              <th style="padding:6px 8px;text-align:left;">操作</th>
              <th style="padding:6px 8px;text-align:right;">数量</th>
              <th style="padding:6px 8px;text-align:right;">价格</th>
              <th style="padding:6px 8px;text-align:right;">金额(USD)</th>
              <th style="padding:6px 8px;text-align:left;">理由</th>
              <th style="padding:6px 8px;text-align:left;">状态</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>`;
    } catch (e) {
      list.innerHTML = `<div style="color:var(--color-down);padding:20px;">加载失败: ${_esc(e.message)}</div>`;
    }
  }

  function _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  async function _onAddSubmit() {
    const pane = document.querySelector('.bottom-pane[data-pane="portfolio"]');
    const symbol = pane.querySelector('#port-add-symbol').value.trim();
    const market = pane.querySelector('#port-add-market').value;
    const qty = parseFloat(pane.querySelector('#port-add-qty').value);
    const cost = parseFloat(pane.querySelector('#port-add-cost').value);
    const notes = pane.querySelector('#port-add-notes').value.trim();
    if (!symbol || !qty || !cost) {
      if (typeof showToast === 'function') showToast('品种 / 数量 / 成本必填', 'warning');
      return;
    }
    try {
      const resp = await fetch('/api/positions', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol, market, quantity: qty, avg_cost: cost, notes }),
      });
      const d = await resp.json();
      if (!resp.ok) {
        if (typeof showToast === 'function') showToast(d.detail || '添加失败', 'error');
        return;
      }
      if (typeof showToast === 'function') showToast(`已添加持仓 ${symbol}`, 'success');
      pane.querySelector('#port-add-symbol').value = '';
      pane.querySelector('#port-add-qty').value = '';
      pane.querySelector('#port-add-cost').value = '';
      pane.querySelector('#port-add-notes').value = '';
      pane.querySelector('.portfolio-add-form').style.display = 'none';
      await refresh();
      // 自动为新添加的持仓调用 AI 补 SL/TP（fire-and-forget；失败也不影响添加）
      const newPid = d.id || (d.position && d.position.id);
      if (newPid) {
        setTimeout(() => _backfillTargets(false, newPid), 800);
      }
    } catch (e) {
      console.error('[Portfolio] 添加失败', e);
    }
  }

  // ─── 调 AI 补 SL/TP ───
  async function _backfillTargets(force = false, positionId = null) {
    const btn = document.getElementById('port-backfill-targets-btn');
    if (btn) { btn.disabled = true; btn.textContent = '🎯 AI 分析中...'; }
    try {
      if (typeof showToast === 'function') {
        showToast(positionId ? '🎯 为新持仓调用 AI 补止盈止损...' : '🎯 扫描所有缺 SL/TP 的持仓 → 调用 AI 补全...', 'info', 6000);
      }
      const body = {};
      if (positionId) body.position_id = positionId;
      if (force) body.force = true;
      const r = await fetch('/api/auto-trade/backfill-targets', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      if (!r.ok || d.error) {
        if (typeof showToast === 'function') showToast(`❌ ${d.error || '失败'}`, 'error');
        return;
      }
      const skipBudget = d.skipped_budget || 0;
      let msg = `🎯 处理 ${d.processed} 个 · 成功 ${d.filled} · 失败 ${d.failed}`;
      if (skipBudget > 0) msg += ` · ⏸ 预算用尽跳过 ${skipBudget}（明天重试）`;
      if (typeof showToast === 'function') showToast(msg, d.filled > 0 ? 'success' : 'warning', 6000);
      await refresh();   // 拉新数据，让 SL/TP 立即出现在 UI
    } catch (e) {
      if (typeof showToast === 'function') showToast(`❌ ${e.message}`, 'error');
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = '🎯 补 SL/TP'; }
    }
  }

  async function refresh() {
    try {
      const resp = await fetch('/api/positions');
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      _items = await resp.json();
      // 拉取每个持仓的最新 AI 建议
      try {
        const advResp = await fetch('/api/positions/advices/latest');
        if (advResp.ok) {
          const arr = await advResp.json();
          _latestAdvices = {};
          for (const a of arr) {
            if (a.advice) _latestAdvices[a.position_id] = a;
          }
        }
      } catch {}
      render();
      const statusEl = document.querySelector('.bottom-pane[data-pane="portfolio"] #port-status');
      if (statusEl) statusEl.textContent = `共 ${_items.length} 个持仓 · ${new Date().toLocaleTimeString()}`;
    } catch (e) {
      console.warn('[Portfolio] 刷新失败:', e);
    }
  }

  function render() {
    // 仅在 positions 视图下渲染；其他 tab（history/fills/rejects）有自己的 renderer，不要被覆盖
    if (_viewMode !== 'positions') return;
    // positions 视图特有：渲染市场过滤栏（每次 render 都同步计数）
    _renderMarketFilter();

    const listEl = document.querySelector('.bottom-pane[data-pane="portfolio"] .portfolio-list');
    if (!listEl) return;
    // 空仓 → 教程卡片
    if (!_items.length) {
      listEl.innerHTML = _renderEmptyState();
      _bindEmptyStateEvents(listEl);
      return;
    }
    // 应用市场过滤
    const visible = _marketFilter === 'all' ? _items : _items.filter(p => p.market === _marketFilter);
    if (!visible.length) {
      listEl.innerHTML = `<div style="padding:60px 20px;text-align:center;color:var(--text-tertiary);">
        <div style="font-size:32px;margin-bottom:8px;opacity:0.5;">📭</div>
        当前 <strong>${_marketFilter === 'all' ? '全部' : _marketFilter.toUpperCase()}</strong> 市场没有持仓
      </div>`;
      return;
    }
    listEl.innerHTML = `
      <table class="port-table" style="width:100%;font-size:12px;border-collapse:collapse;">
        <thead>
          <tr style="border-bottom:1px solid var(--border-secondary);color:var(--text-tertiary);background:var(--bg-secondary);position:sticky;top:0;z-index:1;">
            <th style="padding:8px 10px;text-align:left;font-weight:600;">品种</th>
            <th style="padding:8px 10px;text-align:right;font-weight:600;">数量</th>
            <th style="padding:8px 10px;text-align:right;font-weight:600;">持仓价值</th>
            <th style="padding:8px 10px;text-align:right;font-weight:600;">入场 → 现价</th>
            <th style="padding:8px 10px;text-align:right;font-weight:600;">浮动盈亏</th>
            <th style="padding:8px 10px;text-align:left;font-weight:600;width:200px;">止盈止损区间</th>
            <th style="padding:8px 10px;text-align:left;font-weight:600;width:220px;">🤖 AI 建议</th>
            <th style="padding:8px 10px;text-align:right;font-weight:600;">操作</th>
          </tr>
        </thead>
        <tbody>${visible.map(_renderRow).join('')}</tbody>
      </table>
    `;
    // 事件委托：在 listEl 上绑一次（防 detached DOM 累积）
    if (!listEl._delegated) {
      listEl._delegated = true;
      listEl.addEventListener('click', async (e) => {
        const view = e.target.closest('[data-action="view"]');
        if (view) {
          const sym = view.dataset.symbol; const mkt = view.dataset.market;
          if (typeof switchMarket === 'function' && mkt !== window.currentMarket) {
            switchMarket(mkt);
            setTimeout(() => switchSymbol && switchSymbol(sym, mkt), 200);
          } else if (typeof switchSymbol === 'function') {
            switchSymbol(sym, mkt);
          }
          return;
        }
        // ⋮ 菜单（用 confirm 选项+多选 modal 太重，改用 prompt 风格的简单弹层）
        const kebab = e.target.closest('.port-kebab');
        if (kebab) {
          e.stopPropagation();
          const id = kebab.dataset.id;
          const sym = kebab.dataset.symbol;
          // 已有菜单 → 关掉
          document.querySelectorAll('.port-kebab-menu').forEach(m => m.remove());
          const rect = kebab.getBoundingClientRect();
          const menu = document.createElement('div');
          menu.className = 'port-kebab-menu';
          menu.style.cssText = `position:fixed;top:${rect.bottom+2}px;left:${rect.left-160}px;z-index:9998;
            background:var(--bg-secondary);border:1px solid var(--border-secondary);border-radius:6px;
            box-shadow:0 4px 16px rgba(0,0,0,0.4);padding:4px 0;min-width:160px;font-size:12px;`;
          menu.innerHTML = `
            <button data-act="advise" style="display:block;width:100%;text-align:left;padding:6px 12px;background:none;border:0;color:var(--color-purple);cursor:pointer;">🤖 AI 出建议</button>
            <button data-act="advice" style="display:block;width:100%;text-align:left;padding:6px 12px;background:none;border:0;color:var(--text-primary);cursor:pointer;">📋 建议历史</button>
            <button data-act="trades" style="display:block;width:100%;text-align:left;padding:6px 12px;background:none;border:0;color:var(--color-accent);cursor:pointer;">📊 交易明细</button>
            <hr style="border:0;border-top:1px solid var(--border-secondary);margin:4px 0;">
            <button data-act="remove" style="display:block;width:100%;text-align:left;padding:6px 12px;background:none;border:0;color:var(--color-down);cursor:pointer;">× 删除持仓</button>
          `;
          document.body.appendChild(menu);
          // 点击菜单项 → 触发对应原 handler（合成一个携带 dataset 的伪 button click）
          menu.addEventListener('click', (ev) => {
            const btn = ev.target.closest('button');
            if (!btn) return;
            const act = btn.dataset.act;
            const fakeBtn = document.createElement('button');
            fakeBtn.dataset.action = act;
            fakeBtn.dataset.id = id;
            if (act === 'trades') fakeBtn.dataset.symbol = sym;
            // 用现有事件委托链路触发
            listEl.appendChild(fakeBtn);
            fakeBtn.click();
            fakeBtn.remove();
            menu.remove();
          });
          // 点击外部关闭
          setTimeout(() => {
            const handler = (ev) => {
              if (!menu.contains(ev.target)) { menu.remove(); document.removeEventListener('click', handler); }
            };
            document.addEventListener('click', handler);
          }, 50);
          return;
        }
        // 主动触发 AI 建议（即使无新闻）
        const advise = e.target.closest('[data-action="advise"]');
        if (advise) {
          const id = advise.dataset.id;
          if (typeof showToast === 'function') showToast('🤖 AI 正在分析（约 30-60 秒）...', 'info', 5000);
          advise.disabled = true;
          advise.textContent = '🤖 分析中...';
          try {
            const resp = await fetch(`/api/positions/${id}/advise`, { method: 'POST' });
            if (!resp.ok) {
              const err = await resp.json().catch(() => ({}));
              throw new Error(err.detail || `HTTP ${resp.status}`);
            }
            const data = await resp.json();
            const a = data.advice || {};
            const ADVICE_ZH = { hold: '持有', reduce: '减仓', add: '加仓', close: '清仓' };
            if (typeof showToast === 'function') {
              showToast(`✅ AI 建议: ${ADVICE_ZH[a.advice] || a.advice} — ${(a.reason || '').substring(0, 60)}`, 'success', 8000);
            }
            await refresh();
          } catch (err) {
            console.warn(err);
            if (typeof showToast === 'function') showToast(`生成失败: ${err.message}`, 'error', 4000);
          } finally {
            advise.disabled = false;
            advise.textContent = '🤖 建议';
          }
          return;
        }
        const adv = e.target.closest('[data-action="advice"]');
        if (adv) {
          const id = adv.dataset.id;
          // 老请求未完成就点了别的 → 取消
          if (window._adviceCtrl) { try { window._adviceCtrl.abort(); } catch {} }
          const ctrl = new AbortController();
          window._adviceCtrl = ctrl;
          try {
            const resp = await fetch(`/api/positions/${id}/advices?limit=20`, { signal: ctrl.signal });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const arr = await resp.json();
            if (!Array.isArray(arr) || !arr.length) {
              if (typeof showToast === 'function') showToast('暂无历史建议（点击"🤖 建议"主动生成）', 'info', 3500);
              return;
            }
            _showAdvicesModal(arr);
          } catch (err) {
            if (err.name === 'AbortError') return;
            console.warn(err);
            if (typeof showToast === 'function') showToast(`加载建议失败: ${err.message}`, 'error', 3000);
          }
          return;
        }
        const trades = e.target.closest('[data-action="trades"]');
        if (trades) {
          const id = trades.dataset.id;
          const sym = trades.dataset.symbol;
          if (window._tradesCtrl) { try { window._tradesCtrl.abort(); } catch {} }
          const ctrl = new AbortController();
          window._tradesCtrl = ctrl;
          try {
            const resp = await fetch(`/api/auto-trade/log?position_id=${encodeURIComponent(id)}&limit=200`, { signal: ctrl.signal });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            _showTradesModal(sym, data.items || [], data.summary || null);
          } catch (err) {
            if (err.name === 'AbortError') return;
            console.warn(err);
            if (typeof showToast === 'function') showToast(`加载交易记录失败: ${err.message}`, 'error', 3000);
          }
          return;
        }
        const rm = e.target.closest('[data-action="remove"]');
        if (rm) {
          const id = rm.dataset.id;
          if (!confirm('确认删除此持仓？')) return;
          try {
            const resp = await fetch(`/api/positions/${id}`, { method: 'DELETE' });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            if (typeof showToast === 'function') showToast('已删除', 'success');
            await refresh();
          } catch (err) {
            if (typeof showToast === 'function') showToast(`删除失败: ${err.message}`, 'error');
          }
          return;
        }
      });
    }
  }

  // 市场币种符号映射：成本/现价/本币盈亏 都按市场币种显示
  const MARKET_CCY = {
    crypto: { code: 'USDT', symbol: '$',    prefix: '',     fxTarget: 'USD' },  // USDT ≈ USD
    us:     { code: 'USD',  symbol: '$',    prefix: '',     fxTarget: 'USD' },
    hk:     { code: 'HKD',  symbol: 'HK$',  prefix: 'HK$',  fxTarget: 'USD' },
    cn:     { code: 'CNY',  symbol: '¥',    prefix: '¥',    fxTarget: 'USD' },
  };
  function _fmtMoney(market, value, decimals) {
    if (value == null || !isFinite(value)) return '—';
    const ccy = MARKET_CCY[market] || MARKET_CCY.us;
    const d = decimals == null ? 4 : decimals;
    // 负数把符号放前面
    const neg = value < 0 ? '-' : '';
    return `${neg}${ccy.prefix || ccy.symbol}${Math.abs(value).toFixed(d)}`;
  }
  function _ccyCode(market) {
    return (MARKET_CCY[market] || MARKET_CCY.us).code;
  }

  function _renderRow(p) {
    const MARKET_LABEL = { crypto: '加密', us: '美股', hk: '港股', cn: 'A股' };
    const MARKET_FLAG = { crypto: '🪙', us: '🇺🇸', hk: '🇭🇰', cn: '🇨🇳' };
    const ccyCode = _ccyCode(p.market);

    // ─── 1. 品种 + 方向 + 标签 ───
    const side = p.side || 'long';
    const sideChip = side === 'short'
      ? '<span class="port-side-chip" style="background:rgba(215,90,90,0.18);color:var(--color-down);">空</span>'
      : '<span class="port-side-chip" style="background:rgba(76,175,80,0.18);color:var(--color-up);">多</span>';
    const autoFlag = p.auto_traded
      ? '<span style="font-size:10px;color:var(--color-purple);margin-left:4px;" title="自动交易建仓">🤖</span>'
      : '';
    const openTs = p.opened_at || 0;
    const openTime = openTs
      ? new Date(openTs * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
      : '';
    // v11.3 持仓天数 + 上次诊断时间标
    const nowSec = Math.floor(Date.now() / 1000);
    const ageDays = openTs ? ((nowSec - openTs) / 86400) : 0;
    const ageStr = ageDays >= 1 ? `${Math.round(ageDays)}d` : ageDays >= 1/24 ? `${Math.round(ageDays*24)}h` : '<1h';
    let lastAdv = '';
    const advData = _latestAdvices[p.id];
    if (advData && advData.advised_at) {
      // v12.6: 修单位 BUG — advised_at 可能是秒（< 1e10）或毫秒（>= 1e10），统一转毫秒
      const tsMs = advData.advised_at < 1e10 ? advData.advised_at * 1000 : advData.advised_at;
      const advHrs = Math.max(0, (Date.now() - tsMs) / 3600000);
      const advStr = advHrs < 1 ? `${Math.round(advHrs * 60)}min前` : advHrs < 24 ? `${Math.round(advHrs)}h前` : `${Math.round(advHrs/24)}d前`;
      lastAdv = ` · 诊断 ${advStr}`;
    } else {
      lastAdv = ' · 无诊断';
    }
    // 僵尸标记（≥ 14 天 + 浮盈 ±5% 内）
    const isZombie = ageDays >= 14 && p.pnl_pct != null && Math.abs(Number(p.pnl_pct)) < 5;
    const zombieFlag = isZombie ? '<span style="color:var(--color-warning);font-size:10px;margin-left:4px;" title="持仓 14 天+ 浮盈 ±5% 内，每 1h 巡检会自动减半">💤</span>' : '';
    // v12.16.3 入场策略显示
    const STRATEGY_NAME_CN = {
      ma_cross:'均线金叉死叉', donchian_breakout:'唐奇安通道突破', bollinger_reversion:'布林带均值回归',
      volume_breakout:'成交量突破', flash_event:'新闻事件驱动', chanlun:'缠论买卖点',
      macd_cross:'MACD 金叉死叉', ema_triple:'EMA 三线排列', squeeze_breakout:'布林挤压突破',
      adx_trend_follow:'ADX 趋势跟随',
      rsi_pullback:'RSI 趋势回踩', rsi_real_divergence:'RSI 真背离', rsi_breakout_50:'RSI 50 上穿',
      resonance:'🌟 多策略共振',
      funding_extreme:'资金费率极值', oi_breakout:'OI 持仓突破', long_short_ratio:'多空比反转',
      fear_greed_reversal:'F&G 极值反转', limit_up_followup:'涨停后回踩',
      northbound_flow_top:'北向资金排名', sector_momentum:'板块联动',
      southbound_inflow:'港股通南向', ah_spread_revert:'AH 价差回归',
      gap_up_continuation:'高开延续', vwap_pullback:'VWAP 回踩', earnings_window_filter:'财报窗口过滤',
    };
    let strategyChip = '';
    if (p.entry_strategy) {
      if (p.entry_strategy === 'resonance' && p.entry_strategies) {
        const lvl = p.entry_resonance_level || '?';
        const boost = p.entry_sizing_boost || 1.0;
        const names = p.entry_strategies.map(s => STRATEGY_NAME_CN[s] || s).join('+');
        strategyChip = `<span style="font-size:10px;color:var(--color-accent);background:rgba(88,166,255,0.12);padding:1px 6px;border-radius:4px;" title="共振 Level ${lvl}, 仓位 ×${boost}">📡 共振L${lvl}: ${names}</span>`;
      } else {
        const cn = STRATEGY_NAME_CN[p.entry_strategy] || p.entry_strategy;
        strategyChip = `<span style="font-size:10px;color:var(--text-secondary);background:var(--bg-tertiary);padding:1px 6px;border-radius:4px;">📡 ${cn}</span>`;
      }
    }
    const symbolCell = `
      <div style="display:flex;flex-direction:column;gap:2px;">
        <div style="display:flex;align-items:center;gap:6px;">
          <span style="font-weight:700;font-size:13px;">${p.symbol}</span>
          ${sideChip}${autoFlag}${zombieFlag}
        </div>
        <div style="font-size:10px;color:var(--text-tertiary);">
          ${MARKET_FLAG[p.market]||''} ${MARKET_LABEL[p.market] || p.market.toUpperCase()} · ${openTime} · 持仓 ${ageStr}${lastAdv}
        </div>
        ${strategyChip ? `<div style="margin-top:2px;">${strategyChip}</div>` : ''}
      </div>`;

    // ─── 2. 数量 ───
    const qty = p.quantity == null ? 0 : Number(p.quantity);
    const qtyCell = `
      <div style="font-weight:600;">${qty.toFixed(4)}</div>
      <div style="font-size:10px;color:var(--text-tertiary);">${p.market === 'crypto' ? '币' : '股'}</div>`;

    // ─── 3. 持仓价值（USD）───
    const valueUsd = (p.current_value_usd != null && isFinite(p.current_value_usd))
      ? Number(p.current_value_usd)
      : (p.total_cost_usd != null ? Number(p.total_cost_usd) : null);
    const valueCell = valueUsd != null
      ? `<div style="font-weight:600;">${_fmtUsd(valueUsd)}</div>
         <div style="font-size:10px;color:var(--text-tertiary);">USD</div>`
      : '<span style="color:var(--text-tertiary);">—</span>';

    // ─── 4. 入场→现价 ───
    const curPrice = p.current_price;
    const avgCost = p.avg_cost;
    let priceCell = '<span style="color:var(--text-tertiary);">—</span>';
    if (avgCost != null) {
      const cur = curPrice != null ? _fmtMoney(p.market, curPrice, 4) : '—';
      const avg = _fmtMoney(p.market, avgCost, 4);
      let arrowColor = 'var(--text-tertiary)', arrow = '→';
      if (curPrice != null && avgCost > 0) {
        const up = side === 'long' ? curPrice > avgCost : curPrice < avgCost;
        arrowColor = up ? 'var(--color-up)' : 'var(--color-down)';
        arrow = up ? '↗' : '↘';
      }
      priceCell = `
        <div style="font-size:11px;color:var(--text-secondary);">${avg}</div>
        <div style="font-weight:600;font-size:13px;color:${arrowColor};">${arrow} ${cur}</div>
        <div style="font-size:10px;color:var(--text-tertiary);">${ccyCode}</div>`;
    }

    // ─── 5. 浮动盈亏（金额 + %）───
    const pnlLocal = p.pnl_local == null ? null : Number(p.pnl_local);
    const pnlUsd   = p.pnl_usd   == null ? null : Number(p.pnl_usd);
    const pnlPct   = p.pnl_pct   == null ? null : Number(p.pnl_pct);
    let pnlCell = '<span style="color:var(--text-tertiary);">—</span>';
    if (pnlLocal != null && isFinite(pnlLocal)) {
      const color = pnlLocal >= 0 ? 'var(--color-up)' : 'var(--color-down)';
      const sign = pnlLocal >= 0 ? '+' : '';
      const localStr = `${sign}${_fmtMoney(p.market, pnlLocal, 2)} ${ccyCode}`;
      const usdHint = (ccyCode !== 'USD' && ccyCode !== 'USDT' && pnlUsd != null && isFinite(pnlUsd))
        ? `<div style="font-size:10px;color:var(--text-tertiary);">≈ ${sign}${_fmtUsd(Math.abs(pnlUsd))}</div>`
        : '';
      const pctStr = (pnlPct != null && isFinite(pnlPct))
        ? `<div style="font-size:11px;color:${color};font-weight:600;">${pnlPct>=0?'+':''}${pnlPct.toFixed(2)}%</div>`
        : '';
      pnlCell = `
        <div style="color:${color};font-weight:700;font-size:13px;">${localStr}</div>
        ${pctStr}${usdHint}`;
    }

    // ─── 6. 止盈止损区间进度条（mark price 落在 SL→TP 之间的位置）───
    const sl = p.ai_stop_loss != null ? Number(p.ai_stop_loss) : null;
    const tp = p.ai_take_profit != null ? Number(p.ai_take_profit) : null;
    let slTpCell = '<span style="color:var(--text-tertiary);font-size:10px;">未设置</span>';
    if (sl != null && tp != null && curPrice != null && sl > 0 && tp > 0) {
      const lo = Math.min(sl, tp), hi = Math.max(sl, tp);
      const range = hi - lo;
      let pos = range > 0 ? ((curPrice - lo) / range) * 100 : 50;
      pos = Math.max(0, Math.min(100, pos));
      // 多头：左 SL → 右 TP；空头反过来
      const isLong = side === 'long';
      const leftLabel = isLong ? `SL ${_fmtMoney(p.market, sl, 4)}` : `TP ${_fmtMoney(p.market, tp, 4)}`;
      const rightLabel = isLong ? `TP ${_fmtMoney(p.market, tp, 4)}` : `SL ${_fmtMoney(p.market, sl, 4)}`;
      const leftColor = isLong ? 'var(--color-down)' : 'var(--color-up)';
      const rightColor = isLong ? 'var(--color-up)' : 'var(--color-down)';
      // 距离百分比（绝对值）
      const distSL = Math.abs((curPrice - sl) / curPrice * 100);
      const distTP = Math.abs((tp - curPrice) / curPrice * 100);
      slTpCell = `
        <div style="position:relative;">
          <div class="port-sltp-bar">
            <div class="port-sltp-bar-grad" style="background:linear-gradient(90deg, ${leftColor} 0%, var(--bg-tertiary) 50%, ${rightColor} 100%);"></div>
            <div class="port-sltp-marker" style="left:${pos.toFixed(1)}%;" title="现价 ${_fmtMoney(p.market, curPrice, 4)} (距 SL ${distSL.toFixed(1)}% / 距 TP ${distTP.toFixed(1)}%)"></div>
          </div>
          <div style="display:flex;justify-content:space-between;font-size:9px;margin-top:2px;color:var(--text-tertiary);">
            <span style="color:${leftColor};">${leftLabel}</span>
            <span style="color:${rightColor};">${rightLabel}</span>
          </div>
        </div>`;
    } else if (sl != null || tp != null) {
      const slTxt = sl != null ? `SL ${_fmtMoney(p.market, sl, 4)}` : '';
      const tpTxt = tp != null ? `TP ${_fmtMoney(p.market, tp, 4)}` : '';
      slTpCell = `<span style="color:var(--text-tertiary);font-size:10px;">${[slTxt, tpTxt].filter(Boolean).join(' / ')}</span>`;
    }

    // ─── 7. AI 建议气泡 ───
    const adv = _latestAdvices[p.id];
    let adviceCell = `<div class="port-advice-empty"><span style="color:var(--text-tertiary);font-size:10px;">⏳ 等待 AI 分析</span></div>`;
    if (adv && adv.advice) {
      const label = ADVICE_LABEL[adv.advice] || adv.advice;
      const color = ADVICE_COLOR[adv.advice] || 'var(--text-secondary)';
      const bg = adv.advice === 'add' ? 'rgba(76,175,80,0.12)'
        : adv.advice === 'reduce' ? 'rgba(255,167,38,0.12)'
        : adv.advice === 'close' ? 'rgba(215,90,90,0.12)'
        : 'rgba(120,140,180,0.10)';
      const when = adv.advised_at
        ? new Date(adv.advised_at * 1000 || adv.advised_at).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false})
        : '';
      const reasonFull = (adv.reason || '').replace(/"/g, '&quot;');
      const reasonShort = (adv.reason || '').substring(0, 50);
      adviceCell = `
        <div class="port-advice-pill" style="background:${bg};border-left:3px solid ${color};" title="${reasonFull}">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;">
            <span style="color:${color};font-weight:700;font-size:12px;">${label}</span>
            <span style="font-size:9px;color:var(--text-tertiary);">${when}</span>
          </div>
          <div style="font-size:10px;color:var(--text-secondary);line-height:1.4;margin-top:2px;">
            ${reasonShort}${(adv.reason||'').length>50?'…':''}
          </div>
        </div>`;
    }

    // ─── 8. 操作按钮 ───
    const actions = `
      <div style="display:inline-flex;gap:4px;align-items:center;">
        <button class="btn btn-sm" data-action="view" data-symbol="${p.symbol}" data-market="${p.market}"
          style="font-size:10px;padding:3px 8px;" title="切换到此品种 K 线图">📊</button>
        <button class="btn btn-sm" data-action="advise" data-id="${p.id}"
          style="font-size:10px;padding:3px 8px;" title="主动调用 AI 出建议（约 30-60s）">🤖</button>
        <button class="btn btn-sm port-kebab" data-id="${p.id}" data-symbol="${p.symbol}"
          style="font-size:14px;padding:1px 8px;line-height:1;" title="更多">⋮</button>
      </div>`;

    return `
      <tr class="port-row" style="border-bottom:1px solid var(--border-secondary);">
        <td style="padding:8px 10px;">${symbolCell}</td>
        <td style="padding:8px 10px;text-align:right;font-size:11px;">${qtyCell}</td>
        <td style="padding:8px 10px;text-align:right;font-size:11px;">${valueCell}</td>
        <td style="padding:8px 10px;text-align:right;font-size:11px;">${priceCell}</td>
        <td style="padding:8px 10px;text-align:right;">${pnlCell}</td>
        <td style="padding:8px 10px;">${slTpCell}</td>
        <td style="padding:8px 10px;">${adviceCell}</td>
        <td style="padding:8px 10px;text-align:right;white-space:nowrap;">${actions}</td>
      </tr>
    `;
  }

  // ─── 空仓教程卡片 ───
  function _renderEmptyState() {
    return `
      <div style="padding:40px 20px;display:flex;justify-content:center;">
        <div style="max-width:580px;width:100%;background:var(--bg-secondary);border:1px solid var(--border-secondary);border-radius:8px;padding:24px;text-align:center;">
          <div style="font-size:42px;margin-bottom:8px;">💼</div>
          <h3 style="margin:0 0 8px;font-size:16px;font-weight:600;">还没有持仓</h3>
          <p style="margin:0 0 18px;color:var(--text-secondary);font-size:12px;line-height:1.6;">
            添加你的实际持仓后，系统会：<br>
            ✅ 自动监控价格 + 计算浮动盈亏<br>
            ✅ AI 实时给出操作建议（持有/加仓/减仓/平仓）<br>
            ✅ 重大新闻/异动触发 → Telegram 推送
          </p>
          <div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap;">
            <button id="empty-add-btn" class="btn btn-primary btn-sm" style="font-size:12px;">＋ 添加第一笔持仓</button>
            <button id="empty-auto-btn" class="btn btn-sm" style="font-size:12px;" title="开启后 AI confirm 信号 + rating=buy 会自动模拟下单">🤖 开启自动交易</button>
          </div>
          <div style="margin-top:18px;padding-top:14px;border-top:1px solid var(--border-secondary);font-size:11px;color:var(--text-tertiary);text-align:left;">
            <strong style="color:var(--text-secondary);">小贴士：</strong>
            添加时填的<strong>成本价</strong>是按市场币种（A股 CNY、港股 HKD、美股/加密 USD），
            汇率会按当时实时换算成 USD 入账。
          </div>
        </div>
      </div>
    `;
  }

  function _bindEmptyStateEvents(listEl) {
    const addBtn = listEl.querySelector('#empty-add-btn');
    const autoBtn = listEl.querySelector('#empty-auto-btn');
    if (addBtn) addBtn.addEventListener('click', () => {
      const f = document.querySelector('.bottom-pane[data-pane="portfolio"] .portfolio-add-form');
      if (f) f.style.display = 'block';
    });
    if (autoBtn) autoBtn.addEventListener('click', () => {
      const t = document.getElementById('auto-trade-toggle');
      if (t && !t.checked) { t.checked = true; t.dispatchEvent(new Event('change')); }
    });
  }

  function _showAdvicesModal(arr) {
    // 复用现有 modal-overlay，没有就创建
    let overlay = document.getElementById('portfolio-advices-modal');
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.id = 'portfolio-advices-modal';
      overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center;';
      overlay.addEventListener('click', (e) => {
        if (e.target === overlay) overlay.remove();
      });
      document.body.appendChild(overlay);
    } else {
      overlay.innerHTML = '';
      overlay.style.display = 'flex';
    }
    const ADVICE_LABEL = { hold: '持有', reduce: '减仓', add: '加仓', close: '清仓' };
    const ADVICE_COLOR = { hold: 'var(--text-secondary)', reduce: 'var(--color-warning)', add: 'var(--color-up)', close: 'var(--color-down)' };
    const rows = arr.map(a => {
      const color = ADVICE_COLOR[a.advice] || 'var(--text-secondary)';
      const label = ADVICE_LABEL[a.advice] || a.advice || '-';
      const ts = new Date((a.advised_at || 0) * 1000).toLocaleString();
      return `<tr style="border-bottom:1px solid var(--border-secondary);">
        <td style="padding:6px 10px;color:var(--text-tertiary);font-size:11px;white-space:nowrap;">${ts}</td>
        <td style="padding:6px 10px;color:${color};font-weight:600;white-space:nowrap;">${label}</td>
        <td style="padding:6px 10px;color:var(--text-secondary);font-size:12px;line-height:1.5;">${(a.reason || '-').replace(/</g,'&lt;')}</td>
      </tr>`;
    }).join('');
    overlay.innerHTML = `
      <div style="background:var(--bg-secondary);border-radius:8px;width:min(720px,90vw);max-height:80vh;display:flex;flex-direction:column;box-shadow:0 8px 32px rgba(0,0,0,0.4);">
        <div style="display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid var(--border-secondary);">
          <span style="font-weight:600;">📋 历史 AI 建议（${arr.length} 条）</span>
          <button id="advices-close" class="btn btn-sm" style="font-size:14px;padding:2px 10px;">×</button>
        </div>
        <div style="overflow-y:auto;flex:1;">
          <table style="width:100%;border-collapse:collapse;font-size:12px;">
            <thead style="position:sticky;top:0;background:var(--bg-secondary);">
              <tr style="border-bottom:1px solid var(--border-primary);">
                <th style="padding:8px 10px;text-align:left;color:var(--text-tertiary);font-weight:600;">时间</th>
                <th style="padding:8px 10px;text-align:left;color:var(--text-tertiary);font-weight:600;">建议</th>
                <th style="padding:8px 10px;text-align:left;color:var(--text-tertiary);font-weight:600;">理由</th>
              </tr>
            </thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </div>
    `;
    overlay.querySelector('#advices-close')?.addEventListener('click', () => overlay.remove());
  }

  function _showTradesModal(symbol, items, summary) {
    let overlay = document.getElementById('portfolio-trades-modal');
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.id = 'portfolio-trades-modal';
      overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center;';
      overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
      document.body.appendChild(overlay);
    } else {
      overlay.innerHTML = '';
      overlay.style.display = 'flex';
    }
    const ACTION_ZH = { open: '📥 开仓', add: '➕ 加仓', reduce: '➖ 减仓', close: '🏁 平仓' };
    const ACTION_COLOR = {
      open: 'var(--color-up)', add: 'var(--color-up)',
      reduce: 'var(--color-warning)', close: 'var(--color-down)',
    };
    const _esc = (s) => (s == null ? '' : String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])));

    const hasItems = items && items.length;
    const execItems = (items || []).filter(i => i.status === 'executed');
    const rejectedItems = (items || []).filter(i => i.status !== 'executed');

    const rowsExec = execItems.map(it => {
      const t = new Date(it.traded_at * 1000).toLocaleString('zh-CN', { month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hour12:false });
      const actColor = ACTION_COLOR[it.action] || 'var(--text-secondary)';
      const actLabel = ACTION_ZH[it.action] || it.action;
      const pnlDetail = it.trigger_detail && it.trigger_detail.realized_pnl_usd != null
        ? `<div style="font-size:11px;color:${it.trigger_detail.realized_pnl_usd >= 0 ? 'var(--color-up)' : 'var(--color-down)'};margin-top:3px;">整单累计盈亏 ${it.trigger_detail.realized_pnl_usd >= 0 ? '+' : ''}$${it.trigger_detail.realized_pnl_usd} (${it.trigger_detail.realized_pnl_pct >= 0 ? '+' : ''}${it.trigger_detail.realized_pnl_pct}%)</div>`
        : '';
      return `<tr style="border-bottom:1px solid var(--border-secondary);">
        <td style="padding:6px 10px;color:var(--text-tertiary);font-size:11px;white-space:nowrap;">${t}</td>
        <td style="padding:6px 10px;color:${actColor};font-weight:600;white-space:nowrap;">${actLabel}</td>
        <td style="padding:6px 10px;text-align:right;">${(it.quantity||0).toFixed(6)}</td>
        <td style="padding:6px 10px;text-align:right;">${(it.price||0).toFixed(4)}</td>
        <td style="padding:6px 10px;text-align:right;font-weight:600;">$${(it.amount_usd||0).toFixed(2)}</td>
        <td style="padding:6px 10px;color:var(--text-secondary);font-size:11px;">${_esc(it.reason||'-')}${pnlDetail}</td>
      </tr>`;
    }).join('');

    const rowsRejected = rejectedItems.map(it => {
      const t = new Date(it.traded_at * 1000).toLocaleString('zh-CN', { month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hour12:false });
      return `<tr style="border-bottom:1px solid var(--border-secondary);opacity:0.6;">
        <td style="padding:6px 10px;color:var(--text-tertiary);font-size:11px;white-space:nowrap;">${t}</td>
        <td style="padding:6px 10px;color:var(--text-tertiary);white-space:nowrap;">${ACTION_ZH[it.action] || it.action}</td>
        <td colspan="3" style="padding:6px 10px;color:var(--text-tertiary);font-size:11px;">⏸ 拒绝: ${_esc(it.rejected_reason || '-')}</td>
        <td style="padding:6px 10px;color:var(--text-tertiary);font-size:11px;">${_esc(it.reason || '-')}</td>
      </tr>`;
    }).join('');

    // 汇总区：已平仓 vs 持仓中 显示不同内容
    let summaryHtml = '';
    if (summary) {
      const statusBadge = summary.is_closed
        ? '<span style="background:rgba(215,90,90,0.2);color:var(--color-down);padding:2px 8px;border-radius:4px;font-size:11px;">已平仓</span>'
        : '<span style="background:rgba(90,175,90,0.2);color:var(--color-up);padding:2px 8px;border-radius:4px;font-size:11px;">持仓中</span>';

      let pnlBlock = '';
      if (summary.is_closed) {
        // 已平仓：显示已实现盈亏
        const c = summary.realized_pnl_usd >= 0 ? 'var(--color-up)' : 'var(--color-down)';
        pnlBlock = `<span style="color:${c};font-weight:600;">
          已实现盈亏: ${summary.realized_pnl_usd >= 0 ? '+' : ''}$${summary.realized_pnl_usd}
          (${summary.realized_pnl_pct >= 0 ? '+' : ''}${summary.realized_pnl_pct}%)
        </span>`;
      } else {
        // 持仓中：显示浮盈 + 若立即平仓的预估盈亏
        const uc = (summary.unrealized_pnl_usd || 0) >= 0 ? 'var(--color-up)' : 'var(--color-down)';
        const pc = (summary.projected_pnl_usd || 0) >= 0 ? 'var(--color-up)' : 'var(--color-down)';
        pnlBlock = `
          <span style="color:${uc};font-weight:600;" title="当前市值 − 成本">
            浮盈: ${(summary.unrealized_pnl_usd||0)>=0?'+':''}$${summary.unrealized_pnl_usd||0}
          </span>
          <span style="color:var(--text-tertiary);font-size:11px;">当前市值 $${summary.current_market_value_usd||0}</span>
          <span style="color:${pc};font-size:11px;" title="已减仓收入 + 当前市值 − 总买入">
            若此刻全平: ${(summary.projected_pnl_usd||0)>=0?'+':''}$${summary.projected_pnl_usd||0}
            (${(summary.projected_pnl_pct||0)>=0?'+':''}${summary.projected_pnl_pct||0}%)
          </span>`;
      }

      summaryHtml = `
        <div style="padding:12px 16px;border-bottom:1px solid var(--border-secondary);background:var(--bg-tertiary);">
          <div style="display:flex;gap:16px;flex-wrap:wrap;font-size:12px;align-items:center;">
            ${statusBadge}
            <span>共 <strong>${summary.leg_count}</strong> 笔</span>
            <span>买入 <strong>$${summary.total_bought_usd.toFixed(2)}</strong></span>
            <span>卖出 <strong>$${summary.total_sold_usd.toFixed(2)}</strong></span>
            ${pnlBlock}
          </div>
        </div>`;
    }

    const bodyHtml = hasItems ? `
      <table style="width:100%;border-collapse:collapse;font-size:12px;">
        <thead style="position:sticky;top:0;background:var(--bg-secondary);z-index:1;">
          <tr style="border-bottom:1px solid var(--border-primary);">
            <th style="padding:8px 10px;text-align:left;color:var(--text-tertiary);">时间</th>
            <th style="padding:8px 10px;text-align:left;color:var(--text-tertiary);">操作</th>
            <th style="padding:8px 10px;text-align:right;color:var(--text-tertiary);">数量</th>
            <th style="padding:8px 10px;text-align:right;color:var(--text-tertiary);">价格</th>
            <th style="padding:8px 10px;text-align:right;color:var(--text-tertiary);">金额(USD)</th>
            <th style="padding:8px 10px;text-align:left;color:var(--text-tertiary);">理由 / 累计盈亏</th>
          </tr>
        </thead>
        <tbody>${rowsExec}${rowsRejected}</tbody>
      </table>
    ` : `
      <div style="padding:40px;text-align:center;color:var(--text-tertiary);">
        这一单没有自动交易记录。<br>
        <span style="font-size:11px;">仅手动添加的持仓或者自动交易未覆盖的品种可能出现此情况。</span>
      </div>`;

    overlay.innerHTML = `
      <div style="background:var(--bg-secondary);border-radius:8px;width:min(960px,92vw);max-height:85vh;display:flex;flex-direction:column;box-shadow:0 8px 32px rgba(0,0,0,0.4);">
        <div style="display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid var(--border-secondary);">
          <span style="font-weight:600;">📊 ${_esc(symbol)} 交易明细（${execItems.length} 笔成交 + ${rejectedItems.length} 笔拒绝）</span>
          <button id="trades-close" class="btn btn-sm" style="font-size:14px;padding:2px 10px;">×</button>
        </div>
        ${summaryHtml}
        <div style="overflow-y:auto;flex:1;">${bodyHtml}</div>
      </div>
    `;
    overlay.querySelector('#trades-close')?.addEventListener('click', () => overlay.remove());
  }

  // (v12.13: WhatsApp 模块已整体移除，仅保留 Telegram)

  // ─── Telegram 通知配置 modal ───
  async function _showTelegramModal() {
    let cur = {};
    try {
      const r = await fetch('/api/settings');
      if (r.ok) cur = await r.json();
    } catch {}
    let overlay = document.getElementById('telegram-config-modal');
    if (overlay) overlay.remove();
    overlay = document.createElement('div');
    overlay.id = 'telegram-config-modal';
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center;';
    overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
    const enabled = !!cur.telegram_enabled;
    const tokenMask = cur.telegram_bot_token || '';
    const chatId = cur.telegram_chat_id || '';

    overlay.innerHTML = `
      <div style="background:var(--bg-secondary);border-radius:8px;width:min(560px,92vw);max-height:88vh;overflow-y:auto;box-shadow:0 8px 32px rgba(0,0,0,0.5);">
        <div style="padding:14px 18px;border-bottom:1px solid var(--border-secondary);display:flex;justify-content:space-between;align-items:center;">
          <strong style="font-size:14px;">✈️ Telegram 自动交易通知配置</strong>
          <button id="tg-close" class="btn btn-sm" style="font-size:14px;padding:2px 10px;">×</button>
        </div>
        <div style="padding:16px 18px;font-size:12px;line-height:1.6;">
          <div style="background:var(--bg-tertiary);padding:10px;border-radius:4px;margin-bottom:14px;color:var(--text-secondary);">
            ℹ️ 开启后，每次自动交易（开仓/加仓/减仓/平仓）都会推送到指定 Telegram 对话。<br>
            同一品种 60 秒内同种动作只发 1 条（防洪流）。Telegram Bot API <strong>免费、无量限</strong>。
          </div>

          <label style="display:flex;align-items:center;gap:8px;margin-bottom:14px;">
            <input type="checkbox" id="tg-enabled" ${enabled?'checked':''}>
            <strong>启用 Telegram 通知</strong>
          </label>

          <div style="border:1px solid var(--border-secondary);border-radius:4px;padding:10px;margin-bottom:12px;">
            <div style="font-weight:600;margin-bottom:6px;">配置步骤</div>
            <ol style="font-size:11px;color:var(--text-tertiary);margin:0 0 8px 18px;padding:0;">
              <li>在 Telegram 里搜 <a href="https://t.me/BotFather" target="_blank" style="color:var(--color-accent);">@BotFather</a> → <code>/newbot</code> 创建你的 bot，拿到 token</li>
              <li>找到你的 bot 发送 <code>/start</code>（或拉进群）</li>
              <li>浏览器打开 <code>https://api.telegram.org/bot&lt;TOKEN&gt;/getUpdates</code>，从结果里复制 <code>chat.id</code> 字段（私聊为正数，群为负数）</li>
            </ol>

            <div style="margin-bottom:8px;">
              <label style="display:block;margin-bottom:2px;color:var(--text-secondary);">Bot Token</label>
              <input id="tg-token" type="password" class="input" style="width:100%;" placeholder="123456:AAAA-BBBB..." value="${tokenMask}">
            </div>
            <div>
              <label style="display:block;margin-bottom:2px;color:var(--text-secondary);">Chat ID</label>
              <input id="tg-chat-id" class="input" style="width:100%;" placeholder="例 123456789 (私聊) 或 -1001234567890 (群)" value="${chatId}">
            </div>
          </div>

          <div style="display:flex;gap:8px;justify-content:flex-end;">
            <button id="tg-test" class="btn btn-sm" style="color:var(--color-accent);">🧪 发送测试消息</button>
            <button id="tg-save" class="btn btn-primary btn-sm">保存配置</button>
          </div>
          <div id="tg-result" style="margin-top:10px;font-size:11px;"></div>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);

    overlay.querySelector('#tg-close').addEventListener('click', () => overlay.remove());

    overlay.querySelector('#tg-save').addEventListener('click', async () => {
      const body = {
        telegram_enabled: overlay.querySelector('#tg-enabled').checked,
        telegram_chat_id: overlay.querySelector('#tg-chat-id').value.trim(),
      };
      const tk = overlay.querySelector('#tg-token').value;
      if (tk && !tk.includes('****')) body.telegram_bot_token = tk;
      const res = overlay.querySelector('#tg-result');
      try {
        const r = await fetch('/api/settings', {
          method: 'PUT', headers: {'Content-Type':'application/json'},
          body: JSON.stringify(body),
        });
        if (r.ok) {
          res.style.color = 'var(--color-up)';
          res.textContent = '✅ 配置已保存';
        } else {
          res.style.color = 'var(--color-down)';
          res.textContent = `❌ 保存失败: HTTP ${r.status}`;
        }
      } catch (e) {
        res.style.color = 'var(--color-down)';
        res.textContent = `❌ 保存失败: ${e.message}`;
      }
    });

    overlay.querySelector('#tg-test').addEventListener('click', async () => {
      const res = overlay.querySelector('#tg-result');
      res.style.color = 'var(--text-secondary)';
      res.textContent = '⏳ 正在发送测试消息...';
      try {
        const r = await fetch('/api/settings/telegram-test', {method: 'POST'});
        const data = await r.json();
        if (data.ok) {
          res.style.color = 'var(--color-up)';
          res.textContent = `✅ 测试消息已发送（chat_id=${data.chat_id}）。检查 Telegram 是否收到。`;
        } else {
          res.style.color = 'var(--color-down)';
          res.textContent = `❌ ${data.error || '发送失败'}`;
        }
      } catch (e) {
        res.style.color = 'var(--color-down)';
        res.textContent = `❌ ${e.message}`;
      }
    });
  }

  return { init, refresh, render };
})();

window.Portfolio = Portfolio;
