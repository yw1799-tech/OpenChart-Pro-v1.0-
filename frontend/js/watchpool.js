/* ============================================================
   WatchPool 模块 — 候选池面板 (Phase 3A，仅股票市场)

   功能：
   - 列表展示候选池（按评分降序）
   - 手动添加股票（带市场约束，加密拒绝）
   - 移除条目
   - WebSocket 推送更新（pool_update）
   - 加密市场时整个面板隐藏
   ============================================================ */

const Watchpool = (function () {
  let _items = [];
  let _filterMarket = 'all';

  function init() {
    _ensureDom();
    if (typeof ws !== 'undefined' && ws) {
      ws.on('pool_update', (msg) => {
        if (!msg) return;
        // 任何变更都重新拉取
        refresh();
      });
    }
    refresh();
    console.log('[WatchPool] 已初始化');
  }

  function _ensureDom() {
    const pane = document.querySelector('.bottom-pane[data-pane="watchpool"]');
    if (!pane) {
      console.warn('[WatchPool] 未找到 [data-pane="watchpool"] 容器');
      return;
    }
    if (pane.dataset.inited) return;
    pane.dataset.inited = '1';
    pane.innerHTML = `
      <div class="pool-toolbar" style="display:flex;gap:8px;padding:8px;border-bottom:1px solid var(--border-secondary);align-items:center;">
        <span style="font-size:13px;font-weight:600;">📊 候选池 <span style="font-weight:normal;color:var(--text-tertiary);font-size:11px;">(仅股票)</span></span>
        <select id="pool-filter-market" class="select" style="width:90px;font-size:11px;">
          <option value="all">全部</option>
          <option value="us">美股</option>
          <option value="hk">港股</option>
          <option value="cn">A股</option>
        </select>
        <button id="pool-add-btn" class="btn btn-primary btn-sm">+ 添加</button>
        <button id="pool-refresh-btn" class="btn btn-sm">🔄 刷新</button>
        <span id="pool-status" style="font-size:11px;color:var(--text-tertiary);margin-left:auto;"></span>
      </div>
      <div class="pool-add-form" style="display:none;padding:8px;background:var(--bg-tertiary);border-bottom:1px solid var(--border-secondary);">
        <div style="display:flex;gap:6px;align-items:center;">
          <input id="pool-add-symbol" class="input" placeholder="股票代码 (如 NVDA / 600519)" style="flex:1;">
          <select id="pool-add-market" class="select" style="width:80px;">
            <option value="us">美股</option>
            <option value="hk">港股</option>
            <option value="cn">A股</option>
          </select>
          <input id="pool-add-reason" class="input" placeholder="备注 (可选)" style="flex:1;">
          <button id="pool-add-submit" class="btn btn-primary btn-sm">确认</button>
          <button id="pool-add-cancel" class="btn btn-sm">取消</button>
        </div>
      </div>
      <div class="pool-list" style="overflow-y:auto;height:calc(100% - 40px);"></div>
    `;
    const $ = (s) => pane.querySelector(s);
    $('#pool-filter-market').addEventListener('change', (e) => {
      _filterMarket = e.target.value;
      render();
    });
    $('#pool-refresh-btn').addEventListener('click', refresh);
    $('#pool-add-btn').addEventListener('click', () => {
      const f = $('.pool-add-form');
      f.style.display = f.style.display === 'none' ? 'block' : 'none';
    });
    $('#pool-add-cancel').addEventListener('click', () => {
      $('.pool-add-form').style.display = 'none';
    });
    $('#pool-add-submit').addEventListener('click', _onAddSubmit);
  }

  async function _onAddSubmit() {
    const pane = document.querySelector('.bottom-pane[data-pane="watchpool"]');
    if (!pane) return;
    const symbol = pane.querySelector('#pool-add-symbol').value.trim();
    const market = pane.querySelector('#pool-add-market').value;
    const reason = pane.querySelector('#pool-add-reason').value.trim();
    if (!symbol) {
      if (typeof showToast === 'function') showToast('请输入股票代码', 'warning');
      return;
    }
    try {
      const resp = await fetch('/api/pool', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol, market, reason: reason || '手动添加', score: 50 }),
      });
      const d = await resp.json();
      if (!resp.ok) {
        if (typeof showToast === 'function') showToast(d.detail || '添加失败', 'error');
        return;
      }
      if (typeof showToast === 'function') showToast(`已添加 ${symbol} 到候选池`, 'success');
      pane.querySelector('#pool-add-symbol').value = '';
      pane.querySelector('#pool-add-reason').value = '';
      pane.querySelector('.pool-add-form').style.display = 'none';
      await refresh();
    } catch (e) {
      console.error('[WatchPool] 添加失败', e);
      if (typeof showToast === 'function') showToast('网络异常', 'error');
    }
  }

  async function refresh() {
    try {
      const resp = await fetch('/api/pool?limit=200');
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const d = await resp.json();
      _items = d.items || [];
      render();
      const statusEl = document.querySelector('.bottom-pane[data-pane="watchpool"] #pool-status');
      if (statusEl) statusEl.textContent = `共 ${_items.length} 只 · ${new Date().toLocaleTimeString()}`;
    } catch (e) {
      console.warn('[WatchPool] 刷新失败:', e);
    }
  }

  function render() {
    const listEl = document.querySelector('.bottom-pane[data-pane="watchpool"] .pool-list');
    if (!listEl) return;
    let visible = _items;
    if (_filterMarket !== 'all') {
      visible = _items.filter((it) => it.market === _filterMarket);
    }
    if (!visible.length) {
      listEl.innerHTML = `<div style="padding:40px;text-align:center;color:var(--text-tertiary);">候选池为空。<br><span style="font-size:11px;">高分新闻自动入池，或点击「+ 添加」手动加入</span></div>`;
      return;
    }
    listEl.innerHTML = `
      <table style="width:100%;font-size:12px;border-collapse:collapse;">
        <thead>
          <tr style="border-bottom:1px solid var(--border-secondary);color:var(--text-tertiary);">
            <th style="padding:6px 8px;text-align:left;">品种</th>
            <th style="padding:6px 8px;text-align:left;">市场</th>
            <th style="padding:6px 8px;text-align:right;">评分</th>
            <th style="padding:6px 8px;text-align:left;">状态</th>
            <th style="padding:6px 8px;text-align:left;">来源</th>
            <th style="padding:6px 8px;text-align:left;">理由</th>
            <th style="padding:6px 8px;text-align:left;">添加时间</th>
            <th style="padding:6px 8px;text-align:center;">操作</th>
          </tr>
        </thead>
        <tbody>
          ${visible.map(_renderRow).join('')}
        </tbody>
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
    listEl.querySelectorAll('[data-action="remove"]').forEach((el) => {
      el.addEventListener('click', async () => {
        const id = el.dataset.id;
        if (!confirm('从候选池移除此品种？')) return;
        try {
          await fetch(`/api/pool/${id}`, { method: 'DELETE' });
          if (typeof showToast === 'function') showToast('已移除', 'success');
          await refresh();
        } catch (e) {
          console.warn('[WatchPool] 移除失败:', e);
        }
      });
    });
  }

  function _renderRow(item) {
    const time = item.added_at ? new Date(item.added_at * 1000).toLocaleDateString() : '-';
    const sourceColor = {
      news: 'color:var(--color-accent);',
      anomaly: 'color:var(--color-warning);',
      macro_theme: 'color:var(--color-purple);',
      manual: 'color:var(--text-secondary);',
    }[item.source] || '';
    return `
      <tr style="border-bottom:1px solid var(--border-secondary);">
        <td style="padding:5px 8px;font-weight:600;">${item.symbol}</td>
        <td style="padding:5px 8px;color:var(--text-secondary);">${item.market.toUpperCase()}</td>
        <td style="padding:5px 8px;text-align:right;color:var(--color-warning);font-weight:600;">${Math.round(item.score)}</td>
        <td style="padding:5px 8px;color:var(--text-secondary);">${item.status}</td>
        <td style="padding:5px 8px;${sourceColor}">${item.source}</td>
        <td style="padding:5px 8px;color:var(--text-tertiary);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${item.reason || ''}">${item.reason || '-'}</td>
        <td style="padding:5px 8px;color:var(--text-tertiary);">${time}</td>
        <td style="padding:5px 8px;text-align:center;">
          <button class="btn btn-sm" data-action="view" data-symbol="${item.symbol}" data-market="${item.market}" style="font-size:10px;padding:2px 6px;">看图</button>
          <button class="btn btn-sm" data-action="remove" data-id="${item.id}" style="font-size:10px;padding:2px 6px;color:var(--color-down);">×</button>
        </td>
      </tr>
    `;
  }

  return { init, refresh, render };
})();

window.Watchpool = Watchpool;
