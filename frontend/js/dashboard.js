/* ============================================================
   OpenChart Pro - 加密货币仪表盘
   调用后端 /api/dashboard/* 各子端点
   ============================================================ */

const Dashboard = (() => {
  let refreshTimer = null;
  let loaded = false;

  function init() {
    // 不自动加载，等用户切换到仪表盘标签页再加载
  }

  async function refresh() {
    await loadAll();
  }

  async function loadAll() {
    console.log('[Dashboard] loadAll() called, market=' + window.currentMarket);
    if (window.currentMarket !== 'crypto' && window.currentMarket !== undefined) {
      const container = document.querySelector('[data-pane="dashboard"]');
      if (container) {
        container.innerHTML = '<div style="color:var(--text-tertiary);padding:40px;text-align:center;">仪表盘仅在加密货币市场可用</div>';
      }
      return;
    }

    const container = document.querySelector('[data-pane="dashboard"]');
    if (!container) return;

    // 显示加载状态
    container.innerHTML = '<div style="color:var(--text-secondary);padding:20px;text-align:center;">加载仪表盘数据...</div>';

    // 并行请求所有数据
    const [fearGreed, fundingRate, oi, lsRatio, calendar] = await Promise.allSettled([
      fetchJSON('/api/dashboard/fear-greed'),
      fetchJSON('/api/dashboard/funding-rate?symbol=BTC-USDT-SWAP'),
      fetchJSON('/api/dashboard/open-interest?symbol=BTC-USDT-SWAP'),
      fetchJSON('/api/dashboard/long-short-ratio?coin=BTC'),
      fetchJSON('/api/dashboard/calendar'),
    ]);

    const fg = fearGreed.status === 'fulfilled' ? fearGreed.value : null;
    const fr = fundingRate.status === 'fulfilled' ? fundingRate.value : null;
    const oiData = oi.status === 'fulfilled' ? oi.value : null;
    const ls = lsRatio.status === 'fulfilled' ? lsRatio.value : null;
    const cal = calendar.status === 'fulfilled' ? calendar.value : null;

    // 渲染
    let html = '<div class="dashboard-grid-inner">';

    // 恐惧贪婪指数
    html += buildCard('恐惧贪婪指数', renderFearGreed(fg));

    // 资金费率
    html += buildCard('资金费率 (BTC)', renderFundingRate(fr));

    // 持仓量
    html += buildCard('持仓量 (BTC)', renderOI(oiData));

    // 多空比
    html += buildCard('多空比 (BTC)', renderLSRatio(ls));

    // 经济日历
    html += buildCard('经济日历 & 加密事件', renderCalendar(cal), true);

    html += '</div>';
    container.innerHTML = html;
    loaded = true;

    // 30秒后自动刷新
    clearInterval(refreshTimer);
    refreshTimer = setInterval(loadAll, 60000);
  }

  function renderFearGreed(data) {
    if (!data) return '<span style="color:var(--text-tertiary)">加载失败</span>';
    const value = data.value || 0;
    const label = data.label || '';
    const labelCN = {'Extreme Fear':'极度恐惧','Fear':'恐惧','Neutral':'中性','Greed':'贪婪','Extreme Greed':'极度贪婪'}[label] || label;
    // 颜色
    let color = '#F59E0B';
    if (value <= 25) color = '#FF1744';
    else if (value <= 40) color = '#FF6B6B';
    else if (value <= 60) color = '#F59E0B';
    else if (value <= 75) color = '#00C853';
    else color = '#00E676';

    return `
      <div style="text-align:center;padding:8px 0;">
        <div style="font-size:36px;font-weight:700;color:${color};">${value}</div>
        <div style="font-size:13px;color:${color};margin-top:4px;">${labelCN}</div>
        <div style="margin-top:8px;height:6px;background:var(--bg-tertiary);border-radius:3px;overflow:hidden;">
          <div style="width:${value}%;height:100%;background:linear-gradient(90deg,#FF1744,#FF6B6B,#F59E0B,#00C853,#00E676);border-radius:3px;"></div>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text-tertiary);margin-top:2px;">
          <span>极度恐惧</span><span>极度贪婪</span>
        </div>
      </div>
    `;
  }

  function renderFundingRate(data) {
    if (!data) return '<span style="color:var(--text-tertiary)">加载失败</span>';
    const current = data.current || {};
    const rate = current.fundingRate || current.rate || data.current_rate || data.rate || 0;
    const rateNum = parseFloat(rate);
    const ratePct = (rateNum * 100).toFixed(4);
    const color = rateNum >= 0 ? 'var(--color-up)' : 'var(--color-down)';
    const annualized = current.annualized || data.annualized_rate || (rateNum * 3 * 365 * 100);

    return `
      <div style="text-align:center;padding:8px 0;">
        <div style="font-size:28px;font-weight:700;color:${color};">${ratePct}%</div>
        <div style="font-size:11px;color:var(--text-secondary);margin-top:4px;">
          年化: ${typeof annualized === 'number' ? annualized.toFixed(2) : annualized}%
        </div>
        <div style="font-size:11px;color:var(--text-tertiary);margin-top:4px;">
          ${rateNum > 0 ? '多头付费给空头' : rateNum < 0 ? '空头付费给多头' : '均衡'}
        </div>
      </div>
    `;
  }

  function renderOI(data) {
    if (!data) return '<span style="color:var(--text-tertiary)">加载失败</span>';
    const oi = data.oi || data.open_interest || data.openInterest || 0;
    return `
      <div style="text-align:center;padding:8px 0;">
        <div style="font-size:24px;font-weight:700;color:var(--text-primary);">${fmtNum(parseFloat(oi))}</div>
        <div style="font-size:11px;color:var(--text-tertiary);margin-top:4px;">BTC 合约持仓量</div>
      </div>
    `;
  }

  function renderLSRatio(data) {
    if (!data) return '<span style="color:var(--text-tertiary)">加载失败</span>';
    const current = data.current || {};
    const ratio = parseFloat(current.ratio || data.ratio || data.long_short_ratio || 1);
    const longPct = (ratio / (1 + ratio) * 100).toFixed(1);
    const shortPct = (100 - parseFloat(longPct)).toFixed(1);

    return `
      <div style="padding:8px 0;">
        <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:6px;">
          <span style="color:var(--color-up);">多 ${longPct}%</span>
          <span style="font-weight:600;">${ratio.toFixed(2)}</span>
          <span style="color:var(--color-down);">空 ${shortPct}%</span>
        </div>
        <div style="height:8px;background:var(--color-down);border-radius:4px;overflow:hidden;">
          <div style="width:${longPct}%;height:100%;background:var(--color-up);border-radius:4px 0 0 4px;"></div>
        </div>
      </div>
    `;
  }

  function renderCalendar(data) {
    if (!data) return '<span style="color:var(--text-tertiary)">加载失败</span>';
    const events = data.events || [];
    const macro = data.macro_events || [];
    const crypto = data.crypto_events || [];
    const all = [
      ...events.map(e => ({...e, cat: e.type || '事件'})),
      ...macro.map(e => ({...e, cat: '宏观'})),
      ...crypto.map(e => ({...e, cat: '加密'})),
    ];

    if (all.length === 0) return '<div style="color:var(--text-tertiary);font-size:12px;">暂无近期事件</div>';

    // 按时间排序，取前15条
    all.sort((a, b) => new Date(a.time || 0) - new Date(b.time || 0));
    return all.slice(0, 15).map(e => {
      // 转换为北京时间 (UTC+8)
      let timeStr = '';
      if (e.time) {
        const d = new Date(e.time);
        // 转北京时间
        const beijing = new Date(d.getTime() + (8 - (-d.getTimezoneOffset() / 60)) * 3600000);
        const month = beijing.getMonth() + 1;
        const day = beijing.getDate();
        const hour = String(beijing.getHours()).padStart(2, '0');
        const min = String(beijing.getMinutes()).padStart(2, '0');
        timeStr = `${month}月${day}日 ${hour}:${min}`;
      }
      const imp = e.importance === 'high' ? '🔴' : e.importance === 'medium' ? '🟡' : '⚪';
      // 预期值和前值
      let extra = '';
      if (e.forecast || e.previous) {
        extra = `<span style="color:var(--text-tertiary);font-size:10px;margin-left:4px;">`;
        if (e.forecast) extra += `预期:${e.forecast} `;
        if (e.previous) extra += `前值:${e.previous}`;
        extra += '</span>';
      }
      return `
        <div style="display:flex;gap:8px;padding:5px 0;border-bottom:1px solid var(--border-secondary);font-size:11px;align-items:center;">
          <span style="min-width:90px;color:var(--text-tertiary);font-size:10px;">${timeStr}</span>
          <span>${imp}</span>
          <span style="flex:1;color:var(--text-primary);">${e.event || e.title || ''}${extra}</span>
          <span style="color:var(--text-tertiary);font-size:10px;">${e.cat}</span>
        </div>
      `;
    }).join('');
  }

  function buildCard(title, content, wide) {
    return `<div class="dashboard-card${wide ? ' wide' : ''}"><div class="dashboard-card-title">${title}</div><div class="dashboard-card-body">${content}</div></div>`;
  }

  function fmtNum(n) {
    if (n == null || isNaN(n)) return '--';
    if (n >= 1e9) return (n/1e9).toFixed(2) + 'B';
    if (n >= 1e6) return (n/1e6).toFixed(2) + 'M';
    if (n >= 1e3) return (n/1e3).toFixed(1) + 'K';
    return n.toFixed(2);
  }

  async function fetchJSON(url) {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return await resp.json();
  }

  return { init, refresh, loadAll };
})();
