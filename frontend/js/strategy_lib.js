// v12.16.2 策略库面板 — 桌面端
// 显示 22 个策略，按类别分组，含中文名 + 描述 + 各市场参数

(function() {
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));

  const STRATEGY_NAME_CN = {
    ma_cross: '均线金叉死叉', donchian_breakout: '唐奇安通道突破',
    bollinger_reversion: '布林带均值回归', volume_breakout: '成交量突破',
    flash_event: '新闻事件驱动', chanlun: '缠论买卖点',
    macd_cross: 'MACD 金叉死叉', ema_triple: 'EMA 三线排列',
    squeeze_breakout: '布林挤压突破', adx_trend_follow: 'ADX 趋势跟随',
    funding_extreme: '资金费率极值', oi_breakout: 'OI 持仓突破',
    long_short_ratio: '多空比反转', fear_greed_reversal: 'F&G 极值反转',
    limit_up_followup: '涨停后回踩', northbound_flow_top: '北向资金排名',
    sector_momentum: '板块联动',
    southbound_inflow: '港股通南向', ah_spread_revert: 'AH 价差回归',
    gap_up_continuation: '高开延续', vwap_pullback: 'VWAP 回踩',
    earnings_window_filter: '财报窗口过滤',
  };

  const GROUPS = [
    { name: '🌐 通用型策略（全市场）',
      strategies: ['ma_cross','donchian_breakout','bollinger_reversion','volume_breakout','flash_event','chanlun','macd_cross','ema_triple','squeeze_breakout','adx_trend_follow'] },
    { name: '💰 加密专属（OKX 衍生品 + F&G）',
      strategies: ['funding_extreme','oi_breakout','long_short_ratio','fear_greed_reversal'] },
    { name: '🇨🇳 A 股专属（东财数据）',
      strategies: ['limit_up_followup','northbound_flow_top','sector_momentum'] },
    { name: '🇭🇰 港股专属（港股通 + AH 价差）',
      strategies: ['southbound_inflow','ah_spread_revert'] },
    { name: '🇺🇸 美股专属（盘前/财报/VWAP）',
      strategies: ['gap_up_continuation','vwap_pullback','earnings_window_filter'] },
  ];

  const RESONANCE_GOLDEN_COMBOS = [
    { name: '趋势 + 量能起爆', strategies: ['ma_cross','volume_breakout','macd_cross'] },
    { name: '反转底部确认', strategies: ['bollinger_reversion','volume_breakout','ema_triple'] },
    { name: '突破共振', strategies: ['donchian_breakout','squeeze_breakout','adx_trend_follow'] },
    { name: '加密 Smart Money', strategies: ['funding_extreme','oi_breakout','long_short_ratio'] },
    { name: '加密极值反转', strategies: ['fear_greed_reversal','funding_extreme'] },
    { name: '缠论 + 量能确认', strategies: ['chanlun','volume_breakout'] },
    { name: 'A 股政策资金', strategies: ['northbound_flow_top','sector_momentum','limit_up_followup'] },
  ];

  const pane = document.querySelector('[data-pane="strategy-lib"]');
  if (!pane) return;

  let _strategies = null;
  let _signalStats = null;

  async function loadStrategies() {
    if (_strategies) return _strategies;
    const r = await fetch('/api/strategies');
    _strategies = await r.json();
    return _strategies;
  }

  async function loadSignalStats() {
    // 拉近 7 天信号统计每个 strategy 的触发频次 + confirm 率
    if (_signalStats) return _signalStats;
    try {
      const r = await fetch('/api/signals?limit=500');
      const d = await r.json();
      const stats = {};
      for (const s of (d.items || [])) {
        const n = s.strategy_name || '';
        if (!stats[n]) stats[n] = { total: 0, confirm: 0, reject: 0, warn: 0 };
        stats[n].total += 1;
        const v = s.ai_verdict || '';
        if (v === 'confirm') stats[n].confirm += 1;
        else if (v === 'reject') stats[n].reject += 1;
        else if (v === 'warn') stats[n].warn += 1;
      }
      _signalStats = stats;
    } catch { _signalStats = {}; }
    return _signalStats;
  }

  async function render() {
    pane.innerHTML = '<div style="padding:20px;color:var(--text-secondary);">加载中…</div>';
    try {
      const [strategies, stats] = await Promise.all([loadStrategies(), loadSignalStats()]);
      const byName = {};
      strategies.forEach(s => byName[s.name] = s);
      let html = `<div style="padding:16px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;">
          <h2 style="margin:0;font-size:18px;">📚 策略库 (共 ${strategies.length} 个)</h2>
          <button id="strat-lib-refresh" class="btn-secondary" style="padding:6px 12px;">🔄 刷新</button>
        </div>`;

      // 黄金组合区块
      html += `<div style="background:var(--bg-card-2);padding:12px;border-radius:8px;margin-bottom:16px;">
        <h3 style="margin:0 0 10px 0;font-size:14px;color:var(--accent);">🌟 黄金共振组合（命中即 conf=100，仓位 ×1.5）</h3>
        <div style="display:grid;grid-template-columns:repeat(auto-fill, minmax(280px, 1fr));gap:8px;">`;
      for (const c of RESONANCE_GOLDEN_COMBOS) {
        html += `<div style="padding:8px 10px;background:var(--bg-card);border-radius:6px;border-left:3px solid var(--accent);">
          <div style="font-weight:600;font-size:13px;">${esc(c.name)}</div>
          <div style="font-size:11px;color:var(--text-tertiary);margin-top:3px;">${c.strategies.map(s => STRATEGY_NAME_CN[s]||s).join(' + ')}</div>
        </div>`;
      }
      html += `</div></div>`;

      // 按分组
      for (const grp of GROUPS) {
        html += `<h3 style="margin:18px 0 10px 0;font-size:14px;color:var(--text-secondary);">${esc(grp.name)}</h3>`;
        html += `<div style="display:grid;grid-template-columns:repeat(auto-fill, minmax(360px, 1fr));gap:10px;">`;
        for (const n of grp.strategies) {
          const s = byName[n];
          if (!s) continue;
          const cn = STRATEGY_NAME_CN[n] || n;
          const st = stats[n] || { total: 0, confirm: 0, reject: 0, warn: 0 };
          const confirmRate = st.total > 0 ? (st.confirm / st.total * 100).toFixed(0) : '-';
          const params = s.default_params || {};
          let paramsStr = Object.entries(params).map(([k,v]) => `${k}=${v}`).join(', ') || '无';
          html += `<div style="padding:10px 12px;background:var(--bg-card);border-radius:6px;border:1px solid var(--border);">
            <div style="display:flex;justify-content:space-between;align-items:baseline;">
              <span style="font-weight:600;font-size:14px;">${esc(cn)}</span>
              <span style="font-size:11px;color:var(--text-tertiary);">${esc(n)}</span>
            </div>
            <div style="font-size:12px;color:var(--text-secondary);margin-top:6px;line-height:1.4;">${esc(s.description || '')}</div>
            <div style="font-size:11px;color:var(--text-tertiary);margin-top:6px;">默认参数: ${esc(paramsStr)}</div>
            <div style="font-size:11px;margin-top:6px;display:flex;gap:10px;">
              <span>近 500 信号: <b>${st.total}</b></span>
              <span style="color:var(--color-up);">已确认: <b>${st.confirm}</b></span>
              <span style="color:var(--color-down);">已拒绝: <b>${st.reject}</b></span>
              <span style="color:var(--accent);">确认率: <b>${confirmRate}%</b></span>
            </div>
          </div>`;
        }
        html += `</div>`;
      }
      html += `</div>`;
      pane.innerHTML = html;
      const refreshBtn = document.getElementById('strat-lib-refresh');
      if (refreshBtn) {
        refreshBtn.addEventListener('click', () => {
          _strategies = null; _signalStats = null;
          render();
        });
      }
    } catch (e) {
      pane.innerHTML = `<div style="padding:20px;color:var(--color-down);">加载失败：${e.message}</div>`;
    }
  }

  // 监听 tab 切换
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.bottom-tab[data-tab="strategy-lib"]');
    if (!btn) return;
    if (!pane.dataset.loaded) {
      pane.dataset.loaded = '1';
      render();
    }
  });
})();
