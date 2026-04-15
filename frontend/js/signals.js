/* ============================================================
   Signals 模块 — 策略信号面板 (Phase 4)

   功能：
   - 列表展示策略信号（按生成时间倒序）
   - WebSocket 实时推送信号 + Toast + 声音
   - 点击信号跳转主图并标记触发点
   - 显示策略绑定情况
   ============================================================ */

const Signals = (function () {
  let _items = [];
  let _filterMarket = 'all';
  let _autoRefreshTimer = null;

  const ACTION_COLOR = { buy: 'var(--color-up)', sell: 'var(--color-down)' };
  const ACTION_ICON = { buy: '🟢 BUY', sell: '🔴 SELL' };

  function init() {
    _ensureDom();
    if (typeof ws !== 'undefined' && ws) {
      ws.on('signal', (msg) => {
        if (!msg || !msg.data) return;
        _items.unshift(msg.data);
        if (_items.length > 200) _items.length = 200;
        render();
        const d = msg.data;
        if (typeof showToast === 'function') {
          showToast(
            `📡 ${ACTION_ICON[d.action] || d.action} ${d.symbol} @${d.price?.toFixed(4)} (${d.confidence}%)`,
            d.action === 'buy' ? 'success' : 'warning',
            6000
          );
        }
        // 简单提示音
        try {
          const audio = new Audio('data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=');
          audio.volume = 0.3;
          audio.play().catch(() => {});
        } catch (e) {}
      });
    }
    refresh();
    _autoRefreshTimer = setInterval(refresh, 30000);
    console.log('[Signals] 已初始化');
  }

  function _ensureDom() {
    const pane = document.querySelector('.bottom-pane[data-pane="signals"]');
    if (!pane || pane.dataset.inited) return;
    pane.dataset.inited = '1';
    pane.innerHTML = `
      <div class="signals-toolbar" style="display:flex;gap:8px;padding:8px;border-bottom:1px solid var(--border-secondary);align-items:center;">
        <span style="font-size:13px;font-weight:600;">📡 策略信号</span>
        <select id="signals-filter-market" class="select" style="width:90px;font-size:11px;">
          <option value="all">全部</option>
          <option value="crypto">加密</option>
          <option value="us">美股</option>
          <option value="hk">港股</option>
          <option value="cn">A股</option>
        </select>
        <button id="signals-refresh-btn" class="btn btn-sm">🔄 刷新</button>
        <span style="font-size:11px;color:var(--text-tertiary);">置信度 ≥ 60 才触发</span>
        <span id="signals-status" style="font-size:11px;color:var(--text-tertiary);margin-left:auto;"></span>
      </div>
      <div class="signals-list" style="overflow-y:auto;height:calc(100% - 40px);"></div>
    `;
    pane.querySelector('#signals-filter-market').addEventListener('change', (e) => {
      _filterMarket = e.target.value;
      render();
    });
    pane.querySelector('#signals-refresh-btn').addEventListener('click', refresh);
  }

  async function refresh() {
    try {
      const resp = await fetch('/api/signals?limit=100');
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const d = await resp.json();
      _items = d.items || [];
      render();
      const statusEl = document.querySelector('.bottom-pane[data-pane="signals"] #signals-status');
      if (statusEl) statusEl.textContent = `共 ${_items.length} 个 · ${new Date().toLocaleTimeString()}`;
    } catch (e) {
      console.warn('[Signals] 刷新失败:', e);
    }
  }

  function render() {
    const listEl = document.querySelector('.bottom-pane[data-pane="signals"] .signals-list');
    if (!listEl) return;
    let visible = _items;
    if (_filterMarket !== 'all') visible = _items.filter((s) => s.market === _filterMarket);
    if (!visible.length) {
      listEl.innerHTML = `<div style="padding:40px;text-align:center;color:var(--text-tertiary);">暂无信号<br><span style="font-size:11px;">加密 6 币种已自动绑定全部策略 + 60s 检查一次</span></div>`;
      return;
    }
    listEl.innerHTML = `
      <table style="width:100%;font-size:12px;border-collapse:collapse;">
        <thead>
          <tr style="border-bottom:1px solid var(--border-secondary);color:var(--text-tertiary);">
            <th style="padding:6px 8px;text-align:left;">时间</th>
            <th style="padding:6px 8px;text-align:left;">品种</th>
            <th style="padding:6px 8px;text-align:left;">操作</th>
            <th style="padding:6px 8px;text-align:right;">价格</th>
            <th style="padding:6px 8px;text-align:right;">置信度</th>
            <th style="padding:6px 8px;text-align:left;">策略</th>
            <th style="padding:6px 8px;text-align:left;">理由</th>
            <th style="padding:6px 8px;text-align:right;">止损/止盈</th>
            <th style="padding:6px 8px;text-align:center;">操作</th>
          </tr>
        </thead>
        <tbody>${visible.map(_renderRow).join('')}</tbody>
      </table>
    `;
    listEl.querySelectorAll('[data-action="view"]').forEach((el) => {
      el.addEventListener('click', () => {
        const sym = el.dataset.symbol;
        const mkt = el.dataset.market;
        if (typeof switchMarket === 'function' && mkt !== window.currentMarket) {
          switchMarket(mkt);
          setTimeout(() => switchSymbol && switchSymbol(sym, mkt), 200);
        } else if (typeof switchSymbol === 'function') {
          switchSymbol(sym, mkt);
        }
      });
    });
  }

  function _renderRow(s) {
    const time = new Date(s.generated_at).toLocaleString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', month: 'numeric', day: 'numeric' });
    const actionColor = ACTION_COLOR[s.action] || '';
    const slTp = (s.stop_loss || s.take_profit)
      ? `SL ${s.stop_loss?.toFixed(2) || '-'} / TP ${s.take_profit?.toFixed(2) || '-'}`
      : '-';
    return `
      <tr style="border-bottom:1px solid var(--border-secondary);">
        <td style="padding:5px 8px;color:var(--text-tertiary);font-size:11px;">${time}</td>
        <td style="padding:5px 8px;font-weight:600;">${s.symbol}</td>
        <td style="padding:5px 8px;color:${actionColor};font-weight:600;">${ACTION_ICON[s.action] || s.action}</td>
        <td style="padding:5px 8px;text-align:right;">${(s.price || 0).toFixed(4)}</td>
        <td style="padding:5px 8px;text-align:right;font-weight:600;color:${s.confidence >= 80 ? 'var(--color-up)' : s.confidence >= 70 ? 'var(--color-warning)' : 'var(--text-secondary)'};">${s.confidence}</td>
        <td style="padding:5px 8px;color:var(--text-secondary);font-size:11px;">${s.strategy_name}</td>
        <td style="padding:5px 8px;color:var(--text-tertiary);max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${s.reason || ''}">${s.reason || '-'}</td>
        <td style="padding:5px 8px;text-align:right;font-size:11px;color:var(--text-tertiary);">${slTp}</td>
        <td style="padding:5px 8px;text-align:center;">
          <button class="btn btn-sm" data-action="view" data-symbol="${s.symbol}" data-market="${s.market}" style="font-size:10px;padding:2px 6px;">看图</button>
        </td>
      </tr>
    `;
  }

  return { init, refresh, render };
})();

window.Signals = Signals;
