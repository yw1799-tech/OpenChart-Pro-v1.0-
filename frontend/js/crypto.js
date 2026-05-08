/* ============================================================
   Crypto 模块 — 加密诊断面板
   功能：6 个币种卡片展示（价格/资金费率/多空比/AI 评级）+ 详情 modal
   ============================================================ */

const Crypto = (function () {
  let _items = [];
  let _autoRefreshTimer = null;
  let _inited = false;

  const RATING_LABEL = {
    strong_buy: { label: '🟢 强烈买入', color: 'var(--color-up)' },
    buy:        { label: '🟢 买入', color: 'var(--color-up)' },
    hold:       { label: '⚪ 持有观察', color: 'var(--text-secondary)' },
    reduce:     { label: '🟡 减仓', color: 'var(--color-warning)' },
    sell:       { label: '🔴 卖出', color: 'var(--color-down)' },
  };
  const REGIME_COLOR = {
    '趋势多头': 'var(--color-up)',
    '趋势空头': 'var(--color-down)',
    '震荡': 'var(--text-secondary)',
    '顶部拥挤': 'var(--color-warning)',
    '底部反转': 'var(--color-up)',
  };

  function _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function init() {
    if (_inited) return;
    _inited = true;
    _ensureDom();
    refresh();
    _autoRefreshTimer = setInterval(refresh, 120000); // 2 分钟轮询
    if (window.__visibilityHandlers) {
      window.__visibilityHandlers.push(({ hidden }) => {
        if (hidden) {
          if (_autoRefreshTimer) { clearInterval(_autoRefreshTimer); _autoRefreshTimer = null; }
        } else if (!_autoRefreshTimer) {
          refresh();
          _autoRefreshTimer = setInterval(refresh, 120000);
        }
      });
    }
    console.log('[Crypto] 已初始化');
  }

  function _ensureDom() {
    const pane = document.querySelector('.bottom-pane[data-pane="crypto"]');
    if (!pane || pane.dataset.inited) return;
    pane.dataset.inited = '1';
    pane.innerHTML = `
      <div class="oc-toolbar crypto-toolbar">
        <span class="oc-text-lg" style="font-weight:600;">🪙 加密诊断</span>
        <span class="oc-text-sm oc-muted">6 币种 · 每 2 分钟刷新 · 后台 30 分钟自动 LLM 诊断</span>
        <span class="oc-toolbar-spacer"></span>
        <button id="crypto-refresh-btn" class="btn btn-sm" title="刷新">🔄</button>
        <span id="crypto-status" class="oc-text-sm oc-muted"></span>
      </div>
      <div class="crypto-list" style="overflow-y:auto;flex:1;min-height:0;padding:12px;"></div>
    `;
    pane.querySelector('#crypto-refresh-btn').addEventListener('click', refresh);
  }

  async function refresh() {
    try {
      const r = await fetch('/api/crypto/list');
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      _items = d.items || [];
      render();
      const statusEl = document.querySelector('#crypto-status');
      if (statusEl) statusEl.textContent = `${_items.length} 币 · ${new Date().toLocaleTimeString()}`;
    } catch (e) {
      console.warn('[Crypto] 刷新失败:', e);
    }
  }

  function render() {
    const list = document.querySelector('.crypto-list');
    if (!list) return;
    if (!_items.length) {
      list.innerHTML = `
        <div class="oc-empty">
          <div class="oc-empty-icon">🪙</div>
          <div class="oc-empty-title">加载中...</div>
          <div class="oc-empty-hint">首次启动需 30-60s 拉取 6 币种数据</div>
        </div>`;
      return;
    }
    list.innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:12px;">
        ${_items.map(_renderCard).join('')}
      </div>`;
    if (!list._delegated) {
      list._delegated = true;
      list.addEventListener('click', (e) => {
        const card = e.target.closest('[data-symbol]');
        if (card) _showDetail(card.dataset.symbol);
      });
    }
  }

  function _renderCard(item) {
    const sym = item.symbol;
    const d = item.diagnosis;
    const ts = item.diagnosed_at ? new Date(item.diagnosed_at * 1000).toLocaleString('zh-CN', { month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit' }) : '尚未诊断';
    const RATING_CHIP_CLASS = {
      strong_buy: 'oc-chip-up', buy: 'oc-chip-up',
      hold: 'oc-chip-neutral',
      reduce: 'oc-chip-warn', sell: 'oc-chip-down',
    };
    const ratingChip = d
      ? `<span class="oc-chip ${RATING_CHIP_CLASS[d.rating] || 'oc-chip-neutral'}">${(RATING_LABEL[d.rating] || {}).label || d.rating || '-'}</span>`
      : '<span class="oc-chip oc-chip-neutral">⏳ 待诊断</span>';
    const regime = d ? d.market_regime : '';
    const regimeColor = REGIME_COLOR[regime] || 'var(--text-secondary)';
    const summary = d ? (d.summary || '') : '后台 30 分钟内自动诊断 · 点卡片立即触发';
    const kl = d ? (d.key_levels || {}) : {};
    const priceStr = `$${(item.price || 0).toFixed(item.price > 1 ? 2 : 4)}`;
    return `
      <div class="crypto-card oc-card" data-symbol="${_esc(sym)}" style="cursor:pointer;transition:all 0.2s;padding:14px;" onmouseover="this.style.borderColor='var(--color-accent)';this.style.boxShadow='0 0 0 1px var(--color-accent-glow)'" onmouseout="this.style.borderColor='var(--border-secondary)';this.style.boxShadow='none'">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;">
          <div>
            <div style="font-weight:700;font-size:16px;">${_esc(sym)}</div>
            <div class="oc-text-xs oc-muted oc-num" style="margin-top:2px;">${priceStr}</div>
          </div>
          <div style="text-align:right;display:flex;flex-direction:column;gap:4px;align-items:flex-end;">
            ${ratingChip}
            ${d && d.confidence ? `<span class="oc-text-xs oc-muted">置信度 ${d.confidence}</span>` : ''}
          </div>
        </div>
        ${regime ? `<div class="oc-text-sm" style="margin-bottom:8px;">📊 <span style="color:${regimeColor};font-weight:500;">${_esc(regime)}</span></div>` : ''}
        <div class="oc-text-sm oc-secondary" style="line-height:1.5;min-height:40px;">${_esc(summary.substring(0, 130))}${summary.length > 130 ? '…' : ''}</div>
        ${(kl.support || kl.resistance) ? `
        <div class="oc-text-xs oc-muted oc-num" style="margin-top:10px;display:flex;gap:12px;padding-top:8px;border-top:1px solid var(--border-secondary);">
          ${kl.support ? `<span>🛡️ 支 <strong style="color:var(--color-up);">${kl.support}</strong></span>` : ''}
          ${kl.resistance ? `<span>🚧 阻 <strong style="color:var(--color-down);">${kl.resistance}</strong></span>` : ''}
          <span style="margin-left:auto;color:var(--text-tertiary);">${ts}</span>
        </div>` : `<div class="oc-text-xs oc-muted" style="margin-top:8px;text-align:right;">${ts}</div>`}
      </div>`;
  }

  async function _showDetail(symbol) {
    let overlay = document.getElementById('crypto-detail-modal');
    if (overlay) overlay.remove();
    overlay = document.createElement('div');
    overlay.id = 'crypto-detail-modal';
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center;';
    overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
    document.body.appendChild(overlay);
    overlay.innerHTML = `
      <div style="background:var(--bg-secondary);border-radius:8px;width:min(760px,94vw);max-height:90vh;display:flex;flex-direction:column;box-shadow:0 8px 32px rgba(0,0,0,0.4);">
        <div style="display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid var(--border-secondary);">
          <span style="font-weight:600;font-size:14px;">🪙 ${_esc(symbol)} 详细诊断</span>
          <div style="display:flex;gap:8px;">
            <button id="cd-view" class="btn btn-sm" title="看该币种 K 线">📈 看图</button>
            <button id="cd-refresh" class="btn btn-sm" title="立即重新诊断（耗时 30-60s）">🔄 重诊断</button>
            <button id="cd-close" class="btn btn-sm" style="font-size:14px;padding:2px 10px;">×</button>
          </div>
        </div>
        <div id="cd-body" style="overflow-y:auto;flex:1;padding:14px 18px;font-size:12px;line-height:1.6;">
          <div style="text-align:center;padding:40px;color:var(--text-tertiary);">⏳ 加载中...</div>
        </div>
      </div>`;
    overlay.querySelector('#cd-close').addEventListener('click', () => overlay.remove());
    overlay.querySelector('#cd-view').addEventListener('click', () => {
      if (typeof switchMarket === 'function' && window.currentMarket !== 'crypto') switchMarket('crypto');
      if (typeof switchSymbol === 'function') switchSymbol(symbol, 'crypto');
      overlay.remove();
    });
    const body = overlay.querySelector('#cd-body');
    overlay.querySelector('#cd-refresh').addEventListener('click', async () => {
      body.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-tertiary);">⏳ AI 诊断中（LLM 推理 30-60s）...</div>';
      try {
        const r = await fetch(`/api/crypto/${encodeURIComponent(symbol)}/diagnose`, { method: 'POST' });
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${r.status}`);
        }
        await _loadDetail(symbol, body);
        refresh();
      } catch (e) {
        body.innerHTML = `<div style="color:var(--color-down);padding:20px;">诊断失败: ${_esc(e.message)}</div>`;
      }
    });
    await _loadDetail(symbol, body);
  }

  async function _loadDetail(symbol, body) {
    try {
      const [diagR, insR] = await Promise.all([
        fetch(`/api/crypto/${encodeURIComponent(symbol)}/diagnosis`).then(r => r.ok ? r.json() : null),
        fetch(`/api/crypto/${encodeURIComponent(symbol)}/insights`).then(r => r.ok ? r.json() : null),
      ]);
      body.innerHTML = _renderDetailBody(symbol, diagR, insR);
    } catch (e) {
      body.innerHTML = `<div style="color:var(--color-down);padding:20px;">加载失败: ${_esc(e.message)}</div>`;
    }
  }

  function _renderDetailBody(symbol, diagR, ins) {
    const diag = diagR && diagR.diagnosis;
    const ts = diagR && diagR.diagnosed_at ? new Date(diagR.diagnosed_at * 1000).toLocaleString() : '尚未诊断';
    const ratingInfo = diag ? (RATING_LABEL[diag.rating] || { label: diag.rating || '-', color: 'var(--text-secondary)' }) : null;
    const regimeColor = diag ? (REGIME_COLOR[diag.market_regime] || 'var(--text-secondary)') : '';

    // 期货市场情绪区块
    const ticker = (ins && ins.ticker) || {};
    const funding = ((ins && ins.funding_rate) || {}).current || {};
    const oi = (ins && ins.oi_history) || {};
    const ls = ((ins && ins.long_short_ratio) || {}).current || {};
    const lsSig = (ins && ins.long_short_ratio && ins.long_short_ratio.signal) || '-';
    const top = ((ins && ins.top_trader_ratio) || {}).current || {};
    const topSig = (ins && ins.top_trader_ratio && ins.top_trader_ratio.signal) || '-';
    const taker = ((ins && ins.taker_volume) || {}).current || {};
    const takerSig = (ins && ins.taker_volume && ins.taker_volume.signal) || '-';
    const fng = (ins && ins.fear_greed) || {};

    const fmtPct = (v, plus) => v == null ? '-' : (plus && v > 0 ? '+' : '') + v.toFixed(2) + '%';
    const color = (v, threshold = 0) => v == null ? 'var(--text-primary)' : v > threshold ? 'var(--color-up)' : v < threshold ? 'var(--color-down)' : 'var(--text-secondary)';

    const marketBlock = `
      <div style="background:var(--bg-tertiary);padding:10px 12px;border-radius:6px;margin-bottom:12px;">
        <div style="font-weight:600;margin-bottom:6px;">📊 市场数据（OKX / Alternative.me）</div>
        <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:4px 16px;font-size:11px;">
          <div>最新价: <strong>$${(ticker.last||0).toFixed(ticker.last > 1 ? 2 : 4)}</strong></div>
          <div>24h 涨跌: <span style="color:${color(ticker.change_pct_24h)};font-weight:600;">${fmtPct(ticker.change_pct_24h, true)}</span></div>
          <div>24h 高/低: ${ticker.high24h ? ticker.high24h.toFixed(2) : '-'} / ${ticker.low24h ? ticker.low24h.toFixed(2) : '-'}</div>
          <div>24h 成交额: ${ticker.vol_ccy_24h ? (ticker.vol_ccy_24h/1e8).toFixed(2) + ' 亿' : '-'}</div>
        </div>
      </div>
      <div style="background:var(--bg-tertiary);padding:10px 12px;border-radius:6px;margin-bottom:12px;">
        <div style="font-weight:600;margin-bottom:6px;">🎯 期货情绪（过去 1 小时）</div>
        <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:6px 16px;font-size:11px;">
          <div>资金费率: <span style="color:${color(funding.rate_pct||0)};font-weight:600;">${funding.rate_pct != null ? (funding.rate_pct>0?'+':'') + funding.rate_pct.toFixed(4) + '%' : '-'}</span>
            <span style="color:var(--text-tertiary);">(年化 ${funding.annualized_pct!=null ? (funding.annualized_pct>0?'+':'')+funding.annualized_pct.toFixed(1)+'%' : '-'})</span>
          </div>
          <div>持仓量 24h 变化: <span style="color:${color(oi.oi_change_24h_pct||0)};font-weight:600;">${fmtPct(oi.oi_change_24h_pct, true)}</span></div>
          <div>散户多空比: <strong>${ls.ratio != null ? ls.ratio.toFixed(2) : '-'}</strong> <span style="color:var(--text-tertiary);">(${lsSig})</span></div>
          <div>💎 大户多空比: <strong style="color:var(--color-purple);">${top.ratio != null ? top.ratio.toFixed(2) : '-'}</strong> <span style="color:var(--text-tertiary);">(${topSig})</span></div>
          <div>主动买盘占比: <strong>${taker.buy_pct != null ? taker.buy_pct + '%' : '-'}</strong> <span style="color:var(--text-tertiary);">(${takerSig})</span></div>
          <div>😱 恐慌贪婪: <strong>${fng.value != null ? fng.value : '-'}</strong> <span style="color:var(--text-tertiary);">(${fng.label_cn || '-'})</span></div>
        </div>
      </div>`;

    if (!diag) {
      return `${marketBlock}<div style="text-align:center;padding:30px;color:var(--text-tertiary);">
        <div style="font-size:32px;margin-bottom:10px;">🪙</div>
        <div>尚未 AI 诊断</div>
        <div style="font-size:11px;margin-top:6px;">系统每 30 分钟自动诊断一轮，也可点击右上角 "🔄 重诊断" 立即触发</div>
      </div>`;
    }

    const list = (arr, color) => Array.isArray(arr) && arr.length
      ? `<ul style="margin:4px 0 0 18px;padding:0;color:${color || 'var(--text-secondary)'};">${arr.map(x => `<li>${_esc(x)}</li>`).join('')}</ul>`
      : '<div style="color:var(--text-tertiary);font-size:11px;">（无）</div>';
    const kl = diag.key_levels || {};
    const ops = diag.operations || {};
    const horizon = { '1-3d':'短线 1-3 天', '3-14d':'中线 3-14 天', '1-3m':'长线 1-3 月' }[diag.horizon] || diag.horizon || '-';

    return `
      ${marketBlock}
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;padding:10px 12px;background:var(--bg-tertiary);border-radius:6px;">
        <div>
          <span style="font-size:18px;font-weight:700;color:${ratingInfo.color};">${ratingInfo.label}</span>
          <span style="margin-left:12px;color:var(--text-tertiary);">置信度 ${diag.confidence || 0} · ${horizon}</span>
        </div>
        <span style="font-size:11px;color:var(--text-tertiary);">${ts}</span>
      </div>
      <div style="background:var(--bg-tertiary);padding:10px 12px;border-radius:6px;margin-bottom:10px;">
        <div style="margin-bottom:4px;"><strong>📊 市场状态: </strong><span style="color:${regimeColor};font-weight:600;">${_esc(diag.market_regime || '-')}</span></div>
        <div style="font-weight:500;">${_esc(diag.summary || '-')}</div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px;">
        <div>
          <div style="color:var(--color-up);font-weight:600;margin-bottom:4px;">✅ 优势 / 做多理由</div>
          ${list(diag.strengths, 'var(--text-secondary)')}
        </div>
        <div>
          <div style="color:var(--color-down);font-weight:600;margin-bottom:4px;">⚠️ 风险</div>
          ${list(diag.risks, 'var(--text-secondary)')}
        </div>
      </div>
      <div style="background:var(--bg-tertiary);padding:10px 12px;border-radius:6px;margin-bottom:10px;">
        <div style="margin-bottom:4px;"><span style="color:var(--text-tertiary);">📈 技术：</span>${_esc(diag.technical_view || '-')}</div>
        <div style="margin-bottom:4px;"><span style="color:var(--text-tertiary);">🎯 期货：</span>${_esc(diag.derivatives_view || '-')}</div>
        <div><span style="color:var(--text-tertiary);">📰 新闻：</span>${_esc(diag.news_view || '-')}</div>
      </div>
      <div style="background:var(--bg-tertiary);padding:10px 12px;border-radius:6px;margin-bottom:10px;">
        <div style="color:var(--text-tertiary);margin-bottom:4px;">🎯 关键位</div>
        <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:4px 16px;font-size:12px;">
          <div>🛡️ 支撑: <strong>${kl.support ?? '-'}</strong></div>
          <div>🚧 阻力: <strong>${kl.resistance ?? '-'}</strong></div>
          <div>⛔ 止损: <strong>${kl.stop_loss ?? '-'}</strong></div>
          <div>🎯 止盈: <strong>${kl.take_profit ?? '-'}</strong></div>
        </div>
      </div>
      <div style="background:rgba(155,109,213,0.1);padding:10px 12px;border-radius:6px;border-left:3px solid var(--color-purple);">
        <div style="color:var(--color-purple);font-weight:600;margin-bottom:6px;">🚦 操作建议</div>
        <div style="display:grid;grid-template-columns:1fr;gap:6px;">
          <div>📥 <strong>开新仓：</strong>${_esc(ops.open_position || '-')}</div>
          <div>➕ <strong>加仓：</strong>${_esc(ops.add_position || '-')}</div>
          <div>➖ <strong>减仓：</strong>${_esc(ops.reduce_position || '-')}</div>
          <div>🏁 <strong>清仓：</strong>${_esc(ops.close_position || '-')}</div>
        </div>
      </div>`;
  }

  return { init, refresh };
})();

window.Crypto = Crypto;

// v11.5: 移除自启动 DOMContentLoaded — 统一由 app.js 的 lazy init 调度，避免双重 init
