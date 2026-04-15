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
  let _autoRefreshTimer = null;

  function init() {
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
    }
    refresh();
    _autoRefreshTimer = setInterval(refresh, 30000);
    console.log('[Portfolio] 已初始化');
  }

  function _ensureDom() {
    const pane = document.querySelector('.bottom-pane[data-pane="portfolio"]');
    if (!pane || pane.dataset.inited) return;
    pane.dataset.inited = '1';
    pane.innerHTML = `
      <div class="portfolio-toolbar" style="display:flex;gap:8px;padding:8px;border-bottom:1px solid var(--border-secondary);align-items:center;">
        <span style="font-size:13px;font-weight:600;">💼 持仓管理</span>
        <button id="port-add-btn" class="btn btn-primary btn-sm">+ 添加持仓</button>
        <button id="port-refresh-btn" class="btn btn-sm">🔄 刷新</button>
        <span style="font-size:11px;color:var(--text-tertiary);">建议每 5 分钟自动检查一次</span>
        <span id="port-status" style="font-size:11px;color:var(--text-tertiary);margin-left:auto;"></span>
      </div>
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
      <div class="portfolio-list" style="overflow-y:auto;height:calc(100% - 40px);"></div>
    `;
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
    } catch (e) {
      console.error('[Portfolio] 添加失败', e);
    }
  }

  async function refresh() {
    try {
      const resp = await fetch('/api/positions');
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      _items = await resp.json();
      render();
      const statusEl = document.querySelector('.bottom-pane[data-pane="portfolio"] #port-status');
      if (statusEl) statusEl.textContent = `共 ${_items.length} 个持仓 · ${new Date().toLocaleTimeString()}`;
    } catch (e) {
      console.warn('[Portfolio] 刷新失败:', e);
    }
  }

  function render() {
    const listEl = document.querySelector('.bottom-pane[data-pane="portfolio"] .portfolio-list');
    if (!listEl) return;
    if (!_items.length) {
      listEl.innerHTML = `<div style="padding:40px;text-align:center;color:var(--text-tertiary);">暂无持仓<br><span style="font-size:11px;">点击「+ 添加持仓」录入持仓信息，系统会自动监控并给出操作建议</span></div>`;
      return;
    }
    listEl.innerHTML = `
      <table style="width:100%;font-size:12px;border-collapse:collapse;">
        <thead>
          <tr style="border-bottom:1px solid var(--border-secondary);color:var(--text-tertiary);">
            <th style="padding:6px 8px;text-align:left;">品种</th>
            <th style="padding:6px 8px;text-align:left;">市场</th>
            <th style="padding:6px 8px;text-align:right;">数量</th>
            <th style="padding:6px 8px;text-align:right;">成本</th>
            <th style="padding:6px 8px;text-align:left;">备注</th>
            <th style="padding:6px 8px;text-align:left;">建仓时间</th>
            <th style="padding:6px 8px;text-align:center;">操作</th>
          </tr>
        </thead>
        <tbody>${_items.map(_renderRow).join('')}</tbody>
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
    listEl.querySelectorAll('[data-action="advice"]').forEach((el) => {
      el.addEventListener('click', async () => {
        const id = el.dataset.id;
        try {
          const resp = await fetch(`/api/positions/${id}/advices?limit=20`);
          const arr = await resp.json();
          if (!arr.length) {
            alert('暂无历史建议（持仓监控每 5 分钟检查一次）');
            return;
          }
          alert(arr.map(a => `[${new Date(a.advised_at*1000).toLocaleString()}] ${a.advice}: ${a.reason}`).join('\n\n'));
        } catch (e) {
          console.warn(e);
        }
      });
    });
    listEl.querySelectorAll('[data-action="remove"]').forEach((el) => {
      el.addEventListener('click', async () => {
        const id = el.dataset.id;
        if (!confirm('确认删除此持仓？')) return;
        try {
          await fetch(`/api/positions/${id}`, { method: 'DELETE' });
          if (typeof showToast === 'function') showToast('已删除', 'success');
          await refresh();
        } catch (e) {}
      });
    });
  }

  function _renderRow(p) {
    const time = p.opened_at ? new Date(p.opened_at * 1000).toLocaleDateString() : '-';
    return `
      <tr style="border-bottom:1px solid var(--border-secondary);">
        <td style="padding:5px 8px;font-weight:600;">${p.symbol}</td>
        <td style="padding:5px 8px;color:var(--text-secondary);">${p.market.toUpperCase()}</td>
        <td style="padding:5px 8px;text-align:right;">${p.quantity}</td>
        <td style="padding:5px 8px;text-align:right;">${p.avg_cost.toFixed(4)}</td>
        <td style="padding:5px 8px;color:var(--text-tertiary);">${p.notes || '-'}</td>
        <td style="padding:5px 8px;color:var(--text-tertiary);">${time}</td>
        <td style="padding:5px 8px;text-align:center;">
          <button class="btn btn-sm" data-action="view" data-symbol="${p.symbol}" data-market="${p.market}" style="font-size:10px;padding:2px 6px;">看图</button>
          <button class="btn btn-sm" data-action="advice" data-id="${p.id}" style="font-size:10px;padding:2px 6px;">建议</button>
          <button class="btn btn-sm" data-action="remove" data-id="${p.id}" style="font-size:10px;padding:2px 6px;color:var(--color-down);">×</button>
        </td>
      </tr>
    `;
  }

  return { init, refresh, render };
})();

window.Portfolio = Portfolio;
