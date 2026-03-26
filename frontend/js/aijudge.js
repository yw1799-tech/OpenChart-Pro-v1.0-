/* ============================================================
   OpenChart Pro - AI 智能研判
   结合K线、技术指标、新闻情绪，给出综合开仓建议
   ============================================================ */

const AIJudge = (() => {

  function init() {
    document.getElementById('aijudge-run')?.addEventListener('click', () => {
      const symbol = window.currentSymbol || 'BTC-USDT';
      const interval = document.getElementById('aijudge-interval')?.value || '1D';
      analyze(symbol, interval);
    });
  }

  async function analyze(symbol, interval) {
    if (!interval) interval = document.getElementById('aijudge-interval')?.value || '1D';
    const market = window.currentMarket === 'a' ? 'cn' : (window.currentMarket || 'crypto');

    // 切换到AI研判标签
    if (typeof switchBottomTab === 'function') switchBottomTab('aijudge');
    if (typeof expandBottomPanel === 'function') expandBottomPanel();

    // 更新标题
    const symEl = document.getElementById('aijudge-symbol');
    if (symEl) symEl.textContent = symbol;

    const content = document.getElementById('aijudge-content');
    if (!content) return;

    content.innerHTML = `<div style="text-align:center;padding:40px;color:var(--text-secondary);">
      <div style="font-size:24px;margin-bottom:12px;">🤖</div>
      <div>正在分析 <strong>${symbol}</strong> (${interval})...</div>
      <div style="font-size:11px;color:var(--text-tertiary);margin-top:8px;">AI正在读取K线数据、计算技术指标、分析新闻情绪</div>
      <div style="margin-top:16px;"><div class="chart-spinner" style="margin:auto;"></div></div>
    </div>`;

    try {
      const resp = await fetch('/api/aijudge/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol, market, interval }),
      });
      const data = await resp.json();

      if (data.error) {
        content.innerHTML = `<div style="color:var(--color-down);padding:20px;text-align:center;">${data.error}</div>`;
        return;
      }

      renderResult(content, data);
    } catch (e) {
      content.innerHTML = `<div style="color:var(--color-down);padding:20px;text-align:center;">研判失败: ${e.message}</div>`;
    }
  }

  function renderResult(container, data) {
    const v = data.verdict || {};
    const direction = v.direction || '观望';
    const confidence = v.confidence || 50;

    // 方向颜色
    let dirColor = 'var(--color-warning)';
    let dirIcon = '⏸️';
    let dirBg = 'rgba(245,158,11,0.1)';
    if (direction.includes('多') || direction.includes('买')) {
      dirColor = 'var(--color-up)'; dirIcon = '📈'; dirBg = 'rgba(0,200,83,0.1)';
    } else if (direction.includes('空') || direction.includes('卖')) {
      dirColor = 'var(--color-down)'; dirIcon = '📉'; dirBg = 'rgba(255,23,68,0.1)';
    }

    // 置信度颜色
    let confColor = confidence >= 70 ? 'var(--color-up)' : confidence >= 40 ? 'var(--color-warning)' : 'var(--color-down)';

    let html = `<div class="aijudge-result">`;

    // ═══ 研判结论（大卡片） ═══
    html += `<div class="aijudge-card aijudge-verdict" style="background:${dirBg};border-color:${dirColor};">
      <div style="font-size:12px;color:var(--text-tertiary);">${data.symbol} · ${data.interval} 周期研判</div>
      <div class="verdict-action" style="color:${dirColor};">${dirIcon} ${direction}</div>
      <div style="display:flex;justify-content:center;gap:24px;margin:8px 0;">
        <div><span style="font-size:11px;color:var(--text-tertiary);">置信度</span><br><span style="font-size:20px;font-weight:700;color:${confColor};">${confidence}%</span></div>
        ${v.entry_price ? `<div><span style="font-size:11px;color:var(--text-tertiary);">入场价</span><br><span style="font-size:16px;font-weight:600;">${fmtNum(v.entry_price)}</span></div>` : ''}
        ${v.stop_loss ? `<div><span style="font-size:11px;color:var(--text-tertiary);">止损</span><br><span style="font-size:16px;font-weight:600;color:var(--color-down);">${fmtNum(v.stop_loss)}</span></div>` : ''}
        ${v.take_profit ? `<div><span style="font-size:11px;color:var(--text-tertiary);">止盈</span><br><span style="font-size:16px;font-weight:600;color:var(--color-up);">${fmtNum(v.take_profit)}</span></div>` : ''}
      </div>
      ${v.position_pct ? `<div style="font-size:12px;color:var(--text-secondary);">建议仓位: <strong>${v.position_pct}%</strong> · 持仓周期: <strong>${v.timeframe || '-'}</strong></div>` : ''}
    </div>`;

    // ═══ 分析理由 ═══
    html += `<div class="aijudge-card full">
      <h5>📋 开仓理由</h5>
      <div style="font-size:12px;color:var(--text-primary);line-height:1.8;">${v.reasoning || '暂无'}</div>
      ${v.entry_logic ? `<div style="margin-top:8px;padding:8px;background:rgba(0,200,83,0.08);border-radius:4px;font-size:12px;"><span style="color:var(--color-up);font-weight:600;">📥 入场逻辑：</span> ${v.entry_logic}</div>` : ''}
      ${v.exit_logic ? `<div style="margin-top:4px;padding:8px;background:rgba(33,150,243,0.08);border-radius:4px;font-size:12px;"><span style="color:var(--color-accent);font-weight:600;">📤 出场逻辑：</span> ${v.exit_logic}</div>` : ''}
      ${v.risk_warning ? `<div style="margin-top:4px;padding:8px;background:rgba(255,23,68,0.08);border-radius:4px;font-size:12px;"><span style="color:var(--color-down);font-weight:600;">⚠️ 风险提示：</span> ${v.risk_warning}</div>` : ''}
    </div>`;

    // ═══ 市场数据 ═══
    if (data.onchain && Object.keys(data.onchain).length) {
      const sectionTitle = data.market === 'crypto' ? '⛓️ 链上数据 & 情绪' :
                           data.market === 'cn' ? '📊 量价分析 & 涨跌停' : '📊 成交量 & 波动率';
      html += `<div class="aijudge-card">
        <h5>${sectionTitle}</h5>`;
      for (const [k, val] of Object.entries(data.onchain)) {
        html += `<div class="aijudge-indicator-row"><span style="color:var(--text-tertiary);">${k}</span><span>${val}</span></div>`;
      }
      html += `</div>`;
    }

    // ═══ 技术指标 ═══
    html += `<div class="aijudge-card">
      <h5>📊 技术指标</h5>`;
    if (data.indicators) {
      for (const [k, val] of Object.entries(data.indicators)) {
        const isUp = String(val).includes('+');
        const isDown = String(val).includes('-') && !k.includes('MA');
        const valColor = isUp ? 'var(--color-up)' : isDown ? 'var(--color-down)' : 'var(--text-primary)';
        html += `<div class="aijudge-indicator-row"><span style="color:var(--text-tertiary);">${k}</span><span style="color:${valColor};">${val}</span></div>`;
      }
    }
    html += `</div>`;

    // ═══ 技术信号 ═══
    html += `<div class="aijudge-card">
      <h5>🎯 技术信号</h5>`;
    if (data.tech_signals) {
      data.tech_signals.forEach(s => {
        const isBull = s.includes('多') || s.includes('金叉') || s.includes('超卖') || s.includes('之上') || s.includes('正');
        const isBear = s.includes('空') || s.includes('死叉') || s.includes('超买') || s.includes('之下') || s.includes('负') || s.includes('跌破');
        const color = isBull ? 'var(--color-up)' : isBear ? 'var(--color-down)' : 'var(--text-secondary)';
        const icon = isBull ? '🟢' : isBear ? '🔴' : '🟡';
        html += `<div style="font-size:11px;padding:3px 0;display:flex;gap:6px;align-items:center;">
          <span>${icon}</span><span style="color:${color};">${s}</span>
        </div>`;
      });
    }
    html += `</div>`;

    // ═══ 关键价位 ═══
    if (v.key_levels) {
      html += `<div class="aijudge-card">
        <h5>📐 关键价位</h5>
        <div style="font-size:11px;margin-bottom:4px;color:var(--color-up);">支撑位</div>`;
      (v.key_levels.support || []).forEach(p => {
        html += `<div class="aijudge-indicator-row"><span style="color:var(--color-up);">S</span><span>${fmtNum(p)}</span></div>`;
      });
      html += `<div style="font-size:11px;margin:6px 0 4px;color:var(--color-down);">压力位</div>`;
      (v.key_levels.resistance || []).forEach(p => {
        html += `<div class="aijudge-indicator-row"><span style="color:var(--color-down);">R</span><span>${fmtNum(p)}</span></div>`;
      });
      html += `</div>`;
    }

    // ═══ 相关新闻 ═══
    html += `<div class="aijudge-card">
      <h5>📰 近期新闻</h5>`;
    if (data.news && data.news.length) {
      data.news.forEach(n => {
        html += `<div style="font-size:11px;padding:3px 0;border-bottom:1px solid var(--border-secondary);">
          <span style="color:var(--text-tertiary);">[${n.source || ''}]</span> ${n.title || ''}
        </div>`;
      });
    } else {
      html += `<div style="font-size:11px;color:var(--text-tertiary);">暂无相关新闻</div>`;
    }
    html += `</div>`;

    html += `</div>`; // close aijudge-result
    container.innerHTML = html;
  }

  function fmtNum(n) {
    if (n == null) return '--';
    n = parseFloat(n);
    if (n >= 10000) return n.toLocaleString('en-US', {maximumFractionDigits: 0});
    if (n >= 1000) return n.toLocaleString('en-US', {maximumFractionDigits: 2});
    if (n >= 1) return n.toFixed(2);
    return n.toFixed(4);
  }

  // 页面加载后初始化
  document.addEventListener('DOMContentLoaded', init);

  return { init, analyze };
})();
