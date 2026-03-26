/* ============================================================
   OpenChart Pro - 警报管理
   ============================================================ */

const Alerts = (() => {
  let overlay = null;
  let alertList = [];

  function init() {
    overlay = document.getElementById('alert-modal');
    if (!overlay) return;

    overlay.querySelector('.modal-close')?.addEventListener('click', close);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    overlay.querySelector('#alert-create-btn')?.addEventListener('click', createAlert);

    // 从服务器加载已有警报
    loadAlerts();

    // WebSocket 警报触发
    ws.on('alert_triggered', (data) => {
      showToast(`⚡ 警报触发: ${data.symbol} ${data.message}`, 'warning', 8000);
      logAlert(data);
      if (typeof Notification !== 'undefined' && Notification.permission === 'granted') {
        new Notification('OpenChart Pro 警报', { body: `${data.symbol}: ${data.message}` });
      }
    });
  }

  function open() {
    if (!overlay) return;
    populateForm();
    overlay.classList.add('show');
  }

  function close() {
    if (!overlay) return;
    overlay.classList.remove('show');
  }

  function populateForm() {
    const symbolInput = overlay.querySelector('#alert-symbol');
    if (symbolInput) symbolInput.value = window.currentSymbol || '';
  }

  async function loadAlerts() {
    try {
      const resp = await fetch('/api/alerts');
      if (resp.ok) {
        const data = await resp.json();
        alertList = data.alerts || data.data || (Array.isArray(data) ? data : []);
        renderAlertList();
        renderInfoPanelAlerts();
      }
    } catch (e) {
      console.warn('[Alerts] 加载警报失败:', e);
    }
  }

  async function createAlert() {
    const symbol = overlay.querySelector('#alert-symbol')?.value?.trim();
    const condition = overlay.querySelector('#alert-condition')?.value;
    const price = parseFloat(overlay.querySelector('#alert-price')?.value);
    const message = overlay.querySelector('#alert-message')?.value?.trim() || '';
    const repeat = overlay.querySelector('#alert-repeat')?.checked || false;

    if (!symbol || !price) {
      showToast('请填写品种和价格', 'warning');
      return;
    }

    try {
      const resp = await fetch('/api/alerts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol, condition, price, message, repeat }),
      });
      if (resp.ok) {
        showToast('警报已创建', 'success');
        close();
        loadAlerts();
      } else {
        throw new Error(`HTTP ${resp.status}`);
      }
    } catch (e) {
      showToast(`创建警报失败: ${e.message}`, 'error');
    }
  }

  function renderAlertList() {
    const container = document.querySelector('.alert-log-content');
    if (!container) return;
    if (alertList.length === 0) {
      container.innerHTML = '<div style="color:var(--text-tertiary);padding:20px;text-align:center;">暂无警报</div>';
      return;
    }
    let html = `<table class="alert-log-table">
      <thead><tr><th>品种</th><th>条件</th><th>价格</th><th>状态</th><th>操作</th></tr></thead><tbody>`;
    for (const a of alertList) {
      html += `<tr>
        <td>${a.symbol}</td>
        <td>${a.condition || '穿越'}</td>
        <td class="mono">${a.price}</td>
        <td>${a.triggered ? '已触发' : '等待中'}</td>
        <td><button class="btn btn-sm btn-danger" onclick="Alerts.remove('${a.id}')">删除</button></td>
      </tr>`;
    }
    html += '</tbody></table>';
    container.innerHTML = html;
  }

  function renderInfoPanelAlerts() {
    const container = document.querySelector('.info-alerts');
    if (!container) return;
    container.innerHTML = '';
    const pending = alertList.filter(a => !a.triggered).slice(0, 5);
    if (pending.length === 0) {
      container.innerHTML = '<div style="color:var(--text-tertiary);font-size:11px;padding:4px 0;">暂无活跃警报</div>';
      return;
    }
    for (const a of pending) {
      const el = document.createElement('div');
      el.className = 'info-alert-item';
      el.innerHTML = `<span class="info-alert-dot"></span><span>${a.symbol} ${a.condition || '≥'} ${a.price}</span>`;
      container.appendChild(el);
    }
  }

  function logAlert(data) {
    const output = document.querySelector('.alert-log-content');
    if (!output) return;
    // 在日志面板追加记录
    loadAlerts();
  }

  async function remove(id) {
    try {
      await fetch(`/api/alerts/${id}`, { method: 'DELETE' });
      showToast('警报已删除', 'info', 2000);
      loadAlerts();
    } catch (e) {
      showToast('删除失败', 'error');
    }
  }

  return { init, open, close, remove, loadAlerts };
})();
